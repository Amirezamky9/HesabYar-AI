"""Ports (interfaces) for the packaging & shipping context.

The application layer talks to HesabYar-side persistence ONLY through
:class:`PackagingRepositoryPort` and to the engine ONLY through
:class:`hesabyar.domain.accounting.ports.AccountingEnginePort` —
never infrastructure (spec §6.3; the architecture guards enforce it).

The repository owns the HesabYar-side records (catalog, consumption
trace, shipment costs, carrier rates, counts, cost centres,
settings, supply watermark). Everything the engine owns (products,
quantities, cost prices, order points, moves, purchase requests,
inventories, analytic accounts/lines, shipment costs) is read or
written through the engine port — the repository never mirrors
engine state.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from hesabyar.domain.packaging.models import (
    CarrierRate,
    ConsumptionRecord,
    InventoryCount,
    OrderCostCenter,
    PackagingMaterial,
    PackagingSettings,
    ShipmentCostRecord,
)


class PackagingRepositoryPort(ABC):
    """Abstract port for HesabYar-side packaging records."""

    # -- materials ---------------------------------------------------------

    @abstractmethod
    def list_materials(self, tenant_id: str, company_id: str) -> Sequence[PackagingMaterial]:
        """All materials of one company (active and inactive, by name)."""
        ...

    @abstractmethod
    def get_material(self, material_id: str, tenant_id: str) -> PackagingMaterial | None:
        """One material for the owning tenant, or None."""
        ...

    @abstractmethod
    def get_material_by_product(
        self, tenant_id: str, company_id: str, tryton_product_id: int
    ) -> PackagingMaterial | None:
        """The material row for one engine product, or None."""
        ...

    @abstractmethod
    def save_material(self, material: PackagingMaterial) -> PackagingMaterial:
        """Insert a new material; returns it with ``created_at`` set."""
        ...

    @abstractmethod
    def update_material(self, material: PackagingMaterial) -> PackagingMaterial:
        """Persist a changed material (name/code/active); returns it."""
        ...

    # -- consumption -------------------------------------------------------

    @abstractmethod
    def get_consumption_by_key(
        self, tenant_id: str, idempotency_key: str
    ) -> ConsumptionRecord | None:
        """The consumption with this idempotency key, or None (replay check)."""
        ...

    @abstractmethod
    def save_consumption(self, record: ConsumptionRecord) -> ConsumptionRecord:
        """Insert a new consumption record; returns it with ``created_at`` set."""
        ...

    @abstractmethod
    def update_consumption(self, record: ConsumptionRecord) -> ConsumptionRecord:
        """Persist a changed consumption record (claim completion); returns it."""
        ...

    @abstractmethod
    def delete_consumption(self, record_id: str, tenant_id: str) -> None:
        """Delete a consumption record (claim rollback when the engine move failed)."""
        ...

    @abstractmethod
    def list_consumptions(
        self,
        tenant_id: str,
        company_id: str,
        *,
        order_ref: str | None = None,
        material_id: str | None = None,
    ) -> Sequence[ConsumptionRecord]:
        """Consumption records of one company, newest first, optionally filtered."""
        ...

    # -- shipment costs ----------------------------------------------------

    @abstractmethod
    def save_shipment_cost(self, record: ShipmentCostRecord) -> ShipmentCostRecord:
        """Insert a new shipment-cost record; returns it with ``created_at`` set."""
        ...

    @abstractmethod
    def list_shipment_costs(
        self, tenant_id: str, company_id: str, *, order_ref: str | None = None
    ) -> Sequence[ShipmentCostRecord]:
        """Shipment-cost records of one company, newest first, optionally filtered."""
        ...

    # -- carrier rates -----------------------------------------------------

    @abstractmethod
    def list_carrier_rates(
        self, tenant_id: str, company_id: str, *, active_only: bool = False
    ) -> Sequence[CarrierRate]:
        """Carrier rates of one company, newest first."""
        ...

    @abstractmethod
    def get_carrier_rate(self, rate_id: str, tenant_id: str) -> CarrierRate | None:
        """One carrier rate for the owning tenant, or None."""
        ...

    @abstractmethod
    def save_carrier_rate(self, rate: CarrierRate) -> CarrierRate:
        """Insert a new carrier rate; returns it with ``created_at`` set."""
        ...

    @abstractmethod
    def update_carrier_rate(self, rate: CarrierRate) -> CarrierRate:
        """Persist a changed carrier rate (rate/active); returns it."""
        ...

    # -- counts ------------------------------------------------------------

    @abstractmethod
    def save_count(self, count: InventoryCount) -> InventoryCount:
        """Insert a new count; returns it with ``created_at`` set."""
        ...

    @abstractmethod
    def get_count(self, count_id: str, tenant_id: str) -> InventoryCount | None:
        """One count for the owning tenant, or None."""
        ...

    @abstractmethod
    def list_counts(self, tenant_id: str, company_id: str) -> Sequence[InventoryCount]:
        """Counts of one company, newest first."""
        ...

    @abstractmethod
    def update_count(self, count: InventoryCount) -> InventoryCount:
        """Persist a changed count (state/lines); returns it."""
        ...

    # -- cost centres ------------------------------------------------------

    @abstractmethod
    def get_cost_center(
        self, tenant_id: str, company_id: str, order_ref: str
    ) -> OrderCostCenter | None:
        """The cost centre for one order, or None."""
        ...

    @abstractmethod
    def save_cost_center(self, center: OrderCostCenter) -> OrderCostCenter:
        """Insert a new cost centre; returns it with ``created_at`` set."""
        ...

    # -- settings + supply watermark ---------------------------------------

    @abstractmethod
    def get_settings(self, tenant_id: str, company_id: str) -> PackagingSettings | None:
        """The company's packaging settings, or None (defaults apply)."""
        ...

    @abstractmethod
    def list_configured_companies(self) -> Sequence[tuple[str, str]]:
        """All (tenant_id, company_id) pairs with saved packaging settings.

        The daily supply-check job discovers its companies here: a
        company is in scope once its settings are saved (provisioned).
        """
        ...

    @abstractmethod
    def save_settings(self, settings: PackagingSettings) -> PackagingSettings:
        """Insert or replace the company's packaging settings."""
        ...

    @abstractmethod
    def get_supply_watermark(self, tenant_id: str, company_id: str) -> int:
        """Highest purchase-request engine id the supply check has seen (0 = none)."""
        ...

    @abstractmethod
    def save_supply_watermark(self, tenant_id: str, company_id: str, watermark: int) -> None:
        """Persist the supply-check watermark (monotonic: never moves backwards)."""
        ...
