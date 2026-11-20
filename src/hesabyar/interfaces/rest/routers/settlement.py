"""Settlement routes: gateway payments, tranches, exceptions, fee records.

US-521 (Phase 5-3 Wave A) — thin routes over the settlement services:
no derivation here, no state-machine logic — the services own those.

- ``POST /settlements/payments`` — record a gateway payment; the
  tranche schedule is derived from the channel's ACTIVE contract
  version and returned with the payment (201). A duplicate
  (channel, gateway transaction) is a 409.
- ``GET /settlements/payments`` / ``{payment_id}`` — payment reads;
  the detail read returns the payment with its full schedule.
- ``POST /settlements/payments/{payment_id}/cancel`` — cancel/refund:
  open tranches cancel; already-paid tranches become a negative
  adjustment (``refunds_deducted_irr``) + a ``cancelled_after_settlement``
  exception.
- ``POST /settlements/payments/{payment_id}/reassign-period`` —
  correct the sales period; open tranches re-derive, confirmed/
  received tranches are untouched, an informational
  ``period_reassigned`` exception is logged.
- ``POST /settlements/payments/bulk-rederive`` — re-derive open
  schedules from the ACTIVE contract version. Explicit user action
  only (actor + reason mandatory) + one audit-log entry.
- ``GET /settlements/tranches`` / ``{tranche_id}`` — tranche reads
  (the cash calendar's raw material).
- ``POST /settlements/tranches/{tranche_id}/write-off`` — write off
  a tranche (human approval only: approver + reason mandatory,
  audit-logged).
- ``GET /settlements/exceptions`` / ``{exception_id}`` +
  ``POST .../resolve`` — the settlement exception queue (§3.7).
- ``GET /settlements/fee-records`` — expected vs actual fee + VAT (§3.6).
- ``POST /settlements/tick`` — run the daily tick now (W1:
  expected→due→overdue + late-tranche escalation).

US-522 (Phase 5-3 Wave B) — the §3.5 journals, posted through the
engine (Path 1b):

- ``POST /settlements/payments/{payment_id}/post-sale-fee`` — fee +
  fee VAT at sale (policy A); a zero-fee payment is a no-op.
- ``POST /settlements/tranches/{tranche_id}/post-clearing`` — the
  clearing transfer on confirmation (payment submit → process →
  succeed, clearing move posted, receivable lines reconciled).
- ``POST /settlements/tranches/{tranche_id}/post-bank-receipt`` —
  the bank receipt on receipt, gated on the batch tie-out.
- ``POST /settlements/payments/{payment_id}/post-fee-variance`` —
  a fee-variance adjustment (never silently absorbed; always a
  ``fee_mismatch`` exception with a reason).
- ``POST /settlements/payments/{payment_id}/apply-refund`` — a
  (partial) post-settlement refund (last open tranche first).
- ``GET /settlements/payments/{payment_id}/postings`` — the
  payment's posting ledger.

Every posting request carries the Tryton company id; the remaining
wiring resolves from the mapping center for the payment's channel
and fails closed (422 ``SETTLEMENT_WIRING_UNRESOLVED``) on a
missing mapping.
US-523 (Phase 5-3 Wave B) — settlement file import + matching +
mandatory tie-out:

- ``POST /settlements/imports`` — upload a gateway settlement file
  (base64 JSON, like the bank imports); parse + dry-run preview +
  a ``MANUAL_IMPORT_REVIEW`` queue item. Re-importing the same file
  is a no-op (file-hash dedupe, ``is_duplicate``).
- ``GET /settlements/imports`` / ``{import_id}`` — import reads; the
  detail read returns the import with its rows and dry-run outcomes.
- ``POST /settlements/imports/{import_id}/approve`` — approve an
  awaiting-review import: authoritative re-match, one settlement
  event + one canonical ``payment.settlement.received`` outbox
  event, fee records reconciled with actuals.
- ``POST /settlements/imports/{import_id}/reject`` — reject an
  awaiting-review import (no events, no matching).
- ``POST /settlements/imports/{import_id}/tie-out`` — run the
  mandatory three-way tie-out for a batch; auto-posts the bank
  move and closes tranches ONLY when the tie-out holds AND the
  policy gate allows.
- ``POST /settlements/deposits`` — record a bank deposit; the C5
  confidence-ordered matching runs (exact reference → amount+date
  → amount-only suggestions → unmatched exception). A company id
  triggers an inline tie-out attempt for the matched batch.
- ``GET /settlements/deposits`` / ``{deposit_id}`` — deposit reads;
  ``unmatched_only`` filters to deposits still waiting for a file.
- ``POST /settlements/deposits/{deposit_id}/match`` — manually
  attach a deposit to a settlement event (operator decision).
- ``GET /settlements/events`` / ``{event_id}`` — settlement event reads.
- ``GET .../matches`` under tranches, events and deposits — match links.

Reads need ``settlement:read``; every mutation needs
``settlement:manage``. Everything is tenant/installation-scoped: a
row of another installation answers 404 as if it did not exist.

Channels and contracts are managed under ``/integrations`` (US-503);
this router reads them (channel_key filters) but never writes them.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable
from datetime import date

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.orm import Session

from hesabyar.application.commands.settlement import SettlementCommandService
from hesabyar.application.commands.settlement_import import SettlementImportService
from hesabyar.application.commands.settlement_posting import SettlementPostingService
from hesabyar.application.queries.settlement import SettlementQueryService
from hesabyar.domain.accounting.refs import CompanyRef
from hesabyar.domain.settlement.commands import (
    ApplyRefund,
    ApproveSettlementImport,
    CancelGatewayPayment,
    CreateGatewayPayment,
    ImportSettlementFile,
    MatchDepositManually,
    PostBankReceipt,
    PostFeeVariance,
    PostSaleFee,
    PostTrancheClearing,
    ReassignSalesPeriod,
    RecordBankDeposit,
    RecordTrancheWriteOff,
    RejectSettlementImport,
    ResolveSettlementException,
    RunSettlementTieOut,
)
from hesabyar.domain.settlement.models import PaymentSource, TrancheState
from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.interfaces.rest.dependencies import (
    company_ref_for_row,
    get_db_session,
    get_settlement_command_service,
    get_settlement_import_service,
    get_settlement_posting_service,
    get_settlement_query_service,
    require_company_row_by_id,
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
from hesabyar.interfaces.rest.schemas.settlement import (
    ApplyRefundRequest,
    ApplyRefundResponse,
    ApproveImportRequest,
    ApproveImportResponse,
    BankDepositResponse,
    BulkRederiveRequest,
    BulkRederiveResponse,
    CancelGatewayPaymentRequest,
    CancelGatewayPaymentResponse,
    CreateGatewayPaymentRequest,
    GatewayPaymentResponse,
    ImportSettlementFileRequest,
    ImportSettlementFileResponse,
    MatchDepositManuallyRequest,
    PaymentScheduleResponse,
    PostBankReceiptRequest,
    PostBankReceiptResponse,
    PostFeeVarianceRequest,
    PostFeeVarianceResponse,
    PostSaleFeeRequest,
    PostSaleFeeResponse,
    PostTrancheClearingRequest,
    PostTrancheClearingResponse,
    ReassignSalesPeriodRequest,
    ReassignSalesPeriodResponse,
    RecordDepositRequest,
    RecordDepositResponse,
    RecordPaymentResponse,
    RejectImportRequest,
    ResolveSettlementExceptionRequest,
    RunTieOutRequest,
    SettlementEventResponse,
    SettlementExceptionResponse,
    SettlementFeeRecordResponse,
    SettlementFileImportDetailResponse,
    SettlementFileImportResponse,
    SettlementMatchResponse,
    SettlementPostingResponse,
    SettlementTrancheResponse,
    TickResponse,
    TieOutRunResponse,
    WriteOffTrancheRequest,
)

router = APIRouter(tags=["settlement"])

_PAYMENT_SOURCES = {source.value for source in PaymentSource}
_TRANCHE_STATES = {state.value for state in TrancheState}


def _parse_payment_source(value: str) -> PaymentSource:
    """Parse a payment source, failing loudly on unknown labels."""
    if value not in _PAYMENT_SOURCES:
        raise ApiError(
            status_code=400,
            code="INVALID_FILTER",
            detail=(
                f"Unknown source {value!r}. Allowed: {', '.join(sorted(_PAYMENT_SOURCES))}."
            ),
        )
    return PaymentSource(value)


def _parse_tranche_state(value: str | None) -> TrancheState | None:
    """Parse a tranche-state filter, failing loudly on unknown labels."""
    if value is None:
        return None
    if value not in _TRANCHE_STATES:
        raise ApiError(
            status_code=400,
            code="INVALID_FILTER",
            detail=(
                f"Unknown tranche state {value!r}. "
                f"Allowed: {', '.join(sorted(_TRANCHE_STATES))}."
            ),
        )
    return TrancheState(value)


# -- gateway payments -----------------------------------------------------


_PAYMENTS_SORT_KEYS: dict[str, Callable[[GatewayPaymentResponse], str]] = {
    "gateway_confirmed_at": lambda item: item.gateway_confirmed_at.isoformat(),
    "sales_period_jalali": lambda item: item.sales_period_jalali,
}


def _payments_tie_key(item: GatewayPaymentResponse) -> str:
    """Tiebreaker for payment pagination: row id (total order)."""
    return item.id


_PAYMENTS_QUERY_PARAMS = {
    "channel_key",
    "status",
    "sales_period",
    "sort",
    "limit",
    "cursor",
}


@router.get(
    "/settlements/payments",
    response_model=PaginatedResponse[GatewayPaymentResponse],
    summary="List gateway payments in this installation",
)
def list_gateway_payments(
    request: Request,
    channel_key: str | None = Query(default=None, description="Filter by channel slug"),
    status: str | None = Query(default=None, description="Filter by payment status"),
    sales_period: str | None = Query(default=None, description="Filter by Jalali sales period 'YYYY/MM'"),
    sort: str | None = Query(
        default=None,
        description="Sort field: 'gateway_confirmed_at' (default) or 'sales_period_jalali'; prefix '-' for descending",
    ),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"),
    cursor: str | None = Query(default=None, description="Opaque cursor from a previous page's 'next_cursor'"),
    client: AuthenticatedClient = Depends(require_scopes("settlement:read")),
    service: SettlementQueryService = Depends(get_settlement_query_service),
) -> PaginatedResponse[GatewayPaymentResponse]:
    """Return the installation's gateway payments, cursor-paginated."""
    reject_unknown_params(request, _PAYMENTS_QUERY_PARAMS)
    field, reverse = parse_sort_param(sort, _PAYMENTS_SORT_KEYS, default="gateway_confirmed_at")
    with translate_errors():
        payments = service.list_payments(
            client.installation_id,
            channel_key=channel_key,
            status=status,
            sales_period_jalali=sales_period,
        )
    items = [GatewayPaymentResponse.from_payment(payment) for payment in payments]
    page, next_cursor = paginate(
        items,
        key_fn=_PAYMENTS_SORT_KEYS[field],
        tie_fn=_payments_tie_key,
        limit=limit,
        cursor=cursor,
        reverse=reverse,
    )
    return PaginatedResponse(items=page, next_cursor=next_cursor)


@router.post(
    "/settlements/payments",
    response_model=RecordPaymentResponse,
    status_code=201,
    summary="Record a gateway payment and derive its tranche schedule",
)
def record_gateway_payment(
    body: CreateGatewayPaymentRequest,
    client: AuthenticatedClient = Depends(require_scopes("settlement:manage")),
    session: Session = Depends(get_db_session),
    service: SettlementCommandService = Depends(get_settlement_command_service),
) -> RecordPaymentResponse:
    """Record a payment; the schedule derives from the ACTIVE contract version.

    Duplicate (channel, gateway transaction) → 409
    ``SETTLEMENT_PAYMENT_EXISTS``. No active contract → 422
    ``SETTLEMENT_CONTRACT_MISSING``. No fee-VAT rate (contract has
    none, no override given) → 422 ``SETTLEMENT_FEE_VAT_RATE_UNKNOWN``.
    """
    source = _parse_payment_source(body.source)
    with translate_errors():
        result = service.record_payment(
            CreateGatewayPayment(
                installation_id=client.installation_id,
                channel_key=body.channel_key,
                gateway_transaction_id=body.gateway_transaction_id,
                gross_irr=body.gross_irr,
                gateway_confirmed_at=body.gateway_confirmed_at,
                source=source,
                store_order_id=body.store_order_id,
                store_payment_id=body.store_payment_id,
                tryton_invoice_id=body.tryton_invoice_id,
                tryton_journal_id=body.tryton_journal_id,
                customer_party_id=body.customer_party_id,
                canonical_event_id=body.canonical_event_id,
                fee_vat_rate_override=body.fee_vat_rate_override,
            )
        )
    session.commit()
    return RecordPaymentResponse.from_result(result)


@router.post(
    "/settlements/payments/bulk-rederive",
    response_model=BulkRederiveResponse,
    summary="Re-derive open schedules from the ACTIVE contract version",
)
def bulk_rederive_payments(
    body: BulkRederiveRequest,
    client: AuthenticatedClient = Depends(require_scopes("settlement:manage")),
    session: Session = Depends(get_db_session),
    service: SettlementCommandService = Depends(get_settlement_command_service),
) -> BulkRederiveResponse:
    """Re-derive schedules explicitly (actor + reason mandatory, audit-logged).

    Only payments whose tranches are ALL still open are re-derived;
    payments with any confirmed/received tranche are reported as
    skipped, never silently rewritten.
    """
    with translate_errors():
        result = service.bulk_rederive(
            client.installation_id,
            tenant_id=client.tenant_id,
            api_client_id=client.client_id,
            channel_key=body.channel_key,
            sales_period_jalali=body.sales_period_jalali,
            actor=body.actor,
            reason=body.reason,
        )
    session.commit()
    return BulkRederiveResponse.from_result(result)


@router.get(
    "/settlements/payments/{payment_id}",
    response_model=PaymentScheduleResponse,
    summary="Get one gateway payment with its full tranche schedule",
)
def get_gateway_payment(
    payment_id: str,
    client: AuthenticatedClient = Depends(require_scopes("settlement:read")),
    service: SettlementQueryService = Depends(get_settlement_query_service),
) -> PaymentScheduleResponse:
    """Return a payment with its tranches, by index."""
    with translate_errors():
        schedule = service.get_payment_schedule(client.installation_id, payment_id)
    return PaymentScheduleResponse(
        payment=GatewayPaymentResponse.from_payment(schedule.payment),
        tranches=[SettlementTrancheResponse.from_tranche(t) for t in schedule.tranches],
    )


@router.post(
    "/settlements/payments/{payment_id}/cancel",
    response_model=CancelGatewayPaymentResponse,
    summary="Cancel/refund a gateway payment",
)
def cancel_gateway_payment(
    payment_id: str,
    body: CancelGatewayPaymentRequest,
    client: AuthenticatedClient = Depends(require_scopes("settlement:manage")),
    session: Session = Depends(get_db_session),
    service: SettlementCommandService = Depends(get_settlement_command_service),
) -> CancelGatewayPaymentResponse:
    """Cancel a payment: open tranches cancel; already-paid tranches
    become a negative adjustment on later settlements."""
    with translate_errors():
        result = service.cancel_payment(
            client.installation_id,
            CancelGatewayPayment(payment_id=payment_id, reason=body.reason, actor=body.actor),
            tenant_id=client.tenant_id,
        )
    session.commit()
    return CancelGatewayPaymentResponse.from_result(result)


@router.post(
    "/settlements/payments/{payment_id}/reassign-period",
    response_model=ReassignSalesPeriodResponse,
    summary="Correct a payment's sales period",
)
def reassign_sales_period(
    payment_id: str,
    body: ReassignSalesPeriodRequest,
    client: AuthenticatedClient = Depends(require_scopes("settlement:manage")),
    session: Session = Depends(get_db_session),
    service: SettlementCommandService = Depends(get_settlement_command_service),
) -> ReassignSalesPeriodResponse:
    """Correct the sales period: open tranches re-derive, confirmed/
    received tranches are untouched, an informational exception is logged."""
    with translate_errors():
        result = service.reassign_period(
            client.installation_id,
            ReassignSalesPeriod(
                payment_id=payment_id,
                new_sales_period_jalali=body.new_sales_period_jalali,
                reason=body.reason,
                actor=body.actor,
            ),
            tenant_id=client.tenant_id,
        )
    session.commit()
    return ReassignSalesPeriodResponse.from_result(result)


# -- tranches -------------------------------------------------------------


_TRANCHES_SORT_KEYS: dict[str, Callable[[SettlementTrancheResponse], str]] = {
    "expected_date": lambda item: item.expected_date.isoformat(),
    "sales_period_jalali": lambda item: item.sales_period_jalali,
}


def _tranches_tie_key(item: SettlementTrancheResponse) -> str:
    """Tiebreaker for tranche pagination: row id (total order)."""
    return item.id


_TRANCHES_QUERY_PARAMS = {
    "channel_key",
    "state",
    "sales_period",
    "due_from",
    "due_to",
    "sort",
    "limit",
    "cursor",
}


@router.get(
    "/settlements/tranches",
    response_model=PaginatedResponse[SettlementTrancheResponse],
    summary="List settlement tranches in this installation",
)
def list_settlement_tranches(
    request: Request,
    channel_key: str | None = Query(default=None, description="Filter by channel slug"),
    state: str | None = Query(default=None, description="Filter by tranche state (§3.3)"),
    sales_period: str | None = Query(default=None, description="Filter by Jalali sales period 'YYYY/MM'"),
    due_from: date | None = Query(default=None, description="Filter to tranches due on/after this date (Gregorian)"),
    due_to: date | None = Query(default=None, description="Filter to tranches due on/before this date (Gregorian)"),
    sort: str | None = Query(
        default=None,
        description="Sort field: 'expected_date' (default) or 'sales_period_jalali'; prefix '-' for descending",
    ),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"),
    cursor: str | None = Query(default=None, description="Opaque cursor from a previous page's 'next_cursor'"),
    client: AuthenticatedClient = Depends(require_scopes("settlement:read")),
    service: SettlementQueryService = Depends(get_settlement_query_service),
) -> PaginatedResponse[SettlementTrancheResponse]:
    """Return the installation's tranches, cursor-paginated."""
    reject_unknown_params(request, _TRANCHES_QUERY_PARAMS)
    field, reverse = parse_sort_param(sort, _TRANCHES_SORT_KEYS, default="expected_date")
    parsed_state = _parse_tranche_state(state)
    with translate_errors():
        tranches = service.list_tranches(
            client.installation_id,
            channel_key=channel_key,
            state=parsed_state,
            sales_period_jalali=sales_period,
            due_from=due_from,
            due_to=due_to,
        )
    items = [SettlementTrancheResponse.from_tranche(tranche) for tranche in tranches]
    page, next_cursor = paginate(
        items,
        key_fn=_TRANCHES_SORT_KEYS[field],
        tie_fn=_tranches_tie_key,
        limit=limit,
        cursor=cursor,
        reverse=reverse,
    )
    return PaginatedResponse(items=page, next_cursor=next_cursor)


@router.get(
    "/settlements/tranches/{tranche_id}",
    response_model=SettlementTrancheResponse,
    summary="Get one settlement tranche",
)
def get_settlement_tranche(
    tranche_id: str,
    client: AuthenticatedClient = Depends(require_scopes("settlement:read")),
    service: SettlementQueryService = Depends(get_settlement_query_service),
) -> SettlementTrancheResponse:
    """Return one tranche by id."""
    with translate_errors():
        tranche = service.get_tranche(client.installation_id, tranche_id)
    return SettlementTrancheResponse.from_tranche(tranche)


@router.post(
    "/settlements/tranches/{tranche_id}/write-off",
    response_model=SettlementTrancheResponse,
    summary="Write off one tranche (human approval only)",
)
def write_off_settlement_tranche(
    tranche_id: str,
    body: WriteOffTrancheRequest,
    client: AuthenticatedClient = Depends(require_scopes("settlement:manage")),
    session: Session = Depends(get_db_session),
    service: SettlementCommandService = Depends(get_settlement_command_service),
) -> SettlementTrancheResponse:
    """Write off a tranche: approver + reason mandatory, audit-logged."""
    with translate_errors():
        tranche = service.write_off_tranche(
            client.installation_id,
            RecordTrancheWriteOff(
                tranche_id=tranche_id, approved_by=body.approved_by, reason=body.reason
            ),
            tenant_id=client.tenant_id,
            api_client_id=client.client_id,
        )
    session.commit()
    return SettlementTrancheResponse.from_tranche(tranche)


# -- exceptions -----------------------------------------------------------


_EXCEPTIONS_SORT_KEYS: dict[str, Callable[[SettlementExceptionResponse], str]] = {
    "created_at": lambda item: item.created_at.isoformat() if item.created_at else "",
}


def _exceptions_tie_key(item: SettlementExceptionResponse) -> str:
    """Tiebreaker for exception pagination: row id (total order)."""
    return item.id


_EXCEPTIONS_QUERY_PARAMS = {"exception_type", "status", "tranche_id", "sort", "limit", "cursor"}


@router.get(
    "/settlements/exceptions",
    response_model=PaginatedResponse[SettlementExceptionResponse],
    summary="List settlement exceptions in this installation",
)
def list_settlement_exceptions(
    request: Request,
    exception_type: str | None = Query(default=None, description="Filter by exception type (§3.7)"),
    status: str | None = Query(default=None, description="Filter by exception status"),
    tranche_id: str | None = Query(default=None, description="Filter by tranche id"),
    sort: str | None = Query(
        default=None,
        description="Sort field: 'created_at' (default); prefix '-' for descending",
    ),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"),
    cursor: str | None = Query(default=None, description="Opaque cursor from a previous page's 'next_cursor'"),
    client: AuthenticatedClient = Depends(require_scopes("settlement:read")),
    service: SettlementQueryService = Depends(get_settlement_query_service),
) -> PaginatedResponse[SettlementExceptionResponse]:
    """Return the installation's settlement exceptions, cursor-paginated."""
    reject_unknown_params(request, _EXCEPTIONS_QUERY_PARAMS)
    field, reverse = parse_sort_param(sort, _EXCEPTIONS_SORT_KEYS, default="created_at")
    with translate_errors():
        exceptions = service.list_exceptions(
            client.installation_id,
            exception_type=exception_type,
            status=status,
            tranche_id=tranche_id,
        )
    items = [SettlementExceptionResponse.from_exception(exc) for exc in exceptions]
    page, next_cursor = paginate(
        items,
        key_fn=_EXCEPTIONS_SORT_KEYS[field],
        tie_fn=_exceptions_tie_key,
        limit=limit,
        cursor=cursor,
        reverse=reverse,
    )
    return PaginatedResponse(items=page, next_cursor=next_cursor)


@router.get(
    "/settlements/exceptions/{exception_id}",
    response_model=SettlementExceptionResponse,
    summary="Get one settlement exception",
)
def get_settlement_exception(
    exception_id: str,
    client: AuthenticatedClient = Depends(require_scopes("settlement:read")),
    service: SettlementQueryService = Depends(get_settlement_query_service),
) -> SettlementExceptionResponse:
    """Return one exception by id."""
    with translate_errors():
        exc = service.get_exception(client.installation_id, exception_id)
    return SettlementExceptionResponse.from_exception(exc)


@router.post(
    "/settlements/exceptions/{exception_id}/resolve",
    response_model=SettlementExceptionResponse,
    summary="Resolve an open settlement exception",
)
def resolve_settlement_exception(
    exception_id: str,
    body: ResolveSettlementExceptionRequest,
    client: AuthenticatedClient = Depends(require_scopes("settlement:manage")),
    session: Session = Depends(get_db_session),
    service: SettlementCommandService = Depends(get_settlement_command_service),
) -> SettlementExceptionResponse:
    """Resolve an exception: resolver + time + note are recorded."""
    with translate_errors():
        exc = service.resolve_exception(
            client.installation_id,
            ResolveSettlementException(
                exception_id=exception_id,
                resolution=body.resolution,
                resolved_by=body.resolved_by,
                note=body.note,
            ),
        )
    session.commit()
    return SettlementExceptionResponse.from_exception(exc)


# -- fee records ------------------------------------------------------------


_FEE_RECORDS_SORT_KEYS: dict[str, Callable[[SettlementFeeRecordResponse], str]] = {
    "created_at": lambda item: item.created_at.isoformat() if item.created_at else "",
}


def _fee_records_tie_key(item: SettlementFeeRecordResponse) -> str:
    """Tiebreaker for fee-record pagination: row id (total order)."""
    return item.id


_FEE_RECORDS_QUERY_PARAMS = {"gateway_payment_id", "channel_key", "sort", "limit", "cursor"}


@router.get(
    "/settlements/fee-records",
    response_model=PaginatedResponse[SettlementFeeRecordResponse],
    summary="List settlement fee records in this installation",
)
def list_settlement_fee_records(
    request: Request,
    gateway_payment_id: str | None = Query(default=None, description="Filter by gateway payment id"),
    channel_key: str | None = Query(default=None, description="Filter by channel slug"),
    sort: str | None = Query(
        default=None,
        description="Sort field: 'created_at' (default); prefix '-' for descending",
    ),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"),
    cursor: str | None = Query(default=None, description="Opaque cursor from a previous page's 'next_cursor'"),
    client: AuthenticatedClient = Depends(require_scopes("settlement:read")),
    service: SettlementQueryService = Depends(get_settlement_query_service),
) -> PaginatedResponse[SettlementFeeRecordResponse]:
    """Return the installation's fee records, cursor-paginated."""
    reject_unknown_params(request, _FEE_RECORDS_QUERY_PARAMS)
    field, reverse = parse_sort_param(sort, _FEE_RECORDS_SORT_KEYS, default="created_at")
    with translate_errors():
        records = service.list_fee_records(
            client.installation_id,
            gateway_payment_id=gateway_payment_id,
            channel_key=channel_key,
        )
    items = [SettlementFeeRecordResponse.from_fee_record(record) for record in records]
    page, next_cursor = paginate(
        items,
        key_fn=_FEE_RECORDS_SORT_KEYS[field],
        tie_fn=_fee_records_tie_key,
        limit=limit,
        cursor=cursor,
        reverse=reverse,
    )
    return PaginatedResponse(items=page, next_cursor=next_cursor)


# -- daily tick -------------------------------------------------------------


@router.post(
    "/settlements/tick",
    response_model=TickResponse,
    summary="Run the daily settlement tick now (W1)",
)
def run_settlement_tick(
    client: AuthenticatedClient = Depends(require_scopes("settlement:manage")),
    session: Session = Depends(get_db_session),
    service: SettlementCommandService = Depends(get_settlement_command_service),
) -> TickResponse:
    """Run the daily tick now: expected→due→overdue + late-tranche
    escalation, across all installations (the scheduled job does the
    same every morning)."""
    with translate_errors():
        report = service.run_daily_tick(datetime.date.today())
    session.commit()
    return TickResponse.from_report(report)


# -- settlement postings (US-522) -------------------------------------------


@router.post(
    "/settlements/payments/{payment_id}/post-sale-fee",
    response_model=PostSaleFeeResponse,
    summary="Recognise a payment's fee + fee VAT at sale (policy A)",
)
def post_sale_fee(
    payment_id: str,
    body: PostSaleFeeRequest,
    client: AuthenticatedClient = Depends(require_scopes("settlement:manage")),
    session: Session = Depends(get_db_session),
    service: SettlementPostingService = Depends(get_settlement_posting_service),
) -> PostSaleFeeResponse:
    """Post the fee journal: Dr fee expense + Dr fee VAT / Cr gateway
    receivable, the receivable leg split per tranche. Under policy B
    (fee at settlement) this refuses with 422
    ``SETTLEMENT_POSTING_PRECONDITION``; a second post is a 409
    ``SETTLEMENT_POSTING_EXISTS``; a zero-fee payment is a no-op
    (posting null)."""
    with translate_errors():
        wiring = service.resolve_wiring_for_payment(
            client.installation_id, payment_id, CompanyRef(body.company_id)
        )
        result = service.post_sale_fee(
            client.installation_id,
            PostSaleFee(
                payment_id=payment_id,
                wiring=wiring,
                date=body.journal_date,
                actor=body.actor,
            ),
        )
    session.commit()
    return PostSaleFeeResponse.from_result(result)


@router.post(
    "/settlements/tranches/{tranche_id}/post-clearing",
    response_model=PostTrancheClearingResponse,
    summary="Post a confirmed tranche's clearing transfer (Path 1b)",
)
def post_tranche_clearing(
    tranche_id: str,
    body: PostTrancheClearingRequest,
    client: AuthenticatedClient = Depends(require_scopes("settlement:manage")),
    session: Session = Depends(get_db_session),
    service: SettlementPostingService = Depends(get_settlement_posting_service),
) -> PostTrancheClearingResponse:
    """Drive the tranche's payment submit → process → succeed with the
    payout date as the clearing date, post the clearing move, and
    reconcile the tranche's receivable lines once fully cleared. The
    tranche must be confirmed (the gateway file confirms first);
    under policy B the tranche's fee share is recognised first."""
    with translate_errors():
        wiring = service.resolve_wiring_for_tranche(
            client.installation_id, tranche_id, CompanyRef(body.company_id)
        )
        result = service.post_tranche_clearing(
            client.installation_id,
            PostTrancheClearing(
                tranche_id=tranche_id,
                wiring=wiring,
                payout_date=body.payout_date,
                actor=body.actor,
            ),
        )
    session.commit()
    return PostTrancheClearingResponse.from_result(result)


@router.post(
    "/settlements/tranches/{tranche_id}/post-bank-receipt",
    response_model=PostBankReceiptResponse,
    summary="Post a received tranche's bank receipt (after tie-out)",
)
def post_bank_receipt(
    tranche_id: str,
    body: PostBankReceiptRequest,
    client: AuthenticatedClient = Depends(require_scopes("settlement:manage")),
    session: Session = Depends(get_db_session),
    service: SettlementPostingService = Depends(get_settlement_posting_service),
) -> PostBankReceiptResponse:
    """Post Dr bank / Cr clearing for the newly received amount and
    reconcile the clearing legs once fully received. Refuses with
    422 ``SETTLEMENT_TIE_OUT_FAILED`` unless the settlement event's
    batch ties out, and with 422
    ``SETTLEMENT_POSTING_PRECONDITION`` unless the tranche's matched
    deposits cover the receipt. A fully received tranche settles."""
    with translate_errors():
        wiring = service.resolve_wiring_for_tranche(
            client.installation_id, tranche_id, CompanyRef(body.company_id)
        )
        result = service.post_bank_receipt(
            client.installation_id,
            PostBankReceipt(
                tranche_id=tranche_id,
                wiring=wiring,
                settlement_event_id=body.settlement_event_id,
                deposit_date=body.deposit_date,
                actor=body.actor,
            ),
        )
    session.commit()
    return PostBankReceiptResponse.from_result(result)


@router.post(
    "/settlements/payments/{payment_id}/post-fee-variance",
    response_model=PostFeeVarianceResponse,
    summary="Post a fee-variance adjustment move",
)
def post_fee_variance(
    payment_id: str,
    body: PostFeeVarianceRequest,
    client: AuthenticatedClient = Depends(require_scopes("settlement:manage")),
    session: Session = Depends(get_db_session),
    service: SettlementPostingService = Depends(get_settlement_posting_service),
) -> PostFeeVarianceResponse:
    """Post the difference between the expected and the real fee (+
    VAT) as an adjustment move — never silently absorbed — and open
    a ``fee_mismatch`` exception with the reason. A zero variance is
    a no-op (posting null)."""
    with translate_errors():
        wiring = service.resolve_wiring_for_payment(
            client.installation_id, payment_id, CompanyRef(body.company_id)
        )
        result = service.post_fee_variance(
            client.installation_id,
            PostFeeVariance(
                payment_id=payment_id,
                wiring=wiring,
                actual_fee_irr=body.actual_fee_irr,
                actual_vat_irr=body.actual_vat_irr,
                reason=body.reason,
                date=body.journal_date,
                tranche_id=body.tranche_id,
                event_ref=body.event_ref,
                actor=body.actor,
            ),
            tenant_id=client.tenant_id,
            api_client_id=client.client_id,
        )
    session.commit()
    return PostFeeVarianceResponse.from_result(result)


@router.post(
    "/settlements/payments/{payment_id}/apply-refund",
    response_model=ApplyRefundResponse,
    summary="Apply a (partial) post-settlement refund",
)
def apply_refund(
    payment_id: str,
    body: ApplyRefundRequest,
    client: AuthenticatedClient = Depends(require_scopes("settlement:manage")),
    session: Session = Depends(get_db_session),
    service: SettlementPostingService = Depends(get_settlement_posting_service),
) -> ApplyRefundResponse:
    """Apply a refund: deducted first from the payment's open
    unreached tranches (last tranche backwards); any excess becomes a
    negative adjustment (``refunds_deducted_irr``) on a later
    settlement, with a ``cancelled_after_settlement`` exception.
    Confirmed/received tranches are untouched."""
    with translate_errors():
        wiring = service.resolve_wiring_for_payment(
            client.installation_id, payment_id, CompanyRef(body.company_id)
        )
        result = service.apply_refund(
            client.installation_id,
            ApplyRefund(
                payment_id=payment_id,
                wiring=wiring,
                refund_amount_irr=body.refund_amount_irr,
                reason=body.reason,
                date=body.journal_date,
                event_ref=body.event_ref,
                actor=body.actor,
            ),
            tenant_id=client.tenant_id,
            api_client_id=client.client_id,
        )
    session.commit()
    return ApplyRefundResponse.from_result(result)


@router.get(
    "/settlements/payments/{payment_id}/postings",
    response_model=list[SettlementPostingResponse],
    summary="List a payment's settlement postings",
)
def list_payment_postings(
    payment_id: str,
    client: AuthenticatedClient = Depends(require_scopes("settlement:read")),
    service: SettlementQueryService = Depends(get_settlement_query_service),
) -> list[SettlementPostingResponse]:
    """Return the payment's posting ledger, oldest first."""
    with translate_errors():
        postings = service.list_postings_for_payment(client.installation_id, payment_id)
    return [SettlementPostingResponse.from_posting(posting) for posting in postings]
# -- settlement file imports (US-523) -----------------------------------------


_IMPORTS_SORT_KEYS: dict[str, Callable[[SettlementFileImportResponse], str]] = {
    "created_at": lambda item: item.created_at.isoformat() if item.created_at else "",
    "period_jalali": lambda item: item.period_jalali,
}


def _imports_tie_key(item: SettlementFileImportResponse) -> str:
    """Tiebreaker for import pagination: row id (total order)."""
    return item.id


_IMPORTS_QUERY_PARAMS = {"channel_key", "status", "sort", "limit", "cursor"}


@router.post(
    "/settlements/imports",
    response_model=ImportSettlementFileResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Import a gateway settlement file (parse + dry-run preview)",
)
def import_settlement_file(
    body: ImportSettlementFileRequest,
    client: AuthenticatedClient = Depends(require_scopes("settlement:manage")),
    session: Session = Depends(get_db_session),
    service: SettlementImportService = Depends(get_settlement_import_service),
    query: SettlementQueryService = Depends(get_settlement_query_service),
) -> ImportSettlementFileResponse:
    """Parse an uploaded settlement file into an awaiting-review import.

    The dry-run preview (accounting-effect totals + per-row match
    outcomes) is computed, and a ``MANUAL_IMPORT_REVIEW`` queue item
    is filed; nothing is confirmed until a human approves. Net Sale
    and totals rows are dropped at parse and logged. Re-importing
    the same file is a no-op (file-hash dedupe, ``is_duplicate``).
    An unreadable file, a missing required column or a file over
    the 2,000-row cap is a 422 ``SETTLEMENT_IMPORT_INVALID``.
    """
    with translate_errors():
        result = service.import_file(
            ImportSettlementFile(
                installation_id=client.installation_id,
                channel_key=body.channel_key,
                file_type=body.file_type,
                period_jalali=body.period_jalali,
                filename=body.filename,
                content=body.content_bytes(),
                currency_unit=body.currency_unit,
                columns=body.columns,
                date_kind=body.date_kind,
                header_row=body.header_row,
                uploaded_by=client.client_name,
                actor=body.actor or client.client_name,
            ),
            tenant_id=client.tenant_id,
        )
        detail = query.get_file_import(
            client.installation_id, result.file_import.id
        )
    session.commit()
    return ImportSettlementFileResponse.from_result(result, list(detail.rows))


@router.get(
    "/settlements/imports",
    response_model=PaginatedResponse[SettlementFileImportResponse],
    summary="List settlement file imports in this installation",
)
def list_settlement_imports(
    request: Request,
    channel_key: str | None = Query(default=None, description="Filter by channel slug"),
    status: str | None = Query(
        default=None,
        description="Filter by import status (awaiting_review / approved / rejected / exception / settled)",
    ),
    sort: str | None = Query(
        default=None,
        description="Sort field: 'created_at' (default) or 'period_jalali'; prefix '-' for descending",
    ),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"),
    cursor: str | None = Query(default=None, description="Opaque cursor from a previous page's 'next_cursor'"),
    client: AuthenticatedClient = Depends(require_scopes("settlement:read")),
    service: SettlementQueryService = Depends(get_settlement_query_service),
) -> PaginatedResponse[SettlementFileImportResponse]:
    """Return the installation's file imports, cursor-paginated."""
    reject_unknown_params(request, _IMPORTS_QUERY_PARAMS)
    field, reverse = parse_sort_param(sort, _IMPORTS_SORT_KEYS, default="created_at")
    with translate_errors():
        imports = service.list_file_imports(
            client.installation_id, channel_key=channel_key, status=status
        )
    items = [SettlementFileImportResponse.from_import(f) for f in imports]
    page, next_cursor = paginate(
        items,
        key_fn=_IMPORTS_SORT_KEYS[field],
        tie_fn=_imports_tie_key,
        limit=limit,
        cursor=cursor,
        reverse=reverse,
    )
    return PaginatedResponse(items=page, next_cursor=next_cursor)


@router.get(
    "/settlements/imports/{import_id}",
    response_model=SettlementFileImportDetailResponse,
    summary="Get one settlement file import with its rows",
)
def get_settlement_import(
    import_id: str,
    client: AuthenticatedClient = Depends(require_scopes("settlement:read")),
    service: SettlementQueryService = Depends(get_settlement_query_service),
) -> SettlementFileImportDetailResponse:
    """Return one import with its parsed rows and dry-run outcomes."""
    with translate_errors():
        detail = service.get_file_import(client.installation_id, import_id)
    return SettlementFileImportDetailResponse.from_detail(
        detail.file_import, list(detail.rows)
    )


@router.post(
    "/settlements/imports/{import_id}/approve",
    response_model=ApproveImportResponse,
    summary="Approve an awaiting-review import (authoritative re-match)",
)
def approve_settlement_import(
    import_id: str,
    body: ApproveImportRequest,
    client: AuthenticatedClient = Depends(require_scopes("settlement:manage")),
    session: Session = Depends(get_db_session),
    service: SettlementImportService = Depends(get_settlement_import_service),
) -> ApproveImportResponse:
    """Approve an import: authoritative re-match (the preview is never
    trusted), one settlement event + one canonical
    ``payment.settlement.received`` outbox event, fee records
    reconciled with actuals, earlier unconfirmed deposits confirmed
    (C7). An import in any other state is a 409.
    """
    with translate_errors():
        result = service.approve_import(
            client.installation_id,
            ApproveSettlementImport(
                import_id=import_id,
                actor=body.actor or client.client_name,
            ),
            tenant_id=client.tenant_id,
        )
    session.commit()
    return ApproveImportResponse.from_result(result)


@router.post(
    "/settlements/imports/{import_id}/reject",
    response_model=SettlementFileImportResponse,
    summary="Reject an awaiting-review import",
)
def reject_settlement_import(
    import_id: str,
    body: RejectImportRequest,
    client: AuthenticatedClient = Depends(require_scopes("settlement:manage")),
    session: Session = Depends(get_db_session),
    service: SettlementImportService = Depends(get_settlement_import_service),
) -> SettlementFileImportResponse:
    """Reject an import: no events, no matching; the linked review
    item is resolved with ``cancel_batch``. An import in any other
    state is a 409.
    """
    with translate_errors():
        file_import = service.reject_import(
            client.installation_id,
            RejectSettlementImport(
                import_id=import_id,
                actor=body.actor or client.client_name,
                reason=body.reason,
            ),
            tenant_id=client.tenant_id,
        )
    session.commit()
    return SettlementFileImportResponse.from_import(file_import)


@router.post(
    "/settlements/imports/{import_id}/tie-out",
    response_model=TieOutRunResponse,
    summary="Run the mandatory three-way tie-out for an import batch",
)
def run_import_tie_out(
    import_id: str,
    body: RunTieOutRequest,
    client: AuthenticatedClient = Depends(require_scopes("settlement:manage")),
    session: Session = Depends(get_db_session),
    service: SettlementImportService = Depends(get_settlement_import_service),
) -> TieOutRunResponse:
    """Run the mandatory tie-out: Σ confirmed tranche nets − Σ
    documented period deductions = Σ matched bank deposits (within
    ``tieout_tolerance_irr``). If the tie-out holds AND the policy
    gate allows, the bank move is auto-posted (clearing → bank) and
    the tranches close (settled). Otherwise the whole batch stays
    in exception (or draft-hold) and NO automatic posting happens.
    Without a company id the tie-out computes but never posts.
    """
    company_ref = None
    if body.company_id is not None:
        row = require_company_row_by_id(session, client, body.company_id)
        company_ref = company_ref_for_row(row)
    with translate_errors():
        result = service.run_tieout(
            client.installation_id,
            RunSettlementTieOut(
                import_id=import_id,
                actor=body.actor or client.client_name,
                tenant_id=client.tenant_id,
            ),
            company=company_ref,
        )
    session.commit()
    return TieOutRunResponse.from_result(result)


# -- settlement events (US-523) -----------------------------------------------


_EVENTS_SORT_KEYS: dict[str, Callable[[SettlementEventResponse], str]] = {
    "payout_date": lambda item: item.payout_date.isoformat(),
    "created_at": lambda item: item.created_at.isoformat() if item.created_at else "",
}


def _events_tie_key(item: SettlementEventResponse) -> str:
    """Tiebreaker for event pagination: row id (total order)."""
    return item.id


_EVENTS_QUERY_PARAMS = {"channel_key", "import_batch_id", "sort", "limit", "cursor"}


@router.get(
    "/settlements/events",
    response_model=PaginatedResponse[SettlementEventResponse],
    summary="List settlement events in this installation",
)
def list_settlement_events(
    request: Request,
    channel_key: str | None = Query(default=None, description="Filter by channel slug"),
    import_batch_id: str | None = Query(default=None, description="Filter by import batch id"),
    sort: str | None = Query(
        default=None,
        description="Sort field: 'payout_date' (default) or 'created_at'; prefix '-' for descending",
    ),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"),
    cursor: str | None = Query(default=None, description="Opaque cursor from a previous page's 'next_cursor'"),
    client: AuthenticatedClient = Depends(require_scopes("settlement:read")),
    service: SettlementQueryService = Depends(get_settlement_query_service),
) -> PaginatedResponse[SettlementEventResponse]:
    """Return the installation's settlement events, cursor-paginated."""
    reject_unknown_params(request, _EVENTS_QUERY_PARAMS)
    field, reverse = parse_sort_param(sort, _EVENTS_SORT_KEYS, default="payout_date")
    with translate_errors():
        events = service.list_settlement_events(
            client.installation_id,
            channel_key=channel_key,
            import_batch_id=import_batch_id,
        )
    items = [SettlementEventResponse.from_event(event) for event in events]
    page, next_cursor = paginate(
        items,
        key_fn=_EVENTS_SORT_KEYS[field],
        tie_fn=_events_tie_key,
        limit=limit,
        cursor=cursor,
        reverse=reverse,
    )
    return PaginatedResponse(items=page, next_cursor=next_cursor)


@router.get(
    "/settlements/events/{event_id}",
    response_model=SettlementEventResponse,
    summary="Get one settlement event",
)
def get_settlement_event(
    event_id: str,
    client: AuthenticatedClient = Depends(require_scopes("settlement:read")),
    service: SettlementQueryService = Depends(get_settlement_query_service),
) -> SettlementEventResponse:
    """Return one settlement event by id."""
    with translate_errors():
        event = service.get_settlement_event(client.installation_id, event_id)
    return SettlementEventResponse.from_event(event)


@router.get(
    "/settlements/events/{event_id}/matches",
    response_model=list[SettlementMatchResponse],
    summary="List a settlement event's match links",
)
def list_event_matches(
    event_id: str,
    client: AuthenticatedClient = Depends(require_scopes("settlement:read")),
    service: SettlementQueryService = Depends(get_settlement_query_service),
) -> list[SettlementMatchResponse]:
    """Return an event's match links (tranches + deposits)."""
    with translate_errors():
        matches = service.list_matches_for_event(client.installation_id, event_id)
    return [SettlementMatchResponse.from_match(match) for match in matches]


# -- bank deposits (US-523) ---------------------------------------------------


_DEPOSITS_SORT_KEYS: dict[str, Callable[[BankDepositResponse], str]] = {
    "deposit_date": lambda item: item.deposit_date.isoformat(),
    "created_at": lambda item: item.created_at.isoformat() if item.created_at else "",
}


def _deposits_tie_key(item: BankDepositResponse) -> str:
    """Tiebreaker for deposit pagination: row id (total order)."""
    return item.id


_DEPOSITS_QUERY_PARAMS = {"unmatched_only", "sort", "limit", "cursor"}


@router.post(
    "/settlements/deposits",
    response_model=RecordDepositResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Record a bank deposit and run the C5 matching",
)
def record_bank_deposit(
    body: RecordDepositRequest,
    client: AuthenticatedClient = Depends(require_scopes("settlement:manage")),
    session: Session = Depends(get_db_session),
    service: SettlementImportService = Depends(get_settlement_import_service),
) -> RecordDepositResponse:
    """Record a bank deposit; matching runs in confidence order:
    exact tracking reference in the narrative → auto-match (1.0);
    amount + date within the match window → auto-match if unique
    (0.95); amount-only → review queue with ranked suggestions
    (≤0.70); no candidate → ``deposit_without_file`` exception.
    With a company id, a matching batch is tied out inline.
    """
    company_ref = None
    if body.company_id is not None:
        row = require_company_row_by_id(session, client, body.company_id)
        company_ref = company_ref_for_row(row)
    with translate_errors():
        result = service.record_deposit(
            RecordBankDeposit(
                installation_id=client.installation_id,
                tenant_id=client.tenant_id,
                deposit_date=body.deposit_date,
                amount_irr=body.amount_irr,
                bank_account_ref=body.bank_account_ref,
                description=body.description,
                tracking_ref=body.tracking_ref,
                bank_import_row_id=body.bank_import_row_id,
                source=body.source,
                channel_key=body.channel_key,
                actor=body.actor or client.client_name,
            ),
            company=company_ref,
        )
    session.commit()
    return RecordDepositResponse.from_result(result)


@router.get(
    "/settlements/deposits",
    response_model=PaginatedResponse[BankDepositResponse],
    summary="List bank deposits in this installation",
)
def list_bank_deposits(
    request: Request,
    unmatched_only: bool = Query(
        default=False, description="Only deposits not yet matched to a settlement event"
    ),
    sort: str | None = Query(
        default=None,
        description="Sort field: 'deposit_date' (default) or 'created_at'; prefix '-' for descending",
    ),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"),
    cursor: str | None = Query(default=None, description="Opaque cursor from a previous page's 'next_cursor'"),
    client: AuthenticatedClient = Depends(require_scopes("settlement:read")),
    service: SettlementQueryService = Depends(get_settlement_query_service),
) -> PaginatedResponse[BankDepositResponse]:
    """Return the installation's bank deposits, cursor-paginated."""
    reject_unknown_params(request, _DEPOSITS_QUERY_PARAMS)
    field, reverse = parse_sort_param(sort, _DEPOSITS_SORT_KEYS, default="deposit_date")
    with translate_errors():
        deposits = service.list_bank_deposits(
            client.installation_id, unmatched_only=unmatched_only
        )
    items = [BankDepositResponse.from_deposit(deposit) for deposit in deposits]
    page, next_cursor = paginate(
        items,
        key_fn=_DEPOSITS_SORT_KEYS[field],
        tie_fn=_deposits_tie_key,
        limit=limit,
        cursor=cursor,
        reverse=reverse,
    )
    return PaginatedResponse(items=page, next_cursor=next_cursor)


@router.get(
    "/settlements/deposits/{deposit_id}",
    response_model=BankDepositResponse,
    summary="Get one bank deposit",
)
def get_bank_deposit(
    deposit_id: str,
    client: AuthenticatedClient = Depends(require_scopes("settlement:read")),
    service: SettlementQueryService = Depends(get_settlement_query_service),
) -> BankDepositResponse:
    """Return one bank deposit by id."""
    with translate_errors():
        deposit = service.get_bank_deposit(client.installation_id, deposit_id)
    return BankDepositResponse.from_deposit(deposit)


@router.get(
    "/settlements/deposits/{deposit_id}/matches",
    response_model=list[SettlementMatchResponse],
    summary="List a bank deposit's match links",
)
def list_deposit_matches(
    deposit_id: str,
    client: AuthenticatedClient = Depends(require_scopes("settlement:read")),
    service: SettlementQueryService = Depends(get_settlement_query_service),
) -> list[SettlementMatchResponse]:
    """Return a deposit's match links (events + tranches)."""
    with translate_errors():
        matches = service.list_matches_for_deposit(client.installation_id, deposit_id)
    return [SettlementMatchResponse.from_match(match) for match in matches]


@router.post(
    "/settlements/deposits/{deposit_id}/match",
    response_model=RecordDepositResponse,
    summary="Manually attach a deposit to a tranche",
)
def match_bank_deposit_manually(
    deposit_id: str,
    body: MatchDepositManuallyRequest,
    client: AuthenticatedClient = Depends(require_scopes("settlement:manage")),
    session: Session = Depends(get_db_session),
    service: SettlementImportService = Depends(get_settlement_import_service),
) -> RecordDepositResponse:
    """Manually attach a deposit to a tranche (the operator's
    decision — resolves an open ``deposit_without_file`` exception
    explicitly, never silently). An overpayment remainder parks as
    an unallocated receipt and is never auto-allocated. With a
    company id, the affected batch is tied out inline.
    """
    company_ref = None
    if body.company_id is not None:
        row = require_company_row_by_id(session, client, body.company_id)
        company_ref = company_ref_for_row(row)
    with translate_errors():
        result = service.match_deposit_manually(
            client.installation_id,
            MatchDepositManually(
                deposit_id=deposit_id,
                tranche_id=body.tranche_id,
                allocated_amount_irr=body.allocated_amount_irr,
                actor=body.actor or client.client_name,
                tenant_id=client.tenant_id,
            ),
            company=company_ref,
        )
    session.commit()
    return RecordDepositResponse.from_result(result)


@router.get(
    "/settlements/tranches/{tranche_id}/matches",
    response_model=list[SettlementMatchResponse],
    summary="List a tranche's match links",
)
def list_tranche_matches(
    tranche_id: str,
    client: AuthenticatedClient = Depends(require_scopes("settlement:read")),
    service: SettlementQueryService = Depends(get_settlement_query_service),
) -> list[SettlementMatchResponse]:
    """Return a tranche's match links (events + deposits)."""
    with translate_errors():
        matches = service.list_matches_for_tranche(client.installation_id, tranche_id)
    return [SettlementMatchResponse.from_match(match) for match in matches]
