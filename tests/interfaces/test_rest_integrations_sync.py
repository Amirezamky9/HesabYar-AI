"""Tests for the store sync REST endpoints (US-512, S2).

The real FastAPI app + scoped API-key auth + the PostgreSQL
database. The store itself is faked two ways: an in-memory fake
connector (via a ``get_store_connector_factory`` override) for the
API path, and a real local SQLite store database (via the real
factory + the stored read-only DSN) for the DB path. The live
Tryton server is NOT needed here — synced events land in the inbox
as ``received``; the pipeline runs separately.

Provisioned rows are committed (the app's per-request sessions must
see them) and removed in teardown: stores + idempotency records by
installation id, inbox events via the tenant cascade.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import os
import sqlite3
import uuid
from collections.abc import Generator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from hesabyar.domain.events.sync import (
    StoreChange,
    StoreEntityKind,
    SyncBatch,
    SyncCursor,
    map_store_change,
)
from hesabyar.infrastructure.auth import ApiKeyCrypto, ApiKeyService
from hesabyar.infrastructure.auth.credential_vault import generate_encryption_key
from hesabyar.infrastructure.config import AppSettings, AuthSettings
from hesabyar.infrastructure.persistence.postgres.models import (
    ApiClientModel,
    Base,
    IdempotencyRecordModel,
    StoreModel,
    TenantModel,
)
from hesabyar.interfaces.rest.app import create_app
from hesabyar.interfaces.rest.dependencies import get_store_connector_factory
from tests.event_fixtures import raw_envelope

TEST_DB_URL = os.getenv(
    "HESABYAR_DATABASE_URL",
    "postgresql+psycopg://hesabyar:hesabyar_pass@hesabyar-postgres:5432/hesabyar_test",
)

ALL_INTEGRATIONS_SCOPES = ["integrations:read", "integrations:manage"]

NOW = datetime.datetime(2026, 11, 18, 9, 0, tzinfo=datetime.UTC)


@dataclass
class World:
    tenant_id: str
    installation_id: str
    key_full_secret: str
    key_read_secret: str
    source_system: str


@pytest.fixture(scope="module")
def engine() -> Generator[Engine, None, None]:
    eng = create_engine(TEST_DB_URL, echo=False)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def settings() -> AppSettings:
    """Fresh settings with a test-generated vault key (dev pepper)."""
    return AppSettings(auth=AuthSettings(credentials_encryption_key=generate_encryption_key()))


@pytest.fixture()
def world(engine: Engine, settings: AppSettings) -> Generator[World, None, None]:
    """Provision a tenant with integrations-scoped API keys."""
    session = sessionmaker(bind=engine)()
    service = ApiKeyService(ApiKeyCrypto(settings.auth.api_key_pepper))
    tenant_id = ""
    installation_id = str(uuid.uuid4())
    source_system = f"sync-rest-{uuid.uuid4().hex[:8]}"
    try:
        tenant = TenantModel(
            id=str(uuid.uuid4()), slug=f"rest-512-{uuid.uuid4().hex[:8]}", name="512 Tenant"
        )
        session.add(tenant)
        session.flush()
        client = ApiClientModel(
            id=str(uuid.uuid4()), tenant_id=tenant.id, name="rest-512 client"
        )
        session.add(client)
        session.flush()
        key_full = service.issue_key(
            session,
            client=client,
            name="rest-512 full",
            scopes=ALL_INTEGRATIONS_SCOPES,
            installation_id=installation_id,
        )
        key_read = service.issue_key(
            session,
            client=client,
            name="rest-512 read",
            scopes=["integrations:read"],
            installation_id=installation_id,
        )
        session.commit()
        tenant_id = tenant.id
        yield World(
            tenant_id=tenant.id,
            installation_id=installation_id,
            key_full_secret=key_full.secret,
            key_read_secret=key_read.secret,
            source_system=source_system,
        )
    finally:
        session.close()
        cleanup = sessionmaker(bind=engine)()
        try:
            cleanup.execute(
                delete(IdempotencyRecordModel).where(
                    IdempotencyRecordModel.installation_id == installation_id
                )
            )
            cleanup.execute(
                delete(StoreModel).where(StoreModel.installation_id == installation_id)
            )
            if tenant_id:
                cleanup.query(TenantModel).filter(
                    TenantModel.id == tenant_id
                ).delete(synchronize_session=False)
            cleanup.commit()
        finally:
            cleanup.close()


@pytest.fixture()
def api(settings: AppSettings) -> TestClient:
    return TestClient(create_app(settings=settings), raise_server_exceptions=False)


def _auth(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


class FakeConnector:
    """In-memory connector serving a fixed change list, paged by offset."""

    def __init__(self, changes: Sequence[StoreChange]) -> None:
        self._changes = list(changes)

    def fetch_changes(self, cursor, *, updated_since=None, page_size=100):
        start = int(cursor.value) if cursor is not None and cursor.value else 0
        rows = self._changes[start : start + page_size]
        events = []
        unmapped = []
        for change in rows:
            mapped = map_store_change(change)
            if mapped:
                events.extend(mapped)
            else:
                unmapped.append(change)
        end = start + len(rows)
        has_more = end < len(self._changes)
        return SyncBatch(
            events=events,
            unmapped_changes=unmapped,
            next_cursor=SyncCursor(value=str(end)) if has_more else None,
            has_more=has_more,
        )

    def close(self) -> None:
        return None


class FakeFactory:
    """Connector factory override serving one fake connector."""

    def __init__(self, changes: Sequence[StoreChange]) -> None:
        self._changes = list(changes)

    def build(self, store, *, source_store_id, source):
        return FakeConnector(self._changes)

    def close(self) -> None:
        return None


def make_change(
    world: World,
    *,
    event_id: str,
    event_name: str = "order.completed",
    entity_type: StoreEntityKind = StoreEntityKind.ORDER,
    payload: dict | None = None,
) -> StoreChange:
    return StoreChange(
        source_system=world.source_system,
        source_store_id="store-1",
        entity_type=entity_type,
        entity_id=f"order-{event_id}",
        event_id=event_id,
        event_name=event_name,
        updated_at=NOW,
        status="completed",
        payload=payload if payload is not None else {"total_amount": 175000},
    )


def _create_store(api: TestClient, world: World, **overrides: Any) -> dict:
    body = {
        "store_key": world.source_system,
        "display_name": "Sync Test Store",
        "store_type": "custom",
        "api_base_url": "https://store.example.com/api/v1",
    }
    body.update(overrides)
    response = api.post(
        "/api/v1/integrations/stores", json=body, headers=_auth(world.key_full_secret)
    )
    assert response.status_code == 201, response.text
    return response.json()


def _sync(
    api: TestClient, world: World, body: dict | None = None, *, key: str | None = None
):
    payload = {"source_store_id": "store-1"}
    if body:
        payload.update(body)
    return api.post(
        f"/api/v1/integrations/{world.source_system}/sync",
        json=payload,
        headers=_auth(key or world.key_full_secret),
    )


def _use_fake_factory(api: TestClient, changes: Sequence[StoreChange]) -> None:
    app = cast(FastAPI, api.app)
    app.dependency_overrides[get_store_connector_factory] = lambda: FakeFactory(changes)


def _build_sqlite_store_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE hesabyar_changes (
                store_id TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                event_name TEXT NOT NULL,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                payload TEXT NOT NULL
            )
            """
        )
        conn.executemany(
            "INSERT INTO hesabyar_changes VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("store-1", "order", "order-1", "db-evt-1", "order.completed", "completed",
                 "2026-11-18T09:00:00+00:00", json.dumps({"total_amount": 175000})),
                ("store-1", "payment", "pay-1", "db-evt-2", "payment.captured", "captured",
                 "2026-11-18T09:01:00+00:00", json.dumps({"gross_amount": 175000})),
            ],
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Sync (fake connector)
# ---------------------------------------------------------------------------


def test_sync_creates_events_and_reports(api: TestClient, world: World) -> None:
    _create_store(api, world)
    _use_fake_factory(
        api,
        [
            make_change(world, event_id="evt-1"),
            make_change(world, event_id="evt-2"),
            make_change(
                world,
                event_id="chq-1",
                event_name="cheque.registered",
                entity_type=StoreEntityKind.CHEQUE,
                payload={"amount": 1000000},
            ),
        ],
    )
    response = _sync(api, world)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed"
    # chq-1 maps since ADR 0004 (US-530) approved cheque.registered
    # and US-533 wired it in — it was the unmapped example before.
    assert body["events_created"] == 3
    assert body["events_duplicate"] == 0
    assert body["changes_unmapped"] == 0
    assert body["events_by_type"] == {
        "sales.order.completed": 2,
        "cheque.received.registered": 1,
    }
    assert body["has_more"] is False
    assert body["next_cursor"] is None
    # The events are in the inbox.
    inbox = api.get(
        "/api/v1/integrations/inbox", headers=_auth(world.key_full_secret)
    )
    assert inbox.status_code == 200, inbox.text
    assert len(inbox.json()["items"]) == 3


def test_sync_second_run_is_all_duplicates(api: TestClient, world: World) -> None:
    _create_store(api, world)
    _use_fake_factory(api, [make_change(world, event_id="evt-1")])
    first = _sync(api, world)
    assert first.status_code == 200, first.text
    assert first.json()["events_created"] == 1
    second = _sync(api, world)
    assert second.status_code == 200, second.text
    assert second.json()["events_created"] == 0
    assert second.json()["events_duplicate"] == 1


def _sign(body: bytes, secret: str) -> str:
    """HMAC-SHA256 hex signature, mirroring test_rest_store_webhooks."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_webhook_then_sync_dedups(api: TestClient, world: World) -> None:
    """The 5ب exit criterion over REST: webhook + poll = one inbox row."""
    # Store creation ALWAYS generates a webhook secret (shown once),
    # so the webhook must be signed — an unsigned POST 401s with
    # EVENT_SIGNATURE_INVALID. (This failed on the local DB AND on
    # lane 1, unlike the invoice-date failures: it is a missing
    # signature, not a date collision.)
    store = _create_store(api, world)
    webhook_secret = store["webhook_secret"]
    envelope = raw_envelope(
        source_system=world.source_system,
        source_store_id="store-1",
        source_event_id="evt-1",
        payload={"total_amount": 175000},
    )
    raw_body = json.dumps(envelope).encode("utf-8")
    ingested = api.post(
        f"/api/v1/integrations/{world.source_system}/events",
        content=raw_body,
        headers={
            **_auth(world.key_full_secret),
            "Content-Type": "application/json",
            "X-HesabYar-Event-Type": "sales.order.completed",
            "X-HesabYar-Signature": _sign(raw_body, webhook_secret),
        },
    )
    assert ingested.status_code == 201, ingested.text
    _use_fake_factory(api, [make_change(world, event_id="evt-1")])
    response = _sync(api, world)
    assert response.status_code == 200, response.text
    assert response.json()["events_created"] == 0
    assert response.json()["events_duplicate"] == 1
    inbox = api.get("/api/v1/integrations/inbox", headers=_auth(world.key_full_secret))
    assert len(inbox.json()["items"]) == 1


def test_sync_unknown_store_is_404(api: TestClient, world: World) -> None:
    response = _sync(api, world)
    assert response.status_code == 404, response.text
    assert response.json()["code"] == "INTEGRATION_STORE_NOT_FOUND"


def test_sync_requires_manage_scope(api: TestClient, world: World) -> None:
    _create_store(api, world)
    response = _sync(api, world, key=world.key_read_secret)
    assert response.status_code == 403, response.text


def test_sync_window_params_are_mutually_exclusive(api: TestClient, world: World) -> None:
    _create_store(api, world)
    response = _sync(
        api,
        world,
        {"updated_since": "2026-11-01T00:00:00Z", "backfill_days": 30},
    )
    assert response.status_code == 422, response.text


def test_sync_backfill_days_accepted(api: TestClient, world: World) -> None:
    _create_store(api, world)
    _use_fake_factory(api, [make_change(world, event_id="evt-1")])
    response = _sync(api, world, {"backfill_days": 30})
    assert response.status_code == 200, response.text
    assert response.json()["events_created"] == 1


def test_sync_updates_last_poll_at(api: TestClient, world: World) -> None:
    _create_store(api, world)
    _use_fake_factory(api, [make_change(world, event_id="evt-1")])
    response = _sync(api, world)
    assert response.status_code == 200, response.text
    store = api.get(
        f"/api/v1/integrations/stores/{world.source_system}",
        headers=_auth(world.key_full_secret),
    )
    assert store.status_code == 200, store.text
    assert store.json()["last_poll_at"] is not None


# ---------------------------------------------------------------------------
# Sync (real factory)
# ---------------------------------------------------------------------------


def test_sync_unknown_source_is_422(api: TestClient, world: World) -> None:
    _create_store(api, world)
    response = _sync(api, world, {"source": "carrier-pigeon"})
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "SYNC_INVALID_REQUEST"


def test_sync_db_without_dsn_is_422(api: TestClient, world: World) -> None:
    _create_store(api, world)
    response = _sync(api, world, {"source": "db"})
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "STORE_NOT_CONFIGURED"


def test_sync_api_unreachable_is_502(api: TestClient, world: World) -> None:
    _create_store(api, world, api_base_url="http://127.0.0.1:9")
    response = _sync(api, world)
    assert response.status_code == 502, response.text
    assert response.json()["code"] == "STORE_UNREACHABLE"


def test_sync_db_end_to_end_via_real_factory(
    api: TestClient, world: World, tmp_path: Path
) -> None:
    """Store a read-only DSN, then sync from the real SQLite store DB."""
    db_path = tmp_path / "store.db"
    _build_sqlite_store_db(db_path)
    _create_store(api, world)
    creds = api.post(
        f"/api/v1/integrations/stores/{world.source_system}/db-credentials",
        json={"dsn": f"sqlite:///{db_path}"},
        headers=_auth(world.key_full_secret),
    )
    assert creds.status_code == 200, creds.text
    assert creds.json()["has_store_db_dsn"] is True
    assert str(db_path) not in creds.text  # the DSN is never returned
    response = _sync(api, world, {"source": "db"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed"
    assert body["connector_kind"] == "db"
    assert body["events_created"] == 2
    assert body["events_by_type"] == {
        "sales.order.completed": 1,
        "payment.captured": 1,
    }


# ---------------------------------------------------------------------------
# DB credentials endpoint
# ---------------------------------------------------------------------------


def test_set_db_credentials_invalid_dsn_is_422(api: TestClient, world: World) -> None:
    _create_store(api, world)
    response = api.post(
        f"/api/v1/integrations/stores/{world.source_system}/db-credentials",
        json={"dsn": "not-a-url"},
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 422, response.text


def test_set_db_credentials_unknown_store_is_404(api: TestClient, world: World) -> None:
    response = api.post(
        f"/api/v1/integrations/stores/{world.source_system}/db-credentials",
        json={"dsn": "postgresql://ro:secret@db:5432/store"},
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 404, response.text


def test_set_db_credentials_requires_manage_scope(api: TestClient, world: World) -> None:
    _create_store(api, world)
    response = api.post(
        f"/api/v1/integrations/stores/{world.source_system}/db-credentials",
        json={"dsn": "postgresql://ro:secret@db:5432/store"},
        headers=_auth(world.key_read_secret),
    )
    assert response.status_code == 403, response.text
