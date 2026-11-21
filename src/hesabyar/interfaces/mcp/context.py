"""MCP request context (US-601).

:class:`McpContext` is the object the SDK lifespan hands to every tool
handler (``ctx.request_context.lifespan_context``) and to the
middleware (``ctx.lifespan_context``). It carries everything a tool
needs that is NOT the call itself:

- the application settings (including ``settings.mcp``);
- a SQLAlchemy session factory (short-lived sessions, one per use —
  the same pattern REST uses per request);
- the tool/resource/prompt scope registry;
- the audit writer;
- the transport name (``stdio`` / ``streamable-http`` / ``memory``),
  recorded on every audit row;
- the optional stdio bootstrap client: the stdio transport has no
  per-request auth, so the process authenticates one API key at
  startup and runs as that key's grant. Streamable HTTP never sets
  this — its identity comes from the per-request bearer token.

Identity resolution (:func:`current_client`) prefers the SDK's bearer
auth context (Streamable HTTP) and falls back to the bootstrap client
(stdio, in-memory tests). It returns None when neither is present, and
the middleware fails such calls closed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mcp.server.auth.middleware.auth_context import get_access_token
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.infrastructure.auth.keys import ApiKeyCrypto
from hesabyar.infrastructure.auth.service import ApiKeyService
from hesabyar.infrastructure.config import AppSettings
from hesabyar.interfaces.mcp.audit import AiAccessAuditWriter
from hesabyar.interfaces.mcp.registry import ToolRegistry

if TYPE_CHECKING:
    from hesabyar.interfaces.mcp.services import McpServices


def create_session_factory(settings: AppSettings) -> tuple[Callable[[], Session], Engine]:
    """Build a session factory over one process-shared engine.

    Unlike REST's per-request engine (a deliberate Phase 1
    simplification), the MCP server is a long-lived process, so one
    pooled engine serves all sessions; each session is still
    short-lived and closed after its use. The caller owns the engine
    and disposes it at shutdown (see :meth:`McpContext.close`).
    """
    engine = create_engine(
        settings.database.sqlalchemy_url,
        pool_size=settings.database.pool_size,
        max_overflow=settings.database.max_overflow,
        pool_timeout=settings.database.pool_timeout_seconds,
        connect_args={"connect_timeout": settings.database.connect_timeout_seconds},
    )
    factory = sessionmaker(bind=engine)
    return factory, engine


@dataclass
class McpContext:
    """Everything an MCP tool call needs beyond its arguments."""

    settings: AppSettings
    session_factory: Callable[[], Session]
    registry: ToolRegistry = field(default_factory=ToolRegistry)
    transport: str = "memory"
    bootstrap_client: AuthenticatedClient | None = None
    engine: Engine | None = None

    @property
    def audit_writer(self) -> AiAccessAuditWriter:
        """Audit writer bound to this context's session factory."""
        return AiAccessAuditWriter(self.session_factory)

    def api_key_service(self) -> ApiKeyService:
        """API-key service keyed with the configured pepper (same as REST)."""
        return ApiKeyService(ApiKeyCrypto(self.settings.auth.api_key_pepper))

    def services(self, session: Session) -> McpServices:
        """Build the application-service container bound to ``session``.

        The container is per-call: it is created inside the tool
        handler (in a thread — all services are synchronous) and closed
        when the call ends.
        """
        from hesabyar.interfaces.mcp.services import McpServices

        return McpServices(session, self.settings)

    def close(self) -> None:
        """Dispose the owned engine, if this context owns one."""
        if self.engine is not None:
            self.engine.dispose()
            self.engine = None


def build_mcp_context(
    settings: AppSettings,
    *,
    transport: str,
    session_factory: Callable[[], Session] | None = None,
    engine: Engine | None = None,
) -> McpContext:
    """Build a production :class:`McpContext`.

    With no ``session_factory`` given, a process-shared pooled engine
    is created (and owned by the returned context). Tests pass their
    own factory bound to the test engine.
    """
    if session_factory is None:
        session_factory, engine = create_session_factory(settings)
    return McpContext(
        settings=settings,
        session_factory=session_factory,
        transport=transport,
        engine=engine,
    )


def current_client(context: McpContext) -> AuthenticatedClient | None:
    """Resolve the identity behind the current MCP call, or None.

    Prefers the SDK bearer-auth context (Streamable HTTP: the
    ``AuthContextMiddleware`` sets it from the verified token), falling
    back to the stdio bootstrap client. A bearer token whose claims do
    not carry a HesabYar identity fails closed (None) instead of
    silently using the bootstrap client — the two identities must never
    be confused.
    """
    # Local import: auth.py imports this module's names at type level.
    from hesabyar.interfaces.mcp.auth import client_from_access_token

    token = get_access_token()
    if token is not None:
        return client_from_access_token(token)
    return context.bootstrap_client
