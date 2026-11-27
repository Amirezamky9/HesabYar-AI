"""Jalali-aware job schedules (US-560, phased design §6.5 / §1.3 ۵خ).

No Jalali cron exists, so ``next_run`` is computed in-app from the
schedule spec, in Tehran wall time. Procrastinate still does the
triggering (a Gregorian cron fires the task); the schedule decides
whether the fired run actually executes — see
:func:`is_scheduled_day` and the recorder in
``hesabyar.infrastructure.events.scheduler_queue``.

Rules (the contract):

- **Friday is the weekend default.** A ``daily`` job does not fire on
  Friday unless its schedule sets ``skip_friday=False``. Interval jobs
  (sub-daily polling) never skip — the Friday rule is about
  day-granularity schedules, and a 15-minute reconciliation poll that
  stopped every Friday would silently lose a day of orders.
  Monthly/yearly jobs fire on their Jalali date even when it is a
  Friday: skipping would lose the whole month/year, which is exactly
  the silent skip this module forbids.
- **Clamp honestly, never silently skip.** Jalali month lengths vary:
  months 1–6 have 31 days, 7–11 have 30, Esfand has 29 or 30. A
  monthly schedule for day 31 fires on the 30th in a 30-day month and
  on Esfand 29 in a 29-day year. The clamp is reported on the
  :class:`NextRun` (``clamped`` + requested/actual day) so the
  registry can show it — the run happens, on a visible date.
- **Tehran wall time.** Iran is UTC+3:30 year-round with no DST since
  2022, so the offset is a fixed constant (no tzdata dependency).
  Procrastinate crons are UTC; the registry's ``time_of_day`` is
  Tehran — the UTC↔Tehran mapping per job is asserted by
  ``tests/unit/test_scheduler_queue.py`` so the two can never drift
  silently.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from enum import StrEnum, unique
from typing import Any

import jdatetime

from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.scheduler.errors import InvalidScheduleError

#: Tehran wall time: Iran is UTC+3:30 year-round, no DST since 2022.
TEHRAN = datetime.timezone(datetime.timedelta(hours=3, minutes=30), "Asia/Tehran")

#: Friday (Python weekday: Monday=0 … Sunday=6) — the Iranian weekend.
FRIDAY_WEEKDAY = 4

#: Safety caps for the next-run search loops (a correct schedule
#: always resolves within a couple of steps; the caps only bound a bug).
_MAX_MONTHLY_STEPS = 14
_MAX_YEARLY_STEPS = 3


@unique
class ScheduleKind(StrEnum):
    """The four schedule shapes the registry understands."""

    INTERVAL = "interval"
    DAILY = "daily"
    JALALI_MONTHLY = "jalali_monthly"
    JALALI_YEARLY = "jalali_yearly"


@dataclass(frozen=True)
class JobSchedule:
    """When a job runs, in Tehran wall time.

    Exactly one shape applies (validated in ``__post_init__``):

    - ``interval``: every ``interval_minutes`` minutes.
    - ``daily``: every day at ``time_of_day`` (skipping Friday unless
      ``skip_friday`` is False).
    - ``jalali_monthly``: on Jalali day ``jalali_day`` of every month
      at ``time_of_day`` (clamped to the month's length).
    - ``jalali_yearly``: on Jalali (``jalali_month``, ``jalali_day``)
      every year at ``time_of_day`` (clamped — Esfand 30 becomes
      Esfand 29 in a 29-day year).
    """

    kind: ScheduleKind
    interval_minutes: int | None = None
    time_of_day: datetime.time | None = None
    jalali_day: int | None = None
    jalali_month: int | None = None
    skip_friday: bool = True

    def __post_init__(self) -> None:
        if self.kind is ScheduleKind.INTERVAL:
            if self.interval_minutes is None or self.interval_minutes < 1:
                raise InvalidScheduleError(
                    "An interval schedule needs interval_minutes >= 1.",
                    details={"kind": self.kind.value, "interval_minutes": self.interval_minutes},
                )
        elif self.kind is ScheduleKind.DAILY:
            if self.time_of_day is None:
                raise InvalidScheduleError(
                    "A daily schedule needs time_of_day.",
                    details={"kind": self.kind.value},
                )
        elif self.kind is ScheduleKind.JALALI_MONTHLY:
            if self.time_of_day is None:
                raise InvalidScheduleError(
                    "A Jalali-monthly schedule needs time_of_day.",
                    details={"kind": self.kind.value},
                )
            if self.jalali_day is None or not 1 <= self.jalali_day <= 31:
                raise InvalidScheduleError(
                    "A Jalali-monthly schedule needs jalali_day in 1..31.",
                    details={"kind": self.kind.value, "jalali_day": self.jalali_day},
                )
        elif self.kind is ScheduleKind.JALALI_YEARLY:
            if self.time_of_day is None:
                raise InvalidScheduleError(
                    "A Jalali-yearly schedule needs time_of_day.",
                    details={"kind": self.kind.value},
                )
            if self.jalali_month is None or not 1 <= self.jalali_month <= 12:
                raise InvalidScheduleError(
                    "A Jalali-yearly schedule needs jalali_month in 1..12.",
                    details={"kind": self.kind.value, "jalali_month": self.jalali_month},
                )
            if self.jalali_day is None or not 1 <= self.jalali_day <= 31:
                raise InvalidScheduleError(
                    "A Jalali-yearly schedule needs jalali_day in 1..31.",
                    details={"kind": self.kind.value, "jalali_day": self.jalali_day},
                )
        else:  # pragma: no cover - StrEnum construction rejects unknown kinds first
            raise InvalidScheduleError(
                f"Unknown schedule kind {self.kind!r}.",
                details={"kind": str(self.kind)},
            )

    def to_dict(self) -> dict[str, Any]:
        """Serialize for the registry's JSONB schedule column."""
        data: dict[str, Any] = {"kind": self.kind.value, "skip_friday": self.skip_friday}
        if self.interval_minutes is not None:
            data["interval_minutes"] = self.interval_minutes
        if self.time_of_day is not None:
            data["time_of_day"] = self.time_of_day.strftime("%H:%M")
        if self.jalali_day is not None:
            data["jalali_day"] = self.jalali_day
        if self.jalali_month is not None:
            data["jalali_month"] = self.jalali_month
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JobSchedule:
        """Parse a registry schedule dict (fail closed on bad input)."""
        if not isinstance(data, dict):
            raise InvalidScheduleError(
                f"A schedule must be an object, got {type(data).__name__}.",
            )
        try:
            kind = ScheduleKind(str(data.get("kind", "")))
        except ValueError as exc:
            raise InvalidScheduleError(
                f"Unknown schedule kind {data.get('kind')!r}.",
                details={"kind": data.get("kind")},
            ) from exc
        raw_time = data.get("time_of_day")
        time_of_day: datetime.time | None = None
        if raw_time is not None:
            try:
                time_of_day = datetime.time.fromisoformat(str(raw_time))
            except ValueError as exc:
                raise InvalidScheduleError(
                    f"Invalid time_of_day {raw_time!r}; expected HH:MM.",
                    details={"time_of_day": raw_time},
                ) from exc
        interval = data.get("interval_minutes")
        if interval is not None and (isinstance(interval, bool) or not isinstance(interval, int)):
            raise InvalidScheduleError(
                f"interval_minutes must be an integer, got {interval!r}.",
                details={"interval_minutes": interval},
            )
        jalali_day = data.get("jalali_day")
        if jalali_day is not None and (isinstance(jalali_day, bool) or not isinstance(jalali_day, int)):
            raise InvalidScheduleError(
                f"jalali_day must be an integer, got {jalali_day!r}.",
                details={"jalali_day": jalali_day},
            )
        jalali_month = data.get("jalali_month")
        if jalali_month is not None and (
            isinstance(jalali_month, bool) or not isinstance(jalali_month, int)
        ):
            raise InvalidScheduleError(
                f"jalali_month must be an integer, got {jalali_month!r}.",
                details={"jalali_month": jalali_month},
            )
        return cls(
            kind=kind,
            interval_minutes=interval,
            time_of_day=time_of_day,
            jalali_day=jalali_day,
            jalali_month=jalali_month,
            skip_friday=bool(data.get("skip_friday", True)),
        )


@dataclass(frozen=True)
class NextRun:
    """The next scheduled run of a job, computed in-app.

    ``clamped`` is True when the Jalali day was clamped to the target
    month's length (e.g. day 31 in Bahman → 30); ``requested_jalali_day``
    / ``actual_jalali_day`` say what was asked and what will run.
    """

    run_at: datetime.datetime  # aware, Tehran wall time
    clamped: bool = False
    requested_jalali_day: int | None = None
    actual_jalali_day: int | None = None


def jalali_month_length(year: int, month: int) -> int:
    """Days in a Jalali month: 1–6 have 31, 7–11 have 30, Esfand 29/30.

    Esfand's length is decided by construction (Esfand 30 exists iff
    the year is a Jalali leap year) — no leap-year table to maintain.
    """
    if not 1 <= month <= 12:
        raise InvalidScheduleError(
            f"Jalali month must be in 1..12, got {month}.",
            details={"month": month},
        )
    if month <= 6:
        return 31
    if month <= 11:
        return 30
    try:
        jdatetime.date(year, 12, 30)
    except ValueError:
        return 29
    return 30


def tehran_now() -> datetime.datetime:
    """Current time in Tehran wall time (aware)."""
    return datetime.datetime.now(TEHRAN)


def tehran_today(timestamp: int = 0) -> datetime.date:
    """The Tehran calendar date for a Procrastinate schedule timestamp.

    Periodic tasks receive their scheduled fire time as unix seconds;
    a delayed run still counts for its scheduled day. ``0`` (a manual
    defer, or no timestamp) means "now".
    """
    if timestamp > 0:
        return datetime.datetime.fromtimestamp(timestamp, tz=TEHRAN).date()
    return tehran_now().date()


def _as_tehran(moment: datetime.datetime) -> datetime.datetime:
    """Coerce a datetime to aware Tehran time (naive = UTC, documented)."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.UTC)
    return moment.astimezone(TEHRAN)


def _ceil_to_minute(moment: datetime.datetime) -> datetime.datetime:
    """Round up to the next whole minute (Procrastinate is minute-granular)."""
    floored = moment.replace(second=0, microsecond=0)
    if floored == moment:
        return moment
    return floored + datetime.timedelta(minutes=1)


def is_scheduled_day(schedule: JobSchedule, day: datetime.date) -> bool:
    """Is ``day`` (a Tehran calendar date) a day this job runs?

    Interval jobs run every day. Daily jobs skip Friday by default.
    Monthly/yearly jobs fire on their (clamped) Jalali date whatever
    weekday it is — skipping a Friday monthly date would lose the
    whole month, and the contract is "clamp honestly, never skip".
    """
    if schedule.kind is ScheduleKind.INTERVAL:
        return True
    if schedule.kind is ScheduleKind.DAILY:
        return not (schedule.skip_friday and day.weekday() == FRIDAY_WEEKDAY)
    fiscal = FiscalDate(day)
    if schedule.kind is ScheduleKind.JALALI_MONTHLY:
        assert schedule.jalali_day is not None  # validated in __post_init__
        clamped_day = min(
            schedule.jalali_day,
            jalali_month_length(fiscal.jalali_year, fiscal.jalali_month),
        )
        return fiscal.jalali_day == clamped_day
    # JALALI_YEARLY
    assert schedule.jalali_month is not None and schedule.jalali_day is not None
    if fiscal.jalali_month != schedule.jalali_month:
        return False
    clamped_day = min(
        schedule.jalali_day,
        jalali_month_length(fiscal.jalali_year, fiscal.jalali_month),
    )
    return fiscal.jalali_day == clamped_day


def compute_next_run(schedule: JobSchedule, after: datetime.datetime) -> NextRun:
    """The next run strictly after ``after`` (Tehran wall time).

    ``after`` may be naive (read as UTC) or aware; the returned
    ``run_at`` is always aware Tehran time. Monthly/yearly results
    report clamping honestly via :class:`NextRun`.
    """
    moment = _as_tehran(after)
    if schedule.kind is ScheduleKind.INTERVAL:
        assert schedule.interval_minutes is not None
        candidate = _ceil_to_minute(
            moment + datetime.timedelta(minutes=schedule.interval_minutes)
        )
        return NextRun(run_at=candidate)
    if schedule.kind is ScheduleKind.DAILY:
        assert schedule.time_of_day is not None
        day = moment.date()
        candidate = datetime.datetime.combine(day, schedule.time_of_day, tzinfo=TEHRAN)
        while candidate <= moment or (
            schedule.skip_friday and candidate.weekday() == FRIDAY_WEEKDAY
        ):
            day += datetime.timedelta(days=1)
            candidate = datetime.datetime.combine(day, schedule.time_of_day, tzinfo=TEHRAN)
        return NextRun(run_at=candidate)
    if schedule.kind is ScheduleKind.JALALI_MONTHLY:
        assert schedule.time_of_day is not None and schedule.jalali_day is not None
        fiscal = FiscalDate(moment.date())
        year, month = fiscal.jalali_year, fiscal.jalali_month
        for _ in range(_MAX_MONTHLY_STEPS):
            length = jalali_month_length(year, month)
            day = min(schedule.jalali_day, length)
            gregorian = FiscalDate.from_jalali(year, month, day).gregorian_date
            candidate = datetime.datetime.combine(
                gregorian, schedule.time_of_day, tzinfo=TEHRAN
            )
            if candidate > moment:
                return NextRun(
                    run_at=candidate,
                    clamped=day != schedule.jalali_day,
                    requested_jalali_day=schedule.jalali_day,
                    actual_jalali_day=day,
                )
            month += 1
            if month > 12:
                month = 1
                year += 1
        raise InvalidScheduleError(
            "Could not find the next monthly run within 14 months.",
            details={"schedule": schedule.to_dict(), "after": moment.isoformat()},
        )
    # JALALI_YEARLY
    assert (
        schedule.time_of_day is not None
        and schedule.jalali_month is not None
        and schedule.jalali_day is not None
    )
    fiscal = FiscalDate(moment.date())
    year = fiscal.jalali_year
    for _ in range(_MAX_YEARLY_STEPS):
        length = jalali_month_length(year, schedule.jalali_month)
        day = min(schedule.jalali_day, length)
        gregorian = FiscalDate.from_jalali(year, schedule.jalali_month, day).gregorian_date
        candidate = datetime.datetime.combine(
            gregorian, schedule.time_of_day, tzinfo=TEHRAN
        )
        if candidate > moment:
            return NextRun(
                run_at=candidate,
                clamped=day != schedule.jalali_day,
                requested_jalali_day=schedule.jalali_day,
                actual_jalali_day=day,
            )
        year += 1
    raise InvalidScheduleError(
        "Could not find the next yearly run within 3 years.",
        details={"schedule": schedule.to_dict(), "after": moment.isoformat()},
    )
