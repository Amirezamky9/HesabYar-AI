"""Tests for the manual-entry REST endpoints (US-513).

The real FastAPI app + scoped API-key auth + the PostgreSQL database.
The pipeline runs with its fail-closed defaults (no F3 mappings yet),
so a worker poll routes manual events to ``needs_review`` without
touching the engine — the live Tryton server is NOT needed here.

Provisioned rows are committed (the app's per-request sessions must
see them) and removed in teardown by deleting the tenant (cascades to
manual entries, inbox events, audit decisions, clients/keys);
idempotency records carry no tenant FK, so they are deleted by
installation id.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Generator
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete
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

TEST_DB_URL = os.getenv(
    "HESABYAR_DATABASE_URL",
    "postgresql+psycopg://hesabyar:hesabyar_pass@hesabyar-postgres:5432/hesabyar_test",
)

INSTALLATION_ID = "00000000-0000-0000-0000-000000000513"

ALL_INTEGRATIONS_SCOPES = ["integrations:read", "integrations:manage"]


@dataclass
class World:
    tenant_id: str
    key_full_secret: str
    key_read_secret: str


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
    try:
        tenant = TenantModel(
            id=str(uuid.uuid4()), slug=f"rest-513-{uuid.uuid4().hex[:8]}", name="513 Tenant"
        )
        session.add(tenant)
        session.flush()
        client = ApiClientModel(
            id=str(uuid.uuid4()), tenant_id=tenant.id, name="rest-513 client"
        )
        session.add(client)
        session.flush()
        key_full = service.issue_key(
            session,
            client=client,
            name="rest-513 full",
            scopes=ALL_INTEGRATIONS_SCOPES,
            installation_id=INSTALLATION_ID,
        )
        key_read = service.issue_key(
            session,
            client=client,
            name="rest-513 read",
            scopes=["integrations:read"],
            installation_id=INSTALLATION_ID,
        )
        session.commit()
        tenant_id = tenant.id
        yield World(
            tenant_id=tenant.id,
            key_full_secret=key_full.secret,
            key_read_secret=key_read.secret,
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


def _order_body(**overrides: object) -> dict:
    body: dict = {
        "order_number": f"ORD-513-{uuid.uuid4().hex[:8]}",
        "lines": [
            {"description": "قهوه اسپرسو", "quantity": 2, "unit_price_toman": 175_000},
            {"description": "کیک شکلاتی", "quantity": 1, "unit_price_toman": 240_000},
        ],
        "discount_toman": 10_000,
        "shipping_toman": 50_000,
    }
    body.update(overrides)
    return body


def _refund_body(order_number: str, **overrides: object) -> dict:
    body: dict = {
        "order_number": order_number,
        "refund_amount_toman": 175_000,
        "refund_reference": f"REF-513-{uuid.uuid4().hex[:8]}",
    }
    body.update(overrides)
    return body


def _create_order(api: TestClient, world: World, **overrides: object) -> dict:
    response = api.post(
        "/api/v1/manual-entries/orders",
        json=_order_body(**overrides),
        headers={**_auth(world.key_full_secret), "Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------------------
# Create: orders
# ---------------------------------------------------------------------------


def test_create_order_returns_201_with_the_canonical_event(api: TestClient, world: World) -> None:
    body = _order_body()
    response = api.post(
        "/api/v1/manual-entries/orders",
        json=body,
        headers={**_auth(world.key_full_secret), "Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["entry_type"] == "order"
    assert created["order_number"] == body["order_number"]
    assert created["source_store_id"] == "manual"
    # 2 × 175,000 + 240,000 − 10,000 + 50,000 = 630,000 Toman.
    assert created["total_amount_toman"] == 630_000
    assert created["entered_by"]
    assert len(created["events"]) == 1
    event = created["events"][0]
    assert event["event_type"] == "sales.order.completed"
    assert event["external_entity_type"] == "order"
    assert event["external_entity_id"] == body["order_number"]
    assert event["status"] == "received"
    assert event["engine_record_id"] is None
    assert event["decision"] is None
    assert created["payload"]["total_amount"] == 630_000
    assert created["payload"]["entry_source"] == "manual"
    assert created["payload"]["manual_entry_id"] == created["entry_id"]


def test_manual_order_lands_in_the_same_inbox_as_a_webhook(api: TestClient, world: World) -> None:
    created = _create_order(api, world)
    event_id = created["events"][0]["inbox_event_id"]
    fetched = api.get(
        f"/api/v1/integrations/inbox/{event_id}", headers=_auth(world.key_read_secret)
    )
    assert fetched.status_code == 200, fetched.text
    inbox_event = fetched.json()
    assert inbox_event["source_system"] == "manual"
    assert inbox_event["event_type"] == "sales.order.completed"
    assert inbox_event["correlation_id"] == created["entry_id"]
    assert inbox_event["payload"]["manual_entry_id"] == created["entry_id"]


def test_create_order_requires_an_idempotency_key(api: TestClient, world: World) -> None:
    response = api.post(
        "/api/v1/manual-entries/orders",
        json=_order_body(),
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_create_order_replays_the_same_key_without_a_second_entry(
    api: TestClient, world: World
) -> None:
    body = _order_body()
    headers = {**_auth(world.key_full_secret), "Idempotency-Key": str(uuid.uuid4())}
    first = api.post("/api/v1/manual-entries/orders", json=body, headers=headers)
    assert first.status_code == 201, first.text
    second = api.post("/api/v1/manual-entries/orders", json=body, headers=headers)
    assert second.status_code == 201, second.text
    assert second.json()["entry_id"] == first.json()["entry_id"]
    listed = api.get("/api/v1/manual-entries", headers=_auth(world.key_read_secret))
    assert len(listed.json()["items"]) == 1


def test_create_order_same_key_different_payload_is_409(api: TestClient, world: World) -> None:
    headers = {**_auth(world.key_full_secret), "Idempotency-Key": str(uuid.uuid4())}
    first = api.post("/api/v1/manual-entries/orders", json=_order_body(), headers=headers)
    assert first.status_code == 201, first.text
    second = api.post(
        "/api/v1/manual-entries/orders",
        json=_order_body(order_number="ORD-513-other"),
        headers=headers,
    )
    assert second.status_code == 409, second.text
    assert second.json()["code"] == "IDEMPOTENCY_CONFLICT"


def test_create_order_empty_lines_is_422(api: TestClient, world: World) -> None:
    response = api.post(
        "/api/v1/manual-entries/orders",
        json=_order_body(lines=[]),
        headers={**_auth(world.key_full_secret), "Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 422, response.text


def test_create_order_fractional_line_total_is_422_manual_entry_invalid(
    api: TestClient, world: World
) -> None:
    response = api.post(
        "/api/v1/manual-entries/orders",
        json=_order_body(
            lines=[{"description": "x", "quantity": "1.5", "unit_price_toman": 101}],
        ),
        headers={**_auth(world.key_full_secret), "Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "MANUAL_ENTRY_INVALID"


def test_create_order_discount_above_subtotal_is_422(api: TestClient, world: World) -> None:
    response = api.post(
        "/api/v1/manual-entries/orders",
        json=_order_body(discount_toman=600_000),
        headers={**_auth(world.key_full_secret), "Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "MANUAL_ENTRY_INVALID"


def test_create_order_with_a_read_only_key_is_403(api: TestClient, world: World) -> None:
    response = api.post(
        "/api/v1/manual-entries/orders",
        json=_order_body(),
        headers={**_auth(world.key_read_secret), "Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 403, response.text


def test_create_order_without_auth_is_401(api: TestClient, world: World) -> None:
    response = api.post(
        "/api/v1/manual-entries/orders",
        json=_order_body(),
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 401, response.text


# ---------------------------------------------------------------------------
# Create: refunds + cancellations
# ---------------------------------------------------------------------------


def test_create_refund_with_payment_emits_two_events(api: TestClient, world: World) -> None:
    order = _create_order(api, world)
    response = api.post(
        "/api/v1/manual-entries/refunds",
        json=_refund_body(order["order_number"]),
        headers={**_auth(world.key_full_secret), "Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["entry_type"] == "refund"
    assert created["total_amount_toman"] == 175_000
    assert [event["event_type"] for event in created["events"]] == [
        "sales.refund.created",
        "payment.refunded",
    ]
    assert created["events"][0]["external_entity_type"] == "refund"
    assert created["events"][1]["external_entity_type"] == "payment_refund"


def test_create_refund_without_payment_emits_one_event(api: TestClient, world: World) -> None:
    order = _create_order(api, world)
    response = api.post(
        "/api/v1/manual-entries/refunds",
        json=_refund_body(order["order_number"], refund_payment=False),
        headers={**_auth(world.key_full_secret), "Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 201, response.text
    assert [event["event_type"] for event in response.json()["events"]] == [
        "sales.refund.created"
    ]


def test_create_refund_lines_must_add_up(api: TestClient, world: World) -> None:
    order = _create_order(api, world)
    response = api.post(
        "/api/v1/manual-entries/refunds",
        json=_refund_body(
            order["order_number"],
            lines=[{"description": "x", "line_total_toman": 100_000}],
        ),
        headers={**_auth(world.key_full_secret), "Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "MANUAL_ENTRY_INVALID"


def test_create_cancellation_emits_one_cancelled_event(api: TestClient, world: World) -> None:
    order = _create_order(api, world)
    response = api.post(
        "/api/v1/manual-entries/cancellations",
        json={
            "order_number": order["order_number"],
            "total_amount_toman": 630_000,
            "reason": "مشتری منصرف شد",
        },
        headers={**_auth(world.key_full_secret), "Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["entry_type"] == "cancellation"
    assert len(created["events"]) == 1
    event = created["events"][0]
    assert event["event_type"] == "sales.order.cancelled"
    assert event["external_entity_type"] == "order_cancellation"
    assert event["external_entity_id"] == order["order_number"]


# ---------------------------------------------------------------------------
# List / get
# ---------------------------------------------------------------------------


def test_list_entries_filters_by_type(api: TestClient, world: World) -> None:
    order = _create_order(api, world)
    api.post(
        "/api/v1/manual-entries/refunds",
        json=_refund_body(order["order_number"]),
        headers={**_auth(world.key_full_secret), "Idempotency-Key": str(uuid.uuid4())},
    )
    all_entries = api.get("/api/v1/manual-entries", headers=_auth(world.key_read_secret))
    assert all_entries.status_code == 200, all_entries.text
    assert len(all_entries.json()["items"]) == 2
    refunds = api.get(
        "/api/v1/manual-entries?entry_type=refund", headers=_auth(world.key_read_secret)
    )
    assert len(refunds.json()["items"]) == 1
    assert refunds.json()["items"][0]["entry_type"] == "refund"
    bad = api.get(
        "/api/v1/manual-entries?entry_type=nope", headers=_auth(world.key_read_secret)
    )
    assert bad.status_code == 400, bad.text


def test_list_entries_paginates_with_cursor(api: TestClient, world: World) -> None:
    _create_order(api, world)
    _create_order(api, world)
    first = api.get(
        "/api/v1/manual-entries?limit=1", headers=_auth(world.key_read_secret)
    )
    assert len(first.json()["items"]) == 1
    assert first.json()["next_cursor"] is not None
    second = api.get(
        f"/api/v1/manual-entries?limit=1&cursor={first.json()['next_cursor']}",
        headers=_auth(world.key_read_secret),
    )
    assert len(second.json()["items"]) == 1
    assert second.json()["items"][0]["entry_id"] != first.json()["items"][0]["entry_id"]
    assert second.json()["next_cursor"] is None


def test_get_entry_returns_the_full_trace(api: TestClient, world: World) -> None:
    order = _create_order(api, world)
    api.post(
        "/api/v1/manual-entries/refunds",
        json=_refund_body(order["order_number"]),
        headers={**_auth(world.key_full_secret), "Idempotency-Key": str(uuid.uuid4())},
    )
    listed = api.get(
        "/api/v1/manual-entries?entry_type=refund", headers=_auth(world.key_read_secret)
    )
    entry_id = listed.json()["items"][0]["entry_id"]
    fetched = api.get(
        f"/api/v1/manual-entries/{entry_id}", headers=_auth(world.key_read_secret)
    )
    assert fetched.status_code == 200, fetched.text
    detail = fetched.json()
    assert detail["entry_id"] == entry_id
    assert len(detail["events"]) == 2
    assert detail["events"][0]["status"] == "received"


def test_get_missing_entry_is_404(api: TestClient, world: World) -> None:
    response = api.get(
        f"/api/v1/manual-entries/{uuid.uuid4()}", headers=_auth(world.key_read_secret)
    )
    assert response.status_code == 404, response.text
    assert response.json()["code"] == "MANUAL_ENTRY_NOT_FOUND"


# ---------------------------------------------------------------------------
# Same pipeline: a worker poll processes the manual event
# ---------------------------------------------------------------------------


def test_worker_poll_routes_manual_order_to_review(api: TestClient, world: World) -> None:
    """With F1 fail-closed defaults, a manual event needs_review like a webhook."""
    created = _create_order(api, world)
    event_id = created["events"][0]["inbox_event_id"]
    response = api.post(
        "/api/v1/integrations/worker/poll", headers=_auth(world.key_full_secret)
    )
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["claimed"] == 1
    assert report["needs_review"] == 1
    fetched = api.get(
        f"/api/v1/manual-entries/{created['entry_id']}", headers=_auth(world.key_read_secret)
    )
    assert fetched.json()["events"][0]["status"] == "needs_review"
    assert fetched.json()["events"][0]["decision"] == "needs_review"
    audit = api.get(
        f"/api/v1/integrations/audit-log?inbox_event_id={event_id}",
        headers=_auth(world.key_read_secret),
    )
    assert audit.json()["items"][0]["decision"] == "needs_review"
