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


def test_list_types_returns_all_nine_with_actions(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get("/api/v1/review-queue/types", headers=_auth(world.read_key))
    assert response.status_code == 200
    types = response.json()
    assert [t["item_type"] for t in types] == ALL_TYPES
    for entry in types:
        assert len(entry["actions"]) >= 1
        assert all("code" in action and "kind" in action for action in entry["actions"])


# ---------------------------------------------------------------------------
# POST + GET /review-queue/items — the nine types, filters, sort, paging
# ---------------------------------------------------------------------------


def test_api_returns_all_nine_item_types(settings: AppSettings, world: World) -> None:
    """Exit criterion (phased design §1.2): one sample item per type, all returned."""
    client = _test_client(settings)
    for item_type in ALL_TYPES:
        _file_item(client, world.resolve_key, item_type=item_type)
    response = client.get("/api/v1/review-queue/items", headers=_auth(world.read_key))
    assert response.status_code == 200
    body = response.json()
    assert body["next_cursor"] is None
    assert len(body["items"]) == 9
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


def test_perform_retry_action_moves_to_in_review(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    created = _file_item(client, world.resolve_key, item_type="inbox_dead_letter", severity="critical")
    response = client.post(
        f"/api/v1/review-queue/items/{created['id']}/actions",
        json={"action": "replay"},
        headers=_auth(world.resolve_key),
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "in_review"
    assert response.json()["resolution"] is None


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


def test_summary_covers_all_nine_types(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    _file_item(client, world.resolve_key, item_type="mapping")
    _file_item(client, world.resolve_key, item_type="mapping")
    response = client.get("/api/v1/review-queue/summary", headers=_auth(world.read_key))
    assert response.status_code == 200
    body = response.json()
    assert len(body["counters"]) == 9
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
    assert len(policies) == 9
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
