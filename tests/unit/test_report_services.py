"""Unit tests for the report query + validation services (US-403).

The engine port is faked in memory; the standards registry and the
report-snapshot port are the real SQLAlchemy implementations over a
transactional session on the test DB (rolled back after each test).
The registry is seeded inside the transaction, so tests never depend
on rows committed by other modules. The fake trial balance stands in
for the live engine read — the adapter's own trial-balance read is
contract-tested against live Tryton separately.
"""

from __future__ import annotations

import datetime
import os
import uuid
from collections.abc import Generator, Sequence
from decimal import (
    Decimal,
)

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from hesabyar.application.commands.reports import (
    ReportValidationService,
    build_trial_balance_document,
)
from hesabyar.application.commands.standards import StandardsValidationService
from hesabyar.application.errors import (
    CompanyNotFoundError,
    ReportValidationRunNotFoundError,
    StandardSourceBlockedError,
    UnbalancedTrialBalanceError,
)
from hesabyar.application.queries.companies import CompanyQueryService
from hesabyar.application.queries.reports import ReportQueryService
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
    TrialBalanceLineView,
    TrialBalanceView,
    ValidationSummary,
)
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.errors import HesabYarError
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.domain.standards.models import RuleOutcome
from hesabyar.infrastructure.persistence.postgres.models import (
    Base,
    StandardRevisionModel,
    StandardRuleModel,
    StandardSourceModel,
    TenantModel,
)
from hesabyar.infrastructure.persistence.postgres.report_snapshot_repository import (
    SqlAlchemyReportSnapshotRepository,
)
from hesabyar.infrastructure.persistence.postgres.seed_standards import seed_standards
from hesabyar.infrastructure.persistence.postgres.standards_repository import (
    PostgresStandardsRegistry,
)
from tests.standards_fixtures import BALANCED_DATA

TEST_DB_URL = os.getenv(
    "HESABYAR_DATABASE_URL",
    "postgresql+psycopg://hesabyar:hesabyar_pass@hesabyar-postgres:5432/hesabyar_test",
)

# 1403/12/29 (before Standard 43 applies), 1404/01/01 (Standard 43
# applies), 1404/06/01 (a 1404 period) and 1405/01/01 (first blocked
# day for Standard 15; Standard 44 applies).
PERIOD_1403_END = datetime.date(2025, 3, 20)
PERIOD_1404_START = datetime.date(2025, 3, 21)
PERIOD_1404 = datetime.date(2025, 8, 23)
PERIOD_1405_START = datetime.date(2026, 3, 21)

COMPANY = CompanyRef(engine_id=1)


def _trial_balance(
    *,
    balanced: bool = True,
    date_from: datetime.date = PERIOD_1404,
    date_to: datetime.date = PERIOD_1404,
) -> TrialBalanceView:
    """A two-account trial balance: cash debit 1,000,000, revenue credit 1,000,000."""
    credit = 1_000_000 if balanced else 999_999
    return TrialBalanceView(
        company=COMPANY,
        date_from=FiscalDate(date_from),
        date_to=FiscalDate(date_to),
        lines=(
            TrialBalanceLineView(
                account=AccountRef(engine_id=35),
                code="1.1.1",
                name="Cash and Cash Equivalents",
                total_debit=MoneyIRR(1_000_000),
                total_credit=MoneyIRR(0),
            ),
            TrialBalanceLineView(
                account=AccountRef(engine_id=36),
                code="4.1.1",
                name="Revenue",
                total_debit=MoneyIRR(0),
                total_credit=MoneyIRR(credit),
            ),
        ),
        total_debit=MoneyIRR(1_000_000),
        total_credit=MoneyIRR(credit),
        move_count=1,
        line_count=2,
    )


class FakeReportEngine:
    """In-memory AccountingEnginePort for report-service tests."""

    def __init__(
        self,
        companies: Sequence[CompanyView] | None = None,
        trial_balance: TrialBalanceView | None = None,
        failure: HesabYarError | None = None,
    ) -> None:
        self._companies = list(companies or [])
        self._trial_balance = trial_balance
        self._failure = failure

    def list_companies(self) -> Sequence[CompanyView]:
        if self._failure is not None:
            raise self._failure
        return list(self._companies)

    def get_chart_of_accounts(self, company: CompanyRef) -> Sequence[AccountView]:
        raise NotImplementedError("report services never read the chart")

    def create_journal_draft(self, command: CreateJournalDraft) -> JournalDraftRef:
        raise NotImplementedError("report services never create drafts")

    def validate_journal_draft(self, draft: JournalDraftRef) -> ValidationSummary:
        raise NotImplementedError("report services never validate drafts")

    def get_journal_draft(self, draft: JournalDraftRef) -> JournalDraftView | None:
        raise NotImplementedError("report services never read drafts")

    def list_journal_drafts(self, company: CompanyRef) -> Sequence[JournalDraftView]:
        raise NotImplementedError("report services never list drafts")

    def update_journal_draft(self, command: UpdateJournalDraft) -> JournalDraftView:
        raise NotImplementedError("report services never update drafts")

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
        raise NotImplementedError("report services never post journals")

    def reverse_journal(self, command: ReverseJournal) -> PostedJournalRef:
        raise NotImplementedError("report services never reverse journals")

    def get_journal(self, posted: PostedJournalRef) -> PostedJournalView | None:
        raise NotImplementedError("report services never read journals")

    def list_fiscal_years(self, company: CompanyRef) -> Sequence[FiscalYearView]:
        raise NotImplementedError("report services never list fiscal years")

    def list_periods(
        self, company: CompanyRef, fiscal_year: FiscalYearRef | None = None
    ) -> Sequence[FiscalPeriodView]:
        raise NotImplementedError("report services never list periods")

    def get_trial_balance(
        self,
        company: CompanyRef,
        date_from: FiscalDate,
        date_to: FiscalDate,
    ) -> TrialBalanceView:
        if self._failure is not None:
            raise self._failure
        assert self._trial_balance is not None, "fake engine has no trial balance"
        return self._trial_balance

    def create_invoice_draft(self, command: CreateInvoiceDraft) -> InvoiceRef:
        raise NotImplementedError("report services never create invoices")

    def validate_invoice_draft(self, invoice: InvoiceRef) -> ValidationSummary:
        raise NotImplementedError("report services never validate invoices")

    def get_invoice(self, invoice: InvoiceRef) -> InvoiceView | None:
        raise NotImplementedError("report services never read invoices")

    def cancel_invoice(self, invoice: InvoiceRef) -> InvoiceView:
        raise NotImplementedError("report services never cancel invoices")

    def list_invoices(
        self,
        company: CompanyRef,
        invoice_type: str | None = None,
        state: str | None = None,
    ) -> Sequence[InvoiceView]:
        raise NotImplementedError("report services never list invoices")

    def get_payment(self, payment: PaymentRef) -> PaymentView | None:
        raise NotImplementedError("report services never read payments")

    def list_payments(
        self,
        company: CompanyRef,
        kind: str | None = None,
        state: str | None = None,
    ) -> Sequence[PaymentView]:
        raise NotImplementedError("report services never list payments")

    def get_bank_statement(self, statement: BankStatementRef) -> BankStatementView | None:
        raise NotImplementedError("report services never read statements")

    def create_bank_statement(self, command: CreateBankStatement) -> BankStatementRef:
        raise NotImplementedError("this fake never creates statements")

    def update_bank_statement_line(self, command: UpdateBankStatementLine) -> None:
        raise NotImplementedError("this fake never reconciles statements")

    def list_statement_journals(self, company: CompanyRef) -> Sequence[StatementJournalView]:
        return []

    def list_bank_statements(
        self, company: CompanyRef, state: str | None = None
    ) -> Sequence[BankStatementView]:
        raise NotImplementedError("report services never list statements")

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


@pytest.fixture(scope="module")
def engine() -> Generator[Engine, None, None]:
    eng = create_engine(TEST_DB_URL, echo=False)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def db(engine: Engine) -> Generator[Session, None, None]:
    connection = engine.connect()
    transaction = connection.begin()
    session = sessionmaker(bind=connection)()
    try:
        yield session
    finally:
        session.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()


@pytest.fixture()
def tenant(db: Session) -> TenantModel:
    row = TenantModel(
        id=str(uuid.uuid4()),
        slug=f"std-403-{uuid.uuid4().hex[:8]}",
        name="Report Test Tenant",
    )
    db.add(row)
    db.flush()
    return row


@pytest.fixture()
def seeded(db: Session) -> None:
    seed_standards(db)
    db.flush()


@pytest.fixture()
def validation(db: Session, seeded: None) -> StandardsValidationService:
    return StandardsValidationService(PostgresStandardsRegistry(db))


@pytest.fixture()
def reports(db: Session, seeded: None, validation: StandardsValidationService) -> ReportValidationService:
    return ReportValidationService(
        FakeReportEngine(
            companies=[CompanyView(ref=COMPANY, name="Acme", currency_code="IRR")],
            trial_balance=_trial_balance(),
        ),
        validation,
        SqlAlchemyReportSnapshotRepository(db),
    )


@pytest.fixture()
def queries() -> ReportQueryService:
    return ReportQueryService(
        FakeReportEngine(
            companies=[CompanyView(ref=COMPANY, name="Acme", currency_code="IRR")],
            trial_balance=_trial_balance(),
        )
    )


def _add_test_rule(
    db: Session,
    *,
    standard_code: str,
    rule_id: str,
    formula: str = "1 <= 1",
) -> None:
    """Insert one test-only rule under a standard's revision (US-401 pattern)."""
    revision = db.execute(
        select(StandardRevisionModel)
        .join(
            StandardSourceModel,
            StandardRevisionModel.source_id == StandardSourceModel.id,
        )
        .where(StandardSourceModel.standard_code == standard_code)
    ).scalar_one()
    db.add(
        StandardRuleModel(
            id=str(uuid.uuid4()),
            revision_id=revision.id,
            rule_id=rule_id,
            category="cross_checks",
            description=f"Test-only rule for the Standard {standard_code} revision",
            dsl_formula=formula,
            rule_type="equation",
            severity="error",
            is_executable=True,
            ruleset_version="1.0.0-legacy",
        )
    )
    db.flush()


# ---------------------------------------------------------------------------
# Trial-balance document
# ---------------------------------------------------------------------------


def test_trial_balance_document_carries_per_account_balances() -> None:
    document = build_trial_balance_document(_trial_balance())
    assert document["total_debit"] == 1_000_000
    assert document["total_credit"] == 1_000_000
    assert document["trial_balance"]["balanced"] is True
    assert document["trial_balance"]["moves_count"] == 1
    assert document["trial_balance"]["lines_count"] == 2
    assert document["trial_balance"]["accounts"]["1.1.1"] == {
        "name": "Cash and Cash Equivalents",
        "debit": 1_000_000,
        "credit": 0,
        "balance": 1_000_000,
    }
    assert document["trial_balance"]["accounts"]["4.1.1"]["balance"] == -1_000_000


# ---------------------------------------------------------------------------
# Report query service
# ---------------------------------------------------------------------------


def test_query_service_returns_trial_balance(queries: ReportQueryService) -> None:
    view = queries.get_trial_balance(COMPANY, PERIOD_1404, PERIOD_1404)
    assert view.is_balanced is True
    assert view.total_debit.amount == 1_000_000
    assert view.total_credit.amount == 1_000_000
    assert view.move_count == 1
    assert view.source_endpoint == "account.move.line (Tryton REST)"


def test_query_service_unknown_company_raises() -> None:
    service = ReportQueryService(FakeReportEngine(companies=[], trial_balance=_trial_balance()))
    with pytest.raises(CompanyNotFoundError):
        service.get_trial_balance(COMPANY, PERIOD_1404, PERIOD_1404)


def test_query_service_bad_period_range_raises(queries: ReportQueryService) -> None:
    with pytest.raises(ValueError):
        queries.get_trial_balance(COMPANY, PERIOD_1404, PERIOD_1403_END)


# ---------------------------------------------------------------------------
# Report validation runs over the live trial balance
# ---------------------------------------------------------------------------


def test_create_run_persists_run_and_snapshot(
    reports: ReportValidationService, tenant: TenantModel, db: Session
) -> None:
    record = reports.create_report_validation_run(
        tenant_id=tenant.id,
        company=COMPANY,
        company_id="1",
        period_start=PERIOD_1404,
        period_end=PERIOD_1404,
        standard_codes=["1"],
    )
    run = record.run
    snapshot = record.snapshot
    # A trial balance alone has no statement-level figures: every
    # statement rule fails closed with ERROR, never PASS — and the run
    # says so per rule. The disclosure rule CF005 is not executed.
    assert run.overall_outcome == RuleOutcome.ERROR
    assert run.is_valid is False
    assert run.rules_executed == 35
    assert run.rules_errored == 35
    assert run.rules_passed == 0
    assert [item.rule_id for item in run.not_executed] == ["CF005"]
    assert run.coverage.corpus_standards == 35
    assert run.coverage.standards_with_rules == 1
    assert run.coverage.rules_implemented == 36
    assert run.coverage.rules_verified == 0
    assert run.coverage.rules_executed == 35
    # Provenance: the snapshot reproduces the run from the live figures.
    assert snapshot.validation_run_id == run.run_id
    assert snapshot.payload_hash == run.input_hash
    assert snapshot.as_of_date == PERIOD_1404
    assert snapshot.source_endpoint == "account.move.line (Tryton REST)"
    assert snapshot.ruleset_version == "1.0.0-legacy"
    assert snapshot.balanced is True
    assert snapshot.total_debit == 1_000_000
    assert snapshot.total_credit == 1_000_000
    assert snapshot.payload["trial_balance"]["accounts"]["1.1.1"]["balance"] == 1_000_000
    assert snapshot.created_at is not None
    # The run round-trips through the snapshot repository.
    fetched = SqlAlchemyReportSnapshotRepository(db).get_snapshot_for_run(run.run_id, tenant.id)
    assert fetched is not None
    assert fetched.snapshot_id == snapshot.snapshot_id
    assert fetched.payload == snapshot.payload


def test_create_run_unbalanced_trial_balance_raises(
    db: Session, seeded: None, validation: StandardsValidationService, tenant: TenantModel
) -> None:
    service = ReportValidationService(
        FakeReportEngine(
            companies=[CompanyView(ref=COMPANY, name="Acme", currency_code="IRR")],
            trial_balance=_trial_balance(balanced=False),
        ),
        validation,
        SqlAlchemyReportSnapshotRepository(db),
    )
    with pytest.raises(UnbalancedTrialBalanceError) as excinfo:
        service.create_report_validation_run(
            tenant_id=tenant.id,
            company=COMPANY,
            period_start=PERIOD_1404,
            period_end=PERIOD_1404,
            standard_codes=["1"],
        )
    assert excinfo.value.details["total_debit"] == 1_000_000
    assert excinfo.value.details["total_credit"] == 999_999
    # Nothing was persisted for the failed run.
    assert SqlAlchemyReportSnapshotRepository(db).get_snapshot_for_run("no-such-run", tenant.id) is None


def test_create_run_bad_period_range_raises(
    reports: ReportValidationService, tenant: TenantModel
) -> None:
    with pytest.raises(ValueError):
        reports.create_report_validation_run(
            tenant_id=tenant.id,
            company=COMPANY,
            period_start=PERIOD_1404,
            period_end=PERIOD_1403_END,
        )


# ---------------------------------------------------------------------------
# Standard 15 gate + effective-date applicability
# ---------------------------------------------------------------------------


def test_standard_15_blocked_for_1405_period(
    reports: ReportValidationService, tenant: TenantModel
) -> None:
    with pytest.raises(StandardSourceBlockedError) as excinfo:
        reports.create_report_validation_run(
            tenant_id=tenant.id,
            company=COMPANY,
            period_start=PERIOD_1405_START,
            period_end=PERIOD_1405_START,
        )
    assert excinfo.value.details["standard_code"] == "15"
    assert excinfo.value.code == "STANDARD_SOURCE_BLOCKED"


def test_standard_15_allowed_for_historical_period(
    reports: ReportValidationService, tenant: TenantModel
) -> None:
    record = reports.create_report_validation_run(
        tenant_id=tenant.id,
        company=COMPANY,
        period_start=PERIOD_1404,
        period_end=PERIOD_1404,
    )
    assert record.run.overall_outcome == RuleOutcome.ERROR  # 35 statement rules ERROR
    assert record.run.rules_not_applicable == 0


def test_effective_date_std44_rule_not_applicable_before_1405(
    reports: ReportValidationService, tenant: TenantModel, db: Session
) -> None:
    """A rule of the Standard 44 revision (effective 1405/01/01) is NA for a 1404 period."""
    _add_test_rule(db, standard_code="44", rule_id="ZZ044")
    view_1404 = reports.create_report_validation_run(
        tenant_id=tenant.id,
        company=COMPANY,
        period_start=PERIOD_1404,
        period_end=PERIOD_1404,
        standard_codes=["44"],
    )
    assert view_1404.run.rules_executed == 0
    assert view_1404.run.rules_not_applicable == 1
    assert view_1404.run.overall_outcome == RuleOutcome.NOT_APPLICABLE
    assert view_1404.run.results[0].rule_id == "ZZ044"
    assert view_1404.run.results[0].outcome == RuleOutcome.NOT_APPLICABLE
    view_1405 = reports.create_report_validation_run(
        tenant_id=tenant.id,
        company=COMPANY,
        period_start=PERIOD_1405_START,
        period_end=PERIOD_1405_START,
        standard_codes=["44"],
    )
    assert view_1405.run.rules_executed == 1
    assert view_1405.run.rules_passed == 1
    assert view_1405.run.overall_outcome == RuleOutcome.PASS


def test_effective_date_std43_rule_applies_from_1404(
    reports: ReportValidationService, tenant: TenantModel, db: Session
) -> None:
    """A rule of the Standard 43 revision (effective 1404/01/01) is NA before it, executed from it."""
    _add_test_rule(db, standard_code="43", rule_id="ZZ043")
    view_before = reports.create_report_validation_run(
        tenant_id=tenant.id,
        company=COMPANY,
        period_start=PERIOD_1403_END,
        period_end=PERIOD_1403_END,
        standard_codes=["43"],
    )
    assert view_before.run.rules_executed == 0
    assert view_before.run.rules_not_applicable == 1
    view_from = reports.create_report_validation_run(
        tenant_id=tenant.id,
        company=COMPANY,
        period_start=PERIOD_1404_START,
        period_end=PERIOD_1404_START,
        standard_codes=["43"],
    )
    assert view_from.run.rules_executed == 1
    assert view_from.run.rules_passed == 1
    assert view_from.run.overall_outcome == RuleOutcome.PASS


# ---------------------------------------------------------------------------
# get_report_validation_run
# ---------------------------------------------------------------------------


def test_get_report_validation_run_round_trip(
    reports: ReportValidationService, tenant: TenantModel
) -> None:
    created = reports.create_report_validation_run(
        tenant_id=tenant.id,
        company=COMPANY,
        period_start=PERIOD_1404,
        period_end=PERIOD_1404,
        standard_codes=["1"],
    )
    fetched = reports.get_report_validation_run(created.run.run_id, tenant.id)
    assert fetched.run == created.run
    assert fetched.snapshot == created.snapshot


def test_get_report_validation_run_unknown_raises(
    reports: ReportValidationService, tenant: TenantModel
) -> None:
    with pytest.raises(ReportValidationRunNotFoundError):
        reports.get_report_validation_run(str(uuid.uuid4()), tenant.id)


def test_get_report_validation_run_other_tenant_looks_missing(
    reports: ReportValidationService, tenant: TenantModel
) -> None:
    created = reports.create_report_validation_run(
        tenant_id=tenant.id,
        company=COMPANY,
        period_start=PERIOD_1404,
        period_end=PERIOD_1404,
        standard_codes=["1"],
    )
    with pytest.raises(ReportValidationRunNotFoundError):
        reports.get_report_validation_run(created.run.run_id, str(uuid.uuid4()))


def test_get_report_validation_run_plain_run_has_no_snapshot(
    reports: ReportValidationService,
    validation: StandardsValidationService,
    tenant: TenantModel,
) -> None:
    """A run created by the plain standards endpoint is not a report run."""
    plain = validation.create_run(
        tenant_id=tenant.id,
        period_start=PERIOD_1404,
        standard_codes=["1"],
        data=BALANCED_DATA,
    )
    with pytest.raises(ReportValidationRunNotFoundError):
        reports.get_report_validation_run(plain.run_id, tenant.id)


def test_company_query_service_still_resolves() -> None:
    """Guard: the report query service's company check uses the shared service."""
    service = CompanyQueryService(
        FakeReportEngine(companies=[CompanyView(ref=COMPANY, name="Acme", currency_code="IRR")])
    )
    assert service.get_company(COMPANY).name == "Acme"


def test_snapshot_repository_round_trip(
    db: Session, seeded: None, validation: StandardsValidationService, tenant: TenantModel
) -> None:
    """The snapshot port persists and reads back by id and by run."""
    from hesabyar.domain.reports.models import ReportSnapshot

    run = validation.create_run(
        tenant_id=tenant.id,
        period_start=PERIOD_1404,
        standard_codes=["1"],
        data=BALANCED_DATA,
    )
    repo = SqlAlchemyReportSnapshotRepository(db)
    snapshot = ReportSnapshot(
        snapshot_id=str(uuid.uuid4()),
        tenant_id=tenant.id,
        company_id="1",
        validation_run_id=run.run_id,
        period_start=PERIOD_1404,
        period_end=PERIOD_1404,
        as_of_date=PERIOD_1404,
        source_endpoint="account.move.line (Tryton REST)",
        ruleset_version="1.0.0-legacy",
        payload_hash=run.input_hash,
        payload={"trial_balance": {"total_debit": 0, "total_credit": 0}},
        balanced=True,
        total_debit=0,
        total_credit=0,
    )
    saved = repo.save_snapshot(snapshot)
    assert saved.created_at is not None
    assert repo.get_snapshot(saved.snapshot_id, tenant.id) == saved
    assert repo.get_snapshot_for_run(run.run_id, tenant.id) == saved
    assert repo.get_snapshot(saved.snapshot_id, str(uuid.uuid4())) is None
