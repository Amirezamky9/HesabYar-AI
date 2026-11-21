"""Unit tests for JobRegistryBackupStatusSource (US-703 + US-704 integration).

The source reads the US-560 job registry: the ``backup.databases``
job backs up both databases in one run, so its latest run IS the
backup status for both. Tests run on an in-memory SQLite schema
(the US-703 drill-test pattern — FKs unenforced, so job runs need
no ``scheduled_jobs`` parent row).
"""

from __future__ import annotations

import datetime
from collections.abc import Iterator
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from hesabyar.domain.monitoring.models import BackupStatusValue
from hesabyar.infrastructure.persistence.postgres.models import Base, ScheduledJobRunModel
from hesabyar.infrastructure.persistence.postgres.monitoring_repository import (
    JobRegistryBackupStatusSource,
)

NOW = datetime.datetime(2026, 11, 21, 13, 0, tzinfo=datetime.UTC)
MAX_AGE_HOURS = 26


@pytest.fixture()
def engine() -> Iterator[Engine]:
    """An empty HesabYar schema on SQLite (FKs unenforced — see above)."""
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine) as sess:
        yield sess


@pytest.fixture()
def source(session: Session) -> JobRegistryBackupStatusSource:
    return JobRegistryBackupStatusSource(session, max_age_hours=MAX_AGE_HOURS)


def _run(
    session: Session,
    *,
    job_id: str = "backup.databases",
    started_at: datetime.datetime,
    finished_at: datetime.datetime | None = None,
    outcome: str = "success",
    error: str | None = None,
) -> ScheduledJobRunModel:
    row = ScheduledJobRunModel(
        id=str(uuid4()),
        job_id=job_id,
        started_at=started_at,
        finished_at=finished_at if finished_at is not None else started_at,
        outcome=outcome,
        summary={},
        error=error,
        triggered_by="scheduled",
    )
    session.add(row)
    session.flush()
    return row


def test_no_runs_is_unknown_not_fabricated(
    source: JobRegistryBackupStatusSource,
) -> None:
    statuses = source.statuses(now=NOW)
    assert [s.database for s in statuses] == ["hesabyar", "tryton"]
    assert all(s.status is BackupStatusValue.UNKNOWN for s in statuses)
    assert all(s.source == "job-registry" for s in statuses)
    assert all(s.last_success_at is None for s in statuses)
    assert all(s.last_attempt_at is None for s in statuses)
    assert all(s.error is None for s in statuses)


def test_recent_success_is_ok_for_both_databases(
    session: Session, source: JobRegistryBackupStatusSource
) -> None:
    started = NOW - datetime.timedelta(hours=1)
    finished = NOW - datetime.timedelta(minutes=55)
    _run(session, started_at=started, finished_at=finished, outcome="success")
    statuses = source.statuses(now=NOW)
    assert all(s.status is BackupStatusValue.OK for s in statuses)
    assert all(s.last_success_at == finished for s in statuses)
    assert all(s.last_attempt_at == started for s in statuses)


def test_old_success_is_stale(
    session: Session, source: JobRegistryBackupStatusSource
) -> None:
    started = NOW - datetime.timedelta(hours=30)
    _run(session, started_at=started, finished_at=started, outcome="success")
    statuses = source.statuses(now=NOW)
    assert all(s.status is BackupStatusValue.STALE for s in statuses)
    assert all(s.last_success_at == started for s in statuses)


def test_failed_latest_run_is_failed_even_with_old_success(
    session: Session, source: JobRegistryBackupStatusSource
) -> None:
    old = NOW - datetime.timedelta(hours=30)
    _run(session, started_at=old, finished_at=old, outcome="success")
    failed_at = NOW - datetime.timedelta(minutes=10)
    _run(
        session,
        started_at=failed_at,
        finished_at=failed_at,
        outcome="failed",
        error="pg_dump exited 1: could not connect to server",
    )
    statuses = source.statuses(now=NOW)
    assert all(s.status is BackupStatusValue.FAILED for s in statuses)
    assert all(s.last_attempt_at == failed_at for s in statuses)
    # The old success is still reported: the operator sees how old
    # the last GOOD backup is, not just that the newest failed.
    assert all(s.last_success_at == old for s in statuses)
    assert all(
        s.error == "pg_dump exited 1: could not connect to server" for s in statuses
    )


def test_failed_run_sanitizes_urls_and_truncates(
    session: Session, source: JobRegistryBackupStatusSource
) -> None:
    failed_at = NOW - datetime.timedelta(minutes=5)
    long_error = "upload failed: https://s3.example.invalid/bucket/key " + "x" * 300
    _run(
        session,
        started_at=failed_at,
        finished_at=failed_at,
        outcome="failed",
        error=long_error,
    )
    statuses = source.statuses(now=NOW)
    assert all(s.status is BackupStatusValue.FAILED for s in statuses)
    for s in statuses:
        assert s.error is not None
        assert "https://" not in s.error
        assert "<url>" in s.error
        assert len(s.error) == 200
        assert s.error.endswith("…")


def test_skipped_latest_run_falls_through_to_success_age(
    session: Session, source: JobRegistryBackupStatusSource
) -> None:
    success_at = NOW - datetime.timedelta(hours=2)
    _run(session, started_at=success_at, finished_at=success_at, outcome="success")
    skipped_at = NOW - datetime.timedelta(minutes=30)
    _run(session, started_at=skipped_at, finished_at=skipped_at, outcome="skipped")
    statuses = source.statuses(now=NOW)
    assert all(s.status is BackupStatusValue.OK for s in statuses)
    assert all(s.last_success_at == success_at for s in statuses)
    assert all(s.last_attempt_at == skipped_at for s in statuses)


def test_skipped_only_is_unknown_with_attempt_recorded(
    session: Session, source: JobRegistryBackupStatusSource
) -> None:
    skipped_at = NOW - datetime.timedelta(minutes=30)
    _run(session, started_at=skipped_at, finished_at=skipped_at, outcome="skipped")
    statuses = source.statuses(now=NOW)
    assert all(s.status is BackupStatusValue.UNKNOWN for s in statuses)
    assert all(s.last_attempt_at == skipped_at for s in statuses)
    assert all(s.last_success_at is None for s in statuses)


def test_other_jobs_runs_are_ignored(
    session: Session, source: JobRegistryBackupStatusSource
) -> None:
    failed_at = NOW - datetime.timedelta(minutes=10)
    _run(
        session,
        job_id="events.sweep",
        started_at=failed_at,
        finished_at=failed_at,
        outcome="failed",
        error="sweep broke",
    )
    statuses = source.statuses(now=NOW)
    assert all(s.status is BackupStatusValue.UNKNOWN for s in statuses)
    assert all(s.last_attempt_at is None for s in statuses)
