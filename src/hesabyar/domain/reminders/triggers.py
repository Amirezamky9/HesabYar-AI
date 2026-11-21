"""Reminder triggers — which (subject, lead) pairs fire today (US-564).

Pure functions — no I/O. The daily job and the tests share exactly
one definition of "this subject is due a reminder today". The
service gathers the views (cheques, legs, boxes, eNamad dates)
and these functions decide what fires.

Day arithmetic is Gregorian day arithmetic on the stored dates
(the same day-count rule as the installment schedule: no
Jalali-month arithmetic anywhere). An offset fires ONLY on its
day: a missed day (worker down, Friday skip) means a missed
reminder for date-offset types — the message text always states
the true days remaining, so a late catch-up send would lie about
the date (the US-544 rule). The cash-box trigger is the
exception: it is a PERSISTENT condition (uncounted for more than
N days), so it fires on every run until the box is counted — the
idempotency key (box + last-count date) keeps it to one reminder
per uncounted period.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from hesabyar.domain.common.dates import FiscalDate


@dataclass(frozen=True, slots=True)
class ChequeDueView:
    """The inputs the cheque trigger needs for one cheque."""

    cheque_id: str
    due_date: FiscalDate
    state: str
    purpose: str
    kind: str


@dataclass(frozen=True, slots=True)
class ChequeDueSoon:
    """One cheque due a reminder today, at one lead."""

    cheque_id: str
    lead_days: int
    due_date: FiscalDate


#: Cheque states whose due date the operator must act on: a held
#: received cheque must be deposited, an issued cheque must be
#: funded, a called-in cheque is commercial now. ``in_clearing``
#: is deliberately absent (the bank has the leaf); ``registered``
#: is absent too (the leaf never arrived — US-531's job owns
#: those reminders); terminal states never fire.
CHEQUE_REMINDABLE_STATES: frozenset[str] = frozenset(
    {"held", "issued", "called_in"}
)


def cheque_due_soon(
    cheques: list[ChequeDueView],
    today: datetime.date,
    lead_days: tuple[int, ...] | list[int],
) -> list[ChequeDueSoon]:
    """Compute the (cheque, lead) pairs that fire on ``today``.

    A cheque fires when it is commercial (``purpose == "payment"``),
    in a remindable state, and ``today`` is exactly one of the
    configured leads before its due date. Results are ordered by
    due date (the most urgent cheque reminds first).
    """
    leads = set(lead_days)
    due: list[ChequeDueSoon] = []
    for cheque in cheques:
        if cheque.purpose != "payment":
            continue
        if cheque.state not in CHEQUE_REMINDABLE_STATES:
            continue
        days_until_due = (cheque.due_date.gregorian_date - today).days
        if days_until_due in leads:
            due.append(
                ChequeDueSoon(
                    cheque_id=cheque.cheque_id,
                    lead_days=days_until_due,
                    due_date=cheque.due_date,
                )
            )
    due.sort(key=lambda item: item.due_date.gregorian_date)
    return due


@dataclass(frozen=True, slots=True)
class CashboxCountView:
    """The inputs the cash-box trigger needs for one box."""

    box_id: str
    box_name: str
    #: Date of the last COMPLETED (archived) count, or the box's
    #: creation date when the box was never counted. None only
    #: when the box has no usable date at all (never fire then).
    reference_date: datetime.date | None
    #: True when the box was never counted (the message says so).
    never_counted: bool
    #: True when a count is currently in progress (any
    #: non-archived session) — the box is being counted, never
    #: remind it.
    has_open_session: bool


@dataclass(frozen=True, slots=True)
class CashboxUncounted:
    """One box due an uncounted reminder today."""

    box_id: str
    box_name: str
    days_uncounted: int
    #: The event identity for the idempotency key: the last count
    #: (or creation) date. A new count changes it, so the next
    #: uncounted period reminds again.
    reference_date: datetime.date
    never_counted: bool


def cashbox_uncounted(
    boxes: list[CashboxCountView],
    today: datetime.date,
    threshold_days: int,
) -> list[CashboxUncounted]:
    """Compute the boxes uncounted for more than ``threshold_days``.

    A box fires when it is active, has no count in progress, and
    ``(today - reference_date).days > threshold_days``. Results
    are ordered longest-uncounted first.
    """
    due: list[CashboxUncounted] = []
    for box in boxes:
        if box.has_open_session:
            continue
        if box.reference_date is None:
            continue
        days = (today - box.reference_date).days
        if days > threshold_days:
            due.append(
                CashboxUncounted(
                    box_id=box.box_id,
                    box_name=box.box_name,
                    days_uncounted=days,
                    reference_date=box.reference_date,
                    never_counted=box.never_counted,
                )
            )
    due.sort(key=lambda item: item.reference_date)
    return due


@dataclass(frozen=True, slots=True)
class EnmadExpiring:
    """The installation's eNamad is due an expiry warning today."""

    lead_days: int
    expiry_date: FiscalDate


def enmad_expiry(
    expiry_date: FiscalDate | None,
    today: datetime.date,
    lead_days: tuple[int, ...] | list[int],
) -> EnmadExpiring | None:
    """Compute whether the eNamad expiry warning fires on ``today``.

    Fires when the expiry date is set and ``today`` is exactly one
    of the configured leads before it (lead 0 = the expiry day).
    An already-expired eNamad (negative days) never fires: the
    service is already disabled, and a reminder cannot undo that
    (the operator needs the renewal, not a daily nag — a
    post-expiry state warning is a future story).
    """
    if expiry_date is None:
        return None
    days_until = (expiry_date.gregorian_date - today).days
    if days_until in set(lead_days):
        return EnmadExpiring(lead_days=days_until, expiry_date=expiry_date)
    return None
