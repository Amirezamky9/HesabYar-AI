"""Tests for the companies + accounts REST endpoints (US-105).

The engine is faked in memory (``FakePort`` behind the real US-104 query
services, injected via ``app.dependency_overrides``); everything else is
real — the FastAPI app, scoped API-key auth, the PostgreSQL database,
and the company-resolution dependencies. The live Tryton server is NOT
needed here; end-to-end tests against the real server live in
``test_rest_companies_accounts_e2e.py``.

Provisioned rows are committed (the app's per-request sessions must see
them) and removed in teardown by deleting the tenant (cascades to
clients/keys/companies).
"""

from __future__ import annotations

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
    get_account_query_service,
    get_company_query_service,
)

TEST_DB_URL = os.getenv(
    "HESABYAR_DATABASE_URL",
    "postgresql+psycopg://hesabyar:hesabyar_pass@hesabyar-postgres:5432/hesabyar_test",
)

INSTALLATION_ID = "00000000-0000-0000-0000-000000000003"

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
        failure: HesabYarError | None = None,
    ) -> None:
        self._companies = list(companies or [])
        self._charts = dict(charts or {})
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
        raise NotImplementedError("query endpoints never list fiscal years")

    def list_periods(
        self, company: CompanyRef, fiscal_year: FiscalYearRef | None = None
    ) -> Sequence[FiscalPeriodView]:
        raise NotImplementedError("query endpoints never list periods")

    def get_trial_balance(
        self,
        company: CompanyRef,
        date_from: FiscalDate,
        date_to: FiscalDate,
    ) -> TrialBalanceView:
        raise NotImplementedError("trial balance is not used here")

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


def _account(
    account_id: int,
    company_id: int,
    code: str,
    name: str,
    type_name: str | None = "Asset",
    *,
    parent: int | None = None,
    closed: bool = False,
    active: bool = True,
) -> AccountView:
    return AccountView(
        ref=AccountRef(account_id),
        company=CompanyRef(company_id),
        code=code,
        name=name,
        type_name=type_name,
        parent=AccountRef(parent) if parent is not None else None,
        closed=closed,
        party_required=False,
        active=active,
    )


def _fake_port(failure: HesabYarError | None = None) -> FakePort:
    companies = [
        _company(ENGINE_A, "Acme Trading", "IRR"),
        _company(ENGINE_B, "Beta Foods", "USD"),
        _company(ENGINE_UNMAPPED, "Ghost Holdings", "IRR"),
    ]
    charts = {
        CompanyRef(ENGINE_A): [
            _account(1, ENGINE_A, "", "Root", None),
            _account(2, ENGINE_A, "1.1.1", "Cash", "Asset", parent=1),
            _account(3, ENGINE_A, "1.1.2", "Bank", "Asset", parent=1),
            _account(4, ENGINE_A, "2.1.1", "Payables", "Liability", parent=1),
            _account(5, ENGINE_A, "4.1.1", "Sales Revenue", "Income", parent=1),
            _account(6, ENGINE_A, "4.1.2", "Old Revenue", "Income", parent=1, closed=True),
            _account(7, ENGINE_A, "9.9.9", "Dormant", "Asset", parent=1, active=False),
        ],
        CompanyRef(ENGINE_B): [
            _account(11, ENGINE_B, "", "Root", None),
            _account(12, ENGINE_B, "1.1.1", "Cash", "Asset", parent=11),
        ],
    }
    return FakePort(companies=companies, charts=charts, failure=failure)


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
    key_full_secret: str  # company:read + account:read, unrestricted
    key_company_secret: str  # company:read only, unrestricted
    key_account_secret: str  # account:read only, unrestricted
    key_restricted_a_secret: str  # both scopes, restricted to company A


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
            id=str(uuid.uuid4()), slug=f"rest-105-{uuid.uuid4().hex[:8]}", name="Tenant 1"
        )
        other_tenant = TenantModel(
            id=str(uuid.uuid4()), slug=f"rest-105-{uuid.uuid4().hex[:8]}", name="Tenant 2"
        )
        session.add_all([tenant, other_tenant])
        session.flush()
        client = ApiClientModel(
            id=str(uuid.uuid4()), tenant_id=tenant.id, name="rest-105 client"
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
                name="rest-105 key",
                scopes=scopes,
                installation_id=INSTALLATION_ID,
                **kwargs,  # type: ignore[arg-type]
            )

        key_full = _key(["company:read", "account:read"])
        key_company = _key(["company:read"])
        key_account = _key(["account:read"])
        key_restricted_a = _key(
            ["company:read", "account:read"], engine_company_id=company_a.id
        )
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
            key_company_secret=key_company.secret,
            key_account_secret=key_account.secret,
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
    app.dependency_overrides[get_company_query_service] = lambda: CompanyQueryService(port)
    app.dependency_overrides[get_account_query_service] = lambda: AccountQueryService(port)
    return TestClient(app, raise_server_exceptions=False)


def _auth(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


# ---------------------------------------------------------------------------
# GET /companies
# ---------------------------------------------------------------------------


def test_list_companies_returns_only_tenant_mapped_companies(
    settings: AppSettings, world: World
) -> None:
    client = _test_client(settings)
    response = client.get("/api/v1/companies", headers=_auth(world.key_full_secret))
    assert response.status_code == 200
    body = response.json()
    assert body["next_cursor"] is None
    names = [item["name"] for item in body["items"]]
    # Mapped companies, sorted by name (default sort).
    assert names == ["Acme Trading", "Beta Foods"]
    # The engine company with no mapping row in this tenant is invisible.
    assert "Ghost Holdings" not in names
    acme = body["items"][0]
    assert acme["id"] == world.company_a_id
    assert acme["engine_id"] == ENGINE_A
    assert acme["currency_code"] == "IRR"
    # No Tryton record or ORM internals leak into the response.
    assert set(acme) == {"id", "engine_id", "name", "currency_code"}


def test_list_companies_restricted_key_sees_only_its_company(
    settings: AppSettings, world: World
) -> None:
    client = _test_client(settings)
    response = client.get(
        "/api/v1/companies", headers=_auth(world.key_restricted_a_secret)
    )
    assert response.status_code == 200
    names = [item["name"] for item in response.json()["items"]]
    assert names == ["Acme Trading"]


def test_list_companies_paginates_with_cursor(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    headers = _auth(world.key_full_secret)
    first = client.get("/api/v1/companies?limit=1", headers=headers)
    assert first.status_code == 200
    assert [item["name"] for item in first.json()["items"]] == ["Acme Trading"]
    cursor = first.json()["next_cursor"]
    assert cursor  # opaque, non-empty
    second = client.get(f"/api/v1/companies?limit=1&cursor={cursor}", headers=headers)
    assert second.status_code == 200
    assert [item["name"] for item in second.json()["items"]] == ["Beta Foods"]
    assert second.json()["next_cursor"] is None


def test_list_companies_filters(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    headers = _auth(world.key_full_secret)
    by_name = client.get("/api/v1/companies?name=acme", headers=headers)
    assert [item["name"] for item in by_name.json()["items"]] == ["Acme Trading"]
    by_currency = client.get("/api/v1/companies?currency_code=USD", headers=headers)
    assert [item["name"] for item in by_currency.json()["items"]] == ["Beta Foods"]
    none = client.get("/api/v1/companies?name=zzz", headers=headers)
    assert none.json()["items"] == []
    assert none.json()["next_cursor"] is None


def test_list_companies_sorts(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    headers = _auth(world.key_full_secret)
    desc = client.get("/api/v1/companies?sort=-name", headers=headers)
    assert [item["name"] for item in desc.json()["items"]] == ["Beta Foods", "Acme Trading"]
    by_currency = client.get("/api/v1/companies?sort=currency_code", headers=headers)
    assert [item["name"] for item in by_currency.json()["items"]] == [
        "Acme Trading",  # IRR
        "Beta Foods",  # USD
    ]


def test_list_companies_rejects_unknown_sort_and_params(
    settings: AppSettings, world: World
) -> None:
    client = _test_client(settings)
    headers = _auth(world.key_full_secret)
    bad_sort = client.get("/api/v1/companies?sort=created_at", headers=headers)
    assert bad_sort.status_code == 400
    assert bad_sort.json()["code"] == "INVALID_SORT"
    bad_param = client.get("/api/v1/companies?curreny_code=IRR", headers=headers)
    assert bad_param.status_code == 400
    assert bad_param.json()["code"] == "INVALID_FILTER"
    assert "curreny_code" in bad_param.json()["detail"]
    bad_cursor = client.get("/api/v1/companies?cursor=not-a-cursor!!", headers=headers)
    assert bad_cursor.status_code == 400
    assert bad_cursor.json()["code"] == "INVALID_CURSOR"


# ---------------------------------------------------------------------------
# GET /companies/{company_id}
# ---------------------------------------------------------------------------


def test_get_company_by_row_id_and_by_engine_id(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    headers = _auth(world.key_full_secret)
    by_row = client.get(f"/api/v1/companies/{world.company_a_id}", headers=headers)
    assert by_row.status_code == 200
    assert by_row.json()["name"] == "Acme Trading"
    assert by_row.json()["engine_id"] == ENGINE_A
    by_engine = client.get(f"/api/v1/companies/{ENGINE_A}", headers=headers)
    assert by_engine.status_code == 200
    assert by_engine.json() == by_row.json()


def test_get_company_unknown_and_other_tenant_return_404(
    settings: AppSettings, world: World
) -> None:
    client = _test_client(settings)
    headers = _auth(world.key_full_secret)
    unknown = client.get(f"/api/v1/companies/{uuid.uuid4()}", headers=headers)
    assert unknown.status_code == 404
    assert unknown.json()["code"] == "COMPANY_NOT_FOUND"
    other = client.get(
        f"/api/v1/companies/{world.other_tenant_company_id}", headers=headers
    )
    assert other.status_code == 404
    assert other.json()["code"] == "COMPANY_NOT_FOUND"


def test_get_company_mapped_but_not_in_engine_returns_404(
    settings: AppSettings, world: World
) -> None:
    # The mapping row exists, but the engine has no such company: the
    # use case raises CompanyNotFoundError and the route maps it to 404.
    client = _test_client(settings)
    response = client.get(
        f"/api/v1/companies/{world.company_missing_id}",
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "COMPANY_NOT_FOUND"


def test_get_company_restricted_key_cannot_access_other_company(
    settings: AppSettings, world: World
) -> None:
    client = _test_client(settings)
    response = client.get(
        f"/api/v1/companies/{world.company_b_id}",
        headers=_auth(world.key_restricted_a_secret),
    )
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


# ---------------------------------------------------------------------------
# Auth: 401 / 403 on the new endpoints
# ---------------------------------------------------------------------------


def test_companies_require_authentication_and_scope(
    settings: AppSettings, world: World
) -> None:
    client = _test_client(settings)
    assert client.get("/api/v1/companies").status_code == 401
    assert client.get("/api/v1/companies").json()["code"] == "UNAUTHORIZED"
    # account:read key has no company:read scope.
    forbidden = client.get(
        "/api/v1/companies", headers=_auth(world.key_account_secret)
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "FORBIDDEN"
    assert "company:read" in forbidden.json()["detail"]


def test_accounts_require_authentication_and_scope(
    settings: AppSettings, world: World
) -> None:
    client = _test_client(settings)
    assert (
        client.get(f"/api/v1/accounting/accounts?company={world.company_a_id}").status_code
        == 401
    )
    # company:read key has no account:read scope.
    forbidden = client.get(
        f"/api/v1/accounting/accounts?company={world.company_a_id}",
        headers=_auth(world.key_company_secret),
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "FORBIDDEN"
    assert "account:read" in forbidden.json()["detail"]


# ---------------------------------------------------------------------------
# GET /accounting/accounts
# ---------------------------------------------------------------------------


def test_list_accounts_returns_scoped_chart(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get(
        f"/api/v1/accounting/accounts?company={world.company_a_id}",
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["next_cursor"] is None
    codes = [item["code"] for item in body["items"]]
    assert codes == ["", "1.1.1", "1.1.2", "2.1.1", "4.1.1", "4.1.2", "9.9.9"]
    cash = next(item for item in body["items"] if item["code"] == "1.1.1")
    assert cash["id"] == 2
    assert cash["company_id"] == world.company_a_id
    assert cash["name"] == "Cash"
    assert cash["type"] == "Asset"
    assert cash["parent_id"] == 1
    assert cash["closed"] is False
    assert cash["party_required"] is False
    assert cash["active"] is True
    assert cash["postable"] is True
    assert set(cash) == {
        "id",
        "company_id",
        "code",
        "name",
        "type",
        "parent_id",
        "closed",
        "party_required",
        "active",
        "postable",
    }
    # The chart root is a group: no type, not postable.
    root = body["items"][0]
    assert root["type"] is None
    assert root["postable"] is False


def test_list_accounts_accepts_engine_company_id(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get(
        f"/api/v1/accounting/accounts?company={ENGINE_B}",
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 200
    assert [item["code"] for item in response.json()["items"]] == ["", "1.1.1"]
    assert response.json()["items"][1]["company_id"] == world.company_b_id


def test_list_accounts_company_param_required_for_unrestricted_key(
    settings: AppSettings, world: World
) -> None:
    client = _test_client(settings)
    response = client.get(
        "/api/v1/accounting/accounts", headers=_auth(world.key_full_secret)
    )
    assert response.status_code == 400
    assert response.json()["code"] == "COMPANY_REQUIRED"


def test_list_accounts_restricted_key_defaults_to_its_company(
    settings: AppSettings, world: World
) -> None:
    client = _test_client(settings)
    response = client.get(
        "/api/v1/accounting/accounts", headers=_auth(world.key_restricted_a_secret)
    )
    assert response.status_code == 200
    assert [item["code"] for item in response.json()["items"]] == [
        "",
        "1.1.1",
        "1.1.2",
        "2.1.1",
        "4.1.1",
        "4.1.2",
        "9.9.9",
    ]


def test_list_accounts_unknown_company_returns_404(
    settings: AppSettings, world: World
) -> None:
    client = _test_client(settings)
    response = client.get(
        f"/api/v1/accounting/accounts?company={uuid.uuid4()}",
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "COMPANY_NOT_FOUND"


def test_list_accounts_restricted_key_cannot_request_other_company(
    settings: AppSettings, world: World
) -> None:
    client = _test_client(settings)
    response = client.get(
        f"/api/v1/accounting/accounts?company={world.company_b_id}",
        headers=_auth(world.key_restricted_a_secret),
    )
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


def test_list_accounts_filters(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    headers = _auth(world.key_full_secret)
    base = f"/api/v1/accounting/accounts?company={world.company_a_id}"
    by_code = client.get(f"{base}&code=1.1", headers=headers)
    assert [item["code"] for item in by_code.json()["items"]] == ["1.1.1", "1.1.2"]
    by_name = client.get(f"{base}&name=revenue", headers=headers)
    assert [item["code"] for item in by_name.json()["items"]] == ["4.1.1", "4.1.2"]
    by_type = client.get(f"{base}&type=Income", headers=headers)
    assert [item["code"] for item in by_type.json()["items"]] == ["4.1.1", "4.1.2"]
    inactive = client.get(f"{base}&active=false", headers=headers)
    assert [item["code"] for item in inactive.json()["items"]] == ["9.9.9"]
    closed = client.get(f"{base}&closed=true", headers=headers)
    assert [item["code"] for item in closed.json()["items"]] == ["4.1.2"]
    bad_filter = client.get(f"{base}&kol=1", headers=headers)
    assert bad_filter.status_code == 400
    assert bad_filter.json()["code"] == "INVALID_FILTER"


def test_list_accounts_sorts_and_paginates(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    headers = _auth(world.key_full_secret)
    base = f"/api/v1/accounting/accounts?company={world.company_a_id}"
    desc = client.get(f"{base}&sort=-code", headers=headers)
    assert [item["code"] for item in desc.json()["items"]] == [
        "9.9.9",
        "4.1.2",
        "4.1.1",
        "2.1.1",
        "1.1.2",
        "1.1.1",
        "",
    ]
    by_name = client.get(f"{base}&sort=name&limit=3", headers=headers)
    assert [item["name"] for item in by_name.json()["items"]] == [
        "Bank",
        "Cash",
        "Dormant",
    ]
    cursor = by_name.json()["next_cursor"]
    assert cursor
    rest = client.get(f"{base}&sort=name&limit=3&cursor={cursor}", headers=headers)
    assert [item["name"] for item in rest.json()["items"]] == [
        "Old Revenue",
        "Payables",
        "Root",
    ]
    cursor2 = rest.json()["next_cursor"]
    assert cursor2
    last = client.get(f"{base}&sort=name&limit=3&cursor={cursor2}", headers=headers)
    assert [item["name"] for item in last.json()["items"]] == ["Sales Revenue"]
    assert last.json()["next_cursor"] is None
    bad_sort = client.get(f"{base}&sort=parent", headers=headers)
    assert bad_sort.status_code == 400
    assert bad_sort.json()["code"] == "INVALID_SORT"


# ---------------------------------------------------------------------------
# GET /accounting/accounts/{account_id}
# ---------------------------------------------------------------------------


def test_get_account_returns_scoped_account(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get(
        f"/api/v1/accounting/accounts/2?company={world.company_a_id}",
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 200
    assert response.json()["code"] == "1.1.1"
    assert response.json()["name"] == "Cash"
    assert response.json()["company_id"] == world.company_a_id


def test_get_account_unknown_id_returns_typed_404(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get(
        f"/api/v1/accounting/accounts/999999999?company={world.company_a_id}",
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "ACCOUNT_NOT_FOUND"


def test_get_account_of_another_company_returns_404(
    settings: AppSettings, world: World
) -> None:
    # Account 12 belongs to company B; in company A's chart it looks
    # exactly like an account that does not exist.
    client = _test_client(settings)
    response = client.get(
        f"/api/v1/accounting/accounts/12?company={world.company_a_id}",
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "ACCOUNT_NOT_FOUND"


def test_get_account_without_company_param_uses_restriction(
    settings: AppSettings, world: World
) -> None:
    client = _test_client(settings)
    ok = client.get(
        "/api/v1/accounting/accounts/2",
        headers=_auth(world.key_restricted_a_secret),
    )
    assert ok.status_code == 200
    assert ok.json()["code"] == "1.1.1"
    missing = client.get(
        "/api/v1/accounting/accounts/2", headers=_auth(world.key_full_secret)
    )
    assert missing.status_code == 400
    assert missing.json()["code"] == "COMPANY_REQUIRED"


# ---------------------------------------------------------------------------
# Engine failures map to typed 502/503
# ---------------------------------------------------------------------------


def test_engine_unavailable_maps_to_503(settings: AppSettings, world: World) -> None:
    client = _test_client(
        settings, failure=EngineUnavailableError("engine down")
    )
    response = client.get("/api/v1/companies", headers=_auth(world.key_full_secret))
    assert response.status_code == 503
    assert response.json()["code"] == "ENGINE_UNAVAILABLE"


def test_engine_error_maps_to_502(settings: AppSettings, world: World) -> None:
    client = _test_client(settings, failure=EngineError("boom"))
    response = client.get(
        f"/api/v1/accounting/accounts?company={world.company_a_id}",
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 502
    assert response.json()["code"] == "ENGINE_ERROR"
    # The engine's own message must not leak into the response.
    assert "boom" not in response.text


# ---------------------------------------------------------------------------
# OpenAPI
# ---------------------------------------------------------------------------


def test_openapi_lists_all_four_endpoints(settings: AppSettings) -> None:
    client = _test_client(settings)
    document = client.get("/openapi.json").json()
    paths = document["paths"]
    for path in (
        "/api/v1/companies",
        "/api/v1/companies/{company_id}",
        "/api/v1/accounting/accounts",
        "/api/v1/accounting/accounts/{account_id}",
    ):
        assert path in paths, f"{path} missing from OpenAPI"
        operation = paths[path]["get"]
        assert {"HesabYar API key": []} in operation.get("security", [])
    accounts_params = {
        p["name"] for p in paths["/api/v1/accounting/accounts"]["get"]["parameters"]
    }
    assert {"company", "code", "name", "type", "active", "closed", "sort", "limit", "cursor"} <= accounts_params
