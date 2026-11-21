"""Bank-statement import + reconciliation domain models (US-514, 5ت B1/B2).

Phased design §4 (bank layer) + §1.3 5ت + ADR 0003 decision 5: Bank
Melli has no public statement API, so manual file import is the FIRST
bank capability — not a side path. An import batch is a first-class
entity: every import has an ``import_batch_id``, an accounting-effect
preview, and a ``manual_import_review`` queue item; nothing posts
without a human approving the preview.

Money is whole Rial everywhere (spec §19 rule 7): a file in Toman is
converted ×10 at the import boundary, explicitly, with the file's unit
recorded on the batch — never guessed.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from enum import StrEnum, unique

from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.integrations.models import SuggestionMode

#: Maximum rows per imported file (phased design §4 §2.1: the 2,000-row
#: bank-export cap, verified for some banks; applied to every import so
#: long ranges are split into smaller date ranges at the source).
MAX_IMPORT_ROWS = 2000

#: Confirmations after which a learned rule may be promoted Suggest →
#: Auto by the operator (the mode change itself is always a human
#: decision; this is only the eligibility floor the UI shows).
RULE_AUTO_MIN_CONFIRMATIONS = 3


@unique
class StatementFileFormat(StrEnum):
    """Statement file formats (phased design §4 §2.1).

    Excel and CSV are first priority; PDF is second priority (table
    extraction is best-effort — rows that cannot be parsed land on the
    preview as invalid, never silently dropped).
    """

    XLSX = "xlsx"
    CSV = "csv"
    PDF = "pdf"


@unique
class FileCurrencyUnit(StrEnum):
    """The currency unit a statement file's amounts are written in.

    Explicit per import — never guessed (the Baam export unit is
    UNVERIFIED until a real customer file is seen). Toman converts ×10
    to Rial at the import boundary.
    """

    RIAL = "rial"
    TOMAN = "toman"


@unique
class ImportBatchStatus(StrEnum):
    """Lifecycle of an import batch.

    A batch is born ``awaiting_review`` (parsed + preview computed +
    queue item filed). Confirming writes the statement to the engine;
    cancelling discards the batch. Both are terminal.
    """

    AWAITING_REVIEW = "awaiting_review"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"


@unique
class ColumnMappingStatus(StrEnum):
    """Lifecycle of one saved column mapping version."""

    ACTIVE = "active"
    SUPERSEDED = "superseded"


@unique
class MatchTargetType(StrEnum):
    """What a statement line matched against."""

    INVOICE = "invoice"
    PAYMENT = "payment"
    RULE = "rule"
    MANUAL = "manual"


@unique
class MatchStatus(StrEnum):
    """Lifecycle of one recorded match.

    ``unmatched`` records that a reconcile run looked at the line and
    found nothing — it exists so re-reconciling does not re-file a
    duplicate ``bank_unmatched`` review item for the same line. It is
    not a live match: a manual match on the line is still allowed.
    ``rejected`` covers both a human rejection and a superseded
    suggestion (a later run found a better decision).
    """

    AUTO_APPLIED = "auto_applied"
    SUGGESTED = "suggested"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    UNMATCHED = "unmatched"


@unique
class ReconcileOutcome(StrEnum):
    """Per-line outcome of a reconcile run."""

    ALREADY_MATCHED = "already_matched"
    MATCHED = "matched"
    SUGGESTED = "suggested"
    AMBIGUOUS = "ambiguous"
    UNMATCHED = "unmatched"
    SKIPPED_ZERO = "skipped_zero"


#: Canonical statement columns a bank file maps onto. ``date`` and
#: ``description`` are always required; the amount comes either from a
#: single signed ``amount`` column or from a ``debit``/``credit`` pair.
REQUIRED_MAPPING_FIELDS = ("date", "description")
AMOUNT_MAPPING_FIELDS = ("amount", "debit", "credit")
OPTIONAL_MAPPING_FIELDS = ("balance", "bank_ref", "counterparty")
ALL_MAPPING_FIELDS = REQUIRED_MAPPING_FIELDS + AMOUNT_MAPPING_FIELDS + OPTIONAL_MAPPING_FIELDS


@dataclass(frozen=True)
class BankColumnMapping:
    """A saved per-bank column mapping (phased design §4 §2.1).

    ``columns`` maps a canonical field name to its source column: a
    header name (matched after normalisation) or a positional
    reference ``#<0-based index>`` for headerless files. ``company_id``
    is None when the mapping is shared across the tenant's companies
    (a bank's layout does not vary by company); a company-scoped
    mapping wins over a shared one for that company.
    """

    id: str
    tenant_id: str
    bank_key: str
    name: str
    file_format: StatementFileFormat
    columns: dict[str, str]
    date_kind: str = "jalali"
    currency_unit: FileCurrencyUnit = FileCurrencyUnit.RIAL
    header_row: int = 0
    company_id: str | None = None
    status: ColumnMappingStatus = ColumnMappingStatus.ACTIVE
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


@dataclass(frozen=True)
class BankImportRow:
    """One parsed row of an import batch.

    Invalid rows keep ``row_date``/``amount_irr`` None and carry
    ``parse_errors``; they appear on the preview so the operator sees
    exactly what was rejected, and they are never written to the
    engine. Duplicate rows keep their parsed values and are excluded
    from the accounting preview.
    """

    id: str
    batch_id: str
    row_number: int
    row_date: FiscalDate | None
    amount_irr: int | None
    description: str
    bank_ref: str | None = None
    balance_irr: int | None = None
    match_key: str | None = None
    is_duplicate: bool = False
    duplicate_reason: str | None = None
    parse_errors: tuple[str, ...] = ()
    was_rounded: bool = False
    suggested_account_id: int | None = None
    suggested_party_id: int | None = None
    suggested_invoice_id: int | None = None
    suggestion_source: str | None = None

    @property
    def is_valid(self) -> bool:
        """True when the row parsed cleanly (date + amount present)."""
        return not self.parse_errors and self.row_date is not None and self.amount_irr is not None

    @property
    def is_new(self) -> bool:
        """True when the row is valid and not a duplicate — it will be imported."""
        return self.is_valid and not self.is_duplicate


@dataclass(frozen=True)
class BankImportBatch:
    """One statement-file import (phased design §4 §2.2).

    ``id`` IS the ``import_batch_id``. Totals cover NEW valid rows
    only — duplicates and invalid rows never enter the accounting
    preview. ``start_balance_irr``/``end_balance_irr`` come from the
    file's balance column when the mapping has one; otherwise they
    stay None and the engine statement is created without balances.
    """

    id: str
    tenant_id: str
    company_id: str
    bank_key: str
    statement_journal_id: int
    filename: str
    file_format: StatementFileFormat
    currency_unit: FileCurrencyUnit
    status: ImportBatchStatus
    total_rows: int
    new_rows: int
    duplicate_rows: int
    invalid_rows: int
    total_deposits_irr: int
    total_withdrawals_irr: int
    default_account_id: int
    column_mapping_id: str | None = None
    start_balance_irr: int | None = None
    end_balance_irr: int | None = None
    engine_statement_id: int | None = None
    review_item_id: str | None = None
    created_at: datetime.datetime | None = None
    confirmed_at: datetime.datetime | None = None
    cancelled_at: datetime.datetime | None = None

    @property
    def net_irr(self) -> int:
        """Deposits minus withdrawals over new rows (signed)."""
        return self.total_deposits_irr - self.total_withdrawals_irr

    @property
    def gross_irr(self) -> int:
        """Total money moved over new rows (the amount at stake in review)."""
        return self.total_deposits_irr + self.total_withdrawals_irr


@dataclass(frozen=True)
class BankMatchRule:
    """One learned bank-coding rule (B2 owns learned mappings).

    A confirmed manual mapping becomes a rule: future lines whose
    description contains ``pattern`` (normalised) and whose direction
    matches are coded to ``target_account_id``. Rules start in
    SUGGEST (one-click confirm) and move to AUTO only when the
    operator promotes them — the Xero bank-rules pattern (§1.4).
    ``bank_key`` empty means the rule applies to every bank.
    """

    id: str
    tenant_id: str
    company_id: str
    bank_key: str
    pattern: str
    direction: str
    target_account_id: int
    target_party_id: int | None = None
    target_kind: str = "account"
    mode: SuggestionMode = SuggestionMode.SUGGEST
    confirmations: int = 1
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


@unique
class BankRuleSuggestionStatus(StrEnum):
    """Lifecycle of one bank-rule suggestion (US-565, W3).

    A suggestion is born ``pending`` when a reconcile run matches a
    line against a SUGGEST-mode bank rule; the operator confirms it
    (the coding is applied) or rejects it (the line never re-suggests
    from that rule). Only confirmed suggestions count toward the
    rule's promotion threshold.
    """

    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


@dataclass(frozen=True)
class BankRule:
    """One user-defined bank auto-coding rule (US-565, W3).

    The Xero-style rule: ``when <statement-line pattern> → suggest
    <account + mapping>``. Unlike a B2 learned rule (one description
    substring), the pattern is a small AND of conditions — a
    description substring, a counterparty name, an amount range
    (whole Rial, on the line's magnitude), and a money direction —
    and at least one condition must be set (a rule with no condition
    would match every line and is rejected at creation).

    Rules are born in SUGGEST mode with zero confirmations; a rule
    becomes AUTO only through a human promotion, and only once
    ``RULE_AUTO_MIN_CONFIRMATIONS`` suggestions from it have been
    confirmed (the same threshold as the US-552 mapping-learning
    promotion). Editing the pattern or the target resets the
    confirmations and drops an AUTO rule back to SUGGEST — the trust
    was earned for the old definition, not the new one.

    ``bank_key`` empty means the rule applies to every bank (the
    matcher does not filter on it — reconcile cannot reliably know a
    statement's bank key, the same as the B2 learned rules; the key
    is recorded for scoping and display). ``priority`` orders rules
    inside one company's set (lower runs first; first match wins —
    the Xero pattern).
    """

    id: str
    tenant_id: str
    company_id: str
    bank_key: str
    name: str
    target_account_id: int
    description_contains: str | None = None
    counterparty_contains: str | None = None
    amount_min_irr: int | None = None
    amount_max_irr: int | None = None
    direction: str = "any"
    target_party_id: int | None = None
    target_kind: str = "account"
    mode: SuggestionMode = SuggestionMode.SUGGEST
    confirmations: int = 0
    priority: int = 100
    enabled: bool = True
    created_by: str | None = None
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None

    @property
    def auto_eligible(self) -> bool:
        """True when a human may promote this rule to AUTO (threshold met)."""
        return self.mode == SuggestionMode.SUGGEST and self.confirmations >= RULE_AUTO_MIN_CONFIRMATIONS

    @property
    def has_pattern(self) -> bool:
        """True when at least one pattern condition is set."""
        return (
            self.description_contains is not None
            or self.counterparty_contains is not None
            or self.amount_min_irr is not None
            or self.amount_max_irr is not None
        )


@dataclass(frozen=True)
class BankRuleSuggestion:
    """One suggestion a bank rule made for one statement line (US-565, W3).

    Recorded when a reconcile run matches a line against a SUGGEST-mode
    bank rule (the reconcile's SUGGESTED match record is the same
    decision from the statement's side; this row is the rule's side —
    the one the operator confirms or rejects on the bank-rules page).
    ``rule_id`` is NULL when the rule was deleted (the audit row
    survives — ``ON DELETE SET NULL``); ``rule_name`` and the target
    snapshot keep the row readable afterwards.
    """

    id: str
    tenant_id: str
    rule_id: str | None
    rule_name: str
    company_id: str
    statement_id: int
    statement_line_id: int
    line_description: str
    line_amount_irr: int
    target_account_id: int
    target_party_id: int | None = None
    status: BankRuleSuggestionStatus = BankRuleSuggestionStatus.PENDING
    confirmed_by: str | None = None
    confirmed_at: datetime.datetime | None = None
    created_at: datetime.datetime | None = None


@dataclass(frozen=True)
class BankStatementMatch:
    """One recorded reconciliation match for a statement line.

    The durable record of a reconcile decision: which line matched
    what, at which priority, with which confidence, and where the
    money's destination was (the statement journal's bank account +
    Shaba when the journal carries one). ``target_id`` is the engine
    id for invoice/payment targets (None for a rule target, whose
    stable key is the rule UUID in ``target_ref``). A suggested match
    that a human confirms becomes CONFIRMED; a rejected suggestion
    becomes REJECTED and never re-suggests for that line. ``outcome``
    is the reconcile outcome that produced the record (None for a
    manual match, which has no reconcile outcome); the §11.2 report
    needs it to tell ambiguous lines from merely suggested ones.
    """

    id: str
    tenant_id: str
    company_id: str
    statement_id: int
    statement_line_id: int
    line_date: FiscalDate
    line_amount_irr: int
    line_description: str
    target_type: MatchTargetType
    target_id: int | None
    target_ref: str
    target_label: str
    priority: int
    confidence: int
    status: MatchStatus
    outcome: ReconcileOutcome | None = None
    destination_account_id: int | None = None
    destination_shaba: str | None = None
    review_item_id: str | None = None
    matched_at: datetime.datetime | None = None
    matched_by: str | None = None


@dataclass(frozen=True)
class MatchSuggestion:
    """One P3 smart suggestion for a line (human approves)."""

    target_type: MatchTargetType
    target_id: int | str
    target_label: str
    confidence: int
    reason: str


@dataclass(frozen=True)
class ReconcileLineResult:
    """Per-line outcome of a reconcile run (returned, not stored)."""

    line_id: int
    line_date: FiscalDate
    amount_irr: int
    description: str
    outcome: ReconcileOutcome
    priority: int | None = None
    confidence: int | None = None
    target_type: MatchTargetType | None = None
    target_id: int | str | None = None
    target_label: str | None = None
    suggestions: tuple[MatchSuggestion, ...] = ()
    review_item_id: str | None = None


@dataclass(frozen=True)
class ReconcileResult:
    """Summary of one reconcile run over a statement."""

    statement_id: int
    company_id: str
    total_lines: int
    already_matched_count: int
    matched_count: int
    priority1_count: int
    priority2_count: int
    suggested_count: int
    ambiguous_count: int
    unmatched_count: int
    matched_total_irr: int
    unmatched_total_irr: int
    review_items_filed: int
    lines: tuple[ReconcileLineResult, ...] = ()


@dataclass(frozen=True)
class BankReconciliationRow:
    """One bank account's reconciliation status in a period (§11.2)."""

    bank_key: str | None
    journal_id: int
    journal_name: str
    destination_shaba: str | None
    statements_count: int
    lines_total: int
    matched_lines: int
    suggested_lines: int
    ambiguous_lines: int
    unmatched_lines: int
    match_rate: float | None
    unmatched_balance_irr: int
    oldest_unmatched_age_days: int | None
    oldest_unmatched_line_id: int | None


@dataclass(frozen=True)
class BankReconciliationStatus:
    """Bank reconciliation status report (spec §11.2).

    Freshness metadata follows §11.1: the report names its sources,
    the source timestamp (newest match/import feeding it), the
    generation timestamp, the company and period, and the report /
    calculation versions, so a reader can tell how fresh it is and
    reproduce it.
    """

    company_id: str
    date_from: FiscalDate
    date_to: FiscalDate
    generated_at: datetime.datetime
    source: str
    source_timestamp: datetime.datetime | None
    report_version: str
    calculation_version: str
    rows: tuple[BankReconciliationRow, ...]
    statements_count: int
    lines_total: int
    matched_lines: int
    suggested_lines: int
    ambiguous_lines: int
    unmatched_lines: int
    match_rate: float | None
    unmatched_balance_irr: int

    @property
    def freshness(self) -> str:
        """Freshness indicator (§11.1): live when a source exists, else empty."""
        return "live" if self.source_timestamp is not None else "empty"
