"""Procrastinate wiring for the event worker (US-501, phased design §6.5).

Procrastinate (MIT, Postgres-native, no Redis) is the trigger /
scheduler for the event platform — it never replaces the outbox:
the inbox/outbox tables stay in HesabYar's own schema, and the
worker polls them with ``FOR UPDATE SKIP LOCKED``. Procrastinate
only decides *when* the worker runs:

- ``events.process_inbox_event`` — a one-shot job deferred on
  ingest (and on DLQ replay) so a fresh event is processed within
  seconds instead of waiting for the next sweep.
- ``events.sweep`` — a periodic job (every minute) that runs one
  full worker sweep: due inbox events (including retries whose time
  has come and events whose trigger fired before the ingest
  transaction committed) + pending outbox events.

US-560 registers three more periodic jobs on the same app (the
``scheduler`` queue): the dunning sweep, the store sync, and the
inbox staleness alert — see
``hesabyar.infrastructure.events.scheduler_queue``. US-563 adds a
fourth on the same queue: the recurring generator. US-703 adds two
more on the ``backup`` queue: the daily database backup and the
monthly restore drill — see
``hesabyar.infrastructure.events.backup_queue``. Every periodic
job is also a registry row (``scheduled_jobs``) with a Jalali-aware
schedule; the recorder gates each run (enabled? scheduled today?)
and records its outcome — see ``docs/scheduler.md``.

Deferring is best-effort: if the Procrastinate schema is not
applied yet (or the queue is down), the defer is logged and the
ingest still succeeds — the sweep is the backstop that guarantees
every received event is eventually processed. The worker script
(``scripts/run_event_worker.py``) applies the Procrastinate schema
on start, so a running worker implies a working queue.

The procrastinate schema (``procrastinate_jobs`` etc.) is managed
by Procrastinate itself (``app.schema_manager.apply_schema()``),
not by Alembic — it is queue state, not HesabYar schema.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import procrastinate
from procrastinate import PsycopgConnector
from procrastinate.connector import BaseAsyncConnector, BaseConnector

from hesabyar.application.commands.events import EventWorker
from hesabyar.domain.scheduler.schedules import tehran_today
from hesabyar.infrastructure.events.backup_queue import (
    BACKUP_CRON,
    RESTORE_DRILL_CRON,
    BackupWorkerFactory,
    register_backup_tasks,
)
from hesabyar.infrastructure.events.cheque_queue import (
    DAILY_CRON,
    ChequeWorkerFactory,
    register_cheque_tasks,
)
from hesabyar.infrastructure.events.monitoring_queue import (
    EVALUATION_CRON,
    MonitoringWorkerFactory,
    register_monitoring_tasks,
)
from hesabyar.infrastructure.events.packaging_queue import (
    SUPPLY_CHECK_CRON,
    PackagingWorkerFactory,
    register_packaging_tasks,
)
from hesabyar.infrastructure.events.reminders_queue import (
    DAILY_CRON as REMINDERS_DAILY_CRON,
)
from hesabyar.infrastructure.events.reminders_queue import (
    RemindersWorkerFactory,
    register_reminders_tasks,
)
from hesabyar.infrastructure.events.scheduler_queue import (
    DUNNING_SWEEP_CRON,
    INBOX_STALENESS_CRON,
    RECURRING_GENERATION_CRON,
    STORE_SYNC_CRON,
    JobRunRecorder,
    SchedulerWorkerFactory,
    register_scheduler_tasks,
    run_recorded,
)
from hesabyar.infrastructure.events.settlement_queue import (
    TICK_CRON,
    SettlementWorkerFactory,
    register_settlement_tasks,
)
from hesabyar.infrastructure.events.sms_queue import (
    DAILY_REMINDERS_CRON,
    SmsWorkerFactory,
    register_sms_tasks,
)

logger = logging.getLogger(__name__)

#: Queue name for event-platform jobs.
EVENTS_QUEUE = "events"

#: One-shot job: process one inbox event by id.
PROCESS_EVENT_TASK = "events.process_inbox_event"

#: Periodic job: run one full worker sweep.
SWEEP_TASK = "events.sweep"

#: Default sweep cadence (every minute; procrastinate periodic
#: tasks use cron, so sub-minute intervals are not expressible —
#: one-shot jobs cover the low-latency path instead).
SWEEP_CRON = "* * * * *"


@dataclass
class WorkerHandle:
    """A worker bound to one session, with its commit/close callbacks.

    The task functions build one handle per job run: the worker
    flushes, the handle commits, and the session is always closed.
    """

    worker: EventWorker
    commit: Callable[[], None]
    close: Callable[[], None]


#: Factory building a fresh worker handle per job run (the worker
#: script supplies it; it owns settings, engine and session).
WorkerFactory = Callable[[], WorkerHandle]


def create_event_app(
    connector: BaseConnector | BaseAsyncConnector,
    worker_factory: WorkerFactory,
    *,
    sweep_cron: str = SWEEP_CRON,
    packaging_factory: PackagingWorkerFactory | None = None,
    supply_check_cron: str = SUPPLY_CHECK_CRON,
    settlement_factory: SettlementWorkerFactory | None = None,
    settlement_tick_cron: str = TICK_CRON,
    cheque_factory: ChequeWorkerFactory | None = None,
    cheque_daily_cron: str = DAILY_CRON,
    sms_factory: SmsWorkerFactory | None = None,
    sms_daily_reminders_cron: str = DAILY_REMINDERS_CRON,
    reminders_factory: RemindersWorkerFactory | None = None,
    reminders_daily_cron: str = REMINDERS_DAILY_CRON,
    scheduler_factory: SchedulerWorkerFactory | None = None,
    dunning_sweep_cron: str = DUNNING_SWEEP_CRON,
    store_sync_cron: str = STORE_SYNC_CRON,
    staleness_cron: str = INBOX_STALENESS_CRON,
    recurring_generation_cron: str = RECURRING_GENERATION_CRON,
    backup_factory: BackupWorkerFactory | None = None,
    backup_cron: str = BACKUP_CRON,
    restore_drill_cron: str = RESTORE_DRILL_CRON,
    monitoring_factory: MonitoringWorkerFactory | None = None,
    monitoring_evaluation_cron: str = EVALUATION_CRON,
    recorder: JobRunRecorder | None = None,
) -> procrastinate.App:
    """Build the Procrastinate app with the event-platform tasks.

    ``connector`` is a ``PsycopgConnector`` in production and an
    ``InMemoryConnector`` in tests. The tasks close over
    ``worker_factory`` so each job run gets a fresh session. When
    ``packaging_factory`` is given (the worker process), the daily
    packaging supply-check task is registered too; when
    ``settlement_factory`` is given, the daily settlement tick is
    registered too; when ``cheque_factory`` is given, the daily
    cheque job (reminders + expiry) is registered too; when
    ``sms_factory`` is given, the daily SMS reminder job is
    registered too; when ``reminders_factory`` is given, the daily
    reminder job (US-564) is registered too; when
    ``scheduler_factory`` is given, the four scheduled jobs
    (dunning sweep, store sync, inbox staleness, recurring
<<<<<<< HEAD
    generation) are registered too; when ``backup_factory`` is
    given, the two backup jobs (daily backup, monthly restore
    drill) are registered too; when ``monitoring_factory`` is
    given, the monitoring evaluation job (US-704) is registered
    too. The API process (``create_defer_app``) passes none of them.

    When ``recorder`` is given (the worker process), every periodic
    task is gated (enabled? scheduled today?) and recorded in the
    job registry; without it, tasks run exactly as before.
    """
    app = procrastinate.App(connector=connector)

    @app.task(queue=EVENTS_QUEUE, name=PROCESS_EVENT_TASK, retry=False)
    def process_inbox_event(event_id: str) -> None:
        handle = worker_factory()
        try:
            handle.worker.process_one(event_id)
            handle.commit()
        finally:
            handle.close()

    @app.periodic(cron=sweep_cron)
    @app.task(queue=EVENTS_QUEUE, name=SWEEP_TASK, retry=False)
    def sweep(timestamp: int = 0) -> None:  # noqa: ARG001 - periodic passes the schedule timestamp
        handle = worker_factory()
        day = tehran_today(timestamp)
        try:

            def _do() -> dict[str, Any]:
                report = handle.worker.poll_once()
                logger.info(
                    "Sweep done: claimed=%d draft=%d posted=%d review=%d "
                    "dead_lettered=%d retries=%d outbox_published=%d "
                    "outbox_failed=%d errors=%d",
                    report.claimed,
                    report.processed_draft,
                    report.processed_posted,
                    report.needs_review,
                    report.dead_lettered,
                    report.retries_scheduled,
                    report.outbox_published,
                    report.outbox_failed,
                    len(report.errors),
                )
                for error in report.errors:
                    logger.error("Sweep error: %s", error)
                return {
                    "claimed": report.claimed,
                    "processed_draft": report.processed_draft,
                    "processed_posted": report.processed_posted,
                    "needs_review": report.needs_review,
                    "dead_lettered": report.dead_lettered,
                    "retries_scheduled": report.retries_scheduled,
                    "outbox_published": report.outbox_published,
                    "outbox_failed": report.outbox_failed,
                    "errors": list(report.errors),
                }

            run_recorded(recorder, SWEEP_TASK, _do, today=day)
            handle.commit()
        finally:
            handle.close()

    if packaging_factory is not None:
        register_packaging_tasks(
            app,
            packaging_factory,
            supply_check_cron=supply_check_cron,
            recorder=recorder,
        )

    if settlement_factory is not None:
        register_settlement_tasks(
            app,
            settlement_factory,
            tick_cron=settlement_tick_cron,
            recorder=recorder,
        )

    if cheque_factory is not None:
        register_cheque_tasks(
            app, cheque_factory, daily_cron=cheque_daily_cron, recorder=recorder
        )

    if sms_factory is not None:
        register_sms_tasks(
            app,
            sms_factory,
            daily_reminders_cron=sms_daily_reminders_cron,
            recorder=recorder,
        )

    if reminders_factory is not None:
        register_reminders_tasks(
            app,
            reminders_factory,
            daily_cron=reminders_daily_cron,
            recorder=recorder,
        )

    if scheduler_factory is not None:
        register_scheduler_tasks(
            app,
            scheduler_factory,
            recorder=recorder,
            dunning_sweep_cron=dunning_sweep_cron,
            store_sync_cron=store_sync_cron,
            staleness_cron=staleness_cron,
            recurring_generation_cron=recurring_generation_cron,
        )

    if backup_factory is not None:
        register_backup_tasks(
            app,
            backup_factory,
            backup_cron=backup_cron,
            restore_drill_cron=restore_drill_cron,
            recorder=recorder,
        )

    if monitoring_factory is not None:
        register_monitoring_tasks(
            app,
            monitoring_factory,
            evaluation_cron=monitoring_evaluation_cron,
            recorder=recorder,
        )

    return app


def create_defer_app(connector: BaseConnector | BaseAsyncConnector) -> procrastinate.App:
    """Build a defer-only app (API process): tasks are registered for
    deferring but never execute here — a task that somehow runs in
    this process raises loudly instead of processing an event without
    a worker session.
    """

    def _never_run() -> WorkerHandle:
        raise RuntimeError("Event tasks do not run in the API process; start the worker.")

    return create_event_app(connector, _never_run)


def create_connector(database_url: str) -> PsycopgConnector:
    """Build a Procrastinate connector for a Postgres URL.

    ``database_url`` is a plain ``postgresql://`` URL (the
    SQLAlchemy ``+psycopg`` dialect prefix is stripped).
    """
    conninfo = database_url
    if conninfo.startswith("postgresql+psycopg://"):
        conninfo = "postgresql://" + conninfo[len("postgresql+psycopg://") :]
    return PsycopgConnector(conninfo=conninfo)


class ProcrastinateJobQueue:
    """``JobQueuePort`` backed by a Procrastinate app.

    Deferring never raises: if the queue is unavailable (schema not
    applied, database down), the failure is logged at warning level
    and the periodic sweep still processes the event. A failed defer
    must never fail an ingest.
    """

    def __init__(self, app: procrastinate.App) -> None:
        self._app = app

    def defer_process_event(self, event_id: str) -> None:
        """Schedule one inbox event for processing (best-effort)."""
        try:
            self._app.tasks[PROCESS_EVENT_TASK].defer(event_id=event_id)
        except Exception:
            logger.warning(
                "Failed to defer process-event job for %s; the sweep will pick it up.",
                event_id,
                exc_info=True,
            )

    def defer_sweep(self) -> None:
        """Schedule a worker sweep (best-effort)."""
        try:
            self._app.tasks[SWEEP_TASK].defer()
        except Exception:
            logger.warning("Failed to defer sweep job.", exc_info=True)
