"""Unit tests for the job registry service (US-560).

In-memory fakes behind the ports — no database. The registry seeds
its defaults on first use; ``next_run`` is computed in-app from the
schedule + last run; a manual trigger ignores the calendar and the
enabled flag; a failed trigger is recorded (never raised away).
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from typing import Any

import pytest

from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.application.commands.scheduler import (
    JobRunContext,
    JobRunner,
    SchedulerService,
    notify_job_failure,
)
from hesabyar.application.errors import (
    ScheduledJobNotFoundError,
    ScheduledJobNotRunnableError,
)
from hesabyar.domain.review_queue.models import (
    ReviewAuditEntry,
    ReviewItem,
    ReviewItemType,
    SlaPolicy,
)
from hesabyar.domain.review_queue.ports import (
    ReviewAuditPort,
    ReviewItemFilters,
    ReviewQueuePort,
)
from hesabyar.domain.scheduler.models import (
    DEFAULT_JOBS,
    JobDefinition,
    JobOutcome,
    JobRun,
    TriggeredBy,
)
from hesabyar.domain.scheduler.ports import (
    JobRegistryPort,
    SchedulerInventoryPort,
    SyncCursorState,
)
from hesabyar.domain.scheduler.schedules import TEHRAN

NOW = datetime.datetime(2026, 11, 21, 10, 0, tzinfo=datetime.UTC)  # Saturday, 13:30 Tehran


class FakeRegistry(JobRegistryPort):
    """In-memory job registry."""

    def __init__(self) -> None:
        self.definitions: dict[str, JobDefinition] = {}
        self.runs: list[JobRun] = []

    def ensure_defaults(self, defaults: Sequence[JobDefinition]) -> None:
        for definition in defaults:
            self.definitions.setdefault(definition.id, definition)

    def list_definitions(self) -> Sequence[JobDefinition]:
        return list(self.definitions.values())

    def get_definition(self, job_id: str) -> JobDefinition | None:
        return self.definitions.get(job_id)

    def set_enabled(self, job_id: str, enabled: bool) -> JobDefinition | None:
        current = self.definitions.get(job_id)
        if current is None:
            return None
        updated = JobDefinition(
            id=current.id,
            queue=current.queue,
            schedule=current.schedule,
            enabled=enabled,
            description=current.description,
        )
        self.definitions[job_id] = updated
        return updated

    def record_run(self, run: JobRun) -> JobRun:
        self.runs.append(run)
        return run

    def latest_run(self, job_id: str) -> JobRun | None:
        matches = [run for run in self.runs if run.job_id == job_id]
        return matches[-1] if matches else None

    def list_runs(self, job_id: str, *, limit: int = 20) -> Sequence[JobRun]:
        matches = [run for run in self.runs if run.job_id == job_id]
        return list(reversed(matches))[:limit]


class FakeInventory(SchedulerInventoryPort):
    """In-memory scheduler inventory (tenants only — the service tests need no stores)."""

    def __init__(self, tenants: Sequence[tuple[str, str]] = ()) -> None:
        self.tenants = list(tenants)

    def list_installations_with_plans(self) -> Sequence[tuple[str, str]]:
        return []

    def list_active_stores(self) -> Sequence[Any]:
        return []

    def list_installation_tenants(self) -> Sequence[tuple[str, str]]:
        return list(self.tenants)

    def latest_source_store_id(self, tenant_id: str, source_system: str) -> str | None:
        return None

    def get_sync_cursor(
        self, tenant_id: str, source_system: str, source_store_id: str, stream: str
    ) -> SyncCursorState | None:
        return None

    def save_sync_cursor(
        self,
        tenant_id: str,
        source_system: str,
        source_store_id: str,
        stream: str,
        *,
        cursor_value: str | None,
        last_synced_at: datetime.datetime,
    ) -> None:
        return None


class FakeReviewQueue(ReviewQueuePort):
    """In-memory review queue (only what the failure notifier needs)."""

    def __init__(self) -> None:
        self.items: list[ReviewItem] = []

    def save_item(self, item: ReviewItem) -> ReviewItem:
        self.items.append(item)
        return item

    def get_item(self, item_id: str, tenant_id: str) -> ReviewItem | None:
        return next(
            (item for item in self.items if item.id == item_id and item.tenant_id == tenant_id),
            None,
        )

    def list_items(self, tenant_id: str, filters: ReviewItemFilters) -> Sequence[ReviewItem]:
        return [
            item
            for item in self.items
            if item.tenant_id == tenant_id
            and (filters.item_type is None or item.item_type == filters.item_type)
        ]

    def update_item(self, item: ReviewItem) -> ReviewItem:
        for index, existing in enumerate(self.items):
            if existing.id == item.id:
                self.items[index] = item
        return item

    def list_policies(self, tenant_id: str) -> Sequence[SlaPolicy]:
        return []

    def upsert_policy(self, tenant_id: str, policy: SlaPolicy) -> SlaPolicy:
        return policy


class FakeAudit(ReviewAuditPort):
    """Review audit sink (records entries, nothing else)."""

    def __init__(self) -> None:
        self.entries: list[ReviewAuditEntry] = []

    def record(self, entry: ReviewAuditEntry) -> ReviewAuditEntry:
        self.entries.append(entry)
        return entry

    def list_for_item(self, item_id: str, tenant_id: str) -> Sequence[ReviewAuditEntry]:
        return [entry for entry in self.entries if entry.item_id == item_id]


class _Runner(JobRunner):
    def __init__(self, result: dict[str, Any] | None = None, error: str | None = None) -> None:
        self.result = result or {"ok": True}
        self.error = error
        self.calls: list[JobRunContext] = []

    def run(self, ctx: JobRunContext) -> dict[str, Any]:
        self.calls.append(ctx)
        if self.error is not None:
            raise RuntimeError(self.error)
        return dict(self.result)


def _service(
    registry: FakeRegistry | None = None,
    runners: dict[str, JobRunner] | None = None,
    inventory: FakeInventory | None = None,
    review: ReviewQueueCommandService | None = None,
    queue: FakeReviewQueue | None = None,
) -> SchedulerService:
    return SchedulerService(
        registry or FakeRegistry(),
        runners=runners,
        inventory=inventory,
        review=review,
        queue=queue,
        now=NOW,
    )


def test_list_jobs_seeds_the_defaults() -> None:
    registry = FakeRegistry()
    service = _service(registry)
    jobs = service.list_jobs()
    assert [job.definition.id for job in jobs] == [job.id for job in DEFAULT_JOBS]
    # 10 jobs: the 8 US-560 defaults + reminders.daily (US-564) +
    # scheduler.recurring_generation (US-563).
    assert len(jobs) == 10
    # A second list does not duplicate or overwrite (an operator's
    # disable survives a re-seed).
    service.set_enabled("settlement.tick", enabled=False)
    jobs = service.list_jobs()
    assert len(jobs) == 10
    tick = next(job for job in jobs if job.definition.id == "settlement.tick")
    assert not tick.definition.enabled


def test_list_jobs_computes_next_run() -> None:
    service = _service()
    jobs = service.list_jobs()
    tick = next(job for job in jobs if job.definition.id == "settlement.tick")
    # Saturday 13:30 Tehran; the tick is daily 09:30 -> tomorrow 09:30.
    assert tick.next_run.run_at == datetime.datetime(2026, 11, 22, 9, 30, tzinfo=TEHRAN)
    assert tick.last_run is None
    assert tick.last_outcome is None
    sweep = next(job for job in jobs if job.definition.id == "events.sweep")
    assert sweep.next_run.run_at == datetime.datetime(2026, 11, 21, 13, 31, tzinfo=TEHRAN)


def test_next_run_uses_last_run_when_it_points_forward() -> None:
    registry = FakeRegistry()
    service = _service(registry)
    service.list_jobs()  # seed
    # The store sync ran 5 minutes ago (13:25 Tehran); the next run
    # is last run + 15 min = 13:40, not now + 15 min.
    registry.record_run(
        JobRun(
            id="run-1",
            job_id="scheduler.store_sync",
            started_at=datetime.datetime(2026, 11, 21, 9, 55, tzinfo=datetime.UTC),
            finished_at=datetime.datetime(2026, 11, 21, 9, 56, tzinfo=datetime.UTC),
            outcome=JobOutcome.SUCCESS,
            summary={},
            triggered_by=TriggeredBy.SCHEDULED,
        )
    )
    jobs = service.list_jobs()
    sync = next(job for job in jobs if job.definition.id == "scheduler.store_sync")
    assert sync.next_run.run_at == datetime.datetime(2026, 11, 21, 13, 40, tzinfo=TEHRAN)
    assert sync.last_outcome is JobOutcome.SUCCESS


def test_next_run_falls_back_to_now_when_last_run_is_stale() -> None:
    registry = FakeRegistry()
    service = _service(registry)
    service.list_jobs()  # seed
    registry.record_run(
        JobRun(
            id="run-1",
            job_id="scheduler.store_sync",
            started_at=datetime.datetime(2026, 11, 20, 9, 55, tzinfo=datetime.UTC),
            finished_at=datetime.datetime(2026, 11, 20, 9, 56, tzinfo=datetime.UTC),
            outcome=JobOutcome.SUCCESS,
            summary={},
            triggered_by=TriggeredBy.SCHEDULED,
        )
    )
    jobs = service.list_jobs()
    sync = next(job for job in jobs if job.definition.id == "scheduler.store_sync")
    # Last run + 15 min is in the past; next run is now + 15 min.
    assert sync.next_run.run_at == datetime.datetime(2026, 11, 21, 13, 45, tzinfo=TEHRAN)


def test_get_job_unknown_raises_404() -> None:
    service = _service()
    with pytest.raises(ScheduledJobNotFoundError):
        service.get_job("no.such.job")


def test_set_enabled_round_trip() -> None:
    service = _service()
    disabled = service.set_enabled("cheque.daily", enabled=False)
    assert not disabled.definition.enabled
    enabled = service.set_enabled("cheque.daily", enabled=True)
    assert enabled.definition.enabled
    with pytest.raises(ScheduledJobNotFoundError):
        service.set_enabled("no.such.job", enabled=False)


def test_trigger_runs_the_runner_and_records_success() -> None:
    registry = FakeRegistry()
    runner = _Runner({"moved_to_due": 3})
    service = _service(registry, runners={"settlement.tick": runner})
    run = service.trigger_job("settlement.tick", actor="operator-1")
    assert run.outcome is JobOutcome.SUCCESS
    assert run.summary == {"moved_to_due": 3}
    assert run.triggered_by is TriggeredBy.MANUAL
    assert run.started_at == NOW
    assert len(runner.calls) == 1
    assert runner.calls[0].actor == "operator-1"
    assert runner.calls[0].today == datetime.date(2026, 11, 21)  # Tehran date
    assert registry.latest_run("settlement.tick") is run


def test_trigger_ignores_disabled_and_friday() -> None:
    registry = FakeRegistry()
    runner = _Runner()
    service = _service(registry, runners={"settlement.tick": runner})
    service.set_enabled("settlement.tick", enabled=False)
    # A manual trigger is an explicit operator action: it runs even
    # for a disabled job, and even on a Friday.
    run = service.trigger_job(
        "settlement.tick", actor="operator-1", today=datetime.date(2026, 11, 20)
    )
    assert run.outcome is JobOutcome.SUCCESS
    assert len(runner.calls) == 1
    assert runner.calls[0].today == datetime.date(2026, 11, 20)


def test_trigger_unknown_job_raises_404() -> None:
    service = _service()
    with pytest.raises(ScheduledJobNotFoundError):
        service.trigger_job("no.such.job", actor="operator-1")


def test_trigger_without_runner_raises_not_runnable() -> None:
    service = _service()
    with pytest.raises(ScheduledJobNotRunnableError):
        service.trigger_job("settlement.tick", actor="operator-1")


def test_trigger_failure_is_recorded_not_raised() -> None:
    registry = FakeRegistry()
    queue = FakeReviewQueue()
    review = ReviewQueueCommandService(queue, FakeAudit())
    inventory = FakeInventory([("tenant-1", "inst-1"), ("tenant-2", "inst-2")])
    runner = _Runner(error="engine exploded")
    service = _service(
        registry,
        runners={"settlement.tick": runner},
        inventory=inventory,
        review=review,
        queue=queue,
    )
    run = service.trigger_job("settlement.tick", actor="operator-1")
    assert run.outcome is JobOutcome.FAILED
    assert run.error == "engine exploded"
    assert registry.latest_run("settlement.tick") is run
    # One inbox_dead_letter item per tenant, deduplicated by job+day.
    assert len(queue.items) == 2
    assert {item.tenant_id for item in queue.items} == {"tenant-1", "tenant-2"}
    assert all(item.item_type is ReviewItemType.INBOX_DEAD_LETTER for item in queue.items)
    assert all(
        item.source_event_ref == "scheduled-job:settlement.tick:failure:2026-11-21"
        for item in queue.items
    )


def test_trigger_failure_without_review_queue_still_records() -> None:
    registry = FakeRegistry()
    runner = _Runner(error="boom")
    service = _service(registry, runners={"settlement.tick": runner})
    run = service.trigger_job("settlement.tick", actor="operator-1")
    assert run.outcome is JobOutcome.FAILED
    assert registry.latest_run("settlement.tick") is run


def test_notify_job_failure_dedupes_per_job_per_day() -> None:
    queue = FakeReviewQueue()
    review = ReviewQueueCommandService(queue, FakeAudit())
    inventory = FakeInventory([("tenant-1", "inst-1")])
    day = datetime.date(2026, 11, 21)
    filed = notify_job_failure(
        inventory=inventory,
        review=review,
        queue=queue,
        job_id="settlement.tick",
        error="boom",
        today=day,
    )
    assert len(filed) == 1
    # The same job failing again the same day files nothing new.
    filed = notify_job_failure(
        inventory=inventory,
        review=review,
        queue=queue,
        job_id="settlement.tick",
        error="boom again",
        today=day,
    )
    assert filed == []
    assert len(queue.items) == 1
    # A different job, or the next day, files again.
    filed = notify_job_failure(
        inventory=inventory,
        review=review,
        queue=queue,
        job_id="cheque.daily",
        error="boom",
        today=day,
    )
    assert len(filed) == 1
    filed = notify_job_failure(
        inventory=inventory,
        review=review,
        queue=queue,
        job_id="settlement.tick",
        error="boom",
        today=datetime.date(2026, 11, 22),
    )
    assert len(filed) == 1


def test_list_runs() -> None:
    registry = FakeRegistry()
    service = _service(registry)
    assert service.list_runs("settlement.tick") == []
    with pytest.raises(ScheduledJobNotFoundError):
        service.list_runs("no.such.job")
