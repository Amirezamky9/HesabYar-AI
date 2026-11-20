"""Tests for the audit-trail query endpoints (US-205).

Audit events live in HesabYar's own database, so no engine is needed:
events are seeded directly into ``audit_events`` with explicit
timestamps, and everything else is real — the FastAPI app, scoped
API-key auth, and the PostgreSQL database.

Provisioned rows are committed (the app's per-request sessions must see
them) and removed in teardown by deleting the tenant (cascades to
clients/keys/companies/audit events).
"""

from __future__ import annotations

import base64
import datetime
import json
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
    AuditEventModel,
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

INSTALLATION_ID = "00000000-0000-0000-0000-000000000008"

ENGINE_A = "101"
ENGINE_B = "102"

T0 = datetime.datetime(2026, 11, 17, 10, 0, tzinfo=datetime.UTC)
T1 = datetime.datetime(2026, 11, 17, 11, 0, tzinfo=datetime.UTC)
T2 = datetime.datetime(2026, 11, 17, 12, 0, tzinfo=datetime.UTC)
T3 = datetime.datetime(2026, 11, 17, 13, 0, tzinfo=datetime.UTC)


@dataclass
class World:
    """Provisioned tenant, companies and keys for one test."""

    tenant_id: str
    company_a_id: str  # engine_companies row id for engine 101
    company_b_id: str  # engine_companies row id for engine 102
    client_id: str
    other_client_id: str
    key_admin_secret: str  # settings:manage, unrestricted
    key_admin_restricted_secret: str  # settings:manage, restricted to company A
    key_read_secret: str  # ledger:read only (not admin)


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
    engine: Engine, settings: AppSettings
) -> Generator[World, None, None]:
    """Provision a tenant mapped to engine companies 101 + 102."""
    session = sessionmaker(bind=engine)()
    service = ApiKeyService(ApiKeyCrypto(settings.auth.api_key_pepper))
    tenant_id = ""
    connection_ids: list[str] = []
    try:
        tenant = TenantModel(
            id=str(uuid.uuid4()), slug=f"rest-205a-{uuid.uuid4().hex[:8]}", name="205a Tenant"
        )
        session.add(tenant)
        session.flush()
        client = ApiClientModel(
            id=str(uuid.uuid4()), tenant_id=tenant.id, name="rest-205a client"
        )
        session.add(client)
        session.flush()
        other_client = ApiClientModel(
            id=str(uuid.uuid4()), tenant_id=tenant.id, name="rest-205a other client"
        )
        session.add(other_client)
        session.flush()

        def _company(external_id: str) -> EngineCompanyModel:
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
                tenant_id=tenant.id,
                engine_connection_id=connection.id,
                external_company_id=external_id,
                company_name=f"Company {external_id}",
            )
            session.add(row)
            session.flush()
            return row

        row_a = _company(ENGINE_A)
        row_b = _company(ENGINE_B)

        def _key(scopes: list[str], company_id: str | None = None):
            return service.issue_key(
                session,
                client=client,
                name="rest-205a key",
                scopes=scopes,
                installation_id=INSTALLATION_ID,
                engine_company_id=company_id,
            )

        key_admin = _key(["settings:manage"])
        key_admin_restricted = _key(["settings:manage"], company_id=row_a.id)
        key_read = _key(["ledger:read"])
        session.commit()
        tenant_id = tenant.id
        yield World(
            tenant_id=tenant.id,
            company_a_id=row_a.id,
            company_b_id=row_b.id,
            client_id=client.id,
            other_client_id=other_client.id,
            key_admin_secret=key_admin.secret,
            key_admin_restricted_secret=key_admin_restricted.secret,
            key_read_secret=key_read.secret,
        )
    finally:
        session.close()
        cleanup = sessionmaker(bind=engine)()
        try:
            if tenant_id:
                cleanup.query(TenantModel).filter(
                    TenantModel.id == tenant_id
                ).delete(synchronize_session=False)
            for connection_id in connection_ids:
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


def _seed_event(
    engine: Engine,
    world: World,
    *,
    action: str,
    occurred_at: datetime.datetime,
    api_client_id: str | None = None,
    company_external_id: str | None = ENGINE_A,
    outcome: str = "success",
    error_code: str | None = None,
    resource_type: str = "posted_journal",
    resource_id: str = "42",
) -> str:
    """Insert one audit event directly and return its id."""
    session = sessionmaker(bind=engine)()
    try:
        event = AuditEventModel(
            id=str(uuid.uuid4()),
            tenant_id=world.tenant_id,
            installation_id=INSTALLATION_ID,
            api_client_id=api_client_id or world.client_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            company_external_id=company_external_id,
            occurred_at=occurred_at,
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            source_system="api",
            authorization_result="allowed",
            outcome=outcome,
            error_code=error_code,
            tryton_reference="account.move:42",
            payload_hash="abc123",
            metadata_after={"idempotency_key": "seed-key"},
        )
        session.add(event)
        session.commit()
        return event.id
    finally:
        session.close()


def _seed_world_events(engine: Engine, world: World) -> list[str]:
    """Seed four events: two posts (A), one reverse (A), one create (B)."""
    return [
        _seed_event(engine, world, action="ledger.draft.create", occurred_at=T0),
        _seed_event(engine, world, action="ledger.post", occurred_at=T1),
        _seed_event(
            engine,
            world,
            action="ledger.post",
            occurred_at=T2,
            outcome="error",
            error_code="LEDGER_POSTED_IMMUTABLE",
        ),
        _seed_event(
            engine,
            world,
            action="ledger.reverse",
            occurred_at=T3,
            company_external_id=ENGINE_B,
            api_client_id=world.other_client_id,
        ),
    ]


# ---------------------------------------------------------------------------
# List: ordering, filters, pagination
# ---------------------------------------------------------------------------


def test_list_events_newest_first(
    api: TestClient, world: World, engine: Engine
) -> None:
    ids = _seed_world_events(engine, world)
    response = api.get("/api/v1/audit/events", headers=_auth(world.key_admin_secret))
    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["id"] for item in body["items"]] == list(reversed(ids))
    assert body["next_cursor"] is None
    first = body["items"][0]
    assert first["action"] == "ledger.reverse"
    assert first["tenant_id"] == world.tenant_id
    assert first["installation_id"] == INSTALLATION_ID
    assert first["occurred_at"] == "2026-11-17T13:00:00Z"


def test_list_events_filter_by_action_actor_company(
    api: TestClient, world: World, engine: Engine
) -> None:
    _seed_world_events(engine, world)
    headers = _auth(world.key_admin_secret)

    response = api.get("/api/v1/audit/events?action=ledger.post", headers=headers)
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 2
    assert all(item["action"] == "ledger.post" for item in items)

    response = api.get(
        f"/api/v1/audit/events?actor={world.other_client_id}", headers=headers
    )
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["action"] == "ledger.reverse"

    # Company filter accepts the engine id...
    response = api.get(
        f"/api/v1/audit/events?company={ENGINE_B}", headers=headers
    )
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["company_external_id"] == ENGINE_B
    # ...and the HesabYar row id.
    response = api.get(
        f"/api/v1/audit/events?company={world.company_b_id}", headers=headers
    )
    assert len(response.json()["items"]) == 1

    # Filters combine (AND).
    response = api.get(
        f"/api/v1/audit/events?action=ledger.post&company={ENGINE_A}",
        headers=headers,
    )
    assert len(response.json()["items"]) == 2


def test_list_events_filter_by_date_range(
    api: TestClient, world: World, engine: Engine
) -> None:
    _seed_world_events(engine, world)
    headers = _auth(world.key_admin_secret)

    # Inclusive bounds: T1..T2 covers exactly the two post events.
    response = api.get(
        "/api/v1/audit/events?from=2026-11-17T11:00:00Z&to=2026-11-17T12:00:00Z",
        headers=headers,
    )
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 2
    assert all(item["action"] == "ledger.post" for item in items)

    # Open-ended: only a lower bound.
    response = api.get(
        "/api/v1/audit/events?from=2026-11-17T12:00:00Z", headers=headers
    )
    assert len(response.json()["items"]) == 2

    # Naive datetimes are read as UTC.
    response = api.get(
        "/api/v1/audit/events?from=2026-11-17T12:00:00", headers=headers
    )
    assert len(response.json()["items"]) == 2

    # An inverted range is a 400.
    response = api.get(
        "/api/v1/audit/events?from=2026-11-17T12:00:00Z&to=2026-11-17T11:00:00Z",
        headers=headers,
    )
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_FILTER"


def test_list_events_cursor_pagination_walks_all(
    api: TestClient, world: World, engine: Engine
) -> None:
    ids = _seed_world_events(engine, world)
    headers = _auth(world.key_admin_secret)
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(6):
        url = "/api/v1/audit/events?limit=2"
        if cursor is not None:
            url += f"&cursor={cursor}"
        response = api.get(url, headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        seen.extend(item["id"] for item in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert sorted(seen) == sorted(ids)
    assert len(seen) == len(set(seen))  # no duplicates across pages


def test_list_events_oldest_first_sort(
    api: TestClient, world: World, engine: Engine
) -> None:
    ids = _seed_world_events(engine, world)
    response = api.get(
        "/api/v1/audit/events?sort=occurred_at",
        headers=_auth(world.key_admin_secret),
    )
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == ids
    response = api.get(
        "/api/v1/audit/events?sort=-occurred_at",
        headers=_auth(world.key_admin_secret),
    )
    assert [item["id"] for item in response.json()["items"]] == list(reversed(ids))


def test_list_events_rejects_bad_params(
    api: TestClient, world: World, engine: Engine
) -> None:
    _seed_world_events(engine, world)
    headers = _auth(world.key_admin_secret)
    response = api.get("/api/v1/audit/events?bogus=1", headers=headers)
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_FILTER"
    response = api.get("/api/v1/audit/events?sort=bogus", headers=headers)
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_SORT"
    response = api.get("/api/v1/audit/events?cursor=not-a-cursor", headers=headers)
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_CURSOR"
    # A well-formed cursor with a non-timestamp key is also invalid.
    payload = base64.urlsafe_b64encode(
        json.dumps({"v": 1, "k": "not-a-date", "t": "x"}).encode()
    ).decode().rstrip("=")
    response = api.get(f"/api/v1/audit/events?cursor={payload}", headers=headers)
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_CURSOR"


# ---------------------------------------------------------------------------
# Get one event
# ---------------------------------------------------------------------------


def test_get_audit_event_returns_event_and_404s(
    api: TestClient, world: World, engine: Engine
) -> None:
    ids = _seed_world_events(engine, world)
    headers = _auth(world.key_admin_secret)
    response = api.get(f"/api/v1/audit/events/{ids[2]}", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == ids[2]
    assert body["action"] == "ledger.post"
    assert body["outcome"] == "error"
    assert body["error_code"] == "LEDGER_POSTED_IMMUTABLE"
    assert body["tryton_reference"] == "account.move:42"
    assert body["metadata_after"] == {"idempotency_key": "seed-key"}

    response = api.get(
        f"/api/v1/audit/events/{uuid.uuid4()}", headers=headers
    )
    assert response.status_code == 404
    assert response.json()["code"] == "AUDIT_EVENT_NOT_FOUND"


def test_audit_events_of_another_tenant_are_invisible(
    api: TestClient, world: World, engine: Engine
) -> None:
    _seed_world_events(engine, world)
    # A second tenant with its own event.
    session = sessionmaker(bind=engine)()
    other_tenant_id = ""
    try:
        other_tenant = TenantModel(
            id=str(uuid.uuid4()),
            slug=f"rest-205b-{uuid.uuid4().hex[:8]}",
            name="Other Tenant",
        )
        session.add(other_tenant)
        session.flush()
        other_tenant_id = other_tenant.id
        event = AuditEventModel(
            id=str(uuid.uuid4()),
            tenant_id=other_tenant.id,
            installation_id=INSTALLATION_ID,
            action="ledger.post",
            resource_type="posted_journal",
            resource_id="99",
            occurred_at=T1,
            source_system="api",
            authorization_result="allowed",
            outcome="success",
        )
        session.add(event)
        session.commit()
        other_event_id = event.id
    finally:
        session.close()
    try:
        headers = _auth(world.key_admin_secret)
        response = api.get("/api/v1/audit/events", headers=headers)
        assert len(response.json()["items"]) == 4  # only this tenant's
        response = api.get(f"/api/v1/audit/events/{other_event_id}", headers=headers)
        assert response.status_code == 404
        assert response.json()["code"] == "AUDIT_EVENT_NOT_FOUND"
    finally:
        cleanup = sessionmaker(bind=engine)()
        try:
            if other_tenant_id:
                cleanup.query(TenantModel).filter(
                    TenantModel.id == other_tenant_id
                ).delete(synchronize_session=False)
                cleanup.commit()
        finally:
            cleanup.close()


# ---------------------------------------------------------------------------
# Auth: admin-only, company-restricted keys
# ---------------------------------------------------------------------------


def test_audit_endpoints_require_admin_scope(
    api: TestClient, world: World, engine: Engine
) -> None:
    ids = _seed_world_events(engine, world)
    # No key → 401.
    response = api.get("/api/v1/audit/events")
    assert response.status_code == 401
    # A non-admin key → 403 naming the missing scope.
    response = api.get(
        "/api/v1/audit/events", headers=_auth(world.key_read_secret)
    )
    assert response.status_code == 403
    assert "settings:manage" in response.json()["detail"]
    response = api.get(
        f"/api/v1/audit/events/{ids[0]}", headers=_auth(world.key_read_secret)
    )
    assert response.status_code == 403


def test_restricted_admin_key_sees_only_its_company(
    api: TestClient, world: World, engine: Engine
) -> None:
    ids = _seed_world_events(engine, world)
    headers = _auth(world.key_admin_restricted_secret)
    # No company param: forced to company A (3 of the 4 events).
    response = api.get("/api/v1/audit/events", headers=headers)
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 3
    assert all(item["company_external_id"] == ENGINE_A for item in items)
    # Asking for another company → 403.
    response = api.get(
        f"/api/v1/audit/events?company={ENGINE_B}", headers=headers
    )
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"
    # Getting another company's event → 404.
    response = api.get(f"/api/v1/audit/events/{ids[3]}", headers=headers)
    assert response.status_code == 404
    assert response.json()["code"] == "AUDIT_EVENT_NOT_FOUND"
    # Getting its own company's event works.
    response = api.get(f"/api/v1/audit/events/{ids[0]}", headers=headers)
    assert response.status_code == 200
