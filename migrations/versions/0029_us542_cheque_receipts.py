"""Cheque receipts on installment receipts (US-542, Phase 5-5 ۵ج).

Revision ID: 0029
Revises: 0030 (US-543 merged first; re-chained at the US-542 merge)
Create Date: 2026-11-20

Two nullable columns on ``installment_receipts`` — no new table,
no backfill, no NOT NULL (existing receipts are cash/bank/other
and keep NULLs, which is correct for them):

- ``cheque_id`` — the cheque backing a cheque receipt (set only
  when ``payment_method = 'cheque'``; one receipt per cheque, so
  the column is the receipt → cheque link the split + the plan
  detail + the collection hook resolve through). Indexed: the
  collection hook looks the receipt up by cheque.
- ``collected_at`` — the date the money actually arrived (the
  collection hook's record, consumed by US-541's income engine:
  income is recognised on COLLECTION, never on receipt). Cash /
  bank-transfer / other receipts are collected on receipt
  (``collected_at == received_date`` at creation); a cheque
  receipt is collected when its cheque clears (the leg outcome
  hook sets it); a bounced cheque's receipt is never collected
  (NULL). Pre-US-542 receipts keep NULL — US-541 treats a NULL
  ``collected_at`` on a non-cheque receipt as collected on its
  ``received_date`` (the backfill is US-541's, not this
  migration's: the column's meaning is defined by the hook, and
  US-541 owns the income query).

The leg ↔ cheque link itself needs no schema change: the cheque's
``installment_leg_id`` (US-531) already points at
``installment_legs``. Leg status stays computed, never stored — a
cheque clear/bounce changes the leg's status with no leg-side
write at all.

Chains after 0027 (US-540 installments). The coordinator re-chains
at merge if a parallel story (US-541) lands a 0028 first.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0029"
down_revision: str | None = "0030"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "installment_receipts",
        sa.Column(
            "cheque_id",
            sa.String(36),
            nullable=True,
            comment=(
                "Cheque backing this receipt (US-542; set only for "
                "payment_method='cheque'; one receipt per cheque)"
            ),
        ),
    )
    op.add_column(
        "installment_receipts",
        sa.Column(
            "collected_at",
            sa.Date(),
            nullable=True,
            comment=(
                "Date the money actually arrived (US-542 collection hook): "
                "received_date for cash/bank/other receipts; the clear date "
                "for a cheque receipt; NULL while uncollected"
            ),
        ),
    )
    op.create_index(
        "ix_installment_receipts_cheque",
        "installment_receipts",
        ["cheque_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_installment_receipts_cheque",
        table_name="installment_receipts",
    )
    op.drop_column("installment_receipts", "collected_at")
    op.drop_column("installment_receipts", "cheque_id")
