"""Domain transfer shapes for the event platform (spec §10, US-501).

These are plain frozen dataclasses — no ORM, no SQLAlchemy, no HTTP.
The SQLAlchemy repository maps them onto the ``inbox_events`` /
``outbox_events`` / ``integration_attempts`` /
``automation_decision_log`` / ``connector_store_settings`` /
``external_id_map`` tables.

The transactional-outbox shapes (``OutboxEvent``,
``IntegrationAttempt`` and their statuses) live here because the
outbox is generic integration infrastructure: Moadian was its first
user in Phase 4, the event platform is its second, and later modules
(gateways, banks, cheques) are next. ``hesabyar.domain.moadian.ports``
re-exports them for its existing importers.
"""

from __future__ import annotations

import datetime
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum, unique
from typing import Any, Literal

from hesabyar.domain.accounting.refs import (
    AccountRef,
    CompanyRef,
    JournalRef,
    PartyRef,
    PaymentJournalRef,
    ProductRef,
)
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.domain.events.currency import ConvertedAmount
from hesabyar.domain.events.envelope import CanonicalEvent, CanonicalEventType


@unique
class InboxEventStatus(StrEnum):
    """Processing status of an inbox event (spec §10.3)."""

    RECEIVED = "received"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"
    DEAD_LETTERED = "dead_lettered"


@unique
class OutboxEventStatus(StrEnum):
    """Delivery status of a transactional-outbox event (spec §10.3)."""

    PENDING = "pending"
    PUBLISHED = "published"
    FAILED = "failed"


@unique
class IntegrationAttemptStatus(StrEnum):
    """Outcome of one attempt to process/publish an event."""

    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class OutboxEvent:
    """One transactional-outbox event (spec §10.3).

    Written in the same transaction as the business change it
    announces; a worker publishes pending rows and marks them
    published. ``payload_hash`` is the SHA-256 hex of the canonical
    payload (tamper evidence).
    """

    id: str
    tenant_id: str
    aggregate_type: str
    aggregate_id: str
    event_type: str
    payload: Mapping[str, Any]
    payload_hash: str | None
    status: OutboxEventStatus
    attempts: int
    last_attempt_at: datetime.datetime | None
    last_error: str | None
    published_at: datetime.datetime | None
    correlation_id: str | None
    created_at: datetime.datetime
    updated_at: datetime.datetime


@dataclass(frozen=True)
class IntegrationAttempt:
    """One attempt to process an inbox event or publish an outbox event (spec §10.3)."""

    id: str
    outbox_event_id: str | None
    attempt_number: int
    status: IntegrationAttemptStatus
    started_at: datetime.datetime
    finished_at: datetime.datetime | None
    error_code: str | None
    error_message: str | None
    created_at: datetime.datetime
    inbox_event_id: str | None = None


@dataclass(frozen=True)
class InboxEvent:
    """One persisted raw event awaiting (or having completed) processing.

    Mirrors the ``inbox_events`` row. The raw envelope is stored in
    the same transaction as the inbox status (§10.3 step 2), before
    any mapping is resolved — so ``tenant_id`` may be None until the
    tenant/company mapping resolves.
    """

    id: str
    tenant_id: str | None
    schema_version: int
    source_system: str
    source_store_id: str
    source_event_id: str
    event_type: str
    occurred_at: datetime.datetime
    received_at: datetime.datetime
    company_mapping: Mapping[str, Any] | None
    currency: str | None
    external_entity_type: str | None
    external_entity_id: str | None
    correlation_id: str | None
    idempotency_key: str | None
    payload: Mapping[str, Any]
    payload_hash: str
    status: InboxEventStatus
    attempts: int
    last_attempt_at: datetime.datetime | None
    next_attempt_at: datetime.datetime | None
    last_error: str | None
    processed_at: datetime.datetime | None
    dead_lettered_at: datetime.datetime | None
    created_at: datetime.datetime
    updated_at: datetime.datetime
    # Logical installation that ingested the event (migration 0012,
    # US-511): the pipeline resolves installation-scoped mappings
    # (mapping center, store registry) against it. None for events
    # ingested before the column existed — those fail closed to
    # review when a mapping is needed.
    installation_id: str | None = None

    def to_canonical_event(self) -> CanonicalEvent:
        """Rebuild the validated envelope from the stored fields."""
        return CanonicalEvent.from_raw(
            {
                "id": self.id,
                "schema_version": self.schema_version,
                "source_system": self.source_system,
                "source_store_id": self.source_store_id,
                "source_event_id": self.source_event_id,
                "occurred_at": self.occurred_at,
                "received_at": self.received_at,
                "company_mapping": self.company_mapping,
                "currency": self.currency,
                "external_entity_type": self.external_entity_type,
                "external_entity_id": self.external_entity_id,
                "correlation_id": self.correlation_id,
                "idempotency_key": self.idempotency_key,
                "payload": self.payload,
                "payload_hash": self.payload_hash,
            }
        )


#: Outcome of a transactional inbox store (§10.4): ``created`` for a
#: new event, ``duplicate`` for a re-sent event with the same hash,
#: ``conflict`` for a re-used key with a different hash.
InboxStoreStatus = Literal["created", "duplicate", "conflict"]


@dataclass(frozen=True)
class InboxStoreOutcome:
    """Result of storing an event in the inbox under the §10.4 constraints."""

    status: InboxStoreStatus
    event: InboxEvent | None
    existing_event_id: str | None = None


@unique
class AutomationDecisionKind(StrEnum):
    """What the pipeline (or a human replay) decided for an event."""

    DRAFT = "draft"
    AUTO_POST = "auto_post"
    NEEDS_REVIEW = "needs_review"
    DEAD_LETTER = "dead_letter"
    REPLAYED = "replayed"


@dataclass(frozen=True)
class GateResult:
    """One policy-gate verdict, recorded for the audit log (§6.7)."""

    name: str
    passed: bool
    reason: str


@dataclass(frozen=True)
class AutomationDecision:
    """One automation-decision audit-log entry (§6.7, US-501).

    Every auto-post/draft decision records the event, the policy
    version, the mapping version, the verdict of every gate, the
    final decision, the time and (for human actions) the actor.
    """

    id: str
    tenant_id: str | None
    inbox_event_id: str
    event_type: str
    decision: AutomationDecisionKind
    policy_version: str
    mapping_version: str | None
    gate_results: tuple[GateResult, ...]
    decided_by: str
    decided_at: datetime.datetime
    correlation_id: str | None
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StoreSettings:
    """Per-store connector settings (ADR 0003 decision 4).

    ``prices_in_toman`` decides whether the ×10 conversion applies at
    the connector boundary for this store. The default for an
    Iranian store is True; a store that prices in Rial sets False.
    """

    source_system: str
    source_store_id: str
    prices_in_toman: bool = True
    currency: str = "IRR"
    updated_at: datetime.datetime | None = None


@dataclass(frozen=True)
class AmountConversion:
    """One money field converted during normalization, with evidence."""

    field_path: str
    converted: ConvertedAmount


@dataclass(frozen=True)
class NormalizedEvent:
    """A canonical event after §10.3 step 3 (normalize + validate).

    ``event_type`` is the validated canonical type, carried
    alongside the envelope: it selects the money fields converted
    here and the policy gate's event-type layer downstream.

    ``converted_payload`` is the payload with every money field
    converted to whole Rial; ``conversions`` records each field's
    source amount and rounding evidence, so the conversion is
    auditable and never silent. The raw envelope is untouched on the
    inbox row — normalization never rewrites history.
    """

    event: CanonicalEvent
    event_type: CanonicalEventType
    store_settings: StoreSettings
    converted_payload: Mapping[str, Any]
    conversions: tuple[AmountConversion, ...] = ()
    # Logical installation that ingested the event (from the inbox
    # row; US-511): the mapping resolver scopes registry reads
    # against it. None when the inbox row predates migration 0012.
    installation_id: str | None = None

    @property
    def total_amount_rial(self) -> MoneyIRR | None:
        """Best-effort total for policy-gate amount caps.

        Reads the converted payload's ``total_amount`` (orders,
        invoices), ``gross_amount`` (payments) or ``refund_amount``
        (refunds) when present; None when the payload carries no
        total (the gate then fails closed on amount, per its own
        contract).
        """
        for key in ("total_amount", "gross_amount", "refund_amount", "amount"):
            value = self.converted_payload.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                return MoneyIRR(value)
        return None


@dataclass(frozen=True)
class ResolvedMapping:
    """Resolved versioned mappings for one event (§10.5).

    Produced by the mapping center (F3). ``unresolved`` names every
    mapping the event needed but could not resolve — a non-empty list
    fails closed to the review queue, never to a default account.
    """

    mapping_version: str
    company: CompanyRef | None = None
    accounts: Mapping[str, AccountRef] = field(default_factory=dict)
    journal: JournalRef | None = None
    party: PartyRef | None = None
    unresolved: tuple[str, ...] = ()
    # Store order lines: SKU → engine product (resolved through the
    # §10.4 external-id map; US-511). Keyed by the store SKU as sent.
    products: Mapping[str, ProductRef] = field(default_factory=dict)
    # Payment journal for gateway payments (``account.payment.journal``,
    # resolved from the gateway_journal mapping; US-511). Distinct
    # from ``journal`` (an ``account.journal`` for journal drafts).
    payment_journal: PaymentJournalRef | None = None

    @property
    def is_complete(self) -> bool:
        """True when every required mapping resolved (company + no gaps)."""
        return self.company is not None and not self.unresolved


@dataclass(frozen=True)
class PolicyDecision:
    """The three-layer policy gate's verdict (§6.2).

    ``allowed`` means auto-post is permitted for this exact event;
    otherwise the draft stays a draft for a human. The gate never
    approves silently: every layer's verdict is recorded.
    """

    allowed: bool
    policy_version: str
    gate_results: tuple[GateResult, ...]
    reason: str

    @property
    def decision(self) -> AutomationDecisionKind:
        """The audit-log decision this verdict implies."""
        return AutomationDecisionKind.AUTO_POST if self.allowed else AutomationDecisionKind.DRAFT


@unique
class PipelineOutcome(StrEnum):
    """Terminal outcome of one §10.3 pipeline run for an event."""

    PROCESSED_DRAFT = "processed_draft"
    PROCESSED_POSTED = "processed_posted"
    NEEDS_REVIEW = "needs_review"
    DEAD_LETTERED = "dead_lettered"
    RETRY_SCHEDULED = "retry_scheduled"


@dataclass(frozen=True)
class PipelineResult:
    """Outcome of processing one inbox event through the §10.3 pipeline."""

    inbox_event_id: str
    outcome: PipelineOutcome
    draft_ref: str | None = None
    posted_ref: str | None = None
    decision_id: str | None = None
    error: str | None = None
    next_attempt_at: datetime.datetime | None = None


@dataclass(frozen=True)
class ReviewItemDraft:
    """A review-queue item the pipeline wants to enqueue (F4 seam).

    The shape mirrors the uniform ``ReviewItem`` model (phased design
    §7.2): the F4 adapter persists it as a review item; the F1
    inbox-backed adapter records the routing in the decision audit
    log and leaves the inbox row (status ``needs_review``) as the
    item until F4 lands.
    """

    type: str
    severity: str
    source_event_ref: str
    reason: str
    suggested_action: str
    amount_irr: int | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExternalIdMapping:
    """A source entity → engine record mapping (§10.4, third constraint).

    The unique constraint on
    ``(source_system, external_entity_type, external_entity_id,
    target_engine)`` is what makes the observable effect exactly-once:
    a reprocessed event finds its engine record here instead of
    creating a second one.
    """

    source_system: str
    external_entity_type: str
    external_entity_id: str
    target_engine: str
    target_entity_type: str
    target_external_id: str
    engine_company_id: str | None = None
    tenant_id: str | None = None


@dataclass(frozen=True)
class IdempotencyRecord:
    """One (installation, operation, key) idempotency record (§10.4)."""

    installation_id: str
    operation: str
    idempotency_key: str
    request_hash: str
    response_status: int | None = None
    response_body: Mapping[str, Any] | None = None
    response_reference: str | None = None
    expires_at: datetime.datetime | None = None

    @property
    def is_completed(self) -> bool:
        """True when the record holds a stored response for replay."""
        return self.response_status is not None and self.response_body is not None


# ---------------------------------------------------------------------------
# CommerceConnectorPort (§10.1) transfer shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IncomingEvent:
    """A raw event as received from a connector, before verification.

    ``event_type`` is the canonical type as the sender declared it
    (the ``X-HesabYar-Event-Type`` header on the ingest request):
    it travels alongside the envelope, never inside it, and the
    receiver validates it against the nine canonical types when
    verifying (``parse_event_type``).
    """

    connector: str
    raw_body: bytes
    signature: str | None = None
    content_type: str | None = None
    event_type: str | None = None


@dataclass(frozen=True)
class VerifiedEvent:
    """An event that passed §10.3 step 1 (verification).

    ``event_type`` is the validated canonical type, carried
    alongside the 15-field envelope from here on. ``event_types``
    lists every canonical type one webhook maps to — usually just
    the primary type, two for a ``refund.created`` fan-out
    (``sales.refund.created`` + ``payment.refunded``); the ingest
    stores one inbox event per type (US-511).
    """

    event: CanonicalEvent
    event_type: CanonicalEventType
    signature_verified: bool
    verification_note: str
    event_types: tuple[CanonicalEventType, ...] = ()


@dataclass(frozen=True)
class ProcessingResult:
    """The outcome a connector is acknowledged with (§10.1)."""

    inbox_event_id: str
    outcome: PipelineOutcome
    error: str | None = None


#: Money fields (dot paths; ``[]`` iterates a list) converted from the
#: store's currency to whole Rial during normalization, per event
#: type. A field that is absent or null is skipped — ``null`` means
#: "unknown" (e.g. an unreported gateway fee), never zero. Fields
#: outside this table are left untouched: only money is converted.
MONEY_FIELDS_BY_EVENT_TYPE: Mapping[CanonicalEventType, tuple[str, ...]] = {
    CanonicalEventType.SALES_ORDER_COMPLETED: (
        "total_amount",
        "subtotal_amount",
        "discount_amount",
        "shipping_amount",
        "tax_amount",
        "lines[].unit_price",
        "lines[].line_total",
        "lines[].discount_amount",
        # Installment fields inside order.completed are carried
        # (US-511): converted like every other amount so the 5ج
        # phase reads whole Rial.
        "installment.down_payment_amount",
        "installment.installment_amount",
    ),
    CanonicalEventType.SALES_ORDER_CANCELLED: ("total_amount",),
    CanonicalEventType.SALES_REFUND_CREATED: (
        "refund_amount",
        "total_amount",
        "lines[].line_total",
    ),
    CanonicalEventType.PAYMENT_CAPTURED: (
        "gross_amount",
        "fee_amount",
        "fee_tax_amount",
        "net_amount",
    ),
    CanonicalEventType.PAYMENT_REFUNDED: (
        "gross_amount",
        "fee_amount",
        "net_amount",
    ),
    CanonicalEventType.PAYMENT_SETTLEMENT_RECEIVED: (
        "settlement_amount",
        "gross_amount",
        "fee_amount",
        "net_amount",
    ),
    CanonicalEventType.PURCHASE_INVOICE_RECEIVED: (
        "total_amount",
        "subtotal_amount",
        "discount_amount",
        "tax_amount",
        "lines[].unit_price",
        "lines[].line_total",
    ),
    CanonicalEventType.INVENTORY_COST_RECOGNIZED: (
        "cost_amount",
        "total_amount",
    ),
    CanonicalEventType.BANK_TRANSACTION_IMPORTED: ("amount",),
    # Cheque intake (US-533): NO generic conversion. The 24-field
    # payload carries its own explicit unit (``amount_toman`` +
    # ``amount_currency: "IRT"`` / ``"IRR"``), so the ×10 Toman→Rial
    # conversion happens in ``validate_registration_payload`` at the
    # connector boundary — converting ``amount_toman`` here as well
    # would double-convert it. The empty entries document the
    # deliberate no-op.
    CanonicalEventType.CHEQUE_RECEIVED_REGISTERED: (),
    CanonicalEventType.CHEQUE_RECEIVED_CANCELLED: (),
}
