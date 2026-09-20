"""Deterministic Iranian Rial (IRR) monetary value object and arithmetic rules."""

from __future__ import annotations

from decimal import (
    ROUND_CEILING,
    ROUND_DOWN,
    ROUND_FLOOR,
    ROUND_HALF_DOWN,
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    ROUND_UP,
    Decimal,
)
from enum import StrEnum
from typing import Any, Self

from hesabyar.domain.common.errors import InvalidMoneyError


class DecimalRoundingPolicy(StrEnum):
    """Explicit decimal rounding policies for statutory and financial calculations."""

    HALF_UP = "ROUND_HALF_UP"
    HALF_EVEN = "ROUND_HALF_EVEN"
    HALF_DOWN = "ROUND_HALF_DOWN"
    UP = "ROUND_UP"
    DOWN = "ROUND_DOWN"
    CEILING = "ROUND_CEILING"
    FLOOR = "ROUND_FLOOR"

    def to_decimal_rounding(self) -> str:
        """Map enum value to Python decimal module rounding mode constant."""
        mapping = {
            self.HALF_UP: ROUND_HALF_UP,
            self.HALF_EVEN: ROUND_HALF_EVEN,
            self.HALF_DOWN: ROUND_HALF_DOWN,
            self.UP: ROUND_UP,
            self.DOWN: ROUND_DOWN,
            self.CEILING: ROUND_CEILING,
            self.FLOOR: ROUND_FLOOR,
        }
        return mapping[self]


class MoneyIRR:
    """Immutable monetary value object for Iranian Rials (IRR).

    Enforces:
      - Underlying value MUST be Python int (Rials).
      - Float constructor is strictly forbidden.
      - Reject negative amounts unless explicitly constructed as an adjustment.
      - Explicit arithmetic methods and Decimal rate multiplication.
    """

    __slots__ = ("_amount", "_is_adjustment")

    def __init__(self, amount: int, is_adjustment: bool = False) -> None:
        if isinstance(amount, float):
            raise InvalidMoneyError("Float constructor is strictly forbidden for MoneyIRR.")
        if isinstance(amount, bool) or not isinstance(amount, int):
            raise InvalidMoneyError(
                f"MoneyIRR amount must be a Python int (Rials), got {type(amount).__name__}."
            )

        if amount < 0 and not is_adjustment:
            raise InvalidMoneyError(
                f"Negative monetary amount ({amount}) is forbidden unless explicitly marked as an adjustment."
            )

        self._amount = amount
        self._is_adjustment = is_adjustment

    @property
    def amount(self) -> int:
        """Underlying amount in Iranian Rials (IRR)."""
        return self._amount

    @property
    def is_adjustment(self) -> bool:
        """Indicates if this monetary value represents an accounting adjustment allowing negative amounts."""
        return self._is_adjustment

    @property
    def is_zero(self) -> bool:
        return self._amount == 0

    @property
    def is_positive(self) -> bool:
        return self._amount > 0

    @property
    def is_negative(self) -> bool:
        return self._amount < 0

    @classmethod
    def zero(cls) -> Self:
        """Return zero Rials."""
        return cls(0)

    @classmethod
    def from_int(cls, amount: int, is_adjustment: bool = False) -> Self:
        """Construct MoneyIRR from an integer amount in Rials."""
        return cls(amount, is_adjustment=is_adjustment)

    @classmethod
    def adjustment(cls, amount: int) -> Self:
        """Construct an explicit adjustment monetary value which may be negative."""
        return cls(amount, is_adjustment=True)

    def add(self, other: MoneyIRR) -> MoneyIRR:
        """Add two MoneyIRR amounts."""
        if not isinstance(other, MoneyIRR):
            raise InvalidMoneyError(f"Cannot add {type(other).__name__} to MoneyIRR.")
        new_amount = self._amount + other._amount
        is_adj = self._is_adjustment or other._is_adjustment or (new_amount < 0 and self._is_adjustment)
        return MoneyIRR(new_amount, is_adjustment=is_adj)

    def sub(self, other: MoneyIRR, allow_adjustment: bool = False) -> MoneyIRR:
        """Subtract other MoneyIRR from this amount."""
        if not isinstance(other, MoneyIRR):
            raise InvalidMoneyError(f"Cannot subtract {type(other).__name__} from MoneyIRR.")
        new_amount = self._amount - other._amount
        is_adj = allow_adjustment or self._is_adjustment or other._is_adjustment
        if new_amount < 0 and not is_adj:
            raise InvalidMoneyError(
                f"Subtraction resulted in negative amount ({new_amount}) without adjustment authorization."
            )
        return MoneyIRR(new_amount, is_adjustment=is_adj)

    def mul_decimal(
        self,
        rate: Decimal,
        rounding_mode: DecimalRoundingPolicy | str = DecimalRoundingPolicy.HALF_UP,
    ) -> MoneyIRR:
        """Multiply monetary amount by a Decimal rate using an explicit rounding policy."""
        if isinstance(rate, float):
            raise InvalidMoneyError("Float rate is strictly forbidden; use Decimal.")
        if not isinstance(rate, Decimal):
            try:
                rate = Decimal(str(rate))
            except Exception as e:
                raise InvalidMoneyError(f"Invalid rate for multiplication: {rate!r}") from e

        if isinstance(rounding_mode, str):
            try:
                rounding_mode = DecimalRoundingPolicy(rounding_mode)
            except ValueError:
                # Also try matching without prefix if user passed e.g. "HALF_UP"
                try:
                    rounding_mode = DecimalRoundingPolicy[rounding_mode]
                except KeyError as exc:
                    raise InvalidMoneyError(f"Unknown rounding policy: {rounding_mode}") from exc

        decimal_amount = Decimal(self._amount)
        calculated = decimal_amount * rate
        rounded = calculated.quantize(Decimal(1), rounding=rounding_mode.to_decimal_rounding())
        new_amount = int(rounded)
        return MoneyIRR(new_amount, is_adjustment=self._is_adjustment)

    def to_formatted_string(self, currency_symbol: str = "ریال", show_symbol: bool = True) -> str:
        """Format the monetary amount with thousands separators and the Rial currency symbol."""
        formatted_num = f"{self._amount:,}"
        if show_symbol and currency_symbol:
            return f"{formatted_num} {currency_symbol}"
        return formatted_num

    def to_toman(self) -> Decimal:
        """Convert to Toman for presentation purposes only (1 Toman = 10 Rials)."""
        return Decimal(self._amount) / Decimal(10)

    def __add__(self, other: MoneyIRR) -> MoneyIRR:
        return self.add(other)

    def __sub__(self, other: MoneyIRR) -> MoneyIRR:
        return self.sub(other)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, MoneyIRR):
            return self._amount == other._amount
        return False

    def __lt__(self, other: MoneyIRR) -> bool:
        if not isinstance(other, MoneyIRR):
            return NotImplemented
        return self._amount < other._amount

    def __le__(self, other: MoneyIRR) -> bool:
        if not isinstance(other, MoneyIRR):
            return NotImplemented
        return self._amount <= other._amount

    def __gt__(self, other: MoneyIRR) -> bool:
        if not isinstance(other, MoneyIRR):
            return NotImplemented
        return self._amount > other._amount

    def __ge__(self, other: MoneyIRR) -> bool:
        if not isinstance(other, MoneyIRR):
            return NotImplemented
        return self._amount >= other._amount

    def __hash__(self) -> int:
        return hash((self.__class__, self._amount))

    def __int__(self) -> int:
        return self._amount

    def __repr__(self) -> str:
        adj_str = ", is_adjustment=True" if self._is_adjustment else ""
        return f"MoneyIRR({self._amount}{adj_str})"

    def __str__(self) -> str:
        return self.to_formatted_string()

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: Any) -> Any:
        from pydantic_core import core_schema

        def validate(v: Any) -> Any:
            if isinstance(v, cls):
                return v
            if isinstance(v, float):
                raise ValueError("Float constructor is strictly forbidden for MoneyIRR.")  # noqa: TRY004
            if isinstance(v, bool):
                raise ValueError("Boolean is not an acceptable integer amount for MoneyIRR.")  # noqa: TRY004
            if isinstance(v, int):
                return cls(v)
            if isinstance(v, str):
                cleaned = v.strip().replace(",", "")
                try:
                    return cls(int(cleaned))
                except ValueError as err:
                    raise ValueError(f"Cannot parse MoneyIRR from string {v!r}") from err
            raise ValueError(f"Cannot parse MoneyIRR from {type(v).__name__}")

        return core_schema.json_or_python_schema(
            json_schema=core_schema.no_info_after_validator_function(validate, core_schema.int_schema()),
            python_schema=core_schema.no_info_plain_validator_function(validate),
            serialization=core_schema.plain_serializer_function_ser_schema(lambda v: v.amount),
        )
