"""Tests for the SQLAlchemy tax-policy repository (US-402).

Runs against the shared Supabase test DB with a transactional session
(rolled back after each test). The registry is seeded inside the
transaction, so tests never depend on rows committed by other modules.
The services themselves are tested DB-free against an in-memory fake
(tests/unit/test_tax_policy_services.py); this module verifies the
repository implementation behind the port.
"""

from __future__ import annotations

import datetime
import os
import uuid
from collections.abc import Generator
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from hesabyar.domain.standards.models import VerificationStatus
from hesabyar.domain.tax_policy.commands import RecordTaxDecision
from hesabyar.domain.tax_policy.models import RuleKey, UsageContext, ValueUnit
from hesabyar.infrastructure.persistence.postgres.models import (
    Base,
    TaxPolicyVersionModel,
    TaxSourceModel,
    TenantModel,
)
from hesabyar.infrastructure.persistence.postgres.seed_tax_policy import (
    FY1405_EFFECTIVE_FROM,
    FY1405_EFFECTIVE_TO,
    seed_tax_policy,
)
from hesabyar.infrastructure.persistence.postgres.tax_policy_repository import (
    SqlAlchemyTaxPolicyRepository,
)

TEST_DB_URL = os.getenv(
    "HESABYAR_DATABASE_URL",
    "postgresql+psycopg://hesabyar:hesabyar_pass@hesabyar-postgres:5432/hesabyar_test",
)

MID_1405 = datetime.date(2026, 9, 20)
DAY_BEFORE_1405 = FY1405_EFFECTIVE_FROM - datetime.timedelta(days=1)


@pytest.fixture(scope="module")
def engine() -> Generator[Engine, None, None]:
    eng = create_engine(TEST_DB_URL, echo=False)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def db(engine: Engine) -> Generator[Session, None, None]:
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


@pytest.fixture()
def repository(db: Session) -> SqlAlchemyTaxPolicyRepository:
    seed_tax_policy(db)
    db.flush()
    return SqlAlchemyTaxPolicyRepository(db)


@pytest.fixture()
def tenant_id(db: Session) -> str:
    """A real tenant row: tax_decisions.tenant_id is an FK to tenants."""
    row = TenantModel(
        id=str(uuid.uuid4()),
        slug=f"tax-repo-402-{uuid.uuid4().hex[:8]}",
        name="Tax Repository Test Tenant",
    )
    db.add(row)
    db.flush()
    return row.id


def _add_version(
    db: Session,
    *,
    rule_key: str,
    version_id: str,
    parameter_value: Decimal,
    verification_status: str,
    status: str = "active",
    effective_from: datetime.date = FY1405_EFFECTIVE_FROM,
    effective_to: datetime.date | None = FY1405_EFFECTIVE_TO,
) -> None:
    from sqlalchemy import select

    source = db.execute(
        select(TaxSourceModel).where(TaxSourceModel.source_key == "tasnim-budget-1404-11-25")
    ).scalar_one()
    db.add(
        TaxPolicyVersionModel(
            source_id=source.id,
            rule_key=rule_key,
            rule_name="قاعده آزمایشی",
            version_id=version_id,
            parameter_value=parameter_value,
            value_unit="percent",
            rule_year=1405,
            performance_year=1405,
            filing_year=1405,
            effective_from=effective_from,
            effective_to=effective_to,
            status=status,
            verification_status=verification_status,
        )
    )
    db.flush()


def test_list_sources(repository: SqlAlchemyTaxPolicyRepository) -> None:
    sources = repository.list_sources()
    assert [s.source_key for s in sources] == [
        "tasnim-art100-announcement-1405-02-28",
        "tasnim-art100-implementation-1405-06-12",
        "tasnim-budget-1404-11-25",
    ]
    assert [s.versions_count for s in sources] == [0, 1, 4]


def test_get_source_returns_none_for_unknown_key(repository: SqlAlchemyTaxPolicyRepository) -> None:
    assert repository.get_source("no-such-source") is None


def test_get_source_returns_versions(repository: SqlAlchemyTaxPolicyRepository) -> None:
    detail = repository.get_source("tasnim-budget-1404-11-25")
    assert detail is not None
    assert [v.rule_key for v in detail.versions] == [
        "art101_exemption",
        "art84_salary_exemption",
        "growth_threshold_art105_131",
        "vat_general_rate",
    ]


def test_list_versions_filters(repository: SqlAlchemyTaxPolicyRepository) -> None:
    assert len(repository.list_versions()) == 5
    assert len(repository.list_versions(rule_key="vat_general_rate")) == 1
    assert len(repository.list_versions(verification_status=VerificationStatus.VERIFIED_TEXT)) == 0
    assert len(repository.list_versions(effective_on=MID_1405)) == 5
    assert repository.list_versions(effective_on=DAY_BEFORE_1405) == []
    assert {v.rule_key for v in repository.list_versions(performance_year=1404)} == {
        "art100_flat_tax_ceiling",
        "growth_threshold_art105_131",
    }


def test_get_version_returns_none_for_unknown_id(repository: SqlAlchemyTaxPolicyRepository) -> None:
    assert repository.get_version(str(uuid.uuid4())) is None


def test_get_version_returns_version_with_source(repository: SqlAlchemyTaxPolicyRepository) -> None:
    version = repository.list_versions(rule_key="art100_flat_tax_ceiling")[0]
    detail = repository.get_version(version.id)
    assert detail is not None
    assert detail.version.parameter_value == Decimal("720000000000")
    assert detail.version.performance_year == 1404
    assert detail.source.source_key == "tasnim-art100-implementation-1405-06-12"


def test_find_active_version_boundaries(repository: SqlAlchemyTaxPolicyRepository) -> None:
    assert repository.find_active_version("vat_general_rate", FY1405_EFFECTIVE_FROM) is not None
    assert repository.find_active_version("vat_general_rate", FY1405_EFFECTIVE_TO) is not None
    assert repository.find_active_version("vat_general_rate", DAY_BEFORE_1405) is None
    assert repository.find_active_version("vat_general_rate", datetime.date(2028, 1, 1)) is None
    assert repository.find_active_version("no_such_rule", MID_1405) is None


def test_find_active_version_prefers_newest_effective(db: Session, repository: SqlAlchemyTaxPolicyRepository) -> None:
    _add_version(
        db,
        rule_key="vat_general_rate",
        version_id="1405-rev2",
        parameter_value=Decimal("11"),
        verification_status="VERIFIED_TEXT",
        effective_from=datetime.date(2026, 9, 1),
    )
    early = repository.find_active_version("vat_general_rate", datetime.date(2026, 6, 1))
    assert early is not None and early.version_id == "1405"
    late = repository.find_active_version("vat_general_rate", MID_1405)
    assert late is not None and late.version_id == "1405-rev2"
    assert late.parameter_value == Decimal("11")


def test_find_active_version_ignores_superseded(db: Session, repository: SqlAlchemyTaxPolicyRepository) -> None:
    _add_version(
        db,
        rule_key="vat_general_rate",
        version_id="1405-superseded",
        parameter_value=Decimal("99"),
        verification_status="VERIFIED_TEXT",
        status="superseded",
    )
    found = repository.find_active_version("vat_general_rate", MID_1405)
    assert found is not None and found.version_id == "1405"


def test_save_and_get_decision(repository: SqlAlchemyTaxPolicyRepository, tenant_id: str) -> None:
    version = repository.find_active_version("vat_general_rate", MID_1405)
    assert version is not None
    saved = repository.save_decision(
        RecordTaxDecision(
            tenant_id=tenant_id,
            company_id="company-1",
            rule_key=RuleKey("vat_general_rate"),
            decision_date=MID_1405,
            usage_context=UsageContext.REFERENCE,
            policy_version_id=version.id,
            version_id=version.version_id,
            parameter_value=version.parameter_value,
            value_unit=ValueUnit.PERCENT,
            verification_status=VerificationStatus.CORROBORATED_CURRENT,
            source_key=version.source_key,
        )
    )
    assert saved.id
    assert saved.created_at is not None
    fetched = repository.get_decision(saved.id, tenant_id)
    assert fetched is not None
    assert fetched == saved


def test_get_decision_returns_none_for_other_tenant(repository: SqlAlchemyTaxPolicyRepository, tenant_id: str) -> None:
    version = repository.find_active_version("vat_general_rate", MID_1405)
    assert version is not None
    saved = repository.save_decision(
        RecordTaxDecision(
            tenant_id=tenant_id,
            company_id=None,
            rule_key=RuleKey("vat_general_rate"),
            decision_date=MID_1405,
            usage_context=UsageContext.REFERENCE,
            policy_version_id=version.id,
            version_id=version.version_id,
            parameter_value=version.parameter_value,
            value_unit=ValueUnit.PERCENT,
            verification_status=VerificationStatus.CORROBORATED_CURRENT,
            source_key=version.source_key,
        )
    )
    assert repository.get_decision(saved.id, str(uuid.uuid4())) is None
    assert repository.get_decision(str(uuid.uuid4()), tenant_id) is None
