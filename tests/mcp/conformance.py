"""Full-surface MCP conformance helpers (US-606, spec §13.2 + §17 phase 6).

The phase exit criterion is a conformance suite over the WHOLE server:
every tool + resource + prompt against the MCP 2026-07-28 spec, with
two independent clients:

- the reference SDK client (``ClientSession``, via the harness), and
- a host-style client speaking raw JSON-RPC 2.0 by hand
  (:class:`RawJsonRpcClient`) — over the in-memory streams AND over
  the real stdio subprocess (exactly the wire a Claude/ChatGPT-style
  host speaks). Live interop against a real AI host stays UNVERIFIED
  (see docs/mcp-server.md); these clients prove the server speaks
  plain protocol, not just SDK-to-SDK.

WIRING FACT: :func:`create_mcp_server` carries ONLY US-602's 14 core
tools; ``main()`` adds US-603's 5 phase-5 tools + US-604's 6 resources
+ 6 prompts. :func:`make_full_surface_server` builds the same full
surface for conformance — factory + ``register_phase5_tools`` +
``register_resources`` + ``register_prompts``. It never calls
``register_core_tools`` again (double-registration behaviour is
unverified). US-605's 4 draft tools are built in parallel and are NOT
part of this surface; the coordinator extends the expected sets when
they land.

The expected surface is stated here INDEPENDENTLY of the per-story
tests (its own property/required maps, its own counts), so a drift in
either place fails loudly instead of silently agreeing.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp.shared.memory import create_client_server_memory_streams
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCNotification, JSONRPCRequest
from sqlalchemy.orm import Session

from hesabyar.infrastructure.config import AppSettings
from hesabyar.interfaces.mcp import McpContext, create_mcp_server, run_over_streams
from hesabyar.interfaces.mcp.prompts import PROMPT_DEFINITIONS, register_prompts
from hesabyar.interfaces.mcp.resources import RESOURCE_DEFINITIONS, register_resources
from hesabyar.interfaces.mcp.tools_core import CORE_TOOL_NAMES
from hesabyar.interfaces.mcp.tools_drafts import DRAFT_TOOLS, register_draft_tools
from hesabyar.interfaces.mcp.tools_phase5 import PHASE5_TOOLS, register_phase5_tools
from tests.mcp.conftest import TEST_DB_URL
from tests.mcp.harness import authenticate_secret

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The MCP spec revision HesabYar targets (spec §13.2). The pinned SDK
#: must agree — asserted in the conformance suite.
MCP_SPEC_VERSION = "2026-07-28"

#: The stdio subprocess must finish well inside the CI per-test budget.
STDIO_TIMEOUT_SECONDS = 60.0

# -- expected surface (Wave B + US-605) ---------------------------------------

#: The 23 tools of the full surface (14 core + 5 phase-5 + 4 draft-only).
EXPECTED_TOOL_NAMES: frozenset[str] = (
    CORE_TOOL_NAMES
    | {name for name, _ in PHASE5_TOOLS}
    | {name for name, _ in DRAFT_TOOLS}
)
EXPECTED_TOOL_COUNT = 23

#: The 6 resource templates.
EXPECTED_RESOURCE_TEMPLATES: frozenset[str] = frozenset(
    d.uri_template for d in RESOURCE_DEFINITIONS
)
EXPECTED_RESOURCE_COUNT = 6

#: The 6 prompts.
EXPECTED_PROMPT_NAMES: frozenset[str] = frozenset(d.name for d in PROMPT_DEFINITIONS)
EXPECTED_PROMPT_COUNT = 6

#: Expected input-schema properties per tool (narrow arguments only).
EXPECTED_TOOL_PROPERTIES: dict[str, frozenset[str]] = {
    "list_companies": frozenset(),
    "get_accounting_summary": frozenset({"company", "date_from", "date_to"}),
    "get_trial_balance": frozenset({"company", "date_from", "date_to"}),
    "get_general_ledger": frozenset({"company", "date_from", "date_to", "account_id"}),
    "get_balance_sheet": frozenset({"company", "as_of", "comparison_as_of"}),
    "get_income_statement": frozenset(
        {"company", "date_from", "date_to", "comparison_from", "comparison_to"}
    ),
    "get_cash_flow": frozenset({"company", "date_from", "date_to"}),
    "get_receivables_aging": frozenset({"company", "as_of", "party_id"}),
    "get_payables_aging": frozenset({"company", "as_of", "party_id"}),
    "get_bank_reconciliation_status": frozenset({"company", "date_from", "date_to"}),
    "get_tax_compliance_status": frozenset({"company", "as_of"}),
    "validate_financial_statement": frozenset(
        {"company", "period_start", "period_end", "standard_codes"}
    ),
    "explain_accounting_rule": frozenset({"standard_code", "rule_id"}),
    "list_pending_review_items": frozenset({"company", "item_type", "severity", "limit"}),
    "get_settlement_status": frozenset(
        {
            "company",
            "channel_key",
            "state",
            "sales_period",
            "due_from",
            "due_to",
            "include_tranches",
            "limit",
        }
    ),
    "get_cheque_status": frozenset(
        {"company", "kind", "state", "due_within_days", "include_collateral", "limit"}
    ),
    "get_packaging_costs": frozenset(
        {"company", "order_ref", "include_consumptions", "include_shipment_costs", "limit"}
    ),
    "get_cashbox_status": frozenset(
        {
            "company",
            "date_from",
            "date_to",
            "cash_box_id",
            "include_boxes",
            "include_sessions",
            "limit",
        }
    ),
    "get_cash_forecast": frozenset({"company", "date_from", "date_to"}),
    "prepare_journal_draft": frozenset(
        {"company", "journal_date", "lines", "description", "journal_id"}
    ),
    "prepare_invoice_draft": frozenset(
        {
            "company",
            "invoice_type",
            "party_id",
            "invoice_date",
            "lines",
            "description",
            "reference",
            "journal_id",
            "account_id",
            "invoice_address_id",
        }
    ),
    "prepare_reconciliation_suggestions": frozenset(
        {"company", "statement_id", "window_days"}
    ),
    "prepare_tax_document_draft": frozenset(
        {"company", "profile_id", "payload", "invoice_id"}
    ),
}

#: Expected required input-schema properties per tool.
EXPECTED_TOOL_REQUIRED: dict[str, frozenset[str]] = {
    "list_companies": frozenset(),
    "get_accounting_summary": frozenset({"company", "date_from", "date_to"}),
    "get_trial_balance": frozenset({"company", "date_from", "date_to"}),
    "get_general_ledger": frozenset({"company", "date_from", "date_to"}),
    "get_balance_sheet": frozenset({"company", "as_of"}),
    "get_income_statement": frozenset({"company", "date_from", "date_to"}),
    "get_cash_flow": frozenset({"company", "date_from", "date_to"}),
    "get_receivables_aging": frozenset({"company", "as_of"}),
    "get_payables_aging": frozenset({"company", "as_of"}),
    "get_bank_reconciliation_status": frozenset({"company", "date_from", "date_to"}),
    "get_tax_compliance_status": frozenset({"company", "as_of"}),
    "validate_financial_statement": frozenset({"company", "period_start", "period_end"}),
    "explain_accounting_rule": frozenset({"standard_code"}),
    "list_pending_review_items": frozenset({"company"}),
    "get_settlement_status": frozenset({"company"}),
    "get_cheque_status": frozenset({"company"}),
    "get_packaging_costs": frozenset({"company"}),
    "get_cashbox_status": frozenset({"company", "date_from", "date_to"}),
    "get_cash_forecast": frozenset({"company", "date_from", "date_to"}),
    "prepare_journal_draft": frozenset({"company", "journal_date", "lines"}),
    "prepare_invoice_draft": frozenset(
        {"company", "invoice_type", "party_id", "invoice_date", "lines"}
    ),
    "prepare_reconciliation_suggestions": frozenset({"company", "statement_id"}),
    "prepare_tax_document_draft": frozenset({"company", "profile_id", "payload"}),
}

# -- full-surface server -------------------------------------------------------


def make_full_surface_server(
    session_factory: Callable[[], Session],
    settings: AppSettings,
    *,
    bootstrap_secret: str | None = None,
    transport: str = "memory",
) -> tuple[Any, McpContext]:
    """Build a server (+ context) carrying the WHOLE production surface.

    Factory (14 core tools) + phase-5 tools + draft tools + resources
    + prompts — the same surface ``main()`` serves, minus the
    stdio/HTTP transport. No fixture echo tool: the conformance
    surface must be exactly the production surface.
    """
    context = McpContext(
        settings=settings,
        session_factory=session_factory,
        transport=transport,
    )
    if bootstrap_secret is not None:
        context.bootstrap_client = authenticate_secret(session_factory, settings, bootstrap_secret)
    server = create_mcp_server(context)
    register_phase5_tools(server, context)
    register_draft_tools(server, context)
    register_resources(server, context)
    register_prompts(server, context)
    return server, context


# -- raw host-style client -----------------------------------------------------


class RawMcpError(Exception):
    """A JSON-RPC error response from the server (host-style client)."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"MCP error {code}: {message}")
        self.code = code
        self.message = message


class RawJsonRpcClient:
    """A minimal MCP client speaking raw JSON-RPC 2.0 — no SDK client.

    Built on two callables (send one JSON-RPC message dict, receive
    the next one) so the same client drives both the in-memory
    streams and the real stdio subprocess. It implements only what
    conformance needs: initialize, request/response with id
    correlation, and notifications. Server-to-client requests are an
    error (the HesabYar server sends none).
    """

    def __init__(
        self,
        send: Callable[[dict[str, Any]], Any],
        receive: Callable[[], Any],
    ) -> None:
        self._send = send
        self._receive = receive
        self._next_id = 0

    async def initialize(self) -> dict[str, Any]:
        """Run the MCP initialize handshake; return the server's result."""
        result = await self.request(
            "initialize",
            {
                "protocolVersion": MCP_SPEC_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "hesabyar-conformance-raw", "version": "1.0"},
            },
        )
        await self.notify("notifications/initialized")
        return result

    async def request(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Send one request; return its ``result`` (raise on error)."""
        self._next_id += 1
        request_id = self._next_id
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        await self._send(message)
        while True:
            response = await self._receive()
            # Notifications carry no id: skip them (the server sends
            # none today, but a host must tolerate them).
            if "id" not in response:
                continue
            if response["id"] != request_id:
                raise RawMcpError(
                    -32603, f"unexpected response id {response['id']!r} (wanted {request_id})"
                )
            if "error" in response:
                error = response["error"]
                raise RawMcpError(int(error["code"]), str(error["message"]))
            if "result" in response:
                result = response["result"]
                assert isinstance(result, dict), f"result is not an object: {result!r}"
                return result
            raise RawMcpError(-32603, f"malformed response (no result/error): {response!r}")

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send one notification (no response expected)."""
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        await self._send(message)


@asynccontextmanager
async def connect_raw_memory(server: Any) -> AsyncIterator[RawJsonRpcClient]:
    """Connect a raw client to ``server`` over the SDK's memory streams."""
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        task = asyncio.create_task(
            run_over_streams(server, server_streams[0], server_streams[1])
        )
        try:
            read_stream, write_stream = client_streams

            async def _send(message: dict[str, Any]) -> None:
                if "id" in message:
                    rpc: JSONRPCMessage = JSONRPCRequest(
                        jsonrpc="2.0",
                        id=message["id"],
                        method=message["method"],
                        params=message.get("params"),
                    )
                else:
                    rpc = JSONRPCNotification(
                        jsonrpc="2.0",
                        method=message["method"],
                        params=message.get("params"),
                    )
                await write_stream.send(SessionMessage(message=rpc))

            async def _receive() -> dict[str, Any]:
                received = await read_stream.receive()
                if isinstance(received, Exception):
                    raise received
                return received.message.model_dump(mode="json")

            yield RawJsonRpcClient(_send, _receive)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


@asynccontextmanager
async def connect_raw_stdio(secret: str) -> AsyncIterator[RawJsonRpcClient]:
    """Connect a raw client to the REAL stdio server subprocess.

    Newline-delimited JSON-RPC over pipes — exactly the wire a
    Claude/ChatGPT-style host speaks. The server authenticates
    ``secret`` at startup (its stderr is discarded: a failed startup
    surfaces as EOF on stdout, and the exit code is checked by the
    caller through :func:`stdio_exit_code` when needed).
    """
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "hesabyar.interfaces.mcp",
        "--transport",
        "stdio",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=str(REPO_ROOT),
        env={
            **os.environ,
            "HESABYAR_DATABASE_URL": TEST_DB_URL,
            "HESABYAR_MCP__STDIO_API_KEY": secret,
        },
    )
    assert proc.stdin is not None and proc.stdout is not None
    try:

        async def _send(message: dict[str, Any]) -> None:
            assert proc.stdin is not None
            proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
            await proc.stdin.drain()

        async def _receive() -> dict[str, Any]:
            assert proc.stdout is not None
            line = await proc.stdout.readline()
            if not line:
                raise RuntimeError(
                    "stdio server closed the connection "
                    f"(exit code {proc.returncode}); the startup key may be invalid."
                )
            parsed = json.loads(line.decode("utf-8"))
            assert isinstance(parsed, dict), f"stdio line is not an object: {parsed!r}"
            return parsed

        yield RawJsonRpcClient(_send, _receive)
    finally:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=10.0)
            except TimeoutError:
                proc.kill()
                await proc.wait()
