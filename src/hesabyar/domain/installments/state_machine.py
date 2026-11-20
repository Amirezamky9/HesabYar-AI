"""Installment leg state machine + receipt allocation (US-540, §1.3 ۵ج I1;
US-542 adds ``pending_clearance`` + the cheque receipt split).

Pure functions — no I/O. The leg status, the plan status and the
receipt allocation rule all live here so the application service,
the query service and the tests share exactly one definition of
"what state is this leg in" and "where does this receipt go".

Statuses are COMPUTED from (receipts + cheque states + wall-clock
date), never stored: a leg's status is a function of its due date,
its principal, what has been allocated to it so far (split into
received vs pending clearance by the backing cheques' states), and
today. Nothing in the database can disagree with the receipts,
because there is nothing in the database to disagree with — a
cheque clear/bounce changes the leg's status with NO leg-side
write at all: the next read recomputes it from the cheque's new
state.

Leg status (``expected → due → partially_paid → paid`` +
``overdue`` + ``pending_clearance``):

- ``paid`` — received >= principal (a leg is never overpaid: the
  allocation rule caps every allocation at the leg's balance).
- ``pending_clearance`` — received + pending-clearance covers the
  principal, but the money has not arrived yet (a cheque receipt
  whose cheque has not cleared). This wins over ``overdue``: the
  pending-clearance period is NOT counted as overdue (§5 §2.1),
  and no overdue dunning is sent for such a leg.
- ``overdue`` — past the due date with an unpaid balance > 0. This
  wins over ``partially_paid``: a leg that was partly paid and then
  passed its due date is overdue, not partially paid.
- ``partially_paid`` — something received, balance > 0, due date
  not yet passed. (A leg partly covered by a PENDING cheque is
  not partially paid — the money has not arrived; its pending
  amount is reported alongside the status instead.)
- ``due`` — nothing received, due date reached (today >= due date).
- ``expected`` — nothing received, due date in the future.

A leg is ``due`` ON its due date and ``overdue`` only AFTER it
(the comparison is by calendar day).

Allocation rule (documented in full in ``docs/installments.md``):
a receipt allocates to the oldest overdue leg first (by due date,
then sequence), then to the earliest due leg (``due`` or
``partially_paid``, by due date, then sequence). A receipt NEVER
prepays a not-yet-due (``expected``) leg and NEVER overpays a leg:
whatever is left after every overdue + due leg is covered is the
receipt's ``advance``, recorded on the receipt and held on the plan.
The advance is not auto-applied later — applying it is a US-541
concern (early settlement / re-amortisation). A CHEQUE receipt
allocates to at most ONE leg (``single_leg``): the cheque ↔ leg
link is single-leg by design, so a cheque pays one leg and the
remainder is advance. A ``pending_clearance`` leg takes no further
allocation — it is already fully covered, and a second receipt
must not double-pay it.

The cheque-state import below is the one domain→domain dependency
in this module, and it is deliberate: US-542 IS the cheque ↔ leg
integration, and the split of a receipt's allocations into
received / pending / void depends on the cheque's state. The
dependency is acyclic (the cheque domain carries its own
``InstallmentLegStatus`` and never imports this module).
"""

from __future__ import annotations

import datetime
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from hesabyar.domain.cheques.models import ChequeState
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.domain.installments.models import (
    InstallmentReceipt,
    LegAllocation,
    LegStatus,
    PaymentMethod,
    PlanStatus,
)

#: Cheque states whose receipt allocations count as RECEIVED: only
#: a cleared cheque's money has arrived.
CLEARED_CHEQUE_STATES: frozenset[ChequeState] = frozenset({ChequeState.CLEARED})

#: Cheque states whose receipt allocations count as PENDING
#: CLEARANCE: the cheque is in flight and may still clear.
#: ``registered`` is listed for completeness — a NEW receipt can
#: only name a ``held``/``in_clearing`` cheque (see
#: ``InstallmentChequeNotReceivableError``), so a receipt's cheque
#: is never ``registered`` in practice, but if one ever were, its
#: money is plainly still in flight.
PENDING_CHEQUE_STATES: frozenset[ChequeState] = frozenset(
    {ChequeState.REGISTERED, ChequeState.HELD, ChequeState.IN_CLEARING}
)

#: Every other cheque state (bounced, returned, endorsed, voided,
#: expired, rejected, issued, called_in) voids the receipt's
#: allocations: the money never arrived and never will from that
#: instrument — the leg returns to unpaid.


@dataclass(frozen=True, slots=True)
class LegView:
    """The inputs the state machine needs for one leg: identity,
    due date, principal, what has been received, and what is
    pending clearance (cheque allocations whose cheque has not
    cleared yet)."""

    leg_id: str
    sequence: int
    due_date: FiscalDate
    principal: MoneyIRR
    received: MoneyIRR = MoneyIRR.zero()
    pending_clearance: MoneyIRR = MoneyIRR.zero()

    @property
    def balance(self) -> MoneyIRR:
        """Unpaid principal (never negative — allocation caps at the balance).

        Pending-clearance money does NOT reduce the balance: the
        cheque may bounce, so the leg still owes its full unpaid
        principal until the money arrives.
        """
        remaining = self.principal.amount - self.received.amount
        return MoneyIRR(max(0, remaining))

    @property
    def covered(self) -> MoneyIRR:
        """Received + pending clearance — the amount the leg is covered for."""
        return MoneyIRR(self.received.amount + self.pending_clearance.amount)


def leg_status(
    due_date: FiscalDate,
    principal: MoneyIRR,
    received: MoneyIRR,
    today: datetime.date,
    pending_clearance: MoneyIRR | None = None,
) -> LegStatus:
    """Compute one leg's status from its receipts + the wall-clock date.

    ``today`` is a plain Gregorian date (the caller owns the clock,
    so tests can pin it). The due-date comparison is by calendar
    day: a leg is ``due`` ON its due date and ``overdue`` only
    AFTER it. ``pending_clearance`` is the cheque money in flight
    for this leg (US-542): a leg fully covered by received +
    pending money is ``pending_clearance`` — which wins over
    ``overdue`` — until the cheque clears (paid) or bounces
    (unpaid again).
    """
    pending = pending_clearance.amount if pending_clearance is not None else 0
    if received.amount >= principal.amount:
        return LegStatus.PAID
    if received.amount + pending >= principal.amount:
        return LegStatus.PENDING_CLEARANCE
    if today > due_date.gregorian_date:
        return LegStatus.OVERDUE
    if received.amount > 0:
        return LegStatus.PARTIALLY_PAID
    if today >= due_date.gregorian_date:
        return LegStatus.DUE
    return LegStatus.EXPECTED


def leg_status_for(view: LegView, today: datetime.date) -> LegStatus:
    """Compute a leg's status from its view."""
    return leg_status(
        view.due_date,
        view.principal,
        view.received,
        today,
        pending_clearance=view.pending_clearance,
    )


def split_allocations(
    receipts: Sequence[InstallmentReceipt],
    cheque_states: Mapping[str, ChequeState],
) -> tuple[dict[str, int], dict[str, int]]:
    """Split every receipt's allocations into received vs pending, per leg.

    Returns ``(received_by_leg, pending_by_leg)`` as Rial totals:

    - a receipt that no longer counts toward legs (a rejected /
      cancelled BNPL receipt, US-543) contributes NOTHING — its
      legs return to ``expected`` and can be paid again;
    - a cash / bank-transfer / other receipt's allocations are
      RECEIVED (the money arrived with the receipt);
    - a cheque receipt's allocations follow its cheque's state:
      cleared → received, in flight (registered / held /
      in_clearing) → pending, anything else (bounced, returned,
      endorsed, voided, expired, rejected) → NEITHER (the money
      never arrived — the allocation stays on the receipt as the
      history of the payment attempt, but contributes nothing);
    - a cheque receipt whose cheque is UNKNOWN (no state supplied
      — the cheque row is missing, or no cheque repository was
      wired) is treated as PENDING: fail-closed in both directions
      (the money is never counted as received, and the leg visibly
      shows the pending cover instead of silently dropping it).

    The cheque's state is the single authority for the split: a
    clear/bounce changes the leg's computed status with no leg-side
    write. ``collected_at`` on the receipt is the income-timing
    record (US-541), not an input to this split.
    """
    received_by_leg: dict[str, int] = {}
    pending_by_leg: dict[str, int] = {}
    for receipt in receipts:
        if not receipt.counts_toward_legs:
            # Rejected/cancelled BNPL receipt: dead — contributes
            # nothing, so its legs return to expected (US-543).
            continue
        if receipt.payment_method is not PaymentMethod.CHEQUE:
            target = received_by_leg
        else:
            state = cheque_states.get(receipt.cheque_id or "")
            if state in CLEARED_CHEQUE_STATES:
                target = received_by_leg
            elif state is None or state in PENDING_CHEQUE_STATES:
                target = pending_by_leg
            else:
                continue
        for allocation in receipt.allocations:
            target[allocation.leg_id] = (
                target.get(allocation.leg_id, 0) + allocation.amount.amount
            )
    return received_by_leg, pending_by_leg


def plan_status(legs: list[LegView], today: datetime.date) -> PlanStatus:
    """Derive the plan-level status from its legs (never stored).

    All legs paid → ``paid``; any leg overdue → ``overdue``;
    otherwise ``active``. A plan with no legs is ``active`` (plans
    are always created with ≥ 1 leg; this is a defensive default,
    not a reachable state). A ``pending_clearance`` leg is neither
    paid nor overdue, so a plan whose only unpaid legs are pending
    clearance is ``active`` — the money is in flight, not late.
    """
    if not legs:
        return PlanStatus.ACTIVE
    statuses = [leg_status_for(view, today) for view in legs]
    if all(status is LegStatus.PAID for status in statuses):
        return PlanStatus.PAID
    if any(status is LegStatus.OVERDUE for status in statuses):
        return PlanStatus.OVERDUE
    return PlanStatus.ACTIVE


@dataclass(frozen=True, slots=True)
class AllocationResult:
    """The outcome of allocating one receipt: per-leg slices + advance."""

    allocations: tuple[LegAllocation, ...]
    advance: MoneyIRR

    @property
    def allocated(self) -> MoneyIRR:
        return MoneyIRR(sum(a.amount.amount for a in self.allocations))

    @property
    def total(self) -> MoneyIRR:
        """Allocated + advance == the receipt amount, always."""
        return MoneyIRR(self.allocated.amount + self.advance.amount)


def allocate_receipt(
    amount: MoneyIRR,
    legs: list[LegView],
    today: datetime.date,
    *,
    single_leg: bool = False,
) -> AllocationResult:
    """Allocate one receipt across a plan's legs (the allocation rule).

    Order: oldest overdue leg first (by due date, then sequence),
    then earliest due leg (``due`` / ``partially_paid``, by due
    date, then sequence). ``expected`` legs receive nothing — a
    receipt never prepays a not-yet-due leg — and no leg receives
    more than its balance. The remainder is the ``advance``.
    ``pending_clearance`` legs receive nothing either: they are
    already fully covered by a cheque in flight, and a second
    receipt must not double-pay them (their views carry the
    pending amount, so the status computation excludes them here).

    ``single_leg`` (US-542, cheque receipts only): stop after the
    FIRST leg that takes a slice. A cheque pays ONE leg — the
    cheque ↔ leg link is single-leg by design — so a cheque
    receipt's remainder is advance even when a second leg is due.

    ``amount`` must be positive; a zero or negative receipt is a
    validation error in the caller, and this function refuses it
    defensively rather than allocating nothing silently.
    """
    if amount.amount <= 0:
        raise ValueError("allocate_receipt requires a positive amount.")

    def _order_key(view: LegView) -> tuple[datetime.date, int]:
        return (view.due_date.gregorian_date, view.sequence)

    overdue = sorted(
        (
            view
            for view in legs
            if leg_status_for(view, today) is LegStatus.OVERDUE
        ),
        key=_order_key,
    )
    due = sorted(
        (
            view
            for view in legs
            if leg_status_for(view, today)
            in (LegStatus.DUE, LegStatus.PARTIALLY_PAID)
        ),
        key=_order_key,
    )
    allocations: list[LegAllocation] = []
    remaining = amount.amount
    for view in (*overdue, *due):
        if remaining <= 0:
            break
        take = min(view.balance.amount, remaining)
        if take <= 0:
            continue
        allocations.append(LegAllocation(leg_id=view.leg_id, amount=MoneyIRR(take)))
        remaining -= take
        if single_leg:
            break
    return AllocationResult(
        allocations=tuple(allocations), advance=MoneyIRR(remaining)
    )
