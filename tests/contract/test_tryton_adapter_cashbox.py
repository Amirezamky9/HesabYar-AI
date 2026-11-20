"""Contract tests for the US-516 adapter methods (Phase 5 wave 2, K2).

These tests run against the LIVE trytond server and the
``hesabyar_tryton_test`` database, using ONLY the dedicated test company
"HesabYar Adapter Test" and the statement journals provisioned by
``~/workspace/coord-logs/us516-provision-cashboxes.py`` (looked up BY
NAME — numeric engine ids are never hardcoded).

Covered: ``get_account_balance`` (posted moves only, as-of date,
cross-checked against an independent ``account.move.line`` search, a
fresh DRAFT move never changes the figures), ``list_statement_journals``
(the two provisioned cash-box daybooks), and ``delete_journal_draft``
(draft deleted, unknown draft refused, posted move refused).

The Tryton connection comes from AppSettings (HESABYAR_TRYTON__* env,
i.e. the Supabase trytond on :8010).

When the server is not reachable (or the test company / statement
journals have not been provisioned yet), every test SKIPS cleanly with
a clear message.
"""

from __future__ import annotations

import datetime
import os
from collections.abc import Generator
from decimal import Decimal

import pytest

from hesabyar.domain.accounting.commands import CreateJournalDraft, JournalDraftLine
from hesabyar.domain.accounting.refs import AccountRef, CompanyRef, JournalDraftRef, JournalRef
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.errors import (
    AccountNotFoundError,
    JournalAlreadyPostedError,
    JournalDraftNotFoundError,
)
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.infrastructure.accounting.tryton.adapter import TrytonAccountingAdapter
from hesabyar.infrastructure.accounting.tryton.client import TrytonRestClient
from hesabyar.infrastructure.config import AppSettings

TEST_COMPANY_NAME = os.environ.get("TRYTON_TEST_COMPANY", "HesabYar Adapter Test")
TEST_JOURNAL_NAME = "HesabYar Adapter Test General Journal"
STORE_JOURNAL_NAME = "HesabYar US-516 Store Cash Box"
PETTY_JOURNAL_NAME = "HesabYar US-516 Petty Cash Box"

CASH_ACCOUNT_CODE = "1.1.1"
PETTY_ACCOUNT_CODE = "1.1.5"
OTHER_ACCOUNT_CODE = "1.1.2"

# A date inside FY 2026's open November period.
MOVE_DATE = FiscalDate.from_gregorian(datetime.date(2026, 11, 18))

MISSING_ID = 999_999_999


@pytest.fixture(scope="module")
def client() -> Generator[TrytonRestClient, None, None]:
    t = AppSettings().tryton
    rest_client = TrytonRestClient(t.base_url, t.database, t.api_key)
    if not rest_client.ping():
        rest_client.close()
        pytest.skip(
            f"trytond server not reachable at {t.base_url} "
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
        f"Test company {TEST_COMPANY_NAME!r} not found in the Tryton DB. "
        "Provision it with setup_adapter_test_company.py (see docs/adapter.md)."
    )


@pytest.fixture(scope="module")
def journal(client: TrytonRestClient) -> JournalRef:
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
    return JournalRef(int(records[0]["id"]))


@pytest.fixture(scope="module")
def cash_account(adapter: TrytonAccountingAdapter, company: CompanyRef) -> AccountRef:
    chart = {view.code: view for view in adapter.get_chart_of_accounts(company)}
    missing = [c for c in (CASH_ACCOUNT_CODE, PETTY_ACCOUNT_CODE, OTHER_ACCOUNT_CODE) if c not in chart]
    if missing:
        pytest.skip(f"Chart accounts missing from test company: {missing}.")
    return chart[CASH_ACCOUNT_CODE].ref


@pytest.fixture(scope="module")
def other_account(adapter: TrytonAccountingAdapter, company: CompanyRef) -> AccountRef:
    chart = {view.code: view for view in adapter.get_chart_of_accounts(company)}
    return chart[OTHER_ACCOUNT_CODE].ref


@pytest.fixture()
def created_drafts(client: TrytonRestClient) -> Generator[list[int], None, None]:
    """Track draft moves a test creates and delete them afterwards."""
    ids: list[int] = []
    yield ids
    for move_id in ids:
        try:
            record = client.read("account.move", move_id, fields=["state"])
        except Exception:
            continue
        if record is not None and record.get("state") == "draft":
            client.delete("account.move", move_id)


def _independent_balance(
    client: TrytonRestClient, company: CompanyRef, account: AccountRef, as_of: FiscalDate
) -> tuple[int, int, int]:
    """Sum posted move lines for one account up to a date, directly."""
    total_debit = Decimal("0")
    total_credit = Decimal("0")
    count = 0
    offset = 0
    while True:
        page = client.search(
            "account.move.line",
            domain=[
                ("account", "=", account.engine_id),
                ("move.company", "=", company.engine_id),
                ("move.state", "=", "posted"),
                ("move.date", "<=", as_of.gregorian_date.isoformat()),
            ],
            fields=["debit", "credit"],
            limit=500,
            offset=offset,
            context={"company": company.engine_id},
        )
        for line in page:
            total_debit += Decimal(str(line.get("debit", 0)))
            total_credit += Decimal(str(line.get("credit", 0)))
        count += len(page)
        if len(page) < 500:
            break
        offset += len(page)
    return int(total_debit), int(total_credit), count


# ---------------------------------------------------------------------------
# list_statement_journals
# ---------------------------------------------------------------------------


def test_list_statement_journals_returns_provisioned_boxes(
    adapter: TrytonAccountingAdapter, company: CompanyRef
) -> None:
    journals = {j.name: j for j in adapter.list_statement_journals(company)}
    missing = [n for n in (STORE_JOURNAL_NAME, PETTY_JOURNAL_NAME) if n not in journals]
    if missing:
        pytest.skip(
            f"Statement journals missing from the test company: {missing}. "
            "Provision them with ~/workspace/coord-logs/us516-provision-cashboxes.py."
        )
    store = journals[STORE_JOURNAL_NAME]
    assert store.account_code == CASH_ACCOUNT_CODE
    assert store.account_name == "Cash and Cash Equivalents"
    assert store.currency_code == "IRR"
    assert store.validation == "balance"
    assert store.active is True
    petty = journals[PETTY_JOURNAL_NAME]
    assert petty.account_code == PETTY_ACCOUNT_CODE
    assert petty.account_name == "Petty Cash"
    assert petty.currency_code == "IRR"
    assert petty.active is True
    assert store.account.engine_id != petty.account.engine_id


# ---------------------------------------------------------------------------
# get_account_balance
# ---------------------------------------------------------------------------


def test_get_account_balance_matches_independent_search(
    client: TrytonRestClient,
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    cash_account: AccountRef,
) -> None:
    as_of = FiscalDate.from_gregorian(datetime.date.today())
    view = adapter.get_account_balance(company, cash_account, as_of)
    assert view.code == CASH_ACCOUNT_CODE
    assert view.as_of == as_of
    debit, credit, count = _independent_balance(client, company, cash_account, as_of)
    assert view.total_debit.amount == debit
    assert view.total_credit.amount == credit
    assert view.line_count == count
    assert view.balance.amount == debit - credit


def test_get_account_balance_ignores_draft_moves(
    client: TrytonRestClient,
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    journal: JournalRef,
    cash_account: AccountRef,
    other_account: AccountRef,
    created_drafts: list[int],
) -> None:
    as_of = FiscalDate.from_gregorian(datetime.date.today())
    before = adapter.get_account_balance(company, cash_account, as_of)
    draft = adapter.create_journal_draft(
        CreateJournalDraft(
            company=company,
            date=MOVE_DATE,
            lines=(
                JournalDraftLine(account=cash_account, debit=MoneyIRR(1_000_000)),
                JournalDraftLine(account=other_account, credit=MoneyIRR(1_000_000)),
            ),
            description="US-516 contract test draft (never posted)",
            journal=journal,
        )
    )
    created_drafts.append(draft.engine_id)
    after = adapter.get_account_balance(company, cash_account, as_of)
    assert after.total_debit == before.total_debit
    assert after.total_credit == before.total_credit
    assert after.line_count == before.line_count


def test_get_account_balance_before_first_move_is_zero(
    client: TrytonRestClient,
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    cash_account: AccountRef,
) -> None:
    earliest = client.search(
        "account.move.line",
        domain=[
            ("account", "=", cash_account.engine_id),
            ("move.company", "=", company.engine_id),
            ("move.state", "=", "posted"),
        ],
        fields=["move"],
        limit=1,
        order=[["move.date", "ASC"]],
        context={"company": company.engine_id},
    )
    if not earliest:
        pytest.skip("No posted cash moves yet; nothing to bound the as-of date.")
    move = client.read("account.move", int(earliest[0]["move"]), fields=["date"])
    assert move is not None
    first_date = datetime.date.fromisoformat(str(move["date"]))
    as_of = FiscalDate.from_gregorian(first_date - datetime.timedelta(days=1))
    view = adapter.get_account_balance(company, cash_account, as_of)
    assert view.total_debit.amount == 0
    assert view.total_credit.amount == 0
    assert view.line_count == 0


def test_get_account_balance_unknown_account(adapter: TrytonAccountingAdapter, company: CompanyRef) -> None:
    with pytest.raises(AccountNotFoundError):
        adapter.get_account_balance(
            company,
            AccountRef(MISSING_ID),
            FiscalDate.from_gregorian(datetime.date.today()),
        )


def test_get_account_balance_wrong_company_account(
    client: TrytonRestClient, adapter: TrytonAccountingAdapter, company: CompanyRef
) -> None:
    # Company 2's hand-made test account (code 1000) does not belong
    # to the Adapter Test company.
    records = client.search(
        "account.account",
        domain=[("company", "=", 2), ("code", "=", "1000")],
        fields=["id"],
    )
    if not records:
        pytest.skip("Company 2's test account is gone; nothing to cross-check.")
    with pytest.raises(AccountNotFoundError):
        adapter.get_account_balance(
            company,
            AccountRef(int(records[0]["id"])),
            FiscalDate.from_gregorian(datetime.date.today()),
        )


# ---------------------------------------------------------------------------
# delete_journal_draft
# ---------------------------------------------------------------------------


def test_delete_journal_draft(
    client: TrytonRestClient,
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    journal: JournalRef,
    cash_account: AccountRef,
    other_account: AccountRef,
) -> None:
    draft = adapter.create_journal_draft(
        CreateJournalDraft(
            company=company,
            date=MOVE_DATE,
            lines=(
                JournalDraftLine(account=cash_account, debit=MoneyIRR(10_000)),
                JournalDraftLine(account=other_account, credit=MoneyIRR(10_000)),
            ),
            description="US-516 contract test draft (deleted by the test)",
            journal=journal,
        )
    )
    assert client.read("account.move", draft.engine_id, fields=["state"]) is not None
    adapter.delete_journal_draft(JournalDraftRef(draft.engine_id))
    assert client.read("account.move", draft.engine_id, fields=["state"]) is None
    # Deleting it again is a stable not-found error.
    with pytest.raises(JournalDraftNotFoundError):
        adapter.delete_journal_draft(JournalDraftRef(draft.engine_id))


def test_delete_journal_draft_unknown(
    adapter: TrytonAccountingAdapter,
) -> None:
    with pytest.raises(JournalDraftNotFoundError):
        adapter.delete_journal_draft(JournalDraftRef(MISSING_ID))


def test_delete_journal_draft_refuses_posted_move(
    client: TrytonRestClient, adapter: TrytonAccountingAdapter, company: CompanyRef
) -> None:
    posted = client.search(
        "account.move",
        domain=[("company", "=", company.engine_id), ("state", "=", "posted")],
        fields=["id"],
        limit=1,
        context={"company": company.engine_id},
    )
    if not posted:
        pytest.skip("No posted move in the test company; nothing to refuse.")
    move_id = int(posted[0]["id"])
    with pytest.raises(JournalAlreadyPostedError):
        adapter.delete_journal_draft(JournalDraftRef(move_id))
    # The posted move is untouched.
    record = client.read("account.move", move_id, fields=["state"])
    assert record is not None and record["state"] == "posted"


# ---------------------------------------------------------------------------
# get_general_journal
# ---------------------------------------------------------------------------


def test_get_general_journal_returns_the_company_named_journal(
    adapter: TrytonAccountingAdapter, company: CompanyRef, journal: JournalRef
) -> None:
    """With several general journals in the engine (Stock, Miscellaneous,
    the test company's own), the company-named one wins."""
    assert adapter.get_general_journal(company) == journal


# ---------------------------------------------------------------------------
# get_account_by_code
# ---------------------------------------------------------------------------


def test_get_account_by_code_returns_the_cash_account(
    adapter: TrytonAccountingAdapter, company: CompanyRef, cash_account: AccountRef
) -> None:
    view = adapter.get_account_by_code(company, CASH_ACCOUNT_CODE)
    assert view is not None
    assert view.ref == cash_account
    assert view.code == CASH_ACCOUNT_CODE
    assert view.is_postable is True


def test_get_account_by_code_returns_none_for_a_missing_code(
    adapter: TrytonAccountingAdapter, company: CompanyRef
) -> None:
    assert adapter.get_account_by_code(company, "9.9.9") is None


# ---------------------------------------------------------------------------
# list_invoice_totals
# ---------------------------------------------------------------------------


def test_list_invoice_totals_matches_the_full_invoice_list(
    adapter: TrytonAccountingAdapter, company: CompanyRef
) -> None:
    totals = adapter.list_invoice_totals(company, invoice_type="out", state="posted")
    full = adapter.list_invoices(company, invoice_type="out", state="posted")
    assert [t.ref for t in totals] == [invoice.ref for invoice in full]
    assert [t.invoice_date for t in totals] == [invoice.invoice_date for invoice in full]
    assert [t.total_amount for t in totals] == [invoice.total_amount for invoice in full]
