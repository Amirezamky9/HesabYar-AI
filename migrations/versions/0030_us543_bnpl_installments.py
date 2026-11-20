"""BNPL installments (US-543, Phase 5-5 ۵ج).

Revision ID: 0030
Revises: 0027
Create Date: 2026-11-20

Phased design §1.3 ۵ج flow I2: the BNPL side of installments —
a sale whose legs are paid by the gateway in tranches (the US-521
settlement engine). The customer's own installments to the
gateway are NEVER in the seller's books; HesabYar tracks only the
gateway receivable + the gateway's settlement tranches.

- ``installment_plans.kind`` — ``internal`` (default) vs ``bnpl``:
  the structural separation (separate report filters, separate
  receivable accounts via the channel's mapping-center wiring; a
  BNPL plan never accrues internal finance income and never
  enters dunning). Existing plans are all internal, so the
  server default backfills them correctly.
- ``installment_plans.gateway_channel_key`` — the ONE gateway
  channel a BNPL plan is pinned to (NULL for internal plans).
- ``installment_receipts.gateway_channel_key`` /
  ``gateway_reference`` / ``gateway_payment_id`` / ``bnpl_status``
  — the BNPL receipt's link to its gateway payment
  (``gateway_payments`` row, created by the settlement engine at
  receipt time; a cross-module reference with NO SQL FK, like the
  settlement tables' Tryton refs) + the gateway-side state
  (``active`` / ``rejected`` / ``cancelled``). All NULL for
  non-BNPL receipts.

Chains after 0027 (US-540); the Phase 5-5 coordinator re-chains
at merge if US-541/US-542 land their own revisions first — this
revision id (0030) is kept stable for that re-chain.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0030"
down_revision: str | None = "0027"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "installment_plans",
        sa.Column(
            "kind",
            sa.String(16),
            nullable=False,
            server_default="internal",
            comment="Plan kind (US-543): 'internal' (customer pays the seller) | 'bnpl' (the gateway pays in tranches)",
        ),
    )
    op.add_column(
        "installment_plans",
        sa.Column(
            "gateway_channel_key",
            sa.String(64),
            nullable=True,
            comment="BNPL gateway channel key (US-543; required for BNPL plans, NULL for internal)",
        ),
    )
    op.create_index(
        "ix_installment_plans_installation_kind",
        "installment_plans",
        ["installation_id", "kind"],
    )

    op.add_column(
        "installment_receipts",
        sa.Column(
            "gateway_channel_key",
            sa.String(64),
            nullable=True,
            comment="BNPL gateway channel key (US-543; NULL for non-BNPL receipts)",
        ),
    )
    op.add_column(
        "installment_receipts",
        sa.Column(
            "gateway_reference",
            sa.String(128),
            nullable=True,
            comment="Gateway transaction id (US-543; the settlement engine's gateway_transaction_id)",
        ),
    )
    op.add_column(
        "installment_receipts",
        sa.Column(
            "gateway_payment_id",
            sa.String(36),
            nullable=True,
            comment="gateway_payments row id (US-543; the receivable from the gateway; no SQL FK — cross-module ref)",
        ),
    )
    op.add_column(
        "installment_receipts",
        sa.Column(
            "bnpl_status",
            sa.String(16),
            nullable=True,
            comment="BNPL status (US-543): 'active' | 'rejected' | 'cancelled'; NULL for non-BNPL receipts",
        ),
    )
    op.create_index(
        "ix_installment_receipts_gateway_payment",
        "installment_receipts",
        ["gateway_payment_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_installment_receipts_gateway_payment", table_name="installment_receipts"
    )
    op.drop_column("installment_receipts", "bnpl_status")
    op.drop_column("installment_receipts", "gateway_payment_id")
    op.drop_column("installment_receipts", "gateway_reference")
    op.drop_column("installment_receipts", "gateway_channel_key")
    op.drop_index("ix_installment_plans_installation_kind", table_name="installment_plans")
    op.drop_column("installment_plans", "gateway_channel_key")
    op.drop_column("installment_plans", "kind")
