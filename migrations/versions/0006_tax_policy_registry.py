"""Tax-policy registry (US-402).

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-17

Design-eng spec §7.4 (Tax policy group) and §12.2: tax rules are
versioned, effective-dated and traceable to an official source
snapshot. Rule year, performance year, filing year and effective date
are separate persisted fields on every version — a 1405 rule may
govern performance year 1404 and be filed in 1405
(docs/CURRENT_RULES_1405.md §3: the Article 100 ceiling), so no value
is ever labelled merely "1405".

Annual parameters live here, never as timeless constants in code:
``parameter_value`` is an exact Numeric with a ``value_unit``
(``percent`` — the figure as written, 10 means 10% — or ``irr``).

Verification statuses follow docs/CURRENT_RULES_1405.md §1, plus
UNVERIFIED (same vocabulary as the standards registry, revision
0005). Only a VERIFIED_TEXT version may be used for production; the
decision service enforces that gate (the schema alone cannot — a
version's status is data, not structure).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

# docs/CURRENT_RULES_1405.md §1 labels + UNVERIFIED (see module docstring).
VERIFICATION_STATUSES = (
    "VERIFIED_TEXT",
    "CORROBORATED_CURRENT",
    "DISCOVERY_ONLY",
    "BLOCKED_SOURCE_GAP",
    "TRANSITIONAL",
    "UNVERIFIED",
)

VALUE_UNITS = ("percent", "irr")
USAGE_CONTEXTS = ("reference", "production")


def upgrade() -> None:
    op.create_table(
        "tax_sources",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key"),
        sa.Column(
            "source_key",
            sa.String(128),
            nullable=False,
            comment="Stable source slug, e.g. 'tasnim-budget-1404-11-25'",
        ),
        sa.Column(
            "title",
            sa.String(512),
            nullable=False,
            comment="Title of the source document",
        ),
        sa.Column(
            "authority",
            sa.String(256),
            nullable=True,
            comment="Issuing/reporting authority, e.g. 'Tasnim News Agency'",
        ),
        sa.Column(
            "source_url",
            sa.String(1024),
            nullable=True,
            comment="Canonical source document URL",
        ),
        sa.Column(
            "snapshot_uri",
            sa.String(1024),
            nullable=True,
            comment="Repo-relative path of the snapshotted text (NULL until an official snapshot is pinned)",
        ),
        sa.Column(
            "sha256",
            sa.String(64),
            nullable=True,
            comment="SHA-256 hex of the snapshotted source bytes (NULL until pinned)",
        ),
        sa.Column(
            "notes",
            sa.Text,
            nullable=True,
            comment="Notes, e.g. that the source is corroboration only, not the official text",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Row creation timestamp (UTC)",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Last-modified timestamp (UTC)",
        ),
        sa.UniqueConstraint("source_key", name="uq_tax_sources_key"),
    )

    op.create_table(
        "tax_policy_versions",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key"),
        sa.Column(
            "source_id",
            sa.String(36),
            sa.ForeignKey("tax_sources.id", ondelete="CASCADE"),
            nullable=False,
            comment="Primary corroborating/official source FK (CASCADE delete)",
        ),
        sa.Column(
            "rule_key",
            sa.String(128),
            nullable=False,
            comment="Stable rule identifier, e.g. 'vat_general_rate'",
        ),
        sa.Column(
            "rule_name",
            sa.String(512),
            nullable=False,
            comment="Persian name of the rule, e.g. 'نرخ عمومی مالیات بر ارزش افزوده'",
        ),
        sa.Column(
            "version_id",
            sa.String(64),
            nullable=False,
            comment="Version identifier of the rule, e.g. '1405'",
        ),
        sa.Column(
            "parameter_value",
            sa.Numeric(20, 4),
            nullable=False,
            comment="Annual parameter value, exact (percent figures as written: 10 means 10%)",
        ),
        sa.Column(
            "value_unit",
            sa.String(16),
            nullable=False,
            comment="Unit of parameter_value: 'percent' or 'irr'",
        ),
        sa.Column(
            "rule_year",
            sa.Integer,
            nullable=False,
            comment="Jalali year of the rule (the year the parameter belongs to)",
        ),
        sa.Column(
            "performance_year",
            sa.Integer,
            nullable=True,
            comment="Jalali performance year the rule governs (NULL when not recorded separately)",
        ),
        sa.Column(
            "filing_year",
            sa.Integer,
            nullable=True,
            comment="Jalali year the rule is filed/administered in",
        ),
        sa.Column(
            "effective_from",
            sa.Date,
            nullable=False,
            comment="First Gregorian date the version applies to (inclusive)",
        ),
        sa.Column(
            "effective_to",
            sa.Date,
            nullable=True,
            comment="Last Gregorian date the version applies to (inclusive); NULL means open-ended",
        ),
        sa.Column(
            "status",
            sa.String(32),
            server_default=sa.text("'active'"),
            nullable=False,
            comment="Registry lifecycle state: 'active' or 'superseded'",
        ),
        sa.Column(
            "verification_status",
            sa.String(32),
            server_default=sa.text("'UNVERIFIED'"),
            nullable=False,
            comment="Verification label (CURRENT_RULES_1405.md §1 + UNVERIFIED)",
        ),
        sa.Column(
            "verified_by",
            sa.String(256),
            nullable=True,
            comment="Who/what verified this version, e.g. 'CURRENT_RULES_1405.md (HYA-RULES-1405-01)'",
        ),
        sa.Column(
            "verified_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the verification was recorded (UTC)",
        ),
        sa.Column(
            "source_authority",
            sa.String(256),
            nullable=True,
            comment="Issuing authority for this version",
        ),
        sa.Column(
            "source_url",
            sa.String(1024),
            nullable=True,
            comment="Source document URL for this version",
        ),
        sa.Column(
            "snapshot_uri",
            sa.String(1024),
            nullable=True,
            comment="Repo-relative path of the snapshotted text",
        ),
        sa.Column(
            "sha256",
            sa.String(64),
            nullable=True,
            comment="SHA-256 hex of the snapshotted source text bytes",
        ),
        sa.Column(
            "corroboration_urls",
            postgresql.JSONB,
            nullable=True,
            comment="Corroborating source URLs for this version (source metadata, not queryable business data)",
        ),
        sa.Column(
            "notes",
            sa.Text,
            nullable=True,
            comment="Scope notes, e.g. the Article 100 performance-1404 distinction",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Row creation timestamp (UTC)",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Last-modified timestamp (UTC)",
        ),
        sa.UniqueConstraint(
            "rule_key",
            "version_id",
            name="uq_tax_policy_versions_rule_version",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'superseded')",
            name="ck_tax_policy_versions_status",
        ),
        sa.CheckConstraint(
            "verification_status IN (" + ", ".join(f"'{status}'" for status in VERIFICATION_STATUSES) + ")",
            name="ck_tax_policy_versions_verification_status",
        ),
        sa.CheckConstraint(
            "value_unit IN (" + ", ".join(f"'{unit}'" for unit in VALUE_UNITS) + ")",
            name="ck_tax_policy_versions_value_unit",
        ),
        sa.CheckConstraint(
            "(effective_to IS NULL OR effective_to >= effective_from)",
            name="ck_tax_policy_versions_effective_range",
        ),
    )
    op.create_index("ix_tax_policy_versions_source_id", "tax_policy_versions", ["source_id"])
    op.create_index("ix_tax_policy_versions_rule_key", "tax_policy_versions", ["rule_key"])
    op.create_index(
        "ix_tax_policy_versions_verification_status",
        "tax_policy_versions",
        ["verification_status"],
    )
    op.create_index(
        "ix_tax_policy_versions_effective_from",
        "tax_policy_versions",
        ["effective_from"],
    )

    op.create_table(
        "tax_decisions",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key"),
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
            "rule_key",
            sa.String(128),
            nullable=False,
            comment="Stable rule identifier that was resolved, e.g. 'vat_general_rate'",
        ),
        sa.Column(
            "decision_date",
            sa.Date,
            nullable=False,
            comment="Date the policy was resolved for (effective dates are inclusive)",
        ),
        sa.Column(
            "usage_context",
            sa.String(16),
            nullable=False,
            comment="Why the decision was resolved: 'reference' or 'production'",
        ),
        sa.Column(
            "policy_version_id",
            sa.String(36),
            sa.ForeignKey("tax_policy_versions.id", ondelete="SET NULL"),
            nullable=True,
            comment="Resolving version row FK (SET NULL); the denormalized fields below are the stable history",
        ),
        sa.Column(
            "version_id",
            sa.String(64),
            nullable=False,
            comment="Version id of the resolving version, denormalized for history",
        ),
        sa.Column(
            "parameter_value",
            sa.Numeric(20, 4),
            nullable=False,
            comment="Parameter value of the resolving version, denormalized for history",
        ),
        sa.Column(
            "value_unit",
            sa.String(16),
            nullable=False,
            comment="Unit of parameter_value, denormalized: 'percent' or 'irr'",
        ),
        sa.Column(
            "verification_status",
            sa.String(32),
            nullable=False,
            comment="Verification label of the resolving version at decision time",
        ),
        sa.Column(
            "source_key",
            sa.String(128),
            nullable=False,
            comment="Source key of the resolving version, denormalized for history",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Row creation timestamp (UTC)",
        ),
        sa.CheckConstraint(
            "usage_context IN (" + ", ".join(f"'{context}'" for context in USAGE_CONTEXTS) + ")",
            name="ck_tax_decisions_usage_context",
        ),
        sa.CheckConstraint(
            "value_unit IN (" + ", ".join(f"'{unit}'" for unit in VALUE_UNITS) + ")",
            name="ck_tax_decisions_value_unit",
        ),
        sa.CheckConstraint(
            "verification_status IN (" + ", ".join(f"'{status}'" for status in VERIFICATION_STATUSES) + ")",
            name="ck_tax_decisions_verification_status",
        ),
    )
    op.create_index("ix_tax_decisions_tenant_id", "tax_decisions", ["tenant_id"])
    op.create_index(
        "ix_tax_decisions_tenant_created",
        "tax_decisions",
        ["tenant_id", "created_at"],
    )
    op.create_index("ix_tax_decisions_rule_key", "tax_decisions", ["rule_key"])


def downgrade() -> None:
    op.drop_table("tax_decisions")
    op.drop_table("tax_policy_versions")
    op.drop_table("tax_sources")
