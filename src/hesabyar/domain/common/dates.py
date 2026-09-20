"""Canonical Gregorian date with bi-directional Jalali and Moadian epoch millisecond conversions."""

from __future__ import annotations

import datetime
import re
from typing import Any, Self

import jdatetime

from hesabyar.domain.common.errors import InvalidDateError

_PERSIAN_ARABIC_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def normalize_digits(value: str) -> str:
    """Normalize Persian and Arabic numerals to ASCII standard digits."""
    return value.translate(_PERSIAN_ARABIC_DIGITS)


class FiscalDate:
    """Immutable domain date value object.

    Canonical representation is strictly Gregorian (datetime.date) to ensure
    persistence and standard database interop, while providing deterministic
    Jalali conversions and Moadian epoch millisecond calculations.
    """

    __slots__ = ("_date",)

    def __init__(self, d: datetime.date | FiscalDate) -> None:
        if isinstance(d, FiscalDate):
            self._date = d._date
        elif isinstance(d, datetime.date):
            if isinstance(d, datetime.datetime):
                self._date = d.date()
            else:
                self._date = d
        else:
            raise InvalidDateError(
                f"FiscalDate requires datetime.date or FiscalDate, got {type(d).__name__}."
            )

    @property
    def gregorian_date(self) -> datetime.date:
        """Return the canonical Gregorian date."""
        return self._date

    @property
    def date(self) -> datetime.date:
        """Alias for gregorian_date."""
        return self._date

    @property
    def jalali_date(self) -> jdatetime.date:
        """Return the date converted to Jalali (Solar Hijri)."""
        return jdatetime.date.fromgregorian(date=self._date)

    @property
    def jalali_year(self) -> int:
        return self.jalali_date.year

    @property
    def jalali_month(self) -> int:
        return self.jalali_date.month

    @property
    def jalali_day(self) -> int:
        return self.jalali_date.day

    @property
    def gregorian_year(self) -> int:
        return self._date.year

    @property
    def gregorian_month(self) -> int:
        return self._date.month

    @property
    def gregorian_day(self) -> int:
        return self._date.day

    @classmethod
    def today(cls) -> Self:
        """Construct a FiscalDate representing current system date."""
        return cls(datetime.datetime.now(tz=datetime.UTC).date())

    @classmethod
    def from_gregorian(cls, d: datetime.date) -> Self:
        """Construct a FiscalDate from a Gregorian date."""
        return cls(d)

    @classmethod
    def from_jalali(cls, year: int, month: int, day: int) -> Self:
        """Construct a FiscalDate from Jalali (Solar Hijri) year, month, day."""
        try:
            jd = jdatetime.date(year, month, day)
            gd = jd.togregorian()
            return cls(gd)
        except (ValueError, TypeError) as exc:
            raise InvalidDateError(
                f"Invalid Jalali date {year:04d}/{month:02d}/{day:02d}: {exc}"
            ) from exc

    @classmethod
    def from_jalali_str(cls, date_str: str) -> Self:
        """Parse Jalali date string in 'YYYY/MM/DD' or 'YYYY-MM-DD' format."""
        if not isinstance(date_str, str):
            raise InvalidDateError(f"Expected str for Jalali date, got {type(date_str).__name__}.")

        normalized = normalize_digits(date_str.strip())
        match = re.match(r"^(\d{4})[/-](\d{1,2})[/-](\d{1,2})$", normalized)
        if not match:
            raise InvalidDateError(
                f"Invalid Jalali date format {date_str!r}. Expected YYYY/MM/DD or YYYY-MM-DD."
            )

        y, m, d = int(match.group(1)), int(match.group(2)), int(match.group(3))
        return cls.from_jalali(y, m, d)

    @classmethod
    def from_iso_str(cls, date_str: str) -> Self:
        """Parse ISO-8601 Gregorian date string 'YYYY-MM-DD'."""
        if not isinstance(date_str, str):
            raise InvalidDateError(f"Expected str for ISO date, got {type(date_str).__name__}.")
        try:
            gd = datetime.date.fromisoformat(normalize_digits(date_str.strip()))
            return cls(gd)
        except ValueError as exc:
            raise InvalidDateError(f"Invalid Gregorian ISO date string {date_str!r}: {exc}") from exc

    @classmethod
    def from_moadian_epoch_ms(cls, epoch_ms: int) -> Self:
        """Construct FiscalDate from Moadian epoch timestamp in milliseconds (UTC)."""
        if isinstance(epoch_ms, bool) or not isinstance(epoch_ms, int):
            raise InvalidDateError(
                f"Moadian epoch timestamp must be integer milliseconds, got {type(epoch_ms).__name__}."
            )
        try:
            dt = datetime.datetime.fromtimestamp(epoch_ms / 1000.0, tz=datetime.UTC)
            return cls(dt.date())
        except (ValueError, OSError, OverflowError) as exc:
            raise InvalidDateError(f"Invalid epoch millisecond value {epoch_ms}: {exc}") from exc

    def to_moadian_epoch_ms(self, time_of_day: datetime.time | None = None) -> int:
        """Convert this date to Moadian epoch milliseconds in UTC."""
        t = time_of_day or datetime.time.min
        dt = datetime.datetime.combine(self._date, t, tzinfo=datetime.UTC)
        return int(dt.timestamp() * 1000)

    def to_jalali_str(self, delimiter: str = "/", pad_zeros: bool = True) -> str:
        """Format as Jalali string (e.g. '1405/01/15')."""
        jd = self.jalali_date
        if pad_zeros:
            return f"{jd.year:04d}{delimiter}{jd.month:02d}{delimiter}{jd.day:02d}"
        return f"{jd.year}{delimiter}{jd.month}{delimiter}{jd.day}"

    def to_gregorian_str(self, delimiter: str = "-") -> str:
        """Format as Gregorian ISO-like string (e.g. '2026-04-04')."""
        return f"{self._date.year:04d}{delimiter}{self._date.month:02d}{delimiter}{self._date.day:02d}"

    def __add__(self, delta: datetime.timedelta) -> FiscalDate:
        if not isinstance(delta, datetime.timedelta):
            return NotImplemented
        return FiscalDate(self._date + delta)

    def __sub__(self, other: FiscalDate | datetime.timedelta) -> FiscalDate | datetime.timedelta:
        if isinstance(other, datetime.timedelta):
            return FiscalDate(self._date - other)
        if isinstance(other, FiscalDate):
            return self._date - other._date
        if isinstance(other, datetime.date):
            return self._date - other
        return NotImplemented

    def __eq__(self, other: object) -> bool:
        if isinstance(other, FiscalDate):
            return self._date == other._date
        if isinstance(other, datetime.date):
            return self._date == other
        return False

    def __lt__(self, other: FiscalDate | datetime.date) -> bool:
        if isinstance(other, FiscalDate):
            return self._date < other._date
        if isinstance(other, datetime.date):
            return self._date < other
        return NotImplemented

    def __le__(self, other: FiscalDate | datetime.date) -> bool:
        if isinstance(other, FiscalDate):
            return self._date <= other._date
        if isinstance(other, datetime.date):
            return self._date <= other
        return NotImplemented

    def __gt__(self, other: FiscalDate | datetime.date) -> bool:
        if isinstance(other, FiscalDate):
            return self._date > other._date
        if isinstance(other, datetime.date):
            return self._date > other
        return NotImplemented

    def __ge__(self, other: FiscalDate | datetime.date) -> bool:
        if isinstance(other, FiscalDate):
            return self._date >= other._date
        if isinstance(other, datetime.date):
            return self._date >= other
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self.__class__, self._date))

    def __repr__(self) -> str:
        return f"FiscalDate({self._date.isoformat()} [Jalali: {self.to_jalali_str()}])"

    def __str__(self) -> str:
        return self.to_jalali_str()

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: Any) -> Any:
        from pydantic_core import core_schema

        def validate(v: Any) -> Any:
            if isinstance(v, cls):
                return v
            if isinstance(v, datetime.date):
                return cls(v)
            if isinstance(v, int):
                return cls.from_moadian_epoch_ms(v)
            if isinstance(v, str):
                cleaned = v.strip()
                # If contains / or starts with 13 or 14 -> Jalali
                if "/" in cleaned or cleaned.startswith(("13", "14")):
                    return cls.from_jalali_str(cleaned)
                return cls.from_iso_str(cleaned)
            raise ValueError(f"Cannot parse FiscalDate from {type(v).__name__}")

        return core_schema.json_or_python_schema(
            json_schema=core_schema.no_info_after_validator_function(validate, core_schema.str_schema()),
            python_schema=core_schema.no_info_plain_validator_function(validate),
            serialization=core_schema.plain_serializer_function_ser_schema(lambda v: v.gregorian_date.isoformat()),
        )
