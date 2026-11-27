"""REST tests for the plan-amendment read (US-545, Phase 5-5 ۵ج).

The amendments endpoint is the audit history of the plan-change
flow: every partial early settlement that re-amortised a schedule
recorded one amendment (before/after leg principals + applied
advance + triggering receipt). US-541 created the rows but exposed
no read for them; US-545 adds ``GET /installments/plans/{id}/amendments``.

No servers needed: the app is built with dependency overrides —
a canned authenticated client, the in-memory fakes behind the
command/query/finance services, the real review-queue command
service over a fake queue port, and a fake journal port (the
US-541 test pattern). The partial-settlement scenario is the
US-541 one (pinned to 1405-08-29 via ``settlement_date``), so the
amendment values are the same ones that test asserts.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hesabyar.application.commands.installments import InstallmentCommandService
from hesabyar.application.commands.installments_finance import (
    InstallmentFinanceService,
)
from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.application.queries.installments import InstallmentQueryService
from hesabyar.infrastructure.config import AppSettings
from hesabyar.interfaces.rest.app import create_app
from hesabyar.interfaces.rest.dependencies import (
    get_authenticated_client,
    get_db_session,
    get_installment_command_service,
    get_installment_finance_service,
    get_installment_query_service,
)
from tests.interfaces.test_rest_installments import (
    _client,
    _company_row,
    _FakeSession,
)
from tests.interfaces.test_rest_installments_us541 import (
    _contract_body,
    _create_contract_and_plan,
    _plan_body,
    _settlement_body,
)
from tests.unit.test_installment_finance_services_us541 import (
    FakeJournals,
    FakeReviewQueue,
)
from tests.unit.test_installment_services import FakeInstallmentRepository


class _World:
    def __init__(self) -> None:
        self.repo = FakeInstallmentRepository()
        self.commands = InstallmentCommandService(self.repo)
        self.queries = InstallmentQueryService(self.repo)
        self.queue = FakeReviewQueue()
        self.journals = FakeJournals()
        self.finance = InstallmentFinanceService(
            self.repo,
            review=ReviewQueueCommandService(self.queue, self.queue),
            queue=self.queue,
            journals=self.journals,
        )


@pytest.fixture()
def world() -> _World:
    return _World()


def _build_api(world: _World, *, scopes: tuple[str, ...]) -> TestClient:
    app = create_app(settings=AppSettings())
    app.dependency_overrides[get_authenticated_client] = lambda: _client(scopes=scopes)
    app.dependency_overrides[get_db_session] = lambda: _FakeSession(_company_row())
    app.dependency_overrides[get_installment_command_service] = lambda: world.commands
    app.dependency_overrides[get_installment_query_service] = lambda: world.queries
    app.dependency_overrides[get_installment_finance_service] = lambda: world.finance
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def api(world: _World) -> TestClient:
    return _build_api(world, scopes=("installments:read", "installments:write"))


@pytest.fixture()
def write_only_api(world: _World) -> TestClient:
    return _build_api(world, scopes=("installments:write",))


# -- the amendments read ----------------------------------------------------------


def test_amendments_for_a_fresh_plan_is_an_empty_list(api: TestClient) -> None:
    _, plan_id = _create_contract_and_plan(api)
    response = api.get(f"/api/v1/installments/plans/{plan_id}/amendments")
    assert response.status_code == 200, response.text
    assert response.json() == []


def test_amendments_after_a_partial_settlement(api: TestClient) -> None:
    # The US-541 partial scenario: 3 legs of 1,000,000 Toman, a
    # 2,500,000 Toman partial settlement pays the two due legs and
    # leaves 500,000 Toman of advance, which re-amortises the one
    # remaining leg from 1,000,000 down to 500,000 Toman.
    contract = api.post(
        "/api/v1/installments/contracts",
        json=_contract_body(
            early_settlement_fee_policy="waived",
            early_settlement_income_policy="waived",
        ),
    )
    assert contract.status_code == 201, contract.text
    plan = api.post("/api/v1/installments/plans", json=_plan_body(contract.json()["id"]))
    assert plan.status_code == 201, plan.text
    plan_id = plan.json()["id"]
    settlement = api.post(
        f"/api/v1/installments/plans/{plan_id}/early-settlement",
        json=_settlement_body(amount_toman=2_500_000, settlement_type="partial"),
    )
    assert settlement.status_code == 201, settlement.text
    receipt_id = settlement.json()["receipt"]["id"]

    response = api.get(f"/api/v1/installments/plans/{plan_id}/amendments")
    assert response.status_code == 200, response.text
    data = response.json()
    assert len(data) == 1
    amendment = data[0]
    assert amendment["plan_id"] == plan_id
    assert amendment["kind"] == "re_amortisation"
    assert amendment["applied_advance_irr"] == 5_000_000
    assert amendment["applied_advance_toman"] == 500_000
    assert amendment["receipt_id"] == receipt_id
    # The re-amortisation touched only the one still-open leg.
    assert [s["principal_irr"] for s in amendment["legs_before"]] == [10_000_000]
    assert [s["principal_irr"] for s in amendment["legs_after"]] == [5_000_000]
    assert amendment["legs_before"][0]["sequence"] == 3
    assert amendment["legs_after"][0]["sequence"] == 3


def test_amendments_for_an_unknown_plan_is_404(api: TestClient) -> None:
    response = api.get("/api/v1/installments/plans/no-such-plan/amendments")
    assert response.status_code == 404
    assert response.json()["code"] == "INSTALLMENT_PLAN_NOT_FOUND"


def test_amendments_needs_the_read_scope(write_only_api: TestClient) -> None:
    response = write_only_api.get("/api/v1/installments/plans/any-plan/amendments")
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"
