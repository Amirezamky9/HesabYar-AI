"""Installment finance columns + tables (US-541, Phase 5-5 ۵ج).

Revision ID: 0028
Revises: 0029 (US-542/US-543 merged first; re-chained at the US-541 merge)
Create Date: 2026-11-20

Phased design §1.3 ۵ج flow I1: the finance side of the in-house
installment schedule — the contract's finance-income rate and
stepped-dunning schedule, the receivable account (debit side of the
late-fee journal), the early-settlement discount rate, the plan's
applied-advance tracking, the settlement portions on receipts, and
two new tables: ``installment_late_fee_assessments`` (the cumulative
per-leg fee snapshot — the dunning run's idempotency record) and
``installment_schedule_amendments`` (the audit record of a
re-amortisation).

All changes are additive: new columns carry defaults (existing
contracts get rate 0 / no dunning schedule / no receivable account
— i.e. "no finance terms", never a guessed rate), and the new
tables start empty. Nothing here alters a 0027 column.

Chains after 0027 (US-540 installments).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0028"
down_revision: str | None = "0029"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    # -- installment_sale_contracts: the US-541 finance terms --------------
    op.add_column(
        "installment_sale_contracts",
        sa.Column(
            "finance_income_rate",
            sa.String(32),
            nullable=False,
            server_default="0",
            comment="Finance-income rate as exact decimal text: the fraction of principal "
            "charged as finance income over the whole plan (0 = none)",
        ),
    )
    op.add_column(
        "installment_sale_contracts",
        sa.Column(
            "dunning_offsets",
            sa.String(256),
            nullable=False,
            server_default="",
            comment="Dunning schedule as comma-separated day offsets, e.g. '3,7,14,30'; "
            "empty means no dunning schedule",
        ),
    )
    op.add_column(
        "installment_sale_contracts",
        sa.Column(
            "receivable_account_engine_id",
            sa.Integer(),
            nullable=True,
            comment="Engine account id of the customer-receivable account: the debit side "
            "of the late-fee journal (NULL = not configured)",
        ),
    )
    op.add_column(
        "installment_sale_contracts",
        sa.Column(
            "receivable_account_code",
            sa.String(32),
            nullable=True,
            comment="Chart code of the receivable account, for display/audit",
        ),
    )
    op.add_column(
        "installment_sale_contracts",
        sa.Column(
            "early_settlement_discount_rate",
            sa.String(32),
            nullable=False,
            server_default="0",
            comment="Discount fraction as exact decimal text, applied to the remaining "
            "unrealised income when the income policy is 'discounted'",
        ),
    )

    # -- installment_plans: applied-advance tracking -------------------------
    op.add_column(
        "installment_plans",
        sa.Column(
            "applied_advance_irr",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
            comment="Advance already applied to the schedule by early settlement; "
            "the held advance is the receipts' advances minus this",
        ),
    )

    # -- installment_receipts: settlement portions ---------------------------
    op.add_column(
        "installment_receipts",
        sa.Column(
            "finance_income_irr",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
            comment="Finance-income portion of a settlement receipt in Rial (0 on an ordinary receipt)",
        ),
    )
    op.add_column(
        "installment_receipts",
        sa.Column(
            "late_fee_irr",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
            comment="Late-fee portion of a settlement receipt in Rial (0 on an ordinary receipt)",
        ),
    )
    op.add_column(
        "installment_receipts",
        sa.Column(
            "applied_advance_irr",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
            comment="Held advance from earlier receipts applied by this receipt in Rial "
            "(a settlement receipt's allocations may exceed its amount by exactly this)",
        ),
    )
    op.add_column(
        "installment_receipts",
        sa.Column(
            "is_early_settlement",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="True when this receipt is an early-settlement receipt",
        ),
    )

    # -- installment_late_fee_assessments (new) ------------------------------
    op.create_table(
        "installment_late_fee_assessments",
        sa.Column("id", sa.String(36), nullable=False, comment="UUID primary key"),
        sa.Column(
            "leg_id",
            sa.String(36),
            sa.ForeignKey("installment_legs.id"),
            nullable=False,
            comment="Leg this assessment covers",
        ),
        sa.Column(
            "plan_id",
            sa.String(36),
            sa.ForeignKey("installment_plans.id"),
            nullable=False,
            comment="Plan the leg belongs to",
        ),
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
            "assessed_through",
            sa.Date(),
            nullable=False,
            comment="Date the fee was accrued through, Gregorian",
        ),
        sa.Column(
            "days_overdue",
            sa.Integer(),
            nullable=False,
            comment="Whole days past the leg's due date at assessment time",
        ),
        sa.Column(
            "fee_irr",
            sa.BigInteger(),
            nullable=False,
            comment="Total late fee accrued on the leg through assessed_through, in Rial",
        ),
        sa.Column(
            "journal_move_id",
            sa.Integer(),
            nullable=True,
            comment="Engine move id of the draft/posted journal for this increment (NULL = not journaled)",
        ),
        sa.Column(
            "journal_state",
            sa.String(16),
            nullable=False,
            server_default="none",
            comment="Journal state: 'none' | 'draft' | 'posted'",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Row creation timestamp (UTC)",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "leg_id",
            "assessed_through",
            name="uq_installment_assessments_leg_date",
        ),
    )
    op.create_index(
        "ix_installment_assessments_leg",
        "installment_late_fee_assessments",
        ["leg_id", "assessed_through"],
    )

    # -- installment_schedule_amendments (new) -------------------------------
    op.create_table(
        "installment_schedule_amendments",
        sa.Column("id", sa.String(36), nullable=False, comment="UUID primary key"),
        sa.Column(
            "plan_id",
            sa.String(36),
            sa.ForeignKey("installment_plans.id"),
            nullable=False,
            comment="Plan whose schedule changed",
        ),
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
            "kind",
            sa.String(32),
            nullable=False,
            comment="Amendment kind: 're_amortisation' (later kinds extend this)",
        ),
        sa.Column(
            "applied_advance_irr",
            sa.BigInteger(),
            nullable=False,
            comment="Advance applied to the schedule by this amendment, in Rial",
        ),
        sa.Column(
            "legs_before",
            JSONB,
            nullable=False,
            comment="Leg principals before the change: [{leg_id, sequence, principal_irr}]",
        ),
        sa.Column(
            "legs_after",
            JSONB,
            nullable=False,
            comment="Leg principals after the change: [{leg_id, sequence, principal_irr}]",
        ),
        sa.Column(
            "receipt_id",
            sa.String(36),
            sa.ForeignKey("installment_receipts.id"),
            nullable=True,
            comment="Receipt that triggered the amendment (the partial settlement receipt)",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Row creation timestamp (UTC)",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_installment_amendments_plan_created",
        "installment_schedule_amendments",
        ["plan_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("installment_schedule_amendments")
    op.drop_table("installment_late_fee_assessments")
    op.drop_column("installment_receipts", "is_early_settlement")
    op.drop_column("installment_receipts", "applied_advance_irr")
    op.drop_column("installment_receipts", "late_fee_irr")
    op.drop_column("installment_receipts", "finance_income_irr")
    op.drop_column("installment_plans", "applied_advance_irr")
    op.drop_column("installment_sale_contracts", "early_settlement_discount_rate")
    op.drop_column("installment_sale_contracts", "receivable_account_code")
    op.drop_column("installment_sale_contracts", "receivable_account_engine_id")
    op.drop_column("installment_sale_contracts", "dunning_offsets")
    op.drop_column("installment_sale_contracts", "finance_income_rate")
