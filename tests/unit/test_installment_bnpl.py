"""Unit tests for BNPL installments (US-543, Phase 5-5 ۵ج).

Domain: the BNPL leg lifecycle derivation + per-tranche progress +
the finance-income/dunning separation predicates. Validation: plan
kind + gateway pinning, BNPL receipt fields. Services: the
installment command/query services over the in-memory fakes, with
the REAL SettlementCommandService over its fakes (a snapppay
4-tranche contract + a digipay lump-sum contract) — the reuse
point under test: a BNPL receipt creates a gateway payment whose
tranches the US-521 engine derives from the contract.

No DB, no engine, no live gateway call. Gateway confirmation in
tests comes through the settlement service's manual tranche
transitions (``_settle_tranche`` — the simulator confirmation):
expected → due → confirmed → received → settled, per the §3.3
machine.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from hesabyar.application.commands.installments import InstallmentCommandService
from hesabyar.application.commands.integrations import (
    ChannelRegistryService,
    GatewayContractService,
)
from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.application.commands.settlement import SettlementCommandService
from hesabyar.application.errors import IntegrationChannelNotFoundError
from hesabyar.application.queries.installments import InstallmentQueryService
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.domain.installments.bnpl import (
    BnplLegStatus,
    bnpl_leg_status,
    bnpl_plan_status,
    bnpl_tranche_progress,
    plan_accrues_finance_income,
    plan_enters_dunning,
)
from hesabyar.domain.installments.errors import (
    InstallmentBnplAlreadyRejectedError,
    InstallmentBnplGatewayMismatchError,
    InstallmentBnplPlanMethodError,
    InstallmentBnplPlanRequiredError,
    InstallmentBnplUnavailableError,
    InstallmentLinkageError,
    InstallmentNotBnplReceiptError,
    InstallmentReceiptNotFoundError,
    InstallmentValidationError,
)
from hesabyar.domain.installments.models import (
    InstallmentPlan,
    LegStatus,
    PlanKind,
    PlanStatus,
)
from hesabyar.domain.installments.validation import validate_plan_payload
from hesabyar.domain.integrations.models import TrancheLeg
from hesabyar.domain.review_queue.models import (
    ReviewAuditEntry,
    ReviewItem,
    ReviewItemType,
    SlaPolicy,
)
from hesabyar.domain.review_queue.ports import (
    ReviewAuditPort,
    ReviewItemFilters,
    ReviewQueuePort,
)
from hesabyar.domain.settlement.commands import CancelGatewayPayment, TransitionTranche
from hesabyar.domain.settlement.errors import SettlementPaymentExistsError
from hesabyar.domain.settlement.models import (
    GatewayPaymentStatus,
    SettlementTranche,
    TrancheState,
)
from tests.unit.test_installment_services import (
    COMPANY_ENGINE_ID,
    FakeInstallmentRepository,
)
from tests.unit.test_integrations_services import FakeIntegrationsRepository
from tests.unit.test_settlement_services import (
    FOUR_TRANCHES,
    FakeSettlementAudit,
    FakeSettlementRepository,
)

TENANT_ID = "00000000-0000-0000-0000-000000000543"
INSTALLATION_ID = "00000000-0000-0000-0000-000000000544"

TODAY = datetime.date(2026, 11, 20)

#: Jalali for 2026-10-21 / 2026-11-20.
PAST_JALALI = "1405-07-29"
TODAY_JALALI = "1405-08-29"

#: DigiPay lump-sum: one leg, 100%, due ``day_lag`` business days
#: after confirmation (the contract's model — never hard-coded).
DIGIPAY_LEGS = (TrancheLeg(0, 5, Decimal("100")),)


@dataclass
class FakeReviewQueue(ReviewQueuePort):
    """Minimal review-queue fake: only filing is under test here."""

    items: dict[str, ReviewItem] = field(default_factory=dict)

    def list_items(self, tenant_id: str, filters: ReviewItemFilters) -> Sequence[ReviewItem]:
        return [i for i in self.items.values() if i.tenant_id == tenant_id]

    def get_item(self, item_id: str, tenant_id: str) -> ReviewItem | None:
        item = self.items.get(item_id)
        if item is None or item.tenant_id != tenant_id:
            return None
        return item

    def save_item(self, item: ReviewItem) -> ReviewItem:
        self.items[item.id] = item
        return item

    def update_item(self, item: ReviewItem) -> ReviewItem:
        self.items[item.id] = item
        return item

    def list_policies(self, tenant_id: str) -> Sequence[SlaPolicy]:
        return []

    def upsert_policy(self, tenant_id: str, policy: SlaPolicy) -> SlaPolicy:
        return policy


@dataclass
class FakeReviewAudit(ReviewAuditPort):
    """Minimal review-audit fake (records entries, never read here)."""

    entries: list[ReviewAuditEntry] = field(default_factory=list)

    def record(self, entry: ReviewAuditEntry) -> ReviewAuditEntry:
        self.entries.append(entry)
        return entry

    def list_for_item(self, item_id: str, tenant_id: str) -> Sequence[ReviewAuditEntry]:
        return [e for e in self.entries if e.item_id == item_id]


@dataclass
class BnplWorld:
    """The wired world: installment services + the real settlement engine over fakes."""

    repo: FakeInstallmentRepository
    settlement_repo: FakeSettlementRepository
    registry: FakeIntegrationsRepository
    review_queue: FakeReviewQueue
    commands: InstallmentCommandService
    queries: InstallmentQueryService
    settlement: SettlementCommandService
    review: ReviewQueueCommandService

    def create_bnpl_plan(self, invoice_id: str, gateway: str = "snapppay"):
        return self.commands.create_plan(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            company_engine_id=COMPANY_ENGINE_ID,
            payload={
                "invoice_id": invoice_id,
                "kind": "bnpl",
                "gateway_channel_key": gateway,
                "total_principal_toman": 3_000_000,
                "leg_count": 3,
                "interval_days": 30,
                "first_due_date": PAST_JALALI,
            },
        )

    def create_internal_plan(self, invoice_id: str):
        return self.commands.create_plan(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            company_engine_id=COMPANY_ENGINE_ID,
            payload={
                "invoice_id": invoice_id,
                "total_principal_toman": 3_000_000,
                "leg_count": 3,
                "interval_days": 30,
                "first_due_date": PAST_JALALI,
            },
        )

    def bnpl_receipt(
        self,
        plan_id: str,
        invoice_id: str,
        *,
        today: datetime.date = TODAY,
        **overrides: object,
    ):
        payload: dict[str, object] = {
            "invoice_id": invoice_id,
            "journal_move_id": 4242,
            "amount_toman": 1_000_000,
            "received_date": TODAY_JALALI,
            "payment_method": "bnpl",
            "gateway": "snapppay",
            "gateway_reference": f"SP-{invoice_id}",
        }
        payload.update(overrides)
        return self.commands.record_receipt(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            plan_id=plan_id,
            payload=payload,
            today=today,
        )

    def settle_tranche(self, tranche_id: str) -> None:
        """Drive one tranche to settled — the simulator confirmation.

        Walks the §3.3 machine (expected → due → confirmed →
        received → settled) through the settlement service, the
        same manual path an operator (or the US-523 import) uses.
        """
        tranche = self.settlement_repo.get_tranche(INSTALLATION_ID, tranche_id)
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
            self.settlement.transition_tranche(INSTALLATION_ID, step)


@pytest.fixture()
def world() -> BnplWorld:
    repo = FakeInstallmentRepository()
    settlement_repo = FakeSettlementRepository()
    registry = FakeIntegrationsRepository()
    channels = ChannelRegistryService(registry)
    contracts = GatewayContractService(registry)
    # SnappPay: 4 equal tranches over 4 months, fee 10% (user-entered).
    channels.create_channel(INSTALLATION_ID, "snapppay", "اسنپ‌پی", "bnpl")
    contracts.create_version(
        INSTALLATION_ID,
        "snapppay",
        valid_from=datetime.date(2026, 10, 23),
        fee_percent=Decimal("10"),
        fee_vat_rate=Decimal("10"),
        tranche_legs=FOUR_TRANCHES,
        payout_window_start_day=1,
        payout_window_end_day=5,
        grace_days_after_due=3,
    )
    # DigiPay: lump-sum, 20 business days, fee 6% (user-entered).
    channels.create_channel(INSTALLATION_ID, "digipay", "دیجی‌پی", "bnpl")
    contracts.create_version(
        INSTALLATION_ID,
        "digipay",
        valid_from=datetime.date(2026, 10, 23),
        fee_percent=Decimal("6"),
        fee_vat_rate=Decimal("10"),
        tranche_legs=DIGIPAY_LEGS,
        day_lag=20,
        grace_days_after_due=3,
    )
    review_queue = FakeReviewQueue()
    review = ReviewQueueCommandService(review_queue, FakeReviewAudit())
    settlement = SettlementCommandService(
        settlement_repo, registry, FakeSettlementAudit(), review=review
    )
    commands = InstallmentCommandService(repo, settlement=settlement, review=review)
    queries = InstallmentQueryService(repo, settlement_repository=settlement_repo)
    return BnplWorld(
        repo=repo,
        settlement_repo=settlement_repo,
        registry=registry,
        review_queue=review_queue,
        commands=commands,
        queries=queries,
        settlement=settlement,
        review=review,
    )


# -- domain: BNPL leg lifecycle -----------------------------------------------------


def _tranche(
    state: TrancheState, *, net_irr: int = 1000, received_net_irr: int = 0
) -> SettlementTranche:
    return SettlementTranche(
        id=f"tr-{state.value}-{net_irr}-{received_net_irr}",
        installation_id=INSTALLATION_ID,
        gateway_payment_id="pay-1",
        tranche_index=1,
        tranche_count=1,
        sales_period_jalali="1405/08",
        expected_window_start=datetime.date(2026, 11, 21),
        expected_date=datetime.date(2026, 11, 26),
        gross_irr=net_irr,
        fee_irr=0,
        fee_vat_irr=0,
        net_irr=net_irr,
        state=state,
        received_net_irr=received_net_irr,
    )


def test_bnpl_leg_status_no_tranches_is_expected() -> None:
    assert bnpl_leg_status([]) == BnplLegStatus.EXPECTED


def test_bnpl_leg_status_open_tranches_is_gateway_confirmed() -> None:
    assert bnpl_leg_status([_tranche(TrancheState.EXPECTED)]) == BnplLegStatus.GATEWAY_CONFIRMED
    assert bnpl_leg_status([_tranche(TrancheState.DUE)]) == BnplLegStatus.GATEWAY_CONFIRMED
    assert bnpl_leg_status([_tranche(TrancheState.OVERDUE)]) == BnplLegStatus.GATEWAY_CONFIRMED
    assert (
        bnpl_leg_status([_tranche(TrancheState.EXPECTED), _tranche(TrancheState.DUE)])
        == BnplLegStatus.GATEWAY_CONFIRMED
    )


def test_bnpl_leg_status_partial_settlement_is_settled_per_tranche() -> None:
    # One tranche settled, one still open: partial gateway
    # settlement allocates per tranche.
    tranches = [
        _tranche(TrancheState.SETTLED, net_irr=1000, received_net_irr=1000),
        _tranche(TrancheState.EXPECTED, net_irr=1000),
    ]
    assert bnpl_leg_status(tranches) == BnplLegStatus.SETTLED_PER_TRANCHE
    # A confirmed-but-not-received tranche is progress too.
    assert (
        bnpl_leg_status([_tranche(TrancheState.CONFIRMED), _tranche(TrancheState.EXPECTED)])
        == BnplLegStatus.SETTLED_PER_TRANCHE
    )
    assert (
        bnpl_leg_status(
            [_tranche(TrancheState.PARTIALLY_RECEIVED, received_net_irr=400)]
        )
        == BnplLegStatus.SETTLED_PER_TRANCHE
    )


def test_bnpl_leg_status_all_settled_is_paid() -> None:
    tranches = [
        _tranche(TrancheState.SETTLED, net_irr=1000, received_net_irr=1000),
        _tranche(TrancheState.SETTLED, net_irr=1000, received_net_irr=1000),
    ]
    assert bnpl_leg_status(tranches) == BnplLegStatus.PAID


def test_bnpl_leg_status_all_cancelled_returns_to_expected() -> None:
    # The gateway cancelled everything without settling: the leg is
    # unpaid again (the review item owns the follow-up).
    assert bnpl_leg_status([_tranche(TrancheState.CANCELLED)]) == BnplLegStatus.EXPECTED
    assert (
        bnpl_leg_status(
            [_tranche(TrancheState.CANCELLED), _tranche(TrancheState.WRITTEN_OFF)]
        )
        == BnplLegStatus.EXPECTED
    )


def test_bnpl_leg_status_settled_plus_cancelled_is_settled_per_tranche() -> None:
    # Partial settlement + a later cancel: progress exists, the leg
    # is not fully paid, and it is not back to expected either.
    tranches = [
        _tranche(TrancheState.SETTLED, net_irr=1000, received_net_irr=1000),
        _tranche(TrancheState.CANCELLED, net_irr=1000),
    ]
    assert bnpl_leg_status(tranches) == BnplLegStatus.SETTLED_PER_TRANCHE


def test_bnpl_plan_status_never_overdue() -> None:
    assert bnpl_plan_status([]) == PlanStatus.ACTIVE
    assert bnpl_plan_status([BnplLegStatus.EXPECTED]) == PlanStatus.ACTIVE
    assert bnpl_plan_status([BnplLegStatus.GATEWAY_CONFIRMED]) == PlanStatus.ACTIVE
    assert bnpl_plan_status([BnplLegStatus.SETTLED_PER_TRANCHE]) == PlanStatus.ACTIVE
    assert bnpl_plan_status([BnplLegStatus.PAID, BnplLegStatus.PAID]) == PlanStatus.PAID
    assert (
        bnpl_plan_status([BnplLegStatus.PAID, BnplLegStatus.GATEWAY_CONFIRMED])
        == PlanStatus.ACTIVE
    )


def test_bnpl_tranche_progress_allocates_per_tranche() -> None:
    progress = bnpl_tranche_progress(
        [
            _tranche(TrancheState.SETTLED, net_irr=1000, received_net_irr=1000),
            _tranche(TrancheState.PARTIALLY_RECEIVED, net_irr=1000, received_net_irr=400),
            _tranche(TrancheState.EXPECTED, net_irr=1000),
            _tranche(TrancheState.CANCELLED, net_irr=1000),
        ]
    )
    assert progress.settled_irr == 1400
    assert progress.open_irr == 1600  # 600 remainder + 1000 open; cancelled counts nowhere
    assert progress.settled_count == 1
    assert progress.total_count == 4
    assert progress.is_fully_settled is False


def test_bnpl_tranche_progress_empty() -> None:
    progress = bnpl_tranche_progress([])
    assert progress.settled_irr == 0
    assert progress.open_irr == 0
    assert progress.is_fully_settled is False


def _plan(kind: PlanKind) -> InstallmentPlan:
    return InstallmentPlan(
        id="plan-1",
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        company_engine_id=COMPANY_ENGINE_ID,
        invoice_id="INV-1",
        total_principal=MoneyIRR(10_000_000),
        leg_count=1,
        kind=kind,
        gateway_channel_key="snapppay" if kind is PlanKind.BNPL else None,
    )


def test_bnpl_plan_never_accrues_finance_income_or_dunning() -> None:
    # The separation rule, single-sourced: US-541 must call these.
    assert plan_accrues_finance_income(_plan(PlanKind.INTERNAL)) is True
    assert plan_enters_dunning(_plan(PlanKind.INTERNAL)) is True
    assert plan_accrues_finance_income(_plan(PlanKind.BNPL)) is False
    assert plan_enters_dunning(_plan(PlanKind.BNPL)) is False


# -- validation: plan kind + gateway pinning ----------------------------------------


def _plan_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "invoice_id": "INV-543-1",
        "total_principal_toman": 3_000_000,
        "leg_count": 3,
        "interval_days": 30,
        "first_due_date": PAST_JALALI,
    }
    payload.update(overrides)
    return payload


def test_plan_kind_defaults_to_internal() -> None:
    assert validate_plan_payload(_plan_payload()).kind is PlanKind.INTERNAL
    assert validate_plan_payload(_plan_payload()).gateway_channel_key is None


def test_plan_bnpl_requires_gateway() -> None:
    with pytest.raises(InstallmentValidationError) as exc_info:
        validate_plan_payload(_plan_payload(kind="bnpl"))
    assert "gateway_channel_key" in exc_info.value.field_errors


def test_plan_bnpl_with_gateway_validates() -> None:
    validated = validate_plan_payload(
        _plan_payload(kind="bnpl", gateway_channel_key="snapppay")
    )
    assert validated.kind is PlanKind.BNPL
    assert validated.gateway_channel_key == "snapppay"


def test_plan_kind_rejects_unknown() -> None:
    with pytest.raises(InstallmentValidationError) as exc_info:
        validate_plan_payload(_plan_payload(kind="crypto"))
    assert "kind" in exc_info.value.field_errors


def test_plan_gateway_forbidden_on_internal() -> None:
    with pytest.raises(InstallmentValidationError) as exc_info:
        validate_plan_payload(_plan_payload(gateway_channel_key="snapppay"))
    assert "gateway_channel_key" in exc_info.value.field_errors


def test_plan_contract_forbidden_on_bnpl() -> None:
    # No late fee / early settlement on a BNPL plan — the gateway
    # owns collection, so a sale contract is meaningless there.
    with pytest.raises(InstallmentValidationError) as exc_info:
        validate_plan_payload(
            _plan_payload(
                kind="bnpl",
                gateway_channel_key="snapppay",
                contract_id="00000000-0000-0000-0000-000000000001",
            )
        )
    assert "contract_id" in exc_info.value.field_errors


# -- services: BNPL receipt creates the gateway receivable ---------------------------


def test_bnpl_receipt_creates_gateway_payment_with_engine_tranches(world: BnplWorld) -> None:
    plan = world.create_bnpl_plan("INV-543-2")
    receipt = world.bnpl_receipt(plan.id, "INV-543-2")
    assert receipt.gateway_payment_id is not None
    assert receipt.gateway_channel_key == "snapppay"
    assert receipt.gateway_reference == "SP-INV-543-2"
    assert receipt.bnpl_status is not None and receipt.bnpl_status.value == "active"

    payment = world.settlement_repo.get_payment(
        INSTALLATION_ID, receipt.gateway_payment_id
    )
    assert payment is not None
    # Gross = the receipt amount; fee + VAT from the CONTRACT (10%),
    # never hard-coded: 10,000,000 gross → 1,000,000 fee + 100,000
    # VAT → 8,900,000 net.
    assert payment.gross_irr == 10_000_000
    assert payment.fee_irr == 1_000_000
    assert payment.fee_vat_irr == 100_000
    assert payment.net_irr == 8_900_000
    assert payment.status is GatewayPaymentStatus.SCHEDULED
    assert payment.gateway_transaction_id == "SP-INV-543-2"
    assert payment.tryton_journal_id == 4242

    tranches = list(
        world.settlement_repo.list_tranches_for_payment(
            INSTALLATION_ID, payment.id
        )
    )
    # Snapp Pay 4×25% over 4 months, derived by the engine.
    assert len(tranches) == 4
    assert [t.tranche_index for t in tranches] == [1, 2, 3, 4]
    assert sum(t.net_irr for t in tranches) == 8_900_000
    assert all(t.net_irr == 2_225_000 for t in tranches)
    assert all(t.state is TrancheState.EXPECTED for t in tranches)


def test_bnpl_receipt_allocates_to_the_overdue_leg_first(world: BnplWorld) -> None:
    plan = world.create_bnpl_plan("INV-543-3")
    receipt = world.bnpl_receipt(plan.id, "INV-543-3")
    # 10,000,000 IRR covers exactly the overdue first leg (the
    # same allocation rule as internal receipts).
    assert len(receipt.allocations) == 1
    assert receipt.allocations[0].amount.amount == 10_000_000
    assert receipt.advance.amount == 0
    legs = world.repo.list_legs(plan.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID)
    assert receipt.allocations[0].leg_id == legs[0].id


def test_bnpl_receipt_digipay_lump_sum_single_tranche(world: BnplWorld) -> None:
    plan = world.create_bnpl_plan("INV-543-4", gateway="digipay")
    receipt = world.bnpl_receipt(
        plan.id, "INV-543-4", gateway="digipay", gateway_reference="DP-543-4"
    )
    assert receipt.gateway_payment_id is not None
    payment = world.settlement_repo.get_payment(
        INSTALLATION_ID, receipt.gateway_payment_id
    )
    assert payment is not None
    # DigiPay contract: 6% fee → 600,000 fee + 60,000 VAT → 9,340,000 net.
    assert payment.fee_irr == 600_000
    assert payment.fee_vat_irr == 60_000
    assert payment.net_irr == 9_340_000
    tranches = list(
        world.settlement_repo.list_tranches_for_payment(
            INSTALLATION_ID, payment.id
        )
    )
    assert len(tranches) == 1
    assert tranches[0].net_irr == 9_340_000
    # Due 20 business days after the 2026-11-20 confirmation.
    assert tranches[0].expected_date > datetime.date(2026, 11, 20)


def test_bnpl_receipt_duplicate_reference_is_a_409(world: BnplWorld) -> None:
    plan = world.create_bnpl_plan("INV-543-5")
    world.bnpl_receipt(plan.id, "INV-543-5")
    with pytest.raises(SettlementPaymentExistsError):
        world.bnpl_receipt(plan.id, "INV-543-5")
    # Nothing stored for the duplicate: still exactly one receipt.
    receipts = world.repo.list_receipts(
        plan.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID
    )
    assert len(receipts) == 1


def test_bnpl_receipt_unknown_gateway_fails_closed(world: BnplWorld) -> None:
    plan = world.create_bnpl_plan("INV-543-6", gateway="unknown-gateway")
    with pytest.raises(IntegrationChannelNotFoundError):
        world.bnpl_receipt(
            plan.id, "INV-543-6", gateway="unknown-gateway", gateway_reference="X-1"
        )
    receipts = world.repo.list_receipts(
        plan.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID
    )
    assert receipts == []


def test_bnpl_receipt_without_settlement_service_refuses() -> None:
    # Fail-closed: no engine, no BNPL receipt (a receipt with no
    # gateway payment behind it must never exist).
    repo = FakeInstallmentRepository()
    commands = InstallmentCommandService(repo)
    plan = commands.create_plan(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        company_engine_id=COMPANY_ENGINE_ID,
        payload=_plan_payload(kind="bnpl", gateway_channel_key="snapppay"),
    )
    with pytest.raises(InstallmentBnplUnavailableError):
        commands.record_receipt(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            plan_id=plan.id,
            payload={
                "invoice_id": "INV-543-1",
                "journal_move_id": 1,
                "amount_toman": 1_000_000,
                "received_date": TODAY_JALALI,
                "payment_method": "bnpl",
                "gateway": "snapppay",
                "gateway_reference": "SP-1",
            },
        )


# -- services: the plan-kind guard ---------------------------------------------------


def test_bnpl_receipt_on_internal_plan_refused(world: BnplWorld) -> None:
    plan = world.create_internal_plan("INV-543-7")
    with pytest.raises(InstallmentBnplPlanRequiredError):
        world.bnpl_receipt(plan.id, "INV-543-7")


def test_cash_receipt_on_bnpl_plan_refused(world: BnplWorld) -> None:
    plan = world.create_bnpl_plan("INV-543-8")
    with pytest.raises(InstallmentBnplPlanMethodError):
        world.commands.record_receipt(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            plan_id=plan.id,
            payload={
                "invoice_id": "INV-543-8",
                "journal_move_id": 7,
                "amount_toman": 1_000_000,
                "received_date": TODAY_JALALI,
                "payment_method": "cash",
            },
            today=TODAY,
        )


def test_bnpl_receipt_gateway_mismatch_refused(world: BnplWorld) -> None:
    plan = world.create_bnpl_plan("INV-543-9", gateway="snapppay")
    with pytest.raises(InstallmentBnplGatewayMismatchError):
        world.bnpl_receipt(
            plan.id, "INV-543-9", gateway="digipay", gateway_reference="DP-9"
        )


# -- services: kind filter + BNPL leg lifecycle in the query service -----------------


def test_list_plans_filters_by_kind(world: BnplWorld) -> None:
    world.create_internal_plan("INV-543-10")
    world.create_bnpl_plan("INV-543-11")
    bnpl = world.queries.list_plans(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        kind=PlanKind.BNPL,
    )
    assert [s.plan.invoice_id for s in bnpl] == ["INV-543-11"]
    internal = world.queries.list_plans(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        kind=PlanKind.INTERNAL,
    )
    assert [s.plan.invoice_id for s in internal] == ["INV-543-10"]
    all_plans = world.queries.list_plans(
        tenant_id=TENANT_ID, installation_id=INSTALLATION_ID
    )
    assert len(all_plans) == 2


def test_bnpl_plan_never_overdue_even_past_due(world: BnplWorld) -> None:
    # All three legs are past due on 2026-11-20 (first due
    # 2026-10-21, 30-day interval) — an internal plan would be
    # overdue; a BNPL plan stays active (the gateway owns collection).
    plan = world.create_bnpl_plan("INV-543-12")
    detail = world.queries.get_plan_detail(
        plan.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID, today=TODAY
    )
    assert detail.status is PlanStatus.ACTIVE
    assert [leg.status for leg in detail.legs] == [
        BnplLegStatus.EXPECTED,
        BnplLegStatus.EXPECTED,
        BnplLegStatus.EXPECTED,
    ]
    assert detail.gateway_receivable.amount == 0
    assert detail.gateway_settled.amount == 0


def test_bnpl_leg_lifecycle_expected_to_paid(world: BnplWorld) -> None:
    plan = world.create_bnpl_plan("INV-543-13")
    # Record on 2026-12-21, when all three legs are due/overdue —
    # the allocation rule never prepays an expected leg, so the
    # full 30,000,000 IRR receipt only covers all three legs once
    # the third leg (due 2026-12-20) is due.
    receipt = world.bnpl_receipt(
        plan.id,
        "INV-543-13",
        today=datetime.date(2026, 12, 21),
        amount_toman=3_000_000,
    )
    assert receipt.gateway_payment_id is not None
    assert len(receipt.allocations) == 3
    assert receipt.advance.amount == 0
    tranches = list(
        world.settlement_repo.list_tranches_for_payment(
            INSTALLATION_ID, receipt.gateway_payment_id
        )
    )
    assert len(tranches) == 4

    def statuses() -> list[LegStatus | BnplLegStatus]:
        detail = world.queries.get_plan_detail(
            plan.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID, today=TODAY
        )
        return [leg.status for leg in detail.legs]

    # Gateway confirmed: all three legs covered, no tranche settled.
    assert statuses() == [
        BnplLegStatus.GATEWAY_CONFIRMED,
        BnplLegStatus.GATEWAY_CONFIRMED,
        BnplLegStatus.GATEWAY_CONFIRMED,
    ]
    detail = world.queries.get_plan_detail(
        plan.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID, today=TODAY
    )
    assert detail.status is PlanStatus.ACTIVE
    # 30,000,000 gross → 3,000,000 fee + 300,000 VAT → 26,700,000 net.
    assert detail.gateway_receivable.amount == 26_700_000
    assert detail.gateway_settled.amount == 0

    # Settle the first tranche: partial settlement allocates per tranche.
    world.settle_tranche(tranches[0].id)
    assert statuses() == [
        BnplLegStatus.SETTLED_PER_TRANCHE,
        BnplLegStatus.SETTLED_PER_TRANCHE,
        BnplLegStatus.SETTLED_PER_TRANCHE,
    ]
    detail = world.queries.get_plan_detail(
        plan.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID, today=TODAY
    )
    assert detail.gateway_settled.amount == 6_675_000
    assert detail.gateway_receivable.amount == 20_025_000
    assert detail.status is PlanStatus.ACTIVE

    # Settle the rest: every leg paid, plan paid.
    for tranche in tranches[1:]:
        world.settle_tranche(tranche.id)
    assert statuses() == [
        BnplLegStatus.PAID,
        BnplLegStatus.PAID,
        BnplLegStatus.PAID,
    ]
    detail = world.queries.get_plan_detail(
        plan.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID, today=TODAY
    )
    assert detail.status is PlanStatus.PAID
    assert detail.gateway_settled.amount == 26_700_000
    assert detail.gateway_receivable.amount == 0
    assert detail.outstanding.amount == 0


def test_bnpl_leg_partial_receipt_only_covers_its_leg(world: BnplWorld) -> None:
    plan = world.create_bnpl_plan("INV-543-14")
    world.bnpl_receipt(plan.id, "INV-543-14")  # 10,000,000 IRR → leg 1 only
    detail = world.queries.get_plan_detail(
        plan.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID, today=TODAY
    )
    assert [leg.status for leg in detail.legs] == [
        BnplLegStatus.GATEWAY_CONFIRMED,
        BnplLegStatus.EXPECTED,
        BnplLegStatus.EXPECTED,
    ]
    assert detail.total_received.amount == 10_000_000
    assert detail.outstanding.amount == 20_000_000


def test_bnpl_query_falls_back_to_the_receipt_without_settlement_repo(
    world: BnplWorld,
) -> None:
    # Bare query service (no settlement repo wired): the receipt
    # alone decides — gateway_confirmed with a live allocation,
    # expected without; gateway totals read zero.
    plan = world.create_bnpl_plan("INV-543-15")
    world.bnpl_receipt(plan.id, "INV-543-15")
    bare = InstallmentQueryService(world.repo)
    detail = bare.get_plan_detail(
        plan.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID, today=TODAY
    )
    assert [leg.status for leg in detail.legs] == [
        BnplLegStatus.GATEWAY_CONFIRMED,
        BnplLegStatus.EXPECTED,
        BnplLegStatus.EXPECTED,
    ]
    assert detail.gateway_receivable.amount == 0
    assert detail.gateway_settled.amount == 0


# -- services: rejection / cancellation ----------------------------------------------


def test_reject_bnpl_receipt_returns_leg_to_expected(world: BnplWorld) -> None:
    plan = world.create_bnpl_plan("INV-543-16")
    receipt = world.bnpl_receipt(plan.id, "INV-543-16")
    assert receipt.gateway_payment_id is not None
    updated = world.commands.reject_bnpl_receipt(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        plan_id=plan.id,
        receipt_id=receipt.id,
        reason="customer not eligible",
        actor="operator-1",
    )
    assert updated.bnpl_status is not None and updated.bnpl_status.value == "rejected"

    # The gateway payment is cancelled; its tranches are cancelled.
    payment = world.settlement_repo.get_payment(
        INSTALLATION_ID, receipt.gateway_payment_id
    )
    assert payment is not None
    assert payment.status is GatewayPaymentStatus.CANCELLED
    tranches = list(
        world.settlement_repo.list_tranches_for_payment(
            INSTALLATION_ID, payment.id
        )
    )
    assert all(t.state is TrancheState.CANCELLED for t in tranches)

    # The leg is expected again; the receipt no longer counts.
    detail = world.queries.get_plan_detail(
        plan.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID, today=TODAY
    )
    assert [leg.status for leg in detail.legs] == [
        BnplLegStatus.EXPECTED,
        BnplLegStatus.EXPECTED,
        BnplLegStatus.EXPECTED,
    ]
    assert detail.total_received.amount == 0
    assert detail.outstanding.amount == 30_000_000
    assert detail.gateway_receivable.amount == 0

    # Exactly ONE review item (the settlement engine does not file
    # a second one on top).
    items = list(world.review_queue.items.values())
    assert len(items) == 1
    item = items[0]
    assert item.item_type is ReviewItemType.SETTLEMENT_VARIANCE
    assert item.subtype == "bnpl_gateway_rejection"
    assert item.channel == "snapppay"
    assert item.amount_irr == 10_000_000
    assert item.severity.value == "warning"
    assert "customer not eligible" in item.reason


def test_reject_bnpl_receipt_after_partial_settlement_is_critical(world: BnplWorld) -> None:
    plan = world.create_bnpl_plan("INV-543-17")
    receipt = world.bnpl_receipt(plan.id, "INV-543-17", amount_toman=3_000_000)
    assert receipt.gateway_payment_id is not None
    tranches = list(
        world.settlement_repo.list_tranches_for_payment(
            INSTALLATION_ID, receipt.gateway_payment_id
        )
    )
    world.settle_tranche(tranches[0].id)  # 6,675,000 IRR already settled
    world.commands.reject_bnpl_receipt(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        plan_id=plan.id,
        receipt_id=receipt.id,
        reason="gateway cancelled the sale",
    )
    payment = world.settlement_repo.get_payment(
        INSTALLATION_ID, receipt.gateway_payment_id
    )
    assert payment is not None
    assert payment.status is GatewayPaymentStatus.PARTIALLY_CANCELLED
    assert payment.refunds_deducted_irr == 6_675_000
    items = list(world.review_queue.items.values())
    assert len(items) == 1
    assert items[0].severity.value == "critical"
    assert "6675000" in items[0].reason


def test_reject_bnpl_receipt_twice_is_a_409(world: BnplWorld) -> None:
    plan = world.create_bnpl_plan("INV-543-18")
    receipt = world.bnpl_receipt(plan.id, "INV-543-18")
    world.commands.reject_bnpl_receipt(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        plan_id=plan.id,
        receipt_id=receipt.id,
        reason="rejected",
    )
    with pytest.raises(InstallmentBnplAlreadyRejectedError):
        world.commands.reject_bnpl_receipt(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            plan_id=plan.id,
            receipt_id=receipt.id,
            reason="rejected again",
        )
    # Still exactly one review item.
    assert len(world.review_queue.items) == 1


def test_reject_non_bnpl_receipt_refused(world: BnplWorld) -> None:
    plan = world.create_internal_plan("INV-543-19")
    receipt = world.commands.record_receipt(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        plan_id=plan.id,
        payload={
            "invoice_id": "INV-543-19",
            "journal_move_id": 9,
            "amount_toman": 1_000_000,
            "received_date": TODAY_JALALI,
            "payment_method": "cash",
        },
        today=TODAY,
    )
    with pytest.raises(InstallmentNotBnplReceiptError):
        world.commands.reject_bnpl_receipt(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            plan_id=plan.id,
            receipt_id=receipt.id,
            reason="not bnpl",
        )


def test_reject_unknown_receipt_is_a_404(world: BnplWorld) -> None:
    plan = world.create_bnpl_plan("INV-543-20")
    with pytest.raises(InstallmentReceiptNotFoundError):
        world.commands.reject_bnpl_receipt(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            plan_id=plan.id,
            receipt_id="00000000-0000-0000-0000-000000000000",
            reason="nope",
        )


def test_reject_when_payment_already_cancelled_in_settlement(world: BnplWorld) -> None:
    # The gateway cancelled the payment through the settlement API
    # directly; rejecting the receipt still marks it dead and files
    # the item (no error).
    plan = world.create_bnpl_plan("INV-543-21")
    receipt = world.bnpl_receipt(plan.id, "INV-543-21")
    assert receipt.gateway_payment_id is not None
    world.settlement.cancel_payment(
        INSTALLATION_ID,
        CancelGatewayPayment(payment_id=receipt.gateway_payment_id, reason="gateway-side"),
    )
    updated = world.commands.reject_bnpl_receipt(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        plan_id=plan.id,
        receipt_id=receipt.id,
        reason="gateway cancelled",
    )
    assert updated.bnpl_status is not None and updated.bnpl_status.value == "rejected"
    items = list(world.review_queue.items.values())
    assert len(items) == 1
    assert "already cancelled" in items[0].reason


def test_rejected_leg_can_be_paid_again(world: BnplWorld) -> None:
    # After a rejection the leg is expected again — a fresh BNPL
    # receipt (a new gateway transaction) allocates to it again.
    plan = world.create_bnpl_plan("INV-543-22")
    first = world.bnpl_receipt(plan.id, "INV-543-22")
    world.commands.reject_bnpl_receipt(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        plan_id=plan.id,
        receipt_id=first.id,
        reason="first attempt rejected",
    )
    second = world.bnpl_receipt(
        plan.id, "INV-543-22", gateway_reference="SP-INV-543-22-retry"
    )
    legs = world.repo.list_legs(plan.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID)
    assert second.allocations[0].leg_id == legs[0].id
    detail = world.queries.get_plan_detail(
        plan.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID, today=TODAY
    )
    assert detail.legs[0].status is BnplLegStatus.GATEWAY_CONFIRMED
    assert detail.total_received.amount == 10_000_000


def test_receipt_validation_still_enforces_the_linkage_guard_for_bnpl(
    world: BnplWorld,
) -> None:
    # The linkage guard runs before anything BNPL-specific: a BNPL
    # receipt without the invoice/journal refs is refused the same
    # way as any other receipt.
    plan = world.create_bnpl_plan("INV-543-23")
    with pytest.raises(InstallmentLinkageError):
        world.commands.record_receipt(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            plan_id=plan.id,
            payload={
                "amount_toman": 1_000_000,
                "payment_method": "bnpl",
                "gateway": "snapppay",
                "gateway_reference": "SP-23",
            },
            today=TODAY,
        )
