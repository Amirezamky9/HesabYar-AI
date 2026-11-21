"""MCP application-service wiring (US-601, spec §0 rules 4+6, §13.3).

The MCP server calls the SAME application query/command handlers as
REST — there is no separate implementation of trial balance, tax
calculation or posting for AI, and no MCP code touches PostgreSQL
directly. :class:`McpServices` is the per-call container that builds
those handlers, mirroring the ``get_*_service`` dependency functions
in ``interfaces/rest/dependencies.py`` (same classes, same ports, same
construction order) minus the FastAPI ``Depends`` wrappers.

The container is built inside a tool handler from the call's session
and closed when the call ends. The Tryton engine adapter is built
lazily (the first engine-backed service) and closed by :meth:`close`.

All services here are SYNCHRONOUS (the Tryton client is sync httpx,
the repositories are sync SQLAlchemy). Tool handlers must run service
calls via ``asyncio.to_thread`` — never call them directly on the
event loop.
"""

from __future__ import annotations

from functools import cached_property

from sqlalchemy.orm import Session

from hesabyar.application.commands.bank_reconcile import BankReconcileService
from hesabyar.application.commands.invoices import InvoiceDraftService
from hesabyar.application.commands.journals import JournalDraftService
from hesabyar.application.commands.moadian import (
    MoadianDocumentService,
    MoadianProfileService,
)
from hesabyar.application.commands.reports import ReportValidationService
from hesabyar.application.commands.review_queue import ReviewQueueCommandService
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
from hesabyar.application.queries.integrations import IntegrationsQueryService
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
from hesabyar.infrastructure.accounting.tryton.client import TrytonRestClient
from hesabyar.infrastructure.config import AppSettings
from hesabyar.infrastructure.persistence.postgres.bank_repository import (
    SqlAlchemyBankRepository,
)
from hesabyar.infrastructure.persistence.postgres.bank_rules_repository import (
    SqlAlchemyBankRulesRepository,
)
from hesabyar.infrastructure.persistence.postgres.cashbox_repository import SqlAlchemyCashBoxRepository
from hesabyar.infrastructure.persistence.postgres.cheques_repository import (
    SqlAlchemyChequeRepository,
)
from hesabyar.infrastructure.persistence.postgres.installments_repository import (
    SqlAlchemyInstallmentRepository,
)
from hesabyar.infrastructure.persistence.postgres.integrations_repository import (
    SqlAlchemyIntegrationsRepository,
)
from hesabyar.infrastructure.persistence.postgres.moadian_repository import SqlAlchemyMoadianRepository
from hesabyar.infrastructure.persistence.postgres.packaging_repository import (
    SqlAlchemyPackagingRepository,
)
from hesabyar.infrastructure.persistence.postgres.report_snapshot_repository import (
    SqlAlchemyReportSnapshotRepository,
)
from hesabyar.infrastructure.persistence.postgres.review_queue_repository import (
    SqlAlchemyReviewQueueRepository,
)
from hesabyar.infrastructure.persistence.postgres.settlement_repository import (
    SqlAlchemySettlementRepository,
)
from hesabyar.infrastructure.persistence.postgres.standards_repository import PostgresStandardsRegistry
from hesabyar.infrastructure.persistence.postgres.tax_policy_repository import (
    SqlAlchemyTaxPolicyRepository,
)


class McpServices:
    """Per-call application services for one MCP tool invocation."""

    def __init__(self, session: Session, settings: AppSettings) -> None:
        self._session = session
        self._settings = settings
        self._tryton_client: TrytonRestClient | None = None
        self._adapter: TrytonAccountingAdapter | None = None

    # -- engine adapter (lazy; closed by close()) ------------------------

    @property
    def engine(self) -> TrytonAccountingAdapter:
        """Tryton adapter, built on first use (same as REST's get_engine_adapter)."""
        if self._adapter is None:
            tryton = self._settings.tryton
            client = TrytonRestClient(
                tryton.base_url,
                tryton.database,
                tryton.api_key,
                timeout=tryton.timeout_seconds,
            )
            self._tryton_client = client
            self._adapter = TrytonAccountingAdapter(client)
        return self._adapter

    def close(self) -> None:
        """Close the Tryton client, if one was built for this call."""
        if self._tryton_client is not None:
            self._tryton_client.close()
            self._tryton_client = None
            self._adapter = None

    # -- repositories (one instance per container, sharing the session) --

    @cached_property
    def _bank_repository(self) -> SqlAlchemyBankRepository:
        return SqlAlchemyBankRepository(self._session)

    @cached_property
    def _bank_rules_repository(self) -> SqlAlchemyBankRulesRepository:
        return SqlAlchemyBankRulesRepository(self._session)

    @cached_property
    def _cashbox_repository(self) -> SqlAlchemyCashBoxRepository:
        return SqlAlchemyCashBoxRepository(self._session)

    @cached_property
    def _cheque_repository(self) -> SqlAlchemyChequeRepository:
        return SqlAlchemyChequeRepository(self._session)

    @cached_property
    def _installment_repository(self) -> SqlAlchemyInstallmentRepository:
        return SqlAlchemyInstallmentRepository(self._session)

    @cached_property
    def _integrations_repository(self) -> SqlAlchemyIntegrationsRepository:
        return SqlAlchemyIntegrationsRepository(self._session)

    @cached_property
    def _moadian_repository(self) -> SqlAlchemyMoadianRepository:
        return SqlAlchemyMoadianRepository(self._session)

    @cached_property
    def _packaging_repository(self) -> SqlAlchemyPackagingRepository:
        return SqlAlchemyPackagingRepository(self._session)

    @cached_property
    def _review_queue_repository(self) -> SqlAlchemyReviewQueueRepository:
        return SqlAlchemyReviewQueueRepository(self._session)

    @cached_property
    def _settlement_repository(self) -> SqlAlchemySettlementRepository:
        return SqlAlchemySettlementRepository(self._session)

    @cached_property
    def _standards_registry(self) -> PostgresStandardsRegistry:
        return PostgresStandardsRegistry(self._session)

    @cached_property
    def _tax_policy_repository(self) -> SqlAlchemyTaxPolicyRepository:
        return SqlAlchemyTaxPolicyRepository(self._session)

    # -- engine-backed query services (US-602 read-only tools) ------------

    @cached_property
    def companies(self) -> CompanyQueryService:
        return CompanyQueryService(self.engine)

    @cached_property
    def accounts(self) -> AccountQueryService:
        return AccountQueryService(self.engine)

    @cached_property
    def fiscal(self) -> FiscalQueryService:
        return FiscalQueryService(self.engine)

    @cached_property
    def setup_status(self) -> SetupStatusService:
        return SetupStatusService(self.engine)

    @cached_property
    def reports(self) -> ReportQueryService:
        return ReportQueryService(self.engine)

    @cached_property
    def payments(self) -> PaymentQueryService:
        return PaymentQueryService(self.engine)

    @cached_property
    def bank_statements(self) -> BankStatementQueryService:
        return BankStatementQueryService(self.engine)

    # -- session-backed query services ------------------------------------

    @cached_property
    def standards(self) -> StandardsQueryService:
        return StandardsQueryService(self._standards_registry)

    @cached_property
    def standards_validation(self) -> StandardsValidationService:
        return StandardsValidationService(self._standards_registry)

    @cached_property
    def report_validation(self) -> ReportValidationService:
        return ReportValidationService(
            self.engine,
            self.standards_validation,
            SqlAlchemyReportSnapshotRepository(self._session),
        )

    @cached_property
    def tax_policy(self) -> TaxPolicyQueryService:
        return TaxPolicyQueryService(self._tax_policy_repository)

    @cached_property
    def tax_decisions(self) -> TaxDecisionService:
        return TaxDecisionService(self._tax_policy_repository)

    @cached_property
    def moadian(self) -> MoadianQueryService:
        return MoadianQueryService(self._moadian_repository)

    @cached_property
    def integrations(self) -> IntegrationsQueryService:
        """Integration registry queries (US-604 status resource)."""
        return IntegrationsQueryService(self._integrations_repository)

    @cached_property
    def bank(self) -> BankQueryService:
        return BankQueryService(self.engine, self._bank_repository)

    @cached_property
    def packaging(self) -> PackagingQueryService:
        return PackagingQueryService(self.engine, self._packaging_repository)

    @cached_property
    def cashbox(self) -> CashBoxQueryService:
        return CashBoxQueryService(self._cashbox_repository, self.engine)

    @cached_property
    def settlement(self) -> SettlementQueryService:
        return SettlementQueryService(self._settlement_repository, self._integrations_repository)

    @cached_property
    def cheques(self) -> ChequeQueryService:
        return ChequeQueryService(self._cheque_repository, self.engine)

    @cached_property
    def installments(self) -> InstallmentQueryService:
        return InstallmentQueryService(
            self._installment_repository,
            self._cheque_repository,
            settlement_repository=self._settlement_repository,
        )

    @cached_property
    def cash_forecast(self) -> CashForecastQueryService:
        return CashForecastQueryService(
            self.engine,
            self._settlement_repository,
            self._installment_repository,
            self._cheque_repository,
            self._packaging_repository,
        )

    @cached_property
    def review_queue(self) -> ReviewQueueQueryService:
        # One repository implements both ports (same as REST).
        return ReviewQueueQueryService(self._review_queue_repository, self._review_queue_repository)

    # -- draft services (US-605 draft-only tools; drafts only, never post) --

    @cached_property
    def journal_drafts(self) -> JournalDraftService:
        return JournalDraftService(self.engine)

    @cached_property
    def invoice_drafts(self) -> InvoiceDraftService:
        return InvoiceDraftService(self.engine)

    @cached_property
    def moadian_documents(self) -> MoadianDocumentService:
        return MoadianDocumentService(self._moadian_repository)

    @cached_property
    def moadian_profiles(self) -> MoadianProfileService:
        return MoadianProfileService(self._moadian_repository)

    @cached_property
    def _review_queue_commands(self) -> ReviewQueueCommandService:
        """Review-queue writes for the reconcile service (filing-only).

        Built without the DLQ replay / mapping effect seams: the
        reconcile service only files items and performs the
        decision-only ``manual_match`` action on superseded items,
        both of which work without them (same as other modules'
        filing-only use). A replay/mapping action would fail loudly
        instead of pretending to run.
        """
        # One repository implements both ports (same as REST).
        return ReviewQueueCommandService(
            self._review_queue_repository, self._review_queue_repository
        )

    @cached_property
    def bank_reconcile(self) -> BankReconcileService:
        """Bank-statement reconciliation (US-514 service, same as REST).

        Never validates or posts the statement: the accountant
        validates in the engine. Wired with the W3 bank-rules
        repository so user-defined rules fire exactly as they do
        from the dashboard.
        """
        return BankReconcileService(
            self.engine,
            self._bank_repository,
            self._review_queue_commands,
            self._review_queue_repository,
            self._integrations_repository,
            bank_rules=self._bank_rules_repository,
        )
