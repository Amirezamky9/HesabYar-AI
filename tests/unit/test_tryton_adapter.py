"""Unit tests for the Tryton REST adapter spike (US-003).

No live server is needed: the HTTP layer is exercised with httpx
MockTransport, and the static guards enforce the architecture rules
(no Tryton imports in domain code, no SQL in the adapter) on every run.
"""

from __future__ import annotations

import ast
import base64
import datetime
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from hesabyar.domain.accounting.commands import (
    CreateInvoiceDraft,
    CreateJournalDraft,
    InvoiceDraftLine,
    JournalDraftLine,
    PostJournal,
    ReverseJournal,
    UpdateJournalDraft,
)
from hesabyar.domain.accounting.refs import (
    AccountRef,
    BankStatementRef,
    CompanyRef,
    FiscalYearRef,
    InvoiceRef,
    JournalDraftRef,
    JournalRef,
    PartyAddressRef,
    PartyRef,
    PaymentRef,
    PostedJournalRef,
)
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.errors import (
    AccountNotFoundError,
    InvalidBankStatementError,
    InvalidInvoiceError,
    InvalidJournalError,
    InvalidMoneyError,
    InvalidPaymentError,
    InvoiceNotFoundError,
    JournalAlreadyPostedError,
    JournalDraftNotFoundError,
    PartyNotFoundError,
    PeriodClosedError,
    PostedJournalNotFoundError,
    UnbalancedJournalError,
)
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.infrastructure.accounting.tryton import contracts
from hesabyar.infrastructure.accounting.tryton.adapter import TrytonAccountingAdapter
from hesabyar.infrastructure.accounting.tryton.client import TrytonRestClient
from hesabyar.infrastructure.accounting.tryton.errors import (
    ENGINE_AUTHENTICATION_FAILED,
    ENGINE_ERROR,
    ENGINE_UNAVAILABLE,
    ENGINE_VALIDATION_FAILED,
    TrytonAdapterError,
)
from hesabyar.infrastructure.accounting.tryton.mapping import (
    invoice_line_record_to_view,
    invoice_record_to_view,
    money_from_tryton,
    parse_invoice_reference,
    parse_move_origin,
    payment_record_to_view,
    quantity_from_tryton,
    quantity_to_tryton,
    statement_line_record_to_view,
    statement_record_to_view,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_URL = "http://tryton.test:8000"
DATABASE = "hesabyar_tryton_test"


def _mock_client(handler) -> TrytonRestClient:
    return TrytonRestClient(
        BASE_URL, DATABASE, "test-key", transport=httpx.MockTransport(handler)
    )


# ---------------------------------------------------------------------------
# Domain command invariants
# ---------------------------------------------------------------------------


def _line(account_id: int, debit: int = 0, credit: int = 0) -> JournalDraftLine:
    return JournalDraftLine(
        account=AccountRef(account_id),
        debit=MoneyIRR(debit),
        credit=MoneyIRR(credit),
    )


def _command(lines, **kwargs) -> CreateJournalDraft:
    return CreateJournalDraft(
        company=CompanyRef(2),
        date=FiscalDate.from_gregorian(datetime.date(2026, 11, 16)),
        lines=tuple(lines),
        **kwargs,
    )


def test_balanced_command_passes():
    command = _command([_line(170, debit=1000), _line(171, credit=1000)])
    assert command.total_debit == MoneyIRR(1000)
    assert command.total_credit == MoneyIRR(1000)


def test_unbalanced_command_rejected():
    with pytest.raises(UnbalancedJournalError) as excinfo:
        _command([_line(170, debit=1000), _line(171, credit=999)])
    assert excinfo.value.code == "LEDGER_UNBALANCED"


def test_two_sided_line_rejected():
    with pytest.raises(InvalidJournalError):
        _line(170, debit=100, credit=100)


def test_zero_line_rejected():
    with pytest.raises(InvalidJournalError):
        _line(170)


def test_negative_line_rejected():
    with pytest.raises(InvalidJournalError):
        _line(170, debit=-100)


def test_single_line_command_rejected():
    with pytest.raises(InvalidJournalError):
        _command([_line(170, debit=1000)])


def test_update_command_requires_a_change():
    with pytest.raises(InvalidJournalError) as excinfo:
        UpdateJournalDraft(draft=JournalDraftRef(42))
    assert excinfo.value.code == "LEDGER_INVALID_JOURNAL"


def test_update_command_rejects_unbalanced_lines():
    with pytest.raises(UnbalancedJournalError) as excinfo:
        UpdateJournalDraft(
            draft=JournalDraftRef(42),
            lines=(_line(170, debit=1000), _line(171, credit=999)),
        )
    assert excinfo.value.code == "LEDGER_UNBALANCED"


def test_update_command_rejects_too_few_lines():
    with pytest.raises(InvalidJournalError):
        UpdateJournalDraft(
            draft=JournalDraftRef(42), lines=(_line(170, debit=1000),)
        )


def test_update_command_description_only_passes():
    command = UpdateJournalDraft(draft=JournalDraftRef(42), description="renamed")
    assert command.description == "renamed"
    assert command.lines is None


# ---------------------------------------------------------------------------
# Money mapping: integer IRR only, fractional rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, 0),
        (1_000_000, 1_000_000),
        (Decimal("2500"), 2500),
        (Decimal("0"), 0),
        ("12345", 12345),
    ],
)
def test_money_from_tryton_accepts_integral(value, expected):
    assert money_from_tryton(value) == MoneyIRR(expected)


@pytest.mark.parametrize("value", [Decimal("100.5"), Decimal("0.1"), "99.9"])
def test_money_from_tryton_rejects_fractional(value):
    with pytest.raises(InvalidMoneyError):
        money_from_tryton(value)


def test_money_from_tryton_rejects_float():
    with pytest.raises(InvalidMoneyError):
        money_from_tryton(100.0)


def test_money_from_tryton_rejects_non_finite():
    with pytest.raises(InvalidMoneyError):
        money_from_tryton(Decimal("NaN"))


# ---------------------------------------------------------------------------
# Wire format
# ---------------------------------------------------------------------------


def test_wire_round_trip():
    payload = {
        "amount": Decimal("1234567"),
        "date": datetime.date(2026, 11, 16),
        "nested": [{"when": datetime.datetime(2026, 11, 16, 10, 30)}],
    }
    encoded = contracts.encode_wire(payload)
    assert encoded["amount"] == {"__class__": "Decimal", "decimal": "1234567"}
    assert encoded["date"] == {"__class__": "date", "year": 2026, "month": 11, "day": 16}
    assert json.loads(json.dumps(encoded)) == encoded  # JSON-safe
    assert contracts.decode_wire(encoded) == payload


def test_domain_encoding_round_trip():
    import base64

    domain = [("start_date", "<=", datetime.date(2026, 11, 16))]
    encoded = contracts.encode_domain(domain)
    decoded = json.loads(base64.urlsafe_b64decode(encoded))
    assert decoded == [
        ["start_date", "<=", {"__class__": "date", "year": 2026, "month": 11, "day": 16}]
    ]


# ---------------------------------------------------------------------------
# REST client behaviour (MockTransport)
# ---------------------------------------------------------------------------


def test_client_search_sends_bearer_key_domain_and_context():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["context"] = request.headers.get("X-Tryton-Context")
        return httpx.Response(200, json=[{"id": 1, "__name__": "res.user"}])

    client = _mock_client(handler)
    result = client.search(
        "res.user",
        domain=[("login", "=", "admin")],
        fields=["login"],
        limit=1,
        context={"company": 2},
    )
    assert result == [{"id": 1, "__name__": "res.user"}]
    assert seen["auth"] == "Bearer test-key"
    assert "rest/model/res.user" in seen["url"]
    assert "f=login" in seen["url"]
    assert "d=" in seen["url"]
    assert "s=1" in seen["url"]
    assert seen["context"] is not None
    import base64

    assert json.loads(base64.urlsafe_b64decode(seen["context"])) == {"company": 2}


def test_client_decodes_decimal_and_date():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "id": 1,
                    "debit": {"__class__": "Decimal", "decimal": "1000000"},
                    "date": {"__class__": "date", "year": 2026, "month": 11, "day": 16},
                }
            ],
        )

    client = _mock_client(handler)
    (record,) = client.search("account.move.line")
    assert record["debit"] == Decimal("1000000")
    assert record["date"] == datetime.date(2026, 11, 16)


def test_client_create_encodes_decimal_and_date():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": 42, "state": "draft"})

    client = _mock_client(handler)
    client.create(
        "account.move",
        {"date": datetime.date(2026, 11, 16), "lines": [["create", [{"debit": Decimal("5")}]]]},
    )
    assert seen["body"]["date"] == {"__class__": "date", "year": 2026, "month": 11, "day": 16}
    assert seen["body"]["lines"][0][1][0]["debit"] == {"__class__": "Decimal", "decimal": "5"}


def test_client_read_returns_none_on_404():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="Not Found")

    client = _mock_client(handler)
    assert client.read("account.move", 999) is None


def test_client_401_maps_to_authentication_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="Unauthorized")

    client = _mock_client(handler)
    with pytest.raises(TrytonAdapterError) as excinfo:
        client.search("res.user")
    assert excinfo.value.code == ENGINE_AUTHENTICATION_FAILED
    assert excinfo.value.status_code == 401


def test_client_500_html_is_sanitized_without_traceback():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            text=(
                "<!doctype html><title>500 Internal Server Error</title>"
                "<h1>Internal Server Error</h1>"
                "<p>Some engine failure</p>"
                "<pre>Traceback (most recent call last):\n"
                '  File "/srv/trytond/model.py", line 1, in x\n'
                "Boom</pre>"
            ),
        )

    client = _mock_client(handler)
    with pytest.raises(TrytonAdapterError) as excinfo:
        client.search("res.user")
    assert excinfo.value.code == ENGINE_ERROR
    assert "Some engine failure" in str(excinfo.value)
    assert "Traceback" not in str(excinfo.value)
    assert "trytond/model.py" not in str(excinfo.value)


def test_client_connect_error_maps_to_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = _mock_client(handler)
    with pytest.raises(TrytonAdapterError) as excinfo:
        client.search("res.user")
    assert excinfo.value.code == ENGINE_UNAVAILABLE
    assert excinfo.value.retryable is True


def test_client_ping_returns_false_without_raising():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    assert _mock_client(handler).ping() is False


# ---------------------------------------------------------------------------
# Adapter logic against a fake Tryton (MockTransport)
# ---------------------------------------------------------------------------

_FAKE_COMPANY_ID = 2
_FAKE_JOURNAL_ID = 9
_FAKE_PERIOD_ID = 11


def _fake_tryton_handler(
    lines_state: str = "valid",
    period_state: str = "open",
    move_state: str = "draft",
    account_closed: bool = False,
):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/rest/model/company.company/2"):
            return httpx.Response(200, json={"id": 2, "currency": 1})
        if path.endswith("/rest/model/currency.currency"):
            return httpx.Response(200, json=[{"id": 1, "code": "IRR"}])
        if path.endswith("/rest/model/account.account"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 170,
                        "closed": account_closed,
                        "party_required": False,
                        "active": True,
                    },
                    {"id": 171, "closed": False, "party_required": False, "active": True},
                ],
            )
        if path.endswith("/rest/model/account.period"):
            return httpx.Response(200, json=[{"id": _FAKE_PERIOD_ID, "name": "2026-11"}])
        if path.endswith("/rest/model/account.period/11"):
            return httpx.Response(
                200,
                json={
                    "id": 11,
                    "name": "2026-11",
                    "start_date": {"__class__": "date", "year": 2026, "month": 11, "day": 1},
                    "end_date": {"__class__": "date", "year": 2026, "month": 11, "day": 30},
                    "state": period_state,
                },
            )
        if path.endswith("/rest/model/account.journal/9"):
            return httpx.Response(200, json={"id": 9, "type": "general"})
        if path.endswith("/rest/model/account.move") and request.method == "POST":
            body = json.loads(request.content)
            assert body["company"] == _FAKE_COMPANY_ID
            assert body["journal"] == _FAKE_JOURNAL_ID
            assert body["period"] == _FAKE_PERIOD_ID
            assert body["lines"][0][0] == "create"  # One2Many command syntax
            return httpx.Response(200, json={"id": 42, "state": "draft"})
        if path.endswith("/rest/model/account.move/42/post") and request.method == "POST":
            # The post action returns the read-back record (rest.py).
            return httpx.Response(
                200,
                json={
                    "id": 42,
                    "number": "0001",
                    "state": "posted",
                    "post_date": {"__class__": "date", "year": 2026, "month": 11, "day": 16},
                },
            )
        if path.endswith("/rest/model/account.move/42"):
            return httpx.Response(
                200,
                json={
                    "id": 42,
                    "company": 2,
                    "journal": 9,
                    "period": 11,
                    "date": {"__class__": "date", "year": 2026, "month": 11, "day": 16},
                    "state": move_state,
                    "description": "test",
                    "number": "0001" if move_state == "posted" else None,
                    "post_date": {"__class__": "date", "year": 2026, "month": 11, "day": 16}
                    if move_state == "posted"
                    else None,
                    "origin": None,
                },
            )
        if path.endswith("/rest/model/account.move.line"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 1,
                        "account": 170,
                        "debit": {"__class__": "Decimal", "decimal": "1000"},
                        "credit": {"__class__": "Decimal", "decimal": "0"},
                        "state": lines_state,
                        "description": "",
                    },
                    {
                        "id": 2,
                        "account": 171,
                        "debit": {"__class__": "Decimal", "decimal": "0"},
                        "credit": {"__class__": "Decimal", "decimal": "1000"},
                        "state": lines_state,
                        "description": "",
                    },
                ],
            )
        return httpx.Response(404, text="not found")

    return handler


def test_adapter_create_draft_happy_path():
    adapter = TrytonAccountingAdapter(_mock_client(_fake_tryton_handler()))
    command = _command(
        [_line(170, debit=1000), _line(171, credit=1000)],
        journal=JournalRef(_FAKE_JOURNAL_ID),
        description="unit test draft",
    )
    ref = adapter.create_journal_draft(command)
    assert ref == JournalDraftRef(42)


def test_adapter_create_draft_rejects_unknown_account():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/rest/model/company.company/2"):
            return httpx.Response(200, json={"id": 2, "currency": 1})
        if path.endswith("/rest/model/currency.currency"):
            return httpx.Response(200, json=[{"id": 1, "code": "IRR"}])
        if path.endswith("/rest/model/account.account"):
            return httpx.Response(200, json=[])  # account not found
        return httpx.Response(404, text="not found")

    adapter = TrytonAccountingAdapter(_mock_client(handler))
    command = _command(
        [_line(999999, debit=1000), _line(171, credit=1000)],
        journal=JournalRef(_FAKE_JOURNAL_ID),
    )
    with pytest.raises(AccountNotFoundError) as excinfo:
        adapter.create_journal_draft(command)
    assert excinfo.value.code == "LEDGER_ACCOUNT_NOT_FOUND"


def test_adapter_validate_balanced_draft_ok():
    adapter = TrytonAccountingAdapter(_mock_client(_fake_tryton_handler(lines_state="valid")))
    summary = adapter.validate_journal_draft(JournalDraftRef(42))
    assert summary.valid is True
    assert summary.issues == ()


def test_adapter_validate_flags_engine_unbalanced_verdict():
    # HesabYar totals balance, but the engine still marks the lines 'draft'.
    adapter = TrytonAccountingAdapter(_mock_client(_fake_tryton_handler(lines_state="draft")))
    summary = adapter.validate_journal_draft(JournalDraftRef(42))
    assert summary.valid is False
    assert "LEDGER_UNBALANCED" in summary.codes()


# ---------------------------------------------------------------------------
# US-201: post / reverse / journal read / fiscal years / periods
# ---------------------------------------------------------------------------


def test_adapter_post_journal_happy_path():
    adapter = TrytonAccountingAdapter(_mock_client(_fake_tryton_handler()))
    ref = adapter.post_journal(PostJournal(JournalDraftRef(42)))
    assert ref == PostedJournalRef(42)


def test_adapter_post_journal_rejects_unbalanced_draft():
    adapter = TrytonAccountingAdapter(_mock_client(_fake_tryton_handler(lines_state="draft")))
    with pytest.raises(UnbalancedJournalError) as excinfo:
        adapter.post_journal(PostJournal(JournalDraftRef(42)))
    assert excinfo.value.code == "LEDGER_UNBALANCED"


def test_adapter_post_journal_rejects_closed_account():
    # Same defense-in-depth story as the closed-period check: the engine
    # refuses to create lines on a closed account and refuses to close an
    # account that has move lines, so a draft on a closed account is only
    # reachable outside REST.
    adapter = TrytonAccountingAdapter(_mock_client(_fake_tryton_handler(account_closed=True)))
    with pytest.raises(AccountNotFoundError) as excinfo:
        adapter.post_journal(PostJournal(JournalDraftRef(42)))
    assert excinfo.value.code == "LEDGER_ACCOUNT_NOT_FOUND"


def test_adapter_post_journal_rejects_closed_period():
    # The engine makes a draft in a closed period unreachable via REST (it
    # refuses to close a period with unposted moves and refuses to create
    # moves in closed periods), so the adapter's closed-period check at
    # post time is defense-in-depth, covered here with a mock.
    adapter = TrytonAccountingAdapter(_mock_client(_fake_tryton_handler(period_state="closed")))
    with pytest.raises(PeriodClosedError) as excinfo:
        adapter.post_journal(PostJournal(JournalDraftRef(42)))
    assert excinfo.value.code == "LEDGER_PERIOD_CLOSED"


def test_adapter_post_journal_rejects_already_posted():
    adapter = TrytonAccountingAdapter(_mock_client(_fake_tryton_handler(move_state="posted")))
    with pytest.raises(JournalAlreadyPostedError) as excinfo:
        adapter.post_journal(PostJournal(JournalDraftRef(42)))
    assert excinfo.value.code == "LEDGER_POSTED_IMMUTABLE"


def test_adapter_post_journal_missing_draft_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    adapter = TrytonAccountingAdapter(_mock_client(handler))
    with pytest.raises(JournalDraftNotFoundError) as excinfo:
        adapter.post_journal(PostJournal(JournalDraftRef(999999)))
    assert excinfo.value.code == "LEDGER_JOURNAL_NOT_FOUND"


def _fake_reverse_handler():
    """Mock Tryton for a reversal: posted move 42 -> posted reversal 43."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/rest/model/account.move/42"):
            return httpx.Response(
                200,
                json={
                    "id": 42,
                    "company": 2,
                    "journal": 9,
                    "period": 11,
                    "date": {"__class__": "date", "year": 2026, "month": 11, "day": 16},
                    "state": "posted",
                    "description": "original",
                    "number": "0001",
                    "origin": None,
                },
            )
        if path.endswith("/rest/model/account.move.line"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 1,
                        "account": 170,
                        "debit": {"__class__": "Decimal", "decimal": "1000"},
                        "credit": {"__class__": "Decimal", "decimal": "0"},
                        "state": "valid",
                        "description": "",
                    },
                    {
                        "id": 2,
                        "account": 171,
                        "debit": {"__class__": "Decimal", "decimal": "0"},
                        "credit": {"__class__": "Decimal", "decimal": "1000"},
                        "state": "valid",
                        "description": "",
                    },
                ],
            )
        if path.endswith("/rest/model/account.period/11"):
            return httpx.Response(
                200,
                json={
                    "id": 11,
                    "name": "2026-11",
                    "start_date": {"__class__": "date", "year": 2026, "month": 11, "day": 1},
                    "end_date": {"__class__": "date", "year": 2026, "month": 11, "day": 30},
                    "state": "open",
                },
            )
        if path.endswith("/rest/model/account.move") and request.method == "POST":
            body = json.loads(request.content)
            seen["create"] = body
            assert body["origin"] == "account.move,42"
            assert body["period"] == 11
            assert body["description"] == "original"
            lines = body["lines"][0][1]
            by_account = {line["account"]: line for line in lines}
            # Reversal swaps the sides (wizard reversal=True), no negation.
            assert by_account[170]["debit"]["decimal"] == "0"
            assert by_account[170]["credit"]["decimal"] == "1000"
            assert by_account[171]["debit"]["decimal"] == "1000"
            assert by_account[171]["credit"]["decimal"] == "0"
            assert by_account[170]["origin"] == "account.move.line,1"
            assert by_account[171]["origin"] == "account.move.line,2"
            return httpx.Response(200, json={"id": 43, "state": "draft"})
        if path.endswith("/rest/model/account.move/43/post") and request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "id": 43,
                    "number": "0002",
                    "state": "posted",
                    "post_date": {"__class__": "date", "year": 2026, "month": 11, "day": 16},
                },
            )
        return httpx.Response(404, text="not found")

    return handler, seen


def test_adapter_reverse_journal_happy_path():
    handler, _seen = _fake_reverse_handler()
    adapter = TrytonAccountingAdapter(_mock_client(handler))
    ref = adapter.reverse_journal(ReverseJournal(PostedJournalRef(42)))
    assert ref == PostedJournalRef(43)


def test_adapter_reverse_journal_rejects_draft():
    adapter = TrytonAccountingAdapter(_mock_client(_fake_tryton_handler()))
    with pytest.raises(InvalidJournalError) as excinfo:
        adapter.reverse_journal(ReverseJournal(PostedJournalRef(42)))
    assert excinfo.value.code == "LEDGER_INVALID_JOURNAL"


def test_adapter_reverse_journal_missing_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    adapter = TrytonAccountingAdapter(_mock_client(handler))
    with pytest.raises(PostedJournalNotFoundError) as excinfo:
        adapter.reverse_journal(ReverseJournal(PostedJournalRef(999999)))
    assert excinfo.value.code == "LEDGER_JOURNAL_NOT_FOUND"


def test_adapter_reverse_journal_failed_post_keeps_draft_id_in_error():
    def handler(request: httpx.Request) -> httpx.Response:
        base, _seen = _fake_reverse_handler()
        path = request.url.path
        if path.endswith("/rest/model/account.move/43/post"):
            return httpx.Response(
                400,
                text='{"status": 400, "message": "To post move, you must balance all its lines.", "description": ""}',
            )
        return base(request)

    adapter = TrytonAccountingAdapter(_mock_client(handler))
    with pytest.raises(TrytonAdapterError) as excinfo:
        adapter.reverse_journal(ReverseJournal(PostedJournalRef(42)))
    assert excinfo.value.details["reversal_draft_id"] == 43
    assert excinfo.value.details["original_posted_id"] == 42


def test_adapter_get_journal_happy_path():
    adapter = TrytonAccountingAdapter(_mock_client(_fake_tryton_handler(move_state="posted")))
    view = adapter.get_journal(PostedJournalRef(42))
    assert view is not None
    assert view.number == "0001"
    assert view.post_date == FiscalDate.from_gregorian(datetime.date(2026, 11, 16))
    assert view.origin is None
    assert view.is_reversal is False
    assert view.is_balanced
    assert view.total_debit == MoneyIRR(1000)
    assert view.total_credit == MoneyIRR(1000)


def test_adapter_get_journal_missing_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    adapter = TrytonAccountingAdapter(_mock_client(handler))
    assert adapter.get_journal(PostedJournalRef(999999)) is None


def test_adapter_get_journal_rejects_draft():
    adapter = TrytonAccountingAdapter(_mock_client(_fake_tryton_handler()))
    with pytest.raises(InvalidJournalError):
        adapter.get_journal(PostedJournalRef(42))


def test_adapter_list_fiscal_years_and_periods():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/rest/model/account.fiscalyear"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 9,
                        "company": 2,
                        "name": "FY 2026",
                        "start_date": {"__class__": "date", "year": 2026, "month": 1, "day": 1},
                        "end_date": {"__class__": "date", "year": 2026, "month": 12, "day": 31},
                        "state": "open",
                    }
                ],
            )
        if path.endswith("/rest/model/account.period"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 11,
                        "name": "2026-11",
                        "start_date": {"__class__": "date", "year": 2026, "month": 11, "day": 1},
                        "end_date": {"__class__": "date", "year": 2026, "month": 11, "day": 30},
                        "state": "open",
                        "type": "standard",
                        "fiscalyear": 9,
                    }
                ],
            )
        return httpx.Response(404, text="not found")

    adapter = TrytonAccountingAdapter(_mock_client(handler))
    years = adapter.list_fiscal_years(CompanyRef(2))
    assert len(years) == 1
    assert years[0].name == "FY 2026"
    assert years[0].is_open
    periods = adapter.list_periods(CompanyRef(2), FiscalYearRef(9))
    assert len(periods) == 1
    assert periods[0].name == "2026-11"
    assert periods[0].ref is not None and periods[0].ref.engine_id == 11
    assert periods[0].fiscal_year is not None and periods[0].fiscal_year.engine_id == 9
    assert periods[0].period_type == "standard"
    assert periods[0].is_open


# ---------------------------------------------------------------------------
# US-202 follow-up: draft list + update
# ---------------------------------------------------------------------------


def _decode_b64url(value: str) -> Any:
    """Decode a base64url JSON query param (domain/order)."""
    padded = value + "=" * (-len(value) % 4)
    return json.loads(base64.urlsafe_b64decode(padded.encode()))


def _fake_list_handler():
    """A fake Tryton holding two draft moves (ids 42 + 43) of company 2."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/rest/model/account.move") and request.method == "GET":
            seen["domain"] = _decode_b64url(str(request.url.params["d"]))
            seen["order"] = _decode_b64url(str(request.url.params["o"]))
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 43,
                        "company": 2,
                        "journal": 9,
                        "period": 11,
                        "date": {"__class__": "date", "year": 2026, "month": 11, "day": 17},
                        "state": "draft",
                        "description": "second",
                    },
                    {
                        "id": 42,
                        "company": 2,
                        "journal": 9,
                        "period": 11,
                        "date": {"__class__": "date", "year": 2026, "month": 11, "day": 16},
                        "state": "draft",
                        "description": "first",
                    },
                ],
            )
        if path.endswith("/rest/model/account.move.line"):
            seen["line_domain"] = _decode_b64url(str(request.url.params["d"]))
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 1,
                        "move": 42,
                        "account": 170,
                        "debit": {"__class__": "Decimal", "decimal": "1000"},
                        "credit": {"__class__": "Decimal", "decimal": "0"},
                        "state": "valid",
                        "description": "",
                    },
                    {
                        "id": 2,
                        "move": 42,
                        "account": 171,
                        "debit": {"__class__": "Decimal", "decimal": "0"},
                        "credit": {"__class__": "Decimal", "decimal": "1000"},
                        "state": "valid",
                        "description": "",
                    },
                    {
                        "id": 3,
                        "move": 43,
                        "account": 170,
                        "debit": {"__class__": "Decimal", "decimal": "2000"},
                        "credit": {"__class__": "Decimal", "decimal": "0"},
                        "state": "valid",
                        "description": "",
                    },
                    {
                        "id": 4,
                        "move": 43,
                        "account": 171,
                        "debit": {"__class__": "Decimal", "decimal": "0"},
                        "credit": {"__class__": "Decimal", "decimal": "2000"},
                        "state": "valid",
                        "description": "",
                    },
                ],
            )
        if path.endswith("/rest/model/account.period"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 11,
                        "name": "2026-11",
                        "start_date": {"__class__": "date", "year": 2026, "month": 11, "day": 1},
                        "end_date": {"__class__": "date", "year": 2026, "month": 11, "day": 30},
                        "state": "open",
                    }
                ],
            )
        return httpx.Response(404, text="not found")

    return handler, seen


def test_adapter_list_journal_drafts_returns_company_drafts():
    handler, seen = _fake_list_handler()
    adapter = TrytonAccountingAdapter(_mock_client(handler))
    views = adapter.list_journal_drafts(CompanyRef(2))
    assert [view.ref.engine_id for view in views] == [43, 42]
    assert [view.description for view in views] == ["second", "first"]
    assert views[0].total_debit == MoneyIRR(2000)
    assert views[1].total_debit == MoneyIRR(1000)
    assert views[0].period.name == "2026-11"
    assert all(view.state == "draft" for view in views)
    # The search is company-scoped and draft-only, newest first.
    assert seen["domain"] == [["company", "=", 2], ["state", "=", "draft"]]
    assert seen["order"] == [["date", "DESC"], ["id", "DESC"]]
    # Lines are fetched in one batched search, not one per move.
    assert seen["line_domain"] == [["move", "in", [43, 42]]]


def test_adapter_list_journal_drafts_empty_when_none():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/rest/model/account.move"):
            return httpx.Response(200, json=[])
        return httpx.Response(404, text="not found")

    adapter = TrytonAccountingAdapter(_mock_client(handler))
    assert adapter.list_journal_drafts(CompanyRef(2)) == []


def _fake_update_handler(
    move_state: str = "draft",
    account_ids: tuple[int, ...] = (170, 171),
    line_reconciliation: int | None = None,
    period_state: str = "open",
):
    """A fake Tryton that applies PUT writes to its in-memory move 42."""
    seen: dict[str, Any] = {"puts": []}
    state: dict[str, Any] = {
        "description": "test",
        "date": {"__class__": "date", "year": 2026, "month": 11, "day": 16},
        "period": 11,
        "lines": [
            {
                "id": 1,
                "move": 42,
                "account": 170,
                "debit": {"__class__": "Decimal", "decimal": "1000"},
                "credit": {"__class__": "Decimal", "decimal": "0"},
                "state": "valid",
                "description": "",
                "reconciliation": line_reconciliation,
            },
            {
                "id": 2,
                "move": 42,
                "account": 171,
                "debit": {"__class__": "Decimal", "decimal": "0"},
                "credit": {"__class__": "Decimal", "decimal": "1000"},
                "state": "valid",
                "description": "",
                "reconciliation": line_reconciliation,
            },
        ],
        "next_line_id": 3,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/rest/model/account.move/42") and request.method == "PUT":
            body = json.loads(request.content)
            seen["puts"].append(body)
            if "description" in body:
                state["description"] = body["description"]
            if "date" in body:
                state["date"] = body["date"]
            if "period" in body:
                state["period"] = body["period"]
            if "lines" in body:
                for command in body["lines"]:
                    if command[0] == "delete":
                        doomed = set(command[1])
                        state["lines"] = [
                            line for line in state["lines"] if line["id"] not in doomed
                        ]
                    elif command[0] == "create":
                        for values in command[1]:
                            line = dict(values)
                            line["id"] = state["next_line_id"]
                            state["next_line_id"] += 1
                            line["move"] = 42
                            line["state"] = "valid"
                            line["reconciliation"] = None
                            state["lines"].append(line)
            return httpx.Response(200, json={"id": 42, "state": "draft"})
        if path.endswith("/rest/model/account.move/42"):
            return httpx.Response(
                200,
                json={
                    "id": 42,
                    "company": 2,
                    "journal": 9,
                    "period": state["period"],
                    "date": state["date"],
                    "state": move_state,
                    "description": state["description"],
                    "number": None,
                    "post_date": None,
                    "origin": None,
                },
            )
        if path.endswith("/rest/model/account.move.line"):
            return httpx.Response(200, json=list(state["lines"]))
        if path.endswith("/rest/model/account.account"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": account_id,
                        "closed": False,
                        "party_required": False,
                        "active": True,
                    }
                    for account_id in account_ids
                ],
            )
        if path.endswith("/rest/model/account.period"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 11,
                        "name": "2026-11",
                        "start_date": {"__class__": "date", "year": 2026, "month": 11, "day": 1},
                        "end_date": {"__class__": "date", "year": 2026, "month": 11, "day": 30},
                        "state": period_state,
                    }
                ],
            )
        if path.endswith("/rest/model/account.period/11"):
            return httpx.Response(
                200,
                json={
                    "id": 11,
                    "name": "2026-11",
                    "start_date": {"__class__": "date", "year": 2026, "month": 11, "day": 1},
                    "end_date": {"__class__": "date", "year": 2026, "month": 11, "day": 30},
                    "state": period_state,
                },
            )
        return httpx.Response(404, text="not found")

    return handler, seen


def test_adapter_update_draft_rewrites_lines_in_one_put():
    handler, seen = _fake_update_handler()
    adapter = TrytonAccountingAdapter(_mock_client(handler))
    updated = adapter.update_journal_draft(
        UpdateJournalDraft(
            draft=JournalDraftRef(42),
            description="renamed",
            date=FiscalDate.from_gregorian(datetime.date(2026, 11, 17)),
            lines=(_line(170, debit=2000), _line(171, credit=2000)),
        )
    )
    assert len(seen["puts"]) == 1
    body = seen["puts"][0]
    assert body["description"] == "renamed"
    assert body["date"] == {"__class__": "date", "year": 2026, "month": 11, "day": 17}
    # A date change carries its period in the same write.
    assert body["period"] == 11
    # Lines are rewritten wholesale: delete old, then create new.
    assert body["lines"][0] == ["delete", [1, 2]]
    assert body["lines"][1][0] == "create"
    created = body["lines"][1][1]
    assert created[0]["account"] == 170
    assert created[0]["debit"] == {"__class__": "Decimal", "decimal": "2000"}
    assert created[1]["account"] == 171
    assert created[1]["credit"] == {"__class__": "Decimal", "decimal": "2000"}
    # The returned view is the engine's read-back after the update.
    assert updated.ref.engine_id == 42
    assert updated.description == "renamed"
    assert updated.date == FiscalDate.from_gregorian(datetime.date(2026, 11, 17))
    assert updated.total_debit == updated.total_credit == MoneyIRR(2000)
    assert updated.is_balanced


def test_adapter_update_draft_description_only_sends_no_lines():
    handler, seen = _fake_update_handler()
    adapter = TrytonAccountingAdapter(_mock_client(handler))
    updated = adapter.update_journal_draft(
        UpdateJournalDraft(draft=JournalDraftRef(42), description="renamed")
    )
    assert len(seen["puts"]) == 1
    assert seen["puts"][0] == {"description": "renamed"}
    assert updated.description == "renamed"
    assert updated.total_debit == MoneyIRR(1000)


def test_adapter_update_draft_rejects_posted_without_put():
    handler, seen = _fake_update_handler(move_state="posted")
    adapter = TrytonAccountingAdapter(_mock_client(handler))
    with pytest.raises(JournalAlreadyPostedError) as excinfo:
        adapter.update_journal_draft(
            UpdateJournalDraft(draft=JournalDraftRef(42), description="renamed")
        )
    assert excinfo.value.code == "LEDGER_POSTED_IMMUTABLE"
    assert seen["puts"] == []


def test_adapter_update_draft_missing_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    adapter = TrytonAccountingAdapter(_mock_client(handler))
    with pytest.raises(JournalDraftNotFoundError) as excinfo:
        adapter.update_journal_draft(
            UpdateJournalDraft(draft=JournalDraftRef(999999), description="renamed")
        )
    assert excinfo.value.code == "LEDGER_JOURNAL_NOT_FOUND"


def test_adapter_update_draft_rejects_unknown_account_without_put():
    handler, seen = _fake_update_handler(account_ids=(171,))
    adapter = TrytonAccountingAdapter(_mock_client(handler))
    with pytest.raises(AccountNotFoundError) as excinfo:
        adapter.update_journal_draft(
            UpdateJournalDraft(
                draft=JournalDraftRef(42),
                lines=(_line(170, debit=2000), _line(171, credit=2000)),
            )
        )
    assert excinfo.value.code == "LEDGER_ACCOUNT_NOT_FOUND"
    assert seen["puts"] == []


def test_adapter_update_draft_rejects_reconciled_lines_without_put():
    handler, seen = _fake_update_handler(line_reconciliation=5)
    adapter = TrytonAccountingAdapter(_mock_client(handler))
    with pytest.raises(InvalidJournalError) as excinfo:
        adapter.update_journal_draft(
            UpdateJournalDraft(
                draft=JournalDraftRef(42),
                lines=(_line(170, debit=2000), _line(171, credit=2000)),
            )
        )
    assert excinfo.value.code == "LEDGER_INVALID_JOURNAL"
    assert "reconciled" in excinfo.value.message
    assert seen["puts"] == []


def test_adapter_update_draft_rejects_closed_period_lines_without_put():
    handler, seen = _fake_update_handler(period_state="closed")
    adapter = TrytonAccountingAdapter(_mock_client(handler))
    with pytest.raises(PeriodClosedError) as excinfo:
        adapter.update_journal_draft(
            UpdateJournalDraft(
                draft=JournalDraftRef(42),
                lines=(_line(170, debit=2000), _line(171, credit=2000)),
            )
        )
    assert excinfo.value.code == "LEDGER_PERIOD_CLOSED"
    assert seen["puts"] == []


def test_parse_move_origin():
    assert parse_move_origin("account.move,42") == PostedJournalRef(42)
    assert parse_move_origin(None) is None
    assert parse_move_origin("account.fiscalyear,9") is None
    assert parse_move_origin("not-a-reference") is None
    assert parse_move_origin("account.move,abc") is None


def test_client_400_json_error_body_is_sanitized():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            text='{"status": 400, "message": "You cannot modify posted move \\"0001\\".", "description": ""}',
        )

    client = _mock_client(handler)
    with pytest.raises(TrytonAdapterError) as excinfo:
        client.search("account.move", fields=["id"])
    assert excinfo.value.code == ENGINE_VALIDATION_FAILED
    assert '{"status"' not in str(excinfo.value)
    assert 'You cannot modify posted move "0001".' in str(excinfo.value)


# ---------------------------------------------------------------------------
# US-204: invoice drafts, payments, bank statements
# ---------------------------------------------------------------------------


def _invoice_line(
    account_id: int,
    quantity: str | int = 1,
    unit_price: int = 1000,
    description: str = "",
) -> InvoiceDraftLine:
    return InvoiceDraftLine(
        account=AccountRef(account_id),
        unit_price=MoneyIRR(unit_price),
        quantity=Decimal(quantity),
        description=description,
    )


def _invoice_command(lines, **kwargs) -> CreateInvoiceDraft:
    values: dict[str, Any] = {
        "company": CompanyRef(2),
        "invoice_type": "out",
        "party": PartyRef(5),
        "date": FiscalDate.from_gregorian(datetime.date(2026, 11, 16)),
        "lines": tuple(lines),
    }
    values.update(kwargs)
    return CreateInvoiceDraft(**values)


def test_invoice_command_totals():
    command = _invoice_command(
        [
            _invoice_line(371, quantity=1, unit_price=500_000),
            _invoice_line(372, quantity=2, unit_price=125_000),
        ]
    )
    assert command.total_amount == MoneyIRR(750_000)
    assert command.lines[0].amount == MoneyIRR(500_000)
    assert command.lines[1].amount == MoneyIRR(250_000)


def test_invoice_line_fractional_quantity_with_integral_amount():
    line = _invoice_line(371, quantity="2.5", unit_price=1000)
    assert line.amount == MoneyIRR(2500)


def test_invoice_command_rejects_bad_type():
    with pytest.raises(InvalidInvoiceError) as excinfo:
        _invoice_command([_invoice_line(371)], invoice_type="sideways")
    assert excinfo.value.code == "LEDGER_INVALID_INVOICE"


def test_invoice_command_rejects_no_lines():
    with pytest.raises(InvalidInvoiceError):
        _invoice_command([])


def test_invoice_command_rejects_zero_total():
    with pytest.raises(InvalidInvoiceError):
        _invoice_command([_invoice_line(371, unit_price=0)])


def test_invoice_line_rejects_fractional_amount():
    # 1.5 x 1 IRR = 1.5 IRR: fractional Rial amounts are never rounded.
    with pytest.raises(InvalidInvoiceError) as excinfo:
        _invoice_line(371, quantity="1.5", unit_price=1)
    assert excinfo.value.code == "LEDGER_INVALID_INVOICE"


def test_invoice_line_rejects_zero_quantity():
    with pytest.raises(InvalidInvoiceError):
        _invoice_line(371, quantity=0)


def test_invoice_line_rejects_negative_quantity():
    with pytest.raises(InvalidInvoiceError):
        _invoice_line(371, quantity="-1")


def test_invoice_line_rejects_non_finite_quantity():
    with pytest.raises(InvalidInvoiceError):
        InvoiceDraftLine(
            account=AccountRef(371),
            unit_price=MoneyIRR(1000),
            quantity=Decimal("NaN"),
        )


def test_invoice_line_rejects_negative_unit_price():
    with pytest.raises(InvalidInvoiceError):
        _invoice_line(371, unit_price=-100)


# ---------------------------------------------------------------------------
# US-204: quantity + invoice/payment/statement mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, Decimal(0)),
        (2, Decimal(2)),
        (1.0, Decimal("1.0")),
        (2.5, Decimal("2.5")),
        ("3.25", Decimal("3.25")),
        (Decimal("4"), Decimal("4")),
    ],
)
def test_quantity_from_tryton(value, expected):
    assert quantity_from_tryton(value) == expected


@pytest.mark.parametrize("value", [True, Decimal("NaN"), Decimal("Infinity"), "abc", None])
def test_quantity_from_tryton_rejects_bad_values(value):
    with pytest.raises((ValueError, TypeError)):
        quantity_from_tryton(value)


def test_quantity_to_tryton_uses_int_when_integral():
    assert quantity_to_tryton(Decimal("2")) == 2
    assert isinstance(quantity_to_tryton(Decimal("2")), int)
    assert quantity_to_tryton(Decimal("2.5")) == 2.5
    assert isinstance(quantity_to_tryton(Decimal("2.5")), float)


def test_parse_invoice_reference():
    assert parse_invoice_reference("account.invoice,7") == InvoiceRef(7)
    assert parse_invoice_reference(None) is None
    assert parse_invoice_reference("account.move,7") is None
    assert parse_invoice_reference("not-a-reference") is None
    assert parse_invoice_reference("account.invoice,abc") is None


def _invoice_record(**overrides) -> dict[str, Any]:
    record: dict[str, Any] = {
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
        "state": "draft",
        "number": None,
        "description": "test invoice",
        "reference": "REF-1",
        "untaxed_amount": Decimal("750000"),
        "tax_amount": Decimal("0"),
        "total_amount": Decimal("750000"),
    }
    record.update(overrides)
    return record


def test_invoice_record_to_view():
    line_records = [
        {
            "id": 1,
            "account": 371,
            "quantity": 1.0,
            "unit_price": Decimal("500000"),
            "amount": Decimal("500000"),
            "type": "line",
            "description": "Goods",
        },
    ]
    view = invoice_record_to_view(
        _invoice_record(),
        [invoice_line_record_to_view(r) for r in line_records],
        party_name="Acme Customer",
        currency_code="IRR",
    )
    assert view.ref == InvoiceRef(42)
    assert view.company == CompanyRef(2)
    assert view.invoice_type == "out"
    assert view.is_customer_invoice is True
    assert view.is_draft is True
    assert view.party == PartyRef(5)
    assert view.party_name == "Acme Customer"
    assert view.number is None  # drafts have no number yet
    assert view.invoice_date == FiscalDate.from_gregorian(datetime.date(2026, 11, 16))
    assert view.accounting_date is None
    assert view.journal == JournalRef(1)
    assert view.account == AccountRef(311)
    assert view.currency_code == "IRR"
    assert view.total_amount == MoneyIRR(750_000)
    assert len(view.lines) == 1
    assert view.lines[0].quantity == Decimal("1.0")
    assert view.lines[0].amount == MoneyIRR(500_000)


def test_invoice_record_to_view_without_journal():
    view = invoice_record_to_view(_invoice_record(journal=None), [], "Acme", "IRR")
    assert view.journal is None


def test_payment_record_to_view():
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
            "reference": "cash",
            "journal": 3,
            "currency": 1,
        },
        party_name="Acme Customer",
        journal_name="Cash Payments",
        currency_code="IRR",
    )
    assert view.ref == PaymentRef(9)
    assert view.number == "3"
    assert view.kind == "receivable"
    assert view.amount == MoneyIRR(500_000)
    assert view.is_draft is True
    assert view.journal_name == "Cash Payments"
    assert view.currency_code == "IRR"


def _statement_record(**overrides) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": 12,
        "company": 2,
        "name": "November statement",
        "journal": 4,
        "currency": 1,
        "date": datetime.date(2026, 11, 16),
        "state": "draft",
        "validation": "balance",
        "start_balance": Decimal("0"),
        "end_balance": Decimal("750000"),
        "balance": Decimal("750000"),
        "total_amount": None,
        "number_of_lines": None,
    }
    record.update(overrides)
    return record


def test_statement_record_to_view_balance_journal():
    line = statement_line_record_to_view(
        {
            "id": 1,
            "statement": 12,
            "date": datetime.date(2026, 11, 16),
            "amount": Decimal("750000"),
            "account": 311,
            "party": 5,
            "description": "Receipt",
            "number": "1",
            "related_to": "account.invoice,42",
        },
        {5: "Acme Customer"},
    )
    view = statement_record_to_view(_statement_record(), [line], "Main Bank", "IRR")
    assert view.ref == BankStatementRef(12)
    assert view.validation == "balance"
    assert view.start_balance == MoneyIRR(0)
    assert view.end_balance == MoneyIRR(750_000)
    assert view.balance == MoneyIRR(750_000)
    assert view.total_amount is None
    assert view.is_draft is True
    assert view.lines[0].related_invoice == InvoiceRef(42)
    assert view.lines[0].party_name == "Acme Customer"


def test_statement_record_to_view_amount_journal_has_no_balance():
    # An 'amount' journal's engine balance Function is 0; the view maps it
    # to None so "not applicable" stays distinct from a zero balance.
    view = statement_record_to_view(
        _statement_record(
            validation="amount",
            start_balance=None,
            end_balance=None,
            balance=Decimal("0"),
            total_amount=Decimal("100000"),
        ),
        [],
        "Main Bank",
        "IRR",
    )
    assert view.balance is None
    assert view.start_balance is None
    assert view.end_balance is None
    assert view.total_amount == MoneyIRR(100_000)


# ---------------------------------------------------------------------------
# US-204: adapter logic against a fake Tryton (MockTransport)
# ---------------------------------------------------------------------------

_FAKE_INVOICE_ACCOUNTS = {
    311: {"id": 311, "closed": False, "active": True, "type": 113},
    339: {"id": 339, "closed": False, "active": True, "type": 102},
    371: {"id": 371, "closed": False, "active": True, "type": 131},
    372: {"id": 372, "closed": False, "active": True, "type": 131},
}
_FAKE_INVOICE_ACCOUNT_TYPES = {
    113: {
        "id": 113,
        "receivable": True,
        "payable": False,
        "revenue": False,
        "expense": False,
        "debt": False,
    },
    102: {
        "id": 102,
        "receivable": False,
        "payable": True,
        "revenue": False,
        "expense": False,
        "debt": False,
    },
    131: {
        "id": 131,
        "receivable": False,
        "payable": False,
        "revenue": True,
        "expense": False,
        "debt": False,
    },
}
_FAKE_INVOICE_JOURNALS = {
    1: {"id": 1, "type": "revenue", "name": "Revenue"},
    2: {"id": 2, "type": "expense", "name": "Expense"},
}


def _request_domain(request: httpx.Request) -> list:
    raw = request.url.params.get("d")
    if not raw:
        return []
    decoded = json.loads(base64.urlsafe_b64decode(raw))
    return decoded if isinstance(decoded, list) else []


def _domain_matches(record: dict, domain: list, type_flags: dict | None = None) -> bool:
    """Tiny subset of Tryton domain matching, enough for the fake below."""
    for clause in domain:
        if not isinstance(clause, list) or len(clause) != 3:
            continue
        field_name, operator, operand = clause
        if field_name == "company":
            continue  # every fake record belongs to company 2
        if field_name in ("start_date", "end_date", "fiscalyear.company"):
            continue
        if field_name.startswith("type.") and type_flags is not None:
            flag = field_name.split(".", 1)[1]
            value = type_flags.get(record.get("type"), {}).get(flag)
        else:
            value = record.get(field_name)
        if operator == "=" and value != operand:
            return False
        if operator == "!=" and value == operand:
            return False
        if operator == "in" and value not in operand:
            return False
    return True


def _fake_invoice_tryton_handler(
    invoice_state: str = "draft",
    invoice_journal: int | None = 1,
    period_open: bool = True,
    party_exists: bool = True,
):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/rest/model/company.company/2"):
            return httpx.Response(200, json={"id": 2, "currency": 1})
        if path.endswith("/rest/model/currency.currency"):
            return httpx.Response(200, json=[{"id": 1, "code": "IRR"}])
        if path.endswith("/rest/model/party.party/5"):
            if not party_exists:
                return httpx.Response(404, text="not found")
            return httpx.Response(200, json={"id": 5, "name": "Acme Customer"})
        if path.endswith("/rest/model/party.party"):
            records = [
                r
                for r in [{"id": 5, "name": "Acme Customer"}]
                if _domain_matches(r, _request_domain(request))
            ]
            return httpx.Response(200, json=records)
        if path.endswith("/rest/model/party.address/7"):
            return httpx.Response(200, json={"id": 7, "party": 5})
        if path.endswith("/rest/model/party.address"):
            records = [
                r
                for r in [{"id": 7, "party": 5}]
                if _domain_matches(r, _request_domain(request))
            ]
            return httpx.Response(200, json=records)
        if path.endswith("/rest/model/account.account"):
            records = [
                r
                for r in _FAKE_INVOICE_ACCOUNTS.values()
                if _domain_matches(r, _request_domain(request), _FAKE_INVOICE_ACCOUNT_TYPES)
            ]
            return httpx.Response(200, json=records)
        if "/rest/model/account.account.type/" in path:
            type_id = int(path.rsplit("/", 1)[-1])
            type_record = _FAKE_INVOICE_ACCOUNT_TYPES.get(type_id)
            if type_record is None:
                return httpx.Response(404, text="not found")
            return httpx.Response(200, json=type_record)
        if path.endswith("/rest/model/account.journal"):
            records = [
                r
                for r in _FAKE_INVOICE_JOURNALS.values()
                if _domain_matches(r, _request_domain(request))
            ]
            return httpx.Response(200, json=records)
        if "/rest/model/account.journal/" in path:
            journal_id = int(path.rsplit("/", 1)[-1])
            journal = _FAKE_INVOICE_JOURNALS.get(journal_id)
            if journal is None:
                return httpx.Response(404, text="not found")
            return httpx.Response(200, json=journal)
        if path.endswith("/rest/model/account.period"):
            if not period_open:
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[{"id": 11, "name": "2026-11"}])
        if path.endswith("/rest/model/account.invoice") and request.method == "POST":
            body = json.loads(request.content)
            assert body["company"] == 2
            assert body["type"] == "out"
            assert body["party"] == 5
            assert body["invoice_address"] == 7
            assert body["currency"] == 1
            assert body["account"] == 311
            assert body["journal"] == 1
            assert body["lines"][0][0] == "create"  # One2Many command syntax
            return httpx.Response(200, json={"id": 42, "state": "draft"})
        if path.endswith("/rest/model/account.invoice/42"):
            return httpx.Response(
                200,
                json={
                    "id": 42,
                    "company": 2,
                    "type": "out",
                    "party": 5,
                    "invoice_address": 7,
                    "currency": 1,
                    "account": 311,
                    "journal": invoice_journal,
                    "invoice_date": {"__class__": "date", "year": 2026, "month": 11, "day": 16},
                    "accounting_date": None,
                    "state": invoice_state,
                    "number": None,
                    "description": "test invoice",
                    "reference": "REF-1",
                    "untaxed_amount": {"__class__": "Decimal", "decimal": "750000"},
                    "tax_amount": {"__class__": "Decimal", "decimal": "0"},
                    "total_amount": {"__class__": "Decimal", "decimal": "750000"},
                },
            )
        if path.endswith("/rest/model/account.invoice"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 42,
                        "company": 2,
                        "type": "out",
                        "party": 5,
                        "invoice_address": 7,
                        "currency": 1,
                        "account": 311,
                        "journal": 1,
                        "invoice_date": {
                            "__class__": "date",
                            "year": 2026,
                            "month": 11,
                            "day": 16,
                        },
                        "accounting_date": None,
                        "state": "draft",
                        "number": None,
                        "description": "test invoice",
                        "reference": "REF-1",
                        "untaxed_amount": {"__class__": "Decimal", "decimal": "750000"},
                        "tax_amount": {"__class__": "Decimal", "decimal": "0"},
                        "total_amount": {"__class__": "Decimal", "decimal": "750000"},
                    }
                ],
            )
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
        if path.endswith("/rest/model/account.payment/9"):
            return httpx.Response(
                200,
                json={
                    "id": 9,
                    "company": 2,
                    "number": "3",
                    "kind": "receivable",
                    "party": 5,
                    "date": {"__class__": "date", "year": 2026, "month": 11, "day": 16},
                    "amount": {"__class__": "Decimal", "decimal": "500000"},
                    "state": "draft",
                    "reference": "cash",
                    "journal": 3,
                    "currency": 1,
                },
            )
        if path.endswith("/rest/model/account.payment"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 9,
                        "company": 2,
                        "number": "3",
                        "kind": "receivable",
                        "party": 5,
                        "date": {"__class__": "date", "year": 2026, "month": 11, "day": 16},
                        "amount": {"__class__": "Decimal", "decimal": "500000"},
                        "state": "draft",
                        "reference": "cash",
                        "journal": 3,
                        "currency": 1,
                    }
                ],
            )
        if path.endswith("/rest/model/account.payment.journal"):
            return httpx.Response(200, json=[{"id": 3, "name": "Cash Payments"}])
        if path.endswith("/rest/model/account.statement/12"):
            return httpx.Response(
                200,
                json={
                    "id": 12,
                    "company": 2,
                    "name": "November statement",
                    "journal": 4,
                    "currency": 1,
                    "date": {"__class__": "date", "year": 2026, "month": 11, "day": 16},
                    "state": "draft",
                    "validation": "balance",
                    "start_balance": {"__class__": "Decimal", "decimal": "0"},
                    "end_balance": {"__class__": "Decimal", "decimal": "750000"},
                    "balance": {"__class__": "Decimal", "decimal": "750000"},
                    "total_amount": None,
                    "number_of_lines": None,
                },
            )
        if path.endswith("/rest/model/account.statement"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 12,
                        "company": 2,
                        "name": "November statement",
                        "journal": 4,
                        "currency": 1,
                        "date": {"__class__": "date", "year": 2026, "month": 11, "day": 16},
                        "state": "draft",
                        "validation": "balance",
                        "start_balance": {"__class__": "Decimal", "decimal": "0"},
                        "end_balance": {"__class__": "Decimal", "decimal": "750000"},
                        "balance": {"__class__": "Decimal", "decimal": "750000"},
                        "total_amount": None,
                        "number_of_lines": None,
                    }
                ],
            )
        if path.endswith("/rest/model/account.statement.line"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 1,
                        "statement": 12,
                        "date": {"__class__": "date", "year": 2026, "month": 11, "day": 16},
                        "amount": {"__class__": "Decimal", "decimal": "750000"},
                        "account": 311,
                        "party": 5,
                        "description": "Receipt",
                        "number": "1",
                        "related_to": "account.invoice,42",
                    }
                ],
            )
        if path.endswith("/rest/model/account.statement.journal"):
            return httpx.Response(200, json=[{"id": 4, "name": "Main Bank"}])
        return httpx.Response(404, text="not found")

    return handler


def _invoice_adapter(**handler_kwargs) -> TrytonAccountingAdapter:
    return TrytonAccountingAdapter(_mock_client(_fake_invoice_tryton_handler(**handler_kwargs)))


def test_adapter_create_invoice_draft_happy_path():
    adapter = _invoice_adapter()
    command = _invoice_command(
        [_invoice_line(371, quantity=1, unit_price=500_000)],
        account=AccountRef(311),
        journal=JournalRef(1),
        invoice_address=PartyAddressRef(7),
    )
    assert adapter.create_invoice_draft(command) == InvoiceRef(42)


def test_adapter_create_invoice_draft_resolves_account_journal_address():
    # No explicit account / journal / address: the fake engine has exactly
    # one receivable account, one revenue journal and one party address.
    adapter = _invoice_adapter()
    command = _invoice_command([_invoice_line(371, quantity=1, unit_price=500_000)])
    assert adapter.create_invoice_draft(command) == InvoiceRef(42)


def test_adapter_create_invoice_draft_rejects_unknown_party():
    adapter = _invoice_adapter(party_exists=False)
    command = _invoice_command(
        [_invoice_line(371, quantity=1, unit_price=500_000)],
        account=AccountRef(311),
        journal=JournalRef(1),
    )
    with pytest.raises(PartyNotFoundError) as excinfo:
        adapter.create_invoice_draft(command)
    assert excinfo.value.code == "LEDGER_PARTY_NOT_FOUND"


def test_adapter_create_invoice_draft_rejects_wrong_account_type():
    # A payable account cannot back a customer ('out') invoice.
    adapter = _invoice_adapter()
    command = _invoice_command(
        [_invoice_line(371, quantity=1, unit_price=500_000)],
        account=AccountRef(339),
        journal=JournalRef(1),
    )
    with pytest.raises(InvalidInvoiceError) as excinfo:
        adapter.create_invoice_draft(command)
    assert excinfo.value.code == "LEDGER_INVALID_INVOICE"


def test_adapter_create_invoice_draft_rejects_wrong_direction_line_account():
    # A payable account (not a revenue account) on a customer invoice
    # line violates the engine's direction domain.
    adapter = _invoice_adapter()
    command = _invoice_command(
        [_invoice_line(339, quantity=1, unit_price=500_000)],
        account=AccountRef(311),
        journal=JournalRef(1),
    )
    with pytest.raises(InvalidInvoiceError) as excinfo:
        adapter.create_invoice_draft(command)
    assert excinfo.value.code == "LEDGER_INVALID_INVOICE"


def test_adapter_create_invoice_draft_rejects_line_on_invoice_account():
    adapter = _invoice_adapter()
    command = _invoice_command(
        [_invoice_line(311, quantity=1, unit_price=500_000)],
        account=AccountRef(311),
        journal=JournalRef(1),
    )
    with pytest.raises(InvalidInvoiceError) as excinfo:
        adapter.create_invoice_draft(command)
    assert excinfo.value.code == "LEDGER_INVALID_INVOICE"


def test_adapter_create_invoice_draft_rejects_unknown_line_account():
    adapter = _invoice_adapter()
    command = _invoice_command(
        [_invoice_line(999_999, quantity=1, unit_price=500_000)],
        account=AccountRef(311),
        journal=JournalRef(1),
    )
    with pytest.raises(AccountNotFoundError) as excinfo:
        adapter.create_invoice_draft(command)
    assert excinfo.value.code == "LEDGER_ACCOUNT_NOT_FOUND"


def test_adapter_validate_invoice_draft_ok():
    adapter = _invoice_adapter()
    summary = adapter.validate_invoice_draft(InvoiceRef(42))
    assert summary.valid is True
    assert summary.issues == ()


def test_adapter_validate_invoice_draft_flags_missing_journal():
    adapter = _invoice_adapter(invoice_journal=None)
    summary = adapter.validate_invoice_draft(InvoiceRef(42))
    assert summary.valid is False
    assert "LEDGER_INVALID_INVOICE" in summary.codes()
    assert any(issue.field == "journal" for issue in summary.issues)


def test_adapter_validate_invoice_draft_flags_closed_period():
    adapter = _invoice_adapter(period_open=False)
    summary = adapter.validate_invoice_draft(InvoiceRef(42))
    assert summary.valid is False
    assert "LEDGER_PERIOD_CLOSED" in summary.codes()


def test_adapter_validate_invoice_draft_flags_validated_state():
    adapter = _invoice_adapter(invoice_state="validated")
    summary = adapter.validate_invoice_draft(InvoiceRef(42))
    assert summary.valid is False
    assert "LEDGER_INVALID_INVOICE" in summary.codes()
    assert any(issue.field == "state" for issue in summary.issues)


def test_adapter_validate_missing_invoice_raises_stable_error():
    adapter = _invoice_adapter()
    with pytest.raises(InvoiceNotFoundError) as excinfo:
        adapter.validate_invoice_draft(InvoiceRef(999_999_999))
    assert excinfo.value.code == "LEDGER_INVOICE_NOT_FOUND"


def test_adapter_get_invoice_happy_path():
    adapter = _invoice_adapter()
    view = adapter.get_invoice(InvoiceRef(42))
    assert view is not None
    assert view.ref == InvoiceRef(42)
    assert view.party_name == "Acme Customer"
    assert view.currency_code == "IRR"
    assert view.total_amount == MoneyIRR(750_000)
    assert len(view.lines) == 2
    assert view.lines[1].quantity == Decimal("2.0")


def test_adapter_get_invoice_missing_returns_none():
    adapter = _invoice_adapter()
    assert adapter.get_invoice(InvoiceRef(999_999_999)) is None


def test_adapter_list_invoices_rejects_bad_filters():
    adapter = _invoice_adapter()
    with pytest.raises(InvalidInvoiceError):
        adapter.list_invoices(CompanyRef(2), invoice_type="sideways")
    with pytest.raises(InvalidInvoiceError):
        adapter.list_invoices(CompanyRef(2), state="limbo")


def test_adapter_list_invoices_returns_views():
    adapter = _invoice_adapter()
    views = adapter.list_invoices(CompanyRef(2))
    assert len(views) == 1
    assert views[0].ref == InvoiceRef(42)
    assert views[0].party_name == "Acme Customer"
    assert len(views[0].lines) == 2


def test_adapter_get_payment_happy_path():
    adapter = _invoice_adapter()
    view = adapter.get_payment(PaymentRef(9))
    assert view is not None
    assert view.ref == PaymentRef(9)
    assert view.number == "3"
    assert view.kind == "receivable"
    assert view.amount == MoneyIRR(500_000)
    assert view.party_name == "Acme Customer"
    assert view.journal_name == "Cash Payments"
    assert view.currency_code == "IRR"


def test_adapter_get_payment_missing_returns_none():
    adapter = _invoice_adapter()
    assert adapter.get_payment(PaymentRef(999_999_999)) is None


def test_adapter_list_payments_rejects_bad_filters():
    adapter = _invoice_adapter()
    with pytest.raises(InvalidPaymentError):
        adapter.list_payments(CompanyRef(2), kind="sideways")
    with pytest.raises(InvalidPaymentError):
        adapter.list_payments(CompanyRef(2), state="limbo")


def test_adapter_get_bank_statement_happy_path():
    adapter = _invoice_adapter()
    view = adapter.get_bank_statement(BankStatementRef(12))
    assert view is not None
    assert view.ref == BankStatementRef(12)
    assert view.name == "November statement"
    assert view.journal_name == "Main Bank"
    assert view.currency_code == "IRR"
    assert view.start_balance == MoneyIRR(0)
    assert view.end_balance == MoneyIRR(750_000)
    assert view.balance == MoneyIRR(750_000)
    assert len(view.lines) == 1
    assert view.lines[0].related_invoice == InvoiceRef(42)
    assert view.lines[0].party_name == "Acme Customer"


def test_adapter_get_bank_statement_missing_returns_none():
    adapter = _invoice_adapter()
    assert adapter.get_bank_statement(BankStatementRef(999_999_999)) is None


def test_adapter_list_bank_statements_rejects_bad_filter():
    adapter = _invoice_adapter()
    with pytest.raises(InvalidBankStatementError):
        adapter.list_bank_statements(CompanyRef(2), state="limbo")


def test_adapter_exposes_no_statement_workflow():
    # US-514: the adapter creates statements (import) and writes match
    # results onto draft lines (reconcile), but never validates, posts,
    # deletes or wizard-reconciles them — those stay with the engine.
    adapter = _invoice_adapter()
    assert hasattr(adapter, "create_bank_statement")
    assert hasattr(adapter, "update_bank_statement_line")
    assert hasattr(adapter, "list_statement_journals")
    for name in (
        "update_bank_statement",
        "delete_bank_statement",
        "validate_bank_statement",
        "post_bank_statement",
        "reconcile_bank_statement",
    ):
        assert not hasattr(adapter, name), f"adapter must not expose {name}"
    for name in (
        "create_payment",
        "submit_payment",
        "approve_payment",
        "process_payment",
        "succeed_payment",
        "fail_payment",
    ):
        assert hasattr(adapter, name), f"US-502: adapter must expose {name}"


# ---------------------------------------------------------------------------
# Static architecture guards
# ---------------------------------------------------------------------------

_FORBIDDEN_DOMAIN_IMPORTS = ("tryton", "httpx", "sqlalchemy", "psycopg", "requests")
_FORBIDDEN_ADAPTER_IMPORTS = ("psycopg", "sqlalchemy")
_FORBIDDEN_ADAPTER_STRINGS = (
    "SELECT ",
    "INSERT INTO",
    "UPDATE ",
    "DELETE FROM",
    "jsonrpc",
    "xmlrpc",
    "/rpc/",
)


def _iter_python_files(package: Path):
    return sorted(p for p in package.rglob("*.py") if p.is_file())


def _imported_top_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    return modules


def test_domain_accounting_has_no_tryton_http_or_sql_imports():
    """ARCHITECTURE.md §5.1: domain must not import Tryton/HTTP/SQLAlchemy."""
    package = REPO_ROOT / "src" / "hesabyar" / "domain" / "accounting"
    assert package.is_dir()
    for path in _iter_python_files(package):
        imported = _imported_top_modules(path)
        forbidden = imported & set(_FORBIDDEN_DOMAIN_IMPORTS)
        assert not forbidden, f"{path.relative_to(REPO_ROOT)} imports {sorted(forbidden)}"


def _code_string_literals(path: Path) -> list[str]:
    """All string literals in a file except module/class/function docstrings."""
    tree = ast.parse(path.read_text(), filename=str(path))
    docstrings: set[int] = set()

    def mark_docstring(node: ast.AST) -> None:
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))

    for node in ast.walk(tree):
        mark_docstring(node)
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def test_tryton_adapter_has_no_sql_or_rpc():
    """ADR 0002: the adapter reaches Tryton only through the REST API.

    Docstrings may name the JSON-RPC protocol module (where the shared wire
    format lives); executable code and string literals may not use it.
    """
    package = REPO_ROOT / "src" / "hesabyar" / "infrastructure" / "accounting"
    assert package.is_dir()
    for path in _iter_python_files(package):
        imported = _imported_top_modules(path)
        forbidden = imported & set(_FORBIDDEN_ADAPTER_IMPORTS)
        assert not forbidden, f"{path.relative_to(REPO_ROOT)} imports {sorted(forbidden)}"
        for literal in _code_string_literals(path):
            for needle in _FORBIDDEN_ADAPTER_STRINGS:
                assert needle not in literal, (
                    f"{path.relative_to(REPO_ROOT)} string literal contains {needle!r}"
                )


# ---------------------------------------------------------------------------
# US-511: invoice-account fallback (party → configuration default → unique)
# ---------------------------------------------------------------------------


def _invoice_account_handler(
    *,
    party_receivable: int | None = None,
    config_receivable: int | None = None,
    accounts: dict[int, dict] | None = None,
    created_bodies: list[dict],
):
    """Fake Tryton for create_invoice_draft, parameterized by the fallback inputs."""
    records = dict(accounts) if accounts is not None else dict(_FAKE_INVOICE_ACCOUNTS)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/rest/model/company.company/2"):
            return httpx.Response(200, json={"id": 2, "currency": 1})
        if path.endswith("/rest/model/currency.currency"):
            return httpx.Response(200, json=[{"id": 1, "code": "IRR"}])
        if path.endswith("/rest/model/party.party/5"):
            return httpx.Response(
                200,
                json={
                    "id": 5,
                    "name": "Acme Customer",
                    "account_receivable": party_receivable,
                },
            )
        if path.endswith("/rest/model/party.address"):
            return httpx.Response(200, json=[{"id": 7, "party": 5}])
        if path.endswith("/rest/model/account.configuration/1"):
            return httpx.Response(
                200,
                json={"id": 1, "default_account_receivable": config_receivable},
            )
        if path.endswith("/rest/model/account.account"):
            matched = [
                r
                for r in records.values()
                if _domain_matches(
                    r, _request_domain(request), _FAKE_INVOICE_ACCOUNT_TYPES
                )
            ]
            return httpx.Response(200, json=matched)
        if "/rest/model/account.account.type/" in path:
            type_id = int(path.rsplit("/", 1)[-1])
            type_record = _FAKE_INVOICE_ACCOUNT_TYPES.get(type_id)
            if type_record is None:
                return httpx.Response(404, text="not found")
            return httpx.Response(200, json=type_record)
        if path.endswith("/rest/model/account.journal"):
            return httpx.Response(
                200, json=[{"id": 1, "type": "revenue", "name": "Revenue"}]
            )
        if path.endswith("/rest/model/account.invoice") and request.method == "POST":
            created_bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"id": 42, "state": "draft"})
        return httpx.Response(404, text="not found")

    return handler


_AMBIGUOUS_CHART = {
    311: {"id": 311, "closed": False, "active": True, "type": 113},
    312: {"id": 312, "closed": False, "active": True, "type": 113},
    371: {"id": 371, "closed": False, "active": True, "type": 131},
    372: {"id": 372, "closed": False, "active": True, "type": 131},
}


def _create_draft_with_fallback(
    *, account=None, party_receivable=None, config_receivable=None, accounts=None
):
    """Create one invoice draft against the fake; return the created body."""
    created_bodies: list[dict] = []
    client = _mock_client(
        _invoice_account_handler(
            party_receivable=party_receivable,
            config_receivable=config_receivable,
            accounts=accounts,
            created_bodies=created_bodies,
        )
    )
    adapter = TrytonAccountingAdapter(client)
    command = _invoice_command([_invoice_line(371)], account=account)
    ref = adapter.create_invoice_draft(command)
    assert ref.engine_id == 42
    assert len(created_bodies) == 1
    return created_bodies[0]


def test_invoice_account_explicit_account_wins_over_defaults():
    body = _create_draft_with_fallback(
        account=AccountRef(312),
        party_receivable=311,
        config_receivable=311,
        accounts=_AMBIGUOUS_CHART,
    )
    assert body["account"] == 312


def test_invoice_account_uses_party_default_on_ambiguous_chart():
    body = _create_draft_with_fallback(
        party_receivable=312,
        config_receivable=311,
        accounts=_AMBIGUOUS_CHART,
    )
    assert body["account"] == 312


def test_invoice_account_uses_config_default_on_ambiguous_chart():
    body = _create_draft_with_fallback(
        config_receivable=311,
        accounts=_AMBIGUOUS_CHART,
    )
    assert body["account"] == 311


def test_invoice_account_fails_on_ambiguous_chart_without_defaults():
    created_bodies: list[dict] = []
    client = _mock_client(
        _invoice_account_handler(
            accounts=_AMBIGUOUS_CHART, created_bodies=created_bodies
        )
    )
    adapter = TrytonAccountingAdapter(client)
    command = _invoice_command([_invoice_line(371)])
    with pytest.raises(InvalidInvoiceError) as excinfo:
        adapter.create_invoice_draft(command)
    assert excinfo.value.code == "LEDGER_INVALID_INVOICE"
    assert created_bodies == []


def test_invoice_account_uses_the_unique_account_without_defaults():
    body = _create_draft_with_fallback()
    assert body["account"] == 311


def test_invoice_account_rejects_a_closed_config_default():
    closed_chart = {
        311: {"id": 311, "closed": True, "active": True, "type": 113},
        371: {"id": 371, "closed": False, "active": True, "type": 131},
        372: {"id": 372, "closed": False, "active": True, "type": 131},
    }
    created_bodies: list[dict] = []
    client = _mock_client(
        _invoice_account_handler(
            config_receivable=311, accounts=closed_chart, created_bodies=created_bodies
        )
    )
    adapter = TrytonAccountingAdapter(client)
    command = _invoice_command([_invoice_line(371)])
    with pytest.raises(InvalidInvoiceError) as excinfo:
        adapter.create_invoice_draft(command)
    assert excinfo.value.code == "LEDGER_INVALID_INVOICE"
    assert "closed" in excinfo.value.message
    assert created_bodies == []


def test_invoice_account_rejects_a_wrong_type_config_default():
    created_bodies: list[dict] = []
    client = _mock_client(
        _invoice_account_handler(
            config_receivable=371, accounts=_AMBIGUOUS_CHART, created_bodies=created_bodies
        )
    )
    adapter = TrytonAccountingAdapter(client)
    command = _invoice_command([_invoice_line(371)])
    with pytest.raises(InvalidInvoiceError) as excinfo:
        adapter.create_invoice_draft(command)
    assert excinfo.value.code == "LEDGER_INVALID_INVOICE"
    assert "not a receivable account" in excinfo.value.message
    assert created_bodies == []


# ---------------------------------------------------------------------------
# US-511: validate_invoice_draft is polarity-aware (credit notes)
# ---------------------------------------------------------------------------


def _credit_note_validation_adapter(*, lines, untaxed, tax, total):
    """Adapter whose invoice 42 carries the given lines and totals."""
    base = _fake_invoice_tryton_handler()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/rest/model/account.invoice/42"):
            return httpx.Response(
                200,
                json={
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
                    "state": "draft",
                    "number": None,
                    "description": "test invoice",
                    "reference": "REF-1",
                    "untaxed_amount": {"__class__": "Decimal", "decimal": str(untaxed)},
                    "tax_amount": {"__class__": "Decimal", "decimal": str(tax)},
                    "total_amount": {"__class__": "Decimal", "decimal": str(total)},
                },
            )
        if path.endswith("/rest/model/account.invoice.line"):
            return httpx.Response(200, json=lines)
        return base(request)

    return TrytonAccountingAdapter(_mock_client(handler))


def _validation_line(line_id, account_id, quantity, unit_price, amount):
    return {
        "id": line_id,
        "invoice": 42,
        "account": account_id,
        "quantity": quantity,
        "unit_price": {"__class__": "Decimal", "decimal": str(unit_price)},
        "amount": {"__class__": "Decimal", "decimal": str(amount)},
        "type": "line",
        "description": "Goods",
    }


_CREDIT_NOTE_LINES = [
    _validation_line(1, 371, -1.0, 500_000, -500_000),
    _validation_line(2, 372, -2.0, 125_000, -250_000),
]


def test_adapter_validate_invoice_draft_accepts_a_credit_note():
    adapter = _credit_note_validation_adapter(
        lines=_CREDIT_NOTE_LINES, untaxed=-750_000, tax=0, total=-750_000
    )
    summary = adapter.validate_invoice_draft(InvoiceRef(42))
    assert summary.valid is True
    assert summary.issues == ()


def test_adapter_validate_invoice_draft_flags_mixed_sign_lines():
    adapter = _credit_note_validation_adapter(
        lines=[
            _validation_line(1, 371, -1.0, 500_000, -500_000),
            _validation_line(2, 372, 2.0, 125_000, 250_000),
        ],
        untaxed=-250_000,
        tax=0,
        total=-250_000,
    )
    summary = adapter.validate_invoice_draft(InvoiceRef(42))
    assert summary.valid is False
    assert any(
        issue.field == "lines" and "mixed" in issue.message for issue in summary.issues
    )


def test_adapter_validate_invoice_draft_flags_a_zero_total():
    adapter = _credit_note_validation_adapter(
        lines=[_validation_line(1, 371, -1.0, 500_000, -500_000)],
        untaxed=-500_000,
        tax=500_000,
        total=0,
    )
    summary = adapter.validate_invoice_draft(InvoiceRef(42))
    assert summary.valid is False
    assert any(issue.field == "total" for issue in summary.issues)


def test_adapter_validate_invoice_draft_flags_a_positive_credit_note_total():
    adapter = _credit_note_validation_adapter(
        lines=_CREDIT_NOTE_LINES, untaxed=-750_000, tax=0, total=750_000
    )
    summary = adapter.validate_invoice_draft(InvoiceRef(42))
    assert summary.valid is False
    assert any(issue.field == "total" for issue in summary.issues)


def test_adapter_validate_invoice_draft_flags_a_zero_quantity_line():
    adapter = _credit_note_validation_adapter(
        lines=[_validation_line(1, 371, 0.0, 500_000, 0)],
        untaxed=0,
        tax=0,
        total=0,
    )
    summary = adapter.validate_invoice_draft(InvoiceRef(42))
    assert summary.valid is False
    assert any(
        issue.field == "lines" and "zero quantity" in issue.message
        for issue in summary.issues
    )
