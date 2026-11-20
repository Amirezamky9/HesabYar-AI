"""Toman→Rial conversion at the connector boundary (ADR 0003 decision 4).

Inside HesabYar everything is whole Rial (``MoneyIRR``, digits=0).
Stores may price in Toman; the conversion happens exactly once, at
the connector boundary, explicitly and with a recorded rounding —
never silently (spec §19 rule 7).

Rules:

- 1 Toman = 10 Rial, exact (``Decimal`` arithmetic only — no floats).
- A Toman amount that does not convert to a whole Rial is rounded
  HALF_UP to the nearest Rial, and the rounding is reported in the
  result (``was_rounded`` + ``rounding_delta_rial``) so the
  normalized event carries the evidence.
- A store that claims Rial prices must send whole Rial amounts: a
  fractional Rial amount is a data error (fail-closed), never
  silently rounded.
- Negative amounts are legitimate (refunds, credit notes).
- Persian/Arabic-Indic digits in string amounts are normalized to
  ASCII before parsing — store payloads are JSON, but a human-entered
  manual event may carry Persian digits.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from hesabyar.domain.common.money import MoneyIRR
from hesabyar.domain.events.errors import InvalidStoreAmountError

#: Exact Toman→Rial rate (1 Toman = 10 Rial).
TOMAN_TO_RIAL_RATE = Decimal(10)

#: ISO-ish code used for Toman in store payloads (no official ISO
#: code exists for Toman; "IRT" is the established convention).
TOMAN_CURRENCY_CODE = "IRT"

#: Rial currency code.
RIAL_CURRENCY_CODE = "IRR"

_PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def parse_store_amount(value: Any) -> Decimal:
    """Parse a store amount into an exact Decimal.

    Accepts ``int``, ``Decimal`` and numeric strings (with optional
    Persian/Arabic-Indic digits, thousands separators and surrounding
    whitespace). Floats, booleans, nulls and non-finite values are
    rejected — a store amount must be exact.
    """
    if isinstance(value, bool) or value is None:
        raise InvalidStoreAmountError(
            f"Store amount must be a number, got {type(value).__name__}.",
            details={"value": str(value)},
        )
    if isinstance(value, float):
        raise InvalidStoreAmountError(
            "Float store amounts are forbidden; send an integer or a decimal string.",
            details={"value": str(value)},
        )
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, int):
        parsed = Decimal(value)
    elif isinstance(value, str):
        cleaned = value.strip().translate(_PERSIAN_DIGITS).replace(",", "").replace("٬", "")
        if not cleaned:
            raise InvalidStoreAmountError("Store amount string is empty.", details={"value": value})
        try:
            parsed = Decimal(cleaned)
        except InvalidOperation as exc:
            raise InvalidStoreAmountError(
                f"Store amount is not a valid number: {value!r}.",
                details={"value": value},
            ) from exc
    else:
        raise InvalidStoreAmountError(
            f"Store amount must be a number, got {type(value).__name__}.",
            details={"value": str(value)},
        )
    if not parsed.is_finite():
        raise InvalidStoreAmountError(
            f"Store amount must be finite, got {value!r}.",
            details={"value": str(value)},
        )
    return parsed


@dataclass(frozen=True, slots=True)
class ConvertedAmount:
    """One converted store amount, with its rounding evidence.

    ``exact_rial`` is the unrounded Rial value; ``amount_rial`` is the
    whole-Rial result. When ``was_rounded`` is False the two agree
    exactly and ``rounding_delta_rial`` is zero.
    """

    amount_rial: MoneyIRR
    source_amount: Decimal
    source_currency: str
    exact_rial: Decimal
    was_rounded: bool
    rounding_delta_rial: Decimal


def convert_store_amount(
    value: Any,
    *,
    prices_in_toman: bool,
) -> ConvertedAmount:
    """Convert one store amount to whole Rial at the connector boundary.

    ``prices_in_toman`` comes from the per-store setting (``StoreSettings``):
    True converts Toman→Rial (×10, HALF_UP rounding when the result is
    not whole); False requires the amount to already be whole Rial.
    """
    source = parse_store_amount(value)
    if prices_in_toman:
        exact = source * TOMAN_TO_RIAL_RATE
        source_currency = TOMAN_CURRENCY_CODE
    else:
        exact = source
        source_currency = RIAL_CURRENCY_CODE
    if exact == exact.to_integral_value():
        amount = MoneyIRR(int(exact))
        return ConvertedAmount(
            amount_rial=amount,
            source_amount=source,
            source_currency=source_currency,
            exact_rial=exact,
            was_rounded=False,
            rounding_delta_rial=Decimal(0),
        )
    if not prices_in_toman:
        # A store claiming Rial prices sent a fractional Rial amount:
        # a data error, never a silent rounding (spec §19 rule 7).
        raise InvalidStoreAmountError(
            f"Rial amount {source} is not a whole number of Rials.",
            details={"value": str(source)},
        )
    rounded = exact.quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return ConvertedAmount(
        amount_rial=MoneyIRR(int(rounded)),
        source_amount=source,
        source_currency=source_currency,
        exact_rial=exact,
        was_rounded=True,
        rounding_delta_rial=rounded - exact,
    )
