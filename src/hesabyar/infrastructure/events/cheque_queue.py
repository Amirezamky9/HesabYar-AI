"""Procrastinate wiring for the daily cheque job (US-531, §5 §2.1).

One periodic job, ``cheque.daily`` (daily 06:30 by default): it runs
the daily cheque job across ALL installations — day-3 and day-(N-1)
never-arrived reminders for site-registered leaves, then the
``expired_unreceived`` transition at the leaf-arrival deadline
(default 7 days).

The job never posts a journal: expiry is a no-journal transition,
and reminders only file review-queue items. Expiry DOES call the
engine (the ``account.check`` record moves too, in the module's own
transaction), so the factory builds a Tryton client like the other
factories. The job is idempotent (a reminder is filed once per
cheque per milestone; expiry is a one-way transition), so a missed
or duplicated run is harmless.
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import Callable
from dataclasses import dataclass

import procrastinate

from hesabyar.application.commands.cheques import ChequeCommandService, ChequeDailyReport

logger = logging.getLogger(__name__)

#: Queue name for cheque jobs.
CHEQUE_QUEUE = "cheques"

#: Periodic job: run the daily cheque job.
DAILY_TASK = "cheque.daily"

#: Default cadence (daily 06:30; overridable via create_event_app).
DAILY_CRON = "30 6 * * *"


@dataclass
class ChequeWorkerHandle:
    """A cheque command service bound to one session, with commit/close.

    The task function builds one handle per job run: the service
    flushes, the handle commits, and the session is always closed.
    """

    commands: ChequeCommandService
    commit: Callable[[], None]
    close: Callable[[], None]


#: Factory building a fresh cheque handle per job run (the worker
#: script supplies it; it owns settings and session).
ChequeWorkerFactory = Callable[[], ChequeWorkerHandle]


def register_cheque_tasks(
    app: procrastinate.App,
    cheque_factory: ChequeWorkerFactory,
    *,
    daily_cron: str = DAILY_CRON,
) -> None:
    """Register the daily cheque task on a Procrastinate app."""

    @app.periodic(cron=daily_cron)
    @app.task(queue=CHEQUE_QUEUE, name=DAILY_TASK, retry=False)
    def daily(timestamp: int = 0) -> None:  # noqa: ARG001 - periodic passes the schedule timestamp
        handle = cheque_factory()
        try:
            report: ChequeDailyReport = handle.commands.run_daily_cheque_job(
                datetime.date.today()
            )
            handle.commit()
            logger.info(
                "Cheque daily job done: reminders_sent=%d expired=%d skipped=%d",
                report.reminders_sent,
                report.expired,
                report.skipped,
            )
        finally:
            handle.close()
