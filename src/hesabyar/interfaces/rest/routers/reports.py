"""Financial-report + report-validation routes (spec §11.2, US-403, US-550).

Thin routes over the report query/validation services: no DSL
evaluation here, no snapshot SQL, no engine calls — the services own
that.

Reads need ``reports:read``; creating a report validation run needs
``standards:validate`` (the run executes the standards rules). A
report validation run belongs to the caller's tenant: a run id of
another tenant — or a plain validation run with no trial-balance
snapshot — answers 404 as if it did not exist.

Every report response carries the §11.1 freshness block (source,
source timestamp, generated-at, company + period, report + calculation
version, freshness indicator, reproducibility metadata). All figures
are read live from the engine — never from a HesabYar cache.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from hesabyar.application.commands.reports import ReportValidationService
from hesabyar.application.queries.cash_forecast import CashForecastQueryService
from hesabyar.application.queries.reports import ReportQueryService
from hesabyar.domain.accounting.refs import AccountRef, PartyRef
from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.infrastructure.persistence.postgres.models import EngineCompanyModel
from hesabyar.interfaces.rest.dependencies import (
    company_ref_for_row,
    get_cash_forecast_query_service,
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
    BalanceSheetResponse,
    CashFlowResponse,
    CashForecastResponse,
    CreateReportValidationRunRequest,
    GeneralLedgerResponse,
    IncomeStatementResponse,
    NotesBalanceResponse,
    ReportValidationRunResponse,
    TrialBalanceResponse,
)

router = APIRouter(tags=["reports"])

_TRIAL_BALANCE_QUERY_PARAMS = {"company", "date_from", "date_to"}
_GENERAL_LEDGER_QUERY_PARAMS = {"company", "date_from", "date_to", "account_id"}
_BALANCE_SHEET_QUERY_PARAMS = {"company", "as_of", "comparison_as_of"}
_INCOME_STATEMENT_QUERY_PARAMS = {"company", "date_from", "date_to", "comparison_from", "comparison_to"}
_CASH_FLOW_QUERY_PARAMS = {"company", "date_from", "date_to"}
_NOTES_BALANCE_QUERY_PARAMS = {"company", "as_of", "instrument", "party_id"}
_CASH_FORECAST_QUERY_PARAMS = {"company", "date_from", "date_to"}


def _check_range(date_from: date, date_to: date) -> None:
    if date_to < date_from:
        raise ApiError(
            status_code=422,
            code="VALIDATION_ERROR",
            detail="date_to must be on or after date_from.",
        )


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
    _check_range(date_from, date_to)
    with translate_errors():
        view = service.get_trial_balance(
            company_ref_for_row(row),
            date_from,
            date_to,
        )
    return TrialBalanceResponse.from_view(view, company_id=row.external_company_id)


@router.get(
    "/reports/general-ledger",
    response_model=GeneralLedgerResponse,
    summary="Get a company's general ledger over posted moves",
)
def get_general_ledger(
    request: Request,
    date_from: date = Query(description="Start of the ledger period (Gregorian, YYYY-MM-DD)"),
    date_to: date = Query(description="End of the ledger period (Gregorian, YYYY-MM-DD)"),
    account_id: int | None = Query(
        default=None, description="Engine account id for one account's lines; omitted returns all accounts"
    ),
    row: EngineCompanyModel = Depends(resolve_accounts_company),
    _client: AuthenticatedClient = Depends(require_scopes("reports:read")),
    service: ReportQueryService = Depends(get_report_query_service),
) -> GeneralLedgerResponse:
    """Return the general ledger for a company and date range.

    Without ``account_id`` every account with posted activity in the
    period is returned with its opening balance and debit/credit
    totals (summary mode). With ``account_id`` only that account is
    returned, with its posted lines and running balances. Only POSTED
    moves contribute; an unknown account answers 404.
    """
    reject_unknown_params(request, _GENERAL_LEDGER_QUERY_PARAMS)
    _check_range(date_from, date_to)
    with translate_errors():
        view = service.get_general_ledger(
            company_ref_for_row(row),
            date_from,
            date_to,
            AccountRef(engine_id=account_id) if account_id is not None else None,
        )
    return GeneralLedgerResponse.from_view(view, company_id=row.external_company_id)


@router.get(
    "/reports/balance-sheet",
    response_model=BalanceSheetResponse,
    summary="Get a company's balance sheet at a date",
)
def get_balance_sheet(
    request: Request,
    as_of: date = Query(description="As-of date of the balance sheet (Gregorian, YYYY-MM-DD)"),
    comparison_as_of: date | None = Query(
        default=None, description="Comparison date; defaults to the same Jalali date one year earlier"
    ),
    row: EngineCompanyModel = Depends(resolve_accounts_company),
    _client: AuthenticatedClient = Depends(require_scopes("reports:read")),
    service: ReportQueryService = Depends(get_report_query_service),
) -> BalanceSheetResponse:
    """Return the balance sheet for a company at a date.

    Assets, liabilities and equity sections from the engine's
    point-in-time account balances, with the current-period result
    inside equity and a prior-period comparison. The balance check
    (total assets == total liabilities + total equity) is carried on
    the response.
    """
    reject_unknown_params(request, _BALANCE_SHEET_QUERY_PARAMS)
    with translate_errors():
        view = service.get_balance_sheet(
            company_ref_for_row(row),
            as_of,
            comparison_as_of,
        )
    return BalanceSheetResponse.from_view(view, company_id=row.external_company_id)


@router.get(
    "/reports/income-statement",
    response_model=IncomeStatementResponse,
    summary="Get a company's income statement over a period",
)
def get_income_statement(
    request: Request,
    date_from: date = Query(description="Start of the income-statement period (Gregorian, YYYY-MM-DD)"),
    date_to: date = Query(description="End of the income-statement period (Gregorian, YYYY-MM-DD)"),
    comparison_from: date | None = Query(
        default=None, description="Start of the comparison period (defaults to the same Jalali period one year earlier)"
    ),
    comparison_to: date | None = Query(default=None, description="End of the comparison period"),
    row: EngineCompanyModel = Depends(resolve_accounts_company),
    _client: AuthenticatedClient = Depends(require_scopes("reports:read")),
    service: ReportQueryService = Depends(get_report_query_service),
) -> IncomeStatementResponse:
    """Return the income statement (P&L) for a company and date range.

    Revenue / expenses sections from the engine's period account
    balances, with a prior-period comparison and a Jalali-month
    revenue/expense breakdown for the chart. The monthly figures
    always sum to the statement totals (``monthly_ties_out``).
    """
    reject_unknown_params(request, _INCOME_STATEMENT_QUERY_PARAMS)
    _check_range(date_from, date_to)
    if (comparison_from is None) != (comparison_to is None):
        raise ApiError(
            status_code=422,
            code="VALIDATION_ERROR",
            detail="comparison_from and comparison_to go together.",
        )
    if comparison_from is not None and comparison_to is not None:
        _check_range(comparison_from, comparison_to)
    with translate_errors():
        view = service.get_income_statement(
            company_ref_for_row(row),
            date_from,
            date_to,
            comparison_from,
            comparison_to,
        )
    return IncomeStatementResponse.from_view(view, company_id=row.external_company_id)


@router.get(
    "/reports/cash-flow",
    response_model=CashFlowResponse,
    summary="Get a company's cash-flow statement over a period",
)
def get_cash_flow(
    request: Request,
    date_from: date = Query(description="Start of the cash-flow period (Gregorian, YYYY-MM-DD)"),
    date_to: date = Query(description="End of the cash-flow period (Gregorian, YYYY-MM-DD)"),
    row: EngineCompanyModel = Depends(resolve_accounts_company),
    _client: AuthenticatedClient = Depends(require_scopes("reports:read")),
    service: ReportQueryService = Depends(get_report_query_service),
) -> CashFlowResponse:
    """Return the direct-method cash-flow statement for a company and date range.

    Posted moves on the company's cash accounts, classified into
    operating / investing / financing by the largest non-cash
    counterpart's account code (calculation ``direct-method/1.0`` —
    Tryton has no cash-flow reference report, so the sections are a
    documented HesabYar derivation; see docs/financial-reports.md).
    The tie-out (opening + sections == closing) is carried on the
    response, with any unclassifiable lines counted, never dropped.
    """
    reject_unknown_params(request, _CASH_FLOW_QUERY_PARAMS)
    _check_range(date_from, date_to)
    with translate_errors():
        view = service.get_cash_flow(
            company_ref_for_row(row),
            date_from,
            date_to,
        )
    return CashFlowResponse.from_view(view, company_id=row.external_company_id)


@router.get(
    "/reports/notes-balance",
    response_model=NotesBalanceResponse,
    summary="Get notes receivable/payable balances with aging (cheque report 7)",
)
def get_notes_balance(
    request: Request,
    as_of: date = Query(description="As-of date the notes are aged at (Gregorian, YYYY-MM-DD)"),
    instrument: str = Query(default="both", description="Instrument filter: 'both', 'receivable' or 'payable'"),
    party_id: int | None = Query(default=None, description="Engine party id to filter to one party"),
    row: EngineCompanyModel = Depends(resolve_accounts_company),
    _client: AuthenticatedClient = Depends(require_scopes("reports:read")),
    service: ReportQueryService = Depends(get_report_query_service),
) -> NotesBalanceResponse:
    """Return notes receivable/payable balances with aging.

    Open posted lines on the notes accounts (1.2.4 Notes Receivable
    Held, 1.2.5 Notes Receivable in Clearing, 2.1.5 Notes Payable),
    aged by the Tryton aged-balance rule (maturity date, falling back
    to the move date). A company whose chart lacks the notes accounts
    reads an honest empty report.
    """
    reject_unknown_params(request, _NOTES_BALANCE_QUERY_PARAMS)
    if instrument not in {"both", "receivable", "payable"}:
        raise ApiError(
            status_code=422,
            code="VALIDATION_ERROR",
            detail="instrument must be 'both', 'receivable' or 'payable'.",
        )
    with translate_errors():
        view = service.get_notes_balance(
            company_ref_for_row(row),
            as_of,
            instrument,
            PartyRef(engine_id=party_id) if party_id is not None else None,
        )
    return NotesBalanceResponse.from_view(view, company_id=row.external_company_id)


@router.get(
    "/reports/cash-forecast",
    response_model=CashForecastResponse,
    summary="Get the cash-forecast calendar and 30/60/90/120-day forecast",
)
def get_cash_forecast(
    request: Request,
    date_from: date = Query(description="Start of the forecast range (Gregorian, YYYY-MM-DD)"),
    date_to: date = Query(description="End of the forecast range (Gregorian, YYYY-MM-DD)"),
    row: EngineCompanyModel = Depends(resolve_accounts_company),
    client: AuthenticatedClient = Depends(require_scopes("reports:read")),
    service: CashForecastQueryService = Depends(get_cash_forecast_query_service),
) -> CashForecastResponse:
    """Return the cash forecast for a company and date range.

    Expected inflows (gateway settlement tranches, sales
    installments, due received cheques, due customer invoices) and
    expected outflows (due issued cheques, supplier invoices,
    packaging purchases) — each item in its قطعی (registered
    record) or پیش‌بینی (expected sales) column, with the monthly
    calendar and the 30/60/90/120-day horizon. Collateral cheques
    appear in NEITHER column, ever. The پیش‌بینی column is empty
    until an expected-sales source exists — never invented.
    """
    reject_unknown_params(request, _CASH_FORECAST_QUERY_PARAMS)
    _check_range(date_from, date_to)
    with translate_errors():
        view = service.get_cash_forecast(
            company_ref_for_row(row),
            tenant_id=client.tenant_id,
            installation_id=client.installation_id,
            company_row_id=row.id,
            date_from=date_from,
            date_to=date_to,
        )
    return CashForecastResponse.from_view(view, company_id=row.external_company_id)


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
