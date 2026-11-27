"""Request/response schemas for the manual-entry endpoints (US-513).

Money in every manual-entry request/response is whole Toman
(``int``) — the operator types Toman and the §10.3 pipeline converts
×10 to Rial at the connector boundary, exactly like a store webhook.
Quantities are exact Decimals (never a binary float): a JSON number
or a decimal string, e.g. ``2`` or ``"1.5"``.

Shape errors (missing fields, wrong types, negative amounts) fail
pydantic validation (422 ``VALIDATION_ERROR``); business rules — a
line total that is not a whole Toman, refund lines that do not add
up, a discount larger than the subtotal — fail domain validation
(422 ``MANUAL_ENTRY_INVALID`` with the field named).

Responses map from the domain views — no ORM object ever leaks. The
list and detail endpoints share one response shape: the list carries
only the primary event's trace, the detail carries every event's.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from hesabyar.domain.events.manual_entries import (
    MANUAL_DEFAULT_STORE_ID,
    ManualEntryDetail,
    ManualEntrySummary,
)


class ManualOrderLineRequest(BaseModel):
    """One line of a manual order."""

    description: str = Field(min_length=1, max_length=500, description="Line description, e.g. the product name")
    quantity: Decimal = Field(gt=0, description="Quantity, exact decimal (JSON number or string, e.g. 2 or \"1.5\")")
    unit_price_toman: int = Field(ge=0, description="Unit price in whole Toman")
    discount_toman: int = Field(default=0, ge=0, description="Line discount in whole Toman")


class CreateManualOrderRequest(BaseModel):
    """Enter one order manually (→ sales.order.completed)."""

    order_number: str = Field(min_length=1, max_length=128, description="Order reference, e.g. the store order number")
    store_id: str = Field(
        default=MANUAL_DEFAULT_STORE_ID,
        min_length=1,
        max_length=128,
        description="Store the entry is attributed to ('manual' when none is named)",
    )
    customer_name: str | None = Field(default=None, max_length=500, description="Customer name (optional)")
    customer_ref: str | None = Field(default=None, max_length=128, description="Customer reference (optional)")
    lines: list[ManualOrderLineRequest] = Field(min_length=1, description="Order lines (at least one)")
    discount_toman: int = Field(default=0, ge=0, description="Order discount in whole Toman")
    shipping_toman: int = Field(default=0, ge=0, description="Shipping charge in whole Toman")
    tax_toman: int = Field(default=0, ge=0, description="Tax in whole Toman")
    occurred_at: datetime | None = Field(
        default=None, description="When the order happened (ISO 8601); now when omitted"
    )
    note: str | None = Field(default=None, max_length=500, description="Operator note (optional)")


class ManualRefundLineRequest(BaseModel):
    """One line of a partial manual refund."""

    description: str = Field(min_length=1, max_length=500, description="Line description, e.g. the returned product")
    line_total_toman: int = Field(gt=0, description="Refunded line total in whole Toman")


class CreateManualRefundRequest(BaseModel):
    """Enter one refund manually (→ sales.refund.created, + payment.refunded when refund_payment)."""

    order_number: str = Field(min_length=1, max_length=128, description="Reference of the order being refunded")
    refund_amount_toman: int = Field(gt=0, description="Refund amount in whole Toman")
    refund_reference: str | None = Field(
        default=None, max_length=128, description="Refund reference (generated when omitted)"
    )
    refund_payment: bool = Field(
        default=True,
        description="True when the money was returned too (emits payment.refunded); false for a credit note only",
    )
    lines: list[ManualRefundLineRequest] = Field(
        default_factory=list,
        description="Partial-refund lines; when given, they must add up to the refund amount",
    )
    reason: str | None = Field(default=None, max_length=500, description="Refund reason (optional)")
    reason_code: str | None = Field(
        default=None, max_length=128, description="Refund reason code — the mapping-center key (optional)"
    )
    store_id: str = Field(
        default=MANUAL_DEFAULT_STORE_ID,
        min_length=1,
        max_length=128,
        description="Store the entry is attributed to ('manual' when none is named)",
    )
    occurred_at: datetime | None = Field(
        default=None, description="When the refund happened (ISO 8601); now when omitted"
    )
    note: str | None = Field(default=None, max_length=500, description="Operator note (optional)")


class CreateManualCancellationRequest(BaseModel):
    """Cancel one order manually (→ sales.order.cancelled)."""

    order_number: str = Field(min_length=1, max_length=128, description="Reference of the order being cancelled")
    total_amount_toman: int | None = Field(
        default=None, ge=0, description="The cancelled order's total in whole Toman, when known"
    )
    reason: str | None = Field(default=None, max_length=500, description="Cancellation reason (optional)")
    store_id: str = Field(
        default=MANUAL_DEFAULT_STORE_ID,
        min_length=1,
        max_length=128,
        description="Store the entry is attributed to ('manual' when none is named)",
    )
    occurred_at: datetime | None = Field(
        default=None, description="When the cancellation happened (ISO 8601); now when omitted"
    )
    note: str | None = Field(default=None, max_length=500, description="Operator note (optional)")


class ManualEntryEventResponse(BaseModel):
    """One canonical event a manual entry produced, with its pipeline trace."""

    event_type: str = Field(description="Canonical event type, e.g. 'sales.order.completed'")
    inbox_event_id: str = Field(description="Inbox event id")
    source_event_id: str = Field(description="Source event id (manual-<uuid>)")
    external_entity_type: str = Field(description="External entity type, e.g. 'order'")
    external_entity_id: str = Field(description="External entity id, e.g. the order number")
    status: str | None = Field(
        default=None, description="Inbox processing status: received/processing/processed/needs_review/dead_lettered"
    )
    last_error: str | None = Field(default=None, description="Last processing error message")
    processed_at: datetime | None = Field(default=None, description="Successful processing timestamp (UTC)")
    engine_record_id: str | None = Field(
        default=None, description="Engine record id from the §10.4 external-id map (the draft/posted journal)"
    )
    decision: str | None = Field(
        default=None, description="Latest automation decision: draft/auto_post/needs_review/dead_letter/replayed"
    )


class ManualEntryResponse(BaseModel):
    """One manual entry with the trace of the event(s) it produced."""

    entry_id: str = Field(description="Manual entry id (also the correlation id of its events)")
    entry_type: str = Field(description="order | refund | cancellation")
    source_store_id: str = Field(description="Store the entry is attributed to")
    order_number: str = Field(description="Order reference")
    total_amount_toman: int = Field(description="Entry total in whole Toman")
    entered_by: str = Field(description="API key id that created the entry")
    entered_by_name: str | None = Field(description="API client name at creation time")
    events: list[ManualEntryEventResponse] = Field(
        description="Canonical events this entry produced (list: primary event only; detail: all events)"
    )
    payload: dict[str, Any] = Field(description="Primary event payload snapshot (whole Toman, as entered)")
    note: str | None = Field(description="Operator note")
    created_at: datetime = Field(description="Entry creation timestamp (UTC)")

    @classmethod
    def from_detail(cls, detail: ManualEntryDetail) -> ManualEntryResponse:
        """Map a detail view (every event's trace) onto the response."""
        entry = detail.entry
        return cls(
            entry_id=entry.id,
            entry_type=entry.entry_type.value,
            source_store_id=entry.source_store_id,
            order_number=entry.order_number,
            total_amount_toman=entry.total_amount_toman,
            entered_by=entry.entered_by,
            entered_by_name=entry.entered_by_name,
            events=[
                ManualEntryEventResponse(
                    event_type=trace.event_type,
                    inbox_event_id=trace.inbox_event_id,
                    source_event_id=trace.source_event_id,
                    external_entity_type=trace.external_entity_type,
                    external_entity_id=trace.external_entity_id,
                    status=trace.status,
                    last_error=trace.last_error,
                    processed_at=trace.processed_at,
                    engine_record_id=trace.engine_record_id,
                    decision=trace.decision,
                )
                for trace in detail.traces
            ],
            payload=dict(entry.payload),
            note=entry.note,
            created_at=entry.created_at,
        )

    @classmethod
    def from_summary(cls, summary: ManualEntrySummary) -> ManualEntryResponse:
        """Map a list view (primary event's outcome only) onto the response."""
        entry = summary.entry
        primary = entry.primary_event
        events = (
            [
                ManualEntryEventResponse(
                    event_type=primary.event_type.value,
                    inbox_event_id=primary.inbox_event_id,
                    source_event_id=primary.source_event_id,
                    external_entity_type=primary.external_entity_type,
                    external_entity_id=primary.external_entity_id,
                    status=summary.primary_status,
                    last_error=None,
                    processed_at=None,
                    engine_record_id=summary.primary_engine_record_id,
                    decision=summary.primary_decision,
                )
            ]
            if primary is not None
            else []
        )
        return cls(
            entry_id=entry.id,
            entry_type=entry.entry_type.value,
            source_store_id=entry.source_store_id,
            order_number=entry.order_number,
            total_amount_toman=entry.total_amount_toman,
            entered_by=entry.entered_by,
            entered_by_name=entry.entered_by_name,
            events=events,
            payload=dict(entry.payload),
            note=entry.note,
            created_at=entry.created_at,
        )
