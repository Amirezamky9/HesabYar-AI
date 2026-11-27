"""Standards registry + validation routes (spec §12.1, US-401).

Thin routes over the standards query/validation services: no DSL
evaluation here, no registry SQL — the services own that.

Reads need ``standards:read``; creating a validation run needs
``standards:validate``. A validation run belongs to the caller's
tenant: a run id of another tenant answers 404 as if it did not exist.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from hesabyar.application.commands.standards import StandardsValidationService
from hesabyar.application.queries.standards import StandardsQueryService
from hesabyar.domain.standards.models import VerificationStatus
from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.interfaces.rest.dependencies import (
    get_db_session,
    get_standards_query_service,
    get_standards_validation_service,
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
from hesabyar.interfaces.rest.schemas.standards import (
    CreateValidationRunRequest,
    StandardDetailResponse,
    StandardListItemResponse,
    ValidationRunResponse,
)

router = APIRouter(tags=["standards"])

_VERIFICATION_STATUSES = {status.value for status in VerificationStatus}

_SORT_KEYS: dict[str, Callable[[StandardListItemResponse], str]] = {
    # Zero-padded so standard 2 sorts before standard 10.
    "standard_code": lambda item: f"{int(item.standard_code):04d}",
    "title": lambda item: item.title,
}


def _tie_key(item: StandardListItemResponse) -> str:
    """Tiebreaker for pagination: zero-padded standard code (total order)."""
    return f"{int(item.standard_code):04d}"


_STANDARDS_QUERY_PARAMS = {
    "verification_status",
    "phase",
    "has_rules",
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
    "/standards",
    response_model=PaginatedResponse[StandardListItemResponse],
    summary="List standards in the registry",
)
def list_standards(
    request: Request,
    verification_status: str | None = Query(
        default=None,
        description="Filter to standards with a revision carrying this verification label",
    ),
    phase: int | None = Query(default=None, description="Filter to standards of this corpus phase (1-8)"),
    has_rules: bool | None = Query(
        default=None,
        description="Filter to standards with (true) or without (false) machine-readable rules",
    ),
    sort: str | None = Query(
        default=None,
        description="Sort field: 'standard_code' (default) or 'title'; prefix '-' for descending",
    ),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"),
    cursor: str | None = Query(default=None, description="Opaque cursor from a previous page's 'next_cursor'"),
    _client: AuthenticatedClient = Depends(require_scopes("standards:read")),
    service: StandardsQueryService = Depends(get_standards_query_service),
) -> PaginatedResponse[StandardListItemResponse]:
    """Return the registry's standards, cursor-paginated."""
    reject_unknown_params(request, _STANDARDS_QUERY_PARAMS)
    status = _parse_verification_status(verification_status)
    field, reverse = parse_sort_param(sort, _SORT_KEYS, default="standard_code")
    with translate_errors():
        sources = service.list_standards(verification_status=status, phase=phase, has_rules=has_rules)
    items = [StandardListItemResponse.from_source(source) for source in sources]
    page, next_cursor = paginate(
        items,
        key_fn=_SORT_KEYS[field],
        tie_fn=_tie_key,
        limit=limit,
        cursor=cursor,
        reverse=reverse,
    )
    return PaginatedResponse(items=page, next_cursor=next_cursor)


@router.get(
    "/standards/{standard_code}",
    response_model=StandardDetailResponse,
    summary="Get one standard with its revisions and rules",
)
def get_standard(
    standard_code: str,
    _client: AuthenticatedClient = Depends(require_scopes("standards:read")),
    service: StandardsQueryService = Depends(get_standards_query_service),
) -> StandardDetailResponse:
    """Return one standard. Unknown code → 404 ``STANDARD_NOT_FOUND``."""
    with translate_errors():
        detail = service.get_standard(standard_code)
    return StandardDetailResponse.from_detail(detail)


@router.post(
    "/validation-runs",
    response_model=ValidationRunResponse,
    status_code=201,
    summary="Create a standards validation run",
)
def create_validation_run(
    body: CreateValidationRunRequest,
    client: AuthenticatedClient = Depends(require_scopes("standards:validate")),
    session: Session = Depends(get_db_session),
    service: StandardsValidationService = Depends(get_standards_validation_service),
) -> ValidationRunResponse:
    """Run the closed DSL engine over the supplied data and persist the run.

    Unknown standard code → 404 ``STANDARD_NOT_FOUND``; a
    BLOCKED_SOURCE_GAP standard for a 1405+ (or unknown) period → 422
    ``STANDARD_SOURCE_BLOCKED``. Missing data makes the affected rules
    ERROR, never PASS.
    """
    with translate_errors():
        record = service.create_run(
            tenant_id=client.tenant_id,
            company_id=body.company_id,
            period_start=body.period_start,
            period_end=body.period_end,
            standard_codes=body.standard_codes,
            data=body.data,
        )
    session.commit()
    return ValidationRunResponse.from_record(record)


@router.get(
    "/validation-runs/{run_id}",
    response_model=ValidationRunResponse,
    summary="Get a validation run with its results",
)
def get_validation_run(
    run_id: str,
    client: AuthenticatedClient = Depends(require_scopes("standards:read")),
    service: StandardsValidationService = Depends(get_standards_validation_service),
) -> ValidationRunResponse:
    """Return one run. Unknown run (or another tenant's run) → 404 ``VALIDATION_RUN_NOT_FOUND``."""
    with translate_errors():
        record = service.get_run(run_id, client.tenant_id)
    return ValidationRunResponse.from_record(record)
