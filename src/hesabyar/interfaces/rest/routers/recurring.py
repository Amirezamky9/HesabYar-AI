"""Recurring-template routes (US-563, Phase 5-7 ۵خ W3).

Thin routes over the ``RecurringTemplateService``: no schedule math
here, no engine calls — the service owns the template lifecycle
and the generator owns the engine. The routes own only HTTP
concerns: auth scopes, company resolution, idempotency, audit
recording and response mapping.

Scopes (no new scopes — the existing catalog fits): an invoice
template is guarded by ``invoice:read`` /
``invoice:draft:create``; a journal template by ``ledger:read`` /
``ledger:draft:create``. The scope depends on the template's kind,
so it is checked inside the handler (after the kind is known)
instead of a static ``require_scopes`` dependency — except the
list, which needs at least one of the two read scopes and then
only returns the kinds the key may read.

Company scoping: creation takes its company from the request body
(``company_id`` — HesabYar row id or engine id). The list takes
an optional ``company`` filter (a restricted key is forced to its
own company). Get/update/pause/resume/delete/preview/occurrences
read the company from the template itself, and a template of
another tenant looks exactly like a template that does not exist
(404 ``RECURRING_TEMPLATE_NOT_FOUND``).

Idempotency (spec §10.4): ``Idempotency-Key`` is required on
template creation and update — a retried create must never
double-schedule the same invoices. Same key + same payload replays
the stored response with no second write; same key + different
payload is a 409 ``IDEMPOTENCY_CONFLICT``. Pause/resume/delete are
idempotent by nature and take no key.

Audit (spec §14.3): every mutation writes an audit event — success
and failure alike — with actor, operation, company, template id,
idempotency key, payload hash and UTC timestamp.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from hesabyar.application.commands.recurring import RecurringTemplateService
from hesabyar.domain.common.errors import HesabYarError
from hesabyar.domain.recurring.models import (
    InvoiceTemplatePayload,
    JournalTemplatePayload,
    TemplateKind,
    TemplateStatus,
)
from hesabyar.domain.scheduler.schedules import tehran_now
from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.infrastructure.persistence.postgres.models import EngineCompanyModel
from hesabyar.interfaces.rest.dependencies import (
    company_ref_for_row,
    get_authenticated_client,
    get_db_session,
    get_recurring_template_service,
    require_company_row_by_id,
)
from hesabyar.interfaces.rest.errors import ApiError, translate_errors
from hesabyar.interfaces.rest.idempotency import (
    IDEMPOTENCY_KEY_HEADER,
    Replay,
    begin_mutation,
    require_idempotency_key,
)
from hesabyar.interfaces.rest.mutations import fail_mutation, record_audit_event
from hesabyar.interfaces.rest.pagination import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    paginate,
    parse_sort_param,
    reject_unknown_params,
)
from hesabyar.interfaces.rest.schemas.pagination import PaginatedResponse
from hesabyar.interfaces.rest.schemas.recurring import (
    CreateRecurringTemplateRequest,
    OccurrencePreviewResponse,
    RecurringOccurrenceResponse,
    RecurringTemplateResponse,
    UpdateRecurringTemplateRequest,
)

router = APIRouter(tags=["recurring-templates"])

# Idempotency operation names (spec §10.4 triple:
# installation_id + operation + key). The audit action reuses the same name.
_OP_CREATE = "recurring.template.create"
_OP_UPDATE = "recurring.template.update"

_READ_SCOPES: dict[TemplateKind, str] = {
    TemplateKind.INVOICE: "invoice:read",
    TemplateKind.JOURNAL: "ledger:read",
}
_WRITE_SCOPES: dict[TemplateKind, str] = {
    TemplateKind.INVOICE: "invoice:draft:create",
    TemplateKind.JOURNAL: "ledger:draft:create",
}

_TEMPLATE_LIST_PARAMS = {"company", "kind", "status", "sort", "limit", "cursor"}
_TEMPLATE_SORT_KEYS: dict[str, Callable[[RecurringTemplateResponse], str]] = {
    "created": lambda item: item.created_at.isoformat(),
    "name": lambda item: item.name,
}
_OCCURRENCE_LIST_PARAMS = {"sort", "limit", "cursor"}
_OCCURRENCE_SORT_KEYS: dict[str, Callable[[RecurringOccurrenceResponse], str]] = {
    # Zero-padded: the paginator sorts string keys, and "10" < "9" lexically.
    "number": lambda item: f"{item.occurrence_number:08d}",
    "date": lambda item: item.scheduled_date.isoformat(),
}


def _require_kind_scope(
    client: AuthenticatedClient, kind: TemplateKind, *, write: bool
) -> None:
    """Require the kind's read or write scope (403 when missing)."""
    scope = _WRITE_SCOPES[kind] if write else _READ_SCOPES[kind]
    missing = client.missing_scopes(scope)
    if missing:
        raise ApiError(
            status_code=403,
            code="FORBIDDEN",
            detail=f"The API key lacks the required scope(s): {', '.join(missing)}.",
        )


def _readable_kinds(client: AuthenticatedClient) -> list[TemplateKind]:
    """The template kinds this key may read (empty when it may read none)."""
    return [
        kind for kind, scope in _READ_SCOPES.items() if client.has_scope(scope)
    ]


def _parse_kind(raw: str | None) -> TemplateKind | None:
    """Parse a ``kind`` filter (400 on an unknown value)."""
    if raw is None:
        return None
    try:
        return TemplateKind(raw)
    except ValueError as exc:
        raise ApiError(
            status_code=400,
            code="INVALID_FILTER",
            detail=f"Unknown kind {raw!r}; allowed: 'invoice', 'journal'.",
        ) from exc


def _parse_status(raw: str | None) -> TemplateStatus | None:
    """Parse a ``status`` filter (400 on an unknown value)."""
    if raw is None:
        return None
    try:
        return TemplateStatus(raw)
    except ValueError as exc:
        raise ApiError(
            status_code=400,
            code="INVALID_FILTER",
            detail=(
                f"Unknown status {raw!r}; allowed: 'active', 'paused', 'completed'."
            ),
        ) from exc


def _resolve_list_company(
    session: Session, client: AuthenticatedClient, company_param: str | None
) -> str | None:
    """The engine_companies row id to filter the list on, or None for all.

    An explicit parameter resolves like any company id (unknown →
    404, restricted away → 403); with no parameter, a restricted key
    is forced to its own company and an unrestricted key sees all
    of the tenant's companies.
    """
    if company_param is not None:
        return require_company_row_by_id(session, client, company_param).id
    if client.engine_company_id is None:
        return None
    row = session.get(EngineCompanyModel, client.engine_company_id)
    if row is None or row.tenant_id != client.tenant_id:
        raise ApiError(
            status_code=404,
            code="COMPANY_NOT_FOUND",
            detail="Company restricted to this API key does not exist.",
        )
    return row.id


def _payload_for_kind(
    body: CreateRecurringTemplateRequest,
) -> InvoiceTemplatePayload | JournalTemplatePayload:
    """Build the domain payload for the body's kind (validated at construction)."""
    if body.kind == TemplateKind.INVOICE.value:
        assert body.invoice is not None  # the schema validator enforces it
        return body.invoice.to_payload()
    assert body.journal is not None  # the schema validator enforces it
    return body.journal.to_payload()


@router.post(
    "/recurring-templates",
    response_model=RecurringTemplateResponse,
    status_code=201,
    summary="Create a recurring template",
)
def create_recurring_template(
    body: CreateRecurringTemplateRequest,
    idempotency_key: str | None = Header(
        default=None,
        alias=IDEMPOTENCY_KEY_HEADER,
        description="Unique key for this mutation; retries replay the original response",
    ),
    client: AuthenticatedClient = Depends(get_authenticated_client),
    session: Session = Depends(get_db_session),
    service: RecurringTemplateService = Depends(get_recurring_template_service),
) -> RecurringTemplateResponse | JSONResponse:
    """Create a template (starts ACTIVE; its first occurrence is generated by the next daily run)."""
    key = require_idempotency_key(idempotency_key)
    kind = TemplateKind(body.kind)
    _require_kind_scope(client, kind, write=True)
    row = require_company_row_by_id(session, client, body.company_id)
    ref = company_ref_for_row(row)
    claim_or_replay = begin_mutation(
        session,
        installation_id=client.installation_id,
        operation=_OP_CREATE,
        key=key,
        payload=body.model_dump(mode="json"),
    )
    if isinstance(claim_or_replay, Replay):
        return JSONResponse(
            status_code=claim_or_replay.status_code, content=claim_or_replay.body
        )
    claim = claim_or_replay
    try:
        with translate_errors():
            template = service.create_template(
                tenant_id=client.tenant_id,
                installation_id=client.installation_id,
                company_id=row.id,
                company_engine_id=ref.engine_id,
                kind=kind,
                name=body.name,
                schedule=body.schedule.to_schedule(),
                payload=_payload_for_kind(body),
                auto_post=body.auto_post,
            )
    except HesabYarError as exc:
        raise fail_mutation(
            session,
            client,
            claim,
            exc,
            action=_OP_CREATE,
            resource_type="recurring_template",
            resource_id=None,
            company_external_id=row.external_company_id,
        ) from exc
    response = RecurringTemplateResponse.from_template(template)
    claim.complete(
        response_status=201,
        response_body=response.model_dump(mode="json"),
        response_reference=f"recurring-templates/{template.id}",
    )
    record_audit_event(
        session,
        client,
        action=_OP_CREATE,
        resource_type="recurring_template",
        resource_id=template.id,
        company_external_id=row.external_company_id,
        outcome="success",
        payload_hash=claim.payload_hash,
        idempotency_key=claim.record.idempotency_key,
        metadata_after={
            "kind": template.kind.value,
            "status": template.status.value,
            "auto_post": template.auto_post,
            "total_amount_irr": template.total_amount_irr,
        },
    )
    session.commit()
    return response


@router.get(
    "/recurring-templates",
    response_model=PaginatedResponse[RecurringTemplateResponse],
    summary="List the tenant's recurring templates",
)
def list_recurring_templates(
    request: Request,
    company: str | None = Query(
        default=None,
        description="Filter on company (API company id); a restricted key is forced to its own",
    ),
    kind: str | None = Query(
        default=None, description="Filter on kind: 'invoice' or 'journal'"
    ),
    status: str | None = Query(
        default=None, description="Filter on status: 'active', 'paused' or 'completed'"
    ),
    sort: str | None = Query(
        default=None,
        description=(
            "Sort field: 'created' (default, newest first) or 'name'; "
            "prefix '-' for descending"
        ),
    ),
    limit: int = Query(
        default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"
    ),
    cursor: str | None = Query(
        default=None, description="Opaque cursor from a previous page's 'next_cursor'"
    ),
    client: AuthenticatedClient = Depends(get_authenticated_client),
    session: Session = Depends(get_db_session),
    service: RecurringTemplateService = Depends(get_recurring_template_service),
) -> PaginatedResponse[RecurringTemplateResponse]:
    """Return the tenant's templates, cursor-paginated, newest first.

    A key with only one read scope sees only that kind; a key with
    neither gets 403. A ``kind`` filter naming a kind the key cannot
    read is 403 too.
    """
    reject_unknown_params(request, _TEMPLATE_LIST_PARAMS)
    readable = _readable_kinds(client)
    if not readable:
        raise ApiError(
            status_code=403,
            code="FORBIDDEN",
            detail=(
                "The API key lacks the required scope(s): invoice:read, ledger:read."
            ),
        )
    kind_filter = _parse_kind(kind)
    if kind_filter is not None and kind_filter not in readable:
        _require_kind_scope(client, kind_filter, write=False)
    status_filter = _parse_status(status)
    company_id = _resolve_list_company(session, client, company)
    field, reverse = parse_sort_param(sort, _TEMPLATE_SORT_KEYS, default="created")
    if sort is None or sort == "":
        # Default order is newest first, matching the repository's order.
        reverse = True
    with translate_errors():
        templates = service.list_templates(
            client.tenant_id,
            company_id=company_id,
            kind=kind_filter,
            status=status_filter,
        )
    items = [
        RecurringTemplateResponse.from_template(template)
        for template in templates
        if template.kind in readable
    ]
    page, next_cursor = paginate(
        items,
        key_fn=_TEMPLATE_SORT_KEYS[field],
        tie_fn=lambda item: item.id,
        limit=limit,
        cursor=cursor,
        reverse=reverse,
    )
    return PaginatedResponse(items=page, next_cursor=next_cursor)


@router.get(
    "/recurring-templates/{template_id}",
    response_model=RecurringTemplateResponse,
    summary="Get one recurring template",
)
def get_recurring_template(
    template_id: str,
    client: AuthenticatedClient = Depends(get_authenticated_client),
    service: RecurringTemplateService = Depends(get_recurring_template_service),
) -> RecurringTemplateResponse:
    """Return one template of this tenant (404 when it does not exist here)."""
    with translate_errors():
        template = service.get_template(client.tenant_id, template_id)
    _require_kind_scope(client, template.kind, write=False)
    return RecurringTemplateResponse.from_template(template)


@router.put(
    "/recurring-templates/{template_id}",
    response_model=RecurringTemplateResponse,
    summary="Update a recurring template",
)
def update_recurring_template(
    template_id: str,
    body: UpdateRecurringTemplateRequest,
    idempotency_key: str | None = Header(
        default=None,
        alias=IDEMPOTENCY_KEY_HEADER,
        description="Unique key for this mutation; retries replay the original response",
    ),
    client: AuthenticatedClient = Depends(get_authenticated_client),
    session: Session = Depends(get_db_session),
    service: RecurringTemplateService = Depends(get_recurring_template_service),
) -> RecurringTemplateResponse | JSONResponse:
    """Update a template (only the given fields change).

    The schedule shape cannot change once the template has an
    occurrence; a payload edit only affects future occurrences
    (each occurrence keeps its snapshot).
    """
    key = require_idempotency_key(idempotency_key)
    with translate_errors():
        current = service.get_template(client.tenant_id, template_id)
    _require_kind_scope(client, current.kind, write=True)
    claim_or_replay = begin_mutation(
        session,
        installation_id=client.installation_id,
        operation=_OP_UPDATE,
        key=key,
        payload={"template_id": template_id, **body.model_dump(mode="json")},
    )
    if isinstance(claim_or_replay, Replay):
        return JSONResponse(
            status_code=claim_or_replay.status_code, content=claim_or_replay.body
        )
    claim = claim_or_replay
    try:
        with translate_errors():
            payload: InvoiceTemplatePayload | JournalTemplatePayload | None = None
            if body.invoice is not None:
                payload = body.invoice.to_payload()
            elif body.journal is not None:
                payload = body.journal.to_payload()
            template = service.update_template(
                client.tenant_id,
                template_id,
                name=body.name,
                schedule=body.schedule.to_schedule() if body.schedule is not None else None,
                payload=payload,
                auto_post=body.auto_post,
            )
    except HesabYarError as exc:
        raise fail_mutation(
            session,
            client,
            claim,
            exc,
            action=_OP_UPDATE,
            resource_type="recurring_template",
            resource_id=template_id,
            company_external_id=None,
        ) from exc
    response = RecurringTemplateResponse.from_template(template)
    claim.complete(
        response_status=200,
        response_body=response.model_dump(mode="json"),
        response_reference=f"recurring-templates/{template.id}",
    )
    record_audit_event(
        session,
        client,
        action=_OP_UPDATE,
        resource_type="recurring_template",
        resource_id=template.id,
        company_external_id=None,
        outcome="success",
        payload_hash=claim.payload_hash,
        idempotency_key=claim.record.idempotency_key,
        metadata_after={
            "kind": template.kind.value,
            "status": template.status.value,
            "auto_post": template.auto_post,
            "total_amount_irr": template.total_amount_irr,
        },
    )
    session.commit()
    return response


@router.delete(
    "/recurring-templates/{template_id}",
    status_code=204,
    summary="Delete a recurring template that never fired",
)
def delete_recurring_template(
    template_id: str,
    client: AuthenticatedClient = Depends(get_authenticated_client),
    session: Session = Depends(get_db_session),
    service: RecurringTemplateService = Depends(get_recurring_template_service),
) -> None:
    """Delete a template with no occurrences (409 when it already fired — pause it instead)."""
    with translate_errors():
        current = service.get_template(client.tenant_id, template_id)
    _require_kind_scope(client, current.kind, write=True)
    with translate_errors():
        service.delete_template(client.tenant_id, template_id)
    record_audit_event(
        session,
        client,
        action="recurring.template.delete",
        resource_type="recurring_template",
        resource_id=template_id,
        company_external_id=None,
        outcome="success",
    )
    session.commit()


@router.post(
    "/recurring-templates/preview",
    response_model=list[OccurrencePreviewResponse],
    summary="Preview an unsaved template's next occurrences",
)
def preview_unsaved_template(
    body: CreateRecurringTemplateRequest,
    count: int = Query(
        default=12, ge=1, le=120, description="How many occurrences to preview (1-120)"
    ),
    client: AuthenticatedClient = Depends(get_authenticated_client),
    session: Session = Depends(get_db_session),
    service: RecurringTemplateService = Depends(get_recurring_template_service),
) -> list[OccurrencePreviewResponse]:
    """Preview the next N occurrences (dates + amounts + clamp report) before saving.

    The body is validated exactly as a create would be (the company
    resolves, the payload invariants hold), but nothing is saved.
    """
    kind = TemplateKind(body.kind)
    _require_kind_scope(client, kind, write=True)
    require_company_row_by_id(session, client, body.company_id)
    with translate_errors():
        payload = _payload_for_kind(body)
        previews = service.preview(
            schedule=body.schedule.to_schedule(),
            total_amount_irr=payload.total_amount_irr,
            count=count,
        )
    return [OccurrencePreviewResponse.from_preview(preview) for preview in previews]


@router.get(
    "/recurring-templates/{template_id}/preview",
    response_model=list[OccurrencePreviewResponse],
    summary="Preview a template's next occurrences",
)
def preview_recurring_template(
    template_id: str,
    count: int = Query(
        default=12, ge=1, le=120, description="How many occurrences to preview (1-120)"
    ),
    client: AuthenticatedClient = Depends(get_authenticated_client),
    service: RecurringTemplateService = Depends(get_recurring_template_service),
) -> list[OccurrencePreviewResponse]:
    """Preview an existing template's next N occurrences from today (Tehran)."""
    with translate_errors():
        template = service.get_template(client.tenant_id, template_id)
    _require_kind_scope(client, template.kind, write=False)
    with translate_errors():
        previews = service.preview_template(
            client.tenant_id,
            template_id,
            count=count,
            after=tehran_now().date(),
        )
    return [OccurrencePreviewResponse.from_preview(preview) for preview in previews]


@router.post(
    "/recurring-templates/{template_id}/pause",
    response_model=RecurringTemplateResponse,
    summary="Pause a recurring template",
)
def pause_recurring_template(
    template_id: str,
    client: AuthenticatedClient = Depends(get_authenticated_client),
    session: Session = Depends(get_db_session),
    service: RecurringTemplateService = Depends(get_recurring_template_service),
) -> RecurringTemplateResponse:
    """Pause a template (idempotent; its due occurrences wait for resume)."""
    with translate_errors():
        current = service.get_template(client.tenant_id, template_id)
    _require_kind_scope(client, current.kind, write=True)
    with translate_errors():
        template = service.pause_template(client.tenant_id, template_id)
    record_audit_event(
        session,
        client,
        action="recurring.template.pause",
        resource_type="recurring_template",
        resource_id=template.id,
        company_external_id=None,
        outcome="success",
        metadata_after={"status": template.status.value},
    )
    session.commit()
    return RecurringTemplateResponse.from_template(template)


@router.post(
    "/recurring-templates/{template_id}/resume",
    response_model=RecurringTemplateResponse,
    summary="Resume a paused recurring template",
)
def resume_recurring_template(
    template_id: str,
    client: AuthenticatedClient = Depends(get_authenticated_client),
    session: Session = Depends(get_db_session),
    service: RecurringTemplateService = Depends(get_recurring_template_service),
) -> RecurringTemplateResponse:
    """Resume a paused template (idempotent; missed occurrences catch up on the next run)."""
    with translate_errors():
        current = service.get_template(client.tenant_id, template_id)
    _require_kind_scope(client, current.kind, write=True)
    with translate_errors():
        template = service.resume_template(client.tenant_id, template_id)
    record_audit_event(
        session,
        client,
        action="recurring.template.resume",
        resource_type="recurring_template",
        resource_id=template.id,
        company_external_id=None,
        outcome="success",
        metadata_after={"status": template.status.value},
    )
    session.commit()
    return RecurringTemplateResponse.from_template(template)


@router.get(
    "/recurring-templates/{template_id}/occurrences",
    response_model=PaginatedResponse[RecurringOccurrenceResponse],
    summary="List a template's generated occurrences",
)
def list_recurring_occurrences(
    template_id: str,
    request: Request,
    sort: str | None = Query(
        default=None,
        description=(
            "Sort field: 'number' (default, oldest first) or 'date'; "
            "prefix '-' for descending"
        ),
    ),
    limit: int = Query(
        default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"
    ),
    cursor: str | None = Query(
        default=None, description="Opaque cursor from a previous page's 'next_cursor'"
    ),
    client: AuthenticatedClient = Depends(get_authenticated_client),
    service: RecurringTemplateService = Depends(get_recurring_template_service),
) -> PaginatedResponse[RecurringOccurrenceResponse]:
    """Return a template's occurrences, in occurrence-number order."""
    reject_unknown_params(request, _OCCURRENCE_LIST_PARAMS)
    with translate_errors():
        template = service.get_template(client.tenant_id, template_id)
    _require_kind_scope(client, template.kind, write=False)
    field, reverse = parse_sort_param(sort, _OCCURRENCE_SORT_KEYS, default="number")
    with translate_errors():
        occurrences = service.list_occurrences(client.tenant_id, template_id)
    items = [
        RecurringOccurrenceResponse.from_occurrence(occurrence)
        for occurrence in occurrences
    ]
    page, next_cursor = paginate(
        items,
        key_fn=_OCCURRENCE_SORT_KEYS[field],
        tie_fn=lambda item: item.id,
        limit=limit,
        cursor=cursor,
        reverse=reverse,
    )
    return PaginatedResponse(items=page, next_cursor=next_cursor)
