"""End-to-end tests for the fiscal-year / period / setup-status REST
endpoints (US-203).

The Phase 2 proof for fiscal reads: a scoped client reads real fiscal
years, periods and setup state through HesabYar — the real FastAPI app,
a real issued API key against the real database, and the LIVE Tryton
8.2.3 server, using ONLY the dedicated test company "HesabYar Adapter
Test" (provisioned by
``~/workspace/projects/hesabyar-reference/setup_adapter_test_company.py``;
see docs/adapter.md). The company is looked up BY NAME — numeric engine
ids are never hardcoded. Tests only read; no Tryton data is created or
modified.

When trytond is not reachable (or the test company has not been
provisioned yet), every test SKIPS cleanly with a clear message — the
suite stays green without a server. With the server up, the tests must
PASS.

Configuration (all optional, dev defaults match docs/dev-setup.md):
  TRYTON_BASE_URL       (default http://localhost:8000)
  TRYTON_DATABASE       (default hesabyar_tryton_test)
  TRYTON_API_KEY        (default hesabyar-dev-rest-key)
  TRYTON_TEST_COMPANY   (default "HesabYar Adapter Test")
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Generator
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from hesabyar.domain.accounting.refs import CompanyRef
from hesabyar.infrastructure.accounting.tryton.adapter import TrytonAccountingAdapter
from hesabyar.infrastructure.accounting.tryton.client import TrytonRestClient
from hesabyar.infrastructure.auth import ApiKeyCrypto, ApiKeyService
from hesabyar.infrastructure.config import AppSettings
from hesabyar.infrastructure.persistence.postgres.models import (
    ApiClientModel,
    Base,
    EngineCompanyModel,
    EngineConnectionModel,
    TenantModel,
)
from hesabyar.interfaces.rest.app import create_app

TEST_DB_URL = os.getenv(
    "HESABYAR_DATABASE_URL",
    "postgresql+psycopg://hesabyar:hesabyar_pass@hesabyar-postgres:5432/hesabyar_test",
)

TRYTON_BASE_URL = os.environ.get("TRYTON_BASE_URL", "http://localhost:8000")
TRYTON_DATABASE = os.environ.get("TRYTON_DATABASE", "hesabyar_tryton_test")
TRYTON_API_KEY = os.environ.get("TRYTON_API_KEY", "hesabyar-dev-rest-key")
TEST_COMPANY_NAME = os.environ.get("TRYTON_TEST_COMPANY", "HesabYar Adapter Test")

INSTALLATION_ID = "00000000-0000-0000-0000-000000000006"

# An id no fiscal year/period will ever have.
MISSING_ID = 999_999_999


# ---------------------------------------------------------------------------
# Live Tryton fixtures (skip cleanly when the server is down)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def tryton_client() -> Generator[TrytonRestClient, None, None]:
    rest_client = TrytonRestClient(TRYTON_BASE_URL, TRYTON_DATABASE, TRYTON_API_KEY)
    if not rest_client.ping():
        rest_client.close()
        pytest.skip(
            f"trytond server not reachable at {TRYTON_BASE_URL} "
            "(start it per docs/dev-setup.md §3 to run the e2e tests)"
        )
    yield rest_client
    rest_client.close()


@pytest.fixture(scope="module")
def test_company(tryton_client: TrytonRestClient) -> CompanyRef:
    """The Adapter Test company's engine ref, looked up BY NAME."""
    adapter = TrytonAccountingAdapter(tryton_client)
    for view in adapter.list_companies():
        if view.name == TEST_COMPANY_NAME:
            return view.ref
    pytest.skip(
        f"Test company {TEST_COMPANY_NAME!r} not found in Tryton DB {TRYTON_DATABASE!r}. "
        "Provision it with setup_adapter_test_company.py (see docs/adapter.md)."
    )


# ---------------------------------------------------------------------------
# Provisioned world (real DB, real keys, real app)
# ---------------------------------------------------------------------------


@dataclass
class World:
    """Provisioned tenant, company mapping and keys for one test."""

    company_row_id: str
    engine_id: int
    key_full_secret: str  # ledger:read, unrestricted
    key_company_secret: str  # company:read only, unrestricted
    key_restricted_secret: str  # ledger:read, restricted to the test company


@pytest.fixture(scope="module")
def engine() -> Generator[Engine, None, None]:
    eng = create_engine(TEST_DB_URL, echo=False)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def settings() -> AppSettings:
    return AppSettings()


@pytest.fixture()
def world(
    engine: Engine, settings: AppSettings, test_company: CompanyRef
) -> Generator[World, None, None]:
    """Provision a tenant mapped to the live Adapter Test company."""
    session = sessionmaker(bind=engine)()
    service = ApiKeyService(ApiKeyCrypto(settings.auth.api_key_pepper))
    tenant: TenantModel | None = None
    tenant_id = ""
    connection_id = ""
    try:
        tenant = TenantModel(
            id=str(uuid.uuid4()), slug=f"rest-203-e2e-{uuid.uuid4().hex[:8]}", name="E2E Tenant"
        )
        session.add(tenant)
        session.flush()
        client = ApiClientModel(
            id=str(uuid.uuid4()), tenant_id=tenant.id, name="rest-203 e2e client"
        )
        session.add(client)
        session.flush()
        connection = EngineConnectionModel(
            id=str(uuid.uuid4()),
            name=f"conn-{uuid.uuid4().hex[:8]}",
            engine="tryton",
            base_url=TRYTON_BASE_URL,
            database_name=f"hesabyar_tryton_test_{uuid.uuid4().hex[:8]}",
        )
        session.add(connection)
        session.flush()
        connection_id = connection.id
        row = EngineCompanyModel(
            id=str(uuid.uuid4()),
            tenant_id=tenant.id,
            engine_connection_id=connection.id,
            external_company_id=str(test_company.engine_id),
            company_name=TEST_COMPANY_NAME,
        )
        session.add(row)
        session.flush()

        def _key(scopes: list[str], **kwargs: object):
            return service.issue_key(
                session,
                client=client,
                name="rest-203 e2e key",
                scopes=scopes,
                installation_id=INSTALLATION_ID,
                **kwargs,  # type: ignore[arg-type]
            )

        key_full = _key(["ledger:read"])
        key_company = _key(["company:read"])
        key_restricted = _key(["ledger:read"], engine_company_id=row.id)
        session.commit()
        tenant_id = tenant.id
        yield World(
            company_row_id=row.id,
            engine_id=test_company.engine_id,
            key_full_secret=key_full.secret,
            key_company_secret=key_company.secret,
            key_restricted_secret=key_restricted.secret,
        )
    finally:
        session.close()
        cleanup = sessionmaker(bind=engine)()
        try:
            if tenant_id:
                cleanup.query(TenantModel).filter(
                    TenantModel.id == tenant_id
                ).delete(synchronize_session=False)
            if connection_id:
                cleanup.query(EngineConnectionModel).filter(
                    EngineConnectionModel.id == connection_id
                ).delete(synchronize_session=False)
            cleanup.commit()
        finally:
            cleanup.close()


@pytest.fixture()
def api(settings: AppSettings) -> TestClient:
    """The real app with real wiring (no overrides at all)."""
    return TestClient(create_app(settings=settings), raise_server_exceptions=False)


def _auth(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


# ---------------------------------------------------------------------------
# The Phase 2 fiscal proof: a scoped client reads real fiscal data end to end
# ---------------------------------------------------------------------------


def test_e2e_scoped_client_reads_fiscal_years_periods_and_setup_status(
    api: TestClient, world: World
) -> None:
    """All five endpoints return real Tryton fiscal data through HesabYar."""
    headers = _auth(world.key_full_secret)
    company = world.company_row_id

    # GET /accounting/fiscal-years — FY 2026, open, with Jalali dates.
    years = api.get(f"/api/v1/accounting/fiscal-years?company={company}", headers=headers)
    assert years.status_code == 200
    items = years.json()["items"]
    assert items, "expected at least one fiscal year for the Adapter Test company"
    fy2026 = next(item for item in items if item["name"] == "FY 2026")
    assert fy2026["company_id"] == company
    assert fy2026["start_date"] == "2026-01-01"
    assert fy2026["end_date"] == "2026-12-31"
    assert fy2026["start_date_jalali"] == "1404/10/11"
    assert fy2026["end_date_jalali"] == "1405/10/10"
    assert fy2026["state"] == "open"
    assert fy2026["is_open"] is True
    assert all(item["company_id"] == company for item in items)

    # GET /accounting/fiscal-years/{id} — the same year, by engine id.
    one_year = api.get(
        f"/api/v1/accounting/fiscal-years/{fy2026['id']}?company={company}",
        headers=headers,
    )
    assert one_year.status_code == 200
    assert one_year.json() == fy2026

    # GET /accounting/fiscal-periods — 12 monthly periods, all open.
    periods = api.get(
        f"/api/v1/accounting/fiscal-periods?company={company}", headers=headers
    )
    assert periods.status_code == 200
    period_items = periods.json()["items"]
    assert len(period_items) >= 12
    assert all(item["company_id"] == company for item in period_items)
    assert all(item["state"] == "open" for item in period_items)
    assert all(item["type"] == "standard" for item in period_items)
    starts = [item["start_date"] for item in period_items]
    assert starts == sorted(starts)

    # Filtered by fiscal year: exactly the 12 periods of FY 2026.
    fy_periods = api.get(
        f"/api/v1/accounting/fiscal-periods?company={company}&fiscal_year={fy2026['id']}",
        headers=headers,
    )
    assert fy_periods.status_code == 200
    assert len(fy_periods.json()["items"]) == 12
    assert all(
        item["fiscal_year_id"] == fy2026["id"] for item in fy_periods.json()["items"]
    )

    # GET /accounting/fiscal-periods/{id} — one period, by engine id.
    first = fy_periods.json()["items"][0]
    one_period = api.get(
        f"/api/v1/accounting/fiscal-periods/{first['id']}?company={company}",
        headers=headers,
    )
    assert one_period.status_code == 200
    assert one_period.json() == first
    assert first["start_date"] == "2026-01-01"
    assert first["end_date"] == "2026-01-31"

    # GET /accounting/setup-status — the fully provisioned company is ready.
    status = api.get(f"/api/v1/accounting/setup-status?company={company}", headers=headers)
    assert status.status_code == 200
    body = status.json()
    assert body["company_id"] == company
    assert body["chart_installed"] is True
    assert body["account_count"] >= 100
    assert body["chart_template"] is None
    assert body["fiscal_year_present"] is True
    # setup-status reports the NEWEST open year (SetupStatusService:
    # "years come newest first, so the first open year is the newest").
    # The CI Tryton DB has both FY 2026 and FY 2027 open, so pin the
    # expectation to the newest open year in the list above instead
    # of assuming FY 2026 is the only open year.
    open_years = [item for item in items if item["is_open"]]
    newest_open = max(open_years, key=lambda item: item["start_date"])
    assert body["open_fiscal_year"]["name"] == newest_open["name"]
    assert body["open_period_present"] is True
    assert body["open_period_count"] >= 12
    assert body["ready"] is True


def test_e2e_periods_paginate_over_live_data(api: TestClient, world: World) -> None:
    """Cursor pagination walks all live periods without loss or duplication."""
    headers = _auth(world.key_full_secret)
    base = f"/api/v1/accounting/fiscal-periods?company={world.company_row_id}"

    def _walk(limit: int) -> list[dict]:
        seen: list[dict] = []
        cursor: str | None = None
        for _ in range(200):  # safety bound; the period list is far smaller
            url = f"{base}&limit={limit}" + (f"&cursor={cursor}" if cursor else "")
            page = api.get(url, headers=headers)
            assert page.status_code == 200
            seen.extend(page.json()["items"])
            cursor = page.json()["next_cursor"]
            if cursor is None:
                return seen
        pytest.fail("pagination did not terminate")

    full = _walk(100)
    assert len(full) >= 12
    small_pages = _walk(2)
    assert [item["id"] for item in small_pages] == [item["id"] for item in full]


def test_e2e_restricted_key_reads_without_company_param(
    api: TestClient, world: World
) -> None:
    """A company-restricted key defaults to its company on fiscal endpoints."""
    headers = _auth(world.key_restricted_secret)
    years = api.get("/api/v1/accounting/fiscal-years", headers=headers)
    assert years.status_code == 200
    assert years.json()["items"]
    assert all(item["company_id"] == world.company_row_id for item in years.json()["items"])
    status = api.get("/api/v1/accounting/setup-status", headers=headers)
    assert status.status_code == 200
    assert status.json()["company_id"] == world.company_row_id
    assert status.json()["ready"] is True


# ---------------------------------------------------------------------------
# 401 / 403 / 404 through HTTP against the live stack
# ---------------------------------------------------------------------------


def test_e2e_no_key_returns_401(api: TestClient, world: World) -> None:
    for path in (
        f"/api/v1/accounting/fiscal-years?company={world.company_row_id}",
        "/api/v1/accounting/fiscal-years/1",
        f"/api/v1/accounting/fiscal-periods?company={world.company_row_id}",
        "/api/v1/accounting/fiscal-periods/1",
        f"/api/v1/accounting/setup-status?company={world.company_row_id}",
    ):
        response = api.get(path)
        assert response.status_code == 401
        assert response.json()["code"] == "UNAUTHORIZED"


def test_e2e_wrong_scope_returns_403(api: TestClient, world: World) -> None:
    # A company:read-only key may not read fiscal data.
    headers = _auth(world.key_company_secret)
    forbidden = api.get(
        f"/api/v1/accounting/fiscal-years?company={world.company_row_id}",
        headers=headers,
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "FORBIDDEN"
    assert "ledger:read" in forbidden.json()["detail"]


def test_e2e_unknown_year_period_and_company_return_typed_404(
    api: TestClient, world: World
) -> None:
    headers = _auth(world.key_full_secret)
    company = world.company_row_id
    year = api.get(
        f"/api/v1/accounting/fiscal-years/{MISSING_ID}?company={company}", headers=headers
    )
    assert year.status_code == 404
    assert year.json()["code"] == "FISCAL_YEAR_NOT_FOUND"
    period = api.get(
        f"/api/v1/accounting/fiscal-periods/{MISSING_ID}?company={company}",
        headers=headers,
    )
    assert period.status_code == 404
    assert period.json()["code"] == "FISCAL_PERIOD_NOT_FOUND"
    bad_year_filter = api.get(
        f"/api/v1/accounting/fiscal-periods?company={company}&fiscal_year={MISSING_ID}",
        headers=headers,
    )
    assert bad_year_filter.status_code == 404
    assert bad_year_filter.json()["code"] == "FISCAL_YEAR_NOT_FOUND"
    unknown_company = api.get(
        f"/api/v1/accounting/fiscal-years?company={uuid.uuid4()}", headers=headers
    )
    assert unknown_company.status_code == 404
    assert unknown_company.json()["code"] == "COMPANY_NOT_FOUND"
