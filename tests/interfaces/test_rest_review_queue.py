"""Tests for the review-queue REST endpoints (US-504, Phase 5 F4).

Everything is real — the FastAPI app, scoped API-key auth, the
PostgreSQL database — except the engine, which these endpoints never
touch (the queue is a HesabYar-side table, not a Tryton read). The
live Tryton server is NOT needed here.

Provisioned tenants/keys are removed in teardown by deleting the
tenant (cascades to clients/keys/items/audit/policies). Items are
filed through the API wherever the test exercises the API; a few
tests insert backdated rows directly through the ORM to control age
(SLA/overdue/sort) deterministically.
"""

from __future__ import annotations

import datetime
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
    InboxEventModel,
    MappingSuggestionModel,
    ReviewItemModel,
    TenantModel,
)
from hesabyar.interfaces.rest.app import create_app

TEST_DB_URL = os.getenv(
    "HESABYAR_DATABASE_URL",
    "postgresql+psycopg://hesabyar:hesabyar_pass@hesabyar-postgres:5432/hesabyar_test",
)

INSTALLATION_ID = "00000000-0000-0000-0000-000000000504"

ALL_TYPES = [
    "mapping",
    "settlement_variance",
    "cheque_exception",
    "bank_unmatched",
    "packaging_reorder",
    "installment_overdue",
    "manual_import_review",
    "cashbox_variance",
    "inbox_dead_letter",
    "sms_delivery_failed",
    "reminder",
]


@dataclass
class World:
    """Provisioned tenant and keys for one test."""

    tenant_id: str
    other_tenant_id: str
    read_key: str
    resolve_key: str
    admin_key: str
    company_key: str
    other_read_key: str


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
    """Provision two tenants and five keys; clean up after."""
    session = sessionmaker(bind=engine)()
    service = ApiKeyService(ApiKeyCrypto(settings.auth.api_key_pepper))
    tenant_ids: list[str] = []
    try:
        tenant = TenantModel(id=str(uuid.uuid4()), slug=f"rest-504-{uuid.uuid4().hex[:8]}", name="Tenant 1")
        other_tenant = TenantModel(id=str(uuid.uuid4()), slug=f"rest-504-{uuid.uuid4().hex[:8]}", name="Tenant 2")
        session.add_all([tenant, other_tenant])
        session.flush()
        client = ApiClientModel(id=str(uuid.uuid4()), tenant_id=tenant.id, name="rest-504 client")
        other_client = ApiClientModel(id=str(uuid.uuid4()), tenant_id=other_tenant.id, name="rest-504 other client")
        session.add_all([client, other_client])
        session.flush()

        def _key(client_row: ApiClientModel, scopes: list[str]):
            return service.issue_key(
                session,
                client=client_row,
                name="rest-504 key",
                scopes=scopes,
                installation_id=INSTALLATION_ID,
            )

        key_read = _key(client, ["review:read"])
        key_resolve = _key(client, ["review:read", "review:resolve", "review:file"])
        key_admin = _key(client, ["review:read", "settings:manage"])
        key_company = _key(client, ["company:read"])
        key_other_read = _key(other_client, ["review:read"])
        session.commit()
        tenant_ids = [tenant.id, other_tenant.id]
        yield World(
            tenant_id=tenant.id,
            other_tenant_id=other_tenant.id,
            read_key=key_read.secret,
            resolve_key=key_resolve.secret,
            admin_key=key_admin.secret,
            company_key=key_company.secret,
            other_read_key=key_other_read.secret,
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
    """Real app (review-queue endpoints need no engine adapter)."""
    app = create_app(settings=settings)
    return TestClient(app, raise_server_exceptions=False)


def _auth(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


def _file_item(client: TestClient, secret: str, **overrides) -> dict:
    body = {
        "item_type": "mapping",
        "severity": "warning",
        "reason": "نگاشت نامشخص برای مشتری جدید",
        "suggested_action": "نگاشت را تکمیل کنید",
    }
    body.update(overrides)
    response = client.post("/api/v1/review-queue/items", json=body, headers=_auth(secret))
    assert response.status_code == 201, response.text
    return response.json()


def _insert_backdated_item(
    engine: Engine,
    tenant_id: str,
    *,
    item_type: str,
    severity: str,
    age_hours: float,
    channel: str | None = None,
    amount_irr: int | None = None,
) -> str:
    """Insert a review item row with a controlled created_at (test-only seeding)."""
    session = sessionmaker(bind=engine)()
    try:
        created = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=age_hours)
        row = ReviewItemModel(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            item_type=item_type,
            severity=severity,
            status="open",
            channel=channel,
            reason="backdated test item",
            suggested_action="test action",
            amount_irr=amount_irr,
            created_at=created,
            updated_at=created,
        )
        session.add(row)
        session.commit()
        return row.id
    finally:
        session.close()


def _insert_inbox_event(
    engine: Engine,
    tenant_id: str,
    *,
    status: str,
    attempts: int = 0,
) -> str:
    """Insert an inbox event row with a controlled status (test-only seeding)."""
    session = sessionmaker(bind=engine)()
    try:
        now = datetime.datetime.now(datetime.UTC)
        row = InboxEventModel(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            installation_id=INSTALLATION_ID,
            schema_version=1,
            source_system="hayat",
            source_store_id="store-1",
            source_event_id=f"evt-{uuid.uuid4().hex[:12]}",
            event_type="sales.order.completed",
            occurred_at=now,
            received_at=now,
            currency="IRR",
            external_entity_type="order",
            external_entity_id="order-1",
            idempotency_key=f"key-{uuid.uuid4().hex[:12]}",
            payload={"total": 1000},
            payload_hash="hash",
            status=status,
            attempts=attempts,
            last_error="Engine unreachable." if status == "dead_lettered" else None,
            dead_lettered_at=now if status == "dead_lettered" else None,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.commit()
        return row.id
    finally:
        session.close()


def _get_inbox_event(engine: Engine, event_id: str) -> InboxEventModel | None:
    """Read one inbox event row back (test-only verification)."""
    session = sessionmaker(bind=engine)()
    try:
        return session.get(InboxEventModel, event_id)
    finally:
        session.close()


def _list_suggestions(engine: Engine, installation_id: str) -> list[MappingSuggestionModel]:
    """Read the installation's mapping suggestions back (test-only verification)."""
    session = sessionmaker(bind=engine)()
    try:
        return list(
            session.query(MappingSuggestionModel)
            .filter(MappingSuggestionModel.installation_id == installation_id)
            .order_by(MappingSuggestionModel.created_at)
            .all()
        )
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_list_items_requires_auth(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get("/api/v1/review-queue/items")
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"


def test_list_items_requires_review_read_scope(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get("/api/v1/review-queue/items", headers=_auth(world.company_key))
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


def test_file_item_requires_review_file_scope(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.post(
        "/api/v1/review-queue/items",
        json={
            "item_type": "mapping",
            "severity": "warning",
            "reason": "r",
            "suggested_action": "s",
        },
        headers=_auth(world.read_key),
    )
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


# ---------------------------------------------------------------------------
# GET /review-queue/types
# ---------------------------------------------------------------------------


def test_list_types_returns_all_eleven_with_actions(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get("/api/v1/review-queue/types", headers=_auth(world.read_key))
    assert response.status_code == 200
    types = response.json()
    assert [t["item_type"] for t in types] == ALL_TYPES
    for entry in types:
        assert len(entry["actions"]) >= 1
        assert all("code" in action and "kind" in action for action in entry["actions"])


# ---------------------------------------------------------------------------
# POST + GET /review-queue/items — the eleven types, filters, sort, paging
# ---------------------------------------------------------------------------


def test_api_returns_all_eleven_item_types(settings: AppSettings, world: World) -> None:
    """Exit criterion (phased design §1.2): one sample item per type, all returned."""
    client = _test_client(settings)
    for item_type in ALL_TYPES:
        _file_item(client, world.resolve_key, item_type=item_type)
    response = client.get("/api/v1/review-queue/items", headers=_auth(world.read_key))
    assert response.status_code == 200
    body = response.json()
    assert body["next_cursor"] is None
    assert len(body["items"]) == 11
    assert {item["item_type"] for item in body["items"]} == set(ALL_TYPES)
    for item in body["items"]:
        assert len(item["actions"]) >= 1
        assert item["status"] == "open"
        assert item["age_hours"] >= 0
        assert item["sla_deadline"] is not None
        assert item["sla_breached"] is False
        assert item["audit"] == []  # list endpoint serves no audit trail


def test_file_item_rejects_unknown_type_and_severity(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.post(
        "/api/v1/review-queue/items",
        json={"item_type": "nope", "severity": "warning", "reason": "r", "suggested_action": "s"},
        headers=_auth(world.resolve_key),
    )
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_FILTER"
    response = client.post(
        "/api/v1/review-queue/items",
        json={"item_type": "mapping", "severity": "urgent", "reason": "r", "suggested_action": "s"},
        headers=_auth(world.resolve_key),
    )
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_FILTER"


def test_list_filters(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    _file_item(
        client,
        world.resolve_key,
        item_type="bank_unmatched",
        severity="critical",
        channel="melli",
        amount_irr=5_000_000,
    )
    _file_item(
        client,
        world.resolve_key,
        item_type="bank_unmatched",
        severity="info",
        channel="zarinpal",
        amount_irr=100_000,
    )
    _file_item(client, world.resolve_key, item_type="mapping", channel="melli", amount_irr=200_000)

    def _ids(params: str) -> list[str]:
        response = client.get(f"/api/v1/review-queue/items{params}", headers=_auth(world.read_key))
        assert response.status_code == 200, response.text
        return [item["item_type"] for item in response.json()["items"]]

    assert _ids("?type=bank_unmatched") == ["bank_unmatched", "bank_unmatched"]
    assert _ids("?severity=critical") == ["bank_unmatched"]
    assert _ids("?channel=melli") == ["bank_unmatched", "mapping"]
    assert _ids("?min_amount=1000000") == ["bank_unmatched"]
    assert _ids("?max_amount=150000") == ["bank_unmatched"]


def test_list_rejects_unknown_params_and_filters(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get("/api/v1/review-queue/items?bogus=1", headers=_auth(world.read_key))
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_FILTER"
    response = client.get("/api/v1/review-queue/items?type=nope", headers=_auth(world.read_key))
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_FILTER"
    response = client.get("/api/v1/review-queue/items?sort=nope", headers=_auth(world.read_key))
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_SORT"


def test_default_sort_is_critical_first_then_oldest(settings: AppSettings, world: World, engine: Engine) -> None:
    client = _test_client(settings)
    old_crit = _insert_backdated_item(
        engine, world.tenant_id, item_type="cheque_exception", severity="critical", age_hours=30
    )
    new_crit = _insert_backdated_item(
        engine, world.tenant_id, item_type="cheque_exception", severity="critical", age_hours=1
    )
    warn = _insert_backdated_item(engine, world.tenant_id, item_type="mapping", severity="warning", age_hours=100)
    info = _insert_backdated_item(engine, world.tenant_id, item_type="mapping", severity="info", age_hours=200)
    response = client.get("/api/v1/review-queue/items", headers=_auth(world.read_key))
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [old_crit, new_crit, warn, info]


def test_list_paginates_with_cursor(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    for _ in range(3):
        _file_item(client, world.resolve_key)
    first = client.get("/api/v1/review-queue/items?limit=2", headers=_auth(world.read_key))
    assert first.status_code == 200
    assert len(first.json()["items"]) == 2
    cursor = first.json()["next_cursor"]
    assert cursor is not None
    second = client.get(f"/api/v1/review-queue/items?limit=2&cursor={cursor}", headers=_auth(world.read_key))
    assert second.status_code == 200
    assert len(second.json()["items"]) == 1
    assert second.json()["next_cursor"] is None
    first_ids = {item["id"] for item in first.json()["items"]}
    second_ids = {item["id"] for item in second.json()["items"]}
    assert not first_ids & second_ids


def test_list_is_tenant_scoped(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    _file_item(client, world.resolve_key)
    response = client.get("/api/v1/review-queue/items", headers=_auth(world.other_read_key))
    assert response.status_code == 200
    assert response.json()["items"] == []


def test_get_item_returns_audit_trail(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    created = _file_item(client, world.resolve_key, channel="zarinpal", amount_irr=750_000)
    response = client.get(f"/api/v1/review-queue/items/{created['id']}", headers=_auth(world.read_key))
    assert response.status_code == 200
    body = response.json()
    assert body["channel"] == "zarinpal"
    assert body["amount_irr"] == 750_000
    assert [entry["action"] for entry in body["audit"]] == ["filed"]
    assert body["audit"][0]["actor"] == "rest-504 client"


def test_get_item_unknown_or_other_tenant_is_404(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    created = _file_item(client, world.resolve_key)
    response = client.get(f"/api/v1/review-queue/items/{created['id']}", headers=_auth(world.other_read_key))
    assert response.status_code == 404
    assert response.json()["code"] == "REVIEW_ITEM_NOT_FOUND"
    response = client.get(
        "/api/v1/review-queue/items/00000000-0000-0000-0000-000000000000",
        headers=_auth(world.read_key),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "REVIEW_ITEM_NOT_FOUND"


# ---------------------------------------------------------------------------
# POST /review-queue/items/{id}/actions + PATCH assign
# ---------------------------------------------------------------------------


def test_perform_resolve_action(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    created = _file_item(client, world.resolve_key)
    response = client.post(
        f"/api/v1/review-queue/items/{created['id']}/actions",
        json={"action": "auto_create_party_item", "note": "ساخته شد"},
        headers=_auth(world.resolve_key),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "resolved"
    assert body["resolution"] == "adjusted"
    assert body["resolved_by"] == "rest-504 client"
    assert [entry["action"] for entry in body["audit"]] == ["filed", "auto_create_party_item"]
    assert body["audit"][1]["note"] == "ساخته شد"


def test_perform_retry_action_moves_to_in_review(
    settings: AppSettings, world: World, engine: Engine
) -> None:
    """US-552: replay really replays — the dead-lettered event returns to received."""
    client = _test_client(settings)
    event_id = _insert_inbox_event(engine, world.tenant_id, status="dead_lettered", attempts=5)
    created = _file_item(
        client,
        world.resolve_key,
        item_type="inbox_dead_letter",
        severity="critical",
        source_event_ref=event_id,
    )
    response = client.post(
        f"/api/v1/review-queue/items/{created['id']}/actions",
        json={"action": "replay"},
        headers=_auth(world.resolve_key),
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "in_review"
    assert response.json()["resolution"] is None
    replayed = _get_inbox_event(engine, event_id)
    assert replayed is not None
    assert replayed.status == "received"
    assert replayed.attempts == 0
    assert replayed.last_error is None


def test_perform_action_rejects_wrong_type_action(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    created = _file_item(client, world.resolve_key, item_type="mapping")
    response = client.post(
        f"/api/v1/review-queue/items/{created['id']}/actions",
        json={"action": "replay"},
        headers=_auth(world.resolve_key),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "REVIEW_ACTION_NOT_PERMITTED"


def test_perform_action_on_terminal_item_is_conflict(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    created = _file_item(client, world.resolve_key)
    client.post(
        f"/api/v1/review-queue/items/{created['id']}/actions",
        json={"action": "skip"},
        headers=_auth(world.resolve_key),
    )
    response = client.post(
        f"/api/v1/review-queue/items/{created['id']}/actions",
        json={"action": "skip"},
        headers=_auth(world.resolve_key),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "REVIEW_INVALID_STATE"


def test_perform_action_requires_resolve_scope(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    created = _file_item(client, world.resolve_key)
    response = client.post(
        f"/api/v1/review-queue/items/{created['id']}/actions",
        json={"action": "skip"},
        headers=_auth(world.read_key),
    )
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


def test_assign_item(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    created = _file_item(client, world.resolve_key)
    response = client.patch(
        f"/api/v1/review-queue/items/{created['id']}",
        json={"assignee": "accountant-1"},
        headers=_auth(world.resolve_key),
    )
    assert response.status_code == 200, response.text
    assert response.json()["assignee"] == "accountant-1"
    assert [entry["action"] for entry in response.json()["audit"]] == ["filed", "assign"]


# ---------------------------------------------------------------------------
# GET /review-queue/summary — counters, ageing, admin notifications
# ---------------------------------------------------------------------------


def test_summary_covers_all_eleven_types(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    _file_item(client, world.resolve_key, item_type="mapping")
    _file_item(client, world.resolve_key, item_type="mapping")
    response = client.get("/api/v1/review-queue/summary", headers=_auth(world.read_key))
    assert response.status_code == 200
    body = response.json()
    assert len(body["counters"]) == 11
    assert {c["item_type"] for c in body["counters"]} == set(ALL_TYPES)
    assert body["total_open"] == 2
    assert body["total_overdue"] == 0
    assert body["critical_overdue_count"] == 0
    assert body["admin_notifications"] == []
    mapping = next(c for c in body["counters"] if c["item_type"] == "mapping")
    assert mapping["open_count"] == 2
    assert mapping["oldest_open_age_hours"] is not None


def test_summary_flags_critical_overdue_with_admin_notification(
    settings: AppSettings, world: World, engine: Engine
) -> None:
    client = _test_client(settings)
    # Cheque SLA is 24h: a 30h-old critical cheque item is overdue + notifiable.
    overdue_id = _insert_backdated_item(
        engine, world.tenant_id, item_type="cheque_exception", severity="critical", age_hours=30
    )
    # A 30h-old warning cheque item is overdue but never notifies an admin.
    _insert_backdated_item(engine, world.tenant_id, item_type="cheque_exception", severity="warning", age_hours=30)
    response = client.get("/api/v1/review-queue/summary", headers=_auth(world.read_key))
    assert response.status_code == 200
    body = response.json()
    assert body["total_overdue"] == 2
    assert body["critical_overdue_count"] == 1
    assert len(body["admin_notifications"]) == 1
    note = body["admin_notifications"][0]
    assert note["item_id"] == overdue_id
    assert note["item_type"] == "cheque_exception"
    assert note["severity"] == "critical"
    assert note["overdue_hours"] == pytest.approx(6.0, abs=0.1)
    # The list's overdue filter agrees.
    overdue_list = client.get("/api/v1/review-queue/items?overdue=true", headers=_auth(world.read_key))
    assert overdue_list.status_code == 200
    assert len(overdue_list.json()["items"]) == 2
    assert all(item["sla_breached"] for item in overdue_list.json()["items"])


# ---------------------------------------------------------------------------
# GET / PATCH /review-queue/sla-policies
# ---------------------------------------------------------------------------


def test_sla_policies_default_then_update(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get("/api/v1/review-queue/sla-policies", headers=_auth(world.read_key))
    assert response.status_code == 200
    policies = {p["item_type"]: p for p in response.json()}
    assert len(policies) == 11
    assert policies["mapping"]["sla_hours"] == 48
    assert policies["mapping"]["warn_hours"] == 12
    assert policies["mapping"]["updated_at"] is None

    updated = client.patch(
        "/api/v1/review-queue/sla-policies/mapping",
        json={"sla_hours": 12, "warn_hours": 3},
        headers=_auth(world.admin_key),
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["sla_hours"] == 12
    assert updated.json()["warn_hours"] == 3
    assert updated.json()["updated_at"] is not None

    again = client.get("/api/v1/review-queue/sla-policies", headers=_auth(world.read_key))
    policies = {p["item_type"]: p for p in again.json()}
    assert policies["mapping"]["sla_hours"] == 12
    assert policies["cheque_exception"]["sla_hours"] == 24  # untouched types keep defaults


def test_sla_policy_update_requires_admin(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.patch(
        "/api/v1/review-queue/sla-policies/mapping",
        json={"sla_hours": 12, "warn_hours": 3},
        headers=_auth(world.resolve_key),
    )
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


def test_sla_policy_update_rejects_invalid_windows(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.patch(
        "/api/v1/review-queue/sla-policies/mapping",
        json={"sla_hours": 12, "warn_hours": 12},
        headers=_auth(world.admin_key),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "REVIEW_INVALID_SLA_POLICY"
    response = client.patch(
        "/api/v1/review-queue/sla-policies/nope",
        json={"sla_hours": 12, "warn_hours": 3},
        headers=_auth(world.admin_key),
    )
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_FILTER"


# ---------------------------------------------------------------------------
# US-552: derived suggestions, replay, mapping learning
# ---------------------------------------------------------------------------

#: Expected derived suggestion per type for a plain warning item (no subtype).
EXPECTED_SUGGESTIONS = {
    "mapping": ("complete_mapping_retry", "mapping_blocks_event"),
    "settlement_variance": ("accept_variance", "small_variance_accept"),
    "cheque_exception": ("contact_customer", "cheque_needs_customer"),
    "bank_unmatched": ("manual_match", "line_needs_match"),
    "packaging_reorder": ("approve_purchase", "stock_below_reorder"),
    "installment_overdue": ("send_reminder", "installment_overdue_remind"),
    "manual_import_review": ("approve_preview", "batch_preview_ready"),
    "cashbox_variance": ("approve_variance_entry", "variance_needs_approval"),
    "inbox_dead_letter": ("replay", "event_dead_lettered"),
    "sms_delivery_failed": ("retry_reminder", "sms_send_failed"),
}


def test_list_items_carry_derived_suggestions(settings: AppSettings, world: World) -> None:
    """Every item carries its derived suggestion (action code + reason key)."""
    client = _test_client(settings)
    for item_type in ALL_TYPES:
        _file_item(client, world.resolve_key, item_type=item_type)
    response = client.get("/api/v1/review-queue/items", headers=_auth(world.read_key))
    assert response.status_code == 200
    by_type = {item["item_type"]: item for item in response.json()["items"]}
    assert set(by_type) == set(ALL_TYPES)
    for item_type, (action, reason_key) in EXPECTED_SUGGESTIONS.items():
        item = by_type[item_type]
        assert item["suggested_action_code"] == action, item_type
        assert item["suggested_reason_key"] == reason_key, item_type
        # The derived action is always one the operator can perform.
        assert action in {a["code"] for a in item["actions"]}, item_type


def test_derived_suggestion_overrides_by_subtype_and_severity(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    fee = _file_item(
        client, world.resolve_key, item_type="settlement_variance", subtype="fee_mismatch"
    )
    assert fee["suggested_action_code"] == "fix_fee_mapping"
    assert fee["suggested_reason_key"] == "variance_is_fee_mismatch"
    large = _file_item(
        client, world.resolve_key, item_type="settlement_variance", severity="critical"
    )
    assert large["suggested_action_code"] == "escalate_to_gateway"
    bounced = _file_item(
        client, world.resolve_key, item_type="cheque_exception", subtype="cheque_bounced"
    )
    assert bounced["suggested_action_code"] == "record_bounce"
    cashbox = _file_item(
        client, world.resolve_key, item_type="cashbox_variance", severity="critical"
    )
    assert cashbox["suggested_action_code"] == "escalate_to_manager"


def test_replay_without_source_event_is_422(settings: AppSettings, world: World) -> None:
    """A replay with nothing to replay fails loudly instead of pretending."""
    client = _test_client(settings)
    created = _file_item(client, world.resolve_key, item_type="inbox_dead_letter", severity="critical")
    response = client.post(
        f"/api/v1/review-queue/items/{created['id']}/actions",
        json={"action": "replay"},
        headers=_auth(world.resolve_key),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "REVIEW_REPLAY_NOT_POSSIBLE"
    assert client.get(
        f"/api/v1/review-queue/items/{created['id']}", headers=_auth(world.read_key)
    ).json()["status"] == "open"


def test_replay_of_missing_event_is_404(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    created = _file_item(
        client,
        world.resolve_key,
        item_type="inbox_dead_letter",
        severity="critical",
        source_event_ref="missing-event",
    )
    response = client.post(
        f"/api/v1/review-queue/items/{created['id']}/actions",
        json={"action": "replay"},
        headers=_auth(world.resolve_key),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "INBOX_EVENT_NOT_FOUND"


def test_replay_of_received_event_is_409(settings: AppSettings, world: World, engine: Engine) -> None:
    client = _test_client(settings)
    event_id = _insert_inbox_event(engine, world.tenant_id, status="received")
    created = _file_item(
        client,
        world.resolve_key,
        item_type="inbox_dead_letter",
        severity="critical",
        source_event_ref=event_id,
    )
    response = client.post(
        f"/api/v1/review-queue/items/{created['id']}/actions",
        json={"action": "replay"},
        headers=_auth(world.resolve_key),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "EVENT_NOT_DEAD_LETTERED"


def test_complete_mapping_retry_learns_mapping_and_reprocesses(
    settings: AppSettings, world: World, engine: Engine
) -> None:
    """The completed mapping is learned as a suggestion; the event is reprocessed."""
    client = _test_client(settings)
    event_id = _insert_inbox_event(engine, world.tenant_id, status="needs_review", attempts=1)
    created = _file_item(
        client, world.resolve_key, item_type="mapping", source_event_ref=event_id
    )
    response = client.post(
        f"/api/v1/review-queue/items/{created['id']}/actions",
        json={
            "action": "complete_mapping_retry",
            "mapping": {
                "mapping_kind": "gateway_journal",
                "source_key": "zarinpal",
                "target_type": "journal",
                "target_ref": "journal-1",
            },
        },
        headers=_auth(world.resolve_key),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "in_review"
    suggestions = _list_suggestions(engine, INSTALLATION_ID)
    assert len(suggestions) == 1
    suggestion = suggestions[0]
    assert suggestion.mapping_kind == "gateway_journal"
    assert suggestion.source_key == "zarinpal"
    assert suggestion.suggested_target_type == "journal"
    assert suggestion.suggested_target_ref == "journal-1"
    assert suggestion.basis == f"review_queue:{created['id']}"
    assert suggestion.status == "pending"
    assert body["audit"][-1]["mapping_version"] == f"suggestion:{suggestion.id}"
    assert f"reprocessed source event {event_id}" in (body["audit"][-1]["note"] or "")
    reprocessed = _get_inbox_event(engine, event_id)
    assert reprocessed is not None
    assert reprocessed.status == "received"


def test_complete_mapping_retry_without_mapping_only_reprocesses(
    settings: AppSettings, world: World, engine: Engine
) -> None:
    """No mapping supplied: the mapping was completed elsewhere; only reprocess."""
    client = _test_client(settings)
    event_id = _insert_inbox_event(engine, world.tenant_id, status="needs_review", attempts=1)
    created = _file_item(
        client, world.resolve_key, item_type="mapping", source_event_ref=event_id
    )
    response = client.post(
        f"/api/v1/review-queue/items/{created['id']}/actions",
        json={"action": "complete_mapping_retry"},
        headers=_auth(world.resolve_key),
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "in_review"
    # No mapping was supplied, so THIS action must learn nothing. The
    # DB is module-shared, so an earlier test's suggestion may exist —
    # assert on this item's basis, not on an empty table.
    assert [
        s
        for s in _list_suggestions(engine, INSTALLATION_ID)
        if s.basis == f"review_queue:{created['id']}"
    ] == []
    assert response.json()["audit"][-1]["mapping_version"] is None
    reprocessed = _get_inbox_event(engine, event_id)
    assert reprocessed is not None
    assert reprocessed.status == "received"


def test_mapping_details_on_wrong_action_is_422(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    created = _file_item(client, world.resolve_key, item_type="mapping")
    response = client.post(
        f"/api/v1/review-queue/items/{created['id']}/actions",
        json={
            "action": "skip",
            "mapping": {
                "mapping_kind": "gateway_journal",
                "source_key": "zarinpal",
                "target_type": "journal",
                "target_ref": "journal-1",
            },
        },
        headers=_auth(world.resolve_key),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "REVIEW_ACTION_NOT_PERMITTED"


def test_mapping_details_with_unknown_kind_is_422(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    created = _file_item(client, world.resolve_key, item_type="mapping")
    response = client.post(
        f"/api/v1/review-queue/items/{created['id']}/actions",
        json={
            "action": "complete_mapping_retry",
            "mapping": {
                "mapping_kind": "not_a_kind",
                "source_key": "zarinpal",
                "target_type": "journal",
                "target_ref": "journal-1",
            },
        },
        headers=_auth(world.resolve_key),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
