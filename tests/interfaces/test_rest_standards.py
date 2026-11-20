"""Tests for the standards registry + validation REST endpoints (US-401).

Everything is real — the FastAPI app, scoped API-key auth, the
PostgreSQL database — except the engine, which these endpoints never
touch (validation runs over supplied data). The live Tryton server is
NOT needed here.

The registry is seeded once per module (committed reference data,
idempotent); provisioned tenants/keys are removed in teardown by
deleting the tenant (cascades to clients/keys/runs).
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

from hesabyar.infrastructure.auth import ApiKeyCrypto, ApiKeyService
from hesabyar.infrastructure.config import AppSettings
from hesabyar.infrastructure.persistence.postgres.models import (
    ApiClientModel,
    Base,
    TenantModel,
)
from hesabyar.infrastructure.persistence.postgres.seed_standards import (
    seed_standards,
)
from hesabyar.interfaces.rest.app import create_app
from tests.standards_fixtures import BALANCED_DATA

TEST_DB_URL = os.getenv(
    "HESABYAR_DATABASE_URL",
    "postgresql+psycopg://hesabyar:hesabyar_pass@hesabyar-postgres:5432/hesabyar_test",
)

INSTALLATION_ID = "00000000-0000-0000-0000-000000000401"

# 1404/06/01 (a 1404 period) and 1405/01/01 (first blocked day for Standard 15).
PERIOD_1404 = "2025-08-23"
PERIOD_1405_START = "2026-03-21"


@dataclass
class World:
    """Provisioned tenant and keys for one test."""

    tenant_id: str
    other_tenant_id: str
    key_read_secret: str
    key_validate_secret: str
    key_company_secret: str
    key_other_read_secret: str


@pytest.fixture(scope="module")
def engine() -> Generator[Engine, None, None]:
    eng = create_engine(TEST_DB_URL, echo=False)
    Base.metadata.create_all(eng)
    # Seed the registry once per module (idempotent reference data).
    session = sessionmaker(bind=eng)()
    try:
        seed_standards(session)
        session.commit()
    finally:
        session.close()
    yield eng
    eng.dispose()


@pytest.fixture()
def settings() -> AppSettings:
    """Fresh settings (dev pepper) — the same pepper the app dependency uses."""
    return AppSettings()


@pytest.fixture()
def world(engine: Engine, settings: AppSettings) -> Generator[World, None, None]:
    """Provision two tenants and four keys; clean up after."""
    session = sessionmaker(bind=engine)()
    service = ApiKeyService(ApiKeyCrypto(settings.auth.api_key_pepper))
    tenant_ids: list[str] = []
    try:
        tenant = TenantModel(id=str(uuid.uuid4()), slug=f"rest-401-{uuid.uuid4().hex[:8]}", name="Tenant 1")
        other_tenant = TenantModel(id=str(uuid.uuid4()), slug=f"rest-401-{uuid.uuid4().hex[:8]}", name="Tenant 2")
        session.add_all([tenant, other_tenant])
        session.flush()
        client = ApiClientModel(id=str(uuid.uuid4()), tenant_id=tenant.id, name="rest-401 client")
        other_client = ApiClientModel(id=str(uuid.uuid4()), tenant_id=other_tenant.id, name="rest-401 other client")
        session.add_all([client, other_client])
        session.flush()

        def _key(client_row: ApiClientModel, scopes: list[str]):
            return service.issue_key(
                session,
                client=client_row,
                name="rest-401 key",
                scopes=scopes,
                installation_id=INSTALLATION_ID,
            )

        key_read = _key(client, ["standards:read"])
        key_validate = _key(client, ["standards:read", "standards:validate"])
        key_company = _key(client, ["company:read"])
        key_other_read = _key(other_client, ["standards:read"])
        session.commit()
        tenant_ids = [tenant.id, other_tenant.id]
        yield World(
            tenant_id=tenant.id,
            other_tenant_id=other_tenant.id,
            key_read_secret=key_read.secret,
            key_validate_secret=key_validate.secret,
            key_company_secret=key_company.secret,
            key_other_read_secret=key_other_read.secret,
        )
    finally:
        session.close()
        cleanup = sessionmaker(bind=engine)()
        try:
            if tenant_ids:
                cleanup.query(TenantModel).filter(TenantModel.id.in_(tenant_ids)).delete(synchronize_session=False)
            cleanup.commit()
        finally:
            cleanup.close()


def _test_client(settings: AppSettings) -> TestClient:
    """Real app (standards endpoints need no engine adapter)."""
    app = create_app(settings=settings)
    return TestClient(app, raise_server_exceptions=False)


def _auth(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


def _create_run(
    client: TestClient,
    secret: str,
    body: dict,
) -> dict:
    response = client.post("/api/v1/validation-runs", json=body, headers=_auth(secret))
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------------------
# GET /standards
# ---------------------------------------------------------------------------


def test_list_standards_requires_auth(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get("/api/v1/standards")
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"


def test_list_standards_requires_scope(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get("/api/v1/standards", headers=_auth(world.key_company_secret))
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


def test_list_standards_returns_35_in_numeric_order(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get("/api/v1/standards", headers=_auth(world.key_read_secret))
    assert response.status_code == 200
    body = response.json()
    assert body["next_cursor"] is None
    assert len(body["items"]) == 35
    assert [item["standard_code"] for item in body["items"]] == [
        "1",
        "2",
        "4",
        "5",
        "8",
        "10",
        "11",
        "12",
        "13",
        "15",
        "16",
        "17",
        "18",
        "20",
        "22",
        "24",
        "25",
        "26",
        "27",
        "28",
        "30",
        "31",
        "32",
        "33",
        "34",
        "35",
        "36",
        "37",
        "38",
        "39",
        "40",
        "41",
        "42",
        "43",
        "44",
    ]
    first = body["items"][0]
    assert first["standard_code"] == "1"
    assert first["rules_count"] == 36
    assert first["latest_verification_status"] == "UNVERIFIED"
    assert first["latest_revision_id"] == "1397"
    assert set(first) == {
        "standard_code",
        "title",
        "title_en",
        "ifrs_equivalent",
        "phase",
        "slug",
        "source_authority",
        "source_url",
        "latest_revision_id",
        "latest_verification_status",
        "latest_effective_from",
        "latest_effective_to",
        "rules_count",
    }


def test_list_standards_filters(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get(
        "/api/v1/standards?verification_status=VERIFIED_TEXT",
        headers=_auth(world.key_read_secret),
    )
    assert response.status_code == 200
    assert [item["standard_code"] for item in response.json()["items"]] == ["43", "44"]
    response = client.get(
        "/api/v1/standards?verification_status=BLOCKED_SOURCE_GAP",
        headers=_auth(world.key_read_secret),
    )
    assert [item["standard_code"] for item in response.json()["items"]] == ["15"]
    response = client.get("/api/v1/standards?has_rules=true", headers=_auth(world.key_read_secret))
    assert [item["standard_code"] for item in response.json()["items"]] == ["1"]
    response = client.get("/api/v1/standards?phase=1", headers=_auth(world.key_read_secret))
    assert {item["standard_code"] for item in response.json()["items"]} == {
        "1",
        "2",
        "34",
        "35",
    }


def test_list_standards_rejects_unknown_filter_and_status(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get(
        "/api/v1/standards?verification_status=BOGUS",
        headers=_auth(world.key_read_secret),
    )
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_FILTER"
    response = client.get("/api/v1/standards?bogus=1", headers=_auth(world.key_read_secret))
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_FILTER"


def test_list_standards_cursor_pagination(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    first = client.get("/api/v1/standards?limit=10", headers=_auth(world.key_read_secret))
    assert first.status_code == 200
    first_body = first.json()
    assert len(first_body["items"]) == 10
    assert first_body["next_cursor"] is not None
    second = client.get(
        f"/api/v1/standards?limit=10&cursor={first_body['next_cursor']}",
        headers=_auth(world.key_read_secret),
    )
    assert second.status_code == 200
    second_body = second.json()
    assert len(second_body["items"]) == 10
    assert second_body["items"][0]["standard_code"] == "16"
    seen = {item["standard_code"] for item in first_body["items"]}
    assert not seen & {item["standard_code"] for item in second_body["items"]}


# ---------------------------------------------------------------------------
# GET /standards/{code}
# ---------------------------------------------------------------------------


def test_get_standard_43_detail(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get("/api/v1/standards/43", headers=_auth(world.key_read_secret))
    assert response.status_code == 200
    body = response.json()
    assert body["standard_code"] == "43"
    assert body["ifrs_equivalent"] == "IFRS 15"
    assert len(body["revisions"]) == 1
    revision = body["revisions"][0]
    assert revision["revision_id"] == "1404"
    assert revision["verification_status"] == "VERIFIED_TEXT"
    assert revision["effective_from"] == "2025-03-21"
    assert revision["effective_to"] is None
    assert revision["sha256"] is not None and len(revision["sha256"]) == 64
    assert revision["snapshot_uri"] == "standards/43-revenue-from-contracts/source/standard.md"
    assert "Supersedes Standards 3, 9 and 29" in (revision["notes"] or "")
    assert body["rules"] == []


def test_get_standard_1_rules(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get("/api/v1/standards/1", headers=_auth(world.key_read_secret))
    assert response.status_code == 200
    rules = response.json()["rules"]
    assert len(rules) == 36
    by_id = {rule["rule_id"]: rule for rule in rules}
    assert by_id["BS001"]["dsl_formula"] is not None
    assert "1/100" in by_id["BS001"]["dsl_formula"]
    assert by_id["CF005"]["is_executable"] is False
    assert by_id["CF005"]["dsl_formula"] is None
    assert by_id["CF005"]["rule_type"] == "requirement"


def test_get_standard_unknown_404(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get("/api/v1/standards/99", headers=_auth(world.key_read_secret))
    assert response.status_code == 404
    assert response.json()["code"] == "STANDARD_NOT_FOUND"


# ---------------------------------------------------------------------------
# POST /validation-runs
# ---------------------------------------------------------------------------


def test_create_run_requires_validate_scope(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.post(
        "/api/v1/validation-runs",
        json={"data": BALANCED_DATA},
        headers=_auth(world.key_read_secret),
    )
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"
    response = client.post("/api/v1/validation-runs", json={"data": BALANCED_DATA})
    assert response.status_code == 401


def test_create_run_balanced_data_passes(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    body = _create_run(
        client,
        world.key_validate_secret,
        {
            "period_start": PERIOD_1404,
            "standard_codes": ["1"],
            "data": BALANCED_DATA,
        },
    )
    assert body["overall_outcome"] == "pass"
    assert body["is_valid"] is True
    assert body["rules_executed"] == 35
    assert body["rules_passed"] == 35
    assert body["rules_failed"] == 0
    assert body["rules_errored"] == 0
    assert body["ruleset_version"] == "1.0.0-legacy"
    assert body["standard_codes"] == ["1"]
    assert body["coverage"] == {
        "corpus_standards": 35,
        "standards_with_rules": 1,
        "rules_implemented": 36,
        "rules_verified": 0,
        "rules_executed": 35,
    }
    assert [item["rule_id"] for item in body["not_executed"]] == ["CF005"]
    assert len(body["results"]) == 35
    by_id = {result["rule_id"]: result for result in body["results"]}
    assert by_id["BS001"]["outcome"] == "pass"
    assert set(body) == {
        "run_id",
        "company_id",
        "period_start",
        "period_end",
        "standard_codes",
        "ruleset_version",
        "overall_outcome",
        "is_valid",
        "rules_executed",
        "rules_passed",
        "rules_failed",
        "rules_not_applicable",
        "rules_errored",
        "coverage",
        "results",
        "not_executed",
        "created_at",
    }


def test_create_run_missing_data_errors(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    body = _create_run(
        client,
        world.key_validate_secret,
        {"period_start": PERIOD_1404, "standard_codes": ["1"], "data": {}},
    )
    assert body["overall_outcome"] == "error"
    assert body["is_valid"] is False
    assert body["rules_errored"] == 35
    assert all(result["outcome"] == "error" for result in body["results"])


def test_create_run_standard_15_blocked_for_1405(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.post(
        "/api/v1/validation-runs",
        json={
            "period_start": PERIOD_1405_START,
            "standard_codes": ["15"],
            "data": BALANCED_DATA,
        },
        headers=_auth(world.key_validate_secret),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "STANDARD_SOURCE_BLOCKED"


def test_create_run_unknown_standard_404(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.post(
        "/api/v1/validation-runs",
        json={"standard_codes": ["99"], "data": BALANCED_DATA},
        headers=_auth(world.key_validate_secret),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "STANDARD_NOT_FOUND"


def test_create_run_bad_period_range_422(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.post(
        "/api/v1/validation-runs",
        json={
            "period_start": "2025-08-23",
            "period_end": "2025-08-22",
            "data": BALANCED_DATA,
        },
        headers=_auth(world.key_validate_secret),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# GET /validation-runs/{run_id}
# ---------------------------------------------------------------------------


def test_get_run_round_trip(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    created = _create_run(
        client,
        world.key_validate_secret,
        {
            "period_start": PERIOD_1404,
            "standard_codes": ["1"],
            "data": BALANCED_DATA,
        },
    )
    response = client.get(
        f"/api/v1/validation-runs/{created['run_id']}",
        headers=_auth(world.key_read_secret),
    )
    assert response.status_code == 200
    fetched = response.json()
    assert fetched["run_id"] == created["run_id"]
    assert fetched["overall_outcome"] == created["overall_outcome"]
    assert fetched["results"] == created["results"]
    assert fetched["coverage"] == created["coverage"]
    assert fetched["not_executed"] == created["not_executed"]


def test_get_run_unknown_404(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get(
        f"/api/v1/validation-runs/{uuid.uuid4()}",
        headers=_auth(world.key_read_secret),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "VALIDATION_RUN_NOT_FOUND"


def test_get_run_other_tenant_404(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    created = _create_run(
        client,
        world.key_validate_secret,
        {
            "period_start": PERIOD_1404,
            "standard_codes": ["1"],
            "data": BALANCED_DATA,
        },
    )
    response = client.get(
        f"/api/v1/validation-runs/{created['run_id']}",
        headers=_auth(world.key_other_read_secret),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "VALIDATION_RUN_NOT_FOUND"


# ---------------------------------------------------------------------------
# OpenAPI
# ---------------------------------------------------------------------------


def test_openapi_lists_standards_routes(settings: AppSettings) -> None:
    client = _test_client(settings)
    response = client.get("/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/api/v1/standards" in paths
    assert "/api/v1/standards/{standard_code}" in paths
    assert "/api/v1/validation-runs" in paths
    assert "/api/v1/validation-runs/{run_id}" in paths
