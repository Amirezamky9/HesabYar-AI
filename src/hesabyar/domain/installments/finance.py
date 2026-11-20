"""Installment finance math (US-541, Phase 5-5 ۵ج).

Pure functions — no I/O — for the money side of an installment plan:

- the **principal / finance-income split** per leg, from the
  contract's user-entered rate (never hard-coded): a leg's finance
  income is its principal × the rate, rounded half-up to whole Rial;
- **gradual recognition** of that income on COLLECTION (the
  installment method, phased design §1.3 ۵ج I1): when a leg (or a
  part of it) is collected, the income share of the collected
  portion moves from unrealised to realised. Recognition happens on
  COLLECTION, never on receipt — a bounced cheque never recognises
  income, because the collection never happened (see
  ``docs/installments.md`` for the decision);
- the **late fee** per contract (basis + rate + grace days), computed
  from the leg's ORIGINAL due date — including after a cheque
  bounce, because the obligation was never satisfied;
- **stepped dunning**: the contract's day offsets divide an overdue
  leg's life into stages; the stage + the next reminder date are
  pure functions of (due date, offsets, today);
- the **payoff quote** (early settlement) and **re-amortisation**
  (a partial early payment reduces the remaining schedule);
- **effective contract-version selection**: which version of a
  contract code governs on a given date.

Rounding: every rate multiplication uses
:meth:`MoneyIRR.mul_decimal` (half-up to whole Rial). A leg's
income is computed from ITS OWN principal, so the plan's total
income is the sum of the legs' incomes — never a separately
rounded total that could disagree with the legs by a Rial.
"""

from __future__ import annotations

import datetime
import math
from dataclasses import dataclass
from decimal import Decimal

from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.domain.installments.models import (
    EarlySettlementFeePolicy,
    EarlySettlementIncomePolicy,
    InstallmentSaleContract,
    LateFeeBasis,
    LegStatus,
)
from hesabyar.domain.installments.state_machine import LegView, leg_status_for

#: A dunning schedule has at most ten reminder offsets (a sanity
#: bound against a typo'd list creating a reminder a day for years).
MAX_DUNNING_OFFSETS = 10

#: The largest single dunning offset, in days (ten years).
MAX_DUNNING_OFFSET_DAYS = 3650

#: Days in a "month" for the ``monthly_percent`` late-fee basis.
#: A started month counts (see :func:`compute_late_fee`).
DAYS_PER_FEE_MONTH = 30


# -- finance income: split + gradual recognition on collection ---------------


def leg_finance_income(principal: MoneyIRR, rate: Decimal) -> MoneyIRR:
    """The finance income of one leg: its principal × the contract rate.

    ``rate`` is the contract's user-entered fraction (e.g.
    ``Decimal("0.10")`` = 10% over the whole plan); a rate of 0
    means the plan carries no finance income. Rounded half-up to
    whole Rial, per leg — the plan total is the sum of the legs.
    """
    if rate == 0 or principal.is_zero:
        return MoneyIRR.zero()
    return principal.mul_decimal(rate)


def recognised_income(collected: MoneyIRR, rate: Decimal) -> MoneyIRR:
    """The finance income recognised on a collected amount.

    THE CONSUMPTION HOOK for US-542: this is the function the
    cheque-clear path calls when a leg's collection is CONFIRMED
    (cash receipt recorded, or cheque cleared). It is NEVER called
    on receipt alone and NEVER for a leg in ``pending_clearance`` —
    a cheque that has not cleared has collected nothing, and a
    bounced cheque collects nothing either, so neither recognises
    income. Recognition is pro-rata on the collected portion: a leg
    collected in full recognises exactly ``leg_finance_income``
    (the same multiplication, the same rounding).
    """
    if rate == 0 or collected.is_zero:
        return MoneyIRR.zero()
    return collected.mul_decimal(rate)


# -- late fee -----------------------------------------------------------------


def days_overdue(due_date: FiscalDate, today: datetime.date) -> int:
    """Whole days past a leg's due date (0 when not past it)."""
    return max(0, (today - due_date.gregorian_date).days)


def chargeable_days(days_past_due: int, grace_days: int) -> int:
    """Overdue days that actually accrue a fee (grace days excluded)."""
    return max(0, days_past_due - grace_days)


def compute_late_fee(
    balance: MoneyIRR,
    *,
    basis: LateFeeBasis,
    rate: Decimal,
    grace_days: int,
    days_past_due: int,
) -> MoneyIRR:
    """Compute the late fee on one leg's unpaid balance.

    The fee base is the leg's BALANCE (unpaid principal) — dunning
    continues on what is still owed, not on the original leg
    amount — and the clock runs from the leg's ORIGINAL due date:
    when a cheque bounces, the leg returns to overdue with its due
    date untouched, so the fee accrues from the original due date,
    including across the ``pending_clearance`` window (the
    obligation was never satisfied — see ``docs/installments.md``).

    Bases (all user-entered on the contract):

    - ``none`` — no fee, ever.
    - ``daily_percent`` — ``balance × rate × chargeable days``.
    - ``monthly_percent`` — ``balance × rate × started months``,
      where a started month is ``ceil(chargeable days / 30)``: a
      leg 31 chargeable days overdue has run 2 months. A started
      month counts in full — the rule is stated here so no caller
      has to guess.
    - ``fixed_per_day`` — ``rate × chargeable days`` (rate in whole
      Rial per day).
    """
    if basis is LateFeeBasis.NONE or balance.amount <= 0:
        return MoneyIRR.zero()
    chargeable = chargeable_days(days_past_due, grace_days)
    if chargeable <= 0:
        return MoneyIRR.zero()
    if basis is LateFeeBasis.DAILY_PERCENT:
        return balance.mul_decimal(rate * chargeable)
    if basis is LateFeeBasis.MONTHLY_PERCENT:
        months = math.ceil(chargeable / DAYS_PER_FEE_MONTH)
        return balance.mul_decimal(rate * months)
    # FIXED_PER_DAY: the rate is whole Rial per day.
    fee = (rate * chargeable).quantize(Decimal(1), rounding="ROUND_HALF_UP")
    return MoneyIRR(int(fee))


# -- stepped dunning -----------------------------------------------------------


def dunning_stage(days_past_due: int, offsets: tuple[int, ...]) -> int:
    """The dunning stage of an overdue leg (0 = no reminder due yet).

    The contract's offsets (strictly increasing day counts, e.g.
    ``(3, 7, 14, 30)``) divide the overdue life into stages: the
    stage is the number of offsets the leg has reached. A leg 6
    days overdue with offsets ``(3, 7, 14)`` is stage 1; at 7 days
    it ages into stage 2. Stage 0 means overdue but before the
    first reminder — the leg shows in the overdue report, but no
    review item is filed for it yet.
    """
    return sum(1 for offset in offsets if days_past_due >= offset)


def next_reminder_date(
    due_date: FiscalDate,
    offsets: tuple[int, ...],
    days_past_due: int,
) -> datetime.date | None:
    """The date of the next dunning reminder (None when all offsets passed)."""
    future = [offset for offset in offsets if offset > days_past_due]
    if not future:
        return None
    return due_date.gregorian_date + datetime.timedelta(days=min(future))


# -- effective contract-version selection --------------------------------------


def select_effective_contract(
    versions: list[InstallmentSaleContract],
    on_date: datetime.date,
) -> InstallmentSaleContract | None:
    """Select the contract version in force on ``on_date``.

    Among the ACTIVE versions whose ``effective_from`` is on or
    before ``on_date``, the latest ``effective_from`` wins; a tie
    breaks to the highest version. Returns None when no version is
    in force yet (every version starts in the future, or every
    version is inactive) — the caller fails closed, never guessing
    a version. A plan's terms are fixed at creation by its linked
    version; this selection is for resolving a contract CODE (plan
    creation by code, the ADR 0005 derivation) — a newer version
    never re-prices an existing plan.
    """
    eligible = [
        version
        for version in versions
        if version.is_active and version.effective_from.gregorian_date <= on_date
    ]
    if not eligible:
        return None
    return max(
        eligible,
        key=lambda version: (version.effective_from.gregorian_date, version.version),
    )


# -- per-leg finance assessment -------------------------------------------------


@dataclass(frozen=True, slots=True)
class LegFinanceAssessment:
    """One leg's finance picture at a point in time (pure, derived).

    Everything money-shaped here is computed from (leg, receipts,
    contract, today) — nothing is stored, so the assessment can
    never go stale.
    """

    leg_id: str
    sequence: int
    due_date: FiscalDate
    principal: MoneyIRR
    received: MoneyIRR
    status: LegStatus
    days_past_due: int
    finance_income: MoneyIRR
    income_recognised: MoneyIRR
    late_fee_accrued: MoneyIRR
    dunning_stage: int
    next_reminder: datetime.date | None

    @property
    def balance(self) -> MoneyIRR:
        return MoneyIRR(max(0, self.principal.amount - self.received.amount))

    @property
    def income_unrealised(self) -> MoneyIRR:
        """Finance income not yet recognised on this leg."""
        return MoneyIRR(
            max(0, self.finance_income.amount - self.income_recognised.amount)
        )


def assess_leg(
    view: LegView,
    *,
    contract: InstallmentSaleContract | None,
    today: datetime.date,
) -> LegFinanceAssessment:
    """Assess one leg's finance state (split, recognition, fee, dunning).

    A plan without a contract has no rate, no fee rule and no
    dunning schedule: the leg's income is 0, its fee is 0, its
    stage is 0 — a missing contract means "no finance terms", never
    a default rate.

    The late fee + dunning stage accrue ONLY while the leg is
    ``overdue``: a ``pending_clearance`` leg (a cheque in flight
    covering it in full) is not chased — the customer has paid,
    the money is in transit. If that cheque bounces, the leg
    becomes overdue and the fee accrues from the ORIGINAL due date
    (``days_past_due`` is measured from it, never from the bounce).
    """
    status = leg_status_for(view, today)
    past_due = days_overdue(view.due_date, today)
    rate = contract.finance_income_rate if contract is not None else Decimal("0")
    income = leg_finance_income(view.principal, rate)
    recognised = recognised_income(view.received, rate)
    if contract is None or status is not LegStatus.OVERDUE:
        fee = MoneyIRR.zero()
        stage = 0
        next_reminder: datetime.date | None = None
    else:
        fee = compute_late_fee(
            view.balance,
            basis=contract.late_fee_basis,
            rate=contract.late_fee_rate,
            grace_days=contract.late_fee_grace_days,
            days_past_due=past_due,
        )
        stage = dunning_stage(past_due, contract.dunning_offsets)
        next_reminder = next_reminder_date(view.due_date, contract.dunning_offsets, past_due)
    return LegFinanceAssessment(
        leg_id=view.leg_id,
        sequence=view.sequence,
        due_date=view.due_date,
        principal=view.principal,
        received=view.received,
        status=status,
        days_past_due=past_due,
        finance_income=income,
        income_recognised=recognised,
        late_fee_accrued=fee,
        dunning_stage=stage,
        next_reminder=next_reminder,
    )


# -- payoff quote (early settlement) ---------------------------------------------


@dataclass(frozen=True, slots=True)
class PayoffQuote:
    """The early-settlement payoff quote for one plan (pure, derived).

    ``payoff_total = remaining principal + income due + late fees
    due − advance held`` (floored at 0 — an over-advanced plan owes
    nothing, and the excess stays as advance). ``income_due`` has
    TWO parts:

    - the remaining legs' UNREALISED income, adjusted by the
      contract's early-settlement income policy (``waived`` → 0,
      ``discounted`` → unrealised × (1 − discount rate), ``full``
      → all of it);
    - the income already REALISED on collections to date but not
      yet PAID (``income_realised_unpaid``) — charged in full,
      whatever the policy: it is already earned, and the policy
      governs unrealised income only. Ordinary receipts pay
      principal; the finance income is collected here, at
      settlement (a plan that runs to term without a settlement
      leaves its realised income as an uncollected receivable —
      the final-leg income collection is a later story).
    """

    plan_id: str
    as_of: datetime.date
    contract_id: str | None
    remaining_principal: MoneyIRR
    income_realised: MoneyIRR
    income_realised_unpaid: MoneyIRR
    income_unrealised: MoneyIRR
    income_due: MoneyIRR
    income_policy: EarlySettlementIncomePolicy
    late_fees_accrued: MoneyIRR
    late_fees_due: MoneyIRR
    fee_policy: EarlySettlementFeePolicy
    advance_held: MoneyIRR
    payoff_total: MoneyIRR
    open_leg_count: int


def compute_payoff_quote(
    plan_id: str,
    legs: list[LegView],
    *,
    contract: InstallmentSaleContract | None,
    advance_held: MoneyIRR,
    income_paid: MoneyIRR,
    today: datetime.date,
) -> PayoffQuote:
    """Compute the payoff quote from the legs' current state.

    Open legs are every leg with a balance > 0 (expected, due,
    partially paid, overdue — early settlement closes the future
    legs too, the one exception to "a receipt never prepays a
    not-yet-due leg"). ``income_paid`` is the finance income
    already collected on earlier (settlement) receipts — ordinary
    receipts pay principal only. A plan without a contract settles
    for its remaining principal minus its held advance: no income,
    no fees.
    """
    assessments = [assess_leg(view, contract=contract, today=today) for view in legs]
    open_legs = [a for a in assessments if a.balance.amount > 0]
    remaining_principal = MoneyIRR(sum(a.balance.amount for a in open_legs))
    income_realised = MoneyIRR(sum(a.income_recognised.amount for a in assessments))
    income_realised_unpaid = MoneyIRR(
        max(0, income_realised.amount - income_paid.amount)
    )
    income_unrealised = MoneyIRR(sum(a.income_unrealised.amount for a in open_legs))
    late_fees_accrued = MoneyIRR(sum(a.late_fee_accrued.amount for a in assessments))
    if contract is None:
        income_policy = EarlySettlementIncomePolicy.WAIVED
        fee_policy = EarlySettlementFeePolicy.WAIVED
        income_due = MoneyIRR.zero()
        late_fees_due = MoneyIRR.zero()
    else:
        income_policy = contract.early_settlement_income_policy
        fee_policy = contract.early_settlement_fee_policy
        if income_policy is EarlySettlementIncomePolicy.WAIVED:
            policy_income = MoneyIRR.zero()
        elif income_policy is EarlySettlementIncomePolicy.DISCOUNTED:
            policy_income = income_unrealised.mul_decimal(
                Decimal(1) - contract.early_settlement_discount_rate
            )
        else:  # FULL
            policy_income = income_unrealised
        # Realised-but-unpaid income is charged in full, whatever
        # the policy (it is already earned).
        income_due = policy_income.add(income_realised_unpaid)
        late_fees_due = (
            late_fees_accrued if fee_policy is EarlySettlementFeePolicy.KEPT else MoneyIRR.zero()
        )
    payoff = (
        remaining_principal.amount + income_due.amount + late_fees_due.amount - advance_held.amount
    )
    return PayoffQuote(
        plan_id=plan_id,
        as_of=today,
        contract_id=contract.id if contract is not None else None,
        remaining_principal=remaining_principal,
        income_realised=income_realised,
        income_realised_unpaid=income_realised_unpaid,
        income_unrealised=income_unrealised,
        income_due=income_due,
        income_policy=income_policy,
        late_fees_accrued=late_fees_accrued,
        late_fees_due=late_fees_due,
        fee_policy=fee_policy,
        advance_held=advance_held,
        payoff_total=MoneyIRR(max(0, payoff)),
        open_leg_count=len(open_legs),
    )


def held_advance(total_advance: MoneyIRR, applied_advance: MoneyIRR) -> MoneyIRR:
    """The advance still held on a plan: receipts' advances minus what
    settlements already applied to the schedule (never negative —
    the applied total can never exceed the advances by construction,
    and the floor is defensive)."""
    return MoneyIRR(max(0, total_advance.amount - applied_advance.amount))


# -- re-amortisation (partial early payment) --------------------------------------


def reamortise_balances(
    open_legs: list[LegView],
    remaining_principal: MoneyIRR,
) -> dict[str, MoneyIRR]:
    """Re-split a reduced remaining principal across the open legs.

    Returns the NEW principal per leg id. Due dates are unchanged —
    only the amounts move. The remaining principal is split equally
    across the open legs (the integer remainder on the LAST leg, the
    same rule as schedule generation), and each leg's new principal
    is its received amount PLUS its new balance share — so a leg's
    new principal can never fall below what was already received
    on it, and a zero remaining principal closes every open leg
    (new principal == received). ``open_legs`` must be in sequence
    order (the caller sorts); an empty list returns an empty map.
    """
    if not open_legs:
        return {}
    count = len(open_legs)
    base = remaining_principal.amount // count
    remainder = remaining_principal.amount - base * count
    new_principals: dict[str, MoneyIRR] = {}
    for index, view in enumerate(open_legs):
        new_balance = base + (remainder if index == count - 1 else 0)
        new_principals[view.leg_id] = MoneyIRR(view.received.amount + new_balance)
    return new_principals
