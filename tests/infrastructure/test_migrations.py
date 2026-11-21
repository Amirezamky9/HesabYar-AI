"""Fresh-install migration roundtrip tests (US-102).

Runs Alembic upgrade/downgrade against a SEPARATE throwaway database
(``hesabyar_migration_test``) — never the shared ``hesabyar_test`` DB, which
other workers use in parallel. The throwaway DB is created and dropped via
the local postgres superuser (``sudo -n -u postgres psql`` — the same
mechanism ``restore-dev-env.sh`` uses); the tests skip cleanly when
superuser access is unavailable.

Covers:
- upgrade head on an empty DB creates every table (0001 + US-102);
- the design-eng §10.4 unique constraints exist and reject duplicates;
- no foreign key points outside the HesabYar schema (no SQL FKs into
  Tryton's database — Tryton ids are external references only);
- ORM models and the migrated schema agree on column names;
- downgrade base drops all tables; upgrade head again recreates them.
"""

from __future__ import annotations

import datetime
import os
import subprocess
import uuid
from collections.abc import Generator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from hesabyar.infrastructure.persistence.postgres.models import (
    Base,
    ExternalIdMapModel,
    IdempotencyRecordModel,
    InboxEventModel,
    TenantModel,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

MIGRATION_DB_NAME = "hesabyar_migration_test"
MIGRATION_DB_URL = "postgresql+psycopg://hesabyar:hesabyar_pass@127.0.0.1:5432/" + MIGRATION_DB_NAME

EXPECTED_TABLES = {
    # 0001 foundation
    "tenants",
    "actors",
    # 0002 engine mapping
    "engine_connections",
    "engine_companies",
    "external_id_map",
    # 0003 API access + audit
    "api_clients",
    "api_keys",
    "audit_events",
    "audit_event_subjects",
    # 0004 integration + idempotency
    "inbox_events",
    "outbox_events",
    "integration_cursors",
    "integration_attempts",
    "idempotency_records",
    # 0005 standards registry + validation runs (US-401)
    "standard_sources",
    "standard_revisions",
    "standard_rules",
    "validation_runs",
    "validation_results",
    # 0006 tax-policy registry (US-402)
    "tax_sources",
    "tax_policy_versions",
    "tax_decisions",
    # 0007 Moadian test profiles + simulator (US-404)
    "moadian_profiles",
    "moadian_documents",
    "moadian_submissions",
    "moadian_status_events",
    # 0008 report snapshots with provenance (US-403)
    "report_snapshots",
    # 0009 event platform: decision audit log + store settings (US-501)
    "automation_decision_log",
    "connector_store_settings",
    # 0010 integrations registries + policy gate (US-503)
    "payment_channels",
    "gateway_contracts",
    "integration_mappings",
    "mapping_suggestions",
    "auto_post_policies",
    "bank_connections",
    "stores",
    "installation_settings",
    "installation_holidays",
    # 0011 review queue (US-504)
    "review_items",
    "review_item_audit",
    "review_sla_policies",
    # 0015 manual entries (US-513)
    "manual_entries",
    # 0016 bank statement import + reconciliation (US-514)
    "bank_column_mappings",
    "bank_import_batches",
    "bank_import_rows",
    "bank_match_rules",
    "bank_statement_matches",
    # 0017 packaging & shipping (US-515)
    "packaging_materials",
    "packaging_consumptions",
    "packaging_shipment_costs",
    "packaging_carrier_rates",
    "packaging_counts",
    "packaging_order_cost_centers",
    "packaging_settings",
    "packaging_supply_watermarks",
    # 0018 cash-box counting (US-516)
    "cash_boxes",
    "cash_count_sessions",
    "cashbox_settings",
    # 0020 settlement engine core (US-521)
    "gateway_payments",
    "settlement_tranches",
    "settlement_events",
    "bank_deposits",
    "settlement_matches",
    "settlement_exceptions",
    "settlement_fee_records",
    "settlement_file_imports",
    "settlement_file_rows",
    # 0021 settlement posting ledger (US-522)
    "settlement_postings",
    # 0023 cheque registry (US-531)
    "cheques",
    "cheque_custody_log",
    "cheque_inquiry_receipts",
    "cheque_settings",
    # 0027 installments (US-540)
    "installment_plans",
    "installment_legs",
    "installment_receipts",
    "installment_receipt_allocations",
    "installment_sale_contracts",
    # 0028 installment finance (US-541)
    "installment_late_fee_assessments",
    "installment_schedule_amendments",
    # 0031 SMS reminders (US-544)
    "sms_providers",
    "sms_reminder_settings",
    "sms_contract_reminder_settings",
    "sms_reminder_log",
    # 0033 monthly clearing-match history (US-553)
    "cheque_match_history",
    # 0034 manual-import batches (US-554)
    "manual_import_batches",
    "settlement_column_mappings",
    # 0035 job registry (US-560)
    "scheduled_jobs",
    "scheduled_job_runs",
    # 0036 rules engine v1 (US-561)
    "rules",
    "rule_executions",
    "rule_set_configs",
    # 0038 reminder preferences + log (US-564)
    "reminder_preferences",
    "reminder_log",
    # 0037 recurring templates + occurrences (US-563)
    "recurring_templates",
    "recurring_occurrences",
    # 0039 bank auto-coding rules + suggestions (US-565)
    "bank_rules",
    "bank_rule_suggestions",
    # 0040 AI access audit log (US-601)
    "ai_access_audit",
    # 0041 MCP authorized-clients registry (US-606)
    "mcp_authorized_clients",
    # 0042 monitoring alerts + channels + token records (US-704)
    "monitoring_alerts",
    "monitoring_alert_channels",
    "monitoring_token_records",
}

# Design-eng §10.4 unique constraints: (table, constraint name, columns).
SECTION_10_4_CONSTRAINTS = [
    (
        "inbox_events",
        "uq_inbox_events_source_event",
        ["source_system", "source_store_id", "source_event_id"],
    ),
    (
        "idempotency_records",
        "uq_idempotency_records_installation_operation_key",
        ["installation_id", "operation", "idempotency_key"],
    ),
    (
        "external_id_map",
        "uq_external_id_map_source_target",
        ["source_system", "external_entity_type", "external_entity_id", "target_engine"],
    ),
    # US-401 registry uniques (spec §7.4/§12.1).
    ("standard_sources", "uq_standard_sources_code", ["standard_code"]),
    ("standard_sources", "uq_standard_sources_slug", ["slug"]),
    (
        "standard_revisions",
        "uq_standard_revisions_source_revision",
        ["source_id", "revision_id"],
    ),
    (
        "standard_rules",
        "uq_standard_rules_revision_rule",
        ["revision_id", "rule_id"],
    ),
    (
        "validation_results",
        "uq_validation_results_run_rule",
        ["run_id", "rule_code"],
    ),
    # US-402 registry uniques (spec §7.4/§12.2).
    ("tax_sources", "uq_tax_sources_key", ["source_key"]),
    (
        "tax_policy_versions",
        "uq_tax_policy_versions_rule_version",
        ["rule_key", "version_id"],
    ),
    # US-404 Moadian uniques (spec §7.4/§12.3).
    (
        "moadian_profiles",
        "uq_moadian_profiles_tenant_slug",
        ["tenant_id", "slug"],
    ),
    (
        "moadian_documents",
        "uq_moadian_documents_tenant_taxid",
        ["tenant_id", "taxid"],
    ),
    (
        "moadian_submissions",
        "uq_moadian_submissions_uid",
        ["uid"],
    ),
    # US-403 report snapshots (spec §7.4/§12.1): one snapshot per run.
    (
        "report_snapshots",
        "uq_report_snapshots_validation_run",
        ["validation_run_id"],
    ),
    # US-563 recurring occurrences (spec §10.4): one occurrence per
    # template per scheduled date AND per occurrence number — the
    # generator's idempotency backstop.
    (
        "recurring_occurrences",
        "uq_recurring_occurrences_template_number",
        ["template_id", "occurrence_number"],
    ),
    (
        "recurring_occurrences",
        "uq_recurring_occurrences_template_date",
        ["template_id", "scheduled_date"],
    ),
    # US-501 store settings (ADR 0003 decision 4): one row per store.
    (
        "connector_store_settings",
        "uq_connector_store_settings_source_store",
        ["source_system", "source_store_id"],
    ),
    # US-503 integrations registries (spec §7.4/§10.5/ADR 0003 item 12).
    (
        "payment_channels",
        "uq_payment_channels_installation_key",
        ["installation_id", "channel_key"],
    ),
    (
        "gateway_contracts",
        "uq_gateway_contracts_channel_version",
        ["channel_id", "version"],
    ),
    (
        "integration_mappings",
        "uq_integration_mappings_identity_version",
        ["installation_id", "mapping_kind", "channel_key", "source_key", "version"],
    ),
    (
        "auto_post_policies",
        "uq_auto_post_policies_installation_event",
        ["installation_id", "event_type"],
    ),
    (
        "bank_connections",
        "uq_bank_connections_installation_company_bank",
        ["installation_id", "company_id", "bank_key"],
    ),
    (
        "stores",
        "uq_stores_installation_key",
        ["installation_id", "store_key"],
    ),
    (
        "installation_holidays",
        "uq_installation_holidays_installation_date",
        ["installation_id", "holiday_date"],
    ),
    # US-504 review queue: one SLA policy per (tenant, item type).
    (
        "review_sla_policies",
        "uq_review_sla_policies_tenant_type",
        ["tenant_id", "item_type"],
    ),
    # US-516 cash box: one box per (tenant, company, statement journal).
    (
        "cash_boxes",
        "uq_cash_boxes_tenant_company_journal",
        ["tenant_id", "company_id", "statement_journal_id"],
    ),
    # US-521 settlement engine: one payment per (channel, gateway transaction).
    (
        "gateway_payments",
        "uq_gateway_payments_channel_transaction",
        ["payment_channel_id", "gateway_transaction_id"],
    ),
    # US-521: one tranche row per (payment, tranche index).
    (
        "settlement_tranches",
        "uq_settlement_tranches_payment_index",
        ["gateway_payment_id", "tranche_index"],
    ),
    # US-521: re-importing the same file (channel + hash) is a no-op.
    (
        "settlement_file_imports",
        "uq_settlement_file_imports_channel_hash",
        ["payment_channel_id", "file_hash_sha256"],
    ),
    # US-522: one fee_at_sale posting per payment (partial unique index).
    (
        "settlement_postings",
        "uq_settlement_postings_payment_kind",
        ["gateway_payment_id", "kind"],
    ),
    # US-522: one fee_at_settlement posting per tranche (partial unique index).
    (
        "settlement_postings",
        "uq_settlement_postings_tranche_kind",
        ["tranche_id", "kind"],
    ),
    # US-553: one match-history row per (installation, Jalali month).
    (
        "cheque_match_history",
        "uq_cheque_match_history_installation_month",
        ["installation_id", "month_jalali"],
    ),
    # US-554: upload dedup — one batch per (tenant, kind, source, file hash).
    (
        "manual_import_batches",
        "uq_manual_import_batches_tenant_kind_source_hash",
        ["tenant_id", "kind", "source_key", "file_hash_sha256"],
    ),
]

HEAD_REVISION = "0042"


def _alembic_config() -> Config:
    """Build an Alembic Config pointing at this repo's migrations."""
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return config


def _run_psql_as_postgres(*statements: str) -> subprocess.CompletedProcess[str]:
    """Run SQL as the postgres superuser via passwordless sudo."""
    args = ["sudo", "-n", "-u", "postgres", "psql", "-v", "ON_ERROR_STOP=1", "-q"]
    for statement in statements:
        args += ["-c", statement]
    return subprocess.run(args, capture_output=True, text=True, timeout=60, check=False)


def _create_migration_db() -> None:
    """Create the throwaway migration DB (dropping any leftover first)."""
    try:
        result = _run_psql_as_postgres(
            f'DROP DATABASE IF EXISTS "{MIGRATION_DB_NAME}" WITH (FORCE);',
            f"CREATE DATABASE \"{MIGRATION_DB_NAME}\" OWNER hesabyar ENCODING 'UTF8';",
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"cannot create throwaway migration DB (no superuser psql): {exc}")
    if result.returncode != 0:
        pytest.skip(f"cannot create throwaway migration DB (sudo -u postgres psql failed): {result.stderr.strip()}")


def _drop_migration_db() -> None:
    try:
        _run_psql_as_postgres(
            f'DROP DATABASE IF EXISTS "{MIGRATION_DB_NAME}" WITH (FORCE);',
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


@pytest.fixture(scope="module")
def migration_engine() -> Generator[Engine, None, None]:
    """Throwaway DB upgraded to head; dropped after the module."""
    _create_migration_db()
    previous_url = os.environ.get("HESABYAR_DATABASE_URL")
    # migrations/env.py prefers HESABYAR_DATABASE_URL over alembic.ini.
    os.environ["HESABYAR_DATABASE_URL"] = MIGRATION_DB_URL
    try:
        command.upgrade(_alembic_config(), "head")
        engine = create_engine(MIGRATION_DB_URL, echo=False)
        yield engine
        engine.dispose()
    finally:
        if previous_url is None:
            os.environ.pop("HESABYAR_DATABASE_URL", None)
        else:
            os.environ["HESABYAR_DATABASE_URL"] = previous_url
        _drop_migration_db()


@pytest.fixture()
def db(migration_engine: Engine) -> Generator[Session, None, None]:
    """Provide a transactional session that rolls back after each test."""
    connection = migration_engine.connect()
    transaction = connection.begin()
    session = sessionmaker(bind=connection)()
    try:
        yield session
    finally:
        session.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()


def _make_tenant(slug: str) -> TenantModel:
    return TenantModel(id=str(uuid.uuid4()), slug=slug, name="Migration Test Tenant")


def test_upgrade_head_creates_all_tables(migration_engine: Engine) -> None:
    inspector = inspect(migration_engine)
    tables = set(inspector.get_table_names())
    missing = EXPECTED_TABLES - tables
    assert not missing, f"tables missing after upgrade head: {sorted(missing)}"
    with migration_engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert version == HEAD_REVISION


def test_model_columns_match_migrated_schema(migration_engine: Engine) -> None:
    """Every ORM model column exists in the migrated DB, and vice versa."""
    inspector = inspect(migration_engine)
    for table_name, table in Base.metadata.tables.items():
        db_columns = {c["name"] for c in inspector.get_columns(table_name)}
        model_columns = set(table.columns.keys())
        assert db_columns == model_columns, (
            f"{table_name}: DB-only={sorted(db_columns - model_columns)}, "
            f"model-only={sorted(model_columns - db_columns)}"
        )


def test_section_10_4_unique_constraints_exist(migration_engine: Engine) -> None:
    inspector = inspect(migration_engine)
    for table, name, columns in SECTION_10_4_CONSTRAINTS:
        uniques = {u["name"]: u["column_names"] for u in inspector.get_unique_constraints(table)}
        assert name in uniques, f"{table} is missing unique constraint {name}"
        assert uniques[name] == columns, f"{table}.{name} covers {uniques[name]}, expected {columns}"


def test_no_foreign_keys_leave_hesabyar_schema(migration_engine: Engine) -> None:
    """Every FK target is a HesabYar-owned table (spec §7.1).

    Cross-database FKs are impossible in PostgreSQL; this asserts the
    checkable form of the rule: no FK references any table outside our
    own migrated schema, so Tryton ids can only be external references.
    """
    inspector = inspect(migration_engine)
    for table in sorted(EXPECTED_TABLES):
        for fk in inspector.get_foreign_keys(table):
            assert fk["referred_table"] in EXPECTED_TABLES, (
                f"{table}.{fk['constrained_columns']} references "
                f"{fk['referred_table']}, which is not a HesabYar-owned table"
            )


def test_duplicate_inbox_event_rejected(db: Session) -> None:
    def _event() -> InboxEventModel:
        return InboxEventModel(
            id=str(uuid.uuid4()),
            source_system="hayat",
            source_store_id="store-1",
            source_event_id="evt-1",
            event_type="sales.order.completed",
            occurred_at=datetime.datetime.now(datetime.UTC),
            payload={"order": 1},
            payload_hash="aabbcc",
        )

    db.add(_event())
    db.flush()
    db.add(_event())
    with pytest.raises(IntegrityError):
        db.flush()


def test_duplicate_idempotency_record_rejected(db: Session) -> None:
    def _record() -> IdempotencyRecordModel:
        return IdempotencyRecordModel(
            id=str(uuid.uuid4()),
            installation_id="00000000-0000-0000-0000-000000000001",
            operation="ledger.draft.create",
            idempotency_key="key-1",
            request_hash="hash1",
        )

    db.add(_record())
    db.flush()
    db.add(_record())
    with pytest.raises(IntegrityError):
        db.flush()


def test_duplicate_external_id_map_rejected(db: Session) -> None:
    tenant = _make_tenant("external-id-map-test")
    db.add(tenant)
    db.flush()

    def _mapping() -> ExternalIdMapModel:
        return ExternalIdMapModel(
            id=str(uuid.uuid4()),
            tenant_id=tenant.id,
            source_system="hayat",
            external_entity_type="order",
            external_entity_id="order-1",
            target_engine="tryton",
            target_entity_type="account.invoice",
            target_external_id="42",
        )

    db.add(_mapping())
    db.flush()
    db.add(_mapping())
    with pytest.raises(IntegrityError):
        db.flush()


def test_downgrade_upgrade_roundtrip(migration_engine: Engine) -> None:
    """Downgrade to base drops all tables; upgrade head recreates them.

    NOTE: this test must stay last in the module — it mutates the shared
    module-scoped database. It re-upgrades to head before returning.
    """
    config = _alembic_config()

    command.downgrade(config, "base")
    tables_after_downgrade = set(inspect(migration_engine).get_table_names())
    remaining = EXPECTED_TABLES & tables_after_downgrade
    assert not remaining, f"tables left after downgrade base: {sorted(remaining)}"

    command.upgrade(config, "head")
    tables_after_upgrade = set(inspect(migration_engine).get_table_names())
    missing = EXPECTED_TABLES - tables_after_upgrade
    assert not missing, f"tables missing after re-upgrade head: {sorted(missing)}"
    with migration_engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert version == HEAD_REVISION
