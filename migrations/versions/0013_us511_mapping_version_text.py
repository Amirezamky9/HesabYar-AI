"""Audit mapping_version: VARCHAR(64) -> TEXT (US-511).

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-18

Phase 5-2 stream S1 (phased design §1.3 5ب): the store pipeline
resolves several registry mappings per event (store→company,
party, one revenue account per category, shipping, gateway
journal), and the audit decision records the full composite
``kind:source_key:vN`` list in ``mapping_version`` for
traceability. A realistic order already exceeds 64 characters
(~160), so the insert failed with StringDataRightTruncation and
poisoned the worker's session (the whole poll sweep 500'd).

The audit trail must keep the full value — truncating or hashing
it would destroy the traceability the column exists for — so
both ``mapping_version`` columns (``automation_decision_log``
and ``review_item_audit``) become unbounded TEXT.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.alter_column(
        "automation_decision_log",
        "mapping_version",
        existing_type=sa.String(64),
        type_=sa.Text(),
        existing_nullable=True,
    )
    op.alter_column(
        "review_item_audit",
        "mapping_version",
        existing_type=sa.String(64),
        type_=sa.Text(),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "review_item_audit",
        "mapping_version",
        existing_type=sa.Text(),
        type_=sa.String(64),
        existing_nullable=True,
    )
    op.alter_column(
        "automation_decision_log",
        "mapping_version",
        existing_type=sa.Text(),
        type_=sa.String(64),
        existing_nullable=True,
    )
