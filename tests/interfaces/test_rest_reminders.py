"""REST tests for the reminder routes (US-564, Phase 5-7 ۵خ W3).

No servers needed: the app is built with dependency overrides —
a canned authenticated client, the in-memory fakes from
``tests.unit.test_reminder_service`` behind the reminder
services, and a stub session (commit is a no-op). The
full-stack path (real DB) is covered by the contract tests.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.infrastructure.config import AppSettings
from hesabyar.interfaces.rest.app import create_app
from hesabyar.interfaces.rest.dependencies import (
    get_authenticated_client,
    get_db_session,
    get_reminder_query_service,
    get_reminder_service,
)
from tests.unit.test_installment_services import INSTALLATION_ID, TENANT_ID
from tests.unit.test_reminder_service import (
    NOW,
    TODAY,
    ReminderWorld,
    _seed_cheque_world,
)

ALL_REMINDER_SCOPES = ("reminders:manage",)


class _FakeSession:
    """Commit/close are no-ops; the reminder routes never execute SQL here."""

    def commit(self) -> None:
        return None

    def close(self) -> None:
        return None


def _client(*, scopes: tuple[str, ...] = ALL_REMINDER_SCOPES) -> AuthenticatedClient:
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
def world() -> ReminderWorld:
    return ReminderWorld()


@pytest.fixture()
def api(world: ReminderWorld) -> TestClient:
    app = create_app(settings=AppSettings())
    app.dependency_overrides[get_authenticated_client] = lambda: _client()
    app.dependency_overrides[get_db_session] = lambda: _FakeSession()
    app.dependency_overrides[get_reminder_service] = lambda: world.service()
    app.dependency_overrides[get_reminder_query_service] = lambda: world.query_service()
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def api_read_only(world: ReminderWorld) -> TestClient:
    """A client with installments:read only (no reminders:manage)."""
    app = create_app(settings=AppSettings())
    app.dependency_overrides[get_authenticated_client] = lambda: _client(
        scopes=("installments:read",)
    )
    app.dependency_overrides[get_db_session] = lambda: _FakeSession()
    app.dependency_overrides[get_reminder_service] = lambda: world.service()
    app.dependency_overrides[get_reminder_query_service] = lambda: world.query_service()
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------


def test_get_preferences_returns_defaults_when_never_saved(api: TestClient) -> None:
    response = api.get("/api/v1/reminders/preferences")
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False  # born disabled — reminders off
    assert body["cheque_due_soon_enabled"] is True
    assert body["cheque_lead_days"] == [7, 3]
    assert body["installment_overdue_days"] == [1, 7]
    assert body["cashbox_uncounted_days"] == 7
    assert body["enmad_lead_days"] == [30, 7, 0]
    assert body["channel_in_app"] is True
    assert body["channel_email"] is True
    assert body["channel_sms"] is True
    assert body["quiet_hours_enabled"] is True
    assert body["quiet_hours_start"] == "22:00:00"
    assert body["quiet_hours_end"] == "08:00:00"
    assert body["enmad_expiry_date"] is None
    assert body["notification_email"] is None


def test_put_preferences_round_trips(api: TestClient) -> None:
    response = api.put(
        "/api/v1/reminders/preferences",
        json={
            "enabled": True,
            "cheque_lead_days": [5],
            "cashbox_uncounted_days": 14,
            "quiet_hours_start": "23:00",
            "quiet_hours_end": "07:00",
            "enmad_expiry_date": "2026-12-20",
            "notification_email": "ops@example.com",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["cheque_lead_days"] == [5]
    assert body["cashbox_uncounted_days"] == 14
    assert body["quiet_hours_start"] == "23:00:00"
    assert body["enmad_expiry_date"] == "2026-12-20"
    assert body["notification_email"] == "ops@example.com"
    # Defaults still apply to the untouched fields.
    assert body["installment_overdue_days"] == [1, 7]

    fetched = api.get("/api/v1/reminders/preferences")
    assert fetched.status_code == 200
    assert fetched.json() == body


def test_put_preferences_with_only_enabled_uses_defaults(api: TestClient) -> None:
    response = api.put("/api/v1/reminders/preferences", json={"enabled": True})
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["cheque_lead_days"] == [7, 3]
    assert body["quiet_hours_enabled"] is True


def test_put_preferences_validation_error_is_422(api: TestClient) -> None:
    response = api.put(
        "/api/v1/reminders/preferences",
        json={"enabled": True, "cheque_lead_days": [99]},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "REMINDER_VALIDATION_FAILED"


def test_put_preferences_rejects_unknown_fields(api: TestClient) -> None:
    response = api.put(
        "/api/v1/reminders/preferences",
        json={"enabled": True, "bogus_field": True},
    )
    assert response.status_code == 422


def test_put_preferences_requires_enabled(api: TestClient) -> None:
    response = api.put("/api/v1/reminders/preferences", json={})
    assert response.status_code == 422


def test_preferences_require_reminders_manage_scope(api_read_only: TestClient) -> None:
    assert api_read_only.get("/api/v1/reminders/preferences").status_code == 403
    assert (
        api_read_only.put("/api/v1/reminders/preferences", json={"enabled": True}).status_code
        == 403
    )


# ---------------------------------------------------------------------------
# Reminder log
# ---------------------------------------------------------------------------


def test_get_log_empty(api: TestClient) -> None:
    response = api.get("/api/v1/reminders/log")
    assert response.status_code == 200
    assert response.json() == []


def test_get_log_after_a_job_run(api: TestClient, world: ReminderWorld) -> None:
    _seed_cheque_world(world)
    world.service().run_daily_reminder_job(today=TODAY, now=NOW)
    response = api.get("/api/v1/reminders/log")
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 3
    by_channel = {row["channel"]: row for row in rows}
    assert by_channel["in_app"]["status"] == "sent"
    assert by_channel["in_app"]["reminder_type"] == "cheque_due_soon"
    assert by_channel["in_app"]["subject_type"] == "cheque"
    assert by_channel["in_app"]["lead_days"] == 3
    assert by_channel["sms"]["status"] == "sent"
    assert by_channel["sms"]["provider_key"] == "simulator"
    assert by_channel["sms"]["recipient"] == "09123456789"
    assert by_channel["email"]["status"] == "skipped"
    assert by_channel["email"]["error_code"] == "EMAIL_NOT_CONFIGURED"
    # Idempotency keys are per channel.
    assert by_channel["sms"]["idempotency_key"].endswith(":sms")
    assert by_channel["in_app"]["idempotency_key"].endswith(":in_app")


def test_get_log_filters(api: TestClient, world: ReminderWorld) -> None:
    _seed_cheque_world(world)
    world.service().run_daily_reminder_job(today=TODAY, now=NOW)
    sms_only = api.get("/api/v1/reminders/log?channel=sms")
    assert sms_only.status_code == 200
    assert [row["channel"] for row in sms_only.json()] == ["sms"]

    sent_only = api.get("/api/v1/reminders/log?status=sent")
    assert sent_only.status_code == 200
    assert {row["channel"] for row in sent_only.json()} == {"in_app", "sms"}

    by_type = api.get("/api/v1/reminders/log?reminder_type=cheque_due_soon")
    assert by_type.status_code == 200
    assert len(by_type.json()) == 3

    by_subject = api.get("/api/v1/reminders/log?subject_id=cheque-1")
    assert by_subject.status_code == 200
    assert len(by_subject.json()) == 3

    other_type = api.get("/api/v1/reminders/log?reminder_type=enmad_expiry")
    assert other_type.status_code == 200
    assert other_type.json() == []


def test_get_log_rejects_unknown_params(api: TestClient) -> None:
    response = api.get("/api/v1/reminders/log?bogus=1")
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_FILTER"


def test_get_log_rejects_unknown_enum_values(api: TestClient) -> None:
    assert api.get("/api/v1/reminders/log?channel=pigeon").status_code == 422
    assert api.get("/api/v1/reminders/log?reminder_type=unknown").status_code == 422
    assert api.get("/api/v1/reminders/log?status=unknown").status_code == 422


def test_get_log_requires_reminders_manage_scope(api_read_only: TestClient) -> None:
    assert api_read_only.get("/api/v1/reminders/log").status_code == 403
