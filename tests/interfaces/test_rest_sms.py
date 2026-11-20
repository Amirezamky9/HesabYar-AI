"""REST tests for the SMS routes (US-544, Phase 5-5 ۵ج).

No servers needed: the app is built with dependency overrides —
a canned authenticated client, the in-memory fakes from
``tests.unit.test_sms_services`` behind the registry/query/
reminder services, and a stub session (commit is a no-op). The
full-stack path (real DB) is covered by the contract tests.

Date-robustness: the manual trigger computes its offset against
the REAL today in these tests, so leg due dates are far-past
(2021-03-21, always overdue) or far-future (2031-03-21, always
expected) — never near today. Assertions use the offset's sign,
never its exact value.
"""

from __future__ import annotations

import datetime

import pytest
from fastapi.testclient import TestClient

from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.infrastructure.config import AppSettings
from hesabyar.interfaces.rest.app import create_app
from hesabyar.interfaces.rest.dependencies import (
    get_authenticated_client,
    get_db_session,
    get_sms_query_service,
    get_sms_registry_service,
    get_sms_reminder_service,
)
from tests.unit.test_installment_services import INSTALLATION_ID, TENANT_ID
from tests.unit.test_sms_services import World

ALL_SMS_SCOPES = ("sms:manage", "installments:read")
PHONE = "09123456789"

#: Far-past / far-future due dates (always overdue / always expected).
PAST_DUE = datetime.date(2021, 3, 21)  # 1400/01/01
FUTURE_DUE = datetime.date(2031, 3, 21)  # 1410/01/01


class _FakeSession:
    """Commit/close are no-ops; the SMS routes never execute SQL here."""

    def commit(self) -> None:
        return None

    def close(self) -> None:
        return None


def _client(*, scopes: tuple[str, ...] = ALL_SMS_SCOPES) -> AuthenticatedClient:
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
def world() -> World:
    return World()


@pytest.fixture()
def api(world: World) -> TestClient:
    app = create_app(settings=AppSettings())
    app.dependency_overrides[get_authenticated_client] = lambda: _client()
    app.dependency_overrides[get_db_session] = lambda: _FakeSession()
    app.dependency_overrides[get_sms_registry_service] = lambda: world.registry()
    app.dependency_overrides[get_sms_query_service] = lambda: world.query_service()
    app.dependency_overrides[get_sms_reminder_service] = lambda: world.reminders()
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def api_read_only(world: World) -> TestClient:
    """A client with installments:read only (no sms:manage)."""
    app = create_app(settings=AppSettings())
    app.dependency_overrides[get_authenticated_client] = lambda: _client(
        scopes=("installments:read",)
    )
    app.dependency_overrides[get_db_session] = lambda: _FakeSession()
    app.dependency_overrides[get_sms_registry_service] = lambda: world.registry()
    app.dependency_overrides[get_sms_query_service] = lambda: world.query_service()
    app.dependency_overrides[get_sms_reminder_service] = lambda: world.reminders()
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------


def test_seed_providers_is_idempotent(api: TestClient) -> None:
    first = api.post("/api/v1/integrations/sms/providers/seed")
    assert first.status_code == 201
    providers = first.json()
    assert [p["provider_key"] for p in providers] == ["kavenegar", "sms_ir", "simulator"]
    simulator = next(p for p in providers if p["provider_key"] == "simulator")
    assert simulator["enabled"] is True
    assert simulator["is_default"] is True
    assert simulator["has_credentials"] is False
    assert "credentials" not in simulator
    assert "api_key" not in first.text

    second = api.post("/api/v1/integrations/sms/providers/seed")
    assert second.status_code == 201
    assert [p["id"] for p in second.json()] == [p["id"] for p in providers]


def test_seed_requires_sms_manage_scope(api_read_only: TestClient) -> None:
    response = api_read_only.post("/api/v1/integrations/sms/providers/seed")
    assert response.status_code == 403


def test_create_provider_round_trip(api: TestClient) -> None:
    created = api.post(
        "/api/v1/integrations/sms/providers",
        json={
            "provider_key": "my-sms",
            "display_name_fa": "پیامک من",
            "provider_kind": "custom",
            "price_per_sms_toman": "125.5",
            "line_type": "service",
            "contract_required": True,
        },
    )
    assert created.status_code == 201
    body = created.json()
    assert body["provider_key"] == "my-sms"
    assert body["enabled"] is False
    assert body["status"] == "setup"
    assert body["price_per_sms_toman"] == "125.5"
    assert body["contract_required"] is True
    assert "credentials" not in body

    listed = api.get("/api/v1/integrations/sms/providers")
    assert listed.status_code == 200
    assert [p["provider_key"] for p in listed.json()] == ["my-sms"]

    fetched = api.get(f"/api/v1/integrations/sms/providers/{body['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["display_name_fa"] == "پیامک من"

    missing = api.get("/api/v1/integrations/sms/providers/no-such-id")
    assert missing.status_code == 404
    assert missing.json()["code"] == "SMS_PROVIDER_NOT_FOUND"


def test_create_provider_duplicate_key_is_409(api: TestClient) -> None:
    api.post("/api/v1/integrations/sms/providers/seed")
    response = api.post(
        "/api/v1/integrations/sms/providers",
        json={
            "provider_key": "kavenegar",
            "display_name_fa": "دوباره",
            "provider_kind": "kavenegar",
        },
    )
    assert response.status_code == 409
    assert response.json()["code"] == "SMS_PROVIDER_KEY_CONFLICT"


def test_create_provider_validation_is_422(api: TestClient) -> None:
    response = api.post(
        "/api/v1/integrations/sms/providers",
        json={
            "provider_key": "BAD KEY",
            "display_name_fa": "x",
            "provider_kind": "telegram",
        },
    )
    assert response.status_code == 422
    assert response.json()["code"] == "SMS_VALIDATION_FAILED"


def test_create_provider_with_credentials_never_returns_them(api: TestClient) -> None:
    response = api.post(
        "/api/v1/integrations/sms/providers",
        json={
            "provider_key": "kavenegar",
            "display_name_fa": "کاوه‌نگار",
            "provider_kind": "kavenegar",
            "credentials": {"api_key": "s3cr3t-key"},
        },
    )
    assert response.status_code == 201
    assert response.json()["has_credentials"] is True
    assert "s3cr3t-key" not in response.text


def test_update_and_delete_provider(api: TestClient) -> None:
    api.post("/api/v1/integrations/sms/providers/seed")
    listed = api.get("/api/v1/integrations/sms/providers").json()
    kavenegar = next(p for p in listed if p["provider_key"] == "kavenegar")
    simulator = next(p for p in listed if p["provider_key"] == "simulator")

    updated = api.patch(
        f"/api/v1/integrations/sms/providers/{kavenegar['id']}",
        json={"display_name_fa": "کاوه‌نگار جدید", "enabled": True},
    )
    assert updated.status_code == 200
    assert updated.json()["display_name_fa"] == "کاوه‌نگار جدید"
    assert updated.json()["enabled"] is True

    # The default provider cannot be deleted.
    refused = api.delete(f"/api/v1/integrations/sms/providers/{simulator['id']}")
    assert refused.status_code == 409
    assert refused.json()["code"] == "SMS_PROVIDER_IN_USE"

    deleted = api.delete(f"/api/v1/integrations/sms/providers/{kavenegar['id']}")
    assert deleted.status_code == 204
    assert api.get(f"/api/v1/integrations/sms/providers/{kavenegar['id']}").status_code == 404

    missing = api.delete("/api/v1/integrations/sms/providers/no-such-id")
    assert missing.status_code == 404


# ---------------------------------------------------------------------------
# Reminder settings + contract opt-ins
# ---------------------------------------------------------------------------


def test_settings_defaults_then_put(api: TestClient) -> None:
    defaults = api.get("/api/v1/integrations/sms/settings")
    assert defaults.status_code == 200
    assert defaults.json()["enabled"] is False
    assert defaults.json()["offsets_days_before"] == [3, 0]
    assert defaults.json()["offsets_days_overdue"] == [1]

    saved = api.put(
        "/api/v1/integrations/sms/settings",
        json={
            "enabled": True,
            "offsets_days_before": [5, 0],
            "offsets_days_overdue": [2],
            "template_before_due": "مانده {amount_toman} تومان",
            "template_overdue": "معوق {amount_toman} تومان",
        },
    )
    assert saved.status_code == 200
    assert saved.json()["enabled"] is True
    assert saved.json()["offsets_days_before"] == [0, 5]

    invalid = api.put(
        "/api/v1/integrations/sms/settings",
        json={
            "enabled": True,
            "offsets_days_before": [],
            "offsets_days_overdue": [],
            "template_before_due": "x",
            "template_overdue": "y",
        },
    )
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "SMS_VALIDATION_FAILED"


def test_contract_opt_in_defaults_then_put(api: TestClient, world: World) -> None:
    world.make_contract()
    defaults = api.get("/api/v1/integrations/sms/contracts/contract-544/reminders")
    assert defaults.status_code == 200
    assert defaults.json()["id"] is None
    assert defaults.json()["reminders_enabled"] is False

    saved = api.put(
        "/api/v1/integrations/sms/contracts/contract-544/reminders",
        json={"reminders_enabled": True},
    )
    assert saved.status_code == 200
    assert saved.json()["reminders_enabled"] is True
    assert saved.json()["id"] is not None

    fetched = api.get("/api/v1/integrations/sms/contracts/contract-544/reminders")
    assert fetched.json()["reminders_enabled"] is True

    missing = api.put(
        "/api/v1/integrations/sms/contracts/no-such-contract/reminders",
        json={"reminders_enabled": True},
    )
    assert missing.status_code == 404
    assert missing.json()["code"] == "INSTALLMENT_CONTRACT_NOT_FOUND"


# ---------------------------------------------------------------------------
# Manual trigger + reminder log
# ---------------------------------------------------------------------------


def _seed_plan(world: World, *, due: datetime.date, plan_id: str = "plan-544") -> None:
    world.seed()
    world.make_plan(
        plan_id=plan_id,
        legs=[("leg-1", 1, due, 10_000_000)],
    )


def test_remind_sends_via_simulator(api: TestClient, world: World) -> None:
    _seed_plan(world, due=FUTURE_DUE)
    response = api.post("/api/v1/installments/plans/plan-544/remind", json={})
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "sent"
    assert body["trigger"] == "manual"
    assert body["provider_key"] == "simulator"
    assert body["offset_days"] < 0  # far-future due date
    assert body["recipient_phone"] == PHONE
    assert "۱۴۱۰/۰۱/۰۱" in body["message_text"]  # Jalali due date, Persian digits
    assert "۱۰۰۰۰۰۰" in body["message_text"]  # 1,000,000 Toman remaining balance
    assert "روز مانده" in body["message_text"]

    # The log read (installments:read) shows it.
    listed = api.get("/api/v1/installments/plans/plan-544/reminders")
    assert listed.status_code == 200
    assert len(listed.json()) == 1
    assert listed.json()[0]["id"] == body["id"]

    # The global log (sms:manage) shows it too, with filters.
    global_log = api.get("/api/v1/integrations/sms/reminders?status=sent&trigger=manual")
    assert global_log.status_code == 200
    assert len(global_log.json()) == 1
    filtered_out = api.get("/api/v1/integrations/sms/reminders?status=failed")
    assert filtered_out.json() == []

    # A second send for the same leg + offset is a 409.
    again = api.post("/api/v1/installments/plans/plan-544/remind", json={})
    assert again.status_code == 409
    assert again.json()["code"] == "SMS_REMINDER_ALREADY_SENT"


def test_remind_overdue_leg_uses_overdue_template(api: TestClient, world: World) -> None:
    _seed_plan(world, due=PAST_DUE)
    response = api.post("/api/v1/installments/plans/plan-544/remind", json={})
    assert response.status_code == 201
    body = response.json()
    assert body["offset_days"] > 0  # far-past due date
    assert "۱۴۰۰/۰۱/۰۱" in body["message_text"]
    assert "معوق" in body["message_text"]


def test_remind_dry_run_previews_without_sending(api: TestClient, world: World) -> None:
    _seed_plan(world, due=FUTURE_DUE)
    response = api.post(
        "/api/v1/installments/plans/plan-544/remind", json={"dry_run": True}
    )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "dry_run"
    assert body["provider_message_id"] is None
    assert "۱۴۱۰/۰۱/۰۱" in body["message_text"]
    # The dry run does not block the real send.
    real = api.post("/api/v1/installments/plans/plan-544/remind", json={})
    assert real.status_code == 201
    assert real.json()["status"] == "sent"


def test_remind_missing_plan_is_404(api: TestClient, world: World) -> None:
    world.seed()
    response = api.post("/api/v1/installments/plans/no-such-plan/remind", json={})
    assert response.status_code == 404
    assert response.json()["code"] == "INSTALLMENT_PLAN_NOT_FOUND"


def test_remind_requires_sms_manage_scope(api_read_only: TestClient, world: World) -> None:
    _seed_plan(world, due=FUTURE_DUE)
    response = api_read_only.post("/api/v1/installments/plans/plan-544/remind", json={})
    assert response.status_code == 403
    # ...but the per-plan log read works with installments:read.
    listed = api_read_only.get("/api/v1/installments/plans/plan-544/reminders")
    assert listed.status_code == 200


def test_reminders_list_rejects_unknown_params_and_status(api: TestClient) -> None:
    response = api.get("/api/v1/integrations/sms/reminders?bogus=1")
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_FILTER"

    response = api.get("/api/v1/integrations/sms/reminders?status=nope")
    assert response.status_code == 422
    assert response.json()["code"] == "SMS_VALIDATION_FAILED"

    response = api.get("/api/v1/installments/plans/plan-544/reminders?status=nope")
    assert response.status_code == 422


def test_remind_no_phone_is_422(api: TestClient, world: World) -> None:
    world.seed()
    world.make_plan(plan_id="plan-nophone", customer_ref=None)
    response = api.post("/api/v1/installments/plans/plan-nophone/remind", json={})
    assert response.status_code == 422
    assert response.json()["code"] == "SMS_RECIPIENT_PHONE_MISSING"

    # A phone override fixes it.
    response = api.post(
        "/api/v1/installments/plans/plan-nophone/remind",
        json={"recipient_phone": "+989121111111"},
    )
    assert response.status_code == 201
    assert response.json()["recipient_phone"] == "09121111111"
