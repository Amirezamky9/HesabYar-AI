"""Contract tests for the adapter financial-report reads (US-550).

These tests run against the LIVE trytond server and the
``hesabyar_tryton_test`` database, using ONLY the dedicated test company
"HesabYar Adapter Test". They never touch another company's records.

Each report is cross-checked against an INDEPENDENT read: the general
ledger against a direct ``account.move.line`` search, the balance
sheet / cash flow against ``get_account_balance`` (which aggregates
posted lines directly, not through the ``account.account`` Function
fields the statements use), the P&L against an independent income-line
sum, and the notes balance against an independent open-line read with
the aged-balance rule applied in the test.

Cross-checks that compare the Function-field reads with direct line
aggregation assume every posted move on the test company falls in FY
2026 (the setup provisions FY 2026/2027 only): a Function-field
balance sees the FY containing the date plus closed years' deferrals,
while the direct read sees every posted line up to the date.

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
from hesabyar.domain.accounting.refs import AccountRef, CompanyRef, JournalRef
from hesabyar.domain.accounting.value_objects import AccountView
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.domain.reports.statements import aging_bucket
from hesabyar.infrastructure.accounting.tryton.adapter import TrytonAccountingAdapter
from hesabyar.infrastructure.accounting.tryton.client import TrytonRestClient
from hesabyar.infrastructure.accounting.tryton.errors import (
    ENGINE_NOT_FOUND,
    TrytonAdapterError,
)
from hesabyar.infrastructure.accounting.tryton.mapping import date_from_tryton

BASE_URL = os.environ.get("TRYTON_BASE_URL", "http://localhost:8000")
DATABASE = os.environ.get("TRYTON_DATABASE", "hesabyar_tryton_test")
API_KEY = os.environ.get("TRYTON_API_KEY", "hesabyar-dev-rest-key")
TEST_COMPANY_NAME = os.environ.get("TRYTON_TEST_COMPANY", "HesabYar Adapter Test")
TEST_JOURNAL_NAME = "HesabYar Adapter Test General Journal"

CASH_ACCOUNT_CODES = ("1.1.1", "1.1.3", "1.1.5", "1.1.6")
NOTES_RECEIVABLE_CODES = ("1.2.4", "1.2.5")
NOTES_PAYABLE_CODES = ("2.1.5",)

# November 2026 (a 1405 period with posted moves on 2026-11-17).
NOV_FROM = FiscalDate.from_gregorian(datetime.date(2026, 11, 1))
NOV_TO = FiscalDate.from_gregorian(datetime.date(2026, 11, 30))
DAY_BEFORE_NOV = FiscalDate.from_gregorian(datetime.date(2026, 10, 31))
MOVE_DATE = FiscalDate.from_gregorian(datetime.date(2026, 11, 16))


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
def chart(
    adapter: TrytonAccountingAdapter, company: CompanyRef
) -> dict[str, AccountView]:
    return {view.code: view for view in adapter.get_chart_of_accounts(company)}


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
    account_ids: list[int] | None = None,
) -> list[dict]:
    """Independent posted-move-line read (direct client, no paging limit)."""
    domain: list = [
        ("move.company", "=", company.engine_id),
        ("move.state", "=", "posted"),
        ("move.date", ">=", date_from.gregorian_date),
        ("move.date", "<=", date_to.gregorian_date),
    ]
    if account_ids is not None:
        domain.append(("account", "in", account_ids))
    return client.search(
        "account.move.line",
        domain=domain,
        fields=["move", "account", "debit", "credit", "date", "description"],
        context={"company": company.engine_id},
    )


def _int(value: object) -> int:
    return int(Decimal(str(value)))


# ---------------------------------------------------------------------------
# General ledger
# ---------------------------------------------------------------------------


def test_general_ledger_summary_matches_trial_balance(
    adapter: TrytonAccountingAdapter, company: CompanyRef
) -> None:
    ledger = adapter.get_general_ledger(company, NOV_FROM, NOV_TO)
    trial = adapter.get_trial_balance(company, NOV_FROM, NOV_TO)
    assert ledger.total_debit == trial.total_debit
    assert ledger.total_credit == trial.total_credit
    assert {a.code for a in ledger.accounts} == {line.code for line in trial.lines}
    assert ledger.move_count == 0 and ledger.line_count == 0, "summary mode has no lines"
    for account in ledger.accounts:
        assert account.lines == ()
        assert account.closing_balance.amount == (
            account.opening_balance.amount
            + account.total_debit.amount
            - account.total_credit.amount
        )


def test_general_ledger_detail_matches_independent_line_sum(
    adapter: TrytonAccountingAdapter,
    client: TrytonRestClient,
    company: CompanyRef,
) -> None:
    trial = adapter.get_trial_balance(company, NOV_FROM, NOV_TO)
    assert trial.lines, "November 2026 has posted moves"
    account = trial.lines[0].account
    ledger = adapter.get_general_ledger(company, NOV_FROM, NOV_TO, account)
    assert len(ledger.accounts) == 1
    section = ledger.accounts[0]
    lines = _posted_lines(
        client, company, NOV_FROM, NOV_TO, account_ids=[account.engine_id]
    )
    assert ledger.line_count == len(lines)
    assert ledger.move_count == len({int(record["move"]) for record in lines})
    assert section.total_debit.amount == sum(_int(r["debit"]) for r in lines)
    assert section.total_credit.amount == sum(_int(r["credit"]) for r in lines)
    # Opening balance cross-check: the direct cumulative read at the
    # day before the period (independent of the Function-field read).
    opening = adapter.get_account_balance(company, account, DAY_BEFORE_NOV)
    assert section.opening_balance == opening.balance
    running = section.opening_balance.amount
    for line in section.lines:
        running += line.debit.amount - line.credit.amount
        assert line.balance.amount == running
    assert section.closing_balance.amount == running


def test_general_ledger_excludes_draft_moves(
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    journal: JournalRef,
    chart: dict[str, AccountView],
    created_moves: list[int],
) -> None:
    if "1.1.1" not in chart or "1.1.2" not in chart:
        pytest.skip("Chart accounts 1.1.1/1.1.2 missing from test company.")
    before = adapter.get_general_ledger(company, MOVE_DATE, MOVE_DATE)
    draft = adapter.create_journal_draft(
        CreateJournalDraft(
            company=company,
            date=MOVE_DATE,
            lines=(
                JournalDraftLine(account=chart["1.1.1"].ref, debit=MoneyIRR(888_001)),
                JournalDraftLine(account=chart["1.1.2"].ref, credit=MoneyIRR(888_001)),
            ),
            description="US-550 contract test draft (must not enter the ledger)",
            journal=journal,
        )
    )
    created_moves.append(draft.engine_id)
    after = adapter.get_general_ledger(company, MOVE_DATE, MOVE_DATE)
    assert after.total_debit == before.total_debit
    assert after.total_credit == before.total_credit
    assert {a.code for a in after.accounts} == {a.code for a in before.accounts}


def test_general_ledger_unknown_account_raises(
    adapter: TrytonAccountingAdapter, company: CompanyRef
) -> None:
    with pytest.raises(TrytonAdapterError) as excinfo:
        adapter.get_general_ledger(
            company, NOV_FROM, NOV_TO, AccountRef(engine_id=999_999)
        )
    assert excinfo.value.code == ENGINE_NOT_FOUND


def test_general_ledger_reversed_range_raises(
    adapter: TrytonAccountingAdapter, company: CompanyRef
) -> None:
    with pytest.raises(ValueError):
        adapter.get_general_ledger(company, NOV_TO, NOV_FROM)


# ---------------------------------------------------------------------------
# Balance sheet
# ---------------------------------------------------------------------------


def test_balance_sheet_balances_and_matches_account_balances(
    adapter: TrytonAccountingAdapter, company: CompanyRef
) -> None:
    sheet = adapter.get_balance_sheet(company, NOV_TO)
    assert [section.key for section in sheet.sections] == ["assets", "liabilities", "equity"]
    assert sheet.total_assets.amount == sheet.total_liabilities.amount + sheet.total_equity.amount
    assert sheet.balanced is True
    # Spot-check one account line against the direct cumulative read.
    cash_lines = [
        line
        for section in sheet.sections
        for line in section.lines
        if line.kind == "account" and line.code == "1.1.1"
    ]
    if cash_lines:
        balance = adapter.get_account_balance(
            company, AccountRef(cash_lines[0].ref_id), NOV_TO
        )
        assert cash_lines[0].amount == balance.balance


def test_balance_sheet_comparison_matches_account_balances(
    adapter: TrytonAccountingAdapter, company: CompanyRef
) -> None:
    comparison = FiscalDate.from_gregorian(datetime.date(2025, 11, 30))
    sheet = adapter.get_balance_sheet(company, NOV_TO, comparison)
    assert sheet.comparison_as_of == comparison
    assert sheet.balanced_cmp is True
    assert sheet.total_assets_cmp is not None
    assert (
        sheet.total_assets_cmp.amount
        == (sheet.total_liabilities_cmp or MoneyIRR(0)).amount
        + (sheet.total_equity_cmp or MoneyIRR(0)).amount
    )


# ---------------------------------------------------------------------------
# Income statement
# ---------------------------------------------------------------------------


def test_income_statement_matches_independent_income_line_sum(
    adapter: TrytonAccountingAdapter,
    client: TrytonRestClient,
    company: CompanyRef,
) -> None:
    # Income accounts: types whose statement is 'income', then the
    # accounts on those types (AccountView carries no type id).
    income_types = {
        int(record["id"])
        for record in client.search(
            "account.account.type",
            domain=[("statement", "=", "income")],
            fields=["statement"],
            context={"company": company.engine_id},
        )
    }
    income_account_records = client.search(
        "account.account",
        domain=[
            ("company", "=", company.engine_id),
            ("type", "in", sorted(income_types)),
        ],
        fields=["type"],
        context={"company": company.engine_id},
    )
    type_of = {
        int(record["id"]): int(record["type"]) for record in income_account_records
    }
    statement = adapter.get_income_statement(company, NOV_FROM, NOV_TO)
    assert [section.key for section in statement.sections] == ["revenue", "expenses"]
    assert statement.net_income.amount == statement.total_revenue.amount - statement.total_expense.amount
    assert statement.monthly_ties_out is True
    assert sum(m.revenue.amount for m in statement.monthly) == statement.total_revenue.amount
    assert sum(m.expense.amount for m in statement.monthly) == statement.total_expense.amount
    assert [m.jalali_month for m in statement.monthly] == ["1405-08", "1405-09"]
    # Independent totals: net per income TYPE, classified by net sign.
    lines = _posted_lines(
        client,
        company,
        NOV_FROM,
        NOV_TO,
        account_ids=sorted(type_of),
    )
    net_by_type: dict[int, int] = {}
    for record in lines:
        type_id = type_of[int(record["account"])]
        net_by_type[type_id] = net_by_type.get(type_id, 0) + (
            _int(record["debit"]) - _int(record["credit"])
        )
    revenue = sum(-net for net in net_by_type.values() if net < 0)
    expense = sum(net for net in net_by_type.values() if net > 0)
    assert statement.total_revenue.amount == revenue
    assert statement.total_expense.amount == expense


def test_income_statement_reversed_range_raises(
    adapter: TrytonAccountingAdapter, company: CompanyRef
) -> None:
    with pytest.raises(ValueError):
        adapter.get_income_statement(company, NOV_TO, NOV_FROM)


# ---------------------------------------------------------------------------
# Cash flow
# ---------------------------------------------------------------------------


def test_cash_flow_ties_out_and_matches_cash_balances(
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    chart: dict[str, AccountView],
) -> None:
    cash = [chart[code] for code in CASH_ACCOUNT_CODES if code in chart]
    if not cash:
        pytest.skip("No cash accounts on the test company chart.")
    flow = adapter.get_cash_flow(company, NOV_FROM, NOV_TO)
    assert [section.key for section in flow.sections] == ["operating", "investing", "financing"]
    assert flow.ties_out is True
    assert flow.net_change.amount == flow.closing_cash.amount - flow.opening_cash.amount
    assert flow.net_change.amount == sum(s.net.amount for s in flow.sections)
    opening = sum(
        adapter.get_account_balance(company, view.ref, DAY_BEFORE_NOV).balance.amount
        for view in cash
    )
    closing = sum(
        adapter.get_account_balance(company, view.ref, NOV_TO).balance.amount
        for view in cash
    )
    assert flow.opening_cash.amount == opening
    assert flow.closing_cash.amount == closing


def test_cash_flow_reversed_range_raises(
    adapter: TrytonAccountingAdapter, company: CompanyRef
) -> None:
    with pytest.raises(ValueError):
        adapter.get_cash_flow(company, NOV_TO, NOV_FROM)


# ---------------------------------------------------------------------------
# Notes balance (cheque report 7)
# ---------------------------------------------------------------------------


def test_notes_balance_matches_independent_open_line_read(
    adapter: TrytonAccountingAdapter,
    client: TrytonRestClient,
    company: CompanyRef,
    chart: dict[str, AccountView],
) -> None:
    notes_codes = NOTES_RECEIVABLE_CODES + NOTES_PAYABLE_CODES
    notes = [chart[code] for code in notes_codes if code in chart]
    if not notes:
        pytest.skip("No notes accounts on the test company chart.")
    notes_ids = [view.ref.engine_id for view in notes]
    report = adapter.get_notes_balance(company, NOV_TO)
    assert set(report.accounts_read) == set(notes_codes)
    # Independent read: posted lines on the notes accounts up to as-of.
    lines = client.search(
        "account.move.line",
        domain=[
            ("move.company", "=", company.engine_id),
            ("move.state", "=", "posted"),
            ("move.date", "<=", NOV_TO.gregorian_date),
            ("account", "in", notes_ids),
        ],
        fields=["account", "party", "maturity_date", "date", "debit", "credit", "reconciliation"],
        context={"company": company.engine_id},
    )
    reconciliation_ids = sorted(
        {int(line["reconciliation"]) for line in lines if line.get("reconciliation") is not None}
    )
    recon_dates: dict[int, FiscalDate] = {}
    if reconciliation_ids:
        for record in client.search(
            "account.move.reconciliation",
            domain=[("id", "in", reconciliation_ids)],
            fields=["date"],
        ):
            recon_dates[int(record["id"])] = date_from_tryton(record["date"])
    # Group open lines by (party, account) with the aged-balance rule.
    expected: dict[tuple[int | None, int], dict[str, int]] = {}
    for line in lines:
        recon_id = line.get("reconciliation")
        if recon_id is not None:
            recon_date = recon_dates.get(int(recon_id))
            if recon_date is not None and recon_date.gregorian_date <= NOV_TO.gregorian_date:
                continue
        maturity = line.get("maturity_date")
        base = date_from_tryton(maturity) if maturity is not None else date_from_tryton(line["date"])
        age = (NOV_TO.gregorian_date - base.gregorian_date).days
        bucket = aging_bucket(age)
        account_id = int(line["account"])
        code = next(view.code for view in notes if view.ref.engine_id == account_id)
        sign = 1 if code in NOTES_RECEIVABLE_CODES else -1
        amount = sign * (_int(line["debit"]) - _int(line["credit"]))
        key = (
            int(line["party"]) if line.get("party") is not None else None,
            account_id,
        )
        row = expected.setdefault(key, {"total": 0, "line_count": 0})
        row[bucket] = row.get(bucket, 0) + amount
        row["total"] += amount
        row["line_count"] += 1
    assert len(report.rows) == len(expected)
    for row in report.rows:
        key = (row.party_id, next(view.ref.engine_id for view in notes if view.code == row.account_code))
        assert key in expected, f"unexpected row {row.party_name} {row.account_code}"
        want = expected[key]
        assert row.total.amount == want["total"]
        assert row.line_count == want["line_count"]
        assert row.bucket_not_yet_due.amount == want.get("not_yet_due", 0)
        assert row.bucket_0_30.amount == want.get("bucket_0_30", 0)
        assert row.bucket_30_60.amount == want.get("bucket_30_60", 0)
        assert row.bucket_60_90.amount == want.get("bucket_60_90", 0)
        assert row.bucket_90_120.amount == want.get("bucket_90_120", 0)
        assert row.bucket_120_plus.amount == want.get("bucket_120_plus", 0)
    # Buckets always sum to the row total, and to the totals row.
    for row in (*report.rows, report.totals):
        assert row is not None
        assert row.total.amount == (
            row.bucket_not_yet_due.amount
            + row.bucket_0_30.amount
            + row.bucket_30_60.amount
            + row.bucket_60_90.amount
            + row.bucket_90_120.amount
            + row.bucket_120_plus.amount
        )


def test_notes_balance_instrument_filter(
    adapter: TrytonAccountingAdapter, company: CompanyRef
) -> None:
    report = adapter.get_notes_balance(company, NOV_TO, instrument="payable")
    assert report.instrument == "payable"
    assert all(row.instrument == "payable" for row in report.rows)
    assert set(report.accounts_read) == set(NOTES_PAYABLE_CODES)


def test_notes_balance_bad_instrument_raises(
    adapter: TrytonAccountingAdapter, company: CompanyRef
) -> None:
    with pytest.raises(ValueError):
        adapter.get_notes_balance(company, NOV_TO, instrument="other")


# ---------------------------------------------------------------------------
# Unknown company
# ---------------------------------------------------------------------------


def test_financial_reports_unknown_company_raises(adapter: TrytonAccountingAdapter) -> None:
    unknown = CompanyRef(engine_id=999_999)
    with pytest.raises(TrytonAdapterError) as excinfo:
        adapter.get_balance_sheet(unknown, NOV_TO)
    assert excinfo.value.code == ENGINE_NOT_FOUND
    with pytest.raises(TrytonAdapterError) as excinfo:
        adapter.get_cash_flow(unknown, NOV_FROM, NOV_TO)
    assert excinfo.value.code == ENGINE_NOT_FOUND
    with pytest.raises(TrytonAdapterError) as excinfo:
        adapter.get_notes_balance(unknown, NOV_TO)
    assert excinfo.value.code == ENGINE_NOT_FOUND
