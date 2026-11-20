"""Unit tests for the journal draft service (US-202).

No live server is needed: the engine is faked in memory with a small
move store that mimics the adapter's observable behaviour (draft/posted
states, reversal linkage, the non-atomic reversal failure that leaves
a surviving reversal draft).
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import (
    Decimal,
)

import pytest

from hesabyar.application.commands.journals import JournalDraftService
from hesabyar.application.errors import EngineError
from hesabyar.domain.accounting.commands import (
    CreateAnalyticAccount,
    CreateBankStatement,
    CreateConsumptionMove,
    CreateCreditNote,
    CreateEngineCheque,
    CreateInventory,
    CreateInvoiceDraft,
    CreateJournalDraft,
    CreatePackagingProduct,
    CreatePayment,
    CreateSale,
    JournalDraftLine,
    PostJournal,
    ReconcileMoveLines,
    ReverseJournal,
    SucceedPayment,
    TransitionEngineCheque,
    UpdateBankStatementLine,
    UpdateInventoryLine,
    UpdateJournalDraft,
    UpsertOrderPoint,
)
from hesabyar.domain.accounting.refs import (
    AccountRef,
    AnalyticAccountRef,
    BankStatementRef,
    CheckbookEngineRef,
    ChequeEngineRef,
    CompanyRef,
    FiscalYearRef,
    InventoryRef,
    InvoiceRef,
    JournalDraftRef,
    JournalRef,
    OutShipmentRef,
    PartyRef,
    PaymentJournalRef,
    PaymentRef,
    PostedJournalRef,
    ProductRef,
    SaleRef,
    StockLocationRef,
)
from hesabyar.domain.accounting.value_objects import (
    AccountBalanceView,
    AccountView,
    AnalyticAccountView,
    AnalyticLineView,
    BankStatementView,
    CheckbookEngineView,
    ChequeEngineView,
    CompanyView,
    FiscalPeriodView,
    FiscalYearView,
    InventoryView,
    InvoiceTotalView,
    InvoiceView,
    JournalDraftView,
    JournalLineView,
    MoveLineView,
    OrderPointView,
    OutShipmentCostView,
    PackagingProductView,
    PaymentJournalView,
    PaymentView,
    PostedJournalView,
    PurchaseRequestView,
    ReconciliationView,
    SaleShipmentCostView,
    SaleView,
    StatementJournalView,
    StockLocationView,
    StockMoveView,
    TrialBalanceView,
    ValidationIssue,
    ValidationSummary,
)
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.errors import (
    HesabYarError,
    InvalidJournalError,
    JournalAlreadyPostedError,
    JournalDraftNotFoundError,
    PostedJournalNotFoundError,
    UnbalancedJournalError,
)
from hesabyar.domain.common.money import MoneyIRR

COMPANY = CompanyRef(101)
OTHER_COMPANY = CompanyRef(102)
JOURNAL = JournalRef(7)
DEBIT_ACCOUNT = AccountRef(11)
CREDIT_ACCOUNT = AccountRef(12)
MOVE_DATE = FiscalDate.from_gregorian(datetime.date(2026, 11, 16))
PERIOD = FiscalPeriodView(
    name="Nov 2026",
    start_date=FiscalDate.from_gregorian(datetime.date(2026, 11, 1)),
    end_date=FiscalDate.from_gregorian(datetime.date(2026, 11, 30)),
    state="open",
)


def _lines(amount: int = 1_000_000) -> tuple[JournalDraftLine, ...]:
    return (
        JournalDraftLine(account=DEBIT_ACCOUNT, debit=MoneyIRR(amount)),
        JournalDraftLine(account=CREDIT_ACCOUNT, credit=MoneyIRR(amount)),
    )


@dataclass
class _Move:
    """One stored move: its draft view plus mutable state and origin."""

    view: JournalDraftView
    state: str = "draft"
    origin: PostedJournalRef | None = None
    number: str = ""


@dataclass
class ReverseFailure:
    """How the fake's reverse_journal should fail (US-201 handoff)."""

    error: HesabYarError
    creates_draft: bool = False
    draft_already_posted: bool = False


class FakeJournalEngine:
    """In-memory journal engine for unit tests (no server)."""

    def __init__(self, companies: Sequence[CompanyView] | None = None) -> None:
        self._companies = list(
            companies or [CompanyView(ref=COMPANY, name="Acme", currency_code="IRR")]
        )
        self._moves: dict[int, _Move] = {}
        self._next_id = 1
        self.create_calls = 0
        self.post_calls = 0
        self.reverse_calls = 0
        self.failure: HesabYarError | None = None
        self.invalid_drafts: set[int] = set()
        self.reverse_failure: ReverseFailure | None = None
        self.fail_post_once: HesabYarError | None = None

    # -- helpers -----------------------------------------------------------

    def _store(
        self, company: CompanyRef, date: FiscalDate, lines: Sequence[JournalDraftLine],
        description: str, origin: PostedJournalRef | None = None,
    ) -> int:
        move_id = self._next_id
        self._next_id += 1
        view = JournalDraftView(
            ref=JournalDraftRef(move_id),
            company=company,
            journal=JOURNAL,
            period=PERIOD,
            date=date,
            state="draft",
            description=description,
            lines=tuple(
                JournalLineView(
                    account=line.account,
                    debit=line.debit,
                    credit=line.credit,
                    state="valid",
                    description=line.description,
                )
                for line in lines
            ),
        )
        self._moves[move_id] = _Move(view=view, origin=origin)
        return move_id

    def _posted_view(self, move_id: int) -> PostedJournalView:
        move = self._moves[move_id]
        view = move.view
        return PostedJournalView(
            ref=PostedJournalRef(move_id),
            company=view.company,
            journal=view.journal,
            period=view.period,
            date=view.date,
            post_date=view.date,
            number=move.number or f"M-{move_id}",
            description=view.description,
            origin=move.origin,
            lines=view.lines,
        )

    @staticmethod
    def _current_view(move: _Move) -> JournalDraftView:
        """The move's view with its live state (draft vs posted)."""
        view = move.view
        return JournalDraftView(
            ref=view.ref,
            company=view.company,
            journal=view.journal,
            period=view.period,
            date=view.date,
            state=move.state,
            description=view.description,
            lines=view.lines,
        )

    # -- port ----------------------------------------------------------------

    def list_companies(self) -> Sequence[CompanyView]:
        if self.failure is not None:
            raise self.failure
        return list(self._companies)

    def get_chart_of_accounts(self, company: CompanyRef) -> Sequence[AccountView]:
        return []

    def create_journal_draft(self, command: CreateJournalDraft) -> JournalDraftRef:
        if self.failure is not None:
            raise self.failure
        self.create_calls += 1
        move_id = self._store(
            command.company, command.date, command.lines, command.description
        )
        return JournalDraftRef(move_id)

    def get_journal_draft(self, draft: JournalDraftRef) -> JournalDraftView | None:
        move = self._moves.get(draft.engine_id)
        if move is None:
            return None
        return self._current_view(move)

    def list_journal_drafts(self, company: CompanyRef) -> Sequence[JournalDraftView]:
        return [
            self._current_view(move)
            for move_id, move in sorted(self._moves.items(), reverse=True)
            if move.view.company == company and move.state == "draft"
        ]

    def update_journal_draft(self, command: UpdateJournalDraft) -> JournalDraftView:
        move = self._moves.get(command.draft.engine_id)
        if move is None:
            raise JournalDraftNotFoundError(
                f"Journal draft {command.draft.engine_id} does not exist.",
                details={"draft_id": command.draft.engine_id},
            )
        if move.state == "posted":
            raise JournalAlreadyPostedError(
                f"Journal {command.draft.engine_id} is already posted.",
                details={"draft_id": command.draft.engine_id},
            )
        view = move.view
        lines = view.lines
        if command.lines is not None:
            lines = tuple(
                JournalLineView(
                    account=line.account,
                    debit=line.debit,
                    credit=line.credit,
                    state="valid",
                    description=line.description,
                )
                for line in command.lines
            )
        move.view = JournalDraftView(
            ref=view.ref,
            company=view.company,
            journal=view.journal,
            period=view.period,
            date=command.date if command.date is not None else view.date,
            state=view.state,
            description=(
                command.description
                if command.description is not None
                else view.description
            ),
            lines=lines,
        )
        return self._current_view(move)

    def list_invoice_totals(
        self,
        company: CompanyRef,
        invoice_type: str | None = None,
        state: str | None = None,
    ) -> Sequence[InvoiceTotalView]:
        raise NotImplementedError

    def get_account_by_code(self, company: CompanyRef, code: str) -> AccountView | None:
        raise NotImplementedError

    def get_general_journal(self, company: CompanyRef) -> JournalRef:
        raise NotImplementedError

    def delete_journal_draft(self, draft: JournalDraftRef) -> None:
        raise NotImplementedError

    def get_account_balance(
        self, company: CompanyRef, account: AccountRef, as_of: FiscalDate
    ) -> AccountBalanceView:
        raise NotImplementedError

    def validate_journal_draft(self, draft: JournalDraftRef) -> ValidationSummary:
        if draft.engine_id not in self._moves:
            raise JournalDraftNotFoundError(
                f"Journal draft {draft.engine_id} does not exist.",
                details={"draft_id": draft.engine_id},
            )
        if draft.engine_id in self.invalid_drafts:
            return ValidationSummary.failed(
                [ValidationIssue(code="LEDGER_UNBALANCED", message="Unbalanced.")]
            )
        return ValidationSummary.ok()

    def post_journal(self, command: PostJournal) -> PostedJournalRef:
        self.post_calls += 1
        if self.fail_post_once is not None:
            error = self.fail_post_once
            self.fail_post_once = None
            raise error
        move = self._moves.get(command.draft.engine_id)
        if move is None:
            raise JournalDraftNotFoundError(
                f"Journal draft {command.draft.engine_id} does not exist.",
                details={"draft_id": command.draft.engine_id},
            )
        if move.state == "posted":
            raise JournalAlreadyPostedError(
                f"Journal {command.draft.engine_id} is already posted.",
                details={"draft_id": command.draft.engine_id},
            )
        move.state = "posted"
        move.number = f"M-{command.draft.engine_id}"
        return PostedJournalRef(engine_id=command.draft.engine_id)

    def reverse_journal(self, command: ReverseJournal) -> PostedJournalRef:
        self.reverse_calls += 1
        original = self._moves.get(command.posted.engine_id)
        if original is None:
            raise PostedJournalNotFoundError(
                f"Posted journal {command.posted.engine_id} does not exist.",
                details={"posted_id": command.posted.engine_id},
            )
        if original.state != "posted":
            raise InvalidJournalError(
                f"Journal {command.posted.engine_id} is not posted.",
                details={"posted_id": command.posted.engine_id},
            )
        if self.reverse_failure is not None:
            failure = self.reverse_failure
            self.reverse_failure = None
            if failure.creates_draft:
                draft_id = self._create_reversal(original, command)
                if failure.draft_already_posted:
                    self._moves[draft_id].state = "posted"
                    self._moves[draft_id].number = f"M-{draft_id}"
                failure.error.details["reversal_draft_id"] = draft_id
                failure.error.details["original_posted_id"] = command.posted.engine_id
            raise failure.error
        reversal_id = self._create_reversal(original, command)
        self._moves[reversal_id].state = "posted"
        self._moves[reversal_id].number = f"M-{reversal_id}"
        return PostedJournalRef(reversal_id)

    def _create_reversal(self, original: _Move, command: ReverseJournal) -> int:
        swapped = tuple(
            JournalDraftLine(
                account=line.account,
                debit=line.credit,
                credit=line.debit,
                description=line.description,
            )
            for line in original.view.lines
        )
        return self._store(
            original.view.company,
            original.view.date,
            swapped,
            command.description or original.view.description,
            origin=PostedJournalRef(original.view.ref.engine_id),
        )

    def get_journal(self, posted: PostedJournalRef) -> PostedJournalView | None:
        move = self._moves.get(posted.engine_id)
        if move is None:
            return None
        if move.state != "posted":
            raise InvalidJournalError(
                f"Journal {posted.engine_id} is not posted.",
                details={"posted_id": posted.engine_id},
            )
        return self._posted_view(posted.engine_id)

    def list_fiscal_years(self, company: CompanyRef) -> Sequence[FiscalYearView]:
        return []

    def list_periods(
        self, company: CompanyRef, fiscal_year: FiscalYearRef | None = None
    ) -> Sequence[FiscalPeriodView]:
        return []

    def get_trial_balance(
        self,
        company: CompanyRef,
        date_from: FiscalDate,
        date_to: FiscalDate,
    ) -> TrialBalanceView:
        raise NotImplementedError("trial balance is not used here")

    def create_invoice_draft(self, command: CreateInvoiceDraft) -> InvoiceRef:
        raise NotImplementedError("journal service never creates invoices")

    def validate_invoice_draft(self, invoice: InvoiceRef) -> ValidationSummary:
        raise NotImplementedError("journal service never validates invoices")

    def get_invoice(self, invoice: InvoiceRef) -> InvoiceView | None:
        raise NotImplementedError("journal service never reads invoices")

    def cancel_invoice(self, invoice: InvoiceRef) -> InvoiceView:
        raise NotImplementedError("journal service never cancels invoices")

    def list_invoices(
        self,
        company: CompanyRef,
        invoice_type: str | None = None,
        state: str | None = None,
    ) -> Sequence[InvoiceView]:
        raise NotImplementedError("journal service never lists invoices")

    def get_payment(self, payment: PaymentRef) -> PaymentView | None:
        raise NotImplementedError("journal service never reads payments")

    def list_payments(
        self,
        company: CompanyRef,
        kind: str | None = None,
        state: str | None = None,
    ) -> Sequence[PaymentView]:
        raise NotImplementedError("journal service never lists payments")

    def get_bank_statement(self, statement: BankStatementRef) -> BankStatementView | None:
        raise NotImplementedError("journal service never reads statements")

    def create_bank_statement(self, command: CreateBankStatement) -> BankStatementRef:
        raise NotImplementedError("this fake never creates statements")

    def update_bank_statement_line(self, command: UpdateBankStatementLine) -> None:
        raise NotImplementedError("this fake never reconciles statements")

    def list_statement_journals(self, company: CompanyRef) -> Sequence[StatementJournalView]:
        return []

    def list_bank_statements(
        self, company: CompanyRef, state: str | None = None
    ) -> Sequence[BankStatementView]:
        raise NotImplementedError("journal service never lists statements")

    def create_credit_note(self, command: CreateCreditNote) -> InvoiceRef:
        raise NotImplementedError

    def create_sale(self, command: CreateSale) -> SaleRef:
        raise NotImplementedError

    def get_sale(self, sale: SaleRef) -> SaleView | None:
        raise NotImplementedError

    def list_sales(
        self,
        company: CompanyRef,
        state: str | None = None,
    ) -> Sequence[SaleView]:
        raise NotImplementedError

    def quote_sale(self, sale: SaleRef) -> SaleView:
        raise NotImplementedError

    def confirm_sale(self, sale: SaleRef) -> SaleView:
        raise NotImplementedError

    def process_sale(self, sale: SaleRef) -> SaleView:
        raise NotImplementedError

    def cancel_sale(self, sale: SaleRef) -> SaleView:
        raise NotImplementedError

    def create_payment(self, command: CreatePayment) -> PaymentRef:
        raise NotImplementedError

    def submit_payment(self, payment: PaymentRef) -> PaymentView:
        raise NotImplementedError

    def approve_payment(self, payment: PaymentRef) -> PaymentView:
        raise NotImplementedError

    def process_payment(self, payment: PaymentRef) -> PaymentView:
        raise NotImplementedError

    def succeed_payment(self, command: SucceedPayment) -> PaymentView:
        raise NotImplementedError

    def fail_payment(self, payment: PaymentRef) -> PaymentView:
        raise NotImplementedError

    def process_payments(self, payments: Sequence[PaymentRef]) -> Sequence[PaymentView]:
        raise NotImplementedError

    def get_payment_journal(
        self, journal: PaymentJournalRef
    ) -> PaymentJournalView | None:
        raise NotImplementedError

    def list_move_lines(
        self,
        company: CompanyRef,
        *,
        move: JournalDraftRef | None = None,
        account: AccountRef | None = None,
        party: PartyRef | None = None,
        maturity_date: FiscalDate | None = None,
        unreconciled_only: bool = False,
    ) -> Sequence[MoveLineView]:
        raise NotImplementedError

    def reconcile_move_lines(self, command: ReconcileMoveLines) -> ReconciliationView:
        raise NotImplementedError

    def validate_invoice(self, invoice: InvoiceRef) -> InvoiceView:
        raise NotImplementedError

    def post_invoice(self, invoice: InvoiceRef) -> InvoiceView:
        raise NotImplementedError

    # -- packaging & shipping (US-515): stubs, never exercised here --

    def list_stock_locations(self, company: CompanyRef) -> Sequence[StockLocationView]:
        raise NotImplementedError

    def get_stock_location(self, location: StockLocationRef) -> StockLocationView | None:
        raise NotImplementedError

    def create_packaging_product(self, command: CreatePackagingProduct) -> ProductRef:
        raise NotImplementedError

    def get_packaging_product(
        self,
        product: ProductRef,
        company: CompanyRef,
        location: StockLocationRef | None = None,
    ) -> PackagingProductView | None:
        raise NotImplementedError

    def list_packaging_products(
        self,
        company: CompanyRef,
        products: Sequence[ProductRef],
        location: StockLocationRef | None = None,
    ) -> Sequence[PackagingProductView]:
        raise NotImplementedError

    def get_product_quantity(
        self, product: ProductRef, location: StockLocationRef
    ) -> Decimal:
        raise NotImplementedError

    def get_product_cost_price(
        self, product: ProductRef, company: CompanyRef
    ) -> Decimal | None:
        raise NotImplementedError

    def upsert_order_point(self, command: UpsertOrderPoint) -> OrderPointView:
        raise NotImplementedError

    def get_order_point(
        self, product: ProductRef, warehouse: StockLocationRef, company: CompanyRef
    ) -> OrderPointView | None:
        raise NotImplementedError

    def list_order_points(self, company: CompanyRef) -> Sequence[OrderPointView]:
        raise NotImplementedError

    def create_consumption_move(self, command: CreateConsumptionMove) -> StockMoveView:
        raise NotImplementedError

    def list_consumption_moves(
        self,
        company: CompanyRef,
        from_location: StockLocationRef,
        to_location: StockLocationRef,
        date_from: FiscalDate,
        date_to: FiscalDate,
        product: ProductRef | None = None,
    ) -> Sequence[StockMoveView]:
        raise NotImplementedError

    def list_purchase_requests(
        self,
        company: CompanyRef,
        state: str | None = None,
        after_id: int | None = None,
    ) -> Sequence[PurchaseRequestView]:
        raise NotImplementedError

    def create_analytic_account(self, command: CreateAnalyticAccount) -> AnalyticAccountRef:
        raise NotImplementedError

    def get_analytic_account(
        self, account: AnalyticAccountRef
    ) -> AnalyticAccountView | None:
        raise NotImplementedError

    def list_analytic_lines(
        self, account: AnalyticAccountRef
    ) -> Sequence[AnalyticLineView]:
        raise NotImplementedError

    def create_inventory(self, command: CreateInventory) -> InventoryRef:
        raise NotImplementedError

    def get_inventory(self, inventory: InventoryRef) -> InventoryView | None:
        raise NotImplementedError

    def complete_inventory_lines(self, inventory: InventoryRef) -> InventoryView:
        raise NotImplementedError

    def update_inventory_line(self, command: UpdateInventoryLine) -> InventoryView:
        raise NotImplementedError

    def confirm_inventory(self, inventory: InventoryRef) -> InventoryView:
        raise NotImplementedError

    def cancel_inventory(self, inventory: InventoryRef) -> InventoryView:
        raise NotImplementedError

    def get_sale_shipment_costs(self, sale: SaleRef) -> Sequence[SaleShipmentCostView]:
        raise NotImplementedError

    def get_out_shipment_cost(self, shipment: OutShipmentRef) -> OutShipmentCostView | None:
        raise NotImplementedError

    # -- cheques (US-531): stubs, never exercised here --

    def create_engine_cheque(self, command: CreateEngineCheque) -> ChequeEngineView:
        raise NotImplementedError

    def get_engine_cheque(self, cheque: ChequeEngineRef) -> ChequeEngineView | None:
        raise NotImplementedError

    def list_engine_cheques(
        self, company: CompanyRef, *, state: str | None = None
    ) -> Sequence[ChequeEngineView]:
        raise NotImplementedError

    def transition_engine_cheque(self, command: TransitionEngineCheque) -> ChequeEngineView:
        raise NotImplementedError

    # -- cheques (US-532): stubs, never exercised here --

    def list_engine_checkbooks(self, company: CompanyRef) -> Sequence[CheckbookEngineView]:
        raise NotImplementedError

    def get_engine_checkbook(
        self, company: CompanyRef, checkbook: CheckbookEngineRef
    ) -> CheckbookEngineView | None:
        raise NotImplementedError


@pytest.fixture()
def engine() -> FakeJournalEngine:
    return FakeJournalEngine()


@pytest.fixture()
def service(engine: FakeJournalEngine) -> JournalDraftService:
    return JournalDraftService(engine)


def _posted(service: JournalDraftService) -> PostedJournalRef:
    draft = service.create_draft(COMPANY, MOVE_DATE, _lines(), description="sale")
    return service.post_draft(draft)


# ---------------------------------------------------------------------------
# Create / get
# ---------------------------------------------------------------------------


def test_create_draft_returns_ref_and_reads_back(
    service: JournalDraftService,
) -> None:
    ref = service.create_draft(
        COMPANY, MOVE_DATE, _lines(), description="office supplies"
    )
    view = service.get_draft(ref)
    assert view.company == COMPANY
    assert view.date == MOVE_DATE
    assert view.description == "office supplies"
    assert view.state == "draft"
    assert view.total_debit == view.total_credit == MoneyIRR(1_000_000)
    assert view.is_balanced


def test_create_draft_unknown_company_raises_company_not_found(
    service: JournalDraftService,
) -> None:
    with pytest.raises(HesabYarError) as excinfo:
        service.create_draft(OTHER_COMPANY, MOVE_DATE, _lines())
    assert excinfo.value.code == "COMPANY_NOT_FOUND"


def test_create_draft_unbalanced_raises_before_engine_call(
    service: JournalDraftService, engine: FakeJournalEngine
) -> None:
    lines = (
        JournalDraftLine(account=DEBIT_ACCOUNT, debit=MoneyIRR(100)),
        JournalDraftLine(account=CREDIT_ACCOUNT, credit=MoneyIRR(200)),
    )
    with pytest.raises(UnbalancedJournalError):
        service.create_draft(COMPANY, MOVE_DATE, lines)
    assert engine.create_calls == 0


def test_get_draft_missing_raises_typed_not_found(
    service: JournalDraftService,
) -> None:
    with pytest.raises(JournalDraftNotFoundError) as excinfo:
        service.get_draft(JournalDraftRef(999))
    assert excinfo.value.code == "LEDGER_JOURNAL_NOT_FOUND"


def test_get_draft_after_posting_reads_back_posted_state(
    service: JournalDraftService,
) -> None:
    draft = service.create_draft(COMPANY, MOVE_DATE, _lines())
    service.post_draft(draft)
    assert service.get_draft(draft).state == "posted"


# ---------------------------------------------------------------------------
# List / update
# ---------------------------------------------------------------------------


def test_list_drafts_returns_only_the_companys_drafts_newest_first() -> None:
    engine = FakeJournalEngine(
        companies=[
            CompanyView(ref=COMPANY, name="Acme", currency_code="IRR"),
            CompanyView(ref=OTHER_COMPANY, name="Beta", currency_code="IRR"),
        ]
    )
    service = JournalDraftService(engine)
    first = service.create_draft(COMPANY, MOVE_DATE, _lines(), description="one")
    service.create_draft(OTHER_COMPANY, MOVE_DATE, _lines(), description="other")
    second = service.create_draft(COMPANY, MOVE_DATE, _lines(), description="two")
    posted = service.create_draft(COMPANY, MOVE_DATE, _lines(), description="posted")
    service.post_draft(posted)
    views = service.list_drafts(COMPANY)
    assert [view.ref for view in views] == [second, first]
    assert all(view.state == "draft" for view in views)


def test_list_drafts_empty_when_no_drafts(service: JournalDraftService) -> None:
    assert service.list_drafts(COMPANY) == []


def test_list_drafts_unknown_company_raises_company_not_found(
    service: JournalDraftService,
) -> None:
    with pytest.raises(HesabYarError) as excinfo:
        service.list_drafts(OTHER_COMPANY)
    assert excinfo.value.code == "COMPANY_NOT_FOUND"


def test_update_draft_description_and_date(service: JournalDraftService) -> None:
    draft = service.create_draft(COMPANY, MOVE_DATE, _lines(), description="old")
    new_date = FiscalDate.from_gregorian(datetime.date(2026, 11, 17))
    updated = service.update_draft(draft, date=new_date, description="new")
    assert updated.ref == draft
    assert updated.description == "new"
    assert updated.date == new_date
    assert updated.total_debit == MoneyIRR(1_000_000)  # lines untouched
    assert service.get_draft(draft).description == "new"


def test_update_draft_replaces_lines_wholesale(service: JournalDraftService) -> None:
    draft = service.create_draft(COMPANY, MOVE_DATE, _lines())
    updated = service.update_draft(draft, lines=_lines(amount=2_000_000))
    assert updated.total_debit == updated.total_credit == MoneyIRR(2_000_000)
    assert updated.is_balanced
    assert updated.description == ""  # description untouched


def test_update_draft_empty_raises_invalid(service: JournalDraftService) -> None:
    draft = service.create_draft(COMPANY, MOVE_DATE, _lines())
    with pytest.raises(InvalidJournalError) as excinfo:
        service.update_draft(draft)
    assert excinfo.value.code == "LEDGER_INVALID_JOURNAL"


def test_update_draft_unbalanced_raises_before_engine_call(
    service: JournalDraftService,
) -> None:
    draft = service.create_draft(COMPANY, MOVE_DATE, _lines())
    lines = (
        JournalDraftLine(account=DEBIT_ACCOUNT, debit=MoneyIRR(100)),
        JournalDraftLine(account=CREDIT_ACCOUNT, credit=MoneyIRR(200)),
    )
    with pytest.raises(UnbalancedJournalError):
        service.update_draft(draft, lines=lines)
    # The draft is untouched: the command failed at construction.
    assert service.get_draft(draft).total_debit == MoneyIRR(1_000_000)


def test_update_draft_missing_raises_not_found(service: JournalDraftService) -> None:
    with pytest.raises(JournalDraftNotFoundError) as excinfo:
        service.update_draft(JournalDraftRef(999), description="new")
    assert excinfo.value.code == "LEDGER_JOURNAL_NOT_FOUND"


def test_update_draft_posted_raises_immutable(service: JournalDraftService) -> None:
    posted = _posted(service)
    with pytest.raises(JournalAlreadyPostedError) as excinfo:
        service.update_draft(JournalDraftRef(posted.engine_id), description="new")
    assert excinfo.value.code == "LEDGER_POSTED_IMMUTABLE"


# ---------------------------------------------------------------------------
# Validate / post
# ---------------------------------------------------------------------------


def test_validate_draft_ok_and_failed(
    service: JournalDraftService, engine: FakeJournalEngine
) -> None:
    draft = service.create_draft(COMPANY, MOVE_DATE, _lines())
    assert service.validate_draft(draft).valid is True
    engine.invalid_drafts.add(draft.engine_id)
    summary = service.validate_draft(draft)
    assert summary.valid is False
    assert summary.codes() == ("LEDGER_UNBALANCED",)


def test_post_draft_returns_posted_ref_and_journal_reads_back(
    service: JournalDraftService,
) -> None:
    draft = service.create_draft(COMPANY, MOVE_DATE, _lines())
    posted = service.post_draft(draft)
    assert posted.engine_id == draft.engine_id
    view = service.get_posted_journal(posted)
    assert view.number == f"M-{draft.engine_id}"
    assert view.post_date == MOVE_DATE
    assert view.is_balanced


def test_post_missing_draft_raises_not_found(service: JournalDraftService) -> None:
    with pytest.raises(JournalDraftNotFoundError):
        service.post_draft(JournalDraftRef(999))


def test_post_already_posted_raises_immutable(service: JournalDraftService) -> None:
    posted = _posted(service)
    with pytest.raises(JournalAlreadyPostedError) as excinfo:
        service.post_draft(JournalDraftRef(posted.engine_id))
    assert excinfo.value.code == "LEDGER_POSTED_IMMUTABLE"


def test_get_posted_journal_missing_raises_not_found(
    service: JournalDraftService,
) -> None:
    with pytest.raises(PostedJournalNotFoundError) as excinfo:
        service.get_posted_journal(PostedJournalRef(999))
    assert excinfo.value.code == "LEDGER_JOURNAL_NOT_FOUND"


def test_get_posted_journal_of_draft_raises_invalid(
    service: JournalDraftService,
) -> None:
    draft = service.create_draft(COMPANY, MOVE_DATE, _lines())
    with pytest.raises(InvalidJournalError):
        service.get_posted_journal(PostedJournalRef(draft.engine_id))


# ---------------------------------------------------------------------------
# Reverse (+ the non-atomic reversal recovery)
# ---------------------------------------------------------------------------


def test_reverse_returns_linked_reversal_with_swapped_lines(
    service: JournalDraftService,
) -> None:
    posted = _posted(service)
    reversal = service.reverse_journal(posted)
    view = service.get_posted_journal(reversal)
    assert view.origin == posted
    assert view.is_reversal
    original = service.get_posted_journal(posted)
    assert [line.debit for line in view.lines] == [
        line.credit for line in original.lines
    ]
    assert [line.credit for line in view.lines] == [
        line.debit for line in original.lines
    ]


def test_reverse_missing_raises_not_found(service: JournalDraftService) -> None:
    with pytest.raises(PostedJournalNotFoundError):
        service.reverse_journal(PostedJournalRef(999))


def test_reverse_draft_raises_invalid(service: JournalDraftService) -> None:
    draft = service.create_draft(COMPANY, MOVE_DATE, _lines())
    with pytest.raises(InvalidJournalError):
        service.reverse_journal(PostedJournalRef(draft.engine_id))


def test_reverse_failure_without_draft_id_reraises_unchanged(
    service: JournalDraftService, engine: FakeJournalEngine
) -> None:
    posted = _posted(service)
    engine.reverse_failure = ReverseFailure(
        error=EngineError("boom", details={}), creates_draft=False
    )
    with pytest.raises(EngineError):
        service.reverse_journal(posted)
    assert engine.post_calls == 1  # no retry without a surviving draft


def test_reverse_retries_post_of_surviving_draft(
    service: JournalDraftService, engine: FakeJournalEngine
) -> None:
    posted = _posted(service)
    engine.reverse_failure = ReverseFailure(
        error=EngineError("post timed out", details={}), creates_draft=True
    )
    reversal = service.reverse_journal(posted)
    view = service.get_posted_journal(reversal)
    assert view.origin == posted
    assert engine.reverse_calls == 1
    assert engine.post_calls == 2  # original post + the recovery retry


def test_reverse_recovers_when_first_post_actually_landed(
    service: JournalDraftService, engine: FakeJournalEngine
) -> None:
    posted = _posted(service)
    engine.reverse_failure = ReverseFailure(
        error=EngineError("response lost", details={}),
        creates_draft=True,
        draft_already_posted=True,
    )
    reversal = service.reverse_journal(posted)
    assert service.get_posted_journal(reversal).origin == posted


def test_reverse_retry_failure_reraises_original_with_draft_id(
    service: JournalDraftService, engine: FakeJournalEngine
) -> None:
    posted = _posted(service)
    engine.reverse_failure = ReverseFailure(
        error=EngineError("post timed out", details={}), creates_draft=True
    )
    engine.fail_post_once = EngineError("still failing", details={})
    with pytest.raises(EngineError) as excinfo:
        service.reverse_journal(posted)
    assert excinfo.value.message == "post timed out"
    assert isinstance(excinfo.value.details.get("reversal_draft_id"), int)
    # The orphaned reversal draft survives for recovery: it is still a draft.
    draft_id = excinfo.value.details["reversal_draft_id"]
    assert service.get_draft(JournalDraftRef(draft_id)).state == "draft"


# ---------------------------------------------------------------------------
# Engine failure translation
# ---------------------------------------------------------------------------


def test_engine_failure_translates_to_application_error(
    service: JournalDraftService, engine: FakeJournalEngine
) -> None:
    engine.failure = EngineError("engine down", details={})
    with pytest.raises(EngineError) as excinfo:
        service.create_draft(COMPANY, MOVE_DATE, _lines())
    assert excinfo.value.code == "ENGINE_ERROR"
    assert excinfo.value.retryable is False
