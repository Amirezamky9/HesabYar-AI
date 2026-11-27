"""Pure tranche derivation (US-521, phased design §3.1–§3.2).

Derives the expected settlement schedule for one gateway payment from
the ACTIVE contract version at payment time. Pure: no I/O, no clock —
the caller supplies the confirmation time and the holiday calendar.

Due-date algorithm (§3.2):
- Month-based models: Jalali (or Gregorian, per the contract's
  ``payout_calendar``) due month = sale month + ``month_offset``; due
  = ``payout_day`` of that month, clamped to the month's last day
  (a payout_day of 31 in a 30-day month lands on the 30th — the
  contract's day is a target, not an invalid date). When
  ``working_day_adjustment`` is on and the due date is a holiday, it
  moves to the next business day. The payout window runs from
  ``payout_window_start_day`` to the due date of the same month
  (SnappPay: days 1–5; due = day 5).
- Day-based models (SalamPay 10 working days, Sadad ~1 working day):
  ``day_lag`` set on the contract + a single leg with
  ``month_offset=0``; due = ``day_lag`` business days after
  ``gateway_confirmed_at``, counted on the installation's holiday
  calendar. A day-lag contract with more than one leg is a
  configuration error (a lag is a single date, not a schedule).

Money (§3.2, all integer IRR via ``MoneyIRR``):
- ``fee = round(G × c)``, ``fee_vat = round(fee × v)``,
  ``net = G − fee − fee_vat`` — half-up rounding per row, the same
  policy as the tax module (US-402).
- Net, fee and VAT are each split across the legs by percent with
  the LAST tranche = REMAINDER, so the tranches sum exactly to the
  payment totals. Gross per tranche is then ``net + fee + vat`` of
  that tranche, which keeps the per-tranche identity
  ``net = gross − fee − vat`` exact as well.
- ``fee_timing=upfront_first_tranche`` puts the whole fee + VAT on
  the first tranche; the net is still split per legs.

The sale month (the anchor) is the Jalali month of
``gateway_confirmed_at`` — the gateway's confirmation, never the
store order date.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from decimal import Decimal

import jdatetime

from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.money import DecimalRoundingPolicy, MoneyIRR
from hesabyar.domain.integrations.holidays import HolidayCalendar
from hesabyar.domain.integrations.models import GatewayContract, TrancheLeg, validate_tranche_legs
from hesabyar.domain.settlement.errors import SettlementDerivationError
from hesabyar.domain.settlement.models import DerivedTranche, sales_period_from

__all__ = [
    "derive_payment_amounts",
    "derive_tranches",
    "split_by_legs",
]

#: Half-up rounding — the tax module's policy (US-402) and the
#: design's "گرد کردن نیم‌به‌بالا" (§3.2). One constant so the two
#: modules cannot drift apart silently.
ROUNDING = DecimalRoundingPolicy.HALF_UP


def _jalali_month_add(year: int, month: int, offset: int) -> tuple[int, int]:
    """Add ``offset`` months to a Jalali (year, month) pair."""
    total = (year * 12 + (month - 1)) + offset
    return total // 12, total % 12 + 1


def _gregorian_month_add(year: int, month: int, offset: int) -> tuple[int, int]:
    """Add ``offset`` months to a Gregorian (year, month) pair."""
    total = (year * 12 + (month - 1)) + offset
    return total // 12, total % 12 + 1


def _days_in_jalali_month(year: int, month: int) -> int:
    """Days in a Jalali month (29–31, leap-aware via jdatetime)."""
    if month == 12:
        first_next = jdatetime.date(year + 1, 1, 1)
    else:
        first_next = jdatetime.date(year, month + 1, 1)
    first_this = jdatetime.date(year, month, 1)
    return (first_next - first_this).days


def _days_in_gregorian_month(year: int, month: int) -> int:
    """Days in a Gregorian month (28–31, leap-aware)."""
    if month == 12:
        first_next = datetime.date(year + 1, 1, 1)
    else:
        first_next = datetime.date(year, month + 1, 1)
    first_this = datetime.date(year, month, 1)
    return (first_next - first_this).days


def _next_business_day(day: datetime.date, calendar: HolidayCalendar) -> datetime.date:
    """Return ``day`` itself when it is a business day, else the next one."""
    current = day
    while not calendar.is_business_day(current):
        current += datetime.timedelta(days=1)
    return current


def _due_date_for_leg(
    *,
    sale_gregorian: datetime.date,
    leg: TrancheLeg,
    contract: GatewayContract,
    calendar: HolidayCalendar,
) -> tuple[datetime.date, datetime.date]:
    """Return ``(expected_window_start, expected_date)`` for one leg.

    Day-lag contracts ignore the leg's ``payout_day``: the due date
    is ``day_lag`` business days after confirmation, and the window
    starts on the due date itself (a lag has no payout window).
    """
    if contract.day_lag is not None:
        due = calendar.add_business_days(sale_gregorian, contract.day_lag)
        return due, due

    if contract.payout_calendar == "gregorian":
        due_year, due_month = _gregorian_month_add(
            sale_gregorian.year, sale_gregorian.month, leg.month_offset
        )
        month_days = _days_in_gregorian_month(due_year, due_month)
        due_day = min(leg.payout_day, month_days)
        due = datetime.date(due_year, due_month, due_day)
        if contract.payout_window_start_day is not None:
            window_day = min(contract.payout_window_start_day, month_days)
            window_start = datetime.date(due_year, due_month, window_day)
        else:
            window_start = due
    else:
        sale_jalali = FiscalDate(sale_gregorian).jalali_date
        due_year, due_month = _jalali_month_add(
            sale_jalali.year, sale_jalali.month, leg.month_offset
        )
        month_days = _days_in_jalali_month(due_year, due_month)
        due_day = min(leg.payout_day, month_days)
        due = jdatetime.date(due_year, due_month, due_day).togregorian()
        if contract.payout_window_start_day is not None:
            window_day = min(contract.payout_window_start_day, month_days)
            window_start = jdatetime.date(due_year, due_month, window_day).togregorian()
        else:
            window_start = due

    if contract.working_day_adjustment:
        due = _next_business_day(due, calendar)
    return window_start, due


def split_by_legs(total_irr: int, legs: tuple[TrancheLeg, ...]) -> tuple[int, ...]:
    """Split ``total_irr`` across legs by percent; LAST leg = REMAINDER.

    Each non-last share is ``round(total × percent / 100)`` (half-up);
    the last share is whatever remains, so the shares always sum to
    exactly ``total_irr`` — no rial is ever created or lost by
    rounding. The same rule applies to the net, fee and VAT splits
    (phased design §3.2).
    """
    if not legs:
        raise SettlementDerivationError(
            "Cannot split an amount across zero tranche legs.",
            details={"total_irr": total_irr},
        )
    total = MoneyIRR(total_irr)
    shares: list[int] = []
    allocated = 0
    for leg in legs[:-1]:
        share = total.mul_decimal(leg.percent / Decimal(100), ROUNDING).amount
        shares.append(share)
        allocated += share
    shares.append(total_irr - allocated)
    return tuple(shares)


def derive_payment_amounts(
    gross_irr: int, fee_percent: Decimal, fee_vat_rate: Decimal
) -> tuple[int, int, int]:
    """Return ``(fee, fee_vat, net)`` for a gross amount (§3.2 formulas).

    ``fee = round(G × c)``; ``fee_vat = round(fee × v)``;
    ``net = G − fee − fee_vat``. All half-up, all integer IRR.
    """
    if gross_irr <= 0:
        raise SettlementDerivationError(
            f"Gross amount must be positive, got {gross_irr}.",
            details={"gross_irr": gross_irr},
        )
    if fee_percent < 0 or fee_percent >= 100:
        raise SettlementDerivationError(
            f"fee_percent must be in [0, 100), got {fee_percent}.",
            details={"fee_percent": str(fee_percent)},
        )
    if fee_vat_rate < 0 or fee_vat_rate > 100:
        raise SettlementDerivationError(
            f"fee_vat_rate must be in [0, 100], got {fee_vat_rate}.",
            details={"fee_vat_rate": str(fee_vat_rate)},
        )
    gross = MoneyIRR(gross_irr)
    fee = gross.mul_decimal(fee_percent / Decimal(100), ROUNDING).amount
    vat = MoneyIRR(fee).mul_decimal(fee_vat_rate / Decimal(100), ROUNDING).amount
    return fee, vat, gross_irr - fee - vat


@dataclass(frozen=True)
class DerivedSchedule:
    """A payment's derived amounts + tranche schedule."""

    fee_irr: int
    fee_vat_irr: int
    net_irr: int
    tranches: tuple[DerivedTranche, ...]


def derive_tranches(
    *,
    gross_irr: int,
    contract: GatewayContract,
    gateway_confirmed_at: datetime.datetime,
    calendar: HolidayCalendar,
    fee_vat_rate: Decimal,
) -> DerivedSchedule:
    """Derive a payment's full schedule from its contract version.

    Validates the contract's legs first (a bad schedule is rejected
    here, at payment time, not discovered at settlement time), then
    computes the payment amounts and one :class:`DerivedTranche` per
    leg. ``fee_vat_rate`` is the rate actually applied (the contract
    snapshot, or an explicit per-payment override) — the caller
    resolves it; this function never guesses a rate.
    """
    legs = tuple(contract.tranche_legs)
    problems = validate_tranche_legs(legs)
    if problems:
        raise SettlementDerivationError(
            f"Contract version {contract.version} has an invalid tranche schedule: "
            f"{'; '.join(problems)}",
            details={"problems": problems, "contract_version": contract.version},
        )
    if contract.day_lag is not None:
        if len(legs) != 1:
            raise SettlementDerivationError(
                "A day-lag contract must have exactly one tranche leg "
                f"(a lag is a single date), got {len(legs)}.",
                details={"legs": len(legs), "day_lag": contract.day_lag},
            )
        if legs[0].month_offset != 0:
            raise SettlementDerivationError(
                "A day-lag contract's leg must have month_offset=0 "
                f"(the lag counts from confirmation), got {legs[0].month_offset}.",
                details={"month_offset": legs[0].month_offset, "day_lag": contract.day_lag},
            )
        if contract.day_lag < 0:
            raise SettlementDerivationError(
                f"day_lag must be >= 0, got {contract.day_lag}.",
                details={"day_lag": contract.day_lag},
            )

    fee, vat, net = derive_payment_amounts(gross_irr, contract.fee_percent, fee_vat_rate)
    net_shares = split_by_legs(net, legs)
    if contract.fee_timing == "upfront_first_tranche":
        fee_shares = (fee,) + (0,) * (len(legs) - 1)
        vat_shares = (vat,) + (0,) * (len(legs) - 1)
    else:
        fee_shares = split_by_legs(fee, legs)
        vat_shares = split_by_legs(vat, legs)

    sales_period = sales_period_from(gateway_confirmed_at)
    confirmed_date = gateway_confirmed_at.date()
    tranches: list[DerivedTranche] = []
    for index, leg in enumerate(legs):
        window_start, due = _due_date_for_leg(
            sale_gregorian=confirmed_date,
            leg=leg,
            contract=contract,
            calendar=calendar,
        )
        net_share = net_shares[index]
        fee_share = fee_shares[index]
        vat_share = vat_shares[index]
        tranches.append(
            DerivedTranche(
                tranche_index=index + 1,
                tranche_count=len(legs),
                sales_period_jalali=sales_period,
                expected_window_start=window_start,
                expected_date=due,
                gross_irr=net_share + fee_share + vat_share,
                fee_irr=fee_share,
                fee_vat_irr=vat_share,
                net_irr=net_share,
            )
        )
    return DerivedSchedule(fee_irr=fee, fee_vat_irr=vat, net_irr=net, tranches=tuple(tranches))
