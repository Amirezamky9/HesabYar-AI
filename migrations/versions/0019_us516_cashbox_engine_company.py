"""Cash-box registry: denormalize the engine company id (US-516).

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-18

Phase 5 wave 2, stream K2 (phased design §6 هـ.۲ / §1.3 5چ): the
get-by-id route must not require a ``company`` query parameter —
the box already knows its company. ``cash_boxes.engine_company_id``
denormalizes the Tryton company id at registration (same as
``cash_count_sessions.engine_company_id`` in 0012) so the live
balance lookup needs no second row resolution.

The backfill joins ``engine_companies`` on the row id; every
cash-box row ever written came from a registration that resolved
its company row first, so the join always matches. The column is
NOT NULL after the backfill.

Chains after 0012 (US-516 cash-box tables).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "cash_boxes",
        sa.Column(
            "engine_company_id",
            sa.Integer(),
            nullable=True,
            comment="Tryton company id, denormalized at registration",
        ),
    )
    op.execute(
        """
        UPDATE cash_boxes cb
        SET engine_company_id = ec.external_company_id::integer
        FROM engine_companies ec
        WHERE ec.id = cb.company_id
        """
    )
    op.alter_column("cash_boxes", "engine_company_id", nullable=False)


def downgrade() -> None:
    op.drop_column("cash_boxes", "engine_company_id")
