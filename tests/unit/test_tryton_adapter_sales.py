"""Unit tests for the US-502 port extension (sales, invoice finalization,
credit notes, payment workflow).

No live server is needed: the HTTP layer is exercised with httpx
MockTransport, following the fake-Tryton style of
``test_tryton_adapter.py``.
"""

from __future__ import annotations

import base64
import datetime
import json
from decimal import Decimal

import httpx
import pytest

from hesabyar.domain.accounting.commands import (
    CreateCreditNote,
    CreatePayment,
    CreateSale,
    SaleLine,
    SucceedPayment,
)
from hesabyar.domain.accounting.refs import (
    AccountRef,
    CompanyRef,
    InvoiceRef,
    MoveLineRef,
    PartyRef,
    PaymentJournalRef,
    PaymentRef,
    ProductRef,
    SaleRef,
)
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.errors import (
    InvalidInvoiceError,
    InvalidPaymentError,
    InvalidSaleError,
    InvoiceNotFoundError,
    PaymentNotFoundError,
    ProductNotFoundError,
    SaleNotFoundError,
)
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.infrastructure.accounting.tryton.adapter import TrytonAccountingAdapter
from hesabyar.infrastructure.accounting.tryton.client import TrytonRestClient
from hesabyar.infrastructure.accounting.tryton.mapping import (
    invoice_record_to_view,
    payment_record_to_view,
    sale_line_record_to_view,
    sale_record_to_view,
)

BASE_URL = "http://tryton.test:8000"
DATABASE = "hesabyar_tryton_test"
COMPANY = CompanyRef(2)
PARTY = PartyRef(5)
DATE = FiscalDate.from_gregorian(datetime.date(2026, 11, 16))


def _mock_client(handler) -> TrytonRestClient:
    return TrytonRestClient(
        BASE_URL, DATABASE, "test-key", transport=httpx.MockTransport(handler)
    )


def _sale_line(product_id: int = 9, quantity=2, unit_price=250_000) -> SaleLine:
    return SaleLine(
        product=ProductRef(product_id),
        unit_price=MoneyIRR(unit_price),
        quantity=Decimal(quantity),
        description="Widget",
    )


def _sale_command(**kwargs) -> CreateSale:
    defaults: dict = {
        "company": COMPANY,
        "party": PARTY,
        "date": DATE,
        "lines": (_sale_line(),),
    }
    defaults.update(kwargs)
    return CreateSale(**defaults)


def _payment_command(**kwargs) -> CreatePayment:
    defaults: dict = {
        "company": COMPANY,
        "party": PARTY,
        "kind": "receivable",
        "amount": MoneyIRR(500_000),
        "date": DATE,
        "journal": PaymentJournalRef(3),
    }
    defaults.update(kwargs)
    return CreatePayment(**defaults)


# ---------------------------------------------------------------------------
# Command invariants
# ---------------------------------------------------------------------------


def test_sale_command_totals():
    command = _sale_command(lines=(_sale_line(quantity=2), _sale_line(9, 1, 100_000)))
    assert command.total_amount == MoneyIRR(600_000)


def test_sale_line_rejects_fractional_amount():
    with pytest.raises(InvalidSaleError):
        SaleLine(
            product=ProductRef(9),
            unit_price=MoneyIRR(333),
            quantity=Decimal("0.5"),
        )


def test_sale_line_rejects_zero_quantity():
    with pytest.raises(InvalidSaleError):
        _sale_line(quantity=0)


def test_sale_line_rejects_negative_unit_price():
    with pytest.raises(InvalidSaleError):
        _sale_line(unit_price=-1)


def test_sale_line_rejects_non_finite_quantity():
    with pytest.raises(InvalidSaleError):
        SaleLine(
            product=ProductRef(9),
            unit_price=MoneyIRR(100),
            quantity=Decimal("NaN"),
        )


def test_sale_command_rejects_bad_invoice_method():
    with pytest.raises(InvalidSaleError):
        _sale_command(invoice_method="sometimes")


def test_sale_command_rejects_bad_shipment_method():
    with pytest.raises(InvalidSaleError):
        _sale_command(shipment_method="teleport")


def test_sale_command_rejects_no_lines():
    with pytest.raises(InvalidSaleError):
        _sale_command(lines=())


def test_sale_command_rejects_zero_total():
    with pytest.raises(InvalidSaleError):
        _sale_command(lines=(_sale_line(unit_price=0),))


def test_payment_command_rejects_bad_kind():
    with pytest.raises(InvalidPaymentError):
        _payment_command(kind="sideways")


def test_payment_command_rejects_zero_amount():
    with pytest.raises(InvalidPaymentError):
        _payment_command(amount=MoneyIRR(0))


def test_payment_command_rejects_negative_amount():
    with pytest.raises(InvalidPaymentError):
        _payment_command(amount=MoneyIRR(-5))


# ---------------------------------------------------------------------------
# Mapping: sale views, invoice amount_to_pay, payment move line
# ---------------------------------------------------------------------------


def _sale_record(**overrides) -> dict:
    record = {
        "id": 42,
        "company": 2,
        "party": 5,
        "number": "SO-001",
        "sale_date": datetime.date(2026, 11, 16),
        "state": "confirmed",
        "invoice_method": "order",
        "shipment_method": "manual",
        "invoice_state": "paid",
        "shipment_state": "none",
        "currency": 1,
        "untaxed_amount": Decimal("500000"),
        "total_amount": Decimal("500000"),
        "description": "store order #1",
        "reference": "ORD-1",
        "invoices": [77],
    }
    record.update(overrides)
    return record


def test_sale_record_to_view():
    line = sale_line_record_to_view(
        {
            "id": 1,
            "sale": 42,
            "type": "line",
            "product": 9,
            "quantity": 2.0,
            "unit_price": Decimal("250000"),
            "amount": Decimal("500000"),
            "description": "Widget",
        },
        {9: "Widget"},
    )
    assert line.product == ProductRef(9)
    assert line.product_name == "Widget"
    assert line.quantity == Decimal("2")
    assert line.amount == MoneyIRR(500_000)
    view = sale_record_to_view(_sale_record(), [line], "Acme Customer", "IRR")
    assert view.ref == SaleRef(42)
    assert view.number == "SO-001"
    assert view.state == "confirmed"
    assert view.invoices == (InvoiceRef(77),)
    assert view.lines == (line,)
    assert view.total_amount == MoneyIRR(500_000)


def test_sale_record_to_view_without_number_or_invoices():
    view = sale_record_to_view(
        _sale_record(number=None, sale_date=None, invoices=[], state="draft"),
        [],
        "Acme Customer",
        "IRR",
    )
    assert view.number is None
    assert view.sale_date is None
    assert view.invoices == ()
    assert view.is_draft


def test_invoice_record_to_view_carries_amount_to_pay():
    view = invoice_record_to_view(
        {
            "id": 42,
            "company": 2,
            "type": "out",
            "party": 5,
            "invoice_address": 7,
            "currency": 1,
            "account": 311,
            "journal": 1,
            "invoice_date": datetime.date(2026, 11, 16),
            "accounting_date": None,
            "state": "posted",
            "number": "INV-1",
            "description": "",
            "reference": "",
            "untaxed_amount": Decimal("750000"),
            "tax_amount": Decimal("0"),
            "total_amount": Decimal("750000"),
            "amount_to_pay": Decimal("250000"),
        },
        [],
        "Acme Customer",
        "IRR",
    )
    assert view.amount_to_pay == MoneyIRR(250_000)


def test_payment_record_to_view_carries_move_line():
    view = payment_record_to_view(
        {
            "id": 9,
            "company": 2,
            "number": "3",
            "kind": "receivable",
            "party": 5,
            "date": datetime.date(2026, 11, 16),
            "amount": Decimal("500000"),
            "state": "draft",
            "reference": "",
            "journal": 3,
            "currency": 1,
            "line": 55,
        },
        "Acme Customer",
        "Manual Payments",
        "IRR",
    )
    assert view.move_line == MoveLineRef(55)


# ---------------------------------------------------------------------------
# Adapter logic against a fake Tryton (MockTransport)
# ---------------------------------------------------------------------------

_FAKE_SALE_PRODUCTS = {
    9: {"id": 9, "template": 3},
    10: {"id": 10, "template": 4},
}
_FAKE_SALE_TEMPLATES = {
    3: {"id": 3, "salable": True, "sale_uom": 1, "default_uom": 1},
    4: {"id": 4, "salable": False, "sale_uom": 1, "default_uom": 1},
}
_FAKE_PAYMENT_JOURNALS = {
    3: {"id": 3, "company": 2, "name": "Manual Payments"},
}
_FAKE_MOVE_LINES = {
    55: {"id": 55, "move": 21, "reconciliation": None},
    56: {"id": 56, "move": 21, "reconciliation": 8},
}
_FAKE_PAYMENT_ACCOUNTS = {
    311: {"id": 311, "closed": False, "active": True, "type": 113},
    339: {"id": 339, "closed": False, "active": True, "type": 102},
}
_FAKE_ACCOUNT_TYPES = {
    113: {"id": 113, "receivable": True, "payable": False},
    102: {"id": 102, "receivable": False, "payable": True},
}


def _request_domain(request: httpx.Request) -> list:
    raw = request.url.params.get("d")
    if not raw:
        return []
    decoded = json.loads(base64.urlsafe_b64decode(raw))
    return decoded if isinstance(decoded, list) else []


def _domain_matches(record: dict, domain: list) -> bool:
    """Tiny subset of Tryton domain matching, enough for the fake below."""
    for clause in domain:
        if not isinstance(clause, list) or len(clause) != 3:
            continue
        field_name, operator, operand = clause
        if field_name == "company":
            continue  # every fake record belongs to company 2
        value = record.get(field_name)
        if operator == "=" and value != operand:
            return False
        if operator == "!=" and value == operand:
            return False
        if operator == "in" and value not in operand:
            return False
    return True


def _fake_sale_record(state: str = "draft", number=None, invoices=None) -> dict:
    return {
        "id": 42,
        "company": 2,
        "party": 5,
        "number": number,
        "sale_date": {"__class__": "date", "year": 2026, "month": 11, "day": 16},
        "state": state,
        "invoice_method": "order",
        "shipment_method": "manual",
        "invoice_state": "none",
        "shipment_state": "none",
        "currency": 1,
        "untaxed_amount": {"__class__": "Decimal", "decimal": "500000"},
        "total_amount": {"__class__": "Decimal", "decimal": "500000"},
        "description": "store order",
        "reference": "ORD-1",
        "invoices": invoices or [],
    }


def _fake_payment_record(state: str = "draft") -> dict:
    return {
        "id": 9,
        "company": 2,
        "number": "3",
        "kind": "receivable",
        "party": 5,
        "date": {"__class__": "date", "year": 2026, "month": 11, "day": 16},
        "amount": {"__class__": "Decimal", "decimal": "500000"},
        "state": state,
        "reference": "tranche-1",
        "journal": 3,
        "currency": 1,
        "line": 55,
    }


def _fake_invoice_record(state: str = "draft", number=None) -> dict:
    return {
        "id": 42,
        "company": 2,
        "type": "out",
        "party": 5,
        "invoice_address": 7,
        "currency": 1,
        "account": 311,
        "journal": 1,
        "invoice_date": {"__class__": "date", "year": 2026, "month": 11, "day": 16},
        "accounting_date": None,
        "state": state,
        "number": number,
        "description": "test invoice",
        "reference": "REF-1",
        "untaxed_amount": {"__class__": "Decimal", "decimal": "750000"},
        "tax_amount": {"__class__": "Decimal", "decimal": "0"},
        "total_amount": {"__class__": "Decimal", "decimal": "750000"},
        "amount_to_pay": {"__class__": "Decimal", "decimal": "750000"},
    }


def _fake_us502_handler(seen: dict, invoice_state: str = "posted"):
    """Fake Tryton for the US-502 adapter tests.

    ``seen`` records request bodies/headers the tests assert on.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/rest/model/company.company/2"):
            return httpx.Response(200, json={"id": 2, "currency": 1})
        if path.endswith("/rest/model/currency.currency"):
            return httpx.Response(200, json=[{"id": 1, "code": "IRR"}])
        if path.endswith("/rest/model/party.party/5"):
            return httpx.Response(200, json={"id": 5, "name": "Acme Customer"})
        if path.endswith("/rest/model/party.party"):
            return httpx.Response(200, json=[{"id": 5, "name": "Acme Customer"}])
        if path.endswith("/rest/model/party.address"):
            return httpx.Response(200, json=[{"id": 7, "party": 5}])
        if path.endswith("/rest/model/product.product"):
            records = [
                r
                for r in _FAKE_SALE_PRODUCTS.values()
                if _domain_matches(r, _request_domain(request))
            ]
            if "rec_name" in request.url.params.get_list("f"):
                records = [
                    {**r, "rec_name": "Widget" if r["id"] == 9 else "Gadget"}
                    for r in records
                ]
            return httpx.Response(200, json=records)
        if path.endswith("/rest/model/product.template"):
            records = [
                r
                for r in _FAKE_SALE_TEMPLATES.values()
                if _domain_matches(r, _request_domain(request))
            ]
            return httpx.Response(200, json=records)
        if path.endswith("/rest/model/account.payment.journal"):
            records = [
                r
                for r in _FAKE_PAYMENT_JOURNALS.values()
                if _domain_matches(r, _request_domain(request))
            ]
            return httpx.Response(200, json=records)
        if path.endswith("/rest/model/account.move.line"):
            records = [
                r
                for r in _FAKE_MOVE_LINES.values()
                if _domain_matches(r, _request_domain(request))
            ]
            return httpx.Response(200, json=records)
        if path.endswith("/rest/model/account.move/21"):
            return httpx.Response(200, json={"id": 21, "company": 2})
        if path.endswith("/rest/model/account.account"):
            records = [
                r
                for r in _FAKE_PAYMENT_ACCOUNTS.values()
                if _domain_matches(r, _request_domain(request))
            ]
            return httpx.Response(200, json=records)
        if "/rest/model/account.account.type/" in path:
            type_id = int(path.rsplit("/", 1)[-1])
            type_record = _FAKE_ACCOUNT_TYPES.get(type_id)
            if type_record is None:
                return httpx.Response(404, text="not found")
            return httpx.Response(200, json=type_record)
        if path.endswith("/rest/model/sale.sale") and request.method == "POST":
            seen["sale_create"] = json.loads(request.content)
            return httpx.Response(200, json={"id": 42, "state": "draft"})
        if path.endswith("/rest/model/sale.sale/42/quote"):
            return httpx.Response(200, json=_fake_sale_record("quotation", "SO-001"))
        if path.endswith("/rest/model/sale.sale/42/confirm"):
            return httpx.Response(200, json=_fake_sale_record("confirmed", "SO-001"))
        if path.endswith("/rest/model/sale.sale/42/process"):
            return httpx.Response(
                200, json=_fake_sale_record("processing", "SO-001", invoices=[77])
            )
        if path.endswith("/rest/model/sale.sale/42/cancel"):
            return httpx.Response(200, json=_fake_sale_record("cancelled", "SO-001"))
        if "/rest/model/sale.sale/" in path and path.split("/")[-1] in (
            "quote",
            "confirm",
            "process",
            "cancel",
        ):
            return httpx.Response(404, text="not found")
        if path.endswith("/rest/model/sale.sale/42"):
            return httpx.Response(200, json=_fake_sale_record("draft"))
        if "/rest/model/sale.sale/" in path:
            return httpx.Response(404, text="not found")
        if path.endswith("/rest/model/sale.sale"):
            return httpx.Response(200, json=[_fake_sale_record("draft")])
        if path.endswith("/rest/model/sale.line"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 1,
                        "sale": 42,
                        "type": "line",
                        "product": 9,
                        "quantity": 2.0,
                        "unit_price": {"__class__": "Decimal", "decimal": "250000"},
                        "amount": {"__class__": "Decimal", "decimal": "500000"},
                        "description": "Widget",
                    }
                ],
            )
        if path.endswith("/rest/model/account.payment") and request.method == "POST":
            seen["payment_create"] = json.loads(request.content)
            return httpx.Response(200, json={"id": 9, "state": "draft"})
        if path.endswith("/rest/model/account.payment/9/submit"):
            return httpx.Response(200, json=_fake_payment_record("submitted"))
        if path.endswith("/rest/model/account.payment/9/approve"):
            return httpx.Response(200, json=_fake_payment_record("approved"))
        if path.endswith("/rest/model/account.payment/9/process_rest"):
            return httpx.Response(200, json=_fake_payment_record("processing"))
        if path.endswith("/rest/model/account.payment/9/succeed"):
            seen["succeed_context"] = request.headers.get("X-Tryton-Context")
            return httpx.Response(200, json=_fake_payment_record("succeeded"))
        if path.endswith("/rest/model/account.payment/9/fail"):
            return httpx.Response(200, json=_fake_payment_record("failed"))
        if "/rest/model/account.payment/" in path and path.split("/")[-1] in (
            "submit",
            "approve",
            "process_rest",
            "succeed",
            "fail",
        ):
            return httpx.Response(404, text="not found")
        if path.endswith("/rest/model/account.payment/9"):
            return httpx.Response(200, json=_fake_payment_record("draft"))
        if "/rest/model/account.payment/" in path:
            return httpx.Response(404, text="not found")
        if path.endswith("/rest/model/account.invoice") and request.method == "POST":
            seen["invoice_create"] = json.loads(request.content)
            return httpx.Response(200, json={"id": 43, "state": "draft"})
        if path.endswith("/rest/model/account.invoice/42/validate_invoice"):
            return httpx.Response(200, json=_fake_invoice_record("validated"))
        if path.endswith("/rest/model/account.invoice/42/post"):
            return httpx.Response(200, json=_fake_invoice_record("posted", "INV-1"))
        if "/rest/model/account.invoice/" in path and path.split("/")[-1] in (
            "validate_invoice",
            "post",
        ):
            return httpx.Response(404, text="not found")
        if path.endswith("/rest/model/account.invoice/42"):
            return httpx.Response(200, json=_fake_invoice_record(invoice_state, "INV-1"))
        if "/rest/model/account.invoice/" in path:
            return httpx.Response(404, text="not found")
        if path.endswith("/rest/model/account.invoice.line"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 1,
                        "invoice": 42,
                        "account": 371,
                        "quantity": 1.0,
                        "unit_price": {"__class__": "Decimal", "decimal": "500000"},
                        "amount": {"__class__": "Decimal", "decimal": "500000"},
                        "type": "line",
                        "description": "Goods",
                    },
                    {
                        "id": 2,
                        "invoice": 42,
                        "account": 372,
                        "quantity": 2.0,
                        "unit_price": {"__class__": "Decimal", "decimal": "125000"},
                        "amount": {"__class__": "Decimal", "decimal": "250000"},
                        "type": "line",
                        "description": "Services",
                    },
                ],
            )
        raise AssertionError(f"unexpected request: {request.method} {path}")

    return handler


def _adapter(seen: dict | None = None, **handler_kwargs) -> TrytonAccountingAdapter:
    seen = seen if seen is not None else {}
    return TrytonAccountingAdapter(
        _mock_client(_fake_us502_handler(seen, **handler_kwargs))
    )


# -- sales ------------------------------------------------------------------


def test_adapter_create_sale_happy_path():
    seen: dict = {}
    adapter = _adapter(seen)
    ref = adapter.create_sale(_sale_command())
    assert ref == SaleRef(42)
    body = seen["sale_create"]
    assert body["company"] == 2
    assert body["party"] == 5
    assert body["currency"] == 1
    assert body["invoice_method"] == "order"
    assert body["shipment_method"] == "manual"
    # No carrier yet: shipment_cost_method must be None, or the sale
    # fails its domain check when leaving draft (sale_shipment_cost).
    assert body["shipment_cost_method"] is None
    assert body["invoice_address"] == 7
    assert body["shipment_address"] == 7
    lines = body["lines"][0][1]
    assert lines[0]["product"] == 9
    assert lines[0]["unit"] == 1  # resolved from the product's sale UoM
    assert lines[0]["quantity"] == 2
    assert lines[0]["type"] == "line"


def test_adapter_create_sale_rejects_unknown_product():
    adapter = _adapter()
    with pytest.raises(ProductNotFoundError):
        adapter.create_sale(_sale_command(lines=(_sale_line(999),)))


def test_adapter_create_sale_rejects_non_sellable_product():
    adapter = _adapter()
    with pytest.raises(ProductNotFoundError):
        adapter.create_sale(_sale_command(lines=(_sale_line(10),)))


def test_adapter_quote_confirm_process_cancel_sale():
    adapter = _adapter()
    quoted = adapter.quote_sale(SaleRef(42))
    assert quoted.state == "quotation"
    assert quoted.number == "SO-001"
    confirmed = adapter.confirm_sale(SaleRef(42))
    assert confirmed.state == "confirmed"
    processed = adapter.process_sale(SaleRef(42))
    assert processed.state == "processing"
    assert processed.invoices == (InvoiceRef(77),)
    cancelled = adapter.cancel_sale(SaleRef(42))
    assert cancelled.state == "cancelled"


def test_adapter_sale_action_missing_raises():
    adapter = _adapter()
    with pytest.raises(SaleNotFoundError):
        adapter.quote_sale(SaleRef(999))


def test_adapter_get_sale_happy_path_and_missing():
    adapter = _adapter()
    view = adapter.get_sale(SaleRef(42))
    assert view is not None
    assert view.state == "draft"
    assert len(view.lines) == 1
    assert view.lines[0].product_name == "Widget"
    assert adapter.get_sale(SaleRef(999)) is None


def test_adapter_list_sales_rejects_bad_filter():
    adapter = _adapter()
    with pytest.raises(InvalidSaleError):
        adapter.list_sales(COMPANY, state="shipped")


def test_adapter_list_sales_returns_views():
    adapter = _adapter()
    views = adapter.list_sales(COMPANY)
    assert len(views) == 1
    assert views[0].ref == SaleRef(42)


# -- payments ---------------------------------------------------------------


def test_adapter_create_payment_happy_path_with_line():
    seen: dict = {}
    adapter = _adapter(seen)
    ref = adapter.create_payment(_payment_command(line=MoveLineRef(55)))
    assert ref == PaymentRef(9)
    body = seen["payment_create"]
    assert body["company"] == 2
    assert body["party"] == 5
    assert body["journal"] == 3
    assert body["kind"] == "receivable"
    assert body["line"] == 55
    assert "account" not in body  # line and account are mutually exclusive


def test_adapter_create_payment_resolves_single_journal():
    seen: dict = {}
    adapter = _adapter(seen)
    ref = adapter.create_payment(_payment_command(journal=None, account=AccountRef(311)))
    assert ref == PaymentRef(9)
    assert seen["payment_create"]["journal"] == 3
    assert seen["payment_create"]["account"] == 311


def test_adapter_create_payment_rejects_reconciled_line():
    adapter = _adapter()
    with pytest.raises(InvalidPaymentError):
        adapter.create_payment(_payment_command(line=MoveLineRef(56)))


def test_adapter_create_payment_rejects_wrong_type_account():
    adapter = _adapter()
    # 339 is payable; a receivable payment needs a receivable account.
    with pytest.raises(InvalidPaymentError):
        adapter.create_payment(_payment_command(account=AccountRef(339)))


def test_adapter_payment_workflow_transitions():
    adapter = _adapter()
    assert adapter.submit_payment(PaymentRef(9)).state == "submitted"
    assert adapter.approve_payment(PaymentRef(9)).state == "approved"
    assert adapter.process_payment(PaymentRef(9)).state == "processing"
    assert adapter.fail_payment(PaymentRef(9)).state == "failed"


def test_adapter_succeed_payment_sends_clearing_date_in_context():
    seen: dict = {}
    adapter = _adapter(seen)
    view = adapter.succeed_payment(
        SucceedPayment(
            payment=PaymentRef(9),
            clearing_date=FiscalDate.from_gregorian(datetime.date(2026, 11, 20)),
        )
    )
    assert view.state == "succeeded"
    header = seen["succeed_context"]
    assert header is not None
    decoded = json.loads(base64.urlsafe_b64decode(header))
    assert decoded["clearing_date"] == {
        "__class__": "date",
        "year": 2026,
        "month": 11,
        "day": 20,
    }


def test_adapter_succeed_payment_without_clearing_date_sends_no_context():
    seen: dict = {}
    adapter = _adapter(seen)
    view = adapter.succeed_payment(SucceedPayment(payment=PaymentRef(9)))
    assert view.state == "succeeded"
    assert seen.get("succeed_context") is None


def test_adapter_payment_action_missing_raises():
    adapter = _adapter()
    with pytest.raises(PaymentNotFoundError):
        adapter.submit_payment(PaymentRef(999))


# -- invoice finalization + credit notes ------------------------------------


def test_adapter_validate_and_post_invoice():
    adapter = _adapter()
    validated = adapter.validate_invoice(InvoiceRef(42))
    assert validated.state == "validated"
    posted = adapter.post_invoice(InvoiceRef(42))
    assert posted.state == "posted"
    assert posted.number == "INV-1"
    assert posted.amount_to_pay == MoneyIRR(750_000)


def test_adapter_invoice_action_missing_raises():
    adapter = _adapter()
    with pytest.raises(InvoiceNotFoundError):
        adapter.validate_invoice(InvoiceRef(999))


def test_adapter_create_credit_note_negates_quantities_and_links_origin():
    seen: dict = {}
    adapter = _adapter(seen, invoice_state="posted")
    ref = adapter.create_credit_note(
        CreateCreditNote(invoice=InvoiceRef(42), date=DATE, description="refund")
    )
    assert ref == InvoiceRef(43)
    body = seen["invoice_create"]
    assert body["type"] == "out"
    assert body["party"] == 5
    assert body["account"] == 311
    assert body["journal"] == 1
    assert body["description"] == "refund"
    lines = body["lines"][0][1]
    assert len(lines) == 2
    assert lines[0]["quantity"] == -1
    assert lines[0]["origin"] == "account.invoice.line,1"
    assert lines[1]["quantity"] == -2
    assert lines[1]["origin"] == "account.invoice.line,2"


def test_adapter_create_credit_note_rejects_draft_source():
    adapter = _adapter(invoice_state="draft")
    with pytest.raises(InvalidInvoiceError):
        adapter.create_credit_note(CreateCreditNote(invoice=InvoiceRef(42), date=DATE))


def test_adapter_create_credit_note_rejects_missing_source():
    adapter = _adapter()
    with pytest.raises(InvoiceNotFoundError):
        adapter.create_credit_note(CreateCreditNote(invoice=InvoiceRef(999), date=DATE))
