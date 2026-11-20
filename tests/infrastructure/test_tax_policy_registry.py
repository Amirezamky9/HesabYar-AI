"""Tests for the tax-policy registry schema + seed (US-402).

Runs against the shared Supabase test DB in the same style as
test_phase1_schema.py: tables are created by Base.metadata.create_all
(a no-op once Alembic is at head) and each test runs in a rolled-back
transaction. The migration roundtrip itself lives in
test_migrations.py on a separate throwaway database.
"""

from __future__ import annotations

import datetime
import os
import uuid
from collections.abc import Generator
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from hesabyar.infrastructure.persistence.postgres.models import (
    Base,
    TaxDecisionModel,
    TaxPolicyVersionModel,
    TaxSourceModel,
    TenantModel,
)
from hesabyar.infrastructure.persistence.postgres.seed_tax_policy import (
    FY1405_EFFECTIVE_FROM,
    FY1405_EFFECTIVE_TO,
    seed_tax_policy,
)

TEST_DB_URL = os.getenv(
    "HESABYAR_DATABASE_URL",
    "postgresql+psycopg://hesabyar:hesabyar_pass@hesabyar-postgres:5432/hesabyar_test",
)

REGISTRY_TABLES = (
    "tax_sources",
    "tax_policy_versions",
    "tax_decisions",
)


@pytest.fixture(scope="module")
def engine() -> Generator[Engine, None, None]:
    """Create a SQLAlchemy engine connected to the test database."""
    eng = create_engine(TEST_DB_URL, echo=False)
    # Ensure tables exist without dropping schema or desynchronizing Alembic.
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def db(engine: Engine) -> Generator[Session, None, None]:
    """Provide a transactional session that rolls back after each test."""
    connection = engine.connect()
    transaction = connection.begin()
    session = sessionmaker(bind=connection)()
    try:
        yield session
    finally:
        session.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()


def test_registry_tables_exist(engine: Engine) -> None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    for table in REGISTRY_TABLES:
        assert table in tables, f"table {table} missing"


def test_seed_is_idempotent(db: Session) -> None:
    first = seed_tax_policy(db)
    db.flush()
    second = seed_tax_policy(db)
    db.flush()
    assert first["sources_created"] + first["sources_updated"] == 3
    assert first["versions_created"] + first["versions_updated"] == 5
    # Second run updates the same rows — nothing new is created.
    assert second["sources_created"] == 0
    assert second["versions_created"] == 0
    assert second["sources_updated"] == 3
    assert second["versions_updated"] == 5
    sources = list(db.execute(select(TaxSourceModel)).scalars())
    versions = list(db.execute(select(TaxPolicyVersionModel)).scalars())
    assert len(sources) == 3
    assert len(versions) == 5


def test_seed_versions_carry_1405_parameters(db: Session) -> None:
    seed_tax_policy(db)
    db.flush()
    versions = {row.rule_key: row for row in db.execute(select(TaxPolicyVersionModel)).scalars()}
    assert versions["vat_general_rate"].parameter_value == Decimal("10")
    assert versions["vat_general_rate"].value_unit == "percent"
    assert versions["art84_salary_exemption"].parameter_value == Decimal("4800000000")
    assert versions["art101_exemption"].parameter_value == Decimal("2800000000")
    assert versions["art100_flat_tax_ceiling"].parameter_value == Decimal("720000000000")
    assert versions["growth_threshold_art105_131"].parameter_value == Decimal("45")
    assert all(row.value_unit in ("percent", "irr") for row in versions.values())
    assert all(row.verification_status == "CORROBORATED_CURRENT" for row in versions.values())
    assert all(row.version_id == "1405" for row in versions.values())
    assert all(row.status == "active" for row in versions.values())
    assert all(row.effective_from == FY1405_EFFECTIVE_FROM for row in versions.values())
    assert all(row.effective_to == FY1405_EFFECTIVE_TO for row in versions.values())


def test_seed_years_stay_separate(db: Session) -> None:
    """Rule, performance and filing years are separate persisted fields (spec §12.2)."""
    seed_tax_policy(db)
    db.flush()
    versions = {row.rule_key: row for row in db.execute(select(TaxPolicyVersionModel)).scalars()}
    art100 = versions["art100_flat_tax_ceiling"]
    assert (art100.rule_year, art100.performance_year, art100.filing_year) == (1405, 1404, 1405)
    growth = versions["growth_threshold_art105_131"]
    assert (growth.rule_year, growth.performance_year, growth.filing_year) == (1405, 1404, 1405)
    for key in ("vat_general_rate", "art84_salary_exemption", "art101_exemption"):
        row = versions[key]
        assert (row.rule_year, row.performance_year, row.filing_year) == (1405, 1405, 1405)


def test_seed_corroboration_urls_recorded(db: Session) -> None:
    seed_tax_policy(db)
    db.flush()
    versions = {row.rule_key: row for row in db.execute(select(TaxPolicyVersionModel)).scalars()}
    art100 = versions["art100_flat_tax_ceiling"]
    assert art100.corroboration_urls == [
        "https://www.tasnimnews.ir/fa/news/1405/06/12/3687803/",
        "https://www.tasnimnews.ir/fa/news/1405/02/28/3593811/",
    ]
    assert versions["vat_general_rate"].corroboration_urls == ["https://www.tasnimnews.ir/fa/news/1404/11/25/3516503/"]


def test_duplicate_rule_version_rejected(db: Session) -> None:
    seed_tax_policy(db)
    db.flush()
    source = db.execute(
        select(TaxSourceModel).where(TaxSourceModel.source_key == "tasnim-budget-1404-11-25")
    ).scalar_one()
    db.add(
        TaxPolicyVersionModel(
            source_id=source.id,
            rule_key="vat_general_rate",
            rule_name="duplicate",
            version_id="1405",
            parameter_value=Decimal("10"),
            value_unit="percent",
            rule_year=1405,
            effective_from=FY1405_EFFECTIVE_FROM,
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()


def test_duplicate_source_key_rejected(db: Session) -> None:
    seed_tax_policy(db)
    db.flush()
    db.add(
        TaxSourceModel(
            source_key="tasnim-budget-1404-11-25",
            title="duplicate",
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()


def test_invalid_verification_status_rejected(db: Session) -> None:
    seed_tax_policy(db)
    db.flush()
    source = db.execute(
        select(TaxSourceModel).where(TaxSourceModel.source_key == "tasnim-budget-1404-11-25")
    ).scalar_one()
    db.add(
        TaxPolicyVersionModel(
            source_id=source.id,
            rule_key="bad_status_rule",
            rule_name="bad",
            version_id="1405",
            parameter_value=Decimal("1"),
            value_unit="percent",
            rule_year=1405,
            effective_from=FY1405_EFFECTIVE_FROM,
            verification_status="MADE_UP",
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()


def test_invalid_value_unit_rejected(db: Session) -> None:
    seed_tax_policy(db)
    db.flush()
    source = db.execute(
        select(TaxSourceModel).where(TaxSourceModel.source_key == "tasnim-budget-1404-11-25")
    ).scalar_one()
    db.add(
        TaxPolicyVersionModel(
            source_id=source.id,
            rule_key="bad_unit_rule",
            rule_name="bad",
            version_id="1405",
            parameter_value=Decimal("1"),
            value_unit="toman",
            rule_year=1405,
            effective_from=FY1405_EFFECTIVE_FROM,
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()


def test_inverted_effective_range_rejected(db: Session) -> None:
    seed_tax_policy(db)
    db.flush()
    source = db.execute(
        select(TaxSourceModel).where(TaxSourceModel.source_key == "tasnim-budget-1404-11-25")
    ).scalar_one()
    db.add(
        TaxPolicyVersionModel(
            source_id=source.id,
            rule_key="bad_range_rule",
            rule_name="bad",
            version_id="1405",
            parameter_value=Decimal("1"),
            value_unit="percent",
            rule_year=1405,
            effective_from=datetime.date(2026, 3, 21),
            effective_to=datetime.date(2026, 3, 20),
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()


def test_invalid_usage_context_rejected(db: Session) -> None:
    seed_tax_policy(db)
    db.flush()
    tenant = TenantModel(id=str(uuid.uuid4()), slug=f"tax-402-{uuid.uuid4().hex[:8]}", name="T")
    db.add(tenant)
    db.flush()
    db.add(
        TaxDecisionModel(
            tenant_id=tenant.id,
            rule_key="vat_general_rate",
            decision_date=datetime.date(2026, 9, 20),
            usage_context="whenever",
            version_id="1405",
            parameter_value=Decimal("10"),
            value_unit="percent",
            verification_status="CORROBORATED_CURRENT",
            source_key="tasnim-budget-1404-11-25",
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()


def test_decision_survives_version_deletion(db: Session) -> None:
    """Deleting a version SET NULLs the decision FK; the denormalized snapshot stays."""
    seed_tax_policy(db)
    db.flush()
    version = db.execute(
        select(TaxPolicyVersionModel).where(TaxPolicyVersionModel.rule_key == "vat_general_rate")
    ).scalar_one()
    tenant = TenantModel(id=str(uuid.uuid4()), slug=f"tax-402-{uuid.uuid4().hex[:8]}", name="T")
    db.add(tenant)
    db.flush()
    decision = TaxDecisionModel(
        tenant_id=tenant.id,
        rule_key="vat_general_rate",
        decision_date=datetime.date(2026, 9, 20),
        usage_context="reference",
        policy_version_id=version.id,
        version_id="1405",
        parameter_value=Decimal("10"),
        value_unit="percent",
        verification_status="CORROBORATED_CURRENT",
        source_key="tasnim-budget-1404-11-25",
    )
    db.add(decision)
    db.flush()
    db.delete(version)
    db.flush()
    db.refresh(decision)
    assert decision.policy_version_id is None
    assert decision.version_id == "1405"
    assert decision.parameter_value == Decimal("10")
    assert decision.value_unit == "percent"
    assert decision.verification_status == "CORROBORATED_CURRENT"
    assert decision.source_key == "tasnim-budget-1404-11-25"
