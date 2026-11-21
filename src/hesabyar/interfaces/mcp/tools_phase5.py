"""Read-only MCP tools for the Phase 5 reports (US-603, spec §13.4/§13.7).

Five tools, one per Phase 5 report family, each reusing the EXISTING
application query service — no report logic is duplicated here:

- ``get_settlement_status`` — settlement tranches, expected vs
  received, variances (``settlement:read``, US-521 service).
- ``get_cheque_status`` — the commercial cheque registry, due-soon,
  in-clearing aging, collateral memorandum (``cheques:read``,
  US-531/US-534 services).
- ``get_packaging_costs`` — order cost + packaging consumptions +
  shipment costs (``packaging:read``, US-515 service).
- ``get_cashbox_status`` — the cash-box vs sales variance report,
  live box balances, counting sessions (``cashbox:read``, US-516
  service).
- ``get_cash_forecast`` — the 30/60/90/120-day cash forecast,
  confirmed vs forecast (``reports:read``, US-553 service).

Every tool takes the ``company`` argument the middleware checks
against the authenticated grant (spec §13.4 SAFETY: a
caller-provided company id is never proof of authorization) and
re-resolves it inside the handler — the handler needs the mapping
row anyway (engine id, row id, company name), and the second check
fails closed if the row vanished between the middleware and the
handler. Every financial result carries the §13.7 provenance block
built by :func:`hesabyar.interfaces.mcp.provenance.build_provenance`
(the shared module — never reimplemented here).

Amounts are integer Rial values; the only decimals that appear are
rates/quantities, serialized as safe decimal STRINGS (never binary
floating-point — the JSON encoder below has no float path).

Results are serialized with the SAME pydantic response models REST
uses (``interfaces/rest/schemas/*``), so an MCP result and the
matching REST response for the same data and period carry the same
figures by construction; the equality tests in
``tests/mcp/test_mcp_tools_phase5.py`` prove it end to end.

Registration: :func:`register_phase5_tools` wires all five tools
onto a server through :func:`register_hesabyar_tool` (scopes recorded
with the tool — a tool without a scope record would bypass the
middleware's scope check). The server entry point (``main`` in
``server.py``) calls it — one line per story module, the same way
US-604 wires its resources + prompts — so the factory
(:func:`create_mcp_server`) stays bare for the US-601 conformance
tests; see ``docs/mcp-server.md`` for the wiring.
"""

from __future__ import annotations

import asyncio
import datetime
import json
from collections.abc import Callable
from decimal import Decimal
from typing import Any, cast

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from hesabyar.domain.accounting.refs import CompanyRef
from hesabyar.domain.cashbox.ports import CountSessionFilters
from hesabyar.domain.cheques.models import ChequeKind, ChequePurpose, ChequeState
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.errors import HesabYarError
from hesabyar.domain.settlement.models import TrancheState
from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.infrastructure.persistence.postgres.models import EngineCompanyModel
from hesabyar.interfaces.mcp.auth import resolve_company_row
from hesabyar.interfaces.mcp.context import McpContext, current_client
from hesabyar.interfaces.mcp.errors import (
    CompanyAccessError,
    forbidden,
    unauthenticated,
)
from hesabyar.interfaces.mcp.provenance import build_provenance
from hesabyar.interfaces.mcp.registry import register_hesabyar_tool
from hesabyar.interfaces.mcp.services import McpServices
from hesabyar.interfaces.rest.schemas.cashbox import (
    CashBoxReportResponse,
    CashBoxResponse,
    CountSessionResponse,
)
from hesabyar.interfaces.rest.schemas.cheques import (
    ChequeResponse,
    ClearingAgingResponse,
    CollateralSummaryResponse,
)
from hesabyar.interfaces.rest.schemas.packaging import (
    ConsumptionResponse,
    OrderCostReportResponse,
    ShipmentCostResponse,
)
from hesabyar.interfaces.rest.schemas.reports import CashForecastResponse
from hesabyar.interfaces.rest.schemas.settlement import SettlementTrancheResponse

__all__ = [
    "PHASE5_TOOLS",
    "get_cash_forecast",
    "get_cashbox_status",
    "get_cheque_status",
    "get_packaging_costs",
    "get_settlement_status",
    "register_phase5_tools",
]

#: (tool name, required scopes) — the registry record for each tool.
PHASE5_TOOLS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("get_settlement_status", ("settlement:read",)),
    ("get_cheque_status", ("cheques:read",)),
    ("get_packaging_costs", ("packaging:read",)),
    ("get_cashbox_status", ("cashbox:read",)),
    ("get_cash_forecast", ("reports:read",)),
)

#: The report-read page size the cheque query service uses for its own
#: report reads (collateral summary, aging, endorsement summary).
_REPORT_PAGE_SIZE = 1000

#: Hard cap on the rows any one tool returns (matches REST's
#: MAX_PAGE_SIZE so an MCP list and a REST page agree).
_MAX_LIMIT = 100


# -- JSON encoding (no binary floating-point, ever) ------------------------


def _json_default(value: Any) -> Any:
    """Serialize Decimals as strings, dates/datetimes as ISO strings.

    ``model_dump(mode="python")`` keeps Decimals and dates as objects;
    this encoder turns them into JSON-safe values. There is deliberately
    no float path: a float anywhere in a tool result would be a bug.
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    raise TypeError(f"Value of type {type(value).__name__} is not JSON serializable")


def _dump(payload: dict[str, Any]) -> str:
    """Serialize a tool payload to its JSON string result."""
    return json.dumps(payload, ensure_ascii=False, default=_json_default)


# -- handler plumbing -------------------------------------------------------


def _require_mcp_context(ctx: Context | None) -> McpContext:
    """The lifespan context behind a tool call, or a tool error."""
    if ctx is None:
        raise ToolError("The MCP request context is missing; the tool cannot run.")
    try:
        mcp_context = ctx.request_context.lifespan_context
    except (RuntimeError, AttributeError):
        raise ToolError("The MCP request context is missing; the tool cannot run.") from None
    if not isinstance(mcp_context, McpContext):
        raise ToolError("The MCP request context is missing; the tool cannot run.")
    return mcp_context


def _engine_id(row: EngineCompanyModel) -> int:
    """The engine company id for a mapping row (corrupt mapping → error)."""
    try:
        return int(row.external_company_id)
    except ValueError:
        raise ToolError("The company mapping is invalid. Please contact support.") from None


def _limit(value: int) -> int:
    """Validate a list limit (1..100, clamped at the REST page max)."""
    if value < 1:
        raise ToolError("limit must be at least 1.")
    return min(value, _MAX_LIMIT)


def _parse_tranche_state(value: str | None) -> TrancheState | None:
    if value is None:
        return None
    try:
        return TrancheState(value)
    except ValueError:
        valid = ", ".join(sorted(state.value for state in TrancheState))
        raise ToolError(f"Unknown tranche state {value!r}. Expected one of: {valid}.") from None


def _parse_cheque_kind(value: str | None) -> ChequeKind | None:
    if value is None:
        return None
    try:
        return ChequeKind(value)
    except ValueError:
        valid = ", ".join(sorted(kind.value for kind in ChequeKind))
        raise ToolError(f"Unknown cheque kind {value!r}. Expected one of: {valid}.") from None


def _parse_cheque_state(value: str | None) -> ChequeState | None:
    if value is None:
        return None
    try:
        return ChequeState(value)
    except ValueError:
        valid = ", ".join(sorted(state.value for state in ChequeState))
        raise ToolError(f"Unknown cheque state {value!r}. Expected one of: {valid}.") from None


async def _call_service(
    mcp_context: McpContext,
    company: str,
    fn: Callable[[AuthenticatedClient, EngineCompanyModel, McpServices], dict[str, Any]],
) -> str:
    """Run a sync service read in a thread and serialize its payload.

    The identity comes from the call's own context (bearer token or
    stdio bootstrap client — the middleware already authenticated it);
    the company is re-resolved against that grant inside the thread.
    All services are synchronous, so the whole read — session,
    company check, service call, serialization — runs off the event
    loop and the container is closed when the call ends.
    """
    client = current_client(mcp_context)
    if client is None:
        raise unauthenticated()

    def _sync() -> str:
        session = mcp_context.session_factory()
        try:
            row = resolve_company_row(session, client, company)
            services = mcp_context.services(session)
            try:
                return _dump(fn(client, row, services))
            finally:
                services.close()
        finally:
            session.close()

    try:
        return await asyncio.to_thread(_sync)
    except CompanyAccessError as exc:
        # The grant check failed inside the handler (the row vanished
        # after the middleware's check, or the middleware never ran):
        # a protocol-level refusal, like the middleware's own.
        raise forbidden(exc.message) from None
    except HesabYarError as exc:
        # Anticipated domain/application failures (unknown channel,
        # unknown payment, reversed range, engine unavailable): the
        # model reads the message and corrects the call.
        raise ToolError(f"{exc.code}: {exc.message}") from None
    except ValueError as exc:
        raise ToolError(str(exc)) from None


def _company_block(row: EngineCompanyModel, engine_id: int) -> dict[str, Any]:
    """The company identity carried on every tool result."""
    return {
        "id": row.id,
        "engine_id": engine_id,
        "name": row.company_name,
    }


# -- get_settlement_status --------------------------------------------------


async def get_settlement_status(
    company: str,
    channel_key: str | None = None,
    state: str | None = None,
    sales_period: str | None = None,
    due_from: datetime.date | None = None,
    due_to: datetime.date | None = None,
    include_tranches: bool = True,
    limit: int = 100,
    ctx: Context | None = None,
) -> str:
    """Settlement status: tranches with expected vs received and variances.

    The gateway settlement tranches matching the filters, with totals
    over ALL matching tranches (expected net, confirmed net, received
    net, outstanding net, variance) and counts by tranche state. The
    ``tranches`` list itself is limited to ``limit`` rows (default
    100); ``tranche_count`` is the total matching, so a caller can
    tell when the list was truncated. Amounts are integer Rial.
    """
    mcp_context = _require_mcp_context(ctx)
    parsed_state = _parse_tranche_state(state)
    capped = _limit(limit)

    def _read(client: AuthenticatedClient, row: EngineCompanyModel, services: McpServices) -> dict[str, Any]:
        tranches = services.settlement.list_tranches(
            client.installation_id,
            channel_key=channel_key,
            state=parsed_state,
            sales_period_jalali=sales_period,
            due_from=due_from,
            due_to=due_to,
        )
        engine_id = _engine_id(row)
        by_state: dict[str, int] = {}
        for tranche in tranches:
            by_state[tranche.state.value] = by_state.get(tranche.state.value, 0) + 1
        if sales_period is not None:
            period_label = f"sales period {sales_period}"
        elif due_from is not None or due_to is not None:
            period_label = f"due {due_from or '…'} to {due_to or '…'}"
        else:
            period_label = "all tranches"
        return {
            "company": _company_block(row, engine_id),
            "filters": {
                "channel_key": channel_key,
                "state": state,
                "sales_period": sales_period,
                "due_from": due_from.isoformat() if due_from else None,
                "due_to": due_to.isoformat() if due_to else None,
            },
            "totals": {
                "tranche_count": len(tranches),
                "expected_net_irr": sum(t.net_irr for t in tranches),
                "confirmed_net_irr": sum(t.confirmed_net_irr for t in tranches),
                "received_net_irr": sum(t.received_net_irr for t in tranches),
                "outstanding_net_irr": sum(t.outstanding_net_irr for t in tranches),
                "variance_irr": sum(t.variance_irr or 0 for t in tranches),
                "by_state": by_state,
            },
            "tranches": [SettlementTrancheResponse.from_tranche(t).model_dump(mode="python") for t in tranches[:capped]]
            if include_tranches
            else [],
            "returned_tranche_count": min(len(tranches), capped) if include_tranches else 0,
            "provenance": build_provenance(
                company_id=engine_id,
                company_name=row.company_name,
                period_label=period_label,
                period_from=due_from.isoformat() if due_from else None,
                period_to=due_to.isoformat() if due_to else None,
                report_type="settlement_status",
                source_engine="HesabYar",
                source_ref="hesabyar:settlement_tranches",
            ).to_dict(),
        }

    return await _call_service(mcp_context, company, _read)


# -- get_cheque_status ------------------------------------------------------


async def get_cheque_status(
    company: str,
    kind: str | None = None,
    state: str | None = None,
    due_within_days: int = 30,
    include_collateral: bool = True,
    limit: int = 100,
    ctx: Context | None = None,
) -> str:
    """Cheque status: commercial registry, due-soon, in-clearing aging, collateral.

    Four sections, all company-scoped. ``registry`` lists the
    COMMERCIAL cheques only (``purpose=payment`` — a collateral cheque
    can never leak into a commercial figure, §2.2). ``due_soon`` is
    the commercial cheques due within ``due_within_days`` days from
    today (inclusive). ``in_clearing_aging`` ages the in-clearing
    cheques (stale ones are only flagged, never auto-bounced).
    ``collateral`` is the memorandum-only collateral summary (report
    6) — clearly labelled, never mixed into the commercial totals.
    Amounts are integer Rial.
    """
    mcp_context = _require_mcp_context(ctx)
    parsed_kind = _parse_cheque_kind(kind)
    parsed_state = _parse_cheque_state(state)
    capped = _limit(limit)
    if not 0 <= due_within_days <= 365:
        raise ToolError("due_within_days must be between 0 and 365.")

    def _read(client: AuthenticatedClient, row: EngineCompanyModel, services: McpServices) -> dict[str, Any]:
        engine_id = _engine_id(row)
        today = datetime.date.today()
        # The segregation seam (§2.2): the registry section is
        # commercial cheques ONLY — purpose=payment at the source, the
        # same filter list_commercial_cheques applies (which does not
        # take kind/state filters, so the tool uses list_cheques with
        # the purpose fixed). A collateral cheque can never leak into
        # a commercial figure.
        commercial = list(
            services.cheques.list_cheques(
                tenant_id=client.tenant_id,
                installation_id=client.installation_id,
                state=parsed_state,
                kind=parsed_kind,
                purpose=ChequePurpose.PAYMENT,
                company_engine_id=engine_id,
                limit=_REPORT_PAGE_SIZE,
            )
        )
        due_cutoff = today + datetime.timedelta(days=due_within_days + 1)
        due_soon = [
            c
            for c in services.cheques.list_cheques(
                tenant_id=client.tenant_id,
                installation_id=client.installation_id,
                due_before=due_cutoff,
                kind=parsed_kind,
                purpose=ChequePurpose.PAYMENT,
                company_engine_id=engine_id,
                limit=_REPORT_PAGE_SIZE,
            )
            if c.due_date.gregorian_date >= today
        ]
        aging = services.cheques.list_in_clearing_aging(
            tenant_id=client.tenant_id,
            installation_id=client.installation_id,
            company_engine_id=engine_id,
            today=today,
        )
        collateral = (
            services.cheques.collateral_summary(
                tenant_id=client.tenant_id,
                installation_id=client.installation_id,
                company_engine_id=engine_id,
            )
            if include_collateral
            else None
        )
        by_kind: dict[str, dict[str, int]] = {}
        for cheque in commercial:
            bucket = by_kind.setdefault(cheque.kind.value, {"count": 0, "total_irr": 0})
            bucket["count"] += 1
            bucket["total_irr"] += cheque.amount_irr
        return {
            "company": _company_block(row, engine_id),
            "as_of": today.isoformat(),
            "filters": {
                "kind": kind,
                "state": state,
                "due_within_days": due_within_days,
            },
            "totals": {
                "registry_count": len(commercial),
                "registry_total_irr": sum(c.amount_irr for c in commercial),
                "by_kind": by_kind,
                "due_soon_count": len(due_soon),
                "due_soon_total_irr": sum(c.amount_irr for c in due_soon),
                "in_clearing_count": len(aging),
                "in_clearing_stale_count": sum(1 for row_ in aging if row_.stale),
            },
            "registry": [ChequeResponse.from_cheque(c).model_dump(mode="python") for c in commercial[:capped]],
            "returned_registry_count": min(len(commercial), capped),
            "due_soon": [ChequeResponse.from_cheque(c).model_dump(mode="python") for c in due_soon[:capped]],
            "in_clearing_aging": [
                ClearingAgingResponse(
                    cheque_id=row_.cheque_id,
                    sayad_id=row_.sayad_id,
                    days_in_clearing=row_.days_in_clearing,
                    stale=row_.stale,
                    window_days=row_.window_days,
                    window_unset=row_.window_unset,
                ).model_dump(mode="python")
                for row_ in aging
            ],
            "collateral": CollateralSummaryResponse.from_summary(collateral).model_dump(mode="python")
            if collateral is not None
            else None,
            "provenance": build_provenance(
                company_id=engine_id,
                company_name=row.company_name,
                period_label=f"as of {today.isoformat()}",
                report_type="cheque_status",
                source_engine="HesabYar",
                source_ref="hesabyar:cheque_registry",
            ).to_dict(),
        }

    return await _call_service(mcp_context, company, _read)


# -- get_packaging_costs ----------------------------------------------------


async def get_packaging_costs(
    company: str,
    order_ref: str | None = None,
    include_consumptions: bool = True,
    include_shipment_costs: bool = True,
    limit: int = 100,
    ctx: Context | None = None,
) -> str:
    """Packaging + shipping costs for a company, optionally one order.

    ``order_cost`` is the full cost of one order (goods from its
    analytic cost centre when one exists, packaging from the
    consumption records, shipment invoiced/actual/absorbed) — present
    only when ``order_ref`` is given. ``shipment_costs`` and
    ``consumptions`` list the company's records (filtered to the
    order when given), with totals over ALL matching records; each
    list is limited to ``limit`` rows. Amounts are integer Rial.
    """
    mcp_context = _require_mcp_context(ctx)
    capped = _limit(limit)

    def _read(client: AuthenticatedClient, row: EngineCompanyModel, services: McpServices) -> dict[str, Any]:
        engine_id = _engine_id(row)
        order_cost = None
        if order_ref is not None:
            order_cost = services.packaging.get_order_cost(client.tenant_id, CompanyRef(engine_id), row.id, order_ref)
        shipments = (
            list(services.packaging.list_shipment_costs(client.tenant_id, row.id, order_ref=order_ref))
            if include_shipment_costs
            else []
        )
        consumptions = (
            list(services.packaging.list_consumptions(client.tenant_id, row.id, order_ref=order_ref))
            if include_consumptions
            else []
        )
        return {
            "company": _company_block(row, engine_id),
            "filters": {"order_ref": order_ref},
            "totals": {
                "shipment_cost_count": len(shipments),
                "shipment_invoiced_irr": sum(s.invoiced_irr for s in shipments),
                "shipment_actual_irr": sum(s.actual_irr for s in shipments),
                "shipment_absorbed_irr": sum(s.absorbed_irr for s in shipments),
                "consumption_count": len(consumptions),
                "consumption_total_cost_irr": sum(c.total_cost_irr for c in consumptions),
            },
            "order_cost": OrderCostReportResponse.from_report(order_cost).model_dump(mode="python")
            if order_cost is not None
            else None,
            "shipment_costs": [
                ShipmentCostResponse.from_record(s).model_dump(mode="python") for s in shipments[:capped]
            ],
            "returned_shipment_cost_count": min(len(shipments), capped),
            "consumptions": [
                ConsumptionResponse.from_record(c).model_dump(mode="python") for c in consumptions[:capped]
            ],
            "returned_consumption_count": min(len(consumptions), capped),
            "provenance": build_provenance(
                company_id=engine_id,
                company_name=row.company_name,
                period_label=f"order {order_ref}" if order_ref else "all orders",
                report_type="packaging_costs",
                source_engine="HesabYar",
                source_ref="hesabyar:packaging_records",
            ).to_dict(),
        }

    return await _call_service(mcp_context, company, _read)


# -- get_cashbox_status -----------------------------------------------------


async def get_cashbox_status(
    company: str,
    date_from: datetime.date,
    date_to: datetime.date,
    cash_box_id: str | None = None,
    include_boxes: bool = True,
    include_sessions: bool = True,
    limit: int = 100,
    ctx: Context | None = None,
) -> str:
    """Cash-box status: variance report, live balances, counting sessions.

    ``report`` is the cash-box vs sales variance report for the
    period (per box: opening/closing book balances, receipts,
    payments, session shortage/overage, recorded sales, and the
    unrecorded-sales indicator). ``boxes`` lists the company's cash
    boxes with their live book balances; ``sessions`` the counting
    sessions in the period with their variances. Amounts are integer
    Rial.
    """
    mcp_context = _require_mcp_context(ctx)
    capped = _limit(limit)

    def _read(client: AuthenticatedClient, row: EngineCompanyModel, services: McpServices) -> dict[str, Any]:
        engine_id = _engine_id(row)
        company_ref = CompanyRef(engine_id)
        report = services.cashbox.cashbox_report(
            client.tenant_id,
            company_ref,
            row.id,
            FiscalDate(date_from),
            FiscalDate(date_to),
            cash_box_id=cash_box_id,
        )
        boxes = list(services.cashbox.list_cash_boxes(client.tenant_id, company_ref, row.id)) if include_boxes else []
        sessions = (
            list(
                services.cashbox.list_sessions(
                    client.tenant_id,
                    CountSessionFilters(
                        cash_box_id=cash_box_id,
                        date_from=date_from,
                        date_to=date_to,
                    ),
                )
            )
            if include_sessions
            else []
        )
        return {
            "company": _company_block(row, engine_id),
            "filters": {
                "date_from": date_from.isoformat(),
                "date_to": date_to.isoformat(),
                "cash_box_id": cash_box_id,
            },
            "totals": {
                "total_shortage_irr": report.total_shortage_irr,
                "total_overage_irr": report.total_overage_irr,
                "total_possible_unrecorded_sales_irr": (report.total_possible_unrecorded_sales_irr),
                "box_count": len(report.lines),
                "session_count": len(sessions),
            },
            "report": CashBoxReportResponse.from_report(report).model_dump(mode="python"),
            "boxes": [CashBoxResponse.from_view(view).model_dump(mode="python") for view in boxes[:capped]],
            "returned_box_count": min(len(boxes), capped),
            "sessions": [CountSessionResponse.from_session(s).model_dump(mode="python") for s in sessions[:capped]],
            "returned_session_count": min(len(sessions), capped),
            "provenance": build_provenance(
                company_id=engine_id,
                company_name=row.company_name,
                period_label=f"{date_from.isoformat()} to {date_to.isoformat()}",
                period_from=date_from.isoformat(),
                period_to=date_to.isoformat(),
                report_type="cashbox_status",
                source_engine="Tryton",
                source_ref=report.source,
                calculation_version=report.calculation_version,
            ).to_dict(),
        }

    return await _call_service(mcp_context, company, _read)


# -- get_cash_forecast ------------------------------------------------------


async def get_cash_forecast(
    company: str,
    date_from: datetime.date,
    date_to: datetime.date,
    ctx: Context | None = None,
) -> str:
    """Cash forecast: 30/60/90/120-day horizon, confirmed vs forecast.

    Expected inflows (gateway settlement tranches, sales
    installments, due received cheques, due customer invoices) and
    expected outflows (due issued cheques, supplier invoices,
    packaging purchases) — each item in its confirmed (قطعی,
    registered record) or forecast (پیش‌بینی, expected sales) column,
    with the monthly calendar and the 30/60/90/120-day horizon.
    Collateral cheques appear in NEITHER column, ever. The forecast
    column is empty until an expected-sales source exists — never
    invented. Amounts are integer Rial.
    """
    mcp_context = _require_mcp_context(ctx)

    def _read(client: AuthenticatedClient, row: EngineCompanyModel, services: McpServices) -> dict[str, Any]:
        engine_id = _engine_id(row)
        view = services.cash_forecast.get_cash_forecast(
            CompanyRef(engine_id),
            tenant_id=client.tenant_id,
            installation_id=client.installation_id,
            company_row_id=row.id,
            date_from=date_from,
            date_to=date_to,
        )
        response = CashForecastResponse.from_view(view, company_id=row.external_company_id)
        payload = cast(dict[str, Any], response.model_dump(mode="python"))
        freshness = view.freshness
        payload["provenance"] = build_provenance(
            company_id=engine_id,
            company_name=row.company_name,
            period_label=f"{date_from.isoformat()} to {date_to.isoformat()}",
            period_from=date_from.isoformat(),
            period_to=date_to.isoformat(),
            report_type="cash_forecast",
            source_engine="Tryton",
            source_data_freshness=(freshness.freshness_indicator if freshness is not None else "live"),
            source_ref=freshness.source if freshness is not None else None,
            calculation_version=(freshness.calculation_version if freshness is not None else "1.0"),
            generated_at=freshness.generated_at if freshness is not None else None,
        ).to_dict()
        return payload

    return await _call_service(mcp_context, company, _read)


# -- registration -----------------------------------------------------------


def register_phase5_tools(server: MCPServer[Any], context: McpContext) -> None:
    """Register the five US-603 read-only Phase 5 report tools.

    Called by the server entry point (``main`` in ``server.py``) —
    one line per story module, so US-602/US-603/US-604/US-605 wire
    side by side. Every tool goes through
    :func:`register_hesabyar_tool` so its scope record is created
    with it.
    """
    register_hesabyar_tool(
        server,
        context.registry,
        get_settlement_status,
        name="get_settlement_status",
        scopes=("settlement:read",),
        description=(
            "Settlement status: gateway tranches with expected vs received "
            "amounts and variances, filtered by channel, state, sales period "
            "or due date. Amounts are integer Rial."
        ),
    )
    register_hesabyar_tool(
        server,
        context.registry,
        get_cheque_status,
        name="get_cheque_status",
        scopes=("cheques:read",),
        description=(
            "Cheque status: the commercial cheque registry (payment cheques "
            "only), cheques due soon, in-clearing aging, and the "
            "memorandum-only collateral summary. Amounts are integer Rial."
        ),
    )
    register_hesabyar_tool(
        server,
        context.registry,
        get_packaging_costs,
        name="get_packaging_costs",
        scopes=("packaging:read",),
        description=(
            "Packaging + shipping costs: full order cost (goods, packaging, "
            "shipment) for one order, plus the company's packaging "
            "consumption and shipment-cost records. Amounts are integer Rial."
        ),
    )
    register_hesabyar_tool(
        server,
        context.registry,
        get_cashbox_status,
        name="get_cashbox_status",
        scopes=("cashbox:read",),
        description=(
            "Cash-box status: the cash-box vs sales variance report for a "
            "period, live box balances, and counting sessions with their "
            "variances. Amounts are integer Rial."
        ),
    )
    register_hesabyar_tool(
        server,
        context.registry,
        get_cash_forecast,
        name="get_cash_forecast",
        scopes=("reports:read",),
        description=(
            "Cash forecast for a company and date range: expected inflows "
            "and outflows with the monthly calendar and the 30/60/90/120-day "
            "horizon, confirmed vs forecast. Amounts are integer Rial."
        ),
    )
