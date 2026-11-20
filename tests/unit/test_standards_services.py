"""Unit tests for the standards query + validation services (US-401).

Services run against the shared Supabase test DB with a transactional
session (rolled back after each test). The registry is seeded inside
the transaction, so tests never depend on rows committed by other
modules.
"""

from __future__ import annotations

import copy
import datetime
import os
import uuid
from collections.abc import Generator

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from hesabyar.application.commands.standards import StandardsValidationService
from hesabyar.application.errors import (
    StandardNotFoundError,
    StandardSourceBlockedError,
    ValidationRunNotFoundError,
)
from hesabyar.application.queries.standards import StandardsQueryService
from hesabyar.domain.standards.models import RuleId, RuleOutcome, VerificationStatus
from hesabyar.infrastructure.persistence.postgres.models import (
    Base,
    StandardRevisionModel,
    StandardRuleModel,
    StandardSourceModel,
    TenantModel,
    ValidationResultModel,
    ValidationRunModel,
)
from hesabyar.infrastructure.persistence.postgres.seed_standards import (
    seed_standards,
)
from hesabyar.infrastructure.persistence.postgres.standards_repository import (
    PostgresStandardsRegistry,
)
from tests.standards_fixtures import BALANCED_DATA

TEST_DB_URL = os.getenv(
    "HESABYAR_DATABASE_URL",
    "postgresql+psycopg://hesabyar:hesabyar_pass@hesabyar-postgres:5432/hesabyar_test",
)

# 1404/06/01 (a 1404 period) and 1405/01/01 (first blocked day for Standard 15).
PERIOD_1404 = datetime.date(2025, 8, 23)
PERIOD_1405_START = datetime.date(2026, 3, 21)


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
def tenant(db: Session) -> TenantModel:
    row = TenantModel(
        id=str(uuid.uuid4()),
        slug=f"std-401-{uuid.uuid4().hex[:8]}",
        name="Standards Test Tenant",
    )
    db.add(row)
    db.flush()
    return row


@pytest.fixture()
def seeded(db: Session) -> None:
    seed_standards(db)
    db.flush()


@pytest.fixture()
def queries(db: Session, seeded: None) -> StandardsQueryService:
    return StandardsQueryService(PostgresStandardsRegistry(db))


@pytest.fixture()
def validation(db: Session, seeded: None) -> StandardsValidationService:
    return StandardsValidationService(PostgresStandardsRegistry(db))


# ---------------------------------------------------------------------------
# Query service
# ---------------------------------------------------------------------------


def test_list_standards_returns_35_in_numeric_order(queries: StandardsQueryService) -> None:
    standards = queries.list_standards()
    assert len(standards) == 35
    assert [source.standard_code for source in standards] == [
        "1",
        "2",
        "4",
        "5",
        "8",
        "10",
        "11",
        "12",
        "13",
        "15",
        "16",
        "17",
        "18",
        "20",
        "22",
        "24",
        "25",
        "26",
        "27",
        "28",
        "30",
        "31",
        "32",
        "33",
        "34",
        "35",
        "36",
        "37",
        "38",
        "39",
        "40",
        "41",
        "42",
        "43",
        "44",
    ]


def test_list_standards_filters(queries: StandardsQueryService) -> None:
    verified = queries.list_standards(verification_status=VerificationStatus.VERIFIED_TEXT)
    assert [s.standard_code for s in verified] == ["43", "44"]
    blocked = queries.list_standards(verification_status=VerificationStatus.BLOCKED_SOURCE_GAP)
    assert [s.standard_code for s in blocked] == ["15"]
    phase_1 = queries.list_standards(phase=1)
    assert {s.standard_code for s in phase_1} == {"1", "2", "34", "35"}
    with_rules = queries.list_standards(has_rules=True)
    assert [s.standard_code for s in with_rules] == ["1"]
    without_rules = queries.list_standards(has_rules=False)
    assert len(without_rules) == 34


def test_get_standard_detail(queries: StandardsQueryService) -> None:
    detail = queries.get_standard("43")
    assert detail.source.standard_code == "43"
    assert detail.source.title.startswith("استاندارد حسابداری شماره 43")
    assert len(detail.source.revisions) == 1
    revision = detail.source.revisions[0]
    assert revision.revision_id == "1404"
    assert revision.verification_status == VerificationStatus.VERIFIED_TEXT
    assert revision.effective_from == datetime.date(2025, 3, 21)
    assert revision.sha256 is not None and len(revision.sha256) == 64
    assert detail.rules == ()


def test_get_standard_with_rules(queries: StandardsQueryService) -> None:
    detail = queries.get_standard("1")
    assert len(detail.rules) == 36
    assert detail.source.rules_count == 36
    by_id = {str(rule.rule_id): rule for rule in detail.rules}
    assert by_id[RuleId("BS001")].dsl_formula is not None
    assert by_id["CF005"].is_executable is False
    assert by_id["CF005"].dsl_formula is None
    assert by_id[RuleId("BS-W03")].severity.value == "warning"


def test_get_standard_unknown_raises(queries: StandardsQueryService) -> None:
    with pytest.raises(StandardNotFoundError, match="99"):
        queries.get_standard("99")


def test_coverage_counts_reported_separately(queries: StandardsQueryService) -> None:
    coverage = queries.coverage()
    assert coverage.corpus_standards == 35
    assert coverage.standards_with_rules == 1
    assert coverage.rules_implemented == 36
    assert coverage.rules_verified == 0
    assert coverage.rules_executable == 35


# ---------------------------------------------------------------------------
# Validation service — happy path + fail-closed
# ---------------------------------------------------------------------------


def test_create_run_balanced_data_passes(validation: StandardsValidationService, tenant: TenantModel) -> None:
    view = validation.create_run(
        tenant_id=tenant.id,
        period_start=PERIOD_1404,
        standard_codes=["1"],
        data=BALANCED_DATA,
    )
    assert view.overall_outcome == "pass"
    assert view.is_valid is True
    assert view.rules_executed == 35
    assert view.rules_passed == 35
    assert view.rules_failed == 0
    assert view.rules_not_applicable == 0
    assert view.rules_errored == 0
    assert view.ruleset_version == "1.0.0-legacy"
    assert view.coverage.corpus_standards == 35
    assert view.coverage.standards_with_rules == 1
    assert view.coverage.rules_implemented == 36
    assert view.coverage.rules_verified == 0
    assert view.coverage.rules_executed == 35
    assert [item.rule_id for item in view.not_executed] == ["CF005"]
    assert len(view.results) == 35
    by_id = {result.rule_id: result for result in view.results}
    assert by_id[RuleId("BS001")].outcome == RuleOutcome.PASS
    assert by_id[RuleId("BS-W03")].outcome == RuleOutcome.PASS
    assert by_id[RuleId("XC005")].outcome == RuleOutcome.PASS


def test_create_run_persists_run_and_results(
    validation: StandardsValidationService,
    tenant: TenantModel,
    db: Session,
) -> None:
    view = validation.create_run(tenant_id=tenant.id, period_start=PERIOD_1404, data=BALANCED_DATA)
    db.flush()
    run = db.execute(select(ValidationRunModel).where(ValidationRunModel.id == view.run_id)).scalar_one()
    assert run.tenant_id == tenant.id
    assert run.overall_outcome == "pass"
    assert run.input_hash is not None and len(run.input_hash) == 64
    assert run.standard_codes == [str(n) for n in range(1, 45) if n not in (3, 6, 7, 9, 14, 19, 21, 23, 29)]
    results = db.execute(select(ValidationResultModel).where(ValidationResultModel.run_id == run.id)).scalars().all()
    assert len(results) == 35
    assert {result.outcome for result in results} == {"pass"}


def test_create_run_unbalanced_data_fails(validation: StandardsValidationService, tenant: TenantModel) -> None:
    data = copy.deepcopy(BALANCED_DATA)
    data["balance_sheet"]["total_assets"] = 999
    view = validation.create_run(
        tenant_id=tenant.id,
        period_start=PERIOD_1404,
        standard_codes=["1"],
        data=data,
    )
    assert view.overall_outcome == "fail"
    assert view.is_valid is False
    # total_assets 999 breaks both the accounting equation (BS001) and the
    # current/non-current split (BS002).
    assert view.rules_failed == 2
    by_id = {result.rule_id: result for result in view.results}
    assert by_id[RuleId("BS001")].outcome == RuleOutcome.FAIL
    assert by_id[RuleId("BS002")].outcome == RuleOutcome.FAIL
    assert by_id[RuleId("IS001")].outcome == RuleOutcome.PASS


def test_create_run_missing_data_errors_never_passes(
    validation: StandardsValidationService, tenant: TenantModel
) -> None:
    view = validation.create_run(
        tenant_id=tenant.id,
        period_start=PERIOD_1404,
        standard_codes=["1"],
        data={},
    )
    assert view.overall_outcome == "error"
    assert view.is_valid is False
    assert view.rules_executed == 35
    assert view.rules_errored == 35
    assert view.rules_passed == 0
    assert all(result.outcome == RuleOutcome.ERROR for result in view.results)


def test_create_run_float_inputs_are_normalized_not_errors(
    validation: StandardsValidationService, tenant: TenantModel
) -> None:
    """JSON floats become exact Decimals/ints at the boundary (no float ERRORs)."""
    data = copy.deepcopy(BALANCED_DATA)
    data["balance_sheet"]["total_assets"] = 1000.0
    data["balance_sheet"]["total_liabilities"] = 600.0
    data["income_statement"]["revenue"] = 500.5  # non-integral float
    view = validation.create_run(
        tenant_id=tenant.id,
        period_start=PERIOD_1404,
        standard_codes=["1"],
        data=data,
    )
    # revenue 500.5 breaks IS002/IS003 (gross profit no longer matches),
    # but no rule errors merely because a float was supplied.
    assert view.rules_errored == 0
    by_id = {result.rule_id: result for result in view.results}
    assert by_id[RuleId("BS001")].outcome == RuleOutcome.PASS
    assert by_id[RuleId("IS003")].outcome == RuleOutcome.FAIL


def test_create_run_unknown_standard_raises(validation: StandardsValidationService, tenant: TenantModel) -> None:
    with pytest.raises(StandardNotFoundError, match="99"):
        validation.create_run(tenant_id=tenant.id, standard_codes=["99"], data=BALANCED_DATA)


def test_create_run_bad_period_range_raises(validation: StandardsValidationService, tenant: TenantModel) -> None:
    with pytest.raises(ValueError, match="period_end must be on or after"):
        validation.create_run(
            tenant_id=tenant.id,
            period_start=datetime.date(2025, 8, 23),
            period_end=datetime.date(2025, 8, 22),
            data=BALANCED_DATA,
        )


def test_input_hash_is_deterministic(validation: StandardsValidationService, tenant: TenantModel, db: Session) -> None:
    first = validation.create_run(tenant_id=tenant.id, period_start=PERIOD_1404, data=BALANCED_DATA)
    second = validation.create_run(tenant_id=tenant.id, period_start=PERIOD_1404, data=BALANCED_DATA)
    assert first.run_id != second.run_id
    run_rows = (
        db.execute(select(ValidationRunModel).where(ValidationRunModel.id.in_([first.run_id, second.run_id])))
        .scalars()
        .all()
    )
    assert len(run_rows) == 2
    assert run_rows[0].input_hash == run_rows[1].input_hash


# ---------------------------------------------------------------------------
# Standard 15 gate + effective-date applicability
# ---------------------------------------------------------------------------


def test_standard_15_blocked_for_1405_period(validation: StandardsValidationService, tenant: TenantModel) -> None:
    with pytest.raises(StandardSourceBlockedError) as excinfo:
        validation.create_run(
            tenant_id=tenant.id,
            period_start=PERIOD_1405_START,
            standard_codes=["15"],
            data=BALANCED_DATA,
        )
    assert excinfo.value.details["standard_code"] == "15"
    assert excinfo.value.code == "STANDARD_SOURCE_BLOCKED"


def test_standard_15_blocked_without_period(validation: StandardsValidationService, tenant: TenantModel) -> None:
    """No period means applicability cannot be established — fail closed."""
    with pytest.raises(StandardSourceBlockedError):
        validation.create_run(tenant_id=tenant.id, standard_codes=["15"], data=BALANCED_DATA)


def test_standard_15_allowed_for_historical_period(validation: StandardsValidationService, tenant: TenantModel) -> None:
    view = validation.create_run(
        tenant_id=tenant.id,
        period_start=PERIOD_1404,
        standard_codes=["15"],
        data=BALANCED_DATA,
    )
    assert view.overall_outcome == "not_applicable"
    assert view.rules_executed == 0
    assert view.results == ()


def test_standard_15_blocked_even_when_mixed_with_standard_1(
    validation: StandardsValidationService, tenant: TenantModel
) -> None:
    with pytest.raises(StandardSourceBlockedError):
        validation.create_run(
            tenant_id=tenant.id,
            period_start=PERIOD_1405_START,
            standard_codes=["1", "15"],
            data=BALANCED_DATA,
        )


def test_effective_date_makes_rule_not_applicable(
    validation: StandardsValidationService,
    tenant: TenantModel,
    db: Session,
) -> None:
    """A rule of a revision effective from 1405/01/01 is NA for a 1404 period."""
    revision = db.execute(
        select(StandardRevisionModel)
        .join(
            StandardSourceModel,
            StandardRevisionModel.source_id == StandardSourceModel.id,
        )
        .where(StandardSourceModel.standard_code == "44")
    ).scalar_one()
    db.add(
        StandardRuleModel(
            id=str(uuid.uuid4()),
            revision_id=revision.id,
            rule_id="ZZ001",
            category="cross_checks",
            description="Test-only rule for the 1405-effective Standard 44 revision",
            dsl_formula="1 <= 1",
            rule_type="equation",
            severity="error",
            is_executable=True,
            ruleset_version="1.0.0-legacy",
        )
    )
    db.flush()
    view_1404 = validation.create_run(
        tenant_id=tenant.id,
        period_start=PERIOD_1404,
        standard_codes=["44"],
        data=BALANCED_DATA,
    )
    assert view_1404.rules_executed == 0
    assert view_1404.rules_not_applicable == 1
    assert view_1404.overall_outcome == "not_applicable"
    assert view_1404.results[0].rule_id == "ZZ001"
    assert view_1404.results[0].outcome == RuleOutcome.NOT_APPLICABLE
    view_1405 = validation.create_run(
        tenant_id=tenant.id,
        period_start=PERIOD_1405_START,
        standard_codes=["44"],
        data=BALANCED_DATA,
    )
    assert view_1405.rules_executed == 1
    assert view_1405.rules_passed == 1
    assert view_1405.overall_outcome == "pass"


# ---------------------------------------------------------------------------
# get_run
# ---------------------------------------------------------------------------


def test_get_run_round_trip(validation: StandardsValidationService, tenant: TenantModel) -> None:
    created = validation.create_run(
        tenant_id=tenant.id,
        period_start=PERIOD_1404,
        standard_codes=["1"],
        data=BALANCED_DATA,
    )
    fetched = validation.get_run(created.run_id, tenant.id)
    assert fetched == created


def test_get_run_unknown_raises(validation: StandardsValidationService, tenant: TenantModel) -> None:
    with pytest.raises(ValidationRunNotFoundError):
        validation.get_run(str(uuid.uuid4()), tenant.id)


def test_get_run_other_tenant_looks_missing(
    validation: StandardsValidationService,
    tenant: TenantModel,
    db: Session,
) -> None:
    created = validation.create_run(tenant_id=tenant.id, period_start=PERIOD_1404, data=BALANCED_DATA)
    other_tenant_id = str(uuid.uuid4())
    with pytest.raises(ValidationRunNotFoundError):
        validation.get_run(created.run_id, other_tenant_id)
