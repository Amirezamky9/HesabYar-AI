"""Contract tests for the Tryton REST adapter spike (US-003).

These tests run against the LIVE trytond server and the
``hesabyar_tryton_test`` database, using ONLY the dedicated test company
"HesabYar Adapter Test" (provisioned by
``~/workspace/projects/hesabyar-reference/setup_adapter_test_company.py``;
see docs/adapter.md). They never touch another company's records.

When the server is not reachable (or the test company has not been
provisioned yet), every test SKIPS cleanly with a clear message -- the suite
stays green without a server. With the server up, the tests must PASS.

Configuration (all optional, dev defaults match docs/dev-setup.md):
  TRYTON_BASE_URL       (default http://localhost:8000)
  TRYTON_DATABASE       (default hesabyar_tryton_test)
  TRYTON_API_KEY        (default hesabyar-dev-rest-key)
  TRYTON_TEST_COMPANY   (default "HesabYar Adapter Test")
"""

from __future__ import annotations

import datetime
import os
from collections.abc import Generator
from decimal import Decimal
from typing import Any

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
from hesabyar.infrastructure.accounting.tryton.adapter import TrytonAccountingAdapter
from hesabyar.infrastructure.accounting.tryton.client import TrytonRestClient
from hesabyar.infrastructure.accounting.tryton.errors import (
    ENGINE_AUTHENTICATION_FAILED,
    TrytonAdapterError,
)

BASE_URL = os.environ.get("TRYTON_BASE_URL", "http://localhost:8000")
DATABASE = os.environ.get("TRYTON_DATABASE", "hesabyar_tryton_test")
API_KEY = os.environ.get("TRYTON_API_KEY", "hesabyar-dev-rest-key")
TEST_COMPANY_NAME = os.environ.get("TRYTON_TEST_COMPANY", "HesabYar Adapter Test")
TEST_JOURNAL_NAME = "HesabYar Adapter Test General Journal"

# Two postable leaf accounts of the universal chart (cash + investments).
DEBIT_ACCOUNT_CODE = "1.1.1"
CREDIT_ACCOUNT_CODE = "1.1.2"

# A date inside FY 2026's open November period.
MOVE_DATE = FiscalDate.from_gregorian(datetime.date(2026, 11, 16))

# A date inside FY 2026's open December period (used only by the
# closed-period reversal test, which closes + reopens December).
DECEMBER_DATE = FiscalDate.from_gregorian(datetime.date(2026, 12, 10))


@pytest.fixture(scope="module")
def client() -> Generator[TrytonRestClient, None, None]:
    rest_client = TrytonRestClient(BASE_URL, DATABASE, API_KEY)
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
def accounts(adapter: TrytonAccountingAdapter, company: CompanyRef):
    chart = {view.code: view for view in adapter.get_chart_of_accounts(company)}
    missing = [code for code in (DEBIT_ACCOUNT_CODE, CREDIT_ACCOUNT_CODE) if code not in chart]
    if missing:
        pytest.skip(f"Chart accounts missing from test company: {missing}.")
    return chart[DEBIT_ACCOUNT_CODE], chart[CREDIT_ACCOUNT_CODE]


@pytest.fixture
def created_moves(client: TrytonRestClient) -> Generator[list[int], None, None]:
    """Track moves created by a test and delete them afterwards (drafts only)."""
    ids: list[int] = []
    yield ids
    for move_id in ids:
        client.delete("account.move", move_id)


def _draft_command(
    company: CompanyRef,
    journal: JournalRef,
    debit_account: AccountRef,
    credit_account: AccountRef,
    amount: int = 1_000_000,
    date: FiscalDate = MOVE_DATE,
    description: str = "US-003 contract test draft",
) -> CreateJournalDraft:
    return CreateJournalDraft(
        company=company,
        date=date,
        lines=(
            JournalDraftLine(account=debit_account, debit=MoneyIRR(amount)),
            JournalDraftLine(account=credit_account, credit=MoneyIRR(amount)),
        ),
        description=description,
        journal=journal,
    )


# ---------------------------------------------------------------------------
# AC 3: chart of accounts
# ---------------------------------------------------------------------------


def test_list_companies_returns_test_company(adapter, company):
    companies = adapter.list_companies()
    names = [view.name for view in companies]
    assert TEST_COMPANY_NAME in names
    view = next(view for view in companies if view.ref == company)
    assert view.currency_code == "IRR"


def test_get_chart_of_accounts(adapter, company):
    chart = adapter.get_chart_of_accounts(company)
    # The universal chart template installs 135 accounts for the company.
    assert len(chart) >= 100
    assert all(view.company == company for view in chart)
    codes = [view.code for view in chart]
    assert len(set(codes)) == len(codes), "account codes must be unique"
    # Ordered by code; the codeless chart root sorts last.
    assert codes == sorted(codes, key=lambda code: (code == "", code))
    by_code = {view.code: view for view in chart}
    root = by_code["1.0.0"]
    assert root.parent is not None  # child of the chart root account
    cash = by_code[DEBIT_ACCOUNT_CODE]
    assert cash.name == "Cash and Cash Equivalents"
    assert cash.is_postable
    assert cash.party_required is False
    assert cash.closed is False


# ---------------------------------------------------------------------------
# AC 4: journal draft create + validate
# ---------------------------------------------------------------------------


def test_create_and_validate_balanced_journal_draft(
    adapter, client, company, journal, accounts, created_moves
):
    debit_account, credit_account = accounts
    command = _draft_command(company, journal, debit_account.ref, credit_account.ref)
    ref = adapter.create_journal_draft(command)
    created_moves.append(ref.engine_id)

    summary = adapter.validate_journal_draft(ref)
    assert summary.valid is True, f"issues: {summary.issues}"
    assert summary.issues == ()

    # Read back through the adapter: the engine holds a draft move with the
    # exact lines and integer IRR amounts we sent.
    view = adapter.get_journal_draft(ref)
    assert view is not None
    assert view.state == "draft"
    assert view.company == company
    assert view.date == MOVE_DATE
    assert view.is_balanced
    assert view.total_debit == MoneyIRR(1_000_000)
    assert view.total_credit == MoneyIRR(1_000_000)
    assert all(line.state == "valid" for line in view.lines), (
        "the engine must mark balanced draft lines 'valid'"
    )


def test_validate_flags_unbalanced_draft(
    adapter, client, company, journal, accounts, created_moves
):
    debit_account, credit_account = accounts
    # Bypass domain validation (which would reject an unbalanced command)
    # and create the unbalanced move through the raw REST client, so the
    # test proves the adapter's validation reads the engine state.
    from decimal import Decimal

    record = client.create(
        "account.move",
        {
            "company": company.engine_id,
            "journal": journal.engine_id,
            "period": _period_for(adapter, client, company),
            "date": MOVE_DATE.gregorian_date,
            "description": "US-003 unbalanced contract test",
            "lines": [
                [
                    "create",
                    [
                        {
                            "account": debit_account.ref.engine_id,
                            "debit": Decimal("1000000"),
                            "credit": Decimal("0"),
                        },
                        {
                            "account": credit_account.ref.engine_id,
                            "debit": Decimal("0"),
                            "credit": Decimal("999999"),
                        },
                    ],
                ]
            ],
        },
        fields=["id"],
        context={"company": company.engine_id},
    )
    created_moves.append(int(record["id"]))
    summary = adapter.validate_journal_draft(JournalDraftRef(int(record["id"])))
    assert summary.valid is False
    assert "LEDGER_UNBALANCED" in summary.codes()


def _period_for(adapter, client, company: CompanyRef) -> int:
    records = client.search(
        "account.period",
        domain=[
            ("fiscalyear.company", "=", company.engine_id),
            ("state", "=", "open"),
            ("start_date", "<=", MOVE_DATE.gregorian_date),
            ("end_date", ">=", MOVE_DATE.gregorian_date),
        ],
        fields=["name"],
        context={"company": company.engine_id},
    )
    assert records, "no open period covers the test date"
    return int(records[0]["id"])


def test_create_draft_rejects_unknown_account(
    adapter, company, journal, accounts
):
    _, credit_account = accounts
    command = _draft_command(
        company,
        journal,
        AccountRef(999_999_999),  # does not exist
        credit_account.ref,
    )
    with pytest.raises(AccountNotFoundError) as excinfo:
        adapter.create_journal_draft(command)
    assert excinfo.value.code == "LEDGER_ACCOUNT_NOT_FOUND"


def test_create_draft_rejects_account_of_another_company(
    adapter, company, journal, accounts
):
    # Any other company in the DB (e.g. the sibling story's test company)
    # owns its own accounts; using one of them in our company's draft must
    # fail. If no other company exists yet, there is nothing to cross-check.
    others = [view for view in adapter.list_companies() if view.ref != company]
    if not others:
        pytest.skip("no other company in the DB; nothing to cross-check")
    other_accounts = adapter.get_chart_of_accounts(others[0].ref)
    if not other_accounts:
        pytest.skip("other company has no chart yet; nothing to cross-check")
    _, credit_account = accounts
    command = _draft_command(
        company,
        journal,
        other_accounts[0].ref,
        credit_account.ref,
    )
    with pytest.raises(AccountNotFoundError):
        adapter.create_journal_draft(command)


def test_create_draft_rejects_date_without_open_period(
    adapter, company, journal, accounts
):
    debit_account, credit_account = accounts
    command = _draft_command(
        company,
        journal,
        debit_account.ref,
        credit_account.ref,
        date=FiscalDate.from_gregorian(datetime.date(2020, 1, 15)),
    )
    with pytest.raises(PeriodClosedError) as excinfo:
        adapter.create_journal_draft(command)
    assert excinfo.value.code == "LEDGER_PERIOD_CLOSED"


def test_validate_missing_draft_raises_stable_error(adapter):
    from hesabyar.domain.common.errors import JournalDraftNotFoundError

    with pytest.raises(JournalDraftNotFoundError) as excinfo:
        adapter.validate_journal_draft(JournalDraftRef(999_999_999))
    assert excinfo.value.code == "LEDGER_JOURNAL_NOT_FOUND"


# ---------------------------------------------------------------------------
# AC 5: error translation
# ---------------------------------------------------------------------------


def test_wrong_api_key_translates_to_stable_error_without_traceback(client):
    # `client` is only used for its skip-when-unreachable fixture behaviour.
    bad_client = TrytonRestClient(BASE_URL, DATABASE, "definitely-not-a-valid-key")
    try:
        with pytest.raises(TrytonAdapterError) as excinfo:
            bad_client.search("res.user", fields=["login"], limit=1)
    finally:
        bad_client.close()
    assert excinfo.value.code == ENGINE_AUTHENTICATION_FAILED
    assert "Traceback" not in str(excinfo.value)
    assert ".py" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# US-201: post / reverse / journal read / fiscal years / periods
#
# Posted moves cannot be deleted (by design), so tests that post a move do
# NOT register it with `created_moves`; they stay in the dedicated test
# company, which is the point of having one.
# ---------------------------------------------------------------------------


def _post_draft(adapter, company, journal, accounts, amount=1_000_000, date=MOVE_DATE):
    debit_account, credit_account = accounts
    command = _draft_command(
        company,
        journal,
        debit_account.ref,
        credit_account.ref,
        amount=amount,
        date=date,
        description="US-201 contract test",
    )
    draft = adapter.create_journal_draft(command)
    return adapter.post_journal(PostJournal(draft))


def test_post_balanced_draft_returns_posted_move_with_number(
    adapter, company, journal, accounts
):
    posted = _post_draft(adapter, company, journal, accounts)
    view = adapter.get_journal(posted)
    assert view is not None
    assert view.ref == posted
    assert view.number, "the engine must assign a move number on posting"
    assert view.post_date is not None
    assert view.date == MOVE_DATE
    assert view.company == company
    assert view.origin is None
    assert view.is_reversal is False
    assert view.is_balanced
    assert view.total_debit == MoneyIRR(1_000_000)
    assert view.total_credit == MoneyIRR(1_000_000)
    assert all(line.state == "valid" for line in view.lines)


def test_post_twice_raises_already_posted(adapter, company, journal, accounts):
    debit_account, credit_account = accounts
    command = _draft_command(company, journal, debit_account.ref, credit_account.ref)
    draft = adapter.create_journal_draft(command)
    adapter.post_journal(PostJournal(draft))
    with pytest.raises(JournalAlreadyPostedError) as excinfo:
        adapter.post_journal(PostJournal(draft))
    assert excinfo.value.code == "LEDGER_POSTED_IMMUTABLE"


def test_post_unbalanced_draft_rejected_and_stays_draft(
    adapter, client, company, journal, accounts, created_moves
):
    debit_account, credit_account = accounts
    # Bypass domain validation and create the unbalanced move through the
    # raw REST client, so the test proves post-time enforcement.
    from decimal import Decimal

    record = client.create(
        "account.move",
        {
            "company": company.engine_id,
            "journal": journal.engine_id,
            "period": _period_for(adapter, client, company),
            "date": MOVE_DATE.gregorian_date,
            "description": "US-201 unbalanced post test",
            "lines": [
                [
                    "create",
                    [
                        {
                            "account": debit_account.ref.engine_id,
                            "debit": Decimal("1000000"),
                            "credit": Decimal("0"),
                        },
                        {
                            "account": credit_account.ref.engine_id,
                            "debit": Decimal("0"),
                            "credit": Decimal("999999"),
                        },
                    ],
                ]
            ],
        },
        fields=["id"],
        context={"company": company.engine_id},
    )
    draft_id = int(record["id"])
    created_moves.append(draft_id)
    with pytest.raises(UnbalancedJournalError) as excinfo:
        adapter.post_journal(PostJournal(JournalDraftRef(draft_id)))
    assert excinfo.value.code == "LEDGER_UNBALANCED"
    # Atomic posting: the failed post left the move exactly as it was.
    view = adapter.get_journal_draft(JournalDraftRef(draft_id))
    assert view is not None
    assert view.state == "draft"


# NOTE: post-time rejection of unknown/closed accounts and of closed
# periods is covered by unit tests (tests/unit/test_tryton_adapter.py),
# not here: the engine makes those states unreachable via REST -- it
# refuses to create lines on a closed account, refuses to close an
# account that has move lines, refuses to close a period with unposted
# moves, and refuses to create moves in closed periods. The adapter's
# checks are defense-in-depth for states reachable only outside REST.
def test_post_missing_draft_raises_stable_error(adapter):
    with pytest.raises(JournalDraftNotFoundError) as excinfo:
        adapter.post_journal(PostJournal(JournalDraftRef(999_999_999)))
    assert excinfo.value.code == "LEDGER_JOURNAL_NOT_FOUND"


def test_reverse_posted_journal_links_to_original(adapter, company, journal, accounts):
    posted = _post_draft(adapter, company, journal, accounts, amount=750_000)
    original = adapter.get_journal(posted)
    assert original is not None
    reversal = adapter.reverse_journal(
        ReverseJournal(posted, description="US-201 reversal test")
    )
    assert reversal != posted
    view = adapter.get_journal(reversal)
    assert view is not None
    assert view.number, "the reversal must get its own move number"
    assert view.number != original.number
    assert view.origin == posted, "the reversal must link back to the original"
    assert view.is_reversal is True
    assert view.description == "US-201 reversal test"
    assert view.is_balanced
    assert view.total_debit == MoneyIRR(750_000)
    assert view.total_credit == MoneyIRR(750_000)
    # The reversal swaps the sides: the original debit account is credited.
    by_account = {line.account: line for line in view.lines}
    debit_account, credit_account = accounts
    assert by_account[debit_account.ref].credit == MoneyIRR(750_000)
    assert by_account[debit_account.ref].debit.is_zero
    assert by_account[credit_account.ref].debit == MoneyIRR(750_000)
    assert by_account[credit_account.ref].credit.is_zero
    # The original is untouched by its reversal.
    assert adapter.get_journal(posted) is not None
    assert adapter.get_journal(posted).number == original.number


def test_reverse_draft_raises(adapter, company, journal, accounts, created_moves):
    debit_account, credit_account = accounts
    command = _draft_command(company, journal, debit_account.ref, credit_account.ref)
    draft = adapter.create_journal_draft(command)
    created_moves.append(draft.engine_id)
    with pytest.raises(InvalidJournalError) as excinfo:
        adapter.reverse_journal(ReverseJournal(PostedJournalRef(draft.engine_id)))
    assert excinfo.value.code == "LEDGER_INVALID_JOURNAL"


def test_reverse_missing_raises_stable_error(adapter):
    with pytest.raises(PostedJournalNotFoundError) as excinfo:
        adapter.reverse_journal(ReverseJournal(PostedJournalRef(999_999_999)))
    assert excinfo.value.code == "LEDGER_JOURNAL_NOT_FOUND"


def test_get_journal_missing_returns_none(adapter):
    assert adapter.get_journal(PostedJournalRef(999_999_999)) is None


def test_get_journal_draft_raises(adapter, company, journal, accounts, created_moves):
    debit_account, credit_account = accounts
    command = _draft_command(company, journal, debit_account.ref, credit_account.ref)
    draft = adapter.create_journal_draft(command)
    created_moves.append(draft.engine_id)
    with pytest.raises(InvalidJournalError):
        adapter.get_journal(PostedJournalRef(draft.engine_id))


def test_posted_move_has_no_edit_or_delete_path(
    adapter, client, company, journal, accounts
):
    # Adapter level: no edit/delete method for journals exists at all.
    assert not hasattr(adapter, "delete_journal")
    assert not hasattr(adapter, "update_journal")
    assert not hasattr(adapter, "delete_posted_journal")
    # Engine level: even raw REST calls cannot mutate a posted move.
    posted = _post_draft(adapter, company, journal, accounts, amount=250_000)
    with pytest.raises(TrytonAdapterError) as excinfo:
        client.delete("account.move", posted.engine_id)
    assert "posted" in str(excinfo.value).lower()
    with pytest.raises(TrytonAdapterError):
        client.write(
            "account.move",
            posted.engine_id,
            {"description": "tampered"},
            context={"company": company.engine_id},
        )
    view = adapter.get_journal(posted)
    assert view is not None
    assert view.description == "US-201 contract test"


def test_reverse_into_closed_period_uses_open_period(
    adapter, client, company, journal, accounts
):
    # Post a move in December, close December, then reverse: like the
    # Tryton cancel wizard, the reversal must land in today's open period.
    posted = _post_draft(
        adapter, company, journal, accounts, amount=300_000, date=DECEMBER_DATE
    )
    periods = client.search(
        "account.period",
        domain=[
            ("fiscalyear.company", "=", company.engine_id),
            ("name", "=", "2026-12"),
        ],
        fields=["name", "state"],
        context={"company": company.engine_id},
    )
    december_id = int(periods[0]["id"])
    try:
        client.action(
            "account.period",
            "close",
            december_id,
            fields=["id", "state"],
            context={"company": company.engine_id},
        )
        reversal = adapter.reverse_journal(ReverseJournal(posted))
        view = adapter.get_journal(reversal)
        assert view is not None
        assert view.date == FiscalDate.from_gregorian(datetime.date.today())
        assert view.period.name == "2026-11"
        assert view.period.is_open
    finally:
        client.action(
            "account.period",
            "reopen",
            december_id,
            fields=["id", "state"],
            context={"company": company.engine_id},
        )
        # Period.reopen does NOT reopen the period's journal-periods
        # (Tryton quirk: they have their own open/closed state), and a
        # closed journal-period blocks all later moves in that period.
        # Reopen them too, or the next run of this test cannot create its
        # December draft.
        journal_periods = client.search(
            "account.journal.period",
            domain=[("period", "=", december_id), ("state", "=", "closed")],
            fields=["state"],
            context={"company": company.engine_id},
        )
        for journal_period in journal_periods:
            client.action(
                "account.journal.period",
                "reopen",
                int(journal_period["id"]),
                fields=["id", "state"],
                context={"company": company.engine_id},
            )
    state = client.read("account.period", december_id, fields=["state"])
    assert state is not None and state["state"] == "open"
    journal_periods = client.search(
        "account.journal.period",
        domain=[("period", "=", december_id)],
        fields=["state"],
        context={"company": company.engine_id},
    )
    assert all(jp["state"] == "open" for jp in journal_periods)


def test_list_fiscal_years(adapter, company):
    years = adapter.list_fiscal_years(company)
    assert years, "the test company must have at least one fiscal year"
    assert all(view.company == company for view in years)
    fy2026 = next(view for view in years if view.name == "FY 2026")
    assert fy2026.is_open
    assert fy2026.start_date == FiscalDate.from_gregorian(datetime.date(2026, 1, 1))
    assert fy2026.end_date == FiscalDate.from_gregorian(datetime.date(2026, 12, 31))
    starts = [view.start_date for view in years]
    assert starts == sorted(starts, reverse=True), "newest fiscal year first"


def test_list_periods(adapter, company):
    years = adapter.list_fiscal_years(company)
    fy2026 = next(view for view in years if view.name == "FY 2026")
    periods = adapter.list_periods(company, fy2026.ref)
    assert len(periods) == 12, "FY 2026 was provisioned with 12 monthly periods"
    assert all(period.fiscal_year == fy2026.ref for period in periods)
    assert all(period.ref is not None for period in periods)
    assert all(period.period_type == "standard" for period in periods)
    assert all(period.is_open for period in periods)
    assert [period.name for period in periods] == [f"2026-{month:02d}" for month in range(1, 13)]
    starts = [period.start_date for period in periods]
    assert starts == sorted(starts), "periods ordered by start date"
    november = next(period for period in periods if period.name == "2026-11")
    assert november.contains(MOVE_DATE)
    assert not november.contains(DECEMBER_DATE)
    # Without a fiscal-year filter, every period of the company is listed.
    all_periods = adapter.list_periods(company)
    assert len(all_periods) >= 12
    assert {period.name for period in periods} <= {period.name for period in all_periods}


# ---------------------------------------------------------------------------
# US-204: invoices, payments, bank statements
#
# The test company has no invoices, payments or statements yet, so these
# tests create every fixture they need through the REST API: a customer
# party + address, a manual payment journal and a balance-validated
# statement journal (module fixtures, deleted afterwards), plus the
# documents themselves (function fixtures, deleted after each test).
# Draft documents are deletable; nothing here posts anything, so no
# permanent records are left behind.
# ---------------------------------------------------------------------------

# Chart accounts used by the invoice tests (universal chart codes).
RECEIVABLE_ACCOUNT_CODE = "1.2.1"
PAYABLE_ACCOUNT_CODE = "2.1.1"
REVENUE_ACCOUNT_CODE = "4.1.1"
SERVICES_ACCOUNT_CODE = "4.1.2"
# The engine enforces a direction domain on invoice line accounts
# (InvoiceLine._account_domain): 'out' lines need revenue accounts,
# 'in' lines need expense (or debt) accounts.
EXPENSE_ACCOUNT_CODE = "5.1.1"
EXPENSE_SERVICES_CODE = "5.1.3"

CUSTOMER_NAME = "HesabYar Contract Customer"
PAYMENT_JOURNAL_NAME = "HesabYar Contract Payments"
STATEMENT_JOURNAL_NAME = "HesabYar Contract Bank"


@pytest.fixture(scope="module")
def irr_currency_id(client: TrytonRestClient) -> int:
    records = client.search(
        "currency.currency", domain=[("code", "=", "IRR")], fields=["code"]
    )
    assert records, "IRR currency not found in the test DB"
    return int(records[0]["id"])


@pytest.fixture(scope="module")
def customer(client: TrytonRestClient):
    """A customer party with one address, created via REST."""
    party = client.create("party.party", {"name": CUSTOMER_NAME}, fields=["id"])
    party_id = int(party["id"])
    address = client.create(
        "party.address", {"party": party_id, "city": "Tehran"}, fields=["id"]
    )
    address_id = int(address["id"])
    yield PartyRef(party_id), PartyAddressRef(address_id)
    client.delete("party.address", address_id)
    client.delete("party.party", party_id)


@pytest.fixture(scope="module")
def party_without_address(client: TrytonRestClient):
    party = client.create(
        "party.party", {"name": "HesabYar Addressless Party"}, fields=["id"]
    )
    party_id = int(party["id"])
    yield PartyRef(party_id)
    client.delete("party.party", party_id)


@pytest.fixture(scope="module")
def invoice_accounts(adapter: TrytonAccountingAdapter, company: CompanyRef):
    chart = {view.code: view for view in adapter.get_chart_of_accounts(company)}
    wanted = (
        RECEIVABLE_ACCOUNT_CODE,
        PAYABLE_ACCOUNT_CODE,
        REVENUE_ACCOUNT_CODE,
        SERVICES_ACCOUNT_CODE,
        EXPENSE_ACCOUNT_CODE,
        EXPENSE_SERVICES_CODE,
    )
    missing = [code for code in wanted if code not in chart]
    if missing:
        pytest.skip(f"Chart accounts missing from test company: {missing}.")
    return {code: chart[code] for code in wanted}


@pytest.fixture(scope="module")
def payment_journal(client: TrytonRestClient, company: CompanyRef, irr_currency_id: int):
    """A manual payment journal, created via REST (deleted afterwards)."""
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
    yield journal_id
    client.delete("account.payment.journal", journal_id, context={"company": company.engine_id})


@pytest.fixture(scope="module")
def statement_journal(
    client: TrytonRestClient,
    company: CompanyRef,
    irr_currency_id: int,
    accounts,
):
    """A balance-validated statement journal, created via REST.

    Uses the global 'Statement' account journal (type 'statement',
    created by module activation) and the cash account 1.1.1.
    """
    journals = client.search(
        "account.journal", domain=[("type", "=", "statement")], fields=["name"]
    )
    if not journals:
        pytest.skip("No account.journal of type 'statement' in the test DB.")
    cash_account, _ = accounts
    record = client.create(
        "account.statement.journal",
        {
            "name": STATEMENT_JOURNAL_NAME,
            "journal": int(journals[0]["id"]),
            "currency": irr_currency_id,
            "company": company.engine_id,
            "validation": "balance",
            "account": cash_account.ref.engine_id,
        },
        fields=["id"],
        context={"company": company.engine_id},
    )
    journal_id = int(record["id"])
    yield journal_id
    client.delete("account.statement.journal", journal_id, context={"company": company.engine_id})


@pytest.fixture
def created_invoices(client: TrytonRestClient) -> Generator[list[int], None, None]:
    """Track invoices created by a test and delete them afterwards (drafts only)."""
    ids: list[int] = []
    yield ids
    for invoice_id in ids:
        client.delete("account.invoice", invoice_id)


@pytest.fixture
def created_payments(client: TrytonRestClient) -> Generator[list[int], None, None]:
    ids: list[int] = []
    yield ids
    for payment_id in ids:
        client.delete("account.payment", payment_id)


@pytest.fixture
def created_statements(client: TrytonRestClient) -> Generator[list[int], None, None]:
    ids: list[int] = []
    yield ids
    for statement_id in ids:
        client.delete("account.statement", statement_id)


def _invoice_command(
    company: CompanyRef,
    party: PartyRef,
    invoice_accounts,
    invoice_type: str = "out",
    date: FiscalDate = MOVE_DATE,
    description: str = "US-204 contract test invoice",
    **kwargs,
) -> CreateInvoiceDraft:
    if invoice_type == "out":
        account = invoice_accounts[RECEIVABLE_ACCOUNT_CODE].ref
        line_accounts = (
            invoice_accounts[REVENUE_ACCOUNT_CODE].ref,
            invoice_accounts[SERVICES_ACCOUNT_CODE].ref,
        )
    else:
        account = invoice_accounts[PAYABLE_ACCOUNT_CODE].ref
        # Supplier lines need expense accounts (engine direction domain).
        line_accounts = (
            invoice_accounts[EXPENSE_ACCOUNT_CODE].ref,
            invoice_accounts[EXPENSE_SERVICES_CODE].ref,
        )
    account = kwargs.pop("account", account)
    return CreateInvoiceDraft(
        company=company,
        invoice_type=invoice_type,
        party=party,
        date=date,
        lines=(
            InvoiceDraftLine(
                account=line_accounts[0],
                unit_price=MoneyIRR(500_000),
                quantity=Decimal(1),
                description="Goods",
            ),
            InvoiceDraftLine(
                account=line_accounts[1],
                unit_price=MoneyIRR(125_000),
                quantity=Decimal(2),
                description="Services",
            ),
        ),
        description=description,
        account=account,
        **kwargs,
    )


def _raw_invoice(
    client: TrytonRestClient,
    company: CompanyRef,
    party: PartyRef,
    address: PartyAddressRef,
    invoice_accounts,
    irr_currency_id: int,
    **overrides,
) -> int:
    """Create an invoice through the raw REST client (bypasses adapter checks)."""
    revenue_journals = client.search(
        "account.journal", domain=[("type", "=", "revenue")], fields=["name"]
    )
    assert revenue_journals, "no revenue journal in the test DB"
    values: dict[str, Any] = {
        "company": company.engine_id,
        "type": "out",
        "party": party.engine_id,
        "invoice_address": address.engine_id,
        "currency": irr_currency_id,
        "account": invoice_accounts[RECEIVABLE_ACCOUNT_CODE].ref.engine_id,
        "journal": int(revenue_journals[0]["id"]),
        "invoice_date": MOVE_DATE.gregorian_date,
        "description": "US-204 raw contract test invoice",
        "lines": [
            [
                "create",
                [
                    {
                        "account": invoice_accounts[REVENUE_ACCOUNT_CODE].ref.engine_id,
                        "quantity": 1,
                        "unit_price": Decimal("500000"),
                        "description": "Goods",
                    }
                ],
            ]
        ],
    }
    values.update(overrides)
    # An override of journal=None / lines=[] means "omit the key entirely":
    # the engine must see a draft with no journal / no lines at all.
    if values.get("journal") is None:
        values.pop("journal", None)
    if not values.get("lines"):
        values.pop("lines", None)
    record = client.create(
        "account.invoice", values, fields=["id"], context={"company": company.engine_id}
    )
    return int(record["id"])


def _raw_payment(
    client: TrytonRestClient,
    company: CompanyRef,
    payment_journal_id: int,
    party: PartyRef,
    amount: int = 500_000,
    kind: str = "receivable",
) -> int:
    record = client.create(
        "account.payment",
        {
            "company": company.engine_id,
            "journal": payment_journal_id,
            "kind": kind,
            "party": party.engine_id,
            "date": MOVE_DATE.gregorian_date,
            "amount": Decimal(amount),
            "reference": "US-204 contract test payment",
        },
        fields=["id"],
        context={"company": company.engine_id},
    )
    return int(record["id"])


def _raw_statement(
    client: TrytonRestClient,
    company: CompanyRef,
    statement_journal_id: int,
    party: PartyRef,
    invoice_accounts,
    name: str = "US-204 contract test statement",
) -> int:
    record = client.create(
        "account.statement",
        {
            "name": name,
            "company": company.engine_id,
            "journal": statement_journal_id,
            "date": MOVE_DATE.gregorian_date,
            "start_balance": Decimal("0"),
            "end_balance": Decimal("750000"),
            "lines": [
                [
                    "create",
                    [
                        {
                            "date": MOVE_DATE.gregorian_date,
                            "amount": Decimal("1000000"),
                            "account": invoice_accounts[
                                RECEIVABLE_ACCOUNT_CODE
                            ].ref.engine_id,
                            "party": party.engine_id,
                            "description": "Customer receipt",
                        },
                        {
                            "date": MOVE_DATE.gregorian_date,
                            "amount": Decimal("-250000"),
                            "account": invoice_accounts[
                                PAYABLE_ACCOUNT_CODE
                            ].ref.engine_id,
                            "party": party.engine_id,
                            "description": "Supplier payment",
                        },
                    ],
                ]
            ],
        },
        fields=["id"],
        context={"company": company.engine_id},
    )
    return int(record["id"])


def test_create_and_validate_invoice_draft(
    adapter, company, customer, invoice_accounts, created_invoices
):
    party, _address = customer
    command = _invoice_command(company, party, invoice_accounts)
    ref = adapter.create_invoice_draft(command)
    created_invoices.append(ref.engine_id)

    summary = adapter.validate_invoice_draft(ref)
    assert summary.valid is True, f"issues: {summary.issues}"
    assert summary.issues == ()

    view = adapter.get_invoice(ref)
    assert view is not None
    assert view.state == "draft"
    assert view.is_draft is True
    assert view.company == company
    assert view.invoice_type == "out"
    assert view.is_customer_invoice is True
    assert view.party == party
    assert view.party_name == CUSTOMER_NAME
    assert view.number is None, "a draft invoice has no number yet"
    assert view.invoice_date == MOVE_DATE
    assert view.currency_code == "IRR"
    assert view.untaxed_amount == MoneyIRR(750_000)
    assert view.tax_amount == MoneyIRR(0)
    assert view.total_amount == MoneyIRR(750_000)
    assert len(view.lines) == 2
    assert view.lines[0].amount == MoneyIRR(500_000)
    assert view.lines[1].quantity == Decimal("2")
    assert view.lines[1].amount == MoneyIRR(250_000)


def test_create_invoice_draft_resolves_revenue_journal(
    adapter, company, customer, invoice_accounts, created_invoices
):
    # No explicit journal: the engine has exactly one revenue journal.
    party, _address = customer
    command = _invoice_command(company, party, invoice_accounts)
    assert command.journal is None
    ref = adapter.create_invoice_draft(command)
    created_invoices.append(ref.engine_id)
    view = adapter.get_invoice(ref)
    assert view is not None
    assert view.journal is not None
    assert adapter.validate_invoice_draft(ref).valid is True


def test_create_invoice_draft_uses_config_default_on_ambiguous_chart(
    adapter, company, customer, invoice_accounts, created_invoices
):
    # The test company has several open receivable accounts, so no
    # unique account exists — but it has a configured default
    # receivable (1.2.1, set by setup_adapter_test_company.py), and
    # the adapter uses it instead of guessing. A configured default
    # is an explicit company choice; the still-ambiguous case (no
    # default at all) is covered by the MockTransport unit tests.
    party, _address = customer
    command = _invoice_command(
        company, party, invoice_accounts, account=None
    )
    assert command.account is None
    ref = adapter.create_invoice_draft(command)
    created_invoices.append(ref.engine_id)
    view = adapter.get_invoice(ref)
    assert view is not None
    assert view.account == invoice_accounts[RECEIVABLE_ACCOUNT_CODE].ref


def test_create_supplier_invoice_draft(
    adapter, company, customer, invoice_accounts, created_invoices
):
    party, _address = customer
    command = _invoice_command(
        company, party, invoice_accounts, invoice_type="in"
    )
    ref = adapter.create_invoice_draft(command)
    created_invoices.append(ref.engine_id)
    assert adapter.validate_invoice_draft(ref).valid is True
    view = adapter.get_invoice(ref)
    assert view is not None
    assert view.invoice_type == "in"
    assert view.is_customer_invoice is False
    assert view.account == invoice_accounts[PAYABLE_ACCOUNT_CODE].ref


def test_create_invoice_draft_rejects_unknown_party(
    adapter, company, invoice_accounts
):
    command = _invoice_command(
        company, PartyRef(999_999_999), invoice_accounts
    )
    with pytest.raises(PartyNotFoundError) as excinfo:
        adapter.create_invoice_draft(command)
    assert excinfo.value.code == "LEDGER_PARTY_NOT_FOUND"


def test_create_invoice_draft_rejects_party_without_address(
    adapter, company, party_without_address, invoice_accounts
):
    command = _invoice_command(
        company, party_without_address, invoice_accounts
    )
    with pytest.raises(InvalidInvoiceError) as excinfo:
        adapter.create_invoice_draft(command)
    assert excinfo.value.code == "LEDGER_INVALID_INVOICE"


def test_create_invoice_draft_rejects_wrong_account_type(
    adapter, company, customer, invoice_accounts
):
    party, _address = customer
    command = _invoice_command(
        company,
        party,
        invoice_accounts,
        account=invoice_accounts[PAYABLE_ACCOUNT_CODE].ref,
    )
    with pytest.raises(InvalidInvoiceError) as excinfo:
        adapter.create_invoice_draft(command)
    assert excinfo.value.code == "LEDGER_INVALID_INVOICE"


def test_create_invoice_draft_rejects_wrong_direction_line_account(
    adapter, company, customer, invoice_accounts
):
    # A revenue account on a supplier invoice (and vice versa) violates
    # the engine's line-account domain; the adapter must fail fast with a
    # stable error instead of leaking the engine's domain error.
    party, _address = customer
    command = CreateInvoiceDraft(
        company=company,
        invoice_type="in",
        party=party,
        date=MOVE_DATE,
        lines=(
            InvoiceDraftLine(
                account=invoice_accounts[REVENUE_ACCOUNT_CODE].ref,
                unit_price=MoneyIRR(500_000),
                quantity=Decimal(1),
            ),
        ),
        account=invoice_accounts[PAYABLE_ACCOUNT_CODE].ref,
    )
    with pytest.raises(InvalidInvoiceError) as excinfo:
        adapter.create_invoice_draft(command)
    assert excinfo.value.code == "LEDGER_INVALID_INVOICE"


def test_create_invoice_draft_rejects_unknown_line_account(
    adapter, company, customer, invoice_accounts
):
    party, _address = customer
    command = CreateInvoiceDraft(
        company=company,
        invoice_type="out",
        party=party,
        date=MOVE_DATE,
        lines=(
            InvoiceDraftLine(
                account=AccountRef(999_999_999),
                unit_price=MoneyIRR(500_000),
                quantity=Decimal(1),
            ),
        ),
        account=invoice_accounts[RECEIVABLE_ACCOUNT_CODE].ref,
    )
    with pytest.raises(AccountNotFoundError) as excinfo:
        adapter.create_invoice_draft(command)
    assert excinfo.value.code == "LEDGER_ACCOUNT_NOT_FOUND"


def test_list_invoices(
    adapter, company, customer, invoice_accounts, created_invoices
):
    party, _address = customer
    out_ref = adapter.create_invoice_draft(
        _invoice_command(company, party, invoice_accounts)
    )
    created_invoices.append(out_ref.engine_id)
    in_ref = adapter.create_invoice_draft(
        _invoice_command(company, party, invoice_accounts, invoice_type="in")
    )
    created_invoices.append(in_ref.engine_id)

    invoices = adapter.list_invoices(company)
    refs = [view.ref for view in invoices]
    assert out_ref in refs
    assert in_ref in refs
    assert all(view.company == company for view in invoices)
    assert all(view.currency_code == "IRR" for view in invoices)

    out_only = adapter.list_invoices(company, invoice_type="out")
    assert out_ref in [view.ref for view in out_only]
    assert in_ref not in [view.ref for view in out_only]
    in_only = adapter.list_invoices(company, invoice_type="in")
    assert in_ref in [view.ref for view in in_only]
    assert out_ref not in [view.ref for view in in_only]

    drafts = adapter.list_invoices(company, state="draft")
    assert out_ref in [view.ref for view in drafts]
    posted = adapter.list_invoices(company, state="posted")
    assert out_ref not in [view.ref for view in posted]

    # Company isolation: another company's list never shows our invoices.
    others = [view for view in adapter.list_companies() if view.ref != company]
    if others:
        other_invoices = adapter.list_invoices(others[0].ref)
        assert all(view.ref not in (out_ref, in_ref) for view in other_invoices)


def test_get_invoice_missing_returns_none(adapter):
    assert adapter.get_invoice(InvoiceRef(999_999_999)) is None


def test_validate_missing_invoice_raises_stable_error(adapter):
    with pytest.raises(InvoiceNotFoundError) as excinfo:
        adapter.validate_invoice_draft(InvoiceRef(999_999_999))
    assert excinfo.value.code == "LEDGER_INVOICE_NOT_FOUND"


def test_validate_invoice_draft_flags_closed_period(
    adapter, company, customer, invoice_accounts, created_invoices
):
    # Creation does not check the period (a draft carries none), but
    # validation must flag a date outside every open period: posting
    # derives the period from the invoice date.
    party, _address = customer
    command = _invoice_command(
        company,
        party,
        invoice_accounts,
        date=FiscalDate.from_gregorian(datetime.date(2020, 1, 15)),
    )
    ref = adapter.create_invoice_draft(command)
    created_invoices.append(ref.engine_id)
    summary = adapter.validate_invoice_draft(ref)
    assert summary.valid is False
    assert "LEDGER_PERIOD_CLOSED" in summary.codes()


def test_validate_invoice_draft_flags_missing_journal(
    adapter, client, company, customer, invoice_accounts, irr_currency_id, created_invoices
):
    # Bypass the adapter (which always resolves a journal) and create the
    # draft through the raw REST client without one.
    party, address = customer
    invoice_id = _raw_invoice(
        client,
        company,
        party,
        address,
        invoice_accounts,
        irr_currency_id,
        journal=None,
    )
    created_invoices.append(invoice_id)
    summary = adapter.validate_invoice_draft(InvoiceRef(invoice_id))
    assert summary.valid is False
    assert "LEDGER_INVALID_INVOICE" in summary.codes()
    assert any(issue.field == "journal" for issue in summary.issues)


def test_validate_invoice_draft_flags_no_lines(
    adapter, client, company, customer, invoice_accounts, irr_currency_id, created_invoices
):
    party, address = customer
    invoice_id = _raw_invoice(
        client,
        company,
        party,
        address,
        invoice_accounts,
        irr_currency_id,
        lines=[],
    )
    created_invoices.append(invoice_id)
    summary = adapter.validate_invoice_draft(InvoiceRef(invoice_id))
    assert summary.valid is False
    assert "LEDGER_INVALID_INVOICE" in summary.codes()
    assert any(issue.field == "lines" for issue in summary.issues)


def test_list_and_get_payments(
    adapter, client, company, customer, payment_journal, created_payments
):
    party, _address = customer
    receivable_id = _raw_payment(
        client, company, payment_journal, party, amount=500_000, kind="receivable"
    )
    created_payments.append(receivable_id)
    payable_id = _raw_payment(
        client, company, payment_journal, party, amount=200_000, kind="payable"
    )
    created_payments.append(payable_id)

    view = adapter.get_payment(PaymentRef(receivable_id))
    assert view is not None
    assert view.number, "the engine must assign a payment number from its sequence"
    assert view.kind == "receivable"
    assert view.party == party
    assert view.party_name == CUSTOMER_NAME
    assert view.date == MOVE_DATE
    assert view.amount == MoneyIRR(500_000)
    assert view.state == "draft"
    assert view.journal_name == PAYMENT_JOURNAL_NAME
    assert view.currency_code == "IRR"

    payments = adapter.list_payments(company)
    refs = [item.ref for item in payments]
    assert PaymentRef(receivable_id) in refs
    assert PaymentRef(payable_id) in refs
    assert all(item.company == company for item in payments)

    receivable_only = adapter.list_payments(company, kind="receivable")
    assert PaymentRef(receivable_id) in [item.ref for item in receivable_only]
    assert PaymentRef(payable_id) not in [item.ref for item in receivable_only]
    drafts = adapter.list_payments(company, state="draft")
    assert PaymentRef(receivable_id) in [item.ref for item in drafts]


def test_get_payment_missing_returns_none(adapter):
    assert adapter.get_payment(PaymentRef(999_999_999)) is None


def test_list_and_get_bank_statements(
    adapter, client, company, customer, statement_journal, invoice_accounts, created_statements
):
    party, _address = customer
    statement_id = _raw_statement(
        client, company, statement_journal, party, invoice_accounts
    )
    created_statements.append(statement_id)

    view = adapter.get_bank_statement(BankStatementRef(statement_id))
    assert view is not None
    assert view.name == "US-204 contract test statement"
    assert view.journal_name == STATEMENT_JOURNAL_NAME
    assert view.date == MOVE_DATE
    assert view.state == "draft"
    assert view.validation == "balance"
    assert view.currency_code == "IRR"
    assert view.start_balance == MoneyIRR(0)
    assert view.end_balance == MoneyIRR(750_000)
    assert view.balance == MoneyIRR(750_000)
    assert len(view.lines) == 2
    assert view.lines[0].amount == MoneyIRR(1_000_000)
    assert view.lines[0].account == invoice_accounts[RECEIVABLE_ACCOUNT_CODE].ref
    assert view.lines[0].party == party
    assert view.lines[0].party_name == CUSTOMER_NAME
    assert view.lines[0].related_invoice is None
    assert view.lines[1].amount == MoneyIRR(-250_000)
    assert view.lines[1].account == invoice_accounts[PAYABLE_ACCOUNT_CODE].ref

    statements = adapter.list_bank_statements(company)
    assert BankStatementRef(statement_id) in [item.ref for item in statements]
    assert all(item.company == company for item in statements)
    drafts = adapter.list_bank_statements(company, state="draft")
    assert BankStatementRef(statement_id) in [item.ref for item in drafts]
    posted = adapter.list_bank_statements(company, state="posted")
    assert BankStatementRef(statement_id) not in [item.ref for item in posted]


def test_get_bank_statement_missing_returns_none(adapter):
    assert adapter.get_bank_statement(BankStatementRef(999_999_999)) is None


def test_list_filters_reject_bad_values(adapter, company):
    with pytest.raises(InvalidInvoiceError):
        adapter.list_invoices(company, invoice_type="sideways")
    with pytest.raises(InvalidInvoiceError):
        adapter.list_invoices(company, state="limbo")
    with pytest.raises(InvalidPaymentError):
        adapter.list_payments(company, kind="sideways")
    with pytest.raises(InvalidPaymentError):
        adapter.list_payments(company, state="limbo")
    with pytest.raises(InvalidBankStatementError):
        adapter.list_bank_statements(company, state="limbo")


# ---------------------------------------------------------------------------
# US-202 follow-up: draft list + update
# ---------------------------------------------------------------------------


def test_list_journal_drafts_returns_created_drafts_company_scoped(
    adapter, company, journal, accounts, created_moves
):
    debit_account, credit_account = accounts
    first = adapter.create_journal_draft(
        _draft_command(
            company,
            journal,
            debit_account.ref,
            credit_account.ref,
            description="US-202 follow-up list test (first)",
        )
    )
    created_moves.append(first.engine_id)
    second = adapter.create_journal_draft(
        _draft_command(
            company,
            journal,
            debit_account.ref,
            credit_account.ref,
            description="US-202 follow-up list test (second)",
        )
    )
    created_moves.append(second.engine_id)
    # A posted journal must never appear in the draft list.
    posted = adapter.create_journal_draft(
        _draft_command(
            company,
            journal,
            debit_account.ref,
            credit_account.ref,
            description="US-202 follow-up list test (posted)",
        )
    )
    adapter.post_journal(PostJournal(draft=posted))

    views = adapter.list_journal_drafts(company)
    ids = [view.ref.engine_id for view in views]
    assert first.engine_id in ids
    assert second.engine_id in ids
    assert posted.engine_id not in ids
    assert all(view.company == company for view in views)
    assert all(view.state == "draft" for view in views)
    # Newest first: same date, so the later-created draft sorts first.
    assert ids.index(second.engine_id) < ids.index(first.engine_id)
    by_id = {view.ref.engine_id: view for view in views}
    assert by_id[first.engine_id].description == "US-202 follow-up list test (first)"
    assert by_id[first.engine_id].total_debit == MoneyIRR(1_000_000)
    assert len(by_id[first.engine_id].lines) == 2


def test_update_draft_description_and_lines(
    adapter, company, journal, accounts, created_moves
):
    debit_account, credit_account = accounts
    ref = adapter.create_journal_draft(
        _draft_command(
            company,
            journal,
            debit_account.ref,
            credit_account.ref,
            description="US-202 follow-up update test",
        )
    )
    created_moves.append(ref.engine_id)

    updated = adapter.update_journal_draft(
        UpdateJournalDraft(
            draft=ref,
            description="US-202 follow-up update test (renamed)",
            lines=(
                JournalDraftLine(
                    account=debit_account.ref, debit=MoneyIRR(2_500_000)
                ),
                JournalDraftLine(
                    account=credit_account.ref, credit=MoneyIRR(2_500_000)
                ),
            ),
        )
    )
    assert updated.ref == ref
    assert updated.state == "draft"
    assert updated.description == "US-202 follow-up update test (renamed)"
    assert updated.date == MOVE_DATE  # unchanged field stays put
    assert updated.total_debit == MoneyIRR(2_500_000)
    assert updated.total_credit == MoneyIRR(2_500_000)
    assert updated.is_balanced
    assert all(line.state == "valid" for line in updated.lines), (
        "the engine must re-mark the rewritten lines 'valid'"
    )
    # The update re-validates: the draft is still postable afterwards.
    assert adapter.validate_journal_draft(ref).valid is True
    # And the read-back agrees with the returned view.
    assert adapter.get_journal_draft(ref) == updated


def test_update_draft_moves_date_to_another_open_period(
    adapter, company, journal, accounts, created_moves
):
    debit_account, credit_account = accounts
    ref = adapter.create_journal_draft(
        _draft_command(company, journal, debit_account.ref, credit_account.ref)
    )
    created_moves.append(ref.engine_id)

    updated = adapter.update_journal_draft(
        UpdateJournalDraft(draft=ref, date=DECEMBER_DATE)
    )
    assert updated.date == DECEMBER_DATE
    assert updated.period.name == "2026-12"
    assert updated.period.is_open
    assert adapter.validate_journal_draft(ref).valid is True


def test_update_draft_rejects_date_without_open_period(
    adapter, company, journal, accounts, created_moves
):
    debit_account, credit_account = accounts
    ref = adapter.create_journal_draft(
        _draft_command(company, journal, debit_account.ref, credit_account.ref)
    )
    created_moves.append(ref.engine_id)

    far_future = FiscalDate.from_gregorian(datetime.date(2031, 1, 15))
    with pytest.raises(PeriodClosedError) as excinfo:
        adapter.update_journal_draft(UpdateJournalDraft(draft=ref, date=far_future))
    assert excinfo.value.code == "LEDGER_PERIOD_CLOSED"
    # The failed update left the draft untouched.
    view = adapter.get_journal_draft(ref)
    assert view is not None
    assert view.date == MOVE_DATE


def test_update_posted_journal_raises_immutable(adapter, company, journal, accounts):
    debit_account, credit_account = accounts
    ref = adapter.create_journal_draft(
        _draft_command(company, journal, debit_account.ref, credit_account.ref)
    )
    adapter.post_journal(PostJournal(draft=ref))
    with pytest.raises(JournalAlreadyPostedError) as excinfo:
        adapter.update_journal_draft(
            UpdateJournalDraft(draft=ref, description="too late")
        )
    assert excinfo.value.code == "LEDGER_POSTED_IMMUTABLE"


def test_update_missing_draft_raises_stable_error(adapter):
    with pytest.raises(JournalDraftNotFoundError) as excinfo:
        adapter.update_journal_draft(
            UpdateJournalDraft(
                draft=JournalDraftRef(999_999_999), description="ghost"
            )
        )
    assert excinfo.value.code == "LEDGER_JOURNAL_NOT_FOUND"


def test_update_draft_rejects_unknown_account_and_keeps_original_lines(
    adapter, company, journal, accounts, created_moves
):
    debit_account, credit_account = accounts
    ref = adapter.create_journal_draft(
        _draft_command(company, journal, debit_account.ref, credit_account.ref)
    )
    created_moves.append(ref.engine_id)

    with pytest.raises(AccountNotFoundError) as excinfo:
        adapter.update_journal_draft(
            UpdateJournalDraft(
                draft=ref,
                lines=(
                    JournalDraftLine(
                        account=AccountRef(999_999_999), debit=MoneyIRR(500)
                    ),
                    JournalDraftLine(
                        account=credit_account.ref, credit=MoneyIRR(500)
                    ),
                ),
            )
        )
    assert excinfo.value.code == "LEDGER_ACCOUNT_NOT_FOUND"
    # The rejected update changed nothing: the original lines survive.
    view = adapter.get_journal_draft(ref)
    assert view is not None
    assert view.total_debit == MoneyIRR(1_000_000)
    assert {line.account for line in view.lines} == {
        debit_account.ref,
        credit_account.ref,
    }


def test_update_draft_rejects_unbalanced_lines(
    adapter, company, journal, accounts, created_moves
):
    debit_account, credit_account = accounts
    ref = adapter.create_journal_draft(
        _draft_command(company, journal, debit_account.ref, credit_account.ref)
    )
    created_moves.append(ref.engine_id)

    # The command fails at construction, before any engine call.
    with pytest.raises(UnbalancedJournalError) as excinfo:
        UpdateJournalDraft(
            draft=ref,
            lines=(
                JournalDraftLine(account=debit_account.ref, debit=MoneyIRR(100)),
                JournalDraftLine(account=credit_account.ref, credit=MoneyIRR(200)),
            ),
        )
    assert excinfo.value.code == "LEDGER_UNBALANCED"
    view = adapter.get_journal_draft(ref)
    assert view is not None
    assert view.total_debit == MoneyIRR(1_000_000)
