"""Store read-only DB DSN column (US-512, S2).

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-18

Phase 5-2 stream S2 (store reconciliation & backfill): the store
registry gains one nullable column, ``stores.store_db_dsn_ciphertext``
— the Fernet-encrypted DSN of the store team's read-only database
user, used ONLY for reconciliation/backfill reads (ADR 0003
decision 3: zero writes, documented versioned queries). The DSN is
supplied by the store team and stored via
``POST /integrations/stores/{store_key}/db-credentials``; no read
ever returns it — the read model carries only ``has_store_db_dsn``.

Chains after 0011 (US-504 review queue). The coordinator re-chains
parallel Phase 5-2 migrations at merge time.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    """Add the encrypted read-only store-DB DSN column to stores."""
    op.add_column(
        "stores",
        sa.Column(
            "store_db_dsn_ciphertext",
            sa.Text(),
            nullable=True,
            comment="Encrypted read-only store-database DSN for reconciliation/backfill (Fernet, US-512)",
        ),
    )


def downgrade() -> None:
    """Drop the encrypted read-only store-DB DSN column from stores."""
    op.drop_column("stores", "store_db_dsn_ciphertext")
