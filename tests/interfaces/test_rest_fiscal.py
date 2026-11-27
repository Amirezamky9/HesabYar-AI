"""Tests for the fiscal-year / period / setup-status REST endpoints (US-203).

The engine is faked in memory (``FakePort`` behind the real US-203 query
services, injected via ``app.dependency_overrides``); everything else is
real — the FastAPI app, scoped API-key auth, the PostgreSQL database,
and the company-resolution dependencies. The live Tryton server is NOT
needed here; end-to-end tests against the real server live in
``test_rest_fiscal_e2e.py``.

Provisioned rows are committed (the app's per-request sessions must see
them) and removed in teardown by deleting the tenant (cascades to
clients/keys/companies).
"""

from __future__ import annotations

import datetime
import os
import uuid
from collections.abc import Generator, Mapping, Sequence
from dataclasses import dataclass
from decimal import (
    Decimal,
)

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from hesabyar.application.errors import EngineError, EngineUnavailableError
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
from hesabyar.domain.common.errors import HesabYarError
from hesabyar.infrastructure.auth import ApiKeyCrypto, ApiKeyService
from hesabyar.infrastructure.config import AppSettings
from hesabyar.infrastructure.persistence.postgres.models import (
    ApiClientModel,
    Base,
    EngineCompanyModel,
    EngineConnectionModel,
    TenantModel,
)
from hesabyar.interfaces.rest.app import create_app
from hesabyar.interfaces.rest.dependencies import (
    get_fiscal_query_service,
    get_setup_status_service,
)

TEST_DB_URL = os.getenv(
    "HESABYAR_DATABASE_URL",
    "postgresql+psycopg://hesabyar:hesabyar_pass@hesabyar-postgres:5432/hesabyar_test",
)

INSTALLATION_ID = "00000000-0000-0000-0000-000000000005"

# Fake engine ids. 101/102 are mapped to this tenant; 103 exists in the
# engine but has NO mapping row (must stay invisible); 104 is mapped but
# does NOT exist in the engine (service-level 404).
ENGINE_A = 101
ENGINE_B = 102
ENGINE_UNMAPPED = 103
ENGINE_MISSING = 104


# ---------------------------------------------------------------------------
# Fake port + fake engine data
# ---------------------------------------------------------------------------


class FakePort:
    """In-memory AccountingEnginePort for endpoint tests (no server)."""

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
        raise NotImplementedError("query endpoints never create drafts")

    def validate_journal_draft(self, draft: JournalDraftRef) -> ValidationSummary:
        raise NotImplementedError("query endpoints never validate drafts")

    def get_journal_draft(self, draft: JournalDraftRef) -> JournalDraftView | None:
        raise NotImplementedError("query endpoints never read drafts")

    def list_journal_drafts(self, company: CompanyRef) -> Sequence[JournalDraftView]:
        raise NotImplementedError("query endpoints never list drafts")

    def update_journal_draft(self, command: UpdateJournalDraft) -> JournalDraftView:
        raise NotImplementedError("query endpoints never update drafts")

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
        raise NotImplementedError("query endpoints never post journals")

    def reverse_journal(self, command: ReverseJournal) -> PostedJournalRef:
        raise NotImplementedError("query endpoints never reverse journals")

    def get_journal(self, posted: PostedJournalRef) -> PostedJournalView | None:
        raise NotImplementedError("query endpoints never read journals")

    def list_fiscal_years(self, company: CompanyRef) -> Sequence[FiscalYearView]:
        if self._failure is not None:
            raise self._failure
        return list(self._years.get(company, []))

    def list_periods(
        self, company: CompanyRef, fiscal_year: FiscalYearRef | None = None
    ) -> Sequence[FiscalPeriodView]:
        if self._failure is not None:
            raise self._failure
        if fiscal_year is not None:
            return list(self._periods.get(fiscal_year, []))
        periods: list[FiscalPeriodView] = []
        for year in self._years.get(company, []):
            periods.extend(self._periods.get(year.ref, []))
        return sorted(periods, key=lambda period: period.start_date)

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
        raise NotImplementedError("query endpoints never create invoices")

    def validate_invoice_draft(self, invoice: InvoiceRef) -> ValidationSummary:
        raise NotImplementedError("query endpoints never validate invoices")

    def get_invoice(self, invoice: InvoiceRef) -> InvoiceView | None:
        raise NotImplementedError("query endpoints never read invoices")

    def cancel_invoice(self, invoice: InvoiceRef) -> InvoiceView:
        raise NotImplementedError("query endpoints never cancel invoices")

    def list_invoices(
        self,
        company: CompanyRef,
        invoice_type: str | None = None,
        state: str | None = None,
    ) -> Sequence[InvoiceView]:
        raise NotImplementedError("query endpoints never list invoices")

    def get_payment(self, payment: PaymentRef) -> PaymentView | None:
        raise NotImplementedError("query endpoints never read payments")

    def list_payments(
        self,
        company: CompanyRef,
        kind: str | None = None,
        state: str | None = None,
    ) -> Sequence[PaymentView]:
        raise NotImplementedError("query endpoints never list payments")

    def get_bank_statement(self, statement: BankStatementRef) -> BankStatementView | None:
        raise NotImplementedError("query endpoints never read statements")

    def create_bank_statement(self, command: CreateBankStatement) -> BankStatementRef:
        raise NotImplementedError("this fake never creates statements")

    def update_bank_statement_line(self, command: UpdateBankStatementLine) -> None:
        raise NotImplementedError("this fake never reconciles statements")

    def list_statement_journals(self, company: CompanyRef) -> Sequence[StatementJournalView]:
        return []

    def list_bank_statements(
        self, company: CompanyRef, state: str | None = None
    ) -> Sequence[BankStatementView]:
        raise NotImplementedError("query endpoints never list statements")

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


def _company(engine_id: int, name: str, currency: str = "IRR") -> CompanyView:
    return CompanyView(ref=CompanyRef(engine_id), name=name, currency_code=currency)


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


def _fake_port(failure: HesabYarError | None = None) -> FakePort:
    companies = [
        _company(ENGINE_A, "Acme Trading", "IRR"),
        _company(ENGINE_B, "Beta Foods", "USD"),
        _company(ENGINE_UNMAPPED, "Ghost Holdings", "IRR"),
    ]
    charts = {
        CompanyRef(ENGINE_A): [_account(1, ENGINE_A, "1.1.1"), _account(2, ENGINE_A, "1.1.2")],
        CompanyRef(ENGINE_B): [_account(11, ENGINE_B, "1.1.1")],
    }
    years = {
        CompanyRef(ENGINE_A): [
            _year(20, ENGINE_A, "FY 2026", "2026-01-01", "2026-12-31", "open"),
            _year(10, ENGINE_A, "FY 2025", "2025-01-01", "2025-12-31", "closed"),
        ],
        CompanyRef(ENGINE_B): [
            _year(30, ENGINE_B, "FY 2025", "2025-01-01", "2025-12-31", "closed"),
        ],
    }
    periods = {
        FiscalYearRef(20): [
            _period(201, 20, "Jan 2026", "2026-01-01", "2026-01-31", "closed"),
            _period(202, 20, "Feb 2026", "2026-02-01", "2026-02-28", "open"),
            _period(203, 20, "Adjustment 2026", "2026-12-01", "2026-12-31", "open", "adjustment"),
        ],
        FiscalYearRef(10): [
            _period(101, 10, "Jan 2025", "2025-01-01", "2025-01-31", "closed"),
        ],
        FiscalYearRef(30): [
            _period(301, 30, "Jan 2025", "2025-01-01", "2025-01-31", "closed"),
        ],
    }
    return FakePort(
        companies=companies, charts=charts, years=years, periods=periods, failure=failure
    )


# ---------------------------------------------------------------------------
# Provisioned world (real DB, real keys)
# ---------------------------------------------------------------------------


@dataclass
class World:
    """Provisioned tenant, companies and keys for one test."""

    company_a_id: str  # engine_companies row id for engine 101
    company_b_id: str  # engine_companies row id for engine 102
    company_missing_id: str  # row id for engine 104 (not in the engine)
    other_tenant_company_id: str
    key_full_secret: str  # ledger:read + company:read + account:read, unrestricted
    key_ledger_secret: str  # ledger:read only, unrestricted
    key_company_secret: str  # company:read only, unrestricted
    key_restricted_a_secret: str  # ledger:read, restricted to company A


@pytest.fixture(scope="module")
def engine() -> Generator[Engine, None, None]:
    eng = create_engine(TEST_DB_URL, echo=False)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def settings() -> AppSettings:
    """Fresh settings (dev pepper) — the same pepper the app dependency uses."""
    return AppSettings()


@pytest.fixture()
def world(engine: Engine, settings: AppSettings) -> Generator[World, None, None]:
    """Provision two tenants, four companies and four keys; clean up after."""
    session = sessionmaker(bind=engine)()
    service = ApiKeyService(ApiKeyCrypto(settings.auth.api_key_pepper))
    tenant: TenantModel | None = None
    other_tenant: TenantModel | None = None
    tenant_ids: list[str] = []
    connection_ids: list[str] = []
    try:
        tenant = TenantModel(
            id=str(uuid.uuid4()), slug=f"rest-203-{uuid.uuid4().hex[:8]}", name="Tenant 1"
        )
        other_tenant = TenantModel(
            id=str(uuid.uuid4()), slug=f"rest-203-{uuid.uuid4().hex[:8]}", name="Tenant 2"
        )
        session.add_all([tenant, other_tenant])
        session.flush()
        client = ApiClientModel(
            id=str(uuid.uuid4()), tenant_id=tenant.id, name="rest-203 client"
        )
        session.add(client)
        session.flush()

        def _company(tenant_id: str, external_id: str) -> EngineCompanyModel:
            # One company per (tenant, connection) — unique constraint.
            connection = EngineConnectionModel(
                id=str(uuid.uuid4()),
                name=f"conn-{uuid.uuid4().hex[:8]}",
                engine="tryton",
                base_url="http://localhost:8000",
                # (engine, database_name) is unique per connection.
                database_name=f"hesabyar_tryton_test_{uuid.uuid4().hex[:8]}",
            )
            session.add(connection)
            session.flush()
            connection_ids.append(connection.id)
            row = EngineCompanyModel(
                id=str(uuid.uuid4()),
                tenant_id=tenant_id,
                engine_connection_id=connection.id,
                external_company_id=external_id,
                company_name=f"Company {external_id}",
            )
            session.add(row)
            session.flush()
            return row

        company_a = _company(tenant.id, str(ENGINE_A))
        company_b = _company(tenant.id, str(ENGINE_B))
        company_missing = _company(tenant.id, str(ENGINE_MISSING))
        company_other = _company(other_tenant.id, "999")

        def _key(scopes: list[str], **kwargs: object):
            return service.issue_key(
                session,
                client=client,
                name="rest-203 key",
                scopes=scopes,
                installation_id=INSTALLATION_ID,
                **kwargs,  # type: ignore[arg-type]
            )

        key_full = _key(["ledger:read", "company:read", "account:read"])
        key_ledger = _key(["ledger:read"])
        key_company = _key(["company:read"])
        key_restricted_a = _key(["ledger:read"], engine_company_id=company_a.id)
        session.commit()
        # Capture ids as plain strings while the session is still open:
        # commit expires the ORM attributes, and teardown closes the
        # session, so accessing tenant.id there would raise
        # DetachedInstanceError.
        tenant_ids = [tenant.id, other_tenant.id]
        yield World(
            company_a_id=company_a.id,
            company_b_id=company_b.id,
            company_missing_id=company_missing.id,
            other_tenant_company_id=company_other.id,
            key_full_secret=key_full.secret,
            key_ledger_secret=key_ledger.secret,
            key_company_secret=key_company.secret,
            key_restricted_a_secret=key_restricted_a.secret,
        )
    finally:
        session.close()
        cleanup = sessionmaker(bind=engine)()
        try:
            if tenant_ids:
                cleanup.query(TenantModel).filter(
                    TenantModel.id.in_(tenant_ids)
                ).delete(synchronize_session=False)
            if connection_ids:
                cleanup.query(EngineConnectionModel).filter(
                    EngineConnectionModel.id.in_(connection_ids)
                ).delete(synchronize_session=False)
            cleanup.commit()
        finally:
            cleanup.close()


def _test_client(settings: AppSettings, failure: HesabYarError | None = None) -> TestClient:
    """Real app with the query services backed by the fake engine."""
    app = create_app(settings=settings)
    port = _fake_port(failure)
    app.dependency_overrides[get_fiscal_query_service] = lambda: FiscalQueryService(port)
    app.dependency_overrides[get_setup_status_service] = lambda: SetupStatusService(port)
    return TestClient(app, raise_server_exceptions=False)


def _auth(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


# ---------------------------------------------------------------------------
# Auth + company scoping (shared by all five endpoints)
# ---------------------------------------------------------------------------


def test_fiscal_endpoints_require_authentication_and_ledger_scope(
    settings: AppSettings, world: World
) -> None:
    client = _test_client(settings)
    base = f"/api/v1/accounting/fiscal-years?company={world.company_a_id}"
    assert client.get(base).status_code == 401
    assert client.get(base).json()["code"] == "UNAUTHORIZED"
    # company:read key has no ledger:read scope.
    forbidden = client.get(base, headers=_auth(world.key_company_secret))
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "FORBIDDEN"
    assert "ledger:read" in forbidden.json()["detail"]
    # ledger:read-only key is enough.
    ok = client.get(base, headers=_auth(world.key_ledger_secret))
    assert ok.status_code == 200


def test_setup_status_requires_ledger_scope(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    path = f"/api/v1/accounting/setup-status?company={world.company_a_id}"
    assert client.get(path).status_code == 401
    forbidden = client.get(path, headers=_auth(world.key_company_secret))
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "FORBIDDEN"


def test_fiscal_endpoints_company_param_rules(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    # Unrestricted key without the company param → 400 COMPANY_REQUIRED.
    missing = client.get(
        "/api/v1/accounting/fiscal-years", headers=_auth(world.key_full_secret)
    )
    assert missing.status_code == 400
    assert missing.json()["code"] == "COMPANY_REQUIRED"
    missing_status = client.get(
        "/api/v1/accounting/setup-status", headers=_auth(world.key_full_secret)
    )
    assert missing_status.status_code == 400
    assert missing_status.json()["code"] == "COMPANY_REQUIRED"
    # Restricted key defaults to its company.
    ok = client.get(
        "/api/v1/accounting/fiscal-years", headers=_auth(world.key_restricted_a_secret)
    )
    assert ok.status_code == 200
    assert [item["name"] for item in ok.json()["items"]] == ["FY 2026", "FY 2025"]
    # Unknown company → 404.
    unknown = client.get(
        f"/api/v1/accounting/fiscal-years?company={uuid.uuid4()}",
        headers=_auth(world.key_full_secret),
    )
    assert unknown.status_code == 404
    assert unknown.json()["code"] == "COMPANY_NOT_FOUND"
    # Restricted key cannot request another company → 403.
    forbidden = client.get(
        f"/api/v1/accounting/fiscal-years?company={world.company_b_id}",
        headers=_auth(world.key_restricted_a_secret),
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "FORBIDDEN"
    # Mapped but not in the engine → 404 from the service.
    not_in_engine = client.get(
        f"/api/v1/accounting/fiscal-years?company={world.company_missing_id}",
        headers=_auth(world.key_full_secret),
    )
    assert not_in_engine.status_code == 404
    assert not_in_engine.json()["code"] == "COMPANY_NOT_FOUND"


# ---------------------------------------------------------------------------
# GET /accounting/fiscal-years
# ---------------------------------------------------------------------------


def test_list_fiscal_years_returns_scoped_years_newest_first(
    settings: AppSettings, world: World
) -> None:
    client = _test_client(settings)
    response = client.get(
        f"/api/v1/accounting/fiscal-years?company={world.company_a_id}",
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["next_cursor"] is None
    assert [item["name"] for item in body["items"]] == ["FY 2026", "FY 2025"]
    fy2026 = body["items"][0]
    assert fy2026["id"] == 20
    assert fy2026["company_id"] == world.company_a_id
    assert fy2026["start_date"] == "2026-01-01"
    assert fy2026["end_date"] == "2026-12-31"
    assert fy2026["start_date_jalali"] == "1404/10/11"
    assert fy2026["end_date_jalali"] == "1405/10/10"
    assert fy2026["state"] == "open"
    assert fy2026["is_open"] is True
    assert set(fy2026) == {
        "id",
        "company_id",
        "name",
        "start_date",
        "end_date",
        "start_date_jalali",
        "end_date_jalali",
        "state",
        "is_open",
    }
    assert body["items"][1]["is_open"] is False


def test_list_fiscal_years_accepts_engine_company_id(
    settings: AppSettings, world: World
) -> None:
    client = _test_client(settings)
    response = client.get(
        f"/api/v1/accounting/fiscal-years?company={ENGINE_B}",
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 200
    assert [item["name"] for item in response.json()["items"]] == ["FY 2025"]
    assert response.json()["items"][0]["company_id"] == world.company_b_id


def test_list_fiscal_years_filters_sorts_and_paginates(
    settings: AppSettings, world: World
) -> None:
    client = _test_client(settings)
    headers = _auth(world.key_full_secret)
    base = f"/api/v1/accounting/fiscal-years?company={world.company_a_id}"
    open_only = client.get(f"{base}&state=open", headers=headers)
    assert [item["name"] for item in open_only.json()["items"]] == ["FY 2026"]
    closed_only = client.get(f"{base}&state=closed", headers=headers)
    assert [item["name"] for item in closed_only.json()["items"]] == ["FY 2025"]
    by_name = client.get(f"{base}&sort=name", headers=headers)
    assert [item["name"] for item in by_name.json()["items"]] == ["FY 2025", "FY 2026"]
    oldest_first = client.get(f"{base}&sort=start_date", headers=headers)
    assert [item["name"] for item in oldest_first.json()["items"]] == ["FY 2025", "FY 2026"]
    first = client.get(f"{base}&limit=1", headers=headers)
    assert [item["name"] for item in first.json()["items"]] == ["FY 2026"]
    cursor = first.json()["next_cursor"]
    assert cursor
    second = client.get(f"{base}&limit=1&cursor={cursor}", headers=headers)
    assert [item["name"] for item in second.json()["items"]] == ["FY 2025"]
    assert second.json()["next_cursor"] is None
    bad_sort = client.get(f"{base}&sort=created_at", headers=headers)
    assert bad_sort.status_code == 400
    assert bad_sort.json()["code"] == "INVALID_SORT"
    bad_param = client.get(f"{base}&stat=open", headers=headers)
    assert bad_param.status_code == 400
    assert bad_param.json()["code"] == "INVALID_FILTER"


# ---------------------------------------------------------------------------
# GET /accounting/fiscal-years/{fiscal_year_id}
# ---------------------------------------------------------------------------


def test_get_fiscal_year_returns_scoped_year(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get(
        f"/api/v1/accounting/fiscal-years/20?company={world.company_a_id}",
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 200
    assert response.json()["name"] == "FY 2026"
    assert response.json()["company_id"] == world.company_a_id


def test_get_fiscal_year_unknown_and_other_company_return_404(
    settings: AppSettings, world: World
) -> None:
    client = _test_client(settings)
    headers = _auth(world.key_full_secret)
    unknown = client.get(
        f"/api/v1/accounting/fiscal-years/999999999?company={world.company_a_id}",
        headers=headers,
    )
    assert unknown.status_code == 404
    assert unknown.json()["code"] == "FISCAL_YEAR_NOT_FOUND"
    # Year 30 belongs to company B; in company A it looks exactly like a
    # year that does not exist.
    other = client.get(
        f"/api/v1/accounting/fiscal-years/30?company={world.company_a_id}",
        headers=headers,
    )
    assert other.status_code == 404
    assert other.json()["code"] == "FISCAL_YEAR_NOT_FOUND"


# ---------------------------------------------------------------------------
# GET /accounting/fiscal-periods
# ---------------------------------------------------------------------------


def test_list_fiscal_periods_returns_scoped_periods_earliest_first(
    settings: AppSettings, world: World
) -> None:
    client = _test_client(settings)
    response = client.get(
        f"/api/v1/accounting/fiscal-periods?company={world.company_a_id}",
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["next_cursor"] is None
    assert [item["name"] for item in body["items"]] == [
        "Jan 2025",
        "Jan 2026",
        "Feb 2026",
        "Adjustment 2026",
    ]
    feb = body["items"][2]
    assert feb["id"] == 202
    assert feb["company_id"] == world.company_a_id
    assert feb["fiscal_year_id"] == 20
    assert feb["start_date"] == "2026-02-01"
    assert feb["end_date"] == "2026-02-28"
    assert feb["start_date_jalali"] == "1404/11/12"
    assert feb["end_date_jalali"] == "1404/12/09"
    assert feb["state"] == "open"
    assert feb["is_open"] is True
    assert feb["type"] == "standard"
    assert set(feb) == {
        "id",
        "company_id",
        "fiscal_year_id",
        "name",
        "start_date",
        "end_date",
        "start_date_jalali",
        "end_date_jalali",
        "state",
        "is_open",
        "type",
    }
    assert body["items"][3]["type"] == "adjustment"


def test_list_fiscal_periods_filters(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    headers = _auth(world.key_full_secret)
    base = f"/api/v1/accounting/fiscal-periods?company={world.company_a_id}"
    by_year = client.get(f"{base}&fiscal_year=20", headers=headers)
    assert [item["name"] for item in by_year.json()["items"]] == [
        "Jan 2026",
        "Feb 2026",
        "Adjustment 2026",
    ]
    open_only = client.get(f"{base}&state=open", headers=headers)
    assert [item["name"] for item in open_only.json()["items"]] == [
        "Feb 2026",
        "Adjustment 2026",
    ]
    adjustments = client.get(f"{base}&type=adjustment", headers=headers)
    assert [item["name"] for item in adjustments.json()["items"]] == ["Adjustment 2026"]
    combined = client.get(f"{base}&fiscal_year=20&state=closed", headers=headers)
    assert [item["name"] for item in combined.json()["items"]] == ["Jan 2026"]
    by_name = client.get(f"{base}&sort=-name&limit=2", headers=headers)
    assert [item["name"] for item in by_name.json()["items"]] == ["Jan 2026", "Jan 2025"]
    bad_filter = client.get(f"{base}&year=20", headers=headers)
    assert bad_filter.status_code == 400
    assert bad_filter.json()["code"] == "INVALID_FILTER"


def test_list_fiscal_periods_unknown_year_returns_404(
    settings: AppSettings, world: World
) -> None:
    client = _test_client(settings)
    headers = _auth(world.key_full_secret)
    base = f"/api/v1/accounting/fiscal-periods?company={world.company_a_id}"
    unknown = client.get(f"{base}&fiscal_year=999999999", headers=headers)
    assert unknown.status_code == 404
    assert unknown.json()["code"] == "FISCAL_YEAR_NOT_FOUND"
    # Year 30 belongs to company B.
    other = client.get(f"{base}&fiscal_year=30", headers=headers)
    assert other.status_code == 404
    assert other.json()["code"] == "FISCAL_YEAR_NOT_FOUND"


# ---------------------------------------------------------------------------
# GET /accounting/fiscal-periods/{period_id}
# ---------------------------------------------------------------------------


def test_get_fiscal_period_returns_scoped_period(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get(
        f"/api/v1/accounting/fiscal-periods/202?company={world.company_a_id}",
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 200
    assert response.json()["name"] == "Feb 2026"
    assert response.json()["fiscal_year_id"] == 20


def test_get_fiscal_period_unknown_and_other_company_return_404(
    settings: AppSettings, world: World
) -> None:
    client = _test_client(settings)
    headers = _auth(world.key_full_secret)
    unknown = client.get(
        f"/api/v1/accounting/fiscal-periods/999999999?company={world.company_a_id}",
        headers=headers,
    )
    assert unknown.status_code == 404
    assert unknown.json()["code"] == "FISCAL_PERIOD_NOT_FOUND"
    # Period 301 belongs to company B.
    other = client.get(
        f"/api/v1/accounting/fiscal-periods/301?company={world.company_a_id}",
        headers=headers,
    )
    assert other.status_code == 404
    assert other.json()["code"] == "FISCAL_PERIOD_NOT_FOUND"


# ---------------------------------------------------------------------------
# GET /accounting/setup-status
# ---------------------------------------------------------------------------


def test_get_setup_status_ready_company(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get(
        f"/api/v1/accounting/setup-status?company={world.company_a_id}",
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["company_id"] == world.company_a_id
    assert body["chart_installed"] is True
    assert body["account_count"] == 2
    assert body["chart_template"] is None
    assert body["fiscal_year_present"] is True
    assert body["fiscal_year_count"] == 2
    assert body["open_fiscal_year"]["name"] == "FY 2026"
    assert body["open_fiscal_year"]["id"] == 20
    assert body["open_fiscal_year"]["company_id"] == world.company_a_id
    assert body["open_period_present"] is True
    assert body["open_period_count"] == 2
    assert body["ready"] is True
    assert set(body) == {
        "company_id",
        "chart_installed",
        "account_count",
        "chart_template",
        "fiscal_year_present",
        "fiscal_year_count",
        "open_fiscal_year",
        "open_period_present",
        "open_period_count",
        "ready",
    }


def test_get_setup_status_company_without_open_period_is_not_ready(
    settings: AppSettings, world: World
) -> None:
    # Company B has a chart and a fiscal year, but both are closed.
    client = _test_client(settings)
    response = client.get(
        f"/api/v1/accounting/setup-status?company={world.company_b_id}",
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["chart_installed"] is True
    assert body["account_count"] == 1
    assert body["fiscal_year_present"] is True
    assert body["fiscal_year_count"] == 1
    assert body["open_fiscal_year"] is None
    assert body["open_period_present"] is False
    assert body["open_period_count"] == 0
    assert body["ready"] is False


def test_get_setup_status_missing_company_returns_404(
    settings: AppSettings, world: World
) -> None:
    client = _test_client(settings)
    response = client.get(
        f"/api/v1/accounting/setup-status?company={world.company_missing_id}",
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "COMPANY_NOT_FOUND"


# ---------------------------------------------------------------------------
# Engine failures map to typed 502/503
# ---------------------------------------------------------------------------


def test_engine_unavailable_maps_to_503(settings: AppSettings, world: World) -> None:
    client = _test_client(settings, failure=EngineUnavailableError("engine down"))
    response = client.get(
        f"/api/v1/accounting/fiscal-years?company={world.company_a_id}",
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 503
    assert response.json()["code"] == "ENGINE_UNAVAILABLE"


def test_engine_error_maps_to_502(settings: AppSettings, world: World) -> None:
    client = _test_client(settings, failure=EngineError("boom"))
    response = client.get(
        f"/api/v1/accounting/setup-status?company={world.company_a_id}",
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 502
    assert response.json()["code"] == "ENGINE_ERROR"
    # The engine's own message must not leak into the response.
    assert "boom" not in response.text


# ---------------------------------------------------------------------------
# OpenAPI
# ---------------------------------------------------------------------------


def test_openapi_lists_all_five_endpoints(settings: AppSettings) -> None:
    client = _test_client(settings)
    document = client.get("/openapi.json").json()
    paths = document["paths"]
    for path in (
        "/api/v1/accounting/fiscal-years",
        "/api/v1/accounting/fiscal-years/{fiscal_year_id}",
        "/api/v1/accounting/fiscal-periods",
        "/api/v1/accounting/fiscal-periods/{period_id}",
        "/api/v1/accounting/setup-status",
    ):
        assert path in paths, f"{path} missing from OpenAPI"
        operation = paths[path]["get"]
        assert {"HesabYar API key": []} in operation.get("security", [])
    years_params = {
        p["name"] for p in paths["/api/v1/accounting/fiscal-years"]["get"]["parameters"]
    }
    assert {"company", "state", "sort", "limit", "cursor"} <= years_params
    periods_params = {
        p["name"]
        for p in paths["/api/v1/accounting/fiscal-periods"]["get"]["parameters"]
    }
    assert {"company", "fiscal_year", "state", "type", "sort", "limit", "cursor"} <= periods_params
