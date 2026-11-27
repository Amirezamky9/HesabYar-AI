"""Live restore-drill tests (US-703).

The REAL drill end to end: pg_dump -> encrypt -> upload to a local
store -> restore into scratch databases -> the five reconciliation
checks. Runs in CI (both python-tests jobs); skips cleanly when:

- pg_dump/pg_restore are missing from the runner;
- no CREATEDB role is available (the drill needs one to make
  scratch databases);
- pg_dump is OLDER than the server (pg_dump refuses to dump a newer
  server — install a matching postgresql-client);
- (the reverse mismatch is handled by the product, not skipped: a
  pg_dump 17+ client dumping a PG16 server writes
  ``SET transaction_timeout = 0;`` into the archive, which the PG16
  server rejects on restore — ``restore`` tolerates exactly that
  version-mismatched GUC SET, see pg_tools._only_benign_guc_errors);
- (full-pair test only) trytond is not reachable, or the Tryton
  database DSN does not connect.

The HesabYar source is a throwaway database upgraded to head and
seeded with one balanced payment + tranches — never the shared
hesabyar_test DB, which other tests use. The Tryton source is the
SHARED Tryton test database, dumped read-only: the test never
writes into it (no moves posted, no records created).
"""

from __future__ import annotations

import datetime
import os
import re
import shutil
import subprocess
import urllib.parse
import uuid
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from hesabyar.domain.backup.models import BackupTarget, DrillCheckStatus
from hesabyar.infrastructure.accounting.tryton.adapter import TrytonAccountingAdapter
from hesabyar.infrastructure.accounting.tryton.client import TrytonRestClient
from hesabyar.infrastructure.auth.credential_vault import generate_encryption_key
from hesabyar.infrastructure.config import BackupSettings
from hesabyar.infrastructure.operations.factories import (
    build_backup_service,
    build_drill_service,
)
from hesabyar.infrastructure.operations.pg_tools import (
    PgDumpRestoreRunner,
    maintenance_url,
    with_database_name,
)
from hesabyar.infrastructure.operations.restore_drill import ScratchEngineFactory
from hesabyar.infrastructure.persistence.postgres.models import (
    GatewayContractModel,
    GatewayPaymentModel,
    PaymentChannelModel,
    SettlementTrancheModel,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_DB_NAME = "hesabyar_drill_source_test"
INSTALLATION_ID = "00000000-0000-0000-0000-000000000001"


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


def _pg_dump_major() -> int:
    result = subprocess.run(
        ["pg_dump", "--version"], capture_output=True, text=True, timeout=30, check=False
    )
    match = re.search(r"(\d+)\.", result.stdout)
    if result.returncode != 0 or not match:
        pytest.skip("could not read the pg_dump version")
    return int(match.group(1))


def _server_major(admin_url: str) -> int:
    with psycopg.connect(_plain_url(admin_url), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SHOW server_version")
            row = cur.fetchone()
    if row is None:
        raise RuntimeError("SHOW server_version returned no row")
    match = re.match(r"(\d+)\.", str(row[0]))
    if not match:
        raise RuntimeError(f"unparseable server version: {row[0]!r}")
    return int(match.group(1))


@dataclass(frozen=True)
class SourceDb:
    """The throwaway HesabYar source database (admin credentials)."""

    url: str  # plain postgresql:// DSN for the source DB
    admin_url: str  # the maintenance-DB DSN the drill creates scratch DBs with


def _seed_payment(source_url: str) -> None:
    """One balanced payment + two tranches (the tranche check's data)."""
    engine = create_engine(_sqlalchemy_url(source_url))
    try:
        with Session(engine) as session:
            channel = PaymentChannelModel(
                installation_id=INSTALLATION_ID,
                channel_key="snapppay",
                display_name_fa="اسنپ‌پی",
                channel_type="bnpl",
                environment="sandbox",
            )
            session.add(channel)
            session.flush()
            contract = GatewayContractModel(
                channel_id=channel.id,
                version=1,
                valid_from=datetime.date(2026, 1, 1),
                fee_percent=Decimal("2"),
            )
            session.add(contract)
            session.flush()
            payment = GatewayPaymentModel(
                installation_id=INSTALLATION_ID,
                payment_channel_id=channel.id,
                gateway_transaction_id=f"drill-{uuid.uuid4().hex[:8]}",
                gross_irr=100_000,
                fee_irr=2_000,
                fee_vat_irr=200,
                net_irr=97_800,
                contract_version_id=contract.id,
                fee_rate_snapshot=Decimal("2"),
                sales_period_jalali="1405-08",
                gateway_confirmed_at=datetime.datetime(
                    2026, 11, 20, 10, 0, tzinfo=datetime.UTC
                ),
                source="store",
            )
            session.add(payment)
            session.flush()
            for index, (gross, net) in enumerate(
                ((50_000, 48_900), (50_000, 48_900)), start=1
            ):
                session.add(
                    SettlementTrancheModel(
                        installation_id=INSTALLATION_ID,
                        gateway_payment_id=payment.id,
                        tranche_index=index,
                        tranche_count=2,
                        sales_period_jalali="1405-08",
                        expected_window_start=datetime.date(2026, 11, 20),
                        expected_date=datetime.date(2026, 12, 20),
                        gross_irr=gross,
                        fee_irr=0,
                        fee_vat_irr=0,
                        net_irr=net,
                        confirmed_net_irr=net if index == 1 else 0,
                        received_net_irr=net if index == 1 else 0,
                    )
                )
            session.commit()
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def source_db() -> Generator[SourceDb, None, None]:
    """Throwaway HesabYar DB at head, seeded; dropped after the module."""
    if shutil.which("pg_dump") is None or shutil.which("pg_restore") is None:
        pytest.skip("pg_dump/pg_restore are not installed on this runner")
    admin_url = _admin_url()
    if admin_url is None:
        pytest.skip(
            "no admin database URL available "
            "(set HESABYAR_TEST_ADMIN_DATABASE_URL)"
        )
    if _pg_dump_major() < _server_major(admin_url):
        pytest.skip(
            "pg_dump is older than the postgres server (it refuses to dump "
            "a newer server) — install a matching postgresql-client"
        )
    dumper = PgDumpRestoreRunner()
    try:
        dumper.drop_database(admin_url, SOURCE_DB_NAME)
        dumper.create_database(admin_url, SOURCE_DB_NAME)
    except Exception as exc:
        pytest.skip(f"cannot create the throwaway source DB (no CREATEDB role?): {exc}")
    source_url = with_database_name(admin_url, SOURCE_DB_NAME)
    previous_url = os.environ.get("HESABYAR_DATABASE_URL")
    # migrations/env.py prefers HESABYAR_DATABASE_URL over alembic.ini.
    os.environ["HESABYAR_DATABASE_URL"] = _sqlalchemy_url(source_url)
    try:
        command.upgrade(_alembic_config(), "head")
        _seed_payment(source_url)
        yield SourceDb(url=_plain_url(source_url), admin_url=admin_url)
    finally:
        if previous_url is None:
            os.environ.pop("HESABYAR_DATABASE_URL", None)
        else:
            os.environ["HESABYAR_DATABASE_URL"] = previous_url
        try:
            dumper.drop_database(admin_url, SOURCE_DB_NAME)
        except Exception:
            pass


def _tryton_database_url() -> str:
    """The pg_dump DSN for the Tryton database (test scaffolding).

    Prefers the real backup setting
    (HESABYAR_BACKUP__TRYTON_DATABASE_URL); otherwise the
    setup-tryton.sh defaults (role tryton / tryton_pass).
    """
    explicit = os.environ.get("HESABYAR_BACKUP__TRYTON_DATABASE_URL")
    if explicit:
        return explicit
    database = os.environ.get("TRYTON_DATABASE", "hesabyar_tryton_test")
    role = os.environ.get("TRYTON_DB_ROLE", "tryton")
    password = os.environ.get("TRYTON_DB_PASSWORD", "tryton_pass")
    host = os.environ.get("TRYTON_DB_HOST", "localhost")
    return f"postgresql://{role}:{urllib.parse.quote(password)}@{host}:5432/{database}"


def _tryton_is_usable() -> tuple[str, str, str]:
    """(base_url, database, api_key) — or skip when trytond is down."""
    base_url = os.environ.get("TRYTON_BASE_URL", "http://localhost:8000")
    database = os.environ.get("TRYTON_DATABASE", "hesabyar_tryton_test")
    api_key = os.environ.get("TRYTON_API_KEY", "hesabyar-dev-rest-key")
    client = TrytonRestClient(base_url, database, api_key, timeout=10.0)
    try:
        if not client.ping():
            pytest.skip("trytond is not reachable (TRYTON_BASE_URL)")
    finally:
        client.close()
    # The DSN must connect too (a wrong role/password is scaffolding,
    # not a drill failure).
    try:
        with psycopg.connect(
            _plain_url(_tryton_database_url()), connect_timeout=5
        ) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
    except Exception as exc:
        pytest.skip(f"cannot connect to the Tryton database: {exc}")
    return base_url, database, api_key


def _engine_factory(base_url: str, api_key: str) -> ScratchEngineFactory:
    """A REST adapter for a Tryton database by name (same trytond)."""

    @contextmanager
    def factory(database: str) -> Iterator[TrytonAccountingAdapter]:
        client = TrytonRestClient(base_url, database, api_key, timeout=30.0)
        try:
            yield TrytonAccountingAdapter(client)
        finally:
            client.close()

    return factory


def _backup_settings(
    tmp_path: Path,
    *,
    tryton_url: str | None = None,
    tryton_admin_url: str | None = None,
) -> BackupSettings:
    return BackupSettings(
        local_directory=str(tmp_path),
        encryption_key=generate_encryption_key(),
        tryton_database_url=tryton_url,
        tryton_admin_database_url=tryton_admin_url,
    )


def test_hesabyar_only_drill_passes(source_db: SourceDb, tmp_path: Path) -> None:
    """The drill restores the HesabYar backup and reconciles it."""
    settings = _backup_settings(tmp_path)
    dumper = PgDumpRestoreRunner()
    backup = build_backup_service(settings, dumper)
    manifest = backup.run_backup(BackupTarget.HESABYAR_DB, source_db.url)
    drill = build_drill_service(
        settings, dumper, None, hesabyar_database_url=source_db.url
    )
    report = drill.run(
        {BackupTarget.HESABYAR_DB: manifest},
        targets=(BackupTarget.HESABYAR_DB,),
    )
    assert report.passed, [f"{c.name}: {c.detail}" for c in report.failed_checks()]
    checks = {c.name: c.status for c in report.checks}
    assert checks["hesabyar.manifest_checksum"] is DrillCheckStatus.PASS
    assert checks["hesabyar.restore"] is DrillCheckStatus.PASS
    assert checks["hesabyar.migrations"] is DrillCheckStatus.PASS
    assert checks["hesabyar.orphaned_documents"] is DrillCheckStatus.PASS
    assert checks["hesabyar.settlement_tranches"] is DrillCheckStatus.PASS
    assert checks["tryton.trial_balance"] is DrillCheckStatus.SKIPPED
    assert checks["cross_database.external_references"] is DrillCheckStatus.SKIPPED
    assert report.cleanup_errors == ()


def test_full_pair_drill_passes(source_db: SourceDb, tmp_path: Path) -> None:
    """The drill restores BOTH databases and fully verifies the pair."""
    base_url, _database, api_key = _tryton_is_usable()
    tryton_url = _tryton_database_url()
    # The Tryton scratch DBs are created on the SAME postgres server
    # (CI runs one service for both databases), so the HesabYar admin
    # URL is the Tryton admin URL too — the tryton role itself has no
    # CREATEDB privilege.
    settings = _backup_settings(
        tmp_path, tryton_url=tryton_url, tryton_admin_url=source_db.admin_url
    )
    dumper = PgDumpRestoreRunner()
    backup = build_backup_service(settings, dumper)
    run = backup.run_all(source_db.url, tryton_url)
    assert run.ok, run.failures
    drill = build_drill_service(
        settings,
        dumper,
        _engine_factory(base_url, api_key),
        hesabyar_database_url=source_db.url,
    )
    report = drill.run(run.manifests)
    assert report.passed, [f"{c.name}: {c.detail}" for c in report.failed_checks()]
    checks = {c.name: c.status for c in report.checks}
    assert checks["hesabyar.migrations"] is DrillCheckStatus.PASS
    assert checks["hesabyar.orphaned_documents"] is DrillCheckStatus.PASS
    assert checks["hesabyar.settlement_tranches"] is DrillCheckStatus.PASS
    assert checks["tryton.trial_balance"] is DrillCheckStatus.PASS
    assert checks["cross_database.external_references"] is DrillCheckStatus.PASS
    assert report.fully_verified
    assert report.cleanup_errors == ()
