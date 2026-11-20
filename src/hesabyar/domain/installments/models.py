"""Installment plan models (US-540 + US-543, Phase 5-5 ۵ج).

The in-house installment schedule (phased design §1.3 ۵ج, flow I1)
plus the BNPL side (flow I2, US-543): one :class:`InstallmentPlan`
per sales invoice, with :class:`InstallmentLeg` rows carrying the
per-invoice schedule, :class:`InstallmentReceipt` rows recording
multi-stage receipts, and :class:`InstallmentSaleContract`
carrying the user-entered late-fee rule, penalty account and
early-settlement terms.

Money is Rial throughout, held as :class:`MoneyIRR` (digits=0);
the API boundary converts Toman inputs ×10 exactly once (see
``hesabyar.interfaces.rest.schemas.installments``). Dates are
Jalali in the API and stored as Gregorian
(:class:`FiscalDate`); timestamps are UTC ISO 8601.

Leg and plan status are NEVER stored — they are computed from the
receipts + the wall-clock date by
``hesabyar.domain.installments.state_machine`` (internal plans)
or from the linked settlement tranches by
``hesabyar.domain.installments.bnpl`` (BNPL plans) every time
they are read, so a status can never go stale or disagree with its
receipts. The only stored money totals are the legs' principal
and the receipts' amounts + allocations; everything else is
derived.

US-541 adds the finance side: the contract's finance-income rate
and dunning schedule, the late-fee assessment and the schedule
amendment (the audit record of a re-amortisation). The split /
recognition / fee / dunning MATH lives in
``hesabyar.domain.installments.finance`` (pure functions); this
module holds the state. Cheque receipt handling IS modelled here
(US-542): a leg paid by cheque enters ``pending_clearance`` until
the cheque clears (``LegStatus.PENDING_CLEARANCE``), the receipt
names its cheque (``InstallmentReceipt.cheque_id``), and the
collection hook (``collected_at``) records when the money actually
arrived for US-541's income recognition. BNPL plans
(``PlanKind.BNPL``, US-543) never accrue internal finance income
and never enter dunning — the gateway owns collection (see
``bnpl.plan_accrues_finance_income`` /
``bnpl.plan_enters_dunning``).
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.money import MoneyIRR


class LegStatus(StrEnum):
    """Computed status of one installment leg (§1.3 ۵ج, flow I1).

    Computed by :func:`leg_status` from (due date, principal,
    received, pending clearance, today) — never stored.
    ``overdue`` wins over ``partially_paid``: a leg past its due
    date with any unpaid balance is overdue, however much was
    already received. ``pending_clearance`` wins over ``overdue``:
    a leg fully covered by a cheque that has not cleared yet is
    NOT overdue (the pending-clearance period is not counted as
    overdue, §5 §2.1) — and no overdue dunning is sent for it
    (US-541's dunning engine must skip ``pending_clearance`` legs).
    """

    EXPECTED = "expected"
    DUE = "due"
    PARTIALLY_PAID = "partially_paid"
    PAID = "paid"
    OVERDUE = "overdue"
    #: Paid by cheque, awaiting clearing (US-542): received +
    #: pending-clearance covers the principal, but the money has
    #: not arrived yet. Clear → paid; bounce → unpaid (overdue
    #: when past the due date).
    PENDING_CLEARANCE = "pending_clearance"


class PlanStatus(StrEnum):
    """Plan-level status, derived from the legs (never stored)."""

    #: No leg is overdue and at least one leg is unpaid.
    ACTIVE = "active"
    #: At least one leg is overdue (internal plans only — a BNPL
    #: plan is never overdue: the gateway owns collection).
    OVERDUE = "overdue"
    #: Every leg is paid.
    PAID = "paid"


class PlanKind(StrEnum):
    """What kind of installment plan this is (US-543, phased design §1.3 ۵ج I2).

    The separation is structural: internal and BNPL legs are NEVER
    summed or netted together — separate kinds, separate receivable
    accounts (customer receivable vs the gateway's receivable
    account from the mapping center), separate report filters
    (``GET /installments/plans?kind=internal|bnpl``). A BNPL plan
    never accrues internal finance income and never enters dunning.
    """

    #: In-house installments: the customer pays the seller directly
    #: (cash / bank transfer / cheque). Finance income + dunning
    #: apply (US-541).
    INTERNAL = "internal"
    #: BNPL sale: the legs are paid by the gateway in tranches
    #: (the US-521 settlement engine). The customer's own
    #: installments to the gateway are NEVER modelled.
    BNPL = "bnpl"


class BnplReceiptStatus(StrEnum):
    """The gateway-side state of a BNPL receipt (US-543).

    Stored on the receipt — it is a fact about the gateway
    payment (recorded / rejected / cancelled), not a computed leg
    status. A ``rejected`` / ``cancelled`` receipt's allocations
    stop counting: its legs return to ``expected`` (the customer
    owes again) and a review-queue item is filed. The receipt row
    itself is kept as the audit record.
    """

    #: The gateway payment is live; its allocations count.
    ACTIVE = "active"
    #: The gateway rejected the payment (or the operator recorded a
    #: rejection): allocations no longer count.
    REJECTED = "rejected"
    #: The gateway payment was cancelled after recording.
    CANCELLED = "cancelled"


class PaymentMethod(StrEnum):
    """How an installment receipt was paid.

    ``cheque`` is enabled by US-542: a cheque receipt names its
    cheque (``cheque_id``) and its allocations stay pending until
    the cheque clears. ``bnpl`` is enabled by US-543 (BNPL plans
    only — see the plan-kind guard).
    """

    CASH = "cash"
    BANK_TRANSFER = "bank_transfer"
    OTHER = "other"
    CHEQUE = "cheque"
    BNPL = "bnpl"


#: Payment methods US-540 accepted on a receipt (kept for reference).
RECEIPT_METHODS_US540: frozenset[PaymentMethod] = frozenset(
    {PaymentMethod.CASH, PaymentMethod.BANK_TRANSFER, PaymentMethod.OTHER}
)

#: Payment methods accepted on a receipt: the US-540 three plus
#: ``cheque`` (US-542) and ``bnpl`` (US-543).
RECEIPT_METHODS: frozenset[PaymentMethod] = frozenset(
    {
        PaymentMethod.CASH,
        PaymentMethod.BANK_TRANSFER,
        PaymentMethod.OTHER,
        PaymentMethod.CHEQUE,
        PaymentMethod.BNPL,
    }
)


class LateFeeBasis(StrEnum):
    """How a contract's late fee is computed — user-entered per contract.

    The ``rate`` on the contract means: for ``daily_percent`` /
    ``monthly_percent``, a fraction (e.g. ``Decimal("0.02")`` = 2%)
    applied per day / per month overdue; for ``fixed_per_day``, a
    whole-Rial amount per overdue day. ``none`` means the contract
    charges no late fee. No rate is ever hard-coded anywhere.
    """

    NONE = "none"
    DAILY_PERCENT = "daily_percent"
    MONTHLY_PERCENT = "monthly_percent"
    FIXED_PER_DAY = "fixed_per_day"


class EarlySettlementFeePolicy(StrEnum):
    """What happens to accrued late fees on early settlement (per contract)."""

    WAIVED = "waived"
    KEPT = "kept"


class EarlySettlementIncomePolicy(StrEnum):
    """What happens to the unrealised finance income of the remaining
    legs on early settlement (per contract; US-541 implements the quote)."""

    WAIVED = "waived"
    DISCOUNTED = "discounted"
    FULL = "full"


class LateFeeJournalState(StrEnum):
    """Whether a late-fee assessment's journal exists in the engine (US-541).

    ``none`` — the fee was assessed but no journal was created (no
    engine wired, or the run had no accounts to post to); a later
    run journals the unjournaled portion. ``draft`` / ``posted`` —
    the draft journal exists (posted only when the plan opted into
    auto-post).
    """

    NONE = "none"
    DRAFT = "draft"
    POSTED = "posted"


class SettlementType(StrEnum):
    """Full or partial early settlement (US-541).

    ``full`` — the caller pays the whole payoff quote and every open
    leg closes atomically. ``partial`` — the caller pays part of the
    schedule: the payment allocates by the normal rule (oldest
    overdue first, then earliest due) and whatever advance it
    creates — plus any advance already held — reduces the remaining
    schedule, which is re-amortised across the still-open legs.
    """

    FULL = "full"
    PARTIAL = "partial"


class AmendmentKind(StrEnum):
    """What changed a plan's schedule (US-541; later kinds extend this).

    ``re_amortisation`` — a partial early payment reduced the
    remaining principal and the open legs were re-split (due dates
    unchanged). The amendment row carries the before/after leg
    principals, so the change is auditable.
    """

    RE_AMORTISATION = "re_amortisation"


class PlanSource(StrEnum):
    """Where a plan came from."""

    #: Created through the REST API by a user.
    MANUAL = "manual"
    #: Derived inside HesabYar from a ``sales.order.completed`` event
    #: (ADR 0005) — the derivation lands in a later 5ج story.
    DERIVED = "derived"


@dataclass(slots=True)
class InstallmentLeg:
    """One leg of an installment plan: a due date + a principal amount.

    The leg carries NO received amount and NO status — both are
    computed from the receipt allocations (see ``state_machine``).
    ``principal`` is the leg's principal (اصل); the principal/
    interest split arrives with US-541 and does not change this
    field (the split is computed from the contract terms, per leg,
    at read time).
    """

    id: str
    plan_id: str
    sequence: int
    due_date: FiscalDate
    principal: MoneyIRR
    created_at: datetime.datetime = field(default_factory=lambda: datetime.datetime.now(datetime.UTC))

    @property
    def principal_irr(self) -> int:
        return self.principal.amount

    @property
    def principal_toman(self) -> int:
        """Principal in Toman (display unit; storage is Rial)."""
        return self.principal.amount // 10


@dataclass(slots=True)
class InstallmentPlan:
    """One installment plan: exactly one sales invoice, one schedule.

    ``total_principal`` is the sum of the legs' principal, stored
    at creation for convenience and validated to equal the leg sum
    (a plan whose total disagrees with its legs is never created).
    ``interval_days`` / ``first_due_date`` record the multi-stage
    payment terms the legs were generated from; both are None when
    the legs were supplied explicitly. ``auto_post`` is the plan's
    posting preference for the journals later stories create
    (late fee, finance income — US-541): draft by default, auto-post
    only with an explicit opt-in (ADR 0003 decision 1). US-540
    itself creates no journal.

    ``kind`` separates the two installment worlds (US-543): an
    ``internal`` plan's legs are paid by the customer; a ``bnpl``
    plan's legs are paid by the gateway in tranches (the US-521
    settlement engine) and the plan pins exactly one gateway
    channel (``gateway_channel_key`` — required for BNPL plans,
    forbidden for internal ones). A BNPL plan never carries a sale
    contract (no late fee, no early settlement — the gateway owns
    collection), never accrues internal finance income, and never
    enters dunning.

    ``applied_advance`` (US-541) is the part of the receipts'
    advance already applied to the schedule by a partial early
    settlement (re-amortisation): the HELD advance is
    ``sum(receipts' advance) - applied_advance``. Receipts are
    never rewritten — the applied amount lives here, on the plan.
    """

    id: str
    tenant_id: str
    installation_id: str
    company_engine_id: int
    invoice_id: str
    total_principal: MoneyIRR
    leg_count: int
    contract_id: str | None = None
    customer_ref: str | None = None
    interval_days: int | None = None
    first_due_date: FiscalDate | None = None
    auto_post: bool = False
    #: Advance already applied to the schedule by partial early
    #: settlement (US-541); the held advance is the receipts'
    #: advances minus this.
    applied_advance: MoneyIRR = field(default_factory=MoneyIRR.zero)
    source: PlanSource = PlanSource.MANUAL
    #: The §10.2 source event the plan was derived from (the
    #: ``sales.order.completed`` source_event_id) — None for manual
    #: plans; set by the derivation story (ADR 0005).
    source_event_id: str | None = None
    kind: PlanKind = PlanKind.INTERNAL
    #: Registry channel key of the BNPL gateway (``snapppay`` /
    #: ``digipay`` / …) — required when ``kind`` is BNPL, None
    #: otherwise. The channel's mapping-center wiring resolves the
    #: gateway receivable account for this plan's postings.
    gateway_channel_key: str | None = None
    created_at: datetime.datetime = field(default_factory=lambda: datetime.datetime.now(datetime.UTC))
    updated_at: datetime.datetime = field(default_factory=lambda: datetime.datetime.now(datetime.UTC))

    @property
    def total_principal_irr(self) -> int:
        return self.total_principal.amount

    def touch(self) -> None:
        self.updated_at = datetime.datetime.now(datetime.UTC)


@dataclass(frozen=True, slots=True)
class LegAllocation:
    """One slice of a receipt applied to one leg (never over the leg's balance)."""

    leg_id: str
    amount: MoneyIRR

    @property
    def amount_irr(self) -> int:
        return self.amount.amount


@dataclass(slots=True)
class InstallmentReceipt:
    """One multi-stage receipt against a plan.

    A receipt is plan-level: the allocation rule
    (:func:`allocate_receipt`) decides which legs it pays, oldest
    overdue first, then earliest due; the remainder is the
    ``advance`` (an overpayment is never silently applied to a leg
    beyond its balance, and never prepays a not-yet-due leg).

    THE LINKAGE GUARD: ``invoice_id`` and ``journal_move_id`` are
    both mandatory — a receipt without both refs is rejected before
    anything is stored (``InstallmentLinkageError``, typed 422).
    ``invoice_id`` must equal the plan's invoice
    (``InstallmentInvoiceMismatchError`` otherwise). The journal
    move is the accounting document the caller already posted (or
    drafted) for this receipt; US-540 records the reference and
    does not verify the move exists in the engine (deferred —
    see ``docs/installments.md``). For a cheque receipt the move
    is the cheque's سند ۱ (the held journal) — the cheque must be
    ``held`` or ``in_clearing`` before a receipt can name it, so
    the سند ۱ move exists by then.

    CHEQUE RECEIPTS (US-542): ``cheque_id`` names the ONE cheque
    backing the receipt (a cheque backs at most one receipt, and a
    cheque receipt allocates to at most ONE leg — the cheque ↔ leg
    link on the cheque row is single-leg by design). The receipt's
    allocations count as ``received`` only once the cheque clears;
    until then they are pending clearance, and on bounce they count
    as nothing (the leg returns to unpaid — the status is computed,
    so no stored state changes on either outcome).

    THE COLLECTION HOOK (US-542, consumed by US-541): ``collected_at``
    is the date the money actually arrived — the date finance
    income is recognised on (income is recognised on COLLECTION,
    never on receipt). Cash / bank-transfer / other receipts are
    collected on receipt (``collected_at == received_date``); a
    cheque receipt is collected when its cheque clears (the leg
    outcome hook sets it), and a bounced cheque's receipt is never
    collected (``collected_at`` stays None). The cheque's state is
    the authority for the leg status; ``collected_at`` is the
    income-timing record, not a second status.

    BNPL receipts (US-543, ``payment_method == BNPL``) carry the
    gateway link: ``gateway_channel_key`` (the registry channel
    key, which must equal the plan's) + ``gateway_reference`` (the
    gateway's transaction id, the settlement engine's
    ``gateway_transaction_id``) + ``gateway_payment_id`` (the
    ``gateway_payments`` row the US-521 engine created for this
    receipt — the receivable FROM THE GATEWAY). ``bnpl_status``
    tracks the gateway-side state; only ``active`` BNPL receipts
    count toward leg totals (a rejected/cancelled receipt's legs
    return to ``expected``). All four fields are None for
    non-BNPL receipts.

    US-541 (early settlement): a settlement receipt's amount is the
    payoff — principal + finance income + late fees — so the
    non-principal portions are recorded here, on the receipt:
    ``finance_income`` is the income portion charged in the payoff
    (per the contract's income policy) and ``late_fee`` is the late
    fees kept in the payoff (per the fee policy). Both are 0 on an
    ordinary receipt. ``is_early_settlement`` marks the receipt as
    a settlement receipt (full or partial); a full settlement's
    allocations cover EVERY open leg, including not-yet-due ones —
    the one exception to "a receipt never prepays a not-yet-due
    leg".
    """

    id: str
    plan_id: str
    tenant_id: str
    installation_id: str
    invoice_id: str
    journal_move_id: int
    amount: MoneyIRR
    received_date: FiscalDate
    payment_method: PaymentMethod
    allocations: tuple[LegAllocation, ...] = ()
    advance: MoneyIRR = field(default_factory=MoneyIRR.zero)
    #: Finance-income portion of a settlement receipt (US-541); 0 on
    #: an ordinary receipt.
    finance_income: MoneyIRR = field(default_factory=MoneyIRR.zero)
    #: Late-fee portion of a settlement receipt (US-541); 0 on an
    #: ordinary receipt.
    late_fee: MoneyIRR = field(default_factory=MoneyIRR.zero)
    #: Held advance from EARLIER receipts applied by this receipt
    #: (US-541): a settlement receipt's allocations may exceed its
    #: amount by exactly this — the payoff nets the held advance,
    #: and this field is where the netting is recorded, so the
    #: identity ``amount + applied_advance == allocated + income +
    #: fee + advance`` holds for every receipt. 0 on an ordinary
    #: receipt.
    applied_advance: MoneyIRR = field(default_factory=MoneyIRR.zero)
    #: True when this receipt is an early-settlement receipt (US-541).
    is_early_settlement: bool = False
    reference: str | None = None
    notes: str | None = None
    #: The cheque backing this receipt (US-542) — set only when
    #: ``payment_method`` is ``cheque``; one receipt per cheque.
    cheque_id: str | None = None
    #: The date the money actually arrived (the collection hook,
    #: US-542): ``received_date`` for cash/bank/other receipts at
    #: creation; the clear date for a cheque receipt (set by the
    #: leg outcome hook on clear); None while uncollected.
    collected_at: FiscalDate | None = None
    gateway_channel_key: str | None = None
    gateway_reference: str | None = None
    gateway_payment_id: str | None = None
    bnpl_status: BnplReceiptStatus | None = None
    created_at: datetime.datetime = field(default_factory=lambda: datetime.datetime.now(datetime.UTC))

    @property
    def counts_toward_legs(self) -> bool:
        """True when this receipt's allocations count toward leg totals.

        Every non-BNPL receipt counts; a BNPL receipt counts only
        while its gateway payment is live (``bnpl_status ==
        active``). A rejected/cancelled BNPL receipt is kept as the
        audit record, but its legs return to ``expected``.
        """
        if self.payment_method is not PaymentMethod.BNPL:
            return True
        return self.bnpl_status is BnplReceiptStatus.ACTIVE

    @property
    def amount_irr(self) -> int:
        return self.amount.amount

    @property
    def advance_irr(self) -> int:
        return self.advance.amount

    @property
    def allocated_irr(self) -> int:
        """Amount actually applied to legs (the sum of the slices).

        The sum — not ``amount − advance``: a settlement receipt's
        amount also carries its finance-income and late-fee
        portions, which are not allocations.
        """
        return sum(a.amount.amount for a in self.allocations)


@dataclass(slots=True)
class InstallmentSaleContract:
    """The installment sale contract (قرارداد فروش قسطی) — NEW entity,
    separate from ``gateway_contract`` (which is gateways-only).

    Everything money-shaped here is user-entered per contract: the
    late-fee rule (basis + rate + grace days), the penalty account
    (finance income — never a vague "miscellaneous" account), and
    the early-settlement terms. NOTHING is hard-coded: a contract
    with ``late_fee_basis == NONE`` charges no late fee, and a
    contract without a penalty account cannot post a late-fee
    journal until one is set (US-541).

    US-541 adds the finance terms: ``finance_income_rate`` (the
    fraction of principal charged as finance income over the whole
    plan — a leg's income is its principal × this rate, recognised
    gradually as the leg is COLLECTED, never on receipt),
    ``dunning_offsets`` (the stepped reminder schedule: day offsets
    past the due date), the ``receivable_account`` (the debit side
    of the late-fee journal — the customer's receivable, which the
    fee increases), and ``early_settlement_discount_rate`` (the
    fraction discounted from the remaining unrealised income when
    the income policy is ``discounted``).

    Versioned: ``version`` increments per contract ``code`` and
    ``effective_from`` says when the version applies;
    ``supersedes_contract_id`` links a version to the one it
    replaces. The effective-version selection (which version
    governs on a given date) is US-541's
    (``hesabyar.domain.installments.finance.select_effective_contract``):
    it resolves a contract CODE at plan-creation time — a plan's
    terms are fixed at creation by its linked version, and a newer
    version never re-prices an existing plan.
    """

    id: str
    tenant_id: str
    installation_id: str
    company_engine_id: int
    code: str
    name: str
    version: int
    effective_from: FiscalDate
    late_fee_basis: LateFeeBasis = LateFeeBasis.NONE
    #: Fraction for *_PERCENT bases (e.g. 0.02 = 2%), whole Rial per
    #: day for FIXED_PER_DAY, 0 when basis is NONE. Never hard-coded.
    late_fee_rate: Decimal = Decimal("0")
    #: Days past the due date before the late fee starts accruing.
    late_fee_grace_days: int = 0
    #: Engine account id of the penalty (finance income) account.
    penalty_account_engine_id: int | None = None
    #: Chart code of the penalty account, for display/audit.
    penalty_account_code: str | None = None
    #: Finance-income rate (US-541): the fraction of principal
    #: charged as finance income over the whole plan (e.g. 0.10 =
    #: 10%). 0 means the plan carries no finance income.
    finance_income_rate: Decimal = Decimal("0")
    #: Dunning offsets (US-541): day counts past the due date at
    #: which reminders are due, strictly increasing (e.g. (3, 7,
    #: 14, 30)). Empty means no dunning schedule.
    dunning_offsets: tuple[int, ...] = ()
    #: Engine account id of the customer-receivable account (US-541):
    #: the debit side of the late-fee journal (the fee increases
    #: what the customer owes). NULL until configured; a contract
    #: without it cannot post a late-fee journal.
    receivable_account_engine_id: int | None = None
    #: Chart code of the receivable account, for display/audit.
    receivable_account_code: str | None = None
    early_settlement_fee_policy: EarlySettlementFeePolicy = EarlySettlementFeePolicy.WAIVED
    early_settlement_income_policy: EarlySettlementIncomePolicy = (
        EarlySettlementIncomePolicy.WAIVED
    )
    #: Discount fraction (US-541) applied to the remaining
    #: unrealised income when the income policy is ``discounted``
    #: (e.g. 0.20 = the customer pays 80% of it). Must be 0 unless
    #: the policy is ``discounted``, and strictly between 0 and 1
    #: when it is.
    early_settlement_discount_rate: Decimal = Decimal("0")
    supersedes_contract_id: str | None = None
    is_active: bool = True
    created_at: datetime.datetime = field(default_factory=lambda: datetime.datetime.now(datetime.UTC))
    updated_at: datetime.datetime = field(default_factory=lambda: datetime.datetime.now(datetime.UTC))

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("contract version must be >= 1.")
        if self.late_fee_grace_days < 0:
            raise ValueError("late_fee_grace_days must be >= 0.")
        if self.late_fee_rate < 0:
            raise ValueError("late_fee_rate must be >= 0.")
        if self.late_fee_basis is LateFeeBasis.NONE and self.late_fee_rate != 0:
            raise ValueError("late_fee_rate must be 0 when late_fee_basis is 'none'.")
        if self.finance_income_rate < 0:
            raise ValueError("finance_income_rate must be >= 0.")
        if self.early_settlement_discount_rate < 0:
            raise ValueError("early_settlement_discount_rate must be >= 0.")
        if self.early_settlement_income_policy is EarlySettlementIncomePolicy.DISCOUNTED:
            if not (Decimal("0") < self.early_settlement_discount_rate < Decimal("1")):
                raise ValueError(
                    "early_settlement_discount_rate must be between 0 and 1 "
                    "when early_settlement_income_policy is 'discounted'."
                )
        elif self.early_settlement_discount_rate != 0:
            raise ValueError(
                "early_settlement_discount_rate must be 0 unless "
                "early_settlement_income_policy is 'discounted'."
            )
        if len(self.dunning_offsets) != len(set(self.dunning_offsets)):
            raise ValueError("dunning_offsets must be strictly increasing.")
        if any(offset < 1 for offset in self.dunning_offsets):
            raise ValueError("dunning_offsets must all be >= 1.")
        if list(self.dunning_offsets) != sorted(self.dunning_offsets):
            raise ValueError("dunning_offsets must be strictly increasing.")

    def touch(self) -> None:
        self.updated_at = datetime.datetime.now(datetime.UTC)


@dataclass(frozen=True, slots=True)
class LegPrincipalSnapshot:
    """One leg's principal in a schedule amendment's before/after record."""

    leg_id: str
    sequence: int
    principal: MoneyIRR


@dataclass(slots=True)
class LateFeeAssessment:
    """A late-fee assessment for one leg (US-541).

    A cumulative snapshot: ``fee`` is the TOTAL fee accrued on the
    leg through ``assessed_through`` (computed from the original due
    date, per the contract). The journal covers only the INCREMENT
    since the previous journaled assessment — the assessment row is
    what makes repeated dunning runs idempotent: a run whose
    computed fee equals the last assessment's fee records nothing.
    ``journal_move_id`` is None when no journal was created for the
    increment (no engine wired, or the run had no accounts to post
    to); the unjournaled portion is picked up by the next run that
    can journal it, because the journaled total is read from the
    latest assessment WITH a journal, never from the latest row.
    """

    id: str
    leg_id: str
    plan_id: str
    tenant_id: str
    installation_id: str
    assessed_through: FiscalDate
    days_overdue: int
    fee: MoneyIRR
    journal_move_id: int | None = None
    journal_state: LateFeeJournalState = LateFeeJournalState.NONE
    created_at: datetime.datetime = field(default_factory=lambda: datetime.datetime.now(datetime.UTC))

    @property
    def fee_irr(self) -> int:
        return self.fee.amount


@dataclass(slots=True)
class ScheduleAmendment:
    """A recorded change to a plan's schedule (US-541).

    Re-amortisation rewrites leg principals — a destructive-looking
    change that must stay auditable. The amendment records the
    before/after principals per leg, how much advance was applied,
    and which receipt triggered it (the partial settlement receipt).
    """

    id: str
    plan_id: str
    tenant_id: str
    installation_id: str
    kind: AmendmentKind
    applied_advance: MoneyIRR
    legs_before: tuple[LegPrincipalSnapshot, ...]
    legs_after: tuple[LegPrincipalSnapshot, ...]
    receipt_id: str | None = None
    created_at: datetime.datetime = field(default_factory=lambda: datetime.datetime.now(datetime.UTC))

    @property
    def applied_advance_irr(self) -> int:
        return self.applied_advance.amount


def new_plan_id() -> str:
    return str(uuid.uuid4())


def new_leg_id() -> str:
    return str(uuid.uuid4())


def new_receipt_id() -> str:
    return str(uuid.uuid4())


def new_contract_id() -> str:
    return str(uuid.uuid4())


def new_assessment_id() -> str:
    return str(uuid.uuid4())


def new_amendment_id() -> str:
    return str(uuid.uuid4())
