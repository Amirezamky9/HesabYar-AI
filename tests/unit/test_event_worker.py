"""Unit tests for the event worker (database poller, US-501).

The worker runs against the in-memory fakes — no database, no server.
"""

from __future__ import annotations

import datetime

from hesabyar.application.commands.events import EventPipelineService, EventWorker
from hesabyar.domain.events.models import (
    InboxEventStatus,
    OutboxEventStatus,
)
from hesabyar.infrastructure.events.defaults import InProcessOutboxPublisher
from tests.event_fixtures import (
    FakeAuditLog,
    FakeDraftBuilder,
    FakeEngine,
    FakeExternalIdMap,
    FakeInboxRepository,
    FakeMappingResolver,
    FakeOutboxRepository,
    FakePolicyGate,
    FakeReviewQueue,
    FakeStoreSettings,
    ingest_into,
    make_inbox_event,
    make_outbox_event,
    must_get,
    must_get_outbox,
)


def _worker(
    *,
    gate_allowed: bool = False,
    publisher: InProcessOutboxPublisher | None = None,
) -> tuple[EventWorker, FakeInboxRepository, FakeOutboxRepository, FakeEngine]:
    inbox = FakeInboxRepository()
    outbox = FakeOutboxRepository()
    engine = FakeEngine()
    pipeline = EventPipelineService(
        inbox=inbox,
        outbox=outbox,
        external_ids=FakeExternalIdMap(),
        audit_log=FakeAuditLog(),
        store_settings=FakeStoreSettings(),
        mapping_resolver=FakeMappingResolver(),
        draft_builder=FakeDraftBuilder(),
        policy_gate=FakePolicyGate(allowed=gate_allowed),
        review_queue=FakeReviewQueue(),
        engine=engine,
    )
    worker = EventWorker(
        pipeline=pipeline,
        inbox=inbox,
        outbox=outbox,
        publisher=publisher or InProcessOutboxPublisher(),
    )
    return worker, inbox, outbox, engine


def test_poll_once_processes_due_events() -> None:
    worker, inbox, _outbox, engine = _worker()
    first = ingest_into(inbox, source_event_id="evt-1", external_entity_id="order-1")
    second = ingest_into(inbox, source_event_id="evt-2", external_entity_id="order-2")
    report = worker.poll_once()
    assert report.claimed == 2
    assert report.processed_draft == 2
    assert report.errors == ()
    assert must_get(inbox, first).status == InboxEventStatus.PROCESSED
    assert must_get(inbox, second).status == InboxEventStatus.PROCESSED
    assert engine.create_calls == 2


def test_poll_once_skips_events_not_yet_due() -> None:
    worker, inbox, _outbox, engine = _worker()
    event_id = ingest_into(inbox)
    stored = must_get(inbox, event_id)
    inbox.save(make_inbox_event(
        stored.to_canonical_event(),
        event_type=stored.event_type,
        tenant_id=stored.tenant_id,
        next_attempt_at=datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=10),
    ))
    report = worker.poll_once()
    assert report.claimed == 0
    assert engine.create_calls == 0


def test_poll_once_publishes_pending_outbox_events() -> None:
    received: list[str] = []

    def _record(event: object) -> bool:
        received.append(event.id)  # type: ignore[attr-defined]
        return True

    publisher = InProcessOutboxPublisher({"events.processed": _record})
    worker, inbox, outbox, _engine = _worker(publisher=publisher)
    event = make_outbox_event()
    outbox.save(event)
    report = worker.poll_once()
    assert report.outbox_published == 1
    assert report.outbox_failed == 0
    assert received == [event.id]
    assert must_get_outbox(outbox, event.id).status == OutboxEventStatus.PUBLISHED
    assert must_get_outbox(outbox, event.id).published_at is not None


def test_poll_once_marks_failed_publishes_and_continues() -> None:
    def _boom(event: object) -> bool:
        raise RuntimeError("Consumer is down.")

    publisher = InProcessOutboxPublisher({"events.processed": _boom})
    worker, _inbox, outbox, _engine = _worker(publisher=publisher)
    event = make_outbox_event()
    outbox.save(event)
    report = worker.poll_once()
    assert report.outbox_published == 0
    assert report.outbox_failed == 1
    stored = must_get_outbox(outbox, event.id)
    assert stored.status == OutboxEventStatus.FAILED
    assert stored.last_error is not None and "Consumer is down" in stored.last_error


def test_poll_once_with_no_handlers_publishes_to_zero_consumers() -> None:
    """The default publisher has no handlers — events still publish cleanly."""
    worker, _inbox, outbox, _engine = _worker()
    event = make_outbox_event()
    outbox.save(event)
    report = worker.poll_once()
    assert report.outbox_published == 1
    assert must_get_outbox(outbox, event.id).status == OutboxEventStatus.PUBLISHED


def test_process_one_returns_none_for_missing_events() -> None:
    """A trigger that fires before the ingest commits is a no-op (sweep backstop)."""
    worker, _inbox, _outbox, _engine = _worker()
    assert worker.process_one("does-not-exist") is None


def test_process_one_claims_and_processes() -> None:
    worker, inbox, _outbox, engine = _worker(gate_allowed=True)
    event_id = ingest_into(inbox)
    result = worker.process_one(event_id)
    assert result is not None
    assert result.outcome.value == "processed_posted"
    assert must_get(inbox, event_id).status == InboxEventStatus.PROCESSED
    assert engine.post_calls == 1


def test_process_one_does_not_reprocess_a_processed_event() -> None:
    worker, inbox, _outbox, engine = _worker()
    event_id = ingest_into(inbox)
    worker.process_one(event_id)
    # A second trigger for the same event finds it processed and skips it.
    assert worker.process_one(event_id) is None
    assert engine.create_calls == 1
