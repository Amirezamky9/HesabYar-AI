"""Cash-box routes (US-516, Phase 5 wave 2, stream K2).

Thin routes over the cash-box query/command services: no SQL here,
no variance math here — the services own that.

Reads need ``cashbox:read``; counting (start/enter/sign/recount)
needs ``cashbox:count``; registering/updating boxes and changing
settings need ``cashbox:manage``. Posting a variance journal needs
``cashbox:manage`` AND ``ledger:post`` — and the linked
``cashbox_variance`` review item's manager approval (checked by the
service against the item's audit trail). A cash shortage is NEVER
auto-posted.

Boxes and sessions belong to the caller's tenant: an id of another
tenant answers 404 as if it did not exist. The company comes from
the ``company`` query parameter (or the key's company restriction
where a query param exists) or from the request body, and accepts
the HesabYar row id or the engine company id.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from hesabyar.application.commands.cashbox import CashBoxCommandService
from hesabyar.application.queries.cashbox import CashBoxQueryService
from hesabyar.domain.cashbox.models import (
    CashBoxKind,
    CountSessionStatus,
    DenominationCount,
    VariancePolicy,
)
from hesabyar.domain.cashbox.ports import CountSessionFilters
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.infrastructure.persistence.postgres.models import EngineCompanyModel
from hesabyar.interfaces.rest.dependencies import (
    company_ref_for_row,
    get_cashbox_command_service,
    get_cashbox_query_service,
    get_db_session,
    require_company_row_by_id,
    require_scopes,
    resolve_accounts_company,
)
from hesabyar.interfaces.rest.errors import ApiError, translate_errors
from hesabyar.interfaces.rest.pagination import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    paginate,
    parse_sort_param,
    reject_unknown_params,
)
from hesabyar.interfaces.rest.schemas.cashbox import (
    CashBoxReportResponse,
    CashBoxResponse,
    CashBoxSettingsResponse,
    CountSessionResponse,
    CreateCashBoxRequest,
    EnterCountRequest,
    PostVarianceJournalRequest,
    RecountRequest,
    SignSessionRequest,
    StartCountSessionRequest,
    StatementJournalResponse,
    UpdateCashBoxRequest,
    UpdateCashBoxSettingsRequest,
)
from hesabyar.interfaces.rest.schemas.pagination import PaginatedResponse

router = APIRouter(tags=["cash-box"])

_KINDS = {kind.value for kind in CashBoxKind}
_POLICIES = {policy.value for policy in VariancePolicy}
_SESSION_STATUSES = {status.value for status in CountSessionStatus}

_BOX_SORT_KEYS: dict[str, Callable[[CashBoxResponse], str]] = {
    "name": lambda box: box.name_fa,
    "created_at": lambda box: box.created_at.isoformat() if box.created_at is not None else "",
}

_SESSION_SORT_KEYS: dict[str, Callable[[CountSessionResponse], str]] = {
    "created_at": lambda session: session.created_at.isoformat(),
    "variance": lambda session: f"{session.variance_irr if session.variance_irr is not None else 0:020d}",
}


def _tie_key(item: CashBoxResponse | CountSessionResponse) -> str:
    """Tiebreaker for pagination: the item id (total order)."""
    return item.id


def _parse_enum(value: str | None, allowed: set[str], param: str) -> str | None:
    """Parse an enum filter, failing loudly on unknown labels."""
    if value is None:
        return None
    if value not in allowed:
        raise ApiError(
            status_code=400,
            code="INVALID_FILTER",
            detail=f"Unknown {param} {value!r}. Allowed: {', '.join(sorted(allowed))}.",
        )
    return value


# -- settings ---------------------------------------------------------------


@router.get(
    "/cash-boxes/settings",
    response_model=CashBoxSettingsResponse,
    summary="Get the installation's cash-box policy settings",
)
def get_cashbox_settings(
    client: AuthenticatedClient = Depends(require_scopes("cashbox:read")),
    service: CashBoxQueryService = Depends(get_cashbox_query_service),
) -> CashBoxSettingsResponse:
    """Return the cash-box settings (defaults when never set)."""
    with translate_errors():
        settings = service.get_settings(client.tenant_id)
    return CashBoxSettingsResponse.from_settings(settings)


@router.patch(
    "/cash-boxes/settings",
    response_model=CashBoxSettingsResponse,
    summary="Set the installation's cash-box policy settings",
)
def update_cashbox_settings(
    body: UpdateCashBoxSettingsRequest,
    client: AuthenticatedClient = Depends(require_scopes("cashbox:manage")),
    session: Session = Depends(get_db_session),
    service: CashBoxCommandService = Depends(get_cashbox_command_service),
) -> CashBoxSettingsResponse:
    """Set the variance policy and counting thresholds (admin).

    Unknown policy → 400 ``INVALID_FILTER``; invalid thresholds →
    422 ``CASHBOX_SETTINGS_INVALID``.
    """
    policy = _parse_enum(body.variance_policy, _POLICIES, "variance_policy")
    assert policy is not None
    with translate_errors():
        settings = service.update_settings(
            tenant_id=client.tenant_id,
            variance_policy=VariancePolicy(policy),
            material_variance_threshold_irr=body.material_variance_threshold_irr,
            count_reminder_days=body.count_reminder_days,
            count_schedule=body.count_schedule,
            supervisor_required=body.supervisor_required,
            shortage_settlement_hours=body.shortage_settlement_hours,
        )
    session.commit()
    return CashBoxSettingsResponse.from_settings(settings)


# -- statement journals -------------------------------------------------------


_STATEMENT_JOURNALS_QUERY_PARAMS = {"company"}


@router.get(
    "/cash-boxes/statement-journals",
    response_model=list[StatementJournalResponse],
    summary="List a company's Tryton statement journals (daybooks)",
)
def list_statement_journals(
    request: Request,
    row: EngineCompanyModel = Depends(resolve_accounts_company),
    _client: AuthenticatedClient = Depends(require_scopes("cashbox:read")),
    service: CashBoxQueryService = Depends(get_cashbox_query_service),
) -> list[StatementJournalResponse]:
    """Return the company's Statement Journals — each is a daybook a cash box can register on."""
    reject_unknown_params(request, _STATEMENT_JOURNALS_QUERY_PARAMS)
    with translate_errors():
        journals = service.list_statement_journals(company_ref_for_row(row))
    return [StatementJournalResponse.from_view(journal) for journal in journals]


# -- report -------------------------------------------------------------------


_REPORT_QUERY_PARAMS = {"company", "date_from", "date_to", "cash_box_id"}


@router.get(
    "/cash-boxes/report",
    response_model=CashBoxReportResponse,
    summary="Cash-box vs sales variance report (unrecorded sales)",
)
def get_cashbox_report(
    request: Request,
    date_from: date = Query(description="Period start (Gregorian, YYYY-MM-DD)"),
    date_to: date = Query(description="Period end (Gregorian, YYYY-MM-DD)"),
    cash_box_id: str | None = Query(default=None, description="Restrict the report to one cash box"),
    row: EngineCompanyModel = Depends(resolve_accounts_company),
    client: AuthenticatedClient = Depends(require_scopes("cashbox:read")),
    service: CashBoxQueryService = Depends(get_cashbox_query_service),
) -> CashBoxReportResponse:
    """Return the cash-box variance report for a company and period.

    Per box: opening/closing book balances (posted moves only),
    receipts/payments, counting sessions with shortage/overage
    totals, and the recorded-sales total (posted customer invoices)
    with the unrecorded-sales indicator (net overage floored at
    zero). Reversed dates → 422 ``CASHBOX_INVALID``.
    """
    reject_unknown_params(request, _REPORT_QUERY_PARAMS)
    with translate_errors():
        report = service.cashbox_report(
            client.tenant_id,
            company_ref_for_row(row),
            row.id,
            FiscalDate(date_from),
            FiscalDate(date_to),
            cash_box_id=cash_box_id,
        )
    return CashBoxReportResponse.from_report(report)


# -- cash boxes ---------------------------------------------------------------


_BOXES_QUERY_PARAMS = {"company", "sort", "limit", "cursor"}


@router.get(
    "/cash-boxes",
    response_model=PaginatedResponse[CashBoxResponse],
    summary="List a company's cash boxes with live book balances",
)
def list_cash_boxes(
    request: Request,
    sort: str | None = Query(
        default=None,
        description="Sort field: 'name' (default) or 'created_at'; prefix '-' for descending",
    ),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"),
    cursor: str | None = Query(default=None, description="Opaque cursor from a previous page's 'next_cursor'"),
    row: EngineCompanyModel = Depends(resolve_accounts_company),
    client: AuthenticatedClient = Depends(require_scopes("cashbox:read")),
    service: CashBoxQueryService = Depends(get_cashbox_query_service),
) -> PaginatedResponse[CashBoxResponse]:
    """Return the company's cash boxes, cursor-paginated."""
    reject_unknown_params(request, _BOXES_QUERY_PARAMS)
    field, reverse = parse_sort_param(sort, _BOX_SORT_KEYS, default="name")
    with translate_errors():
        views = service.list_cash_boxes(client.tenant_id, company_ref_for_row(row), row.id)
    boxes = [CashBoxResponse.from_view(view) for view in views]
    page, next_cursor = paginate(
        boxes,
        key_fn=_BOX_SORT_KEYS[field],
        tie_fn=_tie_key,
        limit=limit,
        cursor=cursor,
        reverse=reverse,
    )
    return PaginatedResponse(items=page, next_cursor=next_cursor)


@router.post(
    "/cash-boxes",
    response_model=CashBoxResponse,
    status_code=201,
    summary="Register a cash box on a Statement Journal",
)
def create_cash_box(
    body: CreateCashBoxRequest,
    client: AuthenticatedClient = Depends(require_scopes("cashbox:manage")),
    session: Session = Depends(get_db_session),
    service: CashBoxCommandService = Depends(get_cashbox_command_service),
    query: CashBoxQueryService = Depends(get_cashbox_query_service),
) -> CashBoxResponse:
    """Register a cash box.

    The box's cash account is the journal's account (resolved from
    the live engine read — never typed in). Unknown kind → 400
    ``INVALID_FILTER``; unknown journal or bad ceiling → 422
    ``CASHBOX_INVALID``; a journal that already has a box → 409
    ``CASHBOX_JOURNAL_CONFLICT``.
    """
    kind = _parse_enum(body.kind, _KINDS, "kind")
    assert kind is not None
    row = require_company_row_by_id(session, client, body.company_id)
    with translate_errors():
        box = service.register_cash_box(
            tenant_id=client.tenant_id,
            company=company_ref_for_row(row),
            company_row_id=row.id,
            name_fa=body.name_fa,
            kind=CashBoxKind(kind),
            statement_journal_id=body.statement_journal_id,
            ceiling_irr=body.ceiling_irr,
        )
    session.commit()
    # Re-read through the query path so the response carries the live
    # book balance and counting state.
    with translate_errors():
        view = query.get_cash_box(client.tenant_id, box.id)
    return CashBoxResponse.from_view(view)


@router.get(
    "/cash-boxes/{box_id}",
    response_model=CashBoxResponse,
    summary="Get one cash box with its live book balance",
)
def get_cash_box(
    box_id: str,
    client: AuthenticatedClient = Depends(require_scopes("cashbox:read")),
    service: CashBoxQueryService = Depends(get_cashbox_query_service),
) -> CashBoxResponse:
    """Return one cash box. Unknown id (or another tenant's box) → 404 ``CASHBOX_NOT_FOUND``.

    No ``company`` parameter: the box carries its own company.
    """
    with translate_errors():
        view = service.get_cash_box(client.tenant_id, box_id)
    return CashBoxResponse.from_view(view)


@router.patch(
    "/cash-boxes/{box_id}",
    response_model=CashBoxResponse,
    summary="Update a cash box",
)
def update_cash_box(
    box_id: str,
    body: UpdateCashBoxRequest,
    client: AuthenticatedClient = Depends(require_scopes("cashbox:manage")),
    session: Session = Depends(get_db_session),
    service: CashBoxCommandService = Depends(get_cashbox_command_service),
    query: CashBoxQueryService = Depends(get_cashbox_query_service),
) -> CashBoxResponse:
    """Update a cash box's name, kind, ceiling and active flag.

    The statement journal never changes (register a new box for a
    different journal). Unknown kind → 400 ``INVALID_FILTER``; bad
    ceiling → 422 ``CASHBOX_INVALID``.
    """
    kind = _parse_enum(body.kind, _KINDS, "kind")
    assert kind is not None
    with translate_errors():
        box = service.update_cash_box(
            tenant_id=client.tenant_id,
            box_id=box_id,
            name_fa=body.name_fa,
            kind=CashBoxKind(kind),
            ceiling_irr=body.ceiling_irr,
            active=body.active,
        )
    # Resolve the box's company BEFORE committing: a key restricted
    # to another company 403s here and the flushed update rolls back
    # with the session instead of landing. (The row itself is not
    # needed afterwards — the box carries its engine company id.)
    require_company_row_by_id(session, client, box.company_id)
    session.commit()
    # Re-read through the query path so the response carries the live
    # book balance and counting state.
    with translate_errors():
        view = query.get_cash_box(client.tenant_id, box.id)
    return CashBoxResponse.from_view(view)


# -- counting sessions ----------------------------------------------------------


@router.post(
    "/cash-boxes/{box_id}/count-sessions",
    response_model=CountSessionResponse,
    status_code=201,
    summary="Start a supervised counting session on a cash box",
)
def start_count_session(
    box_id: str,
    body: StartCountSessionRequest,
    client: AuthenticatedClient = Depends(require_scopes("cashbox:count")),
    session: Session = Depends(get_db_session),
    service: CashBoxCommandService = Depends(get_cashbox_command_service),
) -> CountSessionResponse:
    """Start a counting session.

    The book balance is snapshotted read-only from the engine at
    start. Supervisor == cashier → 422
    ``COUNT_SUPERVISOR_MUST_DIFFER``; an already-open session on the
    box → 409 ``COUNT_SESSION_ALREADY_OPEN``; an inactive box → 422
    ``CASHBOX_INVALID``.
    """
    row = require_company_row_by_id(session, client, body.company_id)
    with translate_errors():
        count_session = service.start_session(
            tenant_id=client.tenant_id,
            company=company_ref_for_row(row),
            company_row_id=row.id,
            cash_box_id=box_id,
            cashier=body.cashier,
            supervisor=body.supervisor,
        )
    session.commit()
    return CountSessionResponse.from_session(count_session)


_SESSIONS_QUERY_PARAMS = {"company", "cash_box_id", "status", "date_from", "date_to", "sort", "limit", "cursor"}


@router.get(
    "/count-sessions",
    response_model=PaginatedResponse[CountSessionResponse],
    summary="List counting sessions (default sort: newest first)",
)
def list_count_sessions(
    request: Request,
    company: str | None = Query(default=None, description="Filter to an API company id"),
    cash_box_id: str | None = Query(default=None, description="Filter to one cash box"),
    status: str | None = Query(
        default=None,
        description="Filter to a status: in_progress | awaiting_supervisor | awaiting_manager | archived",
    ),
    date_from: date | None = Query(default=None, description="Earliest session start (Gregorian, YYYY-MM-DD)"),
    date_to: date | None = Query(default=None, description="Latest session start (Gregorian, YYYY-MM-DD)"),
    sort: str | None = Query(
        default=None,
        description="Sort field: 'created_at' (default, newest first) or 'variance'; prefix '-' for descending",
    ),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"),
    cursor: str | None = Query(default=None, description="Opaque cursor from a previous page's 'next_cursor'"),
    client: AuthenticatedClient = Depends(require_scopes("cashbox:read")),
    session: Session = Depends(get_db_session),
    service: CashBoxQueryService = Depends(get_cashbox_query_service),
) -> PaginatedResponse[CountSessionResponse]:
    """Return the tenant's counting sessions, cursor-paginated."""
    reject_unknown_params(request, _SESSIONS_QUERY_PARAMS)
    status_value = _parse_enum(status, _SESSION_STATUSES, "status")
    field, reverse = parse_sort_param(sort, _SESSION_SORT_KEYS, default="created_at")
    if sort is None or sort == "":
        # Default is newest first; an explicit sort=created_at is oldest first.
        reverse = True
    company_row_id = None
    if company is not None:
        company_row_id = require_company_row_by_id(session, client, company).id
    filters = CountSessionFilters(
        cash_box_id=cash_box_id,
        company_id=company_row_id,
        status=CountSessionStatus(status_value) if status_value is not None else None,
        date_from=date_from,
        date_to=date_to,
    )
    with translate_errors():
        sessions = service.list_sessions(client.tenant_id, filters)
    responses = [CountSessionResponse.from_session(item) for item in sessions]
    page, next_cursor = paginate(
        responses,
        key_fn=_SESSION_SORT_KEYS[field],
        tie_fn=_tie_key,
        limit=limit,
        cursor=cursor,
        reverse=reverse,
    )
    return PaginatedResponse(items=page, next_cursor=next_cursor)


@router.get(
    "/count-sessions/{session_id}",
    response_model=CountSessionResponse,
    summary="Get one counting session",
)
def get_count_session(
    session_id: str,
    client: AuthenticatedClient = Depends(require_scopes("cashbox:read")),
    service: CashBoxQueryService = Depends(get_cashbox_query_service),
) -> CountSessionResponse:
    """Return one session. Unknown id (or another tenant's session) → 404 ``COUNT_SESSION_NOT_FOUND``."""
    with translate_errors():
        count_session = service.get_session(client.tenant_id, session_id)
    return CountSessionResponse.from_session(count_session)


@router.post(
    "/count-sessions/{session_id}/count",
    response_model=CountSessionResponse,
    summary="Enter (or correct) the counted amount of a session",
)
def enter_count(
    session_id: str,
    body: EnterCountRequest,
    client: AuthenticatedClient = Depends(require_scopes("cashbox:count")),
    session: Session = Depends(get_db_session),
    service: CashBoxCommandService = Depends(get_cashbox_command_service),
) -> CountSessionResponse:
    """Enter the counted amount.

    Only the session's cashier enters the count. A denomination
    breakdown that does not sum to the amount → 422
    ``COUNT_DENOMINATIONS_MISMATCH``; a count after the supervisor
    has attended → 409 ``COUNT_SESSION_INVALID_STATE`` (recount flow
    only). With zero variance the session archives immediately.
    """
    with translate_errors():
        count_session = service.enter_count(
            tenant_id=client.tenant_id,
            session_id=session_id,
            counted_amount_irr=body.counted_amount_irr,
            denominations=tuple(
                DenominationCount(
                    denomination_irr=line.denomination_irr,
                    count=line.count,
                )
                for line in body.denominations
            ),
            actor=body.actor,
        )
    session.commit()
    return CountSessionResponse.from_session(count_session)


@router.post(
    "/count-sessions/{session_id}/sign",
    response_model=CountSessionResponse,
    summary="Record one signature on a counting session",
)
def sign_session(
    session_id: str,
    body: SignSessionRequest,
    client: AuthenticatedClient = Depends(require_scopes("cashbox:count")),
    session: Session = Depends(get_db_session),
    service: CashBoxCommandService = Depends(get_cashbox_command_service),
) -> CountSessionResponse:
    """Record a signature (cashier / supervisor / manager).

    The supervisor's signature records attendance in every session;
    the signed minutes (cashier + supervisor + manager) are required
    only when the variance is non-zero. A wrong actor for the role →
    422 ``COUNT_WRONG_ACTOR``; a missing minutes body on a variance
    session → 422 ``COUNT_MINUTES_REQUIRED``. When the minutes are
    complete, the DRAFT variance journal is created and a
    ``cashbox_variance`` review item is filed.
    """
    with translate_errors():
        count_session = service.sign_session(
            tenant_id=client.tenant_id,
            session_id=session_id,
            role=body.role,
            actor=body.actor,
            minutes_text=body.minutes_text,
        )
    session.commit()
    return CountSessionResponse.from_session(count_session)


@router.post(
    "/count-sessions/{session_id}/post-variance-journal",
    response_model=CountSessionResponse,
    summary="Post a variance session's DRAFT journal after manager approval",
)
def post_variance_journal(
    session_id: str,
    body: PostVarianceJournalRequest,
    client: AuthenticatedClient = Depends(require_scopes("cashbox:manage", "ledger:post")),
    session: Session = Depends(get_db_session),
    service: CashBoxCommandService = Depends(get_cashbox_command_service),
) -> CountSessionResponse:
    """Post the DRAFT variance journal.

    The linked ``cashbox_variance`` review item must be resolved
    with the ``approve_variance_entry`` action (checked in the
    item's audit trail, not just its status) — without that
    approval the post is refused with 409
    ``COUNT_REVIEW_NOT_APPROVED``. After posting, the session is
    archived.
    """
    with translate_errors():
        count_session = service.post_variance_journal(
            tenant_id=client.tenant_id,
            session_id=session_id,
            actor=body.actor,
        )
    session.commit()
    return CountSessionResponse.from_session(count_session)


@router.post(
    "/count-sessions/{session_id}/recount",
    response_model=CountSessionResponse,
    summary="Reopen a variance session for a recount, as the manager requested",
)
def recount_session(
    session_id: str,
    body: RecountRequest,
    client: AuthenticatedClient = Depends(require_scopes("cashbox:count")),
    session: Session = Depends(get_db_session),
    service: CashBoxCommandService = Depends(get_cashbox_command_service),
) -> CountSessionResponse:
    """Reopen a session for a recount.

    The linked review item must be ``in_review`` with a ``recount``
    action in its audit trail — a recount without a manager's
    request is refused with 409 ``COUNT_RECOUNT_NOT_REQUESTED``.
    The superseded DRAFT journal is deleted and the count, minutes
    and signatures are cleared.
    """
    with translate_errors():
        count_session = service.recount(
            tenant_id=client.tenant_id,
            session_id=session_id,
            actor=body.actor,
        )
    session.commit()
    return CountSessionResponse.from_session(count_session)
