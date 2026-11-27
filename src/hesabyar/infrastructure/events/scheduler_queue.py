"""Procrastinate wiring for the US-560 scheduled jobs + the run recorder.

Four periodic jobs, all on the ``scheduler`` queue:

- ``scheduler.dunning_sweep`` (daily 08:00 Tehran) — the ۵ج dunning
  sweep (US-541), previously on-demand only.
- ``scheduler.store_sync`` (every 15 minutes) — reconciliation
  polling: sync every active store's changes with its saved cursor
  (US-512/US-533).
- ``scheduler.inbox_staleness`` (hourly) — the inbox staleness alert
  (US-501), previously a GET endpoint only.
- ``scheduler.recurring_generation`` (daily 07:30 Tehran) — the
  recurring generator (US-563): every due recurring invoice/journal
  occurrence becomes a draft in the engine.

Plus the :class:`JobRunRecorder`, which every periodic task on the
app uses (the existing jobs opt in via a ``recorder`` parameter on
their ``register_*_tasks`` function). The recorder is what makes the
registry real:

1. **Gate**: before a run, it reads the job's registry row (seeding
   defaults first). A disabled job is skipped; a job whose schedule
   says "not today" (a daily job on Friday) is skipped. Both skips
   are RECORDED as ``skipped`` runs — never silent.
2. **Record**: after a run, it writes the run row (outcome +
   summary) on its OWN session, committed immediately — so the
   record survives even when the job's own transaction rolls back.
3. **Notify**: on failure it files one ``inbox_dead_letter`` review
   item per tenant (deduplicated per job per day) and re-raises, so
   Procrastinate marks the job failed too.

The registry is observability, not a second gate: when the registry
itself is unreachable, the recorder logs a warning and the job still
runs unrecorded — a broken registry must never stop the sweep that
keeps events flowing. (In practice the registry shares the job's
database, so a registry outage usually means the job fails anyway.)
"""

from __future__ import annotations

import datetime
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import procrastinate

from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.application.commands.scheduled_jobs import ScheduledJobsService
from hesabyar.application.commands.scheduler import notify_job_failure
from hesabyar.domain.review_queue.ports import ReviewQueuePort
from hesabyar.domain.scheduler.models import (
    DEFAULT_JOBS,
    RECURRING_GENERATION_JOB_ID,
    JobOutcome,
    JobRun,
    TriggeredBy,
)
from hesabyar.domain.scheduler.ports import JobRegistryPort, SchedulerInventoryPort
from hesabyar.domain.scheduler.schedules import is_scheduled_day, tehran_today

logger = logging.getLogger(__name__)

#: Queue name for the US-560 scheduled jobs.
SCHEDULER_QUEUE = "scheduler"

#: Periodic job: the dunning sweep across all installations.
DUNNING_SWEEP_TASK = "scheduler.dunning_sweep"

#: Periodic job: reconciliation polling across all active stores.
STORE_SYNC_TASK = "scheduler.store_sync"

#: Periodic job: the inbox staleness alert.
INBOX_STALENESS_TASK = "scheduler.inbox_staleness"

#: Periodic job: the recurring generator (US-563) — the id is the
#: domain constant, so the generator service files its review items
#: under the same id without importing infrastructure.
RECURRING_GENERATION_TASK = RECURRING_GENERATION_JOB_ID

#: Default cadence: daily 08:00 Tehran (04:30 UTC — Iran is UTC+3:30
#: year-round, no DST since 2022). Overridable via create_event_app.
DUNNING_SWEEP_CRON = "30 4 * * *"

#: Default cadence: every 15 minutes (US-560 acceptance criterion).
STORE_SYNC_CRON = "*/15 * * * *"

#: Default cadence: hourly.
INBOX_STALENESS_CRON = "0 * * * *"

#: Default cadence: daily 07:30 Tehran (04:00 UTC). Overridable via
#: create_event_app / HESABYAR_SCHEDULER__RECURRING_GENERATION_CRON.
RECURRING_GENERATION_CRON = "0 4 * * *"


@dataclass
class SchedulerWorkerHandle:
    """A scheduled-jobs service bound to one session, with commit/close.

    The task function builds one handle per job run: the service
    flushes, the handle commits, and the session is always closed.
    """

    jobs: ScheduledJobsService
    commit: Callable[[], None]
    close: Callable[[], None]


#: Factory building a fresh scheduler handle per job run (the worker
#: script supplies it; it owns settings, engine and session).
SchedulerWorkerFactory = Callable[[], SchedulerWorkerHandle]


@dataclass
class RecorderHandle:
    """Registry + review ports bound to one short-lived session.

    The recorder opens a fresh handle per registry operation and
    commits it immediately, so a run record is durable even when
    the job's own transaction rolls back.
    """

    registry: JobRegistryPort
    inventory: SchedulerInventoryPort
    review: ReviewQueueCommandService | None
    queue: ReviewQueuePort | None
    commit: Callable[[], None]
    close: Callable[[], None]


#: Factory building a fresh recorder handle per registry operation.
RecorderFactory = Callable[[], RecorderHandle]


class JobRunRecorder:
    """Gate + record + notify for scheduled job runs (see module docstring)."""

    def __init__(
        self,
        factory: RecorderFactory,
        *,
        now: Callable[[], datetime.datetime] | None = None,
    ) -> None:
        self._factory = factory
        self._now = now or (lambda: datetime.datetime.now(datetime.UTC))

    def _check(
        self, job_id: str, today: datetime.date
    ) -> tuple[bool, str | None]:
        """Should the job run today? Returns (run?, skip reason).

        A registry failure is fail-open with a warning: the job runs
        unrecorded rather than being blocked by observability.
        """
        handle = self._factory()
        try:
            handle.registry.ensure_defaults(DEFAULT_JOBS)
            definition = handle.registry.get_definition(job_id)
            handle.commit()
        except Exception:
            logger.warning(
                "Job registry unreachable for %s; running unrecorded.",
                job_id,
                exc_info=True,
            )
            return True, None
        finally:
            handle.close()
        if definition is None:
            logger.warning(
                "Job %s has no registry row; running unrecorded.", job_id
            )
            return True, None
        if not definition.enabled:
            return False, "disabled"
        if not is_scheduled_day(definition.schedule, today):
            return False, "not a scheduled day"
        return True, None

    def _record(
        self,
        job_id: str,
        *,
        started_at: datetime.datetime,
        outcome: JobOutcome,
        summary: dict[str, Any],
        error: str | None,
    ) -> None:
        """Write one run record (best-effort: a failure is logged, never raised)."""
        handle = self._factory()
        try:
            handle.registry.record_run(
                JobRun(
                    id=str(uuid.uuid4()),
                    job_id=job_id,
                    started_at=started_at,
                    finished_at=self._now(),
                    outcome=outcome,
                    summary=summary,
                    error=error,
                    triggered_by=TriggeredBy.SCHEDULED,
                )
            )
            handle.commit()
        except Exception:
            logger.warning(
                "Could not record the %s run for %s.", outcome.value, job_id, exc_info=True
            )
        finally:
            handle.close()

    def _notify_failure(
        self, job_id: str, error: str, today: datetime.date
    ) -> None:
        """File the per-tenant failure items (best-effort)."""
        handle = self._factory()
        try:
            filed = notify_job_failure(
                inventory=handle.inventory,
                review=handle.review,
                queue=handle.queue,
                job_id=job_id,
                error=error,
                today=today,
            )
            handle.commit()
            if filed:
                logger.warning(
                    "Job %s failed; filed %d review item(s).", job_id, len(filed)
                )
        except Exception:
            logger.warning(
                "Could not file failure review items for %s.", job_id, exc_info=True
            )
        finally:
            handle.close()

    def run_recorded(
        self,
        job_id: str,
        func: Callable[[], dict[str, Any]],
        *,
        today: datetime.date,
    ) -> dict[str, Any] | None:
        """Gate, run, and record one scheduled run.

        Returns the job's summary dict, or None when the run was
        skipped (disabled / not a scheduled day). A job failure is
        recorded + notified, then re-raised so Procrastinate marks
        the job failed too.
        """
        should_run, skip_reason = self._check(job_id, today)
        started_at = self._now()
        if not should_run:
            logger.info("Job %s skipped (%s).", job_id, skip_reason)
            self._record(
                job_id,
                started_at=started_at,
                outcome=JobOutcome.SKIPPED,
                summary={"reason": skip_reason},
                error=None,
            )
            return None
        try:
            summary = func()
        except Exception as exc:
            error = str(exc) or exc.__class__.__name__
            logger.exception("Job %s failed.", job_id)
            self._record(
                job_id,
                started_at=started_at,
                outcome=JobOutcome.FAILED,
                summary={},
                error=error,
            )
            self._notify_failure(job_id, error, today)
            raise
        self._record(
            job_id,
            started_at=started_at,
            outcome=JobOutcome.SUCCESS,
            summary=summary,
            error=None,
        )
        return summary


def run_recorded(
    recorder: JobRunRecorder | None,
    job_id: str,
    func: Callable[[], dict[str, Any]],
    *,
    today: datetime.date,
) -> dict[str, Any] | None:
    """Run ``func`` under the recorder, or bare when none is wired.

    The no-recorder path keeps the existing queue tests (and the
    defer-only app) behaving exactly as before: the job runs, nothing
    is recorded, and no Friday check applies.
    """
    if recorder is None:
        return func()
    return recorder.run_recorded(job_id, func, today=today)


def register_scheduler_tasks(
    app: procrastinate.App,
    scheduler_factory: SchedulerWorkerFactory,
    *,
    recorder: JobRunRecorder | None = None,
    dunning_sweep_cron: str = DUNNING_SWEEP_CRON,
    store_sync_cron: str = STORE_SYNC_CRON,
    staleness_cron: str = INBOX_STALENESS_CRON,
    recurring_generation_cron: str = RECURRING_GENERATION_CRON,
) -> None:
    """Register the scheduled tasks on a Procrastinate app (US-560 + US-563)."""

    @app.periodic(cron=dunning_sweep_cron)
    @app.task(queue=SCHEDULER_QUEUE, name=DUNNING_SWEEP_TASK, retry=False)
    def dunning_sweep(timestamp: int = 0) -> None:  # noqa: ARG001 - periodic passes the schedule timestamp
        handle = scheduler_factory()
        day = tehran_today(timestamp)
        try:

            def _do() -> dict[str, Any]:
                report = handle.jobs.run_dunning_sweep_all(today=day)
                logger.info(
                    "Dunning sweep done: installations=%d plans=%d overdue_legs=%d "
                    "items_filed=%d fees_assessed_irr=%d journals=%d failures=%d",
                    report.installations_scanned,
                    report.plans_scanned,
                    report.overdue_legs,
                    report.items_filed,
                    report.fees_assessed_irr,
                    report.journals_created,
                    len(report.failures),
                )
                for failure in report.failures:
                    logger.warning(
                        "Dunning sweep failure for %s: %s", failure.unit, failure.error
                    )
                return report.summary()

            run_recorded(recorder, DUNNING_SWEEP_TASK, _do, today=day)
            handle.commit()
        finally:
            handle.close()

    @app.periodic(cron=store_sync_cron)
    @app.task(queue=SCHEDULER_QUEUE, name=STORE_SYNC_TASK, retry=False)
    def store_sync(timestamp: int = 0) -> None:  # noqa: ARG001 - periodic passes the schedule timestamp
        handle = scheduler_factory()
        day = tehran_today(timestamp)
        try:

            def _do() -> dict[str, Any]:
                report = handle.jobs.run_store_sync_all(today=day)
                logger.info(
                    "Store sync done: stores=%d synced=%d skipped=%d created=%d "
                    "duplicate=%d unmapped=%d pages=%d failures=%d",
                    report.stores_scanned,
                    report.stores_synced,
                    report.stores_skipped,
                    report.events_created,
                    report.events_duplicate,
                    report.changes_unmapped,
                    report.pages_fetched,
                    len(report.failures),
                )
                for failure in report.failures:
                    logger.warning(
                        "Store sync failure for %s: %s", failure.unit, failure.error
                    )
                return report.summary()

            run_recorded(recorder, STORE_SYNC_TASK, _do, today=day)
            handle.commit()
        finally:
            handle.close()

    @app.periodic(cron=staleness_cron)
    @app.task(queue=SCHEDULER_QUEUE, name=INBOX_STALENESS_TASK, retry=False)
    def inbox_staleness(timestamp: int = 0) -> None:  # noqa: ARG001 - periodic passes the schedule timestamp
        handle = scheduler_factory()
        day = tehran_today(timestamp)
        try:

            def _do() -> dict[str, Any]:
                report = handle.jobs.run_staleness_alert()
                logger.info(
                    "Inbox staleness alert done: stale=%d filed=%d already_filed=%d "
                    "skipped_no_tenant=%d",
                    report.stale_count,
                    report.items_filed,
                    report.already_filed,
                    report.skipped_no_tenant,
                )
                return report.summary()

            run_recorded(recorder, INBOX_STALENESS_TASK, _do, today=day)
            handle.commit()
        finally:
            handle.close()

    @app.periodic(cron=recurring_generation_cron)
    @app.task(queue=SCHEDULER_QUEUE, name=RECURRING_GENERATION_TASK, retry=False)
    def recurring_generation(timestamp: int = 0) -> None:  # noqa: ARG001 - periodic passes the schedule timestamp
        handle = scheduler_factory()
        day = tehran_today(timestamp)
        try:

            def _do() -> dict[str, Any]:
                report = handle.jobs.run_recurring_generation_all(today=day)
                logger.info(
                    "Recurring generation done: templates=%d due=%d generated=%d "
                    "posted=%d skipped=%d failed=%d completed=%d",
                    report.templates_scanned,
                    report.occurrences_due,
                    report.generated,
                    report.posted,
                    report.skipped_existing,
                    report.failed,
                    report.completed_templates,
                )
                for failure in report.failures:
                    logger.warning(
                        "Recurring generation failure for %s on %s: %s",
                        failure.template_id,
                        failure.scheduled_date,
                        failure.error,
                    )
                return report.summary()

            run_recorded(recorder, RECURRING_GENERATION_TASK, _do, today=day)
            handle.commit()
        finally:
            handle.close()
