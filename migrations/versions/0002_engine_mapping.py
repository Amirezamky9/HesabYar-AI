"""Engine mapping: engine connections, companies, external ID map

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-16

Design-eng spec §7.4 (Engine mapping group) and §10.4 (idempotency
constraints). Tryton record ids are stored as plain external-reference
columns — never SQL foreign keys into Tryton's database (spec §7.1).

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "engine_connections",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key"),
        sa.Column(
            "name",
            sa.String(128),
            nullable=False,
            comment="Human-readable connection name",
        ),
        sa.Column(
            "engine",
            sa.String(32),
            nullable=False,
            comment="Engine provider key, e.g. 'tryton'",
        ),
        sa.Column(
            "base_url",
            sa.String(512),
            nullable=False,
            comment="Engine base URL",
        ),
        sa.Column(
            "database_name",
            sa.String(128),
            nullable=False,
            comment="Engine database name",
        ),
        sa.Column(
            "is_active",
            sa.Boolean,
            server_default=sa.text("true"),
            nullable=False,
            comment="Whether the connection may be used",
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
            "engine", "database_name", name="uq_engine_connections_engine_database"
        ),
    )

    op.create_table(
        "engine_companies",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key"),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            comment="Owning tenant FK (CASCADE delete)",
        ),
        sa.Column(
            "engine_connection_id",
            sa.String(36),
            sa.ForeignKey("engine_connections.id", ondelete="CASCADE"),
            nullable=False,
            comment="Owning engine connection FK (CASCADE delete)",
        ),
        sa.Column(
            "external_company_id",
            sa.String(64),
            nullable=False,
            comment="Engine company id (e.g. Tryton company.company id) — external reference, not a cross-DB FK",
        ),
        sa.Column(
            "company_name",
            sa.String(256),
            nullable=True,
            comment="Cached engine company display name",
        ),
        sa.Column(
            "currency_code",
            sa.String(3),
            nullable=True,
            comment="Engine company currency code, e.g. 'IRR'",
        ),
        sa.Column(
            "is_active",
            sa.Boolean,
            server_default=sa.text("true"),
            nullable=False,
            comment="Whether the mapping may be used",
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
            "tenant_id", "engine_connection_id", name="uq_engine_companies_tenant_connection"
        ),
        sa.UniqueConstraint(
            "engine_connection_id",
            "external_company_id",
            name="uq_engine_companies_connection_external",
        ),
    )
    op.create_index("ix_engine_companies_tenant_id", "engine_companies", ["tenant_id"])
    op.create_index(
        "ix_engine_companies_engine_connection_id", "engine_companies", ["engine_connection_id"]
    )

    op.create_table(
        "external_id_map",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key"),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            comment="Owning tenant FK (CASCADE delete)",
        ),
        sa.Column(
            "source_system",
            sa.String(64),
            nullable=False,
            comment="Source system key, e.g. 'hayat'",
        ),
        sa.Column(
            "external_entity_type",
            sa.String(128),
            nullable=False,
            comment="Source entity type, e.g. 'order', 'customer'",
        ),
        sa.Column(
            "external_entity_id",
            sa.String(128),
            nullable=False,
            comment="Source entity id — external reference",
        ),
        sa.Column(
            "target_engine",
            sa.String(32),
            nullable=False,
            comment="Target engine key, e.g. 'tryton'",
        ),
        sa.Column(
            "target_entity_type",
            sa.String(128),
            nullable=False,
            comment="Engine model, e.g. 'account.invoice'",
        ),
        sa.Column(
            "target_external_id",
            sa.String(128),
            nullable=False,
            comment="Engine record id — external reference, not a cross-database FK",
        ),
        sa.Column(
            "engine_company_id",
            sa.String(36),
            sa.ForeignKey("engine_companies.id", ondelete="SET NULL"),
            nullable=True,
            comment="Optional engine company scope FK (SET NULL on delete)",
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
            "source_system",
            "external_entity_type",
            "external_entity_id",
            "target_engine",
            name="uq_external_id_map_source_target",
        ),
    )
    op.create_index("ix_external_id_map_tenant_id", "external_id_map", ["tenant_id"])
    op.create_index(
        "ix_external_id_map_engine_company_id", "external_id_map", ["engine_company_id"]
    )
    op.create_index(
        "ix_external_id_map_target",
        "external_id_map",
        ["target_engine", "target_entity_type", "target_external_id"],
    )


def downgrade() -> None:
    op.drop_table("external_id_map")
    op.drop_table("engine_companies")
    op.drop_table("engine_connections")
