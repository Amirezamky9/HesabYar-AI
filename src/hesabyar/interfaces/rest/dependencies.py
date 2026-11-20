"""Shared FastAPI dependencies for the REST API (US-101, US-103).

``get_settings_dep`` and ``get_health_service`` are the override points
tests use (``app.dependency_overrides[...]``); production code never
overrides them. ``get_authenticated_client`` is the equivalent override
point for auth: tests that do not exercise auth itself override it with
a canned :class:`AuthenticatedClient`.

Authentication (spec §7.5, §14.1/§14.2)
----------------------------------------
Clients authenticate with an API key in the ``Authorization`` header,
``Bearer`` scheme — the same header scheme the Tryton REST API uses, so
integrations carry one auth convention across both APIs, and FastAPI's
``HTTPBearer`` documents it in the generated OpenAPI security scheme.
(The spec does not mandate a header; ``Authorization: Bearer`` was
chosen over a custom ``X-API-Key`` header for exactly that reason.)

- Missing, malformed, unknown, revoked or expired key, or a key whose
  client is inactive → **401 UNAUTHORIZED**, always with the same
  generic detail so callers cannot probe key state.
- Authenticated key lacking a required scope, or restricted to a
  different company → **403 FORBIDDEN**.
- Every failure is a typed problem-details error (``ApiError``), never a
  bare ``HTTPException`` and never a body containing key material.

Endpoint authors use :func:`require_scopes` (and, for company-scoped
routes, :func:`require_company_access`); see ``docs/api-key-auth.md``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Generator

from fastapi import Depends, Path, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import create_engine, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from hesabyar.application.commands.bank_import import BankImportService
from hesabyar.application.commands.bank_reconcile import BankReconcileService
from hesabyar.application.commands.cashbox import CashBoxCommandService
from hesabyar.application.commands.cheque_clearing import ChequeClearingService
from hesabyar.application.commands.cheque_intake import SiteChequeIntakeHandler
from hesabyar.application.commands.cheques import ChequeCommandService
from hesabyar.application.commands.events import (
    DlqReplayService,
    EventIngestService,
    EventPipelineService,
    EventWorker,
    StalenessService,
    StoreSettingsService,
)
from hesabyar.application.commands.gateways import GatewayReconciliationService
from hesabyar.application.commands.integrations import (
    BankConnectionService,
    ChannelRegistryService,
    GatewayContractService,
    InstallationSettingsService,
    MappingService,
    PolicyGateConfigService,
    PolicyGateService,
    StoreService,
)
from hesabyar.application.commands.invoices import InvoiceDraftService
from hesabyar.application.commands.journals import JournalDraftService
from hesabyar.application.commands.manual_entries import ManualEntryService
from hesabyar.application.commands.moadian import (
    MoadianDocumentService,
    MoadianProfileService,
    MoadianReconciliationWorker,
    MoadianSubmissionService,
)
from hesabyar.application.commands.packaging import PackagingCommandService
from hesabyar.application.commands.policy_gate import RegistryPolicyGate
from hesabyar.application.commands.reports import ReportValidationService
from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.application.commands.sayyad import SayyadInquiryService
from hesabyar.application.commands.settlement import SettlementCommandService
from hesabyar.application.commands.settlement_import import SettlementImportService
from hesabyar.application.commands.settlement_posting import SettlementPostingService
from hesabyar.application.commands.standards import StandardsValidationService
from hesabyar.application.commands.store_events import StoreDraftBuilder, StoreMappingResolver
from hesabyar.application.commands.sync import StoreSyncService
from hesabyar.application.commands.tax_policy import TaxDecisionService
from hesabyar.application.queries.accounts import AccountQueryService
from hesabyar.application.queries.bank import BankQueryService
from hesabyar.application.queries.cashbox import CashBoxQueryService
from hesabyar.application.queries.cheques import ChequeQueryService
from hesabyar.application.queries.companies import CompanyQueryService
from hesabyar.application.queries.events import EventQueryService
from hesabyar.application.queries.fiscal import FiscalQueryService, SetupStatusService
from hesabyar.application.queries.integrations import IntegrationsQueryService
from hesabyar.application.queries.manual_entries import ManualEntryQueryService
from hesabyar.application.queries.moadian import MoadianQueryService
from hesabyar.application.queries.packaging import PackagingQueryService
from hesabyar.application.queries.payments import PaymentQueryService
from hesabyar.application.queries.reports import ReportQueryService
from hesabyar.application.queries.review_queue import ReviewQueueQueryService
from hesabyar.application.queries.settlement import SettlementQueryService
from hesabyar.application.queries.standards import StandardsQueryService
from hesabyar.application.queries.statements import BankStatementQueryService
from hesabyar.application.queries.tax_policy import TaxPolicyQueryService
from hesabyar.domain.accounting.refs import CompanyRef
from hesabyar.domain.events.ports import JobQueuePort
from hesabyar.domain.integrations.ports import CredentialVaultPort, FiscalPeriodCheckerPort
from hesabyar.domain.moadian.ports import MoadianRepositoryPort, MoadianSimulatorPort
from hesabyar.domain.settlement.ports import SettlementPostingPort
from hesabyar.infrastructure.accounting.tryton.adapter import TrytonAccountingAdapter
from hesabyar.infrastructure.accounting.tryton.client import TrytonRestClient
from hesabyar.infrastructure.accounting.tryton.fiscal_period_checker import (
    TrytonFiscalPeriodChecker,
)
from hesabyar.infrastructure.accounting.tryton.settlement_posting import (
    EngineSettlementPosting,
)
from hesabyar.infrastructure.auth.credential_vault import FernetCredentialVault
from hesabyar.infrastructure.auth.errors import AuthenticationError
from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.infrastructure.auth.keys import ApiKeyCrypto
from hesabyar.infrastructure.auth.service import ApiKeyService
from hesabyar.infrastructure.bank.file_readers import StatementFileReader
from hesabyar.infrastructure.config import AppSettings, get_settings
from hesabyar.infrastructure.events.defaults import (
    InboxReviewQueueAdapter,
    InProcessOutboxPublisher,
)
from hesabyar.infrastructure.events.procrastinate_queue import (
    ProcrastinateJobQueue,
    create_connector,
    create_defer_app,
)
from hesabyar.infrastructure.events.store_secrets import RegistryWebhookSecretStore
from hesabyar.infrastructure.files.local_scan_store import LocalChequeScanStore
from hesabyar.infrastructure.gateways.factory import GatewayClientFactory
from hesabyar.infrastructure.moadian.simulator import DeterministicMoadianSimulator
from hesabyar.infrastructure.persistence.postgres.bank_repository import (
    SqlAlchemyBankRepository,
)
from hesabyar.infrastructure.persistence.postgres.cashbox_repository import SqlAlchemyCashBoxRepository
from hesabyar.infrastructure.persistence.postgres.cheques_repository import (
    SqlAlchemyChequeRepository,
)
from hesabyar.infrastructure.persistence.postgres.event_repositories import (
    SqlAlchemyDecisionAuditLogRepository,
    SqlAlchemyEventIdempotencyStore,
    SqlAlchemyEventInboxRepository,
    SqlAlchemyEventOutboxRepository,
    SqlAlchemyExternalIdMapRepository,
    SqlAlchemyStoreSettingsRepository,
)
from hesabyar.infrastructure.persistence.postgres.integrations_repository import (
    SqlAlchemyIntegrationsRepository,
)
from hesabyar.infrastructure.persistence.postgres.manual_entry_repository import (
    SqlAlchemyManualEntryRepository,
)
from hesabyar.infrastructure.persistence.postgres.moadian_repository import SqlAlchemyMoadianRepository
from hesabyar.infrastructure.persistence.postgres.models import EngineCompanyModel
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
    SqlAlchemySettlementAudit,
    SqlAlchemySettlementRepository,
)
from hesabyar.infrastructure.persistence.postgres.standards_repository import PostgresStandardsRegistry
from hesabyar.infrastructure.persistence.postgres.tax_policy_repository import (
    SqlAlchemyTaxPolicyRepository,
)
from hesabyar.infrastructure.sayyad.simulator import (
    DeterministicEhrazinoSimulator,
    DeterministicFintechSayyadSimulator,
)
from hesabyar.infrastructure.store_connectors.factory import StoreConnectorFactory
from hesabyar.interfaces.rest.errors import ApiError
from hesabyar.interfaces.rest.services import HealthService

# Bearer scheme, documented in OpenAPI. auto_error=False so missing/invalid
# credentials reach our handler and become the typed 401 problem envelope
# instead of FastAPI's default 403.
_API_KEY_SCHEME = HTTPBearer(
    auto_error=False,
    scheme_name="HesabYar API key",
    description="HesabYar API key as a Bearer token: 'Authorization: Bearer hya_live_…'",
)


def get_settings_dep() -> AppSettings:
    """Return the cached application settings."""
    return get_settings()


def get_health_service(settings: AppSettings = Depends(get_settings_dep)) -> HealthService:
    """Build the (stateless) health service for this request."""
    return HealthService(settings)


def get_db_session(settings: AppSettings = Depends(get_settings_dep)) -> Generator[Session, None, None]:
    """Yield one short-lived SQLAlchemy session for the request.

    Like the health service's probes, this builds a short-lived engine
    per request (``NullPool`` — no pooling across requests). That is a
    deliberate Phase 1 simplification, matching US-101; a shared engine
    and session factory land with the connection-pool story.
    """
    engine = create_engine(settings.database.sqlalchemy_url, poolclass=NullPool)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        # Teardown must never fail the request: against a remote
        # database the connection can die between the last query and
        # the close (Supabase pooler: "SSL error: unexpected eof"),
        # and close() rolls back on the dead connection. The route
        # already committed its writes (or the dead connection drops
        # the uncommitted transaction on its own), so a teardown
        # failure is harmless — swallow it instead of 500ing a
        # response that is already built.
        try:
            session.close()
        except SQLAlchemyError:
            pass
        try:
            engine.dispose()
        except SQLAlchemyError:
            pass


def get_api_key_service(settings: AppSettings = Depends(get_settings_dep)) -> ApiKeyService:
    """Build the API-key service for this request (pepper from settings)."""
    return ApiKeyService(ApiKeyCrypto(settings.auth.api_key_pepper))


def get_authenticated_client(
    credentials: HTTPAuthorizationCredentials | None = Depends(_API_KEY_SCHEME),
    session: Session = Depends(get_db_session),
    service: ApiKeyService = Depends(get_api_key_service),
) -> AuthenticatedClient:
    """Authenticate the request's API key (no scope requirement).

    Raises 401 for every failure mode with one generic message. On
    success, commits the session so the key's ``last_used_at`` update
    (flushed by the service) is persisted.
    """
    if credentials is None:
        raise ApiError(
            status_code=401,
            code="UNAUTHORIZED",
            detail="A valid API key is required.",
        )
    try:
        client = service.authenticate(session, secret=credentials.credentials)
    except AuthenticationError:
        # The service logs the reason + prefix server-side; the client
        # sees the same generic 401 for every failure mode.
        raise ApiError(
            status_code=401,
            code="UNAUTHORIZED",
            detail="A valid API key is required.",
        ) from None
    session.commit()
    return client


def require_scopes(*scopes: str) -> Callable[[AuthenticatedClient], AuthenticatedClient]:
    """Return a dependency requiring the listed scopes (exact match).

    Usage: ``client: AuthenticatedClient = Depends(require_scopes("company:read"))``.
    Unauthenticated requests fail with 401 (from
    :func:`get_authenticated_client`); an authenticated key missing any
    listed scope fails with 403 naming the missing scope(s). There is no
    wildcard scope — an operation needs exactly the scopes it lists.
    """

    def _require_scopes(
        client: AuthenticatedClient = Depends(get_authenticated_client),
    ) -> AuthenticatedClient:
        missing = client.missing_scopes(*scopes)
        if missing:
            raise ApiError(
                status_code=403,
                code="FORBIDDEN",
                detail=f"The API key lacks the required scope(s): {', '.join(missing)}.",
            )
        return client

    return _require_scopes


def _find_company_row(session: Session, tenant_id: str, company_id: str) -> EngineCompanyModel | None:
    """Find a tenant's ``engine_companies`` row by row id or engine id.

    ``company_id`` matches either the HesabYar row id or the engine's
    external company id (both are valid API company ids). A row
    belonging to another tenant is invisible — the lookup is always
    tenant-scoped, so it looks exactly like a row that does not exist.
    """
    stmt = select(EngineCompanyModel).where(
        EngineCompanyModel.tenant_id == tenant_id,
        or_(
            EngineCompanyModel.id == company_id,
            EngineCompanyModel.external_company_id == company_id,
        ),
    )
    return session.execute(stmt).scalar_one_or_none()


def _require_company_row(session: Session, client: AuthenticatedClient, company_id: str) -> EngineCompanyModel:
    """Resolve ``company_id`` for the client, enforcing its restriction.

    Unknown company (or a company of another tenant) → 404
    ``COMPANY_NOT_FOUND``; a key restricted to a different company →
    403 ``FORBIDDEN``.
    """
    row = _find_company_row(session, client.tenant_id, company_id)
    if row is None:
        raise ApiError(
            status_code=404,
            code="COMPANY_NOT_FOUND",
            detail=f"Company {company_id!r} does not exist.",
        )
    if not client.can_access_company(row.id):
        raise ApiError(
            status_code=403,
            code="FORBIDDEN",
            detail="The API key is restricted to a different company.",
        )
    return row


def resolve_company_row(
    company_id: str = Path(..., description="API company id (path parameter)"),
    client: AuthenticatedClient = Depends(get_authenticated_client),
    session: Session = Depends(get_db_session),
) -> EngineCompanyModel:
    """Resolve a route's ``{company_id}`` path param to its mapping row.

    Accepts the HesabYar ``engine_companies`` row id or the engine's
    external company id, within the client's tenant, and enforces the
    key's company restriction (see :func:`_require_company_row`).
    """
    return _require_company_row(session, client, company_id)


def require_company_access(
    client: AuthenticatedClient = Depends(get_authenticated_client),
    _row: EngineCompanyModel = Depends(resolve_company_row),
) -> AuthenticatedClient:
    """Enforce company restriction for routes with a ``{company_id}`` path param.

    Resolves ``company_id`` to an ``engine_companies`` row within the
    client's tenant — matching either the HesabYar row id or the engine's
    external company id — then requires the key to be unrestricted or
    restricted to that row. Unknown company → 404; wrong company → 403.

    Routes that also need the resolved row (to call the engine) depend
    on :func:`resolve_company_row` directly instead.
    """
    return client


def get_tenant_company_rows(
    client: AuthenticatedClient = Depends(get_authenticated_client),
    session: Session = Depends(get_db_session),
) -> list[EngineCompanyModel]:
    """Return the ``engine_companies`` rows the client may see.

    All of the tenant's rows for an unrestricted key; only the bound row
    for a company-restricted key. The companies list endpoint joins the
    engine's companies against these rows, so a company with no mapping
    in this tenant is invisible to it.
    """
    stmt = select(EngineCompanyModel).where(EngineCompanyModel.tenant_id == client.tenant_id)
    if client.engine_company_id is not None:
        stmt = stmt.where(EngineCompanyModel.id == client.engine_company_id)
    return list(session.execute(stmt).scalars())


def resolve_accounts_company(
    company: str | None = Query(
        default=None,
        description=(
            "API company id (HesabYar row id or engine company id). "
            "Required unless the API key is restricted to one company, "
            "in which case that company is used."
        ),
    ),
    client: AuthenticatedClient = Depends(get_authenticated_client),
    session: Session = Depends(get_db_session),
) -> EngineCompanyModel:
    """Resolve the company an accounts endpoint operates on.

    Accounts are company-scoped, so the company comes from the
    ``company`` query parameter — or, when the parameter is absent, from
    the key's company restriction. Neither → typed 400
    ``COMPANY_REQUIRED``. Unknown company → 404; a company the key is
    restricted away from → 403.
    """
    if company is None:
        if client.engine_company_id is None:
            raise ApiError(
                status_code=400,
                code="COMPANY_REQUIRED",
                detail=(
                    "Query parameter 'company' is required: accounts are "
                    "company-scoped, and this API key is not restricted "
                    "to one company."
                ),
            )
        row = session.get(EngineCompanyModel, client.engine_company_id)
        if row is None or row.tenant_id != client.tenant_id:
            raise ApiError(
                status_code=404,
                code="COMPANY_NOT_FOUND",
                detail="Company restricted to this API key does not exist.",
            )
        return row
    return _require_company_row(session, client, company)


def company_ref_for_row(row: EngineCompanyModel) -> CompanyRef:
    """Return the engine reference for a mapping row.

    The row's ``external_company_id`` is the engine company id as a
    string; a value that is not an integer means the mapping itself is
    corrupt, which is a 500 — the client cannot fix it.
    """
    try:
        engine_id = int(row.external_company_id)
    except ValueError:
        raise ApiError(
            status_code=500,
            code="INTERNAL_ERROR",
            detail="The company mapping is invalid. Please contact support.",
        ) from None
    return CompanyRef(engine_id)


#: Admin endpoints require the ``settings:manage`` scope (US-103 replaces
#: the US-101 placeholder admin token with real API-key auth).
require_admin = require_scopes("settings:manage")


# -- Query services (US-104 use cases over the real engine adapter) --------


def get_engine_adapter(
    settings: AppSettings = Depends(get_settings_dep),
) -> Generator[TrytonAccountingAdapter, None, None]:
    """Build the Tryton adapter for this request; closed when it ends.

    Like the health probes, this builds a short-lived client per request
    (no pooling across requests — a shared engine lands with the
    connection-pool story).
    """
    tryton = settings.tryton
    with TrytonRestClient(
        tryton.base_url,
        tryton.database,
        tryton.api_key,
        timeout=tryton.timeout_seconds,
    ) as client:
        yield TrytonAccountingAdapter(client)


def get_company_query_service(
    adapter: TrytonAccountingAdapter = Depends(get_engine_adapter),
) -> CompanyQueryService:
    """Build the company query service for this request (override point for tests)."""
    return CompanyQueryService(adapter)


def get_account_query_service(
    adapter: TrytonAccountingAdapter = Depends(get_engine_adapter),
) -> AccountQueryService:
    """Build the account query service for this request (override point for tests)."""
    return AccountQueryService(adapter)


def get_fiscal_query_service(
    adapter: TrytonAccountingAdapter = Depends(get_engine_adapter),
) -> FiscalQueryService:
    """Build the fiscal query service for this request (override point for tests)."""
    return FiscalQueryService(adapter)


def get_setup_status_service(
    adapter: TrytonAccountingAdapter = Depends(get_engine_adapter),
) -> SetupStatusService:
    """Build the setup-status service for this request (override point for tests)."""
    return SetupStatusService(adapter)


# -- Journal draft service + company helpers (US-202) -----------------------


def get_journal_draft_service(
    adapter: TrytonAccountingAdapter = Depends(get_engine_adapter),
) -> JournalDraftService:
    """Build the journal draft service for this request (override point for tests)."""
    return JournalDraftService(adapter)


def require_company_row_by_id(session: Session, client: AuthenticatedClient, company_id: str) -> EngineCompanyModel:
    """Resolve a body-supplied company id to its mapping row.

    Same contract as :func:`_require_company_row` (unknown company or a
    company of another tenant → 404 ``COMPANY_NOT_FOUND``; a key
    restricted to a different company → 403), but callable from routes
    whose company comes from the request body rather than a path or
    query parameter.
    """
    return _require_company_row(session, client, company_id)


def find_company_row_for_engine_id(
    session: Session, tenant_id: str, engine_company_id: int
) -> EngineCompanyModel | None:
    """Find a tenant's mapping row for an engine company id, or None.

    Journal routes read the company from the engine record itself (the
    draft's company), then use this to check the company is mapped to
    the caller's tenant. A missing row means the record is invisible to
    this tenant — callers answer 404 as if the record did not exist.
    """
    stmt = select(EngineCompanyModel).where(
        EngineCompanyModel.tenant_id == tenant_id,
        EngineCompanyModel.external_company_id == str(engine_company_id),
    )
    return session.execute(stmt).scalar_one_or_none()


def require_draft_company_access(
    session: Session, client: AuthenticatedClient, company: CompanyRef
) -> EngineCompanyModel | None:
    """Return the mapping row for a draft's company, or None when invisible.

    None means the company has no mapping row in the client's tenant —
    the draft looks exactly like a draft that does not exist. A key
    restricted to a different company raises 403 ``FORBIDDEN``.
    """
    row = find_company_row_for_engine_id(session, client.tenant_id, company.engine_id)
    if row is None:
        return None
    if not client.can_access_company(row.id):
        raise ApiError(
            status_code=403,
            code="FORBIDDEN",
            detail="The API key is restricted to a different company.",
        )
    return row


# -- Record-company helpers (US-205) ---------------------------------------
#
# Get-by-id endpoints read the company from the engine record itself
# (invoices, payments, statements), like the journal endpoints do. The
# journal router keeps its own private copy of this helper; new routers
# use the shared one here.


def require_visible_company(
    session: Session,
    client: AuthenticatedClient,
    company: CompanyRef,
    resource_id: int,
    *,
    resource_kind: str,
    error_code: str,
) -> EngineCompanyModel:
    """Resolve a record's company to its tenant mapping row.

    A company with no mapping row in the caller's tenant means the
    record is invisible: 404 as if the record did not exist. A key
    restricted to a different company gets 403 (from
    :func:`require_draft_company_access`).
    """
    row = require_draft_company_access(session, client, company)
    if row is None:
        raise ApiError(
            status_code=404,
            code=error_code,
            detail=f"{resource_kind} {resource_id} does not exist in the engine.",
        )
    return row


def external_company_id_for_param(session: Session, tenant_id: str, company_param: str) -> str:
    """Resolve a company filter value to the engine company id it names.

    Accepts the HesabYar ``engine_companies`` row id (resolved through
    the tenant's mapping rows) or an engine company id (used as-is).
    A value naming no row of this tenant is passed through unchanged —
    it then matches nothing, exactly like an unknown engine id.
    """
    row = _find_company_row(session, tenant_id, company_param)
    if row is not None:
        return row.external_company_id
    return company_param


def resolve_audit_company(session: Session, client: AuthenticatedClient, company_param: str | None) -> str | None:
    """Resolve the audit-trail ``company`` filter for the client.

    Returns the engine company id to filter on, or None for no filter.
    A company-restricted key is forced to its own company: with no
    parameter the filter is that company, and a parameter naming a
    different company is 403 ``FORBIDDEN``.
    """
    forced: str | None = None
    if client.engine_company_id is not None:
        row = session.get(EngineCompanyModel, client.engine_company_id)
        if row is None or row.tenant_id != client.tenant_id:
            raise ApiError(
                status_code=404,
                code="COMPANY_NOT_FOUND",
                detail="Company restricted to this API key does not exist.",
            )
        forced = row.external_company_id
    if company_param is None:
        return forced
    resolved = external_company_id_for_param(session, client.tenant_id, company_param)
    if forced is not None and resolved != forced:
        raise ApiError(
            status_code=403,
            code="FORBIDDEN",
            detail="The API key is restricted to a different company.",
        )
    return resolved


# -- Invoice / payment / statement services (US-205) -----------------------


def get_invoice_draft_service(
    adapter: TrytonAccountingAdapter = Depends(get_engine_adapter),
) -> InvoiceDraftService:
    """Build the invoice draft service for this request (override point for tests)."""
    return InvoiceDraftService(adapter)


def get_payment_query_service(
    adapter: TrytonAccountingAdapter = Depends(get_engine_adapter),
) -> PaymentQueryService:
    """Build the payment query service for this request (override point for tests)."""
    return PaymentQueryService(adapter)


def get_bank_statement_query_service(
    adapter: TrytonAccountingAdapter = Depends(get_engine_adapter),
) -> BankStatementQueryService:
    """Build the bank-statement query service for this request (override point for tests)."""
    return BankStatementQueryService(adapter)


# -- Standards registry services (US-401) ----------------------------------


def get_standards_query_service(
    session: Session = Depends(get_db_session),
) -> StandardsQueryService:
    """Build the standards query service for this request (override point for tests)."""
    return StandardsQueryService(PostgresStandardsRegistry(session))


def get_standards_validation_service(
    session: Session = Depends(get_db_session),
) -> StandardsValidationService:
    """Build the standards validation service for this request (override point for tests)."""
    return StandardsValidationService(PostgresStandardsRegistry(session))


# -- Report validation services (US-403) ---------------------------------


def get_report_snapshot_repository(
    session: Session = Depends(get_db_session),
) -> SqlAlchemyReportSnapshotRepository:
    """Build the SQLAlchemy report-snapshot repository for this request."""
    return SqlAlchemyReportSnapshotRepository(session)


def get_report_query_service(
    adapter: TrytonAccountingAdapter = Depends(get_engine_adapter),
) -> ReportQueryService:
    """Build the report query service for this request (override point for tests)."""
    return ReportQueryService(adapter)


def get_report_validation_service(
    adapter: TrytonAccountingAdapter = Depends(get_engine_adapter),
    session: Session = Depends(get_db_session),
) -> ReportValidationService:
    """Build the report validation service for this request (override point for tests)."""
    return ReportValidationService(
        adapter,
        StandardsValidationService(PostgresStandardsRegistry(session)),
        SqlAlchemyReportSnapshotRepository(session),
    )


# -- Tax-policy registry services (US-402) ---------------------------------


def get_tax_policy_repository(
    session: Session = Depends(get_db_session),
) -> SqlAlchemyTaxPolicyRepository:
    """Build the SQLAlchemy tax-policy repository for this request."""
    return SqlAlchemyTaxPolicyRepository(session)


def get_tax_policy_query_service(
    repository: SqlAlchemyTaxPolicyRepository = Depends(get_tax_policy_repository),
) -> TaxPolicyQueryService:
    """Build the tax-policy query service for this request (override point for tests)."""
    return TaxPolicyQueryService(repository)


def get_tax_decision_service(
    repository: SqlAlchemyTaxPolicyRepository = Depends(get_tax_policy_repository),
) -> TaxDecisionService:
    """Build the tax decision service for this request (override point for tests)."""
    return TaxDecisionService(repository)


# -- Moadian services (US-404) ---------------------------------------------


def get_moadian_repository(
    session: Session = Depends(get_db_session),
) -> MoadianRepositoryPort:
    """Build the Moadian repository for this request (override point for tests).

    The repository shares the request session (FastAPI caches
    ``get_db_session`` per request), so the router's commit covers the
    services' flushed writes.
    """
    return SqlAlchemyMoadianRepository(session)


def get_moadian_query_service(
    repository: MoadianRepositoryPort = Depends(get_moadian_repository),
) -> MoadianQueryService:
    """Build the Moadian query service for this request (override point for tests)."""
    return MoadianQueryService(repository)


def get_moadian_profile_service(
    repository: MoadianRepositoryPort = Depends(get_moadian_repository),
) -> MoadianProfileService:
    """Build the Moadian profile service for this request (override point for tests)."""
    return MoadianProfileService(repository)


def get_moadian_document_service(
    repository: MoadianRepositoryPort = Depends(get_moadian_repository),
) -> MoadianDocumentService:
    """Build the Moadian document service for this request (override point for tests)."""
    return MoadianDocumentService(repository)


def get_moadian_submission_service(
    repository: MoadianRepositoryPort = Depends(get_moadian_repository),
) -> MoadianSubmissionService:
    """Build the Moadian submission service for this request (override point for tests)."""
    return MoadianSubmissionService(repository)


def get_moadian_simulator() -> MoadianSimulatorPort:
    """Build the deterministic Moadian simulator for this request.

    A fresh in-memory instance per request: the database is the
    durable record, and the reconciliation worker re-seeds the
    simulator from it on every pass. No network calls — ever.
    """
    return DeterministicMoadianSimulator()


def get_moadian_reconciliation_worker(
    repository: MoadianRepositoryPort = Depends(get_moadian_repository),
    simulator: MoadianSimulatorPort = Depends(get_moadian_simulator),
) -> MoadianReconciliationWorker:
    """Build the Moadian reconciliation worker for this request (override point for tests)."""
    return MoadianReconciliationWorker(repository, simulator)


# -- Event-platform services (US-501) --------------------------------------


def get_event_inbox_repository(
    session: Session = Depends(get_db_session),
) -> SqlAlchemyEventInboxRepository:
    """Build the event inbox repository for this request."""
    return SqlAlchemyEventInboxRepository(session)


def get_event_outbox_repository(
    session: Session = Depends(get_db_session),
) -> SqlAlchemyEventOutboxRepository:
    """Build the event outbox repository for this request."""
    return SqlAlchemyEventOutboxRepository(session)


def get_event_idempotency_store(
    session: Session = Depends(get_db_session),
) -> SqlAlchemyEventIdempotencyStore:
    """Build the event idempotency store for this request."""
    return SqlAlchemyEventIdempotencyStore(session)


def get_external_id_map_repository(
    session: Session = Depends(get_db_session),
) -> SqlAlchemyExternalIdMapRepository:
    """Build the external-id-map repository for this request."""
    return SqlAlchemyExternalIdMapRepository(session)


def get_decision_audit_log_repository(
    session: Session = Depends(get_db_session),
) -> SqlAlchemyDecisionAuditLogRepository:
    """Build the decision audit-log repository for this request."""
    return SqlAlchemyDecisionAuditLogRepository(session)


def get_store_settings_repository(
    session: Session = Depends(get_db_session),
) -> SqlAlchemyStoreSettingsRepository:
    """Build the store-settings repository for this request."""
    return SqlAlchemyStoreSettingsRepository(session)


# Defer-only Procrastinate apps, one per database URL. The API process
# only defers jobs (it never runs tasks); the app is cached so a
# connector is not rebuilt per request. A failed defer is logged by
# the queue and never fails a request — the sweep is the backstop.
logger = logging.getLogger("hesabyar.interfaces.rest.dependencies")

_DEFER_APPS: dict[str, ProcrastinateJobQueue] = {}


def get_job_queue(settings: AppSettings = Depends(get_settings_dep)) -> JobQueuePort:
    """Build the job queue for this request (override point for tests).

    The defer app is opened once per process (cached by DB URL): an
    unopened Procrastinate app cannot defer jobs, and every defer
    would fail — caught by the best-effort queue, but the one-shot
    triggers would silently never fire. Opening is best-effort: if
    the queue is unreachable, the queue is still returned and defers
    fail soft (the periodic sweep is the backstop) instead of
    breaking ingest.
    """
    url = settings.database.sqlalchemy_url
    queue = _DEFER_APPS.get(url)
    if queue is None:
        app = create_defer_app(create_connector(url))
        try:
            app.open()
        except Exception:
            logger.warning(
                "Could not open the Procrastinate defer app; defers will fail soft.",
                exc_info=True,
            )
        queue = ProcrastinateJobQueue(app)
        _DEFER_APPS[url] = queue
    return queue


def _webhook_vault_or_none(settings: AppSettings) -> CredentialVaultPort | None:
    """Build the credential vault for webhook verification, or None.

    Unlike :func:`get_credential_vault` (which 503s on a malformed
    key), a malformed or missing key here returns None: the ingest
    service only needs the vault when a store actually HAS a secret,
    and then the secret store raises the typed vault-unavailable
    error. Stores without secrets keep ingesting.
    """
    key = settings.auth.credentials_encryption_key
    if key is None:
        return None
    try:
        return FernetCredentialVault(key)
    except ValueError:
        return None


def get_event_ingest_service(
    inbox: SqlAlchemyEventInboxRepository = Depends(get_event_inbox_repository),
    idempotency: SqlAlchemyEventIdempotencyStore = Depends(get_event_idempotency_store),
    job_queue: JobQueuePort = Depends(get_job_queue),
    session: Session = Depends(get_db_session),
    settings: AppSettings = Depends(get_settings_dep),
) -> EventIngestService:
    """Build the event ingest service for this request (override point for tests).

    US-511: HMAC secrets come from the store registry (decrypted
    through the vault) instead of the F1 no-secrets default.
    """
    registry = SqlAlchemyIntegrationsRepository(session)
    secrets = RegistryWebhookSecretStore(registry, _webhook_vault_or_none(settings))
    return EventIngestService(inbox, idempotency, secrets, job_queue)


def get_store_sync_service(
    ingest: EventIngestService = Depends(get_event_ingest_service),
) -> StoreSyncService:
    """Build the store reconciliation/backfill service (override point for tests)."""
    return StoreSyncService(ingest)


def get_event_query_service(
    inbox: SqlAlchemyEventInboxRepository = Depends(get_event_inbox_repository),
    outbox: SqlAlchemyEventOutboxRepository = Depends(get_event_outbox_repository),
    audit_log: SqlAlchemyDecisionAuditLogRepository = Depends(get_decision_audit_log_repository),
) -> EventQueryService:
    """Build the event query service for this request (override point for tests)."""
    return EventQueryService(inbox, outbox, audit_log)


def get_dlq_replay_service(
    inbox: SqlAlchemyEventInboxRepository = Depends(get_event_inbox_repository),
    audit_log: SqlAlchemyDecisionAuditLogRepository = Depends(get_decision_audit_log_repository),
    job_queue: JobQueuePort = Depends(get_job_queue),
) -> DlqReplayService:
    """Build the DLQ replay service for this request (override point for tests)."""
    return DlqReplayService(inbox, audit_log, job_queue)


def get_store_settings_service(
    repository: SqlAlchemyStoreSettingsRepository = Depends(get_store_settings_repository),
) -> StoreSettingsService:
    """Build the store-settings service for this request (override point for tests)."""
    return StoreSettingsService(repository)


def get_staleness_service(
    inbox: SqlAlchemyEventInboxRepository = Depends(get_event_inbox_repository),
) -> StalenessService:
    """Build the staleness service for this request (override point for tests)."""
    return StalenessService(inbox)


# -- Manual entries (US-513, dual entry) ------------------------------------


def get_manual_entry_repository(
    session: Session = Depends(get_db_session),
) -> SqlAlchemyManualEntryRepository:
    """Build the SQLAlchemy manual-entry repository for this request."""
    return SqlAlchemyManualEntryRepository(session)


def get_manual_entry_service(
    ingest: EventIngestService = Depends(get_event_ingest_service),
    repository: SqlAlchemyManualEntryRepository = Depends(get_manual_entry_repository),
) -> ManualEntryService:
    """Build the manual-entry service for this request (override point for tests).

    The service ingests through the shared event ingest service, so a
    manual entry travels the same verify → inbox → pipeline path as a
    webhook event.
    """
    return ManualEntryService(ingest, repository)


def get_manual_entry_query_service(
    repository: SqlAlchemyManualEntryRepository = Depends(get_manual_entry_repository),
    inbox: SqlAlchemyEventInboxRepository = Depends(get_event_inbox_repository),
    external_ids: SqlAlchemyExternalIdMapRepository = Depends(get_external_id_map_repository),
    audit_log: SqlAlchemyDecisionAuditLogRepository = Depends(get_decision_audit_log_repository),
) -> ManualEntryQueryService:
    """Build the manual-entry query service for this request (override point for tests)."""
    return ManualEntryQueryService(repository, inbox, external_ids, audit_log)


# -- Integration registries + policy gate (US-503) -------------------------


def get_integrations_repository(
    session: Session = Depends(get_db_session),
) -> SqlAlchemyIntegrationsRepository:
    """Build the SQLAlchemy integrations repository for this request."""
    return SqlAlchemyIntegrationsRepository(session)


def get_integrations_query_service(
    repository: SqlAlchemyIntegrationsRepository = Depends(get_integrations_repository),
) -> IntegrationsQueryService:
    """Build the integrations query service for this request (override point for tests)."""
    return IntegrationsQueryService(repository)


def get_gateway_contract_service(
    repository: SqlAlchemyIntegrationsRepository = Depends(get_integrations_repository),
) -> GatewayContractService:
    """Build the gateway contract service for this request (override point for tests)."""
    return GatewayContractService(repository)


def get_mapping_service(
    repository: SqlAlchemyIntegrationsRepository = Depends(get_integrations_repository),
) -> MappingService:
    """Build the mapping service for this request (override point for tests)."""
    return MappingService(repository)


def get_policy_gate_config_service(
    repository: SqlAlchemyIntegrationsRepository = Depends(get_integrations_repository),
) -> PolicyGateConfigService:
    """Build the policy-gate config service for this request (override point for tests)."""
    return PolicyGateConfigService(repository)


def get_fiscal_period_checker(
    adapter: TrytonAccountingAdapter = Depends(get_engine_adapter),
) -> FiscalPeriodCheckerPort:
    """Build the engine-backed fiscal-period checker (override point for tests)."""
    return TrytonFiscalPeriodChecker(adapter)


def get_policy_gate_service(
    repository: SqlAlchemyIntegrationsRepository = Depends(get_integrations_repository),
    checker: FiscalPeriodCheckerPort = Depends(get_fiscal_period_checker),
) -> PolicyGateService:
    """Build the policy-gate service for this request (override point for tests)."""
    return PolicyGateService(repository, checker)


def get_credential_vault(
    settings: AppSettings = Depends(get_settings_dep),
) -> CredentialVaultPort | None:
    """Build the credential vault, or None when no key is configured.

    A missing key is NOT an error here: reads and credential-less
    writes work without a vault. Services that need to store a secret
    fail closed with a typed 503 when the vault is None. A malformed
    configured key is a 503 too — the vault cannot be trusted.
    """
    key = settings.auth.credentials_encryption_key
    if key is None:
        return None
    try:
        return FernetCredentialVault(key)
    except ValueError:
        raise ApiError(
            status_code=503,
            code="INTEGRATION_CREDENTIAL_VAULT_UNAVAILABLE",
            detail="The configured credential-encryption key is invalid.",
        ) from None


def get_bank_connection_service(
    repository: SqlAlchemyIntegrationsRepository = Depends(get_integrations_repository),
    vault: CredentialVaultPort | None = Depends(get_credential_vault),
) -> BankConnectionService:
    """Build the bank-connection service for this request (override point for tests)."""
    return BankConnectionService(repository, vault)


def get_channel_registry_service(
    repository: SqlAlchemyIntegrationsRepository = Depends(get_integrations_repository),
    vault: CredentialVaultPort | None = Depends(get_credential_vault),
) -> ChannelRegistryService:
    """Build the channel registry service for this request (override point for tests)."""
    return ChannelRegistryService(repository, vault)


def get_store_service(
    repository: SqlAlchemyIntegrationsRepository = Depends(get_integrations_repository),
    vault: CredentialVaultPort | None = Depends(get_credential_vault),
) -> StoreService:
    """Build the store service for this request (override point for tests)."""
    return StoreService(repository, vault)


def get_store_connector_factory(
    repository: SqlAlchemyIntegrationsRepository = Depends(get_integrations_repository),
    vault: CredentialVaultPort | None = Depends(get_credential_vault),
) -> StoreConnectorFactory:
    """Build the store-connector factory for this request (override point for tests).

    Sync always needs the vault (both transports authenticate with an
    encrypted credential), so a missing key is a typed 503 here
    instead of a failure mid-sync.
    """
    if vault is None:
        raise ApiError(
            status_code=503,
            code="INTEGRATION_CREDENTIAL_VAULT_UNAVAILABLE",
            detail=(
                "Cannot sync a store: no credential-encryption key is configured "
                "(HESABYAR_AUTH__CREDENTIALS_ENCRYPTION_KEY)."
            ),
        )
    return StoreConnectorFactory(repository, vault)


def get_installation_settings_service(
    repository: SqlAlchemyIntegrationsRepository = Depends(get_integrations_repository),
) -> InstallationSettingsService:
    """Build the installation-settings service for this request (override point for tests)."""
    return InstallationSettingsService(repository)


# -- Review-queue services (US-504) ----------------------------------------


def get_review_queue_repository(
    session: Session = Depends(get_db_session),
) -> SqlAlchemyReviewQueueRepository:
    """Build the SQLAlchemy review-queue repository for this request.

    One repository implements both ports (items + audit trail); the
    services share it, and the request session is shared too (FastAPI
    caches ``get_db_session`` per request), so the router's commit
    covers the services' flushed writes.
    """
    return SqlAlchemyReviewQueueRepository(session)


def get_review_queue_query_service(
    repository: SqlAlchemyReviewQueueRepository = Depends(get_review_queue_repository),
) -> ReviewQueueQueryService:
    """Build the review-queue query service for this request (override point for tests)."""
    return ReviewQueueQueryService(repository, repository)


def get_review_queue_command_service(
    repository: SqlAlchemyReviewQueueRepository = Depends(get_review_queue_repository),
) -> ReviewQueueCommandService:
    """Build the review-queue command service for this request (override point for tests)."""
    return ReviewQueueCommandService(repository, repository)


# -- Bank import + reconcile services (US-514) -------------------------------


def get_bank_repository(
    session: Session = Depends(get_db_session),
) -> SqlAlchemyBankRepository:
    """Build the SQLAlchemy bank repository for this request."""
    return SqlAlchemyBankRepository(session)


def get_bank_file_reader() -> StatementFileReader:
    """Build the statement file reader (stateless; override point for tests)."""
    return StatementFileReader()


def get_bank_import_service(
    adapter: TrytonAccountingAdapter = Depends(get_engine_adapter),
    repository: SqlAlchemyBankRepository = Depends(get_bank_repository),
    review: ReviewQueueCommandService = Depends(get_review_queue_command_service),
    reader: StatementFileReader = Depends(get_bank_file_reader),
) -> BankImportService:
    """Build the bank-import service for this request (override point for tests)."""
    return BankImportService(adapter, repository, review, reader)


def get_bank_reconcile_service(
    adapter: TrytonAccountingAdapter = Depends(get_engine_adapter),
    repository: SqlAlchemyBankRepository = Depends(get_bank_repository),
    review: ReviewQueueCommandService = Depends(get_review_queue_command_service),
    queue_repository: SqlAlchemyReviewQueueRepository = Depends(get_review_queue_repository),
    integrations_repository: SqlAlchemyIntegrationsRepository = Depends(
        get_integrations_repository
    ),
) -> BankReconcileService:
    """Build the bank-reconcile service for this request (override point for tests)."""
    return BankReconcileService(
        adapter, repository, review, queue_repository, integrations_repository
    )


def get_bank_query_service(
    adapter: TrytonAccountingAdapter = Depends(get_engine_adapter),
    repository: SqlAlchemyBankRepository = Depends(get_bank_repository),
) -> BankQueryService:
    """Build the bank query service for this request (override point for tests)."""
    return BankQueryService(adapter, repository)


# -- Packaging services (US-515) ---------------------------------------------


def get_packaging_repository(
    session: Session = Depends(get_db_session),
) -> SqlAlchemyPackagingRepository:
    """Build the SQLAlchemy packaging repository for this request."""
    return SqlAlchemyPackagingRepository(session)


def get_packaging_query_service(
    adapter: TrytonAccountingAdapter = Depends(get_engine_adapter),
    repository: SqlAlchemyPackagingRepository = Depends(get_packaging_repository),
) -> PackagingQueryService:
    """Build the packaging query service for this request (override point for tests)."""
    return PackagingQueryService(adapter, repository)


def get_packaging_command_service(
    adapter: TrytonAccountingAdapter = Depends(get_engine_adapter),
    repository: SqlAlchemyPackagingRepository = Depends(get_packaging_repository),
    review_repository: SqlAlchemyReviewQueueRepository = Depends(get_review_queue_repository),
    review_commands: ReviewQueueCommandService = Depends(get_review_queue_command_service),
) -> PackagingCommandService:
    """Build the packaging command service for this request (override point for tests).

    The command service also files ``packaging_reorder`` review items,
    so it shares the request's review-queue repository and command
    service (same session — the router's commit covers all writes).
    """
    return PackagingCommandService(adapter, repository, review_repository, review_commands)


# -- Cash-box services (US-516) ----------------------------------------------


def get_cashbox_repository(
    session: Session = Depends(get_db_session),
) -> SqlAlchemyCashBoxRepository:
    """Build the SQLAlchemy cash-box repository for this request."""
    return SqlAlchemyCashBoxRepository(session)


def get_cashbox_query_service(
    repository: SqlAlchemyCashBoxRepository = Depends(get_cashbox_repository),
    adapter: TrytonAccountingAdapter = Depends(get_engine_adapter),
) -> CashBoxQueryService:
    """Build the cash-box query service for this request (override point for tests)."""
    return CashBoxQueryService(repository, adapter)


def get_cashbox_command_service(
    repository: SqlAlchemyCashBoxRepository = Depends(get_cashbox_repository),
    adapter: TrytonAccountingAdapter = Depends(get_engine_adapter),
    review_queue: ReviewQueueCommandService = Depends(get_review_queue_command_service),
    review_query: ReviewQueueQueryService = Depends(get_review_queue_query_service),
) -> CashBoxCommandService:
    """Build the cash-box command service for this request (override point for tests)."""
    return CashBoxCommandService(repository, adapter, review_queue, review_query)


# -- Settlement services (US-521) ---------------------------------------------


def get_settlement_repository(
    session: Session = Depends(get_db_session),
) -> SqlAlchemySettlementRepository:
    """Build the SQLAlchemy settlement repository for this request."""
    return SqlAlchemySettlementRepository(session)


def get_settlement_audit(
    session: Session = Depends(get_db_session),
) -> SqlAlchemySettlementAudit:
    """Build the settlement audit-log adapter for this request."""
    return SqlAlchemySettlementAudit(session)


def get_settlement_query_service(
    repository: SqlAlchemySettlementRepository = Depends(get_settlement_repository),
    registry: SqlAlchemyIntegrationsRepository = Depends(get_integrations_repository),
) -> SettlementQueryService:
    """Build the settlement query service for this request (override point for tests)."""
    return SettlementQueryService(repository, registry)


def get_settlement_command_service(
    repository: SqlAlchemySettlementRepository = Depends(get_settlement_repository),
    registry: SqlAlchemyIntegrationsRepository = Depends(get_integrations_repository),
    audit: SqlAlchemySettlementAudit = Depends(get_settlement_audit),
    review: ReviewQueueCommandService = Depends(get_review_queue_command_service),
) -> SettlementCommandService:
    """Build the settlement command service for this request (override point for tests)."""
    return SettlementCommandService(repository, registry, audit, review=review)


def get_settlement_posting_service(
    repository: SqlAlchemySettlementRepository = Depends(get_settlement_repository),
    registry: SqlAlchemyIntegrationsRepository = Depends(get_integrations_repository),
    adapter: TrytonAccountingAdapter = Depends(get_engine_adapter),
    audit: SqlAlchemySettlementAudit = Depends(get_settlement_audit),
) -> SettlementPostingService:
    """Build the settlement posting service for this request (override point for tests)."""
    return SettlementPostingService(repository, registry, adapter, audit)


def get_settlement_posting(
    repository: SqlAlchemySettlementRepository = Depends(get_settlement_repository),
    service: SettlementPostingService = Depends(get_settlement_posting_service),
) -> SettlementPostingPort:
    """Return the settlement posting implementation (US-522 engine).

    The real implementation fans each tie-out batch out to one
    engine posting per tranche through the settlement posting
    service, with the posting ledger as the retry guard (already
    posted tranches are skipped, never re-posted). A tranche
    failure raises ``SettlementPostingError`` and the batch stays
    in exception with a review item; tranches posted before the
    failure stay posted (partial posting by design).
    """
    return EngineSettlementPosting(service, repository)


def get_settlement_import_service(
    repository: SqlAlchemySettlementRepository = Depends(get_settlement_repository),
    registry: SqlAlchemyIntegrationsRepository = Depends(get_integrations_repository),
    audit: SqlAlchemySettlementAudit = Depends(get_settlement_audit),
    commands: SettlementCommandService = Depends(get_settlement_command_service),
    review: ReviewQueueCommandService = Depends(get_review_queue_command_service),
    review_queries: ReviewQueueQueryService = Depends(get_review_queue_query_service),
    reader: StatementFileReader = Depends(get_bank_file_reader),
    outbox: SqlAlchemyEventOutboxRepository = Depends(get_event_outbox_repository),
    posting: SettlementPostingPort = Depends(get_settlement_posting),
    policy_gate: PolicyGateService = Depends(get_policy_gate_service),
) -> SettlementImportService:
    """Build the settlement import service for this request (override point for tests)."""
    return SettlementImportService(
        repository,
        registry,
        audit,
        commands,
        review,
        review_queries,
        reader,
        outbox,
        posting,
        policy_gate,
    )


# -- Gateway reconciliation (US-524) ----------------------------------------


def get_cheque_repository(
    session: Session = Depends(get_db_session),
) -> SqlAlchemyChequeRepository:
    """Build the SQLAlchemy cheque repository for this request."""
    return SqlAlchemyChequeRepository(session)


def get_cheque_query_service(
    repository: SqlAlchemyChequeRepository = Depends(get_cheque_repository),
    engine: TrytonAccountingAdapter = Depends(get_engine_adapter),
) -> ChequeQueryService:
    """Build the cheque query service for this request (override point for tests).

    The engine adapter is wired so the checkbook endpoints (report 8,
    US-532) can read leaf counts live; the registry-only reads do not
    touch it.
    """
    return ChequeQueryService(repository, engine)


def get_cheque_scan_store(
    settings: AppSettings = Depends(get_settings_dep),
) -> LocalChequeScanStore:
    """Build the local filestore for cheque leaf scans (US-533)."""
    return LocalChequeScanStore(settings.filestore.directory)


def get_cheque_command_service(
    repository: SqlAlchemyChequeRepository = Depends(get_cheque_repository),
    adapter: TrytonAccountingAdapter = Depends(get_engine_adapter),
    gate: PolicyGateService = Depends(get_policy_gate_service),
    review: ReviewQueueCommandService = Depends(get_review_queue_command_service),
    scan_store: LocalChequeScanStore = Depends(get_cheque_scan_store),
) -> ChequeCommandService:
    """Build the cheque command service for this request (override point for tests)."""
    return ChequeCommandService(repository, adapter, gate=gate, review=review, scan_store=scan_store)


def get_sayyad_inquiry_service(
    repository: SqlAlchemyChequeRepository = Depends(get_cheque_repository),
    review: ReviewQueueCommandService = Depends(get_review_queue_command_service),
    queue_repository: SqlAlchemyReviewQueueRepository = Depends(
        get_review_queue_repository
    ),
) -> SayyadInquiryService:
    """Build the Sayyad inquiry service (simulator ports until B3, US-534)."""
    return SayyadInquiryService(
        repository,
        finnotech=DeterministicFintechSayyadSimulator(),
        ehrazino=DeterministicEhrazinoSimulator(),
        review=review,
        queue=queue_repository,
    )


def get_cheque_clearing_service(
    adapter: TrytonAccountingAdapter = Depends(get_engine_adapter),
    repository: SqlAlchemyChequeRepository = Depends(get_cheque_repository),
    bank_repository: SqlAlchemyBankRepository = Depends(get_bank_repository),
    cheque_service: ChequeCommandService = Depends(get_cheque_command_service),
    review: ReviewQueueCommandService = Depends(get_review_queue_command_service),
    queue_repository: SqlAlchemyReviewQueueRepository = Depends(
        get_review_queue_repository
    ),
) -> ChequeClearingService:
    """Build the cheque clearing service (US-534, Layer 3 on US-514)."""
    return ChequeClearingService(
        adapter, repository, bank_repository, cheque_service, review, queue_repository
    )


def get_event_pipeline_service(
    inbox: SqlAlchemyEventInboxRepository = Depends(get_event_inbox_repository),
    outbox: SqlAlchemyEventOutboxRepository = Depends(get_event_outbox_repository),
    external_ids: SqlAlchemyExternalIdMapRepository = Depends(get_external_id_map_repository),
    audit_log: SqlAlchemyDecisionAuditLogRepository = Depends(get_decision_audit_log_repository),
    store_settings: SqlAlchemyStoreSettingsRepository = Depends(get_store_settings_repository),
    adapter: TrytonAccountingAdapter = Depends(get_engine_adapter),
    session: Session = Depends(get_db_session),
    cheque_commands: ChequeCommandService = Depends(get_cheque_command_service),
) -> EventPipelineService:
    """Build the event pipeline service for this request (override point for tests).

    US-511: the mapping resolver, draft builder and policy gate are
    the registry-backed store implementations (F3 seams filled); the
    inbox-backed review queue stays until F4 replaces it. Draft is
    still the default — the registry gate only auto-posts when the
    installation opted in, the event type has an allowed policy row
    with a cap, and the fiscal period is open.

    US-533: site cheque events branch to the intake handler (the
    cheque command service), because intake creates no engine
    document — it writes the cheque registry instead.
    """
    registry = SqlAlchemyIntegrationsRepository(session)
    return EventPipelineService(
        inbox,
        outbox,
        external_ids,
        audit_log,
        store_settings,
        StoreMappingResolver(registry, external_ids, adapter),
        StoreDraftBuilder(adapter, external_ids),
        RegistryPolicyGate(
            PolicyGateService(registry, TrytonFiscalPeriodChecker(adapter))
        ),
        InboxReviewQueueAdapter(),
        adapter,
        SiteChequeIntakeHandler(cheque_commands),
    )


def get_event_worker(
    pipeline: EventPipelineService = Depends(get_event_pipeline_service),
    inbox: SqlAlchemyEventInboxRepository = Depends(get_event_inbox_repository),
    outbox: SqlAlchemyEventOutboxRepository = Depends(get_event_outbox_repository),
) -> EventWorker:
    """Build the event worker for this request (override point for tests)."""
    return EventWorker(pipeline, inbox, outbox, InProcessOutboxPublisher())


def get_gateway_client_factory() -> GatewayClientFactory:
    """Build the gateway client factory (override point for tests).

    Tests override this with a factory holding an ``httpx.MockTransport``
    — no live gateway call is ever made from a test.
    """
    return GatewayClientFactory()


def get_gateway_reconciliation_service(
    registry: SqlAlchemyIntegrationsRepository = Depends(get_integrations_repository),
    settlement_repository: SqlAlchemySettlementRepository = Depends(
        get_settlement_repository
    ),
    settlement_service: SettlementCommandService = Depends(
        get_settlement_command_service
    ),
    factory: GatewayClientFactory = Depends(get_gateway_client_factory),
    vault: CredentialVaultPort | None = Depends(get_credential_vault),
) -> GatewayReconciliationService:
    """Build the gateway reconciliation service for this request (override point for tests)."""
    return GatewayReconciliationService(
        registry, settlement_repository, settlement_service, factory, vault
    )
