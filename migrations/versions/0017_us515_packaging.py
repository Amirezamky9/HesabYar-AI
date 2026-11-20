"""Packaging & shipping: materials, consumptions, shipment costs, rates, counts (US-515).

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-18

Phase 5 wave 2, stream K1 (phased design §1.3 5چ / §6 هـ.۱): packaging
materials as consumable goods in Tryton stock, with HesabYar-side
records for the catalog, the consumption trace, shipment costs,
carrier rates, periodic counts, per-order cost centres, settings and
the supply-check watermark. Engine state (products, quantities, cost
prices, order points, moves, purchase requests, inventories, analytic
accounts/lines) is NEVER mirrored here — it is read live through the
engine port.

Eight tables:

- ``packaging_materials`` — catalog row over a Tryton product
  (unique per tenant/company/product).
- ``packaging_consumptions`` — one consumption record per order +
  shipment + material; ``idempotency_key`` unique per tenant.
- ``packaging_shipment_costs`` — the three shipment figures
  (invoiced / actual / absorbed) per order.
- ``packaging_carrier_rates`` — user-entered carrier rates for the
  price calculator (never hard-coded).
- ``packaging_counts`` — periodic materials counts (``stock.inventory``
  mirror; lines as a JSONB snapshot, quantities as strings).
- ``packaging_order_cost_centers`` — order ref -> analytic account
  (unique per tenant/company/order).
- ``packaging_settings`` — per-company configuration (one row per
  tenant/company).
- ``packaging_supply_watermarks`` — highest purchase.request engine
  id the supply check has seen (monotonic).

Chains after 0011 (US-504 review queue). The coordinator re-chains
migrations at merge time.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "packaging_materials",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key (material id)"),
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
            nullable=False,
            comment="API company id as supplied (row id or engine id); plain external reference, no FK",
        ),
        sa.Column("tryton_product_id", sa.Integer, nullable=False, comment="Engine product.product id (the variant)"),
        sa.Column("tryton_template_id", sa.Integer, nullable=False, comment="Engine product.template id"),
        sa.Column("name_fa", sa.String(256), nullable=False, comment="Persian material name"),
        sa.Column(
            "cost_method",
            sa.String(16),
            nullable=False,
            server_default="average",
            comment="Cost method: 'average' (default) | 'fifo'",
        ),
        sa.Column(
            "code", sa.String(64), nullable=True, comment="Optional material code (mirrors the Tryton template code)"
        ),
        sa.Column(
            "uom_name",
            sa.String(64),
            nullable=True,
            comment="Unit of measure name at creation (display only; engine is authoritative)",
        ),
        sa.Column(
            "is_active",
            sa.Boolean,
            nullable=False,
            server_default=sa.true(),
            comment="Inactive materials stay in history but cannot be consumed",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment="Row creation timestamp (UTC)",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment="Last-modified timestamp (UTC)",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "company_id",
            "tryton_product_id",
            name="uq_packaging_materials_tenant_company_product",
        ),
        sa.CheckConstraint("cost_method IN ('average', 'fifo')", name="ck_packaging_materials_cost_method"),
    )
    op.create_index(
        "ix_packaging_materials_tenant_company",
        "packaging_materials",
        ["tenant_id", "company_id"],
    )

    op.create_table(
        "packaging_consumptions",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key (consumption id)"),
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
            nullable=False,
            comment="API company id as supplied (row id or engine id); plain external reference, no FK",
        ),
        sa.Column(
            "material_id",
            sa.String(36),
            sa.ForeignKey("packaging_materials.id", ondelete="RESTRICT"),
            nullable=False,
            comment="Material FK — a material with consumption history cannot be deleted",
        ),
        sa.Column(
            "order_ref",
            sa.String(128),
            nullable=False,
            comment="Store order reference (manual entry until US-511 wires the store)",
        ),
        sa.Column(
            "shipment_ref",
            sa.String(128),
            nullable=False,
            comment="Shipment reference within the order (manual entry until US-511)",
        ),
        sa.Column("quantity", sa.Numeric(18, 4), nullable=False, comment="Consumed quantity (must be > 0)"),
        sa.Column(
            "unit_cost_irr",
            sa.Numeric(18, 4),
            nullable=False,
            comment="Engine average cost price per unit at move time (may be fractional)",
        ),
        sa.Column(
            "total_cost_irr", sa.BigInteger, nullable=False, comment="Whole-Rial total (quantity x unit cost, HALF_UP)"
        ),
        sa.Column(
            "rounding_delta_irr",
            sa.BigInteger,
            nullable=False,
            comment="Total minus the exact product, in rials — recorded, never hidden",
        ),
        sa.Column(
            "idempotency_key",
            sa.String(256),
            nullable=False,
            comment="pack:use:{order_ref}:{shipment_ref}:{material_id}",
        ),
        sa.Column(
            "source",
            sa.String(16),
            nullable=False,
            server_default="manual",
            comment="Where the consumption came from: 'manual' | 'webhook'",
        ),
        sa.Column(
            "analytic_account_engine_id",
            sa.Integer,
            nullable=True,
            comment="Order's analytic account engine id (NULL when the order has no cost centre yet)",
        ),
        sa.Column(
            "tryton_move_id",
            sa.Integer,
            nullable=True,
            comment="Engine stock.move id (NULL while the claim is unexecuted)",
        ),
        sa.Column(
            "consumed_on",
            sa.Date,
            nullable=True,
            comment="Consumption date (Gregorian; the move's planned/effective date)",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment="Row creation timestamp (UTC)",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_packaging_consumptions_tenant_key",
        ),
        sa.CheckConstraint("source IN ('manual', 'webhook')", name="ck_packaging_consumptions_source"),
        sa.CheckConstraint("quantity > 0", name="ck_packaging_consumptions_quantity_positive"),
    )
    op.create_index(
        "ix_packaging_consumptions_tenant_company",
        "packaging_consumptions",
        ["tenant_id", "company_id"],
    )
    op.create_index(
        "ix_packaging_consumptions_tenant_company_order",
        "packaging_consumptions",
        ["tenant_id", "company_id", "order_ref"],
    )
    op.create_index(
        "ix_packaging_consumptions_material",
        "packaging_consumptions",
        ["material_id"],
    )

    op.create_table(
        "packaging_shipment_costs",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key (shipment-cost id)"),
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
            nullable=False,
            comment="API company id as supplied (row id or engine id); plain external reference, no FK",
        ),
        sa.Column("order_ref", sa.String(128), nullable=False, comment="Store order reference"),
        sa.Column(
            "method",
            sa.String(16),
            nullable=False,
            comment="How the cost is charged: 'on_order' | 'on_shipment' | 'none'",
        ),
        sa.Column(
            "invoiced_irr",
            sa.BigInteger,
            nullable=False,
            comment="Amount charged to the customer (0 when method is 'none')",
        ),
        sa.Column("actual_irr", sa.BigInteger, nullable=False, comment="Amount actually paid to the carrier"),
        sa.Column(
            "carrier_name",
            sa.String(128),
            nullable=True,
            comment="Carrier name (free text; the rate table owns the configured rates)",
        ),
        sa.Column(
            "tryton_sale_id",
            sa.Integer,
            nullable=True,
            comment="Engine sale.sale id when the shipment cost is linked to a Tryton sale",
        ),
        sa.Column(
            "tryton_shipment_id",
            sa.Integer,
            nullable=True,
            comment="Engine stock.shipment.out id when linked to a Tryton shipment",
        ),
        sa.Column(
            "analytic_account_engine_id",
            sa.Integer,
            nullable=True,
            comment="Order's analytic account engine id (NULL when the order has no cost centre yet)",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment="Row creation timestamp (UTC)",
        ),
        sa.CheckConstraint("method IN ('on_order', 'on_shipment', 'none')", name="ck_packaging_shipment_costs_method"),
        sa.CheckConstraint("invoiced_irr >= 0 AND actual_irr >= 0", name="ck_packaging_shipment_costs_non_negative"),
    )
    op.create_index(
        "ix_packaging_shipment_costs_tenant_company",
        "packaging_shipment_costs",
        ["tenant_id", "company_id"],
    )
    op.create_index(
        "ix_packaging_shipment_costs_tenant_company_order",
        "packaging_shipment_costs",
        ["tenant_id", "company_id", "order_ref"],
    )

    op.create_table(
        "packaging_carrier_rates",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key (carrier-rate id)"),
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
            nullable=False,
            comment="API company id as supplied (row id or engine id); plain external reference, no FK",
        ),
        sa.Column("carrier_name", sa.String(128), nullable=False, comment="Carrier name (free text)"),
        sa.Column("rate_irr", sa.BigInteger, nullable=False, comment="Rate in whole IRR, per the basis"),
        sa.Column(
            "basis",
            sa.String(32),
            nullable=False,
            server_default="per_shipment",
            comment="Rate basis: 'per_shipment' (default) | 'per_kg' | 'per_km' — free string",
        ),
        sa.Column(
            "tryton_carrier_id",
            sa.Integer,
            nullable=True,
            comment="Engine carrier id when the rate is linked to a Tryton carrier",
        ),
        sa.Column(
            "effective_from",
            sa.Date,
            nullable=True,
            comment="First effective date (Gregorian; NULL = effective immediately)",
        ),
        sa.Column(
            "is_active",
            sa.Boolean,
            nullable=False,
            server_default=sa.true(),
            comment="Inactive rates stay in history but are not offered for new quotes",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment="Row creation timestamp (UTC)",
        ),
        sa.CheckConstraint("rate_irr >= 0", name="ck_packaging_carrier_rates_non_negative"),
    )
    op.create_index(
        "ix_packaging_carrier_rates_tenant_company",
        "packaging_carrier_rates",
        ["tenant_id", "company_id"],
    )

    op.create_table(
        "packaging_counts",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key (count id)"),
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
            nullable=False,
            comment="API company id as supplied (row id or engine id); plain external reference, no FK",
        ),
        sa.Column("tryton_inventory_id", sa.Integer, nullable=False, comment="Engine stock.inventory id"),
        sa.Column(
            "location_engine_id",
            sa.Integer,
            nullable=False,
            comment="Counted location engine id (the packaging storage location)",
        ),
        sa.Column(
            "location_name", sa.String(256), nullable=False, comment="Counted location name at creation (display only)"
        ),
        sa.Column("count_date", sa.Date, nullable=False, comment="Count date (Gregorian)"),
        sa.Column(
            "state",
            sa.String(16),
            nullable=False,
            server_default="draft",
            comment="Count state: 'draft' | 'done' | 'cancelled'",
        ),
        sa.Column("counted_by", sa.String(128), nullable=True, comment="Who performed the count (actor id/name)"),
        sa.Column(
            "lines",
            JSONB,
            nullable=False,
            server_default="[]",
            comment="Line snapshot: [{material_id, material_name, expected_quantity, "
            "counted_quantity, tryton_line_id}]",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment="Row creation timestamp (UTC)",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment="Last-modified timestamp (UTC)",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "tryton_inventory_id",
            name="uq_packaging_counts_tenant_inventory",
        ),
        sa.CheckConstraint("state IN ('draft', 'done', 'cancelled')", name="ck_packaging_counts_state"),
    )
    op.create_index(
        "ix_packaging_counts_tenant_company",
        "packaging_counts",
        ["tenant_id", "company_id"],
    )

    op.create_table(
        "packaging_order_cost_centers",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key (cost-centre id)"),
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
            nullable=False,
            comment="API company id as supplied (row id or engine id); plain external reference, no FK",
        ),
        sa.Column("order_ref", sa.String(128), nullable=False, comment="Store order reference"),
        sa.Column(
            "analytic_account_engine_id",
            sa.Integer,
            nullable=False,
            comment="Engine analytic_account.account id (type 'normal' under the orders root)",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment="Row creation timestamp (UTC)",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "company_id",
            "order_ref",
            name="uq_packaging_cost_centers_tenant_company_order",
        ),
    )
    op.create_index(
        "ix_packaging_cost_centers_tenant_company",
        "packaging_order_cost_centers",
        ["tenant_id", "company_id"],
    )

    op.create_table(
        "packaging_settings",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key (settings id)"),
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
            nullable=False,
            comment="API company id as supplied (row id or engine id); plain external reference, no FK",
        ),
        sa.Column(
            "cost_method",
            sa.String(16),
            nullable=False,
            server_default="average",
            comment="Default cost method for new materials: 'average' | 'fifo'",
        ),
        sa.Column(
            "shipment_cost_method",
            sa.String(16),
            nullable=False,
            server_default="on_order",
            comment="Default shipment-charge method: 'on_order' | 'on_shipment' | 'none'",
        ),
        sa.Column(
            "days_of_cover_window_days",
            sa.Integer,
            nullable=False,
            server_default="30",
            comment="Days-of-cover window in days (default 30)",
        ),
        sa.Column(
            "low_stock_alert_days",
            sa.Integer,
            nullable=False,
            server_default="7",
            comment="Low-stock alert threshold in days of cover remaining (default 7)",
        ),
        sa.Column(
            "packaging_location_id",
            sa.Integer,
            nullable=True,
            comment="Engine id of the packaging storage location (code PACK)",
        ),
        sa.Column(
            "production_location_id",
            sa.Integer,
            nullable=True,
            comment="Engine id of the consumption (production) location (code PACK-CONSUME)",
        ),
        sa.Column(
            "warehouse_id",
            sa.Integer,
            nullable=True,
            comment="Engine id of the main warehouse (order points attach here)",
        ),
        sa.Column(
            "packaging_category_id",
            sa.Integer,
            nullable=True,
            comment="Engine id of the packaging product category (accounting lives here)",
        ),
        sa.Column(
            "analytic_root_id",
            sa.Integer,
            nullable=True,
            comment="Engine id of the analytic root for per-order cost centres",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment="Last-modified timestamp (UTC)",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "company_id",
            name="uq_packaging_settings_tenant_company",
        ),
        sa.CheckConstraint("cost_method IN ('average', 'fifo')", name="ck_packaging_settings_cost_method"),
        sa.CheckConstraint(
            "shipment_cost_method IN ('on_order', 'on_shipment', 'none')",
            name="ck_packaging_settings_shipment_cost_method",
        ),
        sa.CheckConstraint("days_of_cover_window_days >= 1", name="ck_packaging_settings_window_positive"),
        sa.CheckConstraint("low_stock_alert_days >= 0", name="ck_packaging_settings_alert_non_negative"),
    )

    op.create_table(
        "packaging_supply_watermarks",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key (watermark id)"),
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
            nullable=False,
            comment="API company id as supplied (row id or engine id); plain external reference, no FK",
        ),
        sa.Column(
            "watermark",
            sa.Integer,
            nullable=False,
            server_default="0",
            comment="Highest purchase.request engine id processed (0 = none yet)",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment="Last-modified timestamp (UTC)",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "company_id",
            name="uq_packaging_watermarks_tenant_company",
        ),
    )


def downgrade() -> None:
    op.drop_table("packaging_supply_watermarks")
    op.drop_table("packaging_settings")
    op.drop_index("ix_packaging_cost_centers_tenant_company", table_name="packaging_order_cost_centers")
    op.drop_table("packaging_order_cost_centers")
    op.drop_index("ix_packaging_counts_tenant_company", table_name="packaging_counts")
    op.drop_table("packaging_counts")
    op.drop_index("ix_packaging_carrier_rates_tenant_company", table_name="packaging_carrier_rates")
    op.drop_table("packaging_carrier_rates")
    op.drop_index(
        "ix_packaging_shipment_costs_tenant_company_order",
        table_name="packaging_shipment_costs",
    )
    op.drop_index(
        "ix_packaging_shipment_costs_tenant_company",
        table_name="packaging_shipment_costs",
    )
    op.drop_table("packaging_shipment_costs")
    op.drop_index("ix_packaging_consumptions_material", table_name="packaging_consumptions")
    op.drop_index(
        "ix_packaging_consumptions_tenant_company_order",
        table_name="packaging_consumptions",
    )
    op.drop_index("ix_packaging_consumptions_tenant_company", table_name="packaging_consumptions")
    op.drop_table("packaging_consumptions")
    op.drop_index("ix_packaging_materials_tenant_company", table_name="packaging_materials")
    op.drop_table("packaging_materials")
