"""Contract tests for the US-502 port extension (sales, invoice
finalization, credit notes, payment workflow, chart update).

These tests run against the LIVE trytond server and the
``hesabyar_tryton_test`` database, using ONLY the dedicated test company
"HesabYar Adapter Test" (provisioned by
``~/workspace/projects/hesabyar-reference/setup_adapter_test_company.py``;
see docs/adapter.md). They never touch another company's records.

When the server is not reachable (or the test company has not been
provisioned yet), every test SKIPS cleanly with a clear message -- the suite
stays green without a server. With the server up, the tests must PASS.

The F2 modules (sale family, stock family, clearing, dunning,
hesabyar_setup) must be ACTIVATED in the test DB first -- see
docs/tryton-modules.md and scripts/activate-tryton-modules.sh. The
first test asserts the module set explicitly so a missing activation
fails loudly instead of surfacing as confusing downstream errors.

Provisioning prerequisite: the test company needs
``account.configuration`` defaults (``default_account_receivable``
1.2.1 / ``default_account_payable`` 2.1.1) -- the sale flow resolves
the invoice account through the party fallback to these defaults, and
the original provisioning script never set them (it only pre-filled
when exactly one receivable/payable account existed; the universal
chart has three of each). Set once via REST, e.g. with the company
context (see docs/tryton-modules.md).

Configuration (all optional, dev defaults match docs/dev-setup.md):
  TRYTON_BASE_URL       (default http://localhost:8000)
  TRYTON_DATABASE       (default hesabyar_tryton_test)
  TRYTON_API_KEY        (default hesabyar-dev-rest-key)
  TRYTON_TEST_COMPANY   (default "HesabYar Adapter Test")

NOTE: the dev server serves Supabase on :8010, so run with
TRYTON_BASE_URL=http://localhost:8010 (the .env.supabase file only sets
HESABYAR_TRYTON__BASE_URL, which these tests do not read).
"""

from __future__ import annotations

import datetime
import os
from collections.abc import Generator
from decimal import Decimal

import pytest

from hesabyar.domain.accounting.commands import (
    CreateCreditNote,
    CreateInvoiceDraft,
    CreatePayment,
    CreateSale,
    InvoiceDraftLine,
    SaleLine,
    SucceedPayment,
)
from hesabyar.domain.accounting.refs import (
    CompanyRef,
    InvoiceRef,
    MoveLineRef,
    PartyAddressRef,
    PartyRef,
    PaymentJournalRef,
    ProductRef,
    SaleRef,
)
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.errors import (
    InvalidInvoiceError,
    InvalidPaymentError,
    ProductNotFoundError,
)
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.infrastructure.accounting.tryton.adapter import TrytonAccountingAdapter
from hesabyar.infrastructure.accounting.tryton.client import TrytonRestClient

BASE_URL = os.environ.get("TRYTON_BASE_URL", "http://localhost:8000")
DATABASE = os.environ.get("TRYTON_DATABASE", "hesabyar_tryton_test")
API_KEY = os.environ.get("TRYTON_API_KEY", "hesabyar-dev-rest-key")
TEST_COMPANY_NAME = os.environ.get("TRYTON_TEST_COMPANY", "HesabYar Adapter Test")
TEST_JOURNAL_NAME = "HesabYar Adapter Test General Journal"

# Chart codes of the universal chart (see test_tryton_adapter.py).
CASH_ACCOUNT_CODE = "1.1.1"
RECEIVABLE_ACCOUNT_CODE = "1.2.1"
PAYABLE_ACCOUNT_CODE = "2.1.1"
REVENUE_ACCOUNT_CODE = "4.1.1"
SERVICES_ACCOUNT_CODE = "4.1.2"

# The ten HesabYar template accounts (tryton-modules/hesabyar_setup).
HESABYAR_ACCOUNT_CODES = (
    "1.2.4",
    "1.2.5",
    "2.1.5",
    "7.0.0",
    "7.1.0",
    "7.1.1",
    "7.1.2",
    "7.2.0",
    "7.2.1",
    "7.2.2",
)

# F2 modules that must be activated for these tests (US-502).
F2_MODULES = (
    "sale",
    "sale_shipment_cost",
    "sale_payment",
    "sale_discount",
    "sale_amendment",
    "sale_invoice_grouping",
    "account_payment_clearing",
    "account_dunning",
    "account_dunning_fee",
    "stock",
    "stock_supply",
    "purchase_request",
    "stock_package",
    "stock_shipment_cost",
    "carrier",
    "account_stock_landed_cost",
    "account_stock_anglo_saxon",
    "analytic_sale",
    "account_statement_rule",
    "hesabyar_setup",
)

CUSTOMER_NAME = "HesabYar US-502 Contract Customer"
GATEWAY_NAME = "HesabYar US-502 Contract Gateway"
PRODUCT_NAME = "HesabYar US-502 Contract Widget"
PAYMENT_JOURNAL_NAME = "HesabYar US-502 Contract Payments"
CLEARING_JOURNAL_NAME = "HesabYar US-502 Contract Clearing"

# The sale/invoice dates are dynamic (today), NOT fixed: the shared
# Tryton DB accumulates posted customer invoices from other tests
# (e.g. the US-511 webhook fixtures dated 2026-11-17), and Tryton
# numbers invoices chronologically — a fixed date eventually falls
# behind the sequence and posting fails. "Today" always stays ahead
# of past test residue. The clearing date stays 4 days after the
# move date, inside the same open period.
MOVE_DATE = FiscalDate.from_gregorian(datetime.date.today())
CLEARING_DATE = FiscalDate.from_gregorian(
    datetime.date.today() + datetime.timedelta(days=4)
)


@pytest.fixture(scope="module")
def client() -> Generator[TrytonRestClient, None, None]:
    # 120s timeout (not the 30s default): update_chart is a heavy admin
    # action -- creating 10 accounts + types over the slow remote DB
    # exceeds 30s on a fresh chart, and the client would time out even
    # though the server commits.
    rest_client = TrytonRestClient(BASE_URL, DATABASE, API_KEY, timeout=120.0)
    if not rest_client.ping():
        rest_client.close()
        pytest.skip(
            f"trytond server not reachable at {BASE_URL} "
            "(start it per docs/dev-setup.md §3 to run the contract tests)"
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
def irr_currency_id(client: TrytonRestClient) -> int:
    records = client.search(
        "currency.currency", domain=[("code", "=", "IRR")], fields=["code"]
    )
    assert records, "IRR currency not found in the test DB"
    return int(records[0]["id"])


@pytest.fixture(scope="module")
def accounts(adapter: TrytonAccountingAdapter, company: CompanyRef):
    chart = {view.code: view for view in adapter.get_chart_of_accounts(company)}
    wanted = (
        CASH_ACCOUNT_CODE,
        RECEIVABLE_ACCOUNT_CODE,
        PAYABLE_ACCOUNT_CODE,
        REVENUE_ACCOUNT_CODE,
        SERVICES_ACCOUNT_CODE,
    )
    missing = [code for code in wanted if code not in chart]
    if missing:
        pytest.skip(f"Chart accounts missing from test company: {missing}.")
    return {code: chart[code] for code in wanted}


@pytest.fixture(scope="module")
def general_journal(client: TrytonRestClient) -> int:
    records = client.search(
        "account.journal",
        domain=[("name", "=", TEST_JOURNAL_NAME), ("type", "=", "general")],
        fields=["name"],
    )
    if not records:
        pytest.skip(
            f"Test journal {TEST_JOURNAL_NAME!r} not found. "
            "Provision it with setup_adapter_test_company.py (see docs/adapter.md)."
        )
    return int(records[0]["id"])


@pytest.fixture(scope="module")
def customer(client: TrytonRestClient):
    """A customer party with one address, created via REST.

    Teardown is best-effort: posted invoices leave move lines
    referencing the party, which blocks deletion (expected residue,
    like the posted moves the other contract tests leave behind).
    """
    party = client.create("party.party", {"name": CUSTOMER_NAME}, fields=["id"])
    party_id = int(party["id"])
    address = client.create(
        "party.address", {"party": party_id, "city": "Tehran"}, fields=["id"]
    )
    address_id = int(address["id"])
    yield PartyRef(party_id), PartyAddressRef(address_id)
    for model, rid in (
        ("party.address", address_id),
        ("party.party", party_id),
    ):
        try:
            client.delete(model, rid)
        except Exception:
            pass  # referenced by posted invoice move lines


@pytest.fixture(scope="module")
def gateway(client: TrytonRestClient):
    """A gateway party (alternative payee), created via REST.

    Teardown is best-effort: the alternative-payee test posts an
    invoice whose receivable line references the gateway party.
    """
    party = client.create("party.party", {"name": GATEWAY_NAME}, fields=["id"])
    party_id = int(party["id"])
    yield PartyRef(party_id)
    try:
        client.delete("party.party", party_id)
    except Exception:
        pass  # referenced by a posted invoice move line


@pytest.fixture(scope="module")
def unit_uom(client: TrytonRestClient) -> int:
    records = client.search(
        "product.uom", domain=[("symbol", "=", "u")], fields=["symbol"]
    )
    if not records:
        records = client.search(
            "product.uom", domain=[("name", "=", "Unit")], fields=["name"]
        )
    if not records:
        pytest.skip("No 'Unit' UoM in the test DB.")
    return int(records[0]["id"])


@pytest.fixture(scope="module")
def sellable_product(
    client: TrytonRestClient,
    company: CompanyRef,
    accounts,
    unit_uom: int,
):
    """A sellable goods product with a revenue account, via REST.

    The revenue account comes from a product CATEGORY
    (``product.category.account``), not the template: ``product.template``
    has no ``account_revenue`` field at all (``account_product`` only
    defines it on the category; ``Template.account_revenue_used``
    resolves through ``account_category``). Without it the sale's
    invoice creation fails (sale.msg_sale_product_missing_account_revenue).
    """
    category = client.create(
        "product.category",
        {"name": PRODUCT_NAME + " Category", "accounting": True},
        fields=["id"],
    )
    category_id = int(category["id"])
    cat_account = client.create(
        "product.category.account",
        {
            "category": category_id,
            "company": company.engine_id,
            "account_revenue": accounts[REVENUE_ACCOUNT_CODE].ref.engine_id,
        },
        fields=["id"],
        context={"company": company.engine_id},
    )
    cat_account_id = int(cat_account["id"])
    template = client.create(
        "product.template",
        {
            "name": PRODUCT_NAME,
            "type": "goods",
            "default_uom": unit_uom,
            "salable": True,
            "sale_uom": unit_uom,
            "list_price": Decimal("250000"),
            "account_category": category_id,
        },
        fields=["id"],
        context={"company": company.engine_id},
    )
    template_id = int(template["id"])
    product = client.create(
        "product.product",
        {"template": template_id},
        fields=["id"],
        context={"company": company.engine_id},
    )
    product_id = int(product["id"])
    yield ProductRef(product_id)
    client.delete("product.product", product_id)
    client.delete("product.template", template_id)
    client.delete("product.category.account", cat_account_id)
    client.delete("product.category", category_id)


@pytest.fixture
def sale_flow_data(
    client: TrytonRestClient,
    company: CompanyRef,
    accounts,
    unit_uom: int,
):
    """Own party + category + product for the full sale-flow test.

    A processed sale can never be cancelled (Tryton's sale workflow
    allows cancel only from draft/quotation), so the flow test's sale
    stays in the shared DB as test residue -- like the posted moves the
    other contract tests leave behind. It gets its own party/product so
    the module fixtures stay clean and deletable. Teardown is
    best-effort: the sale blocks deleting its party/product.
    """
    party = client.create(
        "party.party", {"name": CUSTOMER_NAME + " (sale flow)"}, fields=["id"]
    )
    party_id = int(party["id"])
    address = client.create(
        "party.address", {"party": party_id, "city": "Tehran"}, fields=["id"]
    )
    address_id = int(address["id"])
    category = client.create(
        "product.category",
        {"name": PRODUCT_NAME + " (sale flow) Category", "accounting": True},
        fields=["id"],
    )
    category_id = int(category["id"])
    cat_account = client.create(
        "product.category.account",
        {
            "category": category_id,
            "company": company.engine_id,
            "account_revenue": accounts[REVENUE_ACCOUNT_CODE].ref.engine_id,
        },
        fields=["id"],
        context={"company": company.engine_id},
    )
    cat_account_id = int(cat_account["id"])
    template = client.create(
        "product.template",
        {
            "name": PRODUCT_NAME + " (sale flow)",
            "type": "goods",
            "default_uom": unit_uom,
            "salable": True,
            "sale_uom": unit_uom,
            "list_price": Decimal("250000"),
            "account_category": category_id,
        },
        fields=["id"],
        context={"company": company.engine_id},
    )
    template_id = int(template["id"])
    product = client.create(
        "product.product",
        {"template": template_id},
        fields=["id"],
        context={"company": company.engine_id},
    )
    product_id = int(product["id"])
    yield PartyRef(party_id), ProductRef(product_id)
    for model, rid in (
        ("party.address", address_id),
        ("party.party", party_id),
        ("product.product", product_id),
        ("product.template", template_id),
        ("product.category.account", cat_account_id),
        ("product.category", category_id),
    ):
        try:
            client.delete(model, rid)
        except Exception:
            pass  # residue of the un-cancellable processed sale


@pytest.fixture(scope="module")
def payment_journal(
    client: TrytonRestClient, company: CompanyRef, irr_currency_id: int
):
    """A manual payment journal, created via REST.

    Teardown is best-effort: payments created by the tests reference
    the journal, and succeeded payments cannot be deleted.
    """
    record = client.create(
        "account.payment.journal",
        {
            "name": PAYMENT_JOURNAL_NAME,
            "currency": irr_currency_id,
            "company": company.engine_id,
            "process_method": "manual",
        },
        fields=["id"],
        context={"company": company.engine_id},
    )
    journal_id = int(record["id"])
    yield PaymentJournalRef(journal_id)
    try:
        client.delete(
            "account.payment.journal",
            journal_id,
            context={"company": company.engine_id},
        )
    except Exception:
        pass  # referenced by test payments


@pytest.fixture(scope="module")
def clearing_payment_journal(
    client: TrytonRestClient,
    company: CompanyRef,
    irr_currency_id: int,
    accounts,
    general_journal: int,
):
    """A manual payment journal WITH a clearing account + journal.

    The clearing account must be postable, open and NOT party_required
    (account_payment_clearing domain) -- cash 1.1.1 qualifies.
    """
    record = client.create(
        "account.payment.journal",
        {
            "name": CLEARING_JOURNAL_NAME,
            "currency": irr_currency_id,
            "company": company.engine_id,
            "process_method": "manual",
            "clearing_account": accounts[CASH_ACCOUNT_CODE].ref.engine_id,
            "clearing_journal": general_journal,
        },
        fields=["id"],
        context={"company": company.engine_id},
    )
    journal_id = int(record["id"])
    yield PaymentJournalRef(journal_id)
    try:
        client.delete(
            "account.payment.journal",
            journal_id,
            context={"company": company.engine_id},
        )
    except Exception:
        pass  # referenced by test payments


@pytest.fixture
def created_sales(client: TrytonRestClient) -> Generator[list[int], None, None]:
    """Track sales created by a test; cancel + delete them afterwards."""
    ids: list[int] = []
    yield ids
    for sale_id in ids:
        try:
            record = client.read("sale.sale", sale_id, fields=["state"])
        except Exception:
            continue
        if record is None or record["state"] not in ("draft", "cancelled"):
            try:
                client.action("sale.sale", "cancel", sale_id)
            except Exception:
                continue
        try:
            client.delete("sale.sale", sale_id)
        except Exception:
            pass


@pytest.fixture
def created_invoices(client: TrytonRestClient) -> Generator[list[int], None, None]:
    """Track invoices created by a test and delete them afterwards (drafts only)."""
    ids: list[int] = []
    yield ids
    for invoice_id in ids:
        try:
            client.delete("account.invoice", invoice_id)
        except Exception:
            pass


@pytest.fixture
def created_payments(client: TrytonRestClient) -> Generator[list[int], None, None]:
    ids: list[int] = []
    yield ids
    for payment_id in ids:
        try:
            client.delete("account.payment", payment_id)
        except Exception:
            pass


def _sale_command(
    company: CompanyRef,
    party: PartyRef,
    product: ProductRef,
    date: FiscalDate = MOVE_DATE,
) -> CreateSale:
    return CreateSale(
        company=company,
        party=party,
        date=date,
        lines=(
            SaleLine(
                product=product,
                unit_price=MoneyIRR(250_000),
                quantity=Decimal(2),
                description="Widget",
            ),
        ),
    )


def _invoice_command(
    company: CompanyRef,
    party: PartyRef,
    accounts,
    date: FiscalDate = MOVE_DATE,
) -> CreateInvoiceDraft:
    return CreateInvoiceDraft(
        company=company,
        invoice_type="out",
        party=party,
        date=date,
        account=accounts[RECEIVABLE_ACCOUNT_CODE].ref,
        lines=(
            InvoiceDraftLine(
                account=accounts[REVENUE_ACCOUNT_CODE].ref,
                unit_price=MoneyIRR(500_000),
                quantity=Decimal(1),
                description="Goods",
            ),
            InvoiceDraftLine(
                account=accounts[SERVICES_ACCOUNT_CODE].ref,
                unit_price=MoneyIRR(125_000),
                quantity=Decimal(2),
                description="Services",
            ),
        ),
        description="US-502 contract test invoice",
    )


def _posted_invoice(
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    party: PartyRef,
    accounts,
) -> InvoiceRef:
    """Create + validate + post an invoice; returns its ref."""
    ref = adapter.create_invoice_draft(_invoice_command(company, party, accounts))
    adapter.validate_invoice(ref)
    adapter.post_invoice(ref)
    return ref


def _receivable_move_line(
    client: TrytonRestClient, invoice_id: int, receivable_account_id: int
) -> int:
    """The posted invoice's receivable move line id."""
    invoice = client.read("account.invoice", invoice_id, fields=["move"])
    assert invoice is not None and invoice["move"], "posted invoice has no move"
    lines = client.search(
        "account.move.line",
        domain=[
            ("move", "=", int(invoice["move"])),
            ("account", "=", receivable_account_id),
        ],
        fields=["party"],
    )
    assert len(lines) == 1, f"expected 1 receivable line, got {lines}"
    return int(lines[0]["id"])


# ---------------------------------------------------------------------------
# Module activation
# ---------------------------------------------------------------------------


def test_f2_modules_activated(client: TrytonRestClient):
    records = client.search(
        "ir.module",
        domain=[("name", "in", list(F2_MODULES))],
        fields=["name", "state"],
    )
    by_name = {record["name"]: record["state"] for record in records}
    missing = [name for name in F2_MODULES if by_name.get(name) != "activated"]
    assert not missing, (
        f"F2 modules not activated in {DATABASE!r}: {missing}. "
        "Run scripts/activate-tryton-modules.sh (see docs/tryton-modules.md)."
    )


# ---------------------------------------------------------------------------
# Chart update (hesabyar_setup)
# ---------------------------------------------------------------------------


def test_update_chart_adds_hesabyar_accounts(
    client: TrytonRestClient, adapter: TrytonAccountingAdapter, company: CompanyRef
):
    # Idempotent: a second run adds 0 accounts and still reports the chart.
    summary = client.action("company.company", "update_chart", company.engine_id)
    assert summary["company"] == company.engine_id
    assert summary["accounts_after"] >= summary["accounts_before"]
    chart = {
        view.code: view
        for view in adapter.get_chart_of_accounts(company)
    }
    missing = [code for code in HESABYAR_ACCOUNT_CODES if code not in chart]
    assert not missing, f"HesabYar chart accounts missing: {missing}"
    # Spot-check the memorandum leaf: off-balance type, party required.
    memorandum = chart["7.1.1"]
    assert memorandum.party_required is True
    assert memorandum.type_name == "Off-Balance"


# ---------------------------------------------------------------------------
# Sales: store order -> Sale -> invoice
# ---------------------------------------------------------------------------


def test_sale_full_flow_creates_invoice(
    adapter: TrytonAccountingAdapter,
    client: TrytonRestClient,
    company: CompanyRef,
    sale_flow_data,
    created_sales,
    created_invoices,
):
    party, product = sale_flow_data
    ref = adapter.create_sale(_sale_command(company, party, product))
    created_sales.append(ref.engine_id)
    view = adapter.get_sale(ref)
    assert view is not None
    assert view.is_draft
    assert view.total_amount == MoneyIRR(500_000)
    assert len(view.lines) == 1
    assert view.lines[0].product == product

    quoted = adapter.quote_sale(ref)
    assert quoted.state == "quotation"
    assert quoted.number
    confirmed = adapter.confirm_sale(ref)
    assert confirmed.state == "confirmed"
    processed = adapter.process_sale(ref)
    assert processed.state == "processing"
    assert len(processed.invoices) == 1

    invoice = adapter.get_invoice(processed.invoices[0])
    assert invoice is not None
    assert invoice.state == "draft"
    assert invoice.total_amount == MoneyIRR(500_000)
    created_invoices.append(invoice.ref.engine_id)


def test_create_sale_rejects_unknown_product(
    adapter: TrytonAccountingAdapter, company: CompanyRef, customer
):
    party, _address = customer
    with pytest.raises(ProductNotFoundError):
        adapter.create_sale(
            _sale_command(company, party, ProductRef(999_999_999))
        )


def test_create_sale_rejects_non_sellable_product(
    adapter: TrytonAccountingAdapter,
    client: TrytonRestClient,
    company: CompanyRef,
    customer,
    unit_uom: int,
):
    party, _address = customer
    template = client.create(
        "product.template",
        {
            "name": "HesabYar US-502 Non-sellable",
            "type": "goods",
            "default_uom": unit_uom,
            "salable": False,
        },
        fields=["id"],
        context={"company": company.engine_id},
    )
    template_id = int(template["id"])
    product = client.create(
        "product.product",
        {"template": template_id},
        fields=["id"],
        context={"company": company.engine_id},
    )
    product_id = int(product["id"])
    try:
        with pytest.raises(ProductNotFoundError):
            adapter.create_sale(
                _sale_command(company, party, ProductRef(product_id))
            )
    finally:
        client.delete("product.product", product_id)
        client.delete("product.template", template_id)


def test_get_sale_missing_returns_none(adapter: TrytonAccountingAdapter):
    assert adapter.get_sale(SaleRef(999_999_999)) is None


# ---------------------------------------------------------------------------
# Invoice finalization + credit notes
# ---------------------------------------------------------------------------


def test_validate_post_and_credit_note(
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    customer,
    accounts,
    created_invoices,
):
    party, _address = customer
    ref = adapter.create_invoice_draft(_invoice_command(company, party, accounts))
    validated = adapter.validate_invoice(ref)
    assert validated.state == "validated"
    posted = adapter.post_invoice(ref)
    assert posted.state == "posted"
    assert posted.number
    assert posted.total_amount == MoneyIRR(750_000)
    assert posted.amount_to_pay == MoneyIRR(750_000)

    credit_ref = adapter.create_credit_note(
        CreateCreditNote(invoice=ref, date=MOVE_DATE, description="refund")
    )
    created_invoices.append(credit_ref.engine_id)
    credit = adapter.get_invoice(credit_ref)
    assert credit is not None
    assert credit.state == "draft"
    assert credit.total_amount == MoneyIRR(-750_000)


def test_create_credit_note_rejects_draft_source(
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    customer,
    accounts,
    created_invoices,
):
    party, _address = customer
    ref = adapter.create_invoice_draft(_invoice_command(company, party, accounts))
    created_invoices.append(ref.engine_id)
    with pytest.raises(InvalidInvoiceError):
        adapter.create_credit_note(
            CreateCreditNote(invoice=ref, date=MOVE_DATE)
        )


def test_invoice_with_alternative_payee_posts_to_gateway_party(
    adapter: TrytonAccountingAdapter,
    client: TrytonRestClient,
    company: CompanyRef,
    customer,
    gateway: PartyRef,
    accounts,
):
    # Spike 4: a single alternative payee moves the posted receivable
    # line's party from the customer to the gateway.
    party, _address = customer
    ref = adapter.create_invoice_draft(_invoice_command(company, party, accounts))
    client.write(
        "account.invoice",
        ref.engine_id,
        {"alternative_payees": [["add", [gateway.engine_id]]]},
    )
    adapter.validate_invoice(ref)
    adapter.post_invoice(ref)
    invoice = client.read("account.invoice", ref.engine_id, fields=["move"])
    assert invoice is not None and invoice["move"], "posted invoice has no move"
    lines = client.search(
        "account.move.line",
        domain=[
            ("move", "=", int(invoice["move"])),
            ("account", "=", accounts[RECEIVABLE_ACCOUNT_CODE].ref.engine_id),
        ],
        fields=["party"],
    )
    assert len(lines) == 1
    assert int(lines[0]["party"]) == gateway.engine_id


# ---------------------------------------------------------------------------
# Payments: workflow + clearing + amount_to_pay
# ---------------------------------------------------------------------------


def test_payment_workflow_with_clearing(
    adapter: TrytonAccountingAdapter,
    client: TrytonRestClient,
    company: CompanyRef,
    customer,
    accounts,
    clearing_payment_journal: PaymentJournalRef,
):
    party, _address = customer
    ref = adapter.create_payment(
        CreatePayment(
            company=company,
            party=party,
            kind="receivable",
            amount=MoneyIRR(500_000),
            date=MOVE_DATE,
            journal=clearing_payment_journal,
            account=accounts[RECEIVABLE_ACCOUNT_CODE].ref,
            reference="US-502 clearing test",
        )
    )
    assert adapter.submit_payment(ref).state == "submitted"
    assert adapter.process_payment(ref).state == "processing"
    succeeded = adapter.succeed_payment(
        SucceedPayment(payment=ref, clearing_date=CLEARING_DATE)
    )
    assert succeeded.state == "succeeded"

    # The clearing move exists and is dated to the clearing date
    # (spike 1: clearing_date arrives via the REST context).
    record = client.read(
        "account.payment", ref.engine_id, fields=["clearing_move"]
    )
    assert record is not None and record["clearing_move"], (
        "succeeded payment has no clearing move"
    )
    move = client.read(
        "account.move", int(record["clearing_move"]), fields=["date", "state"]
    )
    assert move is not None
    assert move["date"] == CLEARING_DATE.gregorian_date
    assert move["state"] == "draft"


def test_payment_workflow_without_clearing_journal(
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    customer,
    accounts,
    payment_journal: PaymentJournalRef,
    created_payments,
):
    party, _address = customer
    ref = adapter.create_payment(
        CreatePayment(
            company=company,
            party=party,
            kind="receivable",
            amount=MoneyIRR(100_000),
            date=MOVE_DATE,
            journal=payment_journal,
            account=accounts[RECEIVABLE_ACCOUNT_CODE].ref,
        )
    )
    created_payments.append(ref.engine_id)
    assert adapter.submit_payment(ref).state == "submitted"
    # fail is only allowed from processing/succeeded, so the payment
    # must be processed first.
    assert adapter.process_payment(ref).state == "processing"
    assert adapter.fail_payment(ref).state == "failed"


def test_draft_payment_reduces_amount_to_pay(
    adapter: TrytonAccountingAdapter,
    client: TrytonRestClient,
    company: CompanyRef,
    customer,
    accounts,
    payment_journal: PaymentJournalRef,
    created_payments,
):
    # Spike 3: a Draft payment already counts against amount_to_pay.
    party, _address = customer
    invoice_ref = _posted_invoice(adapter, company, party, accounts)
    line_id = _receivable_move_line(
        client, invoice_ref.engine_id, accounts[RECEIVABLE_ACCOUNT_CODE].ref.engine_id
    )
    ref = adapter.create_payment(
        CreatePayment(
            company=company,
            party=party,
            kind="receivable",
            amount=MoneyIRR(250_000),
            date=MOVE_DATE,
            journal=payment_journal,
            line=MoveLineRef(line_id),
        )
    )
    created_payments.append(ref.engine_id)
    view = adapter.get_invoice(invoice_ref)
    assert view is not None
    assert view.amount_to_pay == MoneyIRR(500_000)


def test_create_payment_rejects_reconciled_line(
    adapter: TrytonAccountingAdapter,
    client: TrytonRestClient,
    company: CompanyRef,
    customer,
    accounts,
    clearing_payment_journal: PaymentJournalRef,
):
    # Pay an invoice in full, then try to pay the same (now reconciled)
    # line again: the adapter must refuse.
    #
    # NOTE: the CLEARING journal is required here. A base Tryton
    # payment creates no accounting move and reconciles nothing -- the
    # invoice only *looks* paid (amount_to_pay drops to 0). Only the
    # clearing module's succeed reconciles the linked line, and only
    # for a full payment (clearing move leg + invoice line sum to
    # zero). With a manual journal the line stays unreconciled and the
    # second payment is legitimately allowed.
    party, _address = customer
    invoice_ref = _posted_invoice(adapter, company, party, accounts)
    line_id = _receivable_move_line(
        client, invoice_ref.engine_id, accounts[RECEIVABLE_ACCOUNT_CODE].ref.engine_id
    )
    ref = adapter.create_payment(
        CreatePayment(
            company=company,
            party=party,
            kind="receivable",
            amount=MoneyIRR(750_000),
            date=MOVE_DATE,
            journal=clearing_payment_journal,
            line=MoveLineRef(line_id),
        )
    )
    adapter.submit_payment(ref)
    adapter.process_payment(ref)
    adapter.succeed_payment(SucceedPayment(payment=ref))
    with pytest.raises(InvalidPaymentError):
        adapter.create_payment(
            CreatePayment(
                company=company,
                party=party,
                kind="receivable",
                amount=MoneyIRR(1_000),
                date=MOVE_DATE,
                journal=clearing_payment_journal,
                line=MoveLineRef(line_id),
            )
        )
