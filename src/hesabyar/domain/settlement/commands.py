"""Command objects for settlement writes (US-521).

Commands are plain data: the application service validates them and
the repository persists them. Amounts are integer IRR; percents are
exact ``Decimal`` (``10`` means 10%).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from decimal import Decimal

from hesabyar.domain.accounting.refs import (
    AccountRef,
    CompanyRef,
    JournalRef,
    PartyRef,
    PaymentJournalRef,
)
from hesabyar.domain.settlement.models import PaymentSource

__all__ = [
    "ApplyRefund",
    "ApproveSettlementImport",
    "CancelGatewayPayment",
    "CreateBankDeposit",
    "CreateGatewayPayment",
    "CreateSettlementEvent",
    "CreateSettlementException",
    "CreateSettlementFileImport",
    "CreateSettlementFileRow",
    "CreateSettlementMatch",
    "PostBankReceipt",
    "PostFeeVariance",
    "PostSaleFee",
    "PostTrancheClearing",
    "ImportSettlementFile",
    "MatchDepositManually",
    "PostBatchBankReceipt",
    "RecordBankDeposit",
    "RecordTrancheWriteOff",
    "ReassignSalesPeriod",
    "RejectSettlementImport",
    "ResolveSettlementException",
    "SettlementPostingWiring",
    "RunSettlementTieOut",
    "TransitionTranche",
]


@dataclass(frozen=True)
class CreateGatewayPayment:
    """Record a gateway payment and derive its tranche schedule."""

    installation_id: str
    channel_key: str
    gateway_transaction_id: str
    gross_irr: int
    gateway_confirmed_at: datetime.datetime
    source: PaymentSource
    store_order_id: str | None = None
    store_payment_id: str | None = None
    tryton_invoice_id: int | None = None
    tryton_journal_id: int | None = None
    customer_party_id: int | None = None
    canonical_event_id: str | None = None
    fee_vat_rate_override: Decimal | None = None
    """Explicit fee-VAT rate for this payment (overrides the contract
    snapshot). None = use the contract's ``fee_vat_rate``."""


@dataclass(frozen=True)
class CancelGatewayPayment:
    """Cancel/refund a payment before (or after) settlement."""

    payment_id: str
    reason: str
    actor: str | None = None


@dataclass(frozen=True)
class ReassignSalesPeriod:
    """Correct a payment's sales period (gateway file disagreed).

    Open tranches re-derive their due dates from the new period;
    confirmed/received tranches are untouched; an informational
    ``period_reassigned`` exception is logged (phased design §3.2).
    """

    payment_id: str
    new_sales_period_jalali: str
    reason: str
    actor: str | None = None


@dataclass(frozen=True)
class TransitionTranche:
    """Move one tranche to a new state (validated against §3.3)."""

    tranche_id: str
    to_state: str
    actor: str | None = None
    confirmed_net_irr: int | None = None
    received_net_irr: int | None = None
    variance_irr: int | None = None
    variance_reason: str | None = None
    match_method: str | None = None
    match_confidence: Decimal | None = None


@dataclass(frozen=True)
class RecordTrancheWriteOff:
    """Write off one tranche — human approval only (phased design §3.3).

    ``approved_by`` and ``reason`` are mandatory: a write-off without
    a named approver and a reason is refused, and the service records
    an audit-log entry for every write-off.
    """

    tranche_id: str
    approved_by: str
    reason: str


@dataclass(frozen=True)
class CreateSettlementException:
    """Open a settlement exception (§3.7)."""

    installation_id: str
    exception_type: str
    severity: str
    tranche_id: str | None = None
    settlement_event_id: str | None = None
    bank_deposit_id: str | None = None
    gateway_payment_id: str | None = None
    expected_amount_irr: int | None = None
    actual_amount_irr: int | None = None
    escalation_level: int = 0
    note: str | None = None


@dataclass(frozen=True)
class ResolveSettlementException:
    """Resolve an open exception (resolver + time + note are recorded)."""

    exception_id: str
    resolution: str
    resolved_by: str
    note: str | None = None


@dataclass(frozen=True)
class CreateSettlementEvent:
    """Record a gateway settlement event (C1)."""

    installation_id: str
    channel_key: str
    payout_date: datetime.date
    net_irr: int
    source: str
    deductions_irr: int = 0
    tracking_ref: str | None = None
    destination_iban: str | None = None
    gateway_batch_ref: str | None = None
    import_batch_id: str | None = None
    canonical_event_id: str | None = None


@dataclass(frozen=True)
class CreateBankDeposit:
    """Record a bank deposit (C4)."""

    installation_id: str
    deposit_date: datetime.date
    amount_irr: int
    bank_account_ref: str | None = None
    description: str | None = None
    tracking_ref: str | None = None
    bank_import_row_id: str | None = None
    source: str = "statement_import"


@dataclass(frozen=True)
class CreateSettlementMatch:
    """Link a tranche to a deposit and/or a settlement event (C5)."""

    installation_id: str
    tranche_id: str
    allocated_amount_irr: int
    method: str
    bank_deposit_id: str | None = None
    settlement_event_id: str | None = None
    confidence: Decimal | None = None
    matched_by: str | None = None
    is_automatic: bool = True


@dataclass(frozen=True)
class CreateSettlementFileImport:
    """Register an imported settlement file (§3.2ب)."""

    installation_id: str
    channel_key: str
    file_type: str
    period_jalali: str
    file_hash_sha256: str
    import_batch_id: str
    uploaded_by: str | None = None
    row_count: int = 0


@dataclass(frozen=True)
class CreateSettlementFileRow:
    """Record one parsed row of an imported settlement file (§3.2ب)."""

    import_id: str
    row_no: int
    raw_json: dict[str, object] = field(default_factory=dict)
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

# -- settlement posting (US-522, Phase 5-3 Wave B) ---------------------------
#
# The §3.5 journals are posted through the AccountingEnginePort. Every
# posting command carries its engine wiring explicitly (company,
# gateway party, payment journal, accounts): the mapping-center
# resolver in the application layer builds it from the registry for
# REST callers, and in-process callers (US-523) pass it directly.
# Nothing about money is defaulted — a missing account fails closed.


@dataclass(frozen=True)
class SettlementPostingWiring:
    """Engine wiring for one channel's settlement postings (US-522).

    ``gateway_party`` is the Tryton party of the GATEWAY (the
    receivable is from the gateway, never the customer — ADR 0003).
    ``payment_journal`` must be clearing-configured (clearing account
    + journal) or the clearing transfer refuses to run. ``fee_vat_account``
    is the non-deductible VAT account while ``fee_vat_claimable`` is
    false (phased design §3.6). ``general_journal`` may be None, in
    which case the adapter resolves the company's single general
    journal.
    """

    company: CompanyRef
    gateway_party: PartyRef
    payment_journal: PaymentJournalRef
    receivable_account: AccountRef
    clearing_account: AccountRef
    bank_account: AccountRef
    fee_expense_account: AccountRef
    fee_vat_account: AccountRef
    revenue_account: AccountRef
    general_journal: JournalRef | None = None


@dataclass(frozen=True)
class PostSaleFee:
    """Recognise a payment's fee + fee VAT (policy A, at sale).

    One journal, posted: Dr fee expense + Dr fee VAT / Cr gateway
    receivable — the receivable leg is split per tranche (one line per
    tranche, party=gateway, maturity_date=tranche due) so each tranche
    reconciles independently when its clearing leg posts. Under
    policy B (``fee_accrual_policy == 'at_settlement'``) the service
    refuses: the fee is recognised per tranche at confirmation
    instead (see ``PostTrancheClearing``).
    """

    payment_id: str
    wiring: SettlementPostingWiring
    date: datetime.date
    actor: str | None = None

# -- US-523: settlement file import + matching + tie-out ---------------------


@dataclass(frozen=True)
class ImportSettlementFile:
    """Import a gateway settlement file (C1).

    ``content`` is the raw file bytes (Excel/CSV — the reader sniffs
    the format); ``currency_unit`` is the per-import currency setting
    (``rial``/``toman`` — the SnappPay Excel currency is UNVERIFIED
    until the first real customer file, so the operator declares it
    per import). ``columns`` is an optional inline column mapping
    (canonical field → header name or ``#<n>`` positional ref); when
    omitted, headers are auto-detected from the SnappPay synonym map.
    ``date_kind`` is ``jalali`` (default) or ``gregorian``.
    ``file_type`` is ``detail`` (per-transaction rows) / ``payouts``
    (aggregate payout rows) / ``statement`` (monthly statement).
    """

    installation_id: str
    channel_key: str
    file_type: str
    period_jalali: str
    filename: str
    content: bytes
    currency_unit: str = "rial"
    columns: dict[str, str] | None = None
    date_kind: str = "jalali"
    header_row: int = 0
    uploaded_by: str | None = None
    actor: str | None = None


@dataclass(frozen=True)
class PostTrancheClearing:
    """Post a confirmed tranche's clearing transfer (C3, Path 1b).

    Creates the tranche's ``account.payment`` (net amount, linked to
    the tranche's receivable line) ONLY now — never at schedule time
    (a Draft payment already reduces ``amount_to_pay``) — drives it
    ``submit → process_rest → succeed`` with ``payout_date`` as the
    clearing date, posts the clearing move, and reconciles the
    tranche's receivable lines (invoice + fee + clearing legs).
    Under policy B the tranche's fee share is recognised first.
    The tranche must be ``confirmed``/``partially_confirmed`` (manual
    path) or ``received``/``partially_received`` (batch path: the
    tie-out clears late, before the bank receipt).
    """

    tranche_id: str
    wiring: SettlementPostingWiring
    payout_date: datetime.date
    actor: str | None = None


@dataclass(frozen=True)
class ApproveSettlementImport:
    """Approve an awaiting-review import (authoritative matching + events)."""

    import_id: str
    actor: str


@dataclass(frozen=True)
class RejectSettlementImport:
    """Reject (cancel) an awaiting-review import — nothing is applied."""

    import_id: str
    actor: str
    reason: str | None = None


@dataclass(frozen=True)
class RecordBankDeposit:
    """Record a bank deposit and run C5 matching (C4/C5).

    ``company_engine_id`` (when given) lets the service attempt the
    full tie-out + auto-post inline once the deposit completes a
    batch; without it, matching runs but posting waits for an
    explicit tie-out run. ``tenant_id`` is needed for the review
    items the matching may file.
    """

    installation_id: str
    tenant_id: str
    deposit_date: datetime.date
    amount_irr: int
    bank_account_ref: str | None = None
    description: str | None = None
    tracking_ref: str | None = None
    bank_import_row_id: str | None = None
    source: str = "statement_import"
    channel_key: str | None = None
    company_engine_id: int | None = None
    actor: str | None = None


@dataclass(frozen=True)
class MatchDepositManually:
    """Apply a human-confirmed deposit↔tranche match (C5 rule 3 follow-up).

    ``allocated_amount_irr`` must not exceed the tranche's outstanding
    net nor the deposit's unallocated remainder. The match is
    recorded with ``is_automatic=False`` and the actor's name.
    """

    deposit_id: str
    tranche_id: str
    allocated_amount_irr: int
    actor: str
    tenant_id: str
    company_engine_id: int | None = None


@dataclass(frozen=True)
class RunSettlementTieOut:
    """Run the mandatory tie-out (C6) for one import batch.

    ``company_engine_id`` is required for the policy gate's
    fiscal-period layer: without a company the gate fails closed and
    the batch can only reach ``received``, never auto-post.
    """

    import_id: str
    actor: str
    tenant_id: str
    company_engine_id: int | None = None


@dataclass(frozen=True)
class PostBankReceipt:
    """Post a received tranche's bank receipt (C6, after tie-out).

    Dr bank / Cr clearing for the tranche's received net, then the
    two clearing legs are reconciled. Refuses unless the tranche is
    ``received`` (or ``partially_received``), its matched deposits
    cover the amount, and the settlement event's batch tie-out passes
    (Σ confirmed tranche nets − Σ documented deductions = Σ matched
    bank deposits, within the contract's ``tieout_tolerance_irr``).
    A fully received tranche moves to ``settled``.
    """

    tranche_id: str
    wiring: SettlementPostingWiring
    settlement_event_id: str
    deposit_date: datetime.date
    actor: str | None = None


@dataclass(frozen=True)
class PostBatchBankReceipt:
    """Post the bank leg of a settled batch (Dr bank / Cr clearing).

    The command carries everything the posting implementation needs
    and nothing engine-specific: the implementation (US-522) resolves
    the company, the bank account and the clearing account from the
    channel's mappings, posts the move dated at ``deposit_date``,
    and returns the posted journal id. ``amount_irr`` is the total
    matched-deposit amount of the batch (== the tie-out total).

    NOTE (merge US-522/US-523): US-523 defined this as a second
    ``PostBankReceipt``; it is a batch-level command, distinct from
    US-522's tranche-level ``PostBankReceipt``, so it carries its
    own name.
    """

    installation_id: str
    tenant_id: str
    channel_key: str
    settlement_event_id: str
    bank_deposit_ids: tuple[str, ...]
    tranche_ids: tuple[str, ...]
    amount_irr: int
    payout_date: datetime.date
    deposit_date: datetime.date
    import_batch_id: str
    company_engine_id: int | None = None


@dataclass(frozen=True)
class PostFeeVariance:
    """Post a fee-variance adjustment move (US-522, §3.5).

    The gateway file showed a real fee (and VAT) different from the
    expected figures: the difference is posted as an adjustment move
    (Dr/Cr fee expense + corresponding VAT against the gateway
    receivable) — NEVER silently absorbed. A ``fee_mismatch``
    exception is always opened with the reason, and the fee record
    is updated with the actuals. ``tranche_id`` scopes the variance
    to one tranche; None means payment-level.
    """

    payment_id: str
    wiring: SettlementPostingWiring
    actual_fee_irr: int
    actual_vat_irr: int
    reason: str
    date: datetime.date
    tranche_id: str | None = None
    event_ref: str | None = None
    actor: str | None = None


@dataclass(frozen=True)
class ApplyRefund:
    """Apply a (partial) post-settlement refund (US-522, §3.5).

    The refunded amount is deducted FIRST from the payment's OPEN
    UNREACHED tranches (last tranche backwards); any excess becomes a
    negative adjustment (``refunds_deducted_irr``) on the next tranche
    of the same gateway. Confirmed/received tranches are untouched.
    The sales-return journal (Dr revenue / Cr gateway receivable) is
    posted for the refunded amount, and every adjustment is logged
    with the reason + the refund event reference.

    NOTE on the journal sides: the design text (§3.5, part B) lists
    this entry as "Cr revenue / Dr gateway receivable", which is the
    reverse of the standard sales-return reversal (the sale was Dr
    receivable / Cr revenue; a return reverses it). The service posts
    the accounting-correct reversal and the discrepancy is recorded
    in docs/settlement.md.
    """

    payment_id: str
    wiring: SettlementPostingWiring
    refund_amount_irr: int
    reason: str
    date: datetime.date
    event_ref: str | None = None
    actor: str | None = None
