"""MCP service-container tests (US-601).

The container wires the SAME application services REST uses — these
tests prove construction (same classes, same ports) without touching
the network or the database: service construction never queries, and
the Tryton client is only built, never called.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from hesabyar.application.commands.invoices import InvoiceDraftService
from hesabyar.application.commands.journals import JournalDraftService
from hesabyar.application.commands.moadian import MoadianDocumentService
from hesabyar.application.commands.reports import ReportValidationService
from hesabyar.application.commands.standards import StandardsValidationService
from hesabyar.application.commands.tax_policy import TaxDecisionService
from hesabyar.application.queries.accounts import AccountQueryService
from hesabyar.application.queries.bank import BankQueryService
from hesabyar.application.queries.cash_forecast import CashForecastQueryService
from hesabyar.application.queries.cashbox import CashBoxQueryService
from hesabyar.application.queries.cheques import ChequeQueryService
from hesabyar.application.queries.companies import CompanyQueryService
from hesabyar.application.queries.fiscal import FiscalQueryService, SetupStatusService
from hesabyar.application.queries.installments import InstallmentQueryService
from hesabyar.application.queries.moadian import MoadianQueryService
from hesabyar.application.queries.packaging import PackagingQueryService
from hesabyar.application.queries.payments import PaymentQueryService
from hesabyar.application.queries.reports import ReportQueryService
from hesabyar.application.queries.review_queue import ReviewQueueQueryService
from hesabyar.application.queries.settlement import SettlementQueryService
from hesabyar.application.queries.standards import StandardsQueryService
from hesabyar.application.queries.statements import BankStatementQueryService
from hesabyar.application.queries.tax_policy import TaxPolicyQueryService
from hesabyar.infrastructure.accounting.tryton.adapter import TrytonAccountingAdapter
from hesabyar.infrastructure.config import AppSettings
from hesabyar.interfaces.mcp.services import McpServices


def _services() -> McpServices:
    """Container over an unbound session: construction touches nothing."""
    return McpServices(Session(), AppSettings())


def test_engine_adapter_builds_lazily() -> None:
    services = _services()
    adapter = services.engine
    assert isinstance(adapter, TrytonAccountingAdapter)
    assert services.engine is adapter  # built once
    services.close()
    # Closing drops the adapter; a later access rebuilds it.
    assert isinstance(services.engine, TrytonAccountingAdapter)
    services.close()


def test_engine_backed_query_services() -> None:
    services = _services()
    assert isinstance(services.companies, CompanyQueryService)
    assert isinstance(services.accounts, AccountQueryService)
    assert isinstance(services.fiscal, FiscalQueryService)
    assert isinstance(services.setup_status, SetupStatusService)
    assert isinstance(services.reports, ReportQueryService)
    assert isinstance(services.payments, PaymentQueryService)
    assert isinstance(services.bank_statements, BankStatementQueryService)
    services.close()


def test_session_backed_query_services() -> None:
    services = _services()
    assert isinstance(services.standards, StandardsQueryService)
    assert isinstance(services.standards_validation, StandardsValidationService)
    assert isinstance(services.report_validation, ReportValidationService)
    assert isinstance(services.tax_policy, TaxPolicyQueryService)
    assert isinstance(services.tax_decisions, TaxDecisionService)
    assert isinstance(services.moadian, MoadianQueryService)
    assert isinstance(services.bank, BankQueryService)
    assert isinstance(services.packaging, PackagingQueryService)
    assert isinstance(services.cashbox, CashBoxQueryService)
    assert isinstance(services.settlement, SettlementQueryService)
    assert isinstance(services.cheques, ChequeQueryService)
    assert isinstance(services.installments, InstallmentQueryService)
    assert isinstance(services.cash_forecast, CashForecastQueryService)
    assert isinstance(services.review_queue, ReviewQueueQueryService)
    services.close()


def test_draft_services() -> None:
    services = _services()
    assert isinstance(services.journal_drafts, JournalDraftService)
    assert isinstance(services.invoice_drafts, InvoiceDraftService)
    assert isinstance(services.moadian_documents, MoadianDocumentService)
    services.close()


def test_services_are_cached_per_container() -> None:
    services = _services()
    assert services.companies is services.companies
    assert services.standards is services.standards
    assert services.settlement is services.settlement
    services.close()
