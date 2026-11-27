"""The Iranian holiday calendar for one installation (US-503, Phase 5 F3).

No verified Iranian-holiday API was found (phased design §8.4), so
the holiday list is operator-maintained per installation until a
source is chosen — and, until then, the settlement/bank matching
window uses CALENDAR days instead of "business days" (§6.6). This
module implements exactly that:

- :meth:`HolidayCalendar.is_holiday` — a date is a holiday when it is
  the weekly holiday (Friday by default) or on the installation's
  configured holiday list.
- :meth:`HolidayCalendar.matching_window` — the ±N-calendar-day window
  around a date (the operative matching window; ``N`` comes from
  ``InstallationSettings.matching_window_days``).
- :meth:`HolidayCalendar.add_business_days` — business-day arithmetic
  for day-lag settlement models (e.g. a 10-working-day lump sum).
  Business days are defined by this same calendar, so the arithmetic
  stays honest about what it knows: it counts only the configured
  holidays, and the docstring on the matching window says plainly
  that the window itself is calendar-based until a holiday source is
  verified.

Pure: no I/O. The application service builds a calendar from the
registry rows.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

__all__ = ["FRIDAY", "HolidayCalendar"]

#: Python weekday number for Friday (Monday=0 … Sunday=6) — the default
#: weekly holiday in Iran (phased design §1.2 F3).
FRIDAY = 4


@dataclass(frozen=True)
class HolidayCalendar:
    """An installation's holiday calendar: weekly holiday + date list."""

    weekly_holiday: int = FRIDAY
    holidays: frozenset[datetime.date] = frozenset()

    def is_holiday(self, day: datetime.date) -> bool:
        """True when ``day`` is the weekly holiday or a listed holiday."""
        return day.weekday() == self.weekly_holiday or day in self.holidays

    def is_business_day(self, day: datetime.date) -> bool:
        """True when ``day`` is neither the weekly holiday nor listed."""
        return not self.is_holiday(day)

    def matching_window(
        self, day: datetime.date, window_days: int
    ) -> tuple[datetime.date, datetime.date]:
        """Return the ±``window_days`` CALENDAR-day window around ``day``.

        Calendar days, not business days: no verified holiday source
        exists yet (§8.4), so a business-day window would silently
        depend on an incomplete holiday list. Callers matching bank
        deposits to expected tranches use this window.
        """
        delta = datetime.timedelta(days=window_days)
        return (day - delta, day + delta)

    def add_business_days(self, day: datetime.date, days: int) -> datetime.date:
        """Add ``days`` business days to ``day`` (day-lag models).

        Counts only days :meth:`is_business_day` accepts, using the
        installation's configured holidays. ``days`` must be >= 0.
        """
        if days < 0:
            raise ValueError(f"days must be >= 0, got {days}")
        current = day
        remaining = days
        while remaining > 0:
            current += datetime.timedelta(days=1)
            if self.is_business_day(current):
                remaining -= 1
        return current

    def add_calendar_days(self, day: datetime.date, days: int) -> datetime.date:
        """Add ``days`` calendar days to ``day`` (plain date arithmetic)."""
        return day + datetime.timedelta(days=days)
