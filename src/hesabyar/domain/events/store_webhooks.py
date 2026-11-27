"""The generic store-connector contract (US-511, Phase 5-2 stream S1).

A store pushes its financial events to HesabYar through one webhook
receiver (``POST /api/v1/integrations/{connector}/events``). The store
speaks its own nine webhook topics; HesabYar maps each topic to one
or more canonical events (spec §10.2) before the US-501 pipeline
touches it:

=========================  ============================================
Store webhook              Canonical event(s)
=========================  ============================================
``order.completed``        ``sales.order.completed``
``order.cancelled``        ``sales.order.cancelled``
``refund.created``         ``sales.refund.created`` + ``payment.refunded``
``payment.captured``       ``payment.captured``
``payment.refunded``       ``payment.refunded``
``cheque.registered``      ``cheque.received.registered`` (US-533)
``cheque.cancelled``       ``cheque.received.cancelled`` (US-533)
``bank_transfer.recorded`` *pending ADR approval* (``payment.bank_transfer.recorded``)
``shipment.dispatched``    *pending ADR approval* (packaging + shipment-cost consumers)
=========================  ============================================

Only the twelve canonical types may be used — a new type name
needs ADR approval first (ADR 0003 round-2 update 8). The two
pending rows are defined here so the mapping is explicit and
reviewable, but the receiver rejects them with
``EVENT_TYPE_PENDING_ADR_APPROVAL`` (422) until the ADR lands; they
are never silently ingested under a made-up type. The cheque rows
were pending until ADR 0004 (US-530) approved the names and US-533
wired them in; the store's cancellation topic ``cheque.cancelled``
is the ninth store topic (ADR 0004: "the site's cancellation
topic"). The twelfth canonical type,
``installment.schedule.created`` (ADR 0005, US-540), is derived
inside HesabYar and is NOT a store topic — it has no row here.

How the topic arrives: the store sends its webhook topic (or, for
callers that already speak canonical events, the canonical type) in
the ``X-HesabYar-Event-Type`` header — the US-501 contract. The
receiver resolves the header value with
:func:`resolve_header_event_types`: a canonical type passes through
unchanged, a store topic maps through the table above, anything else
is ``EVENT_UNKNOWN_TYPE``.

Fan-out: ``refund.created`` maps to TWO canonical events. The second
(``payment.refunded``) is derived from the first with
:func:`derive_fanout_event`: a new HesabYar id, a derived
``source_event_id`` (``{original}:payment-refunded`` — the shared
webhook-vs-poll convention, see below) so the §10.4
source-event constraint sees two distinct events, and a derived
idempotency key. The derivation is deterministic, so a re-sent
webhook re-derives the same pair and replays as duplicates.

Per-type payload validation (:func:`validate_store_payload`) runs in
the store document builder — after money conversion, so amounts are
whole-Rial integers by then. A missing required field, an invalid
amount, or an arithmetic mismatch fails closed to the review queue
(permanent failure, never a guessed document), exactly as the phased
design §2 §1.2 requires. The validation also enforces the
external-entity contract: every store event must identify its OWN
entity (the order, the refund, the payment, the cancellation) — the
§10.4 third constraint is what makes the engine effect exactly-once,
and a cancel/refund that pointed at the parent order would be
suppressed by the order's own mapping.

One webhook per payment (ADR 0003 / phased design §2 §1.3): gateways
have no server webhook, so the store takes the gateway callback,
verifies it, and sends the result. A ``payment.captured`` event is
therefore sent only after a successful Verify; a payload that
declares ``verified: false`` is rejected.

Installments: an installment order carries its installment fields
inside ``order.completed`` (``installment`` object). They are
carried through normalization (money fields converted) and stored
with the event for the 5ج installments phase. The derived
``installment.schedule.created`` event is NOT built here — its name
is approved by ADR 0005 (US-540) and the derivation lands in a
later 5ج story; it is never a store webhook topic.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum, unique
from typing import Any

from hesabyar.domain.cheques.errors import ChequeValidationError
from hesabyar.domain.cheques.validation import validate_registration_payload
from hesabyar.domain.events.envelope import CanonicalEvent, CanonicalEventType
from hesabyar.domain.events.errors import (
    EventTypePendingAdrError,
    InvalidEventEnvelopeError,
    UnknownEventTypeError,
)

#: Canonical event types the store document builder handles (US-511).
#: The cheque intake types are in this set so the builder's guard
#: accepts them — but the builder never builds a document for them:
#: the pipeline branches cheque events to the intake handler before
#: the builder runs (US-533), and :meth:`StoreDraftBuilder.build`
#: refuses them explicitly. The settlement type is NOT in this set:
#: the pipeline branches ``payment.settlement.received`` to the
#: settlement-received handler before the builder runs (US-554).
#: The remaining canonical types (purchase invoice, inventory cost,
#: bank transaction) belong to later streams and fail closed to the
#: review queue when they reach the store builder.
STORE_DOCUMENT_EVENT_TYPES: frozenset[CanonicalEventType] = frozenset(
    {
        CanonicalEventType.SALES_ORDER_COMPLETED,
        CanonicalEventType.SALES_ORDER_CANCELLED,
        CanonicalEventType.SALES_REFUND_CREATED,
        CanonicalEventType.PAYMENT_CAPTURED,
        CanonicalEventType.PAYMENT_REFUNDED,
        CanonicalEventType.CHEQUE_RECEIVED_REGISTERED,
        CanonicalEventType.CHEQUE_RECEIVED_CANCELLED,
    }
)

#: The §10.4 external-entity type for a site cheque registration.
#: The entity id is the cheque record id on the store site (ADR
#: 0004); the business key the intake converges on is the payload's
#: ``sayad_id``.
CHEQUE_ENTITY_TYPE = "cheque"

#: External-entity type for a site cheque cancellation. A
#: cancellation is its own entity — never the registered cheque —
#: so the §10.4 exactly-once pre-check does not suppress it when
#: the registration's own mapping already exists (the same pattern
#: as ``order_cancellation``).
CHEQUE_CANCELLATION_ENTITY_TYPE = "cheque_cancellation"

#: External-entity type for an order cancellation. A cancellation is
#: its own entity — never the parent order — so the §10.4 exactly-once
#: pre-check does not suppress it when the order's sale already exists.
ORDER_CANCELLATION_ENTITY_TYPE = "order_cancellation"

#: External-entity type for a payment refund (one per refund, not per
#: payment: a payment refunded twice has two distinct refund legs).
PAYMENT_REFUND_ENTITY_TYPE = "payment_refund"

#: Suffix appended to a refund's store event id for its second
#: canonical event (``payment.refunded``); the first event
#: (``sales.refund.created``) keeps the bare store event id. Shared
#: verbatim with the US-512 polling path
#: (``domain/events/sync.py::REFUND_PAYMENT_EVENT_SUFFIX``) — the two
#: paths MUST derive the same id, or a refund that arrives by webhook
#: and is later re-polled ingests its payment leg twice under
#: different idempotency keys. Change it in both places or not at all.
REFUND_PAYMENT_EVENT_SUFFIX = ":payment-refunded"

#: Default revenue-category / shipping source key when the store sends
#: no category: the installation maps ``"default"`` to its main
#: revenue / shipping account in the mapping center (§10.5).
DEFAULT_MAPPING_SOURCE_KEY = "default"


# ---------------------------------------------------------------------------
# Store webhook topics → canonical events
# ---------------------------------------------------------------------------


@unique
class StoreWebhookTopic(StrEnum):
    """The nine outbound webhooks of the generic store contract."""

    ORDER_COMPLETED = "order.completed"
    ORDER_CANCELLED = "order.cancelled"
    REFUND_CREATED = "refund.created"
    PAYMENT_CAPTURED = "payment.captured"
    PAYMENT_REFUNDED = "payment.refunded"
    CHEQUE_REGISTERED = "cheque.registered"
    CHEQUE_CANCELLED = "cheque.cancelled"
    BANK_TRANSFER_RECORDED = "bank_transfer.recorded"
    SHIPMENT_DISPATCHED = "shipment.dispatched"


@dataclass(frozen=True)
class StoreWebhookMapping:
    """One store webhook topic mapped to its canonical event(s).

    ``canonical_types`` is empty when ``pending_adr`` is True: the
    proposed target names live in ``proposed_targets`` for review,
    but no canonical event is produced until the ADR approves them.
    """

    topic: StoreWebhookTopic
    canonical_types: tuple[CanonicalEventType, ...]
    pending_adr: bool = False
    proposed_targets: tuple[str, ...] = ()
    note: str = ""


STORE_WEBHOOK_MAPPINGS: Mapping[str, StoreWebhookMapping] = {
    StoreWebhookTopic.ORDER_COMPLETED.value: StoreWebhookMapping(
        topic=StoreWebhookTopic.ORDER_COMPLETED,
        canonical_types=(CanonicalEventType.SALES_ORDER_COMPLETED,),
        note="A completed store order becomes a customer invoice draft in Tryton.",
    ),
    StoreWebhookTopic.ORDER_CANCELLED.value: StoreWebhookMapping(
        topic=StoreWebhookTopic.ORDER_CANCELLED,
        canonical_types=(CanonicalEventType.SALES_ORDER_CANCELLED,),
        note="Cancels the draft invoice, or credits a posted one — never a rewrite.",
    ),
    StoreWebhookTopic.REFUND_CREATED.value: StoreWebhookMapping(
        topic=StoreWebhookTopic.REFUND_CREATED,
        canonical_types=(
            CanonicalEventType.SALES_REFUND_CREATED,
            CanonicalEventType.PAYMENT_REFUNDED,
        ),
        note=(
            "Fan-out: the refund leg (credit note) and the payment leg "
            "(payable payment) are two canonical events from one webhook."
        ),
    ),
    StoreWebhookTopic.PAYMENT_CAPTURED.value: StoreWebhookMapping(
        topic=StoreWebhookTopic.PAYMENT_CAPTURED,
        canonical_types=(CanonicalEventType.PAYMENT_CAPTURED,),
        note="One webhook per payment, sent only after a successful gateway Verify.",
    ),
    StoreWebhookTopic.PAYMENT_REFUNDED.value: StoreWebhookMapping(
        topic=StoreWebhookTopic.PAYMENT_REFUNDED,
        canonical_types=(CanonicalEventType.PAYMENT_REFUNDED,),
        note="A refunded payment becomes a payable payment draft (money out).",
    ),
    StoreWebhookTopic.CHEQUE_REGISTERED.value: StoreWebhookMapping(
        topic=StoreWebhookTopic.CHEQUE_REGISTERED,
        canonical_types=(CanonicalEventType.CHEQUE_RECEIVED_REGISTERED,),
        note=(
            "Cheque intake (US-533): one account.check in state "
            "registered, NO journal — the first journal is issued only "
            "at the held transition."
        ),
    ),
    StoreWebhookTopic.CHEQUE_CANCELLED.value: StoreWebhookMapping(
        topic=StoreWebhookTopic.CHEQUE_CANCELLED,
        canonical_types=(CanonicalEventType.CHEQUE_RECEIVED_CANCELLED,),
        note=(
            "Site cancellation of a cheque registration (US-533): "
            "before held it voids the intake; after held it goes to "
            "the review queue (manual return, never automatic void)."
        ),
    ),
    StoreWebhookTopic.BANK_TRANSFER_RECORDED.value: StoreWebhookMapping(
        topic=StoreWebhookTopic.BANK_TRANSFER_RECORDED,
        canonical_types=(),
        pending_adr=True,
        proposed_targets=("payment.bank_transfer.recorded",),
        note="Bank-transfer recording lands with the bank layer (5ت); the event name needs ADR approval first.",
    ),
    StoreWebhookTopic.SHIPMENT_DISPATCHED.value: StoreWebhookMapping(
        topic=StoreWebhookTopic.SHIPMENT_DISPATCHED,
        canonical_types=(),
        pending_adr=True,
        proposed_targets=("inventory.packaging.consumed", "shipment.cost.charged"),
        note=(
            "Packaging-consumption + shipment-cost consumers land with phase 5چ "
            "(US-515); the event names need ADR approval first."
        ),
    ),
}


def map_store_webhook(topic: str) -> StoreWebhookMapping:
    """Map a store webhook topic to its canonical event(s).

    Raises :class:`UnknownEventTypeError` for a topic that is neither
    a canonical type nor one of the eight store topics — the receiver
    never guesses a mapping.
    """
    mapping = STORE_WEBHOOK_MAPPINGS.get(topic.strip())
    if mapping is None:
        raise UnknownEventTypeError(
            f"Unknown store webhook topic {topic!r}; known topics: "
            f"{', '.join(sorted(STORE_WEBHOOK_MAPPINGS))}.",
            details={"event_type": topic},
        )
    return mapping


def resolve_header_event_types(value: str | None) -> tuple[CanonicalEventType, ...]:
    """Resolve the ``X-HesabYar-Event-Type`` header to canonical type(s).

    A canonical type passes through unchanged (the US-501 contract);
    a store webhook topic maps through :data:`STORE_WEBHOOK_MAPPINGS`
    (``refund.created`` fans out to two types). A topic whose target
    is pending ADR approval is rejected — never ingested under an
    unapproved name. A missing header is an envelope error; an
    unknown value is ``EVENT_UNKNOWN_TYPE``.
    """
    if value is None or not value.strip():
        raise InvalidEventEnvelopeError(
            "Field 'event_type' is required and must be a non-empty string.",
            details={"field": "event_type"},
        )
    raw = value.strip()
    try:
        return (CanonicalEventType(raw),)
    except ValueError:
        pass
    mapping = STORE_WEBHOOK_MAPPINGS.get(raw)
    if mapping is None:
        raise UnknownEventTypeError(
            f"Unknown canonical event type {raw!r}; new event names need "
            "ADR approval before use.",
            details={"event_type": raw},
        )
    if mapping.pending_adr:
        raise EventTypePendingAdrError(
            f"Store webhook {raw!r} maps to "
            f"{', '.join(mapping.proposed_targets)}, which "
            "need ADR approval before use; the webhook cannot be ingested yet.",
            details={
                "webhook_topic": raw,
                "proposed_targets": list(mapping.proposed_targets),
            },
        )
    return mapping.canonical_types


def derive_fanout_event(event: CanonicalEvent, canonical_type: CanonicalEventType) -> CanonicalEvent:
    """Derive a fan-out canonical event from one store webhook.

    The derived event shares the webhook's payload (and therefore its
    payload hash) but carries its own HesabYar id, a derived
    ``source_event_id`` and a derived idempotency key, so the §10.4
    constraints treat it as a distinct event. The derivation is
    deterministic: a re-sent webhook re-derives the same event and
    replays as a duplicate.

    The derived id follows the shared webhook-vs-poll convention
    (US-512, ``domain/events/sync.py``): the first event keeps the
    bare store event id and the ``payment.refunded`` leg appends
    ``:payment-refunded`` — the same id the polling path derives
    for the same refund, so a refund that arrives by webhook and is
    later re-polled dedups on both events instead of ingesting the
    payment leg twice. Any future fan-out target needs its own
    suffix agreed with the sync path first; an unmapped target
    raises rather than inventing one.

    The external entity is re-pointed at the derived event's own
    entity: for the ``payment.refunded`` leg of a refund, the payment
    refund (``payment_refund`` / refund id), never the parent refund.
    """
    if canonical_type is not CanonicalEventType.PAYMENT_REFUNDED:
        raise ValueError(
            f"No fan-out suffix is defined for {canonical_type.value!r}: "
            "a new fan-out target needs its suffix agreed with the sync "
            "path (US-512) before it can be derived."
        )
    derived_source_event_id = f"{event.source_event_id}{REFUND_PAYMENT_EVENT_SUFFIX}"
    entity_type = event.external_entity_type
    entity_id = event.external_entity_id
    refund_id = event.payload.get("refund_id")
    entity_type = PAYMENT_REFUND_ENTITY_TYPE
    entity_id = (
        refund_id
        if isinstance(refund_id, str) and refund_id.strip()
        else derived_source_event_id
    )
    return CanonicalEvent(
        id=str(uuid.uuid4()),
        schema_version=event.schema_version,
        source_system=event.source_system,
        source_store_id=event.source_store_id,
        source_event_id=derived_source_event_id,
        occurred_at=event.occurred_at,
        received_at=event.received_at,
        company_mapping=event.company_mapping,
        currency=event.currency,
        external_entity_type=entity_type,
        external_entity_id=entity_id,
        correlation_id=event.correlation_id,
        idempotency_key=(
            f"event:{event.source_system}:{event.source_store_id}:{derived_source_event_id}"
        ),
        payload=event.payload,
        payload_hash=event.payload_hash,
    )


# ---------------------------------------------------------------------------
# Order-status mapping rules (phased design §2 §1.4)
# ---------------------------------------------------------------------------


@unique
class OrderStatusAction(StrEnum):
    """What HesabYar does with a store order status."""

    CREATE_DOCUMENT = "create_document"
    """The order is final: it becomes an invoice (draft by default)."""

    IGNORE = "ignore"
    """Not final yet (pending / on-hold / processing): no document, no error."""

    CANCEL = "cancel"
    """The order was cancelled: cancel the draft invoice or credit a posted one."""

    REFUND = "refund"
    """The order was refunded: refund handling (credit note + payable payment)."""

    REVIEW = "review"
    """Unknown status: fail closed to the review queue, never guessed."""


#: Store order status (lowercased) → handling. The webhook topics
#: carry the transition, so these rules classify the ``status`` field
#: inside a payload (contradictions fail closed) and the orders a
#: reconciliation poll fetches (US-512).
ORDER_STATUS_RULES: Mapping[str, OrderStatusAction] = {
    "completed": OrderStatusAction.CREATE_DOCUMENT,
    "paid": OrderStatusAction.CREATE_DOCUMENT,
    "fulfilled": OrderStatusAction.CREATE_DOCUMENT,
    "done": OrderStatusAction.CREATE_DOCUMENT,
    "pending": OrderStatusAction.IGNORE,
    "on-hold": OrderStatusAction.IGNORE,
    "on_hold": OrderStatusAction.IGNORE,
    "processing": OrderStatusAction.IGNORE,
    "awaiting-payment": OrderStatusAction.IGNORE,
    "awaiting_payment": OrderStatusAction.IGNORE,
    "awaiting-fulfillment": OrderStatusAction.IGNORE,
    "awaiting_fulfillment": OrderStatusAction.IGNORE,
    "cancelled": OrderStatusAction.CANCEL,
    "canceled": OrderStatusAction.CANCEL,
    "refunded": OrderStatusAction.REFUND,
    "partially-refunded": OrderStatusAction.REFUND,
    "partially_refunded": OrderStatusAction.REFUND,
}

#: Statuses that agree with an ``order.completed`` webhook: the
#: payload's status, when present, must be one of these.
COMPLETED_ORDER_STATUSES: frozenset[str] = frozenset(
    status for status, action in ORDER_STATUS_RULES.items() if action is OrderStatusAction.CREATE_DOCUMENT
)


def order_status_action(status: str | None) -> OrderStatusAction:
    """Classify a store order status (case-insensitive).

    Unknown, missing or blank statuses fail closed to REVIEW — the
    caller never guesses what an unfamiliar status means.
    """
    if status is None:
        return OrderStatusAction.REVIEW
    return ORDER_STATUS_RULES.get(status.strip().lower(), OrderStatusAction.REVIEW)


# ---------------------------------------------------------------------------
# Customer / product matching rules (phased design §2 §1.4)
# ---------------------------------------------------------------------------


def customer_match_keys(customer: Mapping[str, Any]) -> tuple[str, ...]:
    """Ordered matching keys for a store customer.

    The external customer id is the primary key (resolved through the
    mapping center's ``external_party`` kind); email and phone are
    recorded as fallback hints for the operator when the id has no
    mapping — matching by contact alone is never done silently, so an
    unmatched customer fails closed to the review queue.
    """
    keys: list[str] = []
    customer_id = customer.get("id")
    if isinstance(customer_id, str) and customer_id.strip():
        keys.append(f"external_id:{customer_id.strip()}")
    email = customer.get("email")
    if isinstance(email, str) and email.strip():
        keys.append(f"email:{email.strip().lower()}")
    phone = customer.get("phone")
    if isinstance(phone, str) and phone.strip():
        keys.append(f"phone:{phone.strip()}")
    return tuple(keys)


def product_match_key(line: Mapping[str, Any]) -> str:
    """The matching key for a store order line: its SKU.

    SKUs resolve through the §10.4 external-id map
    (``(source_system, "product", sku)`` → Tryton product); an
    unmapped SKU fails closed to the review queue — products are
    never auto-created from a webhook.
    """
    sku = line.get("product_sku")
    if not isinstance(sku, str) or not sku.strip():
        raise InvalidEventEnvelopeError(
            "Order line field 'product_sku' is required and must be a non-empty string.",
            details={"field": "lines[].product_sku"},
        )
    return sku.strip()


# ---------------------------------------------------------------------------
# Per-type payload validation (phased design §2 §1.2)
# ---------------------------------------------------------------------------


def _require_string(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise InvalidEventEnvelopeError(
            f"Payload field {field!r} is required and must be a non-empty string.",
            details={"field": field},
        )
    return value.strip()


def _optional_string(payload: Mapping[str, Any], field: str) -> str | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise InvalidEventEnvelopeError(
            f"Payload field {field!r} must be a non-empty string when present.",
            details={"field": field},
        )
    return value.strip()


def _require_amount(payload: Mapping[str, Any], field: str, *, positive: bool) -> int:
    """A converted money amount: a whole-Rial integer, never a float/bool."""
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidEventEnvelopeError(
            f"Payload field {field!r} is required and must be a whole-Rial integer amount.",
            details={"field": field},
        )
    if positive and value <= 0:
        raise InvalidEventEnvelopeError(
            f"Payload field {field!r} must be greater than zero, got {value}.",
            details={"field": field, "value": value},
        )
    if not positive and value < 0:
        raise InvalidEventEnvelopeError(
            f"Payload field {field!r} must not be negative, got {value}.",
            details={"field": field, "value": value},
        )
    return value


def _optional_amount(payload: Mapping[str, Any], field: str) -> int | None:
    """An optional converted money amount; null means unknown, never zero."""
    value = payload.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidEventEnvelopeError(
            f"Payload field {field!r} must be a non-negative whole-Rial integer amount or null.",
            details={"field": field},
        )
    return value


def _optional_number(payload: Mapping[str, Any], field: str) -> float | None:
    value = payload.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise InvalidEventEnvelopeError(
            f"Payload field {field!r} must be a number when present.",
            details={"field": field},
        )
    return float(value)


def _require_customer(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    customer = payload.get("customer")
    if not isinstance(customer, Mapping):
        raise InvalidEventEnvelopeError(
            "Payload field 'customer' is required and must be a JSON object.",
            details={"field": "customer"},
        )
    _require_string(customer, "id")
    _optional_string(customer, "email")
    _optional_string(customer, "phone")
    return customer


def _optional_customer(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    customer = payload.get("customer")
    if customer is None:
        return None
    if not isinstance(customer, Mapping):
        raise InvalidEventEnvelopeError(
            "Payload field 'customer' must be a JSON object when present.",
            details={"field": "customer"},
        )
    _require_string(customer, "id")
    _optional_string(customer, "email")
    _optional_string(customer, "phone")
    return customer


def _quantity_to_decimal(value: Any, field: str) -> Decimal:
    """A line quantity as an exact Decimal (never a binary float)."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise InvalidEventEnvelopeError(
            f"Payload field {field!r} must be a number.",
            details={"field": field},
        )
    try:
        quantity = Decimal(str(value))
    except InvalidOperation as exc:
        raise InvalidEventEnvelopeError(
            f"Payload field {field!r} is not a valid number: {value!r}.",
            details={"field": field},
        ) from exc
    if not quantity.is_finite() or quantity <= 0:
        raise InvalidEventEnvelopeError(
            f"Payload field {field!r} must be a finite number greater than zero.",
            details={"field": field},
        )
    return quantity


def _validate_order_lines(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    lines = payload.get("lines")
    if not isinstance(lines, list) or not lines:
        raise InvalidEventEnvelopeError(
            "Payload field 'lines' is required and must be a non-empty list.",
            details={"field": "lines"},
        )
    validated: list[dict[str, Any]] = []
    for index, raw_line in enumerate(lines):
        if not isinstance(raw_line, Mapping):
            raise InvalidEventEnvelopeError(
                f"Payload field 'lines[{index}]' must be a JSON object.",
                details={"field": f"lines[{index}]"},
            )
        line = dict(raw_line)
        product_match_key(line)
        quantity = _quantity_to_decimal(line.get("quantity"), f"lines[{index}].quantity")
        unit_price = _require_amount(line, "unit_price", positive=False)
        line_amount = quantity * unit_price
        if line_amount != line_amount.to_integral_value():
            raise InvalidEventEnvelopeError(
                f"Order line {index} amount must be an integral number of Rials "
                f"(quantity {quantity} x unit price {unit_price} IRR = {line_amount} IRR); "
                "fractional Rial amounts are never rounded.",
                details={"field": f"lines[{index}]"},
            )
        _optional_amount(line, "discount_amount")
        line_total = _optional_amount(line, "line_total")
        if line_total is not None and line_total != int(line_amount):
            raise InvalidEventEnvelopeError(
                f"Order line {index} line_total ({line_total}) does not match "
                f"quantity x unit price ({int(line_amount)}).",
                details={"field": f"lines[{index}].line_total"},
            )
        _optional_string(line, "description")
        _optional_string(line, "revenue_category")
        validated.append(line)
    return validated


def _validate_installment(payload: Mapping[str, Any]) -> None:
    """Installment fields inside order.completed are CARRIED, not acted on.

    The 5ج installments phase derives the schedule from these fields;
    here they are only shape-checked (and their money fields converted
    at the connector boundary like every other amount). No derived
    ``installment.schedule.created`` event is created here — the name
    is approved by ADR 0005 (US-540) and the derivation lands in a
    later 5ج story.
    """
    installment = payload.get("installment")
    if installment is None:
        return
    if not isinstance(installment, Mapping):
        raise InvalidEventEnvelopeError(
            "Payload field 'installment' must be a JSON object when present.",
            details={"field": "installment"},
        )
    count = installment.get("installment_count")
    if count is not None and (isinstance(count, bool) or not isinstance(count, int) or count <= 0):
        raise InvalidEventEnvelopeError(
            "Payload field 'installment.installment_count' must be a positive integer when present.",
            details={"field": "installment.installment_count"},
        )
    _optional_amount(installment, "down_payment_amount")
    _optional_amount(installment, "installment_amount")
    _optional_string(installment, "interval")
    _optional_string(installment, "first_due_date")


def _validate_external_entity(
    event_type: CanonicalEventType,
    payload: Mapping[str, Any],
    *,
    external_entity_type: str | None,
    external_entity_id: str | None,
    expected_type: str,
    expected_id: str,
) -> None:
    """The envelope must identify the event's OWN entity (§10.4).

    A store event without an entity cannot be made exactly-once at
    the engine (a reprocessed event would draft twice), and an entity
    pointing at the wrong record (e.g. a cancellation pointing at the
    parent order) would be suppressed by that record's own mapping.
    Both fail closed to the review queue.
    """
    if external_entity_type is None or external_entity_id is None:
        raise InvalidEventEnvelopeError(
            f"A {event_type.value} event must carry external_entity_type and "
            "external_entity_id identifying its own entity.",
            details={"event_type": event_type.value},
        )
    if external_entity_type != expected_type or external_entity_id != expected_id:
        raise InvalidEventEnvelopeError(
            f"A {event_type.value} event must identify its own entity "
            f"({expected_type}/{expected_id}), got "
            f"{external_entity_type}/{external_entity_id}.",
            details={
                "event_type": event_type.value,
                "external_entity_type": external_entity_type,
                "external_entity_id": external_entity_id,
            },
        )


#: sayad_id shape, shared with the cheque payload validator
#: (``hesabyar.domain.cheques.validation``): exactly 16 digits.
_SAYAD_ID_RE = re.compile(r"^\d{16}$")


def _validate_cheque_registration_payload(
    payload: Mapping[str, Any],
    *,
    external_entity_type: str | None,
    external_entity_id: str | None,
) -> None:
    """Validate a ``cheque.received.registered`` payload (US-533).

    The 24-field contract lives in exactly one place —
    :func:`validate_registration_payload
    <hesabyar.domain.cheques.validation.validate_registration_payload>`
    — and this adapter-boundary check delegates to it, translating
    its error into the pipeline's envelope error so the event fails
    closed to the review queue. The payload still carries Toman
    (``amount_toman`` + explicit ``amount_currency: "IRT"``); the
    ×10 Toman→Rial conversion happens inside that validator, at the
    connector boundary — the generic normalizer deliberately does
    NOT convert ``amount_toman`` (it has no entry in
    ``MONEY_FIELDS_BY_EVENT_TYPE``), because the field is
    self-describing by its explicit unit.

    The envelope entity is the store's own cheque record id (ADR
    0004: ``external_entity_type = "cheque"``); the business key
    the intake converges on is the payload's ``sayad_id``, checked
    by the intake service, not here.
    """
    if external_entity_type is None or external_entity_id is None:
        raise InvalidEventEnvelopeError(
            "A cheque.received.registered event must carry "
            "external_entity_type and external_entity_id identifying "
            "its own entity (the store's cheque record).",
            details={"event_type": "cheque.received.registered"},
        )
    if external_entity_type != CHEQUE_ENTITY_TYPE:
        raise InvalidEventEnvelopeError(
            "A cheque.received.registered event must identify its own "
            f"entity ({CHEQUE_ENTITY_TYPE}/{external_entity_id}), got "
            f"{external_entity_type}/{external_entity_id}.",
            details={
                "event_type": "cheque.received.registered",
                "external_entity_type": external_entity_type,
                "external_entity_id": external_entity_id,
            },
        )
    try:
        validate_registration_payload(payload)
    except ChequeValidationError as exc:
        raise InvalidEventEnvelopeError(
            f"Cheque payload failed validation: {exc.message}",
            details={
                "event_type": "cheque.received.registered",
                "field_errors": exc.field_errors,
            },
        ) from exc


def validate_store_payload(
    event_type: CanonicalEventType,
    payload: Mapping[str, Any],
    *,
    external_entity_type: str | None,
    external_entity_id: str | None,
) -> None:
    """Validate a store event's payload (after money conversion).

    Raises :class:`InvalidEventEnvelopeError` on the first problem —
    the pipeline treats it as a permanent failure and routes the
    event to the review queue, never guessing a document. Amounts
    here are whole-Rial integers (the connector boundary already
    converted them); quantities are exact Decimals.

    The per-type contract is documented in ``docs/store-connector.md``.
    """
    if event_type is CanonicalEventType.SALES_ORDER_COMPLETED:
        order_id = _require_string(payload, "order_id")
        _require_customer(payload)
        lines = _validate_order_lines(payload)
        total_amount = _require_amount(payload, "total_amount", positive=True)
        subtotal = _optional_amount(payload, "subtotal_amount")
        discount = _optional_amount(payload, "discount_amount") or 0
        shipping = _optional_amount(payload, "shipping_amount") or 0
        tax = _optional_amount(payload, "tax_amount") or 0
        line_sum = sum(
            int(_quantity_to_decimal(line.get("quantity"), "quantity") * line["unit_price"])
            for line in lines
        )
        if subtotal is not None and subtotal != line_sum:
            raise InvalidEventEnvelopeError(
                f"Payload subtotal_amount ({subtotal}) does not match the sum of "
                f"line amounts ({line_sum}).",
                details={"field": "subtotal_amount"},
            )
        expected_total = line_sum + shipping - discount + tax
        if total_amount != expected_total:
            raise InvalidEventEnvelopeError(
                f"Payload total_amount ({total_amount}) does not match lines "
                f"({line_sum}) + shipping ({shipping}) - discount ({discount}) "
                f"+ tax ({tax}) = {expected_total}.",
                details={"field": "total_amount"},
            )
        status = _optional_string(payload, "status")
        if status is not None and status.strip().lower() not in COMPLETED_ORDER_STATUSES:
            raise InvalidEventEnvelopeError(
                f"An order.completed payload with status {status!r} contradicts its "
                "topic; completed-equivalent statuses are "
                f"{', '.join(sorted(COMPLETED_ORDER_STATUSES))}.",
                details={"field": "status"},
            )
        _validate_installment(payload)
        _validate_external_entity(
            event_type,
            payload,
            external_entity_type=external_entity_type,
            external_entity_id=external_entity_id,
            expected_type="order",
            expected_id=order_id,
        )
        return

    if event_type is CanonicalEventType.SALES_ORDER_CANCELLED:
        order_id = _require_string(payload, "order_id")
        _optional_string(payload, "reason")
        _optional_amount(payload, "total_amount")
        _optional_string(payload, "cancelled_at")
        # The cancellation_id is the store's own record id for the
        # cancellation — payload metadata, carried but NOT the
        # entity: the cancellation acts on the order, so its entity
        # is always (order_cancellation, order_id). One cancellation
        # per order; a second cancellation of the same order is a
        # duplicate, and a late completion is detectable through the
        # same mapping (see the pipeline pre-check).
        _optional_string(payload, "cancellation_id")
        refund_expected = payload.get("refund_expected")
        if refund_expected is not None and not isinstance(refund_expected, bool):
            raise InvalidEventEnvelopeError(
                "Payload field 'refund_expected' must be a boolean when present.",
                details={"field": "refund_expected"},
            )
        _validate_external_entity(
            event_type,
            payload,
            external_entity_type=external_entity_type,
            external_entity_id=external_entity_id,
            expected_type=ORDER_CANCELLATION_ENTITY_TYPE,
            expected_id=order_id,
        )
        return

    if event_type is CanonicalEventType.SALES_REFUND_CREATED:
        refund_id = _require_string(payload, "refund_id")
        _require_string(payload, "order_id")
        _require_amount(payload, "refund_amount", positive=True)
        _optional_string(payload, "reason")
        _optional_customer(payload)
        _optional_string(payload, "gateway_payment_id")
        _optional_amount(payload, "gross_amount")
        # The refund fans out to a payment.refunded leg, which resolves
        # its payment journal from the channel — so the channel is
        # required here too (a cash refund uses a custom "cash"
        # channel in the registry, never a missing channel).
        _require_string(payload, "payment_channel")
        refund_lines = payload.get("lines")
        if refund_lines is not None:
            if not isinstance(refund_lines, list):
                raise InvalidEventEnvelopeError(
                    "Payload field 'lines' must be a list when present.",
                    details={"field": "lines"},
                )
            for index, raw_line in enumerate(refund_lines):
                if not isinstance(raw_line, Mapping):
                    raise InvalidEventEnvelopeError(
                        f"Payload field 'lines[{index}]' must be a JSON object.",
                        details={"field": f"lines[{index}]"},
                    )
                product_match_key(raw_line)
                _quantity_to_decimal(raw_line.get("quantity"), f"lines[{index}].quantity")
                _optional_amount(raw_line, "line_total")
        _validate_external_entity(
            event_type,
            payload,
            external_entity_type=external_entity_type,
            external_entity_id=external_entity_id,
            expected_type="refund",
            expected_id=refund_id,
        )
        return

    if event_type is CanonicalEventType.PAYMENT_CAPTURED:
        # payment_channel is a first-class field: it selects the
        # channel registry row (and its gateway-journal mapping), so
        # it is required — never inferred from anything else.
        _require_string(payload, "payment_channel")
        gateway_payment_id = _require_string(payload, "gateway_payment_id")
        _require_amount(payload, "gross_amount", positive=True)
        _optional_amount(payload, "fee_amount")
        _optional_amount(payload, "fee_tax_amount")
        _optional_amount(payload, "net_amount")
        fee_rate = _optional_number(payload, "fee_rate_snapshot")
        if fee_rate is not None and not 0 <= fee_rate <= 100:
            raise InvalidEventEnvelopeError(
                "Payload field 'fee_rate_snapshot' must be a percentage in [0, 100].",
                details={"field": "fee_rate_snapshot"},
            )
        _optional_string(payload, "order_id")
        # The customer is optional: when the payload names the order,
        # the builder falls back to the order's invoice party. A
        # payment with neither a customer nor an order fails closed
        # in the builder.
        _optional_customer(payload)
        verified = payload.get("verified")
        if verified is not None and verified is not True:
            raise InvalidEventEnvelopeError(
                "A payment.captured event must only be sent after a successful "
                "gateway Verify; a payload declaring verified=false is rejected.",
                details={"field": "verified"},
            )
        _validate_external_entity(
            event_type,
            payload,
            external_entity_type=external_entity_type,
            external_entity_id=external_entity_id,
            expected_type="payment",
            expected_id=gateway_payment_id,
        )
        return

    if event_type is CanonicalEventType.PAYMENT_REFUNDED:
        refund_id = _require_string(payload, "refund_id")
        _require_amount(payload, "gross_amount", positive=True)
        _optional_amount(payload, "fee_amount")
        _optional_amount(payload, "net_amount")
        _optional_string(payload, "gateway_payment_id")
        _optional_string(payload, "order_id")
        _optional_customer(payload)
        # The payment journal resolves from the channel (gateway_journal
        # mapping), so the channel is required — same as payment.captured.
        _require_string(payload, "payment_channel")
        _validate_external_entity(
            event_type,
            payload,
            external_entity_type=external_entity_type,
            external_entity_id=external_entity_id,
            expected_type=PAYMENT_REFUND_ENTITY_TYPE,
            expected_id=refund_id,
        )
        return

    if event_type is CanonicalEventType.CHEQUE_RECEIVED_REGISTERED:
        _validate_cheque_registration_payload(
            payload,
            external_entity_type=external_entity_type,
            external_entity_id=external_entity_id,
        )
        return

    if event_type is CanonicalEventType.CHEQUE_RECEIVED_CANCELLED:
        sayad_id = _require_string(payload, "sayad_id")
        if not _SAYAD_ID_RE.match(sayad_id):
            raise InvalidEventEnvelopeError(
                "Payload field 'sayad_id' must be exactly 16 digits, no spaces or dashes.",
                details={"field": "sayad_id"},
            )
        _optional_string(payload, "reason")
        _optional_string(payload, "cancelled_at")
        _validate_external_entity(
            event_type,
            payload,
            external_entity_type=external_entity_type,
            external_entity_id=external_entity_id,
            expected_type=CHEQUE_CANCELLATION_ENTITY_TYPE,
            expected_id=sayad_id,
        )
        return

    raise InvalidEventEnvelopeError(
        f"No store payload contract exists for event type {event_type.value!r}.",
        details={"event_type": event_type.value},
    )
