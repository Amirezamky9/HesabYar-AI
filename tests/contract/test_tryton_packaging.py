"""Contract tests for the packaging adapter methods (US-515, Phase 5 wave 2).

These tests run against the LIVE trytond server and the
``hesabyar_tryton_test`` database, using ONLY the dedicated test company
"HesabYar Adapter Test" (provisioned by
``~/workspace/projects/hesabyar-reference/setup_adapter_test_company.py``,
plus the packaging provisioning in
``~/workspace/projects/hesabyar-reference/setup_packaging_us515.py`` —
packaging location, production location, category, analytic root,
anglo-saxon FY 2026, supply cron; see docs/packaging.md). They never
touch another company's records.

When the server is not reachable (or the test company / packaging
provisioning is missing), every test SKIPS cleanly with a clear
message -- the suite stays green without a server. With the server up,
the tests must PASS.

Done stock moves and confirmed inventories cannot be deleted, so the
flow tests leave their moves behind (like the other contract tests
leave posted journals). Products are created with a unique name per
test and deleted afterwards when still unused; used products stay.

Configuration (all optional, dev defaults match docs/dev-setup.md):
  TRYTON_BASE_URL       (default http://localhost:8000)
  TRYTON_DATABASE       (default hesabyar_tryton_test)
  TRYTON_API_KEY        (default hesabyar-dev-rest-key)
  TRYTON_TEST_COMPANY   (default "HesabYar Adapter Test")
"""

from __future__ import annotations

import datetime
import os
import uuid
from collections.abc import Generator
from decimal import Decimal

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
    ProductRef,
    SaleRef,
    StockLocationRef,
)
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.errors import (
    ProductNotFoundError,
    StockLocationNotFoundError,
)
from hesabyar.domain.packaging.errors import FifoUnavailableError
from hesabyar.infrastructure.accounting.tryton.adapter import TrytonAccountingAdapter
from hesabyar.infrastructure.accounting.tryton.client import TrytonRestClient

BASE_URL = os.environ.get("TRYTON_BASE_URL", "http://localhost:8000")
DATABASE = os.environ.get("TRYTON_DATABASE", "hesabyar_tryton_test")
API_KEY = os.environ.get("TRYTON_API_KEY", "hesabyar-dev-rest-key")
TEST_COMPANY_NAME = os.environ.get("TRYTON_TEST_COMPANY", "HesabYar Adapter Test")

CATEGORY_NAME = "مواد بسته‌بندی"
ANALYTIC_ROOT_NAME = "سفارش‌ها"

# A date inside FY 2026's open November period.
MOVE_DATE = FiscalDate.from_gregorian(datetime.date.today())

# Chart codes for the category's accounting (see setup_packaging_us515.py).
INVENTORY_ACCOUNT_CODE = "1.3.2"
CONSUMPTION_EXPENSE_CODE = "5.2.2"


@pytest.fixture(scope="module")
def client() -> Generator[TrytonRestClient, None, None]:
    # 120s timeout (not the 30s default): stock moves and inventory
    # confirm are heavy engine actions over the slow remote DB — a
    # single consumption do measured 41s and a 14-line inventory
    # confirm 53s on lane 2 (2026-11-18), and the client would time
    # out even though the server commits (same pattern as
    # test_tryton_adapter_sales.py).
    rest_client = TrytonRestClient(BASE_URL, DATABASE, API_KEY, timeout=120.0)
    if not rest_client.ping():
        rest_client.close()
        pytest.skip(
            f"trytond server not reachable at {BASE_URL} (start it per docs/dev-setup.md §3 to run the contract tests)"
        )
    yield rest_client
    rest_client.close()


@pytest.fixture(scope="module")
def adapter(client: TrytonRestClient) -> TrytonAccountingAdapter:
    return TrytonAccountingAdapter(client)


@pytest.fixture(scope="module")
def company(adapter: TrytonAccountingAdapter) -> CompanyRef:
    for view in adapter.list_companies():
        if view.name == TEST_COMPANY_NAME:
            return view.ref
    pytest.skip(
        f"Test company {TEST_COMPANY_NAME!r} not found in Tryton DB {DATABASE!r}. "
        "Provision it with setup_adapter_test_company.py (see docs/adapter.md)."
    )


@pytest.fixture(scope="module")
def locations(adapter: TrytonAccountingAdapter, company: CompanyRef) -> dict[str, StockLocationRef]:
    by_code = {loc.code: loc.ref for loc in adapter.list_stock_locations(company) if loc.code}
    missing = [code for code in ("WH", "PACK", "PACK-CONSUME", "IN") if code not in by_code]
    if missing:
        pytest.skip(
            f"Packaging locations missing from test company: {missing}. "
            "Provision them with setup_packaging_us515.py (see docs/packaging.md)."
        )
    return by_code


@pytest.fixture(scope="module")
def category_id(client: TrytonRestClient) -> int:
    records = client.search(
        "product.category",
        domain=[("name", "=", CATEGORY_NAME)],
        fields=["name"],
    )
    if not records:
        pytest.skip(
            f"Product category {CATEGORY_NAME!r} not found. "
            "Provision it with setup_packaging_us515.py (see docs/packaging.md)."
        )
    return int(records[0]["id"])


@pytest.fixture(scope="module")
def analytic_root(client: TrytonRestClient, company: CompanyRef) -> AnalyticAccountRef:
    records = client.search(
        "analytic_account.account",
        domain=[
            ("company", "=", company.engine_id),
            ("type", "=", "root"),
            ("name", "=", ANALYTIC_ROOT_NAME),
        ],
        fields=["name"],
    )
    if not records:
        pytest.skip(
            f"Analytic root {ANALYTIC_ROOT_NAME!r} not found. "
            "Provision it with setup_packaging_us515.py (see docs/packaging.md)."
        )
    return AnalyticAccountRef(int(records[0]["id"]))


def _create_test_product(
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    category_id: int,
) -> ProductRef:
    """Create a uniquely named packaging product (average cost)."""
    return adapter.create_packaging_product(
        CreatePackagingProduct(
            company=company,
            name=f"US-515 test {uuid.uuid4().hex[:8]}",
            category_engine_id=category_id,
            cost_method="average",
        )
    )


@pytest.fixture()
def product(
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    category_id: int,
    client: TrytonRestClient,
) -> Generator[ProductRef, None, None]:
    ref = _create_test_product(adapter, company, category_id)
    yield ref
    # Best-effort cleanup: delete the variant, then its template.
    # Used products (moves reference them) cannot be deleted and stay.
    try:
        record = client.read("product.product", ref.engine_id, fields=["template"])
        if record is None:
            return
        template_id = int(record["template"])
        client.delete("product.product", ref.engine_id)
        client.delete("product.template", template_id)
    except Exception:  # noqa: BLE001 - cleanup is best-effort by design
        pass


def _receive_stock(
    client: TrytonRestClient,
    company: CompanyRef,
    product: ProductRef,
    locations: dict[str, StockLocationRef],
    quantity: Decimal,
    unit_price: Decimal,
) -> int:
    """Receive stock into PACK (a done move) and set the product cost.

    The receipt comes from the Input location, NOT the supplier: the
    ``purchase`` module requires supplier moves to carry an origin
    (a purchase line), which a test helper cannot fabricate. An
    input→storage move needs no currency/unit price
    (``unit_price_required`` is False). Note the Input Zone is type
    ``storage`` on the dev DB, so the plain ``do`` button is readonly
    on the draft (assignation required) — the helper completes the
    move with the same ``assign_and_do_rest`` wrapper the adapter
    uses. The product's cost price is written BEFORE the move: once
    a product has stock moves its cost price is locked ("You must
    use the Modify Cost Price wizard"), so the write has to come
    first — the receipt then posts at ``unit_price``.
    """
    client.write(
        "product.product",
        product.engine_id,
        {"cost_price": float(unit_price)},
        context={"company": company.engine_id},
    )
    record = client.create(
        "stock.move",
        {
            "product": product.engine_id,
            "unit": 1,  # UoM "Unit"
            "quantity": float(quantity),
            "from_location": locations["IN"].engine_id,
            "to_location": locations["PACK"].engine_id,
            "company": company.engine_id,
            "planned_date": MOVE_DATE.gregorian_date.isoformat(),
        },
    )
    move_id = int(record["id"])
    client.action("stock.move", "assign_and_do_rest", move_id)
    return move_id


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------


def test_locations_provisioned(
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    locations: dict[str, StockLocationRef],
) -> None:
    pack = adapter.get_stock_location(locations["PACK"])
    assert pack.location_type == "storage"
    assert pack.parent is not None
    assert pack.parent.engine_id == locations["WH"].engine_id
    produce = adapter.get_stock_location(locations["PACK-CONSUME"])
    assert produce.location_type == "production"


def test_supply_cron_exists(client: TrytonRestClient) -> None:
    records = client.search(
        "ir.cron",
        domain=[("method", "=", "stock.order_point|supply_stock")],
        fields=["method", "active"],
    )
    assert records, "stock.order_point|supply_stock cron not found (run setup_packaging_us515.py)"
    assert records[0]["active"] is True


def test_get_stock_location_missing_raises(
    adapter: TrytonAccountingAdapter,
) -> None:
    with pytest.raises(StockLocationNotFoundError):
        adapter.get_stock_location(StockLocationRef(999999))


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------


def test_create_packaging_product_average(
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    category_id: int,
    product: ProductRef,
) -> None:
    view = adapter.get_packaging_product(product, company)
    assert view.name.startswith("US-515 test")
    assert view.cost_method == "average"
    assert view.template_id > 0
    # The modelling decision: materials are goods, NOT the Tryton
    # consumable flag (which would exclude them from order points).
    assert view.product_type == "goods"
    assert view.consumable is False
    assert view.purchasable is True


def test_create_packaging_product_fifo_unavailable(
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    category_id: int,
) -> None:
    with pytest.raises(FifoUnavailableError):
        adapter.create_packaging_product(
            CreatePackagingProduct(
                company=company,
                name=f"US-515 fifo {uuid.uuid4().hex[:8]}",
                category_engine_id=category_id,
                cost_method="fifo",
            )
        )


def test_get_packaging_product_missing_raises(
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
) -> None:
    with pytest.raises(ProductNotFoundError):
        adapter.get_packaging_product(ProductRef(999999), company)


# ---------------------------------------------------------------------------
# Order points
# ---------------------------------------------------------------------------


def test_order_point_upsert_and_read(
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    locations: dict[str, StockLocationRef],
    product: ProductRef,
    client: TrytonRestClient,
) -> None:
    warehouse = locations["WH"]
    created = adapter.upsert_order_point(
        UpsertOrderPoint(
            company=company,
            product=product,
            warehouse=warehouse,
            min_quantity=Decimal(10),
            target_quantity=Decimal(50),
        )
    )
    assert created.min_quantity == Decimal(10)
    assert created.target_quantity == Decimal(50)
    assert created.warehouse.engine_id == warehouse.engine_id
    try:
        updated = adapter.upsert_order_point(
            UpsertOrderPoint(
                company=company,
                product=product,
                warehouse=warehouse,
                min_quantity=Decimal(5),
                target_quantity=Decimal(40),
            )
        )
        assert updated.ref == created.ref  # same point, updated in place
        assert updated.min_quantity == Decimal(5)
        assert updated.target_quantity == Decimal(40)

        read_back = adapter.get_order_point(product, warehouse, company)
        assert read_back is not None
        assert read_back.min_quantity == Decimal(5)
    finally:
        client.delete("stock.order_point", created.ref.engine_id)


# ---------------------------------------------------------------------------
# Consumption flow (stock + accounting)
# ---------------------------------------------------------------------------


def test_consumption_move_posts_anglo_saxon_accounting(
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    locations: dict[str, StockLocationRef],
    product: ProductRef,
    client: TrytonRestClient,
) -> None:
    """Receive 10 @ 12000, consume 3 → stock 7 and a 36000 accounting move.

    The consumption move (PACK → production) posts, under anglo-saxon,
    debit 5.2.2 (SG&A — the packaging expense) / credit 1.3.2
    (inventory). This is the proof the FY stock method provisioning
    works: with account_stock_method None, no account move is created.
    """
    _receive_stock(client, company, product, locations, Decimal(10), Decimal(12000))
    stocked = adapter.get_packaging_product(product, company, locations["PACK"])
    assert stocked.quantity == Decimal(10)
    assert stocked.cost_price == Decimal(12000)

    move = adapter.create_consumption_move(
        CreateConsumptionMove(
            company=company,
            product=product,
            quantity=Decimal(3),
            from_location=locations["PACK"],
            to_location=locations["PACK-CONSUME"],
            date=MOVE_DATE,
        )
    )
    assert move.state == "done"
    assert move.cost_price == Decimal(12000)

    after = adapter.get_packaging_product(product, company, locations["PACK"])
    assert after.quantity == Decimal(7)

    # The engine's accounting move for the stock move.
    account_moves = client.search(
        "account.move",
        domain=[("origin", "=", f"stock.move,{move.ref.engine_id}")],
        fields=["state"],
    )
    assert account_moves, "No account.move for the consumption move — is FY 2026 anglo_saxon?"
    account_move_id = int(account_moves[0]["id"])
    lines = client.search(
        "account.move.line",
        domain=[("move", "=", account_move_id)],
        fields=["account", "debit", "credit"],
    )
    assert len(lines) == 2
    by_code: dict[str, dict] = {}
    for line in lines:
        account = client.read("account.account", int(line["account"]), fields=["code"])
        assert account is not None
        by_code[account["code"]] = line
    assert set(by_code) == {INVENTORY_ACCOUNT_CODE, CONSUMPTION_EXPENSE_CODE}
    assert Decimal(str(by_code[CONSUMPTION_EXPENSE_CODE]["debit"])) == Decimal(36000)
    assert Decimal(str(by_code[INVENTORY_ACCOUNT_CODE]["credit"])) == Decimal(36000)


def test_consumption_past_zero_goes_negative(
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    locations: dict[str, StockLocationRef],
    product: ProductRef,
) -> None:
    """``do`` does not check availability — over-consumption is a signal, not an error."""
    move = adapter.create_consumption_move(
        CreateConsumptionMove(
            company=company,
            product=product,
            quantity=Decimal(2),
            from_location=locations["PACK"],
            to_location=locations["PACK-CONSUME"],
            date=MOVE_DATE,
        )
    )
    assert move.state == "done"
    after = adapter.get_packaging_product(product, company, locations["PACK"])
    assert after.quantity == Decimal(-2)


# ---------------------------------------------------------------------------
# Analytic accounts
# ---------------------------------------------------------------------------


def test_create_analytic_account_under_root(
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    analytic_root: AnalyticAccountRef,
    client: TrytonRestClient,
) -> None:
    ref = adapter.create_analytic_account(
        CreateAnalyticAccount(
            company=company,
            name="سفارش US-515",
            account_type="normal",
            code="ORD-US515",
            root=analytic_root,
            parent=analytic_root,
        )
    )
    try:
        view = adapter.get_analytic_account(ref)
        assert view.name == "سفارش US-515"
        assert view.account_type == "normal"
        # The view carries no root/parent — verify them on the raw record.
        record = client.read("analytic_account.account", ref.engine_id, fields=["root", "parent"])
        assert record is not None
        assert int(record["root"]) == analytic_root.engine_id
        assert int(record["parent"]) == analytic_root.engine_id
    finally:
        client.delete("analytic_account.account", ref.engine_id)


def test_list_analytic_lines_empty_for_new_account(
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    analytic_root: AnalyticAccountRef,
    client: TrytonRestClient,
) -> None:
    ref = adapter.create_analytic_account(
        CreateAnalyticAccount(
            company=company,
            name="سفارش US-515 خالی",
            account_type="normal",
            code="ORD-US515-EMPTY",
            root=analytic_root,
            parent=analytic_root,
        )
    )
    try:
        assert list(adapter.list_analytic_lines(ref)) == []
    finally:
        client.delete("analytic_account.account", ref.engine_id)


# ---------------------------------------------------------------------------
# Purchase requests + shipment costs (read paths)
# ---------------------------------------------------------------------------


def test_list_purchase_requests_read_path(
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
) -> None:
    requests = adapter.list_purchase_requests(company)
    assert isinstance(requests, list)
    for request in requests:
        assert request.state in ("draft", "processing", "done", "cancelled", "rejected")


def test_get_sale_shipment_costs_unknown_sale_is_empty(
    adapter: TrytonAccountingAdapter,
) -> None:
    assert list(adapter.get_sale_shipment_costs(SaleRef(999999))) == []


# ---------------------------------------------------------------------------
# Inventory counts
# ---------------------------------------------------------------------------


def test_inventory_count_lifecycle(
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    locations: dict[str, StockLocationRef],
    product: ProductRef,
    client: TrytonRestClient,
) -> None:
    """Create → complete lines → count → confirm; the variance adjusts stock."""
    _receive_stock(client, company, product, locations, Decimal(10), Decimal(12000))
    ref = adapter.create_inventory(
        CreateInventory(
            company=company,
            location=locations["PACK"],
            date=MOVE_DATE,
        )
    )
    view = adapter.complete_inventory_lines(ref)
    assert view.state == "draft"
    line = next(
        (item for item in view.lines if item.product.engine_id == product.engine_id),
        None,
    )
    assert line is not None
    assert line.expected_quantity == Decimal(10)
    assert line.quantity is None

    # Count 9 (one short) and confirm: the engine adjusts the stock.
    updated = adapter.update_inventory_line(UpdateInventoryLine(line=line.ref, quantity=Decimal(9)))
    counted = next(item for item in updated.lines if item.ref == line.ref)
    assert counted.quantity == Decimal(9)

    done = adapter.confirm_inventory(ref)
    assert done.state == "done"

    after = adapter.get_packaging_product(product, company, locations["PACK"])
    assert after.quantity == Decimal(9)
