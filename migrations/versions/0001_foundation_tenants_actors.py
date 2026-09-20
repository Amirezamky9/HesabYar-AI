"""Foundation schema: tenants and actors

Revision ID: 0001
Revises:
Create Date: 2026-09-20

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key"),
        sa.Column(
            "slug",
            sa.String(128),
            unique=True,
            nullable=False,
            comment="URL-safe unique identifier for the tenant",
        ),
        sa.Column(
            "name",
            sa.String(256),
            nullable=False,
            comment="Organization or business legal/trading name",
        ),
        sa.Column(
            "is_active",
            sa.Boolean,
            server_default=sa.text("true"),
            nullable=False,
            comment="Whether the tenant may perform transactions",
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
    )

    op.create_table(
        "actors",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key"),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            comment="Owning tenant FK (CASCADE delete)",
        ),
        sa.Column(
            "username",
            sa.String(128),
            nullable=False,
            comment="Login or system identity within the tenant",
        ),
        sa.Column(
            "display_name",
            sa.String(256),
            nullable=False,
            comment="Human-readable display name",
        ),
        sa.Column(
            "scopes",
            sa.Text,
            server_default=sa.text("''"),
            nullable=False,
            comment="Comma-separated permission scopes",
        ),
        sa.Column(
            "is_active",
            sa.Boolean,
            server_default=sa.text("true"),
            nullable=False,
            comment="Whether the actor is allowed to operate",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Row creation timestamp (UTC)",
        ),
    )

    op.create_index("ix_actors_tenant_id", "actors", ["tenant_id"])
    op.create_unique_constraint("uq_actors_tenant_username", "actors", ["tenant_id", "username"])


def downgrade() -> None:
    op.drop_table("actors")
    op.drop_table("tenants")
