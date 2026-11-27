"""Review-queue routes (US-504, Phase 5 F4; completed US-552).

Thin routes over the review-queue query/command services: no SQL
here, no SLA math here — the services own that.

Reads need ``review:read``; filing an item needs ``review:file``;
performing an action or assigning needs ``review:resolve``; SLA
policy updates need ``settings:manage`` (admin). Items belong to the
caller's tenant: an id of another tenant answers 404 as if it did
not exist.

Default list order is critical first, then oldest (phased design
§7.2); ``sort`` accepts ``priority`` (default), ``created_at``,
``amount`` and ``age`` (prefix ``-`` for descending).

US-552: every item carries the derived suggestion
(``suggested_action_code`` / ``suggested_reason_key``); a
``complete_mapping_retry`` action may carry the completed mapping
(learned as a mapping-center suggestion); a ``replay`` action
really replays the item's source event through the DLQ path.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from hesabyar.application.commands.review_queue import MappingLearningInput, ReviewQueueCommandService
from hesabyar.application.queries.review_queue import ReviewQueueQueryService
from hesabyar.domain.review_queue.models import ReviewItemStatus, ReviewItemType, ReviewSeverity
from hesabyar.domain.review_queue.ports import ReviewItemFilters
from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.interfaces.rest.dependencies import (
    get_db_session,
    get_review_queue_command_service,
    get_review_queue_query_service,
    require_admin,
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
from hesabyar.interfaces.rest.schemas.review_queue import (
    AssignReviewItemRequest,
    CreateReviewItemRequest,
    PerformReviewActionRequest,
    ReviewItemResponse,
    ReviewItemTypeResponse,
    ReviewQueueSummaryResponse,
    SlaPolicyResponse,
    UpdateSlaPolicyRequest,
)

router = APIRouter(tags=["review-queue"])

_ITEM_TYPES = {item_type.value for item_type in ReviewItemType}
_SEVERITIES = {severity.value for severity in ReviewSeverity}
_STATUSES = {status.value for status in ReviewItemStatus}

#: Severity rank for the default (priority) sort: critical 0, warning 1, info 2.
_SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}

_SORT_KEYS: dict[str, Callable[[ReviewItemResponse], str]] = {
    # Zero-padded rank + ISO timestamp: string sort == priority order.
    "priority": lambda item: f"{_SEVERITY_RANK[item.severity]}-{item.created_at.isoformat()}",
    "created_at": lambda item: item.created_at.isoformat(),
    # Null amounts sort as -1 (first ascending, last descending).
    "amount": lambda item: f"{item.amount_irr if item.amount_irr is not None else -1:020d}",
    "age": lambda item: f"{item.age_hours:020.6f}",
}


def _tie_key(item: ReviewItemResponse) -> str:
    """Tiebreaker for pagination: the item id (total order)."""
    return item.id


_ITEMS_QUERY_PARAMS = {
    "type",
    "severity",
    "status",
    "channel",
    "company",
    "assignee",
    "min_amount",
    "max_amount",
    "min_age_hours",
    "max_age_hours",
    "overdue",
    "sort",
    "limit",
    "cursor",
}


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


@router.get(
    "/review-queue/types",
    response_model=list[ReviewItemTypeResponse],
    summary="List the eleven review item types with their permitted actions",
)
def list_review_types(
    _client: AuthenticatedClient = Depends(require_scopes("review:read")),
) -> list[ReviewItemTypeResponse]:
    """Return all eleven item types with their permitted actions."""
    return [ReviewItemTypeResponse.from_type(item_type) for item_type in ReviewItemType]


@router.get(
    "/review-queue/items",
    response_model=PaginatedResponse[ReviewItemResponse],
    summary="List review items (default sort: critical first, then oldest)",
)
def list_review_items(
    request: Request,
    type: str | None = Query(default=None, description="Filter to one of the eleven item types"),
    severity: str | None = Query(default=None, description="Filter to a severity: info | warning | critical"),
    status: str | None = Query(
        default=None, description="Filter to a status: open | in_review | resolved | written_off"
    ),
    channel: str | None = Query(default=None, description="Filter to a payment channel / gateway key"),
    company: str | None = Query(default=None, description="Filter to an API company id"),
    assignee: str | None = Query(default=None, description="Filter to an assignee"),
    min_amount: int | None = Query(default=None, ge=0, description="Minimum amount_irr (IRR)"),
    max_amount: int | None = Query(default=None, ge=0, description="Maximum amount_irr (IRR)"),
    min_age_hours: float | None = Query(default=None, ge=0, description="Minimum age in hours"),
    max_age_hours: float | None = Query(default=None, ge=0, description="Maximum age in hours"),
    overdue: bool | None = Query(default=None, description="When true, only items past their SLA deadline"),
    sort: str | None = Query(
        default=None,
        description="Sort field: 'priority' (default), 'created_at', 'amount' or 'age'; prefix '-' for descending",
    ),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"),
    cursor: str | None = Query(default=None, description="Opaque cursor from a previous page's 'next_cursor'"),
    client: AuthenticatedClient = Depends(require_scopes("review:read")),
    service: ReviewQueueQueryService = Depends(get_review_queue_query_service),
) -> PaginatedResponse[ReviewItemResponse]:
    """Return the tenant's review items, cursor-paginated."""
    reject_unknown_params(request, _ITEMS_QUERY_PARAMS)
    item_type = _parse_enum(type, _ITEM_TYPES, "type")
    severity_value = _parse_enum(severity, _SEVERITIES, "severity")
    status_value = _parse_enum(status, _STATUSES, "status")
    field, reverse = parse_sort_param(sort, _SORT_KEYS, default="priority")
    filters = ReviewItemFilters(
        item_type=ReviewItemType(item_type) if item_type is not None else None,
        severity=ReviewSeverity(severity_value) if severity_value is not None else None,
        status=ReviewItemStatus(status_value) if status_value is not None else None,
        channel=channel,
        company_id=company,
        assignee=assignee,
        min_amount_irr=min_amount,
        max_amount_irr=max_amount,
        min_age_hours=min_age_hours,
        max_age_hours=max_age_hours,
        overdue_only=bool(overdue),
    )
    with translate_errors():
        views = service.list_items(client.tenant_id, filters)
    items = [ReviewItemResponse.from_view(view) for view in views]
    page, next_cursor = paginate(
        items,
        key_fn=_SORT_KEYS[field],
        tie_fn=_tie_key,
        limit=limit,
        cursor=cursor,
        reverse=reverse,
    )
    return PaginatedResponse(items=page, next_cursor=next_cursor)


@router.post(
    "/review-queue/items",
    response_model=ReviewItemResponse,
    status_code=201,
    summary="File a new review item",
)
def create_review_item(
    body: CreateReviewItemRequest,
    client: AuthenticatedClient = Depends(require_scopes("review:file")),
    session: Session = Depends(get_db_session),
    service: ReviewQueueCommandService = Depends(get_review_queue_command_service),
    query: ReviewQueueQueryService = Depends(get_review_queue_query_service),
) -> ReviewItemResponse:
    """File a new exception item (status ``open``).

    Unknown type/severity → 400 ``INVALID_FILTER``. The item is born
    with a ``filed`` audit entry.
    """
    item_type = _parse_enum(body.item_type, _ITEM_TYPES, "item_type")
    severity = _parse_enum(body.severity, _SEVERITIES, "severity")
    assert item_type is not None and severity is not None  # _parse_enum raises on None input only when given
    with translate_errors():
        item = service.file_item(
            tenant_id=client.tenant_id,
            item_type=ReviewItemType(item_type),
            severity=ReviewSeverity(severity),
            reason=body.reason,
            suggested_action=body.suggested_action,
            company_id=body.company_id,
            subtype=body.subtype,
            channel=body.channel,
            source_event_ref=body.source_event_ref,
            amount_irr=body.amount_irr,
            assignee=body.assignee,
            actor=client.client_name,
        )
    session.commit()
    # Re-read through the query path so the response carries computed
    # ageing/SLA fields (the file use case returns the bare item).
    with translate_errors():
        view = query.get_item(item.id, client.tenant_id)
    return ReviewItemResponse.from_view(view, include_audit=True)


@router.get(
    "/review-queue/items/{item_id}",
    response_model=ReviewItemResponse,
    summary="Get one review item with its audit trail",
)
def get_review_item(
    item_id: str,
    client: AuthenticatedClient = Depends(require_scopes("review:read")),
    service: ReviewQueueQueryService = Depends(get_review_queue_query_service),
) -> ReviewItemResponse:
    """Return one item. Unknown id (or another tenant's item) → 404 ``REVIEW_ITEM_NOT_FOUND``."""
    with translate_errors():
        view = service.get_item(item_id, client.tenant_id)
    return ReviewItemResponse.from_view(view, include_audit=True)


@router.post(
    "/review-queue/items/{item_id}/actions",
    response_model=ReviewItemResponse,
    summary="Perform a permitted action on a review item",
)
def perform_review_action(
    item_id: str,
    body: PerformReviewActionRequest,
    client: AuthenticatedClient = Depends(require_scopes("review:resolve")),
    session: Session = Depends(get_db_session),
    service: ReviewQueueCommandService = Depends(get_review_queue_command_service),
    query: ReviewQueueQueryService = Depends(get_review_queue_query_service),
) -> ReviewItemResponse:
    """Perform an action permitted for the item's type (§7.2).

    Unknown item → 404 ``REVIEW_ITEM_NOT_FOUND``; terminal item → 409
    ``REVIEW_INVALID_STATE``; action outside the type's list → 422
    ``REVIEW_ACTION_NOT_PERMITTED``. A ``replay`` on an item without
    a source event → 422 ``REVIEW_REPLAY_NOT_POSSIBLE``; a replay of
    a missing event → 404 ``INBOX_EVENT_NOT_FOUND``; of a
    not-dead-lettered event → 409 ``EVENT_NOT_DEAD_LETTERED``. The
    action is recorded in the item's audit trail.
    """
    learning = (
        MappingLearningInput(
            mapping_kind=body.mapping.mapping_kind,
            source_key=body.mapping.source_key,
            target_type=body.mapping.target_type,
            target_ref=body.mapping.target_ref,
            channel_key=body.mapping.channel_key,
        )
        if body.mapping is not None
        else None
    )
    with translate_errors():
        item = service.perform_action(
            item_id=item_id,
            tenant_id=client.tenant_id,
            action_code=body.action,
            actor=client.client_name,
            note=body.note,
            installation_id=client.installation_id,
            mapping=learning,
        )
    session.commit()
    with translate_errors():
        view = query.get_item(item.id, client.tenant_id)
    return ReviewItemResponse.from_view(view, include_audit=True)


@router.patch(
    "/review-queue/items/{item_id}",
    response_model=ReviewItemResponse,
    summary="Assign (or unassign) a review item",
)
def assign_review_item(
    item_id: str,
    body: AssignReviewItemRequest,
    client: AuthenticatedClient = Depends(require_scopes("review:resolve")),
    session: Session = Depends(get_db_session),
    service: ReviewQueueCommandService = Depends(get_review_queue_command_service),
    query: ReviewQueueQueryService = Depends(get_review_queue_query_service),
) -> ReviewItemResponse:
    """Assign an item to an actor (null unassigns). Unknown item → 404; terminal item → 409."""
    with translate_errors():
        item = service.assign_item(
            item_id=item_id,
            tenant_id=client.tenant_id,
            assignee=body.assignee,
            actor=client.client_name,
        )
    session.commit()
    with translate_errors():
        view = query.get_item(item.id, client.tenant_id)
    return ReviewItemResponse.from_view(view, include_audit=True)


@router.get(
    "/review-queue/summary",
    response_model=ReviewQueueSummaryResponse,
    summary="Per-type counters + ageing + admin notifications for the dashboard",
)
def get_review_queue_summary(
    company: str | None = Query(default=None, description="Filter counters to an API company id"),
    client: AuthenticatedClient = Depends(require_scopes("review:read")),
    service: ReviewQueueQueryService = Depends(get_review_queue_query_service),
) -> ReviewQueueSummaryResponse:
    """Return the queue summary (all eleven types, even with zero items)."""
    with translate_errors():
        summary = service.summary(client.tenant_id, company_id=company)
    return ReviewQueueSummaryResponse.from_summary(summary)


@router.get(
    "/review-queue/sla-policies",
    response_model=list[SlaPolicyResponse],
    summary="Effective SLA policy per item type (configured over default)",
)
def list_sla_policies(
    client: AuthenticatedClient = Depends(require_scopes("review:read")),
    service: ReviewQueueQueryService = Depends(get_review_queue_query_service),
) -> list[SlaPolicyResponse]:
    """Return the effective SLA policy for all eleven types."""
    with translate_errors():
        policies = service.sla_policies(client.tenant_id)
    return [SlaPolicyResponse.from_policy(policy) for policy in policies]


@router.patch(
    "/review-queue/sla-policies/{item_type}",
    response_model=SlaPolicyResponse,
    summary="Set one type's SLA policy (admin)",
)
def update_sla_policy(
    item_type: str,
    body: UpdateSlaPolicyRequest,
    client: AuthenticatedClient = Depends(require_admin),
    session: Session = Depends(get_db_session),
    service: ReviewQueueCommandService = Depends(get_review_queue_command_service),
) -> SlaPolicyResponse:
    """Set a type's SLA policy. Unknown type → 400; invalid hours (0 < warn < sla) → 422."""
    parsed = _parse_enum(item_type, _ITEM_TYPES, "item_type")
    assert parsed is not None  # _parse_enum raises on unknown values
    with translate_errors():
        policy = service.update_sla_policy(
            tenant_id=client.tenant_id,
            item_type=ReviewItemType(parsed),
            sla_hours=body.sla_hours,
            warn_hours=body.warn_hours,
        )
    session.commit()
    return SlaPolicyResponse.from_policy(policy)
