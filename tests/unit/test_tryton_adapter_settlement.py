"""Unit tests for the US-522 adapter extension (settlement accounting).

No live server is needed: the HTTP layer is exercised with httpx
MockTransport, following the fake-Tryton style of
``test_tryton_adapter_sales.py``. Live behaviour (the SnappPay
worked example end-to-end) is covered by
``tests/contract/test_settlement_posting.py``.
"""

from __future__ import annotations

import base64
import datetime
import json
from decimal import Decimal

import httpx
import pytest

from hesabyar.domain.accounting.commands import (
    CreateInvoiceDraft,
    CreateJournalDraft,
    InvoiceDraftLine,
    JournalDraftLine,
    ReconcileMoveLines,
)
from hesabyar.domain.accounting.refs import (
    AccountRef,
    CompanyRef,
    JournalDraftRef,
    JournalRef,
    MoveLineRef,
    PartyRef,
    PaymentJournalRef,
    PaymentRef,
    PaymentTermRef,
)
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.errors import (
    InvalidInvoiceError,
    InvalidJournalError,
    PaymentNotFoundError,
)
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.infrastructure.accounting.tryton.adapter import TrytonAccountingAdapter
from hesabyar.infrastructure.accounting.tryton.client import TrytonRestClient
from hesabyar.infrastructure.accounting.tryton.mapping import (
    invoice_record_to_view,
    move_line_record_to_view,
    payment_journal_record_to_view,
    payment_record_to_view,
)

BASE_URL = "http://tryton.test:8000"
DATABASE = "hesabyar_tryton_test"
COMPANY = CompanyRef(2)
GATEWAY = PartyRef(10)
CUSTOMER = PartyRef(5)
DATE = FiscalDate.from_gregorian(datetime.date(2026, 11, 19))
DUE = FiscalDate.from_gregorian(datetime.date(2026, 11, 26))

RECEIVABLE = AccountRef(100)
CLEARING = AccountRef(101)
BANK = AccountRef(102)
FEE_EXPENSE = AccountRef(103)
FEE_VAT = AccountRef(104)
REVENUE = AccountRef(105)


def _mock_client(handler) -> TrytonRestClient:
    return TrytonRestClient(BASE_URL, DATABASE, "test-key", transport=httpx.MockTransport(handler))


def _date_wire(day: datetime.date) -> dict:
    return {"__class__": "date", "year": day.year, "month": day.month, "day": day.day}


def _decimal_wire(value: int) -> dict:
    return {"__class__": "Decimal", "decimal": str(value)}


# ---------------------------------------------------------------------------
# Command invariants
# ---------------------------------------------------------------------------


def test_reconcile_command_rejects_too_few_lines():
    with pytest.raises(InvalidJournalError, match="at least two"):
        ReconcileMoveLines(company=COMPANY, lines=(MoveLineRef(1),))


def test_reconcile_command_rejects_duplicate_lines():
    with pytest.raises(InvalidJournalError, match="distinct"):
        ReconcileMoveLines(company=COMPANY, lines=(MoveLineRef(1), MoveLineRef(1)))


def test_journal_line_carries_party_and_maturity():
    line = JournalDraftLine(
        account=RECEIVABLE,
        credit=MoneyIRR(2_750_000),
        party=GATEWAY,
        maturity_date=DUE,
    )
    assert line.party == GATEWAY
    assert line.maturity_date == DUE


def test_invoice_command_rejects_two_alternative_payees():
    with pytest.raises(InvalidInvoiceError, match="at most one"):
        CreateInvoiceDraft(
            company=COMPANY,
            invoice_type="out",
            party=CUSTOMER,
            date=DATE,
            lines=(InvoiceDraftLine(account=REVENUE, unit_price=MoneyIRR(100_000_000)),),
            alternative_payees=(GATEWAY, PartyRef(11)),
        )


# ---------------------------------------------------------------------------
# Mapping: move lines, payment journals, clearing_move / move on views
# ---------------------------------------------------------------------------


def _move_line_record(**overrides) -> dict:
    record = {
        "id": 55,
        "move": 21,
        "account": 100,
        "debit": Decimal("25000000"),
        "credit": Decimal("0"),
        "state": "valid",
        "party": 10,
        "maturity_date": datetime.date(2026, 11, 26),
        "reconciliation": None,
        "description": "invoice line",
    }
    record.update(overrides)
    return record


def test_move_line_record_to_view():
    view = move_line_record_to_view(_move_line_record())
    assert view.ref == MoveLineRef(55)
    assert view.move == JournalDraftRef(21)
    assert view.account == RECEIVABLE
    assert view.debit == MoneyIRR(25_000_000)
    assert view.credit == MoneyIRR(0)
    assert view.balance == MoneyIRR(25_000_000)
    assert view.state == "valid"
    assert view.party == GATEWAY
    assert view.maturity_date == DUE
    assert view.reconciliation is None
    assert view.description == "invoice line"


def test_move_line_record_to_view_without_party_or_maturity():
    view = move_line_record_to_view(
        _move_line_record(
            id=56,
            account=101,
            debit=Decimal("0"),
            credit=Decimal("22250000"),
            party=None,
            maturity_date=None,
            reconciliation=8,
        )
    )
    assert view.party is None
    assert view.maturity_date is None
    assert view.reconciliation is not None
    assert view.reconciliation.engine_id == 8
    assert view.balance == MoneyIRR(-22_250_000)


def test_payment_journal_record_to_view_with_clearing():
    view = payment_journal_record_to_view(
        {
            "id": 20,
            "company": 2,
            "name": "SnappPay",
            "process_method": "manual",
            "clearing_account": 101,
            "clearing_journal": 9,
        }
    )
    assert view.ref == PaymentJournalRef(20)
    assert view.clearing_account == CLEARING
    assert view.clearing_journal == JournalRef(9)
    assert view.is_clearing_configured


def test_payment_journal_record_to_view_without_clearing():
    view = payment_journal_record_to_view(
        {
            "id": 21,
            "company": 2,
            "name": "Plain",
            "process_method": "manual",
            "clearing_account": None,
            "clearing_journal": None,
        }
    )
    assert view.clearing_account is None
    assert view.clearing_journal is None
    assert not view.is_clearing_configured


def test_payment_record_to_view_carries_clearing_move():
    view = payment_record_to_view(
        {
            "id": 9,
            "company": 2,
            "number": "3",
            "kind": "receivable",
            "party": 10,
            "date": datetime.date(2026, 11, 26),
            "amount": Decimal("22250000"),
            "state": "succeeded",
            "reference": "settlement:t-1",
            "journal": 20,
            "currency": 1,
            "line": 55,
            "clearing_move": 77,
        },
        "SnappPay",
        "SnappPay",
        "IRR",
    )
    assert view.clearing_move == JournalDraftRef(77)
    assert view.move_line == MoveLineRef(55)


def test_invoice_record_to_view_carries_move():
    view = invoice_record_to_view(
        {
            "id": 42,
            "company": 2,
            "type": "out",
            "party": 5,
            "invoice_address": 7,
            "currency": 1,
            "account": 100,
            "journal": 1,
            "invoice_date": datetime.date(2026, 11, 19),
            "accounting_date": None,
            "state": "posted",
            "number": "INV-1",
            "description": "",
            "reference": "",
            "untaxed_amount": Decimal("100000000"),
            "tax_amount": Decimal("0"),
            "total_amount": Decimal("100000000"),
            "amount_to_pay": Decimal("100000000"),
            "move": 88,
        },
        [],
        "Acme Customer",
        "IRR",
    )
    assert view.move is not None
    assert view.move.engine_id == 88


# ---------------------------------------------------------------------------
# Adapter logic against a fake Tryton (MockTransport)
# ---------------------------------------------------------------------------

_FAKE_ACCOUNTS = {
    100: {
        "id": 100,
        "closed": False,
        "active": True,
        "party_required": True,
        "type": 1,
    },
    101: {"id": 101, "closed": False, "active": True, "party_required": False},
    102: {"id": 102, "closed": False, "active": True, "party_required": False},
    103: {"id": 103, "closed": False, "active": True, "party_required": False},
    104: {"id": 104, "closed": False, "active": True, "party_required": False},
    105: {
        "id": 105,
        "closed": False,
        "active": True,
        "party_required": False,
        "type": 2,
    },
}

_FAKE_ACCOUNT_TYPES = {
    1: {"id": 1, "receivable": True, "payable": False, "revenue": False},
    2: {"id": 2, "receivable": False, "payable": False, "revenue": True},
}

_FAKE_PARTIES = {
    5: {"id": 5, "name": "Acme Customer"},
    10: {"id": 10, "name": "SnappPay"},
}

_FAKE_MOVE_LINES = {
    # Tranche 1: invoice gross 25M Dr, fee 2.75M Cr, clearing 22.25M Cr.
    55: {
        "id": 55,
        "move": 21,
        "account": 100,
        "debit": _decimal_wire(25_000_000),
        "credit": _decimal_wire(0),
        "state": "valid",
        "party": 10,
        "maturity_date": _date_wire(datetime.date(2026, 11, 26)),
        "reconciliation": None,
        "description": "invoice line",
    },
    56: {
        "id": 56,
        "move": 22,
        "account": 100,
        "debit": _decimal_wire(0),
        "credit": _decimal_wire(2_750_000),
        "state": "valid",
        "party": 10,
        "maturity_date": _date_wire(datetime.date(2026, 11, 26)),
        "reconciliation": None,
        "description": "fee leg",
    },
    57: {
        "id": 57,
        "move": 23,
        "account": 100,
        "debit": _decimal_wire(0),
        "credit": _decimal_wire(22_250_000),
        "state": "valid",
        "party": 10,
        "maturity_date": None,
        "reconciliation": None,
        "description": "clearing leg",
    },
    # A line on another account (for the same-account check).
    58: {
        "id": 58,
        "move": 23,
        "account": 101,
        "debit": _decimal_wire(22_250_000),
        "credit": _decimal_wire(0),
        "state": "valid",
        "party": None,
        "maturity_date": None,
        "reconciliation": None,
        "description": "clearing Dr",
    },
    # An already-reconciled line.
    59: {
        "id": 59,
        "move": 24,
        "account": 100,
        "debit": _decimal_wire(1_000_000),
        "credit": _decimal_wire(0),
        "state": "valid",
        "party": 10,
        "maturity_date": _date_wire(datetime.date(2026, 11, 26)),
        "reconciliation": 8,
        "description": "reconciled",
    },
    # A draft-state line (unbalanced move).
    60: {
        "id": 60,
        "move": 25,
        "account": 100,
        "debit": _decimal_wire(1_000_000),
        "credit": _decimal_wire(0),
        "state": "draft",
        "party": 10,
        "maturity_date": _date_wire(datetime.date(2026, 11, 26)),
        "reconciliation": None,
        "description": "draft line",
    },
    # Same account as 55 but a different party (the customer, not the
    # gateway) — for the same-party check.
    61: {
        "id": 61,
        "move": 26,
        "account": 100,
        "debit": _decimal_wire(1_000_000),
        "credit": _decimal_wire(0),
        "state": "valid",
        "party": 5,
        "maturity_date": _date_wire(datetime.date(2026, 11, 26)),
        "reconciliation": None,
        "description": "customer line",
    },
}

_FAKE_PAYMENTS = {
    9: {
        "id": 9,
        "company": 2,
        "number": "3",
        "kind": "receivable",
        "party": 10,
        "date": _date_wire(datetime.date(2026, 11, 26)),
        "amount": _decimal_wire(22_250_000),
        "state": "processing",
        "reference": "settlement:t-1",
        "journal": 20,
        "currency": 1,
        "line": 55,
        "clearing_move": None,
    },
    10: {
        "id": 10,
        "company": 2,
        "number": "4",
        "kind": "receivable",
        "party": 10,
        "date": _date_wire(datetime.date(2026, 11, 26)),
        "amount": _decimal_wire(22_250_000),
        "state": "processing",
        "reference": "settlement:t-2",
        "journal": 20,
        "currency": 1,
        "line": 65,
        "clearing_move": None,
    },
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
        if field_name in ("company", "move.company", "fiscalyear.company"):
            continue  # every fake record belongs to company 2
        if field_name == "move":
            value = record.get("move")
        else:
            value = record.get(field_name)
        if operator == "=" and value != operand:
            return False
        if operator == "!=" and value == operand:
            return False
        if operator == "in" and value not in operand:
            return False
    return True


def _fake_us522_handler(seen: dict):
    """Fake Tryton for the US-522 adapter tests.

    ``seen`` records request bodies the tests assert on.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/rest/model/company.company/2"):
            return httpx.Response(200, json={"id": 2, "currency": 1})
        if path.endswith("/rest/model/currency.currency"):
            return httpx.Response(200, json=[{"id": 1, "code": "IRR"}])
        if path.endswith("/rest/model/party.party/5"):
            return httpx.Response(200, json=_FAKE_PARTIES[5])
        if path.endswith("/rest/model/party.party/10"):
            return httpx.Response(200, json=_FAKE_PARTIES[10])
        if path.endswith("/rest/model/party.party"):
            records = [r for r in _FAKE_PARTIES.values() if _domain_matches(r, _request_domain(request))]
            return httpx.Response(200, json=records)
        if path.endswith("/rest/model/party.address"):
            return httpx.Response(200, json=[{"id": 7, "party": 5}])
        if path.endswith("/rest/model/account.invoice.payment_term/7"):
            return httpx.Response(200, json={"id": 7, "name": "4 tranches"})
        if path.endswith("/rest/model/account.account"):
            records = [r for r in _FAKE_ACCOUNTS.values() if _domain_matches(r, _request_domain(request))]
            return httpx.Response(200, json=records)
        if "/rest/model/account.account.type/" in path:
            type_id = int(path.rsplit("/", 1)[-1])
            type_record = _FAKE_ACCOUNT_TYPES.get(type_id)
            if type_record is None:
                return httpx.Response(404, text="not found")
            return httpx.Response(200, json=type_record)
        if path.endswith("/rest/model/account.period"):
            return httpx.Response(200, json=[{"id": 11, "name": "2026-11"}])
        if path.endswith("/rest/model/account.journal/9"):
            return httpx.Response(200, json={"id": 9, "type": "general"})
        if path.endswith("/rest/model/account.journal/1"):
            return httpx.Response(200, json={"id": 1, "type": "revenue"})
        if path.endswith("/rest/model/account.move") and request.method == "POST":
            seen["move_create"] = json.loads(request.content)
            return httpx.Response(200, json={"id": 500, "state": "draft"})
        if path.endswith("/rest/model/account.invoice") and request.method == "POST":
            seen["invoice_create"] = json.loads(request.content)
            return httpx.Response(200, json={"id": 600, "state": "draft"})
        if path.endswith("/rest/model/account.payment.journal/20"):
            return httpx.Response(
                200,
                json={
                    "id": 20,
                    "company": 2,
                    "name": "SnappPay",
                    "process_method": "manual",
                    "clearing_account": 101,
                    "clearing_journal": 9,
                },
            )
        if path.endswith("/rest/model/account.payment.journal/21"):
            return httpx.Response(
                200,
                json={
                    "id": 21,
                    "company": 2,
                    "name": "Plain",
                    "process_method": "manual",
                    "clearing_account": None,
                    "clearing_journal": None,
                },
            )
        if path.endswith("/rest/model/account.payment.journal"):
            records = [
                {"id": 20, "name": "SnappPay"},
                {"id": 21, "name": "Plain"},
            ]
            return httpx.Response(
                200,
                json=[r for r in records if _domain_matches(r, _request_domain(request))],
            )
        if path.endswith("/rest/model/account.payment/process_rest_batch"):
            seen["process_batch"] = json.loads(request.content)
            return httpx.Response(200, json={})
        if path.endswith("/rest/model/account.move.line/reconcile_rest"):
            seen["reconcile"] = json.loads(request.content)
            return httpx.Response(200, json={"reconciliation_id": 900, "number": "R-1"})
        if path.endswith("/rest/model/account.move.line"):
            records = [r for r in _FAKE_MOVE_LINES.values() if _domain_matches(r, _request_domain(request))]
            return httpx.Response(200, json=records)
        if "/rest/model/account.payment/" in path:
            payment_id = int(path.rsplit("/", 1)[-1])
            record = _FAKE_PAYMENTS.get(payment_id)
            if record is None:
                return httpx.Response(404, text="not found")
            return httpx.Response(200, json=record)
        return httpx.Response(404, text="not found")

    return handler


def _adapter(seen: dict) -> TrytonAccountingAdapter:
    return TrytonAccountingAdapter(_mock_client(_fake_us522_handler(seen)))


# -- journal drafts: party / maturity pass-through + party rules -------------


def test_create_journal_draft_passes_party_and_maturity():
    seen: dict = {}
    adapter = _adapter(seen)
    ref = adapter.create_journal_draft(
        CreateJournalDraft(
            company=COMPANY,
            date=DATE,
            lines=(
                JournalDraftLine(
                    account=FEE_EXPENSE,
                    debit=MoneyIRR(2_500_000),
                    description="fee",
                ),
                JournalDraftLine(
                    account=FEE_VAT,
                    debit=MoneyIRR(250_000),
                    description="fee VAT",
                ),
                JournalDraftLine(
                    account=RECEIVABLE,
                    credit=MoneyIRR(2_750_000),
                    party=GATEWAY,
                    maturity_date=DUE,
                    description="fee leg",
                ),
            ),
            description="Gateway fee",
            journal=JournalRef(9),
        )
    )
    assert ref.engine_id == 500
    body = seen["move_create"]
    lines = body["lines"][0][1]
    assert lines[0] == {
        "account": 103,
        "debit": _decimal_wire(2_500_000),
        "credit": _decimal_wire(0),
        "description": "fee",
    }
    assert lines[2]["party"] == 10
    assert lines[2]["maturity_date"] == _date_wire(datetime.date(2026, 11, 26))
    assert body["date"] == _date_wire(datetime.date(2026, 11, 19))


def test_create_journal_draft_rejects_party_on_non_party_account():
    adapter = _adapter({})
    with pytest.raises(InvalidJournalError, match="does not take a party"):
        adapter.create_journal_draft(
            CreateJournalDraft(
                company=COMPANY,
                date=DATE,
                lines=(
                    JournalDraftLine(
                        account=FEE_EXPENSE,
                        debit=MoneyIRR(1_000),
                        party=GATEWAY,
                        description="fee",
                    ),
                    JournalDraftLine(account=BANK, credit=MoneyIRR(1_000), description="bank"),
                ),
                journal=JournalRef(9),
            )
        )


def test_create_journal_draft_rejects_missing_party_on_party_account():
    adapter = _adapter({})
    with pytest.raises(InvalidJournalError, match="requires a party"):
        adapter.create_journal_draft(
            CreateJournalDraft(
                company=COMPANY,
                date=DATE,
                lines=(
                    JournalDraftLine(
                        account=RECEIVABLE,
                        debit=MoneyIRR(1_000),
                        description="receivable",
                    ),
                    JournalDraftLine(account=REVENUE, credit=MoneyIRR(1_000), description="revenue"),
                ),
                journal=JournalRef(9),
            )
        )


# -- invoice drafts: payment term + alternative payees -----------------------


def test_create_invoice_draft_passes_term_and_payee():
    seen: dict = {}
    adapter = _adapter(seen)
    ref = adapter.create_invoice_draft(
        CreateInvoiceDraft(
            company=COMPANY,
            invoice_type="out",
            party=CUSTOMER,
            date=DATE,
            lines=(InvoiceDraftLine(account=REVENUE, unit_price=MoneyIRR(100_000_000)),),
            journal=JournalRef(1),
            account=RECEIVABLE,
            payment_term=PaymentTermRef(7),
            alternative_payees=(GATEWAY,),
        )
    )
    assert ref.engine_id == 600
    body = seen["invoice_create"]
    assert body["payment_term"] == 7
    assert body["alternative_payees"] == [["add", [10]]]
    assert body["party"] == 5  # the customer stays the invoice party


def test_create_invoice_draft_without_term_or_payee_omits_fields():
    seen: dict = {}
    adapter = _adapter(seen)
    adapter.create_invoice_draft(
        CreateInvoiceDraft(
            company=COMPANY,
            invoice_type="out",
            party=CUSTOMER,
            date=DATE,
            lines=(InvoiceDraftLine(account=REVENUE, unit_price=MoneyIRR(100_000_000)),),
            journal=JournalRef(1),
            account=RECEIVABLE,
        )
    )
    body = seen["invoice_create"]
    assert "payment_term" not in body
    assert "alternative_payees" not in body


# -- payment journals + batch processing -------------------------------------


def test_get_payment_journal_returns_clearing_config():
    adapter = _adapter({})
    view = adapter.get_payment_journal(PaymentJournalRef(20))
    assert view is not None
    assert view.clearing_account == CLEARING
    assert view.clearing_journal == JournalRef(9)
    assert view.is_clearing_configured
    plain = adapter.get_payment_journal(PaymentJournalRef(21))
    assert plain is not None
    assert not plain.is_clearing_configured


def test_process_payments_calls_batch_once_and_reads_back_in_order():
    seen: dict = {}
    adapter = _adapter(seen)
    views = adapter.process_payments([PaymentRef(9), PaymentRef(10)])
    assert seen["process_batch"] == {"payment_ids": [9, 10]}
    assert [view.ref.engine_id for view in views] == [9, 10]
    assert all(view.state == "processing" for view in views)
    assert views[0].move_line == MoveLineRef(55)


def test_process_payments_empty_is_a_noop():
    seen: dict = {}
    adapter = _adapter(seen)
    assert adapter.process_payments([]) == []
    assert "process_batch" not in seen


def test_process_payments_missing_payment_is_not_found():
    adapter = _adapter({})
    with pytest.raises(PaymentNotFoundError):
        adapter.process_payments([PaymentRef(999)])


# -- move lines --------------------------------------------------------------


def test_list_move_lines_requires_move_or_account():
    adapter = _adapter({})
    with pytest.raises(InvalidJournalError, match="at least one of"):
        adapter.list_move_lines(COMPANY)


def test_list_move_lines_filters_by_account_party_and_maturity():
    adapter = _adapter({})
    views = adapter.list_move_lines(
        COMPANY,
        account=RECEIVABLE,
        party=GATEWAY,
        maturity_date=DUE,
        unreconciled_only=True,
    )
    # Lines 55 + 56 match (57 has no maturity; 59 is reconciled; 60 is
    # draft-state but the filter is on reconciliation, not state).
    assert {view.ref.engine_id for view in views} == {55, 56, 60}


def test_list_move_lines_by_move():
    adapter = _adapter({})
    views = adapter.list_move_lines(COMPANY, move=JournalDraftRef(23))
    assert {view.ref.engine_id for view in views} == {57, 58}


# -- reconciliation ----------------------------------------------------------


def test_reconcile_move_lines_happy_path():
    seen: dict = {}
    adapter = _adapter(seen)
    view = adapter.reconcile_move_lines(
        ReconcileMoveLines(
            company=COMPANY,
            lines=(MoveLineRef(55), MoveLineRef(56), MoveLineRef(57)),
        )
    )
    assert seen["reconcile"] == {"line_ids": [55, 56, 57]}
    assert view.ref.engine_id == 900
    assert view.number == "R-1"
    assert view.lines == (MoveLineRef(55), MoveLineRef(56), MoveLineRef(57))


def test_reconcile_rejects_missing_line():
    adapter = _adapter({})
    with pytest.raises(InvalidJournalError, match="not found"):
        adapter.reconcile_move_lines(ReconcileMoveLines(company=COMPANY, lines=(MoveLineRef(55), MoveLineRef(999))))


def test_reconcile_rejects_mixed_accounts():
    adapter = _adapter({})
    with pytest.raises(InvalidJournalError, match="same account"):
        adapter.reconcile_move_lines(ReconcileMoveLines(company=COMPANY, lines=(MoveLineRef(55), MoveLineRef(58))))


def test_reconcile_rejects_mixed_parties():
    adapter = _adapter({})
    with pytest.raises(InvalidJournalError, match="same party"):
        adapter.reconcile_move_lines(ReconcileMoveLines(company=COMPANY, lines=(MoveLineRef(55), MoveLineRef(61))))


def test_reconcile_rejects_already_reconciled_line():
    adapter = _adapter({})
    with pytest.raises(InvalidJournalError, match="already reconciled"):
        adapter.reconcile_move_lines(ReconcileMoveLines(company=COMPANY, lines=(MoveLineRef(55), MoveLineRef(59))))


def test_reconcile_rejects_draft_state_line():
    adapter = _adapter({})
    with pytest.raises(InvalidJournalError, match="only 'valid' lines"):
        adapter.reconcile_move_lines(ReconcileMoveLines(company=COMPANY, lines=(MoveLineRef(55), MoveLineRef(60))))


def test_reconcile_rejects_unbalanced_set():
    adapter = _adapter({})
    with pytest.raises(InvalidJournalError, match="do not sum to zero"):
        adapter.reconcile_move_lines(ReconcileMoveLines(company=COMPANY, lines=(MoveLineRef(55), MoveLineRef(56))))
