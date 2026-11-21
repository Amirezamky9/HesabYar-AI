"""Restore drill: prove a backup pair restores and reconciles (US-703).

Spec §15.4: "a backup is not considered valid until it has been
restored and checked." The drill downloads the newest backup of each
database, verifies checksums, decrypts, restores both into SCRATCH
databases (never the live ones), and runs the reconciliation checks:

- ``<target>.manifest_checksum`` — the stored bytes match the
  manifest, and the decrypted dump matches its plaintext checksum.
- ``<target>.restore`` — pg_restore into the scratch database succeeds.
- ``hesabyar.migrations`` — the restored HesabYar DB has a SINGLE
  Alembic head and its current revision IS that head.
- ``hesabyar.orphaned_documents`` — a generic foreign-key sweep over
  every HesabYar table: no row references a missing row.
- ``hesabyar.settlement_tranches`` — every payment's tranches tie
  out (Σ tranche net == payment net, Σ gross == payment gross,
  per-tranche progress bounds hold).
- ``cross_database.external_references`` — every external-id-map row
  pointing at Tryton resolves in the RESTORED Tryton database (via
  the REST adapter, never SQL against Tryton's tables).
- ``tryton.trial_balance`` — every company's trial balance over its
  fiscal years balances (posted moves only, via the REST adapter).

The Tryton checks run against the SAME trytond that serves
production, pointed at the scratch database by name (the adapter's
``database`` setting) — no second trytond is needed, and the
restored database already contains the REST application key, so the
same API key works. One wrinkle: trytond CACHES its database list
(``Database.list``, TTL = the ``session.timeout`` setting, default
300s), so a scratch database created after trytond started is
invisible to it — every REST call 404s — until the cache expires.
The drill therefore WAITS for the scratch database to become
visible (bounded, default 360s) before running the Tryton checks;
if it never becomes visible (an operator raised ``session.timeout``
far above the default), the Tryton checks are SKIPPED with that
reason, never silently passed. When no engine factory is wired,
the Tryton checks are SKIPPED (reported, never silent) — the drill
still verifies the HesabYar side.

Scratch databases are dropped in a ``finally`` (``--keep-scratch``
keeps them for debugging). Their names always contain ``_drill_``
so an operator can tell at a glance that they are throwaway.
"""

from __future__ import annotations

import datetime
import logging
import os
import secrets
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import and_, create_engine, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool

from hesabyar.domain.accounting.ports import AccountingEnginePort
from hesabyar.domain.accounting.refs import InvoiceRef, PaymentRef, PostedJournalRef
from hesabyar.domain.backup.errors import BackupError
from hesabyar.domain.backup.models import (
    BackupManifest,
    BackupTarget,
    DrillCheckResult,
    DrillCheckStatus,
    DrillReport,
)
from hesabyar.domain.backup.ports import (
    BackupCipherPort,
    DatabaseDumperPort,
    ObjectStorePort,
)
from hesabyar.domain.common.errors import HesabYarError
from hesabyar.infrastructure.operations.backup_service import BackupService
from hesabyar.infrastructure.operations.pg_tools import with_database_name
from hesabyar.infrastructure.persistence.postgres.models import (
    Base,
    ExternalIdMapModel,
    GatewayPaymentModel,
    SettlementTrancheModel,
)

logger = logging.getLogger(__name__)

#: Check names (stable strings — the report, the CLI and the tests use them).
CHECK_MANIFEST_CHECKSUM = "{target}.manifest_checksum"
CHECK_RESTORE = "{target}.restore"
CHECK_MIGRATIONS = "hesabyar.migrations"
CHECK_ORPHANS = "hesabyar.orphaned_documents"
CHECK_TRANCHES = "hesabyar.settlement_tranches"
CHECK_EXTERNAL_REFS = "cross_database.external_references"
CHECK_TRIAL_BALANCE = "tryton.trial_balance"

#: How many orphan details one check result carries (the rest is a count).
_MAX_ORPHAN_DETAILS = 10

#: External-id-map target types the drill can resolve through the
#: engine port (anything else is reported as unchecked, not failed).
_READABLE_TARGET_TYPES = ("account.invoice", "account.move", "account.payment")

#: The engine error code for "trytond does not serve this database"
#: (HTTP 404 — the adapter's ENGINE_NOT_FOUND taxonomy code, see
#: docs/adapter.md). The drill matches on the domain error's code,
#: not on the Tryton-specific error type, so the wait stays
#: engine-agnostic.
_ENGINE_NOT_FOUND_CODE = "ENGINE_NOT_FOUND"

#: Default bound for the scratch-visibility wait: trytond's
#: database-list cache TTL is its session.timeout (default 300s), so
#: 360s covers the worst case (cache refreshed a moment before the
#: scratch DB was created) plus one poll interval.
DEFAULT_SCRATCH_VISIBILITY_TIMEOUT_SECONDS = 360.0
DEFAULT_SCRATCH_VISIBILITY_POLL_SECONDS = 20.0


def _wait_for_scratch_database(
    engine: AccountingEnginePort,
    *,
    timeout_seconds: float,
    poll_seconds: float,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> bool:
    """True once trytond serves the scratch database (list_companies answers).

    trytond caches its database list (``Database.list`` in trytond
    8.2.3, TTL = the ``session.timeout`` setting, default 300s), so
    a database created after trytond started is invisible until the
    cache expires — every call 404s until then (CI run #32). Only a
    not-found error is retried: any other failure (auth, transport,
    a 500) raises immediately, because waiting cannot fix it.
    """
    deadline = monotonic() + timeout_seconds
    while True:
        try:
            engine.list_companies()
            return True
        except HesabYarError as exc:
            if exc.code != _ENGINE_NOT_FOUND_CODE:
                raise
        if monotonic() >= deadline:
            return False
        sleep(poll_seconds)


def _pass(name: str, detail: str = "") -> DrillCheckResult:
    return DrillCheckResult(name=name, status=DrillCheckStatus.PASS, detail=detail)


def _fail(name: str, detail: str) -> DrillCheckResult:
    return DrillCheckResult(name=name, status=DrillCheckStatus.FAIL, detail=detail)


def _skip(name: str, detail: str) -> DrillCheckResult:
    return DrillCheckResult(name=name, status=DrillCheckStatus.SKIPPED, detail=detail)


def _sqlalchemy_url(database_url: str) -> str:
    """A plain ``postgresql://`` DSN as a SQLAlchemy psycopg URL."""
    if database_url.startswith("postgresql://"):
        return "postgresql+psycopg://" + database_url[len("postgresql://") :]
    return database_url


def resolve_migrations_dir() -> Path:
    """The Alembic migrations directory (same env override as the REST service)."""
    override = os.environ.get("HESABYAR_MIGRATIONS_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[4] / "migrations"


# ---------------------------------------------------------------------------
# Checks (module-level so the unit tests can drive them directly)
# ---------------------------------------------------------------------------


def check_alembic(engine: Engine, migrations_dir: Path) -> DrillCheckResult:
    """Single Alembic head + the restored DB is AT that head."""
    if not (migrations_dir / "env.py").is_file():
        return _fail(
            CHECK_MIGRATIONS, f"Migrations directory not found: {migrations_dir}"
        )
    try:
        config = Config()
        config.set_main_option("script_location", str(migrations_dir))
        heads = ScriptDirectory.from_config(config).get_heads()
    except Exception as exc:
        return _fail(CHECK_MIGRATIONS, f"Could not read the Alembic script directory: {exc}")
    if len(heads) != 1:
        return _fail(
            CHECK_MIGRATIONS,
            f"Expected a single Alembic head, found {len(heads)}: {sorted(heads)}",
        )
    head = heads[0]
    try:
        with engine.connect() as connection:
            current = MigrationContext.configure(connection).get_current_revision()
    except Exception as exc:
        return _fail(CHECK_MIGRATIONS, f"Could not read alembic_version: {exc}")
    if current is None:
        return _fail(
            CHECK_MIGRATIONS,
            "The restored database has no alembic_version row (not a HesabYar database?).",
        )
    if current != head:
        return _fail(
            CHECK_MIGRATIONS,
            f"Restored database is at revision {current}, head is {head} — "
            "run the migrations after restoring (see docs/backup-restore.md).",
        )
    return _pass(CHECK_MIGRATIONS, f"single head {head}, restored database is at head")


def check_orphaned_documents(engine: Engine) -> DrillCheckResult:
    """Generic foreign-key sweep: no HesabYar row references a missing row.

    Every FK in the ORM metadata is anti-joined (MATCH SIMPLE: a row
    needs a parent only when ALL of its FK columns are non-null).
    PostgreSQL enforces FKs, so a consistent restore has zero
    orphans — a non-zero count means the restore was partial or the
    dump was corrupt, and the drill must fail.
    """
    orphans: dict[str, int] = {}
    total = 0
    with engine.connect() as connection:
        for table in Base.metadata.tables.values():
            for fk in table.foreign_key_constraints:
                parent = fk.referred_table
                alias = parent.alias() if parent is table else parent
                non_null = [
                    table.c[element.parent.name].is_not(None) for element in fk.elements
                ]
                join_on = and_(
                    *[
                        table.c[element.parent.name] == alias.c[element.column.name]
                        for element in fk.elements
                    ]
                )
                # The parent PK names come from the real table (an
                # Alias has no primary-key constraint of its own);
                # the NULL test runs against the alias so a
                # self-referencing FK still anti-joins correctly.
                parent_pk_names = [c.name for c in parent.primary_key.columns]
                if not parent_pk_names:
                    continue
                query = (
                    select(func.count())
                    .select_from(table.outerjoin(alias, join_on))
                    .where(and_(*non_null, alias.c[parent_pk_names[0]].is_(None)))
                )
                count = connection.execute(query).scalar_one()
                if count:
                    label = (
                        f"{table.name}.{','.join(e.parent.name for e in fk.elements)}"
                        f" -> {parent.name}"
                    )
                    orphans[label] = int(count)
                    total += int(count)
    if total:
        details = "; ".join(
            f"{label}: {count}"
            for label, count in sorted(orphans.items())[:_MAX_ORPHAN_DETAILS]
        )
        return _fail(
            CHECK_ORPHANS,
            f"{total} orphaned row(s) in the restored database ({details})"
            + ("; …" if len(orphans) > _MAX_ORPHAN_DETAILS else ""),
        )
    return _pass(CHECK_ORPHANS, "no orphaned rows (every foreign key resolves)")


def check_settlement_tranches(engine: Engine) -> DrillCheckResult:
    """Settlement tranches tie out against their payments (all IRR ints).

    Per payment: Σ tranche net == payment net, Σ tranche gross ==
    payment gross, and the stored tranche count matches the rows.
    Per tranche: 0 <= received <= confirmed <= net (partial progress
    can never exceed the tranche). No floats anywhere; no
    Rial/Toman conversion — these are stored Rial integers.
    """
    mismatches: list[str] = []
    payments_checked = 0
    tranches_checked = 0
    with engine.connect() as connection:
        payments = connection.execute(
            select(
                GatewayPaymentModel.id,
                GatewayPaymentModel.gross_irr,
                GatewayPaymentModel.net_irr,
            )
        ).all()
        for payment_id, gross_irr, net_irr in payments:
            payments_checked += 1
            rows = connection.execute(
                select(
                    SettlementTrancheModel.gross_irr,
                    SettlementTrancheModel.net_irr,
                    SettlementTrancheModel.confirmed_net_irr,
                    SettlementTrancheModel.received_net_irr,
                    SettlementTrancheModel.tranche_count,
                ).where(SettlementTrancheModel.gateway_payment_id == payment_id)
            ).all()
            tranches_checked += len(rows)
            if not rows:
                mismatches.append(f"payment {payment_id}: no tranches")
                continue
            sum_gross = sum(row[0] for row in rows)
            sum_net = sum(row[1] for row in rows)
            if sum_gross != gross_irr:
                mismatches.append(
                    f"payment {payment_id}: Σ tranche gross {sum_gross} != "
                    f"payment gross {gross_irr}"
                )
            if sum_net != net_irr:
                mismatches.append(
                    f"payment {payment_id}: Σ tranche net {sum_net} != "
                    f"payment net {net_irr}"
                )
            expected_count = rows[0][4]
            if len(rows) != expected_count:
                mismatches.append(
                    f"payment {payment_id}: {len(rows)} tranche rows, "
                    f"stored tranche_count is {expected_count}"
                )
            for gross, net, confirmed, received, _ in rows:
                if not (0 <= received <= confirmed <= net):
                    mismatches.append(
                        f"payment {payment_id}: tranche progress out of bounds "
                        f"(net={net} confirmed={confirmed} received={received})"
                    )
                if gross < 0 or net < 0:
                    mismatches.append(
                        f"payment {payment_id}: negative tranche amount "
                        f"(gross={gross} net={net})"
                    )
    if mismatches:
        return _fail(
            CHECK_TRANCHES,
            f"{len(mismatches)} tie-out problem(s) "
            f"({payments_checked} payments, {tranches_checked} tranches): "
            + "; ".join(mismatches[:_MAX_ORPHAN_DETAILS]),
        )
    return _pass(
        CHECK_TRANCHES,
        f"{payments_checked} payment(s), {tranches_checked} tranche(s): "
        "Σ tranche net == payment net, Σ gross == payment gross, progress in bounds",
    )


def check_trial_balance(engine: AccountingEnginePort) -> DrillCheckResult:
    """Every company's trial balance balances (posted moves only).

    Read through the engine port — the trial balance aggregates
    posted ``account.move.line`` rows, so it is NOT affected by the
    account-balance descendant gotcha (Tryton's ``get_balance``
    includes descendants unless ``flat_balance`` is set; this read
    never touches account balances).
    """
    try:
        companies = engine.list_companies()
    except Exception as exc:
        return _fail(CHECK_TRIAL_BALANCE, f"Could not list companies: {exc}")
    if not companies:
        return _skip(
            CHECK_TRIAL_BALANCE,
            "No companies in the restored Tryton database — nothing to balance.",
        )
    details: list[str] = []
    skipped_companies: list[str] = []
    for company in companies:
        try:
            years = engine.list_fiscal_years(company.ref)
        except Exception as exc:
            return _fail(
                CHECK_TRIAL_BALANCE,
                f"Could not list fiscal years for {company.name}: {exc}",
            )
        if not years:
            skipped_companies.append(company.name)
            continue
        date_from = min(year.start_date for year in years)
        date_to = max(year.end_date for year in years)
        try:
            trial_balance = engine.get_trial_balance(company.ref, date_from, date_to)
        except Exception as exc:
            return _fail(
                CHECK_TRIAL_BALANCE,
                f"Could not read the trial balance for {company.name}: {exc}",
            )
        if not trial_balance.is_balanced:
            return _fail(
                CHECK_TRIAL_BALANCE,
                f"Trial balance does NOT balance for {company.name}: "
                f"debit={trial_balance.total_debit.amount} IRR, "
                f"credit={trial_balance.total_credit.amount} IRR "
                f"({trial_balance.move_count} moves).",
            )
        details.append(
            f"{company.name}: debit == credit == "
            f"{trial_balance.total_debit.amount} IRR ({trial_balance.move_count} moves)"
        )
    if not details:
        return _skip(
            CHECK_TRIAL_BALANCE,
            "No company has fiscal years in the restored database: "
            + ", ".join(skipped_companies),
        )
    detail = "; ".join(details)
    if skipped_companies:
        detail += f" (no fiscal years, skipped: {', '.join(skipped_companies)})"
    return _pass(CHECK_TRIAL_BALANCE, detail)


def check_external_references(
    hesabyar_engine: Engine,
    engine: AccountingEnginePort,
    *,
    max_refs: int,
) -> DrillCheckResult:
    """Every Tryton-pointing external-id-map row resolves in the restored engine.

    The check reads the map from the RESTORED HesabYar database and
    resolves each target through the REST adapter against the
    RESTORED Tryton database — the cross-database reconciliation the
    spec's restore procedure requires. A target the port cannot read
    (an unknown model) is counted as unchecked, not failed; a
    non-integer target id cannot resolve to any engine record and
    counts as orphaned. An adapter ERROR (as opposed to a clean
    "not found") fails the check — the drill cannot verify what it
    cannot read.
    """
    with hesabyar_engine.connect() as connection:
        total = connection.execute(
            select(func.count())
            .select_from(ExternalIdMapModel)
            .where(ExternalIdMapModel.target_engine == "tryton")
        ).scalar_one()
        rows = connection.execute(
            select(
                ExternalIdMapModel.target_entity_type,
                ExternalIdMapModel.target_external_id,
            )
            .where(ExternalIdMapModel.target_engine == "tryton")
            .order_by(ExternalIdMapModel.id)
            .limit(max_refs)
        ).all()
    checked = 0
    orphans: list[str] = []
    unchecked = 0
    for target_type, target_id in rows:
        if target_type not in _READABLE_TARGET_TYPES:
            unchecked += 1
            continue
        try:
            engine_id = int(target_id)
        except (TypeError, ValueError):
            orphans.append(f"{target_type}:{target_id} (not an integer id)")
            continue
        try:
            if target_type == "account.invoice":
                found = engine.get_invoice(InvoiceRef(engine_id)) is not None
            elif target_type == "account.move":
                found = engine.get_journal(PostedJournalRef(engine_id)) is not None
            else:  # account.payment
                found = engine.get_payment(PaymentRef(engine_id)) is not None
        except Exception as exc:
            return _fail(
                CHECK_EXTERNAL_REFS,
                f"Could not resolve {target_type}:{target_id} against the "
                f"restored engine: {exc}",
            )
        checked += 1
        if not found:
            orphans.append(f"{target_type}:{target_id}")
    unchecked += max(0, int(total) - len(rows))
    if orphans:
        return _fail(
            CHECK_EXTERNAL_REFS,
            f"{len(orphans)} external reference(s) do not resolve in the "
            f"restored Tryton database (checked {checked}, unchecked {unchecked}): "
            + "; ".join(orphans[:_MAX_ORPHAN_DETAILS]),
        )
    return _pass(
        CHECK_EXTERNAL_REFS,
        f"{checked} reference(s) resolve in the restored Tryton database"
        + (f", {unchecked} unchecked (unknown model or over the {max_refs} cap)"
           if unchecked else ""),
    )


# ---------------------------------------------------------------------------
# The drill
# ---------------------------------------------------------------------------

#: Factory building an engine port for one Tryton database name. A
#: context manager so the underlying REST client is closed after the
#: check (the port itself has no close method).
ScratchEngineFactory = Callable[[str], AbstractContextManager[AccountingEnginePort]]


class RestoreDrillService:
    """Download + restore + reconcile one backup pair into scratch databases."""

    def __init__(
        self,
        *,
        store: ObjectStorePort,
        cipher: BackupCipherPort,
        dumper: DatabaseDumperPort,
        hesabyar_admin_url: str | None,
        tryton_admin_url: str | None,
        engine_factory: ScratchEngineFactory | None = None,
        migrations_dir: Path | None = None,
        max_external_refs_checked: int = 200,
        clock: Callable[[], datetime.datetime] | None = None,
        scratch_visibility_timeout_seconds: float = (
            DEFAULT_SCRATCH_VISIBILITY_TIMEOUT_SECONDS
        ),
        scratch_visibility_poll_seconds: float = (
            DEFAULT_SCRATCH_VISIBILITY_POLL_SECONDS
        ),
        sleep: Callable[[float], None] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        # An admin URL may be None (that database is not configured
        # for drilling): the target's restore check FAILS at run time
        # instead of the service refusing to construct — a
        # HesabYar-only drill must not need the Tryton DSN.
        self._backup = BackupService(store=store, cipher=cipher, dumper=dumper)
        self._store = store
        self._dumper = dumper
        self._hesabyar_admin_url = hesabyar_admin_url
        self._tryton_admin_url = tryton_admin_url
        self._engine_factory = engine_factory
        self._migrations_dir = migrations_dir or resolve_migrations_dir()
        self._max_external_refs_checked = max_external_refs_checked
        self._clock = clock or (lambda: datetime.datetime.now(datetime.UTC))
        self._scratch_visibility_timeout_seconds = scratch_visibility_timeout_seconds
        self._scratch_visibility_poll_seconds = scratch_visibility_poll_seconds
        # Injectable for tests (a real 6-minute wait has no place in a
        # unit test); the monotonic clock is separate from ``clock``
        # (wall time for the report) on purpose — a fixed test clock
        # must never freeze the wait loop.
        self._sleep = sleep or time.sleep
        self._monotonic = monotonic or time.monotonic

    def latest_manifest(self, target: BackupTarget) -> BackupManifest | None:
        """The newest backup manifest for a target (the drill's input)."""
        return self._backup.latest_manifest(target)

    def run(
        self,
        manifests: Mapping[BackupTarget, BackupManifest],
        *,
        targets: tuple[BackupTarget, ...] = (
            BackupTarget.HESABYAR_DB,
            BackupTarget.TRYTON_DB,
        ),
        keep_scratch: bool = False,
        now: datetime.datetime | None = None,
    ) -> DrillReport:
        """Run the drill for ``targets``; always cleans up scratch databases."""
        started_at = (now or self._clock()).astimezone(datetime.UTC)
        checks: list[DrillCheckResult] = []
        scratch: dict[BackupTarget, str] = {}
        scratch_admin: dict[BackupTarget, str] = {}  # target -> admin URL used
        restored: dict[BackupTarget, str] = {}  # target -> scratch DB name
        cleanup_errors: list[str] = []
        try:
            try:
                tools = self._dumper.check_tools()
            except BackupError as exc:
                checks.append(_fail("preflight.tools", str(exc)))
                return self._report(
                    started_at, checks, manifests, scratch, cleanup_errors, keep_scratch
                )
            logger.info(
                "Restore drill starting (pg_dump %s, pg_restore %s).",
                tools.pg_dump_version,
                tools.pg_restore_version,
            )
            # -- download + verify + restore, per target -------------------
            dumps: dict[BackupTarget, bytes] = {}
            for target in targets:
                manifest = manifests.get(target)
                checksum_name = CHECK_MANIFEST_CHECKSUM.format(target=target.value)
                if manifest is None:
                    checks.append(
                        _fail(checksum_name, f"No backup manifest given for {target.value}.")
                    )
                    continue
                try:
                    dumps[target] = self._backup.download_backup(manifest)
                except BackupError as exc:
                    checks.append(_fail(checksum_name, str(exc)))
                    continue
                checks.append(
                    _pass(
                        checksum_name,
                        f"checksum + decrypt ok ({manifest.plaintext_size_bytes} bytes, "
                        f"key {manifest.encryption_key_id})",
                    )
                )
            stamp = started_at.strftime("%Y%m%d_%H%M%S")
            for target, data in dumps.items():
                scratch_name = (
                    f"{target.value}_drill_{stamp}_{secrets.token_hex(4)}"
                )
                restore_name = CHECK_RESTORE.format(target=target.value)
                admin_url = (
                    self._hesabyar_admin_url
                    if target is BackupTarget.HESABYAR_DB
                    else self._tryton_admin_url
                )
                if admin_url is None:
                    checks.append(
                        _fail(
                            restore_name,
                            f"No admin database URL is configured for {target.value} "
                            "(the drill needs a CREATEDB role to make scratch databases).",
                        )
                    )
                    continue
                try:
                    self._dumper.create_database(admin_url, scratch_name)
                    scratch[target] = scratch_name
                    scratch_admin[target] = admin_url
                except BackupError as exc:
                    checks.append(_fail(restore_name, str(exc)))
                    continue
                # Ownership differs per target, and it matters:
                # the HesabYar scratch DB is read by the drill itself
                # as the admin superuser, so --no-owner is fine (and
                # more robust). The Tryton scratch DB is read by
                # trytond AS THE TRYTON ROLE (a plain LOGIN role, not
                # a superuser) — a --no-owner restore would leave
                # every object owned by the admin with no ACLs, and
                # trytond could not read a single table. So the
                # Tryton restore KEEPS owners + ACLs: the drill
                # restores onto the SAME server, where the tryton
                # role exists (a cross-server restore needs the
                # roles created first — see docs/backup-restore.md).
                try:
                    self._dumper.restore(
                        with_database_name(admin_url, scratch_name),
                        data,
                        no_owner=target is BackupTarget.HESABYAR_DB,
                    )
                except BackupError as exc:
                    checks.append(_fail(restore_name, str(exc)))
                    continue
                restored[target] = scratch_name
                checks.append(
                    _pass(restore_name, f"restored into scratch database {scratch_name}")
                )
            # -- HesabYar checks -------------------------------------------
            hesabyar_engine: Engine | None = None
            try:
                if BackupTarget.HESABYAR_DB in restored:
                    hesabyar_engine = create_engine(
                        _sqlalchemy_url(
                            with_database_name(
                                scratch_admin[BackupTarget.HESABYAR_DB],
                                restored[BackupTarget.HESABYAR_DB],
                            )
                        ),
                        poolclass=NullPool,
                    )
                    checks.append(
                        self._run_check(
                            CHECK_MIGRATIONS,
                            check_alembic,
                            hesabyar_engine,
                            self._migrations_dir,
                        )
                    )
                    checks.append(
                        self._run_check(
                            CHECK_ORPHANS, check_orphaned_documents, hesabyar_engine
                        )
                    )
                    checks.append(
                        self._run_check(
                            CHECK_TRANCHES, check_settlement_tranches, hesabyar_engine
                        )
                    )
                else:
                    for name in (CHECK_MIGRATIONS, CHECK_ORPHANS, CHECK_TRANCHES):
                        checks.append(
                            _skip(name, "HesabYar scratch database was not restored.")
                        )
                # -- Tryton + cross-database checks -------------------------
                self._run_tryton_checks(
                    checks, restored, hesabyar_engine,
                )
            finally:
                if hesabyar_engine is not None:
                    hesabyar_engine.dispose()
        finally:
            if not keep_scratch:
                for target, name in scratch.items():
                    try:
                        self._dumper.drop_database(scratch_admin[target], name)
                    except BackupError as exc:
                        cleanup_errors.append(f"{name}: {exc}")
                        logger.warning("Could not drop scratch database %s: %s", name, exc)
        return self._report(
            started_at, checks, manifests, scratch, cleanup_errors, keep_scratch
        )

    @staticmethod
    def _run_check(
        name: str, func: Callable[..., DrillCheckResult], *args: Any, **kwargs: Any
    ) -> DrillCheckResult:
        """Run one check; an unexpected error FAILS it (never aborts the drill)."""
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            logger.exception("Drill check %s raised.", name)
            return _fail(name, f"Check raised unexpectedly: {exc}")

    def _run_tryton_checks(
        self,
        checks: list[DrillCheckResult],
        restored: dict[BackupTarget, str],
        hesabyar_engine: Engine | None,
    ) -> None:
        """Trial-balance + cross-database checks (need the scratch Tryton DB + engine)."""
        tryton_restored = BackupTarget.TRYTON_DB in restored
        if not tryton_restored:
            checks.append(
                _skip(CHECK_TRIAL_BALANCE, "Tryton scratch database was not restored.")
            )
            checks.append(
                _skip(
                    CHECK_EXTERNAL_REFS,
                    "Tryton scratch database was not restored.",
                )
            )
            return
        if self._engine_factory is None:
            checks.append(
                _skip(
                    CHECK_TRIAL_BALANCE,
                    "No scratch engine is configured — the drill cannot read "
                    "the restored Tryton database (see docs/backup-restore.md).",
                )
            )
            checks.append(
                _skip(
                    CHECK_EXTERNAL_REFS,
                    "No scratch engine is configured — the drill cannot resolve "
                    "external references against the restored Tryton database.",
                )
            )
            return
        try:
            with self._engine_factory(restored[BackupTarget.TRYTON_DB]) as engine:
                # trytond caches its database list (session.timeout,
                # default 300s): a scratch DB created after trytond
                # started 404s until the cache expires. Wait for it
                # to become visible; if it never does, SKIP the
                # Tryton checks with the reason — the restore itself
                # succeeded, only the verification could not run.
                if not _wait_for_scratch_database(
                    engine,
                    timeout_seconds=self._scratch_visibility_timeout_seconds,
                    poll_seconds=self._scratch_visibility_poll_seconds,
                    sleep=self._sleep,
                    monotonic=self._monotonic,
                ):
                    reason = (
                        "trytond does not serve the scratch database yet: "
                        "its database-list cache (session.timeout) did not "
                        f"expire within {self._scratch_visibility_timeout_seconds:.0f}s. "
                        "Restart trytond after the restore (or lower "
                        "session.timeout) and re-run the drill — see "
                        "docs/backup-restore.md."
                    )
                    checks.append(_skip(CHECK_TRIAL_BALANCE, reason))
                    checks.append(_skip(CHECK_EXTERNAL_REFS, reason))
                    return
                checks.append(
                    self._run_check(CHECK_TRIAL_BALANCE, check_trial_balance, engine)
                )
                if hesabyar_engine is not None:
                    checks.append(
                        self._run_check(
                            CHECK_EXTERNAL_REFS,
                            check_external_references,
                            hesabyar_engine,
                            engine,
                            max_refs=self._max_external_refs_checked,
                        )
                    )
                else:
                    checks.append(
                        _skip(
                            CHECK_EXTERNAL_REFS,
                            "HesabYar scratch database was not restored.",
                        )
                    )
        except Exception as exc:
            logger.exception("Drill Tryton checks raised.")
            checks.append(
                _fail(CHECK_TRIAL_BALANCE, f"Could not open the scratch engine: {exc}")
            )
            checks.append(
                _skip(
                    CHECK_EXTERNAL_REFS,
                    "Skipped: the scratch engine could not be opened.",
                )
            )

    def _report(
        self,
        started_at: datetime.datetime,
        checks: list[DrillCheckResult],
        manifests: Mapping[BackupTarget, BackupManifest],
        scratch: dict[BackupTarget, str],
        cleanup_errors: list[str],
        keep_scratch: bool,
    ) -> DrillReport:
        finished_at = self._clock().astimezone(datetime.UTC)
        report = DrillReport(
            started_at=started_at,
            finished_at=finished_at,
            checks=tuple(checks),
            hesabyar_manifest=manifests.get(BackupTarget.HESABYAR_DB),
            tryton_manifest=manifests.get(BackupTarget.TRYTON_DB),
            scratch_databases=tuple(sorted(scratch.values())),
            cleanup_errors=tuple(cleanup_errors),
            keep_scratch=keep_scratch,
        )
        logger.info(
            "Restore drill done: passed=%s fully_verified=%s failed=%s skipped=%s",
            report.passed,
            report.fully_verified,
            [c.name for c in report.failed_checks()],
            [c.name for c in report.skipped_checks()],
        )
        return report
