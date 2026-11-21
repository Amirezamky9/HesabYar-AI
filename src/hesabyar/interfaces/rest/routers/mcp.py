"""MCP authorized-client management routes (US-606, spec §13.8 + §18.10).

The admin surface for the MCP server's clients: list / create /
rotate / revoke authorized MCP clients, and read the AI access log
(``ai_access_audit``). All endpoints require ``api_keys:manage`` —
managing an MCP client IS managing an API key (the registry row is
the management record over an ordinary ``hya_live_`` key), so no new
scope was minted for it.

- ``GET /mcp/clients`` — list the tenant's authorized MCP clients,
  newest first (``include_revoked=false`` hides revoked rows).
- ``POST /mcp/clients`` — create a client: provisions an API client
  + key and a registry row. The response carries the secret EXACTLY
  ONCE (``secret``); it is never stored or returned again.
- ``GET /mcp/clients/{client_id}`` — one client (no secret).
- ``POST /mcp/clients/{client_id}/rotate`` — revoke the current key
  and issue a replacement with identical scopes/grant/expiry. The
  response carries the new secret exactly once. A revoked client
  cannot rotate (409).
- ``POST /mcp/clients/{client_id}/revoke`` — revoke the client and
  its key immediately (idempotent: a second revoke is a no-op).
- ``GET /mcp/audit`` — the tenant's AI access log, cursor-paginated,
  newest first (filters: client, kind, outcome, tool, company,
  from/to).

Every mutation writes TWO audit records, atomically with the change:
a REST audit event (``audit_events``, action ``mcp.client.*`` — the
record of WHO acted, the acting operator's key) and an
``ai_access_audit`` management row (the record of WHAT changed, the
subject client's identity) so the access log shows a revocation in
the same place as the calls it stops.
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from hesabyar.infrastructure.auth.errors import UnknownScopeError
from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.infrastructure.auth.mcp_clients import (
    McpClientCompanyNotFoundError,
    McpClientNameTakenError,
    McpClientRevokedError,
    McpClientService,
)
from hesabyar.infrastructure.auth.scopes import parse_scopes
from hesabyar.infrastructure.persistence.postgres.audit import (
    AiAccessAuditReader,
    AuditRecorder,
)
from hesabyar.infrastructure.persistence.postgres.models import (
    EngineCompanyModel,
    McpAuthorizedClientModel,
)
from hesabyar.interfaces.mcp.audit import AiAccessAuditWriter
from hesabyar.interfaces.rest.dependencies import (
    get_db_session,
    get_mcp_client_service,
    require_scopes,
)
from hesabyar.interfaces.rest.errors import ApiError
from hesabyar.interfaces.rest.middleware import get_current_request_id
from hesabyar.interfaces.rest.pagination import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    decode_cursor,
    encode_cursor,
    parse_sort_param,
    reject_unknown_params,
)
from hesabyar.interfaces.rest.schemas.mcp import (
    McpAuditRowResponse,
    McpClientCreateRequest,
    McpClientResponse,
    McpClientRevokeRequest,
    McpClientWithSecretResponse,
)
from hesabyar.interfaces.rest.schemas.pagination import PaginatedResponse

router = APIRouter(tags=["mcp"])

#: Managing MCP clients is managing API keys — the existing scope.
require_mcp_manage = require_scopes("api_keys:manage")

_MCP_AUDIT_LIST_PARAMS = {
    "client",
    "kind",
    "outcome",
    "tool",
    "company",
    "from",
    "to",
    "sort",
    "limit",
    "cursor",
}
_MCP_AUDIT_SORT_KEYS = {"created_at"}
_MCP_AUDIT_KINDS = {"tool", "resource", "prompt", "management"}
_MCP_AUDIT_OUTCOMES = {"allowed", "denied", "error"}


# -- helpers ---------------------------------------------------------------


def _company_names(
    session: Session, tenant_id: str, company_ids: set[str]
) -> dict[str, str | None]:
    """Map HesabYar company row ids to display names (one query)."""
    if not company_ids:
        return {}
    stmt = select(EngineCompanyModel).where(
        EngineCompanyModel.tenant_id == tenant_id,
        EngineCompanyModel.id.in_(company_ids),
    )
    return {row.id: row.company_name for row in session.execute(stmt).scalars()}


def _company_external_id(session: Session, tenant_id: str, row_id: str | None) -> str | None:
    """Engine company id for a registry row's grant (for the audit event)."""
    if row_id is None:
        return None
    row = session.get(EngineCompanyModel, row_id)
    if row is None or row.tenant_id != tenant_id:
        return None
    return row.external_company_id


def _subject_identity(record: McpAuthorizedClientModel) -> AuthenticatedClient:
    """The management row's identity: the client acted upon (the subject).

    A lifecycle row names the MCP client whose access changed, not the
    operator who changed it — the operator is recorded in the REST
    audit event (``audit_events``) written alongside.
    """
    key = record.key
    return AuthenticatedClient(
        client_id=record.client_id,
        tenant_id=record.tenant_id,
        installation_id=key.installation_id,
        key_id=key.id,
        key_prefix=key.key_prefix,
        client_name=record.name,
        scopes=parse_scopes(record.scopes),
        engine_company_id=record.engine_company_id,
    )


def _record_lifecycle(
    session: Session,
    client: AuthenticatedClient,
    record: McpAuthorizedClientModel,
    *,
    action: str,
) -> None:
    """Write the two audit records for a client lifecycle mutation.

    Both flush into the request transaction (the route commits):
    the REST audit event (who acted) and the ``ai_access_audit``
    management row (what changed). ``action`` is one of
    ``mcp.client.create`` / ``mcp.client.rotate`` /
    ``mcp.client.revoke``.
    """
    AuditRecorder(session).record(
        tenant_id=client.tenant_id,
        installation_id=client.installation_id,
        api_client_id=client.client_id,
        action=action,
        resource_type="mcp_client",
        resource_id=record.id,
        company_external_id=_company_external_id(
            session, client.tenant_id, record.engine_company_id
        ),
        outcome="success",
        request_id=get_current_request_id(),
    )
    AiAccessAuditWriter.record_in_session(
        session,
        client=_subject_identity(record),
        tool=action,
        kind="management",
        company_id=record.engine_company_id,
        outcome="allowed",
        request_id=get_current_request_id(),
        transport="rest",
    )


def _require_client(
    session: Session,
    service: McpClientService,
    client: AuthenticatedClient,
    client_id: str,
) -> McpAuthorizedClientModel:
    """Load a registry row of the caller's tenant (404 otherwise)."""
    record = service.get_client(session, tenant_id=client.tenant_id, client_id=client_id)
    if record is None:
        raise ApiError(
            status_code=404,
            code="MCP_CLIENT_NOT_FOUND",
            detail=f"MCP client {client_id!r} does not exist.",
        )
    return record


def _as_utc(value: datetime.datetime) -> datetime.datetime:
    """Return the datetime as UTC (a naive value is read as UTC)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.UTC)
    return value.astimezone(datetime.UTC)


def _decode_position(cursor: str) -> tuple[datetime.datetime, str]:
    """Decode a pagination cursor into its (created_at, id) position."""
    key, tie = decode_cursor(cursor)
    try:
        created_at = datetime.datetime.fromisoformat(key)
    except ValueError as exc:
        raise ApiError(
            status_code=400,
            code="INVALID_CURSOR",
            detail="The 'cursor' parameter is not a valid pagination cursor.",
        ) from exc
    return _as_utc(created_at), tie


# -- clients -----------------------------------------------------------------


@router.get(
    "/mcp/clients",
    response_model=list[McpClientResponse],
    summary="List the tenant's authorized MCP clients",
)
def list_mcp_clients(
    include_revoked: bool = Query(
        default=True, description="Include revoked clients (shown with status 'revoked')"
    ),
    client: AuthenticatedClient = Depends(require_mcp_manage),
    session: Session = Depends(get_db_session),
    service: McpClientService = Depends(get_mcp_client_service),
) -> list[McpClientResponse]:
    """Return the tenant's authorized MCP clients, newest first."""
    records = service.list_clients(
        session, tenant_id=client.tenant_id, include_revoked=include_revoked
    )
    names = _company_names(
        session,
        client.tenant_id,
        {r.engine_company_id for r in records if r.engine_company_id is not None},
    )
    return [
        McpClientResponse.from_record(
            record, company_name=names.get(record.engine_company_id or "")
        )
        for record in records
    ]


@router.post(
    "/mcp/clients",
    response_model=McpClientWithSecretResponse,
    status_code=201,
    summary="Create an authorized MCP client (secret shown once)",
)
def create_mcp_client(
    body: McpClientCreateRequest,
    client: AuthenticatedClient = Depends(require_mcp_manage),
    session: Session = Depends(get_db_session),
    service: McpClientService = Depends(get_mcp_client_service),
) -> McpClientWithSecretResponse:
    """Create a client: API client + key + registry row; secret once.

    Unknown scope → 400 ``UNKNOWN_SCOPE``; scopes without
    ``mcp:connect`` → 400 ``INVALID_SCOPES``; unknown company →
    404 ``COMPANY_NOT_FOUND``; duplicate name → 409
    ``MCP_CLIENT_NAME_TAKEN``.
    """
    try:
        issued = service.create_client(
            session,
            tenant_id=client.tenant_id,
            name=body.name,
            description=body.description,
            scopes=body.scopes,
            installation_id=client.installation_id,
            company_id=body.company_id,
            expires_at=body.expires_at,
        )
    except UnknownScopeError as exc:
        raise ApiError(
            status_code=400,
            code="UNKNOWN_SCOPE",
            detail=f"Unknown scope: {exc.scope!r}.",
        ) from None
    except McpClientCompanyNotFoundError:
        raise ApiError(
            status_code=404,
            code="COMPANY_NOT_FOUND",
            detail=f"Company {body.company_id!r} does not exist.",
        ) from None
    except McpClientNameTakenError:
        raise ApiError(
            status_code=409,
            code="MCP_CLIENT_NAME_TAKEN",
            detail=f"An MCP client named {body.name!r} already exists.",
        ) from None
    except ValueError as exc:
        raise ApiError(
            status_code=400, code="INVALID_SCOPES", detail=str(exc)
        ) from None
    except IntegrityError:
        # A concurrent create with the same name won the race (the
        # service's check-then-insert is not atomic).
        session.rollback()
        raise ApiError(
            status_code=409,
            code="MCP_CLIENT_NAME_TAKEN",
            detail=f"An MCP client named {body.name!r} already exists.",
        ) from None
    record = issued.record
    _record_lifecycle(session, client, record, action="mcp.client.create")
    session.commit()
    names = _company_names(
        session,
        client.tenant_id,
        {record.engine_company_id} if record.engine_company_id is not None else set(),
    )
    response = McpClientResponse.from_record(
        record, company_name=names.get(record.engine_company_id or "")
    )
    return McpClientWithSecretResponse(**response.model_dump(), secret=issued.secret)


@router.get(
    "/mcp/clients/{client_id}",
    response_model=McpClientResponse,
    summary="Get one authorized MCP client",
)
def get_mcp_client(
    client_id: str,
    client: AuthenticatedClient = Depends(require_mcp_manage),
    session: Session = Depends(get_db_session),
    service: McpClientService = Depends(get_mcp_client_service),
) -> McpClientResponse:
    """Return one authorized MCP client of the caller's tenant."""
    record = _require_client(session, service, client, client_id)
    names = _company_names(
        session,
        client.tenant_id,
        {record.engine_company_id} if record.engine_company_id is not None else set(),
    )
    return McpClientResponse.from_record(
        record, company_name=names.get(record.engine_company_id or "")
    )


@router.post(
    "/mcp/clients/{client_id}/rotate",
    response_model=McpClientWithSecretResponse,
    summary="Rotate a client's key (new secret shown once)",
)
def rotate_mcp_client(
    client_id: str,
    client: AuthenticatedClient = Depends(require_mcp_manage),
    session: Session = Depends(get_db_session),
    service: McpClientService = Depends(get_mcp_client_service),
) -> McpClientWithSecretResponse:
    """Rotate a client's key; the old key stops working immediately.

    A revoked client cannot rotate (409 ``MCP_CLIENT_REVOKED``).
    """
    record = _require_client(session, service, client, client_id)
    try:
        issued = service.rotate_client(session, record=record)
    except McpClientRevokedError:
        raise ApiError(
            status_code=409,
            code="MCP_CLIENT_REVOKED",
            detail=f"MCP client {client_id!r} is revoked and cannot rotate.",
        ) from None
    _record_lifecycle(session, client, record, action="mcp.client.rotate")
    session.commit()
    names = _company_names(
        session,
        client.tenant_id,
        {record.engine_company_id} if record.engine_company_id is not None else set(),
    )
    response = McpClientResponse.from_record(
        record, company_name=names.get(record.engine_company_id or "")
    )
    return McpClientWithSecretResponse(**response.model_dump(), secret=issued.secret)


@router.post(
    "/mcp/clients/{client_id}/revoke",
    response_model=McpClientResponse,
    summary="Revoke an authorized MCP client (its key stops working)",
)
def revoke_mcp_client(
    client_id: str,
    body: McpClientRevokeRequest | None = None,
    client: AuthenticatedClient = Depends(require_mcp_manage),
    session: Session = Depends(get_db_session),
    service: McpClientService = Depends(get_mcp_client_service),
) -> McpClientResponse:
    """Revoke a client and its key immediately (idempotent)."""
    record = _require_client(session, service, client, client_id)
    reason = body.reason if body is not None and body.reason else "revoked"
    changed = service.revoke_client(session, record=record, reason=reason)
    if changed:
        _record_lifecycle(session, client, record, action="mcp.client.revoke")
    session.commit()
    names = _company_names(
        session,
        client.tenant_id,
        {record.engine_company_id} if record.engine_company_id is not None else set(),
    )
    return McpClientResponse.from_record(
        record, company_name=names.get(record.engine_company_id or "")
    )


# -- AI access log -------------------------------------------------------------


@router.get(
    "/mcp/audit",
    response_model=PaginatedResponse[McpAuditRowResponse],
    summary="List the tenant's AI access log (MCP calls + lifecycle events)",
)
def list_mcp_audit(
    request: Request,
    client_id: str | None = Query(
        default=None,
        alias="client",
        description="Filter on the API client id (the MCP client's api_clients id)",
    ),
    kind: str | None = Query(
        default=None, description="Filter by kind: 'tool', 'resource', 'prompt' or 'management'"
    ),
    outcome: str | None = Query(
        default=None, description="Filter by outcome: 'allowed', 'denied' or 'error'"
    ),
    tool: str | None = Query(
        default=None,
        description="Filter on the tool name, resource URI, prompt name or lifecycle action",
    ),
    company: str | None = Query(
        default=None, description="Filter on the HesabYar company row id"
    ),
    from_: datetime.datetime | None = Query(
        default=None,
        alias="from",
        description="Inclusive lower bound on created_at (ISO datetime, UTC)",
    ),
    to: datetime.datetime | None = Query(
        default=None,
        alias="to",
        description="Inclusive upper bound on created_at (ISO datetime, UTC)",
    ),
    sort: str | None = Query(
        default=None,
        description=(
            "Sort field: 'created_at' only; default is newest first "
            "('sort=created_at' gives oldest first)"
        ),
    ),
    limit: int = Query(
        default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"
    ),
    cursor: str | None = Query(
        default=None, description="Opaque cursor from a previous page's 'next_cursor'"
    ),
    client: AuthenticatedClient = Depends(require_mcp_manage),
    session: Session = Depends(get_db_session),
) -> PaginatedResponse[McpAuditRowResponse]:
    """Return the tenant's AI access rows, cursor-paginated, newest first."""
    reject_unknown_params(request, _MCP_AUDIT_LIST_PARAMS)
    _field, reverse = parse_sort_param(sort, _MCP_AUDIT_SORT_KEYS, default="created_at")
    # Default order is newest first; an explicit sort=created_at sorts
    # ascending (oldest first) and sort=-created_at is the default.
    newest_first = reverse if sort not in (None, "") else True
    if kind is not None and kind not in _MCP_AUDIT_KINDS:
        raise ApiError(
            status_code=400,
            code="INVALID_FILTER",
            detail=f"The 'kind' filter must be one of: {', '.join(sorted(_MCP_AUDIT_KINDS))}.",
        )
    if outcome is not None and outcome not in _MCP_AUDIT_OUTCOMES:
        raise ApiError(
            status_code=400,
            code="INVALID_FILTER",
            detail=(
                "The 'outcome' filter must be one of: "
                f"{', '.join(sorted(_MCP_AUDIT_OUTCOMES))}."
            ),
        )
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
    after = _decode_position(cursor) if cursor is not None else None
    rows, next_position = AiAccessAuditReader(session).list_events(
        tenant_id=client.tenant_id,
        client_id=client_id,
        kind=kind,
        outcome=outcome,
        tool=tool,
        company_id=company,
        occurred_from=occurred_from,
        occurred_to=occurred_to,
        limit=limit,
        after=after,
        newest_first=newest_first,
    )
    names = _company_names(
        session,
        client.tenant_id,
        {row.company_id for row in rows if row.company_id is not None},
    )
    items = [
        McpAuditRowResponse.from_model(row, company_name=names.get(row.company_id or ""))
        for row in rows
    ]
    next_cursor = (
        encode_cursor(next_position[0].isoformat(), next_position[1])
        if next_position is not None
        else None
    )
    return PaginatedResponse(items=items, next_cursor=next_cursor)
