"""Store reconciliation & backfill transfer shapes (US-512, S2 — spec §10.1).

The polling side of the generic store connector: a read-only change
feed that closes every gap the webhooks miss. Two transports share
this contract — the store's HTTP changes API and the read-only
store-DB read (ADR 0003 decision 3) — and both feed the same
``EventIngestService`` path, so a missed webhook is filled with the
SAME idempotency key and has no duplicate accounting effect.

Key ideas:

- **One change row, one or more canonical events.** A polled
  :class:`StoreChange` carries the store's own event id
  (``event_id``) — the SAME id the store puts in the webhook's
  ``source_event_id``. The mapper (:func:`map_store_change`) turns
  the row into canonical envelopes whose default idempotency key is
  ``event:{source_system}:{source_store_id}:{source_event_id}``,
  exactly as the webhook path derives it, so the §10.4 constraints
  dedup the two paths against each other.
- **The type travels with the event.** ``SyncBatch.events`` carries
  :class:`SyncedEvent` pairs (envelope + canonical type): the
  envelope has no type field (§10.2), so the batch must carry the
  routing discriminator alongside each event. This resolves the
  Phase 5 deferred item "SyncBatch.events type-carrying shape".
- **Unmapped changes are listed, never dropped.** A change whose
  event name has no canonical type yet (``bank_transfer.recorded``,
  ``shipment.dispatched`` — their canonical names need ADR
  approval) is returned in ``SyncBatch.unmapped_changes`` and
  counted in the sync report, so the operator sees it instead of
  losing it silently. (Cheques were unmapped until ADR 0004 +
  US-533 wired ``cheque.registered`` / ``cheque.cancelled`` in.)
- **Stable sort on ``updated_at``.** Pages are ordered by
  ``(updated_at, event_id)`` ascending; the event-id tiebreaker
  makes the order total, so keyset pagination never skips or repeats
  a row when several rows share a timestamp. Cancelled/deleted rows
  stay in the feed with their status flag — they are never
  physically removed from the list (phased design §2 §1.5).
- **Multi-store isolation.** Every change carries its connector's
  ``source_system``/``source_store_id``; the sync service refuses
  a batch whose events belong to another store.
"""

from __future__ import annotations

import base64
import binascii
import datetime
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum, unique
from typing import Any

from hesabyar.domain.events.envelope import (
    CanonicalEvent,
    CanonicalEventType,
)
from hesabyar.domain.events.errors import (
    InvalidSyncCursorError,
    InvalidSyncRequestError,
)
from hesabyar.domain.events.store_webhooks import (
    ORDER_CANCELLATION_ENTITY_TYPE,
    PAYMENT_REFUND_ENTITY_TYPE,
)

#: Default page size for a polling call (rows per ``fetch_changes`` page).
DEFAULT_SYNC_PAGE_SIZE = 100

#: Hard cap on the page size a caller may request.
MAX_SYNC_PAGE_SIZE = 500

#: Lookback used when a sync is started with neither a cursor nor an
#: explicit ``updated_since``/backfill window: recent changes only.
DEFAULT_SYNC_LOOKBACK_DAYS = 7

#: Suffix appended to a refund's store event id for its second
#: canonical event (``payment.refunded``). The first event
#: (``sales.refund.created``) keeps the bare store event id. The
#: webhook receiver (US-511, S1) MUST use the same convention, so a
#: refund that arrives by webhook and is later re-polled dedups on
#: both events instead of creating a second ``payment.refunded``.
REFUND_PAYMENT_EVENT_SUFFIX = ":payment-refunded"

#: Store-native webhook names mapped to their canonical event type(s)
#: (phased design §2 §1.2). A name absent from this table has no
#: canonical type yet and its changes are returned unmapped:
#: ``bank_transfer.recorded`` → ``payment.bank_transfer.recorded``
#: and ``shipment.dispatched`` → packaging/shipment consumers all
#: need ADR approval for their canonical names before they can be
#: ingested (ADR 0003 round-2 update 8). Cheques were unmapped until
#: ADR 0004 (US-530) + US-533 wired them in.
STORE_EVENT_NAME_TO_CANONICAL: Mapping[str, tuple[CanonicalEventType, ...]] = {
    "order.completed": (CanonicalEventType.SALES_ORDER_COMPLETED,),
    "order.cancelled": (CanonicalEventType.SALES_ORDER_CANCELLED,),
    "refund.created": (
        CanonicalEventType.SALES_REFUND_CREATED,
        CanonicalEventType.PAYMENT_REFUNDED,
    ),
    "payment.captured": (CanonicalEventType.PAYMENT_CAPTURED,),
    "payment.refunded": (CanonicalEventType.PAYMENT_REFUNDED,),
    "cheque.registered": (CanonicalEventType.CHEQUE_RECEIVED_REGISTERED,),
    "cheque.cancelled": (CanonicalEventType.CHEQUE_RECEIVED_CANCELLED,),
}


@unique
class StoreEntityKind(StrEnum):
    """Entity kinds a store change feed lists (spec §10.1, S2)."""

    ORDER = "order"
    PAYMENT = "payment"
    CHEQUE = "cheque"


@dataclass(frozen=True)
class StoreChange:
    """One polled change row from a store (HTTP changes API or store DB).

    ``event_id`` is the store's own event id — the SAME id the store
    sends as ``source_event_id`` on its webhooks, which is what makes
    polling dedup against webhooks. ``event_name`` is the store-native
    webhook name (``order.completed``, ``refund.created``, ...).
    ``status`` is the entity's status flag: cancelled/deleted rows
    stay in the feed with their status, never removed. ``payload`` is
    the store's event payload, carried verbatim into the canonical
    envelope (money fields are converted by the §10.3 pipeline, using
    the per-store Toman setting — never here).
    """

    source_system: str
    source_store_id: str
    entity_type: StoreEntityKind
    entity_id: str
    event_id: str
    event_name: str
    updated_at: datetime.datetime
    status: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        for field_name in (
            "source_system",
            "source_store_id",
            "entity_id",
            "event_id",
            "event_name",
            "status",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise InvalidSyncRequestError(
                    f"Store change field {field_name!r} is required and must be a non-empty string.",
                    details={"field": field_name},
                )
        if not isinstance(self.payload, Mapping):
            raise InvalidSyncRequestError(
                "Store change payload must be a JSON object.",
                details={"field": "payload"},
            )
        if self.updated_at.tzinfo is None:
            # A naive timestamp is assumed UTC and recorded as such —
            # the assumption is explicit here, not hidden in a reader.
            object.__setattr__(self, "updated_at", self.updated_at.replace(tzinfo=datetime.UTC))
        else:
            object.__setattr__(self, "updated_at", self.updated_at.astimezone(datetime.UTC))


@dataclass(frozen=True)
class SyncedEvent:
    """One canonical event from a poll, with its type alongside it.

    The 15-field envelope carries no type (§10.2), so the polling
    batch pairs each envelope with the canonical type the receiver
    validated — the same "type alongside the envelope" pattern the
    webhook ingest uses with its ``X-HesabYar-Event-Type`` header.
    """

    event: CanonicalEvent
    event_type: CanonicalEventType


@dataclass(frozen=True)
class SyncCursor:
    """Opaque cursor for cursor-based change polling (§10.1).

    The value is opaque to the caller: the HTTP connector passes the
    store's own cursor through unchanged, while the store-DB reader
    encodes a ``(updated_at, event_id)`` keyset position (see
    :func:`encode_sync_cursor`). ``None`` (or an empty value) means
    "first page".
    """

    value: str | None = None


@dataclass(frozen=True)
class SyncBatch:
    """One page of changes from a connector's polling API.

    ``events`` are the changes mapped to canonical events, ready to
    ingest; ``unmapped_changes`` are the changes whose event name has
    no canonical type yet (bank transfers and shipments today) —
    listed and counted, never silently dropped. ``next_cursor`` is the cursor for the following
    page (None when this is the last page); ``has_more`` says whether
    the caller should keep polling.
    """

    events: Sequence[SyncedEvent]
    unmapped_changes: Sequence[StoreChange] = ()
    next_cursor: SyncCursor | None = None
    has_more: bool = False


@dataclass(frozen=True)
class StoreChangePage:
    """One page of raw change rows from the read-only store-DB reader.

    The DB reader returns rows, not events: the connector maps them
    (via :func:`map_store_change`) so both transports share one
    mapping implementation.
    """

    changes: Sequence[StoreChange]
    has_more: bool = False


# ---------------------------------------------------------------------------
# Keyset cursor codec (store-DB path)
# ---------------------------------------------------------------------------

_CURSOR_VERSION = 1


def encode_sync_cursor(updated_at: datetime.datetime, last_id: str) -> SyncCursor:
    """Encode a keyset position ``(updated_at, event_id)`` as an opaque cursor.

    The next page selects rows with ``(updated_at, event_id)``
    strictly greater than this position, so pagination is stable
    even when many rows share a timestamp.
    """
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=datetime.UTC)
    payload = {
        "v": _CURSOR_VERSION,
        "u": updated_at.astimezone(datetime.UTC).isoformat(),
        "i": last_id,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    return SyncCursor(value=encoded)


def decode_sync_cursor(cursor: SyncCursor | None) -> tuple[datetime.datetime, str] | None:
    """Decode a keyset cursor back to its ``(updated_at, event_id)`` position.

    Returns None for a first-page cursor. A malformed or
    wrong-version cursor is rejected — a bad cursor must never
    silently restart a backfill from the beginning.
    """
    if cursor is None or cursor.value is None or not cursor.value.strip():
        return None
    try:
        raw = json.loads(base64.urlsafe_b64decode(cursor.value.encode("ascii")))
    except (ValueError, binascii.Error) as exc:
        raise InvalidSyncCursorError(
            "The sync cursor is not a valid keyset cursor.",
            details={"cursor": cursor.value},
        ) from exc
    if not isinstance(raw, dict) or raw.get("v") != _CURSOR_VERSION:
        raise InvalidSyncCursorError(
            "The sync cursor has an unsupported version.",
            details={"cursor": cursor.value},
        )
    updated_raw = raw.get("u")
    last_id = raw.get("i")
    if not isinstance(updated_raw, str) or not isinstance(last_id, str) or not last_id:
        raise InvalidSyncCursorError(
            "The sync cursor is missing its position fields.",
            details={"cursor": cursor.value},
        )
    text = updated_raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        updated_at = datetime.datetime.fromisoformat(text)
    except ValueError as exc:
        raise InvalidSyncCursorError(
            "The sync cursor holds an invalid timestamp.",
            details={"cursor": cursor.value},
        ) from exc
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=datetime.UTC)
    return updated_at.astimezone(datetime.UTC), last_id


def validate_page_size(page_size: int) -> int:
    """Validate a requested polling page size (1..``MAX_SYNC_PAGE_SIZE``)."""
    if isinstance(page_size, bool) or not isinstance(page_size, int):
        raise InvalidSyncRequestError(
            "page_size must be an integer.",
            details={"page_size": str(page_size)},
        )
    if page_size < 1 or page_size > MAX_SYNC_PAGE_SIZE:
        raise InvalidSyncRequestError(
            f"page_size must be between 1 and {MAX_SYNC_PAGE_SIZE}, got {page_size}.",
            details={"page_size": page_size},
        )
    return page_size


# ---------------------------------------------------------------------------
# Store change → canonical event mapping
# ---------------------------------------------------------------------------


def _canonical_entity(
    event_type: CanonicalEventType,
    payload: Mapping[str, Any],
    *,
    source_event_id: str,
) -> tuple[str, str]:
    """Derive the canonical §10.4 entity for one polled event.

    The store feed's entity (a coarse ``StoreEntityKind`` + the row's
    id) is the store's own kind — it is NOT the entity the US-511
    validator requires. Each canonical type identifies its OWN entity
    (``docs/store-connector.md`` Part I §3 + §5, mirrored from
    :func:`validate_store_payload
    <hesabyar.domain.events.store_webhooks.validate_store_payload>`):

    - ``sales.order.completed`` → ``("order", order_id)``
    - ``sales.order.cancelled`` → ``("order_cancellation", order_id)``
    - ``sales.refund.created`` → ``("refund", refund_id)``
    - ``payment.captured`` → ``("payment", gateway_payment_id)``
    - ``payment.refunded`` → ``("payment_refund", refund_id)``
    - ``cheque.received.registered`` → ``("cheque", source_event_id)``
      (the payload carries no site cheque-record id; the store event
      id is the entity id — webhook and poll share it, so the ingest
      idempotency key dedups the two paths)
    - ``cheque.received.cancelled`` → ``("cheque_cancellation", sayad_id)``
      (its own entity, like ``order_cancellation``: the cancellation
      must not be suppressed by the registration's own mapping)

    Copying the row's entity verbatim routed poll-only refunds and
    cancellations to the review queue (a cancellation row carries the
    parent order's entity; a refund row has no ``payment_refund``
    entity at all). The payment-refund leg mirrors
    :func:`derive_fanout_event
    <hesabyar.domain.events.store_webhooks.derive_fanout_event>` —
    the same derivation the webhook path applies to the fan-out
    event — so a refund that arrives by webhook and is later
    re-polled dedups on both legs.

    A missing id field falls back to the source event id (the same
    fallback ``derive_fanout_event`` uses): the mapper runs inside
    the connectors' ``fetch_changes``, where raising would fail the
    whole page, so the pipeline validator rejects the event with the
    honest "missing required field" error instead.
    """
    def _payload_id(field: str) -> str:
        value = payload.get(field)
        if isinstance(value, str) and value.strip():
            return value
        return source_event_id

    if event_type is CanonicalEventType.SALES_ORDER_COMPLETED:
        return ("order", _payload_id("order_id"))
    if event_type is CanonicalEventType.SALES_ORDER_CANCELLED:
        return (ORDER_CANCELLATION_ENTITY_TYPE, _payload_id("order_id"))
    if event_type is CanonicalEventType.SALES_REFUND_CREATED:
        return ("refund", _payload_id("refund_id"))
    if event_type is CanonicalEventType.PAYMENT_CAPTURED:
        return ("payment", _payload_id("gateway_payment_id"))
    if event_type is CanonicalEventType.PAYMENT_REFUNDED:
        return (PAYMENT_REFUND_ENTITY_TYPE, _payload_id("refund_id"))
    if event_type is CanonicalEventType.CHEQUE_RECEIVED_REGISTERED:
        # No site cheque-record id exists in the 24-field payload, so
        # the store event id is the entity id (webhook and poll share
        # it — the ingest idempotency key dedups the two paths).
        return ("cheque", source_event_id)
    if event_type is CanonicalEventType.CHEQUE_RECEIVED_CANCELLED:
        return ("cheque_cancellation", _payload_id("sayad_id"))
    raise ValueError(
        f"No canonical entity is defined for {event_type.value!r}: "
        "a new polled event type needs its entity agreed with the "
        "webhook path (US-511) before it can be mapped."
    )


def map_store_change(change: StoreChange) -> tuple[SyncedEvent, ...]:
    """Map one polled change to its canonical event(s).

    Returns an empty tuple when the change's event name has no
    canonical type yet — the caller reports it as unmapped. A refund
    yields two events (``sales.refund.created`` + ``payment.refunded``,
    phased design §2 §1.2): the first keeps the store event id, the
    second appends :data:`REFUND_PAYMENT_EVENT_SUFFIX`, so each has
    its own §10.4 source-event identity and its own idempotency key.

    The envelope's idempotency key defaults to
    ``event:{source_system}:{source_store_id}:{source_event_id}`` —
    the same key the webhook path derives for the same store event —
    which is what makes a polled fill of a missed webhook a duplicate
    instead of a second accounting effect. The envelope's external
    entity is likewise derived per event type from the payload
    (:func:`_canonical_entity`), never copied from the change row —
    the row's coarse entity would fail the US-511 validator for
    cancellations, refunds, and payment refunds.
    """
    event_types = STORE_EVENT_NAME_TO_CANONICAL.get(change.event_name)
    if event_types is None:
        return ()
    synced: list[SyncedEvent] = []
    for index, event_type in enumerate(event_types):
        source_event_id = change.event_id
        if index > 0:
            source_event_id = f"{change.event_id}{REFUND_PAYMENT_EVENT_SUFFIX}"
        entity_type, entity_id = _canonical_entity(
            event_type, change.payload, source_event_id=source_event_id
        )
        event = CanonicalEvent.from_raw(
            {
                "schema_version": 1,
                "source_system": change.source_system,
                "source_store_id": change.source_store_id,
                "source_event_id": source_event_id,
                "occurred_at": change.updated_at.isoformat(),
                "currency": "IRR",
                "external_entity_type": entity_type,
                "external_entity_id": entity_id,
                "payload": dict(change.payload),
            }
        )
        synced.append(SyncedEvent(event=event, event_type=event_type))
    return tuple(synced)
