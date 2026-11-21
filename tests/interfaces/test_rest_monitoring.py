"""REST tests for the monitoring routes (US-704, Phase 7).

No servers needed: the app is built with dependency overrides — a
canned authenticated client, the in-memory fake monitoring
repository behind the real monitoring service, and a stub session
(commit is a no-op). The full-stack path (real DB) is covered by
the migration tests + contract tests.

The fake repository's time-sensitive reads are anchored to the real
clock at fixture time (the router calls ``overview()`` without a
``now``), so worker lag / token days-left stay deterministic.
"""

from __future__ import annotations

import datetime

import pytest
from fastapi.testclient import TestClient

from hesabyar.application.commands.monitoring import MonitoringService
from hesabyar.domain.monitoring.metrics import request_metrics
from hesabyar.domain.monitoring.models import (
    BackupStatus,
    BackupStatusValue,
    MonitoringThresholds,
    TokenKind,
    TokenRecord,
)
from hesabyar.domain.monitoring.ports import ApiKeyExpiry
from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.infrastructure.config import AppSettings
from hesabyar.interfaces.rest.app import create_app
from hesabyar.interfaces.rest.dependencies import (
    get_authenticated_client,
    get_db_session,
    get_monitoring_service,
)
from tests.unit.test_installment_services import INSTALLATION_ID, TENANT_ID
from tests.unit.test_monitoring_service import (
    FakeBackupSource,
    FakeHealthChecks,
    FakeMcpProber,
    FakeMonitoringRepository,
)


class _FakeSession:
    """Commit/close are no-ops; the monitoring routes never execute SQL here."""

    def commit(self) -> None:
        return None

    def close(self) -> None:
        return None


def _client(*, scopes: tuple[str, ...] = ("monitoring:read", "monitoring:manage")) -> AuthenticatedClient:
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


def _repo() -> FakeMonitoringRepository:
    now = datetime.datetime.now(datetime.UTC)
    repo = FakeMonitoringRepository()
    repo.last_sweep = now - datetime.timedelta(minutes=2)
    repo.api_keys = [
        ApiKeyExpiry(
            key_id="key-1",
            key_prefix="hya_live_ab12",
            name="کلید انبار",
            expires_at=now + datetime.timedelta(days=2),
        )
    ]
    repo.token_records["rec-fintech"] = TokenRecord(
        id="rec-fintech",
        kind=TokenKind.FINTECH,
        label="توکن فینوتک",
        expires_at=now + datetime.timedelta(days=10),
    )
    return repo


def _service(repo: FakeMonitoringRepository) -> MonitoringService:
    # The backup source mirrors a fresh install: no backup run
    # recorded yet, so UNKNOWN for both databases.
    backups = FakeBackupSource(
        statuses_list=[
            BackupStatus(database="hesabyar", status=BackupStatusValue.UNKNOWN),
            BackupStatus(database="tryton", status=BackupStatusValue.UNKNOWN),
        ]
    )
    return MonitoringService(
        repo,
        thresholds=MonitoringThresholds(),
        backup_source=backups,
        mcp_prober=FakeMcpProber(),
        health_checks=FakeHealthChecks(),
    )


@pytest.fixture()
def api() -> TestClient:
    # One service (one repo) per fixture: state must persist across
    # the test's requests, so the override returns the SAME instance.
    service = _service(_repo())
    app = create_app(settings=AppSettings())
    app.dependency_overrides[get_authenticated_client] = lambda: _client()
    app.dependency_overrides[get_db_session] = lambda: _FakeSession()
    app.dependency_overrides[get_monitoring_service] = lambda: service
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def api_without_scope() -> TestClient:
    service = _service(_repo())
    app = create_app(settings=AppSettings())
    app.dependency_overrides[get_authenticated_client] = lambda: _client(scopes=("installments:read",))
    app.dependency_overrides[get_db_session] = lambda: _FakeSession()
    app.dependency_overrides[get_monitoring_service] = lambda: service
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def api_stale() -> TestClient:
    """A client whose repo has one stale inbox event (evaluate fires)."""
    repo = _repo()
    now = datetime.datetime.now(datetime.UTC)
    repo.stale = (1, now - datetime.timedelta(hours=25))
    service = _service(repo)
    app = create_app(settings=AppSettings())
    app.dependency_overrides[get_authenticated_client] = lambda: _client()
    app.dependency_overrides[get_db_session] = lambda: _FakeSession()
    app.dependency_overrides[get_monitoring_service] = lambda: service
    return TestClient(app, raise_server_exceptions=False)


def test_overview(api: TestClient) -> None:
    response = api.get("/api/v1/monitoring/overview")
    assert response.status_code == 200
    body = response.json()
    assert [process["name"] for process in body["processes"]] == [
        "api",
        "database",
        "tryton",
        "worker",
        "mcp",
    ]
    worker = next(p for p in body["processes"] if p["name"] == "worker")
    assert worker["status"] == "ok"
    assert body["queue_depths"]["inbox_received"] == 2
    assert body["queue_depths"]["outbox_pending"] == 3
    assert body["queue_depths"]["procrastinate_todo"] == {"events": 1, "scheduler": 0}
    assert body["stale_inbox_count"] == 0
    assert body["worker_lag_minutes"] == 2
    # No request metrics wired into the fake service: honestly null.
    assert body["error_rate_5xx"] is None
    assert [token["label"] for token in body["token_expiries"]] == [
        "کلید انبار (hya_live_ab12)",
        "توکن فینوتک",
    ]
    assert body["token_expiries"][0]["severity"] == "critical"
    assert body["token_expiries"][1]["severity"] == "warning"
    # No backup source until US-703 lands: UNKNOWN, never fabricated.
    assert [backup["database"] for backup in body["backups"]] == ["hesabyar", "tryton"]
    assert all(backup["status"] == "unknown" for backup in body["backups"])
    assert all(backup["source"] == "none" for backup in body["backups"])


def test_overview_requires_read_scope(api_without_scope: TestClient) -> None:
    assert api_without_scope.get("/api/v1/monitoring/overview").status_code == 403


def test_evaluate_fires_and_lists_alerts(api_stale: TestClient) -> None:
    response = api_stale.post("/api/v1/monitoring/evaluate")
    assert response.status_code == 200
    body = response.json()
    assert len(body["fired"]) == 1
    assert body["fired"][0]["rule"] == "inbox_stale"
    assert body["fired"][0]["severity"] == "critical"
    assert body["fired"][0]["status"] == "open"
    assert body["already_open"] == 0
    assert body["dispatched"] == []  # no channels configured

    listed = api_stale.get("/api/v1/monitoring/alerts")
    assert listed.status_code == 200
    assert len(listed.json()) == 1

    second = api_stale.post("/api/v1/monitoring/evaluate")
    assert second.json()["fired"] == []
    assert second.json()["already_open"] == 1


def test_evaluate_requires_manage_scope(api_without_scope: TestClient) -> None:
    assert api_without_scope.post("/api/v1/monitoring/evaluate").status_code == 403


def test_alerts_filters_and_lifecycle(api_stale: TestClient) -> None:
    fired = api_stale.post("/api/v1/monitoring/evaluate").json()["fired"][0]
    alert_id = fired["id"]

    assert api_stale.get("/api/v1/monitoring/alerts?status=open").json() != []
    assert api_stale.get("/api/v1/monitoring/alerts?status=resolved").json() == []
    assert api_stale.get("/api/v1/monitoring/alerts?rule=inbox_stale").json() != []
    assert api_stale.get("/api/v1/monitoring/alerts?rule=worker_lag").json() == []
    assert api_stale.get("/api/v1/monitoring/alerts?limit=0").status_code == 422

    acked = api_stale.post(f"/api/v1/monitoring/alerts/{alert_id}/acknowledge")
    assert acked.status_code == 200
    assert acked.json()["status"] == "acknowledged"
    assert acked.json()["acknowledged_by"] == "test client"

    resolved = api_stale.post(f"/api/v1/monitoring/alerts/{alert_id}/resolve")
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "resolved"
    assert resolved.json()["resolved_by"] == "test client"

    unknown = api_stale.post("/api/v1/monitoring/alerts/nope/acknowledge")
    assert unknown.status_code == 404
    assert unknown.json()["code"] == "MONITORING_NOT_FOUND"


def test_channels_crud(api: TestClient) -> None:
    assert api.get("/api/v1/monitoring/channels").json() == []

    created = api.post(
        "/api/v1/monitoring/channels",
        json={
            "kind": "webhook",
            "name": "وب‌هوک عملیات",
            "target": "https://hooks.example.com/alerts",
        },
    )
    assert created.status_code == 201
    channel = created.json()
    assert channel["kind"] == "webhook"
    assert channel["enabled"] is True
    assert channel["min_severity"] == "warning"

    # An SMS channel defaults its installation to the caller's.
    sms = api.post(
        "/api/v1/monitoring/channels",
        json={
            "kind": "sms",
            "name": "پیامک مدیر",
            "target": "09123456789",
            "provider_key": "simulator",
        },
    )
    assert sms.status_code == 201
    assert sms.json()["installation_id"] == INSTALLATION_ID

    listed = api.get("/api/v1/monitoring/channels").json()
    assert len(listed) == 2

    updated = api.put(
        f"/api/v1/monitoring/channels/{channel['id']}",
        json={
            "kind": "webhook",
            "name": "وب‌هوک جدید",
            "target": "https://hooks.example.com/other",
            "min_severity": "critical",
        },
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "وب‌هوک جدید"
    assert updated.json()["min_severity"] == "critical"

    deleted = api.delete(f"/api/v1/monitoring/channels/{channel['id']}")
    assert deleted.status_code == 204
    assert api.delete(f"/api/v1/monitoring/channels/{channel['id']}").status_code == 404
    assert len(api.get("/api/v1/monitoring/channels").json()) == 1


def test_create_channel_validation_error(api: TestClient) -> None:
    response = api.post(
        "/api/v1/monitoring/channels",
        json={
            "kind": "webhook",
            "name": "x",
            "target": "http://hooks.example.com/plain",  # http refused
        },
    )
    assert response.status_code == 422
    assert response.json()["code"] == "MONITORING_VALIDATION_FAILED"


def test_tokens_crud(api: TestClient) -> None:
    listed = api.get("/api/v1/monitoring/tokens").json()
    assert len(listed) == 1
    assert listed[0]["label"] == "توکن فینوتک"

    created = api.post(
        "/api/v1/monitoring/tokens",
        json={
            "kind": "other",
            "label": "توکن درگاه",
            "expires_at": "2030-01-01T00:00:00+00:00",
        },
    )
    assert created.status_code == 201
    record = created.json()
    assert record["kind"] == "other"
    assert record["enabled"] is True

    updated = api.put(
        f"/api/v1/monitoring/tokens/{record['id']}",
        json={
            "kind": "other",
            "label": "توکن درگاه جدید",
            "expires_at": "2030-06-01T00:00:00+00:00",
        },
    )
    assert updated.status_code == 200
    assert updated.json()["label"] == "توکن درگاه جدید"

    assert api.delete(f"/api/v1/monitoring/tokens/{record['id']}").status_code == 204
    assert api.delete(f"/api/v1/monitoring/tokens/{record['id']}").status_code == 404


def test_create_token_past_expiry_is_422(api: TestClient) -> None:
    response = api.post(
        "/api/v1/monitoring/tokens",
        json={
            "kind": "fintech",
            "label": "توکن قدیمی",
            "expires_at": "2020-01-01T00:00:00+00:00",
        },
    )
    assert response.status_code == 422
    assert response.json()["code"] == "MONITORING_VALIDATION_FAILED"


def test_request_metrics_middleware_counts(api: TestClient) -> None:
    """The 5xx middleware records into the process-wide counters."""
    request_metrics.reset()
    assert api.get("/api/v1/monitoring/overview").status_code == 200
    assert api.get("/api/v1/monitoring/overview").status_code == 200
    assert api.get("/api/v1/no-such-route").status_code == 404
    snapshot = request_metrics.snapshot()
    assert snapshot.total == 3
    assert snapshot.server_errors == 0
    assert snapshot.error_rate == 0.0
    request_metrics.reset()
