"""Manual-import batch use cases (US-554, Phase 5-6 ۵ح).

One batch cycle for every manual import (statement B1 + settlement
P2): upload → format / column-mapping detection (mapping savable
per source) → validation + dedup → accounting-effect preview →
review item → approve → the normal pipeline → batch variance
report.

The batch is an ORCHESTRATION record: the parse / validation /
dedup / matching / policy-gate / tie-out work stays in the US-514
bank import and US-523 settlement import services, and this
service wraps one underlying import in the staged cycle the
underlying imports lack — a stored preview computed by the SAME
code as the real posting effect, rejection-with-reason + archiving,
re-runs from the stored rows, and the batch variance report.

Draft by default (ADR 0003 decision 1): approving a batch confirms
the import (statement → draft statement; settlement → tranches
confirmed + settlement event) but posts NOTHING unless the
operator explicitly opts the batch into auto-post (``auto_post``)
— and even then the policy gate must allow it (the contract's own
opt-in + fiscal period + tie-out). A statement approve always
writes a DRAFT statement; there is no statement auto-post.

A rejected batch is archived with its reason and is re-runnable:
re-run reopens the underlying import from its stored rows (no
re-upload), recomputes the preview, and files a fresh review item
— nothing is silently dropped.

File bytes are never stored — only the filename + SHA-256 hash
for provenance and file-hash dedupe (re-uploading the same file
for the same source is a no-op returning the existing batch).
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from hesabyar.application.commands.bank_import import BankImportService, FileDetection
from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.application.commands.settlement_import import (
    IMPORT_APPROVED,
    IMPORT_AWAITING_REVIEW,
    IMPORT_EXCEPTION,
    IMPORT_REJECTED,
    IMPORT_SETTLED,
    ImportPreview,
    SettlementImportService,
    TieOutRunResult,
)
from hesabyar.domain.accounting.refs import CompanyRef
from hesabyar.domain.bank.models import (
    ColumnMappingStatus,
    FileCurrencyUnit,
    ImportBatchStatus,
    StatementFileFormat,
)
from hesabyar.domain.bank.ports import BankRepositoryPort, StatementFileReaderPort
from hesabyar.domain.manual_imports.errors import (
    ManualImportFileError,
    ManualImportInvalidStateError,
    ManualImportNotFoundError,
    SettlementColumnMappingInvalidError,
    SettlementColumnMappingNotFoundError,
)
from hesabyar.domain.manual_imports.models import (
    BatchVarianceReport,
    ImportBatchPreview,
    ManualImportBatch,
    ManualImportBatchStatus,
    ManualImportKind,
    ManualImportStage,
    SettlementColumnMapping,
)
from hesabyar.domain.manual_imports.ports import ManualImportRepositoryPort
from hesabyar.domain.manual_imports.state_machine import require_transition
from hesabyar.domain.review_queue.models import ReviewItemType, ReviewSeverity
from hesabyar.domain.settlement.commands import (
    ApproveSettlementImport,
    ImportSettlementFile,
    RejectSettlementImport,
)
from hesabyar.domain.settlement.import_parsing import (
    suggest_settlement_mapping,
    validate_settlement_mapping_columns,
)
from hesabyar.domain.settlement.matching import TieOutResult
from hesabyar.domain.settlement.ports import SettlementRepositoryPort

__all__ = [
    "ApproveBatchResult",
    "ManualImportService",
    "RetryPostingResult",
    "UploadBatchResult",
]

#: Posting-step statuses (stored on the batch's ``posting_status``).
POSTING_DRAFT_HOLD = "draft_hold"
POSTING_HELD_GATE = "held_gate"
POSTING_TIEOUT_FAILED = "tieout_failed"
POSTING_FAILED = "posting_failed"
POSTING_POSTED = "posted"


@dataclass(frozen=True)
class UploadBatchResult:
    """The outcome of an upload: the batch + its preview."""

    batch: ManualImportBatch
    preview: ImportBatchPreview
    duplicate: bool = False
    """True when the file was already imported (a no-op re-upload)."""


@dataclass(frozen=True)
class ApproveBatchResult:
    """The outcome of approving a batch."""

    batch: ManualImportBatch
    posted: bool
    """True when the engine write happened (statement draft / bank receipt)."""
    tryton_journal_id: int | None = None
    settlement_event_id: str | None = None
    tieout: TieOutResult | None = None
    gate_reason: str | None = None
    confirmed_tranche_ids: tuple[str, ...] = ()
    created_payment_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class RetryPostingResult:
    """The outcome of retrying the posting step of an approved batch."""

    batch: ManualImportBatch
    posted: bool
    tryton_journal_id: int | None = None
    tieout: TieOutResult | None = None
    gate_reason: str | None = None
    already_settled: bool = False
    """True when the underlying import had already settled (no re-post)."""


class ManualImportService:
    """The unified manual-import batch cycle over the import services."""

    def __init__(
        self,
        batches: ManualImportRepositoryPort,
        bank: BankRepositoryPort,
        bank_import: BankImportService,
        settlement_import: SettlementImportService,
        settlement: SettlementRepositoryPort,
        review: ReviewQueueCommandService,
        reader: StatementFileReaderPort,
        *,
        now: datetime.datetime | None = None,
    ) -> None:
        self._batches = batches
        self._bank = bank
        self._bank_import = bank_import
        self._settlement_import = settlement_import
        self._settlement = settlement
        self._review = review
        self._reader = reader
        self._now = now

    def _clock(self) -> datetime.datetime:
        if self._now is not None:
            return self._now
        return datetime.datetime.now(datetime.UTC)

    # -- detection -----------------------------------------------------------

    def detect_file(
        self,
        *,
        kind: ManualImportKind,
        content: bytes,
        filename: str,
        file_type: str = "detail",
    ) -> FileDetection:
        """Detect a file's format + suggest a column mapping (no persistence).

        Statements delegate to the US-514 detector; settlements use
        the same reader + the US-523 synonym map. Unknown formats
        raise — the file is quarantined, never processed on a guess.
        """
        if kind == ManualImportKind.STATEMENT:
            return self._bank_import.detect_file(content=content, filename=filename)
        detected = self._reader.read_file(content, filename)
        if not detected.grid:
            raise ManualImportFileError("The file has no rows.")
        headers = [str(cell or "") for cell in detected.grid[0]]
        suggested = suggest_settlement_mapping(headers)
        problems = validate_settlement_mapping_columns(suggested, file_type)
        sample = tuple(
            tuple(str(cell) if cell is not None else None for cell in row)
            for row in detected.grid[1:6]
        )
        return FileDetection(
            file_format=detected.file_format,
            encoding=detected.encoding,
            columns=tuple(headers),
            sample_rows=sample,
            suggested_mapping=suggested,
            missing_fields=tuple(problems),
            detection_notes=detected.detection_notes,
        )

    # -- settlement column mappings ------------------------------------------

    def save_settlement_mapping(
        self,
        *,
        tenant_id: str,
        channel_key: str,
        name: str,
        file_format: StatementFileFormat,
        file_type: str,
        columns: dict[str, str],
        date_kind: str = "jalali",
        currency_unit: FileCurrencyUnit = FileCurrencyUnit.RIAL,
        header_row: int = 0,
        company_id: str | None = None,
    ) -> SettlementColumnMapping:
        """Save a per-channel settlement column mapping (validated first)."""
        problems = validate_settlement_mapping_columns(columns, file_type)
        if date_kind not in ("jalali", "gregorian"):
            problems.append(f"unknown date_kind {date_kind!r} (expected 'jalali' or 'gregorian')")
        if header_row < 0:
            problems.append("header_row must be >= 0")
        if problems:
            raise SettlementColumnMappingInvalidError(
                f"Column mapping is invalid: {'; '.join(problems)}.",
                details={"problems": problems},
            )
        now = self._clock()
        return self._batches.save_settlement_mapping(
            SettlementColumnMapping(
                id=str(uuid.uuid4()),
                tenant_id=tenant_id,
                channel_key=channel_key,
                name=name,
                file_format=file_format,
                file_type=file_type,
                columns=dict(columns),
                date_kind=date_kind,
                currency_unit=currency_unit,
                header_row=header_row,
                company_id=company_id,
                status=ColumnMappingStatus.ACTIVE,
                created_at=now,
                updated_at=now,
            )
        )

    def supersede_settlement_mapping(
        self, *, tenant_id: str, mapping_id: str
    ) -> SettlementColumnMapping:
        """Supersede a saved settlement mapping (stays for provenance)."""
        mapping = self._batches.get_settlement_mapping(mapping_id, tenant_id)
        if mapping is None:
            raise SettlementColumnMappingNotFoundError(
                f"Settlement column mapping {mapping_id!r} does not exist.",
                details={"mapping_id": mapping_id},
            )
        return self._batches.update_settlement_mapping(
            dataclasses.replace(mapping, status=ColumnMappingStatus.SUPERSEDED)
        )

    def list_settlement_mappings(
        self,
        tenant_id: str,
        channel_key: str | None = None,
        company_id: str | None = None,
    ) -> Sequence[SettlementColumnMapping]:
        """Saved settlement mappings (active first, newest first)."""
        return self._batches.list_settlement_mappings(tenant_id, channel_key, company_id)

    def get_settlement_mapping(
        self, tenant_id: str, mapping_id: str
    ) -> SettlementColumnMapping:
        """One settlement mapping for the owning tenant (404 when missing)."""
        mapping = self._batches.get_settlement_mapping(mapping_id, tenant_id)
        if mapping is None:
            raise SettlementColumnMappingNotFoundError(
                f"Settlement column mapping {mapping_id!r} does not exist.",
                details={"mapping_id": mapping_id},
            )
        return mapping

    # -- upload ---------------------------------------------------------------

    def upload_statement_batch(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        company: CompanyRef,
        company_id: str,
        bank_key: str,
        statement_journal_id: int,
        filename: str,
        content: bytes,
        currency_unit: FileCurrencyUnit,
        default_account_id: int,
        mapping_id: str | None = None,
        mapping_columns: dict[str, str] | None = None,
        save_mapping: bool = False,
        mapping_name: str | None = None,
        date_kind: str = "jalali",
        header_row: int = 0,
        actor: str,
    ) -> UploadBatchResult:
        """Upload a statement file into a batch awaiting review.

        The parse stages (uploaded → mapped → validated → previewed)
        complete inside this call — the file is parsed once, from
        the uploaded bytes — and are recorded in the stage history;
        the batch is born ``awaiting_review`` with its preview
        computed and a ``manual_import_review`` queue item filed.
        Re-uploading the same file for the same bank is a no-op
        returning the existing batch.
        """
        file_hash = hashlib.sha256(content).hexdigest()
        existing = self._batches.get_batch_by_file_hash(
            tenant_id, ManualImportKind.STATEMENT, bank_key, file_hash
        )
        if existing is not None and existing.preview is not None:
            return UploadBatchResult(batch=existing, preview=existing.preview, duplicate=True)
        if save_mapping and mapping_columns is not None and mapping_id is None:
            # Detect the format with the BANK reader (the same reader
            # create_import validates the mapping against) — NOT the
            # settlement reader: a CSV statement detected as XLSX
            # would make create_import reject its own mapping.
            detected_format = self._bank_import.detect_file(
                content=content, filename=filename
            ).file_format
            saved = self._bank_import.save_mapping(
                tenant_id=tenant_id,
                bank_key=bank_key,
                name=mapping_name or f"{bank_key} — {filename}",
                file_format=detected_format,
                columns=mapping_columns,
                date_kind=date_kind,
                currency_unit=currency_unit,
                header_row=header_row,
                company_id=company_id,
            )
            mapping_id = saved.id
        bank_batch = self._bank_import.create_import(
            tenant_id=tenant_id,
            company=company,
            company_id=company_id,
            bank_key=bank_key,
            statement_journal_id=statement_journal_id,
            filename=filename,
            content=content,
            currency_unit=currency_unit,
            default_account_id=default_account_id,
            mapping_id=mapping_id,
            mapping_columns=mapping_columns,
            date_kind=date_kind,
            header_row=header_row,
            actor=actor,
        )
        preview = ImportBatchPreview.from_statement_totals(
            total_deposits_irr=bank_batch.total_deposits_irr,
            total_withdrawals_irr=bank_batch.total_withdrawals_irr,
            new_rows=bank_batch.new_rows,
            duplicate_rows=bank_batch.duplicate_rows,
            invalid_rows=bank_batch.invalid_rows,
        )
        now = self._clock()
        batch = self._batches.save_batch(
            ManualImportBatch(
                id=str(uuid.uuid4()),
                tenant_id=tenant_id,
                installation_id=installation_id,
                company_id=company_id,
                kind=ManualImportKind.STATEMENT,
                source_key=bank_key,
                filename=filename,
                file_format=bank_batch.file_format,
                file_hash_sha256=file_hash,
                currency_unit=currency_unit,
                status=ManualImportBatchStatus.AWAITING_REVIEW,
                attempt=1,
                date_kind=date_kind,
                header_row=header_row,
                source_import_id=bank_batch.id,
                source_import_batch_id=bank_batch.id,
                column_mapping_id=mapping_id,
                mapping_columns=dict(mapping_columns or {}),
                statement_journal_id=statement_journal_id,
                default_account_id=default_account_id,
                preview=preview,
                review_item_id=bank_batch.review_item_id,
                stages=_upload_stages(now),
                created_at=now,
                updated_at=now,
            )
        )
        return UploadBatchResult(batch=batch, preview=preview, duplicate=False)

    def upload_settlement_batch(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        company_id: str,
        channel_key: str,
        file_type: str,
        period_jalali: str,
        filename: str,
        content: bytes,
        currency_unit: str,
        mapping_id: str | None = None,
        mapping_columns: dict[str, str] | None = None,
        save_mapping: bool = False,
        mapping_name: str | None = None,
        date_kind: str = "jalali",
        header_row: int = 0,
        actor: str,
    ) -> UploadBatchResult:
        """Upload a settlement file into a batch awaiting review.

        Same cycle as statements: the parse stages complete inside
        this call and are recorded in the stage history; the batch
        is born ``awaiting_review`` with the US-523 preview and a
        ``manual_import_review`` queue item. Re-uploading the same
        file for the same channel is a no-op returning the existing
        batch (the underlying import's file-hash dedupe agrees).
        """
        file_hash = hashlib.sha256(content).hexdigest()
        existing = self._batches.get_batch_by_file_hash(
            tenant_id, ManualImportKind.SETTLEMENT, channel_key, file_hash
        )
        if existing is not None and existing.preview is not None:
            return UploadBatchResult(batch=existing, preview=existing.preview, duplicate=True)
        detected = self._reader.read_file(content, filename)
        columns = mapping_columns
        if mapping_id is not None:
            mapping = self._batches.get_settlement_mapping(mapping_id, tenant_id)
            if mapping is None:
                raise SettlementColumnMappingNotFoundError(
                    f"Settlement column mapping {mapping_id!r} does not exist.",
                    details={"mapping_id": mapping_id},
                )
            columns = dict(mapping.columns)
            date_kind = mapping.date_kind
            header_row = mapping.header_row
        if save_mapping and mapping_columns is not None and mapping_id is None:
            saved = self.save_settlement_mapping(
                tenant_id=tenant_id,
                channel_key=channel_key,
                name=mapping_name or f"{channel_key} — {filename}",
                file_format=detected.file_format,
                file_type=file_type,
                columns=mapping_columns,
                date_kind=date_kind,
                currency_unit=FileCurrencyUnit(currency_unit),
                header_row=header_row,
                company_id=company_id,
            )
            mapping_id = saved.id
        result = self._settlement_import.import_file(
            ImportSettlementFile(
                installation_id=installation_id,
                channel_key=channel_key,
                file_type=file_type,
                period_jalali=period_jalali,
                filename=filename,
                content=content,
                currency_unit=currency_unit,
                columns=columns,
                date_kind=date_kind,
                header_row=header_row,
                uploaded_by=actor,
            ),
            tenant_id=tenant_id,
        )
        preview = _preview_from_settlement(result.preview)
        now = self._clock()
        status = _batch_status_from_import(result.file_import.status)
        batch = self._batches.save_batch(
            ManualImportBatch(
                id=str(uuid.uuid4()),
                tenant_id=tenant_id,
                installation_id=installation_id,
                company_id=company_id,
                kind=ManualImportKind.SETTLEMENT,
                source_key=channel_key,
                filename=filename,
                file_format=detected.file_format,
                file_hash_sha256=file_hash,
                currency_unit=FileCurrencyUnit(currency_unit),
                status=status,
                attempt=1,
                date_kind=date_kind,
                header_row=header_row,
                source_import_id=result.file_import.id,
                source_import_batch_id=result.file_import.import_batch_id,
                column_mapping_id=mapping_id,
                mapping_columns=dict(columns or {}),
                file_type=file_type,
                period_jalali=period_jalali,
                preview=preview,
                review_item_id=result.review_item_id,
                stages=_upload_stages(now),
                created_at=now,
                updated_at=now,
            )
        )
        return UploadBatchResult(batch=batch, preview=preview, duplicate=result.duplicate)

    # -- approve / reject / re-run ---------------------------------------------

    def approve_batch(
        self,
        *,
        tenant_id: str,
        batch_id: str,
        company: CompanyRef,
        auto_post: bool = False,
        actor: str,
    ) -> ApproveBatchResult:
        """Approve a batch: the approved batch flows through the normal pipeline.

        Statements: the US-514 confirm writes the statement to the
        engine as a DRAFT (draft by default; there is no statement
        auto-post) and the batch posts. Settlements: the US-523
        approve confirms tranches + creates the settlement event;
        the tie-out + auto-post runs ONLY with explicit
        ``auto_post`` opt-in (and the policy gate must still allow
        it). Without the opt-in the batch stays ``approved`` with
        ``posting_status=draft_hold``.
        """
        batch = self._require_batch(batch_id, tenant_id)
        require_transition(batch.status, ManualImportBatchStatus.APPROVED)
        now = self._clock()
        if batch.kind == ManualImportKind.STATEMENT:
            assert batch.source_import_id is not None
            confirmed = self._bank_import.confirm_import(
                tenant_id=tenant_id,
                batch_id=batch.source_import_id,
                company=company,
                actor=actor,
            )
            approved = self._transition(
                batch,
                ManualImportBatchStatus.APPROVED,
                now,
                note="تأیید شد — صورت‌حساب به‌صورت پیش‌نویس در موتور ثبت می‌شود.",
            )
            posted = self._transition(
                approved,
                ManualImportBatchStatus.POSTED,
                now,
                note="صورت‌حساب پیش‌نویس ثبت شد.",
            )
            saved = self._batches.update_batch(
                dataclasses.replace(
                    posted,
                    auto_post_opt_in=auto_post,
                    posting_status=POSTING_POSTED,
                    posting_detail="Statement created as a draft in the engine.",
                    engine_ref_id=confirmed.engine_statement_id,
                    approved_at=now,
                    posted_at=now,
                )
            )
            return ApproveBatchResult(
                batch=saved, posted=True, tryton_journal_id=confirmed.engine_statement_id
            )
        assert batch.source_import_id is not None
        assert batch.source_import_batch_id is not None
        approved_import = self._settlement_import.approve_import(
            batch.installation_id,
            ApproveSettlementImport(import_id=batch.source_import_id, actor=actor),
            tenant_id=tenant_id,
        )
        approved = self._transition(
            batch,
            ManualImportBatchStatus.APPROVED,
            now,
            note="تأیید شد — قسط‌ها تأیید و رویداد تسویه ساخته شد.",
        )
        saved = self._batches.update_batch(
            dataclasses.replace(
                approved,
                auto_post_opt_in=auto_post,
                approved_at=now,
            )
        )
        if not auto_post:
            held = self._batches.update_batch(
                dataclasses.replace(
                    saved,
                    posting_status=POSTING_DRAFT_HOLD,
                    posting_detail=(
                        "Approved without auto-post opt-in: tranches confirmed, "
                        "settlement event created, nothing posted (draft by default)."
                    ),
                )
            )
            return ApproveBatchResult(
                batch=held,
                posted=False,
                settlement_event_id=approved_import.settlement_event.id,
                confirmed_tranche_ids=approved_import.confirmed_tranche_ids,
                created_payment_ids=approved_import.created_payment_ids,
            )
        run = self._settlement_import.run_tieout_for_batch(
            batch.installation_id,
            tenant_id,
            batch.source_import_batch_id,
            company,
            actor,
        )
        return self._apply_tieout_run(
            saved,
            run,
            settlement_event_id=approved_import.settlement_event.id,
            confirmed_tranche_ids=approved_import.confirmed_tranche_ids,
            created_payment_ids=approved_import.created_payment_ids,
        )

    def reject_batch(
        self, *, tenant_id: str, batch_id: str, reason: str, actor: str
    ) -> ManualImportBatch:
        """Reject a batch: archive it with its reason; nothing is dropped.

        The underlying import is rejected/cancelled first (its rows
        stay stored); the batch then moves ``rejected → archived``
        with the reason on both the batch and the stage history. An
        archived batch is re-runnable.
        """
        if not reason.strip():
            raise ManualImportInvalidStateError(
                "Rejecting a batch needs a reason.",
                details={"batch_id": batch_id},
            )
        batch = self._require_batch(batch_id, tenant_id)
        require_transition(batch.status, ManualImportBatchStatus.REJECTED)
        now = self._clock()
        if batch.kind == ManualImportKind.STATEMENT:
            assert batch.source_import_id is not None
            self._bank_import.cancel_import(
                tenant_id=tenant_id, batch_id=batch.source_import_id, actor=actor
            )
        else:
            assert batch.source_import_id is not None
            self._settlement_import.reject_import(
                batch.installation_id,
                RejectSettlementImport(
                    import_id=batch.source_import_id, actor=actor, reason=reason.strip()
                ),
                tenant_id=tenant_id,
            )
        rejected = self._transition(
            batch,
            ManualImportBatchStatus.REJECTED,
            now,
            note=reason.strip(),
        )
        archived = self._transition(
            rejected,
            ManualImportBatchStatus.ARCHIVED,
            now,
            note="بایگانی شد — ردیف‌ها و دلیل رد نگه داشته شد.",
        )
        return self._batches.update_batch(
            dataclasses.replace(
                archived,
                rejection_reason=reason.strip(),
                rejected_at=now,
                archived_at=now,
            )
        )

    def rerun_batch(self, *, tenant_id: str, batch_id: str, actor: str) -> ManualImportBatch:
        """Re-run an archived batch: a new attempt on the same batch.

        The underlying import reopens from its stored rows (no
        re-upload): the preview is recomputed, a fresh
        ``manual_import_review`` item is filed, and the batch
        re-traverses mapped → validated → previewed →
        awaiting_review with ``attempt`` incremented. The previous
        rejection stays in the stage history.
        """
        batch = self._require_batch(batch_id, tenant_id)
        if not batch.can_rerun:
            raise ManualImportInvalidStateError(
                f"Import batch {batch_id} is {batch.status.value} and cannot be re-run "
                "(only a rejected/archived batch can be re-run).",
                details={"batch_id": batch_id, "status": batch.status.value},
            )
        now = self._clock()
        current = batch
        if current.status == ManualImportBatchStatus.REJECTED:
            current = self._transition(
                current, ManualImportBatchStatus.ARCHIVED, now, note="بایگانی شد."
            )
        mapped = self._transition(
            current,
            ManualImportBatchStatus.MAPPED,
            now,
            note=f"اجرای دوباره — تلاش {current.attempt + 1}.",
        )
        if batch.kind == ManualImportKind.STATEMENT:
            preview, review_item_id = self._rerun_statement(batch, actor)
        else:
            preview, review_item_id = self._rerun_settlement(batch, actor)
        validated = self._transition(mapped, ManualImportBatchStatus.VALIDATED, now)
        previewed = self._transition(validated, ManualImportBatchStatus.PREVIEWED, now)
        awaiting = self._transition(
            previewed, ManualImportBatchStatus.AWAITING_REVIEW, now
        )
        return self._batches.update_batch(
            dataclasses.replace(
                awaiting,
                attempt=batch.attempt + 1,
                preview=preview,
                review_item_id=review_item_id,
                rejection_reason=None,
                rejected_at=None,
                archived_at=None,
                posting_status=None,
                posting_detail=None,
            )
        )

    def retry_posting(
        self,
        *,
        tenant_id: str,
        batch_id: str,
        company: CompanyRef,
        actor: str,
    ) -> RetryPostingResult:
        """Retry the posting step of an approved settlement batch.

        The tie-out may have held (gate denied, deposit missing) at
        approve time; once the deposit is recorded or the gate
        allows, this re-runs the same tie-out + auto-post path. A
        batch whose underlying import already settled syncs to
        ``posted`` without re-posting.
        """
        batch = self._require_batch(batch_id, tenant_id)
        if batch.kind != ManualImportKind.SETTLEMENT:
            raise ManualImportInvalidStateError(
                "Only a settlement batch has a posting step to retry.",
                details={"batch_id": batch_id, "kind": batch.kind.value},
            )
        if batch.status != ManualImportBatchStatus.APPROVED:
            raise ManualImportInvalidStateError(
                f"Import batch {batch_id} is {batch.status.value}; only an approved "
                "batch can retry its posting step.",
                details={"batch_id": batch_id, "status": batch.status.value},
            )
        assert batch.source_import_id is not None
        assert batch.source_import_batch_id is not None
        file_import = self._settlement.get_file_import(
            batch.installation_id, batch.source_import_id
        )
        if file_import is not None and file_import.status == IMPORT_SETTLED:
            # The import settled through the settlement tie-out
            # endpoint (or an event) — sync the batch, never re-post.
            now = self._clock()
            posted = self._transition(
                batch, ManualImportBatchStatus.POSTED, now, note="قبلاً تسویه شده بود."
            )
            saved = self._batches.update_batch(
                dataclasses.replace(
                    posted,
                    posting_status=POSTING_POSTED,
                    posting_detail="Underlying import already settled; batch synced, nothing re-posted.",
                    posted_at=now,
                )
            )
            return RetryPostingResult(batch=saved, posted=True, already_settled=True)
        run = self._settlement_import.run_tieout_for_batch(
            batch.installation_id,
            tenant_id,
            batch.source_import_batch_id,
            company,
            actor,
        )
        if run is None:
            raise ManualImportInvalidStateError(
                f"Import batch {batch_id} is not in a tie-out-able state.",
                details={"batch_id": batch_id, "status": batch.status.value},
            )
        result = self._apply_tieout_run(batch, run)
        return RetryPostingResult(
            batch=result.batch,
            posted=result.posted,
            tryton_journal_id=result.tryton_journal_id,
            tieout=result.tieout,
            gate_reason=result.gate_reason,
        )

    # -- reads -----------------------------------------------------------------

    def get_batch(self, tenant_id: str, batch_id: str) -> ManualImportBatch:
        """One batch for the owning tenant (404 when missing)."""
        return self._require_batch(batch_id, tenant_id)

    def list_batches(
        self,
        tenant_id: str,
        *,
        kind: ManualImportKind | None = None,
        status: ManualImportBatchStatus | None = None,
        company_id: str | None = None,
        source_import_id: str | None = None,
    ) -> Sequence[ManualImportBatch]:
        """Batches of one tenant, newest first (the dashboard reads this)."""
        return self._batches.list_batches(
            tenant_id,
            kind=kind,
            status=status,
            company_id=company_id,
            source_import_id=source_import_id,
        )

    def variance_report(
        self, *, tenant_id: str, batch_id: str
    ) -> BatchVarianceReport:
        """The batch variance report: expected vs imported vs posted.

        Expected is the preview's expected deposit (settlement) or
        signed net (statement); imported is what the pipeline
        actually imported (settlement event nets / statement net);
        posted is what reached the engine (the bank-receipt amount
        once posted / the draft statement's net once written). The
        tie-out figures are recomputed read-only from the same sums
        the tie-out itself uses — no second calculator.
        """
        batch = self._require_batch(batch_id, tenant_id)
        now = self._clock()
        preview = batch.preview
        expected = preview.expected_deposit_irr if preview is not None else 0
        if batch.kind == ManualImportKind.STATEMENT:
            expected = preview.net_irr if preview is not None else 0
            imported = expected
            posted = 0
            if batch.source_import_id is not None:
                bank_batch = self._bank.get_batch(batch.source_import_id, tenant_id)
                if bank_batch is not None:
                    imported = bank_batch.net_irr
                    if bank_batch.engine_statement_id is not None:
                        posted = bank_batch.net_irr
            return BatchVarianceReport(
                expected_net_irr=expected,
                imported_net_irr=imported,
                posted_net_irr=posted,
                difference_irr=imported - posted,
                tieout_holds=None,
                tieout_difference_irr=None,
                tieout_tolerance_irr=None,
                gate_allowed=True if batch.status == ManualImportBatchStatus.POSTED else None,
                gate_reason=batch.posting_detail,
                posting_status=batch.posting_status,
                generated_at=now,
            )
        assert batch.source_import_batch_id is not None
        events = self._settlement.list_settlement_events(
            batch.installation_id, import_batch_id=batch.source_import_batch_id
        )
        imported = sum(event.net_irr - event.deductions_irr for event in events)
        tieout = self._settlement_import.compute_tieout_figures(
            batch.installation_id, batch.source_import_batch_id
        )
        posted = 0
        if batch.status == ManualImportBatchStatus.POSTED and tieout is not None:
            # The bank receipt posts exactly the matched deposits.
            posted = tieout.matched_deposits_irr
        gate_allowed: bool | None
        if batch.posting_status == POSTING_POSTED:
            gate_allowed = True
        elif batch.posting_status == POSTING_HELD_GATE:
            gate_allowed = False
        else:
            gate_allowed = None
        return BatchVarianceReport(
            expected_net_irr=expected,
            imported_net_irr=imported,
            posted_net_irr=posted,
            difference_irr=imported - posted,
            tieout_holds=tieout.holds if tieout is not None else None,
            tieout_difference_irr=tieout.difference_irr if tieout is not None else None,
            tieout_tolerance_irr=tieout.tolerance_irr if tieout is not None else None,
            gate_allowed=gate_allowed,
            gate_reason=batch.posting_detail,
            posting_status=batch.posting_status,
            generated_at=now,
        )

    # -- helpers ----------------------------------------------------------------

    def _require_batch(self, batch_id: str, tenant_id: str) -> ManualImportBatch:
        batch = self._batches.get_batch(batch_id, tenant_id)
        if batch is None:
            raise ManualImportNotFoundError(
                f"Import batch {batch_id!r} does not exist.",
                details={"batch_id": batch_id},
            )
        return batch

    @staticmethod
    def _transition(
        batch: ManualImportBatch,
        target: ManualImportBatchStatus,
        now: datetime.datetime,
        note: str | None = None,
    ) -> ManualImportBatch:
        """Move a batch to ``target`` (machine-checked) + record the stage."""
        require_transition(batch.status, target)
        return dataclasses.replace(
            batch,
            status=target,
            stages=(
                *batch.stages,
                ManualImportStage(stage=target.value, at=now, note=note),
            ),
            updated_at=now,
        )

    def _apply_tieout_run(
        self,
        batch: ManualImportBatch,
        run: TieOutRunResult | None,
        *,
        settlement_event_id: str | None = None,
        confirmed_tranche_ids: tuple[str, ...] = (),
        created_payment_ids: tuple[str, ...] = (),
    ) -> ApproveBatchResult:
        """Apply a tie-out run's outcome to an approved batch."""
        now = self._clock()
        if run is None:
            held = self._batches.update_batch(
                dataclasses.replace(
                    batch,
                    posting_status=POSTING_HELD_GATE,
                    posting_detail="Tie-out could not run for this batch.",
                )
            )
            return ApproveBatchResult(
                batch=held,
                posted=False,
                settlement_event_id=settlement_event_id,
                confirmed_tranche_ids=confirmed_tranche_ids,
                created_payment_ids=created_payment_ids,
            )
        if run.posted:
            posted = self._transition(
                batch,
                ManualImportBatchStatus.POSTED,
                now,
                note="tie-out برقرار و سند بانکی ثبت شد.",
            )
            saved = self._batches.update_batch(
                dataclasses.replace(
                    posted,
                    posting_status=POSTING_POSTED,
                    posting_detail=run.gate_reason,
                    engine_ref_id=run.tryton_journal_id,
                    posted_at=now,
                )
            )
            return ApproveBatchResult(
                batch=saved,
                posted=True,
                tryton_journal_id=run.tryton_journal_id,
                settlement_event_id=settlement_event_id,
                tieout=run.tieout,
                gate_reason=run.gate_reason,
                confirmed_tranche_ids=confirmed_tranche_ids,
                created_payment_ids=created_payment_ids,
            )
        if run.status == IMPORT_EXCEPTION:
            status, detail = POSTING_TIEOUT_FAILED, (
                f"Tie-out failed (difference {run.tieout.difference_irr:,} IRR): "
                "no automatic posting happens; the batch stays approved."
            )
        else:
            status, detail = POSTING_HELD_GATE, run.gate_reason or "Policy gate denied auto-post."
        held = self._batches.update_batch(
            dataclasses.replace(batch, posting_status=status, posting_detail=detail)
        )
        return ApproveBatchResult(
            batch=held,
            posted=False,
            settlement_event_id=settlement_event_id,
            tieout=run.tieout,
            gate_reason=run.gate_reason,
            confirmed_tranche_ids=confirmed_tranche_ids,
            created_payment_ids=created_payment_ids,
        )

    def _rerun_statement(
        self, batch: ManualImportBatch, actor: str
    ) -> tuple[ImportBatchPreview, str]:
        """Reopen a cancelled statement import from its stored rows."""
        assert batch.source_import_id is not None
        bank_batch = self._bank.get_batch(batch.source_import_id, batch.tenant_id)
        if bank_batch is None:
            raise ManualImportInvalidStateError(
                f"Import batch {batch.id} has no underlying statement import to re-run.",
                details={"batch_id": batch.id},
            )
        if bank_batch.status != ImportBatchStatus.CANCELLED:
            raise ManualImportInvalidStateError(
                f"Import batch {batch.id} cannot be re-run: its statement import "
                f"is {bank_batch.status.value}, not cancelled.",
                details={"batch_id": batch.id, "status": bank_batch.status.value},
            )
        reopened = self._bank.update_batch(
            dataclasses.replace(
                bank_batch, status=ImportBatchStatus.AWAITING_REVIEW, cancelled_at=None
            )
        )
        preview = ImportBatchPreview.from_statement_totals(
            total_deposits_irr=reopened.total_deposits_irr,
            total_withdrawals_irr=reopened.total_withdrawals_irr,
            new_rows=reopened.new_rows,
            duplicate_rows=reopened.duplicate_rows,
            invalid_rows=reopened.invalid_rows,
        )
        item = self._review.file_item(
            tenant_id=batch.tenant_id,
            item_type=ReviewItemType.MANUAL_IMPORT_REVIEW,
            severity=ReviewSeverity.WARNING,
            reason=(
                f"ایمپورت صورت‌حساب {batch.source_key} ({batch.filename}) — "
                f"تلاش دوبارهٔ {batch.attempt + 1}: {reopened.new_rows} ردیف جدید، "
                f"{reopened.duplicate_rows} تکراری، {reopened.invalid_rows} نامعتبر؛ "
                f"واریز {reopened.total_deposits_irr:,} ریال، "
                f"برداشت {reopened.total_withdrawals_irr:,} ریال."
            ),
            suggested_action="پیش‌نمایش اثر حسابداری را بررسی و تأیید کنید، یا بچ را رد کنید.",
            company_id=batch.company_id,
            channel=batch.source_key,
            source_event_ref=f"bank-import:{reopened.id}",
            amount_irr=reopened.gross_irr,
            actor=actor,
        )
        self._bank.update_batch(dataclasses.replace(reopened, review_item_id=item.id))
        return preview, item.id

    def _rerun_settlement(
        self, batch: ManualImportBatch, actor: str
    ) -> tuple[ImportBatchPreview, str]:
        """Reopen a rejected settlement import from its stored rows."""
        assert batch.source_import_id is not None
        file_import = self._settlement.get_file_import(
            batch.installation_id, batch.source_import_id
        )
        if file_import is None:
            raise ManualImportInvalidStateError(
                f"Import batch {batch.id} has no underlying settlement import to re-run.",
                details={"batch_id": batch.id},
            )
        if file_import.status != IMPORT_REJECTED:
            raise ManualImportInvalidStateError(
                f"Import batch {batch.id} cannot be re-run: its settlement import "
                f"is {file_import.status}, not rejected.",
                details={"batch_id": batch.id, "status": file_import.status},
            )
        self._settlement.update_file_import(
            file_import.id, status=IMPORT_AWAITING_REVIEW
        )
        settlement_preview = self._settlement_import.preview_for_import(
            batch.installation_id, file_import.id
        )
        preview = _preview_from_settlement(settlement_preview)
        item = self._review.file_item(
            tenant_id=batch.tenant_id,
            item_type=ReviewItemType.MANUAL_IMPORT_REVIEW,
            severity=ReviewSeverity.WARNING,
            reason=(
                f"ایمپورت فایل تسویهٔ {batch.source_key} ({batch.filename}) — "
                f"تلاش دوبارهٔ {batch.attempt + 1}: "
                f"{settlement_preview.matched_rows} ردیف منطبق، "
                f"{settlement_preview.new_payment_rows} پرداخت جدید، "
                f"{settlement_preview.unknown_rows} مرجع ناشناخته، "
                f"{settlement_preview.invalid_rows} نامعتبر؛ "
                f"خالص {settlement_preview.net_irr:,} ریال، "
                f"واریز موردانتظار {settlement_preview.expected_deposit_irr:,} ریال."
            ),
            suggested_action="پیش‌نمایش اثر حسابداری را بررسی و تأیید کنید، یا بچ را رد کنید.",
            channel=batch.source_key,
            source_event_ref=f"settlement-import:{file_import.id}",
            amount_irr=settlement_preview.net_irr,
            actor=actor,
        )
        return preview, item.id


def _upload_stages(now: datetime.datetime) -> tuple[ManualImportStage, ...]:
    """The parse stages an upload completes in one call (recorded, not long-lived)."""
    return (
        ManualImportStage(stage=ManualImportBatchStatus.UPLOADED.value, at=now),
        ManualImportStage(stage=ManualImportBatchStatus.MAPPED.value, at=now),
        ManualImportStage(stage=ManualImportBatchStatus.VALIDATED.value, at=now),
        ManualImportStage(stage=ManualImportBatchStatus.PREVIEWED.value, at=now),
        ManualImportStage(stage=ManualImportBatchStatus.AWAITING_REVIEW.value, at=now),
    )


def _preview_from_settlement(preview: ImportPreview) -> ImportBatchPreview:
    """Map the US-523 ImportPreview onto the unified batch preview."""
    return ImportBatchPreview(
        gross_irr=preview.gross_irr,
        fee_irr=preview.fee_irr,
        tax_irr=preview.vat_irr,
        refunds_irr=preview.refunds_irr,
        net_irr=preview.net_irr,
        deductions_irr=preview.deductions_irr,
        receivable_before_irr=preview.receivable_before_irr,
        receivable_after_irr=preview.receivable_after_irr,
        expected_deposit_irr=preview.expected_deposit_irr,
        matched_rows=preview.matched_rows,
        new_rows=preview.new_payment_rows,
        duplicate_rows=preview.duplicate_rows,
        invalid_rows=preview.invalid_rows,
        dropped_rows=preview.dropped_rows,
    )


def _batch_status_from_import(import_status: str) -> ManualImportBatchStatus:
    """Map a settlement file-import status onto a batch status.

    A duplicate re-upload of an import uploaded before US-554 (no
    batch wrapper yet) lands here: the batch mirrors the import's
    state instead of pretending it is awaiting review.
    """
    if import_status == IMPORT_AWAITING_REVIEW:
        return ManualImportBatchStatus.AWAITING_REVIEW
    if import_status == IMPORT_APPROVED:
        return ManualImportBatchStatus.APPROVED
    if import_status == IMPORT_SETTLED:
        return ManualImportBatchStatus.POSTED
    if import_status == IMPORT_EXCEPTION:
        return ManualImportBatchStatus.APPROVED
    if import_status == IMPORT_REJECTED:
        return ManualImportBatchStatus.ARCHIVED
    return ManualImportBatchStatus.AWAITING_REVIEW
