"""Unit tests for the application query services (US-104).

No live server is needed: the engine port is faked in memory, and the
static guards enforce the dependency direction (spec §6.3: application
depends only on the domain) on every run.
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
from decimal import (
    Decimal,
)
from pathlib import Path

import pytest

from hesabyar.application.errors import (
    COMPANY_NOT_FOUND,
    ApplicationError,
    CompanyNotFoundError,
    EngineAuthenticationError,
    EngineError,
    EngineUnavailableError,
    EngineValidationError,
    translate_engine_error,
)
from hesabyar.application.queries.accounts import AccountQueryService
from hesabyar.application.queries.companies import CompanyQueryService
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
    BalanceSheetView,
    BankStatementView,
    CashFlowView,
    CheckbookEngineView,
    ChequeEngineView,
    CompanyView,
    FiscalPeriodView,
    FiscalYearView,
    GeneralLedgerView,
    IncomeStatementView,
    InventoryView,
    InvoiceTotalView,
    InvoiceView,
    JournalDraftView,
    MoveLineView,
    NotesBalanceView,
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
    ValidationSummary,
)
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.errors import AccountNotFoundError, HesabYarError

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Fake port
# ---------------------------------------------------------------------------


class FakePort:
    """In-memory AccountingEnginePort for unit tests (no server)."""

    def __init__(
        self,
        companies: Sequence[CompanyView] | None = None,
        charts: dict[CompanyRef, Sequence[AccountView]] | None = None,
        failure: HesabYarError | None = None,
    ) -> None:
        self._companies = list(companies or [])
        self._charts = charts or {}
        self._failure = failure

    def list_companies(self) -> Sequence[CompanyView]:
        if self._failure is not None:
            raise self._failure
        return list(self._companies)

    def get_chart_of_accounts(self, company: CompanyRef) -> Sequence[AccountView]:
        if self._failure is not None:
            raise self._failure
        # Like the real engine, an unknown company id yields an empty chart.
        return list(self._charts.get(company, []))

    def create_journal_draft(self, command: CreateJournalDraft) -> JournalDraftRef:
        raise NotImplementedError("query services never create drafts")

    def validate_journal_draft(self, draft: JournalDraftRef) -> ValidationSummary:
        raise NotImplementedError("query services never validate drafts")

    def get_journal_draft(self, draft: JournalDraftRef) -> JournalDraftView | None:
        raise NotImplementedError("query services never read drafts")

    def list_journal_drafts(self, company: CompanyRef) -> Sequence[JournalDraftView]:
        raise NotImplementedError("query services never list drafts")

    def update_journal_draft(self, command: UpdateJournalDraft) -> JournalDraftView:
        raise NotImplementedError("query services never update drafts")

    def post_journal(self, command: PostJournal) -> PostedJournalRef:
        raise NotImplementedError("query services never post journals")

    def reverse_journal(self, command: ReverseJournal) -> PostedJournalRef:
        raise NotImplementedError("query services never reverse journals")

    def get_journal(self, posted: PostedJournalRef) -> PostedJournalView | None:
        raise NotImplementedError("query services never read journals")

    def list_fiscal_years(self, company: CompanyRef) -> Sequence[FiscalYearView]:
        raise NotImplementedError("query services never list fiscal years")

    def list_periods(
        self, company: CompanyRef, fiscal_year: FiscalYearRef | None = None
    ) -> Sequence[FiscalPeriodView]:
        raise NotImplementedError("query services never list periods")

    def get_trial_balance(
        self,
        company: CompanyRef,
        date_from: FiscalDate,
        date_to: FiscalDate,
    ) -> TrialBalanceView:
        raise NotImplementedError("trial balance is not used here")

    def get_general_ledger(
        self,
        company: CompanyRef,
        date_from: FiscalDate,
        date_to: FiscalDate,
        account: AccountRef | None = None,
    ) -> GeneralLedgerView:
        raise NotImplementedError("general ledger is not used here")

    def get_balance_sheet(
        self,
        company: CompanyRef,
        as_of: FiscalDate,
        comparison_as_of: FiscalDate | None = None,
    ) -> BalanceSheetView:
        raise NotImplementedError("balance sheet is not used here")

    def get_income_statement(
        self,
        company: CompanyRef,
        date_from: FiscalDate,
        date_to: FiscalDate,
        comparison_from: FiscalDate | None = None,
        comparison_to: FiscalDate | None = None,
    ) -> IncomeStatementView:
        raise NotImplementedError("income statement is not used here")

    def get_cash_flow(
        self,
        company: CompanyRef,
        date_from: FiscalDate,
        date_to: FiscalDate,
    ) -> CashFlowView:
        raise NotImplementedError("cash-flow statement is not used here")

    def get_notes_balance(
        self,
        company: CompanyRef,
        as_of: FiscalDate,
        instrument: str = "both",
        party: PartyRef | None = None,
    ) -> NotesBalanceView:
        raise NotImplementedError("notes balance is not used here")

    def create_invoice_draft(self, command: CreateInvoiceDraft) -> InvoiceRef:
        raise NotImplementedError("query services never create invoices")

    def validate_invoice_draft(self, invoice: InvoiceRef) -> ValidationSummary:
        raise NotImplementedError("query services never validate invoices")

    def get_invoice(self, invoice: InvoiceRef) -> InvoiceView | None:
        raise NotImplementedError("query services never read invoices")

    def cancel_invoice(self, invoice: InvoiceRef) -> InvoiceView:
        raise NotImplementedError("query services never cancel invoices")

    def list_invoices(
        self,
        company: CompanyRef,
        invoice_type: str | None = None,
        state: str | None = None,
    ) -> Sequence[InvoiceView]:
        raise NotImplementedError("query services never list invoices")

    def get_payment(self, payment: PaymentRef) -> PaymentView | None:
        raise NotImplementedError("query services never read payments")

    def list_payments(
        self,
        company: CompanyRef,
        kind: str | None = None,
        state: str | None = None,
    ) -> Sequence[PaymentView]:
        raise NotImplementedError("query services never list payments")

    def get_bank_statement(self, statement: BankStatementRef) -> BankStatementView | None:
        raise NotImplementedError("query services never read statements")

    def create_bank_statement(self, command: CreateBankStatement) -> BankStatementRef:
        raise NotImplementedError("this fake never creates statements")

    def update_bank_statement_line(self, command: UpdateBankStatementLine) -> None:
        raise NotImplementedError("this fake never reconciles statements")

    def list_statement_journals(self, company: CompanyRef) -> Sequence[StatementJournalView]:
        return []

    def list_bank_statements(
        self, company: CompanyRef, state: str | None = None
    ) -> Sequence[BankStatementView]:
        raise NotImplementedError("query services never list statements")

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

    # -- cash box (US-516): stubs, never exercised here --

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
        raise NotImplementedError("query services never delete drafts")

    def get_account_balance(
        self, company: CompanyRef, account: AccountRef, as_of: FiscalDate
    ) -> AccountBalanceView:
        raise NotImplementedError("query services never read account balances here")


def _company(company_id: int, name: str = "Acme", currency: str = "IRR") -> CompanyView:
    return CompanyView(ref=CompanyRef(company_id), name=name, currency_code=currency)


def _account(
    account_id: int,
    company_id: int,
    code: str,
    name: str = "Cash",
) -> AccountView:
    return AccountView(
        ref=AccountRef(account_id),
        company=CompanyRef(company_id),
        code=code,
        name=name,
        type_name="Asset",
        parent=None,
        closed=False,
        party_required=False,
        active=True,
    )


# ---------------------------------------------------------------------------
# list_companies / get_company
# ---------------------------------------------------------------------------


def test_list_companies_returns_engine_neutral_views():
    service = CompanyQueryService(FakePort(companies=[_company(1, "Acme"), _company(2, "Beta")]))
    companies = service.list_companies()
    assert companies == [_company(1, "Acme"), _company(2, "Beta")]
    assert all(isinstance(view, CompanyView) for view in companies)


def test_list_companies_empty_when_engine_has_none():
    service = CompanyQueryService(FakePort(companies=[]))
    assert service.list_companies() == []


def test_get_company_returns_matching_view():
    service = CompanyQueryService(FakePort(companies=[_company(1, "Acme"), _company(2, "Beta")]))
    assert service.get_company(CompanyRef(2)) == _company(2, "Beta")


def test_get_company_missing_raises_typed_not_found():
    service = CompanyQueryService(FakePort(companies=[_company(1, "Acme")]))
    with pytest.raises(CompanyNotFoundError) as excinfo:
        service.get_company(CompanyRef(999))
    assert excinfo.value.code == COMPANY_NOT_FOUND
    assert excinfo.value.details == {"company_id": 999}
    assert isinstance(excinfo.value, ApplicationError)


# ---------------------------------------------------------------------------
# get_chart_of_accounts
# ---------------------------------------------------------------------------


def test_get_chart_of_accounts_returns_views_in_engine_order():
    company = CompanyRef(1)
    chart = [_account(10, 1, "1.1.1", "Cash"), _account(11, 1, "1.1.2", "Bank")]
    service = AccountQueryService(FakePort(companies=[_company(1)], charts={company: chart}))
    assert service.get_chart_of_accounts(company) == chart
    assert all(isinstance(view, AccountView) for view in chart)


def test_get_chart_of_accounts_empty_for_existing_company():
    company = CompanyRef(1)
    service = AccountQueryService(FakePort(companies=[_company(1)], charts={}))
    assert service.get_chart_of_accounts(company) == []


def test_get_chart_of_accounts_missing_company_raises_typed_not_found():
    # The engine answers an unknown company with an empty chart; the
    # service must still report it as a missing company, not an empty one.
    service = AccountQueryService(FakePort(companies=[_company(1)], charts={}))
    with pytest.raises(CompanyNotFoundError) as excinfo:
        service.get_chart_of_accounts(CompanyRef(999))
    assert excinfo.value.code == COMPANY_NOT_FOUND
    assert excinfo.value.details == {"company_id": 999}


# ---------------------------------------------------------------------------
# Error translation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected_type", "retryable"),
    [
        ("ENGINE_UNAVAILABLE", EngineUnavailableError, True),
        ("ENGINE_AUTHENTICATION_FAILED", EngineAuthenticationError, False),
        ("ENGINE_VALIDATION_FAILED", EngineValidationError, False),
        ("ENGINE_ERROR", EngineError, False),
    ],
)
def test_engine_failure_translates_to_application_error(code, expected_type, retryable):
    failure = HesabYarError("engine exploded", code=code)
    service = CompanyQueryService(FakePort(failure=failure))
    with pytest.raises(expected_type) as excinfo:
        service.list_companies()
    assert excinfo.value.code == code
    assert excinfo.value.retryable is retryable
    assert isinstance(excinfo.value, ApplicationError)
    assert excinfo.value is not failure


def test_engine_failure_translates_through_chart_service():
    failure = HesabYarError("engine exploded", code="ENGINE_UNAVAILABLE")
    service = AccountQueryService(FakePort(failure=failure))
    with pytest.raises(EngineUnavailableError) as excinfo:
        service.get_chart_of_accounts(CompanyRef(1))
    assert excinfo.value.code == "ENGINE_UNAVAILABLE"


def test_ledger_error_passes_through_unchanged():
    # Domain ledger errors are already engine-neutral: the service must not
    # re-wrap them into a different type or code.
    failure = AccountNotFoundError("account 7 missing", details={"account_id": 7})
    service = CompanyQueryService(FakePort(failure=failure))
    with pytest.raises(AccountNotFoundError) as excinfo:
        service.list_companies()
    assert excinfo.value is failure
    assert excinfo.value.code == "LEDGER_ACCOUNT_NOT_FOUND"


def test_translate_engine_error_returns_unknown_codes_unchanged():
    error = HesabYarError("something else", code="SOME_OTHER_CODE")
    assert translate_engine_error(error) is error
    not_found = CompanyNotFoundError("missing", details={"company_id": 1})
    assert translate_engine_error(not_found) is not_found


# ---------------------------------------------------------------------------
# Static architecture guards (spec §6.3)
# ---------------------------------------------------------------------------

_FORBIDDEN_APPLICATION_IMPORTS = (
    "tryton",
    "httpx",
    "sqlalchemy",
    "psycopg",
    "requests",
    "fastapi",
    "starlette",
    "uvicorn",
    "alembic",
    "mcp",
)


def _iter_python_files(package: Path):
    return sorted(p for p in package.rglob("*.py") if p.is_file())


def _imported_modules(path: Path) -> set[str]:
    """Full dotted module names imported by a file."""
    tree = ast.parse(path.read_text(), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_application_layer_has_no_forbidden_imports():
    """Spec §6.3: no Tryton/HTTP/ORM/REST imports in the application layer."""
    package = REPO_ROOT / "src" / "hesabyar" / "application"
    assert package.is_dir()
    for path in _iter_python_files(package):
        top_modules = {name.split(".")[0] for name in _imported_modules(path)}
        forbidden = top_modules & set(_FORBIDDEN_APPLICATION_IMPORTS)
        assert not forbidden, f"{path.relative_to(REPO_ROOT)} imports {sorted(forbidden)}"


def test_application_layer_imports_only_domain_internally():
    """Spec §6.3: application -> domain ports only; never infrastructure/interfaces."""
    package = REPO_ROOT / "src" / "hesabyar" / "application"
    assert package.is_dir()
    for path in _iter_python_files(package):
        internal = {
            name
            for name in _imported_modules(path)
            if name == "hesabyar" or name.startswith("hesabyar.")
        }
        allowed = {
            name
            for name in internal
            if name.startswith("hesabyar.domain") or name.startswith("hesabyar.application")
        }
        forbidden = internal - allowed
        assert not forbidden, (
            f"{path.relative_to(REPO_ROOT)} imports internal modules outside "
            f"domain/application: {sorted(forbidden)}"
        )
