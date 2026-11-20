"""Canonical event envelope (design-eng spec §10.2, US-501).

Every event — external (store/gateway/bank webhook), internal, or
manual — travels in the same 15-field envelope:

``id, schema_version, source_system, source_store_id, source_event_id,
occurred_at, received_at, company_mapping, currency,
external_entity_type, external_entity_id, correlation_id,
idempotency_key, payload, payload_hash``

``event_type`` is the routing discriminator carried alongside the
envelope (stored in ``inbox_events.event_type``, migration 0004);
it is one of the eleven canonical types — the nine of §10.2 plus
the two cheque intake types approved by ADR 0004 (US-530) and
wired ingestible by US-533 — validated by
:func:`parse_event_type` at the receiver — never a field of the
envelope itself. New event names beyond the eleven need ADR approval
(ADR 0003 round-2 update 8).

Schema versioning: new events are ``schema_version=1``. An event
carrying any other version is rejected — a newer producer must not
silently feed an older consumer.

``payload_hash`` is the SHA-256 hex of the canonical JSON of the
payload (sorted keys, compact separators). A supplied hash that does
not match the payload is a validation error, never ignored.
"""

from __future__ import annotations

import base64
import binascii
import datetime
import hashlib
import hmac
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum, unique
from typing import Any

from hesabyar.domain.events.errors import (
    InvalidEventEnvelopeError,
    PayloadHashMismatchError,
    UnknownEventTypeError,
    UnsupportedSchemaVersionError,
)

#: Current canonical-event schema version. New events are version 1.
CURRENT_SCHEMA_VERSION = 1

#: Operation name used for the §10.4 idempotency-key constraint
#: (installation_id, operation, idempotency_key) on event ingest.
EVENT_INGEST_OPERATION = "events.ingest"


@unique
class CanonicalEventType(StrEnum):
    """The eleven canonical event types: the nine of spec §10.2 plus
    the two cheque intake types approved by ADR 0004 (US-530) and
    wired ingestible by US-533."""

    SALES_ORDER_COMPLETED = "sales.order.completed"
    SALES_ORDER_CANCELLED = "sales.order.cancelled"
    SALES_REFUND_CREATED = "sales.refund.created"
    PAYMENT_CAPTURED = "payment.captured"
    PAYMENT_REFUNDED = "payment.refunded"
    PAYMENT_SETTLEMENT_RECEIVED = "payment.settlement.received"
    PURCHASE_INVOICE_RECEIVED = "purchase.invoice.received"
    INVENTORY_COST_RECOGNIZED = "inventory.cost.recognized"
    BANK_TRANSACTION_IMPORTED = "bank.transaction.imported"
    CHEQUE_RECEIVED_REGISTERED = "cheque.received.registered"
    CHEQUE_RECEIVED_CANCELLED = "cheque.received.cancelled"


CANONICAL_EVENT_TYPES: frozenset[str] = frozenset(t.value for t in CanonicalEventType)


def parse_event_type(value: str | None) -> CanonicalEventType:
    """Validate the event type carried alongside an envelope.

    The canonical type travels outside the 15-field envelope — on
    the ingest request it arrives in the ``X-HesabYar-Event-Type``
    header, and the receiver assigns it when normalizing (phased
    design §2 §1.3). A missing type is an envelope error; an
    unknown type is rejected, never silently accepted — new event
    names need ADR approval before use.
    """
    if value is None or not value.strip():
        raise InvalidEventEnvelopeError(
            "Field 'event_type' is required and must be a non-empty string.",
            details={"field": "event_type"},
        )
    event_type_raw = value.strip()
    try:
        return CanonicalEventType(event_type_raw)
    except ValueError as exc:
        raise UnknownEventTypeError(
            f"Unknown canonical event type {event_type_raw!r}; "
            "new event names need ADR approval before use.",
            details={"event_type": event_type_raw},
        ) from exc


def canonical_json(payload: Any) -> str:
    """Canonical JSON of a payload: sorted keys, compact separators, UTF-8.

    The same payload always serializes the same way, whatever order
    the sender used — so hashes and comparisons are order-independent.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def canonical_payload_hash(payload: Mapping[str, Any]) -> str:
    """SHA-256 hex of a payload's canonical JSON form."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def verify_hmac_sha256(secret: str, body: bytes, signature: str) -> bool:
    """Verify an HMAC-SHA256 webhook signature with a timing-safe compare.

    Accepts the signature as hex or base64 (the WooCommerce pattern is
    ``base64(HMAC-SHA256(body, secret))``). An unparseable signature
    simply fails — it is never an exception.
    """
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    candidate = signature.strip()
    for decoded in (
        _try_hex(candidate),
        _try_base64(candidate),
    ):
        if decoded is not None and hmac.compare_digest(decoded, expected):
            return True
    return False


def _try_hex(candidate: str) -> bytes | None:
    try:
        return bytes.fromhex(candidate)
    except ValueError:
        return None


def _try_base64(candidate: str) -> bytes | None:
    try:
        return base64.b64decode(candidate, validate=True)
    except (binascii.Error, ValueError):
        return None


def _parse_datetime(value: Any, field_name: str) -> datetime.datetime:
    """Parse an ISO-8601 timestamp into a timezone-aware UTC datetime."""
    if isinstance(value, datetime.datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.datetime.fromisoformat(text)
        except ValueError as exc:
            raise InvalidEventEnvelopeError(
                f"Field {field_name!r} is not a valid ISO-8601 timestamp: {value!r}.",
                details={"field": field_name},
            ) from exc
    else:
        raise InvalidEventEnvelopeError(
            f"Field {field_name!r} must be an ISO-8601 timestamp string.",
            details={"field": field_name},
        )
    if parsed.tzinfo is None:
        # A naive timestamp is assumed UTC and recorded as such — the
        # assumption is explicit here, not hidden in a reader.
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return parsed.astimezone(datetime.UTC)


def _require_string(raw: Mapping[str, Any], field_name: str) -> str:
    value = raw.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise InvalidEventEnvelopeError(
            f"Field {field_name!r} is required and must be a non-empty string.",
            details={"field": field_name},
        )
    return value.strip()


def _optional_string(raw: Mapping[str, Any], field_name: str) -> str | None:
    value = raw.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidEventEnvelopeError(
            f"Field {field_name!r} must be a string.",
            details={"field": field_name},
        )
    stripped = value.strip()
    return stripped or None


@dataclass(frozen=True, slots=True)
class CanonicalEvent:
    """A validated canonical event envelope (§10.2).

    Immutable: the envelope is the contract between the store and
    HesabYar, and nothing downstream rewrites it. Normalization
    produces a new :class:`NormalizedEvent` instead.
    """

    id: str
    schema_version: int
    source_system: str
    source_store_id: str
    source_event_id: str
    occurred_at: datetime.datetime
    received_at: datetime.datetime
    company_mapping: Mapping[str, Any] | None
    currency: str | None
    external_entity_type: str | None
    external_entity_id: str | None
    correlation_id: str | None
    idempotency_key: str
    payload: Mapping[str, Any]
    payload_hash: str

    @classmethod
    def from_raw(
        cls,
        raw: Mapping[str, Any],
        *,
        received_at: datetime.datetime | None = None,
        event_id: str | None = None,
    ) -> CanonicalEvent:
        """Validate a raw envelope dict into a canonical event.

        Missing ``schema_version`` defaults to 1 (new events); a
        present version other than 1 is rejected. A missing
        ``payload_hash`` is computed; a present one must match.
        A missing ``idempotency_key`` defaults to the source-event
        triple, so every event has a business idempotency key.

        The envelope carries no ``event_type`` — the type travels
        alongside it and is validated separately with
        :func:`parse_event_type`. An ``event_type`` key inside the
        raw dict is ignored: it is not one of the 15 §10.2 fields.
        """
        if not isinstance(raw, Mapping):
            raise InvalidEventEnvelopeError("The event envelope must be a JSON object.")

        schema_version = raw.get("schema_version", CURRENT_SCHEMA_VERSION)
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise InvalidEventEnvelopeError(
                "Field 'schema_version' must be an integer.",
                details={"field": "schema_version"},
            )
        if schema_version != CURRENT_SCHEMA_VERSION:
            raise UnsupportedSchemaVersionError(
                f"Unsupported canonical event schema version {schema_version}; "
                f"this build reads version {CURRENT_SCHEMA_VERSION} only.",
                details={"schema_version": schema_version},
            )

        payload = raw.get("payload")
        if not isinstance(payload, Mapping):
            raise InvalidEventEnvelopeError(
                "Field 'payload' is required and must be a JSON object.",
                details={"field": "payload"},
            )
        payload_dict: dict[str, Any] = dict(payload)
        computed_hash = canonical_payload_hash(payload_dict)
        supplied_hash = _optional_string(raw, "payload_hash")
        if supplied_hash is not None and not hmac.compare_digest(supplied_hash.lower(), computed_hash):
            raise PayloadHashMismatchError(
                "The supplied payload_hash does not match the payload.",
                details={"supplied_hash": supplied_hash, "computed_hash": computed_hash},
            )

        source_system = _require_string(raw, "source_system")
        source_store_id = _require_string(raw, "source_store_id")
        source_event_id = _require_string(raw, "source_event_id")
        occurred_at = _parse_datetime(raw.get("occurred_at"), "occurred_at")
        if received_at is None:
            received_value = raw.get("received_at")
            if received_value is not None:
                received_at = _parse_datetime(received_value, "received_at")
            else:
                received_at = datetime.datetime.now(datetime.UTC)
        elif received_at.tzinfo is None:
            received_at = received_at.replace(tzinfo=datetime.UTC)
        else:
            received_at = received_at.astimezone(datetime.UTC)

        company_mapping = raw.get("company_mapping")
        if company_mapping is not None and not isinstance(company_mapping, Mapping):
            raise InvalidEventEnvelopeError(
                "Field 'company_mapping' must be a JSON object.",
                details={"field": "company_mapping"},
            )
        currency = _optional_string(raw, "currency")
        if currency is not None:
            currency = currency.upper()
            if len(currency) != 3 or not currency.isalpha():
                raise InvalidEventEnvelopeError(
                    f"Field 'currency' must be a 3-letter code, got {currency!r}.",
                    details={"field": "currency"},
                )

        idempotency_key = _optional_string(raw, "idempotency_key")
        if idempotency_key is None:
            idempotency_key = f"event:{source_system}:{source_store_id}:{source_event_id}"

        return cls(
            id=event_id or _optional_string(raw, "id") or str(uuid.uuid4()),
            schema_version=schema_version,
            source_system=source_system,
            source_store_id=source_store_id,
            source_event_id=source_event_id,
            occurred_at=occurred_at,
            received_at=received_at,
            company_mapping=dict(company_mapping) if company_mapping is not None else None,
            currency=currency,
            external_entity_type=_optional_string(raw, "external_entity_type"),
            external_entity_id=_optional_string(raw, "external_entity_id"),
            correlation_id=_optional_string(raw, "correlation_id"),
            idempotency_key=idempotency_key,
            payload=payload_dict,
            payload_hash=computed_hash,
        )

    def to_envelope_dict(self) -> dict[str, Any]:
        """Render the 15 envelope fields (§10.2) as a plain dict."""
        return {
            "id": self.id,
            "schema_version": self.schema_version,
            "source_system": self.source_system,
            "source_store_id": self.source_store_id,
            "source_event_id": self.source_event_id,
            "occurred_at": self.occurred_at.isoformat(),
            "received_at": self.received_at.isoformat(),
            "company_mapping": dict(self.company_mapping) if self.company_mapping is not None else None,
            "currency": self.currency,
            "external_entity_type": self.external_entity_type,
            "external_entity_id": self.external_entity_id,
            "correlation_id": self.correlation_id,
            "idempotency_key": self.idempotency_key,
            "payload": dict(self.payload),
            "payload_hash": self.payload_hash,
        }
