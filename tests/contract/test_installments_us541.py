"""Contract tests for US-541 (Phase 5-5 / ۵ج): installment finance.

The REAL stack (InstallmentCommandService + InstallmentFinanceService
+ InstallmentQueryService + SqlAlchemyInstallmentRepository, and the
REAL review-queue command service over SqlAlchemyReviewQueueRepository
for the dunning sweep) against the LIVE HesabYar PostgreSQL database.
No Tryton involvement — the finance service is wired WITHOUT a
journal service here, so late fees are assessed and recorded
unjournaled (the journal path is covered by the unit tests with a
fake journal port).

Each test uses a FRESH tenant (a real ``tenants`` row, so the
review-item FK holds) + installation id and a UNIQUE invoice id.
Teardown deletes the test's rows (assessments → amendments →
allocations → receipts → legs → plans → contracts, then the tenant
row — which CASCADE-deletes its review items + audit entries).

Configuration (all optional):
  HESABYAR_DATABASE_URL (the HesabYar DB; CI sets it, local runs
                         source hesabyar-reference/.env.local-db)

When the DB is not reachable, every test SKIPS cleanly with a clear
message. Dates are far-past (1400-01-01 = 2021-03-21: every leg
overdue on any run date) or far-future (1410-01-01 = 2031-03-21:
never due), so statuses are deterministic.
"""

from __future__ import annotations

import datetime
import os
import uuid
from collections.abc import Generator

import jdatetime
import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from hesabyar.application.commands.installments import InstallmentCommandService
from hesabyar.application.commands.installments_finance import (
    InstallmentFinanceService,
)
from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.application.queries.installments import InstallmentQueryService
from hesabyar.domain.installments.errors import InstallmentSettlementAmountError
from hesabyar.domain.installments.models import LegStatus, PlanStatus
from hesabyar.domain.review_queue.models import ReviewItemType
from hesabyar.domain.review_queue.ports import ReviewItemFilters
from hesabyar.infrastructure.persistence.postgres.installments_repository import (
    SqlAlchemyInstallmentRepository,
)
from hesabyar.infrastructure.persistence.postgres.models import (
    Base,
    InstallmentLateFeeAssessmentModel,
    InstallmentLegModel,
    InstallmentPlanModel,
    InstallmentReceiptAllocationModel,
    InstallmentReceiptModel,
    InstallmentSaleContractModel,
    InstallmentScheduleAmendmentModel,
    TenantModel,
)
from hesabyar.infrastructure.persistence.postgres.review_queue_repository import (
    SqlAlchemyReviewQueueRepository,
)

HESABYAR_DB_URL = os.environ.get("HESABYAR_DATABASE_URL", "")

COMPANY_ENGINE_ID = 541

#: Far-past / far-future Jalali dates (always overdue / always expected).
PAST_JALALI = "1400-01-01"
FUTURE_JALALI = "1410-01-01"


def _unique_invoice_id() -> str:
    return f"INV-US541-{uuid.uuid4().hex[:12]}"


def _installation_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture(scope="module")
def hesabyar_engine() -> Generator[Engine, None, None]:
    if not HESABYAR_DB_URL:
        pytest.skip("HESABYAR_DATABASE_URL is not set.")
    eng = create_engine(HESABYAR_DB_URL, echo=False)
    try:
        with eng.connect():
            pass
    except Exception:
        eng.dispose()
        pytest.skip("HesabYar DB is not reachable.")
    # Creates missing tables only — never alters existing ones.
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(hesabyar_engine: Engine) -> Generator[Session, None, None]:
    factory = sessionmaker(bind=hesabyar_engine)
    sess = factory()
    try:
        yield sess
    finally:
        sess.rollback()
        sess.close()


@pytest.fixture()
def tenant(hesabyar_engine: Engine) -> Generator[str, None, None]:
    """A real tenant row (the review-item FK requires one)."""
    tenant_id = str(uuid.uuid4())
    setup = sessionmaker(bind=hesabyar_engine)()
    try:
        setup.add(
            TenantModel(
                id=tenant_id,
                slug=f"contract-541-{uuid.uuid4().hex[:8]}",
                name="541 Contract Tenant",
            )
        )
        setup.commit()
    finally:
        setup.close()
    yield tenant_id


@pytest.fixture()
def installation() -> str:
    return _installation_id()


@pytest.fixture()
def services(
    session: Session,
) -> tuple[InstallmentCommandService, InstallmentFinanceService, InstallmentQueryService]:
    repo = SqlAlchemyInstallmentRepository(session)
    review_repo = SqlAlchemyReviewQueueRepository(session)
    review = ReviewQueueCommandService(review_repo, review_repo)
    finance = InstallmentFinanceService(repo, review=review, queue=review_repo)
    return InstallmentCommandService(repo), finance, InstallmentQueryService(repo)


@pytest.fixture()
def _cleanup(
    hesabyar_engine: Engine, tenant: str, installation: str
) -> Generator[None, None, None]:
    yield
    # A FRESH session for cleanup (the US-533 pattern): the test's
    # own session may already be closed by teardown time. Reverse FK
    # order; installment rows scoped to this test's installation,
    # the tenant row deleted last (CASCADE removes its review items
    # + audit entries).
    cleanup = sessionmaker(bind=hesabyar_engine)()
    try:
        cleanup.execute(
            delete(InstallmentLateFeeAssessmentModel).where(
                InstallmentLateFeeAssessmentModel.installation_id == installation
            )
        )
        cleanup.execute(
            delete(InstallmentScheduleAmendmentModel).where(
                InstallmentScheduleAmendmentModel.installation_id == installation
            )
        )
        cleanup.execute(
            delete(InstallmentReceiptAllocationModel).where(
                InstallmentReceiptAllocationModel.receipt_id.in_(
                    select(InstallmentReceiptModel.id).where(
                        InstallmentReceiptModel.installation_id == installation
                    )
                )
            )
        )
        cleanup.execute(
            delete(InstallmentReceiptModel).where(
                InstallmentReceiptModel.installation_id == installation
            )
        )
        cleanup.execute(
            delete(InstallmentLegModel).where(
                InstallmentLegModel.plan_id.in_(
                    select(InstallmentPlanModel.id).where(
                        InstallmentPlanModel.installation_id == installation
                    )
                )
            )
        )
        cleanup.execute(
            delete(InstallmentPlanModel).where(
                InstallmentPlanModel.installation_id == installation
            )
        )
        cleanup.execute(
            delete(InstallmentSaleContractModel).where(
                InstallmentSaleContractModel.installation_id == installation
            )
        )
        cleanup.execute(delete(TenantModel).where(TenantModel.id == tenant))
        cleanup.commit()
    finally:
        cleanup.close()


def _contract_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "code": f"US541-{uuid.uuid4().hex[:8]}",
        "name": "قرارداد تست",
        "version": 1,
        "effective_from": "1400-01-01",
        "late_fee_basis": "daily_percent",
        "late_fee_rate": "0.001",
        "late_fee_grace_days": 0,
        "penalty_account_engine_id": 77,
        "penalty_account_code": "6.1.2",
        "receivable_account_engine_id": 55,
        "receivable_account_code": "1.1.3",
        "finance_income_rate": "0.10",
        "dunning_offsets": [3, 7, 14],
        "early_settlement_fee_policy": "kept",
        "early_settlement_income_policy": "full",
    }
    payload.update(overrides)
    return payload


def _plan_payload(invoice_id: str, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "invoice_id": invoice_id,
        "legs": [
            {"due_date": PAST_JALALI, "principal_toman": 1_000_000},
            {"due_date": PAST_JALALI, "principal_toman": 1_000_000},
            {"due_date": FUTURE_JALALI, "principal_toman": 1_000_000},
        ],
    }
    payload.update(overrides)
    return payload


def _settlement_payload(invoice_id: str, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "invoice_id": invoice_id,
        "journal_move_id": 4242,
        "amount_toman": 3_300_000,
        "settlement_type": "full",
        "payment_method": "cash",
    }
    payload.update(overrides)
    return payload


# -- contract finance terms round-trip -------------------------------------------


@pytest.mark.usefixtures("_cleanup")
def test_contract_finance_terms_round_trip_on_real_db(
    session: Session,
    services: tuple[InstallmentCommandService, InstallmentFinanceService, InstallmentQueryService],
    tenant: str,
    installation: str,
) -> None:
    commands, _, queries = services
    contract = commands.create_contract(
        tenant_id=tenant,
        installation_id=installation,
        company_engine_id=COMPANY_ENGINE_ID,
        payload=_contract_payload(
            early_settlement_income_policy="discounted",
            early_settlement_discount_rate="0.20",
        ),
    )
    session.commit()
    found = queries.get_contract(contract.id, tenant_id=tenant, installation_id=installation)
    assert str(found.finance_income_rate) == "0.10"
    assert found.dunning_offsets == (3, 7, 14)
    assert found.receivable_account_engine_id == 55
    assert found.receivable_account_code == "1.1.3"
    assert str(found.early_settlement_discount_rate) == "0.20"


@pytest.mark.usefixtures("_cleanup")
def test_create_plan_by_code_selects_the_version_in_force_on_real_db(
    session: Session,
    services: tuple[InstallmentCommandService, InstallmentFinanceService, InstallmentQueryService],
    tenant: str,
    installation: str,
) -> None:
    commands, _, queries = services
    code = f"US541-VER-{uuid.uuid4().hex[:8]}"
    commands.create_contract(
        tenant_id=tenant,
        installation_id=installation,
        company_engine_id=COMPANY_ENGINE_ID,
        payload=_contract_payload(code=code, version=1, effective_from="1400-01-01"),
    )
    v2 = commands.create_contract(
        tenant_id=tenant,
        installation_id=installation,
        company_engine_id=COMPANY_ENGINE_ID,
        payload=_contract_payload(code=code, version=2, effective_from="1400-06-01"),
    )
    session.commit()
    invoice_id = _unique_invoice_id()
    plan = commands.create_plan(
        tenant_id=tenant,
        installation_id=installation,
        company_engine_id=COMPANY_ENGINE_ID,
        payload=_plan_payload(invoice_id, contract_code=code),
    )
    session.commit()
    assert plan.contract_id == v2.id
    found = queries.get_effective_contract(
        code, tenant_id=tenant, installation_id=installation
    )
    assert found.id == v2.id


# -- payoff quote + full settlement --------------------------------------------------


@pytest.mark.usefixtures("_cleanup")
def test_payoff_quote_and_full_settlement_on_real_db(
    session: Session,
    services: tuple[InstallmentCommandService, InstallmentFinanceService, InstallmentQueryService],
    tenant: str,
    installation: str,
) -> None:
    commands, finance, queries = services
    contract = commands.create_contract(
        tenant_id=tenant,
        installation_id=installation,
        company_engine_id=COMPANY_ENGINE_ID,
        payload=_contract_payload(),
    )
    invoice_id = _unique_invoice_id()
    plan = commands.create_plan(
        tenant_id=tenant,
        installation_id=installation,
        company_engine_id=COMPANY_ENGINE_ID,
        payload=_plan_payload(invoice_id, contract_id=contract.id),
    )
    session.commit()

    # The quote as of today: 30,000,000 principal + 3,000,000 income
    # + the two overdue legs' fees (days depend on the run date, so
    # the fee is asserted as > 0 and the payoff as principal +
    # income + fees, not as a fixed number). One date is pinned for
    # both the quote and the settlement, so a midnight boundary
    # between the two calls cannot move the fee.
    today = datetime.date.today()
    today_jalali = jdatetime.date.fromgregorian(date=today).strftime("%Y-%m-%d")
    quote = queries.get_payoff_quote(
        plan.id, tenant_id=tenant, installation_id=installation, today=today
    )
    assert quote.remaining_principal.amount == 30_000_000
    assert quote.income_due.amount == 3_000_000
    assert quote.late_fees_accrued.amount > 0
    assert quote.late_fees_due.amount == quote.late_fees_accrued.amount
    assert (
        quote.payoff_total.amount
        == 33_000_000 + quote.late_fees_accrued.amount
    )

    # Settle for exactly the quote (converted back to Toman).
    result = finance.settle_early(
        tenant_id=tenant,
        installation_id=installation,
        plan_id=plan.id,
        payload=_settlement_payload(
            invoice_id,
            amount_toman=quote.payoff_total.amount // 10,
            settlement_date=today_jalali,
        ),
    )
    session.commit()
    assert result.receipt.allocated_irr == 30_000_000
    assert result.receipt.finance_income.amount == 3_000_000
    assert result.receipt.late_fee.amount == quote.late_fees_accrued.amount
    assert result.receipt.is_early_settlement is True

    detail = queries.get_plan_detail(
        plan.id, tenant_id=tenant, installation_id=installation
    )
    assert detail.status is PlanStatus.PAID
    assert [leg.status for leg in detail.legs] == [
        LegStatus.PAID,
        LegStatus.PAID,
        LegStatus.PAID,
    ]


@pytest.mark.usefixtures("_cleanup")
def test_settlement_below_the_payoff_is_refused_on_real_db(
    session: Session,
    services: tuple[InstallmentCommandService, InstallmentFinanceService, InstallmentQueryService],
    tenant: str,
    installation: str,
) -> None:
    commands, finance, queries = services
    contract = commands.create_contract(
        tenant_id=tenant,
        installation_id=installation,
        company_engine_id=COMPANY_ENGINE_ID,
        payload=_contract_payload(),
    )
    invoice_id = _unique_invoice_id()
    plan = commands.create_plan(
        tenant_id=tenant,
        installation_id=installation,
        company_engine_id=COMPANY_ENGINE_ID,
        payload=_plan_payload(invoice_id, contract_id=contract.id),
    )
    session.commit()
    with pytest.raises(InstallmentSettlementAmountError):
        finance.settle_early(
            tenant_id=tenant,
            installation_id=installation,
            plan_id=plan.id,
            payload=_settlement_payload(invoice_id, amount_toman=1_000_000),
        )
    session.rollback()
    detail = queries.get_plan_detail(
        plan.id, tenant_id=tenant, installation_id=installation
    )
    assert detail.receipts == []


# -- partial settlement + re-amortisation ---------------------------------------------


@pytest.mark.usefixtures("_cleanup")
def test_partial_settlement_reamortises_on_real_db(
    session: Session,
    services: tuple[InstallmentCommandService, InstallmentFinanceService, InstallmentQueryService],
    tenant: str,
    installation: str,
) -> None:
    commands, finance, queries = services
    contract = commands.create_contract(
        tenant_id=tenant,
        installation_id=installation,
        company_engine_id=COMPANY_ENGINE_ID,
        payload=_contract_payload(
            early_settlement_fee_policy="waived",
            early_settlement_income_policy="waived",
        ),
    )
    invoice_id = _unique_invoice_id()
    plan = commands.create_plan(
        tenant_id=tenant,
        installation_id=installation,
        company_engine_id=COMPANY_ENGINE_ID,
        payload=_plan_payload(invoice_id, contract_id=contract.id),
    )
    session.commit()
    # Pay 25,000,000: the two overdue legs (20M) + 5M advance, which
    # reduces the future leg from 10M to 5M.
    result = finance.settle_early(
        tenant_id=tenant,
        installation_id=installation,
        plan_id=plan.id,
        payload=_settlement_payload(
            invoice_id, amount_toman=2_500_000, settlement_type="partial"
        ),
    )
    session.commit()
    assert result.amendment is not None
    assert result.amendment.applied_advance.amount == 5_000_000
    detail = queries.get_plan_detail(
        plan.id, tenant_id=tenant, installation_id=installation
    )
    assert [leg.leg.principal.amount for leg in detail.legs] == [
        10_000_000,
        10_000_000,
        5_000_000,
    ]
    assert detail.outstanding.amount == 5_000_000
    # The amendment row persists and is readable back.
    repo = SqlAlchemyInstallmentRepository(session)
    amendments = repo.list_schedule_amendments(
        plan.id, tenant_id=tenant, installation_id=installation
    )
    assert len(amendments) == 1
    assert amendments[0].receipt_id == result.receipt.id
    assert amendments[0].legs_after[0].principal.amount == 5_000_000


# -- overdue report ----------------------------------------------------------------------


@pytest.mark.usefixtures("_cleanup")
def test_overdue_report_on_real_db(
    session: Session,
    services: tuple[InstallmentCommandService, InstallmentFinanceService, InstallmentQueryService],
    tenant: str,
    installation: str,
) -> None:
    commands, _, queries = services
    contract = commands.create_contract(
        tenant_id=tenant,
        installation_id=installation,
        company_engine_id=COMPANY_ENGINE_ID,
        payload=_contract_payload(),
    )
    invoice_id = _unique_invoice_id()
    plan = commands.create_plan(
        tenant_id=tenant,
        installation_id=installation,
        company_engine_id=COMPANY_ENGINE_ID,
        payload=_plan_payload(invoice_id, contract_id=contract.id),
    )
    session.commit()
    report = queries.list_overdue(
        tenant_id=tenant, installation_id=installation, plan_id=plan.id
    )
    assert report.total_rows == 2  # the two far-past legs
    assert all(row.dunning_stage == 3 for row in report.rows)
    assert all(row.late_fee_accrued.amount > 0 for row in report.rows)
    assert report.total_balance.amount == 20_000_000
    assert report.rows[0].contract_code == contract.code
    assert report.rows[0].invoice_id == invoice_id


# -- dunning sweep (real review queue) ------------------------------------------------------


@pytest.mark.usefixtures("_cleanup")
def test_dunning_run_files_review_items_and_assesses_fees_on_real_db(
    session: Session,
    services: tuple[InstallmentCommandService, InstallmentFinanceService, InstallmentQueryService],
    tenant: str,
    installation: str,
) -> None:
    commands, finance, _ = services
    contract = commands.create_contract(
        tenant_id=tenant,
        installation_id=installation,
        company_engine_id=COMPANY_ENGINE_ID,
        payload=_contract_payload(),
    )
    invoice_id = _unique_invoice_id()
    plan = commands.create_plan(
        tenant_id=tenant,
        installation_id=installation,
        company_engine_id=COMPANY_ENGINE_ID,
        payload=_plan_payload(invoice_id, contract_id=contract.id),
    )
    session.commit()

    result = finance.run_dunning(
        tenant_id=tenant, installation_id=installation, today=datetime.date.today()
    )
    session.commit()
    assert result.plans_scanned == 1
    assert result.overdue_legs == 2
    assert len(result.items_filed) == 2
    assert result.fees_assessed_irr > 0
    assert result.journals_created == ()  # no engine wired in contract tests

    # The items are real review-queue rows of the right type.
    review_repo = SqlAlchemyReviewQueueRepository(session)
    items = review_repo.list_items(
        tenant, ReviewItemFilters(item_type=ReviewItemType.INSTALLMENT_OVERDUE)
    )
    assert len(items) == 2
    assert {item.id for item in items} == set(result.items_filed)
    assert all(item.severity.value == "critical" for item in items)

    # The fee assessments persist (unjournaled: no engine here).
    repo = SqlAlchemyInstallmentRepository(session)
    legs = repo.list_legs(plan.id, tenant_id=tenant, installation_id=installation)
    assessments = repo.list_late_fee_assessments(
        legs[0].id, tenant_id=tenant, installation_id=installation
    )
    assert len(assessments) == 1
    assert assessments[0].fee.amount > 0
    assert assessments[0].journal_move_id is None

    # A second run the same day is idempotent: nothing new filed.
    second = finance.run_dunning(
        tenant_id=tenant, installation_id=installation, today=datetime.date.today()
    )
    session.commit()
    assert second.items_filed == ()
    assert second.fees_assessed_irr == 0
    items_after = review_repo.list_items(
        tenant, ReviewItemFilters(item_type=ReviewItemType.INSTALLMENT_OVERDUE)
    )
    assert len(items_after) == 2
