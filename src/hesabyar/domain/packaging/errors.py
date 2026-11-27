"""Domain errors for the packaging & shipping context (US-515).

Codes are stable strings surfaced through the RFC 9457 envelope; the
application layer maps them to HTTP statuses (see
``hesabyar.application.errors``).
"""

from __future__ import annotations

from hesabyar.domain.common.errors import DomainError


class PackagingError(DomainError):
    """Base class for packaging-domain errors."""


class MaterialNotFoundError(PackagingError):
    """A packaging material id does not exist for this tenant/company."""

    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__(message, code="PACKAGING_MATERIAL_NOT_FOUND", details=details)


class InvalidMaterialError(PackagingError):
    """A material definition violates an invariant (bad cost method, ...)."""

    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__(message, code="PACKAGING_INVALID_MATERIAL", details=details)


class FifoUnavailableError(PackagingError):
    """FIFO costing was requested but ``product_cost_fifo`` is not activated.

    Tryton's base ``COST_PRICE_METHODS`` are only ``fixed``/``average``;
    FIFO arrives with the ``product_cost_fifo`` module, which is not
    installed on the dev database. Average costing works; FIFO needs
    the module installed + activated (see docs/packaging.md §6).
    """

    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__(message, code="PACKAGING_FIFO_UNAVAILABLE", details=details)


class InvalidReorderPointError(PackagingError):
    """A reorder point violates min <= target (<= max for internal points)."""

    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__(message, code="PACKAGING_INVALID_REORDER_POINT", details=details)


class InvalidConsumptionError(PackagingError):
    """A consumption record violates an invariant (quantity, refs, ...)."""

    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__(message, code="PACKAGING_INVALID_CONSUMPTION", details=details)


class InvalidShipmentCostError(PackagingError):
    """A shipment-cost record violates an invariant (method/figures)."""

    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__(message, code="PACKAGING_INVALID_SHIPMENT_COST", details=details)


class InvalidPriceQuoteError(PackagingError):
    """A price-quote request violates an invariant (margin, amounts)."""

    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__(message, code="PACKAGING_INVALID_PRICE_QUOTE", details=details)


class InvalidCountError(PackagingError):
    """An inventory count violates an invariant (state, quantities)."""

    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__(message, code="PACKAGING_INVALID_COUNT", details=details)


class CountNotFoundError(PackagingError):
    """An inventory count id does not exist for this tenant/company."""

    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__(message, code="PACKAGING_COUNT_NOT_FOUND", details=details)


class CarrierRateNotFoundError(PackagingError):
    """A carrier-rate id does not exist for this tenant/company."""

    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__(message, code="PACKAGING_CARRIER_RATE_NOT_FOUND", details=details)


class InvalidCarrierRateError(PackagingError):
    """A carrier rate violates an invariant (negative rate, ...)."""

    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__(message, code="PACKAGING_INVALID_CARRIER_RATE", details=details)


class CostCenterExistsError(PackagingError):
    """An analytic cost centre already exists for this order."""

    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__(message, code="PACKAGING_COST_CENTER_EXISTS", details=details)


class PackagingSetupError(PackagingError):
    """Packaging is not set up for this company (locations/category/root)."""

    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__(message, code="PACKAGING_SETUP_REQUIRED", details=details)
