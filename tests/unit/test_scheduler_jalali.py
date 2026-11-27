"""Unit tests for Jalali-aware scheduling (US-560).

No Jalali cron exists, so ``next_run`` is computed in-app. These
tests pin the contract: Friday is the weekend default for daily
jobs, Jalali month lengths vary (Esfand 29/30 days) and clamp
honestly, never silently skip.

Ground truths (verified against jdatetime):
- 2026-11-20 is a Friday (1405/08/29); 2026-11-21 is a Saturday.
- 1404 Esfand has 29 days (1404/12/29 = 2026-03-20, a Friday);
  1403 Esfand has 30 days (1403/12/30 = 2025-03-20, a Thursday).
- Bahman (month 11) has 30 days: 1405/11/30 = 2027-02-19 (a Friday).
- 1405/01/01 = 2026-03-21 (a Saturday).
"""

from __future__ import annotations

import datetime

import jdatetime
import pytest

from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.scheduler.errors import InvalidScheduleError
from hesabyar.domain.scheduler.schedules import (
    TEHRAN,
    JobSchedule,
    ScheduleKind,
    compute_next_run,
    is_scheduled_day,
    jalali_month_length,
    tehran_today,
)


def _tehran(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime.datetime:
    return datetime.datetime(year, month, day, hour, minute, tzinfo=TEHRAN)


def _daily(hour: int, minute: int, *, skip_friday: bool = True) -> JobSchedule:
    return JobSchedule(
        kind=ScheduleKind.DAILY,
        time_of_day=datetime.time(hour, minute),
        skip_friday=skip_friday,
    )


def _monthly(jalali_day: int, hour: int = 9, minute: int = 0) -> JobSchedule:
    return JobSchedule(
        kind=ScheduleKind.JALALI_MONTHLY,
        time_of_day=datetime.time(hour, minute),
        jalali_day=jalali_day,
    )


def _yearly(jalali_month: int, jalali_day: int, hour: int = 9) -> JobSchedule:
    return JobSchedule(
        kind=ScheduleKind.JALALI_YEARLY,
        time_of_day=datetime.time(hour, 0),
        jalali_month=jalali_month,
        jalali_day=jalali_day,
    )


def test_jalali_month_length() -> None:
    assert jalali_month_length(1405, 1) == 31  # Farvardin
    assert jalali_month_length(1405, 6) == 31  # Shahrivar
    assert jalali_month_length(1405, 7) == 30  # Mehr
    assert jalali_month_length(1405, 11) == 30  # Bahman
    assert jalali_month_length(1404, 12) == 29  # Esfand, non-leap
    assert jalali_month_length(1403, 12) == 30  # Esfand, leap
    with pytest.raises(InvalidScheduleError):
        jalali_month_length(1405, 13)


def test_daily_next_run_today_when_time_ahead() -> None:
    nxt = compute_next_run(_daily(9, 30), _tehran(2026, 11, 21, 7, 0))
    assert nxt.run_at == _tehran(2026, 11, 21, 9, 30)
    assert not nxt.clamped


def test_daily_next_run_tomorrow_when_time_passed() -> None:
    nxt = compute_next_run(_daily(9, 30), _tehran(2026, 11, 21, 10, 0))
    assert nxt.run_at == _tehran(2026, 11, 22, 9, 30)


def test_daily_skips_friday_by_default() -> None:
    # Thursday 2026-11-19, 10:00 — the next 09:30 is Friday, so it
    # must jump to Saturday 2026-11-21.
    nxt = compute_next_run(_daily(9, 30), _tehran(2026, 11, 19, 10, 0))
    assert nxt.run_at == _tehran(2026, 11, 21, 9, 30)
    assert nxt.run_at.weekday() == 5  # Saturday


def test_daily_on_friday_morning_skips_to_saturday() -> None:
    nxt = compute_next_run(_daily(9, 30), _tehran(2026, 11, 20, 8, 0))
    assert nxt.run_at == _tehran(2026, 11, 21, 9, 30)


def test_daily_runs_on_friday_when_explicitly_configured() -> None:
    nxt = compute_next_run(
        _daily(9, 30, skip_friday=False), _tehran(2026, 11, 20, 8, 0)
    )
    assert nxt.run_at == _tehran(2026, 11, 20, 9, 30)


def test_interval_next_run() -> None:
    schedule = JobSchedule(kind=ScheduleKind.INTERVAL, interval_minutes=15)
    assert compute_next_run(schedule, _tehran(2026, 11, 21, 10, 0)).run_at == _tehran(
        2026, 11, 21, 10, 15
    )
    # Sub-minute input rounds up to the next whole minute (Procrastinate
    # is minute-granular): 10:00:30 + 15 min = 10:15:30 -> 10:16.
    after = datetime.datetime(2026, 11, 21, 10, 0, 30, tzinfo=TEHRAN)
    assert compute_next_run(schedule, after).run_at == _tehran(2026, 11, 21, 10, 16)


def test_interval_jobs_never_skip_friday() -> None:
    schedule = JobSchedule(kind=ScheduleKind.INTERVAL, interval_minutes=15)
    nxt = compute_next_run(schedule, _tehran(2026, 11, 20, 10, 0))  # Friday
    assert nxt.run_at == _tehran(2026, 11, 20, 10, 15)
    assert is_scheduled_day(schedule, datetime.date(2026, 11, 20))


def test_monthly_day_31_clamps_to_30_in_bahman() -> None:
    # 1405/11/29 10:00 Tehran = 2027-02-18 10:00; Bahman has 30 days,
    # so the day-31 schedule fires on Bahman 30 (2027-02-19), clamped.
    nxt = compute_next_run(_monthly(31), _tehran(2027, 2, 18, 10, 0))
    assert nxt.run_at == _tehran(2027, 2, 19, 9, 0)
    assert nxt.clamped
    assert nxt.requested_jalali_day == 31
    assert nxt.actual_jalali_day == 30


def test_monthly_day_31_no_clamp_in_31_day_month() -> None:
    # Mordad (month 5) has 31 days: 1405/05/30 10:00 -> 1405/05/31 09:00.
    after = jdatetime.date(1405, 5, 30).togregorian()
    nxt = compute_next_run(
        _monthly(31), datetime.datetime.combine(after, datetime.time(10, 0), TEHRAN)
    )
    assert FiscalDate(nxt.run_at.date()).jalali_date == jdatetime.date(1405, 5, 31)
    assert not nxt.clamped


def test_monthly_esfand_30_clamps_to_29_in_non_leap_year() -> None:
    # 1404/12/28 10:00 -> 1404/12/29 09:00 (Esfand 1404 has 29 days).
    after = jdatetime.date(1404, 12, 28).togregorian()
    nxt = compute_next_run(
        _monthly(30), datetime.datetime.combine(after, datetime.time(10, 0), TEHRAN)
    )
    assert FiscalDate(nxt.run_at.date()).jalali_date == jdatetime.date(1404, 12, 29)
    assert nxt.clamped
    assert nxt.requested_jalali_day == 30
    assert nxt.actual_jalali_day == 29


def test_monthly_esfand_30_runs_on_30_in_leap_year() -> None:
    # 1403/12/29 10:00 -> 1403/12/30 09:00 (Esfand 1403 has 30 days).
    after = jdatetime.date(1403, 12, 29).togregorian()
    nxt = compute_next_run(
        _monthly(30), datetime.datetime.combine(after, datetime.time(10, 0), TEHRAN)
    )
    assert FiscalDate(nxt.run_at.date()).jalali_date == jdatetime.date(1403, 12, 30)
    assert not nxt.clamped


def test_monthly_advances_past_current_month() -> None:
    # 1405/08/30 (2026-11-21) 10:00, day-1 schedule -> 1405/09/01 09:00.
    nxt = compute_next_run(_monthly(1), _tehran(2026, 11, 21, 10, 0))
    assert FiscalDate(nxt.run_at.date()).jalali_date == jdatetime.date(1405, 9, 1)
    assert nxt.run_at == _tehran(2026, 11, 22, 9, 0)


def test_yearly_esfand_30_clamps_in_non_leap_year() -> None:
    after = jdatetime.date(1404, 12, 28).togregorian()
    nxt = compute_next_run(
        _yearly(12, 30), datetime.datetime.combine(after, datetime.time(10, 0), TEHRAN)
    )
    assert FiscalDate(nxt.run_at.date()).jalali_date == jdatetime.date(1404, 12, 29)
    assert nxt.clamped


def test_yearly_esfand_30_runs_on_30_in_leap_year() -> None:
    after = jdatetime.date(1403, 12, 29).togregorian()
    nxt = compute_next_run(
        _yearly(12, 30), datetime.datetime.combine(after, datetime.time(10, 0), TEHRAN)
    )
    assert FiscalDate(nxt.run_at.date()).jalali_date == jdatetime.date(1403, 12, 30)
    assert not nxt.clamped


def test_yearly_advances_to_next_year() -> None:
    # 1405/01/01 10:00 (2026-03-21), yearly Farvardin 1 -> 1406/01/01 09:00.
    nxt = compute_next_run(_yearly(1, 1), _tehran(2026, 3, 21, 10, 0))
    assert FiscalDate(nxt.run_at.date()).jalali_date == jdatetime.date(1406, 1, 1)


def test_is_scheduled_day_daily() -> None:
    friday = datetime.date(2026, 11, 20)
    saturday = datetime.date(2026, 11, 21)
    assert not is_scheduled_day(_daily(9, 30), friday)
    assert is_scheduled_day(_daily(9, 30), saturday)
    assert is_scheduled_day(_daily(9, 30, skip_friday=False), friday)


def test_is_scheduled_day_monthly_clamped_date() -> None:
    # Day-31 monthly: Bahman 30 (2027-02-19) is the scheduled day;
    # Bahman 29 is not.
    assert is_scheduled_day(_monthly(31), datetime.date(2027, 2, 19))
    assert not is_scheduled_day(_monthly(31), datetime.date(2027, 2, 18))


def test_is_scheduled_day_monthly_fires_on_friday() -> None:
    # 1405/11/30 is a Friday — a monthly job still fires: skipping
    # would lose the whole month.
    assert datetime.date(2027, 2, 19).weekday() == 4  # Friday
    assert is_scheduled_day(_monthly(31), datetime.date(2027, 2, 19))


def test_is_scheduled_day_yearly_clamped_date() -> None:
    # Yearly Esfand 30: in 1404 (29-day Esfand) the scheduled day is
    # Esfand 29 = 2026-03-20.
    assert is_scheduled_day(_yearly(12, 30), datetime.date(2026, 3, 20))
    assert not is_scheduled_day(_yearly(12, 30), datetime.date(2026, 3, 19))
    # In 1403 (leap) it is Esfand 30 = 2025-03-20.
    assert is_scheduled_day(_yearly(12, 30), datetime.date(2025, 3, 20))


def test_schedule_validation() -> None:
    with pytest.raises(InvalidScheduleError):
        JobSchedule(kind=ScheduleKind.INTERVAL, interval_minutes=0)
    with pytest.raises(InvalidScheduleError):
        JobSchedule(kind=ScheduleKind.INTERVAL)
    with pytest.raises(InvalidScheduleError):
        JobSchedule(kind=ScheduleKind.DAILY)
    with pytest.raises(InvalidScheduleError):
        JobSchedule(
            kind=ScheduleKind.JALALI_MONTHLY,
            time_of_day=datetime.time(9, 0),
            jalali_day=32,
        )
    with pytest.raises(InvalidScheduleError):
        JobSchedule(
            kind=ScheduleKind.JALALI_YEARLY,
            time_of_day=datetime.time(9, 0),
            jalali_month=13,
            jalali_day=1,
        )


def test_schedule_from_dict_rejects_bad_input() -> None:
    with pytest.raises(InvalidScheduleError):
        JobSchedule.from_dict({"kind": "weekly"})
    with pytest.raises(InvalidScheduleError):
        JobSchedule.from_dict({"kind": "daily", "time_of_day": "9am"})
    with pytest.raises(InvalidScheduleError):
        JobSchedule.from_dict({"kind": "interval", "interval_minutes": "15"})
    with pytest.raises(InvalidScheduleError):
        JobSchedule.from_dict({"kind": "daily"})  # missing time_of_day


def test_schedule_round_trip() -> None:
    schedules = [
        JobSchedule(kind=ScheduleKind.INTERVAL, interval_minutes=15),
        _daily(9, 30),
        _daily(9, 30, skip_friday=False),
        _monthly(31),
        _yearly(12, 30),
    ]
    for schedule in schedules:
        assert JobSchedule.from_dict(schedule.to_dict()) == schedule


def test_next_run_is_strictly_after() -> None:
    after = _tehran(2026, 11, 21, 9, 30)  # exactly the daily fire time
    nxt = compute_next_run(_daily(9, 30), after)
    assert nxt.run_at > after
    assert nxt.run_at == _tehran(2026, 11, 22, 9, 30)


def test_next_run_accepts_naive_datetime_as_utc() -> None:
    # Naive 2026-11-21 06:30 UTC = 10:00 Tehran; the daily 09:30 run
    # already passed, so the next run is tomorrow 09:30 Tehran.
    nxt = compute_next_run(_daily(9, 30), datetime.datetime(2026, 11, 21, 6, 30))
    assert nxt.run_at == _tehran(2026, 11, 22, 9, 30)


def test_tehran_today_from_timestamp() -> None:
    # 2026-11-20 05:00 UTC = 08:30 Tehran, Friday.
    timestamp = int(datetime.datetime(2026, 11, 20, 5, 0, tzinfo=datetime.UTC).timestamp())
    assert tehran_today(timestamp) == datetime.date(2026, 11, 20)
    assert tehran_today(0) == datetime.datetime.now(TEHRAN).date()
