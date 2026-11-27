"""Manual-import batch routes (US-554, Phase 5-6 ۵ح).

One batch cycle for every manual import (statement B1 + settlement
P2): detect → upload → preview → approve → pipeline → variance
report; reject (archive with reason) and re-run.

Thin routes over the manual-import service: no accounting rules
here — the service owns the cycle and delegates the import work to
the US-514 bank import and US-523 settlement import services.

Scopes are PER KIND (the batch kind decides the scope — a key with
only ``banking:import`` can run statement batches but not
settlement batches):

- statements: ``banking:read`` (reads) / ``banking:import`` (writes);
- settlements: ``settlement:read`` (reads) / ``settlement:manage`` (writes).

Mutations require an ``Idempotency-Key`` header. Uploads are base64
JSON (the admin BFF proxy parses bodies as JSON only); file bytes
are never stored — only the filename + hash for provenance.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Query, Request, status
from sqlalchemy.orm import Session

from hesabyar.application.commands.manual_imports import ManualImportService
from hesabyar.domain.bank.models import FileCurrencyUnit, StatementFileFormat
from hesabyar.domain.manual_imports.models import ManualImportBatchStatus, ManualImportKind
from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.interfaces.rest.dependencies import (
    company_ref_for_row,
    get_authenticated_client,
    get_db_session,
    get_manual_import_service,
    require_company_row_by_id,
    require_scopes,
)
from hesabyar.interfaces.rest.errors import ApiError, translate_errors
from hesabyar.interfaces.rest.idempotency import (
    IDEMPOTENCY_KEY_HEADER,
    require_idempotency_key,
)
from hesabyar.interfaces.rest.pagination import reject_unknown_params
from hesabyar.interfaces.rest.schemas.bank import FileDetectionResponse
from hesabyar.interfaces.rest.schemas.imports import (
    ApproveBatchRequest,
    ApproveBatchResponse,
    BatchVarianceReportResponse,
    DetectImportFileRequest,
    ImportBatchPreviewResponse,
    ImportBatchUploadRequest,
    ManualImportBatchResponse,
    RejectBatchRequest,
    RetryPostingResponse,
    SettlementColumnMappingCreateRequest,
    SettlementColumnMappingResponse,
    UploadBatchResponse,
)

router = APIRouter(tags=["imports"])

_LIST_PARAMS = {"kind", "status", "company", "source_import_id"}
_MAPPING_LIST_PARAMS = {"channel_key", "company"}

#: Per-kind scopes: (read scope, manage scope).
_KIND_SCOPES: dict[ManualImportKind, tuple[str, str]] = {
    ManualImportKind.STATEMENT: ("banking:read", "banking:import"),
    ManualImportKind.SETTLEMENT: ("settlement:read", "settlement:manage"),
}


def _parse_kind(value: str) -> ManualImportKind:
    try:
        return ManualImportKind(value)
    except ValueError:
        raise ApiError(
            status_code=422,
            code="VALIDATION_ERROR",
            detail=f"Unknown batch kind {value!r} (expected 'statement' or 'settlement').",
        ) from None


def _parse_batch_status(value: str) -> ManualImportBatchStatus:
    try:
        return ManualImportBatchStatus(value)
    except ValueError:
        raise ApiError(
            status_code=422,
            code="VALIDATION_ERROR",
            detail=f"Unknown batch status {value!r}.",
        ) from None


def _parse_file_format(value: str) -> StatementFileFormat:
    try:
        return StatementFileFormat(value)
    except ValueError:
        raise ApiError(
            status_code=422,
            code="SETTLEMENT_MAPPING_INVALID",
            detail=f"Unknown file format {value!r} (expected 'xlsx', 'csv' or 'pdf').",
        ) from None


def _parse_currency_unit(value: str) -> FileCurrencyUnit:
    try:
        return FileCurrencyUnit(value)
    except ValueError:
        raise ApiError(
            status_code=422,
            code="VALIDATION_ERROR",
            detail=f"Unknown currency unit {value!r} (expected 'rial' or 'toman').",
        ) from None


def _require_kind_scope(
    client: AuthenticatedClient, kind: ManualImportKind, *, manage: bool
) -> None:
    """Require the read or manage scope for one batch kind (403 otherwise)."""
    read_scope, manage_scope = _KIND_SCOPES[kind]
    scope = manage_scope if manage else read_scope
    if not client.has_scope(scope):
        raise ApiError(
            status_code=403,
            code="FORBIDDEN",
            detail=f"The API key lacks the required scope(s): {scope}.",
        )


def _readable_kinds(client: AuthenticatedClient) -> list[ManualImportKind]:
    """The batch kinds this key may read (for the unfiltered list)."""
    return [
        kind
        for kind, (read_scope, _manage_scope) in _KIND_SCOPES.items()
        if client.has_scope(read_scope)
    ]


# -- detection ---------------------------------------------------------------


@router.post(
    "/imports/batches/detect",
    response_model=FileDetectionResponse,
    summary="Detect a file's format and columns (no persistence)",
)
def detect_import_file(
    body: DetectImportFileRequest,
    client: AuthenticatedClient = Depends(get_authenticated_client),
    service: ManualImportService = Depends(get_manual_import_service),
) -> FileDetectionResponse:
    """Read an uploaded file and suggest a column mapping.

    Pure detection — nothing is stored. An unreadable file is a 422
    (quarantined, never guessed). The scope follows the batch kind.
    """
    kind = _parse_kind(body.kind)
    _require_kind_scope(client, kind, manage=True)
    with translate_errors():
        detection = service.detect_file(
            kind=kind,
            content=body.content_bytes(),
            filename=body.filename,
            file_type=body.file_type,
        )
    return FileDetectionResponse.from_detection(detection)


# -- batches -----------------------------------------------------------------


@router.post(
    "/imports/batches",
    response_model=UploadBatchResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a statement or settlement file (creates a batch awaiting review)",
)
def upload_import_batch(
    body: ImportBatchUploadRequest,
    idempotency_key: str | None = Header(
        default=None,
        alias=IDEMPOTENCY_KEY_HEADER,
        description="Unique key for this mutation; retries replay the original response",
    ),
    client: AuthenticatedClient = Depends(get_authenticated_client),
    session: Session = Depends(get_db_session),
    service: ManualImportService = Depends(get_manual_import_service),
) -> UploadBatchResponse:
    """Parse an uploaded file into a batch awaiting review.

    The parse stages (uploaded → mapped → validated → previewed)
    complete inside this call and are recorded in the stage history;
    the batch is born ``awaiting_review`` with its accounting-effect
    preview computed and a ``manual_import_review`` queue item filed.
    Re-uploading the same file for the same source is a no-op
    returning the existing batch (``duplicate: true``).
    """
    require_idempotency_key(idempotency_key)
    kind = _parse_kind(body.kind)
    _require_kind_scope(client, kind, manage=True)
    row = require_company_row_by_id(session, client, body.company)
    ref = company_ref_for_row(row)
    with translate_errors():
        if kind == ManualImportKind.STATEMENT:
            if body.statement_journal_id is None or body.default_account_id is None:
                raise ApiError(
                    status_code=422,
                    code="VALIDATION_ERROR",
                    detail="A statement upload needs statement_journal_id and default_account_id.",
                )
            result = service.upload_statement_batch(
                tenant_id=client.tenant_id,
                installation_id=client.installation_id,
                company=ref,
                company_id=row.id,
                bank_key=body.source_key,
                statement_journal_id=body.statement_journal_id,
                filename=body.filename,
                content=body.content_bytes(),
                currency_unit=_parse_currency_unit(body.currency_unit),
                default_account_id=body.default_account_id,
                mapping_id=body.mapping_id,
                mapping_columns=body.columns,
                save_mapping=body.save_mapping,
                mapping_name=body.mapping_name,
                date_kind=body.date_kind,
                header_row=body.header_row,
                actor=client.client_name,
            )
        else:
            if body.period_jalali is None:
                raise ApiError(
                    status_code=422,
                    code="VALIDATION_ERROR",
                    detail="A settlement upload needs period_jalali.",
                )
            result = service.upload_settlement_batch(
                tenant_id=client.tenant_id,
                installation_id=client.installation_id,
                company_id=row.id,
                channel_key=body.source_key,
                file_type=body.file_type,
                period_jalali=body.period_jalali,
                filename=body.filename,
                content=body.content_bytes(),
                currency_unit=body.currency_unit,
                mapping_id=body.mapping_id,
                mapping_columns=body.columns,
                save_mapping=body.save_mapping,
                mapping_name=body.mapping_name,
                date_kind=body.date_kind,
                header_row=body.header_row,
                actor=client.client_name,
            )
    session.commit()
    return UploadBatchResponse.from_result(result)


@router.get(
    "/imports/batches",
    response_model=list[ManualImportBatchResponse],
    summary="List manual-import batches (the dashboard's manual-imports section reads this)",
)
def list_import_batches(
    request: Request,
    kind: str | None = Query(default=None, description="Filter by batch kind"),
    status_filter: str | None = Query(
        default=None, alias="status", description="Filter by batch status"
    ),
    company: str | None = Query(
        default=None, description="Filter by HesabYar company row id"
    ),
    source_import_id: str | None = Query(
        default=None, description="Filter by underlying import id"
    ),
    client: AuthenticatedClient = Depends(get_authenticated_client),
    service: ManualImportService = Depends(get_manual_import_service),
) -> list[ManualImportBatchResponse]:
    """Return the tenant's import batches, newest first.

    Without a ``kind`` filter, only the kinds the key may read are
    returned (a banking-only key never sees settlement batches).
    """
    reject_unknown_params(request, _LIST_PARAMS)
    wanted_kind = _parse_kind(kind) if kind is not None else None
    wanted_status = _parse_batch_status(status_filter) if status_filter is not None else None
    if wanted_kind is not None:
        _require_kind_scope(client, wanted_kind, manage=False)
        kinds = [wanted_kind]
    else:
        kinds = _readable_kinds(client)
        if not kinds:
            raise ApiError(
                status_code=403,
                code="FORBIDDEN",
                detail="The API key lacks the required scope(s): banking:read, settlement:read.",
            )
    with translate_errors():
        batches = service.list_batches(
            client.tenant_id,
            status=wanted_status,
            company_id=company,
            source_import_id=source_import_id,
        )
    items = [
        ManualImportBatchResponse.from_batch(batch)
        for batch in batches
        if batch.kind in kinds
    ]
    return items


@router.get(
    "/imports/batches/{batch_id}",
    response_model=ManualImportBatchResponse,
    summary="Get one import batch with its preview and stage history",
)
def get_import_batch(
    batch_id: str,
    client: AuthenticatedClient = Depends(get_authenticated_client),
    service: ManualImportService = Depends(get_manual_import_service),
) -> ManualImportBatchResponse:
    """Return one import batch. Unknown id → 404."""
    with translate_errors():
        batch = service.get_batch(client.tenant_id, batch_id)
    _require_kind_scope(client, batch.kind, manage=False)
    return ManualImportBatchResponse.from_batch(batch)


@router.get(
    "/imports/batches/{batch_id}/preview",
    response_model=ImportBatchPreviewResponse,
    summary="Get one batch's accounting-effect preview",
)
def get_import_batch_preview(
    batch_id: str,
    client: AuthenticatedClient = Depends(get_authenticated_client),
    service: ManualImportService = Depends(get_manual_import_service),
) -> ImportBatchPreviewResponse:
    """Return the stored preview (computed by the same code as the real effect)."""
    with translate_errors():
        batch = service.get_batch(client.tenant_id, batch_id)
    _require_kind_scope(client, batch.kind, manage=False)
    if batch.preview is None:
        raise ApiError(
            status_code=404,
            code="MANUAL_IMPORT_NOT_FOUND",
            detail=f"Import batch {batch_id} has no preview yet.",
        )
    return ImportBatchPreviewResponse.from_preview(batch.preview)


@router.get(
    "/imports/batches/{batch_id}/variance",
    response_model=BatchVarianceReportResponse,
    summary="Get one batch's variance report (expected vs imported vs posted)",
)
def get_import_batch_variance(
    batch_id: str,
    client: AuthenticatedClient = Depends(get_authenticated_client),
    service: ManualImportService = Depends(get_manual_import_service),
) -> BatchVarianceReportResponse:
    """Return the batch variance report (computed live from the stored rows)."""
    with translate_errors():
        batch = service.get_batch(client.tenant_id, batch_id)
    _require_kind_scope(client, batch.kind, manage=False)
    with translate_errors():
        report = service.variance_report(tenant_id=client.tenant_id, batch_id=batch_id)
    return BatchVarianceReportResponse.from_report(report)


@router.post(
    "/imports/batches/{batch_id}/approve",
    response_model=ApproveBatchResponse,
    summary="Approve a batch (runs the normal pipeline; draft by default)",
)
def approve_import_batch(
    batch_id: str,
    body: ApproveBatchRequest | None = None,
    idempotency_key: str | None = Header(
        default=None,
        alias=IDEMPOTENCY_KEY_HEADER,
        description="Unique key for this mutation; retries replay the original response",
    ),
    client: AuthenticatedClient = Depends(get_authenticated_client),
    session: Session = Depends(get_db_session),
    service: ManualImportService = Depends(get_manual_import_service),
) -> ApproveBatchResponse:
    """Approve an awaiting-review batch through the normal pipeline.

    Statements: the statement is written to the engine as a DRAFT
    and the batch posts. Settlements: tranches confirm + the
    settlement event is created; the tie-out + auto-post runs ONLY
    with explicit ``auto_post`` opt-in (and the policy gate must
    still allow it) — otherwise the batch stays ``approved`` with
    ``posting_status=draft_hold``. A batch in any other state is a 409.
    """
    require_idempotency_key(idempotency_key)
    with translate_errors():
        batch = service.get_batch(client.tenant_id, batch_id)
    _require_kind_scope(client, batch.kind, manage=True)
    row = require_company_row_by_id(session, client, batch.company_id)
    ref = company_ref_for_row(row)
    with translate_errors():
        result = service.approve_batch(
            tenant_id=client.tenant_id,
            batch_id=batch_id,
            company=ref,
            auto_post=body.auto_post if body is not None else False,
            actor=client.client_name,
        )
    session.commit()
    return ApproveBatchResponse.from_result(result)


@router.post(
    "/imports/batches/{batch_id}/reject",
    response_model=ManualImportBatchResponse,
    summary="Reject a batch (archived with its reason; re-runnable)",
)
def reject_import_batch(
    batch_id: str,
    body: RejectBatchRequest,
    idempotency_key: str | None = Header(
        default=None,
        alias=IDEMPOTENCY_KEY_HEADER,
        description="Unique key for this mutation; retries replay the original response",
    ),
    client: AuthenticatedClient = Depends(get_authenticated_client),
    session: Session = Depends(get_db_session),
    service: ManualImportService = Depends(get_manual_import_service),
) -> ManualImportBatchResponse:
    """Reject an awaiting-review batch with a reason.

    The underlying import is rejected/cancelled (its rows stay
    stored); the batch is archived with the reason and can be
    re-run. A batch in any other state is a 409.
    """
    require_idempotency_key(idempotency_key)
    with translate_errors():
        batch = service.get_batch(client.tenant_id, batch_id)
    _require_kind_scope(client, batch.kind, manage=True)
    with translate_errors():
        rejected = service.reject_batch(
            tenant_id=client.tenant_id,
            batch_id=batch_id,
            reason=body.reason,
            actor=client.client_name,
        )
    session.commit()
    return ManualImportBatchResponse.from_batch(rejected)


@router.post(
    "/imports/batches/{batch_id}/re-run",
    response_model=ManualImportBatchResponse,
    summary="Re-run an archived batch (a new attempt on the same batch)",
)
def rerun_import_batch(
    batch_id: str,
    idempotency_key: str | None = Header(
        default=None,
        alias=IDEMPOTENCY_KEY_HEADER,
        description="Unique key for this mutation; retries replay the original response",
    ),
    client: AuthenticatedClient = Depends(get_authenticated_client),
    session: Session = Depends(get_db_session),
    service: ManualImportService = Depends(get_manual_import_service),
) -> ManualImportBatchResponse:
    """Re-run a rejected/archived batch from its stored rows (no re-upload).

    The underlying import reopens, the preview is recomputed, a
    fresh review item is filed, and the batch returns to
    ``awaiting_review`` with ``attempt`` incremented. A batch in
    any other state is a 409.
    """
    require_idempotency_key(idempotency_key)
    with translate_errors():
        batch = service.get_batch(client.tenant_id, batch_id)
    _require_kind_scope(client, batch.kind, manage=True)
    with translate_errors():
        rerun = service.rerun_batch(
            tenant_id=client.tenant_id,
            batch_id=batch_id,
            actor=client.client_name,
        )
    session.commit()
    return ManualImportBatchResponse.from_batch(rerun)


@router.post(
    "/imports/batches/{batch_id}/retry-posting",
    response_model=RetryPostingResponse,
    summary="Retry the posting step of an approved settlement batch",
)
def retry_batch_posting(
    batch_id: str,
    idempotency_key: str | None = Header(
        default=None,
        alias=IDEMPOTENCY_KEY_HEADER,
        description="Unique key for this mutation; retries replay the original response",
    ),
    client: AuthenticatedClient = Depends(get_authenticated_client),
    session: Session = Depends(get_db_session),
    service: ManualImportService = Depends(get_manual_import_service),
) -> RetryPostingResponse:
    """Re-run the tie-out + auto-post for an approved settlement batch.

    Use after recording the missing bank deposit or fixing the gate.
    A batch whose import already settled syncs to ``posted`` without
    re-posting. A batch in any other state is a 409.
    """
    require_idempotency_key(idempotency_key)
    with translate_errors():
        batch = service.get_batch(client.tenant_id, batch_id)
    _require_kind_scope(client, batch.kind, manage=True)
    row = require_company_row_by_id(session, client, batch.company_id)
    ref = company_ref_for_row(row)
    with translate_errors():
        result = service.retry_posting(
            tenant_id=client.tenant_id,
            batch_id=batch_id,
            company=ref,
            actor=client.client_name,
        )
    session.commit()
    return RetryPostingResponse.from_result(result)


# -- settlement column mappings -----------------------------------------------


@router.get(
    "/imports/settlement-column-mappings",
    response_model=list[SettlementColumnMappingResponse],
    summary="List saved per-channel settlement column mappings",
)
def list_settlement_column_mappings(
    request: Request,
    channel_key: str | None = Query(default=None, description="Filter by channel key"),
    company: str | None = Query(
        default=None, description="Filter by HesabYar company row id"
    ),
    client: AuthenticatedClient = Depends(require_scopes("settlement:read")),
    service: ManualImportService = Depends(get_manual_import_service),
) -> list[SettlementColumnMappingResponse]:
    """Return the tenant's saved settlement mappings (active first, newest first)."""
    reject_unknown_params(request, _MAPPING_LIST_PARAMS)
    with translate_errors():
        mappings = service.list_settlement_mappings(client.tenant_id, channel_key, company)
    return [SettlementColumnMappingResponse.from_mapping(m) for m in mappings]


@router.post(
    "/imports/settlement-column-mappings",
    response_model=SettlementColumnMappingResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Save a per-channel settlement column mapping",
)
def create_settlement_column_mapping(
    body: SettlementColumnMappingCreateRequest,
    idempotency_key: str | None = Header(
        default=None,
        alias=IDEMPOTENCY_KEY_HEADER,
        description="Unique key for this mutation; retries replay the original response",
    ),
    client: AuthenticatedClient = Depends(require_scopes("settlement:manage")),
    session: Session = Depends(get_db_session),
    service: ManualImportService = Depends(get_manual_import_service),
) -> SettlementColumnMappingResponse:
    """Save a per-channel settlement column mapping (validated before it is stored)."""
    require_idempotency_key(idempotency_key)
    if body.company is not None:
        require_company_row_by_id(session, client, body.company)
    with translate_errors():
        mapping = service.save_settlement_mapping(
            tenant_id=client.tenant_id,
            channel_key=body.channel_key,
            name=body.name,
            file_format=_parse_file_format(body.file_format),
            file_type=body.file_type,
            columns=body.columns,
            date_kind=body.date_kind,
            currency_unit=_parse_currency_unit(body.currency_unit),
            header_row=body.header_row,
            company_id=body.company,
        )
    session.commit()
    return SettlementColumnMappingResponse.from_mapping(mapping)


@router.get(
    "/imports/settlement-column-mappings/{mapping_id}",
    response_model=SettlementColumnMappingResponse,
    summary="Get one saved settlement column mapping",
)
def get_settlement_column_mapping(
    mapping_id: str,
    client: AuthenticatedClient = Depends(require_scopes("settlement:read")),
    service: ManualImportService = Depends(get_manual_import_service),
) -> SettlementColumnMappingResponse:
    """Return one saved settlement mapping. Unknown id → 404."""
    with translate_errors():
        mapping = service.get_settlement_mapping(client.tenant_id, mapping_id)
    return SettlementColumnMappingResponse.from_mapping(mapping)


@router.post(
    "/imports/settlement-column-mappings/{mapping_id}/supersede",
    response_model=SettlementColumnMappingResponse,
    summary="Supersede a saved settlement column mapping",
)
def supersede_settlement_column_mapping(
    mapping_id: str,
    idempotency_key: str | None = Header(
        default=None,
        alias=IDEMPOTENCY_KEY_HEADER,
        description="Unique key for this mutation; retries replay the original response",
    ),
    client: AuthenticatedClient = Depends(require_scopes("settlement:manage")),
    session: Session = Depends(get_db_session),
    service: ManualImportService = Depends(get_manual_import_service),
) -> SettlementColumnMappingResponse:
    """Supersede a mapping (it stays for provenance, unused for new imports)."""
    require_idempotency_key(idempotency_key)
    with translate_errors():
        mapping = service.supersede_settlement_mapping(
            tenant_id=client.tenant_id, mapping_id=mapping_id
        )
    session.commit()
    return SettlementColumnMappingResponse.from_mapping(mapping)
