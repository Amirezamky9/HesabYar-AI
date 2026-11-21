"""REST tests for the scheduled-jobs routes (US-560, Phase 5-7 ۵خ W1).

No servers needed: the app is built with dependency overrides — a
canned authenticated client, the in-memory fake registry behind the
scheduler service, and a stub session (commit is a no-op). The
full-stack path (real DB) is covered by the migration tests +
contract tests.
"""

from __future__ import annotations

import datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from hesabyar.application.commands.scheduler import JobRunContext, SchedulerService
from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.infrastructure.config import AppSettings
from hesabyar.interfaces.rest.app import create_app
from hesabyar.interfaces.rest.dependencies import (
    get_authenticated_client,
    get_db_session,
    get_scheduler_service,
)
from tests.unit.test_installment_services import INSTALLATION_ID, TENANT_ID
from tests.unit.test_scheduler_service import FakeRegistry

NOW = datetime.datetime(2026, 11, 21, 10, 0, tzinfo=datetime.UTC)  # Saturday, 13:30 Tehran


class _Runner:
    def __init__(self, result: dict[str, Any] | None = None, error: str | None = None) -> None:
        self.result = result or {"ok": True}
        self.error = error
        self.calls: list[JobRunContext] = []

    def run(self, ctx: JobRunContext) -> dict[str, Any]:
        self.calls.append(ctx)
        if self.error is not None:
            raise RuntimeError(self.error)
        return dict(self.result)


class _FakeSession:
    """Commit/close are no-ops; the jobs routes never execute SQL here."""

    def commit(self) -> None:
        return None

    def close(self) -> None:
        return None


def _client(*, scopes: tuple[str, ...] = ("jobs:manage",)) -> AuthenticatedClient:
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
def api() -> TestClient:
    registry = FakeRegistry()
    service = SchedulerService(
        registry,
        runners={
            "settlement.tick": _Runner({"moved_to_due": 3}),
            "cheque.daily": _Runner(error="engine exploded"),
        },
        now=NOW,
    )
    app = create_app(settings=AppSettings())
    app.dependency_overrides[get_authenticated_client] = lambda: _client()
    app.dependency_overrides[get_db_session] = lambda: _FakeSession()
    app.dependency_overrides[get_scheduler_service] = lambda: service
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def api_without_scope() -> TestClient:
    app = create_app(settings=AppSettings())
    app.dependency_overrides[get_authenticated_client] = lambda: _client(
        scopes=("installments:read",)
    )
    app.dependency_overrides[get_db_session] = lambda: _FakeSession()
    app.dependency_overrides[get_scheduler_service] = lambda: SchedulerService(
        FakeRegistry(), now=NOW
    )
    return TestClient(app, raise_server_exceptions=False)


def test_list_jobs(api: TestClient) -> None:
    response = api.get("/api/v1/jobs")
    assert response.status_code == 200
    jobs = response.json()
    assert [job["id"] for job in jobs] == [
        "events.sweep",
        "settlement.tick",
        "packaging.supply_check",
        "cheque.daily",
        "sms.daily_reminders",
        "scheduler.dunning_sweep",
        "scheduler.store_sync",
        "scheduler.inbox_staleness",
        "scheduler.recurring_generation",
    ]
    tick = next(job for job in jobs if job["id"] == "settlement.tick")
    assert tick["queue"] == "settlement"
    assert tick["enabled"] is True
    assert tick["schedule"]["kind"] == "daily"
    assert tick["schedule"]["time_of_day"] == "09:30"
    assert tick["schedule"]["skip_friday"] is True
    assert tick["last_run"] is None
    assert tick["last_outcome"] is None
    # Saturday 13:30 Tehran; the tick is daily 09:30 -> tomorrow.
    assert tick["next_run"] == "2026-11-22T09:30:00+03:30"
    assert tick["next_run_clamped"] is False
    sync = next(job for job in jobs if job["id"] == "scheduler.store_sync")
    assert sync["schedule"]["kind"] == "interval"
    assert sync["schedule"]["interval_minutes"] == 15
    assert sync["next_run"] == "2026-11-21T13:45:00+03:30"


def test_get_job(api: TestClient) -> None:
    response = api.get("/api/v1/jobs/cheque.daily")
    assert response.status_code == 200
    assert response.json()["id"] == "cheque.daily"
    assert response.json()["queue"] == "cheques"


def test_get_unknown_job_is_404(api: TestClient) -> None:
    response = api.get("/api/v1/jobs/no.such.job")
    assert response.status_code == 404
    assert response.json()["code"] == "SCHEDULED_JOB_NOT_FOUND"


def test_list_job_runs(api: TestClient) -> None:
    assert api.get("/api/v1/jobs/settlement.tick/runs").json() == []
    triggered = api.post("/api/v1/jobs/settlement.tick/trigger")
    assert triggered.status_code == 200
    runs = api.get("/api/v1/jobs/settlement.tick/runs").json()
    assert len(runs) == 1
    assert runs[0]["id"] == triggered.json()["id"]
    assert runs[0]["outcome"] == "success"
    assert runs[0]["triggered_by"] == "manual"


def test_trigger_job_runs_and_returns_the_run(api: TestClient) -> None:
    response = api.post("/api/v1/jobs/settlement.tick/trigger")
    assert response.status_code == 200
    run = response.json()
    assert run["job_id"] == "settlement.tick"
    assert run["outcome"] == "success"
    assert run["summary"] == {"moved_to_due": 3}
    assert run["error"] is None
    assert run["triggered_by"] == "manual"
    # The list view now shows the last run.
    jobs = api.get("/api/v1/jobs").json()
    tick = next(job for job in jobs if job["id"] == "settlement.tick")
    assert tick["last_outcome"] == "success"
    assert tick["last_run"]["id"] == run["id"]


def test_trigger_failure_returns_the_failed_run(api: TestClient) -> None:
    response = api.post("/api/v1/jobs/cheque.daily/trigger")
    assert response.status_code == 200  # the run happened and failed; here is the record
    run = response.json()
    assert run["outcome"] == "failed"
    assert run["error"] == "engine exploded"


def test_trigger_unknown_job_is_404(api: TestClient) -> None:
    response = api.post("/api/v1/jobs/no.such.job/trigger")
    assert response.status_code == 404
    assert response.json()["code"] == "SCHEDULED_JOB_NOT_FOUND"


def test_trigger_without_runner_is_503(api: TestClient) -> None:
    response = api.post("/api/v1/jobs/sms.daily_reminders/trigger")
    assert response.status_code == 503
    assert response.json()["code"] == "SCHEDULED_JOB_NOT_RUNNABLE"


def test_enable_disable_round_trip(api: TestClient) -> None:
    disabled = api.post("/api/v1/jobs/cheque.daily/disable")
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    enabled = api.post("/api/v1/jobs/cheque.daily/enable")
    assert enabled.status_code == 200
    assert enabled.json()["enabled"] is True
    unknown = api.post("/api/v1/jobs/no.such.job/disable")
    assert unknown.status_code == 404


def test_jobs_endpoints_require_jobs_manage_scope(api_without_scope: TestClient) -> None:
    assert api_without_scope.get("/api/v1/jobs").status_code == 403
    assert api_without_scope.get("/api/v1/jobs/settlement.tick").status_code == 403
    assert api_without_scope.post("/api/v1/jobs/settlement.tick/trigger").status_code == 403
    assert api_without_scope.post("/api/v1/jobs/settlement.tick/disable").status_code == 403
