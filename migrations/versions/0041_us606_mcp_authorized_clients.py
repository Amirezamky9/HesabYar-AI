"""MCP authorized-clients registry (US-606, Phase 6).

Revision ID: 0041
Revises: 0040
Create Date: 2026-11-21

- ``mcp_authorized_clients`` — the management record for one AI
  client's MCP access: which API client + key it uses, its scopes
  and company grant, and its rotation/revocation state. This is NOT
  a second credential system: the credential is an ordinary
  ``api_keys`` row (``hya_live_``, HMAC-SHA-256 + pepper) issued
  through ``ApiKeyService``; the MCP server keeps authenticating
  that key and never consults this table. Registry membership gates
  nothing at call time.

- ``ai_access_audit`` gains ``kind='management'`` rows for client
  lifecycle events (``mcp.client.create`` / ``rotate`` /
  ``revoke``), written by the REST management endpoints over
  ``transport='rest'`` — so the operator's access log shows a
  revocation in the same place as the calls it stops. Both check
  constraints are widened (the old constraint names are kept, so
  the downgrade restores the 0040 definitions exactly).

Chains after 0040 (US-601 AI access audit — the Alembic head at
branch time).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "mcp_authorized_clients",
        sa.Column("id", sa.String(36), nullable=False, comment="UUID primary key"),
        sa.Column(
            "tenant_id",
            sa.String(36),
            nullable=False,
            comment="Owning tenant FK (CASCADE delete)",
        ),
        sa.Column(
            "client_id",
            sa.String(36),
            nullable=False,
            comment="Owning API client FK (CASCADE delete; one api_clients row per MCP client)",
        ),
        sa.Column(
            "key_id",
            sa.String(36),
            nullable=False,
            comment="Current API key FK (moves on rotation; keys are revoked, never deleted)",
        ),
        sa.Column(
            "name",
            sa.String(128),
            nullable=False,
            comment="Human-readable client name (unique per tenant via api_clients)",
        ),
        sa.Column(
            "description",
            sa.Text,
            nullable=True,
            comment="What this client is for (which AI host, which team)",
        ),
        sa.Column(
            "scopes",
            sa.Text,
            nullable=False,
            comment="Comma-separated scopes, mirroring the current key (always includes mcp:connect)",
        ),
        sa.Column(
            "engine_company_id",
            sa.String(36),
            nullable=True,
            comment="Company grant, mirroring the current key (NULL = every company of the tenant)",
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
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Revocation timestamp (UTC); NULL means not revoked",
        ),
        sa.Column(
            "revoked_reason",
            sa.String(256),
            nullable=True,
            comment="Why the client was revoked",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["client_id"], ["api_clients.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["key_id"], ["api_keys.id"]),
        sa.ForeignKeyConstraint(
            ["engine_company_id"], ["engine_companies.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_mcp_authorized_clients_tenant_created",
        "mcp_authorized_clients",
        ["tenant_id", "created_at"],
    )
    op.create_index(
        "ix_mcp_authorized_clients_key",
        "mcp_authorized_clients",
        ["key_id"],
    )
    # ai_access_audit: lifecycle ('management') rows over the REST
    # transport. Drop + recreate the two check constraints (same
    # names, widened predicates).
    op.drop_constraint("ck_ai_access_audit_kind", "ai_access_audit", type_="check")
    op.create_check_constraint(
        "ck_ai_access_audit_kind",
        "ai_access_audit",
        "kind IN ('tool', 'resource', 'prompt', 'management')",
    )
    op.drop_constraint("ck_ai_access_audit_transport", "ai_access_audit", type_="check")
    op.create_check_constraint(
        "ck_ai_access_audit_transport",
        "ai_access_audit",
        "transport IN ('stdio', 'streamable-http', 'memory', 'rest')",
    )


def downgrade() -> None:
    # Management rows cannot exist under 0040's narrower constraint;
    # remove them (the REST audit trail keeps the lifecycle record).
    op.execute("DELETE FROM ai_access_audit WHERE kind = 'management'")
    op.drop_constraint("ck_ai_access_audit_transport", "ai_access_audit", type_="check")
    op.create_check_constraint(
        "ck_ai_access_audit_transport",
        "ai_access_audit",
        "transport IN ('stdio', 'streamable-http', 'memory')",
    )
    op.drop_constraint("ck_ai_access_audit_kind", "ai_access_audit", type_="check")
    op.create_check_constraint(
        "ck_ai_access_audit_kind",
        "ai_access_audit",
        "kind IN ('tool', 'resource', 'prompt')",
    )
    op.drop_table("mcp_authorized_clients")
