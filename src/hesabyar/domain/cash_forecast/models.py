"""Cash-forecast domain models (Phase 5-6, US-553).

A forecast is a view over registered records only — no record, no
figure. Every item carries its ``bucket`` (قطعی / پیش‌بینی) and
its ``source`` so the two columns and the per-source breakdown can
never mix, and every amount is integer Rial (``MoneyIRR``
digits=0; no floats anywhere in financial paths).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from enum import StrEnum

from hesabyar.domain.accounting.value_objects import ReportFreshness
from hesabyar.domain.common.dates import FiscalDate


class ForecastBucket(StrEnum):
    """قطعی vs پیش‌بینی — the two columns of every forecast view."""

    #: A registered record with a due date and an amount (contract /
    #: cheque / installment / invoice / purchase request).
    CONFIRMED = "confirmed"
    #: Expected sales with no registered record yet. No source exists
    #: for this bucket today — it is always empty, honestly so.
    FORECAST = "forecast"


class ForecastDirection(StrEnum):
    """Money in vs money out."""

    INFLOW = "inflow"
    OUTFLOW = "outflow"


class ForecastSource(StrEnum):
    """The registered record a forecast item comes from."""

    #: A gateway settlement tranche (US-521) — inflow.
    SETTLEMENT_TRANCHE = "settlement_tranche"
    #: An unpaid internal installment leg (US-540) — inflow.
    INSTALLMENT_LEG = "installment_leg"
    #: A due received commercial cheque (report 14) — inflow.
    CHEQUE_RECEIVED = "cheque_received"
    #: A due issued commercial cheque (report 14) — outflow.
    CHEQUE_ISSUED = "cheque_issued"
    #: A posted customer invoice with an unpaid balance — inflow.
    INVOICE_RECEIVABLE = "invoice_receivable"
    #: A posted supplier invoice with an unpaid balance — outflow.
    INVOICE_PAYABLE = "invoice_payable"
    #: An open packaging purchase request (supply wizard) — outflow.
    PACKAGING_PURCHASE = "packaging_purchase"
    #: Expected sales (no registered record) — the پیش‌بینی bucket.
    EXPECTED_SALE = "expected_sale"


@dataclass(frozen=True, slots=True)
class ForecastItem:
    """One expected cash movement on one date.

    ``ref`` identifies the source record (``"tranche:<id>"``,
    ``"leg:<id>"``, ``"cheque:<sayad_id>"``, ``"invoice:<id>"``,
    ``"purchase_request:<id>"``) so the calendar can link back to
    it; ``label`` is the human-readable caption (party / channel /
    product name as read from the source — never invented).
    """

    date: FiscalDate
    direction: ForecastDirection
    bucket: ForecastBucket
    source: ForecastSource
    amount_irr: int
    ref: str
    label: str = ""


@dataclass(frozen=True, slots=True)
class CalendarDay:
    """One calendar day's forecast totals (قطعی and پیش‌بینی split)."""

    date: FiscalDate
    inflow_confirmed_irr: int = 0
    outflow_confirmed_irr: int = 0
    inflow_forecast_irr: int = 0
    outflow_forecast_irr: int = 0
    item_count: int = 0

    @property
    def net_confirmed_irr(self) -> int:
        return self.inflow_confirmed_irr - self.outflow_confirmed_irr

    @property
    def net_forecast_irr(self) -> int:
        return self.inflow_forecast_irr - self.outflow_forecast_irr


@dataclass(frozen=True, slots=True)
class CalendarMonth:
    """One Jalali month of the forecast calendar.

    ``month_jalali`` is the ``"YYYY-MM"`` Jalali key; ``days``
    covers every day of the month that falls inside the requested
    range (a partial month at the range edges is honest — the days
    outside the range simply are not there).
    """

    month_jalali: str
    days: tuple[CalendarDay, ...] = ()

    @property
    def inflow_confirmed_irr(self) -> int:
        return sum(day.inflow_confirmed_irr for day in self.days)

    @property
    def outflow_confirmed_irr(self) -> int:
        return sum(day.outflow_confirmed_irr for day in self.days)

    @property
    def inflow_forecast_irr(self) -> int:
        return sum(day.inflow_forecast_irr for day in self.days)

    @property
    def outflow_forecast_irr(self) -> int:
        return sum(day.outflow_forecast_irr for day in self.days)


@dataclass(frozen=True, slots=True)
class HorizonBucket:
    """One 30/60/90/120-day horizon bucket (§11.2 settlement report).

    ``key`` is one of ``"overdue"``, ``"0_30"``, ``"31_60"``,
    ``"61_90"``, ``"91_120"``, ``"beyond_120"``; ``from_days`` /
    ``to_days`` are the inclusive day offsets from the as-of date
    (``overdue`` is everything before the as-of date, ``beyond_120``
    everything past 120 days).
    """

    key: str
    from_days: int | None
    to_days: int | None
    inflow_confirmed_irr: int = 0
    outflow_confirmed_irr: int = 0
    inflow_forecast_irr: int = 0
    outflow_forecast_irr: int = 0
    item_count: int = 0

    @property
    def net_confirmed_irr(self) -> int:
        return self.inflow_confirmed_irr - self.outflow_confirmed_irr

    @property
    def net_forecast_irr(self) -> int:
        return self.inflow_forecast_irr - self.outflow_forecast_irr


@dataclass(frozen=True, slots=True)
class CashForecastTotals:
    """Whole-range totals, قطعی and پیش‌بینی kept apart."""

    inflow_confirmed_irr: int = 0
    outflow_confirmed_irr: int = 0
    inflow_forecast_irr: int = 0
    outflow_forecast_irr: int = 0
    item_count: int = 0

    @property
    def net_confirmed_irr(self) -> int:
        return self.inflow_confirmed_irr - self.outflow_confirmed_irr

    @property
    def net_forecast_irr(self) -> int:
        return self.inflow_forecast_irr - self.outflow_forecast_irr


@dataclass(frozen=True, slots=True)
class CashForecastView:
    """The cash forecast for one company over one date range.

    ``items`` are the dated movements (the calendar's raw material);
    ``months`` is the same data grouped by Jalali month/day;
    ``horizon`` is the 30/60/90/120-day view from the as-of date.
    ``source_counts`` counts items per source so the UI can show an
    honest empty state per source (a source with no records reads
    zero — never an invented figure). ``skipped_undated`` counts
    posted invoices with an unpaid balance that could not be dated
    at all (no maturity date, no invoice date) — dropped from the
    calendar, counted here instead of silently lost.
    """

    company_id: str
    date_from: FiscalDate
    date_to: FiscalDate
    as_of: FiscalDate
    items: tuple[ForecastItem, ...] = ()
    months: tuple[CalendarMonth, ...] = ()
    horizon: tuple[HorizonBucket, ...] = ()
    totals: CashForecastTotals = field(default_factory=CashForecastTotals)
    source_counts: dict[str, int] = field(default_factory=dict)
    skipped_undated: int = 0
    freshness: ReportFreshness | None = None


@dataclass(frozen=True, slots=True)
class MonthlyMatchTrendMonth:
    """One stored month of clearing-match history (report 12).

    The counts come from the ``cheque_match_history`` row — the
    trend renders FROM stored history, never recomputed from raw
    match records at read time. ``match_rate`` is the share of
    lines the matcher decided (exact + probable) out of all lines
    it saw, as an exact decimal fraction (0–1).
    """

    month_jalali: str
    exact_count: int
    probable_count: int
    ambiguous_count: int
    unmatched_count: int
    total_lines: int
    match_rate: str  # Decimal rendered as text (e.g. "0.75")

    @property
    def decided_count(self) -> int:
        return self.exact_count + self.probable_count


@dataclass(frozen=True, slots=True)
class MonthlyMatchTrend:
    """Cheque report 12: the monthly match-trend from stored history.

    ``months`` are oldest first; ``totals`` aggregate every stored
    month (the donut's input). An empty history is an honest empty
    trend — months with no matching run carry no row at all, and
    the UI says history starts with the first run after deploy.
    """

    months: tuple[MonthlyMatchTrendMonth, ...] = ()
    total_exact: int = 0
    total_probable: int = 0
    total_ambiguous: int = 0
    total_unmatched: int = 0
    total_lines: int = 0
    overall_match_rate: str = "0"
    freshness: ReportFreshness | None = None
    generated_at: datetime.datetime | None = None
