"""REST tests for the rules-engine routes (US-561, Phase 5-7 ۵خ W2).

No servers needed: the app is built with dependency overrides — a
canned authenticated client, the in-memory fake repository behind
the rules service, a fake inbox for past-event dry-runs, and a stub
session (commit is a no-op). The full-stack path (real DB) is
covered by the migration tests + contract tests.
"""

from __future__ import annotations

import datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from hesabyar.application.commands.rules import RuleActionExecutor, RuleEngineService
from hesabyar.domain.events.envelope import canonical_payload_hash
from hesabyar.domain.events.models import InboxEvent, InboxEventStatus, StoreSettings
from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.infrastructure.config import AppSettings
from hesabyar.interfaces.rest.app import create_app
from hesabyar.interfaces.rest.dependencies import (
    get_authenticated_client,
    get_db_session,
    get_event_inbox_repository,
    get_rule_engine_service,
    get_store_settings_repository,
)
from tests.unit.test_installment_services import INSTALLATION_ID, TENANT_ID
from tests.unit.test_rules_engine import FakeNotifications
from tests.unit.test_rules_service import FakeRuleRepository

NOW = datetime.datetime(2026, 11, 21, 10, 0, tzinfo=datetime.UTC)

NOTIFY_ACTION = {
    "kind": "notify",
    "params": {
        "recipients": ["role:accountant"],
        "channels": ["in_app"],
        "subject": "Big order",
        "message": "Amount {{amount_irr}}",
    },
}

RULE_BODY = {
    "name": "Notify on big orders",
    "description": "Ping accounting for orders over a million Rial.",
    "event_type": "sales.order.completed",
    "conditions": {">=": [{"var": "amount_irr"}, 1_000_000]},
    "action": NOTIFY_ACTION,
    "priority": 10,
    "enabled": True,
}


class _FakeSession:
    """Commit/close are no-ops; the rules routes never execute SQL here."""

    def commit(self) -> None:
        return None

    def close(self) -> None:
        return None


class _FakeInbox:
    """One past inbox event (Toman amounts — the router normalizes them)."""

    def __init__(self) -> None:
        self.events: dict[str, InboxEvent] = {}
        occurred = datetime.datetime(2026, 11, 20, 10, 0, tzinfo=datetime.UTC)
        payload = {"total_amount": 500_000, "payment_channel": "zarinpal"}
        self.events["evt-past-1"] = InboxEvent(
            id="evt-past-1",
            tenant_id=TENANT_ID,
            schema_version=1,
            source_system="hayat",
            source_store_id="store-1",
            source_event_id="order-1",
            event_type="sales.order.completed",
            occurred_at=occurred,
            received_at=occurred,
            company_mapping=None,
            currency="IRT",
            external_entity_type="order",
            external_entity_id="order-1",
            correlation_id=None,
            idempotency_key="key-1",
            payload=payload,
            payload_hash=canonical_payload_hash(payload),
            status=InboxEventStatus.PROCESSED,
            attempts=1,
            last_attempt_at=occurred,
            next_attempt_at=None,
            last_error=None,
            processed_at=occurred,
            dead_lettered_at=None,
            created_at=occurred,
            updated_at=occurred,
            installation_id=INSTALLATION_ID,
        )

    def get(self, event_id: str, tenant_id: str | None = None) -> InboxEvent | None:
        event = self.events.get(event_id)
        if event is None:
            return None
        if tenant_id is not None and event.tenant_id != tenant_id:
            return None
        return event


class _FakeStoreSettings:
    """No stored settings — the router falls back to the Toman default."""

    def get(self, source_system: str, source_store_id: str) -> StoreSettings | None:
        return None


def _client(*, scopes: tuple[str, ...] = ("rules:read", "rules:manage")) -> AuthenticatedClient:
    return AuthenticatedClient(
        client_id="test-client",
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        key_id="test-key",
        key_prefix="hya_live_test1234",
        client_name="test client",
        scopes=frozenset(scopes),
        engine_company_id=None,
    )


@pytest.fixture()
def world() -> dict[str, Any]:
    repository = FakeRuleRepository()
    notifications = FakeNotifications()
    executor = RuleActionExecutor(notifications=notifications, now=NOW)
    service = RuleEngineService(repository, executor=executor, now=NOW)
    return {
        "repository": repository,
        "notifications": notifications,
        "service": service,
    }


@pytest.fixture()
def api(world: dict[str, Any]) -> TestClient:
    app = create_app(settings=AppSettings())
    app.dependency_overrides[get_authenticated_client] = lambda: _client()
    app.dependency_overrides[get_db_session] = lambda: _FakeSession()
    app.dependency_overrides[get_rule_engine_service] = lambda: world["service"]
    app.dependency_overrides[get_event_inbox_repository] = lambda: _FakeInbox()
    app.dependency_overrides[get_store_settings_repository] = lambda: _FakeStoreSettings()
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def api_read_only(world: dict[str, Any]) -> TestClient:
    app = create_app(settings=AppSettings())
    app.dependency_overrides[get_authenticated_client] = lambda: _client(scopes=("rules:read",))
    app.dependency_overrides[get_db_session] = lambda: _FakeSession()
    app.dependency_overrides[get_rule_engine_service] = lambda: world["service"]
    app.dependency_overrides[get_event_inbox_repository] = lambda: _FakeInbox()
    app.dependency_overrides[get_store_settings_repository] = lambda: _FakeStoreSettings()
    return TestClient(app, raise_server_exceptions=False)


def _create_rule(api: TestClient, body: dict[str, Any] | None = None) -> dict[str, Any]:
    response = api.post("/api/v1/rules", json=body or RULE_BODY)
    assert response.status_code == 201, response.text
    return response.json()


def test_create_and_get_rule(api: TestClient) -> None:
    created = _create_rule(api)
    assert created["name"] == "Notify on big orders"
    assert created["event_type"] == "sales.order.completed"
    assert created["action"]["kind"] == "notify"
    assert created["priority"] == 10
    assert created["enabled"] is True
    assert created["tenant_id"] == TENANT_ID
    got = api.get(f"/api/v1/rules/{created['id']}")
    assert got.status_code == 200
    assert got.json()["id"] == created["id"]


def test_create_rule_with_unknown_action_is_422(api: TestClient) -> None:
    body = dict(RULE_BODY, action={"kind": "execute_python_code", "params": {}})
    response = api.post("/api/v1/rules", json=body)
    assert response.status_code == 422
    assert response.json()["code"] == "RULE_INVALID"


def test_create_rule_with_unknown_event_type_is_422(api: TestClient) -> None:
    body = dict(RULE_BODY, event_type="order.made_up")
    response = api.post("/api/v1/rules", json=body)
    assert response.status_code == 422
    assert response.json()["code"] == "RULE_INVALID"


def test_get_unknown_rule_is_404(api: TestClient) -> None:
    response = api.get("/api/v1/rules/does-not-exist")
    assert response.status_code == 404
    assert response.json()["code"] == "RULE_NOT_FOUND"


def test_list_rules_and_filters(api: TestClient) -> None:
    _create_rule(api)
    _create_rule(api, dict(RULE_BODY, name="Second", priority=5, enabled=False))
    listed = api.get("/api/v1/rules")
    assert listed.status_code == 200
    assert [r["name"] for r in listed.json()] == ["Second", "Notify on big orders"]
    enabled = api.get("/api/v1/rules", params={"enabled": True})
    assert [r["name"] for r in enabled.json()] == ["Notify on big orders"]
    other_type = api.get("/api/v1/rules", params={"event_type": "payment.captured"})
    assert other_type.json() == []


def test_update_rule(api: TestClient) -> None:
    created = _create_rule(api)
    response = api.put(
        f"/api/v1/rules/{created['id']}",
        json={"name": "Renamed", "priority": 1},
    )
    assert response.status_code == 200
    assert response.json()["name"] == "Renamed"
    assert response.json()["priority"] == 1
    assert response.json()["event_type"] == "sales.order.completed"


def test_delete_rule(api: TestClient) -> None:
    created = _create_rule(api)
    response = api.delete(f"/api/v1/rules/{created['id']}")
    assert response.status_code == 204
    assert api.get(f"/api/v1/rules/{created['id']}").status_code == 404


def test_enable_disable_round_trip(api: TestClient) -> None:
    created = _create_rule(api)
    disabled = api.post(f"/api/v1/rules/{created['id']}/disable")
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    enabled = api.post(f"/api/v1/rules/{created['id']}/enable")
    assert enabled.json()["enabled"] is True


def test_dry_run_sample_payload(api: TestClient, world: dict[str, Any]) -> None:
    created = _create_rule(api)
    response = api.post(
        f"/api/v1/rules/{created['id']}/dry-run",
        json={
            "event_type": "sales.order.completed",
            "payload": {"total_amount": 5_000_000, "payment_channel": "zarinpal"},
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["matched"] is True
    assert body["decision"] == "would_execute"
    assert body["would_do"] == "Notify role:accountant via in_app."
    assert body["execution_id"]
    # No side effects: nothing was notified.
    assert world["notifications"].sent == []


def test_dry_run_against_a_past_event_normalizes_toman(api: TestClient) -> None:
    # The past event's payload carries 500,000 Toman; the router
    # normalizes it to 5,000,000 Rial before evaluating — so the
    # >= 1,000,000 Rial condition matches.
    created = _create_rule(api)
    response = api.post(
        f"/api/v1/rules/{created['id']}/dry-run",
        json={"event_id": "evt-past-1"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["matched"] is True


def test_dry_run_unknown_event_is_404(api: TestClient) -> None:
    created = _create_rule(api)
    response = api.post(
        f"/api/v1/rules/{created['id']}/dry-run",
        json={"event_id": "no-such-event"},
    )
    assert response.status_code == 404
    assert response.json()["code"] == "EVENT_NOT_FOUND"


def test_dry_run_needs_an_event_or_a_payload(api: TestClient) -> None:
    created = _create_rule(api)
    response = api.post(f"/api/v1/rules/{created['id']}/dry-run", json={})
    assert response.status_code == 400
    response = api.post(
        f"/api/v1/rules/{created['id']}/dry-run",
        json={
            "event_id": "evt-past-1",
            "event_type": "sales.order.completed",
            "payload": {},
        },
    )
    assert response.status_code == 400


def test_dry_run_definition(api: TestClient) -> None:
    response = api.post(
        "/api/v1/rules/dry-run-definition",
        json={
            "name": "Draft",
            "conditions": {">=": [{"var": "amount_irr"}, 1_000_000]},
            "action": NOTIFY_ACTION,
            "event_type": "sales.order.completed",
            "payload": {"total_amount": 100},
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["rule_id"] is None
    assert body["matched"] is False
    assert body["decision"] == "not_matched"


def test_evaluate_executes_matching_rules(api: TestClient, world: dict[str, Any]) -> None:
    _create_rule(api)
    response = api.post(
        "/api/v1/rules/evaluate",
        json={
            "event_type": "sales.order.completed",
            "payload": {"total_amount": 5_000_000, "payment_channel": "zarinpal"},
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["matched_rules"] == 1
    assert body["executions"][0]["mode"] == "live"
    assert body["executions"][0]["decision"] == "executed"
    assert body["executions"][0]["outcome"] == "notified"
    assert len(world["notifications"].sent) == 1


def test_execution_history(api: TestClient) -> None:
    created = _create_rule(api)
    api.post(
        f"/api/v1/rules/{created['id']}/dry-run",
        json={
            "event_type": "sales.order.completed",
            "payload": {"total_amount": 5_000_000},
        },
    )
    api.post(
        "/api/v1/rules/evaluate",
        json={
            "event_type": "sales.order.completed",
            "payload": {"total_amount": 5_000_000},
        },
    )
    history = api.get("/api/v1/rules/executions")
    assert history.status_code == 200
    assert len(history.json()) == 2
    assert history.json()[0]["mode"] == "live"  # newest first
    per_rule = api.get(f"/api/v1/rules/{created['id']}/executions")
    assert len(per_rule.json()) == 2
    one = api.get(f"/api/v1/rules/executions/{history.json()[0]['id']}")
    assert one.status_code == 200
    assert api.get("/api/v1/rules/executions/nope").status_code == 404
    assert api.get("/api/v1/rules/nope/executions").status_code == 404


def test_rule_set_mode_endpoints(api: TestClient) -> None:
    assert api.get("/api/v1/rules/sets").json() == []
    response = api.put(
        "/api/v1/rules/sets/sales.order.completed",
        json={"match_mode": "all_matches"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["match_mode"] == "all_matches"
    assert len(api.get("/api/v1/rules/sets").json()) == 1
    bad = api.put(
        "/api/v1/rules/sets/sales.order.completed",
        json={"match_mode": "random"},
    )
    assert bad.status_code == 422
    assert bad.json()["code"] == "RULE_INVALID"


def test_rules_endpoints_require_scopes(api_read_only: TestClient) -> None:
    # Reads work with rules:read alone.
    assert api_read_only.get("/api/v1/rules").status_code == 200
    assert api_read_only.get("/api/v1/rules/executions").status_code == 200
    assert api_read_only.get("/api/v1/rules/sets").status_code == 200
    # Writes need rules:manage.
    assert api_read_only.post("/api/v1/rules", json=RULE_BODY).status_code == 403
    assert api_read_only.put("/api/v1/rules/x", json={"name": "y"}).status_code == 403
    assert api_read_only.delete("/api/v1/rules/x").status_code == 403
    assert api_read_only.post("/api/v1/rules/x/enable").status_code == 403
    assert (
        api_read_only.post(
            "/api/v1/rules/x/dry-run",
            json={"event_type": "sales.order.completed", "payload": {}},
        ).status_code
        == 403
    )
    assert (
        api_read_only.post(
            "/api/v1/rules/evaluate",
            json={"event_type": "sales.order.completed", "payload": {}},
        ).status_code
        == 403
    )
    assert (
        api_read_only.put(
            "/api/v1/rules/sets/sales.order.completed",
            json={"match_mode": "all_matches"},
        ).status_code
        == 403
    )


def test_openapi_lists_rules_paths() -> None:
    app = create_app(settings=AppSettings())
    client = TestClient(app)
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/v1/rules" in paths
    assert "/api/v1/rules/{rule_id}" in paths
    assert "/api/v1/rules/{rule_id}/dry-run" in paths
    assert "/api/v1/rules/evaluate" in paths
    assert "/api/v1/rules/executions" in paths
    assert "/api/v1/rules/sets/{event_type}" in paths
