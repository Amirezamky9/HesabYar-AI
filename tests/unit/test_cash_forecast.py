"""Unit tests for the pure cash-forecast builders (US-553).

No I/O, no engine, no database: the builders take hand-built
records and return forecast items, the monthly calendar and the
30/60/90/120-day horizon. The service tests
(``test_cash_forecast_service.py``) cover the reads + the
double-count guards; the REST tests cover the endpoints.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

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
    ForecastBucket,
    ForecastDirection,
    ForecastItem,
    ForecastSource,
)
from hesabyar.domain.cheques.models import (
    Cheque,
    ChequeKind,
    ChequePurpose,
    ChequeState,
    MonthlyMatchHistory,
)
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.domain.installments.models import InstallmentLeg
from hesabyar.domain.settlement.models import SettlementTranche, TrancheState

TODAY = datetime.date(2026, 11, 20)  # 1405-08-29
RANGE_FROM = datetime.date(2026, 11, 20)
RANGE_TO = datetime.date(2026, 12, 22)  # 1405-10-01

TENANT_ID = "00000000-0000-0000-0000-000000000553"
INSTALLATION_ID = "00000000-0000-0000-0000-000000000554"


def _cheque(**overrides: object) -> Cheque:
    values: dict[str, object] = {
        "id": "cheque-1",
        "tenant_id": TENANT_ID,
        "installation_id": INSTALLATION_ID,
        "company_engine_id": 2,
        "kind": ChequeKind.RECEIVED,
        "purpose": ChequePurpose.PAYMENT,
        "sayad_id": "SAYAD-1",
        "amount_irr": 10_000_000,
        "issue_date": FiscalDate(datetime.date(2026, 11, 1)),
        "due_date": FiscalDate(datetime.date(2026, 11, 25)),
        "issuer_name": "صادرکننده",
        "issuer_national_id": "0012345678",
        "issuer_bank": "بانک ملی",
        "payee_name": "گیرنده",
        "payee_national_id": "0012345679",
        "order_id": "ORDER-1",
        "customer_ref": "CUST-1",
        "sayad_registered": True,
        "state": ChequeState.HELD,
    }
    values.update(overrides)
    return Cheque(**values)  # type: ignore[arg-type]


def _tranche(**overrides: object) -> SettlementTranche:
    values: dict[str, object] = {
        "id": "tranche-1",
        "installation_id": INSTALLATION_ID,
        "gateway_payment_id": "payment-1",
        "tranche_index": 0,
        "tranche_count": 4,
        "sales_period_jalali": "1405-08",
        "expected_window_start": datetime.date(2026, 11, 20),
        "expected_date": datetime.date(2026, 11, 25),
        "gross_irr": 10_000_000,
        "fee_irr": 500_000,
        "fee_vat_irr": 50_000,
        "net_irr": 9_450_000,
        "state": TrancheState.EXPECTED,
    }
    values.update(overrides)
    return SettlementTranche(**values)  # type: ignore[arg-type]


def _leg(**overrides: object) -> InstallmentLeg:
    values: dict[str, object] = {
        "id": "leg-1",
        "plan_id": "plan-1",
        "sequence": 1,
        "due_date": FiscalDate(datetime.date(2026, 11, 25)),
        "principal": MoneyIRR(10_000_000),
    }
    values.update(overrides)
    return InstallmentLeg(**values)  # type: ignore[arg-type]


def _item(**overrides: object) -> ForecastItem:
    values: dict[str, object] = {
        "date": FiscalDate(datetime.date(2026, 11, 25)),
        "direction": ForecastDirection.INFLOW,
        "bucket": ForecastBucket.CONFIRMED,
        "source": ForecastSource.SETTLEMENT_TRANCHE,
        "amount_irr": 1_000_000,
        "ref": "tranche:t-1",
    }
    values.update(overrides)
    return ForecastItem(**values)  # type: ignore[arg-type]


# -- Jalali month keys ----------------------------------------------------------


def test_jalali_month_key() -> None:
    assert jalali_month_key(datetime.date(2026, 11, 20)) == "1405-08"
    assert jalali_month_key(datetime.date(2026, 11, 22)) == "1405-09"
    assert jalali_month_key(datetime.date(2026, 12, 22)) == "1405-10"
    assert jalali_month_key(datetime.date(2027, 3, 21)) == "1406-01"


# -- cheque items (report 14 rule) ----------------------------------------------


def test_cheque_items_follow_report_14_states() -> None:
    received_held = _cheque(id="c-held", state=ChequeState.HELD)
    received_clearing = _cheque(
        id="c-clearing",
        sayad_id="SAYAD-2",
        state=ChequeState.IN_CLEARING,
        amount_irr=5_000_000,
    )
    issued = _cheque(
        id="c-issued",
        sayad_id="SAYAD-3",
        kind=ChequeKind.ISSUED,
        state=ChequeState.ISSUED,
        amount_irr=7_000_000,
    )
    items = cheque_forecast_items(
        [received_held, received_clearing, issued],
        date_from=RANGE_FROM,
        date_to=RANGE_TO,
    )
    assert len(items) == 3
    inflows = [i for i in items if i.direction is ForecastDirection.INFLOW]
    outflows = [i for i in items if i.direction is ForecastDirection.OUTFLOW]
    assert sum(i.amount_irr for i in inflows) == 15_000_000
    assert sum(i.amount_irr for i in outflows) == 7_000_000
    assert all(i.bucket is ForecastBucket.CONFIRMED for i in items)
    assert {i.source for i in inflows} == {ForecastSource.CHEQUE_RECEIVED}
    assert {i.source for i in outflows} == {ForecastSource.CHEQUE_ISSUED}


def test_cheque_items_exclude_non_forecast_states() -> None:
    # registered is a claim, not money; cleared/bounced already resolved.
    registered = _cheque(id="c-reg", state=ChequeState.REGISTERED)
    cleared = _cheque(id="c-cleared", sayad_id="SAYAD-2", state=ChequeState.CLEARED)
    bounced = _cheque(id="c-bounced", sayad_id="SAYAD-3", state=ChequeState.BOUNCED)
    voided_issued = _cheque(
        id="c-voided",
        sayad_id="SAYAD-4",
        kind=ChequeKind.ISSUED,
        state=ChequeState.VOIDED,
    )
    items = cheque_forecast_items(
        [registered, cleared, bounced, voided_issued],
        date_from=RANGE_FROM,
        date_to=RANGE_TO,
    )
    assert items == []


def test_cheque_items_exclude_out_of_range() -> None:
    before = _cheque(
        id="c-before", due_date=FiscalDate(datetime.date(2026, 11, 19))
    )
    after = _cheque(
        id="c-after",
        sayad_id="SAYAD-2",
        due_date=FiscalDate(datetime.date(2026, 12, 23)),
    )
    items = cheque_forecast_items(
        [before, after], date_from=RANGE_FROM, date_to=RANGE_TO
    )
    assert items == []


def test_cheque_items_collateral_never_included() -> None:
    """Collateral cheques appear in NEITHER column, ever (§7.5).

    The builder re-checks the purpose even if the source filter let
    a collateral row through — a collateral cheque is dropped here,
    not trusted.
    """
    collateral_received = _cheque(
        id="c-coll-r",
        purpose=ChequePurpose.COLLATERAL,
        state=ChequeState.HELD,
        amount_irr=100_000_000,
    )
    collateral_issued = _cheque(
        id="c-coll-i",
        sayad_id="SAYAD-2",
        kind=ChequeKind.ISSUED,
        purpose=ChequePurpose.COLLATERAL,
        state=ChequeState.ISSUED,
        amount_irr=200_000_000,
    )
    items = cheque_forecast_items(
        [collateral_received, collateral_issued],
        date_from=RANGE_FROM,
        date_to=RANGE_TO,
    )
    assert items == []
    # And mixed with a commercial cheque, only the commercial one counts.
    commercial = _cheque(id="c-com", sayad_id="SAYAD-3")
    items = cheque_forecast_items(
        [collateral_received, collateral_issued, commercial],
        date_from=RANGE_FROM,
        date_to=RANGE_TO,
    )
    assert len(items) == 1
    assert items[0].ref == "cheque:SAYAD-3"


# -- tranche items ---------------------------------------------------------------


def test_tranche_items_only_open_states() -> None:
    open_states = [
        TrancheState.EXPECTED,
        TrancheState.DUE,
        TrancheState.OVERDUE,
        TrancheState.CONFIRMED,
        TrancheState.PARTIALLY_CONFIRMED,
        TrancheState.PARTIALLY_RECEIVED,
    ]
    closed_states = [
        TrancheState.RECEIVED,
        TrancheState.SETTLED,
        TrancheState.CANCELLED,
        TrancheState.EXCEPTION,
        TrancheState.WRITTEN_OFF,
    ]
    tranches = [
        _tranche(id=f"t-open-{i}", state=state) for i, state in enumerate(open_states)
    ] + [
        _tranche(id=f"t-closed-{i}", state=state) for i, state in enumerate(closed_states)
    ]
    items = tranche_forecast_items(tranches, date_from=RANGE_FROM, date_to=RANGE_TO)
    assert len(items) == len(open_states)
    assert all(i.direction is ForecastDirection.INFLOW for i in items)
    assert all(i.bucket is ForecastBucket.CONFIRMED for i in items)


def test_tranche_items_count_outstanding_only() -> None:
    partial = _tranche(
        id="t-partial",
        state=TrancheState.PARTIALLY_RECEIVED,
        net_irr=9_450_000,
        received_net_irr=4_000_000,
    )
    items = tranche_forecast_items([partial], date_from=RANGE_FROM, date_to=RANGE_TO)
    assert len(items) == 1
    assert items[0].amount_irr == 5_450_000


def test_tranche_items_skip_fully_received() -> None:
    received = _tranche(
        id="t-full",
        state=TrancheState.PARTIALLY_RECEIVED,
        net_irr=9_450_000,
        received_net_irr=9_450_000,
    )
    assert tranche_forecast_items([received], date_from=RANGE_FROM, date_to=RANGE_TO) == []


# -- leg items --------------------------------------------------------------------


def test_leg_items_use_uncovered_balance_only() -> None:
    leg = _leg()
    items = leg_forecast_items([(leg, 6_000_000)], date_from=RANGE_FROM, date_to=RANGE_TO)
    assert len(items) == 1
    assert items[0].amount_irr == 6_000_000
    assert items[0].source is ForecastSource.INSTALLMENT_LEG
    # Fully covered (received + pending) legs promise nothing.
    assert leg_forecast_items([(leg, 0)], date_from=RANGE_FROM, date_to=RANGE_TO) == []
    assert leg_forecast_items([(leg, -5)], date_from=RANGE_FROM, date_to=RANGE_TO) == []


def test_leg_items_exclude_out_of_range() -> None:
    leg = _leg(due_date=FiscalDate(datetime.date(2027, 1, 15)))
    assert (
        leg_forecast_items([(leg, 6_000_000)], date_from=RANGE_FROM, date_to=RANGE_TO)
        == []
    )


# -- invoice items -----------------------------------------------------------------


def test_invoice_items_direction_by_type() -> None:
    rows = [
        ("101", "out", 8_000_000, datetime.date(2026, 11, 26), "مشتری الف"),
        ("102", "in", 3_000_000, datetime.date(2026, 11, 27), "تأمین‌کننده ب"),
    ]
    items, skipped = invoice_forecast_items(rows, date_from=RANGE_FROM, date_to=RANGE_TO)
    assert skipped == 0
    assert len(items) == 2
    inflow = next(i for i in items if i.direction is ForecastDirection.INFLOW)
    outflow = next(i for i in items if i.direction is ForecastDirection.OUTFLOW)
    assert inflow.amount_irr == 8_000_000
    assert inflow.source is ForecastSource.INVOICE_RECEIVABLE
    assert outflow.amount_irr == 3_000_000
    assert outflow.source is ForecastSource.INVOICE_PAYABLE


def test_invoice_items_count_undated_instead_of_dropping_silently() -> None:
    rows = [
        ("101", "out", 8_000_000, None, "مشتری الف"),
        ("102", "out", 0, datetime.date(2026, 11, 26), "مشتری ب"),
    ]
    items, skipped = invoice_forecast_items(rows, date_from=RANGE_FROM, date_to=RANGE_TO)
    assert items == []
    assert skipped == 1  # only the undated one counts; the zero one is just paid


# -- purchase request items ----------------------------------------------------------


def test_purchase_request_items() -> None:
    rows = [
        ("201", "کارتن", 2_500_000, datetime.date(2026, 11, 28)),
        ("202", "چسب", 0, datetime.date(2026, 11, 28)),
        ("203", "نایلون", 1_000_000, None),
        ("204", "برچسب", 1_000_000, datetime.date(2027, 2, 1)),
    ]
    items = purchase_request_items(rows, date_from=RANGE_FROM, date_to=RANGE_TO)
    assert len(items) == 1
    assert items[0].amount_irr == 2_500_000
    assert items[0].direction is ForecastDirection.OUTFLOW
    assert items[0].source is ForecastSource.PACKAGING_PURCHASE
    assert items[0].ref == "purchase_request:201"


# -- calendar -------------------------------------------------------------------------


def test_build_calendar_groups_by_jalali_month_and_day() -> None:
    items = [
        _item(date=FiscalDate(datetime.date(2026, 11, 20)), amount_irr=1_000_000),
        _item(
            date=FiscalDate(datetime.date(2026, 11, 22)),
            amount_irr=2_000_000,
            ref="tranche:t-2",
        ),
        _item(
            date=FiscalDate(datetime.date(2026, 12, 22)),
            direction=ForecastDirection.OUTFLOW,
            bucket=ForecastBucket.FORECAST,
            source=ForecastSource.EXPECTED_SALE,
            amount_irr=3_000_000,
            ref="expected:sale-1",
        ),
    ]
    months = build_calendar(items, date_from=RANGE_FROM, date_to=RANGE_TO)
    assert [m.month_jalali for m in months] == ["1405-08", "1405-09", "1405-10"]
    aban = months[0]
    assert len(aban.days) == 2  # 1405-08-29, 1405-08-30
    assert aban.days[0].inflow_confirmed_irr == 1_000_000
    assert aban.days[0].item_count == 1
    assert aban.days[1].inflow_confirmed_irr == 0  # an empty day reads zero
    azar = months[1]
    assert len(azar.days) == 30
    assert azar.days[0].inflow_confirmed_irr == 2_000_000
    dey = months[2]
    assert len(dey.days) == 1
    assert dey.days[0].outflow_forecast_irr == 3_000_000
    assert dey.days[0].outflow_confirmed_irr == 0


def test_build_calendar_empty_range_reads_zero() -> None:
    months = build_calendar([], date_from=RANGE_FROM, date_to=RANGE_TO)
    assert sum(m.inflow_confirmed_irr for m in months) == 0
    assert all(day.item_count == 0 for m in months for day in m.days)


# -- horizon -----------------------------------------------------------------------------


def test_build_horizon_bucket_boundaries() -> None:
    def item_on(offset: int) -> ForecastItem:
        return _item(
            date=FiscalDate(TODAY + datetime.timedelta(days=offset)),
            ref=f"tranche:t-{offset}",
        )

    items = [item_on(offset) for offset in (-1, 0, 30, 31, 60, 61, 90, 91, 120, 121, 400)]
    buckets = build_horizon(items, today=TODAY)
    by_key = {b.key: b for b in buckets}
    assert by_key["overdue"].item_count == 1
    assert by_key["0_30"].item_count == 2  # day 0 and day 30
    assert by_key["31_60"].item_count == 2  # day 31 and day 60
    assert by_key["61_90"].item_count == 2
    assert by_key["91_120"].item_count == 2
    assert by_key["beyond_120"].item_count == 2
    assert sum(b.item_count for b in buckets) == len(items)


def test_build_horizon_keeps_buckets_separate() -> None:
    items = [
        _item(amount_irr=1_000_000),
        _item(
            bucket=ForecastBucket.FORECAST,
            source=ForecastSource.EXPECTED_SALE,
            amount_irr=2_000_000,
            ref="expected:sale-1",
        ),
        _item(
            direction=ForecastDirection.OUTFLOW,
            source=ForecastSource.CHEQUE_ISSUED,
            amount_irr=500_000,
            ref="cheque:S-1",
        ),
    ]
    buckets = build_horizon(items, today=TODAY)
    bucket_0_30 = next(b for b in buckets if b.key == "0_30")
    assert bucket_0_30.inflow_confirmed_irr == 1_000_000
    assert bucket_0_30.inflow_forecast_irr == 2_000_000
    assert bucket_0_30.outflow_confirmed_irr == 500_000
    assert bucket_0_30.net_confirmed_irr == 500_000
    assert bucket_0_30.net_forecast_irr == 2_000_000


# -- aggregation ---------------------------------------------------------------------------


def test_aggregate_items_keeps_buckets_separate() -> None:
    items = [
        _item(amount_irr=1_000_000),
        _item(
            bucket=ForecastBucket.FORECAST,
            source=ForecastSource.EXPECTED_SALE,
            amount_irr=2_000_000,
            ref="expected:sale-1",
        ),
        _item(
            direction=ForecastDirection.OUTFLOW,
            source=ForecastSource.CHEQUE_ISSUED,
            amount_irr=500_000,
            ref="cheque:S-1",
        ),
        _item(
            direction=ForecastDirection.OUTFLOW,
            bucket=ForecastBucket.FORECAST,
            source=ForecastSource.EXPECTED_SALE,
            amount_irr=700_000,
            ref="expected:sale-2",
        ),
    ]
    totals = aggregate_items(items)
    assert totals.inflow_confirmed_irr == 1_000_000
    assert totals.inflow_forecast_irr == 2_000_000
    assert totals.outflow_confirmed_irr == 500_000
    assert totals.outflow_forecast_irr == 700_000
    assert totals.net_confirmed_irr == 500_000
    assert totals.net_forecast_irr == 1_300_000
    assert totals.item_count == 4


# -- match rate + trend -----------------------------------------------------------------------


def test_match_rate() -> None:
    assert match_rate(0, 0, 0, 0) == Decimal("0")
    assert match_rate(3, 1, 1, 1) == Decimal("0.6667")
    assert match_rate(5, 0, 0, 0) == Decimal("1.0000")
    assert match_rate(0, 0, 2, 3) == Decimal("0.0000")


def test_build_monthly_match_trend_from_stored_rows() -> None:
    history = [
        MonthlyMatchHistory(
            id="h-1",
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            month_jalali="1405-07",
            exact_count=8,
            probable_count=2,
            ambiguous_count=1,
            unmatched_count=1,
        ),
        MonthlyMatchHistory(
            id="h-2",
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            month_jalali="1405-08",
            exact_count=5,
            probable_count=0,
            ambiguous_count=0,
            unmatched_count=5,
        ),
    ]
    trend = build_monthly_match_trend(history)
    assert [m.month_jalali for m in trend.months] == ["1405-07", "1405-08"]
    assert trend.months[0].match_rate == "0.8333"
    assert trend.months[0].total_lines == 12
    assert trend.months[1].match_rate == "0.5000"
    assert trend.total_exact == 13
    assert trend.total_probable == 2
    assert trend.total_ambiguous == 1
    assert trend.total_unmatched == 6
    assert trend.total_lines == 22
    assert trend.overall_match_rate == "0.6818"


def test_build_monthly_match_trend_empty_history_is_empty() -> None:
    trend = build_monthly_match_trend([])
    assert trend.months == ()
    assert trend.total_lines == 0
    assert trend.overall_match_rate == "0"
