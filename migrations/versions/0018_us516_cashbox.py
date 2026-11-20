"""Cash-box counting: registry, sessions, settings (US-516).

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-18

Phase 5 wave 2, stream K2 (phased design §6 هـ.۲ / §1.3 5چ): the
Iranian three-step cash-box method — supervised count, signed
minutes as the supporting document, shortage/surplus journal. Three
tables:

- ``cash_boxes`` — one row per cash box: the HesabYar-side record
  for one Tryton Statement Journal (the daybook) with its cash
  account. One box per (tenant, company, statement journal);
  petty-cash boxes carry a positive ceiling.
- ``cash_count_sessions`` — one row per counting session (a
  HesabYar-side entity — Tryton has none): cashier, supervisor,
  read-only book-balance snapshot, counted amount with an optional
  denomination breakdown, signed-minutes signatures, and the links
  to the ``cashbox_variance`` review item and the draft/posted
  variance journal. Link-extension columns (cheques, bank deposit,
  orders, installments) stay empty until their source modules land.
- ``cashbox_settings`` — one row per tenant: the variance policy
  (policy a ``expense`` by default, policy b
  ``receivable_from_cashier``), the material-variance threshold,
  the uncounted-box reminder threshold, the count schedule, the
  supervisor control and the policy-b settlement deadline.

Chains after 0011 (US-504 review queue).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "cash_boxes",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key (cash box id)"),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            comment="Owning tenant FK (CASCADE delete)",
        ),
        sa.Column(
            "company_id",
            sa.String(128),
            nullable=False,
            comment="HesabYar engine_companies row id; plain external reference, no FK",
        ),
        sa.Column(
            "name_fa",
            sa.String(128),
            nullable=False,
            comment="Cash-box name in Persian (e.g. 'صندوق فروشگاه')",
        ),
        sa.Column(
            "kind",
            sa.String(16),
            nullable=False,
            comment="store | petty_cash | other",
        ),
        sa.Column(
            "statement_journal_id",
            sa.Integer,
            nullable=False,
            comment="Engine id of the Tryton account.statement.journal (the daybook)",
        ),
        sa.Column(
            "cash_account_id",
            sa.Integer,
            nullable=False,
            comment="Engine id of the journal's cash account",
        ),
        sa.Column(
            "cash_account_code",
            sa.String(32),
            nullable=False,
            comment="Code of the journal's cash account (e.g. '1.1.1')",
        ),
        sa.Column(
            "ceiling_irr",
            sa.BigInteger,
            nullable=True,
            comment="Petty-cash ceiling in IRR (petty-cash boxes only)",
        ),
        sa.Column(
            "active",
            sa.Boolean,
            nullable=False,
            server_default=sa.true(),
            comment="Inactive boxes cannot start counting sessions",
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
            comment="Last update timestamp (UTC)",
        ),
        sa.CheckConstraint(
            "kind IN ('store', 'petty_cash', 'other')",
            name="ck_cash_boxes_kind",
        ),
        sa.CheckConstraint(
            "(ceiling_irr IS NULL OR ceiling_irr > 0)",
            name="ck_cash_boxes_ceiling_positive",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "company_id",
            "statement_journal_id",
            name="uq_cash_boxes_tenant_company_journal",
        ),
        sa.Index("ix_cash_boxes_tenant_company", "tenant_id", "company_id"),
    )
    op.create_table(
        "cash_count_sessions",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key (session id)"),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            comment="Owning tenant FK (CASCADE delete)",
        ),
        sa.Column(
            "company_id",
            sa.String(128),
            nullable=False,
            comment="HesabYar engine_companies row id; plain external reference, no FK",
        ),
        sa.Column(
            "engine_company_id",
            sa.Integer,
            nullable=False,
            comment="Engine company id (denormalized from company_id at session start)",
        ),
        sa.Column(
            "cash_box_id",
            sa.String(36),
            sa.ForeignKey("cash_boxes.id", ondelete="CASCADE"),
            nullable=False,
            comment="Cash box FK (CASCADE delete)",
        ),
        sa.Column(
            "cashier",
            sa.String(128),
            nullable=False,
            comment="Cashier (actor name/id) who counts",
        ),
        sa.Column(
            "supervisor",
            sa.String(128),
            nullable=False,
            comment="Supervisor (actor name/id) who witnesses — differs from the cashier",
        ),
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            comment="in_progress | awaiting_supervisor | awaiting_manager | archived",
        ),
        sa.Column(
            "book_balance_irr",
            sa.BigInteger,
            nullable=False,
            comment="Book balance snapshot (IRR, posted moves only, read-only from Tryton)",
        ),
        sa.Column(
            "counted_amount_irr",
            sa.BigInteger,
            nullable=True,
            comment="Physically counted amount in IRR (None until entered)",
        ),
        sa.Column(
            "denominations",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
            comment="Optional denomination breakdown: [{denomination_irr, count}]",
        ),
        sa.Column(
            "minutes_text",
            sa.Text,
            nullable=True,
            comment="Signed-minutes body (Persian; required when the variance is non-zero)",
        ),
        sa.Column(
            "witnessed_by",
            sa.String(128),
            nullable=True,
            comment="Supervisor who attended the count (required in every session)",
        ),
        sa.Column(
            "witnessed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the supervisor attended (UTC)",
        ),
        sa.Column(
            "minutes_cashier_signed_by",
            sa.String(128),
            nullable=True,
            comment="Cashier who signed the minutes",
        ),
        sa.Column(
            "minutes_cashier_signed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the cashier signed the minutes (UTC)",
        ),
        sa.Column(
            "minutes_supervisor_signed_by",
            sa.String(128),
            nullable=True,
            comment="Supervisor who signed the minutes",
        ),
        sa.Column(
            "minutes_supervisor_signed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the supervisor signed the minutes (UTC)",
        ),
        sa.Column(
            "minutes_manager_signed_by",
            sa.String(128),
            nullable=True,
            comment="Manager who signed the minutes",
        ),
        sa.Column(
            "minutes_manager_signed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the manager signed the minutes (UTC)",
        ),
        sa.Column(
            "review_item_id",
            sa.String(36),
            nullable=True,
            comment="Linked cashbox_variance review item; plain external reference, no FK",
        ),
        sa.Column(
            "draft_journal_id",
            sa.Integer,
            nullable=True,
            comment="Engine id of the DRAFT variance journal (posted only after manager approval)",
        ),
        sa.Column(
            "posted_journal_id",
            sa.Integer,
            nullable=True,
            comment="Engine id of the variance journal once posted",
        ),
        sa.Column(
            "notes",
            sa.Text,
            nullable=True,
            comment="Free-form session notes",
        ),
        sa.Column(
            "cheque_refs",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
            comment="Link extension point: cheque refs (5ث) — empty until that module lands",
        ),
        sa.Column(
            "deposit_ref",
            sa.String(128),
            nullable=True,
            comment="Link extension point: bank-deposit ref (5ت B1/B2) — empty until it lands",
        ),
        sa.Column(
            "order_refs",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
            comment="Link extension point: order refs (5ب S1/P1) — empty until they land",
        ),
        sa.Column(
            "installment_refs",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
            comment="Link extension point: installment refs (5ج) — empty until they land",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Row creation timestamp (UTC) — the session start",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Last update timestamp (UTC)",
        ),
        sa.CheckConstraint(
            "status IN ('in_progress', 'awaiting_supervisor', 'awaiting_manager', 'archived')",
            name="ck_cash_count_sessions_status",
        ),
        sa.CheckConstraint(
            "(counted_amount_irr IS NULL OR counted_amount_irr >= 0)",
            name="ck_cash_count_sessions_counted_non_negative",
        ),
        sa.Index("ix_cash_count_sessions_tenant_box_created", "tenant_id", "cash_box_id", "created_at"),
        sa.Index("ix_cash_count_sessions_tenant_status", "tenant_id", "status"),
    )
    op.create_table(
        "cashbox_settings",
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            primary_key=True,
            comment="Owning tenant FK (CASCADE delete) — one settings row per tenant",
        ),
        sa.Column(
            "variance_policy",
            sa.String(32),
            nullable=False,
            comment="expense (policy a, default) | receivable_from_cashier (policy b)",
        ),
        sa.Column(
            "material_variance_threshold_irr",
            sa.BigInteger,
            nullable=True,
            comment="Material-variance threshold in IRR (None = no materiality split)",
        ),
        sa.Column(
            "count_reminder_days",
            sa.Integer,
            nullable=False,
            server_default="7",
            comment="'Cash box uncounted for N days' alert threshold",
        ),
        sa.Column(
            "count_schedule",
            sa.String(16),
            nullable=False,
            server_default="daily",
            comment="daily | weekly | period_end",
        ),
        sa.Column(
            "supervisor_required",
            sa.Boolean,
            nullable=False,
            server_default=sa.true(),
            comment="Internal control: the supervisor must differ from the cashier",
        ),
        sa.Column(
            "shortage_settlement_hours",
            sa.Integer,
            nullable=False,
            server_default="24",
            comment="Shortage-settlement deadline under policy (b), in hours",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Last update timestamp (UTC)",
        ),
        sa.CheckConstraint(
            "variance_policy IN ('expense', 'receivable_from_cashier')",
            name="ck_cashbox_settings_variance_policy",
        ),
        sa.CheckConstraint(
            "(material_variance_threshold_irr IS NULL OR material_variance_threshold_irr >= 0)",
            name="ck_cashbox_settings_threshold_non_negative",
        ),
        sa.CheckConstraint(
            "count_reminder_days >= 1",
            name="ck_cashbox_settings_reminder_days_positive",
        ),
        sa.CheckConstraint(
            "count_schedule IN ('daily', 'weekly', 'period_end')",
            name="ck_cashbox_settings_count_schedule",
        ),
        sa.CheckConstraint(
            "shortage_settlement_hours >= 1",
            name="ck_cashbox_settings_settlement_hours_positive",
        ),
    )


def downgrade() -> None:
    op.drop_table("cashbox_settings")
    op.drop_table("cash_count_sessions")
    op.drop_table("cash_boxes")
