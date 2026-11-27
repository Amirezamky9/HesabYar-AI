"""Unit tests for the packaging & shipping services (US-515, stream K1).

No database, no live Tryton: the engine port and the repository are
faked in memory, and the clock is fixed, so coverage/quote math is
deterministic. The REST tests (``tests/interfaces/test_rest_packaging.py``)
cover the real repository + HTTP layer against PostgreSQL, and the
contract tests (``tests/contracts/test_tryton_packaging.py``) cover
the adapter against the live engine.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Sequence
from decimal import Decimal

import pytest

from hesabyar.application.commands.packaging import PackagingCommandService
from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.application.queries.packaging import PackagingQueryService, QuotePackagingItem
from hesabyar.domain.accounting.commands import (
    CreateAnalyticAccount,
    CreateConsumptionMove,
    CreateInventory,
    CreatePackagingProduct,
    UpdateInventoryLine,
    UpsertOrderPoint,
)
from hesabyar.domain.accounting.refs import (
    AnalyticAccountRef,
    CompanyRef,
    InventoryLineRef,
    InventoryRef,
    OrderPointRef,
    OutShipmentRef,
    ProductRef,
    PurchaseRequestRef,
    SaleRef,
    StockLocationRef,
    StockMoveRef,
)
from hesabyar.domain.accounting.value_objects import (
    AnalyticAccountView,
    AnalyticLineView,
    InventoryLineView,
    InventoryView,
    OrderPointView,
    PackagingProductView,
    PurchaseRequestView,
    SaleShipmentCostView,
    StockLocationView,
    StockMoveView,
)
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.errors import (
    HesabYarError,
    ProductNotFoundError,
    StockLocationNotFoundError,
)
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.domain.packaging import coverage as coverage_calc
from hesabyar.domain.packaging.calculator import (
    PackagingQuoteInput,
    PriceQuoteRequest,
    compute_price_quote,
)
from hesabyar.domain.packaging.errors import (
    CarrierRateNotFoundError,
    CostCenterExistsError,
    CountNotFoundError,
    FifoUnavailableError,
    InvalidConsumptionError,
    InvalidCountError,
    InvalidMaterialError,
    InvalidPriceQuoteError,
    InvalidShipmentCostError,
    MaterialNotFoundError,
    PackagingSetupError,
)
from hesabyar.domain.packaging.models import (
    CarrierRate,
    ConsumptionRecord,
    CostMethod,
    CountState,
    InventoryCount,
    OrderCostCenter,
    PackagingMaterial,
    PackagingSettings,
    ShipmentCostMethod,
    ShipmentCostRecord,
    consumption_idempotency_key,
)
from hesabyar.domain.packaging.ports import PackagingRepositoryPort
from hesabyar.domain.review_queue.models import (
    ReviewAuditEntry,
    ReviewItem,
    ReviewItemStatus,
    ReviewItemType,
    ReviewSeverity,
    SlaPolicy,
)
from hesabyar.domain.review_queue.ports import (
    ReviewAuditPort,
    ReviewItemFilters,
    ReviewQueuePort,
)

NOW = datetime.datetime(2026, 11, 18, 12, 0, tzinfo=datetime.UTC)
TODAY = FiscalDate.from_gregorian(datetime.date(2026, 11, 18))
TENANT = "tenant-515"
COMPANY_ID = "company-515"
COMPANY = CompanyRef(1)
CATEGORY_ID = 900
WAREHOUSE_ID = 5
PACK_ID = 101
PRODUCE_ID = 102
ROOT_ID = 700


def _settings(**overrides) -> PackagingSettings:
    base = {
        "tenant_id": TENANT,
        "company_id": COMPANY_ID,
        "packaging_location_id": PACK_ID,
        "production_location_id": PRODUCE_ID,
        "warehouse_id": WAREHOUSE_ID,
        "packaging_category_id": CATEGORY_ID,
        "analytic_root_id": ROOT_ID,
    }
    base.update(overrides)
    return PackagingSettings(**base)


def _material(**overrides) -> PackagingMaterial:
    base = {
        "id": str(uuid.uuid4()),
        "tenant_id": TENANT,
        "company_id": COMPANY_ID,
        "tryton_product_id": 1001,
        "tryton_template_id": 2001,
        "name_fa": "کارتن مقوایی",
    }
    base.update(overrides)
    return PackagingMaterial(**base)


def _location(engine_id: int, name: str, location_type: str, code: str | None = None) -> StockLocationView:
    return StockLocationView(
        ref=StockLocationRef(engine_id),
        name=name,
        location_type=location_type,
        code=code,
    )


class InMemoryPackagingRepository(PackagingRepositoryPort):
    """In-memory packaging repository fake."""

    def __init__(self) -> None:
        self.materials: dict[str, PackagingMaterial] = {}
        self.consumptions: dict[str, ConsumptionRecord] = {}
        self.shipment_costs: dict[str, ShipmentCostRecord] = {}
        self.rates: dict[str, CarrierRate] = {}
        self.counts: dict[str, InventoryCount] = {}
        self.centers: dict[str, OrderCostCenter] = {}
        self.settings: dict[tuple[str, str], PackagingSettings] = {}
        self.watermarks: dict[tuple[str, str], int] = {}

    def list_materials(self, tenant_id: str, company_id: str) -> Sequence[PackagingMaterial]:
        return sorted(
            (m for m in self.materials.values() if m.tenant_id == tenant_id and m.company_id == company_id),
            key=lambda m: m.name_fa,
        )

    def get_material(self, material_id: str, tenant_id: str) -> PackagingMaterial | None:
        material = self.materials.get(material_id)
        return material if material is not None and material.tenant_id == tenant_id else None

    def get_material_by_product(
        self, tenant_id: str, company_id: str, tryton_product_id: int
    ) -> PackagingMaterial | None:
        for material in self.materials.values():
            if (
                material.tenant_id == tenant_id
                and material.company_id == company_id
                and material.tryton_product_id == tryton_product_id
            ):
                return material
        return None

    def save_material(self, material: PackagingMaterial) -> PackagingMaterial:
        self.materials[material.id] = material
        return material

    def update_material(self, material: PackagingMaterial) -> PackagingMaterial:
        self.materials[material.id] = material
        return material

    def get_consumption_by_key(
        self, tenant_id: str, idempotency_key: str
    ) -> ConsumptionRecord | None:
        for record in self.consumptions.values():
            if record.tenant_id == tenant_id and record.idempotency_key == idempotency_key:
                return record
        return None

    def save_consumption(self, record: ConsumptionRecord) -> ConsumptionRecord:
        self.consumptions[record.id] = record
        return record

    def update_consumption(self, record: ConsumptionRecord) -> ConsumptionRecord:
        self.consumptions[record.id] = record
        return record

    def delete_consumption(self, record_id: str, tenant_id: str) -> None:
        record = self.consumptions.get(record_id)
        if record is not None and record.tenant_id == tenant_id:
            del self.consumptions[record_id]

    def list_consumptions(
        self,
        tenant_id: str,
        company_id: str,
        *,
        order_ref: str | None = None,
        material_id: str | None = None,
    ) -> Sequence[ConsumptionRecord]:
        out = [
            r
            for r in self.consumptions.values()
            if r.tenant_id == tenant_id and r.company_id == company_id
        ]
        if order_ref is not None:
            out = [r for r in out if r.order_ref == order_ref]
        if material_id is not None:
            out = [r for r in out if r.material_id == material_id]
        return out

    def save_shipment_cost(self, record: ShipmentCostRecord) -> ShipmentCostRecord:
        self.shipment_costs[record.id] = record
        return record

    def list_shipment_costs(
        self, tenant_id: str, company_id: str, *, order_ref: str | None = None
    ) -> Sequence[ShipmentCostRecord]:
        out = [
            r
            for r in self.shipment_costs.values()
            if r.tenant_id == tenant_id and r.company_id == company_id
        ]
        if order_ref is not None:
            out = [r for r in out if r.order_ref == order_ref]
        return out

    def list_carrier_rates(
        self, tenant_id: str, company_id: str, *, active_only: bool = False
    ) -> Sequence[CarrierRate]:
        out = [
            r
            for r in self.rates.values()
            if r.tenant_id == tenant_id and r.company_id == company_id
        ]
        if active_only:
            out = [r for r in out if r.is_active]
        return out

    def get_carrier_rate(self, rate_id: str, tenant_id: str) -> CarrierRate | None:
        rate = self.rates.get(rate_id)
        return rate if rate is not None and rate.tenant_id == tenant_id else None

    def save_carrier_rate(self, rate: CarrierRate) -> CarrierRate:
        self.rates[rate.id] = rate
        return rate

    def update_carrier_rate(self, rate: CarrierRate) -> CarrierRate:
        self.rates[rate.id] = rate
        return rate

    def save_count(self, count: InventoryCount) -> InventoryCount:
        self.counts[count.id] = count
        return count

    def get_count(self, count_id: str, tenant_id: str) -> InventoryCount | None:
        count = self.counts.get(count_id)
        return count if count is not None and count.tenant_id == tenant_id else None

    def list_counts(self, tenant_id: str, company_id: str) -> Sequence[InventoryCount]:
        return [
            c
            for c in self.counts.values()
            if c.tenant_id == tenant_id and c.company_id == company_id
        ]

    def update_count(self, count: InventoryCount) -> InventoryCount:
        self.counts[count.id] = count
        return count

    def get_cost_center(
        self, tenant_id: str, company_id: str, order_ref: str
    ) -> OrderCostCenter | None:
        for center in self.centers.values():
            if (
                center.tenant_id == tenant_id
                and center.company_id == company_id
                and center.order_ref == order_ref
            ):
                return center
        return None

    def save_cost_center(self, center: OrderCostCenter) -> OrderCostCenter:
        self.centers[center.id] = center
        return center

    def get_settings(self, tenant_id: str, company_id: str) -> PackagingSettings | None:
        return self.settings.get((tenant_id, company_id))

    def save_settings(self, settings: PackagingSettings) -> PackagingSettings:
        self.settings[(settings.tenant_id, settings.company_id)] = settings
        return settings

    def list_configured_companies(self) -> Sequence[tuple[str, str]]:
        return sorted(self.settings.keys())

    def get_supply_watermark(self, tenant_id: str, company_id: str) -> int:
        return self.watermarks.get((tenant_id, company_id), 0)

    def save_supply_watermark(self, tenant_id: str, company_id: str, watermark: int) -> None:
        self.watermarks[(tenant_id, company_id)] = max(
            watermark, self.watermarks.get((tenant_id, company_id), 0)
        )


class FakePackagingEngine:
    """Duck-typed AccountingEnginePort stand-in for the packaging methods."""

    def __init__(self) -> None:
        self.locations: list[StockLocationView] = [
            _location(WAREHOUSE_ID, "انبار اصلی", "warehouse"),
            _location(PACK_ID, "مواد بسته‌بندی", "storage", "PACK"),
            _location(PRODUCE_ID, "مصرف بسته‌بندی", "production", "PACK-CONSUME"),
        ]
        self.products: dict[int, dict] = {}
        self.next_product_id = 1001
        self.next_template_id = 2001
        self.list_packaging_products_calls = 0
        self.get_packaging_product_calls = 0
        self.order_points: dict[tuple[int, int], OrderPointView] = {}
        self.next_order_point_id = 3001
        self.moves: list[StockMoveView] = []
        self.next_move_id = 4001
        self.fail_next_move: HesabYarError | None = None
        self.purchase_requests: list[PurchaseRequestView] = []
        self.analytic_accounts: dict[int, AnalyticAccountView] = {
            ROOT_ID: AnalyticAccountView(
                ref=AnalyticAccountRef(ROOT_ID),
                name="سفارش‌ها",
                code="ORDERS",
                account_type="root",
                company=COMPANY,
            )
        }
        self.next_analytic_id = ROOT_ID + 1
        self.analytic_lines: dict[int, list[AnalyticLineView]] = {}
        self.inventories: dict[int, InventoryView] = {}
        self.next_inventory_id = 5001
        self.next_inventory_line_id = 6001
        self.sale_shipment_costs: dict[int, list[SaleShipmentCostView]] = {}
        self.fifo_available = True
        self.move_cost_price: Decimal | None = Decimal("12000")

    # -- seeding -----------------------------------------------------------

    def seed_product(
        self,
        product_id: int,
        template_id: int,
        *,
        quantity: Decimal = Decimal(50),
        cost_price: Decimal | None = Decimal("12000"),
        cost_method: str = "average",
        name: str = "کارتن مقوایی",
    ) -> None:
        self.products[product_id] = {
            "template_id": template_id,
            "name": name,
            "cost_method": cost_method,
            "quantity": quantity,
            "cost_price": cost_price,
        }

    def seed_move(
        self,
        product_id: int,
        quantity: Decimal,
        effective: FiscalDate,
        *,
        cost_price: Decimal | None = Decimal("12000"),
    ) -> None:
        move_id = self.next_move_id
        self.next_move_id += 1
        self.moves.append(
            StockMoveView(
                ref=StockMoveRef(move_id),
                product=ProductRef(product_id),
                quantity=quantity,
                from_location=StockLocationRef(PACK_ID),
                to_location=StockLocationRef(PRODUCE_ID),
                state="done",
                company=COMPANY,
                planned_date=effective,
                effective_date=effective,
                cost_price=cost_price,
            )
        )

    def seed_purchase_request(self, request_id: int, product_id: int, quantity: Decimal) -> None:
        self.purchase_requests.append(
            PurchaseRequestView(
                ref=PurchaseRequestRef(request_id),
                product=ProductRef(product_id),
                quantity=quantity,
                state="draft",
                company=COMPANY,
            )
        )

    # -- locations ---------------------------------------------------------

    def list_stock_locations(self, company: CompanyRef) -> Sequence[StockLocationView]:
        return list(self.locations)

    def get_stock_location(self, location: StockLocationRef) -> StockLocationView:
        for loc in self.locations:
            if loc.ref.engine_id == location.engine_id:
                return loc
        raise StockLocationNotFoundError(
            f"Stock location {location.engine_id} does not exist.",
            details={"location_id": location.engine_id},
        )

    # -- products ----------------------------------------------------------

    def create_packaging_product(self, command: CreatePackagingProduct) -> ProductRef:
        if command.cost_method == "fifo" and not self.fifo_available:
            raise FifoUnavailableError(
                "FIFO costing needs the product_cost_fifo module, which is not activated.",
                details={"module": "product_cost_fifo"},
            )
        product_id = self.next_product_id
        template_id = self.next_template_id
        self.next_product_id += 1
        self.next_template_id += 1
        self.products[product_id] = {
            "template_id": template_id,
            "name": command.name,
            "cost_method": command.cost_method,
            "quantity": Decimal(0),
            "cost_price": None,
        }
        return ProductRef(product_id)

    def get_packaging_product(
        self,
        product: ProductRef,
        company: CompanyRef,
        location: StockLocationRef | None = None,
    ) -> PackagingProductView:
        self.get_packaging_product_calls += 1
        view = self._view_for_product(product, location)
        if view is None:
            raise ProductNotFoundError(
                f"Product {product.engine_id} does not exist.",
                details={"product_id": product.engine_id},
            )
        return view

    def _view_for_product(
        self,
        product: ProductRef,
        location: StockLocationRef | None,
    ) -> PackagingProductView | None:
        data = self.products.get(product.engine_id)
        if data is None:
            return None
        return PackagingProductView(
            ref=product,
            template_id=data["template_id"],
            name=data["name"],
            code=None,
            product_type="goods",
            consumable=False,
            purchasable=True,
            cost_method=data["cost_method"],
            default_uom_name="Unit",
            quantity=data["quantity"] if location is not None else None,
            cost_price=data["cost_price"],
        )

    def list_packaging_products(
        self,
        company: CompanyRef,
        products: Sequence[ProductRef],
        location: StockLocationRef | None = None,
    ) -> Sequence[PackagingProductView]:
        # Batch read: missing products are skipped (port contract).
        self.list_packaging_products_calls += 1
        return [
            view
            for product in products
            if (view := self._view_for_product(product, location)) is not None
        ]

    def get_product_cost_price(
        self, product: ProductRef, company: CompanyRef
    ) -> Decimal | None:
        data = self.products.get(product.engine_id)
        if data is None:
            raise ProductNotFoundError(
                f"Product {product.engine_id} does not exist.",
                details={"product_id": product.engine_id},
            )
        return data["cost_price"]

    # -- order points ------------------------------------------------------

    def upsert_order_point(self, command: UpsertOrderPoint) -> OrderPointView:
        key = (command.product.engine_id, command.warehouse.engine_id)
        existing = self.order_points.get(key)
        point_id = existing.ref.engine_id if existing is not None else self.next_order_point_id
        if existing is None:
            self.next_order_point_id += 1
        view = OrderPointView(
            ref=OrderPointRef(point_id),
            product=command.product,
            warehouse=command.warehouse,
            point_type="purchase",
            min_quantity=command.min_quantity,
            target_quantity=command.target_quantity,
        )
        self.order_points[key] = view
        return view

    def get_order_point(
        self,
        product: ProductRef,
        warehouse: StockLocationRef,
        company: CompanyRef,
    ) -> OrderPointView | None:
        return self.order_points.get((product.engine_id, warehouse.engine_id))

    def list_order_points(self, company: CompanyRef) -> Sequence[OrderPointView]:
        return list(self.order_points.values())

    # -- moves -------------------------------------------------------------

    def create_consumption_move(self, command: CreateConsumptionMove) -> StockMoveView:
        if self.fail_next_move is not None:
            error, self.fail_next_move = self.fail_next_move, None
            raise error
        move_id = self.next_move_id
        self.next_move_id += 1
        view = StockMoveView(
            ref=StockMoveRef(move_id),
            product=command.product,
            quantity=command.quantity,
            from_location=command.from_location,
            to_location=command.to_location,
            state="done",
            company=command.company,
            planned_date=command.date,
            effective_date=command.date,
            cost_price=self.move_cost_price,
        )
        self.moves.append(view)
        data = self.products.get(command.product.engine_id)
        if data is not None:
            data["quantity"] = data["quantity"] - command.quantity
        return view

    def list_consumption_moves(
        self,
        company: CompanyRef,
        from_location: StockLocationRef,
        to_location: StockLocationRef,
        date_from: FiscalDate,
        date_to: FiscalDate,
        product: ProductRef | None = None,
    ) -> Sequence[StockMoveView]:
        out = [
            m
            for m in self.moves
            if m.from_location.engine_id == from_location.engine_id
            and m.to_location.engine_id == to_location.engine_id
            and m.effective_date is not None
            and date_from <= m.effective_date <= date_to
        ]
        if product is not None:
            out = [m for m in out if m.product.engine_id == product.engine_id]
        return out

    # -- purchase requests -------------------------------------------------

    def list_purchase_requests(
        self,
        company: CompanyRef,
        state: str | None = None,
        after_id: int | None = None,
    ) -> Sequence[PurchaseRequestView]:
        out = list(self.purchase_requests)
        if state is not None:
            out = [r for r in out if r.state == state]
        if after_id is not None:
            out = [r for r in out if r.ref.engine_id > after_id]
        return out

    # -- analytic ----------------------------------------------------------

    def create_analytic_account(self, command: CreateAnalyticAccount) -> AnalyticAccountRef:
        account_id = self.next_analytic_id
        self.next_analytic_id += 1
        self.analytic_accounts[account_id] = AnalyticAccountView(
            ref=AnalyticAccountRef(account_id),
            name=command.name,
            code=command.code,
            account_type=command.account_type,
            company=command.company,
        )
        return AnalyticAccountRef(account_id)

    def get_analytic_account(self, account: AnalyticAccountRef) -> AnalyticAccountView:
        view = self.analytic_accounts.get(account.engine_id)
        if view is None:
            raise HesabYarError(
                f"Analytic account {account.engine_id} does not exist.",
                code="LEDGER_ANALYTIC_ACCOUNT_NOT_FOUND",
            )
        return view

    def list_analytic_lines(self, account: AnalyticAccountRef) -> Sequence[AnalyticLineView]:
        return list(self.analytic_lines.get(account.engine_id, []))

    # -- inventories -------------------------------------------------------

    def create_inventory(self, command: CreateInventory) -> InventoryRef:
        inventory_id = self.next_inventory_id
        self.next_inventory_id += 1
        location_name = next(
            (loc.name for loc in self.locations if loc.ref.engine_id == command.location.engine_id),
            "",
        )
        self.inventories[inventory_id] = InventoryView(
            ref=InventoryRef(inventory_id),
            number=None,
            location=command.location,
            location_name=location_name,
            date=command.date,
            state="draft",
            company=command.company,
            lines=(),
        )
        return InventoryRef(inventory_id)

    def get_inventory(self, inventory: InventoryRef) -> InventoryView:
        view = self.inventories.get(inventory.engine_id)
        if view is None:
            raise HesabYarError(
                f"Inventory {inventory.engine_id} does not exist.",
                code="LEDGER_INVENTORY_NOT_FOUND",
            )
        return view

    def complete_inventory_lines(self, inventory: InventoryRef) -> InventoryView:
        view = self.get_inventory(inventory)
        lines = tuple(
            InventoryLineView(
                ref=InventoryLineRef(self.next_inventory_line_id + index),
                product=ProductRef(product_id),
                product_name=data["name"],
                expected_quantity=data["quantity"],
                quantity=None,
            )
            for index, (product_id, data) in enumerate(sorted(self.products.items()))
        )
        self.next_inventory_line_id += len(lines)
        completed = InventoryView(
            ref=view.ref,
            number=view.number,
            location=view.location,
            location_name=view.location_name,
            date=view.date,
            state=view.state,
            company=view.company,
            lines=lines,
        )
        self.inventories[inventory.engine_id] = completed
        return completed

    def update_inventory_line(self, command: UpdateInventoryLine) -> InventoryView:
        for inventory_id, view in self.inventories.items():
            for line in view.lines:
                if line.ref.engine_id == command.line.engine_id:
                    updated_lines = tuple(
                        InventoryLineView(
                            ref=item.ref,
                            product=item.product,
                            product_name=item.product_name,
                            expected_quantity=item.expected_quantity,
                            quantity=(
                                command.quantity
                                if item.ref.engine_id == command.line.engine_id
                                else item.quantity
                            ),
                        )
                        for item in view.lines
                    )
                    updated = InventoryView(
                        ref=view.ref,
                        number=view.number,
                        location=view.location,
                        location_name=view.location_name,
                        date=view.date,
                        state=view.state,
                        company=view.company,
                        lines=updated_lines,
                    )
                    self.inventories[inventory_id] = updated
                    return updated
        raise HesabYarError(
            f"Inventory line {command.line.engine_id} does not exist.",
            code="LEDGER_INVALID_INVENTORY",
        )

    def confirm_inventory(self, inventory: InventoryRef) -> InventoryView:
        view = self.get_inventory(inventory)
        confirmed = InventoryView(
            ref=view.ref,
            number=view.number,
            location=view.location,
            location_name=view.location_name,
            date=view.date,
            state="done",
            company=view.company,
            lines=view.lines,
        )
        self.inventories[inventory.engine_id] = confirmed
        return confirmed

    def cancel_inventory(self, inventory: InventoryRef) -> InventoryView:
        view = self.get_inventory(inventory)
        cancelled = InventoryView(
            ref=view.ref,
            number=view.number,
            location=view.location,
            location_name=view.location_name,
            date=view.date,
            state="cancelled",
            company=view.company,
            lines=view.lines,
        )
        self.inventories[inventory.engine_id] = cancelled
        return cancelled

    # -- shipment costs ----------------------------------------------------

    def get_sale_shipment_costs(self, sale: SaleRef) -> Sequence[SaleShipmentCostView]:
        return list(self.sale_shipment_costs.get(sale.engine_id, []))


class InMemoryReviewQueue(ReviewQueuePort):
    """In-memory review-queue fake (items only; the supply check lists by type+company)."""

    def __init__(self) -> None:
        self.items: dict[str, ReviewItem] = {}
        self.policies: dict[ReviewItemType, SlaPolicy] = {}

    def list_items(self, tenant_id: str, filters: ReviewItemFilters) -> Sequence[ReviewItem]:
        out = [item for item in self.items.values() if item.tenant_id == tenant_id]
        if filters.item_type is not None:
            out = [item for item in out if item.item_type == filters.item_type]
        if filters.status is not None:
            out = [item for item in out if item.status == filters.status]
        if filters.company_id is not None:
            out = [item for item in out if item.company_id == filters.company_id]
        return out

    def get_item(self, item_id: str, tenant_id: str) -> ReviewItem | None:
        item = self.items.get(item_id)
        return item if item is not None and item.tenant_id == tenant_id else None

    def save_item(self, item: ReviewItem) -> ReviewItem:
        self.items[item.id] = item
        return item

    def update_item(self, item: ReviewItem) -> ReviewItem:
        self.items[item.id] = item
        return item

    def list_policies(self, tenant_id: str) -> Sequence[SlaPolicy]:
        return list(self.policies.values())

    def upsert_policy(self, tenant_id: str, policy: SlaPolicy) -> SlaPolicy:
        self.policies[policy.item_type] = policy
        return policy


class InMemoryReviewAudit(ReviewAuditPort):
    """In-memory audit fake: append-only, oldest first."""

    def __init__(self) -> None:
        self.entries: list[ReviewAuditEntry] = []

    def record(self, entry: ReviewAuditEntry) -> ReviewAuditEntry:
        self.entries.append(entry)
        return entry

    def list_for_item(self, item_id: str, tenant_id: str) -> Sequence[ReviewAuditEntry]:
        return [entry for entry in self.entries if entry.item_id == item_id]


def _services(
    engine: FakePackagingEngine | None = None,
    repo: InMemoryPackagingRepository | None = None,
    review_queue: InMemoryReviewQueue | None = None,
) -> tuple[
    PackagingCommandService,
    PackagingQueryService,
    FakePackagingEngine,
    InMemoryPackagingRepository,
    InMemoryReviewQueue,
]:
    engine = engine or FakePackagingEngine()
    repo = repo or InMemoryPackagingRepository()
    review_queue = review_queue or InMemoryReviewQueue()
    audit = InMemoryReviewAudit()
    review_commands = ReviewQueueCommandService(review_queue, audit, now=NOW)
    commands = PackagingCommandService(
        engine,  # type: ignore[arg-type]
        repo,
        review_queue,
        review_commands,
        now=NOW,
        today=TODAY,
    )
    queries = PackagingQueryService(engine, repo, today=TODAY)  # type: ignore[arg-type]
    return commands, queries, engine, repo, review_queue


def _seed_configured_company(
    engine: FakePackagingEngine, repo: InMemoryPackagingRepository, **settings_overrides
) -> PackagingMaterial:
    """Save full settings + one material + its engine product; return the material."""
    repo.save_settings(_settings(**settings_overrides))
    material = _material()
    repo.save_material(material)
    engine.seed_product(material.tryton_product_id, material.tryton_template_id)
    return material


# ---------------------------------------------------------------------------
# Domain: coverage
# ---------------------------------------------------------------------------


def test_coverage_divides_by_full_window():
    cover = coverage_calc.compute_days_of_cover(
        material_id="m1",
        quantity_on_hand=Decimal(60),
        consumed_in_window=Decimal(30),
        window_days=30,
    )
    assert cover.avg_daily_consumption == Decimal(1)
    assert cover.days_of_cover == pytest.approx(60.0)


def test_coverage_none_when_no_consumption():
    cover = coverage_calc.compute_days_of_cover(
        material_id="m1",
        quantity_on_hand=Decimal(60),
        consumed_in_window=Decimal(0),
        window_days=30,
    )
    assert cover.avg_daily_consumption == Decimal(0)
    assert cover.days_of_cover is None


def test_coverage_negative_when_stock_negative():
    cover = coverage_calc.compute_days_of_cover(
        material_id="m1",
        quantity_on_hand=Decimal(-5),
        consumed_in_window=Decimal(30),
        window_days=30,
    )
    assert cover.days_of_cover == pytest.approx(-5.0)
    assert cover.is_out_of_stock is True


def test_coverage_low_threshold():
    cover = coverage_calc.compute_days_of_cover(
        material_id="m1",
        quantity_on_hand=Decimal(5),
        consumed_in_window=Decimal(30),
        window_days=30,
    )
    assert cover.is_low(7) is True
    assert cover.is_low(3) is False


# ---------------------------------------------------------------------------
# Domain: price calculator
# ---------------------------------------------------------------------------


def test_quote_totals_goods_packaging_shipment_margin():
    quote = compute_price_quote(
        PriceQuoteRequest(
            goods_cost_irr=100_000,
            packaging=(
                PackagingQuoteInput(
                    material_id="m1",
                    material_name="کارتن",
                    quantity=Decimal(2),
                    unit_cost_irr=Decimal("12000"),
                    quantity_on_hand=Decimal(50),
                    stock_known=True,
                ),
            ),
            shipment_cost_irr=50_000,
            margin_percent=Decimal(10),
        )
    )
    assert quote.approximate is True
    assert quote.packaging_cost_irr == 24_000
    assert quote.margin_irr == 17_400  # 10% of 174,000
    assert quote.total_irr == 191_400


def test_quote_margin_fixed_and_percent_are_exclusive():
    with pytest.raises(InvalidPriceQuoteError):
        compute_price_quote(
            PriceQuoteRequest(
                goods_cost_irr=100_000,
                margin_percent=Decimal(10),
                margin_fixed_irr=5_000,
            )
        )


def test_quote_stale_price_warning_when_out_of_stock():
    quote = compute_price_quote(
        PriceQuoteRequest(
            goods_cost_irr=0,
            packaging=(
                PackagingQuoteInput(
                    material_id="m1",
                    material_name="کارتن",
                    quantity=Decimal(1),
                    unit_cost_irr=Decimal("12000"),
                    quantity_on_hand=Decimal(0),
                    stock_known=True,
                ),
            ),
        )
    )
    assert quote.lines[0].stale_price is True
    assert quote.warnings  # the stale price is flagged, not hidden


def test_quote_unknown_stock_warning_when_no_location():
    quote = compute_price_quote(
        PriceQuoteRequest(
            goods_cost_irr=0,
            packaging=(
                PackagingQuoteInput(
                    material_id="m1",
                    material_name="کارتن",
                    quantity=Decimal(1),
                    unit_cost_irr=Decimal("12000"),
                    quantity_on_hand=Decimal(0),
                    stock_known=False,
                ),
            ),
        )
    )
    assert quote.lines[0].stale_price is False
    assert quote.warnings  # unknown stock is flagged too


# ---------------------------------------------------------------------------
# Domain: model validation
# ---------------------------------------------------------------------------


def test_material_requires_name_and_engine_ids():
    with pytest.raises(InvalidMaterialError):
        _material(name_fa="  ")
    with pytest.raises(InvalidMaterialError):
        _material(tryton_product_id=0)


def test_consumption_record_rejects_non_positive_quantity():
    with pytest.raises(InvalidConsumptionError):
        ConsumptionRecord(
            id=str(uuid.uuid4()),
            tenant_id=TENANT,
            company_id=COMPANY_ID,
            material_id="m1",
            order_ref="ORD-1",
            shipment_ref="S1",
            quantity=Decimal(0),
            unit_cost_irr=Decimal(0),
            total_cost_irr=0,
            rounding_delta_irr=0,
            idempotency_key="pack:use:ORD-1:S1:m1",
        )


def test_shipment_record_rejects_invoiced_with_method_none():
    with pytest.raises(InvalidShipmentCostError):
        ShipmentCostRecord(
            id=str(uuid.uuid4()),
            tenant_id=TENANT,
            company_id=COMPANY_ID,
            order_ref="ORD-1",
            method=ShipmentCostMethod.NONE,
            invoiced_irr=1000,
            actual_irr=5000,
        )


def test_shipment_absorbed_is_actual_minus_invoiced():
    record = ShipmentCostRecord(
        id=str(uuid.uuid4()),
        tenant_id=TENANT,
        company_id=COMPANY_ID,
        order_ref="ORD-1",
        method=ShipmentCostMethod.ON_ORDER,
        invoiced_irr=30_000,
        actual_irr=50_000,
    )
    assert record.absorbed_irr == 20_000


def test_idempotency_key_shape():
    assert (
        consumption_idempotency_key("ORD-1", "S1", "m1") == "pack:use:ORD-1:S1:m1"
    )


# ---------------------------------------------------------------------------
# Services: settings
# ---------------------------------------------------------------------------


def test_settings_default_when_never_configured():
    _, queries, _, _, _ = _services()
    settings = queries.get_settings(TENANT, COMPANY_ID)
    assert settings.cost_method is CostMethod.AVERAGE
    assert settings.packaging_location_id is None


def test_save_settings_verifies_location_ids():
    commands, _, _, _, _ = _services()
    with pytest.raises(HesabYarError):
        commands.save_settings(
            tenant_id=TENANT,
            company_id=COMPANY_ID,
            company=COMPANY,
            packaging_location_id=99999,
        )


def test_save_settings_round_trip():
    commands, queries, _, _, _ = _services()
    saved = commands.save_settings(
        tenant_id=TENANT,
        company_id=COMPANY_ID,
        company=COMPANY,
        packaging_location_id=PACK_ID,
        warehouse_id=WAREHOUSE_ID,
        analytic_root_id=ROOT_ID,
    )
    assert saved.packaging_location_id == PACK_ID
    assert queries.get_settings(TENANT, COMPANY_ID).warehouse_id == WAREHOUSE_ID


# ---------------------------------------------------------------------------
# Services: materials
# ---------------------------------------------------------------------------


def test_create_material_requires_category():
    commands, _, _, repo, _ = _services()
    repo.save_settings(_settings(packaging_category_id=None))
    with pytest.raises(PackagingSetupError):
        commands.create_material(
            tenant_id=TENANT, company=COMPANY, company_id=COMPANY_ID, name_fa="چسب"
        )


def test_create_material_creates_engine_product_and_catalog_row():
    commands, _, engine, repo, _ = _services()
    repo.save_settings(_settings())
    detail = commands.create_material(
        tenant_id=TENANT,
        company=COMPANY,
        company_id=COMPANY_ID,
        name_fa="چسب نواری",
        code="TAPE",
    )
    material = detail.material
    assert material.tryton_product_id > 0
    assert material.tryton_template_id > 0
    assert material.uom_name == "Unit"
    assert repo.get_material(material.id, TENANT) is not None
    assert engine.products[material.tryton_product_id]["cost_method"] == "average"
    # The detail is built from known facts (zero stock, no consumption),
    # not re-read from the engine.
    assert detail.stock is not None
    assert detail.stock.quantity == Decimal(0)
    assert detail.reorder_point is None
    assert detail.coverage is not None
    assert detail.coverage.is_out_of_stock is True
    assert detail.coverage.days_of_cover is None


def test_create_material_fifo_without_module_fails():
    commands, _, engine, repo, _ = _services()
    repo.save_settings(_settings())
    engine.fifo_available = False
    with pytest.raises(FifoUnavailableError):
        commands.create_material(
            tenant_id=TENANT,
            company=COMPANY,
            company_id=COMPANY_ID,
            name_fa="چسب",
            cost_method=CostMethod.FIFO,
        )


def test_update_material_changes_catalog_only():
    commands, _, engine, repo, _ = _services()
    material = _seed_configured_company(engine, repo)
    updated = commands.update_material(
        tenant_id=TENANT, material_id=material.id, name_fa="کارتن بزرگ", is_active=False
    )
    assert updated.name_fa == "کارتن بزرگ"
    assert updated.is_active is False
    assert updated.tryton_product_id == material.tryton_product_id


def test_update_unknown_material_404():
    commands, _, _, _, _ = _services()
    with pytest.raises(MaterialNotFoundError):
        commands.update_material(tenant_id=TENANT, material_id="missing", name_fa="x")


def test_set_reorder_point_uses_settings_warehouse():
    commands, _, engine, repo, _ = _services()
    material = _seed_configured_company(engine, repo)
    point = commands.set_reorder_point(
        tenant_id=TENANT,
        company=COMPANY,
        company_id=COMPANY_ID,
        material_id=material.id,
        min_quantity=Decimal(10),
        target_quantity=Decimal(50),
    )
    assert point.warehouse_engine_id == WAREHOUSE_ID
    assert point.min_quantity == Decimal(10)
    assert point.target_quantity == Decimal(50)
    assert point.tryton_order_point_id is not None


def test_set_reorder_point_falls_back_to_single_warehouse():
    commands, _, engine, repo, _ = _services()
    material = _seed_configured_company(engine, repo, warehouse_id=None)
    point = commands.set_reorder_point(
        tenant_id=TENANT,
        company=COMPANY,
        company_id=COMPANY_ID,
        material_id=material.id,
        min_quantity=Decimal(10),
        target_quantity=Decimal(50),
    )
    assert point.warehouse_engine_id == WAREHOUSE_ID


def test_set_reorder_point_ambiguous_warehouses_setup_error():
    commands, _, engine, repo, _ = _services()
    material = _seed_configured_company(engine, repo, warehouse_id=None)
    engine.locations.append(_location(6, "انبار دوم", "warehouse"))
    with pytest.raises(PackagingSetupError):
        commands.set_reorder_point(
            tenant_id=TENANT,
            company=COMPANY,
            company_id=COMPANY_ID,
            material_id=material.id,
            min_quantity=Decimal(10),
            target_quantity=Decimal(50),
        )


# ---------------------------------------------------------------------------
# Services: consumption
# ---------------------------------------------------------------------------


def test_record_consumption_moves_stock_and_costs_it():
    commands, _, engine, repo, _ = _services()
    material = _seed_configured_company(engine, repo)
    record, created = commands.record_consumption(
        tenant_id=TENANT,
        company=COMPANY,
        company_id=COMPANY_ID,
        material_id=material.id,
        order_ref="ORD-1",
        shipment_ref="S1",
        quantity=Decimal(3),
    )
    assert created is True
    assert record.tryton_move_id is not None
    assert record.unit_cost_irr == Decimal("12000")
    assert record.total_cost_irr == 36_000
    assert record.rounding_delta_irr == 0
    assert engine.products[material.tryton_product_id]["quantity"] == Decimal(47)


def test_record_consumption_is_idempotent():
    commands, _, engine, repo, _ = _services()
    material = _seed_configured_company(engine, repo)
    first, created_first = commands.record_consumption(
        tenant_id=TENANT,
        company=COMPANY,
        company_id=COMPANY_ID,
        material_id=material.id,
        order_ref="ORD-1",
        shipment_ref="S1",
        quantity=Decimal(3),
    )
    second, created_second = commands.record_consumption(
        tenant_id=TENANT,
        company=COMPANY,
        company_id=COMPANY_ID,
        material_id=material.id,
        order_ref="ORD-1",
        shipment_ref="S1",
        quantity=Decimal(3),
    )
    assert created_first is True
    assert created_second is False
    assert second.id == first.id
    assert len(engine.moves) == 1  # no second move


def test_record_consumption_rolls_back_claim_when_move_fails():
    commands, _, engine, repo, _ = _services()
    material = _seed_configured_company(engine, repo)
    engine.fail_next_move = HesabYarError("engine down", code="ENGINE_UNAVAILABLE")
    with pytest.raises(HesabYarError):
        commands.record_consumption(
            tenant_id=TENANT,
            company=COMPANY,
            company_id=COMPANY_ID,
            material_id=material.id,
            order_ref="ORD-1",
            shipment_ref="S1",
            quantity=Decimal(3),
        )
    # The claim is rolled back: a clean retry is possible.
    assert repo.get_consumption_by_key(TENANT, "pack:use:ORD-1:S1:" + material.id) is None
    record, created = commands.record_consumption(
        tenant_id=TENANT,
        company=COMPANY,
        company_id=COMPANY_ID,
        material_id=material.id,
        order_ref="ORD-1",
        shipment_ref="S1",
        quantity=Decimal(3),
    )
    assert created is True


def test_record_consumption_rounds_half_up_and_records_delta():
    commands, _, engine, repo, _ = _services()
    material = _seed_configured_company(engine, repo)
    engine.move_cost_price = Decimal("3333.5")
    record, _ = commands.record_consumption(
        tenant_id=TENANT,
        company=COMPANY,
        company_id=COMPANY_ID,
        material_id=material.id,
        order_ref="ORD-1",
        shipment_ref="S1",
        quantity=Decimal(3),
    )
    # 3 x 3333.5 = 10000.5 -> HALF_UP 10001, delta +1 (never silently rounded).
    assert record.total_cost_irr == 10_001
    assert record.rounding_delta_irr == 1


def test_record_consumption_tags_cost_center_when_it_exists():
    commands, _, engine, repo, _ = _services()
    material = _seed_configured_company(engine, repo)
    center = commands.create_cost_center(
        tenant_id=TENANT, company=COMPANY, company_id=COMPANY_ID, order_ref="ORD-1"
    )
    record, _ = commands.record_consumption(
        tenant_id=TENANT,
        company=COMPANY,
        company_id=COMPANY_ID,
        material_id=material.id,
        order_ref="ORD-1",
        shipment_ref="S1",
        quantity=Decimal(1),
    )
    assert record.analytic_account_engine_id == center.analytic_account_engine_id


# ---------------------------------------------------------------------------
# Services: shipment costs
# ---------------------------------------------------------------------------


def test_record_shipment_cost_three_figures():
    commands, _, _, repo, _ = _services()
    repo.save_settings(_settings())
    record = commands.record_shipment_cost(
        tenant_id=TENANT,
        company=COMPANY,
        company_id=COMPANY_ID,
        order_ref="ORD-1",
        method=ShipmentCostMethod.ON_ORDER,
        actual_irr=50_000,
        invoiced_irr=30_000,
        carrier_name="پست",
    )
    assert record.invoiced_irr == 30_000
    assert record.actual_irr == 50_000
    assert record.absorbed_irr == 20_000


def test_record_shipment_cost_engine_figure_is_authoritative():
    commands, _, engine, repo, _ = _services()
    repo.save_settings(_settings())
    engine.sale_shipment_costs[77] = [
        SaleShipmentCostView(
            sale=SaleRef(77),
            shipment=OutShipmentRef(88),
            amount=MoneyIRR(40_000),
            currency_code="IRR",
        )
    ]
    record = commands.record_shipment_cost(
        tenant_id=TENANT,
        company=COMPANY,
        company_id=COMPANY_ID,
        order_ref="ORD-1",
        method=ShipmentCostMethod.ON_ORDER,
        actual_irr=50_000,
        tryton_sale_id=77,
    )
    assert record.invoiced_irr == 40_000


def test_record_shipment_cost_mismatch_is_422():
    commands, _, engine, repo, _ = _services()
    repo.save_settings(_settings())
    engine.sale_shipment_costs[77] = [
        SaleShipmentCostView(
            sale=SaleRef(77),
            shipment=OutShipmentRef(88),
            amount=MoneyIRR(40_000),
            currency_code="IRR",
        )
    ]
    with pytest.raises(InvalidShipmentCostError):
        commands.record_shipment_cost(
            tenant_id=TENANT,
            company=COMPANY,
            company_id=COMPANY_ID,
            order_ref="ORD-1",
            method=ShipmentCostMethod.ON_ORDER,
            actual_irr=50_000,
            invoiced_irr=30_000,
            tryton_sale_id=77,
        )


# ---------------------------------------------------------------------------
# Services: carrier rates
# ---------------------------------------------------------------------------


def test_carrier_rate_create_update_and_not_found():
    commands, _, _, _, _ = _services()
    rate = commands.create_carrier_rate(
        tenant_id=TENANT, company_id=COMPANY_ID, carrier_name="پست", rate_irr=45_000
    )
    assert rate.is_active is True
    updated = commands.update_carrier_rate(
        tenant_id=TENANT, rate_id=rate.id, rate_irr=50_000, is_active=False
    )
    assert updated.rate_irr == 50_000
    assert updated.is_active is False
    with pytest.raises(CarrierRateNotFoundError):
        commands.update_carrier_rate(tenant_id=TENANT, rate_id="missing", rate_irr=1)


# ---------------------------------------------------------------------------
# Services: cost centres
# ---------------------------------------------------------------------------


def test_create_cost_center_and_duplicate_conflict():
    commands, _, _, repo, _ = _services()
    repo.save_settings(_settings())
    center = commands.create_cost_center(
        tenant_id=TENANT, company=COMPANY, company_id=COMPANY_ID, order_ref="ORD-1"
    )
    assert center.analytic_account_engine_id > ROOT_ID
    with pytest.raises(CostCenterExistsError):
        commands.create_cost_center(
            tenant_id=TENANT, company=COMPANY, company_id=COMPANY_ID, order_ref="ORD-1"
        )


def test_create_cost_center_requires_analytic_root():
    commands, _, _, repo, _ = _services()
    repo.save_settings(_settings(analytic_root_id=None))
    with pytest.raises(PackagingSetupError):
        commands.create_cost_center(
            tenant_id=TENANT, company=COMPANY, company_id=COMPANY_ID, order_ref="ORD-1"
        )


# ---------------------------------------------------------------------------
# Services: counts
# ---------------------------------------------------------------------------


def test_count_lifecycle_create_line_confirm():
    commands, queries, engine, repo, _ = _services()
    material = _seed_configured_company(engine, repo)
    count = commands.create_count(
        tenant_id=TENANT, company=COMPANY, company_id=COMPANY_ID, counted_by="حسابدار"
    )
    assert count.state is CountState.DRAFT
    assert len(count.lines) == 1
    assert count.lines[0].material_id == material.id
    assert count.lines[0].expected_quantity == Decimal(50)
    assert count.lines[0].counted_quantity is None

    line_id = count.lines[0].tryton_line_id
    assert line_id is not None
    with pytest.raises(InvalidCountError):
        commands.confirm_count(tenant_id=TENANT, count_id=count.id)  # uncounted line

    counted = commands.set_count_line(
        tenant_id=TENANT, count_id=count.id, line_id=line_id, quantity=Decimal(48)
    )
    assert counted.lines[0].counted_quantity == Decimal(48)
    assert counted.lines[0].variance == Decimal(-2)

    confirmed = commands.confirm_count(tenant_id=TENANT, count_id=count.id)
    assert confirmed.state is CountState.DONE
    with pytest.raises(InvalidCountError):
        commands.set_count_line(
            tenant_id=TENANT, count_id=count.id, line_id=line_id, quantity=Decimal(48)
        )


def test_count_cancel_and_unknown_count():
    commands, _, engine, repo, _ = _services()
    _seed_configured_company(engine, repo)
    count = commands.create_count(tenant_id=TENANT, company=COMPANY, company_id=COMPANY_ID)
    cancelled = commands.cancel_count(tenant_id=TENANT, count_id=count.id)
    assert cancelled.state is CountState.CANCELLED
    with pytest.raises(InvalidCountError):
        commands.confirm_count(tenant_id=TENANT, count_id=count.id)
    with pytest.raises(CountNotFoundError):
        commands.confirm_count(tenant_id=TENANT, count_id="missing")


def test_get_count_refreshes_from_engine():
    commands, queries, engine, repo, _ = _services()
    _seed_configured_company(engine, repo)
    count = commands.create_count(tenant_id=TENANT, company=COMPANY, company_id=COMPANY_ID)
    refreshed = queries.get_count(TENANT, count.id)
    assert refreshed.state is CountState.DRAFT
    assert len(refreshed.lines) == 1


# ---------------------------------------------------------------------------
# Services: supply check
# ---------------------------------------------------------------------------


def test_supply_check_files_items_for_new_purchase_requests():
    commands, _, engine, repo, queue = _services()
    material = _seed_configured_company(engine, repo)
    engine.seed_purchase_request(11, material.tryton_product_id, Decimal(20))
    report = commands.run_supply_check(
        tenant_id=TENANT, company=COMPANY, company_id=COMPANY_ID, actor="worker"
    )
    assert report.purchase_requests_new == 1
    assert report.items_filed == 1
    assert repo.get_supply_watermark(TENANT, COMPANY_ID) == 11
    items = list(queue.items.values())
    assert len(items) == 1
    assert items[0].item_type is ReviewItemType.PACKAGING_REORDER
    assert items[0].source_event_ref == "purchase_request:11"
    assert items[0].amount_irr == 240_000  # 20 x 12,000


def test_supply_check_deduplicates_open_items():
    commands, _, engine, repo, queue = _services()
    material = _seed_configured_company(engine, repo)
    engine.seed_purchase_request(11, material.tryton_product_id, Decimal(20))
    first = commands.run_supply_check(
        tenant_id=TENANT, company=COMPANY, company_id=COMPANY_ID, actor="worker"
    )
    assert first.items_filed == 1
    # Second run: the watermark moved past 11, so nothing new is seen.
    second = commands.run_supply_check(
        tenant_id=TENANT, company=COMPANY, company_id=COMPANY_ID, actor="worker"
    )
    assert second.purchase_requests_new == 0
    assert second.items_filed == 0
    assert len(queue.items) == 1


def test_supply_check_low_stock_files_warning_and_out_of_stock_critical():
    commands, _, engine, repo, queue = _services()
    material = _seed_configured_company(engine, repo)
    # 30 consumed in the last 30 days, 5 on hand -> 5 days of cover (< 7 alert).
    engine.seed_product(
        material.tryton_product_id,
        material.tryton_template_id,
        quantity=Decimal(5),
        cost_price=Decimal("12000"),
    )
    engine.seed_move(
        material.tryton_product_id,
        Decimal(30),
        FiscalDate.from_gregorian(datetime.date(2026, 11, 10)),
    )
    report = commands.run_supply_check(
        tenant_id=TENANT, company=COMPANY, company_id=COMPANY_ID, actor="worker"
    )
    assert report.items_filed == 1
    assert report.low_stock_materials == [material.id]
    item = next(iter(queue.items.values()))
    assert item.severity is ReviewSeverity.WARNING
    assert item.source_event_ref == f"low_stock:{material.id}"

    # Out of stock -> critical on the next run (after resolving the first item).
    item2 = next(iter(queue.items.values()))
    queue.items[item2.id] = ReviewItem(
        id=item2.id,
        tenant_id=item2.tenant_id,
        item_type=item2.item_type,
        severity=item2.severity,
        status=ReviewItemStatus.RESOLVED,
        reason=item2.reason,
        suggested_action=item2.suggested_action,
        created_at=item2.created_at,
        company_id=item2.company_id,
        source_event_ref=item2.source_event_ref,
    )
    engine.seed_product(
        material.tryton_product_id,
        material.tryton_template_id,
        quantity=Decimal(0),
        cost_price=Decimal("12000"),
    )
    report2 = commands.run_supply_check(
        tenant_id=TENANT, company=COMPANY, company_id=COMPANY_ID, actor="worker"
    )
    assert report2.items_filed == 1
    critical = [i for i in queue.items.values() if i.severity is ReviewSeverity.CRITICAL]
    assert len(critical) == 1


def test_supply_check_healthy_material_files_nothing():
    commands, _, engine, repo, queue = _services()
    _seed_configured_company(engine, repo)
    report = commands.run_supply_check(
        tenant_id=TENANT, company=COMPANY, company_id=COMPANY_ID, actor="worker"
    )
    assert report.items_filed == 0
    assert report.materials_checked == 1
    assert queue.items == {}


def test_supply_check_per_material_error_is_collected_not_fatal():
    commands, _, engine, repo, _ = _services()
    material = _seed_configured_company(engine, repo)
    # The catalog row exists but the engine product is gone.
    del engine.products[material.tryton_product_id]
    report = commands.run_supply_check(
        tenant_id=TENANT, company=COMPANY, company_id=COMPANY_ID, actor="worker"
    )
    assert len(report.errors) == 1
    assert report.materials_checked == 1


def test_supply_check_reads_products_in_one_batch():
    # Per-product reads took ~149s for 21 materials over the remote DB
    # (US-515, 2026-11-18) — past the 120s BFF timeout, so the
    # Playwright supply-check step hung. The check must batch.
    commands, _, engine, repo, _ = _services()
    _seed_configured_company(engine, repo)
    second = _material(
        name_fa="نوار چسب", tryton_product_id=1002, tryton_template_id=2002
    )
    repo.save_material(second)
    engine.seed_product(second.tryton_product_id, second.tryton_template_id)
    report = commands.run_supply_check(
        tenant_id=TENANT, company=COMPANY, company_id=COMPANY_ID, actor="worker"
    )
    assert report.materials_checked == 2
    assert engine.list_packaging_products_calls == 1
    assert engine.get_packaging_product_calls == 0


# ---------------------------------------------------------------------------
# Queries: materials / coverage / order cost / quotes
# ---------------------------------------------------------------------------


def test_list_materials_stock_none_without_location():
    _, queries, engine, repo, _ = _services()
    material = _material()
    repo.save_material(material)
    repo.save_settings(_settings(packaging_location_id=None, production_location_id=None))
    engine.seed_product(material.tryton_product_id, material.tryton_template_id)
    details = queries.list_materials(TENANT, COMPANY, COMPANY_ID)
    assert len(details) == 1
    # Unknown stock is None — never a fake zero.
    assert details[0].stock is None
    assert details[0].coverage is None


def test_list_materials_reads_products_in_one_batch():
    # Per-product reads took ~30s for 20 materials over the remote DB
    # (US-515, 2026-11-18) — the list must use the batch call.
    _, queries, engine, repo, _ = _services()
    material = _seed_configured_company(engine, repo)
    second = _material(
        name_fa="نوار چسب", tryton_product_id=1002, tryton_template_id=2002
    )
    repo.save_material(second)
    engine.seed_product(second.tryton_product_id, second.tryton_template_id)
    details = queries.list_materials(TENANT, COMPANY, COMPANY_ID)
    assert len(details) == 2
    assert engine.list_packaging_products_calls == 1
    assert engine.get_packaging_product_calls == 0
    assert all(detail.stock is not None for detail in details)
    assert {detail.material.id for detail in details} == {material.id, second.id}


def test_get_material_detail_with_coverage_and_point():
    _, queries, engine, repo, _ = _services()
    material = _seed_configured_company(engine, repo)
    engine.seed_move(
        material.tryton_product_id,
        Decimal(30),
        FiscalDate.from_gregorian(datetime.date(2026, 11, 10)),
    )
    commands, _, _, _, _ = _services(engine, repo)
    commands.set_reorder_point(
        tenant_id=TENANT,
        company=COMPANY,
        company_id=COMPANY_ID,
        material_id=material.id,
        min_quantity=Decimal(10),
        target_quantity=Decimal(50),
    )
    detail = queries.get_material(TENANT, COMPANY, COMPANY_ID, material.id)
    assert detail.stock is not None
    assert detail.stock.quantity == Decimal(50)
    assert detail.coverage is not None
    assert detail.coverage.days_of_cover == pytest.approx(50.0)
    assert detail.reorder_point is not None
    assert detail.is_below_min is False


def test_get_coverage_without_location_is_setup_error():
    _, queries, engine, repo, _ = _services()
    material = _material()
    repo.save_material(material)
    repo.save_settings(_settings(packaging_location_id=None, production_location_id=None))
    engine.seed_product(material.tryton_product_id, material.tryton_template_id)
    with pytest.raises(PackagingSetupError):
        queries.get_coverage(TENANT, COMPANY, COMPANY_ID, material.id)


def test_get_order_cost_merges_analytic_packaging_and_shipment():
    commands, queries, engine, repo, _ = _services()
    material = _seed_configured_company(engine, repo)
    center = commands.create_cost_center(
        tenant_id=TENANT, company=COMPANY, company_id=COMPANY_ID, order_ref="ORD-1"
    )
    engine.analytic_lines[center.analytic_account_engine_id] = [
        AnalyticLineView(
            account=AnalyticAccountRef(center.analytic_account_engine_id),
            date=TODAY,
            debit=MoneyIRR(200_000),
            credit=MoneyIRR(0),
        ),
        AnalyticLineView(
            account=AnalyticAccountRef(center.analytic_account_engine_id),
            date=TODAY,
            debit=MoneyIRR(0),
            credit=MoneyIRR(500_000),
        ),
    ]
    commands.record_consumption(
        tenant_id=TENANT,
        company=COMPANY,
        company_id=COMPANY_ID,
        material_id=material.id,
        order_ref="ORD-1",
        shipment_ref="S1",
        quantity=Decimal(2),
    )
    commands.record_shipment_cost(
        tenant_id=TENANT,
        company=COMPANY,
        company_id=COMPANY_ID,
        order_ref="ORD-1",
        method=ShipmentCostMethod.ON_ORDER,
        actual_irr=50_000,
        invoiced_irr=30_000,
    )
    report = queries.get_order_cost(TENANT, COMPANY, COMPANY_ID, "ORD-1")
    assert report.goods_cost_irr == 200_000
    assert report.revenue_irr == 500_000
    assert report.packaging_cost_irr == 24_000
    assert report.shipment_invoiced_irr == 30_000
    assert report.shipment_actual_irr == 50_000
    assert report.shipment_absorbed_irr == 20_000
    assert report.total_cost_irr == 274_000
    assert report.margin_irr == 226_000
    assert report.analytic_line_count == 2
    assert report.consumption_count == 1


def test_get_order_cost_without_cost_center_is_honest_zero():
    _, queries, engine, repo, _ = _services()
    _seed_configured_company(engine, repo)
    report = queries.get_order_cost(TENANT, COMPANY, COMPANY_ID, "ORD-9")
    assert report.analytic_account_engine_id is None
    assert report.goods_cost_irr == 0
    assert report.analytic_line_count == 0


def test_compute_quote_with_carrier_rate():
    commands, queries, engine, repo, _ = _services()
    material = _seed_configured_company(engine, repo)
    rate = commands.create_carrier_rate(
        tenant_id=TENANT, company_id=COMPANY_ID, carrier_name="پست", rate_irr=45_000
    )
    quote = queries.compute_quote(
        TENANT,
        COMPANY,
        COMPANY_ID,
        goods_cost_irr=100_000,
        packaging=[QuotePackagingItem(material_id=material.id, quantity=Decimal(2))],
        carrier_rate_id=rate.id,
        margin_percent=Decimal(10),
    )
    assert quote.shipment_cost_irr == 45_000
    assert quote.carrier_name == "پست"
    assert quote.total_irr == 185_900  # (100k + 24k + 45k) x 1.1


def test_compute_quote_rate_and_explicit_shipment_are_exclusive():
    _, queries, engine, repo, _ = _services()
    _seed_configured_company(engine, repo)
    with pytest.raises(InvalidPriceQuoteError):
        queries.compute_quote(
            TENANT,
            COMPANY,
            COMPANY_ID,
            goods_cost_irr=0,
            shipment_cost_irr=10_000,
            carrier_rate_id="some-rate",
        )


def test_compute_quote_unknown_rate_404():
    _, queries, engine, repo, _ = _services()
    _seed_configured_company(engine, repo)
    with pytest.raises(CarrierRateNotFoundError):
        queries.compute_quote(
            TENANT, COMPANY, COMPANY_ID, goods_cost_irr=0, carrier_rate_id="missing"
        )
