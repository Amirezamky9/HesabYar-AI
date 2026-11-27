"""Unit tests for the installment finance services (US-541, Phase 5-5 ۵ج).

``InstallmentFinanceService`` (early settlement + dunning sweep +
late-fee journals) and the finance reads on
``InstallmentQueryService`` (payoff quote, overdue report,
effective-contract lookup), over the in-memory fake repository from
``tests.unit.test_installment_services``, the REAL review-queue
command service over a fake queue port, and a fake journal port
(records the drafts it would create — no engine).

"Today" is pinned explicitly (2026-11-20) in every test; leg due
dates are fixed Gregorian dates via Jalali payload strings.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from dataclasses import dataclass, field

import pytest

from hesabyar.application.commands.installments import InstallmentCommandService
from hesabyar.application.commands.installments_finance import (
    InstallmentFinanceService,
)
from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.application.queries.installments import InstallmentQueryService
from hesabyar.domain.accounting.commands import JournalDraftLine
from hesabyar.domain.accounting.refs import (
    CompanyRef,
    JournalDraftRef,
    JournalRef,
    PostedJournalRef,
)
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.installments.errors import (
    InstallmentAlreadySettledError,
    InstallmentContractNotFoundError,
    InstallmentLateFeeAccountMissingError,
    InstallmentLinkageError,
    InstallmentPlanNotFoundError,
    InstallmentSettlementAmountError,
    InstallmentValidationError,
)
from hesabyar.domain.installments.models import (
    InstallmentPlan,
    InstallmentSaleContract,
    LateFeeJournalState,
    LegStatus,
    PlanStatus,
)
from hesabyar.domain.review_queue.models import (
    ReviewAuditEntry,
    ReviewItem,
    ReviewItemStatus,
    ReviewItemType,
    ReviewSeverity,
    SlaPolicy,
)
from hesabyar.domain.review_queue.ports import (
    ReviewAuditPort,
    ReviewItemFilters,
    ReviewQueuePort,
)
from tests.unit.test_installment_services import (
    COMPANY_ENGINE_ID,
    INSTALLATION_ID,
    TENANT_ID,
    FakeInstallmentRepository,
)

TODAY = datetime.date(2026, 11, 20)

#: Jalali for 2026-10-21 / 2026-11-17 / 2026-11-20 / 2026-12-20.
PAST_JALALI = "1405-07-29"
RECENT_JALALI = "1405-08-26"
TODAY_JALALI = "1405-08-29"
FUTURE_JALALI = "1405-09-29"


class FakeReviewQueue(ReviewQueuePort, ReviewAuditPort):
    """In-memory review queue (items + audit) for the finance service."""

    def __init__(self) -> None:
        self.items: dict[str, ReviewItem] = {}
        self.entries: list[ReviewAuditEntry] = []
        self.policies: dict[ReviewItemType, SlaPolicy] = {}

    def list_items(
        self, tenant_id: str, filters: ReviewItemFilters
    ) -> Sequence[ReviewItem]:
        found = [item for item in self.items.values() if item.tenant_id == tenant_id]
        if filters.item_type is not None:
            found = [item for item in found if item.item_type == filters.item_type]
        if filters.severity is not None:
            found = [item for item in found if item.severity == filters.severity]
        if filters.status is not None:
            found = [item for item in found if item.status == filters.status]
        return found

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
        return list(self.policies.values())

    def upsert_policy(self, tenant_id: str, policy: SlaPolicy) -> SlaPolicy:
        self.policies[policy.item_type] = policy
        return policy

    def record(self, entry: ReviewAuditEntry) -> ReviewAuditEntry:
        self.entries.append(entry)
        return entry

    def list_for_item(self, item_id: str, tenant_id: str) -> Sequence[ReviewAuditEntry]:
        return [entry for entry in self.entries if entry.item_id == item_id]


@dataclass
class FakeJournals:
    """Records the draft journals the finance service would create."""

    drafts: list[tuple[CompanyRef, FiscalDate, tuple[JournalDraftLine, ...], str]] = field(
        default_factory=list
    )
    posted: list[int] = field(default_factory=list)
    _next_id: int = 9000

    def create_draft(
        self,
        company: CompanyRef,
        date: FiscalDate,
        lines: Sequence[JournalDraftLine],
        description: str = "",
        journal: JournalRef | None = None,
    ) -> JournalDraftRef:
        self._next_id += 1
        ref = JournalDraftRef(engine_id=self._next_id)
        self.drafts.append((company, date, tuple(lines), description))
        return ref

    def post_draft(self, draft: JournalDraftRef) -> PostedJournalRef:
        self.posted.append(draft.engine_id)
        return PostedJournalRef(engine_id=draft.engine_id)


def _commands(repo: FakeInstallmentRepository) -> InstallmentCommandService:
    return InstallmentCommandService(repo)


def _queries(repo: FakeInstallmentRepository) -> InstallmentQueryService:
    return InstallmentQueryService(repo)


def _finance(
    repo: FakeInstallmentRepository,
    *,
    review_queue: FakeReviewQueue | None = None,
    journals: FakeJournals | None = None,
) -> InstallmentFinanceService:
    review = None
    if review_queue is not None:
        review = ReviewQueueCommandService(review_queue, review_queue)
    return InstallmentFinanceService(
        repo, review=review, queue=review_queue, journals=journals
    )


def _contract_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "code": "STD-541",
        "name": "قرارداد استاندارد",
        "version": 1,
        "effective_from": "1405-01-01",
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


def _create_contract(
    service: InstallmentCommandService, **overrides: object
) -> InstallmentSaleContract:
    return service.create_contract(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        company_engine_id=COMPANY_ENGINE_ID,
        payload=_contract_payload(**overrides),
    )


def _plan_payload(invoice_id: str, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "invoice_id": invoice_id,
        "legs": [
            {"due_date": PAST_JALALI, "principal_toman": 1_000_000},
            {"due_date": TODAY_JALALI, "principal_toman": 1_000_000},
            {"due_date": FUTURE_JALALI, "principal_toman": 1_000_000},
        ],
    }
    payload.update(overrides)
    return payload


def _create_plan(
    service: InstallmentCommandService, invoice_id: str, **overrides: object
) -> InstallmentPlan:
    return service.create_plan(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        company_engine_id=COMPANY_ENGINE_ID,
        payload=_plan_payload(invoice_id, **overrides),
        today=TODAY,
    )


def _receipt_payload(invoice_id: str, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "invoice_id": invoice_id,
        "journal_move_id": 42,
        "amount_toman": 1_000_000,
        "received_date": TODAY_JALALI,
        "payment_method": "cash",
    }
    payload.update(overrides)
    return payload


def _settlement_payload(invoice_id: str, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "invoice_id": invoice_id,
        "journal_move_id": 4242,
        "amount_toman": 3_330_000,
        "settlement_type": "full",
        "settlement_date": TODAY_JALALI,
        "payment_method": "cash",
    }
    payload.update(overrides)
    return payload


# -- payoff quote (query service) ---------------------------------------------------


def test_payoff_quote_matches_the_pure_math() -> None:
    repo = FakeInstallmentRepository()
    commands = _commands(repo)
    contract = _create_contract(commands)
    plan = _create_plan(commands, "INV-541-Q1", contract_id=contract.id)
    quote = _queries(repo).get_payoff_quote(
        plan.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID, today=TODAY
    )
    assert quote.remaining_principal.amount == 30_000_000
    assert quote.income_unrealised.amount == 3_000_000
    assert quote.income_due.amount == 3_000_000
    assert quote.late_fees_accrued.amount == 300_000
    assert quote.payoff_total.amount == 33_300_000
    assert quote.contract_id == contract.id


def test_payoff_quote_for_an_unknown_plan_is_404() -> None:
    repo = FakeInstallmentRepository()
    with pytest.raises(InstallmentPlanNotFoundError):
        _queries(repo).get_payoff_quote(
            "no-such-plan", tenant_id=TENANT_ID, installation_id=INSTALLATION_ID, today=TODAY
        )


# -- overdue report -------------------------------------------------------------------


def test_overdue_report_lists_overdue_legs_most_overdue_first() -> None:
    repo = FakeInstallmentRepository()
    commands = _commands(repo)
    contract = _create_contract(commands)
    _create_plan(commands, "INV-541-O1", contract_id=contract.id)
    _create_plan(
        commands,
        "INV-541-O2",
        legs=[{"due_date": RECENT_JALALI, "principal_toman": 500_000}],
    )
    report = _queries(repo).list_overdue(
        tenant_id=TENANT_ID, installation_id=INSTALLATION_ID, today=TODAY
    )
    assert report.total_rows == 2
    # Leg 1 of plan O1 is 30 days overdue; the O2 leg is 3 days overdue.
    assert [row.days_overdue for row in report.rows] == [30, 3]
    assert report.rows[0].dunning_stage == 3
    assert report.rows[0].late_fee_accrued.amount == 300_000
    assert report.rows[1].dunning_stage == 0  # no contract on plan O2
    assert report.rows[1].late_fee_accrued.amount == 0
    assert report.total_balance.amount == 15_000_000
    assert report.total_late_fees.amount == 300_000


def test_overdue_report_scopes_to_one_plan() -> None:
    repo = FakeInstallmentRepository()
    commands = _commands(repo)
    plan_a = _create_plan(commands, "INV-541-O3")
    _create_plan(commands, "INV-541-O4")
    report = _queries(repo).list_overdue(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        plan_id=plan_a.id,
        today=TODAY,
    )
    assert report.total_rows == 1
    assert report.rows[0].plan_id == plan_a.id


# -- effective-version selection ----------------------------------------------------------


def test_create_plan_by_code_selects_the_version_in_force() -> None:
    repo = FakeInstallmentRepository()
    commands = _commands(repo)
    _create_contract(commands, version=1, effective_from="1405-01-01")
    v2 = _create_contract(
        commands, version=2, effective_from="1405-06-01", name="نسخه دوم"
    )
    _create_contract(commands, version=3, effective_from="1406-01-01")
    plan = _create_plan(commands, "INV-541-C1", contract_code="STD-541")
    assert plan.contract_id == v2.id


def test_create_plan_by_unknown_code_is_404() -> None:
    repo = FakeInstallmentRepository()
    commands = _commands(repo)
    with pytest.raises(InstallmentContractNotFoundError):
        _create_plan(commands, "INV-541-C2", contract_code="NO-SUCH-CODE")


def test_create_plan_with_both_contract_id_and_code_is_422() -> None:
    repo = FakeInstallmentRepository()
    commands = _commands(repo)
    contract = _create_contract(commands)
    with pytest.raises(InstallmentValidationError):
        _create_plan(
            commands, "INV-541-C3", contract_id=contract.id, contract_code="STD-541"
        )


def test_get_effective_contract_returns_the_version_in_force() -> None:
    repo = FakeInstallmentRepository()
    commands = _commands(repo)
    _create_contract(commands, version=1, effective_from="1405-01-01")
    v2 = _create_contract(commands, version=2, effective_from="1405-06-01")
    found = _queries(repo).get_effective_contract(
        "STD-541",
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        on_date=TODAY,
    )
    assert found.id == v2.id
    with pytest.raises(InstallmentContractNotFoundError):
        _queries(repo).get_effective_contract(
            "STD-541",
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            on_date=datetime.date(2025, 1, 1),
        )


# -- full early settlement ------------------------------------------------------------------


def test_full_settlement_closes_every_open_leg_atomically() -> None:
    repo = FakeInstallmentRepository()
    commands = _commands(repo)
    journals = FakeJournals()
    contract = _create_contract(commands)
    plan = _create_plan(commands, "INV-541-S1", contract_id=contract.id)
    result = _finance(repo, journals=journals).settle_early(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        plan_id=plan.id,
        payload=_settlement_payload("INV-541-S1"),
    )
    assert result.settlement_type.value == "full"
    assert result.receipt.amount.amount == 33_300_000
    assert result.receipt.allocated_irr == 30_000_000
    assert result.receipt.finance_income.amount == 3_000_000
    assert result.receipt.late_fee.amount == 300_000
    assert result.receipt.advance_irr == 0
    assert result.receipt.is_early_settlement is True
    assert len(result.receipt.allocations) == 3
    assert result.amendment is None
    # The late fee was journaled (one draft: Dr receivable / Cr penalty).
    assert result.journal_move_ids == (9001,)
    assert len(journals.drafts) == 1
    _, _, lines, _ = journals.drafts[0]
    assert lines[0].debit.amount == 300_000
    assert lines[0].account.engine_id == 55
    assert lines[1].credit.amount == 300_000
    assert lines[1].account.engine_id == 77
    assert journals.posted == []  # draft by default
    # The plan is paid now.
    detail = _queries(repo).get_plan_detail(
        plan.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID, today=TODAY
    )
    assert detail.status is PlanStatus.PAID
    assert [leg.status for leg in detail.legs] == [
        LegStatus.PAID,
        LegStatus.PAID,
        LegStatus.PAID,
    ]
    assert detail.outstanding.amount == 0


def test_full_settlement_records_the_fee_assessment() -> None:
    repo = FakeInstallmentRepository()
    commands = _commands(repo)
    contract = _create_contract(commands)
    plan = _create_plan(commands, "INV-541-S2", contract_id=contract.id)
    _finance(repo, journals=FakeJournals()).settle_early(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        plan_id=plan.id,
        payload=_settlement_payload("INV-541-S2"),
    )
    legs = repo.list_legs(plan.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID)
    assessments = repo.list_late_fee_assessments(
        legs[0].id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID
    )
    assert len(assessments) == 1
    assert assessments[0].fee.amount == 300_000
    assert assessments[0].days_overdue == 30
    assert assessments[0].journal_move_id == 9001
    assert assessments[0].journal_state is LateFeeJournalState.DRAFT


def test_full_settlement_with_auto_post_posts_the_fee_journal() -> None:
    repo = FakeInstallmentRepository()
    commands = _commands(repo)
    journals = FakeJournals()
    contract = _create_contract(commands)
    plan = _create_plan(commands, "INV-541-S3", contract_id=contract.id, auto_post=True)
    _finance(repo, journals=journals).settle_early(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        plan_id=plan.id,
        payload=_settlement_payload("INV-541-S3"),
    )
    assert journals.posted == [9001]
    legs = repo.list_legs(plan.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID)
    assessments = repo.list_late_fee_assessments(
        legs[0].id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID
    )
    assert assessments[0].journal_state is LateFeeJournalState.POSTED


def test_full_settlement_nets_the_held_advance() -> None:
    repo = FakeInstallmentRepository()
    commands = _commands(repo)
    contract = _create_contract(commands)
    plan = _create_plan(commands, "INV-541-S4", contract_id=contract.id)
    # Overpay first: 25,000,000 against the two due legs → 5,000,000 held advance.
    commands.record_receipt(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        plan_id=plan.id,
        payload=_receipt_payload("INV-541-S4", amount_toman=2_500_000),
        today=TODAY,
    )
    # Payoff now: remaining 10,000,000 + income due (2,000,000 realised-unpaid
    # on the two collected legs + 1,000,000 unrealised on the last leg)
    # + 0 fees − 5,000,000 held advance = 8,000,000.
    quote = _queries(repo).get_payoff_quote(
        plan.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID, today=TODAY
    )
    assert quote.payoff_total.amount == 8_000_000
    result = _finance(repo).settle_early(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        plan_id=plan.id,
        payload=_settlement_payload("INV-541-S4", amount_toman=800_000),
    )
    assert result.receipt.amount.amount == 8_000_000
    assert result.receipt.finance_income.amount == 3_000_000
    assert result.receipt.allocated_irr == 10_000_000
    assert result.receipt.applied_advance.amount == 5_000_000
    assert result.receipt.advance_irr == 0
    detail = _queries(repo).get_plan_detail(
        plan.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID, today=TODAY
    )
    assert detail.status is PlanStatus.PAID
    assert detail.plan.applied_advance.amount == 5_000_000


def test_full_settlement_below_the_payoff_is_refused() -> None:
    repo = FakeInstallmentRepository()
    commands = _commands(repo)
    contract = _create_contract(commands)
    plan = _create_plan(commands, "INV-541-S5", contract_id=contract.id)
    with pytest.raises(InstallmentSettlementAmountError):
        _finance(repo).settle_early(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            plan_id=plan.id,
            payload=_settlement_payload("INV-541-S5", amount_toman=3_000_000),
        )
    receipts = repo.list_receipts(plan.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID)
    assert receipts == []


def test_settlement_on_an_already_settled_plan_is_a_conflict() -> None:
    repo = FakeInstallmentRepository()
    commands = _commands(repo)
    contract = _create_contract(commands)
    plan = _create_plan(commands, "INV-541-S6", contract_id=contract.id)
    finance = _finance(repo)
    finance.settle_early(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        plan_id=plan.id,
        payload=_settlement_payload("INV-541-S6"),
    )
    with pytest.raises(InstallmentAlreadySettledError):
        finance.settle_early(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            plan_id=plan.id,
            payload=_settlement_payload("INV-541-S6"),
        )


def test_settlement_enforces_the_linkage_guard() -> None:
    repo = FakeInstallmentRepository()
    commands = _commands(repo)
    plan = _create_plan(commands, "INV-541-S7")
    with pytest.raises(InstallmentLinkageError):
        _finance(repo).settle_early(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            plan_id=plan.id,
            payload=_settlement_payload("INV-541-S7", journal_move_id=0),
        )


def test_settlement_with_kept_fees_but_no_accounts_fails_closed() -> None:
    repo = FakeInstallmentRepository()
    commands = _commands(repo)
    # A fee rule, but neither posting account configured.
    contract = _create_contract(
        commands,
        penalty_account_engine_id=None,
        receivable_account_engine_id=None,
    )
    plan = _create_plan(commands, "INV-541-S8", contract_id=contract.id)
    with pytest.raises(InstallmentLateFeeAccountMissingError) as exc_info:
        _finance(repo, journals=FakeJournals()).settle_early(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            plan_id=plan.id,
            payload=_settlement_payload("INV-541-S8"),
        )
    assert set(exc_info.value.missing) == {
        "penalty_account_engine_id",
        "receivable_account_engine_id",
    }
    assert exc_info.value.contract_id == contract.id


def test_settlement_without_an_engine_records_the_fee_unjournaled() -> None:
    repo = FakeInstallmentRepository()
    commands = _commands(repo)
    contract = _create_contract(commands)
    plan = _create_plan(commands, "INV-541-S9", contract_id=contract.id)
    # No journals service wired: the settlement proceeds, the fee is
    # assessed, and a later run with the engine journals it.
    result = _finance(repo).settle_early(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        plan_id=plan.id,
        payload=_settlement_payload("INV-541-S9"),
    )
    assert result.journal_move_ids == ()
    legs = repo.list_legs(plan.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID)
    assessments = repo.list_late_fee_assessments(
        legs[0].id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID
    )
    assert assessments[0].journal_move_id is None
    assert assessments[0].journal_state is LateFeeJournalState.NONE


# -- partial early settlement + re-amortisation -------------------------------------


def test_partial_settlement_reamortises_the_remaining_legs() -> None:
    repo = FakeInstallmentRepository()
    commands = _commands(repo)
    contract = _create_contract(
        commands,
        early_settlement_income_policy="waived",
        early_settlement_fee_policy="waived",
    )
    plan = _create_plan(commands, "INV-541-P1", contract_id=contract.id)
    # Pay 25,000,000: 10M to the overdue leg, 10M to the due leg,
    # 5M advance — which reduces the last leg from 10M to 5M.
    result = _finance(repo).settle_early(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        plan_id=plan.id,
        payload=_settlement_payload(
            "INV-541-P1", amount_toman=2_500_000, settlement_type="partial"
        ),
    )
    assert result.settlement_type.value == "partial"
    assert result.receipt.allocated_irr == 20_000_000
    assert result.receipt.advance_irr == 0
    assert result.amendment is not None
    assert result.amendment.applied_advance.amount == 5_000_000
    assert [s.principal.amount for s in result.amendment.legs_before] == [10_000_000]
    assert [s.principal.amount for s in result.amendment.legs_after] == [5_000_000]
    legs = repo.list_legs(plan.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID)
    assert [leg.principal.amount for leg in legs] == [10_000_000, 10_000_000, 5_000_000]
    detail = _queries(repo).get_plan_detail(
        plan.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID, today=TODAY
    )
    assert detail.outstanding.amount == 5_000_000
    assert detail.status is PlanStatus.ACTIVE


def test_partial_settlement_applies_the_already_held_advance() -> None:
    repo = FakeInstallmentRepository()
    commands = _commands(repo)
    contract = _create_contract(
        commands,
        early_settlement_income_policy="waived",
        early_settlement_fee_policy="waived",
    )
    plan = _create_plan(commands, "INV-541-P2", contract_id=contract.id)
    # First an ordinary overpayment: 25M → 5M held advance.
    commands.record_receipt(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        plan_id=plan.id,
        payload=_receipt_payload("INV-541-P2", amount_toman=2_500_000),
        today=TODAY,
    )
    # Then a small partial settlement: 1M new money + 5M held = 6M
    # applied; the last leg drops from 10M to 4M.
    result = _finance(repo).settle_early(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        plan_id=plan.id,
        payload=_settlement_payload(
            "INV-541-P2", amount_toman=100_000, settlement_type="partial"
        ),
    )
    assert result.receipt.amount.amount == 1_000_000
    assert result.receipt.allocated_irr == 0  # no due leg left to pay
    assert result.receipt.advance_irr == 0
    assert result.receipt.applied_advance.amount == 5_000_000
    assert result.amendment is not None
    assert result.amendment.applied_advance.amount == 6_000_000
    legs = repo.list_legs(plan.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID)
    assert legs[2].principal.amount == 4_000_000
    detail = _queries(repo).get_plan_detail(
        plan.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID, today=TODAY
    )
    assert detail.plan.applied_advance.amount == 5_000_000


def test_partial_settlement_cannot_close_the_last_leg_with_income_due() -> None:
    repo = FakeInstallmentRepository()
    commands = _commands(repo)
    contract = _create_contract(commands)
    plan = _create_plan(
        commands,
        "INV-541-P3",
        contract_id=contract.id,
        legs=[{"due_date": PAST_JALALI, "principal_toman": 1_000_000}],
    )
    # The single leg's payoff is 10M principal + 1M income + 300k fee;
    # a 10M partial payment would close the leg and strand the income.
    with pytest.raises(InstallmentSettlementAmountError):
        _finance(repo).settle_early(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            plan_id=plan.id,
            payload=_settlement_payload(
                "INV-541-P3", amount_toman=1_000_000, settlement_type="partial"
            ),
        )


def test_partial_settlement_without_advance_changes_nothing() -> None:
    repo = FakeInstallmentRepository()
    commands = _commands(repo)
    contract = _create_contract(
        commands,
        early_settlement_income_policy="waived",
        early_settlement_fee_policy="waived",
    )
    plan = _create_plan(commands, "INV-541-P4", contract_id=contract.id)
    # Exactly the overdue leg's amount: no advance, no re-amortisation.
    result = _finance(repo).settle_early(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        plan_id=plan.id,
        payload=_settlement_payload(
            "INV-541-P4", amount_toman=1_000_000, settlement_type="partial"
        ),
    )
    assert result.amendment is None
    assert result.receipt.allocated_irr == 10_000_000
    legs = repo.list_legs(plan.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID)
    assert [leg.principal.amount for leg in legs] == [10_000_000, 10_000_000, 10_000_000]


# -- dunning sweep ----------------------------------------------------------------------


def _dunning_plan(commands: InstallmentCommandService, invoice_id: str) -> InstallmentPlan:
    contract = _create_contract(commands)
    return _create_plan(commands, invoice_id, contract_id=contract.id)


def test_dunning_run_files_one_item_per_leg_per_stage() -> None:
    repo = FakeInstallmentRepository()
    commands = _commands(repo)
    queue = FakeReviewQueue()
    journals = FakeJournals()
    _dunning_plan(commands, "INV-541-D1")
    result = _finance(repo, review_queue=queue, journals=journals).run_dunning(
        tenant_id=TENANT_ID, installation_id=INSTALLATION_ID, today=TODAY
    )
    assert result.plans_scanned == 1
    assert result.overdue_legs == 1
    assert len(result.items_filed) == 1
    # 30 days overdue with offsets (3, 7, 14) → final stage → critical.
    item = queue.items[result.items_filed[0]]
    assert item.item_type is ReviewItemType.INSTALLMENT_OVERDUE
    assert item.severity is ReviewSeverity.CRITICAL
    assert item.status is ReviewItemStatus.OPEN
    assert item.subtype == "dunning-stage-3"
    assert item.amount_irr == 10_300_000  # balance 10M + fee 300k
    assert item.source_event_ref is not None
    # The ref is colon-segmented (the codebase convention, and the
    # documented form in docs/installments.md); only the subtype
    # uses the dash form ("dunning-stage-3").
    assert item.source_event_ref.endswith(":dunning-stage:3")
    # The fee was assessed and journaled (draft).
    assert result.fees_assessed_irr == 300_000
    assert result.journals_created == (9001,)
    assert result.legs_skipped_no_account == ()


def test_dunning_run_is_idempotent_within_a_day() -> None:
    repo = FakeInstallmentRepository()
    commands = _commands(repo)
    queue = FakeReviewQueue()
    journals = FakeJournals()
    _dunning_plan(commands, "INV-541-D2")
    finance = _finance(repo, review_queue=queue, journals=journals)
    first = finance.run_dunning(
        tenant_id=TENANT_ID, installation_id=INSTALLATION_ID, today=TODAY
    )
    second = finance.run_dunning(
        tenant_id=TENANT_ID, installation_id=INSTALLATION_ID, today=TODAY
    )
    assert len(first.items_filed) == 1
    assert second.items_filed == ()
    assert second.fees_assessed_irr == 0
    assert second.journals_created == ()
    assert len(journals.drafts) == 1


def test_dunning_run_files_a_new_item_when_the_leg_ages_into_a_stage() -> None:
    repo = FakeInstallmentRepository()
    commands = _commands(repo)
    queue = FakeReviewQueue()
    contract = _create_contract(commands)
    plan = _create_plan(commands, "INV-541-D3", contract_id=contract.id)
    finance = _finance(repo, review_queue=queue)
    # Day 3 overdue: stage 1 (warning).
    day3 = datetime.date(2026, 10, 24)
    first = finance.run_dunning(
        tenant_id=TENANT_ID, installation_id=INSTALLATION_ID, today=day3
    )
    assert len(first.items_filed) == 1
    assert queue.items[first.items_filed[0]].severity is ReviewSeverity.WARNING
    # Day 7 overdue: stage 2 — a second item, the stage-1 item untouched.
    day7 = datetime.date(2026, 10, 28)
    second = finance.run_dunning(
        tenant_id=TENANT_ID, installation_id=INSTALLATION_ID, today=day7
    )
    assert len(second.items_filed) == 1
    assert queue.items[second.items_filed[0]].subtype == "dunning-stage-2"
    assert len(queue.items) == 2
    # The fee grew between the runs (3 days → 7 days) and was re-assessed.
    legs = repo.list_legs(plan.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID)
    assessments = repo.list_late_fee_assessments(
        legs[0].id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID
    )
    assert [a.fee.amount for a in assessments] == [70_000, 30_000]


def test_dunning_run_skips_stage_zero_legs_for_filing_but_assesses_their_fee() -> None:
    repo = FakeInstallmentRepository()
    commands = _commands(repo)
    queue = FakeReviewQueue()
    contract = _create_contract(commands)
    _create_plan(
        commands,
        "INV-541-D4",
        contract_id=contract.id,
        legs=[{"due_date": "1405-08-28", "principal_toman": 1_000_000}],
    )
    # 1 day overdue: before the first offset (3) → stage 0, no item.
    result = _finance(repo, review_queue=queue).run_dunning(
        tenant_id=TENANT_ID, installation_id=INSTALLATION_ID, today=TODAY
    )
    assert result.overdue_legs == 1
    assert result.items_filed == ()
    assert result.fees_assessed_irr == 10_000  # 10M × 0.001 × 1 day


def test_dunning_run_skips_journaling_when_the_accounts_are_missing() -> None:
    repo = FakeInstallmentRepository()
    commands = _commands(repo)
    queue = FakeReviewQueue()
    journals = FakeJournals()
    contract = _create_contract(
        commands,
        penalty_account_engine_id=None,
        receivable_account_engine_id=None,
    )
    plan = _create_plan(commands, "INV-541-D5", contract_id=contract.id)
    result = _finance(repo, review_queue=queue, journals=journals).run_dunning(
        tenant_id=TENANT_ID, installation_id=INSTALLATION_ID, today=TODAY
    )
    assert len(result.items_filed) == 1  # filing still happens
    assert result.fees_assessed_irr == 300_000  # the fee is still assessed
    assert result.journals_created == ()
    legs = repo.list_legs(plan.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID)
    assert result.legs_skipped_no_account == (legs[0].id,)
    assessments = repo.list_late_fee_assessments(
        legs[0].id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID
    )
    assert assessments[0].journal_state is LateFeeJournalState.NONE


def test_dunning_run_journals_the_unjournaled_portion_once_the_engine_is_wired() -> None:
    repo = FakeInstallmentRepository()
    commands = _commands(repo)
    queue = FakeReviewQueue()
    _dunning_plan(commands, "INV-541-D6")
    # First run: no engine — the fee is assessed, unjournaled.
    _finance(repo, review_queue=queue).run_dunning(
        tenant_id=TENANT_ID, installation_id=INSTALLATION_ID, today=TODAY
    )
    # Second run, a week later, with the engine: the journal covers
    # the whole accrued fee (nothing was journaled before).
    journals = FakeJournals()
    later = datetime.date(2026, 11, 27)
    result = _finance(repo, review_queue=queue, journals=journals).run_dunning(
        tenant_id=TENANT_ID, installation_id=INSTALLATION_ID, today=later
    )
    # TWO journals: leg 1 (37 days overdue, 370,000) and leg 2 —
    # due on the first run's day, 7 days overdue by now (70,000).
    # Each overdue leg gets its own fee journal.
    assert result.journals_created == (9001, 9002)
    _, _, lines, _ = journals.drafts[0]
    # 37 days overdue: 10M × 0.001 × 37 = 370,000.
    assert lines[0].debit.amount == 370_000
    assert lines[1].credit.amount == 370_000
    _, _, lines2, _ = journals.drafts[1]
    # 7 days overdue: 10M × 0.001 × 7 = 70,000.
    assert lines2[0].debit.amount == 70_000
    assert lines2[1].credit.amount == 70_000
