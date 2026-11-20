"""Cheque routes: manual registration + the §2.1 lifecycle (US-531)
+ issued / collateral (US-532) + Sayyad reconciliation (US-534).

Thin routes over the cheque services: no state-machine logic here —
the service owns transitions, the engine owns the journals.

- ``POST /cheques`` — manual registration (the 24-field payload,
  intake, NO journal; 201). A live cheque with the same sayad_id is
  a 409, unless its engine link failed (then the link is retried).
- ``GET /cheques`` / ``{cheque_id}`` — registry reads (detail carries
  the custody history + inquiry receipts).
- ``POST /cheques/{id}/confirm-receipt`` — registered -> held (سند ۱).
- ``POST /cheques/{id}/deposit`` — held -> in_clearing (سند ۲).
- ``POST /cheques/{id}/clear`` — in_clearing -> cleared (سند ۳).
- ``POST /cheques/{id}/bounce`` — in_clearing -> bounced (سند ۴,
  current period; bank document required).
- ``POST /cheques/{id}/endorse`` — held -> endorsed (سند ۵; Sayyad
  transfer required; endorsement to a non-supplier defers its
  journal for the accountant, §9.1 #12).
- ``POST /cheques/{id}/return`` — held -> returned (سند ۶) or
  bounced -> returned (no journal).
- ``POST /cheques/{id}/reject-mismatch`` — registered ->
  rejected_mismatch (never confirm on mismatch; no journal).
- ``POST /cheques/{id}/receipts`` — record a manual Sayyad inquiry
  receipt (verdict/code/time only, never raw PII).
- ``GET /cheques/settings`` — the installation's cheque policy.
- ``POST /cheques/{id}/sayyad-inquiries`` — run one Fintech Sayyad
  scope inquiry (simulator until B3, US-534); the verdict is stored
  as a structured receipt.
- ``POST /cheques/{id}/ehrazino-inquiries`` — run one Ehrazino
  inquiry, ``color`` or ``sayad_check`` (simulator until B3
  verifies transport, US-534).
- ``POST /cheques/{id}/clearing/confirm`` — human-confirm a
  suggested clearing match (US-534).
- ``GET /cheques/{id}/inquiries`` — the cheque's inquiry receipts.
- ``GET /cheques/reports/in-clearing-aging`` — in-clearing ages
  (report 4 inputs, US-534); ``GET /cheques/reports/inquiry-ledger``
  — aggregate inquiry ledger (report 13 inputs).
- ``POST /cheques/reports/receipt-rule-audit`` — file
  ``missing_inquiry_receipt`` items for held cheques without an
  item/status receipt (mandatory receipt rule, §1.4).
- ``POST /cheques/{id}/issue`` — registered -> issued (سند ۱۰;
  collateral issues the memorandum in the issued direction).
- ``POST /cheques/{id}/void`` — issued -> voided (سند ۱۳; leaf
  voided in its checkbook, appears in report 8).
- ``POST /cheques/{id}/call-in`` — held -> called_in (سند ۹الف +
  ۹ب in one transaction; accountant approval required, §9.1 #4).
- ``POST /cheques/{id}/unexpected-call-in`` — issued collateral
  presented without notice (memorandum reversal only; the
  commercial journal is accountant-gated, §9.1 #4).
- ``POST /cheques/{id}/endorsed-bounce`` — endorsed -> bounced
  (NO journal — the reverse-of-endorsement design is
  accountant-gated, §9.1 #3).
- ``GET /cheques/checkbooks`` / ``{id}`` — report 8 (leaf counts +
  Fintech inquiry simulator).
- ``GET /cheques/collateral-summary`` — report 6 (memorandum-only).
- ``GET /cheques/endorsement-summary`` — report 5 (supplier vs other).

Reads need ``cheques:read``; registration needs ``cheques:write``;
every transition needs ``cheques:transition``. Everything is
tenant/installation-scoped: a row of another installation answers
404 as if it did not exist. Re-presentment of a bounced cheque
(bounced -> in_clearing) is DISABLED by design (§9.1 #5) — there is
no endpoint for it.
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.orm import Session

from hesabyar.application.commands.cheque_clearing import ChequeClearingService
from hesabyar.application.commands.cheques import ChequeCommandService
from hesabyar.application.commands.sayyad import SayyadInquiryService
from hesabyar.application.queries.cheques import ChequeQueryService
from hesabyar.domain.cheques.errors import ChequeValidationError
from hesabyar.domain.cheques.models import (
    ChequeKind,
    ChequePurpose,
    ChequeState,
    StorageLocation,
)
from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.infrastructure.persistence.postgres.models import EngineCompanyModel
from hesabyar.interfaces.rest.dependencies import (
    company_ref_for_row,
    find_company_row_for_engine_id,
    get_cheque_clearing_service,
    get_cheque_command_service,
    get_cheque_query_service,
    get_db_session,
    get_sayyad_inquiry_service,
    require_company_row_by_id,
    require_scopes,
)
from hesabyar.interfaces.rest.errors import ApiError, translate_errors
from hesabyar.interfaces.rest.pagination import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    reject_unknown_params,
)
from hesabyar.interfaces.rest.schemas.cheques import (
    BounceRequest,
    CallInRequest,
    CheckbookResponse,
    ChequeDetailResponse,
    ChequeResponse,
    ChequeSettingsResponse,
    ClearingAgingResponse,
    ClearingConfirmRequest,
    ClearingLineDecisionResponse,
    ClearRequest,
    CollateralSummaryResponse,
    ConfirmReceiptRequest,
    CreateChequeRequest,
    DepositRequest,
    EhrazinoInquiryRequest,
    EndorsedBounceRequest,
    EndorsementSummaryResponse,
    EndorseRequest,
    InquiryLedgerResponse,
    InquiryReceiptRequest,
    InquiryReceiptResponse,
    IssueRequest,
    ReceiptRuleAuditResponse,
    RejectMismatchRequest,
    SayyadInquiryRequest,
    UnexpectedCallInRequest,
    VoidRequest,
)

router = APIRouter(tags=["cheques"])

_LIST_QUERY_PARAMS = frozenset(
    {
        "state",
        "issuer_national_id",
        "due_before",
        "kind",
        "purpose",
        "endorsee_type",
        "storage_location",
        "limit",
        "offset",
    }
)


def _require_cheque_company_access(
    session: Session, client: AuthenticatedClient, company_engine_id: int
) -> None:
    """Enforce the key's company restriction for a cheque's company.

    The cheque's company must be mapped to this tenant; a key
    restricted to a different company gets 403, an unmapped company
    answers 404 as if the cheque did not exist.
    """
    row = find_company_row_for_engine_id(session, client.tenant_id, company_engine_id)
    if row is None:
        raise ApiError(
            status_code=404,
            code="COMPANY_NOT_FOUND",
            detail=f"Company {company_engine_id} does not exist.",
        )
    if not client.can_access_company(row.id):
        raise ApiError(
            status_code=403,
            code="FORBIDDEN",
            detail="The API key is restricted to a different company.",
        )


@router.post(
    "/cheques",
    response_model=ChequeResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a cheque manually (intake, no journal)",
)
def create_cheque(
    body: CreateChequeRequest,
    client: AuthenticatedClient = Depends(require_scopes("cheques:write")),
    session: Session = Depends(get_db_session),
    service: ChequeCommandService = Depends(get_cheque_command_service),
) -> ChequeResponse:
    """Register a cheque manually: the 24-field payload, validated fail-closed."""
    row: EngineCompanyModel = require_company_row_by_id(
        session, client, body.company_id
    )
    company_ref = company_ref_for_row(row)
    with translate_errors():
        cheque = service.register_manual(
            tenant_id=client.tenant_id,
            installation_id=client.installation_id,
            company_engine_id=company_ref.engine_id,
            payload=body.payload_fields(),
            actor=client.client_id,
            kind=ChequeKind(body.kind),
            checkbook_engine_id=body.checkbook_id,
            secured_obligation=body.secured_obligation,
        )
    session.commit()
    return ChequeResponse.from_cheque(cheque)


@router.get(
    "/cheques",
    response_model=list[ChequeResponse],
    summary="List cheques in this installation",
)
def list_cheques(
    request: Request,
    state: str | None = Query(default=None, description="Filter by state"),
    issuer_national_id: str | None = Query(default=None, description="Filter by issuer national id"),
    due_before: str | None = Query(default=None, description="Filter by due date before this Jalali YYYY-MM-DD date"),
    kind: str | None = Query(default=None, description="Filter by kind: received | issued"),
    purpose: str | None = Query(default=None, description="Filter by purpose: payment | collateral"),
    endorsee_type: str | None = Query(default=None, description="Filter by endorsee type: supplier | other"),
    storage_location: str | None = Query(default=None, description="Filter by storage location"),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"),
    offset: int = Query(default=0, ge=0, description="Page offset"),
    client: AuthenticatedClient = Depends(require_scopes("cheques:read")),
    service: ChequeQueryService = Depends(get_cheque_query_service),
) -> list[ChequeResponse]:
    """Return the installation's cheques, newest first."""
    reject_unknown_params(request, _LIST_QUERY_PARAMS)
    parsed_state: ChequeState | None = None
    if state is not None:
        try:
            parsed_state = ChequeState(state)
        except ValueError as exc:
            raise ApiError(
                status_code=422,
                code="CHEQUE_VALIDATION_FAILED",
                detail=f"Unknown cheque state {state!r}.",
            ) from exc
    parsed_due: datetime.date | None = None
    if due_before is not None:
        from hesabyar.domain.cheques.validation import (
            parse_jalali_date,  # noqa: PLC0415 - local to keep the import list short
        )

        with translate_errors():
            parsed_due = parse_jalali_date(due_before).date
    parsed_kind: ChequeKind | None = None
    if kind is not None:
        try:
            parsed_kind = ChequeKind(kind)
        except ValueError as exc:
            raise ApiError(
                status_code=422,
                code="CHEQUE_VALIDATION_FAILED",
                detail=f"Unknown cheque kind {kind!r}.",
            ) from exc
    parsed_purpose: ChequePurpose | None = None
    if purpose is not None:
        try:
            parsed_purpose = ChequePurpose(purpose)
        except ValueError as exc:
            raise ApiError(
                status_code=422,
                code="CHEQUE_VALIDATION_FAILED",
                detail=f"Unknown cheque purpose {purpose!r}.",
            ) from exc
    parsed_storage: StorageLocation | None = None
    if storage_location is not None:
        try:
            parsed_storage = StorageLocation(storage_location)
        except ValueError as exc:
            raise ApiError(
                status_code=422,
                code="CHEQUE_VALIDATION_FAILED",
                detail=f"Unknown storage location {storage_location!r}.",
            ) from exc
    with translate_errors():
        cheques = service.list_cheques(
            tenant_id=client.tenant_id,
            installation_id=client.installation_id,
            state=parsed_state,
            issuer_national_id=issuer_national_id,
            due_before=parsed_due,
            kind=parsed_kind,
            purpose=parsed_purpose,
            endorsee_type=endorsee_type,
            storage_location=parsed_storage,
            limit=limit,
            offset=offset,
        )
    return [ChequeResponse.from_cheque(cheque) for cheque in cheques]


@router.get(
    "/cheques/settings",
    response_model=ChequeSettingsResponse,
    summary="Get the installation's cheque policy",
)
def get_cheque_settings(
    client: AuthenticatedClient = Depends(require_scopes("cheques:read")),
    service: ChequeQueryService = Depends(get_cheque_query_service),
) -> ChequeSettingsResponse:
    """Return the installation's cheque settings (defaults when unset)."""
    with translate_errors():
        settings = service.get_settings(client.installation_id)
    return ChequeSettingsResponse(
        installation_id=settings.installation_id,
        leaf_arrival_deadline_days=settings.leaf_arrival_deadline_days,
        clearing_window_days=settings.clearing_window_days,
        bounce_destination=settings.bounce_destination.value,
        red_brown_policy=settings.red_brown_policy.value,
        max_in_transit_commitments_irr=settings.max_in_transit_commitments_irr,
        default_storage_location=settings.default_storage_location.value,
    )


@router.get(
    "/cheques/checkbooks",
    response_model=list[CheckbookResponse],
    summary="List checkbooks with leaf counts + inquiry (report 8)",
)
def list_checkbooks(
    company_id: int = Query(description="Engine company id"),
    client: AuthenticatedClient = Depends(require_scopes("cheques:read")),
    service: ChequeQueryService = Depends(get_cheque_query_service),
) -> list[CheckbookResponse]:
    """Report 8: every checkbook of the company with used/voided/remaining leaves."""
    with translate_errors():
        statuses = service.get_checkbooks(company_engine_id=company_id)
    return [CheckbookResponse.from_status(status) for status in statuses]


@router.get(
    "/cheques/checkbooks/{checkbook_id}",
    response_model=CheckbookResponse,
    summary="Get one checkbook with leaf counts + inquiry (report 8)",
)
def get_checkbook(
    checkbook_id: int,
    company_id: int = Query(description="Engine company id"),
    client: AuthenticatedClient = Depends(require_scopes("cheques:read")),
    service: ChequeQueryService = Depends(get_cheque_query_service),
) -> CheckbookResponse:
    """Report 8 for one checkbook."""
    with translate_errors():
        status = service.get_checkbook_status(
            company_engine_id=company_id, checkbook_engine_id=checkbook_id
        )
    return CheckbookResponse.from_status(status)


@router.get(
    "/cheques/collateral-summary",
    response_model=CollateralSummaryResponse,
    summary="Collateral summary (report 6, memorandum-only)",
)
def get_collateral_summary(
    client: AuthenticatedClient = Depends(require_scopes("cheques:read")),
    service: ChequeQueryService = Depends(get_cheque_query_service),
) -> CollateralSummaryResponse:
    """Report 6: collateral cheques + memorandum totals + the secured-obligation dimension."""
    with translate_errors():
        summary = service.collateral_summary(
            tenant_id=client.tenant_id,
            installation_id=client.installation_id,
        )
    return CollateralSummaryResponse.from_summary(summary)


@router.get(
    "/cheques/endorsement-summary",
    response_model=EndorsementSummaryResponse,
    summary="Endorsement summary (report 5, supplier vs other)",
)
def get_endorsement_summary(
    client: AuthenticatedClient = Depends(require_scopes("cheques:read")),
    service: ChequeQueryService = Depends(get_cheque_query_service),
) -> EndorsementSummaryResponse:
    """Report 5: endorsed cheques split by endorsee type (other is accountant-gated, §9.1 #12)."""
    with translate_errors():
        summary = service.endorsement_summary(
            tenant_id=client.tenant_id,
            installation_id=client.installation_id,
        )
    return EndorsementSummaryResponse.from_summary(summary)


@router.get(
    "/cheques/intake",
    response_model=list[ChequeResponse],
    summary="List the intake queue: registered cheques awaiting the leaf",
)
def list_cheque_intake(
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"),
    offset: int = Query(default=0, ge=0, description="Page offset"),
    client: AuthenticatedClient = Depends(require_scopes("cheques:read")),
    service: ChequeQueryService = Depends(get_cheque_query_service),
) -> list[ChequeResponse]:
    """Return the intake queue: registered cheques, oldest first.

    US-533 — the queue the operator watches for the physical leaf.
    ``age_days`` (in the response) is the number of days since the
    site registered the cheque; the day-3 and day-(N-1) reminders
    are filed by the daily Procrastinate job. This route must stay
    ABOVE ``/cheques/{cheque_id}`` or FastAPI captures ``intake``
    as a cheque id.
    """
    with translate_errors():
        cheques = service.list_intake_queue(
            tenant_id=client.tenant_id,
            installation_id=client.installation_id,
            limit=limit,
            offset=offset,
        )
    return [ChequeResponse.from_cheque(cheque) for cheque in cheques]


@router.get(
    "/cheques/{cheque_id}",
    response_model=ChequeDetailResponse,
    summary="Get one cheque with its custody history and inquiry receipts",
)
def get_cheque(
    cheque_id: str,
    client: AuthenticatedClient = Depends(require_scopes("cheques:read")),
    service: ChequeQueryService = Depends(get_cheque_query_service),
) -> ChequeDetailResponse:
    """Return one cheque by id."""
    with translate_errors():
        detail = service.get_cheque(
            cheque_id, tenant_id=client.tenant_id, installation_id=client.installation_id
        )
    return ChequeDetailResponse.from_detail(detail)


def _transition_cheque(
    cheque_id: str,
    client: AuthenticatedClient,
    session: Session,
    service: ChequeCommandService,
    call,
) -> ChequeResponse:
    """Run one transition with company-access enforcement + commit."""
    with translate_errors():
        cheque = service.get_cheque(
            cheque_id, tenant_id=client.tenant_id, installation_id=client.installation_id
        )
    _require_cheque_company_access(session, client, cheque.company_engine_id)
    with translate_errors():
        updated = call(cheque)
    session.commit()
    return ChequeResponse.from_cheque(updated)


@router.post(
    "/cheques/{cheque_id}/confirm-receipt",
    response_model=ChequeResponse,
    summary="Confirm the leaf arrived + Sayyad confirmed (registered -> held)",
)
def confirm_receipt(
    cheque_id: str,
    body: ConfirmReceiptRequest,
    client: AuthenticatedClient = Depends(require_scopes("cheques:transition")),
    session: Session = Depends(get_db_session),
    service: ChequeCommandService = Depends(get_cheque_command_service),
) -> ChequeResponse:
    """Confirm receipt: pre-acceptance (item match + issuer credit) then سند ۱."""
    storage = None
    if body.storage_location is not None:
        try:
            storage = StorageLocation(body.storage_location)
        except ValueError as exc:
            raise ApiError(
                status_code=422,
                code="CHEQUE_VALIDATION_FAILED",
                detail=f"Unknown storage location {body.storage_location!r}.",
            ) from exc
    receipt = body.inquiry_receipt.model_dump() if body.inquiry_receipt is not None else None
    return _transition_cheque(
        cheque_id,
        client,
        session,
        service,
        lambda cheque: service.confirm_receipt(
            tenant_id=client.tenant_id,
            installation_id=client.installation_id,
            cheque_id=cheque.id,
            actor=client.client_id,
            items_matched=body.items_matched,
            sayad_accepted=body.sayad_accepted,
            color_code=body.color_code,
            back_cheques_count=body.back_cheques_count,
            in_transit_commitments_irr=body.in_transit_commitments_irr,
            manager_approval=body.manager_approval,
            inquiry_receipt=receipt,
            storage_location=storage,
        ),
    )


@router.post(
    "/cheques/{cheque_id}/deposit",
    response_model=ChequeResponse,
    summary="Hand the leaf to the bank for clearing (held -> in_clearing)",
)
def deposit_cheque(
    cheque_id: str,
    body: DepositRequest,
    client: AuthenticatedClient = Depends(require_scopes("cheques:transition")),
    session: Session = Depends(get_db_session),
    service: ChequeCommandService = Depends(get_cheque_command_service),
) -> ChequeResponse:
    """Deposit: the destination bank account is chosen here; the leaf moves to the bank."""
    return _transition_cheque(
        cheque_id,
        client,
        session,
        service,
        lambda cheque: service.deposit(
            tenant_id=client.tenant_id,
            installation_id=client.installation_id,
            cheque_id=cheque.id,
            actor=client.client_id,
            destination_account_id=body.destination_account_id,
            destination_bank_name=body.destination_bank_name,
        ),
    )


@router.post(
    "/cheques/{cheque_id}/clear",
    response_model=ChequeResponse,
    summary="Confirm clearing (in_clearing -> cleared)",
)
def clear_cheque(
    cheque_id: str,
    body: ClearRequest,
    client: AuthenticatedClient = Depends(require_scopes("cheques:transition")),
    session: Session = Depends(get_db_session),
    service: ChequeCommandService = Depends(get_cheque_command_service),
) -> ChequeResponse:
    """Clear: manual confirmation (statement matching is US-534)."""
    return _transition_cheque(
        cheque_id,
        client,
        session,
        service,
        lambda cheque: service.clear(
            tenant_id=client.tenant_id,
            installation_id=client.installation_id,
            cheque_id=cheque.id,
            actor=client.client_id,
            statement_reference=body.statement_reference,
        ),
    )


@router.post(
    "/cheques/{cheque_id}/bounce",
    response_model=ChequeResponse,
    summary="Record a bounce (in_clearing -> bounced)",
)
def bounce_cheque(
    cheque_id: str,
    body: BounceRequest,
    client: AuthenticatedClient = Depends(require_scopes("cheques:transition")),
    session: Session = Depends(get_db_session),
    service: ChequeCommandService = Depends(get_cheque_command_service),
) -> ChequeResponse:
    """Bounce: reason + date + bank certificate required; reversal in the current period."""
    bounce_destination = None
    if body.bounce_destination is not None:
        from hesabyar.domain.cheques.models import (
            BounceDestination,  # noqa: PLC0415 - local to keep the import list short
        )

        try:
            bounce_destination = BounceDestination(body.bounce_destination)
        except ValueError as exc:
            raise ApiError(
                status_code=422,
                code="CHEQUE_VALIDATION_FAILED",
                detail=f"Unknown bounce destination {body.bounce_destination!r}.",
            ) from exc
    return _transition_cheque(
        cheque_id,
        client,
        session,
        service,
        lambda cheque: service.bounce(
            tenant_id=client.tenant_id,
            installation_id=client.installation_id,
            cheque_id=cheque.id,
            actor=client.client_id,
            bounce_reason=body.bounce_reason,
            bounce_date=body.bounce_date,
            certificate_reference=body.certificate_reference,
            bounce_destination=bounce_destination,
        ),
    )


@router.post(
    "/cheques/{cheque_id}/endorse",
    response_model=ChequeResponse,
    summary="Endorse the cheque on (held -> endorsed)",
)
def endorse_cheque(
    cheque_id: str,
    body: EndorseRequest,
    client: AuthenticatedClient = Depends(require_scopes("cheques:transition")),
    session: Session = Depends(get_db_session),
    service: ChequeCommandService = Depends(get_cheque_command_service),
) -> ChequeResponse:
    """Endorse: Sayyad transfer registration required; non-supplier endorsees defer the journal."""
    return _transition_cheque(
        cheque_id,
        client,
        session,
        service,
        lambda cheque: service.endorse(
            tenant_id=client.tenant_id,
            installation_id=client.installation_id,
            cheque_id=cheque.id,
            actor=client.client_id,
            endorsee_name=body.endorsee_name,
            endorsee_type=body.endorsee_type,
            sayad_transfer_reference=body.sayad_transfer_reference,
        ),
    )


@router.post(
    "/cheques/{cheque_id}/return",
    response_model=ChequeResponse,
    summary="Return the leaf to the issuer (held -> returned, or bounced -> returned)",
)
def return_cheque(
    cheque_id: str,
    client: AuthenticatedClient = Depends(require_scopes("cheques:transition")),
    session: Session = Depends(get_db_session),
    service: ChequeCommandService = Depends(get_cheque_command_service),
) -> ChequeResponse:
    """Return: سند ۶ from held; no journal from bounced."""
    return _transition_cheque(
        cheque_id,
        client,
        session,
        service,
        lambda cheque: service.return_cheque(
            tenant_id=client.tenant_id,
            installation_id=client.installation_id,
            cheque_id=cheque.id,
            actor=client.client_id,
        ),
    )


@router.post(
    "/cheques/{cheque_id}/reject-mismatch",
    response_model=ChequeResponse,
    summary="Reject a registered cheque whose leaf disagrees with Sayyad",
)
def reject_mismatch(
    cheque_id: str,
    body: RejectMismatchRequest,
    client: AuthenticatedClient = Depends(require_scopes("cheques:transition")),
    session: Session = Depends(get_db_session),
    service: ChequeCommandService = Depends(get_cheque_command_service),
) -> ChequeResponse:
    """Reject on mismatch: never confirm on mismatch; no journal."""
    return _transition_cheque(
        cheque_id,
        client,
        session,
        service,
        lambda cheque: service.reject_mismatch(
            tenant_id=client.tenant_id,
            installation_id=client.installation_id,
            cheque_id=cheque.id,
            actor=client.client_id,
            mismatch_detail=body.mismatch_detail,
        ),
    )


@router.post(
    "/cheques/{cheque_id}/issue",
    response_model=ChequeResponse,
    summary="Issue a cheque from our checkbook (registered -> issued, سند ۱۰)",
)
def issue_cheque(
    cheque_id: str,
    body: IssueRequest,
    client: AuthenticatedClient = Depends(require_scopes("cheques:transition")),
    session: Session = Depends(get_db_session),
    service: ChequeCommandService = Depends(get_cheque_command_service),
) -> ChequeResponse:
    """Issue: the leaf leaves the checkbook for the beneficiary; collateral issues the memorandum."""
    return _transition_cheque(
        cheque_id,
        client,
        session,
        service,
        lambda cheque: service.issue_cheque(
            tenant_id=client.tenant_id,
            installation_id=client.installation_id,
            cheque_id=cheque.id,
            actor=client.client_id,
            destination_account_id=body.destination_account_id,
            destination_bank_name=body.destination_bank_name,
        ),
    )


@router.post(
    "/cheques/{cheque_id}/void",
    response_model=ChequeResponse,
    summary="Void an issued cheque (issued -> voided, سند ۱۳)",
)
def void_issued_cheque(
    cheque_id: str,
    body: VoidRequest,
    client: AuthenticatedClient = Depends(require_scopes("cheques:transition")),
    session: Session = Depends(get_db_session),
    service: ChequeCommandService = Depends(get_cheque_command_service),
) -> ChequeResponse:
    """Void: the obligation returns to the beneficiary; the leaf is voided in its checkbook."""
    return _transition_cheque(
        cheque_id,
        client,
        session,
        service,
        lambda cheque: service.void_issued(
            tenant_id=client.tenant_id,
            installation_id=client.installation_id,
            cheque_id=cheque.id,
            actor=client.client_id,
            void_reason=body.void_reason,
        ),
    )


@router.post(
    "/cheques/{cheque_id}/call-in",
    response_model=ChequeResponse,
    summary="Call in a received collateral cheque (held -> called_in, سند ۹الف + ۹ب)",
)
def call_in_cheque(
    cheque_id: str,
    body: CallInRequest,
    client: AuthenticatedClient = Depends(require_scopes("cheques:transition")),
    session: Session = Depends(get_db_session),
    service: ChequeCommandService = Depends(get_cheque_command_service),
) -> ChequeResponse:
    """Call in: two journals in one transaction, accountant approval required (§9.1 #4)."""
    return _transition_cheque(
        cheque_id,
        client,
        session,
        service,
        lambda cheque: service.call_in(
            tenant_id=client.tenant_id,
            installation_id=client.installation_id,
            cheque_id=cheque.id,
            actor=client.client_id,
            accountant_approval=body.accountant_approval,
        ),
    )


@router.post(
    "/cheques/{cheque_id}/unexpected-call-in",
    response_model=ChequeResponse,
    summary="Record an unexpected call-in of an issued collateral cheque",
)
def unexpected_call_in_cheque(
    cheque_id: str,
    body: UnexpectedCallInRequest,
    client: AuthenticatedClient = Depends(require_scopes("cheques:transition")),
    session: Session = Depends(get_db_session),
    service: ChequeCommandService = Depends(get_cheque_command_service),
) -> ChequeResponse:
    """Unexpected call-in: alert + statement matching + memorandum
    reversal; the commercial journal is accountant-gated."""
    return _transition_cheque(
        cheque_id,
        client,
        session,
        service,
        lambda cheque: service.unexpected_call_in(
            tenant_id=client.tenant_id,
            installation_id=client.installation_id,
            cheque_id=cheque.id,
            actor=client.client_id,
            statement_reference=body.statement_reference,
        ),
    )


@router.post(
    "/cheques/{cheque_id}/endorsed-bounce",
    response_model=ChequeResponse,
    summary="Record that an endorsed cheque bounced (endorsed -> bounced, no journal)",
)
def endorsed_bounce_cheque(
    cheque_id: str,
    body: EndorsedBounceRequest,
    client: AuthenticatedClient = Depends(require_scopes("cheques:transition")),
    session: Session = Depends(get_db_session),
    service: ChequeCommandService = Depends(get_cheque_command_service),
) -> ChequeResponse:
    """Endorsed bounce: no journal is guessed (§9.1 #3); the evidence is recorded and a CRITICAL review is filed."""
    return _transition_cheque(
        cheque_id,
        client,
        session,
        service,
        lambda cheque: service.record_endorsed_bounce(
            tenant_id=client.tenant_id,
            installation_id=client.installation_id,
            cheque_id=cheque.id,
            actor=client.client_id,
            bounce_reason=body.bounce_reason,
            bounce_date=body.bounce_date,
            certificate_reference=body.certificate_reference,
        ),
    )


@router.post(
    "/cheques/{cheque_id}/receipts",
    response_model=InquiryReceiptResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Record a manual Sayyad inquiry receipt",
)
def record_receipt(
    cheque_id: str,
    body: InquiryReceiptRequest,
    client: AuthenticatedClient = Depends(require_scopes("cheques:transition")),
    session: Session = Depends(get_db_session),
    service: ChequeCommandService = Depends(get_cheque_command_service),
) -> InquiryReceiptResponse:
    """Record a manual inquiry result (verdict/code/time only — never raw PII)."""
    with translate_errors():
        cheque = service.get_cheque(
            cheque_id, tenant_id=client.tenant_id, installation_id=client.installation_id
        )
    _require_cheque_company_access(session, client, cheque.company_engine_id)
    with translate_errors():
        receipt = service.record_inquiry_receipt(
            tenant_id=client.tenant_id,
            installation_id=client.installation_id,
            cheque_id=cheque.id,
            actor=client.client_id,
            channel=body.channel,
            inquiry_type=body.inquiry_type,
            result_code=body.result_code,
            result_summary=body.result_summary,
            reference=body.reference,
            cost_irr=body.cost_irr,
            requested_at=body.requested_at,
            responded_at=body.responded_at,
        )
    session.commit()
    return InquiryReceiptResponse.from_receipt(receipt)


# -- Sayyad inquiries (US-534, Layers 1–2) --------------------------------------


@router.post(
    "/cheques/{cheque_id}/sayyad-inquiries",
    response_model=InquiryReceiptResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Run one Fintech Sayyad scope inquiry (simulator until B3)",
)
def run_sayyad_inquiry(
    cheque_id: str,
    body: SayyadInquiryRequest,
    client: AuthenticatedClient = Depends(require_scopes("cheques:transition")),
    session: Session = Depends(get_db_session),
    service: SayyadInquiryService = Depends(get_sayyad_inquiry_service),
) -> InquiryReceiptResponse:
    """Run one Fintech Sayyad scope inquiry; the verdict is stored as a receipt.

    Simulator until the ۵ت remainder (B3) verifies the Fintech
    transport — the scope's field shapes are UNVERIFIED (§9.2 #1–3)
    and no field is guessed. Only verdict/code/time/reference are
    stored (§3.5); an unknown scope fails closed.
    """
    with translate_errors():
        cheque = service.get_cheque(
            cheque_id, tenant_id=client.tenant_id, installation_id=client.installation_id
        )
    _require_cheque_company_access(session, client, cheque.company_engine_id)
    with translate_errors():
        receipt = service.inquire_finnotech(
            tenant_id=client.tenant_id,
            installation_id=client.installation_id,
            cheque_id=cheque.id,
            actor=client.client_id,
            scope=body.scope,
            scenario=body.scenario,
        )
    session.commit()
    return InquiryReceiptResponse.from_receipt(receipt)


@router.post(
    "/cheques/{cheque_id}/ehrazino-inquiries",
    response_model=InquiryReceiptResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Run one Ehrazino inquiry (simulator until B3 verifies transport)",
)
def run_ehrazino_inquiry(
    cheque_id: str,
    body: EhrazinoInquiryRequest,
    client: AuthenticatedClient = Depends(require_scopes("cheques:transition")),
    session: Session = Depends(get_db_session),
    service: SayyadInquiryService = Depends(get_sayyad_inquiry_service),
) -> InquiryReceiptResponse:
    """Run an Ehrazino ``color`` or ``sayad_check`` inquiry; verdict stored as a receipt.

    Colour is issuer history only — never a statement about this
    leaf. The endpoint path + auth header are still UNVERIFIED, so
    this runs against the simulator until B3 verifies the transport.
    """
    with translate_errors():
        cheque = service.get_cheque(
            cheque_id, tenant_id=client.tenant_id, installation_id=client.installation_id
        )
    _require_cheque_company_access(session, client, cheque.company_engine_id)
    with translate_errors():
        if body.kind == "color":
            receipt = service.inquire_ehrazino_color(
                tenant_id=client.tenant_id,
                installation_id=client.installation_id,
                cheque_id=cheque.id,
                actor=client.client_id,
                scenario=body.scenario,
            )
        elif body.kind == "sayad_check":
            receipt = service.inquire_ehrazino_sayad_check(
                tenant_id=client.tenant_id,
                installation_id=client.installation_id,
                cheque_id=cheque.id,
                actor=client.client_id,
                scenario=body.scenario,
            )
        else:
            raise ChequeValidationError(
                f"Unknown Ehrazino inquiry kind {body.kind!r}.",
                field_errors={"kind": "Must be 'color' or 'sayad_check'."},
            )
    session.commit()
    return InquiryReceiptResponse.from_receipt(receipt)


@router.get(
    "/cheques/{cheque_id}/inquiries",
    response_model=list[InquiryReceiptResponse],
    summary="List a cheque's Sayyad inquiry receipts",
)
def list_cheque_inquiries(
    cheque_id: str,
    client: AuthenticatedClient = Depends(require_scopes("cheques:read")),
    session: Session = Depends(get_db_session),
    service: ChequeQueryService = Depends(get_cheque_query_service),
) -> list[InquiryReceiptResponse]:
    """Return the cheque's structured inquiry receipts, oldest first."""
    with translate_errors():
        detail = service.get_cheque(
            cheque_id, tenant_id=client.tenant_id, installation_id=client.installation_id
        )
    _require_cheque_company_access(session, client, detail.cheque.company_engine_id)
    return [InquiryReceiptResponse.from_receipt(r) for r in detail.inquiry_receipts]


# -- clearing confirm + reports (US-534, Layer 3) --------------------------------


@router.post(
    "/cheques/{cheque_id}/clearing/confirm",
    response_model=ClearingLineDecisionResponse,
    summary="Human-confirm a suggested clearing match",
)
def confirm_clearing_match(
    cheque_id: str,
    body: ClearingConfirmRequest,
    client: AuthenticatedClient = Depends(require_scopes("cheques:transition")),
    session: Session = Depends(get_db_session),
    service: ChequeClearingService = Depends(get_cheque_clearing_service),
) -> ClearingLineDecisionResponse:
    """Confirm a probable/ambiguous clearing match: clear the cheque.

    The line must carry a SUGGESTED match pointing at this cheque.
    An ambiguous match is never guessed by the matcher — a human
    picks the cheque here.
    """
    with translate_errors():
        cheque = service.get_cheque(
            cheque_id, tenant_id=client.tenant_id, installation_id=client.installation_id
        )
    row = find_company_row_for_engine_id(session, client.tenant_id, cheque.company_engine_id)
    if row is None:
        raise ApiError(
            status_code=404,
            code="COMPANY_NOT_FOUND",
            detail=f"Company {cheque.company_engine_id} does not exist.",
        )
    if not client.can_access_company(row.id):
        raise ApiError(
            status_code=403,
            code="FORBIDDEN",
            detail="The API key is restricted to a different company.",
        )
    with translate_errors():
        decision = service.confirm_match(
            tenant_id=client.tenant_id,
            installation_id=client.installation_id,
            company=company_ref_for_row(row),
            company_id=row.id,
            cheque_id=cheque.id,
            statement_id=body.statement_id,
            line_id=body.line_id,
            actor=client.client_id,
        )
    session.commit()
    return ClearingLineDecisionResponse(
        line_id=decision.line_id,
        confidence=decision.confidence.value,
        cheque_id=decision.cheque_id,
        sayad_id=decision.sayad_id,
        action=decision.action,
        reconciliation_id=decision.reconciliation_id,
        review_item_id=decision.review_item_id,
        reasons=list(decision.reasons),
    )


@router.get(
    "/cheques/reports/in-clearing-aging",
    response_model=list[ClearingAgingResponse],
    summary="In-clearing ages (report 4 inputs)",
)
def in_clearing_aging(
    client: AuthenticatedClient = Depends(require_scopes("cheques:read")),
    service: ChequeQueryService = Depends(get_cheque_query_service),
) -> list[ClearingAgingResponse]:
    """Ages of all in-clearing cheques; stale ones are only flagged, never auto-bounced."""
    with translate_errors():
        rows = service.list_in_clearing_aging(
            tenant_id=client.tenant_id, installation_id=client.installation_id
        )
    return [
        ClearingAgingResponse(
            cheque_id=row.cheque_id,
            sayad_id=row.sayad_id,
            days_in_clearing=row.days_in_clearing,
            stale=row.stale,
            window_days=row.window_days,
            window_unset=row.window_unset,
        )
        for row in rows
    ]


@router.get(
    "/cheques/reports/inquiry-ledger",
    response_model=InquiryLedgerResponse,
    summary="Aggregate inquiry ledger (report 13 inputs)",
)
def inquiry_ledger(
    client: AuthenticatedClient = Depends(require_scopes("cheques:read")),
    service: ChequeQueryService = Depends(get_cheque_query_service),
) -> InquiryLedgerResponse:
    """Counts by channel, colour distribution and aggregate inquiry cost."""
    with translate_errors():
        ledger = service.inquiry_ledger(
            tenant_id=client.tenant_id, installation_id=client.installation_id
        )
    return InquiryLedgerResponse(
        total_receipts=ledger.total_receipts,
        by_channel=dict(ledger.by_channel),
        color_distribution=dict(ledger.color_distribution),
        total_cost_irr=ledger.total_cost_irr,
    )


@router.post(
    "/cheques/reports/receipt-rule-audit",
    response_model=ReceiptRuleAuditResponse,
    summary="Audit the mandatory receipt rule (§1.4)",
)
def receipt_rule_audit(
    client: AuthenticatedClient = Depends(require_scopes("cheques:transition")),
    session: Session = Depends(get_db_session),
    service: SayyadInquiryService = Depends(get_sayyad_inquiry_service),
) -> ReceiptRuleAuditResponse:
    """File ``missing_inquiry_receipt`` items for held cheques without an item/status receipt.

    Idempotent per open item — a re-run files nothing new while the
    item is still open.
    """
    with translate_errors():
        audit = service.audit_receipt_rule(
            tenant_id=client.tenant_id,
            installation_id=client.installation_id,
            actor=client.client_id,
        )
    session.commit()
    return ReceiptRuleAuditResponse(
        checked=audit.checked,
        missing=audit.missing,
        items_filed=audit.items_filed,
        cheque_ids=list(audit.cheque_ids),
    )
