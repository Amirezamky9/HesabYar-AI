"""Tests for the FastAPI auth dependencies (US-103): require_scopes,
require_company_access and get_authenticated_client, exercised end to end
through TestClient against the real database.

Test-only routes are mounted on a fresh app per test under
``/api/v1/_test/`` — they exist only in this module. Provisioned rows are
committed (the app's per-request sessions must see them) and removed in
teardown by deleting the tenant (cascades to clients/keys/companies).
"""

from __future__ import annotations

import datetime
import logging
import os
import uuid
from collections.abc import Generator
from dataclasses import dataclass

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from hesabyar.infrastructure.auth import ApiKeyCrypto, ApiKeyService
from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.infrastructure.config import AppSettings
from hesabyar.infrastructure.persistence.postgres.models import (
    ApiClientModel,
    ApiKeyModel,
    Base,
    EngineCompanyModel,
    EngineConnectionModel,
    TenantModel,
)
from hesabyar.interfaces.rest.app import create_app
from hesabyar.interfaces.rest.dependencies import require_company_access, require_scopes

TEST_DB_URL = os.getenv(
    "HESABYAR_DATABASE_URL",
    "postgresql+psycopg://hesabyar:hesabyar_pass@hesabyar-postgres:5432/hesabyar_test",
)

INSTALLATION_ID = "00000000-0000-0000-0000-000000000002"


@dataclass
class World:
    """Provisioned tenants, companies and keys for one test."""

    tenant_id: str
    other_tenant_id: str
    company_a_id: str
    company_a_external_id: str
    company_b_id: str
    company_c_id: str  # belongs to the OTHER tenant
    key_full_secret: str  # company:read + account:read, unrestricted
    key_company_secret: str  # company:read only, unrestricted
    key_restricted_a_secret: str  # company:read, restricted to company A
    key_revoked_secret: str
    key_expired_secret: str
    key_full_id: str


def _test_client(settings: AppSettings) -> TestClient:
    """App with test-only routes protected by the real dependencies."""
    app = create_app(settings=settings)

    @app.get("/api/v1/_test/companies")
    def _companies(
        client: AuthenticatedClient = Depends(require_scopes("company:read")),
    ) -> dict[str, object]:
        return {"client_id": client.client_id, "scopes": sorted(client.scopes)}

    @app.get("/api/v1/_test/accounts")
    def _accounts(
        client: AuthenticatedClient = Depends(require_scopes("account:read")),
    ) -> dict[str, object]:
        return {"client_id": client.client_id, "scopes": sorted(client.scopes)}

    @app.get("/api/v1/_test/companies/{company_id}")
    def _company(
        company_id: str,
        client: AuthenticatedClient = Depends(require_company_access),
    ) -> dict[str, object]:
        return {"company_id": company_id, "client_id": client.client_id}

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(scope="module")
def engine() -> Generator[Engine, None, None]:
    eng = create_engine(TEST_DB_URL, echo=False)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def settings() -> AppSettings:
    """Fresh settings (dev pepper) — the same pepper the app dependency uses."""
    return AppSettings()


@pytest.fixture()
def world(engine: Engine, settings: AppSettings) -> Generator[World, None, None]:
    """Provision two tenants, three companies and five keys; clean up after."""
    session = sessionmaker(bind=engine)()
    service = ApiKeyService(ApiKeyCrypto(settings.auth.api_key_pepper))
    tenant: TenantModel | None = None
    other_tenant: TenantModel | None = None
    connection_ids: list[str] = []
    try:
        tenant = TenantModel(
            id=str(uuid.uuid4()), slug=f"rest-auth-{uuid.uuid4().hex[:8]}", name="Tenant 1"
        )
        other_tenant = TenantModel(
            id=str(uuid.uuid4()), slug=f"rest-auth-{uuid.uuid4().hex[:8]}", name="Tenant 2"
        )
        session.add_all([tenant, other_tenant])
        session.flush()
        client = ApiClientModel(
            id=str(uuid.uuid4()), tenant_id=tenant.id, name="rest-auth client"
        )
        session.add(client)
        session.flush()

        def _company(tenant_id: str, external_id: str) -> EngineCompanyModel:
            # One company per (tenant, connection) — unique constraint.
            connection = EngineConnectionModel(
                id=str(uuid.uuid4()),
                name=f"conn-{uuid.uuid4().hex[:8]}",
                engine="tryton",
                base_url="http://localhost:8000",
                # (engine, database_name) is unique per connection.
                database_name=f"hesabyar_tryton_test_{uuid.uuid4().hex[:8]}",
            )
            session.add(connection)
            session.flush()
            connection_ids.append(connection.id)
            row = EngineCompanyModel(
                id=str(uuid.uuid4()),
                tenant_id=tenant_id,
                engine_connection_id=connection.id,
                external_company_id=external_id,
                company_name=f"Company {external_id}",
            )
            session.add(row)
            session.flush()
            return row

        company_a = _company(tenant.id, "201")
        company_b = _company(tenant.id, "202")
        company_c = _company(other_tenant.id, "203")

        def _key(scopes: list[str], **kwargs: object):
            return service.issue_key(
                session,
                client=client,
                name="rest-auth key",
                scopes=scopes,
                installation_id=INSTALLATION_ID,
                **kwargs,  # type: ignore[arg-type]
            )

        key_full = _key(["company:read", "account:read"])
        key_company = _key(["company:read"])
        key_restricted_a = _key(["company:read"], engine_company_id=company_a.id)
        key_revoked = _key(["company:read"])
        service.revoke_key(session, key=key_revoked.record, reason="test")
        key_expired = _key(
            ["company:read"],
            expires_at=datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=1),
        )
        session.commit()
        yield World(
            tenant_id=tenant.id,
            other_tenant_id=other_tenant.id,
            company_a_id=company_a.id,
            company_a_external_id=company_a.external_company_id,
            company_b_id=company_b.id,
            company_c_id=company_c.id,
            key_full_secret=key_full.secret,
            key_company_secret=key_company.secret,
            key_restricted_a_secret=key_restricted_a.secret,
            key_revoked_secret=key_revoked.secret,
            key_expired_secret=key_expired.secret,
            key_full_id=key_full.record.id,
        )
    finally:
        session.close()
        cleanup = sessionmaker(bind=engine)()
        try:
            tenant_ids = [t.id for t in (tenant, other_tenant) if t is not None]
            if tenant_ids:
                cleanup.query(TenantModel).filter(
                    TenantModel.id.in_(tenant_ids)
                ).delete(synchronize_session=False)
            if connection_ids:
                cleanup.query(EngineConnectionModel).filter(
                    EngineConnectionModel.id.in_(connection_ids)
                ).delete(synchronize_session=False)
            cleanup.commit()
        finally:
            cleanup.close()


def _auth(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


# ---------------------------------------------------------------------------
# 401 — missing / invalid authentication
# ---------------------------------------------------------------------------


def test_missing_auth_returns_401_problem(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get("/api/v1/_test/companies")
    assert response.status_code == 401
    assert response.headers["Content-Type"].startswith("application/problem+json")
    body = response.json()
    assert body["code"] == "UNAUTHORIZED"
    assert body["status"] == 401
    assert body["detail"] == "A valid API key is required."
    assert body["request_id"] == response.headers["X-Request-ID"]


def test_wrong_scheme_returns_401(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get(
        "/api/v1/_test/companies",
        headers={"Authorization": f"Token {world.key_full_secret}"},
    )
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"


def test_unknown_key_returns_401(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    unknown = ApiKeyCrypto(settings.auth.api_key_pepper).generate()
    response = client.get("/api/v1/_test/companies", headers=_auth(unknown.secret))
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"
    # The generic detail gives no hint about why the key failed.
    assert response.json()["detail"] == "A valid API key is required."


def test_malformed_key_returns_401(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get("/api/v1/_test/companies", headers=_auth("not-a-key"))
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"


def test_revoked_and_expired_keys_return_401(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    for secret in (world.key_revoked_secret, world.key_expired_secret):
        response = client.get("/api/v1/_test/companies", headers=_auth(secret))
        assert response.status_code == 401
        assert response.json()["code"] == "UNAUTHORIZED"
        assert response.json()["detail"] == "A valid API key is required."


# ---------------------------------------------------------------------------
# 403 — insufficient scope
# ---------------------------------------------------------------------------


def test_valid_key_with_scope_returns_200(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get("/api/v1/_test/companies", headers=_auth(world.key_full_secret))
    assert response.status_code == 200
    body = response.json()
    assert body["scopes"] == ["account:read", "company:read"]
    assert body["client_id"]


def test_each_scope_is_enforced_independently(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    # company:read key: companies OK, accounts forbidden.
    ok = client.get("/api/v1/_test/companies", headers=_auth(world.key_company_secret))
    assert ok.status_code == 200
    forbidden = client.get("/api/v1/_test/accounts", headers=_auth(world.key_company_secret))
    assert forbidden.status_code == 403
    body = forbidden.json()
    assert body["code"] == "FORBIDDEN"
    assert body["status"] == 403
    assert "account:read" in body["detail"]
    assert forbidden.headers["Content-Type"].startswith("application/problem+json")
    assert forbidden.headers.get("X-Request-ID")


def test_key_with_both_scopes_passes_both(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    for path in ("/api/v1/_test/companies", "/api/v1/_test/accounts"):
        response = client.get(path, headers=_auth(world.key_full_secret))
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Company restriction
# ---------------------------------------------------------------------------


def test_restricted_key_can_access_its_own_company(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get(
        f"/api/v1/_test/companies/{world.company_a_id}",
        headers=_auth(world.key_restricted_a_secret),
    )
    assert response.status_code == 200
    assert response.json()["company_id"] == world.company_a_id


def test_restricted_key_cannot_access_another_company(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get(
        f"/api/v1/_test/companies/{world.company_b_id}",
        headers=_auth(world.key_restricted_a_secret),
    )
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"
    assert "different company" in response.json()["detail"]


def test_company_resolution_also_accepts_external_id(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get(
        f"/api/v1/_test/companies/{world.company_a_external_id}",
        headers=_auth(world.key_restricted_a_secret),
    )
    assert response.status_code == 200


def test_unrestricted_key_can_access_any_company(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    for company_id in (world.company_a_id, world.company_b_id):
        response = client.get(
            f"/api/v1/_test/companies/{company_id}",
            headers=_auth(world.key_full_secret),
        )
        assert response.status_code == 200


def test_unknown_company_returns_404(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get(
        f"/api/v1/_test/companies/{uuid.uuid4()}",
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "COMPANY_NOT_FOUND"


def test_company_of_another_tenant_is_invisible(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    # Company C exists, but belongs to another tenant: it must look
    # exactly like a company that does not exist (404, not 403).
    response = client.get(
        f"/api/v1/_test/companies/{world.company_c_id}",
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "COMPANY_NOT_FOUND"


# ---------------------------------------------------------------------------
# Last-used + never-in-logs / never-in-responses
# ---------------------------------------------------------------------------


def test_successful_request_records_last_used(
    settings: AppSettings, world: World, engine: Engine
) -> None:
    client = _test_client(settings)
    response = client.get("/api/v1/_test/companies", headers=_auth(world.key_full_secret))
    assert response.status_code == 200
    session = sessionmaker(bind=engine)()
    try:
        row = session.get(ApiKeyModel, world.key_full_id)
        assert row is not None
        assert row.last_used_at is not None
    finally:
        session.close()


def test_keys_never_appear_in_error_responses(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    responses = [
        client.get("/api/v1/_test/companies"),
        client.get("/api/v1/_test/companies", headers=_auth(world.key_revoked_secret)),
        client.get("/api/v1/_test/companies", headers=_auth(world.key_expired_secret)),
        client.get("/api/v1/_test/accounts", headers=_auth(world.key_company_secret)),
        client.get(
            f"/api/v1/_test/companies/{world.company_b_id}",
            headers=_auth(world.key_restricted_a_secret),
        ),
    ]
    for response in responses:
        assert response.status_code in (401, 403)
        for secret in (
            world.key_full_secret,
            world.key_company_secret,
            world.key_restricted_a_secret,
            world.key_revoked_secret,
            world.key_expired_secret,
        ):
            assert secret not in response.text


def test_keys_never_appear_in_logs(
    settings: AppSettings, world: World, caplog: pytest.LogCaptureFixture
) -> None:
    client = _test_client(settings)
    with caplog.at_level(logging.INFO, logger="hesabyar"):
        client.get("/api/v1/_test/companies", headers=_auth(world.key_full_secret))
        client.get("/api/v1/_test/companies", headers=_auth(world.key_revoked_secret))
        client.get("/api/v1/_test/companies")
    assert caplog.text
    for secret in (
        world.key_full_secret,
        world.key_company_secret,
        world.key_restricted_a_secret,
        world.key_revoked_secret,
        world.key_expired_secret,
    ):
        assert secret not in caplog.text


# ---------------------------------------------------------------------------
# OpenAPI documents the Bearer scheme
# ---------------------------------------------------------------------------


def test_openapi_documents_api_key_security(settings: AppSettings) -> None:
    client = _test_client(settings)
    response = client.get("/openapi.json")
    assert response.status_code == 200
    document = response.json()
    schemes = document["components"]["securitySchemes"]
    assert schemes["HesabYar API key"]["type"] == "http"
    assert schemes["HesabYar API key"]["scheme"] == "bearer"
    operation = document["paths"]["/api/v1/_test/companies"]["get"]
    assert {"HesabYar API key": []} in operation.get("security", [])


