"""Tests for the journal draft + posted-journal REST endpoints (US-202).

The engine is faked in memory (``FakeJournalEngine`` behind the real
``JournalDraftService``, injected via ``app.dependency_overrides``);
everything else is real — the FastAPI app, scoped API-key auth, the
PostgreSQL database (idempotency records + audit events), and the
company-resolution dependencies. The live Tryton server is NOT needed
here; end-to-end tests against the real server live in
``test_rest_journal_drafts_e2e.py``.

Provisioned rows are committed (the app's per-request sessions must see
them) and removed in teardown by deleting the tenant (cascades to
clients/keys/companies); idempotency records carry no tenant FK, so
they are deleted by installation id.
"""

from __future__ import annotations

import datetime
import os
import uuid
from collections.abc import Generator, Sequence
from dataclasses import dataclass
from decimal import (
    Decimal,
)

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

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
from hesabyar.interfaces.rest.dependencies import get_journal_draft_service

TEST_DB_URL = os.getenv(
    "HESABYAR_DATABASE_URL",
    "postgresql+psycopg://hesabyar:hesabyar_pass@hesabyar-postgres:5432/hesabyar_test",
)

INSTALLATION_ID = "00000000-0000-0000-0000-000000000005"

# Fake engine ids. 101/102 are mapped to this tenant; 103 exists in the
# engine but has NO mapping row (drafts of it must stay invisible).
ENGINE_A = 101
ENGINE_B = 102
ENGINE_UNMAPPED = 103

MOVE_DATE = "2026-11-16"
MISSING_ID = 999_999_999

PERIOD = FiscalPeriodView(
    name="Nov 2026",
    start_date=FiscalDate.from_gregorian(datetime.date(2026, 11, 1)),
    end_date=FiscalDate.from_gregorian(datetime.date(2026, 11, 30)),
    state="open",
)


# ---------------------------------------------------------------------------
# Fake journal engine
# ---------------------------------------------------------------------------


@dataclass
class _Move:
    view: JournalDraftView
    state: str = "draft"
    origin: PostedJournalRef | None = None
    number: str = ""


class FakeJournalEngine:
    """In-memory journal engine for endpoint tests (no server)."""

    def __init__(self) -> None:
        self._moves: dict[int, _Move] = {}
        self._next_id = 1
        self.create_calls = 0
        self.post_calls = 0
        self.reverse_calls = 0
        self.invalid_drafts: set[int] = set()
        self.fail_post_once: HesabYarError | None = None

    def seed_draft(self, company_id: int) -> int:
        """Insert a draft directly, bypassing the service (for scoping tests)."""
        view = JournalDraftView(
            ref=JournalDraftRef(self._next_id),
            company=CompanyRef(company_id),
            journal=JournalRef(7),
            period=PERIOD,
            date=FiscalDate.from_gregorian(datetime.date(2026, 11, 16)),
            state="draft",
            description="seeded",
            lines=(
                JournalLineView(
                    account=AccountRef(11),
                    debit=MoneyIRR(500),
                    credit=MoneyIRR(0),
                    state="valid",
                ),
                JournalLineView(
                    account=AccountRef(12),
                    debit=MoneyIRR(0),
                    credit=MoneyIRR(500),
                    state="valid",
                ),
            ),
        )
        move_id = self._next_id
        self._next_id += 1
        self._moves[move_id] = _Move(view=view)
        return move_id

    def _store(
        self,
        company: CompanyRef,
        date: FiscalDate,
        lines: Sequence[JournalDraftLine],
        description: str,
        origin: PostedJournalRef | None = None,
    ) -> int:
        move_id = self._next_id
        self._next_id += 1
        view = JournalDraftView(
            ref=JournalDraftRef(move_id),
            company=company,
            journal=JournalRef(7),
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
        swapped = tuple(
            JournalDraftLine(
                account=line.account,
                debit=line.credit,
                credit=line.debit,
                description=line.description,
            )
            for line in original.view.lines
        )
        reversal_id = self._store(
            original.view.company,
            original.view.date,
            swapped,
            command.description or original.view.description,
            origin=PostedJournalRef(original.view.ref.engine_id),
        )
        self._moves[reversal_id].state = "posted"
        self._moves[reversal_id].number = f"M-{reversal_id}"
        return PostedJournalRef(reversal_id)

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
        raise NotImplementedError("journal endpoints never create invoices")

    def validate_invoice_draft(self, invoice: InvoiceRef) -> ValidationSummary:
        raise NotImplementedError("journal endpoints never validate invoices")

    def get_invoice(self, invoice: InvoiceRef) -> InvoiceView | None:
        raise NotImplementedError("journal endpoints never read invoices")

    def cancel_invoice(self, invoice: InvoiceRef) -> InvoiceView:
        raise NotImplementedError("journal endpoints never cancel invoices")

    def list_invoices(
        self,
        company: CompanyRef,
        invoice_type: str | None = None,
        state: str | None = None,
    ) -> Sequence[InvoiceView]:
        raise NotImplementedError("journal endpoints never list invoices")

    def get_payment(self, payment: PaymentRef) -> PaymentView | None:
        raise NotImplementedError("journal endpoints never read payments")

    def list_payments(
        self,
        company: CompanyRef,
        kind: str | None = None,
        state: str | None = None,
    ) -> Sequence[PaymentView]:
        raise NotImplementedError("journal endpoints never list payments")

    def get_bank_statement(self, statement: BankStatementRef) -> BankStatementView | None:
        raise NotImplementedError("journal endpoints never read statements")

    def create_bank_statement(self, command: CreateBankStatement) -> BankStatementRef:
        raise NotImplementedError("this fake never creates statements")

    def update_bank_statement_line(self, command: UpdateBankStatementLine) -> None:
        raise NotImplementedError("this fake never reconciles statements")

    def list_statement_journals(self, company: CompanyRef) -> Sequence[StatementJournalView]:
        return []

    def list_bank_statements(
        self, company: CompanyRef, state: str | None = None
    ) -> Sequence[BankStatementView]:
        raise NotImplementedError("journal endpoints never list statements")

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
    key_full_secret: str  # all ledger scopes, unrestricted
    key_read_secret: str  # ledger:read only
    key_no_post_secret: str  # all ledger scopes except ledger:post
    key_restricted_a_secret: str  # all ledger scopes, restricted to company A


ALL_LEDGER_SCOPES = [
    "ledger:read",
    "ledger:draft:create",
    "ledger:draft:update",
    "ledger:post",
    "ledger:reverse",
]


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
def fake_engine() -> FakeJournalEngine:
    return FakeJournalEngine()


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
            id=str(uuid.uuid4()), slug=f"rest-202-{uuid.uuid4().hex[:8]}", name="202 Tenant"
        )
        session.add(tenant)
        session.flush()
        client = ApiClientModel(
            id=str(uuid.uuid4()), tenant_id=tenant.id, name="rest-202 client"
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

        def _key(scopes: list[str], **kwargs: object):
            return service.issue_key(
                session,
                client=client,
                name="rest-202 key",
                scopes=scopes,
                installation_id=INSTALLATION_ID,
                **kwargs,  # type: ignore[arg-type]
            )

        key_full = _key(ALL_LEDGER_SCOPES)
        key_read = _key(["ledger:read"])
        key_no_post = _key([s for s in ALL_LEDGER_SCOPES if s != "ledger:post"])
        key_restricted = _key(ALL_LEDGER_SCOPES, engine_company_id=row_a.id)
        session.commit()
        tenant_id = tenant.id
        yield World(
            tenant_id=tenant.id,
            company_a_id=row_a.id,
            company_b_id=row_b.id,
            key_full_secret=key_full.secret,
            key_read_secret=key_read.secret,
            key_no_post_secret=key_no_post.secret,
            key_restricted_a_secret=key_restricted.secret,
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
            if connection_ids:
                cleanup.query(EngineConnectionModel).filter(
                    EngineConnectionModel.id.in_(connection_ids)
                ).delete(synchronize_session=False)
            cleanup.commit()
        finally:
            cleanup.close()


@pytest.fixture()
def api(
    settings: AppSettings, fake_engine: FakeJournalEngine
) -> TestClient:
    """The real app with the journal service backed by the fake engine."""
    app = create_app(settings=settings)
    app.dependency_overrides[get_journal_draft_service] = lambda: JournalDraftService(
        fake_engine
    )
    return TestClient(app, raise_server_exceptions=False)


def _auth(secret: str, key: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {secret}"}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _draft_body(company_id: str, amount: int = 1_000_000) -> dict:
    return {
        "company_id": company_id,
        "date": MOVE_DATE,
        "description": "test draft",
        "lines": [
            {"account_id": 11, "debit": amount, "credit": 0},
            {"account_id": 12, "debit": 0, "credit": amount},
        ],
    }


def _create_draft(api: TestClient, world: World, key: str | None = None) -> dict:
    response = api.post(
        "/api/v1/accounting/journal-drafts",
        json=_draft_body(world.company_a_id),
        headers=_auth(world.key_full_secret, key or f"create-{uuid.uuid4().hex}"),
    )
    assert response.status_code == 201, response.text
    return response.json()


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
# Create
# ---------------------------------------------------------------------------


def test_create_draft_returns_201_with_engine_view(
    api: TestClient, world: World
) -> None:
    response = api.post(
        "/api/v1/accounting/journal-drafts",
        json=_draft_body(world.company_a_id),
        headers=_auth(world.key_full_secret, "create-1"),
    )
    assert response.status_code == 201
    body = response.json()
    assert body["company_id"] == world.company_a_id
    assert body["engine_company_id"] == ENGINE_A
    assert body["state"] == "draft"
    assert body["date"] == MOVE_DATE
    assert body["description"] == "test draft"
    assert body["total_debit"] == body["total_credit"] == 1_000_000
    assert body["currency"] == "IRR"
    assert body["unit"] == "rial"
    assert body["period"]["name"] == "Nov 2026"
    assert len(body["lines"]) == 2
    assert body["lines"][0]["account_id"] == 11
    assert body["lines"][0]["debit"] == 1_000_000


def test_create_draft_unbalanced_returns_400(api: TestClient, world: World) -> None:
    body = _draft_body(world.company_a_id)
    body["lines"][1]["credit"] = 999
    response = api.post(
        "/api/v1/accounting/journal-drafts",
        json=body,
        headers=_auth(world.key_full_secret, "create-unbalanced"),
    )
    assert response.status_code == 400
    assert response.json()["code"] == "LEDGER_UNBALANCED"


def test_create_draft_two_sided_line_returns_400(api: TestClient, world: World) -> None:
    body = _draft_body(world.company_a_id)
    body["lines"][0]["credit"] = 100
    response = api.post(
        "/api/v1/accounting/journal-drafts",
        json=body,
        headers=_auth(world.key_full_secret, "create-twosided"),
    )
    assert response.status_code == 400
    assert response.json()["code"] == "LEDGER_INVALID_JOURNAL"


def test_create_draft_unknown_company_returns_404(api: TestClient, world: World) -> None:
    response = api.post(
        "/api/v1/accounting/journal-drafts",
        json=_draft_body(str(uuid.uuid4())),
        headers=_auth(world.key_full_secret, "create-unknown-co"),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "COMPANY_NOT_FOUND"


def test_create_draft_restricted_key_other_company_returns_403(
    api: TestClient, world: World
) -> None:
    response = api.post(
        "/api/v1/accounting/journal-drafts",
        json=_draft_body(world.company_b_id),
        headers=_auth(world.key_restricted_a_secret, "create-restricted"),
    )
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


def test_create_draft_missing_key_returns_400(api: TestClient, world: World) -> None:
    response = api.post(
        "/api/v1/accounting/journal-drafts",
        json=_draft_body(world.company_a_id),
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 400
    assert response.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_create_draft_wrong_scope_returns_403(api: TestClient, world: World) -> None:
    response = api.post(
        "/api/v1/accounting/journal-drafts",
        json=_draft_body(world.company_a_id),
        headers=_auth(world.key_read_secret, "create-noscope"),
    )
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"
    assert "ledger:draft:create" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Idempotency (create + post)
# ---------------------------------------------------------------------------


def test_create_replay_returns_same_draft_without_second_engine_call(
    api: TestClient, world: World, fake_engine: FakeJournalEngine
) -> None:
    headers = _auth(world.key_full_secret, "replay-create")
    first = api.post(
        "/api/v1/accounting/journal-drafts",
        json=_draft_body(world.company_a_id),
        headers=headers,
    )
    assert first.status_code == 201
    second = api.post(
        "/api/v1/accounting/journal-drafts",
        json=_draft_body(world.company_a_id),
        headers=headers,
    )
    assert second.status_code == 201
    assert second.json() == first.json()
    assert fake_engine.create_calls == 1


def test_create_same_key_different_payload_returns_409(
    api: TestClient, world: World
) -> None:
    headers = _auth(world.key_full_secret, "conflict-create")
    first = api.post(
        "/api/v1/accounting/journal-drafts",
        json=_draft_body(world.company_a_id),
        headers=headers,
    )
    assert first.status_code == 201
    changed = _draft_body(world.company_a_id, amount=2_000_000)
    second = api.post(
        "/api/v1/accounting/journal-drafts", json=changed, headers=headers
    )
    assert second.status_code == 409
    assert second.json()["code"] == "IDEMPOTENCY_CONFLICT"


def test_post_replay_returns_same_result_without_second_posting(
    api: TestClient, world: World, fake_engine: FakeJournalEngine
) -> None:
    draft = _create_draft(api, world)
    headers = _auth(world.key_full_secret, "replay-post")
    first = api.post(f"/api/v1/accounting/journal-drafts/{draft['id']}/post", headers=headers)
    assert first.status_code == 200
    second = api.post(f"/api/v1/accounting/journal-drafts/{draft['id']}/post", headers=headers)
    assert second.status_code == 200
    assert second.json() == first.json()
    assert fake_engine.post_calls == 1


def test_post_same_key_is_scoped_to_its_draft(
    api: TestClient, world: World
) -> None:
    first_draft = _create_draft(api, world)
    second_draft = _create_draft(api, world)
    headers = _auth(world.key_full_secret, "post-scoped-key")
    posted = api.post(
        f"/api/v1/accounting/journal-drafts/{first_draft['id']}/post", headers=headers
    )
    assert posted.status_code == 200
    # Same key, different draft id in the payload → conflict, not a replay.
    conflict = api.post(
        f"/api/v1/accounting/journal-drafts/{second_draft['id']}/post", headers=headers
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"


def test_failed_post_releases_key_for_retry(
    api: TestClient, world: World, fake_engine: FakeJournalEngine
) -> None:
    draft = _create_draft(api, world)
    fake_engine.fail_post_once = EngineError("engine down", details={})
    headers = _auth(world.key_full_secret, "retry-post")
    failed = api.post(f"/api/v1/accounting/journal-drafts/{draft['id']}/post", headers=headers)
    assert failed.status_code == 502
    assert failed.json()["code"] == "ENGINE_ERROR"
    retried = api.post(f"/api/v1/accounting/journal-drafts/{draft['id']}/post", headers=headers)
    assert retried.status_code == 200
    assert retried.json()["id"] == draft["id"]


# ---------------------------------------------------------------------------
# Get / validate
# ---------------------------------------------------------------------------


def test_get_draft_returns_view(api: TestClient, world: World) -> None:
    draft = _create_draft(api, world)
    response = api.get(
        f"/api/v1/accounting/journal-drafts/{draft['id']}",
        headers=_auth(world.key_read_secret),
    )
    assert response.status_code == 200
    assert response.json() == draft


def test_get_draft_missing_returns_404(api: TestClient, world: World) -> None:
    response = api.get(
        f"/api/v1/accounting/journal-drafts/{MISSING_ID}",
        headers=_auth(world.key_read_secret),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "LEDGER_JOURNAL_NOT_FOUND"


def test_get_draft_of_unmapped_company_returns_404(
    api: TestClient, world: World, fake_engine: FakeJournalEngine
) -> None:
    draft_id = fake_engine.seed_draft(ENGINE_UNMAPPED)
    response = api.get(
        f"/api/v1/accounting/journal-drafts/{draft_id}",
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "LEDGER_JOURNAL_NOT_FOUND"


def test_get_draft_restricted_key_other_company_returns_403(
    api: TestClient, world: World, fake_engine: FakeJournalEngine
) -> None:
    draft_id = fake_engine.seed_draft(ENGINE_B)
    response = api.get(
        f"/api/v1/accounting/journal-drafts/{draft_id}",
        headers=_auth(world.key_restricted_a_secret),
    )
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


def test_validate_draft_ok_and_failed(
    api: TestClient, world: World, fake_engine: FakeJournalEngine
) -> None:
    draft = _create_draft(api, world)
    headers = _auth(world.key_full_secret)
    ok = api.post(f"/api/v1/accounting/journal-drafts/{draft['id']}/validate", headers=headers)
    assert ok.status_code == 200
    assert ok.json() == {"valid": True, "issues": []}
    fake_engine.invalid_drafts.add(draft["id"])
    bad = api.post(f"/api/v1/accounting/journal-drafts/{draft['id']}/validate", headers=headers)
    assert bad.status_code == 200
    assert bad.json()["valid"] is False
    assert bad.json()["issues"][0]["code"] == "LEDGER_UNBALANCED"


def test_validate_missing_draft_returns_404(api: TestClient, world: World) -> None:
    response = api.post(
        f"/api/v1/accounting/journal-drafts/{MISSING_ID}/validate",
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "LEDGER_JOURNAL_NOT_FOUND"


# ---------------------------------------------------------------------------
# Post / reverse
# ---------------------------------------------------------------------------


def test_post_draft_returns_posted_journal(api: TestClient, world: World) -> None:
    draft = _create_draft(api, world)
    response = api.post(
        f"/api/v1/accounting/journal-drafts/{draft['id']}/post",
        headers=_auth(world.key_full_secret, "post-1"),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == draft["id"]
    assert body["number"] == f"M-{draft['id']}"
    assert body["post_date"] == MOVE_DATE
    assert body["origin_id"] is None
    assert body["total_debit"] == body["total_credit"] == 1_000_000


def test_post_already_posted_returns_409(api: TestClient, world: World) -> None:
    draft = _create_draft(api, world)
    first = api.post(
        f"/api/v1/accounting/journal-drafts/{draft['id']}/post",
        headers=_auth(world.key_full_secret, "post-twice-1"),
    )
    assert first.status_code == 200
    second = api.post(
        f"/api/v1/accounting/journal-drafts/{draft['id']}/post",
        headers=_auth(world.key_full_secret, "post-twice-2"),
    )
    assert second.status_code == 409
    assert second.json()["code"] == "LEDGER_POSTED_IMMUTABLE"


def test_post_missing_key_returns_400(api: TestClient, world: World) -> None:
    draft = _create_draft(api, world)
    response = api.post(
        f"/api/v1/accounting/journal-drafts/{draft['id']}/post",
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 400
    assert response.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_post_without_scope_returns_403(api: TestClient, world: World) -> None:
    draft = _create_draft(api, world)
    response = api.post(
        f"/api/v1/accounting/journal-drafts/{draft['id']}/post",
        headers=_auth(world.key_no_post_secret, "post-noscope"),
    )
    assert response.status_code == 403
    assert "ledger:post" in response.json()["detail"]


def test_reverse_returns_linked_reversal(api: TestClient, world: World) -> None:
    draft = _create_draft(api, world)
    posted = api.post(
        f"/api/v1/accounting/journal-drafts/{draft['id']}/post",
        headers=_auth(world.key_full_secret, "reverse-post"),
    )
    assert posted.status_code == 200
    response = api.post(
        f"/api/v1/accounting/journals/{draft['id']}/reverse",
        json={"description": "correction"},
        headers=_auth(world.key_full_secret, "reverse-1"),
    )
    assert response.status_code == 201
    body = response.json()
    assert body["origin_id"] == draft["id"]
    assert body["description"] == "correction"
    assert body["lines"][0]["debit"] == 0
    assert body["lines"][0]["credit"] == 1_000_000
    assert body["lines"][1]["debit"] == 1_000_000


def test_reverse_without_body_keeps_original_description(
    api: TestClient, world: World
) -> None:
    draft = _create_draft(api, world)
    api.post(
        f"/api/v1/accounting/journal-drafts/{draft['id']}/post",
        headers=_auth(world.key_full_secret, "reverse-nobody-post"),
    )
    response = api.post(
        f"/api/v1/accounting/journals/{draft['id']}/reverse",
        headers=_auth(world.key_full_secret, "reverse-nobody"),
    )
    assert response.status_code == 201
    assert response.json()["description"] == "test draft"


def test_reverse_draft_returns_400(api: TestClient, world: World) -> None:
    draft = _create_draft(api, world)
    response = api.post(
        f"/api/v1/accounting/journals/{draft['id']}/reverse",
        headers=_auth(world.key_full_secret, "reverse-draft"),
    )
    assert response.status_code == 400
    assert response.json()["code"] == "LEDGER_INVALID_JOURNAL"


def test_reverse_missing_returns_404(api: TestClient, world: World) -> None:
    response = api.post(
        f"/api/v1/accounting/journals/{MISSING_ID}/reverse",
        headers=_auth(world.key_full_secret, "reverse-missing"),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "LEDGER_JOURNAL_NOT_FOUND"


def test_reverse_replay_returns_same_reversal(
    api: TestClient, world: World, fake_engine: FakeJournalEngine
) -> None:
    draft = _create_draft(api, world)
    api.post(
        f"/api/v1/accounting/journal-drafts/{draft['id']}/post",
        headers=_auth(world.key_full_secret, "reverse-replay-post"),
    )
    headers = _auth(world.key_full_secret, "reverse-replay")
    first = api.post(f"/api/v1/accounting/journals/{draft['id']}/reverse", headers=headers)
    assert first.status_code == 201
    second = api.post(f"/api/v1/accounting/journals/{draft['id']}/reverse", headers=headers)
    assert second.status_code == 201
    assert second.json() == first.json()
    assert fake_engine.reverse_calls == 1


# ---------------------------------------------------------------------------
# List + update (live since the US-202 follow-up)
# ---------------------------------------------------------------------------


def test_list_drafts_returns_company_drafts_only(api: TestClient, world: World) -> None:
    first = _create_draft(api, world)
    second = _create_draft(api, world)
    # A draft of company B must not leak into company A's list.
    other = api.post(
        "/api/v1/accounting/journal-drafts",
        json=_draft_body(world.company_b_id),
        headers=_auth(world.key_full_secret, f"create-b-{uuid.uuid4().hex}"),
    )
    assert other.status_code == 201
    response = api.get(
        f"/api/v1/accounting/journal-drafts?company={world.company_a_id}",
        headers=_auth(world.key_read_secret),
    )
    assert response.status_code == 200
    body = response.json()
    # Default sort is date ascending, ties by id ascending.
    assert [item["id"] for item in body["items"]] == [first["id"], second["id"]]
    assert body["next_cursor"] is None
    assert all(item["company_id"] == world.company_a_id for item in body["items"])
    newest_first = api.get(
        f"/api/v1/accounting/journal-drafts?company={world.company_a_id}&sort=-id",
        headers=_auth(world.key_read_secret),
    )
    assert [item["id"] for item in newest_first.json()["items"]] == [
        second["id"],
        first["id"],
    ]


def test_list_drafts_excludes_posted_journals(api: TestClient, world: World) -> None:
    draft = _create_draft(api, world)
    posted = api.post(
        f"/api/v1/accounting/journal-drafts/{draft['id']}/post",
        headers=_auth(world.key_full_secret, "list-excludes-posted"),
    )
    assert posted.status_code == 200
    response = api.get(
        f"/api/v1/accounting/journal-drafts?company={world.company_a_id}",
        headers=_auth(world.key_read_secret),
    )
    assert response.status_code == 200
    assert response.json()["items"] == []


def test_list_drafts_paginates_with_cursor(api: TestClient, world: World) -> None:
    ids = [_create_draft(api, world)["id"] for _ in range(3)]
    first_page = api.get(
        f"/api/v1/accounting/journal-drafts?company={world.company_a_id}&limit=2",
        headers=_auth(world.key_read_secret),
    )
    assert first_page.status_code == 200
    assert len(first_page.json()["items"]) == 2
    cursor = first_page.json()["next_cursor"]
    assert cursor
    second_page = api.get(
        f"/api/v1/accounting/journal-drafts?company={world.company_a_id}"
        f"&limit=2&cursor={cursor}",
        headers=_auth(world.key_read_secret),
    )
    assert second_page.status_code == 200
    assert len(second_page.json()["items"]) == 1
    assert second_page.json()["next_cursor"] is None
    seen = {item["id"] for item in first_page.json()["items"]} | {
        item["id"] for item in second_page.json()["items"]
    }
    assert seen == set(ids)


def test_list_drafts_validates_params(api: TestClient, world: World) -> None:
    missing_company = api.get(
        "/api/v1/accounting/journal-drafts", headers=_auth(world.key_read_secret)
    )
    assert missing_company.status_code == 400
    assert missing_company.json()["code"] == "COMPANY_REQUIRED"
    bad_sort = api.get(
        f"/api/v1/accounting/journal-drafts?company={world.company_a_id}&sort=nope",
        headers=_auth(world.key_read_secret),
    )
    assert bad_sort.status_code == 400
    assert bad_sort.json()["code"] == "INVALID_SORT"


def test_patch_draft_updates_description_and_lines(
    api: TestClient, world: World
) -> None:
    draft = _create_draft(api, world)
    response = api.patch(
        f"/api/v1/accounting/journal-drafts/{draft['id']}",
        json={
            "description": "renamed",
            "lines": [
                {"account_id": 11, "debit": 2_000_000, "credit": 0},
                {"account_id": 12, "debit": 0, "credit": 2_000_000},
            ],
        },
        headers=_auth(world.key_full_secret, "patch-live"),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == draft["id"]
    assert body["description"] == "renamed"
    assert body["total_debit"] == body["total_credit"] == 2_000_000
    assert body["date"] == MOVE_DATE  # unchanged field stays put
    read_back = api.get(
        f"/api/v1/accounting/journal-drafts/{draft['id']}",
        headers=_auth(world.key_read_secret),
    )
    assert read_back.json()["description"] == "renamed"


def test_patch_draft_posted_returns_409(api: TestClient, world: World) -> None:
    draft = _create_draft(api, world)
    posted = api.post(
        f"/api/v1/accounting/journal-drafts/{draft['id']}/post",
        headers=_auth(world.key_full_secret, "patch-posted-post"),
    )
    assert posted.status_code == 200
    response = api.patch(
        f"/api/v1/accounting/journal-drafts/{draft['id']}",
        json={"description": "renamed"},
        headers=_auth(world.key_full_secret, "patch-posted"),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "LEDGER_POSTED_IMMUTABLE"


def test_patch_draft_unbalanced_returns_400(api: TestClient, world: World) -> None:
    draft = _create_draft(api, world)
    response = api.patch(
        f"/api/v1/accounting/journal-drafts/{draft['id']}",
        json={
            "lines": [
                {"account_id": 11, "debit": 100, "credit": 0},
                {"account_id": 12, "debit": 0, "credit": 200},
            ]
        },
        headers=_auth(world.key_full_secret, "patch-unbalanced"),
    )
    assert response.status_code == 400
    assert response.json()["code"] == "LEDGER_UNBALANCED"


def test_patch_draft_empty_body_returns_400(api: TestClient, world: World) -> None:
    draft = _create_draft(api, world)
    response = api.patch(
        f"/api/v1/accounting/journal-drafts/{draft['id']}",
        json={},
        headers=_auth(world.key_full_secret, "patch-empty"),
    )
    assert response.status_code == 400
    assert response.json()["code"] == "LEDGER_INVALID_JOURNAL"


def test_patch_draft_restricted_key_other_company_returns_403(
    api: TestClient, world: World, fake_engine: FakeJournalEngine
) -> None:
    draft_id = fake_engine.seed_draft(ENGINE_B)
    response = api.patch(
        f"/api/v1/accounting/journal-drafts/{draft_id}",
        json={"description": "renamed"},
        headers=_auth(world.key_restricted_a_secret, "patch-restricted"),
    )
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


def test_patch_draft_requires_key_and_resolves_draft_first(
    api: TestClient, world: World
) -> None:
    draft = _create_draft(api, world)
    no_key = api.patch(
        f"/api/v1/accounting/journal-drafts/{draft['id']}",
        json={"description": "renamed"},
        headers=_auth(world.key_full_secret),
    )
    assert no_key.status_code == 400
    assert no_key.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    missing = api.patch(
        f"/api/v1/accounting/journal-drafts/{MISSING_ID}",
        json={"description": "renamed"},
        headers=_auth(world.key_full_secret, "patch-missing"),
    )
    assert missing.status_code == 404
    assert missing.json()["code"] == "LEDGER_JOURNAL_NOT_FOUND"


# ---------------------------------------------------------------------------
# Auth + audit
# ---------------------------------------------------------------------------


def test_no_key_returns_401_on_all_journal_endpoints(
    api: TestClient, world: World
) -> None:
    draft = _create_draft(api, world)
    paths = [
        ("POST", "/api/v1/accounting/journal-drafts"),
        ("GET", "/api/v1/accounting/journal-drafts?company=" + world.company_a_id),
        ("GET", f"/api/v1/accounting/journal-drafts/{draft['id']}"),
        ("PATCH", f"/api/v1/accounting/journal-drafts/{draft['id']}"),
        ("POST", f"/api/v1/accounting/journal-drafts/{draft['id']}/validate"),
        ("POST", f"/api/v1/accounting/journal-drafts/{draft['id']}/post"),
        ("POST", f"/api/v1/accounting/journals/{draft['id']}/reverse"),
    ]
    for method, path in paths:
        response = api.request(method, path, json={} if method in ("POST", "PATCH") else None)
        assert response.status_code == 401, path
        assert response.json()["code"] == "UNAUTHORIZED"


def test_create_post_reverse_write_audit_events(
    api: TestClient, world: World, engine: Engine
) -> None:
    draft = _create_draft(api, world, key="audit-create")
    api.post(
        f"/api/v1/accounting/journal-drafts/{draft['id']}/post",
        headers=_auth(world.key_full_secret, "audit-post"),
    )
    reversal = api.post(
        f"/api/v1/accounting/journals/{draft['id']}/reverse",
        headers=_auth(world.key_full_secret, "audit-reverse"),
    )
    assert reversal.status_code == 201
    reversal_id = reversal.json()["id"]

    events = _audit_events(engine, world.tenant_id)
    assert [event.action for event in events] == [
        "ledger.draft.create",
        "ledger.post",
        "ledger.reverse",
    ]
    for event in events:
        assert event.tenant_id == world.tenant_id
        assert event.installation_id == INSTALLATION_ID
        assert event.api_client_id is not None
        assert event.outcome == "success"
        assert event.error_code is None
        assert event.company_external_id == str(ENGINE_A)
        assert event.occurred_at is not None
        assert event.payload_hash
        assert event.request_id
    create, post, reverse = events
    assert create.resource_type == "journal_draft"
    assert create.resource_id == str(draft["id"])
    assert create.tryton_reference == f"account.move:{draft['id']}"
    assert create.metadata_after is not None
    assert create.metadata_after["idempotency_key"] == "audit-create"
    assert post.resource_type == "posted_journal"
    assert post.tryton_reference == f"account.move:{draft['id']}"
    assert post.metadata_after is not None
    assert post.metadata_after["idempotency_key"] == "audit-post"
    assert post.metadata_after["number"] == f"M-{draft['id']}"
    assert reverse.resource_id == str(reversal_id)
    assert reverse.tryton_reference == f"account.move:{reversal_id}"
    assert reverse.metadata_after is not None
    assert reverse.metadata_after["idempotency_key"] == "audit-reverse"
    assert reverse.metadata_after["original_journal_id"] == draft["id"]


def test_failed_post_writes_error_audit_event(
    api: TestClient, world: World, engine: Engine, fake_engine: FakeJournalEngine
) -> None:
    draft = _create_draft(api, world)
    fake_engine.fail_post_once = EngineError("engine down", details={})
    failed = api.post(
        f"/api/v1/accounting/journal-drafts/{draft['id']}/post",
        headers=_auth(world.key_full_secret, "audit-fail-post"),
    )
    assert failed.status_code == 502
    events = _audit_events(engine, world.tenant_id)
    failures = [e for e in events if e.action == "ledger.post"]
    assert len(failures) == 1
    assert failures[0].outcome == "error"
    assert failures[0].error_code == "ENGINE_ERROR"
    assert failures[0].tryton_reference == f"account.move:{draft['id']}"
    assert failures[0].metadata_after is not None
    assert failures[0].metadata_after["idempotency_key"] == "audit-fail-post"


def test_idempotency_record_stores_response_for_replay(
    api: TestClient, world: World, engine: Engine
) -> None:
    draft = _create_draft(api, world, key="record-create")
    session = sessionmaker(bind=engine)()
    try:
        record = (
            session.query(IdempotencyRecordModel)
            .filter(
                IdempotencyRecordModel.installation_id == INSTALLATION_ID,
                IdempotencyRecordModel.operation == "ledger.draft.create",
                IdempotencyRecordModel.idempotency_key == "record-create",
            )
            .one()
        )
        assert record.response_status == 201
        assert record.response_body is not None
        assert record.response_body["id"] == draft["id"]
        assert record.response_reference == f"accounting/journal-drafts/{draft['id']}"
        assert record.request_hash
    finally:
        session.close()
