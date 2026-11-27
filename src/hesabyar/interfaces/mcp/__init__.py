"""HesabYar MCP server (US-601, spec §13).

Another interface to the application layer, parallel to
:mod:`hesabyar.interfaces.rest`: the official Python MCP SDK v2
(``MCPServer``), stdio + Streamable HTTP transports, the same API-key
+ scope auth as REST, the same application services, and an audit row
for every call.

Layout:

- :mod:`server` — the ``MCPServer`` factory + CLI entry points.
- :mod:`context` — :class:`McpContext`, the lifespan object every
  handler and the middleware share.
- :mod:`auth` — the SDK ``TokenVerifier`` over the API-key service,
  identity claims, and the company-grant check.
- :mod:`middleware` — transport-agnostic enforcement (auth, scopes,
  company) + audit.
- :mod:`registry` — required scopes per tool/resource/prompt.
- :mod:`audit` — the ``ai_access_audit`` writer.
- :mod:`services` — the per-call application-service container.
- :mod:`http` — the Streamable HTTP app (DNS-rebinding protection,
  body-size + rate limits, TLS) and the uvicorn runner.
- :mod:`rate_limit` — fixed-window rate limiting.
- :mod:`errors` — typed MCP (JSON-RPC) errors.
"""

from __future__ import annotations

from hesabyar.interfaces.mcp.audit import AiAccessAuditWriter
from hesabyar.interfaces.mcp.auth import MCP_BASE_SCOPE, HesabYarTokenVerifier
from hesabyar.interfaces.mcp.context import McpContext, build_mcp_context, current_client
from hesabyar.interfaces.mcp.http import build_streamable_http_app, serve_streamable_http
from hesabyar.interfaces.mcp.middleware import HesabYarMcpMiddleware
from hesabyar.interfaces.mcp.registry import ToolRegistry, register_hesabyar_tool
from hesabyar.interfaces.mcp.server import SERVER_NAME, create_mcp_server, main, run_over_streams
from hesabyar.interfaces.mcp.services import McpServices

__all__ = [
    "MCP_BASE_SCOPE",
    "SERVER_NAME",
    "AiAccessAuditWriter",
    "HesabYarMcpMiddleware",
    "HesabYarTokenVerifier",
    "McpContext",
    "McpServices",
    "ToolRegistry",
    "build_mcp_context",
    "build_streamable_http_app",
    "create_mcp_server",
    "current_client",
    "main",
    "register_hesabyar_tool",
    "run_over_streams",
    "serve_streamable_http",
]
