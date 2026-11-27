"""Mapping confirmation counter for Suggest → Auto promotion (US-552, Phase 5-6 ۵ح).

Revision ID: 0032
Revises: 0034
Create Date: 2026-11-20

Phased design §7.2 (mapping learning): a confirmed manual mapping
becomes a suggestion/rule for the future; rules start in Suggest
(one-click confirm) and only become Auto after the configured
confirmation threshold (``MAPPING_AUTO_MIN_CONFIRMATIONS`` = 3,
mirroring the bank rules' ``RULE_AUTO_MIN_CONFIRMATIONS``).

This migration adds ONE column — no new table: the mapping center
(US-503) is already the store. ``integration_mappings.confirmations``
counts how many times the SAME target has been confirmed for one
(kind, channel, source): re-creating a mapping with an unchanged
target increments it, changing the target resets it to 1, and
promotion carries it unchanged (promoting is not a confirmation).
Existing rows get 1 (their creation was one confirmation).

Chains after 0034 (US-554 manual imports) — the revision id keeps
the US-552 allocation, but the merge order is the chain order
(US-553's 0033 and US-554's 0034 landed first).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0032"
down_revision: str | None = "0034"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "integration_mappings",
        sa.Column(
            "confirmations",
            sa.Integer(),
            nullable=False,
            server_default="1",
            comment="Confirmations of the same target (kind, channel, source); Auto needs 3 (US-552)",
        ),
    )


def downgrade() -> None:
    op.drop_column("integration_mappings", "confirmations")
