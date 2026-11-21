"""REST tests for the cash-forecast + match-trend routes (US-553).

No servers needed: the app is built with dependency overrides — a
canned authenticated client, the real services over the in-memory
fakes (settlement / installments / cheques / packaging stub / fake
engine), and a stub session that resolves the company mapping. The
full-stack path (real DB + real engine) is covered by the contract
tests.
"""

from __future__ import annotations

import datetime
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hesabyar.application.queries.cash_forecast import CashForecastQueryService
from hesabyar.application.queries.cheques import ChequeQueryService
from hesabyar.domain.accounting.ports import AccountingEnginePort
from hesabyar.domain.cheques.models import MonthlyMatchHistory
from hesabyar.domain.packaging.ports import PackagingRepositoryPort
from hesabyar.infrastructure.config import AppSettings
from hesabyar.interfaces.rest.app import create_app
from hesabyar.interfaces.rest.dependencies import (
    get_authenticated_client,
    get_cash_forecast_query_service,
    get_cheque_query_service,
    get_db_session,
)
from tests.interfaces.test_rest_cheques import (
    COMPANY_ROW_ID,
    _client,
    _company_row,
    _FakeSession,
)
from tests.unit.test_cash_forecast import _cheque, _tranche
from tests.unit.test_cash_forecast_service import World as ForecastWorld
from tests.unit.test_cheque_services import INSTALLATION_ID, TENANT_ID

ALL_SCOPES = ("reports:read", "cheques:read")
FORECAST_URL = (
    f"/api/v1/reports/cash-forecast?company={COMPANY_ROW_ID}"
    "&date_from=2026-11-20&date_to=2027-03-20"
)
TREND_URL = "/api/v1/cheques/reports/monthly-match-trend"


class _World:
    def __init__(self) -> None:
        self.forecast = ForecastWorld()
        self.cheques = self.forecast.cheques
        self.queries = ChequeQueryService(self.cheques)

    def cash_forecast_service(self) -> CashForecastQueryService:
        return CashForecastQueryService(
            cast(AccountingEnginePort, self.forecast.engine),
            cast(Any, self.forecast.settlement),
            cast(Any, self.forecast.installments),
            cast(Any, self.forecast.cheques),
            cast(PackagingRepositoryPort, self.forecast.packaging),
            today=datetime.date(2026, 11, 20),
        )


@pytest.fixture()
def world() -> _World:
    return _World()


@pytest.fixture()
def api(world: _World, request: pytest.FixtureRequest) -> TestClient:
    scopes = getattr(request, "param", ALL_SCOPES)
    app = create_app(settings=AppSettings())
    app.dependency_overrides[get_authenticated_client] = lambda: _client(scopes=scopes)
    app.dependency_overrides[get_db_session] = lambda: _FakeSession(_company_row())
    app.dependency_overrides[get_cash_forecast_query_service] = (
        lambda: world.cash_forecast_service()
    )
    app.dependency_overrides[get_cheque_query_service] = lambda: world.queries
    return TestClient(app, raise_server_exceptions=False)


# -- cash forecast ---------------------------------------------------------------------


def test_cash_forecast_empty_is_honest_empty(api: TestClient) -> None:
    response = api.get(FORECAST_URL)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["company_id"] == "2"
    assert data["date_from"] == "2026-11-20"
    assert data["date_to"] == "2027-03-20"
    assert data["as_of"] == "2026-11-20"
    assert data["items"] == []
    assert data["totals"]["item_count"] == 0
    assert data["totals"]["inflow_confirmed_irr"] == 0
    assert data["totals"]["inflow_forecast_irr"] == 0
    assert data["source_counts"]["expected_sale"] == 0
    assert data["skipped_undated"] == 0
    # The horizon always carries all six buckets, even empty.
    assert [b["key"] for b in data["horizon"]] == [
        "overdue",
        "0_30",
        "31_60",
        "61_90",
        "91_120",
        "beyond_120",
    ]
    freshness = data["freshness"]
    assert freshness["source"] == "hesabyar-registry+tryton:cash-forecast"
    assert freshness["freshness_indicator"] == "live"
    assert freshness["calculation_version"] == "registered-records/1.0"
    assert freshness["company_id"] == "2"


def test_cash_forecast_aggregates_tranche_and_cheque(
    api: TestClient, world: _World
) -> None:
    world.forecast.settlement.tranches["tranche-1"] = _tranche(
        installation_id=INSTALLATION_ID,
        expected_date=datetime.date(2026, 11, 25),
        net_irr=9_450_000,
    )
    world.cheques.create(
        _cheque(id="c-1", tenant_id=TENANT_ID, installation_id=INSTALLATION_ID)
    )
    response = api.get(FORECAST_URL)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["totals"]["inflow_confirmed_irr"] == 19_450_000
    assert data["totals"]["item_count"] == 2
    assert data["source_counts"]["settlement_tranche"] == 1
    assert data["source_counts"]["cheque_received"] == 1
    items = data["items"]
    assert items[0]["date"] == "2026-11-25"  # sorted oldest first
    assert items[0]["bucket"] == "confirmed"
    assert items[0]["direction"] == "inflow"
    # 2026-11-25 is 1405-09-04 (Azar) — Aban ends 2026-11-21 — so the
    # tranche day lives in the Azar month, not Aban.
    azar = next(m for m in data["months"] if m["month_jalali"] == "1405-09")
    day = next(d for d in azar["days"] if d["date"] == "2026-11-25")
    assert day["inflow_confirmed_irr"] == 9_450_000
    assert day["item_count"] == 1


def test_cash_forecast_needs_reports_read_scope(api: TestClient) -> None:
    cast(FastAPI, api.app).dependency_overrides[get_authenticated_client] = lambda: _client(
        scopes=("cheques:read",)
    )
    response = api.get(FORECAST_URL)
    assert response.status_code == 403, response.text


def test_cash_forecast_requires_company(api: TestClient) -> None:
    response = api.get("/api/v1/reports/cash-forecast?date_from=2026-11-20&date_to=2027-03-20")
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "COMPANY_REQUIRED"


def test_cash_forecast_unknown_company_is_404(api: TestClient) -> None:
    cast(FastAPI, api.app).dependency_overrides[get_db_session] = lambda: _FakeSession(None)
    response = api.get(FORECAST_URL)
    assert response.status_code == 404, response.text


def test_cash_forecast_reversed_range_is_422(api: TestClient) -> None:
    response = api.get(
        f"/api/v1/reports/cash-forecast?company={COMPANY_ROW_ID}"
        "&date_from=2027-03-20&date_to=2026-11-20"
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_cash_forecast_rejects_unknown_params(api: TestClient) -> None:
    # Codebase convention: unknown query params are 400 INVALID_FILTER
    # (reject_unknown_params), not FastAPI's 422.
    response = api.get(FORECAST_URL + "&bogus=1")
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "INVALID_FILTER"


def test_openapi_lists_cash_forecast_path(api: TestClient) -> None:
    response = api.get("/openapi.json")
    assert response.status_code == 200, response.text
    paths = response.json()["paths"]
    assert "/api/v1/reports/cash-forecast" in paths
    assert "/api/v1/cheques/reports/monthly-match-trend" in paths


# -- monthly match trend -------------------------------------------------------------------


def _seed_history(world: _World) -> None:
    world.cheques.record_match_history(
        MonthlyMatchHistory(
            id="h-1",
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            month_jalali="1405-07",
            exact_count=8,
            probable_count=2,
            ambiguous_count=1,
            unmatched_count=1,
        )
    )
    world.cheques.record_match_history(
        MonthlyMatchHistory(
            id="h-2",
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            month_jalali="1405-08",
            exact_count=5,
            probable_count=0,
            ambiguous_count=0,
            unmatched_count=5,
        )
    )


def test_match_trend_renders_from_stored_history(api: TestClient, world: _World) -> None:
    _seed_history(world)
    response = api.get(TREND_URL)
    assert response.status_code == 200, response.text
    data = response.json()
    assert [m["month_jalali"] for m in data["months"]] == ["1405-07", "1405-08"]
    assert data["months"][0]["match_rate"] == "0.8333"
    assert data["months"][0]["total_lines"] == 12
    assert data["total_exact"] == 13
    assert data["total_probable"] == 2
    assert data["total_ambiguous"] == 1
    assert data["total_unmatched"] == 6
    assert data["total_lines"] == 22
    assert data["overall_match_rate"] == "0.6818"
    assert data["freshness"]["freshness_indicator"] == "stored"
    assert data["freshness"]["calculation_version"] == "stored-history/1.0"


def test_match_trend_empty_history_is_empty(api: TestClient) -> None:
    response = api.get(TREND_URL)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["months"] == []
    assert data["total_lines"] == 0
    assert data["overall_match_rate"] == "0"


def test_match_trend_months_param_limits_history(api: TestClient, world: _World) -> None:
    _seed_history(world)
    response = api.get(TREND_URL + "?months=1")
    assert response.status_code == 200, response.text
    data = response.json()
    assert [m["month_jalali"] for m in data["months"]] == ["1405-08"]


def test_match_trend_months_param_validated(api: TestClient) -> None:
    assert api.get(TREND_URL + "?months=0").status_code == 422
    assert api.get(TREND_URL + "?months=25").status_code == 422


def test_match_trend_needs_cheques_read_scope(api: TestClient) -> None:
    cast(FastAPI, api.app).dependency_overrides[get_authenticated_client] = lambda: _client(
        scopes=("reports:read",)
    )
    response = api.get(TREND_URL)
    assert response.status_code == 403, response.text


def test_match_trend_rejects_unknown_params(api: TestClient) -> None:
    # Codebase convention: unknown query params are 400 INVALID_FILTER
    # (reject_unknown_params), not FastAPI's 422.
    response = api.get(TREND_URL + "?bogus=1")
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "INVALID_FILTER"
