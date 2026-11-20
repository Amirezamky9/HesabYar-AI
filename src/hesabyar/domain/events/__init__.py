"""Canonical event platform domain (spec §10, US-501).

Envelope, currency conversion, transfer shapes, ports and pipeline
mechanics for the one verified, exactly-once event pipeline every
store/gateway/bank event flows through.
"""

from hesabyar.domain.events.envelope import (
    CANONICAL_EVENT_TYPES,
    CURRENT_SCHEMA_VERSION,
    EVENT_INGEST_OPERATION,
    CanonicalEvent,
    CanonicalEventType,
    canonical_payload_hash,
    verify_hmac_sha256,
)

__all__ = [
    "CANONICAL_EVENT_TYPES",
    "CURRENT_SCHEMA_VERSION",
    "EVENT_INGEST_OPERATION",
    "CanonicalEvent",
    "CanonicalEventType",
    "canonical_payload_hash",
    "verify_hmac_sha256",
]
