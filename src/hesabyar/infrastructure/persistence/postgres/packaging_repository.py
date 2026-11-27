"""PostgreSQL implementation of the packaging repository port (US-515).

Implements ``domain/packaging/ports.py`` (``PackagingRepositoryPort``)
over the ``packaging_*`` tables. This is the ONLY place that knows
the packaging SQLAlchemy models — the application layer talks to the
port, never to these tables (spec §6.3).

Persistence contract (same as every other writer in the codebase):
``save_*`` / ``update_*`` flush so ids/timestamps exist but do NOT
commit — the caller (REST route / worker) owns the transaction.

The repository owns ONLY HesabYar-side records (catalog, consumption
trace, shipment costs, carrier rates, counts, cost centres, settings,
supply watermark). Engine state (products, quantities, cost prices,
order points, moves, purchase requests, inventories, analytic
accounts/lines) is never mirrored here — the application layer reads
it through the engine port.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.packaging.models import (
    CarrierRate,
    ConsumptionRecord,
    ConsumptionSource,
    CostMethod,
    CountLine,
    CountState,
    InventoryCount,
    OrderCostCenter,
    PackagingMaterial,
    PackagingSettings,
    ShipmentCostMethod,
    ShipmentCostRecord,
)
from hesabyar.domain.packaging.ports import PackagingRepositoryPort
from hesabyar.infrastructure.persistence.postgres.models import (
    PackagingCarrierRateModel,
    PackagingConsumptionModel,
    PackagingCountModel,
    PackagingMaterialModel,
    PackagingOrderCostCenterModel,
    PackagingSettingsModel,
    PackagingShipmentCostModel,
    PackagingSupplyWatermarkModel,
)


def _material_from_row(row: PackagingMaterialModel) -> PackagingMaterial:
    return PackagingMaterial(
        id=row.id,
        tenant_id=row.tenant_id,
        company_id=row.company_id,
        tryton_product_id=row.tryton_product_id,
        tryton_template_id=row.tryton_template_id,
        name_fa=row.name_fa,
        cost_method=CostMethod(row.cost_method),
        code=row.code,
        uom_name=row.uom_name,
        is_active=row.is_active,
        created_at=row.created_at,
    )


def _consumption_from_row(row: PackagingConsumptionModel) -> ConsumptionRecord:
    return ConsumptionRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        company_id=row.company_id,
        material_id=row.material_id,
        order_ref=row.order_ref,
        shipment_ref=row.shipment_ref,
        quantity=row.quantity,
        unit_cost_irr=row.unit_cost_irr,
        total_cost_irr=row.total_cost_irr,
        rounding_delta_irr=row.rounding_delta_irr,
        idempotency_key=row.idempotency_key,
        source=ConsumptionSource(row.source),
        analytic_account_engine_id=row.analytic_account_engine_id,
        tryton_move_id=row.tryton_move_id,
        consumed_on=FiscalDate.from_gregorian(row.consumed_on)
        if row.consumed_on is not None
        else None,
        created_at=row.created_at,
    )


def _shipment_cost_from_row(row: PackagingShipmentCostModel) -> ShipmentCostRecord:
    return ShipmentCostRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        company_id=row.company_id,
        order_ref=row.order_ref,
        method=ShipmentCostMethod(row.method),
        invoiced_irr=row.invoiced_irr,
        actual_irr=row.actual_irr,
        carrier_name=row.carrier_name,
        tryton_sale_id=row.tryton_sale_id,
        tryton_shipment_id=row.tryton_shipment_id,
        analytic_account_engine_id=row.analytic_account_engine_id,
        created_at=row.created_at,
    )


def _carrier_rate_from_row(row: PackagingCarrierRateModel) -> CarrierRate:
    return CarrierRate(
        id=row.id,
        tenant_id=row.tenant_id,
        company_id=row.company_id,
        carrier_name=row.carrier_name,
        rate_irr=row.rate_irr,
        basis=row.basis,
        tryton_carrier_id=row.tryton_carrier_id,
        effective_from=FiscalDate.from_gregorian(row.effective_from)
        if row.effective_from is not None
        else None,
        is_active=row.is_active,
        created_at=row.created_at,
    )


def _count_line_from_json(item: dict[str, Any]) -> CountLine:
    counted = item.get("counted_quantity")
    return CountLine(
        material_id=str(item["material_id"]),
        material_name=str(item.get("material_name") or ""),
        expected_quantity=Decimal(str(item["expected_quantity"])),
        counted_quantity=Decimal(str(counted)) if counted is not None else None,
        tryton_line_id=item.get("tryton_line_id"),
    )


def _count_line_to_json(line: CountLine) -> dict[str, Any]:
    return {
        "material_id": line.material_id,
        "material_name": line.material_name,
        "expected_quantity": str(line.expected_quantity),
        "counted_quantity": (
            str(line.counted_quantity) if line.counted_quantity is not None else None
        ),
        "tryton_line_id": line.tryton_line_id,
    }


def _count_from_row(row: PackagingCountModel) -> InventoryCount:
    return InventoryCount(
        id=row.id,
        tenant_id=row.tenant_id,
        company_id=row.company_id,
        tryton_inventory_id=row.tryton_inventory_id,
        location_engine_id=row.location_engine_id,
        location_name=row.location_name,
        count_date=FiscalDate.from_gregorian(row.count_date),
        state=CountState(row.state),
        counted_by=row.counted_by,
        lines=tuple(_count_line_from_json(item) for item in row.lines),
        created_at=row.created_at,
    )


def _cost_center_from_row(row: PackagingOrderCostCenterModel) -> OrderCostCenter:
    return OrderCostCenter(
        id=row.id,
        tenant_id=row.tenant_id,
        company_id=row.company_id,
        order_ref=row.order_ref,
        analytic_account_engine_id=row.analytic_account_engine_id,
        created_at=row.created_at,
    )


def _settings_from_row(row: PackagingSettingsModel) -> PackagingSettings:
    return PackagingSettings(
        tenant_id=row.tenant_id,
        company_id=row.company_id,
        cost_method=CostMethod(row.cost_method),
        shipment_cost_method=ShipmentCostMethod(row.shipment_cost_method),
        days_of_cover_window_days=row.days_of_cover_window_days,
        low_stock_alert_days=row.low_stock_alert_days,
        packaging_location_id=row.packaging_location_id,
        production_location_id=row.production_location_id,
        warehouse_id=row.warehouse_id,
        packaging_category_id=row.packaging_category_id,
        analytic_root_id=row.analytic_root_id,
        updated_at=row.updated_at,
    )


class SqlAlchemyPackagingRepository(PackagingRepositoryPort):
    """SQLAlchemy implementation of the packaging repository port."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- materials ---------------------------------------------------------

    def list_materials(self, tenant_id: str, company_id: str) -> Sequence[PackagingMaterial]:
        stmt = (
            select(PackagingMaterialModel)
            .where(
                PackagingMaterialModel.tenant_id == tenant_id,
                PackagingMaterialModel.company_id == company_id,
            )
            .order_by(PackagingMaterialModel.name_fa.asc())
        )
        return [_material_from_row(row) for row in self._session.scalars(stmt)]

    def get_material(self, material_id: str, tenant_id: str) -> PackagingMaterial | None:
        row = self._session.get(PackagingMaterialModel, material_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        return _material_from_row(row)

    def get_material_by_product(
        self, tenant_id: str, company_id: str, tryton_product_id: int
    ) -> PackagingMaterial | None:
        stmt = select(PackagingMaterialModel).where(
            PackagingMaterialModel.tenant_id == tenant_id,
            PackagingMaterialModel.company_id == company_id,
            PackagingMaterialModel.tryton_product_id == tryton_product_id,
        )
        row = self._session.scalars(stmt).first()
        return _material_from_row(row) if row is not None else None

    def save_material(self, material: PackagingMaterial) -> PackagingMaterial:
        row = PackagingMaterialModel(
            id=material.id or str(uuid.uuid4()),
            tenant_id=material.tenant_id,
            company_id=material.company_id,
            tryton_product_id=material.tryton_product_id,
            tryton_template_id=material.tryton_template_id,
            name_fa=material.name_fa,
            cost_method=material.cost_method.value,
            code=material.code,
            uom_name=material.uom_name,
            is_active=material.is_active,
        )
        self._session.add(row)
        self._session.flush()
        return _material_from_row(row)

    def update_material(self, material: PackagingMaterial) -> PackagingMaterial:
        row = self._session.get(PackagingMaterialModel, material.id)
        if row is None or row.tenant_id != material.tenant_id:
            raise KeyError(f"Packaging material {material.id} does not exist.")
        row.name_fa = material.name_fa
        row.code = material.code
        row.uom_name = material.uom_name
        row.is_active = material.is_active
        self._session.flush()
        return _material_from_row(row)

    # -- consumption -------------------------------------------------------

    def get_consumption_by_key(
        self, tenant_id: str, idempotency_key: str
    ) -> ConsumptionRecord | None:
        stmt = select(PackagingConsumptionModel).where(
            PackagingConsumptionModel.tenant_id == tenant_id,
            PackagingConsumptionModel.idempotency_key == idempotency_key,
        )
        row = self._session.scalars(stmt).first()
        return _consumption_from_row(row) if row is not None else None

    def save_consumption(self, record: ConsumptionRecord) -> ConsumptionRecord:
        row = PackagingConsumptionModel(
            id=record.id or str(uuid.uuid4()),
            tenant_id=record.tenant_id,
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
            consumed_on=record.consumed_on.gregorian_date
            if record.consumed_on is not None
            else None,
        )
        self._session.add(row)
        self._session.flush()
        return _consumption_from_row(row)

    def update_consumption(self, record: ConsumptionRecord) -> ConsumptionRecord:
        row = self._session.get(PackagingConsumptionModel, record.id)
        if row is None or row.tenant_id != record.tenant_id:
            raise KeyError(f"Packaging consumption {record.id} does not exist.")
        row.tryton_move_id = record.tryton_move_id
        row.unit_cost_irr = record.unit_cost_irr
        row.total_cost_irr = record.total_cost_irr
        row.rounding_delta_irr = record.rounding_delta_irr
        row.analytic_account_engine_id = record.analytic_account_engine_id
        row.consumed_on = (
            record.consumed_on.gregorian_date if record.consumed_on is not None else None
        )
        self._session.flush()
        return _consumption_from_row(row)

    def delete_consumption(self, record_id: str, tenant_id: str) -> None:
        row = self._session.get(PackagingConsumptionModel, record_id)
        if row is None or row.tenant_id != tenant_id:
            return
        self._session.delete(row)
        self._session.flush()

    def list_consumptions(
        self,
        tenant_id: str,
        company_id: str,
        *,
        order_ref: str | None = None,
        material_id: str | None = None,
    ) -> Sequence[ConsumptionRecord]:
        stmt = (
            select(PackagingConsumptionModel)
            .where(
                PackagingConsumptionModel.tenant_id == tenant_id,
                PackagingConsumptionModel.company_id == company_id,
            )
            .order_by(PackagingConsumptionModel.created_at.desc())
        )
        if order_ref is not None:
            stmt = stmt.where(PackagingConsumptionModel.order_ref == order_ref)
        if material_id is not None:
            stmt = stmt.where(PackagingConsumptionModel.material_id == material_id)
        return [_consumption_from_row(row) for row in self._session.scalars(stmt)]

    # -- shipment costs ----------------------------------------------------

    def save_shipment_cost(self, record: ShipmentCostRecord) -> ShipmentCostRecord:
        row = PackagingShipmentCostModel(
            id=record.id or str(uuid.uuid4()),
            tenant_id=record.tenant_id,
            company_id=record.company_id,
            order_ref=record.order_ref,
            method=record.method.value,
            invoiced_irr=record.invoiced_irr,
            actual_irr=record.actual_irr,
            carrier_name=record.carrier_name,
            tryton_sale_id=record.tryton_sale_id,
            tryton_shipment_id=record.tryton_shipment_id,
            analytic_account_engine_id=record.analytic_account_engine_id,
        )
        self._session.add(row)
        self._session.flush()
        return _shipment_cost_from_row(row)

    def list_shipment_costs(
        self, tenant_id: str, company_id: str, *, order_ref: str | None = None
    ) -> Sequence[ShipmentCostRecord]:
        stmt = (
            select(PackagingShipmentCostModel)
            .where(
                PackagingShipmentCostModel.tenant_id == tenant_id,
                PackagingShipmentCostModel.company_id == company_id,
            )
            .order_by(PackagingShipmentCostModel.created_at.desc())
        )
        if order_ref is not None:
            stmt = stmt.where(PackagingShipmentCostModel.order_ref == order_ref)
        return [_shipment_cost_from_row(row) for row in self._session.scalars(stmt)]

    # -- carrier rates -----------------------------------------------------

    def list_carrier_rates(
        self, tenant_id: str, company_id: str, *, active_only: bool = False
    ) -> Sequence[CarrierRate]:
        stmt = (
            select(PackagingCarrierRateModel)
            .where(
                PackagingCarrierRateModel.tenant_id == tenant_id,
                PackagingCarrierRateModel.company_id == company_id,
            )
            .order_by(PackagingCarrierRateModel.created_at.desc())
        )
        if active_only:
            stmt = stmt.where(PackagingCarrierRateModel.is_active.is_(True))
        return [_carrier_rate_from_row(row) for row in self._session.scalars(stmt)]

    def get_carrier_rate(self, rate_id: str, tenant_id: str) -> CarrierRate | None:
        row = self._session.get(PackagingCarrierRateModel, rate_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        return _carrier_rate_from_row(row)

    def save_carrier_rate(self, rate: CarrierRate) -> CarrierRate:
        row = PackagingCarrierRateModel(
            id=rate.id or str(uuid.uuid4()),
            tenant_id=rate.tenant_id,
            company_id=rate.company_id,
            carrier_name=rate.carrier_name,
            rate_irr=rate.rate_irr,
            basis=rate.basis,
            tryton_carrier_id=rate.tryton_carrier_id,
            effective_from=rate.effective_from.gregorian_date
            if rate.effective_from is not None
            else None,
            is_active=rate.is_active,
        )
        self._session.add(row)
        self._session.flush()
        return _carrier_rate_from_row(row)

    def update_carrier_rate(self, rate: CarrierRate) -> CarrierRate:
        row = self._session.get(PackagingCarrierRateModel, rate.id)
        if row is None or row.tenant_id != rate.tenant_id:
            raise KeyError(f"Carrier rate {rate.id} does not exist.")
        row.carrier_name = rate.carrier_name
        row.rate_irr = rate.rate_irr
        row.basis = rate.basis
        row.tryton_carrier_id = rate.tryton_carrier_id
        row.effective_from = (
            rate.effective_from.gregorian_date if rate.effective_from is not None else None
        )
        row.is_active = rate.is_active
        self._session.flush()
        return _carrier_rate_from_row(row)

    # -- counts ------------------------------------------------------------

    def save_count(self, count: InventoryCount) -> InventoryCount:
        row = PackagingCountModel(
            id=count.id or str(uuid.uuid4()),
            tenant_id=count.tenant_id,
            company_id=count.company_id,
            tryton_inventory_id=count.tryton_inventory_id,
            location_engine_id=count.location_engine_id,
            location_name=count.location_name,
            count_date=count.count_date.gregorian_date,
            state=count.state.value,
            counted_by=count.counted_by,
            lines=[_count_line_to_json(line) for line in count.lines],
        )
        self._session.add(row)
        self._session.flush()
        return _count_from_row(row)

    def get_count(self, count_id: str, tenant_id: str) -> InventoryCount | None:
        row = self._session.get(PackagingCountModel, count_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        return _count_from_row(row)

    def list_counts(self, tenant_id: str, company_id: str) -> Sequence[InventoryCount]:
        stmt = (
            select(PackagingCountModel)
            .where(
                PackagingCountModel.tenant_id == tenant_id,
                PackagingCountModel.company_id == company_id,
            )
            .order_by(PackagingCountModel.created_at.desc())
        )
        return [_count_from_row(row) for row in self._session.scalars(stmt)]

    def update_count(self, count: InventoryCount) -> InventoryCount:
        row = self._session.get(PackagingCountModel, count.id)
        if row is None or row.tenant_id != count.tenant_id:
            raise KeyError(f"Packaging count {count.id} does not exist.")
        row.state = count.state.value
        row.counted_by = count.counted_by
        row.lines = [_count_line_to_json(line) for line in count.lines]
        self._session.flush()
        return _count_from_row(row)

    # -- cost centres ------------------------------------------------------

    def get_cost_center(
        self, tenant_id: str, company_id: str, order_ref: str
    ) -> OrderCostCenter | None:
        stmt = select(PackagingOrderCostCenterModel).where(
            PackagingOrderCostCenterModel.tenant_id == tenant_id,
            PackagingOrderCostCenterModel.company_id == company_id,
            PackagingOrderCostCenterModel.order_ref == order_ref,
        )
        row = self._session.scalars(stmt).first()
        return _cost_center_from_row(row) if row is not None else None

    def save_cost_center(self, center: OrderCostCenter) -> OrderCostCenter:
        row = PackagingOrderCostCenterModel(
            id=center.id or str(uuid.uuid4()),
            tenant_id=center.tenant_id,
            company_id=center.company_id,
            order_ref=center.order_ref,
            analytic_account_engine_id=center.analytic_account_engine_id,
        )
        self._session.add(row)
        self._session.flush()
        return _cost_center_from_row(row)

    # -- settings + supply watermark ---------------------------------------

    def get_settings(self, tenant_id: str, company_id: str) -> PackagingSettings | None:
        stmt = select(PackagingSettingsModel).where(
            PackagingSettingsModel.tenant_id == tenant_id,
            PackagingSettingsModel.company_id == company_id,
        )
        row = self._session.scalars(stmt).first()
        return _settings_from_row(row) if row is not None else None

    def list_configured_companies(self) -> Sequence[tuple[str, str]]:
        stmt = select(
            PackagingSettingsModel.tenant_id, PackagingSettingsModel.company_id
        ).order_by(PackagingSettingsModel.tenant_id, PackagingSettingsModel.company_id)
        return [(tenant_id, company_id) for tenant_id, company_id in self._session.execute(stmt)]

    def save_settings(self, settings: PackagingSettings) -> PackagingSettings:
        stmt = select(PackagingSettingsModel).where(
            PackagingSettingsModel.tenant_id == settings.tenant_id,
            PackagingSettingsModel.company_id == settings.company_id,
        )
        row = self._session.scalars(stmt).first()
        if row is None:
            row = PackagingSettingsModel(
                id=str(uuid.uuid4()),
                tenant_id=settings.tenant_id,
                company_id=settings.company_id,
            )
            self._session.add(row)
        row.cost_method = settings.cost_method.value
        row.shipment_cost_method = settings.shipment_cost_method.value
        row.days_of_cover_window_days = settings.days_of_cover_window_days
        row.low_stock_alert_days = settings.low_stock_alert_days
        row.packaging_location_id = settings.packaging_location_id
        row.production_location_id = settings.production_location_id
        row.warehouse_id = settings.warehouse_id
        row.packaging_category_id = settings.packaging_category_id
        row.analytic_root_id = settings.analytic_root_id
        row.updated_at = datetime.datetime.now(datetime.UTC)
        self._session.flush()
        return _settings_from_row(row)

    def get_supply_watermark(self, tenant_id: str, company_id: str) -> int:
        stmt = select(PackagingSupplyWatermarkModel).where(
            PackagingSupplyWatermarkModel.tenant_id == tenant_id,
            PackagingSupplyWatermarkModel.company_id == company_id,
        )
        row = self._session.scalars(stmt).first()
        return row.watermark if row is not None else 0

    def save_supply_watermark(self, tenant_id: str, company_id: str, watermark: int) -> None:
        stmt = select(PackagingSupplyWatermarkModel).where(
            PackagingSupplyWatermarkModel.tenant_id == tenant_id,
            PackagingSupplyWatermarkModel.company_id == company_id,
        )
        row = self._session.scalars(stmt).first()
        if row is None:
            row = PackagingSupplyWatermarkModel(
                id=str(uuid.uuid4()),
                tenant_id=tenant_id,
                company_id=company_id,
                watermark=watermark,
            )
            self._session.add(row)
        elif watermark > row.watermark:
            # Monotonic: the watermark never moves backwards.
            row.watermark = watermark
        self._session.flush()
