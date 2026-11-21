"""AI access audit writer (US-601, spec §13.8).

Every MCP call — allowed, denied or failed — lands one row in
``ai_access_audit`` (migration 0040): client, tool, company, scope,
outcome, timestamp. The writer stores NO tool arguments (callers may
pass sensitive data), NO chain-of-thought or model reasoning, and NO
secrets — only the key's visible prefix.

The writer is synchronous SQLAlchemy (like the rest of HesabYar's
persistence); async callers use :meth:`AiAccessAuditWriter.record_async`,
which runs the write in a thread. A failed write raises: the middleware
fails the call closed rather than serving an unaudited call.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Literal

from sqlalchemy.orm import Session

from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.infrastructure.auth.scopes import format_scopes
from hesabyar.infrastructure.persistence.postgres.models import AiAccessAuditModel

#: Audit outcomes (``ai_access_audit.outcome`` check constraint).
AuditOutcome = Literal["allowed", "denied", "error"]
#: Audit call kinds (``ai_access_audit.kind`` check constraint).
#: ``management`` (US-606) is a client lifecycle event
#: (``mcp.client.create`` / ``rotate`` / ``revoke``), written by the
#: REST management endpoints — not by the middleware.
AuditKind = Literal["tool", "resource", "prompt", "management"]


class AiAccessAuditWriter:
    """Write MCP call audit rows through a session factory."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    @staticmethod
    def record_in_session(
        session: Session,
        *,
        client: AuthenticatedClient | None,
        tool: str,
        kind: AuditKind,
        company_id: str | None,
        outcome: AuditOutcome,
        error_code: str | None = None,
        request_id: str | None = None,
        transport: str,
        client_info: str | None = None,
    ) -> None:
        """Insert one audit row into ``session`` (flushes, never commits).

        The caller owns the transaction — the REST management endpoints
        use this so a lifecycle (``management``) row commits atomically
        with the mutation it records. ``client`` is None for attempts
        that failed before any identity was established.
        """
        session.add(
            AiAccessAuditModel(
                tenant_id=client.tenant_id if client is not None else None,
                client_id=client.client_id if client is not None else None,
                key_id=client.key_id if client is not None else None,
                key_prefix=client.key_prefix if client is not None else None,
                tool=tool[:128],
                kind=kind,
                company_id=company_id,
                scopes=format_scopes(client.scopes) if client is not None else None,
                outcome=outcome,
                error_code=error_code,
                request_id=request_id[:64] if request_id is not None else None,
                transport=transport,
                client_info=client_info[:256] if client_info is not None else None,
            )
        )
        session.flush()

    def record(
        self,
        *,
        client: AuthenticatedClient | None,
        tool: str,
        kind: AuditKind,
        company_id: str | None,
        outcome: AuditOutcome,
        error_code: str | None = None,
        request_id: str | None = None,
        transport: str,
        client_info: str | None = None,
    ) -> None:
        """Insert one audit row and commit it.

        ``client`` is None for attempts that failed before any identity
        was established (unauthenticated probes are still recorded, with
        NULL identity columns). Raises on any persistence failure — the
        caller decides whether that fails the MCP call closed.
        """
        session = self._session_factory()
        try:
            AiAccessAuditWriter.record_in_session(
                session,
                client=client,
                tool=tool,
                kind=kind,
                company_id=company_id,
                outcome=outcome,
                error_code=error_code,
                request_id=request_id,
                transport=transport,
                client_info=client_info,
            )
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    async def record_async(
        self,
        *,
        client: AuthenticatedClient | None,
        tool: str,
        kind: AuditKind,
        company_id: str | None,
        outcome: AuditOutcome,
        error_code: str | None = None,
        request_id: str | None = None,
        transport: str,
        client_info: str | None = None,
    ) -> None:
        """Async wrapper for :meth:`record` (runs the sync write in a thread)."""
        await asyncio.to_thread(
            self.record,
            client=client,
            tool=tool,
            kind=kind,
            company_id=company_id,
            outcome=outcome,
            error_code=error_code,
            request_id=request_id,
            transport=transport,
            client_info=client_info,
        )
