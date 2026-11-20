"""Contract tests for US-543 (Phase 5-5 / ۵ج): BNPL installments.

The REAL stack (InstallmentCommandService + InstallmentQueryService
+ SqlAlchemyInstallmentRepository + the REAL SettlementCommandService
over the SQLAlchemy settlement + integrations repositories) against
the LIVE HesabYar PostgreSQL database. No Tryton involvement — the
BNPL receipt creates a gateway payment + tranche schedule in
HesabYar's own tables; engine posting is US-522's job and is not
exercised here.

Each test uses a FRESH tenant + installation id and UNIQUE invoice
ids + gateway references. Teardown deletes the test's rows
(installment + settlement + review + channel/contract rows).

Gateway confirmation in these tests comes through the settlement
service's manual tranche transitions (``_settle_tranche`` — the
simulator confirmation): expected → due → confirmed → received →
settled, per the §3.3 machine. No live gateway call is made.

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
from dataclasses import dataclass
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from hesabyar.application.commands.installments import InstallmentCommandService
from hesabyar.application.commands.integrations import (
    ChannelRegistryService,
    GatewayContractService,
)
from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.application.commands.settlement import SettlementCommandService
from hesabyar.application.queries.installments import InstallmentQueryService
from hesabyar.domain.installments.bnpl import BnplLegStatus
from hesabyar.domain.installments.models import PlanKind, PlanStatus
from hesabyar.domain.integrations.models import TrancheLeg
from hesabyar.domain.settlement.commands import TransitionTranche
from hesabyar.domain.settlement.models import GatewayPaymentStatus, TrancheState
from hesabyar.infrastructure.persistence.postgres.installments_repository import (
    SqlAlchemyInstallmentRepository,
)
from hesabyar.infrastructure.persistence.postgres.integrations_repository import (
    SqlAlchemyIntegrationsRepository,
)
from hesabyar.infrastructure.persistence.postgres.models import (
    BankDepositModel,
    Base,
    GatewayContractModel,
    GatewayPaymentModel,
    InstallmentLegModel,
    InstallmentPlanModel,
    InstallmentReceiptAllocationModel,
    InstallmentReceiptModel,
    InstallmentSaleContractModel,
    PaymentChannelModel,
    ReviewItemAuditModel,
    ReviewItemModel,
    SettlementEventModel,
    SettlementExceptionModel,
    SettlementFeeRecordModel,
    SettlementFileImportModel,
    SettlementMatchModel,
    SettlementPostingModel,
    SettlementTrancheModel,
)
from hesabyar.infrastructure.persistence.postgres.review_queue_repository import (
    SqlAlchemyReviewQueueRepository,
)
from hesabyar.infrastructure.persistence.postgres.settlement_repository import (
    SqlAlchemySettlementAudit,
    SqlAlchemySettlementRepository,
)

HESABYAR_DB_URL = os.environ.get("HESABYAR_DATABASE_URL", "")

COMPANY_ENGINE_ID = 543

#: Far-past Jalali first due date (1400-01-01 = 2021-03-21): every
#: leg is overdue on any run date, so allocation is deterministic.
PAST_JALALI = "1400-01-01"
#: Far-past receipt date: the gateway confirmation's Jalali month
#: (1400/01) anchors the tranche schedule deterministically.
RECEIPT_JALALI = "1400-01-15"

FOUR_TRANCHES = (
    TrancheLeg(1, 5, Decimal("25")),
    TrancheLeg(2, 5, Decimal("25")),
    TrancheLeg(3, 5, Decimal("25")),
    TrancheLeg(4, 5, Decimal("25")),
)

#: Settlement tables in FK-safe cleanup order (postings first).
_SETTLEMENT_TABLES = (
    SettlementPostingModel,
    SettlementMatchModel,
    SettlementExceptionModel,
    SettlementFeeRecordModel,
    SettlementFileImportModel,
    SettlementTrancheModel,
    SettlementEventModel,
    BankDepositModel,
    GatewayPaymentModel,
)


def _unique_invoice_id() -> str:
    return f"INV-US543-{uuid.uuid4().hex[:12]}"


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
    # (CI runs `alembic upgrade head` first, so migration 0030's
    # columns exist on the installment tables.)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@dataclass
class World:
    """One test's installation: session + repos + wired services."""

    tenant_id: str
    installation_id: str
    session: Session
    commands: InstallmentCommandService
    queries: InstallmentQueryService
    settlement: SettlementCommandService
    settlement_repo: SqlAlchemySettlementRepository

    def commit(self) -> None:
        self.session.commit()

    def setup_snapppay(self) -> None:
        """Create the snapppay channel + active 4-tranche contract (fee 10%)."""
        registry = SqlAlchemyIntegrationsRepository(self.session)
        channels = ChannelRegistryService(registry)
        contracts = GatewayContractService(registry)
        channels.create_channel(self.installation_id, "snapppay", "اسنپ‌پی", "bnpl")
        contracts.create_version(
            self.installation_id,
            "snapppay",
            valid_from=datetime.date(2026, 10, 23),
            fee_percent=Decimal("10"),
            fee_vat_rate=Decimal("10"),
            tranche_legs=FOUR_TRANCHES,
            payout_window_start_day=1,
            payout_window_end_day=5,
            grace_days_after_due=3,
        )
        self.commit()

    def create_bnpl_plan(self, invoice_id: str):
        return self.commands.create_plan(
            tenant_id=self.tenant_id,
            installation_id=self.installation_id,
            company_engine_id=COMPANY_ENGINE_ID,
            payload={
                "invoice_id": invoice_id,
                "kind": "bnpl",
                "gateway_channel_key": "snapppay",
                "total_principal_toman": 3_000_000,
                "leg_count": 3,
                "interval_days": 30,
                "first_due_date": PAST_JALALI,
            },
        )

    def bnpl_receipt(self, plan_id: str, invoice_id: str, reference: str):
        receipt = self.commands.record_receipt(
            tenant_id=self.tenant_id,
            installation_id=self.installation_id,
            plan_id=plan_id,
            payload={
                "invoice_id": invoice_id,
                "journal_move_id": 5430,
                "amount_toman": 3_000_000,
                "received_date": RECEIPT_JALALI,
                "payment_method": "bnpl",
                "gateway": "snapppay",
                "gateway_reference": reference,
            },
        )
        self.commit()
        return receipt

    def settle_tranche(self, tranche_id: str) -> None:
        """Drive one tranche to settled — the simulator confirmation."""
        tranche = self.settlement_repo.get_tranche(self.installation_id, tranche_id)
        assert tranche is not None
        net = tranche.net_irr
        steps = (
            TransitionTranche(tranche_id=tranche_id, to_state="due"),
            TransitionTranche(
                tranche_id=tranche_id, to_state="confirmed", confirmed_net_irr=net
            ),
            TransitionTranche(
                tranche_id=tranche_id, to_state="received", received_net_irr=net
            ),
            TransitionTranche(tranche_id=tranche_id, to_state="settled"),
        )
        for step in steps:
            self.settlement.transition_tranche(self.installation_id, step)
        self.commit()


@pytest.fixture()
def world(
    hesabyar_engine: Engine,
) -> Generator[World, None, None]:
    tenant_id = _tenant_id()
    installation_id = _installation_id()
    session = sessionmaker(bind=hesabyar_engine)()
    repo = SqlAlchemyInstallmentRepository(session)
    settlement_repo = SqlAlchemySettlementRepository(session)
    registry = SqlAlchemyIntegrationsRepository(session)
    audit = SqlAlchemySettlementAudit(session)
    review_repo = SqlAlchemyReviewQueueRepository(session)
    review = ReviewQueueCommandService(review_repo, review_repo)
    settlement = SettlementCommandService(
        settlement_repo, registry, audit, review=review
    )
    commands = InstallmentCommandService(repo, settlement=settlement, review=review)
    queries = InstallmentQueryService(repo, settlement_repository=settlement_repo)
    world = World(
        tenant_id=tenant_id,
        installation_id=installation_id,
        session=session,
        commands=commands,
        queries=queries,
        settlement=settlement,
        settlement_repo=settlement_repo,
    )
    try:
        yield world
    finally:
        session.rollback()
        session.close()
        # A FRESH session for cleanup (the US-533/US-540 pattern):
        # reverse FK order, scoped to this test's installation
        # (review items are tenant-scoped instead).
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
            cleanup.execute(
                delete(InstallmentSaleContractModel).where(
                    InstallmentSaleContractModel.installation_id == installation_id
                )
            )
            for model in _SETTLEMENT_TABLES:
                cleanup.execute(
                    delete(model).where(model.installation_id == installation_id)  # type: ignore[attr-defined]
                )
            cleanup.execute(
                delete(ReviewItemAuditModel).where(
                    ReviewItemAuditModel.item_id.in_(
                        select(ReviewItemModel.id).where(
                            ReviewItemModel.tenant_id == tenant_id
                        )
                    )
                )
            )
            cleanup.execute(
                delete(ReviewItemModel).where(ReviewItemModel.tenant_id == tenant_id)
            )
            channel_ids = list(
                cleanup.execute(
                    select(PaymentChannelModel.id).where(
                        PaymentChannelModel.installation_id == installation_id
                    )
                ).scalars()
            )
            if channel_ids:
                cleanup.execute(
                    delete(GatewayContractModel).where(
                        GatewayContractModel.channel_id.in_(channel_ids)
                    )
                )
            cleanup.execute(
                delete(PaymentChannelModel).where(
                    PaymentChannelModel.installation_id == installation_id
                )
            )
            cleanup.commit()
        finally:
            cleanup.close()


def test_bnpl_plan_persists_kind_and_gateway(world: World) -> None:
    invoice_id = _unique_invoice_id()
    plan = world.create_bnpl_plan(invoice_id)
    world.commit()
    assert plan.kind is PlanKind.BNPL
    assert plan.gateway_channel_key == "snapppay"
    # Round-trip through the repository: kind + gateway survive.
    reread = world.queries.get_plan_detail(
        plan.id, tenant_id=world.tenant_id, installation_id=world.installation_id
    )
    assert reread.plan.kind is PlanKind.BNPL
    assert reread.plan.gateway_channel_key == "snapppay"
    assert reread.status is PlanStatus.ACTIVE


def test_bnpl_receipt_creates_gateway_payment_and_tranches(world: World) -> None:
    world.setup_snapppay()
    invoice_id = _unique_invoice_id()
    plan = world.create_bnpl_plan(invoice_id)
    world.commit()
    receipt = world.bnpl_receipt(plan.id, invoice_id, f"SP-{uuid.uuid4().hex[:8]}")
    assert receipt.gateway_payment_id is not None

    payment = world.settlement_repo.get_payment(
        world.installation_id, receipt.gateway_payment_id
    )
    assert payment is not None
    assert payment.gross_irr == 30_000_000
    assert payment.fee_irr == 3_000_000
    assert payment.fee_vat_irr == 300_000
    assert payment.net_irr == 26_700_000
    assert payment.status is GatewayPaymentStatus.SCHEDULED

    tranches = list(
        world.settlement_repo.list_tranches_for_payment(
            world.installation_id, payment.id
        )
    )
    assert len(tranches) == 4
    assert sum(t.net_irr for t in tranches) == 26_700_000
    assert all(t.state is TrancheState.EXPECTED for t in tranches)
    # Anchor: the receipt's Jalali month (1400/01) → dues are the
    # 5th of 1400/02..05.
    assert all(t.sales_period_jalali == "1400/01" for t in tranches)

    detail = world.queries.get_plan_detail(
        plan.id, tenant_id=world.tenant_id, installation_id=world.installation_id
    )
    assert [leg.status for leg in detail.legs] == [
        BnplLegStatus.GATEWAY_CONFIRMED,
        BnplLegStatus.GATEWAY_CONFIRMED,
        BnplLegStatus.GATEWAY_CONFIRMED,
    ]
    assert detail.gateway_receivable.amount == 26_700_000
    assert detail.gateway_settled.amount == 0
    assert detail.outstanding.amount == 0


def test_bnpl_leg_lifecycle_to_paid_via_simulator_confirmation(world: World) -> None:
    world.setup_snapppay()
    invoice_id = _unique_invoice_id()
    plan = world.create_bnpl_plan(invoice_id)
    world.commit()
    receipt = world.bnpl_receipt(plan.id, invoice_id, f"SP-{uuid.uuid4().hex[:8]}")
    assert receipt.gateway_payment_id is not None
    tranches = list(
        world.settlement_repo.list_tranches_for_payment(
            world.installation_id, receipt.gateway_payment_id
        )
    )

    world.settle_tranche(tranches[0].id)
    detail = world.queries.get_plan_detail(
        plan.id, tenant_id=world.tenant_id, installation_id=world.installation_id
    )
    assert [leg.status for leg in detail.legs] == [
        BnplLegStatus.SETTLED_PER_TRANCHE,
        BnplLegStatus.SETTLED_PER_TRANCHE,
        BnplLegStatus.SETTLED_PER_TRANCHE,
    ]
    assert detail.gateway_settled.amount == 6_675_000
    assert detail.gateway_receivable.amount == 20_025_000

    for tranche in tranches[1:]:
        world.settle_tranche(tranche.id)
    detail = world.queries.get_plan_detail(
        plan.id, tenant_id=world.tenant_id, installation_id=world.installation_id
    )
    assert [leg.status for leg in detail.legs] == [
        BnplLegStatus.PAID,
        BnplLegStatus.PAID,
        BnplLegStatus.PAID,
    ]
    assert detail.status is PlanStatus.PAID
    assert detail.gateway_settled.amount == 26_700_000
    assert detail.gateway_receivable.amount == 0


def test_bnpl_rejection_returns_leg_to_expected_and_files_review_item(
    world: World,
) -> None:
    world.setup_snapppay()
    invoice_id = _unique_invoice_id()
    plan = world.create_bnpl_plan(invoice_id)
    world.commit()
    receipt = world.bnpl_receipt(plan.id, invoice_id, f"SP-{uuid.uuid4().hex[:8]}")
    assert receipt.gateway_payment_id is not None

    world.commands.reject_bnpl_receipt(
        tenant_id=world.tenant_id,
        installation_id=world.installation_id,
        plan_id=plan.id,
        receipt_id=receipt.id,
        reason="customer not eligible",
        actor="contract-test",
    )
    world.commit()

    payment = world.settlement_repo.get_payment(
        world.installation_id, receipt.gateway_payment_id
    )
    assert payment is not None
    assert payment.status is GatewayPaymentStatus.CANCELLED

    detail = world.queries.get_plan_detail(
        plan.id, tenant_id=world.tenant_id, installation_id=world.installation_id
    )
    assert [leg.status for leg in detail.legs] == [
        BnplLegStatus.EXPECTED,
        BnplLegStatus.EXPECTED,
        BnplLegStatus.EXPECTED,
    ]
    assert detail.total_received.amount == 0
    assert detail.outstanding.amount == 30_000_000

    items = world.session.execute(
        select(ReviewItemModel).where(ReviewItemModel.tenant_id == world.tenant_id)
    ).scalars().all()
    assert len(items) == 1
    assert items[0].item_type == "settlement_variance"
    assert items[0].subtype == "bnpl_gateway_rejection"
    assert items[0].channel == "snapppay"


def test_list_plans_filters_by_kind(world: World) -> None:
    internal_invoice = _unique_invoice_id()
    world.commands.create_plan(
        tenant_id=world.tenant_id,
        installation_id=world.installation_id,
        company_engine_id=COMPANY_ENGINE_ID,
        payload={
            "invoice_id": internal_invoice,
            "total_principal_toman": 1_000_000,
            "leg_count": 1,
            "interval_days": 30,
            "first_due_date": PAST_JALALI,
        },
    )
    bnpl_invoice = _unique_invoice_id()
    world.create_bnpl_plan(bnpl_invoice)
    world.commit()

    bnpl = world.queries.list_plans(
        tenant_id=world.tenant_id,
        installation_id=world.installation_id,
        kind=PlanKind.BNPL,
    )
    assert [s.plan.invoice_id for s in bnpl] == [bnpl_invoice]
    internal = world.queries.list_plans(
        tenant_id=world.tenant_id,
        installation_id=world.installation_id,
        kind=PlanKind.INTERNAL,
    )
    assert [s.plan.invoice_id for s in internal] == [internal_invoice]
