"""Manual entries: dual-entry traceability anchor (US-513).

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-18

Phase 5 wave 2 stream S3 (phased design §1.3 / §5): manual
order/invoice/refund entry produces the same canonical envelope as a
store webhook and travels the same §10.3 pipeline. One table:

- ``manual_entries`` — one row per manual entry: the operator's entry
  (who, when, what) linked to every canonical event it produced.
  ``events`` is a JSONB list of produced events (event type + inbox
  event id + source event id + external-entity identity); ``payload``
  snapshots the primary event's payload (whole Toman, as entered) so
  the entry stays readable even if the inbox row is purged. The
  correlation id of every produced event is the entry id, so the
  chain manual entry → inbox event → pipeline decision → engine
  record is traceable in both directions.

Chains after 0011 (US-504 review queue).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "manual_entries",
        sa.Column(
            "id",
            sa.String(36),
            primary_key=True,
            comment="UUID primary key (manual entry id; also the correlation id of its events)",
        ),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            comment="Owning tenant FK (CASCADE delete)",
        ),
        sa.Column("entry_type", sa.String(16), nullable=False, comment="order | refund | cancellation"),
        sa.Column(
            "source_store_id",
            sa.String(128),
            nullable=False,
            comment="Store the entry is attributed to ('manual' when the operator named none)",
        ),
        sa.Column(
            "order_number",
            sa.String(128),
            nullable=False,
            comment="Operator-supplied order reference (the external entity id of the order event)",
        ),
        sa.Column(
            "total_amount_toman",
            sa.BigInteger(),
            nullable=False,
            comment="Entry total in whole Toman (order total / refund amount / cancelled total, 0 when unknown)",
        ),
        sa.Column(
            "entered_by",
            sa.String(128),
            nullable=False,
            comment="API key id that created the entry (the actor, for the audit trail)",
        ),
        sa.Column(
            "entered_by_name",
            sa.String(256),
            nullable=True,
            comment="API client name at creation time (display only)",
        ),
        sa.Column(
            "events",
            JSONB,
            nullable=False,
            comment=(
                "Produced events: [{event_type, inbox_event_id, source_event_id, "
                "external_entity_type, external_entity_id}]"
            ),
        ),
        sa.Column(
            "payload",
            JSONB,
            nullable=False,
            comment="Primary event payload snapshot (whole Toman, as entered)",
        ),
        sa.Column("note", sa.Text(), nullable=True, comment="Operator note (optional)"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="Entry creation timestamp (UTC)",
        ),
        sa.CheckConstraint(
            "entry_type IN ('order', 'refund', 'cancellation')",
            name="ck_manual_entries_entry_type",
        ),
        sa.CheckConstraint(
            "total_amount_toman >= 0",
            name="ck_manual_entries_total_non_negative",
        ),
    )
    op.create_index("ix_manual_entries_tenant_id", "manual_entries", ["tenant_id"])
    op.create_index(
        "ix_manual_entries_tenant_type_created",
        "manual_entries",
        ["tenant_id", "entry_type", "created_at"],
    )
    op.create_index("ix_manual_entries_tenant_order", "manual_entries", ["tenant_id", "order_number"])


def downgrade() -> None:
    op.drop_index("ix_manual_entries_tenant_order", table_name="manual_entries")
    op.drop_index("ix_manual_entries_tenant_type_created", table_name="manual_entries")
    op.drop_index("ix_manual_entries_tenant_id", table_name="manual_entries")
    op.drop_table("manual_entries")
