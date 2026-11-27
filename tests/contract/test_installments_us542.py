"""Live contract tests for US-542 (Phase 5-5 / ۵ج): installment payment by cheque.

THE TWO-WAY TEST deferred from US-531, now against REAL
``installment_legs`` rows (US-540): a leg paid by cheque enters
``pending_clearance`` until the cheque clears — clear → leg paid
(+ the collection recorded for US-541's income engine), bounce →
leg unpaid + overdue again.

The REAL stack (InstallmentCommandService + InstallmentQueryService
+ ChequeCommandService with the collection hook wired, real
SQLAlchemy repositories, real Tryton adapter) against the LIVE
trytond server and the LIVE HesabYar PostgreSQL database, using
ONLY the dedicated test company "HesabYar Adapter Test"
(provisioned by
``~/workspace/projects/hesabyar-reference/setup_adapter_test_company.py``).
Cheque transitions run through the real ``account_check_ir``
module — every transition is ONE engine call, state + journal
together.

Each test uses a FRESH tenant + installation id, a UNIQUE invoice
id, and a RANDOM sayad_id (the live-sayad index is global on the
shared DB, so fixed ids would collide across runs). Teardown
deletes the test's installment rows, its registry rows, and its
Tryton ``account.check`` records.

Configuration (all optional, dev defaults match docs/dev-setup.md):
  HESABYAR_DATABASE_URL (the HesabYar DB; CI sets it, local runs
                         source hesabyar-reference/.env.local-db)
  TRYTON_BASE_URL       (default http://localhost:8010)
  TRYTON_DATABASE       (default hesabyar_tryton_test)
  TRYTON_API_KEY        (default hesabyar-dev-rest-key)
  TRYTON_TEST_COMPANY   (default "HesabYar Adapter Test")

When a server is not reachable (or the test company / module /
chart is missing), every test SKIPS cleanly with a clear message.
"""

from __future__ import annotations

import datetime
import os
import random
import uuid
from collections.abc import Generator
from dataclasses import dataclass

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from hesabyar.application.commands.cheques import ChequeCommandService
from hesabyar.application.commands.installments import (
    InstallmentCollectionHook,
    InstallmentCommandService,
)
from hesabyar.application.queries.installments import InstallmentQueryService
from hesabyar.domain.accounting.refs import AccountRef, ChequeEngineRef, CompanyRef
from hesabyar.domain.cheques.models import (
    ChequeState,
    InstallmentLegStatus,
    StorageLocation,
)
from hesabyar.domain.installments.errors import InstallmentChequeNotReceivableError
from hesabyar.domain.installments.models import LegStatus, PlanStatus
from hesabyar.infrastructure.accounting.tryton.adapter import TrytonAccountingAdapter
from hesabyar.infrastructure.accounting.tryton.client import TrytonRestClient
from hesabyar.infrastructure.accounting.tryton.errors import TrytonAdapterError
from hesabyar.infrastructure.persistence.postgres.cheques_repository import (
    SqlAlchemyChequeRepository,
)
from hesabyar.infrastructure.persistence.postgres.installments_repository import (
    SqlAlchemyInstallmentRepository,
)
from hesabyar.infrastructure.persistence.postgres.models import (
    Base,
    ChequeCustodyLogModel,
    ChequeInquiryReceiptModel,
    ChequeModel,
    InstallmentLegModel,
    InstallmentPlanModel,
    InstallmentReceiptAllocationModel,
    InstallmentReceiptModel,
)

HESABYAR_DB_URL = os.environ.get("HESABYAR_DATABASE_URL", "")
TRYTON_BASE_URL = os.environ.get("TRYTON_BASE_URL", "http://localhost:8010")
TRYTON_DATABASE = os.environ.get("TRYTON_DATABASE", "hesabyar_tryton_test")
TRYTON_API_KEY = os.environ.get("TRYTON_API_KEY", "hesabyar-dev-rest-key")
TEST_COMPANY_NAME = os.environ.get("TRYTON_TEST_COMPANY", "HesabYar Adapter Test")

#: Cheque chart accounts (US-502 / US-530): held, in-clearing,
#: payable, bounced, collateral memorandum + contras.
CHEQUE_ACCOUNT_CODES = (
    "1.2.4",
    "1.2.5",
    "2.1.5",
    "1.2.9",
    "7.1.1",
    "7.1.2",
    "7.2.1",
    "7.2.2",
)

#: Destination bank account for the deposit transition: the test
#: company's cash account (1.1.1 Cash and Cash Equivalents).
DESTINATION_ACCOUNT_CODE = "1.1.1"

#: Far-past Jalali first due date (1400-01-01 = 2021-03-21): every
#: leg is overdue on any run date, so allocation + the bounce
#: re-overdue are deterministic.
PAST_JALALI = "1400-01-01"

#: Bounce date (Jalali 1405-10-20 = 2027-01-10, the US-531 value).
BOUNCE_JALALI = "1405-10-20"

PAYEE_NATIONAL_ID = "00123456789"
ACTOR = "contract-us542"


def _unique_sayad_id() -> str:
    """A random 16-digit sayad_id (the live-sayad index is global)."""
    return str(random.randint(10**15, 10**16 - 1))


def _unique_invoice_id() -> str:
    return f"INV-US542-{uuid.uuid4().hex[:12]}"


def _registration_payload(sayad_id: str, leg_id: str, **overrides: object) -> dict:
    payload: dict[str, object] = {
        "sayad_id": sayad_id,
        "amount_toman": 1_000_000,
        "amount_currency": "IRT",
        "issue_date": "1405-08-26",
        "due_date": "1405-10-15",
        "issuer_name": "مشتری نمونه",
        "issuer_national_id": "0012345678",
        "issuer_bank": "056 سامان",
        "payee_name": "کافه ایوان",
        "payee_national_id": PAYEE_NATIONAL_ID,
        "order_id": f"ord-{sayad_id[-8:]}",
        "customer_ref": "cust-542",
        "sayad_registered": True,
        "purpose": "payment",
        "installment_leg_id": leg_id,
    }
    payload.update(overrides)
    return payload


def _plan_payload(invoice_id: str) -> dict[str, object]:
    return {
        "invoice_id": invoice_id,
        "total_principal_toman": 3_000_000,
        "leg_count": 3,
        "interval_days": 30,
        "first_due_date": PAST_JALALI,
    }


def _receipt_payload(invoice_id: str, journal_move_id: int, cheque_id: str) -> dict[str, object]:
    return {
        "invoice_id": invoice_id,
        "journal_move_id": journal_move_id,
        "amount_toman": 1_000_000,
        "payment_method": "cheque",
        "cheque_id": cheque_id,
    }


# ---------------------------------------------------------------------------
# Fixtures: live servers
# ---------------------------------------------------------------------------


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


@pytest.fixture(scope="module")
def tryton_client() -> Generator[TrytonRestClient, None, None]:
    rest_client = TrytonRestClient(TRYTON_BASE_URL, TRYTON_DATABASE, TRYTON_API_KEY, timeout=120.0)
    if not rest_client.ping():
        rest_client.close()
        pytest.skip(
            f"trytond server not reachable at {TRYTON_BASE_URL} "
            "(start it per docs/dev-setup.md §3 to run the contract tests)"
        )
    yield rest_client
    rest_client.close()


@pytest.fixture(scope="module")
def adapter(tryton_client: TrytonRestClient) -> TrytonAccountingAdapter:
    return TrytonAccountingAdapter(tryton_client)


@pytest.fixture(scope="module")
def company(adapter: TrytonAccountingAdapter) -> CompanyRef:
    for view in adapter.list_companies():
        if view.name == TEST_COMPANY_NAME:
            return view.ref
    pytest.skip(
        f"Test company {TEST_COMPANY_NAME!r} not found in Tryton DB {TRYTON_DATABASE!r}. "
        "Provision it with setup_adapter_test_company.py (see docs/adapter.md)."
    )


@pytest.fixture(scope="module")
def module_active(tryton_client: TrytonRestClient) -> None:
    records = tryton_client.search(
        "ir.module",
        domain=[("name", "=", "account_check_ir")],
        fields=["name", "state"],
    )
    if not records or records[0].get("state") != "activated":
        pytest.skip(
            "account_check_ir is not activated in this Tryton DB "
            "(run the US-530 install procedure first)."
        )


@pytest.fixture(scope="module")
def transition_available(tryton_client: TrytonRestClient) -> None:
    """Skip transition tests until ``transition_rest`` lands in the module."""
    try:
        tryton_client.action(
            "account.check",
            "transition_rest",
            record_id=999999999,
            data={"transition": "clear"},
        )
    except TrytonAdapterError as exc:
        if exc.status_code == 403:
            pytest.skip(
                "account.check.transition_rest is not exposed yet "
                "(US-531 adds the transition methods to the module)."
            )
        return
    return


@pytest.fixture(scope="module")
def chart(adapter: TrytonAccountingAdapter, company: CompanyRef):
    chart = {view.code: view for view in adapter.get_chart_of_accounts(company)}
    missing = [code for code in CHEQUE_ACCOUNT_CODES if code not in chart]
    if missing:
        pytest.skip(
            "Cheque chart accounts missing from test company: "
            f"{missing}. Run update_chart (US-530 install procedure)."
        )
    if DESTINATION_ACCOUNT_CODE not in chart:
        pytest.skip(
            f"Destination account {DESTINATION_ACCOUNT_CODE} missing from test company."
        )
    return chart


@pytest.fixture(scope="module")
def destination_account(chart) -> AccountRef:
    return chart[DESTINATION_ACCOUNT_CODE].ref


# ---------------------------------------------------------------------------
# Fixtures: per-test world (real services over one session)
# ---------------------------------------------------------------------------


@dataclass
class World:
    installation_id: str
    tenant_id: str
    session: Session
    installments: InstallmentCommandService
    queries: InstallmentQueryService
    cheques: ChequeCommandService
    sayad_ids: list[str]
    tryton_ids: list[int]


@pytest.fixture()
def world(
    hesabyar_engine: Engine,
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    module_active: None,
    transition_available: None,
    tryton_client: TrytonRestClient,
) -> Generator[World, None, None]:
    """Build the real services: installments + cheques + the collection hook.

    The cheque command service carries the leg-outcome hook
    (InstallmentCollectionHook over the same session), exactly as
    the production wiring does — a clear through this service
    records the collection, a bounce observes it.
    """
    installation_id = str(uuid.uuid4())
    tenant_id = str(uuid.uuid4())
    session = sessionmaker(bind=hesabyar_engine)()
    sayad_ids: list[str] = []
    tryton_ids: list[int] = []
    try:
        installment_repo = SqlAlchemyInstallmentRepository(session)
        cheque_repo = SqlAlchemyChequeRepository(session)
        yield World(
            installation_id=installation_id,
            tenant_id=tenant_id,
            session=session,
            installments=InstallmentCommandService(installment_repo, cheque_repo),
            queries=InstallmentQueryService(installment_repo, cheque_repo),
            cheques=ChequeCommandService(
                cheque_repo,
                adapter,
                leg_hook=InstallmentCollectionHook(installment_repo),
            ),
            sayad_ids=sayad_ids,
            tryton_ids=tryton_ids,
        )
    finally:
        session.close()
        cleanup = sessionmaker(bind=hesabyar_engine)()
        try:
            cleanup.execute(
                delete(InstallmentReceiptAllocationModel).where(
                    InstallmentReceiptAllocationModel.receipt_id.in_(
                        select(InstallmentReceiptModel.id).where(
                            InstallmentReceiptModel.installation_id == installation_id
                        )
                    )
                )
            )
            cleanup.execute(
                delete(InstallmentReceiptModel).where(
                    InstallmentReceiptModel.installation_id == installation_id
                )
            )
            cleanup.execute(
                delete(InstallmentLegModel).where(
                    InstallmentLegModel.plan_id.in_(
                        select(InstallmentPlanModel.id).where(
                            InstallmentPlanModel.installation_id == installation_id
                        )
                    )
                )
            )
            cleanup.execute(
                delete(InstallmentPlanModel).where(
                    InstallmentPlanModel.installation_id == installation_id
                )
            )
            if sayad_ids:
                cheque_ids = [
                    row[0]
                    for row in cleanup.execute(
                        select(ChequeModel.id).where(
                            ChequeModel.sayad_id.in_(sayad_ids)
                        )
                    ).all()
                ]
                if cheque_ids:
                    cleanup.execute(
                        delete(ChequeCustodyLogModel).where(
                            ChequeCustodyLogModel.cheque_id.in_(cheque_ids)
                        )
                    )
                    cleanup.execute(
                        delete(ChequeInquiryReceiptModel).where(
                            ChequeInquiryReceiptModel.cheque_id.in_(cheque_ids)
                        )
                    )
                cleanup.execute(
                    delete(ChequeModel).where(ChequeModel.sayad_id.in_(sayad_ids))
                )
            cleanup.commit()
        finally:
            cleanup.close()
        for engine_id in tryton_ids:
            try:
                tryton_client.delete(
                    "account.check",
                    engine_id,
                    context={"company": company.engine_id},
                )
            except Exception:
                pass  # held/cleared records may refuse deletion; residue is fine


# ---------------------------------------------------------------------------
# Helpers: the shared road to pending_clearance
# ---------------------------------------------------------------------------


def _pending_leg(
    world: World,
    company: CompanyRef,
    adapter: TrytonAccountingAdapter,
):
    """Create a plan + a held cheque + a cheque receipt for leg 1.

    Returns (plan, leg_1, cheque, receipt). Asserts the intake
    created NO journal (US-533 rule, unchanged by US-542) and the
    leg is pending_clearance with the linked cheque shown.
    """
    invoice_id = _unique_invoice_id()
    plan = world.installments.create_plan(
        tenant_id=world.tenant_id,
        installation_id=world.installation_id,
        company_engine_id=company.engine_id,
        payload=_plan_payload(invoice_id),
    )
    world.session.commit()
    legs = world.queries.get_plan_detail(
        plan.id, tenant_id=world.tenant_id, installation_id=world.installation_id
    ).legs
    leg_1 = legs[0].leg
    assert leg_1.principal_irr == 10_000_000

    sayad_id = _unique_sayad_id()
    world.sayad_ids.append(sayad_id)
    cheque = world.cheques.register_manual(
        tenant_id=world.tenant_id,
        installation_id=world.installation_id,
        company_engine_id=company.engine_id,
        payload=_registration_payload(sayad_id, leg_1.id),
        actor=ACTOR,
    )
    assert cheque.engine_id is not None
    world.tryton_ids.append(cheque.engine_id)
    world.session.commit()
    # Intake creates NO journal until held (US-533 rule, unchanged).
    assert cheque.state is ChequeState.REGISTERED
    assert cheque.move_ids == {}
    engine_view = adapter.get_engine_cheque(ChequeEngineRef(engine_id=cheque.engine_id))
    assert engine_view is not None
    assert engine_view.state == "registered"
    assert engine_view.move_ids == {}

    held = world.cheques.confirm_receipt(
        tenant_id=world.tenant_id,
        installation_id=world.installation_id,
        cheque_id=cheque.id,
        actor=ACTOR,
        items_matched=True,
        sayad_accepted=True,
        color_code=2,
        storage_location=StorageLocation.VAULT,
    )
    world.session.commit()
    assert held.state is ChequeState.HELD
    assert "confirm_receipt" in held.move_ids  # سند ۱ — the receipt's journal

    receipt = world.installments.record_receipt(
        tenant_id=world.tenant_id,
        installation_id=world.installation_id,
        plan_id=plan.id,
        payload=_receipt_payload(
            invoice_id,
            journal_move_id=held.move_ids["confirm_receipt"],
            cheque_id=cheque.id,
        ),
    )
    world.session.commit()
    assert receipt.cheque_id == cheque.id
    assert receipt.collected_at is None
    assert [(a.leg_id, a.amount.amount) for a in receipt.allocations] == [
        (leg_1.id, 10_000_000)
    ]

    detail = world.queries.get_plan_detail(
        plan.id, tenant_id=world.tenant_id, installation_id=world.installation_id
    )
    assert detail.legs[0].status is LegStatus.PENDING_CLEARANCE
    assert detail.legs[0].received.amount == 0
    assert detail.legs[0].pending_clearance.amount == 10_000_000
    assert detail.legs[0].linked_cheque is not None
    assert detail.legs[0].linked_cheque.sayad_id == sayad_id
    assert detail.legs[0].linked_cheque.state is ChequeState.HELD
    assert detail.legs[0].linked_cheque.due_date.gregorian_date == datetime.date(
        2027, 1, 5
    )
    assert detail.total_pending_clearance.amount == 10_000_000
    return plan, leg_1, held, receipt


def _to_in_clearing(
    world: World, cheque_id: str, destination_account: AccountRef
):
    clearing = world.cheques.deposit(
        tenant_id=world.tenant_id,
        installation_id=world.installation_id,
        cheque_id=cheque_id,
        actor=ACTOR,
        destination_account_id=destination_account.engine_id,
        destination_bank_name="بانک سامان",
    )
    world.session.commit()
    assert clearing.state is ChequeState.IN_CLEARING
    assert "deposit" in clearing.move_ids  # سند ۲
    return clearing


# ---------------------------------------------------------------------------
# The two-way test (deferred from US-531)
# ---------------------------------------------------------------------------


def test_clear_marks_the_leg_paid_and_records_the_collection(
    world: World,
    company: CompanyRef,
    destination_account: AccountRef,
    adapter: TrytonAccountingAdapter,
) -> None:
    plan, leg_1, cheque, receipt = _pending_leg(world, company, adapter)
    _to_in_clearing(world, cheque.id, destination_account)

    # Still pending while in clearing — the money has not arrived.
    detail = world.queries.get_plan_detail(
        plan.id, tenant_id=world.tenant_id, installation_id=world.installation_id
    )
    assert detail.legs[0].status is LegStatus.PENDING_CLEARANCE

    cleared = world.cheques.clear(
        tenant_id=world.tenant_id,
        installation_id=world.installation_id,
        cheque_id=cheque.id,
        actor=ACTOR,
        statement_reference="stmt-us542-clear",
    )
    world.session.commit()
    assert cleared.state is ChequeState.CLEARED
    assert "clear" in cleared.move_ids  # سند ۳
    assert cleared.installment_leg_status is InstallmentLegStatus.PAID
    assert cheque.engine_id is not None
    engine_view = adapter.get_engine_cheque(ChequeEngineRef(engine_id=cheque.engine_id))
    assert engine_view is not None
    assert engine_view.state == "cleared"

    # Clear → leg paid, and the collection is recorded (the hook
    # US-541's income engine consumes: income on COLLECTION).
    detail = world.queries.get_plan_detail(
        plan.id, tenant_id=world.tenant_id, installation_id=world.installation_id
    )
    assert detail.legs[0].leg.id == leg_1.id
    assert detail.legs[0].status is LegStatus.PAID
    assert detail.legs[0].received.amount == 10_000_000
    assert detail.legs[0].pending_clearance.amount == 0
    assert detail.total_pending_clearance.amount == 0
    saved_receipt = detail.receipts[0]
    assert saved_receipt.id == receipt.id
    assert saved_receipt.collected_at is not None
    assert saved_receipt.collected_at.gregorian_date == datetime.date.today()


def test_bounce_returns_the_leg_to_unpaid_and_overdue(
    world: World,
    company: CompanyRef,
    destination_account: AccountRef,
    adapter: TrytonAccountingAdapter,
) -> None:
    plan, leg_1, cheque, receipt = _pending_leg(world, company, adapter)
    _to_in_clearing(world, cheque.id, destination_account)

    bounced = world.cheques.bounce(
        tenant_id=world.tenant_id,
        installation_id=world.installation_id,
        cheque_id=cheque.id,
        actor=ACTOR,
        bounce_reason="عدم کفایت موجودی",
        bounce_date=BOUNCE_JALALI,
        certificate_reference="cert-us542-1",
    )
    world.session.commit()
    assert bounced.state is ChequeState.BOUNCED
    assert "bounce" in bounced.move_ids  # سند ۴
    assert bounced.installment_leg_status is InstallmentLegStatus.UNPAID
    assert cheque.engine_id is not None
    engine_view = adapter.get_engine_cheque(ChequeEngineRef(engine_id=cheque.engine_id))
    assert engine_view is not None
    assert engine_view.state == "bounced"

    # Bounce → leg unpaid + overdue again (due 1400-01-01, far past).
    # This is the input US-541's late-fee engine operates on — the
    # fee itself is computed from the ORIGINAL due date (§9.1 #13),
    # by US-541, not here.
    detail = world.queries.get_plan_detail(
        plan.id, tenant_id=world.tenant_id, installation_id=world.installation_id
    )
    assert detail.legs[0].leg.id == leg_1.id
    assert detail.legs[0].status is LegStatus.OVERDUE
    assert detail.legs[0].received.amount == 0
    assert detail.legs[0].pending_clearance.amount == 0
    assert detail.status is PlanStatus.OVERDUE
    saved_receipt = detail.receipts[0]
    assert saved_receipt.id == receipt.id
    assert saved_receipt.collected_at is None  # never collected
    assert saved_receipt.allocations  # the attempt stays as history


# ---------------------------------------------------------------------------
# The receivable-state guard on the live registry
# ---------------------------------------------------------------------------


def test_cheque_receipt_for_a_registered_cheque_is_rejected(
    world: World,
    company: CompanyRef,
) -> None:
    # A registered cheque has no journal yet (intake creates none
    # until held), so it cannot back a receipt — the receipt's
    # journal_move_id would reference a سند ۱ that does not exist.
    invoice_id = _unique_invoice_id()
    plan = world.installments.create_plan(
        tenant_id=world.tenant_id,
        installation_id=world.installation_id,
        company_engine_id=company.engine_id,
        payload=_plan_payload(invoice_id),
    )
    world.session.commit()
    legs = world.queries.get_plan_detail(
        plan.id, tenant_id=world.tenant_id, installation_id=world.installation_id
    ).legs
    sayad_id = _unique_sayad_id()
    world.sayad_ids.append(sayad_id)
    cheque = world.cheques.register_manual(
        tenant_id=world.tenant_id,
        installation_id=world.installation_id,
        company_engine_id=company.engine_id,
        payload=_registration_payload(sayad_id, legs[0].leg.id),
        actor=ACTOR,
    )
    assert cheque.engine_id is not None
    world.tryton_ids.append(cheque.engine_id)
    world.session.commit()
    assert cheque.state is ChequeState.REGISTERED
    with pytest.raises(InstallmentChequeNotReceivableError):
        world.installments.record_receipt(
            tenant_id=world.tenant_id,
            installation_id=world.installation_id,
            plan_id=plan.id,
            payload=_receipt_payload(
                invoice_id, journal_move_id=4242, cheque_id=cheque.id
            ),
        )
    world.session.rollback()
    detail = world.queries.get_plan_detail(
        plan.id, tenant_id=world.tenant_id, installation_id=world.installation_id
    )
    assert detail.receipts == ()
    assert detail.legs[0].status is LegStatus.OVERDUE
