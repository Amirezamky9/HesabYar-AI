"""Company routes (spec §9.2, US-105).

Thin routes over the US-104 ``CompanyQueryService``: no accounting rules
here, no adapter or Tryton calls — the service owns the engine port.

Company identity: ``{company_id}`` accepts the HesabYar
``engine_companies`` row id or the engine's company id (see
:func:`hesabyar.interfaces.rest.dependencies.resolve_company_row`). The
list endpoint only shows companies mapped to the caller's tenant, so a
company with no mapping in this tenant is invisible to it; a
company-restricted key sees only its own company.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, Query, Request

from hesabyar.application.queries.companies import CompanyQueryService
from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.infrastructure.persistence.postgres.models import EngineCompanyModel
from hesabyar.interfaces.rest.dependencies import (
    company_ref_for_row,
    get_company_query_service,
    get_tenant_company_rows,
    require_scopes,
    resolve_company_row,
)
from hesabyar.interfaces.rest.errors import translate_errors
from hesabyar.interfaces.rest.pagination import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    paginate,
    parse_sort_param,
    reject_unknown_params,
)
from hesabyar.interfaces.rest.schemas.companies import CompanyResponse
from hesabyar.interfaces.rest.schemas.pagination import PaginatedResponse

router = APIRouter(tags=["companies"])

_COMPANY_SORT_KEYS: dict[str, Callable[[CompanyResponse], str]] = {
    "name": lambda item: item.name,
    "currency_code": lambda item: item.currency_code,
}
_COMPANY_QUERY_PARAMS = {"name", "currency_code", "sort", "limit", "cursor"}


@router.get(
    "/companies",
    response_model=PaginatedResponse[CompanyResponse],
    summary="List companies visible to this API key",
)
def list_companies(
    request: Request,
    name: str | None = Query(
        default=None,
        description="Substring filter on company name (case-insensitive)",
    ),
    currency_code: str | None = Query(
        default=None, description="Exact filter on currency code (e.g. 'IRR')"
    ),
    sort: str | None = Query(
        default=None,
        description="Sort field: 'name' (default) or 'currency_code'; prefix '-' for descending",
    ),
    limit: int = Query(
        default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"
    ),
    cursor: str | None = Query(
        default=None, description="Opaque cursor from a previous page's 'next_cursor'"
    ),
    _client: AuthenticatedClient = Depends(require_scopes("company:read")),
    rows: list[EngineCompanyModel] = Depends(get_tenant_company_rows),
    service: CompanyQueryService = Depends(get_company_query_service),
) -> PaginatedResponse[CompanyResponse]:
    """Return the tenant's companies, cursor-paginated."""
    reject_unknown_params(request, _COMPANY_QUERY_PARAMS)
    field, reverse = parse_sort_param(sort, _COMPANY_SORT_KEYS, default="name")
    with translate_errors():
        views = service.list_companies()
    # Join engine companies against this tenant's mappings: an engine
    # company with no row in this tenant is invisible to it.
    by_external = {row.external_company_id: row for row in rows}
    items: list[CompanyResponse] = []
    for view in views:
        row = by_external.get(str(view.ref.engine_id))
        if row is None:
            continue
        items.append(CompanyResponse.from_view(view, row.id))
    if name is not None:
        needle = name.lower()
        items = [item for item in items if needle in item.name.lower()]
    if currency_code is not None:
        items = [item for item in items if item.currency_code == currency_code]
    page, next_cursor = paginate(
        items,
        key_fn=_COMPANY_SORT_KEYS[field],
        tie_fn=lambda item: item.id,
        limit=limit,
        cursor=cursor,
        reverse=reverse,
    )
    return PaginatedResponse(items=page, next_cursor=next_cursor)


@router.get(
    "/companies/{company_id}",
    response_model=CompanyResponse,
    summary="Get one company",
)
def get_company(
    company_id: str,
    _client: AuthenticatedClient = Depends(require_scopes("company:read")),
    row: EngineCompanyModel = Depends(resolve_company_row),
    service: CompanyQueryService = Depends(get_company_query_service),
) -> CompanyResponse:
    """Return one company. Unknown company → 404 ``COMPANY_NOT_FOUND``.

    The company must exist both as a mapping row in this tenant (else
    the dependency 404s) and in the engine (else the service 404s).
    """
    ref = company_ref_for_row(row)
    with translate_errors():
        view = service.get_company(ref)
    return CompanyResponse.from_view(view, row.id)
