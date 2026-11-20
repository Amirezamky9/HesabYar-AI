"""Tests for the tax-policy registry + decision REST endpoints (US-402).

Everything is real — the FastAPI app, scoped API-key auth, the
PostgreSQL database — except the engine, which these endpoints never
touch (decisions resolve against the registry). The live Tryton server
is NOT needed here.

The registry is seeded once per module (committed reference data,
idempotent); provisioned tenants/keys are removed in teardown by
deleting the tenant (cascades to clients/keys/decisions).
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
from hesabyar.infrastructure.persistence.postgres.seed_tax_policy import (
    seed_tax_policy,
)
from hesabyar.interfaces.rest.app import create_app

TEST_DB_URL = os.getenv(
    "HESABYAR_DATABASE_URL",
    "postgresql+psycopg://hesabyar:hesabyar_pass@hesabyar-postgres:5432/hesabyar_test",
)

INSTALLATION_ID = "00000000-0000-0000-0000-000000000402"

# A 1405 date and the effective-date boundaries of the seeded versions.
MID_1405 = "2026-09-20"
EFFECTIVE_FROM = "2026-03-21"
EFFECTIVE_TO = "2027-03-20"
DAY_BEFORE_1405 = "2026-03-20"
HISTORICAL = "2024-06-15"


@dataclass
class World:
    """Provisioned tenant and keys for one test."""

    tenant_id: str
    other_tenant_id: str
    key_read_secret: str
    key_decide_secret: str
    key_company_secret: str
    key_other_read_secret: str


@pytest.fixture(scope="module")
def engine() -> Generator[Engine, None, None]:
    eng = create_engine(TEST_DB_URL, echo=False)
    Base.metadata.create_all(eng)
    # Seed the registry once per module (idempotent reference data).
    session = sessionmaker(bind=eng)()
    try:
        seed_tax_policy(session)
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
        tenant = TenantModel(id=str(uuid.uuid4()), slug=f"rest-402-{uuid.uuid4().hex[:8]}", name="Tenant 1")
        other_tenant = TenantModel(id=str(uuid.uuid4()), slug=f"rest-402-{uuid.uuid4().hex[:8]}", name="Tenant 2")
        session.add_all([tenant, other_tenant])
        session.flush()
        client = ApiClientModel(id=str(uuid.uuid4()), tenant_id=tenant.id, name="rest-402 client")
        other_client = ApiClientModel(id=str(uuid.uuid4()), tenant_id=other_tenant.id, name="rest-402 other client")
        session.add_all([client, other_client])
        session.flush()

        def _key(client_row: ApiClientModel, scopes: list[str]):
            return service.issue_key(
                session,
                client=client_row,
                name="rest-402 key",
                scopes=scopes,
                installation_id=INSTALLATION_ID,
            )

        key_read = _key(client, ["tax:read"])
        key_decide = _key(client, ["tax:read", "tax:decide"])
        key_company = _key(client, ["company:read"])
        key_other_read = _key(other_client, ["tax:read"])
        session.commit()
        tenant_ids = [tenant.id, other_tenant.id]
        yield World(
            tenant_id=tenant.id,
            other_tenant_id=other_tenant.id,
            key_read_secret=key_read.secret,
            key_decide_secret=key_decide.secret,
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
    """Real app (tax-policy endpoints need no engine adapter)."""
    app = create_app(settings=settings)
    return TestClient(app, raise_server_exceptions=False)


def _auth(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


def _resolve_decision(
    client: TestClient,
    secret: str,
    body: dict,
) -> dict:
    response = client.post("/api/v1/tax-policy/decisions", json=body, headers=_auth(secret))
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------------------
# GET /tax-policy/sources
# ---------------------------------------------------------------------------


def test_list_sources_requires_auth(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get("/api/v1/tax-policy/sources")
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"


def test_list_sources_requires_scope(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get("/api/v1/tax-policy/sources", headers=_auth(world.key_company_secret))
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


def test_list_sources_returns_three(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get("/api/v1/tax-policy/sources", headers=_auth(world.key_read_secret))
    assert response.status_code == 200
    body = response.json()
    assert body["next_cursor"] is None
    assert [item["source_key"] for item in body["items"]] == [
        "tasnim-art100-announcement-1405-02-28",
        "tasnim-art100-implementation-1405-06-12",
        "tasnim-budget-1404-11-25",
    ]
    assert [item["versions_count"] for item in body["items"]] == [0, 1, 4]
    assert all(item["sha256"] is None for item in body["items"])


def test_list_sources_paginates(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    first = client.get("/api/v1/tax-policy/sources?limit=2", headers=_auth(world.key_read_secret)).json()
    assert len(first["items"]) == 2
    assert first["next_cursor"] is not None
    second = client.get(
        f"/api/v1/tax-policy/sources?limit=2&cursor={first['next_cursor']}",
        headers=_auth(world.key_read_secret),
    ).json()
    assert len(second["items"]) == 1
    assert second["next_cursor"] is None
    assert second["items"][0]["source_key"] == "tasnim-budget-1404-11-25"


def test_get_source_returns_versions(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get(
        "/api/v1/tax-policy/sources/tasnim-art100-implementation-1405-06-12",
        headers=_auth(world.key_read_secret),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["title"].startswith("Article 100 current implementation")
    assert len(body["versions"]) == 1
    version = body["versions"][0]
    assert version["rule_key"] == "art100_flat_tax_ceiling"
    assert version["parameter_value"] == "720000000000"
    assert version["performance_year"] == 1404
    assert version["filing_year"] == 1405


def test_get_source_unknown_key_404(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get("/api/v1/tax-policy/sources/no-such-source", headers=_auth(world.key_read_secret))
    assert response.status_code == 404
    assert response.json()["code"] == "TAX_SOURCE_NOT_FOUND"


# ---------------------------------------------------------------------------
# GET /tax-policy/versions
# ---------------------------------------------------------------------------


def test_list_versions_returns_five(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get("/api/v1/tax-policy/versions", headers=_auth(world.key_read_secret))
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 5
    assert [item["rule_key"] for item in body["items"]] == [
        "art100_flat_tax_ceiling",
        "art101_exemption",
        "art84_salary_exemption",
        "growth_threshold_art105_131",
        "vat_general_rate",
    ]
    vat = next(item for item in body["items"] if item["rule_key"] == "vat_general_rate")
    assert vat["parameter_value"] == "10"
    assert vat["value_unit"] == "percent"
    assert vat["verification_status"] == "CORROBORATED_CURRENT"
    assert vat["effective_from"] == EFFECTIVE_FROM
    assert vat["effective_to"] == EFFECTIVE_TO


def test_list_versions_filters(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    headers = _auth(world.key_read_secret)

    response = client.get("/api/v1/tax-policy/versions?rule_key=art101_exemption", headers=headers)
    assert [item["rule_key"] for item in response.json()["items"]] == ["art101_exemption"]

    response = client.get("/api/v1/tax-policy/versions?verification_status=VERIFIED_TEXT", headers=headers)
    assert response.json()["items"] == []

    response = client.get(f"/api/v1/tax-policy/versions?effective_on={MID_1405}", headers=headers)
    assert len(response.json()["items"]) == 5

    response = client.get(f"/api/v1/tax-policy/versions?effective_on={DAY_BEFORE_1405}", headers=headers)
    assert response.json()["items"] == []

    response = client.get("/api/v1/tax-policy/versions?performance_year=1404", headers=headers)
    assert {item["rule_key"] for item in response.json()["items"]} == {
        "art100_flat_tax_ceiling",
        "growth_threshold_art105_131",
    }

    response = client.get("/api/v1/tax-policy/versions?rule_year=1404", headers=headers)
    assert response.json()["items"] == []


def test_list_versions_rejects_unknown_filter(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get(
        "/api/v1/tax-policy/versions?verification_status=MADE_UP",
        headers=_auth(world.key_read_secret),
    )
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_FILTER"


def test_list_versions_rejects_unknown_param(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get("/api/v1/tax-policy/versions?bogus=1", headers=_auth(world.key_read_secret))
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_FILTER"


def test_get_version_returns_version_with_source(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    headers = _auth(world.key_read_secret)
    listed = client.get("/api/v1/tax-policy/versions?rule_key=vat_general_rate", headers=headers).json()
    version_id = listed["items"][0]["id"]
    response = client.get(f"/api/v1/tax-policy/versions/{version_id}", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["version"]["rule_key"] == "vat_general_rate"
    assert body["version"]["rule_year"] == 1405
    assert body["version"]["corroboration_urls"] == ["https://www.tasnimnews.ir/fa/news/1404/11/25/3516503/"]
    assert body["source"]["source_key"] == "tasnim-budget-1404-11-25"
    assert body["source"]["authority"] == "Tasnim News Agency"


def test_get_version_unknown_id_404(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get(f"/api/v1/tax-policy/versions/{uuid.uuid4()}", headers=_auth(world.key_read_secret))
    assert response.status_code == 404
    assert response.json()["code"] == "TAX_POLICY_NOT_FOUND"


# ---------------------------------------------------------------------------
# POST /tax-policy/decisions
# ---------------------------------------------------------------------------


def test_resolve_decision_requires_decide_scope(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.post(
        "/api/v1/tax-policy/decisions",
        json={"rule_key": "vat_general_rate", "decision_date": MID_1405},
        headers=_auth(world.key_read_secret),
    )
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


def test_resolve_reference_decision(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    body = _resolve_decision(
        client,
        world.key_decide_secret,
        {"rule_key": "vat_general_rate", "decision_date": MID_1405, "company_id": "company-1"},
    )
    assert body["rule_key"] == "vat_general_rate"
    assert body["decision_date"] == MID_1405
    assert body["company_id"] == "company-1"
    assert body["usage_context"] == "reference"
    assert body["parameter_value"] == "10"
    assert body["value_unit"] == "percent"
    assert body["version_id"] == "1405"
    assert body["verification_status"] == "CORROBORATED_CURRENT"
    assert body["source_key"] == "tasnim-budget-1404-11-25"
    assert body["policy_version_id"] is not None


def test_resolve_decision_on_effective_boundaries(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    first = _resolve_decision(
        client,
        world.key_decide_secret,
        {"rule_key": "art84_salary_exemption", "decision_date": EFFECTIVE_FROM},
    )
    assert first["parameter_value"] == "4800000000"
    last = _resolve_decision(
        client,
        world.key_decide_secret,
        {"rule_key": "art101_exemption", "decision_date": EFFECTIVE_TO},
    )
    assert last["parameter_value"] == "2800000000"


def test_resolve_decision_no_covering_version_404(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.post(
        "/api/v1/tax-policy/decisions",
        json={"rule_key": "vat_general_rate", "decision_date": HISTORICAL},
        headers=_auth(world.key_decide_secret),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "TAX_POLICY_NOT_FOUND"


def test_resolve_decision_unknown_rule_404(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.post(
        "/api/v1/tax-policy/decisions",
        json={"rule_key": "no_such_rule", "decision_date": MID_1405},
        headers=_auth(world.key_decide_secret),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "TAX_POLICY_NOT_FOUND"


def test_resolve_production_decision_is_blocked(settings: AppSettings, world: World) -> None:
    """The gate survives the REST boundary: production + CORROBORATED_CURRENT → 422."""
    client = _test_client(settings)
    response = client.post(
        "/api/v1/tax-policy/decisions",
        json={
            "rule_key": "vat_general_rate",
            "decision_date": MID_1405,
            "usage_context": "production",
        },
        headers=_auth(world.key_decide_secret),
    )
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "TAX_POLICY_SOURCE_BLOCKED"
    assert "official source" in body["detail"]
    assert "pin" in body["detail"].lower()


def test_resolve_decision_bad_usage_context_422(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.post(
        "/api/v1/tax-policy/decisions",
        json={
            "rule_key": "vat_general_rate",
            "decision_date": MID_1405,
            "usage_context": "whenever",
        },
        headers=_auth(world.key_decide_secret),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# GET /tax-policy/decisions/{decision_id}
# ---------------------------------------------------------------------------


def test_get_decision_roundtrip(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    created = _resolve_decision(
        client,
        world.key_decide_secret,
        {"rule_key": "art100_flat_tax_ceiling", "decision_date": MID_1405},
    )
    response = client.get(f"/api/v1/tax-policy/decisions/{created['id']}", headers=_auth(world.key_read_secret))
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == created["id"]
    assert body["parameter_value"] == "720000000000"
    assert body["value_unit"] == "irr"


def test_get_decision_other_tenant_404(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    created = _resolve_decision(
        client,
        world.key_decide_secret,
        {"rule_key": "vat_general_rate", "decision_date": MID_1405},
    )
    response = client.get(
        f"/api/v1/tax-policy/decisions/{created['id']}",
        headers=_auth(world.key_other_read_secret),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "TAX_DECISION_NOT_FOUND"


def test_get_decision_unknown_id_404(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get(f"/api/v1/tax-policy/decisions/{uuid.uuid4()}", headers=_auth(world.key_read_secret))
    assert response.status_code == 404
    assert response.json()["code"] == "TAX_DECISION_NOT_FOUND"


# ---------------------------------------------------------------------------
# OpenAPI
# ---------------------------------------------------------------------------


def test_openapi_lists_tax_policy_paths(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get("/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]
    for path in (
        "/api/v1/tax-policy/sources",
        "/api/v1/tax-policy/sources/{source_key}",
        "/api/v1/tax-policy/versions",
        "/api/v1/tax-policy/versions/{version_id}",
        "/api/v1/tax-policy/decisions",
        "/api/v1/tax-policy/decisions/{decision_id}",
    ):
        assert path in paths, f"{path} missing from OpenAPI"
