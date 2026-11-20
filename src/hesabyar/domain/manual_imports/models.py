"""Manual-import batch domain models (US-554, Phase 5-6 ۵ح).

One batch cycle for every manual import (statement B1 + settlement
P2): upload → format / column-mapping detection → validation +
dedup → accounting-effect preview → review item → approve → the
normal pipeline → batch variance report.

The batch is an ORCHESTRATION record, not a second import engine:
the parse / validation / dedup / matching / policy-gate / tie-out
work stays in the US-514 bank import and US-523 settlement import
services, and the batch wraps one underlying import
(``source_import_id``: a ``bank_import_batches`` id for statements,
a ``settlement_file_imports`` id for settlements). The batch adds
the cycle the underlying imports lack: a staged state machine, a
stored accounting-effect preview computed by the SAME code as the
real posting effect, rejection-with-reason + archiving, re-runs,
and the batch variance report.

File bytes are never stored — only the filename + SHA-256 hash for
provenance and file-hash dedupe. The parse stages (uploaded →
mapped → validated → previewed) complete inside the upload call
(the file is parsed once, from the uploaded bytes); the stage
history records each stage with its timestamp, and the batch is
born ``awaiting_review`` with its preview computed and a
``manual_import_review`` queue item filed — exactly like the
underlying imports. A staged upload (persist the file between
steps) would need the grid stored between calls; it is deferred
(see docs/manual-imports.md).

Money is whole Rial everywhere (spec §19 rule 7): a file in Toman
converts ×10 at the import boundary, explicitly, with the file's
unit recorded on the batch — never guessed.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from enum import StrEnum, unique

from hesabyar.domain.bank.models import (
    ColumnMappingStatus,
    FileCurrencyUnit,
    StatementFileFormat,
)

__all__ = [
    "BatchVarianceReport",
    "ImportBatchPreview",
    "ManualImportBatch",
    "ManualImportBatchStatus",
    "ManualImportKind",
    "ManualImportStage",
    "SettlementColumnMapping",
]


@unique
class ManualImportKind(StrEnum):
    """What kind of file a batch imports."""

    STATEMENT = "statement"
    SETTLEMENT = "settlement"


@unique
class ManualImportBatchStatus(StrEnum):
    """Lifecycle of a manual-import batch (US-554).

    The happy path is ``uploaded → mapped → validated → previewed →
    awaiting_review → approved → posted``. The parse stages complete
    inside the upload call and are recorded in the stage history;
    ``awaiting_review`` is the first long-lived state. A rejected
    batch is archived with its reason (``rejected → archived``) and
    is re-runnable (``archived → mapped`` starts a new attempt on
    the same batch — the stored rows are re-validated, never
    silently dropped).
    """

    UPLOADED = "uploaded"
    MAPPED = "mapped"
    VALIDATED = "validated"
    PREVIEWED = "previewed"
    AWAITING_REVIEW = "awaiting_review"
    APPROVED = "approved"
    POSTED = "posted"
    REJECTED = "rejected"
    ARCHIVED = "archived"


@dataclass(frozen=True)
class ManualImportStage:
    """One recorded stage transition (the batch's audit trail).

    ``stage`` is a :class:`ManualImportBatchStatus` value; ``at`` is
    when the batch entered it; ``note`` is free text (e.g. the
    rejection reason on ``rejected``, the attempt number on a
    re-run).
    """

    stage: str
    at: datetime.datetime
    note: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {"stage": self.stage, "at": self.at.isoformat(), "note": self.note}

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> ManualImportStage:
        at_raw = raw.get("at")
        at = (
            datetime.datetime.fromisoformat(str(at_raw))
            if isinstance(at_raw, str)
            else datetime.datetime.now(datetime.UTC)
        )
        note = raw.get("note")
        return cls(
            stage=str(raw.get("stage", "")),
            at=at,
            note=str(note) if isinstance(note, str) else None,
        )


@dataclass(frozen=True)
class ImportBatchPreview:
    """The accounting-effect preview of a batch (US-554).

    Computed by the SAME code that computes the real posting effect
    — the statement preview comes from the US-514 batch totals, the
    settlement preview from the US-523 ``ImportPreview`` — never by
    a second calculator. Statement batches have no fee/tax/refunds
    (0) and no channel receivable (None): a statement is money that
    already moved, not money a gateway owes.
    """

    gross_irr: int
    fee_irr: int
    tax_irr: int
    refunds_irr: int
    net_irr: int
    deductions_irr: int
    receivable_before_irr: int | None
    receivable_after_irr: int | None
    expected_deposit_irr: int
    matched_rows: int = 0
    new_rows: int = 0
    duplicate_rows: int = 0
    invalid_rows: int = 0
    dropped_rows: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "gross_irr": self.gross_irr,
            "fee_irr": self.fee_irr,
            "tax_irr": self.tax_irr,
            "refunds_irr": self.refunds_irr,
            "net_irr": self.net_irr,
            "deductions_irr": self.deductions_irr,
            "receivable_before_irr": self.receivable_before_irr,
            "receivable_after_irr": self.receivable_after_irr,
            "expected_deposit_irr": self.expected_deposit_irr,
            "matched_rows": self.matched_rows,
            "new_rows": self.new_rows,
            "duplicate_rows": self.duplicate_rows,
            "invalid_rows": self.invalid_rows,
            "dropped_rows": self.dropped_rows,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> ImportBatchPreview:
        def _int(key: str) -> int:
            value = raw.get(key)
            return int(value) if isinstance(value, int) else 0

        def _optional_int(key: str) -> int | None:
            value = raw.get(key)
            return int(value) if isinstance(value, int) else None

        return cls(
            gross_irr=_int("gross_irr"),
            fee_irr=_int("fee_irr"),
            tax_irr=_int("tax_irr"),
            refunds_irr=_int("refunds_irr"),
            net_irr=_int("net_irr"),
            deductions_irr=_int("deductions_irr"),
            receivable_before_irr=_optional_int("receivable_before_irr"),
            receivable_after_irr=_optional_int("receivable_after_irr"),
            expected_deposit_irr=_int("expected_deposit_irr"),
            matched_rows=_int("matched_rows"),
            new_rows=_int("new_rows"),
            duplicate_rows=_int("duplicate_rows"),
            invalid_rows=_int("invalid_rows"),
            dropped_rows=_int("dropped_rows"),
        )

    @classmethod
    def from_statement_totals(
        cls,
        *,
        total_deposits_irr: int,
        total_withdrawals_irr: int,
        new_rows: int,
        duplicate_rows: int,
        invalid_rows: int,
    ) -> ImportBatchPreview:
        """Build a statement preview from the US-514 batch totals.

        ``gross`` is the money moved (deposits + withdrawals);
        ``net`` is signed (deposits − withdrawals); the expected
        deposit is the money in (deposits). No fee/tax/refunds and
        no channel receivable exist for a statement.
        """
        return cls(
            gross_irr=total_deposits_irr + total_withdrawals_irr,
            fee_irr=0,
            tax_irr=0,
            refunds_irr=0,
            net_irr=total_deposits_irr - total_withdrawals_irr,
            deductions_irr=0,
            receivable_before_irr=None,
            receivable_after_irr=None,
            expected_deposit_irr=total_deposits_irr,
            matched_rows=0,
            new_rows=new_rows,
            duplicate_rows=duplicate_rows,
            invalid_rows=invalid_rows,
            dropped_rows=0,
        )


@dataclass(frozen=True)
class BatchVarianceReport:
    """Expected vs imported vs posted for one batch (US-554).

    ``expected_net_irr`` is the preview's expected deposit;
    ``imported_net_irr`` is what the pipeline actually imported
    (settlement event net / statement net); ``posted_net_irr`` is
    what reached the engine (bank-receipt postings / the draft
    statement). ``tieout_*`` is None for statement batches (no
    tie-out applies) and for settlement batches whose tie-out has
    not run yet. ``difference_irr`` is imported − posted: money
    imported but not yet posted is visible, never absorbed.
    """

    expected_net_irr: int
    imported_net_irr: int
    posted_net_irr: int
    difference_irr: int
    tieout_holds: bool | None
    tieout_difference_irr: int | None
    tieout_tolerance_irr: int | None
    gate_allowed: bool | None
    gate_reason: str | None
    posting_status: str | None
    generated_at: datetime.datetime

    def to_dict(self) -> dict[str, object]:
        return {
            "expected_net_irr": self.expected_net_irr,
            "imported_net_irr": self.imported_net_irr,
            "posted_net_irr": self.posted_net_irr,
            "difference_irr": self.difference_irr,
            "tieout_holds": self.tieout_holds,
            "tieout_difference_irr": self.tieout_difference_irr,
            "tieout_tolerance_irr": self.tieout_tolerance_irr,
            "gate_allowed": self.gate_allowed,
            "gate_reason": self.gate_reason,
            "posting_status": self.posting_status,
            "generated_at": self.generated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> BatchVarianceReport:
        def _int(key: str) -> int:
            value = raw.get(key)
            return int(value) if isinstance(value, int) else 0

        def _optional_int(key: str) -> int | None:
            value = raw.get(key)
            return int(value) if isinstance(value, int) else None

        def _optional_bool(key: str) -> bool | None:
            value = raw.get(key)
            return bool(value) if isinstance(value, bool) else None

        def _optional_str(key: str) -> str | None:
            value = raw.get(key)
            return str(value) if isinstance(value, str) else None

        generated_raw = raw.get("generated_at")
        generated_at = (
            datetime.datetime.fromisoformat(str(generated_raw))
            if isinstance(generated_raw, str)
            else datetime.datetime.now(datetime.UTC)
        )
        return cls(
            expected_net_irr=_int("expected_net_irr"),
            imported_net_irr=_int("imported_net_irr"),
            posted_net_irr=_int("posted_net_irr"),
            difference_irr=_int("difference_irr"),
            tieout_holds=_optional_bool("tieout_holds"),
            tieout_difference_irr=_optional_int("tieout_difference_irr"),
            tieout_tolerance_irr=_optional_int("tieout_tolerance_irr"),
            gate_allowed=_optional_bool("gate_allowed"),
            gate_reason=_optional_str("gate_reason"),
            posting_status=_optional_str("posting_status"),
            generated_at=generated_at,
        )


@dataclass(frozen=True)
class ManualImportBatch:
    """One manual-import batch: the cycle wrapper around an import.

    ``source_key`` is the bank key (statements) or channel key
    (settlements) — the source the column mapping is saved per.
    ``source_import_id`` is the underlying import's id
    (``bank_import_batches.id`` / ``settlement_file_imports.id``);
    ``source_import_batch_id`` is the settlement ``import_batch_id``
    (the tie-out key) — for statements it equals the batch id.
    ``attempt`` counts upload attempts on the same batch (1 = the
    original upload; a re-run increments it). ``posting_status``
    tracks the posting step separately from the cycle state:
    ``draft_hold`` (approved, auto-post not opted in),
    ``held_gate`` (tie-out held, gate denied), ``tieout_failed``,
    ``posting_failed`` or ``posted``. ``stages`` is the audit trail
    of every state the batch passed through.
    """

    id: str
    tenant_id: str
    installation_id: str
    company_id: str
    kind: ManualImportKind
    source_key: str
    filename: str
    file_format: StatementFileFormat
    file_hash_sha256: str
    currency_unit: FileCurrencyUnit
    status: ManualImportBatchStatus
    attempt: int = 1
    date_kind: str = "jalali"
    header_row: int = 0
    source_import_id: str | None = None
    source_import_batch_id: str | None = None
    column_mapping_id: str | None = None
    mapping_columns: dict[str, str] = field(default_factory=dict)
    file_type: str | None = None
    period_jalali: str | None = None
    statement_journal_id: int | None = None
    default_account_id: int | None = None
    preview: ImportBatchPreview | None = None
    review_item_id: str | None = None
    rejection_reason: str | None = None
    auto_post_opt_in: bool = False
    posting_status: str | None = None
    posting_detail: str | None = None
    engine_ref_id: int | None = None
    stages: tuple[ManualImportStage, ...] = ()
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None
    approved_at: datetime.datetime | None = None
    posted_at: datetime.datetime | None = None
    rejected_at: datetime.datetime | None = None
    archived_at: datetime.datetime | None = None

    @property
    def is_terminal(self) -> bool:
        """True when the batch cannot move again (posted; archived re-runs)."""
        return self.status == ManualImportBatchStatus.POSTED

    @property
    def can_rerun(self) -> bool:
        """True when the batch may be re-run (rejected/archived only)."""
        return self.status in (
            ManualImportBatchStatus.REJECTED,
            ManualImportBatchStatus.ARCHIVED,
        )


@dataclass(frozen=True)
class SettlementColumnMapping:
    """A saved per-channel settlement column mapping (US-554).

    The settlement twin of :class:`BankColumnMapping
    <hesabyar.domain.bank.models.BankColumnMapping>`: ``columns``
    maps a canonical settlement field (see
    ``ALL_SETTLEMENT_FIELDS``) to its source column — a header name
    (matched after normalisation) or a positional reference
    ``#<0-based index>`` for headerless files. ``company_id`` is
    None when the mapping is shared across the tenant's companies
    (a gateway's layout does not vary by company); a company-scoped
    mapping wins over a shared one for that company.

    Stored in the batch-scoped ``settlement_column_mappings``
    table, NOT the US-503 mapping center: the center's nine kinds
    are entity mappings (gateway → journal, category → account, …)
    with a closed vocabulary, while a column mapping is file
    layout per source — a different kind of mapping with a
    different lifecycle (see docs/manual-imports.md).
    """

    id: str
    tenant_id: str
    channel_key: str
    name: str
    file_format: StatementFileFormat
    file_type: str
    columns: dict[str, str]
    date_kind: str = "jalali"
    currency_unit: FileCurrencyUnit = FileCurrencyUnit.RIAL
    header_row: int = 0
    company_id: str | None = None
    status: ColumnMappingStatus = ColumnMappingStatus.ACTIVE
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None
