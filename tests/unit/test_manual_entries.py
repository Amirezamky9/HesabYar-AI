"""Unit tests for dual-entry manual orders/invoices/refunds (US-513).

All services run against the in-memory fakes in
``tests/event_fixtures.py`` — no database, no server.

The tests prove the dual-entry contract:

- a manual entry builds the SAME 15-field canonical envelope a store
  webhook would produce (``source_system="manual"``, one of the nine
  canonical types, Toman money fields the pipeline converts);
- it is ingested through the SAME ``EventIngestService`` and travels
  the SAME ``EventPipelineService`` (normalize → map → draft →
  validate → gate → post-or-draft → audit log);
- every manual record traces to its entry (entry → inbox event →
  decision → engine record) and every auto-created record traces to
  its source event (the §10.4 external-id map).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from hesabyar.application.commands.events import EventIngestService, EventPipelineService
from hesabyar.application.commands.manual_entries import ManualEntryService
from hesabyar.application.queries.manual_entries import ManualEntryQueryService
from hesabyar.domain.events.envelope import CanonicalEventType
from hesabyar.domain.events.errors import (
    ManualEntryNotFoundError,
    ManualEntryValidationError,
)
from hesabyar.domain.events.manual_entries import (
    MANUAL_SOURCE_SYSTEM,
    CreateManualCancellation,
    CreateManualOrder,
    CreateManualRefund,
    ManualEntryType,
    ManualOrderLine,
    ManualRefundLine,
    build_cancellation_event,
    build_order_event,
    build_refund_events,
    new_entry_id,
)
from hesabyar.domain.events.models import (
    AutomationDecisionKind,
    InboxEventStatus,
    PipelineOutcome,
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
    FakeManualEntryRepository,
    FakeMappingResolver,
    FakeOutboxRepository,
    FakePolicyGate,
    FakeReviewQueue,
    FakeStoreSettings,
    FakeWebhookSecrets,
    must_get,
)

ACTOR_KEY_ID = "key-manual-tests"
ACTOR_NAME = "Manual Test Client"


def _order_command(**overrides) -> CreateManualOrder:
    values: dict = {
        "order_number": "ORD-1001",
        "lines": (
            ManualOrderLine(description="قهوه اسپرسو", quantity=Decimal(2), unit_price_toman=175_000),
            ManualOrderLine(description="کیک شکلاتی", quantity=Decimal(1), unit_price_toman=240_000),
        ),
        "discount_toman": 10_000,
        "shipping_toman": 50_000,
        "tax_toman": 0,
    }
    values.update(overrides)
    return CreateManualOrder(**values)


def _refund_command(**overrides) -> CreateManualRefund:
    values: dict = {
        "order_number": "ORD-1001",
        "refund_amount_toman": 175_000,
        "refund_reference": "REF-1",
    }
    values.update(overrides)
    return CreateManualRefund(**values)


def _cancellation_command(**overrides) -> CreateManualCancellation:
    values: dict = {
        "order_number": "ORD-1001",
        "total_amount_toman": 630_000,
        "reason": "مشتری منصرف شد",
    }
    values.update(overrides)
    return CreateManualCancellation(**values)


def _manual_service(
    inbox: FakeInboxRepository | None = None,
    entries: FakeManualEntryRepository | None = None,
) -> tuple[ManualEntryService, FakeInboxRepository, FakeManualEntryRepository]:
    inbox = inbox or FakeInboxRepository()
    entries = entries or FakeManualEntryRepository()
    ingest = EventIngestService(
        inbox, FakeIdempotencyStore(), FakeWebhookSecrets(), InMemoryJobQueue()
    )
    return ManualEntryService(ingest, entries), inbox, entries


def _pipeline(
    *,
    inbox: FakeInboxRepository,
    outbox: FakeOutboxRepository | None = None,
    external_ids: FakeExternalIdMap | None = None,
    audit_log: FakeAuditLog | None = None,
    engine: FakeEngine | None = None,
    fail_closed: bool = False,
    gate_allowed: bool = False,
) -> tuple[EventPipelineService, FakeEngine, FakeExternalIdMap, FakeAuditLog, FakeReviewQueue]:
    engine = engine or FakeEngine()
    external_ids = external_ids or FakeExternalIdMap()
    audit_log = audit_log or FakeAuditLog()
    review_queue = FakeReviewQueue()
    if fail_closed:
        mapping_resolver: FakeMappingResolver | FailClosedMappingResolver = FailClosedMappingResolver()
        draft_builder: FakeDraftBuilder | FailClosedDraftBuilder = FailClosedDraftBuilder()
        gate: FakePolicyGate | DraftOnlyPolicyGate = DraftOnlyPolicyGate()
    else:
        mapping_resolver = FakeMappingResolver()
        draft_builder = FakeDraftBuilder()
        gate = FakePolicyGate(gate_allowed)
    service = EventPipelineService(
        inbox=inbox,
        outbox=outbox or FakeOutboxRepository(),
        external_ids=external_ids,
        audit_log=audit_log,
        store_settings=FakeStoreSettings(),
        mapping_resolver=mapping_resolver,
        draft_builder=draft_builder,
        policy_gate=gate,
        review_queue=review_queue,
        engine=engine,
    )
    return service, engine, external_ids, audit_log, review_queue


def _create_order(service: ManualEntryService, command: CreateManualOrder | None = None):
    return service.create_order(
        command or _order_command(),
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        entered_by=ACTOR_KEY_ID,
        entered_by_name=ACTOR_NAME,
    )


# ---------------------------------------------------------------------------
# Envelope construction: the same envelope a webhook would produce
# ---------------------------------------------------------------------------


def test_order_event_is_a_valid_15_field_envelope() -> None:
    entry_id = new_entry_id()
    built = build_order_event(_order_command(), entry_id=entry_id)
    envelope = built.event.to_envelope_dict()
    assert set(envelope) == {
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
    assert built.event_type == CanonicalEventType.SALES_ORDER_COMPLETED
    assert built.event.source_system == MANUAL_SOURCE_SYSTEM
    assert built.event.source_store_id == "manual"
    assert built.event.source_event_id.startswith("manual-")
    assert built.event.currency == "IRR"
    assert built.event.schema_version == 1
    assert built.event.external_entity_type == "order"
    assert built.event.external_entity_id == "ORD-1001"
    assert built.event.correlation_id == entry_id
    assert built.event.idempotency_key == (
        f"event:manual:manual:{built.event.source_event_id}"
    )


def test_order_payload_carries_toman_money_fields_and_entry_markers() -> None:
    entry_id = new_entry_id()
    built = build_order_event(_order_command(), entry_id=entry_id)
    payload = built.event.payload
    assert payload["entry_source"] == "manual"
    assert payload["manual_entry_id"] == entry_id
    assert payload["order_number"] == "ORD-1001"
    # 2 × 175,000 + 240,000 = 590,000 subtotal; −10,000 + 50,000 = 630,000 total.
    assert payload["subtotal_amount"] == 590_000
    assert payload["discount_amount"] == 10_000
    assert payload["shipping_amount"] == 50_000
    assert payload["tax_amount"] == 0
    assert payload["total_amount"] == 630_000
    assert payload["lines"][0]["line_total"] == 350_000
    assert payload["lines"][0]["unit_price"] == 175_000
    assert payload["lines"][1]["line_total"] == 240_000


def test_refund_with_payment_produces_two_events_with_one_correlation_id() -> None:
    entry_id = new_entry_id()
    built = build_refund_events(_refund_command(), entry_id=entry_id)
    assert [event.event_type for event in built] == [
        CanonicalEventType.SALES_REFUND_CREATED,
        CanonicalEventType.PAYMENT_REFUNDED,
    ]
    first, second = built
    assert first.event.correlation_id == entry_id
    assert second.event.correlation_id == entry_id
    assert first.event.source_event_id != second.event.source_event_id
    assert first.event.external_entity_type == "refund"
    assert first.event.external_entity_id == "REF-1"
    assert second.event.external_entity_type == "payment_refund"
    assert second.event.external_entity_id == "REF-1"
    assert first.event.payload["refund_amount"] == 175_000
    assert first.event.payload["total_amount"] == 175_000
    assert second.event.payload["gross_amount"] == 175_000
    assert second.event.payload["net_amount"] == 175_000


def test_refund_without_payment_produces_only_the_credit_note_event() -> None:
    built = build_refund_events(_refund_command(refund_payment=False), entry_id=new_entry_id())
    assert [event.event_type for event in built] == [CanonicalEventType.SALES_REFUND_CREATED]


def test_refund_reference_is_generated_when_omitted() -> None:
    built = build_refund_events(_refund_command(refund_reference=None), entry_id=new_entry_id())
    assert built[0].event.external_entity_id is not None
    assert built[0].event.external_entity_id.startswith("manual-refund-")
    assert built[0].event.payload["refund_reference"] == built[0].event.external_entity_id


def test_cancellation_event_uses_a_distinct_entity_type() -> None:
    built = build_cancellation_event(_cancellation_command(), entry_id=new_entry_id())
    assert built.event_type == CanonicalEventType.SALES_ORDER_CANCELLED
    # Distinct from ("order", order_number): the pipeline's exactly-once
    # pre-check must not mistake a cancellation for a re-processed order.
    assert built.event.external_entity_type == "order_cancellation"
    assert built.event.external_entity_id == "ORD-1001"
    assert built.event.payload["total_amount"] == 630_000
    assert built.event.payload["reason"] == "مشتری منصرف شد"


# ---------------------------------------------------------------------------
# Validation: the operator's mistakes fail here, never in the pipeline
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "field"),
    [
        (_order_command(order_number="  "), "order_number"),
        (_order_command(lines=()), "lines"),
        (
            _order_command(
                lines=(ManualOrderLine(description="x", quantity=Decimal(0), unit_price_toman=100),)
            ),
            "lines[0].quantity",
        ),
        (
            _order_command(
                lines=(ManualOrderLine(description="x", quantity=Decimal(1), unit_price_toman=-5),)
            ),
            "lines[0].unit_price_toman",
        ),
        (
            _order_command(
                lines=(
                    ManualOrderLine(
                        description="x",
                        quantity=Decimal(1),
                        unit_price_toman=100,
                        discount_toman=101,
                    ),
                )
            ),
            "lines[0]",
        ),
        (
            _order_command(
                lines=(
                    ManualOrderLine(
                        description="x", quantity=Decimal("1.5"), unit_price_toman=101
                    ),
                )
            ),
            "lines[0]",
        ),
        (_order_command(discount_toman=600_000), "discount_toman"),
        (
            _order_command(
                lines=(ManualOrderLine(description="x", quantity=Decimal(1), unit_price_toman=0),),
                discount_toman=0,
                shipping_toman=0,
            ),
            "total_amount",
        ),
        (_refund_command(refund_amount_toman=0), "refund_amount_toman"),
        (
            _refund_command(
                lines=(ManualRefundLine(description="x", line_total_toman=100_000),),
            ),
            "lines",
        ),
        (_cancellation_command(order_number=""), "order_number"),
        (_cancellation_command(total_amount_toman=-1), "total_amount_toman"),
    ],
)
def test_invalid_manual_entries_fail_validation(command, field: str) -> None:
    with pytest.raises(ManualEntryValidationError) as excinfo:
        command.validate()
    assert excinfo.value.details is not None
    assert excinfo.value.details.get("field") == field


def test_fractional_quantity_is_allowed_when_the_line_total_is_whole() -> None:
    command = _order_command(
        lines=(ManualOrderLine(description="زعفران", quantity=Decimal("0.5"), unit_price_toman=200_000),)
    )
    command.validate()
    built = build_order_event(command, entry_id=new_entry_id())
    assert built.event.payload["lines"][0]["quantity"] == "0.5"
    assert built.event.payload["lines"][0]["line_total"] == 100_000


# ---------------------------------------------------------------------------
# Service: entry + inbox rows in one transaction, traceable both ways
# ---------------------------------------------------------------------------


def test_create_order_stores_entry_and_inbox_event() -> None:
    service, inbox, entries = _manual_service()
    entry = _create_order(service)

    assert entry.entry_type == ManualEntryType.ORDER
    assert entry.order_number == "ORD-1001"
    assert entry.total_amount_toman == 630_000
    assert entry.entered_by == ACTOR_KEY_ID
    assert entry.entered_by_name == ACTOR_NAME
    assert entry.tenant_id == TENANT_ID
    assert len(entry.events) == 1

    stored = entries.get(entry.id, TENANT_ID)
    assert stored is not None
    assert stored.id == entry.id

    ref = entry.events[0]
    assert ref.event_type == CanonicalEventType.SALES_ORDER_COMPLETED
    inbox_event = must_get(inbox, ref.inbox_event_id)
    assert inbox_event.source_system == MANUAL_SOURCE_SYSTEM
    assert inbox_event.source_event_id == ref.source_event_id
    assert inbox_event.event_type == "sales.order.completed"
    assert inbox_event.status == InboxEventStatus.RECEIVED
    assert inbox_event.correlation_id == entry.id
    assert inbox_event.payload["manual_entry_id"] == entry.id
    assert entry.payload == dict(inbox_event.payload)


def test_create_refund_stores_two_events_and_one_entry() -> None:
    service, inbox, _entries = _manual_service()
    entry = service.create_refund(
        _refund_command(),
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        entered_by=ACTOR_KEY_ID,
        entered_by_name=ACTOR_NAME,
    )
    assert entry.entry_type == ManualEntryType.REFUND
    assert entry.total_amount_toman == 175_000
    assert len(entry.events) == 2
    assert {ref.event_type for ref in entry.events} == {
        CanonicalEventType.SALES_REFUND_CREATED,
        CanonicalEventType.PAYMENT_REFUNDED,
    }
    for ref in entry.events:
        assert must_get(inbox, ref.inbox_event_id).correlation_id == entry.id


def test_create_cancellation_stores_one_event() -> None:
    service, inbox, _entries = _manual_service()
    entry = service.create_cancellation(
        _cancellation_command(),
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        entered_by=ACTOR_KEY_ID,
        entered_by_name=ACTOR_NAME,
    )
    assert entry.entry_type == ManualEntryType.CANCELLATION
    assert entry.total_amount_toman == 630_000
    assert len(entry.events) == 1
    assert entry.events[0].event_type == CanonicalEventType.SALES_ORDER_CANCELLED
    assert must_get(inbox, entry.events[0].inbox_event_id).status == InboxEventStatus.RECEIVED


def test_validation_failure_stores_nothing() -> None:
    service, inbox, entries = _manual_service()
    with pytest.raises(ManualEntryValidationError):
        service.create_order(
            _order_command(lines=()),
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            entered_by=ACTOR_KEY_ID,
            entered_by_name=ACTOR_NAME,
        )
    assert entries.entries == {}
    assert inbox.events == {}


# ---------------------------------------------------------------------------
# Same pipeline: a manual event travels the §10.3 pipeline like a webhook
# ---------------------------------------------------------------------------


def test_manual_order_converts_toman_to_rial_and_drafts() -> None:
    service, inbox, _entries = _manual_service()
    entry = _create_order(service)
    pipeline, engine, external_ids, audit_log, _review = _pipeline(inbox=inbox)

    result = pipeline.process(entry.events[0].inbox_event_id)

    assert result.outcome == PipelineOutcome.PROCESSED_DRAFT
    assert engine.create_calls == 1
    # The draft total is the converted Rial total: 630,000 Toman × 10.
    draft = next(iter(engine.drafts.values()))
    assert draft.total_debit.amount == 6_300_000
    # The §10.4 external-id map links the order to its engine draft.
    mapping = external_ids.get("manual", "order", "ORD-1001")
    assert mapping is not None
    assert mapping.target_entity_type == "account.move"
    assert result.draft_ref == mapping.target_external_id
    # The decision is in the audit log with policy + mapping versions.
    assert len(audit_log.decisions) == 1
    decision = audit_log.decisions[0]
    assert decision.decision == AutomationDecisionKind.DRAFT
    assert decision.policy_version == "policy-v2"
    assert decision.mapping_version == "mapping-v3"
    assert decision.inbox_event_id == entry.events[0].inbox_event_id
    assert must_get(inbox, entry.events[0].inbox_event_id).status == InboxEventStatus.PROCESSED


def test_manual_order_with_gate_allowed_posts() -> None:
    service, inbox, _entries = _manual_service()
    entry = _create_order(service)
    pipeline, engine, _external_ids, audit_log, _review = _pipeline(inbox=inbox, gate_allowed=True)

    result = pipeline.process(entry.events[0].inbox_event_id)

    assert result.outcome == PipelineOutcome.PROCESSED_POSTED
    assert result.posted_ref is not None
    assert engine.post_calls == 1
    assert audit_log.decisions[0].decision == AutomationDecisionKind.AUTO_POST


def test_manual_order_fails_closed_to_review_like_a_webhook() -> None:
    # Production wiring is still the F1 fail-closed defaults: a manual
    # event routes to needs_review exactly as a webhook event does.
    service, inbox, _entries = _manual_service()
    entry = _create_order(service)
    pipeline, engine, _external_ids, audit_log, review = _pipeline(inbox=inbox, fail_closed=True)

    result = pipeline.process(entry.events[0].inbox_event_id)

    assert result.outcome == PipelineOutcome.NEEDS_REVIEW
    assert engine.create_calls == 0
    assert must_get(inbox, entry.events[0].inbox_event_id).status == InboxEventStatus.NEEDS_REVIEW
    assert len(review.items) == 1
    assert review.items[0].type == "mapping"
    assert audit_log.decisions[0].decision == AutomationDecisionKind.NEEDS_REVIEW


def test_second_entry_of_the_same_order_is_duplicate_suppressed() -> None:
    service, inbox, _entries = _manual_service()
    first = _create_order(service)
    second = _create_order(service)  # same order number, a new entry + event
    assert first.id != second.id
    assert first.events[0].inbox_event_id != second.events[0].inbox_event_id
    pipeline, engine, _external_ids, _audit_log, _review = _pipeline(inbox=inbox)

    first_result = pipeline.process(first.events[0].inbox_event_id)
    second_result = pipeline.process(second.events[0].inbox_event_id)

    assert first_result.outcome == PipelineOutcome.PROCESSED_DRAFT
    assert second_result.outcome == PipelineOutcome.PROCESSED_DRAFT
    # Exactly one engine draft: the second event found the §10.4 mapping.
    assert engine.create_calls == 1
    assert second_result.draft_ref == first_result.draft_ref


def test_cancellation_of_a_processed_order_is_not_suppressed() -> None:
    service, inbox, _entries = _manual_service()
    order = _create_order(service)
    cancellation = service.create_cancellation(
        _cancellation_command(),
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        entered_by=ACTOR_KEY_ID,
        entered_by_name=ACTOR_NAME,
    )
    pipeline, engine, external_ids, _audit_log, _review = _pipeline(inbox=inbox)

    pipeline.process(order.events[0].inbox_event_id)
    result = pipeline.process(cancellation.events[0].inbox_event_id)

    assert result.outcome == PipelineOutcome.PROCESSED_DRAFT
    # Two drafts: the order's and the cancellation's corrective document.
    assert engine.create_calls == 2
    assert external_ids.get("manual", "order_cancellation", "ORD-1001") is not None


def test_refund_events_both_process_through_the_pipeline() -> None:
    service, inbox, _entries = _manual_service()
    entry = service.create_refund(
        _refund_command(),
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        entered_by=ACTOR_KEY_ID,
        entered_by_name=ACTOR_NAME,
    )
    pipeline, engine, external_ids, _audit_log, _review = _pipeline(inbox=inbox)

    outcomes = [pipeline.process(ref.inbox_event_id).outcome for ref in entry.events]

    assert outcomes == [PipelineOutcome.PROCESSED_DRAFT, PipelineOutcome.PROCESSED_DRAFT]
    assert engine.create_calls == 2
    assert external_ids.get("manual", "refund", "REF-1") is not None
    assert external_ids.get("manual", "payment_refund", "REF-1") is not None


# ---------------------------------------------------------------------------
# Query service: the read side of the traceability chain
# ---------------------------------------------------------------------------


def _query_service(
    entries: FakeManualEntryRepository,
    inbox: FakeInboxRepository,
    external_ids: FakeExternalIdMap,
    audit_log: FakeAuditLog,
) -> ManualEntryQueryService:
    return ManualEntryQueryService(entries, inbox, external_ids, audit_log)


def test_get_entry_traces_event_to_engine_record() -> None:
    service, inbox, entries = _manual_service()
    entry = _create_order(service)
    external_ids = FakeExternalIdMap()
    audit_log = FakeAuditLog()
    pipeline, engine, _, _, _ = _pipeline(inbox=inbox, external_ids=external_ids, audit_log=audit_log)
    pipeline.process(entry.events[0].inbox_event_id)

    detail = _query_service(entries, inbox, external_ids, audit_log).get_entry(entry.id, TENANT_ID)

    assert detail.entry.id == entry.id
    assert len(detail.traces) == 1
    trace = detail.traces[0]
    assert trace.event_type == "sales.order.completed"
    assert trace.status == "processed"
    assert trace.engine_record_id == "1"
    assert trace.decision == "draft"
    assert trace.processed_at is not None


def test_get_entry_missing_raises_typed_not_found() -> None:
    query = _query_service(
        FakeManualEntryRepository(), FakeInboxRepository(), FakeExternalIdMap(), FakeAuditLog()
    )
    with pytest.raises(ManualEntryNotFoundError):
        query.get_entry("no-such-entry", TENANT_ID)


def test_get_entry_of_another_tenant_looks_missing() -> None:
    service, inbox, entries = _manual_service()
    entry = _create_order(service)
    query = _query_service(entries, inbox, FakeExternalIdMap(), FakeAuditLog())
    with pytest.raises(ManualEntryNotFoundError):
        query.get_entry(entry.id, "another-tenant")


def test_list_entries_filters_by_type_and_carries_primary_status() -> None:
    service, inbox, entries = _manual_service()
    _create_order(service)
    service.create_refund(
        _refund_command(),
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        entered_by=ACTOR_KEY_ID,
        entered_by_name=ACTOR_NAME,
    )
    external_ids = FakeExternalIdMap()
    audit_log = FakeAuditLog()
    pipeline, _engine, _, _, _ = _pipeline(
        inbox=inbox, external_ids=external_ids, audit_log=audit_log
    )
    order_entry = entries.list_entries(TENANT_ID, entry_type=ManualEntryType.ORDER)[0]
    pipeline.process(order_entry.events[0].inbox_event_id)
    query = _query_service(entries, inbox, external_ids, audit_log)

    all_entries = query.list_entries(TENANT_ID)
    assert len(all_entries) == 2
    refunds = query.list_entries(TENANT_ID, entry_type=ManualEntryType.REFUND)
    assert len(refunds) == 1
    assert refunds[0].entry.entry_type == ManualEntryType.REFUND
    assert refunds[0].primary_status == "received"
    assert refunds[0].primary_engine_record_id is None
    orders = query.list_entries(TENANT_ID, entry_type=ManualEntryType.ORDER)
    assert orders[0].primary_status == "processed"
    assert orders[0].primary_engine_record_id == "1"
    assert orders[0].primary_decision == "draft"
