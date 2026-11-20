"""Tests for the integrations REST endpoints (US-501).

The real FastAPI app + scoped API-key auth + the PostgreSQL database.
The pipeline runs with its fail-closed defaults (no F3 mappings yet),
so a worker poll routes events to ``needs_review`` without touching
the engine — the live Tryton server is NOT needed here.

Provisioned rows are committed (the app's per-request sessions must
see them) and removed in teardown by deleting the tenant (cascades to
inbox events, audit decisions, clients/keys); idempotency records
carry no tenant FK, so they are deleted by installation id; store
settings are deleted by the unique source system.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Generator
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from hesabyar.infrastructure.auth import ApiKeyCrypto, ApiKeyService
from hesabyar.infrastructure.config import AppSettings
from hesabyar.infrastructure.persistence.postgres.models import (
    ApiClientModel,
    Base,
    IdempotencyRecordModel,
    TenantModel,
)
from hesabyar.interfaces.rest.app import create_app
from tests.event_fixtures import raw_envelope

TEST_DB_URL = os.getenv(
    "HESABYAR_DATABASE_URL",
    "postgresql+psycopg://hesabyar:hesabyar_pass@hesabyar-postgres:5432/hesabyar_test",
)

INSTALLATION_ID = "00000000-0000-0000-0000-000000000501"

ALL_INTEGRATIONS_SCOPES = ["integrations:read", "integrations:manage"]


@dataclass
class World:
    tenant_id: str
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
    return AppSettings()


@pytest.fixture()
def world(engine: Engine, settings: AppSettings) -> Generator[World, None, None]:
    """Provision a tenant with integrations-scoped API keys."""
    session = sessionmaker(bind=engine)()
    service = ApiKeyService(ApiKeyCrypto(settings.auth.api_key_pepper))
    tenant_id = ""
    source_system = f"evt-rest-{uuid.uuid4().hex[:8]}"
    try:
        tenant = TenantModel(
            id=str(uuid.uuid4()), slug=f"rest-501-{uuid.uuid4().hex[:8]}", name="501 Tenant"
        )
        session.add(tenant)
        session.flush()
        client = ApiClientModel(
            id=str(uuid.uuid4()), tenant_id=tenant.id, name="rest-501 client"
        )
        session.add(client)
        session.flush()
        key_full = service.issue_key(
            session,
            client=client,
            name="rest-501 full",
            scopes=ALL_INTEGRATIONS_SCOPES,
            installation_id=INSTALLATION_ID,
        )
        key_read = service.issue_key(
            session,
            client=client,
            name="rest-501 read",
            scopes=["integrations:read"],
            installation_id=INSTALLATION_ID,
        )
        session.commit()
        tenant_id = tenant.id
        yield World(
            tenant_id=tenant.id,
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
                    IdempotencyRecordModel.installation_id == INSTALLATION_ID
                )
            )
            cleanup.execute(
                text("DELETE FROM connector_store_settings WHERE source_system = :source"),
                {"source": source_system},
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


def _ingest_body(world: World, **overrides: object) -> dict:
    return raw_envelope(source_system=world.source_system, **overrides)  # type: ignore[arg-type]


def _ingest_headers(world: World, event_type: str | None = "sales.order.completed") -> dict[str, str]:
    """Auth + content-type + the alongside event-type header."""
    headers = {**_auth(world.key_full_secret), "Content-Type": "application/json"}
    if event_type is not None:
        headers["X-HesabYar-Event-Type"] = event_type
    return headers


def _ingest(api: TestClient, world: World, **overrides: object) -> dict:
    response = api.post(
        f"/api/v1/integrations/{world.source_system}/events",
        content=json.dumps(_ingest_body(world, **overrides)),
        headers=_ingest_headers(world),
    )
    assert response.status_code in (200, 201), response.text
    return response.json()


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


def test_ingest_cheque_registered_is_accepted(api: TestClient, world: World) -> None:
    """US-533: the cheque webhook type is accepted at the edge (202/201).

    The 24-field payload is validated by the worker, not the
    receiver — ingest only proves the canonical name is known and
    the envelope lands in the inbox (the pipeline + intake are
    covered by tests/contract/test_cheque_intake_us533.py).
    """
    body = _ingest_body(
        world,
        payload={
            "sayad_id": "1234567890123456",
            "amount_toman": 1750000,
            "amount_currency": "IRT",
            "issue_date": "1405-08-26",
            "due_date": "1405-10-15",
            "issuer_name": "مشتری نمونه",
            "issuer_national_id": "0012345678",
            "issuer_bank": "056 سامان",
            "payee_name": "کافه ایوان",
            "payee_national_id": "00123456789",
            "order_id": "ord-1",
            "customer_ref": "cust-1",
            "sayad_registered": True,
            "purpose": "payment",
        },
        external_entity_type="cheque",
        external_entity_id="evt-cheque-1",
    )
    response = api.post(
        f"/api/v1/integrations/{world.source_system}/events",
        content=json.dumps(body),
        headers=_ingest_headers(world, event_type="cheque.received.registered"),
    )
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "created"
    assert response.json()["inbox_status"] == "received"


def test_ingest_returns_201_then_200_for_a_duplicate(api: TestClient, world: World) -> None:
    body = _ingest_body(world)
    headers = _ingest_headers(world)
    first = api.post(
        f"/api/v1/integrations/{world.source_system}/events",
        content=json.dumps(body),
        headers=headers,
    )
    assert first.status_code == 201, first.text
    assert first.json()["status"] == "created"
    assert first.json()["inbox_status"] == "received"
    assert first.json()["signature_verified"] is False
    second = api.post(
        f"/api/v1/integrations/{world.source_system}/events",
        content=json.dumps(body),
        headers=headers,
    )
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "duplicate"
    assert second.json()["inbox_event_id"] == first.json()["inbox_event_id"]


def test_ingest_same_key_different_payload_is_409(api: TestClient, world: World) -> None:
    _ingest(api, world, payload={"total_amount": 175000})
    response = api.post(
        f"/api/v1/integrations/{world.source_system}/events",
        content=json.dumps(_ingest_body(world, payload={"total_amount": 999000})),
        headers=_ingest_headers(world),
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "IDEMPOTENCY_CONFLICT"


def test_ingest_unknown_event_type_header_is_422(api: TestClient, world: World) -> None:
    """An unknown alongside type is rejected — new names need ADR approval."""
    response = api.post(
        f"/api/v1/integrations/{world.source_system}/events",
        content=json.dumps(_ingest_body(world)),
        headers=_ingest_headers(world, event_type="payment.bank_transfer.recorded"),
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "EVENT_UNKNOWN_TYPE"


def test_ingest_missing_event_type_header_is_422(api: TestClient, world: World) -> None:
    """The alongside type header is required — the envelope carries no type."""
    response = api.post(
        f"/api/v1/integrations/{world.source_system}/events",
        content=json.dumps(_ingest_body(world)),
        headers=_ingest_headers(world, event_type=None),
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "EVENT_INVALID_ENVELOPE"


def test_ingest_requires_manage_scope(api: TestClient, world: World) -> None:
    response = api.post(
        f"/api/v1/integrations/{world.source_system}/events",
        content=json.dumps(_ingest_body(world)),
        headers={
            **_auth(world.key_read_secret),
            "Content-Type": "application/json",
            "X-HesabYar-Event-Type": "sales.order.completed",
        },
    )
    assert response.status_code == 403, response.text


def test_ingest_requires_auth(api: TestClient, world: World) -> None:
    response = api.post(
        f"/api/v1/integrations/{world.source_system}/events",
        content=json.dumps(_ingest_body(world)),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 401, response.text


# ---------------------------------------------------------------------------
# Inbox reads
# ---------------------------------------------------------------------------


def test_inbox_list_and_get(api: TestClient, world: World) -> None:
    created = _ingest(api, world)
    event_id = created["inbox_event_id"]
    listing = api.get("/api/v1/integrations/inbox", headers=_auth(world.key_read_secret))
    assert listing.status_code == 200, listing.text
    items = listing.json()["items"]
    assert len(items) == 1
    assert items[0]["inbox_event_id"] == event_id
    assert items[0]["status"] == "received"
    assert items[0]["event_type"] == "sales.order.completed"
    assert items[0]["payload"]["total_amount"] == 175000
    fetched = api.get(f"/api/v1/integrations/inbox/{event_id}", headers=_auth(world.key_read_secret))
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["inbox_event_id"] == event_id
    assert fetched.json()["source_event_id"] == "evt-1"
    missing = api.get("/api/v1/integrations/inbox/nope", headers=_auth(world.key_read_secret))
    assert missing.status_code == 404, missing.text


def test_inbox_list_filters_by_status(api: TestClient, world: World) -> None:
    _ingest(api, world, source_event_id="evt-1")
    _ingest(api, world, source_event_id="evt-2")
    listing = api.get(
        "/api/v1/integrations/inbox?status=processed", headers=_auth(world.key_read_secret)
    )
    assert listing.status_code == 200, listing.text
    assert listing.json()["items"] == []


def test_stale_inbox_is_empty_for_fresh_events(api: TestClient, world: World) -> None:
    _ingest(api, world)
    response = api.get("/api/v1/integrations/inbox/stale", headers=_auth(world.key_read_secret))
    assert response.status_code == 200, response.text
    assert response.json()["stale_count"] == 0
    assert response.json()["events"] == []


# ---------------------------------------------------------------------------
# Worker poll + audit log + outbox
# ---------------------------------------------------------------------------


def test_worker_poll_routes_events_to_review_with_fail_closed_defaults(
    api: TestClient, world: World
) -> None:
    """With F1 alone (no F3 mappings), the pipeline fails closed to review."""
    created = _ingest(api, world)
    event_id = created["inbox_event_id"]
    response = api.post("/api/v1/integrations/worker/poll", headers=_auth(world.key_full_secret))
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["claimed"] == 1
    assert report["needs_review"] == 1
    assert report["processed_draft"] == 0
    fetched = api.get(f"/api/v1/integrations/inbox/{event_id}", headers=_auth(world.key_read_secret))
    assert fetched.json()["status"] == "needs_review"


def test_audit_log_records_the_pipeline_decision(api: TestClient, world: World) -> None:
    created = _ingest(api, world)
    event_id = created["inbox_event_id"]
    api.post("/api/v1/integrations/worker/poll", headers=_auth(world.key_full_secret))
    response = api.get(
        f"/api/v1/integrations/audit-log?inbox_event_id={event_id}",
        headers=_auth(world.key_read_secret),
    )
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["decision"] == "needs_review"
    assert items[0]["policy_version"] == "n/a"
    assert items[0]["inbox_event_id"] == event_id


def test_outbox_lists_processed_events(api: TestClient, world: World) -> None:
    _ingest(api, world)
    api.post("/api/v1/integrations/worker/poll", headers=_auth(world.key_full_secret))
    response = api.get("/api/v1/integrations/outbox", headers=_auth(world.key_read_secret))
    assert response.status_code == 200, response.text
    # A needs_review event emits no processed outbox event (only processed ones do).
    assert response.json()["items"] == []


# ---------------------------------------------------------------------------
# DLQ replay
# ---------------------------------------------------------------------------


def test_replay_rejects_events_that_are_not_dead_lettered(api: TestClient, world: World) -> None:
    created = _ingest(api, world)
    event_id = created["inbox_event_id"]
    response = api.post(
        f"/api/v1/integrations/inbox/{event_id}/replay", headers=_auth(world.key_full_secret)
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "EVENT_NOT_DEAD_LETTERED"


def test_replay_of_missing_event_is_404(api: TestClient, world: World) -> None:
    response = api.post(
        "/api/v1/integrations/inbox/nope/replay", headers=_auth(world.key_full_secret)
    )
    assert response.status_code == 404, response.text


# ---------------------------------------------------------------------------
# Store settings
# ---------------------------------------------------------------------------


def test_store_settings_default_and_update(api: TestClient, world: World) -> None:
    base = f"/api/v1/integrations/stores/{world.source_system}/store-1/settings"
    fetched = api.get(base, headers=_auth(world.key_read_secret))
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["prices_in_toman"] is True
    assert fetched.json()["currency"] == "IRR"
    updated = api.put(
        base,
        json={"prices_in_toman": False},
        headers=_auth(world.key_full_secret),
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["prices_in_toman"] is False
    again = api.get(base, headers=_auth(world.key_read_secret))
    assert again.json()["prices_in_toman"] is False
