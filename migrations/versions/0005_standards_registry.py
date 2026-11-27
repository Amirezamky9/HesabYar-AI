"""Standards registry + validation runs (US-401).

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-17

Design-eng spec §7.4 (Standards + Validation groups) and §12.1
(standards engine): every standard revision carries full provenance
(standard_code, revision_id, title, published_at, effective_from/to,
source_authority, source_url, snapshot_uri, sha256, verification_status,
verified_by/at); validation runs are deterministic records with a
ruleset version and per-rule results in exactly the closed outcome set
PASS / FAIL / NOT_APPLICABLE / ERROR.

Verification statuses follow docs/CURRENT_RULES_1405.md §1, plus
UNVERIFIED (bundled text present, official source snapshot not yet
pinned/verified — the honest default; see the seed module).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | None = "0004"
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


def upgrade() -> None:
    op.create_table(
        "standard_sources",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key"),
        sa.Column(
            "standard_code",
            sa.String(16),
            nullable=False,
            comment="Plain standard number as a string, e.g. '1', '43'",
        ),
        sa.Column(
            "title",
            sa.String(512),
            nullable=False,
            comment="Persian title from metadata.json",
        ),
        sa.Column(
            "title_en",
            sa.String(512),
            nullable=True,
            comment="English title from metadata.json",
        ),
        sa.Column(
            "ifrs_equivalent",
            sa.String(64),
            nullable=True,
            comment="IFRS/IAS equivalent, e.g. 'IAS 1', 'IFRS 15'",
        ),
        sa.Column(
            "phase",
            sa.Integer,
            nullable=True,
            comment="Corpus phase (1-8) from metadata.json",
        ),
        sa.Column(
            "slug",
            sa.String(128),
            nullable=False,
            comment="URL slug from metadata.json, e.g. '01-presentation'",
        ),
        sa.Column(
            "source_authority",
            sa.String(256),
            nullable=True,
            comment="Issuing authority, e.g. 'Auditing Organization of Iran'",
        ),
        sa.Column(
            "source_url",
            sa.String(1024),
            nullable=True,
            comment="Canonical source document URL (per-standard PDF)",
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
        sa.UniqueConstraint("standard_code", name="uq_standard_sources_code"),
        sa.UniqueConstraint("slug", name="uq_standard_sources_slug"),
    )

    op.create_table(
        "standard_revisions",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key"),
        sa.Column(
            "source_id",
            sa.String(36),
            sa.ForeignKey("standard_sources.id", ondelete="CASCADE"),
            nullable=False,
            comment="Owning standard source FK (CASCADE delete)",
        ),
        sa.Column(
            "revision_id",
            sa.String(64),
            nullable=False,
            comment="Revision identifier of the bundled text, e.g. '1397', '1404'",
        ),
        sa.Column(
            "title",
            sa.String(512),
            nullable=False,
            comment="Persian title of this revision",
        ),
        sa.Column(
            "published_at",
            sa.Date,
            nullable=True,
            comment="Publication date of the revision, when known",
        ),
        sa.Column(
            "effective_from",
            sa.Date,
            nullable=True,
            comment="First period-begin date the revision applies to",
        ),
        sa.Column(
            "effective_to",
            sa.Date,
            nullable=True,
            comment="Last period-begin date the revision applies to",
        ),
        sa.Column(
            "status",
            sa.String(32),
            server_default=sa.text("'active'"),
            nullable=False,
            comment="Registry lifecycle state: 'active' or 'superseded'",
        ),
        sa.Column(
            "supersedes_revision_id",
            sa.String(64),
            nullable=True,
            comment="Revision id this revision supersedes, when recorded",
        ),
        sa.Column(
            "source_authority",
            sa.String(256),
            nullable=True,
            comment="Issuing authority for this revision",
        ),
        sa.Column(
            "source_url",
            sa.String(1024),
            nullable=True,
            comment="Source document URL for this revision",
        ),
        sa.Column(
            "snapshot_uri",
            sa.String(1024),
            nullable=True,
            comment="Repo-relative path of the snapshotted text, e.g. 'standards/01-presentation/source/standard.md'",
        ),
        sa.Column(
            "sha256",
            sa.String(64),
            nullable=True,
            comment="SHA-256 hex of the snapshotted source text bytes",
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
            comment="Who/what verified this revision, e.g. 'CURRENT_RULES_1405.md (HYA-RULES-1405-01)'",
        ),
        sa.Column(
            "verified_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the verification was recorded (UTC)",
        ),
        sa.Column(
            "notes",
            sa.Text,
            nullable=True,
            comment="Supersession / applicability notes, e.g. Standard 43 superseding 3, 9, 29",
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
            "source_id",
            "revision_id",
            name="uq_standard_revisions_source_revision",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'superseded')",
            name="ck_standard_revisions_status",
        ),
        sa.CheckConstraint(
            "verification_status IN (" + ", ".join(f"'{status}'" for status in VERIFICATION_STATUSES) + ")",
            name="ck_standard_revisions_verification_status",
        ),
        sa.CheckConstraint(
            "(effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from)",
            name="ck_standard_revisions_effective_range",
        ),
    )
    op.create_index("ix_standard_revisions_source_id", "standard_revisions", ["source_id"])
    op.create_index(
        "ix_standard_revisions_verification_status",
        "standard_revisions",
        ["verification_status"],
    )

    op.create_table(
        "standard_rules",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key"),
        sa.Column(
            "revision_id",
            sa.String(36),
            sa.ForeignKey("standard_revisions.id", ondelete="CASCADE"),
            nullable=False,
            comment="Owning standard revision FK (CASCADE delete)",
        ),
        sa.Column(
            "rule_id",
            sa.String(32),
            nullable=False,
            comment="Stable rule identifier, e.g. 'BS001'",
        ),
        sa.Column(
            "category",
            sa.String(64),
            nullable=False,
            comment="Rule group from rules.yaml, e.g. 'balance_sheet'",
        ),
        sa.Column(
            "description",
            sa.Text,
            nullable=False,
            comment="Persian rule description from rules.yaml",
        ),
        sa.Column(
            "dsl_formula",
            sa.Text,
            nullable=True,
            comment="Boolean expression in the closed typed DSL (NULL when not executable)",
        ),
        sa.Column(
            "rule_type",
            sa.String(32),
            nullable=False,
            comment="Rule kind: 'equation', 'threshold' or 'requirement'",
        ),
        sa.Column(
            "severity",
            sa.String(16),
            nullable=False,
            comment="Rule severity: 'error' or 'warning'",
        ),
        sa.Column(
            "standard_refs",
            postgresql.JSONB,
            nullable=True,
            comment="Standard paragraph references, e.g. ['1.55']",
        ),
        sa.Column(
            "is_executable",
            sa.Boolean,
            server_default=sa.text("true"),
            nullable=False,
            comment="False when the rule has no DSL formula and the engine cannot evaluate it",
        ),
        sa.Column(
            "ruleset_version",
            sa.String(64),
            nullable=True,
            comment="Source ruleset version the rule was ported from, e.g. '1.0.0-legacy'",
        ),
        sa.Column(
            "verification_status",
            sa.String(32),
            server_default=sa.text("'UNVERIFIED'"),
            nullable=False,
            comment="Verification label of the rule (inherited from its revision at seed time)",
        ),
        sa.Column(
            "verified_by",
            sa.String(256),
            nullable=True,
            comment="Who/what verified this rule",
        ),
        sa.Column(
            "verified_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the rule verification was recorded (UTC)",
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
            "revision_id",
            "rule_id",
            name="uq_standard_rules_revision_rule",
        ),
        sa.CheckConstraint(
            "rule_type IN ('equation', 'threshold', 'requirement')",
            name="ck_standard_rules_rule_type",
        ),
        sa.CheckConstraint(
            "severity IN ('error', 'warning')",
            name="ck_standard_rules_severity",
        ),
        sa.CheckConstraint(
            "verification_status IN (" + ", ".join(f"'{status}'" for status in VERIFICATION_STATUSES) + ")",
            name="ck_standard_rules_verification_status",
        ),
        sa.CheckConstraint(
            "(is_executable AND dsl_formula IS NOT NULL) OR (NOT is_executable AND dsl_formula IS NULL)",
            name="ck_standard_rules_executable_formula",
        ),
    )
    op.create_index("ix_standard_rules_revision_id", "standard_rules", ["revision_id"])
    op.create_index("ix_standard_rules_rule_id", "standard_rules", ["rule_id"])

    op.create_table(
        "validation_runs",
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
            "period_start",
            sa.Date,
            nullable=True,
            comment="Start of the validated financial period (period-begin dates drive applicability)",
        ),
        sa.Column(
            "period_end",
            sa.Date,
            nullable=True,
            comment="End of the validated financial period",
        ),
        sa.Column(
            "standard_codes",
            postgresql.JSONB,
            nullable=False,
            comment="Resolved list of standard codes the run covered",
        ),
        sa.Column(
            "ruleset_version",
            sa.String(64),
            nullable=False,
            comment="Ruleset version executed, e.g. '1.0.0-legacy'",
        ),
        sa.Column(
            "input_hash",
            sa.String(64),
            nullable=False,
            comment="SHA-256 hex of the canonical supplied financial data",
        ),
        sa.Column(
            "overall_outcome",
            sa.String(32),
            nullable=False,
            comment="Run outcome: 'pass', 'fail', 'error' or 'not_applicable'",
        ),
        sa.Column(
            "is_valid",
            sa.Boolean,
            nullable=False,
            comment="False when any ERROR-severity rule failed or errored (domain ValidationReport.is_valid)",
        ),
        sa.Column(
            "rules_executed",
            sa.Integer,
            server_default=sa.text("0"),
            nullable=False,
            comment="Rules actually evaluated (PASS/FAIL/ERROR outcomes)",
        ),
        sa.Column(
            "rules_passed",
            sa.Integer,
            server_default=sa.text("0"),
            nullable=False,
            comment="Rules with outcome PASS",
        ),
        sa.Column(
            "rules_failed",
            sa.Integer,
            server_default=sa.text("0"),
            nullable=False,
            comment="Rules with outcome FAIL",
        ),
        sa.Column(
            "rules_not_applicable",
            sa.Integer,
            server_default=sa.text("0"),
            nullable=False,
            comment="Rules with outcome NOT_APPLICABLE",
        ),
        sa.Column(
            "rules_errored",
            sa.Integer,
            server_default=sa.text("0"),
            nullable=False,
            comment="Rules with outcome ERROR",
        ),
        sa.Column(
            "coverage_corpus_standards",
            sa.Integer,
            nullable=False,
            comment="Coverage snapshot: standards present in the registry at run time",
        ),
        sa.Column(
            "coverage_standards_with_rules",
            sa.Integer,
            nullable=False,
            comment="Coverage snapshot: standards with machine-readable rules",
        ),
        sa.Column(
            "coverage_rules_implemented",
            sa.Integer,
            nullable=False,
            comment="Coverage snapshot: rules seeded in the registry",
        ),
        sa.Column(
            "coverage_rules_verified",
            sa.Integer,
            nullable=False,
            comment="Coverage snapshot: rules whose revision is VERIFIED_TEXT",
        ),
        sa.Column(
            "coverage_rules_executed",
            sa.Integer,
            nullable=False,
            comment="Coverage snapshot: rules this run executed (equals rules_executed)",
        ),
        sa.Column(
            "not_executed_rules",
            postgresql.JSONB,
            nullable=True,
            comment="Rules implemented but not executable, e.g. [{'rule_id': 'CF005', ...}]",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Row creation timestamp (UTC)",
        ),
        sa.CheckConstraint(
            "overall_outcome IN ('pass', 'fail', 'error', 'not_applicable')",
            name="ck_validation_runs_overall_outcome",
        ),
        sa.CheckConstraint(
            "(period_end IS NULL OR period_start IS NULL OR period_end >= period_start)",
            name="ck_validation_runs_period_range",
        ),
    )
    op.create_index("ix_validation_runs_tenant_id", "validation_runs", ["tenant_id"])
    op.create_index(
        "ix_validation_runs_tenant_created",
        "validation_runs",
        ["tenant_id", "created_at"],
    )

    op.create_table(
        "validation_results",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key"),
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("validation_runs.id", ondelete="CASCADE"),
            nullable=False,
            comment="Owning validation run FK (CASCADE delete)",
        ),
        sa.Column(
            "rule_row_id",
            sa.String(36),
            sa.ForeignKey("standard_rules.id", ondelete="SET NULL"),
            nullable=True,
            comment="Registry rule row FK (SET NULL); rule_code below is the stable history",
        ),
        sa.Column(
            "rule_code",
            sa.String(32),
            nullable=False,
            comment="Stable rule identifier, e.g. 'BS001'",
        ),
        sa.Column(
            "standard_code",
            sa.String(16),
            nullable=False,
            comment="Standard number the rule belongs to, denormalized for history",
        ),
        sa.Column(
            "revision_id",
            sa.String(64),
            nullable=False,
            comment="Revision id of the rule executed, denormalized for history",
        ),
        sa.Column(
            "outcome",
            sa.String(32),
            nullable=False,
            comment="Rule outcome: 'pass', 'fail', 'not_applicable' or 'error'",
        ),
        sa.Column(
            "severity",
            sa.String(16),
            nullable=False,
            comment="Rule severity at execution time: 'error' or 'warning'",
        ),
        sa.Column(
            "message",
            sa.Text,
            nullable=False,
            server_default=sa.text("''"),
            comment="Human-readable outcome message",
        ),
        sa.Column(
            "detail",
            sa.Text,
            nullable=True,
            comment="Extra outcome detail (e.g. evaluation error text)",
        ),
        sa.Column(
            "dsl_formula",
            sa.Text,
            nullable=True,
            comment="DSL formula actually executed (NULL for NOT_APPLICABLE rows)",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Row creation timestamp (UTC)",
        ),
        sa.UniqueConstraint(
            "run_id",
            "rule_code",
            name="uq_validation_results_run_rule",
        ),
        sa.CheckConstraint(
            "outcome IN ('pass', 'fail', 'not_applicable', 'error')",
            name="ck_validation_results_outcome",
        ),
        sa.CheckConstraint(
            "severity IN ('error', 'warning')",
            name="ck_validation_results_severity",
        ),
    )
    op.create_index("ix_validation_results_run_id", "validation_results", ["run_id"])


def downgrade() -> None:
    op.drop_table("validation_results")
    op.drop_table("validation_runs")
    op.drop_table("standard_rules")
    op.drop_table("standard_revisions")
    op.drop_table("standard_sources")
