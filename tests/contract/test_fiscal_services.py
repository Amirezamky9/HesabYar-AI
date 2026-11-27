"""Contract tests for the fiscal + setup-status query services (US-203).

These tests run the services against the LIVE trytond server and the
``hesabyar_tryton_test`` database, through the Phase 0
:class:`TrytonAccountingAdapter`, using ONLY the dedicated test company
"HesabYar Adapter Test" (provisioned by
``~/workspace/projects/hesabyar-reference/setup_adapter_test_company.py``;
see docs/adapter.md). They never touch another company's records, and
they only read — no data is created or modified.

When the server is not reachable (or the test company has not been
provisioned yet), every test SKIPS cleanly with a clear message — the
suite stays green without a server. With the server up, the tests must
PASS.

Configuration (all optional, dev defaults match docs/dev-setup.md):
  TRYTON_BASE_URL       (default http://localhost:8000)
  TRYTON_DATABASE       (default hesabyar_tryton_test)
  TRYTON_API_KEY        (default hesabyar-dev-rest-key)
  TRYTON_TEST_COMPANY   (default "HesabYar Adapter Test")
"""

from __future__ import annotations

import os
from collections.abc import Generator

import pytest

from hesabyar.application.errors import COMPANY_NOT_FOUND, CompanyNotFoundError
from hesabyar.application.queries.fiscal import FiscalQueryService, SetupStatusService
from hesabyar.domain.accounting.refs import CompanyRef, FiscalPeriodRef, FiscalYearRef
from hesabyar.domain.accounting.value_objects import (
    CompanySetupStatus,
    FiscalPeriodView,
    FiscalYearView,
)
from hesabyar.domain.common.errors import (
    FiscalPeriodNotFoundError,
    FiscalYearNotFoundError,
)
from hesabyar.infrastructure.accounting.tryton.adapter import TrytonAccountingAdapter
from hesabyar.infrastructure.accounting.tryton.client import TrytonRestClient

BASE_URL = os.environ.get("TRYTON_BASE_URL", "http://localhost:8000")
DATABASE = os.environ.get("TRYTON_DATABASE", "hesabyar_tryton_test")
API_KEY = os.environ.get("TRYTON_API_KEY", "hesabyar-dev-rest-key")
TEST_COMPANY_NAME = os.environ.get("TRYTON_TEST_COMPANY", "HesabYar Adapter Test")

# An id no company/year/period will ever have.
MISSING_ID = 999_999_999


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
def fiscal_service(adapter: TrytonAccountingAdapter) -> FiscalQueryService:
    return FiscalQueryService(adapter)


@pytest.fixture(scope="module")
def setup_service(adapter: TrytonAccountingAdapter) -> SetupStatusService:
    return SetupStatusService(adapter)


# ---------------------------------------------------------------------------
# Fiscal years
# ---------------------------------------------------------------------------


def test_list_fiscal_years_via_service(fiscal_service, company):
    years = fiscal_service.list_fiscal_years(company)
    assert years, "the test company must have at least one fiscal year"
    assert all(isinstance(year, FiscalYearView) for year in years)
    assert all(year.company == company for year in years)
    # Newest first.
    starts = [year.start_date for year in years]
    assert starts == sorted(starts, reverse=True)
    fy2026 = next(year for year in years if year.name == "FY 2026")
    assert fy2026.state == "open"
    assert fy2026.is_open is True
    assert fy2026.start_date.to_gregorian_str() == "2026-01-01"
    assert fy2026.end_date.to_gregorian_str() == "2026-12-31"


def test_get_fiscal_year_via_service(fiscal_service, company):
    years = fiscal_service.list_fiscal_years(company)
    fy2026 = next(year for year in years if year.name == "FY 2026")
    found = fiscal_service.get_fiscal_year(company, fy2026.ref)
    assert found == fy2026


def test_get_fiscal_year_missing_raises_typed_not_found(fiscal_service, company):
    with pytest.raises(FiscalYearNotFoundError) as excinfo:
        fiscal_service.get_fiscal_year(company, FiscalYearRef(MISSING_ID))
    assert excinfo.value.code == "LEDGER_FISCAL_YEAR_NOT_FOUND"


def test_list_fiscal_years_missing_company_raises_typed_not_found(fiscal_service):
    with pytest.raises(CompanyNotFoundError) as excinfo:
        fiscal_service.list_fiscal_years(CompanyRef(MISSING_ID))
    assert excinfo.value.code == COMPANY_NOT_FOUND


# ---------------------------------------------------------------------------
# Periods
# ---------------------------------------------------------------------------


def test_list_periods_via_service(fiscal_service, company):
    periods = fiscal_service.list_periods(company)
    # FY 2026 was provisioned with 12 monthly periods.
    assert len(periods) >= 12
    assert all(isinstance(period, FiscalPeriodView) for period in periods)
    # Ordered by start date.
    starts = [period.start_date for period in periods]
    assert starts == sorted(starts)
    # Every period belongs to one of the company's years.
    years = {year.ref for year in fiscal_service.list_fiscal_years(company)}
    assert all(period.fiscal_year in years for period in periods)
    assert all(period.ref is not None for period in periods)
    assert all(period.period_type == "standard" for period in periods)
    assert all(period.is_open for period in periods)


def test_list_periods_filtered_by_fiscal_year(fiscal_service, company):
    years = fiscal_service.list_fiscal_years(company)
    fy2026 = next(year for year in years if year.name == "FY 2026")
    periods = fiscal_service.list_periods(company, fy2026.ref)
    assert len(periods) == 12
    assert all(period.fiscal_year == fy2026.ref for period in periods)
    assert periods[0].start_date.to_gregorian_str() == "2026-01-01"
    assert periods[-1].end_date.to_gregorian_str() == "2026-12-31"


def test_list_periods_unknown_year_raises_typed_not_found(fiscal_service, company):
    with pytest.raises(FiscalYearNotFoundError):
        fiscal_service.list_periods(company, FiscalYearRef(MISSING_ID))


def test_get_period_via_service(fiscal_service, company):
    periods = fiscal_service.list_periods(company)
    first = periods[0]
    assert first.ref is not None
    found = fiscal_service.get_period(company, first.ref)
    assert found == first


def test_get_period_missing_raises_typed_not_found(fiscal_service, company):
    with pytest.raises(FiscalPeriodNotFoundError) as excinfo:
        fiscal_service.get_period(company, FiscalPeriodRef(MISSING_ID))
    assert excinfo.value.code == "LEDGER_FISCAL_PERIOD_NOT_FOUND"


# ---------------------------------------------------------------------------
# Setup status
# ---------------------------------------------------------------------------


def test_get_setup_status_via_service(setup_service, company, adapter):
    status = setup_service.get_setup_status(company)
    assert isinstance(status, CompanySetupStatus)
    assert status.company == company
    # The Adapter Test company is fully provisioned: universal chart
    # (135 accounts), FY 2026 open with 12 open periods — plus FY 2027,
    # added by the settlement phase (US-522) for tranche maturities.
    # The open year is therefore derived, never hard-coded: the service
    # returns the latest open fiscal year.
    assert status.chart_installed is True
    assert status.account_count >= 100
    assert status.chart_template is None
    assert status.fiscal_year_present is True
    assert status.fiscal_year_count >= 1
    assert status.open_fiscal_year is not None
    open_years = [y for y in adapter.list_fiscal_years(company) if y.is_open]
    expected = max(open_years, key=lambda y: y.start_date.gregorian_date)
    assert status.open_fiscal_year.name == expected.name
    assert status.open_period_present is True
    assert status.open_period_count >= 12
    assert status.ready is True


def test_get_setup_status_missing_company_raises_typed_not_found(setup_service):
    with pytest.raises(CompanyNotFoundError) as excinfo:
        setup_service.get_setup_status(CompanyRef(MISSING_ID))
    assert excinfo.value.code == COMPANY_NOT_FOUND
