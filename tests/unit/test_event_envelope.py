"""Unit tests for the canonical event envelope (spec §10.2, US-501).

Pure domain tests — no database, no server.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import hmac
import json

import pytest

from hesabyar.domain.events.envelope import (
    CANONICAL_EVENT_TYPES,
    CURRENT_SCHEMA_VERSION,
    CanonicalEvent,
    CanonicalEventType,
    canonical_payload_hash,
    parse_event_type,
    verify_hmac_sha256,
)
from hesabyar.domain.events.errors import (
    InvalidEventEnvelopeError,
    PayloadHashMismatchError,
    UnknownEventTypeError,
    UnsupportedSchemaVersionError,
)
from tests.event_fixtures import raw_envelope


def test_envelope_has_all_15_fields() -> None:
    """The envelope carries exactly the 15 §10.2 fields, in spec order."""
    event = CanonicalEvent.from_raw(raw_envelope())
    assert [field.name for field in dataclasses.fields(event)] == [
        "id",
        "schema_version",
        "source_system",
        "source_store_id",
        "source_event_id",
        "occurred_at",
        "received_at",
        "company_mapping",
        "currency",
        "external_entity_type",
        "external_entity_id",
        "correlation_id",
        "idempotency_key",
        "payload",
        "payload_hash",
    ]
    rendered = event.to_envelope_dict()
    assert set(rendered) == {
        "id",
        "schema_version",
        "source_system",
        "source_store_id",
        "source_event_id",
        "occurred_at",
        "received_at",
        "company_mapping",
        "currency",
        "external_entity_type",
        "external_entity_id",
        "correlation_id",
        "idempotency_key",
        "payload",
        "payload_hash",
    }
    assert rendered["schema_version"] == CURRENT_SCHEMA_VERSION == 1


def test_envelope_has_no_event_type_field() -> None:
    """event_type travels alongside the envelope — it is not a §10.2 field."""
    raw = raw_envelope()
    assert "event_type" not in raw
    event = CanonicalEvent.from_raw(raw)
    assert not hasattr(event, "event_type")
    # A stray event_type key inside the body is ignored, not read.
    stray = CanonicalEvent.from_raw(raw_envelope(event_type="payment.captured"))
    assert not hasattr(stray, "event_type")
    assert stray.source_event_id == event.source_event_id


def test_new_events_default_to_schema_version_1() -> None:
    """A missing schema_version defaults to 1 (new events)."""
    raw = raw_envelope()
    del raw["schema_version"]
    event = CanonicalEvent.from_raw(raw)
    assert event.schema_version == 1


def test_unsupported_schema_version_is_rejected() -> None:
    """A newer producer must not silently feed an older consumer."""
    with pytest.raises(UnsupportedSchemaVersionError, match="version 2"):
        CanonicalEvent.from_raw(raw_envelope(schema_version=2))


def test_non_integer_schema_version_is_rejected() -> None:
    with pytest.raises(InvalidEventEnvelopeError, match="schema_version"):
        CanonicalEvent.from_raw(raw_envelope(schema_version="1"))


def test_all_twelve_canonical_event_types_are_accepted() -> None:
    """Every §10.2 event type parses (validated alongside the envelope).

    Nine from US-501 plus the two cheque names wired by US-533
    (ADR 0004) plus ``installment.schedule.created`` approved by
    ADR 0005 (US-540).
    """
    assert len(CANONICAL_EVENT_TYPES) == 12
    for event_type in CanonicalEventType:
        assert parse_event_type(event_type.value) == event_type


def test_unknown_event_type_is_rejected() -> None:
    """New event names need ADR approval — they are rejected, never accepted."""
    with pytest.raises(UnknownEventTypeError, match="payment.bank_transfer.recorded"):
        parse_event_type("payment.bank_transfer.recorded")


def test_missing_event_type_is_rejected() -> None:
    """The alongside type is required — a missing type is an envelope error."""
    with pytest.raises(InvalidEventEnvelopeError, match="event_type"):
        parse_event_type(None)
    with pytest.raises(InvalidEventEnvelopeError, match="event_type"):
        parse_event_type("   ")


def test_missing_required_field_is_rejected() -> None:
    for field in ("source_system", "source_store_id", "source_event_id", "occurred_at"):
        raw = raw_envelope()
        del raw[field]
        with pytest.raises(InvalidEventEnvelopeError, match=field):
            CanonicalEvent.from_raw(raw)


def test_payload_must_be_an_object() -> None:
    raw = raw_envelope()
    raw["payload"] = None
    with pytest.raises(InvalidEventEnvelopeError, match="payload"):
        CanonicalEvent.from_raw(raw)
    raw = raw_envelope()
    raw["payload"] = [1, 2]
    with pytest.raises(InvalidEventEnvelopeError, match="payload"):
        CanonicalEvent.from_raw(raw)


def test_payload_hash_is_computed_when_absent() -> None:
    """A missing payload_hash is computed from the canonical payload."""
    payload = {"total_amount": 175000, "b": 1, "a": 2}
    event = CanonicalEvent.from_raw(raw_envelope(payload=payload))
    assert event.payload_hash == canonical_payload_hash(payload)


def test_payload_hash_is_order_independent() -> None:
    """The same payload hashes the same whatever order the sender used."""
    first = canonical_payload_hash({"a": 1, "b": {"c": 2, "d": 3}})
    second = canonical_payload_hash({"b": {"d": 3, "c": 2}, "a": 1})
    assert first == second
    assert first == hashlib.sha256(
        json.dumps({"a": 1, "b": {"c": 2, "d": 3}}, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def test_supplied_payload_hash_must_match() -> None:
    """A supplied hash that does not match the payload is an error, never ignored."""
    with pytest.raises(PayloadHashMismatchError):
        CanonicalEvent.from_raw(raw_envelope(payload_hash="0" * 64))


def test_supplied_matching_payload_hash_is_accepted() -> None:
    payload = {"total_amount": 175000}
    event = CanonicalEvent.from_raw(
        raw_envelope(payload=payload, payload_hash=canonical_payload_hash(payload))
    )
    assert event.payload_hash == canonical_payload_hash(payload)


def test_idempotency_key_defaults_to_source_event_triple() -> None:
    """Every event has a business idempotency key, even when the store sends none."""
    event = CanonicalEvent.from_raw(raw_envelope())
    assert event.idempotency_key == "event:hayat:store-1:evt-1"


def test_explicit_idempotency_key_is_kept() -> None:
    event = CanonicalEvent.from_raw(raw_envelope(idempotency_key="store-key-99"))
    assert event.idempotency_key == "store-key-99"


def test_occurred_at_parses_z_suffix_and_offsets() -> None:
    event = CanonicalEvent.from_raw(raw_envelope(occurred_at="2026-11-17T10:00:00Z"))
    assert event.occurred_at == datetime.datetime(2026, 11, 17, 10, 0, tzinfo=datetime.UTC)
    event = CanonicalEvent.from_raw(raw_envelope(occurred_at="2026-11-17T13:30:00+03:30"))
    assert event.occurred_at == datetime.datetime(2026, 11, 17, 10, 0, tzinfo=datetime.UTC)


def test_naive_occurred_at_is_assumed_utc() -> None:
    event = CanonicalEvent.from_raw(raw_envelope(occurred_at="2026-11-17T10:00:00"))
    assert event.occurred_at == datetime.datetime(2026, 11, 17, 10, 0, tzinfo=datetime.UTC)


def test_invalid_occurred_at_is_rejected() -> None:
    with pytest.raises(InvalidEventEnvelopeError, match="occurred_at"):
        CanonicalEvent.from_raw(raw_envelope(occurred_at="not-a-date"))


def test_currency_is_normalized_and_validated() -> None:
    event = CanonicalEvent.from_raw(raw_envelope(currency="irr"))
    assert event.currency == "IRR"
    with pytest.raises(InvalidEventEnvelopeError, match="currency"):
        CanonicalEvent.from_raw(raw_envelope(currency="RIAL"))


def test_company_mapping_must_be_an_object() -> None:
    with pytest.raises(InvalidEventEnvelopeError, match="company_mapping"):
        CanonicalEvent.from_raw(raw_envelope(company_mapping="acme"))


def test_received_at_defaults_to_now_and_is_overridable() -> None:
    before = datetime.datetime.now(datetime.UTC)
    event = CanonicalEvent.from_raw(raw_envelope())
    after = datetime.datetime.now(datetime.UTC)
    assert before <= event.received_at <= after
    fixed = datetime.datetime(2026, 11, 17, 12, 0, tzinfo=datetime.UTC)
    event = CanonicalEvent.from_raw(raw_envelope(), received_at=fixed)
    assert event.received_at == fixed


def test_hmac_verification_accepts_hex_and_base64() -> None:
    import base64

    secret = "webhook-secret"
    body = b'{"total_amount": 175000}'
    digest = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    assert verify_hmac_sha256(secret, body, digest.hex()) is True
    assert verify_hmac_sha256(secret, body, base64.b64encode(digest).decode()) is True
    assert verify_hmac_sha256(secret, body, "00" * 32) is False
    assert verify_hmac_sha256(secret, body, "not-a-signature") is False
    assert verify_hmac_sha256("wrong-secret", body, digest.hex()) is False
