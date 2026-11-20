"""Event platform: decision audit log + store settings + DLQ column (US-501).

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-17

Design-eng spec §10 (canonical events, reliable delivery,
idempotency) and the phased design §6.7 (automation-decision audit
log) / ADR 0003 decision 4 (per-store Toman setting):

- ``automation_decision_log`` — append-only record of every
  auto-post/draft/needs-review/dead-letter/replay decision, with
  its policy version, mapping version and per-gate verdicts.
- ``connector_store_settings`` — one row per
  (source_system, source_store_id) holding the "prices are in
  Toman" setting that drives the ×10 conversion at the connector
  boundary. (The store registry in F3 owns the full store record;
  this narrow table owns only the currency setting.)
- ``inbox_events.dead_lettered_at`` — when the event entered the
  dead-letter queue (NULL otherwise), plus a
  (status, received_at) index for the staleness query.

The inbox/outbox/idempotency tables and the three §10.4 unique
constraints already exist (migration 0004 / 0002); this migration
adds only what the event platform newly needs.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "automation_decision_log",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key"),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=True,
            comment="Owning tenant FK (CASCADE delete); NULL until tenant mapping resolves",
        ),
        sa.Column(
            "inbox_event_id",
            sa.String(36),
            sa.ForeignKey("inbox_events.id", ondelete="CASCADE"),
            nullable=False,
            comment="Inbox event the decision was made for FK (CASCADE delete)",
        ),
        sa.Column(
            "event_type",
            sa.String(128),
            nullable=False,
            comment="Canonical event type, e.g. 'sales.order.completed'",
        ),
        sa.Column(
            "decision",
            sa.String(32),
            nullable=False,
            comment="Decision: 'draft', 'auto_post', 'needs_review', 'dead_letter' or 'replayed'",
        ),
        sa.Column(
            "policy_version",
            sa.String(64),
            nullable=False,
            comment="Policy version the decision used ('n/a' when no gate ran)",
        ),
        sa.Column(
            "mapping_version",
            sa.String(64),
            nullable=True,
            comment="Mapping version the decision used (NULL when no mapping ran)",
        ),
        sa.Column(
            "gate_results",
            postgresql.JSONB,
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
            comment="Per-gate verdicts: [{name, passed, reason}] (JSONB)",
        ),
        sa.Column(
            "decided_by",
            sa.String(128),
            nullable=False,
            comment="Decision actor: 'system' or the API client id of a human",
        ),
        sa.Column(
            "decided_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Decision timestamp (UTC)",
        ),
        sa.Column(
            "correlation_id",
            sa.String(64),
            nullable=True,
            comment="Correlation id across services/events",
        ),
        sa.Column(
            "details",
            postgresql.JSONB,
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
            comment="Decision details: reason, refs, error codes (JSONB)",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Row creation timestamp (UTC)",
        ),
    )
    op.create_index(
        "ix_automation_decision_log_tenant_id", "automation_decision_log", ["tenant_id"]
    )
    op.create_index(
        "ix_automation_decision_log_inbox_event_id",
        "automation_decision_log",
        ["inbox_event_id"],
    )
    op.create_index(
        "ix_automation_decision_log_decision", "automation_decision_log", ["decision"]
    )
    op.create_index(
        "ix_automation_decision_log_decided_at", "automation_decision_log", ["decided_at"]
    )

    op.create_table(
        "connector_store_settings",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key"),
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
            "prices_in_toman",
            sa.Boolean,
            server_default=sa.text("true"),
            nullable=False,
            comment="True when the store's prices are in Toman (convert ×10 at the boundary)",
        ),
        sa.Column(
            "currency",
            sa.String(3),
            server_default=sa.text("'IRR'"),
            nullable=False,
            comment="Store currency code, e.g. 'IRR'",
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
            name="uq_connector_store_settings_source_store",
        ),
    )

    op.add_column(
        "inbox_events",
        sa.Column(
            "dead_lettered_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Dead-letter timestamp (UTC); set when retries are exhausted, cleared on replay",
        ),
    )
    op.create_index(
        "ix_inbox_events_status_received_at",
        "inbox_events",
        ["status", "received_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_inbox_events_status_received_at", table_name="inbox_events")
    op.drop_column("inbox_events", "dead_lettered_at")
    op.drop_table("connector_store_settings")
    op.drop_table("automation_decision_log")
