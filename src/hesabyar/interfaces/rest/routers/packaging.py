"""Packaging & shipping routes (US-515, Phase 5 wave 2, stream K1).

Thin routes over the packaging query/command services: no SQL here,
no coverage math here — the services own that.

Reads need ``packaging:read``; writes need ``packaging:manage``.
The company travels as a ``company_id`` query parameter on GETs and
a body field on writes (row id or engine id, resolved via
``require_company_row_by_id``; unknown company → 404, a key
restricted to another company → 403). Materials belong to the
caller's tenant: an id of another tenant answers 404 as if it did
not exist.

The quote endpoint (``POST /packaging/price-calculator/quote``) is
read-only computation — it needs only ``packaging:read`` and never
writes to an invoice.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session

from hesabyar.application.commands.packaging import PackagingCommandService
from hesabyar.application.queries.packaging import PackagingQueryService, QuotePackagingItem
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.packaging.models import (
    ConsumptionSource,
    CostMethod,
    ShipmentCostMethod,
)
from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.interfaces.rest.dependencies import (
    company_ref_for_row,
    get_db_session,
    get_packaging_command_service,
    get_packaging_query_service,
    require_company_row_by_id,
    require_scopes,
)
from hesabyar.interfaces.rest.errors import ApiError, translate_errors
from hesabyar.interfaces.rest.pagination import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    paginate,
    parse_sort_param,
    reject_unknown_params,
)
from hesabyar.interfaces.rest.schemas.packaging import (
    CarrierRateResponse,
    ComputeQuoteRequest,
    ConsumptionResponse,
    CreateCarrierRateRequest,
    CreateCostCenterRequest,
    CreateCountRequest,
    CreateMaterialRequest,
    DaysOfCoverResponse,
    InventoryCountResponse,
    MaterialDetailResponse,
    OrderCostCenterResponse,
    OrderCostReportResponse,
    PackagingSettingsResponse,
    PriceQuoteResponse,
    RecordConsumptionRequest,
    RecordShipmentCostRequest,
    RunSupplyCheckRequest,
    SaveSettingsRequest,
    SetCountLineRequest,
    SetReorderPointRequest,
    ShipmentCostResponse,
    StockLocationResponse,
    SupplyCheckReportResponse,
    UpdateCarrierRateRequest,
    UpdateMaterialRequest,
)
from hesabyar.interfaces.rest.schemas.pagination import PaginatedResponse

router = APIRouter(tags=["packaging"])

_COST_METHODS = {method.value for method in CostMethod}
_SHIPMENT_COST_METHODS = {method.value for method in ShipmentCostMethod}

_MATERIALS_QUERY_PARAMS = {"company_id", "sort", "limit", "cursor"}
_CONSUMPTIONS_QUERY_PARAMS = {"company_id", "order_ref", "material_id", "sort", "limit", "cursor"}
_SHIPMENT_COSTS_QUERY_PARAMS = {"company_id", "order_ref", "sort", "limit", "cursor"}
_CARRIER_RATES_QUERY_PARAMS = {"company_id", "active_only", "sort", "limit", "cursor"}
_COUNTS_QUERY_PARAMS = {"company_id", "sort", "limit", "cursor"}

_MATERIAL_SORT_KEYS: dict[str, Callable[[MaterialDetailResponse], str]] = {
    "name": lambda item: item.material.name_fa,
    "created_at": lambda item: (
        item.material.created_at.isoformat() if item.material.created_at is not None else ""
    ),
}
_CONSUMPTION_SORT_KEYS: dict[str, Callable[[ConsumptionResponse], str]] = {
    "created_at": lambda item: item.created_at.isoformat() if item.created_at is not None else "",
    "order_ref": lambda item: item.order_ref,
}
_SHIPMENT_SORT_KEYS: dict[str, Callable[[ShipmentCostResponse], str]] = {
    "created_at": lambda item: item.created_at.isoformat() if item.created_at is not None else "",
    "order_ref": lambda item: item.order_ref,
}
_RATE_SORT_KEYS: dict[str, Callable[[CarrierRateResponse], str]] = {
    "created_at": lambda item: item.created_at.isoformat() if item.created_at is not None else "",
    "carrier_name": lambda item: item.carrier_name,
}
_COUNT_SORT_KEYS: dict[str, Callable[[InventoryCountResponse], str]] = {
    "created_at": lambda item: item.created_at.isoformat() if item.created_at is not None else "",
    "count_date": lambda item: item.count_date.isoformat(),
}


def _parse_enum(value: str | None, allowed: set[str], param: str) -> str | None:
    """Parse an enum body value, failing loudly on unknown labels."""
    if value is None:
        return None
    if value not in allowed:
        raise ApiError(
            status_code=400,
            code="INVALID_FILTER",
            detail=f"Unknown {param} {value!r}. Allowed: {', '.join(sorted(allowed))}.",
        )
    return value


def _to_fiscal(day: date | None) -> FiscalDate | None:
    """Convert a Gregorian request date to FiscalDate (None stays None)."""
    return FiscalDate.from_gregorian(day) if day is not None else None


# -- locations ---------------------------------------------------------------


@router.get(
    "/packaging/locations",
    response_model=list[StockLocationResponse],
    summary="List the engine's stock locations (for the settings UI)",
)
def list_locations(
    company_id: str = Query(description="API company id (row id or engine id)"),
    client: AuthenticatedClient = Depends(require_scopes("packaging:read")),
    session: Session = Depends(get_db_session),
    service: PackagingQueryService = Depends(get_packaging_query_service),
) -> list[StockLocationResponse]:
    """Return the engine's stock locations for the company."""
    row = require_company_row_by_id(session, client, company_id)
    with translate_errors():
        locations = service.list_locations(company_ref_for_row(row))
    return [StockLocationResponse.from_view(location) for location in locations]


# -- settings ----------------------------------------------------------------


@router.get(
    "/packaging/settings",
    response_model=PackagingSettingsResponse,
    summary="Get the company's packaging settings (defaults when never configured)",
)
def get_settings(
    company_id: str = Query(description="API company id (row id or engine id)"),
    client: AuthenticatedClient = Depends(require_scopes("packaging:read")),
    session: Session = Depends(get_db_session),
    service: PackagingQueryService = Depends(get_packaging_query_service),
) -> PackagingSettingsResponse:
    """Return the company's packaging settings, or the defaults."""
    row = require_company_row_by_id(session, client, company_id)
    with translate_errors():
        settings = service.get_settings(client.tenant_id, row.id)
    return PackagingSettingsResponse.from_settings(settings)


@router.put(
    "/packaging/settings",
    response_model=PackagingSettingsResponse,
    summary="Insert or replace the company's packaging settings",
)
def save_settings(
    body: SaveSettingsRequest,
    client: AuthenticatedClient = Depends(require_scopes("packaging:manage")),
    session: Session = Depends(get_db_session),
    service: PackagingCommandService = Depends(get_packaging_command_service),
) -> PackagingSettingsResponse:
    """Save the company's packaging settings.

    Location and analytic-root ids are verified against the engine
    before saving (a typo'd id fails fast with 404).
    """
    row = require_company_row_by_id(session, client, body.company_id)
    cost_method = _parse_enum(body.cost_method, _COST_METHODS, "cost_method")
    shipment_method = _parse_enum(
        body.shipment_cost_method, _SHIPMENT_COST_METHODS, "shipment_cost_method"
    )
    with translate_errors():
        settings = service.save_settings(
            tenant_id=client.tenant_id,
            company_id=row.id,
            company=company_ref_for_row(row),
            cost_method=CostMethod(cost_method) if cost_method is not None else None,
            shipment_cost_method=(
                ShipmentCostMethod(shipment_method) if shipment_method is not None else None
            ),
            days_of_cover_window_days=body.days_of_cover_window_days,
            low_stock_alert_days=body.low_stock_alert_days,
            packaging_location_id=body.packaging_location_id,
            production_location_id=body.production_location_id,
            warehouse_id=body.warehouse_id,
            packaging_category_id=body.packaging_category_id,
            analytic_root_id=body.analytic_root_id,
        )
    session.commit()
    return PackagingSettingsResponse.from_settings(settings)


# -- materials ---------------------------------------------------------------


@router.get(
    "/packaging/materials",
    response_model=PaginatedResponse[MaterialDetailResponse],
    summary="List materials with live stock, reorder point and coverage",
)
def list_materials(
    request: Request,
    company_id: str = Query(description="API company id (row id or engine id)"),
    sort: str | None = Query(
        default=None,
        description="Sort field: 'name' (default) or 'created_at'; prefix '-' for descending",
    ),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"),
    cursor: str | None = Query(default=None, description="Opaque cursor from a previous page's 'next_cursor'"),
    client: AuthenticatedClient = Depends(require_scopes("packaging:read")),
    session: Session = Depends(get_db_session),
    service: PackagingQueryService = Depends(get_packaging_query_service),
) -> PaginatedResponse[MaterialDetailResponse]:
    """Return the company's materials, cursor-paginated."""
    reject_unknown_params(request, _MATERIALS_QUERY_PARAMS)
    field, reverse = parse_sort_param(sort, _MATERIAL_SORT_KEYS, default="name")
    row = require_company_row_by_id(session, client, company_id)
    with translate_errors():
        details = service.list_materials(client.tenant_id, company_ref_for_row(row), row.id)
    items = [MaterialDetailResponse.from_detail(detail) for detail in details]
    page, next_cursor = paginate(
        items,
        key_fn=_MATERIAL_SORT_KEYS[field],
        tie_fn=lambda item: item.material.id,
        limit=limit,
        cursor=cursor,
        reverse=reverse,
    )
    return PaginatedResponse(items=page, next_cursor=next_cursor)


@router.post(
    "/packaging/materials",
    response_model=MaterialDetailResponse,
    status_code=201,
    summary="Create a packaging material (engine product + catalog row)",
)
def create_material(
    body: CreateMaterialRequest,
    client: AuthenticatedClient = Depends(require_scopes("packaging:manage")),
    session: Session = Depends(get_db_session),
    service: PackagingCommandService = Depends(get_packaging_command_service),
) -> MaterialDetailResponse:
    """Create a material. FIFO without the product_cost_fifo module → 422."""
    row = require_company_row_by_id(session, client, body.company_id)
    cost_method = _parse_enum(body.cost_method, _COST_METHODS, "cost_method")
    with translate_errors():
        # The command returns the detail built from known facts (zero
        # stock, no consumption) — no engine re-read; the read-back
        # pushed this endpoint to ~29s over the remote DB.
        detail = service.create_material(
            tenant_id=client.tenant_id,
            company=company_ref_for_row(row),
            company_id=row.id,
            name_fa=body.name_fa,
            cost_method=CostMethod(cost_method) if cost_method is not None else None,
            code=body.code,
        )
    session.commit()
    return MaterialDetailResponse.from_detail(detail)


@router.get(
    "/packaging/materials/{material_id}",
    response_model=MaterialDetailResponse,
    summary="Get one material with live stock, reorder point and coverage",
)
def get_material(
    material_id: str,
    company_id: str = Query(description="API company id (row id or engine id)"),
    client: AuthenticatedClient = Depends(require_scopes("packaging:read")),
    session: Session = Depends(get_db_session),
    service: PackagingQueryService = Depends(get_packaging_query_service),
) -> MaterialDetailResponse:
    """Return one material's detail (live engine figures included)."""
    row = require_company_row_by_id(session, client, company_id)
    with translate_errors():
        detail = service.get_material(client.tenant_id, company_ref_for_row(row), row.id, material_id)
    return MaterialDetailResponse.from_detail(detail)


@router.patch(
    "/packaging/materials/{material_id}",
    response_model=MaterialDetailResponse,
    summary="Update a material's catalog row (the engine product is untouched)",
)
def update_material(
    material_id: str,
    body: UpdateMaterialRequest,
    client: AuthenticatedClient = Depends(require_scopes("packaging:manage")),
    session: Session = Depends(get_db_session),
    service: PackagingCommandService = Depends(get_packaging_command_service),
    query: PackagingQueryService = Depends(get_packaging_query_service),
) -> MaterialDetailResponse:
    """Update a material's name/code/active flag."""
    with translate_errors():
        material = service.update_material(
            tenant_id=client.tenant_id,
            material_id=material_id,
            name_fa=body.name_fa,
            code=body.code,
            is_active=body.is_active,
        )
        # The detail needs a company: the material's own company row.
        row = require_company_row_by_id(session, client, material.company_id)
        detail = query.get_material(client.tenant_id, company_ref_for_row(row), row.id, material.id)
    session.commit()
    return MaterialDetailResponse.from_detail(detail)


@router.put(
    "/packaging/materials/{material_id}/reorder-point",
    response_model=MaterialDetailResponse,
    summary="Create or update a material's purchase order point at the warehouse",
)
def set_reorder_point(
    material_id: str,
    body: SetReorderPointRequest,
    client: AuthenticatedClient = Depends(require_scopes("packaging:manage")),
    session: Session = Depends(get_db_session),
    service: PackagingCommandService = Depends(get_packaging_command_service),
    query: PackagingQueryService = Depends(get_packaging_query_service),
) -> MaterialDetailResponse:
    """Set the reorder point (min/target); the daily supply run acts on it."""
    row = require_company_row_by_id(session, client, body.company_id)
    with translate_errors():
        service.set_reorder_point(
            tenant_id=client.tenant_id,
            company=company_ref_for_row(row),
            company_id=row.id,
            material_id=material_id,
            min_quantity=body.min_quantity,
            target_quantity=body.target_quantity,
        )
        detail = query.get_material(client.tenant_id, company_ref_for_row(row), row.id, material_id)
    session.commit()
    return MaterialDetailResponse.from_detail(detail)


@router.get(
    "/packaging/materials/{material_id}/coverage",
    response_model=DaysOfCoverResponse,
    summary="Days-of-cover for one material (live quantity + window consumption)",
)
def get_coverage(
    material_id: str,
    company_id: str = Query(description="API company id (row id or engine id)"),
    client: AuthenticatedClient = Depends(require_scopes("packaging:read")),
    session: Session = Depends(get_db_session),
    service: PackagingQueryService = Depends(get_packaging_query_service),
) -> DaysOfCoverResponse:
    """Return days-of-cover — "when will it run out?"."""
    row = require_company_row_by_id(session, client, company_id)
    with translate_errors():
        coverage = service.get_coverage(client.tenant_id, company_ref_for_row(row), row.id, material_id)
    return DaysOfCoverResponse.from_coverage(coverage)


# -- consumptions ------------------------------------------------------------


@router.post(
    "/packaging/consumptions",
    response_model=ConsumptionResponse,
    status_code=201,
    summary="Record a packaging consumption for an order (idempotent)",
)
def record_consumption(
    body: RecordConsumptionRequest,
    response: Response,
    client: AuthenticatedClient = Depends(require_scopes("packaging:manage")),
    session: Session = Depends(get_db_session),
    service: PackagingCommandService = Depends(get_packaging_command_service),
) -> ConsumptionResponse:
    """Record a consumption. A replay of the same order + shipment +
    material returns the stored record with 200 instead of moving
    stock twice.
    """
    row = require_company_row_by_id(session, client, body.company_id)
    with translate_errors():
        record, created = service.record_consumption(
            tenant_id=client.tenant_id,
            company=company_ref_for_row(row),
            company_id=row.id,
            material_id=body.material_id,
            order_ref=body.order_ref,
            shipment_ref=body.shipment_ref,
            quantity=body.quantity,
            consumed_on=_to_fiscal(body.consumed_on),
            source=ConsumptionSource.MANUAL,
        )
    session.commit()
    response.status_code = 201 if created else 200
    return ConsumptionResponse.from_record(record)


@router.get(
    "/packaging/consumptions",
    response_model=PaginatedResponse[ConsumptionResponse],
    summary="List consumption records (newest first)",
)
def list_consumptions(
    request: Request,
    company_id: str = Query(description="API company id (row id or engine id)"),
    order_ref: str | None = Query(default=None, description="Filter to one order"),
    material_id: str | None = Query(default=None, description="Filter to one material"),
    sort: str | None = Query(
        default=None,
        description="Sort field: 'created_at' (default) or 'order_ref'; prefix '-' for descending",
    ),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"),
    cursor: str | None = Query(default=None, description="Opaque cursor from a previous page's 'next_cursor'"),
    client: AuthenticatedClient = Depends(require_scopes("packaging:read")),
    session: Session = Depends(get_db_session),
    service: PackagingQueryService = Depends(get_packaging_query_service),
) -> PaginatedResponse[ConsumptionResponse]:
    """Return the company's consumption records, cursor-paginated."""
    reject_unknown_params(request, _CONSUMPTIONS_QUERY_PARAMS)
    field, reverse = parse_sort_param(sort, _CONSUMPTION_SORT_KEYS, default="created_at")
    row = require_company_row_by_id(session, client, company_id)
    with translate_errors():
        records = service.list_consumptions(
            client.tenant_id, row.id, order_ref=order_ref, material_id=material_id
        )
    items = [ConsumptionResponse.from_record(record) for record in records]
    page, next_cursor = paginate(
        items,
        key_fn=_CONSUMPTION_SORT_KEYS[field],
        tie_fn=lambda item: item.id,
        limit=limit,
        cursor=cursor,
        reverse=reverse,
    )
    return PaginatedResponse(items=page, next_cursor=next_cursor)


# -- shipment costs ----------------------------------------------------------


@router.post(
    "/packaging/shipment-costs",
    response_model=ShipmentCostResponse,
    status_code=201,
    summary="Record the three shipment-cost figures for one order",
)
def record_shipment_cost(
    body: RecordShipmentCostRequest,
    client: AuthenticatedClient = Depends(require_scopes("packaging:manage")),
    session: Session = Depends(get_db_session),
    service: PackagingCommandService = Depends(get_packaging_command_service),
) -> ShipmentCostResponse:
    """Record a shipment cost. When a Tryton sale is linked, the
    engine's invoiced figure is authoritative — a mismatch is 422.
    """
    row = require_company_row_by_id(session, client, body.company_id)
    method = _parse_enum(body.method, _SHIPMENT_COST_METHODS, "method")
    assert method is not None  # _parse_enum only returns None for a None input
    with translate_errors():
        record = service.record_shipment_cost(
            tenant_id=client.tenant_id,
            company=company_ref_for_row(row),
            company_id=row.id,
            order_ref=body.order_ref,
            method=ShipmentCostMethod(method),
            actual_irr=body.actual_irr,
            invoiced_irr=body.invoiced_irr,
            carrier_name=body.carrier_name,
            tryton_sale_id=body.tryton_sale_id,
            tryton_shipment_id=body.tryton_shipment_id,
        )
    session.commit()
    return ShipmentCostResponse.from_record(record)


@router.get(
    "/packaging/shipment-costs",
    response_model=PaginatedResponse[ShipmentCostResponse],
    summary="List shipment-cost records (newest first)",
)
def list_shipment_costs(
    request: Request,
    company_id: str = Query(description="API company id (row id or engine id)"),
    order_ref: str | None = Query(default=None, description="Filter to one order"),
    sort: str | None = Query(
        default=None,
        description="Sort field: 'created_at' (default) or 'order_ref'; prefix '-' for descending",
    ),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"),
    cursor: str | None = Query(default=None, description="Opaque cursor from a previous page's 'next_cursor'"),
    client: AuthenticatedClient = Depends(require_scopes("packaging:read")),
    session: Session = Depends(get_db_session),
    service: PackagingQueryService = Depends(get_packaging_query_service),
) -> PaginatedResponse[ShipmentCostResponse]:
    """Return the company's shipment-cost records, cursor-paginated."""
    reject_unknown_params(request, _SHIPMENT_COSTS_QUERY_PARAMS)
    field, reverse = parse_sort_param(sort, _SHIPMENT_SORT_KEYS, default="created_at")
    row = require_company_row_by_id(session, client, company_id)
    with translate_errors():
        records = service.list_shipment_costs(client.tenant_id, row.id, order_ref=order_ref)
    items = [ShipmentCostResponse.from_record(record) for record in records]
    page, next_cursor = paginate(
        items,
        key_fn=_SHIPMENT_SORT_KEYS[field],
        tie_fn=lambda item: item.id,
        limit=limit,
        cursor=cursor,
        reverse=reverse,
    )
    return PaginatedResponse(items=page, next_cursor=next_cursor)


# -- carrier rates -----------------------------------------------------------


@router.post(
    "/packaging/carrier-rates",
    response_model=CarrierRateResponse,
    status_code=201,
    summary="Add a user-entered carrier rate (no rate is ever hard-coded)",
)
def create_carrier_rate(
    body: CreateCarrierRateRequest,
    client: AuthenticatedClient = Depends(require_scopes("packaging:manage")),
    session: Session = Depends(get_db_session),
    service: PackagingCommandService = Depends(get_packaging_command_service),
) -> CarrierRateResponse:
    """Add a carrier rate for the price calculator."""
    row = require_company_row_by_id(session, client, body.company_id)
    with translate_errors():
        rate = service.create_carrier_rate(
            tenant_id=client.tenant_id,
            company_id=row.id,
            carrier_name=body.carrier_name,
            rate_irr=body.rate_irr,
            basis=body.basis,
            tryton_carrier_id=body.tryton_carrier_id,
            effective_from=_to_fiscal(body.effective_from),
        )
    session.commit()
    return CarrierRateResponse.from_rate(rate)


@router.get(
    "/packaging/carrier-rates",
    response_model=PaginatedResponse[CarrierRateResponse],
    summary="List carrier rates (newest first)",
)
def list_carrier_rates(
    request: Request,
    company_id: str = Query(description="API company id (row id or engine id)"),
    active_only: bool = Query(default=False, description="When true, only active rates"),
    sort: str | None = Query(
        default=None,
        description="Sort field: 'created_at' (default) or 'carrier_name'; prefix '-' for descending",
    ),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"),
    cursor: str | None = Query(default=None, description="Opaque cursor from a previous page's 'next_cursor'"),
    client: AuthenticatedClient = Depends(require_scopes("packaging:read")),
    session: Session = Depends(get_db_session),
    service: PackagingQueryService = Depends(get_packaging_query_service),
) -> PaginatedResponse[CarrierRateResponse]:
    """Return the company's carrier rates, cursor-paginated."""
    reject_unknown_params(request, _CARRIER_RATES_QUERY_PARAMS)
    field, reverse = parse_sort_param(sort, _RATE_SORT_KEYS, default="created_at")
    row = require_company_row_by_id(session, client, company_id)
    with translate_errors():
        rates = service.list_carrier_rates(client.tenant_id, row.id, active_only=active_only)
    items = [CarrierRateResponse.from_rate(rate) for rate in rates]
    page, next_cursor = paginate(
        items,
        key_fn=_RATE_SORT_KEYS[field],
        tie_fn=lambda item: item.id,
        limit=limit,
        cursor=cursor,
        reverse=reverse,
    )
    return PaginatedResponse(items=page, next_cursor=next_cursor)


@router.patch(
    "/packaging/carrier-rates/{rate_id}",
    response_model=CarrierRateResponse,
    summary="Update a carrier rate's amount or active flag",
)
def update_carrier_rate(
    rate_id: str,
    body: UpdateCarrierRateRequest,
    client: AuthenticatedClient = Depends(require_scopes("packaging:manage")),
    session: Session = Depends(get_db_session),
    service: PackagingCommandService = Depends(get_packaging_command_service),
) -> CarrierRateResponse:
    """Update a carrier rate."""
    with translate_errors():
        rate = service.update_carrier_rate(
            tenant_id=client.tenant_id,
            rate_id=rate_id,
            rate_irr=body.rate_irr,
            is_active=body.is_active,
        )
    session.commit()
    return CarrierRateResponse.from_rate(rate)


# -- order cost centres & cost -----------------------------------------------


@router.post(
    "/packaging/orders/{order_ref}/cost-center",
    response_model=OrderCostCenterResponse,
    status_code=201,
    summary="Create the analytic cost centre for one order (one per order)",
)
def create_cost_center(
    order_ref: str,
    body: CreateCostCenterRequest,
    client: AuthenticatedClient = Depends(require_scopes("packaging:manage")),
    session: Session = Depends(get_db_session),
    service: PackagingCommandService = Depends(get_packaging_command_service),
) -> OrderCostCenterResponse:
    """Create the order's analytic account (a second call for the same
    order is 409).
    """
    row = require_company_row_by_id(session, client, body.company_id)
    with translate_errors():
        center = service.create_cost_center(
            tenant_id=client.tenant_id,
            company=company_ref_for_row(row),
            company_id=row.id,
            order_ref=order_ref,
        )
    session.commit()
    return OrderCostCenterResponse.from_center(center)


@router.get(
    "/packaging/orders/{order_ref}/cost",
    response_model=OrderCostReportResponse,
    summary="Full cost of one order from its analytic cost centre",
)
def get_order_cost(
    order_ref: str,
    company_id: str = Query(description="API company id (row id or engine id)"),
    client: AuthenticatedClient = Depends(require_scopes("packaging:read")),
    session: Session = Depends(get_db_session),
    service: PackagingQueryService = Depends(get_packaging_query_service),
) -> OrderCostReportResponse:
    """Return the order's full cost: goods (analytic) + packaging +
    shipment (three figures) + revenue and margin.
    """
    row = require_company_row_by_id(session, client, company_id)
    with translate_errors():
        report = service.get_order_cost(client.tenant_id, company_ref_for_row(row), row.id, order_ref)
    return OrderCostReportResponse.from_report(report)


# -- price calculator --------------------------------------------------------


@router.post(
    "/packaging/price-calculator/quote",
    response_model=PriceQuoteResponse,
    summary="Compute an approximate price quote (never auto-applied to an invoice)",
)
def compute_quote(
    body: ComputeQuoteRequest,
    client: AuthenticatedClient = Depends(require_scopes("packaging:read")),
    session: Session = Depends(get_db_session),
    service: PackagingQueryService = Depends(get_packaging_query_service),
) -> PriceQuoteResponse:
    """Compute a quote from current costs + entered rates. The result
    is labelled approximate and never touches an invoice.
    """
    row = require_company_row_by_id(session, client, body.company_id)
    with translate_errors():
        quote = service.compute_quote(
            client.tenant_id,
            company_ref_for_row(row),
            row.id,
            goods_cost_irr=body.goods_cost_irr,
            packaging=[
                QuotePackagingItem(material_id=item.material_id, quantity=item.quantity)
                for item in body.packaging
            ],
            shipment_cost_irr=body.shipment_cost_irr,
            carrier_rate_id=body.carrier_rate_id,
            margin_percent=body.margin_percent,
            margin_fixed_irr=body.margin_fixed_irr,
        )
    return PriceQuoteResponse.from_quote(quote)


# -- counts ------------------------------------------------------------------


@router.post(
    "/packaging/counts",
    response_model=InventoryCountResponse,
    status_code=201,
    summary="Open a periodic materials count at the packaging location",
)
def create_count(
    body: CreateCountRequest,
    client: AuthenticatedClient = Depends(require_scopes("packaging:manage")),
    session: Session = Depends(get_db_session),
    service: PackagingCommandService = Depends(get_packaging_command_service),
) -> InventoryCountResponse:
    """Open a count: the engine inventory is created and its lines
    completed with the expected quantities.
    """
    row = require_company_row_by_id(session, client, body.company_id)
    with translate_errors():
        count = service.create_count(
            tenant_id=client.tenant_id,
            company=company_ref_for_row(row),
            company_id=row.id,
            count_date=_to_fiscal(body.count_date),
            counted_by=body.counted_by,
        )
    session.commit()
    return InventoryCountResponse.from_count(count)


@router.get(
    "/packaging/counts",
    response_model=PaginatedResponse[InventoryCountResponse],
    summary="List counts (newest first)",
)
def list_counts(
    request: Request,
    company_id: str = Query(description="API company id (row id or engine id)"),
    sort: str | None = Query(
        default=None,
        description="Sort field: 'created_at' (default) or 'count_date'; prefix '-' for descending",
    ),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"),
    cursor: str | None = Query(default=None, description="Opaque cursor from a previous page's 'next_cursor'"),
    client: AuthenticatedClient = Depends(require_scopes("packaging:read")),
    session: Session = Depends(get_db_session),
    service: PackagingQueryService = Depends(get_packaging_query_service),
) -> PaginatedResponse[InventoryCountResponse]:
    """Return the company's counts, cursor-paginated."""
    reject_unknown_params(request, _COUNTS_QUERY_PARAMS)
    field, reverse = parse_sort_param(sort, _COUNT_SORT_KEYS, default="created_at")
    row = require_company_row_by_id(session, client, company_id)
    with translate_errors():
        counts = service.list_counts(client.tenant_id, row.id)
    items = [InventoryCountResponse.from_count(count) for count in counts]
    page, next_cursor = paginate(
        items,
        key_fn=_COUNT_SORT_KEYS[field],
        tie_fn=lambda item: item.id,
        limit=limit,
        cursor=cursor,
        reverse=reverse,
    )
    return PaginatedResponse(items=page, next_cursor=next_cursor)


@router.get(
    "/packaging/counts/{count_id}",
    response_model=InventoryCountResponse,
    summary="Get one count (refreshed from the engine)",
)
def get_count(
    count_id: str,
    client: AuthenticatedClient = Depends(require_scopes("packaging:read")),
    service: PackagingQueryService = Depends(get_packaging_query_service),
) -> InventoryCountResponse:
    """Return one count, refreshed from the engine inventory."""
    with translate_errors():
        count = service.get_count(client.tenant_id, count_id)
    return InventoryCountResponse.from_count(count)


@router.post(
    "/packaging/counts/{count_id}/lines",
    response_model=InventoryCountResponse,
    summary="Set the counted quantity on one count line (draft counts only)",
)
def set_count_line(
    count_id: str,
    body: SetCountLineRequest,
    client: AuthenticatedClient = Depends(require_scopes("packaging:manage")),
    session: Session = Depends(get_db_session),
    service: PackagingCommandService = Depends(get_packaging_command_service),
) -> InventoryCountResponse:
    """Set one line's counted quantity."""
    with translate_errors():
        count = service.set_count_line(
            tenant_id=client.tenant_id,
            count_id=count_id,
            line_id=body.line_id,
            quantity=body.quantity,
        )
    session.commit()
    return InventoryCountResponse.from_count(count)


@router.post(
    "/packaging/counts/{count_id}/confirm",
    response_model=InventoryCountResponse,
    summary="Confirm a count (human approval: the engine posts the adjustments)",
)
def confirm_count(
    count_id: str,
    client: AuthenticatedClient = Depends(require_scopes("packaging:manage")),
    session: Session = Depends(get_db_session),
    service: PackagingCommandService = Depends(get_packaging_command_service),
) -> InventoryCountResponse:
    """Confirm a count. All lines must be counted; the engine creates
    the lost-and-found adjustment moves on confirm.
    """
    with translate_errors():
        count = service.confirm_count(tenant_id=client.tenant_id, count_id=count_id)
    session.commit()
    return InventoryCountResponse.from_count(count)


@router.post(
    "/packaging/counts/{count_id}/cancel",
    response_model=InventoryCountResponse,
    summary="Cancel a draft count",
)
def cancel_count(
    count_id: str,
    client: AuthenticatedClient = Depends(require_scopes("packaging:manage")),
    session: Session = Depends(get_db_session),
    service: PackagingCommandService = Depends(get_packaging_command_service),
) -> InventoryCountResponse:
    """Cancel a draft count (no adjustment is posted)."""
    with translate_errors():
        count = service.cancel_count(tenant_id=client.tenant_id, count_id=count_id)
    session.commit()
    return InventoryCountResponse.from_count(count)


# -- supply check ------------------------------------------------------------


@router.post(
    "/packaging/supply-check",
    response_model=SupplyCheckReportResponse,
    summary="Run the supply check now (reads the wizard's results; never runs the wizard)",
)
def run_supply_check(
    body: RunSupplyCheckRequest,
    client: AuthenticatedClient = Depends(require_scopes("packaging:manage")),
    session: Session = Depends(get_db_session),
    service: PackagingCommandService = Depends(get_packaging_command_service),
) -> SupplyCheckReportResponse:
    """Run the supply check: read new purchase requests since the
    watermark + live coverage, and file ``packaging_reorder`` review
    items (deduplicated against open items).
    """
    row = require_company_row_by_id(session, client, body.company_id)
    with translate_errors():
        report = service.run_supply_check(
            tenant_id=client.tenant_id,
            company=company_ref_for_row(row),
            company_id=row.id,
            actor=client.client_name,
        )
    session.commit()
    return SupplyCheckReportResponse.from_report(report)
