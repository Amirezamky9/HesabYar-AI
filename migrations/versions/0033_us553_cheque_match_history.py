"""Monthly clearing-match history (US-553, Phase 5-6 ۵ح).

Revision ID: 0033
Revises: 0031
Create Date: 2026-11-20

Cheque report 12 (monthly match-trend) needs STORED monthly match
history: the ``cheque_match_history`` table holds one row per
(installation, Jalali month) with the clearing matcher's outcome
counts (exact / probable / ambiguous / unmatched). The clearing
service records every run's newly decided lines here (upsert-add);
the trend chart renders FROM these rows, never recomputed from raw
match records at read time.

NO BACKFILL: history starts with the first matching run after
deploy. The pre-existing ``bank_statement_matches`` rows cannot
seed it faithfully — an unmatched clearing line is indistinguishable
from a generic reconcile unmatched line (both are
``target_type=MANUAL`` with an empty ``target_ref``), and suggested
matches carry no decision timestamp. See docs/cash-forecast.md.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0033"
down_revision: str | None = "0031"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "cheque_match_history",
        sa.Column("id", sa.String(36), nullable=False, comment="UUID primary key"),
        sa.Column(
            "tenant_id",
            sa.String(36),
            nullable=False,
            comment="Tenant id (from the authenticated API key)",
        ),
        sa.Column(
            "installation_id",
            sa.String(36),
            nullable=False,
            comment="Installation id (from the authenticated API key)",
        ),
        sa.Column(
            "month_jalali",
            sa.String(7),
            nullable=False,
            comment="Jalali month key 'YYYY-MM' the run's lines belong to",
        ),
        sa.Column(
            "exact_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="Lines matched exactly this month",
        ),
        sa.Column(
            "probable_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="Lines matched probably this month",
        ),
        sa.Column(
            "ambiguous_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="Lines left ambiguous this month",
        ),
        sa.Column(
            "unmatched_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="Lines left unmatched this month",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Row creation timestamp (UTC)",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Last-modified timestamp (UTC)",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "installation_id",
            "month_jalali",
            name="uq_cheque_match_history_installation_month",
        ),
    )
    op.create_index(
        "ix_cheque_match_history_installation_month",
        "cheque_match_history",
        ["installation_id", "month_jalali"],
    )


def downgrade() -> None:
    op.drop_table("cheque_match_history")
