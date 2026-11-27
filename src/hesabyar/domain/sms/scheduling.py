"""Reminder scheduling — which (leg, offset) pairs fire today (US-544).

Pure functions — no I/O. The daily job and the manual trigger
share exactly one definition of "this leg is due a reminder
today": the leg is unpaid, not awaiting cheque clearing, and
today is exactly one of the installation's configured offsets
from its due date.

Jalali-aware scheduling, NO Jalali cron (ADR 0003 decision 9):
the Procrastinate job runs on an ordinary Gregorian cron and the
next run's targets are computed in-app from the Jalali due dates
— ``next_run`` is a computed scan, not a cron expression. An
offset fires ONLY on its day: a missed day (worker down) means a
missed reminder, visible as an absent log row — the message text
always states the true days remaining, so a late catch-up send
would lie about the date. The manual trigger is the recovery path.

Sign convention: ``offset_days`` is negative before the due date
(``-3`` = 3 days before), ``0`` on the day, positive when overdue
(``+1`` = 1 day overdue). It is ``-(due_date - today).days``.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.money import MoneyIRR


@dataclass(frozen=True, slots=True)
class LegReminderView:
    """The inputs the scheduler needs for one leg."""

    leg_id: str
    sequence: int
    due_date: FiscalDate
    principal: MoneyIRR
    received: MoneyIRR = MoneyIRR.zero()
    #: True when the leg was paid by cheque and the cheque has not
    #: cleared yet — the reminder service computes this from the
    #: real cheque states (US-542) via ``split_allocations``; the
    #: explicit caller set unions with the computed one.
    pending_clearance: bool = False

    @property
    def balance(self) -> MoneyIRR:
        """Unpaid principal (never negative — allocation caps at the balance)."""
        return MoneyIRR(max(0, self.principal.amount - self.received.amount))


@dataclass(frozen=True, slots=True)
class DueReminder:
    """One leg that is due a reminder today, at one offset."""

    leg_id: str
    sequence: int
    offset_days: int
    balance: MoneyIRR
    due_date: FiscalDate


def due_reminders(
    legs: list[LegReminderView],
    today: datetime.date,
    offsets_days_before: tuple[int, ...] | list[int],
    offsets_days_overdue: tuple[int, ...] | list[int],
) -> list[DueReminder]:
    """Compute the (leg, offset) pairs that fire on ``today``.

    A leg fires when it is unpaid (balance > 0), not pending
    clearance, and ``today`` is exactly one of the configured
    offsets from its due date. Results are ordered by due date,
    then sequence (the oldest obligation reminds first).
    """
    before = set(offsets_days_before)
    overdue = set(offsets_days_overdue)
    due: list[DueReminder] = []
    for leg in legs:
        if leg.pending_clearance:
            continue
        balance = leg.balance
        if balance.amount <= 0:
            continue
        days_until_due = (leg.due_date.gregorian_date - today).days
        offset: int | None = None
        if days_until_due >= 0 and days_until_due in before:
            offset = -days_until_due
        elif days_until_due < 0 and -days_until_due in overdue:
            offset = -days_until_due
        if offset is None:
            continue
        due.append(
            DueReminder(
                leg_id=leg.leg_id,
                sequence=leg.sequence,
                offset_days=offset,
                balance=balance,
                due_date=leg.due_date,
            )
        )
    due.sort(key=lambda item: (item.due_date.gregorian_date, item.sequence))
    return due
