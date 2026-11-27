"""Reminder preference + log routes (US-564, Phase 5-7 ۵خ W3).

Thin routes over the reminder services: no reminder logic here —
the command service owns the policy + the daily job, the query
service owns the reads.

- ``GET /reminders/preferences`` / ``PUT`` — the installation's
  reminder policy (which reminders are on, which channels, lead
  times, quiet hours, eNamad expiry date). The GET returns the
  defaults when never saved (disabled — reminders off).
- ``GET /reminders/log`` — the reminder log (the DLQ + audit
  trail), newest first.

Everything needs ``reminders:manage`` (a new scope — no existing
scope fits both resources: the preferences are installation
settings, the log is the reminder job's output, and neither
``settings:manage`` nor ``jobs:manage`` covers both; the single
scope mirrors ``sms:manage``). Everything is
tenant/installation-scoped: a row of another installation
answers 404 as if it did not exist.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from hesabyar.application.commands.reminders import ReminderService
from hesabyar.application.queries.reminders import ReminderQueryService
from hesabyar.domain.reminders.models import (
    ReminderChannel,
    ReminderLogStatus,
    ReminderType,
)
from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.interfaces.rest.dependencies import (
    get_db_session,
    get_reminder_query_service,
    get_reminder_service,
    require_scopes,
)
from hesabyar.interfaces.rest.errors import ApiError, translate_errors
from hesabyar.interfaces.rest.pagination import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    reject_unknown_params,
)
from hesabyar.interfaces.rest.schemas.reminders import (
    ReminderLogResponse,
    ReminderPreferencesRequest,
    ReminderPreferencesResponse,
)

router = APIRouter(tags=["reminders"])

_LIST_LOG_QUERY_PARAMS = frozenset(
    {"reminder_type", "subject_id", "channel", "status", "limit", "offset"}
)


@router.get(
    "/reminders/preferences",
    response_model=ReminderPreferencesResponse,
    summary="Get the installation's reminder policy",
)
def get_reminder_preferences(
    client: AuthenticatedClient = Depends(require_scopes("reminders:manage")),
    service: ReminderQueryService = Depends(get_reminder_query_service),
) -> ReminderPreferencesResponse:
    """Return the reminder policy (defaults when never saved)."""
    with translate_errors():
        prefs = service.get_preferences(
            tenant_id=client.tenant_id, installation_id=client.installation_id
        )
    return ReminderPreferencesResponse.from_domain(prefs)


@router.put(
    "/reminders/preferences",
    response_model=ReminderPreferencesResponse,
    summary="Replace the installation's reminder policy",
)
def put_reminder_preferences(
    body: ReminderPreferencesRequest,
    client: AuthenticatedClient = Depends(require_scopes("reminders:manage")),
    session: Session = Depends(get_db_session),
    service: ReminderService = Depends(get_reminder_service),
) -> ReminderPreferencesResponse:
    """Insert or replace the reminder policy (only ``enabled`` is required)."""
    with translate_errors():
        prefs = service.save_preferences(
            tenant_id=client.tenant_id,
            installation_id=client.installation_id,
            payload=body.payload_fields(),
        )
    session.commit()
    return ReminderPreferencesResponse.from_domain(prefs)


@router.get(
    "/reminders/log",
    response_model=list[ReminderLogResponse],
    summary="List the reminder log (DLQ + audit trail)",
)
def list_reminder_log(
    request: Request,
    reminder_type: str | None = Query(
        default=None,
        description="Filter by type: cheque_due_soon | installment_overdue | cashbox_uncounted | enmad_expiry",
    ),
    subject_id: str | None = Query(default=None, description="Filter by subject id"),
    channel: str | None = Query(
        default=None, description="Filter by channel: in_app | email | sms"
    ),
    status: str | None = Query(
        default=None,
        description="Filter by status: queued | sent | delivered | failed | skipped",
    ),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"),
    offset: int = Query(default=0, ge=0, description="Page offset"),
    client: AuthenticatedClient = Depends(require_scopes("reminders:manage")),
    service: ReminderQueryService = Depends(get_reminder_query_service),
) -> list[ReminderLogResponse]:
    """Return reminder-log rows, newest first (all filters optional)."""
    reject_unknown_params(request, _LIST_LOG_QUERY_PARAMS)
    parsed_type: ReminderType | None = None
    if reminder_type is not None:
        try:
            parsed_type = ReminderType(reminder_type)
        except ValueError as exc:
            raise ApiError(
                status_code=422,
                code="REMINDER_VALIDATION_FAILED",
                detail=f"Unknown reminder type {reminder_type!r}.",
            ) from exc
    parsed_channel: ReminderChannel | None = None
    if channel is not None:
        try:
            parsed_channel = ReminderChannel(channel)
        except ValueError as exc:
            raise ApiError(
                status_code=422,
                code="REMINDER_VALIDATION_FAILED",
                detail=f"Unknown reminder channel {channel!r}.",
            ) from exc
    parsed_status: ReminderLogStatus | None = None
    if status is not None:
        try:
            parsed_status = ReminderLogStatus(status)
        except ValueError as exc:
            raise ApiError(
                status_code=422,
                code="REMINDER_VALIDATION_FAILED",
                detail=f"Unknown reminder status {status!r}.",
            ) from exc
    with translate_errors():
        entries = service.list_log(
            installation_id=client.installation_id,
            reminder_type=parsed_type,
            subject_id=subject_id,
            channel=parsed_channel,
            status=parsed_status,
            limit=limit,
            offset=offset,
        )
    return [ReminderLogResponse.from_domain(entry) for entry in entries]
