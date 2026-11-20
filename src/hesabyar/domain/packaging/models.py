"""Domain models for packaging & shipping (US-515, stream K1).

Money is whole IRR (``int``); quantities are ``Decimal`` (Tryton
stores them as floats — the adapter converts at the boundary).
Persian labels live in the frontend; this layer stays English.

Two modelling facts (verified against Tryton 8.2.3 source, see
``docs/packaging.md``):

- A material is a Tryton product of type ``goods`` with
  ``consumable=False`` and ``purchasable=True``. The Tryton
  ``consumable`` flag means "assign moves regardless of stock" and
  *excludes* the product from order points, ``generate_requests``
  and inventory line-filling — the opposite of what the phased
  design's "consumable" wording suggests. "Consumable" here is the
  business classification: packaging materials are consumables
  (operating expense), never fixed assets, never depreciation.
- Consumption moves go from the packaging storage location to a
  ``production`` location. Under anglo-saxon accounting that posts
  debit ``account_stock_out`` (the packaging expense account) /
  credit ``account_stock`` (inventory) — the consumption is
  expensed at the moment of use, at the product's current cost
  price. Internal storage-to-storage moves post nothing.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum, unique

from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.packaging.errors import (
    InvalidCarrierRateError,
    InvalidConsumptionError,
    InvalidMaterialError,
    InvalidPriceQuoteError,
    InvalidReorderPointError,
    InvalidShipmentCostError,
)


@unique
class CostMethod(StrEnum):
    """Tryton cost-price method for a material (average default, FIFO optional)."""

    AVERAGE = "average"
    FIFO = "fifo"


@unique
class ShipmentCostMethod(StrEnum):
    """How the shipment cost is charged to the customer (``sale_shipment_cost``).

    ``on_order``: one shipment-cost line for the whole sale.
    ``on_shipment``: one line per shipment. ``none``: no charge to the
    customer — the whole actual cost is absorbed into the margin.
    """

    ON_ORDER = "on_order"
    ON_SHIPMENT = "on_shipment"
    NONE = "none"


@unique
class ConsumptionSource(StrEnum):
    """Where a consumption record came from (dual-entry, phased design §هـ.۱.۶)."""

    MANUAL = "manual"
    WEBHOOK = "webhook"  # shipment.dispatched from the store (US-511 wires it)


@unique
class CountState(StrEnum):
    """Lifecycle of a periodic materials count (``stock.inventory`` mirrors it)."""

    DRAFT = "draft"
    DONE = "done"
    CANCELLED = "cancelled"


#: Default days-of-cover window (design §هـ.۱.۳): 30-day average consumption.
DEFAULT_COVERAGE_WINDOW_DAYS = 30

#: Default low-stock alert threshold in days of cover remaining.
DEFAULT_LOW_STOCK_ALERT_DAYS = 7


def consumption_idempotency_key(order_ref: str, shipment_ref: str, material_id: str) -> str:
    """Idempotency key for one consumption line (design §هـ.۱.۶).

    The design's key is ``pack:use:{order_ref}:{shipment_ref}`` for a
    whole shipment event (with ``items[]`` inside); the REST API
    records one material per call, so the material id joins the key.
    The same order + shipment + material can only ever consume once —
    a re-send replays the stored record instead of moving stock twice.
    """
    return f"pack:use:{order_ref}:{shipment_ref}:{material_id}"


@dataclass(frozen=True, slots=True)
class PackagingMaterial:
    """One packaging material: a HesabYar catalog row over a Tryton product.

    ``tryton_product_id`` / ``tryton_template_id`` are engine ids
    (opaque ints); quantity and cost price are NEVER copied here —
    they are always read live from the engine through the port.
    """

    id: str
    tenant_id: str
    company_id: str
    tryton_product_id: int
    tryton_template_id: int
    name_fa: str
    cost_method: CostMethod = CostMethod.AVERAGE
    code: str | None = None
    uom_name: str | None = None
    is_active: bool = True
    created_at: datetime.datetime | None = None

    def __post_init__(self) -> None:
        if not self.name_fa.strip():
            raise InvalidMaterialError(
                "A packaging material needs a Persian name.",
                details={"material_id": self.id},
            )
        if self.tryton_product_id <= 0 or self.tryton_template_id <= 0:
            raise InvalidMaterialError(
                "A packaging material needs valid Tryton product/template ids.",
                details={"material_id": self.id},
            )


@dataclass(frozen=True, slots=True)
class ReorderPoint:
    """Order point (min/target/max) for one material at one warehouse.

    Lives in Tryton as ``stock.order_point`` (type ``purchase``); the
    daily supply run (``ir.cron`` → ``stock.order_point|supply_stock``)
    turns a breached point into a ``purchase.request``. ``max_quantity``
    only applies to ``internal`` points; purchase points carry min +
    target (Tryton hides max for them).
    """

    material_id: str
    warehouse_engine_id: int
    min_quantity: Decimal
    target_quantity: Decimal
    tryton_order_point_id: int | None = None
    max_quantity: Decimal | None = None

    def __post_init__(self) -> None:
        if self.min_quantity < 0 or self.target_quantity <= 0:
            raise InvalidReorderPointError(
                "min_quantity must be >= 0 and target_quantity > 0.",
                details={"material_id": self.material_id},
            )
        if self.min_quantity > self.target_quantity:
            raise InvalidReorderPointError(
                f"min_quantity ({self.min_quantity}) must not exceed "
                f"target_quantity ({self.target_quantity}).",
                details={"material_id": self.material_id},
            )
        if self.max_quantity is not None and self.target_quantity > self.max_quantity:
            raise InvalidReorderPointError(
                f"target_quantity ({self.target_quantity}) must not exceed "
                f"max_quantity ({self.max_quantity}).",
                details={"material_id": self.material_id},
            )


@dataclass(frozen=True, slots=True)
class MaterialStock:
    """Live stock snapshot of one material at the packaging location.

    Read from the engine on demand — never stored. ``quantity`` may be
    negative (consumption recorded ahead of purchase): that is a
    warning signal, not an error (design §هـ.۱.۶).
    """

    material_id: str
    quantity: Decimal
    unit_cost_irr: Decimal | None
    location_engine_id: int
    location_name: str

    @property
    def stock_value_irr(self) -> Decimal | None:
        """Quantity x unit cost (None when the engine has no cost price yet)."""
        if self.unit_cost_irr is None:
            return None
        return self.quantity * self.unit_cost_irr

    @property
    def is_negative(self) -> bool:
        return self.quantity < 0


@dataclass(frozen=True, slots=True)
class DaysOfCover:
    """Days-of-cover for one material — "when will it run out?".

    ``days_of_cover`` is None when there was no consumption in the
    window (nothing to project from — the UI shows "—", never a fake
    number). Negative when stock is already negative.
    """

    material_id: str
    quantity_on_hand: Decimal
    window_days: int
    consumed_in_window: Decimal
    avg_daily_consumption: Decimal
    days_of_cover: float | None
    last_consumption_date: FiscalDate | None = None

    @property
    def is_out_of_stock(self) -> bool:
        return self.quantity_on_hand <= 0

    def is_low(self, alert_days: int) -> bool:
        """True when the cover is at or below the alert threshold (or out of stock)."""
        if self.is_out_of_stock:
            return True
        return self.days_of_cover is not None and self.days_of_cover <= alert_days


@dataclass(frozen=True, slots=True)
class ConsumptionRecord:
    """One recorded packaging consumption for an order (dual-entry record).

    The Tryton stock move (packaging location → production location)
    is the accounting truth; this record is the operational trace:
    which order, which shipment, which analytic cost centre, and the
    cost at the moment of consumption. ``unit_cost_irr`` is the
    engine's average cost price at move time (may be fractional per
    unit); ``total_cost_irr`` is the whole-Rial total, HALF_UP, with
    the rounding delta recorded — never silently rounded.
    """

    id: str
    tenant_id: str
    company_id: str
    material_id: str
    order_ref: str
    shipment_ref: str
    quantity: Decimal
    unit_cost_irr: Decimal
    total_cost_irr: int
    rounding_delta_irr: int
    idempotency_key: str
    source: ConsumptionSource = ConsumptionSource.MANUAL
    analytic_account_engine_id: int | None = None
    tryton_move_id: int | None = None
    consumed_on: FiscalDate | None = None
    created_at: datetime.datetime | None = None

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise InvalidConsumptionError(
                "Consumption quantity must be greater than zero.",
                details={"material_id": self.material_id, "order_ref": self.order_ref},
            )
        if not self.order_ref.strip():
            raise InvalidConsumptionError(
                "Consumption needs an order reference.",
                details={"material_id": self.material_id},
            )
        if self.unit_cost_irr < 0:
            raise InvalidConsumptionError(
                "Consumption unit cost must be non-negative.",
                details={"material_id": self.material_id},
            )


@dataclass(frozen=True, slots=True)
class ShipmentCostRecord:
    """The three shipment-cost figures for one order, kept separate.

    - ``invoiced_irr``: charged to the customer (``sale_shipment_cost``;
      0 when the method is ``none``).
    - ``actual_irr``: actually paid to the carrier (carrier invoice /
      contract; entered manually when the store does not send it).
    - ``absorbed_irr``: ``actual - invoiced`` — the part the margin
      absorbs. Negative means shipping earned margin; reported
      honestly, never clamped to zero.
    """

    id: str
    tenant_id: str
    company_id: str
    order_ref: str
    method: ShipmentCostMethod
    invoiced_irr: int
    actual_irr: int
    carrier_name: str | None = None
    tryton_sale_id: int | None = None
    tryton_shipment_id: int | None = None
    analytic_account_engine_id: int | None = None
    created_at: datetime.datetime | None = None

    def __post_init__(self) -> None:
        if self.invoiced_irr < 0 or self.actual_irr < 0:
            raise InvalidShipmentCostError(
                "Shipment cost figures must be non-negative.",
                details={"order_ref": self.order_ref},
            )
        if self.method is ShipmentCostMethod.NONE and self.invoiced_irr != 0:
            raise InvalidShipmentCostError(
                "Method 'none' absorbs the whole cost: invoiced must be 0.",
                details={"order_ref": self.order_ref},
            )

    @property
    def absorbed_irr(self) -> int:
        """Actual minus invoiced — the part absorbed into the margin."""
        return self.actual_irr - self.invoiced_irr


@dataclass(frozen=True, slots=True)
class CarrierRate:
    """A carrier rate used by the price calculator — user-entered config.

    No rate is ever hard-coded: rates change, so a rate change is a
    new row with a new ``effective_from`` (old quotes keep the rate
    they were computed with — the quote stores its inputs).
    """

    id: str
    tenant_id: str
    company_id: str
    carrier_name: str
    rate_irr: int
    basis: str = "per_shipment"
    tryton_carrier_id: int | None = None
    effective_from: FiscalDate | None = None
    is_active: bool = True
    created_at: datetime.datetime | None = None

    def __post_init__(self) -> None:
        if not self.carrier_name.strip():
            raise InvalidCarrierRateError(
                "A carrier rate needs a carrier name.",
                details={"carrier_rate_id": self.id},
            )
        if self.rate_irr < 0:
            raise InvalidCarrierRateError(
                "A carrier rate must be non-negative.",
                details={"carrier_rate_id": self.id},
            )


@dataclass(frozen=True, slots=True)
class PriceQuoteLine:
    """One priced line of an approximate quote (packaging material)."""

    material_id: str
    material_name: str
    quantity: Decimal
    unit_cost_irr: Decimal
    line_cost_irr: int
    stale_price: bool = False


@dataclass(frozen=True, slots=True)
class PriceQuote:
    """An approximate price quote — labelled approximate, never auto-applied.

    The quote is computed HesabYar-side from current costs and
    user-entered rates; it NEVER writes to an invoice (no invoice
    interaction exists on this path at all). ``stale_price`` lines
    (material out of stock → last purchase price) are flagged, not
    hidden.
    """

    goods_cost_irr: int
    packaging_cost_irr: int
    shipment_cost_irr: int
    margin_irr: int
    total_irr: int
    approximate: bool = True
    lines: tuple[PriceQuoteLine, ...] = ()
    warnings: tuple[str, ...] = ()
    margin_percent: Decimal | None = None
    carrier_name: str | None = None

    def __post_init__(self) -> None:
        if not self.approximate:
            raise InvalidPriceQuoteError("A price quote is always approximate.")
        if self.total_irr != (
            self.goods_cost_irr + self.packaging_cost_irr + self.shipment_cost_irr + self.margin_irr
        ):
            raise InvalidPriceQuoteError(
                "Quote total must equal goods + packaging + shipment + margin.",
            )


@dataclass(frozen=True, slots=True)
class CountLine:
    """One line of a materials count: expected (engine) vs counted (human)."""

    material_id: str
    material_name: str
    expected_quantity: Decimal
    counted_quantity: Decimal | None = None
    tryton_line_id: int | None = None

    @property
    def variance(self) -> Decimal | None:
        """Counted minus expected (None until the line is counted)."""
        if self.counted_quantity is None:
            return None
        return self.counted_quantity - self.expected_quantity


@dataclass(frozen=True, slots=True)
class InventoryCount:
    """A periodic materials count (``stock.inventory`` in the engine).

    Confirming a count is the human approval step: the engine creates
    the adjustment moves (to/from the lost-and-found location) only
    on confirm, and confirm is always an explicit operator action —
    nothing auto-confirms a count.
    """

    id: str
    tenant_id: str
    company_id: str
    tryton_inventory_id: int
    location_engine_id: int
    location_name: str
    count_date: FiscalDate
    state: CountState = CountState.DRAFT
    counted_by: str | None = None
    lines: tuple[CountLine, ...] = ()
    created_at: datetime.datetime | None = None


@dataclass(frozen=True, slots=True)
class OrderCostCenter:
    """One analytic account (cost centre) per order.

    The analytic account lives in Tryton (``analytic_account.account``,
    type ``normal`` under the installation's orders root); this row is
    the HesabYar-side link from the store order reference to it.
    Consumption and shipment-cost records carry the same analytic
    account id — that shared tag is what makes "full cost of each
    order" one report.
    """

    id: str
    tenant_id: str
    company_id: str
    order_ref: str
    analytic_account_engine_id: int
    created_at: datetime.datetime | None = None


@dataclass(frozen=True, slots=True)
class OrderCostReport:
    """Full cost of one order, from one analytic cost centre.

    - ``goods_cost_irr``: total debits on the order's analytic
      account (costs tagged to the order via ``analytic_sale`` /
      ``analytic_invoice``) — 0 with ``analytic_line_count == 0``
      when nothing is tagged yet (reported honestly, not estimated).
    - ``revenue_irr``: total credits on the same account (revenue
      tagged to the order).
    - ``packaging_cost_irr``: HesabYar consumption records for the order.
    - ``shipment_*``: the three shipment figures for the order.
    """

    order_ref: str
    analytic_account_engine_id: int | None
    goods_cost_irr: int
    packaging_cost_irr: int
    shipment_invoiced_irr: int
    shipment_actual_irr: int
    shipment_absorbed_irr: int
    revenue_irr: int = 0
    analytic_line_count: int = 0
    consumption_count: int = 0

    @property
    def total_cost_irr(self) -> int:
        """Goods + packaging + actual shipment cost (what the order really cost)."""
        return self.goods_cost_irr + self.packaging_cost_irr + self.shipment_actual_irr

    @property
    def margin_irr(self) -> int:
        """Revenue minus total cost (negative when the order lost money)."""
        return self.revenue_irr - self.total_cost_irr


@dataclass(frozen=True, slots=True)
class MaterialDetail:
    """One material with its live stock, reorder point and coverage.

    A read model assembled by the query service from the repository
    (catalog row), the engine (stock, order point) and the coverage
    computation (consumption moves). Nothing here is stored. The
    create command builds it too, from facts known by construction
    (zero stock, no consumption) instead of re-reading the engine.
    """

    material: PackagingMaterial
    stock: MaterialStock | None
    reorder_point: ReorderPoint | None
    coverage: DaysOfCover | None

    @property
    def is_below_min(self) -> bool:
        """True when live stock is below the reorder point's minimum."""
        if self.stock is None or self.reorder_point is None:
            return False
        return self.stock.quantity < self.reorder_point.min_quantity


@dataclass(frozen=True, slots=True)
class PackagingSettings:
    """Per-company packaging configuration (design §هـ.۱.۳ — settings, not secrets).

    Location/category/root ids are engine ids, set once at
    provisioning (the setup script prints them; the admin page
    stores them). The service falls back to code lookup
    (``PACK`` / ``PACK-CONSUME`` / ``WH``) when an id is unset.
    """

    tenant_id: str
    company_id: str
    cost_method: CostMethod = CostMethod.AVERAGE
    shipment_cost_method: ShipmentCostMethod = ShipmentCostMethod.ON_ORDER
    days_of_cover_window_days: int = DEFAULT_COVERAGE_WINDOW_DAYS
    low_stock_alert_days: int = DEFAULT_LOW_STOCK_ALERT_DAYS
    packaging_location_id: int | None = None
    production_location_id: int | None = None
    warehouse_id: int | None = None
    packaging_category_id: int | None = None
    analytic_root_id: int | None = None
    updated_at: datetime.datetime | None = None

    def __post_init__(self) -> None:
        if self.days_of_cover_window_days < 1:
            raise InvalidMaterialError(
                "Days-of-cover window must be at least 1 day.",
                details={"company_id": self.company_id},
            )
        if self.low_stock_alert_days < 0:
            raise InvalidMaterialError(
                "Low-stock alert threshold must be non-negative.",
                details={"company_id": self.company_id},
            )


@dataclass
class SupplyCheckReport:
    """Result of one supply-check run (Procrastinate job / ops endpoint).

    The job READS the supply wizard's results (new ``purchase.request``
    rows since the last watermark) and the live days-of-cover, and
    files ``packaging_reorder`` review items — it never runs the
    wizard itself (the wizard runs inside Tryton via ``ir.cron`` +
    ``trytond-cron``) and never posts anything.
    """

    company_id: str
    checked_at: datetime.datetime
    materials_checked: int = 0
    purchase_requests_seen: int = 0
    purchase_requests_new: int = 0
    items_filed: int = 0
    items_skipped_open: int = 0
    low_stock_materials: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
