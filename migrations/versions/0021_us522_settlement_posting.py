"""Settlement posting ledger (US-522, Phase 5-3 Wave B).

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-19

Phased design §3.5: every journal the settlement engine posts to
Tryton leaves one row in ``settlement_postings`` — the idempotency
key (one posting per (payment/tranche, kind)), the Tryton
move/payment/reconciliation ids, the amount, and the reason + event
reference for adjustments. ``details`` is free-form JSONB for the
posting's breakdown (refund allocations per tranche, tie-out figures
for a bank receipt, variance figures for a fee adjustment); it never
carries secrets.

No other schema change: the tranche amount columns already exist
(``update_tranche`` gains amount parameters for refund allocation,
no DDL needed), and the tie-out gate is computed from the US-521
tables (events, matches, deposits, tranches) at post time — US-523
owns the tie-out record itself.

Chains after 0020 (US-521 settlement engine).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "settlement_postings",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key"),
        sa.Column(
            "installation_id",
            sa.String(36),
            nullable=False,
            comment="Installation id (from the authenticated API key)",
        ),
        sa.Column(
            "gateway_payment_id",
            sa.String(36),
            sa.ForeignKey("gateway_payments.id", ondelete="CASCADE"),
            nullable=False,
            comment="Owning gateway payment row",
        ),
        sa.Column(
            "tranche_id",
            sa.String(36),
            sa.ForeignKey("settlement_tranches.id", ondelete="SET NULL"),
            nullable=True,
            comment="Tranche this posting is for, when tranche-scoped",
        ),
        sa.Column(
            "kind",
            sa.String(32),
            nullable=False,
            comment=(
                "Posting kind: fee_at_sale / fee_at_settlement / clearing / "
                "bank_receipt / fee_variance / refund / refund_adjustment"
            ),
        ),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="posted",
            comment="posted (Tryton move posted) or draft (awaiting approval)",
        ),
        sa.Column(
            "amount_irr",
            sa.BigInteger(),
            nullable=False,
            comment="The posting's principal amount in whole rials",
        ),
        sa.Column(
            "tryton_move_id",
            sa.Integer(),
            nullable=True,
            comment="Tryton account.move id (external reference, no SQL FK into Tryton)",
        ),
        sa.Column(
            "tryton_payment_id",
            sa.Integer(),
            nullable=True,
            comment="Tryton account.payment id, for clearing postings (external reference)",
        ),
        sa.Column(
            "tryton_reconciliation_id",
            sa.Integer(),
            nullable=True,
            comment="Tryton account.move.reconciliation id (external reference)",
        ),
        sa.Column(
            "reason",
            sa.Text(),
            nullable=True,
            comment="Reason for the posting (mandatory for adjustments/refunds)",
        ),
        sa.Column(
            "event_ref",
            sa.String(512),
            nullable=True,
            comment="Canonical event / refund event reference for the posting",
        ),
        sa.Column(
            "details",
            postgresql.JSONB,
            nullable=True,
            comment="Posting breakdown (refund allocations, tie-out figures, variance figures)",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_settlement_postings_payment",
        "settlement_postings",
        ["gateway_payment_id"],
    )
    op.create_index(
        "ix_settlement_postings_tranche",
        "settlement_postings",
        ["tranche_id"],
    )
    op.create_index(
        "ix_settlement_postings_installation_kind",
        "settlement_postings",
        ["installation_id", "kind"],
    )
    # Idempotency: the once-only kinds get a partial unique index —
    # one fee_at_sale per payment, one fee_at_settlement per tranche.
    # clearing / bank_receipt are deliberately NOT unique: a tranche
    # that confirms or arrives in parts is cleared / received in
    # several postings (cumulative-amount math in the service makes a
    # duplicate call a no-op instead). fee_variance / refund /
    # refund_adjustment may also repeat per payment. (A single unique
    # index on (payment, tranche, kind) would not catch payment-level
    # duplicates — NULL tranche never equals NULL — hence two partial
    # indexes.)
    op.create_index(
        "uq_settlement_postings_tranche_kind",
        "settlement_postings",
        ["tranche_id", "kind"],
        unique=True,
        postgresql_where=sa.text(
            "tranche_id IS NOT NULL AND kind = 'fee_at_settlement'"
        ),
    )
    op.create_index(
        "uq_settlement_postings_payment_kind",
        "settlement_postings",
        ["gateway_payment_id", "kind"],
        unique=True,
        postgresql_where=sa.text("tranche_id IS NULL AND kind = 'fee_at_sale'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_settlement_postings_payment_kind", table_name="settlement_postings"
    )
    op.drop_index(
        "uq_settlement_postings_tranche_kind", table_name="settlement_postings"
    )
    op.drop_index(
        "ix_settlement_postings_installation_kind", table_name="settlement_postings"
    )
    op.drop_index("ix_settlement_postings_tranche", table_name="settlement_postings")
    op.drop_index("ix_settlement_postings_payment", table_name="settlement_postings")
    op.drop_table("settlement_postings")
