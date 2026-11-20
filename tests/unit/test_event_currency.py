"""Unit tests for Toman→Rial conversion at the connector boundary (US-501).

ADR 0003 decision 4 + spec §19 rule 7: the conversion is explicit,
tested, and never silent. Pure domain tests — no database, no server.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from hesabyar.domain.events.currency import (
    RIAL_CURRENCY_CODE,
    TOMAN_CURRENCY_CODE,
    TOMAN_TO_RIAL_RATE,
    convert_store_amount,
    parse_store_amount,
)
from hesabyar.domain.events.errors import InvalidStoreAmountError


def test_toman_to_rial_rate_is_exactly_10() -> None:
    assert TOMAN_TO_RIAL_RATE == Decimal(10)


def test_toman_amount_converts_times_10() -> None:
    converted = convert_store_amount(175, prices_in_toman=True)
    assert converted.amount_rial.amount == 1750
    assert converted.source_currency == TOMAN_CURRENCY_CODE
    assert converted.exact_rial == Decimal(1750)
    assert converted.was_rounded is False
    assert converted.rounding_delta_rial == Decimal(0)


def test_toman_string_amount_converts() -> None:
    converted = convert_store_amount("175", prices_in_toman=True)
    assert converted.amount_rial.amount == 1750


def test_toman_decimal_amount_converts_exactly() -> None:
    converted = convert_store_amount(Decimal("12.5"), prices_in_toman=True)
    assert converted.amount_rial.amount == 125
    assert converted.was_rounded is False


def test_toman_fractional_rial_rounds_half_up_and_reports_it() -> None:
    """0.05 Toman = 0.5 Rial → rounds HALF_UP to 1 Rial, with evidence."""
    converted = convert_store_amount(Decimal("0.05"), prices_in_toman=True)
    assert converted.amount_rial.amount == 1
    assert converted.was_rounded is True
    assert converted.exact_rial == Decimal("0.5")
    assert converted.rounding_delta_rial == Decimal("0.5")


def test_toman_fractional_rial_rounds_down_below_half() -> None:
    """0.04 Toman = 0.4 Rial → rounds to 0 Rial, with evidence."""
    converted = convert_store_amount(Decimal("0.04"), prices_in_toman=True)
    assert converted.amount_rial.amount == 0
    assert converted.was_rounded is True
    assert converted.rounding_delta_rial == Decimal("-0.4")


def test_negative_toman_amount_converts() -> None:
    """Refunds are negative amounts and convert the same way."""
    converted = convert_store_amount(-175, prices_in_toman=True)
    assert converted.amount_rial.amount == -1750
    converted = convert_store_amount(Decimal("-0.05"), prices_in_toman=True)
    assert converted.amount_rial.amount == -1  # HALF_UP rounds away from zero at .5
    assert converted.was_rounded is True


def test_zero_converts_to_zero() -> None:
    converted = convert_store_amount(0, prices_in_toman=True)
    assert converted.amount_rial.amount == 0
    assert converted.was_rounded is False


def test_rial_amount_passes_through_unchanged() -> None:
    converted = convert_store_amount(1750, prices_in_toman=False)
    assert converted.amount_rial.amount == 1750
    assert converted.source_currency == RIAL_CURRENCY_CODE
    assert converted.was_rounded is False


def test_fractional_rial_is_a_data_error_not_a_silent_rounding() -> None:
    """A store claiming Rial prices must send whole Rial amounts (§19 rule 7)."""
    with pytest.raises(InvalidStoreAmountError, match="whole number"):
        convert_store_amount(Decimal("1750.5"), prices_in_toman=False)


def test_float_amounts_are_forbidden() -> None:
    """No binary floats in financial arithmetic (§19 rule 6)."""
    with pytest.raises(InvalidStoreAmountError, match="Float"):
        convert_store_amount(175.0, prices_in_toman=True)


def test_bool_none_and_nan_are_rejected() -> None:
    with pytest.raises(InvalidStoreAmountError):
        convert_store_amount(True, prices_in_toman=True)
    with pytest.raises(InvalidStoreAmountError):
        convert_store_amount(None, prices_in_toman=True)
    with pytest.raises(InvalidStoreAmountError):
        convert_store_amount(Decimal("NaN"), prices_in_toman=True)
    with pytest.raises(InvalidStoreAmountError):
        convert_store_amount(Decimal("Infinity"), prices_in_toman=True)


def test_garbage_string_is_rejected() -> None:
    with pytest.raises(InvalidStoreAmountError):
        convert_store_amount("one hundred", prices_in_toman=True)
    with pytest.raises(InvalidStoreAmountError):
        convert_store_amount("", prices_in_toman=True)


def test_persian_digits_parse() -> None:
    """A human-entered manual event may carry Persian digits."""
    assert parse_store_amount("۱۷۵") == Decimal(175)
    assert parse_store_amount("١٧٥") == Decimal(175)
    converted = convert_store_amount("۱۷۵", prices_in_toman=True)
    assert converted.amount_rial.amount == 1750


def test_thousands_separators_and_whitespace_parse() -> None:
    assert parse_store_amount(" 1,750 ") == Decimal(1750)
    assert parse_store_amount("۱٬۷۵۰") == Decimal(1750)
