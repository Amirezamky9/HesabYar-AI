"""Chart-of-accounts routes (spec §9.2, US-105) and fiscal-year / period /
setup-status routes (US-203).

Thin routes over the US-104 ``AccountQueryService`` and the US-203
``FiscalQueryService`` / ``SetupStatusService``: no accounting rules here,
no adapter or Tryton calls — the services own the engine port.

Accounts, fiscal years, periods and setup status are company-scoped. The
company comes from the ``company`` query parameter (HesabYar row id or
engine company id), or — when the parameter is absent — from the API key's
company restriction; a key with no restriction and no parameter gets a
typed 400 ``COMPANY_REQUIRED`` (see
:func:`hesabyar.interfaces.rest.dependencies.resolve_accounts_company`).

The port has no single-account / single-year / single-period lookup, so
``GET .../accounts/{id}``, ``GET .../fiscal-years/{id}`` and
``GET .../fiscal-periods/{id}`` resolve the record from the company's
list; an id that is not in that company's list is a typed 404
(``ACCOUNT_NOT_FOUND`` / ``FISCAL_YEAR_NOT_FOUND`` /
``FISCAL_PERIOD_NOT_FOUND``).
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, Query, Request

from hesabyar.application.queries.accounts import AccountQueryService
from hesabyar.application.queries.fiscal import FiscalQueryService, SetupStatusService
from hesabyar.domain.accounting.refs import FiscalPeriodRef, FiscalYearRef
from hesabyar.domain.common.errors import AccountNotFoundError
from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.infrastructure.persistence.postgres.models import EngineCompanyModel
from hesabyar.interfaces.rest.dependencies import (
    company_ref_for_row,
    get_account_query_service,
    get_fiscal_query_service,
    get_setup_status_service,
    require_scopes,
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
from hesabyar.interfaces.rest.schemas.accounting import (
    AccountResponse,
    FiscalPeriodResponse,
    FiscalYearResponse,
    SetupStatusResponse,
)
from hesabyar.interfaces.rest.schemas.pagination import PaginatedResponse

router = APIRouter(tags=["accounting"])

_ACCOUNT_SORT_KEYS: dict[str, Callable[[AccountResponse], str]] = {
    "code": lambda item: item.code,
    "name": lambda item: item.name,
    "type": lambda item: item.type or "",
}
_ACCOUNT_QUERY_PARAMS = {
    "company",
    "code",
    "name",
    "type",
    "active",
    "closed",
    "sort",
    "limit",
    "cursor",
}

_FISCAL_YEAR_SORT_KEYS: dict[str, Callable[[FiscalYearResponse], str]] = {
    "start_date": lambda item: item.start_date.isoformat(),
    "name": lambda item: item.name,
}
_FISCAL_YEAR_QUERY_PARAMS = {"company", "state", "sort", "limit", "cursor"}

_FISCAL_PERIOD_SORT_KEYS: dict[str, Callable[[FiscalPeriodResponse], str]] = {
    "start_date": lambda item: item.start_date.isoformat(),
    "name": lambda item: item.name,
}
_FISCAL_PERIOD_QUERY_PARAMS = {
    "company",
    "fiscal_year",
    "state",
    "type",
    "sort",
    "limit",
    "cursor",
}


@router.get(
    "/accounting/accounts",
    response_model=PaginatedResponse[AccountResponse],
    summary="List a company's chart of accounts",
)
def list_accounts(
    request: Request,
    code: str | None = Query(
        default=None, description="Prefix filter on account code (e.g. '1.1')"
    ),
    name: str | None = Query(
        default=None,
        description="Substring filter on account name (case-insensitive)",
    ),
    type: str | None = Query(
        default=None, description="Exact filter on account type name (e.g. 'Asset')"
    ),
    active: bool | None = Query(default=None, description="Filter on the active flag"),
    closed: bool | None = Query(default=None, description="Filter on the closed flag"),
    sort: str | None = Query(
        default=None,
        description="Sort field: 'code' (default), 'name' or 'type'; prefix '-' for descending",
    ),
    limit: int = Query(
        default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"
    ),
    cursor: str | None = Query(
        default=None, description="Opaque cursor from a previous page's 'next_cursor'"
    ),
    _client: AuthenticatedClient = Depends(require_scopes("account:read")),
    row: EngineCompanyModel = Depends(resolve_accounts_company),
    service: AccountQueryService = Depends(get_account_query_service),
) -> PaginatedResponse[AccountResponse]:
    """Return the company's chart of accounts, cursor-paginated."""
    reject_unknown_params(request, _ACCOUNT_QUERY_PARAMS)
    field, reverse = parse_sort_param(sort, _ACCOUNT_SORT_KEYS, default="code")
    ref = company_ref_for_row(row)
    with translate_errors():
        views = service.get_chart_of_accounts(ref)
    items = [AccountResponse.from_view(view, row.id) for view in views]
    if code is not None:
        items = [item for item in items if item.code.startswith(code)]
    if name is not None:
        needle = name.lower()
        items = [item for item in items if needle in item.name.lower()]
    if type is not None:
        items = [item for item in items if item.type == type]
    if active is not None:
        items = [item for item in items if item.active is active]
    if closed is not None:
        items = [item for item in items if item.closed is closed]
    page, next_cursor = paginate(
        items,
        key_fn=_ACCOUNT_SORT_KEYS[field],
        tie_fn=lambda item: str(item.id),
        limit=limit,
        cursor=cursor,
        reverse=reverse,
    )
    return PaginatedResponse(items=page, next_cursor=next_cursor)


@router.get(
    "/accounting/accounts/{account_id}",
    response_model=AccountResponse,
    summary="Get one account",
)
def get_account(
    account_id: int,
    _client: AuthenticatedClient = Depends(require_scopes("account:read")),
    row: EngineCompanyModel = Depends(resolve_accounts_company),
    service: AccountQueryService = Depends(get_account_query_service),
) -> AccountResponse:
    """Return one account of the company in scope.

    Unknown account → 404 ``ACCOUNT_NOT_FOUND``. The account must be in
    the scoped company's chart — an account of another company looks
    exactly like an account that does not exist.
    """
    ref = company_ref_for_row(row)
    with translate_errors():
        views = service.get_chart_of_accounts(ref)
        for view in views:
            if view.ref.engine_id == account_id:
                return AccountResponse.from_view(view, row.id)
        raise AccountNotFoundError(
            f"Account {account_id} does not exist for this company.",
            details={"account_id": account_id},
        )


# ---------------------------------------------------------------------------
# Fiscal years (US-203)
# ---------------------------------------------------------------------------


@router.get(
    "/accounting/fiscal-years",
    response_model=PaginatedResponse[FiscalYearResponse],
    summary="List a company's fiscal years",
)
def list_fiscal_years(
    request: Request,
    state: str | None = Query(
        default=None,
        description="Exact filter on engine state ('open', 'closed' or 'locked')",
    ),
    sort: str | None = Query(
        default=None,
        description=(
            "Sort field: 'start_date' (default, newest first) or 'name'; "
            "prefix '-' for descending"
        ),
    ),
    limit: int = Query(
        default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"
    ),
    cursor: str | None = Query(
        default=None, description="Opaque cursor from a previous page's 'next_cursor'"
    ),
    _client: AuthenticatedClient = Depends(require_scopes("ledger:read")),
    row: EngineCompanyModel = Depends(resolve_accounts_company),
    service: FiscalQueryService = Depends(get_fiscal_query_service),
) -> PaginatedResponse[FiscalYearResponse]:
    """Return the company's fiscal years, cursor-paginated, newest first."""
    reject_unknown_params(request, _FISCAL_YEAR_QUERY_PARAMS)
    field, reverse = parse_sort_param(sort, _FISCAL_YEAR_SORT_KEYS, default="start_date")
    if sort is None or sort == "":
        # Default order is newest first, matching the port's order; an
        # explicit sort=start_date sorts ascending (oldest first).
        reverse = True
    ref = company_ref_for_row(row)
    with translate_errors():
        views = service.list_fiscal_years(ref)
    items = [FiscalYearResponse.from_view(view, row.id) for view in views]
    if state is not None:
        items = [item for item in items if item.state == state]
    page, next_cursor = paginate(
        items,
        key_fn=_FISCAL_YEAR_SORT_KEYS[field],
        tie_fn=lambda item: str(item.id),
        limit=limit,
        cursor=cursor,
        reverse=reverse,
    )
    return PaginatedResponse(items=page, next_cursor=next_cursor)


@router.get(
    "/accounting/fiscal-years/{fiscal_year_id}",
    response_model=FiscalYearResponse,
    summary="Get one fiscal year",
)
def get_fiscal_year(
    fiscal_year_id: int,
    _client: AuthenticatedClient = Depends(require_scopes("ledger:read")),
    row: EngineCompanyModel = Depends(resolve_accounts_company),
    service: FiscalQueryService = Depends(get_fiscal_query_service),
) -> FiscalYearResponse:
    """Return one fiscal year of the company in scope.

    Unknown fiscal year → 404 ``FISCAL_YEAR_NOT_FOUND``. The year must
    belong to the scoped company — a year of another company looks
    exactly like a year that does not exist.
    """
    ref = company_ref_for_row(row)
    with translate_errors():
        view = service.get_fiscal_year(ref, FiscalYearRef(fiscal_year_id))
    return FiscalYearResponse.from_view(view, row.id)


# ---------------------------------------------------------------------------
# Fiscal periods (US-203)
# ---------------------------------------------------------------------------


@router.get(
    "/accounting/fiscal-periods",
    response_model=PaginatedResponse[FiscalPeriodResponse],
    summary="List a company's fiscal periods",
)
def list_fiscal_periods(
    request: Request,
    fiscal_year: int | None = Query(
        default=None,
        description="Engine id of a fiscal year: only that year's periods are returned",
    ),
    state: str | None = Query(
        default=None,
        description="Exact filter on engine state ('open', 'closed' or 'locked')",
    ),
    type: str | None = Query(
        default=None,
        description="Exact filter on period type ('standard' or 'adjustment')",
    ),
    sort: str | None = Query(
        default=None,
        description=(
            "Sort field: 'start_date' (default, earliest first) or 'name'; "
            "prefix '-' for descending"
        ),
    ),
    limit: int = Query(
        default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"
    ),
    cursor: str | None = Query(
        default=None, description="Opaque cursor from a previous page's 'next_cursor'"
    ),
    _client: AuthenticatedClient = Depends(require_scopes("ledger:read")),
    row: EngineCompanyModel = Depends(resolve_accounts_company),
    service: FiscalQueryService = Depends(get_fiscal_query_service),
) -> PaginatedResponse[FiscalPeriodResponse]:
    """Return the company's fiscal periods, cursor-paginated, earliest first.

    A ``fiscal_year`` that does not belong to the company in scope is a
    404 ``FISCAL_YEAR_NOT_FOUND``.
    """
    reject_unknown_params(request, _FISCAL_PERIOD_QUERY_PARAMS)
    field, reverse = parse_sort_param(sort, _FISCAL_PERIOD_SORT_KEYS, default="start_date")
    ref = company_ref_for_row(row)
    with translate_errors():
        views = service.list_periods(
            ref, FiscalYearRef(fiscal_year) if fiscal_year is not None else None
        )
    items = [FiscalPeriodResponse.from_view(view, row.id) for view in views]
    if state is not None:
        items = [item for item in items if item.state == state]
    if type is not None:
        items = [item for item in items if item.type == type]
    page, next_cursor = paginate(
        items,
        key_fn=_FISCAL_PERIOD_SORT_KEYS[field],
        tie_fn=lambda item: str(item.id),
        limit=limit,
        cursor=cursor,
        reverse=reverse,
    )
    return PaginatedResponse(items=page, next_cursor=next_cursor)


@router.get(
    "/accounting/fiscal-periods/{period_id}",
    response_model=FiscalPeriodResponse,
    summary="Get one fiscal period",
)
def get_fiscal_period(
    period_id: int,
    _client: AuthenticatedClient = Depends(require_scopes("ledger:read")),
    row: EngineCompanyModel = Depends(resolve_accounts_company),
    service: FiscalQueryService = Depends(get_fiscal_query_service),
) -> FiscalPeriodResponse:
    """Return one fiscal period of the company in scope.

    Unknown period → 404 ``FISCAL_PERIOD_NOT_FOUND``. The period must
    belong to the scoped company — a period of another company looks
    exactly like a period that does not exist.
    """
    ref = company_ref_for_row(row)
    with translate_errors():
        view = service.get_period(ref, FiscalPeriodRef(period_id))
    return FiscalPeriodResponse.from_view(view, row.id)


# ---------------------------------------------------------------------------
# Setup status (US-203)
# ---------------------------------------------------------------------------


@router.get(
    "/accounting/setup-status",
    response_model=SetupStatusResponse,
    summary="Get a company's setup state for the dashboard wizard",
)
def get_setup_status(
    _client: AuthenticatedClient = Depends(require_scopes("ledger:read")),
    row: EngineCompanyModel = Depends(resolve_accounts_company),
    service: SetupStatusService = Depends(get_setup_status_service),
) -> SetupStatusResponse:
    """Return read-only setup state: chart installed, fiscal years and open periods.

    Derived purely from engine reads (see
    :class:`hesabyar.application.queries.fiscal.SetupStatusService`); the
    wizard that acts on this state is a later story
    (``docs/setup-wizard.md``).
    """
    ref = company_ref_for_row(row)
    with translate_errors():
        view = service.get_setup_status(ref)
    return SetupStatusResponse.from_view(view, row.id)
