"""Infrastructure tests for the event-platform repositories (US-501).

Runs against the shared Supabase test DB (serialized with the other
agents' DB tests via the pytest lock). Sessions are transactional and
rolled back after each test, except the concurrency tests, which use
their own committed sessions and clean up their unique rows.
"""

from __future__ import annotations

import dataclasses
import datetime
import os
import threading
import uuid
from collections.abc import Generator

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from hesabyar.domain.events.envelope import CanonicalEvent, CanonicalEventType
from hesabyar.domain.events.models import (
    AutomationDecision,
    AutomationDecisionKind,
    ExternalIdMapping,
    InboxEventStatus,
    IntegrationAttempt,
    IntegrationAttemptStatus,
    StoreSettings,
)
from hesabyar.domain.events.pipeline import IDEMPOTENCY_TTL
from hesabyar.infrastructure.persistence.postgres.event_repositories import (
    SqlAlchemyDecisionAuditLogRepository,
    SqlAlchemyEventIdempotencyStore,
    SqlAlchemyEventInboxRepository,
    SqlAlchemyEventOutboxRepository,
    SqlAlchemyExternalIdMapRepository,
    SqlAlchemyStoreSettingsRepository,
)
from hesabyar.infrastructure.persistence.postgres.models import (
    Base,
    ConnectorStoreSettingsModel,
    TenantModel,
)
from tests.event_fixtures import make_outbox_event, raw_envelope

TEST_DB_URL = os.getenv(
    "HESABYAR_DATABASE_URL",
    "postgresql+psycopg://hesabyar:hesabyar_pass@hesabyar-postgres:5432/hesabyar_test",
)

EXPECTED_COLUMNS = {
    "automation_decision_log": {
        "id",
        "tenant_id",
        "inbox_event_id",
        "event_type",
        "decision",
        "policy_version",
        "mapping_version",
        "gate_results",
        "decided_by",
        "decided_at",
        "correlation_id",
        "details",
        "created_at",
    },
    "connector_store_settings": {
        "id",
        "source_system",
        "source_store_id",
        "prices_in_toman",
        "currency",
        "created_at",
        "updated_at",
    },
}


@pytest.fixture(scope="module")
def engine() -> Generator[Engine, None, None]:
    eng = create_engine(TEST_DB_URL, echo=False)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def db(engine: Engine) -> Generator[Session, None, None]:
    connection = engine.connect()
    transaction = connection.begin()
    session = sessionmaker(bind=connection)()
    try:
        yield session
    finally:
        session.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()


@pytest.fixture()
def tenant(db: Session) -> TenantModel:
    row = TenantModel(
        id=str(uuid.uuid4()),
        slug=f"evt-infra-501-{uuid.uuid4().hex[:8]}",
        name="Event Infra Test Tenant",
    )
    db.add(row)
    db.flush()
    return row


def _unique_source() -> str:
    return f"evt-test-{uuid.uuid4().hex[:10]}"


def _event(source_system: str | None = None, **overrides: object) -> CanonicalEvent:
    return CanonicalEvent.from_raw(
        raw_envelope(source_system=source_system or _unique_source(), **overrides)  # type: ignore[arg-type]
    )


#: The alongside event type used by the store tests (the envelope has no type field).
EVENT_TYPE = CanonicalEventType.SALES_ORDER_COMPLETED


def test_event_tables_exist_with_expected_columns(engine: Engine) -> None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    for table, columns in EXPECTED_COLUMNS.items():
        assert table in tables, f"{table} missing"
        db_columns = {c["name"] for c in inspector.get_columns(table)}
        assert db_columns == columns, (
            f"{table}: DB-only={sorted(db_columns - columns)}, "
            f"model-only={sorted(columns - db_columns)}"
        )
    inbox_columns = {c["name"] for c in inspector.get_columns("inbox_events")}
    assert "dead_lettered_at" in inbox_columns


def test_inbox_store_created_duplicate_conflict(db: Session, tenant: TenantModel) -> None:
    repo = SqlAlchemyEventInboxRepository(db)
    source = _unique_source()
    first = _event(source, payload={"total_amount": 175000})
    outcome = repo.store(first, EVENT_TYPE, tenant.id)
    assert outcome.status == "created"
    assert outcome.event is not None
    assert outcome.event.status == InboxEventStatus.RECEIVED
    assert outcome.event.event_type == "sales.order.completed"

    duplicate = repo.store(first, EVENT_TYPE, tenant.id)
    assert duplicate.status == "duplicate"
    assert duplicate.event is not None
    assert duplicate.event.id == outcome.event.id

    changed = _event(source, payload={"total_amount": 999000})
    conflict = repo.store(changed, EVENT_TYPE, tenant.id)
    assert conflict.status == "conflict"
    assert conflict.existing_event_id == outcome.event.id
    assert repo.count_events(tenant.id) == 1


def test_concurrent_duplicate_store_serializes(engine: Engine) -> None:
    """Two concurrent stores of the same event: one created, one duplicate, one row.

    Uses committed sessions (the advisory lock is transaction-scoped,
    so the rolled-back fixture session cannot exercise it); the rows
    are deleted with a committed session afterwards.
    """
    source = _unique_source()
    event = _event(source)
    results: list[str] = []

    def _store() -> None:
        session = sessionmaker(bind=engine)()
        try:
            outcome = SqlAlchemyEventInboxRepository(session).store(event, EVENT_TYPE, None)
            results.append(outcome.status)
            session.commit()
        finally:
            session.close()

    threads = [threading.Thread(target=_store) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == ["created", "duplicate"]
    session = sessionmaker(bind=engine)()
    try:
        repo = SqlAlchemyEventInboxRepository(session)
        assert repo.count_events(None, source_system=source) == 1
    finally:
        session.execute(
            text("DELETE FROM inbox_events WHERE source_system = :source"),
            {"source": source},
        )
        session.commit()
        session.close()


def test_claim_due_claims_each_event_once(engine: Engine) -> None:
    """Two workers claiming concurrently never claim the same event (SKIP LOCKED)."""
    source = _unique_source()
    setup = sessionmaker(bind=engine)()
    try:
        repo = SqlAlchemyEventInboxRepository(setup)
        for index in range(4):
            repo.store(_event(source, source_event_id=f"evt-{index}"), EVENT_TYPE, None)
        setup.commit()
    finally:
        setup.close()
    claimed: list[str] = []

    def _claim() -> None:
        session = sessionmaker(bind=engine)()
        try:
            events = SqlAlchemyEventInboxRepository(session).claim_due(10)
            claimed.extend(event.id for event in events)
            session.commit()
        finally:
            session.close()

    threads = [threading.Thread(target=_claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(claimed) == 4
    assert len(set(claimed)) == 4
    session = sessionmaker(bind=engine)()
    try:
        repo = SqlAlchemyEventInboxRepository(session)
        stored = repo.list_events(None, status=InboxEventStatus.PROCESSING, limit=100)
        mine = [event for event in stored if event.source_system == source]
        assert len(mine) == 4
        assert all(event.attempts == 1 for event in mine)
    finally:
        session.execute(
            text("DELETE FROM inbox_events WHERE source_system = :source"),
            {"source": source},
        )
        session.commit()
        session.close()


def test_claim_one_semantics(db: Session, tenant: TenantModel) -> None:
    repo = SqlAlchemyEventInboxRepository(db)
    event = _event()
    outcome = repo.store(event, EVENT_TYPE, tenant.id)
    assert outcome.event is not None
    claimed = repo.claim_one(outcome.event.id)
    assert claimed is not None
    assert claimed.status == InboxEventStatus.PROCESSING
    assert claimed.attempts == 1
    # A processed event is not claimable.
    processed = repo.save(dataclasses.replace(claimed, status=InboxEventStatus.PROCESSED))
    assert repo.claim_one(processed.id) is None
    # An event whose next attempt is in the future is not claimable.
    future = repo.save(
        dataclasses.replace(
            processed,
            status=InboxEventStatus.RECEIVED,
            next_attempt_at=datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1),
        )
    )
    assert repo.claim_one(future.id) is None
    # A missing event is not claimable.
    assert repo.claim_one(str(uuid.uuid4())) is None


def test_list_stale_only_returns_stuck_events(db: Session, tenant: TenantModel) -> None:
    repo = SqlAlchemyEventInboxRepository(db)
    old = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=25)
    recent = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=1)
    stale = repo.store(_event(source_event_id="old"), EVENT_TYPE, tenant.id).event
    fresh = repo.store(_event(source_event_id="fresh"), EVENT_TYPE, tenant.id).event
    done = repo.store(_event(source_event_id="done"), EVENT_TYPE, tenant.id).event
    assert stale is not None and fresh is not None and done is not None
    db.execute(
        text("UPDATE inbox_events SET received_at = :at WHERE id = :id"),
        {"at": old, "id": stale.id},
    )
    db.execute(
        text("UPDATE inbox_events SET received_at = :at WHERE id = :id"),
        {"at": recent, "id": fresh.id},
    )
    db.execute(
        text("UPDATE inbox_events SET received_at = :at, status = 'processed' WHERE id = :id"),
        {"at": old, "id": done.id},
    )
    db.flush()
    stale_events = repo.list_stale(datetime.timedelta(hours=24))
    # list_stale is cross-tenant; filter to this test's tenant.
    mine = [event for event in stale_events if event.tenant_id == tenant.id]
    assert [event.id for event in mine] == [stale.id]
    assert repo.count_stale(datetime.timedelta(hours=24)) >= 1


def test_idempotency_store_claim_complete_ttl_and_purge(db: Session) -> None:
    store = SqlAlchemyEventIdempotencyStore(db)
    installation = f"inst-{uuid.uuid4().hex[:8]}"
    claimed = store.claim(installation, "events.ingest", "key-1", "hash-1")
    assert claimed is not None
    assert claimed.response_status is None
    assert store.claim(installation, "events.ingest", "key-1", "hash-1") is None
    found = store.find(installation, "events.ingest", "key-1")
    assert found is not None and found.request_hash == "hash-1"
    assert found.is_completed is False
    store.complete(claimed, response_status=201, response_body={"id": "evt-1"}, response_reference="evt-1")
    completed = store.find(installation, "events.ingest", "key-1")
    assert completed is not None and completed.is_completed is True
    assert completed.response_status == 201
    assert completed.response_reference == "evt-1"
    # Completed event records carry the 30-day TTL, not the 24h default.
    assert completed.expires_at is not None
    remaining = completed.expires_at - datetime.datetime.now(datetime.UTC)
    assert datetime.timedelta(days=29) < remaining <= IDEMPOTENCY_TTL
    # An expired record purges; a live one does not.
    store.claim(installation, "events.ingest", "key-expired", "hash-2")
    db.execute(
        text(
            "UPDATE idempotency_records SET expires_at = :at "
            "WHERE installation_id = :inst AND idempotency_key = :key"
        ),
        {
            "at": datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=1),
            "inst": installation,
            "key": "key-expired",
        },
    )
    db.flush()
    # purge_expired deletes every expired record in the shared DB, so
    # the count is a lower bound, not exact.
    assert store.purge_expired(datetime.datetime.now(datetime.UTC)) >= 1
    assert store.find(installation, "events.ingest", "key-expired") is None
    assert store.find(installation, "events.ingest", "key-1") is not None


def test_external_id_map_save_get_and_duplicate(db: Session, tenant: TenantModel) -> None:
    repo = SqlAlchemyExternalIdMapRepository(db)
    mapping = ExternalIdMapping(
        source_system=_unique_source(),
        external_entity_type="order",
        external_entity_id="order-1",
        target_engine="tryton",
        target_entity_type="account.move",
        target_external_id="42",
        engine_company_id=None,
        tenant_id=tenant.id,
    )
    saved = repo.save(mapping)
    assert saved.target_external_id == "42"
    found = repo.get(mapping.source_system, "order", "order-1")
    assert found is not None and found.target_external_id == "42"
    # Saving the same mapping twice returns the existing row (no error).
    again = repo.save(mapping)
    assert again.target_external_id == "42"
    assert repo.get(mapping.source_system, "order", "order-2") is None


def test_decision_audit_log_round_trip_and_filters(db: Session, tenant: TenantModel) -> None:
    inbox_repo = SqlAlchemyEventInboxRepository(db)
    audit_repo = SqlAlchemyDecisionAuditLogRepository(db)
    event = inbox_repo.store(_event(), EVENT_TYPE, tenant.id).event
    assert event is not None
    decision = AutomationDecision(
        id=str(uuid.uuid4()),
        tenant_id=tenant.id,
        inbox_event_id=event.id,
        event_type="sales.order.completed",
        decision=AutomationDecisionKind.AUTO_POST,
        policy_version="policy-v2",
        mapping_version="mapping-v3",
        gate_results=(),
        decided_by="system",
        decided_at=datetime.datetime.now(datetime.UTC),
        correlation_id="corr-1",
        details={"reason": "Gate allowed."},
    )
    recorded = audit_repo.record(decision)
    assert recorded.id == decision.id
    found = audit_repo.list_decisions(tenant.id, inbox_event_id=event.id)
    assert len(found) == 1
    assert found[0].policy_version == "policy-v2"
    assert found[0].mapping_version == "mapping-v3"
    assert audit_repo.list_decisions(tenant.id, decision="draft") == []
    assert audit_repo.list_decisions(tenant.id, event_type="payment.captured") == []


def test_store_settings_round_trip_and_unique(db: Session) -> None:
    repo = SqlAlchemyStoreSettingsRepository(db)
    source = _unique_source()
    assert repo.get(source, "store-1") is None
    saved = repo.save(
        StoreSettings(
            source_system=source,
            source_store_id="store-1",
            prices_in_toman=False,
            currency="IRR",
        )
    )
    assert saved.prices_in_toman is False
    found = repo.get(source, "store-1")
    assert found is not None and found.prices_in_toman is False
    # The (source_system, source_store_id) unique constraint holds.
    db.add(
        ConnectorStoreSettingsModel(
            source_system=source,
            source_store_id="store-1",
            prices_in_toman=True,
            currency="IRR",
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()


def test_outbox_save_list_claim_and_attempts(db: Session, tenant: TenantModel) -> None:
    repo = SqlAlchemyEventOutboxRepository(db)
    event = make_outbox_event(tenant_id=tenant.id)
    saved = repo.save(event)
    assert saved.id == event.id
    assert repo.get(event.id) is not None
    listed = repo.list_events(tenant.id, status="pending")
    assert any(item.id == event.id for item in listed)
    pending = repo.claim_pending(10)
    assert any(item.id == event.id for item in pending)
    attempt = IntegrationAttempt(
        id=str(uuid.uuid4()),
        inbox_event_id=None,
        outbox_event_id=event.id,
        attempt_number=1,
        status=IntegrationAttemptStatus.SUCCEEDED,
        started_at=datetime.datetime.now(datetime.UTC),
        finished_at=datetime.datetime.now(datetime.UTC),
        error_code=None,
        error_message=None,
        created_at=datetime.datetime.now(datetime.UTC),
    )
    saved_attempt = repo.save_integration_attempt(attempt)
    assert saved_attempt.id == attempt.id
