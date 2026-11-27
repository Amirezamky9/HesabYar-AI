"""Installment tables (US-540, Phase 5-5 ۵ج).

Revision ID: 0027
Revises: 0026
Create Date: 2026-11-20

Phased design §1.3 ۵ج flow I1: the in-house installment schedule —
``installment_plans`` (one plan per sales invoice), ``installment_legs``
(the per-invoice schedule: sequence + due date + principal),
``installment_receipts`` (multi-stage receipts — the linkage guard's
record: ``invoice_id`` AND ``journal_move_id`` are both NOT NULL),
``installment_receipt_allocations`` (per-leg slices of a receipt),
and ``installment_sale_contracts`` (the user-entered late-fee rule,
penalty account and early-settlement terms, versioned).

No status column exists anywhere by design: leg and plan status are
computed from receipts + the wall-clock date, never stored. The
one-plan-per-invoice rule is a unique constraint on
(installation_id, invoice_id); the cheque module's
``installment_leg_id`` already points at ``installment_legs``
(US-531 carried the link; US-542 consumes it).

Chains after 0026 (US-533 cheque intake).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "installment_sale_contracts",
        sa.Column("id", sa.String(36), nullable=False, comment="UUID primary key"),
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
            "company_engine_id",
            sa.Integer(),
            nullable=False,
            comment="Engine company id holding this contract's books",
        ),
        sa.Column(
            "code",
            sa.String(64),
            nullable=False,
            comment="Contract code (versions share a code)",
        ),
        sa.Column(
            "name",
            sa.String(256),
            nullable=False,
            comment="Contract display name",
        ),
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            server_default="1",
            comment="Contract version (increments per code)",
        ),
        sa.Column(
            "effective_from",
            sa.Date(),
            nullable=False,
            comment="Date this version takes effect, Gregorian (payload is Jalali)",
        ),
        sa.Column(
            "late_fee_basis",
            sa.String(32),
            nullable=False,
            server_default="none",
            comment="Late-fee basis: 'none' | 'daily_percent' | 'monthly_percent' | 'fixed_per_day'",
        ),
        sa.Column(
            "late_fee_rate",
            sa.String(32),
            nullable=False,
            server_default="0",
            comment="Late-fee rate as exact decimal text (fraction for *_PERCENT, Rial/day for FIXED_PER_DAY)",
        ),
        sa.Column(
            "late_fee_grace_days",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="Days past the due date before the late fee starts accruing",
        ),
        sa.Column(
            "penalty_account_engine_id",
            sa.Integer(),
            nullable=True,
            comment="Engine account id of the penalty (finance income) account (NULL = not configured)",
        ),
        sa.Column(
            "penalty_account_code",
            sa.String(32),
            nullable=True,
            comment="Chart code of the penalty account, for display/audit",
        ),
        sa.Column(
            "early_settlement_fee_policy",
            sa.String(16),
            nullable=False,
            server_default="waived",
            comment="Accrued late fees on early settlement: 'waived' | 'kept'",
        ),
        sa.Column(
            "early_settlement_income_policy",
            sa.String(16),
            nullable=False,
            server_default="waived",
            comment="Unrealised finance income on early settlement: 'waived' | 'discounted' | 'full'",
        ),
        sa.Column(
            "supersedes_contract_id",
            sa.String(36),
            sa.ForeignKey("installment_sale_contracts.id"),
            nullable=True,
            comment="Previous version this version replaces (NULL for version 1)",
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
            comment="Whether this version is currently in force",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Row creation timestamp (UTC)",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Last-modified timestamp (UTC)",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "installation_id",
            "code",
            "version",
            name="uq_installment_contracts_installation_code_version",
        ),
    )
    op.create_index(
        "ix_installment_contracts_installation_code",
        "installment_sale_contracts",
        ["installation_id", "code"],
    )

    op.create_table(
        "installment_plans",
        sa.Column("id", sa.String(36), nullable=False, comment="UUID primary key"),
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
            "company_engine_id",
            sa.Integer(),
            nullable=False,
            comment="Engine company id holding this plan's books",
        ),
        sa.Column(
            "invoice_id",
            sa.String(128),
            nullable=False,
            comment="The ONE sales invoice this plan belongs to (HesaYar invoice id or engine id)",
        ),
        sa.Column(
            "contract_id",
            sa.String(36),
            sa.ForeignKey("installment_sale_contracts.id"),
            nullable=True,
            comment="Sale contract governing late fees / early settlement (NULL = no contract)",
        ),
        sa.Column(
            "customer_ref",
            sa.String(128),
            nullable=True,
            comment="Site customer reference (email/phone/customer code)",
        ),
        sa.Column(
            "total_principal_irr",
            sa.BigInteger(),
            nullable=False,
            comment="Total principal in Rial (sum of the legs; validated at creation)",
        ),
        sa.Column(
            "leg_count",
            sa.Integer(),
            nullable=False,
            comment="Number of legs in the schedule",
        ),
        sa.Column(
            "interval_days",
            sa.Integer(),
            nullable=True,
            comment="Days between legs (payment terms; NULL when legs were supplied explicitly)",
        ),
        sa.Column(
            "first_due_date",
            sa.Date(),
            nullable=True,
            comment="First leg due date, Gregorian (payment terms; NULL for explicit legs)",
        ),
        sa.Column(
            "auto_post",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="Posting preference for journals later stories create: draft by default (ADR 0003 decision 1)",
        ),
        sa.Column(
            "source",
            sa.String(16),
            nullable=False,
            server_default="manual",
            comment="Plan source: 'manual' (REST) | 'derived' (from sales.order.completed, ADR 0005)",
        ),
        sa.Column(
            "source_event_id",
            sa.String(128),
            nullable=True,
            comment="§10.2 source_event_id of the order event the plan was derived from (NULL for manual plans)",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Row creation timestamp (UTC)",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Last-modified timestamp (UTC)",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "installation_id",
            "invoice_id",
            name="uq_installment_plans_installation_invoice",
        ),
    )
    op.create_index(
        "ix_installment_plans_installation_created",
        "installment_plans",
        ["installation_id", "created_at"],
    )
    op.create_index(
        "ix_installment_plans_installation_contract",
        "installment_plans",
        ["installation_id", "contract_id"],
    )

    op.create_table(
        "installment_legs",
        sa.Column("id", sa.String(36), nullable=False, comment="UUID primary key"),
        sa.Column(
            "plan_id",
            sa.String(36),
            sa.ForeignKey("installment_plans.id"),
            nullable=False,
            comment="Plan this leg belongs to",
        ),
        sa.Column(
            "sequence",
            sa.Integer(),
            nullable=False,
            comment="Leg sequence within the plan (1-based)",
        ),
        sa.Column(
            "due_date",
            sa.Date(),
            nullable=False,
            comment="Leg due date, Gregorian (payload is Jalali)",
        ),
        sa.Column(
            "principal_irr",
            sa.BigInteger(),
            nullable=False,
            comment="Leg principal in Rial (payload Toman x10 at the boundary)",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Row creation timestamp (UTC)",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "plan_id",
            "sequence",
            name="uq_installment_legs_plan_sequence",
        ),
    )
    op.create_index(
        "ix_installment_legs_plan_due",
        "installment_legs",
        ["plan_id", "due_date"],
    )

    op.create_table(
        "installment_receipts",
        sa.Column("id", sa.String(36), nullable=False, comment="UUID primary key"),
        sa.Column(
            "plan_id",
            sa.String(36),
            sa.ForeignKey("installment_plans.id"),
            nullable=False,
            comment="Plan this receipt pays into",
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
            "invoice_id",
            sa.String(128),
            nullable=False,
            comment="Original invoice ref (linkage guard: must equal the plan's invoice)",
        ),
        sa.Column(
            "journal_move_id",
            sa.Integer(),
            nullable=False,
            comment="Engine move id of the accounting document for this receipt (linkage guard)",
        ),
        sa.Column(
            "amount_irr",
            sa.BigInteger(),
            nullable=False,
            comment="Receipt amount in Rial (payload Toman x10 at the boundary)",
        ),
        sa.Column(
            "received_date",
            sa.Date(),
            nullable=False,
            comment="Receipt date, Gregorian (payload is Jalali)",
        ),
        sa.Column(
            "payment_method",
            sa.String(16),
            nullable=False,
            server_default="cash",
            comment="Payment method: 'cash' | 'bank_transfer' | 'other' (US-540); "
            "'cheque'/'bnpl' arrive with US-542/US-543",
        ),
        sa.Column(
            "advance_irr",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
            comment="Overpayment remainder in Rial (amount - allocated; held, never auto-applied)",
        ),
        sa.Column(
            "reference",
            sa.String(128),
            nullable=True,
            comment="Caller-supplied receipt reference (bank ref, receipt number)",
        ),
        sa.Column(
            "notes",
            sa.Text(),
            nullable=True,
            comment="Free-text notes",
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
        "ix_installment_receipts_plan_received",
        "installment_receipts",
        ["plan_id", "received_date"],
    )
    op.create_index(
        "ix_installment_receipts_installation_invoice",
        "installment_receipts",
        ["installation_id", "invoice_id"],
    )

    op.create_table(
        "installment_receipt_allocations",
        sa.Column("id", sa.String(36), nullable=False, comment="UUID primary key"),
        sa.Column(
            "receipt_id",
            sa.String(36),
            sa.ForeignKey("installment_receipts.id"),
            nullable=False,
            comment="Receipt this allocation belongs to",
        ),
        sa.Column(
            "leg_id",
            sa.String(36),
            sa.ForeignKey("installment_legs.id"),
            nullable=False,
            comment="Leg this slice pays",
        ),
        sa.Column(
            "amount_irr",
            sa.BigInteger(),
            nullable=False,
            comment="Allocated amount in Rial (never more than the leg's balance)",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Row creation timestamp (UTC)",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "receipt_id",
            "leg_id",
            name="uq_installment_allocations_receipt_leg",
        ),
    )
    op.create_index(
        "ix_installment_allocations_leg",
        "installment_receipt_allocations",
        ["leg_id"],
    )


def downgrade() -> None:
    op.drop_table("installment_receipt_allocations")
    op.drop_table("installment_receipts")
    op.drop_table("installment_legs")
    op.drop_table("installment_plans")
    op.drop_table("installment_sale_contracts")
