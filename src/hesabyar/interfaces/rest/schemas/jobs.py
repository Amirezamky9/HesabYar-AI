"""Request/response schemas for the scheduled-jobs endpoints (US-560).

Responses map from the domain objects — no ORM object ever leaks.
``next_run`` is an aware Tehran datetime (UTC+3:30); ``time_of_day``
is Tehran wall time (``HH:MM``). There are no request bodies: list /
trigger / enable / disable take only the job id from the path.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from hesabyar.domain.scheduler.models import JobRun, JobStatus
from hesabyar.domain.scheduler.schedules import JobSchedule


class JobScheduleResponse(BaseModel):
    """A job's schedule spec, as stored in the registry."""

    kind: str = Field(description="interval | daily | jalali_monthly | jalali_yearly")
    interval_minutes: int | None = Field(default=None, description="Interval cadence in minutes")
    time_of_day: str | None = Field(default=None, description="Tehran wall time (HH:MM) for daily/monthly/yearly")
    jalali_day: int | None = Field(default=None, description="Jalali day of month (monthly/yearly)")
    jalali_month: int | None = Field(default=None, description="Jalali month (yearly)")
    skip_friday: bool = Field(description="Whether a daily job skips Friday (the weekend default)")

    @classmethod
    def from_schedule(cls, schedule: JobSchedule) -> JobScheduleResponse:
        """Map a domain schedule to its wire form."""
        return cls(
            kind=schedule.kind.value,
            interval_minutes=schedule.interval_minutes,
            time_of_day=(
                schedule.time_of_day.strftime("%H:%M") if schedule.time_of_day else None
            ),
            jalali_day=schedule.jalali_day,
            jalali_month=schedule.jalali_month,
            skip_friday=schedule.skip_friday,
        )


class JobRunResponse(BaseModel):
    """One recorded run of a job."""

    id: str = Field(description="Run id (UUID)")
    job_id: str = Field(description="Owning job id")
    started_at: datetime = Field(description="Run start (UTC)")
    finished_at: datetime | None = Field(description="Run finish (UTC); null while running")
    outcome: str = Field(description="success | failed | skipped")
    summary: dict[str, Any] = Field(description="Run outcome summary (counts + ids)")
    error: str | None = Field(description="Failure text when the outcome is failed")
    triggered_by: str = Field(description="scheduled | manual")

    @classmethod
    def from_run(cls, run: JobRun) -> JobRunResponse:
        """Map a domain run to its wire form."""
        return cls(
            id=run.id,
            job_id=run.job_id,
            started_at=run.started_at,
            finished_at=run.finished_at,
            outcome=run.outcome.value,
            summary=dict(run.summary),
            error=run.error,
            triggered_by=run.triggered_by.value,
        )


class JobResponse(BaseModel):
    """One registered job: definition + last run + computed next run."""

    id: str = Field(description="Job id == Procrastinate task name")
    queue: str = Field(description="Procrastinate queue the job runs on")
    description: str = Field(description="What the job does")
    enabled: bool = Field(description="False stops the schedule (manual triggers still work)")
    schedule: JobScheduleResponse = Field(description="The job's schedule spec")
    last_run: JobRunResponse | None = Field(description="Most recent run, or null when never run")
    last_outcome: str | None = Field(description="Last run's outcome, or null when never run")
    next_run: datetime = Field(description="Next scheduled run (Tehran wall time, computed in-app)")
    next_run_clamped: bool = Field(
        description="True when the next run's Jalali day was clamped to the month's length"
    )

    @classmethod
    def from_status(cls, status: JobStatus) -> JobResponse:
        """Map a domain job status to its wire form."""
        definition = status.definition
        return cls(
            id=definition.id,
            queue=definition.queue,
            description=definition.description,
            enabled=definition.enabled,
            schedule=JobScheduleResponse.from_schedule(definition.schedule),
            last_run=(
                JobRunResponse.from_run(status.last_run) if status.last_run else None
            ),
            last_outcome=(
                status.last_outcome.value if status.last_outcome is not None else None
            ),
            next_run=status.next_run.run_at,
            next_run_clamped=status.next_run.clamped,
        )
