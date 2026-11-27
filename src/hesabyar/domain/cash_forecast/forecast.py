"""Pure cash-forecast builders (Phase 5-6, US-553).

No I/O, no engine calls, no database: the query service loads the
source records and these functions turn them into forecast items,
the monthly calendar and the 30/60/90/120-day horizon. Unit tests
exercise the builders directly with hand-built records.

Double-count guards (the forecast must never count the same money
twice — see docs/cash-forecast.md):

- a BNPL installment leg is NEVER an item: the gateway pays it in
  settlement tranches, and the tranche is the cash;
- a leg covered by a pending-clearance cheque contributes only its
  UNCOVERED remainder: the pending slice is expected through the
  cheque channel (report 14), not the leg channel;
- an invoice with an installment plan is excluded from the
  due-invoice source (the service owns that filter — it needs the
  plan registry, which these pure functions never see).

Collateral cheques appear in NEITHER bucket, ever (§7.5): the
cheque read filters ``purpose=payment`` at the source, and
:func:`cheque_forecast_items` re-checks the purpose on every row —
a row that slipped through the filter is dropped here, not trusted.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from decimal import Decimal

from hesabyar.domain.cash_forecast.models import (
    CalendarDay,
    CalendarMonth,
    CashForecastTotals,
    ForecastBucket,
    ForecastDirection,
    ForecastItem,
    ForecastSource,
    HorizonBucket,
    MonthlyMatchTrend,
    MonthlyMatchTrendMonth,
)
from hesabyar.domain.cheques.models import (
    Cheque,
    ChequeKind,
    ChequePurpose,
    ChequeState,
    MonthlyMatchHistory,
)
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.installments.models import InstallmentLeg
from hesabyar.domain.settlement.models import SettlementTranche, TrancheState

#: Tranche states that still promise future cash. ``settled`` /
#: ``received`` already arrived (not a forecast), ``cancelled`` /
#: ``exception`` / ``written_off`` promise nothing.
OPEN_TRANCHE_STATES: frozenset[TrancheState] = frozenset(
    {
        TrancheState.EXPECTED,
        TrancheState.DUE,
        TrancheState.OVERDUE,
        TrancheState.CONFIRMED,
        TrancheState.PARTIALLY_CONFIRMED,
        TrancheState.PARTIALLY_RECEIVED,
    }
)

#: Received-cheque states that promise a collection (report 14).
#: ``registered`` is a claim, not money — the leaf has not arrived.
RECEIVED_CHEQUE_FORECAST_STATES: frozenset[ChequeState] = frozenset(
    {ChequeState.HELD, ChequeState.IN_CLEARING}
)

#: Issued-cheque state that promises a clearing (report 14).
ISSUED_CHEQUE_FORECAST_STATES: frozenset[ChequeState] = frozenset(
    {ChequeState.ISSUED}
)

#: Horizon buckets: (key, from_days, to_days). ``overdue`` is
#: everything before the as-of date; ``beyond_120`` everything
#: past 120 days. Day 0 (due today) belongs to ``0_30``.
HORIZON_BUCKETS: tuple[tuple[str, int | None, int | None], ...] = (
    ("overdue", None, -1),
    ("0_30", 0, 30),
    ("31_60", 31, 60),
    ("61_90", 61, 90),
    ("91_120", 91, 120),
    ("beyond_120", 121, None),
)


def jalali_month_key(day: datetime.date | FiscalDate) -> str:
    """The Jalali ``"YYYY-MM"`` key of a date (the calendar's month key)."""
    fiscal = FiscalDate(day)
    jalali = fiscal.jalali_date
    return f"{jalali.year:04d}-{jalali.month:02d}"


def match_rate(
    exact: int, probable: int, ambiguous: int, unmatched: int
) -> Decimal:
    """Share of matched lines the matcher decided (exact + probable).

    An exact decimal fraction (0–1), 4 places; a month with no
    lines rates 0 — never a division error, never a guess.
    """
    total = exact + probable + ambiguous + unmatched
    if total == 0:
        return Decimal("0")
    return (Decimal(exact + probable) / Decimal(total)).quantize(Decimal("0.0001"))


def _in_range(
    day: datetime.date, date_from: datetime.date, date_to: datetime.date
) -> bool:
    return date_from <= day <= date_to


def tranche_forecast_items(
    tranches: Sequence[SettlementTranche],
    *,
    date_from: datetime.date,
    date_to: datetime.date,
) -> list[ForecastItem]:
    """Expected inflows from open settlement tranches (قطعی).

    Only the OUTSTANDING net (``net_irr - received_net_irr``) is
    expected — a partially received tranche promises only its
    remainder. Settled / received / cancelled / exception /
    written-off tranches promise nothing.
    """
    items: list[ForecastItem] = []
    for tranche in tranches:
        if tranche.state not in OPEN_TRANCHE_STATES:
            continue
        outstanding = tranche.outstanding_net_irr
        if outstanding <= 0:
            continue
        if not _in_range(tranche.expected_date, date_from, date_to):
            continue
        items.append(
            ForecastItem(
                date=FiscalDate(tranche.expected_date),
                direction=ForecastDirection.INFLOW,
                bucket=ForecastBucket.CONFIRMED,
                source=ForecastSource.SETTLEMENT_TRANCHE,
                amount_irr=outstanding,
                ref=f"tranche:{tranche.id}",
                label=(
                    f"تسویهٔ {tranche.sales_period_jalali} — "
                    f"قسط {tranche.tranche_index + 1} از {tranche.tranche_count}"
                ),
            )
        )
    return items


def leg_forecast_items(
    legs: Sequence[tuple[InstallmentLeg, int]],
    *,
    date_from: datetime.date,
    date_to: datetime.date,
) -> list[ForecastItem]:
    """Expected inflows from unpaid installment legs (قطعی).

    Each leg arrives with its UNCOVERED balance (principal −
    received − pending clearance — the caller computes it from the
    receipts + cheque states, so a cheque-backed leg is never
    counted twice: the pending slice comes through the cheque
    channel). BNPL legs never reach this function — the caller
    filters them (the gateway's tranches are the cash).
    """
    items: list[ForecastItem] = []
    for leg, uncovered_irr in legs:
        if uncovered_irr <= 0:
            continue
        due = leg.due_date.gregorian_date
        if not _in_range(due, date_from, date_to):
            continue
        items.append(
            ForecastItem(
                date=leg.due_date,
                direction=ForecastDirection.INFLOW,
                bucket=ForecastBucket.CONFIRMED,
                source=ForecastSource.INSTALLMENT_LEG,
                amount_irr=uncovered_irr,
                ref=f"leg:{leg.id}",
                label=f"قسط {leg.sequence}",
            )
        )
    return items


def cheque_forecast_items(
    cheques: Sequence[Cheque],
    *,
    date_from: datetime.date,
    date_to: datetime.date,
) -> list[ForecastItem]:
    """Expected cheque collections / clearings (report 14 rule).

    The backend mirror of the report 14 rule in
    ``apps/admin/features/cheques/cheque-data.ts`` (``cashForecast``):
    a received commercial cheque held or in clearing is an expected
    inflow at its due date; an issued commercial cheque still
    outstanding is an expected outflow at its due date. Collateral
    cheques (``purpose != payment``) are dropped here even if the
    source filter let them through — they appear in NEITHER column,
    ever.
    """
    items: list[ForecastItem] = []
    for cheque in cheques:
        if cheque.purpose != ChequePurpose.PAYMENT:
            continue
        due = cheque.due_date.gregorian_date
        if not _in_range(due, date_from, date_to):
            continue
        if (
            cheque.kind == ChequeKind.RECEIVED
            and cheque.state in RECEIVED_CHEQUE_FORECAST_STATES
        ):
            items.append(
                ForecastItem(
                    date=cheque.due_date,
                    direction=ForecastDirection.INFLOW,
                    bucket=ForecastBucket.CONFIRMED,
                    source=ForecastSource.CHEQUE_RECEIVED,
                    amount_irr=cheque.amount_irr,
                    ref=f"cheque:{cheque.sayad_id}",
                    label=f"چک دریافتی {cheque.sayad_id}",
                )
            )
        elif (
            cheque.kind == ChequeKind.ISSUED
            and cheque.state in ISSUED_CHEQUE_FORECAST_STATES
        ):
            items.append(
                ForecastItem(
                    date=cheque.due_date,
                    direction=ForecastDirection.OUTFLOW,
                    bucket=ForecastBucket.CONFIRMED,
                    source=ForecastSource.CHEQUE_ISSUED,
                    amount_irr=cheque.amount_irr,
                    ref=f"cheque:{cheque.sayad_id}",
                    label=f"چک صادره {cheque.sayad_id}",
                )
            )
    return items


def invoice_forecast_items(
    invoices: Sequence[tuple[str, str, int, datetime.date | None, str]],
    *,
    date_from: datetime.date,
    date_to: datetime.date,
) -> tuple[list[ForecastItem], int]:
    """Expected invoice collections / payments (قطعی).

    Each invoice arrives as ``(invoice_id, invoice_type,
    amount_to_pay_irr, due_date, party_name)`` — the caller resolves
    the due date (maturity date, falling back to the invoice date)
    and excludes invoices with an installment plan. ``invoice_type``
    is ``"out"`` (customer → inflow) or ``"in"`` (supplier →
    outflow). An invoice with no due date at all cannot be placed
    on the calendar: it is dropped and counted in the returned
    ``skipped_undated`` — never silently lost, never invented.
    """
    items: list[ForecastItem] = []
    skipped_undated = 0
    for invoice_id, invoice_type, amount_irr, due_date, party_name in invoices:
        if amount_irr <= 0:
            continue
        if due_date is None:
            skipped_undated += 1
            continue
        if not _in_range(due_date, date_from, date_to):
            continue
        if invoice_type == "out":
            items.append(
                ForecastItem(
                    date=FiscalDate(due_date),
                    direction=ForecastDirection.INFLOW,
                    bucket=ForecastBucket.CONFIRMED,
                    source=ForecastSource.INVOICE_RECEIVABLE,
                    amount_irr=amount_irr,
                    ref=f"invoice:{invoice_id}",
                    label=party_name,
                )
            )
        elif invoice_type == "in":
            items.append(
                ForecastItem(
                    date=FiscalDate(due_date),
                    direction=ForecastDirection.OUTFLOW,
                    bucket=ForecastBucket.CONFIRMED,
                    source=ForecastSource.INVOICE_PAYABLE,
                    amount_irr=amount_irr,
                    ref=f"invoice:{invoice_id}",
                    label=party_name,
                )
            )
    return items, skipped_undated


def purchase_request_items(
    requests: Sequence[tuple[str, str, int, datetime.date | None]],
    *,
    date_from: datetime.date,
    date_to: datetime.date,
) -> list[ForecastItem]:
    """Expected packaging purchases (قطعی).

    Each request arrives as ``(request_id, product_name,
    amount_irr, due_date)`` — the caller prices it (quantity ×
    cost price) and dates it (supply date, else purchase date,
    else the request date). A request with no price or no date
    cannot be placed: the caller drops it before calling here.
    """
    items: list[ForecastItem] = []
    for request_id, product_name, amount_irr, due_date in requests:
        if amount_irr <= 0 or due_date is None:
            continue
        if not _in_range(due_date, date_from, date_to):
            continue
        items.append(
            ForecastItem(
                date=FiscalDate(due_date),
                direction=ForecastDirection.OUTFLOW,
                bucket=ForecastBucket.CONFIRMED,
                source=ForecastSource.PACKAGING_PURCHASE,
                amount_irr=amount_irr,
                ref=f"purchase_request:{request_id}",
                label=product_name,
            )
        )
    return items


def build_calendar(
    items: Sequence[ForecastItem],
    *,
    date_from: datetime.date,
    date_to: datetime.date,
) -> tuple[CalendarMonth, ...]:
    """Group forecast items by Jalali month and day.

    Every day in the requested range gets a row (a day with no
    items reads zero — the calendar grid needs the full month);
    months are ordered oldest first. The قطعی / پیش‌بینی split is
    kept per day, so the two columns can never mix.
    """
    by_day: dict[datetime.date, list[ForecastItem]] = {}
    for item in items:
        by_day.setdefault(item.date.gregorian_date, []).append(item)
    months: dict[str, list[CalendarDay]] = {}
    day = date_from
    while day <= date_to:
        day_items = by_day.get(day, [])
        calendar_day = CalendarDay(
            date=FiscalDate(day),
            inflow_confirmed_irr=sum(
                item.amount_irr
                for item in day_items
                if item.direction == ForecastDirection.INFLOW
                and item.bucket == ForecastBucket.CONFIRMED
            ),
            outflow_confirmed_irr=sum(
                item.amount_irr
                for item in day_items
                if item.direction == ForecastDirection.OUTFLOW
                and item.bucket == ForecastBucket.CONFIRMED
            ),
            inflow_forecast_irr=sum(
                item.amount_irr
                for item in day_items
                if item.direction == ForecastDirection.INFLOW
                and item.bucket == ForecastBucket.FORECAST
            ),
            outflow_forecast_irr=sum(
                item.amount_irr
                for item in day_items
                if item.direction == ForecastDirection.OUTFLOW
                and item.bucket == ForecastBucket.FORECAST
            ),
            item_count=len(day_items),
        )
        months.setdefault(jalali_month_key(day), []).append(calendar_day)
        day += datetime.timedelta(days=1)
    return tuple(
        CalendarMonth(month_jalali=key, days=tuple(days))
        for key, days in sorted(months.items())
    )


def build_horizon(
    items: Sequence[ForecastItem], *, today: datetime.date
) -> tuple[HorizonBucket, ...]:
    """The 30/60/90/120-day cash-flow forecast (§11.2).

    Buckets are day offsets from the as-of date: overdue (past
    due, still expected), 0–30, 31–60, 61–90, 91–120, beyond 120.
    Every item lands in exactly one bucket; the buckets always sum
    to the range totals.
    """
    grouped: dict[str, list[ForecastItem]] = {key: [] for key, _, _ in HORIZON_BUCKETS}
    for item in items:
        offset = (item.date.gregorian_date - today).days
        for key, from_days, to_days in HORIZON_BUCKETS:
            lower_ok = from_days is None or offset >= from_days
            upper_ok = to_days is None or offset <= to_days
            if lower_ok and upper_ok:
                grouped[key].append(item)
                break
    buckets: list[HorizonBucket] = []
    for key, from_days, to_days in HORIZON_BUCKETS:
        bucket_items = grouped[key]
        buckets.append(
            HorizonBucket(
                key=key,
                from_days=from_days,
                to_days=to_days,
                inflow_confirmed_irr=sum(
                    item.amount_irr
                    for item in bucket_items
                    if item.direction == ForecastDirection.INFLOW
                    and item.bucket == ForecastBucket.CONFIRMED
                ),
                outflow_confirmed_irr=sum(
                    item.amount_irr
                    for item in bucket_items
                    if item.direction == ForecastDirection.OUTFLOW
                    and item.bucket == ForecastBucket.CONFIRMED
                ),
                inflow_forecast_irr=sum(
                    item.amount_irr
                    for item in bucket_items
                    if item.direction == ForecastDirection.INFLOW
                    and item.bucket == ForecastBucket.FORECAST
                ),
                outflow_forecast_irr=sum(
                    item.amount_irr
                    for item in bucket_items
                    if item.direction == ForecastDirection.OUTFLOW
                    and item.bucket == ForecastBucket.FORECAST
                ),
                item_count=len(bucket_items),
            )
        )
    return tuple(buckets)


def aggregate_items(items: Sequence[ForecastItem]) -> CashForecastTotals:
    """Whole-range totals, قطعی and پیش‌بینی kept apart."""
    return CashForecastTotals(
        inflow_confirmed_irr=sum(
            item.amount_irr
            for item in items
            if item.direction == ForecastDirection.INFLOW
            and item.bucket == ForecastBucket.CONFIRMED
        ),
        outflow_confirmed_irr=sum(
            item.amount_irr
            for item in items
            if item.direction == ForecastDirection.OUTFLOW
            and item.bucket == ForecastBucket.CONFIRMED
        ),
        inflow_forecast_irr=sum(
            item.amount_irr
            for item in items
            if item.direction == ForecastDirection.INFLOW
            and item.bucket == ForecastBucket.FORECAST
        ),
        outflow_forecast_irr=sum(
            item.amount_irr
            for item in items
            if item.direction == ForecastDirection.OUTFLOW
            and item.bucket == ForecastBucket.FORECAST
        ),
        item_count=len(items),
    )


def build_monthly_match_trend(
    history: Sequence[MonthlyMatchHistory],
) -> MonthlyMatchTrend:
    """Cheque report 12 from STORED monthly history (never recomputed).

    The rows arrive oldest first (the repository orders them); the
    totals aggregate every stored month. Months with no matching
    run carry no row — the trend shows only months that ran, and
    the UI says so.
    """
    months = tuple(
        MonthlyMatchTrendMonth(
            month_jalali=row.month_jalali,
            exact_count=row.exact_count,
            probable_count=row.probable_count,
            ambiguous_count=row.ambiguous_count,
            unmatched_count=row.unmatched_count,
            total_lines=row.total_lines,
            match_rate=str(row.match_rate),
        )
        for row in history
    )
    total_exact = sum(row.exact_count for row in history)
    total_probable = sum(row.probable_count for row in history)
    total_ambiguous = sum(row.ambiguous_count for row in history)
    total_unmatched = sum(row.unmatched_count for row in history)
    return MonthlyMatchTrend(
        months=months,
        total_exact=total_exact,
        total_probable=total_probable,
        total_ambiguous=total_ambiguous,
        total_unmatched=total_unmatched,
        total_lines=total_exact + total_probable + total_ambiguous + total_unmatched,
        overall_match_rate=str(
            match_rate(total_exact, total_probable, total_ambiguous, total_unmatched)
        ),
    )
