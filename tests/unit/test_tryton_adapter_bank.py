"""Unit tests for the adapter's US-514 bank methods (MockTransport).

Covers create_bank_statement, update_bank_statement_line and
list_statement_journals against a fake Tryton — no live server.
"""

from __future__ import annotations

import base64
import datetime
import json
from typing import Any

import httpx
import pytest

from hesabyar.domain.accounting.commands import (
    CreateBankStatement,
    CreateBankStatementLine,
    UpdateBankStatementLine,
)
from hesabyar.domain.accounting.refs import (
    AccountRef,
    BankStatementLineRef,
    BankStatementRef,
    CompanyRef,
    InvoiceRef,
    PartyRef,
    StatementJournalRef,
)
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.errors import (
    InvalidBankStatementError,
    InvoiceNotFoundError,
    PartyNotFoundError,
)
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.infrastructure.accounting.tryton.adapter import TrytonAccountingAdapter
from hesabyar.infrastructure.accounting.tryton.client import TrytonRestClient

BASE_URL = "http://tryton.test:8000"
DATABASE = "hesabyar_tryton_test"

COMPANY_ID = 2
JOURNAL_ID = 4
ACCOUNT_ID = 900
PARTY_ID = 5
INVOICE_ID = 42
STATEMENT_ID = 12
LINE_ID = 77


def _request_domain(request: httpx.Request) -> list:
    raw = request.url.params.get("d")
    if not raw:
        return []
    decoded = json.loads(base64.urlsafe_b64decode(raw))
    return decoded if isinstance(decoded, list) else []


def _domain_value(domain: list, field: str):
    for clause in domain:
        if isinstance(clause, list) and len(clause) == 3 and clause[0] == field:
            return clause[2]
    return None


def _bank_handler(
    *,
    invoice_state: str = "posted",
    invoice_account: int = ACCOUNT_ID,
    statement_state: str = "draft",
    account_closed: bool = False,
    journal_exists: bool = True,
    party_exists: bool = True,
    invoice_exists: bool = True,
    bank_account_id: int | None = 8,
):
    """A fake Tryton serving the bank endpoints. Captures the last create/write."""
    captured: dict[str, Any] = {}

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
        if path.endswith("/rest/model/account.account"):
            wanted = _domain_value(_request_domain(request), "id")
            if isinstance(wanted, list):
                records = [
                    {"id": aid, "closed": account_closed, "active": True} for aid in wanted
                ]
            else:
                records = [{"id": ACCOUNT_ID, "closed": account_closed, "active": True}]
            return httpx.Response(200, json=records)
        if path.endswith("/rest/model/account.invoice/42"):
            if not invoice_exists:
                return httpx.Response(404, text="not found")
            return httpx.Response(
                200,
                json={
                    "id": 42,
                    "company": COMPANY_ID,
                    "account": invoice_account,
                    "state": invoice_state,
                },
            )
        if path.endswith("/rest/model/account.statement.journal"):
            if not journal_exists:
                return httpx.Response(200, json=[])
            return httpx.Response(
                200,
                json=[
                    {
                        "id": JOURNAL_ID,
                        "company": COMPANY_ID,
                        "name": "Main Bank",
                        "journal": 9,
                        "currency": 1,
                        "validation": "balance",
                        "account": 100,
                        "bank_account": bank_account_id,
                    }
                ],
            )
        if path.endswith("/rest/model/bank.account.number"):
            return httpx.Response(
                200,
                json=[{"id": 1, "account": 8, "number": "IR062060000000100324200001"}],
            )
        if path.endswith("/rest/model/account.statement/12"):
            return httpx.Response(
                200, json={"id": 12, "company": COMPANY_ID, "state": statement_state}
            )
        if path.endswith("/rest/model/account.statement.line/77"):
            if request.method == "PUT":
                captured["write"] = json.loads(request.content)
                return httpx.Response(200, json={"id": 77})
            return httpx.Response(
                200,
                json={
                    "id": 77,
                    "statement": STATEMENT_ID,
                    "account": ACCOUNT_ID,
                    "party": None,
                    "related_to": None,
                },
            )
        if path.endswith("/rest/model/account.statement") and request.method == "POST":
            captured["create"] = json.loads(request.content)
            return httpx.Response(200, json={"id": 55, "state": "draft"})
        return httpx.Response(404, text="not found")

    return handler, captured


def _bank_adapter(**handler_kwargs):
    handler, captured = _bank_handler(**handler_kwargs)
    client = TrytonRestClient(
        BASE_URL, DATABASE, "test-key", transport=httpx.MockTransport(handler)
    )
    return TrytonAccountingAdapter(client), captured


def _create_command(**overrides) -> CreateBankStatement:
    base = {
        "company": CompanyRef(COMPANY_ID),
        "journal": StatementJournalRef(JOURNAL_ID),
        "name": "Melli 2026-11-17 [abcd1234]",
        "date": FiscalDate(datetime.date(2026, 11, 17)),
        "lines": (
            CreateBankStatementLine(
                date=FiscalDate(datetime.date(2026, 11, 17)),
                amount=MoneyIRR(1_000_000),
                account=AccountRef(ACCOUNT_ID),
                description="واریز مشتری",
                number="REF-1",
                party=PartyRef(PARTY_ID),
            ),
            CreateBankStatementLine(
                date=FiscalDate(datetime.date(2026, 11, 17)),
                amount=MoneyIRR(-250_000),
                account=AccountRef(ACCOUNT_ID),
                description="پرداخت تأمین‌کننده",
            ),
        ),
        "start_balance": MoneyIRR(4_000_000),
        "end_balance": MoneyIRR(4_750_000),
    }
    base.update(overrides)
    return CreateBankStatement(**base)


# -- create_bank_statement ---------------------------------------------------------


def test_create_bank_statement_happy_path() -> None:
    adapter, captured = _bank_adapter()
    ref = adapter.create_bank_statement(_create_command())
    assert ref == BankStatementRef(55)
    payload = captured["create"]
    assert payload["company"] == COMPANY_ID
    assert payload["journal"] == JOURNAL_ID
    assert payload["name"] == "Melli 2026-11-17 [abcd1234]"
    # Decimals travel as Tryton's Decimal dict on the wire.
    assert payload["start_balance"] == {"__class__": "Decimal", "decimal": "4000000"}
    assert payload["end_balance"] == {"__class__": "Decimal", "decimal": "4750000"}
    create_op, lines = payload["lines"][0]
    assert create_op == "create"
    assert len(lines) == 2
    assert lines[0]["amount"] == {"__class__": "Decimal", "decimal": "1000000"}
    assert lines[0]["number"] == "REF-1"
    assert lines[0]["party"] == PARTY_ID
    assert lines[1]["amount"] == {"__class__": "Decimal", "decimal": "-250000"}
    assert "number" not in lines[1]


def test_create_bank_statement_links_posted_invoice() -> None:
    adapter, captured = _bank_adapter()
    command = _create_command(
        lines=(
            CreateBankStatementLine(
                date=FiscalDate(datetime.date(2026, 11, 17)),
                amount=MoneyIRR(1_000_000),
                account=AccountRef(ACCOUNT_ID),
                description="واریز مشتری",
                related_invoice=InvoiceRef(INVOICE_ID),
            ),
        )
    )
    adapter.create_bank_statement(command)
    lines = captured["create"]["lines"][0][1]
    assert lines[0]["related_to"] == "account.invoice,42"


def test_create_bank_statement_rejects_unknown_journal() -> None:
    adapter, _ = _bank_adapter(journal_exists=False)
    with pytest.raises(InvalidBankStatementError, match="journal"):
        adapter.create_bank_statement(_create_command())


def test_create_bank_statement_rejects_closed_account() -> None:
    adapter, _ = _bank_adapter(account_closed=True)
    with pytest.raises(InvalidBankStatementError, match="closed"):
        adapter.create_bank_statement(_create_command())


def test_create_bank_statement_rejects_unknown_party() -> None:
    adapter, _ = _bank_adapter(party_exists=False)
    with pytest.raises(PartyNotFoundError):
        adapter.create_bank_statement(_create_command())


def test_create_bank_statement_rejects_draft_invoice_link() -> None:
    adapter, _ = _bank_adapter(invoice_state="draft")
    command = _create_command(
        lines=(
            CreateBankStatementLine(
                date=FiscalDate(datetime.date(2026, 11, 17)),
                amount=MoneyIRR(1_000_000),
                account=AccountRef(ACCOUNT_ID),
                description="واریز",
                related_invoice=InvoiceRef(INVOICE_ID),
            ),
        )
    )
    with pytest.raises(InvalidBankStatementError, match="not posted"):
        adapter.create_bank_statement(command)


def test_create_bank_statement_rejects_invoice_account_mismatch() -> None:
    adapter, _ = _bank_adapter(invoice_account=501)
    command = _create_command(
        lines=(
            CreateBankStatementLine(
                date=FiscalDate(datetime.date(2026, 11, 17)),
                amount=MoneyIRR(1_000_000),
                account=AccountRef(ACCOUNT_ID),
                description="واریز",
                related_invoice=InvoiceRef(INVOICE_ID),
            ),
        )
    )
    with pytest.raises(InvalidBankStatementError, match="must match"):
        adapter.create_bank_statement(command)


def test_create_bank_statement_rejects_missing_invoice() -> None:
    adapter, _ = _bank_adapter(invoice_exists=False)
    command = _create_command(
        lines=(
            CreateBankStatementLine(
                date=FiscalDate(datetime.date(2026, 11, 17)),
                amount=MoneyIRR(1_000_000),
                account=AccountRef(ACCOUNT_ID),
                description="واریز",
                related_invoice=InvoiceRef(INVOICE_ID),
            ),
        )
    )
    with pytest.raises(InvoiceNotFoundError):
        adapter.create_bank_statement(command)


# -- update_bank_statement_line -------------------------------------------------------


def test_update_line_sets_invoice_link() -> None:
    adapter, captured = _bank_adapter()
    adapter.update_bank_statement_line(
        UpdateBankStatementLine(
            statement=BankStatementRef(STATEMENT_ID),
            line=BankStatementLineRef(LINE_ID),
            account=AccountRef(ACCOUNT_ID),
            party=PartyRef(PARTY_ID),
            related_invoice=InvoiceRef(INVOICE_ID),
        )
    )
    payload = captured["write"]
    assert payload["related_to"] == "account.invoice,42"
    assert payload["account"] == ACCOUNT_ID
    assert payload["party"] == PARTY_ID


def test_update_line_party_only() -> None:
    adapter, captured = _bank_adapter()
    adapter.update_bank_statement_line(
        UpdateBankStatementLine(
            statement=BankStatementRef(STATEMENT_ID),
            line=BankStatementLineRef(LINE_ID),
            party=PartyRef(PARTY_ID),
        )
    )
    assert captured["write"] == {"party": PARTY_ID}


def test_update_line_clear_invoice_link() -> None:
    adapter, captured = _bank_adapter()
    adapter.update_bank_statement_line(
        UpdateBankStatementLine(
            statement=BankStatementRef(STATEMENT_ID),
            line=BankStatementLineRef(LINE_ID),
            clear_related_invoice=True,
        )
    )
    assert captured["write"] == {"related_to": None}


def test_update_line_rejects_validated_statement() -> None:
    adapter, _ = _bank_adapter(statement_state="validated")
    with pytest.raises(InvalidBankStatementError, match="not a draft"):
        adapter.update_bank_statement_line(
            UpdateBankStatementLine(
                statement=BankStatementRef(STATEMENT_ID),
                line=BankStatementLineRef(LINE_ID),
                party=PartyRef(PARTY_ID),
            )
        )


def test_update_line_rejects_draft_invoice_link() -> None:
    adapter, _ = _bank_adapter(invoice_state="draft")
    with pytest.raises(InvalidBankStatementError, match="not posted"):
        adapter.update_bank_statement_line(
            UpdateBankStatementLine(
                statement=BankStatementRef(STATEMENT_ID),
                line=BankStatementLineRef(LINE_ID),
                related_invoice=InvoiceRef(INVOICE_ID),
            )
        )


# -- list_statement_journals ------------------------------------------------------------


def test_list_statement_journals_resolves_shaba() -> None:
    adapter, _ = _bank_adapter()
    journals = adapter.list_statement_journals(CompanyRef(COMPANY_ID))
    assert len(journals) == 1
    journal = journals[0]
    assert journal.id == JOURNAL_ID
    assert journal.name == "Main Bank"
    assert journal.account.engine_id == 100
    assert journal.validation == "balance"
    assert journal.currency_code == "IRR"
    assert journal.bank_account_number == "IR062060000000100324200001"


def test_list_statement_journals_without_bank_account() -> None:
    adapter, _ = _bank_adapter(bank_account_id=None)
    journals = adapter.list_statement_journals(CompanyRef(COMPANY_ID))
    assert journals[0].bank_account_number is None


def test_list_statement_journals_empty() -> None:
    adapter, _ = _bank_adapter(journal_exists=False)
    assert adapter.list_statement_journals(CompanyRef(COMPANY_ID)) == []


# -- command invariants -------------------------------------------------------------------


def test_create_bank_statement_requires_lines() -> None:
    with pytest.raises(InvalidBankStatementError, match="at least one line"):
        _create_command(lines=())


def test_update_line_requires_a_change() -> None:
    with pytest.raises(InvalidBankStatementError, match="at least one changed field"):
        UpdateBankStatementLine(
            statement=BankStatementRef(STATEMENT_ID),
            line=BankStatementLineRef(LINE_ID),
        )
