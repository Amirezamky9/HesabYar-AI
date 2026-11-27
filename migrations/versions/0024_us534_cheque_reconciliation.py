"""Cheque reconciliation columns (US-534, Phase 5-4 ۵ث).

Revision ID: 0024
Revises: 0023
Create Date: 2026-11-19

Phased design §5 §1.5: the clearing matcher needs the deposit date
(the start of the ``[deposit, deposit + clearing window]`` window),
and the clearing-matching-rate report (report 12) needs the
confidence + statement reference of the match that cleared a
cheque, plus the engine reconciliation id joining the deposit
move's clearing leg (سند ۲) with the clear move's clearing leg
(سند ۳).

All four columns are nullable: cheques deposited before US-534
recorded no deposit date, and they can never auto-match (the window
key cannot be verified for them) — they clear only by manual
confirmation. The deposit date is NEVER backfilled by this
migration.

NOTE (parallel stories): US-532 and US-533 are building in
parallel and may also add a migration; the coordinator resolves
migration numbering at merge. This file is named 0024 and chains
after 0023 — if another story takes 0024 first, renumber this file
(and its revision id) to the next free number.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "cheques",
        sa.Column(
            "deposit_date",
            sa.Date(),
            nullable=True,
            comment=(
                "Day the leaf was handed to the bank (start of the clearing "
                "date window, US-534 §1.5); NULL for cheques deposited before "
                "US-534 recorded it"
            ),
        ),
    )
    op.add_column(
        "cheques",
        sa.Column(
            "clearing_confidence",
            sa.String(16),
            nullable=True,
            comment="Confidence of the clearing match that cleared this cheque ('exact' | 'probable', US-534)",
        ),
    )
    op.add_column(
        "cheques",
        sa.Column(
            "clearing_statement_ref",
            sa.String(128),
            nullable=True,
            comment="Statement line that cleared this cheque ('statement:<id>:line:<id>', US-534)",
        ),
    )
    op.add_column(
        "cheques",
        sa.Column(
            "clearing_reconciliation_id",
            sa.Integer(),
            nullable=True,
            comment=(
                "Engine reconciliation id joining the deposit move's clearing "
                "leg (سند ۲) with the clear move's clearing leg (سند ۳), US-534"
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("cheques", "clearing_reconciliation_id")
    op.drop_column("cheques", "clearing_statement_ref")
    op.drop_column("cheques", "clearing_confidence")
    op.drop_column("cheques", "deposit_date")
