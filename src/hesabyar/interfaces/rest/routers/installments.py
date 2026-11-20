"""Installment routes (US-540 + US-541 + US-542 + US-543, Phase 5-5 ۵ج).

Thin routes over the installment services: no state-machine or
allocation logic here — the service owns creation + the linkage
guard, the query service owns the computed statuses.

- ``POST /installments/plans`` — create a plan + legs (terms-based
  or explicit legs; 201). One plan per invoice (a second plan for
  the same invoice is a 409). US-543: ``kind`` is ``internal``
  (default) or ``bnpl`` (requires ``gateway_channel_key``).
- ``GET /installments/plans`` / ``{plan_id}`` — plan reads (detail
  carries the legs with computed statuses, the receipts with their
  allocations, and the derived totals). US-543: the list filters
  by ``kind=internal|bnpl``; the detail carries the gateway-side
  totals for BNPL plans.
- ``POST /installments/plans/{plan_id}/receipts`` — record a
  receipt (201). THE LINKAGE GUARD is enforced: a receipt without
  BOTH the original invoice ref and the accounting-document
  (journal move) ref is rejected with a typed 422
  (``INSTALLMENT_RECEIPT_LINKAGE_REQUIRED``); a receipt naming the
  wrong invoice is a typed 422 (``INSTALLMENT_INVOICE_MISMATCH``).
  A cheque receipt (US-542) names its cheque (``cheque_id`` — a
  received commercial cheque in ``held`` or ``in_clearing``);
  the leg it pays enters ``pending_clearance`` until the cheque
  clears. US-543: ``payment_method=bnpl`` (BNPL plans only, with
  ``gateway`` + ``gateway_reference``) creates the gateway
  receivable through the settlement engine.
- ``POST /installments/plans/{plan_id}/receipts/{receipt_id}/reject-bnpl``
  — record a gateway rejection/cancellation for a BNPL receipt
  (US-543): cancels the gateway payment, returns the legs to
  ``expected``, files a review-queue item.
- ``POST /installments/contracts`` — create one version of a sale
  contract (201); ``GET /installments/contracts`` / ``{id}`` — reads.
- ``GET /installments/plans/{plan_id}/payoff-quote`` — the
  early-settlement payoff quote (US-541).
- ``POST /installments/plans/{plan_id}/early-settlement`` — settle
  early, full or partial (US-541; the linkage guard applies).
- ``GET /installments/overdue`` — the overdue report (US-541).
- ``POST /installments/dunning/run`` — one dunning sweep: file
  ``installment_overdue`` review items + assess late fees (US-541).
- ``POST /installments/plans/{plan_id}/remind`` — manually trigger
  an SMS reminder for one leg (201; US-544). The manual trigger is
  itself the opt-in for this one send; the contract opt-in gates
  AUTOMATIC sends only. Needs ``sms:manage``.
- ``GET /installments/plans/{plan_id}/reminders`` — the plan's
  reminder log (US-544). Needs ``installments:read``.
- ``GET /installments/plans/{plan_id}/amendments`` — the plan's
  schedule amendments, oldest first (US-545): the audit history
  of the plan-change flow (partial early settlements that
  re-amortised the schedule). Needs ``installments:read``.

Reads need ``installments:read``; creation needs
``installments:write``. Everything is tenant/installation-scoped:
a row of another installation answers 404 as if it did not exist.
Late-fee journals are drafts by default, auto-posted only when the
plan opted in (US-541).
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.orm import Session

from hesabyar.application.commands.installments import InstallmentCommandService
from hesabyar.application.commands.installments_finance import InstallmentFinanceService
from hesabyar.application.commands.sms import SmsReminderService
from hesabyar.application.queries.installments import InstallmentQueryService
from hesabyar.application.queries.sms import SmsQueryService
from hesabyar.domain.installments.models import PlanKind, PlanStatus
from hesabyar.domain.installments.validation import parse_jalali_date
from hesabyar.domain.sms.models import SmsLogStatus
from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.infrastructure.persistence.postgres.models import EngineCompanyModel
from hesabyar.interfaces.rest.dependencies import (
    company_ref_for_row,
    get_db_session,
    get_installment_command_service,
    get_installment_finance_service,
    get_installment_query_service,
    get_sms_query_service,
    get_sms_reminder_service,
    require_company_row_by_id,
    require_scopes,
)
from hesabyar.interfaces.rest.errors import ApiError, translate_errors
from hesabyar.interfaces.rest.pagination import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    reject_unknown_params,
)
from hesabyar.interfaces.rest.schemas.installments import (
    ContractResponse,
    CreateContractRequest,
    CreatePlanRequest,
    CreateReceiptRequest,
    DunningRunRequest,
    DunningRunResponse,
    EarlySettlementRequest,
    EarlySettlementResponse,
    OverdueReportResponse,
    PayoffQuoteResponse,
    PlanDetailResponse,
    PlanResponse,
    ReceiptResponse,
    RejectBnplRequest,
    ScheduleAmendmentResponse,
)
from hesabyar.interfaces.rest.schemas.sms import (
    ReminderLogResponse,
    TriggerReminderRequest,
)

router = APIRouter(tags=["installments"])

_LIST_PLANS_QUERY_PARAMS = frozenset({"invoice_id", "kind", "status", "limit", "offset"})
_LIST_CONTRACTS_QUERY_PARAMS = frozenset({"code", "limit", "offset"})
_OVERDUE_QUERY_PARAMS = frozenset({"plan_id", "limit", "offset"})
_LIST_PLAN_REMINDERS_QUERY_PARAMS = frozenset({"status", "leg_id", "limit", "offset"})


def _parse_as_of(raw: str | None, field_name: str = "as_of") -> datetime.date | None:
    """Parse an optional Jalali as-of date (422 on a bad value)."""
    if raw is None:
        return None
    errors: dict[str, str] = {}
    parsed = parse_jalali_date(raw, field_name, errors)
    if parsed is None:
        raise ApiError(
            status_code=422,
            code="INSTALLMENT_VALIDATION_FAILED",
            detail=f"Invalid {field_name}: {errors.get(field_name, raw)}",
        )
    return parsed.gregorian_date


@router.post(
    "/installments/plans",
    response_model=PlanResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an installment plan + legs for one invoice",
)
def create_plan(
    body: CreatePlanRequest,
    client: AuthenticatedClient = Depends(require_scopes("installments:write")),
    session: Session = Depends(get_db_session),
    service: InstallmentCommandService = Depends(get_installment_command_service),
    query: InstallmentQueryService = Depends(get_installment_query_service),
) -> PlanResponse:
    """Create a plan: one invoice, one schedule (terms-based or explicit legs)."""
    row: EngineCompanyModel = require_company_row_by_id(session, client, body.company_id)
    company_ref = company_ref_for_row(row)
    with translate_errors():
        plan = service.create_plan(
            tenant_id=client.tenant_id,
            installation_id=client.installation_id,
            company_engine_id=company_ref.engine_id,
            payload=body.payload_fields(),
        )
        detail = query.get_plan_detail(
            plan.id, tenant_id=client.tenant_id, installation_id=client.installation_id
        )
    session.commit()
    return PlanResponse.from_detail(detail)


@router.get(
    "/installments/plans",
    response_model=list[PlanResponse],
    summary="List installment plans in this installation",
)
def list_plans(
    request: Request,
    invoice_id: str | None = Query(default=None, description="Filter by invoice id"),
    kind: str | None = Query(
        default=None, description="Filter by plan kind (US-543): internal | bnpl"
    ),
    status: str | None = Query(
        default=None, description="Filter by derived status: active | overdue | paid"
    ),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"),
    offset: int = Query(default=0, ge=0, description="Page offset"),
    client: AuthenticatedClient = Depends(require_scopes("installments:read")),
    service: InstallmentQueryService = Depends(get_installment_query_service),
) -> list[PlanResponse]:
    """Return the installation's plans, newest first, with derived statuses."""
    reject_unknown_params(request, _LIST_PLANS_QUERY_PARAMS)
    parsed_kind: PlanKind | None = None
    if kind is not None:
        try:
            parsed_kind = PlanKind(kind)
        except ValueError as exc:
            raise ApiError(
                status_code=422,
                code="INSTALLMENT_VALIDATION_FAILED",
                detail=f"Unknown plan kind {kind!r}.",
            ) from exc
    parsed_status: PlanStatus | None = None
    if status is not None:
        try:
            parsed_status = PlanStatus(status)
        except ValueError as exc:
            raise ApiError(
                status_code=422,
                code="INSTALLMENT_VALIDATION_FAILED",
                detail=f"Unknown plan status {status!r}.",
            ) from exc
    with translate_errors():
        summaries = service.list_plans(
            tenant_id=client.tenant_id,
            installation_id=client.installation_id,
            invoice_id=invoice_id,
            kind=parsed_kind,
            status=parsed_status,
            limit=limit,
            offset=offset,
        )
    return [PlanResponse.from_summary(s) for s in summaries]


@router.get(
    "/installments/plans/{plan_id}",
    response_model=PlanDetailResponse,
    summary="Get one installment plan with legs + receipts",
)
def get_plan(
    plan_id: str,
    client: AuthenticatedClient = Depends(require_scopes("installments:read")),
    service: InstallmentQueryService = Depends(get_installment_query_service),
) -> PlanDetailResponse:
    """Return one plan: legs with computed statuses, receipts, derived totals."""
    with translate_errors():
        detail = service.get_plan_detail(
            plan_id, tenant_id=client.tenant_id, installation_id=client.installation_id
        )
    return PlanDetailResponse.from_detail(detail)


@router.get(
    "/installments/plans/{plan_id}/amendments",
    response_model=list[ScheduleAmendmentResponse],
    summary="List a plan's schedule amendments (the plan-change audit history)",
)
def list_plan_amendments(
    plan_id: str,
    client: AuthenticatedClient = Depends(require_scopes("installments:read")),
    service: InstallmentQueryService = Depends(get_installment_query_service),
) -> list[ScheduleAmendmentResponse]:
    """Return the plan's amendments, oldest first: every partial
    early settlement that re-amortised the schedule, with its
    before/after leg principals, applied advance and triggering
    receipt (US-545). 404 when the plan does not exist."""
    with translate_errors():
        amendments = service.list_plan_amendments(
            plan_id, tenant_id=client.tenant_id, installation_id=client.installation_id
        )
    return [ScheduleAmendmentResponse.from_amendment(a) for a in amendments]


@router.post(
    "/installments/plans/{plan_id}/receipts",
    response_model=ReceiptResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Record a receipt against a plan (linkage guard enforced)",
)
def create_receipt(
    plan_id: str,
    body: CreateReceiptRequest,
    client: AuthenticatedClient = Depends(require_scopes("installments:write")),
    session: Session = Depends(get_db_session),
    service: InstallmentCommandService = Depends(get_installment_command_service),
) -> ReceiptResponse:
    """Record a receipt: the linkage guard rejects a receipt without
    BOTH the original invoice ref and the accounting-document ref.
    A cheque receipt names its cheque and pays one leg, which
    enters ``pending_clearance`` until the cheque clears (US-542)."""
    with translate_errors():
        receipt = service.record_receipt(
            tenant_id=client.tenant_id,
            installation_id=client.installation_id,
            plan_id=plan_id,
            payload=body.payload_fields(),
        )
    session.commit()
    return ReceiptResponse.from_receipt(receipt)


@router.post(
    "/installments/plans/{plan_id}/receipts/{receipt_id}/reject-bnpl",
    response_model=ReceiptResponse,
    summary="Record a gateway rejection/cancellation for a BNPL receipt (US-543)",
)
def reject_bnpl_receipt(
    plan_id: str,
    receipt_id: str,
    body: RejectBnplRequest,
    client: AuthenticatedClient = Depends(require_scopes("installments:write")),
    session: Session = Depends(get_db_session),
    service: InstallmentCommandService = Depends(get_installment_command_service),
) -> ReceiptResponse:
    """Reject a BNPL receipt: cancel its gateway payment, return the
    legs to ``expected``, and file a review-queue item."""
    with translate_errors():
        receipt = service.reject_bnpl_receipt(
            tenant_id=client.tenant_id,
            installation_id=client.installation_id,
            plan_id=plan_id,
            receipt_id=receipt_id,
            reason=body.reason,
            actor=client.client_name,
        )
    session.commit()
    return ReceiptResponse.from_receipt(receipt)


@router.post(
    "/installments/contracts",
    response_model=ContractResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create one version of an installment sale contract",
)
def create_contract(
    body: CreateContractRequest,
    client: AuthenticatedClient = Depends(require_scopes("installments:write")),
    session: Session = Depends(get_db_session),
    service: InstallmentCommandService = Depends(get_installment_command_service),
) -> ContractResponse:
    """Create a sale contract version (late-fee rule, penalty account, early-settlement terms)."""
    row: EngineCompanyModel = require_company_row_by_id(session, client, body.company_id)
    company_ref = company_ref_for_row(row)
    with translate_errors():
        contract = service.create_contract(
            tenant_id=client.tenant_id,
            installation_id=client.installation_id,
            company_engine_id=company_ref.engine_id,
            payload=body.payload_fields(),
        )
    session.commit()
    return ContractResponse.from_contract(contract)


@router.get(
    "/installments/contracts",
    response_model=list[ContractResponse],
    summary="List installment sale contracts in this installation",
)
def list_contracts(
    request: Request,
    code: str | None = Query(default=None, description="Filter by contract code (one code's versions)"),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"),
    offset: int = Query(default=0, ge=0, description="Page offset"),
    client: AuthenticatedClient = Depends(require_scopes("installments:read")),
    service: InstallmentQueryService = Depends(get_installment_query_service),
) -> list[ContractResponse]:
    """Return the installation's contracts, newest first."""
    reject_unknown_params(request, _LIST_CONTRACTS_QUERY_PARAMS)
    with translate_errors():
        contracts = service.list_contracts(
            tenant_id=client.tenant_id,
            installation_id=client.installation_id,
            code=code,
            limit=limit,
            offset=offset,
        )
    return [ContractResponse.from_contract(c) for c in contracts]


@router.get(
    "/installments/contracts/{contract_id}",
    response_model=ContractResponse,
    summary="Get one installment sale contract",
)
def get_contract(
    contract_id: str,
    client: AuthenticatedClient = Depends(require_scopes("installments:read")),
    service: InstallmentQueryService = Depends(get_installment_query_service),
) -> ContractResponse:
    """Return one contract version."""
    with translate_errors():
        contract = service.get_contract(
            contract_id, tenant_id=client.tenant_id, installation_id=client.installation_id
        )
    return ContractResponse.from_contract(contract)


@router.get(
    "/installments/plans/{plan_id}/payoff-quote",
    response_model=PayoffQuoteResponse,
    summary="Get the early-settlement payoff quote for one plan",
)
def get_payoff_quote(
    plan_id: str,
    as_of: str | None = Query(
        default=None, description="Quote date, Jalali YYYY-MM-DD (default: today)"
    ),
    client: AuthenticatedClient = Depends(require_scopes("installments:read")),
    service: InstallmentQueryService = Depends(get_installment_query_service),
) -> PayoffQuoteResponse:
    """Return the payoff quote: remaining principal + income due + late fees due − advance held."""
    day = _parse_as_of(as_of)
    with translate_errors():
        quote = service.get_payoff_quote(
            plan_id,
            tenant_id=client.tenant_id,
            installation_id=client.installation_id,
            today=day,
        )
    return PayoffQuoteResponse.from_quote(quote)


@router.post(
    "/installments/plans/{plan_id}/early-settlement",
    response_model=EarlySettlementResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Settle a plan early (full close or partial payment + re-amortisation)",
)
def create_early_settlement(
    plan_id: str,
    body: EarlySettlementRequest,
    client: AuthenticatedClient = Depends(require_scopes("installments:write")),
    session: Session = Depends(get_db_session),
    service: InstallmentFinanceService = Depends(get_installment_finance_service),
) -> EarlySettlementResponse:
    """Settle early: a full settlement closes every open leg for the
    payoff amount; a partial settlement pays part of the schedule
    and re-amortises the rest. THE LINKAGE GUARD applies (a
    settlement is a receipt)."""
    with translate_errors():
        result = service.settle_early(
            tenant_id=client.tenant_id,
            installation_id=client.installation_id,
            plan_id=plan_id,
            payload=body.payload_fields(),
        )
    session.commit()
    return EarlySettlementResponse.from_result(result)


@router.get(
    "/installments/overdue",
    response_model=OverdueReportResponse,
    summary="List overdue installment legs (the overdue report)",
)
def list_overdue(
    request: Request,
    plan_id: str | None = Query(default=None, description="Scope the report to one plan"),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"),
    offset: int = Query(default=0, ge=0, description="Page offset"),
    client: AuthenticatedClient = Depends(require_scopes("installments:read")),
    service: InstallmentQueryService = Depends(get_installment_query_service),
) -> OverdueReportResponse:
    """Return the installation's overdue legs, most overdue first, with dunning stages + accrued late fees."""
    reject_unknown_params(request, _OVERDUE_QUERY_PARAMS)
    with translate_errors():
        report = service.list_overdue(
            tenant_id=client.tenant_id,
            installation_id=client.installation_id,
            plan_id=plan_id,
            limit=limit,
            offset=offset,
        )
    return OverdueReportResponse.from_report(report)


@router.post(
    "/installments/dunning/run",
    response_model=DunningRunResponse,
    summary="Run one dunning sweep (file overdue review items + assess late fees)",
)
def run_dunning(
    body: DunningRunRequest | None = None,
    client: AuthenticatedClient = Depends(require_scopes("installments:write")),
    session: Session = Depends(get_db_session),
    service: InstallmentFinanceService = Depends(get_installment_finance_service),
) -> DunningRunResponse:
    """Sweep the installation's plans: file ``installment_overdue``
    review items for legs reaching a new dunning stage, and assess
    (and journal, when the engine is wired) their late fees.
    Re-running the sweep is idempotent — a leg already filed at its
    current stage, or already assessed through today, is skipped."""
    day = _parse_as_of(body.as_of if body is not None else None)
    with translate_errors():
        result = service.run_dunning(
            tenant_id=client.tenant_id,
            installation_id=client.installation_id,
            today=day,
        )
    session.commit()
    return DunningRunResponse.from_result(result)
# ---------------------------------------------------------------------------
# SMS reminders for a plan (US-544)
# ---------------------------------------------------------------------------


@router.post(
    "/installments/plans/{plan_id}/remind",
    response_model=ReminderLogResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Manually trigger an SMS reminder for one leg",
)
def remind_plan(
    plan_id: str,
    body: TriggerReminderRequest,
    client: AuthenticatedClient = Depends(require_scopes("sms:manage")),
    session: Session = Depends(get_db_session),
    service: SmsReminderService = Depends(get_sms_reminder_service),
) -> ReminderLogResponse:
    """Send (or dry-run) a reminder for one leg of this plan.

    The manual trigger is itself the opt-in for this one send; the
    leg must be unpaid and not awaiting cheque clearing, and a
    reminder for this leg + offset must not already have gone out
    (409). A dry run renders the message, logs a ``dry_run`` row,
    and sends nothing.
    """
    with translate_errors():
        entry = service.send_manual_reminder(
            tenant_id=client.tenant_id,
            installation_id=client.installation_id,
            plan_id=plan_id,
            leg_id=body.leg_id,
            provider_id=body.provider_id,
            recipient_phone=body.recipient_phone,
            dry_run=body.dry_run,
            actor=f"api:{client.key_id}",
        )
    session.commit()
    return ReminderLogResponse.from_domain(entry)


@router.get(
    "/installments/plans/{plan_id}/reminders",
    response_model=list[ReminderLogResponse],
    summary="List a plan's reminder log",
)
def list_plan_reminders(
    plan_id: str,
    request: Request,
    status: str | None = Query(
        default=None,
        description="Filter by status: queued | sent | delivered | failed | skipped | dry_run",
    ),
    leg_id: str | None = Query(default=None, description="Filter by installment leg id"),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"),
    offset: int = Query(default=0, ge=0, description="Page offset"),
    client: AuthenticatedClient = Depends(require_scopes("installments:read")),
    service: SmsQueryService = Depends(get_sms_query_service),
) -> list[ReminderLogResponse]:
    """Return the plan's reminder-log rows, newest first."""
    reject_unknown_params(request, _LIST_PLAN_REMINDERS_QUERY_PARAMS)
    parsed_status: SmsLogStatus | None = None
    if status is not None:
        try:
            parsed_status = SmsLogStatus(status)
        except ValueError as exc:
            raise ApiError(
                status_code=422,
                code="SMS_VALIDATION_FAILED",
                detail=f"Unknown reminder status {status!r}.",
            ) from exc
    with translate_errors():
        entries = service.list_log(
            installation_id=client.installation_id,
            plan_id=plan_id,
            leg_id=leg_id,
            status=parsed_status,
            limit=limit,
            offset=offset,
        )
    return [ReminderLogResponse.from_domain(entry) for entry in entries]
