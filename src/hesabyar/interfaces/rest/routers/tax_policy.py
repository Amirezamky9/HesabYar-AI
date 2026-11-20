"""Tax-policy registry + decision routes (spec §12.2, US-402).

Thin routes over the tax-policy query/decision services: no registry
SQL here, no resolution logic — the services own that.

Reads need ``tax:read``; resolving a decision needs ``tax:decide``. A
decision belongs to the caller's tenant: a decision id of another
tenant answers 404 as if it did not exist.

Tax CALCULATION on invoices is out of scope — these endpoints expose
the registry and the recorded decisions only.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from hesabyar.application.commands.tax_policy import TaxDecisionService
from hesabyar.application.queries.tax_policy import TaxPolicyQueryService
from hesabyar.domain.standards.models import VerificationStatus
from hesabyar.domain.tax_policy.models import UsageContext
from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.interfaces.rest.dependencies import (
    get_db_session,
    get_tax_decision_service,
    get_tax_policy_query_service,
    require_scopes,
)
from hesabyar.interfaces.rest.errors import ApiError, translate_errors
from hesabyar.interfaces.rest.pagination import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    paginate,
    parse_sort_param,
    reject_unknown_params,
)
from hesabyar.interfaces.rest.schemas.pagination import PaginatedResponse
from hesabyar.interfaces.rest.schemas.tax_policy import (
    ResolveTaxDecisionRequest,
    TaxDecisionResponse,
    TaxPolicyVersionDetailResponse,
    TaxPolicyVersionResponse,
    TaxSourceDetailResponse,
    TaxSourceResponse,
)

router = APIRouter(tags=["tax-policy"])

_VERIFICATION_STATUSES = {status.value for status in VerificationStatus}

_SOURCES_SORT_KEYS: dict[str, Callable[[TaxSourceResponse], str]] = {
    "source_key": lambda item: item.source_key,
    "title": lambda item: item.title,
}


def _sources_tie_key(item: TaxSourceResponse) -> str:
    """Tiebreaker for source pagination: source key (total order)."""
    return item.source_key


_SOURCES_QUERY_PARAMS = {"sort", "limit", "cursor"}

_VERSIONS_SORT_KEYS: dict[str, Callable[[TaxPolicyVersionResponse], str]] = {
    "rule_key": lambda item: item.rule_key,
    # ISO dates sort chronologically as strings; empty sorts first.
    "effective_from": lambda item: item.effective_from.isoformat() if item.effective_from else "",
}


def _versions_tie_key(item: TaxPolicyVersionResponse) -> str:
    """Tiebreaker for version pagination: rule key + version id (total order)."""
    return f"{item.rule_key}/{item.version_id}"


_VERSIONS_QUERY_PARAMS = {
    "rule_key",
    "verification_status",
    "effective_on",
    "rule_year",
    "performance_year",
    "filing_year",
    "sort",
    "limit",
    "cursor",
}


def _parse_verification_status(value: str | None) -> VerificationStatus | None:
    """Parse the verification_status filter, failing loudly on unknown labels."""
    if value is None:
        return None
    if value not in _VERIFICATION_STATUSES:
        raise ApiError(
            status_code=400,
            code="INVALID_FILTER",
            detail=(f"Unknown verification_status {value!r}. Allowed: {', '.join(sorted(_VERIFICATION_STATUSES))}."),
        )
    return VerificationStatus(value)


@router.get(
    "/tax-policy/sources",
    response_model=PaginatedResponse[TaxSourceResponse],
    summary="List tax sources in the registry",
)
def list_tax_sources(
    request: Request,
    sort: str | None = Query(
        default=None,
        description="Sort field: 'source_key' (default) or 'title'; prefix '-' for descending",
    ),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"),
    cursor: str | None = Query(default=None, description="Opaque cursor from a previous page's 'next_cursor'"),
    _client: AuthenticatedClient = Depends(require_scopes("tax:read")),
    service: TaxPolicyQueryService = Depends(get_tax_policy_query_service),
) -> PaginatedResponse[TaxSourceResponse]:
    """Return the registry's tax sources, cursor-paginated."""
    reject_unknown_params(request, _SOURCES_QUERY_PARAMS)
    field, reverse = parse_sort_param(sort, _SOURCES_SORT_KEYS, default="source_key")
    with translate_errors():
        sources = service.list_sources()
    items = [TaxSourceResponse.from_source(source) for source in sources]
    page, next_cursor = paginate(
        items,
        key_fn=_SOURCES_SORT_KEYS[field],
        tie_fn=_sources_tie_key,
        limit=limit,
        cursor=cursor,
        reverse=reverse,
    )
    return PaginatedResponse(items=page, next_cursor=next_cursor)


@router.get(
    "/tax-policy/sources/{source_key}",
    response_model=TaxSourceDetailResponse,
    summary="Get one tax source with its policy versions",
)
def get_tax_source(
    source_key: str,
    _client: AuthenticatedClient = Depends(require_scopes("tax:read")),
    service: TaxPolicyQueryService = Depends(get_tax_policy_query_service),
) -> TaxSourceDetailResponse:
    """Return one source. Unknown key → 404 ``TAX_SOURCE_NOT_FOUND``."""
    with translate_errors():
        detail = service.get_source(source_key)
    return TaxSourceDetailResponse.from_detail(detail)


@router.get(
    "/tax-policy/versions",
    response_model=PaginatedResponse[TaxPolicyVersionResponse],
    summary="List tax-policy versions with effective-date filters",
)
def list_tax_policy_versions(
    request: Request,
    rule_key: str | None = Query(default=None, description="Filter to versions of this rule"),
    verification_status: str | None = Query(
        default=None,
        description="Filter to versions carrying this verification label",
    ),
    effective_on: date | None = Query(
        default=None,
        description="Filter to versions whose effective range covers this date (inclusive)",
    ),
    rule_year: int | None = Query(default=None, description="Filter to versions of this Jalali rule year"),
    performance_year: int | None = Query(
        default=None, description="Filter to versions governing this Jalali performance year"
    ),
    filing_year: int | None = Query(
        default=None, description="Filter to versions filed/administered in this Jalali year"
    ),
    sort: str | None = Query(
        default=None,
        description="Sort field: 'rule_key' (default) or 'effective_from'; prefix '-' for descending",
    ),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"),
    cursor: str | None = Query(default=None, description="Opaque cursor from a previous page's 'next_cursor'"),
    _client: AuthenticatedClient = Depends(require_scopes("tax:read")),
    service: TaxPolicyQueryService = Depends(get_tax_policy_query_service),
) -> PaginatedResponse[TaxPolicyVersionResponse]:
    """Return the registry's policy versions, cursor-paginated."""
    reject_unknown_params(request, _VERSIONS_QUERY_PARAMS)
    status = _parse_verification_status(verification_status)
    field, reverse = parse_sort_param(sort, _VERSIONS_SORT_KEYS, default="rule_key")
    with translate_errors():
        versions = service.list_versions(
            rule_key=rule_key,
            verification_status=status,
            effective_on=effective_on,
            rule_year=rule_year,
            performance_year=performance_year,
            filing_year=filing_year,
        )
    items = [TaxPolicyVersionResponse.from_version(version) for version in versions]
    page, next_cursor = paginate(
        items,
        key_fn=_VERSIONS_SORT_KEYS[field],
        tie_fn=_versions_tie_key,
        limit=limit,
        cursor=cursor,
        reverse=reverse,
    )
    return PaginatedResponse(items=page, next_cursor=next_cursor)


@router.get(
    "/tax-policy/versions/{version_id}",
    response_model=TaxPolicyVersionDetailResponse,
    summary="Get one policy version with its source",
)
def get_tax_policy_version(
    version_id: str,
    _client: AuthenticatedClient = Depends(require_scopes("tax:read")),
    service: TaxPolicyQueryService = Depends(get_tax_policy_query_service),
) -> TaxPolicyVersionDetailResponse:
    """Return one version. Unknown id → 404 ``TAX_POLICY_NOT_FOUND``."""
    with translate_errors():
        detail = service.get_version(version_id)
    return TaxPolicyVersionDetailResponse.from_detail(detail)


@router.post(
    "/tax-policy/decisions",
    response_model=TaxDecisionResponse,
    status_code=201,
    summary="Resolve a tax decision for a date/company",
)
def resolve_tax_decision(
    body: ResolveTaxDecisionRequest,
    client: AuthenticatedClient = Depends(require_scopes("tax:decide")),
    session: Session = Depends(get_db_session),
    service: TaxDecisionService = Depends(get_tax_decision_service),
) -> TaxDecisionResponse:
    """Resolve the active policy version and record the decision.

    No active version covers the date (unknown rule, historical or
    future date) → 404 ``TAX_POLICY_NOT_FOUND``. A production
    resolution with a version that is not VERIFIED_TEXT → 422
    ``TAX_POLICY_SOURCE_BLOCKED`` naming the missing official source
    pin; nothing is recorded. A reference resolution records the
    decision with the version's verification status.
    """
    with translate_errors():
        decision = service.resolve_decision(
            tenant_id=client.tenant_id,
            company_id=body.company_id,
            rule_key=body.rule_key,
            decision_date=body.decision_date,
            usage_context=UsageContext(body.usage_context),
        )
    session.commit()
    return TaxDecisionResponse.from_decision(decision)


@router.get(
    "/tax-policy/decisions/{decision_id}",
    response_model=TaxDecisionResponse,
    summary="Get a recorded tax decision",
)
def get_tax_decision(
    decision_id: str,
    client: AuthenticatedClient = Depends(require_scopes("tax:read")),
    service: TaxDecisionService = Depends(get_tax_decision_service),
) -> TaxDecisionResponse:
    """Return one decision. Unknown decision (or another tenant's) → 404 ``TAX_DECISION_NOT_FOUND``."""
    with translate_errors():
        decision = service.get_decision(decision_id, client.tenant_id)
    return TaxDecisionResponse.from_decision(decision)
