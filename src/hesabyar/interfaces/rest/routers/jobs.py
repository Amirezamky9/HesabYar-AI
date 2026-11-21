"""Scheduled-jobs routes (US-560, Phase 5-7 ۵خ W1).

Thin routes over the scheduler service: list jobs, trigger one run
on demand, enable/disable a job. The registry is tenantless by
design (jobs run across all installations), so these endpoints are
NOT tenant-scoped — the ``jobs:manage`` scope is the guard.

- ``GET /jobs`` — every registered job with its schedule, last run,
  last outcome, and computed next run (seeds the registry first).
- ``GET /jobs/{job_id}`` — one job's status.
- ``GET /jobs/{job_id}/runs`` — the job's recent runs, newest first.
- ``POST /jobs/{job_id}/trigger`` — run one job now, on demand
  (synchronous; returns the recorded run — a failed trigger returns
  the failed run record, not an exception).
- ``POST /jobs/{job_id}/enable`` / ``POST /jobs/{job_id}/disable``
  — enable/disable the schedule (a disabled job is skipped by its
  Procrastinate task; a manual trigger still works).

A manual trigger ignores the enabled flag and the calendar
(Friday-skip applies to scheduled runs only). See
``docs/scheduler.md``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.orm import Session

from hesabyar.application.commands.scheduler import SchedulerService
from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.interfaces.rest.dependencies import (
    get_db_session,
    get_scheduler_service,
    require_scopes,
)
from hesabyar.interfaces.rest.errors import translate_errors
from hesabyar.interfaces.rest.schemas.jobs import (
    JobResponse,
    JobRunResponse,
)

router = APIRouter(tags=["jobs"])


@router.get(
    "/jobs",
    response_model=list[JobResponse],
    summary="List scheduled jobs with last run and computed next run",
)
def list_jobs(
    client: AuthenticatedClient = Depends(require_scopes("jobs:manage")),  # noqa: ARG001 - scope guard
    session: Session = Depends(get_db_session),
    service: SchedulerService = Depends(get_scheduler_service),
) -> list[JobResponse]:
    """Return every registered job (seeds the registry on first call)."""
    with translate_errors():
        jobs = service.list_jobs()
    session.commit()  # persist the seed rows, if any were inserted
    return [JobResponse.from_status(job) for job in jobs]


@router.get(
    "/jobs/{job_id}",
    response_model=JobResponse,
    summary="Get one scheduled job's status",
)
def get_job(
    job_id: str = Path(..., description="Job id, e.g. 'settlement.tick'"),
    client: AuthenticatedClient = Depends(require_scopes("jobs:manage")),  # noqa: ARG001 - scope guard
    session: Session = Depends(get_db_session),
    service: SchedulerService = Depends(get_scheduler_service),
) -> JobResponse:
    """Return one job's status (404 when the id is not registered)."""
    with translate_errors():
        job = service.get_job(job_id)
    session.commit()  # persist the seed rows, if any were inserted
    return JobResponse.from_status(job)


@router.get(
    "/jobs/{job_id}/runs",
    response_model=list[JobRunResponse],
    summary="List a job's recent runs, newest first",
)
def list_job_runs(
    job_id: str = Path(..., description="Job id, e.g. 'settlement.tick'"),
    limit: int = Query(default=20, ge=1, le=100, description="Max runs to return"),
    client: AuthenticatedClient = Depends(require_scopes("jobs:manage")),  # noqa: ARG001 - scope guard
    session: Session = Depends(get_db_session),
    service: SchedulerService = Depends(get_scheduler_service),
) -> list[JobRunResponse]:
    """Return a job's recent runs (404 when the id is not registered)."""
    with translate_errors():
        runs = service.list_runs(job_id, limit=limit)
    session.commit()  # persist the seed rows, if any were inserted
    return [JobRunResponse.from_run(run) for run in runs]


@router.post(
    "/jobs/{job_id}/trigger",
    response_model=JobRunResponse,
    summary="Run one job now, on demand",
)
def trigger_job(
    job_id: str = Path(..., description="Job id, e.g. 'settlement.tick'"),
    client: AuthenticatedClient = Depends(require_scopes("jobs:manage")),
    session: Session = Depends(get_db_session),
    service: SchedulerService = Depends(get_scheduler_service),
) -> JobRunResponse:
    """Run one job synchronously and return the recorded run.

    A failed trigger returns the failed run record (outcome
    ``failed`` with the error) — the failure is recorded in the
    registry and filed to the review queue, never raised away.
    """
    with translate_errors():
        run = service.trigger_job(job_id, actor=client.client_id)
    session.commit()
    return JobRunResponse.from_run(run)


@router.post(
    "/jobs/{job_id}/enable",
    response_model=JobResponse,
    summary="Enable a scheduled job",
)
def enable_job(
    job_id: str = Path(..., description="Job id, e.g. 'settlement.tick'"),
    client: AuthenticatedClient = Depends(require_scopes("jobs:manage")),  # noqa: ARG001 - scope guard
    session: Session = Depends(get_db_session),
    service: SchedulerService = Depends(get_scheduler_service),
) -> JobResponse:
    """Enable a job's schedule (404 when the id is not registered)."""
    with translate_errors():
        job = service.set_enabled(job_id, enabled=True)
    session.commit()
    return JobResponse.from_status(job)


@router.post(
    "/jobs/{job_id}/disable",
    response_model=JobResponse,
    summary="Disable a scheduled job",
)
def disable_job(
    job_id: str = Path(..., description="Job id, e.g. 'settlement.tick'"),
    client: AuthenticatedClient = Depends(require_scopes("jobs:manage")),  # noqa: ARG001 - scope guard
    session: Session = Depends(get_db_session),
    service: SchedulerService = Depends(get_scheduler_service),
) -> JobResponse:
    """Disable a job's schedule (manual triggers still work)."""
    with translate_errors():
        job = service.set_enabled(job_id, enabled=False)
    session.commit()
    return JobResponse.from_status(job)
