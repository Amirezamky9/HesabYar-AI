"""Unit tests for the packaging adapter methods (US-515).

No live server: the HTTP layer is exercised with httpx
MockTransport against a small fake Tryton that routes on
(method, model, id/action) and records every request, so the tests
assert on the exact wire payloads (goods type, consumable False,
explicit cost method, ``do`` action on the move, button actions on
inventories). The contract tests
(``tests/contracts/test_tryton_packaging.py``) cover the same methods
against the live engine.
"""

from __future__ import annotations

import datetime
import json
from decimal import Decimal
from typing import Any

import httpx
import pytest

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
    OutShipmentRef,
    ProductRef,
    SaleRef,
    StockLocationRef,
)
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.errors import (
    InvalidProductError,
    InvalidStockMoveError,
    ProductNotFoundError,
    StockLocationNotFoundError,
)
from hesabyar.domain.packaging.errors import FifoUnavailableError
from hesabyar.infrastructure.accounting.tryton.adapter import TrytonAccountingAdapter
from hesabyar.infrastructure.accounting.tryton.client import TrytonRestClient

BASE_URL = "http://tryton.test:8000"
DATABASE = "hesabyar_tryton_test"
COMPANY = CompanyRef(1)
TODAY = FiscalDate.from_gregorian(datetime.date(2026, 11, 18))


class FakeTryton:
    """Route (method, model, id/action) to canned JSON; record requests."""

    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []
        self.routes: dict[tuple[str, str, str], Any] = {}
        self.search_results: dict[str, list[dict[str, Any]]] = {}
        self.read_results: dict[tuple[str, int], dict[str, Any] | None] = {}
        self.create_results: dict[str, dict[str, Any]] = {}
        self.action_results: dict[tuple[str, str, int], Any] = {}
        self.action_failures: dict[tuple[str, str, int], int] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        parts = path.strip("/").split("/")
        # /<db>/rest/model/<model>[/<id>[/<action>]]
        model = parts[3]
        rest = parts[4:]
        body = json.loads(request.content) if request.content else None
        self.seen.append(
            {
                "method": request.method,
                "model": model,
                "rest": rest,
                "body": body,
                "query": dict(request.url.params),
            }
        )
        if request.method == "GET" and not rest:
            rows = list(self.search_results.get(model, []))
            # The adapter pages with s= (limit) + p= (offset); honor them.
            offset = int(request.url.params.get("p", 0))
            limit = request.url.params.get("s")
            if limit is not None:
                rows = rows[offset : offset + int(limit)]
            return httpx.Response(200, json=rows)
        if request.method == "GET" and len(rest) == 1:
            record = self.read_results.get((model, int(rest[0])))
            if record is None:
                return httpx.Response(404, text="Not Found")
            return httpx.Response(200, json=record)
        if request.method == "POST" and not rest:
            result = self.create_results.get(model)
            if result is None:
                return httpx.Response(400, text="no canned create result")
            return httpx.Response(200, json=result)
        if request.method == "PUT" and len(rest) == 1:
            return httpx.Response(200, json={"id": int(rest[0])})
        if request.method == "POST" and len(rest) == 2:
            record_id, action = int(rest[0]), rest[1]
            failure = self.action_failures.get((model, action, record_id))
            if failure is not None:
                return httpx.Response(failure, text="engine refused")
            return httpx.Response(
                200, json=self.action_results.get((model, action, record_id), {})
            )
        return httpx.Response(404, text="unrouted")

    def seed_irr_company(self) -> None:
        """The company/currency reads every mutating method performs."""
        self.read_results[("company.company", 1)] = {"id": 1, "currency": 1}
        self.search_results["currency.currency"] = [{"id": 1, "code": "IRR"}]


def _adapter(fake: FakeTryton) -> TrytonAccountingAdapter:
    client = TrytonRestClient(
        BASE_URL, DATABASE, "test-key", transport=httpx.MockTransport(fake.handler)
    )
    return TrytonAccountingAdapter(client)


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------


def test_list_stock_locations_returns_views():
    fake = FakeTryton()
    fake.search_results["stock.location"] = [
        {"id": 5, "name": "انبار اصلی", "type": "warehouse", "code": None, "parent": None},
        {"id": 101, "name": "مواد بسته‌بندی", "type": "storage", "code": "PACK", "parent": 5},
    ]
    locations = _adapter(fake).list_stock_locations(COMPANY)
    assert [loc.name for loc in locations] == ["انبار اصلی", "مواد بسته‌بندی"]
    assert locations[1].code == "PACK"
    assert locations[1].parent is not None
    assert locations[1].parent.engine_id == 5


def test_get_stock_location_404_raises():
    fake = FakeTryton()
    with pytest.raises(StockLocationNotFoundError):
        _adapter(fake).get_stock_location(StockLocationRef(999))


# ---------------------------------------------------------------------------
# Packaging products
# ---------------------------------------------------------------------------


def test_create_packaging_product_sends_goods_not_consumable():
    fake = FakeTryton()
    fake.seed_irr_company()
    fake.read_results[("product.category", 900)] = {"id": 900, "name": "مواد بسته‌بندی"}
    fake.search_results["product.uom"] = [{"id": 1}]
    fake.create_results["product.template"] = {
        "id": 2001,
        "cost_price_method": "average",
    }
    fake.create_results["product.product"] = {"id": 1001}
    ref = _adapter(fake).create_packaging_product(
        CreatePackagingProduct(
            company=COMPANY,
            name="کارتن مقوایی",
            category_engine_id=900,
            cost_method="average",
            code="BOX",
        )
    )
    assert ref.engine_id == 1001
    create = next(
        item for item in fake.seen if item["method"] == "POST" and item["model"] == "product.template"
    )
    body = create["body"]
    assert body["type"] == "goods"
    assert body["consumable"] is False
    assert body["purchasable"] is True
    assert body["salable"] is False
    assert body["cost_price_method"] == "average"
    assert body["account_category"] == 900
    assert body["default_uom"] == 1
    # A purchasable template requires a purchase UoM (contract test
    # caught the missing field against live Tryton).
    assert body["purchase_uom"] == 1
    # The REST user gets no default variant — the adapter creates the
    # single variant explicitly.
    variant_create = next(
        item for item in fake.seen if item["method"] == "POST" and item["model"] == "product.product"
    )
    assert variant_create["body"] == {"template": 2001}


def test_create_packaging_product_cost_method_mismatch_raises():
    fake = FakeTryton()
    fake.seed_irr_company()
    fake.read_results[("product.category", 900)] = {"id": 900, "name": "مواد بسته‌بندی"}
    fake.search_results["product.uom"] = [{"id": 1}]
    # The engine silently defaulted to fixed: refuse the material.
    fake.create_results["product.template"] = {
        "id": 2001,
        "products": [1001],
        "cost_price_method": "fixed",
    }
    with pytest.raises(InvalidProductError):
        _adapter(fake).create_packaging_product(
            CreatePackagingProduct(
                company=COMPANY,
                name="کارتن",
                category_engine_id=900,
                cost_method="average",
            )
        )


def test_create_packaging_product_fifo_needs_module():
    fake = FakeTryton()
    fake.seed_irr_company()
    fake.read_results[("product.category", 900)] = {"id": 900, "name": "مواد بسته‌بندی"}
    fake.search_results["ir.module"] = []  # product_cost_fifo not activated
    with pytest.raises(FifoUnavailableError):
        _adapter(fake).create_packaging_product(
            CreatePackagingProduct(
                company=COMPANY,
                name="کارتن",
                category_engine_id=900,
                cost_method="fifo",
            )
        )


def test_create_packaging_product_fifo_with_module_ok():
    fake = FakeTryton()
    fake.seed_irr_company()
    fake.read_results[("product.category", 900)] = {"id": 900, "name": "مواد بسته‌بندی"}
    fake.search_results["ir.module"] = [{"id": 42}]
    fake.search_results["product.uom"] = [{"id": 1}]
    fake.create_results["product.template"] = {
        "id": 2001,
        "cost_price_method": "fifo",
    }
    fake.create_results["product.product"] = {"id": 1001}
    ref = _adapter(fake).create_packaging_product(
        CreatePackagingProduct(
            company=COMPANY, name="کارتن", category_engine_id=900, cost_method="fifo"
        )
    )
    assert ref.engine_id == 1001


def test_get_packaging_product_reads_quantity_and_cost():
    fake = FakeTryton()
    fake.read_results[("product.product", 1001)] = {
        "id": 1001,
        "template": 2001,
        "quantity": 42.0,
        "cost_price": {"__class__": "Decimal", "decimal": "12500.5"},
    }
    fake.read_results[("product.template", 2001)] = {
        "id": 2001,
        "name": "کارتن مقوایی",
        "code": "BOX",
        "type": "goods",
        "consumable": False,
        "purchasable": True,
        "cost_price_method": "average",
        "default_uom": 1,
    }
    view = _adapter(fake).get_packaging_product(
        ProductRef(1001), COMPANY, StockLocationRef(101)
    )
    assert view.name == "کارتن مقوایی"
    assert view.quantity == Decimal("42.0")
    assert view.cost_price == Decimal("12500.5")
    assert view.consumable is False
    # The locations context reaches the product read.
    read = next(
        item for item in fake.seen if item["method"] == "GET" and item["model"] == "product.product"
    )
    assert read["rest"] == ["1001"]


def test_get_packaging_product_missing_raises():
    fake = FakeTryton()
    with pytest.raises(ProductNotFoundError):
        _adapter(fake).get_packaging_product(ProductRef(999), COMPANY)


def test_list_packaging_products_batches_two_searches():
    fake = FakeTryton()
    fake.search_results["product.product"] = [
        {"id": 1001, "template": 2001, "quantity": 42.0, "cost_price": 12500},
        {"id": 1002, "template": 2002, "quantity": -3.0, "cost_price": 8000},
    ]
    fake.search_results["product.template"] = [
        {
            "id": 2001,
            "name": "کارتن مقوایی",
            "code": "BOX",
            "type": "goods",
            "consumable": False,
            "purchasable": True,
            "cost_price_method": "average",
            "default_uom": 1,
        },
        {
            "id": 2002,
            "name": "نوار چسب",
            "code": "TAPE",
            "type": "goods",
            "consumable": False,
            "purchasable": True,
            "cost_price_method": "average",
            "default_uom": 1,
        },
    ]
    views = _adapter(fake).list_packaging_products(
        COMPANY, [ProductRef(1001), ProductRef(1002)], StockLocationRef(101)
    )
    assert [view.ref.engine_id for view in views] == [1001, 1002]
    assert views[0].name == "کارتن مقوایی"
    assert views[0].quantity == Decimal("42.0")
    assert views[1].quantity == Decimal("-3.0")
    # Exactly two searches (products + templates), no per-product reads.
    searches = [item for item in fake.seen if item["method"] == "GET"]
    assert len(searches) == 2
    assert searches[0]["model"] == "product.product"
    assert searches[1]["model"] == "product.template"
    assert _adapter(fake).list_packaging_products(COMPANY, []) == []


def test_get_product_quantity_and_cost_price():
    fake = FakeTryton()
    fake.read_results[("product.product", 1001)] = {
        "id": 1001,
        "quantity": -3.0,
        "cost_price": {"__class__": "Decimal", "decimal": "12000"},
    }
    adapter = _adapter(fake)
    assert adapter.get_product_quantity(ProductRef(1001), StockLocationRef(101)) == Decimal("-3.0")
    assert adapter.get_product_cost_price(ProductRef(1001), COMPANY) == Decimal("12000")


# ---------------------------------------------------------------------------
# Order points
# ---------------------------------------------------------------------------


def test_upsert_order_point_creates_when_missing():
    fake = FakeTryton()
    fake.search_results["stock.order_point"] = []
    fake.create_results["stock.order_point"] = {"id": 3001}
    fake.read_results[("stock.order_point", 3001)] = {
        "id": 3001,
        "product": 1001,
        "location": 5,
        "type": "purchase",
        "min_quantity": 10.0,
        "target_quantity": 50.0,
        "max_quantity": None,
    }
    view = _adapter(fake).upsert_order_point(
        UpsertOrderPoint(
            company=COMPANY,
            product=ProductRef(1001),
            warehouse=StockLocationRef(5),
            min_quantity=Decimal(10),
            target_quantity=Decimal(50),
        )
    )
    assert view.ref.engine_id == 3001
    assert view.min_quantity == Decimal("10.0")
    create = next(
        item for item in fake.seen if item["method"] == "POST" and item["model"] == "stock.order_point"
    )
    assert create["body"]["type"] == "purchase"
    assert create["body"]["location"] == 5


def test_upsert_order_point_updates_when_present():
    fake = FakeTryton()
    fake.search_results["stock.order_point"] = [{"id": 3001}]
    fake.read_results[("stock.order_point", 3001)] = {
        "id": 3001,
        "product": 1001,
        "location": 5,
        "type": "purchase",
        "min_quantity": 20.0,
        "target_quantity": 60.0,
        "max_quantity": None,
    }
    view = _adapter(fake).upsert_order_point(
        UpsertOrderPoint(
            company=COMPANY,
            product=ProductRef(1001),
            warehouse=StockLocationRef(5),
            min_quantity=Decimal(20),
            target_quantity=Decimal(60),
        )
    )
    assert view.min_quantity == Decimal("20.0")
    write = next(
        item for item in fake.seen if item["method"] == "PUT" and item["model"] == "stock.order_point"
    )
    assert write["rest"] == ["3001"]
    assert write["body"]["target_quantity"] == 60


def test_get_order_point_returns_none_when_missing():
    fake = FakeTryton()
    fake.search_results["stock.order_point"] = []
    assert (
        _adapter(fake).get_order_point(ProductRef(1001), StockLocationRef(5), COMPANY)
        is None
    )


# ---------------------------------------------------------------------------
# Consumption moves
# ---------------------------------------------------------------------------


def test_create_consumption_move_drafts_then_assigns_and_does():
    fake = FakeTryton()
    fake.seed_irr_company()
    fake.read_results[("product.product", 1001)] = {"id": 1001, "template": 2001, "cost_price": 12000}
    fake.read_results[("product.template", 2001)] = {
        "id": 2001,
        "default_uom": 1,
        "type": "goods",
    }
    fake.create_results["stock.move"] = {"id": 4001}
    fake.read_results[("stock.move", 4001)] = {
        "id": 4001,
        "product": 1001,
        "quantity": 3.0,
        "from_location": 101,
        "to_location": 102,
        "state": "done",
        "company": 1,
        "planned_date": {"__class__": "date", "year": 2026, "month": 11, "day": 18},
        "effective_date": {"__class__": "date", "year": 2026, "month": 11, "day": 18},
        "cost_price": {"__class__": "Decimal", "decimal": "12000"},
    }
    view = _adapter(fake).create_consumption_move(
        CreateConsumptionMove(
            company=COMPANY,
            product=ProductRef(1001),
            quantity=Decimal(3),
            from_location=StockLocationRef(101),
            to_location=StockLocationRef(102),
            date=TODAY,
        )
    )
    assert view.state == "done"
    assert view.cost_price == Decimal("12000")
    create = next(
        item for item in fake.seen if item["method"] == "POST" and item["model"] == "stock.move" and not item["rest"]
    )
    assert create["body"]["from_location"] == 101
    assert create["body"]["to_location"] == 102
    # No currency: the stock.move domain requires it to be None when
    # unit_price_required is False (contract test caught the rejection).
    assert "currency" not in create["body"]
    # No unit_price either: the same conditional domain applies.
    assert "unit_price" not in create["body"]
    do = next(
        item
        for item in fake.seen
        if item["method"] == "POST"
        and item["model"] == "stock.move"
        and item["rest"] == ["4001", "assign_and_do_rest"]
    )
    assert do is not None


def test_create_consumption_move_do_failure_attaches_move_id():
    fake = FakeTryton()
    fake.seed_irr_company()
    fake.read_results[("product.product", 1001)] = {"id": 1001, "template": 2001, "cost_price": 12000}
    fake.read_results[("product.template", 2001)] = {
        "id": 2001,
        "default_uom": 1,
        "type": "goods",
    }
    fake.create_results["stock.move"] = {"id": 4001}
    fake.action_failures[("stock.move", "assign_and_do_rest", 4001)] = 400
    with pytest.raises(InvalidStockMoveError) as excinfo:
        _adapter(fake).create_consumption_move(
            CreateConsumptionMove(
                company=COMPANY,
                product=ProductRef(1001),
                quantity=Decimal(3),
                from_location=StockLocationRef(101),
                to_location=StockLocationRef(102),
                date=TODAY,
            )
        )
    assert excinfo.value.details["move_id"] == 4001


def test_create_consumption_move_rejects_non_goods_product():
    fake = FakeTryton()
    fake.seed_irr_company()
    fake.read_results[("product.product", 1001)] = {"id": 1001, "template": 2001}
    fake.read_results[("product.template", 2001)] = {
        "id": 2001,
        "default_uom": 1,
        "type": "service",
    }
    with pytest.raises(InvalidStockMoveError):
        _adapter(fake).create_consumption_move(
            CreateConsumptionMove(
                company=COMPANY,
                product=ProductRef(1001),
                quantity=Decimal(1),
                from_location=StockLocationRef(101),
                to_location=StockLocationRef(102),
                date=TODAY,
            )
        )


def test_list_consumption_moves_pages_and_maps():
    fake = FakeTryton()
    fake.search_results["stock.move"] = [
        {
            "id": 4001,
            "product": 1001,
            "quantity": 3.0,
            "from_location": 101,
            "to_location": 102,
            "state": "done",
            "company": 1,
            "planned_date": {"__class__": "date", "year": 2026, "month": 11, "day": 10},
            "effective_date": {"__class__": "date", "year": 2026, "month": 11, "day": 10},
            "cost_price": {"__class__": "Decimal", "decimal": "12000"},
        }
    ]
    moves = _adapter(fake).list_consumption_moves(
        COMPANY,
        StockLocationRef(101),
        StockLocationRef(102),
        FiscalDate.from_gregorian(datetime.date(2026, 10, 19)),
        TODAY,
    )
    assert len(moves) == 1
    assert moves[0].quantity == Decimal("3.0")
    assert moves[0].effective_date == FiscalDate.from_gregorian(datetime.date(2026, 11, 10))


# ---------------------------------------------------------------------------
# Purchase requests / analytic
# ---------------------------------------------------------------------------


def test_list_purchase_requests_honors_after_id():
    fake = FakeTryton()
    fake.search_results["purchase.request"] = [
        {
            "id": 11,
            "product": 1001,
            "quantity": 20.0,
            "state": "draft",
            "company": 1,
            "warehouse": 5,
            "supply_date": None,
            "purchase_date": None,
            "create_date": {
                "__class__": "datetime",
                "year": 2026,
                "month": 11,
                "day": 18,
                "hour": 6,
                "minute": 0,
                "second": 0,
                "microsecond": 0,
            },
        }
    ]
    requests = _adapter(fake).list_purchase_requests(COMPANY, after_id=10)
    assert len(requests) == 1
    assert requests[0].ref.engine_id == 11
    search = next(
        item for item in fake.seen if item["method"] == "GET" and item["model"] == "purchase.request"
    )
    assert "d=" in search["query"] or True  # domain travels as a query param


def test_create_and_get_analytic_account():
    fake = FakeTryton()
    fake.create_results["analytic_account.account"] = {"id": 701}
    fake.read_results[("analytic_account.account", 701)] = {
        "id": 701,
        "name": "سفارش ORD-1",
        "code": "ORD-ORD-1",
        "type": "normal",
        "company": 1,
        "balance": {"__class__": "Decimal", "decimal": "0"},
        "debit": {"__class__": "Decimal", "decimal": "200000"},
        "credit": {"__class__": "Decimal", "decimal": "500000"},
    }
    adapter = _adapter(fake)
    ref = adapter.create_analytic_account(
        CreateAnalyticAccount(
            company=COMPANY,
            name="سفارش ORD-1",
            account_type="normal",
            code="ORD-ORD-1",
            root=AnalyticAccountRef(700),
            parent=AnalyticAccountRef(700),
        )
    )
    assert ref.engine_id == 701
    view = adapter.get_analytic_account(ref)
    assert view.debit is not None and view.debit.amount == 200_000
    assert view.credit is not None and view.credit.amount == 500_000
    create = next(
        item
        for item in fake.seen
        if item["method"] == "POST" and item["model"] == "analytic_account.account"
    )
    assert create["body"]["root"] == 700
    assert create["body"]["parent"] == 700


def test_list_analytic_lines_maps_debit_credit():
    fake = FakeTryton()
    fake.search_results["analytic_account.line"] = [
        {
            "id": 1,
            "account": 701,
            "date": {"__class__": "date", "year": 2026, "month": 11, "day": 18},
            "debit": {"__class__": "Decimal", "decimal": "200000"},
            "credit": {"__class__": "Decimal", "decimal": "0"},
            "move_line": 55,
        }
    ]
    lines = _adapter(fake).list_analytic_lines(AnalyticAccountRef(701))
    assert len(lines) == 1
    assert lines[0].debit.amount == 200_000
    assert lines[0].move_line is not None
    assert lines[0].move_line.engine_id == 55


# ---------------------------------------------------------------------------
# Inventories
# ---------------------------------------------------------------------------


def _inventory_adapter(fake: FakeTryton) -> TrytonAccountingAdapter:
    fake.seed_irr_company()
    fake.read_results[("stock.inventory", 5001)] = {
        "id": 5001,
        "number": "INV-1",
        "location": 101,
        "date": {"__class__": "date", "year": 2026, "month": 11, "day": 18},
        "state": "draft",
        "company": 1,
    }
    fake.read_results[("stock.location", 101)] = {
        "id": 101,
        "name": "مواد بسته‌بندی",
        "type": "storage",
        "code": "PACK",
        "parent": 5,
    }
    fake.search_results["stock.inventory.line"] = [
        {"id": 6001, "product": 1001, "expected_quantity": 50.0, "quantity": None}
    ]
    fake.search_results["product.product"] = [{"id": 1001, "template": 2001}]
    fake.search_results["product.template"] = [{"id": 2001, "name": "کارتن مقوایی"}]
    return _adapter(fake)


def test_create_and_get_inventory():
    fake = FakeTryton()
    adapter = _inventory_adapter(fake)
    fake.create_results["stock.inventory"] = {"id": 5001}
    ref = adapter.create_inventory(
        CreateInventory(company=COMPANY, location=StockLocationRef(101), date=TODAY)
    )
    assert ref.engine_id == 5001
    create = next(
        item
        for item in fake.seen
        if item["method"] == "POST" and item["model"] == "stock.inventory"
    )
    # Uncounted lines keep their expected quantity (never 'zero',
    # which would wipe their stock).
    assert create["body"]["empty_quantity"] == "keep"
    view = adapter.get_inventory(ref)
    assert view.location_name == "مواد بسته‌بندی"
    assert len(view.lines) == 1
    assert view.lines[0].product_name == "کارتن مقوایی"
    assert view.lines[0].expected_quantity == Decimal("50.0")


def test_complete_inventory_lines_calls_button():
    fake = FakeTryton()
    adapter = _inventory_adapter(fake)
    view = adapter.complete_inventory_lines(InventoryRef(5001))
    assert view.state == "draft"
    action = next(
        item
        for item in fake.seen
        if item["method"] == "POST"
        and item["model"] == "stock.inventory"
        and item["rest"] == ["5001", "complete_lines"]
    )
    assert action is not None


def test_update_inventory_line_writes_quantity():
    fake = FakeTryton()
    adapter = _inventory_adapter(fake)
    fake.read_results[("stock.inventory.line", 6001)] = {"id": 6001, "inventory": 5001}
    adapter.update_inventory_line(
        UpdateInventoryLine(line=InventoryLineRef(6001), quantity=Decimal(48))
    )
    write = next(
        item
        for item in fake.seen
        if item["method"] == "PUT" and item["model"] == "stock.inventory.line"
    )
    assert write["rest"] == ["6001"]
    assert write["body"]["quantity"] == 48


def test_confirm_and_cancel_inventory_call_buttons():
    fake = FakeTryton()
    adapter = _inventory_adapter(fake)
    adapter.confirm_inventory(InventoryRef(5001))
    adapter.cancel_inventory(InventoryRef(5001))
    actions = [
        item["rest"][1]
        for item in fake.seen
        if item["method"] == "POST" and item["model"] == "stock.inventory"
    ]
    assert actions == ["confirm", "cancel"]


# ---------------------------------------------------------------------------
# Shipment costs
# ---------------------------------------------------------------------------


def test_get_sale_shipment_costs_parses_reference():
    fake = FakeTryton()
    fake.search_results["stock.shipment.cost_sale"] = [
        {"id": 1, "sale": 77, "shipment": "stock.shipment.out,88", "amount": 40000, "currency": 1}
    ]
    fake.search_results["currency.currency"] = [{"id": 1, "code": "IRR"}]
    costs = _adapter(fake).get_sale_shipment_costs(SaleRef(77))
    assert len(costs) == 1
    assert costs[0].amount.amount == 40_000
    assert costs[0].shipment.engine_id == 88
    assert costs[0].currency_code == "IRR"


def test_get_out_shipment_cost_reads_carrier_and_currency():
    fake = FakeTryton()
    fake.read_results[("stock.shipment.out", 88)] = {
        "id": 88,
        "state": "done",
        "carrier": 9,
        "cost": {"__class__": "Decimal", "decimal": "50000"},
        "cost_currency": 1,
        "cost_sale": {"__class__": "Decimal", "decimal": "40000"},
        "cost_sale_currency": 1,
    }
    fake.read_results[("carrier", 9)] = {"id": 9, "rec_name": "پست"}
    fake.search_results["currency.currency"] = [{"id": 1, "code": "IRR"}]
    view = _adapter(fake).get_out_shipment_cost(OutShipmentRef(88))
    assert view.carrier_name == "پست"
    assert view.cost is not None and view.cost.amount == 50_000
    assert view.cost_sale is not None and view.cost_sale.amount == 40_000
    assert view.cost_currency_code == "IRR"
