"""Store webhook mapping + payload validation (US-511).

Covers the §2 store-topic table, the header contract, the
refund.created fan-out, the order-status rules, the customer/product
matching rules and the per-type payload contracts in
``hesabyar.domain.events.store_webhooks``. Pure domain tests — no
database, no server.
"""

from __future__ import annotations

import pytest

from hesabyar.domain.events.envelope import CanonicalEvent, CanonicalEventType
from hesabyar.domain.events.errors import (
    EventTypePendingAdrError,
    InvalidEventEnvelopeError,
    UnknownEventTypeError,
)
from hesabyar.domain.events.store_webhooks import (
    COMPLETED_ORDER_STATUSES,
    ORDER_STATUS_RULES,
    REFUND_PAYMENT_EVENT_SUFFIX,
    STORE_WEBHOOK_MAPPINGS,
    OrderStatusAction,
    customer_match_keys,
    derive_fanout_event,
    map_store_webhook,
    order_status_action,
    product_match_key,
    resolve_header_event_types,
    validate_store_payload,
)
from tests.event_fixtures import raw_envelope

# ---------------------------------------------------------------------------
# The mapping table (all 9 store webhooks — cheque.cancelled added by US-533)
# ---------------------------------------------------------------------------


def test_mapping_table_covers_all_nine_store_webhooks() -> None:
    assert set(STORE_WEBHOOK_MAPPINGS) == {
        "order.completed",
        "order.cancelled",
        "refund.created",
        "payment.captured",
        "payment.refunded",
        "cheque.registered",
        "cheque.cancelled",
        "bank_transfer.recorded",
        "shipment.dispatched",
    }


@pytest.mark.parametrize(
    ("topic", "expected"),
    [
        ("order.completed", ("sales.order.completed",)),
        ("order.cancelled", ("sales.order.cancelled",)),
        ("refund.created", ("sales.refund.created", "payment.refunded")),
        ("payment.captured", ("payment.captured",)),
        ("payment.refunded", ("payment.refunded",)),
        ("cheque.registered", ("cheque.received.registered",)),
        ("cheque.cancelled", ("cheque.received.cancelled",)),
    ],
    ids=["order", "cancel", "refund-fanout", "capture", "refund-payment", "cheque", "cheque-cancel"],
)
def test_approved_topics_map_to_canonical_types(
    topic: str, expected: tuple[str, ...]
) -> None:
    mapping = map_store_webhook(topic)
    assert mapping.pending_adr is False
    assert tuple(t.value for t in mapping.canonical_types) == expected


@pytest.mark.parametrize(
    ("topic", "proposed"),
    [
        ("bank_transfer.recorded", ("payment.bank_transfer.recorded",)),
        (
            "shipment.dispatched",
            ("inventory.packaging.consumed", "shipment.cost.charged"),
        ),
    ],
    ids=["bank-transfer", "shipment"],
)
def test_pending_adr_topics_carry_proposed_targets(
    topic: str, proposed: tuple[str, ...]
) -> None:
    mapping = map_store_webhook(topic)
    assert mapping.pending_adr is True
    assert mapping.canonical_types == ()
    assert mapping.proposed_targets == proposed


def test_unknown_topic_raises_unknown_type() -> None:
    with pytest.raises(UnknownEventTypeError) as exc_info:
        map_store_webhook("order.shipped")
    assert exc_info.value.code == "EVENT_UNKNOWN_TYPE"


# ---------------------------------------------------------------------------
# Header resolution (X-HesabYar-Event-Type)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "canonical",
    [t.value for t in CanonicalEventType],
    ids=[t.value for t in CanonicalEventType],
)
def test_canonical_type_passes_through_unchanged(canonical: str) -> None:
    assert resolve_header_event_types(canonical) == (CanonicalEventType(canonical),)


@pytest.mark.parametrize(
    ("topic", "expected"),
    [
        ("order.completed", ("sales.order.completed",)),
        ("refund.created", ("sales.refund.created", "payment.refunded")),
    ],
    ids=["order", "refund-fanout"],
)
def test_topic_maps_to_canonical_types(topic: str, expected: tuple[str, ...]) -> None:
    assert tuple(t.value for t in resolve_header_event_types(topic)) == expected


def test_header_value_is_stripped() -> None:
    assert resolve_header_event_types("  order.completed  ") == (
        CanonicalEventType.SALES_ORDER_COMPLETED,
    )


@pytest.mark.parametrize("value", [None, "", "   "], ids=["missing", "empty", "blank"])
def test_missing_header_is_an_envelope_error(value: str | None) -> None:
    with pytest.raises(InvalidEventEnvelopeError) as exc_info:
        resolve_header_event_types(value)
    assert exc_info.value.code == "EVENT_INVALID_ENVELOPE"


def test_unknown_header_value_is_unknown_type() -> None:
    with pytest.raises(UnknownEventTypeError) as exc_info:
        resolve_header_event_types("sales.order.shipped")
    assert exc_info.value.code == "EVENT_UNKNOWN_TYPE"


@pytest.mark.parametrize(
    "topic",
    ["bank_transfer.recorded", "shipment.dispatched"],
    ids=["bank-transfer", "shipment"],
)
def test_pending_adr_topic_is_rejected_never_ingested(topic: str) -> None:
    with pytest.raises(EventTypePendingAdrError) as exc_info:
        resolve_header_event_types(topic)
    assert exc_info.value.code == "EVENT_TYPE_PENDING_ADR_APPROVAL"
    assert exc_info.value.details["webhook_topic"] == topic


# ---------------------------------------------------------------------------
# Fan-out derivation (refund.created → sales.refund.created + payment.refunded)
# ---------------------------------------------------------------------------


def _refund_event() -> CanonicalEvent:
    return CanonicalEvent.from_raw(
        raw_envelope(
            source_event_id="webhook-refund-1",
            payload={
                "refund_id": "refund-1",
                "order_id": "order-1",
                "refund_amount": 50000,
                "payment_channel": "zarinpal",
            },
            external_entity_type="refund",
            external_entity_id="refund-1",
        )
    )


def test_fanout_derives_a_distinct_event_with_its_own_identity() -> None:
    original = _refund_event()
    derived = derive_fanout_event(original, CanonicalEventType.PAYMENT_REFUNDED)

    assert derived.id != original.id
    assert derived.source_event_id == "webhook-refund-1:payment-refunded"
    assert derived.idempotency_key.endswith(":payment-refunded")
    assert derived.payload == original.payload
    assert derived.payload_hash == original.payload_hash
    assert derived.source_system == original.source_system
    assert derived.correlation_id == original.correlation_id


def test_fanout_repoints_the_entity_at_the_payment_refund() -> None:
    original = _refund_event()
    derived = derive_fanout_event(original, CanonicalEventType.PAYMENT_REFUNDED)
    assert derived.external_entity_type == "payment_refund"
    assert derived.external_entity_id == "refund-1"


def test_fanout_is_deterministic_for_replays() -> None:
    original = _refund_event()
    first = derive_fanout_event(original, CanonicalEventType.PAYMENT_REFUNDED)
    second = derive_fanout_event(original, CanonicalEventType.PAYMENT_REFUNDED)
    # The uuid differs per derivation, but the §10.4 identity is stable:
    # a re-sent webhook re-derives the same event and replays as a duplicate.
    assert first.source_event_id == second.source_event_id
    assert first.idempotency_key == second.idempotency_key
    assert first.external_entity_id == second.external_entity_id


def test_fanout_without_refund_id_falls_back_to_the_source_event() -> None:
    original = CanonicalEvent.from_raw(
        raw_envelope(
            source_event_id="webhook-refund-2",
            payload={"refund_amount": 50000},
            external_entity_type="refund",
            external_entity_id="refund-2",
        )
    )
    derived = derive_fanout_event(original, CanonicalEventType.PAYMENT_REFUNDED)
    assert derived.external_entity_type == "payment_refund"
    assert derived.external_entity_id == "webhook-refund-2:payment-refunded"


def test_fanout_rejects_a_target_with_no_agreed_suffix() -> None:
    # The primary leg is the original event itself — it is never
    # derived. A fan-out target without a suffix agreed with the
    # sync path (US-512) raises instead of inventing one.
    original = _refund_event()
    with pytest.raises(ValueError, match="No fan-out suffix"):
        derive_fanout_event(original, CanonicalEventType.SALES_REFUND_CREATED)


def test_the_refund_suffix_matches_the_polling_path() -> None:
    # Pinned: US-512's domain/events/sync.py carries the same
    # constant with the same value. Change it in both places or
    # webhook-vs-poll dedup breaks for refunds.
    assert REFUND_PAYMENT_EVENT_SUFFIX == ":payment-refunded"


# ---------------------------------------------------------------------------
# Order-status rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    ["completed", "paid", "fulfilled", "done", "COMPLETED", " Paid "],
    ids=["completed", "paid", "fulfilled", "done", "upper", "padded"],
)
def test_completed_statuses_create_a_document(status: str) -> None:
    assert order_status_action(status) is OrderStatusAction.CREATE_DOCUMENT


@pytest.mark.parametrize(
    "status",
    [
        "pending",
        "on-hold",
        "on_hold",
        "processing",
        "awaiting-payment",
        "awaiting_payment",
        "awaiting-fulfillment",
        "awaiting_fulfillment",
    ],
)
def test_non_final_statuses_are_ignored(status: str) -> None:
    assert order_status_action(status) is OrderStatusAction.IGNORE


@pytest.mark.parametrize("status", ["cancelled", "canceled"])
def test_cancelled_status_cancels(status: str) -> None:
    assert order_status_action(status) is OrderStatusAction.CANCEL


@pytest.mark.parametrize("status", ["refunded", "partially-refunded", "partially_refunded"])
def test_refunded_status_refunds(status: str) -> None:
    assert order_status_action(status) is OrderStatusAction.REFUND


@pytest.mark.parametrize("status", [None, "", "   ", "shipped", "weird-status"])
def test_unknown_status_fails_closed_to_review(status: str | None) -> None:
    assert order_status_action(status) is OrderStatusAction.REVIEW


def test_completed_order_statuses_cover_the_create_document_rule() -> None:
    assert COMPLETED_ORDER_STATUSES == {
        status
        for status, action in ORDER_STATUS_RULES.items()
        if action is OrderStatusAction.CREATE_DOCUMENT
    }


# ---------------------------------------------------------------------------
# Customer / product matching rules
# ---------------------------------------------------------------------------


def test_customer_match_keys_order_id_email_phone() -> None:
    keys = customer_match_keys(
        {"id": "cust-7", "email": "a@b.ir", "phone": "+98 912 000 0000"}
    )
    assert keys == ("external_id:cust-7", "email:a@b.ir", "phone:+98 912 000 0000")


def test_customer_match_keys_skip_blank_values() -> None:
    keys = customer_match_keys({"id": "  ", "email": "a@b.ir"})
    assert keys == ("email:a@b.ir",)


def test_customer_match_keys_empty_without_identifiers() -> None:
    assert customer_match_keys({}) == ()
    assert customer_match_keys({"name": "Nobody"}) == ()


def test_product_match_key_returns_the_sku() -> None:
    assert product_match_key({"product_sku": "SKU-1", "quantity": 2}) == "SKU-1"


def test_product_match_key_rejects_a_line_without_sku() -> None:
    with pytest.raises(InvalidEventEnvelopeError):
        product_match_key({"quantity": 2})


# ---------------------------------------------------------------------------
# Payload validation — sales.order.completed
# ---------------------------------------------------------------------------


def _valid_order_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "order_id": "order-1",
        "customer": {"id": "cust-1", "email": "a@b.ir"},
        "lines": [
            {
                "product_sku": "SKU-1",
                "quantity": 2,
                "unit_price": 100000,
                "line_total": 200000,
                "revenue_category": "electronics",
            },
            {"product_sku": "SKU-2", "quantity": 1, "unit_price": 50000},
        ],
        "subtotal_amount": 250000,
        "shipping_amount": 30000,
        "discount_amount": 10000,
        "tax_amount": 5000,
        "total_amount": 275000,
        "status": "completed",
    }
    payload.update(overrides)
    return payload


def _validate_order(payload: dict[str, object], **entity: str) -> None:
    validate_store_payload(
        CanonicalEventType.SALES_ORDER_COMPLETED,
        payload,
        external_entity_type=entity.get("type", "order"),
        external_entity_id=entity.get("id", "order-1"),
    )


def test_valid_order_payload_passes() -> None:
    _validate_order(_valid_order_payload())


def test_order_without_optional_fields_passes() -> None:
    _validate_order(
        {
            "order_id": "order-1",
            "customer": {"id": "cust-1"},
            "lines": [{"product_sku": "SKU-1", "quantity": 1, "unit_price": 100000}],
            "total_amount": 100000,
        }
    )


@pytest.mark.parametrize(
    "field",
    ["order_id", "customer", "lines", "total_amount"],
    ids=["order_id", "customer", "lines", "total_amount"],
)
def test_order_missing_required_field_fails(field: str) -> None:
    payload = _valid_order_payload()
    del payload[field]
    with pytest.raises(InvalidEventEnvelopeError):
        _validate_order(payload)


def test_order_with_empty_lines_fails() -> None:
    with pytest.raises(InvalidEventEnvelopeError):
        _validate_order(_valid_order_payload(lines=[]))


def test_order_line_without_sku_fails() -> None:
    with pytest.raises(InvalidEventEnvelopeError):
        _validate_order(
            _valid_order_payload(
                lines=[{"quantity": 1, "unit_price": 100000}],
                total_amount=100000,
                subtotal_amount=100000,
            )
        )


def test_order_line_with_zero_quantity_fails() -> None:
    with pytest.raises(InvalidEventEnvelopeError):
        _validate_order(
            _valid_order_payload(
                lines=[{"product_sku": "SKU-1", "quantity": 0, "unit_price": 100000}],
                total_amount=0,
            )
        )


def test_order_line_total_mismatch_fails() -> None:
    with pytest.raises(InvalidEventEnvelopeError):
        _validate_order(
            _valid_order_payload(
                lines=[
                    {
                        "product_sku": "SKU-1",
                        "quantity": 2,
                        "unit_price": 100000,
                        "line_total": 199999,
                    }
                ],
                total_amount=200000,
                subtotal_amount=200000,
            )
        )


def test_order_subtotal_mismatch_fails() -> None:
    with pytest.raises(InvalidEventEnvelopeError):
        _validate_order(_valid_order_payload(subtotal_amount=249999))


def test_order_total_mismatch_fails() -> None:
    with pytest.raises(InvalidEventEnvelopeError):
        _validate_order(_valid_order_payload(total_amount=275001))


def test_order_with_non_completed_status_fails() -> None:
    with pytest.raises(InvalidEventEnvelopeError):
        _validate_order(_valid_order_payload(status="pending"))


def test_order_with_boolean_amount_fails() -> None:
    with pytest.raises(InvalidEventEnvelopeError):
        _validate_order(_valid_order_payload(total_amount=True))  # type: ignore[arg-type]


def test_order_with_fractional_amount_fails() -> None:
    with pytest.raises(InvalidEventEnvelopeError):
        _validate_order(_valid_order_payload(total_amount=275000.5))  # type: ignore[arg-type]


def test_order_with_wrong_entity_fails() -> None:
    with pytest.raises(InvalidEventEnvelopeError):
        _validate_order(_valid_order_payload(), type="order", id="order-2")
    with pytest.raises(InvalidEventEnvelopeError):
        validate_store_payload(
            CanonicalEventType.SALES_ORDER_COMPLETED,
            _valid_order_payload(),
            external_entity_type=None,
            external_entity_id=None,
        )


def test_order_installment_fields_are_carried() -> None:
    _validate_order(
        _valid_order_payload(
            installment={
                "installment_count": 4,
                "down_payment_amount": 75000,
                "installment_amount": 50000,
                "interval": "monthly",
                "first_due_date": "1405-09-01",
            }
        )
    )


def test_order_installment_with_zero_count_fails() -> None:
    with pytest.raises(InvalidEventEnvelopeError):
        _validate_order(_valid_order_payload(installment={"installment_count": 0}))


def test_order_installment_with_negative_down_payment_fails() -> None:
    with pytest.raises(InvalidEventEnvelopeError):
        _validate_order(
            _valid_order_payload(
                installment={"installment_count": 4, "down_payment_amount": -1}
            )
        )


# ---------------------------------------------------------------------------
# Payload validation — sales.order.cancelled
# ---------------------------------------------------------------------------


def _validate_cancel(payload: dict[str, object], **entity: str) -> None:
    validate_store_payload(
        CanonicalEventType.SALES_ORDER_CANCELLED,
        payload,
        external_entity_type=entity.get("type", "order_cancellation"),
        external_entity_id=entity.get("id", "order-1"),
    )


def test_valid_cancel_payload_passes() -> None:
    _validate_cancel(
        {"order_id": "order-1", "reason": "customer request", "refund_expected": True}
    )


def test_cancel_with_own_cancellation_id_passes() -> None:
    # The cancellation_id is payload metadata — the entity is always
    # the order being cancelled, so a second cancellation of the same
    # order is a duplicate.
    _validate_cancel({"order_id": "order-1", "cancellation_id": "cancel-9"})


def test_cancel_missing_order_id_fails() -> None:
    with pytest.raises(InvalidEventEnvelopeError):
        _validate_cancel({"reason": "no order"})


def test_cancel_pointing_at_the_order_entity_fails() -> None:
    # The cancellation is its own entity; pointing at the parent order
    # would be suppressed by the order's own mapping.
    with pytest.raises(InvalidEventEnvelopeError):
        _validate_cancel({"order_id": "order-1"}, type="order", id="order-1")


def test_cancel_with_non_boolean_refund_expected_fails() -> None:
    with pytest.raises(InvalidEventEnvelopeError):
        _validate_cancel({"order_id": "order-1", "refund_expected": "yes"})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Payload validation — sales.refund.created
# ---------------------------------------------------------------------------


def _valid_refund_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "refund_id": "refund-1",
        "order_id": "order-1",
        "refund_amount": 50000,
        "payment_channel": "zarinpal",
    }
    payload.update(overrides)
    return payload


def _validate_refund(payload: dict[str, object], **entity: str) -> None:
    validate_store_payload(
        CanonicalEventType.SALES_REFUND_CREATED,
        payload,
        external_entity_type=entity.get("type", "refund"),
        external_entity_id=entity.get("id", "refund-1"),
    )


def test_valid_refund_payload_passes() -> None:
    _validate_refund(_valid_refund_payload())


def test_refund_missing_payment_channel_fails() -> None:
    payload = _valid_refund_payload()
    del payload["payment_channel"]
    with pytest.raises(InvalidEventEnvelopeError):
        _validate_refund(payload)


@pytest.mark.parametrize("field", ["refund_id", "order_id", "refund_amount"])
def test_refund_missing_required_field_fails(field: str) -> None:
    payload = _valid_refund_payload()
    del payload[field]
    with pytest.raises(InvalidEventEnvelopeError):
        _validate_refund(payload)


def test_refund_with_zero_amount_fails() -> None:
    with pytest.raises(InvalidEventEnvelopeError):
        _validate_refund(_valid_refund_payload(refund_amount=0))


def test_refund_lines_validate_sku_and_quantity() -> None:
    _validate_refund(
        _valid_refund_payload(
            lines=[{"product_sku": "SKU-1", "quantity": 1, "line_total": 50000}]
        )
    )
    with pytest.raises(InvalidEventEnvelopeError):
        _validate_refund(_valid_refund_payload(lines=[{"quantity": 1}]))
    with pytest.raises(InvalidEventEnvelopeError):
        _validate_refund(_valid_refund_payload(lines=[{"product_sku": "SKU-1"}]))


# ---------------------------------------------------------------------------
# Payload validation — payment.captured
# ---------------------------------------------------------------------------


def _valid_capture_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "payment_channel": "zarinpal",
        "gateway_payment_id": "pay-1",
        "gross_amount": 275000,
        "fee_amount": 5000,
        "net_amount": 270000,
        "order_id": "order-1",
        "customer": {"id": "cust-1"},
        "verified": True,
    }
    payload.update(overrides)
    return payload


def _validate_capture(payload: dict[str, object], **entity: str) -> None:
    validate_store_payload(
        CanonicalEventType.PAYMENT_CAPTURED,
        payload,
        external_entity_type=entity.get("type", "payment"),
        external_entity_id=entity.get("id", "pay-1"),
    )


def test_valid_capture_payload_passes() -> None:
    _validate_capture(_valid_capture_payload())


@pytest.mark.parametrize(
    "field", ["payment_channel", "gateway_payment_id", "gross_amount"]
)
def test_capture_missing_required_field_fails(field: str) -> None:
    payload = _valid_capture_payload()
    del payload[field]
    with pytest.raises(InvalidEventEnvelopeError):
        _validate_capture(payload)


def test_capture_without_a_customer_passes_validation() -> None:
    # The customer is optional: the builder falls back to the linked
    # order's invoice party when the payload names the order.
    payload = _valid_capture_payload()
    del payload["customer"]
    _validate_capture(payload)


def test_capture_declaring_verified_false_is_rejected() -> None:
    with pytest.raises(InvalidEventEnvelopeError):
        _validate_capture(_valid_capture_payload(verified=False))


def test_capture_fee_rate_snapshot_must_be_a_percentage() -> None:
    _validate_capture(_valid_capture_payload(fee_rate_snapshot=2.5))
    with pytest.raises(InvalidEventEnvelopeError):
        _validate_capture(_valid_capture_payload(fee_rate_snapshot=101))
    with pytest.raises(InvalidEventEnvelopeError):
        _validate_capture(_valid_capture_payload(fee_rate_snapshot=-1))


# ---------------------------------------------------------------------------
# Payload validation — payment.refunded
# ---------------------------------------------------------------------------


def _valid_payment_refund_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "refund_id": "refund-1",
        "gross_amount": 50000,
        "payment_channel": "zarinpal",
        "gateway_payment_id": "pay-1",
        "order_id": "order-1",
    }
    payload.update(overrides)
    return payload


def _validate_payment_refund(payload: dict[str, object], **entity: str) -> None:
    validate_store_payload(
        CanonicalEventType.PAYMENT_REFUNDED,
        payload,
        external_entity_type=entity.get("type", "payment_refund"),
        external_entity_id=entity.get("id", "refund-1"),
    )


def test_valid_payment_refund_payload_passes() -> None:
    _validate_payment_refund(_valid_payment_refund_payload())


@pytest.mark.parametrize("field", ["refund_id", "gross_amount", "payment_channel"])
def test_payment_refund_missing_required_field_fails(field: str) -> None:
    payload = _valid_payment_refund_payload()
    del payload[field]
    with pytest.raises(InvalidEventEnvelopeError):
        _validate_payment_refund(payload)


def test_payment_refund_pointing_at_the_parent_refund_fails() -> None:
    # The derived leg's entity is the payment refund, never the parent refund.
    with pytest.raises(InvalidEventEnvelopeError):
        _validate_payment_refund(
            _valid_payment_refund_payload(), type="refund", id="refund-1"
        )


def test_non_store_event_type_has_no_payload_contract() -> None:
    with pytest.raises(InvalidEventEnvelopeError):
        validate_store_payload(
            CanonicalEventType.BANK_TRANSACTION_IMPORTED,
            {},
            external_entity_type="bank_transaction",
            external_entity_id="txn-1",
        )
