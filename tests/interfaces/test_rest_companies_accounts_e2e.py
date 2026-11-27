"""End-to-end tests for the companies + accounts REST endpoints (US-105).

The Phase 1 exit proof: a scoped client reads real company and account
metadata through HesabYar — the real FastAPI app, a real issued API key
against the real database, and the LIVE Tryton 8.2.3 server, using ONLY
the dedicated test company "HesabYar Adapter Test" (provisioned by
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

INSTALLATION_ID = "00000000-0000-0000-0000-000000000004"

# A postable leaf account of the universal chart (cash), per the US-104
# contract tests.
CASH_ACCOUNT_CODE = "1.1.1"

# An id no company/account will ever have.
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
    key_full_secret: str  # company:read + account:read, unrestricted
    key_company_secret: str  # company:read only, unrestricted
    key_restricted_secret: str  # both scopes, restricted to the test company


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
            id=str(uuid.uuid4()), slug=f"rest-105-e2e-{uuid.uuid4().hex[:8]}", name="E2E Tenant"
        )
        session.add(tenant)
        session.flush()
        client = ApiClientModel(
            id=str(uuid.uuid4()), tenant_id=tenant.id, name="rest-105 e2e client"
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
                name="rest-105 e2e key",
                scopes=scopes,
                installation_id=INSTALLATION_ID,
                **kwargs,  # type: ignore[arg-type]
            )

        key_full = _key(["company:read", "account:read"])
        key_company = _key(["company:read"])
        key_restricted = _key(
            ["company:read", "account:read"], engine_company_id=row.id
        )
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
# The Phase 1 exit proof: a scoped client reads real metadata end to end
# ---------------------------------------------------------------------------


def test_e2e_scoped_client_reads_company_and_accounts(
    api: TestClient, world: World
) -> None:
    """All four endpoints return real Tryton metadata through HesabYar."""
    headers = _auth(world.key_full_secret)

    # GET /companies — the tenant's mapped company, with real metadata.
    companies = api.get("/api/v1/companies", headers=headers)
    assert companies.status_code == 200
    items = companies.json()["items"]
    assert [item["name"] for item in items] == [TEST_COMPANY_NAME]
    company = items[0]
    assert company["id"] == world.company_row_id
    assert company["engine_id"] == world.engine_id
    assert company["currency_code"] == "IRR"
    # Other engine companies (e.g. "HesabYar IRR Test") are not mapped
    # to this tenant and stay invisible.
    assert len(items) == 1

    # GET /companies/{id} — by row id and by engine id.
    for company_id in (world.company_row_id, world.engine_id):
        one = api.get(f"/api/v1/companies/{company_id}", headers=headers)
        assert one.status_code == 200
        assert one.json() == company

    # GET /accounting/accounts — the real chart of the real company.
    chart = api.get(
        f"/api/v1/accounting/accounts?company={world.company_row_id}",
        headers=headers,
    )
    assert chart.status_code == 200
    accounts = chart.json()["items"]
    assert accounts, "expected a non-empty chart for the Adapter Test company"
    assert all(item["company_id"] == world.company_row_id for item in accounts)
    cash = next(
        (item for item in accounts if item["code"] == CASH_ACCOUNT_CODE), None
    )
    assert cash is not None, f"expected account {CASH_ACCOUNT_CODE!r} in the chart"
    assert cash["name"]
    assert cash["active"] is True
    assert cash["closed"] is False
    assert cash["postable"] is True

    # GET /accounting/accounts/{id} — the same account, by engine id.
    one_account = api.get(
        f"/api/v1/accounting/accounts/{cash['id']}?company={world.company_row_id}",
        headers=headers,
    )
    assert one_account.status_code == 200
    assert one_account.json() == cash


def test_e2e_chart_paginates_over_live_data(api: TestClient, world: World) -> None:
    """Cursor pagination walks the whole live chart without loss or duplication."""
    headers = _auth(world.key_full_secret)
    base = f"/api/v1/accounting/accounts?company={world.company_row_id}"

    def _walk(limit: int) -> list[dict]:
        seen: list[dict] = []
        cursor: str | None = None
        for _ in range(200):  # safety bound; the chart is far smaller
            url = f"{base}&limit={limit}" + (f"&cursor={cursor}" if cursor else "")
            page = api.get(url, headers=headers)
            assert page.status_code == 200
            seen.extend(page.json()["items"])
            cursor = page.json()["next_cursor"]
            if cursor is None:
                return seen
        pytest.fail("pagination did not terminate")

    full = _walk(100)
    assert len(full) > 2, "chart too small to exercise pagination"
    small_pages = _walk(2)
    assert [item["id"] for item in small_pages] == [item["id"] for item in full]


def test_e2e_restricted_key_reads_without_company_param(
    api: TestClient, world: World
) -> None:
    """A company-restricted key defaults to its company on account endpoints."""
    headers = _auth(world.key_restricted_secret)
    chart = api.get("/api/v1/accounting/accounts", headers=headers)
    assert chart.status_code == 200
    accounts = chart.json()["items"]
    assert accounts
    assert all(item["company_id"] == world.company_row_id for item in accounts)
    one = api.get(f"/api/v1/accounting/accounts/{accounts[1]['id']}", headers=headers)
    assert one.status_code == 200
    assert one.json()["company_id"] == world.company_row_id


# ---------------------------------------------------------------------------
# 401 / 403 / 404 through HTTP against the live stack
# ---------------------------------------------------------------------------


def test_e2e_no_key_returns_401(api: TestClient, world: World) -> None:
    for path in (
        "/api/v1/companies",
        f"/api/v1/companies/{world.company_row_id}",
        f"/api/v1/accounting/accounts?company={world.company_row_id}",
        f"/api/v1/accounting/accounts/1?company={world.company_row_id}",
    ):
        response = api.get(path)
        assert response.status_code == 401
        assert response.json()["code"] == "UNAUTHORIZED"


def test_e2e_wrong_scope_returns_403(api: TestClient, world: World) -> None:
    # A company:read-only key may list companies but not read accounts.
    headers = _auth(world.key_company_secret)
    ok = api.get("/api/v1/companies", headers=headers)
    assert ok.status_code == 200
    forbidden = api.get(
        f"/api/v1/accounting/accounts?company={world.company_row_id}",
        headers=headers,
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "FORBIDDEN"
    assert "account:read" in forbidden.json()["detail"]


def test_e2e_unknown_company_and_account_return_typed_404(
    api: TestClient, world: World
) -> None:
    headers = _auth(world.key_full_secret)
    company = api.get(f"/api/v1/companies/{uuid.uuid4()}", headers=headers)
    assert company.status_code == 404
    assert company.json()["code"] == "COMPANY_NOT_FOUND"
    account = api.get(
        f"/api/v1/accounting/accounts/{MISSING_ID}?company={world.company_row_id}",
        headers=headers,
    )
    assert account.status_code == 404
    assert account.json()["code"] == "ACCOUNT_NOT_FOUND"
