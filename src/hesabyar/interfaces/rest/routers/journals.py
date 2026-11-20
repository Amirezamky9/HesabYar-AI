"""Journal draft + posted-journal routes (spec §9.2, US-202).

Thin routes over the US-202 ``JournalDraftService``: no accounting rules
here, no adapter or Tryton calls — the service owns the engine port.
The routes own only HTTP concerns: auth scopes, company resolution,
idempotency, audit recording and response mapping.

Company scoping: draft creation takes its company from the request
body (``company_id`` — HesabYar row id or engine id). Every other
endpoint reads the company from the engine record itself, then checks
the company is mapped to the caller's tenant: a draft of an unmapped
company looks exactly like a draft that does not exist (404
``LEDGER_JOURNAL_NOT_FOUND``), and a key restricted to another company
gets 403.

Idempotency (spec §10.4): ``Idempotency-Key`` is required on create,
post and reverse. Same key + same payload replays the stored response
with no second engine call; same key + different payload is a 409
``IDEMPOTENCY_CONFLICT``. PATCH also requires the key (its contract is
stable even though the operation itself is not implemented yet).

Audit (spec §14.3): every create/post/reverse writes an audit event —
success and failure alike — with actor, operation, company, Tryton
reference, idempotency key, payload hash and UTC timestamp.

PATCH requires an `Idempotency-Key` like the other mutations, but does
not yet claim it: a retried PATCH re-executes the update instead of
replaying a stored response. That is safe by construction — applying
the same date/description/lines twice lands in the same state — but
there is no stored-response replay or audit event for updates yet.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from hesabyar.application.commands.journals import JournalDraftService
from hesabyar.domain.accounting.commands import JournalDraftLine
from hesabyar.domain.accounting.refs import (
    AccountRef,
    CompanyRef,
    JournalDraftRef,
    JournalRef,
    PostedJournalRef,
)
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.errors import HesabYarError
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.infrastructure.persistence.postgres.audit import AuditRecorder
from hesabyar.infrastructure.persistence.postgres.models import EngineCompanyModel
from hesabyar.interfaces.rest.dependencies import (
    company_ref_for_row,
    get_db_session,
    get_journal_draft_service,
    require_company_row_by_id,
    require_draft_company_access,
    require_scopes,
    resolve_accounts_company,
)
from hesabyar.interfaces.rest.errors import (
    ApiError,
    api_error_from_hesabyar_error,
    translate_errors,
)
from hesabyar.interfaces.rest.idempotency import (
    IDEMPOTENCY_KEY_HEADER,
    IdempotencyClaim,
    Replay,
    begin_mutation,
    require_idempotency_key,
)
from hesabyar.interfaces.rest.middleware import get_current_request_id
from hesabyar.interfaces.rest.pagination import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    paginate,
    parse_sort_param,
    reject_unknown_params,
)
from hesabyar.interfaces.rest.schemas.journals import (
    CreateJournalDraftRequest,
    JournalDraftLineRequest,
    JournalDraftResponse,
    PostedJournalResponse,
    ReverseJournalRequest,
    UpdateJournalDraftRequest,
    ValidationResponse,
)
from hesabyar.interfaces.rest.schemas.pagination import PaginatedResponse

router = APIRouter(tags=["accounting"])

# Idempotency operation names (spec §10.4 triple:
# installation_id + operation + key). Audit actions reuse the same names.
_OP_CREATE = "ledger.draft.create"
_OP_POST = "ledger.post"
_OP_REVERSE = "ledger.reverse"

_DRAFT_LIST_PARAMS = {"company", "sort", "limit", "cursor"}
_DRAFT_SORT_KEYS: dict[str, Callable[[JournalDraftResponse], str]] = {
    "date": lambda item: item.date.isoformat(),
    "id": lambda item: str(item.id),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _draft_lines(lines: list[JournalDraftLineRequest]) -> tuple[JournalDraftLine, ...]:
    """Map request lines onto domain lines (invariants enforced at construction)."""
    return tuple(
        JournalDraftLine(
            account=AccountRef(line.account_id),
            debit=MoneyIRR(line.debit),
            credit=MoneyIRR(line.credit),
            description=line.description,
        )
        for line in lines
    )


def _require_visible_company(
    session: Session,
    client: AuthenticatedClient,
    company: CompanyRef,
    resource_id: int,
    *,
    posted: bool = False,
) -> EngineCompanyModel:
    """Resolve a record's company to its tenant mapping row.

    A company with no mapping row in the caller's tenant means the
    record is invisible: 404 as if the record did not exist. A key
    restricted to a different company gets 403 (from
    :func:`require_draft_company_access`).
    """
    row = require_draft_company_access(session, client, company)
    if row is None:
        kind = "Posted journal" if posted else "Journal draft"
        raise ApiError(
            status_code=404,
            code="LEDGER_JOURNAL_NOT_FOUND",
            detail=f"{kind} {resource_id} does not exist in the engine.",
        )
    return row


def _record_audit(
    session: Session,
    client: AuthenticatedClient,
    *,
    action: str,
    resource_type: str,
    resource_id: str | None,
    company_external_id: str | None,
    outcome: str,
    error_code: str | None = None,
    tryton_reference: str | None = None,
    payload_hash: str | None = None,
    idempotency_key: str | None = None,
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
        company_external_id=company_external_id,
        outcome=outcome,
        request_id=get_current_request_id(),
        error_code=error_code,
        tryton_reference=tryton_reference,
        payload_hash=payload_hash,
        idempotency_key=idempotency_key,
        metadata_after=metadata_after,
    )


def _complete_mutation(
    session: Session,
    client: AuthenticatedClient,
    claim: IdempotencyClaim,
    *,
    response_status: int,
    response: JournalDraftResponse | PostedJournalResponse,
    response_reference: str,
    action: str,
    resource_type: str,
    resource_id: str,
    company_external_id: str,
    tryton_reference: str,
    metadata_after: dict[str, Any] | None = None,
) -> None:
    """Store the idempotent response + success audit event, then commit."""
    claim.complete(
        response_status=response_status,
        response_body=response.model_dump(mode="json"),
        response_reference=response_reference,
    )
    _record_audit(
        session,
        client,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        company_external_id=company_external_id,
        outcome="success",
        tryton_reference=tryton_reference,
        payload_hash=claim.payload_hash,
        idempotency_key=claim.record.idempotency_key,
        metadata_after=metadata_after,
    )
    session.commit()


def _fail_mutation(
    session: Session,
    client: AuthenticatedClient,
    claim: IdempotencyClaim,
    exc: HesabYarError,
    *,
    action: str,
    resource_type: str,
    resource_id: str | None,
    company_external_id: str | None,
    tryton_reference: str | None = None,
    metadata_after: dict[str, Any] | None = None,
) -> ApiError:
    """Record a failure audit event, release the claim, commit, translate the error.

    The claim is released so the same key can be retried; the failure
    audit event keeps the stable error code and — for a failed reversal
    post — the surviving ``reversal_draft_id`` for recovery.
    """
    metadata: dict[str, Any] = dict(metadata_after or {})
    reversal_draft_id = exc.details.get("reversal_draft_id")
    if isinstance(reversal_draft_id, int):
        metadata["reversal_draft_id"] = reversal_draft_id
    _record_audit(
        session,
        client,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        company_external_id=company_external_id,
        outcome="error",
        error_code=exc.code,
        tryton_reference=tryton_reference,
        payload_hash=claim.payload_hash,
        idempotency_key=claim.record.idempotency_key,
        metadata_after=metadata or None,
    )
    claim.release()
    session.commit()
    return api_error_from_hesabyar_error(exc)


# ---------------------------------------------------------------------------
# Create / list / get / update
# ---------------------------------------------------------------------------


@router.post(
    "/accounting/journal-drafts",
    response_model=JournalDraftResponse,
    status_code=201,
    summary="Create a journal draft",
)
def create_journal_draft(
    body: CreateJournalDraftRequest,
    idempotency_key: str | None = Header(
        default=None,
        alias=IDEMPOTENCY_KEY_HEADER,
        description="Unique key for this mutation; retries replay the original response",
    ),
    client: AuthenticatedClient = Depends(require_scopes("ledger:draft:create")),
    session: Session = Depends(get_db_session),
    service: JournalDraftService = Depends(get_journal_draft_service),
) -> JournalDraftResponse | JSONResponse:
    """Create a draft journal (an unposted move) in the engine.

    The company comes from the request body. The draft is read back
    after creation, so the response is the engine's own view of it.
    """
    key = require_idempotency_key(idempotency_key)
    row = require_company_row_by_id(session, client, body.company_id)
    ref = company_ref_for_row(row)
    claim_or_replay = begin_mutation(
        session,
        installation_id=client.installation_id,
        operation=_OP_CREATE,
        key=key,
        payload=body.model_dump(mode="json"),
    )
    if isinstance(claim_or_replay, Replay):
        return JSONResponse(
            status_code=claim_or_replay.status_code, content=claim_or_replay.body
        )
    claim = claim_or_replay
    created_ref: JournalDraftRef | None = None
    try:
        draft_ref = service.create_draft(
            ref,
            FiscalDate.from_gregorian(body.date),
            _draft_lines(body.lines),
            description=body.description,
            journal=JournalRef(body.journal_id) if body.journal_id is not None else None,
        )
        created_ref = draft_ref
        view = service.get_draft(draft_ref)
    except HesabYarError as exc:
        raise _fail_mutation(
            session,
            client,
            claim,
            exc,
            action=_OP_CREATE,
            resource_type="journal_draft",
            resource_id=str(created_ref.engine_id) if created_ref is not None else None,
            company_external_id=row.external_company_id,
            tryton_reference=(
                f"account.move:{created_ref.engine_id}"
                if created_ref is not None
                else None
            ),
        ) from exc
    response = JournalDraftResponse.from_view(view, row.id)
    _complete_mutation(
        session,
        client,
        claim,
        response_status=201,
        response=response,
        response_reference=f"accounting/journal-drafts/{view.ref.engine_id}",
        action=_OP_CREATE,
        resource_type="journal_draft",
        resource_id=str(view.ref.engine_id),
        company_external_id=row.external_company_id,
        tryton_reference=f"account.move:{view.ref.engine_id}",
        metadata_after={
            "total_debit": view.total_debit.amount,
            "total_credit": view.total_credit.amount,
            "line_count": len(view.lines),
        },
    )
    return response


@router.get(
    "/accounting/journal-drafts",
    response_model=PaginatedResponse[JournalDraftResponse],
    summary="List a company's journal drafts",
)
def list_journal_drafts(
    request: Request,
    sort: str | None = Query(
        default=None,
        description="Sort field: 'date' (default) or 'id'; prefix '-' for descending",
    ),
    limit: int = Query(
        default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"
    ),
    cursor: str | None = Query(
        default=None, description="Opaque cursor from a previous page's 'next_cursor'"
    ),
    _client: AuthenticatedClient = Depends(require_scopes("ledger:read")),
    row: EngineCompanyModel = Depends(resolve_accounts_company),
    service: JournalDraftService = Depends(get_journal_draft_service),
) -> PaginatedResponse[JournalDraftResponse]:
    """Return the company's draft journals, cursor-paginated.

    Drafts of other companies never appear: the list is scoped to the
    ``company`` query parameter (or the key's company restriction),
    newest first by default.
    """
    reject_unknown_params(request, _DRAFT_LIST_PARAMS)
    field, reverse = parse_sort_param(sort, _DRAFT_SORT_KEYS, default="date")
    ref = company_ref_for_row(row)
    with translate_errors():
        views = service.list_drafts(ref)
    items = [JournalDraftResponse.from_view(view, row.id) for view in views]
    page, next_cursor = paginate(
        items,
        key_fn=_DRAFT_SORT_KEYS[field],
        tie_fn=lambda item: str(item.id),
        limit=limit,
        cursor=cursor,
        reverse=reverse,
    )
    return PaginatedResponse(items=page, next_cursor=next_cursor)


@router.get(
    "/accounting/journal-drafts/{draft_id}",
    response_model=JournalDraftResponse,
    summary="Get one journal draft",
)
def get_journal_draft(
    draft_id: int,
    client: AuthenticatedClient = Depends(require_scopes("ledger:read")),
    session: Session = Depends(get_db_session),
    service: JournalDraftService = Depends(get_journal_draft_service),
) -> JournalDraftResponse:
    """Return one draft journal of a company in scope.

    Unknown draft (or a draft of a company with no mapping in this
    tenant) → 404 ``LEDGER_JOURNAL_NOT_FOUND``. A draft that has since
    been posted reads back with ``state == "posted"``.
    """
    with translate_errors():
        view = service.get_draft(JournalDraftRef(draft_id))
    row = _require_visible_company(session, client, view.company, draft_id)
    return JournalDraftResponse.from_view(view, row.id)


@router.patch(
    "/accounting/journal-drafts/{draft_id}",
    response_model=JournalDraftResponse,
    summary="Update a journal draft",
)
def update_journal_draft(
    draft_id: int,
    body: UpdateJournalDraftRequest,
    idempotency_key: str | None = Header(
        default=None,
        alias=IDEMPOTENCY_KEY_HEADER,
        description="Unique key for this mutation; retries replay the original response",
    ),
    client: AuthenticatedClient = Depends(require_scopes("ledger:draft:update")),
    session: Session = Depends(get_db_session),
    service: JournalDraftService = Depends(get_journal_draft_service),
) -> JournalDraftResponse:
    """Update a draft journal's date, description or lines.

    Only the fields present in the body change; ``lines`` replaces the
    draft's lines wholesale. Updating a posted journal is a 409
    ``LEDGER_POSTED_IMMUTABLE``. Requires an ``Idempotency-Key`` (not
    yet claimed for replay — see the module docstring).
    """
    require_idempotency_key(idempotency_key)
    with translate_errors():
        view = service.get_draft(JournalDraftRef(draft_id))
    row = _require_visible_company(session, client, view.company, draft_id)
    with translate_errors():
        updated = service.update_draft(
            JournalDraftRef(draft_id),
            date=FiscalDate.from_gregorian(body.date) if body.date is not None else None,
            description=body.description,
            lines=_draft_lines(body.lines) if body.lines is not None else None,
        )
    return JournalDraftResponse.from_view(updated, row.id)


# ---------------------------------------------------------------------------
# Validate / post / reverse
# ---------------------------------------------------------------------------


@router.post(
    "/accounting/journal-drafts/{draft_id}/validate",
    response_model=ValidationResponse,
    summary="Validate a journal draft",
)
def validate_journal_draft(
    draft_id: int,
    client: AuthenticatedClient = Depends(require_scopes("ledger:draft:update")),
    session: Session = Depends(get_db_session),
    service: JournalDraftService = Depends(get_journal_draft_service),
) -> ValidationResponse:
    """Validate a draft against the §8.2 invariants (never mutates it)."""
    with translate_errors():
        view = service.get_draft(JournalDraftRef(draft_id))
    _require_visible_company(session, client, view.company, draft_id)
    with translate_errors():
        summary = service.validate_draft(JournalDraftRef(draft_id))
    return ValidationResponse.from_summary(summary)


@router.post(
    "/accounting/journal-drafts/{draft_id}/post",
    response_model=PostedJournalResponse,
    summary="Post a journal draft",
)
def post_journal_draft(
    draft_id: int,
    idempotency_key: str | None = Header(
        default=None,
        alias=IDEMPOTENCY_KEY_HEADER,
        description="Unique key for this mutation; retries replay the original response",
    ),
    client: AuthenticatedClient = Depends(require_scopes("ledger:post")),
    session: Session = Depends(get_db_session),
    service: JournalDraftService = Depends(get_journal_draft_service),
) -> PostedJournalResponse | JSONResponse:
    """Post a draft journal, atomically, in the engine.

    The draft is re-validated first; unbalanced, unknown-account and
    closed-period drafts fail with stable ledger errors before anything
    is posted. Posting an already-posted journal is a 409
    ``LEDGER_POSTED_IMMUTABLE``. The posted journal is read back, so
    the response carries the engine-assigned number and post date.
    """
    with translate_errors():
        draft_view = service.get_draft(JournalDraftRef(draft_id))
    row = _require_visible_company(session, client, draft_view.company, draft_id)
    key = require_idempotency_key(idempotency_key)
    claim_or_replay = begin_mutation(
        session,
        installation_id=client.installation_id,
        operation=_OP_POST,
        key=key,
        payload={"draft_id": draft_id},
    )
    if isinstance(claim_or_replay, Replay):
        return JSONResponse(
            status_code=claim_or_replay.status_code, content=claim_or_replay.body
        )
    claim = claim_or_replay
    try:
        posted_ref = service.post_draft(JournalDraftRef(draft_id))
        view = service.get_posted_journal(posted_ref)
    except HesabYarError as exc:
        raise _fail_mutation(
            session,
            client,
            claim,
            exc,
            action=_OP_POST,
            resource_type="posted_journal",
            resource_id=str(draft_id),
            company_external_id=row.external_company_id,
            tryton_reference=f"account.move:{draft_id}",
            metadata_after={"draft_id": draft_id},
        ) from exc
    response = PostedJournalResponse.from_view(view, row.id)
    _complete_mutation(
        session,
        client,
        claim,
        response_status=200,
        response=response,
        response_reference=f"accounting/journals/{view.ref.engine_id}",
        action=_OP_POST,
        resource_type="posted_journal",
        resource_id=str(view.ref.engine_id),
        company_external_id=row.external_company_id,
        tryton_reference=f"account.move:{view.ref.engine_id}",
        metadata_after={
            "draft_id": draft_id,
            "number": view.number,
            "total_debit": view.total_debit.amount,
            "total_credit": view.total_credit.amount,
        },
    )
    return response


@router.post(
    "/accounting/journals/{journal_id}/reverse",
    response_model=PostedJournalResponse,
    status_code=201,
    summary="Reverse a posted journal",
)
def reverse_posted_journal(
    journal_id: int,
    body: ReverseJournalRequest | None = None,
    idempotency_key: str | None = Header(
        default=None,
        alias=IDEMPOTENCY_KEY_HEADER,
        description="Unique key for this mutation; retries replay the original response",
    ),
    client: AuthenticatedClient = Depends(require_scopes("ledger:reverse")),
    session: Session = Depends(get_db_session),
    service: JournalDraftService = Depends(get_journal_draft_service),
) -> PostedJournalResponse | JSONResponse:
    """Reverse a posted journal with a new posted reversal journal.

    The reversal's lines cancel the original's (debit/credit swapped)
    and its ``origin_id`` links back to the original. Only posted
    journals can be reversed. If the reversal's post fails after its
    draft was created, the service retries the post once; a failure
    that still leaves a reversal draft surfaces its id in the audit
    trail for recovery.
    """
    with translate_errors():
        original = service.get_posted_journal(PostedJournalRef(journal_id))
    row = _require_visible_company(session, client, original.company, journal_id, posted=True)
    key = require_idempotency_key(idempotency_key)
    description = body.description if body is not None else ""
    claim_or_replay = begin_mutation(
        session,
        installation_id=client.installation_id,
        operation=_OP_REVERSE,
        key=key,
        payload={"journal_id": journal_id, "description": description},
    )
    if isinstance(claim_or_replay, Replay):
        return JSONResponse(
            status_code=claim_or_replay.status_code, content=claim_or_replay.body
        )
    claim = claim_or_replay
    try:
        reversal_ref = service.reverse_journal(
            PostedJournalRef(journal_id), description=description
        )
        view = service.get_posted_journal(reversal_ref)
    except HesabYarError as exc:
        raise _fail_mutation(
            session,
            client,
            claim,
            exc,
            action=_OP_REVERSE,
            resource_type="posted_journal",
            resource_id=str(journal_id),
            company_external_id=row.external_company_id,
            tryton_reference=f"account.move:{journal_id}",
            metadata_after={"original_journal_id": journal_id},
        ) from exc
    response = PostedJournalResponse.from_view(view, row.id)
    _complete_mutation(
        session,
        client,
        claim,
        response_status=201,
        response=response,
        response_reference=f"accounting/journals/{view.ref.engine_id}",
        action=_OP_REVERSE,
        resource_type="posted_journal",
        resource_id=str(view.ref.engine_id),
        company_external_id=row.external_company_id,
        tryton_reference=f"account.move:{view.ref.engine_id}",
        metadata_after={
            "original_journal_id": journal_id,
            "number": view.number,
            "total_debit": view.total_debit.amount,
            "total_credit": view.total_credit.amount,
        },
    )
    return response
