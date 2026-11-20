"""Review queue: uniform items, audit trail, SLA policies (US-504).

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-18

Phase 5 foundation stream F4 (phased design §1.2 / §7.2): one uniform
queue for every exception type. Three tables:

- ``review_items`` — one row per exception, all nine types, with
  type/severity/status check constraints and a resolution/status
  consistency constraint (a resolution exists exactly when the item
  is resolved or written off). Age and SLA deadline are computed at
  read time from ``created_at`` + the type's SLA policy, never stored.
- ``review_item_audit`` — append-only audit trail: every action on
  every item (who, when, which action, which decision, with which
  policy/mapping version). When F1's decision audit log lands, that
  log becomes the audit source; the seam is the ``ReviewAuditPort``.
- ``review_sla_policies`` — one configurable SLA policy per (tenant,
  item type); a missing row means the skeleton default applies.

Chains after 0010 (US-503 registries).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_ITEM_TYPES = (
    "'mapping', 'settlement_variance', 'cheque_exception', "
    "'bank_unmatched', 'packaging_reorder', 'installment_overdue', "
    "'manual_import_review', 'cashbox_variance', 'inbox_dead_letter'"
)


def upgrade() -> None:
    op.create_table(
        "review_items",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key (review item id)"),
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
            nullable=True,
            comment="API company id as supplied (row id or engine id); plain external reference, no FK",
        ),
        sa.Column(
            "item_type",
            sa.String(32),
            nullable=False,
            comment="Exception type — one of the nine §7.2 types",
        ),
        sa.Column(
            "subtype",
            sa.String(64),
            nullable=True,
            comment="Subtype for filtering/stats (e.g. cheque bounce vs overdue)",
        ),
        sa.Column(
            "severity",
            sa.String(16),
            nullable=False,
            comment="info | warning | critical",
        ),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="open",
            comment="open | in_review | resolved | written_off",
        ),
        sa.Column(
            "channel",
            sa.String(64),
            nullable=True,
            comment="Payment channel / gateway key the exception came from (e.g. 'zarinpal')",
        ),
        sa.Column(
            "source_event_ref",
            sa.String(128),
            nullable=True,
            comment="Reference to the originating event (inbox event id / source event id)",
        ),
        sa.Column(
            "reason",
            sa.Text(),
            nullable=False,
            comment="Why the item needs review (Persian, human-readable)",
        ),
        sa.Column(
            "suggested_action",
            sa.Text(),
            nullable=False,
            comment="System-suggested next step (Persian, human-readable)",
        ),
        sa.Column(
            "amount_irr",
            sa.BigInteger(),
            nullable=True,
            comment="Amount at stake in IRR (nullable — not every exception carries money)",
        ),
        sa.Column(
            "assignee",
            sa.String(128),
            nullable=True,
            comment="Actor id/name assigned to the item (default assignee: accountant)",
        ),
        sa.Column(
            "resolution",
            sa.String(16),
            nullable=True,
            comment="How the item was closed (set only on resolved/written_off)",
        ),
        sa.Column(
            "resolved_by",
            sa.String(128),
            nullable=True,
            comment="Actor who closed the item",
        ),
        sa.Column(
            "resolved_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the item was closed (UTC)",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Row creation timestamp (UTC) — the age/SLA anchor",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Last update timestamp (UTC)",
        ),
        sa.CheckConstraint(
            f"item_type IN ({_ITEM_TYPES})",
            name="ck_review_items_item_type",
        ),
        sa.CheckConstraint(
            "severity IN ('info', 'warning', 'critical')",
            name="ck_review_items_severity",
        ),
        sa.CheckConstraint(
            "status IN ('open', 'in_review', 'resolved', 'written_off')",
            name="ck_review_items_status",
        ),
        sa.CheckConstraint(
            "resolution IS NULL OR resolution IN ('confirmed_ok', 'adjusted', 'write_off', "
            "'escalated', 'rescheduled', 'skipped', 'deferred')",
            name="ck_review_items_resolution",
        ),
        sa.CheckConstraint(
            "(amount_irr IS NULL OR amount_irr >= 0)",
            name="ck_review_items_amount_non_negative",
        ),
        sa.CheckConstraint(
            "(status IN ('resolved', 'written_off') AND resolution IS NOT NULL) "
            "OR (status IN ('open', 'in_review') AND resolution IS NULL)",
            name="ck_review_items_resolution_matches_status",
        ),
    )
    op.create_index("ix_review_items_tenant_id", "review_items", ["tenant_id"])
    op.create_index(
        "ix_review_items_tenant_type_status",
        "review_items",
        ["tenant_id", "item_type", "status"],
    )
    op.create_index(
        "ix_review_items_tenant_severity_created",
        "review_items",
        ["tenant_id", "severity", "created_at"],
    )
    op.create_index("ix_review_items_tenant_channel", "review_items", ["tenant_id", "channel"])
    op.create_index("ix_review_items_tenant_company", "review_items", ["tenant_id", "company_id"])

    op.create_table(
        "review_item_audit",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key (audit entry id)"),
        sa.Column(
            "item_id",
            sa.String(36),
            sa.ForeignKey("review_items.id", ondelete="CASCADE"),
            nullable=False,
            comment="Review item FK (CASCADE delete)",
        ),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            comment="Owning tenant FK (CASCADE delete) — denormalized for tenant-scoped reads",
        ),
        sa.Column(
            "actor",
            sa.String(128),
            nullable=False,
            comment="Actor who performed the action (API client id / user id)",
        ),
        sa.Column(
            "action",
            sa.String(64),
            nullable=False,
            comment="Action code performed (e.g. 'replay', 'filed', 'assign')",
        ),
        sa.Column(
            "decision",
            sa.String(64),
            nullable=True,
            comment="Decision recorded by the action (resolution or resulting status)",
        ),
        sa.Column(
            "note",
            sa.Text(),
            nullable=True,
            comment="Human note attached to the action",
        ),
        sa.Column(
            "policy_version",
            sa.String(64),
            nullable=True,
            comment="Policy version the decision used (None until F1's audit log supplies it)",
        ),
        sa.Column(
            "mapping_version",
            sa.String(64),
            nullable=True,
            comment="Mapping version the decision used (None until F1's audit log supplies it)",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Row creation timestamp (UTC)",
        ),
    )
    op.create_index(
        "ix_review_item_audit_item_created",
        "review_item_audit",
        ["item_id", "created_at"],
    )

    op.create_table(
        "review_sla_policies",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key (SLA policy id)"),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            comment="Owning tenant FK (CASCADE delete)",
        ),
        sa.Column(
            "item_type",
            sa.String(32),
            nullable=False,
            comment="Exception type — one of the nine §7.2 types",
        ),
        sa.Column(
            "sla_hours",
            sa.Integer(),
            nullable=False,
            comment="SLA deadline in whole hours after filing",
        ),
        sa.Column(
            "warn_hours",
            sa.Integer(),
            nullable=False,
            comment="Warn window in whole hours before the deadline",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Last update timestamp (UTC)",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "item_type",
            name="uq_review_sla_policies_tenant_type",
        ),
        sa.CheckConstraint(
            f"item_type IN ({_ITEM_TYPES})",
            name="ck_review_sla_policies_item_type",
        ),
        sa.CheckConstraint(
            "sla_hours > 0 AND warn_hours > 0 AND warn_hours < sla_hours",
            name="ck_review_sla_policies_hours",
        ),
    )
    op.create_index("ix_review_sla_policies_tenant_id", "review_sla_policies", ["tenant_id"])


def downgrade() -> None:
    op.drop_table("review_sla_policies")
    op.drop_table("review_item_audit")
    op.drop_table("review_items")
