"""Event-platform query use cases (US-501).

Read-only views over the inbox, outbox and automation-decision audit
log. All reads are tenant-scoped: an event of another tenant looks
exactly like one that does not exist.
"""

from __future__ import annotations

from collections.abc import Sequence

from hesabyar.domain.events.errors import EventNotFoundError
from hesabyar.domain.events.models import (
    AutomationDecision,
    InboxEvent,
    InboxEventStatus,
    OutboxEvent,
)
from hesabyar.domain.events.ports import (
    DecisionAuditLogPort,
    EventInboxRepositoryPort,
    EventOutboxRepositoryPort,
)


class EventQueryService:
    """Query inbox/outbox/audit-log views for the integrations API."""

    def __init__(
        self,
        inbox: EventInboxRepositoryPort,
        outbox: EventOutboxRepositoryPort,
        audit_log: DecisionAuditLogPort,
    ) -> None:
        self._inbox = inbox
        self._outbox = outbox
        self._audit_log = audit_log

    def get_inbox_event(self, event_id: str, tenant_id: str | None = None) -> InboxEvent:
        """Return one inbox event, or raise when it does not exist in the tenant."""
        event = self._inbox.get(event_id, tenant_id)
        if event is None:
            raise EventNotFoundError(f"Inbox event {event_id!r} does not exist.")
        return event

    def list_inbox_events(
        self,
        tenant_id: str | None = None,
        *,
        status: InboxEventStatus | None = None,
        source_system: str | None = None,
        event_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[InboxEvent]:
        """List inbox events, newest first, optionally filtered."""
        return self._inbox.list_events(
            tenant_id,
            status=status,
            source_system=source_system,
            event_type=event_type,
            limit=limit,
            offset=offset,
        )

    def count_inbox_events(
        self,
        tenant_id: str | None = None,
        *,
        status: InboxEventStatus | None = None,
        source_system: str | None = None,
        event_type: str | None = None,
    ) -> int:
        """Count inbox events matching the same filters as :meth:`list_inbox_events`."""
        return self._inbox.count_events(
            tenant_id,
            status=status,
            source_system=source_system,
            event_type=event_type,
        )

    def list_outbox_events(
        self,
        tenant_id: str | None = None,
        *,
        status: str | None = None,
        event_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[OutboxEvent]:
        """List outbox events, newest first, optionally filtered."""
        return self._outbox.list_events(
            tenant_id,
            status=status,
            event_type=event_type,
            limit=limit,
            offset=offset,
        )

    def list_decisions(
        self,
        tenant_id: str | None = None,
        *,
        inbox_event_id: str | None = None,
        decision: str | None = None,
        event_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[AutomationDecision]:
        """List automation decisions, newest first, optionally filtered."""
        return self._audit_log.list_decisions(
            tenant_id,
            inbox_event_id=inbox_event_id,
            decision=decision,
            event_type=event_type,
            limit=limit,
            offset=offset,
        )
