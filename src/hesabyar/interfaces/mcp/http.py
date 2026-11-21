"""Streamable HTTP transport for the MCP server (US-601, spec §13.8).

:func:`build_streamable_http_app` wraps the SDK's
``MCPServer.streamable_http_app()`` (which already installs the bearer
auth backend, the auth-context middleware and the
``RequireAuthMiddleware`` 401/403 gate) with HesabYar's transport
protections:

- **DNS rebinding protection** — the SDK's
  ``TransportSecuritySettings`` validates Host and Origin against the
  configured allow-lists (``settings.mcp.allowed_hosts`` /
  ``allowed_origins``). Always enabled, including on localhost.
- **Request-size limits** — ``max_request_body_size`` from settings
  (SDK default 4 MiB).
- **Rate limits** — :class:`RateLimitMiddleware` (pure ASGI), per
  API-key prefix per minute, falling back to client IP for
  unauthenticated requests. A limited request gets 429 + ``Retry-After``.
- **Timeouts** — the SDK session idle timeout (a session with no
  request for that long is reaped) plus uvicorn's keep-alive handling;
  per-call Tryton timeouts come from ``settings.tryton``.
- **TLS** — :func:`serve_streamable_http` passes the configured
  certificate/key to uvicorn when set; production MUST serve behind
  TLS (directly or via a terminating proxy).

No legacy HTTP+SSE transport is built (spec §13.2).
"""

from __future__ import annotations

import json
from typing import Any

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp, Receive, Scope, Send

from hesabyar.infrastructure.auth.keys import extract_key_prefix
from hesabyar.infrastructure.config import AppSettings
from hesabyar.interfaces.mcp.rate_limit import WINDOW_SECONDS, RateLimiter


class RateLimitMiddleware:
    """Pure-ASGI fixed-window rate limiting in front of the MCP app.

    The key is the API-key prefix from the Bearer token when one is
    presented (the prefix is safe to use as a bucket key — it is the
    visible, non-secret part), else the client IP. Only HTTP scopes
    are limited; lifespan/websocket scopes pass through untouched.
    """

    def __init__(self, app: ASGIApp, limiter: RateLimiter) -> None:
        self._app = app
        self._limiter = limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        if not self._limiter.allow(_rate_limit_key(scope)):
            body = json.dumps(
                {
                    "error": "rate_limited",
                    "error_description": "Too many requests.",
                }
            ).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 429,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                        (b"retry-after", str(int(WINDOW_SECONDS)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self._app(scope, receive, send)


def _rate_limit_key(scope: Scope) -> str:
    """Bucket key: ``key:<prefix>`` for a Bearer token, else ``ip:<addr>``."""
    headers = {name.lower(): value for name, value in scope.get("headers", [])}
    auth = headers.get(b"authorization", b"").decode("latin-1")
    if auth.lower().startswith("bearer "):
        prefix = extract_key_prefix(auth[7:].strip())
        if prefix is not None:
            return f"key:{prefix}"
    client = scope.get("client")
    if client is not None:
        return f"ip:{client[0]}"
    return "ip:unknown"


def build_streamable_http_app(server: MCPServer[Any], settings: AppSettings) -> ASGIApp:
    """Build the Streamable HTTP ASGI app for ``server``.

    The SDK app handles MCP sessions + bearer auth; this function adds
    the transport-security settings and the rate-limit wrapper.
    """
    mcp_settings = settings.mcp
    app = server.streamable_http_app(
        streamable_http_path=mcp_settings.path,
        max_request_body_size=mcp_settings.max_request_body_bytes,
        session_idle_timeout=mcp_settings.session_idle_timeout_seconds,
        max_sessions=mcp_settings.max_sessions,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(mcp_settings.allowed_hosts),
            allowed_origins=list(mcp_settings.allowed_origins),
        ),
        host=mcp_settings.host,
    )
    limiter = RateLimiter(mcp_settings.rate_limit_per_minute)
    return RateLimitMiddleware(app, limiter)


def serve_streamable_http(
    app: ASGIApp,
    settings: AppSettings,
    *,
    host: str | None = None,
    port: int | None = None,
) -> None:
    """Serve ``app`` with uvicorn (blocking).

    TLS is enabled when both ``tls_certfile`` and ``tls_keyfile`` are
    configured; production MUST set them (or terminate TLS in front of
    the server) — API keys travel as Bearer tokens and must never
    cross a network in cleartext.
    """
    mcp_settings = settings.mcp
    uvicorn.run(
        app,
        host=host or mcp_settings.host,
        port=port or mcp_settings.port,
        log_level="info",
        ssl_keyfile=mcp_settings.tls_keyfile,
        ssl_certfile=mcp_settings.tls_certfile,
    )
