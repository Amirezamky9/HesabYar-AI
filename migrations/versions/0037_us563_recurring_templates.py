"""Recurring templates + occurrences (US-563, Phase 5-7 ۵خ W3).

Revision ID: 0037
Revises: 0035
Create Date: 2026-11-21

A recurring template is a standing instruction — "for this
company, on this Jalali schedule, create this invoice / journal" —
and each firing creates one occurrence row linking the generated
engine document to its template + occurrence number.

- ``recurring_templates`` — one row per template: tenant +
  installation + company (the HesabYar ``engine_companies`` row id
  AND the engine company id the generator drafts into), kind
  (invoice/journal), name, status (active/paused/completed), the
  Jalali schedule spec + the document payload (both JSONB), the
  auto-post opt-in (default False — draft by default, ADR 0003
  decision 1), and the payload total in Rial.
- ``recurring_occurrences`` — one row per scheduled occurrence:
  its number in the sequence, its scheduled (Gregorian) date + the
  Jalali label, the clamp report (day 31 in a short month fires on
  the last day, honestly), its status (pending/draft/posted/
  failed), the payload snapshot it was generated from (an edit
  never rewrites history), the engine document id, the policy-gate
  verdict when auto-post was attempted, and the failure text.

Idempotency is structural: ``uq_recurring_occurrences_template_date``
(one occurrence per template per scheduled date) is the backstop
that keeps two generator runs from ever creating the same
occurrence twice, and ``uq_recurring_occurrences_template_number``
keeps occurrence numbers unique per template.

Chains after 0035 (US-560 job registry — the Alembic head at
branch time). The other Wave B workers (US-561, US-564) also
branch from 0035; the coordinator re-chains at merge.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0037"
down_revision: str | None = "0038"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "recurring_templates",
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
            comment=(
                "Installation id (from the authenticated API key; "
                "the policy gate evaluates auto-post against its registry policy)"
            ),
        ),
        sa.Column(
            "company_id",
            sa.String(36),
            nullable=False,
            comment="HesabYar engine_companies row id (the API-facing company id)",
        ),
        sa.Column(
            "company_engine_id",
            sa.Integer,
            nullable=False,
            comment="Engine company id the generator drafts into",
        ),
        sa.Column(
            "kind",
            sa.String(16),
            nullable=False,
            comment="invoice | journal",
        ),
        sa.Column(
            "name",
            sa.String(256),
            nullable=False,
            comment="Operator-facing template name",
        ),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            comment="active | paused | completed",
        ),
        sa.Column(
            "schedule",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="Jalali schedule spec (kind + day/month/interval + start/end + max occurrences)",
        ),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="The document the template generates (invoice or journal payload)",
        ),
        sa.Column(
            "auto_post",
            sa.Boolean,
            nullable=False,
            server_default=sa.false(),
            comment="Explicit auto-post opt-in (draft by default; the policy gate still decides)",
        ),
        sa.Column(
            "total_amount_irr",
            sa.BigInteger,
            nullable=False,
            comment="Payload total in Rial (the preview amount)",
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
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "kind IN ('invoice', 'journal')",
            name="ck_recurring_templates_kind",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'completed')",
            name="ck_recurring_templates_status",
        ),
        sa.Index(
            "ix_recurring_templates_tenant_company",
            "tenant_id",
            "company_id",
        ),
        sa.Index(
            "ix_recurring_templates_status",
            "status",
        ),
    )
    op.create_table(
        "recurring_occurrences",
        sa.Column("id", sa.String(36), nullable=False, comment="UUID primary key"),
        sa.Column(
            "template_id",
            sa.String(36),
            sa.ForeignKey("recurring_templates.id", ondelete="CASCADE"),
            nullable=False,
            comment="Owning template FK (CASCADE delete)",
        ),
        sa.Column(
            "occurrence_number",
            sa.Integer,
            nullable=False,
            comment="Position in the schedule's sequence from the start date (1-based)",
        ),
        sa.Column(
            "scheduled_date",
            sa.Date,
            nullable=False,
            comment="Scheduled (Gregorian) date of the occurrence",
        ),
        sa.Column(
            "jalali_date",
            sa.String(10),
            nullable=False,
            comment="The same date as an ISO-like Jalali string (1405-08-15)",
        ),
        sa.Column(
            "clamped",
            sa.Boolean,
            nullable=False,
            server_default=sa.false(),
            comment="True when the Jalali day was clamped to the month's length",
        ),
        sa.Column(
            "requested_jalali_day",
            sa.Integer,
            nullable=False,
            comment="The Jalali day the schedule asked for",
        ),
        sa.Column(
            "actual_jalali_day",
            sa.Integer,
            nullable=False,
            comment="The Jalali day the occurrence actually fires on",
        ),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            comment="pending | draft | posted | failed",
        ),
        sa.Column(
            "total_amount_irr",
            sa.BigInteger,
            nullable=False,
            comment="Occurrence total in Rial (from the snapshot)",
        ),
        sa.Column(
            "snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="Payload + schedule the occurrence was generated from (an edit never rewrites it)",
        ),
        sa.Column(
            "engine_document_id",
            sa.Integer,
            nullable=True,
            comment="Engine id of the generated invoice / journal (NULL until generated)",
        ),
        sa.Column(
            "gate_decision",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="Policy-gate verdict when auto-post was attempted (NULL when never opted in)",
        ),
        sa.Column(
            "error",
            sa.Text(),
            nullable=True,
            comment="Draft-creation failure (status failed) or post failure after the draft landed (status draft)",
        ),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the document was generated (UTC); NULL until generated",
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
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('pending', 'draft', 'posted', 'failed')",
            name="ck_recurring_occurrences_status",
        ),
        sa.UniqueConstraint(
            "template_id",
            "occurrence_number",
            name="uq_recurring_occurrences_template_number",
        ),
        sa.UniqueConstraint(
            "template_id",
            "scheduled_date",
            name="uq_recurring_occurrences_template_date",
        ),
        sa.Index(
            "ix_recurring_occurrences_template_number",
            "template_id",
            "occurrence_number",
        ),
    )


def downgrade() -> None:
    op.drop_table("recurring_occurrences")
    op.drop_table("recurring_templates")
