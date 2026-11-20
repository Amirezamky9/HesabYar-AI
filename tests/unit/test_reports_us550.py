"""Unit tests for the US-550 financial reports.

Pure builder tests (statement sections, cash-flow classification,
aging buckets, Jalali months) plus service tests over a fake engine
(freshness stamping, Jalali comparison defaults, validation, unknown
company). The adapter's own reads are contract-tested against live
Tryton separately (tests/contract/test_tryton_adapter_financial_reports.py).
"""

from __future__ import annotations

import datetime
from collections.abc import Callable

import jdatetime
import pytest

from hesabyar.application.errors import CompanyNotFoundError
from hesabyar.application.queries.reports import ReportQueryService
from hesabyar.domain.accounting.refs import AccountRef, CompanyRef, PartyRef
from hesabyar.domain.accounting.value_objects import (
    BalanceSheetView,
    CashFlowView,
    CompanyView,
    GeneralLedgerView,
    IncomeStatementView,
    MoneyIRR,
    NotesBalanceView,
    TrialBalanceView,
)
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.reports.statements import (
    CALCULATION_VERSIONS,
    AccountBalanceRow,
    AccountTypeNode,
    aging_bucket,
    build_balance_sheet_sections,
    build_income_statement_sections,
    classify_cash_flow_section,
    classify_income_types,
    jalali_months_in_range,
)
from tests.unit.test_report_services import FakeReportEngine

COMPANY = CompanyRef(1)
UNKNOWN = CompanyRef(99)


def _company() -> CompanyView:
    return CompanyView(ref=COMPANY, name="Acme", currency_code="IRR")


def _types() -> list[AccountTypeNode]:
    """A small tree mirroring the universal chart's shape.

    The income types sit UNDER Equity (profit/loss), like the real
    chart — the balance-sheet builder must not trip over them.
    """
    return [
        AccountTypeNode(1, "Universal Chart of Account Types", None, None, 0),
        AccountTypeNode(2, "Assets", 1, "balance", 1),
        AccountTypeNode(3, "Cash and Cash Equivalents", 2, "balance", 1),
        AccountTypeNode(4, "Accounts Receivable", 2, "balance", 2),
        AccountTypeNode(5, "Liabilities", 1, "balance", 2),
        AccountTypeNode(6, "Accounts Payable", 5, "balance", 1),
        AccountTypeNode(7, "Equity / capital", 1, "balance", 3),
        AccountTypeNode(8, "Capital", 7, "balance", 1),
        AccountTypeNode(9, "Current Period Profit or Lost", 7, "income", 2),
        AccountTypeNode(10, "Revenue", 9, "income", 1),
        AccountTypeNode(11, "Expenses", 9, "income", 2),
        AccountTypeNode(12, "Off-Balance", 1, "off-balance", 4),
        AccountTypeNode(13, "Memorandum", 12, "off-balance", 1),
    ]


def _row(
    account_id: int,
    code: str,
    name: str,
    type_id: int,
    balance: int,
    balance_cmp: int | None = None,
) -> AccountBalanceRow:
    return AccountBalanceRow(
        account_id=account_id,
        code=code,
        name=name,
        type_id=type_id,
        balance=MoneyIRR(balance),
        balance_cmp=MoneyIRR(balance_cmp) if balance_cmp is not None else None,
    )


def _accounts() -> list[AccountBalanceRow]:
    """A balanced ledger: debits 1,800,000 = credits 1,800,000."""
    return [
        _row(11, "1.1.1", "Cash", 3, 1_000_000, 1_000_000),
        _row(12, "1.2.1", "Receivables", 4, 500_000, 400_000),
        _row(21, "2.1.1", "Payables", 6, -400_000, -350_000),
        _row(31, "3.1.1", "Capital", 8, -800_000, -800_000),
        _row(41, "4.1.1", "Sales", 10, -600_000, -500_000),
        _row(51, "5.1.1", "Rent", 11, 300_000, 250_000),
    ]


# ---------------------------------------------------------------------------
# Statement builders
# ---------------------------------------------------------------------------


def test_balance_sheet_sections_totals_and_balance_check() -> None:
    result = build_balance_sheet_sections(_types(), _accounts())
    assert result.total_assets.amount == 1_500_000
    assert result.total_liabilities.amount == 400_000
    # Equity = capital 800,000 + current-period profit 300,000.
    assert result.total_equity.amount == 1_100_000
    assert result.balanced is True
    assert [section.key for section in result.sections] == ["assets", "liabilities", "equity"]


def test_balance_sheet_comparison_totals() -> None:
    result = build_balance_sheet_sections(_types(), _accounts())
    assert result.total_assets_cmp is not None and result.total_assets_cmp.amount == 1_400_000
    assert result.total_liabilities_cmp is not None and result.total_liabilities_cmp.amount == 350_000
    assert result.total_equity_cmp is not None and result.total_equity_cmp.amount == 1_050_000
    assert result.balanced_cmp is True


def test_balance_sheet_current_period_result_line_in_equity() -> None:
    result = build_balance_sheet_sections(_types(), _accounts())
    equity = next(section for section in result.sections if section.key == "equity")
    profit = [line for line in equity.lines if line.label == "Current Period Profit or Lost"]
    assert len(profit) == 1
    assert profit[0].amount.amount == 300_000
    assert profit[0].amount_cmp is not None and profit[0].amount_cmp.amount == 250_000
    assert profit[0].ref_id == 9


def test_balance_sheet_net_loss_is_negative_equity_line() -> None:
    accounts = _accounts()
    # Flip the result to a loss: expenses exceed revenue.
    accounts = [
        _row(11, "1.1.1", "Cash", 3, 400_000),
        _row(21, "2.1.1", "Payables", 6, -100_000),
        _row(31, "3.1.1", "Capital", 8, -800_000),
        _row(41, "4.1.1", "Sales", 10, -200_000),
        _row(51, "5.1.1", "Rent", 11, 700_000),
    ]
    result = build_balance_sheet_sections(_types(), accounts)
    equity = next(section for section in result.sections if section.key == "equity")
    profit = [line for line in equity.lines if line.label == "Current Period Profit or Lost"]
    assert len(profit) == 1
    assert profit[0].amount.amount == -500_000
    assert result.total_equity.amount == 300_000
    assert result.balanced is True


def test_balance_sheet_excludes_off_balance_accounts() -> None:
    accounts = [*_accounts(), _row(71, "7.1.1", "Memorandum", 13, 999_999)]
    result = build_balance_sheet_sections(_types(), accounts)
    assert result.total_assets.amount == 1_500_000
    assert result.balanced is True
    labels = [line.label for section in result.sections for line in section.lines]
    assert "Memorandum" not in labels


def test_balance_sheet_drops_zero_rows() -> None:
    accounts = [*_accounts(), _row(14, "1.1.9", "Empty account", 3, 0)]
    result = build_balance_sheet_sections(_types(), accounts)
    labels = [line.label for section in result.sections for line in section.lines]
    assert "Empty account" not in labels


def test_balance_sheet_keeps_zero_row_with_nonzero_comparison() -> None:
    accounts = [*_accounts(), _row(14, "1.1.9", "Closed account", 3, 0, 50_000)]
    result = build_balance_sheet_sections(_types(), accounts)
    assets = next(section for section in result.sections if section.key == "assets")
    closed = [line for line in assets.lines if line.label == "Closed account"]
    assert len(closed) == 1
    assert closed[0].amount.amount == 0
    assert closed[0].amount_cmp is not None and closed[0].amount_cmp.amount == 50_000


def test_classify_income_types_by_net_sign() -> None:
    classification = classify_income_types(_types(), _accounts())
    assert classification[10] == "revenue"
    assert classification[11] == "expense"


def test_classify_income_types_sign_flip() -> None:
    # A "Revenue" type with a net debit over the period is an expense:
    # classification is data-driven, not name-driven.
    accounts = [_row(41, "4.1.1", "Sales", 10, 100)]
    assert classify_income_types(_types(), accounts) == {10: "expense"}


def test_classify_income_types_zero_net_is_neither() -> None:
    accounts = [
        _row(41, "4.1.1", "Sales", 10, 100),
        _row(42, "4.2.1", "Other income", 10, -100),
        _row(51, "5.1.1", "Rent", 11, 0),
    ]
    assert classify_income_types(_types(), accounts) == {}


def test_income_statement_sections_and_net_income() -> None:
    result = build_income_statement_sections(_types(), _accounts())
    assert result.total_revenue.amount == 600_000
    assert result.total_expense.amount == 300_000
    assert result.net_income.amount == 300_000
    assert result.total_revenue_cmp is not None and result.total_revenue_cmp.amount == 500_000
    assert result.total_expense_cmp is not None and result.total_expense_cmp.amount == 250_000
    assert result.net_income_cmp is not None and result.net_income_cmp.amount == 250_000
    assert [section.key for section in result.sections] == ["revenue", "expenses"]


def test_income_statement_net_loss() -> None:
    accounts = [
        _row(41, "4.1.1", "Sales", 10, -200_000),
        _row(51, "5.1.1", "Rent", 11, 700_000),
    ]
    result = build_income_statement_sections(_types(), accounts)
    assert result.net_income.amount == -500_000


# ---------------------------------------------------------------------------
# Cash-flow classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("1.1.2", "investing"),
        ("1.1.4", "investing"),
        ("1.1.1", "transfer"),
        ("1.1.3", "transfer"),
        ("1.1.5", "transfer"),
        ("1.1.6", "transfer"),
        ("1.5.1", "investing"),
        ("1.6.2", "investing"),
        ("1.9.9", "investing"),
        ("2.3.1", "financing"),
        ("2.3.9", "financing"),
        ("3.1.1", "financing"),
        ("3.9", "financing"),
        ("7.1.1", "unclassified"),
        ("7.9", "unclassified"),
        ("1.2.1", "operating"),
        ("1.3.1", "operating"),
        ("1.4.1", "operating"),
        ("2.1.1", "operating"),
        ("2.2.0", "operating"),
        ("2.4.1", "operating"),
        ("4.1.1", "operating"),
        ("5.1.1", "operating"),
        ("6.1.1", "operating"),
        ("", "unclassified"),
        ("9.9.9", "unclassified"),
        (" 1.1.2 ", "investing"),
    ],
)
def test_classify_cash_flow_section(code: str, expected: str) -> None:
    assert classify_cash_flow_section(code) == expected


# ---------------------------------------------------------------------------
# Aging buckets
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("age_days", "expected"),
    [
        (-30, "not_yet_due"),
        (-1, "not_yet_due"),
        (0, "bucket_0_30"),
        (29, "bucket_0_30"),
        (30, "bucket_30_60"),
        (59, "bucket_30_60"),
        (60, "bucket_60_90"),
        (89, "bucket_60_90"),
        (90, "bucket_90_120"),
        (119, "bucket_90_120"),
        (120, "bucket_120_plus"),
        (365, "bucket_120_plus"),
    ],
)
def test_aging_bucket_boundaries(age_days: int, expected: str) -> None:
    assert aging_bucket(age_days) == expected


# ---------------------------------------------------------------------------
# Jalali months
# ---------------------------------------------------------------------------


def test_jalali_months_single_month_clipped() -> None:
    months = jalali_months_in_range(
        FiscalDate(datetime.date(2026, 3, 25)),
        FiscalDate(datetime.date(2026, 3, 28)),
    )
    assert len(months) == 1
    label, start, end = months[0]
    assert label == "1405-01"
    assert start.gregorian_date == datetime.date(2026, 3, 25)
    assert end.gregorian_date == datetime.date(2026, 3, 28)


def test_jalali_months_multi_month_with_clipping() -> None:
    months = jalali_months_in_range(
        FiscalDate(datetime.date(2026, 3, 21)),
        FiscalDate(datetime.date(2026, 6, 15)),
    )
    assert [label for label, _, _ in months] == ["1405-01", "1405-02", "1405-03"]
    assert months[1][1].gregorian_date == datetime.date(2026, 4, 21)
    assert months[1][2].gregorian_date == datetime.date(2026, 5, 21)
    assert months[2][2].gregorian_date == datetime.date(2026, 6, 15)


def test_jalali_months_leap_esfand_has_30_days() -> None:
    start = jdatetime.date(1403, 12, 1).togregorian()
    end = jdatetime.date(1403, 12, 30).togregorian()
    months = jalali_months_in_range(FiscalDate(start), FiscalDate(end))
    assert [label for label, _, _ in months] == ["1403-12"]
    assert (months[0][2].gregorian_date - months[0][1].gregorian_date).days == 29


def test_jalali_months_common_esfand_has_29_days() -> None:
    start = jdatetime.date(1404, 12, 1).togregorian()
    end = jdatetime.date(1404, 12, 29).togregorian()
    months = jalali_months_in_range(FiscalDate(start), FiscalDate(end))
    assert [label for label, _, _ in months] == ["1404-12"]
    assert (months[0][2].gregorian_date - months[0][1].gregorian_date).days == 28


def test_jalali_months_year_boundary() -> None:
    months = jalali_months_in_range(
        FiscalDate(datetime.date(2026, 3, 19)),
        FiscalDate(datetime.date(2026, 3, 22)),
    )
    assert [label for label, _, _ in months] == ["1404-12", "1405-01"]
    assert months[0][2].gregorian_date == datetime.date(2026, 3, 20)
    assert months[1][1].gregorian_date == datetime.date(2026, 3, 21)


def test_jalali_months_empty_range() -> None:
    assert (
        jalali_months_in_range(
            FiscalDate(datetime.date(2026, 3, 28)),
            FiscalDate(datetime.date(2026, 3, 25)),
        )
        == []
    )


# ---------------------------------------------------------------------------
# Service: freshness, comparison defaults, validation
# ---------------------------------------------------------------------------


def _d(year: int, month: int, day: int) -> datetime.date:
    return datetime.date(year, month, day)


class FakeFinancialReportEngine(FakeReportEngine):
    """FakeReportEngine with canned US-550 views + recorded calls."""

    def __init__(self, companies: list[CompanyView], views: dict[str, object]) -> None:
        super().__init__(companies=companies)
        self._views = views
        self.calls: dict[str, tuple[object, ...]] = {}

    def get_general_ledger(
        self,
        company: CompanyRef,
        date_from: FiscalDate,
        date_to: FiscalDate,
        account: AccountRef | None = None,
    ) -> GeneralLedgerView:
        self.calls["general_ledger"] = (company, date_from, date_to, account)
        return self._views["general_ledger"]  # type: ignore[return-value]

    def get_balance_sheet(
        self,
        company: CompanyRef,
        as_of: FiscalDate,
        comparison_as_of: FiscalDate | None = None,
    ) -> BalanceSheetView:
        self.calls["balance_sheet"] = (company, as_of, comparison_as_of)
        return self._views["balance_sheet"]  # type: ignore[return-value]

    def get_income_statement(
        self,
        company: CompanyRef,
        date_from: FiscalDate,
        date_to: FiscalDate,
        comparison_from: FiscalDate | None = None,
        comparison_to: FiscalDate | None = None,
    ) -> IncomeStatementView:
        self.calls["income_statement"] = (company, date_from, date_to, comparison_from, comparison_to)
        return self._views["income_statement"]  # type: ignore[return-value]

    def get_cash_flow(
        self,
        company: CompanyRef,
        date_from: FiscalDate,
        date_to: FiscalDate,
    ) -> CashFlowView:
        self.calls["cash_flow"] = (company, date_from, date_to)
        return self._views["cash_flow"]  # type: ignore[return-value]

    def get_notes_balance(
        self,
        company: CompanyRef,
        as_of: FiscalDate,
        instrument: str = "both",
        party: PartyRef | None = None,
    ) -> NotesBalanceView:
        self.calls["notes_balance"] = (company, as_of, instrument, party)
        return self._views["notes_balance"]  # type: ignore[return-value]


def _views() -> dict[str, object]:
    period = (FiscalDate(_d(2026, 3, 21)), FiscalDate(_d(2026, 6, 15)))
    as_of = FiscalDate(_d(2026, 6, 15))
    return {
        "general_ledger": GeneralLedgerView(COMPANY, period[0], period[1]),
        "balance_sheet": BalanceSheetView(COMPANY, as_of, None),
        "income_statement": IncomeStatementView(COMPANY, period[0], period[1], None, None),
        "cash_flow": CashFlowView(COMPANY, period[0], period[1], MoneyIRR(0), MoneyIRR(0)),
        "notes_balance": NotesBalanceView(COMPANY, as_of, "both"),
    }


def _service() -> tuple[ReportQueryService, FakeFinancialReportEngine]:
    engine = FakeFinancialReportEngine([_company()], _views())
    return ReportQueryService(engine), engine


_ReportView = (
    GeneralLedgerView
    | BalanceSheetView
    | IncomeStatementView
    | CashFlowView
    | NotesBalanceView
)


@pytest.mark.parametrize(
    ("report_key", "call"),
    [
        ("general_ledger", lambda s: s.get_general_ledger(COMPANY, _d(2026, 3, 21), _d(2026, 6, 15))),
        ("balance_sheet", lambda s: s.get_balance_sheet(COMPANY, _d(2026, 6, 15))),
        ("income_statement", lambda s: s.get_income_statement(COMPANY, _d(2026, 3, 21), _d(2026, 6, 15))),
        ("cash_flow", lambda s: s.get_cash_flow(COMPANY, _d(2026, 3, 21), _d(2026, 6, 15))),
        ("notes_balance", lambda s: s.get_notes_balance(COMPANY, _d(2026, 6, 15))),
    ],
)
def test_service_stamps_freshness_on_every_report(
    report_key: str, call: Callable[[ReportQueryService], _ReportView]
) -> None:
    service, _ = _service()
    view = call(service)
    assert view.freshness is not None
    freshness = view.freshness
    assert freshness.source == f"tryton:{view.source_endpoint}"
    assert freshness.source_timestamp == freshness.generated_at
    assert freshness.company_id == "1"
    assert freshness.report_version == "1.0.0"
    assert freshness.calculation_version == CALCULATION_VERSIONS[report_key]
    assert freshness.freshness_indicator == "live"
    assert freshness.reproducibility["engine"] == "tryton"
    assert freshness.reproducibility["engine_version"] == "8.2.x"
    assert freshness.reproducibility["source_endpoint"] == view.source_endpoint
    assert freshness.reproducibility["company_id"] == "1"


def test_service_trial_balance_freshness_stamped() -> None:
    trial_balance = TrialBalanceView(
        COMPANY, FiscalDate(_d(2026, 3, 21)), FiscalDate(_d(2026, 6, 15))
    )
    engine = FakeReportEngine(companies=[_company()], trial_balance=trial_balance)
    service = ReportQueryService(engine)
    view = service.get_trial_balance(COMPANY, _d(2026, 3, 21), _d(2026, 6, 15))
    assert view.freshness is not None
    assert view.freshness.calculation_version == CALCULATION_VERSIONS["trial_balance"]
    assert view.freshness.period_start is not None
    assert view.freshness.period_start.gregorian_date == _d(2026, 3, 21)
    assert view.freshness.period_end is not None
    assert view.freshness.period_end.gregorian_date == _d(2026, 6, 15)


def test_service_balance_sheet_comparison_defaults_one_jalali_year_earlier() -> None:
    service, engine = _service()
    as_of = jdatetime.date(1405, 8, 15).togregorian()
    service.get_balance_sheet(COMPANY, as_of)
    _, _, comparison = engine.calls["balance_sheet"]
    assert isinstance(comparison, FiscalDate)
    assert comparison.gregorian_date == jdatetime.date(1404, 8, 15).togregorian()


def test_service_balance_sheet_comparison_esfand_30_clamps() -> None:
    service, engine = _service()
    # 1403-12-30 exists (leap year); 1404 has only 29 Esfand days.
    as_of = jdatetime.date(1403, 12, 30).togregorian()
    service.get_balance_sheet(COMPANY, as_of)
    _, _, comparison = engine.calls["balance_sheet"]
    assert isinstance(comparison, FiscalDate)
    assert comparison.gregorian_date == jdatetime.date(1404, 12, 29).togregorian()


def test_service_balance_sheet_explicit_comparison_passes_through() -> None:
    service, engine = _service()
    service.get_balance_sheet(COMPANY, _d(2026, 6, 15), _d(2025, 6, 15))
    _, _, comparison = engine.calls["balance_sheet"]
    assert isinstance(comparison, FiscalDate)
    assert comparison.gregorian_date == _d(2025, 6, 15)


def test_service_income_statement_comparison_defaults_one_jalali_year_earlier() -> None:
    service, engine = _service()
    date_from = jdatetime.date(1405, 1, 1).togregorian()
    date_to = jdatetime.date(1405, 3, 31).togregorian()
    service.get_income_statement(COMPANY, date_from, date_to)
    _, _, _, comparison_from, comparison_to = engine.calls["income_statement"]
    assert isinstance(comparison_from, FiscalDate)
    assert comparison_from.gregorian_date == jdatetime.date(1404, 1, 1).togregorian()
    assert isinstance(comparison_to, FiscalDate)
    assert comparison_to.gregorian_date == jdatetime.date(1404, 3, 31).togregorian()


def test_service_reproducibility_carries_filters() -> None:
    service, _ = _service()
    view = service.get_general_ledger(
        COMPANY, _d(2026, 3, 21), _d(2026, 6, 15), AccountRef(42)
    )
    assert view.freshness is not None
    assert view.freshness.reproducibility["account_id"] == 42
    notes = service.get_notes_balance(
        COMPANY, _d(2026, 6, 15), "receivable", PartyRef(7)
    )
    assert notes.freshness is not None
    assert notes.freshness.reproducibility["instrument"] == "receivable"
    assert notes.freshness.reproducibility["party_id"] == 7


@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.get_trial_balance(COMPANY, _d(2026, 6, 15), _d(2026, 3, 21)),
        lambda s: s.get_general_ledger(COMPANY, _d(2026, 6, 15), _d(2026, 3, 21)),
        lambda s: s.get_income_statement(COMPANY, _d(2026, 6, 15), _d(2026, 3, 21)),
        lambda s: s.get_cash_flow(COMPANY, _d(2026, 6, 15), _d(2026, 3, 21)),
    ],
)
def test_service_reversed_range_raises(call: Callable[[ReportQueryService], object]) -> None:
    service, _ = _service()
    with pytest.raises(ValueError, match="date_to must be on or after date_from"):
        call(service)


def test_service_income_statement_mismatched_comparison_raises() -> None:
    service, _ = _service()
    with pytest.raises(ValueError, match="go together"):
        service.get_income_statement(
            COMPANY, _d(2026, 3, 21), _d(2026, 6, 15), comparison_from=_d(2025, 3, 21)
        )


def test_service_income_statement_reversed_comparison_raises() -> None:
    service, _ = _service()
    with pytest.raises(ValueError, match="comparison_to must be on or after"):
        service.get_income_statement(
            COMPANY,
            _d(2026, 3, 21),
            _d(2026, 6, 15),
            comparison_from=_d(2025, 6, 15),
            comparison_to=_d(2025, 3, 21),
        )


def test_service_notes_balance_bad_instrument_raises() -> None:
    service, _ = _service()
    with pytest.raises(ValueError, match="instrument must be"):
        service.get_notes_balance(COMPANY, _d(2026, 6, 15), "both-ways")


def test_service_unknown_company_raises() -> None:
    engine = FakeFinancialReportEngine([], _views())
    service = ReportQueryService(engine)
    with pytest.raises(CompanyNotFoundError):
        service.get_balance_sheet(UNKNOWN, _d(2026, 6, 15))
    with pytest.raises(CompanyNotFoundError):
        service.get_notes_balance(UNKNOWN, _d(2026, 6, 15))
