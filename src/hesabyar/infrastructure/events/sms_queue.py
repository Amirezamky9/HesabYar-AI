"""Procrastinate wiring for the daily SMS reminder job (US-544, §1.3 ۵ج W3).

One periodic job, ``sms.daily_reminders`` (daily 09:00 Tehran by
default): it runs the daily reminder scan across ALL installations
with reminders enabled — legs due soon or overdue at a configured
offset get one SMS reminder each (provider + cost + delivery
status logged; failures to the DLQ + review queue, never silent).

Jalali-aware scheduling, NO Jalali cron (ADR 0003 decision 9):
the job runs on an ordinary Gregorian cron and the targets are
computed in-app from the Jalali due dates — ``next_run`` is a
computed scan, not a cron expression. The job is idempotent (a
leg + offset is reminded at most once — the log dedup + the
partial unique index), so a missed or duplicated run is harmless.

The job never posts a journal and never touches the engine: it
only reads installment schedules and sends SMS. The factory
builds the reminder service like the other factories (fresh
session per run, closed afterwards).
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import Callable
from dataclasses import dataclass

import procrastinate

from hesabyar.application.commands.sms import SmsDailyReport, SmsReminderService

logger = logging.getLogger(__name__)

#: Queue name for SMS jobs.
SMS_QUEUE = "sms"

#: Periodic job: run the daily reminder scan.
DAILY_REMINDERS_TASK = "sms.daily_reminders"

#: Default cadence: daily 09:00 Tehran (05:30 UTC — Iran is UTC+3:30
#: year-round, no DST since 2022). Overridable via create_event_app.
DAILY_REMINDERS_CRON = "30 5 * * *"


@dataclass
class SmsWorkerHandle:
    """An SMS reminder service bound to one session, with commit/close.

    The task function builds one handle per job run: the service
    flushes, the handle commits, and the session is always closed.
    """

    reminders: SmsReminderService
    commit: Callable[[], None]
    close: Callable[[], None]


#: Factory building a fresh SMS handle per job run (the worker
#: script supplies it; it owns settings and session).
SmsWorkerFactory = Callable[[], SmsWorkerHandle]


def register_sms_tasks(
    app: procrastinate.App,
    sms_factory: SmsWorkerFactory,
    *,
    daily_reminders_cron: str = DAILY_REMINDERS_CRON,
) -> None:
    """Register the daily SMS reminder task on a Procrastinate app."""

    @app.periodic(cron=daily_reminders_cron)
    @app.task(queue=SMS_QUEUE, name=DAILY_REMINDERS_TASK, retry=False)
    def daily_reminders(timestamp: int = 0) -> None:  # noqa: ARG001 - periodic passes the schedule timestamp
        handle = sms_factory()
        try:
            report: SmsDailyReport = handle.reminders.run_daily_reminder_job(
                today=datetime.date.today()
            )
            handle.commit()
            logger.info(
                "SMS daily reminders done: installations=%d plans=%d due=%d sent=%d "
                "failed=%d skipped_no_phone=%d skipped_not_opted_in=%d "
                "skipped_no_provider=%d skipped_pending_clearance=%d "
                "skipped_already_sent=%d",
                report.installations_scanned,
                report.plans_scanned,
                report.reminders_due,
                report.sent,
                report.failed,
                report.skipped_no_phone,
                report.skipped_not_opted_in,
                report.skipped_no_provider,
                report.skipped_pending_clearance,
                report.skipped_already_sent,
            )
            for error in report.errors:
                logger.warning("SMS daily reminders error: %s", error)
        finally:
            handle.close()
