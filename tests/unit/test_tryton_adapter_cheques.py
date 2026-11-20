"""Unit tests for the US-531 adapter extension (cheque accounting).

No live server is needed: the HTTP layer is exercised with httpx
MockTransport, following the fake-Tryton style of
``test_tryton_adapter_settlement.py``. Live behaviour (the real
``account_check_ir`` module) is covered by contract tests once
US-530 installs the module on lane 2.
"""

from __future__ import annotations

import base64
import datetime
import json
from decimal import Decimal

import httpx
import pytest

from hesabyar.domain.accounting.commands import CreateEngineCheque, TransitionEngineCheque
from hesabyar.domain.accounting.refs import (
    AccountRef,
    ChequeEngineRef,
    CompanyRef,
    PartyRef,
)
from hesabyar.domain.cheques.errors import ChequeEngineError
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.infrastructure.accounting.tryton.adapter import TrytonAccountingAdapter
from hesabyar.infrastructure.accounting.tryton.client import TrytonRestClient
from hesabyar.infrastructure.accounting.tryton.mapping import cheque_record_to_view

BASE_URL = "http://tryton.test:8011"
DATABASE = "hesabyar_tryton_test"
COMPANY = CompanyRef(2)
DATE = FiscalDate.from_gregorian(datetime.date(2026, 11, 19))
DUE = FiscalDate.from_gregorian(datetime.date(2027, 1, 5))
BOUNCE_DAY = FiscalDate.from_gregorian(datetime.date(2027, 1, 10))


def _mock_client(handler) -> TrytonRestClient:
    return TrytonRestClient(BASE_URL, DATABASE, "test-key", transport=httpx.MockTransport(handler))


def _date_wire(day: datetime.date) -> dict:
    return {"__class__": "date", "year": day.year, "month": day.month, "day": day.day}


def _decimal_wire(value: int) -> dict:
    return {"__class__": "Decimal", "decimal": str(value)}


def _cheque_record(**overrides) -> dict:
    # Wire format (the client decodes ``__class__`` dicts into
    # Decimal/date before mapping — see test_tryton_adapter_settlement).
    record = {
        "id": 1001,
        "company": 2,
        "kind": "received",
        "purpose": "payment",
        "sayad_id": "1234567890123456",
        "sayad_status": "unregistered",
        "state": "registered",
        "storage_location": None,
        "amount": _decimal_wire(17_500_000),
        "issue_date": _date_wire(datetime.date(2026, 11, 17)),
        "due_date": _date_wire(datetime.date(2027, 1, 5)),
        "number": "847291",
        "bank": "056 سامان",
        "branch": None,
        "issuer_account_number": None,
        "endorsee": None,
        "endorsee_type": None,
        "moves": [],
    }
    record.update(overrides)
    return record


def _create_command(**overrides) -> CreateEngineCheque:
    kwargs = {
        "company": COMPANY,
        "kind": "received",
        "purpose": "payment",
        "sayad_id": "1234567890123456",
        "amount": MoneyIRR(17_500_000),
        "issue_date": DATE,
        "due_date": DUE,
        "issuer_name": "مشتری نمونه",
        "issuer_national_id": "0012345678",
        "issuer_bank": "056 سامان",
        "payee_name": "کافه ایوان",
        "payee_national_id": "00123456789",
        "order_id": "ord-1",
        "customer_ref": "cust-1",
        "sayad_registered": True,
        "source": "manual",
    }
    kwargs.update(overrides)
    return CreateEngineCheque(**kwargs)


def _fake_cheque_handler(seen: dict, *, state: str = "registered", moves=None):
    """Fake Tryton serving company/currency + one account.check record.

    ``moves`` is the record's ``moves`` one2many — a list of move
    ids, as the real module returns it (no transition attribution;
    the attribution comes from the action's ``move_id`` return).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/rest/model/company.company/2"):
            return httpx.Response(200, json={"id": 2, "currency": 1})
        if path.endswith("/rest/model/currency.currency"):
            return httpx.Response(200, json=[{"id": 1, "code": "IRR"}])
        if path.endswith("/rest/model/party.party") and request.method == "GET":
            encoded = str(request.url.params.get("d", ""))
            seen["party_search"] = (
                json.loads(base64.b64decode(encoded).decode())
                if encoded
                else []
            )
            return httpx.Response(
                200, json=seen.get("party_search_result", [])
            )
        if path.endswith("/rest/model/party.party") and request.method == "POST":
            seen["party_create"] = json.loads(request.content)
            return httpx.Response(200, json={"id": 2001})
        if path.endswith("/rest/model/account.check") and request.method == "POST":
            seen["create"] = json.loads(request.content)
            return httpx.Response(200, json={"id": 1001})
        if path.endswith("/rest/model/account.check/1001/transition_rest"):
            seen["transition"] = json.loads(request.content)
            return httpx.Response(
                200, json={"id": 1001, "state": state, "move_id": 5000}
            )
        if path.endswith("/rest/model/account.check/1001"):
            return httpx.Response(
                200,
                json=_cheque_record(state=state, moves=moves if moves is not None else []),
            )
        if path.endswith("/rest/model/account.check"):
            return httpx.Response(200, json=[_cheque_record(state=state)])
        return httpx.Response(404, text="not found")

    return handler


# -- mapping ----------------------------------------------------------------------


def _decoded_record(**overrides) -> dict:
    """A cheque record as the mapping layer sees it (post-decode)."""
    record = _cheque_record(**overrides)
    record["amount"] = Decimal(17_500_000)
    record["issue_date"] = datetime.date(2026, 11, 17)
    record["due_date"] = datetime.date(2027, 1, 5)
    return record


def test_cheque_record_to_view_with_list_moves():
    # A plain read returns move ids with no transition attribution.
    view = cheque_record_to_view(_decoded_record(state="held", moves=[5000]))
    assert view.ref == ChequeEngineRef(engine_id=1001)
    assert view.company == COMPANY
    assert view.state == "held"
    assert view.sayad_id == "1234567890123456"
    assert view.amount == MoneyIRR(17_500_000)
    assert view.move_ids == {}
    assert view.leaf_number == "847291"


def test_cheque_record_to_view_with_dict_moves_and_no_moves():
    # Action-shaped records may carry the attribution explicitly.
    view = cheque_record_to_view(
        _decoded_record(moves={"deposit": 5001, "clear": 5002})
    )
    assert view.move_ids == {"deposit": 5001, "clear": 5002}
    view = cheque_record_to_view(_decoded_record(moves=None))
    assert view.move_ids == {}


# -- create -------------------------------------------------------------------------


def test_create_sends_the_prd_payload_and_reads_the_record_back():
    seen: dict = {}
    adapter = TrytonAccountingAdapter(_mock_client(_fake_cheque_handler(seen)))
    view = adapter.create_engine_cheque(_create_command())
    assert view.ref == ChequeEngineRef(engine_id=1001)
    assert view.state == "registered"
    payload = seen["create"]
    assert payload["sayad_id"] == "1234567890123456"
    assert payload["kind"] == "received"
    assert payload["purpose"] == "payment"
    assert payload["amount"] == _decimal_wire(17_500_000)
    assert payload["issue_date"] == _date_wire(datetime.date(2026, 11, 19))
    assert payload["due_date"] == _date_wire(datetime.date(2027, 1, 5))
    assert payload["sayad_status"] == "registered"
    assert payload["source"] == "manual"
    # Only the fields the model has travel: issuer/payee names,
    # national ids, order/customer refs, reason and scan ref stay
    # in the HesabYar registry (no engine field exists for them).
    assert "issuer_name" not in payload
    assert "issuer_national_id" not in payload
    assert "payee_name" not in payload
    assert "order_id" not in payload
    assert "customer_ref" not in payload
    assert "bank" not in payload
    assert "reason" not in payload
    assert "scan_ref" not in payload
    # Optional fields are omitted when empty (no guessed keys).
    assert "number" not in payload
    assert "invoice" not in payload
    assert "installment_leg" not in payload


def test_create_sends_optional_fields_when_present():
    seen: dict = {}
    adapter = TrytonAccountingAdapter(_mock_client(_fake_cheque_handler(seen)))
    adapter.create_engine_cheque(
        _create_command(
            leaf_number="847291",
            issuer_branch="شعبه مرکزی",
            invoice_engine_id=77,
            installment_leg_id="leg-7",
            scan_ref="scan-1",
        )
    )
    payload = seen["create"]
    assert payload["number"] == "847291"
    assert payload["branch"] == "شعبه مرکزی"
    assert payload["invoice"] == 77
    assert payload["installment_leg"] == "leg-7"
    assert "scan_ref" not in payload  # registry-only; no engine field


def test_create_resolves_the_issuer_party_by_national_id():
    # No party on the command: the adapter finds no party by code,
    # creates one (name + national id as code), and sends its id.
    seen: dict = {}
    adapter = TrytonAccountingAdapter(_mock_client(_fake_cheque_handler(seen)))
    adapter.create_engine_cheque(_create_command())
    assert seen["party_search"] == [["code", "=", "0012345678"]]
    assert seen["party_create"] == {
        "name": "مشتری نمونه",
        "code": "0012345678",
    }
    assert seen["create"]["party"] == 2001


def test_create_reuses_the_existing_issuer_party():
    # A party with the national id as code already exists: reuse it,
    # do not create a duplicate.
    seen: dict = {"party_search_result": [{"id": 2001}]}
    adapter = TrytonAccountingAdapter(_mock_client(_fake_cheque_handler(seen)))
    adapter.create_engine_cheque(_create_command())
    assert "party_create" not in seen
    assert seen["create"]["party"] == 2001


def test_create_keeps_an_explicit_party_without_a_search():
    # The mapping center already resolved the party: the adapter
    # sends it as-is and never searches.
    seen: dict = {}
    adapter = TrytonAccountingAdapter(_mock_client(_fake_cheque_handler(seen)))
    adapter.create_engine_cheque(
        _create_command(party=PartyRef(engine_id=3001))
    )
    assert "party_search" not in seen
    assert "party_create" not in seen
    assert seen["create"]["party"] == 3001


def test_create_403_means_the_module_is_not_installed():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/rest/model/company.company/2"):
            return httpx.Response(200, json={"id": 2, "currency": 1})
        if path.endswith("/rest/model/currency.currency"):
            return httpx.Response(200, json=[{"id": 1, "code": "IRR"}])
        if path.endswith("/rest/model/party.party") and request.method == "GET":
            return httpx.Response(200, json=[])
        if path.endswith("/rest/model/party.party") and request.method == "POST":
            return httpx.Response(200, json={"id": 2001})
        if path.endswith("/rest/model/account.check") and request.method == "POST":
            return httpx.Response(403, text="forbidden")
        return httpx.Response(404, text="not found")

    adapter = TrytonAccountingAdapter(_mock_client(handler))
    with pytest.raises(ChequeEngineError, match="not installed"):
        adapter.create_engine_cheque(_create_command())


# -- transition -----------------------------------------------------------------------


def test_transition_sends_business_params_only_and_reads_back():
    seen: dict = {}
    adapter = TrytonAccountingAdapter(
        _mock_client(
            _fake_cheque_handler(
                seen,
                state="held",
                moves=[5000],
            )
        )
    )
    view = adapter.transition_engine_cheque(
        TransitionEngineCheque(
            cheque=ChequeEngineRef(engine_id=1001),
            company=COMPANY,
            transition="confirm_receipt",
            auto_post=False,
            storage_location="vault",
        )
    )
    assert view.state == "held"
    # The attribution comes from the action's move_id return, not
    # from the plain read (whose moves list is unattributed).
    assert view.move_ids == {"confirm_receipt": 5000}
    payload = seen["transition"]
    assert payload["transition"] == "confirm_receipt"
    assert payload["auto_post"] is False
    assert payload["defer_journal"] is False
    assert payload["storage_location"] == "vault"
    # No journal-construction fields travel: the engine owns سند ۱.
    assert "destination_account" not in payload
    assert "bounce_reason" not in payload


def test_transition_bounce_carries_reason_date_and_certificate():
    seen: dict = {}
    adapter = TrytonAccountingAdapter(
        _mock_client(_fake_cheque_handler(seen, state="bounced"))
    )
    adapter.transition_engine_cheque(
        TransitionEngineCheque(
            cheque=ChequeEngineRef(engine_id=1001),
            company=COMPANY,
            transition="bounce",
            auto_post=False,
            bounce_destination="receivables",
            bounce_reason="عدم کفایت موجودی",
            bounce_date=BOUNCE_DAY,
            certificate_reference="cert-1",
        )
    )
    payload = seen["transition"]
    assert payload["bounce_reason"] == "عدم کفایت موجودی"
    assert payload["bounce_date"] == _date_wire(datetime.date(2027, 1, 10))
    assert payload["certificate_reference"] == "cert-1"
    assert payload["bounce_destination"] == "receivables"


def test_transition_deposit_carries_the_destination_account():
    seen: dict = {}
    adapter = TrytonAccountingAdapter(
        _mock_client(_fake_cheque_handler(seen, state="in_clearing"))
    )
    adapter.transition_engine_cheque(
        TransitionEngineCheque(
            cheque=ChequeEngineRef(engine_id=1001),
            company=COMPANY,
            transition="deposit",
            auto_post=True,
            destination_account=AccountRef(engine_id=42),
        )
    )
    payload = seen["transition"]
    assert payload["destination_account"] == 42
    assert payload["auto_post"] is True


def test_transition_403_means_the_module_is_not_installed():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/rest/model/company.company/2"):
            return httpx.Response(200, json={"id": 2, "currency": 1})
        if path.endswith("/rest/model/currency.currency"):
            return httpx.Response(200, json=[{"id": 1, "code": "IRR"}])
        if path.endswith("/rest/model/account.check/1001/transition_rest"):
            return httpx.Response(403, text="forbidden")
        return httpx.Response(404, text="not found")

    adapter = TrytonAccountingAdapter(_mock_client(handler))
    with pytest.raises(ChequeEngineError, match="not installed"):
        adapter.transition_engine_cheque(
            TransitionEngineCheque(
                cheque=ChequeEngineRef(engine_id=1001),
                company=COMPANY,
                transition="clear",
            )
        )


# -- reads ------------------------------------------------------------------------------


def test_get_engine_cheque_returns_none_on_404():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/rest/model/account.check/9999"):
            return httpx.Response(404, text="not found")
        return httpx.Response(404, text="not found")

    adapter = TrytonAccountingAdapter(_mock_client(handler))
    assert adapter.get_engine_cheque(ChequeEngineRef(engine_id=9999)) is None


def test_list_engine_cheques_returns_newest_first():
    seen: dict = {}
    adapter = TrytonAccountingAdapter(_mock_client(_fake_cheque_handler(seen)))
    views = adapter.list_engine_cheques(COMPANY)
    assert len(views) == 1
    assert views[0].ref == ChequeEngineRef(engine_id=1001)
    assert views[0].state == "registered"


# -- US-532: issued / collateral / checkbooks -----------------------------------


def test_create_issued_sends_checkbook_obligation_and_beneficiary_party():
    seen: dict = {}
    adapter = TrytonAccountingAdapter(_mock_client(_fake_cheque_handler(seen)))
    view = adapter.create_engine_cheque(
        _create_command(
            kind="issued",
            checkbook_engine_id=7,
            secured_obligation="قرارداد اجاره ۱۴۰۵/۰۳",
        )
    )
    assert view.state == "registered"
    payload = seen["create"]
    assert payload["kind"] == "issued"
    assert payload["checkbook"] == 7
    assert payload["secured_obligation"] == "قرارداد اجاره ۱۴۰۵/۰۳"
    # An issued cheque's engine party is the BENEFICIARY (payee),
    # not the issuer — every chart account is party_required.
    assert seen["party_search"] == [["code", "=", "00123456789"]]
    assert seen["party_create"] == {
        "name": "کافه ایوان",
        "code": "00123456789",
    }
    assert payload["party"] == 2001


def test_create_received_sends_no_checkbook_key():
    seen: dict = {}
    adapter = TrytonAccountingAdapter(_mock_client(_fake_cheque_handler(seen)))
    adapter.create_engine_cheque(_create_command())
    assert "checkbook" not in seen["create"]
    assert "secured_obligation" not in seen["create"]


def _action_handler(seen: dict, *, state: str, action_result: dict):
    """Fake Tryton whose transition_rest returns an explicit result."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/rest/model/company.company/2"):
            return httpx.Response(200, json={"id": 2, "currency": 1})
        if path.endswith("/rest/model/currency.currency"):
            return httpx.Response(200, json=[{"id": 1, "code": "IRR"}])
        if path.endswith("/rest/model/account.check/1001/transition_rest"):
            seen["transition"] = json.loads(request.content)
            return httpx.Response(200, json=action_result)
        if path.endswith("/rest/model/account.check/1001"):
            return httpx.Response(200, json=_cheque_record(state=state))
        return httpx.Response(404, text="not found")

    return handler


def test_transition_call_in_merges_both_moves():
    seen: dict = {}
    adapter = TrytonAccountingAdapter(
        _mock_client(
            _action_handler(
                seen,
                state="called_in",
                action_result={
                    "id": 1001,
                    "state": "called_in",
                    "move_id": None,
                    "move_ids": {"call_in_reversal": 6001, "call_in": 6002},
                },
            )
        )
    )
    view = adapter.transition_engine_cheque(
        TransitionEngineCheque(
            cheque=ChequeEngineRef(engine_id=1001),
            company=COMPANY,
            transition="call_in",
            auto_post=False,
            accountant_approval="حسابدار ارشد",
            kind="received",
        )
    )
    assert view.state == "called_in"
    assert view.move_ids == {"call_in_reversal": 6001, "call_in": 6002}
    assert seen["transition"]["accountant_approval"] == "حسابدار ارشد"


def test_transition_void_carries_the_reason():
    seen: dict = {}
    adapter = TrytonAccountingAdapter(
        _mock_client(
            _action_handler(
                seen,
                state="voided",
                action_result={"id": 1001, "state": "voided", "move_id": 5000},
            )
        )
    )
    view = adapter.transition_engine_cheque(
        TransitionEngineCheque(
            cheque=ChequeEngineRef(engine_id=1001),
            company=COMPANY,
            transition="void",
            auto_post=False,
            void_reason="اشتباه در مبلغ",
            kind="issued",
        )
    )
    assert view.state == "voided"
    assert view.move_ids == {"void": 5000}
    assert seen["transition"]["void_reason"] == "اشتباه در مبلغ"


def test_transition_unexpected_call_in_carries_the_statement_reference():
    seen: dict = {}
    adapter = TrytonAccountingAdapter(
        _mock_client(
            _action_handler(
                seen,
                state="called_in",
                action_result={"id": 1001, "state": "called_in", "move_id": 5000},
            )
        )
    )
    view = adapter.transition_engine_cheque(
        TransitionEngineCheque(
            cheque=ChequeEngineRef(engine_id=1001),
            company=COMPANY,
            transition="unexpected_call_in",
            auto_post=False,
            statement_reference="stmt-1",
            kind="issued",
        )
    )
    assert view.state == "called_in"
    assert view.move_ids == {"unexpected_call_in": 5000}
    assert seen["transition"]["statement_reference"] == "stmt-1"


def test_transition_endorsed_bounce_carries_evidence_and_no_move():
    seen: dict = {}
    adapter = TrytonAccountingAdapter(
        _mock_client(
            _action_handler(
                seen,
                state="bounced",
                action_result={"id": 1001, "state": "bounced", "move_id": None},
            )
        )
    )
    view = adapter.transition_engine_cheque(
        TransitionEngineCheque(
            cheque=ChequeEngineRef(engine_id=1001),
            company=COMPANY,
            transition="endorsed_bounce",
            auto_post=False,
            bounce_reason="عدم کفایت موجودی",
            bounce_date=BOUNCE_DAY,
            certificate_reference="cert-9",
            kind="received",
        )
    )
    assert view.state == "bounced"
    assert view.move_ids == {}  # §9.1 #3: no journal is guessed
    payload = seen["transition"]
    assert payload["bounce_reason"] == "عدم کفایت موجودی"
    assert payload["certificate_reference"] == "cert-9"


def test_checkbook_record_to_view_maps_leaf_counts():
    from hesabyar.infrastructure.accounting.tryton.mapping import (
        checkbook_record_to_view,
    )

    view = checkbook_record_to_view(
        {
            "id": 7,
            "company": 2,
            "name": "دسته‌چک سامان",
            "bank": 3,
            "bank_account": 9,
            "first_number": 100,
            "last_number": 109,
            "total_leaves": 10,
            "used_leaves": 3,
            "voided_leaves": 1,
            "remaining_leaves": 6,
        }
    )
    assert view.ref.engine_id == 7
    assert view.company == COMPANY
    assert view.name == "دسته‌چک سامان"
    assert view.bank_engine_id == 3
    assert view.bank_account_engine_id == 9
    assert view.first_number == 100
    assert view.last_number == 109
    assert view.total_leaves == 10
    assert view.used_leaves == 3
    assert view.voided_leaves == 1
    assert view.remaining_leaves == 6


def test_checkbook_record_to_view_defaults_missing_counts_to_zero():
    from hesabyar.infrastructure.accounting.tryton.mapping import (
        checkbook_record_to_view,
    )

    view = checkbook_record_to_view({"id": 7, "company": 2})
    assert view.total_leaves == 0
    assert view.used_leaves == 0
    assert view.voided_leaves == 0
    assert view.remaining_leaves == 0
    assert view.bank_engine_id is None


def test_list_engine_checkbooks_returns_newest_first():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/rest/model/company.company/2"):
            return httpx.Response(200, json={"id": 2, "currency": 1})
        if path.endswith("/rest/model/currency.currency"):
            return httpx.Response(200, json=[{"id": 1, "code": "IRR"}])
        if path.endswith("/rest/model/account.checkbook"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 8,
                        "company": 2,
                        "name": "دسته‌چک دوم",
                        "bank": 3,
                        "bank_account": 9,
                        "first_number": 200,
                        "last_number": 209,
                        "total_leaves": 10,
                        "used_leaves": 0,
                        "voided_leaves": 0,
                        "remaining_leaves": 10,
                    },
                    {
                        "id": 7,
                        "company": 2,
                        "name": "دسته‌چک سامان",
                        "bank": 3,
                        "bank_account": 9,
                        "first_number": 100,
                        "last_number": 109,
                        "total_leaves": 10,
                        "used_leaves": 3,
                        "voided_leaves": 1,
                        "remaining_leaves": 6,
                    },
                ],
            )
        return httpx.Response(404, text="not found")

    adapter = TrytonAccountingAdapter(_mock_client(handler))
    views = adapter.list_engine_checkbooks(COMPANY)
    assert [v.ref.engine_id for v in views] == [8, 7]
    assert views[1].remaining_leaves == 6


def test_get_engine_checkbook_returns_none_on_404():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/rest/model/company.company/2"):
            return httpx.Response(200, json={"id": 2, "currency": 1})
        if path.endswith("/rest/model/currency.currency"):
            return httpx.Response(200, json=[{"id": 1, "code": "IRR"}])
        if path.endswith("/rest/model/account.checkbook/9999"):
            return httpx.Response(404, text="not found")
        return httpx.Response(404, text="not found")

    from hesabyar.domain.accounting.refs import CheckbookEngineRef

    adapter = TrytonAccountingAdapter(_mock_client(handler))
    assert (
        adapter.get_engine_checkbook(COMPANY, CheckbookEngineRef(engine_id=9999))
        is None
    )
