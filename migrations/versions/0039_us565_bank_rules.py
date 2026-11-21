"""Bank auto-coding rules + rule suggestions (US-565, Phase 5-7 ۵خ W3).

Revision ID: 0039
Revises: 0037
Create Date: 2026-11-21

The W3 user-defined bank rules (Xero-style): ``when <statement-line
pattern> → suggest <account + mapping>``. They are a different store
from the B2 learned rules (``bank_match_rules``, US-514): a W3 rule
is an explicit user definition with a richer pattern (description
substring, counterparty name, amount range in whole Rial, direction),
and it is born in SUGGEST mode — it becomes AUTO only through a human
promotion once ``RULE_AUTO_MIN_CONFIRMATIONS`` (3) of its suggestions
have been confirmed, the same threshold as the US-552
mapping-learning promotion.

- ``bank_rules`` — one row per user-defined rule: tenant + company,
  bank key (empty = every bank), name, the pattern conditions (at
  least one must be set — the ``ck_bank_rules_has_pattern`` check),
  the target account (+ optional party), the mode (suggest/auto),
  the confirmation count, the firing priority (lower first, first
  match wins), and the enabled flag.
- ``bank_rule_suggestions`` — one row per suggestion a rule made for
  one statement line, recorded when a reconcile run matches a line
  against a SUGGEST-mode rule. The partial unique index
  (``uq_bank_rule_suggestions_pending``) is the dedup backstop: at
  most one pending suggestion per (rule, statement, line), so a
  reconcile re-run cannot record the same decision twice. The row
  survives its rule's deletion (``ON DELETE SET NULL``) with the
  rule name + target snapshotted, so the audit trail stays readable.

Chains after 0037 (US-563 recurring templates — the Alembic head at
branch time).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0039"
down_revision: str | None = "0037"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "bank_rules",
        sa.Column("id", sa.String(36), nullable=False, comment="UUID primary key (bank rule id)"),
        sa.Column(
            "tenant_id",
            sa.String(36),
            nullable=False,
            comment="Owning tenant FK (CASCADE delete)",
        ),
        sa.Column(
            "company_id",
            sa.String(128),
            nullable=False,
            comment="HesabYar company row id (plain external reference, no FK)",
        ),
        sa.Column(
            "bank_key",
            sa.String(64),
            nullable=False,
            server_default="",
            comment="Bank key the rule applies to; empty = every bank",
        ),
        sa.Column(
            "name",
            sa.String(200),
            nullable=False,
            comment="Operator-facing rule name",
        ),
        sa.Column(
            "description_contains",
            sa.String(256),
            nullable=True,
            comment="Normalised description substring (NULL = no description condition)",
        ),
        sa.Column(
            "counterparty_contains",
            sa.String(256),
            nullable=True,
            comment="Normalised counterparty name substring (NULL = no counterparty condition)",
        ),
        sa.Column(
            "amount_min_irr",
            sa.Integer,
            nullable=True,
            comment="Minimum line magnitude in whole Rial, inclusive (NULL = no lower bound)",
        ),
        sa.Column(
            "amount_max_irr",
            sa.Integer,
            nullable=True,
            comment="Maximum line magnitude in whole Rial, inclusive (NULL = no upper bound)",
        ),
        sa.Column(
            "direction",
            sa.String(8),
            nullable=False,
            server_default="any",
            comment="in | out | any — money direction the rule applies to",
        ),
        sa.Column(
            "target_account_id",
            sa.Integer,
            nullable=False,
            comment="Engine account id the rule codes to",
        ),
        sa.Column(
            "target_party_id",
            sa.Integer,
            nullable=True,
            comment="Engine party id the rule codes to, when known",
        ),
        sa.Column(
            "target_kind",
            sa.String(32),
            nullable=False,
            server_default="account",
            comment="account | misc_income — what the target means",
        ),
        sa.Column(
            "mode",
            sa.String(16),
            nullable=False,
            server_default="suggest",
            comment="suggest | auto — Suggest first, Auto only after human promotion at threshold",
        ),
        sa.Column(
            "confirmations",
            sa.Integer,
            nullable=False,
            server_default="0",
            comment="How many of this rule's suggestions a human confirmed",
        ),
        sa.Column(
            "priority",
            sa.Integer,
            nullable=False,
            server_default="100",
            comment="Firing order inside one company's set (lower runs first; first match wins)",
        ),
        sa.Column(
            "enabled",
            sa.Boolean,
            nullable=False,
            server_default=sa.true(),
            comment="A disabled rule never fires (live or suggested)",
        ),
        sa.Column(
            "created_by",
            sa.String(128),
            nullable=True,
            comment="API client id that created the rule (NULL = system)",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Creation timestamp (UTC)",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Last update timestamp (UTC)",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "direction IN ('in', 'out', 'any')",
            name="ck_bank_rules_direction",
        ),
        sa.CheckConstraint("mode IN ('suggest', 'auto')", name="ck_bank_rules_mode"),
        sa.CheckConstraint("confirmations >= 0", name="ck_bank_rules_confirmations"),
        sa.CheckConstraint(
            "amount_min_irr IS NULL OR amount_min_irr >= 0",
            name="ck_bank_rules_amount_min_nonneg",
        ),
        sa.CheckConstraint(
            "amount_max_irr IS NULL OR amount_max_irr >= 0",
            name="ck_bank_rules_amount_max_nonneg",
        ),
        sa.CheckConstraint(
            "amount_min_irr IS NULL OR amount_max_irr IS NULL OR amount_min_irr <= amount_max_irr",
            name="ck_bank_rules_amount_range",
        ),
        sa.CheckConstraint(
            "description_contains IS NOT NULL OR counterparty_contains IS NOT NULL "
            "OR amount_min_irr IS NOT NULL OR amount_max_irr IS NOT NULL",
            name="ck_bank_rules_has_pattern",
        ),
        sa.CheckConstraint(
            "target_account_id > 0",
            name="ck_bank_rules_target_account",
        ),
    )
    op.create_index("ix_bank_rules_tenant_company", "bank_rules", ["tenant_id", "company_id"])
    op.create_index("ix_bank_rules_tenant_mode", "bank_rules", ["tenant_id", "mode"])

    op.create_table(
        "bank_rule_suggestions",
        sa.Column("id", sa.String(36), nullable=False, comment="UUID primary key (suggestion id)"),
        sa.Column(
            "tenant_id",
            sa.String(36),
            nullable=False,
            comment="Owning tenant FK (CASCADE delete)",
        ),
        sa.Column(
            "rule_id",
            sa.String(36),
            nullable=True,
            comment="Bank rule that suggested this coding (NULL when the rule was deleted)",
        ),
        sa.Column(
            "rule_name",
            sa.String(200),
            nullable=False,
            comment="Rule name snapshot (readable after the rule is deleted)",
        ),
        sa.Column(
            "company_id",
            sa.String(128),
            nullable=False,
            comment="HesabYar company row id (plain external reference, no FK)",
        ),
        sa.Column(
            "statement_id",
            sa.Integer,
            nullable=False,
            comment="Engine statement id of the suggested line",
        ),
        sa.Column(
            "statement_line_id",
            sa.Integer,
            nullable=False,
            comment="Engine statement line id",
        ),
        sa.Column(
            "line_description",
            sa.Text,
            nullable=False,
            comment="Statement line description snapshot",
        ),
        sa.Column(
            "line_amount_irr",
            sa.Integer,
            nullable=False,
            comment="Signed line amount in whole Rial",
        ),
        sa.Column(
            "target_account_id",
            sa.Integer,
            nullable=False,
            comment="Engine account id the rule suggested",
        ),
        sa.Column(
            "target_party_id",
            sa.Integer,
            nullable=True,
            comment="Engine party id the rule suggested, when known",
        ),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="pending",
            comment="pending | confirmed | rejected",
        ),
        sa.Column(
            "confirmed_by",
            sa.String(128),
            nullable=True,
            comment="Actor that confirmed/rejected the suggestion",
        ),
        sa.Column(
            "confirmed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the suggestion was confirmed/rejected (UTC)",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Creation timestamp (UTC)",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["rule_id"], ["bank_rules.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('pending', 'confirmed', 'rejected')",
            name="ck_bank_rule_suggestions_status",
        ),
        sa.CheckConstraint(
            "target_account_id > 0",
            name="ck_bank_rule_suggestions_target_account",
        ),
    )
    op.create_index(
        "ix_bank_rule_suggestions_tenant_rule",
        "bank_rule_suggestions",
        ["tenant_id", "rule_id"],
    )
    op.create_index(
        "ix_bank_rule_suggestions_tenant_status",
        "bank_rule_suggestions",
        ["tenant_id", "status"],
    )
    op.create_index(
        "uq_bank_rule_suggestions_pending",
        "bank_rule_suggestions",
        ["rule_id", "statement_id", "statement_line_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_table("bank_rule_suggestions")
    op.drop_table("bank_rules")
