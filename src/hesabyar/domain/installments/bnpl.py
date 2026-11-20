"""BNPL leg lifecycle + separation rules (US-543, Phase 5-5 ۵ج, flow I2).

Pure functions — no I/O. A BNPL plan is a sale whose legs are paid
by the gateway in tranches: the customer→gateway installment
schedule is NEVER in the seller's books (phased design §1.3 ۵ج,
flow I2). HesabYar tracks only the gateway receivable + the
gateway's SETTLEMENT tranches to the seller, which the US-521
settlement engine derives from the gateway's contract. A BNPL
receipt creates one ``gateway_payment`` (gross = the receipt
amount) and the leg's lifecycle follows that payment's tranches:

```
expected → gateway_confirmed → settled_per_tranche → paid
```

- ``expected`` — no live BNPL receipt allocates to the leg (or
  every linked payment was cancelled: the gateway rejected it and
  the customer owes again).
- ``gateway_confirmed`` — a BNPL receipt exists and its tranches
  are all still open (``expected`` / ``due`` / ``overdue``): the
  gateway confirmed the payment and owes the seller, but no
  tranche has settled yet.
- ``settled_per_tranche`` — at least one linked tranche has
  settled (or is confirmed/received), but not all of them: partial
  gateway settlement allocates per tranche, and the leg reflects
  exactly that progress.
- ``paid`` — every linked tranche is settled: the gateway has paid
  everything it owes for this leg (net of the fee the settlement
  engine already recognised as the seller's cost).

The status is COMPUTED from the settlement tranches at read time,
never stored — the same rule as the internal leg machine. A
gateway rejection/cancellation returns the leg to ``expected``:
the receipt's ``bnpl_status`` goes dead (``rejected`` /
``cancelled``), its allocations stop counting, and a review-queue
item is filed (the command service owns that transition; this
module only derives the status from whatever tranches are linked).

Separation (criterion 2): BNPL and internal legs are NEVER summed
or netted together — separate plan kinds, separate receivable
accounts (the gateway's receivable account from the mapping
center vs the customer receivable), separate report filters. A
BNPL plan NEVER accrues internal finance income and NEVER enters
dunning — the gateway owns collection. The two predicates at the
bottom of this module are the single place that rule lives, so
US-541 (finance income / dunning) and US-545 (reports) cannot
accidentally include a BNPL plan.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from hesabyar.domain.installments.models import PlanKind, PlanStatus
from hesabyar.domain.settlement.models import SettlementTranche, TrancheState

if TYPE_CHECKING:
    from hesabyar.domain.installments.models import InstallmentPlan

__all__ = [
    "BnplLegStatus",
    "BnplTrancheProgress",
    "bnpl_leg_status",
    "bnpl_plan_status",
    "bnpl_tranche_progress",
    "plan_accrues_finance_income",
    "plan_enters_dunning",
]


class BnplLegStatus(StrEnum):
    """Computed status of one BNPL plan leg (US-543, flow I2).

    Derived by :func:`bnpl_leg_status` from the linked gateway
    payment's settlement tranches — never stored. There is NO
    ``overdue`` here by design: a BNPL leg past its due date is
    still ``gateway_confirmed`` (the gateway owes it, not the
    customer) — the gateway owns collection, so dunning never
    applies.
    """

    EXPECTED = "expected"
    GATEWAY_CONFIRMED = "gateway_confirmed"
    SETTLED_PER_TRANCHE = "settled_per_tranche"
    PAID = "paid"


#: Tranche states that count as settlement progress toward the leg:
#: the gateway has confirmed or paid (part of) the tranche.
_PROGRESS_STATES: frozenset[TrancheState] = frozenset(
    {
        TrancheState.CONFIRMED,
        TrancheState.PARTIALLY_CONFIRMED,
        TrancheState.RECEIVED,
        TrancheState.PARTIALLY_RECEIVED,
        TrancheState.SETTLED,
    }
)

#: Tranche states that end the gateway's obligation for the tranche
#: with nothing paid (a cancel / write-off — the leg's remainder is
#: the customer's debt again, via the review item).
_DEAD_STATES: frozenset[TrancheState] = frozenset(
    {TrancheState.CANCELLED, TrancheState.WRITTEN_OFF}
)


def bnpl_leg_status(tranches: list[SettlementTranche] | tuple[SettlementTranche, ...]) -> BnplLegStatus:
    """Compute one BNPL leg's status from its linked settlement tranches.

    ``tranches`` are the tranches of the leg's ACTIVE BNPL
    receipts' gateway payments (a rejected/cancelled receipt
    contributes no tranches — its legs are ``expected`` again).
    An empty list is ``expected``: no live gateway payment covers
    this leg.
    """
    if not tranches:
        return BnplLegStatus.EXPECTED
    states = [tranche.state for tranche in tranches]
    if all(state is TrancheState.SETTLED for state in states):
        return BnplLegStatus.PAID
    if any(state in _PROGRESS_STATES for state in states):
        return BnplLegStatus.SETTLED_PER_TRANCHE
    if all(state in _DEAD_STATES for state in states):
        # Every linked tranche was cancelled or written off without
        # any settlement: the gateway owes nothing for this leg any
        # more, so the leg is unpaid again (the review item filed
        # at rejection/cancellation owns the follow-up).
        return BnplLegStatus.EXPECTED
    # All tranches still open (expected / due / overdue / exception):
    # the gateway confirmed the payment and the schedule exists, but
    # no tranche has settled yet.
    return BnplLegStatus.GATEWAY_CONFIRMED


def bnpl_plan_status(leg_statuses: list[BnplLegStatus] | tuple[BnplLegStatus, ...]) -> PlanStatus:
    """Derive a BNPL plan's status from its legs (never stored).

    All legs paid → ``paid``; otherwise ``active``. There is no
    ``overdue`` for a BNPL plan, ever: the gateway owns collection,
    so a BNPL plan never enters dunning.
    """
    if leg_statuses and all(status is BnplLegStatus.PAID for status in leg_statuses):
        return PlanStatus.PAID
    return PlanStatus.ACTIVE


@dataclass(frozen=True, slots=True)
class BnplTrancheProgress:
    """Per-tranche settlement progress of a BNPL plan (or leg).

    ``settled_irr`` is the money that has actually landed (received
    nets; a settled tranche contributes its full net). ``open_irr``
    is the gateway receivable still outstanding (open tranches'
    nets minus what they already received). The two never mix with
    internal-installment figures: this progress is gateway money,
    not customer money.
    """

    settled_irr: int
    open_irr: int
    settled_count: int
    total_count: int

    @property
    def is_fully_settled(self) -> bool:
        """True when every tranche is settled (and at least one exists)."""
        return self.total_count > 0 and self.settled_count == self.total_count


def bnpl_tranche_progress(
    tranches: list[SettlementTranche] | tuple[SettlementTranche, ...],
) -> BnplTrancheProgress:
    """Summarise per-tranche settlement progress (partial settlement allocates per tranche).

    Each tranche contributes independently: a partially received
    tranche contributes its received amount, a settled tranche its
    full net, an open tranche its outstanding remainder to
    ``open_irr``. Cancelled / written-off tranches contribute
    nothing to either side — their money is not coming, and it is
    not counted as settled either.
    """
    settled_irr = 0
    open_irr = 0
    settled_count = 0
    for tranche in tranches:
        if tranche.state is TrancheState.SETTLED:
            settled_irr += tranche.net_irr
            settled_count += 1
        elif tranche.state in (TrancheState.RECEIVED, TrancheState.PARTIALLY_RECEIVED):
            settled_irr += tranche.received_net_irr
            open_irr += max(0, tranche.net_irr - tranche.received_net_irr)
        elif tranche.state in _DEAD_STATES:
            continue
        else:
            # Open (expected / due / overdue / exception) or
            # confirmed-but-not-received: nothing landed yet, the
            # whole net is still receivable.
            open_irr += max(0, tranche.net_irr - tranche.received_net_irr)
    return BnplTrancheProgress(
        settled_irr=settled_irr,
        open_irr=open_irr,
        settled_count=settled_count,
        total_count=len(tranches),
    )


def plan_accrues_finance_income(plan: InstallmentPlan) -> bool:
    """True only for INTERNAL plans — a BNPL plan never accrues internal finance income.

    The Snapp Pay "installment price <= cash price" tension is
    resolved by construction: on a BNPL sale the seller charges the
    customer no finance income at all (the gateway's fee is the
    seller's cost, recognised by the settlement engine from the
    gateway contract — never charged to the customer as interest).
    US-541's finance-income recognition must call this before
    accruing anything for a plan.
    """
    return plan.kind is PlanKind.INTERNAL


def plan_enters_dunning(plan: InstallmentPlan) -> bool:
    """True only for INTERNAL plans — a BNPL plan never enters dunning.

    The gateway owns collection on a BNPL sale: HesabYar never
    sends a BNPL customer a due-date reminder or a late fee, and a
    BNPL leg never becomes ``overdue``. US-541's dunning must call
    this before queuing any reminder or fee for a plan.
    """
    return plan.kind is PlanKind.INTERNAL
