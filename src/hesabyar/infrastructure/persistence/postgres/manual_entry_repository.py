"""PostgreSQL implementation of the manual-entry port (US-513).

Implements ``domain/events/ports.py`` (``ManualEntryRepositoryPort``)
over the ``manual_entries`` table. This is the ONLY place that knows
the manual-entry SQLAlchemy model — the application layer talks to
the port, never to this table (spec §6.3).

Persistence contract (same as every other writer in the codebase):
``save`` flushes so ids/timestamps exist but does NOT commit — the
caller (REST route) owns the transaction.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from hesabyar.domain.events.envelope import CanonicalEventType
from hesabyar.domain.events.manual_entries import (
    ManualEntry,
    ManualEntryEventRef,
    ManualEntryType,
)
from hesabyar.domain.events.ports import ManualEntryRepositoryPort
from hesabyar.infrastructure.persistence.postgres.models import ManualEntryModel


def _events_to_json(events: tuple[ManualEntryEventRef, ...]) -> list[dict[str, Any]]:
    return [
        {
            "event_type": ref.event_type.value,
            "inbox_event_id": ref.inbox_event_id,
            "source_event_id": ref.source_event_id,
            "external_entity_type": ref.external_entity_type,
            "external_entity_id": ref.external_entity_id,
        }
        for ref in events
    ]


def _events_from_json(raw: Any) -> tuple[ManualEntryEventRef, ...]:
    if not isinstance(raw, list):
        return ()
    refs: list[ManualEntryEventRef] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        refs.append(
            ManualEntryEventRef(
                event_type=CanonicalEventType(str(item.get("event_type", ""))),
                inbox_event_id=str(item.get("inbox_event_id", "")),
                source_event_id=str(item.get("source_event_id", "")),
                external_entity_type=str(item.get("external_entity_type", "")),
                external_entity_id=str(item.get("external_entity_id", "")),
            )
        )
    return tuple(refs)


def _entry_from_row(row: ManualEntryModel) -> ManualEntry:
    """Map a manual-entry row onto the domain dataclass."""
    return ManualEntry(
        id=row.id,
        tenant_id=row.tenant_id,
        entry_type=ManualEntryType(row.entry_type),
        source_store_id=row.source_store_id,
        order_number=row.order_number,
        total_amount_toman=row.total_amount_toman,
        entered_by=row.entered_by,
        entered_by_name=row.entered_by_name,
        events=_events_from_json(row.events),
        payload=dict(row.payload),
        note=row.note,
        created_at=row.created_at,
    )


class SqlAlchemyManualEntryRepository(ManualEntryRepositoryPort):
    """Postgres manual-entry repository (``manual_entries`` table)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def save(self, entry: ManualEntry) -> ManualEntry:
        """Insert one manual entry; flushes, the caller commits."""
        row = ManualEntryModel(
            id=entry.id,
            tenant_id=entry.tenant_id,
            entry_type=entry.entry_type.value,
            source_store_id=entry.source_store_id,
            order_number=entry.order_number,
            total_amount_toman=entry.total_amount_toman,
            entered_by=entry.entered_by,
            entered_by_name=entry.entered_by_name,
            events=_events_to_json(entry.events),
            payload=dict(entry.payload),
            note=entry.note,
            created_at=entry.created_at,
        )
        self.session.add(row)
        self.session.flush()
        return _entry_from_row(row)

    def get(self, entry_id: str, tenant_id: str | None = None) -> ManualEntry | None:
        """Return one entry, or None when it does not exist in the tenant."""
        stmt = select(ManualEntryModel).where(ManualEntryModel.id == entry_id)
        if tenant_id is not None:
            stmt = stmt.where(ManualEntryModel.tenant_id == tenant_id)
        row = self.session.execute(stmt).scalar_one_or_none()
        return _entry_from_row(row) if row is not None else None

    def list_entries(
        self,
        tenant_id: str | None = None,
        *,
        entry_type: ManualEntryType | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[ManualEntry]:
        """List entries, newest first, optionally filtered by type."""
        stmt = select(ManualEntryModel)
        if tenant_id is not None:
            stmt = stmt.where(ManualEntryModel.tenant_id == tenant_id)
        if entry_type is not None:
            stmt = stmt.where(ManualEntryModel.entry_type == entry_type.value)
        stmt = stmt.order_by(ManualEntryModel.created_at.desc()).limit(limit).offset(offset)
        return [_entry_from_row(row) for row in self.session.execute(stmt).scalars()]
