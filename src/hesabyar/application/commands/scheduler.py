"""Job registry use cases (US-560, Phase 5-7 ۵خ W1).

:class:`SchedulerService` is the registry's front door: list jobs
(with the in-app ``next_run``), trigger one run on demand, and
enable/disable a job. The Procrastinate tasks call the same runners
through the recorder (``hesabyar.infrastructure.events.scheduler_queue``),
so a scheduled run and a manual trigger share one execution path.

Semantics worth knowing:

- **Disabling stops the schedule, not manual triggers.** A disabled
  job is skipped by its Procrastinate task (recorded as ``skipped``,
  never silent); an operator can still trigger it by hand — e.g. to
  test a fix — and that run is recorded like any other.
- **A manual trigger ignores the calendar.** Friday-skip and the
  Jalali date apply to scheduled runs only; a trigger is an explicit
  operator action and runs now.
- **A failed run is never silent.** The failure is recorded on the
  run row (the job's dead-letter record) and filed as an
  ``inbox_dead_letter`` review item — one per tenant, deduplicated
  per job per day — via :func:`notify_job_failure`. Per-unit
  failures (one store, one installation) are filed by the job
  itself with a per-unit ref; this module's notifier covers the
  wholesale failure (the runner raised before it could report).
- **``next_run`` is computed, never stored**: from the last run's
  start when that still points forward, else from now.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.application.errors import (
    ScheduledJobNotFoundError,
    ScheduledJobNotRunnableError,
)
from hesabyar.domain.review_queue.models import (
    ReviewItemStatus,
    ReviewItemType,
    ReviewSeverity,
)
from hesabyar.domain.review_queue.ports import ReviewItemFilters, ReviewQueuePort
from hesabyar.domain.scheduler.models import (
    DEFAULT_JOBS,
    JobDefinition,
    JobOutcome,
    JobRun,
    JobStatus,
    TriggeredBy,
)
from hesabyar.domain.scheduler.ports import JobRegistryPort, SchedulerInventoryPort
from hesabyar.domain.scheduler.schedules import TEHRAN, NextRun, compute_next_run

#: Max characters of a failure message kept on a review item reason
#: (the full text lives on the run record).
_MAX_REASON_ERROR_CHARS = 500


@dataclass(frozen=True)
class JobRunContext:
    """What a job runner needs: the business day, the actor, the trigger."""

    today: datetime.date
    actor: str
    triggered_by: TriggeredBy


class JobRunner(Protocol):
    """One job's execution: run it now, return a JSON summary dict."""

    def run(self, ctx: JobRunContext) -> dict[str, Any]: ...


def file_job_failure_item(
    *,
    review: ReviewQueueCommandService,
    queue: ReviewQueuePort,
    tenant_id: str,
    job_id: str,
    ref: str,
    reason: str,
    actor: str,
) -> str | None:
    """File one ``inbox_dead_letter`` item unless one with ``ref`` is open.

    Returns the new item's id, or None when an open/in-review item
    with the same ref already exists (the dedup that keeps a failing
    15-minute job from filing 96 items a day).
    """
    existing_refs = {
        item.source_event_ref
        for item in queue.list_items(
            tenant_id,
            ReviewItemFilters(item_type=ReviewItemType.INBOX_DEAD_LETTER),
        )
        if item.status in (ReviewItemStatus.OPEN, ReviewItemStatus.IN_REVIEW)
        and item.source_event_ref is not None
    }
    if ref in existing_refs:
        return None
    item = review.file_item(
        tenant_id=tenant_id,
        item_type=ReviewItemType.INBOX_DEAD_LETTER,
        severity=ReviewSeverity.WARNING,
        reason=reason,
        suggested_action=(
            "Check the job's run record (GET /jobs) and re-trigger it once the cause is fixed."
        ),
        subtype="scheduled_job",
        source_event_ref=ref,
        actor=actor,
    )
    return item.id


def notify_job_failure(
    *,
    inventory: SchedulerInventoryPort,
    review: ReviewQueueCommandService | None,
    queue: ReviewQueuePort | None,
    job_id: str,
    error: str,
    today: datetime.date,
    actor: str = "scheduler",
) -> list[str]:
    """File one failure item per tenant for a wholesale job failure.

    Used when the runner raised before it could report per-unit
    failures (per-unit failures are filed by the job itself). One
    item per tenant, deduplicated per job per day. Returns the filed
    item ids (empty when no review queue is wired, or when no tenant
    exists yet — the failed run record is still the durable DLQ
    entry in those cases).
    """
    if review is None or queue is None:
        return []
    tenants = sorted({tenant_id for tenant_id, _ in inventory.list_installation_tenants()})
    ref = f"scheduled-job:{job_id}:failure:{today.isoformat()}"
    reason = f"Scheduled job {job_id} failed: {error[:_MAX_REASON_ERROR_CHARS]}"
    filed: list[str] = []
    for tenant_id in tenants:
        item_id = file_job_failure_item(
            review=review,
            queue=queue,
            tenant_id=tenant_id,
            job_id=job_id,
            ref=ref,
            reason=reason,
            actor=actor,
        )
        if item_id is not None:
            filed.append(item_id)
    return filed


class SchedulerService:
    """List / trigger / enable-disable for the job registry."""

    def __init__(
        self,
        registry: JobRegistryPort,
        *,
        runners: Mapping[str, JobRunner] | None = None,
        inventory: SchedulerInventoryPort | None = None,
        review: ReviewQueueCommandService | None = None,
        queue: ReviewQueuePort | None = None,
        now: datetime.datetime | None = None,
    ) -> None:
        self._registry = registry
        self._runners = dict(runners or {})
        self._inventory = inventory
        self._review = review
        self._queue = queue
        self._now = now

    def _clock(self) -> datetime.datetime:
        if self._now is not None:
            return self._now
        return datetime.datetime.now(datetime.UTC)

    def _next_run_for(self, definition: JobDefinition, now: datetime.datetime) -> NextRun:
        """Next run from the last run's start when it points forward, else from now."""
        last = self._registry.latest_run(definition.id)
        if last is not None:
            candidate = compute_next_run(definition.schedule, last.started_at)
            if candidate.run_at > now:  # aware datetimes compare across zones
                return candidate
        return compute_next_run(definition.schedule, now)

    def _status_for(self, definition: JobDefinition, now: datetime.datetime) -> JobStatus:
        return JobStatus(
            definition=definition,
            last_run=self._registry.latest_run(definition.id),
            next_run=self._next_run_for(definition, now),
        )

    def list_jobs(self) -> list[JobStatus]:
        """All registered jobs with last run + computed next run (seeds first)."""
        self._registry.ensure_defaults(DEFAULT_JOBS)
        now = self._clock()
        return [
            self._status_for(definition, now)
            for definition in self._registry.list_definitions()
        ]

    def get_job(self, job_id: str) -> JobStatus:
        """One job's status, or 404 when the id is not registered."""
        self._registry.ensure_defaults(DEFAULT_JOBS)
        definition = self._registry.get_definition(job_id)
        if definition is None:
            raise ScheduledJobNotFoundError(
                f"Scheduled job {job_id!r} is not registered.",
                details={"job_id": job_id},
            )
        return self._status_for(definition, self._clock())

    def set_enabled(self, job_id: str, *, enabled: bool) -> JobStatus:
        """Enable/disable a job (the schedule, not manual triggers)."""
        self._registry.ensure_defaults(DEFAULT_JOBS)
        definition = self._registry.set_enabled(job_id, enabled)
        if definition is None:
            raise ScheduledJobNotFoundError(
                f"Scheduled job {job_id!r} is not registered.",
                details={"job_id": job_id},
            )
        return self._status_for(definition, self._clock())

    def list_runs(self, job_id: str, *, limit: int = 20) -> Sequence[JobRun]:
        """A job's recent runs, newest first (404 when unregistered)."""
        self._registry.ensure_defaults(DEFAULT_JOBS)
        if self._registry.get_definition(job_id) is None:
            raise ScheduledJobNotFoundError(
                f"Scheduled job {job_id!r} is not registered.",
                details={"job_id": job_id},
            )
        return self._registry.list_runs(job_id, limit=limit)

    def trigger_job(
        self,
        job_id: str,
        *,
        actor: str,
        today: datetime.date | None = None,
    ) -> JobRun:
        """Run one job now, on demand; record the run either way.

        A manual trigger ignores the enabled flag and the calendar.
        When the runner raises, the run is recorded as ``failed``
        (with the error), the failure is notified to the review
        queue, and the failed run is returned — the caller gets the
        record, not an exception. A job with no runner wired in this
        process raises :class:`ScheduledJobNotRunnableError` (503).
        """
        self._registry.ensure_defaults(DEFAULT_JOBS)
        if self._registry.get_definition(job_id) is None:
            raise ScheduledJobNotFoundError(
                f"Scheduled job {job_id!r} is not registered.",
                details={"job_id": job_id},
            )
        runner = self._runners.get(job_id)
        if runner is None:
            raise ScheduledJobNotRunnableError(
                f"Scheduled job {job_id!r} has no runner in this process.",
                details={"job_id": job_id},
            )
        started_at = self._clock()
        # The business day comes from the injected clock too — a frozen
        # clock must freeze the run record whole (started_at AND today),
        # not leave today reading the real wall clock.
        day = today or self._clock().astimezone(TEHRAN).date()
        try:
            summary = runner.run(
                JobRunContext(today=day, actor=actor, triggered_by=TriggeredBy.MANUAL)
            )
        except Exception as exc:
            error = str(exc) or exc.__class__.__name__
            run = JobRun(
                id=str(uuid.uuid4()),
                job_id=job_id,
                started_at=started_at,
                finished_at=self._clock(),
                outcome=JobOutcome.FAILED,
                summary={},
                error=error,
                triggered_by=TriggeredBy.MANUAL,
            )
            saved = self._registry.record_run(run)
            if self._inventory is not None:
                notify_job_failure(
                    inventory=self._inventory,
                    review=self._review,
                    queue=self._queue,
                    job_id=job_id,
                    error=error,
                    today=day,
                    actor=actor,
                )
            return saved
        run = JobRun(
            id=str(uuid.uuid4()),
            job_id=job_id,
            started_at=started_at,
            finished_at=self._clock(),
            outcome=JobOutcome.SUCCESS,
            summary=summary,
            error=None,
            triggered_by=TriggeredBy.MANUAL,
        )
        return self._registry.record_run(run)
