"""Concurrent report reads (US-702, spec §17 phase 7).

Live-Tryton contract tests: several threads read the same reports
for the same company and period at once, each through its own REST
client + adapter (the production shape — the REST layer builds a
short-lived client per request). Every thread must see the SAME
figures: concurrent reads never return partial, mixed or stale
results, and no read fails.

Read-only: no cleanup. The November 2026 period is the one the
financial-reports contract tests use; the assertion is agreement
between threads, which holds however many moves the period carries
at the time this file runs (contract tests run before the
interfaces tests that post the 2026-11-17 fixtures, so no
non-emptiness is asserted — like
``test_tryton_adapter_financial_reports.py``). Skip when trytond is
unreachable or the test company is missing, like the other contract
tests.

Latencies are recorded for ``docs/performance-baselines.md`` and
never asserted.
"""

from __future__ import annotations

import datetime
import os
import time
from collections.abc import Generator
from typing import Any

import pytest

from hesabyar.domain.accounting.refs import CompanyRef
from hesabyar.domain.accounting.value_objects import (
    BalanceSheetView,
    GeneralLedgerView,
    IncomeStatementView,
    TrialBalanceView,
)
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.infrastructure.accounting.tryton.adapter import TrytonAccountingAdapter
from hesabyar.infrastructure.accounting.tryton.client import TrytonRestClient
from tests.load_support import LatencyRecorder, run_threads

BASE_URL = os.environ.get("TRYTON_BASE_URL", "http://localhost:8000")
DATABASE = os.environ.get("TRYTON_DATABASE", "hesabyar_tryton_test")
API_KEY = os.environ.get("TRYTON_API_KEY", "hesabyar-dev-rest-key")
TEST_COMPANY_NAME = os.environ.get("TRYTON_TEST_COMPANY", "HesabYar Adapter Test")

NOV_FROM = FiscalDate.from_gregorian(datetime.date(2026, 11, 1))
NOV_TO = FiscalDate.from_gregorian(datetime.date(2026, 11, 30))


@pytest.fixture(scope="module")
def client() -> Generator[TrytonRestClient, None, None]:
    rest_client = TrytonRestClient(BASE_URL, DATABASE, API_KEY, timeout=120.0)
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


def _adapter() -> tuple[TrytonRestClient, TrytonAccountingAdapter]:
    """A fresh client + adapter pair (one per thread)."""
    rest_client = TrytonRestClient(BASE_URL, DATABASE, API_KEY, timeout=120.0)
    return rest_client, TrytonAccountingAdapter(rest_client)


def _trial_balance_figures(view: TrialBalanceView) -> tuple[Any, ...]:
    # Figures only — freshness metadata (if ever stamped) is not a
    # figure and must not fail the comparison.
    return (
        view.total_debit,
        view.total_credit,
        view.move_count,
        view.line_count,
        view.lines,
    )


def _balance_sheet_figures(view: BalanceSheetView) -> tuple[Any, ...]:
    return (
        view.total_assets,
        view.total_liabilities,
        view.total_equity,
        view.sections,
    )


def _income_statement_figures(view: IncomeStatementView) -> tuple[Any, ...]:
    return (
        view.total_revenue,
        view.total_expense,
        view.net_income,
        view.sections,
        view.monthly,
    )


def _ledger_figures(view: GeneralLedgerView) -> tuple[Any, ...]:
    return (
        view.total_debit,
        view.total_credit,
        view.move_count,
        view.line_count,
        view.accounts,
    )


def test_concurrent_trial_balance_reads_agree(company: CompanyRef) -> None:
    """Eight threads reading one trial balance: identical figures, no errors."""
    recorder = LatencyRecorder()

    def _read(_index: int) -> tuple[Any, ...]:
        rest_client, thread_adapter = _adapter()
        try:
            started = time.monotonic()
            view = thread_adapter.get_trial_balance(company, NOV_FROM, NOV_TO)
            recorder.record("reports.trial_balance", time.monotonic() - started)
            assert view.is_balanced
            return _trial_balance_figures(view)
        finally:
            rest_client.close()

    figures = run_threads(8, _read)
    first = figures[0]
    for other in figures[1:]:
        assert other == first
    print(recorder.report())
    recorder.maybe_write_json()


def test_concurrent_mixed_report_reads_agree(company: CompanyRef) -> None:
    """Four threads reading all four reports at once: identical figures each."""
    recorder = LatencyRecorder()

    def _read(_index: int) -> dict[str, tuple[Any, ...]]:
        rest_client, thread_adapter = _adapter()
        try:
            started = time.monotonic()
            trial_balance = thread_adapter.get_trial_balance(company, NOV_FROM, NOV_TO)
            recorder.record("reports.trial_balance", time.monotonic() - started)
            started = time.monotonic()
            balance_sheet = thread_adapter.get_balance_sheet(company, NOV_TO)
            recorder.record("reports.balance_sheet", time.monotonic() - started)
            started = time.monotonic()
            income = thread_adapter.get_income_statement(company, NOV_FROM, NOV_TO)
            recorder.record("reports.income_statement", time.monotonic() - started)
            started = time.monotonic()
            ledger = thread_adapter.get_general_ledger(company, NOV_FROM, NOV_TO)
            recorder.record("reports.general_ledger", time.monotonic() - started)
            return {
                "trial_balance": _trial_balance_figures(trial_balance),
                "balance_sheet": _balance_sheet_figures(balance_sheet),
                "income_statement": _income_statement_figures(income),
                "general_ledger": _ledger_figures(ledger),
            }
        finally:
            rest_client.close()

    results = run_threads(4, _read)
    first = results[0]
    for other in results[1:]:
        assert other == first
    print(recorder.report())
    recorder.maybe_write_json()
