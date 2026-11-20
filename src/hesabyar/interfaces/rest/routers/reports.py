"""Trial-balance + report-validation routes (spec §12.1, US-403).

Thin routes over the report query/validation services: no DSL
evaluation here, no snapshot SQL, no engine calls — the services own
that.

Reads need ``reports:read``; creating a report validation run needs
``standards:validate`` (the run executes the standards rules). A
report validation run belongs to the caller's tenant: a run id of
another tenant — or a plain validation run with no trial-balance
snapshot — answers 404 as if it did not exist.

P&L, balance sheet and cash flow are NOT served here: only the trial
balance closes the Phase 2 ``get_report`` gap; the other statements
stay honestly deferred.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from hesabyar.application.commands.reports import ReportValidationService
from hesabyar.application.queries.reports import ReportQueryService
from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.infrastructure.persistence.postgres.models import EngineCompanyModel
from hesabyar.interfaces.rest.dependencies import (
    company_ref_for_row,
    get_db_session,
    get_report_query_service,
    get_report_validation_service,
    require_company_row_by_id,
    require_scopes,
    resolve_accounts_company,
)
from hesabyar.interfaces.rest.errors import ApiError, translate_errors
from hesabyar.interfaces.rest.pagination import reject_unknown_params
from hesabyar.interfaces.rest.schemas.reports import (
    CreateReportValidationRunRequest,
    ReportValidationRunResponse,
    TrialBalanceResponse,
)

router = APIRouter(tags=["reports"])

_TRIAL_BALANCE_QUERY_PARAMS = {"company", "date_from", "date_to"}


@router.get(
    "/reports/trial-balance",
    response_model=TrialBalanceResponse,
    summary="Get a company's trial balance over posted moves",
)
def get_trial_balance(
    request: Request,
    date_from: date = Query(description="Start of the trial-balance period (Gregorian, YYYY-MM-DD)"),
    date_to: date = Query(description="End of the trial-balance period (Gregorian, YYYY-MM-DD)"),
    row: EngineCompanyModel = Depends(resolve_accounts_company),
    _client: AuthenticatedClient = Depends(require_scopes("reports:read")),
    service: ReportQueryService = Depends(get_report_query_service),
) -> TrialBalanceResponse:
    """Return the trial balance for a company and date range.

    The company comes from the ``company`` query parameter (HesabYar
    row id or engine company id), or — when absent — from the API key's
    company restriction. Only POSTED moves contribute; the response
    carries the source endpoint, move/line counts and the balance
    check with every figure.
    """
    reject_unknown_params(request, _TRIAL_BALANCE_QUERY_PARAMS)
    if date_to < date_from:
        raise ApiError(
            status_code=422,
            code="VALIDATION_ERROR",
            detail="date_to must be on or after date_from.",
        )
    with translate_errors():
        view = service.get_trial_balance(
            company_ref_for_row(row),
            date_from,
            date_to,
        )
    return TrialBalanceResponse.from_view(view, company_id=row.external_company_id)


@router.post(
    "/report-validation-runs",
    response_model=ReportValidationRunResponse,
    status_code=201,
    summary="Create a report validation run over the live trial balance",
)
def create_report_validation_run(
    body: CreateReportValidationRunRequest,
    client: AuthenticatedClient = Depends(require_scopes("standards:validate")),
    session: Session = Depends(get_db_session),
    service: ReportValidationService = Depends(get_report_validation_service),
) -> ReportValidationRunResponse:
    """Validate the company's live trial balance and persist run + snapshot.

    Unknown company → 404 ``COMPANY_NOT_FOUND``; a BLOCKED_SOURCE_GAP
    standard for a 1405+ period → 422 ``STANDARD_SOURCE_BLOCKED``.
    Statement rules whose figures are not in a trial balance yield
    ERROR, never PASS.
    """
    row = require_company_row_by_id(session, client, body.company_id)
    with translate_errors():
        record = service.create_report_validation_run(
            tenant_id=client.tenant_id,
            company=company_ref_for_row(row),
            company_id=body.company_id,
            period_start=body.period_start,
            period_end=body.period_end,
            standard_codes=body.standard_codes,
        )
    session.commit()
    return ReportValidationRunResponse.from_record(record)


@router.get(
    "/report-validation-runs/{run_id}",
    response_model=ReportValidationRunResponse,
    summary="Get a report validation run with its results and provenance",
)
def get_report_validation_run(
    run_id: str,
    client: AuthenticatedClient = Depends(require_scopes("reports:read")),
    service: ReportValidationService = Depends(get_report_validation_service),
) -> ReportValidationRunResponse:
    """Return one report validation run.

    Unknown run (or another tenant's run) → 404
    ``REPORT_VALIDATION_RUN_NOT_FOUND``.
    """
    with translate_errors():
        record = service.get_report_validation_run(run_id, client.tenant_id)
    return ReportValidationRunResponse.from_record(record)
