"""Report snapshots with provenance (US-403).

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-17

Design-eng spec §7.4 (reports group) and §12.1 (validation
provenance): one ``report_snapshots`` table holding the reproducible
record of a live trial balance — the trial-balance document itself
(payload), its SHA-256 (equal to the validation run's input_hash),
the source endpoint the figures came from, the ruleset version they
were validated against and the as-of date. Normalized, typed columns
with constraints + indexes; no EAV. The snapshot joins its validation
run (one snapshot per run); validation runs themselves reuse the
US-401 ``validation_runs`` / ``validation_results`` tables.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "report_snapshots",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key (snapshot id)"),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            comment="Owning tenant FK (CASCADE delete)",
        ),
        sa.Column(
            "company_id",
            sa.String(128),
            nullable=True,
            comment="API company id as supplied (row id or engine id); plain external reference, no FK",
        ),
        sa.Column(
            "validation_run_id",
            sa.String(36),
            sa.ForeignKey("validation_runs.id", ondelete="CASCADE"),
            nullable=True,
            comment="Validation run this snapshot was built for (CASCADE delete)",
        ),
        sa.Column(
            "period_start",
            sa.Date(),
            nullable=True,
            comment="Start of the trial-balance period",
        ),
        sa.Column(
            "period_end",
            sa.Date(),
            nullable=True,
            comment="End of the trial-balance period",
        ),
        sa.Column(
            "as_of_date",
            sa.Date(),
            nullable=False,
            comment="As-of date of the snapshot (the trial-balance period end)",
        ),
        sa.Column(
            "source_endpoint",
            sa.String(128),
            nullable=False,
            comment="Engine read the figures came from, e.g. 'account.move.line (Tryton REST)'",
        ),
        sa.Column(
            "ruleset_version",
            sa.String(64),
            nullable=True,
            comment="Ruleset version the snapshot was validated against",
        ),
        sa.Column(
            "payload_hash",
            sa.String(64),
            nullable=False,
            comment="SHA-256 hex of the canonical trial-balance document (equals the run's input_hash)",
        ),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="Trial-balance document: per-account debit/credit/balance + totals",
        ),
        sa.Column(
            "balanced",
            sa.Boolean(),
            nullable=False,
            comment="True when total debits equal total credits over the posted moves",
        ),
        sa.Column(
            "total_debit",
            sa.BigInteger(),
            nullable=False,
            comment="Total debits over posted moves in the period (IRR)",
        ),
        sa.Column(
            "total_credit",
            sa.BigInteger(),
            nullable=False,
            comment="Total credits over posted moves in the period (IRR)",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Row creation timestamp (UTC)",
        ),
        sa.UniqueConstraint(
            "validation_run_id",
            name="uq_report_snapshots_validation_run",
        ),
        sa.CheckConstraint(
            "(period_end IS NULL OR period_start IS NULL OR period_end >= period_start)",
            name="ck_report_snapshots_period_range",
        ),
        sa.CheckConstraint(
            "total_debit >= 0 AND total_credit >= 0",
            name="ck_report_snapshots_totals_non_negative",
        ),
    )
    op.create_index(
        "ix_report_snapshots_tenant_id",
        "report_snapshots",
        ["tenant_id"],
    )
    op.create_index(
        "ix_report_snapshots_tenant_created",
        "report_snapshots",
        ["tenant_id", "created_at"],
    )
    op.create_index(
        "ix_report_snapshots_validation_run_id",
        "report_snapshots",
        ["validation_run_id"],
    )


def downgrade() -> None:
    op.drop_table("report_snapshots")
