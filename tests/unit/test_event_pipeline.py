"""Unit tests for the event ingest + §10.3 pipeline + DLQ replay (US-501).

All services run against the in-memory fakes in ``tests/event_fixtures.py``
— no database, no server.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
from typing import Any

import pytest

from hesabyar.application.commands.events import (
    DlqReplayService,
    EventIngestService,
    EventPipelineService,
    StalenessService,
    StoreSettingsService,
    is_retryable,
    normalize_event,
)
from hesabyar.application.errors import ApplicationError, EngineUnavailableError
from hesabyar.domain.common.errors import HesabYarError
from hesabyar.domain.events.envelope import CanonicalEvent, CanonicalEventType
from hesabyar.domain.events.errors import (
    EventNotDeadLetteredError,
    EventNotFoundError,
    EventNotReprocessableError,
    IdempotencyConflictError,
    InvalidEventEnvelopeError,
    InvalidStoreAmountError,
    MappingUnresolvedError,
    UnknownEventTypeError,
)
from hesabyar.domain.events.models import (
    AutomationDecisionKind,
    InboxEventStatus,
    IncomingEvent,
    PipelineOutcome,
    PipelineResult,
    StoreSettings,
)
from hesabyar.domain.events.pipeline import (
    MAX_ATTEMPTS,
    retry_delay_seconds,
)
from hesabyar.infrastructure.events.defaults import (
    DraftOnlyPolicyGate,
    FailClosedDraftBuilder,
    FailClosedMappingResolver,
)
from hesabyar.infrastructure.events.in_memory_queue import InMemoryJobQueue
from tests.event_fixtures import (
    INSTALLATION_ID,
    TENANT_ID,
    FakeAuditLog,
    FakeDraftBuilder,
    FakeEngine,
    FakeExternalIdMap,
    FakeIdempotencyStore,
    FakeInboxRepository,
    FakeMappingResolver,
    FakeOutboxRepository,
    FakePolicyGate,
    FakeReviewQueue,
    FakeStoreSettings,
    FakeWebhookSecrets,
    ingest_into,
    make_inbox_event,
    must_get,
    raw_envelope,
)


def _incoming(
    payload: dict[str, Any] | None = None,
    event_type: str | None = "sales.order.completed",
    **overrides: Any,
) -> IncomingEvent:
    body = json.dumps(raw_envelope(payload=payload, **overrides)).encode()
    return IncomingEvent(connector="hayat", raw_body=body, event_type=event_type)


def _signed(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _pipeline(
    *,
    gate_allowed: bool = False,
    engine: FakeEngine | None = None,
    inbox: FakeInboxRepository | None = None,
    outbox: FakeOutboxRepository | None = None,
    external_ids: FakeExternalIdMap | None = None,
    audit_log: FakeAuditLog | None = None,
    settings: FakeStoreSettings | None = None,
    mapping_resolver: FakeMappingResolver | None = None,
    draft_builder: FakeDraftBuilder | None = None,
    review_queue: FakeReviewQueue | None = None,
) -> tuple[
    EventPipelineService,
    FakeEngine,
    FakeInboxRepository,
    FakeOutboxRepository,
    FakeExternalIdMap,
    FakeAuditLog,
]:
    engine = engine or FakeEngine()
    inbox = inbox or FakeInboxRepository()
    outbox = outbox or FakeOutboxRepository()
    external_ids = external_ids or FakeExternalIdMap()
    audit_log = audit_log or FakeAuditLog()
    settings = settings or FakeStoreSettings()
    review_queue = review_queue or FakeReviewQueue()
    service = EventPipelineService(
        inbox=inbox,
        outbox=outbox,
        external_ids=external_ids,
        audit_log=audit_log,
        store_settings=settings,
        mapping_resolver=mapping_resolver or FakeMappingResolver(),
        draft_builder=draft_builder or FakeDraftBuilder(),
        policy_gate=FakePolicyGate(allowed=gate_allowed),
        review_queue=review_queue,
        engine=engine,
    )
    return service, engine, inbox, outbox, external_ids, audit_log


def _ingest_event(inbox: FakeInboxRepository, **overrides: Any) -> str:
    """Store an event through the fake inbox and return its id."""
    return ingest_into(inbox, **overrides)


# ---------------------------------------------------------------------------
# Ingest (§10.3 steps 1–2)
# ---------------------------------------------------------------------------


def test_ingest_stores_a_valid_event() -> None:
    inbox = FakeInboxRepository()
    service = EventIngestService(inbox, FakeIdempotencyStore(), FakeWebhookSecrets(), InMemoryJobQueue())
    result = service.ingest(_incoming(), tenant_id=TENANT_ID, installation_id=INSTALLATION_ID)
    assert result.status == "created"
    assert result.event.status == InboxEventStatus.RECEIVED
    assert result.event.source_event_id == "evt-1"
    assert result.event.event_type == "sales.order.completed"
    assert result.signature_verified is False
    assert "No webhook secret" in result.signature_note


def test_ingest_without_event_type_is_rejected() -> None:
    """The alongside type is required — a request without it is a 422."""
    service = EventIngestService(
        FakeInboxRepository(), FakeIdempotencyStore(), FakeWebhookSecrets(), InMemoryJobQueue()
    )
    with pytest.raises(InvalidEventEnvelopeError, match="event_type"):
        service.ingest(_incoming(event_type=None), tenant_id=TENANT_ID, installation_id=INSTALLATION_ID)


def test_ingest_with_unknown_event_type_is_rejected() -> None:
    """New event names need ADR approval — they are rejected, never accepted."""
    service = EventIngestService(
        FakeInboxRepository(), FakeIdempotencyStore(), FakeWebhookSecrets(), InMemoryJobQueue()
    )
    with pytest.raises(UnknownEventTypeError, match="payment.bank_transfer.recorded"):
        service.ingest(
            _incoming(event_type="payment.bank_transfer.recorded"),
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
        )


def test_ingest_with_valid_hmac_signature_verifies() -> None:
    secrets = FakeWebhookSecrets({("hayat", "store-1"): "s3cr3t"})
    inbox = FakeInboxRepository()
    service = EventIngestService(inbox, FakeIdempotencyStore(), secrets, InMemoryJobQueue())
    body = json.dumps(raw_envelope()).encode()
    request = IncomingEvent(
        connector="hayat",
        raw_body=body,
        signature=_signed(body, "s3cr3t"),
        event_type="sales.order.completed",
    )
    result = service.ingest(request, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID)
    assert result.signature_verified is True


def test_ingest_with_missing_signature_is_rejected() -> None:
    secrets = FakeWebhookSecrets({("hayat", "store-1"): "s3cr3t"})
    service = EventIngestService(
        FakeInboxRepository(), FakeIdempotencyStore(), secrets, InMemoryJobQueue()
    )
    with pytest.raises(HesabYarError, match="missing signature"):
        service.ingest(_incoming(), tenant_id=TENANT_ID, installation_id=INSTALLATION_ID)


def test_ingest_with_wrong_signature_is_rejected() -> None:
    secrets = FakeWebhookSecrets({("hayat", "store-1"): "s3cr3t"})
    service = EventIngestService(
        FakeInboxRepository(), FakeIdempotencyStore(), secrets, InMemoryJobQueue()
    )
    body = json.dumps(raw_envelope()).encode()
    request = IncomingEvent(
        connector="hayat",
        raw_body=body,
        signature=_signed(body, "wrong"),
        event_type="sales.order.completed",
    )
    with pytest.raises(HesabYarError, match="does not match"):
        service.ingest(request, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID)


def test_duplicate_resend_replays_the_stored_event() -> None:
    """Same key + same hash → 200 replay, one inbox row, no duplicate."""
    inbox = FakeInboxRepository()
    service = EventIngestService(inbox, FakeIdempotencyStore(), FakeWebhookSecrets(), InMemoryJobQueue())
    first = service.ingest(_incoming(), tenant_id=TENANT_ID, installation_id=INSTALLATION_ID)
    second = service.ingest(_incoming(), tenant_id=TENANT_ID, installation_id=INSTALLATION_ID)
    assert second.status == "duplicate"
    assert second.event.id == first.event.id
    assert inbox.count_events(TENANT_ID) == 1


def test_same_key_different_payload_is_a_409_conflict() -> None:
    """§10.4: a re-used idempotency key with a different hash conflicts."""
    service = EventIngestService(
        FakeInboxRepository(), FakeIdempotencyStore(), FakeWebhookSecrets(), InMemoryJobQueue()
    )
    service.ingest(_incoming(payload={"total_amount": 175000}), tenant_id=TENANT_ID, installation_id=INSTALLATION_ID)
    with pytest.raises(IdempotencyConflictError, match="different payload"):
        service.ingest(
            _incoming(payload={"total_amount": 999000}), tenant_id=TENANT_ID, installation_id=INSTALLATION_ID
        )


def test_connector_mismatch_is_rejected() -> None:
    service = EventIngestService(
        FakeInboxRepository(), FakeIdempotencyStore(), FakeWebhookSecrets(), InMemoryJobQueue()
    )
    body = json.dumps(raw_envelope(source_system="hayat")).encode()
    request = IncomingEvent(connector="zarinpal", raw_body=body, event_type="sales.order.completed")
    with pytest.raises(InvalidEventEnvelopeError, match="does not match the connector"):
        service.ingest(request, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID)


def test_invalid_json_body_is_rejected() -> None:
    service = EventIngestService(
        FakeInboxRepository(), FakeIdempotencyStore(), FakeWebhookSecrets(), InMemoryJobQueue()
    )
    with pytest.raises(InvalidEventEnvelopeError, match="not valid JSON"):
        service.ingest(
            IncomingEvent(connector="hayat", raw_body=b"{not json", event_type="sales.order.completed"),
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
        )


# ---------------------------------------------------------------------------
# Normalization (§10.3 step 3)
# ---------------------------------------------------------------------------


def test_normalize_converts_toman_amounts_to_rial() -> None:
    """A Toman-priced store's 175 Toman order becomes 1,750 Rial inside."""
    event = CanonicalEvent.from_raw(
        raw_envelope(
            payload={
                "total_amount": 175,
                "shipping_amount": 25,
                "lines": [{"unit_price": 100, "line_total": 175}],
            }
        )
    )
    settings = StoreSettings(source_system="hayat", source_store_id="store-1", prices_in_toman=True)
    normalized = normalize_event(event, settings, event_type=CanonicalEventType.SALES_ORDER_COMPLETED)
    assert normalized.converted_payload["total_amount"] == 1750
    assert normalized.converted_payload["shipping_amount"] == 250
    assert normalized.converted_payload["lines"][0]["unit_price"] == 1000
    assert normalized.converted_payload["lines"][0]["line_total"] == 1750
    assert len(normalized.conversions) == 4
    assert normalized.total_amount_rial is not None and normalized.total_amount_rial.amount == 1750
    # The raw envelope is untouched — normalization never rewrites history.
    assert event.payload["total_amount"] == 175


def test_normalize_passes_rial_amounts_through() -> None:
    event = CanonicalEvent.from_raw(raw_envelope(payload={"total_amount": 1750}))
    settings = StoreSettings(source_system="hayat", source_store_id="store-1", prices_in_toman=False)
    normalized = normalize_event(event, settings, event_type=CanonicalEventType.SALES_ORDER_COMPLETED)
    assert normalized.converted_payload["total_amount"] == 1750


def test_normalize_skips_null_money_fields() -> None:
    """null means 'unknown' (e.g. an unreported gateway fee), never zero."""
    event = CanonicalEvent.from_raw(
        raw_envelope(payload={"gross_amount": 500, "fee_amount": None})
    )
    settings = StoreSettings(source_system="hayat", source_store_id="store-1", prices_in_toman=True)
    normalized = normalize_event(event, settings, event_type=CanonicalEventType.PAYMENT_CAPTURED)
    assert normalized.converted_payload["gross_amount"] == 5000
    assert normalized.converted_payload["fee_amount"] is None
    assert [c.field_path for c in normalized.conversions] == ["gross_amount"]
    assert normalized.event_type == CanonicalEventType.PAYMENT_CAPTURED


def test_normalize_rejects_unconvertible_amounts() -> None:
    event = CanonicalEvent.from_raw(raw_envelope(payload={"total_amount": 175.5}))
    settings = StoreSettings(source_system="hayat", source_store_id="store-1", prices_in_toman=False)
    with pytest.raises(InvalidStoreAmountError):
        normalize_event(event, settings, event_type=CanonicalEventType.SALES_ORDER_COMPLETED)


# ---------------------------------------------------------------------------
# Pipeline (§10.3 steps 3–9)
# ---------------------------------------------------------------------------


def test_pipeline_posts_when_the_gate_allows() -> None:
    service, engine, inbox, outbox, external_ids, audit_log = _pipeline(gate_allowed=True)
    event_id = _ingest_event(inbox, payload={"total_amount": 175})
    result = service.process(event_id)
    assert result.outcome == PipelineOutcome.PROCESSED_POSTED
    assert result.draft_ref == "1"
    assert result.posted_ref == "1"
    assert engine.create_calls == 1
    assert engine.post_calls == 1
    assert must_get(inbox, event_id).status == InboxEventStatus.PROCESSED
    # External-entity mapping saved (exactly-once anchor).
    mapping = external_ids.get("hayat", "order", "order-1")
    assert mapping is not None and mapping.target_external_id == "1"
    # Outbox result event emitted.
    assert len(outbox.list_events(TENANT_ID)) == 1
    assert outbox.list_events(TENANT_ID)[0].event_type == "events.processed"
    # Decision audit log: auto_post with policy + mapping versions.
    assert len(audit_log.decisions) == 1
    decision = audit_log.decisions[0]
    assert decision.decision == AutomationDecisionKind.AUTO_POST
    assert decision.policy_version == "policy-v2"
    assert decision.mapping_version == "mapping-v3"
    assert len(decision.gate_results) == 3


def test_pipeline_stays_draft_when_the_gate_denies() -> None:
    service, engine, inbox, _outbox, _external_ids, audit_log = _pipeline(gate_allowed=False)
    event_id = _ingest_event(inbox)
    result = service.process(event_id)
    assert result.outcome == PipelineOutcome.PROCESSED_DRAFT
    assert result.draft_ref == "1"
    assert result.posted_ref is None
    assert engine.create_calls == 1
    assert engine.post_calls == 0
    assert audit_log.decisions[0].decision == AutomationDecisionKind.DRAFT


def test_draft_amount_uses_the_converted_rial_total() -> None:
    """The draft is built from the converted payload, not the raw Toman."""
    engine = FakeEngine()
    service, _, inbox, _, _, _ = _pipeline(gate_allowed=False, engine=engine)
    event_id = _ingest_event(inbox, payload={"total_amount": 175})
    service.process(event_id)
    draft = engine.drafts[1]
    assert draft.total_debit.amount == 1750


def test_pipeline_fails_closed_on_unresolved_mapping() -> None:
    """An unknown mapping never falls back to a default account (§10.5)."""
    review_queue = FakeReviewQueue()
    resolver = FakeMappingResolver(error=MappingUnresolvedError("No mapping for this store."))
    service, engine, inbox, _, _, audit_log = _pipeline(mapping_resolver=resolver, review_queue=review_queue)
    event_id = _ingest_event(inbox)
    result = service.process(event_id)
    assert result.outcome == PipelineOutcome.NEEDS_REVIEW
    assert engine.create_calls == 0
    assert must_get(inbox, event_id).status == InboxEventStatus.NEEDS_REVIEW
    assert len(review_queue.items) == 1
    assert review_queue.items[0].type == "mapping"
    assert audit_log.decisions[0].decision == AutomationDecisionKind.NEEDS_REVIEW
    assert audit_log.decisions[0].policy_version == "n/a"


def test_fail_closed_defaults_route_every_event_to_review() -> None:
    """With F1 alone (no F3 mappings), every event needs review — never posts."""
    engine = FakeEngine()
    inbox = FakeInboxRepository()
    outbox = FakeOutboxRepository()
    review_queue = FakeReviewQueue()
    service = EventPipelineService(
        inbox=inbox,
        outbox=outbox,
        external_ids=FakeExternalIdMap(),
        audit_log=FakeAuditLog(),
        store_settings=FakeStoreSettings(),
        mapping_resolver=FailClosedMappingResolver(),
        draft_builder=FailClosedDraftBuilder(),
        policy_gate=DraftOnlyPolicyGate(),
        review_queue=review_queue,
        engine=engine,
    )
    event_id = _ingest_event(inbox)
    result = service.process(event_id)
    assert result.outcome == PipelineOutcome.NEEDS_REVIEW
    assert engine.create_calls == 0


def test_invalid_draft_validation_routes_to_review() -> None:
    engine = FakeEngine()
    engine.invalid_draft_ids.add(1)
    service, _, inbox, _, _, _ = _pipeline(engine=engine)
    event_id = _ingest_event(inbox)
    result = service.process(event_id)
    assert result.outcome == PipelineOutcome.NEEDS_REVIEW
    assert must_get(inbox, event_id).status == InboxEventStatus.NEEDS_REVIEW


def test_transient_engine_failure_schedules_a_retry() -> None:
    engine = FakeEngine()
    engine.fail_next = EngineUnavailableError("The engine is unreachable.")
    service, _, inbox, _, _, _ = _pipeline(engine=engine)
    event_id = _ingest_event(inbox)
    result = service.process(event_id)
    assert result.outcome == PipelineOutcome.RETRY_SCHEDULED
    stored = must_get(inbox, event_id)
    assert stored.status == InboxEventStatus.RECEIVED
    assert stored.last_error is not None and "unreachable" in stored.last_error
    assert result.next_attempt_at is not None
    delay = (result.next_attempt_at - datetime.datetime.now(datetime.UTC)).total_seconds()
    assert 30 <= delay <= 60  # first retry: 60s backoff


def test_retry_exhaustion_dead_letters_the_event() -> None:
    """After MAX_ATTEMPTS transient failures the event goes to the DLQ."""
    engine = FakeEngine()
    service, _, inbox, _, _, audit_log = _pipeline(engine=engine)
    event_id = _ingest_event(inbox)
    result: PipelineResult | None = None
    for attempt in range(MAX_ATTEMPTS):
        # Simulate the worker's claim: attempts increments before process.
        stored = must_get(inbox, event_id)
        inbox.save(make_inbox_event(
            stored.to_canonical_event(),
            event_type=stored.event_type,
            tenant_id=TENANT_ID,
            status=InboxEventStatus.PROCESSING,
            attempts=stored.attempts + 1,
            next_attempt_at=None,
        ))
        engine.fail_next = EngineUnavailableError("The engine is unreachable.")
        result = service.process(event_id)
        if attempt < MAX_ATTEMPTS - 1:
            assert result.outcome == PipelineOutcome.RETRY_SCHEDULED
    assert result is not None
    assert result.outcome == PipelineOutcome.DEAD_LETTERED
    stored = must_get(inbox, event_id)
    assert stored.status == InboxEventStatus.DEAD_LETTERED
    assert stored.dead_lettered_at is not None
    assert audit_log.decisions[-1].decision == AutomationDecisionKind.DEAD_LETTER


def test_retry_backoff_doubles_and_caps() -> None:
    assert retry_delay_seconds(1) == 60
    assert retry_delay_seconds(2) == 120
    assert retry_delay_seconds(3) == 240
    assert retry_delay_seconds(6) == 1920
    assert retry_delay_seconds(7) == 3600
    assert retry_delay_seconds(100) == 3600


def test_exactly_once_existing_engine_record_creates_no_draft() -> None:
    """A reprocessed event whose entity already maps is marked processed."""
    from hesabyar.domain.events.models import ExternalIdMapping

    external_ids = FakeExternalIdMap()
    external_ids.save(
        ExternalIdMapping(
            source_system="hayat",
            external_entity_type="order",
            external_entity_id="order-1",
            target_engine="tryton",
            target_entity_type="account.move",
            target_external_id="42",
            engine_company_id=None,
            tenant_id=TENANT_ID,
        )
    )
    service, engine, inbox, _, _, audit_log = _pipeline(gate_allowed=True, external_ids=external_ids)
    event_id = _ingest_event(inbox)
    result = service.process(event_id)
    assert result.outcome == PipelineOutcome.PROCESSED_DRAFT
    assert result.draft_ref == "42"
    assert engine.create_calls == 0
    assert engine.post_calls == 0
    assert audit_log.decisions[0].details["duplicate_suppressed"] is True


def test_reprocessing_a_processed_event_is_a_no_op() -> None:
    service, engine, inbox, _, _, _ = _pipeline(gate_allowed=False)
    event_id = _ingest_event(inbox)
    first = service.process(event_id)
    assert first.outcome == PipelineOutcome.PROCESSED_DRAFT
    second = service.process(event_id)
    assert second.outcome == PipelineOutcome.PROCESSED_DRAFT
    assert second.error is not None and "already processed" in second.error
    assert engine.create_calls == 1


def test_is_retryable_classification() -> None:
    assert is_retryable(MappingUnresolvedError("x")) is False
    assert is_retryable(InvalidStoreAmountError("x")) is False
    assert is_retryable(InvalidEventEnvelopeError("x")) is False
    assert is_retryable(EngineUnavailableError("x")) is True
    assert is_retryable(ApplicationError("x", code="APP", retryable=False)) is False
    assert is_retryable(HesabYarError("x", code="DOM")) is False
    assert is_retryable(ValueError("unexpected")) is True  # unknown → transient, DLQ backstop


# ---------------------------------------------------------------------------
# DLQ replay
# ---------------------------------------------------------------------------


def _dead_lettered(inbox: FakeInboxRepository, **overrides: Any) -> str:
    event_id = _ingest_event(inbox, **overrides)
    stored = must_get(inbox, event_id)
    inbox.save(make_inbox_event(
        stored.to_canonical_event(),
        event_type=stored.event_type,
        tenant_id=TENANT_ID,
        status=InboxEventStatus.DEAD_LETTERED,
        attempts=MAX_ATTEMPTS,
        last_error="Engine unreachable.",
    ))
    return event_id


def test_dlq_replay_resets_the_event_and_requeues_it() -> None:
    inbox = FakeInboxRepository()
    audit_log = FakeAuditLog()
    queue = InMemoryJobQueue()
    service = DlqReplayService(inbox, audit_log, queue)
    event_id = _dead_lettered(inbox)
    replayed = service.replay(event_id, actor="user-1", tenant_id=TENANT_ID)
    assert replayed.status == InboxEventStatus.RECEIVED
    assert replayed.attempts == 0
    assert replayed.last_error is None
    assert replayed.dead_lettered_at is None
    assert queue.deferred_events == [event_id]
    assert len(audit_log.decisions) == 1
    decision = audit_log.decisions[0]
    assert decision.decision == AutomationDecisionKind.REPLAYED
    assert decision.decided_by == "user-1"
    assert decision.details["previous_attempts"] == MAX_ATTEMPTS


def test_dlq_replay_keeps_the_idempotency_key() -> None:
    """The replayed event keeps its key — reprocessing cannot double-post."""
    inbox = FakeInboxRepository()
    audit_log = FakeAuditLog()
    service = DlqReplayService(inbox, audit_log, InMemoryJobQueue())
    event_id = _dead_lettered(inbox)
    before = must_get(inbox, event_id)
    replayed = service.replay(event_id, actor="user-1")
    assert replayed.idempotency_key == before.idempotency_key
    assert replayed.source_event_id == before.source_event_id


def test_dlq_replay_rejects_non_dead_lettered_events() -> None:
    inbox = FakeInboxRepository()
    service = DlqReplayService(inbox, FakeAuditLog(), InMemoryJobQueue())
    event_id = _ingest_event(inbox)
    with pytest.raises(EventNotDeadLetteredError, match="not dead-lettered"):
        service.replay(event_id, actor="user-1")


def test_dlq_replay_rejects_missing_events() -> None:
    service = DlqReplayService(FakeInboxRepository(), FakeAuditLog(), InMemoryJobQueue())
    with pytest.raises(EventNotFoundError):
        service.replay("missing", actor="user-1")


def test_dlq_replay_then_process_has_no_duplicate_effect() -> None:
    """Replay + reprocess of an already-recorded entity creates no draft."""
    from hesabyar.domain.events.models import ExternalIdMapping

    external_ids = FakeExternalIdMap()
    external_ids.save(
        ExternalIdMapping(
            source_system="hayat",
            external_entity_type="order",
            external_entity_id="order-1",
            target_engine="tryton",
            target_entity_type="account.move",
            target_external_id="7",
            engine_company_id=None,
            tenant_id=TENANT_ID,
        )
    )
    inbox = FakeInboxRepository()
    engine = FakeEngine()
    replay_service = DlqReplayService(inbox, FakeAuditLog(), InMemoryJobQueue())
    pipeline, _, _, _, _, _ = _pipeline(inbox=inbox, external_ids=external_ids, engine=engine)
    event_id = _dead_lettered(inbox)
    replay_service.replay(event_id, actor="user-1")
    result = pipeline.process(event_id)
    assert result.outcome == PipelineOutcome.PROCESSED_DRAFT
    assert result.draft_ref == "7"
    assert engine.create_calls == 0


# ---------------------------------------------------------------------------
# Reprocess (US-552: the review queue's path for a completed mapping)
# ---------------------------------------------------------------------------


def _needs_review(inbox: FakeInboxRepository, **overrides: Any) -> str:
    event_id = _ingest_event(inbox, **overrides)
    stored = must_get(inbox, event_id)
    inbox.save(make_inbox_event(
        stored.to_canonical_event(),
        event_type=stored.event_type,
        tenant_id=TENANT_ID,
        status=InboxEventStatus.NEEDS_REVIEW,
        attempts=1,
        last_error="Mapping unresolved.",
    ))
    return event_id


def test_reprocess_accepts_needs_review_events() -> None:
    """A mapping gap the reviewer just fixed is reprocessable."""
    inbox = FakeInboxRepository()
    audit_log = FakeAuditLog()
    queue = InMemoryJobQueue()
    service = DlqReplayService(inbox, audit_log, queue)
    event_id = _needs_review(inbox)
    reprocessed = service.reprocess(event_id, actor="operator", tenant_id=TENANT_ID)
    assert reprocessed.status == InboxEventStatus.RECEIVED
    assert reprocessed.attempts == 0
    assert reprocessed.last_error is None
    assert queue.deferred_events == [event_id]
    assert len(audit_log.decisions) == 1
    decision = audit_log.decisions[0]
    assert decision.decision == AutomationDecisionKind.REPLAYED
    assert decision.decided_by == "operator"
    assert decision.details["previous_status"] == "needs_review"
    assert decision.details["previous_attempts"] == 1


def test_reprocess_accepts_dead_lettered_events() -> None:
    """Reprocess shares the reset with replay — one mechanism, not two."""
    inbox = FakeInboxRepository()
    audit_log = FakeAuditLog()
    queue = InMemoryJobQueue()
    service = DlqReplayService(inbox, audit_log, queue)
    event_id = _dead_lettered(inbox)
    reprocessed = service.reprocess(event_id, actor="operator")
    assert reprocessed.status == InboxEventStatus.RECEIVED
    assert reprocessed.attempts == 0
    assert reprocessed.dead_lettered_at is None
    assert queue.deferred_events == [event_id]
    assert audit_log.decisions[0].details["previous_status"] == "dead_lettered"


def test_reprocess_keeps_the_idempotency_key() -> None:
    """The reprocessed event keeps its key — reprocessing cannot double-post."""
    inbox = FakeInboxRepository()
    service = DlqReplayService(inbox, FakeAuditLog(), InMemoryJobQueue())
    event_id = _needs_review(inbox)
    before = must_get(inbox, event_id)
    reprocessed = service.reprocess(event_id, actor="operator")
    assert reprocessed.idempotency_key == before.idempotency_key
    assert reprocessed.source_event_id == before.source_event_id


@pytest.mark.parametrize(
    "status",
    [
        InboxEventStatus.RECEIVED,
        InboxEventStatus.PROCESSING,
        InboxEventStatus.PROCESSED,
        InboxEventStatus.FAILED,
    ],
)
def test_reprocess_rejects_done_in_flight_or_failed_events(status: InboxEventStatus) -> None:
    inbox = FakeInboxRepository()
    service = DlqReplayService(inbox, FakeAuditLog(), InMemoryJobQueue())
    event_id = _ingest_event(inbox)
    stored = must_get(inbox, event_id)
    inbox.save(make_inbox_event(
        stored.to_canonical_event(),
        event_type=stored.event_type,
        tenant_id=TENANT_ID,
        status=status,
    ))
    with pytest.raises(EventNotReprocessableError, match="only dead-lettered or needs-review"):
        service.reprocess(event_id, actor="operator")


def test_reprocess_rejects_missing_events() -> None:
    service = DlqReplayService(FakeInboxRepository(), FakeAuditLog(), InMemoryJobQueue())
    with pytest.raises(EventNotFoundError):
        service.reprocess("missing", actor="operator")


# ---------------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------------


def test_staleness_lists_only_stuck_events() -> None:
    inbox = FakeInboxRepository()
    old = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=25)
    recent = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=1)
    stale_id = _ingest_event(inbox, source_event_id="old")
    inbox.save(make_inbox_event(
        must_get(inbox, stale_id).to_canonical_event(),
        event_type=must_get(inbox, stale_id).event_type,
        tenant_id=TENANT_ID,
        received_at=old,
    ))
    fresh_id = _ingest_event(inbox, source_event_id="fresh")
    inbox.save(make_inbox_event(
        must_get(inbox, fresh_id).to_canonical_event(),
        event_type=must_get(inbox, fresh_id).event_type,
        tenant_id=TENANT_ID,
        received_at=recent,
    ))
    done_id = _ingest_event(inbox, source_event_id="done")
    inbox.save(make_inbox_event(
        must_get(inbox, done_id).to_canonical_event(),
        event_type=must_get(inbox, done_id).event_type,
        tenant_id=TENANT_ID,
        status=InboxEventStatus.PROCESSED,
        received_at=old,
    ))
    service = StalenessService(inbox)
    stale = service.list_stale()
    assert [event.id for event in stale] == [stale_id]
    assert service.count_stale() == 1


# ---------------------------------------------------------------------------
# Store settings
# ---------------------------------------------------------------------------


def test_store_settings_default_to_toman_prices() -> None:
    """An Iranian store's prices are Toman unless configured otherwise."""
    service = StoreSettingsService(FakeStoreSettings())
    settings = service.get_settings("hayat", "store-1")
    assert settings.prices_in_toman is True
    assert settings.currency == "IRR"


def test_store_settings_update_round_trips() -> None:
    service = StoreSettingsService(FakeStoreSettings())
    updated = service.update_settings("hayat", "store-1", prices_in_toman=False)
    assert updated.prices_in_toman is False
    assert service.get_settings("hayat", "store-1").prices_in_toman is False


def test_store_settings_rejects_bad_currency() -> None:
    service = StoreSettingsService(FakeStoreSettings())
    with pytest.raises(InvalidEventEnvelopeError, match="3-letter"):
        service.update_settings("hayat", "store-1", prices_in_toman=True, currency="RIAL")
