"""REST tests for the installment finance routes (US-541, Phase 5-5 ۵ج).

No servers needed: the app is built with dependency overrides —
a canned authenticated client, the in-memory fakes behind the
command/query/finance services, the real review-queue command
service over a fake queue port, and a fake journal port. The
full-stack path (real DB) is covered by the contract tests.

Date-robustness: the quote and the dunning run take an explicit
``as_of`` (Jalali), so the fee/stage math is pinned to 1405-08-29
(2026-11-20) and never depends on the run date. Leg due dates are
1405-07-29 (30 days before), 1405-08-29 (the as-of day) and
1405-09-29 (30 days after).
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
    COMPANY_ROW_ID,
    _client,
    _company_row,
    _FakeSession,
)
from tests.unit.test_installment_finance_services_us541 import (
    FakeJournals,
    FakeReviewQueue,
)
from tests.unit.test_installment_services import FakeInstallmentRepository

#: Pinned as-of date (Jalali 1405-08-29 = 2026-11-20) + leg due dates.
AS_OF_JALALI = "1405-08-29"
PAST_JALALI = "1405-07-29"
TODAY_JALALI = "1405-08-29"
FUTURE_JALALI = "1405-09-29"


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


@pytest.fixture()
def api(world: _World) -> TestClient:
    app = create_app(settings=AppSettings())
    app.dependency_overrides[get_authenticated_client] = lambda: _client()
    app.dependency_overrides[get_db_session] = lambda: _FakeSession(_company_row())
    app.dependency_overrides[get_installment_command_service] = lambda: world.commands
    app.dependency_overrides[get_installment_query_service] = lambda: world.queries
    app.dependency_overrides[get_installment_finance_service] = lambda: world.finance
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def read_only_api(world: _World) -> TestClient:
    app = create_app(settings=AppSettings())
    app.dependency_overrides[get_authenticated_client] = lambda: _client(
        scopes=("installments:read",)
    )
    app.dependency_overrides[get_db_session] = lambda: _FakeSession(_company_row())
    app.dependency_overrides[get_installment_command_service] = lambda: world.commands
    app.dependency_overrides[get_installment_query_service] = lambda: world.queries
    app.dependency_overrides[get_installment_finance_service] = lambda: world.finance
    return TestClient(app, raise_server_exceptions=False)


def _contract_body(**overrides) -> dict:
    body = {
        "company_id": COMPANY_ROW_ID,
        "code": "STD-541-REST",
        "name": "قرارداد استاندارد",
        "version": 1,
        "effective_from": "1405-01-01",
        "late_fee_basis": "daily_percent",
        "late_fee_rate": "0.001",
        "late_fee_grace_days": 0,
        "penalty_account_engine_id": 77,
        "penalty_account_code": "6.1.2",
        "receivable_account_engine_id": 55,
        "receivable_account_code": "1.1.3",
        "finance_income_rate": "0.10",
        "dunning_offsets": [3, 7, 14],
        "early_settlement_fee_policy": "kept",
        "early_settlement_income_policy": "full",
    }
    body.update(overrides)
    return body


def _plan_body(contract_id: str, **overrides) -> dict:
    body = {
        "company_id": COMPANY_ROW_ID,
        "invoice_id": "INV-REST-541-1",
        "contract_id": contract_id,
        "legs": [
            {"due_date": PAST_JALALI, "principal_toman": 1_000_000},
            {"due_date": TODAY_JALALI, "principal_toman": 1_000_000},
            {"due_date": FUTURE_JALALI, "principal_toman": 1_000_000},
        ],
    }
    body.update(overrides)
    return body


def _settlement_body(**overrides) -> dict:
    body = {
        "invoice_id": "INV-REST-541-1",
        "journal_move_id": 4242,
        "amount_toman": 3_330_000,
        "settlement_type": "full",
        "settlement_date": AS_OF_JALALI,
        "payment_method": "cash",
    }
    body.update(overrides)
    return body


def _create_contract_and_plan(api: TestClient) -> tuple[str, str]:
    contract = api.post("/api/v1/installments/contracts", json=_contract_body())
    assert contract.status_code == 201, contract.text
    contract_id = contract.json()["id"]
    plan = api.post("/api/v1/installments/plans", json=_plan_body(contract_id))
    assert plan.status_code == 201, plan.text
    return contract_id, plan.json()["id"]


# -- contract finance fields ---------------------------------------------------------


def test_create_contract_accepts_the_us541_finance_terms(api: TestClient) -> None:
    response = api.post("/api/v1/installments/contracts", json=_contract_body())
    assert response.status_code == 201, response.text
    data = response.json()
    # Decimal fields serialise as JSON numbers (the US-540 pattern).
    assert str(data["finance_income_rate"]) == "0.1"
    assert data["dunning_offsets"] == [3, 7, 14]
    assert data["receivable_account_engine_id"] == 55
    assert data["receivable_account_code"] == "1.1.3"
    assert str(data["early_settlement_discount_rate"]) == "0"


# -- payoff quote -----------------------------------------------------------------------


def test_payoff_quote_returns_200_with_the_breakdown(api: TestClient) -> None:
    _, plan_id = _create_contract_and_plan(api)
    response = api.get(
        f"/api/v1/installments/plans/{plan_id}/payoff-quote",
        params={"as_of": AS_OF_JALALI},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["plan_id"] == plan_id
    assert data["as_of"] == AS_OF_JALALI
    assert data["remaining_principal_irr"] == 30_000_000
    assert data["income_unrealised_irr"] == 3_000_000
    assert data["income_due_irr"] == 3_000_000
    assert data["income_policy"] == "full"
    # One leg 30 days overdue: 10,000,000 × 0.001 × 30.
    assert data["late_fees_accrued_irr"] == 300_000
    assert data["late_fees_due_irr"] == 300_000
    assert data["fee_policy"] == "kept"
    assert data["advance_held_irr"] == 0
    assert data["payoff_total_irr"] == 33_300_000
    assert data["open_leg_count"] == 3


def test_payoff_quote_for_an_unknown_plan_is_404(api: TestClient) -> None:
    response = api.get("/api/v1/installments/plans/no-such-plan/payoff-quote")
    assert response.status_code == 404
    assert response.json()["code"] == "INSTALLMENT_PLAN_NOT_FOUND"


def test_payoff_quote_with_a_bad_as_of_is_422(api: TestClient) -> None:
    _, plan_id = _create_contract_and_plan(api)
    response = api.get(
        f"/api/v1/installments/plans/{plan_id}/payoff-quote",
        params={"as_of": "not-a-date"},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "INSTALLMENT_VALIDATION_FAILED"


# -- early settlement ----------------------------------------------------------------------


def test_early_settlement_full_closes_the_plan(api: TestClient, world: _World) -> None:
    _, plan_id = _create_contract_and_plan(api)
    response = api.post(
        f"/api/v1/installments/plans/{plan_id}/early-settlement",
        json=_settlement_body(),
    )
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["settlement_type"] == "full"
    assert data["receipt"]["allocated_irr"] == 30_000_000
    assert data["receipt"]["finance_income_irr"] == 3_000_000
    assert data["receipt"]["late_fee_irr"] == 300_000
    assert data["receipt"]["is_early_settlement"] is True
    assert data["quote"]["payoff_total_irr"] == 33_300_000
    assert data["journal_move_ids"] == [9001]
    assert data["amendment"] is None
    assert len(world.journals.drafts) == 1

    detail = api.get(f"/api/v1/installments/plans/{plan_id}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "paid"


def test_early_settlement_partial_reamortises(api: TestClient) -> None:
    contract = api.post(
        "/api/v1/installments/contracts",
        json=_contract_body(
            early_settlement_fee_policy="waived",
            early_settlement_income_policy="waived",
        ),
    )
    assert contract.status_code == 201, contract.text
    plan = api.post(
        "/api/v1/installments/plans", json=_plan_body(contract.json()["id"])
    )
    assert plan.status_code == 201, plan.text
    plan_id = plan.json()["id"]
    response = api.post(
        f"/api/v1/installments/plans/{plan_id}/early-settlement",
        json=_settlement_body(amount_toman=2_500_000, settlement_type="partial"),
    )
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["settlement_type"] == "partial"
    assert data["amendment"] is not None
    assert data["amendment"]["kind"] == "re_amortisation"
    assert data["amendment"]["applied_advance_irr"] == 5_000_000
    assert data["amendment"]["legs_after"][0]["principal_irr"] == 5_000_000

    detail = api.get(f"/api/v1/installments/plans/{plan_id}")
    assert detail.json()["outstanding_irr"] == 5_000_000


def test_early_settlement_below_the_payoff_is_422(api: TestClient) -> None:
    _, plan_id = _create_contract_and_plan(api)
    response = api.post(
        f"/api/v1/installments/plans/{plan_id}/early-settlement",
        json=_settlement_body(amount_toman=1_000_000),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "INSTALLMENT_SETTLEMENT_AMOUNT_INSUFFICIENT"


def test_early_settlement_enforces_the_linkage_guard(api: TestClient) -> None:
    _, plan_id = _create_contract_and_plan(api)
    response = api.post(
        f"/api/v1/installments/plans/{plan_id}/early-settlement",
        json=_settlement_body(invoice_id="", journal_move_id=0),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "INSTALLMENT_RECEIPT_LINKAGE_REQUIRED"


def test_early_settlement_needs_the_write_scope(read_only_api: TestClient) -> None:
    response = read_only_api.post(
        "/api/v1/installments/plans/any-plan/early-settlement",
        json=_settlement_body(),
    )
    assert response.status_code == 403


# -- overdue report --------------------------------------------------------------------------


def test_overdue_report_returns_the_overdue_legs(api: TestClient) -> None:
    # Far-past legs (overdue on any run date) + one far-future leg.
    contract = api.post("/api/v1/installments/contracts", json=_contract_body())
    assert contract.status_code == 201, contract.text
    plan = api.post(
        "/api/v1/installments/plans",
        json=_plan_body(
            contract.json()["id"],
            legs=[
                {"due_date": "1400-01-01", "principal_toman": 1_000_000},
                {"due_date": "1400-01-01", "principal_toman": 1_000_000},
                {"due_date": "1410-01-01", "principal_toman": 1_000_000},
            ],
        ),
    )
    assert plan.status_code == 201, plan.text
    plan_id = plan.json()["id"]

    response = api.get("/api/v1/installments/overdue")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total_rows"] == 2
    assert data["total_balance_irr"] == 20_000_000
    assert data["rows"][0]["plan_id"] == plan_id
    assert data["rows"][0]["dunning_stage"] == 3
    assert data["rows"][0]["late_fee_accrued_irr"] > 0

    scoped = api.get("/api/v1/installments/overdue", params={"plan_id": plan_id})
    assert scoped.status_code == 200
    assert scoped.json()["total_rows"] == 2

    empty = api.get("/api/v1/installments/overdue", params={"plan_id": "no-such-plan"})
    assert empty.status_code == 200
    assert empty.json()["total_rows"] == 0


# -- dunning run ------------------------------------------------------------------------------


def test_dunning_run_files_items_and_journals_fees(
    api: TestClient, world: _World
) -> None:
    _create_contract_and_plan(api)
    response = api.post("/api/v1/installments/dunning/run", json={"as_of": AS_OF_JALALI})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["as_of"] == AS_OF_JALALI
    assert data["plans_scanned"] == 1
    assert data["overdue_legs"] == 1
    assert len(data["items_filed"]) == 1
    assert data["fees_assessed_irr"] == 300_000
    assert data["journals_created"] == [9001]
    assert data["legs_skipped_no_account"] == []
    assert len(world.queue.items) == 1

    # A second run the same day files nothing new.
    again = api.post("/api/v1/installments/dunning/run", json={"as_of": AS_OF_JALALI})
    assert again.status_code == 200
    assert again.json()["items_filed"] == []
    assert again.json()["fees_assessed_irr"] == 0


def test_dunning_run_needs_the_write_scope(read_only_api: TestClient) -> None:
    response = read_only_api.post("/api/v1/installments/dunning/run", json={})
    assert response.status_code == 403
