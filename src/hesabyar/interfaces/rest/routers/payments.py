"""Payment routes (spec §9.2, US-205).

Thin routes over the US-205 ``PaymentQueryService``: no accounting
rules here, no adapter or Tryton calls — the service owns the engine
port. Payments are read-only through this API: creating and processing
payments is a later story (see ``docs/adapter.md``).

Company scoping: the list endpoint takes its company from the
``company`` query parameter (or the API key's company restriction,
like the accounts endpoints). Get-by-id reads the company from the
engine record itself, then checks the company is mapped to the
caller's tenant: a payment of an unmapped company looks exactly like
a payment that does not exist (404 ``LEDGER_PAYMENT_NOT_FOUND``),
and a key restricted to another company gets 403.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from hesabyar.application.queries.payments import PaymentQueryService
from hesabyar.domain.accounting.refs import PaymentRef
from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.infrastructure.persistence.postgres.models import EngineCompanyModel
from hesabyar.interfaces.rest.dependencies import (
    company_ref_for_row,
    get_db_session,
    get_payment_query_service,
    require_scopes,
    require_visible_company,
    resolve_accounts_company,
)
from hesabyar.interfaces.rest.errors import translate_errors
from hesabyar.interfaces.rest.pagination import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    paginate,
    parse_sort_param,
    reject_unknown_params,
)
from hesabyar.interfaces.rest.schemas.pagination import PaginatedResponse
from hesabyar.interfaces.rest.schemas.payments import PaymentResponse

router = APIRouter(tags=["payments"])

_PAYMENT_LIST_PARAMS = {"company", "kind", "state", "sort", "limit", "cursor"}
_PAYMENT_SORT_KEYS: dict[str, Callable[[PaymentResponse], str]] = {
    "date": lambda item: item.date.isoformat(),
    "id": lambda item: str(item.id),
}


@router.get(
    "/payments",
    response_model=PaginatedResponse[PaymentResponse],
    summary="List a company's payments",
)
def list_payments(
    request: Request,
    kind: str | None = Query(
        default=None,
        description="Filter on payment kind: 'receivable' or 'payable'",
    ),
    state: str | None = Query(
        default=None,
        description=(
            "Filter on engine state: 'draft', 'submitted', 'approved', "
            "'processing', 'succeeded' or 'failed'"
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
    _client: AuthenticatedClient = Depends(require_scopes("payment:read")),
    row: EngineCompanyModel = Depends(resolve_accounts_company),
    service: PaymentQueryService = Depends(get_payment_query_service),
) -> PaginatedResponse[PaymentResponse]:
    """Return the company's payments, cursor-paginated, newest first.

    An invalid ``kind`` / ``state`` filter is a 400
    ``LEDGER_INVALID_PAYMENT`` (the engine port validates filters
    before delegating).
    """
    reject_unknown_params(request, _PAYMENT_LIST_PARAMS)
    field, reverse = parse_sort_param(sort, _PAYMENT_SORT_KEYS, default="date")
    if sort is None or sort == "":
        # Default order is newest first, matching the port's order; an
        # explicit sort=date sorts ascending (oldest first).
        reverse = True
    ref = company_ref_for_row(row)
    with translate_errors():
        views = service.list_payments(ref, kind, state)
    items = [PaymentResponse.from_view(view, row.id) for view in views]
    page, next_cursor = paginate(
        items,
        key_fn=_PAYMENT_SORT_KEYS[field],
        tie_fn=lambda item: str(item.id),
        limit=limit,
        cursor=cursor,
        reverse=reverse,
    )
    return PaginatedResponse(items=page, next_cursor=next_cursor)


@router.get(
    "/payments/{payment_id}",
    response_model=PaymentResponse,
    summary="Get one payment",
)
def get_payment(
    payment_id: int,
    client: AuthenticatedClient = Depends(require_scopes("payment:read")),
    session: Session = Depends(get_db_session),
    service: PaymentQueryService = Depends(get_payment_query_service),
) -> PaymentResponse:
    """Return one payment of a company in scope.

    Unknown payment (or a payment of a company with no mapping in
    this tenant) → 404 ``LEDGER_PAYMENT_NOT_FOUND``.
    """
    with translate_errors():
        view = service.get_payment(PaymentRef(payment_id))
    row = require_visible_company(
        session,
        client,
        view.company,
        payment_id,
        resource_kind="Payment",
        error_code="LEDGER_PAYMENT_NOT_FOUND",
    )
    return PaymentResponse.from_view(view, row.id)
