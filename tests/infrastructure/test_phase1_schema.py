"""Tests for the Phase 1 schema models (US-102): API access, audit, engine
mapping, inbox/outbox, integration cursors/attempts, idempotency.

Runs against the shared hesabyar_test DB in the same style as
test_postgres.py: tables are created by Base.metadata.create_all (a no-op
once Alembic is at head) and each test runs in a rolled-back transaction.
The migration roundtrip itself lives in test_migrations.py on a separate
throwaway database.
"""

from __future__ import annotations

import datetime
import os
import uuid
from collections.abc import Generator

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from hesabyar.infrastructure.persistence.postgres.models import (
    ActorModel,
    ApiClientModel,
    ApiKeyModel,
    AuditEventModel,
    AuditEventSubjectModel,
    Base,
    EngineCompanyModel,
    EngineConnectionModel,
    ExternalIdMapModel,
    IdempotencyRecordModel,
    InboxEventModel,
    IntegrationAttemptModel,
    IntegrationCursorModel,
    OutboxEventModel,
    TenantModel,
)

TEST_DB_URL = os.getenv(
    "HESABYAR_DATABASE_URL",
    "postgresql+psycopg://hesabyar:hesabyar_pass@hesabyar-postgres:5432/hesabyar_test",
)

INSTALLATION_ID = "00000000-0000-0000-0000-000000000001"

TRUNCATE_SQL = (
    "TRUNCATE TABLE integration_attempts, inbox_events, outbox_events, "
    "integration_cursors, idempotency_records, audit_event_subjects, "
    "audit_events, api_keys, api_clients, external_id_map, engine_companies, "
    "engine_connections, actors, tenants RESTART IDENTITY CASCADE;"
)


@pytest.fixture(scope="module")
def engine():
    """Create a SQLAlchemy engine connected to the test database."""
    eng = create_engine(TEST_DB_URL, echo=False)
    # Ensure tables exist without dropping schema or desynchronizing Alembic.
    Base.metadata.create_all(eng)
    with eng.begin() as conn:
        conn.execute(text(TRUNCATE_SQL))
    yield eng
    with eng.begin() as conn:
        conn.execute(text(TRUNCATE_SQL))
    eng.dispose()


@pytest.fixture()
def db(engine: Engine) -> Generator[Session, None, None]:
    """Provide a transactional session that rolls back after each test."""
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


def _make_tenant(slug: str) -> TenantModel:
    return TenantModel(id=str(uuid.uuid4()), slug=slug, name="Phase 1 Test Tenant")


def _make_actor(tenant_id: str, username: str) -> ActorModel:
    return ActorModel(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        username=username,
        display_name="Test Actor",
    )


def _make_client(tenant_id: str, name: str = "hayat-shop") -> ApiClientModel:
    return ApiClientModel(id=str(uuid.uuid4()), tenant_id=tenant_id, name=name)


def _make_key(client_id: str, key_hash: str, prefix: str = "hy_live_") -> ApiKeyModel:
    return ApiKeyModel(
        id=str(uuid.uuid4()),
        client_id=client_id,
        installation_id=INSTALLATION_ID,
        name="shop key",
        key_prefix=prefix,
        key_hash=key_hash,
        scopes="company:read,account:read",
    )


def _make_connection(name: str = "tryton-dev") -> EngineConnectionModel:
    return EngineConnectionModel(
        id=str(uuid.uuid4()),
        name=name,
        engine="tryton",
        base_url="http://localhost:8000",
        database_name="hesabyar_tryton_test",
    )


def _make_company(
    tenant_id: str, connection_id: str, external_id: str = "2"
) -> EngineCompanyModel:
    return EngineCompanyModel(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        engine_connection_id=connection_id,
        external_company_id=external_id,
        company_name="HesabYar Adapter Test",
        currency_code="IRR",
    )


def _make_inbox_event(**overrides: object) -> InboxEventModel:
    values: dict[str, object] = {
        "id": str(uuid.uuid4()),
        "source_system": "hayat",
        "source_store_id": "store-1",
        "source_event_id": "evt-1",
        "event_type": "sales.order.completed",
        "occurred_at": datetime.datetime.now(datetime.UTC),
        "payload": {"order": 1},
        "payload_hash": "aabbcc",
    }
    values.update(overrides)
    return InboxEventModel(**values)  # type: ignore[arg-type]


def _make_outbox_event(tenant_id: str) -> OutboxEventModel:
    return OutboxEventModel(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        aggregate_type="journal_draft",
        aggregate_id="draft-1",
        event_type="ledger.journal.posted",
        payload={"draft": "draft-1"},
    )


class TestApiAccess:
    def test_create_client_and_key(self, db: Session) -> None:
        tenant = _make_tenant("api-client-test")
        db.add(tenant)
        db.flush()

        client = _make_client(tenant.id)
        db.add(client)
        db.flush()

        key = _make_key(client.id, key_hash="00" * 32)
        db.add(key)
        db.flush()

        found = db.execute(select(ApiKeyModel).where(ApiKeyModel.id == key.id)).scalar_one()
        assert found.key_prefix == "hy_live_"
        assert found.key_hash == "00" * 32
        assert found.scopes == "company:read,account:read"
        assert found.scope_list == ["company:read", "account:read"]
        assert found.last_used_at is None
        assert found.expires_at is None
        assert found.revoked_at is None
        assert found.engine_company_id is None
        assert found.created_at is not None

    def test_key_scope_list_empty(self, db: Session) -> None:
        tenant = _make_tenant("api-empty-scopes")
        client = _make_client(tenant.id)
        db.add_all([tenant, client])
        db.flush()

        key = _make_key(client.id, key_hash="11" * 32, prefix="hy_noscope")
        key.scopes = ""
        db.add(key)
        db.flush()
        assert key.scope_list == []

    def test_key_with_company_restriction_and_creator(self, db: Session) -> None:
        tenant = _make_tenant("api-restricted-key")
        actor = _make_actor(tenant.id, "key-creator")
        connection = _make_connection()
        company = _make_company(tenant.id, connection.id)
        client = _make_client(tenant.id)
        db.add_all([tenant, actor, connection, company, client])
        db.flush()

        key = _make_key(client.id, key_hash="22" * 32)
        key.engine_company_id = company.id
        key.created_by_actor_id = actor.id
        key.expires_at = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=30)
        db.add(key)
        db.flush()

        found = db.execute(select(ApiKeyModel).where(ApiKeyModel.id == key.id)).scalar_one()
        assert found.engine_company_id == company.id
        assert found.created_by_actor_id == actor.id
        assert found.expires_at is not None

    def test_duplicate_key_hash_rejected(self, db: Session) -> None:
        tenant = _make_tenant("api-dup-hash")
        client = _make_client(tenant.id)
        db.add_all([tenant, client])
        db.flush()

        db.add(_make_key(client.id, key_hash="33" * 32, prefix="hy_first_"))
        db.flush()
        db.add(_make_key(client.id, key_hash="33" * 32, prefix="hy_second"))
        with pytest.raises(IntegrityError):
            db.flush()

    def test_key_requires_existing_client(self, db: Session) -> None:
        key = _make_key(str(uuid.uuid4()), key_hash="44" * 32)
        db.add(key)
        with pytest.raises(IntegrityError):
            db.flush()

    def test_client_name_unique_per_tenant(self, db: Session) -> None:
        tenant = _make_tenant("api-dup-name")
        db.add(tenant)
        db.flush()

        db.add(_make_client(tenant.id, name="same-name"))
        db.flush()
        db.add(_make_client(tenant.id, name="same-name"))
        with pytest.raises(IntegrityError):
            db.flush()

    def test_same_client_name_different_tenant_allowed(self, db: Session) -> None:
        t1 = _make_tenant("api-name-a")
        t2 = _make_tenant("api-name-b")
        db.add_all([t1, t2])
        db.flush()

        db.add_all([_make_client(t1.id, name="shared-name"), _make_client(t2.id, name="shared-name")])
        db.flush()  # No IntegrityError — different tenants.

    def test_delete_client_cascades_keys(self, db: Session) -> None:
        tenant = _make_tenant("api-cascade")
        client = _make_client(tenant.id)
        db.add_all([tenant, client])
        db.flush()

        key = _make_key(client.id, key_hash="55" * 32)
        db.add(key)
        db.flush()

        db.delete(client)
        db.flush()

        remaining = db.execute(
            select(ApiKeyModel).where(ApiKeyModel.id == key.id)
        ).scalar_one_or_none()
        assert remaining is None


class TestAudit:
    def _make_event(self, tenant_id: str) -> AuditEventModel:
        return AuditEventModel(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            installation_id=INSTALLATION_ID,
            action="ledger.draft.create",
            resource_type="journal_draft",
            resource_id="draft-1",
            outcome="success",
        )

    def test_create_event_with_subjects(self, db: Session) -> None:
        tenant = _make_tenant("audit-test")
        db.add(tenant)
        db.flush()

        event = self._make_event(tenant.id)
        event.metadata_before = {"status": "draft"}
        event.metadata_after = {"status": "validated"}
        event.payload_hash = "deadbeef"
        db.add(event)
        db.flush()

        db.add_all([
            AuditEventSubjectModel(
                id=str(uuid.uuid4()),
                audit_event_id=event.id,
                subject_type="company",
                subject_id="2",
                role="target",
            ),
            AuditEventSubjectModel(
                id=str(uuid.uuid4()),
                audit_event_id=event.id,
                subject_type="journal_draft",
                subject_id="draft-1",
                role="affected",
            ),
        ])
        db.flush()

        db.refresh(event)
        assert len(event.subjects) == 2
        assert event.metadata_after == {"status": "validated"}
        assert event.authorization_result == "allowed"
        assert event.occurred_at is not None

    def test_duplicate_subject_rejected(self, db: Session) -> None:
        tenant = _make_tenant("audit-dup-subject")
        db.add(tenant)
        db.flush()

        event = self._make_event(tenant.id)
        db.add(event)
        db.flush()

        def _subject() -> AuditEventSubjectModel:
            return AuditEventSubjectModel(
                id=str(uuid.uuid4()),
                audit_event_id=event.id,
                subject_type="company",
                subject_id="2",
            )

        db.add(_subject())
        db.flush()
        db.add(_subject())
        with pytest.raises(IntegrityError):
            db.flush()

    def test_delete_event_cascades_subjects(self, db: Session) -> None:
        tenant = _make_tenant("audit-cascade")
        db.add(tenant)
        db.flush()

        event = self._make_event(tenant.id)
        db.add(event)
        db.flush()
        subject = AuditEventSubjectModel(
            id=str(uuid.uuid4()),
            audit_event_id=event.id,
            subject_type="company",
            subject_id="2",
        )
        db.add(subject)
        db.flush()

        db.delete(event)
        db.flush()

        remaining = db.execute(
            select(AuditEventSubjectModel).where(AuditEventSubjectModel.id == subject.id)
        ).scalar_one_or_none()
        assert remaining is None

    def test_event_requires_tenant(self, db: Session) -> None:
        event = self._make_event(str(uuid.uuid4()))
        db.add(event)
        with pytest.raises(IntegrityError):
            db.flush()


class TestEngineMapping:
    def test_create_connection_company_and_map(self, db: Session) -> None:
        tenant = _make_tenant("engine-map-test")
        connection = _make_connection()
        company = _make_company(tenant.id, connection.id)
        db.add_all([tenant, connection, company])
        db.flush()

        mapping = ExternalIdMapModel(
            id=str(uuid.uuid4()),
            tenant_id=tenant.id,
            source_system="hayat",
            external_entity_type="order",
            external_entity_id="order-1",
            target_engine="tryton",
            target_entity_type="account.invoice",
            target_external_id="42",
            engine_company_id=company.id,
        )
        db.add(mapping)
        db.flush()

        found = db.execute(
            select(ExternalIdMapModel).where(ExternalIdMapModel.id == mapping.id)
        ).scalar_one()
        assert found.target_external_id == "42"
        assert found.engine_company_id == company.id
        assert connection.is_active is True
        assert company.currency_code == "IRR"

    def test_tenant_connection_unique_rejected(self, db: Session) -> None:
        tenant = _make_tenant("engine-dup-tenant-conn")
        connection = _make_connection()
        db.add_all([tenant, connection])
        db.flush()

        db.add(_make_company(tenant.id, connection.id, external_id="2"))
        db.flush()
        db.add(_make_company(tenant.id, connection.id, external_id="3"))
        with pytest.raises(IntegrityError):
            db.flush()

    def test_connection_external_company_unique_rejected(self, db: Session) -> None:
        t1 = _make_tenant("engine-dup-ext-a")
        t2 = _make_tenant("engine-dup-ext-b")
        connection = _make_connection()
        db.add_all([t1, t2, connection])
        db.flush()

        db.add(_make_company(t1.id, connection.id, external_id="2"))
        db.flush()
        db.add(_make_company(t2.id, connection.id, external_id="2"))
        with pytest.raises(IntegrityError):
            db.flush()

    def test_external_id_map_unique_rejected(self, db: Session) -> None:
        tenant = _make_tenant("engine-dup-map")
        db.add(tenant)
        db.flush()

        def _mapping() -> ExternalIdMapModel:
            return ExternalIdMapModel(
                id=str(uuid.uuid4()),
                tenant_id=tenant.id,
                source_system="hayat",
                external_entity_type="order",
                external_entity_id="order-1",
                target_engine="tryton",
                target_entity_type="account.invoice",
                target_external_id="42",
            )

        db.add(_mapping())
        db.flush()
        db.add(_mapping())
        with pytest.raises(IntegrityError):
            db.flush()

    def test_same_external_entity_different_engine_allowed(self, db: Session) -> None:
        tenant = _make_tenant("engine-multi-engine")
        db.add(tenant)
        db.flush()

        db.add_all([
            ExternalIdMapModel(
                id=str(uuid.uuid4()),
                tenant_id=tenant.id,
                source_system="hayat",
                external_entity_type="order",
                external_entity_id="order-1",
                target_engine="tryton",
                target_entity_type="account.invoice",
                target_external_id="42",
            ),
            ExternalIdMapModel(
                id=str(uuid.uuid4()),
                tenant_id=tenant.id,
                source_system="hayat",
                external_entity_type="order",
                external_entity_id="order-1",
                target_engine="other-engine",
                target_entity_type="invoice",
                target_external_id="7",
            ),
        ])
        db.flush()  # No IntegrityError — target_engine differs.


class TestIntegration:
    def test_create_inbox_event_defaults(self, db: Session) -> None:
        event = _make_inbox_event()
        db.add(event)
        db.flush()

        found = db.execute(
            select(InboxEventModel).where(InboxEventModel.id == event.id)
        ).scalar_one()
        assert found.status == "received"
        assert found.attempts == 0
        assert found.schema_version == 1
        assert found.tenant_id is None
        assert found.payload == {"order": 1}
        assert found.received_at is not None

    def test_inbox_event_unique_rejected(self, db: Session) -> None:
        db.add(_make_inbox_event())
        db.flush()
        db.add(_make_inbox_event())
        with pytest.raises(IntegrityError):
            db.flush()

    def test_same_source_event_different_store_allowed(self, db: Session) -> None:
        db.add(_make_inbox_event(source_store_id="store-1"))
        db.flush()
        db.add(_make_inbox_event(source_store_id="store-2"))
        db.flush()  # No IntegrityError — source_store_id differs.

    def test_create_outbox_event_defaults(self, db: Session) -> None:
        tenant = _make_tenant("outbox-test")
        db.add(tenant)
        db.flush()

        event = _make_outbox_event(tenant.id)
        db.add(event)
        db.flush()

        found = db.execute(
            select(OutboxEventModel).where(OutboxEventModel.id == event.id)
        ).scalar_one()
        assert found.status == "pending"
        assert found.attempts == 0
        assert found.payload == {"draft": "draft-1"}

    def test_cursor_unique_rejected(self, db: Session) -> None:
        tenant = _make_tenant("cursor-test")
        db.add(tenant)
        db.flush()

        def _cursor() -> IntegrationCursorModel:
            return IntegrationCursorModel(
                id=str(uuid.uuid4()),
                tenant_id=tenant.id,
                source_system="hayat",
                source_store_id="store-1",
                stream="orders",
                cursor_value="2026-11-16T00:00:00Z",
            )

        db.add(_cursor())
        db.flush()
        db.add(_cursor())
        with pytest.raises(IntegrityError):
            db.flush()

    def test_attempt_with_inbox_event_ok(self, db: Session) -> None:
        event = _make_inbox_event()
        db.add(event)
        db.flush()

        attempt = IntegrationAttemptModel(
            id=str(uuid.uuid4()),
            inbox_event_id=event.id,
            attempt_number=1,
            status="succeeded",
        )
        db.add(attempt)
        db.flush()

        db.refresh(event)
        assert len(event.attempts_log) == 1
        assert event.attempts_log[0].status == "succeeded"

    def test_attempt_with_outbox_event_ok(self, db: Session) -> None:
        tenant = _make_tenant("attempt-outbox-test")
        db.add(tenant)
        db.flush()

        event = _make_outbox_event(tenant.id)
        db.add(event)
        db.flush()

        attempt = IntegrationAttemptModel(
            id=str(uuid.uuid4()),
            outbox_event_id=event.id,
            attempt_number=1,
            status="failed",
            error_code="ENGINE_UNAVAILABLE",
            error_message="connection refused",
        )
        db.add(attempt)
        db.flush()

        db.refresh(event)
        assert len(event.attempts_log) == 1
        assert event.attempts_log[0].error_code == "ENGINE_UNAVAILABLE"

    def test_attempt_with_no_event_rejected(self, db: Session) -> None:
        attempt = IntegrationAttemptModel(
            id=str(uuid.uuid4()),
            attempt_number=1,
            status="started",
        )
        db.add(attempt)
        with pytest.raises(IntegrityError):
            db.flush()

    def test_attempt_with_both_events_rejected(self, db: Session) -> None:
        tenant = _make_tenant("attempt-both-test")
        inbox_event = _make_inbox_event()
        outbox_event = _make_outbox_event(tenant.id)
        db.add_all([tenant, inbox_event, outbox_event])
        db.flush()

        attempt = IntegrationAttemptModel(
            id=str(uuid.uuid4()),
            inbox_event_id=inbox_event.id,
            outbox_event_id=outbox_event.id,
            attempt_number=1,
            status="started",
        )
        db.add(attempt)
        with pytest.raises(IntegrityError):
            db.flush()


class TestIdempotency:
    def _make_record(self, **overrides: object) -> IdempotencyRecordModel:
        values: dict[str, object] = {
            "id": str(uuid.uuid4()),
            "installation_id": INSTALLATION_ID,
            "operation": "ledger.draft.create",
            "idempotency_key": "key-1",
            "request_hash": "hash1",
        }
        values.update(overrides)
        return IdempotencyRecordModel(**values)  # type: ignore[arg-type]

    def test_create_record(self, db: Session) -> None:
        record = self._make_record(
            response_status=201,
            response_body={"id": "draft-1"},
            response_reference="journal-drafts/draft-1",
        )
        db.add(record)
        db.flush()

        found = db.execute(
            select(IdempotencyRecordModel).where(IdempotencyRecordModel.id == record.id)
        ).scalar_one()
        assert found.response_status == 201
        assert found.response_body == {"id": "draft-1"}
        assert found.response_reference == "journal-drafts/draft-1"
        assert found.created_at is not None

    def test_duplicate_record_rejected(self, db: Session) -> None:
        db.add(self._make_record())
        db.flush()
        db.add(self._make_record())
        with pytest.raises(IntegrityError):
            db.flush()

    def test_same_key_different_operation_allowed(self, db: Session) -> None:
        db.add(self._make_record(operation="ledger.draft.create"))
        db.flush()
        db.add(self._make_record(operation="ledger.draft.post"))
        db.flush()  # No IntegrityError — operation differs.

    def test_same_key_different_installation_allowed(self, db: Session) -> None:
        db.add(self._make_record(installation_id=INSTALLATION_ID))
        db.flush()
        db.add(self._make_record(installation_id="00000000-0000-0000-0000-000000000002"))
        db.flush()  # No IntegrityError — installation_id differs.
