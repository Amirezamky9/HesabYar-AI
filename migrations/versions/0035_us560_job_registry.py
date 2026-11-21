"""Job registry tables (US-560, Phase 5-7 ۵خ W1).

Revision ID: 0035
Revises: 0032
Create Date: 2026-11-21

The scheduler (Procrastinate, from F1/US-501) gains a registry: every
periodic job has one row with its id (= the Procrastinate task name),
queue, Jalali-aware schedule spec, and enabled flag; every run is
recorded with its outcome. ``next_run`` is computed in-app from the
schedule + last run, never stored.

- ``scheduled_jobs`` — one row per periodic job. The table is
  tenantless by design: jobs run across all installations (the
  registry is an operator surface, guarded by the ``jobs:manage``
  scope). Rows are seeded by the application on first use
  (``ensure_defaults`` inserts only missing rows), not by this
  migration — a data migration would overwrite an operator's
  enable/disable choice on re-run.
- ``scheduled_job_runs`` — one row per run (append-only). A failed
  run is the job's dead-letter record: the DLQ view for scheduler
  failures, next to the review-queue item. Retention purging is a
  later maintenance job (see docs/scheduler.md).

Chains after 0032 (US-552 mapping confirmations — the Alembic head
at branch time).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0035"
down_revision: str | None = "0032"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "scheduled_jobs",
        sa.Column(
            "id",
            sa.String(128),
            nullable=False,
            comment="Job id == Procrastinate task name (e.g. 'settlement.tick')",
        ),
        sa.Column(
            "queue",
            sa.String(64),
            nullable=False,
            comment="Procrastinate queue the job runs on",
        ),
        sa.Column(
            "schedule",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="Schedule spec (kind + interval/time/Jalali day + skip_friday)",
        ),
        sa.Column(
            "enabled",
            sa.Boolean,
            nullable=False,
            server_default=sa.true(),
            comment="False stops the schedule (manual triggers still work)",
        ),
        sa.Column(
            "description",
            sa.Text(),
            nullable=False,
            server_default="",
            comment="Operator-facing description of what the job does",
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
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "scheduled_job_runs",
        sa.Column("id", sa.String(36), nullable=False, comment="UUID primary key"),
        sa.Column(
            "job_id",
            sa.String(128),
            sa.ForeignKey("scheduled_jobs.id", ondelete="CASCADE"),
            nullable=False,
            comment="Owning job FK (CASCADE delete)",
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Run start timestamp (UTC)",
        ),
        sa.Column(
            "finished_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Run finish timestamp (UTC); NULL while running",
        ),
        sa.Column(
            "outcome",
            sa.String(16),
            nullable=False,
            comment="success | failed | skipped",
        ),
        sa.Column(
            "summary",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="Run outcome summary (counts + ids, never secrets)",
        ),
        sa.Column(
            "error",
            sa.Text(),
            nullable=True,
            comment="Failure text when outcome is 'failed'",
        ),
        sa.Column(
            "triggered_by",
            sa.String(16),
            nullable=False,
            comment="scheduled | manual",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "outcome IN ('success', 'failed', 'skipped')",
            name="ck_scheduled_job_runs_outcome",
        ),
        sa.CheckConstraint(
            "triggered_by IN ('scheduled', 'manual')",
            name="ck_scheduled_job_runs_triggered_by",
        ),
        sa.Index(
            "ix_scheduled_job_runs_job_started",
            "job_id",
            "started_at",
        ),
    )


def downgrade() -> None:
    op.drop_table("scheduled_job_runs")
    op.drop_table("scheduled_jobs")
