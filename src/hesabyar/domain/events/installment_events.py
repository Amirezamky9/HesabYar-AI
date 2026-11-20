"""Installment event names approved by ADR 0005 (US-540, Phase 5-5 / ۵ج).

The one canonical name extends the eleven §10.2 + ADR 0004 types to
twelve. Unlike the cheque names, it is DERIVED inside HesabYar from
``sales.order.completed`` (when the order carries installment terms)
— it is NOT a store webhook topic, has no row in the
``store_webhooks`` topic→canonical table, and no store ever sends
it. The derivation itself lands in a later 5ج story; US-540 approves
the name and wires it into the ingest enum (``CanonicalEventType``)
so that story has a decided contract to build against.

The canonical event travels in the 15-field envelope (§10.2) with
the event type alongside in the ``X-HesabYar-Event-Type`` header
(US-501 contract) — see ADR 0005 §3 for the envelope fields.
"""

from __future__ import annotations

#: Canonical installment event type approved by ADR 0005 (beyond
#: the nine in spec §10.2 and the two cheque types of ADR 0004).
#: One event per plan: HesabYar derived an installment schedule
#: (plan + legs) from a completed order's installment terms.
INSTALLMENT_SCHEDULE_CREATED = "installment.schedule.created"

#: Canonical installment event types (one, for now).
INSTALLMENT_CANONICAL_EVENT_TYPES: tuple[str, ...] = (INSTALLMENT_SCHEDULE_CREATED,)

#: External entity type carried in the envelope for this event.
INSTALLMENT_EVENT_ENTITY_TYPE = "installment_plan"


def installment_schedule_idempotency_key(order_id: str | None, plan_id: str) -> str:
    """The §10.4 idempotency key for a schedule event (ADR 0005 §3).

    Derived from an order: ``installment-schedule:{order_id}``;
    manual plan: ``installment-schedule:manual:{plan_id}``.
    """
    if order_id:
        return f"installment-schedule:{order_id}"
    return f"installment-schedule:manual:{plan_id}"


def installment_schedule_source_event_id(order_source_event_id: str | None) -> str | None:
    """The derived ``source_event_id`` for a schedule event (ADR 0005 §3).

    ``{order_source_event_id}:installment-schedule`` — the shared
    webhook-vs-poll derivation convention, so the §10.4 source-event
    constraint sees the schedule as distinct from the order event it
    derives from. None for a manual plan (no order event to derive from).
    """
    if order_source_event_id is None:
        return None
    return f"{order_source_event_id}:installment-schedule"
