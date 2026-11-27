"""Request/response schemas for the packaging & shipping endpoints (US-515).

Responses map from the domain models — no ORM object ever leaks.
Money is whole IRR integers; quantities and per-unit costs are
Decimals (JSON numbers). Dates are Gregorian ISO dates (the frontend
renders Jalali). The quote response is always labelled approximate.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from hesabyar.domain.accounting.value_objects import StockLocationView
from hesabyar.domain.packaging.models import (
    CarrierRate,
    ConsumptionRecord,
    CountLine,
    DaysOfCover,
    InventoryCount,
    MaterialDetail,
    MaterialStock,
    OrderCostCenter,
    OrderCostReport,
    PackagingMaterial,
    PackagingSettings,
    PriceQuote,
    PriceQuoteLine,
    ReorderPoint,
    ShipmentCostRecord,
    SupplyCheckReport,
)

# -- requests ----------------------------------------------------------------


class CreateMaterialRequest(BaseModel):
    """Create a packaging material (engine product + catalog row)."""

    company_id: str = Field(description="API company id (row id or engine id)")
    name_fa: str = Field(min_length=1, max_length=256, description="Persian material name")
    cost_method: str = Field(
        default="average", description="Cost method: 'average' (default) | 'fifo'"
    )
    code: str | None = Field(default=None, max_length=64, description="Optional material code")


class UpdateMaterialRequest(BaseModel):
    """Update a material's catalog row (the engine product is untouched)."""

    name_fa: str | None = Field(default=None, min_length=1, max_length=256)
    code: str | None = Field(default=None, max_length=64)
    is_active: bool | None = Field(default=None)


class SetReorderPointRequest(BaseModel):
    """Create or update a material's purchase order point at the warehouse."""

    company_id: str = Field(description="API company id (row id or engine id)")
    min_quantity: Decimal = Field(ge=0, description="Reorder when stock falls below this")
    target_quantity: Decimal = Field(gt=0, description="Quantity the supply run orders up to")


class RecordConsumptionRequest(BaseModel):
    """Record a packaging consumption for an order (idempotent)."""

    company_id: str = Field(description="API company id (row id or engine id)")
    material_id: str = Field(description="Packaging material id")
    order_ref: str = Field(min_length=1, max_length=128, description="Store order reference")
    shipment_ref: str | None = Field(
        default=None, max_length=128, description="Shipment reference (defaults to the order ref)"
    )
    quantity: Decimal = Field(gt=0, description="Consumed quantity (must be > 0)")
    consumed_on: date | None = Field(default=None, description="Consumption date (default: today)")


class RecordShipmentCostRequest(BaseModel):
    """Record the three shipment-cost figures for one order."""

    company_id: str = Field(description="API company id (row id or engine id)")
    order_ref: str = Field(min_length=1, max_length=128, description="Store order reference")
    method: str = Field(description="How the cost is charged: 'on_order' | 'on_shipment' | 'none'")
    actual_irr: int = Field(ge=0, description="Amount actually paid to the carrier (IRR)")
    invoiced_irr: int | None = Field(
        default=None,
        ge=0,
        description="Amount charged to the customer (IRR); when a Tryton sale is linked, "
        "the engine figure is authoritative",
    )
    carrier_name: str | None = Field(default=None, max_length=128)
    tryton_sale_id: int | None = Field(default=None, description="Engine sale.sale id, when linked")
    tryton_shipment_id: int | None = Field(
        default=None, description="Engine stock.shipment.out id, when linked"
    )


class CreateCarrierRateRequest(BaseModel):
    """Add a user-entered carrier rate (no rate is ever hard-coded)."""

    company_id: str = Field(description="API company id (row id or engine id)")
    carrier_name: str = Field(min_length=1, max_length=128, description="Carrier name")
    rate_irr: int = Field(ge=0, description="Rate in whole IRR, per the basis")
    basis: str = Field(default="per_shipment", description="Rate basis (default 'per_shipment')")
    tryton_carrier_id: int | None = Field(default=None)
    effective_from: date | None = Field(default=None, description="First effective date")


class UpdateCarrierRateRequest(BaseModel):
    """Update a carrier rate's amount or active flag."""

    rate_irr: int | None = Field(default=None, ge=0)
    is_active: bool | None = Field(default=None)


class CreateCostCenterRequest(BaseModel):
    """Create the analytic cost centre for one order (one per order)."""

    company_id: str = Field(description="API company id (row id or engine id)")
    order_ref: str = Field(min_length=1, max_length=128, description="Store order reference")


class CreateCountRequest(BaseModel):
    """Open a periodic materials count at the packaging location."""

    company_id: str = Field(description="API company id (row id or engine id)")
    count_date: date | None = Field(default=None, description="Count date (default: today)")
    counted_by: str | None = Field(default=None, max_length=128, description="Who performs the count")


class SetCountLineRequest(BaseModel):
    """Set the counted quantity on one count line (draft counts only)."""

    line_id: int = Field(description="Engine stock.inventory.line id")
    quantity: Decimal = Field(ge=0, description="Counted quantity")


class SaveSettingsRequest(BaseModel):
    """Insert or replace the company's packaging settings."""

    company_id: str = Field(description="API company id (row id or engine id)")
    cost_method: str | None = Field(default=None, description="'average' | 'fifo'")
    shipment_cost_method: str | None = Field(
        default=None, description="'on_order' | 'on_shipment' | 'none'"
    )
    days_of_cover_window_days: int | None = Field(default=None, ge=1)
    low_stock_alert_days: int | None = Field(default=None, ge=0)
    packaging_location_id: int | None = Field(default=None, description="Engine location id (PACK)")
    production_location_id: int | None = Field(
        default=None, description="Engine location id (PACK-CONSUME)"
    )
    warehouse_id: int | None = Field(default=None, description="Engine warehouse id")
    packaging_category_id: int | None = Field(default=None, description="Engine product category id")
    analytic_root_id: int | None = Field(default=None, description="Engine analytic root id")


class RunSupplyCheckRequest(BaseModel):
    """Run the supply check now (reads the wizard's results; never runs the wizard)."""

    company_id: str = Field(description="API company id (row id or engine id)")


class QuotePackagingItemRequest(BaseModel):
    """One packaging line requested in a quote (material + quantity)."""

    material_id: str = Field(description="Packaging material id")
    quantity: Decimal = Field(gt=0, description="Quantity of the material")


class ComputeQuoteRequest(BaseModel):
    """Compute an approximate price quote (never auto-applied to an invoice)."""

    company_id: str = Field(description="API company id (row id or engine id)")
    goods_cost_irr: int = Field(ge=0, description="Cost of the goods themselves (IRR)")
    packaging: list[QuotePackagingItemRequest] = Field(
        default_factory=list, description="Packaging materials and quantities"
    )
    shipment_cost_irr: int | None = Field(
        default=None, ge=0, description="Explicit shipment cost (IRR) — XOR with carrier_rate_id"
    )
    carrier_rate_id: str | None = Field(
        default=None, description="Carrier rate id — XOR with shipment_cost_irr"
    )
    margin_percent: Decimal | None = Field(
        default=None, ge=0, description="Margin as a percent — XOR with margin_fixed_irr"
    )
    margin_fixed_irr: int | None = Field(
        default=None, ge=0, description="Margin as a fixed amount (IRR) — XOR with margin_percent"
    )


# -- responses ---------------------------------------------------------------


class StockLocationResponse(BaseModel):
    """One engine stock location (for the settings UI)."""

    engine_id: int = Field(description="Engine stock.location id")
    name: str = Field(description="Location name")
    location_type: str = Field(description="Location type: warehouse | storage | view | ...")
    code: str | None = Field(description="Location code (e.g. 'PACK')")
    parent_engine_id: int | None = Field(description="Parent location engine id")

    @classmethod
    def from_view(cls, view: StockLocationView) -> StockLocationResponse:
        """Build a response from an engine location view."""
        return cls(
            engine_id=view.ref.engine_id,
            name=view.name,
            location_type=view.location_type,
            code=view.code,
            parent_engine_id=view.parent.engine_id if view.parent is not None else None,
        )


class MaterialResponse(BaseModel):
    """One packaging material (catalog row; live figures live on the detail response)."""

    id: str = Field(description="Material id")
    company_id: str = Field(description="API company id")
    tryton_product_id: int = Field(description="Engine product id")
    tryton_template_id: int = Field(description="Engine product template id")
    name_fa: str = Field(description="Persian material name")
    cost_method: str = Field(description="Cost method: 'average' | 'fifo'")
    code: str | None = Field(description="Material code")
    uom_name: str | None = Field(description="Unit of measure name")
    is_active: bool = Field(description="Whether the material can be consumed")
    created_at: datetime | None = Field(description="When the material was created (UTC)")

    @classmethod
    def from_material(cls, material: PackagingMaterial) -> MaterialResponse:
        """Build a response from a domain material."""
        return cls(
            id=material.id,
            company_id=material.company_id,
            tryton_product_id=material.tryton_product_id,
            tryton_template_id=material.tryton_template_id,
            name_fa=material.name_fa,
            cost_method=material.cost_method.value,
            code=material.code,
            uom_name=material.uom_name,
            is_active=material.is_active,
            created_at=material.created_at,
        )


class MaterialStockResponse(BaseModel):
    """Live stock snapshot of one material at the packaging location."""

    material_id: str = Field(description="Material id")
    quantity: Decimal = Field(description="Quantity on hand (may be negative)")
    unit_cost_irr: Decimal | None = Field(description="Current unit cost (IRR, may be fractional)")
    location_engine_id: int = Field(description="Packaging location engine id")
    location_name: str = Field(description="Packaging location name")
    stock_value_irr: Decimal | None = Field(description="Quantity x unit cost")
    is_negative: bool = Field(description="True when stock is negative (warning signal)")

    @classmethod
    def from_stock(cls, stock: MaterialStock) -> MaterialStockResponse:
        """Build a response from a live stock snapshot."""
        return cls(
            material_id=stock.material_id,
            quantity=stock.quantity,
            unit_cost_irr=stock.unit_cost_irr,
            location_engine_id=stock.location_engine_id,
            location_name=stock.location_name,
            stock_value_irr=stock.stock_value_irr,
            is_negative=stock.is_negative,
        )


class ReorderPointResponse(BaseModel):
    """Order point (min/target) for one material at one warehouse."""

    material_id: str = Field(description="Material id")
    warehouse_engine_id: int = Field(description="Warehouse engine id")
    min_quantity: Decimal = Field(description="Reorder when stock falls below this")
    target_quantity: Decimal = Field(description="Quantity the supply run orders up to")
    tryton_order_point_id: int | None = Field(description="Engine stock.order_point id")
    max_quantity: Decimal | None = Field(description="Max (internal points only)")

    @classmethod
    def from_point(cls, point: ReorderPoint) -> ReorderPointResponse:
        """Build a response from a domain reorder point."""
        return cls(
            material_id=point.material_id,
            warehouse_engine_id=point.warehouse_engine_id,
            min_quantity=point.min_quantity,
            target_quantity=point.target_quantity,
            tryton_order_point_id=point.tryton_order_point_id,
            max_quantity=point.max_quantity,
        )


class DaysOfCoverResponse(BaseModel):
    """Days-of-cover for one material — "when will it run out?"."""

    material_id: str = Field(description="Material id")
    quantity_on_hand: Decimal = Field(description="Live quantity on hand")
    window_days: int = Field(description="Consumption window in days")
    consumed_in_window: Decimal = Field(description="Quantity consumed in the window")
    avg_daily_consumption: Decimal = Field(description="Average daily consumption in the window")
    days_of_cover: float | None = Field(
        description="Days until stock runs out at the average rate (null when no consumption in the window)"
    )
    last_consumption_date: date | None = Field(description="Date of the last consumption in the window")
    is_out_of_stock: bool = Field(description="True when quantity on hand is <= 0")

    @classmethod
    def from_coverage(cls, coverage: DaysOfCover) -> DaysOfCoverResponse:
        """Build a response from a days-of-cover computation."""
        return cls(
            material_id=coverage.material_id,
            quantity_on_hand=coverage.quantity_on_hand,
            window_days=coverage.window_days,
            consumed_in_window=coverage.consumed_in_window,
            avg_daily_consumption=coverage.avg_daily_consumption,
            days_of_cover=coverage.days_of_cover,
            last_consumption_date=(
                coverage.last_consumption_date.gregorian_date
                if coverage.last_consumption_date is not None
                else None
            ),
            is_out_of_stock=coverage.is_out_of_stock,
        )


class MaterialDetailResponse(BaseModel):
    """One material with its live stock, reorder point and coverage."""

    material: MaterialResponse = Field(description="Catalog row")
    stock: MaterialStockResponse | None = Field(
        description="Live stock (null when no packaging location is configured)"
    )
    reorder_point: ReorderPointResponse | None = Field(description="Order point, when set")
    coverage: DaysOfCoverResponse | None = Field(
        description="Days-of-cover (null when no packaging location is configured)"
    )
    is_below_min: bool = Field(description="True when live stock is below the reorder minimum")

    @classmethod
    def from_detail(cls, detail: MaterialDetail) -> MaterialDetailResponse:
        """Build a response from a material detail read model."""
        return cls(
            material=MaterialResponse.from_material(detail.material),
            stock=MaterialStockResponse.from_stock(detail.stock) if detail.stock is not None else None,
            reorder_point=(
                ReorderPointResponse.from_point(detail.reorder_point)
                if detail.reorder_point is not None
                else None
            ),
            coverage=(
                DaysOfCoverResponse.from_coverage(detail.coverage)
                if detail.coverage is not None
                else None
            ),
            is_below_min=detail.is_below_min,
        )


class ConsumptionResponse(BaseModel):
    """One recorded packaging consumption for an order."""

    id: str = Field(description="Consumption id")
    company_id: str = Field(description="API company id")
    material_id: str = Field(description="Material id")
    order_ref: str = Field(description="Store order reference")
    shipment_ref: str = Field(description="Shipment reference")
    quantity: Decimal = Field(description="Consumed quantity")
    unit_cost_irr: Decimal = Field(description="Unit cost at consumption time (IRR)")
    total_cost_irr: int = Field(description="Whole-Rial total (HALF_UP)")
    rounding_delta_irr: int = Field(description="Rounding delta in rials (recorded, never hidden)")
    idempotency_key: str = Field(description="Idempotency key")
    source: str = Field(description="Where the consumption came from: manual | webhook")
    analytic_account_engine_id: int | None = Field(description="Order's analytic account engine id")
    tryton_move_id: int | None = Field(description="Engine stock.move id")
    consumed_on: date | None = Field(description="Consumption date")
    created_at: datetime | None = Field(description="When the record was created (UTC)")

    @classmethod
    def from_record(cls, record: ConsumptionRecord) -> ConsumptionResponse:
        """Build a response from a domain consumption record."""
        return cls(
            id=record.id,
            company_id=record.company_id,
            material_id=record.material_id,
            order_ref=record.order_ref,
            shipment_ref=record.shipment_ref,
            quantity=record.quantity,
            unit_cost_irr=record.unit_cost_irr,
            total_cost_irr=record.total_cost_irr,
            rounding_delta_irr=record.rounding_delta_irr,
            idempotency_key=record.idempotency_key,
            source=record.source.value,
            analytic_account_engine_id=record.analytic_account_engine_id,
            tryton_move_id=record.tryton_move_id,
            consumed_on=record.consumed_on.gregorian_date if record.consumed_on is not None else None,
            created_at=record.created_at,
        )


class ShipmentCostResponse(BaseModel):
    """The three shipment-cost figures for one order, kept separate."""

    id: str = Field(description="Shipment-cost id")
    company_id: str = Field(description="API company id")
    order_ref: str = Field(description="Store order reference")
    method: str = Field(description="How the cost is charged: on_order | on_shipment | none")
    invoiced_irr: int = Field(description="Amount charged to the customer (IRR)")
    actual_irr: int = Field(description="Amount actually paid to the carrier (IRR)")
    absorbed_irr: int = Field(description="Actual minus invoiced — the part the margin absorbs (IRR)")
    carrier_name: str | None = Field(description="Carrier name")
    tryton_sale_id: int | None = Field(description="Engine sale id, when linked")
    tryton_shipment_id: int | None = Field(description="Engine shipment id, when linked")
    analytic_account_engine_id: int | None = Field(description="Order's analytic account engine id")
    created_at: datetime | None = Field(description="When the record was created (UTC)")

    @classmethod
    def from_record(cls, record: ShipmentCostRecord) -> ShipmentCostResponse:
        """Build a response from a domain shipment-cost record."""
        return cls(
            id=record.id,
            company_id=record.company_id,
            order_ref=record.order_ref,
            method=record.method.value,
            invoiced_irr=record.invoiced_irr,
            actual_irr=record.actual_irr,
            absorbed_irr=record.absorbed_irr,
            carrier_name=record.carrier_name,
            tryton_sale_id=record.tryton_sale_id,
            tryton_shipment_id=record.tryton_shipment_id,
            analytic_account_engine_id=record.analytic_account_engine_id,
            created_at=record.created_at,
        )


class CarrierRateResponse(BaseModel):
    """A carrier rate used by the price calculator."""

    id: str = Field(description="Carrier-rate id")
    company_id: str = Field(description="API company id")
    carrier_name: str = Field(description="Carrier name")
    rate_irr: int = Field(description="Rate in whole IRR, per the basis")
    basis: str = Field(description="Rate basis")
    tryton_carrier_id: int | None = Field(description="Engine carrier id, when linked")
    effective_from: date | None = Field(description="First effective date")
    is_active: bool = Field(description="Whether the rate is offered for new quotes")
    created_at: datetime | None = Field(description="When the rate was created (UTC)")

    @classmethod
    def from_rate(cls, rate: CarrierRate) -> CarrierRateResponse:
        """Build a response from a domain carrier rate."""
        return cls(
            id=rate.id,
            company_id=rate.company_id,
            carrier_name=rate.carrier_name,
            rate_irr=rate.rate_irr,
            basis=rate.basis,
            tryton_carrier_id=rate.tryton_carrier_id,
            effective_from=rate.effective_from.gregorian_date if rate.effective_from is not None else None,
            is_active=rate.is_active,
            created_at=rate.created_at,
        )


class CountLineResponse(BaseModel):
    """One line of a materials count: expected (engine) vs counted (human)."""

    material_id: str = Field(description="Material id")
    material_name: str = Field(description="Material name")
    expected_quantity: Decimal = Field(description="Expected quantity from the engine")
    counted_quantity: Decimal | None = Field(description="Counted quantity (null until counted)")
    variance: Decimal | None = Field(description="Counted minus expected (null until counted)")
    tryton_line_id: int | None = Field(description="Engine stock.inventory.line id")

    @classmethod
    def from_line(cls, line: CountLine) -> CountLineResponse:
        """Build a response from a domain count line."""
        return cls(
            material_id=line.material_id,
            material_name=line.material_name,
            expected_quantity=line.expected_quantity,
            counted_quantity=line.counted_quantity,
            variance=line.variance,
            tryton_line_id=line.tryton_line_id,
        )


class InventoryCountResponse(BaseModel):
    """A periodic materials count (``stock.inventory`` in the engine)."""

    id: str = Field(description="Count id")
    company_id: str = Field(description="API company id")
    tryton_inventory_id: int = Field(description="Engine stock.inventory id")
    location_engine_id: int = Field(description="Counted location engine id")
    location_name: str = Field(description="Counted location name")
    count_date: date = Field(description="Count date")
    state: str = Field(description="Count state: draft | done | cancelled")
    counted_by: str | None = Field(description="Who performed the count")
    lines: list[CountLineResponse] = Field(description="Count lines (expected vs counted)")
    created_at: datetime | None = Field(description="When the count was created (UTC)")

    @classmethod
    def from_count(cls, count: InventoryCount) -> InventoryCountResponse:
        """Build a response from a domain inventory count."""
        return cls(
            id=count.id,
            company_id=count.company_id,
            tryton_inventory_id=count.tryton_inventory_id,
            location_engine_id=count.location_engine_id,
            location_name=count.location_name,
            count_date=count.count_date.gregorian_date,
            state=count.state.value,
            counted_by=count.counted_by,
            lines=[CountLineResponse.from_line(line) for line in count.lines],
            created_at=count.created_at,
        )


class OrderCostCenterResponse(BaseModel):
    """One analytic account (cost centre) per order."""

    id: str = Field(description="Cost-centre id")
    company_id: str = Field(description="API company id")
    order_ref: str = Field(description="Store order reference")
    analytic_account_engine_id: int = Field(description="Engine analytic account id")
    created_at: datetime | None = Field(description="When the cost centre was created (UTC)")

    @classmethod
    def from_center(cls, center: OrderCostCenter) -> OrderCostCenterResponse:
        """Build a response from a domain cost centre."""
        return cls(
            id=center.id,
            company_id=center.company_id,
            order_ref=center.order_ref,
            analytic_account_engine_id=center.analytic_account_engine_id,
            created_at=center.created_at,
        )


class OrderCostReportResponse(BaseModel):
    """Full cost of one order, from one analytic cost centre."""

    order_ref: str = Field(description="Store order reference")
    analytic_account_engine_id: int | None = Field(description="Engine analytic account id")
    goods_cost_irr: int = Field(description="Goods cost from the analytic lines (IRR)")
    packaging_cost_irr: int = Field(description="Packaging cost from the consumption records (IRR)")
    shipment_invoiced_irr: int = Field(description="Shipment cost charged to the customer (IRR)")
    shipment_actual_irr: int = Field(description="Shipment cost actually paid to the carrier (IRR)")
    shipment_absorbed_irr: int = Field(description="Shipment cost absorbed into the margin (IRR)")
    revenue_irr: int = Field(description="Revenue tagged to the order (IRR)")
    total_cost_irr: int = Field(description="Goods + packaging + actual shipment cost (IRR)")
    margin_irr: int = Field(description="Revenue minus total cost (IRR)")
    analytic_line_count: int = Field(description="Analytic lines tagged to the order")
    consumption_count: int = Field(description="Consumption records for the order")

    @classmethod
    def from_report(cls, report: OrderCostReport) -> OrderCostReportResponse:
        """Build a response from a domain order-cost report."""
        return cls(
            order_ref=report.order_ref,
            analytic_account_engine_id=report.analytic_account_engine_id,
            goods_cost_irr=report.goods_cost_irr,
            packaging_cost_irr=report.packaging_cost_irr,
            shipment_invoiced_irr=report.shipment_invoiced_irr,
            shipment_actual_irr=report.shipment_actual_irr,
            shipment_absorbed_irr=report.shipment_absorbed_irr,
            revenue_irr=report.revenue_irr,
            total_cost_irr=report.total_cost_irr,
            margin_irr=report.margin_irr,
            analytic_line_count=report.analytic_line_count,
            consumption_count=report.consumption_count,
        )


class PackagingSettingsResponse(BaseModel):
    """Per-company packaging configuration (settings, not secrets)."""

    company_id: str = Field(description="API company id")
    cost_method: str = Field(description="Default cost method for new materials")
    shipment_cost_method: str = Field(description="Default shipment-charge method")
    days_of_cover_window_days: int = Field(description="Days-of-cover window in days")
    low_stock_alert_days: int = Field(description="Low-stock alert threshold in days of cover")
    packaging_location_id: int | None = Field(description="Engine id of the packaging location")
    production_location_id: int | None = Field(description="Engine id of the consumption location")
    warehouse_id: int | None = Field(description="Engine id of the main warehouse")
    packaging_category_id: int | None = Field(description="Engine id of the packaging category")
    analytic_root_id: int | None = Field(description="Engine id of the analytic root")
    updated_at: datetime | None = Field(description="When the settings were last updated (UTC)")

    @classmethod
    def from_settings(cls, settings: PackagingSettings) -> PackagingSettingsResponse:
        """Build a response from domain packaging settings."""
        return cls(
            company_id=settings.company_id,
            cost_method=settings.cost_method.value,
            shipment_cost_method=settings.shipment_cost_method.value,
            days_of_cover_window_days=settings.days_of_cover_window_days,
            low_stock_alert_days=settings.low_stock_alert_days,
            packaging_location_id=settings.packaging_location_id,
            production_location_id=settings.production_location_id,
            warehouse_id=settings.warehouse_id,
            packaging_category_id=settings.packaging_category_id,
            analytic_root_id=settings.analytic_root_id,
            updated_at=settings.updated_at,
        )


class SupplyCheckReportResponse(BaseModel):
    """Result of one supply-check run."""

    company_id: str = Field(description="API company id")
    checked_at: datetime = Field(description="When the check ran (UTC)")
    materials_checked: int = Field(description="Materials whose coverage was checked")
    purchase_requests_seen: int = Field(description="Purchase requests read since the watermark")
    purchase_requests_new: int = Field(description="New purchase requests (above the watermark)")
    items_filed: int = Field(description="packaging_reorder items filed")
    items_skipped_open: int = Field(description="Items skipped because an open item already exists")
    low_stock_materials: list[str] = Field(description="Material ids found low on stock")
    errors: list[str] = Field(description="Per-material errors (collected, never fatal)")

    @classmethod
    def from_report(cls, report: SupplyCheckReport) -> SupplyCheckReportResponse:
        """Build a response from a domain supply-check report."""
        return cls(
            company_id=report.company_id,
            checked_at=report.checked_at,
            materials_checked=report.materials_checked,
            purchase_requests_seen=report.purchase_requests_seen,
            purchase_requests_new=report.purchase_requests_new,
            items_filed=report.items_filed,
            items_skipped_open=report.items_skipped_open,
            low_stock_materials=list(report.low_stock_materials),
            errors=list(report.errors),
        )


class PriceQuoteLineResponse(BaseModel):
    """One priced line of an approximate quote (packaging material)."""

    material_id: str = Field(description="Material id")
    material_name: str = Field(description="Material name")
    quantity: Decimal = Field(description="Quantity")
    unit_cost_irr: Decimal = Field(description="Unit cost used (IRR)")
    line_cost_irr: int = Field(description="Line cost in whole IRR")
    stale_price: bool = Field(description="True when the material is out of stock (last price used)")

    @classmethod
    def from_line(cls, line: PriceQuoteLine) -> PriceQuoteLineResponse:
        """Build a response from a domain quote line."""
        return cls(
            material_id=line.material_id,
            material_name=line.material_name,
            quantity=line.quantity,
            unit_cost_irr=line.unit_cost_irr,
            line_cost_irr=line.line_cost_irr,
            stale_price=line.stale_price,
        )


class PriceQuoteResponse(BaseModel):
    """An approximate price quote — labelled approximate, never auto-applied."""

    goods_cost_irr: int = Field(description="Cost of the goods (IRR)")
    packaging_cost_irr: int = Field(description="Packaging cost at current prices (IRR)")
    shipment_cost_irr: int = Field(description="Shipment cost (IRR)")
    margin_irr: int = Field(description="Margin (IRR)")
    total_irr: int = Field(description="Total (IRR)")
    approximate: bool = Field(description="Always true: the quote is an estimate")
    lines: list[PriceQuoteLineResponse] = Field(description="Priced packaging lines")
    warnings: list[str] = Field(description="Warnings (stale prices, unknown stock)")
    margin_percent: Decimal | None = Field(description="Margin percent used, when given")
    carrier_name: str | None = Field(description="Carrier the shipment rate came from")

    @classmethod
    def from_quote(cls, quote: PriceQuote) -> PriceQuoteResponse:
        """Build a response from a domain price quote."""
        return cls(
            goods_cost_irr=quote.goods_cost_irr,
            packaging_cost_irr=quote.packaging_cost_irr,
            shipment_cost_irr=quote.shipment_cost_irr,
            margin_irr=quote.margin_irr,
            total_irr=quote.total_irr,
            approximate=quote.approximate,
            lines=[PriceQuoteLineResponse.from_line(line) for line in quote.lines],
            warnings=list(quote.warnings),
            margin_percent=quote.margin_percent,
            carrier_name=quote.carrier_name,
        )
