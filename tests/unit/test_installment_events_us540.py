"""Unit tests for the installment schedule event (US-540, ADR 0005).

The ADR 0005 decision, as tests: ``installment.schedule.created``
is the twelfth canonical event type, accepted by
``parse_event_type`` and travelling in the 15-field envelope with
the type alongside in the ``X-HesabYar-Event-Type`` header
(US-501 contract). It is DERIVED inside HesabYar from
``sales.order.completed`` — never a store webhook topic: no store
topic maps to it, and the store document builder has no handler
for it (a store-sent copy fails closed, like the other non-store
canonical types).
"""

from __future__ import annotations

import pytest

from hesabyar.domain.events.envelope import (
    CANONICAL_EVENT_TYPES,
    CanonicalEventType,
    canonical_payload_hash,
    parse_event_type,
)
from hesabyar.domain.events.errors import UnknownEventTypeError
from hesabyar.domain.events.installment_events import (
    INSTALLMENT_CANONICAL_EVENT_TYPES,
    INSTALLMENT_EVENT_ENTITY_TYPE,
    INSTALLMENT_SCHEDULE_CREATED,
    installment_schedule_idempotency_key,
    installment_schedule_source_event_id,
)
from hesabyar.domain.events.store_webhooks import (
    STORE_DOCUMENT_EVENT_TYPES,
    STORE_WEBHOOK_MAPPINGS,
)


def test_installment_schedule_created_is_the_twelfth_canonical_type() -> None:
    assert CanonicalEventType.INSTALLMENT_SCHEDULE_CREATED.value == (
        "installment.schedule.created"
    )
    assert len(CANONICAL_EVENT_TYPES) == 12
    assert INSTALLMENT_SCHEDULE_CREATED in CANONICAL_EVENT_TYPES


def test_parse_event_type_accepts_installment_schedule_created() -> None:
    assert (
        parse_event_type("installment.schedule.created")
        is CanonicalEventType.INSTALLMENT_SCHEDULE_CREATED
    )


def test_parse_event_type_still_rejects_unknown_types() -> None:
    with pytest.raises(UnknownEventTypeError):
        parse_event_type("installment.leg.due")


def test_installment_event_constants() -> None:
    assert INSTALLMENT_CANONICAL_EVENT_TYPES == ("installment.schedule.created",)
    assert INSTALLMENT_EVENT_ENTITY_TYPE == "installment_plan"


def test_no_store_webhook_topic_maps_to_the_schedule_event() -> None:
    # The event is derived inside HesabYar; no store topic produces it.
    for topic, mapping in STORE_WEBHOOK_MAPPINGS.items():
        assert CanonicalEventType.INSTALLMENT_SCHEDULE_CREATED not in mapping.canonical_types, (
            f"store topic {topic!r} must not map to installment.schedule.created"
        )


def test_store_document_builder_has_no_handler_for_the_schedule_event() -> None:
    # A store-sent copy fails closed to the review queue, exactly like
    # the other non-store canonical types (settlement, purchase
    # invoice, inventory cost, bank transaction).
    assert CanonicalEventType.INSTALLMENT_SCHEDULE_CREATED not in STORE_DOCUMENT_EVENT_TYPES


def test_idempotency_key_derived_from_order() -> None:
    assert (
        installment_schedule_idempotency_key("order-123", "plan-1")
        == "installment-schedule:order-123"
    )


def test_idempotency_key_manual_plan() -> None:
    assert (
        installment_schedule_idempotency_key(None, "plan-1")
        == "installment-schedule:manual:plan-1"
    )


def test_source_event_id_derivation() -> None:
    assert (
        installment_schedule_source_event_id("evt-order-1")
        == "evt-order-1:installment-schedule"
    )
    assert installment_schedule_source_event_id(None) is None


def test_schedule_payload_hashes_deterministically() -> None:
    # The envelope contract: payload_hash is the SHA-256 of the
    # canonical JSON — the same schedule payload always hashes the same.
    payload = {
        "plan_id": "plan-1",
        "invoice_id": "INV-1",
        "total_principal_irr": 30_000_000,
        "leg_count": 3,
        "legs": [
            {"sequence": 1, "due_date": "1405-09-29", "principal_irr": 10_000_000},
            {"sequence": 2, "due_date": "1405-10-29", "principal_irr": 10_000_000},
            {"sequence": 3, "due_date": "1405-11-29", "principal_irr": 10_000_000},
        ],
    }
    first = canonical_payload_hash(payload)
    reordered = canonical_payload_hash(dict(reversed(list(payload.items()))))
    assert first == reordered
    assert len(first) == 64
