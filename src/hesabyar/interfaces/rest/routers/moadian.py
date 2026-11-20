"""Moadian test profiles + simulator routes (spec §12.3, US-404).

Thin routes over the Moadian query/command services and the
reconciliation worker: no payload validation here, no simulator calls
— the services own those.

Reads need ``moadian:read``; every mutation (including the
reconciliation pass) needs ``moadian:submit``. Everything is
tenant-scoped: a profile, document or submission of another tenant
answers 404 as if it did not exist.

Lifecycle (spec §19 rule 11): documents are created draft-only;
submissions move draft → approved → queued → submitted →
accepted/rejected. Approval is a recorded human step; queueing a
production submission is refused with 501 until the production
connector exists — the simulator is the only submission target in
this phase. Approve, queue and reconcile write audit events
(spec §14.3).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from hesabyar.application.commands.moadian import (
    MoadianDocumentService,
    MoadianProfileService,
    MoadianReconciliationWorker,
    MoadianSubmissionService,
)
from hesabyar.application.queries.moadian import MoadianQueryService
from hesabyar.domain.moadian.models import (
    DocumentStatus,
    MoadianEnvironment,
    SubmissionStatus,
)
from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.infrastructure.persistence.postgres.audit import AuditRecorder
from hesabyar.interfaces.rest.dependencies import (
    get_db_session,
    get_moadian_document_service,
    get_moadian_profile_service,
    get_moadian_query_service,
    get_moadian_reconciliation_worker,
    get_moadian_submission_service,
    require_scopes,
)
from hesabyar.interfaces.rest.errors import ApiError, translate_errors
from hesabyar.interfaces.rest.middleware import get_current_request_id
from hesabyar.interfaces.rest.pagination import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    paginate,
    parse_sort_param,
    reject_unknown_params,
)
from hesabyar.interfaces.rest.schemas.moadian import (
    ApproveMoadianSubmissionRequest,
    CreateMoadianDocumentRequest,
    CreateMoadianProfileRequest,
    CreateMoadianSubmissionRequest,
    MoadianDocumentResponse,
    MoadianProfileResponse,
    MoadianStatusEventResponse,
    MoadianSubmissionResponse,
    ReconciliationReportResponse,
    UpdateMoadianProfileRequest,
)
from hesabyar.interfaces.rest.schemas.pagination import PaginatedResponse

router = APIRouter(tags=["moadian"])

_ENVIRONMENTS = {env.value for env in MoadianEnvironment}
_DOCUMENT_STATUSES = {status.value for status in DocumentStatus}
_SUBMISSION_STATUSES = {status.value for status in SubmissionStatus}


def _record_audit(
    session: Session,
    client: AuthenticatedClient,
    *,
    action: str,
    resource_type: str,
    resource_id: str | None,
    outcome: str,
    error_code: str | None = None,
    payload_hash: str | None = None,
    metadata_after: dict[str, Any] | None = None,
) -> None:
    """Append one audit event for the current request (flushes; caller commits)."""
    AuditRecorder(session).record(
        tenant_id=client.tenant_id,
        installation_id=client.installation_id,
        api_client_id=client.client_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        company_external_id=None,
        outcome=outcome,
        request_id=get_current_request_id(),
        error_code=error_code,
        payload_hash=payload_hash,
        metadata_after=metadata_after,
    )


def _parse_environment(value: str | None) -> MoadianEnvironment | None:
    """Parse the environment filter, failing loudly on unknown values."""
    if value is None:
        return None
    if value not in _ENVIRONMENTS:
        raise ApiError(
            status_code=400,
            code="INVALID_FILTER",
            detail=f"Unknown environment {value!r}. Allowed: {', '.join(sorted(_ENVIRONMENTS))}.",
        )
    return MoadianEnvironment(value)


def _parse_document_status(value: str | None) -> DocumentStatus | None:
    """Parse the document status filter, failing loudly on unknown values."""
    if value is None:
        return None
    if value not in _DOCUMENT_STATUSES:
        raise ApiError(
            status_code=400,
            code="INVALID_FILTER",
            detail=f"Unknown status {value!r}. Allowed: {', '.join(sorted(_DOCUMENT_STATUSES))}.",
        )
    return DocumentStatus(value)


def _parse_submission_status(value: str | None) -> SubmissionStatus | None:
    """Parse the submission status filter, failing loudly on unknown values."""
    if value is None:
        return None
    if value not in _SUBMISSION_STATUSES:
        raise ApiError(
            status_code=400,
            code="INVALID_FILTER",
            detail=f"Unknown status {value!r}. Allowed: {', '.join(sorted(_SUBMISSION_STATUSES))}.",
        )
    return SubmissionStatus(value)


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------

_PROFILES_QUERY_PARAMS = {"environment", "is_active", "sort", "limit", "cursor"}

_PROFILE_SORT_KEYS: dict[str, Callable[[MoadianProfileResponse], str]] = {
    "slug": lambda item: item.slug,
    "name": lambda item: item.name,
    "created_at": lambda item: item.created_at.isoformat(),
}


@router.get(
    "/moadian/profiles",
    response_model=PaginatedResponse[MoadianProfileResponse],
    summary="List Moadian profiles",
)
def list_profiles(
    request: Request,
    environment: str | None = Query(default=None, description="Filter by environment: 'test' or 'production'"),
    is_active: bool | None = Query(default=None, description="Filter by active state"),
    sort: str | None = Query(
        default=None,
        description="Sort field: 'slug' (default), 'name' or 'created_at'; prefix '-' for descending",
    ),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"),
    cursor: str | None = Query(default=None, description="Opaque cursor from a previous page's 'next_cursor'"),
    client: AuthenticatedClient = Depends(require_scopes("moadian:read")),
    service: MoadianQueryService = Depends(get_moadian_query_service),
) -> PaginatedResponse[MoadianProfileResponse]:
    """Return the tenant's Moadian profiles, cursor-paginated."""
    reject_unknown_params(request, _PROFILES_QUERY_PARAMS)
    env = _parse_environment(environment)
    field, reverse = parse_sort_param(sort, _PROFILE_SORT_KEYS, default="slug")
    with translate_errors():
        profiles = service.list_profiles(client.tenant_id, environment=env, is_active=is_active)
    items = [MoadianProfileResponse.from_profile(profile) for profile in profiles]
    page, next_cursor = paginate(
        items,
        key_fn=_PROFILE_SORT_KEYS[field],
        tie_fn=lambda item: item.profile_id,
        limit=limit,
        cursor=cursor,
        reverse=reverse,
    )
    return PaginatedResponse(items=page, next_cursor=next_cursor)


@router.post(
    "/moadian/profiles",
    response_model=MoadianProfileResponse,
    status_code=201,
    summary="Create a Moadian profile",
)
def create_profile(
    body: CreateMoadianProfileRequest,
    client: AuthenticatedClient = Depends(require_scopes("moadian:submit")),
    session: Session = Depends(get_db_session),
    service: MoadianProfileService = Depends(get_moadian_profile_service),
) -> MoadianProfileResponse:
    """Create a versioned protocol profile.

    A duplicate slug in this tenant is 409
    ``MOADIAN_PROFILE_SLUG_CONFLICT``. ``key_reference`` names where
    the signing key lives — key material is refused, never stored.
    """
    with translate_errors():
        profile = service.create_profile(
            tenant_id=client.tenant_id,
            name=body.name,
            slug=body.slug,
            environment=body.environment,
            sender_mode=body.sender_mode,
            fiscal_id=body.fiscal_id,
            economic_code=body.economic_code,
            packet_type=body.packet_type,
            api_base_url=body.api_base_url,
            protocol_version=body.protocol_version,
            source_authority=body.source_authority,
            source_url=body.source_url,
            snapshot_uri=body.snapshot_uri,
            verification_status=body.verification_status,
            key_reference=body.key_reference,
            key_fingerprint=body.key_fingerprint,
            notes=body.notes,
        )
    _record_audit(
        session,
        client,
        action="moadian.profile.create",
        resource_type="moadian_profile",
        resource_id=profile.id,
        outcome="success",
        metadata_after={"slug": profile.slug, "environment": profile.environment.value},
    )
    session.commit()
    return MoadianProfileResponse.from_profile(profile)


@router.get(
    "/moadian/profiles/{profile_id}",
    response_model=MoadianProfileResponse,
    summary="Get one Moadian profile",
)
def get_profile(
    profile_id: str,
    client: AuthenticatedClient = Depends(require_scopes("moadian:read")),
    service: MoadianQueryService = Depends(get_moadian_query_service),
) -> MoadianProfileResponse:
    """Return one profile. Unknown profile (or another tenant's) → 404 ``MOADIAN_PROFILE_NOT_FOUND``."""
    with translate_errors():
        profile = service.get_profile(profile_id, client.tenant_id)
    return MoadianProfileResponse.from_profile(profile)


@router.patch(
    "/moadian/profiles/{profile_id}",
    response_model=MoadianProfileResponse,
    summary="Update a Moadian profile",
)
def update_profile(
    profile_id: str,
    body: UpdateMoadianProfileRequest,
    client: AuthenticatedClient = Depends(require_scopes("moadian:submit")),
    session: Session = Depends(get_db_session),
    service: MoadianProfileService = Depends(get_moadian_profile_service),
) -> MoadianProfileResponse:
    """Update a profile's mutable fields (name, active state, fiscal id, key reference, notes).

    Only the fields present in the body change. Environment, sender
    mode, packet type and protocol version are immutable — changing
    them means creating a new profile.
    """
    with translate_errors():
        profile = service.update_profile(
            profile_id,
            client.tenant_id,
            name=body.name,
            is_active=body.is_active,
            fiscal_id=body.fiscal_id,
            economic_code=body.economic_code,
            key_reference=body.key_reference,
            key_fingerprint=body.key_fingerprint,
            notes=body.notes,
        )
    session.commit()
    return MoadianProfileResponse.from_profile(profile)


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

_DOCUMENTS_QUERY_PARAMS = {"profile_id", "status", "sort", "limit", "cursor"}

_DOCUMENT_SORT_KEYS: dict[str, Callable[[MoadianDocumentResponse], str]] = {
    "created_at": lambda item: item.created_at.isoformat(),
    "taxid": lambda item: item.taxid or "",
}


@router.get(
    "/moadian/documents",
    response_model=PaginatedResponse[MoadianDocumentResponse],
    summary="List Moadian documents",
)
def list_documents(
    request: Request,
    profile_id: str | None = Query(default=None, description="Filter by owning profile id"),
    status: str | None = Query(
        default=None,
        description="Filter by status: 'draft', 'submitted', 'accepted', 'rejected'",
    ),
    sort: str | None = Query(
        default=None,
        description="Sort field: 'created_at' (default) or 'taxid'; prefix '-' for descending",
    ),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"),
    cursor: str | None = Query(default=None, description="Opaque cursor from a previous page's 'next_cursor'"),
    client: AuthenticatedClient = Depends(require_scopes("moadian:read")),
    service: MoadianQueryService = Depends(get_moadian_query_service),
) -> PaginatedResponse[MoadianDocumentResponse]:
    """Return the tenant's Moadian documents, cursor-paginated."""
    reject_unknown_params(request, _DOCUMENTS_QUERY_PARAMS)
    doc_status = _parse_document_status(status)
    field, reverse = parse_sort_param(sort, _DOCUMENT_SORT_KEYS, default="created_at")
    with translate_errors():
        documents = service.list_documents(client.tenant_id, profile_id=profile_id, status=doc_status)
    items = [MoadianDocumentResponse.from_document(document) for document in documents]
    page, next_cursor = paginate(
        items,
        key_fn=_DOCUMENT_SORT_KEYS[field],
        tie_fn=lambda item: item.document_id,
        limit=limit,
        cursor=cursor,
        reverse=reverse,
    )
    return PaginatedResponse(items=page, next_cursor=next_cursor)


@router.post(
    "/moadian/documents",
    response_model=MoadianDocumentResponse,
    status_code=201,
    summary="Create a Moadian document",
)
def create_document(
    body: CreateMoadianDocumentRequest,
    client: AuthenticatedClient = Depends(require_scopes("moadian:submit")),
    session: Session = Depends(get_db_session),
    service: MoadianDocumentService = Depends(get_moadian_document_service),
) -> MoadianDocumentResponse:
    """Create a draft-only document from an invoice payload.

    The payload is validated against instruction Table 7; an invalid
    payload is 422 ``MOADIAN_INVALID_DOCUMENT`` with the problem list.
    A duplicate taxid in this tenant is 409
    ``MOADIAN_DUPLICATE_TAXID``. Draft creation needs no approval.
    """
    with translate_errors():
        document = service.create_document(
            tenant_id=client.tenant_id,
            profile_id=body.profile_id,
            payload=body.payload,
            invoice_id=body.invoice_id,
        )
    _record_audit(
        session,
        client,
        action="moadian.document.create",
        resource_type="moadian_document",
        resource_id=document.id,
        outcome="success",
        payload_hash=document.payload_hash,
        metadata_after={"profile_id": document.profile_id, "taxid": document.taxid},
    )
    session.commit()
    return MoadianDocumentResponse.from_document(document)


@router.get(
    "/moadian/documents/{document_id}",
    response_model=MoadianDocumentResponse,
    summary="Get one Moadian document",
)
def get_document(
    document_id: str,
    client: AuthenticatedClient = Depends(require_scopes("moadian:read")),
    service: MoadianQueryService = Depends(get_moadian_query_service),
) -> MoadianDocumentResponse:
    """Return one document. Unknown document (or another tenant's) → 404 ``MOADIAN_DOCUMENT_NOT_FOUND``."""
    with translate_errors():
        document = service.get_document(document_id, client.tenant_id)
    return MoadianDocumentResponse.from_document(document)


# ---------------------------------------------------------------------------
# Submissions + status events + reconciliation
# ---------------------------------------------------------------------------

_SUBMISSIONS_QUERY_PARAMS = {"document_id", "profile_id", "status", "sort", "limit", "cursor"}

_SUBMISSION_SORT_KEYS: dict[str, Callable[[MoadianSubmissionResponse], str]] = {
    "created_at": lambda item: item.created_at.isoformat(),
    "status": lambda item: item.status,
}

_STATUS_EVENTS_QUERY_PARAMS = {"sort", "limit", "cursor"}

_STATUS_EVENT_SORT_KEYS: dict[str, Callable[[MoadianStatusEventResponse], str]] = {
    "occurred_at": lambda item: item.occurred_at.isoformat(),
}


@router.get(
    "/moadian/submissions",
    response_model=PaginatedResponse[MoadianSubmissionResponse],
    summary="List Moadian submissions",
)
def list_submissions(
    request: Request,
    document_id: str | None = Query(default=None, description="Filter by document id"),
    profile_id: str | None = Query(default=None, description="Filter by profile id"),
    status: str | None = Query(
        default=None,
        description="Filter by status: 'draft', 'approved', 'queued', 'submitted', 'accepted', 'rejected', 'failed'",
    ),
    sort: str | None = Query(
        default=None,
        description="Sort field: 'created_at' (default) or 'status'; prefix '-' for descending",
    ),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"),
    cursor: str | None = Query(default=None, description="Opaque cursor from a previous page's 'next_cursor'"),
    client: AuthenticatedClient = Depends(require_scopes("moadian:read")),
    service: MoadianQueryService = Depends(get_moadian_query_service),
) -> PaginatedResponse[MoadianSubmissionResponse]:
    """Return the tenant's Moadian submissions, cursor-paginated."""
    reject_unknown_params(request, _SUBMISSIONS_QUERY_PARAMS)
    sub_status = _parse_submission_status(status)
    field, reverse = parse_sort_param(sort, _SUBMISSION_SORT_KEYS, default="created_at")
    with translate_errors():
        submissions = service.list_submissions(
            client.tenant_id,
            document_id=document_id,
            profile_id=profile_id,
            status=sub_status,
        )
    items = [MoadianSubmissionResponse.from_submission(submission) for submission in submissions]
    page, next_cursor = paginate(
        items,
        key_fn=_SUBMISSION_SORT_KEYS[field],
        tie_fn=lambda item: item.submission_id,
        limit=limit,
        cursor=cursor,
        reverse=reverse,
    )
    return PaginatedResponse(items=page, next_cursor=next_cursor)


@router.post(
    "/moadian/submissions",
    response_model=MoadianSubmissionResponse,
    status_code=201,
    summary="Create a draft Moadian submission",
)
def create_submission(
    body: CreateMoadianSubmissionRequest,
    client: AuthenticatedClient = Depends(require_scopes("moadian:submit")),
    session: Session = Depends(get_db_session),
    service: MoadianSubmissionService = Depends(get_moadian_submission_service),
) -> MoadianSubmissionResponse:
    """Create a draft submission for a document.

    Draft creation needs no approval — only queueing does. A second
    submission for the same document is a resubmission (``retry``
    true, per the instruction).
    """
    with translate_errors():
        submission = service.create_submission(
            tenant_id=client.tenant_id,
            document_id=body.document_id,
            scenario=body.scenario,
        )
    session.commit()
    return MoadianSubmissionResponse.from_submission(submission)


@router.get(
    "/moadian/submissions/{submission_id}",
    response_model=MoadianSubmissionResponse,
    summary="Get one Moadian submission",
)
def get_submission(
    submission_id: str,
    client: AuthenticatedClient = Depends(require_scopes("moadian:read")),
    service: MoadianQueryService = Depends(get_moadian_query_service),
) -> MoadianSubmissionResponse:
    """Return one submission. Unknown submission (or another tenant's) → 404 ``MOADIAN_SUBMISSION_NOT_FOUND``."""
    with translate_errors():
        submission = service.get_submission(submission_id, client.tenant_id)
    return MoadianSubmissionResponse.from_submission(submission)


@router.post(
    "/moadian/submissions/{submission_id}/approve",
    response_model=MoadianSubmissionResponse,
    summary="Approve a draft Moadian submission",
)
def approve_submission(
    submission_id: str,
    body: ApproveMoadianSubmissionRequest,
    client: AuthenticatedClient = Depends(require_scopes("moadian:submit")),
    session: Session = Depends(get_db_session),
    service: MoadianSubmissionService = Depends(get_moadian_submission_service),
) -> MoadianSubmissionResponse:
    """Record the human approval of a draft submission.

    The approver (API client id), timestamp and note are recorded on
    the submission, and an audit event is written. Approving anything
    but a draft is 409 ``MOADIAN_INVALID_STATE``.
    """
    with translate_errors():
        submission = service.approve_submission(
            submission_id,
            client.tenant_id,
            approved_by=client.client_id,
            approval_note=body.note,
        )
    _record_audit(
        session,
        client,
        action="moadian.submission.approve",
        resource_type="moadian_submission",
        resource_id=submission.id,
        outcome="success",
        metadata_after={
            "document_id": submission.document_id,
            "environment": submission.environment.value,
        },
    )
    session.commit()
    return MoadianSubmissionResponse.from_submission(submission)


@router.post(
    "/moadian/submissions/{submission_id}/queue",
    response_model=MoadianSubmissionResponse,
    summary="Queue an approved Moadian submission",
)
def queue_submission(
    submission_id: str,
    client: AuthenticatedClient = Depends(require_scopes("moadian:submit")),
    session: Session = Depends(get_db_session),
    service: MoadianSubmissionService = Depends(get_moadian_submission_service),
) -> MoadianSubmissionResponse:
    """Queue an approved submission for dispatch via the outbox.

    Only an approved submission can be queued (409
    ``MOADIAN_INVALID_STATE`` otherwise). A production submission is
    refused with 501 ``MOADIAN_PRODUCTION_NOT_CONNECTED`` — the
    simulator is the only submission target in this phase.
    """
    with translate_errors():
        submission = service.queue_submission(submission_id, client.tenant_id)
    _record_audit(
        session,
        client,
        action="moadian.submission.queue",
        resource_type="moadian_submission",
        resource_id=submission.id,
        outcome="success",
        metadata_after={
            "document_id": submission.document_id,
            "uid": submission.uid,
            "outbox_event_id": submission.outbox_event_id,
        },
    )
    session.commit()
    return MoadianSubmissionResponse.from_submission(submission)


@router.get(
    "/moadian/submissions/{submission_id}/status-events",
    response_model=PaginatedResponse[MoadianStatusEventResponse],
    summary="List a submission's status events",
)
def list_status_events(
    submission_id: str,
    request: Request,
    sort: str | None = Query(
        default=None,
        description="Sort field: 'occurred_at' (default); prefix '-' for descending",
    ),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"),
    cursor: str | None = Query(default=None, description="Opaque cursor from a previous page's 'next_cursor'"),
    client: AuthenticatedClient = Depends(require_scopes("moadian:read")),
    service: MoadianQueryService = Depends(get_moadian_query_service),
) -> PaginatedResponse[MoadianStatusEventResponse]:
    """Return a submission's official status events, oldest first.

    Unknown submission (or another tenant's) → 404
    ``MOADIAN_SUBMISSION_NOT_FOUND``.
    """
    reject_unknown_params(request, _STATUS_EVENTS_QUERY_PARAMS)
    field, reverse = parse_sort_param(sort, _STATUS_EVENT_SORT_KEYS, default="occurred_at")
    with translate_errors():
        events = service.list_status_events(submission_id, client.tenant_id)
    items = [MoadianStatusEventResponse.from_event(event) for event in events]
    page, next_cursor = paginate(
        items,
        key_fn=_STATUS_EVENT_SORT_KEYS[field],
        tie_fn=lambda item: item.event_id,
        limit=limit,
        cursor=cursor,
        reverse=reverse,
    )
    return PaginatedResponse(items=page, next_cursor=next_cursor)


@router.post(
    "/moadian/reconcile",
    response_model=ReconciliationReportResponse,
    summary="Run one Moadian reconciliation pass",
)
def reconcile(
    client: AuthenticatedClient = Depends(require_scopes("moadian:submit")),
    session: Session = Depends(get_db_session),
    worker: MoadianReconciliationWorker = Depends(get_moadian_reconciliation_worker),
) -> ReconciliationReportResponse:
    """Dispatch queued submissions to the simulator and poll in-flight ones.

    One pass: pending outbox events are submitted to the deterministic
    simulator, then every in-flight submission is polled and each
    inquiry response is persisted as a status event with its evidence.
    A submission needs two passes to reach a terminal status (first
    poll: PENDING, second: SUCCESS/FAILED) — exactly like the real
    async lifecycle.
    """
    with translate_errors():
        report = worker.run_pass(client.tenant_id)
    _record_audit(
        session,
        client,
        action="moadian.reconcile",
        resource_type="moadian_submission",
        resource_id=None,
        outcome="success",
        metadata_after={
            "dispatched": report.dispatched,
            "submit_failed": report.submit_failed,
            "polled": report.polled,
            "accepted": report.accepted,
            "rejected": report.rejected,
        },
    )
    session.commit()
    return ReconciliationReportResponse.from_report(report)
