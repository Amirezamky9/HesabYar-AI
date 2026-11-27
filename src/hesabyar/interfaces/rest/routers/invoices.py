"""Invoice routes (spec §9.2, US-205).

Thin routes over the US-205 ``InvoiceDraftService``: no accounting rules
here, no adapter or Tryton calls — the service owns the engine port.
The routes own only HTTP concerns: auth scopes, company resolution,
idempotency, audit recording and response mapping.

Company scoping: draft creation takes its company from the request
body (``company_id`` — HesabYar row id or engine id). The list endpoint
takes its company from the ``company`` query parameter (or the API
key's company restriction, like the accounts endpoints). Get/validate
read the company from the engine record itself, then check the company
is mapped to the caller's tenant: an invoice of an unmapped company
looks exactly like an invoice that does not exist (404
``LEDGER_INVOICE_NOT_FOUND``), and a key restricted to another company
gets 403.

Idempotency (spec §10.4): ``Idempotency-Key`` is required on draft
creation. Same key + same payload replays the stored response with no
second engine call; same key + different payload is a 409
``IDEMPOTENCY_CONFLICT``.

Audit (spec §14.3): every draft creation writes an audit event —
success and failure alike — with actor, operation, company, Tryton
reference, idempotency key, payload hash and UTC timestamp. Validation
is read-only and writes no audit event, like journal validation.

Scopes: list/get behind ``invoice:read``; draft creation and
validation behind ``invoice:draft:create`` — the draft-workflow scope
(the invoice equivalent of journal validation's ``ledger:draft:update``;
there is no ``invoice:draft:update`` scope in the §14.2 catalog).
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from hesabyar.application.commands.invoices import InvoiceDraftService
from hesabyar.domain.accounting.commands import InvoiceDraftLine
from hesabyar.domain.accounting.refs import (
    AccountRef,
    InvoiceRef,
    JournalRef,
    PartyAddressRef,
    PartyRef,
)
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.errors import HesabYarError
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.infrastructure.persistence.postgres.models import EngineCompanyModel
from hesabyar.interfaces.rest.dependencies import (
    company_ref_for_row,
    get_db_session,
    get_invoice_draft_service,
    require_company_row_by_id,
    require_scopes,
    require_visible_company,
    resolve_accounts_company,
)
from hesabyar.interfaces.rest.errors import translate_errors
from hesabyar.interfaces.rest.idempotency import (
    IDEMPOTENCY_KEY_HEADER,
    Replay,
    begin_mutation,
    require_idempotency_key,
)
from hesabyar.interfaces.rest.mutations import complete_mutation, fail_mutation
from hesabyar.interfaces.rest.pagination import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    paginate,
    parse_sort_param,
    reject_unknown_params,
)
from hesabyar.interfaces.rest.schemas.invoices import (
    CreateInvoiceDraftRequest,
    InvoiceDraftLineRequest,
    InvoiceResponse,
)
from hesabyar.interfaces.rest.schemas.journals import ValidationResponse
from hesabyar.interfaces.rest.schemas.pagination import PaginatedResponse

router = APIRouter(tags=["invoices"])

# Idempotency operation name (spec §10.4 triple:
# installation_id + operation + key). The audit action reuses the same name.
_OP_CREATE = "invoice.draft.create"

_INVOICE_LIST_PARAMS = {"company", "type", "state", "sort", "limit", "cursor"}
_INVOICE_SORT_KEYS: dict[str, Callable[[InvoiceResponse], str]] = {
    # Drafts may have no invoice date yet; they sort as the empty string
    # (last when newest-first, first when oldest-first).
    "date": lambda item: item.invoice_date.isoformat() if item.invoice_date else "",
    "id": lambda item: str(item.id),
}


def _invoice_lines(lines: list[InvoiceDraftLineRequest]) -> tuple[InvoiceDraftLine, ...]:
    """Map request lines onto domain lines (invariants enforced at construction)."""
    return tuple(
        InvoiceDraftLine(
            account=AccountRef(line.account_id),
            unit_price=MoneyIRR(line.unit_price),
            quantity=line.quantity,
            description=line.description,
        )
        for line in lines
    )


@router.get(
    "/invoices",
    response_model=PaginatedResponse[InvoiceResponse],
    summary="List a company's invoices",
)
def list_invoices(
    request: Request,
    type: str | None = Query(
        default=None,
        description="Filter on invoice type: 'out' (customer) or 'in' (supplier)",
    ),
    state: str | None = Query(
        default=None,
        description=(
            "Filter on engine state: 'draft', 'validated', 'posted', 'paid' "
            "or 'cancelled'"
        ),
    ),
    sort: str | None = Query(
        default=None,
        description=(
            "Sort field: 'date' (default, newest first) or 'id'; "
            "prefix '-' for descending"
        ),
    ),
    limit: int = Query(
        default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"
    ),
    cursor: str | None = Query(
        default=None, description="Opaque cursor from a previous page's 'next_cursor'"
    ),
    _client: AuthenticatedClient = Depends(require_scopes("invoice:read")),
    row: EngineCompanyModel = Depends(resolve_accounts_company),
    service: InvoiceDraftService = Depends(get_invoice_draft_service),
) -> PaginatedResponse[InvoiceResponse]:
    """Return the company's invoices, cursor-paginated, newest first.

    An invalid ``type`` / ``state`` filter is a 400
    ``LEDGER_INVALID_INVOICE`` (the engine port validates filters
    before delegating).
    """
    reject_unknown_params(request, _INVOICE_LIST_PARAMS)
    field, reverse = parse_sort_param(sort, _INVOICE_SORT_KEYS, default="date")
    if sort is None or sort == "":
        # Default order is newest first, matching the port's order; an
        # explicit sort=date sorts ascending (oldest first).
        reverse = True
    ref = company_ref_for_row(row)
    with translate_errors():
        views = service.list_invoices(ref, type, state)
    items = [InvoiceResponse.from_view(view, row.id) for view in views]
    page, next_cursor = paginate(
        items,
        key_fn=_INVOICE_SORT_KEYS[field],
        tie_fn=lambda item: str(item.id),
        limit=limit,
        cursor=cursor,
        reverse=reverse,
    )
    return PaginatedResponse(items=page, next_cursor=next_cursor)


@router.get(
    "/invoices/{invoice_id}",
    response_model=InvoiceResponse,
    summary="Get one invoice",
)
def get_invoice(
    invoice_id: int,
    client: AuthenticatedClient = Depends(require_scopes("invoice:read")),
    session: Session = Depends(get_db_session),
    service: InvoiceDraftService = Depends(get_invoice_draft_service),
) -> InvoiceResponse:
    """Return one invoice of a company in scope.

    Unknown invoice (or an invoice of a company with no mapping in
    this tenant) → 404 ``LEDGER_INVOICE_NOT_FOUND``.
    """
    with translate_errors():
        view = service.get_invoice(InvoiceRef(invoice_id))
    row = require_visible_company(
        session,
        client,
        view.company,
        invoice_id,
        resource_kind="Invoice",
        error_code="LEDGER_INVOICE_NOT_FOUND",
    )
    return InvoiceResponse.from_view(view, row.id)


@router.post(
    "/invoices/drafts",
    response_model=InvoiceResponse,
    status_code=201,
    summary="Create an invoice draft",
)
def create_invoice_draft(
    body: CreateInvoiceDraftRequest,
    idempotency_key: str | None = Header(
        default=None,
        alias=IDEMPOTENCY_KEY_HEADER,
        description="Unique key for this mutation; retries replay the original response",
    ),
    client: AuthenticatedClient = Depends(require_scopes("invoice:draft:create")),
    session: Session = Depends(get_db_session),
    service: InvoiceDraftService = Depends(get_invoice_draft_service),
) -> InvoiceResponse | JSONResponse:
    """Create a draft invoice in the engine.

    The company comes from the request body. The draft is read back
    after creation, so the response is the engine's own view of it
    (with the engine-computed totals).
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
    created_ref: InvoiceRef | None = None
    try:
        invoice_ref = service.create_draft(
            ref,
            body.invoice_type,
            PartyRef(body.party_id),
            FiscalDate.from_gregorian(body.date),
            _invoice_lines(body.lines),
            description=body.description,
            reference=body.reference,
            journal=JournalRef(body.journal_id) if body.journal_id is not None else None,
            account=AccountRef(body.account_id) if body.account_id is not None else None,
            invoice_address=(
                PartyAddressRef(body.invoice_address_id)
                if body.invoice_address_id is not None
                else None
            ),
        )
        created_ref = invoice_ref
        view = service.get_invoice(invoice_ref)
    except HesabYarError as exc:
        raise fail_mutation(
            session,
            client,
            claim,
            exc,
            action=_OP_CREATE,
            resource_type="invoice_draft",
            resource_id=str(created_ref.engine_id) if created_ref is not None else None,
            company_external_id=row.external_company_id,
            tryton_reference=(
                f"account.invoice:{created_ref.engine_id}"
                if created_ref is not None
                else None
            ),
        ) from exc
    response = InvoiceResponse.from_view(view, row.id)
    complete_mutation(
        session,
        client,
        claim,
        response_status=201,
        response=response,
        response_reference=f"invoices/{view.ref.engine_id}",
        action=_OP_CREATE,
        resource_type="invoice_draft",
        resource_id=str(view.ref.engine_id),
        company_external_id=row.external_company_id,
        tryton_reference=f"account.invoice:{view.ref.engine_id}",
        metadata_after={
            "invoice_type": view.invoice_type,
            "total_amount": view.total_amount.amount,
            "line_count": len(view.lines),
        },
    )
    return response


@router.post(
    "/invoices/{invoice_id}/validate",
    response_model=ValidationResponse,
    summary="Validate an invoice draft",
)
def validate_invoice_draft(
    invoice_id: int,
    client: AuthenticatedClient = Depends(require_scopes("invoice:draft:create")),
    session: Session = Depends(get_db_session),
    service: InvoiceDraftService = Depends(get_invoice_draft_service),
) -> ValidationResponse:
    """Validate a draft invoice against the postability invariants.

    Read-only: never mutates the invoice and never calls the engine's
    ``validate_invoice`` transition (finalizing an invoice is a later
    story). Unknown invoice → 404 ``LEDGER_INVOICE_NOT_FOUND``.
    """
    with translate_errors():
        view = service.get_invoice(InvoiceRef(invoice_id))
    require_visible_company(
        session,
        client,
        view.company,
        invoice_id,
        resource_kind="Invoice",
        error_code="LEDGER_INVOICE_NOT_FOUND",
    )
    with translate_errors():
        summary = service.validate_invoice_draft(InvoiceRef(invoice_id))
    return ValidationResponse.from_summary(summary)
