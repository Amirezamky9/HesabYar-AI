"""SMS provider registry + reminder log (US-544, Phase 5-5 ۵ج).

Revision ID: 0031
Revises: 0027
Create Date: 2026-11-20

ADR 0003 amendment (2026-11-18): the multi-provider SMS registry
— ``sms_providers`` (Kavenegar + sms.ir + simulator built-ins,
other providers addable per installation; price / rate limit /
line type / contract / eNamad are USER-ENTERED columns, never
hard-coded), ``sms_reminder_settings`` (one policy row per
installation: master opt-in + offsets + user-editable templates),
``sms_contract_reminder_settings`` (the per-contract opt-in for
automatic sends), and ``sms_reminder_log`` (every reminder attempt
— sent, failed, skipped, or dry-run — with provider, cost, and
delivery status; the DLQ + the audit trail).

Exactly one default provider per installation (partial unique
index on ``is_default``); a leg + offset is reminded at most once
(partial unique index on (installation, leg, offset) over the
sent statuses — the dedup backstop). Credentials live in
``credentials_ciphertext`` (Fernet-encrypted JSON); nothing in
this migration touches a live provider.

Also widens the review queue's item-type check constraints
(``ck_review_items_item_type``,
``ck_review_sla_policies_item_type``) with the tenth type
``sms_delivery_failed``: a failed reminder files a review item,
never silently.

Chains after 0027 (US-540 installments).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0031"
down_revision: str | None = "0028"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_ITEM_TYPES_NINE = (
    "'mapping', 'settlement_variance', 'cheque_exception', "
    "'bank_unmatched', 'packaging_reorder', 'installment_overdue', "
    "'manual_import_review', 'cashbox_variance', 'inbox_dead_letter'"
)
_ITEM_TYPES_TEN = _ITEM_TYPES_NINE + ", 'sms_delivery_failed'"


def upgrade() -> None:
    op.create_table(
        "sms_providers",
        sa.Column("id", sa.String(36), nullable=False, comment="UUID primary key"),
        sa.Column(
            "installation_id",
            sa.String(36),
            nullable=False,
            comment="Installation id (from the authenticated API key)",
        ),
        sa.Column(
            "provider_key",
            sa.String(64),
            nullable=False,
            comment="Stable slug: 'kavenegar' | 'sms_ir' | 'simulator' | a custom key",
        ),
        sa.Column(
            "display_name_fa",
            sa.String(256),
            nullable=False,
            comment="Provider display name (Persian)",
        ),
        sa.Column(
            "provider_kind",
            sa.String(32),
            nullable=False,
            comment="Client kind: 'kavenegar' | 'sms_ir' | 'simulator' | 'custom'",
        ),
        sa.Column(
            "is_builtin",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="True for the rows the seed creates",
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="Whether reminders may send through this provider",
        ),
        sa.Column(
            "is_default",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="Whether this is the installation's default provider (one per installation)",
        ),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="setup",
            comment="Operational status: 'setup' | 'active' | 'disabled' | 'error'",
        ),
        sa.Column(
            "line_number",
            sa.String(32),
            nullable=True,
            comment="Sender line number (user-entered; sms.ir requires an int64 line)",
        ),
        sa.Column(
            "price_per_sms_toman",
            sa.String(32),
            nullable=True,
            comment="Informational price per SMS in Toman, exact decimal text (user-entered; never x10-converted)",
        ),
        sa.Column(
            "rate_limit_per_minute",
            sa.Integer(),
            nullable=True,
            comment="User-entered rate limit in messages per minute (NULL = unknown)",
        ),
        sa.Column(
            "line_type",
            sa.String(32),
            nullable=True,
            comment="Line kind, free string (conventional: 'service' | 'dedicated')",
        ),
        sa.Column(
            "contract_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="Whether the provider requires a contract with the operator",
        ),
        sa.Column(
            "enmad_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="Whether the provider requires an eNamad for the store",
        ),
        sa.Column(
            "notes",
            sa.Text(),
            nullable=True,
            comment="Free-text operator notes",
        ),
        sa.Column(
            "credentials_ciphertext",
            sa.Text(),
            nullable=True,
            comment="Fernet-encrypted credential JSON (never logged, never returned)",
        ),
        sa.Column(
            "credentials_key_id",
            sa.String(64),
            nullable=True,
            comment="Encryption key id (for future rotation)",
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
            "provider_key",
            name="uq_sms_providers_installation_key",
        ),
    )
    op.create_index(
        "uq_sms_providers_installation_default",
        "sms_providers",
        ["installation_id"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )
    op.create_index(
        "ix_sms_providers_installation_key",
        "sms_providers",
        ["installation_id", "provider_key"],
    )

    op.create_table(
        "sms_reminder_settings",
        sa.Column(
            "installation_id",
            sa.String(36),
            nullable=False,
            comment="Installation id (one policy row per installation)",
        ),
        sa.Column(
            "tenant_id",
            sa.String(36),
            nullable=False,
            comment="Tenant id (lets the daily job scan plans without a key)",
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="Master opt-in for automatic reminder sends (default: off)",
        ),
        sa.Column(
            "offsets_days_before",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
            comment="Days before the due date that fire (e.g. [3, 0])",
        ),
        sa.Column(
            "offsets_days_overdue",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
            comment="Days after the due date that fire (e.g. [1])",
        ),
        sa.Column(
            "template_before_due",
            sa.Text(),
            nullable=False,
            comment="User-editable Persian template for a not-yet-overdue leg",
        ),
        sa.Column(
            "template_overdue",
            sa.Text(),
            nullable=False,
            comment="User-editable Persian template for an overdue leg",
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
        sa.PrimaryKeyConstraint("installation_id"),
    )
    op.create_index(
        "ix_sms_reminder_settings_enabled",
        "sms_reminder_settings",
        ["installation_id", "enabled"],
    )

    op.create_table(
        "sms_contract_reminder_settings",
        sa.Column("id", sa.String(36), nullable=False, comment="UUID primary key"),
        sa.Column(
            "installation_id",
            sa.String(36),
            nullable=False,
            comment="Installation id (from the authenticated API key)",
        ),
        sa.Column(
            "contract_id",
            sa.String(36),
            sa.ForeignKey("installment_sale_contracts.id"),
            nullable=False,
            comment="Sale contract this opt-in belongs to",
        ),
        sa.Column(
            "reminders_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="Whether automatic reminders are opted in for this contract",
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
            "contract_id",
            name="uq_sms_contract_settings_installation_contract",
        ),
    )
    op.create_index(
        "ix_sms_contract_settings_installation",
        "sms_contract_reminder_settings",
        ["installation_id", "contract_id"],
    )

    op.create_table(
        "sms_reminder_log",
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
            "plan_id",
            sa.String(36),
            sa.ForeignKey("installment_plans.id"),
            nullable=False,
            comment="Plan this reminder belongs to",
        ),
        sa.Column(
            "leg_id",
            sa.String(36),
            sa.ForeignKey("installment_legs.id"),
            nullable=False,
            comment="Leg this reminder is about",
        ),
        sa.Column(
            "provider_id",
            sa.String(36),
            sa.ForeignKey("sms_providers.id", ondelete="SET NULL"),
            nullable=True,
            comment="Provider row (NULL when the provider was deleted; provider_key survives)",
        ),
        sa.Column(
            "provider_key",
            sa.String(64),
            nullable=False,
            comment="Denormalized provider key (survives a provider delete)",
        ),
        sa.Column(
            "recipient_phone",
            sa.String(32),
            nullable=False,
            server_default="",
            comment="Recipient mobile in canonical 09... form (empty when unknown, e.g. a skipped row)",
        ),
        sa.Column(
            "message_text",
            sa.Text(),
            nullable=False,
            comment="Rendered message text (Persian, Jalali due date, Persian digits)",
        ),
        sa.Column(
            "offset_days",
            sa.Integer(),
            nullable=False,
            comment="Days from the due date: negative = before, 0 = on the day, positive = overdue",
        ),
        sa.Column(
            "trigger",
            sa.String(16),
            nullable=False,
            comment="What caused the send: 'scheduled' (daily job) | 'manual' (operator trigger)",
        ),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            comment="Outcome: 'queued' | 'sent' | 'delivered' | 'failed' | 'skipped' | 'dry_run'",
        ),
        sa.Column(
            "provider_message_id",
            sa.String(128),
            nullable=True,
            comment="Provider-side message id (fake 'sim-...' id for the simulator)",
        ),
        sa.Column(
            "provider_status_raw",
            sa.String(256),
            nullable=True,
            comment="Raw provider status (code + text, or pack id for sms.ir)",
        ),
        sa.Column(
            "cost_toman",
            sa.String(32),
            nullable=True,
            comment="Informational cost in Toman, exact decimal text (only when the provider's unit is known)",
        ),
        sa.Column(
            "provider_cost_raw",
            sa.String(64),
            nullable=True,
            comment="Provider-reported cost, verbatim",
        ),
        sa.Column(
            "provider_cost_unit",
            sa.String(16),
            nullable=True,
            comment="Unit of provider_cost_raw: 'IRR' (Kavenegar) | 'unverified' (sms.ir) | NULL",
        ),
        sa.Column(
            "error_code",
            sa.String(64),
            nullable=True,
            comment="Stable error code for failed/skipped rows (NULL otherwise)",
        ),
        sa.Column(
            "error_detail",
            sa.Text(),
            nullable=True,
            comment="Human-readable failure detail (never credential material)",
        ),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the provider accepted the send (NULL for failed/skipped/dry-run rows)",
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
        "uq_sms_reminder_log_installation_leg_offset_sent",
        "sms_reminder_log",
        ["installation_id", "leg_id", "offset_days"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'sent', 'delivered')"),
    )
    op.create_index(
        "ix_sms_reminder_log_installation_created",
        "sms_reminder_log",
        ["installation_id", "created_at"],
    )
    op.create_index(
        "ix_sms_reminder_log_plan_created",
        "sms_reminder_log",
        ["plan_id", "created_at"],
    )
    op.create_index(
        "ix_sms_reminder_log_installation_status",
        "sms_reminder_log",
        ["installation_id", "status"],
    )

    # Tenth review-queue type: a failed reminder files an item.
    op.drop_constraint("ck_review_items_item_type", "review_items", type_="check")
    op.create_check_constraint(
        "ck_review_items_item_type",
        "review_items",
        f"item_type IN ({_ITEM_TYPES_TEN})",
    )
    op.drop_constraint(
        "ck_review_sla_policies_item_type", "review_sla_policies", type_="check"
    )
    op.create_check_constraint(
        "ck_review_sla_policies_item_type",
        "review_sla_policies",
        f"item_type IN ({_ITEM_TYPES_TEN})",
    )


def downgrade() -> None:
    op.drop_constraint("ck_review_items_item_type", "review_items", type_="check")
    op.create_check_constraint(
        "ck_review_items_item_type",
        "review_items",
        f"item_type IN ({_ITEM_TYPES_NINE})",
    )
    op.drop_constraint(
        "ck_review_sla_policies_item_type", "review_sla_policies", type_="check"
    )
    op.create_check_constraint(
        "ck_review_sla_policies_item_type",
        "review_sla_policies",
        f"item_type IN ({_ITEM_TYPES_NINE})",
    )
    op.drop_table("sms_reminder_log")
    op.drop_table("sms_contract_reminder_settings")
    op.drop_table("sms_reminder_settings")
    op.drop_table("sms_providers")
