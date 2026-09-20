"""Tests for PostgreSQL persistence models, migration schema, and tenant isolation.

These tests run against a live PostgreSQL instance and verify:
- Tenant and actor CRUD operations
- Composite unique constraint (tenant_id, username) enforcement
- Foreign key CASCADE deletion
- Tenant isolation: cross-tenant queries return nothing
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Generator

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from hesabyar.infrastructure.persistence.postgres.models import (
    ActorModel,
    Base,
    TenantModel,
)

TEST_DB_URL = os.getenv(
    "HESABYAR_DATABASE_URL",
    "postgresql+psycopg://hesabyar:hesabyar_pass@hesabyar-postgres:5432/hesabyar_test",
)


@pytest.fixture(scope="module")
def engine():
    """Create a SQLAlchemy engine connected to the test database."""
    eng = create_engine(TEST_DB_URL, echo=False)
    # Drop and recreate all tables so tests start with a clean schema.
    Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture()
def db(engine) -> Generator[Session, None, None]:
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


def _make_tenant(slug: str = "acme", name: str = "Acme Corp", is_active: bool = True) -> TenantModel:
    return TenantModel(id=str(uuid.uuid4()), slug=slug, name=name, is_active=is_active)


def _make_actor(
    tenant_id: str,
    username: str = "ali",
    display_name: str = "Ali Ahmadi",
    scopes: str = "hesabyar.read,hesabyar.ledger.draft",
) -> ActorModel:
    return ActorModel(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        username=username,
        display_name=display_name,
        scopes=scopes,
    )


class TestTenantCreation:
    def test_create_tenant(self, db: Session) -> None:
        tenant = _make_tenant()
        db.add(tenant)
        db.flush()

        result = db.execute(select(TenantModel).where(TenantModel.slug == "acme")).scalar_one()
        assert result.name == "Acme Corp"
        assert result.is_active is True
        assert result.created_at is not None
        assert result.updated_at is not None

    def test_tenant_slug_unique(self, db: Session) -> None:
        t1 = _make_tenant(slug="duplicate-slug")
        t2 = _make_tenant(slug="duplicate-slug")
        db.add(t1)
        db.flush()
        db.add(t2)
        with pytest.raises(IntegrityError):
            db.flush()


class TestActorCreation:
    def test_create_actor(self, db: Session) -> None:
        tenant = _make_tenant(slug="actor-test")
        db.add(tenant)
        db.flush()

        actor = _make_actor(tenant_id=tenant.id, username="reza")
        db.add(actor)
        db.flush()

        result = db.execute(
            select(ActorModel).where(ActorModel.username == "reza")
        ).scalar_one()
        assert result.tenant_id == tenant.id
        assert result.display_name == "Ali Ahmadi"
        assert result.is_active is True
        assert result.scope_list == ["hesabyar.read", "hesabyar.ledger.draft"]

    def test_actor_empty_scopes(self, db: Session) -> None:
        tenant = _make_tenant(slug="empty-scopes")
        db.add(tenant)
        db.flush()

        actor = _make_actor(tenant_id=tenant.id, username="no-scope", scopes="")
        db.add(actor)
        db.flush()
        assert actor.scope_list == []


class TestCompositeUniqueConstraint:
    def test_duplicate_username_same_tenant_rejected(self, db: Session) -> None:
        tenant = _make_tenant(slug="unique-test")
        db.add(tenant)
        db.flush()

        a1 = _make_actor(tenant_id=tenant.id, username="duplicate-user")
        a2 = _make_actor(tenant_id=tenant.id, username="duplicate-user")
        db.add(a1)
        db.flush()
        db.add(a2)
        with pytest.raises(IntegrityError):
            db.flush()

    def test_same_username_different_tenant_allowed(self, db: Session) -> None:
        t1 = _make_tenant(slug="tenant-a")
        t2 = _make_tenant(slug="tenant-b")
        db.add_all([t1, t2])
        db.flush()

        a1 = _make_actor(tenant_id=t1.id, username="shared-user")
        a2 = _make_actor(tenant_id=t2.id, username="shared-user")
        db.add_all([a1, a2])
        db.flush()  # No IntegrityError — different tenants.

        actors = db.execute(
            select(ActorModel).where(ActorModel.username == "shared-user")
        ).scalars().all()
        assert len(actors) == 2


class TestForeignKeyCascade:
    def test_delete_tenant_cascades_to_actors(self, db: Session) -> None:
        tenant = _make_tenant(slug="cascade-test")
        db.add(tenant)
        db.flush()

        db.add_all([
            _make_actor(tenant_id=tenant.id, username="user1"),
            _make_actor(tenant_id=tenant.id, username="user2"),
        ])
        db.flush()

        # Verify actors exist.
        count = db.execute(
            select(ActorModel).where(ActorModel.tenant_id == tenant.id)
        ).scalars().all()
        assert len(count) == 2

        # Delete tenant — actors should cascade-delete.
        db.delete(tenant)
        db.flush()

        remaining = db.execute(
            select(ActorModel).where(ActorModel.tenant_id == tenant.id)
        ).scalars().all()
        assert len(remaining) == 0

    def test_actor_with_invalid_tenant_rejected(self, db: Session) -> None:
        fake_tenant_id = str(uuid.uuid4())
        actor = _make_actor(tenant_id=fake_tenant_id, username="orphan")
        db.add(actor)
        with pytest.raises(IntegrityError):
            db.flush()


class TestTenantIsolation:
    def test_cross_tenant_query_returns_nothing(self, db: Session) -> None:
        t1 = _make_tenant(slug="iso-tenant-1")
        t2 = _make_tenant(slug="iso-tenant-2")
        db.add_all([t1, t2])
        db.flush()

        db.add(_make_actor(tenant_id=t1.id, username="t1-user"))
        db.add(_make_actor(tenant_id=t2.id, username="t2-user"))
        db.flush()

        # Query actors for t1 — should not see t2's actor.
        t1_actors = db.execute(
            select(ActorModel).where(ActorModel.tenant_id == t1.id)
        ).scalars().all()
        assert len(t1_actors) == 1
        assert t1_actors[0].username == "t1-user"

        # Query actors for t2 — should not see t1's actor.
        t2_actors = db.execute(
            select(ActorModel).where(ActorModel.tenant_id == t2.id)
        ).scalars().all()
        assert len(t2_actors) == 1
        assert t2_actors[0].username == "t2-user"

    def test_relationship_traversal_respects_tenant(self, db: Session) -> None:
        t1 = _make_tenant(slug="rel-tenant")
        db.add(t1)
        db.flush()

        db.add_all([
            _make_actor(tenant_id=t1.id, username="member1"),
            _make_actor(tenant_id=t1.id, username="member2"),
        ])
        db.flush()

        # Refresh and traverse relationship.
        db.refresh(t1)
        assert len(t1.actors) == 2
        usernames = {a.username for a in t1.actors}
        assert usernames == {"member1", "member2"}
