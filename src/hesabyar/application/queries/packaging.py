"""Packaging & shipping read use cases (US-515, stream K1).

Materials with live stock + coverage, order-cost reports, price
quotes, counts, carrier rates and settings. Like every query
service, this layer talks to persistence ONLY through the domain
ports — never infrastructure (spec §6.3).

Reads never cache engine state: quantity, cost price, order points
and analytic lines are read live from the engine on every call
(spec §11.1 — HesabYar must not recompute an authoritative balance
from an incomplete cache when the Tryton read is available).
"""

from __future__ import annotations

import dataclasses
import datetime
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from hesabyar.application.errors import translate_engine_error
from hesabyar.domain.accounting.ports import AccountingEnginePort
from hesabyar.domain.accounting.refs import (
    AnalyticAccountRef,
    CompanyRef,
    InventoryRef,
    ProductRef,
    StockLocationRef,
)
from hesabyar.domain.accounting.value_objects import (
    OrderPointView,
    PackagingProductView,
    StockMoveView,
)
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.errors import HesabYarError
from hesabyar.domain.packaging import calculator as calculator_mod
from hesabyar.domain.packaging import coverage as coverage_calc
from hesabyar.domain.packaging.calculator import PackagingQuoteInput, PriceQuoteRequest
from hesabyar.domain.packaging.errors import (
    CarrierRateNotFoundError,
    CountNotFoundError,
    InvalidPriceQuoteError,
    MaterialNotFoundError,
    PackagingSetupError,
)
from hesabyar.domain.packaging.models import (
    CarrierRate,
    ConsumptionRecord,
    CountLine,
    CountState,
    DaysOfCover,
    InventoryCount,
    MaterialDetail,
    MaterialStock,
    OrderCostReport,
    PackagingMaterial,
    PackagingSettings,
    PriceQuote,
    ReorderPoint,
    ShipmentCostRecord,
)
from hesabyar.domain.packaging.ports import PackagingRepositoryPort


@dataclass(frozen=True, slots=True)
class QuotePackagingItem:
    """One packaging line requested in a quote (material + quantity)."""

    material_id: str
    quantity: Decimal


class PackagingQueryService:
    """Read use cases for packaging & shipping over the domain ports."""

    def __init__(
        self,
        engine: AccountingEnginePort,
        repository: PackagingRepositoryPort,
        *,
        today: FiscalDate | None = None,
    ) -> None:
        self._engine = engine
        self._repository = repository
        self._today_override = today

    def _today(self) -> FiscalDate:
        if self._today_override is not None:
            return self._today_override
        return FiscalDate(datetime.datetime.now(datetime.UTC).date())

    # -- settings ----------------------------------------------------------

    def get_settings(self, tenant_id: str, company_id: str) -> PackagingSettings:
        """The company's settings, or the defaults when never configured."""
        settings = self._repository.get_settings(tenant_id, company_id)
        if settings is None:
            return PackagingSettings(tenant_id=tenant_id, company_id=company_id)
        return settings

    def list_locations(self, company: CompanyRef):
        """The engine's stock locations (for the settings UI)."""
        try:
            return self._engine.list_stock_locations(company)
        except HesabYarError as exc:
            raise translate_engine_error(exc) from exc

    # -- materials ---------------------------------------------------------

    def list_materials(
        self, tenant_id: str, company: CompanyRef, company_id: str
    ) -> Sequence[MaterialDetail]:
        """All materials of one company with live stock + coverage.

        Engine reads are batched where the port allows: order points,
        consumption moves and locations are listed once for the
        company, and product quantities + cost prices come from one
        ``list_packaging_products`` call — per-product reads took
        ~30s for 20 materials over the remote DB (US-515, 2026-11-18),
        right at the BFF timeout.
        """
        settings = self.get_settings(tenant_id, company_id)
        materials = self._repository.list_materials(tenant_id, company_id)
        try:
            points = {
                view.product.engine_id: view
                for view in self._engine.list_order_points(company)
            }
            moves = self._consumption_moves_in_window(company, settings)
            location_names = {
                loc.ref.engine_id: loc.name for loc in self._engine.list_stock_locations(company)
            }
            product_views = {
                view.ref.engine_id: view
                for view in self._engine.list_packaging_products(
                    company,
                    [ProductRef(material.tryton_product_id) for material in materials],
                    StockLocationRef(settings.packaging_location_id)
                    if settings.packaging_location_id is not None
                    else None,
                )
            }
        except HesabYarError as exc:
            raise translate_engine_error(exc) from exc
        consumed = self._consumed_by_product(moves)
        last_date = self._last_date_by_product(moves)
        details: list[MaterialDetail] = []
        for material in materials:
            details.append(
                self._detail_for_material(
                    material, company, settings, points.get(material.tryton_product_id),
                    consumed, last_date, location_names,
                    product_view=product_views.get(material.tryton_product_id),
                    product_view_fetched=True,
                )
            )
        return details

    def get_material(
        self, tenant_id: str, company: CompanyRef, company_id: str, material_id: str
    ) -> MaterialDetail:
        """One material with live stock + coverage."""
        material = self._require_material(material_id, tenant_id)
        settings = self.get_settings(tenant_id, company_id)
        try:
            moves = self._consumption_moves_in_window(company, settings)
            point = self._point_for_material(material, company, settings)
            location_names = {
                loc.ref.engine_id: loc.name for loc in self._engine.list_stock_locations(company)
            }
        except HesabYarError as exc:
            raise translate_engine_error(exc) from exc
        consumed = self._consumed_by_product(moves)
        last_date = self._last_date_by_product(moves)
        return self._detail_for_material(
            material, company, settings, point, consumed, last_date, location_names
        )

    def get_coverage(
        self, tenant_id: str, company: CompanyRef, company_id: str, material_id: str
    ) -> DaysOfCover:
        """Days-of-cover for one material (live quantity + window consumption)."""
        detail = self.get_material(tenant_id, company, company_id, material_id)
        if detail.coverage is None:
            raise PackagingSetupError(
                "No packaging location configured for this company; "
                "coverage cannot be computed.",
                details={"company_id": company_id, "material_id": material_id},
            )
        return detail.coverage

    # -- consumptions / shipment costs / rates -----------------------------

    def list_consumptions(
        self,
        tenant_id: str,
        company_id: str,
        *,
        order_ref: str | None = None,
        material_id: str | None = None,
    ) -> Sequence[ConsumptionRecord]:
        """Consumption records of one company, newest first."""
        return self._repository.list_consumptions(
            tenant_id, company_id, order_ref=order_ref, material_id=material_id
        )

    def list_shipment_costs(
        self, tenant_id: str, company_id: str, *, order_ref: str | None = None
    ) -> Sequence[ShipmentCostRecord]:
        """Shipment-cost records of one company, newest first."""
        return self._repository.list_shipment_costs(tenant_id, company_id, order_ref=order_ref)

    def list_carrier_rates(
        self, tenant_id: str, company_id: str, *, active_only: bool = False
    ) -> Sequence[CarrierRate]:
        """Carrier rates of one company, newest first."""
        return self._repository.list_carrier_rates(
            tenant_id, company_id, active_only=active_only
        )

    # -- counts ------------------------------------------------------------

    def list_counts(self, tenant_id: str, company_id: str) -> Sequence[InventoryCount]:
        """Counts of one company, newest first (stored state)."""
        return self._repository.list_counts(tenant_id, company_id)

    def get_count(self, tenant_id: str, count_id: str) -> InventoryCount:
        """One count, refreshed from the engine (state + lines)."""
        count = self._repository.get_count(count_id, tenant_id)
        if count is None:
            raise CountNotFoundError(
                f"Inventory count {count_id!r} does not exist.",
                details={"count_id": count_id},
            )
        try:
            view = self._engine.get_inventory(InventoryRef(count.tryton_inventory_id))
        except HesabYarError as exc:
            raise translate_engine_error(exc) from exc
        if view is None:
            return count
        return dataclasses.replace(
            count,
            state=CountState(view.state),
            lines=tuple(
                CountLine(
                    material_id=self._material_id_for_product(
                        tenant_id, count.company_id, line.product.engine_id
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

    # -- order cost --------------------------------------------------------

    def get_order_cost(
        self, tenant_id: str, company: CompanyRef, company_id: str, order_ref: str
    ) -> OrderCostReport:
        """Full cost of one order from its analytic cost centre.

        Goods cost + revenue come from the Tryton analytic lines on
        the order's cost centre (0 when nothing is tagged yet);
        packaging cost from the consumption records; shipment figures
        from the shipment-cost records (latest per order).
        """
        center = self._repository.get_cost_center(tenant_id, company_id, order_ref)
        goods_cost = 0
        revenue = 0
        line_count = 0
        if center is not None:
            try:
                lines = self._engine.list_analytic_lines(
                    AnalyticAccountRef(center.analytic_account_engine_id)
                )
            except HesabYarError as exc:
                raise translate_engine_error(exc) from exc
            line_count = len(lines)
            goods_cost = sum(line.debit.amount for line in lines)
            revenue = sum(line.credit.amount for line in lines)
        consumptions = [
            record
            for record in self._repository.list_consumptions(
                tenant_id, company_id, order_ref=order_ref
            )
            if record.tryton_move_id is not None
        ]
        packaging_cost = sum(record.total_cost_irr for record in consumptions)
        shipment_records = self._repository.list_shipment_costs(
            tenant_id, company_id, order_ref=order_ref
        )
        invoiced = sum(record.invoiced_irr for record in shipment_records)
        actual = sum(record.actual_irr for record in shipment_records)
        return OrderCostReport(
            order_ref=order_ref,
            analytic_account_engine_id=(
                center.analytic_account_engine_id if center is not None else None
            ),
            goods_cost_irr=goods_cost,
            packaging_cost_irr=packaging_cost,
            shipment_invoiced_irr=invoiced,
            shipment_actual_irr=actual,
            shipment_absorbed_irr=actual - invoiced,
            revenue_irr=revenue,
            analytic_line_count=line_count,
            consumption_count=len(consumptions),
        )

    # -- price calculator --------------------------------------------------

    def compute_quote(
        self,
        tenant_id: str,
        company: CompanyRef,
        company_id: str,
        *,
        goods_cost_irr: int,
        packaging: Sequence[QuotePackagingItem] = (),
        shipment_cost_irr: int | None = None,
        carrier_rate_id: str | None = None,
        margin_percent: Decimal | None = None,
        margin_fixed_irr: int | None = None,
    ) -> PriceQuote:
        """Compute an approximate quote from current costs + entered rates.

        Shipment cost comes from a carrier rate (by id) or an explicit
        amount — never from a hard-coded table. Material costs are the
        engine's current cost prices; out-of-stock materials get a
        stale-price warning on the quote.
        """
        if shipment_cost_irr is not None and carrier_rate_id is not None:
            raise InvalidPriceQuoteError(
                "Give either a carrier rate or an explicit shipment cost, not both."
            )
        shipment = 0
        carrier_name: str | None = None
        if carrier_rate_id is not None:
            rate = self._repository.get_carrier_rate(carrier_rate_id, tenant_id)
            if rate is None:
                raise CarrierRateNotFoundError(
                    f"Carrier rate {carrier_rate_id!r} does not exist.",
                    details={"carrier_rate_id": carrier_rate_id},
                )
            shipment = rate.rate_irr
            carrier_name = rate.carrier_name
        elif shipment_cost_irr is not None:
            shipment = shipment_cost_irr
        settings = self.get_settings(tenant_id, company_id)
        inputs: list[PackagingQuoteInput] = []
        for item in packaging:
            material = self._require_material(item.material_id, tenant_id)
            try:
                location_id = settings.packaging_location_id
                view = self._engine.get_packaging_product(
                    ProductRef(material.tryton_product_id),
                    company,
                    StockLocationRef(location_id) if location_id is not None else None,
                )
            except HesabYarError as exc:
                raise translate_engine_error(exc) from exc
            if view is None:
                raise MaterialNotFoundError(
                    f"Packaging material {material.name_fa!r} is not in the engine.",
                    details={"material_id": material.id},
                )
            unit_cost = view.cost_price if view.cost_price is not None else Decimal(0)
            on_hand = view.quantity if view.quantity is not None else Decimal(0)
            inputs.append(
                PackagingQuoteInput(
                    material_id=material.id,
                    material_name=material.name_fa,
                    quantity=item.quantity,
                    unit_cost_irr=unit_cost,
                    quantity_on_hand=on_hand,
                    stock_known=location_id is not None,
                )
            )
        return calculator_mod.compute_price_quote(
            PriceQuoteRequest(
                goods_cost_irr=goods_cost_irr,
                packaging=tuple(inputs),
                shipment_cost_irr=shipment,
                carrier_name=carrier_name,
                margin_percent=margin_percent,
                margin_fixed_irr=margin_fixed_irr,
            )
        )

    # -- helpers -----------------------------------------------------------

    def _require_material(self, material_id: str, tenant_id: str) -> PackagingMaterial:
        material = self._repository.get_material(material_id, tenant_id)
        if material is None:
            raise MaterialNotFoundError(
                f"Packaging material {material_id!r} does not exist.",
                details={"material_id": material_id},
            )
        return material

    def _material_id_for_product(
        self, tenant_id: str, company_id: str, product_engine_id: int
    ) -> str | None:
        material = self._repository.get_material_by_product(tenant_id, company_id, product_engine_id)
        return material.id if material is not None else None

    def _consumption_moves_in_window(
        self, company: CompanyRef, settings: PackagingSettings
    ) -> Sequence[StockMoveView]:
        """Done consumption moves in the coverage window (all products)."""
        if settings.packaging_location_id is None or settings.production_location_id is None:
            return []
        today = self._today()
        window_start = FiscalDate(
            today.gregorian_date - datetime.timedelta(days=settings.days_of_cover_window_days)
        )
        return self._engine.list_consumption_moves(
            company,
            StockLocationRef(settings.packaging_location_id),
            StockLocationRef(settings.production_location_id),
            window_start,
            today,
        )

    @staticmethod
    def _consumed_by_product(moves: Sequence[StockMoveView]) -> dict[int, Decimal]:
        consumed: dict[int, Decimal] = {}
        for move in moves:
            pid = move.product.engine_id
            consumed[pid] = consumed.get(pid, Decimal(0)) + move.quantity
        return consumed

    @staticmethod
    def _last_date_by_product(moves: Sequence[StockMoveView]) -> dict[int, FiscalDate]:
        last: dict[int, FiscalDate] = {}
        for move in moves:
            if move.effective_date is None:
                continue
            pid = move.product.engine_id
            if pid not in last or move.effective_date > last[pid]:
                last[pid] = move.effective_date
        return last

    def _point_for_material(
        self, material: PackagingMaterial, company: CompanyRef, settings: PackagingSettings
    ) -> OrderPointView | None:
        if settings.warehouse_id is None:
            return None
        return self._engine.get_order_point(
            ProductRef(material.tryton_product_id),
            StockLocationRef(settings.warehouse_id),
            company,
        )

    def _detail_for_material(
        self,
        material: PackagingMaterial,
        company: CompanyRef,
        settings: PackagingSettings,
        point_view: OrderPointView | None,
        consumed: dict[int, Decimal],
        last_date: dict[int, FiscalDate],
        location_names: dict[int, str],
        *,
        product_view: PackagingProductView | None = None,
        product_view_fetched: bool = False,
    ) -> MaterialDetail:
        point: ReorderPoint | None = None
        if point_view is not None:
            point = ReorderPoint(
                material_id=material.id,
                warehouse_engine_id=point_view.warehouse.engine_id,
                min_quantity=point_view.min_quantity
                if point_view.min_quantity is not None
                else Decimal(0),
                target_quantity=point_view.target_quantity,
                tryton_order_point_id=point_view.ref.engine_id,
                max_quantity=point_view.max_quantity,
            )
        location_id = settings.packaging_location_id
        if location_id is None:
            # No packaging location configured: stock and coverage are
            # unknown, not zero — report them as absent.
            return MaterialDetail(material=material, stock=None, reorder_point=point, coverage=None)
        if product_view_fetched:
            # Batch path (list_materials): a missing view means the
            # product is gone from the engine — report stock unknown,
            # never fall back to a single read (that would reintroduce
            # the N+1 the batch exists to avoid).
            view = product_view
        else:
            try:
                view = self._engine.get_packaging_product(
                    ProductRef(material.tryton_product_id),
                    company,
                    StockLocationRef(location_id),
                )
            except HesabYarError as exc:
                raise translate_engine_error(exc) from exc
        if view is None:
            return MaterialDetail(material=material, stock=None, reorder_point=point, coverage=None)
        on_hand = view.quantity if view.quantity is not None else Decimal(0)
        stock = MaterialStock(
            material_id=material.id,
            quantity=on_hand,
            unit_cost_irr=view.cost_price,
            location_engine_id=location_id,
            location_name=location_names.get(location_id, ""),
        )
        coverage = coverage_calc.compute_days_of_cover(
            material_id=material.id,
            quantity_on_hand=on_hand,
            consumed_in_window=consumed.get(material.tryton_product_id, Decimal(0)),
            window_days=settings.days_of_cover_window_days,
            last_consumption_date=last_date.get(material.tryton_product_id),
        )
        return MaterialDetail(
            material=material,
            stock=stock,
            reorder_point=point,
            coverage=coverage,
        )
