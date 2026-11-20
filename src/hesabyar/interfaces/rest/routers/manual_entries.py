"""Manual-entry routes: dual-entry manual order/invoice/refund (US-513).

Thin routes over the manual-entry command/query services: no envelope
construction here, no pipeline logic — the services own those.

- ``POST /manual-entries/orders`` — enter one order manually
  (→ ``sales.order.completed``).
- ``POST /manual-entries/refunds`` — enter one refund manually
  (→ ``sales.refund.created``, + ``payment.refunded`` when the money
  was returned too).
- ``POST /manual-entries/cancellations`` — cancel one order manually
  (→ ``sales.order.cancelled``).
- ``GET /manual-entries`` / ``{entry_id}`` — list/get entries with
  the pipeline trace of the event(s) each entry produced.

Every manual entry produces the SAME 15-field canonical envelope a
store webhook would produce (``source_system="manual"``) and travels
the SAME §10.3 pipeline (validation, mapping, policy gate, audit
log) — dual entry per ADR 0003 decision 1. Money in requests and
responses is whole Toman; the pipeline converts ×10 to Rial.

Mutations need ``integrations:manage`` and a unique
``Idempotency-Key`` header (spec §9.1): a retry with the same key
and payload replays the original 201 response, never a second
entry. Reads need ``integrations:read``. Everything is
tenant/installation-scoped: a row of another installation answers
404 as if it did not exist.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, Path, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from hesabyar.application.commands.manual_entries import ManualEntryService
from hesabyar.application.queries.manual_entries import ManualEntryQueryService
from hesabyar.domain.common.errors import HesabYarError
from hesabyar.domain.events.manual_entries import (
    CreateManualCancellation,
    CreateManualOrder,
    CreateManualRefund,
    ManualEntryType,
    ManualOrderLine,
    ManualRefundLine,
)
from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.interfaces.rest.dependencies import (
    get_db_session,
    get_manual_entry_query_service,
    get_manual_entry_service,
    require_scopes,
)
from hesabyar.interfaces.rest.errors import (
    ApiError,
    api_error_from_hesabyar_error,
    translate_errors,
)
from hesabyar.interfaces.rest.idempotency import (
    IDEMPOTENCY_KEY_HEADER,
    Replay,
    begin_mutation,
    require_idempotency_key,
)
from hesabyar.interfaces.rest.pagination import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    paginate,
    parse_sort_param,
    reject_unknown_params,
)
from hesabyar.interfaces.rest.schemas.manual_entries import (
    CreateManualCancellationRequest,
    CreateManualOrderRequest,
    CreateManualRefundRequest,
    ManualEntryResponse,
)
from hesabyar.interfaces.rest.schemas.pagination import PaginatedResponse

router = APIRouter(tags=["manual-entries"])

_OP_CREATE_ORDER = "manual_entries.orders.create"
_OP_CREATE_REFUND = "manual_entries.refunds.create"
_OP_CREATE_CANCELLATION = "manual_entries.cancellations.create"

_LIST_FETCH_CAP = 1000
_LIST_QUERY_PARAMS = {"entry_type", "sort", "limit", "cursor"}
_ENTRY_TYPES = {entry_type.value for entry_type in ManualEntryType}

_LIST_SORT_KEYS: dict[str, Callable[[ManualEntryResponse], str]] = {
    "created_at": lambda item: item.created_at.isoformat(),
    "entry_type": lambda item: item.entry_type,
    "order_number": lambda item: item.order_number,
    "total_amount_toman": lambda item: str(item.total_amount_toman).zfill(20),
}


def _parse_entry_type(value: str | None) -> ManualEntryType | None:
    if value is None:
        return None
    if value not in _ENTRY_TYPES:
        raise ApiError(
            status_code=400,
            code="BAD_REQUEST",
            detail=f"Unknown entry type {value!r}. Known: {', '.join(sorted(_ENTRY_TYPES))}.",
        )
    return ManualEntryType(value)


def _order_command(body: CreateManualOrderRequest) -> CreateManualOrder:
    return CreateManualOrder(
        order_number=body.order_number,
        lines=tuple(
            ManualOrderLine(
                description=line.description,
                quantity=Decimal(line.quantity),
                unit_price_toman=line.unit_price_toman,
                discount_toman=line.discount_toman,
            )
            for line in body.lines
        ),
        store_id=body.store_id,
        customer_name=body.customer_name,
        customer_ref=body.customer_ref,
        discount_toman=body.discount_toman,
        shipping_toman=body.shipping_toman,
        tax_toman=body.tax_toman,
        occurred_at=body.occurred_at,
        note=body.note,
    )


def _refund_command(body: CreateManualRefundRequest) -> CreateManualRefund:
    return CreateManualRefund(
        order_number=body.order_number,
        refund_amount_toman=body.refund_amount_toman,
        refund_reference=body.refund_reference,
        refund_payment=body.refund_payment,
        lines=tuple(
            ManualRefundLine(description=line.description, line_total_toman=line.line_total_toman)
            for line in body.lines
        ),
        reason=body.reason,
        reason_code=body.reason_code,
        store_id=body.store_id,
        occurred_at=body.occurred_at,
        note=body.note,
    )


def _cancellation_command(body: CreateManualCancellationRequest) -> CreateManualCancellation:
    return CreateManualCancellation(
        order_number=body.order_number,
        total_amount_toman=body.total_amount_toman,
        reason=body.reason,
        store_id=body.store_id,
        occurred_at=body.occurred_at,
        note=body.note,
    )


@router.post(
    "/manual-entries/orders",
    response_model=ManualEntryResponse,
    status_code=201,
    summary="Enter an order manually",
)
def create_manual_order(
    body: CreateManualOrderRequest,
    idempotency_key: str | None = Header(
        default=None,
        alias=IDEMPOTENCY_KEY_HEADER,
        description="Unique key for this mutation; retries replay the original response",
    ),
    client: AuthenticatedClient = Depends(require_scopes("integrations:manage")),
    session: Session = Depends(get_db_session),
    service: ManualEntryService = Depends(get_manual_entry_service),
    query: ManualEntryQueryService = Depends(get_manual_entry_query_service),
) -> ManualEntryResponse | JSONResponse:
    """Enter one order manually: the same sales.order.completed event a webhook would send."""
    key = require_idempotency_key(idempotency_key)
    claim_or_replay = begin_mutation(
        session,
        installation_id=client.installation_id,
        operation=_OP_CREATE_ORDER,
        key=key,
        payload=body.model_dump(mode="json"),
    )
    if isinstance(claim_or_replay, Replay):
        return JSONResponse(status_code=claim_or_replay.status_code, content=claim_or_replay.body)
    claim = claim_or_replay
    try:
        entry = service.create_order(
            _order_command(body),
            tenant_id=client.tenant_id,
            installation_id=client.installation_id,
            entered_by=client.key_id,
            entered_by_name=client.client_name,
        )
        detail = query.get_entry(entry.id, client.tenant_id)
    except HesabYarError as exc:
        claim.release()
        session.commit()
        raise api_error_from_hesabyar_error(exc) from exc
    response = ManualEntryResponse.from_detail(detail)
    claim.complete(
        response_status=201,
        response_body=response.model_dump(mode="json"),
        response_reference=f"manual_entries/{entry.id}",
    )
    session.commit()
    return response


@router.post(
    "/manual-entries/refunds",
    response_model=ManualEntryResponse,
    status_code=201,
    summary="Enter a refund manually",
)
def create_manual_refund(
    body: CreateManualRefundRequest,
    idempotency_key: str | None = Header(
        default=None,
        alias=IDEMPOTENCY_KEY_HEADER,
        description="Unique key for this mutation; retries replay the original response",
    ),
    client: AuthenticatedClient = Depends(require_scopes("integrations:manage")),
    session: Session = Depends(get_db_session),
    service: ManualEntryService = Depends(get_manual_entry_service),
    query: ManualEntryQueryService = Depends(get_manual_entry_query_service),
) -> ManualEntryResponse | JSONResponse:
    """Enter one refund manually: sales.refund.created (+ payment.refunded when the money was returned)."""
    key = require_idempotency_key(idempotency_key)
    claim_or_replay = begin_mutation(
        session,
        installation_id=client.installation_id,
        operation=_OP_CREATE_REFUND,
        key=key,
        payload=body.model_dump(mode="json"),
    )
    if isinstance(claim_or_replay, Replay):
        return JSONResponse(status_code=claim_or_replay.status_code, content=claim_or_replay.body)
    claim = claim_or_replay
    try:
        entry = service.create_refund(
            _refund_command(body),
            tenant_id=client.tenant_id,
            installation_id=client.installation_id,
            entered_by=client.key_id,
            entered_by_name=client.client_name,
        )
        detail = query.get_entry(entry.id, client.tenant_id)
    except HesabYarError as exc:
        claim.release()
        session.commit()
        raise api_error_from_hesabyar_error(exc) from exc
    response = ManualEntryResponse.from_detail(detail)
    claim.complete(
        response_status=201,
        response_body=response.model_dump(mode="json"),
        response_reference=f"manual_entries/{entry.id}",
    )
    session.commit()
    return response


@router.post(
    "/manual-entries/cancellations",
    response_model=ManualEntryResponse,
    status_code=201,
    summary="Cancel an order manually",
)
def create_manual_cancellation(
    body: CreateManualCancellationRequest,
    idempotency_key: str | None = Header(
        default=None,
        alias=IDEMPOTENCY_KEY_HEADER,
        description="Unique key for this mutation; retries replay the original response",
    ),
    client: AuthenticatedClient = Depends(require_scopes("integrations:manage")),
    session: Session = Depends(get_db_session),
    service: ManualEntryService = Depends(get_manual_entry_service),
    query: ManualEntryQueryService = Depends(get_manual_entry_query_service),
) -> ManualEntryResponse | JSONResponse:
    """Cancel one order manually: the same sales.order.cancelled event a webhook would send."""
    key = require_idempotency_key(idempotency_key)
    claim_or_replay = begin_mutation(
        session,
        installation_id=client.installation_id,
        operation=_OP_CREATE_CANCELLATION,
        key=key,
        payload=body.model_dump(mode="json"),
    )
    if isinstance(claim_or_replay, Replay):
        return JSONResponse(status_code=claim_or_replay.status_code, content=claim_or_replay.body)
    claim = claim_or_replay
    try:
        entry = service.create_cancellation(
            _cancellation_command(body),
            tenant_id=client.tenant_id,
            installation_id=client.installation_id,
            entered_by=client.key_id,
            entered_by_name=client.client_name,
        )
        detail = query.get_entry(entry.id, client.tenant_id)
    except HesabYarError as exc:
        claim.release()
        session.commit()
        raise api_error_from_hesabyar_error(exc) from exc
    response = ManualEntryResponse.from_detail(detail)
    claim.complete(
        response_status=201,
        response_body=response.model_dump(mode="json"),
        response_reference=f"manual_entries/{entry.id}",
    )
    session.commit()
    return response


@router.get(
    "/manual-entries",
    response_model=PaginatedResponse[ManualEntryResponse],
    summary="List manual entries",
)
def list_manual_entries(
    request: Request,
    entry_type: str | None = Query(default=None, description="Filter by entry type: order/refund/cancellation"),
    sort: str | None = Query(
        default=None,
        description=(
            "Sort field: 'created_at' (default), 'entry_type', 'order_number' or "
            "'total_amount_toman'; prefix '-' for descending"
        ),
    ),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"),
    cursor: str | None = Query(default=None, description="Opaque cursor from a previous page's 'next_cursor'"),
    client: AuthenticatedClient = Depends(require_scopes("integrations:read")),
    service: ManualEntryQueryService = Depends(get_manual_entry_query_service),
) -> PaginatedResponse[ManualEntryResponse]:
    """Return the tenant's manual entries, newest first, with each primary event's outcome."""
    reject_unknown_params(request, _LIST_QUERY_PARAMS)
    parsed_type = _parse_entry_type(entry_type)
    field, reverse = parse_sort_param(sort, _LIST_SORT_KEYS, default="created_at")
    if sort is None:
        reverse = True  # newest first by default
    with translate_errors():
        entries = service.list_entries(
            client.tenant_id,
            entry_type=parsed_type,
            limit=_LIST_FETCH_CAP,
        )
    items = [ManualEntryResponse.from_summary(summary) for summary in entries]
    page, next_cursor = paginate(
        items,
        key_fn=_LIST_SORT_KEYS[field],
        tie_fn=lambda item: item.entry_id,
        limit=limit,
        cursor=cursor,
        reverse=reverse,
    )
    return PaginatedResponse(items=page, next_cursor=next_cursor)


@router.get(
    "/manual-entries/{entry_id}",
    response_model=ManualEntryResponse,
    summary="Get one manual entry",
)
def get_manual_entry(
    entry_id: str = Path(..., description="Manual entry id"),
    client: AuthenticatedClient = Depends(require_scopes("integrations:read")),
    service: ManualEntryQueryService = Depends(get_manual_entry_query_service),
) -> ManualEntryResponse:
    """Return one manual entry with the pipeline trace of every event it produced."""
    with translate_errors():
        detail = service.get_entry(entry_id, client.tenant_id)
    return ManualEntryResponse.from_detail(detail)
