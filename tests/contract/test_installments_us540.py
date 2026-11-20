"""Contract tests for US-540 (Phase 5-5 / ۵ج): installment plans + linkage guard.

The REAL stack (InstallmentCommandService + InstallmentQueryService
+ SqlAlchemyInstallmentRepository) against the LIVE HesabYar
PostgreSQL database. No Tryton involvement — US-540 creates no
journal and makes no engine call, so these tests need only the
HesabYar DB (migration 0027's tables, created by ``create_all`` in
the fixture when missing).

Each test uses a FRESH tenant + installation id and a UNIQUE
invoice id (the one-plan-per-invoice index is per-installation, but
unique ids keep runs independent on the shared dev DB). Teardown
deletes the test's rows (allocations → receipts → legs → plans →
contracts).

Configuration (all optional):
  HESABYAR_DATABASE_URL (the HesabYar DB; CI sets it, local runs
                         source hesabyar-reference/.env.local-db)

When the DB is not reachable, every test SKIPS cleanly with a clear
message.
"""

from __future__ import annotations

import datetime
import os
import uuid
from collections.abc import Generator

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from hesabyar.application.commands.installments import InstallmentCommandService
from hesabyar.application.queries.installments import InstallmentQueryService
from hesabyar.domain.installments.errors import (
    InstallmentInvoiceMismatchError,
    InstallmentLinkageError,
    InstallmentPlanExistsError,
)
from hesabyar.domain.installments.models import LegStatus, PlanStatus
from hesabyar.infrastructure.persistence.postgres.installments_repository import (
    SqlAlchemyInstallmentRepository,
)
from hesabyar.infrastructure.persistence.postgres.models import (
    Base,
    InstallmentLegModel,
    InstallmentPlanModel,
    InstallmentReceiptAllocationModel,
    InstallmentReceiptModel,
    InstallmentSaleContractModel,
)

HESABYAR_DB_URL = os.environ.get("HESABYAR_DATABASE_URL", "")

COMPANY_ENGINE_ID = 540

#: Far-past Jalali first due date (1400-01-01 = 2021-03-21): every
#: leg is overdue on any run date, so allocation is deterministic.
PAST_JALALI = "1400-01-01"


def _unique_invoice_id() -> str:
    return f"INV-US540-{uuid.uuid4().hex[:12]}"


def _tenant_id() -> str:
    return str(uuid.uuid4())


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
def tenant() -> str:
    return _tenant_id()


@pytest.fixture()
def installation() -> str:
    return _installation_id()


@pytest.fixture()
def services(session: Session) -> tuple[InstallmentCommandService, InstallmentQueryService]:
    repo = SqlAlchemyInstallmentRepository(session)
    return InstallmentCommandService(repo), InstallmentQueryService(repo)


@pytest.fixture()
def _cleanup(
    hesabyar_engine: Engine, installation: str
) -> Generator[None, None, None]:
    yield
    # A FRESH session for cleanup (the US-533 pattern): the test's
    # own session may already be closed by teardown time, and the
    # deletes must not depend on fixture teardown order. Reverse FK
    # order; scoped to this test's installation.
    cleanup = sessionmaker(bind=hesabyar_engine)()
    try:
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
        cleanup.commit()
    finally:
        cleanup.close()


def _plan_payload(invoice_id: str, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "invoice_id": invoice_id,
        "total_principal_toman": 3_000_000,
        "leg_count": 3,
        "interval_days": 30,
        "first_due_date": PAST_JALALI,
    }
    payload.update(overrides)
    return payload


def _receipt_payload(invoice_id: str, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "invoice_id": invoice_id,
        "journal_move_id": 4242,
        "amount_toman": 1_000_000,
        "payment_method": "cash",
    }
    payload.update(overrides)
    return payload


# -- plan + legs round-trip -------------------------------------------------------


@pytest.mark.usefixtures("_cleanup")
def test_plan_and_legs_round_trip_through_real_db(
    session: Session,
    services: tuple[InstallmentCommandService, InstallmentQueryService],
    tenant: str,
    installation: str,
) -> None:
    commands, queries = services
    invoice_id = _unique_invoice_id()
    plan = commands.create_plan(
        tenant_id=tenant,
        installation_id=installation,
        company_engine_id=COMPANY_ENGINE_ID,
        payload=_plan_payload(invoice_id),
    )
    session.commit()

    # A fresh read (new repository over the same session) sees the
    # plan, its legs, and the derived overdue status.
    detail = queries.get_plan_detail(
        plan.id, tenant_id=tenant, installation_id=installation
    )
    assert detail.plan.invoice_id == invoice_id
    assert detail.plan.total_principal_irr == 30_000_000
    assert len(detail.legs) == 3
    assert [leg.leg.principal_irr for leg in detail.legs] == [
        10_000_000,
        10_000_000,
        10_000_000,
    ]
    assert [leg.status for leg in detail.legs] == [
        LegStatus.OVERDUE,
        LegStatus.OVERDUE,
        LegStatus.OVERDUE,
    ]
    assert detail.status is PlanStatus.OVERDUE
    assert detail.outstanding.amount == 30_000_000


@pytest.mark.usefixtures("_cleanup")
def test_second_plan_for_same_invoice_is_rejected_on_real_db(
    session: Session,
    services: tuple[InstallmentCommandService, InstallmentQueryService],
    tenant: str,
    installation: str,
) -> None:
    commands, _ = services
    invoice_id = _unique_invoice_id()
    commands.create_plan(
        tenant_id=tenant,
        installation_id=installation,
        company_engine_id=COMPANY_ENGINE_ID,
        payload=_plan_payload(invoice_id),
    )
    session.commit()
    with pytest.raises(InstallmentPlanExistsError):
        commands.create_plan(
            tenant_id=tenant,
            installation_id=installation,
            company_engine_id=COMPANY_ENGINE_ID,
            payload=_plan_payload(invoice_id),
        )


# -- receipts + THE LINKAGE GUARD (real DB) -----------------------------------------


@pytest.mark.usefixtures("_cleanup")
def test_receipt_allocates_and_persists_on_real_db(
    session: Session,
    services: tuple[InstallmentCommandService, InstallmentQueryService],
    tenant: str,
    installation: str,
) -> None:
    commands, queries = services
    invoice_id = _unique_invoice_id()
    plan = commands.create_plan(
        tenant_id=tenant,
        installation_id=installation,
        company_engine_id=COMPANY_ENGINE_ID,
        payload=_plan_payload(invoice_id),
    )
    session.commit()
    receipt = commands.record_receipt(
        tenant_id=tenant,
        installation_id=installation,
        plan_id=plan.id,
        payload=_receipt_payload(invoice_id),
    )
    session.commit()
    assert receipt.amount_irr == 10_000_000
    assert receipt.allocated_irr == 10_000_000
    assert receipt.advance_irr == 0

    detail = queries.get_plan_detail(
        plan.id, tenant_id=tenant, installation_id=installation
    )
    assert len(detail.receipts) == 1
    assert detail.receipts[0].journal_move_id == 4242
    assert detail.legs[0].status is LegStatus.PAID
    assert detail.legs[0].received.amount == 10_000_000
    assert detail.total_received.amount == 10_000_000
    assert detail.outstanding.amount == 20_000_000


@pytest.mark.usefixtures("_cleanup")
def test_receipt_overpayment_persists_advance_on_real_db(
    session: Session,
    services: tuple[InstallmentCommandService, InstallmentQueryService],
    tenant: str,
    installation: str,
) -> None:
    commands, queries = services
    invoice_id = _unique_invoice_id()
    plan = commands.create_plan(
        tenant_id=tenant,
        installation_id=installation,
        company_engine_id=COMPANY_ENGINE_ID,
        payload=_plan_payload(invoice_id),
    )
    session.commit()
    receipt = commands.record_receipt(
        tenant_id=tenant,
        installation_id=installation,
        plan_id=plan.id,
        payload=_receipt_payload(invoice_id, amount_toman=5_000_000),
    )
    session.commit()
    assert receipt.allocated_irr == 30_000_000
    assert receipt.advance_irr == 20_000_000

    detail = queries.get_plan_detail(
        plan.id, tenant_id=tenant, installation_id=installation
    )
    assert detail.status is PlanStatus.PAID
    assert detail.total_advance.amount == 20_000_000


@pytest.mark.usefixtures("_cleanup")
def test_guard_rejects_receipt_without_both_refs_on_real_db(
    session: Session,
    services: tuple[InstallmentCommandService, InstallmentQueryService],
    tenant: str,
    installation: str,
) -> None:
    commands, queries = services
    invoice_id = _unique_invoice_id()
    plan = commands.create_plan(
        tenant_id=tenant,
        installation_id=installation,
        company_engine_id=COMPANY_ENGINE_ID,
        payload=_plan_payload(invoice_id),
    )
    session.commit()
    payload = _receipt_payload(invoice_id)
    payload["invoice_id"] = ""
    payload["journal_move_id"] = 0
    with pytest.raises(InstallmentLinkageError) as exc_info:
        commands.record_receipt(
            tenant_id=tenant,
            installation_id=installation,
            plan_id=plan.id,
            payload=payload,
        )
    assert exc_info.value.missing == ("invoice_id", "journal_move_id")
    session.rollback()
    detail = queries.get_plan_detail(
        plan.id, tenant_id=tenant, installation_id=installation
    )
    assert detail.receipts == ()
    assert detail.total_received.amount == 0


@pytest.mark.usefixtures("_cleanup")
def test_guard_rejects_receipt_with_wrong_invoice_on_real_db(
    session: Session,
    services: tuple[InstallmentCommandService, InstallmentQueryService],
    tenant: str,
    installation: str,
) -> None:
    commands, queries = services
    invoice_id = _unique_invoice_id()
    plan = commands.create_plan(
        tenant_id=tenant,
        installation_id=installation,
        company_engine_id=COMPANY_ENGINE_ID,
        payload=_plan_payload(invoice_id),
    )
    session.commit()
    with pytest.raises(InstallmentInvoiceMismatchError):
        commands.record_receipt(
            tenant_id=tenant,
            installation_id=installation,
            plan_id=plan.id,
            payload=_receipt_payload(_unique_invoice_id()),
        )
    session.rollback()
    detail = queries.get_plan_detail(
        plan.id, tenant_id=tenant, installation_id=installation
    )
    assert detail.receipts == ()


# -- contracts + leg lookup -----------------------------------------------------------


@pytest.mark.usefixtures("_cleanup")
def test_contract_round_trip_and_plan_link_on_real_db(
    session: Session,
    services: tuple[InstallmentCommandService, InstallmentQueryService],
    tenant: str,
    installation: str,
) -> None:
    commands, queries = services
    contract = commands.create_contract(
        tenant_id=tenant,
        installation_id=installation,
        company_engine_id=COMPANY_ENGINE_ID,
        payload={
            "code": f"US540-{uuid.uuid4().hex[:8]}",
            "name": "قرارداد تست",
            "version": 1,
            "effective_from": "1405-08-01",
            "late_fee_basis": "daily_percent",
            "late_fee_rate": "0.02",
            "late_fee_grace_days": 3,
            "penalty_account_engine_id": 77,
            "penalty_account_code": "6.1.2",
        },
    )
    session.commit()
    found = queries.get_contract(
        contract.id, tenant_id=tenant, installation_id=installation
    )
    assert found.late_fee_basis.value == "daily_percent"
    assert str(found.late_fee_rate) == "0.02"
    assert found.penalty_account_engine_id == 77

    invoice_id = _unique_invoice_id()
    plan = commands.create_plan(
        tenant_id=tenant,
        installation_id=installation,
        company_engine_id=COMPANY_ENGINE_ID,
        payload=_plan_payload(invoice_id, contract_id=contract.id),
    )
    session.commit()
    detail = queries.get_plan_detail(
        plan.id, tenant_id=tenant, installation_id=installation
    )
    assert detail.plan.contract_id == contract.id


@pytest.mark.usefixtures("_cleanup")
def test_leg_lookup_resolves_the_cheque_link_target_on_real_db(
    session: Session,
    services: tuple[InstallmentCommandService, InstallmentQueryService],
    tenant: str,
    installation: str,
) -> None:
    # US-542 resolves a cheque's installment_leg_id through the
    # repository's get_leg — the link target must be findable on the
    # real tables (and scoped: another installation sees nothing).
    commands, _ = services
    repo = SqlAlchemyInstallmentRepository(session)
    invoice_id = _unique_invoice_id()
    plan = commands.create_plan(
        tenant_id=tenant,
        installation_id=installation,
        company_engine_id=COMPANY_ENGINE_ID,
        payload=_plan_payload(invoice_id),
    )
    session.commit()
    legs = repo.list_legs(plan.id, tenant_id=tenant, installation_id=installation)
    assert len(legs) == 3
    found = repo.get_leg(legs[1].id, tenant_id=tenant, installation_id=installation)
    assert found is not None
    assert found.sequence == 2
    assert found.plan_id == plan.id
    assert (
        repo.get_leg(legs[1].id, tenant_id=tenant, installation_id=_installation_id())
        is None
    )


@pytest.mark.usefixtures("_cleanup")
def test_plan_list_scoped_to_installation_on_real_db(
    session: Session,
    services: tuple[InstallmentCommandService, InstallmentQueryService],
    tenant: str,
    installation: str,
) -> None:
    commands, queries = services
    invoice_id = _unique_invoice_id()
    commands.create_plan(
        tenant_id=tenant,
        installation_id=installation,
        company_engine_id=COMPANY_ENGINE_ID,
        payload=_plan_payload(invoice_id),
    )
    session.commit()
    mine = queries.list_plans(
        tenant_id=tenant, installation_id=installation, today=datetime.date.today()
    )
    assert [s.plan.invoice_id for s in mine] == [invoice_id]
    other = queries.list_plans(
        tenant_id=tenant,
        installation_id=_installation_id(),
        today=datetime.date.today(),
    )
    assert other == []
