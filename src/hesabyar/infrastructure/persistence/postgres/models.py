"""SQLAlchemy ORM models for the HesabYar foundation schema.

Models follow the Architecture Authority (ARCHITECTURE.md §7) tenant isolation rules:
every business object belongs to a tenant, and tenant_id exists on all operational tables.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base for all HesabYar SQLAlchemy models."""


class TenantModel(Base):
    """Multi-tenant organization record.

    Maps to the 'tenants' table. Every business object in the system references
    a tenant via foreign key, enforcing data isolation at the database level.
    """

    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    slug: Mapped[str] = mapped_column(
        String(128),
        unique=True,
        nullable=False,
        comment="URL-safe unique identifier for the tenant",
    )
    name: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
        comment="Organization or business legal/trading name",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        comment="Whether the tenant may perform transactions",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Row creation timestamp (UTC)",
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        onupdate=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Last-modified timestamp (UTC)",
    )

    actors: Mapped[list[ActorModel]] = relationship(
        "ActorModel",
        back_populates="tenant",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"TenantModel(id={self.id!r}, slug={self.slug!r}, name={self.name!r})"


class ActorModel(Base):
    """Authenticated user or automated agent operating within a tenant.

    Maps to the 'actors' table. Composite uniqueness on (tenant_id, username)
    ensures no duplicate usernames within a single tenant while allowing the
    same username across different tenants.
    """

    __tablename__ = "actors"
    __table_args__ = (
        UniqueConstraint("tenant_id", "username", name="uq_actors_tenant_username"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Owning tenant FK (CASCADE delete)",
    )
    username: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Login or system identity within the tenant",
    )
    display_name: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
        comment="Human-readable display name",
    )
    scopes: Mapped[str] = mapped_column(
        Text,
        default="",
        nullable=False,
        comment="Comma-separated permission scopes (e.g. hesabyar.read,hesabyar.ledger.draft)",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        comment="Whether the actor is allowed to operate",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Row creation timestamp (UTC)",
    )

    tenant: Mapped[TenantModel] = relationship(
        "TenantModel",
        back_populates="actors",
    )

    @property
    def scope_list(self) -> list[str]:
        """Return scopes as a list, splitting the comma-separated string."""
        return [s.strip() for s in self.scopes.split(",") if s.strip()] if self.scopes else []

    def __repr__(self) -> str:
        return f"ActorModel(id={self.id!r}, tenant_id={self.tenant_id!r}, username={self.username!r})"
