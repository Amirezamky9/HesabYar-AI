"""AI access audit log (US-601, Phase 6 MCP server core).

Revision ID: 0040
Revises: 0039
Create Date: 2026-11-21

Every MCP call is audit-logged (spec §13.8): the client identity, the
tool/resource/prompt name, the company it touched, the scopes the key
carried, the outcome, and the timestamp. The table stores NO tool
arguments, NO chain-of-thought or model reasoning, and NO secrets —
only the key's visible prefix, never key material.

- ``ai_access_audit`` — one row per MCP call attempt, including denied
  and failed attempts (a denied call is the audit event that matters
  most). ``tenant_id``/``client_id``/``key_id`` are NULL for attempts
  that failed before any identity could be established, so unauthenticated
  probes are still recorded.

Chains after 0039 (US-565 bank rules — the Alembic head at branch time).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_access_audit",
        sa.Column("id", sa.String(36), nullable=False, comment="UUID primary key (audit row id)"),
        sa.Column(
            "tenant_id",
            sa.String(36),
            nullable=True,
            comment="Owning tenant FK (CASCADE delete); NULL when no identity was established",
        ),
        sa.Column(
            "client_id",
            sa.String(36),
            nullable=True,
            comment="API client id (SET NULL on delete); NULL when unauthenticated",
        ),
        sa.Column(
            "key_id",
            sa.String(36),
            nullable=True,
            comment="API key id (SET NULL on delete); NULL when unauthenticated",
        ),
        sa.Column(
            "key_prefix",
            sa.String(16),
            nullable=True,
            comment="Visible API-key prefix (safe to log); NULL when unauthenticated",
        ),
        sa.Column(
            "tool",
            sa.String(128),
            nullable=False,
            comment="Tool name, resource URI or prompt name that was called",
        ),
        sa.Column(
            "kind",
            sa.String(16),
            nullable=False,
            comment="tool | resource | prompt — what kind of MCP call this was",
        ),
        sa.Column(
            "company_id",
            sa.String(128),
            nullable=True,
            comment="HesabYar company row id the call touched (NULL when no company was involved)",
        ),
        sa.Column(
            "scopes",
            sa.Text,
            nullable=True,
            comment="Comma-separated scopes the key carried at call time (NULL when unauthenticated)",
        ),
        sa.Column(
            "outcome",
            sa.String(16),
            nullable=False,
            comment="allowed | denied | error — whether the call ran, was refused, or failed",
        ),
        sa.Column(
            "error_code",
            sa.String(64),
            nullable=True,
            comment="Machine-readable failure reason for denied/error outcomes (NULL when allowed)",
        ),
        sa.Column(
            "request_id",
            sa.String(64),
            nullable=True,
            comment="MCP request id, when the transport supplied one",
        ),
        sa.Column(
            "transport",
            sa.String(32),
            nullable=False,
            comment="stdio | streamable-http | memory — which transport served the call",
        ),
        sa.Column(
            "client_info",
            sa.String(256),
            nullable=True,
            comment="MCP client name/version from initialize, when known (e.g. 'claude/1.0')",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Call timestamp (UTC)",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["client_id"], ["api_clients.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["key_id"], ["api_keys.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "kind IN ('tool', 'resource', 'prompt')",
            name="ck_ai_access_audit_kind",
        ),
        sa.CheckConstraint(
            "outcome IN ('allowed', 'denied', 'error')",
            name="ck_ai_access_audit_outcome",
        ),
        sa.CheckConstraint(
            "transport IN ('stdio', 'streamable-http', 'memory')",
            name="ck_ai_access_audit_transport",
        ),
    )
    op.create_index(
        "ix_ai_access_audit_tenant_created",
        "ai_access_audit",
        ["tenant_id", "created_at"],
    )
    op.create_index(
        "ix_ai_access_audit_client_created",
        "ai_access_audit",
        ["client_id", "created_at"],
    )
    op.create_index(
        "ix_ai_access_audit_tool_created",
        "ai_access_audit",
        ["tool", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("ai_access_audit")
