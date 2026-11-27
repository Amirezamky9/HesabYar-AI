"""Unit tests for Jalali-aware recurring schedules (US-563).

The occurrence sequence is computed in-app (no Jalali cron
exists). These tests pin the contract: Jalali month lengths vary
(Esfand 29/30 days) and a day-31 recurrence CLAMPS honestly to
the month's last day — never silently skips — and says so on the
occurrence (``clamped`` + requested/actual day).

Ground truths (verified against jdatetime):
- 1405/08/01 = 2026-10-23; Aban (month 8) has 30 days, so
  1405/08/31 does not exist.
- 1405/08/29 = 2026-11-20 (Friday); 1405/08/30 = 2026-11-21.
- 1405/09/30 = 2026-12-21; 1405/10/30 = 2027-01-20;
  1405/11/30 = 2027-02-19; 1405/12/29 = 2027-03-20.
- 1405 Esfand has 29 days (1405/12/30 invalid); 1404 Esfand has
  29 days (1404/12/29 = 2026-03-20); 1403 Esfand has 30 days
  (1403/12/30 = 2025-03-20, a leap year).
- 1405/01/01 = 2026-03-21; 1406/01/01 = 2027-03-21;
  1406/01/31 = 2027-04-20; 1406/02/15 = 2027-05-05.
"""

from __future__ import annotations

import datetime

import pytest

from hesabyar.domain.recurring.errors import InvalidRecurringTemplateError
from hesabyar.domain.recurring.schedules import (
    RecurringSchedule,
    RecurringScheduleKind,
    format_jalali,
    occurrence_dates,
)


def _monthly(
    jalali_day: int,
    start: datetime.date,
    *,
    end: datetime.date | None = None,
    max_occurrences: int | None = None,
) -> RecurringSchedule:
    return RecurringSchedule(
        kind=RecurringScheduleKind.MONTHLY,
        jalali_day=jalali_day,
        start_date=start,
        end_date=end,
        max_occurrences=max_occurrences,
    )


def _every_n_months(
    jalali_day: int, n: int, start: datetime.date
) -> RecurringSchedule:
    return RecurringSchedule(
        kind=RecurringScheduleKind.EVERY_N_MONTHS,
        jalali_day=jalali_day,
        start_date=start,
        interval_months=n,
    )


def _yearly(
    jalali_month: int, jalali_day: int, start: datetime.date
) -> RecurringSchedule:
    return RecurringSchedule(
        kind=RecurringScheduleKind.YEARLY,
        jalali_day=jalali_day,
        start_date=start,
        jalali_month=jalali_month,
    )


def test_format_jalali() -> None:
    assert format_jalali(datetime.date(2026, 11, 20)) == "1405-08-29"
    assert format_jalali(datetime.date(2026, 3, 21)) == "1405-01-01"
    assert format_jalali(datetime.date(2027, 3, 20)) == "1405-12-29"


def test_monthly_day_15_sequence() -> None:
    dates = occurrence_dates(_monthly(15, datetime.date(2026, 10, 23)), count=3)
    assert [(d.occurrence_number, d.scheduled_date, d.jalali_date) for d in dates] == [
        (1, datetime.date(2026, 11, 6), "1405-08-15"),
        (2, datetime.date(2026, 12, 6), "1405-09-15"),
        (3, datetime.date(2027, 1, 5), "1405-10-15"),
    ]
    assert all(not d.clamped for d in dates)


def test_monthly_day_31_clamps_honestly_through_short_months() -> None:
    """Day 31 fires on the last day of every short month — never skips."""
    dates = occurrence_dates(_monthly(31, datetime.date(2026, 10, 23)), count=6)
    assert [(d.scheduled_date, d.jalali_date, d.clamped, d.actual_jalali_day) for d in dates] == [
        (datetime.date(2026, 11, 21), "1405-08-30", True, 30),  # Aban: 30 days
        (datetime.date(2026, 12, 21), "1405-09-30", True, 30),  # Azar: 30 days
        (datetime.date(2027, 1, 20), "1405-10-30", True, 30),  # Dey: 30 days
        (datetime.date(2027, 2, 19), "1405-11-30", True, 30),  # Bahman: 30 days
        (datetime.date(2027, 3, 20), "1405-12-29", True, 29),  # Esfand: 29 days
        (datetime.date(2027, 4, 20), "1406-01-31", False, 31),  # Farvardin: 31 days
    ]
    assert all(d.requested_jalali_day == 31 for d in dates)
    assert [d.occurrence_number for d in dates] == [1, 2, 3, 4, 5, 6]


def test_monthly_candidate_before_start_is_skipped_without_a_number() -> None:
    """Day 10 with a start on the 15th: Aban's 10th is not occurrence #1."""
    dates = occurrence_dates(_monthly(10, datetime.date(2026, 11, 6)), count=2)
    assert [(d.occurrence_number, d.scheduled_date) for d in dates] == [
        (1, datetime.date(2026, 12, 1)),  # 1405/09/10
        (2, datetime.date(2026, 12, 31)),  # 1405/10/10
    ]


def test_every_n_months_anchors_at_the_start_month() -> None:
    """Every 3 months from Aban: Aban, Bahman, Ordibehesht (not Azar)."""
    dates = occurrence_dates(
        _every_n_months(15, 3, datetime.date(2026, 11, 6)), count=3
    )
    assert [(d.occurrence_number, d.scheduled_date, d.jalali_date) for d in dates] == [
        (1, datetime.date(2026, 11, 6), "1405-08-15"),
        (2, datetime.date(2027, 2, 4), "1405-11-15"),
        (3, datetime.date(2027, 5, 5), "1406-02-15"),
    ]


def test_every_n_months_day_31_clamps() -> None:
    dates = occurrence_dates(
        _every_n_months(31, 2, datetime.date(2026, 10, 23)), count=3
    )
    assert [(d.scheduled_date, d.jalali_date, d.clamped) for d in dates] == [
        (datetime.date(2026, 11, 21), "1405-08-30", True),  # Aban: 30
        (datetime.date(2027, 1, 20), "1405-10-30", True),  # Dey: 30
        (datetime.date(2027, 3, 20), "1405-12-29", True),  # Esfand: 29
    ]


def test_yearly_esfand_30_clamps_only_in_short_years() -> None:
    """Esfand 30 exists in leap 1403, clamps to 29 in 1404 and 1405."""
    dates = occurrence_dates(
        _yearly(12, 30, datetime.date(2024, 3, 20)), count=3
    )
    assert [(d.scheduled_date, d.jalali_date, d.clamped, d.actual_jalali_day) for d in dates] == [
        (datetime.date(2025, 3, 20), "1403-12-30", False, 30),
        (datetime.date(2026, 3, 20), "1404-12-29", True, 29),
        (datetime.date(2027, 3, 20), "1405-12-29", True, 29),
    ]


def test_yearly_farvardin_1() -> None:
    dates = occurrence_dates(_yearly(1, 1, datetime.date(2026, 3, 21)), count=2)
    assert [d.scheduled_date for d in dates] == [
        datetime.date(2026, 3, 21),
        datetime.date(2027, 3, 21),
    ]
    assert all(not d.clamped for d in dates)


def test_yearly_candidate_before_start_is_skipped() -> None:
    """Yearly Farvardin 1 starting mid-year: the first is next year."""
    dates = occurrence_dates(_yearly(1, 1, datetime.date(2026, 11, 6)), count=1)
    assert [(d.occurrence_number, d.scheduled_date) for d in dates] == [
        (1, datetime.date(2027, 3, 21)),
    ]


def test_end_date_truncates_the_sequence() -> None:
    dates = occurrence_dates(
        _monthly(15, datetime.date(2026, 10, 23), end=datetime.date(2026, 12, 22))
    )
    assert [d.scheduled_date for d in dates] == [
        datetime.date(2026, 11, 6),
        datetime.date(2026, 12, 6),
    ]


def test_max_occurrences_truncates_the_sequence() -> None:
    dates = occurrence_dates(
        _monthly(15, datetime.date(2026, 10, 23), max_occurrences=2)
    )
    assert [d.occurrence_number for d in dates] == [1, 2]
    assert dates[-1].scheduled_date == datetime.date(2026, 12, 6)


def test_until_bounds_the_due_window() -> None:
    dates = occurrence_dates(
        _monthly(15, datetime.date(2026, 10, 23)), until=datetime.date(2026, 12, 6)
    )
    assert [d.scheduled_date for d in dates] == [
        datetime.date(2026, 11, 6),
        datetime.date(2026, 12, 6),
    ]


def test_after_keeps_global_numbering() -> None:
    """Previewing from December: the first shown is still #2, not #1."""
    dates = occurrence_dates(
        _monthly(15, datetime.date(2026, 10, 23)),
        count=2,
        after=datetime.date(2026, 12, 1),
    )
    assert [(d.occurrence_number, d.scheduled_date) for d in dates] == [
        (2, datetime.date(2026, 12, 6)),
        (3, datetime.date(2027, 1, 5)),
    ]


def test_unbounded_enumeration_is_refused() -> None:
    with pytest.raises(InvalidRecurringTemplateError, match="needs a bound"):
        occurrence_dates(_monthly(15, datetime.date(2026, 10, 23)))


def test_count_must_be_positive() -> None:
    with pytest.raises(InvalidRecurringTemplateError, match="count must be"):
        occurrence_dates(_monthly(15, datetime.date(2026, 10, 23)), count=0)


def test_schedule_validation() -> None:
    with pytest.raises(InvalidRecurringTemplateError, match="jalali_day"):
        RecurringSchedule(
            kind=RecurringScheduleKind.MONTHLY,
            jalali_day=32,
            start_date=datetime.date(2026, 10, 23),
        )
    with pytest.raises(InvalidRecurringTemplateError, match="jalali_month"):
        RecurringSchedule(
            kind=RecurringScheduleKind.YEARLY,
            jalali_day=1,
            start_date=datetime.date(2026, 10, 23),
        )
    with pytest.raises(InvalidRecurringTemplateError, match="interval_months"):
        RecurringSchedule(
            kind=RecurringScheduleKind.MONTHLY,
            jalali_day=15,
            start_date=datetime.date(2026, 10, 23),
            interval_months=2,
        )
    with pytest.raises(InvalidRecurringTemplateError, match="interval_months"):
        RecurringSchedule(
            kind=RecurringScheduleKind.EVERY_N_MONTHS,
            jalali_day=15,
            start_date=datetime.date(2026, 10, 23),
        )
    with pytest.raises(InvalidRecurringTemplateError, match="end_date"):
        RecurringSchedule(
            kind=RecurringScheduleKind.MONTHLY,
            jalali_day=15,
            start_date=datetime.date(2026, 10, 23),
            end_date=datetime.date(2026, 10, 22),
        )
    with pytest.raises(InvalidRecurringTemplateError, match="max_occurrences"):
        RecurringSchedule(
            kind=RecurringScheduleKind.MONTHLY,
            jalali_day=15,
            start_date=datetime.date(2026, 10, 23),
            max_occurrences=0,
        )


def test_schedule_roundtrip() -> None:
    schedule = RecurringSchedule(
        kind=RecurringScheduleKind.EVERY_N_MONTHS,
        jalali_day=31,
        start_date=datetime.date(2026, 10, 23),
        interval_months=3,
        end_date=datetime.date(2027, 12, 31),
        max_occurrences=12,
    )
    assert RecurringSchedule.from_dict(schedule.to_dict()) == schedule


def test_schedule_from_dict_rejects_bad_input() -> None:
    with pytest.raises(InvalidRecurringTemplateError, match="Unknown schedule kind"):
        RecurringSchedule.from_dict({"kind": "weekly", "jalali_day": 1, "start_date": "2026-10-23"})
    with pytest.raises(InvalidRecurringTemplateError, match="start_date"):
        RecurringSchedule.from_dict(
            {"kind": "monthly", "jalali_day": 15, "start_date": "not-a-date"}
        )


def test_schedule_shape_ignores_end_conditions() -> None:
    base = _monthly(15, datetime.date(2026, 10, 23))
    tighter_end = _monthly(
        15, datetime.date(2026, 10, 23), end=datetime.date(2027, 1, 1)
    )
    different_day = _monthly(16, datetime.date(2026, 10, 23))
    assert base.shape() == tighter_end.shape()
    assert base.shape() != different_day.shape()
