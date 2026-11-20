"""Verify an incoming event (spec §10.3 step 1, shared by ingest paths).

The one verification implementation every receiver uses: the REST
ingest endpoint (via :class:`EventIngestService`) and the store
connectors (via :class:`CommerceConnectorPort.verify_event`). Keeping
it here — pure domain logic over the envelope — means the webhook
path and the polling path can never drift apart on what "verified"
means.

Steps: parse the raw JSON body → validate the 15-field envelope
(``CanonicalEvent.from_raw``) → validate the canonical event type
carried alongside it (the ``X-HesabYar-Event-Type`` header on ingest)
→ check the envelope's ``source_system`` matches the connector that
received it → look up the store's secret (by the parsed envelope's
``source_system``/``source_store_id``) and verify the HMAC-SHA256
signature when one is configured (hex or base64; timing-safe
compare). No secret → the check is skipped and the skip is recorded
on the result, never silently assumed.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from hesabyar.domain.events.envelope import (
    CanonicalEvent,
    parse_event_type,
    verify_hmac_sha256,
)
from hesabyar.domain.events.errors import EventSignatureError, InvalidEventEnvelopeError
from hesabyar.domain.events.models import IncomingEvent, VerifiedEvent


def verify_incoming_event(
    request: IncomingEvent,
    *,
    secret_lookup: Callable[[str, str], str | None],
) -> VerifiedEvent:
    """Verify one incoming event: envelope + type + (when configured) HMAC.

    ``secret_lookup`` resolves a store's webhook secret from its
    ``(source_system, source_store_id)`` — the ingest service passes
    its ``WebhookSecretPort`` lookup; a connector bound to one store
    passes a lookup returning its configured secret (or None). The
    lookup runs after the envelope is parsed, because the secret is
    per store.
    """
    try:
        raw = json.loads(request.raw_body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise InvalidEventEnvelopeError(
            "The event body is not valid JSON.",
            details={"connector": request.connector},
        ) from exc
    if not isinstance(raw, dict):
        raise InvalidEventEnvelopeError(
            "The event body must be a JSON object.",
            details={"connector": request.connector},
        )
    event = CanonicalEvent.from_raw(raw)
    event_type = parse_event_type(request.event_type)
    if event.source_system != request.connector:
        raise InvalidEventEnvelopeError(
            f"The envelope's source_system {event.source_system!r} does not "
            f"match the connector {request.connector!r}.",
            details={"source_system": event.source_system, "connector": request.connector},
        )
    secret = secret_lookup(event.source_system, event.source_store_id)
    if secret is None:
        return VerifiedEvent(
            event=event,
            event_type=event_type,
            signature_verified=False,
            verification_note="No webhook secret is configured for this store; signature check skipped.",
        )
    if request.signature is None:
        raise EventSignatureError(
            "This store requires a signed webhook (missing signature header).",
            details={"source_system": event.source_system},
        )
    if not verify_hmac_sha256(secret, request.raw_body, request.signature):
        raise EventSignatureError(
            "The webhook signature does not match the body.",
            details={"source_system": event.source_system},
        )
    return VerifiedEvent(
        event=event,
        event_type=event_type,
        signature_verified=True,
        verification_note="HMAC-SHA256 signature verified.",
    )
