"""MCP server factory + entry points (US-601, spec §13.2/§13.3).

The server is the official Python MCP SDK v2 ``MCPServer`` (mcp 2.x —
``FastMCP`` was renamed to ``MCPServer`` in v2), targeting the MCP
2026-07-28 specification. It is another interface to the application
layer, parallel to REST: the same API-key auth, the same scopes, the
same application services, the same audit discipline.

Transports:

- **stdio** (local developer): the process authenticates ONE API key
  at startup (``--api-key`` or ``HESABYAR_MCP__STDIO_API_KEY``) and
  runs as that key's grant — there is no per-request auth on stdio.
  Startup fails fast when the key is missing, invalid, revoked,
  expired, or lacks ``mcp:connect``.
- **Streamable HTTP** (remote/production): every HTTP request is
  authenticated by the SDK's bearer backend against
  :class:`HesabYarTokenVerifier`; see :mod:`hesabyar.interfaces.mcp.http`.

Run it::

    python -m hesabyar.interfaces.mcp --transport stdio
    python -m hesabyar.interfaces.mcp --transport streamable-http --port 8002

(``python -m hesabyar.interfaces.mcp.server`` works too, with a
benign runpy warning — the package entry point is canonical.)

US-601 registers no production tools: the tool registry starts empty
and the real tools land in US-602–US-605 via
:func:`hesabyar.interfaces.mcp.registry.register_hesabyar_tool`.
US-602's 14 read-only tools (``tools_core.register_core_tools``) are
wired into :func:`create_mcp_server` below, so the factory — and the
in-memory conformance harness built on it — always carries them.
US-603's Phase 5 tools and US-604's resources + prompts are wired in
:func:`main` instead, so tests can register them selectively; the CLI
server (stdio + Streamable HTTP) gets the full surface. US-605 wires
its draft tools the same way as US-603.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from pydantic import AnyHttpUrl

from hesabyar import __version__
from hesabyar.infrastructure.auth.errors import AuthenticationError
from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.infrastructure.auth.keys import ApiKeyCrypto
from hesabyar.infrastructure.config import AppSettings, get_settings
from hesabyar.interfaces.mcp.auth import MCP_BASE_SCOPE, HesabYarTokenVerifier
from hesabyar.interfaces.mcp.context import McpContext, build_mcp_context
from hesabyar.interfaces.mcp.http import build_streamable_http_app, serve_streamable_http
from hesabyar.interfaces.mcp.middleware import HesabYarMcpMiddleware
from hesabyar.interfaces.mcp.prompts import register_prompts
from hesabyar.interfaces.mcp.resources import register_resources
from hesabyar.interfaces.mcp.tools_core import register_core_tools
from hesabyar.interfaces.mcp.tools_drafts import register_draft_tools
from hesabyar.interfaces.mcp.tools_phase5 import register_phase5_tools

SERVER_NAME = "hesabyar"

SERVER_INSTRUCTIONS = (
    "HesabYar MCP server: retrieve approved Iranian accounting reports "
    "and prepare drafts through the same application services as the "
    "dashboard. All amounts are integer Rial — never binary floating "
    "point. The model interprets supplied data; it does not create "
    "authoritative financial facts."
)


def create_mcp_server(context: McpContext) -> MCPServer[McpContext]:
    """Build the HesabYar MCP server bound to ``context``.

    The lifespan yields the context itself, so every tool handler
    reaches it as ``ctx.request_context.lifespan_context`` and the
    middleware as ``ctx.lifespan_context``. Auth is the API-key
    verifier + ``AuthSettings`` with the base scope required and the
    token resource validated against the server's public URL.
    """
    public_url = context.settings.mcp.public_url
    verifier = HesabYarTokenVerifier(
        context.session_factory,
        ApiKeyCrypto(context.settings.auth.api_key_pepper),
        resource_url=public_url,
    )
    auth = AuthSettings(
        # The MCP server is its own issuer for the self-issued API-key
        # credentials (there is no separate OAuth authorization server;
        # see docs/mcp-server.md).
        issuer_url=AnyHttpUrl(public_url),
        resource_server_url=AnyHttpUrl(public_url),
        required_scopes=[MCP_BASE_SCOPE],
        validate_token_resource=True,
    )

    @asynccontextmanager
    async def _lifespan(_server: MCPServer[McpContext]) -> AsyncIterator[McpContext]:
        yield context

    server = MCPServer(
        name=SERVER_NAME,
        title="HesabYar",
        description=(
            "Deterministic Iranian accounting ledger and tax compliance "
            "engine — MCP access to approved reports and draft preparation."
        ),
        instructions=SERVER_INSTRUCTIONS,
        version=__version__,
        auth=auth,
        token_verifier=verifier,
        lifespan=_lifespan,
        middleware=[HesabYarMcpMiddleware(context)],
    )
    # US-602: the 14 read-only tools of spec §13.4. US-603's tools and
    # US-604's resources/prompts are wired in main() so tests can
    # register them selectively (see the module docstring).
    register_core_tools(server, context)
    return server


async def run_over_streams(server: MCPServer[Any], read_stream: Any, write_stream: Any) -> None:
    """Run ``server`` over explicit message streams.

    The transport-agnostic entry point: stdio uses the SDK's
    ``stdio_server()`` streams, and the conformance tests use the
    SDK's in-memory streams (``mcp.shared.memory``). Reaches the
    low-level server through the SDK's ``_lowlevel_server`` attribute
    (verified present on the pinned mcp 2.3.0; there is no public
    run-over-streams API in SDK v2).
    """
    lowlevel = server._lowlevel_server
    await lowlevel.run(
        read_stream,
        write_stream,
        lowlevel.create_initialization_options(),
    )


def _authenticate_stdio_key(context: McpContext, secret: str) -> AuthenticatedClient | None:
    """Authenticate the stdio bootstrap key; None for every failure mode."""
    session = context.session_factory()
    try:
        try:
            client = context.api_key_service().authenticate(session, secret=secret)
        except AuthenticationError:
            session.rollback()
            return None
        session.commit()
        return client
    finally:
        session.close()


def main(argv: list[str] | None = None) -> int:
    """CLI entry point (``python -m hesabyar.interfaces.mcp.server``)."""
    parser = argparse.ArgumentParser(
        prog="hesabyar-mcp",
        description="HesabYar MCP server (SDK v2, MCP 2026-07-28).",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="Transport to serve (default: stdio).",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Streamable HTTP bind address (default: settings.mcp.host).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Streamable HTTP port (default: settings.mcp.port).",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help=(
            "API key for stdio mode (default: HESABYAR_MCP__STDIO_API_KEY). "
            "The stdio process runs as this key's grant. Prefer the "
            "environment variable — a CLI argument is visible in the "
            "process list."
        ),
    )
    args = parser.parse_args(argv)
    settings: AppSettings = get_settings()
    context = build_mcp_context(settings, transport=args.transport)
    try:
        if args.transport == "stdio":
            secret = args.api_key or settings.mcp.stdio_api_key
            if not secret:
                print(
                    "hesabyar-mcp: stdio mode needs an API key: pass --api-key "
                    "or set HESABYAR_MCP__STDIO_API_KEY.",
                    file=sys.stderr,
                )
                return 2
            client = _authenticate_stdio_key(context, secret)
            if client is None:
                print(
                    "hesabyar-mcp: the stdio API key is invalid, revoked or expired.",
                    file=sys.stderr,
                )
                return 1
            if not client.has_scope(MCP_BASE_SCOPE):
                print(
                    f"hesabyar-mcp: the stdio API key lacks the required scope: {MCP_BASE_SCOPE}.",
                    file=sys.stderr,
                )
                return 1
            context.bootstrap_client = client
        server = create_mcp_server(context)
        # US-603: the five Phase 5 report tools. US-604: resources +
        # prompts. US-605: the four draft-only tools. (US-602's core
        # tools are wired in create_mcp_server.)
        register_phase5_tools(server, context)
        register_draft_tools(server, context)
        register_resources(server, context)
        register_prompts(server, context)
        if args.transport == "stdio":
            asyncio.run(server.run_stdio_async())
        else:
            app = build_streamable_http_app(server, settings)
            serve_streamable_http(app, settings, host=args.host, port=args.port)
        return 0
    finally:
        context.close()


if __name__ == "__main__":
    raise SystemExit(main())
