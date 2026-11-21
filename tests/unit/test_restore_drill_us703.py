"""Unit tests for the restore drill (US-703).

The five reconciliation checks run against a SQLite database built
from the real ORM metadata (``Base.metadata.create_all`` — the
models' JSONB columns degrade to plain JSON on SQLite via a Variant
alias in models.py, CHECK constraints are standard SQL) plus a fake
engine port, so the check LOGIC is pinned here without a live server. The
full drill against real pg_dump/pg_restore + live Tryton is
tests/infrastructure/test_restore_drill_us703.py.

Also covered: drill orchestration with fake tools (preflight
failure, missing/tampered backups, scratch cleanup).
"""

from __future__ import annotations

import datetime
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from hesabyar.domain.accounting.refs import CompanyRef, FiscalYearRef
from hesabyar.domain.accounting.value_objects import (
    CompanyView,
    FiscalYearView,
    TrialBalanceView,
)
from hesabyar.domain.backup.errors import BackupToolError, ObjectStoreError
from hesabyar.domain.backup.models import (
    BackupManifest,
    BackupTarget,
    DrillCheckStatus,
)
from hesabyar.domain.backup.ports import DumpResult, PgToolsInfo
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.infrastructure.auth.credential_vault import generate_encryption_key
from hesabyar.infrastructure.operations.backup_cipher import FernetBackupCipher
from hesabyar.infrastructure.operations.backup_service import BackupService
from hesabyar.infrastructure.operations.restore_drill import (
    CHECK_EXTERNAL_REFS,
    CHECK_MIGRATIONS,
    CHECK_ORPHANS,
    CHECK_TRANCHES,
    CHECK_TRIAL_BALANCE,
    RestoreDrillService,
    check_alembic,
    check_external_references,
    check_orphaned_documents,
    check_settlement_tranches,
    check_trial_balance,
)
from hesabyar.infrastructure.persistence.postgres.models import (
    Base,
    ExternalIdMapModel,
    GatewayContractModel,
    GatewayPaymentModel,
    PaymentChannelModel,
    SettlementTrancheModel,
    TenantModel,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "migrations"
NOW = datetime.datetime(2026, 11, 21, 22, 30, tzinfo=datetime.UTC)
INSTALLATION_ID = "00000000-0000-0000-0000-000000000001"


@pytest.fixture()
def engine() -> Iterator[Engine]:
    """An empty HesabYar schema on SQLite (FKs unenforced — see below)."""
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine) as sess:
        yield sess


def _tenant(session: Session) -> TenantModel:
    tenant = TenantModel(id=str(uuid4()), slug="drill-test", name="Drill Test")
    session.add(tenant)
    session.flush()
    return tenant


def _channel_and_contract(session: Session) -> tuple[PaymentChannelModel, GatewayContractModel]:
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
    return channel, contract


def _payment(
    session: Session,
    channel: PaymentChannelModel,
    contract: GatewayContractModel,
    *,
    gross_irr: int = 100_000,
    fee_irr: int = 2_000,
    fee_vat_irr: int = 200,
    net_irr: int = 97_800,
) -> GatewayPaymentModel:
    payment = GatewayPaymentModel(
        installation_id=INSTALLATION_ID,
        payment_channel_id=channel.id,
        gateway_transaction_id=f"txn-{uuid4().hex[:8]}",
        gross_irr=gross_irr,
        fee_irr=fee_irr,
        fee_vat_irr=fee_vat_irr,
        net_irr=net_irr,
        contract_version_id=contract.id,
        fee_rate_snapshot=Decimal("2"),
        sales_period_jalali="1405-08",
        gateway_confirmed_at=datetime.datetime(2026, 11, 20, 10, 0, tzinfo=datetime.UTC),
        source="store",
    )
    session.add(payment)
    session.flush()
    return payment


def _tranche(
    session: Session,
    payment: GatewayPaymentModel,
    *,
    index: int,
    count: int,
    gross_irr: int,
    net_irr: int,
    confirmed_net_irr: int = 0,
    received_net_irr: int = 0,
) -> SettlementTrancheModel:
    tranche = SettlementTrancheModel(
        installation_id=INSTALLATION_ID,
        gateway_payment_id=payment.id,
        tranche_index=index,
        tranche_count=count,
        sales_period_jalali="1405-08",
        expected_window_start=datetime.date(2026, 11, 20),
        expected_date=datetime.date(2026, 12, 20),
        gross_irr=gross_irr,
        fee_irr=0,
        fee_vat_irr=0,
        net_irr=net_irr,
        confirmed_net_irr=confirmed_net_irr,
        received_net_irr=received_net_irr,
    )
    session.add(tranche)
    session.flush()
    return tranche


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
# Alembic check
# ---------------------------------------------------------------------------


def test_alembic_check_passes_at_head(engine: Engine) -> None:
    _set_alembic_version(engine, _head_revision())
    result = check_alembic(engine, MIGRATIONS_DIR)
    assert result.status is DrillCheckStatus.PASS
    assert result.name == CHECK_MIGRATIONS


def test_alembic_check_fails_when_behind_head(engine: Engine) -> None:
    _set_alembic_version(engine, "0001")
    result = check_alembic(engine, MIGRATIONS_DIR)
    assert result.status is DrillCheckStatus.FAIL
    assert "head is" in result.detail


def test_alembic_check_fails_without_a_version_table(engine: Engine) -> None:
    result = check_alembic(engine, MIGRATIONS_DIR)
    assert result.status is DrillCheckStatus.FAIL


def test_alembic_check_fails_without_a_migrations_dir(engine: Engine) -> None:
    result = check_alembic(engine, REPO_ROOT / "no-such-migrations")
    assert result.status is DrillCheckStatus.FAIL
    assert "not found" in result.detail


# ---------------------------------------------------------------------------
# Orphan sweep
# ---------------------------------------------------------------------------


def test_orphan_check_passes_on_an_empty_database(engine: Engine) -> None:
    result = check_orphaned_documents(engine)
    assert result.status is DrillCheckStatus.PASS
    assert result.name == CHECK_ORPHANS


def test_orphan_check_passes_with_consistent_rows(engine: Engine, session: Session) -> None:
    channel, contract = _channel_and_contract(session)
    payment = _payment(session, channel, contract)
    _tranche(session, payment, index=1, count=1, gross_irr=100_000, net_irr=97_800)
    session.commit()
    assert check_orphaned_documents(engine).status is DrillCheckStatus.PASS


def test_orphan_check_fails_on_a_dangling_tranche(engine: Engine, session: Session) -> None:
    # SQLite does not enforce FKs by default, so a dangling row can be
    # inserted deliberately — exactly what a corrupt restore would hold.
    tranche = SettlementTrancheModel(
        installation_id=INSTALLATION_ID,
        gateway_payment_id="00000000-0000-0000-0000-000000000099",  # no such payment
        tranche_index=1,
        tranche_count=1,
        sales_period_jalali="1405-08",
        expected_window_start=datetime.date(2026, 11, 20),
        expected_date=datetime.date(2026, 12, 20),
        gross_irr=100_000,
        fee_irr=0,
        fee_vat_irr=0,
        net_irr=97_800,
    )
    session.add(tranche)
    session.commit()
    result = check_orphaned_documents(engine)
    assert result.status is DrillCheckStatus.FAIL
    assert "settlement_tranches.gateway_payment_id" in result.detail
    assert "1 orphaned row" in result.detail


# ---------------------------------------------------------------------------
# Settlement tranche tie-out
# ---------------------------------------------------------------------------


def test_tranche_check_passes_on_an_empty_database(engine: Engine) -> None:
    result = check_settlement_tranches(engine)
    assert result.status is DrillCheckStatus.PASS
    assert result.name == CHECK_TRANCHES


def test_tranche_check_passes_when_tranches_tie_out(engine: Engine, session: Session) -> None:
    channel, contract = _channel_and_contract(session)
    payment = _payment(session, channel, contract)
    _tranche(session, payment, index=1, count=2, gross_irr=50_000, net_irr=48_900,
             confirmed_net_irr=48_900, received_net_irr=48_900)
    _tranche(session, payment, index=2, count=2, gross_irr=50_000, net_irr=48_900)
    session.commit()
    result = check_settlement_tranches(engine)
    assert result.status is DrillCheckStatus.PASS
    assert "1 payment(s), 2 tranche(s)" in result.detail


def test_tranche_check_fails_when_nets_do_not_tie_out(engine: Engine, session: Session) -> None:
    channel, contract = _channel_and_contract(session)
    payment = _payment(session, channel, contract)
    _tranche(session, payment, index=1, count=1, gross_irr=100_000, net_irr=90_000)
    session.commit()
    result = check_settlement_tranches(engine)
    assert result.status is DrillCheckStatus.FAIL
    assert "Σ tranche net 90000 != payment net 97800" in result.detail


def test_tranche_check_fails_on_out_of_bounds_progress(engine: Engine, session: Session) -> None:
    channel, contract = _channel_and_contract(session)
    payment = _payment(session, channel, contract)
    _tranche(session, payment, index=1, count=1, gross_irr=100_000, net_irr=97_800,
             confirmed_net_irr=50_000, received_net_irr=60_000)  # received > confirmed
    session.commit()
    result = check_settlement_tranches(engine)
    assert result.status is DrillCheckStatus.FAIL
    assert "out of bounds" in result.detail


def test_tranche_check_fails_when_a_payment_has_no_tranches(engine: Engine, session: Session) -> None:
    channel, contract = _channel_and_contract(session)
    _payment(session, channel, contract)
    session.commit()
    result = check_settlement_tranches(engine)
    assert result.status is DrillCheckStatus.FAIL
    assert "no tranches" in result.detail


def test_tranche_check_fails_on_a_count_mismatch(engine: Engine, session: Session) -> None:
    channel, contract = _channel_and_contract(session)
    payment = _payment(session, channel, contract)
    _tranche(session, payment, index=1, count=2, gross_irr=100_000, net_irr=97_800)
    session.commit()
    result = check_settlement_tranches(engine)
    assert result.status is DrillCheckStatus.FAIL
    assert "stored tranche_count is 2" in result.detail


# ---------------------------------------------------------------------------
# Trial balance (fake engine)
# ---------------------------------------------------------------------------


class FakeEngine:
    """A minimal AccountingEnginePort stand-in for the drill checks."""

    def __init__(
        self,
        *,
        companies: list[CompanyView] | None = None,
        fiscal_years: dict[int, list[FiscalYearView]] | None = None,
        trial_balances: dict[int, TrialBalanceView] | None = None,
        invoices: set[int] | None = None,
        moves: set[int] | None = None,
        payments: set[int] | None = None,
        explode_on: str | None = None,
    ) -> None:
        self._companies = companies if companies is not None else []
        self._fiscal_years = fiscal_years or {}
        self._trial_balances = trial_balances or {}
        self._invoices = invoices or set()
        self._moves = moves or set()
        self._payments = payments or set()
        self._explode_on = explode_on

    def _maybe_explode(self, name: str) -> None:
        if self._explode_on == name:
            raise RuntimeError(f"engine exploded in {name}")

    def list_companies(self) -> list[CompanyView]:
        self._maybe_explode("list_companies")
        return self._companies

    def list_fiscal_years(self, company: CompanyRef) -> list[FiscalYearView]:
        self._maybe_explode("list_fiscal_years")
        return self._fiscal_years.get(company.engine_id, [])

    def get_trial_balance(
        self, company: CompanyRef, date_from: FiscalDate, date_to: FiscalDate
    ) -> TrialBalanceView:
        self._maybe_explode("get_trial_balance")
        return self._trial_balances[company.engine_id]

    def get_invoice(self, invoice: Any) -> Any:
        self._maybe_explode("get_invoice")
        return object() if invoice.engine_id in self._invoices else None

    def get_journal(self, journal: Any) -> Any:
        self._maybe_explode("get_journal")
        return object() if journal.engine_id in self._moves else None

    def get_payment(self, payment: Any) -> Any:
        self._maybe_explode("get_payment")
        return object() if payment.engine_id in self._payments else None


def _company(company_id: int = 1) -> CompanyView:
    return CompanyView(ref=CompanyRef(company_id), name="Test Co", currency_code="IRR")


def _fiscal_year(company_id: int = 1) -> FiscalYearView:
    return FiscalYearView(
        ref=FiscalYearRef(1),
        company=CompanyRef(company_id),
        name="FY 1405",
        start_date=FiscalDate.from_gregorian(datetime.date(2026, 3, 21)),
        end_date=FiscalDate.from_gregorian(datetime.date(2027, 3, 20)),
        state="open",
    )


def _trial_balance(company_id: int = 1, *, debit: int, credit: int) -> TrialBalanceView:
    return TrialBalanceView(
        company=CompanyRef(company_id),
        date_from=FiscalDate.from_gregorian(datetime.date(2026, 3, 21)),
        date_to=FiscalDate.from_gregorian(datetime.date(2027, 3, 20)),
        total_debit=MoneyIRR(debit),
        total_credit=MoneyIRR(credit),
        move_count=3,
    )


def test_trial_balance_check_passes_when_balanced() -> None:
    engine = FakeEngine(
        companies=[_company()],
        fiscal_years={1: [_fiscal_year()]},
        trial_balances={1: _trial_balance(debit=1_000_000, credit=1_000_000)},
    )
    result = check_trial_balance(engine)  # type: ignore[arg-type]
    assert result.status is DrillCheckStatus.PASS
    assert result.name == CHECK_TRIAL_BALANCE
    assert "debit == credit == 1000000 IRR" in result.detail


def test_trial_balance_check_fails_when_unbalanced() -> None:
    engine = FakeEngine(
        companies=[_company()],
        fiscal_years={1: [_fiscal_year()]},
        trial_balances={1: _trial_balance(debit=1_000_000, credit=999_999)},
    )
    result = check_trial_balance(engine)  # type: ignore[arg-type]
    assert result.status is DrillCheckStatus.FAIL
    assert "does NOT balance" in result.detail
    assert "debit=1000000 IRR" in result.detail
    assert "credit=999999 IRR" in result.detail


def test_trial_balance_check_skips_without_companies() -> None:
    result = check_trial_balance(FakeEngine())  # type: ignore[arg-type]
    assert result.status is DrillCheckStatus.SKIPPED
    assert "No companies" in result.detail


def test_trial_balance_check_skips_a_company_without_fiscal_years() -> None:
    engine = FakeEngine(companies=[_company()], fiscal_years={})
    result = check_trial_balance(engine)  # type: ignore[arg-type]
    assert result.status is DrillCheckStatus.SKIPPED
    assert "No company has fiscal years" in result.detail


def test_trial_balance_check_fails_when_the_engine_errors() -> None:
    engine = FakeEngine(explode_on="list_companies")
    result = check_trial_balance(engine)  # type: ignore[arg-type]
    assert result.status is DrillCheckStatus.FAIL
    assert "Could not list companies" in result.detail


# ---------------------------------------------------------------------------
# Cross-database external references
# ---------------------------------------------------------------------------


def _map_row(
    session: Session,
    tenant: TenantModel,
    *,
    target_type: str,
    target_id: str,
    source_id: str,
) -> None:
    session.add(
        ExternalIdMapModel(
            tenant_id=tenant.id,
            source_system="hayat",
            external_entity_type="order",
            external_entity_id=source_id,
            target_engine="tryton",
            target_entity_type=target_type,
            target_external_id=target_id,
        )
    )
    session.flush()


def test_external_refs_pass_with_no_rows(engine: Engine) -> None:
    result = check_external_references(engine, FakeEngine(), max_refs=200)  # type: ignore[arg-type]
    assert result.status is DrillCheckStatus.PASS
    assert result.name == CHECK_EXTERNAL_REFS


def test_external_refs_pass_when_everything_resolves(
    engine: Engine, session: Session
) -> None:
    tenant = _tenant(session)
    _map_row(session, tenant, target_type="account.invoice", target_id="42", source_id="o-1")
    _map_row(session, tenant, target_type="account.move", target_id="7", source_id="o-2")
    _map_row(session, tenant, target_type="account.payment", target_id="9", source_id="o-3")
    session.commit()
    fake = FakeEngine(invoices={42}, moves={7}, payments={9})
    result = check_external_references(engine, fake, max_refs=200)  # type: ignore[arg-type]
    assert result.status is DrillCheckStatus.PASS
    assert "3 reference(s) resolve" in result.detail


def test_external_refs_fail_on_an_unresolvable_row(engine: Engine, session: Session) -> None:
    tenant = _tenant(session)
    _map_row(session, tenant, target_type="account.invoice", target_id="42", source_id="o-1")
    _map_row(session, tenant, target_type="account.invoice", target_id="43", source_id="o-2")
    session.commit()
    fake = FakeEngine(invoices={42})
    result = check_external_references(engine, fake, max_refs=200)  # type: ignore[arg-type]
    assert result.status is DrillCheckStatus.FAIL
    assert "account.invoice:43" in result.detail


def test_external_refs_fail_on_a_non_integer_target_id(engine: Engine, session: Session) -> None:
    tenant = _tenant(session)
    _map_row(session, tenant, target_type="account.invoice", target_id="not-an-int", source_id="o-1")
    session.commit()
    result = check_external_references(engine, FakeEngine(), max_refs=200)  # type: ignore[arg-type]
    assert result.status is DrillCheckStatus.FAIL
    assert "not an integer id" in result.detail


def test_external_refs_report_unknown_models_as_unchecked(
    engine: Engine, session: Session
) -> None:
    tenant = _tenant(session)
    _map_row(session, tenant, target_type="account.invoice", target_id="42", source_id="o-1")
    _map_row(session, tenant, target_type="stock.shipment", target_id="5", source_id="o-2")
    session.commit()
    fake = FakeEngine(invoices={42})
    result = check_external_references(engine, fake, max_refs=200)  # type: ignore[arg-type]
    assert result.status is DrillCheckStatus.PASS
    assert "1 unchecked" in result.detail


def test_external_refs_respect_the_cap(engine: Engine, session: Session) -> None:
    tenant = _tenant(session)
    for i in range(3):
        _map_row(session, tenant, target_type="account.invoice", target_id=str(40 + i), source_id=f"o-{i}")
    session.commit()
    fake = FakeEngine(invoices={40, 41, 42})
    result = check_external_references(engine, fake, max_refs=1)  # type: ignore[arg-type]
    assert result.status is DrillCheckStatus.PASS
    assert "1 reference(s) resolve" in result.detail
    assert "2 unchecked" in result.detail


def test_external_refs_fail_when_the_engine_errors(engine: Engine, session: Session) -> None:
    tenant = _tenant(session)
    _map_row(session, tenant, target_type="account.invoice", target_id="42", source_id="o-1")
    session.commit()
    fake = FakeEngine(explode_on="get_invoice")
    result = check_external_references(engine, fake, max_refs=200)  # type: ignore[arg-type]
    assert result.status is DrillCheckStatus.FAIL
    assert "Could not resolve account.invoice:42" in result.detail


# ---------------------------------------------------------------------------
# Drill orchestration (fake tools, no real database)
# ---------------------------------------------------------------------------


class _FakeStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_object(self, key: str, data: bytes, *, content_type: str) -> None:
        self.objects[key] = data

    def get_object(self, key: str) -> bytes:
        try:
            return self.objects[key]
        except KeyError as exc:
            raise ObjectStoreError(f"missing {key}", not_found=True) from exc

    def list_objects(self, prefix: str) -> list[str]:
        return sorted(k for k in self.objects if k.startswith(prefix))

    def delete_object(self, key: str) -> None:
        self.objects.pop(key, None)


class _FakeDumper:
    """Records create/drop/restore; never touches a real database."""

    def __init__(self, *, fail_tools: bool = False) -> None:
        self.created: list[str] = []
        self.dropped: list[str] = []
        self.restored: list[str] = []
        self.restore_no_owner: dict[str, bool] = {}
        self.fail_tools = fail_tools

    def check_tools(self) -> PgToolsInfo:
        if self.fail_tools:
            raise BackupToolError("pg_dump was not found on PATH")
        return PgToolsInfo(
            pg_dump_path="pg_dump",
            pg_dump_version="16.4",
            pg_restore_path="pg_restore",
            pg_restore_version="16.4",
        )

    def dump(self, database_url: str) -> DumpResult:
        database = database_url.rsplit("/", 1)[-1]
        return DumpResult(data=f"dump of {database}".encode(), pg_dump_version="16.4", database=database)

    def restore(self, database_url: str, data: bytes, *, no_owner: bool) -> None:
        database = database_url.rsplit("/", 1)[-1]
        self.restored.append(database)
        self.restore_no_owner[database] = no_owner

    def create_database(self, admin_url: str, name: str) -> None:
        self.created.append(name)

    def drop_database(self, admin_url: str, name: str) -> None:
        self.dropped.append(name)


def _drill_service(
    store: _FakeStore, dumper: _FakeDumper, *, engine_factory: Any = None
) -> RestoreDrillService:
    return RestoreDrillService(
        store=store,  # type: ignore[arg-type]
        cipher=FernetBackupCipher(generate_encryption_key()),
        dumper=dumper,  # type: ignore[arg-type]
        # Port 1 refuses immediately: the HesabYar checks fail fast
        # (no real database) instead of hanging on a connect timeout.
        hesabyar_admin_url="postgresql://u:p@127.0.0.1:1/postgres",
        tryton_admin_url="postgresql://u:p@127.0.0.1:1/postgres",
        engine_factory=engine_factory,
        clock=lambda: NOW,
    )


def _seed_with_cipher(
    store: _FakeStore, dumper: _FakeDumper, cipher: FernetBackupCipher
) -> dict[BackupTarget, BackupManifest]:
    backup = BackupService(
        store=store,  # type: ignore[arg-type]
        cipher=cipher,
        dumper=dumper,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )
    return {
        BackupTarget.HESABYAR_DB: backup.run_backup(
            BackupTarget.HESABYAR_DB, "postgresql://u:p@h/hesabyar_test", now=NOW
        ),
        BackupTarget.TRYTON_DB: backup.run_backup(
            BackupTarget.TRYTON_DB, "postgresql://u:p@h/hesabyar_tryton_test", now=NOW
        ),
    }


def test_drill_fails_fast_when_the_tools_are_missing() -> None:
    store = _FakeStore()
    dumper = _FakeDumper(fail_tools=True)
    service = _drill_service(store, dumper)
    report = service.run({})
    assert report.passed is False
    assert [c.name for c in report.failed_checks()] == ["preflight.tools"]
    assert dumper.created == []  # nothing was attempted


def test_drill_fails_on_a_missing_manifest() -> None:
    store = _FakeStore()
    dumper = _FakeDumper()
    service = _drill_service(store, dumper)
    report = service.run({})
    assert report.passed is False
    failed = {c.name for c in report.failed_checks()}
    assert "hesabyar.manifest_checksum" in failed
    assert "tryton.manifest_checksum" in failed
    assert dumper.created == []


def test_drill_fails_on_a_tampered_backup() -> None:
    store = _FakeStore()
    dumper = _FakeDumper()
    cipher = FernetBackupCipher(generate_encryption_key())
    manifests = _seed_with_cipher(store, dumper, cipher)
    manifest = manifests[BackupTarget.HESABYAR_DB]
    tampered = bytearray(store.objects[manifest.object_key])
    tampered[0] ^= 0xFF
    store.objects[manifest.object_key] = bytes(tampered)
    service = RestoreDrillService(
        store=store,  # type: ignore[arg-type]
        cipher=cipher,
        dumper=dumper,  # type: ignore[arg-type]
        hesabyar_admin_url="postgresql://u:p@127.0.0.1:1/postgres",
        tryton_admin_url="postgresql://u:p@127.0.0.1:1/postgres",
        clock=lambda: NOW,
    )
    report = service.run(manifests)
    assert report.passed is False
    checks = {c.name: c.status for c in report.checks}
    assert checks["hesabyar.manifest_checksum"] is DrillCheckStatus.FAIL
    assert checks["tryton.manifest_checksum"] is DrillCheckStatus.PASS
    # Only the Tryton scratch DB was created (HesabYar never got that far).
    assert len(dumper.created) == 1
    assert dumper.created[0].startswith("tryton_drill_")
    assert dumper.dropped == dumper.created  # cleaned up


def test_drill_drops_scratch_databases_unless_kept() -> None:
    store = _FakeStore()
    dumper = _FakeDumper()
    cipher = FernetBackupCipher(generate_encryption_key())
    manifests = _seed_with_cipher(store, dumper, cipher)
    service = RestoreDrillService(
        store=store,  # type: ignore[arg-type]
        cipher=cipher,
        dumper=dumper,  # type: ignore[arg-type]
        hesabyar_admin_url="postgresql://u:p@127.0.0.1:1/postgres",
        tryton_admin_url="postgresql://u:p@127.0.0.1:1/postgres",
        clock=lambda: NOW,
    )
    report = service.run(manifests)
    checks = {c.name: c.status for c in report.checks}
    # The restores "succeeded" (fake dumper); the HesabYar checks fail
    # because there is no real scratch database behind the fake.
    assert checks["hesabyar.restore"] is DrillCheckStatus.PASS
    assert checks["tryton.restore"] is DrillCheckStatus.PASS
    # Ownership differs per target: HesabYar is read as the admin
    # superuser (--no-owner is fine), Tryton is read by trytond as
    # the tryton role (owners + ACLs must be kept).
    hesabyar_scratch = next(n for n in dumper.restored if n.startswith("hesabyar_drill_"))
    tryton_scratch = next(n for n in dumper.restored if n.startswith("tryton_drill_"))
    assert dumper.restore_no_owner[hesabyar_scratch] is True
    assert dumper.restore_no_owner[tryton_scratch] is False
    assert checks["hesabyar.migrations"] is DrillCheckStatus.FAIL
    assert checks["tryton.trial_balance"] is DrillCheckStatus.SKIPPED  # no engine factory
    assert sorted(dumper.dropped) == sorted(dumper.created)
    assert report.cleanup_errors == ()

    dumper2 = _FakeDumper()
    service2 = RestoreDrillService(
        store=store,  # type: ignore[arg-type]
        cipher=cipher,
        dumper=dumper2,  # type: ignore[arg-type]
        hesabyar_admin_url="postgresql://u:p@127.0.0.1:1/postgres",
        tryton_admin_url="postgresql://u:p@127.0.0.1:1/postgres",
        clock=lambda: NOW,
    )
    report2 = service2.run(manifests, keep_scratch=True)
    assert report2.keep_scratch is True
    assert dumper2.created != []
    assert dumper2.dropped == []


def test_drill_with_no_targets_reports_only_skips() -> None:
    store = _FakeStore()
    dumper = _FakeDumper()
    service = _drill_service(store, dumper)
    report = service.run({}, targets=())
    assert report.passed is True  # nothing failed
    assert report.fully_verified is False  # ...but nothing was verified either
    assert all(c.status is DrillCheckStatus.SKIPPED for c in report.checks)
    assert dumper.created == []


def test_drill_runs_the_tryton_checks_through_the_engine_factory() -> None:
    store = _FakeStore()
    dumper = _FakeDumper()
    cipher = FernetBackupCipher(generate_encryption_key())
    manifests = _seed_with_cipher(store, dumper, cipher)
    seen_databases: list[str] = []
    fake_engine = FakeEngine(
        companies=[_company()],
        fiscal_years={1: [_fiscal_year()]},
        trial_balances={1: _trial_balance(debit=5, credit=5)},
    )

    @contextmanager
    def factory(database: str) -> Iterator[FakeEngine]:
        seen_databases.append(database)
        yield fake_engine

    service = RestoreDrillService(
        store=store,  # type: ignore[arg-type]
        cipher=cipher,
        dumper=dumper,  # type: ignore[arg-type]
        hesabyar_admin_url="postgresql://u:p@127.0.0.1:1/postgres",
        tryton_admin_url="postgresql://u:p@127.0.0.1:1/postgres",
        engine_factory=factory,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )
    report = service.run(manifests)
    checks = {c.name: c.status for c in report.checks}
    # The trial-balance check ran against the SCRATCH database name.
    assert checks["tryton.trial_balance"] is DrillCheckStatus.PASS
    assert len(seen_databases) == 1  # one engine session for both Tryton checks
    assert seen_databases[0].startswith("tryton_drill_")
    # The cross-database check RAN (create_engine is lazy, so the
    # HesabYar engine object exists) and failed: there is no real
    # scratch database behind the fake dumper, so the connect fails.
    assert checks["cross_database.external_references"] is DrillCheckStatus.FAIL
