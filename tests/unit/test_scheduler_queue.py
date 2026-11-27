"""Unit tests for the scheduler's Procrastinate wiring (US-560).

Uses ``procrastinate.testing.InMemoryConnector`` — no database, no
server. Covers: task registration (names, queues, crons), the
cron↔Tehran drift guard (the registry's ``time_of_day`` is Tehran
wall time; the Procrastinate crons are UTC — the two must agree),
the run recorder (gate / record / notify), and the three new tasks
running end-to-end through the worker.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest
from procrastinate.testing import InMemoryConnector

from hesabyar.application.commands.events import (
    EventPipelineService,
    EventWorker,
    StalenessService,
)
from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.application.commands.scheduled_jobs import ScheduledJobsService
from hesabyar.domain.backup.ports import DumpResult, PgToolsInfo
from hesabyar.domain.events.models import InboxEventStatus
from hesabyar.domain.scheduler.models import DEFAULT_JOBS, JobOutcome
from hesabyar.domain.scheduler.schedules import ScheduleKind
from hesabyar.infrastructure.auth.credential_vault import generate_encryption_key
from hesabyar.infrastructure.config import BackupSettings
from hesabyar.infrastructure.events.backup_queue import (
    BACKUP_CRON,
    BACKUP_QUEUE,
    BACKUP_TASK,
    RESTORE_DRILL_CRON,
    RESTORE_DRILL_TASK,
    BackupWorkerHandle,
)
from hesabyar.infrastructure.events.cheque_queue import DAILY_CRON, DAILY_TASK
from hesabyar.infrastructure.events.defaults import InProcessOutboxPublisher
from hesabyar.infrastructure.events.monitoring_queue import (
    EVALUATION_CRON,
    MONITORING_EVALUATE_TASK,
)
from hesabyar.infrastructure.events.packaging_queue import (
    SUPPLY_CHECK_CRON,
    SUPPLY_CHECK_TASK,
)
from hesabyar.infrastructure.events.procrastinate_queue import (
    PROCESS_EVENT_TASK,
    SWEEP_CRON,
    SWEEP_TASK,
    WorkerHandle,
    create_event_app,
)
from hesabyar.infrastructure.events.reminders_queue import (
    DAILY_CRON as REMINDERS_DAILY_CRON,
)
from hesabyar.infrastructure.events.reminders_queue import (
    DAILY_TASK as REMINDERS_DAILY_TASK,
)
from hesabyar.infrastructure.events.scheduler_queue import (
    DUNNING_SWEEP_CRON,
    DUNNING_SWEEP_TASK,
    INBOX_STALENESS_CRON,
    INBOX_STALENESS_TASK,
    RECURRING_GENERATION_CRON,
    RECURRING_GENERATION_TASK,
    SCHEDULER_QUEUE,
    STORE_SYNC_CRON,
    STORE_SYNC_TASK,
    JobRunRecorder,
    RecorderHandle,
    SchedulerWorkerHandle,
    run_recorded,
)
from hesabyar.infrastructure.events.settlement_queue import TICK_CRON, TICK_TASK
from hesabyar.infrastructure.events.sms_queue import (
    DAILY_REMINDERS_CRON,
    DAILY_REMINDERS_TASK,
)
from tests.event_fixtures import (
    TENANT_ID,
    FakeAuditLog,
    FakeDraftBuilder,
    FakeEngine,
    FakeExternalIdMap,
    FakeInboxRepository,
    FakeMappingResolver,
    FakeOutboxRepository,
    FakePolicyGate,
    FakeReviewQueue,
    FakeStoreSettings,
    ingest_into,
    must_get,
)
from tests.unit.test_scheduled_jobs import (
    INSTALLATION_ID,
    FakeConnectorFactory,
    FakeFinance,
    FakeInventory,
    FakeStoreService,
    FakeSync,
    FakeTenantResolver,
)
from tests.unit.test_scheduler_service import FakeAudit, FakeRegistry
from tests.unit.test_scheduler_service import (
    FakeReviewQueue as SchedulerFakeReviewQueue,
)

NOW = datetime.datetime(2026, 11, 21, 10, 0, tzinfo=datetime.UTC)  # Saturday
FRIDAY = datetime.date(2026, 11, 20)
SATURDAY = datetime.date(2026, 11, 21)

#: job id -> the Procrastinate cron that fires it (UTC).
JOB_CRONS = {
    SWEEP_TASK: SWEEP_CRON,
    TICK_TASK: TICK_CRON,
    SUPPLY_CHECK_TASK: SUPPLY_CHECK_CRON,
    DAILY_TASK: DAILY_CRON,
    DAILY_REMINDERS_TASK: DAILY_REMINDERS_CRON,
    DUNNING_SWEEP_TASK: DUNNING_SWEEP_CRON,
    STORE_SYNC_TASK: STORE_SYNC_CRON,
    INBOX_STALENESS_TASK: INBOX_STALENESS_CRON,
    REMINDERS_DAILY_TASK: REMINDERS_DAILY_CRON,
    RECURRING_GENERATION_TASK: RECURRING_GENERATION_CRON,
    BACKUP_TASK: BACKUP_CRON,
    RESTORE_DRILL_TASK: RESTORE_DRILL_CRON,
    MONITORING_EVALUATE_TASK: EVALUATION_CRON,
}


def _make_worker(inbox: FakeInboxRepository, outbox: FakeOutboxRepository) -> EventWorker:
    pipeline = EventPipelineService(
        inbox=inbox,
        outbox=outbox,
        external_ids=FakeExternalIdMap(),
        audit_log=FakeAuditLog(),
        store_settings=FakeStoreSettings(),
        mapping_resolver=FakeMappingResolver(),
        draft_builder=FakeDraftBuilder(),
        policy_gate=FakePolicyGate(allowed=False),
        review_queue=FakeReviewQueue(),
        engine=FakeEngine(),
    )
    return EventWorker(
        pipeline=pipeline,
        inbox=inbox,
        outbox=outbox,
        publisher=InProcessOutboxPublisher(),
    )


def _worker_factory(inbox: FakeInboxRepository, outbox: FakeOutboxRepository) -> WorkerHandle:
    return WorkerHandle(
        worker=_make_worker(inbox, outbox), commit=lambda: None, close=lambda: None
    )


class _BackupFakeDumper:
    """Dumps fixed bytes per database; records the dump calls (US-703)."""

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def check_tools(self) -> PgToolsInfo:
        return PgToolsInfo(
            pg_dump_path="pg_dump",
            pg_dump_version="16.4",
            pg_restore_path="pg_restore",
            pg_restore_version="16.4",
        )

    def dump(self, database_url: str) -> DumpResult:
        self.calls.append(database_url)
        database = database_url.rsplit("/", 1)[-1]
        return DumpResult(
            data=f"pg_dump of {database}".encode(),
            pg_dump_version="16.4",
            database=database,
        )

    def restore(self, database_url: str, data: bytes, *, no_owner: bool) -> None:
        raise NotImplementedError

    def create_database(self, admin_url: str, name: str) -> None:
        raise NotImplementedError

    def drop_database(self, admin_url: str, name: str) -> None:
        raise NotImplementedError


def _backup_factory(
    tmp_path: Path,
    dumps: list[str] | None = None,
    commits: list[int] | None = None,
    closes: list[int] | None = None,
) -> BackupWorkerHandle:
    """A backup handle writing encrypted backups into ``tmp_path`` (US-703)."""
    return BackupWorkerHandle(
        settings=BackupSettings(
            local_directory=str(tmp_path),
            encryption_key=generate_encryption_key(),
        ),
        dumper=_BackupFakeDumper(dumps if dumps is not None else []),  # type: ignore[arg-type]
        engine_factory=None,
        hesabyar_database_url="postgresql://u:p@h/hesabyar_test",
        tryton_database_url="postgresql://u:p@h/hesabyar_tryton_test",
        commit=lambda: (commits if commits is not None else []).append(1),
        close=lambda: (closes if closes is not None else []).append(1),
    )


def _utc_cron_to_tehran(cron: str) -> tuple[str, datetime.time | int]:
    """Parse a simple UTC cron into (kind, Tehran time or interval minutes).

    Handles only the shapes this codebase uses: ``* * * * *`` (every
    minute), ``*/N * * * *`` (every N minutes), ``0 * * * *``
    (hourly), and ``M H * * *`` (daily at H:M UTC).
    """
    minute, hour, *_rest = cron.split()
    if minute == "*" and hour == "*":
        return "interval", 1
    if minute.startswith("*/"):
        return "interval", int(minute[2:])
    if minute == "0" and hour == "*":
        return "interval", 60
    utc_time = datetime.time(int(hour), int(minute))
    tehran = (
        datetime.datetime.combine(datetime.date(2026, 1, 1), utc_time)
        + datetime.timedelta(hours=3, minutes=30)
    ).time()
    return "daily", tehran


def test_scheduler_tasks_are_registered_under_their_contract_names() -> None:
    inbox = FakeInboxRepository()
    outbox = FakeOutboxRepository()
    app = create_event_app(
        InMemoryConnector(),
        lambda: _worker_factory(inbox, outbox),
        scheduler_factory=lambda: SchedulerWorkerHandle(
            jobs=None,  # type: ignore[arg-type]
            commit=lambda: None,
            close=lambda: None,
        ),
    )
    for task in (
        DUNNING_SWEEP_TASK,
        STORE_SYNC_TASK,
        INBOX_STALENESS_TASK,
        RECURRING_GENERATION_TASK,
    ):
        assert task in app.tasks
        assert app.tasks[task].queue == SCHEDULER_QUEUE
    assert DUNNING_SWEEP_CRON == "30 4 * * *"  # daily 08:00 Tehran
    assert STORE_SYNC_CRON == "*/15 * * * *"  # every 15 minutes
    assert INBOX_STALENESS_CRON == "0 * * * *"  # hourly
    assert RECURRING_GENERATION_CRON == "0 4 * * *"  # daily 07:30 Tehran
    # The one-shot task is NOT a registry job (no schedule).
    assert PROCESS_EVENT_TASK in app.tasks
    assert PROCESS_EVENT_TASK not in {job.id for job in DEFAULT_JOBS}


def test_backup_tasks_are_registered_under_their_contract_names(tmp_path: Path) -> None:
    inbox = FakeInboxRepository()
    outbox = FakeOutboxRepository()
    app = create_event_app(
        InMemoryConnector(),
        lambda: _worker_factory(inbox, outbox),
        backup_factory=lambda: _backup_factory(tmp_path),
    )
    for task in (BACKUP_TASK, RESTORE_DRILL_TASK):
        assert task in app.tasks
        assert app.tasks[task].queue == BACKUP_QUEUE
    assert BACKUP_CRON == "30 22 * * *"  # daily 02:00 Tehran
    assert RESTORE_DRILL_CRON == "30 23 * * *"  # daily 03:00 Tehran (runs on the 1st)
    # Both ARE registry jobs (unlike the one-shot PROCESS_EVENT_TASK).
    assert {BACKUP_TASK, RESTORE_DRILL_TASK} <= {job.id for job in DEFAULT_JOBS}


def test_backup_tasks_are_not_registered_without_a_backup_factory() -> None:
    inbox = FakeInboxRepository()
    outbox = FakeOutboxRepository()
    app = create_event_app(InMemoryConnector(), lambda: _worker_factory(inbox, outbox))
    assert BACKUP_TASK not in app.tasks
    assert RESTORE_DRILL_TASK not in app.tasks


def test_registry_schedules_match_the_procrastinate_crons() -> None:
    """Drift guard: Tehran time_of_day == UTC cron + 3:30, per job."""
    assert set(JOB_CRONS) == {job.id for job in DEFAULT_JOBS}
    for job in DEFAULT_JOBS:
        kind, value = _utc_cron_to_tehran(JOB_CRONS[job.id])
        if job.schedule.kind is ScheduleKind.INTERVAL:
            assert kind == "interval", job.id
            assert job.schedule.interval_minutes == value, job.id
        else:
            assert kind == "daily", job.id
            assert job.schedule.time_of_day == value, (
                f"{job.id}: registry {job.schedule.time_of_day} != "
                f"cron {JOB_CRONS[job.id]} (Tehran {value})"
            )


def _recorder(
    registry: FakeRegistry,
    *,
    inventory: FakeInventory | None = None,
    queue: SchedulerFakeReviewQueue | None = None,
    review: ReviewQueueCommandService | None = None,
) -> tuple[JobRunRecorder, list[int]]:
    commits: list[int] = []

    def factory() -> RecorderHandle:
        return RecorderHandle(
            registry=registry,
            inventory=inventory or FakeInventory(),
            review=review,
            queue=queue,
            commit=lambda: commits.append(1),
            close=lambda: None,
        )

    return JobRunRecorder(factory, now=lambda: NOW), commits


def test_recorder_runs_and_records_success() -> None:
    registry = FakeRegistry()
    recorder, _commits = _recorder(registry)
    calls: list[str] = []

    def _do() -> dict[str, int]:
        calls.append("ran")
        return {"moved_to_due": 3}

    result = recorder.run_recorded("settlement.tick", _do, today=SATURDAY)
    assert result == {"moved_to_due": 3}
    assert calls == ["ran"]
    run = registry.latest_run("settlement.tick")
    assert run is not None
    assert run.outcome is JobOutcome.SUCCESS
    assert run.summary == {"moved_to_due": 3}
    assert run.started_at == NOW
    # The check seeded the registry.
    assert registry.get_definition("settlement.tick") is not None


def test_recorder_skips_disabled_job() -> None:
    registry = FakeRegistry()
    recorder, _commits = _recorder(registry)
    recorder.run_recorded("settlement.tick", lambda: {"ok": True}, today=SATURDAY)
    registry.set_enabled("settlement.tick", False)
    calls: list[str] = []
    result = recorder.run_recorded(
        "settlement.tick", lambda: calls.append("ran") or {}, today=SATURDAY
    )
    assert result is None
    assert calls == []
    run = registry.latest_run("settlement.tick")
    assert run is not None
    assert run.outcome is JobOutcome.SKIPPED
    assert run.summary == {"reason": "disabled"}


def test_recorder_skips_daily_job_on_friday() -> None:
    registry = FakeRegistry()
    recorder, _commits = _recorder(registry)
    calls: list[str] = []
    result = recorder.run_recorded(
        "settlement.tick", lambda: calls.append("ran") or {}, today=FRIDAY
    )
    assert result is None
    assert calls == []
    run = registry.latest_run("settlement.tick")
    assert run is not None
    assert run.outcome is JobOutcome.SKIPPED
    assert run.summary == {"reason": "not a scheduled day"}


def test_recorder_runs_interval_job_on_friday() -> None:
    registry = FakeRegistry()
    recorder, _commits = _recorder(registry)
    result = recorder.run_recorded(
        "scheduler.store_sync", lambda: {"stores_synced": 0}, today=FRIDAY
    )
    assert result == {"stores_synced": 0}
    run = registry.latest_run("scheduler.store_sync")
    assert run is not None
    assert run.outcome is JobOutcome.SUCCESS


def test_recorder_records_failure_files_review_item_and_reraises() -> None:
    registry = FakeRegistry()
    inventory = FakeInventory()
    inventory.tenants = [(TENANT_ID, INSTALLATION_ID)]
    queue = SchedulerFakeReviewQueue()
    review = ReviewQueueCommandService(queue, FakeAudit())
    recorder, _commits = _recorder(
        registry, inventory=inventory, queue=queue, review=review
    )

    def _boom() -> dict[str, int]:
        raise RuntimeError("engine exploded")

    with pytest.raises(RuntimeError, match="engine exploded"):
        recorder.run_recorded("settlement.tick", _boom, today=SATURDAY)
    run = registry.latest_run("settlement.tick")
    assert run is not None
    assert run.outcome is JobOutcome.FAILED
    assert run.error == "engine exploded"
    assert len(queue.items) == 1
    assert (
        queue.items[0].source_event_ref
        == "scheduled-job:settlement.tick:failure:2026-11-21"
    )


def test_recorder_fails_open_when_registry_unreachable() -> None:
    class _BrokenRegistry(FakeRegistry):
        def ensure_defaults(self, defaults):  # type: ignore[no-untyped-def]
            raise RuntimeError("database down")

        def record_run(self, run):  # type: ignore[no-untyped-def]
            raise RuntimeError("database down")

    registry = _BrokenRegistry()
    recorder, _commits = _recorder(registry)
    # The job still runs; the registry gap is a warning, not a blocker.
    result = recorder.run_recorded("settlement.tick", lambda: {"ok": True}, today=SATURDAY)
    assert result == {"ok": True}


def test_run_recorded_without_recorder_runs_bare() -> None:
    calls: list[str] = []
    result = run_recorded(None, "settlement.tick", lambda: calls.append("ran") or {"ok": 1}, today=FRIDAY)
    assert result == {"ok": 1}
    assert calls == ["ran"]  # no Friday check without a recorder


def test_scheduler_tasks_run_through_the_worker() -> None:
    """US-560: the three new tasks execute via Procrastinate."""
    inbox = FakeInboxRepository()
    outbox = FakeOutboxRepository()
    finance = FakeFinance()
    inventory = FakeInventory()
    inventory.plans = [(TENANT_ID, INSTALLATION_ID)]
    sync = FakeSync()
    commits = 0
    closes = 0

    def scheduler_factory() -> SchedulerWorkerHandle:
        nonlocal commits, closes

        def commit() -> None:
            nonlocal commits
            commits += 1

        def close() -> None:
            nonlocal closes
            closes += 1

        jobs = ScheduledJobsService(
            finance=finance,  # type: ignore[arg-type]
            inventory=inventory,
            sync=sync,  # type: ignore[arg-type]
            connector_factory=FakeConnectorFactory(),  # type: ignore[arg-type]
            store_service=FakeStoreService(),  # type: ignore[arg-type]
            staleness=StalenessService(inbox),
            review=None,
            queue=None,
            tenant_resolver=FakeTenantResolver({INSTALLATION_ID: TENANT_ID}),  # type: ignore[arg-type]
        )
        return SchedulerWorkerHandle(jobs=jobs, commit=commit, close=close)

    app = create_event_app(
        InMemoryConnector(),
        lambda: _worker_factory(inbox, outbox),
        scheduler_factory=scheduler_factory,
    )
    app.tasks[DUNNING_SWEEP_TASK].defer()
    app.tasks[STORE_SYNC_TASK].defer()
    app.tasks[INBOX_STALENESS_TASK].defer()
    app.run_worker(wait=False)
    # The dunning sweep ran (the periodic scheduler may fire tasks
    # again, so >= 1, like the sweep test).
    assert len(finance.calls) >= 1
    assert finance.calls[0] == (TENANT_ID, INSTALLATION_ID)
    assert commits >= 3
    assert closes == commits


def test_backup_task_runs_through_the_worker(tmp_path: Path) -> None:
    """US-703: the backup task dumps both databases into the local store."""
    inbox = FakeInboxRepository()
    outbox = FakeOutboxRepository()
    registry = FakeRegistry()
    recorder, _commits = _recorder(registry)
    dumps: list[str] = []
    commits: list[int] = []
    closes: list[int] = []
    app = create_event_app(
        InMemoryConnector(),
        lambda: _worker_factory(inbox, outbox),
        backup_factory=lambda: _backup_factory(tmp_path, dumps, commits, closes),
        recorder=recorder,
    )
    app.tasks[BACKUP_TASK].defer()
    app.run_worker(wait=False)
    # Both databases were dumped (the periodic scheduler may fire the
    # task again, so >= 1 run per target).
    assert dumps.count("postgresql://u:p@h/hesabyar_test") >= 1
    assert dumps.count("postgresql://u:p@h/hesabyar_tryton_test") >= 1
    # The objects landed in the local store: one dump + one manifest
    # per target per run.
    files = [path for path in tmp_path.rglob("*") if path.is_file()]
    encrypted = [path for path in files if path.suffix == ".enc"]
    manifests = [path for path in files if path.name.endswith(".manifest.json")]
    assert len(encrypted) >= 2
    assert len(manifests) >= 2
    assert {path.parent.parent.name for path in encrypted} == {"hesabyar", "tryton"}
    # The run was recorded as a success and the handle closed.
    runs = registry.list_runs(BACKUP_TASK)
    assert len(runs) >= 1
    assert all(run.outcome is JobOutcome.SUCCESS for run in runs)
    assert len(commits) >= 1
    assert closes == commits


def test_sweep_still_processes_events_with_recorder_wired() -> None:
    """The recorder must not change the sweep's behaviour (US-501)."""
    inbox = FakeInboxRepository()
    outbox = FakeOutboxRepository()
    registry = FakeRegistry()
    recorder, _commits = _recorder(registry)
    app = create_event_app(
        InMemoryConnector(),
        lambda: _worker_factory(inbox, outbox),
        recorder=recorder,
    )
    event_id = ingest_into(inbox)
    app.tasks[SWEEP_TASK].defer()
    app.run_worker(wait=False)
    assert must_get(inbox, event_id).status == InboxEventStatus.PROCESSED
    # The sweep ran (possibly more than once via the periodic
    # scheduler) and recorded its runs.
    runs = registry.list_runs(SWEEP_TASK)
    assert len(runs) >= 1
    assert all(run.outcome is JobOutcome.SUCCESS for run in runs)
