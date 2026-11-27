"""The canonical event envelope, as a data contract (US-503, Phase 5 F3).

Spec §10.2 fixes the envelope at 15 fields; the phased design §2
(§1.2) fixes the same 15 fields for the F1 event platform. F1 (US-501)
owns the inbox/outbox implementation of this contract and is being
built in parallel — this module is F3's read of the SAME contract, so
the mapping center and the policy gate can be built and tested against
it now and integrated with F1's implementation when it lands.

The envelope carries no explicit ``event_type`` field: the canonical
event type (``sales.order.completed``, ``payment.captured``, … — the
nine §10.2 types plus ADR-approved additions) is the routing key the
webhook receiver assigns when it normalizes a store webhook into the
canonical form (phased design §1.2 table), and it travels alongside
the envelope into the pipeline. The policy gate therefore takes
``event_type`` as an explicit parameter next to the envelope — see
:mod:`hesabyar.domain.integrations.policy_gate`.

All amounts inside ``payload`` are whole rials (``MoneyIRR``,
digits=0); Toman→Rial conversion happens at the connector boundary,
before the amount enters the envelope (ADR 0003 decision 4).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any

__all__ = ["CanonicalEventEnvelope", "SCHEMA_VERSION_1"]

#: Schema version stamped on every new event (phased design §1.2 F1).
SCHEMA_VERSION_1 = 1


@dataclass(frozen=True)
class CanonicalEventEnvelope:
    """The 15-field canonical event envelope (spec §10.2).

    ``company_mapping`` names the target company (the store→company
    mapping resolves it); ``currency`` is always ``"IRR"`` inside
    HesabYar; ``payload`` is the event body (amounts in whole rials);
    ``payload_hash`` is the SHA-256 of the canonical payload bytes and
    is the basis of the §10.4 conflict rule (same idempotency key +
    different hash = 409 Conflict).
    """

    id: str
    schema_version: int
    source_system: str
    source_store_id: str
    source_event_id: str
    occurred_at: datetime.datetime
    received_at: datetime.datetime
    company_mapping: str
    currency: str
    external_entity_type: str
    external_entity_id: str
    correlation_id: str | None
    idempotency_key: str
    payload: dict[str, Any]
    payload_hash: str

    def is_current_schema(self) -> bool:
        """True when the envelope uses the current schema version."""
        return self.schema_version == SCHEMA_VERSION_1
