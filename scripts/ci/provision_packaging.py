# ruff: noqa: F821  # trytond-console provides pool/transaction as globals
# Provision the Tryton side of US-515 (packaging & shipping) for the
# "HesabYar Adapter Test" company (id 1) on the Tryton test DB.
#
# This is the in-repo copy of the original dev provisioning script
# (~/workspace/projects/hesabyar-reference/setup_packaging_us515.py);
# the logic is identical, only the paths changed so CI can run it from a
# bare checkout. Normally run via scripts/ci/setup-tryton.sh, which
# passes the right --lock-table flags. Run standalone with:
#   <tryton-env>/bin/trytond-console -c <trytond.conf> \
#       -d <database> \
#       --lock-table account_fiscalyear \
#       -f scripts/ci/provision_packaging.py
#
# The --lock-table flag is REQUIRED: FiscalYear.write calls lock(),
# which raises _TransactionLockError unless the table was pre-declared
# (see setup_adapter_test_company.py).
# trytond-console provides `pool` and `transaction` in the script
# namespace and ROLLS BACK the transaction when it exits -- so the
# script commits explicitly at the end.
#
# What it creates (idempotent -- safe to re-run; existing records are
# reused and missing values are filled in):
#   * stock location "مواد بسته‌بندی" (code PACK, type storage) under
#     the main warehouse WH, and "مصرف بسته‌بندی" (code PACK-CONSUME,
#     type production). Consumption moves go PACK -> PACK-CONSUME.
#   * the warehouse's lost-found location (needed by inventory counts).
#   * product category "مواد بسته‌بندی" with accounting (company 1):
#     account_stock 1.3.2 (inventory), account_stock_in 1.3.2 (the
#     receipt/invoice pair nets on the inventory account — stock_in is
#     a clearing account, and the only stock-flagged balance type is
#     Inventories), account_stock_out 5.2.2 (SG&A — the consumption
#     expense; the stock_in/out domain requires type.stock=True, which
#     the 5.1.1 expense type does NOT have), account_expense 5.1.1,
#     account_cogs 5.2.1, account_revenue 4.1.1. Materials are goods,
#     consumable=False, purchasable=True, salable=False (the
#     "consumable" business classification is NOT the Tryton consumable
#     flag -- see docs/packaging.md).
#   * analytic root "سفارش‌ها" (type root) for per-order cost centres.
#   * account_stock_method = 'anglo_saxon' on every open fiscal year
#     of company 1 (stock moves currently post NOTHING -- the method
#     is None everywhere).
#   * ir.cron "stock.order_point|supply_stock" (daily) so the supply
#     wizard runs inside Tryton; HesabYar only reads its results.
#     NOTE: the cron only fires when trytond-cron runs (a deployment
#     requirement -- it does not run in this dev environment).
#
# The script prints the engine ids at the end; store them in
# packaging settings (PUT /api/v1/packaging/settings) or let the
# service resolve them by code (PACK / PACK-CONSUME / WH).

import datetime

from trytond.transaction import Transaction

COMPANY_NAME = "HesabYar Adapter Test"
CATEGORY_NAME = "مواد بسته‌بندی"
PACK_NAME = "مواد بسته‌بندی"
PACK_CODE = "PACK"
PRODUCE_NAME = "مصرف بسته‌بندی"
PRODUCE_CODE = "PACK-CONSUME"
ANALYTIC_ROOT_NAME = "سفارش‌ها"
SUPPLY_CRON_METHOD = "stock.order_point|supply_stock"

# Chart codes (company 1) for the category's accounting.
ACCOUNT_STOCK_CODE = "1.3.2"       # Raw Material, Parts And Supplies (inventory)
ACCOUNT_STOCK_OUT_CODE = "5.2.2"   # Selling, General And Administrative (consumption expense)
ACCOUNT_EXPENSE_CODE = "5.1.1"     # Merchandise, Material, Parts And Supplies
ACCOUNT_COGS_CODE = "5.2.1"        # Cost Of Sales
ACCOUNT_REVENUE_CODE = "4.1.1"     # Goods


def get_company():
    Company = pool.get("company.company")
    companies = Company.search([("party.name", "=", COMPANY_NAME)], limit=1)
    assert companies, f"Company {COMPANY_NAME!r} not found"
    company, = companies
    print(f"Company: {company.rec_name} (id={company.id})")
    return company


def get_account_by_code(company, code):
    Account = pool.get("account.account")
    accounts = Account.search(
        [("company", "=", company.id), ("code", "=", code)], limit=1
    )
    assert accounts, f"Account {code} not found for company {company.id}"
    account, = accounts
    print(f"Account {code} {account.name!r} (id={account.id})")
    return account


def get_or_create_packaging_location(company, warehouse):
    Location = pool.get("stock.location")
    existing = Location.search([("code", "=", PACK_CODE)], limit=1)
    if existing:
        location, = existing
        print(f"Packaging location already exists (id={location.id}, "
              f"parent={location.parent.id if location.parent else None})")
        if location.parent is None or location.parent.id != warehouse.id:
            Location.write([location], {"parent": warehouse.id})
            print(f"  -> parent set to warehouse {warehouse.id}")
        return location
    location, = Location.create([{
        "name": PACK_NAME,
        "code": PACK_CODE,
        "type": "storage",
        "parent": warehouse.id,
    }])
    print(f"Created packaging location (id={location.id}, parent=warehouse {warehouse.id})")
    return location


def get_or_create_production_location():
    Location = pool.get("stock.location")
    existing = Location.search([("code", "=", PRODUCE_CODE)], limit=1)
    if existing:
        location, = existing
        print(f"Production location already exists (id={location.id})")
        return location
    location, = Location.create([{
        "name": PRODUCE_NAME,
        "code": PRODUCE_CODE,
        "type": "production",
    }])
    print(f"Created production location (id={location.id})")
    return location


def ensure_warehouse_lost_found(warehouse):
    Location = pool.get("stock.location")
    lost_found = Location.search(
        [("type", "=", "lost_found")], limit=1
    )
    if not lost_found:
        print("WARNING: no lost-found location exists; inventory counts cannot adjust")
        return
    target, = lost_found
    if warehouse.lost_found_location and warehouse.lost_found_location.id == target.id:
        print(f"Warehouse lost-found already set (id={target.id})")
        return
    Location.write([warehouse], {"lost_found_location": target.id})
    print(f"Warehouse lost-found set to location {target.id}")


def get_or_create_category(company, accounts):
    Category = pool.get("product.category")
    existing = Category.search([("name", "=", CATEGORY_NAME)], limit=1)
    values = {
        "accounting": True,
        "account_stock": accounts["stock"].id,
        "account_stock_in": accounts["stock"].id,
        "account_stock_out": accounts["stock_out"].id,
        "account_expense": accounts["expense"].id,
        "account_cogs": accounts["cogs"].id,
        "account_revenue": accounts["revenue"].id,
    }
    if existing:
        category, = existing
        print(f"Category already exists (id={category.id})")
        with Transaction().set_context(company=company.id):
            Category.write([category], values)
        print("  -> accounting values (re)applied for company "
              f"{company.id}")
        return category
    with Transaction().set_context(company=company.id):
        category, = Category.create([{"name": CATEGORY_NAME, **values}])
    print(f"Created category (id={category.id})")
    return category


def get_or_create_analytic_root(company):
    AnalyticAccount = pool.get("analytic_account.account")
    existing = AnalyticAccount.search(
        [
            ("company", "=", company.id),
            ("type", "=", "root"),
            ("name", "=", ANALYTIC_ROOT_NAME),
        ],
        limit=1,
    )
    if existing:
        root, = existing
        print(f"Analytic root already exists (id={root.id})")
        return root
    # A root account has no root and no parent (both are only required
    # for non-root types; the server leaves root None for roots).
    root, = AnalyticAccount.create([{
        "name": ANALYTIC_ROOT_NAME,
        "code": "ORDERS",
        "type": "root",
        "company": company.id,
    }])
    print(f"Created analytic root (id={root.id})")
    return root


def ensure_anglo_saxon(company):
    FiscalYear = pool.get("account.fiscalyear")
    years = FiscalYear.search(
        [("company", "=", company.id), ("state", "=", "open")]
    )
    for year in years:
        if year.account_stock_method == "anglo_saxon":
            print(f"Fiscal year {year.name!r} (id={year.id}) already anglo_saxon")
            continue
        FiscalYear.write([year], {"account_stock_method": "anglo_saxon"})
        print(f"Fiscal year {year.name!r} (id={year.id}): "
              f"account_stock_method None -> anglo_saxon")


def get_or_create_supply_cron():
    Cron = pool.get("ir.cron")
    existing = Cron.search([("method", "=", SUPPLY_CRON_METHOD)], limit=1)
    if existing:
        cron, = existing
        print(f"Supply cron already exists (id={cron.id}, active={cron.active})")
        return cron
    tomorrow_6am = (
        datetime.datetime.now(datetime.UTC)
        .replace(hour=6, minute=0, second=0, microsecond=0)
        + datetime.timedelta(days=1)
    )
    cron, = Cron.create([{
        "method": SUPPLY_CRON_METHOD,
        "interval_number": 1,
        "interval_type": "days",
        "next_call": tomorrow_6am,
        "active": True,
    }])
    print(f"Created supply cron (id={cron.id}, next_call={tomorrow_6am} UTC, daily)")
    return cron


def main():
    company = get_company()
    Location = pool.get("stock.location")
    warehouses = Location.search(
        [("type", "=", "warehouse"), ("code", "=", "WH")], limit=1
    )
    assert warehouses, "Warehouse WH not found"
    warehouse, = warehouses
    print(f"Warehouse: {warehouse.name!r} (id={warehouse.id})")

    pack = get_or_create_packaging_location(company, warehouse)
    produce = get_or_create_production_location()
    ensure_warehouse_lost_found(warehouse)

    accounts = {
        "stock": get_account_by_code(company, ACCOUNT_STOCK_CODE),
        "stock_out": get_account_by_code(company, ACCOUNT_STOCK_OUT_CODE),
        "expense": get_account_by_code(company, ACCOUNT_EXPENSE_CODE),
        "cogs": get_account_by_code(company, ACCOUNT_COGS_CODE),
        "revenue": get_account_by_code(company, ACCOUNT_REVENUE_CODE),
    }
    category = get_or_create_category(company, accounts)
    root = get_or_create_analytic_root(company)
    ensure_anglo_saxon(company)
    cron = get_or_create_supply_cron()

    transaction.commit()
    print()
    print("Provisioning complete. Engine ids for packaging settings:")
    print(f"  packaging_location_id = {pack.id}")
    print(f"  production_location_id = {produce.id}")
    print(f"  warehouse_id = {warehouse.id}")
    print(f"  packaging_category_id = {category.id}")
    print(f"  analytic_root_id = {root.id}")
    print(f"  supply_cron_id = {cron.id}")


main()
