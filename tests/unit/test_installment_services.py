"""Unit tests for the installment services (US-540 + US-542, Phase 5-5 ۵ج).

The command + query services over an in-memory fake repository
(:class:`FakeInstallmentRepository`, also used by the REST tests in
``tests.interfaces.test_rest_installments``) and the cheque fake
from ``tests.unit.test_cheque_services``. No DB, no engine —
US-540 creates no journal and makes no engine call; US-542 links
cheques through the registry fake (the live engine is covered by
the contract tests).

Covers: plan creation (terms + explicit legs, one-plan-per-invoice,
dangling contract ref), receipt recording with the linkage guard +
invoice match + allocation, cheque receipts (pending_clearance,
the receivable-state / leg-link / amount / reuse guards, the
single-leg rule), the collection hook (clear → collected, bounce
→ unpaid), and the query service's computed statuses/totals.
"""

from __future__ import annotations

import copy
import datetime
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from hesabyar.application.commands.installments import (
    InstallmentCollectionHook,
    InstallmentCommandService,
)
from hesabyar.application.queries.installments import InstallmentQueryService
from hesabyar.domain.cheques.errors import ChequeNotFoundError
from hesabyar.domain.cheques.models import (
    Cheque,
    ChequeKind,
    ChequePurpose,
    ChequeState,
    InstallmentLegStatus,
)
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.domain.installments.errors import (
    InstallmentChequeAlreadyUsedError,
    InstallmentChequeAmountError,
    InstallmentChequeLinkError,
    InstallmentChequeNotReceivableError,
    InstallmentContractNotFoundError,
    InstallmentInvoiceMismatchError,
    InstallmentLinkageError,
    InstallmentPlanExistsError,
    InstallmentPlanNotFoundError,
)
from hesabyar.domain.installments.models import (
    BnplReceiptStatus,
    InstallmentLeg,
    InstallmentPlan,
    InstallmentReceipt,
    InstallmentSaleContract,
    LateFeeAssessment,
    LegStatus,
    PlanKind,
    PlanStatus,
    ScheduleAmendment,
)
from tests.unit.test_cheque_services import FakeChequeRepository

TENANT_ID = "00000000-0000-0000-0000-000000000540"
INSTALLATION_ID = "00000000-0000-0000-0000-000000000541"
COMPANY_ENGINE_ID = 540

TODAY = datetime.date(2026, 11, 20)
PAST = datetime.date(2026, 10, 21)
FUTURE = datetime.date(2027, 2, 18)

#: Jalali for 2026-10-21 / 2026-11-20 / 2027-02-18.
PAST_JALALI = "1405-07-29"
TODAY_JALALI = "1405-08-29"
FUTURE_JALALI = "1405-11-29"


@dataclass
class FakeInstallmentRepository:
    """In-memory installment repository (tenant scoping mirrors the SQL one)."""

    plans: dict[str, InstallmentPlan] = field(default_factory=dict)
    legs: dict[str, InstallmentLeg] = field(default_factory=dict)
    receipts: dict[str, InstallmentReceipt] = field(default_factory=dict)
    contracts: dict[str, InstallmentSaleContract] = field(default_factory=dict)
    assessments: dict[str, LateFeeAssessment] = field(default_factory=dict)
    amendments: dict[str, ScheduleAmendment] = field(default_factory=dict)

    def create_plan(
        self, plan: InstallmentPlan, legs: Sequence[InstallmentLeg]
    ) -> InstallmentPlan:
        self.plans[plan.id] = copy.deepcopy(plan)
        for leg in legs:
            self.legs[leg.id] = copy.deepcopy(leg)
        return plan

    def get_plan(
        self, plan_id: str, *, tenant_id: str, installation_id: str
    ) -> InstallmentPlan | None:
        plan = self.plans.get(plan_id)
        if plan is None or plan.tenant_id != tenant_id:
            return None
        if plan.installation_id != installation_id:
            return None
        return copy.deepcopy(plan)

    def get_plan_by_invoice(
        self, invoice_id: str, *, tenant_id: str, installation_id: str
    ) -> InstallmentPlan | None:
        for plan in self.plans.values():
            if (
                plan.invoice_id == invoice_id
                and plan.tenant_id == tenant_id
                and plan.installation_id == installation_id
            ):
                return copy.deepcopy(plan)
        return None

    def list_plans(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        invoice_id: str | None = None,
        kind: PlanKind | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[InstallmentPlan]:
        found = [
            copy.deepcopy(plan)
            for plan in self.plans.values()
            if plan.tenant_id == tenant_id and plan.installation_id == installation_id
        ]
        if invoice_id is not None:
            found = [p for p in found if p.invoice_id == invoice_id]
        if kind is not None:
            found = [p for p in found if p.kind is kind]
        found.sort(key=lambda p: p.created_at, reverse=True)
        return found[offset : offset + limit]

    def list_legs(
        self, plan_id: str, *, tenant_id: str, installation_id: str
    ) -> list[InstallmentLeg]:
        if self.get_plan(plan_id, tenant_id=tenant_id, installation_id=installation_id) is None:
            return []
        found = [copy.deepcopy(leg) for leg in self.legs.values() if leg.plan_id == plan_id]
        found.sort(key=lambda leg: leg.sequence)
        return found

    def get_leg(
        self, leg_id: str, *, tenant_id: str, installation_id: str
    ) -> InstallmentLeg | None:
        leg = self.legs.get(leg_id)
        if leg is None:
            return None
        if (
            self.get_plan(leg.plan_id, tenant_id=tenant_id, installation_id=installation_id)
            is None
        ):
            return None
        return copy.deepcopy(leg)

    def create_receipt(self, receipt: InstallmentReceipt) -> InstallmentReceipt:
        self.receipts[receipt.id] = copy.deepcopy(receipt)
        return receipt

    def list_receipts(
        self, plan_id: str, *, tenant_id: str, installation_id: str
    ) -> list[InstallmentReceipt]:
        found = [
            copy.deepcopy(receipt)
            for receipt in self.receipts.values()
            if receipt.plan_id == plan_id
            and receipt.tenant_id == tenant_id
            and receipt.installation_id == installation_id
        ]
        found.sort(key=lambda r: r.created_at)
        return found

    def get_receipt(
        self, receipt_id: str, *, tenant_id: str, installation_id: str
    ) -> InstallmentReceipt | None:
        receipt = self.receipts.get(receipt_id)
        if receipt is None or receipt.tenant_id != tenant_id:
            return None
        if receipt.installation_id != installation_id:
            return None
        return copy.deepcopy(receipt)

    def update_receipt_bnpl_status(
        self,
        receipt_id: str,
        *,
        tenant_id: str,
        installation_id: str,
        bnpl_status: BnplReceiptStatus,
    ) -> InstallmentReceipt | None:
        receipt = self.get_receipt(
            receipt_id, tenant_id=tenant_id, installation_id=installation_id
        )
        if receipt is None:
            return None
        receipt.bnpl_status = bnpl_status
        self.receipts[receipt_id] = copy.deepcopy(receipt)
        return copy.deepcopy(receipt)

    def get_receipt_by_cheque(
        self, cheque_id: str, *, tenant_id: str, installation_id: str
    ) -> InstallmentReceipt | None:
        for receipt in self.receipts.values():
            if (
                receipt.cheque_id == cheque_id
                and receipt.tenant_id == tenant_id
                and receipt.installation_id == installation_id
            ):
                return copy.deepcopy(receipt)
        return None

    def save_receipt(self, receipt: InstallmentReceipt) -> InstallmentReceipt:
        if receipt.id not in self.receipts:
            raise ValueError(f"Installment receipt {receipt.id!r} does not exist.")
        self.receipts[receipt.id] = copy.deepcopy(receipt)
        return receipt

    def create_contract(self, contract: InstallmentSaleContract) -> InstallmentSaleContract:
        self.contracts[contract.id] = copy.deepcopy(contract)
        return contract

    def get_contract(
        self, contract_id: str, *, tenant_id: str, installation_id: str
    ) -> InstallmentSaleContract | None:
        contract = self.contracts.get(contract_id)
        if contract is None or contract.tenant_id != tenant_id:
            return None
        if contract.installation_id != installation_id:
            return None
        return copy.deepcopy(contract)

    def list_contracts(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        code: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[InstallmentSaleContract]:
        found = [
            copy.deepcopy(contract)
            for contract in self.contracts.values()
            if contract.tenant_id == tenant_id and contract.installation_id == installation_id
        ]
        if code is not None:
            found = [c for c in found if c.code == code]
        found.sort(key=lambda c: c.created_at, reverse=True)
        return found[offset : offset + limit]

    # -- finance (US-541) ----------------------------------------------------

    def update_leg_principals(
        self,
        plan_id: str,
        principals: Mapping[str, MoneyIRR],
        *,
        tenant_id: str,
        installation_id: str,
    ) -> None:
        if self.get_plan(plan_id, tenant_id=tenant_id, installation_id=installation_id) is None:
            return
        for leg_id, principal in principals.items():
            leg = self.legs.get(leg_id)
            if leg is not None and leg.plan_id == plan_id:
                leg.principal = principal

    def update_applied_advance(
        self,
        plan_id: str,
        applied_advance: MoneyIRR,
        *,
        tenant_id: str,
        installation_id: str,
    ) -> None:
        plan = self.plans.get(plan_id)
        if (
            plan is None
            or plan.tenant_id != tenant_id
            or plan.installation_id != installation_id
        ):
            return
        plan.applied_advance = applied_advance

    def create_late_fee_assessment(self, assessment: LateFeeAssessment) -> LateFeeAssessment:
        self.assessments[assessment.id] = copy.deepcopy(assessment)
        return assessment

    def list_late_fee_assessments(
        self, leg_id: str, *, tenant_id: str, installation_id: str
    ) -> list[LateFeeAssessment]:
        found = [
            copy.deepcopy(assessment)
            for assessment in self.assessments.values()
            if assessment.leg_id == leg_id
            and assessment.tenant_id == tenant_id
            and assessment.installation_id == installation_id
        ]
        found.sort(key=lambda a: (a.assessed_through.gregorian_date, a.created_at), reverse=True)
        return found

    def create_schedule_amendment(self, amendment: ScheduleAmendment) -> ScheduleAmendment:
        self.amendments[amendment.id] = copy.deepcopy(amendment)
        return amendment

    def list_schedule_amendments(
        self, plan_id: str, *, tenant_id: str, installation_id: str
    ) -> list[ScheduleAmendment]:
        found = [
            copy.deepcopy(amendment)
            for amendment in self.amendments.values()
            if amendment.plan_id == plan_id
            and amendment.tenant_id == tenant_id
            and amendment.installation_id == installation_id
        ]
        found.sort(key=lambda a: a.created_at)
        return found


def _service(repo: FakeInstallmentRepository | None = None) -> InstallmentCommandService:
    return InstallmentCommandService(repo or FakeInstallmentRepository())


def _queries(repo: FakeInstallmentRepository) -> InstallmentQueryService:
    return InstallmentQueryService(repo)


def _plan_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "invoice_id": "INV-540-1",
        "total_principal_toman": 3_000_000,
        "leg_count": 3,
        "interval_days": 30,
        "first_due_date": PAST_JALALI,
    }
    payload.update(overrides)
    return payload


def _create_plan(
    service: InstallmentCommandService, **overrides: object
) -> InstallmentPlan:
    return service.create_plan(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        company_engine_id=COMPANY_ENGINE_ID,
        payload=_plan_payload(**overrides),
    )


def _receipt_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "invoice_id": "INV-540-1",
        "journal_move_id": 42,
        "amount_toman": 1_000_000,
        "received_date": TODAY_JALALI,
        "payment_method": "cash",
    }
    payload.update(overrides)
    return payload


def _contract_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "code": "STD-540",
        "name": "قرارداد استاندارد",
        "version": 1,
        "effective_from": "1405-08-01",
        "late_fee_basis": "daily_percent",
        "late_fee_rate": "0.02",
        "late_fee_grace_days": 3,
        "penalty_account_engine_id": 77,
    }
    payload.update(overrides)
    return payload


# -- plan creation ---------------------------------------------------------------


def test_create_plan_terms_based_generates_legs() -> None:
    repo = FakeInstallmentRepository()
    plan = _create_plan(_service(repo))
    assert plan.total_principal.amount == 30_000_000
    assert plan.leg_count == 3
    assert plan.auto_post is False
    legs = repo.list_legs(plan.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID)
    assert [leg.principal.amount for leg in legs] == [10_000_000, 10_000_000, 10_000_000]
    assert [leg.sequence for leg in legs] == [1, 2, 3]
    assert legs[0].due_date.gregorian_date == PAST
    assert legs[1].due_date.gregorian_date == datetime.date(2026, 11, 20)
    assert legs[2].due_date.gregorian_date == datetime.date(2026, 12, 20)


def test_create_plan_explicit_legs() -> None:
    repo = FakeInstallmentRepository()
    plan = _create_plan(
        _service(repo),
        invoice_id="INV-540-2",
        legs=[
            {"due_date": PAST_JALALI, "principal_toman": 500_000},
            {"due_date": FUTURE_JALALI, "principal_toman": 2_500_000},
        ],
        total_principal_toman=None,
        leg_count=None,
        interval_days=None,
        first_due_date=None,
    )
    # The None-valued terms keys must not count as "terms present" —
    # the payload builder drops them; here the service sees them as
    # absent because validate_plan_payload treats None as absent.
    assert plan.total_principal.amount == 30_000_000
    legs = repo.list_legs(plan.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID)
    assert [leg.principal.amount for leg in legs] == [5_000_000, 25_000_000]


def test_create_plan_second_plan_for_same_invoice_is_409() -> None:
    service = _service()
    _create_plan(service)
    with pytest.raises(InstallmentPlanExistsError) as exc_info:
        _create_plan(service)
    assert exc_info.value.code == "INSTALLMENT_PLAN_EXISTS"


def test_create_plan_with_dangling_contract_ref_is_404() -> None:
    with pytest.raises(InstallmentContractNotFoundError):
        _create_plan(_service(), contract_id="00000000-0000-0000-0000-000000000999")


def test_create_plan_with_real_contract_links_it() -> None:
    repo = FakeInstallmentRepository()
    service = _service(repo)
    contract = service.create_contract(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        company_engine_id=COMPANY_ENGINE_ID,
        payload=_contract_payload(),
    )
    plan = _create_plan(service, contract_id=contract.id)
    assert plan.contract_id == contract.id


# -- receipts + the linkage guard ----------------------------------------------------


def test_record_receipt_allocates_to_oldest_overdue_leg() -> None:
    repo = FakeInstallmentRepository()
    service = _service(repo)
    plan = _create_plan(service)
    receipt = service.record_receipt(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        plan_id=plan.id,
        payload=_receipt_payload(),
        today=TODAY,
    )
    assert receipt.amount.amount == 10_000_000
    assert len(receipt.allocations) == 1
    assert receipt.allocations[0].amount.amount == 10_000_000
    assert receipt.advance.amount == 0
    legs = repo.list_legs(plan.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID)
    assert receipt.allocations[0].leg_id == legs[0].id


def test_record_receipt_overpayment_becomes_advance() -> None:
    repo = FakeInstallmentRepository()
    service = _service(repo)
    plan = _create_plan(service)
    receipt = service.record_receipt(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        plan_id=plan.id,
        payload=_receipt_payload(amount_toman=5_000_000),
        today=TODAY,
    )
    # Legs 1 (overdue) + 2 (due today) take 10,000,000 each; leg 3 is
    # expected, so the remaining 30,000,000 is advance — never prepayment.
    assert receipt.allocated_irr == 20_000_000
    assert receipt.advance.amount == 30_000_000


def test_second_receipt_allocates_against_updated_totals() -> None:
    repo = FakeInstallmentRepository()
    service = _service(repo)
    plan = _create_plan(service)
    service.record_receipt(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        plan_id=plan.id,
        payload=_receipt_payload(amount_toman=500_000),
        today=TODAY,
    )
    second = service.record_receipt(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        plan_id=plan.id,
        payload=_receipt_payload(amount_toman=500_000),
        today=TODAY,
    )
    # First receipt paid half of leg 1; the second pays the other half.
    legs = repo.list_legs(plan.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID)
    assert [(a.leg_id, a.amount.amount) for a in second.allocations] == [
        (legs[0].id, 5_000_000)
    ]
    assert second.advance.amount == 0


def test_record_receipt_without_invoice_ref_is_rejected_by_the_guard() -> None:
    repo = FakeInstallmentRepository()
    service = _service(repo)
    plan = _create_plan(service)
    with pytest.raises(InstallmentLinkageError) as exc_info:
        service.record_receipt(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            plan_id=plan.id,
            payload=_receipt_payload(invoice_id=""),
            today=TODAY,
        )
    assert exc_info.value.missing == ("invoice_id",)
    assert repo.list_receipts(plan.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID) == []


def test_record_receipt_without_journal_ref_is_rejected_by_the_guard() -> None:
    repo = FakeInstallmentRepository()
    service = _service(repo)
    plan = _create_plan(service)
    with pytest.raises(InstallmentLinkageError) as exc_info:
        service.record_receipt(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            plan_id=plan.id,
            payload=_receipt_payload(journal_move_id=0),
            today=TODAY,
        )
    assert exc_info.value.missing == ("journal_move_id",)
    assert repo.list_receipts(plan.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID) == []


def test_record_receipt_with_wrong_invoice_is_rejected() -> None:
    repo = FakeInstallmentRepository()
    service = _service(repo)
    plan = _create_plan(service)
    with pytest.raises(InstallmentInvoiceMismatchError) as exc_info:
        service.record_receipt(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            plan_id=plan.id,
            payload=_receipt_payload(invoice_id="INV-OTHER"),
            today=TODAY,
        )
    assert exc_info.value.code == "INSTALLMENT_INVOICE_MISMATCH"
    assert repo.list_receipts(plan.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID) == []


def test_record_receipt_on_missing_plan_is_404() -> None:
    with pytest.raises(InstallmentPlanNotFoundError):
        _service().record_receipt(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            plan_id="00000000-0000-0000-0000-000000000999",
            payload=_receipt_payload(),
            today=TODAY,
        )


def test_record_receipt_on_other_installation_plan_is_404() -> None:
    repo = FakeInstallmentRepository()
    service = _service(repo)
    plan = _create_plan(service)
    with pytest.raises(InstallmentPlanNotFoundError):
        service.record_receipt(
            tenant_id=TENANT_ID,
            installation_id="00000000-0000-0000-0000-000000000542",
            plan_id=plan.id,
            payload=_receipt_payload(),
            today=TODAY,
        )


# -- query service -------------------------------------------------------------------


def test_plan_detail_computes_leg_statuses_and_totals() -> None:
    repo = FakeInstallmentRepository()
    service = _service(repo)
    plan = _create_plan(service)
    service.record_receipt(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        plan_id=plan.id,
        payload=_receipt_payload(amount_toman=500_000),
        today=TODAY,
    )
    detail = _queries(repo).get_plan_detail(
        plan.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID, today=TODAY
    )
    # Leg 1: half-paid + past due -> overdue. Leg 2: due today, unpaid
    # -> due. Leg 3: future, unpaid -> expected.
    assert [d.status for d in detail.legs] == [
        LegStatus.OVERDUE,
        LegStatus.DUE,
        LegStatus.EXPECTED,
    ]
    assert detail.status is PlanStatus.OVERDUE
    assert detail.total_received.amount == 5_000_000
    assert detail.outstanding.amount == 25_000_000
    assert detail.total_advance.amount == 0


def test_plan_detail_paid_when_all_legs_paid() -> None:
    repo = FakeInstallmentRepository()
    service = _service(repo)
    plan = _create_plan(service)
    # Pay legs 1+2 (due) in full; leg 3 is expected, so a receipt can
    # never pay it — pay it by moving "today" past its due date.
    service.record_receipt(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        plan_id=plan.id,
        payload=_receipt_payload(amount_toman=2_000_000),
        today=TODAY,
    )
    service.record_receipt(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        plan_id=plan.id,
        payload=_receipt_payload(amount_toman=1_000_000),
        today=datetime.date(2026, 12, 21),
    )
    detail = _queries(repo).get_plan_detail(
        plan.id,
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        today=datetime.date(2026, 12, 21),
    )
    assert [d.status for d in detail.legs] == [
        LegStatus.PAID,
        LegStatus.PAID,
        LegStatus.PAID,
    ]
    assert detail.status is PlanStatus.PAID
    assert detail.outstanding.amount == 0


def test_list_plans_status_filter_uses_derived_status() -> None:
    repo = FakeInstallmentRepository()
    service = _service(repo)
    overdue_plan = _create_plan(service)
    _create_plan(service, invoice_id="INV-540-9", first_due_date=FUTURE_JALALI)
    summaries = _queries(repo).list_plans(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        status=PlanStatus.OVERDUE,
        today=TODAY,
    )
    assert [s.plan.id for s in summaries] == [overdue_plan.id]


def test_get_contract_round_trip() -> None:
    repo = FakeInstallmentRepository()
    service = _service(repo)
    contract = service.create_contract(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        company_engine_id=COMPANY_ENGINE_ID,
        payload=_contract_payload(),
    )
    found = _queries(repo).get_contract(
        contract.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID
    )
    assert found.code == "STD-540"
    assert found.late_fee_rate == Decimal("0.02")


def test_leg_lookup_resolves_the_cheque_link_target() -> None:
    # US-542 resolves a cheque's installment_leg_id through get_leg;
    # the link target must be findable (and scoped).
    repo = FakeInstallmentRepository()
    plan = _create_plan(_service(repo))
    legs = repo.list_legs(plan.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID)
    found = repo.get_leg(legs[1].id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID)
    assert found is not None
    assert found.sequence == 2
    assert (
        repo.get_leg(
            legs[1].id,
            tenant_id=TENANT_ID,
            installation_id="00000000-0000-0000-0000-000000000542",
        )
        is None
    )


def test_fiscal_date_import_used_for_explicit_leg_dates() -> None:
    # Sanity: the service stores Gregorian dates from Jalali input.
    repo = FakeInstallmentRepository()
    plan = _create_plan(_service(repo))
    legs = repo.list_legs(plan.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID)
    assert legs[0].due_date == FiscalDate.from_gregorian(PAST)


# -- cheque receipts (US-542) --------------------------------------------------------


def _cheque(
    cheque_id: str | None = None,
    *,
    state: ChequeState = ChequeState.HELD,
    amount_irr: int = 10_000_000,
    kind: ChequeKind = ChequeKind.RECEIVED,
    purpose: ChequePurpose = ChequePurpose.PAYMENT,
    installment_leg_id: str | None = None,
    sayad_id: str = "5420000000000001",
) -> Cheque:
    return Cheque(
        id=cheque_id or str(uuid.uuid4()),
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        company_engine_id=COMPANY_ENGINE_ID,
        kind=kind,
        purpose=purpose,
        sayad_id=sayad_id,
        amount_irr=amount_irr,
        issue_date=FiscalDate.from_gregorian(PAST),
        due_date=FiscalDate.from_gregorian(datetime.date(2027, 1, 5)),
        issuer_name="مشتری نمونه",
        issuer_national_id="0012345678",
        issuer_bank="056 سامان",
        payee_name="کافه ایوان",
        payee_national_id="00123456789",
        order_id="ord-542",
        customer_ref="cust-542",
        sayad_registered=True,
        state=state,
        installment_leg_id=installment_leg_id,
    )


def _cheque_world(
    cheque: Cheque,
) -> tuple[
    FakeInstallmentRepository,
    FakeChequeRepository,
    InstallmentCommandService,
    InstallmentQueryService,
]:
    repo = FakeInstallmentRepository()
    cheques = FakeChequeRepository()
    cheques.create(cheque)
    return (
        repo,
        cheques,
        InstallmentCommandService(repo, cheques),
        InstallmentQueryService(repo, cheques),
    )


def _cheque_receipt_payload(cheque_id: str, **overrides: object) -> dict[str, object]:
    return _receipt_payload(
        payment_method="cheque", cheque_id=cheque_id, **overrides
    )


def test_cheque_receipt_enters_pending_clearance_and_links_the_cheque() -> None:
    cheque = _cheque()
    repo, cheques, service, queries = _cheque_world(cheque)
    plan = _create_plan(service)
    legs = repo.list_legs(plan.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID)
    receipt = service.record_receipt(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        plan_id=plan.id,
        payload=_cheque_receipt_payload(cheque.id),
        today=TODAY,
    )
    assert receipt.cheque_id == cheque.id
    assert receipt.collected_at is None  # not collected until the cheque clears
    assert [(a.leg_id, a.amount.amount) for a in receipt.allocations] == [
        (legs[0].id, 10_000_000)
    ]
    assert receipt.advance.amount == 0
    # The cheque ↔ leg link is set on the cheque row.
    saved = cheques.get(cheque.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID)
    assert saved is not None
    assert saved.installment_leg_id == legs[0].id
    assert saved.applied_amount_irr == 10_000_000
    assert saved.installment_leg_status is InstallmentLegStatus.PENDING_CLEARANCE
    # The leg is pending_clearance — NOT paid, NOT overdue — and the
    # detail shows the linked cheque.
    detail = queries.get_plan_detail(
        plan.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID, today=TODAY
    )
    assert detail.legs[0].status is LegStatus.PENDING_CLEARANCE
    assert detail.legs[0].received.amount == 0
    assert detail.legs[0].pending_clearance.amount == 10_000_000
    assert detail.legs[0].balance.amount == 10_000_000
    assert detail.legs[0].linked_cheque is not None
    assert detail.legs[0].linked_cheque.sayad_id == "5420000000000001"
    assert detail.legs[0].linked_cheque.state is ChequeState.HELD
    assert detail.legs[0].linked_cheque.due_date.gregorian_date == datetime.date(2027, 1, 5)
    assert detail.status is PlanStatus.ACTIVE
    assert detail.total_pending_clearance.amount == 10_000_000
    assert detail.outstanding.amount == 30_000_000


def test_second_receipt_does_not_double_pay_a_pending_leg() -> None:
    cheque = _cheque()
    repo, _, service, _ = _cheque_world(cheque)
    plan = _create_plan(service)
    service.record_receipt(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        plan_id=plan.id,
        payload=_cheque_receipt_payload(cheque.id),
        today=TODAY,
    )
    second = service.record_receipt(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        plan_id=plan.id,
        payload=_receipt_payload(),
        today=TODAY,
    )
    legs = repo.list_legs(plan.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID)
    # Leg 1 is pending (fully covered) — the cash receipt pays leg 2.
    assert [(a.leg_id, a.amount.amount) for a in second.allocations] == [
        (legs[1].id, 10_000_000)
    ]


def test_cheque_receipt_allocates_to_one_leg_only() -> None:
    cheque = _cheque(amount_irr=25_000_000)
    _, _, service, _ = _cheque_world(cheque)
    plan = _create_plan(service)
    receipt = service.record_receipt(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        plan_id=plan.id,
        payload=_cheque_receipt_payload(cheque.id, amount_toman=2_500_000),
        today=TODAY,
    )
    assert len(receipt.allocations) == 1
    assert receipt.allocated_irr == 10_000_000
    assert receipt.advance_irr == 15_000_000


def test_cheque_receipt_with_no_due_leg_is_all_advance_and_unlinked() -> None:
    cheque = _cheque()
    repo, cheques, service, queries = _cheque_world(cheque)
    plan = _create_plan(service, first_due_date=FUTURE_JALALI)
    receipt = service.record_receipt(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        plan_id=plan.id,
        payload=_cheque_receipt_payload(cheque.id),
        today=TODAY,
    )
    assert receipt.allocations == ()
    assert receipt.advance_irr == 10_000_000
    saved = cheques.get(cheque.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID)
    assert saved is not None
    assert saved.installment_leg_id is None  # no leg to link
    detail = queries.get_plan_detail(
        plan.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID, today=TODAY
    )
    assert [d.status for d in detail.legs] == [
        LegStatus.EXPECTED,
        LegStatus.EXPECTED,
        LegStatus.EXPECTED,
    ]
    assert detail.total_pending_clearance.amount == 0


def test_cash_receipt_is_collected_on_receipt() -> None:
    repo = FakeInstallmentRepository()
    service = _service(repo)
    plan = _create_plan(service)
    receipt = service.record_receipt(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        plan_id=plan.id,
        payload=_receipt_payload(),
        today=TODAY,
    )
    assert receipt.collected_at is not None
    assert receipt.collected_at.gregorian_date == TODAY


def test_cheque_receipt_rejects_registered_cheque_with_no_journal_yet() -> None:
    cheque = _cheque(state=ChequeState.REGISTERED)
    _, _, service, _ = _cheque_world(cheque)
    plan = _create_plan(service)
    with pytest.raises(InstallmentChequeNotReceivableError) as exc_info:
        service.record_receipt(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            plan_id=plan.id,
            payload=_cheque_receipt_payload(cheque.id),
            today=TODAY,
        )
    assert exc_info.value.code == "INSTALLMENT_CHEQUE_NOT_RECEIVABLE"


def test_cheque_receipt_rejects_cleared_bounced_and_returned_cheques() -> None:
    for state in (
        ChequeState.CLEARED,
        ChequeState.BOUNCED,
        ChequeState.RETURNED,
        ChequeState.ENDORSED,
        ChequeState.VOIDED,
    ):
        cheque = _cheque(state=state)
        _, _, service, _ = _cheque_world(cheque)
        plan = _create_plan(service)
        with pytest.raises(InstallmentChequeNotReceivableError):
            service.record_receipt(
                tenant_id=TENANT_ID,
                installation_id=INSTALLATION_ID,
                plan_id=plan.id,
                payload=_cheque_receipt_payload(cheque.id),
                today=TODAY,
            )


def test_cheque_receipt_rejects_issued_and_collateral_cheques() -> None:
    for cheque in (
        _cheque(kind=ChequeKind.ISSUED, state=ChequeState.ISSUED),
        _cheque(purpose=ChequePurpose.COLLATERAL),
    ):
        _, _, service, _ = _cheque_world(cheque)
        plan = _create_plan(service)
        with pytest.raises(InstallmentChequeNotReceivableError):
            service.record_receipt(
                tenant_id=TENANT_ID,
                installation_id=INSTALLATION_ID,
                plan_id=plan.id,
                payload=_cheque_receipt_payload(cheque.id),
                today=TODAY,
            )


def test_cheque_receipt_accepts_in_clearing_cheque() -> None:
    cheque = _cheque(state=ChequeState.IN_CLEARING)
    _, _, service, _ = _cheque_world(cheque)
    plan = _create_plan(service)
    receipt = service.record_receipt(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        plan_id=plan.id,
        payload=_cheque_receipt_payload(cheque.id),
        today=TODAY,
    )
    assert receipt.cheque_id == cheque.id


def test_cheque_receipt_rejects_unknown_cheque_as_404() -> None:
    _, _, service, _ = _cheque_world(_cheque())
    plan = _create_plan(service)
    with pytest.raises(ChequeNotFoundError):
        service.record_receipt(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            plan_id=plan.id,
            payload=_cheque_receipt_payload("00000000-0000-0000-0000-000000000999"),
            today=TODAY,
        )


def test_cheque_receipt_rejects_cheque_linked_to_another_leg() -> None:
    cheque = _cheque()
    repo, cheques, service, _ = _cheque_world(cheque)
    plan = _create_plan(service)
    legs = repo.list_legs(plan.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID)
    # Intake-linked to leg 2; the receipt would pay leg 1 (oldest overdue).
    cheque.installment_leg_id = legs[1].id
    cheques.save(cheque)
    with pytest.raises(InstallmentChequeLinkError) as exc_info:
        service.record_receipt(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            plan_id=plan.id,
            payload=_cheque_receipt_payload(cheque.id),
            today=TODAY,
        )
    assert exc_info.value.code == "INSTALLMENT_CHEQUE_LINK_MISMATCH"


def test_cheque_receipt_accepts_cheque_already_linked_to_the_same_leg() -> None:
    cheque = _cheque()
    repo, cheques, service, _ = _cheque_world(cheque)
    plan = _create_plan(service)
    legs = repo.list_legs(plan.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID)
    cheque.installment_leg_id = legs[0].id  # intake-linked to the leg it will pay
    cheques.save(cheque)
    receipt = service.record_receipt(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        plan_id=plan.id,
        payload=_cheque_receipt_payload(cheque.id),
        today=TODAY,
    )
    assert receipt.allocations[0].leg_id == legs[0].id


def test_cheque_receipt_rejects_amount_above_the_cheque() -> None:
    cheque = _cheque(amount_irr=10_000_000)
    _, _, service, _ = _cheque_world(cheque)
    plan = _create_plan(service)
    with pytest.raises(InstallmentChequeAmountError) as exc_info:
        service.record_receipt(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            plan_id=plan.id,
            payload=_cheque_receipt_payload(cheque.id, amount_toman=1_500_000),
            today=TODAY,
        )
    assert exc_info.value.code == "INSTALLMENT_CHEQUE_AMOUNT_EXCEEDED"


def test_cheque_receipt_rejects_second_receipt_on_the_same_cheque() -> None:
    cheque = _cheque()
    _, _, service, _ = _cheque_world(cheque)
    plan = _create_plan(service)
    service.record_receipt(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        plan_id=plan.id,
        payload=_cheque_receipt_payload(cheque.id),
        today=TODAY,
    )
    with pytest.raises(InstallmentChequeAlreadyUsedError) as exc_info:
        service.record_receipt(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            plan_id=plan.id,
            payload=_cheque_receipt_payload(cheque.id),
            today=TODAY,
        )
    assert exc_info.value.code == "INSTALLMENT_CHEQUE_ALREADY_USED"


def test_cheque_receipt_without_a_cheque_repository_is_a_configuration_error() -> None:
    repo = FakeInstallmentRepository()
    service = InstallmentCommandService(repo)  # no cheque repository wired
    plan = _create_plan(service)
    with pytest.raises(RuntimeError, match="cheque repository"):
        service.record_receipt(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            plan_id=plan.id,
            payload=_cheque_receipt_payload(str(uuid.uuid4())),
            today=TODAY,
        )


def test_plan_detail_without_cheque_repo_treats_cheque_receipt_as_pending() -> None:
    # Fail-closed fallback: the query service without a cheque
    # repository never counts a cheque receipt as received.
    cheque = _cheque()
    repo, _, service, _ = _cheque_world(cheque)
    plan = _create_plan(service)
    service.record_receipt(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        plan_id=plan.id,
        payload=_cheque_receipt_payload(cheque.id),
        today=TODAY,
    )
    unwired = InstallmentQueryService(repo)
    detail = unwired.get_plan_detail(
        plan.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID, today=TODAY
    )
    assert detail.legs[0].status is LegStatus.PENDING_CLEARANCE
    assert detail.legs[0].linked_cheque is None


# -- the collection hook (US-542) ------------------------------------------------------


def _pending_world() -> tuple[
    FakeInstallmentRepository,
    FakeChequeRepository,
    InstallmentCommandService,
    InstallmentQueryService,
    InstallmentPlan,
    Cheque,
    InstallmentReceipt,
]:
    """A plan with leg 1 pending clearance behind one held cheque."""
    cheque = _cheque()
    repo, cheques, service, queries = _cheque_world(cheque)
    plan = _create_plan(service)
    receipt = service.record_receipt(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        plan_id=plan.id,
        payload=_cheque_receipt_payload(cheque.id),
        today=TODAY,
    )
    return repo, cheques, service, queries, plan, cheque, receipt


def _flip_cheque_state(
    cheques: FakeChequeRepository, cheque: Cheque, state: ChequeState
) -> Cheque:
    saved = cheques.get(cheque.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID)
    assert saved is not None
    saved.state = state
    saved.installment_leg_status = (
        InstallmentLegStatus.PAID
        if state is ChequeState.CLEARED
        else InstallmentLegStatus.UNPAID
        if state is ChequeState.BOUNCED
        else saved.installment_leg_status
    )
    return cheques.save(saved)


def test_clear_marks_the_leg_paid_and_records_the_collection() -> None:
    repo, cheques, _, queries, plan, cheque, receipt = _pending_world()
    cleared = _flip_cheque_state(cheques, cheque, ChequeState.CLEARED)
    hook = InstallmentCollectionHook(repo)
    hook.on_leg_outcome(
        cheque=cleared,
        outcome=InstallmentLegStatus.PAID,
        event_date=datetime.date(2026, 11, 25),
    )
    detail = queries.get_plan_detail(
        plan.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID, today=TODAY
    )
    assert detail.legs[0].status is LegStatus.PAID
    assert detail.legs[0].received.amount == 10_000_000
    assert detail.legs[0].pending_clearance.amount == 0
    saved_receipt = repo.get_receipt_by_cheque(
        cheque.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID
    )
    assert saved_receipt is not None
    assert saved_receipt.collected_at is not None
    assert saved_receipt.collected_at.gregorian_date == datetime.date(2026, 11, 25)
    assert saved_receipt.id == receipt.id


def test_collection_hook_is_idempotent_on_replay() -> None:
    repo, cheques, _, _, _, cheque, _ = _pending_world()
    cleared = _flip_cheque_state(cheques, cheque, ChequeState.CLEARED)
    hook = InstallmentCollectionHook(repo)
    hook.on_leg_outcome(
        cheque=cleared,
        outcome=InstallmentLegStatus.PAID,
        event_date=datetime.date(2026, 11, 25),
    )
    hook.on_leg_outcome(
        cheque=cleared,
        outcome=InstallmentLegStatus.PAID,
        event_date=datetime.date(2026, 11, 26),
    )
    saved_receipt = repo.get_receipt_by_cheque(
        cheque.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID
    )
    assert saved_receipt is not None
    assert saved_receipt.collected_at is not None
    # The re-fire never moves the collection date.
    assert saved_receipt.collected_at.gregorian_date == datetime.date(2026, 11, 25)


def test_bounce_returns_the_leg_to_unpaid_and_overdue() -> None:
    repo, cheques, _, queries, plan, cheque, _ = _pending_world()
    bounced = _flip_cheque_state(cheques, cheque, ChequeState.BOUNCED)
    hook = InstallmentCollectionHook(repo)
    hook.on_leg_outcome(
        cheque=bounced,
        outcome=InstallmentLegStatus.UNPAID,
        event_date=datetime.date(2026, 11, 25),
    )
    detail = queries.get_plan_detail(
        plan.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID, today=TODAY
    )
    # Leg 1 was due 2026-10-21 — bounced, it is overdue again (the
    # input US-541's late-fee engine operates on, from the original
    # due date). No fee is computed here — US-541 owns that.
    assert detail.legs[0].status is LegStatus.OVERDUE
    assert detail.legs[0].received.amount == 0
    assert detail.legs[0].pending_clearance.amount == 0
    assert detail.status is PlanStatus.OVERDUE
    saved_receipt = repo.get_receipt_by_cheque(
        cheque.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID
    )
    assert saved_receipt is not None
    assert saved_receipt.collected_at is None  # never collected
    assert saved_receipt.allocations  # the attempt stays on the receipt as history


def test_bounced_leg_can_be_repaid_by_a_new_receipt() -> None:
    repo, cheques, service, queries, plan, cheque, _ = _pending_world()
    bounced = _flip_cheque_state(cheques, cheque, ChequeState.BOUNCED)
    InstallmentCollectionHook(repo).on_leg_outcome(
        cheque=bounced,
        outcome=InstallmentLegStatus.UNPAID,
        event_date=datetime.date(2026, 11, 25),
    )
    service.record_receipt(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        plan_id=plan.id,
        payload=_receipt_payload(),
        today=TODAY,
    )
    detail = queries.get_plan_detail(
        plan.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID, today=TODAY
    )
    assert detail.legs[0].status is LegStatus.PAID


def test_collection_hook_tolerates_a_missing_receipt() -> None:
    # A cheque linked by intake, with no receipt yet: the clear has
    # no collection to record — the hook logs and returns, never raises.
    repo = FakeInstallmentRepository()
    plan = _create_plan(InstallmentCommandService(repo))
    legs = repo.list_legs(plan.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID)
    linked = _cheque(sayad_id="5420000000000002")
    linked.installment_leg_id = legs[0].id
    linked.state = ChequeState.CLEARED
    InstallmentCollectionHook(repo).on_leg_outcome(
        cheque=linked,
        outcome=InstallmentLegStatus.PAID,
        event_date=datetime.date(2026, 11, 25),
    )


def test_collection_hook_tolerates_a_dangling_leg_link() -> None:
    repo = FakeInstallmentRepository()
    cheque = _cheque(installment_leg_id="00000000-0000-0000-0000-000000000999")
    cheque.state = ChequeState.CLEARED
    InstallmentCollectionHook(repo).on_leg_outcome(
        cheque=cheque,
        outcome=InstallmentLegStatus.PAID,
        event_date=datetime.date(2026, 11, 25),
    )


def test_collection_hook_ignores_a_bounce_without_writing() -> None:
    repo, cheques, _, _, _, cheque, receipt = _pending_world()
    bounced = _flip_cheque_state(cheques, cheque, ChequeState.BOUNCED)
    InstallmentCollectionHook(repo).on_leg_outcome(
        cheque=bounced,
        outcome=InstallmentLegStatus.UNPAID,
        event_date=datetime.date(2026, 11, 25),
    )
    saved_receipt = repo.get_receipt_by_cheque(
        cheque.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID
    )
    assert saved_receipt is not None
    assert saved_receipt.id == receipt.id
    assert saved_receipt.collected_at is None
