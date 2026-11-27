"""Packaging & shipping write use cases (US-515, stream K1).

Materials, reorder points, consumption recording, shipment costs,
carrier rates, counts, cost centres, settings, and the daily
supply check. This layer talks to persistence ONLY through the
domain ports — never infrastructure (spec §6.3).

Dual-entry note: consumption has two paths to the same record —
manual entry here and (once US-511 lands) the store's
``shipment.dispatched`` webhook. Both go through
:meth:`PackagingCommandService.record_consumption` with the same
idempotency key (``pack:use:{order_ref}:{shipment_ref}:{material}``),
so a manual entry and a webhook for the same shipment consume once.

Claim-then-execute: ``record_consumption`` inserts the consumption
row (claiming the idempotency key) BEFORE doing the engine move, so
a crash between the two cannot double-move on retry — the retry
finds the claim. If the move itself fails, the claim is rolled back
so a clean retry is possible. The residual window (move done, claim
update lost) leaves a visible pending record (``tryton_move_id`` is
None) — the same honesty as the event pipeline's residual window.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from decimal import ROUND_HALF_UP, Decimal

from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.application.errors import translate_engine_error
from hesabyar.domain.accounting.commands import (
    CreateAnalyticAccount,
    CreateConsumptionMove,
    CreateInventory,
    CreatePackagingProduct,
    UpdateInventoryLine,
    UpsertOrderPoint,
)
from hesabyar.domain.accounting.ports import AccountingEnginePort
from hesabyar.domain.accounting.refs import (
    AnalyticAccountRef,
    CompanyRef,
    InventoryLineRef,
    InventoryRef,
    ProductRef,
    SaleRef,
    StockLocationRef,
)
from hesabyar.domain.accounting.value_objects import InventoryView
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.errors import HesabYarError
from hesabyar.domain.packaging import coverage as coverage_calc
from hesabyar.domain.packaging.errors import (
    CarrierRateNotFoundError,
    CostCenterExistsError,
    CountNotFoundError,
    InvalidCountError,
    InvalidShipmentCostError,
    MaterialNotFoundError,
    PackagingSetupError,
)
from hesabyar.domain.packaging.models import (
    CarrierRate,
    ConsumptionRecord,
    ConsumptionSource,
    CostMethod,
    CountLine,
    CountState,
    InventoryCount,
    MaterialDetail,
    MaterialStock,
    OrderCostCenter,
    PackagingMaterial,
    PackagingSettings,
    ReorderPoint,
    ShipmentCostMethod,
    ShipmentCostRecord,
    SupplyCheckReport,
    consumption_idempotency_key,
)
from hesabyar.domain.packaging.ports import PackagingRepositoryPort
from hesabyar.domain.review_queue.models import (
    ReviewItemStatus,
    ReviewItemType,
    ReviewSeverity,
)
from hesabyar.domain.review_queue.ports import ReviewItemFilters, ReviewQueuePort

#: Location codes the provisioning script assigns (docs/packaging.md §7).
PACKAGING_LOCATION_CODE = "PACK"
PRODUCTION_LOCATION_CODE = "PACK-CONSUME"


class PackagingCommandService:
    """Write use cases for packaging & shipping over the domain ports."""

    def __init__(
        self,
        engine: AccountingEnginePort,
        repository: PackagingRepositoryPort,
        review_queue: ReviewQueuePort,
        review_commands: ReviewQueueCommandService,
        *,
        now: datetime.datetime | None = None,
        today: FiscalDate | None = None,
    ) -> None:
        self._engine = engine
        self._repository = repository
        self._review_queue = review_queue
        self._review_commands = review_commands
        self._now = now
        self._today_override = today

    def _clock(self) -> datetime.datetime:
        if self._now is not None:
            return self._now
        return datetime.datetime.now(datetime.UTC)

    def _today(self) -> FiscalDate:
        if self._today_override is not None:
            return self._today_override
        return FiscalDate(datetime.datetime.now(datetime.UTC).date())

    # -- settings ----------------------------------------------------------

    def get_or_default_settings(self, tenant_id: str, company_id: str) -> PackagingSettings:
        """The company's settings, or the defaults when never configured."""
        settings = self._repository.get_settings(tenant_id, company_id)
        if settings is None:
            return PackagingSettings(tenant_id=tenant_id, company_id=company_id)
        return settings

    def save_settings(
        self,
        *,
        tenant_id: str,
        company_id: str,
        company: CompanyRef,
        cost_method: CostMethod | None = None,
        shipment_cost_method: ShipmentCostMethod | None = None,
        days_of_cover_window_days: int | None = None,
        low_stock_alert_days: int | None = None,
        packaging_location_id: int | None = None,
        production_location_id: int | None = None,
        warehouse_id: int | None = None,
        packaging_category_id: int | None = None,
        analytic_root_id: int | None = None,
    ) -> PackagingSettings:
        """Insert or replace the company's packaging settings.

        Location and analytic-root ids are verified against the engine
        before saving (fail fast on a typo'd id); the product category
        is verified at material creation (the port has no category
        read — the adapter checks it there).
        """
        current = self.get_or_default_settings(tenant_id, company_id)
        try:
            for location_id in (
                packaging_location_id,
                production_location_id,
                warehouse_id,
            ):
                if location_id is not None and (
                    self._engine.get_stock_location(StockLocationRef(location_id)) is None
                ):
                    raise PackagingSetupError(
                        f"Stock location {location_id} does not exist in the engine.",
                        details={"location_id": location_id},
                    )
            if analytic_root_id is not None and (
                self._engine.get_analytic_account(AnalyticAccountRef(analytic_root_id)) is None
            ):
                raise PackagingSetupError(
                    f"Analytic account {analytic_root_id} does not exist in the engine.",
                    details={"analytic_account_id": analytic_root_id},
                )
        except HesabYarError as exc:
            raise translate_engine_error(exc) from exc
        settings = PackagingSettings(
            tenant_id=tenant_id,
            company_id=company_id,
            cost_method=cost_method if cost_method is not None else current.cost_method,
            shipment_cost_method=(
                shipment_cost_method if shipment_cost_method is not None else current.shipment_cost_method
            ),
            days_of_cover_window_days=(
                days_of_cover_window_days
                if days_of_cover_window_days is not None
                else current.days_of_cover_window_days
            ),
            low_stock_alert_days=(
                low_stock_alert_days if low_stock_alert_days is not None else current.low_stock_alert_days
            ),
            packaging_location_id=(
                packaging_location_id
                if packaging_location_id is not None
                else current.packaging_location_id
            ),
            production_location_id=(
                production_location_id
                if production_location_id is not None
                else current.production_location_id
            ),
            warehouse_id=warehouse_id if warehouse_id is not None else current.warehouse_id,
            packaging_category_id=(
                packaging_category_id
                if packaging_category_id is not None
                else current.packaging_category_id
            ),
            analytic_root_id=(
                analytic_root_id if analytic_root_id is not None else current.analytic_root_id
            ),
            updated_at=self._clock(),
        )
        return self._repository.save_settings(settings)

    # -- materials ---------------------------------------------------------

    def create_material(
        self,
        *,
        tenant_id: str,
        company: CompanyRef,
        company_id: str,
        name_fa: str,
        cost_method: CostMethod | None = None,
        code: str | None = None,
    ) -> MaterialDetail:
        """Create a packaging material (engine product + catalog row).

        Needs the packaging category configured in settings. FIFO
        raises ``FifoUnavailableError`` unless ``product_cost_fifo``
        is activated in the engine (the adapter checks).

        Returns the detail built from facts known by construction —
        a fresh product has zero stock, no consumption and no reorder
        point — instead of re-reading the engine: the read-back
        (order points + moves + locations + product) cost ~12s over
        the remote DB and pushed the create endpoint to ~29s, right
        at the BFF timeout (US-515, 2026-11-18). The location name is
        empty here (resolving it needs an engine read); the materials
        list fills it in.
        """
        settings = self.get_or_default_settings(tenant_id, company_id)
        if settings.packaging_category_id is None:
            raise PackagingSetupError(
                "Packaging is not set up for this company: no packaging "
                "product category configured. Run the provisioning step "
                "and store the category id in packaging settings.",
                details={"company_id": company_id},
            )
        method = cost_method if cost_method is not None else settings.cost_method
        try:
            product_ref = self._engine.create_packaging_product(
                CreatePackagingProduct(
                    company=company,
                    name=name_fa,
                    category_engine_id=settings.packaging_category_id,
                    cost_method=method.value,
                    code=code,
                )
            )
            view = self._engine.get_packaging_product(product_ref, company)
        except HesabYarError as exc:
            raise translate_engine_error(exc) from exc
        if view is None:  # pragma: no cover - the engine just created it
            raise MaterialNotFoundError(
                "The engine did not return the created product.",
                details={"product_id": product_ref.engine_id},
            )
        material = PackagingMaterial(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            company_id=company_id,
            tryton_product_id=product_ref.engine_id,
            tryton_template_id=view.template_id,
            name_fa=name_fa,
            cost_method=method,
            code=code,
            uom_name=view.default_uom_name,
            created_at=self._clock(),
        )
        saved = self._repository.save_material(material)
        location_id = settings.packaging_location_id
        if location_id is None:
            return MaterialDetail(
                material=saved, stock=None, reorder_point=None, coverage=None
            )
        stock = MaterialStock(
            material_id=saved.id,
            quantity=Decimal(0),
            unit_cost_irr=view.cost_price,
            location_engine_id=location_id,
            location_name="",
        )
        coverage = coverage_calc.compute_days_of_cover(
            material_id=saved.id,
            quantity_on_hand=Decimal(0),
            consumed_in_window=Decimal(0),
            window_days=settings.days_of_cover_window_days,
        )
        return MaterialDetail(
            material=saved, stock=stock, reorder_point=None, coverage=coverage
        )

    def update_material(
        self,
        *,
        tenant_id: str,
        material_id: str,
        name_fa: str | None = None,
        code: str | None = None,
        is_active: bool | None = None,
    ) -> PackagingMaterial:
        """Update a material's catalog row (the engine product is untouched)."""
        material = self._require_material(material_id, tenant_id)
        updated = dataclasses.replace(
            material,
            name_fa=name_fa if name_fa is not None else material.name_fa,
            code=code if code is not None else material.code,
            is_active=is_active if is_active is not None else material.is_active,
        )
        return self._repository.update_material(updated)

    def set_reorder_point(
        self,
        *,
        tenant_id: str,
        company: CompanyRef,
        company_id: str,
        material_id: str,
        min_quantity: Decimal,
        target_quantity: Decimal,
    ) -> ReorderPoint:
        """Create or update a material's purchase order point at the warehouse."""
        material = self._require_material(material_id, tenant_id)
        settings = self.get_or_default_settings(tenant_id, company_id)
        try:
            warehouse_id = self._resolve_warehouse_id(company, settings)
            view = self._engine.upsert_order_point(
                UpsertOrderPoint(
                    company=company,
                    product=ProductRef(material.tryton_product_id),
                    warehouse=StockLocationRef(warehouse_id),
                    min_quantity=min_quantity,
                    target_quantity=target_quantity,
                )
            )
        except HesabYarError as exc:
            raise translate_engine_error(exc) from exc
        return ReorderPoint(
            material_id=material.id,
            warehouse_engine_id=warehouse_id,
            min_quantity=view.min_quantity if view.min_quantity is not None else Decimal(0),
            target_quantity=view.target_quantity,
            tryton_order_point_id=view.ref.engine_id,
            max_quantity=view.max_quantity,
        )

    # -- consumption -------------------------------------------------------

    def record_consumption(
        self,
        *,
        tenant_id: str,
        company: CompanyRef,
        company_id: str,
        material_id: str,
        order_ref: str,
        shipment_ref: str | None = None,
        quantity: Decimal,
        consumed_on: FiscalDate | None = None,
        source: ConsumptionSource = ConsumptionSource.MANUAL,
    ) -> tuple[ConsumptionRecord, bool]:
        """Record a packaging consumption for an order (idempotent).

        Returns ``(record, created)`` — ``created`` is False when the
        idempotency key already claimed the consumption (replay: the
        stored record is returned, no second move is made).
        """
        material = self._require_material(material_id, tenant_id)
        shipment = shipment_ref or "manual"
        key = consumption_idempotency_key(order_ref, shipment, material.id)
        existing = self._repository.get_consumption_by_key(tenant_id, key)
        if existing is not None:
            return existing, False
        settings = self.get_or_default_settings(tenant_id, company_id)
        date = consumed_on if consumed_on is not None else self._today()
        claim = ConsumptionRecord(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            company_id=company_id,
            material_id=material.id,
            order_ref=order_ref,
            shipment_ref=shipment,
            quantity=quantity,
            unit_cost_irr=Decimal(0),
            total_cost_irr=0,
            rounding_delta_irr=0,
            idempotency_key=key,
            source=source,
            analytic_account_engine_id=self._cost_center_id(tenant_id, company_id, order_ref),
            consumed_on=date,
            created_at=self._clock(),
        )
        self._repository.save_consumption(claim)
        try:
            from_id, to_id = self._resolve_consumption_locations(company, settings)
            move = self._engine.create_consumption_move(
                CreateConsumptionMove(
                    company=company,
                    product=ProductRef(material.tryton_product_id),
                    quantity=quantity,
                    from_location=StockLocationRef(from_id),
                    to_location=StockLocationRef(to_id),
                    date=date,
                )
            )
        except HesabYarError as exc:
            # The move failed: roll the claim back so a clean retry works.
            self._repository.delete_consumption(claim.id, tenant_id)
            raise translate_engine_error(exc) from exc
        unit_cost = move.cost_price if move.cost_price is not None else Decimal(0)
        exact_total = quantity * unit_cost
        total = int(exact_total.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        completed = dataclasses.replace(
            claim,
            unit_cost_irr=unit_cost,
            total_cost_irr=total,
            rounding_delta_irr=total - int(exact_total),
            tryton_move_id=move.ref.engine_id,
        )
        return self._repository.update_consumption(completed), True

    # -- shipment costs ----------------------------------------------------

    def record_shipment_cost(
        self,
        *,
        tenant_id: str,
        company: CompanyRef,
        company_id: str,
        order_ref: str,
        method: ShipmentCostMethod,
        actual_irr: int,
        invoiced_irr: int | None = None,
        carrier_name: str | None = None,
        tryton_sale_id: int | None = None,
        tryton_shipment_id: int | None = None,
    ) -> ShipmentCostRecord:
        """Record the three shipment-cost figures for one order.

        When a Tryton sale is linked, the invoiced figure is read from
        the engine (authoritative): a caller-supplied figure that
        disagrees is a 422 variance, never silently overwritten.
        """
        invoiced = invoiced_irr
        if tryton_sale_id is not None:
            try:
                engine_lines = self._engine.get_sale_shipment_costs(SaleRef(tryton_sale_id))
            except HesabYarError as exc:
                raise translate_engine_error(exc) from exc
            engine_invoiced = sum(line.amount.amount for line in engine_lines)
            if invoiced is not None and invoiced != engine_invoiced:
                raise InvalidShipmentCostError(
                    f"Invoiced shipment cost {invoiced} IRR disagrees with the "
                    f"engine's {engine_invoiced} IRR for sale {tryton_sale_id}.",
                    details={
                        "order_ref": order_ref,
                        "sale_id": tryton_sale_id,
                        "invoiced_irr": invoiced,
                        "engine_invoiced_irr": engine_invoiced,
                    },
                )
            invoiced = engine_invoiced
        if invoiced is None:
            invoiced = 0
        record = ShipmentCostRecord(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            company_id=company_id,
            order_ref=order_ref,
            method=method,
            invoiced_irr=invoiced,
            actual_irr=actual_irr,
            carrier_name=carrier_name,
            tryton_sale_id=tryton_sale_id,
            tryton_shipment_id=tryton_shipment_id,
            analytic_account_engine_id=self._cost_center_id(tenant_id, company_id, order_ref),
            created_at=self._clock(),
        )
        return self._repository.save_shipment_cost(record)

    # -- carrier rates -----------------------------------------------------

    def create_carrier_rate(
        self,
        *,
        tenant_id: str,
        company_id: str,
        carrier_name: str,
        rate_irr: int,
        basis: str = "per_shipment",
        tryton_carrier_id: int | None = None,
        effective_from: FiscalDate | None = None,
    ) -> CarrierRate:
        """Add a user-entered carrier rate (no rate is ever hard-coded)."""
        rate = CarrierRate(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            company_id=company_id,
            carrier_name=carrier_name,
            rate_irr=rate_irr,
            basis=basis,
            tryton_carrier_id=tryton_carrier_id,
            effective_from=effective_from,
            created_at=self._clock(),
        )
        return self._repository.save_carrier_rate(rate)

    def update_carrier_rate(
        self,
        *,
        tenant_id: str,
        rate_id: str,
        rate_irr: int | None = None,
        is_active: bool | None = None,
    ) -> CarrierRate:
        """Update a carrier rate's amount or active flag."""
        rate = self._repository.get_carrier_rate(rate_id, tenant_id)
        if rate is None:
            raise CarrierRateNotFoundError(
                f"Carrier rate {rate_id!r} does not exist.",
                details={"carrier_rate_id": rate_id},
            )
        updated = dataclasses.replace(
            rate,
            rate_irr=rate_irr if rate_irr is not None else rate.rate_irr,
            is_active=is_active if is_active is not None else rate.is_active,
        )
        return self._repository.update_carrier_rate(updated)

    # -- cost centres ------------------------------------------------------

    def create_cost_center(
        self,
        *,
        tenant_id: str,
        company: CompanyRef,
        company_id: str,
        order_ref: str,
    ) -> OrderCostCenter:
        """Create the analytic cost centre for one order (one per order)."""
        existing = self._repository.get_cost_center(tenant_id, company_id, order_ref)
        if existing is not None:
            raise CostCenterExistsError(
                f"Order {order_ref!r} already has a cost centre.",
                details={"order_ref": order_ref},
            )
        settings = self.get_or_default_settings(tenant_id, company_id)
        if settings.analytic_root_id is None:
            raise PackagingSetupError(
                "Packaging is not set up for this company: no analytic "
                "root configured. Run the provisioning step and store "
                "the root id in packaging settings.",
                details={"company_id": company_id},
            )
        root = AnalyticAccountRef(settings.analytic_root_id)
        try:
            ref = self._engine.create_analytic_account(
                CreateAnalyticAccount(
                    company=company,
                    name=f"سفارش {order_ref}",
                    account_type="normal",
                    code=f"ORD-{order_ref}"[:64],
                    root=root,
                    parent=root,
                )
            )
        except HesabYarError as exc:
            raise translate_engine_error(exc) from exc
        center = OrderCostCenter(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            company_id=company_id,
            order_ref=order_ref,
            analytic_account_engine_id=ref.engine_id,
            created_at=self._clock(),
        )
        return self._repository.save_cost_center(center)

    # -- counts ------------------------------------------------------------

    def create_count(
        self,
        *,
        tenant_id: str,
        company: CompanyRef,
        company_id: str,
        count_date: FiscalDate | None = None,
        counted_by: str | None = None,
    ) -> InventoryCount:
        """Open a periodic materials count at the packaging location.

        The engine inventory is created and its lines completed with
        expected quantities; the operator then enters counted
        quantities per line and confirms (the human approval step).
        """
        settings = self.get_or_default_settings(tenant_id, company_id)
        try:
            location_id = self._resolve_packaging_location_id(company, settings)
            ref = self._engine.create_inventory(
                CreateInventory(
                    company=company,
                    location=StockLocationRef(location_id),
                    date=count_date if count_date is not None else self._today(),
                )
            )
            view = self._engine.complete_inventory_lines(ref)
        except HesabYarError as exc:
            raise translate_engine_error(exc) from exc
        count = InventoryCount(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            company_id=company_id,
            tryton_inventory_id=ref.engine_id,
            location_engine_id=location_id,
            location_name=view.location_name,
            count_date=view.date,
            state=CountState(view.state),
            counted_by=counted_by,
            lines=tuple(
                CountLine(
                    material_id=self._material_id_for_product(
                        tenant_id, company_id, line.product.engine_id
                    )
                    or str(line.product.engine_id),
                    material_name=line.product_name,
                    expected_quantity=line.expected_quantity,
                    counted_quantity=line.quantity,
                    tryton_line_id=line.ref.engine_id,
                )
                for line in view.lines
            ),
            created_at=self._clock(),
        )
        return self._repository.save_count(count)

    def set_count_line(
        self,
        *,
        tenant_id: str,
        count_id: str,
        line_id: int,
        quantity: Decimal,
    ) -> InventoryCount:
        """Set the counted quantity on one count line (draft counts only)."""
        count = self._require_count(count_id, tenant_id)
        if count.state is not CountState.DRAFT:
            raise InvalidCountError(
                f"Count {count_id!r} is {count.state.value} and no longer accepts quantities.",
                details={"count_id": count_id, "state": count.state.value},
            )
        try:
            view = self._engine.update_inventory_line(
                UpdateInventoryLine(line=InventoryLineRef(line_id), quantity=quantity)
            )
        except HesabYarError as exc:
            raise translate_engine_error(exc) from exc
        return self._refresh_count(count, view)

    def confirm_count(self, *, tenant_id: str, count_id: str) -> InventoryCount:
        """Confirm a count — the human approval for the adjustment moves.

        The engine creates the to/from lost-and-found moves and does
        them. Nothing calls this automatically.
        """
        count = self._require_count(count_id, tenant_id)
        if count.state is not CountState.DRAFT:
            raise InvalidCountError(
                f"Count {count_id!r} is {count.state.value} and cannot be confirmed.",
                details={"count_id": count_id, "state": count.state.value},
            )
        uncounted = [line for line in count.lines if line.counted_quantity is None]
        if uncounted:
            raise InvalidCountError(
                f"Count {count_id!r} has {len(uncounted)} uncounted line(s); "
                "enter every counted quantity before confirming.",
                details={"count_id": count_id, "uncounted_lines": len(uncounted)},
            )
        try:
            view = self._engine.confirm_inventory(InventoryRef(count.tryton_inventory_id))
        except HesabYarError as exc:
            raise translate_engine_error(exc) from exc
        return self._refresh_count(count, view)

    def cancel_count(self, *, tenant_id: str, count_id: str) -> InventoryCount:
        """Cancel a draft count (no adjustment moves are created)."""
        count = self._require_count(count_id, tenant_id)
        if count.state is not CountState.DRAFT:
            raise InvalidCountError(
                f"Count {count_id!r} is {count.state.value} and cannot be cancelled.",
                details={"count_id": count_id, "state": count.state.value},
            )
        try:
            view = self._engine.cancel_inventory(InventoryRef(count.tryton_inventory_id))
        except HesabYarError as exc:
            raise translate_engine_error(exc) from exc
        return self._refresh_count(count, view)

    # -- supply check ------------------------------------------------------

    def run_supply_check(
        self,
        *,
        tenant_id: str,
        company: CompanyRef,
        company_id: str,
        actor: str,
    ) -> SupplyCheckReport:
        """Read the supply wizard's results + live coverage; file reorder items.

        The wizard itself runs inside Tryton (``ir.cron`` +
        ``trytond-cron``); this job only READS: new purchase requests
        since the watermark, and days-of-cover per material. Each new
        purchase request and each low-stock material files one
        ``packaging_reorder`` review item — deduplicated against open
        items, so a daily run never spams the queue. A per-material
        failure is collected on the report, never fatal to the run.
        """
        now = self._clock()
        today = self._today()
        settings = self.get_or_default_settings(tenant_id, company_id)
        report = SupplyCheckReport(company_id=company_id, checked_at=now)
        watermark = self._repository.get_supply_watermark(tenant_id, company_id)
        try:
            requests = self._engine.list_purchase_requests(
                company, after_id=watermark if watermark > 0 else None
            )
        except HesabYarError as exc:
            raise translate_engine_error(exc) from exc
        report.purchase_requests_seen = len(requests)
        max_seen = watermark
        for request in requests:
            max_seen = max(max_seen, request.ref.engine_id)
            source_ref = f"purchase_request:{request.ref.engine_id}"
            if self._has_open_item(tenant_id, company_id, source_ref):
                report.items_skipped_open += 1
                continue
            material = self._repository.get_material_by_product(
                tenant_id, company_id, request.product.engine_id
            )
            material_name = material.name_fa if material is not None else f"#{request.product.engine_id}"
            amount = self._estimate_request_amount(company, request.product.engine_id, request.quantity)
            self._review_commands.file_item(
                tenant_id=tenant_id,
                item_type=ReviewItemType.PACKAGING_REORDER,
                severity=ReviewSeverity.WARNING,
                reason=(
                    f"درخواست خرید خودکار برای «{material_name}» — "
                    f"مقدار {request.quantity:g} (نقطهٔ سفارش رد شد)."
                ),
                suggested_action="درخواست خرید را تأیید کنید یا نقطهٔ سفارش را تغییر دهید.",
                company_id=company_id,
                source_event_ref=source_ref,
                amount_irr=amount,
                actor=actor,
            )
            report.purchase_requests_new += 1
            report.items_filed += 1
        self._repository.save_supply_watermark(tenant_id, company_id, max_seen)

        materials = [
            m for m in self._repository.list_materials(tenant_id, company_id) if m.is_active
        ]
        report.materials_checked = len(materials)
        window_start = FiscalDate(today.gregorian_date - datetime.timedelta(days=settings.days_of_cover_window_days))
        try:
            from_id = self._resolve_packaging_location_id(company, settings)
            to_id = self._resolve_production_location_id(company, settings)
            moves = self._engine.list_consumption_moves(
                company,
                StockLocationRef(from_id),
                StockLocationRef(to_id),
                window_start,
                today,
            )
        except HesabYarError as exc:
            raise translate_engine_error(exc) from exc
        consumed_by_product: dict[int, Decimal] = {}
        last_date_by_product: dict[int, FiscalDate] = {}
        for move in moves:
            pid = move.product.engine_id
            consumed_by_product[pid] = consumed_by_product.get(pid, Decimal(0)) + move.quantity
            if move.effective_date is not None and (
                pid not in last_date_by_product or move.effective_date > last_date_by_product[pid]
            ):
                last_date_by_product[pid] = move.effective_date
        try:
            warehouse_id = self._resolve_warehouse_id(company, settings)
        except HesabYarError as exc:
            # Without a warehouse, order-point breaches cannot be
            # evaluated — but low-cover checks still run below.
            report.errors.append(f"Warehouse resolution failed: {exc.message}")
            warehouse_id = -1
        # Batch the per-material engine reads (one call each for all
        # materials): per-product reads took ~149s for 21 materials
        # over the remote DB (US-515, 2026-11-18), past the 120s BFF
        # timeout — the same N+1 the materials list already fixed.
        try:
            product_views = {
                view.ref.engine_id: view
                for view in self._engine.list_packaging_products(
                    company,
                    [ProductRef(material.tryton_product_id) for material in materials],
                    StockLocationRef(from_id),
                )
            }
            points = (
                {
                    view.product.engine_id: view
                    for view in self._engine.list_order_points(company)
                    if view.warehouse.engine_id == warehouse_id
                }
                if warehouse_id > 0
                else {}
            )
        except HesabYarError as exc:
            raise translate_engine_error(exc) from exc
        for material in materials:
            try:
                view = product_views.get(material.tryton_product_id)
                if view is None:
                    report.errors.append(f"Material {material.name_fa!r}: product not in engine.")
                    continue
                on_hand = view.quantity if view.quantity is not None else Decimal(0)
                cover = coverage_calc.compute_days_of_cover(
                    material_id=material.id,
                    quantity_on_hand=on_hand,
                    consumed_in_window=consumed_by_product.get(material.tryton_product_id, Decimal(0)),
                    window_days=settings.days_of_cover_window_days,
                    last_consumption_date=last_date_by_product.get(material.tryton_product_id),
                )
                point = points.get(material.tryton_product_id) if warehouse_id > 0 else None
                below_min = (
                    point is not None
                    and point.min_quantity is not None
                    and on_hand < point.min_quantity
                )
                low_cover = cover.is_low(settings.low_stock_alert_days)
                if not below_min and not low_cover:
                    continue
                source_ref = f"low_stock:{material.id}"
                if self._has_open_item(tenant_id, company_id, source_ref):
                    report.items_skipped_open += 1
                    continue
                report.low_stock_materials.append(material.id)
                severity = ReviewSeverity.CRITICAL if cover.is_out_of_stock else ReviewSeverity.WARNING
                cover_text = (
                    "مصرفی در بازه ثبت نشده است"
                    if cover.days_of_cover is None
                    else f"≈ {cover.days_of_cover:.1f} روز پوشش باقی مانده است"
                )
                self._review_commands.file_item(
                    tenant_id=tenant_id,
                    item_type=ReviewItemType.PACKAGING_REORDER,
                    severity=severity,
                    reason=(
                        f"موجودی «{material.name_fa}» کم است — "
                        f"موجودی {on_hand:g}؛ {cover_text}."
                    ),
                    suggested_action="درخواست خرید را تأیید کنید یا نقطهٔ سفارش را تغییر دهید.",
                    company_id=company_id,
                    source_event_ref=source_ref,
                    actor=actor,
                )
                report.items_filed += 1
            except HesabYarError as exc:
                report.errors.append(f"Material {material.name_fa!r}: {exc.message}")
        return report

    # -- helpers -----------------------------------------------------------

    def _require_material(self, material_id: str, tenant_id: str) -> PackagingMaterial:
        material = self._repository.get_material(material_id, tenant_id)
        if material is None:
            raise MaterialNotFoundError(
                f"Packaging material {material_id!r} does not exist.",
                details={"material_id": material_id},
            )
        return material

    def _require_count(self, count_id: str, tenant_id: str) -> InventoryCount:
        count = self._repository.get_count(count_id, tenant_id)
        if count is None:
            raise CountNotFoundError(
                f"Inventory count {count_id!r} does not exist.",
                details={"count_id": count_id},
            )
        return count

    def _cost_center_id(self, tenant_id: str, company_id: str, order_ref: str) -> int | None:
        center = self._repository.get_cost_center(tenant_id, company_id, order_ref)
        return center.analytic_account_engine_id if center is not None else None

    def _material_id_for_product(
        self, tenant_id: str, company_id: str, product_engine_id: int
    ) -> str | None:
        material = self._repository.get_material_by_product(tenant_id, company_id, product_engine_id)
        return material.id if material is not None else None

    def _refresh_count(self, count: InventoryCount, view: InventoryView) -> InventoryCount:
        """Refresh a stored count from an engine inventory read-back."""
        updated = dataclasses.replace(
            count,
            state=CountState(view.state),
            lines=tuple(
                CountLine(
                    material_id=self._material_id_for_product(
                        count.tenant_id, count.company_id, line.product.engine_id
                    )
                    or str(line.product.engine_id),
                    material_name=line.product_name,
                    expected_quantity=line.expected_quantity,
                    counted_quantity=line.quantity,
                    tryton_line_id=line.ref.engine_id,
                )
                for line in view.lines
            ),
        )
        return self._repository.update_count(updated)

    def _has_open_item(self, tenant_id: str, company_id: str, source_ref: str) -> bool:
        """True when an open/in-review item already covers this source ref."""
        items = self._review_queue.list_items(
            tenant_id,
            ReviewItemFilters(
                item_type=ReviewItemType.PACKAGING_REORDER,
                company_id=company_id,
            ),
        )
        return any(
            item.source_event_ref == source_ref
            and item.status in (ReviewItemStatus.OPEN, ReviewItemStatus.IN_REVIEW)
            for item in items
        )

    def _estimate_request_amount(
        self, company: CompanyRef, product_engine_id: int, quantity: Decimal
    ) -> int | None:
        """Best-effort amount for a purchase request (qty x current cost)."""
        try:
            cost = self._engine.get_product_cost_price(ProductRef(product_engine_id), company)
        except HesabYarError:
            return None
        if cost is None:
            return None
        return int((quantity * cost).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    def _resolve_packaging_location_id(
        self, company: CompanyRef, settings: PackagingSettings
    ) -> int:
        if settings.packaging_location_id is not None:
            return settings.packaging_location_id
        return self._find_location_by_code(company, PACKAGING_LOCATION_CODE, "packaging")

    def _resolve_production_location_id(
        self, company: CompanyRef, settings: PackagingSettings
    ) -> int:
        if settings.production_location_id is not None:
            return settings.production_location_id
        return self._find_location_by_code(company, PRODUCTION_LOCATION_CODE, "production")

    def _resolve_consumption_locations(
        self, company: CompanyRef, settings: PackagingSettings
    ) -> tuple[int, int]:
        return (
            self._resolve_packaging_location_id(company, settings),
            self._resolve_production_location_id(company, settings),
        )

    def _resolve_warehouse_id(self, company: CompanyRef, settings: PackagingSettings) -> int:
        if settings.warehouse_id is not None:
            return settings.warehouse_id
        locations = self._engine.list_stock_locations(company)
        warehouses = [loc for loc in locations if loc.location_type == "warehouse"]
        if len(warehouses) == 1:
            return warehouses[0].ref.engine_id
        raise PackagingSetupError(
            "No warehouse configured in packaging settings and the engine "
            f"has {len(warehouses)} warehouses — configure one explicitly.",
            details={"warehouse_count": len(warehouses)},
        )

    def _find_location_by_code(self, company: CompanyRef, code: str, role: str) -> int:
        locations = self._engine.list_stock_locations(company)
        for location in locations:
            if location.code == code:
                return location.ref.engine_id
        raise PackagingSetupError(
            f"No {role} location configured in packaging settings and no "
            f"location with code {code!r} exists in the engine. Run the "
            "provisioning step or configure the location id.",
            details={"location_code": code, "role": role},
        )
