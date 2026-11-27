"""Tests for the packaging REST endpoints (US-515, Phase 5 wave 2).

Everything is real — the FastAPI app, scoped API-key auth, the
PostgreSQL database — except the engine, which is the
``FakePackagingEngine`` from the unit tests (imported, not duplicated),
injected via ``app.dependency_overrides[get_engine_adapter]``. The live
Tryton server is NOT needed here; end-to-end tests against the real
server live in ``tests/contracts/test_tryton_packaging.py``.

Provisioned tenants/keys/companies are removed in teardown by deleting
the tenant (cascades to clients/keys/companies/packaging rows) and the
engine-connection rows.
"""

from __future__ import annotations

import datetime
import os
import uuid
from collections.abc import Generator
from dataclasses import dataclass
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from hesabyar.domain.common.dates import FiscalDate
from hesabyar.infrastructure.auth import ApiKeyCrypto, ApiKeyService
from hesabyar.infrastructure.config import AppSettings
from hesabyar.infrastructure.persistence.postgres.models import (
    ApiClientModel,
    Base,
    EngineCompanyModel,
    EngineConnectionModel,
    TenantModel,
)
from hesabyar.interfaces.rest.app import create_app
from hesabyar.interfaces.rest.dependencies import get_engine_adapter
from tests.unit.test_packaging_services import (
    CATEGORY_ID,
    PACK_ID,
    PRODUCE_ID,
    ROOT_ID,
    WAREHOUSE_ID,
    FakePackagingEngine,
)

TEST_DB_URL = os.getenv(
    "HESABYAR_DATABASE_URL",
    "postgresql+psycopg://hesabyar:hesabyar_pass@hesabyar-postgres:5432/hesabyar_test",
)

INSTALLATION_ID = "00000000-0000-0000-0000-000000000515"

BASE = "/api/v1/packaging"


@dataclass
class World:
    """Provisioned tenant, company and keys for one test."""

    tenant_id: str
    other_tenant_id: str
    company_id: str
    read_key: str
    manage_key: str
    company_key: str
    other_read_key: str


@pytest.fixture(scope="module")
def engine() -> Generator[Engine, None, None]:
    eng = create_engine(TEST_DB_URL, echo=False)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def settings() -> AppSettings:
    """Fresh settings (dev pepper) — the same pepper the app dependency uses."""
    return AppSettings()


@pytest.fixture()
def world(engine: Engine, settings: AppSettings) -> Generator[World, None, None]:
    """Provision two tenants, one company and four keys; clean up after."""
    session = sessionmaker(bind=engine)()
    service = ApiKeyService(ApiKeyCrypto(settings.auth.api_key_pepper))
    tenant_ids: list[str] = []
    connection_ids: list[str] = []
    try:
        tenant = TenantModel(id=str(uuid.uuid4()), slug=f"rest-515-{uuid.uuid4().hex[:8]}", name="Tenant 1")
        other_tenant = TenantModel(id=str(uuid.uuid4()), slug=f"rest-515-{uuid.uuid4().hex[:8]}", name="Tenant 2")
        session.add_all([tenant, other_tenant])
        session.flush()
        client = ApiClientModel(id=str(uuid.uuid4()), tenant_id=tenant.id, name="rest-515 client")
        other_client = ApiClientModel(id=str(uuid.uuid4()), tenant_id=other_tenant.id, name="rest-515 other client")
        session.add_all([client, other_client])
        session.flush()

        connection = EngineConnectionModel(
            id=str(uuid.uuid4()),
            name=f"conn-{uuid.uuid4().hex[:8]}",
            engine="tryton",
            base_url="http://localhost:8000",
            database_name=f"hesabyar_tryton_test_{uuid.uuid4().hex[:8]}",
        )
        session.add(connection)
        session.flush()
        connection_ids.append(connection.id)
        # The fake engine's company is CompanyRef(1).
        company = EngineCompanyModel(
            id=str(uuid.uuid4()),
            tenant_id=tenant.id,
            engine_connection_id=connection.id,
            external_company_id="1",
            company_name="Company 1",
        )
        session.add(company)
        session.flush()

        def _key(client_row: ApiClientModel, scopes: list[str]):
            return service.issue_key(
                session,
                client=client_row,
                name="rest-515 key",
                scopes=scopes,
                installation_id=INSTALLATION_ID,
            )

        key_read = _key(client, ["packaging:read"])
        key_manage = _key(client, ["packaging:read", "packaging:manage", "review:read"])
        key_company = _key(client, ["company:read"])
        key_other_read = _key(other_client, ["packaging:read"])
        session.commit()
        tenant_ids = [tenant.id, other_tenant.id]
        yield World(
            tenant_id=tenant.id,
            other_tenant_id=other_tenant.id,
            company_id=company.id,
            read_key=key_read.secret,
            manage_key=key_manage.secret,
            company_key=key_company.secret,
            other_read_key=key_other_read.secret,
        )
    finally:
        session.close()
        cleanup = sessionmaker(bind=engine)()
        try:
            if tenant_ids:
                cleanup.query(TenantModel).filter(TenantModel.id.in_(tenant_ids)).delete(synchronize_session=False)
            if connection_ids:
                cleanup.query(EngineConnectionModel).filter(EngineConnectionModel.id.in_(connection_ids)).delete(
                    synchronize_session=False
                )
            cleanup.commit()
        finally:
            cleanup.close()


def _test_client(settings: AppSettings, fake: FakePackagingEngine) -> TestClient:
    """Real app with the fake engine behind the packaging services."""
    app = create_app(settings=settings)
    app.dependency_overrides[get_engine_adapter] = lambda: fake
    return TestClient(app, raise_server_exceptions=False)


def _auth(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


def _configure(client: TestClient, world: World, fake: FakePackagingEngine, **overrides) -> dict:
    """Save full packaging settings (category + locations + root)."""
    body = {
        "company_id": world.company_id,
        "cost_method": "average",
        "shipment_cost_method": "on_order",
        "days_of_cover_window_days": 30,
        "low_stock_alert_days": 7,
        "packaging_location_id": PACK_ID,
        "production_location_id": PRODUCE_ID,
        "warehouse_id": WAREHOUSE_ID,
        "packaging_category_id": CATEGORY_ID,
        "analytic_root_id": ROOT_ID,
    }
    body.update(overrides)
    response = client.put(f"{BASE}/settings", json=body, headers=_auth(world.manage_key))
    assert response.status_code == 200, response.text
    return response.json()


def _create_material(client: TestClient, world: World, *, name: str = "کارتن مقوایی", **overrides) -> dict:
    """Create a material via the API; return the nested material dict."""
    body = {"company_id": world.company_id, "name_fa": name, "cost_method": "average"}
    body.update(overrides)
    response = client.post(f"{BASE}/materials", json=body, headers=_auth(world.manage_key))
    assert response.status_code == 201, response.text
    return response.json()["material"]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_list_materials_requires_auth(settings: AppSettings, world: World) -> None:
    client = _test_client(settings, FakePackagingEngine())
    response = client.get(f"{BASE}/materials", params={"company_id": world.company_id})
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"


def test_list_materials_requires_packaging_read_scope(settings: AppSettings, world: World) -> None:
    client = _test_client(settings, FakePackagingEngine())
    response = client.get(
        f"{BASE}/materials",
        params={"company_id": world.company_id},
        headers=_auth(world.company_key),
    )
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


def test_create_material_requires_packaging_manage_scope(settings: AppSettings, world: World) -> None:
    client = _test_client(settings, FakePackagingEngine())
    response = client.post(
        f"{BASE}/materials",
        json={"company_id": world.company_id, "name_fa": "کارتن مقوایی"},
        headers=_auth(world.read_key),
    )
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


def test_other_tenant_company_is_404(settings: AppSettings, world: World) -> None:
    client = _test_client(settings, FakePackagingEngine())
    response = client.get(
        f"{BASE}/materials",
        params={"company_id": world.company_id},
        headers=_auth(world.other_read_key),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "COMPANY_NOT_FOUND"


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_settings_default_when_never_configured(settings: AppSettings, world: World) -> None:
    client = _test_client(settings, FakePackagingEngine())
    response = client.get(f"{BASE}/settings", params={"company_id": world.company_id}, headers=_auth(world.read_key))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["company_id"] == world.company_id
    assert body["cost_method"] == "average"
    assert body["packaging_location_id"] is None
    assert body["analytic_root_id"] is None


def test_save_settings_round_trip(settings: AppSettings, world: World) -> None:
    fake = FakePackagingEngine()
    client = _test_client(settings, fake)
    saved = _configure(client, world, fake)
    assert saved["packaging_location_id"] == PACK_ID
    assert saved["production_location_id"] == PRODUCE_ID
    assert saved["warehouse_id"] == WAREHOUSE_ID
    assert saved["packaging_category_id"] == CATEGORY_ID
    assert saved["analytic_root_id"] == ROOT_ID
    assert saved["days_of_cover_window_days"] == 30

    response = client.get(f"{BASE}/settings", params={"company_id": world.company_id}, headers=_auth(world.read_key))
    assert response.status_code == 200
    assert response.json()["packaging_location_id"] == PACK_ID


def test_save_settings_unknown_location_is_404(settings: AppSettings, world: World) -> None:
    fake = FakePackagingEngine()
    client = _test_client(settings, fake)
    response = client.put(
        f"{BASE}/settings",
        json={"company_id": world.company_id, "packaging_location_id": 99999},
        headers=_auth(world.manage_key),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "LEDGER_STOCK_LOCATION_NOT_FOUND"


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------


def test_list_locations(settings: AppSettings, world: World) -> None:
    client = _test_client(settings, FakePackagingEngine())
    response = client.get(f"{BASE}/locations", params={"company_id": world.company_id}, headers=_auth(world.read_key))
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body) == 3
    codes = {loc["code"] for loc in body}
    assert codes == {None, "PACK", "PACK-CONSUME"}


# ---------------------------------------------------------------------------
# Materials
# ---------------------------------------------------------------------------


def test_create_material_requires_category(settings: AppSettings, world: World) -> None:
    client = _test_client(settings, FakePackagingEngine())
    response = client.post(
        f"{BASE}/materials",
        json={"company_id": world.company_id, "name_fa": "کارتن مقوایی"},
        headers=_auth(world.manage_key),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "PACKAGING_SETUP_REQUIRED"


def test_create_and_get_material(settings: AppSettings, world: World) -> None:
    fake = FakePackagingEngine()
    client = _test_client(settings, fake)
    _configure(client, world, fake)
    created = _create_material(client, world, code="BOX-01")
    assert created["name_fa"] == "کارتن مقوایی"
    assert created["code"] == "BOX-01"
    assert created["cost_method"] == "average"
    assert created["tryton_product_id"] == 1001
    assert created["tryton_template_id"] == 2001
    assert created["is_active"] is True

    response = client.get(
        f"{BASE}/materials/{created['id']}",
        params={"company_id": world.company_id},
        headers=_auth(world.read_key),
    )
    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail["material"]["id"] == created["id"]
    # No stock yet (fake product starts at 0) and no consumption → the
    # coverage object exists but days_of_cover is null (nothing to project from).
    assert detail["stock"]["quantity"] == "0"
    assert detail["coverage"]["days_of_cover"] is None
    assert detail["coverage"]["quantity_on_hand"] == "0"
    assert detail["reorder_point"] is None


def test_create_material_fifo_without_module_is_422(settings: AppSettings, world: World) -> None:
    fake = FakePackagingEngine()
    fake.fifo_available = False
    client = _test_client(settings, fake)
    _configure(client, world, fake)
    response = client.post(
        f"{BASE}/materials",
        json={"company_id": world.company_id, "name_fa": "کارتن مقوایی", "cost_method": "fifo"},
        headers=_auth(world.manage_key),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "PACKAGING_FIFO_UNAVAILABLE"


def test_list_materials(settings: AppSettings, world: World) -> None:
    fake = FakePackagingEngine()
    client = _test_client(settings, fake)
    _configure(client, world, fake)
    _create_material(client, world, name="کارتن مقوایی")
    _create_material(client, world, name="چسب نواری")
    response = client.get(f"{BASE}/materials", params={"company_id": world.company_id}, headers=_auth(world.read_key))
    assert response.status_code == 200, response.text
    names = [m["material"]["name_fa"] for m in response.json()["items"]]
    # Default sort is by name (Persian alphabetical: چ before ک).
    assert names == ["چسب نواری", "کارتن مقوایی"]


def test_update_material(settings: AppSettings, world: World) -> None:
    fake = FakePackagingEngine()
    client = _test_client(settings, fake)
    _configure(client, world, fake)
    created = _create_material(client, world)
    response = client.patch(
        f"{BASE}/materials/{created['id']}",
        json={"name_fa": "کارتن دوجداره", "is_active": False},
        headers=_auth(world.manage_key),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["material"]["name_fa"] == "کارتن دوجداره"
    assert body["material"]["is_active"] is False


def test_update_unknown_material_is_404(settings: AppSettings, world: World) -> None:
    client = _test_client(settings, FakePackagingEngine())
    response = client.patch(
        f"{BASE}/materials/{uuid.uuid4()}",
        json={"name_fa": "کارتن دوجداره"},
        headers=_auth(world.manage_key),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "PACKAGING_MATERIAL_NOT_FOUND"


def test_set_reorder_point(settings: AppSettings, world: World) -> None:
    fake = FakePackagingEngine()
    client = _test_client(settings, fake)
    _configure(client, world, fake)
    created = _create_material(client, world)
    response = client.put(
        f"{BASE}/materials/{created['id']}/reorder-point",
        json={"company_id": world.company_id, "min_quantity": "10", "target_quantity": "50"},
        headers=_auth(world.manage_key),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["reorder_point"]["min_quantity"] == "10"
    assert body["reorder_point"]["target_quantity"] == "50"
    assert body["reorder_point"]["warehouse_engine_id"] == WAREHOUSE_ID


def test_coverage_from_seeded_moves(settings: AppSettings, world: World) -> None:
    fake = FakePackagingEngine()
    client = _test_client(settings, fake)
    _configure(client, world, fake)
    created = _create_material(client, world)
    fake.seed_product(
        created["tryton_product_id"],
        created["tryton_template_id"],
        quantity=Decimal(60),
    )
    # 30 units consumed over the last 30 days → 1/day → 60 days of cover.
    fake.seed_move(
        created["tryton_product_id"],
        Decimal(30),
        FiscalDate.from_gregorian(datetime.date(2026, 11, 10)),
    )
    response = client.get(
        f"{BASE}/materials/{created['id']}/coverage",
        params={"company_id": world.company_id},
        headers=_auth(world.read_key),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["quantity_on_hand"] == "60"
    assert body["avg_daily_consumption"] == "1"
    assert body["days_of_cover"] == 60
    assert body["is_out_of_stock"] is False


# ---------------------------------------------------------------------------
# Consumptions
# ---------------------------------------------------------------------------


def test_record_consumption_and_replay(settings: AppSettings, world: World) -> None:
    fake = FakePackagingEngine()
    client = _test_client(settings, fake)
    _configure(client, world, fake)
    created = _create_material(client, world)
    body = {
        "company_id": world.company_id,
        "material_id": created["id"],
        "order_ref": "ORD-1001",
        "shipment_ref": "SHIP-1",
        "quantity": "2",
    }
    response = client.post(f"{BASE}/consumptions", json=body, headers=_auth(world.manage_key))
    assert response.status_code == 201, response.text
    record = response.json()
    assert record["order_ref"] == "ORD-1001"
    # The DB stores quantities as Numeric(?, 4) — compare numerically.
    assert Decimal(record["quantity"]) == Decimal(2)
    assert record["source"] == "manual"
    assert record["total_cost_irr"] == 24000  # 2 × 12000 fake cost price

    # Replay with the same idempotency key → 200, same record, no second move.
    response = client.post(f"{BASE}/consumptions", json=body, headers=_auth(world.manage_key))
    assert response.status_code == 200, response.text
    assert response.json()["id"] == record["id"]
    assert len(fake.moves) == 1


def test_record_consumption_unknown_material_is_404(settings: AppSettings, world: World) -> None:
    fake = FakePackagingEngine()
    client = _test_client(settings, fake)
    _configure(client, world, fake)
    response = client.post(
        f"{BASE}/consumptions",
        json={
            "company_id": world.company_id,
            "material_id": str(uuid.uuid4()),
            "order_ref": "ORD-1001",
            "quantity": "2",
        },
        headers=_auth(world.manage_key),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "PACKAGING_MATERIAL_NOT_FOUND"


def test_list_consumptions(settings: AppSettings, world: World) -> None:
    fake = FakePackagingEngine()
    client = _test_client(settings, fake)
    _configure(client, world, fake)
    created = _create_material(client, world)
    for order_ref in ("ORD-1001", "ORD-1002"):
        response = client.post(
            f"{BASE}/consumptions",
            json={
                "company_id": world.company_id,
                "material_id": created["id"],
                "order_ref": order_ref,
                "quantity": "1",
            },
            headers=_auth(world.manage_key),
        )
        assert response.status_code == 201, response.text
    response = client.get(
        f"{BASE}/consumptions",
        params={"company_id": world.company_id, "order_ref": "ORD-1001"},
        headers=_auth(world.read_key),
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) == 1


# ---------------------------------------------------------------------------
# Shipment costs
# ---------------------------------------------------------------------------


def test_record_and_list_shipment_cost(settings: AppSettings, world: World) -> None:
    fake = FakePackagingEngine()
    client = _test_client(settings, fake)
    _configure(client, world, fake)
    response = client.post(
        f"{BASE}/shipment-costs",
        json={
            "company_id": world.company_id,
            "order_ref": "ORD-1001",
            "method": "none",
            "actual_irr": 85000,
            "carrier_name": "تیپاکس",
        },
        headers=_auth(world.manage_key),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["actual_irr"] == 85000
    assert body["invoiced_irr"] == 0
    assert body["absorbed_irr"] == 85000

    response = client.get(
        f"{BASE}/shipment-costs",
        params={"company_id": world.company_id, "order_ref": "ORD-1001"},
        headers=_auth(world.read_key),
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) == 1


# ---------------------------------------------------------------------------
# Carrier rates
# ---------------------------------------------------------------------------


def test_carrier_rate_crud(settings: AppSettings, world: World) -> None:
    fake = FakePackagingEngine()
    client = _test_client(settings, fake)
    _configure(client, world, fake)
    response = client.post(
        f"{BASE}/carrier-rates",
        json={
            "company_id": world.company_id,
            "carrier_name": "تیپاکس",
            "rate_irr": 75000,
            "basis": "per_shipment",
        },
        headers=_auth(world.manage_key),
    )
    assert response.status_code == 201, response.text
    rate = response.json()
    assert rate["rate_irr"] == 75000
    assert rate["is_active"] is True

    response = client.patch(
        f"{BASE}/carrier-rates/{rate['id']}",
        json={"rate_irr": 80000},
        headers=_auth(world.manage_key),
    )
    assert response.status_code == 200, response.text
    assert response.json()["rate_irr"] == 80000

    response = client.get(
        f"{BASE}/carrier-rates", params={"company_id": world.company_id}, headers=_auth(world.read_key)
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) == 1


# ---------------------------------------------------------------------------
# Cost centres + order cost
# ---------------------------------------------------------------------------


def test_create_cost_center_and_duplicate_is_409(settings: AppSettings, world: World) -> None:
    fake = FakePackagingEngine()
    client = _test_client(settings, fake)
    _configure(client, world, fake)
    response = client.post(
        f"{BASE}/orders/ORD-1001/cost-center",
        json={"company_id": world.company_id, "order_ref": "ORD-1001"},
        headers=_auth(world.manage_key),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["order_ref"] == "ORD-1001"
    assert body["analytic_account_engine_id"] == ROOT_ID + 1

    response = client.post(
        f"{BASE}/orders/ORD-1001/cost-center",
        json={"company_id": world.company_id, "order_ref": "ORD-1001"},
        headers=_auth(world.manage_key),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "PACKAGING_COST_CENTER_EXISTS"


def test_order_cost_merges_figures(settings: AppSettings, world: World) -> None:
    fake = FakePackagingEngine()
    client = _test_client(settings, fake)
    _configure(client, world, fake)
    created = _create_material(client, world)
    response = client.post(
        f"{BASE}/orders/ORD-1001/cost-center",
        json={"company_id": world.company_id, "order_ref": "ORD-1001"},
        headers=_auth(world.manage_key),
    )
    assert response.status_code == 201, response.text
    response = client.post(
        f"{BASE}/consumptions",
        json={
            "company_id": world.company_id,
            "material_id": created["id"],
            "order_ref": "ORD-1001",
            "quantity": "1",
        },
        headers=_auth(world.manage_key),
    )
    assert response.status_code == 201, response.text
    response = client.post(
        f"{BASE}/shipment-costs",
        json={
            "company_id": world.company_id,
            "order_ref": "ORD-1001",
            "method": "none",
            "actual_irr": 85000,
        },
        headers=_auth(world.manage_key),
    )
    assert response.status_code == 201, response.text
    response = client.get(
        f"{BASE}/orders/ORD-1001/cost",
        params={"company_id": world.company_id},
        headers=_auth(world.read_key),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["packaging_cost_irr"] == 12000
    assert body["shipment_actual_irr"] == 85000
    assert body["goods_cost_irr"] == 0
    assert body["total_cost_irr"] == 97000


# ---------------------------------------------------------------------------
# Price calculator
# ---------------------------------------------------------------------------


def test_compute_quote(settings: AppSettings, world: World) -> None:
    fake = FakePackagingEngine()
    client = _test_client(settings, fake)
    _configure(client, world, fake)
    created = _create_material(client, world)
    # The fake's created products have no cost price — seed one so the
    # quote prices the packaging at 12000/unit like the unit tests do.
    fake.seed_product(
        created["tryton_product_id"],
        created["tryton_template_id"],
        cost_price=Decimal(12000),
    )
    response = client.post(
        f"{BASE}/price-calculator/quote",
        json={
            "company_id": world.company_id,
            "goods_cost_irr": 500000,
            "packaging": [{"material_id": created["id"], "quantity": "2"}],
            "shipment_cost_irr": 75000,
            "margin_percent": "10",
        },
        headers=_auth(world.read_key),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # goods 500000 + packaging 2×12000 + shipment 75000 = 599000; +10% = 658900.
    assert body["goods_cost_irr"] == 500000
    assert body["packaging_cost_irr"] == 24000
    assert body["shipment_cost_irr"] == 75000
    assert body["margin_irr"] == 59900
    assert body["total_irr"] == 658900
    assert body["approximate"] is True


# ---------------------------------------------------------------------------
# Counts
# ---------------------------------------------------------------------------


def test_count_lifecycle(settings: AppSettings, world: World) -> None:
    fake = FakePackagingEngine()
    client = _test_client(settings, fake)
    _configure(client, world, fake)
    created = _create_material(client, world)
    response = client.post(
        f"{BASE}/counts",
        json={"company_id": world.company_id, "counted_by": "انباردار"},
        headers=_auth(world.manage_key),
    )
    assert response.status_code == 201, response.text
    count = response.json()
    assert count["state"] == "draft"
    assert count["counted_by"] == "انباردار"
    # The fake inventory has one line per material (expected 0).
    assert len(count["lines"]) == 1
    line = count["lines"][0]
    assert line["material_id"] == created["id"]

    response = client.post(
        f"{BASE}/counts/{count['id']}/lines",
        json={"line_id": line["tryton_line_id"], "quantity": "48"},
        headers=_auth(world.manage_key),
    )
    assert response.status_code == 200, response.text
    assert response.json()["lines"][0]["counted_quantity"] == "48"

    response = client.post(f"{BASE}/counts/{count['id']}/confirm", headers=_auth(world.manage_key))
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "done"


def test_confirm_count_with_uncounted_lines_is_400(settings: AppSettings, world: World) -> None:
    fake = FakePackagingEngine()
    client = _test_client(settings, fake)
    _configure(client, world, fake)
    _create_material(client, world)
    response = client.post(
        f"{BASE}/counts",
        json={"company_id": world.company_id},
        headers=_auth(world.manage_key),
    )
    assert response.status_code == 201, response.text
    count = response.json()
    response = client.post(f"{BASE}/counts/{count['id']}/confirm", headers=_auth(world.manage_key))
    assert response.status_code == 400
    assert response.json()["code"] == "PACKAGING_INVALID_COUNT"


# ---------------------------------------------------------------------------
# Supply check
# ---------------------------------------------------------------------------


def test_supply_check_files_reorder_item(settings: AppSettings, world: World) -> None:
    fake = FakePackagingEngine()
    client = _test_client(settings, fake)
    _configure(client, world, fake)
    created = _create_material(client, world)
    fake.seed_purchase_request(9001, created["tryton_product_id"], Decimal(40))
    response = client.post(
        f"{BASE}/supply-check",
        json={"company_id": world.company_id},
        headers=_auth(world.manage_key),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["purchase_requests_new"] == 1
    # Two items: the purchase request AND a low-stock item (the material
    # starts at 0 stock, which counts as low).
    assert body["items_filed"] == 2
    assert body["materials_checked"] == 1
    assert body["low_stock_materials"] == [created["id"]]

    # The items are visible in the review queue.
    response = client.get(
        "/api/v1/review-queue/items",
        params={"company": world.company_id, "type": "packaging_reorder"},
        headers=_auth(world.manage_key),
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) == 2

    # Second run: the open low-stock item dedupes the filing.
    response = client.post(
        f"{BASE}/supply-check",
        json={"company_id": world.company_id},
        headers=_auth(world.manage_key),
    )
    assert response.status_code == 200, response.text
    assert response.json()["items_filed"] == 0
    assert response.json()["items_skipped_open"] == 1
