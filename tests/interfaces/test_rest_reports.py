"""Tests for the trial-balance + report-validation REST endpoints (US-403).

Everything is real — the FastAPI app, scoped API-key auth, the
PostgreSQL database, and the LIVE Tryton server for the trial balance
(the Adapter Test company). The registry is seeded once per module
(committed reference data, idempotent); provisioned tenants/keys/
company mappings are removed in teardown by deleting the tenant and
the engine connection (cascades to clients/keys/runs and mappings).
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
from hesabyar.infrastructure.persistence.postgres.seed_standards import seed_standards
from hesabyar.interfaces.rest.app import create_app

TEST_DB_URL = os.getenv(
    "HESABYAR_DATABASE_URL",
    "postgresql+psycopg://hesabyar:hesabyar_pass@hesabyar-postgres:5432/hesabyar_test",
)

INSTALLATION_ID = "00000000-0000-0000-0000-000000000403"

TRYTON_BASE_URL = os.environ.get("TRYTON_BASE_URL", "http://localhost:8010")
TRYTON_DATABASE = os.environ.get("TRYTON_DATABASE", "hesabyar_tryton_test")
TRYTON_API_KEY = os.environ.get("TRYTON_API_KEY", "hesabyar-dev-rest-key")
TEST_COMPANY_NAME = os.environ.get("TRYTON_TEST_COMPANY", "HesabYar Adapter Test")

# November 2026 (a 1405 period with posted moves on 2026-11-17) and
# 1405/01/01 (first blocked day for Standard 15).
PERIOD_NOV_2026 = ("2026-11-01", "2026-11-30")
PERIOD_1405_START = ("2026-03-21", "2026-03-31")


@dataclass
class World:
    """Provisioned tenant, company mapping and keys for one test."""

    tenant_id: str
    other_tenant_id: str
    company_row_id: str
    engine_id: int
    key_reports_secret: str
    key_validate_secret: str
    key_standards_secret: str
    key_other_reports_secret: str


@pytest.fixture(scope="module")
def engine() -> Generator[Engine, None, None]:
    eng = create_engine(TEST_DB_URL, echo=False)
    Base.metadata.create_all(eng)
    session = sessionmaker(bind=eng)()
    try:
        seed_standards(session)
        session.commit()
    finally:
        session.close()
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def test_company() -> CompanyRef:
    """Resolve the live Adapter Test company (skip when Tryton is down)."""
    client = TrytonRestClient(TRYTON_BASE_URL, TRYTON_DATABASE, TRYTON_API_KEY)
    try:
        if not client.ping():
            pytest.skip(f"trytond server not reachable at {TRYTON_BASE_URL}")
        for record in client.search("company.company", fields=["rec_name"]):
            if record["rec_name"] == TEST_COMPANY_NAME:
                return CompanyRef(engine_id=int(record["id"]))
    finally:
        client.close()
    pytest.skip(f"Test company {TEST_COMPANY_NAME!r} not found in Tryton DB {TRYTON_DATABASE!r}.")


@pytest.fixture()
def settings() -> AppSettings:
    """Fresh settings (dev pepper) — the same pepper the app dependency uses."""
    return AppSettings()


@pytest.fixture()
def world(
    engine: Engine, settings: AppSettings, test_company: CompanyRef
) -> Generator[World, None, None]:
    """Provision two tenants, a company mapping and three keys; clean up after."""
    session = sessionmaker(bind=engine)()
    service = ApiKeyService(ApiKeyCrypto(settings.auth.api_key_pepper))
    tenant_ids: list[str] = []
    connection_ids: list[str] = []
    try:
        tenant = TenantModel(id=str(uuid.uuid4()), slug=f"rest-403-{uuid.uuid4().hex[:8]}", name="Tenant 1")
        other_tenant = TenantModel(
            id=str(uuid.uuid4()), slug=f"rest-403-{uuid.uuid4().hex[:8]}", name="Tenant 2"
        )
        session.add_all([tenant, other_tenant])
        session.flush()
        client = ApiClientModel(id=str(uuid.uuid4()), tenant_id=tenant.id, name="rest-403 client")
        other_client = ApiClientModel(
            id=str(uuid.uuid4()), tenant_id=other_tenant.id, name="rest-403 other client"
        )
        session.add_all([client, other_client])
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
        connection_ids.append(connection.id)
        row = EngineCompanyModel(
            id=str(uuid.uuid4()),
            tenant_id=tenant.id,
            engine_connection_id=connection.id,
            external_company_id=str(test_company.engine_id),
            company_name=TEST_COMPANY_NAME,
        )
        session.add(row)
        session.flush()

        def _key(client_row: ApiClientModel, scopes: list[str]):
            return service.issue_key(
                session,
                client=client_row,
                name="rest-403 key",
                scopes=scopes,
                installation_id=INSTALLATION_ID,
            )

        key_reports = _key(client, ["reports:read"])
        key_validate = _key(client, ["reports:read", "standards:validate"])
        key_standards = _key(client, ["standards:read"])
        key_other_reports = _key(other_client, ["reports:read"])
        session.commit()
        tenant_ids = [tenant.id, other_tenant.id]
        yield World(
            tenant_id=tenant.id,
            other_tenant_id=other_tenant.id,
            company_row_id=row.id,
            engine_id=test_company.engine_id,
            key_reports_secret=key_reports.secret,
            key_validate_secret=key_validate.secret,
            key_standards_secret=key_standards.secret,
            key_other_reports_secret=key_other_reports.secret,
        )
    finally:
        session.close()
        cleanup = sessionmaker(bind=engine)()
        try:
            if tenant_ids:
                cleanup.query(TenantModel).filter(TenantModel.id.in_(tenant_ids)).delete(
                    synchronize_session=False
                )
            if connection_ids:
                cleanup.query(EngineConnectionModel).filter(
                    EngineConnectionModel.id.in_(connection_ids)
                ).delete(synchronize_session=False)
            cleanup.commit()
        finally:
            cleanup.close()


def _test_client(settings: AppSettings) -> TestClient:
    """Real app with real wiring (no overrides)."""
    app = create_app(settings=settings)
    return TestClient(app, raise_server_exceptions=False)


def _auth(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


def _trial_balance_url(world: World, date_from: str, date_to: str) -> str:
    return (
        f"/api/v1/reports/trial-balance?company={world.engine_id}"
        f"&date_from={date_from}&date_to={date_to}"
    )


def _create_report_run(
    client: TestClient, secret: str, world: World, body: dict
) -> dict:
    response = client.post("/api/v1/report-validation-runs", json=body, headers=_auth(secret))
    assert response.status_code == 201, response.text
    return response.json()


def _report_run_body(
    world: World, period: tuple[str, str], standard_codes: list[str] | None = None
) -> dict:
    body: dict = {
        "company_id": str(world.engine_id),
        "period_start": period[0],
        "period_end": period[1],
    }
    if standard_codes is not None:
        body["standard_codes"] = standard_codes
    return body


# ---------------------------------------------------------------------------
# GET /reports/trial-balance
# ---------------------------------------------------------------------------


def test_trial_balance_requires_auth(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get(_trial_balance_url(world, *PERIOD_NOV_2026))
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"


def test_trial_balance_requires_scope(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get(
        _trial_balance_url(world, *PERIOD_NOV_2026),
        headers=_auth(world.key_standards_secret),
    )
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


def test_trial_balance_returns_posted_figures_with_provenance(
    settings: AppSettings, world: World
) -> None:
    client = _test_client(settings)
    response = client.get(
        _trial_balance_url(world, *PERIOD_NOV_2026),
        headers=_auth(world.key_reports_secret),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["company_id"] == str(world.engine_id)
    assert body["as_of_date"] == PERIOD_NOV_2026[1]
    assert body["balanced"] is True
    assert body["total_debit"] == body["total_credit"]
    assert body["total_debit"] > 0
    assert body["move_count"] >= 1
    assert body["line_count"] >= 2
    assert body["source_endpoint"] == "account.move.line (Tryton REST)"
    assert body["lines"], "November 2026 has posted moves"
    for line in body["lines"]:
        assert line["balance"] == line["total_debit"] - line["total_credit"]
        assert line["code"] and line["name"]
    assert [line["code"] for line in body["lines"]] == sorted(
        line["code"] for line in body["lines"]
    )


def test_trial_balance_empty_period_is_balanced_zero(
    settings: AppSettings, world: World
) -> None:
    client = _test_client(settings)
    response = client.get(
        _trial_balance_url(world, "2020-01-01", "2020-01-31"),
        headers=_auth(world.key_reports_secret),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["balanced"] is True
    assert body["total_debit"] == 0
    assert body["total_credit"] == 0
    assert body["lines"] == []


def test_trial_balance_unknown_company_404(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get(
        "/api/v1/reports/trial-balance?company=999999&date_from=2026-11-01&date_to=2026-11-30",
        headers=_auth(world.key_reports_secret),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "COMPANY_NOT_FOUND"


def test_trial_balance_reversed_range_422(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get(
        _trial_balance_url(world, "2026-11-30", "2026-11-01"),
        headers=_auth(world.key_reports_secret),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# POST /report-validation-runs
# ---------------------------------------------------------------------------


def test_create_report_run_returns_run_with_provenance(
    settings: AppSettings, world: World
) -> None:
    client = _test_client(settings)
    body = _create_report_run(
        client,
        world.key_validate_secret,
        world,
        _report_run_body(world, PERIOD_NOV_2026, ["1"]),
    )
    run = body["run"]
    # Trial-balance data has no statement figures: statement rules ERROR.
    assert run["overall_outcome"] == "error"
    assert run["is_valid"] is False
    assert run["rules_executed"] == 35
    assert run["rules_errored"] == 35
    assert run["rules_passed"] == 0
    assert [item["rule_id"] for item in run["not_executed"]] == ["CF005"]
    assert run["coverage"]["corpus_standards"] == 35
    assert run["coverage"]["rules_implemented"] == 36
    assert run["coverage"]["rules_executed"] == 35
    assert run["period_start"] == PERIOD_NOV_2026[0]
    assert run["period_end"] == PERIOD_NOV_2026[1]
    assert run["standard_codes"] == ["1"]
    provenance = body["provenance"]
    assert provenance["snapshot_id"]
    assert provenance["source_endpoint"] == "account.move.line (Tryton REST)"
    assert provenance["as_of_date"] == PERIOD_NOV_2026[1]
    assert len(provenance["payload_hash"]) == 64
    assert provenance["ruleset_version"] == "1.0.0-legacy"
    assert provenance["balanced"] is True
    assert provenance["total_debit"] == provenance["total_credit"] > 0
    assert body["note"], "the Persian note explains why statement rules errored"


def test_create_report_run_blocked_for_1405_period(
    settings: AppSettings, world: World
) -> None:
    client = _test_client(settings)
    response = client.post(
        "/api/v1/report-validation-runs",
        json=_report_run_body(world, PERIOD_1405_START),
        headers=_auth(world.key_validate_secret),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "STANDARD_SOURCE_BLOCKED"


def test_create_report_run_requires_validate_scope(
    settings: AppSettings, world: World
) -> None:
    client = _test_client(settings)
    response = client.post(
        "/api/v1/report-validation-runs",
        json=_report_run_body(world, PERIOD_NOV_2026, ["1"]),
        headers=_auth(world.key_reports_secret),
    )
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


def test_create_report_run_unknown_company_404(
    settings: AppSettings, world: World
) -> None:
    client = _test_client(settings)
    response = client.post(
        "/api/v1/report-validation-runs",
        json=_report_run_body(world, PERIOD_NOV_2026, ["1"]) | {"company_id": "999999"},
        headers=_auth(world.key_validate_secret),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "COMPANY_NOT_FOUND"


# ---------------------------------------------------------------------------
# GET /report-validation-runs/{run_id}
# ---------------------------------------------------------------------------


def test_get_report_run_round_trip(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    created = _create_report_run(
        client,
        world.key_validate_secret,
        world,
        _report_run_body(world, PERIOD_NOV_2026, ["1"]),
    )
    run_id = created["run"]["run_id"]
    response = client.get(
        f"/api/v1/report-validation-runs/{run_id}",
        headers=_auth(world.key_reports_secret),
    )
    assert response.status_code == 200, response.text
    assert response.json() == created


def test_get_report_run_unknown_404(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get(
        f"/api/v1/report-validation-runs/{uuid.uuid4()}",
        headers=_auth(world.key_reports_secret),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "REPORT_VALIDATION_RUN_NOT_FOUND"


def test_get_report_run_other_tenant_404(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    created = _create_report_run(
        client,
        world.key_validate_secret,
        world,
        _report_run_body(world, PERIOD_NOV_2026, ["1"]),
    )
    response = client.get(
        f"/api/v1/report-validation-runs/{created['run']['run_id']}",
        headers=_auth(world.key_other_reports_secret),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "REPORT_VALIDATION_RUN_NOT_FOUND"


def test_get_report_run_plain_validation_run_404(
    settings: AppSettings, world: World
) -> None:
    """A run created by POST /validation-runs has no snapshot: not a report run."""
    client = _test_client(settings)
    response = client.post(
        "/api/v1/validation-runs",
        json={
            "period_start": PERIOD_NOV_2026[0],
            "period_end": PERIOD_NOV_2026[1],
            "standard_codes": ["1"],
            "data": {"balance_sheet": {"total_assets": 1}},
        },
        headers=_auth(world.key_validate_secret),
    )
    assert response.status_code == 201, response.text
    run_id = response.json()["run_id"]
    response = client.get(
        f"/api/v1/report-validation-runs/{run_id}",
        headers=_auth(world.key_reports_secret),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "REPORT_VALIDATION_RUN_NOT_FOUND"


def test_openapi_lists_report_endpoints(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get("/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/api/v1/reports/trial-balance" in paths
    assert "/api/v1/report-validation-runs" in paths
    assert "/api/v1/report-validation-runs/{run_id}" in paths
