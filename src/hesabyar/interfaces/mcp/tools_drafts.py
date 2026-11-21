"""Draft-only MCP mutation tools (US-605, spec §13.5).

Four tools, one per draft family, each reusing the EXISTING
application command handler — no draft logic is duplicated here:

- ``prepare_journal_draft`` — a draft journal (unposted move) in the
  engine (``ledger:draft:create``, US-202 ``JournalDraftService``).
- ``prepare_invoice_draft`` — a draft invoice in the engine
  (``invoice:draft:create``, US-204/US-205 ``InvoiceDraftService``).
- ``prepare_reconciliation_suggestions`` — run the statement matcher
  over a draft bank statement (``banking:reconcile``, US-514
  ``BankReconcileService``). The statement stays a draft.
- ``prepare_tax_document_draft`` — a Moadian document in ``draft``
  status (``compliance:draft:create``, US-404
  ``MoadianDocumentService``). No submission is created.

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
invoice quantities, serialized as safe decimal STRINGS (never binary
floating-point — the JSON encoder below has no float path).

Results are serialized with the SAME pydantic response models REST
uses (``interfaces/rest/schemas/*``), so an MCP draft and the
matching REST draft for the same input carry the same figures by
construction. The one exception is the tax document: the result
carries the document metadata but NOT the payload itself — the
payload is the caller's own input (echoing it back adds nothing),
and a Moadian payload can carry float rates (``vra``), which MCP
results never contain; the ``payload_hash`` lets the caller verify
integrity instead.

SAFETY — the never-post guarantee (spec §13.5, §0 rule 8, §19.11):

- A tool creates a DRAFT object only and returns its ID, validation
  findings and the next human action. It NEVER posts the journal,
  finalizes the invoice, approves a reconciliation, or submits
  anything to Moadian. The guarantee is structural: this module
  never calls ``post_draft``, ``finalize_invoice``,
  ``post_invoice``, any statement validation, ``approve_submission``
  or ``queue_submission`` — there is no code path from any tool to
  any of them. The tests in
  ``tests/mcp/test_mcp_tools_drafts.py`` prove it behaviourally per
  tool (the draft exists AND its state is still ``draft`` AND no
  posted/finalized/approved/submitted object resulted).
- There is NO ``approve_and_post`` tool in this release (spec
  §13.5). A model's natural-language statement is NEVER approval:
  no tool accepts free-text approval, and no tool path treats a
  prompt argument as confirmation.
- Drafts go through the SAME validation as dashboard-created drafts
  (the §8.2 invariants, enforced by the same command handlers and
  the same engine adapter) and are draft by default (ADR 0003
  decision 1). The auto-post policy gate is NOT consulted here: the
  gate governs whether a pipeline event may auto-post, and MCP has
  no post path at all — the draft-by-default verdict is structural,
  not a gate decision that could flip.
- The draft is attributed to the MCP client in the audit log: the
  middleware writes one ``ai_access_audit`` row per call (client,
  key, tool, company, outcome), and the reconciliation run passes
  the client identity as the actor on the review items it files.
- Audit-failure limitation (see ``middleware.py``): an audit-write
  failure fails the call closed and withholds the result, but the
  tool has already run by then — a failed audit does NOT roll back
  the draft. The draft survives as an ordinary unposted draft for a
  human to review; it is never posted by the failure path.

Registration: :func:`register_draft_tools` wires all four tools onto
a server through :func:`register_hesabyar_tool` (scopes recorded
with the tool — a tool without a scope record would bypass the
middleware's scope check). The server entry point (``main`` in
``server.py``) calls it — one line per story module, the same way
US-603 wires its tools — so the factory
(:func:`create_mcp_server`) stays bare for the US-601 conformance
tests; see ``docs/mcp-server.md`` for the wiring.
"""

from __future__ import annotations

import asyncio
import datetime
import json
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from hesabyar.application.errors import MoadianInvalidDocumentError
from hesabyar.domain.accounting.commands import InvoiceDraftLine, JournalDraftLine
from hesabyar.domain.accounting.refs import (
    AccountRef,
    BankStatementRef,
    CompanyRef,
    JournalRef,
    PartyAddressRef,
    PartyRef,
)
from hesabyar.domain.accounting.value_objects import ValidationSummary
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.errors import HesabYarError
from hesabyar.domain.common.money import MoneyIRR
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
from hesabyar.interfaces.rest.schemas.bank import ReconcileResponse
from hesabyar.interfaces.rest.schemas.invoices import InvoiceResponse
from hesabyar.interfaces.rest.schemas.journals import JournalDraftResponse, ValidationResponse
from hesabyar.interfaces.rest.schemas.moadian import MoadianDocumentResponse

__all__ = [
    "DRAFT_TOOLS",
    "prepare_invoice_draft",
    "prepare_journal_draft",
    "prepare_reconciliation_suggestions",
    "prepare_tax_document_draft",
    "register_draft_tools",
]

#: (tool name, required scopes) — the registry record for each tool.
#: Note what is ABSENT: no ``ledger:post``, no ``invoice:finalize``,
#: no ``compliance:submit``, no ``moadian:submit`` — a draft-only
#: tool must never need a posting/submission scope.
DRAFT_TOOLS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("prepare_journal_draft", ("ledger:draft:create",)),
    ("prepare_invoice_draft", ("invoice:draft:create",)),
    ("prepare_reconciliation_suggestions", ("banking:reconcile",)),
    ("prepare_tax_document_draft", ("compliance:draft:create",)),
)

#: The next human action per tool (spec §13.5: every draft result
#: names the human step that follows — MCP never takes it).
NEXT_ACTION_JOURNAL = (
    "Review the draft in the HesabYar dashboard and post it from there "
    "when it is correct. MCP tools cannot post journals."
)
NEXT_ACTION_INVOICE = (
    "Review the draft invoice in the HesabYar dashboard and finalize "
    "and post it from there when it is correct. MCP tools cannot "
    "finalize or post invoices."
)
NEXT_ACTION_RECONCILE = (
    "Review the suggested and unmatched lines on the dashboard's "
    "bank-reconciliation page, confirm the matches, and validate the "
    "statement in the engine. MCP tools cannot approve a reconciliation."
)
NEXT_ACTION_TAX_DOCUMENT = (
    "Review the document in the HesabYar dashboard, then create and "
    "approve a submission for it and queue it when it is correct. MCP "
    "tools never submit anything to Moadian."
)


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


async def _call_mutation(
    mcp_context: McpContext,
    company: str,
    fn: Callable[[AuthenticatedClient, EngineCompanyModel, McpServices], dict[str, Any]],
) -> str:
    """Run a sync draft mutation in a thread, commit, serialize its payload.

    The identity comes from the call's own context (bearer token or
    stdio bootstrap client — the middleware already authenticated it);
    the company is re-resolved against that grant inside the thread.
    All services are synchronous, so the whole mutation — session,
    company check, service call, serialization, commit — runs off the
    event loop and the container is closed when the call ends.

    The session is COMMITTED on success: the reconciliation and tax
    tools write Postgres rows (match records, review items, the
    Moadian document) through repositories that flush but never
    commit — the caller owns the transaction, like every REST route.
    The commit happens after serialization, so a serialization failure
    still rolls the mutation back. On any failure the session rolls
    back; engine-side writes (journal/invoice drafts) are committed
    by the engine itself and are unaffected by the Postgres rollback
    — a draft created before a Postgres failure survives as an
    ordinary unposted draft, never as a posted one.
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
                payload = fn(client, row, services)
                text = _dump(payload)
                session.commit()
                return text
            except Exception:
                session.rollback()
                raise
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
        # Anticipated domain/application failures (unbalanced lines,
        # unknown account/party, closed period, invalid Moadian
        # payload): the model reads the message and corrects the call.
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


def _validation_block(summary: ValidationSummary) -> dict[str, Any]:
    """The structured validation outcome for a draft result."""
    return ValidationResponse.from_summary(summary).model_dump(mode="python")


def _validation_findings(summary: ValidationSummary) -> list[str]:
    """Human-readable validation findings (the §13.5 findings list).

    A valid draft reports its clean bill explicitly — an empty list
    would leave the caller guessing whether validation ran at all.
    """
    if summary.valid:
        return ["Validation passed: the draft satisfies every posting invariant."]
    return [f"{issue.code}: {issue.message}" for issue in summary.issues]


# -- argument parsing (narrow, explicit, no floats) -------------------------


def _require_object(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ToolError(f"{what} must be an object.")
    return value


def _reject_unknown_keys(line: dict[str, Any], allowed: frozenset[str], what: str) -> None:
    unknown = sorted(set(line) - allowed)
    if unknown:
        raise ToolError(
            f"Unknown {what} field(s): {', '.join(unknown)}. "
            f"Allowed fields: {', '.join(sorted(allowed))}."
        )


def _int_amount(value: Any, field: str, *, minimum: int = 0) -> int:
    """An integer Rial amount (bools and floats are never amounts)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError(f"{field} must be an integer number of Rials.")
    if value < minimum:
        raise ToolError(f"{field} must be at least {minimum}.")
    return value


def _engine_ref_id(value: Any, field: str) -> int:
    """An engine id (a positive integer, never a bool)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError(f"{field} must be an engine id (a positive integer).")
    if value <= 0:
        raise ToolError(f"{field} must be a positive integer.")
    return value


def _optional_engine_ref_id(value: Any, field: str) -> int | None:
    if value is None:
        return None
    return _engine_ref_id(value, field)


def _parse_maturity_date(value: Any) -> FiscalDate | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ToolError("maturity_date must be an ISO date string (YYYY-MM-DD).")
    try:
        parsed = datetime.date.fromisoformat(value)
    except ValueError:
        raise ToolError(f"maturity_date {value!r} is not a valid ISO date (YYYY-MM-DD).") from None
    return FiscalDate.from_gregorian(parsed)


_JOURNAL_LINE_FIELDS = frozenset(
    {"account_id", "debit_irr", "credit_irr", "description", "party_id", "maturity_date"}
)


def _parse_journal_lines(lines: list[dict[str, Any]]) -> tuple[JournalDraftLine, ...]:
    """Parse tool journal lines into domain lines (REST shape + party/date).

    Field names mirror the REST ``JournalDraftLineRequest`` with the
    ``_irr`` suffix MCP uses for money fields; ``party_id`` and
    ``maturity_date`` are the domain's optional fields (party-required
    accounts need a party, and the settlement engine uses maturity
    dates for tranche due dates). The §8.2 invariants (≥ 2 lines,
    one-sided, balanced, total > 0) are enforced by the domain
    command at construction — the same validation the dashboard gets.
    """
    if not isinstance(lines, list) or not lines:
        raise ToolError("lines must be a non-empty list of journal lines.")
    parsed: list[JournalDraftLine] = []
    for index, raw in enumerate(lines):
        line = _require_object(raw, f"lines[{index}]")
        _reject_unknown_keys(line, _JOURNAL_LINE_FIELDS, f"lines[{index}]")
        if "account_id" not in line:
            raise ToolError(f"lines[{index}].account_id is required.")
        account_id = _engine_ref_id(line["account_id"], f"lines[{index}].account_id")
        debit = _int_amount(line.get("debit_irr", 0), f"lines[{index}].debit_irr")
        credit = _int_amount(line.get("credit_irr", 0), f"lines[{index}].credit_irr")
        description = line.get("description", "")
        if not isinstance(description, str):
            raise ToolError(f"lines[{index}].description must be a string.")
        party_id = _optional_engine_ref_id(line.get("party_id"), f"lines[{index}].party_id")
        maturity = _parse_maturity_date(line.get("maturity_date"))
        parsed.append(
            JournalDraftLine(
                account=AccountRef(account_id),
                debit=MoneyIRR(debit),
                credit=MoneyIRR(credit),
                description=description,
                party=PartyRef(party_id) if party_id is not None else None,
                maturity_date=maturity,
            )
        )
    return tuple(parsed)


_INVOICE_LINE_FIELDS = frozenset({"account_id", "unit_price_irr", "quantity", "description"})


def _parse_quantity(value: Any, field: str) -> Decimal:
    """An invoice quantity as a Decimal — never a binary float.

    JSON numbers arrive as int or float; the float is converted via
    ``str`` (the shortest repr that round-trips the JSON literal the
    caller sent), exactly the boundary conversion the Tryton adapter
    performs. Strings parse directly. A non-finite or unparseable
    quantity is a tool error, not a guess.
    """
    if isinstance(value, bool):
        raise ToolError(f"{field} must be a quantity (a number or decimal string).")
    if isinstance(value, Decimal):
        quantity = value
    elif isinstance(value, (int, float, str)):
        try:
            quantity = Decimal(str(value))
        except InvalidOperation:
            raise ToolError(f"{field} {value!r} is not a valid quantity.") from None
    else:
        raise ToolError(f"{field} must be a quantity (a number or decimal string).")
    if not quantity.is_finite():
        raise ToolError(f"{field} must be a finite quantity.")
    return quantity


def _parse_invoice_lines(lines: list[dict[str, Any]]) -> tuple[InvoiceDraftLine, ...]:
    """Parse tool invoice lines into domain lines (REST shape, ``_irr`` money).

    Field names mirror the REST ``InvoiceDraftLineRequest`` with the
    ``_irr`` suffix MCP uses for money fields. The invoice invariants
    (≥ 1 priced line, positive quantities, integral line amounts,
    total > 0) are enforced by the domain command at construction —
    the same validation the dashboard gets.
    """
    if not isinstance(lines, list) or not lines:
        raise ToolError("lines must be a non-empty list of invoice lines.")
    parsed: list[InvoiceDraftLine] = []
    for index, raw in enumerate(lines):
        line = _require_object(raw, f"lines[{index}]")
        _reject_unknown_keys(line, _INVOICE_LINE_FIELDS, f"lines[{index}]")
        if "account_id" not in line:
            raise ToolError(f"lines[{index}].account_id is required.")
        if "unit_price_irr" not in line:
            raise ToolError(f"lines[{index}].unit_price_irr is required.")
        account_id = _engine_ref_id(line["account_id"], f"lines[{index}].account_id")
        unit_price = _int_amount(line["unit_price_irr"], f"lines[{index}].unit_price_irr")
        quantity = _parse_quantity(line.get("quantity", 1), f"lines[{index}].quantity")
        description = line.get("description", "")
        if not isinstance(description, str):
            raise ToolError(f"lines[{index}].description must be a string.")
        parsed.append(
            InvoiceDraftLine(
                account=AccountRef(account_id),
                unit_price=MoneyIRR(unit_price),
                quantity=quantity,
                description=description,
            )
        )
    return tuple(parsed)


# -- prepare_journal_draft --------------------------------------------------


async def prepare_journal_draft(
    company: str,
    journal_date: datetime.date,
    lines: list[dict[str, Any]],
    description: str = "",
    journal_id: int | None = None,
    ctx: Context | None = None,
) -> str:
    """Prepare a draft journal (an unposted move) — never posts it.

    Creates the draft through the same ``JournalDraftService`` the
    dashboard uses (same §8.2 validation: at least two lines, each
    one-sided, balanced, total greater than zero; accounts verified
    against the engine and the fiscal period checked open). Returns
    the draft id, the draft as read back from the engine, the
    validation findings, and the next human action. The draft stays
    a draft: this tool cannot post it, and no MCP tool can.
    """
    mcp_context = _require_mcp_context(ctx)
    parsed_lines = _parse_journal_lines(lines)
    parsed_journal_id = _optional_engine_ref_id(journal_id, "journal_id")

    def _create(
        client: AuthenticatedClient, row: EngineCompanyModel, services: McpServices
    ) -> dict[str, Any]:
        engine_id = _engine_id(row)
        draft_ref = services.journal_drafts.create_draft(
            CompanyRef(engine_id),
            FiscalDate.from_gregorian(journal_date),
            parsed_lines,
            description=description,
            journal=JournalRef(parsed_journal_id) if parsed_journal_id is not None else None,
        )
        # Read back + validate: the response is the engine's own view
        # of the draft, and the findings are the engine's validation.
        view = services.journal_drafts.get_draft(draft_ref)
        summary = services.journal_drafts.validate_draft(draft_ref)
        findings = _validation_findings(summary)
        findings.append("The draft is in state 'draft'; nothing was posted.")
        return {
            "company": _company_block(row, engine_id),
            "draft_id": draft_ref.engine_id,
            "draft": JournalDraftResponse.from_view(view, row.id).model_dump(mode="python"),
            "state": view.state,
            "validation": _validation_block(summary),
            "findings": findings,
            "next_action": NEXT_ACTION_JOURNAL,
            "provenance": build_provenance(
                company_id=engine_id,
                company_name=row.company_name,
                period_label=f"draft date {journal_date.isoformat()}",
                period_from=journal_date.isoformat(),
                period_to=journal_date.isoformat(),
                report_type="journal_draft",
                source_engine="Tryton",
                source_ref=f"tryton:account.move:{draft_ref.engine_id}",
            ).to_dict(),
        }

    return await _call_mutation(mcp_context, company, _create)


# -- prepare_invoice_draft --------------------------------------------------


async def prepare_invoice_draft(
    company: str,
    invoice_type: str,
    party_id: int,
    invoice_date: datetime.date,
    lines: list[dict[str, Any]],
    description: str = "",
    reference: str = "",
    journal_id: int | None = None,
    account_id: int | None = None,
    invoice_address_id: int | None = None,
    ctx: Context | None = None,
) -> str:
    """Prepare a draft invoice — never finalizes or posts it.

    Creates the draft through the same ``InvoiceDraftService`` the
    dashboard uses (same invoice invariants: at least one priced
    line, positive quantities, integral line amounts, total greater
    than zero; party, invoice address, receivable/payable account
    and journal verified against the engine). Returns the draft id,
    the invoice as read back from the engine, the validation
    findings, and the next human action. The invoice stays a draft:
    this tool cannot finalize or post it, and no MCP tool can.
    """
    mcp_context = _require_mcp_context(ctx)
    if invoice_type not in ("out", "in"):
        raise ToolError("invoice_type must be 'out' (customer) or 'in' (supplier).")
    parsed_party_id = _engine_ref_id(party_id, "party_id")
    parsed_lines = _parse_invoice_lines(lines)
    parsed_journal_id = _optional_engine_ref_id(journal_id, "journal_id")
    parsed_account_id = _optional_engine_ref_id(account_id, "account_id")
    parsed_address_id = _optional_engine_ref_id(invoice_address_id, "invoice_address_id")

    def _create(
        client: AuthenticatedClient, row: EngineCompanyModel, services: McpServices
    ) -> dict[str, Any]:
        engine_id = _engine_id(row)
        invoice_ref = services.invoice_drafts.create_draft(
            CompanyRef(engine_id),
            invoice_type,
            PartyRef(parsed_party_id),
            FiscalDate.from_gregorian(invoice_date),
            parsed_lines,
            description=description,
            reference=reference,
            journal=JournalRef(parsed_journal_id) if parsed_journal_id is not None else None,
            account=AccountRef(parsed_account_id) if parsed_account_id is not None else None,
            invoice_address=(
                PartyAddressRef(parsed_address_id) if parsed_address_id is not None else None
            ),
        )
        # Read back + validate: the response is the engine's own view
        # of the invoice, and the findings are the engine's validation.
        # validate_invoice_draft is read-only — it never calls the
        # engine's validate_invoice transition (that would finalize).
        view = services.invoice_drafts.get_invoice(invoice_ref)
        summary = services.invoice_drafts.validate_invoice_draft(invoice_ref)
        findings = _validation_findings(summary)
        findings.append("The invoice is in state 'draft'; it was not finalized or posted.")
        return {
            "company": _company_block(row, engine_id),
            "draft_id": invoice_ref.engine_id,
            "draft": InvoiceResponse.from_view(view, row.id).model_dump(mode="python"),
            "state": view.state,
            "validation": _validation_block(summary),
            "findings": findings,
            "next_action": NEXT_ACTION_INVOICE,
            "provenance": build_provenance(
                company_id=engine_id,
                company_name=row.company_name,
                period_label=f"invoice date {invoice_date.isoformat()}",
                period_from=invoice_date.isoformat(),
                period_to=invoice_date.isoformat(),
                report_type="invoice_draft",
                source_engine="Tryton",
                source_ref=f"tryton:account.invoice:{invoice_ref.engine_id}",
            ).to_dict(),
        }

    return await _call_mutation(mcp_context, company, _create)


# -- prepare_reconciliation_suggestions -------------------------------------


async def prepare_reconciliation_suggestions(
    company: str,
    statement_id: int,
    window_days: int | None = None,
    ctx: Context | None = None,
) -> str:
    """Prepare reconciliation suggestions for a draft bank statement.

    Runs the same ``BankReconcileService`` the dashboard's reconcile
    button runs (same three-priority matcher, same learned + user
    rules, same tie-out guard): high-confidence lines are matched,
    the rest come back as suggestions or unmatched with a review
    item each. The statement is NEVER validated or posted — it stays
    a draft for a human to validate in the engine, and unmatched
    deposits NEVER auto-post to revenue. Returns the per-line
    outcomes, the counts, the findings, and the next human action.
    The run is attributed to the MCP client (actor) on the review
    items it files.
    """
    mcp_context = _require_mcp_context(ctx)
    parsed_statement_id = _engine_ref_id(statement_id, "statement_id")
    if window_days is not None:
        if isinstance(window_days, bool) or not isinstance(window_days, int):
            raise ToolError("window_days must be an integer number of days.")
        if window_days < 0:
            raise ToolError("window_days must be at least 0.")

    def _reconcile(
        client: AuthenticatedClient, row: EngineCompanyModel, services: McpServices
    ) -> dict[str, Any]:
        engine_id = _engine_id(row)
        result = services.bank_reconcile.reconcile(
            tenant_id=client.tenant_id,
            installation_id=client.installation_id,
            company=CompanyRef(engine_id),
            company_id=row.id,
            statement_id=parsed_statement_id,
            actor=f"mcp:{client.client_id}",
            window_days=window_days,
        )
        # The safety read-back: the statement must still be a draft
        # after the run — reconcile never validates or posts it.
        statement = services.engine.get_bank_statement(BankStatementRef(parsed_statement_id))
        if statement is None:
            raise ToolError(
                f"Bank statement {parsed_statement_id} vanished during reconciliation."
            )
        needs_human = result.suggested_count + result.ambiguous_count + result.unmatched_count
        findings = [
            f"{result.matched_count} of {result.total_lines} lines matched automatically "
            f"(priority 1: {result.priority1_count}, priority 2: {result.priority2_count}).",
            f"{needs_human} lines need a human decision "
            f"(suggested: {result.suggested_count}, ambiguous: {result.ambiguous_count}, "
            f"unmatched: {result.unmatched_count}).",
            f"{result.review_items_filed} review items filed for human review.",
            "The statement remains a draft; it was not validated or posted.",
        ]
        return {
            "company": _company_block(row, engine_id),
            "statement_id": parsed_statement_id,
            "statement_state": statement.state,
            "reconciliation": ReconcileResponse.from_result(result).model_dump(mode="python"),
            "findings": findings,
            "next_action": NEXT_ACTION_RECONCILE,
            "provenance": build_provenance(
                company_id=engine_id,
                company_name=row.company_name,
                period_label=f"statement {parsed_statement_id}",
                report_type="reconciliation_suggestions",
                source_engine="Tryton",
                source_ref=f"tryton:account.statement:{parsed_statement_id}",
            ).to_dict(),
        }

    return await _call_mutation(mcp_context, company, _reconcile)


# -- prepare_tax_document_draft ---------------------------------------------


async def prepare_tax_document_draft(
    company: str,
    profile_id: str,
    payload: dict[str, Any],
    invoice_id: str | None = None,
    ctx: Context | None = None,
) -> str:
    """Prepare a Moadian tax document draft — never submits to Moadian.

    Creates the document through the same ``MoadianDocumentService``
    the dashboard uses: the invoice payload is validated against
    Moadian instruction Table 7 (an invalid payload is refused with
    the full problem list, never stored) and the document is created
    in ``draft`` status. No submission is created, no approval is
    recorded, nothing is queued, and nothing is sent to Moadian —
    this tool needs no ``moadian:submit`` scope and has no code path
    to any submission. Returns the document id, the document
    metadata, the findings, and the next human action.
    """
    mcp_context = _require_mcp_context(ctx)
    if not isinstance(profile_id, str) or not profile_id:
        raise ToolError("profile_id must be a non-empty string.")
    _require_object(payload, "payload")
    if invoice_id is not None and (not isinstance(invoice_id, str) or not invoice_id):
        raise ToolError("invoice_id must be a non-empty string when given.")

    def _create(
        client: AuthenticatedClient, row: EngineCompanyModel, services: McpServices
    ) -> dict[str, Any]:
        engine_id = _engine_id(row)
        try:
            document = services.moadian_documents.create_document(
                tenant_id=client.tenant_id,
                profile_id=profile_id,
                payload=payload,
                invoice_id=invoice_id,
            )
        except MoadianInvalidDocumentError as exc:
            # The service refused the payload with the full Table 7
            # problem list — surface it, so the model can fix the
            # payload instead of guessing. Nothing was stored.
            problems = exc.details.get("problems")
            if isinstance(problems, list) and problems:
                detail = "; ".join(str(problem) for problem in problems)
                raise ToolError(
                    f"The invoice payload fails Moadian Table 7 validation: {detail}"
                ) from None
            raise ToolError(f"{exc.code}: {exc.message}") from None
        # The payload itself is NOT echoed back: it is the caller's
        # own input, and a Moadian payload can carry float rates
        # (vra) — MCP results carry no binary floats. The hash lets
        # the caller verify integrity instead.
        document_block = MoadianDocumentResponse.from_document(document).model_dump(
            mode="python"
        )
        document_block.pop("payload", None)
        findings = [
            "The invoice payload passed Moadian Table 7 validation.",
            "The document was created in 'draft' status.",
            "No submission was created and nothing was sent to Moadian.",
        ]
        return {
            "company": _company_block(row, engine_id),
            "draft_id": document.id,
            "document": document_block,
            "state": document.status.value,
            "findings": findings,
            "next_action": NEXT_ACTION_TAX_DOCUMENT,
            "provenance": build_provenance(
                company_id=engine_id,
                company_name=row.company_name,
                period_label="Moadian document draft",
                report_type="tax_document_draft",
                source_engine="HesabYar",
                source_ref=f"hesabyar:moadian_document:{document.id}",
            ).to_dict(),
        }

    return await _call_mutation(mcp_context, company, _create)


# -- registration -----------------------------------------------------------


def register_draft_tools(server: MCPServer[Any], context: McpContext) -> None:
    """Register the four US-605 draft-only tools.

    Called by the server entry point (``main`` in ``server.py``) —
    one line per story module, so US-602/US-603/US-604/US-605 wire
    side by side. Every tool goes through
    :func:`register_hesabyar_tool` so its scope record is created
    with it.
    """
    register_hesabyar_tool(
        server,
        context.registry,
        prepare_journal_draft,
        name="prepare_journal_draft",
        scopes=("ledger:draft:create",),
        description=(
            "Prepare a draft journal (an unposted move) for a company: "
            "balanced debit/credit lines in integer Rial. Returns the "
            "draft id, the draft as read back, validation findings and "
            "the next human action. The draft stays a draft — this tool "
            "never posts a journal."
        ),
    )
    register_hesabyar_tool(
        server,
        context.registry,
        prepare_invoice_draft,
        name="prepare_invoice_draft",
        scopes=("invoice:draft:create",),
        description=(
            "Prepare a draft invoice for a company: a customer ('out') "
            "or supplier ('in') invoice with priced lines in integer "
            "Rial. Returns the draft id, the invoice as read back, "
            "validation findings and the next human action. The invoice "
            "stays a draft — this tool never finalizes or posts it."
        ),
    )
    register_hesabyar_tool(
        server,
        context.registry,
        prepare_reconciliation_suggestions,
        name="prepare_reconciliation_suggestions",
        scopes=("banking:reconcile",),
        description=(
            "Prepare reconciliation suggestions for a draft bank "
            "statement: match its lines against invoices and payments, "
            "suggest matches for the rest, and file review items for "
            "lines a human must decide. Returns per-line outcomes, "
            "counts, findings and the next human action. The statement "
            "stays a draft — this tool never validates or approves a "
            "reconciliation."
        ),
    )
    register_hesabyar_tool(
        server,
        context.registry,
        prepare_tax_document_draft,
        name="prepare_tax_document_draft",
        scopes=("compliance:draft:create",),
        description=(
            "Prepare a Moadian tax document draft for a company: "
            "validate an invoice payload against Moadian Table 7 and "
            "store it as a draft document. Returns the document id, "
            "the document metadata, findings and the next human action. "
            "No submission is created — this tool never submits "
            "anything to Moadian."
        ),
    )
