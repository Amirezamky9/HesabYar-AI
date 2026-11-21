"""REST security suite (US-701).

Proves the properties the threat model (docs/security/threat-model.md)
claims for the REST boundary, end to end through TestClient against
the real database:

- token replay after rotation: the old secret stops working
  immediately, the replacement works with the same grant;
- scope escalation: a ``reports:read`` key cannot call a
  ``banking:read`` (or ``banking:import``) endpoint, and vice versa;
- request-body size limit: oversized bodies get a 413 before any
  handler sees them (Content-Length and chunked paths), while
  legitimate large bodies pass;
- no secrets in logs or error responses over a full request cycle
  (success, failed auth, scope denial, wrong company, unknown
  company).

The basic 401/403/company matrix already lives in
``test_rest_auth.py`` (missing/invalid/expired/revoked keys,
per-scope enforcement, cross-tenant invisibility) and is not
duplicated here.
"""

from __future__ import annotations

import asyncio
import json
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
from starlette.types import Message, Receive, Scope, Send

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
from hesabyar.interfaces.rest.middleware import MAX_REQUEST_BODY_BYTES, RequestSizeLimitMiddleware

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
    company_b_id: str
    company_c_id: str  # belongs to the OTHER tenant
    key_reports_secret: str  # reports:read, unrestricted
    key_banking_secret: str  # banking:read, unrestricted
    key_rotatable_secret: str  # reports:read, unrestricted
    key_rotatable_id: str
    key_restricted_a_secret: str  # reports:read, restricted to company A
    key_revoked_secret: str


def _test_client(settings: AppSettings) -> TestClient:
    """App with test-only routes protected by the real dependencies."""
    app = create_app(settings=settings)

    @app.get("/api/v1/_test/reports")
    def _reports(
        client: AuthenticatedClient = Depends(require_scopes("reports:read")),
    ) -> dict[str, object]:
        return {"client_id": client.client_id, "scopes": sorted(client.scopes)}

    @app.get("/api/v1/_test/banking")
    def _banking(
        client: AuthenticatedClient = Depends(require_scopes("banking:read")),
    ) -> dict[str, object]:
        return {"client_id": client.client_id, "scopes": sorted(client.scopes)}

    @app.post("/api/v1/_test/banking/import")
    def _banking_import(
        client: AuthenticatedClient = Depends(require_scopes("banking:import")),
    ) -> dict[str, object]:
        return {"client_id": client.client_id, "scopes": sorted(client.scopes)}

    @app.get("/api/v1/_test/companies/{company_id}")
    def _company(
        company_id: str,
        client: AuthenticatedClient = Depends(require_company_access),
    ) -> dict[str, object]:
        return {"company_id": company_id, "client_id": client.client_id}

    @app.post("/api/v1/_test/echo")
    def _echo(
        body: dict,
        _client: AuthenticatedClient = Depends(require_scopes("reports:read")),
    ) -> dict[str, object]:
        return {"length": len(json.dumps(body))}

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
            id=str(uuid.uuid4()), slug=f"rest-sec-{uuid.uuid4().hex[:8]}", name="Tenant 1"
        )
        other_tenant = TenantModel(
            id=str(uuid.uuid4()), slug=f"rest-sec-{uuid.uuid4().hex[:8]}", name="Tenant 2"
        )
        session.add_all([tenant, other_tenant])
        session.flush()
        client = ApiClientModel(
            id=str(uuid.uuid4()), tenant_id=tenant.id, name="rest-security client"
        )
        session.add(client)
        session.flush()

        def _company(tenant_id: str, external_id: str) -> EngineCompanyModel:
            connection = EngineConnectionModel(
                id=str(uuid.uuid4()),
                name=f"conn-{uuid.uuid4().hex[:8]}",
                engine="tryton",
                base_url="http://localhost:8000",
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

        company_a = _company(tenant.id, "701")
        company_b = _company(tenant.id, "702")
        company_c = _company(other_tenant.id, "703")

        def _key(scopes: list[str], **kwargs: object):
            return service.issue_key(
                session,
                client=client,
                name="rest-security key",
                scopes=scopes,
                installation_id=INSTALLATION_ID,
                **kwargs,  # type: ignore[arg-type]
            )

        key_reports = _key(["reports:read"])
        key_banking = _key(["banking:read"])
        key_rotatable = _key(["reports:read"])
        key_restricted_a = _key(["reports:read"], engine_company_id=company_a.id)
        key_revoked = _key(["reports:read"])
        service.revoke_key(session, key=key_revoked.record, reason="test")
        session.commit()
        yield World(
            tenant_id=tenant.id,
            other_tenant_id=other_tenant.id,
            company_a_id=company_a.id,
            company_b_id=company_b.id,
            company_c_id=company_c.id,
            key_reports_secret=key_reports.secret,
            key_banking_secret=key_banking.secret,
            key_rotatable_secret=key_rotatable.secret,
            key_rotatable_id=key_rotatable.record.id,
            key_restricted_a_secret=key_restricted_a.secret,
            key_revoked_secret=key_revoked.secret,
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


def _all_secrets(world: World) -> list[str]:
    return [
        world.key_reports_secret,
        world.key_banking_secret,
        world.key_rotatable_secret,
        world.key_restricted_a_secret,
        world.key_revoked_secret,
    ]


# ---------------------------------------------------------------------------
# Token replay after rotation
# ---------------------------------------------------------------------------


def test_rotated_key_stops_working_immediately(
    settings: AppSettings, world: World, engine: Engine
) -> None:
    """The old secret 401s the moment rotation commits; the new one works."""
    session = sessionmaker(bind=engine)()
    try:
        service = ApiKeyService(ApiKeyCrypto(settings.auth.api_key_pepper))
        record = session.get(ApiKeyModel, world.key_rotatable_id)
        assert record is not None
        rotated = service.rotate_key(session, key=record)
        session.commit()
        new_secret = rotated.secret
    finally:
        session.close()

    assert new_secret != world.key_rotatable_secret
    client = _test_client(settings)
    old = client.get("/api/v1/_test/reports", headers=_auth(world.key_rotatable_secret))
    assert old.status_code == 401
    assert old.json()["code"] == "UNAUTHORIZED"
    assert old.json()["detail"] == "A valid API key is required."
    # The replacement inherits the grant: same scopes, same access.
    new = client.get("/api/v1/_test/reports", headers=_auth(new_secret))
    assert new.status_code == 200
    assert new.json()["scopes"] == ["reports:read"]
    # And the old secret stays dead (no grace window, no replay).
    again = client.get("/api/v1/_test/reports", headers=_auth(world.key_rotatable_secret))
    assert again.status_code == 401


# ---------------------------------------------------------------------------
# Scope escalation (the AC's named pair: reports:read vs banking:read)
# ---------------------------------------------------------------------------


def test_reports_key_cannot_call_banking_endpoints(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    read = client.get("/api/v1/_test/banking", headers=_auth(world.key_reports_secret))
    assert read.status_code == 403
    assert read.json()["code"] == "FORBIDDEN"
    assert "banking:read" in read.json()["detail"]
    write = client.post("/api/v1/_test/banking/import", headers=_auth(world.key_reports_secret))
    assert write.status_code == 403
    assert "banking:import" in write.json()["detail"]


def test_banking_key_cannot_call_reports_endpoints(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    read = client.get("/api/v1/_test/reports", headers=_auth(world.key_banking_secret))
    assert read.status_code == 403
    assert read.json()["code"] == "FORBIDDEN"
    assert "reports:read" in read.json()["detail"]


def test_each_key_passes_its_own_endpoints(settings: AppSettings, world: World) -> None:
    """Control: the denials above are scope denials, not auth failures."""
    client = _test_client(settings)
    assert (
        client.get("/api/v1/_test/reports", headers=_auth(world.key_reports_secret)).status_code
        == 200
    )
    assert (
        client.get("/api/v1/_test/banking", headers=_auth(world.key_banking_secret)).status_code
        == 200
    )


# ---------------------------------------------------------------------------
# Request-body size limit (413)
# ---------------------------------------------------------------------------


async def _tiny_echo_app(scope: Scope, receive: Receive, send: Send) -> None:
    """Minimal ASGI app that reads the whole body and reports its length."""
    body = b""
    while True:
        message = await receive()
        body += message.get("body", b"")
        if not message.get("more_body", False):
            break
    payload = json.dumps({"length": len(body)}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


def test_oversized_body_gets_413_before_the_handler() -> None:
    """Content-Length path: the handler never sees the body."""
    app = RequestSizeLimitMiddleware(_tiny_echo_app, max_bytes=1024)
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/", content=b"x" * 2048)
    assert response.status_code == 413
    assert response.headers["Content-Type"].startswith("application/problem+json")
    body = response.json()
    assert body["code"] == "REQUEST_BODY_TOO_LARGE"
    assert body["status"] == 413
    assert "1024" in body["detail"]
    # A body under the limit passes through untouched.
    ok = client.post("/", content=b"x" * 512)
    assert ok.status_code == 200
    assert ok.json() == {"length": 512}


def test_chunked_body_over_the_limit_gets_413() -> None:
    """Streaming path: no Content-Length, chunks add up past the cap."""

    async def _run() -> list[Message]:
        messages: list[Message] = []
        chunks = [b"x" * 600, b"y" * 600]
        iterator = iter(chunks)

        async def _receive() -> Message:
            try:
                return {"type": "http.request.body", "body": next(iterator), "more_body": True}
            except StopIteration:
                return {"type": "http.request.body", "body": b"", "more_body": False}

        async def _send(message: Message) -> None:
            messages.append(message)

        scope: Scope = {"type": "http", "method": "POST", "path": "/", "headers": []}
        await RequestSizeLimitMiddleware(_tiny_echo_app, max_bytes=1024)(
            scope, _receive, _send
        )
        return messages

    messages = asyncio.run(_run())
    assert messages[0]["type"] == "http.response.start"
    assert messages[0]["status"] == 413
    assert json.loads(messages[1]["body"])["code"] == "REQUEST_BODY_TOO_LARGE"


def test_production_limit_allows_legitimate_large_bodies(
    settings: AppSettings, world: World
) -> None:
    """The 20 MiB default does not break real traffic (a 1 MiB JSON body)."""
    assert MAX_REQUEST_BODY_BYTES == 20 * 1024 * 1024
    client = _test_client(settings)
    response = client.post(
        "/api/v1/_test/echo",
        json={"pad": "x" * (1024 * 1024)},
        headers=_auth(world.key_reports_secret),
    )
    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# No secrets in logs or error responses over a full request cycle
# ---------------------------------------------------------------------------


def test_no_secrets_in_logs_or_responses_over_a_full_cycle(
    settings: AppSettings, world: World, caplog: pytest.LogCaptureFixture
) -> None:
    client = _test_client(settings)
    responses = []
    with caplog.at_level(logging.INFO, logger="hesabyar"):
        # Success.
        responses.append(
            client.get("/api/v1/_test/reports", headers=_auth(world.key_reports_secret))
        )
        # Failed auth: revoked, unknown, malformed, missing.
        responses.append(
            client.get("/api/v1/_test/reports", headers=_auth(world.key_revoked_secret))
        )
        responses.append(
            client.get(
                "/api/v1/_test/reports",
                headers=_auth("hya_live_no-such-key-000000000000"),
            )
        )
        responses.append(client.get("/api/v1/_test/reports", headers=_auth("not-a-key")))
        responses.append(client.get("/api/v1/_test/reports"))
        # Scope denial.
        responses.append(
            client.get("/api/v1/_test/banking", headers=_auth(world.key_reports_secret))
        )
        # Wrong company (403) and unknown company (404).
        responses.append(
            client.get(
                f"/api/v1/_test/companies/{world.company_b_id}",
                headers=_auth(world.key_restricted_a_secret),
            )
        )
        responses.append(
            client.get(
                f"/api/v1/_test/companies/{uuid.uuid4()}",
                headers=_auth(world.key_reports_secret),
            )
        )
        # Another tenant's company (404 — invisible, not forbidden).
        responses.append(
            client.get(
                f"/api/v1/_test/companies/{world.company_c_id}",
                headers=_auth(world.key_reports_secret),
            )
        )
    assert caplog.text, "expected log output from the request cycle"
    assert responses[0].status_code == 200
    assert {r.status_code for r in responses} >= {200, 401, 403, 404}
    for secret in _all_secrets(world):
        assert secret not in caplog.text
        for response in responses:
            assert secret not in response.text
        # The secret must not appear even partially disguised: the full
        # random part (after the scheme prefix) is the sensitive half.
        assert secret[len("hya_live_"):] not in caplog.text
