"""Audit event recording and querying (US-202 record, US-205 query; spec §14.3).

Every post/reverse (and draft create) writes one immutable row to
``audit_events`` with the actor, operation, company, Tryton reference,
idempotency key, payload hash and UTC timestamp. Audit rows never store
secrets — no API keys, no passwords — only the client id, the key
prefix (never the secret) and hashes.

Like ``ApiKeyService``, the recorder flushes but never commits — the
caller owns the transaction. The reader is read-only: audit events are
immutable, and no query here ever writes.
"""

from __future__ import annotations

import datetime
from typing import Any

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from hesabyar.infrastructure.persistence.postgres.models import (
    AiAccessAuditModel,
    AuditEventModel,
)


class AuditRecorder:
    """Write-only access to the ``audit_events`` table."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def record(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        api_client_id: str | None,
        action: str,
        resource_type: str,
        resource_id: str | None,
        company_external_id: str | None,
        outcome: str,
        request_id: str | None = None,
        error_code: str | None = None,
        tryton_reference: str | None = None,
        payload_hash: str | None = None,
        idempotency_key: str | None = None,
        source_system: str = "api",
        metadata_after: dict[str, Any] | None = None,
    ) -> AuditEventModel:
        """Append one audit event and return it.

        ``outcome`` is ``"success"`` or ``"error"``; failures carry the
        stable ``error_code``. The idempotency key is stored inside
        ``metadata_after`` (the table has no dedicated column for it) —
        it is a client-supplied dedup key, not a secret.
        """
        metadata: dict[str, Any] = dict(metadata_after or {})
        if idempotency_key is not None:
            metadata["idempotency_key"] = idempotency_key
        event = AuditEventModel(
            tenant_id=tenant_id,
            installation_id=installation_id,
            api_client_id=api_client_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            company_external_id=company_external_id,
            request_id=request_id,
            source_system=source_system,
            authorization_result="allowed",
            outcome=outcome,
            error_code=error_code,
            tryton_reference=tryton_reference,
            payload_hash=payload_hash,
            metadata_after=metadata or None,
        )
        self.session.add(event)
        self.session.flush()
        return event


class AuditReader:
    """Read-only access to the ``audit_events`` table (US-205).

    Queries are always tenant-scoped — a caller only ever sees its own
    tenant's events — and paginate with true SQL keyset pagination over
    ``(occurred_at, id)``, so page boundaries stay stable while new
    events arrive. The cursor position is a typed ``(datetime, id)``
    pair: cursor encoding/decoding (opaque strings) lives in the REST
    layer (``interfaces.rest.pagination``), not here, so this module
    never imports the interface layer.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_event(self, *, tenant_id: str, event_id: str) -> AuditEventModel | None:
        """Return one audit event of the tenant, or None when missing.

        An event of another tenant looks exactly like an event that
        does not exist.
        """
        stmt = select(AuditEventModel).where(
            AuditEventModel.tenant_id == tenant_id,
            AuditEventModel.id == event_id,
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def list_events(
        self,
        *,
        tenant_id: str,
        api_client_id: str | None = None,
        action: str | None = None,
        company_external_id: str | None = None,
        occurred_from: datetime.datetime | None = None,
        occurred_to: datetime.datetime | None = None,
        limit: int = 50,
        after: tuple[datetime.datetime, str] | None = None,
        newest_first: bool = True,
    ) -> tuple[list[AuditEventModel], tuple[datetime.datetime, str] | None]:
        """Return one page of the tenant's audit events plus the next position.

        Filters are exact matches (``occurred_from`` / ``occurred_to``
        are inclusive bounds on ``occurred_at``). ``after`` is the
        ``(occurred_at, id)`` position the previous page ended at; the
        page starts strictly after it. The second return value is the
        position to pass back as ``after`` for the next page, or None
        when this is the last page. Events order by ``(occurred_at,
        id)`` — newest first by default — so the order is total and
        stable even when two events share a timestamp.
        """
        stmt = select(AuditEventModel).where(AuditEventModel.tenant_id == tenant_id)
        if api_client_id is not None:
            stmt = stmt.where(AuditEventModel.api_client_id == api_client_id)
        if action is not None:
            stmt = stmt.where(AuditEventModel.action == action)
        if company_external_id is not None:
            stmt = stmt.where(
                AuditEventModel.company_external_id == company_external_id
            )
        if occurred_from is not None:
            stmt = stmt.where(AuditEventModel.occurred_at >= occurred_from)
        if occurred_to is not None:
            stmt = stmt.where(AuditEventModel.occurred_at <= occurred_to)
        if after is not None:
            cursor_ts, cursor_id = after
            key = tuple_(AuditEventModel.occurred_at, AuditEventModel.id)
            if newest_first:
                stmt = stmt.where(key < (cursor_ts, cursor_id))
            else:
                stmt = stmt.where(key > (cursor_ts, cursor_id))
        if newest_first:
            stmt = stmt.order_by(
                AuditEventModel.occurred_at.desc(), AuditEventModel.id.desc()
            )
        else:
            stmt = stmt.order_by(
                AuditEventModel.occurred_at.asc(), AuditEventModel.id.asc()
            )
        stmt = stmt.limit(limit + 1)
        rows = list(self.session.execute(stmt).scalars().all())
        if len(rows) > limit:
            last = rows[limit - 1]
            return rows[:limit], (last.occurred_at, last.id)
        return rows, None


class AiAccessAuditReader:
    """Read-only access to the ``ai_access_audit`` table (US-606).

    The MCP access log: one row per MCP call attempt (tool / resource
    / prompt) plus ``management`` rows for client lifecycle events.
    Queries are always tenant-scoped and paginate with true SQL
    keyset pagination over ``(created_at, id)`` — the same contract
    as :class:`AuditReader` (cursor encoding lives in the REST layer).
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def list_events(
        self,
        *,
        tenant_id: str,
        client_id: str | None = None,
        kind: str | None = None,
        outcome: str | None = None,
        tool: str | None = None,
        company_id: str | None = None,
        occurred_from: datetime.datetime | None = None,
        occurred_to: datetime.datetime | None = None,
        limit: int = 50,
        after: tuple[datetime.datetime, str] | None = None,
        newest_first: bool = True,
    ) -> tuple[list[AiAccessAuditModel], tuple[datetime.datetime, str] | None]:
        """Return one page of the tenant's AI access rows plus the next position.

        Filters are exact matches (``occurred_from`` / ``occurred_to``
        are inclusive bounds on ``created_at``); ``tool`` matches the
        tool name / resource URI / prompt name / lifecycle action
        exactly. ``after`` is the ``(created_at, id)`` position the
        previous page ended at; the page starts strictly after it.
        The second return value is the position to pass back as
        ``after`` for the next page, or None when this is the last
        page.
        """
        stmt = select(AiAccessAuditModel).where(
            AiAccessAuditModel.tenant_id == tenant_id
        )
        if client_id is not None:
            stmt = stmt.where(AiAccessAuditModel.client_id == client_id)
        if kind is not None:
            stmt = stmt.where(AiAccessAuditModel.kind == kind)
        if outcome is not None:
            stmt = stmt.where(AiAccessAuditModel.outcome == outcome)
        if tool is not None:
            stmt = stmt.where(AiAccessAuditModel.tool == tool)
        if company_id is not None:
            stmt = stmt.where(AiAccessAuditModel.company_id == company_id)
        if occurred_from is not None:
            stmt = stmt.where(AiAccessAuditModel.created_at >= occurred_from)
        if occurred_to is not None:
            stmt = stmt.where(AiAccessAuditModel.created_at <= occurred_to)
        if after is not None:
            cursor_ts, cursor_id = after
            key = tuple_(AiAccessAuditModel.created_at, AiAccessAuditModel.id)
            if newest_first:
                stmt = stmt.where(key < (cursor_ts, cursor_id))
            else:
                stmt = stmt.where(key > (cursor_ts, cursor_id))
        if newest_first:
            stmt = stmt.order_by(
                AiAccessAuditModel.created_at.desc(), AiAccessAuditModel.id.desc()
            )
        else:
            stmt = stmt.order_by(
                AiAccessAuditModel.created_at.asc(), AiAccessAuditModel.id.asc()
            )
        stmt = stmt.limit(limit + 1)
        rows = list(self.session.execute(stmt).scalars().all())
        if len(rows) > limit:
            last = rows[limit - 1]
            return rows[:limit], (last.created_at, last.id)
        return rows, None
