"""Shared guards for MCP resource + prompt handlers (US-604).

The SDK hands every resource/prompt handler a bare ``Context``; these
helpers turn it into an authorized call:

- :func:`require_mcp_context` — the ``McpContext`` from the lifespan,
  or a fail-closed error when there is none;
- :func:`authenticated_client` — the identity behind the call plus its
  scope check (missing identity → unauthenticated; missing scope →
  forbidden).

Company scoping lives in the callers: resources resolve the ``company``
URI segment and prompts resolve the ``company`` argument against the
grant (:func:`hesabyar.interfaces.mcp.auth.resolve_company_row`) before
any data is touched — a caller-provided company id is never proof of
authorization.
"""

from __future__ import annotations

from typing import cast

from mcp.server.mcpserver import Context

from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.interfaces.mcp.context import McpContext, current_client
from hesabyar.interfaces.mcp.errors import forbidden, internal_error, unauthenticated


def require_mcp_context(ctx: Context | None) -> McpContext:
    """Return the lifespan ``McpContext`` for a handler call.

    Fail closed: no context (or a context that is not ours) means the
    call cannot be authorized, so it is refused rather than served
    without an identity.
    """
    if ctx is None:
        raise unauthenticated()
    try:
        lifespan = ctx.request_context.lifespan_context
    except (RuntimeError, AttributeError):
        raise internal_error("MCP context is unavailable.") from None
    if not isinstance(lifespan, McpContext):
        raise internal_error("MCP context is unavailable.")
    return cast(McpContext, lifespan)


def authenticated_client(
    mcp_context: McpContext, scopes: tuple[str, ...]
) -> AuthenticatedClient:
    """Return the call's identity, enforcing ``scopes``.

    The identity comes from :func:`current_client` (bearer token on
    Streamable HTTP, bootstrap client on stdio / in-memory tests). A
    key missing any required scope is denied before any data access.
    """
    client = current_client(mcp_context)
    if client is None:
        raise unauthenticated()
    missing = client.missing_scopes(*sorted(scopes))
    if missing:
        raise forbidden(f"The API key lacks the required scope(s): {', '.join(missing)}.")
    return client
