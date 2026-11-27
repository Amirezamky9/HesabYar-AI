"""Monitoring evaluation task wiring (US-704, Phase 7).

Registers ``monitoring.evaluate`` — the periodic alert evaluation —
on the Procrastinate app (the ``scheduler`` queue, every 15 minutes
by default). The task builds a fresh monitoring handle per run,
evaluates the rules, persists newly fired alerts and dispatches
them, then commits. The run is recorded in the job registry
(``scheduled_job_runs``) like every other periodic job, so the
operator can see the evaluation's own history in ``/jobs``.

The worker's evaluation runs WITHOUT health checks and WITHOUT
request metrics: the DB/Tryton probes live in the REST layer (the
worker must not import the web stack), and the 5xx counters live
in the API process's memory. Neither gap matters here — no rule
reads process health, and the 5xx rule skips when it has no data
(see docs/monitoring.md). The panel always reads the snapshot
from the API process, where both are live.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import procrastinate

from hesabyar.application.commands.monitoring import MonitoringService
from hesabyar.domain.scheduler.schedules import tehran_today
from hesabyar.infrastructure.events.scheduler_queue import (
    SCHEDULER_QUEUE,
    JobRunRecorder,
    run_recorded,
)

logger = logging.getLogger(__name__)

#: Periodic job: the monitoring alert evaluation.
MONITORING_EVALUATE_TASK = "monitoring.evaluate"

#: Default evaluation cadence (every 15 minutes).
EVALUATION_CRON = "*/15 * * * *"


@dataclass
class MonitoringWorkerHandle:
    """A monitoring service bound to one session, with commit/close callbacks."""

    service: MonitoringService
    commit: Callable[[], None]
    close: Callable[[], None]


#: Factory building a fresh monitoring handle per job run (the worker
#: script supplies it; it owns settings, engine and session).
MonitoringWorkerFactory = Callable[[], MonitoringWorkerHandle]


def register_monitoring_tasks(
    app: procrastinate.App,
    monitoring_factory: MonitoringWorkerFactory,
    *,
    evaluation_cron: str = EVALUATION_CRON,
    recorder: JobRunRecorder | None = None,
) -> None:
    """Register the monitoring evaluation task on a Procrastinate app."""

    @app.periodic(cron=evaluation_cron)
    @app.task(queue=SCHEDULER_QUEUE, name=MONITORING_EVALUATE_TASK, retry=False)
    def monitoring_evaluate(timestamp: int = 0) -> None:  # noqa: ARG001 - periodic passes the schedule timestamp
        handle = monitoring_factory()
        day = tehran_today(timestamp)
        try:

            def _do() -> dict[str, Any]:
                report = handle.service.evaluate()
                sent = sum(1 for result in report.dispatched if result.status == "sent")
                failed = sum(1 for result in report.dispatched if result.status == "failed")
                skipped = sum(1 for result in report.dispatched if result.status == "skipped")
                logger.info(
                    "Monitoring evaluation done: fired=%d already_open=%d dispatched_sent=%d failed=%d skipped=%d",
                    len(report.fired),
                    report.already_open,
                    sent,
                    failed,
                    skipped,
                )
                for alert in report.fired:
                    logger.info(
                        "Alert fired: rule=%s severity=%s title=%s",
                        alert.rule.value,
                        alert.severity.value,
                        alert.title_fa,
                    )
                return {
                    "fired": len(report.fired),
                    "already_open": report.already_open,
                    "dispatched_sent": sent,
                    "dispatched_failed": failed,
                    "dispatched_skipped": skipped,
                    "rules_fired": [alert.rule.value for alert in report.fired],
                }

            run_recorded(recorder, MONITORING_EVALUATE_TASK, _do, today=day)
            handle.commit()
        finally:
            handle.close()
