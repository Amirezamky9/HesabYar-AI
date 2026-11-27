"""Procrastinate wiring for the daily reminder job (US-564, §1.3 ۵خ W3).

One periodic job, ``reminders.daily`` (daily 08:30 Tehran by
default): it runs the daily reminder scan across ALL installations
with reminders enabled — cheque due-soon, overdue installments,
uncounted cash boxes, and eNamad expiry — over in-app + email +
SMS (via the US-544 provider registry). Every attempt is logged
with channel + cost; failures go to the DLQ + review queue, never
silent.

Jalali-aware scheduling, NO Jalali cron (ADR 0003 decision 9):
the job runs on an ordinary Gregorian cron and the targets are
computed in-app from the Jalali due dates — ``next_run`` is a
computed scan, not a cron expression. The job is idempotent (one
reminder per event per lead-time per channel — the log dedup +
the partial unique index), so a missed or duplicated run is
harmless. One job with per-type enablement in the reminder
preferences (not one job per type — the simpler shape).

The job never posts a journal and never touches the engine: it
only reads cheque/installment/cash-box schedules and sends
reminders. The factory builds the reminder service like the other
factories (fresh session per run, closed afterwards).

US-560: the job is registered (``reminders.daily``, daily 08:30
Tehran, Friday skipped by default). When a recorder is wired, each
run is gated and recorded.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import procrastinate

from hesabyar.application.commands.reminders import ReminderService
from hesabyar.domain.scheduler.schedules import tehran_today
from hesabyar.infrastructure.events.scheduler_queue import (
    JobRunRecorder,
    run_recorded,
)

logger = logging.getLogger(__name__)

#: Queue name for reminder jobs.
REMINDERS_QUEUE = "reminders"

#: Periodic job: run the daily reminder scan.
DAILY_TASK = "reminders.daily"

#: Default cadence: daily 08:30 Tehran (05:00 UTC — Iran is UTC+3:30
#: year-round, no DST since 2022). Overridable via create_event_app.
#: 08:30 sits just after the default quiet hours end (08:00), so the
#: scheduled run is never quiet-hours-suppressed by default.
DAILY_CRON = "0 5 * * *"


@dataclass
class RemindersWorkerHandle:
    """A reminder service bound to one session, with commit/close.

    The task function builds one handle per job run: the service
    flushes, the handle commits, and the session is always closed.
    """

    reminders: ReminderService
    commit: Callable[[], None]
    close: Callable[[], None]


#: Factory building a fresh reminders handle per job run (the worker
#: script supplies it; it owns settings and session).
RemindersWorkerFactory = Callable[[], RemindersWorkerHandle]


def register_reminders_tasks(
    app: procrastinate.App,
    reminders_factory: RemindersWorkerFactory,
    *,
    daily_cron: str = DAILY_CRON,
    recorder: JobRunRecorder | None = None,
) -> None:
    """Register the daily reminder task on a Procrastinate app."""

    @app.periodic(cron=daily_cron)
    @app.task(queue=REMINDERS_QUEUE, name=DAILY_TASK, retry=False)
    def daily_reminders(timestamp: int = 0) -> None:  # noqa: ARG001 - periodic passes the schedule timestamp
        handle = reminders_factory()
        day = tehran_today(timestamp)
        try:

            def _do() -> dict[str, Any]:
                report = handle.reminders.run_daily_reminder_job(today=day)
                logger.info(
                    "Daily reminders done: installations=%d due=%d in_app=%d "
                    "email=%d sms=%d failed=%d skipped_quiet=%d "
                    "skipped_no_consent=%d skipped_no_phone=%d "
                    "skipped_no_provider=%d skipped_already_sent=%d "
                    "skipped_email_not_configured=%d",
                    report.installations_scanned,
                    report.reminders_due,
                    report.sent_in_app,
                    report.sent_email,
                    report.sent_sms,
                    report.failed,
                    report.skipped_quiet_hours,
                    report.skipped_no_consent,
                    report.skipped_no_phone,
                    report.skipped_no_provider,
                    report.skipped_already_sent,
                    report.skipped_email_not_configured,
                )
                for error in report.errors:
                    logger.warning("Daily reminders error: %s", error)
                return report.summary()

            run_recorded(recorder, DAILY_TASK, _do, today=day)
            handle.commit()
        finally:
            handle.close()
