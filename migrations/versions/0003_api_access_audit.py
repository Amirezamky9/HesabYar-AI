"""API access and audit: api clients, api keys, audit events

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-16

Design-eng spec §7.4 (API access + Audit groups), §7.5 (API-key model) and
§14.3 (audit events). API keys store only a visible prefix + secure hash —
never the raw secret. Audit events never store secrets.

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "api_clients",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key"),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            comment="Owning tenant FK (CASCADE delete)",
        ),
        sa.Column(
            "name",
            sa.String(128),
            nullable=False,
            comment="Human-readable client name",
        ),
        sa.Column(
            "description",
            sa.Text,
            nullable=True,
            comment="What this client integrates",
        ),
        sa.Column(
            "is_active",
            sa.Boolean,
            server_default=sa.text("true"),
            nullable=False,
            comment="Whether the client may authenticate",
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
        sa.UniqueConstraint("tenant_id", "name", name="uq_api_clients_tenant_name"),
    )
    op.create_index("ix_api_clients_tenant_id", "api_clients", ["tenant_id"])

    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key"),
        sa.Column(
            "client_id",
            sa.String(36),
            sa.ForeignKey("api_clients.id", ondelete="CASCADE"),
            nullable=False,
            comment="Owning API client FK (CASCADE delete)",
        ),
        sa.Column(
            "installation_id",
            sa.String(36),
            nullable=False,
            comment="Logical installation/workspace id (spec §7.5); plain value, no FK yet",
        ),
        sa.Column(
            "name",
            sa.String(128),
            nullable=False,
            comment="Human-readable key name",
        ),
        sa.Column(
            "key_prefix",
            sa.String(16),
            nullable=False,
            comment="Visible key prefix (e.g. first 8 chars); the raw secret is never stored",
        ),
        sa.Column(
            "key_hash",
            sa.String(128),
            nullable=False,
            comment="Keyed HMAC-SHA-256 hash of the secret with a server-side pepper (hex); never the raw secret",
        ),
        sa.Column(
            "scopes",
            sa.Text,
            server_default=sa.text("''"),
            nullable=False,
            comment="Comma-separated permission scopes (e.g. company:read,account:read)",
        ),
        sa.Column(
            "engine_company_id",
            sa.String(36),
            sa.ForeignKey("engine_companies.id", ondelete="SET NULL"),
            nullable=True,
            comment="Optional restriction to one engine company (HesabYar-owned row; SET NULL on delete)",
        ),
        sa.Column(
            "created_by_actor_id",
            sa.String(36),
            sa.ForeignKey("actors.id", ondelete="SET NULL"),
            nullable=True,
            comment="Creator actor FK (SET NULL on delete)",
        ),
        sa.Column(
            "allowed_ips",
            sa.Text,
            nullable=True,
            comment="Optional comma-separated CIDR/IP allowlist",
        ),
        sa.Column(
            "rate_limit_per_minute",
            sa.Integer,
            nullable=True,
            comment="Optional per-key rate limit (requests per minute)",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Row creation timestamp (UTC)",
        ),
        sa.Column(
            "last_used_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Last successful use timestamp (UTC)",
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Expiry timestamp (UTC); NULL means no expiry",
        ),
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Revocation timestamp (UTC); NULL means not revoked",
        ),
        sa.Column(
            "revoked_reason",
            sa.String(256),
            nullable=True,
            comment="Why the key was revoked",
        ),
        sa.UniqueConstraint("key_hash", name="uq_api_keys_key_hash"),
    )
    op.create_index("ix_api_keys_client_id", "api_keys", ["client_id"])
    op.create_index("ix_api_keys_key_prefix", "api_keys", ["key_prefix"])
    op.create_index("ix_api_keys_engine_company_id", "api_keys", ["engine_company_id"])

    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key"),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            comment="Owning tenant FK (CASCADE delete)",
        ),
        sa.Column(
            "installation_id",
            sa.String(36),
            nullable=False,
            comment="Logical installation/workspace id; plain value, no FK until the installations table lands",
        ),
        sa.Column(
            "actor_id",
            sa.String(36),
            sa.ForeignKey("actors.id", ondelete="SET NULL"),
            nullable=True,
            comment="Acting actor FK (SET NULL on delete); NULL for system events",
        ),
        sa.Column(
            "api_client_id",
            sa.String(36),
            sa.ForeignKey("api_clients.id", ondelete="SET NULL"),
            nullable=True,
            comment="Source API client FK (SET NULL on delete); NULL for non-API actions",
        ),
        sa.Column(
            "action",
            sa.String(128),
            nullable=False,
            comment="Action performed, e.g. 'ledger.draft.create'",
        ),
        sa.Column(
            "resource_type",
            sa.String(128),
            nullable=False,
            comment="Resource type acted on, e.g. 'journal_draft'",
        ),
        sa.Column(
            "resource_id",
            sa.String(128),
            nullable=True,
            comment="Resource id acted on",
        ),
        sa.Column(
            "company_external_id",
            sa.String(64),
            nullable=True,
            comment="Engine company id — external reference, not a cross-database FK",
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Event timestamp (UTC)",
        ),
        sa.Column(
            "request_id",
            sa.String(64),
            nullable=True,
            comment="Request id that caused the event",
        ),
        sa.Column(
            "correlation_id",
            sa.String(64),
            nullable=True,
            comment="Correlation id across services/events",
        ),
        sa.Column(
            "source_system",
            sa.String(64),
            nullable=True,
            comment="Source system key, e.g. 'hayat', 'mcp', 'dashboard'",
        ),
        sa.Column(
            "authorization_result",
            sa.String(32),
            server_default=sa.text("'allowed'"),
            nullable=False,
            comment="Authorization outcome: 'allowed' or 'denied'",
        ),
        sa.Column(
            "outcome",
            sa.String(32),
            nullable=False,
            comment="Event outcome: 'success', 'failure' or 'error'",
        ),
        sa.Column(
            "error_code",
            sa.String(64),
            nullable=True,
            comment="Stable error code when the outcome is not success",
        ),
        sa.Column(
            "tryton_reference",
            sa.String(128),
            nullable=True,
            comment="Engine reference for accounting operations, e.g. 'account.move:123' — external reference",
        ),
        sa.Column(
            "payload_hash",
            sa.String(128),
            nullable=True,
            comment="SHA-256 hex of the request payload (tamper evidence)",
        ),
        sa.Column(
            "evidence_pointer",
            sa.String(512),
            nullable=True,
            comment="Pointer to stored evidence (object storage key / document reference)",
        ),
        sa.Column(
            "metadata_before",
            postgresql.JSONB,
            nullable=True,
            comment="Before-change metadata (JSONB)",
        ),
        sa.Column(
            "metadata_after",
            postgresql.JSONB,
            nullable=True,
            comment="After-change metadata (JSONB)",
        ),
    )
    op.create_index("ix_audit_events_tenant_id", "audit_events", ["tenant_id"])
    op.create_index("ix_audit_events_actor_id", "audit_events", ["actor_id"])
    op.create_index("ix_audit_events_request_id", "audit_events", ["request_id"])
    op.create_index(
        "ix_audit_events_tenant_occurred", "audit_events", ["tenant_id", "occurred_at"]
    )
    op.create_index(
        "ix_audit_events_correlation_id", "audit_events", ["correlation_id"]
    )

    op.create_table(
        "audit_event_subjects",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key"),
        sa.Column(
            "audit_event_id",
            sa.String(36),
            sa.ForeignKey("audit_events.id", ondelete="CASCADE"),
            nullable=False,
            comment="Owning audit event FK (CASCADE delete)",
        ),
        sa.Column(
            "subject_type",
            sa.String(128),
            nullable=False,
            comment="Subject type, e.g. 'company', 'invoice'",
        ),
        sa.Column(
            "subject_id",
            sa.String(128),
            nullable=False,
            comment="Subject id — external reference where the subject lives in the engine",
        ),
        sa.Column(
            "role",
            sa.String(64),
            nullable=True,
            comment="Subject role in the event, e.g. 'target', 'affected'",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Row creation timestamp (UTC)",
        ),
        sa.UniqueConstraint(
            "audit_event_id",
            "subject_type",
            "subject_id",
            name="uq_audit_event_subjects_event_subject",
        ),
    )
    op.create_index(
        "ix_audit_event_subjects_audit_event_id",
        "audit_event_subjects",
        ["audit_event_id"],
    )


def downgrade() -> None:
    op.drop_table("audit_event_subjects")
    op.drop_table("audit_events")
    op.drop_table("api_keys")
    op.drop_table("api_clients")
