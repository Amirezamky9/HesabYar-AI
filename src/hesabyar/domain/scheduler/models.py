"""Job registry domain model (US-560, phased design §6.5 / §1.3 ۵خ).

Every periodic job in the platform has one registry row: an id (the
Procrastinate task name — the id IS the task, so the registry can
never name a job that does not exist), a queue, a Jalali-aware
schedule, an enabled flag, and its run history. ``next_run`` is
computed in-app from the schedule + last run, never stored.

The registry seeds :data:`DEFAULT_JOBS` on first use (the service's
``ensure_defaults`` inserts only missing rows, so an operator's
enable/disable choice is never overwritten by a re-seed). A job id
not in the seed is unknown — the registry is closed, like the
canonical event types: a new job needs a code change + a seed entry,
never an ad-hoc row.

Timezone note: the ``time_of_day`` values below are Tehran wall time;
the Procrastinate crons that fire them are UTC (Iran is UTC+3:30,
no DST). The UTC↔Tehran mapping is asserted per job by
``tests/unit/test_scheduler_queue.py``.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from enum import StrEnum, unique
from typing import Any

from hesabyar.domain.scheduler.schedules import (
    JobSchedule,
    NextRun,
    ScheduleKind,
)


@unique
class JobOutcome(StrEnum):
    """How one job run ended."""

    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"  # disabled, or not a scheduled day (e.g. Friday)


@unique
class TriggeredBy(StrEnum):
    """What started a run: the Procrastinate schedule, or an operator."""

    SCHEDULED = "scheduled"
    MANUAL = "manual"


@dataclass(frozen=True)
class JobDefinition:
    """One registered periodic job (a row in ``scheduled_jobs``)."""

    id: str  # == the Procrastinate task name
    queue: str
    schedule: JobSchedule
    enabled: bool = True
    description: str = ""


@dataclass(frozen=True)
class JobRun:
    """One recorded run of a job (a row in ``scheduled_job_runs``).

    ``summary`` is a small JSON object (counts + ids, never secrets);
    ``error`` carries the failure text when ``outcome`` is ``failed``.
    A failed run is the job's dead-letter record — the DLQ view for
    scheduler failures, alongside the review-queue item.
    """

    id: str
    job_id: str
    started_at: datetime.datetime
    finished_at: datetime.datetime | None
    outcome: JobOutcome
    summary: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    triggered_by: TriggeredBy = TriggeredBy.SCHEDULED


@dataclass(frozen=True)
class JobStatus:
    """The registry's read view: definition + last run + next run."""

    definition: JobDefinition
    last_run: JobRun | None
    next_run: NextRun

    @property
    def last_outcome(self) -> JobOutcome | None:
        """The last run's outcome, or None when the job never ran."""
        return self.last_run.outcome if self.last_run is not None else None


def _daily(hour: int, minute: int, *, skip_friday: bool = True) -> JobSchedule:
    return JobSchedule(
        kind=ScheduleKind.DAILY,
        time_of_day=datetime.time(hour, minute),
        skip_friday=skip_friday,
    )


def _interval(minutes: int) -> JobSchedule:
    return JobSchedule(kind=ScheduleKind.INTERVAL, interval_minutes=minutes)


#: The recurring-generation job id (US-563): the daily run that
#: generates due recurring invoices/journals as drafts. Defined here
#: (not in the queue module) so the generator service can file its
#: review items under the same id without importing infrastructure.
RECURRING_GENERATION_JOB_ID = "scheduler.recurring_generation"


#: The platform's periodic jobs, in registry order. The id is the
#: Procrastinate task name; the schedule is Tehran wall time (see the
#: module docstring for the UTC cron mapping).
DEFAULT_JOBS: tuple[JobDefinition, ...] = (
    JobDefinition(
        id="events.sweep",
        queue="events",
        schedule=_interval(1),
        description="Event inbox/outbox sweep: process due inbox events + pending outbox events (US-501).",
    ),
    JobDefinition(
        id="settlement.tick",
        queue="settlement",
        schedule=_daily(9, 30),
        description="Settlement daily tick: tranche expected → due at maturity, due → overdue past grace (US-521).",
    ),
    JobDefinition(
        id="packaging.supply_check",
        queue="packaging",
        schedule=_daily(10, 0),
        description="Supply-wizard result read: new purchase requests since the watermark + days-of-cover (US-515).",
    ),
    JobDefinition(
        id="cheque.daily",
        queue="cheques",
        schedule=_daily(10, 0),
        description="Cheque daily job: due-soon reminders + never-arrived expiry (US-531).",
    ),
    JobDefinition(
        id="sms.daily_reminders",
        queue="sms",
        schedule=_daily(9, 0),
        description="SMS daily reminders: installment legs due soon or overdue, one SMS per leg per offset (US-544).",
    ),
    JobDefinition(
        id="scheduler.dunning_sweep",
        queue="scheduler",
        schedule=_daily(8, 0),
        description=(
            "Dunning sweep: overdue installment legs → review items + "
            "late-fee assessment (US-541, on schedule since US-560)."
        ),
    ),
    JobDefinition(
        id="scheduler.store_sync",
        queue="scheduler",
        schedule=_interval(15),
        description=(
            "Reconciliation polling: sync every active store's changes "
            "with its saved cursor (US-512/US-533, on schedule since US-560)."
        ),
    ),
    JobDefinition(
        id="scheduler.inbox_staleness",
        queue="scheduler",
        schedule=_interval(60),
        description=(
            "Inbox staleness alert: events stuck in received/processing "
            "past 24h → review items (US-501, on schedule since US-560)."
        ),
    ),
    JobDefinition(
        id="reminders.daily",
        queue="reminders",
        schedule=_daily(8, 30),
        description=(
            "Daily reminders: cheque due-soon (7/3 days), overdue "
            "installments, uncounted cash boxes, eNamad expiry — "
            "in-app + email + SMS via the US-544 registry (US-564). "
            "One job with per-type enablement in the reminder "
            "preferences (not one job per type)."
        ),
    ),
    JobDefinition(
        id=RECURRING_GENERATION_JOB_ID,
        queue="scheduler",
        schedule=_daily(7, 30),
        description=(
            "Recurring generation: every due recurring invoice/journal "
            "occurrence becomes a draft in the engine (US-563)."
        ),
    ),
)

#: Ids of the seeded jobs, for the closed-registry check.
DEFAULT_JOB_IDS: frozenset[str] = frozenset(job.id for job in DEFAULT_JOBS)
