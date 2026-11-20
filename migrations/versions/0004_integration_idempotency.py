"""Integration and idempotency: inbox/outbox, cursors, attempts, idempotency

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-16

Design-eng spec §7.4 (Integration + Idempotency groups), §10.2 (canonical
events), §10.3 (reliable delivery) and §10.4 (idempotency constraints).

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "inbox_events",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key"),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=True,
            comment="Owning tenant FK (CASCADE delete); NULL until tenant mapping resolves",
        ),
        sa.Column(
            "schema_version",
            sa.Integer,
            server_default=sa.text("1"),
            nullable=False,
            comment="Canonical event schema version",
        ),
        sa.Column(
            "source_system",
            sa.String(64),
            nullable=False,
            comment="Source system key, e.g. 'hayat'",
        ),
        sa.Column(
            "source_store_id",
            sa.String(128),
            nullable=False,
            comment="Source store id within the source system",
        ),
        sa.Column(
            "source_event_id",
            sa.String(128),
            nullable=False,
            comment="Source event id — external reference",
        ),
        sa.Column(
            "event_type",
            sa.String(128),
            nullable=False,
            comment="Canonical event type, e.g. 'sales.order.completed'",
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="When the event occurred in the source system (UTC)",
        ),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="When HesabYar received the event (UTC)",
        ),
        sa.Column(
            "company_mapping",
            postgresql.JSONB,
            nullable=True,
            comment="Resolved company mapping (JSONB)",
        ),
        sa.Column(
            "currency",
            sa.String(3),
            nullable=True,
            comment="Event currency code, e.g. 'IRR'",
        ),
        sa.Column(
            "external_entity_type",
            sa.String(128),
            nullable=True,
            comment="Source entity type, e.g. 'order'",
        ),
        sa.Column(
            "external_entity_id",
            sa.String(128),
            nullable=True,
            comment="Source entity id — external reference",
        ),
        sa.Column(
            "correlation_id",
            sa.String(64),
            nullable=True,
            comment="Correlation id across services/events",
        ),
        sa.Column(
            "idempotency_key",
            sa.String(128),
            nullable=True,
            comment="Business idempotency key supplied by the source",
        ),
        sa.Column(
            "payload",
            postgresql.JSONB,
            nullable=False,
            comment="Raw event envelope (JSONB)",
        ),
        sa.Column(
            "payload_hash",
            sa.String(128),
            nullable=False,
            comment="SHA-256 hex of the canonical raw payload",
        ),
        sa.Column(
            "status",
            sa.String(32),
            server_default=sa.text("'received'"),
            nullable=False,
            comment="Processing status: 'received', 'processing', 'processed', 'failed' or 'needs_review'",
        ),
        sa.Column(
            "attempts",
            sa.Integer,
            server_default=sa.text("0"),
            nullable=False,
            comment="Number of processing attempts so far",
        ),
        sa.Column(
            "last_attempt_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Last processing attempt timestamp (UTC)",
        ),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Next scheduled retry timestamp (UTC)",
        ),
        sa.Column(
            "last_error",
            sa.Text,
            nullable=True,
            comment="Last processing error message",
        ),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Successful processing timestamp (UTC)",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Row creation timestamp (UTC)",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Last-modified timestamp (UTC)",
        ),
        sa.UniqueConstraint(
            "source_system",
            "source_store_id",
            "source_event_id",
            name="uq_inbox_events_source_event",
        ),
    )
    op.create_index("ix_inbox_events_tenant_id", "inbox_events", ["tenant_id"])
    op.create_index("ix_inbox_events_correlation_id", "inbox_events", ["correlation_id"])
    op.create_index(
        "ix_inbox_events_status_next_attempt",
        "inbox_events",
        ["status", "next_attempt_at"],
    )

    op.create_table(
        "outbox_events",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key"),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            comment="Owning tenant FK (CASCADE delete)",
        ),
        sa.Column(
            "aggregate_type",
            sa.String(128),
            nullable=False,
            comment="Aggregate type the event belongs to, e.g. 'journal_draft'",
        ),
        sa.Column(
            "aggregate_id",
            sa.String(128),
            nullable=False,
            comment="Aggregate id the event belongs to",
        ),
        sa.Column(
            "event_type",
            sa.String(128),
            nullable=False,
            comment="Event type, e.g. 'ledger.journal.posted'",
        ),
        sa.Column(
            "payload",
            postgresql.JSONB,
            nullable=False,
            comment="Event payload (JSONB)",
        ),
        sa.Column(
            "payload_hash",
            sa.String(128),
            nullable=True,
            comment="SHA-256 hex of the canonical payload (tamper evidence)",
        ),
        sa.Column(
            "status",
            sa.String(32),
            server_default=sa.text("'pending'"),
            nullable=False,
            comment="Delivery status: 'pending', 'published' or 'failed'",
        ),
        sa.Column(
            "attempts",
            sa.Integer,
            server_default=sa.text("0"),
            nullable=False,
            comment="Number of delivery attempts so far",
        ),
        sa.Column(
            "last_attempt_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Last delivery attempt timestamp (UTC)",
        ),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Next scheduled retry timestamp (UTC)",
        ),
        sa.Column(
            "last_error",
            sa.Text,
            nullable=True,
            comment="Last delivery error message",
        ),
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Successful publication timestamp (UTC)",
        ),
        sa.Column(
            "correlation_id",
            sa.String(64),
            nullable=True,
            comment="Correlation id across services/events",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Row creation timestamp (UTC)",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Last-modified timestamp (UTC)",
        ),
    )
    op.create_index("ix_outbox_events_tenant_id", "outbox_events", ["tenant_id"])
    op.create_index(
        "ix_outbox_events_status_next_attempt",
        "outbox_events",
        ["status", "next_attempt_at"],
    )
    op.create_index(
        "ix_outbox_events_aggregate",
        "outbox_events",
        ["aggregate_type", "aggregate_id"],
    )

    op.create_table(
        "integration_cursors",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key"),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            comment="Owning tenant FK (CASCADE delete)",
        ),
        sa.Column(
            "source_system",
            sa.String(64),
            nullable=False,
            comment="Source system key, e.g. 'hayat'",
        ),
        sa.Column(
            "source_store_id",
            sa.String(128),
            nullable=False,
            comment="Source store id within the source system",
        ),
        sa.Column(
            "stream",
            sa.String(128),
            nullable=False,
            comment="Stream name, e.g. 'orders', 'payments'",
        ),
        sa.Column(
            "cursor_value",
            sa.String(512),
            nullable=True,
            comment="Opaque source-system cursor (offset/token/timestamp)",
        ),
        sa.Column(
            "last_synced_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Last successful sync timestamp (UTC)",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Row creation timestamp (UTC)",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Last-modified timestamp (UTC)",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "source_system",
            "source_store_id",
            "stream",
            name="uq_integration_cursors_tenant_stream",
        ),
    )
    op.create_index(
        "ix_integration_cursors_tenant_id", "integration_cursors", ["tenant_id"]
    )

    op.create_table(
        "integration_attempts",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key"),
        sa.Column(
            "inbox_event_id",
            sa.String(36),
            sa.ForeignKey("inbox_events.id", ondelete="CASCADE"),
            nullable=True,
            comment="Inbox event being processed FK (CASCADE delete); NULL for outbox attempts",
        ),
        sa.Column(
            "outbox_event_id",
            sa.String(36),
            sa.ForeignKey("outbox_events.id", ondelete="CASCADE"),
            nullable=True,
            comment="Outbox event being published FK (CASCADE delete); NULL for inbox attempts",
        ),
        sa.Column(
            "attempt_number",
            sa.Integer,
            nullable=False,
            comment="1-based attempt number for the event",
        ),
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            comment="Attempt status: 'started', 'succeeded' or 'failed'",
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Attempt start timestamp (UTC)",
        ),
        sa.Column(
            "finished_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Attempt finish timestamp (UTC)",
        ),
        sa.Column(
            "error_code",
            sa.String(64),
            nullable=True,
            comment="Stable error code when the attempt failed",
        ),
        sa.Column(
            "error_message",
            sa.Text,
            nullable=True,
            comment="Failure detail when the attempt failed",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Row creation timestamp (UTC)",
        ),
        sa.CheckConstraint(
            "(inbox_event_id IS NOT NULL) <> (outbox_event_id IS NOT NULL)",
            name="ck_integration_attempts_single_event",
        ),
    )
    op.create_index(
        "ix_integration_attempts_inbox_event_id",
        "integration_attempts",
        ["inbox_event_id"],
    )
    op.create_index(
        "ix_integration_attempts_outbox_event_id",
        "integration_attempts",
        ["outbox_event_id"],
    )

    op.create_table(
        "idempotency_records",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key"),
        sa.Column(
            "installation_id",
            sa.String(36),
            nullable=False,
            comment="Logical installation/workspace id; plain value, no FK until the installations table lands",
        ),
        sa.Column(
            "operation",
            sa.String(128),
            nullable=False,
            comment="Operation name, e.g. 'ledger.draft.create'",
        ),
        sa.Column(
            "idempotency_key",
            sa.String(128),
            nullable=False,
            comment="Client-supplied idempotency key",
        ),
        sa.Column(
            "request_hash",
            sa.String(128),
            nullable=False,
            comment="SHA-256 hex of the canonical request payload; reuse with a different hash is a conflict",
        ),
        sa.Column(
            "response_status",
            sa.Integer,
            nullable=True,
            comment="HTTP status of the stored response",
        ),
        sa.Column(
            "response_body",
            postgresql.JSONB,
            nullable=True,
            comment="Stored response payload for replay (JSONB)",
        ),
        sa.Column(
            "response_reference",
            sa.String(512),
            nullable=True,
            comment="Pointer to the created resource, e.g. 'companies/<id>'",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Row creation timestamp (UTC)",
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Record expiry timestamp (UTC); expired records may be purged",
        ),
        sa.UniqueConstraint(
            "installation_id",
            "operation",
            "idempotency_key",
            name="uq_idempotency_records_installation_operation_key",
        ),
    )
    op.create_index(
        "ix_idempotency_records_expires_at", "idempotency_records", ["expires_at"]
    )


def downgrade() -> None:
    op.drop_table("idempotency_records")
    op.drop_table("integration_attempts")
    op.drop_table("integration_cursors")
    op.drop_table("outbox_events")
    op.drop_table("inbox_events")
