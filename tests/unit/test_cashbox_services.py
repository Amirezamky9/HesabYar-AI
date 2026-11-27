"""Unit tests for the cash-box services (US-516, Phase 5 wave 2, K2).

No database: the cash-box port and the engine port are faked in
memory, the review queue runs over its in-memory fakes, and the
clock is fixed. The REST tests
(``tests/interfaces/test_rest_cashbox.py``) cover the real
repository + HTTP layer against PostgreSQL.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence

import pytest

from hesabyar.application.commands.cashbox import CashBoxCommandService
from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.application.errors import (
    CashBoxInvalidError,
    CashBoxJournalConflictError,
    CashBoxNotFoundError,
    CashBoxSettingsInvalidError,
    CompanyNotFoundError,
    CountDenominationsMismatchError,
    CountMinutesRequiredError,
    CountRecountNotRequestedError,
    CountReviewNotApprovedError,
    CountSessionAlreadyOpenError,
    CountSessionInvalidStateError,
    CountSessionNotFoundError,
    CountSupervisorMustDifferError,
    CountWrongActorError,
    ReviewInvalidStateError,
)
from hesabyar.application.queries.cashbox import CashBoxQueryService
from hesabyar.application.queries.review_queue import ReviewQueueQueryService
from hesabyar.domain.accounting.commands import CreateJournalDraft, PostJournal
from hesabyar.domain.accounting.refs import (
    AccountRef,
    CompanyRef,
    InvoiceRef,
    JournalDraftRef,
    JournalRef,
    PartyRef,
    PostedJournalRef,
    StatementJournalRef,
)
from hesabyar.domain.accounting.value_objects import (
    AccountBalanceView,
    AccountView,
    CompanyView,
    InvoiceTotalView,
    InvoiceView,
    StatementJournalView,
    TrialBalanceLineView,
    TrialBalanceView,
)
from hesabyar.domain.cashbox.models import (
    CashBox,
    CashBoxKind,
    CashBoxReportLine,
    CashBoxSettings,
    CountSession,
    CountSessionStatus,
    DenominationCount,
    VariancePolicy,
    variance_account_code,
    variance_journal_lines,
)
from hesabyar.domain.cashbox.ports import CashBoxPort, CountSessionFilters
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.errors import AccountNotFoundError, JournalDraftNotFoundError
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.domain.review_queue.models import (
    ReviewAuditEntry,
    ReviewItem,
    ReviewItemStatus,
    ReviewItemType,
    ReviewResolution,
    ReviewSeverity,
    SlaPolicy,
)
from hesabyar.domain.review_queue.ports import (
    ReviewAuditPort,
    ReviewItemFilters,
    ReviewQueuePort,
)
from tests.unit.test_application_services import FakePort

NOW = datetime.datetime(2026, 11, 18, 12, 0, tzinfo=datetime.UTC)
TENANT = "tenant-516"
COMPANY = CompanyRef(1)
COMPANY_ROW_ID = "row-1"
CASH_ACCOUNT_ID = 35
PETTY_ACCOUNT_ID = 36
VARIANCE_ACCOUNT_ID = 37
TEMP_DEBTORS_ACCOUNT_ID = 38
MISC_CREDITORS_ACCOUNT_ID = 39
GENERAL_JOURNAL_ID = 9


def _account(account_id: int, code: str, name: str) -> AccountView:
    return AccountView(
        ref=AccountRef(account_id),
        company=COMPANY,
        code=code,
        name=name,
        type_name="Asset",
        parent=None,
        closed=False,
        party_required=False,
        active=True,
    )


CHART = [
    _account(CASH_ACCOUNT_ID, "1.1.1", "Cash and Cash Equivalents"),
    _account(PETTY_ACCOUNT_ID, "1.1.5", "Petty Cash"),
    _account(VARIANCE_ACCOUNT_ID, "1.2.6", "Cash Shortage and Overage"),
    _account(TEMP_DEBTORS_ACCOUNT_ID, "1.2.7", "Temporary Debtors — Cash Shortage"),
    _account(MISC_CREDITORS_ACCOUNT_ID, "2.1.6", "Miscellaneous Creditors — Cash Overage"),
]


def _journal(journal_id: int, name: str, account_id: int, code: str, *, active: bool = True) -> StatementJournalView:
    return StatementJournalView(
        ref=StatementJournalRef(journal_id),
        company=COMPANY,
        name=name,
        journal=JournalRef(journal_id + 100),
        account=AccountRef(account_id),
        account_code=code,
        account_name=name,
        currency_code="IRR",
        validation="balance",
        active=active,
    )


JOURNALS = [
    _journal(10, "صندوق فروشگاه", CASH_ACCOUNT_ID, "1.1.1"),
    _journal(11, "تنخواه", PETTY_ACCOUNT_ID, "1.1.5"),
]


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class InMemoryCashBox(CashBoxPort):
    """In-memory cash-box port: newest session first, like the real repo."""

    def __init__(self) -> None:
        self.boxes: dict[str, CashBox] = {}
        self.sessions: dict[str, CountSession] = {}
        self.settings: dict[str, CashBoxSettings] = {}

    def list_cash_boxes(self, tenant_id: str, company_id: str | None = None) -> Sequence[CashBox]:
        out = [box for box in self.boxes.values() if box.tenant_id == tenant_id]
        if company_id is not None:
            out = [box for box in out if box.company_id == company_id]
        return sorted(out, key=lambda box: (box.created_at or NOW, box.id))

    def get_cash_box(self, box_id: str, tenant_id: str) -> CashBox | None:
        box = self.boxes.get(box_id)
        return box if box is not None and box.tenant_id == tenant_id else None

    def get_cash_box_by_journal(self, tenant_id: str, company_id: str, statement_journal_id: int) -> CashBox | None:
        for box in self.boxes.values():
            if (
                box.tenant_id == tenant_id
                and box.company_id == company_id
                and box.statement_journal_id == statement_journal_id
            ):
                return box
        return None

    def save_cash_box(self, box: CashBox) -> CashBox:
        self.boxes[box.id] = box
        return box

    def update_cash_box(self, box: CashBox) -> CashBox:
        self.boxes[box.id] = box
        return box

    def list_sessions(self, tenant_id: str, filters: CountSessionFilters) -> Sequence[CountSession]:
        out = [s for s in self.sessions.values() if s.tenant_id == tenant_id]
        if filters.cash_box_id is not None:
            out = [s for s in out if s.cash_box_id == filters.cash_box_id]
        if filters.company_id is not None:
            out = [s for s in out if s.company_id == filters.company_id]
        if filters.status is not None:
            out = [s for s in out if s.status == filters.status]
        if filters.date_from is not None:
            out = [s for s in out if s.created_at.date() >= filters.date_from]
        if filters.date_to is not None:
            out = [s for s in out if s.created_at.date() <= filters.date_to]
        return sorted(out, key=lambda s: (s.created_at, s.id), reverse=True)

    def get_session(self, session_id: str, tenant_id: str) -> CountSession | None:
        session = self.sessions.get(session_id)
        return session if session is not None and session.tenant_id == tenant_id else None

    def get_open_session_for_box(self, cash_box_id: str, tenant_id: str) -> CountSession | None:
        open_sessions = [
            s
            for s in self.sessions.values()
            if s.cash_box_id == cash_box_id and s.tenant_id == tenant_id and s.status != CountSessionStatus.ARCHIVED
        ]
        return max(open_sessions, key=lambda s: s.created_at) if open_sessions else None

    def save_session(self, session: CountSession) -> CountSession:
        self.sessions[session.id] = session
        return session

    def update_session(self, session: CountSession) -> CountSession:
        self.sessions[session.id] = session
        return session

    def get_settings(self, tenant_id: str) -> CashBoxSettings | None:
        return self.settings.get(tenant_id)

    def upsert_settings(self, settings: CashBoxSettings) -> CashBoxSettings:
        self.settings[settings.tenant_id] = settings
        return settings


class FakeCashBoxEngine(FakePort):
    """In-memory engine fake for the cash-box services.

    Subclasses the complete ``FakePort`` (test_application_services)
    so the fake stays a full ``AccountingEnginePort`` — the methods
    the cash-box services never call stay ``NotImplementedError``
    stubs inherited from it.
    """

    def __init__(
        self,
        *,
        chart: Sequence[AccountView] | None = None,
        journals: Sequence[StatementJournalView] | None = None,
        balances: dict[int, int] | None = None,
        balances_by_date: dict[tuple[int, datetime.date], int] | None = None,
        trial_lines: Sequence[TrialBalanceLineView] | None = None,
        invoices: Sequence[InvoiceView] | None = None,
    ) -> None:
        super().__init__()
        self._chart = list(chart if chart is not None else CHART)
        self._journals = list(journals if journals is not None else JOURNALS)
        self._balances = balances if balances is not None else {CASH_ACCOUNT_ID: 5_000_000}
        self._balances_by_date = balances_by_date or {}
        self._trial_lines = list(trial_lines or [])
        self._invoices = list(invoices or [])
        self.drafts: dict[int, CreateJournalDraft] = {}
        self.posted: list[int] = []
        self._next_draft_id = 100

    def set_chart(self, chart: Sequence[AccountView]) -> None:
        """Swap the chart (a test-only stand-in for a chart update landing mid-test)."""
        self._chart = list(chart)

    def list_companies(self) -> Sequence[CompanyView]:
        return [CompanyView(ref=COMPANY, name="Test Co", currency_code="IRR")]

    def get_chart_of_accounts(self, company: CompanyRef) -> Sequence[AccountView]:
        return list(self._chart) if company == COMPANY else []

    def get_account_by_code(self, company: CompanyRef, code: str) -> AccountView | None:
        if company != COMPANY:
            return None
        return next((account for account in self._chart if account.code == code), None)

    def get_account_balance(self, company: CompanyRef, account: AccountRef, as_of: FiscalDate) -> AccountBalanceView:
        if company != COMPANY:
            raise AccountNotFoundError(f"Account {account.engine_id} does not exist for company {company.engine_id}.")
        amount = self._balances_by_date.get((account.engine_id, as_of.date))
        if amount is None:
            amount = self._balances.get(account.engine_id)
        if amount is None:
            raise AccountNotFoundError(f"Account {account.engine_id} does not exist for company {company.engine_id}.")
        code = next(
            (view.code for view in self._chart if view.ref == account),
            "",
        )
        return AccountBalanceView(
            account=account,
            code=code,
            name="",
            as_of=as_of,
            total_debit=MoneyIRR(amount) if amount >= 0 else MoneyIRR(0),
            total_credit=MoneyIRR(-amount) if amount < 0 else MoneyIRR(0),
            line_count=1,
        )

    def list_statement_journals(self, company: CompanyRef) -> Sequence[StatementJournalView]:
        return list(self._journals) if company == COMPANY else []

    def get_trial_balance(self, company: CompanyRef, date_from: FiscalDate, date_to: FiscalDate) -> TrialBalanceView:
        return TrialBalanceView(company=company, date_from=date_from, date_to=date_to, lines=tuple(self._trial_lines))

    def list_invoices(
        self,
        company: CompanyRef,
        invoice_type: str | None = None,
        state: str | None = None,
    ) -> Sequence[InvoiceView]:
        return list(self._invoices)

    def list_invoice_totals(
        self,
        company: CompanyRef,
        invoice_type: str | None = None,
        state: str | None = None,
    ) -> Sequence[InvoiceTotalView]:
        return [
            InvoiceTotalView(
                ref=invoice.ref,
                invoice_date=invoice.invoice_date,
                total_amount=invoice.total_amount,
            )
            for invoice in self._invoices
        ]

    def get_general_journal(self, company: CompanyRef) -> JournalRef:
        return JournalRef(GENERAL_JOURNAL_ID)

    def create_journal_draft(self, command: CreateJournalDraft) -> JournalDraftRef:
        draft_id = self._next_draft_id
        self._next_draft_id += 1
        self.drafts[draft_id] = command
        return JournalDraftRef(draft_id)

    def post_journal(self, command: PostJournal) -> PostedJournalRef:
        self.posted.append(command.draft.engine_id)
        return PostedJournalRef(command.draft.engine_id)

    def delete_journal_draft(self, draft: JournalDraftRef) -> None:
        if draft.engine_id not in self.drafts:
            raise JournalDraftNotFoundError(f"Journal draft {draft.engine_id} does not exist.")
        del self.drafts[draft.engine_id]


class InMemoryReviewQueue(ReviewQueuePort):
    """In-memory review-queue fake (same shape as test_review_queue_services)."""

    def __init__(self) -> None:
        self.items: dict[str, ReviewItem] = {}
        self.policies: dict[ReviewItemType, SlaPolicy] = {}

    def list_items(self, tenant_id: str, filters: ReviewItemFilters) -> Sequence[ReviewItem]:
        out = [item for item in self.items.values() if item.tenant_id == tenant_id]
        if filters.item_type is not None:
            out = [item for item in out if item.item_type == filters.item_type]
        if filters.severity is not None:
            out = [item for item in out if item.severity == filters.severity]
        if filters.status is not None:
            out = [item for item in out if item.status == filters.status]
        if filters.channel is not None:
            out = [item for item in out if item.channel == filters.channel]
        if filters.company_id is not None:
            out = [item for item in out if item.company_id == filters.company_id]
        if filters.assignee is not None:
            out = [item for item in out if item.assignee == filters.assignee]
        if filters.min_amount_irr is not None:
            out = [item for item in out if item.amount_irr is not None and item.amount_irr >= filters.min_amount_irr]
        if filters.max_amount_irr is not None:
            out = [item for item in out if item.amount_irr is not None and item.amount_irr <= filters.max_amount_irr]
        return out

    def get_item(self, item_id: str, tenant_id: str) -> ReviewItem | None:
        item = self.items.get(item_id)
        return item if item is not None and item.tenant_id == tenant_id else None

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


class InMemoryReviewAudit(ReviewAuditPort):
    """In-memory audit fake: append-only, oldest first."""

    def __init__(self) -> None:
        self.entries: list[ReviewAuditEntry] = []

    def record(self, entry: ReviewAuditEntry) -> ReviewAuditEntry:
        self.entries.append(entry)
        return entry

    def list_for_item(self, item_id: str, tenant_id: str) -> Sequence[ReviewAuditEntry]:
        return [entry for entry in self.entries if entry.item_id == item_id]


@pytest.fixture()
def world() -> tuple[
    InMemoryCashBox,
    FakeCashBoxEngine,
    CashBoxQueryService,
    CashBoxCommandService,
    ReviewQueueCommandService,
    ReviewQueueQueryService,
]:
    """Fresh ports + services with a fixed clock."""
    cashbox = InMemoryCashBox()
    engine = FakeCashBoxEngine()
    queue = InMemoryReviewQueue()
    audit = InMemoryReviewAudit()
    review_query = ReviewQueueQueryService(queue, audit, now=NOW)
    review_cmd = ReviewQueueCommandService(queue, audit, now=NOW)
    query = CashBoxQueryService(cashbox, engine, now=NOW)
    cmd = CashBoxCommandService(cashbox, engine, review_cmd, review_query, now=NOW)
    return cashbox, engine, query, cmd, review_cmd, review_query


def _register_box(
    cmd: CashBoxCommandService,
    *,
    journal_id: int = 10,
    kind: CashBoxKind = CashBoxKind.STORE,
    name_fa: str = "صندوق فروشگاه",
    ceiling_irr: int | None = None,
) -> CashBox:
    return cmd.register_cash_box(
        tenant_id=TENANT,
        company=COMPANY,
        company_row_id=COMPANY_ROW_ID,
        name_fa=name_fa,
        kind=kind,
        statement_journal_id=journal_id,
        ceiling_irr=ceiling_irr,
    )


def _start_session(cmd: CashBoxCommandService, box: CashBox) -> CountSession:
    return cmd.start_session(
        tenant_id=TENANT,
        company=COMPANY,
        company_row_id=COMPANY_ROW_ID,
        cash_box_id=box.id,
        cashier="cashier-1",
        supervisor="supervisor-1",
    )


def _complete_variance_session(cmd: CashBoxCommandService, session: CountSession, counted: int) -> CountSession:
    """Drive a session to awaiting_manager: count + all three signatures."""
    cmd.enter_count(tenant_id=TENANT, session_id=session.id, counted_amount_irr=counted, actor="cashier-1")
    cmd.sign_session(
        tenant_id=TENANT,
        session_id=session.id,
        role="cashier",
        actor="cashier-1",
        minutes_text="صورتجلسهٔ شمارش صندوق.",
    )
    cmd.sign_session(tenant_id=TENANT, session_id=session.id, role="supervisor", actor="supervisor-1")
    return cmd.sign_session(tenant_id=TENANT, session_id=session.id, role="manager", actor="manager-1")


# ---------------------------------------------------------------------------
# Domain: variance math, policies, journal lines
# ---------------------------------------------------------------------------


def _session(*, book: int = 5_000_000, counted: int | None = None) -> CountSession:
    return CountSession(
        id="session-1",
        tenant_id=TENANT,
        company_id=COMPANY_ROW_ID,
        cash_box_id="box-1",
        cashier="cashier-1",
        supervisor="supervisor-1",
        status=CountSessionStatus.AWAITING_SUPERVISOR,
        book_balance_irr=book,
        created_at=NOW,
        counted_amount_irr=counted,
    )


def test_variance_zero_is_balanced_and_needs_no_minutes():
    session = _session(counted=5_000_000)
    assert session.variance_irr == 0
    assert session.is_balanced is True
    assert session.is_shortage is False
    assert session.is_overage is False
    assert session.minutes_required is False


def test_variance_negative_is_a_shortage_and_needs_minutes():
    session = _session(counted=4_900_000)
    assert session.variance_irr == -100_000
    assert session.is_shortage is True
    assert session.is_overage is False
    assert session.is_balanced is False
    assert session.minutes_required is True


def test_variance_positive_is_an_overage_and_needs_minutes():
    session = _session(counted=5_050_000)
    assert session.variance_irr == 50_000
    assert session.is_overage is True
    assert session.minutes_required is True


def test_variance_none_until_the_count_is_entered():
    session = _session(counted=None)
    assert session.variance_irr is None
    assert session.is_balanced is False
    assert session.minutes_required is False


def test_denominations_total():
    session = _session(counted=5_000_000)
    assert session.denominations_total_irr == 0
    with_denominations = CountSession(
        id="session-2",
        tenant_id=TENANT,
        company_id=COMPANY_ROW_ID,
        cash_box_id="box-1",
        cashier="cashier-1",
        supervisor="supervisor-1",
        status=CountSessionStatus.AWAITING_SUPERVISOR,
        book_balance_irr=5_000_000,
        created_at=NOW,
        counted_amount_irr=5_000_000,
        denominations=(
            DenominationCount(denomination_irr=1_000_000, count=4),
            DenominationCount(denomination_irr=500_000, count=2),
        ),
    )
    assert with_denominations.denominations_total_irr == 5_000_000


def test_variance_account_code_policy_a_uses_one_account_both_ways():
    assert variance_account_code(VariancePolicy.EXPENSE, -100_000) == "1.2.6"
    assert variance_account_code(VariancePolicy.EXPENSE, 100_000) == "1.2.6"


def test_variance_account_code_policy_b_splits_shortage_and_overage():
    assert variance_account_code(VariancePolicy.RECEIVABLE_FROM_CASHIER, -100_000) == "1.2.7"
    assert variance_account_code(VariancePolicy.RECEIVABLE_FROM_CASHIER, 100_000) == "2.1.6"


def test_variance_account_code_zero_raises():
    with pytest.raises(ValueError, match="zero variance"):
        variance_account_code(VariancePolicy.EXPENSE, 0)


def test_variance_journal_lines_shortage_debits_variance_credits_cash():
    cash = AccountRef(CASH_ACCOUNT_ID)
    variance = AccountRef(VARIANCE_ACCOUNT_ID)
    line_a, line_b = variance_journal_lines(variance_irr=-100_000, cash_account=cash, variance_account=variance)
    assert line_a.account == variance and line_a.debit.amount == 100_000 and line_a.credit.amount == 0
    assert line_b.account == cash and line_b.debit.amount == 0 and line_b.credit.amount == 100_000


def test_variance_journal_lines_overage_debits_cash_credits_variance():
    cash = AccountRef(CASH_ACCOUNT_ID)
    variance = AccountRef(VARIANCE_ACCOUNT_ID)
    line_a, line_b = variance_journal_lines(variance_irr=50_000, cash_account=cash, variance_account=variance)
    assert line_a.account == cash and line_a.debit.amount == 50_000 and line_a.credit.amount == 0
    assert line_b.account == variance and line_b.debit.amount == 0 and line_b.credit.amount == 50_000


def test_variance_journal_lines_are_balanced():
    for amount in (-100_000, 50_000):
        lines = variance_journal_lines(
            variance_irr=amount,
            cash_account=AccountRef(CASH_ACCOUNT_ID),
            variance_account=AccountRef(VARIANCE_ACCOUNT_ID),
        )
        assert sum(line.debit.amount for line in lines) == sum(line.credit.amount for line in lines)


def test_variance_journal_lines_zero_raises():
    with pytest.raises(ValueError, match="zero variance"):
        variance_journal_lines(
            variance_irr=0,
            cash_account=AccountRef(CASH_ACCOUNT_ID),
            variance_account=AccountRef(VARIANCE_ACCOUNT_ID),
        )


def test_report_line_unrecorded_sales_indicator_is_net_overage_floored_at_zero():
    overage_line = CashBoxReportLine(
        cash_box_id="box-1",
        cash_box_name_fa="صندوق فروشگاه",
        cash_account_code="1.1.1",
        opening_book_balance_irr=4_000_000,
        closing_book_balance_irr=5_000_000,
        receipts_irr=2_000_000,
        payments_irr=1_000_000,
        session_count=2,
        shortage_irr=10_000,
        overage_irr=60_000,
        recorded_sales_irr=1_500_000,
    )
    assert overage_line.net_variance_irr == 50_000
    assert overage_line.possible_unrecorded_sales_irr == 50_000
    shortage_line = CashBoxReportLine(
        cash_box_id="box-1",
        cash_box_name_fa="صندوق فروشگاه",
        cash_account_code="1.1.1",
        opening_book_balance_irr=4_000_000,
        closing_book_balance_irr=5_000_000,
        receipts_irr=2_000_000,
        payments_irr=1_000_000,
        session_count=1,
        shortage_irr=80_000,
        overage_irr=10_000,
        recorded_sales_irr=1_500_000,
    )
    assert shortage_line.net_variance_irr == -70_000
    assert shortage_line.possible_unrecorded_sales_irr == 0


# ---------------------------------------------------------------------------
# Registry: register / update
# ---------------------------------------------------------------------------


def test_register_cash_box_derives_the_cash_account_from_the_journal(world):
    _, _, _, cmd, _, _ = world
    box = _register_box(cmd)
    assert box.cash_account_id == CASH_ACCOUNT_ID
    assert box.cash_account_code == "1.1.1"
    assert box.statement_journal_id == 10
    assert box.active is True
    assert box.ceiling_irr is None


def test_register_petty_cash_box_with_ceiling(world):
    _, _, _, cmd, _, _ = world
    box = _register_box(cmd, journal_id=11, kind=CashBoxKind.PETTY_CASH, name_fa="تنخواه", ceiling_irr=2_000_000)
    assert box.kind is CashBoxKind.PETTY_CASH
    assert box.cash_account_id == PETTY_ACCOUNT_ID
    assert box.cash_account_code == "1.1.5"
    assert box.ceiling_irr == 2_000_000


def test_register_unknown_journal_is_invalid(world):
    _, _, _, cmd, _, _ = world
    with pytest.raises(CashBoxInvalidError) as excinfo:
        _register_box(cmd, journal_id=999)
    assert excinfo.value.code == "CASHBOX_INVALID"


def test_register_inactive_journal_is_invalid(world):
    cashbox, _, _, _, _, _ = world
    engine = FakeCashBoxEngine(journals=[_journal(10, "صندوق فروشگاه", CASH_ACCOUNT_ID, "1.1.1", active=False)])
    queue = InMemoryReviewQueue()
    audit = InMemoryReviewAudit()
    review_query = ReviewQueueQueryService(queue, audit, now=NOW)
    review_cmd = ReviewQueueCommandService(queue, audit, now=NOW)
    cmd = CashBoxCommandService(cashbox, engine, review_cmd, review_query, now=NOW)
    with pytest.raises(CashBoxInvalidError):
        _register_box(cmd)


def test_register_duplicate_journal_conflicts(world):
    _, _, _, cmd, _, _ = world
    _register_box(cmd)
    with pytest.raises(CashBoxJournalConflictError) as excinfo:
        _register_box(cmd, name_fa="صندوق دوم")
    assert excinfo.value.code == "CASHBOX_JOURNAL_CONFLICT"


def test_register_empty_name_is_invalid(world):
    _, _, _, cmd, _, _ = world
    with pytest.raises(CashBoxInvalidError):
        _register_box(cmd, name_fa="   ")


def test_register_petty_cash_without_ceiling_is_invalid(world):
    _, _, _, cmd, _, _ = world
    with pytest.raises(CashBoxInvalidError):
        _register_box(cmd, journal_id=11, kind=CashBoxKind.PETTY_CASH, name_fa="تنخواه")


def test_register_store_box_with_ceiling_is_invalid(world):
    _, _, _, cmd, _, _ = world
    with pytest.raises(CashBoxInvalidError):
        _register_box(cmd, ceiling_irr=1_000_000)


def test_register_unknown_company_raises_not_found(world):
    _, _, _, cmd, _, _ = world
    with pytest.raises(CompanyNotFoundError):
        cmd.register_cash_box(
            tenant_id=TENANT,
            company=CompanyRef(999),
            company_row_id="row-999",
            name_fa="صندوق",
            kind=CashBoxKind.STORE,
            statement_journal_id=10,
        )


def test_update_cash_box_changes_name_kind_ceiling_and_active(world):
    _, _, _, cmd, _, _ = world
    box = _register_box(cmd)
    updated = cmd.update_cash_box(
        tenant_id=TENANT,
        box_id=box.id,
        name_fa="صندوق اصلی",
        kind=CashBoxKind.OTHER,
        ceiling_irr=None,
        active=False,
    )
    assert updated.name_fa == "صندوق اصلی"
    assert updated.kind is CashBoxKind.OTHER
    assert updated.active is False
    # The journal and cash account never change.
    assert updated.statement_journal_id == 10
    assert updated.cash_account_id == CASH_ACCOUNT_ID


def test_update_cash_box_to_petty_cash_without_ceiling_is_invalid(world):
    _, _, _, cmd, _, _ = world
    box = _register_box(cmd)
    with pytest.raises(CashBoxInvalidError):
        cmd.update_cash_box(
            tenant_id=TENANT,
            box_id=box.id,
            name_fa="تنخواه",
            kind=CashBoxKind.PETTY_CASH,
        )


def test_update_unknown_cash_box_raises_not_found(world):
    _, _, _, cmd, _, _ = world
    with pytest.raises(CashBoxNotFoundError):
        cmd.update_cash_box(
            tenant_id=TENANT,
            box_id="no-such-box",
            name_fa="صندوق",
            kind=CashBoxKind.STORE,
        )


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_settings_default_to_policy_a_before_any_update(world):
    _, _, query, _, _, _ = world
    settings = query.get_settings(TENANT)
    assert settings.variance_policy is VariancePolicy.EXPENSE
    assert settings.material_variance_threshold_irr is None
    assert settings.count_reminder_days == 7
    assert settings.count_schedule == "daily"
    assert settings.supervisor_required is True
    assert settings.shortage_settlement_hours == 24


def test_update_settings_persists_policy_b_and_thresholds(world):
    _, _, _, cmd, _, _ = world
    settings = cmd.update_settings(
        tenant_id=TENANT,
        variance_policy=VariancePolicy.RECEIVABLE_FROM_CASHIER,
        material_variance_threshold_irr=500_000,
        count_reminder_days=3,
        count_schedule="weekly",
        supervisor_required=False,
        shortage_settlement_hours=48,
    )
    assert settings.variance_policy is VariancePolicy.RECEIVABLE_FROM_CASHIER
    assert settings.material_variance_threshold_irr == 500_000
    assert settings.count_reminder_days == 3
    assert settings.count_schedule == "weekly"
    assert settings.supervisor_required is False
    assert settings.shortage_settlement_hours == 48


def test_update_settings_rejects_invalid_values(world):
    _, _, _, cmd, _, _ = world
    with pytest.raises(CashBoxSettingsInvalidError):
        cmd.update_settings(
            tenant_id=TENANT,
            variance_policy=VariancePolicy.EXPENSE,
            material_variance_threshold_irr=-1,
        )
    with pytest.raises(CashBoxSettingsInvalidError):
        cmd.update_settings(tenant_id=TENANT, variance_policy=VariancePolicy.EXPENSE, count_reminder_days=0)
    with pytest.raises(CashBoxSettingsInvalidError):
        cmd.update_settings(tenant_id=TENANT, variance_policy=VariancePolicy.EXPENSE, count_schedule="hourly")
    with pytest.raises(CashBoxSettingsInvalidError):
        cmd.update_settings(tenant_id=TENANT, variance_policy=VariancePolicy.EXPENSE, shortage_settlement_hours=0)


# ---------------------------------------------------------------------------
# Sessions: start
# ---------------------------------------------------------------------------


def test_start_session_snapshots_the_book_balance_read_only(world):
    _, _, _, cmd, _, _ = world
    box = _register_box(cmd)
    session = _start_session(cmd, box)
    assert session.status is CountSessionStatus.IN_PROGRESS
    assert session.book_balance_irr == 5_000_000
    assert session.counted_amount_irr is None
    assert session.engine_company_id == 1


def test_start_session_supervisor_must_differ_from_cashier(world):
    _, _, _, cmd, _, _ = world
    box = _register_box(cmd)
    with pytest.raises(CountSupervisorMustDifferError) as excinfo:
        cmd.start_session(
            tenant_id=TENANT,
            company=COMPANY,
            company_row_id=COMPANY_ROW_ID,
            cash_box_id=box.id,
            cashier="same-user",
            supervisor="same-user",
        )
    assert excinfo.value.code == "COUNT_SUPERVISOR_MUST_DIFFER"


def test_start_session_same_actor_allowed_when_supervisor_control_is_disabled(world):
    _, _, _, cmd, _, _ = world
    cmd.update_settings(tenant_id=TENANT, variance_policy=VariancePolicy.EXPENSE, supervisor_required=False)
    box = _register_box(cmd)
    session = cmd.start_session(
        tenant_id=TENANT,
        company=COMPANY,
        company_row_id=COMPANY_ROW_ID,
        cash_box_id=box.id,
        cashier="same-user",
        supervisor="same-user",
    )
    assert session.status is CountSessionStatus.IN_PROGRESS


def test_start_session_second_open_session_conflicts(world):
    _, _, _, cmd, _, _ = world
    box = _register_box(cmd)
    _start_session(cmd, box)
    with pytest.raises(CountSessionAlreadyOpenError) as excinfo:
        _start_session(cmd, box)
    assert excinfo.value.code == "COUNT_SESSION_ALREADY_OPEN"


def test_start_session_inactive_box_is_an_invalid_state(world):
    _, _, _, cmd, _, _ = world
    box = _register_box(cmd)
    cmd.update_cash_box(tenant_id=TENANT, box_id=box.id, name_fa=box.name_fa, kind=box.kind, active=False)
    with pytest.raises(CountSessionInvalidStateError):
        _start_session(cmd, box)


def test_start_session_box_of_another_company_is_not_found(world):
    _, _, _, cmd, _, _ = world
    box = _register_box(cmd)
    with pytest.raises(CashBoxNotFoundError):
        cmd.start_session(
            tenant_id=TENANT,
            company=COMPANY,
            company_row_id="row-other",
            cash_box_id=box.id,
            cashier="cashier-1",
            supervisor="supervisor-1",
        )


def test_start_session_unknown_box_is_not_found(world):
    _, _, _, cmd, _, _ = world
    with pytest.raises(CashBoxNotFoundError):
        cmd.start_session(
            tenant_id=TENANT,
            company=COMPANY,
            company_row_id=COMPANY_ROW_ID,
            cash_box_id="no-such-box",
            cashier="cashier-1",
            supervisor="supervisor-1",
        )


# ---------------------------------------------------------------------------
# Sessions: enter count
# ---------------------------------------------------------------------------


def test_enter_count_computes_the_variance_and_awaits_the_supervisor(world):
    _, _, _, cmd, _, _ = world
    box = _register_box(cmd)
    session = _start_session(cmd, box)
    updated = cmd.enter_count(tenant_id=TENANT, session_id=session.id, counted_amount_irr=4_900_000, actor="cashier-1")
    assert updated.counted_amount_irr == 4_900_000
    assert updated.variance_irr == -100_000
    assert updated.status is CountSessionStatus.AWAITING_SUPERVISOR


def test_enter_count_with_a_matching_denominations_breakdown(world):
    _, _, _, cmd, _, _ = world
    box = _register_box(cmd)
    session = _start_session(cmd, box)
    updated = cmd.enter_count(
        tenant_id=TENANT,
        session_id=session.id,
        counted_amount_irr=5_000_000,
        denominations=(
            DenominationCount(denomination_irr=1_000_000, count=4),
            DenominationCount(denomination_irr=500_000, count=2),
        ),
        actor="cashier-1",
    )
    assert updated.denominations_total_irr == 5_000_000
    assert updated.is_balanced is True


def test_enter_count_denominations_mismatch_is_rejected(world):
    _, _, _, cmd, _, _ = world
    box = _register_box(cmd)
    session = _start_session(cmd, box)
    with pytest.raises(CountDenominationsMismatchError) as excinfo:
        cmd.enter_count(
            tenant_id=TENANT,
            session_id=session.id,
            counted_amount_irr=5_000_000,
            denominations=(DenominationCount(denomination_irr=1_000_000, count=4),),
            actor="cashier-1",
        )
    assert excinfo.value.code == "COUNT_DENOMINATIONS_MISMATCH"


def test_enter_count_only_the_cashier_may_enter(world):
    _, _, _, cmd, _, _ = world
    box = _register_box(cmd)
    session = _start_session(cmd, box)
    with pytest.raises(CountWrongActorError) as excinfo:
        cmd.enter_count(tenant_id=TENANT, session_id=session.id, counted_amount_irr=5_000_000, actor="supervisor-1")
    assert excinfo.value.code == "COUNT_WRONG_ACTOR"


def test_enter_count_negative_amount_is_invalid(world):
    _, _, _, cmd, _, _ = world
    box = _register_box(cmd)
    session = _start_session(cmd, box)
    with pytest.raises(CashBoxInvalidError):
        cmd.enter_count(tenant_id=TENANT, session_id=session.id, counted_amount_irr=-1, actor="cashier-1")


def test_enter_count_can_be_corrected_before_any_signature(world):
    _, _, _, cmd, _, _ = world
    box = _register_box(cmd)
    session = _start_session(cmd, box)
    cmd.enter_count(tenant_id=TENANT, session_id=session.id, counted_amount_irr=4_900_000, actor="cashier-1")
    corrected = cmd.enter_count(
        tenant_id=TENANT, session_id=session.id, counted_amount_irr=4_950_000, actor="cashier-1"
    )
    assert corrected.counted_amount_irr == 4_950_000
    assert corrected.variance_irr == -50_000


def test_enter_count_is_locked_after_the_supervisor_attends(world):
    _, _, _, cmd, _, _ = world
    box = _register_box(cmd)
    session = _start_session(cmd, box)
    cmd.enter_count(tenant_id=TENANT, session_id=session.id, counted_amount_irr=5_000_000, actor="cashier-1")
    # Balanced session: the supervisor's signature archives it.
    cmd.sign_session(tenant_id=TENANT, session_id=session.id, role="supervisor", actor="supervisor-1")
    with pytest.raises(CountSessionInvalidStateError):
        cmd.enter_count(tenant_id=TENANT, session_id=session.id, counted_amount_irr=5_000_000, actor="cashier-1")


# ---------------------------------------------------------------------------
# Sessions: zero variance — supervisor attendance is enough
# ---------------------------------------------------------------------------


def test_balanced_session_archives_on_supervisor_attendance_without_minutes_or_journal(world):
    _, engine, _, cmd, _, _ = world
    box = _register_box(cmd)
    session = _start_session(cmd, box)
    cmd.enter_count(tenant_id=TENANT, session_id=session.id, counted_amount_irr=5_000_000, actor="cashier-1")
    archived = cmd.sign_session(tenant_id=TENANT, session_id=session.id, role="supervisor", actor="supervisor-1")
    assert archived.status is CountSessionStatus.ARCHIVED
    assert archived.witnessed_by == "supervisor-1"
    assert archived.minutes_text is None
    assert archived.review_item_id is None
    assert archived.draft_journal_id is None
    assert engine.drafts == {}


def test_balanced_session_has_no_minutes_to_sign(world):
    _, _, _, cmd, _, _ = world
    box = _register_box(cmd)
    session = _start_session(cmd, box)
    cmd.enter_count(tenant_id=TENANT, session_id=session.id, counted_amount_irr=5_000_000, actor="cashier-1")
    with pytest.raises(CountSessionInvalidStateError):
        cmd.sign_session(
            tenant_id=TENANT,
            session_id=session.id,
            role="cashier",
            actor="cashier-1",
            minutes_text="صورتجلسه.",
        )
    with pytest.raises(CountSessionInvalidStateError):
        cmd.sign_session(tenant_id=TENANT, session_id=session.id, role="manager", actor="manager-1")


# ---------------------------------------------------------------------------
# Sessions: variance — signed minutes, DRAFT journal, review item
# ---------------------------------------------------------------------------


def test_variance_session_completes_with_draft_and_review_item(world):
    _, engine, _, cmd, review_cmd, review_query = world
    box = _register_box(cmd)
    session = _start_session(cmd, box)
    completed = _complete_variance_session(cmd, session, counted=4_900_000)
    assert completed.status is CountSessionStatus.AWAITING_MANAGER
    assert completed.minutes_signed is True
    assert completed.minutes_text == "صورتجلسهٔ شمارش صندوق."
    assert completed.draft_journal_id is not None
    assert completed.review_item_id is not None
    # Policy (a): the draft debits the shortage/overage account, credits cash.
    draft = engine.drafts[completed.draft_journal_id]
    assert draft.company == COMPANY
    # The draft is booked to the resolved general journal — never left
    # to the adapter's single-journal fallback, which fails once the
    # engine holds several general journals.
    assert draft.journal == JournalRef(GENERAL_JOURNAL_ID)
    line_a, line_b = draft.lines
    assert line_a.account == AccountRef(VARIANCE_ACCOUNT_ID) and line_a.debit.amount == 100_000
    assert line_b.account == AccountRef(CASH_ACCOUNT_ID) and line_b.credit.amount == 100_000
    # The review item carries the variance figures, warning severity by default.
    view = review_query.get_item(completed.review_item_id, TENANT)
    assert view.item.item_type is ReviewItemType.CASHBOX_VARIANCE
    assert view.item.status is ReviewItemStatus.OPEN
    assert view.item.severity is ReviewSeverity.WARNING
    assert view.item.amount_irr == 100_000
    assert "کسری" in view.item.reason
    assert view.item.company_id == COMPANY_ROW_ID


def test_variance_session_overage_draft_reverses_the_lines(world):
    _, engine, _, cmd, _, _ = world
    box = _register_box(cmd)
    session = _start_session(cmd, box)
    completed = _complete_variance_session(cmd, session, counted=5_050_000)
    draft = engine.drafts[completed.draft_journal_id]
    line_a, line_b = draft.lines
    assert line_a.account == AccountRef(CASH_ACCOUNT_ID) and line_a.debit.amount == 50_000
    assert line_b.account == AccountRef(VARIANCE_ACCOUNT_ID) and line_b.credit.amount == 50_000


def test_variance_session_material_threshold_makes_the_item_critical(world):
    _, _, _, cmd, _, review_query = world
    cmd.update_settings(
        tenant_id=TENANT,
        variance_policy=VariancePolicy.EXPENSE,
        material_variance_threshold_irr=100_000,
    )
    box = _register_box(cmd)
    session = _start_session(cmd, box)
    completed = _complete_variance_session(cmd, session, counted=4_900_000)
    view = review_query.get_item(completed.review_item_id, TENANT)
    assert view.item.severity is ReviewSeverity.CRITICAL


def test_variance_session_below_material_threshold_stays_warning(world):
    _, _, _, cmd, _, review_query = world
    cmd.update_settings(
        tenant_id=TENANT,
        variance_policy=VariancePolicy.EXPENSE,
        material_variance_threshold_irr=200_000,
    )
    box = _register_box(cmd)
    session = _start_session(cmd, box)
    completed = _complete_variance_session(cmd, session, counted=4_900_000)
    view = review_query.get_item(completed.review_item_id, TENANT)
    assert view.item.severity is ReviewSeverity.WARNING


def test_variance_session_policy_b_shortage_uses_temporary_debtors(world):
    _, engine, _, cmd, _, _ = world
    cmd.update_settings(tenant_id=TENANT, variance_policy=VariancePolicy.RECEIVABLE_FROM_CASHIER)
    box = _register_box(cmd)
    session = _start_session(cmd, box)
    completed = _complete_variance_session(cmd, session, counted=4_900_000)
    draft = engine.drafts[completed.draft_journal_id]
    line_a, _ = draft.lines
    assert line_a.account == AccountRef(TEMP_DEBTORS_ACCOUNT_ID) and line_a.debit.amount == 100_000


def test_variance_session_policy_b_overage_uses_misc_creditors(world):
    _, engine, _, cmd, _, _ = world
    cmd.update_settings(tenant_id=TENANT, variance_policy=VariancePolicy.RECEIVABLE_FROM_CASHIER)
    box = _register_box(cmd)
    session = _start_session(cmd, box)
    completed = _complete_variance_session(cmd, session, counted=5_050_000)
    draft = engine.drafts[completed.draft_journal_id]
    _, line_b = draft.lines
    assert line_b.account == AccountRef(MISC_CREDITORS_ACCOUNT_ID) and line_b.credit.amount == 50_000


def test_variance_session_without_the_variance_account_in_the_chart_fails(world):
    cashbox, _, _, _, _, _ = world
    chart_without_variance = [view for view in CHART if view.code != "1.2.6"]
    engine = FakeCashBoxEngine(chart=chart_without_variance)
    queue = InMemoryReviewQueue()
    audit = InMemoryReviewAudit()
    review_query = ReviewQueueQueryService(queue, audit, now=NOW)
    review_cmd = ReviewQueueCommandService(queue, audit, now=NOW)
    cmd = CashBoxCommandService(cashbox, engine, review_cmd, review_query, now=NOW)
    box = _register_box(cmd)
    session = _start_session(cmd, box)
    cmd.enter_count(tenant_id=TENANT, session_id=session.id, counted_amount_irr=4_900_000, actor="cashier-1")
    cmd.sign_session(
        tenant_id=TENANT,
        session_id=session.id,
        role="cashier",
        actor="cashier-1",
        minutes_text="صورتجلسهٔ شمارش صندوق.",
    )
    cmd.sign_session(tenant_id=TENANT, session_id=session.id, role="supervisor", actor="supervisor-1")
    with pytest.raises(AccountNotFoundError):
        cmd.sign_session(tenant_id=TENANT, session_id=session.id, role="manager", actor="manager-1")


def test_cashier_signature_without_minutes_text_is_rejected(world):
    _, _, _, cmd, _, _ = world
    box = _register_box(cmd)
    session = _start_session(cmd, box)
    cmd.enter_count(tenant_id=TENANT, session_id=session.id, counted_amount_irr=4_900_000, actor="cashier-1")
    with pytest.raises(CountMinutesRequiredError) as excinfo:
        cmd.sign_session(tenant_id=TENANT, session_id=session.id, role="cashier", actor="cashier-1")
    assert excinfo.value.code == "COUNT_MINUTES_REQUIRED"


def test_manager_must_differ_from_cashier_and_supervisor(world):
    _, _, _, cmd, _, _ = world
    box = _register_box(cmd)
    session = _start_session(cmd, box)
    cmd.enter_count(tenant_id=TENANT, session_id=session.id, counted_amount_irr=4_900_000, actor="cashier-1")
    cmd.sign_session(
        tenant_id=TENANT,
        session_id=session.id,
        role="cashier",
        actor="cashier-1",
        minutes_text="صورتجلسهٔ شمارش صندوق.",
    )
    cmd.sign_session(tenant_id=TENANT, session_id=session.id, role="supervisor", actor="supervisor-1")
    with pytest.raises(CountWrongActorError):
        cmd.sign_session(tenant_id=TENANT, session_id=session.id, role="manager", actor="cashier-1")
    with pytest.raises(CountWrongActorError):
        cmd.sign_session(tenant_id=TENANT, session_id=session.id, role="manager", actor="supervisor-1")


def test_sign_with_wrong_actor_for_the_role_is_rejected(world):
    _, _, _, cmd, _, _ = world
    box = _register_box(cmd)
    session = _start_session(cmd, box)
    cmd.enter_count(tenant_id=TENANT, session_id=session.id, counted_amount_irr=4_900_000, actor="cashier-1")
    with pytest.raises(CountWrongActorError):
        cmd.sign_session(
            tenant_id=TENANT,
            session_id=session.id,
            role="cashier",
            actor="someone-else",
            minutes_text="صورتجلسه.",
        )
    with pytest.raises(CountWrongActorError):
        cmd.sign_session(tenant_id=TENANT, session_id=session.id, role="supervisor", actor="cashier-1")


def test_sign_with_an_unknown_role_is_invalid(world):
    _, _, _, cmd, _, _ = world
    box = _register_box(cmd)
    session = _start_session(cmd, box)
    cmd.enter_count(tenant_id=TENANT, session_id=session.id, counted_amount_irr=4_900_000, actor="cashier-1")
    with pytest.raises(CashBoxInvalidError):
        cmd.sign_session(tenant_id=TENANT, session_id=session.id, role="auditor", actor="auditor-1")


def test_sign_before_the_count_is_entered_is_an_invalid_state(world):
    _, _, _, cmd, _, _ = world
    box = _register_box(cmd)
    session = _start_session(cmd, box)
    with pytest.raises(CountSessionInvalidStateError):
        cmd.sign_session(tenant_id=TENANT, session_id=session.id, role="supervisor", actor="supervisor-1")


def test_double_supervisor_attendance_is_rejected(world):
    _, _, _, cmd, _, _ = world
    box = _register_box(cmd)
    session = _start_session(cmd, box)
    cmd.enter_count(tenant_id=TENANT, session_id=session.id, counted_amount_irr=4_900_000, actor="cashier-1")
    cmd.sign_session(tenant_id=TENANT, session_id=session.id, role="supervisor", actor="supervisor-1")
    with pytest.raises(CountSessionInvalidStateError):
        cmd.sign_session(tenant_id=TENANT, session_id=session.id, role="supervisor", actor="supervisor-1")


def test_double_cashier_signature_is_rejected(world):
    _, _, _, cmd, _, _ = world
    box = _register_box(cmd)
    session = _start_session(cmd, box)
    cmd.enter_count(tenant_id=TENANT, session_id=session.id, counted_amount_irr=4_900_000, actor="cashier-1")
    cmd.sign_session(
        tenant_id=TENANT,
        session_id=session.id,
        role="cashier",
        actor="cashier-1",
        minutes_text="صورتجلسهٔ شمارش صندوق.",
    )
    with pytest.raises(CountSessionInvalidStateError):
        cmd.sign_session(
            tenant_id=TENANT,
            session_id=session.id,
            role="cashier",
            actor="cashier-1",
            minutes_text="صورتجلسهٔ دوم.",
        )


def test_enter_count_is_locked_once_the_supervisor_signs_a_variance_session(world):
    _, _, _, cmd, _, _ = world
    box = _register_box(cmd)
    session = _start_session(cmd, box)
    cmd.enter_count(tenant_id=TENANT, session_id=session.id, counted_amount_irr=4_900_000, actor="cashier-1")
    cmd.sign_session(tenant_id=TENANT, session_id=session.id, role="supervisor", actor="supervisor-1")
    with pytest.raises(CountSessionInvalidStateError):
        cmd.enter_count(tenant_id=TENANT, session_id=session.id, counted_amount_irr=4_950_000, actor="cashier-1")


def test_failed_completion_can_be_retried_by_a_re_sign(world):
    cashbox, _, _, _, _, _ = world
    chart_without_variance = [view for view in CHART if view.code != "1.2.6"]
    engine = FakeCashBoxEngine(chart=chart_without_variance)
    queue = InMemoryReviewQueue()
    audit = InMemoryReviewAudit()
    review_query = ReviewQueueQueryService(queue, audit, now=NOW)
    review_cmd = ReviewQueueCommandService(queue, audit, now=NOW)
    cmd = CashBoxCommandService(cashbox, engine, review_cmd, review_query, now=NOW)
    box = _register_box(cmd)
    session = _start_session(cmd, box)
    cmd.enter_count(tenant_id=TENANT, session_id=session.id, counted_amount_irr=4_900_000, actor="cashier-1")
    cmd.sign_session(
        tenant_id=TENANT,
        session_id=session.id,
        role="cashier",
        actor="cashier-1",
        minutes_text="صورتجلسهٔ شمارش صندوق.",
    )
    cmd.sign_session(tenant_id=TENANT, session_id=session.id, role="supervisor", actor="supervisor-1")
    with pytest.raises(AccountNotFoundError):
        cmd.sign_session(tenant_id=TENANT, session_id=session.id, role="manager", actor="manager-1")
    # The signatures are recorded; the session is stuck awaiting the
    # supervisor until the chart is fixed — then a re-sign by the
    # manager retries the completion instead of a duplicate error.
    engine.set_chart(CHART)
    completed = cmd.sign_session(tenant_id=TENANT, session_id=session.id, role="manager", actor="manager-1")
    assert completed.status is CountSessionStatus.AWAITING_MANAGER
    assert completed.draft_journal_id is not None
    assert completed.review_item_id is not None


def test_completion_retry_by_a_non_signer_is_rejected(world):
    cashbox, _, _, _, _, _ = world
    chart_without_variance = [view for view in CHART if view.code != "1.2.6"]
    engine = FakeCashBoxEngine(chart=chart_without_variance)
    queue = InMemoryReviewQueue()
    audit = InMemoryReviewAudit()
    review_query = ReviewQueueQueryService(queue, audit, now=NOW)
    review_cmd = ReviewQueueCommandService(queue, audit, now=NOW)
    cmd = CashBoxCommandService(cashbox, engine, review_cmd, review_query, now=NOW)
    box = _register_box(cmd)
    session = _start_session(cmd, box)
    cmd.enter_count(tenant_id=TENANT, session_id=session.id, counted_amount_irr=4_900_000, actor="cashier-1")
    cmd.sign_session(
        tenant_id=TENANT,
        session_id=session.id,
        role="cashier",
        actor="cashier-1",
        minutes_text="صورتجلسهٔ شمارش صندوق.",
    )
    cmd.sign_session(tenant_id=TENANT, session_id=session.id, role="supervisor", actor="supervisor-1")
    with pytest.raises(AccountNotFoundError):
        cmd.sign_session(tenant_id=TENANT, session_id=session.id, role="manager", actor="manager-1")
    with pytest.raises(CountWrongActorError):
        cmd.sign_session(tenant_id=TENANT, session_id=session.id, role="manager", actor="manager-2")


# ---------------------------------------------------------------------------
# Posting: never without the manager's approval
# ---------------------------------------------------------------------------


def test_post_without_manager_approval_is_refused(world):
    _, engine, _, cmd, _, _ = world
    box = _register_box(cmd)
    session = _start_session(cmd, box)
    completed = _complete_variance_session(cmd, session, counted=4_900_000)
    with pytest.raises(CountReviewNotApprovedError) as excinfo:
        cmd.post_variance_journal(tenant_id=TENANT, session_id=completed.id, actor="manager-1")
    assert excinfo.value.code == "COUNT_REVIEW_NOT_APPROVED"
    assert engine.posted == []


def test_post_after_manager_approval_posts_the_draft_and_archives(world):
    _, engine, _, cmd, review_cmd, _ = world
    box = _register_box(cmd)
    session = _start_session(cmd, box)
    completed = _complete_variance_session(cmd, session, counted=4_900_000)
    review_cmd.perform_action(
        item_id=completed.review_item_id,
        tenant_id=TENANT,
        action_code="approve_variance_entry",
        actor="manager-1",
        note="صورتجلسه بررسی شد؛ سند تأیید می‌شود.",
    )
    posted = cmd.post_variance_journal(tenant_id=TENANT, session_id=completed.id, actor="manager-1")
    assert posted.status is CountSessionStatus.ARCHIVED
    assert posted.posted_journal_id == completed.draft_journal_id
    assert engine.posted == [completed.draft_journal_id]


def test_post_is_refused_when_the_item_was_resolved_another_way(world):
    _, _, _, cmd, review_cmd, _ = world
    box = _register_box(cmd)
    session = _start_session(cmd, box)
    completed = _complete_variance_session(cmd, session, counted=4_900_000)
    # escalate_to_manager does not resolve the item — the post still needs
    # the approve_variance_entry action.
    review_cmd.perform_action(
        item_id=completed.review_item_id,
        tenant_id=TENANT,
        action_code="escalate_to_manager",
        actor="supervisor-1",
    )
    with pytest.raises(CountReviewNotApprovedError):
        cmd.post_variance_journal(tenant_id=TENANT, session_id=completed.id, actor="manager-1")


def test_post_on_a_session_without_a_variance_journal_is_an_invalid_state(world):
    _, _, _, cmd, _, _ = world
    box = _register_box(cmd)
    session = _start_session(cmd, box)
    with pytest.raises(CountSessionInvalidStateError):
        cmd.post_variance_journal(tenant_id=TENANT, session_id=session.id, actor="manager-1")


# ---------------------------------------------------------------------------
# Recount
# ---------------------------------------------------------------------------


def test_recount_without_a_manager_request_is_refused(world):
    _, _, _, cmd, _, _ = world
    box = _register_box(cmd)
    session = _start_session(cmd, box)
    completed = _complete_variance_session(cmd, session, counted=4_900_000)
    with pytest.raises(CountRecountNotRequestedError) as excinfo:
        cmd.recount(tenant_id=TENANT, session_id=completed.id, actor="cashier-1")
    assert excinfo.value.code == "COUNT_RECOUNT_NOT_REQUESTED"


def test_recount_after_manager_request_reopens_and_deletes_the_draft(world):
    _, engine, _, cmd, review_cmd, _ = world
    box = _register_box(cmd)
    session = _start_session(cmd, box)
    completed = _complete_variance_session(cmd, session, counted=4_900_000)
    draft_id = completed.draft_journal_id
    assert draft_id in engine.drafts
    review_cmd.perform_action(
        item_id=completed.review_item_id,
        tenant_id=TENANT,
        action_code="recount",
        actor="manager-1",
        note="به نتیجهٔ شمارش مشکوک هستم؛ دوباره بشمارید.",
    )
    reopened = cmd.recount(tenant_id=TENANT, session_id=completed.id, actor="cashier-1")
    assert reopened.status is CountSessionStatus.IN_PROGRESS
    assert reopened.counted_amount_irr is None
    assert reopened.minutes_text is None
    assert reopened.witnessed_by is None
    assert reopened.draft_journal_id is None
    assert reopened.review_item_id == completed.review_item_id
    assert draft_id not in engine.drafts


def test_recount_tolerates_an_already_deleted_draft(world):
    _, engine, _, cmd, review_cmd, _ = world
    box = _register_box(cmd)
    session = _start_session(cmd, box)
    completed = _complete_variance_session(cmd, session, counted=4_900_000)
    # The draft vanished out-of-band; the recount still proceeds.
    del engine.drafts[completed.draft_journal_id]
    review_cmd.perform_action(
        item_id=completed.review_item_id,
        tenant_id=TENANT,
        action_code="recount",
        actor="manager-1",
    )
    reopened = cmd.recount(tenant_id=TENANT, session_id=completed.id, actor="cashier-1")
    assert reopened.status is CountSessionStatus.IN_PROGRESS


def test_recount_completion_refreshes_the_same_review_item(world):
    _, engine, _, cmd, review_cmd, review_query = world
    box = _register_box(cmd)
    session = _start_session(cmd, box)
    completed = _complete_variance_session(cmd, session, counted=4_900_000)
    review_cmd.perform_action(
        item_id=completed.review_item_id,
        tenant_id=TENANT,
        action_code="recount",
        actor="manager-1",
    )
    reopened = cmd.recount(tenant_id=TENANT, session_id=completed.id, actor="cashier-1")
    # The recount finds a smaller shortage: -40,000 instead of -100,000.
    second = _complete_variance_session(cmd, reopened, counted=4_960_000)
    assert second.review_item_id == completed.review_item_id
    assert second.draft_journal_id != completed.draft_journal_id
    view = review_query.get_item(completed.review_item_id, TENANT)
    assert view.item.status is ReviewItemStatus.OPEN
    assert view.item.amount_irr == 40_000
    assert "40,000" in view.item.reason
    assert any(entry.action == "figures_updated" for entry in view.item.audit)
    draft = engine.drafts[second.draft_journal_id]
    assert draft.lines[0].debit.amount == 40_000


def test_zero_variance_recount_archives_and_zeroes_the_item(world):
    _, _, _, cmd, review_cmd, review_query = world
    box = _register_box(cmd)
    session = _start_session(cmd, box)
    completed = _complete_variance_session(cmd, session, counted=4_900_000)
    review_cmd.perform_action(
        item_id=completed.review_item_id,
        tenant_id=TENANT,
        action_code="recount",
        actor="manager-1",
    )
    reopened = cmd.recount(tenant_id=TENANT, session_id=completed.id, actor="cashier-1")
    # The recount finds the count matches the books exactly.
    cmd.enter_count(tenant_id=TENANT, session_id=reopened.id, counted_amount_irr=5_000_000, actor="cashier-1")
    archived = cmd.sign_session(tenant_id=TENANT, session_id=reopened.id, role="supervisor", actor="supervisor-1")
    assert archived.status is CountSessionStatus.ARCHIVED
    # The item's figures are refreshed to zero (info severity) so the
    # manager closes an honest item; the session's post gate checks
    # the session's current state, so approving it can never post a
    # journal.
    view = review_query.get_item(completed.review_item_id, TENANT)
    assert view.item.status is ReviewItemStatus.OPEN
    assert view.item.amount_irr == 0
    assert view.item.severity is ReviewSeverity.INFO
    assert "برابر است" in view.item.reason


def test_recount_completing_after_the_manager_closed_the_item_files_a_new_one(world):
    _, _, _, cmd, review_cmd, review_query = world
    box = _register_box(cmd)
    session = _start_session(cmd, box)
    completed = _complete_variance_session(cmd, session, counted=4_900_000)
    review_cmd.perform_action(
        item_id=completed.review_item_id,
        tenant_id=TENANT,
        action_code="recount",
        actor="manager-1",
    )
    reopened = cmd.recount(tenant_id=TENANT, session_id=completed.id, actor="cashier-1")
    # The manager closes the old item while the recount is in flight.
    review_cmd.perform_action(
        item_id=completed.review_item_id,
        tenant_id=TENANT,
        action_code="approve_variance_entry",
        actor="manager-1",
    )
    second = _complete_variance_session(cmd, reopened, counted=4_960_000)
    assert second.review_item_id is not None
    assert second.review_item_id != completed.review_item_id
    view = review_query.get_item(second.review_item_id, TENANT)
    assert view.item.status is ReviewItemStatus.OPEN
    assert view.item.amount_irr == 40_000
    old_view = review_query.get_item(completed.review_item_id, TENANT)
    assert old_view.item.status is ReviewItemStatus.RESOLVED
    assert old_view.item.resolution is ReviewResolution.ADJUSTED


# ---------------------------------------------------------------------------
# Review queue: update_item_figures
# ---------------------------------------------------------------------------


def test_update_item_figures_refreshes_an_open_item(world):
    _, _, _, _, review_cmd, review_query = world
    item = review_cmd.file_item(
        tenant_id=TENANT,
        item_type=ReviewItemType.CASHBOX_VARIANCE,
        severity=ReviewSeverity.WARNING,
        reason="کسری صندوق: ۱۰۰٬۰۰۰ ریال.",
        suggested_action="بررسی کنید.",
        amount_irr=100_000,
        actor="cashier-1",
    )
    updated = review_cmd.update_item_figures(
        item_id=item.id,
        tenant_id=TENANT,
        reason="کسری صندوق: ۴۰٬۰۰۰ ریال.",
        suggested_action="بررسی کنید.",
        amount_irr=40_000,
        severity=ReviewSeverity.CRITICAL,
        actor="cashier-1",
        note="شمارش مجدد انجام شد.",
    )
    assert updated.status is ReviewItemStatus.OPEN
    assert updated.reason == "کسری صندوق: ۴۰٬۰۰۰ ریال."
    assert updated.amount_irr == 40_000
    assert updated.severity is ReviewSeverity.CRITICAL
    view = review_query.get_item(item.id, TENANT)
    assert any(entry.action == "figures_updated" for entry in view.item.audit)


def test_update_item_figures_on_a_terminal_item_is_an_invalid_state(world):
    _, _, _, _, review_cmd, _ = world
    item = review_cmd.file_item(
        tenant_id=TENANT,
        item_type=ReviewItemType.CASHBOX_VARIANCE,
        severity=ReviewSeverity.WARNING,
        reason="کسری صندوق.",
        suggested_action="بررسی کنید.",
        amount_irr=100_000,
        actor="cashier-1",
    )
    review_cmd.perform_action(
        item_id=item.id,
        tenant_id=TENANT,
        action_code="approve_variance_entry",
        actor="manager-1",
    )
    with pytest.raises(ReviewInvalidStateError):
        review_cmd.update_item_figures(
            item_id=item.id,
            tenant_id=TENANT,
            reason="کسری صندوق: ۴۰٬۰۰۰ ریال.",
            suggested_action="بررسی کنید.",
            amount_irr=40_000,
            severity=ReviewSeverity.WARNING,
            actor="cashier-1",
        )


# ---------------------------------------------------------------------------
# Query service: boxes, sessions, report
# ---------------------------------------------------------------------------


def test_list_cash_boxes_carries_live_balances(world):
    _, _, query, cmd, _, _ = world
    _register_box(cmd)
    _register_box(cmd, journal_id=11, kind=CashBoxKind.PETTY_CASH, name_fa="تنخواه", ceiling_irr=2_000_000)
    views = query.list_cash_boxes(TENANT, COMPANY, COMPANY_ROW_ID)
    assert len(views) == 2
    by_kind = {view.box.kind: view for view in views}
    assert by_kind[CashBoxKind.STORE].book_balance_irr == 5_000_000
    # The petty-cash account has no balance in the fake engine: honestly None.
    assert by_kind[CashBoxKind.PETTY_CASH].book_balance_irr is None
    assert by_kind[CashBoxKind.STORE].days_since_last_count is None
    assert by_kind[CashBoxKind.STORE].uncounted_alert is False


def test_list_cash_boxes_flags_an_uncounted_box(world):
    cashbox, engine, _, cmd, _, _ = world
    box = _register_box(cmd)
    # A session counted 10 days ago; the reminder threshold is 7 days.
    old_session = CountSession(
        id="old-session",
        tenant_id=TENANT,
        company_id=COMPANY_ROW_ID,
        cash_box_id=box.id,
        cashier="cashier-1",
        supervisor="supervisor-1",
        status=CountSessionStatus.ARCHIVED,
        book_balance_irr=5_000_000,
        created_at=NOW - datetime.timedelta(days=10),
        counted_amount_irr=5_000_000,
    )
    cashbox.save_session(old_session)
    query = CashBoxQueryService(cashbox, engine, now=NOW)
    (view,) = query.list_cash_boxes(TENANT, COMPANY, COMPANY_ROW_ID)
    assert view.days_since_last_count == 10
    assert view.uncounted_alert is True


def test_list_cash_boxes_unknown_company_raises_not_found(world):
    _, _, query, _, _, _ = world
    with pytest.raises(CompanyNotFoundError):
        query.list_cash_boxes(TENANT, CompanyRef(999), "row-999")


def test_get_cash_box_unknown_id_raises_not_found(world):
    _, _, query, _, _, _ = world
    with pytest.raises(CashBoxNotFoundError):
        query.get_cash_box(TENANT, "no-such-box")


def test_list_statement_journals_returns_the_company_journals(world):
    _, _, query, _, _, _ = world
    journals = query.list_statement_journals(COMPANY)
    assert [journal.ref.engine_id for journal in journals] == [10, 11]
    assert journals[0].account_code == "1.1.1"


def test_list_sessions_filters_by_box_and_status(world):
    cashbox, _, _, _, _, _ = world
    engine = FakeCashBoxEngine(balances={CASH_ACCOUNT_ID: 5_000_000, PETTY_ACCOUNT_ID: 1_000_000})
    queue = InMemoryReviewQueue()
    audit = InMemoryReviewAudit()
    review_query = ReviewQueueQueryService(queue, audit, now=NOW)
    review_cmd = ReviewQueueCommandService(queue, audit, now=NOW)
    query = CashBoxQueryService(cashbox, engine, now=NOW)
    cmd = CashBoxCommandService(cashbox, engine, review_cmd, review_query, now=NOW)
    box = _register_box(cmd)
    other = _register_box(cmd, journal_id=11, kind=CashBoxKind.PETTY_CASH, name_fa="تنخواه", ceiling_irr=2_000_000)
    session = _start_session(cmd, box)
    _start_session(cmd, other)
    by_box = query.list_sessions(TENANT, CountSessionFilters(cash_box_id=box.id))
    assert [item.id for item in by_box] == [session.id]
    in_progress = query.list_sessions(TENANT, CountSessionFilters(status=CountSessionStatus.IN_PROGRESS))
    assert len(in_progress) == 2
    archived = query.list_sessions(TENANT, CountSessionFilters(status=CountSessionStatus.ARCHIVED))
    assert archived == []


def test_get_session_unknown_id_raises_not_found(world):
    _, _, query, _, _, _ = world
    with pytest.raises(CountSessionNotFoundError):
        query.get_session(TENANT, "no-such-session")


def _invoice(
    invoice_id: int,
    invoice_date: FiscalDate | None,
    total: int,
) -> InvoiceView:
    return InvoiceView(
        ref=InvoiceRef(invoice_id),
        company=COMPANY,
        invoice_type="out",
        party=PartyRef(1),
        party_name="مشتری",
        number=f"INV-{invoice_id}",
        invoice_date=invoice_date,
        accounting_date=invoice_date,
        state="posted",
        description="",
        reference="",
        journal=None,
        account=AccountRef(1),
        currency_code="IRR",
        untaxed_amount=MoneyIRR(total),
        tax_amount=MoneyIRR(0),
        total_amount=MoneyIRR(total),
    )


def test_cashbox_report_math(world):
    cashbox, _, _, cmd, _, _ = world
    date_from = FiscalDate(datetime.date(2026, 11, 10))
    date_to = FiscalDate(datetime.date(2026, 11, 18))
    engine = FakeCashBoxEngine(
        balances_by_date={
            (CASH_ACCOUNT_ID, datetime.date(2026, 11, 9)): 4_000_000,
            (CASH_ACCOUNT_ID, datetime.date(2026, 11, 18)): 5_000_000,
        },
        trial_lines=[
            TrialBalanceLineView(
                account=AccountRef(CASH_ACCOUNT_ID),
                code="1.1.1",
                name="Cash",
                total_debit=MoneyIRR(2_000_000),
                total_credit=MoneyIRR(1_000_000),
            )
        ],
        invoices=[
            _invoice(1, FiscalDate(datetime.date(2026, 11, 12)), 1_500_000),
            _invoice(2, FiscalDate(datetime.date(2026, 11, 1)), 9_000_000),  # before the period
            _invoice(3, None, 7_000_000),  # undated: skipped
        ],
    )
    query = CashBoxQueryService(cashbox, engine, now=NOW)
    box = _register_box(cmd)
    # Two sessions in the period: a shortage of 100,000 and an overage of 60,000.
    shortage = CountSession(
        id="session-shortage",
        tenant_id=TENANT,
        company_id=COMPANY_ROW_ID,
        cash_box_id=box.id,
        cashier="cashier-1",
        supervisor="supervisor-1",
        status=CountSessionStatus.ARCHIVED,
        book_balance_irr=5_000_000,
        created_at=datetime.datetime(2026, 11, 12, 10, 0, tzinfo=datetime.UTC),
        counted_amount_irr=4_900_000,
    )
    overage = CountSession(
        id="session-overage",
        tenant_id=TENANT,
        company_id=COMPANY_ROW_ID,
        cash_box_id=box.id,
        cashier="cashier-1",
        supervisor="supervisor-1",
        status=CountSessionStatus.ARCHIVED,
        book_balance_irr=5_000_000,
        created_at=datetime.datetime(2026, 11, 15, 10, 0, tzinfo=datetime.UTC),
        counted_amount_irr=5_060_000,
    )
    outside = CountSession(
        id="session-outside",
        tenant_id=TENANT,
        company_id=COMPANY_ROW_ID,
        cash_box_id=box.id,
        cashier="cashier-1",
        supervisor="supervisor-1",
        status=CountSessionStatus.ARCHIVED,
        book_balance_irr=5_000_000,
        created_at=datetime.datetime(2026, 11, 1, 10, 0, tzinfo=datetime.UTC),
        counted_amount_irr=4_000_000,
    )
    cashbox.save_session(shortage)
    cashbox.save_session(overage)
    cashbox.save_session(outside)
    report = query.cashbox_report(TENANT, COMPANY, COMPANY_ROW_ID, date_from, date_to)
    assert report.company_id == COMPANY_ROW_ID
    assert report.sales_source == "tryton_invoices"
    assert report.generated_at == NOW
    (line,) = report.lines
    assert line.opening_book_balance_irr == 4_000_000
    assert line.closing_book_balance_irr == 5_000_000
    assert line.receipts_irr == 2_000_000
    assert line.payments_irr == 1_000_000
    assert line.session_count == 2
    assert line.shortage_irr == 100_000
    assert line.overage_irr == 60_000
    assert line.net_variance_irr == -40_000
    assert line.recorded_sales_irr == 1_500_000
    assert line.possible_unrecorded_sales_irr == 0
    assert report.total_shortage_irr == 100_000
    assert report.total_overage_irr == 60_000
    assert report.total_possible_unrecorded_sales_irr == 0


def test_cashbox_report_reversed_dates_are_invalid(world):
    _, _, query, _, _, _ = world
    with pytest.raises(CashBoxInvalidError):
        query.cashbox_report(
            TENANT,
            COMPANY,
            COMPANY_ROW_ID,
            FiscalDate(datetime.date(2026, 11, 18)),
            FiscalDate(datetime.date(2026, 11, 10)),
        )


def test_cashbox_report_unknown_box_is_not_found(world):
    _, _, query, _, _, _ = world
    with pytest.raises(CashBoxNotFoundError):
        query.cashbox_report(
            TENANT,
            COMPANY,
            COMPANY_ROW_ID,
            FiscalDate(datetime.date(2026, 11, 10)),
            FiscalDate(datetime.date(2026, 11, 18)),
            cash_box_id="no-such-box",
        )
