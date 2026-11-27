"""The 14 read-only tools of spec §13.4 (US-602).

Every tool calls the SAME application query service as the matching
REST endpoint (spec §13.3) — there is no second implementation of any
report here — and serializes its result with the SAME REST response
schema (``interfaces.rest.schemas``), so an MCP result and a REST
response for the same company, date range and source snapshot carry
the same figures by construction. The MCP envelope adds the §13.7
provenance block (``provenance.py``) on top of the REST-shaped
``result``.

Tool arguments are explicit and narrow: company, date range, report
type, account filter, output detail — nothing else. The company is
ALWAYS checked against the authenticated grant: the middleware
(``middleware.py``) resolves the ``company`` argument before the
handler runs, and the handler re-resolves it inside its own session
before touching any service — a caller-provided company id is never
proof of authorization.

Amounts are integer Rial values or safe decimal strings — never
binary floating-point. (``match_rate`` on the bank report and
``age_hours`` on review items are ratios/durations, not amounts;
they serialize exactly as REST serializes them.)

Gaps, documented honestly (no matching REST endpoint or query
service exists for these — the tools wire the closest existing
service instead of inventing a report):

- ``get_accounting_summary`` — no summary service or endpoint
  exists. The tool composes four existing ReportQueryService reads
  (trial balance, balance sheet, income statement, cash flow) and
  carries each component's §11.1 freshness block in ``result.sources``.
- ``get_receivables_aging`` / ``get_payables_aging`` — no
  party open-balance aging exists (Tryton ``aged_balance`` over the
  receivable/payable accounts is not wired anywhere). The tools wire
  ``ReportQueryService.get_notes_balance`` (cheque report 7: notes
  receivable/payable with aging) and say so in ``result.coverage_note``
  — they return NOTES aging, not customer/supplier open-invoice aging.
- ``get_tax_compliance_status`` — no single compliance-status service
  exists. The tool composes TaxPolicyQueryService (active policy
  versions effective on the as-of date) with MoadianQueryService
  (profiles + document/submission counts). Tax policy is a global
  registry; Moadian data is tenant-scoped, not company-scoped —
  the result says so in ``scope_note``.
- ``validate_financial_statement`` persists a validation run +
  trial-balance snapshot, exactly like the REST endpoint it mirrors
  (``POST /report-validation-runs``). That is a validation record,
  not a posting: nothing is journaled, finalized or submitted.

Provenance (§13.7) is attached to every company-scoped financial
result. Two tools carry none, by design: ``list_companies`` (a
multi-company directory listing — there is no single company or
period to provenance) and ``explain_accounting_rule`` (a registry
lookup with no company and no figures).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterable, Sequence
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, cast

from mcp.server.mcpserver import Context, MCPServer
from mcp.shared.exceptions import MCPError
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from hesabyar.domain.accounting.refs import AccountRef, CompanyRef, PartyRef
from hesabyar.domain.accounting.value_objects import ReportFreshness
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.errors import HesabYarError
from hesabyar.domain.review_queue.models import (
    ReviewItemStatus,
    ReviewItemType,
    ReviewSeverity,
)
from hesabyar.domain.review_queue.ports import ReviewItemFilters
from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.infrastructure.persistence.postgres.models import EngineCompanyModel
from hesabyar.interfaces.mcp.auth import resolve_company_row
from hesabyar.interfaces.mcp.context import McpContext, current_client
from hesabyar.interfaces.mcp.errors import (
    MCP_ERROR_INTERNAL,
    CompanyAccessError,
    forbidden,
    internal_error,
    unauthenticated,
)
from hesabyar.interfaces.mcp.provenance import Provenance, build_provenance
from hesabyar.interfaces.mcp.registry import register_hesabyar_tool
from hesabyar.interfaces.mcp.services import McpServices
from hesabyar.interfaces.rest.schemas.bank import ReconciliationStatusResponse
from hesabyar.interfaces.rest.schemas.companies import CompanyResponse
from hesabyar.interfaces.rest.schemas.moadian import MoadianProfileResponse
from hesabyar.interfaces.rest.schemas.reports import (
    BalanceSheetResponse,
    CashFlowResponse,
    GeneralLedgerResponse,
    IncomeStatementResponse,
    NotesBalanceResponse,
    ReportFreshnessResponse,
    ReportValidationRunResponse,
    TrialBalanceResponse,
)
from hesabyar.interfaces.rest.schemas.review_queue import ReviewItemResponse
from hesabyar.interfaces.rest.schemas.standards import StandardDetailResponse
from hesabyar.interfaces.rest.schemas.tax_policy import TaxPolicyVersionResponse

#: JSON-RPC "Invalid params": malformed or out-of-range tool arguments
#: (a reversed date range, an unknown enum value, an unknown rule id).
MCP_ERROR_INVALID_PARAMS = -32602

#: Required scopes per tool (spec §13.4). The middleware enforces them
#: from the registry; a tool missing its scope record would bypass the
#: check, so registration always goes through register_hesabyar_tool.
CORE_TOOL_SCOPES: dict[str, tuple[str, ...]] = {
    "list_companies": ("company:read",),
    "get_accounting_summary": ("reports:read",),
    "get_trial_balance": ("reports:read",),
    "get_general_ledger": ("ledger:read",),
    "get_balance_sheet": ("reports:read",),
    "get_income_statement": ("reports:read",),
    "get_cash_flow": ("reports:read",),
    "get_receivables_aging": ("reports:read",),
    "get_payables_aging": ("reports:read",),
    "get_bank_reconciliation_status": ("banking:read",),
    "get_tax_compliance_status": ("compliance:read",),
    "validate_financial_statement": ("standards:validate",),
    "explain_accounting_rule": ("standards:read",),
    "list_pending_review_items": ("workflow:read",),
}

#: Names of the 14 US-602 tools (tests assert the server lists them).
CORE_TOOL_NAMES: frozenset[str] = frozenset(CORE_TOOL_SCOPES)

#: Pending review items: open or in review (resolved/written-off excluded).
_PENDING_STATUSES = (ReviewItemStatus.OPEN, ReviewItemStatus.IN_REVIEW)

#: list_pending_review_items page cap (same bound as the REST list endpoint).
_MAX_REVIEW_ITEMS = 100


# ---------------------------------------------------------------------------
# Envelope + JSON helpers
# ---------------------------------------------------------------------------


def _json_default(value: Any) -> Any:
    """JSON fallback for values ``model_dump(mode='python')`` leaves native.

    Dates/datetimes serialize as ISO strings (the REST wire form);
    Decimals serialize as decimal strings (never binary floats — the
    §9.3 contract). Anything else is a programming error and fails
    loudly instead of silently stringifying.
    """
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"Value of type {type(value).__name__} is not JSON serializable")


def _dump(model: BaseModel) -> dict[str, Any]:
    """Dump a REST response model to a plain dict (native values kept).

    ``mode='python'`` on purpose: ``mode='json'`` would turn Decimals
    into binary floats. The envelope's ``default=`` hook converts the
    remaining native values (dates, Decimals) safely.
    """
    return cast(dict[str, Any], model.model_dump(mode="python"))


def _envelope(result: dict[str, Any], provenance: Provenance | None) -> str:
    """The tool result: the REST-shaped ``result`` plus §13.7 provenance."""
    payload: dict[str, Any] = {"result": result}
    if provenance is not None:
        payload["provenance"] = provenance.to_dict()
    return json.dumps(payload, ensure_ascii=False, default=_json_default)


# ---------------------------------------------------------------------------
# Per-call plumbing (session + services + identity + error translation)
# ---------------------------------------------------------------------------

#: A tool's synchronous work: given its session, services and identity,
#: return the JSON result string. Runs in a thread (all services are sync).
_Work = Callable[[Session, McpServices, AuthenticatedClient], str]


def _mcp_context(ctx: Context | None) -> McpContext:
    """The lifespan McpContext for this call, or a loud internal error."""
    if ctx is None:
        raise internal_error("MCP context is unavailable.")
    try:
        return cast(McpContext, ctx.request_context.lifespan_context)
    except (RuntimeError, AttributeError) as exc:
        raise internal_error("MCP context is unavailable.") from exc


async def _run_tool(ctx: Context | None, work: _Work) -> str:
    """Run sync ``work`` in a thread with a fresh session + services.

    The identity is resolved on the event loop (the bearer context is
    a contextvar the thread cannot see); the session, the service
    container and the company re-check all happen inside the thread.
    Application errors translate to MCP errors carrying the same
    stable code REST returns (``COMPANY_NOT_FOUND: ...``); argument
    errors (a reversed range, a bad enum) are JSON-RPC invalid params.
    """
    mcp_context = _mcp_context(ctx)
    client = current_client(mcp_context)
    if client is None:
        raise unauthenticated()

    def _sync() -> str:
        session = mcp_context.session_factory()
        try:
            services = mcp_context.services(session)
            try:
                return work(session, services, client)
            finally:
                services.close()
        finally:
            session.close()

    try:
        return await asyncio.to_thread(_sync)
    except MCPError:
        raise
    except CompanyAccessError as exc:
        # The middleware already ran this check; a failure here means
        # the grant changed mid-call — still fail closed.
        raise forbidden(exc.message) from None
    except HesabYarError as exc:
        raise MCPError(MCP_ERROR_INTERNAL, f"{exc.code}: {exc.message}") from None
    except ValueError as exc:
        raise MCPError(MCP_ERROR_INVALID_PARAMS, str(exc)) from None


def _resolve_company(
    session: Session, client: AuthenticatedClient, company: str
) -> tuple[EngineCompanyModel, CompanyRef]:
    """Resolve a caller-supplied company id against the grant (again).

    The middleware already checked it; the handler re-resolves it in
    its own session because it needs the mapping row (engine id +
    stored name) — and because a check that only lives in the
    middleware is a check a future handler could bypass.
    """
    row = resolve_company_row(session, client, company)
    try:
        engine_id = int(row.external_company_id)
    except ValueError:
        raise internal_error(
            "The company mapping is invalid. Please contact support."
        ) from None
    return row, CompanyRef(engine_id)


def _tenant_company_rows(
    session: Session, client: AuthenticatedClient
) -> list[EngineCompanyModel]:
    """The ``engine_companies`` rows the client may see.

    Mirrors REST's ``get_tenant_company_rows``: all of the tenant's
    rows for an unrestricted key, only the bound row for a
    company-restricted key.
    """
    stmt = select(EngineCompanyModel).where(EngineCompanyModel.tenant_id == client.tenant_id)
    if client.engine_company_id is not None:
        stmt = stmt.where(EngineCompanyModel.id == client.engine_company_id)
    return list(session.execute(stmt).scalars())


def _require_freshness(view: Any) -> ReportFreshness:
    """A stamped view's §11.1 freshness block, or a loud failure."""
    freshness = view.freshness
    if freshness is None:
        raise ValueError(
            "Report views must be stamped by the report query service before serialization."
        )
    return freshness


def _provenance_from_freshness(
    row: EngineCompanyModel,
    engine_id: int,
    report_type: str,
    period_label: str,
    freshness: ReportFreshness,
    *,
    period_from: str | None = None,
    period_to: str | None = None,
    snapshot_ref: str | None = None,
    ruleset_version: str | None = None,
    validation_warnings: Sequence[str] = (),
    incomplete_data_warnings: Sequence[str] = (),
) -> Provenance:
    """§13.7 provenance for a live engine read (freshness-stamped view)."""
    return build_provenance(
        company_id=engine_id,
        company_name=row.company_name,
        period_label=period_label,
        report_type=report_type,
        period_from=period_from,
        period_to=period_to,
        source_data_freshness=freshness.freshness_indicator,
        source_ref=freshness.source,
        calculation_version=freshness.calculation_version,
        ruleset_version=ruleset_version,
        snapshot_ref=snapshot_ref,
        validation_warnings=list(validation_warnings),
        incomplete_data_warnings=list(incomplete_data_warnings),
        generated_at=freshness.generated_at,
    )


def _parse_enum[EnumT: Enum](value: str | None, enum: type[EnumT], param: str) -> EnumT | None:
    """Parse an optional enum-valued tool argument (invalid → -32602)."""
    if value is None:
        return None
    try:
        return enum(value)
    except ValueError:
        allowed = ", ".join(sorted(member.value for member in enum))
        raise MCPError(
            MCP_ERROR_INVALID_PARAMS,
            f"{param} must be one of: {allowed} (got {value!r}).",
        ) from None


def _count_by_status(items: Iterable[Any]) -> dict[str, int]:
    """Count domain rows by their StrEnum ``status`` value."""
    counts: dict[str, int] = {}
    for item in items:
        key = str(item.status.value)
        counts[key] = counts.get(key, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# list_companies
# ---------------------------------------------------------------------------


async def list_companies(ctx: Context | None = None) -> str:
    """List the companies available to the authenticated client."""

    def _work(session: Session, services: McpServices, client: AuthenticatedClient) -> str:
        rows = _tenant_company_rows(session, client)
        # Join engine companies against this tenant's mappings: an
        # engine company with no row in this tenant is invisible to it
        # (same join as the REST list endpoint).
        by_external = {row.external_company_id: row for row in rows}
        items: list[CompanyResponse] = []
        for view in services.companies.list_companies():
            row = by_external.get(str(view.ref.engine_id))
            if row is None:
                continue
            items.append(CompanyResponse.from_view(view, row.id))
        items.sort(key=lambda item: item.name)
        return _envelope(
            {"companies": [_dump(item) for item in items], "count": len(items)},
            None,  # no single company/period: no provenance (see module docstring)
        )

    return await _run_tool(ctx, _work)


# ---------------------------------------------------------------------------
# get_accounting_summary
# ---------------------------------------------------------------------------


async def get_accounting_summary(
    company: str,
    date_from: date,
    date_to: date,
    ctx: Context | None = None,
) -> str:
    """Current-period balance and high-level KPIs for a company."""

    def _work(session: Session, services: McpServices, client: AuthenticatedClient) -> str:
        row, ref = _resolve_company(session, client, company)
        trial_balance = services.reports.get_trial_balance(ref, date_from, date_to)
        balance_sheet = services.reports.get_balance_sheet(ref, date_to)
        income_statement = services.reports.get_income_statement(ref, date_from, date_to)
        cash_flow = services.reports.get_cash_flow(ref, date_from, date_to)
        tb_freshness = _require_freshness(trial_balance)
        result = {
            "company_id": str(ref.engine_id),
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "trial_balance": {
                "total_debit": trial_balance.total_debit.amount,
                "total_credit": trial_balance.total_credit.amount,
                "balanced": trial_balance.is_balanced,
                "move_count": trial_balance.move_count,
                "line_count": trial_balance.line_count,
            },
            "balance_sheet": {
                "total_assets": balance_sheet.total_assets.amount,
                "total_liabilities": balance_sheet.total_liabilities.amount,
                "total_equity": balance_sheet.total_equity.amount,
                "balanced": balance_sheet.balanced,
            },
            "income_statement": {
                "total_revenue": income_statement.total_revenue.amount,
                "total_expense": income_statement.total_expense.amount,
                "net_income": income_statement.net_income.amount,
            },
            "cash_flow": {
                "opening_cash": cash_flow.opening_cash.amount,
                "closing_cash": cash_flow.closing_cash.amount,
                "net_change": cash_flow.net_change.amount,
            },
            # Each component's own §11.1 freshness block (source +
            # calculation version), so the composite never hides which
            # read a figure came from.
            "sources": {
                "trial_balance": _dump(ReportFreshnessResponse.from_model(tb_freshness)),
                "balance_sheet": _dump(
                    ReportFreshnessResponse.from_model(_require_freshness(balance_sheet))
                ),
                "income_statement": _dump(
                    ReportFreshnessResponse.from_model(_require_freshness(income_statement))
                ),
                "cash_flow": _dump(
                    ReportFreshnessResponse.from_model(_require_freshness(cash_flow))
                ),
            },
        }
        warnings = (
            ["The trial balance does not balance over posted moves."]
            if not trial_balance.is_balanced
            else []
        )
        provenance = _provenance_from_freshness(
            row,
            ref.engine_id,
            "accounting_summary",
            f"{date_from.isoformat()} to {date_to.isoformat()}",
            tb_freshness,
            period_from=date_from.isoformat(),
            period_to=date_to.isoformat(),
            validation_warnings=warnings,
        )
        return _envelope(result, provenance)

    return await _run_tool(ctx, _work)


# ---------------------------------------------------------------------------
# get_trial_balance
# ---------------------------------------------------------------------------


async def get_trial_balance(
    company: str,
    date_from: date,
    date_to: date,
    ctx: Context | None = None,
) -> str:
    """Trial balance for a company and date range (posted moves only)."""

    def _work(session: Session, services: McpServices, client: AuthenticatedClient) -> str:
        row, ref = _resolve_company(session, client, company)
        view = services.reports.get_trial_balance(ref, date_from, date_to)
        freshness = _require_freshness(view)
        result = _dump(TrialBalanceResponse.from_view(view, company_id=str(ref.engine_id)))
        warnings = (
            ["The trial balance does not balance over posted moves."]
            if not view.is_balanced
            else []
        )
        provenance = _provenance_from_freshness(
            row,
            ref.engine_id,
            "trial_balance",
            f"{date_from.isoformat()} to {date_to.isoformat()}",
            freshness,
            period_from=date_from.isoformat(),
            period_to=date_to.isoformat(),
            validation_warnings=warnings,
        )
        return _envelope(result, provenance)

    return await _run_tool(ctx, _work)


# ---------------------------------------------------------------------------
# get_general_ledger
# ---------------------------------------------------------------------------


async def get_general_ledger(
    company: str,
    date_from: date,
    date_to: date,
    account_id: int | None = None,
    ctx: Context | None = None,
) -> str:
    """Account movements for a company and period.

    Without ``account_id`` every account with posted activity is
    returned with totals only (summary mode); with it, only that
    account, with its posted lines and running balances.
    """

    def _work(session: Session, services: McpServices, client: AuthenticatedClient) -> str:
        row, ref = _resolve_company(session, client, company)
        account = AccountRef(engine_id=account_id) if account_id is not None else None
        view = services.reports.get_general_ledger(ref, date_from, date_to, account)
        freshness = _require_freshness(view)
        result = _dump(GeneralLedgerResponse.from_view(view, company_id=str(ref.engine_id)))
        provenance = _provenance_from_freshness(
            row,
            ref.engine_id,
            "general_ledger",
            f"{date_from.isoformat()} to {date_to.isoformat()}",
            freshness,
            period_from=date_from.isoformat(),
            period_to=date_to.isoformat(),
        )
        return _envelope(result, provenance)

    return await _run_tool(ctx, _work)


# ---------------------------------------------------------------------------
# get_balance_sheet
# ---------------------------------------------------------------------------


async def get_balance_sheet(
    company: str,
    as_of: date,
    comparison_as_of: date | None = None,
    ctx: Context | None = None,
) -> str:
    """Balance sheet for a company at a date (prior-period comparison)."""

    def _work(session: Session, services: McpServices, client: AuthenticatedClient) -> str:
        row, ref = _resolve_company(session, client, company)
        view = services.reports.get_balance_sheet(ref, as_of, comparison_as_of)
        freshness = _require_freshness(view)
        result = _dump(BalanceSheetResponse.from_view(view, company_id=str(ref.engine_id)))
        warnings = (
            ["Total assets do not equal total liabilities plus total equity."]
            if not view.balanced
            else []
        )
        provenance = _provenance_from_freshness(
            row,
            ref.engine_id,
            "balance_sheet",
            f"as of {as_of.isoformat()}",
            freshness,
            period_from=as_of.isoformat(),
            period_to=as_of.isoformat(),
            validation_warnings=warnings,
        )
        return _envelope(result, provenance)

    return await _run_tool(ctx, _work)


# ---------------------------------------------------------------------------
# get_income_statement
# ---------------------------------------------------------------------------


async def get_income_statement(
    company: str,
    date_from: date,
    date_to: date,
    comparison_from: date | None = None,
    comparison_to: date | None = None,
    ctx: Context | None = None,
) -> str:
    """Income statement (P&L) for a company and date range."""

    def _work(session: Session, services: McpServices, client: AuthenticatedClient) -> str:
        row, ref = _resolve_company(session, client, company)
        view = services.reports.get_income_statement(
            ref, date_from, date_to, comparison_from, comparison_to
        )
        freshness = _require_freshness(view)
        result = _dump(IncomeStatementResponse.from_view(view, company_id=str(ref.engine_id)))
        warnings = (
            ["The Jalali-month breakdown does not sum to the statement totals."]
            if not view.monthly_ties_out
            else []
        )
        provenance = _provenance_from_freshness(
            row,
            ref.engine_id,
            "income_statement",
            f"{date_from.isoformat()} to {date_to.isoformat()}",
            freshness,
            period_from=date_from.isoformat(),
            period_to=date_to.isoformat(),
            validation_warnings=warnings,
        )
        return _envelope(result, provenance)

    return await _run_tool(ctx, _work)


# ---------------------------------------------------------------------------
# get_cash_flow
# ---------------------------------------------------------------------------


async def get_cash_flow(
    company: str,
    date_from: date,
    date_to: date,
    ctx: Context | None = None,
) -> str:
    """Direct-method cash-flow statement for a company and date range."""

    def _work(session: Session, services: McpServices, client: AuthenticatedClient) -> str:
        row, ref = _resolve_company(session, client, company)
        view = services.reports.get_cash_flow(ref, date_from, date_to)
        freshness = _require_freshness(view)
        result = _dump(CashFlowResponse.from_view(view, company_id=str(ref.engine_id)))
        warnings: list[str] = []
        if not view.ties_out:
            warnings.append("Opening cash plus the sections does not equal closing cash.")
        if view.unclassified_count:
            warnings.append(
                f"{view.unclassified_count} cash line(s) could not be classified "
                "into a section (counted, never dropped)."
            )
        provenance = _provenance_from_freshness(
            row,
            ref.engine_id,
            "cash_flow",
            f"{date_from.isoformat()} to {date_to.isoformat()}",
            freshness,
            period_from=date_from.isoformat(),
            period_to=date_to.isoformat(),
            validation_warnings=warnings,
        )
        return _envelope(result, provenance)

    return await _run_tool(ctx, _work)


# ---------------------------------------------------------------------------
# get_receivables_aging / get_payables_aging
# ---------------------------------------------------------------------------

#: Honest coverage note (see the module docstring): these tools return
#: NOTES aging (cheque report 7), not party open-invoice aging.
_AGING_COVERAGE_NOTE = (
    "Covers open NOTES (cheques/notes on the notes accounts 1.2.4/1.2.5 "
    "receivable, 2.1.5 payable) with aging by maturity date — cheque "
    "report 7. It does NOT cover customer/supplier open-invoice aging: "
    "no party open-balance aging service exists yet."
)


def _notes_aging(
    session: Session,
    services: McpServices,
    client: AuthenticatedClient,
    *,
    company: str,
    as_of: date,
    party_id: int | None,
    instrument: str,
    report_type: str,
) -> str:
    """Shared notes-aging read for the receivables/payables tools."""
    row, ref = _resolve_company(session, client, company)
    party = PartyRef(engine_id=party_id) if party_id is not None else None
    view = services.reports.get_notes_balance(ref, as_of, instrument, party)
    freshness = _require_freshness(view)
    result = _dump(NotesBalanceResponse.from_view(view, company_id=str(ref.engine_id)))
    result["aging_scope"] = f"notes_{instrument}_only"
    result["coverage_note"] = _AGING_COVERAGE_NOTE
    incomplete = (
        ["The company's chart has no notes accounts; the report is empty."]
        if not view.accounts_read
        else []
    )
    provenance = _provenance_from_freshness(
        row,
        ref.engine_id,
        report_type,
        f"as of {as_of.isoformat()}",
        freshness,
        period_from=as_of.isoformat(),
        period_to=as_of.isoformat(),
        incomplete_data_warnings=incomplete,
    )
    return _envelope(result, provenance)


async def get_receivables_aging(
    company: str,
    as_of: date,
    party_id: int | None = None,
    ctx: Context | None = None,
) -> str:
    """Outstanding notes receivable balances with aging.

    NOTES aging (cheque report 7) — see ``result.coverage_note``:
    no party open-invoice aging service exists yet.
    """

    def _work(session: Session, services: McpServices, client: AuthenticatedClient) -> str:
        return _notes_aging(
            session,
            services,
            client,
            company=company,
            as_of=as_of,
            party_id=party_id,
            instrument="receivable",
            report_type="receivables_aging",
        )

    return await _run_tool(ctx, _work)


async def get_payables_aging(
    company: str,
    as_of: date,
    party_id: int | None = None,
    ctx: Context | None = None,
) -> str:
    """Outstanding notes payable balances with aging.

    NOTES aging (cheque report 7) — see ``result.coverage_note``:
    no party open-invoice aging service exists yet.
    """

    def _work(session: Session, services: McpServices, client: AuthenticatedClient) -> str:
        return _notes_aging(
            session,
            services,
            client,
            company=company,
            as_of=as_of,
            party_id=party_id,
            instrument="payable",
            report_type="payables_aging",
        )

    return await _run_tool(ctx, _work)


# ---------------------------------------------------------------------------
# get_bank_reconciliation_status
# ---------------------------------------------------------------------------


async def get_bank_reconciliation_status(
    company: str,
    date_from: date,
    date_to: date,
    ctx: Context | None = None,
) -> str:
    """Matched/unmatched bank statement status per journal (§11.2)."""

    def _work(session: Session, services: McpServices, client: AuthenticatedClient) -> str:
        row, ref = _resolve_company(session, client, company)
        status = services.bank.reconciliation_status(
            tenant_id=client.tenant_id,
            company=ref,
            company_id=row.id,
            date_from=FiscalDate(date_from),
            date_to=FiscalDate(date_to),
        )
        result = _dump(ReconciliationStatusResponse.from_status(status))
        warnings = (
            [f"{status.unmatched_lines} statement line(s) have no match and no suggestion."]
            if status.unmatched_lines
            else []
        )
        provenance = build_provenance(
            company_id=ref.engine_id,
            company_name=row.company_name,
            period_label=f"{date_from.isoformat()} to {date_to.isoformat()}",
            report_type="bank_reconciliation_status",
            period_from=date_from.isoformat(),
            period_to=date_to.isoformat(),
            source_data_freshness=status.freshness,
            source_ref=status.source,
            calculation_version=status.calculation_version,
            validation_warnings=warnings,
            generated_at=status.generated_at,
        )
        return _envelope(result, provenance)

    return await _run_tool(ctx, _work)


# ---------------------------------------------------------------------------
# get_tax_compliance_status
# ---------------------------------------------------------------------------


async def get_tax_compliance_status(
    company: str,
    as_of: date | None = None,
    ctx: Context | None = None,
) -> str:
    """Tax / e-invoice status using the active policy version.

    Composes the tax-policy registry (active versions effective on
    the as-of date) with Moadian (profiles + document/submission
    counts). Tax policy is a global registry; Moadian data is
    tenant-scoped, not company-scoped (see ``result.scope_note``).
    """

    def _work(session: Session, services: McpServices, client: AuthenticatedClient) -> str:
        row, ref = _resolve_company(session, client, company)
        as_of_date = as_of or date.today()
        versions = [
            version
            for version in services.tax_policy.list_versions(effective_on=as_of_date)
            if version.status == "active"
        ]
        profiles = services.moadian.list_profiles(client.tenant_id, is_active=True)
        documents = services.moadian.list_documents(client.tenant_id)
        submissions = services.moadian.list_submissions(client.tenant_id)
        result = {
            "company_id": str(ref.engine_id),
            "as_of": as_of_date.isoformat(),
            "active_policy_versions": [
                _dump(TaxPolicyVersionResponse.from_version(version)) for version in versions
            ],
            "active_policy_version_count": len(versions),
            "moadian_profiles": [
                _dump(MoadianProfileResponse.from_profile(profile)) for profile in profiles
            ],
            "moadian_documents_by_status": _count_by_status(documents),
            "moadian_submissions_by_status": _count_by_status(submissions),
            "scope_note": (
                "Tax policy versions come from the global registry; Moadian "
                "profiles, documents and submissions are tenant-scoped — "
                "HesabYar does not scope Moadian data per company."
            ),
        }
        provenance = build_provenance(
            company_id=ref.engine_id,
            company_name=row.company_name,
            period_label=f"as of {as_of_date.isoformat()}",
            report_type="tax_compliance_status",
            period_from=as_of_date.isoformat(),
            period_to=as_of_date.isoformat(),
            source_engine="HesabYar",
            source_ref="hesabyar:tax_policy_versions + hesabyar:moadian_profiles/documents/submissions",
        )
        return _envelope(result, provenance)

    return await _run_tool(ctx, _work)


# ---------------------------------------------------------------------------
# validate_financial_statement
# ---------------------------------------------------------------------------


async def validate_financial_statement(
    company: str,
    period_start: date,
    period_end: date,
    standard_codes: list[str] | None = None,
    ctx: Context | None = None,
) -> str:
    """Run deterministic standards checks over the live trial balance.

    Persists a validation run + trial-balance snapshot, exactly like
    the REST endpoint it mirrors (``POST /report-validation-runs``).
    That is a validation record, not a posting: nothing is journaled,
    finalized or submitted. Without ``standard_codes`` every standard
    in the registry is checked.
    """

    def _work(session: Session, services: McpServices, client: AuthenticatedClient) -> str:
        row, ref = _resolve_company(session, client, company)
        record = services.report_validation.create_report_validation_run(
            tenant_id=client.tenant_id,
            company=ref,
            # The engine id, as REST callers supply it: the run record
            # stores the supplied id, and the equality test compares
            # MCP against REST for the same supplied id.
            company_id=str(ref.engine_id),
            period_start=period_start,
            period_end=period_end,
            standard_codes=list(standard_codes) if standard_codes is not None else None,
        )
        session.commit()
        result = _dump(ReportValidationRunResponse.from_record(record))
        warnings: list[str] = []
        if record.run.rules_failed:
            warnings.append(f"{record.run.rules_failed} rule(s) failed.")
        if record.run.rules_errored:
            warnings.append(
                f"{record.run.rules_errored} rule(s) errored: they need statement "
                "figures (balance sheet / income statement) that a trial "
                "balance does not carry — an error is not a pass."
            )
        provenance = build_provenance(
            company_id=ref.engine_id,
            company_name=row.company_name,
            period_label=f"{period_start.isoformat()} to {period_end.isoformat()}",
            report_type="financial_statement_validation",
            period_from=period_start.isoformat(),
            period_to=period_end.isoformat(),
            snapshot_ref=record.snapshot.snapshot_id,
            source_ref=record.snapshot.source_endpoint,
            calculation_version=record.snapshot.ruleset_version or "unknown",
            ruleset_version=record.run.ruleset_version,
            validation_warnings=warnings,
            generated_at=record.run.created_at,
        )
        return _envelope(result, provenance)

    return await _run_tool(ctx, _work)


# ---------------------------------------------------------------------------
# explain_accounting_rule
# ---------------------------------------------------------------------------


async def explain_accounting_rule(
    standard_code: str,
    rule_id: str | None = None,
    ctx: Context | None = None,
) -> str:
    """Retrieve a source-backed rule explanation for a standard.

    Returns the standard's revisions (with source URLs, snapshots and
    verification labels) and its machine-readable rules. With
    ``rule_id`` only that rule is returned; an unknown rule id is an
    invalid-params error. No company argument: standards are registry
    data, not company data — and no figures are involved, so there is
    no §13.7 provenance on this result.
    """

    def _work(session: Session, services: McpServices, client: AuthenticatedClient) -> str:
        detail = services.standards.get_standard(standard_code)
        result = _dump(StandardDetailResponse.from_detail(detail))
        if rule_id is not None:
            rules = [rule for rule in result["rules"] if rule["rule_id"] == rule_id]
            if not rules:
                raise MCPError(
                    MCP_ERROR_INVALID_PARAMS,
                    f"Rule {rule_id!r} is not part of standard {standard_code!r}.",
                )
            result["rules"] = rules
        return _envelope(result, None)

    return await _run_tool(ctx, _work)


# ---------------------------------------------------------------------------
# list_pending_review_items
# ---------------------------------------------------------------------------


async def list_pending_review_items(
    company: str,
    item_type: str | None = None,
    severity: str | None = None,
    limit: int = 50,
    ctx: Context | None = None,
) -> str:
    """Drafts and failed integration events awaiting review (open/in-review).

    Items are the tenant's review queue filtered to the company
    (``company_id`` is the HesabYar API company id), default-sorted
    critical first, then oldest. ``limit`` is clamped to 1–100.
    """

    def _work(session: Session, services: McpServices, client: AuthenticatedClient) -> str:
        row, ref = _resolve_company(session, client, company)
        parsed_type = _parse_enum(item_type, ReviewItemType, "item_type")
        parsed_severity = _parse_enum(severity, ReviewSeverity, "severity")
        limit_value = min(max(limit, 1), _MAX_REVIEW_ITEMS)
        filters = ReviewItemFilters(
            item_type=parsed_type,
            severity=parsed_severity,
            company_id=row.id,
        )
        views = services.review_queue.list_items(client.tenant_id, filters)
        pending = [view for view in views if view.item.status in _PENDING_STATUSES]
        page = pending[:limit_value]
        result = {
            "company_id": str(ref.engine_id),
            "items": [_dump(ReviewItemResponse.from_view(view)) for view in page],
            "count": len(page),
            "total_pending": len(pending),
        }
        provenance = build_provenance(
            company_id=ref.engine_id,
            company_name=row.company_name,
            period_label="pending",
            report_type="pending_review_items",
            source_engine="HesabYar",
            source_ref="hesabyar:review_queue_items",
        )
        return _envelope(result, provenance)

    return await _run_tool(ctx, _work)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

#: Tool functions in catalog order (spec §13.4 table order).
_CORE_TOOLS: tuple[tuple[str, Callable[..., Any], str], ...] = (
    (
        "list_companies",
        list_companies,
        "List the companies available to the authenticated client (tenant-mapped only).",
    ),
    (
        "get_accounting_summary",
        get_accounting_summary,
        "Current-period balance and high-level KPIs: trial-balance totals, balance-sheet totals, "
        "income-statement totals and cash position for a company and date range.",
    ),
    (
        "get_trial_balance",
        get_trial_balance,
        "Trial balance for a company and date range, over posted moves only.",
    ),
    (
        "get_general_ledger",
        get_general_ledger,
        "Account movements for a company and period; with account_id, one account's posted lines "
        "and running balances, otherwise every active account with totals only.",
    ),
    (
        "get_balance_sheet",
        get_balance_sheet,
        "Balance sheet for a company at a date, with a prior-period comparison.",
    ),
    (
        "get_income_statement",
        get_income_statement,
        "Income statement (P&L) for a company and date range, with a prior-period comparison.",
    ),
    (
        "get_cash_flow",
        get_cash_flow,
        "Direct-method cash-flow statement for a company and date range.",
    ),
    (
        "get_receivables_aging",
        get_receivables_aging,
        "Outstanding notes receivable balances with aging by maturity date (cheque report 7). "
        "NOT party open-invoice aging — see the result's coverage_note.",
    ),
    (
        "get_payables_aging",
        get_payables_aging,
        "Outstanding notes payable balances with aging by maturity date (cheque report 7). "
        "NOT party open-invoice aging — see the result's coverage_note.",
    ),
    (
        "get_bank_reconciliation_status",
        get_bank_reconciliation_status,
        "Matched/unmatched bank statement status per journal over a period (spec §11.2).",
    ),
    (
        "get_tax_compliance_status",
        get_tax_compliance_status,
        "Tax / e-invoice status using the active policy version: active tax-policy versions "
        "effective on the as-of date plus Moadian profiles and document/submission counts.",
    ),
    (
        "validate_financial_statement",
        validate_financial_statement,
        "Run deterministic standards checks over the company's live trial balance for a period. "
        "Persists a validation run + snapshot (like the REST endpoint); posts nothing.",
    ),
    (
        "explain_accounting_rule",
        explain_accounting_rule,
        "Retrieve a source-backed rule explanation: a standard's revisions (sources, snapshots, "
        "verification labels) and machine-readable rules, optionally one rule.",
    ),
    (
        "list_pending_review_items",
        list_pending_review_items,
        "Review-queue items awaiting action (open/in-review) for a company, critical first, "
        "then oldest.",
    ),
)


def register_core_tools(server: MCPServer[Any], context: McpContext) -> None:
    """Register the 14 US-602 read-only tools on ``server`` (+ their scopes).

    Registration goes through :func:`register_hesabyar_tool` so a
    tool's scope record cannot be forgotten. Called from
    :func:`hesabyar.interfaces.mcp.server.create_mcp_server` — and
    directly by tests that build their own server.
    """
    assert len(_CORE_TOOLS) == len(CORE_TOOL_SCOPES) == 14
    for name, fn, description in _CORE_TOOLS:
        register_hesabyar_tool(
            server,
            context.registry,
            fn,
            name=name,
            scopes=CORE_TOOL_SCOPES[name],
            description=description,
        )
