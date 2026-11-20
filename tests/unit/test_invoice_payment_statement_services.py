"""Unit tests for the invoice/payment/statement services (US-205).

The engine is faked in memory (``FakeDocumentEngine``); no server and
no database are needed. The fake mirrors the adapter's filter
validation (bad type/state/kind → the matching ``LEDGER_INVALID_*``
error) so the services' pass-through behaviour is tested against the
real contract.
"""

from __future__ import annotations

import dataclasses
import datetime
from collections.abc import Sequence
from decimal import Decimal

import pytest

from hesabyar.application.commands.invoices import InvoiceDraftService
from hesabyar.application.errors import CompanyNotFoundError, EngineUnavailableError
from hesabyar.application.queries.payments import PaymentQueryService
from hesabyar.application.queries.statements import BankStatementQueryService
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
    InvoiceDraftLine,
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
    BankStatementLineView,
    BankStatementView,
    CheckbookEngineView,
    ChequeEngineView,
    CompanyView,
    FiscalPeriodView,
    FiscalYearView,
    InventoryView,
    InvoiceLineView,
    InvoiceTotalView,
    InvoiceView,
    JournalDraftView,
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
    BankStatementNotFoundError,
    HesabYarError,
    InvalidBankStatementError,
    InvalidInvoiceError,
    InvalidPaymentError,
    InvoiceNotFoundError,
    PaymentNotFoundError,
)
from hesabyar.domain.common.money import MoneyIRR

COMPANY = CompanyRef(101)
OTHER_COMPANY = CompanyRef(102)
UNKNOWN_COMPANY = CompanyRef(999)
PARTY = PartyRef(201)
JOURNAL = JournalRef(7)
RECEIVABLE = AccountRef(301)
REVENUE = AccountRef(401)
SERVICES = AccountRef(402)
MOVE_DATE = FiscalDate.from_gregorian(datetime.date(2026, 11, 17))
MISSING_ID = 999_999_999


def _invoice_lines(amounts: Sequence[int] = (500_000, 250_000)) -> list[InvoiceDraftLine]:
    accounts = (REVENUE, SERVICES)
    return [
        InvoiceDraftLine(
            account=accounts[i % len(accounts)],
            unit_price=MoneyIRR(amount),
            quantity=Decimal(1),
            description=f"line {i}",
        )
        for i, amount in enumerate(amounts)
    ]


def _invoice_view(
    invoice_id: int,
    company: CompanyRef = COMPANY,
    invoice_type: str = "out",
    state: str = "draft",
    total: int = 750_000,
) -> InvoiceView:
    return InvoiceView(
        ref=InvoiceRef(invoice_id),
        company=company,
        invoice_type=invoice_type,
        party=PARTY,
        party_name="Acme Customer",
        number=None,
        invoice_date=MOVE_DATE,
        accounting_date=None,
        state=state,
        description="test invoice",
        reference="",
        journal=JOURNAL,
        account=RECEIVABLE,
        currency_code="IRR",
        untaxed_amount=MoneyIRR(total),
        tax_amount=MoneyIRR(0),
        total_amount=MoneyIRR(total),
        lines=(
            InvoiceLineView(
                account=REVENUE,
                quantity=Decimal(1),
                unit_price=MoneyIRR(total),
                amount=MoneyIRR(total),
            ),
        ),
    )


def _payment_view(
    payment_id: int,
    company: CompanyRef = COMPANY,
    kind: str = "receivable",
    state: str = "draft",
) -> PaymentView:
    return PaymentView(
        ref=PaymentRef(payment_id),
        company=company,
        number=f"PAY-{payment_id}",
        kind=kind,
        party=PARTY,
        party_name="Acme Customer",
        date=MOVE_DATE,
        amount=MoneyIRR(500_000),
        state=state,
        reference="",
        journal_name="Manual Payments",
        currency_code="IRR",
    )


def _statement_view(
    statement_id: int,
    company: CompanyRef = COMPANY,
    state: str = "draft",
    matched: bool = False,
) -> BankStatementView:
    return BankStatementView(
        ref=BankStatementRef(statement_id),
        company=company,
        name=f"Statement {statement_id}",
        journal_name="Bank",
        journal_id=700,
        date=MOVE_DATE,
        state=state,
        validation="balance",
        currency_code="IRR",
        start_balance=MoneyIRR(0),
        end_balance=MoneyIRR(750_000),
        balance=MoneyIRR(750_000),
        total_amount=None,
        number_of_lines=None,
        lines=(
            BankStatementLineView(
                id=9001,
                date=MOVE_DATE,
                amount=MoneyIRR(1_000_000),
                account=RECEIVABLE,
                description="receipt",
                related_invoice=InvoiceRef(1) if matched else None,
            ),
            BankStatementLineView(
                id=9002,
                date=MOVE_DATE,
                amount=MoneyIRR(-250_000),
                account=AccountRef(501),
                description="payment",
            ),
        ),
    )


class FakeDocumentEngine:
    """In-memory invoice/payment/statement engine for unit tests."""

    def __init__(self) -> None:
        self._companies = [
            CompanyView(ref=COMPANY, name="Acme", currency_code="IRR"),
            CompanyView(ref=OTHER_COMPANY, name="Beta", currency_code="IRR"),
        ]
        self._invoices: dict[int, InvoiceView] = {}
        self._payments: dict[int, PaymentView] = {}
        self._statements: dict[int, BankStatementView] = {}
        self._next_id = 1
        self.failure: HesabYarError | None = None
        self.invalid_invoices: set[int] = set()

    # -- seeding -----------------------------------------------------------

    def seed_invoice(self, **kwargs) -> int:
        invoice_id = self._next_id
        self._next_id += 1
        self._invoices[invoice_id] = _invoice_view(invoice_id, **kwargs)
        return invoice_id

    def seed_payment(self, **kwargs) -> int:
        payment_id = self._next_id
        self._next_id += 1
        self._payments[payment_id] = _payment_view(payment_id, **kwargs)
        return payment_id

    def seed_statement(self, **kwargs) -> int:
        statement_id = self._next_id
        self._next_id += 1
        self._statements[statement_id] = _statement_view(statement_id, **kwargs)
        return statement_id

    # -- port: companies / journals (stubs) --------------------------------

    def list_companies(self) -> Sequence[CompanyView]:
        if self.failure is not None:
            raise self.failure
        return list(self._companies)

    def get_chart_of_accounts(self, company: CompanyRef) -> Sequence[AccountView]:
        return []

    def create_journal_draft(self, command: CreateJournalDraft) -> JournalDraftRef:
        raise NotImplementedError("document services never create journals")

    def get_journal_draft(self, draft: JournalDraftRef) -> JournalDraftView | None:
        raise NotImplementedError("document services never read journal drafts")

    def list_journal_drafts(self, company: CompanyRef) -> Sequence[JournalDraftView]:
        raise NotImplementedError("document services never list journal drafts")

    def update_journal_draft(self, command: UpdateJournalDraft) -> JournalDraftView:
        raise NotImplementedError("document services never update journal drafts")

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
        raise NotImplementedError("document services never validate journals")

    def post_journal(self, command: PostJournal) -> PostedJournalRef:
        raise NotImplementedError("document services never post journals")

    def reverse_journal(self, command: ReverseJournal) -> PostedJournalRef:
        raise NotImplementedError("document services never reverse journals")

    def get_journal(self, posted: PostedJournalRef) -> PostedJournalView | None:
        raise NotImplementedError("document services never read journals")

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

    # -- port: invoices ----------------------------------------------------

    def create_invoice_draft(self, command: CreateInvoiceDraft) -> InvoiceRef:
        if self.failure is not None:
            raise self.failure
        invoice_id = self._next_id
        self._next_id += 1
        total = sum(line.amount.amount for line in command.lines)
        self._invoices[invoice_id] = InvoiceView(
            ref=InvoiceRef(invoice_id),
            company=command.company,
            invoice_type=command.invoice_type,
            party=command.party,
            party_name="Acme Customer",
            number=None,
            invoice_date=command.date,
            accounting_date=None,
            state="draft",
            description=command.description,
            reference=command.reference,
            journal=command.journal or JOURNAL,
            account=command.account or RECEIVABLE,
            currency_code="IRR",
            untaxed_amount=MoneyIRR(total),
            tax_amount=MoneyIRR(0),
            total_amount=MoneyIRR(total),
            lines=tuple(
                InvoiceLineView(
                    account=line.account,
                    quantity=line.quantity,
                    unit_price=line.unit_price,
                    amount=line.amount,
                    description=line.description,
                )
                for line in command.lines
            ),
        )
        return InvoiceRef(invoice_id)

    def validate_invoice_draft(self, invoice: InvoiceRef) -> ValidationSummary:
        if invoice.engine_id not in self._invoices:
            raise InvoiceNotFoundError(
                f"Invoice {invoice.engine_id} does not exist.",
                details={"invoice_id": invoice.engine_id},
            )
        if invoice.engine_id in self.invalid_invoices:
            return ValidationSummary.failed(
                [
                    ValidationIssue(
                        code="LEDGER_INVALID_INVOICE",
                        message="Invoice has no priced lines.",
                    )
                ]
            )
        return ValidationSummary.ok()

    def get_invoice(self, invoice: InvoiceRef) -> InvoiceView | None:
        return self._invoices.get(invoice.engine_id)

    def cancel_invoice(self, invoice: InvoiceRef) -> InvoiceView:
        current = self._invoices.get(invoice.engine_id)
        if current is None:
            raise InvoiceNotFoundError(
                f"Invoice {invoice.engine_id} does not exist.",
                details={"invoice_id": invoice.engine_id},
            )
        cancelled = dataclasses.replace(current, state="cancelled")
        self._invoices[invoice.engine_id] = cancelled
        return cancelled

    def list_invoices(
        self,
        company: CompanyRef,
        invoice_type: str | None = None,
        state: str | None = None,
    ) -> Sequence[InvoiceView]:
        if invoice_type is not None and invoice_type not in ("out", "in"):
            raise InvalidInvoiceError(
                f"Invoice type filter must be 'out' or 'in', got {invoice_type!r}."
            )
        if state is not None and state not in (
            "draft",
            "validated",
            "posted",
            "paid",
            "cancelled",
        ):
            raise InvalidInvoiceError(
                f"Invoice state filter must be a valid state, got {state!r}."
            )
        return [
            view
            for view in self._invoices.values()
            if view.company == company
            and (invoice_type is None or view.invoice_type == invoice_type)
            and (state is None or view.state == state)
        ]

    # -- port: payments ----------------------------------------------------

    def get_payment(self, payment: PaymentRef) -> PaymentView | None:
        return self._payments.get(payment.engine_id)

    def list_payments(
        self,
        company: CompanyRef,
        kind: str | None = None,
        state: str | None = None,
    ) -> Sequence[PaymentView]:
        if kind is not None and kind not in ("receivable", "payable"):
            raise InvalidPaymentError(
                f"Payment kind filter must be 'receivable' or 'payable', got {kind!r}."
            )
        if state is not None and state not in (
            "draft",
            "submitted",
            "approved",
            "processing",
            "succeeded",
            "failed",
        ):
            raise InvalidPaymentError(
                f"Payment state filter must be a valid state, got {state!r}."
            )
        return [
            view
            for view in self._payments.values()
            if view.company == company
            and (kind is None or view.kind == kind)
            and (state is None or view.state == state)
        ]

    # -- port: bank statements ----------------------------------------------

    def get_bank_statement(self, statement: BankStatementRef) -> BankStatementView | None:
        return self._statements.get(statement.engine_id)

    def create_bank_statement(self, command: CreateBankStatement) -> BankStatementRef:
        raise NotImplementedError("this fake never creates statements")

    def update_bank_statement_line(self, command: UpdateBankStatementLine) -> None:
        raise NotImplementedError("this fake never reconciles statements")

    def list_statement_journals(self, company: CompanyRef) -> Sequence[StatementJournalView]:
        return []

    def list_bank_statements(
        self, company: CompanyRef, state: str | None = None
    ) -> Sequence[BankStatementView]:
        if state is not None and state not in (
            "draft",
            "validated",
            "posted",
            "cancelled",
        ):
            raise InvalidBankStatementError(
                f"Statement state filter must be a valid state, got {state!r}."
            )
        return [
            view
            for view in self._statements.values()
            if view.company == company
            and (state is None or view.state == state)
        ]

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
def engine() -> FakeDocumentEngine:
    return FakeDocumentEngine()


@pytest.fixture()
def invoices(engine: FakeDocumentEngine) -> InvoiceDraftService:
    return InvoiceDraftService(engine)


@pytest.fixture()
def payments(engine: FakeDocumentEngine) -> PaymentQueryService:
    return PaymentQueryService(engine)


@pytest.fixture()
def statements(engine: FakeDocumentEngine) -> BankStatementQueryService:
    return BankStatementQueryService(engine)


# ---------------------------------------------------------------------------
# Invoices: create / get
# ---------------------------------------------------------------------------


def test_create_draft_returns_ref_and_reads_back(
    invoices: InvoiceDraftService,
) -> None:
    ref = invoices.create_draft(
        COMPANY, "out", PARTY, MOVE_DATE, _invoice_lines(), description="sale"
    )
    view = invoices.get_invoice(ref)
    assert view.state == "draft"
    assert view.invoice_type == "out"
    assert view.total_amount == MoneyIRR(750_000)
    assert view.untaxed_amount == MoneyIRR(750_000)
    assert view.tax_amount == MoneyIRR(0)
    assert len(view.lines) == 2
    assert view.number is None  # the engine assigns the number later


def test_create_draft_unknown_company_raises(
    invoices: InvoiceDraftService,
) -> None:
    with pytest.raises(CompanyNotFoundError, match="999"):
        invoices.create_draft(
            UNKNOWN_COMPANY, "out", PARTY, MOVE_DATE, _invoice_lines()
        )


def test_create_draft_invalid_type_raises(
    invoices: InvoiceDraftService,
) -> None:
    with pytest.raises(InvalidInvoiceError, match="out.*in"):
        invoices.create_draft(
            COMPANY, "sideways", PARTY, MOVE_DATE, _invoice_lines()
        )


def test_create_draft_zero_total_raises(invoices: InvoiceDraftService) -> None:
    with pytest.raises(InvalidInvoiceError, match="greater than zero"):
        invoices.create_draft(
            COMPANY,
            "out",
            PARTY,
            MOVE_DATE,
            [InvoiceDraftLine(account=REVENUE, unit_price=MoneyIRR(0))],
        )


def test_get_invoice_missing_raises(invoices: InvoiceDraftService) -> None:
    with pytest.raises(InvoiceNotFoundError) as exc_info:
        invoices.get_invoice(InvoiceRef(MISSING_ID))
    assert exc_info.value.code == "LEDGER_INVOICE_NOT_FOUND"


def test_engine_failure_translates_to_application_error(
    engine: FakeDocumentEngine, invoices: InvoiceDraftService
) -> None:
    engine.failure = HesabYarError("boom", code="ENGINE_UNAVAILABLE")
    with pytest.raises(EngineUnavailableError) as exc_info:
        invoices.create_draft(COMPANY, "out", PARTY, MOVE_DATE, _invoice_lines())
    assert exc_info.value.code == "ENGINE_UNAVAILABLE"
    assert exc_info.value.retryable is True


# ---------------------------------------------------------------------------
# Invoices: list / validate
# ---------------------------------------------------------------------------


def test_list_invoices_filters_by_type_and_state(
    engine: FakeDocumentEngine, invoices: InvoiceDraftService
) -> None:
    engine.seed_invoice(invoice_type="out", state="draft")
    engine.seed_invoice(invoice_type="in", state="draft")
    engine.seed_invoice(invoice_type="out", state="posted")
    engine.seed_invoice(invoice_type="out", state="draft", company=OTHER_COMPANY)

    assert len(invoices.list_invoices(COMPANY)) == 3
    assert len(invoices.list_invoices(COMPANY, invoice_type="out")) == 2
    assert len(invoices.list_invoices(COMPANY, state="draft")) == 2
    assert len(invoices.list_invoices(COMPANY, invoice_type="in", state="draft")) == 1
    assert len(invoices.list_invoices(OTHER_COMPANY)) == 1


def test_list_invoices_unknown_company_raises(
    invoices: InvoiceDraftService,
) -> None:
    with pytest.raises(CompanyNotFoundError):
        invoices.list_invoices(UNKNOWN_COMPANY)


def test_list_invoices_invalid_filter_raises(
    invoices: InvoiceDraftService,
) -> None:
    with pytest.raises(InvalidInvoiceError):
        invoices.list_invoices(COMPANY, invoice_type="sideways")
    with pytest.raises(InvalidInvoiceError):
        invoices.list_invoices(COMPANY, state="limbo")


def test_validate_invoice_draft_ok_and_failed(
    engine: FakeDocumentEngine, invoices: InvoiceDraftService
) -> None:
    good = engine.seed_invoice()
    bad = engine.seed_invoice()
    engine.invalid_invoices.add(bad)
    assert invoices.validate_invoice_draft(InvoiceRef(good)).valid is True
    summary = invoices.validate_invoice_draft(InvoiceRef(bad))
    assert summary.valid is False
    assert summary.codes() == ("LEDGER_INVALID_INVOICE",)


def test_validate_missing_invoice_raises(invoices: InvoiceDraftService) -> None:
    with pytest.raises(InvoiceNotFoundError):
        invoices.validate_invoice_draft(InvoiceRef(MISSING_ID))


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------


def test_list_payments_filters_by_kind_and_state(
    engine: FakeDocumentEngine, payments: PaymentQueryService
) -> None:
    engine.seed_payment(kind="receivable", state="draft")
    engine.seed_payment(kind="payable", state="draft")
    engine.seed_payment(kind="receivable", state="succeeded")
    engine.seed_payment(kind="receivable", state="draft", company=OTHER_COMPANY)

    assert len(payments.list_payments(COMPANY)) == 3
    assert len(payments.list_payments(COMPANY, kind="receivable")) == 2
    assert len(payments.list_payments(COMPANY, state="draft")) == 2
    assert len(payments.list_payments(COMPANY, kind="payable", state="draft")) == 1
    assert len(payments.list_payments(OTHER_COMPANY)) == 1


def test_list_payments_invalid_filter_raises(payments: PaymentQueryService) -> None:
    with pytest.raises(InvalidPaymentError):
        payments.list_payments(COMPANY, kind="sideways")
    with pytest.raises(InvalidPaymentError):
        payments.list_payments(COMPANY, state="limbo")


def test_get_payment_missing_raises(payments: PaymentQueryService) -> None:
    with pytest.raises(PaymentNotFoundError) as exc_info:
        payments.get_payment(PaymentRef(MISSING_ID))
    assert exc_info.value.code == "LEDGER_PAYMENT_NOT_FOUND"


def test_get_payment_reads_back(
    engine: FakeDocumentEngine, payments: PaymentQueryService
) -> None:
    payment_id = engine.seed_payment()
    view = payments.get_payment(PaymentRef(payment_id))
    assert view.number == f"PAY-{payment_id}"
    assert view.amount == MoneyIRR(500_000)
    assert view.is_draft is True


# ---------------------------------------------------------------------------
# Bank statements
# ---------------------------------------------------------------------------


def test_list_statements_filters_by_state(
    engine: FakeDocumentEngine, statements: BankStatementQueryService
) -> None:
    engine.seed_statement(state="draft")
    engine.seed_statement(state="posted")
    engine.seed_statement(state="draft", company=OTHER_COMPANY)

    assert len(statements.list_bank_statements(COMPANY)) == 2
    assert len(statements.list_bank_statements(COMPANY, state="draft")) == 1
    assert len(statements.list_bank_statements(OTHER_COMPANY)) == 1


def test_list_statements_invalid_filter_raises(
    statements: BankStatementQueryService,
) -> None:
    with pytest.raises(InvalidBankStatementError):
        statements.list_bank_statements(COMPANY, state="limbo")


def test_get_statement_missing_raises(
    statements: BankStatementQueryService,
) -> None:
    with pytest.raises(BankStatementNotFoundError) as exc_info:
        statements.get_bank_statement(BankStatementRef(MISSING_ID))
    assert exc_info.value.code == "LEDGER_STATEMENT_NOT_FOUND"


def test_import_status_counts_states_and_lines(
    engine: FakeDocumentEngine, statements: BankStatementQueryService
) -> None:
    engine.seed_statement(state="draft", matched=True)
    engine.seed_statement(state="posted", matched=False)
    engine.seed_statement(state="draft", company=OTHER_COMPANY)

    status = statements.get_import_status(COMPANY)
    assert status.total_statements == 2
    assert status.draft_count == 1
    assert status.posted_count == 1
    assert status.validated_count == 0
    assert status.cancelled_count == 0
    assert status.latest_statement is not None
    assert status.total_lines == 4  # two lines per statement
    assert status.matched_lines == 1  # only the first statement's receipt line
    assert status.unmatched_lines == 3


def test_import_status_empty_company_is_zeros(
    statements: BankStatementQueryService,
) -> None:
    status = statements.get_import_status(COMPANY)
    assert status.total_statements == 0
    assert status.latest_statement is None
    assert status.total_lines == 0
    assert status.matched_lines == 0
    assert status.unmatched_lines == 0
