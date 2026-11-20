"""Unit tests for the fiscal + setup-status query services (US-203).

No live server is needed: the engine port is faked in memory. The static
architecture guards in ``test_application_services.py`` cover the whole
application package, so this module needs none of its own.
"""

from __future__ import annotations

import datetime
from collections.abc import Mapping, Sequence
from decimal import (
    Decimal,
)

import pytest

from hesabyar.application.errors import CompanyNotFoundError, EngineUnavailableError
from hesabyar.application.queries.fiscal import FiscalQueryService, SetupStatusService
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
    FiscalPeriodRef,
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
    CompanySetupStatus,
    CompanyView,
    FiscalPeriodView,
    FiscalYearView,
    InventoryView,
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
    ValidationSummary,
)
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.errors import (
    FiscalPeriodNotFoundError,
    FiscalYearNotFoundError,
    HesabYarError,
)

# ---------------------------------------------------------------------------
# Fake port
# ---------------------------------------------------------------------------


class FakePort:
    """In-memory AccountingEnginePort for unit tests (no server)."""

    def __init__(
        self,
        companies: Sequence[CompanyView] | None = None,
        charts: Mapping[CompanyRef, Sequence[AccountView]] | None = None,
        years: Mapping[CompanyRef, Sequence[FiscalYearView]] | None = None,
        periods: Mapping[FiscalYearRef, Sequence[FiscalPeriodView]] | None = None,
        failure: HesabYarError | None = None,
    ) -> None:
        self._companies = list(companies or [])
        self._charts = dict(charts or {})
        self._years = dict(years or {})
        self._periods = dict(periods or {})
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

    def post_journal(self, command: PostJournal) -> PostedJournalRef:
        raise NotImplementedError("query services never post journals")

    def reverse_journal(self, command: ReverseJournal) -> PostedJournalRef:
        raise NotImplementedError("query services never reverse journals")

    def get_journal(self, posted: PostedJournalRef) -> PostedJournalView | None:
        raise NotImplementedError("query services never read journals")

    def list_fiscal_years(self, company: CompanyRef) -> Sequence[FiscalYearView]:
        if self._failure is not None:
            raise self._failure
        # Like the real engine, an unknown company id yields an empty list.
        return list(self._years.get(company, []))

    def list_periods(
        self, company: CompanyRef, fiscal_year: FiscalYearRef | None = None
    ) -> Sequence[FiscalPeriodView]:
        if self._failure is not None:
            raise self._failure
        if fiscal_year is not None:
            return list(self._periods.get(fiscal_year, []))
        years = self._years.get(company, [])
        periods: list[FiscalPeriodView] = []
        for year in years:
            periods.extend(self._periods.get(year.ref, []))
        return sorted(periods, key=lambda period: period.start_date)

    def get_trial_balance(
        self,
        company: CompanyRef,
        date_from: FiscalDate,
        date_to: FiscalDate,
    ) -> TrialBalanceView:
        raise NotImplementedError("trial balance is not used here")

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


def _company(company_id: int, name: str = "Acme", currency: str = "IRR") -> CompanyView:
    return CompanyView(ref=CompanyRef(company_id), name=name, currency_code=currency)


def _account(account_id: int, company_id: int, code: str) -> AccountView:
    return AccountView(
        ref=AccountRef(account_id),
        company=CompanyRef(company_id),
        code=code,
        name="Cash",
        type_name="Asset",
        parent=None,
        closed=False,
        party_required=False,
        active=True,
    )


def _year(
    year_id: int,
    company_id: int,
    name: str,
    start: str,
    end: str,
    state: str = "open",
) -> FiscalYearView:
    return FiscalYearView(
        ref=FiscalYearRef(year_id),
        company=CompanyRef(company_id),
        name=name,
        start_date=FiscalDate(datetime.date.fromisoformat(start)),
        end_date=FiscalDate(datetime.date.fromisoformat(end)),
        state=state,
    )


def _period(
    period_id: int,
    year_id: int,
    name: str,
    start: str,
    end: str,
    state: str = "open",
    period_type: str = "standard",
) -> FiscalPeriodView:
    return FiscalPeriodView(
        name=name,
        start_date=FiscalDate(datetime.date.fromisoformat(start)),
        end_date=FiscalDate(datetime.date.fromisoformat(end)),
        state=state,
        ref=FiscalPeriodRef(period_id),
        fiscal_year=FiscalYearRef(year_id),
        period_type=period_type,
    )


def _fake_port() -> FakePort:
    """Company 1: two years (2025 closed, 2026 open), three periods.

    Company 2: no years, no periods, no chart — a company mid-setup.
    """
    years_1 = [
        _year(20, 1, "FY 2026", "2026-01-01", "2026-12-31", "open"),
        _year(10, 1, "FY 2025", "2025-01-01", "2025-12-31", "closed"),
    ]
    periods = {
        FiscalYearRef(20): [
            _period(201, 20, "Jan 2026", "2026-01-01", "2026-01-31", "closed"),
            _period(202, 20, "Feb 2026", "2026-02-01", "2026-02-28", "open"),
            _period(203, 20, "Adjustment 2026", "2026-12-01", "2026-12-31", "open", "adjustment"),
        ],
        FiscalYearRef(10): [
            _period(101, 10, "Jan 2025", "2025-01-01", "2025-01-31", "closed"),
        ],
    }
    return FakePort(
        companies=[_company(1), _company(2, "Beta")],
        charts={CompanyRef(1): [_account(1, 1, "1.1.1"), _account(2, 1, "1.1.2")]},
        years={CompanyRef(1): years_1},
        periods=periods,
    )


# ---------------------------------------------------------------------------
# list_fiscal_years / get_fiscal_year
# ---------------------------------------------------------------------------


def test_list_fiscal_years_returns_engine_order_newest_first():
    service = FiscalQueryService(_fake_port())
    years = service.list_fiscal_years(CompanyRef(1))
    assert [year.name for year in years] == ["FY 2026", "FY 2025"]
    assert years[0].is_open is True
    assert years[1].is_open is False
    assert all(isinstance(year, FiscalYearView) for year in years)


def test_list_fiscal_years_empty_for_company_without_years():
    service = FiscalQueryService(_fake_port())
    assert service.list_fiscal_years(CompanyRef(2)) == []


def test_list_fiscal_years_missing_company_raises_typed_not_found():
    service = FiscalQueryService(_fake_port())
    with pytest.raises(CompanyNotFoundError) as excinfo:
        service.list_fiscal_years(CompanyRef(999))
    assert excinfo.value.code == "COMPANY_NOT_FOUND"
    assert excinfo.value.details == {"company_id": 999}


def test_get_fiscal_year_returns_matching_view():
    service = FiscalQueryService(_fake_port())
    year = service.get_fiscal_year(CompanyRef(1), FiscalYearRef(10))
    assert year.name == "FY 2025"
    assert year.state == "closed"


def test_get_fiscal_year_missing_raises_typed_not_found():
    service = FiscalQueryService(_fake_port())
    with pytest.raises(FiscalYearNotFoundError) as excinfo:
        service.get_fiscal_year(CompanyRef(1), FiscalYearRef(999))
    assert excinfo.value.code == "LEDGER_FISCAL_YEAR_NOT_FOUND"
    assert excinfo.value.details == {"fiscal_year_id": 999, "company_id": 1}


def test_get_fiscal_year_of_another_company_raises_not_found():
    # Year 20 belongs to company 1; in company 2 it looks exactly like a
    # year that does not exist.
    service = FiscalQueryService(_fake_port())
    with pytest.raises(FiscalYearNotFoundError) as excinfo:
        service.get_fiscal_year(CompanyRef(2), FiscalYearRef(20))
    assert excinfo.value.code == "LEDGER_FISCAL_YEAR_NOT_FOUND"


# ---------------------------------------------------------------------------
# list_periods / get_period
# ---------------------------------------------------------------------------


def test_list_periods_returns_all_company_periods_by_start_date():
    service = FiscalQueryService(_fake_port())
    periods = service.list_periods(CompanyRef(1))
    assert [period.name for period in periods] == [
        "Jan 2025",
        "Jan 2026",
        "Feb 2026",
        "Adjustment 2026",
    ]
    assert all(isinstance(period, FiscalPeriodView) for period in periods)


def test_list_periods_filtered_by_fiscal_year():
    service = FiscalQueryService(_fake_port())
    periods = service.list_periods(CompanyRef(1), FiscalYearRef(20))
    assert [period.name for period in periods] == [
        "Jan 2026",
        "Feb 2026",
        "Adjustment 2026",
    ]
    assert all(period.fiscal_year == FiscalYearRef(20) for period in periods)


def test_list_periods_unknown_year_raises_not_found_not_empty_list():
    service = FiscalQueryService(_fake_port())
    with pytest.raises(FiscalYearNotFoundError):
        service.list_periods(CompanyRef(1), FiscalYearRef(999))


def test_list_periods_year_of_another_company_raises_not_found():
    service = FiscalQueryService(_fake_port())
    with pytest.raises(FiscalYearNotFoundError):
        service.list_periods(CompanyRef(2), FiscalYearRef(20))


def test_list_periods_missing_company_raises_typed_not_found():
    service = FiscalQueryService(_fake_port())
    with pytest.raises(CompanyNotFoundError):
        service.list_periods(CompanyRef(999))


def test_list_periods_empty_for_company_without_periods():
    service = FiscalQueryService(_fake_port())
    assert service.list_periods(CompanyRef(2)) == []


def test_get_period_returns_matching_view():
    service = FiscalQueryService(_fake_port())
    period = service.get_period(CompanyRef(1), FiscalPeriodRef(202))
    assert period.name == "Feb 2026"
    assert period.is_open is True
    assert period.period_type == "standard"
    adjustment = service.get_period(CompanyRef(1), FiscalPeriodRef(203))
    assert adjustment.period_type == "adjustment"


def test_get_period_missing_raises_typed_not_found():
    service = FiscalQueryService(_fake_port())
    with pytest.raises(FiscalPeriodNotFoundError) as excinfo:
        service.get_period(CompanyRef(1), FiscalPeriodRef(999))
    assert excinfo.value.code == "LEDGER_FISCAL_PERIOD_NOT_FOUND"
    assert excinfo.value.details == {"fiscal_period_id": 999, "company_id": 1}


def test_get_period_of_another_company_raises_not_found():
    service = FiscalQueryService(_fake_port())
    with pytest.raises(FiscalPeriodNotFoundError):
        service.get_period(CompanyRef(2), FiscalPeriodRef(202))


# ---------------------------------------------------------------------------
# get_setup_status
# ---------------------------------------------------------------------------


def test_get_setup_status_ready_company():
    service = SetupStatusService(_fake_port())
    status = service.get_setup_status(CompanyRef(1))
    assert isinstance(status, CompanySetupStatus)
    assert status.company == CompanyRef(1)
    assert status.chart_installed is True
    assert status.account_count == 2
    # The port does not expose account templates yet (see
    # docs/setup-wizard.md §5); the engine does record them.
    assert status.chart_template is None
    assert status.fiscal_year_present is True
    assert status.fiscal_year_count == 2
    assert status.open_fiscal_year is not None
    assert status.open_fiscal_year.name == "FY 2026"
    assert status.open_period_present is True
    assert status.open_period_count == 2
    assert status.ready is True


def test_get_setup_status_company_mid_setup_is_not_ready():
    service = SetupStatusService(_fake_port())
    status = service.get_setup_status(CompanyRef(2))
    assert status.chart_installed is False
    assert status.account_count == 0
    assert status.fiscal_year_present is False
    assert status.fiscal_year_count == 0
    assert status.open_fiscal_year is None
    assert status.open_period_present is False
    assert status.open_period_count == 0
    assert status.ready is False


def test_get_setup_status_chart_without_open_period_is_not_ready():
    port = FakePort(
        companies=[_company(1)],
        charts={CompanyRef(1): [_account(1, 1, "1.1.1")]},
        years={CompanyRef(1): [_year(10, 1, "FY 2025", "2025-01-01", "2025-12-31", "closed")]},
        periods={
            FiscalYearRef(10): [
                _period(101, 10, "Jan 2025", "2025-01-01", "2025-01-31", "closed")
            ]
        },
    )
    status = SetupStatusService(port).get_setup_status(CompanyRef(1))
    assert status.chart_installed is True
    assert status.fiscal_year_present is True
    assert status.open_fiscal_year is None
    assert status.open_period_present is False
    assert status.ready is False


def test_get_setup_status_missing_company_raises_typed_not_found():
    service = SetupStatusService(_fake_port())
    with pytest.raises(CompanyNotFoundError):
        service.get_setup_status(CompanyRef(999))


# ---------------------------------------------------------------------------
# Error translation
# ---------------------------------------------------------------------------


def test_engine_failure_translates_through_fiscal_service():
    failure = HesabYarError("engine exploded", code="ENGINE_UNAVAILABLE")
    port = FakePort(companies=[_company(1)], failure=failure)
    service = FiscalQueryService(port)
    with pytest.raises(EngineUnavailableError) as excinfo:
        service.list_fiscal_years(CompanyRef(1))
    assert excinfo.value.code == "ENGINE_UNAVAILABLE"
    assert excinfo.value.retryable is True


def test_engine_failure_translates_through_setup_status_service():
    failure = HesabYarError("engine exploded", code="ENGINE_UNAVAILABLE")
    port = FakePort(companies=[_company(1)], failure=failure)
    service = SetupStatusService(port)
    with pytest.raises(EngineUnavailableError):
        service.get_setup_status(CompanyRef(1))
