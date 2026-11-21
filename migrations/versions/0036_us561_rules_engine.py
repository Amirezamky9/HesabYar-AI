"""Rules engine v1 tables (US-561, Phase 5-7 ۵خ W2).

Revision ID: 0036
Revises: 0035
Create Date: 2026-11-21

The user rules engine (phased design §6.4): ``when <event> +
<conditions> → do <action>`` over the twelve canonical event types,
with a closed safe action set, dry-run, and a full audit log.

- ``rules`` — one row per user rule: event type, condition DSL
  tree (JSONLogic-style JSONB, validated by the domain — no user
  code), action (``{"kind", "params"}`` from the closed set),
  priority, enabled flag. A NULL ``installation_id`` means the
  rule is tenant-wide (it fires for every installation of the
  tenant).
- ``rule_executions`` — one row per dry-run and per live execution
  of a matched rule: the §6.7 automation decision audit log for
  rules (rule id, event id, decision, outcome, evidence in
  ``details``). Append-only; ``rule_id`` is NULL for dry-runs of
  unsaved definitions and SET NULL when a rule is deleted, so the
  audit row survives the rule.
- ``rule_set_configs`` — one row per rule set (a tenant's rules
  for one event type) with its match mode (``first_match`` — the
  Xero pattern — or ``all_matches``). No row means the default
  (``first_match``).
- ``review_items.tags`` — a JSONB list of strings for the rules
  engine's ``tag`` action (US-561); existing rows get ``[]``.

Chains after 0035 (US-560 job registry — the Alembic head at
branch time). NOTE: the other Wave B workers (US-563, US-564)
also branch from 0035; the coordinator re-chains at merge.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "rules",
        sa.Column("id", sa.String(36), nullable=False, comment="UUID primary key (rule id)"),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            comment="Owning tenant FK (CASCADE delete)",
        ),
        sa.Column(
            "installation_id",
            sa.String(36),
            nullable=True,
            comment="Owning installation (NULL = tenant-wide rule)",
        ),
        sa.Column(
            "name",
            sa.String(200),
            nullable=False,
            comment="Operator-facing rule name",
        ),
        sa.Column(
            "description",
            sa.Text(),
            nullable=False,
            server_default="",
            comment="Operator-facing description of what the rule does",
        ),
        sa.Column(
            "event_type",
            sa.String(64),
            nullable=False,
            comment="Canonical event type that triggers the rule",
        ),
        sa.Column(
            "conditions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="Validated condition DSL tree ({} = no conditions)",
        ),
        sa.Column(
            "action",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment='Action from the closed set: {"kind": ..., "params": {...}}',
        ),
        sa.Column(
            "priority",
            sa.Integer(),
            nullable=False,
            server_default="100",
            comment="Order inside a rule set (lower runs first)",
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
            comment="False stops the rule from firing live (dry-run still works)",
        ),
        sa.Column(
            "created_by",
            sa.String(128),
            nullable=True,
            comment="Actor (API client id) that created the rule",
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
        sa.CheckConstraint("priority >= 0", name="ck_rules_priority_non_negative"),
        sa.Index("ix_rules_tenant_installation_event", "tenant_id", "installation_id", "event_type"),
        sa.Index("ix_rules_tenant_event_priority", "tenant_id", "event_type", "priority"),
    )
    op.create_table(
        "rule_executions",
        sa.Column("id", sa.String(36), nullable=False, comment="UUID primary key (execution id)"),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            comment="Owning tenant FK (CASCADE delete)",
        ),
        sa.Column(
            "rule_id",
            sa.String(36),
            sa.ForeignKey("rules.id", ondelete="SET NULL"),
            nullable=True,
            comment="Rule FK (SET NULL on delete — the audit row survives the rule)",
        ),
        sa.Column(
            "event_id",
            sa.String(36),
            nullable=True,
            comment="Inbox event id (NULL for sample-payload dry-runs)",
        ),
        sa.Column(
            "event_type",
            sa.String(64),
            nullable=False,
            comment="Canonical event type that was evaluated",
        ),
        sa.Column(
            "mode",
            sa.String(16),
            nullable=False,
            comment="live | dry_run",
        ),
        sa.Column(
            "decision",
            sa.String(32),
            nullable=False,
            comment="not_matched | would_execute | executed | blocked_by_policy_gate | skipped | failed",
        ),
        sa.Column(
            "outcome",
            sa.String(64),
            nullable=False,
            comment="Machine-readable outcome code (draft_created, posted, draft_kept, ...)",
        ),
        sa.Column(
            "action_kind",
            sa.String(32),
            nullable=True,
            comment="Action kind that ran (NULL when the rule did not match)",
        ),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="Evidence: gate verdicts, draft/posted refs, channel results",
        ),
        sa.Column(
            "error",
            sa.Text(),
            nullable=True,
            comment="Failure text when the decision is 'failed'",
        ),
        sa.Column(
            "executed_by",
            sa.String(128),
            nullable=False,
            server_default="system",
            comment="Actor (API client id) that triggered the evaluation",
        ),
        sa.Column(
            "executed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="Execution timestamp (UTC)",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("mode IN ('live', 'dry_run')", name="ck_rule_executions_mode"),
        sa.CheckConstraint(
            "decision IN ('not_matched', 'would_execute', 'executed', 'blocked_by_policy_gate', 'skipped', 'failed')",
            name="ck_rule_executions_decision",
        ),
        sa.Index("ix_rule_executions_tenant_rule_executed", "tenant_id", "rule_id", "executed_at"),
        sa.Index("ix_rule_executions_tenant_event", "tenant_id", "event_id"),
    )
    op.create_table(
        "rule_set_configs",
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            comment="Owning tenant FK (CASCADE delete)",
        ),
        sa.Column(
            "event_type",
            sa.String(64),
            nullable=False,
            comment="Canonical event type the set covers",
        ),
        sa.Column(
            "match_mode",
            sa.String(16),
            nullable=False,
            comment="first_match | all_matches",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="Last-modified timestamp (UTC)",
        ),
        sa.PrimaryKeyConstraint("tenant_id", "event_type"),
        sa.CheckConstraint(
            "match_mode IN ('first_match', 'all_matches')",
            name="ck_rule_set_configs_match_mode",
        ),
    )
    op.add_column(
        "review_items",
        sa.Column(
            "tags",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
            comment="Tags applied by rules (US-561 'tag' action) or operators",
        ),
    )


def downgrade() -> None:
    op.drop_column("review_items", "tags")
    op.drop_table("rule_set_configs")
    op.drop_table("rule_executions")
    op.drop_table("rules")
