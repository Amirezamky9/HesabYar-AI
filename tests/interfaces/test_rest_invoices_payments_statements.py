"""Tests for the invoice/payment/statement REST endpoints (US-205).

The engine is faked in memory (``FakeDocumentEngine`` behind the real
services, injected via ``app.dependency_overrides``); everything else
is real — the FastAPI app, scoped API-key auth, the PostgreSQL database
(idempotency records + audit events), and the company-resolution
dependencies. The live Tryton server is NOT needed here; end-to-end
tests against the real server live in ``test_rest_phase2_e2e.py``.

Provisioned rows are committed (the app's per-request sessions must see
them) and removed in teardown by deleting the tenant (cascades to
clients/keys/companies); idempotency records carry no tenant FK, so
they are deleted by installation id.
"""

from __future__ import annotations

import dataclasses
import datetime
import os
import uuid
from collections.abc import Generator, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from hesabyar.application.commands.invoices import InvoiceDraftService
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
    StatementJournalRef,
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
    InvalidBankStatementError,
    InvalidInvoiceError,
    InvalidPaymentError,
    InvoiceNotFoundError,
)
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.infrastructure.auth import ApiKeyCrypto, ApiKeyService
from hesabyar.infrastructure.config import AppSettings
from hesabyar.infrastructure.persistence.postgres.models import (
    ApiClientModel,
    AuditEventModel,
    Base,
    EngineCompanyModel,
    EngineConnectionModel,
    IdempotencyRecordModel,
    TenantModel,
)
from hesabyar.interfaces.rest.app import create_app
from hesabyar.interfaces.rest.dependencies import (
    get_bank_statement_query_service,
    get_engine_adapter,
    get_invoice_draft_service,
    get_payment_query_service,
)

TEST_DB_URL = os.getenv(
    "HESABYAR_DATABASE_URL",
    "postgresql+psycopg://hesabyar:hesabyar_pass@hesabyar-postgres:5432/hesabyar_test",
)

INSTALLATION_ID = "00000000-0000-0000-0000-000000000007"

# Fake engine ids. 101/102 are mapped to this tenant; 103 exists in the
# engine but has NO mapping row (documents of it must stay invisible).
ENGINE_A = 101
ENGINE_B = 102
ENGINE_UNMAPPED = 103

PARTY_ID = 201
RECEIVABLE_ID = 301
REVENUE_ID = 401
MISSING_ID = 999_999_999

ALL_SCOPES = [
    "invoice:read",
    "invoice:draft:create",
    "payment:read",
    "banking:read",
    "banking:reconcile",
]


def _date(day: int) -> FiscalDate:
    return FiscalDate.from_gregorian(datetime.date(2026, 11, day))


def _invoice_view(
    invoice_id: int,
    company_id: int,
    invoice_type: str = "out",
    state: str = "draft",
    day: int = 17,
    total: int = 750_000,
) -> InvoiceView:
    return InvoiceView(
        ref=InvoiceRef(invoice_id),
        company=CompanyRef(company_id),
        invoice_type=invoice_type,
        party=PartyRef(PARTY_ID),
        party_name="Acme Customer",
        number=None,
        invoice_date=_date(day),
        accounting_date=None,
        state=state,
        description="test invoice",
        reference="",
        journal=JournalRef(7),
        account=AccountRef(RECEIVABLE_ID),
        currency_code="IRR",
        untaxed_amount=MoneyIRR(total),
        tax_amount=MoneyIRR(0),
        total_amount=MoneyIRR(total),
        lines=(
            InvoiceLineView(
                account=AccountRef(REVENUE_ID),
                quantity=Decimal(1),
                unit_price=MoneyIRR(total),
                amount=MoneyIRR(total),
            ),
        ),
    )


def _payment_view(
    payment_id: int,
    company_id: int,
    kind: str = "receivable",
    state: str = "draft",
    day: int = 17,
) -> PaymentView:
    return PaymentView(
        ref=PaymentRef(payment_id),
        company=CompanyRef(company_id),
        number=f"PAY-{payment_id}",
        kind=kind,
        party=PartyRef(PARTY_ID),
        party_name="Acme Customer",
        date=_date(day),
        amount=MoneyIRR(500_000),
        state=state,
        reference="",
        journal_name="Manual Payments",
        currency_code="IRR",
    )


def _statement_view(
    statement_id: int,
    company_id: int,
    state: str = "draft",
    day: int = 17,
    matched: bool = False,
) -> BankStatementView:
    return BankStatementView(
        ref=BankStatementRef(statement_id),
        company=CompanyRef(company_id),
        name=f"Statement {statement_id}",
        journal_name="Bank",
        journal_id=700,
        date=_date(day),
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
                date=_date(day),
                amount=MoneyIRR(1_000_000),
                account=AccountRef(RECEIVABLE_ID),
                description="receipt",
                related_invoice=InvoiceRef(1) if matched else None,
            ),
            BankStatementLineView(
                id=9002,
                date=_date(day),
                amount=MoneyIRR(-250_000),
                account=AccountRef(501),
                description="payment",
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Fake document engine
# ---------------------------------------------------------------------------


class FakeDocumentEngine:
    """In-memory invoice/payment/statement engine for endpoint tests."""

    def __init__(self) -> None:
        self._invoices: dict[int, InvoiceView] = {}
        self._payments: dict[int, PaymentView] = {}
        self._statements: dict[int, BankStatementView] = {}
        self._next_id = 1
        self.create_calls = 0
        self.invalid_invoices: set[int] = set()

    # -- seeding -----------------------------------------------------------

    def seed_invoice(self, company_id: int = ENGINE_A, **kwargs) -> int:
        invoice_id = self._next_id
        self._next_id += 1
        self._invoices[invoice_id] = _invoice_view(invoice_id, company_id, **kwargs)
        return invoice_id

    def seed_payment(self, company_id: int = ENGINE_A, **kwargs) -> int:
        payment_id = self._next_id
        self._next_id += 1
        self._payments[payment_id] = _payment_view(payment_id, company_id, **kwargs)
        return payment_id

    def seed_statement(self, company_id: int = ENGINE_A, **kwargs) -> int:
        statement_id = self._next_id
        self._next_id += 1
        self._statements[statement_id] = _statement_view(
            statement_id, company_id, **kwargs
        )
        return statement_id

    # -- port: companies / journals (stubs) --------------------------------

    def list_companies(self) -> Sequence[CompanyView]:
        return [
            CompanyView(ref=CompanyRef(ENGINE_A), name="Acme", currency_code="IRR"),
            CompanyView(ref=CompanyRef(ENGINE_B), name="Beta", currency_code="IRR"),
            CompanyView(
                ref=CompanyRef(ENGINE_UNMAPPED), name="Ghost", currency_code="IRR"
            ),
        ]

    def get_chart_of_accounts(self, company: CompanyRef) -> Sequence[AccountView]:
        return []

    def create_journal_draft(self, command: CreateJournalDraft) -> JournalDraftRef:
        raise NotImplementedError("document endpoints never create journals")

    def get_journal_draft(self, draft: JournalDraftRef) -> JournalDraftView | None:
        raise NotImplementedError("document endpoints never read journal drafts")

    def list_journal_drafts(self, company: CompanyRef) -> Sequence[JournalDraftView]:
        raise NotImplementedError("document endpoints never list journal drafts")

    def update_journal_draft(self, command: UpdateJournalDraft) -> JournalDraftView:
        raise NotImplementedError("document endpoints never update journal drafts")

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
        raise NotImplementedError("document endpoints never validate journals")

    def post_journal(self, command: PostJournal) -> PostedJournalRef:
        raise NotImplementedError("document endpoints never post journals")

    def reverse_journal(self, command: ReverseJournal) -> PostedJournalRef:
        raise NotImplementedError("document endpoints never reverse journals")

    def get_journal(self, posted: PostedJournalRef) -> PostedJournalView | None:
        raise NotImplementedError("document endpoints never read journals")

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
        self.create_calls += 1
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
            journal=command.journal or JournalRef(7),
            account=command.account or AccountRef(RECEIVABLE_ID),
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

    def create_bank_statement(self, command: CreateBankStatement) -> BankStatementRef:
        raise NotImplementedError("this fake never creates statements")

    def list_statement_journals(
        self, company: CompanyRef
    ) -> Sequence[StatementJournalView]:
        return [
            StatementJournalView(
                ref=StatementJournalRef(700),
                company=company,
                name="Bank",
                account=AccountRef(100),
                validation="balance",
                currency_code="IRR",
                bank_account_number="IR062060000000100324200001",
            )
        ]

    def update_bank_statement_line(self, command: UpdateBankStatementLine) -> None:
        view = self._statements[command.statement.engine_id]
        lines = []
        for line in view.lines:
            if line.id != command.line.engine_id:
                lines.append(line)
                continue
            lines.append(
                replace(
                    line,
                    account=command.account if command.account is not None else line.account,
                    party=command.party if command.party is not None else line.party,
                    related_invoice=(
                        None
                        if command.clear_related_invoice
                        else (
                            command.related_invoice
                            if command.related_invoice is not None
                            else line.related_invoice
                        )
                    ),
                )
            )
        self._statements[command.statement.engine_id] = replace(view, lines=tuple(lines))

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


# ---------------------------------------------------------------------------
# Provisioned world (real DB, real keys)
# ---------------------------------------------------------------------------


@dataclass
class World:
    """Provisioned tenant, companies and keys for one test."""

    tenant_id: str
    company_a_id: str  # engine_companies row id for engine 101
    company_b_id: str  # engine_companies row id for engine 102
    client_id: str
    key_full_secret: str  # all invoice/payment/banking scopes, unrestricted
    key_invoice_read_secret: str  # invoice:read only
    key_banking_read_secret: str  # banking:read only
    key_restricted_a_secret: str  # all scopes, restricted to company A


@pytest.fixture(scope="module")
def engine() -> Generator[Engine, None, None]:
    eng = create_engine(TEST_DB_URL, echo=False)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def settings() -> AppSettings:
    return AppSettings()


@pytest.fixture()
def fake_engine() -> FakeDocumentEngine:
    return FakeDocumentEngine()


@pytest.fixture()
def world(
    engine: Engine, settings: AppSettings
) -> Generator[World, None, None]:
    """Provision a tenant mapped to fake engine companies 101 + 102."""
    session = sessionmaker(bind=engine)()
    service = ApiKeyService(ApiKeyCrypto(settings.auth.api_key_pepper))
    tenant_id = ""
    connection_ids: list[str] = []
    try:
        tenant = TenantModel(
            id=str(uuid.uuid4()), slug=f"rest-205-{uuid.uuid4().hex[:8]}", name="205 Tenant"
        )
        session.add(tenant)
        session.flush()
        client = ApiClientModel(
            id=str(uuid.uuid4()), tenant_id=tenant.id, name="rest-205 client"
        )
        session.add(client)
        session.flush()

        def _company(external_id: str) -> EngineCompanyModel:
            # One company per (tenant, connection) — unique constraint.
            connection = EngineConnectionModel(
                id=str(uuid.uuid4()),
                name=f"conn-{uuid.uuid4().hex[:8]}",
                engine="tryton",
                base_url="http://localhost:8000",
                database_name=f"hesabyar_tryton_test_{uuid.uuid4().hex[:8]}",
            )
            session.add(connection)
            session.flush()
            connection_ids.append(connection.id)
            row = EngineCompanyModel(
                id=str(uuid.uuid4()),
                tenant_id=tenant.id,
                engine_connection_id=connection.id,
                external_company_id=external_id,
                company_name=f"Company {external_id}",
            )
            session.add(row)
            session.flush()
            return row

        row_a = _company(str(ENGINE_A))
        row_b = _company(str(ENGINE_B))

        def _key(scopes: list[str], company_id: str | None = None):
            return service.issue_key(
                session,
                client=client,
                name="rest-205 key",
                scopes=scopes,
                installation_id=INSTALLATION_ID,
                engine_company_id=company_id,
            )

        key_full = _key(ALL_SCOPES)
        key_invoice_read = _key(["invoice:read"])
        key_banking_read = _key(["banking:read"])
        key_restricted_a = _key(ALL_SCOPES, company_id=row_a.id)
        session.commit()
        tenant_id = tenant.id
        yield World(
            tenant_id=tenant.id,
            company_a_id=row_a.id,
            company_b_id=row_b.id,
            client_id=client.id,
            key_full_secret=key_full.secret,
            key_invoice_read_secret=key_invoice_read.secret,
            key_banking_read_secret=key_banking_read.secret,
            key_restricted_a_secret=key_restricted_a.secret,
        )
    finally:
        session.close()
        cleanup = sessionmaker(bind=engine)()
        try:
            cleanup.execute(
                delete(IdempotencyRecordModel).where(
                    IdempotencyRecordModel.installation_id == INSTALLATION_ID
                )
            )
            if tenant_id:
                cleanup.query(TenantModel).filter(
                    TenantModel.id == tenant_id
                ).delete(synchronize_session=False)
            for connection_id in connection_ids:
                cleanup.query(EngineConnectionModel).filter(
                    EngineConnectionModel.id == connection_id
                ).delete(synchronize_session=False)
            cleanup.commit()
        finally:
            cleanup.close()


@pytest.fixture()
def api(
    settings: AppSettings, fake_engine: FakeDocumentEngine
) -> Generator[TestClient, None, None]:
    """The real app with the engine faked (auth + DB are real)."""
    app = create_app(settings=settings)
    app.dependency_overrides[get_invoice_draft_service] = lambda: InvoiceDraftService(
        fake_engine
    )
    app.dependency_overrides[get_payment_query_service] = lambda: PaymentQueryService(
        fake_engine
    )
    app.dependency_overrides[get_bank_statement_query_service] = (
        lambda: BankStatementQueryService(fake_engine)
    )
    # The bank services (import/reconcile/query) resolve the engine
    # through get_engine_adapter — point it at the fake too.
    app.dependency_overrides[get_engine_adapter] = lambda: fake_engine
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def _auth(secret: str, key: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {secret}"}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _invoice_body(world: World, **overrides) -> dict:
    body = {
        "company_id": world.company_a_id,
        "invoice_type": "out",
        "party_id": PARTY_ID,
        "date": "2026-11-17",
        "description": "US-205 test invoice",
        "lines": [
            {"account_id": REVENUE_ID, "unit_price": 500_000, "quantity": 1},
            {"account_id": 402, "unit_price": 125_000, "quantity": 2},
        ],
    }
    body.update(overrides)
    return body


def _audit_events(engine: Engine, tenant_id: str) -> list[AuditEventModel]:
    session = sessionmaker(bind=engine)()
    try:
        return list(
            session.query(AuditEventModel)
            .filter(AuditEventModel.tenant_id == tenant_id)
            .order_by(AuditEventModel.occurred_at)
            .all()
        )
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Invoices: list
# ---------------------------------------------------------------------------


def test_list_invoices_returns_company_invoices_newest_first(
    api: TestClient, world: World, fake_engine: FakeDocumentEngine
) -> None:
    first = fake_engine.seed_invoice(day=15)
    second = fake_engine.seed_invoice(day=17)
    fake_engine.seed_invoice(company_id=ENGINE_B, day=18)

    response = api.get(
        f"/api/v1/invoices?company={world.company_a_id}",
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["id"] for item in body["items"]] == [second, first]
    assert body["next_cursor"] is None
    assert body["items"][0]["company_id"] == world.company_a_id
    assert body["items"][0]["currency"] == "IRR"
    assert body["items"][0]["unit"] == "rial"


def test_list_invoices_filters_by_type_and_state(
    api: TestClient, world: World, fake_engine: FakeDocumentEngine
) -> None:
    fake_engine.seed_invoice(invoice_type="out", state="draft")
    fake_engine.seed_invoice(invoice_type="in", state="draft")
    fake_engine.seed_invoice(invoice_type="out", state="posted")

    headers = _auth(world.key_full_secret)
    base = f"/api/v1/invoices?company={world.company_a_id}"
    assert len(api.get(f"{base}&type=out", headers=headers).json()["items"]) == 2
    assert len(api.get(f"{base}&state=draft", headers=headers).json()["items"]) == 2
    assert (
        len(api.get(f"{base}&type=in&state=draft", headers=headers).json()["items"])
        == 1
    )


def test_list_invoices_invalid_filter_is_400(
    api: TestClient, world: World
) -> None:
    headers = _auth(world.key_full_secret)
    base = f"/api/v1/invoices?company={world.company_a_id}"
    response = api.get(f"{base}&type=sideways", headers=headers)
    assert response.status_code == 400
    assert response.json()["code"] == "LEDGER_INVALID_INVOICE"
    response = api.get(f"{base}&state=limbo", headers=headers)
    assert response.status_code == 400
    assert response.json()["code"] == "LEDGER_INVALID_INVOICE"


def test_list_invoices_cursor_pagination_walks_all(
    api: TestClient, world: World, fake_engine: FakeDocumentEngine
) -> None:
    ids = [fake_engine.seed_invoice(day=day) for day in (11, 12, 13, 14, 15)]
    headers = _auth(world.key_full_secret)
    seen: list[int] = []
    cursor: str | None = None
    for _ in range(5):
        url = f"/api/v1/invoices?company={world.company_a_id}&limit=2"
        if cursor is not None:
            url += f"&cursor={cursor}"
        response = api.get(url, headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        seen.extend(item["id"] for item in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert sorted(seen) == sorted(ids)
    assert len(seen) == len(set(seen))  # no duplicates across pages


def test_list_invoices_rejects_unknown_params_and_bad_cursor(
    api: TestClient, world: World
) -> None:
    headers = _auth(world.key_full_secret)
    base = f"/api/v1/invoices?company={world.company_a_id}"
    response = api.get(f"{base}&bogus=1", headers=headers)
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_FILTER"
    response = api.get(f"{base}&sort=bogus", headers=headers)
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_SORT"
    response = api.get(f"{base}&cursor=not-a-cursor", headers=headers)
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_CURSOR"


def test_list_invoices_company_scoping(
    api: TestClient, world: World, fake_engine: FakeDocumentEngine
) -> None:
    fake_engine.seed_invoice()
    # Unrestricted key + no company param → 400 COMPANY_REQUIRED.
    response = api.get("/api/v1/invoices", headers=_auth(world.key_full_secret))
    assert response.status_code == 400
    assert response.json()["code"] == "COMPANY_REQUIRED"
    # Unknown company → 404.
    response = api.get(
        "/api/v1/invoices?company=nope", headers=_auth(world.key_full_secret)
    )
    assert response.status_code == 404
    assert response.json()["code"] == "COMPANY_NOT_FOUND"
    # Restricted key without a param uses its own company.
    response = api.get(
        "/api/v1/invoices", headers=_auth(world.key_restricted_a_secret)
    )
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
    # Restricted key asking for another company → 403.
    response = api.get(
        f"/api/v1/invoices?company={world.company_b_id}",
        headers=_auth(world.key_restricted_a_secret),
    )
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


def test_list_invoices_auth_enforced(api: TestClient, world: World) -> None:
    response = api.get(f"/api/v1/invoices?company={world.company_a_id}")
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"


# ---------------------------------------------------------------------------
# Invoices: get
# ---------------------------------------------------------------------------


def test_get_invoice_returns_invoice(
    api: TestClient, world: World, fake_engine: FakeDocumentEngine
) -> None:
    invoice_id = fake_engine.seed_invoice()
    response = api.get(
        f"/api/v1/invoices/{invoice_id}", headers=_auth(world.key_full_secret)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == invoice_id
    assert body["company_id"] == world.company_a_id
    assert body["engine_company_id"] == ENGINE_A
    assert body["invoice_type"] == "out"
    assert body["party_id"] == PARTY_ID
    assert body["total_amount"] == 750_000
    # Quantities serialize as decimal strings (precision-safe, spec §9.3).
    assert body["lines"][0]["quantity"] == "1"


def test_get_invoice_missing_and_invisible_are_404(
    api: TestClient, world: World, fake_engine: FakeDocumentEngine
) -> None:
    headers = _auth(world.key_full_secret)
    response = api.get(f"/api/v1/invoices/{MISSING_ID}", headers=headers)
    assert response.status_code == 404
    assert response.json()["code"] == "LEDGER_INVOICE_NOT_FOUND"
    # An invoice of the unmapped company looks exactly like a missing one.
    ghost = fake_engine.seed_invoice(company_id=ENGINE_UNMAPPED)
    response = api.get(f"/api/v1/invoices/{ghost}", headers=headers)
    assert response.status_code == 404
    assert response.json()["code"] == "LEDGER_INVOICE_NOT_FOUND"


def test_get_invoice_restricted_key_other_company_is_403(
    api: TestClient, world: World, fake_engine: FakeDocumentEngine
) -> None:
    invoice_id = fake_engine.seed_invoice(company_id=ENGINE_B)
    response = api.get(
        f"/api/v1/invoices/{invoice_id}",
        headers=_auth(world.key_restricted_a_secret),
    )
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


# ---------------------------------------------------------------------------
# Invoices: create draft
# ---------------------------------------------------------------------------


def test_create_invoice_draft_201_and_audited(
    api: TestClient,
    world: World,
    fake_engine: FakeDocumentEngine,
    engine: Engine,
) -> None:
    response = api.post(
        "/api/v1/invoices/drafts",
        json=_invoice_body(world),
        headers=_auth(world.key_full_secret, "inv-create-1"),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["state"] == "draft"
    assert body["company_id"] == world.company_a_id
    assert body["total_amount"] == 750_000
    assert body["untaxed_amount"] == 750_000
    assert body["tax_amount"] == 0
    assert len(body["lines"]) == 2
    assert fake_engine.create_calls == 1

    events = _audit_events(engine, world.tenant_id)
    assert len(events) == 1
    assert events[0].action == "invoice.draft.create"
    assert events[0].outcome == "success"
    assert events[0].resource_type == "invoice_draft"
    assert events[0].resource_id == str(body["id"])
    assert events[0].company_external_id == str(ENGINE_A)
    assert events[0].tryton_reference == f"account.invoice:{body['id']}"
    assert events[0].metadata_after is not None
    assert events[0].metadata_after["idempotency_key"] == "inv-create-1"
    assert events[0].metadata_after["total_amount"] == 750_000


def test_create_invoice_draft_replay_does_not_create_twice(
    api: TestClient, world: World, fake_engine: FakeDocumentEngine
) -> None:
    headers = _auth(world.key_full_secret, "inv-replay")
    first = api.post(
        "/api/v1/invoices/drafts", json=_invoice_body(world), headers=headers
    )
    assert first.status_code == 201
    second = api.post(
        "/api/v1/invoices/drafts", json=_invoice_body(world), headers=headers
    )
    assert second.status_code == 201
    assert second.json() == first.json()
    assert fake_engine.create_calls == 1


def test_create_invoice_draft_conflict_and_missing_key(
    api: TestClient, world: World, fake_engine: FakeDocumentEngine
) -> None:
    headers = _auth(world.key_full_secret, "inv-conflict")
    first = api.post(
        "/api/v1/invoices/drafts", json=_invoice_body(world), headers=headers
    )
    assert first.status_code == 201
    changed = _invoice_body(world, description="different")
    second = api.post("/api/v1/invoices/drafts", json=changed, headers=headers)
    assert second.status_code == 409
    assert second.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert fake_engine.create_calls == 1

    no_key = api.post(
        "/api/v1/invoices/drafts",
        json=_invoice_body(world),
        headers=_auth(world.key_full_secret),
    )
    assert no_key.status_code == 400
    assert no_key.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_create_invoice_draft_invalid_payload_audits_error_and_releases_key(
    api: TestClient,
    world: World,
    fake_engine: FakeDocumentEngine,
    engine: Engine,
) -> None:
    headers = _auth(world.key_full_secret, "inv-invalid")
    response = api.post(
        "/api/v1/invoices/drafts",
        json=_invoice_body(world, invoice_type="sideways"),
        headers=headers,
    )
    assert response.status_code == 400
    assert response.json()["code"] == "LEDGER_INVALID_INVOICE"

    events = _audit_events(engine, world.tenant_id)
    assert len(events) == 1
    assert events[0].action == "invoice.draft.create"
    assert events[0].outcome == "error"
    assert events[0].error_code == "LEDGER_INVALID_INVOICE"

    # The failed claim was released: the same key works with a fixed payload.
    retry = api.post(
        "/api/v1/invoices/drafts", json=_invoice_body(world), headers=headers
    )
    assert retry.status_code == 201
    assert fake_engine.create_calls == 1


def test_create_invoice_draft_company_and_scope_checks(
    api: TestClient, world: World
) -> None:
    headers = _auth(world.key_full_secret, "inv-company")
    response = api.post(
        "/api/v1/invoices/drafts",
        json=_invoice_body(world, company_id="nope"),
        headers=headers,
    )
    assert response.status_code == 404
    assert response.json()["code"] == "COMPANY_NOT_FOUND"

    # A read-only key cannot create drafts.
    response = api.post(
        "/api/v1/invoices/drafts",
        json=_invoice_body(world),
        headers=_auth(world.key_invoice_read_secret, "inv-scope"),
    )
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"
    assert "invoice:draft:create" in response.json()["detail"]

    # No key at all → 401.
    response = api.post(
        "/api/v1/invoices/drafts",
        json=_invoice_body(world),
        headers={"Idempotency-Key": "inv-nokey"},
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Invoices: validate
# ---------------------------------------------------------------------------


def test_validate_invoice_draft_valid_and_invalid(
    api: TestClient, world: World, fake_engine: FakeDocumentEngine
) -> None:
    good = fake_engine.seed_invoice()
    bad = fake_engine.seed_invoice()
    fake_engine.invalid_invoices.add(bad)
    headers = _auth(world.key_full_secret)

    response = api.post(f"/api/v1/invoices/{good}/validate", headers=headers)
    assert response.status_code == 200
    assert response.json() == {"valid": True, "issues": []}

    response = api.post(f"/api/v1/invoices/{bad}/validate", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert body["issues"][0]["code"] == "LEDGER_INVALID_INVOICE"


def test_validate_invoice_draft_missing_and_scope(
    api: TestClient, world: World, fake_engine: FakeDocumentEngine
) -> None:
    response = api.post(
        f"/api/v1/invoices/{MISSING_ID}/validate",
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "LEDGER_INVOICE_NOT_FOUND"

    # Validation needs the draft scope, not just invoice:read.
    invoice_id = fake_engine.seed_invoice()
    response = api.post(
        f"/api/v1/invoices/{invoice_id}/validate",
        headers=_auth(world.key_invoice_read_secret),
    )
    assert response.status_code == 403
    assert "invoice:draft:create" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------


def test_list_payments_filters_and_invalid_filter(
    api: TestClient, world: World, fake_engine: FakeDocumentEngine
) -> None:
    fake_engine.seed_payment(kind="receivable", state="draft")
    fake_engine.seed_payment(kind="payable", state="draft")
    fake_engine.seed_payment(kind="receivable", state="succeeded")

    headers = _auth(world.key_full_secret)
    base = f"/api/v1/payments?company={world.company_a_id}"
    assert len(api.get(base, headers=headers).json()["items"]) == 3
    assert len(api.get(f"{base}&kind=payable", headers=headers).json()["items"]) == 1
    assert len(api.get(f"{base}&state=draft", headers=headers).json()["items"]) == 2
    response = api.get(f"{base}&kind=sideways", headers=headers)
    assert response.status_code == 400
    assert response.json()["code"] == "LEDGER_INVALID_PAYMENT"
    response = api.get(f"{base}&state=limbo", headers=headers)
    assert response.status_code == 400
    assert response.json()["code"] == "LEDGER_INVALID_PAYMENT"


def test_get_payment_returns_payment_and_404s(
    api: TestClient, world: World, fake_engine: FakeDocumentEngine
) -> None:
    payment_id = fake_engine.seed_payment()
    response = api.get(
        f"/api/v1/payments/{payment_id}", headers=_auth(world.key_full_secret)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == payment_id
    assert body["number"] == f"PAY-{payment_id}"
    assert body["amount"] == 500_000
    assert body["kind"] == "receivable"

    response = api.get(
        f"/api/v1/payments/{MISSING_ID}", headers=_auth(world.key_full_secret)
    )
    assert response.status_code == 404
    assert response.json()["code"] == "LEDGER_PAYMENT_NOT_FOUND"

    ghost = fake_engine.seed_payment(company_id=ENGINE_UNMAPPED)
    response = api.get(
        f"/api/v1/payments/{ghost}", headers=_auth(world.key_full_secret)
    )
    assert response.status_code == 404
    assert response.json()["code"] == "LEDGER_PAYMENT_NOT_FOUND"


def test_payments_require_payment_read_scope(
    api: TestClient, world: World
) -> None:
    response = api.get(
        f"/api/v1/payments?company={world.company_a_id}",
        headers=_auth(world.key_invoice_read_secret),
    )
    assert response.status_code == 403
    assert "payment:read" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Bank statements
# ---------------------------------------------------------------------------


def test_list_statements_filters_and_invalid_filter(
    api: TestClient, world: World, fake_engine: FakeDocumentEngine
) -> None:
    fake_engine.seed_statement(state="draft")
    fake_engine.seed_statement(state="posted")

    headers = _auth(world.key_full_secret)
    base = f"/api/v1/bank/statements?company={world.company_a_id}"
    assert len(api.get(base, headers=headers).json()["items"]) == 2
    assert len(api.get(f"{base}&state=draft", headers=headers).json()["items"]) == 1
    response = api.get(f"{base}&state=limbo", headers=headers)
    assert response.status_code == 400
    assert response.json()["code"] == "LEDGER_INVALID_STATEMENT"


def test_get_statement_returns_statement_with_signed_lines(
    api: TestClient, world: World, fake_engine: FakeDocumentEngine
) -> None:
    statement_id = fake_engine.seed_statement(matched=True)
    response = api.get(
        f"/api/v1/bank/statements/{statement_id}",
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == statement_id
    assert body["validation"] == "balance"
    assert body["start_balance"] == 0
    assert body["end_balance"] == 750_000
    assert body["total_amount"] is None  # balance journals carry no total amount
    assert body["lines"][0]["amount"] == 1_000_000
    assert body["lines"][0]["related_invoice_id"] == 1
    assert body["lines"][1]["amount"] == -250_000
    assert body["lines"][1]["related_invoice_id"] is None

    response = api.get(
        f"/api/v1/bank/statements/{MISSING_ID}", headers=_auth(world.key_full_secret)
    )
    assert response.status_code == 404
    assert response.json()["code"] == "LEDGER_STATEMENT_NOT_FOUND"


def test_import_status_counts_states_and_matches(
    api: TestClient, world: World, fake_engine: FakeDocumentEngine
) -> None:
    fake_engine.seed_statement(state="draft", matched=True)
    fake_engine.seed_statement(state="posted", matched=False)
    fake_engine.seed_statement(state="draft", company_id=ENGINE_B)

    response = api.get(
        f"/api/v1/bank/statements/import-status?company={world.company_a_id}",
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["company_id"] == world.company_a_id
    assert body["total_statements"] == 2
    assert body["draft_count"] == 1
    assert body["posted_count"] == 1
    assert body["validated_count"] == 0
    assert body["cancelled_count"] == 0
    assert body["latest_statement_id"] is not None
    assert body["total_lines"] == 4
    assert body["matched_lines"] == 1
    assert body["unmatched_lines"] == 3


def test_import_status_empty_company_is_zeros(
    api: TestClient, world: World
) -> None:
    response = api.get(
        f"/api/v1/bank/statements/import-status?company={world.company_a_id}",
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total_statements"] == 0
    assert body["latest_statement_id"] is None
    assert body["latest_statement_date"] is None
    assert body["total_lines"] == 0


def test_reconcile_runs_with_contract_enforced(
    api: TestClient, world: World, fake_engine: FakeDocumentEngine
) -> None:
    statement_id = fake_engine.seed_statement()
    invoice_id = fake_engine.seed_invoice(state="posted", total=1_000_000, day=17)

    # Without an Idempotency-Key the endpoint refuses.
    response = api.post(
        f"/api/v1/bank/statements/{statement_id}/reconcile",
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 400
    assert response.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    # With a key: 200 with the reconcile result — the +1,000,000 line
    # P2-matches the posted invoice; the −250,000 line has no
    # candidate and files a bank_unmatched review item.
    response = api.post(
        f"/api/v1/bank/statements/{statement_id}/reconcile",
        headers=_auth(world.key_full_secret, "recon-1"),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["statement_id"] == statement_id
    assert body["total_lines"] == 2
    assert body["matched_count"] == 1
    assert body["priority2_count"] == 1
    assert body["unmatched_count"] == 1
    assert body["review_items_filed"] == 1
    assert body["lines"][0]["outcome"] == "matched"
    assert body["lines"][0]["target_id"] == invoice_id
    assert body["lines"][1]["outcome"] == "unmatched"
    assert body["lines"][1]["review_item_id"] is not None
    statement = fake_engine.get_bank_statement(BankStatementRef(statement_id))
    assert statement is not None
    assert statement.lines[0].related_invoice == InvoiceRef(invoice_id)

    # Unknown statement → 404.
    response = api.post(
        f"/api/v1/bank/statements/{MISSING_ID}/reconcile",
        headers=_auth(world.key_full_secret, "recon-2"),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "LEDGER_STATEMENT_NOT_FOUND"

    # A banking:read key lacks the reconcile scope.
    response = api.post(
        f"/api/v1/bank/statements/{statement_id}/reconcile",
        headers=_auth(world.key_banking_read_secret, "recon-3"),
    )
    assert response.status_code == 403
    assert "banking:reconcile" in response.json()["detail"]
