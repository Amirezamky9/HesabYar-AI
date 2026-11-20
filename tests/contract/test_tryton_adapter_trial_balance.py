"""Contract tests for the adapter trial-balance read (US-403).

These tests run against the LIVE trytond server and the
``hesabyar_tryton_test`` database, using ONLY the dedicated test company
"HesabYar Adapter Test". They never touch another company's records.

The trial balance must come from POSTED moves only: the tests cross-check
the adapter's aggregation against an independent ``account.move.line``
search and prove a freshly created DRAFT move never changes the figures.

When the server is not reachable (or the test company has not been
provisioned yet), every test SKIPS cleanly with a clear message.

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

import pytest

from hesabyar.domain.accounting.commands import CreateJournalDraft, JournalDraftLine
from hesabyar.domain.accounting.refs import CompanyRef, JournalRef
from hesabyar.domain.accounting.value_objects import AccountView
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.infrastructure.accounting.tryton.adapter import TrytonAccountingAdapter
from hesabyar.infrastructure.accounting.tryton.client import TrytonRestClient
from hesabyar.infrastructure.accounting.tryton.errors import (
    ENGINE_NOT_FOUND,
    TrytonAdapterError,
)

BASE_URL = os.environ.get("TRYTON_BASE_URL", "http://localhost:8000")
DATABASE = os.environ.get("TRYTON_DATABASE", "hesabyar_tryton_test")
API_KEY = os.environ.get("TRYTON_API_KEY", "hesabyar-dev-rest-key")
TEST_COMPANY_NAME = os.environ.get("TRYTON_TEST_COMPANY", "HesabYar Adapter Test")
TEST_JOURNAL_NAME = "HesabYar Adapter Test General Journal"

DEBIT_ACCOUNT_CODE = "1.1.1"
CREDIT_ACCOUNT_CODE = "1.1.2"

# A date inside FY 2026's open November period.
MOVE_DATE = FiscalDate.from_gregorian(datetime.date(2026, 11, 16))

WIDE_FROM = FiscalDate.from_gregorian(datetime.date(2026, 1, 1))
WIDE_TO = FiscalDate.from_gregorian(datetime.date(2026, 12, 31))


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
def accounts(
    adapter: TrytonAccountingAdapter, company: CompanyRef
) -> tuple[AccountView, AccountView]:
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


def _posted_lines(
    client: TrytonRestClient,
    company: CompanyRef,
    date_from: FiscalDate,
    date_to: FiscalDate,
) -> list[dict]:
    """Independent posted-move-line read (no paging limit, direct client)."""
    return client.search(
        "account.move.line",
        domain=[
            ("move.company", "=", company.engine_id),
            ("move.state", "=", "posted"),
            ("move.date", ">=", date_from.gregorian_date),
            ("move.date", "<=", date_to.gregorian_date),
        ],
        fields=["move", "account", "debit", "credit"],
        context={"company": company.engine_id},
    )


# ---------------------------------------------------------------------------
# Trial balance from posted moves
# ---------------------------------------------------------------------------


def test_trial_balance_matches_independent_posted_line_sum(
    adapter: TrytonAccountingAdapter, client: TrytonRestClient, company: CompanyRef
) -> None:
    view = adapter.get_trial_balance(company, WIDE_FROM, WIDE_TO)
    lines = _posted_lines(client, company, WIDE_FROM, WIDE_TO)
    assert view.line_count == len(lines)
    assert view.move_count == len({int(record["move"]) for record in lines})
    expected_debit = sum((Decimal(str(record["debit"])) for record in lines), Decimal(0))
    expected_credit = sum((Decimal(str(record["credit"])) for record in lines), Decimal(0))
    assert view.total_debit.amount == int(expected_debit)
    assert view.total_credit.amount == int(expected_credit)
    assert view.is_balanced is True
    assert view.source_endpoint == "account.move.line (Tryton REST)"
    assert view.as_of_date == WIDE_TO
    # Per-account totals match the independent sum.
    by_account: dict[int, tuple[int, int]] = {}
    for record in lines:
        account_id = int(record["account"])
        debit, credit = by_account.get(account_id, (0, 0))
        by_account[account_id] = (
            debit + int(Decimal(str(record["debit"]))),
            credit + int(Decimal(str(record["credit"]))),
        )
    assert {line.account.engine_id for line in view.lines} == set(by_account)
    for line in view.lines:
        assert (line.total_debit.amount, line.total_credit.amount) == by_account[line.account.engine_id]
        assert line.code, "trial-balance lines carry the account code"
        assert line.name, "trial-balance lines carry the account name"
    assert [line.code for line in view.lines] == sorted(line.code for line in view.lines)


def test_trial_balance_date_range_filters(
    adapter: TrytonAccountingAdapter, client: TrytonRestClient, company: CompanyRef
) -> None:
    day = FiscalDate.from_gregorian(datetime.date(2026, 11, 17))
    view = adapter.get_trial_balance(company, day, day)
    lines = _posted_lines(client, company, day, day)
    assert view.line_count == len(lines)
    assert view.move_count == len({int(record["move"]) for record in lines})
    assert view.total_debit.amount == sum(int(Decimal(str(r["debit"]))) for r in lines)
    assert view.total_credit.amount == sum(int(Decimal(str(r["credit"]))) for r in lines)


def test_trial_balance_excludes_draft_moves(
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    journal: JournalRef,
    accounts: tuple[AccountView, AccountView],
    created_moves: list[int],
) -> None:
    debit_account, credit_account = accounts
    before = adapter.get_trial_balance(company, MOVE_DATE, MOVE_DATE)
    draft = adapter.create_journal_draft(
        CreateJournalDraft(
            company=company,
            date=MOVE_DATE,
            lines=(
                JournalDraftLine(account=debit_account.ref, debit=MoneyIRR(777_001)),
                JournalDraftLine(account=credit_account.ref, credit=MoneyIRR(777_001)),
            ),
            description="US-403 contract test draft (must not enter the trial balance)",
            journal=journal,
        )
    )
    created_moves.append(draft.engine_id)
    # The draft exists in the engine — but the trial balance must not move.
    assert adapter.get_journal_draft(draft) is not None
    after = adapter.get_trial_balance(company, MOVE_DATE, MOVE_DATE)
    assert after.total_debit == before.total_debit
    assert after.total_credit == before.total_credit
    assert after.line_count == before.line_count
    assert after.move_count == before.move_count
    assert all(line.total_debit.amount != 777_001 for line in after.lines)


def test_trial_balance_unknown_company_raises(adapter: TrytonAccountingAdapter) -> None:
    with pytest.raises(TrytonAdapterError) as excinfo:
        adapter.get_trial_balance(
            CompanyRef(engine_id=999_999),
            WIDE_FROM,
            WIDE_TO,
        )
    assert excinfo.value.code == ENGINE_NOT_FOUND


def test_trial_balance_reversed_range_raises(
    adapter: TrytonAccountingAdapter, company: CompanyRef
) -> None:
    with pytest.raises(ValueError):
        adapter.get_trial_balance(company, WIDE_TO, WIDE_FROM)
