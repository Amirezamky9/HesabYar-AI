"""Unit tests for the scriptable pre-upgrade checks (US-705).

The checks from ``docs/runbook-upgrade-rollback.md`` §2 that a
script can run: migrations at head (the US-703 drill check,
reused), backup freshness (the US-703/US-704 BackupStatusSource
contract), and Tryton reachability (a minimal engine-port read).
The backup source and the engine are fakes; the migrations check
runs against a SQLite schema with a hand-set ``alembic_version``
row (the US-703 unit-test pattern).
"""

from __future__ import annotations

import datetime
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from hesabyar.domain.accounting.refs import CompanyRef
from hesabyar.domain.accounting.value_objects import CompanyView
from hesabyar.domain.backup.models import DrillCheckStatus
from hesabyar.domain.monitoring.models import BackupStatus, BackupStatusValue
from hesabyar.infrastructure.operations.pre_upgrade_checks import (
    CHECK_BACKUP_FRESH,
    CHECK_TRYTON_REACHABLE,
    check_backup_fresh,
    check_tryton_reachable,
    run_pre_upgrade_checks,
)
from hesabyar.infrastructure.operations.restore_drill import CHECK_MIGRATIONS
from hesabyar.infrastructure.persistence.postgres.models import Base

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "migrations"
NOW = datetime.datetime(2026, 11, 21, 22, 30, tzinfo=datetime.UTC)


@pytest.fixture()
def engine() -> Iterator[Engine]:
    """An empty HesabYar schema on SQLite."""
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


class FakeBackupSource:
    """A BackupStatusSource stand-in returning canned statuses."""

    def __init__(
        self,
        statuses: list[BackupStatus] | None = None,
        *,
        explode: bool = False,
    ) -> None:
        self._statuses = statuses if statuses is not None else []
        self._explode = explode

    def statuses(self, *, now: datetime.datetime) -> list[BackupStatus]:
        if self._explode:
            raise RuntimeError("status source exploded")
        return self._statuses


class FakeEngine:
    """A minimal AccountingEnginePort stand-in (list_companies only)."""

    def __init__(self, *, companies: list[CompanyView] | None = None, explode: bool = False) -> None:
        self._companies = companies if companies is not None else []
        self._explode = explode

    def list_companies(self) -> list[CompanyView]:
        if self._explode:
            raise RuntimeError("engine exploded in list_companies")
        return self._companies


def _status(database: str, value: BackupStatusValue, **kwargs: object) -> BackupStatus:
    return BackupStatus(database=database, status=value, source="test", **kwargs)  # type: ignore[arg-type]


def _company() -> CompanyView:
    return CompanyView(ref=CompanyRef(1), name="Test Co", currency_code="IRR")


def _head_revision() -> str:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert len(heads) == 1  # the single-head invariant, asserted here too
    return heads[0]


def _set_alembic_version(engine: Engine, version: str) -> None:
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        conn.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:v)"), {"v": version}
        )


# ---------------------------------------------------------------------------
# Backup freshness
# ---------------------------------------------------------------------------


def test_backup_fresh_passes_when_both_databases_ok() -> None:
    source = FakeBackupSource(
        [
            _status("hesabyar", BackupStatusValue.OK),
            _status("tryton", BackupStatusValue.OK),
        ]
    )
    result = check_backup_fresh(source, now=NOW)
    assert result.name == CHECK_BACKUP_FRESH
    assert result.status is DrillCheckStatus.PASS


def test_backup_fresh_fails_when_a_database_failed() -> None:
    source = FakeBackupSource(
        [
            _status("hesabyar", BackupStatusValue.OK),
            _status("tryton", BackupStatusValue.FAILED, error="pg_dump was not found"),
        ]
    )
    result = check_backup_fresh(source, now=NOW)
    assert result.status is DrillCheckStatus.FAIL
    assert "tryton" in result.detail
    assert "pg_dump was not found" in result.detail


def test_backup_fresh_fails_when_a_database_is_stale() -> None:
    source = FakeBackupSource(
        [
            _status(
                "hesabyar",
                BackupStatusValue.STALE,
                last_success_at=datetime.datetime(2026, 11, 19, 22, 30, tzinfo=datetime.UTC),
            ),
            _status("tryton", BackupStatusValue.OK),
        ]
    )
    result = check_backup_fresh(source, now=NOW)
    assert result.status is DrillCheckStatus.FAIL
    assert "hesabyar" in result.detail


def test_backup_fresh_skips_when_unknown_never_silent() -> None:
    """UNKNOWN is reported as SKIPPED — never a fabricated PASS or FAIL."""
    source = FakeBackupSource(
        [
            _status("hesabyar", BackupStatusValue.UNKNOWN),
            _status("tryton", BackupStatusValue.UNKNOWN),
        ]
    )
    result = check_backup_fresh(source, now=NOW)
    assert result.status is DrillCheckStatus.SKIPPED
    assert "unknown" in result.detail


def test_backup_fresh_skips_when_the_source_reports_nothing() -> None:
    result = check_backup_fresh(FakeBackupSource([]), now=NOW)
    assert result.status is DrillCheckStatus.SKIPPED


def test_backup_fresh_fails_when_the_source_raises() -> None:
    result = check_backup_fresh(FakeBackupSource(explode=True), now=NOW)
    assert result.status is DrillCheckStatus.FAIL


def test_backup_fresh_fail_beats_unknown() -> None:
    """One FAILED database fails the check even when the other is UNKNOWN."""
    source = FakeBackupSource(
        [
            _status("hesabyar", BackupStatusValue.UNKNOWN),
            _status("tryton", BackupStatusValue.FAILED, error="S3 403"),
        ]
    )
    result = check_backup_fresh(source, now=NOW)
    assert result.status is DrillCheckStatus.FAIL


# ---------------------------------------------------------------------------
# Tryton reachability
# ---------------------------------------------------------------------------


def test_tryton_reachable_passes_when_the_engine_answers() -> None:
    result = check_tryton_reachable(FakeEngine(companies=[_company()]))  # type: ignore[arg-type]
    assert result.name == CHECK_TRYTON_REACHABLE
    assert result.status is DrillCheckStatus.PASS
    assert "1 company" in result.detail


def test_tryton_reachable_passes_with_zero_companies() -> None:
    """An empty installation is reachable — zero companies is a fact, not a failure."""
    result = check_tryton_reachable(FakeEngine(companies=[]))  # type: ignore[arg-type]
    assert result.status is DrillCheckStatus.PASS
    assert "0 companies" in result.detail


def test_tryton_reachable_fails_when_the_engine_raises() -> None:
    result = check_tryton_reachable(FakeEngine(explode=True))  # type: ignore[arg-type]
    assert result.status is DrillCheckStatus.FAIL


# ---------------------------------------------------------------------------
# Runner composition
# ---------------------------------------------------------------------------


def test_runner_passes_when_everything_is_green(engine: Engine) -> None:
    _set_alembic_version(engine, _head_revision())
    source = FakeBackupSource(
        [
            _status("hesabyar", BackupStatusValue.OK),
            _status("tryton", BackupStatusValue.OK),
        ]
    )
    results = run_pre_upgrade_checks(
        hesabyar_engine=engine,
        migrations_dir=MIGRATIONS_DIR,
        backup_source=source,
        tryton_engine=FakeEngine(companies=[_company()]),  # type: ignore[arg-type]
        now=NOW,
    )
    assert [r.name for r in results] == [
        CHECK_MIGRATIONS,
        CHECK_BACKUP_FRESH,
        CHECK_TRYTON_REACHABLE,
    ]
    assert all(r.status is DrillCheckStatus.PASS for r in results)


def test_runner_reports_each_failure_without_aborting(engine: Engine) -> None:
    """A behind-head database + a stale backup + a dead engine: all three report."""
    _set_alembic_version(engine, "0001")
    source = FakeBackupSource(
        [
            _status("hesabyar", BackupStatusValue.STALE),
            _status("tryton", BackupStatusValue.OK),
        ]
    )
    results = run_pre_upgrade_checks(
        hesabyar_engine=engine,
        migrations_dir=MIGRATIONS_DIR,
        backup_source=source,
        tryton_engine=FakeEngine(explode=True),  # type: ignore[arg-type]
        now=NOW,
    )
    assert len(results) == 3
    assert all(r.status is DrillCheckStatus.FAIL for r in results)
    assert "0001" in results[0].detail  # the migrations check names the stale revision
