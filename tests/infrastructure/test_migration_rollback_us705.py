"""Migration rollback test: 0042 downgrades and re-upgrades cleanly (US-705).

The upgrade runbook (``docs/runbook-upgrade-rollback.md`` §4)
promises a migration downgrade as the schema-rollback step — this
test proves it for the most recent reversible migration, 0042
(US-704 monitoring tables): on a scratch database, ``alembic
downgrade 0041`` drops the three monitoring tables, and ``alembic
upgrade head`` brings them back (tables + the partial unique
dedup index), with ``alembic_version`` tracking each step.

Patterns, both pre-existing — no new harness:

- the scratch database via an admin DSN is the US-703 live-drill
  pattern (``tests/infrastructure/test_restore_drill_us703.py``:
  ``HESABYAR_TEST_ADMIN_DATABASE_URL``, else the postgres
  superuser via ``PGPASSWORD``, else the application URL's
  credentials against the maintenance database). It runs in both
  CI python jobs, unlike ``test_migrations.py``'s sudo-based
  fixture, which skips where no local postgres superuser exists;
- the Alembic command calls are ``test_migrations.py``'s pattern
  (``HESABYAR_DATABASE_URL`` override + ``alembic.command``).

The scratch database is never the shared ``hesabyar_test`` DB,
which other tests use. Skips cleanly when no admin database URL
is available or the server cannot be reached.
"""

from __future__ import annotations

import os
import urllib.parse
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

from hesabyar.infrastructure.operations.pg_tools import maintenance_url

REPO_ROOT = Path(__file__).resolve().parents[2]

ROLLBACK_DB_NAME = "hesabyar_rollback_test"
TARGET_REVISION = "0042"
PREVIOUS_REVISION = "0041"

#: The tables 0042 creates (and its downgrade drops).
MONITORING_TABLES = {
    "monitoring_alerts",
    "monitoring_alert_channels",
    "monitoring_token_records",
}

#: The partial unique index on firing alerts (the dedup race backstop).
DEDUP_INDEX = "uq_monitoring_alerts_dedup_firing"


def _alembic_config() -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return config


def _plain_url(database_url: str) -> str:
    """Strip the SQLAlchemy +psycopg dialect (psycopg wants plain)."""
    if database_url.startswith("postgresql+psycopg://"):
        return "postgresql://" + database_url[len("postgresql+psycopg://") :]
    return database_url


def _sqlalchemy_url(database_url: str) -> str:
    """Add the +psycopg dialect SQLAlchemy needs."""
    if database_url.startswith("postgresql://"):
        return "postgresql+psycopg://" + database_url[len("postgresql://") :]
    return database_url


def _admin_url() -> str | None:
    """A CREATEDB DSN for the HesabYar postgres server, or None.

    The US-703 live-drill pattern, duplicated (this file's only
    shared helper with that module — the same duplication
    ``_alembic_config`` already has between the two migration-test
    files):

    1. HESABYAR_TEST_ADMIN_DATABASE_URL (explicit test override);
    2. the postgres superuser via PGPASSWORD (the tryton CI job
       exports it);
    3. the application database URL's credentials against the
       maintenance database (the python-tests job's hesabyar role
       IS the bootstrap superuser).
    """
    explicit = os.environ.get("HESABYAR_TEST_ADMIN_DATABASE_URL")
    if explicit:
        return explicit
    password = os.environ.get("PGPASSWORD")
    if password:
        quoted = urllib.parse.quote(password)
        return f"postgresql://postgres:{quoted}@localhost:5432/postgres"
    source = os.environ.get("HESABYAR_DATABASE_URL")
    if source:
        return maintenance_url(source)
    return None


def _scratch_url(admin_url: str) -> str:
    """The scratch database's DSN (same server/credentials as the admin URL)."""
    parsed = urllib.parse.urlparse(_plain_url(admin_url))
    return urllib.parse.urlunparse(parsed._replace(path=f"/{ROLLBACK_DB_NAME}"))


@contextmanager
def _database_url_override(url: str) -> Iterator[None]:
    """Point migrations/env.py at the scratch DB for one Alembic call."""
    previous = os.environ.get("HESABYAR_DATABASE_URL")
    os.environ["HESABYAR_DATABASE_URL"] = url
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("HESABYAR_DATABASE_URL", None)
        else:
            os.environ["HESABYAR_DATABASE_URL"] = previous


def _drop_database(admin_url: str) -> None:
    with psycopg.connect(_plain_url(admin_url), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{ROLLBACK_DB_NAME}" WITH (FORCE);')


def _current_revision(engine: Engine) -> str | None:
    with engine.connect() as conn:
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()


def _tables(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def _index_names(engine: Engine, table: str) -> set[str]:
    return {
        name
        for index in inspect(engine).get_indexes(table)
        if (name := index["name"]) is not None
    }


@pytest.fixture(scope="module")
def rollback_engine() -> Generator[Engine, None, None]:
    """Scratch DB upgraded to head; dropped after the module."""
    admin_url = _admin_url()
    if admin_url is None:
        pytest.skip("no admin database URL available (no scratch DB for the rollback test)")
    assert admin_url is not None  # for pyright: _admin_url's None is handled above
    scratch = _scratch_url(admin_url)
    try:
        _drop_database(admin_url)
        with psycopg.connect(_plain_url(admin_url), autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(f'CREATE DATABASE "{ROLLBACK_DB_NAME}" ENCODING \'UTF8\';')
    except psycopg.Error as exc:
        pytest.skip(f"cannot create the scratch database: {exc}")
    try:
        with _database_url_override(_sqlalchemy_url(scratch)):
            command.upgrade(_alembic_config(), "head")
        engine = create_engine(_sqlalchemy_url(scratch), echo=False)
        yield engine
        engine.dispose()
    finally:
        try:
            _drop_database(admin_url)
        except psycopg.Error:
            pass


def test_0042_downgrade_then_upgrade_roundtrips(rollback_engine: Engine) -> None:
    """downgrade 0041 drops the monitoring tables; upgrade head restores them."""
    scratch_url = str(rollback_engine.url)
    assert _current_revision(rollback_engine) == TARGET_REVISION
    assert MONITORING_TABLES <= _tables(rollback_engine)
    assert DEDUP_INDEX in _index_names(rollback_engine, "monitoring_alerts")

    try:
        with _database_url_override(scratch_url):
            command.downgrade(_alembic_config(), PREVIOUS_REVISION)
        assert _current_revision(rollback_engine) == PREVIOUS_REVISION
        remaining = MONITORING_TABLES & _tables(rollback_engine)
        assert not remaining, f"tables left after downgrade to 0041: {sorted(remaining)}"
    finally:
        # Always re-upgrade: the fixture drops the DB, but a failed
        # test must not leave the scratch DB behind head either.
        with _database_url_override(scratch_url):
            command.upgrade(_alembic_config(), "head")

    assert _current_revision(rollback_engine) == TARGET_REVISION
    missing = MONITORING_TABLES - _tables(rollback_engine)
    assert not missing, f"tables missing after re-upgrade to head: {sorted(missing)}"
    assert DEDUP_INDEX in _index_names(rollback_engine, "monitoring_alerts")
