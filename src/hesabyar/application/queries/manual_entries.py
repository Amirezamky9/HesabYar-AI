"""Manual-entry query use cases (US-513).

Read-only views over manual entries, enriched with the pipeline
trace of every event an entry produced: inbox status, the engine
record the §10.4 external-id map points at, and the latest
automation-decision — the read side of the traceability chain
(manual entry → inbox event → pipeline decision → engine record).

All reads are tenant-scoped: an entry of another tenant looks
exactly like one that does not exist.
"""

from __future__ import annotations

from collections.abc import Sequence

from hesabyar.domain.events.errors import ManualEntryNotFoundError
from hesabyar.domain.events.manual_entries import (
    MANUAL_SOURCE_SYSTEM,
    ManualEntry,
    ManualEntryDetail,
    ManualEntryEventRef,
    ManualEntryEventTrace,
    ManualEntrySummary,
    ManualEntryType,
)
from hesabyar.domain.events.ports import (
    DecisionAuditLogPort,
    EventInboxRepositoryPort,
    ExternalIdMapPort,
    ManualEntryRepositoryPort,
)


class ManualEntryQueryService:
    """Query manual entries with their pipeline traces."""

    def __init__(
        self,
        entries: ManualEntryRepositoryPort,
        inbox: EventInboxRepositoryPort,
        external_ids: ExternalIdMapPort,
        audit_log: DecisionAuditLogPort,
    ) -> None:
        self._entries = entries
        self._inbox = inbox
        self._external_ids = external_ids
        self._audit_log = audit_log

    def get_entry(self, entry_id: str, tenant_id: str | None = None) -> ManualEntryDetail:
        """Return one entry with every event's trace, or raise when missing."""
        entry = self._entries.get(entry_id, tenant_id)
        if entry is None:
            raise ManualEntryNotFoundError(f"Manual entry {entry_id!r} does not exist.")
        return ManualEntryDetail(
            entry=entry, traces=tuple(self._trace(ref, tenant_id) for ref in entry.events)
        )

    def list_entries(
        self,
        tenant_id: str | None = None,
        *,
        entry_type: ManualEntryType | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[ManualEntrySummary]:
        """List entries, newest first, with each primary event's outcome."""
        entries = self._entries.list_entries(
            tenant_id, entry_type=entry_type, limit=limit, offset=offset
        )
        return [self._summarize(entry, tenant_id) for entry in entries]

    def _summarize(self, entry: ManualEntry, tenant_id: str | None) -> ManualEntrySummary:
        primary = entry.primary_event
        if primary is None:
            return ManualEntrySummary(entry=entry)
        trace = self._trace(primary, tenant_id)
        return ManualEntrySummary(
            entry=entry,
            primary_status=trace.status,
            primary_engine_record_id=trace.engine_record_id,
            primary_decision=trace.decision,
        )

    def _trace(self, ref: ManualEntryEventRef, tenant_id: str | None) -> ManualEntryEventTrace:
        """Enrich one event ref with its inbox row, engine record and decision."""
        inbox_event = self._inbox.get(ref.inbox_event_id, tenant_id)
        mapping = self._external_ids.get(
            MANUAL_SOURCE_SYSTEM, ref.external_entity_type, ref.external_entity_id
        )
        decisions = self._audit_log.list_decisions(
            tenant_id, inbox_event_id=ref.inbox_event_id, limit=1
        )
        return ManualEntryEventTrace(
            event_type=ref.event_type.value,
            inbox_event_id=ref.inbox_event_id,
            source_event_id=ref.source_event_id,
            external_entity_type=ref.external_entity_type,
            external_entity_id=ref.external_entity_id,
            status=inbox_event.status.value if inbox_event is not None else None,
            last_error=inbox_event.last_error if inbox_event is not None else None,
            processed_at=inbox_event.processed_at if inbox_event is not None else None,
            engine_record_id=mapping.target_external_id if mapping is not None else None,
            decision=decisions[0].decision.value if decisions else None,
        )
