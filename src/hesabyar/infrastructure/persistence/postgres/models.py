"""SQLAlchemy ORM models for the HesabYar foundation schema.

Models follow the Architecture Authority (ARCHITECTURE.md §7) tenant isolation rules:
every business object belongs to a tenant, and tenant_id exists on all operational tables.
"""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB as _PGJSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

#: Postgres JSONB that degrades to plain JSON on SQLite.
#:
#: The restore-drill / reconciliation / backup-status / pre-upgrade
#: unit tests build the full schema on an in-memory SQLite database
#: (``Base.metadata.create_all``), and SQLite's compiler cannot render
#: the Postgres-specific JSONB type (CI run #31: ``CompileError:
#: SQLiteTypeCompiler can't render element of type JSONB``). ``Variant``
#: is the documented fix: on Postgres this renders as JSONB, on SQLite
#: as JSON. All 17 JSONB columns in this module use the bare module
#: name, so they all pick this up with no change at the usage sites.
#: (A ``load_dialect_impl`` subclass does NOT work here: the DDL
#: compiler dispatches on ``__visit_name__`` without consulting it —
#: only ``Variant``'s ``visit_variant`` resolves the dialect impl.)
JSONB = _PGJSONB().with_variant(JSON(), "sqlite")


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
    __table_args__ = (UniqueConstraint("tenant_id", "username", name="uq_actors_tenant_username"),)

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


# ---------------------------------------------------------------------------
# Phase 1 schema (US-102): engine mapping, API access, audit, integration,
# idempotency. Design-eng spec §7.4/§7.5, §10.4, §14.3.
#
# Cross-database rule (spec §7.1, ARCHITECTURE.md §7): HesabYar tables store
# Tryton record ids as plain external-reference columns (VARCHAR). They NEVER
# create SQL foreign keys into Tryton's database. Foreign keys below point
# only at HesabYar-owned tables in this same database.
#
# installation_id is a plain String column (no FK): the installations table
# is out of scope for US-102. A single-installation deployment uses one
# stable installation id; the FK is added when the installations table lands.
# ---------------------------------------------------------------------------


class EngineConnectionModel(Base):
    """Connection to an accounting engine instance (e.g. a Tryton server).

    Maps to the 'engine_connections' table. Deployment-level configuration:
    engine credentials live in the secret manager / environment, never here.
    """

    __tablename__ = "engine_connections"
    __table_args__ = (UniqueConstraint("engine", "database_name", name="uq_engine_connections_engine_database"),)

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    name: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Human-readable connection name",
    )
    engine: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="Engine provider key, e.g. 'tryton'",
    )
    base_url: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        comment="Engine base URL",
    )
    database_name: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Engine database name",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        comment="Whether the connection may be used",
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

    companies: Mapped[list[EngineCompanyModel]] = relationship(
        "EngineCompanyModel",
        back_populates="connection",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"EngineConnectionModel(id={self.id!r}, engine={self.engine!r}, name={self.name!r})"


class EngineCompanyModel(Base):
    """Mapping of a HesabYar tenant to one company in an accounting engine.

    Maps to the 'engine_companies' table. The Tryton company id is stored as
    a plain external reference (external_company_id) — never a cross-database
    foreign key (spec §7.1).
    """

    __tablename__ = "engine_companies"
    __table_args__ = (
        UniqueConstraint("tenant_id", "engine_connection_id", name="uq_engine_companies_tenant_connection"),
        UniqueConstraint(
            "engine_connection_id",
            "external_company_id",
            name="uq_engine_companies_connection_external",
        ),
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
    engine_connection_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("engine_connections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Owning engine connection FK (CASCADE delete)",
    )
    external_company_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Engine company id (e.g. Tryton company.company id) — external reference, not a cross-DB FK",
    )
    company_name: Mapped[str | None] = mapped_column(
        String(256),
        nullable=True,
        comment="Cached engine company display name",
    )
    currency_code: Mapped[str | None] = mapped_column(
        String(3),
        nullable=True,
        comment="Engine company currency code, e.g. 'IRR'",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        comment="Whether the mapping may be used",
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

    tenant: Mapped[TenantModel] = relationship("TenantModel")
    connection: Mapped[EngineConnectionModel] = relationship(
        "EngineConnectionModel",
        back_populates="companies",
    )

    def __repr__(self) -> str:
        return (
            f"EngineCompanyModel(id={self.id!r}, tenant_id={self.tenant_id!r}, "
            f"external_company_id={self.external_company_id!r})"
        )


class ExternalIdMapModel(Base):
    """Mapping from an external entity to its engine record.

    Maps to the 'external_id_map' table. Both the source id and the engine
    record id are plain external references — never SQL foreign keys into
    another database (spec §7.1). Unique per design-eng §10.4.
    """

    __tablename__ = "external_id_map"
    __table_args__ = (
        UniqueConstraint(
            "source_system",
            "external_entity_type",
            "external_entity_id",
            "target_engine",
            name="uq_external_id_map_source_target",
        ),
        Index(
            "ix_external_id_map_target",
            "target_engine",
            "target_entity_type",
            "target_external_id",
        ),
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
    source_system: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Source system key, e.g. 'hayat'",
    )
    external_entity_type: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Source entity type, e.g. 'order', 'customer'",
    )
    external_entity_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Source entity id — external reference",
    )
    target_engine: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="Target engine key, e.g. 'tryton'",
    )
    target_entity_type: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Engine model, e.g. 'account.invoice'",
    )
    target_external_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Engine record id — external reference, not a cross-database FK",
    )
    engine_company_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("engine_companies.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Optional engine company scope FK (SET NULL on delete)",
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

    tenant: Mapped[TenantModel] = relationship("TenantModel")
    engine_company: Mapped[EngineCompanyModel | None] = relationship("EngineCompanyModel")

    def __repr__(self) -> str:
        return (
            f"ExternalIdMapModel(id={self.id!r}, source_system={self.source_system!r}, "
            f"external_entity_id={self.external_entity_id!r}, target_external_id={self.target_external_id!r})"
        )


class ApiClientModel(Base):
    """An external integration client that owns API keys.

    Maps to the 'api_clients' table (spec §7.4 API access group).
    """

    __tablename__ = "api_clients"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_api_clients_tenant_name"),)

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
    name: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Human-readable client name",
    )
    description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="What this client integrates",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        comment="Whether the client may authenticate",
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

    tenant: Mapped[TenantModel] = relationship("TenantModel")
    keys: Mapped[list[ApiKeyModel]] = relationship(
        "ApiKeyModel",
        back_populates="client",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"ApiClientModel(id={self.id!r}, tenant_id={self.tenant_id!r}, name={self.name!r})"


class ApiKeyModel(Base):
    """A scoped API key. Only the prefix + secure hash are stored.

    Maps to the 'api_keys' table (spec §7.5). The raw secret is shown exactly
    once at issuance and never stored, logged, or returned. Key generation /
    verification logic lives in US-103; this model is storage only.
    """

    __tablename__ = "api_keys"
    __table_args__ = (UniqueConstraint("key_hash", name="uq_api_keys_key_hash"),)

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    client_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("api_clients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Owning API client FK (CASCADE delete)",
    )
    installation_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        comment="Logical installation/workspace id (spec §7.5); plain value, no FK until the installations table lands",
    )
    name: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Human-readable key name",
    )
    key_prefix: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        index=True,
        comment="Visible key prefix (e.g. first 8 chars); the raw secret is never stored",
    )
    key_hash: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Keyed HMAC-SHA-256 hash of the secret with a server-side pepper (hex); never the raw secret",
    )
    scopes: Mapped[str] = mapped_column(
        Text,
        default="",
        nullable=False,
        comment="Comma-separated permission scopes (e.g. company:read,account:read)",
    )
    engine_company_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("engine_companies.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Optional restriction to one engine company (HesabYar-owned row; SET NULL on delete)",
    )
    created_by_actor_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("actors.id", ondelete="SET NULL"),
        nullable=True,
        comment="Creator actor FK (SET NULL on delete)",
    )
    allowed_ips: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Optional comma-separated CIDR/IP allowlist",
    )
    rate_limit_per_minute: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Optional per-key rate limit (requests per minute)",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Row creation timestamp (UTC)",
    )
    last_used_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Last successful use timestamp (UTC)",
    )
    expires_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Expiry timestamp (UTC); NULL means no expiry",
    )
    revoked_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Revocation timestamp (UTC); NULL means not revoked",
    )
    revoked_reason: Mapped[str | None] = mapped_column(
        String(256),
        nullable=True,
        comment="Why the key was revoked",
    )

    client: Mapped[ApiClientModel] = relationship(
        "ApiClientModel",
        back_populates="keys",
    )
    engine_company: Mapped[EngineCompanyModel | None] = relationship("EngineCompanyModel")
    created_by_actor: Mapped[ActorModel | None] = relationship("ActorModel")

    @property
    def scope_list(self) -> list[str]:
        """Return scopes as a list, splitting the comma-separated string."""
        return [s.strip() for s in self.scopes.split(",") if s.strip()] if self.scopes else []

    def __repr__(self) -> str:
        # Never include key_hash in the repr.
        return f"ApiKeyModel(id={self.id!r}, client_id={self.client_id!r}, key_prefix={self.key_prefix!r})"


class AuditEventModel(Base):
    """An immutable audit record for one actor/service action.

    Maps to the 'audit_events' table (spec §14.3). Never stores secrets:
    no passwords, raw API keys, tokens, or signing keys — only hashes and
    evidence pointers.
    """

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_tenant_occurred", "tenant_id", "occurred_at"),
        Index("ix_audit_events_correlation_id", "correlation_id"),
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
    installation_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        comment="Logical installation/workspace id; plain value, no FK until the installations table lands",
    )
    actor_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("actors.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Acting actor FK (SET NULL on delete); NULL for system events",
    )
    api_client_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("api_clients.id", ondelete="SET NULL"),
        nullable=True,
        comment="Source API client FK (SET NULL on delete); NULL for non-API actions",
    )
    action: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Action performed, e.g. 'ledger.draft.create'",
    )
    resource_type: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Resource type acted on, e.g. 'journal_draft'",
    )
    resource_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="Resource id acted on",
    )
    company_external_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Engine company id — external reference, not a cross-database FK",
    )
    occurred_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Event timestamp (UTC)",
    )
    request_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        index=True,
        comment="Request id that caused the event",
    )
    correlation_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Correlation id across services/events",
    )
    source_system: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Source system key, e.g. 'hayat', 'mcp', 'dashboard'",
    )
    authorization_result: Mapped[str] = mapped_column(
        String(32),
        default="allowed",
        nullable=False,
        comment="Authorization outcome: 'allowed' or 'denied'",
    )
    outcome: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="Event outcome: 'success', 'failure' or 'error'",
    )
    error_code: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Stable error code when the outcome is not success",
    )
    tryton_reference: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="Engine reference for accounting operations, e.g. 'account.move:123' — external reference",
    )
    payload_hash: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="SHA-256 hex of the request payload (tamper evidence)",
    )
    evidence_pointer: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
        comment="Pointer to stored evidence (object storage key / document reference)",
    )
    metadata_before: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Before-change metadata (JSONB)",
    )
    metadata_after: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="After-change metadata (JSONB)",
    )

    tenant: Mapped[TenantModel] = relationship("TenantModel")
    actor: Mapped[ActorModel | None] = relationship("ActorModel")
    api_client: Mapped[ApiClientModel | None] = relationship("ApiClientModel")
    subjects: Mapped[list[AuditEventSubjectModel]] = relationship(
        "AuditEventSubjectModel",
        back_populates="event",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"AuditEventModel(id={self.id!r}, action={self.action!r}, resource_type={self.resource_type!r})"


class AuditEventSubjectModel(Base):
    """A subject (affected entity) of an audit event.

    Maps to the 'audit_event_subjects' table. One audit event may touch
    several subjects (e.g. a company and an invoice).
    """

    __tablename__ = "audit_event_subjects"
    __table_args__ = (
        UniqueConstraint(
            "audit_event_id",
            "subject_type",
            "subject_id",
            name="uq_audit_event_subjects_event_subject",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    audit_event_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("audit_events.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Owning audit event FK (CASCADE delete)",
    )
    subject_type: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Subject type, e.g. 'company', 'invoice'",
    )
    subject_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Subject id — external reference where the subject lives in the engine",
    )
    role: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Subject role in the event, e.g. 'target', 'affected'",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Row creation timestamp (UTC)",
    )

    event: Mapped[AuditEventModel] = relationship(
        "AuditEventModel",
        back_populates="subjects",
    )

    def __repr__(self) -> str:
        return (
            f"AuditEventSubjectModel(id={self.id!r}, subject_type={self.subject_type!r}, "
            f"subject_id={self.subject_id!r})"
        )


class InboxEventModel(Base):
    """A raw external event received for reliable, retry-safe processing.

    Maps to the 'inbox_events' table (spec §10.2/§10.3). The raw envelope is
    persisted in the same transaction as the inbox status, before any
    mapping is resolved — so tenant_id is nullable here: an event may arrive
    before its tenant/company mapping is known. Unique per §10.4.
    """

    __tablename__ = "inbox_events"
    __table_args__ = (
        UniqueConstraint(
            "source_system",
            "source_store_id",
            "source_event_id",
            name="uq_inbox_events_source_event",
        ),
        Index("ix_inbox_events_status_next_attempt", "status", "next_attempt_at"),
        Index("ix_inbox_events_status_received_at", "status", "received_at"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    tenant_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment="Owning tenant FK (CASCADE delete); NULL until tenant mapping resolves",
    )
    installation_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        index=True,
        comment=(
            "Logical installation that ingested the event (migration 0012, US-511); "
            "plain value, no FK until the installations table lands. NULL for events "
            "ingested before the column existed."
        ),
    )
    schema_version: Mapped[int] = mapped_column(
        Integer,
        default=1,
        nullable=False,
        comment="Canonical event schema version",
    )
    source_system: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Source system key, e.g. 'hayat'",
    )
    source_store_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Source store id within the source system",
    )
    source_event_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Source event id — external reference",
    )
    event_type: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Canonical event type, e.g. 'sales.order.completed'",
    )
    occurred_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="When the event occurred in the source system (UTC)",
    )
    received_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="When HesabYar received the event (UTC)",
    )
    company_mapping: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Resolved company mapping (JSONB)",
    )
    currency: Mapped[str | None] = mapped_column(
        String(3),
        nullable=True,
        comment="Event currency code, e.g. 'IRR'",
    )
    external_entity_type: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="Source entity type, e.g. 'order'",
    )
    external_entity_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="Source entity id — external reference",
    )
    correlation_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        index=True,
        comment="Correlation id across services/events",
    )
    idempotency_key: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="Business idempotency key supplied by the source",
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        comment="Raw event envelope (JSONB)",
    )
    payload_hash: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="SHA-256 hex of the canonical raw payload",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        default="received",
        nullable=False,
        comment="Processing status: 'received', 'processing', 'processed', 'failed', 'needs_review' or 'dead_lettered'",
    )
    attempts: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
        comment="Number of processing attempts so far",
    )
    last_attempt_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Last processing attempt timestamp (UTC)",
    )
    next_attempt_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Next scheduled retry timestamp (UTC)",
    )
    last_error: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Last processing error message",
    )
    processed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Successful processing timestamp (UTC)",
    )
    dead_lettered_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Dead-letter timestamp (UTC); set when retries are exhausted, cleared on replay",
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

    tenant: Mapped[TenantModel | None] = relationship("TenantModel")
    attempts_log: Mapped[list[IntegrationAttemptModel]] = relationship(
        "IntegrationAttemptModel",
        back_populates="inbox_event",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return (
            f"InboxEventModel(id={self.id!r}, source_system={self.source_system!r}, "
            f"source_event_id={self.source_event_id!r}, status={self.status!r})"
        )


class OutboxEventModel(Base):
    """An event HesabYar must reliably deliver to an external system.

    Maps to the 'outbox_events' table (spec §10.3 transactional outbox).
    Written in the same transaction as the business change it announces;
    a worker publishes pending rows and marks them published.
    """

    __tablename__ = "outbox_events"
    __table_args__ = (
        Index("ix_outbox_events_status_next_attempt", "status", "next_attempt_at"),
        Index("ix_outbox_events_aggregate", "aggregate_type", "aggregate_id"),
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
    aggregate_type: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Aggregate type the event belongs to, e.g. 'journal_draft'",
    )
    aggregate_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Aggregate id the event belongs to",
    )
    event_type: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Event type, e.g. 'ledger.journal.posted'",
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        comment="Event payload (JSONB)",
    )
    payload_hash: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="SHA-256 hex of the canonical payload (tamper evidence)",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        default="pending",
        nullable=False,
        comment="Delivery status: 'pending', 'published' or 'failed'",
    )
    attempts: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
        comment="Number of delivery attempts so far",
    )
    last_attempt_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Last delivery attempt timestamp (UTC)",
    )
    next_attempt_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Next scheduled retry timestamp (UTC)",
    )
    last_error: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Last delivery error message",
    )
    published_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Successful publication timestamp (UTC)",
    )
    correlation_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Correlation id across services/events",
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

    tenant: Mapped[TenantModel] = relationship("TenantModel")
    attempts_log: Mapped[list[IntegrationAttemptModel]] = relationship(
        "IntegrationAttemptModel",
        back_populates="outbox_event",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return (
            f"OutboxEventModel(id={self.id!r}, event_type={self.event_type!r}, "
            f"aggregate_id={self.aggregate_id!r}, status={self.status!r})"
        )


class IntegrationCursorModel(Base):
    """A sync cursor for polling one external stream.

    Maps to the 'integration_cursors' table. One cursor per
    (tenant, source system, store, stream); the cursor value is opaque to
    HesabYar (offset, token, or timestamp, as the source system defines it).
    """

    __tablename__ = "integration_cursors"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "source_system",
            "source_store_id",
            "stream",
            name="uq_integration_cursors_tenant_stream",
        ),
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
    source_system: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Source system key, e.g. 'hayat'",
    )
    source_store_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Source store id within the source system",
    )
    stream: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Stream name, e.g. 'orders', 'payments'",
    )
    cursor_value: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
        comment="Opaque source-system cursor (offset/token/timestamp)",
    )
    last_synced_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Last successful sync timestamp (UTC)",
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

    tenant: Mapped[TenantModel] = relationship("TenantModel")

    def __repr__(self) -> str:
        return f"IntegrationCursorModel(id={self.id!r}, source_system={self.source_system!r}, stream={self.stream!r})"


class IntegrationAttemptModel(Base):
    """One attempt to process an inbox event or publish an outbox event.

    Maps to the 'integration_attempts' table. Exactly one of inbox_event_id
    / outbox_event_id is set (enforced by a CHECK constraint).
    """

    __tablename__ = "integration_attempts"
    __table_args__ = (
        CheckConstraint(
            "(inbox_event_id IS NOT NULL) <> (outbox_event_id IS NOT NULL)",
            name="ck_integration_attempts_single_event",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    inbox_event_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("inbox_events.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment="Inbox event being processed FK (CASCADE delete); NULL for outbox attempts",
    )
    outbox_event_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("outbox_events.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment="Outbox event being published FK (CASCADE delete); NULL for inbox attempts",
    )
    attempt_number: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="1-based attempt number for the event",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="Attempt status: 'started', 'succeeded' or 'failed'",
    )
    started_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Attempt start timestamp (UTC)",
    )
    finished_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Attempt finish timestamp (UTC)",
    )
    error_code: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Stable error code when the attempt failed",
    )
    error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Failure detail when the attempt failed",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Row creation timestamp (UTC)",
    )

    inbox_event: Mapped[InboxEventModel | None] = relationship(
        "InboxEventModel",
        back_populates="attempts_log",
    )
    outbox_event: Mapped[OutboxEventModel | None] = relationship(
        "OutboxEventModel",
        back_populates="attempts_log",
    )

    def __repr__(self) -> str:
        return (
            f"IntegrationAttemptModel(id={self.id!r}, attempt_number={self.attempt_number!r}, status={self.status!r})"
        )


class IdempotencyRecordModel(Base):
    """A stored idempotency record for one mutation.

    Maps to the 'idempotency_records' table. Unique per design-eng §10.4 on
    (installation_id, operation, idempotency_key). Reusing a key with the
    same request hash replays the stored response; a different request hash
    is a conflict (IDEMPOTENCY_CONFLICT) — never silently treated as the
    same operation.
    """

    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint(
            "installation_id",
            "operation",
            "idempotency_key",
            name="uq_idempotency_records_installation_operation_key",
        ),
        Index("ix_idempotency_records_expires_at", "expires_at"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    installation_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        comment="Logical installation/workspace id; plain value, no FK until the installations table lands",
    )
    operation: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Operation name, e.g. 'ledger.draft.create'",
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Client-supplied idempotency key",
    )
    request_hash: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="SHA-256 hex of the canonical request payload; reuse with a different hash is a conflict",
    )
    response_status: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="HTTP status of the stored response",
    )
    response_body: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Stored response payload for replay (JSONB)",
    )
    response_reference: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
        comment="Pointer to the created resource, e.g. 'companies/<id>'",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Row creation timestamp (UTC)",
    )
    expires_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Record expiry timestamp (UTC); expired records may be purged",
    )

    def __repr__(self) -> str:
        return (
            f"IdempotencyRecordModel(id={self.id!r}, operation={self.operation!r}, "
            f"idempotency_key={self.idempotency_key!r})"
        )


# ---------------------------------------------------------------------------
# Phase 4 schema (US-401): standards registry + validation runs.
# Design-eng spec §7.4 (Standards + Validation groups), §12.1.
#
# Verification statuses follow docs/CURRENT_RULES_1405.md §1 plus
# UNVERIFIED (bundled text present, official source not yet pinned).
# See the seed module for the per-standard labels and the Standard 15
# gate (BLOCKED_SOURCE_GAP for periods beginning 1405/01/01+).
# ---------------------------------------------------------------------------

#: Verification labels (CURRENT_RULES_1405.md §1 + UNVERIFIED extension).
VERIFICATION_STATUSES: tuple[str, ...] = (
    "VERIFIED_TEXT",
    "CORROBORATED_CURRENT",
    "DISCOVERY_ONLY",
    "BLOCKED_SOURCE_GAP",
    "TRANSITIONAL",
    "UNVERIFIED",
)


class StandardSourceModel(Base):
    """One accounting standard in the registry.

    Maps to the 'standard_sources' table. One row per standard of the
    35-standard corpus (``metadata.json``); the standard's revisions
    live in ``standard_revisions``.
    """

    __tablename__ = "standard_sources"
    __table_args__ = (
        UniqueConstraint("standard_code", name="uq_standard_sources_code"),
        UniqueConstraint("slug", name="uq_standard_sources_slug"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    standard_code: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="Plain standard number as a string, e.g. '1', '43'",
    )
    title: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        comment="Persian title from metadata.json",
    )
    title_en: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
        comment="English title from metadata.json",
    )
    ifrs_equivalent: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="IFRS/IAS equivalent, e.g. 'IAS 1', 'IFRS 15'",
    )
    phase: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Corpus phase (1-8) from metadata.json",
    )
    slug: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="URL slug from metadata.json, e.g. '01-presentation'",
    )
    source_authority: Mapped[str | None] = mapped_column(
        String(256),
        nullable=True,
        comment="Issuing authority, e.g. 'Auditing Organization of Iran'",
    )
    source_url: Mapped[str | None] = mapped_column(
        String(1024),
        nullable=True,
        comment="Canonical source document URL (per-standard PDF)",
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

    revisions: Mapped[list[StandardRevisionModel]] = relationship(
        "StandardRevisionModel",
        back_populates="source",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"StandardSourceModel(id={self.id!r}, standard_code={self.standard_code!r}, title={self.title!r})"


class StandardRevisionModel(Base):
    """One revision of an accounting standard, with full §12.1 provenance.

    Maps to the 'standard_revisions' table. ``sha256`` pins the exact
    snapshotted text (``snapshot_uri``); ``verification_status`` is the
    CURRENT_RULES_1405.md label (+ UNVERIFIED). A revision with
    ``verification_status`` BLOCKED_SOURCE_GAP must never be used to
    validate the periods it is blocked for (enforced by the validation
    service, not by the schema alone).
    """

    __tablename__ = "standard_revisions"
    __table_args__ = (
        UniqueConstraint(
            "source_id",
            "revision_id",
            name="uq_standard_revisions_source_revision",
        ),
        CheckConstraint(
            "status IN ('active', 'superseded')",
            name="ck_standard_revisions_status",
        ),
        CheckConstraint(
            "verification_status IN (" + ", ".join(f"'{status}'" for status in VERIFICATION_STATUSES) + ")",
            name="ck_standard_revisions_verification_status",
        ),
        CheckConstraint(
            "(effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from)",
            name="ck_standard_revisions_effective_range",
        ),
        Index("ix_standard_revisions_source_id", "source_id"),
        Index("ix_standard_revisions_verification_status", "verification_status"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    source_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("standard_sources.id", ondelete="CASCADE"),
        nullable=False,
        comment="Owning standard source FK (CASCADE delete)",
    )
    revision_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Revision identifier of the bundled text, e.g. '1397', '1404'",
    )
    title: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        comment="Persian title of this revision",
    )
    published_at: Mapped[datetime.date | None] = mapped_column(
        Date,
        nullable=True,
        comment="Publication date of the revision, when known",
    )
    effective_from: Mapped[datetime.date | None] = mapped_column(
        Date,
        nullable=True,
        comment="First period-begin date the revision applies to",
    )
    effective_to: Mapped[datetime.date | None] = mapped_column(
        Date,
        nullable=True,
        comment="Last period-begin date the revision applies to",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        default="active",
        nullable=False,
        comment="Registry lifecycle state: 'active' or 'superseded'",
    )
    supersedes_revision_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Revision id this revision supersedes, when recorded",
    )
    source_authority: Mapped[str | None] = mapped_column(
        String(256),
        nullable=True,
        comment="Issuing authority for this revision",
    )
    source_url: Mapped[str | None] = mapped_column(
        String(1024),
        nullable=True,
        comment="Source document URL for this revision",
    )
    snapshot_uri: Mapped[str | None] = mapped_column(
        String(1024),
        nullable=True,
        comment="Repo-relative path of the snapshotted text",
    )
    sha256: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="SHA-256 hex of the snapshotted source text bytes",
    )
    verification_status: Mapped[str] = mapped_column(
        String(32),
        default="UNVERIFIED",
        nullable=False,
        comment="Verification label (CURRENT_RULES_1405.md §1 + UNVERIFIED)",
    )
    verified_by: Mapped[str | None] = mapped_column(
        String(256),
        nullable=True,
        comment="Who/what verified this revision",
    )
    verified_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the verification was recorded (UTC)",
    )
    notes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Supersession / applicability notes",
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

    source: Mapped[StandardSourceModel] = relationship(
        "StandardSourceModel",
        back_populates="revisions",
    )
    rules: Mapped[list[StandardRuleModel]] = relationship(
        "StandardRuleModel",
        back_populates="revision",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return (
            f"StandardRevisionModel(id={self.id!r}, revision_id={self.revision_id!r}, "
            f"verification_status={self.verification_status!r})"
        )


class StandardRuleModel(Base):
    """One machine-readable validation rule of a standard revision.

    Maps to the 'standard_rules' table. ``dsl_formula`` is a boolean
    expression in the closed typed DSL (``domain/standards/dsl.py``).
    The check constraint keeps ``is_executable`` and ``dsl_formula``
    consistent: a rule is executable iff it has a formula. Rules without
    a formula (e.g. the CF005 disclosure requirement) are implemented
    but never executed — a run reports them as not executed, never as a
    fabricated PASS.
    """

    __tablename__ = "standard_rules"
    __table_args__ = (
        UniqueConstraint(
            "revision_id",
            "rule_id",
            name="uq_standard_rules_revision_rule",
        ),
        CheckConstraint(
            "rule_type IN ('equation', 'threshold', 'requirement')",
            name="ck_standard_rules_rule_type",
        ),
        CheckConstraint(
            "severity IN ('error', 'warning')",
            name="ck_standard_rules_severity",
        ),
        CheckConstraint(
            "verification_status IN (" + ", ".join(f"'{status}'" for status in VERIFICATION_STATUSES) + ")",
            name="ck_standard_rules_verification_status",
        ),
        CheckConstraint(
            "(is_executable AND dsl_formula IS NOT NULL) OR (NOT is_executable AND dsl_formula IS NULL)",
            name="ck_standard_rules_executable_formula",
        ),
        Index("ix_standard_rules_revision_id", "revision_id"),
        Index("ix_standard_rules_rule_id", "rule_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    revision_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("standard_revisions.id", ondelete="CASCADE"),
        nullable=False,
        comment="Owning standard revision FK (CASCADE delete)",
    )
    rule_id: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="Stable rule identifier, e.g. 'BS001'",
    )
    category: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Rule group from rules.yaml, e.g. 'balance_sheet'",
    )
    description: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Persian rule description from rules.yaml",
    )
    dsl_formula: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Boolean expression in the closed typed DSL (NULL when not executable)",
    )
    rule_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="Rule kind: 'equation', 'threshold' or 'requirement'",
    )
    severity: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="Rule severity: 'error' or 'warning'",
    )
    standard_refs: Mapped[list[str] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Standard paragraph references, e.g. ['1.55']",
    )
    is_executable: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        comment="False when the rule has no DSL formula and the engine cannot evaluate it",
    )
    ruleset_version: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Source ruleset version the rule was ported from, e.g. '1.0.0-legacy'",
    )
    verification_status: Mapped[str] = mapped_column(
        String(32),
        default="UNVERIFIED",
        nullable=False,
        comment="Verification label of the rule (inherited from its revision at seed time)",
    )
    verified_by: Mapped[str | None] = mapped_column(
        String(256),
        nullable=True,
        comment="Who/what verified this rule",
    )
    verified_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the rule verification was recorded (UTC)",
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

    revision: Mapped[StandardRevisionModel] = relationship(
        "StandardRevisionModel",
        back_populates="rules",
    )

    def __repr__(self) -> str:
        return f"StandardRuleModel(id={self.id!r}, rule_id={self.rule_id!r}, rule_type={self.rule_type!r})"


class ValidationRunModel(Base):
    """One deterministic standards-validation run.

    Maps to the 'validation_runs' table. The run records the resolved
    standard codes, the ruleset version, a hash of the canonical input
    data, the outcome counts and a coverage snapshot taken at run time —
    so the run is reproducible and auditable without re-running it.
    Coverage is reported separately per spec §12.1: corpus vs
    implemented vs verified vs executed.
    """

    __tablename__ = "validation_runs"
    __table_args__ = (
        CheckConstraint(
            "overall_outcome IN ('pass', 'fail', 'error', 'not_applicable')",
            name="ck_validation_runs_overall_outcome",
        ),
        CheckConstraint(
            "(period_end IS NULL OR period_start IS NULL OR period_end >= period_start)",
            name="ck_validation_runs_period_range",
        ),
        Index("ix_validation_runs_tenant_id", "tenant_id"),
        Index("ix_validation_runs_tenant_created", "tenant_id", "created_at"),
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
        comment="Owning tenant FK (CASCADE delete)",
    )
    company_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="API company id as supplied (row id or engine id); plain external reference, no FK",
    )
    period_start: Mapped[datetime.date | None] = mapped_column(
        Date,
        nullable=True,
        comment="Start of the validated financial period",
    )
    period_end: Mapped[datetime.date | None] = mapped_column(
        Date,
        nullable=True,
        comment="End of the validated financial period",
    )
    standard_codes: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        comment="Resolved list of standard codes the run covered",
    )
    ruleset_version: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Ruleset version executed, e.g. '1.0.0-legacy'",
    )
    input_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="SHA-256 hex of the canonical supplied financial data",
    )
    overall_outcome: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="Run outcome: 'pass', 'fail', 'error' or 'not_applicable'",
    )
    is_valid: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        comment="False when any ERROR-severity rule failed or errored",
    )
    rules_executed: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
        comment="Rules actually evaluated (PASS/FAIL/ERROR outcomes)",
    )
    rules_passed: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
        comment="Rules with outcome PASS",
    )
    rules_failed: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
        comment="Rules with outcome FAIL",
    )
    rules_not_applicable: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
        comment="Rules with outcome NOT_APPLICABLE",
    )
    rules_errored: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
        comment="Rules with outcome ERROR",
    )
    coverage_corpus_standards: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Coverage snapshot: standards present in the registry at run time",
    )
    coverage_standards_with_rules: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Coverage snapshot: standards with machine-readable rules",
    )
    coverage_rules_implemented: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Coverage snapshot: rules seeded in the registry",
    )
    coverage_rules_verified: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Coverage snapshot: rules whose revision is VERIFIED_TEXT",
    )
    coverage_rules_executed: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Coverage snapshot: rules this run executed",
    )
    not_executed_rules: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Rules of the covered standards that were implemented but not executable",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Row creation timestamp (UTC)",
    )

    results: Mapped[list[ValidationResultModel]] = relationship(
        "ValidationResultModel",
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return (
            f"ValidationRunModel(id={self.id!r}, overall_outcome={self.overall_outcome!r}, "
            f"rules_executed={self.rules_executed!r})"
        )


class ValidationResultModel(Base):
    """One per-rule outcome of a validation run.

    Maps to the 'validation_results' table. ``rule_code``,
    ``standard_code`` and ``revision_id`` are denormalized into the row
    so the result stays readable even if the registry rule is later
    removed (``rule_row_id`` is SET NULL then). ``outcome`` is exactly
    one of PASS / FAIL / NOT_APPLICABLE / ERROR — stored lowercase to
    match the domain ``RuleOutcome`` values.
    """

    __tablename__ = "validation_results"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "rule_code",
            name="uq_validation_results_run_rule",
        ),
        CheckConstraint(
            "outcome IN ('pass', 'fail', 'not_applicable', 'error')",
            name="ck_validation_results_outcome",
        ),
        CheckConstraint(
            "severity IN ('error', 'warning')",
            name="ck_validation_results_severity",
        ),
        Index("ix_validation_results_run_id", "run_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("validation_runs.id", ondelete="CASCADE"),
        nullable=False,
        comment="Owning validation run FK (CASCADE delete)",
    )
    rule_row_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("standard_rules.id", ondelete="SET NULL"),
        nullable=True,
        comment="Registry rule row FK (SET NULL); rule_code below is the stable history",
    )
    rule_code: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="Stable rule identifier, e.g. 'BS001'",
    )
    standard_code: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="Standard number the rule belongs to, denormalized for history",
    )
    revision_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Revision id of the rule executed, denormalized for history",
    )
    outcome: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="Rule outcome: 'pass', 'fail', 'not_applicable' or 'error'",
    )
    severity: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="Rule severity at execution time: 'error' or 'warning'",
    )
    message: Mapped[str] = mapped_column(
        Text,
        default="",
        nullable=False,
        comment="Human-readable outcome message",
    )
    detail: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Extra outcome detail (e.g. evaluation error text)",
    )
    dsl_formula: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="DSL formula actually executed (NULL for NOT_APPLICABLE rows)",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Row creation timestamp (UTC)",
    )

    run: Mapped[ValidationRunModel] = relationship(
        "ValidationRunModel",
        back_populates="results",
    )

    def __repr__(self) -> str:
        return f"ValidationResultModel(id={self.id!r}, rule_code={self.rule_code!r}, outcome={self.outcome!r})"


# ---------------------------------------------------------------------------
# Phase 4 schema (US-402): tax-policy registry.
# Design-eng spec §7.4 (Tax policy group), §12.2.
#
# Rule year, performance year, filing year and effective date are
# separate persisted fields on every version (CURRENT_RULES_1405.md §3:
# the Article 100 ceiling is a 1405 rule about performance year 1404,
# filed in 1405). Annual parameters live here as exact Numerics with a
# value_unit ('percent' or 'irr') — never as timeless constants in code.
# Verification statuses reuse the shared VERIFICATION_STATUSES above;
# only a VERIFIED_TEXT version may be used for production (enforced by
# the decision service, not by the schema alone).
# ---------------------------------------------------------------------------

#: Units of a tax-policy parameter value (see domain ValueUnit).
VALUE_UNITS: tuple[str, ...] = ("percent", "irr")

#: Usage contexts of a recorded tax decision (see domain UsageContext).
USAGE_CONTEXTS: tuple[str, ...] = ("reference", "production")


class TaxSourceModel(Base):
    """One corroborating or official source document in the registry.

    Maps to the 'tax_sources' table. ``sha256`` pins the snapshotted
    bytes once an official snapshot exists; it stays NULL while the
    source is only corroboration (e.g. the Tasnim budget reporting
    behind the 1405 candidates) — which is exactly why those versions
    are CORROBORATED_CURRENT and blocked for production.
    """

    __tablename__ = "tax_sources"
    __table_args__ = (
        UniqueConstraint("source_key", name="uq_tax_sources_key"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    source_key: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Stable source slug, e.g. 'tasnim-budget-1404-11-25'",
    )
    title: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        comment="Title of the source document",
    )
    authority: Mapped[str | None] = mapped_column(
        String(256),
        nullable=True,
        comment="Issuing/reporting authority, e.g. 'Tasnim News Agency'",
    )
    source_url: Mapped[str | None] = mapped_column(
        String(1024),
        nullable=True,
        comment="Canonical source document URL",
    )
    snapshot_uri: Mapped[str | None] = mapped_column(
        String(1024),
        nullable=True,
        comment="Repo-relative path of the snapshotted text (NULL until an official snapshot is pinned)",
    )
    sha256: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="SHA-256 hex of the snapshotted source bytes (NULL until pinned)",
    )
    notes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Notes, e.g. that the source is corroboration only, not the official text",
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

    versions: Mapped[list[TaxPolicyVersionModel]] = relationship(
        "TaxPolicyVersionModel",
        back_populates="source",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"TaxSourceModel(id={self.id!r}, source_key={self.source_key!r})"


class TaxPolicyVersionModel(Base):
    """One versioned tax rule with its annual parameter.

    Maps to the 'tax_policy_versions' table. ``rule_year``,
    ``performance_year`` and ``filing_year`` are separate columns (spec
    §12.2); ``parameter_value`` is an exact Numeric whose meaning comes
    from ``value_unit``. ``corroboration_urls`` is source metadata (a
    JSONB list of URLs, like ``standard_refs`` on standard_rules), not
    queryable business data.
    """

    __tablename__ = "tax_policy_versions"
    __table_args__ = (
        UniqueConstraint(
            "rule_key",
            "version_id",
            name="uq_tax_policy_versions_rule_version",
        ),
        CheckConstraint(
            "status IN ('active', 'superseded')",
            name="ck_tax_policy_versions_status",
        ),
        CheckConstraint(
            "verification_status IN (" + ", ".join(f"'{status}'" for status in VERIFICATION_STATUSES) + ")",
            name="ck_tax_policy_versions_verification_status",
        ),
        CheckConstraint(
            "value_unit IN (" + ", ".join(f"'{unit}'" for unit in VALUE_UNITS) + ")",
            name="ck_tax_policy_versions_value_unit",
        ),
        CheckConstraint(
            "(effective_to IS NULL OR effective_to >= effective_from)",
            name="ck_tax_policy_versions_effective_range",
        ),
        Index("ix_tax_policy_versions_source_id", "source_id"),
        Index("ix_tax_policy_versions_rule_key", "rule_key"),
        Index("ix_tax_policy_versions_verification_status", "verification_status"),
        Index("ix_tax_policy_versions_effective_from", "effective_from"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    source_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tax_sources.id", ondelete="CASCADE"),
        nullable=False,
        comment="Primary corroborating/official source FK (CASCADE delete)",
    )
    rule_key: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Stable rule identifier, e.g. 'vat_general_rate'",
    )
    rule_name: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        comment="Persian name of the rule",
    )
    version_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Version identifier of the rule, e.g. '1405'",
    )
    parameter_value: Mapped[Decimal] = mapped_column(
        Numeric(20, 4),
        nullable=False,
        comment="Annual parameter value, exact (percent figures as written: 10 means 10%)",
    )
    value_unit: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="Unit of parameter_value: 'percent' or 'irr'",
    )
    rule_year: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Jalali year of the rule (the year the parameter belongs to)",
    )
    performance_year: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Jalali performance year the rule governs",
    )
    filing_year: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Jalali year the rule is filed/administered in",
    )
    effective_from: Mapped[datetime.date] = mapped_column(
        Date,
        nullable=False,
        comment="First Gregorian date the version applies to (inclusive)",
    )
    effective_to: Mapped[datetime.date | None] = mapped_column(
        Date,
        nullable=True,
        comment="Last Gregorian date the version applies to (inclusive); NULL means open-ended",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        default="active",
        nullable=False,
        comment="Registry lifecycle state: 'active' or 'superseded'",
    )
    verification_status: Mapped[str] = mapped_column(
        String(32),
        default="UNVERIFIED",
        nullable=False,
        comment="Verification label (CURRENT_RULES_1405.md §1 + UNVERIFIED)",
    )
    verified_by: Mapped[str | None] = mapped_column(
        String(256),
        nullable=True,
        comment="Who/what verified this version",
    )
    verified_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the verification was recorded (UTC)",
    )
    source_authority: Mapped[str | None] = mapped_column(
        String(256),
        nullable=True,
        comment="Issuing authority for this version",
    )
    source_url: Mapped[str | None] = mapped_column(
        String(1024),
        nullable=True,
        comment="Source document URL for this version",
    )
    snapshot_uri: Mapped[str | None] = mapped_column(
        String(1024),
        nullable=True,
        comment="Repo-relative path of the snapshotted text",
    )
    sha256: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="SHA-256 hex of the snapshotted source text bytes",
    )
    corroboration_urls: Mapped[list[str] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Corroborating source URLs for this version (source metadata, not queryable business data)",
    )
    notes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Scope notes, e.g. the Article 100 performance-1404 distinction",
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

    source: Mapped[TaxSourceModel] = relationship(
        "TaxSourceModel",
        back_populates="versions",
    )
    decisions: Mapped[list[TaxDecisionModel]] = relationship(
        "TaxDecisionModel",
        back_populates="policy_version",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return (
            f"TaxPolicyVersionModel(id={self.id!r}, rule_key={self.rule_key!r}, "
            f"version_id={self.version_id!r}, verification_status={self.verification_status!r})"
        )


class TaxDecisionModel(Base):
    """One recorded tax-policy resolution for a date + company + rule.

    Maps to the 'tax_decisions' table. The resolving version's
    ``version_id``, ``parameter_value``, ``value_unit``,
    ``verification_status`` and ``source_key`` are denormalized into
    the row so the decision stays auditable even if the version row is
    later deleted (``policy_version_id`` is SET NULL then).
    """

    __tablename__ = "tax_decisions"
    __table_args__ = (
        CheckConstraint(
            "usage_context IN (" + ", ".join(f"'{context}'" for context in USAGE_CONTEXTS) + ")",
            name="ck_tax_decisions_usage_context",
        ),
        CheckConstraint(
            "value_unit IN (" + ", ".join(f"'{unit}'" for unit in VALUE_UNITS) + ")",
            name="ck_tax_decisions_value_unit",
        ),
        CheckConstraint(
            "verification_status IN (" + ", ".join(f"'{status}'" for status in VERIFICATION_STATUSES) + ")",
            name="ck_tax_decisions_verification_status",
        ),
        Index("ix_tax_decisions_tenant_id", "tenant_id"),
        Index("ix_tax_decisions_tenant_created", "tenant_id", "created_at"),
        Index("ix_tax_decisions_rule_key", "rule_key"),
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
        comment="Owning tenant FK (CASCADE delete)",
    )
    company_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="API company id as supplied (row id or engine id); plain external reference, no FK",
    )
    rule_key: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Stable rule identifier that was resolved",
    )
    decision_date: Mapped[datetime.date] = mapped_column(
        Date,
        nullable=False,
        comment="Date the policy was resolved for (effective dates are inclusive)",
    )
    usage_context: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="Why the decision was resolved: 'reference' or 'production'",
    )
    policy_version_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("tax_policy_versions.id", ondelete="SET NULL"),
        nullable=True,
        comment="Resolving version row FK (SET NULL); the denormalized fields below are the stable history",
    )
    version_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Version id of the resolving version, denormalized for history",
    )
    parameter_value: Mapped[Decimal] = mapped_column(
        Numeric(20, 4),
        nullable=False,
        comment="Parameter value of the resolving version, denormalized for history",
    )
    value_unit: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="Unit of parameter_value, denormalized: 'percent' or 'irr'",
    )
    verification_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="Verification label of the resolving version at decision time",
    )
    source_key: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Source key of the resolving version, denormalized for history",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Row creation timestamp (UTC)",
    )

    policy_version: Mapped[TaxPolicyVersionModel | None] = relationship(
        "TaxPolicyVersionModel",
        back_populates="decisions",
    )

    def __repr__(self) -> str:
        return (
            f"TaxDecisionModel(id={self.id!r}, rule_key={self.rule_key!r}, "
            f"decision_date={self.decision_date!r}, usage_context={self.usage_context!r})"
        )

# Phase 4 schema (US-404): Moadian test profiles + simulator records.
# Design-eng spec §7.4 (Moadian group), §12.3 (Moadian boundary).
#
# Profiles carry only a key *reference* (env-var name / secret-manager
# path) and the public-key fingerprint — never key material. Submissions
# move draft → approved → queued → submitted → accepted/rejected/failed;
# queueing requires a recorded human approval, and production queueing
# is refused until the production connector exists (the simulator is
# the only submission target in this phase).
# ---------------------------------------------------------------------------

#: Moadian environments: test (simulator) or production (not connected yet).
MOADIAN_ENVIRONMENTS: tuple[str, ...] = ("test", "production")

#: Sender modes (instruction §"آدرس API ها"): self-tsp / tsp.
SENDER_MODES: tuple[str, ...] = ("self-tsp", "tsp")

#: Document lifecycle states.
DOCUMENT_STATUSES: tuple[str, ...] = ("draft", "submitted", "accepted", "rejected")

#: Submission lifecycle states.
SUBMISSION_STATUSES: tuple[str, ...] = (
    "draft",
    "approved",
    "queued",
    "submitted",
    "accepted",
    "rejected",
    "failed",
)

#: Simulator fixture outcomes.
SIMULATION_SCENARIOS: tuple[str, ...] = ("accept", "reject")

#: Official inquiry statuses (instruction: PENDING, FAILED; public SDK
#: corroboration: SUCCESS, IN_PROGRESS, NOT_FOUND, TIMEOUT).
OFFICIAL_STATUSES: tuple[str, ...] = (
    "PENDING",
    "SUCCESS",
    "FAILED",
    "IN_PROGRESS",
    "NOT_FOUND",
    "TIMEOUT",
)

#: Status-event evidence sources.
STATUS_EVENT_SOURCES: tuple[str, ...] = ("simulator", "operator", "system")


class MoadianProfileModel(Base):
    """A versioned Moadian protocol profile (one fiscal memory).

    Maps to the 'moadian_profiles' table. ``key_reference`` names where
    the signing key lives — an env-var name for test keys, a
    secret-manager path for production keys — and is a reference only,
    never key material. ``key_fingerprint`` is the SHA-256 hex of the
    *public* key. ``api_base_url`` is recorded profile metadata; no
    code in this phase performs HTTP calls against it.
    """

    __tablename__ = "moadian_profiles"
    __table_args__ = (
        UniqueConstraint("tenant_id", "slug", name="uq_moadian_profiles_tenant_slug"),
        CheckConstraint(
            "environment IN (" + ", ".join(f"'{value}'" for value in MOADIAN_ENVIRONMENTS) + ")",
            name="ck_moadian_profiles_environment",
        ),
        CheckConstraint(
            "sender_mode IN (" + ", ".join(f"'{value}'" for value in SENDER_MODES) + ")",
            name="ck_moadian_profiles_sender_mode",
        ),
        CheckConstraint(
            "verification_status IN (" + ", ".join(f"'{status}'" for status in VERIFICATION_STATUSES) + ")",
            name="ck_moadian_profiles_verification_status",
        ),
        CheckConstraint(
            "(fiscal_id IS NULL OR length(fiscal_id) = 6)",
            name="ck_moadian_profiles_fiscal_id_length",
        ),
        CheckConstraint(
            "(economic_code IS NULL OR length(economic_code) IN (10, 14))",
            name="ck_moadian_profiles_economic_code_length",
        ),
        Index("ix_moadian_profiles_tenant_id", "tenant_id"),
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
        comment="Owning tenant FK (CASCADE delete)",
    )
    name: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
        comment="Human-readable profile name",
    )
    slug: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="URL-safe profile key, unique per tenant",
    )
    environment: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="Target environment: 'test' (simulator) or 'production' (connector not built yet)",
    )
    sender_mode: Mapped[str] = mapped_column(
        String(16),
        default="self-tsp",
        nullable=False,
        comment="Who sends: 'self-tsp' (taxpayer directly) or 'tsp' (trusted company)",
    )
    fiscal_id: Mapped[str | None] = mapped_column(
        String(6),
        nullable=True,
        comment="Fiscal memory unique id (6 characters)",
    )
    economic_code: Mapped[str | None] = mapped_column(
        String(14),
        nullable=True,
        comment="Seller economic code (10 digits per Table 7, or 14 for the new economic code)",
    )
    packet_type: Mapped[str] = mapped_column(
        String(32),
        default="INVOICE.V01",
        nullable=False,
        comment="Packet type, e.g. 'INVOICE.V01'",
    )
    api_base_url: Mapped[str] = mapped_column(
        String(512),
        default="https://tp.tax.gov.ir/req/",
        nullable=False,
        comment="Official API base URL (profile metadata only — no code in this phase calls it)",
    )
    protocol_version: Mapped[str] = mapped_column(
        String(64),
        default="1401-07-26",
        nullable=False,
        comment="Version of the official technical instruction this profile was modelled from",
    )
    source_authority: Mapped[str | None] = mapped_column(
        String(256),
        nullable=True,
        comment="Issuing authority, e.g. 'Iranian National Tax Administration (INTA)'",
    )
    source_url: Mapped[str | None] = mapped_column(
        String(1024),
        nullable=True,
        comment="Official source document URL (INTA portal)",
    )
    snapshot_uri: Mapped[str | None] = mapped_column(
        String(1024),
        nullable=True,
        comment="Repo-relative path of the snapshotted source, when pinned",
    )
    verification_status: Mapped[str] = mapped_column(
        String(32),
        default="UNVERIFIED",
        nullable=False,
        comment="Verification label (CURRENT_RULES_1405.md §1 + UNVERIFIED)",
    )
    key_reference: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
        comment="Where the signing key lives (env-var name / secret-manager path) — "
        "a reference only, NEVER key material",
    )
    key_fingerprint: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="SHA-256 hex of the public key, when known (never the private key)",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        comment="Whether the profile may be used for new submissions",
    )
    notes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Free-form notes, e.g. documented assumptions about the profile",
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

    documents: Mapped[list[MoadianDocumentModel]] = relationship(
        "MoadianDocumentModel",
        back_populates="profile",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return (
            f"MoadianProfileModel(id={self.id!r}, slug={self.slug!r}, "
            f"environment={self.environment!r})"
        )


class MoadianDocumentModel(Base):
    """One invoice prepared for Moadian submission.

    Maps to the 'moadian_documents' table. ``payload`` is the Moadian
    invoice JSON (header/body/payment per instruction Table 7);
    ``payload_hash`` pins its canonical bytes. ``invoice_id`` is the
    HesabYar/Tryton invoice this document was built from — a plain
    external reference, never a cross-database FK (spec §7.1).
    """

    __tablename__ = "moadian_documents"
    __table_args__ = (
        UniqueConstraint("tenant_id", "taxid", name="uq_moadian_documents_tenant_taxid"),
        CheckConstraint(
            "status IN (" + ", ".join(f"'{value}'" for value in DOCUMENT_STATUSES) + ")",
            name="ck_moadian_documents_status",
        ),
        CheckConstraint(
            "invoice_type BETWEEN 1 AND 3",
            name="ck_moadian_documents_invoice_type",
        ),
        CheckConstraint(
            "invoice_pattern BETWEEN 1 AND 6",
            name="ck_moadian_documents_invoice_pattern",
        ),
        CheckConstraint(
            "invoice_subject BETWEEN 1 AND 4",
            name="ck_moadian_documents_invoice_subject",
        ),
        CheckConstraint(
            "(taxid IS NULL OR length(taxid) = 22)",
            name="ck_moadian_documents_taxid_length",
        ),
        CheckConstraint(
            "(serial IS NULL OR length(serial) = 10)",
            name="ck_moadian_documents_serial_length",
        ),
        Index("ix_moadian_documents_tenant_id", "tenant_id"),
        Index("ix_moadian_documents_profile_id", "profile_id"),
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
        comment="Owning tenant FK (CASCADE delete)",
    )
    profile_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("moadian_profiles.id", ondelete="CASCADE"),
        nullable=False,
        comment="Owning Moadian profile FK (CASCADE delete)",
    )
    invoice_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="HesabYar/Tryton invoice this document was built from — external reference, no cross-DB FK",
    )
    taxid: Mapped[str | None] = mapped_column(
        String(22),
        nullable=True,
        comment="Unique tax number (22 characters), when assigned",
    )
    serial: Mapped[str | None] = mapped_column(
        String(10),
        nullable=True,
        comment="Invoice serial (inno, 10 characters), when assigned",
    )
    invoice_type: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Invoice type (inty, 1-3)",
    )
    invoice_pattern: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Invoice pattern (inp, 1-6)",
    )
    invoice_subject: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Invoice subject (ins, 1-4)",
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        comment="Moadian invoice JSON (header/body/payment per instruction Table 7)",
    )
    payload_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="SHA-256 hex of the canonical invoice payload",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        default="draft",
        nullable=False,
        comment="Document lifecycle: 'draft', 'submitted', 'accepted' or 'rejected'",
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

    profile: Mapped[MoadianProfileModel] = relationship(
        "MoadianProfileModel",
        back_populates="documents",
    )
    submissions: Mapped[list[MoadianSubmissionModel]] = relationship(
        "MoadianSubmissionModel",
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return (
            f"MoadianDocumentModel(id={self.id!r}, taxid={self.taxid!r}, "
            f"status={self.status!r})"
        )


class MoadianSubmissionModel(Base):
    """One submission attempt of a document to Moadian.

    Maps to the 'moadian_submissions' table. ``uid`` is the
    client-generated unique submission id (uuid3, sent unencrypted);
    ``reference_number`` is the server-issued receipt id. The
    ``approved_by`` / ``approved_at`` / ``approval_note`` fields are
    the recorded human approval (spec §19 rule 11) — the check
    constraint keeps approval state and lifecycle consistent: a
    submission is a draft iff it has no approval. ``request_hash`` /
    ``response_hash`` are the §12.3 evidence hashes.
    """

    __tablename__ = "moadian_submissions"
    __table_args__ = (
        UniqueConstraint("uid", name="uq_moadian_submissions_uid"),
        CheckConstraint(
            "status IN (" + ", ".join(f"'{value}'" for value in SUBMISSION_STATUSES) + ")",
            name="ck_moadian_submissions_status",
        ),
        CheckConstraint(
            "environment IN (" + ", ".join(f"'{value}'" for value in MOADIAN_ENVIRONMENTS) + ")",
            name="ck_moadian_submissions_environment",
        ),
        CheckConstraint(
            "scenario IN (" + ", ".join(f"'{value}'" for value in SIMULATION_SCENARIOS) + ")",
            name="ck_moadian_submissions_scenario",
        ),
        CheckConstraint(
            "(status = 'draft') = (approved_at IS NULL)",
            name="ck_moadian_submissions_approval_state",
        ),
        Index("ix_moadian_submissions_tenant_id", "tenant_id"),
        Index("ix_moadian_submissions_document_id", "document_id"),
        Index("ix_moadian_submissions_status", "tenant_id", "status"),
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
        comment="Owning tenant FK (CASCADE delete)",
    )
    document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("moadian_documents.id", ondelete="CASCADE"),
        nullable=False,
        comment="Owning document FK (CASCADE delete)",
    )
    profile_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("moadian_profiles.id", ondelete="CASCADE"),
        nullable=False,
        comment="Profile used for this submission FK (CASCADE delete; denormalized from the document)",
    )
    uid: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        comment="Unique submission id (uuid3, client-generated, sent unencrypted)",
    )
    reference_number: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        comment="Unique receipt id (uuid) issued by the (simulated) server on acceptance",
    )
    correlation_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Correlation id across the submission, outbox event and status events",
    )
    environment: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="Target environment denormalized from the profile: 'test' or 'production'",
    )
    scenario: Mapped[str] = mapped_column(
        String(16),
        default="accept",
        nullable=False,
        comment="Simulator fixture outcome: 'accept' or 'reject' (test environment only)",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        default="draft",
        nullable=False,
        comment="Submission lifecycle: draft → approved → queued → submitted → accepted/rejected/failed",
    )
    retry: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        comment="True when this is a resubmission of a document that already has a submission",
    )
    approved_by: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="API client id that approved the submission (the recorded human approval)",
    )
    approved_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the submission was approved (UTC); NULL until approved",
    )
    approval_note: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Optional note recorded with the approval",
    )
    request_hash: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="SHA-256 hex of the canonical submitted packet",
    )
    response_hash: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="SHA-256 hex of the canonical (simulated) server response",
    )
    request_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Submitted packet envelope (packet + simulator marker + scenario)",
    )
    response_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Latest (simulated) server response (submit receipt or inquiry item)",
    )
    outbox_event_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("outbox_events.id", ondelete="SET NULL"),
        nullable=True,
        comment="Outbox event that dispatched this submission (SET NULL on delete)",
    )
    last_error_code: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Stable error code of the last failure, when any",
    )
    last_error_detail: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Detail of the last failure, when any",
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

    document: Mapped[MoadianDocumentModel] = relationship(
        "MoadianDocumentModel",
        back_populates="submissions",
    )
    profile: Mapped[MoadianProfileModel] = relationship("MoadianProfileModel")
    status_events: Mapped[list[MoadianStatusEventModel]] = relationship(
        "MoadianStatusEventModel",
        back_populates="submission",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return (
            f"MoadianSubmissionModel(id={self.id!r}, uid={self.uid!r}, "
            f"status={self.status!r})"
        )


class MoadianStatusEventModel(Base):
    """One official status observation for a submission.

    Maps to the 'moadian_status_events' table. ``evidence`` is the
    inquiry response item verbatim (``{uid, referenceNumber, status,
    data, packetType, fiscalId}`` — instruction Table 10), so the
    official status is always traceable to the evidence that reported
    it.
    """

    __tablename__ = "moadian_status_events"
    __table_args__ = (
        CheckConstraint(
            "status IN (" + ", ".join(f"'{value}'" for value in OFFICIAL_STATUSES) + ")",
            name="ck_moadian_status_events_status",
        ),
        CheckConstraint(
            "source IN (" + ", ".join(f"'{value}'" for value in STATUS_EVENT_SOURCES) + ")",
            name="ck_moadian_status_events_source",
        ),
        Index(
            "ix_moadian_status_events_submission_occurred",
            "submission_id",
            "occurred_at",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    submission_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("moadian_submissions.id", ondelete="CASCADE"),
        nullable=False,
        comment="Owning submission FK (CASCADE delete)",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="Official status reported: PENDING, SUCCESS, FAILED, IN_PROGRESS, NOT_FOUND or TIMEOUT",
    )
    source: Mapped[str] = mapped_column(
        String(32),
        default="simulator",
        nullable=False,
        comment="Where the evidence came from: 'simulator', 'operator' or 'system'",
    )
    evidence: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Inquiry response item verbatim ({uid, referenceNumber, status, data, packetType, fiscalId})",
    )
    correlation_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Correlation id of the submission this event belongs to",
    )
    occurred_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="When the status was observed (UTC)",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Row creation timestamp (UTC)",
    )

    submission: Mapped[MoadianSubmissionModel] = relationship(
        "MoadianSubmissionModel",
        back_populates="status_events",
    )

    def __repr__(self) -> str:
        return f"MoadianStatusEventModel(id={self.id!r}, status={self.status!r}, source={self.source!r})"


class ReportSnapshotModel(Base):
    """A persisted snapshot of one live trial balance (US-403).

    Maps to the 'report_snapshots' table. The snapshot is the
    reproducible record of the trial balance a validation run was built
    on: the trial-balance document itself (``payload``), its SHA-256
    (``payload_hash`` — equal to the validation run's ``input_hash``),
    the source endpoint the figures came from, the ruleset version they
    were validated against and the as-of date. ``validation_run_id``
    joins the snapshot to its run; it is unique — one snapshot per run.
    """

    __tablename__ = "report_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "validation_run_id",
            name="uq_report_snapshots_validation_run",
        ),
        CheckConstraint(
            "(period_end IS NULL OR period_start IS NULL OR period_end >= period_start)",
            name="ck_report_snapshots_period_range",
        ),
        CheckConstraint(
            "total_debit >= 0 AND total_credit >= 0",
            name="ck_report_snapshots_totals_non_negative",
        ),
        Index("ix_report_snapshots_tenant_id", "tenant_id"),
        Index("ix_report_snapshots_tenant_created", "tenant_id", "created_at"),
        Index("ix_report_snapshots_validation_run_id", "validation_run_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key (snapshot id)",
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Owning tenant FK (CASCADE delete)",
    )
    company_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="API company id as supplied (row id or engine id); plain external reference, no FK",
    )
    validation_run_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("validation_runs.id", ondelete="CASCADE"),
        nullable=True,
        comment="Validation run this snapshot was built for (CASCADE delete)",
    )
    period_start: Mapped[datetime.date | None] = mapped_column(
        Date,
        nullable=True,
        comment="Start of the trial-balance period",
    )
    period_end: Mapped[datetime.date | None] = mapped_column(
        Date,
        nullable=True,
        comment="End of the trial-balance period",
    )
    as_of_date: Mapped[datetime.date] = mapped_column(
        Date,
        nullable=False,
        comment="As-of date of the snapshot (the trial-balance period end)",
    )
    source_endpoint: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Engine read the figures came from, e.g. 'account.move.line (Tryton REST)'",
    )
    ruleset_version: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Ruleset version the snapshot was validated against",
    )
    payload_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="SHA-256 hex of the canonical trial-balance document (equals the run's input_hash)",
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        comment="Trial-balance document: per-account debit/credit/balance + totals",
    )
    balanced: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        comment="True when total debits equal total credits over the posted moves",
    )
    total_debit: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="Total debits over posted moves in the period (IRR)",
    )
    total_credit: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="Total credits over posted moves in the period (IRR)",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Row creation timestamp (UTC)",
    )

    def __repr__(self) -> str:
        return (
            f"ReportSnapshotModel(id={self.id!r}, "
            f"validation_run_id={self.validation_run_id!r}, as_of_date={self.as_of_date!r})"
        )


class AutomationDecisionLogModel(Base):
    """One automation-decision audit-log entry (US-501, phased design §6.7).

    Maps to the 'automation_decision_log' table. Append-only: every
    auto-post/draft/needs-review/dead-letter/replay decision records
    the event, the policy version, the mapping version, the verdict
    of every gate (``gate_results`` JSONB), the final decision, the
    time and (for human actions) the actor. Decisions are never
    edited or deleted, so an auditor can always reconstruct what
    the platform decided and why.
    """

    __tablename__ = "automation_decision_log"
    __table_args__ = (
        Index("ix_automation_decision_log_tenant_id", "tenant_id"),
        Index("ix_automation_decision_log_inbox_event_id", "inbox_event_id"),
        Index("ix_automation_decision_log_decision", "decision"),
        Index("ix_automation_decision_log_decided_at", "decided_at"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    tenant_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
        comment="Owning tenant FK (CASCADE delete); NULL until tenant mapping resolves",
    )
    inbox_event_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("inbox_events.id", ondelete="CASCADE"),
        nullable=False,
        comment="Inbox event the decision was made for FK (CASCADE delete)",
    )
    event_type: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Canonical event type, e.g. 'sales.order.completed'",
    )
    decision: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="Decision: 'draft', 'auto_post', 'needs_review', 'dead_letter' or 'replayed'",
    )
    policy_version: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Policy version the decision used ('n/a' when no gate ran)",
    )
    mapping_version: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment=(
            "Mapping version the decision used (NULL when no mapping ran); "
            "a comma-joined list of kind:source_key:vN entries, unbounded "
            "because one event resolves several mappings (US-511)"
        ),
    )
    gate_results: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        comment="Per-gate verdicts: [{name, passed, reason}] (JSONB)",
    )
    decided_by: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Decision actor: 'system' or the API client id of a human",
    )
    decided_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Decision timestamp (UTC)",
    )
    correlation_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Correlation id across services/events",
    )
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        comment="Decision details: reason, refs, error codes (JSONB)",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Row creation timestamp (UTC)",
    )

    tenant: Mapped[TenantModel | None] = relationship("TenantModel")
    inbox_event: Mapped[InboxEventModel] = relationship("InboxEventModel")

    def __repr__(self) -> str:
        return (
            f"AutomationDecisionLogModel(id={self.id!r}, "
            f"inbox_event_id={self.inbox_event_id!r}, decision={self.decision!r})"
        )


class ConnectorStoreSettingsModel(Base):
    """Per-store connector settings (US-501, ADR 0003 decision 4).

    Maps to the 'connector_store_settings' table. One row per
    (source_system, source_store_id): whether the store's prices are
    in Toman (decides the ×10 conversion at the connector boundary)
    and the store's currency. The store registry (F3) owns the full
    store record (API keys, webhook secrets); this narrow table owns
    only the currency-conversion setting, so the two never collide.
    """

    __tablename__ = "connector_store_settings"
    __table_args__ = (
        UniqueConstraint(
            "source_system",
            "source_store_id",
            name="uq_connector_store_settings_source_store",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    source_system: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Source system key, e.g. 'hayat'",
    )
    source_store_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Source store id within the source system",
    )
    prices_in_toman: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        comment="True when the store's prices are in Toman (convert ×10 at the boundary)",
    )
    currency: Mapped[str] = mapped_column(
        String(3),
        default="IRR",
        nullable=False,
        comment="Store currency code, e.g. 'IRR'",
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

    def __repr__(self) -> str:
        return (
            f"ConnectorStoreSettingsModel(source_system={self.source_system!r}, "
            f"source_store_id={self.source_store_id!r}, "
            f"prices_in_toman={self.prices_in_toman!r})"
        )


# ---------------------------------------------------------------------------
# Integration registries + policy gate (US-503, Phase 5 F3 — migration 0010)
# ---------------------------------------------------------------------------


class PaymentChannelModel(Base):
    """One payment channel in an installation's registry.

    Maps to the 'payment_channels' table. ``channel_key`` is the stable
    slug — data, never an enum (ADR 0003 item 12). ``is_builtin`` marks
    the rows the seed creates; a built-in channel is born disabled
    (``enabled=False``, ``status='setup'``) until the operator enables
    it and attaches a contract.
    """

    __tablename__ = "payment_channels"
    __table_args__ = (
        UniqueConstraint(
            "installation_id", "channel_key", name="uq_payment_channels_installation_key"
        ),
        Index("ix_payment_channels_installation", "installation_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    installation_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        comment="Installation id (from the authenticated API key); the registry is per-installation",
    )
    channel_key: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Stable channel slug, e.g. 'zarinpal' (data — never an enum)",
    )
    display_name_fa: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
        comment="Persian display name, e.g. 'زرین‌پال'",
    )
    channel_type: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Channel kind as a free string: 'gateway' | 'bnpl' | 'marketplace' | 'custom'",
    )
    is_builtin: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="True for the rows the built-in seed creates",
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="Operator toggle: a disabled channel never auto-posts",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="setup",
        comment="Operational status: 'active' | 'disabled' | 'setup' | 'error'",
    )
    merchant_ref: Mapped[str | None] = mapped_column(
        String(256),
        nullable=True,
        comment="Merchant/terminal identifier (non-secret)",
    )
    settlement_iban: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Destination IBAN for settlements (in the merchant's name)",
    )
    environment: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="production",
        comment="Gateway environment: 'stage' | 'production'",
    )
    notes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Operator notes",
    )
    credentials_ciphertext: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment=(
            "Fernet ciphertext of the channel's gateway credential payload "
            "(JSON object: secrets + optional non-secret overrides). NULL for "
            "manual channels with no API. Never exposed through REST."
        ),
    )
    credentials_key_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Id of the encryption key version that produced the ciphertext",
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

    contracts: Mapped[list[GatewayContractModel]] = relationship(
        "GatewayContractModel",
        back_populates="channel",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"PaymentChannelModel(id={self.id!r}, channel_key={self.channel_key!r})"


class GatewayContractModel(Base):
    """One versioned gateway contract (phased design §3.1).

    Maps to the 'gateway_contracts' table. A rate change is a NEW row
    with ``version`` = max + 1; the previous active row is superseded
    (status + ``valid_to`` only) and its figures are never rewritten.
    ``tranche_legs`` is JSONB: a list of {month_offset, payout_day,
    percent} with percent stored as a string to keep the Decimal exact.
    """

    __tablename__ = "gateway_contracts"
    __table_args__ = (
        UniqueConstraint("channel_id", "version", name="uq_gateway_contracts_channel_version"),
        Index("ix_gateway_contracts_channel_status", "channel_id", "status"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    channel_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("payment_channels.id", ondelete="CASCADE"),
        nullable=False,
        comment="Owning payment channel row",
    )
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Contract version number, incrementing per channel (1-based)",
    )
    valid_from: Mapped[datetime.date] = mapped_column(
        Date,
        nullable=False,
        comment="First effective date of this version (inclusive)",
    )
    valid_to: Mapped[datetime.date | None] = mapped_column(
        Date,
        nullable=True,
        comment="Last effective date (inclusive); NULL while the version is active",
    )
    fee_percent: Mapped[Decimal] = mapped_column(
        Numeric(8, 4),
        nullable=False,
        comment="User-entered fee percent (10 means 10%) — never hard-coded",
    )
    fee_vat_rate: Mapped[Decimal | None] = mapped_column(
        Numeric(8, 4),
        nullable=True,
        comment="VAT rate on the fee, from the active tax policy (NULL = unknown)",
    )
    fee_vat_claimable: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="Whether fee VAT is claimable as input tax (False until official evidence)",
    )
    fee_timing: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="proportional",
        comment="Fee timing: 'proportional' (per tranche) | 'upfront_first_tranche'",
    )
    fee_accrual_policy: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="at_sale",
        comment="Fee accrual: 'at_sale' (accrual, default) | 'at_settlement' (cash-basis)",
    )
    settlement_model: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="tranches_4",
        comment="Settlement model as a free string, e.g. 'tranches_4' | 'monthly_lump' | 'daily'",
    )
    tranche_legs: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        comment="Settlement schedule: [{month_offset, payout_day, percent}] (percent as string)",
    )
    payout_window_start_day: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="First day of the payout window in the payout month (1..31)",
    )
    payout_window_end_day: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Last day of the payout window (the due date) in the payout month (1..31)",
    )
    payout_calendar: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="jalali",
        comment="Payout calendar: 'jalali' | 'gregorian'",
    )
    working_day_adjustment: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="When True, a due date falling on a holiday moves to the next business day",
    )
    day_lag: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Day-lag settlement models: business days after confirmation (NULL = month-based)",
    )
    grace_days_after_due: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="Grace days after the due date before a tranche counts overdue",
    )
    match_tolerance_irr: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        comment="Amount-matching tolerance in rials",
    )
    tieout_tolerance_irr: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        comment="Settlement tie-out tolerance in rials",
    )
    auto_post_policy: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="draft_only",
        comment="Auto-post policy: 'draft_only' (default) | 'auto_post_within_policy'",
    )
    auto_post_max_amount_irr: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="Auto-post amount cap for this contract in rials (NULL = no contract cap)",
    )
    source_note: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Reference for the contract figures (contract number, signature date, file name)",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="active",
        comment="Version lifecycle: 'active' | 'superseded'",
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

    channel: Mapped[PaymentChannelModel] = relationship(
        "PaymentChannelModel",
        back_populates="contracts",
    )

    def __repr__(self) -> str:
        return (
            f"GatewayContractModel(id={self.id!r}, channel_id={self.channel_id!r}, "
            f"version={self.version!r})"
        )


class IntegrationMappingModel(Base):
    """One versioned mapping in the mapping center (spec §10.5).

    Maps to the 'integration_mappings' table. ``channel_key`` scopes
    the mapping to one channel (empty string = installation-wide).
    A new version supersedes the previous active one; unknown mappings
    fail closed into the review queue, never to a default account.
    """

    __tablename__ = "integration_mappings"
    __table_args__ = (
        UniqueConstraint(
            "installation_id",
            "mapping_kind",
            "channel_key",
            "source_key",
            "version",
            name="uq_integration_mappings_identity_version",
        ),
        Index(
            "ix_integration_mappings_resolve",
            "installation_id",
            "mapping_kind",
            "channel_key",
            "source_key",
            "status",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    installation_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        comment="Installation id (from the authenticated API key)",
    )
    mapping_kind: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Mapping kind: one of the nine spec §10.5 kinds",
    )
    channel_key: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="",
        comment="Owning channel key, or '' for an installation-wide mapping",
    )
    source_key: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        comment="External key (store SKU, gateway slug, revenue-category name, ...)",
    )
    target_type: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Tryton target kind: 'account' | 'journal' | 'party' | 'tax_policy' | 'dimension' | 'company'",
    )
    target_ref: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        comment="Engine reference of the target (e.g. an account code)",
    )
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Mapping version number, incrementing per (kind, channel, source) (1-based)",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="active",
        comment="Version lifecycle: 'active' | 'superseded'",
    )
    suggestion_mode: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="suggest",
        comment="How the rule applies: 'suggest' (confirm first) | 'auto'",
    )
    source: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="manual",
        comment="Mapping origin: 'manual' | 'learned' | 'imported'",
    )
    confidence: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 4),
        nullable=True,
        comment="Confidence 0..1 for learned mappings (NULL for manual ones)",
    )
    notes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Operator notes",
    )
    created_by: Mapped[str | None] = mapped_column(
        String(256),
        nullable=True,
        comment="Actor that created this version (user id or key name)",
    )
    confirmations: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
        comment="Confirmations of the same target (kind, channel, source); Auto needs 3 (US-552)",
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

    def __repr__(self) -> str:
        return (
            f"IntegrationMappingModel(id={self.id!r}, mapping_kind={self.mapping_kind!r}, "
            f"source_key={self.source_key!r}, version={self.version!r})"
        )


class MappingSuggestionModel(Base):
    """A proposed mapping awaiting the operator's decision (§1.4).

    Maps to the 'mapping_suggestions' table. At most one PENDING
    suggestion exists per (installation, kind, channel, source) —
    enforced by the partial unique index
    ``uq_mapping_suggestions_pending``.
    """

    __tablename__ = "mapping_suggestions"
    __table_args__ = (
        Index(
            "uq_mapping_suggestions_pending",
            "installation_id",
            "mapping_kind",
            "channel_key",
            "source_key",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        Index("ix_mapping_suggestions_installation_status", "installation_id", "status"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    installation_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        comment="Installation id (from the authenticated API key)",
    )
    mapping_kind: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Mapping kind: one of the nine spec §10.5 kinds",
    )
    channel_key: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="",
        comment="Owning channel key, or '' for an installation-wide mapping",
    )
    source_key: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        comment="External key the suggestion is about",
    )
    suggested_target_type: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Proposed Tryton target kind",
    )
    suggested_target_ref: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        comment="Proposed engine reference of the target",
    )
    confidence: Mapped[Decimal] = mapped_column(
        Numeric(5, 4),
        nullable=False,
        comment="Suggestion confidence 0..1",
    )
    basis: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
        comment="Why this target was suggested (e.g. 'learned_from_manual_mapping')",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="pending",
        comment="Suggestion lifecycle: 'pending' | 'accepted' | 'rejected'",
    )
    resolved_by: Mapped[str | None] = mapped_column(
        String(256),
        nullable=True,
        comment="Actor that accepted/rejected the suggestion",
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

    def __repr__(self) -> str:
        return (
            f"MappingSuggestionModel(id={self.id!r}, mapping_kind={self.mapping_kind!r}, "
            f"source_key={self.source_key!r}, status={self.status!r})"
        )


class AutoPostPolicyModel(Base):
    """One installation's auto-post policy for one event type (§6.2).

    Maps to the 'auto_post_policies' table. Layer 1 of the gate: an
    event type auto-posts only when a row exists with ``allowed=True``
    AND the installation-level opt-in is on.
    """

    __tablename__ = "auto_post_policies"
    __table_args__ = (
        UniqueConstraint(
            "installation_id", "event_type", name="uq_auto_post_policies_installation_event"
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    installation_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        comment="Installation id (from the authenticated API key)",
    )
    event_type: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Canonical event type, e.g. 'payment.captured'",
    )
    allowed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        comment="Whether this event type may auto-post (explicit opt-in per type)",
    )
    max_amount_irr: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="Layer-2 amount cap for this event type in rials (NULL = no policy cap)",
    )
    require_tieout: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="Whether settlement tie-out must hold before auto-post (settlement events)",
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

    def __repr__(self) -> str:
        return (
            f"AutoPostPolicyModel(id={self.id!r}, event_type={self.event_type!r}, "
            f"allowed={self.allowed!r})"
        )


class BankConnectionModel(Base):
    """One bank connection in an installation's registry (ADR 0003 item 5).

    Maps to the 'bank_connections' table. Per-company: ``company_id``
    is the ``engine_companies`` row id. Credentials are stored
    ENCRYPTED (``credentials_ciphertext``) — never plaintext, and
    never an internet-banking password (the service rejects those).
    Fintech inquiry results are ephemeral and are never stored here.
    """

    __tablename__ = "bank_connections"
    __table_args__ = (
        UniqueConstraint(
            "installation_id",
            "company_id",
            "bank_key",
            name="uq_bank_connections_installation_company_bank",
        ),
        Index("ix_bank_connections_installation_company", "installation_id", "company_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    installation_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        comment="Installation id (from the authenticated API key)",
    )
    company_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("engine_companies.id", ondelete="CASCADE"),
        nullable=False,
        comment="HesabYar engine_companies row id this connection belongs to",
    )
    bank_key: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Stable bank slug, e.g. 'melli' (data — never an enum)",
    )
    display_name: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
        comment="Persian display name, e.g. 'بانک ملی'",
    )
    connection_kind: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Connection kind as a free string: 'manual_import' | 'api_oauth' | 'aggregator' | ...",
    )
    credentials_ciphertext: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Encrypted credential JSON (Fernet); NULL for credential-less connections",
    )
    credentials_key_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Id of the encryption key version that produced the ciphertext",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="active",
        comment="Connection status: 'active' | 'disabled' | 'error'",
    )
    last_checked_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Last successful connectivity check (UTC)",
    )
    notes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Operator notes",
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

    def __repr__(self) -> str:
        return (
            f"BankConnectionModel(id={self.id!r}, bank_key={self.bank_key!r}, "
            f"company_id={self.company_id!r})"
        )


class StoreModel(Base):
    """One store in an installation's registry (phased design §2.1).

    Maps to the 'stores' table. The webhook secret and the scoped
    read-only API key are generated by HesabYar and stored ENCRYPTED;
    plaintext is shown to the operator exactly once at
    creation/rotation and never again.
    """

    __tablename__ = "stores"
    __table_args__ = (
        UniqueConstraint("installation_id", "store_key", name="uq_stores_installation_key"),
        Index("ix_stores_installation", "installation_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    installation_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        comment="Installation id (from the authenticated API key)",
    )
    store_key: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Stable store slug, used as {connector} in the events endpoint",
    )
    display_name: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
        comment="Persian display name",
    )
    store_type: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Store kind as a free string: 'custom' | 'woocommerce' | 'sazito' | ...",
    )
    api_base_url: Mapped[str | None] = mapped_column(
        String(1024),
        nullable=True,
        comment="Store API base URL for reconciliation polling (non-secret)",
    )
    prices_in_toman: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        comment="Per-store Toman setting: True converts Toman to Rial (x10) at the connector boundary",
    )
    webhook_secret_ciphertext: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Encrypted HMAC webhook secret (Fernet)",
    )
    api_key_ciphertext: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Encrypted scoped read-only API key for polling (Fernet)",
    )
    store_db_dsn_ciphertext: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Encrypted read-only store-database DSN for reconciliation/backfill (Fernet, US-512)",
    )
    api_key_scopes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Comma-separated read-only scopes of the store API key",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="active",
        comment="Store status: 'active' | 'disabled' | 'error'",
    )
    last_webhook_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Last webhook received from this store (UTC)",
    )
    last_poll_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Last successful reconciliation poll of this store (UTC)",
    )
    notes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Operator notes",
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

    def __repr__(self) -> str:
        return f"StoreModel(id={self.id!r}, store_key={self.store_key!r})"


class InstallationSettingsModel(Base):
    """Installation-level integration settings (one row per installation).

    Maps to the 'installation_settings' table. ``weekly_holiday`` is
    the weekday number (Monday=0 … Sunday=6); Friday (4) is the
    default weekly holiday in Iran. ``matching_window_days`` is the
    settlement/bank matching window in CALENDAR days (no verified
    holiday source exists yet — §8.4). ``auto_post_enabled`` is the
    global auto-post opt-in (draft is the default).
    """

    __tablename__ = "installation_settings"

    installation_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        comment="Installation id (from the authenticated API key)",
    )
    weekly_holiday: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=4,
        comment="Weekly holiday weekday number (Monday=0 … Sunday=6); Friday=4 is the default",
    )
    matching_window_days: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=2,
        comment="Settlement/bank matching window in CALENDAR days (±N days)",
    )
    auto_post_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="Global auto-post opt-in (False = draft only)",
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

    def __repr__(self) -> str:
        return f"InstallationSettingsModel(installation_id={self.installation_id!r})"


class InstallationHolidayModel(Base):
    """One official holiday date for an installation (configurable list).

    Maps to the 'installation_holidays' table. No verified
    Iranian-holiday API was found (§8.4), so the list is
    operator-maintained until a source is chosen.
    """

    __tablename__ = "installation_holidays"
    __table_args__ = (
        UniqueConstraint(
            "installation_id", "holiday_date", name="uq_installation_holidays_installation_date"
        ),
        Index("ix_installation_holidays_installation", "installation_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    installation_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        comment="Installation id (from the authenticated API key)",
    )
    holiday_date: Mapped[datetime.date] = mapped_column(
        Date,
        nullable=False,
        comment="Holiday date (Gregorian)",
    )
    title: Mapped[str | None] = mapped_column(
        String(256),
        nullable=True,
        comment="Holiday name, e.g. 'نوروز'",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Row creation timestamp (UTC)",
    )

    def __repr__(self) -> str:
        return (
            f"InstallationHolidayModel(id={self.id!r}, "
            f"holiday_date={self.holiday_date!r})"
        )


class ReviewItemModel(Base):
    """One uniform review-queue item (US-504, Phase 5 F4).

    Maps to the 'review_items' table. Every exception — from any
    module — lands here with the same shape (phased design §1.2 F4 /
    §7.2): type, severity, status, channel, source event reference,
    reason, suggested action, amount (IRR, nullable — not every
    exception carries money), assignee and resolution. Age and SLA
    deadline are computed at read time from ``created_at`` + the
    type's SLA policy, never stored.
    """

    __tablename__ = "review_items"
    __table_args__ = (
        CheckConstraint(
            "item_type IN ('mapping', 'settlement_variance', 'cheque_exception', "
            "'bank_unmatched', 'packaging_reorder', 'installment_overdue', "
            "'manual_import_review', 'cashbox_variance', 'inbox_dead_letter', "
            "'sms_delivery_failed', 'reminder')",
            name="ck_review_items_item_type",
        ),
        CheckConstraint(
            "severity IN ('info', 'warning', 'critical')",
            name="ck_review_items_severity",
        ),
        CheckConstraint(
            "status IN ('open', 'in_review', 'resolved', 'written_off')",
            name="ck_review_items_status",
        ),
        CheckConstraint(
            "resolution IS NULL OR resolution IN ('confirmed_ok', 'adjusted', 'write_off', "
            "'escalated', 'rescheduled', 'skipped', 'deferred')",
            name="ck_review_items_resolution",
        ),
        CheckConstraint(
            "(amount_irr IS NULL OR amount_irr >= 0)",
            name="ck_review_items_amount_non_negative",
        ),
        CheckConstraint(
            "(status IN ('resolved', 'written_off') AND resolution IS NOT NULL) "
            "OR (status IN ('open', 'in_review') AND resolution IS NULL)",
            name="ck_review_items_resolution_matches_status",
        ),
        Index("ix_review_items_tenant_id", "tenant_id"),
        Index("ix_review_items_tenant_type_status", "tenant_id", "item_type", "status"),
        Index("ix_review_items_tenant_severity_created", "tenant_id", "severity", "created_at"),
        Index("ix_review_items_tenant_channel", "tenant_id", "channel"),
        Index("ix_review_items_tenant_company", "tenant_id", "company_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key (review item id)",
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Owning tenant FK (CASCADE delete)",
    )
    company_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="API company id as supplied (row id or engine id); plain external reference, no FK",
    )
    item_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="Exception type — one of the ten §7.2 + US-544 types",
    )
    subtype: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Subtype for filtering/stats (e.g. cheque bounce vs overdue)",
    )
    severity: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="info | warning | critical",
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="open",
        comment="open | in_review | resolved | written_off",
    )
    channel: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Payment channel / gateway key the exception came from (e.g. 'zarinpal')",
    )
    source_event_ref: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="Reference to the originating event (inbox event id / source event id)",
    )
    reason: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Why the item needs review (Persian, human-readable)",
    )
    suggested_action: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="System-suggested next step (Persian, human-readable)",
    )
    amount_irr: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="Amount at stake in IRR (nullable — not every exception carries money)",
    )
    assignee: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="Actor id/name assigned to the item (default assignee: accountant)",
    )
    tags: Mapped[list] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        comment="Tags applied by rules (US-561 'tag' action) or operators — a JSON list of strings",
    )
    resolution: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
        comment="How the item was closed (set only on resolved/written_off)",
    )
    resolved_by: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="Actor who closed the item",
    )
    resolved_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the item was closed (UTC)",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Row creation timestamp (UTC) — the age/SLA anchor",
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Last update timestamp (UTC)",
    )

    def __repr__(self) -> str:
        return (
            f"ReviewItemModel(id={self.id!r}, item_type={self.item_type!r}, "
            f"severity={self.severity!r}, status={self.status!r})"
        )


class ReviewItemAuditModel(Base):
    """One audit entry on a review item (US-504, Phase 5 F4).

    Maps to the 'review_item_audit' table. Append-only: every action
    on every item leaves an entry (who, when, which action, which
    decision, with which policy/mapping version). When F1's decision
    audit log lands, that log becomes the audit source and this
    table's rows are the queue-side record the port merges with it —
    the seam is the ``ReviewAuditPort``, not this table.
    """

    __tablename__ = "review_item_audit"
    __table_args__ = (Index("ix_review_item_audit_item_created", "item_id", "created_at"),)

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key (audit entry id)",
    )
    item_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("review_items.id", ondelete="CASCADE"),
        nullable=False,
        comment="Review item FK (CASCADE delete)",
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Owning tenant FK (CASCADE delete) — denormalized for tenant-scoped reads",
    )
    actor: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Actor who performed the action (API client id / user id)",
    )
    action: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Action code performed (e.g. 'replay', 'filed', 'assign')",
    )
    decision: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Decision recorded by the action (resolution or resulting status)",
    )
    note: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Human note attached to the action",
    )
    policy_version: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Policy version the decision used (None until F1's audit log supplies it)",
    )
    mapping_version: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment=(
            "Mapping version the decision used (None until F1's audit log "
            "supplies it); unbounded — one event resolves several mappings "
            "(US-511)"
        ),
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Row creation timestamp (UTC)",
    )

    def __repr__(self) -> str:
        return (
            f"ReviewItemAuditModel(id={self.id!r}, item_id={self.item_id!r}, "
            f"action={self.action!r}, actor={self.actor!r})"
        )


class ReviewSlaPolicyModel(Base):
    """One type's SLA policy for one tenant (US-504, Phase 5 F4).

    Maps to the 'review_sla_policies' table. One row per (tenant,
    item_type); a missing row means the skeleton default applies (see
    ``domain/review_queue/models.py::DEFAULT_SLA_POLICIES``). Admins
    configure policies through the PATCH endpoint — never by editing
    the table.
    """

    __tablename__ = "review_sla_policies"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "item_type",
            name="uq_review_sla_policies_tenant_type",
        ),
        CheckConstraint(
            "item_type IN ('mapping', 'settlement_variance', 'cheque_exception', "
            "'bank_unmatched', 'packaging_reorder', 'installment_overdue', "
            "'manual_import_review', 'cashbox_variance', 'inbox_dead_letter', "
            "'sms_delivery_failed')",
            name="ck_review_sla_policies_item_type",
        ),
        CheckConstraint(
            "sla_hours > 0 AND warn_hours > 0 AND warn_hours < sla_hours",
            name="ck_review_sla_policies_hours",
        ),
        Index("ix_review_sla_policies_tenant_id", "tenant_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key (SLA policy id)",
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Owning tenant FK (CASCADE delete)",
    )
    item_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="Exception type — one of the ten §7.2 + US-544 types",
    )
    sla_hours: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="SLA deadline in whole hours after filing",
    )
    warn_hours: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Warn window in whole hours before the deadline",
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Last update timestamp (UTC)",
    )

    def __repr__(self) -> str:
        return (
            f"ReviewSlaPolicyModel(tenant_id={self.tenant_id!r}, item_type={self.item_type!r}, "
            f"sla_hours={self.sla_hours!r}, warn_hours={self.warn_hours!r})"
        )


class ManualEntryModel(Base):
    """One manual order/invoice/refund entry (US-513, Phase 5 wave 2, stream S3).

    Maps to the 'manual_entries' table. The traceability anchor for
    dual entry (ADR 0003 decision 1): the operator's entry (who,
    when, what) linked to every canonical event it produced. The
    ``events`` JSONB column carries the produced events
    (``event_type`` + ``inbox_event_id`` + ``source_event_id`` +
    external-entity identity); the primary event's payload is
    snapshotted in ``payload`` so the entry stays readable even if
    the inbox row is purged. Money is whole Toman (``int``) — the
    operator types Toman and the pipeline converts ×10 to Rial.
    """

    __tablename__ = "manual_entries"
    __table_args__ = (
        CheckConstraint(
            "entry_type IN ('order', 'refund', 'cancellation')",
            name="ck_manual_entries_entry_type",
        ),
        CheckConstraint(
            "total_amount_toman >= 0",
            name="ck_manual_entries_total_non_negative",
        ),
        Index("ix_manual_entries_tenant_id", "tenant_id"),
        Index("ix_manual_entries_tenant_type_created", "tenant_id", "entry_type", "created_at"),
        Index("ix_manual_entries_tenant_order", "tenant_id", "order_number"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key (manual entry id; also the correlation id of its events)",
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Owning tenant FK (CASCADE delete)",
    )
    entry_type: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="order | refund | cancellation",
    )
    source_store_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Store the entry is attributed to ('manual' when the operator named none)",
    )
    order_number: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Operator-supplied order reference (the external entity id of the order event)",
    )
    total_amount_toman: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="Entry total in whole Toman (order total / refund amount / cancelled total, 0 when unknown)",
    )
    entered_by: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="API key id that created the entry (the actor, for the audit trail)",
    )
    entered_by_name: Mapped[str | None] = mapped_column(
        String(256),
        nullable=True,
        comment="API client name at creation time (display only)",
    )
    events: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        comment="Produced events: [{event_type, inbox_event_id, source_event_id, ...}]",
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        comment="Primary event payload snapshot (whole Toman, as entered)",
    )
    note: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Operator note (optional)",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Entry creation timestamp (UTC)",
    )

    def __repr__(self) -> str:
        return (
            f"ManualEntryModel(id={self.id!r}, entry_type={self.entry_type!r}, "
            f"order_number={self.order_number!r})"
        )


# ---------------------------------------------------------------------------
# Bank statement import + reconciliation (US-514, Phase 5 wave 2, 5ت B1/B2)
# ---------------------------------------------------------------------------


class BankColumnMappingModel(Base):
    """A saved per-bank column mapping (phased design §4 §2.1).

    Maps to the 'bank_column_mappings' table. ``columns`` is a JSONB
    object: canonical field name -> source column (header name, or a
    positional ``#<index>`` reference for headerless files).
    ``company_id`` is NULL when the mapping is shared across the
    tenant's companies (a bank's layout does not vary by company).
    """

    __tablename__ = "bank_column_mappings"
    __table_args__ = (
        CheckConstraint(
            "file_format IN ('xlsx', 'csv', 'pdf')",
            name="ck_bank_column_mappings_file_format",
        ),
        CheckConstraint(
            "date_kind IN ('jalali', 'gregorian')",
            name="ck_bank_column_mappings_date_kind",
        ),
        CheckConstraint(
            "currency_unit IN ('rial', 'toman')",
            name="ck_bank_column_mappings_currency_unit",
        ),
        CheckConstraint(
            "status IN ('active', 'superseded')",
            name="ck_bank_column_mappings_status",
        ),
        CheckConstraint("header_row >= 0", name="ck_bank_column_mappings_header_row"),
        Index("ix_bank_column_mappings_tenant_bank", "tenant_id", "bank_key", "status"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key (column mapping id)",
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Owning tenant FK (CASCADE delete)",
    )
    company_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="HesabYar company row id when company-scoped; NULL = shared across companies",
    )
    bank_key: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Bank key (e.g. 'melli') — a registry key, not a hard-coded list",
    )
    name: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Operator-visible mapping name",
    )
    file_format: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="xlsx | csv | pdf",
    )
    columns: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        comment="Canonical field -> source column (header name or '#<index>')",
    )
    date_kind: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="jalali",
        comment="jalali | gregorian — how the file's dates are written",
    )
    currency_unit: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="rial",
        comment="rial | toman — the file's amount unit (last used; the import request may override)",
    )
    header_row: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="0-based index of the header row in the file grid",
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="active",
        comment="active | superseded",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Creation timestamp (UTC)",
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Last update timestamp (UTC)",
    )

    def __repr__(self) -> str:
        return (
            f"BankColumnMappingModel(id={self.id!r}, bank_key={self.bank_key!r}, "
            f"name={self.name!r}, status={self.status!r})"
        )


class BankImportBatchModel(Base):
    """One statement-file import batch (phased design §4 §2.2).

    Maps to the 'bank_import_batches' table. The batch id IS the
    ``import_batch_id``. Totals cover NEW valid rows only — duplicates
    and invalid rows never enter the accounting preview.
    """

    __tablename__ = "bank_import_batches"
    __table_args__ = (
        CheckConstraint(
            "file_format IN ('xlsx', 'csv', 'pdf')",
            name="ck_bank_import_batches_file_format",
        ),
        CheckConstraint(
            "currency_unit IN ('rial', 'toman')",
            name="ck_bank_import_batches_currency_unit",
        ),
        CheckConstraint(
            "status IN ('awaiting_review', 'confirmed', 'cancelled')",
            name="ck_bank_import_batches_status",
        ),
        CheckConstraint("total_rows >= 0", name="ck_bank_import_batches_total_rows"),
        CheckConstraint("new_rows >= 0", name="ck_bank_import_batches_new_rows"),
        CheckConstraint("duplicate_rows >= 0", name="ck_bank_import_batches_duplicate_rows"),
        CheckConstraint("invalid_rows >= 0", name="ck_bank_import_batches_invalid_rows"),
        CheckConstraint(
            "total_deposits_irr >= 0", name="ck_bank_import_batches_deposits_non_negative"
        ),
        CheckConstraint(
            "total_withdrawals_irr >= 0", name="ck_bank_import_batches_withdrawals_non_negative"
        ),
        Index("ix_bank_import_batches_tenant_company", "tenant_id", "company_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key (import_batch_id)",
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Owning tenant FK (CASCADE delete)",
    )
    company_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="HesabYar company row id (plain external reference, no FK)",
    )
    bank_key: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Bank key (e.g. 'melli')",
    )
    statement_journal_id: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Engine account.statement.journal id the statement is created under",
    )
    filename: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        comment="Uploaded filename (provenance only — file bytes are never stored)",
    )
    file_format: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="xlsx | csv | pdf",
    )
    currency_unit: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="rial | toman — the file's amount unit, as declared on import",
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="awaiting_review",
        comment="awaiting_review | confirmed | cancelled",
    )
    total_rows: Mapped[int] = mapped_column(Integer, nullable=False, comment="Data rows in the file")
    new_rows: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="Valid, non-duplicate rows (will be imported)"
    )
    duplicate_rows: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="Rows skipped as duplicates (either dedup layer)"
    )
    invalid_rows: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="Rows that failed parsing (shown on the preview)"
    )
    total_deposits_irr: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="Deposits over new rows, whole Rial"
    )
    total_withdrawals_irr: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="Withdrawals over new rows, whole Rial (positive)"
    )
    default_account_id: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Engine account id used for uncoded rows (Tryton requires an account per line)",
    )
    column_mapping_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("bank_column_mappings.id", ondelete="SET NULL"),
        nullable=True,
        comment="Saved mapping used, when one was used (NULL = inline mapping)",
    )
    start_balance_irr: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="Opening balance from the file's balance column, if any"
    )
    end_balance_irr: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="Closing balance from the file's balance column, if any"
    )
    engine_statement_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Engine account.statement id, set on confirm",
    )
    review_item_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        comment="manual_import_review queue item id for this batch",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Creation timestamp (UTC)",
    )
    confirmed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Confirmation timestamp (UTC), when confirmed",
    )
    cancelled_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Cancellation timestamp (UTC), when cancelled",
    )

    def __repr__(self) -> str:
        return (
            f"BankImportBatchModel(id={self.id!r}, bank_key={self.bank_key!r}, "
            f"status={self.status!r}, new_rows={self.new_rows!r})"
        )


class BankImportRowModel(Base):
    """One parsed row of an import batch (phased design §4 §2.2).

    Maps to the 'bank_import_rows' table. Tenant/company/bank are
    denormalised here so the dedup checks never join the batch table.
    Dedup layer 1 is the partial unique index on
    (tenant, company, bank, bank_ref, row_date) for rows WITH a bank
    reference that are NOT flagged duplicates: a re-uploaded file's
    rows are stored flagged ``is_duplicate`` (so the preview can show
    them) and must not trip the constraint — the constraint guards
    only new rows, i.e. the same line being imported twice as new
    data. Layer 2 is the ``match_key`` index (date+amount+
    description) for files without one.
    """

    __tablename__ = "bank_import_rows"
    __table_args__ = (
        CheckConstraint(
            "(is_duplicate = FALSE) OR (duplicate_reason IS NOT NULL)",
            name="ck_bank_import_rows_duplicate_reason",
        ),
        Index("ix_bank_import_rows_batch", "batch_id", "row_number"),
        Index(
            "ix_bank_import_rows_match_key",
            "tenant_id",
            "company_id",
            "bank_key",
            "match_key",
        ),
        Index(
            "uq_bank_import_rows_bank_ref",
            "tenant_id",
            "company_id",
            "bank_key",
            "bank_ref",
            "row_date",
            unique=True,
            postgresql_where=text("bank_ref IS NOT NULL AND is_duplicate = FALSE"),
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key (import row id)",
    )
    batch_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("bank_import_batches.id", ondelete="CASCADE"),
        nullable=False,
        comment="Owning batch FK (CASCADE delete)",
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Owning tenant FK (CASCADE delete, denormalised for dedup)",
    )
    company_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="HesabYar company row id (denormalised for dedup)",
    )
    bank_key: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Bank key (denormalised for dedup)",
    )
    row_number: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="1-based row number in the file (excluding the header)"
    )
    row_date: Mapped[datetime.date | None] = mapped_column(
        Date, nullable=True, comment="Gregorian transaction date (NULL when unparseable)"
    )
    amount_irr: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="Signed amount, whole Rial (NULL when unparseable)"
    )
    description: Mapped[str] = mapped_column(
        Text, nullable=False, default="", comment="Transaction description as parsed"
    )
    bank_ref: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="Bank tracking/reference number, when the file has one"
    )
    balance_irr: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="Running balance after the row, whole Rial, if mapped"
    )
    match_key: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="sha256(date|amount|description) — dedup layer 2 (NULL for invalid rows)",
    )
    is_duplicate: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, comment="True when dedup skipped this row"
    )
    duplicate_reason: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="Which dedup layer caught it ('bank_ref' | 'match_key')",
    )
    parse_errors: Mapped[list] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        comment="Per-row parse problems (empty when the row parsed cleanly)",
    )
    was_rounded: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="True when a Toman amount rounded HALF_UP to a whole Rial",
    )
    suggested_account_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="Suggested counterpart engine account id (preview)"
    )
    suggested_party_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="Suggested party engine id (preview)"
    )
    suggested_invoice_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="Suggested invoice engine id (preview)"
    )
    suggestion_source: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="Where the suggestion came from ('rule' | 'invoice' | 'payment' | 'default')",
    )

    def __repr__(self) -> str:
        return (
            f"BankImportRowModel(id={self.id!r}, batch_id={self.batch_id!r}, "
            f"row_number={self.row_number!r}, amount_irr={self.amount_irr!r})"
        )


class BankMatchRuleModel(Base):
    """One learned bank-coding rule (B2 owns learned mappings).

    Maps to the 'bank_match_rules' table. A confirmed manual mapping
    becomes (or confirms) a rule; the unique constraint makes
    re-confirming the same (pattern, direction, target) increment
    ``confirmations`` instead of duplicating the rule. ``bank_key``
    empty means the rule applies to every bank.
    """

    __tablename__ = "bank_match_rules"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "company_id",
            "bank_key",
            "pattern",
            "direction",
            "target_account_id",
            name="uq_bank_match_rules_pattern_target",
        ),
        CheckConstraint(
            "direction IN ('in', 'out', 'any')",
            name="ck_bank_match_rules_direction",
        ),
        CheckConstraint("mode IN ('suggest', 'auto')", name="ck_bank_match_rules_mode"),
        CheckConstraint("confirmations >= 1", name="ck_bank_match_rules_confirmations"),
        Index("ix_bank_match_rules_tenant_company", "tenant_id", "company_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key (match rule id)",
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Owning tenant FK (CASCADE delete)",
    )
    company_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="HesabYar company row id (plain external reference, no FK)",
    )
    bank_key: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="",
        comment="Bank key the rule applies to; empty = every bank",
    )
    pattern: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
        comment="Normalised description substring the rule matches on",
    )
    direction: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        comment="in | out | any — money direction the rule applies to",
    )
    target_account_id: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="Engine account id the rule codes to"
    )
    target_party_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="Engine party id the rule codes to, when known"
    )
    target_kind: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="account",
        comment="account | misc_income — what the target means",
    )
    mode: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="suggest",
        comment="suggest | auto — Suggest first, Auto only after operator trust",
    )
    confirmations: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        comment="How many confirmed manual mappings back this rule",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Creation timestamp (UTC)",
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Last update timestamp (UTC)",
    )

    def __repr__(self) -> str:
        return (
            f"BankMatchRuleModel(id={self.id!r}, pattern={self.pattern!r}, "
            f"mode={self.mode!r}, confirmations={self.confirmations!r})"
        )


class BankRuleModel(Base):
    """One user-defined bank auto-coding rule (US-565, W3).

    Maps to the 'bank_rules' table. The Xero-style rule: ``when
    <statement-line pattern> → suggest <account + mapping>``. The
    pattern is an AND of optional conditions (description substring,
    counterparty name, amount range in whole Rial, direction); the
    check constraint requires at least one condition — a rule with
    no condition would match every line. Rules are born in SUGGEST
    mode with zero confirmations and become AUTO only through a
    human promotion at the threshold (``RULE_AUTO_MIN_CONFIRMATIONS``
    = 3, the same floor as the B2 learned rules and the US-552
    mapping-learning promotion). ``bank_key`` empty means every bank.
    """

    __tablename__ = "bank_rules"
    __table_args__ = (
        CheckConstraint(
            "direction IN ('in', 'out', 'any')",
            name="ck_bank_rules_direction",
        ),
        CheckConstraint("mode IN ('suggest', 'auto')", name="ck_bank_rules_mode"),
        CheckConstraint("confirmations >= 0", name="ck_bank_rules_confirmations"),
        CheckConstraint(
            "amount_min_irr IS NULL OR amount_min_irr >= 0",
            name="ck_bank_rules_amount_min_nonneg",
        ),
        CheckConstraint(
            "amount_max_irr IS NULL OR amount_max_irr >= 0",
            name="ck_bank_rules_amount_max_nonneg",
        ),
        CheckConstraint(
            "amount_min_irr IS NULL OR amount_max_irr IS NULL OR amount_min_irr <= amount_max_irr",
            name="ck_bank_rules_amount_range",
        ),
        CheckConstraint(
            "description_contains IS NOT NULL OR counterparty_contains IS NOT NULL "
            "OR amount_min_irr IS NOT NULL OR amount_max_irr IS NOT NULL",
            name="ck_bank_rules_has_pattern",
        ),
        CheckConstraint(
            "target_account_id > 0",
            name="ck_bank_rules_target_account",
        ),
        Index("ix_bank_rules_tenant_company", "tenant_id", "company_id"),
        Index("ix_bank_rules_tenant_mode", "tenant_id", "mode"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key (bank rule id)",
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Owning tenant FK (CASCADE delete)",
    )
    company_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="HesabYar company row id (plain external reference, no FK)",
    )
    bank_key: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="",
        comment="Bank key the rule applies to; empty = every bank",
    )
    name: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        comment="Operator-facing rule name",
    )
    description_contains: Mapped[str | None] = mapped_column(
        String(256),
        nullable=True,
        comment="Normalised description substring (NULL = no description condition)",
    )
    counterparty_contains: Mapped[str | None] = mapped_column(
        String(256),
        nullable=True,
        comment="Normalised counterparty name substring (NULL = no counterparty condition)",
    )
    amount_min_irr: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Minimum line magnitude in whole Rial, inclusive (NULL = no lower bound)",
    )
    amount_max_irr: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Maximum line magnitude in whole Rial, inclusive (NULL = no upper bound)",
    )
    direction: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        default="any",
        comment="in | out | any — money direction the rule applies to",
    )
    target_account_id: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="Engine account id the rule codes to"
    )
    target_party_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="Engine party id the rule codes to, when known"
    )
    target_kind: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="account",
        comment="account | misc_income — what the target means",
    )
    mode: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="suggest",
        comment="suggest | auto — Suggest first, Auto only after human promotion at threshold",
    )
    confirmations: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="How many of this rule's suggestions a human confirmed",
    )
    priority: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=100,
        comment="Firing order inside one company's set (lower runs first; first match wins)",
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        comment="A disabled rule never fires (live or suggested)",
    )
    created_by: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="API client id that created the rule (NULL = system)",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Creation timestamp (UTC)",
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Last update timestamp (UTC)",
    )

    def __repr__(self) -> str:
        return (
            f"BankRuleModel(id={self.id!r}, name={self.name!r}, "
            f"mode={self.mode!r}, confirmations={self.confirmations!r})"
        )


class BankRuleSuggestionModel(Base):
    """One suggestion a bank rule made for one statement line (US-565, W3).

    Maps to the 'bank_rule_suggestions' table. Recorded when a
    reconcile run matches a line against a SUGGEST-mode bank rule.
    ``rule_id`` is NULL when the rule was deleted (the audit row
    survives — ON DELETE SET NULL); ``rule_name`` and the target
    snapshot keep the row readable afterwards. The partial unique
    index is the dedup backstop: at most one pending suggestion per
    (rule, statement, line) — a reconcile re-run must not record a
    second pending row for the same decision.
    """

    __tablename__ = "bank_rule_suggestions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'confirmed', 'rejected')",
            name="ck_bank_rule_suggestions_status",
        ),
        CheckConstraint(
            "target_account_id > 0",
            name="ck_bank_rule_suggestions_target_account",
        ),
        Index("ix_bank_rule_suggestions_tenant_rule", "tenant_id", "rule_id"),
        Index("ix_bank_rule_suggestions_tenant_status", "tenant_id", "status"),
        Index(
            "uq_bank_rule_suggestions_pending",
            "rule_id",
            "statement_id",
            "statement_line_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key (suggestion id)",
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Owning tenant FK (CASCADE delete)",
    )
    rule_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("bank_rules.id", ondelete="SET NULL"),
        nullable=True,
        comment="Bank rule that suggested this coding (NULL when the rule was deleted)",
    )
    rule_name: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        comment="Rule name snapshot (readable after the rule is deleted)",
    )
    company_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="HesabYar company row id (plain external reference, no FK)",
    )
    statement_id: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="Engine statement id of the suggested line"
    )
    statement_line_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="Engine statement line id")
    line_description: Mapped[str] = mapped_column(Text, nullable=False, comment="Statement line description snapshot")
    line_amount_irr: Mapped[int] = mapped_column(Integer, nullable=False, comment="Signed line amount in whole Rial")
    target_account_id: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="Engine account id the rule suggested"
    )
    target_party_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="Engine party id the rule suggested, when known"
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="pending",
        comment="pending | confirmed | rejected",
    )
    confirmed_by: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="Actor that confirmed/rejected the suggestion",
    )
    confirmed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the suggestion was confirmed/rejected (UTC)",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Creation timestamp (UTC)",
    )

    def __repr__(self) -> str:
        return f"BankRuleSuggestionModel(id={self.id!r}, rule_id={self.rule_id!r}, status={self.status!r})"


class BankStatementMatchModel(Base):
    """One recorded reconciliation match for a statement line.

    Maps to the 'bank_statement_matches' table. The durable record of
    a reconcile decision (which line matched what, at which priority,
    with which confidence, and to which destination bank account).
    ``target_id`` is the engine id for invoice/payment targets (NULL
    for a rule target, whose stable key is the rule UUID in
    ``target_ref``).
    """

    __tablename__ = "bank_statement_matches"
    __table_args__ = (
        CheckConstraint(
            "target_type IN ('invoice', 'payment', 'rule', 'manual')",
            name="ck_bank_statement_matches_target_type",
        ),
        CheckConstraint(
            "status IN ('auto_applied', 'suggested', 'confirmed', 'rejected', 'unmatched')",
            name="ck_bank_statement_matches_status",
        ),
        CheckConstraint(
            "outcome IS NULL OR outcome IN "
            "('already_matched', 'matched', 'suggested', 'ambiguous', 'unmatched', 'skipped_zero')",
            name="ck_bank_statement_matches_outcome",
        ),
        CheckConstraint(
            "priority IN (1, 2, 3)", name="ck_bank_statement_matches_priority"
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 100",
            name="ck_bank_statement_matches_confidence",
        ),
        Index("ix_bank_statement_matches_statement", "tenant_id", "statement_id"),
        Index("ix_bank_statement_matches_company", "tenant_id", "company_id"),
        Index(
            "ix_bank_statement_matches_line",
            "tenant_id",
            "statement_id",
            "statement_line_id",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key (match id)",
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Owning tenant FK (CASCADE delete)",
    )
    company_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="HesabYar company row id (plain external reference, no FK)",
    )
    statement_id: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="Engine account.statement id"
    )
    statement_line_id: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="Engine account.statement.line id"
    )
    line_date: Mapped[datetime.date] = mapped_column(
        Date, nullable=False, comment="Gregorian date of the matched line"
    )
    line_amount_irr: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="Signed line amount, whole Rial"
    )
    line_description: Mapped[str] = mapped_column(
        Text, nullable=False, default="", comment="Line description at match time"
    )
    target_type: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="invoice | payment | rule | manual"
    )
    target_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="Engine target id (NULL for rule targets)"
    )
    target_ref: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Stable target key: engine id as text, or the rule UUID",
    )
    target_label: Mapped[str] = mapped_column(
        String(256), nullable=False, comment="Operator-visible target label"
    )
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="Matching priority that produced this (1 | 2 | 3)"
    )
    confidence: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="Match confidence 0–100"
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="auto_applied | suggested | confirmed | rejected | unmatched",
    )
    outcome: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
        comment="Reconcile outcome that produced the record (NULL for manual matches)",
    )
    destination_account_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Statement journal's bank account id (the money's destination)",
    )
    destination_shaba: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="Destination Shaba (IBAN) when the journal carries a bank account",
    )
    review_item_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        comment="bank_unmatched queue item id, when one was filed for this line",
    )
    matched_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Match timestamp (UTC)",
    )
    matched_by: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="Actor that confirmed/applied the match (NULL for automatic matches)",
    )

    def __repr__(self) -> str:
        return (
            f"BankStatementMatchModel(id={self.id!r}, statement_id={self.statement_id!r}, "
            f"line_id={self.statement_line_id!r}, status={self.status!r})"
        )


class PackagingMaterialModel(Base):
    """One packaging material: a HesabYar catalog row over a Tryton product.

    Maps to the 'packaging_materials' table (US-515, stream K1).
    Quantity and cost price are NEVER stored here — they are always
    read live from the engine through the port. ``tryton_product_id``
    / ``tryton_template_id`` are opaque engine ids.
    """

    __tablename__ = "packaging_materials"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "company_id",
            "tryton_product_id",
            name="uq_packaging_materials_tenant_company_product",
        ),
        CheckConstraint(
            "cost_method IN ('average', 'fifo')",
            name="ck_packaging_materials_cost_method",
        ),
        Index("ix_packaging_materials_tenant_company", "tenant_id", "company_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key (material id)",
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Owning tenant FK (CASCADE delete)",
    )
    company_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="API company id as supplied (row id or engine id); plain external reference, no FK",
    )
    tryton_product_id: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Engine product.product id (the variant)",
    )
    tryton_template_id: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Engine product.template id",
    )
    name_fa: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
        comment="Persian material name",
    )
    cost_method: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="average",
        comment="Cost method: 'average' (default) | 'fifo'",
    )
    code: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Optional material code (mirrors the Tryton template code)",
    )
    uom_name: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Unit of measure name at creation (display only; engine is authoritative)",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        comment="Inactive materials stay in history but cannot be consumed",
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

    def __repr__(self) -> str:
        return (
            f"PackagingMaterialModel(id={self.id!r}, name_fa={self.name_fa!r}, "
            f"tryton_product_id={self.tryton_product_id!r})"
        )


class PackagingConsumptionModel(Base):
    """One recorded packaging consumption for an order (dual-entry trace).

    Maps to the 'packaging_consumptions' table (US-515). The Tryton
    stock move is the accounting truth; this row is the operational
    trace (order, shipment, cost centre, cost at consumption time).
    ``idempotency_key`` is unique per tenant: the same order +
    shipment + material can only ever consume once.
    """

    __tablename__ = "packaging_consumptions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_packaging_consumptions_tenant_key",
        ),
        CheckConstraint(
            "source IN ('manual', 'webhook')",
            name="ck_packaging_consumptions_source",
        ),
        CheckConstraint("quantity > 0", name="ck_packaging_consumptions_quantity_positive"),
        Index("ix_packaging_consumptions_tenant_company", "tenant_id", "company_id"),
        Index(
            "ix_packaging_consumptions_tenant_company_order",
            "tenant_id",
            "company_id",
            "order_ref",
        ),
        Index(
            "ix_packaging_consumptions_material",
            "material_id",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key (consumption id)",
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Owning tenant FK (CASCADE delete)",
    )
    company_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="API company id as supplied (row id or engine id); plain external reference, no FK",
    )
    material_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("packaging_materials.id", ondelete="RESTRICT"),
        nullable=False,
        comment="Material FK — a material with consumption history cannot be deleted",
    )
    order_ref: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Store order reference (manual entry until US-511 wires the store)",
    )
    shipment_ref: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Shipment reference within the order (manual entry until US-511)",
    )
    quantity: Mapped[Decimal] = mapped_column(
        Numeric(18, 4),
        nullable=False,
        comment="Consumed quantity (must be > 0)",
    )
    unit_cost_irr: Mapped[Decimal] = mapped_column(
        Numeric(18, 4),
        nullable=False,
        comment="Engine average cost price per unit at move time (may be fractional)",
    )
    total_cost_irr: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="Whole-Rial total (quantity x unit cost, HALF_UP)",
    )
    rounding_delta_irr: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="Total minus the exact product, in rials — recorded, never hidden",
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
        comment="pack:use:{order_ref}:{shipment_ref}:{material_id}",
    )
    source: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="manual",
        comment="Where the consumption came from: 'manual' | 'webhook'",
    )
    analytic_account_engine_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Order's analytic account engine id (NULL when the order has no cost centre yet)",
    )
    tryton_move_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Engine stock.move id (NULL while the claim is unexecuted)",
    )
    consumed_on: Mapped[datetime.date | None] = mapped_column(
        Date,
        nullable=True,
        comment="Consumption date (Gregorian; the move's planned/effective date)",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Row creation timestamp (UTC)",
    )

    def __repr__(self) -> str:
        return (
            f"PackagingConsumptionModel(id={self.id!r}, order_ref={self.order_ref!r}, "
            f"material_id={self.material_id!r})"
        )


class PackagingShipmentCostModel(Base):
    """The three shipment-cost figures for one order, kept separate.

    Maps to the 'packaging_shipment_costs' table (US-515). Invoiced
    (charged to the customer), actual (paid to the carrier) and
    absorbed (actual - invoiced, computed at read time) are never
    merged — the margin impact must stay visible.
    """

    __tablename__ = "packaging_shipment_costs"
    __table_args__ = (
        CheckConstraint(
            "method IN ('on_order', 'on_shipment', 'none')",
            name="ck_packaging_shipment_costs_method",
        ),
        CheckConstraint(
            "invoiced_irr >= 0 AND actual_irr >= 0",
            name="ck_packaging_shipment_costs_non_negative",
        ),
        Index("ix_packaging_shipment_costs_tenant_company", "tenant_id", "company_id"),
        Index(
            "ix_packaging_shipment_costs_tenant_company_order",
            "tenant_id",
            "company_id",
            "order_ref",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key (shipment-cost id)",
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Owning tenant FK (CASCADE delete)",
    )
    company_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="API company id as supplied (row id or engine id); plain external reference, no FK",
    )
    order_ref: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Store order reference",
    )
    method: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="How the cost is charged: 'on_order' | 'on_shipment' | 'none'",
    )
    invoiced_irr: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="Amount charged to the customer (0 when method is 'none')",
    )
    actual_irr: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="Amount actually paid to the carrier",
    )
    carrier_name: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="Carrier name (free text; the rate table owns the configured rates)",
    )
    tryton_sale_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Engine sale.sale id when the shipment cost is linked to a Tryton sale",
    )
    tryton_shipment_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Engine stock.shipment.out id when linked to a Tryton shipment",
    )
    analytic_account_engine_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Order's analytic account engine id (NULL when the order has no cost centre yet)",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Row creation timestamp (UTC)",
    )

    def __repr__(self) -> str:
        return (
            f"PackagingShipmentCostModel(id={self.id!r}, order_ref={self.order_ref!r}, "
            f"method={self.method!r})"
        )


class PackagingCarrierRateModel(Base):
    """A carrier rate used by the price calculator — user-entered config.

    Maps to the 'packaging_carrier_rates' table (US-515). No rate is
    ever hard-coded: a rate change is a NEW row with a new
    ``effective_from``; old quotes keep the rate they were computed
    with (the quote stores its inputs).
    """

    __tablename__ = "packaging_carrier_rates"
    __table_args__ = (
        CheckConstraint("rate_irr >= 0", name="ck_packaging_carrier_rates_non_negative"),
        Index("ix_packaging_carrier_rates_tenant_company", "tenant_id", "company_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key (carrier-rate id)",
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Owning tenant FK (CASCADE delete)",
    )
    company_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="API company id as supplied (row id or engine id); plain external reference, no FK",
    )
    carrier_name: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Carrier name (free text)",
    )
    rate_irr: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="Rate in whole IRR, per the basis",
    )
    basis: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="per_shipment",
        comment="Rate basis: 'per_shipment' (default) | 'per_kg' | 'per_km' — free string",
    )
    tryton_carrier_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Engine carrier id when the rate is linked to a Tryton carrier",
    )
    effective_from: Mapped[datetime.date | None] = mapped_column(
        Date,
        nullable=True,
        comment="First effective date (Gregorian; NULL = effective immediately)",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        comment="Inactive rates stay in history but are not offered for new quotes",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Row creation timestamp (UTC)",
    )

    def __repr__(self) -> str:
        return (
            f"PackagingCarrierRateModel(id={self.id!r}, carrier_name={self.carrier_name!r}, "
            f"rate_irr={self.rate_irr!r})"
        )


class PackagingCountModel(Base):
    """A periodic materials count (``stock.inventory`` in the engine).

    Maps to the 'packaging_counts' table (US-515). The engine owns
    the inventory document and its lines; this row is the HesabYar-side
    mirror (state + a JSONB snapshot of the lines) so the UI can list
    counts without an engine round-trip per row. ``lines`` stores
    [{material_id, material_name, expected_quantity, counted_quantity,
    tryton_line_id}] with quantities as strings to keep Decimals exact.
    """

    __tablename__ = "packaging_counts"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "tryton_inventory_id",
            name="uq_packaging_counts_tenant_inventory",
        ),
        CheckConstraint(
            "state IN ('draft', 'done', 'cancelled')",
            name="ck_packaging_counts_state",
        ),
        Index("ix_packaging_counts_tenant_company", "tenant_id", "company_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key (count id)",
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Owning tenant FK (CASCADE delete)",
    )
    company_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="API company id as supplied (row id or engine id); plain external reference, no FK",
    )
    tryton_inventory_id: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Engine stock.inventory id",
    )
    location_engine_id: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Counted location engine id (the packaging storage location)",
    )
    location_name: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
        comment="Counted location name at creation (display only)",
    )
    count_date: Mapped[datetime.date] = mapped_column(
        Date,
        nullable=False,
        comment="Count date (Gregorian)",
    )
    state: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="draft",
        comment="Count state: 'draft' | 'done' | 'cancelled'",
    )
    counted_by: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="Who performed the count (actor id/name)",
    )
    lines: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        comment="Line snapshot: [{material_id, material_name, expected_quantity, counted_quantity, tryton_line_id}]",
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

    def __repr__(self) -> str:
        return (
            f"PackagingCountModel(id={self.id!r}, tryton_inventory_id={self.tryton_inventory_id!r}, "
            f"state={self.state!r})"
        )


class PackagingOrderCostCenterModel(Base):
    """One analytic account (cost centre) per order.

    Maps to the 'packaging_order_cost_centers' table (US-515). The
    analytic account lives in Tryton; this row is the HesabYar-side
    link from the store order reference to it.
    """

    __tablename__ = "packaging_order_cost_centers"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "company_id",
            "order_ref",
            name="uq_packaging_cost_centers_tenant_company_order",
        ),
        Index("ix_packaging_cost_centers_tenant_company", "tenant_id", "company_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key (cost-centre id)",
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Owning tenant FK (CASCADE delete)",
    )
    company_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="API company id as supplied (row id or engine id); plain external reference, no FK",
    )
    order_ref: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Store order reference",
    )
    analytic_account_engine_id: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Engine analytic_account.account id (type 'normal' under the orders root)",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Row creation timestamp (UTC)",
    )

    def __repr__(self) -> str:
        return (
            f"PackagingOrderCostCenterModel(id={self.id!r}, order_ref={self.order_ref!r}, "
            f"analytic_account_engine_id={self.analytic_account_engine_id!r})"
        )


class PackagingSettingsModel(Base):
    """Per-company packaging configuration (settings, not secrets).

    Maps to the 'packaging_settings' table (US-515). Location /
    category / root ids are engine ids, set once at provisioning.
    One row per (tenant, company); the service falls back to code
    lookup (PACK / PACK-CONSUME / WH) when an id is unset.
    """

    __tablename__ = "packaging_settings"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "company_id",
            name="uq_packaging_settings_tenant_company",
        ),
        CheckConstraint(
            "cost_method IN ('average', 'fifo')",
            name="ck_packaging_settings_cost_method",
        ),
        CheckConstraint(
            "shipment_cost_method IN ('on_order', 'on_shipment', 'none')",
            name="ck_packaging_settings_shipment_cost_method",
        ),
        CheckConstraint(
            "days_of_cover_window_days >= 1",
            name="ck_packaging_settings_window_positive",
        ),
        CheckConstraint(
            "low_stock_alert_days >= 0",
            name="ck_packaging_settings_alert_non_negative",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key (settings id)",
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Owning tenant FK (CASCADE delete)",
    )
    company_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="API company id as supplied (row id or engine id); plain external reference, no FK",
    )
    cost_method: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="average",
        comment="Default cost method for new materials: 'average' | 'fifo'",
    )
    shipment_cost_method: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="on_order",
        comment="Default shipment-charge method: 'on_order' | 'on_shipment' | 'none'",
    )
    days_of_cover_window_days: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=30,
        comment="Days-of-cover window in days (default 30)",
    )
    low_stock_alert_days: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=7,
        comment="Low-stock alert threshold in days of cover remaining (default 7)",
    )
    packaging_location_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Engine id of the packaging storage location (code PACK)",
    )
    production_location_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Engine id of the consumption (production) location (code PACK-CONSUME)",
    )
    warehouse_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Engine id of the main warehouse (order points attach here)",
    )
    packaging_category_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Engine id of the packaging product category (accounting lives here)",
    )
    analytic_root_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Engine id of the analytic root for per-order cost centres",
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        onupdate=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Last-modified timestamp (UTC)",
    )

    def __repr__(self) -> str:
        return (
            f"PackagingSettingsModel(tenant_id={self.tenant_id!r}, "
            f"company_id={self.company_id!r})"
        )


class PackagingSupplyWatermarkModel(Base):
    """Highest purchase-request engine id the supply check has seen.

    Maps to the 'packaging_supply_watermarks' table (US-515). One row
    per (tenant, company); the watermark is monotonic (never moves
    backwards) so a re-run never re-files the same purchase request.
    """

    __tablename__ = "packaging_supply_watermarks"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "company_id",
            name="uq_packaging_watermarks_tenant_company",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key (watermark id)",
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Owning tenant FK (CASCADE delete)",
    )
    company_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="API company id as supplied (row id or engine id); plain external reference, no FK",
    )
    watermark: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="Highest purchase.request engine id processed (0 = none yet)",
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        onupdate=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Last-modified timestamp (UTC)",
    )

    def __repr__(self) -> str:
        return (
            f"PackagingSupplyWatermarkModel(tenant_id={self.tenant_id!r}, "
            f"company_id={self.company_id!r}, watermark={self.watermark!r})"
        )


class CashBoxModel(Base):
    """One cash box in an installation's registry (US-516, Phase 5 wave 2, K2).

    Maps to the 'cash_boxes' table. The HesabYar-side record for one
    Tryton Statement Journal (the daybook): ``statement_journal_id``
    is the engine id of the ``account.statement.journal`` and
    ``cash_account_id`` / ``cash_account_code`` its cash account.
    One box per (tenant, company, statement journal); petty-cash
    boxes carry a positive ceiling.
    """

    __tablename__ = "cash_boxes"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('store', 'petty_cash', 'other')",
            name="ck_cash_boxes_kind",
        ),
        CheckConstraint(
            "(ceiling_irr IS NULL OR ceiling_irr > 0)",
            name="ck_cash_boxes_ceiling_positive",
        ),
        UniqueConstraint(
            "tenant_id",
            "company_id",
            "statement_journal_id",
            name="uq_cash_boxes_tenant_company_journal",
        ),
        Index("ix_cash_boxes_tenant_company", "tenant_id", "company_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key (cash box id)",
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Owning tenant FK (CASCADE delete)",
    )
    company_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="HesabYar engine_companies row id; plain external reference, no FK",
    )
    engine_company_id: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Tryton company id, denormalized at registration (0013)",
    )
    name_fa: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Cash-box name in Persian (e.g. 'صندوق فروشگاه')",
    )
    kind: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="store | petty_cash | other",
    )
    statement_journal_id: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Engine id of the Tryton account.statement.journal (the daybook)",
    )
    cash_account_id: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Engine id of the journal's cash account",
    )
    cash_account_code: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="Code of the journal's cash account (e.g. '1.1.1')",
    )
    ceiling_irr: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="Petty-cash ceiling in IRR (petty-cash boxes only)",
    )
    active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        comment="Inactive boxes cannot start counting sessions",
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
        nullable=False,
        comment="Last update timestamp (UTC)",
    )

    def __repr__(self) -> str:
        return (
            f"CashBoxModel(id={self.id!r}, name_fa={self.name_fa!r}, "
            f"kind={self.kind!r}, statement_journal_id={self.statement_journal_id!r})"
        )


class CountSessionModel(Base):
    """One supervised cash-counting session (US-516, Phase 5 wave 2, K2).

    Maps to the 'cash_count_sessions' table. A HesabYar-side entity —
    Tryton has no counting session. The book balance is snapshotted
    read-only from the engine at start; the counted amount carries an
    optional denomination breakdown (JSONB list of
    {denomination_irr, count}); the signed minutes carry three
    signatures (cashier + supervisor + manager, only when the
    variance is non-zero). ``review_item_id`` links the
    ``cashbox_variance`` review item (plain external reference, no
    FK); ``draft_journal_id`` / ``posted_journal_id`` are engine move
    ids. The link-extension columns (cheques, bank deposit, orders,
    installments) stay empty until their source modules land.
    """

    __tablename__ = "cash_count_sessions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('in_progress', 'awaiting_supervisor', 'awaiting_manager', 'archived')",
            name="ck_cash_count_sessions_status",
        ),
        CheckConstraint(
            "(counted_amount_irr IS NULL OR counted_amount_irr >= 0)",
            name="ck_cash_count_sessions_counted_non_negative",
        ),
        Index("ix_cash_count_sessions_tenant_box_created", "tenant_id", "cash_box_id", "created_at"),
        Index("ix_cash_count_sessions_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key (session id)",
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Owning tenant FK (CASCADE delete)",
    )
    company_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="HesabYar engine_companies row id; plain external reference, no FK",
    )
    engine_company_id: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Engine company id (denormalized from company_id at session start)",
    )
    cash_box_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("cash_boxes.id", ondelete="CASCADE"),
        nullable=False,
        comment="Cash box FK (CASCADE delete)",
    )
    cashier: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Cashier (actor name/id) who counts",
    )
    supervisor: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Supervisor (actor name/id) who witnesses — differs from the cashier",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="in_progress | awaiting_supervisor | awaiting_manager | archived",
    )
    book_balance_irr: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="Book balance snapshot (IRR, posted moves only, read-only from Tryton)",
    )
    counted_amount_irr: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="Physically counted amount in IRR (None until entered)",
    )
    denominations: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        comment="Optional denomination breakdown: [{denomination_irr, count}]",
    )
    minutes_text: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Signed-minutes body (Persian; required when the variance is non-zero)",
    )
    witnessed_by: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="Supervisor who attended the count (required in every session)",
    )
    witnessed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the supervisor attended (UTC)",
    )
    minutes_cashier_signed_by: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="Cashier who signed the minutes",
    )
    minutes_cashier_signed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the cashier signed the minutes (UTC)",
    )
    minutes_supervisor_signed_by: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="Supervisor who signed the minutes",
    )
    minutes_supervisor_signed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the supervisor signed the minutes (UTC)",
    )
    minutes_manager_signed_by: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="Manager who signed the minutes",
    )
    minutes_manager_signed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the manager signed the minutes (UTC)",
    )
    review_item_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        comment="Linked cashbox_variance review item; plain external reference, no FK",
    )
    draft_journal_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Engine id of the DRAFT variance journal (posted only after manager approval)",
    )
    posted_journal_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Engine id of the variance journal once posted",
    )
    notes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Free-form session notes",
    )
    cheque_refs: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        comment="Link extension point: cheque refs (5ث) — empty until that module lands",
    )
    deposit_ref: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="Link extension point: bank-deposit ref (5ت B1/B2) — empty until it lands",
    )
    order_refs: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        comment="Link extension point: order refs (5ب S1/P1) — empty until they land",
    )
    installment_refs: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        comment="Link extension point: installment refs (5ج) — empty until they land",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Row creation timestamp (UTC) — the session start",
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Last update timestamp (UTC)",
    )

    def __repr__(self) -> str:
        return (
            f"CountSessionModel(id={self.id!r}, cash_box_id={self.cash_box_id!r}, "
            f"status={self.status!r})"
        )


class CashBoxSettingsModel(Base):
    """One installation's cash-box policy settings (US-516, Phase 5 wave 2, K2).

    Maps to the 'cashbox_settings' table. One row per tenant (the
    tenant id IS the primary key); a missing row means the defaults
    apply (policy a, 7-day reminder, daily schedule, supervisor
    control on, 24-hour policy-b settlement).
    """

    __tablename__ = "cashbox_settings"
    __table_args__ = (
        CheckConstraint(
            "variance_policy IN ('expense', 'receivable_from_cashier')",
            name="ck_cashbox_settings_variance_policy",
        ),
        CheckConstraint(
            "(material_variance_threshold_irr IS NULL OR material_variance_threshold_irr >= 0)",
            name="ck_cashbox_settings_threshold_non_negative",
        ),
        CheckConstraint(
            "count_reminder_days >= 1",
            name="ck_cashbox_settings_reminder_days_positive",
        ),
        CheckConstraint(
            "count_schedule IN ('daily', 'weekly', 'period_end')",
            name="ck_cashbox_settings_count_schedule",
        ),
        CheckConstraint(
            "shortage_settlement_hours >= 1",
            name="ck_cashbox_settings_settlement_hours_positive",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        primary_key=True,
        comment="Owning tenant FK (CASCADE delete) — one settings row per tenant",
    )
    variance_policy: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="expense (policy a, default) | receivable_from_cashier (policy b)",
    )
    material_variance_threshold_irr: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="Material-variance threshold in IRR (None = no materiality split)",
    )
    count_reminder_days: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=7,
        comment="'Cash box uncounted for N days' alert threshold",
    )
    count_schedule: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="daily",
        comment="daily | weekly | period_end",
    )
    supervisor_required: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        comment="Internal control: the supervisor must differ from the cashier",
    )
    shortage_settlement_hours: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=24,
        comment="Shortage-settlement deadline under policy (b), in hours",
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Last update timestamp (UTC)",
    )

    def __repr__(self) -> str:
        return (
            f"CashBoxSettingsModel(tenant_id={self.tenant_id!r}, "
            f"variance_policy={self.variance_policy!r})"
        )


class GatewayPaymentModel(Base):
    """One gateway payment (phased design §3.2الف).

    Maps to the 'gateway_payments' table. The fee % and VAT rate
    actually applied are snapshotted on the row; a later contract
    version never rewrites them. UNIQUE on (payment_channel_id,
    gateway_transaction_id).
    """

    __tablename__ = "gateway_payments"
    __table_args__ = (
        UniqueConstraint(
            "payment_channel_id",
            "gateway_transaction_id",
            name="uq_gateway_payments_channel_transaction",
        ),
        Index(
            "ix_gateway_payments_installation_channel",
            "installation_id",
            "payment_channel_id",
        ),
        Index(
            "ix_gateway_payments_installation_period",
            "installation_id",
            "sales_period_jalali",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    installation_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        comment="Installation id (from the authenticated API key)",
    )
    payment_channel_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("payment_channels.id", ondelete="CASCADE"),
        nullable=False,
        comment="Owning payment channel row",
    )
    gateway_transaction_id: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        comment="Gateway-side transaction id (unique per channel)",
    )
    store_order_id: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
        comment="Store order id, when the payment came from the store",
    )
    store_payment_id: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
        comment="Store payment id, when the payment came from the store",
    )
    tryton_invoice_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Tryton account.invoice id (external reference, no SQL FK into Tryton)",
    )
    tryton_journal_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Tryton account.move id of the sale journal (external reference)",
    )
    customer_party_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Tryton party id of the CUSTOMER (the settlement receivable is from the gateway)",
    )
    gross_irr: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="Sale amount in whole rials",
    )
    fee_irr: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="Gateway fee in whole rials (derived at recording time)",
    )
    fee_vat_irr: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="VAT on the fee in whole rials (derived at recording time)",
    )
    net_irr: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="Net receivable from the gateway in whole rials (gross - fee - fee VAT)",
    )
    contract_version_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("gateway_contracts.id", ondelete="RESTRICT"),
        nullable=False,
        comment="Contract version the schedule was derived from (immutable once recorded)",
    )
    fee_rate_snapshot: Mapped[Decimal] = mapped_column(
        Numeric(8, 4),
        nullable=False,
        comment="Fee percent actually applied (10 means 10%) — the contract rate at recording time",
    )
    fee_vat_rate_snapshot: Mapped[Decimal | None] = mapped_column(
        Numeric(8, 4),
        nullable=True,
        comment="Fee VAT rate actually applied (10 means 10%) — NULL only when unknown",
    )
    sales_period_jalali: Mapped[str] = mapped_column(
        String(7),
        nullable=False,
        comment="Jalali sales period 'YYYY/MM' from gateway_confirmed_at",
    )
    gateway_confirmed_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="When the gateway confirmed the transaction (the schedule anchor)",
    )
    source: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="Payment origin: 'store_webhook' | 'gateway_poll' | 'manual_import' | 'manual_entry'",
    )
    canonical_event_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        comment="Inbox event id this payment was recorded from, when any",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="scheduled",
        comment="Payment lifecycle: 'scheduled' | 'settling' | 'settled' | 'partially_cancelled' | 'cancelled'",
    )
    refunds_deducted_irr: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        comment="Amount already-paid tranches contribute as a negative adjustment on later settlements",
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

    tranches: Mapped[list[SettlementTrancheModel]] = relationship(
        "SettlementTrancheModel",
        back_populates="payment",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return (
            f"GatewayPaymentModel(id={self.id!r}, channel_id={self.payment_channel_id!r}, "
            f"gateway_transaction_id={self.gateway_transaction_id!r})"
        )


class SettlementTrancheModel(Base):
    """One expected settlement instalment (phased design §3.2–§3.3).

    Maps to the 'settlement_tranches' table. One row per leg of the
    payment's contract schedule; ``expected_date`` is the due date
    and the ``maturity_date`` of the Tryton receivable line.
    """

    __tablename__ = "settlement_tranches"
    __table_args__ = (
        UniqueConstraint(
            "gateway_payment_id",
            "tranche_index",
            name="uq_settlement_tranches_payment_index",
        ),
        Index(
            "ix_settlement_tranches_installation_state",
            "installation_id",
            "state",
        ),
        Index(
            "ix_settlement_tranches_installation_due",
            "installation_id",
            "expected_date",
        ),
        Index("ix_settlement_tranches_payment", "gateway_payment_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    installation_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        comment="Installation id (from the authenticated API key)",
    )
    gateway_payment_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("gateway_payments.id", ondelete="CASCADE"),
        nullable=False,
        comment="Owning gateway payment row",
    )
    tranche_index: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="1-based tranche number within the payment (قسط ۱..N)",
    )
    tranche_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Total tranches of the payment's schedule",
    )
    sales_period_jalali: Mapped[str] = mapped_column(
        String(7),
        nullable=False,
        comment="Jalali sales period 'YYYY/MM' (copied from the payment; corrected on period reassignment)",
    )
    expected_window_start: Mapped[datetime.date] = mapped_column(
        Date,
        nullable=False,
        comment="First day of the payout window (Gregorian)",
    )
    expected_date: Mapped[datetime.date] = mapped_column(
        Date,
        nullable=False,
        comment="Due date (Gregorian) — also the maturity_date of the Tryton receivable line",
    )
    gross_irr: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="Tranche gross share in whole rials",
    )
    fee_irr: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="Tranche fee share in whole rials",
    )
    fee_vat_irr: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="Tranche fee-VAT share in whole rials",
    )
    net_irr: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="Tranche net share in whole rials (expected settlement amount)",
    )
    confirmed_net_irr: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        comment="Net amount confirmed by the gateway file/portal so far",
    )
    received_net_irr: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        comment="Net amount matched to bank deposits so far",
    )
    state: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="expected",
        comment="Tranche state (§3.3)",
    )
    tryton_receivable_line_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Tryton account.move.line id of the receivable (external reference; maturity_date = expected_date)",
    )
    variance_irr: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="Signed variance (actual - expected) in rials, once a file/deposit disagrees",
    )
    variance_reason: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Why the tranche differs from the schedule (never absorbed silently)",
    )
    match_method: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="C5 matching rule that matched this tranche",
    )
    match_confidence: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 4),
        nullable=True,
        comment="Match confidence 0..1 (NULL when unmatched)",
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

    payment: Mapped[GatewayPaymentModel] = relationship(
        "GatewayPaymentModel",
        back_populates="tranches",
    )

    def __repr__(self) -> str:
        return (
            f"SettlementTrancheModel(id={self.id!r}, payment_id={self.gateway_payment_id!r}, "
            f"index={self.tranche_index!r}, state={self.state!r})"
        )


class SettlementEventModel(Base):
    """One gateway settlement event (phased design §3.4 C1).

    Maps to the 'settlement_events' table.
    """

    __tablename__ = "settlement_events"
    __table_args__ = (
        Index(
            "ix_settlement_events_installation_channel",
            "installation_id",
            "payment_channel_id",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    installation_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        comment="Installation id (from the authenticated API key)",
    )
    payment_channel_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("payment_channels.id", ondelete="CASCADE"),
        nullable=False,
        comment="Owning payment channel row",
    )
    payout_date: Mapped[datetime.date] = mapped_column(
        Date,
        nullable=False,
        comment="Gateway payout date (Gregorian)",
    )
    net_irr: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="Net amount the gateway says it paid, in whole rials",
    )
    deductions_irr: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        comment="Documented deductions of the period (prior-period debt/cancels from the statement)",
    )
    tracking_ref: Mapped[str | None] = mapped_column(
        String(256),
        nullable=True,
        comment="Gateway tracking/reference number for the payout",
    )
    destination_iban: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Destination IBAN the gateway paid to",
    )
    gateway_batch_ref: Mapped[str | None] = mapped_column(
        String(256),
        nullable=True,
        comment="Gateway batch reference, when the payout is batched",
    )
    source: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="Event origin: 'payouts_file' | 'portal_manual' | 'detail_file_derived'",
    )
    import_batch_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        comment="Import batch this event came from, when any",
    )
    canonical_event_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        comment="Canonical payment.settlement.received event id, when published",
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

    def __repr__(self) -> str:
        return (
            f"SettlementEventModel(id={self.id!r}, channel_id={self.payment_channel_id!r}, "
            f"payout_date={self.payout_date!r})"
        )


class BankDepositModel(Base):
    """One bank deposit (phased design §3.4 C4).

    Maps to the 'bank_deposits' table.
    """

    __tablename__ = "bank_deposits"
    __table_args__ = (
        Index(
            "ix_bank_deposits_installation_date",
            "installation_id",
            "deposit_date",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    installation_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        comment="Installation id (from the authenticated API key)",
    )
    deposit_date: Mapped[datetime.date] = mapped_column(
        Date,
        nullable=False,
        comment="Bank deposit date (Gregorian)",
    )
    amount_irr: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="Deposit amount in whole rials",
    )
    bank_account_ref: Mapped[str | None] = mapped_column(
        String(256),
        nullable=True,
        comment="Bank account reference (IBAN or account number)",
    )
    description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Bank statement description, as imported",
    )
    tracking_ref: Mapped[str | None] = mapped_column(
        String(256),
        nullable=True,
        comment="Gateway tracking number found in the description, when any",
    )
    bank_import_row_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("bank_import_rows.id", ondelete="SET NULL"),
        nullable=True,
        comment="US-514 bank-statement import row this deposit came from (NULL for manual deposits)",
    )
    source: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="statement_import",
        comment="Deposit origin: 'statement_import' | 'manual'",
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

    def __repr__(self) -> str:
        return (
            f"BankDepositModel(id={self.id!r}, deposit_date={self.deposit_date!r}, "
            f"amount_irr={self.amount_irr!r})"
        )


class SettlementMatchModel(Base):
    """One tranche↔deposit/event match link (phased design §3.4 C5).

    Maps to the 'settlement_matches' table. At least one of the two
    targets must be set (CHECK constraint).
    """

    __tablename__ = "settlement_matches"
    __table_args__ = (
        CheckConstraint(
            "bank_deposit_id IS NOT NULL OR settlement_event_id IS NOT NULL",
            name="ck_settlement_matches_has_target",
        ),
        Index("ix_settlement_matches_tranche", "tranche_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    installation_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        comment="Installation id (from the authenticated API key)",
    )
    tranche_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("settlement_tranches.id", ondelete="CASCADE"),
        nullable=False,
        comment="Matched tranche row",
    )
    bank_deposit_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("bank_deposits.id", ondelete="CASCADE"),
        nullable=True,
        comment="Matched bank deposit (NULL when the match is event-only)",
    )
    settlement_event_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("settlement_events.id", ondelete="CASCADE"),
        nullable=True,
        comment="Matched settlement event (NULL when the match is deposit-only)",
    )
    allocated_amount_irr: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="Amount allocated by this link, in whole rials",
    )
    method: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="C5 matching rule: 'exact_reference' | 'amount_and_date' | 'amount_only' | 'manual'",
    )
    confidence: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 4),
        nullable=True,
        comment="Match confidence 0..1 (NULL for manual matches)",
    )
    matched_by: Mapped[str | None] = mapped_column(
        String(256),
        nullable=True,
        comment="Actor that matched (user id, or 'settlement-worker' for automatic matches)",
    )
    is_automatic: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        comment="True for automatic matches, False for manual ones",
    )
    matched_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the match was made (UTC)",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Row creation timestamp (UTC)",
    )

    def __repr__(self) -> str:
        return (
            f"SettlementMatchModel(id={self.id!r}, tranche_id={self.tranche_id!r}, "
            f"allocated_amount_irr={self.allocated_amount_irr!r})"
        )


class SettlementExceptionModel(Base):
    """One settlement exception (phased design §3.7).

    Maps to the 'settlement_exceptions' table.
    """

    __tablename__ = "settlement_exceptions"
    __table_args__ = (
        Index(
            "ix_settlement_exceptions_installation_status",
            "installation_id",
            "status",
        ),
        Index("ix_settlement_exceptions_tranche", "tranche_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    installation_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        comment="Installation id (from the authenticated API key)",
    )
    exception_type: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Exception type (§3.7)",
    )
    severity: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="Severity: 'info' | 'warning' | 'critical'",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="open",
        comment="Lifecycle: 'open' | 'in_review' | 'resolved' | 'written_off'",
    )
    tranche_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("settlement_tranches.id", ondelete="SET NULL"),
        nullable=True,
        comment="Related tranche, when the exception is about one",
    )
    settlement_event_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("settlement_events.id", ondelete="SET NULL"),
        nullable=True,
        comment="Related settlement event, when any",
    )
    bank_deposit_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("bank_deposits.id", ondelete="SET NULL"),
        nullable=True,
        comment="Related bank deposit, when any",
    )
    gateway_payment_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("gateway_payments.id", ondelete="SET NULL"),
        nullable=True,
        comment="Related gateway payment, when any",
    )
    expected_amount_irr: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="Expected amount in whole rials, when the exception is about an amount",
    )
    actual_amount_irr: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="Actual amount in whole rials, when the exception is about an amount",
    )
    escalation_level: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="Overdue escalation level (0=none, 1=7d, 2=30d, 3=60d past due)",
    )
    resolution: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment=(
            "How it was resolved: 'confirmed_ok' | 'adjusted' | 'write_off' | "
            "'escalated_to_gateway' | 'rescheduled'"
        ),
    )
    resolved_by: Mapped[str | None] = mapped_column(
        String(256),
        nullable=True,
        comment="Actor that resolved the exception",
    )
    resolved_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the exception was resolved (UTC)",
    )
    note: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Operator note (mandatory context on resolution)",
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

    def __repr__(self) -> str:
        return (
            f"SettlementExceptionModel(id={self.id!r}, type={self.exception_type!r}, "
            f"status={self.status!r})"
        )


class SettlementFeeRecordModel(Base):
    """Fee tracking for one payment/tranche (phased design §3.6).

    Maps to the 'settlement_fee_records' table.
    """

    __tablename__ = "settlement_fee_records"
    __table_args__ = (
        Index("ix_settlement_fee_records_payment", "gateway_payment_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    installation_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        comment="Installation id (from the authenticated API key)",
    )
    gateway_payment_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("gateway_payments.id", ondelete="CASCADE"),
        nullable=False,
        comment="Owning gateway payment row",
    )
    tranche_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("settlement_tranches.id", ondelete="SET NULL"),
        nullable=True,
        comment="Tranche this record is about (NULL = whole payment)",
    )
    contract_version_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("gateway_contracts.id", ondelete="RESTRICT"),
        nullable=False,
        comment="Contract version the expected figures came from",
    )
    fee_percent_applied: Mapped[Decimal] = mapped_column(
        Numeric(8, 4),
        nullable=False,
        comment="Fee percent applied (10 means 10%)",
    )
    fee_vat_rate_applied: Mapped[Decimal | None] = mapped_column(
        Numeric(8, 4),
        nullable=True,
        comment="Fee VAT rate applied (10 means 10%)",
    )
    expected_fee_irr: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="Expected fee in whole rials (from the contract)",
    )
    actual_fee_irr: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="Actual fee in whole rials (from the gateway file/portal)",
    )
    fee_variance_irr: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="Fee variance (actual - expected) in whole rials",
    )
    expected_vat_irr: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="Expected fee VAT in whole rials (from the contract)",
    )
    actual_vat_irr: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="Actual fee VAT in whole rials (from the gateway file/portal)",
    )
    vat_variance_irr: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="Fee-VAT variance (actual - expected) in whole rials",
    )
    tryton_fee_journal_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Tryton account.move id of the fee journal (external reference)",
    )
    source: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="calculated",
        comment="Figure origin: 'calculated' | 'file' | 'portal_statement'",
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

    def __repr__(self) -> str:
        return (
            f"SettlementFeeRecordModel(id={self.id!r}, payment_id={self.gateway_payment_id!r}, "
            f"expected_fee_irr={self.expected_fee_irr!r})"
        )


class SettlementPostingModel(Base):
    """One settlement posting: the audit ledger for §3.5 journals (US-522).

    Maps to the 'settlement_postings' table. One row per journal the
    settlement engine posts to Tryton — the idempotency key (one
    posting per (payment/tranche, kind) for the once-only kinds),
    the Tryton move/payment/reconciliation ids, the amount, and the
    reason + event reference for adjustments.
    """

    __tablename__ = "settlement_postings"
    __table_args__ = (
        Index("ix_settlement_postings_payment", "gateway_payment_id"),
        Index("ix_settlement_postings_tranche", "tranche_id"),
        Index(
            "ix_settlement_postings_installation_kind",
            "installation_id",
            "kind",
        ),
        Index(
            "uq_settlement_postings_tranche_kind",
            "tranche_id",
            "kind",
            unique=True,
            postgresql_where=text(
                "tranche_id IS NOT NULL AND kind = 'fee_at_settlement'"
            ),
        ),
        Index(
            "uq_settlement_postings_payment_kind",
            "gateway_payment_id",
            "kind",
            unique=True,
            postgresql_where=text("tranche_id IS NULL AND kind = 'fee_at_sale'"),
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    installation_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        comment="Installation id (from the authenticated API key)",
    )
    gateway_payment_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("gateway_payments.id", ondelete="CASCADE"),
        nullable=False,
        comment="Owning gateway payment row",
    )
    tranche_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("settlement_tranches.id", ondelete="SET NULL"),
        nullable=True,
        comment="Tranche this posting is for, when tranche-scoped",
    )
    kind: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="Posting kind (fee_at_sale / clearing / bank_receipt / ...)",
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="posted",
        comment="posted (Tryton move posted) or draft (awaiting approval)",
    )
    amount_irr: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="The posting's principal amount in whole rials",
    )
    tryton_move_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Tryton account.move id (external reference)",
    )
    tryton_payment_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Tryton account.payment id, for clearing postings (external reference)",
    )
    tryton_reconciliation_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Tryton account.move.reconciliation id (external reference)",
    )
    reason: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Reason for the posting (mandatory for adjustments/refunds)",
    )
    event_ref: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
        comment="Canonical event / refund event reference for the posting",
    )
    details: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Posting breakdown (refund allocations, tie-out figures, variance figures)",
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

    def __repr__(self) -> str:
        return (
            f"SettlementPostingModel(id={self.id!r}, payment_id={self.gateway_payment_id!r}, "
            f"kind={self.kind!r}, amount_irr={self.amount_irr!r})"
        )


class SettlementFileImportModel(Base):
    """One imported gateway settlement file (phased design §3.2ب).

    Maps to the 'settlement_file_imports' table. UNIQUE on
    (payment_channel_id, file_hash_sha256).
    """

    __tablename__ = "settlement_file_imports"
    __table_args__ = (
        UniqueConstraint(
            "payment_channel_id",
            "file_hash_sha256",
            name="uq_settlement_file_imports_channel_hash",
        ),
        Index(
            "ix_settlement_file_imports_installation_channel",
            "installation_id",
            "payment_channel_id",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    installation_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        comment="Installation id (from the authenticated API key)",
    )
    payment_channel_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("payment_channels.id", ondelete="CASCADE"),
        nullable=False,
        comment="Owning payment channel row",
    )
    file_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="File kind: 'detail' | 'payouts' | 'statement'",
    )
    period_jalali: Mapped[str] = mapped_column(
        String(7),
        nullable=False,
        comment="Jalali period the file covers ('YYYY/MM')",
    )
    file_hash_sha256: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="SHA-256 of the uploaded file bytes (dedup key)",
    )
    import_batch_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        comment="Batch id grouping the whole import (reviewable/rollbackable as one unit)",
    )
    uploaded_by: Mapped[str | None] = mapped_column(
        String(256),
        nullable=True,
        comment="Actor that uploaded the file",
    )
    uploaded_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the file was uploaded (UTC)",
    )
    row_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="Parsed rows in the file (excluding dropped Net Sale/total rows)",
    )
    matched_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="Rows matched to existing tranches",
    )
    new_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="Rows that created new payments",
    )
    error_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="Rows that failed to parse or match",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="imported",
        comment=(
            "Import lifecycle (US-523): 'awaiting_review' | 'approved' | "
            "'rejected' | 'exception' | 'settled'. 'imported' is the "
            "transient pre-review state."
        ),
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

    rows: Mapped[list[SettlementFileRowModel]] = relationship(
        "SettlementFileRowModel",
        back_populates="file_import",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return (
            f"SettlementFileImportModel(id={self.id!r}, channel_id={self.payment_channel_id!r}, "
            f"file_type={self.file_type!r})"
        )


class SettlementFileRowModel(Base):
    """One parsed row of an imported settlement file (§3.2ب).

    Maps to the 'settlement_file_rows' table.
    """

    __tablename__ = "settlement_file_rows"
    __table_args__ = (
        Index("ix_settlement_file_rows_import", "import_id", "row_no"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    import_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("settlement_file_imports.id", ondelete="CASCADE"),
        nullable=False,
        comment="Owning file import row",
    )
    row_no: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="1-based row number in the file",
    )
    raw_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        comment="Row exactly as parsed (amounts as strings to stay exact)",
    )
    gateway_transaction_id: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
        comment="Gateway transaction id from the row, when present",
    )
    tran_type: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Row transaction type, as the file names it",
    )
    amount_irr: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="Row gross amount in whole rials, when present",
    )
    fee_irr: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="Row fee in whole rials, when present",
    )
    net_irr: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="Row net amount in whole rials, when present",
    )
    transaction_date: Mapped[datetime.date | None] = mapped_column(
        Date,
        nullable=True,
        comment="Row transaction date (Gregorian), when present",
    )
    tracking_ref: Mapped[str | None] = mapped_column(
        String(256),
        nullable=True,
        comment="Row tracking/reference number, when present",
    )
    matched_tranche_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("settlement_tranches.id", ondelete="SET NULL"),
        nullable=True,
        comment="Tranche this row matched, when matched",
    )
    match_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="unknown_reference",
        comment="Match outcome: 'matched' | 'new_payment' | 'unknown_reference' | 'duplicate' | 'amount_mismatch'",
    )
    note: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Parse/match note for this row",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Row creation timestamp (UTC)",
    )

    file_import: Mapped[SettlementFileImportModel] = relationship(
        "SettlementFileImportModel",
        back_populates="rows",
    )

    def __repr__(self) -> str:
        return (
            f"SettlementFileRowModel(id={self.id!r}, import_id={self.import_id!r}, "
            f"row_no={self.row_no!r})"
        )


# ---------------------------------------------------------------------------
# Cheques (Phase 5-4, US-531)
# ---------------------------------------------------------------------------


class ChequeModel(Base):
    """One cheque in the HesabYar registry (phased design §5 §1.1/§2.1).

    Maps to the 'cheques' table. The accounting truth lives in the
    engine's ``account.check`` record (``engine_id``); this row
    mirrors its state and carries the HesabYar-side fields (the 24
    payload fields, storage, bounce, endorsement, links). ``move_ids``
    maps transition name -> engine move id (the audit trail, report
    17). A live cheque's ``sayad_id`` is unique — the partial index
    excludes ``voided`` / ``expired_unreceived`` /
    ``rejected_mismatch`` rows, so a leaf that was voided, expired
    or was rejected may be registered again as a NEW record (the old
    one never revives).
    """

    __tablename__ = "cheques"
    __table_args__ = (
        Index(
            "uq_cheques_sayad_id_live",
            "sayad_id",
            unique=True,
            postgresql_where=text(
                "state NOT IN ('voided', 'expired_unreceived', 'rejected_mismatch')"
            ),
        ),
        Index("ix_cheques_installation_state", "installation_id", "state"),
        Index("ix_cheques_installation_issuer", "installation_id", "issuer_national_id"),
        Index("ix_cheques_installation_due_date", "installation_id", "due_date"),
        Index("ix_cheques_installation_purpose", "installation_id", "purpose"),
        Index("ix_cheques_installation_kind", "installation_id", "kind"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        comment="Tenant id (from the authenticated API key)",
    )
    installation_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        comment="Installation id (from the authenticated API key)",
    )
    company_engine_id: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Engine company id holding this cheque's books",
    )
    kind: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="received",
        comment="Cheque kind: 'received' | 'issued'",
    )
    purpose: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="payment",
        comment="Cheque purpose: 'payment' (commercial) | 'collateral' (memorandum only)",
    )
    sayad_id: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="16-digit Sayyad identifier (distinct from the leaf serial number)",
    )
    amount_irr: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="Cheque amount in Rial (payload Toman x10 at the boundary)",
    )
    issue_date: Mapped[datetime.date] = mapped_column(
        Date,
        nullable=False,
        comment="Issue date on the leaf (Gregorian; payload is Jalali)",
    )
    due_date: Mapped[datetime.date] = mapped_column(
        Date,
        nullable=False,
        comment="Due date on the leaf (Gregorian; payload is Jalali)",
    )
    issuer_name: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
        comment="Issuer name (customer or third party)",
    )
    issuer_national_id: Mapped[str] = mapped_column(
        String(11),
        nullable=False,
        comment="Issuer national id: 10 digits (natural) or 11 digits (legal)",
    )
    issuer_bank: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Issuing bank: 3-digit code + name, e.g. '056 سامان'",
    )
    payee_name: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
        comment="Payee registered in Sayyad (must be the business)",
    )
    payee_national_id: Mapped[str] = mapped_column(
        String(11),
        nullable=False,
        comment="Payee national id: 10 digits (natural) or 11 digits (legal)",
    )
    order_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Site order id (correlation_id of the order event)",
    )
    customer_ref: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Site customer reference (email/phone/customer code)",
    )
    sayad_registered: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="Site's claim that the issuer registered the leaf in Sayyad (verified by inquiry before held)",
    )
    source: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="manual",
        comment="Registration source: 'manual' | 'site'",
    )
    source_event_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="§10.2 source_event_id of the site event that created this intake (US-533 traceability)",
    )
    state: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="registered",
        comment="Accounting state (§5 §2.1); separate from sayad_status",
    )
    sayad_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="unregistered",
        comment="Sayyad status: 'unregistered' | 'registered' | 'confirmed' | 'transferred'",
    )
    sayad_unconfirmed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="True when sayad_registered=false: 'do not accept an unregistered cheque' (ruling 870)",
    )
    leaf_number: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Serial number printed on the leaf (for bank-statement matching)",
    )
    issuer_branch: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="Issuing bank branch name/code",
    )
    issuer_account_number: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Issuer current-account number, when known",
    )
    reason: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="'بابت' — the reason for the cheque, as registered in Sayyad",
    )
    invoice_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="Linked HesabYar/Tryton invoice id, when the order was invoiced",
    )
    installment_leg_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="Linked installment leg id (the leg this cheque pays; ۵ج consumes the leg-status hook)",
    )
    applied_amount_irr: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="Amount allocated to the linked leg when it differs from the cheque amount",
    )
    scan_url: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Signed scan/photo URL from the site (downloaded into the filestore by US-533)",
    )
    scan_ref: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Scan file reference in the site, when no scan_url was given",
    )
    scan_filestore_ref: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Filestore ref of the downloaded leaf scan (cheques/<sayad_id>-<hash>.<ext>), US-533",
    )
    registered_by: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="Who registered the cheque on the site (operator/customer)",
    )
    notes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Free-form note",
    )
    storage_location: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="Physical leaf location: 'cashbox' | 'vault' | 'bank' | 'with_endorsee' | 'returned_to_issuer'",
    )
    engine_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Engine account.check record id",
    )
    move_ids: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        comment="Transition name -> engine move id (audit trail, report 17)",
    )
    destination_bank_account_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Destination bank account (engine account id) chosen at deposit",
    )
    destination_bank_name: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="Destination bank account display name",
    )
    deposit_date: Mapped[datetime.date | None] = mapped_column(
        Date,
        nullable=True,
        comment="Day the leaf was handed to the bank (start of the clearing window, US-534); NULL before US-534",
    )
    clearing_confidence: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
        comment="Confidence of the clearing match that cleared this cheque ('exact' | 'probable', US-534)",
    )
    clearing_statement_ref: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="Statement line that cleared this cheque ('statement:<id>:line:<id>', US-534)",
    )
    clearing_reconciliation_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Engine reconciliation id joining the deposit/clear clearing legs (سند ۲/۳), US-534",
    )
    issuer_color_code: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Issuer colour code 1-5 at pre-acceptance (point in time; last week's colour is not valid today)",
    )
    issuer_color_inquired_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the issuer colour was inquired",
    )
    issuer_back_cheques: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Issuer bounced-cheque count from the back-cheques inquiry",
    )
    issuer_in_transit_commitments_irr: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="Issuer in-transit (Sayyad) commitments in Rial at pre-acceptance",
    )
    manager_approval_by: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="Manager who approved a red/brown (or high-commitment) cheque",
    )
    manager_approval_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the manager approval was recorded",
    )
    receipt_missing_warning: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="True when a transition was recorded without an inquiry receipt (warning; mismatch report)",
    )
    bounce_reason: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Bounce reason recorded at the bounce transition",
    )
    bounce_date: Mapped[datetime.date | None] = mapped_column(
        Date,
        nullable=True,
        comment="Bounce date recorded at the bounce transition",
    )
    bounce_certificate_reference: Mapped[str | None] = mapped_column(
        String(256),
        nullable=True,
        comment="Bank non-payment certificate / inquiry receipt reference",
    )
    dunning_notified_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the dunning notification for the bounce was filed",
    )
    endorsee_name: Mapped[str | None] = mapped_column(
        String(256),
        nullable=True,
        comment="Endorsee name (supplier or other person)",
    )
    endorsee_type: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
        comment="Endorsee type: 'supplier' | 'other'",
    )
    sayad_transfer_reference: Mapped[str | None] = mapped_column(
        String(256),
        nullable=True,
        comment="Sayyad transfer registration receipt for the endorsement",
    )
    journal_pending_review: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="True when a transition moved state but its journal is accountant-gated (§9.1 #12)",
    )
    installment_leg_status: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="Leg-status effect of the last transition ('paid' on clear, 'unpaid' on bounce; ۵ج)",
    )
    mismatch_detail: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="What mismatched (site vs leaf vs Sayyad) at rejection",
    )
    reminder_day3_sent_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the day-3 never-arrived reminder was filed (job idempotency)",
    )
    reminder_final_sent_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the day-(N-1) never-arrived reminder was filed (job idempotency)",
    )
    secured_obligation: Mapped[str | None] = mapped_column(
        String(256),
        nullable=True,
        comment="Obligation a collateral cheque secures — report 6 dimension (US-532)",
    )
    checkbook_engine_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Engine checkbook id the issued leaf was drawn from (US-532)",
    )
    call_in_approved_by: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="Accountant who approved this case's call-in (§9.1 #4, §5 §4; US-532)",
    )
    call_in_approved_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the call-in was approved",
    )
    void_reason: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
        comment="Why an issued cheque was voided (سند ۱۳ audit trail; US-532)",
    )
    unexpected_call_in: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="True when an issued collateral cheque was presented at the bank without notice (US-532)",
    )
    call_in_statement_reference: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="Bank-statement withdrawal reference matched by leaf number for an unexpected call-in (US-532)",
    )
    stale_in_clearing_filed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the stale_in_clearing review item was filed (US-533 daily job; once per cheque)",
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

    def __repr__(self) -> str:
        return (
            f"ChequeModel(id={self.id!r}, sayad_id={self.sayad_id!r}, "
            f"state={self.state!r})"
        )


class ChequeCustodyLogModel(Base):
    """One append-only physical-leaf move (phased design §5 §1.1).

    Maps to the 'cheque_custody_log' table. Origin/destination/time/
    actor/reason; a custody move is NEVER a journal. Rows are never
    updated or deleted.
    """

    __tablename__ = "cheque_custody_log"
    __table_args__ = (
        Index("ix_cheque_custody_log_cheque", "cheque_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    cheque_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("cheques.id"),
        nullable=False,
        comment="Cheque this custody entry belongs to",
    )
    from_location: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="Previous physical location (NULL when the leaf first arrives)",
    )
    to_location: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="New physical location",
    )
    actor: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Actor who moved the leaf",
    )
    reason: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Why the leaf moved",
    )
    at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="When the leaf moved (UTC)",
    )

    def __repr__(self) -> str:
        return (
            f"ChequeCustodyLogModel(id={self.id!r}, cheque_id={self.cheque_id!r}, "
            f"to_location={self.to_location!r})"
        )


class ChequeInquiryReceiptModel(Base):
    """One structured Sayyad inquiry receipt (phased design §5 §1.4).

    Maps to the 'cheque_inquiry_receipts' table. Only verdict/code/
    time/reference are stored — never the raw inquiry response and
    never full national IDs (§3.5).
    """

    __tablename__ = "cheque_inquiry_receipts"
    __table_args__ = (
        Index("ix_cheque_inquiry_receipts_cheque", "cheque_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    cheque_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("cheques.id"),
        nullable=False,
        comment="Cheque this receipt belongs to",
    )
    channel: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="Channel: 'sms' | 'cbi_website' | 'bank_app' | 'payment_app' | 'finnotech_api' | 'ehrazino_api'",
    )
    inquiry_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="Type: 'color'|'leaf_status'|'items_match'|'issuer_history'|'back_cheques'|'transfer_chain'",
    )
    result_code: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Result code, e.g. 'color=3' or 'items_match=true' (verdict only, no PII)",
    )
    result_summary: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Short summary of the result (never the full response)",
    )
    reference: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
        comment="Response tracking number / redacted reference (no names or full national ids)",
    )
    recorded_by: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Who recorded the receipt",
    )
    cost_irr: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        comment="Inquiry cost in Rial (SMS 350 Toman = 3,500 Rial)",
    )
    requested_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the inquiry was requested",
    )
    responded_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the inquiry response arrived",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Row creation timestamp (UTC)",
    )

    def __repr__(self) -> str:
        return (
            f"ChequeInquiryReceiptModel(id={self.id!r}, cheque_id={self.cheque_id!r}, "
            f"inquiry_type={self.inquiry_type!r})"
        )


class ChequeSettingsModel(Base):
    """Per-installation cheque policy (phased design §5 §3.4/§9.1).

    Maps to the 'cheque_settings' table (one row per installation).
    Auto-post is deliberately NOT here — it lives in the installation
    settings + per-event policy rows the F3/F4 gate reads.
    """

    __tablename__ = "cheque_settings"

    installation_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        comment="Installation id (from the authenticated API key)",
    )
    leaf_arrival_deadline_days: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=7,
        comment="Days a site-registered leaf has to arrive before it expires (default 7, §9.1 #6)",
    )
    clearing_window_days: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        default=None,
        comment="Clearing window (days) — no hard-coded default (§9.1 #6); unset until set",
    )
    bounce_destination: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="receivables",
        comment="Bounce claim destination: 'receivables' | 'bounced_receivables' (§9.1 #2)",
    )
    red_brown_policy: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="manager_approval",
        comment="Red/brown-colour policy: 'reject' | 'manager_approval'",
    )
    max_in_transit_commitments_irr: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="In-transit commitments threshold in Rial (NULL = no threshold)",
    )
    default_storage_location: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="vault",
        comment="Default physical location recorded at held (§9.1 #14)",
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

    def __repr__(self) -> str:
        return f"ChequeSettingsModel(installation_id={self.installation_id!r})"


class ChequeMatchHistoryModel(Base):
    """One Jalali month of clearing-match outcomes (US-553, report 12).

    Maps to the 'cheque_match_history' table. One row per
    (installation, Jalali month): the clearing service adds every
    run's newly decided line counts to its month (upsert-add), and
    the trend chart renders FROM these rows — never recomputed from
    raw match records at read time.
    """

    __tablename__ = "cheque_match_history"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        comment="UUID primary key",
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        comment="Tenant id (from the authenticated API key)",
    )
    installation_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        comment="Installation id (from the authenticated API key)",
    )
    month_jalali: Mapped[str] = mapped_column(
        String(7),
        nullable=False,
        comment="Jalali month key 'YYYY-MM' the run's lines belong to",
    )
    exact_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="Lines matched exactly this month",
    )
    probable_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="Lines matched probably this month",
    )
    ambiguous_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="Lines left ambiguous this month",
    )
    unmatched_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="Lines left unmatched this month",
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

    __table_args__ = (
        UniqueConstraint(
            "installation_id",
            "month_jalali",
            name="uq_cheque_match_history_installation_month",
        ),
        Index(
            "ix_cheque_match_history_installation_month",
            "installation_id",
            "month_jalali",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"ChequeMatchHistoryModel(installation_id={self.installation_id!r}, "
            f"month_jalali={self.month_jalali!r})"
        )


# ---------------------------------------------------------------------------
# Installments (US-540, Phase 5-5 ۵ج)
# ---------------------------------------------------------------------------


class InstallmentPlanModel(Base):
    """One installment plan: exactly one sales invoice, one schedule.

    Maps to the 'installment_plans' table. The unique index on
    (installation_id, invoice_id) is the one-plan-per-invoice
    backstop. No status column exists by design — leg and plan
    status are computed from receipts + the wall-clock date
    (``hesabyar.domain.installments.state_machine``) or from the
    linked settlement tranches (BNPL plans,
    ``hesabyar.domain.installments.bnpl``), never stored. ``kind``
    (US-543) IS stored: it separates the internal and BNPL worlds
    and is the report filter (``GET /installments/plans?kind=``).
    """

    __tablename__ = "installment_plans"
    __table_args__ = (
        UniqueConstraint(
            "installation_id",
            "invoice_id",
            name="uq_installment_plans_installation_invoice",
        ),
        Index("ix_installment_plans_installation_created", "installation_id", "created_at"),
        Index("ix_installment_plans_installation_contract", "installation_id", "contract_id"),
        Index("ix_installment_plans_installation_kind", "installation_id", "kind"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        comment="Tenant id (from the authenticated API key)",
    )
    installation_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        comment="Installation id (from the authenticated API key)",
    )
    company_engine_id: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Engine company id holding this plan's books",
    )
    invoice_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="The ONE sales invoice this plan belongs to (HesaYar invoice id or engine id)",
    )
    contract_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("installment_sale_contracts.id"),
        nullable=True,
        comment="Sale contract governing late fees / early settlement (NULL = no contract)",
    )
    customer_ref: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="Site customer reference (email/phone/customer code)",
    )
    total_principal_irr: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="Total principal in Rial (sum of the legs; validated at creation)",
    )
    leg_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Number of legs in the schedule",
    )
    interval_days: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Days between legs (payment terms; NULL when legs were supplied explicitly)",
    )
    first_due_date: Mapped[datetime.date | None] = mapped_column(
        Date,
        nullable=True,
        comment="First leg due date, Gregorian (payment terms; NULL for explicit legs)",
    )
    auto_post: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="Posting preference for journals later stories create (late fee, finance income): "
        "draft by default (ADR 0003 decision 1)",
    )
    applied_advance_irr: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        comment="Advance already applied to the schedule by early settlement (US-541); "
        "the held advance is the receipts' advances minus this",
    )
    source: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="manual",
        comment="Plan source: 'manual' (REST) | 'derived' (from sales.order.completed, ADR 0005)",
    )
    source_event_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="§10.2 source_event_id of the order event the plan was derived from (NULL for manual plans)",
    )
    kind: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="internal",
        comment="Plan kind (US-543): 'internal' (customer pays the seller) | 'bnpl' (the gateway pays in tranches)",
    )
    gateway_channel_key: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Registry channel key of the BNPL gateway (US-543; required for BNPL plans, NULL for internal ones)",
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

    def __repr__(self) -> str:
        return f"InstallmentPlanModel(id={self.id!r}, invoice_id={self.invoice_id!r})"


class InstallmentLegModel(Base):
    """One leg of an installment plan: sequence + due date + principal.

    Maps to the 'installment_legs' table. No received amount and no
    status column — both are computed from the receipt allocations.
    The cheque module's ``installment_leg_id`` points at this table
    (the link target US-531 already carries; US-542 consumes it).
    """

    __tablename__ = "installment_legs"
    __table_args__ = (
        UniqueConstraint(
            "plan_id",
            "sequence",
            name="uq_installment_legs_plan_sequence",
        ),
        Index("ix_installment_legs_plan_due", "plan_id", "due_date"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    plan_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("installment_plans.id"),
        nullable=False,
        comment="Plan this leg belongs to",
    )
    sequence: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Leg sequence within the plan (1-based)",
    )
    due_date: Mapped[datetime.date] = mapped_column(
        Date,
        nullable=False,
        comment="Leg due date, Gregorian (payload is Jalali)",
    )
    principal_irr: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="Leg principal in Rial (payload Toman x10 at the boundary)",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Row creation timestamp (UTC)",
    )

    def __repr__(self) -> str:
        return f"InstallmentLegModel(id={self.id!r}, plan_id={self.plan_id!r}, sequence={self.sequence!r})"


class InstallmentReceiptModel(Base):
    """One multi-stage receipt against a plan (the linkage guard's record).

    Maps to the 'installment_receipts' table. ``invoice_id`` and
    ``journal_move_id`` are both NOT NULL — the database enforces
    what the guard enforces in the service: a receipt without both
    refs cannot exist. ``advance_irr`` is the overpayment remainder
    (held on the plan, never silently applied to a leg).
    ``cheque_id`` (US-542) names the cheque backing a cheque
    receipt (NULL otherwise); ``collected_at`` (US-542) is the
    date the money actually arrived — the collection hook's record
    for US-541's income recognition. The ``gateway_*`` columns
    (US-543) link a BNPL receipt to its gateway payment (the
    receivable FROM THE GATEWAY, in the settlement engine's
    ``gateway_payments`` table — a cross-module reference, no SQL
    FK, like the settlement tables' Tryton refs); they are NULL
    for non-BNPL receipts.
    """

    __tablename__ = "installment_receipts"
    __table_args__ = (
        Index("ix_installment_receipts_plan_received", "plan_id", "received_date"),
        Index("ix_installment_receipts_installation_invoice", "installation_id", "invoice_id"),
        Index("ix_installment_receipts_cheque", "cheque_id"),
        Index("ix_installment_receipts_gateway_payment", "gateway_payment_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    plan_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("installment_plans.id"),
        nullable=False,
        comment="Plan this receipt pays into",
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        comment="Tenant id (from the authenticated API key)",
    )
    installation_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        comment="Installation id (from the authenticated API key)",
    )
    invoice_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Original invoice ref (linkage guard: must equal the plan's invoice)",
    )
    journal_move_id: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Engine move id of the accounting document for this receipt (linkage guard)",
    )
    amount_irr: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="Receipt amount in Rial (payload Toman x10 at the boundary)",
    )
    received_date: Mapped[datetime.date] = mapped_column(
        Date,
        nullable=False,
        comment="Receipt date, Gregorian (payload is Jalali)",
    )
    payment_method: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="cash",
        comment="Payment method: 'cash' | 'bank_transfer' | 'other' (US-540); "
        "'cheque' (US-542); 'bnpl' (US-543, BNPL plans only)",
    )
    advance_irr: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        comment="Overpayment remainder in Rial (amount - allocated; held, never auto-applied)",
    )
    finance_income_irr: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        comment="Finance-income portion of a settlement receipt in Rial (US-541; 0 on an ordinary receipt)",
    )
    late_fee_irr: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        comment="Late-fee portion of a settlement receipt in Rial (US-541; 0 on an ordinary receipt)",
    )
    applied_advance_irr: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        comment="Held advance from earlier receipts applied by this receipt in Rial (US-541; "
        "a settlement receipt's allocations may exceed its amount by exactly this)",
    )
    is_early_settlement: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="True when this receipt is an early-settlement receipt (US-541)",
    )
    reference: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="Caller-supplied receipt reference (bank ref, receipt number)",
    )
    notes: Mapped[str | None] = mapped_column(
        Text(),
        nullable=True,
        comment="Free-text notes",
    )
    cheque_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        comment="Cheque backing this receipt (US-542; set only for payment_method='cheque'; one receipt per cheque)",
    )
    collected_at: Mapped[datetime.date | None] = mapped_column(
        Date,
        nullable=True,
        comment=(
            "Date the money actually arrived (US-542 collection hook): "
            "received_date for cash/bank/other receipts; the clear date "
            "for a cheque receipt; NULL while uncollected"
        ),
    )
    gateway_channel_key: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="BNPL gateway channel key (US-543; NULL for non-BNPL receipts)",
    )
    gateway_reference: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="Gateway transaction id (US-543; the settlement engine's gateway_transaction_id)",
    )
    gateway_payment_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        comment="gateway_payments row id (US-543; the receivable from the gateway; no SQL FK — cross-module ref)",
    )
    bnpl_status: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
        comment="BNPL gateway-side status (US-543): 'active' | 'rejected' | 'cancelled'; NULL for non-BNPL receipts",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Row creation timestamp (UTC)",
    )

    def __repr__(self) -> str:
        return f"InstallmentReceiptModel(id={self.id!r}, plan_id={self.plan_id!r}, amount_irr={self.amount_irr!r})"


class InstallmentReceiptAllocationModel(Base):
    """One slice of a receipt applied to one leg.

    Maps to the 'installment_receipt_allocations' table. The
    allocation rule caps every slice at the leg's balance, so the
    sum of a leg's slices never exceeds its principal.
    """

    __tablename__ = "installment_receipt_allocations"
    __table_args__ = (
        UniqueConstraint(
            "receipt_id",
            "leg_id",
            name="uq_installment_allocations_receipt_leg",
        ),
        Index("ix_installment_allocations_leg", "leg_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    receipt_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("installment_receipts.id"),
        nullable=False,
        comment="Receipt this allocation belongs to",
    )
    leg_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("installment_legs.id"),
        nullable=False,
        comment="Leg this slice pays",
    )
    amount_irr: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="Allocated amount in Rial (never more than the leg's balance)",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Row creation timestamp (UTC)",
    )

    def __repr__(self) -> str:
        return f"InstallmentReceiptAllocationModel(receipt_id={self.receipt_id!r}, leg_id={self.leg_id!r})"


class InstallmentSaleContractModel(Base):
    """An installment sale contract version (قرارداد فروش قسطی).

    Maps to the 'installment_sale_contracts' table. The late-fee
    rule, penalty account and early-settlement terms are all
    user-entered per contract — nothing money-shaped is hard-coded.
    ``late_fee_rate`` is stored as exact decimal text (a fraction
    for *_PERCENT bases, whole Rial per day for FIXED_PER_DAY).
    """

    __tablename__ = "installment_sale_contracts"
    __table_args__ = (
        UniqueConstraint(
            "installation_id",
            "code",
            "version",
            name="uq_installment_contracts_installation_code_version",
        ),
        Index("ix_installment_contracts_installation_code", "installation_id", "code"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        comment="Tenant id (from the authenticated API key)",
    )
    installation_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        comment="Installation id (from the authenticated API key)",
    )
    company_engine_id: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Engine company id holding this contract's books",
    )
    code: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Contract code (versions share a code)",
    )
    name: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
        comment="Contract display name",
    )
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        comment="Contract version (increments per code)",
    )
    effective_from: Mapped[datetime.date] = mapped_column(
        Date,
        nullable=False,
        comment="Date this version takes effect, Gregorian (payload is Jalali)",
    )
    late_fee_basis: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="none",
        comment="Late-fee basis: 'none' | 'daily_percent' | 'monthly_percent' | 'fixed_per_day'",
    )
    late_fee_rate: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="0",
        comment="Late-fee rate as exact decimal text (fraction for *_PERCENT, Rial/day for FIXED_PER_DAY)",
    )
    late_fee_grace_days: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="Days past the due date before the late fee starts accruing",
    )
    penalty_account_engine_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Engine account id of the penalty (finance income) account (NULL = not configured)",
    )
    penalty_account_code: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="Chart code of the penalty account, for display/audit",
    )
    finance_income_rate: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="0",
        comment="Finance-income rate as exact decimal text (US-541): the fraction of principal "
        "charged as finance income over the whole plan (0 = none)",
    )
    dunning_offsets: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
        default="",
        comment="Dunning schedule as comma-separated day offsets (US-541), e.g. '3,7,14,30'; "
        "empty means no dunning schedule",
    )
    receivable_account_engine_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Engine account id of the customer-receivable account (US-541): the debit side "
        "of the late-fee journal (NULL = not configured)",
    )
    receivable_account_code: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="Chart code of the receivable account, for display/audit",
    )
    early_settlement_fee_policy: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="waived",
        comment="Accrued late fees on early settlement: 'waived' | 'kept'",
    )
    early_settlement_income_policy: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="waived",
        comment="Unrealised finance income on early settlement: 'waived' | 'discounted' | 'full'",
    )
    early_settlement_discount_rate: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="0",
        comment="Discount fraction as exact decimal text (US-541), applied to the remaining "
        "unrealised income when the income policy is 'discounted'",
    )
    supersedes_contract_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("installment_sale_contracts.id"),
        nullable=True,
        comment="Previous version this version replaces (NULL for version 1)",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        comment="Whether this version is currently in force",
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

    def __repr__(self) -> str:
        return f"InstallmentSaleContractModel(id={self.id!r}, code={self.code!r}, version={self.version!r})"


class InstallmentLateFeeAssessmentModel(Base):
    """A late-fee assessment for one leg (US-541, Phase 5-5 ۵ج).

    Maps to the 'installment_late_fee_assessments' table. A
    cumulative snapshot: ``fee_irr`` is the TOTAL fee accrued on
    the leg through ``assessed_through``; the journal (when one was
    created) covers only the increment since the previous journaled
    assessment. One assessment per (leg, date) — the unique
    constraint is the dunning run's idempotency backstop.
    """

    __tablename__ = "installment_late_fee_assessments"
    __table_args__ = (
        UniqueConstraint(
            "leg_id",
            "assessed_through",
            name="uq_installment_assessments_leg_date",
        ),
        Index("ix_installment_assessments_leg", "leg_id", "assessed_through"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    leg_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("installment_legs.id"),
        nullable=False,
        comment="Leg this assessment covers",
    )
    plan_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("installment_plans.id"),
        nullable=False,
        comment="Plan the leg belongs to",
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        comment="Tenant id (from the authenticated API key)",
    )
    installation_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        comment="Installation id (from the authenticated API key)",
    )
    assessed_through: Mapped[datetime.date] = mapped_column(
        Date,
        nullable=False,
        comment="Date the fee was accrued through, Gregorian",
    )
    days_overdue: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Whole days past the leg's due date at assessment time",
    )
    fee_irr: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="Total late fee accrued on the leg through assessed_through, in Rial",
    )
    journal_move_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Engine move id of the draft/posted journal for this increment (NULL = not journaled)",
    )
    journal_state: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="none",
        comment="Journal state: 'none' | 'draft' | 'posted'",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Row creation timestamp (UTC)",
    )

    def __repr__(self) -> str:
        return (
            f"InstallmentLateFeeAssessmentModel(id={self.id!r}, leg_id={self.leg_id!r}, "
            f"fee_irr={self.fee_irr!r})"
        )


class InstallmentScheduleAmendmentModel(Base):
    """A recorded change to a plan's schedule (US-541, Phase 5-5 ۵ج).

    Maps to the 'installment_schedule_amendments' table.
    Re-amortisation rewrites leg principals; the amendment is the
    audit record — the before/after principals per leg (JSONB
    lists of ``{leg_id, sequence, principal_irr}``), how much
    advance was applied, and which receipt triggered it.
    """

    __tablename__ = "installment_schedule_amendments"
    __table_args__ = (
        Index("ix_installment_amendments_plan_created", "plan_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    plan_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("installment_plans.id"),
        nullable=False,
        comment="Plan whose schedule changed",
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        comment="Tenant id (from the authenticated API key)",
    )
    installation_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        comment="Installation id (from the authenticated API key)",
    )
    kind: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="Amendment kind: 're_amortisation' (later kinds extend this)",
    )
    applied_advance_irr: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="Advance applied to the schedule by this amendment, in Rial",
    )
    legs_before: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        comment="Leg principals before the change: [{leg_id, sequence, principal_irr}]",
    )
    legs_after: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        comment="Leg principals after the change: [{leg_id, sequence, principal_irr}]",
    )
    receipt_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("installment_receipts.id"),
        nullable=True,
        comment="Receipt that triggered the amendment (the partial settlement receipt)",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Row creation timestamp (UTC)",
    )

    def __repr__(self) -> str:
        return (
            f"InstallmentScheduleAmendmentModel(id={self.id!r}, plan_id={self.plan_id!r}, "
            f"kind={self.kind!r})"
        )


# ---------------------------------------------------------------------------
# SMS providers + reminders (US-544, Phase 5-5 ۵ج)
# ---------------------------------------------------------------------------


class SmsProviderModel(Base):
    """One SMS provider in an installation's registry.

    Maps to the 'sms_providers' table. Built-ins (Kavenegar, sms.ir,
    simulator) come from the seed; custom providers are user-added
    rows. Price / rate limit / line type / contract / eNamad are
    USER-ENTERED (ADR 0003 amendment) — never hard-coded. The
    credential payload is Fernet-encrypted
    (``credentials_ciphertext``); the read model exposes only
    ``has_credentials``. Exactly one default provider per
    installation (partial unique index on ``is_default``).
    """

    __tablename__ = "sms_providers"
    __table_args__ = (
        UniqueConstraint(
            "installation_id",
            "provider_key",
            name="uq_sms_providers_installation_key",
        ),
        Index(
            "uq_sms_providers_installation_default",
            "installation_id",
            unique=True,
            postgresql_where=text("is_default"),
        ),
        Index("ix_sms_providers_installation_key", "installation_id", "provider_key"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    installation_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        comment="Installation id (from the authenticated API key)",
    )
    provider_key: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Stable slug: 'kavenegar' | 'sms_ir' | 'simulator' | a custom key",
    )
    display_name_fa: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
        comment="Provider display name (Persian)",
    )
    provider_kind: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="Client kind: 'kavenegar' | 'sms_ir' | 'simulator' | 'custom'",
    )
    is_builtin: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="True for the rows the seed creates",
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="Whether reminders may send through this provider",
    )
    is_default: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="Whether this is the installation's default provider (one per installation)",
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="setup",
        comment="Operational status: 'setup' | 'active' | 'disabled' | 'error'",
    )
    line_number: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="Sender line number (user-entered; sms.ir requires an int64 line)",
    )
    price_per_sms_toman: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="Informational price per SMS in Toman, exact decimal text (user-entered; never x10-converted)",
    )
    rate_limit_per_minute: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="User-entered rate limit in messages per minute (NULL = unknown)",
    )
    line_type: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="Line kind, free string (conventional: 'service' | 'dedicated')",
    )
    contract_required: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="Whether the provider requires a contract with the operator",
    )
    enmad_required: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="Whether the provider requires an eNamad for the store",
    )
    notes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Free-text operator notes",
    )
    credentials_ciphertext: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Fernet-encrypted credential JSON (never logged, never returned)",
    )
    credentials_key_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Encryption key id (for future rotation)",
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

    def __repr__(self) -> str:
        return f"SmsProviderModel(id={self.id!r}, provider_key={self.provider_key!r})"


class SmsReminderSettingsModel(Base):
    """One installation's reminder policy (one row per installation).

    Maps to the 'sms_reminder_settings' table. ``enabled`` is the
    master opt-in for AUTOMATIC sends (the daily job sends nothing
    for an installation with no row, or a disabled row). The
    offsets are JSON arrays of day counts; the templates are
    user-editable Persian text with ``{placeholders}``.
    """

    __tablename__ = "sms_reminder_settings"
    __table_args__ = (
        Index("ix_sms_reminder_settings_enabled", "installation_id", "enabled"),
    )

    installation_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        comment="Installation id (one policy row per installation)",
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        comment="Tenant id (lets the daily job scan plans without a key)",
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="Master opt-in for automatic reminder sends (default: off)",
    )
    offsets_days_before: Mapped[list] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        comment="Days before the due date that fire (e.g. [3, 0])",
    )
    offsets_days_overdue: Mapped[list] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        comment="Days after the due date that fire (e.g. [1])",
    )
    template_before_due: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="User-editable Persian template for a not-yet-overdue leg",
    )
    template_overdue: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="User-editable Persian template for an overdue leg",
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

    def __repr__(self) -> str:
        return f"SmsReminderSettingsModel(installation_id={self.installation_id!r}, enabled={self.enabled!r})"


class SmsContractReminderSettingsModel(Base):
    """The per-contract opt-in for automatic reminders.

    Maps to the 'sms_contract_reminder_settings' table. Automatic
    reminders send only when the provider is enabled AND the plan's
    contract has a row here with ``reminders_enabled=True``. Born
    disabled: opt-in is explicit, never implied.
    """

    __tablename__ = "sms_contract_reminder_settings"
    __table_args__ = (
        UniqueConstraint(
            "installation_id",
            "contract_id",
            name="uq_sms_contract_settings_installation_contract",
        ),
        Index(
            "ix_sms_contract_settings_installation",
            "installation_id",
            "contract_id",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    installation_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        comment="Installation id (from the authenticated API key)",
    )
    contract_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("installment_sale_contracts.id"),
        nullable=False,
        comment="Sale contract this opt-in belongs to",
    )
    reminders_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="Whether automatic reminders are opted in for this contract",
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

    def __repr__(self) -> str:
        return (
            f"SmsContractReminderSettingsModel(contract_id={self.contract_id!r}, "
            f"reminders_enabled={self.reminders_enabled!r})"
        )


class SmsReminderLogModel(Base):
    """One reminder attempt in the log (the DLQ + the audit trail).

    Maps to the 'sms_reminder_log' table. Every attempt is logged —
    sent, failed, skipped, or dry-run — with its provider, cost,
    and delivery status. ``offset_days`` is negative before the due
    date (``-3`` = 3 days before), ``0`` on the day, positive when
    overdue. The partial unique index on (installation, leg,
    offset) over the sent statuses (``queued`` / ``sent`` /
    ``delivered``) is the dedup backstop: a leg + offset is never
    reminded twice. ``provider_key`` is denormalized (the provider
    row may later be deleted; the log keeps the key).
    """

    __tablename__ = "sms_reminder_log"
    __table_args__ = (
        Index(
            "uq_sms_reminder_log_installation_leg_offset_sent",
            "installation_id",
            "leg_id",
            "offset_days",
            unique=True,
            postgresql_where=text("status IN ('queued', 'sent', 'delivered')"),
        ),
        Index("ix_sms_reminder_log_installation_created", "installation_id", "created_at"),
        Index("ix_sms_reminder_log_plan_created", "plan_id", "created_at"),
        Index("ix_sms_reminder_log_installation_status", "installation_id", "status"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        comment="Tenant id (from the authenticated API key)",
    )
    installation_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        comment="Installation id (from the authenticated API key)",
    )
    plan_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("installment_plans.id"),
        nullable=False,
        comment="Plan this reminder belongs to",
    )
    leg_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("installment_legs.id"),
        nullable=False,
        comment="Leg this reminder is about",
    )
    provider_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("sms_providers.id", ondelete="SET NULL"),
        nullable=True,
        comment="Provider row (NULL when the provider was deleted; provider_key survives)",
    )
    provider_key: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Denormalized provider key (survives a provider delete)",
    )
    recipient_phone: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="",
        comment="Recipient mobile in canonical 09... form (empty when unknown, e.g. a skipped row)",
    )
    message_text: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Rendered message text (Persian, Jalali due date, Persian digits)",
    )
    offset_days: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Days from the due date: negative = before, 0 = on the day, positive = overdue",
    )
    trigger: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="What caused the send: 'scheduled' (daily job) | 'manual' (operator trigger)",
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="Outcome: 'queued' | 'sent' | 'delivered' | 'failed' | 'skipped' | 'dry_run'",
    )
    provider_message_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="Provider-side message id (fake 'sim-...' id for the simulator)",
    )
    provider_status_raw: Mapped[str | None] = mapped_column(
        String(256),
        nullable=True,
        comment="Raw provider status (code + text, or pack id for sms.ir)",
    )
    cost_toman: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="Informational cost in Toman, exact decimal text (only when the provider's unit is known)",
    )
    provider_cost_raw: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Provider-reported cost, verbatim",
    )
    provider_cost_unit: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
        comment="Unit of provider_cost_raw: 'IRR' (Kavenegar) | 'unverified' (sms.ir) | NULL",
    )
    error_code: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Stable error code for failed/skipped rows (NULL otherwise)",
    )
    error_detail: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Human-readable failure detail (never credential material)",
    )
    sent_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the provider accepted the send (NULL for failed/skipped/dry-run rows)",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Row creation timestamp (UTC)",
    )

    def __repr__(self) -> str:
        return (
            f"SmsReminderLogModel(id={self.id!r}, leg_id={self.leg_id!r}, "
            f"offset_days={self.offset_days!r}, status={self.status!r})"
        )



# ---------------------------------------------------------------------------
# Manual-import batches (US-554, Phase 5-6 ۵ح — migration 0034)
# ---------------------------------------------------------------------------


class ManualImportBatchModel(Base):
    """One manual-import batch: the cycle wrapper around an import.

    Maps to the 'manual_import_batches' table. One row per manual
    import, both kinds (``kind`` discriminator: statement /
    settlement). The batch references the underlying import
    (``source_import_id``) and adds the staged state machine, the
    stored accounting-effect preview (``preview_json``), the stage
    audit trail (``stages_json``), rejection-with-reason + archiving,
    and re-runs. ``column_mapping_id`` has NO foreign key: it
    references ``bank_column_mappings`` for statement batches and
    ``settlement_column_mappings`` for settlement batches.
    """

    __tablename__ = "manual_import_batches"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('statement', 'settlement')",
            name="ck_manual_import_batches_kind",
        ),
        CheckConstraint(
            "file_format IN ('xlsx', 'csv', 'pdf')",
            name="ck_manual_import_batches_file_format",
        ),
        CheckConstraint(
            "currency_unit IN ('rial', 'toman')",
            name="ck_manual_import_batches_currency_unit",
        ),
        CheckConstraint(
            "date_kind IN ('jalali', 'gregorian')",
            name="ck_manual_import_batches_date_kind",
        ),
        CheckConstraint("header_row >= 0", name="ck_manual_import_batches_header_row"),
        CheckConstraint(
            "status IN ('uploaded', 'mapped', 'validated', 'previewed', "
            "'awaiting_review', 'approved', 'posted', 'rejected', 'archived')",
            name="ck_manual_import_batches_status",
        ),
        CheckConstraint("attempt >= 1", name="ck_manual_import_batches_attempt"),
        CheckConstraint(
            "file_type IS NULL OR file_type IN ('detail', 'payouts', 'statement')",
            name="ck_manual_import_batches_file_type",
        ),
        CheckConstraint(
            "posting_status IS NULL OR posting_status IN "
            "('draft_hold', 'held_gate', 'tieout_failed', 'posting_failed', 'posted')",
            name="ck_manual_import_batches_posting_status",
        ),
        UniqueConstraint(
            "tenant_id",
            "kind",
            "source_key",
            "file_hash_sha256",
            name="uq_manual_import_batches_tenant_kind_source_hash",
        ),
        Index(
            "ix_manual_import_batches_tenant_company",
            "tenant_id",
            "company_id",
            "created_at",
        ),
        Index(
            "ix_manual_import_batches_tenant_kind_status",
            "tenant_id",
            "kind",
            "status",
        ),
        Index("ix_manual_import_batches_source_import", "source_import_id"),
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
        comment="Owning tenant FK (CASCADE delete)",
    )
    installation_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        comment="Installation id (plain external reference, no FK)",
    )
    company_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="HesabYar company row id (plain external reference, no FK)",
    )
    kind: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="statement | settlement",
    )
    source_key: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Bank key (statements) or channel key (settlements)",
    )
    filename: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        comment="Uploaded filename (provenance only — file bytes are never stored)",
    )
    file_format: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="xlsx | csv | pdf",
    )
    file_hash_sha256: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="SHA-256 hex of the uploaded bytes (file-hash dedupe)",
    )
    currency_unit: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="rial | toman — the file's amount unit, as declared on import",
    )
    date_kind: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="jalali",
        comment="jalali | gregorian — how the file's dates are written",
    )
    header_row: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="0-based index of the header row in the file grid",
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="awaiting_review",
        comment="uploaded | mapped | validated | previewed | awaiting_review | approved | posted | rejected | archived",
    )
    attempt: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        comment="Upload attempt number on this batch (1 = original; a re-run increments it)",
    )
    source_import_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        comment="Underlying import id: bank_import_batches.id or settlement_file_imports.id",
    )
    source_import_batch_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        comment="Settlement import_batch_id (the tie-out key); the batch id for statements",
    )
    column_mapping_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        comment="Saved mapping used (bank/settlement column mappings by kind; NULL = inline)",
    )
    mapping_columns: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        comment="Applied mapping: canonical field -> source column",
    )
    file_type: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
        comment="Settlement file kind: detail | payouts | statement (NULL for statements)",
    )
    period_jalali: Mapped[str | None] = mapped_column(
        String(7),
        nullable=True,
        comment="Jalali period the settlement file covers (YYYY/MM; NULL for statements)",
    )
    statement_journal_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Engine statement journal id (statements only; kept for re-run provenance)",
    )
    default_account_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Engine account id for uncoded rows (statements only)",
    )
    preview_json: Mapped[dict | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Accounting-effect preview (gross/fee/tax/refunds/net/receivable/deposit/row counts)",
    )
    review_item_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        comment="manual_import_review queue item id for this batch",
    )
    rejection_reason: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Why the batch was rejected (kept on the archived batch)",
    )
    auto_post_opt_in: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="True when the operator explicitly opted this batch into auto-post (ADR 0003 decision 1)",
    )
    posting_status: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
        comment="draft_hold | held_gate | tieout_failed | posting_failed | posted (NULL before approval)",
    )
    posting_detail: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Why the posting step held/failed, when it did",
    )
    engine_ref_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Engine id written on posting (statement id / journal id)",
    )
    stages_json: Mapped[list] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        comment="Stage audit trail: [{stage, at, note}]",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Creation timestamp (UTC)",
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Last update timestamp (UTC)",
    )
    approved_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Approval timestamp (UTC), when approved",
    )
    posted_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Posting timestamp (UTC), when posted",
    )
    rejected_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Rejection timestamp (UTC), when rejected",
    )
    archived_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Archival timestamp (UTC), when archived",
    )

    def __repr__(self) -> str:
        return (
            f"ManualImportBatchModel(id={self.id!r}, kind={self.kind!r}, "
            f"source_key={self.source_key!r}, status={self.status!r})"
        )


class SettlementColumnMappingModel(Base):
    """A saved per-channel settlement column mapping (US-554).

    Maps to the 'settlement_column_mappings' table. The settlement
    twin of 'bank_column_mappings': ``columns`` is a JSONB object
    (canonical settlement field -> source column). ``company_id``
    is NULL when the mapping is shared across the tenant's
    companies (a gateway's layout does not vary by company).
    """

    __tablename__ = "settlement_column_mappings"
    __table_args__ = (
        CheckConstraint(
            "file_format IN ('xlsx', 'csv', 'pdf')",
            name="ck_settlement_column_mappings_file_format",
        ),
        CheckConstraint(
            "file_type IN ('detail', 'payouts', 'statement')",
            name="ck_settlement_column_mappings_file_type",
        ),
        CheckConstraint(
            "date_kind IN ('jalali', 'gregorian')",
            name="ck_settlement_column_mappings_date_kind",
        ),
        CheckConstraint(
            "currency_unit IN ('rial', 'toman')",
            name="ck_settlement_column_mappings_currency_unit",
        ),
        CheckConstraint(
            "status IN ('active', 'superseded')",
            name="ck_settlement_column_mappings_status",
        ),
        CheckConstraint(
            "header_row >= 0", name="ck_settlement_column_mappings_header_row"
        ),
        Index(
            "ix_settlement_column_mappings_tenant_channel",
            "tenant_id",
            "channel_key",
            "status",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key (column mapping id)",
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Owning tenant FK (CASCADE delete)",
    )
    company_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="HesabYar company row id when company-scoped; NULL = shared across companies",
    )
    channel_key: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Payment channel key (e.g. 'snapppay') — a registry key, not a hard-coded list",
    )
    name: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Operator-visible mapping name",
    )
    file_format: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="xlsx | csv | pdf",
    )
    file_type: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="detail | payouts | statement — the settlement file kind this mapping fits",
    )
    columns: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        comment="Canonical settlement field -> source column (header name or '#<index>')",
    )
    date_kind: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="jalali",
        comment="jalali | gregorian — how the file's dates are written",
    )
    currency_unit: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="rial",
        comment="rial | toman — the file's amount unit (last used; the import request may override)",
    )
    header_row: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="0-based index of the header row in the file grid",
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="active",
        comment="active | superseded",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Creation timestamp (UTC)",
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Last update timestamp (UTC)",
    )

    def __repr__(self) -> str:
        return (
            f"SettlementColumnMappingModel(id={self.id!r}, channel_key={self.channel_key!r}, "
            f"name={self.name!r}, status={self.status!r})"
        )


class ScheduledJobModel(Base):
    """One registered periodic job (US-560, Phase 5-7 ۵خ W1).

    Maps to the 'scheduled_jobs' table. The id IS the Procrastinate
    task name, so the registry can never name a job that does not
    exist. ``schedule`` is the JSONB schedule spec (see
    ``hesabyar.domain.scheduler.schedules.JobSchedule``); ``next_run``
    is computed in-app from the schedule + last run, never stored.
    The table is tenantless by design: jobs run across all
    installations (the registry is an operator surface, guarded by
    the ``jobs:manage`` scope, not a tenant surface).
    """

    __tablename__ = "scheduled_jobs"

    id: Mapped[str] = mapped_column(
        String(128),
        primary_key=True,
        comment="Job id == Procrastinate task name (e.g. 'settlement.tick')",
    )
    queue: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Procrastinate queue the job runs on",
    )
    schedule: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        comment="Schedule spec (kind + interval/time/Jalali day + skip_friday)",
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        comment="False stops the schedule (manual triggers still work)",
    )
    description: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="",
        comment="Operator-facing description of what the job does",
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

    def __repr__(self) -> str:
        return f"ScheduledJobModel(id={self.id!r}, enabled={self.enabled!r})"


class ScheduledJobRunModel(Base):
    """One recorded run of a scheduled job (US-560, Phase 5-7 ۵خ W1).

    Maps to the 'scheduled_job_runs' table. A failed run is the job's
    dead-letter record: the DLQ view for scheduler failures, next to
    the review-queue item. ``summary`` is a small JSON object (counts
    + ids, never secrets); ``error`` carries the failure text when
    the outcome is 'failed'. Runs are append-only; retention purging
    is a later maintenance job (see docs/scheduler.md).
    """

    __tablename__ = "scheduled_job_runs"
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('success', 'failed', 'skipped')",
            name="ck_scheduled_job_runs_outcome",
        ),
        CheckConstraint(
            "triggered_by IN ('scheduled', 'manual')",
            name="ck_scheduled_job_runs_triggered_by",
        ),
        Index("ix_scheduled_job_runs_job_started", "job_id", "started_at"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    job_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("scheduled_jobs.id", ondelete="CASCADE"),
        nullable=False,
        comment="Owning job FK (CASCADE delete)",
    )
    started_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="Run start timestamp (UTC)",
    )
    finished_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Run finish timestamp (UTC); NULL while running",
    )
    outcome: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="success | failed | skipped",
    )
    summary: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        comment="Run outcome summary (counts + ids, never secrets)",
    )
    error: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Failure text when outcome is 'failed'",
    )
    triggered_by: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="scheduled | manual",
    )

    def __repr__(self) -> str:
        return (
            f"ScheduledJobRunModel(id={self.id!r}, job_id={self.job_id!r}, "
            f"outcome={self.outcome!r})"
        )


class RuleModel(Base):
    """One user rule: ``when <event> + <conditions> → do <action>`` (US-561, Phase 5-7 ۵خ W2).

    Maps to the 'rules' table. ``event_type`` is one of the twelve
    canonical event types; ``conditions`` is the validated DSL tree
    (JSONLogic-style, no user code); ``action`` is ``{"kind",
    "params"}`` from the closed safe set. ``priority`` orders rules
    inside a rule set (lower runs first). A NULL ``installation_id``
    means the rule is tenant-wide (it fires for every installation
    of the tenant); otherwise the rule belongs to one installation.
    """

    __tablename__ = "rules"
    __table_args__ = (
        CheckConstraint("priority >= 0", name="ck_rules_priority_non_negative"),
        Index("ix_rules_tenant_installation_event", "tenant_id", "installation_id", "event_type"),
        Index("ix_rules_tenant_event_priority", "tenant_id", "event_type", "priority"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key (rule id)",
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Owning tenant FK (CASCADE delete)",
    )
    installation_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        comment="Owning installation (NULL = tenant-wide rule, fires for every installation)",
    )
    name: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        comment="Operator-facing rule name",
    )
    description: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="",
        comment="Operator-facing description of what the rule does",
    )
    event_type: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Canonical event type that triggers the rule",
    )
    conditions: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        comment="Validated condition DSL tree ({} = no conditions)",
    )
    action: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        comment='Action from the closed set: {"kind": ..., "params": {...}}',
    )
    priority: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=100,
        comment="Order inside a rule set (lower runs first)",
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        comment="False stops the rule from firing live (dry-run still works)",
    )
    created_by: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="Actor (API client id) that created the rule",
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

    def __repr__(self) -> str:
        return (
            f"RuleModel(id={self.id!r}, event_type={self.event_type!r}, "
            f"enabled={self.enabled!r})"
        )


class RuleExecutionModel(Base):
    """One audit-logged rule evaluation (US-561, Phase 5-7 ۵خ W2; §6.7).

    Maps to the 'rule_executions' table — the automation decision
    audit log for rules. Every dry-run and every live execution of
    a matched rule appends a row: rule id, event id, decision,
    outcome, plus the evidence (gate verdicts, created draft ids,
    posted refs, notification channel results) in ``details``.
    Append-only: rows are never updated or deleted. ``rule_id`` is
    NULL for dry-runs of unsaved definitions and for executions
    whose rule was deleted (the audit row survives the rule).
    """

    __tablename__ = "rule_executions"
    __table_args__ = (
        CheckConstraint(
            "mode IN ('live', 'dry_run')",
            name="ck_rule_executions_mode",
        ),
        CheckConstraint(
            "decision IN ('not_matched', 'would_execute', 'executed', "
            "'blocked_by_policy_gate', 'skipped', 'failed')",
            name="ck_rule_executions_decision",
        ),
        Index("ix_rule_executions_tenant_rule_executed", "tenant_id", "rule_id", "executed_at"),
        Index("ix_rule_executions_tenant_event", "tenant_id", "event_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key (execution id)",
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Owning tenant FK (CASCADE delete)",
    )
    rule_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("rules.id", ondelete="SET NULL"),
        nullable=True,
        comment="Rule FK (SET NULL on delete — the audit row survives the rule)",
    )
    event_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        comment="Inbox event id (NULL for sample-payload dry-runs)",
    )
    event_type: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Canonical event type that was evaluated",
    )
    mode: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="live | dry_run",
    )
    decision: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="not_matched | would_execute | executed | blocked_by_policy_gate | skipped | failed",
    )
    outcome: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Machine-readable outcome code (draft_created, posted, draft_kept, ...)",
    )
    action_kind: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="Action kind that ran (NULL when the rule did not match)",
    )
    details: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        comment="Evidence: gate verdicts, draft/posted refs, channel results",
    )
    error: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Failure text when the decision is 'failed'",
    )
    executed_by: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        default="system",
        comment="Actor (API client id) that triggered the evaluation",
    )
    executed_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Execution timestamp (UTC)",
    )

    def __repr__(self) -> str:
        return (
            f"RuleExecutionModel(id={self.id!r}, rule_id={self.rule_id!r}, "
            f"decision={self.decision!r}, outcome={self.outcome!r})"
        )


class RuleSetConfigModel(Base):
    """One rule set's match mode (US-561, Phase 5-7 ۵خ W2; §6.4).

    Maps to the 'rule_set_configs' table. A rule set is one tenant's
    rules for one event type; the mode decides whether evaluation
    stops at the first match (the Xero pattern) or runs every
    matching rule. No row means the default (``first_match``).
    """

    __tablename__ = "rule_set_configs"
    __table_args__ = (
        CheckConstraint(
            "match_mode IN ('first_match', 'all_matches')",
            name="ck_rule_set_configs_match_mode",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        primary_key=True,
        comment="Owning tenant FK (CASCADE delete)",
    )
    event_type: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        comment="Canonical event type the set covers",
    )
    match_mode: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="first_match | all_matches",
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        onupdate=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Last-modified timestamp (UTC)",
    )

    def __repr__(self) -> str:
        return (
            f"RuleSetConfigModel(tenant_id={self.tenant_id!r}, "
            f"event_type={self.event_type!r}, match_mode={self.match_mode!r})"
        )


class ReminderPreferencesModel(Base):
    """One installation's reminder policy (one row per installation).

    Maps to the 'reminder_preferences' table. ``enabled`` is the
    master opt-in for AUTOMATIC reminders (the daily job sends
    nothing for an installation with no row, or a disabled row).
    The per-type flags switch individual reminder types; the
    channel flags switch channels globally; the lead lists are
    JSON arrays of day counts; quiet hours are Tehran wall times.
    ``enmad_expiry_date`` is the store's eNamad expiry (NULL = no
    eNamad reminders) — it lives here until a store-profile model
    exists (phased design: settings → store profile).
    """

    __tablename__ = "reminder_preferences"
    __table_args__ = (
        Index("ix_reminder_preferences_enabled", "installation_id", "enabled"),
    )

    installation_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        comment="Installation id (one policy row per installation)",
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        comment="Tenant id (lets the daily job scan without a key)",
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="Master opt-in for automatic reminders (default: off)",
    )
    cheque_due_soon_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        comment="Cheque due-soon reminders on (master switch still applies)",
    )
    installment_overdue_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        comment="Overdue-installment reminders on (master switch still applies)",
    )
    cashbox_uncounted_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        comment="Uncounted-cashbox reminders on (master switch still applies)",
    )
    enmad_expiry_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        comment="eNamad-expiry reminders on (master switch still applies)",
    )
    channel_in_app: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        comment="In-app channel (review-queue item) on",
    )
    channel_email: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        comment="Email channel on (no email provider exists yet — attempts are logged as skipped)",
    )
    channel_sms: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        comment="SMS channel on (via the US-544 provider registry)",
    )
    cheque_lead_days: Mapped[list] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        comment="Cheque due-soon leads in days before the due date (e.g. [7, 3])",
    )
    installment_overdue_days: Mapped[list] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        comment="Installment-overdue offsets in days past the due date (e.g. [1, 7])",
    )
    cashbox_uncounted_days: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=7,
        comment="Uncounted-cashbox threshold: remind when a box went this many days without a count",
    )
    enmad_lead_days: Mapped[list] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        comment="eNamad-expiry leads in days before expiry (0 = the expiry day)",
    )
    quiet_hours_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        comment="Quiet hours on (no SMS or email inside the window; in-app may still record)",
    )
    quiet_hours_start: Mapped[datetime.time] = mapped_column(
        Time,
        nullable=False,
        comment="Quiet-hours start (Tehran wall time)",
    )
    quiet_hours_end: Mapped[datetime.time] = mapped_column(
        Time,
        nullable=False,
        comment="Quiet-hours end (Tehran wall time)",
    )
    enmad_expiry_date: Mapped[datetime.date | None] = mapped_column(
        Date,
        nullable=True,
        comment="The store's eNamad expiry date (NULL = no eNamad reminders)",
    )
    notification_email: Mapped[str | None] = mapped_column(
        String(254),
        nullable=True,
        comment="Operator address for email reminders (NULL = no email recipient configured)",
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

    def __repr__(self) -> str:
        return f"ReminderPreferencesModel(installation_id={self.installation_id!r}, enabled={self.enabled!r})"


class ReminderLogModel(Base):
    """One reminder attempt in the log (the DLQ + the audit trail).

    Maps to the 'reminder_log' table. Every attempt is logged —
    sent, failed, or skipped — with its channel, cost, and reason.
    The partial unique index on (installation, idempotency_key)
    over the sent statuses (``queued`` / ``sent`` / ``delivered``)
    is the dedup backstop: one reminder per event per lead-time
    per channel. ``provider_key`` is denormalized (the provider
    row may later be deleted; the log keeps the key).
    """

    __tablename__ = "reminder_log"
    __table_args__ = (
        CheckConstraint(
            "reminder_type IN ('cheque_due_soon', 'installment_overdue', "
            "'cashbox_uncounted', 'enmad_expiry')",
            name="ck_reminder_log_reminder_type",
        ),
        CheckConstraint(
            "channel IN ('in_app', 'email', 'sms')",
            name="ck_reminder_log_channel",
        ),
        CheckConstraint(
            "status IN ('queued', 'sent', 'delivered', 'failed', 'skipped')",
            name="ck_reminder_log_status",
        ),
        Index(
            "uq_reminder_log_installation_key_sent",
            "installation_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("status IN ('queued', 'sent', 'delivered')"),
        ),
        Index("ix_reminder_log_installation_created", "installation_id", "created_at"),
        Index(
            "ix_reminder_log_installation_type_status",
            "installation_id",
            "reminder_type",
            "status",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        comment="Tenant id (from the authenticated API key)",
    )
    installation_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        comment="Installation id (from the authenticated API key)",
    )
    reminder_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="cheque_due_soon | installment_overdue | cashbox_uncounted | enmad_expiry",
    )
    subject_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="What the reminder is about: cheque | installment_leg | cashbox | installation",
    )
    subject_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        comment="Id of the subject (cheque / leg / cash box / installation)",
    )
    lead_days: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Lead/offset that fired (days before due, days overdue, or the cash-box N threshold)",
    )
    channel: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="in_app | email | sms",
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="queued | sent | delivered | failed | skipped",
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Dedup key: {type}:{subject_id}:{event_ref}:{channel}",
    )
    message_text: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="The message as sent (Persian)",
    )
    recipient: Mapped[str | None] = mapped_column(
        String(254),
        nullable=True,
        comment="Where it went: mobile for SMS, email for email, NULL for in-app",
    )
    provider_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("sms_providers.id", ondelete="SET NULL"),
        nullable=True,
        comment="SMS provider row (NULL when the provider was deleted; provider_key survives)",
    )
    provider_key: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Denormalized SMS provider key (survives a provider delete)",
    )
    provider_message_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="Provider's id for the sent message",
    )
    provider_status_raw: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Provider's verbatim delivery status",
    )
    cost_toman: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="Informational SMS cost in Toman — only when the provider reports "
        "a cost in a KNOWN unit (the US-544 rule)",
    )
    provider_cost_raw: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Provider's verbatim cost figure",
    )
    provider_cost_unit: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="Unit of the verbatim cost ('IRR' for Kavenegar, 'unverified' for sms.ir)",
    )
    error_code: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Machine-readable reason for a failed/skipped attempt",
    )
    error_detail: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Human-readable detail for a failed/skipped attempt",
    )
    sent_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the reminder went out (NULL unless sent)",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Row creation timestamp (UTC)",
    )

    def __repr__(self) -> str:
        return (
            f"ReminderLogModel(id={self.id!r}, reminder_type={self.reminder_type!r}, "
            f"channel={self.channel!r}, status={self.status!r})"
        )


class RecurringTemplateModel(Base):
    """One recurring template (US-563, Phase 5-7 ۵خ W3).

    Maps to the 'recurring_templates' table. A template is a standing
    instruction: "for this company, on this Jalali schedule, create
    this invoice / journal". ``company_id`` is the HesabYar
    ``engine_companies`` row id (the API-facing id);
    ``company_engine_id`` is the Tryton company id the generator
    drafts into. ``schedule`` / ``payload`` are JSONB specs (see
    ``hesabyar.domain.recurring``); ``auto_post`` is the explicit
    opt-in (draft by default — ADR 0003 decision 1 — and the policy
    gate still decides).
    """

    __tablename__ = "recurring_templates"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('invoice', 'journal')",
            name="ck_recurring_templates_kind",
        ),
        CheckConstraint(
            "status IN ('active', 'paused', 'completed')",
            name="ck_recurring_templates_status",
        ),
        Index("ix_recurring_templates_tenant_company", "tenant_id", "company_id"),
        Index("ix_recurring_templates_status", "status"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        comment="Tenant id (from the authenticated API key)",
    )
    installation_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        comment=(
            "Installation id (from the authenticated API key; "
            "the policy gate evaluates auto-post against its registry policy)"
        ),
    )
    company_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        comment="HesabYar engine_companies row id (the API-facing company id)",
    )
    company_engine_id: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Engine company id the generator drafts into",
    )
    kind: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="invoice | journal",
    )
    name: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
        comment="Operator-facing template name",
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="active | paused | completed",
    )
    schedule: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        comment="Jalali schedule spec (kind + day/month/interval + start/end + max occurrences)",
    )
    payload: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        comment="The document the template generates (invoice or journal payload)",
    )
    auto_post: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="Explicit auto-post opt-in (draft by default; the policy gate still decides)",
    )
    total_amount_irr: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="Payload total in Rial (the preview amount)",
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

    def __repr__(self) -> str:
        return (
            f"RecurringTemplateModel(id={self.id!r}, kind={self.kind!r}, "
            f"status={self.status!r})"
        )


class RecurringOccurrenceModel(Base):
    """One generated (or failed) occurrence (US-563, Phase 5-7 ۵خ W3).

    Maps to the 'recurring_occurrences' table. The occurrence row
    links a generated engine document to its template + occurrence
    number. ``snapshot`` is the payload + schedule the occurrence
    was generated from — an edit never rewrites it. The (template,
    scheduled date) unique constraint is the generator's
    idempotency backstop: two runs racing on the same due date
    cannot create the same occurrence twice.
    """

    __tablename__ = "recurring_occurrences"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'draft', 'posted', 'failed')",
            name="ck_recurring_occurrences_status",
        ),
        UniqueConstraint(
            "template_id",
            "occurrence_number",
            name="uq_recurring_occurrences_template_number",
        ),
        UniqueConstraint(
            "template_id",
            "scheduled_date",
            name="uq_recurring_occurrences_template_date",
        ),
        Index(
            "ix_recurring_occurrences_template_number",
            "template_id",
            "occurrence_number",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    template_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("recurring_templates.id", ondelete="CASCADE"),
        nullable=False,
        comment="Owning template FK (CASCADE delete)",
    )
    occurrence_number: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Position in the schedule's sequence from the start date (1-based)",
    )
    scheduled_date: Mapped[datetime.date] = mapped_column(
        Date,
        nullable=False,
        comment="Scheduled (Gregorian) date of the occurrence",
    )
    jalali_date: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        comment="The same date as an ISO-like Jalali string (1405-08-15)",
    )
    clamped: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="True when the Jalali day was clamped to the month's length",
    )
    requested_jalali_day: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="The Jalali day the schedule asked for",
    )
    actual_jalali_day: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="The Jalali day the occurrence actually fires on",
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="pending | draft | posted | failed",
    )
    total_amount_irr: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="Occurrence total in Rial (from the snapshot)",
    )
    snapshot: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        comment="Payload + schedule the occurrence was generated from (an edit never rewrites it)",
    )
    engine_document_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Engine id of the generated invoice / journal (NULL until generated)",
    )
    gate_decision: Mapped[dict | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Policy-gate verdict when auto-post was attempted (NULL when never opted in)",
    )
    error: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Draft-creation failure (status failed) or post failure after the draft landed (status draft)",
    )
    generated_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the document was generated (UTC); NULL until generated",
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

    def __repr__(self) -> str:
        return (
            f"RecurringOccurrenceModel(id={self.id!r}, template_id={self.template_id!r}, "
            f"occurrence_number={self.occurrence_number!r}, status={self.status!r})"
        )


class AiAccessAuditModel(Base):
    """One audit row per MCP call attempt (US-601, spec §13.8).

    Maps to the ``ai_access_audit`` table (migration 0040). Every MCP
    call is recorded — allowed, denied and failed alike — with the
    client identity, tool name, company, scopes, outcome and timestamp.
    Never stores tool arguments (they may carry caller data), hidden
    chain-of-thought, or secrets: only the key's visible prefix, never
    key material.

    US-606 (migration 0041) adds ``kind='management'`` rows for MCP
    client lifecycle events (``mcp.client.create`` / ``rotate`` /
    ``revoke``), written by the REST management endpoints over
    ``transport='rest'``. A management row names the client acted upon
    (``client_id`` / ``key_id`` are the subject's, not the acting
    operator's); the operator is recorded in the REST audit trail
    (``audit_events``).
    """

    __tablename__ = "ai_access_audit"
    __table_args__ = (
        Index("ix_ai_access_audit_tenant_created", "tenant_id", "created_at"),
        Index("ix_ai_access_audit_client_created", "client_id", "created_at"),
        Index("ix_ai_access_audit_tool_created", "tool", "created_at"),
        CheckConstraint(
            "kind IN ('tool', 'resource', 'prompt', 'management')",
            name="ck_ai_access_audit_kind",
        ),
        CheckConstraint(
            "outcome IN ('allowed', 'denied', 'error')",
            name="ck_ai_access_audit_outcome",
        ),
        CheckConstraint(
            "transport IN ('stdio', 'streamable-http', 'memory', 'rest')",
            name="ck_ai_access_audit_transport",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key (audit row id)",
    )
    tenant_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
        comment="Owning tenant FK (CASCADE delete); NULL when no identity was established",
    )
    client_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("api_clients.id", ondelete="SET NULL"),
        nullable=True,
        comment="API client id (SET NULL on delete); NULL when unauthenticated",
    )
    key_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("api_keys.id", ondelete="SET NULL"),
        nullable=True,
        comment="API key id (SET NULL on delete); NULL when unauthenticated",
    )
    key_prefix: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
        comment="Visible API-key prefix (safe to log); NULL when unauthenticated",
    )
    tool: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Tool name, resource URI or prompt name that was called",
    )
    kind: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment=(
            "tool | resource | prompt | management — what kind of MCP call "
            "this was ('management' is a client lifecycle event, US-606)"
        ),
    )
    company_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="HesabYar company row id the call touched (NULL when no company was involved)",
    )
    scopes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Comma-separated scopes the key carried at call time (NULL when unauthenticated)",
    )
    outcome: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="allowed | denied | error — whether the call ran, was refused, or failed",
    )
    error_code: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Machine-readable failure reason for denied/error outcomes (NULL when allowed)",
    )
    request_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="MCP request id, when the transport supplied one",
    )
    transport: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment=(
            "stdio | streamable-http | memory | rest — which transport served "
            "the call ('rest' is a management event, US-606)"
        ),
    )
    client_info: Mapped[str | None] = mapped_column(
        String(256),
        nullable=True,
        comment="MCP client name/version from initialize, when known (e.g. 'claude/1.0')",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="Call timestamp (UTC)",
    )

    def __repr__(self) -> str:
        return (
            f"AiAccessAuditModel(id={self.id!r}, tool={self.tool!r}, "
            f"outcome={self.outcome!r}, client_id={self.client_id!r})"
        )


class McpAuthorizedClientModel(Base):
    """An authorized MCP client (US-606, spec §13.8 + §14.1).

    Maps to the ``mcp_authorized_clients`` table (migration 0041).
    This is the MANAGEMENT record for one AI client's MCP access —
    not a second credential system. The credential is an ordinary
    ``api_keys`` row (``hya_live_``, HMAC-SHA-256 + pepper, issued
    through :class:`ApiKeyService` with ``mcp:connect`` plus the
    tool scopes); the MCP server keeps authenticating that key and
    never consults this table. Registry membership gates nothing at
    call time — it records which clients the operator authorized,
    with what scopes and company grant, and when the key was rotated
    or revoked.

    One registry row owns one ``api_clients`` row (created with it,
    same name — the (tenant, name) uniqueness of ``api_clients``
    makes registry names unique per tenant) and points at the
    CURRENT key (``key_id`` moves on rotation; the old key is
    revoked, never deleted). ``scopes`` and ``engine_company_id``
    mirror the current key's grant at issuance/rotation — the
    service is the only writer, so the two cannot drift.

    A row is revoked when ``revoked_at`` is set (the key is revoked
    with the same reason at the same moment). Revoked rows are kept
    — the audit log (``ai_access_audit``, including the
    ``mcp.client.revoke`` management row) still names them.
    """

    __tablename__ = "mcp_authorized_clients"
    __table_args__ = (
        Index("ix_mcp_authorized_clients_tenant_created", "tenant_id", "created_at"),
        Index("ix_mcp_authorized_clients_key", "key_id"),
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
        comment="Owning tenant FK (CASCADE delete)",
    )
    client_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("api_clients.id", ondelete="CASCADE"),
        nullable=False,
        comment="Owning API client FK (CASCADE delete; one api_clients row per MCP client)",
    )
    key_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("api_keys.id"),
        nullable=False,
        comment="Current API key FK (moves on rotation; keys are revoked, never deleted)",
    )
    name: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Human-readable client name (unique per tenant via api_clients)",
    )
    description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="What this client is for (which AI host, which team)",
    )
    scopes: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Comma-separated scopes, mirroring the current key (always includes mcp:connect)",
    )
    engine_company_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("engine_companies.id", ondelete="SET NULL"),
        nullable=True,
        comment="Company grant, mirroring the current key (NULL = every company of the tenant)",
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
    revoked_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Revocation timestamp (UTC); NULL means not revoked",
    )
    revoked_reason: Mapped[str | None] = mapped_column(
        String(256),
        nullable=True,
        comment="Why the client was revoked",
    )

    tenant: Mapped[TenantModel] = relationship("TenantModel")
    client: Mapped[ApiClientModel] = relationship("ApiClientModel")
    key: Mapped[ApiKeyModel] = relationship("ApiKeyModel")
    company: Mapped[EngineCompanyModel | None] = relationship("EngineCompanyModel")

    def __repr__(self) -> str:
        return (
            f"McpAuthorizedClientModel(id={self.id!r}, tenant_id={self.tenant_id!r}, "
            f"name={self.name!r}, revoked_at={self.revoked_at!r})"
        )


class MonitoringAlertModel(Base):
    """One fired monitoring alert (US-704, Phase 7).

    Maps to the 'monitoring_alerts' table. Tenantless by design: an
    alert is an operator-surface record (like ``scheduled_jobs``) —
    inbox staleness, worker lag and the 5xx rate are installation-
    wide, not tenant-scoped. ``dedup_key`` is stable per firing
    condition; the partial unique index on firing rows (open /
    acknowledged) is the race backstop for concurrent evaluations —
    the service checks first, the index refuses second.
    """

    __tablename__ = "monitoring_alerts"
    __table_args__ = (
        CheckConstraint(
            "rule IN ('inbox_stale', 'worker_lag', 'token_expiring', "
            "'settlement_drift', 'webhook_failed', 'backup_failed', 'error_rate_5xx')",
            name="ck_monitoring_alerts_rule",
        ),
        CheckConstraint(
            "severity IN ('info', 'warning', 'critical')",
            name="ck_monitoring_alerts_severity",
        ),
        CheckConstraint(
            "status IN ('open', 'acknowledged', 'resolved')",
            name="ck_monitoring_alerts_status",
        ),
        Index(
            "uq_monitoring_alerts_dedup_firing",
            "dedup_key",
            unique=True,
            postgresql_where=text("status IN ('open', 'acknowledged')"),
        ),
        Index("ix_monitoring_alerts_status_fired", "status", "fired_at"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    rule: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="Alert rule: 'inbox_stale', 'worker_lag', 'token_expiring', "
        "'settlement_drift', 'webhook_failed', 'backup_failed' or 'error_rate_5xx'",
    )
    severity: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="Alert severity: 'info', 'warning' or 'critical'",
    )
    status: Mapped[str] = mapped_column(
        String(16),
        default="open",
        nullable=False,
        comment="Alert lifecycle: 'open', 'acknowledged' or 'resolved'",
    )
    title_fa: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Persian alert title",
    )
    message_fa: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Persian alert message (counts in Persian digits, money in Rial)",
    )
    dedup_key: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
        comment="Stable dedup key per firing condition (per rule, or per rule + subject)",
    )
    runbook_ref: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
        comment="Documentation reference (docs/monitoring.md section), not a URL",
    )
    context: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        comment="Rule context (JSONB: counts, amounts in Rial, ids — never secrets)",
    )
    fired_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
        comment="When the alert fired (UTC)",
    )
    acknowledged_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Acknowledgement timestamp (UTC); NULL until acknowledged",
    )
    acknowledged_by: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="Operator (API client name) that acknowledged the alert",
    )
    resolved_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Resolution timestamp (UTC); NULL until resolved",
    )
    resolved_by: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="Operator (API client name) that resolved the alert",
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

    def __repr__(self) -> str:
        return (
            f"MonitoringAlertModel(id={self.id!r}, rule={self.rule!r}, "
            f"severity={self.severity!r}, status={self.status!r})"
        )


class MonitoringAlertChannelModel(Base):
    """One configured alert channel (US-704, Phase 7).

    Maps to the 'monitoring_alert_channels' table. Tenantless by
    design (operator surface, like ``scheduled_jobs``).
    ``installation_id`` is the installation whose SMS registry sends
    for SMS channels (the SMS registry is installation-scoped); it is
    a plain value, no FK until the installations table lands (same as
    ``inbox_events.installation_id``). ``target`` is a mobile number
    (SMS), an email address (email) or an https URL (webhook — the
    URL itself is the secret, like a Slack webhook URL).
    """

    __tablename__ = "monitoring_alert_channels"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('sms', 'email', 'webhook')",
            name="ck_monitoring_alert_channels_kind",
        ),
        CheckConstraint(
            "min_severity IN ('info', 'warning', 'critical')",
            name="ck_monitoring_alert_channels_min_severity",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    kind: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="Channel kind: 'sms', 'email' or 'webhook'",
    )
    name: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Operator-facing channel name",
    )
    target: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        comment="Destination: mobile number (SMS), email address (email), https URL (webhook)",
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        comment="False stops dispatch on this channel (the config stays)",
    )
    min_severity: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="warning",
        comment="Only alerts at or above this severity go out on this channel",
    )
    provider_key: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="SMS registry key (kavenegar / sms_ir / simulator) for SMS channels",
    )
    installation_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        comment="Installation whose SMS registry sends (SMS channels only; plain value, no FK)",
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

    def __repr__(self) -> str:
        return (
            f"MonitoringAlertChannelModel(id={self.id!r}, kind={self.kind!r}, "
            f"name={self.name!r}, enabled={self.enabled!r})"
        )


class MonitoringTokenRecordModel(Base):
    """One configured token record (US-704, Phase 7).

    Maps to the 'monitoring_token_records' table. Tenantless by
    design (operator surface). A token the operator tells HesabYar
    about — the Fintech OAuth token first (no live token exists yet,
    the ۵ت remainder is deferred): the expiry alert fires from
    ``expires_at``, and ``rotation_runbook_ref`` names the doc
    section that tells the operator how to rotate it. API keys are
    NOT recorded here: they are read live from ``api_keys``.
    """

    __tablename__ = "monitoring_token_records"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('fintech', 'other')",
            name="ck_monitoring_token_records_kind",
        ),
        Index("ix_monitoring_token_records_enabled_expires", "enabled", "expires_at"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key",
    )
    kind: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="Token kind: 'fintech' or 'other'",
    )
    label: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Operator-facing token label (rendered verbatim in alerts)",
    )
    expires_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="Token expiry (UTC); the alert fires from this + the day thresholds",
    )
    rotation_runbook_ref: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
        comment="Documentation reference for the rotation procedure",
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        comment="False stops expiry alerts for this token (the record stays)",
    )
    notes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Operator notes (never credential material)",
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

    def __repr__(self) -> str:
        return (
            f"MonitoringTokenRecordModel(id={self.id!r}, kind={self.kind!r}, "
            f"label={self.label!r}, expires_at={self.expires_at!r})"
        )
