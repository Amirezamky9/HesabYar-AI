"""Unit tests for the cash-box adapter methods (US-516).

No live server is needed: the HTTP layer is exercised with httpx
MockTransport. The contract tests
(``tests/contract/test_tryton_adapter_cashbox.py``) cover the same
methods against the real engine.
"""

from __future__ import annotations

import base64
import datetime
import json

import httpx
import pytest

from hesabyar.domain.accounting.refs import AccountRef, CompanyRef, JournalDraftRef, JournalRef
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.errors import (
    AccountNotFoundError,
    InvalidInvoiceError,
    InvalidJournalError,
    JournalAlreadyPostedError,
    JournalDraftNotFoundError,
)
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.infrastructure.accounting.tryton.adapter import TrytonAccountingAdapter
from hesabyar.infrastructure.accounting.tryton.client import TrytonRestClient
from hesabyar.infrastructure.accounting.tryton.errors import (
    ENGINE_ERROR,
    ENGINE_NOT_FOUND,
    ENGINE_VALIDATION_FAILED,
    TrytonAdapterError,
)

BASE_URL = "http://tryton.test:8000"
DATABASE = "hesabyar_tryton_test"
COMPANY = CompanyRef(1)
CASH_ACCOUNT = AccountRef(35)
AS_OF = FiscalDate(datetime.date(2026, 11, 18))


def _mock_client(handler) -> TrytonRestClient:
    return TrytonRestClient(BASE_URL, DATABASE, "test-key", transport=httpx.MockTransport(handler))


def _dec(value: int) -> dict[str, object]:
    """A Tryton wire decimal."""
    return {"__class__": "Decimal", "decimal": str(value)}


def _decode_param(value: str) -> object:
    """Decode a base64url-encoded JSON query param (domain / order)."""
    padded = value + "=" * (-len(value) % 4)
    return json.loads(base64.urlsafe_b64decode(padded.encode()))


def _irr_company_handler(*, currency_code: str = "IRR", company_exists: bool = True):
    """Handler fragment: company.company read + currency.currency search."""

    def respond(request: httpx.Request) -> httpx.Response | None:
        path = request.url.path
        if path.endswith("/rest/model/company.company/1"):
            if not company_exists:
                return httpx.Response(404, text="not found")
            return httpx.Response(200, json={"id": 1, "currency": 7})
        if path.endswith("/rest/model/currency.currency"):
            return httpx.Response(200, json=[{"id": 7, "code": currency_code}])
        return None

    return respond


# ---------------------------------------------------------------------------
# get_account_balance
# ---------------------------------------------------------------------------


def test_get_account_balance_aggregates_posted_lines_up_to_as_of():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        company_response = _irr_company_handler()(request)
        if company_response is not None:
            return company_response
        path = request.url.path
        if path.endswith("/rest/model/account.account/35"):
            return httpx.Response(200, json={"id": 35, "company": 1, "code": "1.1.1", "name": "Cash"})
        if path.endswith("/rest/model/account.move.line"):
            seen["domain"] = _decode_param(request.url.params["d"])
            seen["limit"] = request.url.params.get("s")
            return httpx.Response(
                200,
                json=[
                    {"id": 1, "debit": _dec(6_000_000), "credit": _dec(0)},
                    {"id": 2, "debit": _dec(0), "credit": _dec(1_000_000)},
                ],
            )
        return httpx.Response(404, text="not found")

    adapter = TrytonAccountingAdapter(_mock_client(handler))
    balance = adapter.get_account_balance(COMPANY, CASH_ACCOUNT, AS_OF)
    assert balance.code == "1.1.1"
    assert balance.name == "Cash"
    assert balance.as_of == AS_OF
    assert balance.total_debit.amount == 6_000_000
    assert balance.total_credit.amount == 1_000_000
    assert balance.balance.amount == 5_000_000
    assert balance.line_count == 2
    # Posted moves only, the account, the company, on or before as_of.
    assert ["account", "=", 35] in seen["domain"]
    assert ["move.company", "=", 1] in seen["domain"]
    assert ["move.state", "=", "posted"] in seen["domain"]
    assert ["move.date", "<=", {"__class__": "date", "year": 2026, "month": 11, "day": 18}] in seen["domain"]
    assert seen["limit"] == "500"


def test_get_account_balance_pages_through_the_full_line_set():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        company_response = _irr_company_handler()(request)
        if company_response is not None:
            return company_response
        path = request.url.path
        if path.endswith("/rest/model/account.account/35"):
            return httpx.Response(200, json={"id": 35, "company": 1, "code": "1.1.1", "name": "Cash"})
        if path.endswith("/rest/model/account.move.line"):
            offset = int(request.url.params.get("p", 0))
            calls.append(offset)
            if offset == 0:
                # A full page: the adapter must keep paging.
                return httpx.Response(
                    200,
                    json=[{"id": i, "debit": _dec(1_000), "credit": _dec(0)} for i in range(500)],
                )
            return httpx.Response(200, json=[{"id": 501, "debit": _dec(1_000), "credit": _dec(0)}])
        return httpx.Response(404, text="not found")

    adapter = TrytonAccountingAdapter(_mock_client(handler))
    balance = adapter.get_account_balance(COMPANY, CASH_ACCOUNT, AS_OF)
    assert calls == [0, 500]
    assert balance.line_count == 501
    assert balance.total_debit.amount == 501_000


def test_get_account_balance_unknown_account_raises_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        company_response = _irr_company_handler()(request)
        if company_response is not None:
            return company_response
        if request.url.path.endswith("/rest/model/account.account/35"):
            return httpx.Response(404, text="not found")
        return httpx.Response(404, text="not found")

    adapter = TrytonAccountingAdapter(_mock_client(handler))
    with pytest.raises(AccountNotFoundError) as excinfo:
        adapter.get_account_balance(COMPANY, CASH_ACCOUNT, AS_OF)
    assert excinfo.value.code == "LEDGER_ACCOUNT_NOT_FOUND"


def test_get_account_balance_account_of_another_company_raises_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        company_response = _irr_company_handler()(request)
        if company_response is not None:
            return company_response
        if request.url.path.endswith("/rest/model/account.account/35"):
            return httpx.Response(200, json={"id": 35, "company": 2, "code": "1.1.1", "name": "Cash"})
        return httpx.Response(404, text="not found")

    adapter = TrytonAccountingAdapter(_mock_client(handler))
    with pytest.raises(AccountNotFoundError):
        adapter.get_account_balance(COMPANY, CASH_ACCOUNT, AS_OF)


def test_get_account_balance_non_irr_company_is_refused():
    def handler(request: httpx.Request) -> httpx.Response:
        company_response = _irr_company_handler(currency_code="USD")(request)
        if company_response is not None:
            return company_response
        return httpx.Response(404, text="not found")

    adapter = TrytonAccountingAdapter(_mock_client(handler))
    with pytest.raises(TrytonAdapterError) as excinfo:
        adapter.get_account_balance(COMPANY, CASH_ACCOUNT, AS_OF)
    assert excinfo.value.code == ENGINE_VALIDATION_FAILED


def test_get_account_balance_unknown_company_raises_engine_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        company_response = _irr_company_handler(company_exists=False)(request)
        if company_response is not None:
            return company_response
        return httpx.Response(404, text="not found")

    adapter = TrytonAccountingAdapter(_mock_client(handler))
    with pytest.raises(TrytonAdapterError) as excinfo:
        adapter.get_account_balance(COMPANY, CASH_ACCOUNT, AS_OF)
    assert excinfo.value.code == ENGINE_NOT_FOUND


# ---------------------------------------------------------------------------
# list_statement_journals
# ---------------------------------------------------------------------------


def test_list_statement_journals_maps_views_with_batch_reads():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/rest/model/account.statement.journal"):
            seen["domain"] = _decode_param(request.url.params["d"])
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 10,
                        "company": 1,
                        "name": "صندوق فروشگاه",
                        "journal": 110,
                        "account": 35,
                        "currency": 7,
                        "validation": "balance",
                        "active": True,
                    },
                    {
                        "id": 11,
                        "company": 1,
                        "name": "تنخواه",
                        "journal": 111,
                        "account": 36,
                        "currency": 7,
                        "validation": "balance",
                        "active": False,
                    },
                ],
            )
        if path.endswith("/rest/model/account.account"):
            seen["account_domain"] = _decode_param(request.url.params["d"])
            return httpx.Response(
                200,
                json=[
                    {"id": 35, "code": "1.1.1", "name": "Cash"},
                    {"id": 36, "code": "1.1.5", "name": "Petty Cash"},
                ],
            )
        if path.endswith("/rest/model/currency.currency"):
            return httpx.Response(200, json=[{"id": 7, "code": "IRR"}])
        return httpx.Response(404, text="not found")

    adapter = TrytonAccountingAdapter(_mock_client(handler))
    journals = adapter.list_statement_journals(COMPANY)
    assert [journal.ref.engine_id for journal in journals] == [10, 11]
    store, petty = journals
    assert store.name == "صندوق فروشگاه"
    assert store.account.engine_id == 35
    assert store.account_code == "1.1.1"
    assert store.account_name == "Cash"
    assert store.currency_code == "IRR"
    assert store.validation == "balance"
    assert store.active is True
    assert petty.active is False
    assert petty.account_code == "1.1.5"
    assert ["company", "=", 1] in seen["domain"]
    # One batch read for both accounts.
    assert ["id", "in", [35, 36]] in seen["account_domain"]


def test_list_statement_journals_empty_when_the_company_has_none():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/rest/model/account.statement.journal"):
            return httpx.Response(200, json=[])
        return httpx.Response(404, text="not found")

    adapter = TrytonAccountingAdapter(_mock_client(handler))
    assert adapter.list_statement_journals(COMPANY) == []


def test_list_statement_journals_missing_account_is_an_engine_error():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/rest/model/account.statement.journal"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 10,
                        "company": 1,
                        "name": "صندوق فروشگاه",
                        "journal": 110,
                        "account": 35,
                        "currency": 7,
                        "validation": "balance",
                        "active": True,
                    }
                ],
            )
        if path.endswith("/rest/model/account.account"):
            return httpx.Response(200, json=[])  # the account is gone
        return httpx.Response(404, text="not found")

    adapter = TrytonAccountingAdapter(_mock_client(handler))
    with pytest.raises(TrytonAdapterError) as excinfo:
        adapter.list_statement_journals(COMPANY)
    assert excinfo.value.code == ENGINE_ERROR


# ---------------------------------------------------------------------------
# delete_journal_draft
# ---------------------------------------------------------------------------


def test_delete_journal_draft_deletes_a_draft_move():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/rest/model/account.move/42"):
            if request.method == "GET":
                return httpx.Response(200, json={"id": 42, "state": "draft"})
            if request.method == "DELETE":
                seen["deleted"] = True
                return httpx.Response(200, json={})
        return httpx.Response(404, text="not found")

    adapter = TrytonAccountingAdapter(_mock_client(handler))
    adapter.delete_journal_draft(JournalDraftRef(42))
    assert seen["deleted"] is True


def test_delete_journal_draft_unknown_draft_raises_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    adapter = TrytonAccountingAdapter(_mock_client(handler))
    with pytest.raises(JournalDraftNotFoundError) as excinfo:
        adapter.delete_journal_draft(JournalDraftRef(42))
    assert excinfo.value.code == "LEDGER_JOURNAL_NOT_FOUND"


def test_delete_journal_draft_posted_move_is_refused():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/rest/model/account.move/42"):
            return httpx.Response(200, json={"id": 42, "state": "posted"})
        return httpx.Response(404, text="not found")

    adapter = TrytonAccountingAdapter(_mock_client(handler))
    with pytest.raises(JournalAlreadyPostedError) as excinfo:
        adapter.delete_journal_draft(JournalDraftRef(42))
    assert excinfo.value.code == "LEDGER_POSTED_IMMUTABLE"


def test_delete_journal_draft_concurrent_delete_raises_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/rest/model/account.move/42"):
            if request.method == "GET":
                return httpx.Response(200, json={"id": 42, "state": "draft"})
            if request.method == "DELETE":
                return httpx.Response(404, text="not found")
        return httpx.Response(404, text="not found")

    adapter = TrytonAccountingAdapter(_mock_client(handler))
    with pytest.raises(JournalDraftNotFoundError):
        adapter.delete_journal_draft(JournalDraftRef(42))


# ---------------------------------------------------------------------------
# get_general_journal
# ---------------------------------------------------------------------------


def _general_journal_handler(journals: list[dict], *, company_name: str = "Acme"):
    """Handler: IRR company (with a name) + account.journal search."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/rest/model/company.company/1"):
            return httpx.Response(200, json={"id": 1, "currency": 7, "rec_name": company_name})
        if path.endswith("/rest/model/currency.currency"):
            return httpx.Response(200, json=[{"id": 7, "code": "IRR"}])
        if path.endswith("/rest/model/account.journal"):
            return httpx.Response(200, json=journals)
        return httpx.Response(404, text="not found")

    return handler


def test_get_general_journal_returns_the_only_general_journal():
    adapter = TrytonAccountingAdapter(_mock_client(_general_journal_handler([{"id": 4, "name": "Miscellaneous"}])))
    assert adapter.get_general_journal(COMPANY) == JournalRef(4)


def test_get_general_journal_prefers_the_company_named_journal():
    journals = [
        {"id": 4, "name": "Stock"},
        {"id": 5, "name": "Miscellaneous"},
        {"id": 9, "name": "Acme General Journal"},
    ]
    adapter = TrytonAccountingAdapter(_mock_client(_general_journal_handler(journals)))
    assert adapter.get_general_journal(COMPANY) == JournalRef(9)


def test_get_general_journal_ambiguous_without_a_company_named_journal():
    journals = [
        {"id": 4, "name": "Stock"},
        {"id": 5, "name": "Miscellaneous"},
    ]
    adapter = TrytonAccountingAdapter(_mock_client(_general_journal_handler(journals)))
    with pytest.raises(InvalidJournalError) as excinfo:
        adapter.get_general_journal(COMPANY)
    assert excinfo.value.code == "LEDGER_INVALID_JOURNAL"


def test_get_general_journal_with_no_general_journal_raises():
    adapter = TrytonAccountingAdapter(_mock_client(_general_journal_handler([])))
    with pytest.raises(InvalidJournalError) as excinfo:
        adapter.get_general_journal(COMPANY)
    assert excinfo.value.code == "LEDGER_INVALID_JOURNAL"


def test_get_general_journal_unknown_company_raises_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/rest/model/company.company/1"):
            return httpx.Response(404, text="not found")
        return httpx.Response(404, text="not found")

    adapter = TrytonAccountingAdapter(_mock_client(handler))
    with pytest.raises(TrytonAdapterError) as excinfo:
        adapter.get_general_journal(COMPANY)
    assert excinfo.value.code == ENGINE_NOT_FOUND


# ---------------------------------------------------------------------------
# get_account_by_code
# ---------------------------------------------------------------------------


def test_get_account_by_code_returns_the_matching_account():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        company_response = _irr_company_handler()(request)
        if company_response is not None:
            return company_response
        path = request.url.path
        if path.endswith("/rest/model/account.account"):
            seen["domain"] = _decode_param(request.url.params["d"])
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 149,
                        "code": "1.2.6",
                        "name": "Cash Shortage and Overage",
                        "type": 19,
                        "parent": 140,
                        "closed": False,
                        "party_required": False,
                        "active": True,
                    }
                ],
            )
        if path.endswith("/rest/model/account.account.type"):
            return httpx.Response(200, json=[{"id": 19, "name": "Receivable"}])
        return httpx.Response(404, text="not found")

    adapter = TrytonAccountingAdapter(_mock_client(handler))
    view = adapter.get_account_by_code(COMPANY, "1.2.6")
    assert view is not None
    assert view.ref.engine_id == 149
    assert view.code == "1.2.6"
    assert view.name == "Cash Shortage and Overage"
    assert view.type_name == "Receivable"
    assert view.is_postable is True
    # One targeted search: the company and the code, nothing else.
    assert ["company", "=", 1] in seen["domain"]
    assert ["code", "=", "1.2.6"] in seen["domain"]


def test_get_account_by_code_returns_none_when_missing():
    def handler(request: httpx.Request) -> httpx.Response:
        company_response = _irr_company_handler()(request)
        if company_response is not None:
            return company_response
        if request.url.path.endswith("/rest/model/account.account"):
            return httpx.Response(200, json=[])
        return httpx.Response(404, text="not found")

    adapter = TrytonAccountingAdapter(_mock_client(handler))
    assert adapter.get_account_by_code(COMPANY, "9.9.9") is None


def test_get_account_by_code_rejects_a_non_irr_company():
    def handler(request: httpx.Request) -> httpx.Response:
        company_response = _irr_company_handler(currency_code="USD")(request)
        if company_response is not None:
            return company_response
        return httpx.Response(404, text="not found")

    adapter = TrytonAccountingAdapter(_mock_client(handler))
    with pytest.raises(TrytonAdapterError) as excinfo:
        adapter.get_account_by_code(COMPANY, "1.2.6")
    assert excinfo.value.code == ENGINE_VALIDATION_FAILED


# ---------------------------------------------------------------------------
# Company IRR check memoization
# ---------------------------------------------------------------------------


def test_company_irr_check_is_read_once_per_adapter():
    reads: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/rest/model/company.company/" in path:
            reads.append(path)
            return httpx.Response(200, json={"id": 1, "currency": 7, "rec_name": "Test Co"})
        if path.endswith("/rest/model/currency.currency"):
            return httpx.Response(200, json=[{"id": 7, "code": "IRR"}])
        if path.endswith("/rest/model/account.account"):
            return httpx.Response(200, json=[])
        return httpx.Response(404, text="not found")

    adapter = TrytonAccountingAdapter(_mock_client(handler))
    assert adapter.get_account_by_code(COMPANY, "1.2.6") is None
    assert adapter.get_account_by_code(COMPANY, "1.1.1") is None
    # Two reads, one company validation: the second call reuses the memo.
    assert len(reads) == 1


# ---------------------------------------------------------------------------
# list_invoice_totals
# ---------------------------------------------------------------------------


def test_list_invoice_totals_returns_date_and_total_only():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/rest/model/account.invoice"):
            seen["domain"] = _decode_param(request.url.params["d"])
            seen["fields"] = request.url.params.get_list("f")
            return httpx.Response(
                200,
                json=[
                    {"id": 11, "invoice_date": "2026-11-12", "total_amount": "1500000"},
                    {"id": 12, "invoice_date": None, "total_amount": "7000000"},
                ],
            )
        return httpx.Response(404, text="not found")

    adapter = TrytonAccountingAdapter(_mock_client(handler))
    totals = adapter.list_invoice_totals(COMPANY, invoice_type="out", state="posted")
    assert [t.ref.engine_id for t in totals] == [11, 12]
    assert totals[0].invoice_date == FiscalDate(datetime.date(2026, 11, 12))
    assert totals[0].total_amount == MoneyIRR(1_500_000)
    assert totals[1].invoice_date is None
    # One search, date + total only — no lines, parties, or currencies.
    assert set(seen["fields"]) == {"invoice_date", "total_amount"}
    assert ["type", "=", "out"] in seen["domain"]
    assert ["state", "=", "posted"] in seen["domain"]


def test_list_invoice_totals_rejects_bad_filters():
    adapter = TrytonAccountingAdapter(_mock_client(lambda request: httpx.Response(404)))
    with pytest.raises(InvalidInvoiceError):
        adapter.list_invoice_totals(COMPANY, invoice_type="sideways")
    with pytest.raises(InvalidInvoiceError):
        adapter.list_invoice_totals(COMPANY, state="limbo")
