"""Procrastinate wiring for the settlement daily tick (US-521, W1).

One periodic job, ``settlement.tick`` (daily 06:00 by default —
phased design §3.9): it runs the daily tick across ALL
installations — ``expected → due`` at the due date, ``due → overdue``
past due + grace, opening (or escalating) ``late_tranche``
exceptions at the 7/30/60-day thresholds.

The job NEVER posts anything and NEVER touches the engine: it only
moves tranche states and opens exceptions. The tick is idempotent
(a re-run on the same day finds no new candidates), so a missed or
duplicated run is harmless.

US-560: the tick is a registered job (``settlement.tick``, daily
09:30 Tehran, Friday skipped by default — the Saturday run catches
up any Friday maturities). When a recorder is wired, each run is
gated (enabled? scheduled today?) and recorded in the registry.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import procrastinate

from hesabyar.application.commands.settlement import SettlementCommandService
from hesabyar.domain.scheduler.schedules import tehran_today
from hesabyar.infrastructure.events.scheduler_queue import (
    JobRunRecorder,
    run_recorded,
)

logger = logging.getLogger(__name__)

#: Queue name for settlement jobs.
SETTLEMENT_QUEUE = "settlement"

#: Periodic job: run the daily settlement tick.
TICK_TASK = "settlement.tick"

#: Default cadence (daily 06:00; overridable via HESABYAR_SETTLEMENT__TICK_CRON).
TICK_CRON = "0 6 * * *"


@dataclass
class SettlementWorkerHandle:
    """A settlement command service bound to one session, with commit/close.

    The task function builds one handle per job run: the service
    flushes, the handle commits, and the session is always closed.
    """

    commands: SettlementCommandService
    commit: Callable[[], None]
    close: Callable[[], None]


#: Factory building a fresh settlement handle per job run (the worker
#: script supplies it; it owns settings and session).
SettlementWorkerFactory = Callable[[], SettlementWorkerHandle]


def register_settlement_tasks(
    app: procrastinate.App,
    settlement_factory: SettlementWorkerFactory,
    *,
    tick_cron: str = TICK_CRON,
    recorder: JobRunRecorder | None = None,
) -> None:
    """Register the settlement tick task on a Procrastinate app."""

    @app.periodic(cron=tick_cron)
    @app.task(queue=SETTLEMENT_QUEUE, name=TICK_TASK, retry=False)
    def tick(timestamp: int = 0) -> None:  # noqa: ARG001 - periodic passes the schedule timestamp
        handle = settlement_factory()
        day = tehran_today(timestamp)
        try:

            def _do() -> dict[str, Any]:
                report = handle.commands.run_daily_tick(day)
                logger.info(
                    "Settlement tick done: moved_to_due=%d moved_to_overdue=%d "
                    "exceptions_created=%d exceptions_escalated=%d review_items_filed=%d",
                    report.moved_to_due,
                    report.moved_to_overdue,
                    report.exceptions_created,
                    report.exceptions_escalated,
                    report.review_items_filed,
                )
                return {
                    "moved_to_due": report.moved_to_due,
                    "moved_to_overdue": report.moved_to_overdue,
                    "exceptions_created": report.exceptions_created,
                    "exceptions_escalated": report.exceptions_escalated,
                    "review_items_filed": report.review_items_filed,
                }

            run_recorded(recorder, TICK_TASK, _do, today=day)
            handle.commit()
        finally:
            handle.close()
