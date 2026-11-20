"""Contract tests for the application query services (US-104).

These tests run the services against the LIVE trytond server and the
``hesabyar_tryton_test`` database, through the Phase 0
:class:`TrytonAccountingAdapter`, using ONLY the dedicated test company
"HesabYar Adapter Test" (provisioned by
``~/workspace/projects/hesabyar-reference/setup_adapter_test_company.py``;
see docs/adapter.md). They never touch another company's records, and they
only read — no data is created or modified.

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

import os
from collections.abc import Generator

import pytest

from hesabyar.application.errors import (
    COMPANY_NOT_FOUND,
    ApplicationError,
    CompanyNotFoundError,
    EngineAuthenticationError,
)
from hesabyar.application.queries.accounts import AccountQueryService
from hesabyar.application.queries.companies import CompanyQueryService
from hesabyar.domain.accounting.refs import CompanyRef
from hesabyar.domain.accounting.value_objects import AccountView, CompanyView
from hesabyar.infrastructure.accounting.tryton.adapter import TrytonAccountingAdapter
from hesabyar.infrastructure.accounting.tryton.client import TrytonRestClient
from hesabyar.infrastructure.accounting.tryton.errors import TrytonAdapterError

BASE_URL = os.environ.get("TRYTON_BASE_URL", "http://localhost:8000")
DATABASE = os.environ.get("TRYTON_DATABASE", "hesabyar_tryton_test")
API_KEY = os.environ.get("TRYTON_API_KEY", "hesabyar-dev-rest-key")
TEST_COMPANY_NAME = os.environ.get("TRYTON_TEST_COMPANY", "HesabYar Adapter Test")

# A postable leaf account of the universal chart (cash).
CASH_ACCOUNT_CODE = "1.1.1"

# An id no company will ever have.
MISSING_COMPANY = CompanyRef(999_999_999)


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
def company_service(adapter: TrytonAccountingAdapter) -> CompanyQueryService:
    return CompanyQueryService(adapter)


@pytest.fixture(scope="module")
def account_service(adapter: TrytonAccountingAdapter) -> AccountQueryService:
    return AccountQueryService(adapter)


# ---------------------------------------------------------------------------
# Companies
# ---------------------------------------------------------------------------


def test_list_companies_via_service(company_service, company):
    companies = company_service.list_companies()
    assert all(isinstance(view, CompanyView) for view in companies)
    names = [view.name for view in companies]
    assert TEST_COMPANY_NAME in names
    view = next(view for view in companies if view.ref == company)
    assert view.currency_code == "IRR"


def test_get_company_via_service(company_service, company):
    view = company_service.get_company(company)
    assert view.ref == company
    assert view.name == TEST_COMPANY_NAME
    assert view.currency_code == "IRR"


def test_get_company_missing_raises_typed_not_found(company_service):
    with pytest.raises(CompanyNotFoundError) as excinfo:
        company_service.get_company(MISSING_COMPANY)
    assert excinfo.value.code == COMPANY_NOT_FOUND
    assert excinfo.value.details == {"company_id": 999_999_999}
    assert isinstance(excinfo.value, ApplicationError)


# ---------------------------------------------------------------------------
# Chart of accounts
# ---------------------------------------------------------------------------


def test_get_chart_of_accounts_via_service(account_service, company):
    chart = account_service.get_chart_of_accounts(company)
    # The universal chart template installs 135 accounts for the company.
    assert len(chart) >= 100
    assert all(isinstance(view, AccountView) for view in chart)
    assert all(view.company == company for view in chart)
    codes = [view.code for view in chart]
    assert len(set(codes)) == len(codes), "account codes must be unique"
    # Ordered by code; the codeless chart root sorts last.
    assert codes == sorted(codes, key=lambda code: (code == "", code))
    cash = next(view for view in chart if view.code == CASH_ACCOUNT_CODE)
    assert cash.name == "Cash and Cash Equivalents"
    assert cash.is_postable
    assert cash.party_required is False
    assert cash.closed is False


def test_get_chart_of_accounts_missing_company_raises_typed_not_found(account_service):
    with pytest.raises(CompanyNotFoundError) as excinfo:
        account_service.get_chart_of_accounts(MISSING_COMPANY)
    assert excinfo.value.code == COMPANY_NOT_FOUND
    assert isinstance(excinfo.value, ApplicationError)


# ---------------------------------------------------------------------------
# Error translation against the live engine
# ---------------------------------------------------------------------------


def test_wrong_api_key_translates_to_application_error(client):
    # `client` is only used for its skip-when-unreachable fixture behaviour.
    bad_client = TrytonRestClient(BASE_URL, DATABASE, "definitely-not-a-valid-key")
    try:
        bad_service = CompanyQueryService(TrytonAccountingAdapter(bad_client))
        with pytest.raises(EngineAuthenticationError) as excinfo:
            bad_service.list_companies()
    finally:
        bad_client.close()
    # The failure is a HesabYar application error with the stable code —
    # no Tryton adapter error type escapes the service boundary.
    assert excinfo.value.code == "ENGINE_AUTHENTICATION_FAILED"
    assert isinstance(excinfo.value, ApplicationError)
    assert not isinstance(excinfo.value, TrytonAdapterError)
    assert "Traceback" not in str(excinfo.value)
    assert ".py" not in str(excinfo.value)
