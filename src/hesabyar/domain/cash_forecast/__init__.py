"""Cash forecast domain (Phase 5-6, US-553).

The forecast is a §11.2 report built ONLY from registered records:
settlement tranches (US-521), installment legs (US-540), cheques
(report 14 rule, US-535), posted invoices with an unpaid balance,
and open packaging purchase requests. Nothing is invented: where a
source has no records the forecast shows an honest empty bucket.

Two buckets, strictly separated in every aggregation:

- ``confirmed`` (قطعی) — a real record with a due date and an
  amount (a registered contract / cheque / installment / invoice /
  purchase request);
- ``forecast`` (پیش‌بینی) — expected sales with no registered
  record yet. NO expected-sales source exists yet, so the bucket
  is always empty — carried in the model, the API and the UI so a
  future sales-forecast source plugs in without a schema change.

Collateral cheques appear in NEITHER bucket, ever (§7.5): the
cheque read filters ``purpose=payment`` at the source and the
builder re-checks the purpose on every row.
"""

from hesabyar.domain.cash_forecast.forecast import (
    aggregate_items,
    build_calendar,
    build_horizon,
    build_monthly_match_trend,
    cheque_forecast_items,
    invoice_forecast_items,
    jalali_month_key,
    leg_forecast_items,
    match_rate,
    purchase_request_items,
    tranche_forecast_items,
)
from hesabyar.domain.cash_forecast.models import (
    CalendarDay,
    CalendarMonth,
    CashForecastTotals,
    CashForecastView,
    ForecastBucket,
    ForecastDirection,
    ForecastItem,
    ForecastSource,
    HorizonBucket,
    MonthlyMatchTrend,
    MonthlyMatchTrendMonth,
)

__all__ = [
    "CalendarDay",
    "CalendarMonth",
    "CashForecastTotals",
    "CashForecastView",
    "ForecastBucket",
    "ForecastDirection",
    "ForecastItem",
    "ForecastSource",
    "HorizonBucket",
    "MonthlyMatchTrend",
    "MonthlyMatchTrendMonth",
    "aggregate_items",
    "build_calendar",
    "build_horizon",
    "build_monthly_match_trend",
    "cheque_forecast_items",
    "invoice_forecast_items",
    "jalali_month_key",
    "leg_forecast_items",
    "match_rate",
    "purchase_request_items",
    "tranche_forecast_items",
]
