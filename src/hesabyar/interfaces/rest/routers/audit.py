"""Audit-trail query routes (spec §14.3, US-205).

Admin-only (``settings:manage`` scope): the audit trail is the
tamper-evidence record of everything the API did, so only admin keys
read it. Queries are always tenant-scoped — a caller only ever sees
its own tenant's events.

Unlike the engine-backed lists, audit events live in HesabYar's own
database, so pagination is true SQL keyset pagination over
``(occurred_at, id)`` (see ``AuditReader``): page boundaries stay
stable while new events arrive, and no page ever loads the whole
table into memory. The cursor encoding is the same opaque format as
the other list endpoints.

Filters (all exact matches, combined with AND):

- ``actor`` — the source API client id (``api_client_id``) of the event;
- ``action`` — the operation, e.g. ``ledger.post``;
- ``company`` — the engine company id (HesabYar row id or engine id,
  like everywhere else); a company-restricted key is forced to its own
  company and gets 403 for any other;
- ``from`` / ``to`` — inclusive UTC bounds on ``occurred_at``
  (ISO datetimes; a naive datetime is read as UTC).

``sort`` supports only ``occurred_at`` (default: newest first;
``sort=-occurred_at`` is the same default, ``sort=occurred_at`` is
oldest first).
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.infrastructure.persistence.postgres.audit import AuditReader
from hesabyar.interfaces.rest.dependencies import (
    get_db_session,
    require_admin,
    resolve_audit_company,
)
from hesabyar.interfaces.rest.errors import ApiError
from hesabyar.interfaces.rest.pagination import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    decode_cursor,
    encode_cursor,
    parse_sort_param,
    reject_unknown_params,
)
from hesabyar.interfaces.rest.schemas.audit import AuditEventResponse
from hesabyar.interfaces.rest.schemas.pagination import PaginatedResponse

router = APIRouter(tags=["audit"])

_AUDIT_LIST_PARAMS = {
    "actor",
    "action",
    "company",
    "from",
    "to",
    "sort",
    "limit",
    "cursor",
}
_AUDIT_SORT_KEYS = {"occurred_at"}


def _as_utc(value: datetime.datetime) -> datetime.datetime:
    """Return the datetime as UTC (a naive value is read as UTC)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.UTC)
    return value.astimezone(datetime.UTC)


def _decode_position(cursor: str) -> tuple[datetime.datetime, str]:
    """Decode a pagination cursor into its (occurred_at, id) position.

    A cursor that is not one this API issued — malformed, or with a
    key that is not an ISO timestamp — is a typed 400
    ``INVALID_CURSOR``.
    """
    key, tie = decode_cursor(cursor)
    try:
        occurred_at = datetime.datetime.fromisoformat(key)
    except ValueError as exc:
        raise ApiError(
            status_code=400,
            code="INVALID_CURSOR",
            detail="The 'cursor' parameter is not a valid pagination cursor.",
        ) from exc
    return _as_utc(occurred_at), tie


@router.get(
    "/audit/events",
    response_model=PaginatedResponse[AuditEventResponse],
    summary="List the tenant's audit events",
)
def list_audit_events(
    request: Request,
    actor: str | None = Query(
        default=None,
        description="Filter on the source API client id (the event's actor)",
    ),
    action: str | None = Query(
        default=None, description="Filter on the operation, e.g. 'ledger.post'"
    ),
    company: str | None = Query(
        default=None,
        description=(
            "Filter on the company (HesabYar row id or engine company id). "
            "A company-restricted key is forced to its own company."
        ),
    ),
    from_: datetime.datetime | None = Query(
        default=None,
        alias="from",
        description="Inclusive lower bound on occurred_at (ISO datetime, UTC)",
    ),
    to: datetime.datetime | None = Query(
        default=None,
        alias="to",
        description="Inclusive upper bound on occurred_at (ISO datetime, UTC)",
    ),
    sort: str | None = Query(
        default=None,
        description=(
            "Sort field: 'occurred_at' only; default is newest first "
            "('sort=occurred_at' gives oldest first)"
        ),
    ),
    limit: int = Query(
        default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"
    ),
    cursor: str | None = Query(
        default=None, description="Opaque cursor from a previous page's 'next_cursor'"
    ),
    client: AuthenticatedClient = Depends(require_admin),
    session: Session = Depends(get_db_session),
) -> PaginatedResponse[AuditEventResponse]:
    """Return the tenant's audit events, cursor-paginated, newest first."""
    reject_unknown_params(request, _AUDIT_LIST_PARAMS)
    _field, reverse = parse_sort_param(sort, _AUDIT_SORT_KEYS, default="occurred_at")
    # Default order is newest first; an explicit sort=occurred_at sorts
    # ascending (oldest first) and sort=-occurred_at is the default.
    newest_first = reverse if sort not in (None, "") else True
    occurred_from = _as_utc(from_) if from_ is not None else None
    occurred_to = _as_utc(to) if to is not None else None
    if (
        occurred_from is not None
        and occurred_to is not None
        and occurred_from > occurred_to
    ):
        raise ApiError(
            status_code=400,
            code="INVALID_FILTER",
            detail="The 'from' bound must not be after the 'to' bound.",
        )
    company_external_id = resolve_audit_company(session, client, company)
    after = _decode_position(cursor) if cursor is not None else None
    events, next_position = AuditReader(session).list_events(
        tenant_id=client.tenant_id,
        api_client_id=actor,
        action=action,
        company_external_id=company_external_id,
        occurred_from=occurred_from,
        occurred_to=occurred_to,
        limit=limit,
        after=after,
        newest_first=newest_first,
    )
    items = [AuditEventResponse.from_model(event) for event in events]
    next_cursor = (
        encode_cursor(next_position[0].isoformat(), next_position[1])
        if next_position is not None
        else None
    )
    return PaginatedResponse(items=items, next_cursor=next_cursor)


@router.get(
    "/audit/events/{event_id}",
    response_model=AuditEventResponse,
    summary="Get one audit event",
)
def get_audit_event(
    event_id: str,
    client: AuthenticatedClient = Depends(require_admin),
    session: Session = Depends(get_db_session),
) -> AuditEventResponse:
    """Return one audit event of the caller's tenant.

    Unknown event (or an event of another tenant, or — for a
    company-restricted key — an event of another company) → 404
    ``AUDIT_EVENT_NOT_FOUND``.
    """
    event = AuditReader(session).get_event(
        tenant_id=client.tenant_id, event_id=event_id
    )
    if event is None:
        raise ApiError(
            status_code=404,
            code="AUDIT_EVENT_NOT_FOUND",
            detail=f"Audit event {event_id!r} does not exist.",
        )
    if client.engine_company_id is not None:
        # A company-restricted key sees only its own company's events;
        # an event with no company is not its company's either.
        company_external_id = resolve_audit_company(session, client, None)
        if event.company_external_id != company_external_id:
            raise ApiError(
                status_code=404,
                code="AUDIT_EVENT_NOT_FOUND",
                detail=f"Audit event {event_id!r} does not exist.",
            )
    return AuditEventResponse.from_model(event)
