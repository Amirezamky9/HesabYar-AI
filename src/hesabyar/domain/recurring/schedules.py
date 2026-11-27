"""Jalali-aware recurring schedules (US-563, Phase 5-7 ۵خ W3).

A recurring template fires on Jalali dates: monthly on one Jalali
day, every N months on one Jalali day (anchored at the start
month), or yearly on one Jalali date. There is no Jalali cron, so
the occurrence sequence is computed in-app from the schedule spec
— the same discipline as the scheduler's ``next_run``
(:mod:`hesabyar.domain.scheduler.schedules`), which supplies the
month-length helper this module builds on.

Rules (the contract):

- **Clamp honestly, never silently skip.** Jalali month lengths
  vary: months 1–6 have 31 days, 7–11 have 30, Esfand has 29 or 30.
  A monthly schedule for day 31 fires on the 30th in a 30-day
  month and on Esfand 29 in a 29-day year. The clamp is reported
  on every :class:`OccurrenceDate` (``clamped`` + requested/actual
  day) so the preview shows it — the occurrence happens, on a
  visible date.
- **Occurrence numbers are sequence positions.** Numbering counts
  from the first occurrence on or after ``start_date`` (a candidate
  before the start date is not an occurrence and consumes no
  number), so #N is always the Nth scheduled date. The schedule
  shape is immutable once the template has an occurrence (the
  service enforces this), so a number never changes meaning.
- **The sequence is strictly increasing.** Candidates advance one
  month (or N months, or one year) at a time, so ``until``/``after``
  filters can stop early — and a template with no end condition
  still needs an explicit bound (``count`` or ``until``) to
  enumerate, or the call is refused instead of looping forever.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from enum import StrEnum, unique
from typing import Any

from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.recurring.errors import InvalidRecurringTemplateError
from hesabyar.domain.scheduler.schedules import jalali_month_length

#: Backstop for the sequence loop: 1200 monthly steps is 100 years.
#: A correct call always terminates long before this (the caller
#: bounds it with count/until/end/max_occurrences); the cap only
#: bounds a bug.
_MAX_SEQUENCE_STEPS = 1200


@unique
class RecurringScheduleKind(StrEnum):
    """The three schedule shapes a recurring template understands."""

    MONTHLY = "monthly"
    EVERY_N_MONTHS = "every_n_months"
    YEARLY = "yearly"


@dataclass(frozen=True)
class RecurringSchedule:
    """When a recurring template fires, in Jalali terms.

    - ``monthly``: every month on Jalali day ``jalali_day``
      (clamped to the month's length).
    - ``every_n_months``: every ``interval_months`` months on Jalali
      day ``jalali_day``, anchored at ``start_date``'s Jalali month
      (so a template starting 1405/08/15 with N=3 fires in Aban,
      Bahman, Ordibehesht, ... — the anchor month counts as step 0).
    - ``yearly``: every year on Jalali (``jalali_month``,
      ``jalali_day``) (clamped — Esfand 30 becomes Esfand 29 in a
      29-day year).

    ``start_date`` is the first day an occurrence may fall on (a
    candidate before it is skipped, not generated). ``end_date`` /
    ``max_occurrences`` are the two end conditions (either, both,
    or neither — a template with neither runs forever, and the
    generator only ever looks at what is due).
    """

    kind: RecurringScheduleKind
    jalali_day: int
    start_date: datetime.date
    jalali_month: int | None = None
    interval_months: int | None = None
    end_date: datetime.date | None = None
    max_occurrences: int | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.jalali_day <= 31:
            raise InvalidRecurringTemplateError(
                f"jalali_day must be in 1..31, got {self.jalali_day}.",
                details={"jalali_day": self.jalali_day},
            )
        if self.kind is RecurringScheduleKind.YEARLY:
            if self.jalali_month is None or not 1 <= self.jalali_month <= 12:
                raise InvalidRecurringTemplateError(
                    "A yearly schedule needs jalali_month in 1..12.",
                    details={"jalali_month": self.jalali_month},
                )
            if self.interval_months is not None:
                raise InvalidRecurringTemplateError(
                    "A yearly schedule must not carry interval_months.",
                    details={"interval_months": self.interval_months},
                )
        elif self.kind is RecurringScheduleKind.EVERY_N_MONTHS:
            if self.interval_months is None or self.interval_months < 1:
                raise InvalidRecurringTemplateError(
                    "An every-N-months schedule needs interval_months >= 1.",
                    details={"interval_months": self.interval_months},
                )
            if self.jalali_month is not None:
                raise InvalidRecurringTemplateError(
                    "An every-N-months schedule must not carry jalali_month.",
                    details={"jalali_month": self.jalali_month},
                )
        else:  # MONTHLY
            if self.jalali_month is not None:
                raise InvalidRecurringTemplateError(
                    "A monthly schedule must not carry jalali_month.",
                    details={"jalali_month": self.jalali_month},
                )
            if self.interval_months is not None:
                raise InvalidRecurringTemplateError(
                    "A monthly schedule must not carry interval_months.",
                    details={"interval_months": self.interval_months},
                )
        if self.end_date is not None and self.end_date < self.start_date:
            raise InvalidRecurringTemplateError(
                f"end_date {self.end_date.isoformat()} is before start_date "
                f"{self.start_date.isoformat()}.",
                details={
                    "start_date": self.start_date.isoformat(),
                    "end_date": self.end_date.isoformat(),
                },
            )
        if self.max_occurrences is not None and self.max_occurrences < 1:
            raise InvalidRecurringTemplateError(
                f"max_occurrences must be >= 1, got {self.max_occurrences}.",
                details={"max_occurrences": self.max_occurrences},
            )

    def to_dict(self) -> dict[str, Any]:
        """Serialize for the template row's JSONB schedule column."""
        data: dict[str, Any] = {
            "kind": self.kind.value,
            "jalali_day": self.jalali_day,
            "start_date": self.start_date.isoformat(),
        }
        if self.jalali_month is not None:
            data["jalali_month"] = self.jalali_month
        if self.interval_months is not None:
            data["interval_months"] = self.interval_months
        if self.end_date is not None:
            data["end_date"] = self.end_date.isoformat()
        if self.max_occurrences is not None:
            data["max_occurrences"] = self.max_occurrences
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RecurringSchedule:
        """Parse a stored schedule dict (fail closed on bad input)."""
        if not isinstance(data, dict):
            raise InvalidRecurringTemplateError(
                f"A schedule must be an object, got {type(data).__name__}.",
            )
        try:
            kind = RecurringScheduleKind(str(data.get("kind", "")))
        except ValueError as exc:
            raise InvalidRecurringTemplateError(
                f"Unknown schedule kind {data.get('kind')!r}.",
                details={"kind": data.get("kind")},
            ) from exc
        start_date = _parse_date(data.get("start_date"), "start_date")
        end_date = _parse_date(data.get("end_date"), "end_date") if data.get("end_date") else None
        jalali_day = data.get("jalali_day")
        if not isinstance(jalali_day, int) or isinstance(jalali_day, bool):
            raise InvalidRecurringTemplateError(
                f"jalali_day must be an integer, got {jalali_day!r}.",
                details={"jalali_day": jalali_day},
            )
        jalali_month = data.get("jalali_month")
        if jalali_month is not None and (
            isinstance(jalali_month, bool) or not isinstance(jalali_month, int)
        ):
            raise InvalidRecurringTemplateError(
                f"jalali_month must be an integer, got {jalali_month!r}.",
                details={"jalali_month": jalali_month},
            )
        interval_months = data.get("interval_months")
        if interval_months is not None and (
            isinstance(interval_months, bool) or not isinstance(interval_months, int)
        ):
            raise InvalidRecurringTemplateError(
                f"interval_months must be an integer, got {interval_months!r}.",
                details={"interval_months": interval_months},
            )
        max_occurrences = data.get("max_occurrences")
        if max_occurrences is not None and (
            isinstance(max_occurrences, bool) or not isinstance(max_occurrences, int)
        ):
            raise InvalidRecurringTemplateError(
                f"max_occurrences must be an integer, got {max_occurrences!r}.",
                details={"max_occurrences": max_occurrences},
            )
        return cls(
            kind=kind,
            jalali_day=jalali_day,
            start_date=start_date,
            jalali_month=jalali_month,
            interval_months=interval_months,
            end_date=end_date,
            max_occurrences=max_occurrences,
        )

    def shape(self) -> tuple[Any, ...]:
        """The schedule's identity for the edit guard.

        Two schedules with the same shape produce the same occurrence
        dates (and numbers); end conditions are NOT part of the shape
        — tightening or loosening an end only truncates or extends
        the future, it never renumbers what already exists.
        """
        return (
            self.kind,
            self.jalali_day,
            self.start_date,
            self.jalali_month,
            self.interval_months,
        )


def _parse_date(raw: Any, field: str) -> datetime.date:
    """Parse an ISO Gregorian date, or fail closed."""
    if not isinstance(raw, str):
        raise InvalidRecurringTemplateError(
            f"{field} must be an ISO date string, got {raw!r}.",
            details={field: raw},
        )
    try:
        return datetime.date.fromisoformat(raw)
    except ValueError as exc:
        raise InvalidRecurringTemplateError(
            f"{field} must be an ISO date (YYYY-MM-DD), got {raw!r}.",
            details={field: raw},
        ) from exc


@dataclass(frozen=True)
class OccurrenceDate:
    """One scheduled occurrence: its date, number, and clamp report.

    ``scheduled_date`` is Gregorian (the engine's date);
    ``jalali_date`` is the same day as an ISO-like Jalali string
    (``1405-08-15``) for display. ``clamped`` is True when the
    Jalali day was clamped to the target month's length (day 31 in
    Bahman → 30); ``requested_jalali_day`` / ``actual_jalali_day``
    say what was asked and what will run.
    """

    occurrence_number: int
    scheduled_date: datetime.date
    jalali_date: str
    clamped: bool
    requested_jalali_day: int
    actual_jalali_day: int


def format_jalali(day: datetime.date) -> str:
    """Format a Gregorian date as an ISO-like Jalali string (``1405-08-15``)."""
    fiscal = FiscalDate(day)
    return f"{fiscal.jalali_year:04d}-{fiscal.jalali_month:02d}-{fiscal.jalali_day:02d}"


def occurrence_dates(
    schedule: RecurringSchedule,
    *,
    count: int | None = None,
    after: datetime.date | None = None,
    until: datetime.date | None = None,
) -> list[OccurrenceDate]:
    """The schedule's occurrences, in order, with honest clamping.

    - ``count``: at most this many occurrences (the preview's N).
    - ``after``: only occurrences on or after this date — numbering
      is still global (an occurrence before ``after`` consumes its
      number, so #N always means the Nth scheduled date).
    - ``until``: stop at the first occurrence after this date (the
      generator's "due" bound).

    At least one bound is required — ``count``, ``until``, or the
    schedule's own end condition — or the call is refused: an
    unbounded schedule has infinitely many occurrences. The
    sequence is strictly increasing, so ``until`` stops the loop
    early; candidates before ``start_date`` are skipped without
    consuming a number.
    """
    if count is not None and count < 1:
        raise InvalidRecurringTemplateError(
            f"count must be >= 1, got {count}.",
            details={"count": count},
        )
    if (
        count is None
        and until is None
        and schedule.end_date is None
        and schedule.max_occurrences is None
    ):
        raise InvalidRecurringTemplateError(
            "Occurrence enumeration needs a bound: count, until, end_date, "
            "or max_occurrences — this schedule has none.",
            details={"schedule": schedule.to_dict()},
        )
    start = FiscalDate(schedule.start_date)
    year, month = start.jalali_year, start.jalali_month
    found: list[OccurrenceDate] = []
    number = 0
    for _ in range(_MAX_SEQUENCE_STEPS):
        if schedule.kind is RecurringScheduleKind.YEARLY:
            slot_year, slot_month = year, schedule.jalali_month
            assert slot_month is not None  # validated in __post_init__
        else:
            slot_year, slot_month = year, month
        length = jalali_month_length(slot_year, slot_month)
        day = min(schedule.jalali_day, length)
        gregorian = FiscalDate.from_jalali(slot_year, slot_month, day).gregorian_date
        if until is not None and gregorian > until:
            break
        if gregorian >= schedule.start_date:
            if schedule.end_date is not None and gregorian > schedule.end_date:
                break
            number += 1
            if (
                schedule.max_occurrences is not None
                and number > schedule.max_occurrences
            ):
                break
            if after is None or gregorian >= after:
                found.append(
                    OccurrenceDate(
                        occurrence_number=number,
                        scheduled_date=gregorian,
                        jalali_date=format_jalali(gregorian),
                        clamped=day != schedule.jalali_day,
                        requested_jalali_day=schedule.jalali_day,
                        actual_jalali_day=day,
                    )
                )
                if count is not None and len(found) >= count:
                    break
        if schedule.kind is RecurringScheduleKind.YEARLY:
            year += 1
        elif schedule.kind is RecurringScheduleKind.EVERY_N_MONTHS:
            assert schedule.interval_months is not None  # validated in __post_init__
            month += schedule.interval_months
            while month > 12:
                month -= 12
                year += 1
        else:
            month += 1
            if month > 12:
                month = 1
                year += 1
    else:
        raise InvalidRecurringTemplateError(
            f"Could not enumerate the schedule within {_MAX_SEQUENCE_STEPS} steps.",
            details={"schedule": schedule.to_dict()},
        )
    return found
