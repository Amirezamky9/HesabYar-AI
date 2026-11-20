"""Bank routes (spec §9.2, US-205 statements + US-514 import/reconcile).

Thin routes over the statement query service and the US-514 bank
services: no accounting rules here, no adapter or Tryton calls — the
services own the engine port.

Company scoping: list endpoints take their company from the ``company``
query parameter (or the API key's company restriction, like the
accounts endpoints). Body-carried companies resolve through
``require_company_row_by_id`` (same 404/403 contract). Statement-scoped
endpoints read the company from the engine record itself, then check
the company is mapped to the caller's tenant: a statement of an
unmapped company looks exactly like a statement that does not exist
(404 ``LEDGER_STATEMENT_NOT_FOUND``), and a key restricted to another
company gets 403.

Mutations require an ``Idempotency-Key`` header. Uploads are base64
JSON (the admin BFF proxy parses bodies as JSON only); file bytes are
never stored — only the filename for provenance.

No internet-banking password is ever asked for or stored anywhere in
this module (OAuth/consent model only — ADR 0003 decision 10).
"""

from __future__ import annotations

import datetime
from collections.abc import Callable

from fastapi import APIRouter, Depends, Header, Query, Request, status
from sqlalchemy.orm import Session

from hesabyar.application.commands.bank_import import BankImportService
from hesabyar.application.commands.bank_reconcile import BankReconcileService
from hesabyar.application.commands.cheque_clearing import ChequeClearingService
from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.application.errors import ReviewInvalidStateError
from hesabyar.application.queries.bank import BankQueryService
from hesabyar.application.queries.statements import BankStatementQueryService
from hesabyar.domain.accounting.refs import BankStatementRef
from hesabyar.domain.bank.models import (
    FileCurrencyUnit,
    ImportBatchStatus,
    MatchTargetType,
    StatementFileFormat,
)
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.integrations.models import SuggestionMode
from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.infrastructure.persistence.postgres.models import EngineCompanyModel
from hesabyar.interfaces.rest.dependencies import (
    company_ref_for_row,
    get_bank_import_service,
    get_bank_query_service,
    get_bank_reconcile_service,
    get_bank_statement_query_service,
    get_cheque_clearing_service,
    get_db_session,
    get_review_queue_command_service,
    require_company_row_by_id,
    require_scopes,
    require_visible_company,
    resolve_accounts_company,
)
from hesabyar.interfaces.rest.errors import ApiError, translate_errors
from hesabyar.interfaces.rest.idempotency import (
    IDEMPOTENCY_KEY_HEADER,
    require_idempotency_key,
)
from hesabyar.interfaces.rest.pagination import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    paginate,
    parse_sort_param,
    reject_unknown_params,
)
from hesabyar.interfaces.rest.schemas.bank import (
    BankImportBatchDetailResponse,
    BankImportBatchResponse,
    BankStatementMatchResponse,
    ColumnMappingCreateRequest,
    ColumnMappingResponse,
    CreateImportRequest,
    DetectFileRequest,
    FileDetectionResponse,
    ManualMatchRequest,
    MatchRuleResponse,
    ReconcileRequest,
    ReconcileResponse,
    ReconciliationStatusResponse,
    RuleModeRequest,
    StatementJournalResponse,
)
from hesabyar.interfaces.rest.schemas.cheques import (
    ChequeClearingResponse,
    ClearingRunRequest,
)
from hesabyar.interfaces.rest.schemas.pagination import PaginatedResponse
from hesabyar.interfaces.rest.schemas.statements import (
    BankStatementResponse,
    StatementImportStatusResponse,
)

router = APIRouter(tags=["banking"])

_STATEMENT_LIST_PARAMS = {"company", "state", "sort", "limit", "cursor"}
_STATEMENT_SORT_KEYS: dict[str, Callable[[BankStatementResponse], str]] = {
    "date": lambda item: item.date.isoformat(),
    "id": lambda item: str(item.id),
}
_IMPORT_LIST_PARAMS = {"company", "status"}
_MAPPING_LIST_PARAMS = {"bank_key", "company"}
_RULE_LIST_PARAMS = {"company"}


def _parse_file_format(value: str) -> StatementFileFormat:
    try:
        return StatementFileFormat(value)
    except ValueError:
        raise ApiError(
            status_code=422,
            code="BANK_COLUMN_MAPPING_INVALID",
            detail=f"Unknown file format {value!r} (expected 'xlsx', 'csv' or 'pdf').",
        ) from None


def _parse_currency_unit(value: str) -> FileCurrencyUnit:
    try:
        return FileCurrencyUnit(value)
    except ValueError:
        raise ApiError(
            status_code=422,
            code="BANK_COLUMN_MAPPING_INVALID",
            detail=f"Unknown currency unit {value!r} (expected 'rial' or 'toman').",
        ) from None


def _parse_target_type(value: str) -> MatchTargetType:
    try:
        target = MatchTargetType(value)
    except ValueError:
        raise ApiError(
            status_code=422,
            code="BANK_IMPORT_INVALID_STATE",
            detail=f"Unknown target type {value!r} (expected 'invoice', 'payment' or 'manual').",
        ) from None
    if target == MatchTargetType.RULE:
        raise ApiError(
            status_code=422,
            code="BANK_IMPORT_INVALID_STATE",
            detail="A rule target cannot be applied by id alone; apply the rule's account as a manual match.",
        )
    return target


def _parse_rule_mode(value: str) -> SuggestionMode:
    try:
        return SuggestionMode(value)
    except ValueError:
        raise ApiError(
            status_code=422,
            code="BANK_IMPORT_INVALID_STATE",
            detail=f"Unknown rule mode {value!r} (expected 'suggest' or 'auto').",
        ) from None


def _parse_period(date_from: str, date_to: str) -> tuple[FiscalDate, FiscalDate]:
    try:
        start = FiscalDate(datetime.date.fromisoformat(date_from))
        end = FiscalDate(datetime.date.fromisoformat(date_to))
    except ValueError:
        raise ApiError(
            status_code=422,
            code="VALIDATION_ERROR",
            detail="date_from and date_to must be ISO dates (YYYY-MM-DD).",
        ) from None
    if start.gregorian_date > end.gregorian_date:
        raise ApiError(
            status_code=422,
            code="VALIDATION_ERROR",
            detail="date_from must not be after date_to.",
        )
    return start, end


# -- statements (US-205, unchanged) --------------------------------------------


@router.get(
    "/bank/statements",
    response_model=PaginatedResponse[BankStatementResponse],
    summary="List a company's bank/cash statements",
)
def list_bank_statements(
    request: Request,
    state: str | None = Query(
        default=None,
        description=(
            "Filter on engine state: 'draft', 'validated', 'posted' or 'cancelled'"
        ),
    ),
    sort: str | None = Query(
        default=None,
        description=(
            "Sort field: 'date' (default, newest first) or 'id'; "
            "prefix '-' for descending"
        ),
    ),
    limit: int = Query(
        default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"
    ),
    cursor: str | None = Query(
        default=None, description="Opaque cursor from a previous page's 'next_cursor'"
    ),
    _client: AuthenticatedClient = Depends(require_scopes("banking:read")),
    row: EngineCompanyModel = Depends(resolve_accounts_company),
    service: BankStatementQueryService = Depends(get_bank_statement_query_service),
) -> PaginatedResponse[BankStatementResponse]:
    """Return the company's bank/cash statements, cursor-paginated, newest first.

    An invalid ``state`` filter is a 400 ``LEDGER_INVALID_STATEMENT``
    (the engine port validates filters before delegating).
    """
    reject_unknown_params(request, _STATEMENT_LIST_PARAMS)
    field, reverse = parse_sort_param(sort, _STATEMENT_SORT_KEYS, default="date")
    if sort is None or sort == "":
        # Default order is newest first, matching the port's order; an
        # explicit sort=date sorts ascending (oldest first).
        reverse = True
    ref = company_ref_for_row(row)
    with translate_errors():
        views = service.list_bank_statements(ref, state)
    items = [BankStatementResponse.from_view(view, row.id) for view in views]
    page, next_cursor = paginate(
        items,
        key_fn=_STATEMENT_SORT_KEYS[field],
        tie_fn=lambda item: str(item.id),
        limit=limit,
        cursor=cursor,
        reverse=reverse,
    )
    return PaginatedResponse(items=page, next_cursor=next_cursor)


@router.get(
    "/bank/statements/import-status",
    response_model=StatementImportStatusResponse,
    summary="Get a company's statement import status",
)
def get_statement_import_status(
    _client: AuthenticatedClient = Depends(require_scopes("banking:read")),
    row: EngineCompanyModel = Depends(resolve_accounts_company),
    service: BankStatementQueryService = Depends(get_bank_statement_query_service),
) -> StatementImportStatusResponse:
    """Return a read-only summary of the company's imported statements.

    Per-state counts, the newest statement, and how many statement
    lines are matched to an invoice — all derived from engine reads.
    """
    ref = company_ref_for_row(row)
    with translate_errors():
        status = service.get_import_status(ref)
    return StatementImportStatusResponse.from_status(status, row.id)


# NOTE (route order): literal GET routes under /bank/statements/*
# MUST be registered BEFORE the /bank/statements/{statement_id}
# route below. Starlette matches in registration order, and
# "/bank/statements/imports" would otherwise match {statement_id},
# fail int parsing, and answer 422.
@router.get(
    "/bank/statements/imports",
    response_model=list[BankImportBatchResponse],
    summary="List a company's import batches",
)
def list_imports(
    request: Request,
    company: str | None = Query(
        default=None, description="Filter by HesabYar company row id"
    ),
    status_filter: str | None = Query(
        default=None, alias="status", description="Filter by batch status"
    ),
    client: AuthenticatedClient = Depends(require_scopes("banking:read")),
    service: BankQueryService = Depends(get_bank_query_service),
) -> list[BankImportBatchResponse]:
    """Return the tenant's import batches, newest first."""
    reject_unknown_params(request, _IMPORT_LIST_PARAMS)
    wanted_status: ImportBatchStatus | None = None
    if status_filter is not None:
        try:
            wanted_status = ImportBatchStatus(status_filter)
        except ValueError:
            raise ApiError(
                status_code=422,
                code="VALIDATION_ERROR",
                detail=f"Unknown import status {status_filter!r}.",
            ) from None
    with translate_errors():
        batches = service.list_batches(client.tenant_id, company)
    items = [BankImportBatchResponse.from_batch(batch) for batch in batches]
    if wanted_status is not None:
        items = [item for item in items if item.status == wanted_status.value]
    return items


@router.get(
    "/bank/statements/imports/{batch_id}",
    response_model=BankImportBatchDetailResponse,
    summary="Get one import batch with its preview rows",
)
def get_import(
    batch_id: str,
    client: AuthenticatedClient = Depends(require_scopes("banking:read")),
    service: BankQueryService = Depends(get_bank_query_service),
) -> BankImportBatchDetailResponse:
    """Return one import batch with its parsed rows (the preview)."""
    with translate_errors():
        batch = service.get_batch(client.tenant_id, batch_id)
        rows = service.list_rows(client.tenant_id, batch_id)
    return BankImportBatchDetailResponse.from_batch_and_rows(batch, list(rows))


@router.get(
    "/bank/statements/{statement_id}",
    response_model=BankStatementResponse,
    summary="Get one bank/cash statement",
)
def get_bank_statement(
    statement_id: int,
    client: AuthenticatedClient = Depends(require_scopes("banking:read")),
    session: Session = Depends(get_db_session),
    service: BankStatementQueryService = Depends(get_bank_statement_query_service),
) -> BankStatementResponse:
    """Return one bank/cash statement of a company in scope.

    Unknown statement (or a statement of a company with no mapping in
    this tenant) → 404 ``LEDGER_STATEMENT_NOT_FOUND``.
    """
    with translate_errors():
        view = service.get_bank_statement(BankStatementRef(statement_id))
    row = require_visible_company(
        session,
        client,
        view.company,
        statement_id,
        resource_kind="Bank statement",
        error_code="LEDGER_STATEMENT_NOT_FOUND",
    )
    return BankStatementResponse.from_view(view, row.id)


# -- statement journals + reconciliation status (US-514 reads) ------------------


@router.get(
    "/bank/statement-journals",
    response_model=list[StatementJournalResponse],
    summary="List a company's statement journals (import targets)",
)
def list_statement_journals(
    _client: AuthenticatedClient = Depends(require_scopes("banking:read")),
    row: EngineCompanyModel = Depends(resolve_accounts_company),
    service: BankQueryService = Depends(get_bank_query_service),
) -> list[StatementJournalResponse]:
    """Return the company's statement journals with their bank accounts.

    The import form picks one as the statement's destination; the
    journal's bank account (Shaba, when present) is recorded on every
    match the statement later produces.
    """
    ref = company_ref_for_row(row)
    with translate_errors():
        journals = service.list_statement_journals(ref)
    return [StatementJournalResponse.from_view(journal) for journal in journals]


@router.get(
    "/bank/reconciliation-status",
    response_model=ReconciliationStatusResponse,
    summary="Bank reconciliation status report (§11.2)",
)
def get_reconciliation_status(
    date_from: str = Query(description="Period start, ISO date (YYYY-MM-DD)"),
    date_to: str = Query(description="Period end, ISO date (YYYY-MM-DD)"),
    _client: AuthenticatedClient = Depends(require_scopes("banking:read")),
    row: EngineCompanyModel = Depends(resolve_accounts_company),
    service: BankQueryService = Depends(get_bank_query_service),
) -> ReconciliationStatusResponse:
    """Return per-journal reconciliation status over a period (spec §11.2).

    Lines are classified from the engine (``related_to`` set ⇒
    matched) plus the latest HesabYar match record. Cancelled
    statements are excluded. The response carries §11.1 freshness
    metadata (source, source timestamp, versions).
    """
    start, end = _parse_period(date_from, date_to)
    ref = company_ref_for_row(row)
    with translate_errors():
        status = service.reconciliation_status(
            tenant_id=_client.tenant_id,
            company=ref,
            company_id=row.id,
            date_from=start,
            date_to=end,
        )
    return ReconciliationStatusResponse.from_status(status)


# -- column mappings (US-514 B1) -------------------------------------------------


@router.get(
    "/bank/column-mappings",
    response_model=list[ColumnMappingResponse],
    summary="List saved per-bank column mappings",
)
def list_column_mappings(
    request: Request,
    bank_key: str | None = Query(default=None, description="Filter by bank key"),
    company: str | None = Query(
        default=None, description="Filter by HesabYar company row id"
    ),
    client: AuthenticatedClient = Depends(require_scopes("banking:read")),
    service: BankQueryService = Depends(get_bank_query_service),
) -> list[ColumnMappingResponse]:
    """Return the tenant's saved column mappings (active first, newest first)."""
    reject_unknown_params(request, _MAPPING_LIST_PARAMS)
    with translate_errors():
        mappings = service.list_mappings(client.tenant_id, bank_key, company)
    return [ColumnMappingResponse.from_mapping(mapping) for mapping in mappings]


@router.post(
    "/bank/column-mappings",
    response_model=ColumnMappingResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Save a per-bank column mapping",
)
def create_column_mapping(
    body: ColumnMappingCreateRequest,
    idempotency_key: str | None = Header(
        default=None,
        alias=IDEMPOTENCY_KEY_HEADER,
        description="Unique key for this mutation; retries replay the original response",
    ),
    client: AuthenticatedClient = Depends(require_scopes("banking:import")),
    session: Session = Depends(get_db_session),
    service: BankImportService = Depends(get_bank_import_service),
) -> ColumnMappingResponse:
    """Save a per-bank column mapping (validated before it is stored).

    A mapping with a company id is scoped to that company; without one
    it applies tenant-wide. The mapping's company (when given) must
    belong to the caller's tenant.
    """
    require_idempotency_key(idempotency_key)
    if body.company is not None:
        require_company_row_by_id(session, client, body.company)
    with translate_errors():
        mapping = service.save_mapping(
            tenant_id=client.tenant_id,
            bank_key=body.bank_key,
            name=body.name,
            file_format=_parse_file_format(body.file_format),
            columns=body.columns,
            date_kind=body.date_kind,
            currency_unit=_parse_currency_unit(body.currency_unit),
            header_row=body.header_row,
            company_id=body.company,
        )
    session.commit()
    return ColumnMappingResponse.from_mapping(mapping)


@router.get(
    "/bank/column-mappings/{mapping_id}",
    response_model=ColumnMappingResponse,
    summary="Get one saved column mapping",
)
def get_column_mapping(
    mapping_id: str,
    client: AuthenticatedClient = Depends(require_scopes("banking:read")),
    service: BankQueryService = Depends(get_bank_query_service),
) -> ColumnMappingResponse:
    """Return one saved column mapping. Unknown id → 404."""
    with translate_errors():
        mapping = service.get_mapping(client.tenant_id, mapping_id)
    return ColumnMappingResponse.from_mapping(mapping)


@router.post(
    "/bank/column-mappings/{mapping_id}/supersede",
    response_model=ColumnMappingResponse,
    summary="Supersede a saved column mapping",
)
def supersede_column_mapping(
    mapping_id: str,
    idempotency_key: str | None = Header(
        default=None,
        alias=IDEMPOTENCY_KEY_HEADER,
        description="Unique key for this mutation; retries replay the original response",
    ),
    client: AuthenticatedClient = Depends(require_scopes("banking:import")),
    session: Session = Depends(get_db_session),
    service: BankImportService = Depends(get_bank_import_service),
) -> ColumnMappingResponse:
    """Supersede a mapping (it stays for provenance, unused for new imports)."""
    require_idempotency_key(idempotency_key)
    with translate_errors():
        mapping = service.supersede_mapping(
            tenant_id=client.tenant_id, mapping_id=mapping_id
        )
    session.commit()
    return ColumnMappingResponse.from_mapping(mapping)


# -- imports (US-514 B1) ----------------------------------------------------------


@router.post(
    "/bank/statements/imports/detect",
    response_model=FileDetectionResponse,
    summary="Detect a statement file's format and columns (no persistence)",
)
def detect_import_file(
    body: DetectFileRequest,
    _client: AuthenticatedClient = Depends(require_scopes("banking:import")),
    service: BankImportService = Depends(get_bank_import_service),
) -> FileDetectionResponse:
    """Read an uploaded file and suggest a column mapping.

    Pure detection — nothing is stored. An unreadable file is a 422
    ``BANK_IMPORT_FILE_ERROR`` (quarantined, never guessed).
    """
    with translate_errors():
        detection = service.detect_file(
            content=body.content_bytes(), filename=body.filename
        )
    return FileDetectionResponse.from_detection(detection)


@router.post(
    "/bank/statements/imports",
    response_model=BankImportBatchDetailResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Import a bank statement file (creates a batch awaiting review)",
)
def create_import(
    body: CreateImportRequest,
    idempotency_key: str | None = Header(
        default=None,
        alias=IDEMPOTENCY_KEY_HEADER,
        description="Unique key for this mutation; retries replay the original response",
    ),
    client: AuthenticatedClient = Depends(require_scopes("banking:import")),
    session: Session = Depends(get_db_session),
    service: BankImportService = Depends(get_bank_import_service),
    query: BankQueryService = Depends(get_bank_query_service),
) -> BankImportBatchDetailResponse:
    """Parse an uploaded statement file into a batch awaiting review.

    Either a saved mapping (``mapping_id``) or an inline ``columns``
    mapping must be given. The batch is born ``awaiting_review`` with
    its accounting-effect preview computed and a
    ``manual_import_review`` queue item filed; nothing reaches the
    engine until a human confirms. A file over the 2,000-row cap is a
    422 ``BANK_IMPORT_TOO_MANY_ROWS``.
    """
    require_idempotency_key(idempotency_key)
    row = require_company_row_by_id(session, client, body.company)
    ref = company_ref_for_row(row)
    with translate_errors():
        batch = service.create_import(
            tenant_id=client.tenant_id,
            company=ref,
            company_id=row.id,
            bank_key=body.bank_key,
            statement_journal_id=body.statement_journal_id,
            filename=body.filename,
            content=body.content_bytes(),
            currency_unit=_parse_currency_unit(body.currency_unit),
            default_account_id=body.default_account_id,
            mapping_id=body.mapping_id,
            mapping_columns=body.columns,
            date_kind=body.date_kind,
            header_row=body.header_row,
            actor=client.client_name,
        )
        rows = query.list_rows(client.tenant_id, batch.id)
    session.commit()
    return BankImportBatchDetailResponse.from_batch_and_rows(batch, list(rows))


@router.post(
    "/bank/statements/imports/{batch_id}/confirm",
    response_model=BankImportBatchResponse,
    summary="Confirm an import batch (writes the statement to the engine)",
)
def confirm_import(
    batch_id: str,
    idempotency_key: str | None = Header(
        default=None,
        alias=IDEMPOTENCY_KEY_HEADER,
        description="Unique key for this mutation; retries replay the original response",
    ),
    client: AuthenticatedClient = Depends(require_scopes("banking:import")),
    session: Session = Depends(get_db_session),
    service: BankImportService = Depends(get_bank_import_service),
    query: BankQueryService = Depends(get_bank_query_service),
) -> BankImportBatchResponse:
    """Write an awaiting-review batch to the engine as a draft statement.

    The statement is created in state ``draft`` with the preview's
    coding; the linked review item is resolved with
    ``approve_preview``. A batch in any other state is a 409.
    """
    require_idempotency_key(idempotency_key)
    with translate_errors():
        batch = query.get_batch(client.tenant_id, batch_id)
    row = require_company_row_by_id(session, client, batch.company_id)
    ref = company_ref_for_row(row)
    with translate_errors():
        confirmed = service.confirm_import(
            tenant_id=client.tenant_id,
            batch_id=batch_id,
            company=ref,
            actor=client.client_name,
        )
    session.commit()
    return BankImportBatchResponse.from_batch(confirmed)


@router.post(
    "/bank/statements/imports/{batch_id}/cancel",
    response_model=BankImportBatchResponse,
    summary="Cancel an import batch (nothing reaches the engine)",
)
def cancel_import(
    batch_id: str,
    idempotency_key: str | None = Header(
        default=None,
        alias=IDEMPOTENCY_KEY_HEADER,
        description="Unique key for this mutation; retries replay the original response",
    ),
    client: AuthenticatedClient = Depends(require_scopes("banking:import")),
    session: Session = Depends(get_db_session),
    service: BankImportService = Depends(get_bank_import_service),
) -> BankImportBatchResponse:
    """Cancel an awaiting-review batch. A batch in any other state is a 409."""
    require_idempotency_key(idempotency_key)
    with translate_errors():
        cancelled = service.cancel_import(
            tenant_id=client.tenant_id,
            batch_id=batch_id,
            actor=client.client_name,
        )
    session.commit()
    return BankImportBatchResponse.from_batch(cancelled)


# -- reconcile + manual match (US-514 B2) ------------------------------------------


@router.post(
    "/bank/statements/{statement_id}/reconcile",
    response_model=ReconcileResponse,
    summary="Reconcile a bank/cash statement",
)
def reconcile_bank_statement(
    statement_id: int,
    body: ReconcileRequest | None = None,
    idempotency_key: str | None = Header(
        default=None,
        alias=IDEMPOTENCY_KEY_HEADER,
        description="Unique key for this mutation; retries replay the original response",
    ),
    client: AuthenticatedClient = Depends(require_scopes("banking:reconcile")),
    session: Session = Depends(get_db_session),
    query_service: BankStatementQueryService = Depends(get_bank_statement_query_service),
    service: BankReconcileService = Depends(get_bank_reconcile_service),
) -> ReconcileResponse:
    """Reconcile a draft statement with three-priority matching.

    Auto decisions are written onto the engine lines (``related_to`` /
    account / party); suggested, ambiguous and unmatched lines get a
    ``bank_unmatched`` review item for a human. Reconcile never
    validates or posts the statement, and unmatched deposits never
    auto-post to revenue. Re-running is idempotent per line (no
    duplicate queue items). Only draft statements can be reconciled
    (409 ``BANK_STATEMENT_NOT_DRAFT`` otherwise).
    """
    require_idempotency_key(idempotency_key)
    with translate_errors():
        view = query_service.get_bank_statement(BankStatementRef(statement_id))
    row = require_visible_company(
        session,
        client,
        view.company,
        statement_id,
        resource_kind="Bank statement",
        error_code="LEDGER_STATEMENT_NOT_FOUND",
    )
    ref = company_ref_for_row(row)
    with translate_errors():
        result = service.reconcile(
            tenant_id=client.tenant_id,
            installation_id=client.installation_id,
            company=ref,
            company_id=row.id,
            statement_id=statement_id,
            actor=client.client_name,
            window_days=body.window_days if body is not None else None,
        )
    session.commit()
    return ReconcileResponse.from_result(result)


@router.post(
    "/bank/statements/{statement_id}/cheque-clearing",
    response_model=ChequeClearingResponse,
    summary="Match statement lines to in-clearing cheques (US-534, Layer 3)",
)
def cheque_clearing_bank_statement(
    statement_id: int,
    body: ClearingRunRequest | None = None,
    idempotency_key: str | None = Header(
        default=None,
        alias=IDEMPOTENCY_KEY_HEADER,
        description="Unique key for this mutation; retries replay the original response",
    ),
    client: AuthenticatedClient = Depends(require_scopes("cheques:transition")),
    session: Session = Depends(get_db_session),
    query_service: BankStatementQueryService = Depends(get_bank_statement_query_service),
    service: ChequeClearingService = Depends(get_cheque_clearing_service),
) -> ChequeClearingResponse:
    """Run cheque clearing matching over a draft statement.

    Exact matches (all keys) auto-clear their received commercial
    cheque through the policy gate (draft unless auto-post opted
    in) and reconcile its clearing legs; probable/ambiguous lines go
    to the review queue with their candidates; unmatched lines stay
    unmatched and their cheques stay in_clearing. Never auto-bounces
    — a stale in-clearing cheque only appears flagged in the aging
    report. Re-running is idempotent per line. Only draft statements
    can be clearing-matched (409 ``BANK_STATEMENT_NOT_DRAFT``
    otherwise).
    """
    require_idempotency_key(idempotency_key)
    with translate_errors():
        view = query_service.get_bank_statement(BankStatementRef(statement_id))
    row = require_visible_company(
        session,
        client,
        view.company,
        statement_id,
        resource_kind="Bank statement",
        error_code="LEDGER_STATEMENT_NOT_FOUND",
    )
    ref = company_ref_for_row(row)
    with translate_errors():
        result = service.match_statement(
            tenant_id=client.tenant_id,
            installation_id=client.installation_id,
            company=ref,
            company_id=row.id,
            statement_id=statement_id,
            actor=client.client_name,
            bank_key=body.bank_key if body is not None else None,
        )
    session.commit()
    return ChequeClearingResponse.from_result(result)


@router.get(
    "/bank/statements/{statement_id}/matches",
    response_model=list[BankStatementMatchResponse],
    summary="List the recorded matches for a statement",
)
def list_statement_matches(
    statement_id: int,
    client: AuthenticatedClient = Depends(require_scopes("banking:read")),
    session: Session = Depends(get_db_session),
    query_service: BankStatementQueryService = Depends(get_bank_statement_query_service),
    service: BankQueryService = Depends(get_bank_query_service),
) -> list[BankStatementMatchResponse]:
    """Return the match records for one statement, oldest first."""
    with translate_errors():
        view = query_service.get_bank_statement(BankStatementRef(statement_id))
    require_visible_company(
        session,
        client,
        view.company,
        statement_id,
        resource_kind="Bank statement",
        error_code="LEDGER_STATEMENT_NOT_FOUND",
    )
    with translate_errors():
        matches = service.list_matches_for_statement(client.tenant_id, statement_id)
    return [BankStatementMatchResponse.from_match(match) for match in matches]


@router.post(
    "/bank/statements/{statement_id}/lines/{line_id}/match",
    response_model=BankStatementMatchResponse,
    summary="Apply a human-confirmed match to one statement line",
)
def apply_manual_match(
    statement_id: int,
    line_id: int,
    body: ManualMatchRequest,
    idempotency_key: str | None = Header(
        default=None,
        alias=IDEMPOTENCY_KEY_HEADER,
        description="Unique key for this mutation; retries replay the original response",
    ),
    client: AuthenticatedClient = Depends(require_scopes("banking:reconcile")),
    session: Session = Depends(get_db_session),
    query_service: BankStatementQueryService = Depends(get_bank_statement_query_service),
    service: BankImportService = Depends(get_bank_import_service),
    review: ReviewQueueCommandService = Depends(get_review_queue_command_service),
) -> BankStatementMatchResponse:
    """Apply a human-confirmed match to one draft statement line.

    The line's ``related_to``/account/party are written through the
    engine port and the match is recorded CONFIRMED; with
    ``save_as_rule`` the mapping is learned for the future. When the
    request names the ``bank_unmatched`` review item this match
    resolves, that item is resolved with ``manual_match`` (an
    already-terminal item is tolerated). A line with a live match is
    a 409 ``BANK_LINE_ALREADY_MATCHED``.
    """
    require_idempotency_key(idempotency_key)
    with translate_errors():
        view = query_service.get_bank_statement(BankStatementRef(statement_id))
    row = require_visible_company(
        session,
        client,
        view.company,
        statement_id,
        resource_kind="Bank statement",
        error_code="LEDGER_STATEMENT_NOT_FOUND",
    )
    ref = company_ref_for_row(row)
    with translate_errors():
        match = service.apply_manual_match(
            tenant_id=client.tenant_id,
            company=ref,
            company_id=row.id,
            statement_id=statement_id,
            line_id=line_id,
            target_type=_parse_target_type(body.target_type),
            target_id=body.target_id,
            account_id=body.account_id,
            party_id=body.party_id,
            save_as_rule=body.save_as_rule,
            review_item_id=body.review_item_id,
            actor=client.client_name,
        )
    if body.review_item_id is not None:
        try:
            with translate_errors():
                review.perform_action(
                    item_id=body.review_item_id,
                    tenant_id=client.tenant_id,
                    action_code="manual_match",
                    actor=client.client_name,
                )
        except ReviewInvalidStateError:
            # Already resolved elsewhere; the match stands either way.
            pass
    session.commit()
    return BankStatementMatchResponse.from_match(match)


# -- learned match rules (US-514 B2) ------------------------------------------------


@router.get(
    "/bank/match-rules",
    response_model=list[MatchRuleResponse],
    summary="List learned match rules",
)
def list_match_rules(
    request: Request,
    company: str | None = Query(
        default=None, description="Filter by HesabYar company row id"
    ),
    client: AuthenticatedClient = Depends(require_scopes("banking:read")),
    service: BankQueryService = Depends(get_bank_query_service),
) -> list[MatchRuleResponse]:
    """Return the tenant's learned match rules, newest first."""
    reject_unknown_params(request, _RULE_LIST_PARAMS)
    with translate_errors():
        rules = service.list_rules(client.tenant_id, company)
    return [MatchRuleResponse.from_rule(rule) for rule in rules]


@router.post(
    "/bank/match-rules/{rule_id}/mode",
    response_model=MatchRuleResponse,
    summary="Promote/demote a learned rule between Suggest and Auto",
)
def set_match_rule_mode(
    rule_id: str,
    body: RuleModeRequest,
    idempotency_key: str | None = Header(
        default=None,
        alias=IDEMPOTENCY_KEY_HEADER,
        description="Unique key for this mutation; retries replay the original response",
    ),
    client: AuthenticatedClient = Depends(require_scopes("banking:reconcile")),
    session: Session = Depends(get_db_session),
    service: BankImportService = Depends(get_bank_import_service),
) -> MatchRuleResponse:
    """Set a learned rule's mode ('suggest' or 'auto') — a human decision."""
    require_idempotency_key(idempotency_key)
    with translate_errors():
        rule = service.set_rule_mode(
            tenant_id=client.tenant_id,
            rule_id=rule_id,
            mode=_parse_rule_mode(body.mode),
        )
    session.commit()
    return MatchRuleResponse.from_rule(rule)
