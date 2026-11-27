"""Inbox events: installation context (US-511).

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-18

Phase 5-2 stream S1 (phased design §1.3 5ب): the webhook receiver
ingests events under a logical installation (from the API key), and
the registry reads the pipeline makes later — store, mapping,
company — are installation-scoped. The inbox event must therefore
remember which installation ingested it.

Adds a nullable ``installation_id`` to ``inbox_events`` (plain
value, no FK until the installations table lands in a later phase)
plus an index. Existing rows keep NULL; the pipeline fails closed
(unresolved installation context → review) for those.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "inbox_events",
        sa.Column(
            "installation_id",
            sa.String(36),
            nullable=True,
            comment=(
                "Logical installation that ingested the event (US-511); "
                "plain value, no FK until the installations table lands"
            ),
        ),
    )
    op.create_index("ix_inbox_events_installation_id", "inbox_events", ["installation_id"])


def downgrade() -> None:
    op.drop_index("ix_inbox_events_installation_id", table_name="inbox_events")
    op.drop_column("inbox_events", "installation_id")
