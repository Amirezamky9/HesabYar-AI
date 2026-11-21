"""Reminder preferences + reminder log (US-564, Phase 5-7 ۵خ W3).

Revision ID: 0038
Revises: 0035
Create Date: 2026-11-21

- ``reminder_preferences`` — one row per installation: the master
  opt-in, per-type enablement, channel flags, lead times (JSONB
  day lists), quiet hours (Tehran wall time), the eNamad expiry
  date, and the operator notification email. Born disabled: no
  row (or a disabled row) means the daily job sends nothing.
- ``reminder_log`` — one row per reminder attempt (sent, failed,
  or skipped), with its channel, cost, and reason. The partial
  unique index on (installation, idempotency_key) over the sent
  statuses (``queued`` / ``sent`` / ``delivered``) is the dedup
  backstop: one reminder per event per lead-time per channel.
  The key is ``{type}:{subject_id}:{event_ref}:{channel}`` (see
  ``hesabyar.domain.reminders.models.build_idempotency_key``).

Also extends the ``review_items`` item-type check constraint with
``reminder`` (US-564's in-app channel files review items of that
type — the review queue is the operator's in-app surface). The
constraint is dropped and recreated; the downgrade restores the
original text. No rows are touched: existing items keep their
types, and no ``reminder`` item can exist before this migration.

Chains after 0035 (US-560 job registry — the Alembic head at
branch time). NOTE: the sibling Wave B workers (US-561, US-563)
also branch from 0035 — the coordinator re-chains at merge.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0038"
down_revision: str | None = "0036"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_OLD_ITEM_TYPES = (
    "'mapping', 'settlement_variance', 'cheque_exception', "
    "'bank_unmatched', 'packaging_reorder', 'installment_overdue', "
    "'manual_import_review', 'cashbox_variance', 'inbox_dead_letter', "
    "'sms_delivery_failed'"
)

_NEW_ITEM_TYPES = _OLD_ITEM_TYPES + ", 'reminder'"


def upgrade() -> None:
    op.create_table(
        "reminder_preferences",
        sa.Column(
            "installation_id",
            sa.String(36),
            nullable=False,
            comment="Installation id (from the authenticated API key)",
        ),
        sa.Column(
            "tenant_id",
            sa.String(36),
            nullable=False,
            comment="Tenant id (from the authenticated API key)",
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="Master opt-in for automatic reminders (default: off)",
        ),
        sa.Column(
            "cheque_due_soon_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
            comment="Cheque due-soon reminders on (master switch still applies)",
        ),
        sa.Column(
            "installment_overdue_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
            comment="Overdue-installment reminders on (master switch still applies)",
        ),
        sa.Column(
            "cashbox_uncounted_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
            comment="Uncounted-cashbox reminders on (master switch still applies)",
        ),
        sa.Column(
            "enmad_expiry_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
            comment="eNamad-expiry reminders on (master switch still applies)",
        ),
        sa.Column(
            "channel_in_app",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
            comment="In-app channel (review-queue item) on",
        ),
        sa.Column(
            "channel_email",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
            comment="Email channel on (no email provider exists yet — attempts are logged as skipped)",
        ),
        sa.Column(
            "channel_sms",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
            comment="SMS channel on (via the US-544 provider registry)",
        ),
        sa.Column(
            "cheque_lead_days",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[7, 3]'::jsonb"),
            comment="Cheque due-soon leads in days before the due date (e.g. [7, 3])",
        ),
        sa.Column(
            "installment_overdue_days",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[1, 7]'::jsonb"),
            comment="Installment-overdue offsets in days past the due date (e.g. [1, 7])",
        ),
        sa.Column(
            "cashbox_uncounted_days",
            sa.Integer(),
            nullable=False,
            server_default="7",
            comment="Uncounted-cashbox threshold: remind when a box went this many days without a count",
        ),
        sa.Column(
            "enmad_lead_days",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[30, 7, 0]'::jsonb"),
            comment="eNamad-expiry leads in days before expiry (0 = the expiry day)",
        ),
        sa.Column(
            "quiet_hours_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
            comment="Quiet hours on (no SMS or email inside the window; in-app may still record)",
        ),
        sa.Column(
            "quiet_hours_start",
            sa.Time(),
            nullable=False,
            server_default=sa.text("'22:00'::time"),
            comment="Quiet-hours start (Tehran wall time)",
        ),
        sa.Column(
            "quiet_hours_end",
            sa.Time(),
            nullable=False,
            server_default=sa.text("'08:00'::time"),
            comment="Quiet-hours end (Tehran wall time)",
        ),
        sa.Column(
            "enmad_expiry_date",
            sa.Date(),
            nullable=True,
            comment="The store's eNamad expiry date (NULL = no eNamad reminders); "
            "lives here until a store-profile model exists",
        ),
        sa.Column(
            "notification_email",
            sa.String(254),
            nullable=True,
            comment="Operator address for email reminders (NULL = no email recipient configured)",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="Row creation timestamp (UTC)",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="Last-modified timestamp (UTC)",
        ),
        sa.PrimaryKeyConstraint("installation_id"),
    )
    op.create_index(
        "ix_reminder_preferences_enabled",
        "reminder_preferences",
        ["installation_id", "enabled"],
    )
    op.create_table(
        "reminder_log",
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
            "reminder_type",
            sa.String(32),
            nullable=False,
            comment="cheque_due_soon | installment_overdue | cashbox_uncounted | enmad_expiry",
        ),
        sa.Column(
            "subject_type",
            sa.String(32),
            nullable=False,
            comment="What the reminder is about: cheque | installment_leg | cashbox | installation",
        ),
        sa.Column(
            "subject_id",
            sa.String(36),
            nullable=False,
            comment="Id of the subject (cheque / leg / cash box / installation)",
        ),
        sa.Column(
            "lead_days",
            sa.Integer(),
            nullable=False,
            comment="Lead/offset that fired (days before due, days overdue, or the cash-box N threshold)",
        ),
        sa.Column(
            "channel",
            sa.String(16),
            nullable=False,
            comment="in_app | email | sms",
        ),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            comment="queued | sent | delivered | failed | skipped",
        ),
        sa.Column(
            "idempotency_key",
            sa.String(255),
            nullable=False,
            comment="Dedup key: {type}:{subject_id}:{event_ref}:{channel}",
        ),
        sa.Column(
            "message_text",
            sa.Text(),
            nullable=False,
            comment="The message as sent (Persian)",
        ),
        sa.Column(
            "recipient",
            sa.String(254),
            nullable=True,
            comment="Where it went: mobile for SMS, email for email, NULL for in-app",
        ),
        sa.Column(
            "provider_id",
            sa.String(36),
            sa.ForeignKey("sms_providers.id", ondelete="SET NULL"),
            nullable=True,
            comment="SMS provider row (NULL when the provider was deleted; provider_key survives)",
        ),
        sa.Column(
            "provider_key",
            sa.String(64),
            nullable=True,
            comment="Denormalized SMS provider key (survives a provider delete)",
        ),
        sa.Column(
            "provider_message_id",
            sa.String(128),
            nullable=True,
            comment="Provider's id for the sent message",
        ),
        sa.Column(
            "provider_status_raw",
            sa.Text(),
            nullable=True,
            comment="Provider's verbatim delivery status",
        ),
        sa.Column(
            "cost_toman",
            sa.String(32),
            nullable=True,
            comment="Informational SMS cost in Toman — only when the provider reports "
            "a cost in a KNOWN unit (the US-544 rule)",
        ),
        sa.Column(
            "provider_cost_raw",
            sa.String(64),
            nullable=True,
            comment="Provider's verbatim cost figure",
        ),
        sa.Column(
            "provider_cost_unit",
            sa.String(32),
            nullable=True,
            comment="Unit of the verbatim cost ('IRR' for Kavenegar, 'unverified' for sms.ir)",
        ),
        sa.Column(
            "error_code",
            sa.String(64),
            nullable=True,
            comment="Machine-readable reason for a failed/skipped attempt",
        ),
        sa.Column(
            "error_detail",
            sa.Text(),
            nullable=True,
            comment="Human-readable detail for a failed/skipped attempt",
        ),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the reminder went out (NULL unless sent)",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="Row creation timestamp (UTC)",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "reminder_type IN ('cheque_due_soon', 'installment_overdue', "
            "'cashbox_uncounted', 'enmad_expiry')",
            name="ck_reminder_log_reminder_type",
        ),
        sa.CheckConstraint(
            "channel IN ('in_app', 'email', 'sms')",
            name="ck_reminder_log_channel",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'sent', 'delivered', 'failed', 'skipped')",
            name="ck_reminder_log_status",
        ),
    )
    op.create_index(
        "uq_reminder_log_installation_key_sent",
        "reminder_log",
        ["installation_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'sent', 'delivered')"),
    )
    op.create_index(
        "ix_reminder_log_installation_created",
        "reminder_log",
        ["installation_id", "created_at"],
    )
    op.create_index(
        "ix_reminder_log_installation_type_status",
        "reminder_log",
        ["installation_id", "reminder_type", "status"],
    )
    op.drop_constraint("ck_review_items_item_type", "review_items", type_="check")
    op.create_check_constraint(
        "ck_review_items_item_type",
        "review_items",
        f"item_type IN ({_NEW_ITEM_TYPES})",
    )


def downgrade() -> None:
    op.drop_constraint("ck_review_items_item_type", "review_items", type_="check")
    op.create_check_constraint(
        "ck_review_items_item_type",
        "review_items",
        f"item_type IN ({_OLD_ITEM_TYPES})",
    )
    op.drop_table("reminder_log")
    op.drop_table("reminder_preferences")
