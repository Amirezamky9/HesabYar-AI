"""Monitoring + alerting tables (US-704, Phase 7).

Revision ID: 0042
Revises: 0041
Create Date: 2026-11-21

- ``monitoring_alerts`` — fired alerts (rule, severity, status
  lifecycle, Persian title/message, dedup key, runbook ref, JSONB
  context). The partial unique index on ``dedup_key`` over firing
  rows (open/acknowledged) is the race backstop for concurrent
  evaluations — the service checks first, the index refuses second.
- ``monitoring_alert_channels`` — configured alert channels (SMS /
  email / webhook): target, provider key + installation for SMS,
  minimum severity, enabled flag.
- ``monitoring_token_records`` — configured token records (the
  Fintech OAuth token first: no live token exists yet, the ۵ت
  remainder is deferred): kind, label, expiry, rotation runbook
  ref. API keys are NOT recorded here — they are read live from
  ``api_keys``.

All three tables are tenantless by design (operator surface, like
``scheduled_jobs``). No backup table is created here: US-703 owns
backups and the monitoring service reads backup status through the
``BackupStatusSource`` protocol (see docs/monitoring.md).

Chains after 0041 (US-606 MCP authorized clients — the Alembic head
at branch time).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0042"
down_revision: str | None = "0041"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "monitoring_alerts",
        sa.Column("id", sa.String(36), nullable=False, comment="UUID primary key"),
        sa.Column(
            "rule",
            sa.String(32),
            nullable=False,
            comment="Alert rule: 'inbox_stale', 'worker_lag', 'token_expiring', "
            "'settlement_drift', 'webhook_failed', 'backup_failed' or 'error_rate_5xx'",
        ),
        sa.Column(
            "severity",
            sa.String(16),
            nullable=False,
            comment="Alert severity: 'info', 'warning' or 'critical'",
        ),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="open",
            comment="Alert lifecycle: 'open', 'acknowledged' or 'resolved'",
        ),
        sa.Column("title_fa", sa.Text(), nullable=False, comment="Persian alert title"),
        sa.Column(
            "message_fa",
            sa.Text(),
            nullable=False,
            comment="Persian alert message (counts in Persian digits, money in Rial)",
        ),
        sa.Column(
            "dedup_key",
            sa.String(256),
            nullable=False,
            comment="Stable dedup key per firing condition (per rule, or per rule + subject)",
        ),
        sa.Column(
            "runbook_ref",
            sa.String(512),
            nullable=True,
            comment="Documentation reference (docs/monitoring.md section), not a URL",
        ),
        sa.Column(
            "context",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="Rule context (JSONB: counts, amounts in Rial, ids — never secrets)",
        ),
        sa.Column(
            "fired_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="When the alert fired (UTC)",
        ),
        sa.Column(
            "acknowledged_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Acknowledgement timestamp (UTC); NULL until acknowledged",
        ),
        sa.Column(
            "acknowledged_by",
            sa.String(128),
            nullable=True,
            comment="Operator (API client name) that acknowledged the alert",
        ),
        sa.Column(
            "resolved_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Resolution timestamp (UTC); NULL until resolved",
        ),
        sa.Column(
            "resolved_by",
            sa.String(128),
            nullable=True,
            comment="Operator (API client name) that resolved the alert",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="Row creation timestamp (UTC)",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="Last-modified timestamp (UTC)",
        ),
        sa.CheckConstraint(
            "rule IN ('inbox_stale', 'worker_lag', 'token_expiring', "
            "'settlement_drift', 'webhook_failed', 'backup_failed', 'error_rate_5xx')",
            name="ck_monitoring_alerts_rule",
        ),
        sa.CheckConstraint(
            "severity IN ('info', 'warning', 'critical')",
            name="ck_monitoring_alerts_severity",
        ),
        sa.CheckConstraint(
            "status IN ('open', 'acknowledged', 'resolved')",
            name="ck_monitoring_alerts_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_monitoring_alerts_dedup_firing",
        "monitoring_alerts",
        ["dedup_key"],
        unique=True,
        postgresql_where=sa.text("status IN ('open', 'acknowledged')"),
    )
    op.create_index(
        "ix_monitoring_alerts_status_fired",
        "monitoring_alerts",
        ["status", "fired_at"],
    )

    op.create_table(
        "monitoring_alert_channels",
        sa.Column("id", sa.String(36), nullable=False, comment="UUID primary key"),
        sa.Column(
            "kind",
            sa.String(16),
            nullable=False,
            comment="Channel kind: 'sms', 'email' or 'webhook'",
        ),
        sa.Column(
            "name",
            sa.String(128),
            nullable=False,
            comment="Operator-facing channel name",
        ),
        sa.Column(
            "target",
            sa.String(512),
            nullable=False,
            comment="Destination: mobile number (SMS), email address (email), https URL (webhook)",
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
            comment="False stops dispatch on this channel (the config stays)",
        ),
        sa.Column(
            "min_severity",
            sa.String(16),
            nullable=False,
            server_default="warning",
            comment="Only alerts at or above this severity go out on this channel",
        ),
        sa.Column(
            "provider_key",
            sa.String(64),
            nullable=True,
            comment="SMS registry key (kavenegar / sms_ir / simulator) for SMS channels",
        ),
        sa.Column(
            "installation_id",
            sa.String(36),
            nullable=True,
            comment="Installation whose SMS registry sends (SMS channels only; plain value, no FK)",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="Row creation timestamp (UTC)",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="Last-modified timestamp (UTC)",
        ),
        sa.CheckConstraint(
            "kind IN ('sms', 'email', 'webhook')",
            name="ck_monitoring_alert_channels_kind",
        ),
        sa.CheckConstraint(
            "min_severity IN ('info', 'warning', 'critical')",
            name="ck_monitoring_alert_channels_min_severity",
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "monitoring_token_records",
        sa.Column("id", sa.String(36), nullable=False, comment="UUID primary key"),
        sa.Column(
            "kind",
            sa.String(32),
            nullable=False,
            comment="Token kind: 'fintech' or 'other'",
        ),
        sa.Column(
            "label",
            sa.String(128),
            nullable=False,
            comment="Operator-facing token label (rendered verbatim in alerts)",
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Token expiry (UTC); the alert fires from this + the day thresholds",
        ),
        sa.Column(
            "rotation_runbook_ref",
            sa.String(512),
            nullable=True,
            comment="Documentation reference for the rotation procedure",
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
            comment="False stops expiry alerts for this token (the record stays)",
        ),
        sa.Column(
            "notes",
            sa.Text(),
            nullable=True,
            comment="Operator notes (never credential material)",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="Row creation timestamp (UTC)",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="Last-modified timestamp (UTC)",
        ),
        sa.CheckConstraint(
            "kind IN ('fintech', 'other')",
            name="ck_monitoring_token_records_kind",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_monitoring_token_records_enabled_expires",
        "monitoring_token_records",
        ["enabled", "expires_at"],
    )


def downgrade() -> None:
    op.drop_table("monitoring_token_records")
    op.drop_table("monitoring_alert_channels")
    op.drop_table("monitoring_alerts")
