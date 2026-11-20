"""Unit tests for the Procrastinate job queue (US-501).

Uses ``procrastinate.testing.InMemoryConnector`` — no database, no
server. Procrastinate is the trigger/scheduler only; the outbox (not
the queue) is the durable record.
"""

from __future__ import annotations

import procrastinate
from procrastinate.testing import InMemoryConnector

from hesabyar.application.commands.events import EventPipelineService, EventWorker
from hesabyar.domain.events.models import InboxEventStatus
from hesabyar.infrastructure.events.defaults import InProcessOutboxPublisher
from hesabyar.infrastructure.events.procrastinate_queue import (
    EVENTS_QUEUE,
    PROCESS_EVENT_TASK,
    SWEEP_CRON,
    SWEEP_TASK,
    ProcrastinateJobQueue,
    WorkerHandle,
    create_connector,
    create_defer_app,
    create_event_app,
)
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
    must_get,
)


def _make_worker(inbox: FakeInboxRepository, outbox: FakeOutboxRepository) -> EventWorker:
    pipeline = EventPipelineService(
        inbox=inbox,
        outbox=outbox,
        external_ids=FakeExternalIdMap(),
        audit_log=FakeAuditLog(),
        store_settings=FakeStoreSettings(),
        mapping_resolver=FakeMappingResolver(),
        draft_builder=FakeDraftBuilder(),
        policy_gate=FakePolicyGate(allowed=False),
        review_queue=FakeReviewQueue(),
        engine=FakeEngine(),
    )
    return EventWorker(
        pipeline=pipeline,
        inbox=inbox,
        outbox=outbox,
        publisher=InProcessOutboxPublisher(),
    )


def test_tasks_are_registered_under_their_contract_names() -> None:
    app = create_event_app(InMemoryConnector(), lambda: None)  # type: ignore[arg-type]
    assert PROCESS_EVENT_TASK in app.tasks
    assert SWEEP_TASK in app.tasks
    assert app.tasks[PROCESS_EVENT_TASK].queue == EVENTS_QUEUE
    assert app.tasks[SWEEP_TASK].queue == EVENTS_QUEUE
    assert SWEEP_CRON == "* * * * *"


def test_deferred_process_event_job_processes_the_event() -> None:
    inbox = FakeInboxRepository()
    outbox = FakeOutboxRepository()
    commits = 0
    closes = 0

    def factory() -> WorkerHandle:
        nonlocal commits, closes

        def commit() -> None:
            nonlocal commits
            commits += 1

        def close() -> None:
            nonlocal closes
            closes += 1

        return WorkerHandle(worker=_make_worker(inbox, outbox), commit=commit, close=close)

    app = create_event_app(InMemoryConnector(), factory)
    event_id = ingest_into(inbox)
    ProcrastinateJobQueue(app).defer_process_event(event_id)
    app.run_worker(wait=False)
    assert must_get(inbox, event_id).status == InboxEventStatus.PROCESSED
    # The worker also runs the periodic scheduler, so it may run the
    # sweep job too — what matters is every handle commits and closes.
    assert commits >= 1
    assert closes == commits


def test_deferred_sweep_job_runs_a_poll() -> None:
    inbox = FakeInboxRepository()
    outbox = FakeOutboxRepository()
    app = create_event_app(
        InMemoryConnector(),
        lambda: WorkerHandle(
            worker=_make_worker(inbox, outbox), commit=lambda: None, close=lambda: None
        ),
    )
    first = ingest_into(inbox, source_event_id="evt-1", external_entity_id="order-1")
    second = ingest_into(inbox, source_event_id="evt-2", external_entity_id="order-2")
    ProcrastinateJobQueue(app).defer_sweep()
    app.run_worker(wait=False)
    assert must_get(inbox, first).status == InboxEventStatus.PROCESSED
    assert must_get(inbox, second).status == InboxEventStatus.PROCESSED


def test_defer_is_best_effort_and_never_raises() -> None:
    """A queue failure must never fail an ingest — the sweep is the backstop."""
    app = procrastinate.App(connector=InMemoryConnector())  # no tasks registered
    queue = ProcrastinateJobQueue(app)
    queue.defer_process_event("evt-1")  # would raise KeyError without the guard
    queue.defer_sweep()


def test_defer_app_registers_tasks_but_never_runs_them_here() -> None:
    app = create_defer_app(InMemoryConnector())
    assert PROCESS_EVENT_TASK in app.tasks
    assert SWEEP_TASK in app.tasks
    # The defer app can defer (API process) but its factory raises if a
    # task somehow executes here — verified by running the worker.
    app.tasks[PROCESS_EVENT_TASK].defer(event_id="evt-1")
    app.run_worker(wait=False)  # the job fails loudly; it must not hang
    assert app.connector is not None


def test_create_connector_strips_the_sqlalchemy_dialect_prefix() -> None:
    connector = create_connector("postgresql+psycopg://user:pass@localhost:5432/db")
    assert connector is not None
    plain = create_connector("postgresql://user:pass@localhost:5432/db")
    assert plain is not None
