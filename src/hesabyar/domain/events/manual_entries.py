"""Dual-entry manual orders/invoices/refunds (US-513, Phase 5 wave 2, stream S3).

ADR 0003 decision 1 (dual-entry principle): manual entry and automatic
store connectivity are both first-class. A manual entry produces the
SAME 15-field canonical envelope (spec §10.2) a store webhook would
produce and travels the SAME §10.3 pipeline (validation, mapping,
policy gate, audit log) — it is not a side door.

Envelope construction for manual entries (phased design §5, dual-entry
table):

- ``source_system`` is always ``"manual"``; ``source_store_id`` is the
  store the entry is attributed to (``"manual"`` when the operator did
  not name one — an accountant maps it to a company with the
  ``store_company`` mapping kind).
- ``source_event_id`` is a fresh ``manual-<uuid>`` per event, so every
  manual event has a unique §10.4 identity.
- ``correlation_id`` is the manual-entry id: the two events of a
  refund-with-payment (``sales.refund.created`` + ``payment.refunded``)
  share it, so they trace to one entry.
- The payload carries ``entry_source: "manual"`` and the
  ``manual_entry_id``, so the event itself points back at the entry;
  the ``manual_entries`` row points forward at the inbox events. Both
  directions are traceable.
- Money in a manual entry is whole Toman (``int``) — the operator
  types Toman, the pipeline converts ×10 to Rial at the connector
  boundary exactly like a store webhook (ADR 0003 decision 4). A line
  total that is not a whole Toman is rejected here, with the field
  named, so manual events never trigger the pipeline's HALF_UP
  rounding: manual accounting is exact by construction.
- External-entity types are chosen so the pipeline's §10.4
  exactly-once pre-check never suppresses a legitimate follow-up:
  an order is ``("order", order_number)`` (a second entry of the same
  order number is correctly suppressed as a duplicate), a refund is
  ``("refund", refund_reference)``, its payment leg is
  ``("payment_refund", refund_reference)``, and a cancellation is
  ``("order_cancellation", order_number)`` — a distinct entity from
  the order itself, so cancelling an order that already posted is
  never mistaken for re-processing the order.

Only the nine canonical event types are used; no new event name is
introduced (a new name would need ADR approval first).
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum, unique
from typing import Any

from hesabyar.domain.events.envelope import (
    CanonicalEvent,
    CanonicalEventType,
    canonical_json,
)
from hesabyar.domain.events.errors import ManualEntryValidationError

#: ``source_system`` for every manually entered event (phased design §5).
MANUAL_SOURCE_SYSTEM = "manual"

#: Default ``source_store_id`` when the operator names no store.
MANUAL_DEFAULT_STORE_ID = "manual"

#: Payload marker proving an event came from manual entry (not a webhook).
MANUAL_ENTRY_SOURCE = "manual"

#: Maximum free-text lengths (kept small: these are operator notes).
MAX_REFERENCE_LENGTH = 128
MAX_TEXT_LENGTH = 500


@unique
class ManualEntryType(StrEnum):
    """The three manual-entry kinds (phased design §1.3 stream S3)."""

    ORDER = "order"
    REFUND = "refund"
    CANCELLATION = "cancellation"


@unique
class ManualEntityType(StrEnum):
    """External-entity types for manual events (§10.4 constraint 3).

    Distinct entity types keep the pipeline's exactly-once pre-check
    from suppressing a legitimate follow-up (see the module docstring).
    """

    ORDER = "order"
    REFUND = "refund"
    PAYMENT_REFUND = "payment_refund"
    ORDER_CANCELLATION = "order_cancellation"


def _require_text(value: str | None, field_name: str, *, max_length: int) -> str:
    if value is None or not value.strip():
        raise ManualEntryValidationError(
            f"Field {field_name!r} is required and must be a non-empty string.",
            details={"field": field_name},
        )
    stripped = value.strip()
    if len(stripped) > max_length:
        raise ManualEntryValidationError(
            f"Field {field_name!r} must be at most {max_length} characters.",
            details={"field": field_name},
        )
    return stripped


def _optional_text(value: str | None, field_name: str, *, max_length: int) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    if len(stripped) > max_length:
        raise ManualEntryValidationError(
            f"Field {field_name!r} must be at most {max_length} characters.",
            details={"field": field_name},
        )
    return stripped


def _json_number(value: Decimal) -> int | str:
    """Render an exact Decimal as JSON: int when whole, else a decimal string.

    Floats are forbidden in event payloads (the currency module rejects
    them); a non-whole quantity travels as a string and parses exactly.
    """
    if value == value.to_integral_value():
        return int(value)
    return format(value.normalize(), "f")


def _new_source_event_id() -> str:
    return f"manual-{uuid.uuid4().hex}"


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


# ---------------------------------------------------------------------------
# Order lines + commands
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ManualOrderLine:
    """One line of a manual order (whole-Toman pricing, exact quantities)."""

    description: str
    quantity: Decimal
    unit_price_toman: int
    discount_toman: int = 0

    @property
    def line_total_toman(self) -> Decimal:
        """Exact line total: quantity × unit price − line discount."""
        return self.quantity * self.unit_price_toman - self.discount_toman

    def validate(self, *, index: int) -> None:
        _require_text(self.description, f"lines[{index}].description", max_length=MAX_TEXT_LENGTH)
        if not isinstance(self.quantity, Decimal) or not self.quantity.is_finite() or self.quantity <= 0:
            raise ManualEntryValidationError(
                f"Field 'lines[{index}].quantity' must be a positive number.",
                details={"field": f"lines[{index}].quantity"},
            )
        if not isinstance(self.unit_price_toman, int) or isinstance(self.unit_price_toman, bool):
            raise ManualEntryValidationError(
                f"Field 'lines[{index}].unit_price_toman' must be a whole number of Toman.",
                details={"field": f"lines[{index}].unit_price_toman"},
            )
        if self.unit_price_toman < 0:
            raise ManualEntryValidationError(
                f"Field 'lines[{index}].unit_price_toman' must not be negative.",
                details={"field": f"lines[{index}].unit_price_toman"},
            )
        if not isinstance(self.discount_toman, int) or isinstance(self.discount_toman, bool):
            raise ManualEntryValidationError(
                f"Field 'lines[{index}].discount_toman' must be a whole number of Toman.",
                details={"field": f"lines[{index}].discount_toman"},
            )
        if self.discount_toman < 0:
            raise ManualEntryValidationError(
                f"Field 'lines[{index}].discount_toman' must not be negative.",
                details={"field": f"lines[{index}].discount_toman"},
            )
        total = self.line_total_toman
        if total < 0:
            raise ManualEntryValidationError(
                f"Line {index + 1} has a negative total: the discount exceeds "
                "quantity × unit price.",
                details={"field": f"lines[{index}]"},
            )
        if total != total.to_integral_value():
            raise ManualEntryValidationError(
                f"Line {index + 1} totals {total} Toman, which is not a whole "
                "number of Toman — adjust the quantity or the unit price.",
                details={"field": f"lines[{index}]"},
            )

    def to_payload(self) -> dict[str, Any]:
        """Render the line with the §10.3 money-field names (whole Toman ints)."""
        return {
            "description": self.description.strip(),
            "quantity": _json_number(self.quantity),
            "unit_price": self.unit_price_toman,
            "discount_amount": self.discount_toman,
            "line_total": int(self.line_total_toman),
        }


@dataclass(frozen=True)
class CreateManualOrder:
    """Command: enter one order manually (→ ``sales.order.completed``)."""

    order_number: str
    lines: tuple[ManualOrderLine, ...]
    store_id: str = MANUAL_DEFAULT_STORE_ID
    customer_name: str | None = None
    customer_ref: str | None = None
    discount_toman: int = 0
    shipping_toman: int = 0
    tax_toman: int = 0
    occurred_at: datetime.datetime | None = None
    note: str | None = None

    @property
    def subtotal_toman(self) -> int:
        return sum(int(line.line_total_toman) for line in self.lines)

    @property
    def total_toman(self) -> int:
        return self.subtotal_toman - self.discount_toman + self.shipping_toman + self.tax_toman

    def validate(self) -> None:
        _require_text(self.order_number, "order_number", max_length=MAX_REFERENCE_LENGTH)
        _require_text(self.store_id, "store_id", max_length=MAX_REFERENCE_LENGTH)
        if not self.lines:
            raise ManualEntryValidationError(
                "An order needs at least one line.",
                details={"field": "lines"},
            )
        for index, line in enumerate(self.lines):
            line.validate(index=index)
        for field_name in ("discount_toman", "shipping_toman", "tax_toman"):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ManualEntryValidationError(
                    f"Field {field_name!r} must be a whole number of Toman, zero or more.",
                    details={"field": field_name},
                )
        if self.discount_toman > self.subtotal_toman:
            raise ManualEntryValidationError(
                "The order discount exceeds the subtotal.",
                details={"field": "discount_toman"},
            )
        if self.total_toman <= 0:
            raise ManualEntryValidationError(
                "The order total must be more than zero Toman.",
                details={"field": "total_amount"},
            )
        _optional_text(self.customer_name, "customer_name", max_length=MAX_TEXT_LENGTH)
        _optional_text(self.customer_ref, "customer_ref", max_length=MAX_REFERENCE_LENGTH)
        _optional_text(self.note, "note", max_length=MAX_TEXT_LENGTH)


@dataclass(frozen=True)
class ManualRefundLine:
    """One line of a partial manual refund (whole Toman)."""

    description: str
    line_total_toman: int

    def validate(self, *, index: int) -> None:
        _require_text(self.description, f"lines[{index}].description", max_length=MAX_TEXT_LENGTH)
        if (
            not isinstance(self.line_total_toman, int)
            or isinstance(self.line_total_toman, bool)
            or self.line_total_toman <= 0
        ):
            raise ManualEntryValidationError(
                f"Field 'lines[{index}].line_total_toman' must be a whole number of Toman, more than zero.",
                details={"field": f"lines[{index}].line_total_toman"},
            )

    def to_payload(self) -> dict[str, Any]:
        return {"description": self.description.strip(), "line_total": self.line_total_toman}


@dataclass(frozen=True)
class CreateManualRefund:
    """Command: enter one refund manually (→ ``sales.refund.created``).

    When ``refund_payment`` is true (the default — the S1 webhook
    mapping emits both events for ``refund.created``), a second event
    ``payment.refunded`` is produced with the same correlation id, so
    the money leg and the credit-note leg stay tied to one entry.
    """

    order_number: str
    refund_amount_toman: int
    refund_reference: str | None = None
    refund_payment: bool = True
    lines: tuple[ManualRefundLine, ...] = ()
    reason: str | None = None
    reason_code: str | None = None
    store_id: str = MANUAL_DEFAULT_STORE_ID
    occurred_at: datetime.datetime | None = None
    note: str | None = None

    def validate(self) -> None:
        _require_text(self.order_number, "order_number", max_length=MAX_REFERENCE_LENGTH)
        _require_text(self.store_id, "store_id", max_length=MAX_REFERENCE_LENGTH)
        if (
            not isinstance(self.refund_amount_toman, int)
            or isinstance(self.refund_amount_toman, bool)
            or self.refund_amount_toman <= 0
        ):
            raise ManualEntryValidationError(
                "Field 'refund_amount_toman' must be a whole number of Toman, more than zero.",
                details={"field": "refund_amount_toman"},
            )
        if self.refund_reference is not None:
            _require_text(self.refund_reference, "refund_reference", max_length=MAX_REFERENCE_LENGTH)
        for index, line in enumerate(self.lines):
            line.validate(index=index)
        if self.lines and sum(line.line_total_toman for line in self.lines) != self.refund_amount_toman:
            raise ManualEntryValidationError(
                "The refund lines do not add up to the refund amount.",
                details={"field": "lines"},
            )
        _optional_text(self.reason, "reason", max_length=MAX_TEXT_LENGTH)
        _optional_text(self.reason_code, "reason_code", max_length=MAX_REFERENCE_LENGTH)
        _optional_text(self.note, "note", max_length=MAX_TEXT_LENGTH)


@dataclass(frozen=True)
class CreateManualCancellation:
    """Command: cancel one order manually (→ ``sales.order.cancelled``).

    A cancellation is a corrective path, never a rewrite: the original
    order's event and engine record stay untouched; the pipeline
    produces the corrective document (credit note) for the cancelled
    order. ``total_amount_toman`` is the cancelled order's total when
    the operator knows it (recorded for the audit trail).
    """

    order_number: str
    total_amount_toman: int | None = None
    reason: str | None = None
    store_id: str = MANUAL_DEFAULT_STORE_ID
    occurred_at: datetime.datetime | None = None
    note: str | None = None

    def validate(self) -> None:
        _require_text(self.order_number, "order_number", max_length=MAX_REFERENCE_LENGTH)
        _require_text(self.store_id, "store_id", max_length=MAX_REFERENCE_LENGTH)
        if self.total_amount_toman is not None and (
            not isinstance(self.total_amount_toman, int)
            or isinstance(self.total_amount_toman, bool)
            or self.total_amount_toman < 0
        ):
            raise ManualEntryValidationError(
                "Field 'total_amount_toman' must be a whole number of Toman, zero or more.",
                details={"field": "total_amount_toman"},
            )
        _optional_text(self.reason, "reason", max_length=MAX_TEXT_LENGTH)
        _optional_text(self.note, "note", max_length=MAX_TEXT_LENGTH)


# ---------------------------------------------------------------------------
# The manual-entry record (traceability anchor)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ManualEntryEventRef:
    """One canonical event produced by a manual entry."""

    event_type: CanonicalEventType
    inbox_event_id: str
    source_event_id: str
    external_entity_type: str
    external_entity_id: str


@dataclass(frozen=True)
class ManualEntry:
    """One manual entry: the traceability anchor for its events.

    Every manually created record traces to this row (``entered_by``
    + ``created_at`` + the primary payload snapshot), and every event
    it produced traces back via ``events`` (inbox id + source-event
    id + external-entity identity). The audit log's decisions link
    through the inbox event id, so the full chain is:
    manual entry → inbox event → pipeline decision → engine record.
    """

    id: str
    tenant_id: str
    entry_type: ManualEntryType
    source_store_id: str
    order_number: str
    total_amount_toman: int
    entered_by: str
    entered_by_name: str | None
    events: tuple[ManualEntryEventRef, ...]
    payload: dict[str, Any]
    note: str | None
    created_at: datetime.datetime

    @property
    def primary_event(self) -> ManualEntryEventRef | None:
        """The entry's first event (the order/refund/cancellation itself)."""
        return self.events[0] if self.events else None


@dataclass(frozen=True)
class ManualEntryEventTrace:
    """One produced event, enriched with its pipeline outcome.

    Built by the query service from the inbox row, the §10.4
    external-id map and the automation audit log — the read side of
    the traceability chain (manual entry → engine record).
    """

    event_type: str
    inbox_event_id: str
    source_event_id: str
    external_entity_type: str
    external_entity_id: str
    status: str | None
    last_error: str | None
    processed_at: datetime.datetime | None
    engine_record_id: str | None
    decision: str | None


@dataclass(frozen=True)
class ManualEntryDetail:
    """A manual entry with the pipeline trace of every event it produced."""

    entry: ManualEntry
    traces: tuple[ManualEntryEventTrace, ...] = ()


@dataclass(frozen=True)
class ManualEntrySummary:
    """A manual entry for list views: the entry + its primary event's outcome.

    The list shows one row per entry, so only the primary event's
    pipeline state is carried (the detail view loads every trace).
    """

    entry: ManualEntry
    primary_status: str | None = None
    primary_engine_record_id: str | None = None
    primary_decision: str | None = None


# ---------------------------------------------------------------------------
# Envelope construction (the dual-entry seam)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BuiltManualEvent:
    """A validated canonical event, ready for the ingest service."""

    event: CanonicalEvent
    event_type: CanonicalEventType
    external_entity_type: str
    external_entity_id: str

    def to_incoming_body(self) -> bytes:
        """The raw request body the ingest service verifies (canonical JSON)."""
        return canonical_json(self.event.to_envelope_dict()).encode("utf-8")


def _base_envelope(
    *,
    entry_id: str,
    store_id: str,
    occurred_at: datetime.datetime | None,
    external_entity_type: str,
    external_entity_id: str,
    payload: dict[str, Any],
) -> CanonicalEvent:
    """Build the 15-field envelope for one manual event.

    ``CanonicalEvent.from_raw`` validates the envelope — a manual
    event that fails envelope validation never reaches the inbox.
    """
    now = _now()
    return CanonicalEvent.from_raw(
        {
            "source_system": MANUAL_SOURCE_SYSTEM,
            "source_store_id": store_id,
            "source_event_id": _new_source_event_id(),
            "occurred_at": (occurred_at or now).isoformat(),
            "currency": "IRR",
            "external_entity_type": external_entity_type,
            "external_entity_id": external_entity_id,
            "correlation_id": entry_id,
            "payload": payload,
        }
    )


def _with_entry_markers(payload: dict[str, Any], entry_id: str) -> dict[str, Any]:
    """Stamp a payload with its manual-entry origin (both markers)."""
    return {**payload, "entry_source": MANUAL_ENTRY_SOURCE, "manual_entry_id": entry_id}


def build_order_event(command: CreateManualOrder, *, entry_id: str) -> BuiltManualEvent:
    """Build the ``sales.order.completed`` event for a manual order."""
    command.validate()
    payload = _with_entry_markers(
        {
            "order_number": command.order_number.strip(),
            "customer_name": _optional_text(command.customer_name, "customer_name", max_length=MAX_TEXT_LENGTH),
            "customer_ref": _optional_text(command.customer_ref, "customer_ref", max_length=MAX_REFERENCE_LENGTH),
            "lines": [line.to_payload() for line in command.lines],
            "subtotal_amount": command.subtotal_toman,
            "discount_amount": command.discount_toman,
            "shipping_amount": command.shipping_toman,
            "tax_amount": command.tax_toman,
            "total_amount": command.total_toman,
            "note": _optional_text(command.note, "note", max_length=MAX_TEXT_LENGTH),
        },
        entry_id,
    )
    payload = {key: value for key, value in payload.items() if value is not None}
    event = _base_envelope(
        entry_id=entry_id,
        store_id=command.store_id.strip(),
        occurred_at=command.occurred_at,
        external_entity_type=ManualEntityType.ORDER,
        external_entity_id=command.order_number.strip(),
        payload=payload,
    )
    return BuiltManualEvent(
        event=event,
        event_type=CanonicalEventType.SALES_ORDER_COMPLETED,
        external_entity_type=ManualEntityType.ORDER,
        external_entity_id=command.order_number.strip(),
    )


def build_refund_events(
    command: CreateManualRefund, *, entry_id: str
) -> tuple[BuiltManualEvent, ...]:
    """Build the event(s) for a manual refund.

    Always ``sales.refund.created``; plus ``payment.refunded`` when
    the operator says the money was returned (``refund_payment``) —
    the same dual emission the S1 webhook mapping does for
    ``refund.created``. Both events share the entry's correlation id.
    """
    command.validate()
    refund_reference = (
        command.refund_reference.strip()
        if command.refund_reference is not None
        else f"manual-refund-{uuid.uuid4().hex[:12]}"
    )
    order_number = command.order_number.strip()
    store_id = command.store_id.strip()
    refund_payload = _with_entry_markers(
        {
            "order_number": order_number,
            "refund_reference": refund_reference,
            "refund_amount": command.refund_amount_toman,
            "total_amount": command.refund_amount_toman,
            "reason": _optional_text(command.reason, "reason", max_length=MAX_TEXT_LENGTH),
            "reason_code": _optional_text(command.reason_code, "reason_code", max_length=MAX_REFERENCE_LENGTH),
            "lines": [line.to_payload() for line in command.lines] or None,
            "note": _optional_text(command.note, "note", max_length=MAX_TEXT_LENGTH),
        },
        entry_id,
    )
    refund_payload = {key: value for key, value in refund_payload.items() if value is not None}
    refund_event = BuiltManualEvent(
        event=_base_envelope(
            entry_id=entry_id,
            store_id=store_id,
            occurred_at=command.occurred_at,
            external_entity_type=ManualEntityType.REFUND,
            external_entity_id=refund_reference,
            payload=refund_payload,
        ),
        event_type=CanonicalEventType.SALES_REFUND_CREATED,
        external_entity_type=ManualEntityType.REFUND,
        external_entity_id=refund_reference,
    )
    if not command.refund_payment:
        return (refund_event,)
    payment_payload = _with_entry_markers(
        {
            "order_number": order_number,
            "refund_reference": refund_reference,
            "gross_amount": command.refund_amount_toman,
            "net_amount": command.refund_amount_toman,
        },
        entry_id,
    )
    payment_event = BuiltManualEvent(
        event=_base_envelope(
            entry_id=entry_id,
            store_id=store_id,
            occurred_at=command.occurred_at,
            external_entity_type=ManualEntityType.PAYMENT_REFUND,
            external_entity_id=refund_reference,
            payload=payment_payload,
        ),
        event_type=CanonicalEventType.PAYMENT_REFUNDED,
        external_entity_type=ManualEntityType.PAYMENT_REFUND,
        external_entity_id=refund_reference,
    )
    return (refund_event, payment_event)


def build_cancellation_event(
    command: CreateManualCancellation, *, entry_id: str
) -> BuiltManualEvent:
    """Build the ``sales.order.cancelled`` event for a manual cancellation."""
    command.validate()
    payload = _with_entry_markers(
        {
            "order_number": command.order_number.strip(),
            "total_amount": command.total_amount_toman,
            "reason": _optional_text(command.reason, "reason", max_length=MAX_TEXT_LENGTH),
            "note": _optional_text(command.note, "note", max_length=MAX_TEXT_LENGTH),
        },
        entry_id,
    )
    payload = {key: value for key, value in payload.items() if value is not None}
    event = _base_envelope(
        entry_id=entry_id,
        store_id=command.store_id.strip(),
        occurred_at=command.occurred_at,
        external_entity_type=ManualEntityType.ORDER_CANCELLATION,
        external_entity_id=command.order_number.strip(),
        payload=payload,
    )
    return BuiltManualEvent(
        event=event,
        event_type=CanonicalEventType.SALES_ORDER_CANCELLED,
        external_entity_type=ManualEntityType.ORDER_CANCELLATION,
        external_entity_id=command.order_number.strip(),
    )


def new_entry_id() -> str:
    """A fresh manual-entry id (also the correlation id of its events)."""
    return str(uuid.uuid4())


# Re-exported for callers that only import this module.
__all__ = [
    "MANUAL_DEFAULT_STORE_ID",
    "MANUAL_ENTRY_SOURCE",
    "MANUAL_SOURCE_SYSTEM",
    "MAX_REFERENCE_LENGTH",
    "MAX_TEXT_LENGTH",
    "BuiltManualEvent",
    "CreateManualCancellation",
    "CreateManualOrder",
    "CreateManualRefund",
    "ManualEntityType",
    "ManualEntry",
    "ManualEntryDetail",
    "ManualEntryEventRef",
    "ManualEntryEventTrace",
    "ManualEntrySummary",
    "ManualEntryType",
    "ManualOrderLine",
    "ManualRefundLine",
    "build_cancellation_event",
    "build_order_event",
    "build_refund_events",
    "new_entry_id",
]
