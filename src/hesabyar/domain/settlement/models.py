"""Domain models for the settlement engine (US-521, Phase 5-3 Wave A).

Phased design §3 (part B): every gateway payment gets an exact expected
settlement schedule — one ``settlement_tranche`` row per leg of its
contract — derived at payment time from the ACTIVE contract version.
The receivable is from the GATEWAY, never from the customer (BNPL
payments move the customer's debt to the gateway at confirmation).

Money: all amounts are whole Iranian rials (``int`` — ``MoneyIRR``
digits=0); fee percentages and VAT rates are exact ``Decimal`` (``10``
means 10%, never 0.10, never a binary float — the same convention as
the tax-policy registry and the gateway contracts).

The contract itself lives in
:mod:`hesabyar.domain.integrations.models` (``GatewayContract``,
migration 0010, US-503) and already carries every §3.1 field — fee
timing, accrual policy, settlement model + ``tranche_legs``, payout
window/calendar, working-day adjustment, day lag, grace days,
tolerances, auto-post policy, source note. This package does not
redefine it; it DERIVES from it.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum, unique

__all__ = [
    "BankDeposit",
    "DerivedTranche",
    "GatewayPayment",
    "GatewayPaymentStatus",
    "PaymentSource",
    "SettlementEvent",
    "SettlementException",
    "SettlementExceptionResolution",
    "SettlementExceptionSeverity",
    "SettlementExceptionStatus",
    "SettlementExceptionType",
    "SettlementFeeRecord",
    "SettlementFileImport",
    "SettlementFileRow",
    "SettlementMatch",
    "SettlementPosting",
    "SettlementPostingKind",
    "SettlementPostingStatus",
    "SettlementTranche",
    "TrancheState",
    "sales_period_from",
]


@unique
class GatewayPaymentStatus(StrEnum):
    """Lifecycle of a gateway payment (phased design §3.2الف)."""

    SCHEDULED = "scheduled"
    """Tranches derived; nothing settled yet."""

    SETTLING = "settling"
    """At least one tranche confirmed/received; not all settled."""

    SETTLED = "settled"
    """Every tranche settled (or cancelled with nothing outstanding)."""

    PARTIALLY_CANCELLED = "partially_cancelled"
    """Cancelled after some tranches were already paid — the paid part
    is deducted by the gateway from later settlements (a negative
    adjustment; see ``refunds_deducted_irr``)."""

    CANCELLED = "cancelled"
    """Cancelled before any tranche was paid; all tranches cancelled."""


@unique
class PaymentSource(StrEnum):
    """Where a gateway payment was recorded from (phased design §3.2الف)."""

    STORE_WEBHOOK = "store_webhook"
    GATEWAY_POLL = "gateway_poll"
    MANUAL_IMPORT = "manual_import"
    MANUAL_ENTRY = "manual_entry"


@unique
class TrancheState(StrEnum):
    """Tranche lifecycle (phased design §3.3 state machine).

    The happy path is ``expected → due → confirmed → received →
    settled``; ``due`` may also jump straight to ``received`` (reverse
    match C7 — the bank deposit arrived before the gateway file).
    ``overdue`` is a timed state (past due + grace, unconfirmed);
    ``cancelled`` / ``exception`` / ``written_off`` are the exits.
    See :mod:`hesabyar.domain.settlement.state_machine` for the
    transition table.
    """

    EXPECTED = "expected"
    DUE = "due"
    CONFIRMED = "confirmed"
    RECEIVED = "received"
    SETTLED = "settled"
    PARTIALLY_CONFIRMED = "partially_confirmed"
    PARTIALLY_RECEIVED = "partially_received"
    OVERDUE = "overdue"
    CANCELLED = "cancelled"
    EXCEPTION = "exception"
    WRITTEN_OFF = "written_off"


@unique
class SettlementExceptionType(StrEnum):
    """The 11 settlement exception types (phased design §3.7)."""

    LATE_TRANCHE = "late_tranche"
    MISSING_DEPOSIT = "missing_deposit"
    PARTIAL_TRANCHE = "partial_tranche"
    AMOUNT_MISMATCH = "amount_mismatch"
    FEE_MISMATCH = "fee_mismatch"
    DEPOSIT_WITHOUT_FILE = "deposit_without_file"
    FILE_WITHOUT_DEPOSIT = "file_without_deposit"
    UNKNOWN_REFERENCE = "unknown_reference"
    OVERPAYMENT = "overpayment"
    PERIOD_REASSIGNED = "period_reassigned"
    CANCELLED_AFTER_SETTLEMENT = "cancelled_after_settlement"


@unique
class SettlementExceptionSeverity(StrEnum):
    """Exception severity (phased design §3.7)."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@unique
class SettlementExceptionStatus(StrEnum):
    """Exception lifecycle (phased design §3.7)."""

    OPEN = "open"
    IN_REVIEW = "in_review"
    RESOLVED = "resolved"
    WRITTEN_OFF = "written_off"


@unique
class SettlementExceptionResolution(StrEnum):
    """How an exception was resolved (phased design §3.7).

    No exception is ever resolved silently: every resolution carries
    the resolver, the time and a note.
    """

    CONFIRMED_OK = "confirmed_ok"
    ADJUSTED = "adjusted"
    WRITE_OFF = "write_off"
    ESCALATED_TO_GATEWAY = "escalated_to_gateway"
    RESCHEDULED = "rescheduled"


def sales_period_from(confirmed_at: datetime.datetime) -> str:
    """Return the Jalali sales period (``YYYY/MM``) for a confirmation time.

    The sales period comes from ``gateway_confirmed_at`` — the date
    the gateway confirmed the transaction — never from the store order
    date (phased design §3.2).
    """
    from hesabyar.domain.common.dates import FiscalDate

    fiscal = FiscalDate(confirmed_at.date())
    return f"{fiscal.jalali_year:04d}/{fiscal.jalali_month:02d}"


@dataclass(frozen=True)
class GatewayPayment:
    """One gateway payment: the money a gateway owes the merchant.

    ``gross_irr`` is the sale amount; ``fee_irr`` / ``fee_vat_irr`` /
    ``net_irr`` are derived at recording time from the contract
    version the payment was built from (``contract_version_id``) and
    snapshotted there — a later contract version never rewrites them.
    ``refunds_deducted_irr`` is the amount the gateway deducts from
    LATER settlements for tranches already paid before a cancel/refund
    (phased design §3.3/§3.5: a negative adjustment row, never a
    silent absorption). ``customer_party_id`` is the Tryton party id
    of the CUSTOMER (for the sale); the settlement receivable itself
    is from the gateway party, not the customer.
    """

    id: str
    installation_id: str
    payment_channel_id: str
    gateway_transaction_id: str
    gross_irr: int
    fee_irr: int
    fee_vat_irr: int
    net_irr: int
    contract_version_id: str
    fee_rate_snapshot: Decimal
    sales_period_jalali: str
    gateway_confirmed_at: datetime.datetime
    source: PaymentSource
    status: GatewayPaymentStatus = GatewayPaymentStatus.SCHEDULED
    store_order_id: str | None = None
    store_payment_id: str | None = None
    tryton_invoice_id: int | None = None
    tryton_journal_id: int | None = None
    customer_party_id: int | None = None
    fee_vat_rate_snapshot: Decimal | None = None
    canonical_event_id: str | None = None
    refunds_deducted_irr: int = 0
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


@dataclass(frozen=True)
class DerivedTranche:
    """One derived tranche before persistence (pure derivation output).

    ``tranche_index`` is 1-based (the design counts قسط ۱..۴).
    ``expected_date`` is the due date — and the ``maturity_date`` of
    the Tryton receivable line for this tranche (phased design §3.2).
    """

    tranche_index: int
    tranche_count: int
    sales_period_jalali: str
    expected_window_start: datetime.date
    expected_date: datetime.date
    gross_irr: int
    fee_irr: int
    fee_vat_irr: int
    net_irr: int


@dataclass(frozen=True)
class SettlementTranche:
    """One expected settlement instalment of one gateway payment.

    ``confirmed_net_irr`` / ``received_net_irr`` track partial
    progress toward ``net_irr`` (the partial states). ``variance_irr``
    is the signed difference (actual − expected) once a file/deposit
    disagrees with the schedule — never absorbed silently: a variance
    beyond tolerance moves the tranche to ``exception`` with a linked
    ``settlement_exception``. ``tryton_receivable_line_id`` is the
    Tryton move-line id of the receivable for this tranche; its
    ``maturity_date`` is ``expected_date``.
    """

    id: str
    installation_id: str
    gateway_payment_id: str
    tranche_index: int
    tranche_count: int
    sales_period_jalali: str
    expected_window_start: datetime.date
    expected_date: datetime.date
    gross_irr: int
    fee_irr: int
    fee_vat_irr: int
    net_irr: int
    state: TrancheState = TrancheState.EXPECTED
    confirmed_net_irr: int = 0
    received_net_irr: int = 0
    tryton_receivable_line_id: int | None = None
    variance_irr: int | None = None
    variance_reason: str | None = None
    match_method: str | None = None
    match_confidence: Decimal | None = None
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None

    @property
    def outstanding_net_irr(self) -> int:
        """Net amount not yet received for this tranche."""
        return self.net_irr - self.received_net_irr

    @property
    def is_open(self) -> bool:
        """True when the tranche can still be re-derived (period moves).

        Open = scheduled but not yet confirmed or received: the
        gateway has not acted on it, so a corrected sales period may
        move its due dates. Confirmed/received/partial tranches are
        untouched by re-derivation (phased design §3.2).
        """
        return self.state in (
            TrancheState.EXPECTED,
            TrancheState.DUE,
            TrancheState.OVERDUE,
        )


@dataclass(frozen=True)
class SettlementEvent:
    """One gateway settlement event: the gateway says it paid (C1).

    Created by a payouts-file import, a monthly statement import, or
    a manual portal entry. ``deductions_irr`` is the documented
    deductions of the period (prior-period debt / prior cancels from
    the statement); undocumented deductions are NOT deductions — they
    become ``amount_mismatch`` exceptions (phased design §3.4 C6).
    """

    id: str
    installation_id: str
    payment_channel_id: str
    payout_date: datetime.date
    net_irr: int
    source: str
    deductions_irr: int = 0
    tracking_ref: str | None = None
    destination_iban: str | None = None
    gateway_batch_ref: str | None = None
    import_batch_id: str | None = None
    canonical_event_id: str | None = None
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


@dataclass(frozen=True)
class BankDeposit:
    """One bank-deposit row: money that actually landed in the bank (C4).

    ``bank_import_row_id`` links to the US-514 bank-statement import
    row this deposit came from (None for a manually recorded deposit).
    ``tracking_ref`` is the gateway tracking number when the bank
    description happens to carry it (UNVERIFIED for Iranian banks —
    matching rule C5(1) is opportunistic, never the only leg).
    """

    id: str
    installation_id: str
    deposit_date: datetime.date
    amount_irr: int
    bank_account_ref: str | None = None
    description: str | None = None
    tracking_ref: str | None = None
    bank_import_row_id: str | None = None
    source: str = "statement_import"
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


@dataclass(frozen=True)
class SettlementMatch:
    """One many-to-many link between a tranche and a deposit/event (C5).

    One tranche can match several deposits (a partial tranche) and one
    deposit can match several tranches (an aggregated payout) — the
    allocated amount is per link, and the allocations of one tranche
    must never exceed its net. ``method`` names the C5 rule that
    matched (``exact_reference`` / ``amount_and_date`` / ``amount_only``
    / ``manual``); ``confidence`` is 0..1.
    """

    id: str
    installation_id: str
    tranche_id: str
    allocated_amount_irr: int
    method: str
    bank_deposit_id: str | None = None
    settlement_event_id: str | None = None
    confidence: Decimal | None = None
    matched_by: str | None = None
    is_automatic: bool = True
    matched_at: datetime.datetime | None = None
    created_at: datetime.datetime | None = None


@dataclass(frozen=True)
class SettlementException:
    """One settlement exception (phased design §3.7).

    ``escalation_level`` is the overdue escalation (0 = none, 1 = 7
    days, 2 = 30 days, 3 = 60 days past due) for ``late_tranche``.
    Every exception names its expected vs actual amounts so the
    variance is visible, never absorbed. Resolution is recorded with
    resolver + time + note.
    """

    id: str
    installation_id: str
    exception_type: SettlementExceptionType
    severity: SettlementExceptionSeverity
    status: SettlementExceptionStatus = SettlementExceptionStatus.OPEN
    tranche_id: str | None = None
    settlement_event_id: str | None = None
    bank_deposit_id: str | None = None
    gateway_payment_id: str | None = None
    expected_amount_irr: int | None = None
    actual_amount_irr: int | None = None
    escalation_level: int = 0
    resolution: SettlementExceptionResolution | None = None
    resolved_by: str | None = None
    resolved_at: datetime.datetime | None = None
    note: str | None = None
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


@dataclass(frozen=True)
class SettlementFeeRecord:
    """Fee tracking for one payment (and optionally one tranche) (§3.6).

    The expected figures come from the contract version applied at
    payment time; the actual figures come from the gateway file or
    portal statement (US-522/523). A mismatch between expected and
    actual is a variance — recorded here and raised as a
    ``fee_mismatch`` exception, with a draft adjustment move, never a
    silent absorption. ``source`` is ``calculated`` / ``file`` /
    ``portal_statement``.
    """

    id: str
    installation_id: str
    gateway_payment_id: str
    contract_version_id: str
    fee_percent_applied: Decimal
    expected_fee_irr: int
    expected_vat_irr: int
    source: str = "calculated"
    tranche_id: str | None = None
    fee_vat_rate_applied: Decimal | None = None
    actual_fee_irr: int | None = None
    fee_variance_irr: int | None = None
    actual_vat_irr: int | None = None
    vat_variance_irr: int | None = None
    tryton_fee_journal_id: int | None = None
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


@dataclass(frozen=True)
class SettlementFileImport:
    """One imported gateway settlement file (phased design §3.2ب).

    ``file_hash_sha256`` + channel is unique: re-importing the same
    file is a no-op (the import/matching story returns the existing
    import instead of duplicating rows). ``import_batch_id`` groups
    the whole batch so it can be reviewed or rolled back as one unit.
    ``file_type`` is ``detail`` / ``payouts`` / ``statement``.
    """

    id: str
    installation_id: str
    payment_channel_id: str
    file_type: str
    period_jalali: str
    file_hash_sha256: str
    import_batch_id: str
    status: str = "imported"
    uploaded_by: str | None = None
    uploaded_at: datetime.datetime | None = None
    row_count: int = 0
    matched_count: int = 0
    new_count: int = 0
    error_count: int = 0
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


@unique
class SettlementPostingKind(StrEnum):
    """Kind of one settlement posting (US-522, phased design §3.5)."""

    FEE_AT_SALE = "fee_at_sale"
    """Fee + fee VAT recognised at sale (policy A, the default)."""

    FEE_AT_SETTLEMENT = "fee_at_settlement"
    """Fee + fee VAT recognised per tranche as it confirms (policy B)."""

    CLEARING = "clearing"
    """Tranche confirmed: payment driven to succeed, clearing move posted."""

    BANK_RECEIPT = "bank_receipt"
    """Bank deposit matched + tie-out passed: Dr bank / Cr clearing."""

    FEE_VARIANCE = "fee_variance"
    """Fee-variance adjustment move (actual fee != expected fee)."""

    REFUND = "refund"
    """Post-settlement (partial) refund: sales return vs gateway receivable."""

    REFUND_ADJUSTMENT = "refund_adjustment"
    """Refund excess recorded as a negative adjustment (refunds_deducted_irr)."""


@unique
class SettlementPostingStatus(StrEnum):
    """Status of one settlement posting (US-522)."""

    POSTED = "posted"
    """The Tryton move is posted."""

    DRAFT = "draft"
    """The Tryton move exists as a draft (awaiting approval)."""


@dataclass(frozen=True)
class SettlementPosting:
    """One settlement posting: the audit ledger for §3.5 journals (US-522).

    Every journal the settlement engine posts to Tryton leaves one row
    here — the idempotency key (one posting per (payment/tranche,
    kind)), the Tryton move/payment/reconciliation ids, the amount,
    and the reason + event reference for adjustments. ``details`` is
    free-form JSON for the posting's breakdown (refund allocations per
    tranche, tie-out figures for a bank receipt, variance figures for
    a fee adjustment); it never carries secrets.
    """

    id: str
    installation_id: str
    gateway_payment_id: str
    kind: SettlementPostingKind
    amount_irr: int
    status: SettlementPostingStatus = SettlementPostingStatus.POSTED
    tranche_id: str | None = None
    tryton_move_id: int | None = None
    tryton_payment_id: int | None = None
    tryton_reconciliation_id: int | None = None
    reason: str | None = None
    event_ref: str | None = None
    details: dict[str, object] | None = None
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


@dataclass(frozen=True)
class SettlementFileRow:
    """One parsed row of an imported settlement file (§3.2ب).

    ``raw_json`` keeps the row exactly as parsed (amounts as strings
    to stay exact). ``match_status`` is ``matched`` / ``new_payment`` /
    ``unknown_reference`` / ``duplicate`` / ``amount_mismatch``.
    ``Net Sale`` rows and file totals are dropped at parse time and
    logged (they are not settlement rows).
    """

    id: str
    import_id: str
    row_no: int
    raw_json: dict[str, object]
    match_status: str = "unknown_reference"
    gateway_transaction_id: str | None = None
    tran_type: str | None = None
    amount_irr: int | None = None
    fee_irr: int | None = None
    net_irr: int | None = None
    transaction_date: datetime.date | None = None
    tracking_ref: str | None = None
    matched_tranche_id: str | None = None
    note: str | None = None
    created_at: datetime.datetime | None = None
