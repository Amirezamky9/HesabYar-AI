"""Manual-entry command use cases (US-513, Phase 5 wave 2, stream S3).

The dual-entry seam: a manual order/refund/cancellation is validated,
built into the SAME 15-field canonical envelope a store webhook would
produce (``source_system="manual"``), and ingested through the SAME
:class:`EventIngestService` the webhook receiver uses — same
verification, same idempotency claim, same inbox row, same deferred
processing through the §10.3 pipeline. The manual entry itself is
stored in the same transaction as the inbox rows, so an entry and its
events are never half-created.

Persistence: the service persists through the repository ports and
never sees SQLAlchemy or ORM rows (spec §6.3). Repositories flush but
do NOT commit — the caller (REST route) owns the transaction, like
every other service that writes through a request session.
"""

from __future__ import annotations

import datetime

from hesabyar.application.commands.events import EventIngestService
from hesabyar.domain.events.manual_entries import (
    MANUAL_SOURCE_SYSTEM,
    BuiltManualEvent,
    CreateManualCancellation,
    CreateManualOrder,
    CreateManualRefund,
    ManualEntry,
    ManualEntryEventRef,
    ManualEntryType,
    build_cancellation_event,
    build_order_event,
    build_refund_events,
    new_entry_id,
)
from hesabyar.domain.events.models import IncomingEvent
from hesabyar.domain.events.ports import ManualEntryRepositoryPort


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class ManualEntryService:
    """Create manual orders/refunds/cancellations as canonical events.

    Each creation validates the operator's input, builds one (or, for
    a refund with a payment leg, two) canonical events, ingests them
    through the shared ingest service, and stores the manual-entry
    record that ties the operator's entry to those events.
    """

    def __init__(
        self,
        ingest: EventIngestService,
        entries: ManualEntryRepositoryPort,
    ) -> None:
        self._ingest = ingest
        self._entries = entries

    def create_order(
        self,
        command: CreateManualOrder,
        *,
        tenant_id: str,
        installation_id: str,
        entered_by: str,
        entered_by_name: str | None,
    ) -> ManualEntry:
        """Enter one order manually; flushes, the caller commits."""
        command.validate()
        entry_id = new_entry_id()
        built = build_order_event(command, entry_id=entry_id)
        refs = self._ingest_events((built,), tenant_id=tenant_id, installation_id=installation_id)
        entry = ManualEntry(
            id=entry_id,
            tenant_id=tenant_id,
            entry_type=ManualEntryType.ORDER,
            source_store_id=command.store_id.strip(),
            order_number=command.order_number.strip(),
            total_amount_toman=command.total_toman,
            entered_by=entered_by,
            entered_by_name=entered_by_name,
            events=tuple(refs),
            payload=dict(built.event.payload),
            note=command.note.strip() if command.note and command.note.strip() else None,
            created_at=_now(),
        )
        return self._entries.save(entry)

    def create_refund(
        self,
        command: CreateManualRefund,
        *,
        tenant_id: str,
        installation_id: str,
        entered_by: str,
        entered_by_name: str | None,
    ) -> ManualEntry:
        """Enter one refund manually; flushes, the caller commits."""
        command.validate()
        entry_id = new_entry_id()
        built = build_refund_events(command, entry_id=entry_id)
        refs = self._ingest_events(built, tenant_id=tenant_id, installation_id=installation_id)
        entry = ManualEntry(
            id=entry_id,
            tenant_id=tenant_id,
            entry_type=ManualEntryType.REFUND,
            source_store_id=command.store_id.strip(),
            order_number=command.order_number.strip(),
            total_amount_toman=command.refund_amount_toman,
            entered_by=entered_by,
            entered_by_name=entered_by_name,
            events=tuple(refs),
            payload=dict(built[0].event.payload),
            note=command.note.strip() if command.note and command.note.strip() else None,
            created_at=_now(),
        )
        return self._entries.save(entry)

    def create_cancellation(
        self,
        command: CreateManualCancellation,
        *,
        tenant_id: str,
        installation_id: str,
        entered_by: str,
        entered_by_name: str | None,
    ) -> ManualEntry:
        """Cancel one order manually; flushes, the caller commits."""
        command.validate()
        entry_id = new_entry_id()
        built = build_cancellation_event(command, entry_id=entry_id)
        refs = self._ingest_events((built,), tenant_id=tenant_id, installation_id=installation_id)
        entry = ManualEntry(
            id=entry_id,
            tenant_id=tenant_id,
            entry_type=ManualEntryType.CANCELLATION,
            source_store_id=command.store_id.strip(),
            order_number=command.order_number.strip(),
            total_amount_toman=command.total_amount_toman or 0,
            entered_by=entered_by,
            entered_by_name=entered_by_name,
            events=tuple(refs),
            payload=dict(built.event.payload),
            note=command.note.strip() if command.note and command.note.strip() else None,
            created_at=_now(),
        )
        return self._entries.save(entry)

    def _ingest_events(
        self,
        built: tuple[BuiltManualEvent, ...],
        *,
        tenant_id: str,
        installation_id: str,
    ) -> list[ManualEntryEventRef]:
        """Ingest each built event through the shared ingest service.

        Manual events carry no HMAC signature — there is no webhook
        secret for the ``manual`` source system, so verification
        records the skip exactly as it does for a secret-less store.
        A duplicate ingest (the same source-event id re-sent) reuses
        the stored inbox row instead of creating a second event.
        """
        refs: list[ManualEntryEventRef] = []
        for event in built:
            result = self._ingest.ingest(
                IncomingEvent(
                    connector=MANUAL_SOURCE_SYSTEM,
                    raw_body=event.to_incoming_body(),
                    event_type=event.event_type.value,
                ),
                tenant_id=tenant_id,
                installation_id=installation_id,
            )
            refs.append(
                ManualEntryEventRef(
                    event_type=event.event_type,
                    inbox_event_id=result.event.id,
                    source_event_id=event.event.source_event_id,
                    external_entity_type=event.external_entity_type,
                    external_entity_id=event.external_entity_id,
                )
            )
        return refs
