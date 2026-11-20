"""REST tests for the installment routes (US-540 + US-542 + US-543, Phase 5-5 ۵ج).

No servers needed: the app is built with dependency overrides —
a canned authenticated client, the in-memory fakes from
``tests.unit.test_installment_services`` (installments) and
``tests.unit.test_cheque_services`` (cheques) behind the
command/query services, and a stub session that resolves the
company mapping. The full-stack path (real DB) is covered by the
contract tests.

Date-robustness: leg statuses are computed against the REAL today
in these tests, so due dates are far-past (1400-01-01 = 2021-03-21,
always overdue) or far-future (1410-01-01 = 2031-03-21, always
expected) — never near today.

US-543 (BNPL): the world also wires the REAL settlement engine
over its fakes (a snapppay 4-tranche contract), so BNPL receipts
create real gateway payments + tranche schedules in memory.
"""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hesabyar.application.commands.installments import InstallmentCommandService
from hesabyar.application.commands.integrations import (
    ChannelRegistryService,
    GatewayContractService,
)
from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.application.commands.settlement import SettlementCommandService
from hesabyar.application.queries.installments import InstallmentQueryService
from hesabyar.domain.cheques.models import (
    Cheque,
    ChequeKind,
    ChequePurpose,
    ChequeState,
)
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.infrastructure.config import AppSettings
from hesabyar.infrastructure.persistence.postgres.models import EngineCompanyModel
from hesabyar.interfaces.rest.app import create_app
from hesabyar.interfaces.rest.dependencies import (
    get_authenticated_client,
    get_db_session,
    get_installment_command_service,
    get_installment_query_service,
)
from tests.unit.test_cheque_services import FakeChequeRepository
from tests.unit.test_installment_bnpl import FakeReviewAudit, FakeReviewQueue
from tests.unit.test_installment_services import (
    COMPANY_ENGINE_ID,
    INSTALLATION_ID,
    TENANT_ID,
    FakeInstallmentRepository,
)
from tests.unit.test_integrations_services import FakeIntegrationsRepository
from tests.unit.test_settlement_services import (
    FOUR_TRANCHES,
    FakeSettlementAudit,
    FakeSettlementRepository,
)

COMPANY_ROW_ID = "00000000-0000-0000-0000-000000000540"
ALL_INSTALLMENT_SCOPES = ("installments:read", "installments:write")

#: Far-past / far-future Jalali dates (always overdue / always expected).
PAST_JALALI = "1400-01-01"
FUTURE_JALALI = "1410-01-01"


class _FakeResult:
    def __init__(self, row: EngineCompanyModel | None) -> None:
        self._row = row

    def scalar_one_or_none(self) -> EngineCompanyModel | None:
        return self._row


class _FakeSession:
    """Resolves the company mapping; commit/close are no-ops."""

    def __init__(self, row: EngineCompanyModel | None) -> None:
        self._row = row

    def execute(self, stmt) -> _FakeResult:  # noqa: ARG002 - any statement resolves the same row
        return _FakeResult(self._row)

    def commit(self) -> None:
        return None

    def close(self) -> None:
        return None


def _company_row() -> EngineCompanyModel:
    return EngineCompanyModel(
        id=COMPANY_ROW_ID,
        tenant_id=TENANT_ID,
        engine_connection_id="00000000-0000-0000-0000-000000000599",
        external_company_id=str(COMPANY_ENGINE_ID),
        company_name="Test Company",
    )


def _client(*, scopes: tuple[str, ...] = ALL_INSTALLMENT_SCOPES) -> AuthenticatedClient:
    return AuthenticatedClient(
        client_id="test-client",
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        key_id="test-key",
        key_prefix="hya_live_test1234",
        client_name="test client",
        scopes=frozenset(scopes),
        engine_company_id=None,
    )


def _held_cheque(**overrides) -> Cheque:
    """A received commercial cheque, held (US-542), ready to back a receipt."""
    cheque = Cheque(
        id=str(uuid.uuid4()),
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        company_engine_id=COMPANY_ENGINE_ID,
        kind=ChequeKind.RECEIVED,
        purpose=ChequePurpose.PAYMENT,
        sayad_id="5420000000000001",
        amount_irr=10_000_000,
        issue_date=FiscalDate.from_gregorian(datetime.date(2026, 10, 21)),
        due_date=FiscalDate.from_gregorian(datetime.date(2027, 1, 5)),
        issuer_name="مشتری نمونه",
        issuer_national_id="0012345678",
        issuer_bank="056 سامان",
        payee_name="کافه ایوان",
        payee_national_id="00123456789",
        order_id="ord-542",
        customer_ref="cust-542",
        sayad_registered=True,
        state=ChequeState.HELD,
    )
    for key, value in overrides.items():
        setattr(cheque, key, value)
    return cheque


class _World:
    def __init__(self) -> None:
        self.repo = FakeInstallmentRepository()
        self.cheques = FakeChequeRepository()
        # US-543 (BNPL): the real settlement engine over its fakes
        # (snapppay 4-tranche contract, fee 10%) + the review queue,
        # so BNPL receipts + rejections work end to end in memory.
        self.settlement_repo = FakeSettlementRepository()
        registry = FakeIntegrationsRepository()
        channels = ChannelRegistryService(registry)
        contracts = GatewayContractService(registry)
        channels.create_channel(INSTALLATION_ID, "snapppay", "اسنپ‌پی", "bnpl")
        contracts.create_version(
            INSTALLATION_ID,
            "snapppay",
            valid_from=datetime.date(2026, 10, 23),
            fee_percent=Decimal("10"),
            fee_vat_rate=Decimal("10"),
            tranche_legs=FOUR_TRANCHES,
            payout_window_start_day=1,
            payout_window_end_day=5,
            grace_days_after_due=3,
        )
        self.review_queue = FakeReviewQueue()
        review = ReviewQueueCommandService(self.review_queue, FakeReviewAudit())
        settlement = SettlementCommandService(
            self.settlement_repo, registry, FakeSettlementAudit(), review=review
        )
        self.commands = InstallmentCommandService(
            self.repo, self.cheques, settlement=settlement, review=review
        )
        self.queries = InstallmentQueryService(
            self.repo, self.cheques, settlement_repository=self.settlement_repo
        )

    def create_plan(self, **overrides) -> dict:
        body = _plan_body(**overrides)
        plan = self.commands.create_plan(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            company_engine_id=COMPANY_ENGINE_ID,
            payload=_plan_payload(body),
        )
        return {"plan": plan, "body": body}

    def add_cheque(self, **overrides) -> Cheque:
        cheque = _held_cheque(**overrides)
        self.cheques.create(cheque)
        return cheque


@pytest.fixture()
def world() -> _World:
    return _World()


@pytest.fixture()
def api(world: _World, request: pytest.FixtureRequest) -> TestClient:
    app = create_app(settings=AppSettings())
    scopes = getattr(request, "param", ALL_INSTALLMENT_SCOPES)
    app.dependency_overrides[get_authenticated_client] = lambda: _client(scopes=scopes)
    app.dependency_overrides[get_db_session] = lambda: _FakeSession(_company_row())
    app.dependency_overrides[get_installment_command_service] = lambda: world.commands
    app.dependency_overrides[get_installment_query_service] = lambda: world.queries
    return TestClient(app, raise_server_exceptions=False)


def _plan_body(**overrides) -> dict:
    body = {
        "company_id": COMPANY_ROW_ID,
        "invoice_id": "INV-REST-1",
        "total_principal_toman": 3_000_000,
        "leg_count": 3,
        "interval_days": 30,
        "first_due_date": FUTURE_JALALI,
    }
    body.update(overrides)
    return body


def _plan_payload(body: dict) -> dict[str, object]:
    payload: dict[str, object] = {
        "invoice_id": body["invoice_id"],
        "total_principal_toman": body["total_principal_toman"],
        "leg_count": body["leg_count"],
        "interval_days": body["interval_days"],
        "first_due_date": body["first_due_date"],
    }
    # US-543 (BNPL): kind + gateway pass through when the body carries them.
    if body.get("kind") is not None:
        payload["kind"] = body["kind"]
    if body.get("gateway_channel_key") is not None:
        payload["gateway_channel_key"] = body["gateway_channel_key"]
    return payload


def _receipt_body(**overrides) -> dict:
    body = {
        "invoice_id": "INV-REST-1",
        "journal_move_id": 42,
        "amount_toman": 1_000_000,
        "payment_method": "cash",
    }
    body.update(overrides)
    return body


def _contract_body(**overrides) -> dict:
    body = {
        "company_id": COMPANY_ROW_ID,
        "code": "STD-REST",
        "name": "قرارداد استاندارد",
        "version": 1,
        "effective_from": "1405-08-01",
        "late_fee_basis": "daily_percent",
        "late_fee_rate": "0.02",
        "late_fee_grace_days": 3,
        "penalty_account_engine_id": 77,
    }
    body.update(overrides)
    return body


# -- plan creation -------------------------------------------------------------------


def test_create_plan_returns_201_with_generated_legs(api: TestClient) -> None:
    response = api.post("/api/v1/installments/plans", json=_plan_body())
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["invoice_id"] == "INV-REST-1"
    assert data["total_principal_irr"] == 30_000_000
    assert data["total_principal_toman"] == 3_000_000
    assert data["leg_count"] == 3
    assert data["status"] == "active"
    assert data["auto_post"] is False
    assert data["outstanding_irr"] == 30_000_000


def test_create_plan_with_explicit_legs(api: TestClient) -> None:
    body = {
        "company_id": COMPANY_ROW_ID,
        "invoice_id": "INV-REST-2",
        "legs": [
            {"due_date": FUTURE_JALALI, "principal_toman": 500_000},
            {"due_date": "1410-02-01", "principal_toman": 2_500_000},
        ],
    }
    response = api.post("/api/v1/installments/plans", json=body)
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["total_principal_irr"] == 30_000_000
    assert data["leg_count"] == 2


def test_create_plan_second_plan_for_same_invoice_is_409(api: TestClient) -> None:
    first = api.post("/api/v1/installments/plans", json=_plan_body())
    assert first.status_code == 201
    again = api.post("/api/v1/installments/plans", json=_plan_body())
    assert again.status_code == 409, again.text
    assert again.json()["code"] == "INSTALLMENT_PLAN_EXISTS"


def test_create_plan_invalid_payload_is_422(api: TestClient) -> None:
    response = api.post(
        "/api/v1/installments/plans", json=_plan_body(leg_count=0)
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "INSTALLMENT_VALIDATION_FAILED"


def test_create_plan_with_unknown_company_is_404(api: TestClient) -> None:
    cast(FastAPI, api.app).dependency_overrides[get_db_session] = lambda: _FakeSession(None)
    response = api.post("/api/v1/installments/plans", json=_plan_body())
    assert response.status_code == 404, response.text


@pytest.mark.parametrize("api", [("installments:read",)], indirect=True)
def test_create_plan_without_write_scope_is_403(api: TestClient) -> None:
    response = api.post("/api/v1/installments/plans", json=_plan_body())
    assert response.status_code == 403, response.text


# -- plan reads ----------------------------------------------------------------------


def test_get_plan_detail_carries_legs_with_computed_statuses(
    api: TestClient, world: _World
) -> None:
    created = world.create_plan(first_due_date=PAST_JALALI)
    plan_id = created["plan"].id
    response = api.get(f"/api/v1/installments/plans/{plan_id}")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["status"] == "overdue"
    assert len(data["legs"]) == 3
    # All three legs are past due (first due 1400 + 30/60 days is
    # still 1400) and unpaid -> overdue.
    assert [leg["status"] for leg in data["legs"]] == ["overdue", "overdue", "overdue"]
    assert data["legs"][0]["due_date"] == PAST_JALALI
    assert data["legs"][0]["principal_irr"] == 10_000_000
    assert data["outstanding_irr"] == 30_000_000
    assert data["receipts"] == []


def test_get_missing_plan_is_404(api: TestClient) -> None:
    response = api.get("/api/v1/installments/plans/00000000-0000-0000-0000-000000000999")
    assert response.status_code == 404, response.text
    assert response.json()["code"] == "INSTALLMENT_PLAN_NOT_FOUND"


def test_list_plans_filters_by_invoice_and_status(api: TestClient, world: _World) -> None:
    world.create_plan(first_due_date=PAST_JALALI)
    world.create_plan(invoice_id="INV-REST-9", first_due_date=FUTURE_JALALI)
    response = api.get("/api/v1/installments/plans?status=overdue")
    assert response.status_code == 200, response.text
    data = response.json()
    assert [row["invoice_id"] for row in data] == ["INV-REST-1"]
    response = api.get("/api/v1/installments/plans?invoice_id=INV-REST-9")
    assert response.status_code == 200, response.text
    assert [row["invoice_id"] for row in response.json()] == ["INV-REST-9"]


def test_list_plans_unknown_status_is_422(api: TestClient) -> None:
    response = api.get("/api/v1/installments/plans?status=bogus")
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("api", [("installments:write",)], indirect=True)
def test_list_plans_without_read_scope_is_403(api: TestClient) -> None:
    response = api.get("/api/v1/installments/plans")
    assert response.status_code == 403, response.text


# -- receipts + THE LINKAGE GUARD ------------------------------------------------------


def test_record_receipt_returns_201_with_allocation(api: TestClient, world: _World) -> None:
    created = world.create_plan(first_due_date=PAST_JALALI)
    plan_id = created["plan"].id
    response = api.post(
        f"/api/v1/installments/plans/{plan_id}/receipts", json=_receipt_body()
    )
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["invoice_id"] == "INV-REST-1"
    assert data["journal_move_id"] == 42
    assert data["amount_irr"] == 10_000_000
    assert data["allocated_irr"] == 10_000_000
    assert data["advance_irr"] == 0
    assert len(data["allocations"]) == 1
    # The receipt paid the oldest overdue leg (sequence 1).
    detail = api.get(f"/api/v1/installments/plans/{plan_id}").json()
    assert detail["legs"][0]["status"] == "paid"
    assert detail["legs"][0]["received_irr"] == 10_000_000
    assert detail["total_received_irr"] == 10_000_000


def test_receipt_overpayment_returns_advance(api: TestClient, world: _World) -> None:
    created = world.create_plan(first_due_date=PAST_JALALI)
    plan_id = created["plan"].id
    response = api.post(
        f"/api/v1/installments/plans/{plan_id}/receipts",
        json=_receipt_body(amount_toman=5_000_000),
    )
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["allocated_irr"] == 30_000_000
    assert data["advance_irr"] == 20_000_000


def test_receipt_without_invoice_ref_is_typed_422(api: TestClient, world: _World) -> None:
    created = world.create_plan(first_due_date=PAST_JALALI)
    plan_id = created["plan"].id
    body = _receipt_body()
    del body["invoice_id"]
    response = api.post(f"/api/v1/installments/plans/{plan_id}/receipts", json=body)
    # Missing invoice_id fails FastAPI's own body validation first
    # (the field is required) — still a 422, still no receipt stored.
    assert response.status_code == 422, response.text
    detail = api.get(f"/api/v1/installments/plans/{plan_id}").json()
    assert detail["receipts"] == []


def test_receipt_with_blank_invoice_ref_is_guard_422(api: TestClient, world: _World) -> None:
    created = world.create_plan(first_due_date=PAST_JALALI)
    plan_id = created["plan"].id
    response = api.post(
        f"/api/v1/installments/plans/{plan_id}/receipts",
        json=_receipt_body(invoice_id="  "),
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "INSTALLMENT_RECEIPT_LINKAGE_REQUIRED"


def test_receipt_without_journal_ref_is_guard_422(api: TestClient, world: _World) -> None:
    created = world.create_plan(first_due_date=PAST_JALALI)
    plan_id = created["plan"].id
    response = api.post(
        f"/api/v1/installments/plans/{plan_id}/receipts",
        json=_receipt_body(journal_move_id=0),
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "INSTALLMENT_RECEIPT_LINKAGE_REQUIRED"
    detail = api.get(f"/api/v1/installments/plans/{plan_id}").json()
    assert detail["receipts"] == []


def test_receipt_with_wrong_invoice_is_mismatch_422(api: TestClient, world: _World) -> None:
    created = world.create_plan(first_due_date=PAST_JALALI)
    plan_id = created["plan"].id
    response = api.post(
        f"/api/v1/installments/plans/{plan_id}/receipts",
        json=_receipt_body(invoice_id="INV-OTHER"),
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "INSTALLMENT_INVOICE_MISMATCH"


# -- cheque receipts (US-542) ----------------------------------------------------------


def test_receipt_by_cheque_returns_201_and_leg_is_pending_clearance(
    api: TestClient, world: _World
) -> None:
    created = world.create_plan(first_due_date=PAST_JALALI)
    plan_id = created["plan"].id
    cheque = world.add_cheque()
    response = api.post(
        f"/api/v1/installments/plans/{plan_id}/receipts",
        json=_receipt_body(payment_method="cheque", cheque_id=cheque.id),
    )
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["payment_method"] == "cheque"
    assert data["cheque_id"] == cheque.id
    assert data["collected_at"] is None  # not collected until the cheque clears
    assert data["allocated_irr"] == 10_000_000
    detail = api.get(f"/api/v1/installments/plans/{plan_id}").json()
    assert detail["legs"][0]["status"] == "pending_clearance"
    assert detail["legs"][0]["received_irr"] == 0
    assert detail["legs"][0]["pending_clearance_irr"] == 10_000_000
    assert detail["legs"][0]["balance_irr"] == 10_000_000
    linked = detail["legs"][0]["linked_cheque"]
    assert linked is not None
    assert linked["sayad_id"] == "5420000000000001"
    assert linked["due_date"] == "1405-10-15"
    assert linked["state"] == "held"
    assert detail["total_pending_clearance_irr"] == 10_000_000
    assert detail["status"] == "overdue"  # legs 2-3 are still overdue


def test_receipt_by_cheque_without_cheque_id_is_422(api: TestClient, world: _World) -> None:
    created = world.create_plan(first_due_date=PAST_JALALI)
    plan_id = created["plan"].id
    response = api.post(
        f"/api/v1/installments/plans/{plan_id}/receipts",
        json=_receipt_body(payment_method="cheque"),
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "INSTALLMENT_VALIDATION_FAILED"


def test_receipt_by_cheque_in_registered_state_is_422(
    api: TestClient, world: _World
) -> None:
    created = world.create_plan(first_due_date=PAST_JALALI)
    plan_id = created["plan"].id
    cheque = world.add_cheque(state=ChequeState.REGISTERED)
    response = api.post(
        f"/api/v1/installments/plans/{plan_id}/receipts",
        json=_receipt_body(payment_method="cheque", cheque_id=cheque.id),
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "INSTALLMENT_CHEQUE_NOT_RECEIVABLE"


def test_receipt_by_cheque_with_unknown_cheque_is_404(
    api: TestClient, world: _World
) -> None:
    created = world.create_plan(first_due_date=PAST_JALALI)
    plan_id = created["plan"].id
    response = api.post(
        f"/api/v1/installments/plans/{plan_id}/receipts",
        json=_receipt_body(
            payment_method="cheque",
            cheque_id="00000000-0000-0000-0000-000000000999",
        ),
    )
    assert response.status_code == 404, response.text
    assert response.json()["code"] == "CHEQUE_NOT_FOUND"


def test_receipt_with_cheque_id_and_cash_is_422(api: TestClient, world: _World) -> None:
    created = world.create_plan(first_due_date=PAST_JALALI)
    plan_id = created["plan"].id
    cheque = world.add_cheque()
    response = api.post(
        f"/api/v1/installments/plans/{plan_id}/receipts",
        json=_receipt_body(cheque_id=cheque.id),
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "INSTALLMENT_VALIDATION_FAILED"


def test_receipt_on_missing_plan_is_404(api: TestClient) -> None:
    response = api.post(
        "/api/v1/installments/plans/00000000-0000-0000-0000-000000000999/receipts",
        json=_receipt_body(),
    )
    assert response.status_code == 404, response.text


@pytest.mark.parametrize("api", [("installments:read",)], indirect=True)
def test_receipt_without_write_scope_is_403(api: TestClient, world: _World) -> None:
    created = world.create_plan(first_due_date=PAST_JALALI)
    plan_id = created["plan"].id
    response = api.post(
        f"/api/v1/installments/plans/{plan_id}/receipts", json=_receipt_body()
    )
    assert response.status_code == 403, response.text


# -- contracts -----------------------------------------------------------------------


def test_create_contract_returns_201(api: TestClient) -> None:
    response = api.post("/api/v1/installments/contracts", json=_contract_body())
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["code"] == "STD-REST"
    assert data["late_fee_basis"] == "daily_percent"
    # Decimal serializes as a JSON number (0.02) — compare as text.
    assert str(data["late_fee_rate"]) == "0.02"
    assert data["penalty_account_engine_id"] == 77
    assert data["is_active"] is True


def test_create_contract_with_float_rate_is_422(api: TestClient) -> None:
    response = api.post(
        "/api/v1/installments/contracts", json=_contract_body(late_fee_rate=0.02)
    )
    assert response.status_code == 422, response.text


def test_get_contract_and_list_by_code(api: TestClient) -> None:
    created = api.post("/api/v1/installments/contracts", json=_contract_body()).json()
    api.post("/api/v1/installments/contracts", json=_contract_body(code="OTHER", version=1))
    response = api.get(f"/api/v1/installments/contracts/{created['id']}")
    assert response.status_code == 200, response.text
    assert response.json()["code"] == "STD-REST"
    response = api.get("/api/v1/installments/contracts?code=STD-REST")
    assert response.status_code == 200, response.text
    assert [row["code"] for row in response.json()] == ["STD-REST"]


def test_get_missing_contract_is_404(api: TestClient) -> None:
    response = api.get("/api/v1/installments/contracts/00000000-0000-0000-0000-000000000999")
    assert response.status_code == 404, response.text
    assert response.json()["code"] == "INSTALLMENT_CONTRACT_NOT_FOUND"


# -- BNPL (US-543) -------------------------------------------------------------------


def _bnpl_plan_body(**overrides) -> dict:
    body = _plan_body(
        invoice_id="INV-REST-BNPL",
        kind="bnpl",
        gateway_channel_key="snapppay",
        first_due_date=PAST_JALALI,
    )
    body.update(overrides)
    return body


def _bnpl_receipt_body(**overrides) -> dict:
    body = _receipt_body(
        invoice_id="INV-REST-BNPL",
        amount_toman=3_000_000,
        received_date="1405-08-29",
        payment_method="bnpl",
        gateway="snapppay",
        gateway_reference="SP-REST-1",
    )
    body.update(overrides)
    return body


def test_create_bnpl_plan_returns_201_with_kind_and_gateway(api: TestClient) -> None:
    response = api.post("/api/v1/installments/plans", json=_bnpl_plan_body())
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["kind"] == "bnpl"
    assert data["gateway_channel_key"] == "snapppay"
    # BNPL legs are never "overdue": no receipts yet -> expected.
    detail = api.get(f"/api/v1/installments/plans/{data['id']}")
    assert detail.status_code == 200, detail.text
    assert [leg["status"] for leg in detail.json()["legs"]] == [
        "expected",
        "expected",
        "expected",
    ]


def test_create_bnpl_plan_without_gateway_is_422(api: TestClient) -> None:
    body = _bnpl_plan_body()
    del body["gateway_channel_key"]
    response = api.post("/api/v1/installments/plans", json=body)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "INSTALLMENT_VALIDATION_FAILED"


def test_list_plans_filters_by_kind(api: TestClient, world: _World) -> None:
    world.create_plan()  # internal, invoice INV-REST-1
    world.create_plan(**_bnpl_plan_body())
    response = api.get("/api/v1/installments/plans?kind=bnpl")
    assert response.status_code == 200, response.text
    assert [row["invoice_id"] for row in response.json()] == ["INV-REST-BNPL"]
    response = api.get("/api/v1/installments/plans?kind=internal")
    assert response.status_code == 200, response.text
    assert [row["invoice_id"] for row in response.json()] == ["INV-REST-1"]


def test_list_plans_unknown_kind_is_422(api: TestClient) -> None:
    response = api.get("/api/v1/installments/plans?kind=other")
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "INSTALLMENT_VALIDATION_FAILED"


def test_bnpl_receipt_returns_201_with_gateway_payment(
    api: TestClient, world: _World
) -> None:
    created = world.create_plan(**_bnpl_plan_body())
    plan_id = created["plan"].id
    response = api.post(
        f"/api/v1/installments/plans/{plan_id}/receipts",
        json=_bnpl_receipt_body(),
    )
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["payment_method"] == "bnpl"
    assert data["gateway_channel_key"] == "snapppay"
    assert data["gateway_reference"] == "SP-REST-1"
    assert data["gateway_payment_id"] is not None
    assert data["bnpl_status"] == "active"
    assert len(data["allocations"]) == 3

    detail = api.get(f"/api/v1/installments/plans/{plan_id}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert [leg["status"] for leg in body["legs"]] == [
        "gateway_confirmed",
        "gateway_confirmed",
        "gateway_confirmed",
    ]
    assert body["gateway_receivable_irr"] == 26_700_000
    assert body["gateway_settled_irr"] == 0


def test_bnpl_receipt_missing_gateway_is_422(api: TestClient, world: _World) -> None:
    created = world.create_plan(**_bnpl_plan_body())
    body = _bnpl_receipt_body()
    del body["gateway"]
    response = api.post(
        f"/api/v1/installments/plans/{created['plan'].id}/receipts", json=body
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "INSTALLMENT_VALIDATION_FAILED"


def test_bnpl_receipt_on_internal_plan_is_422(api: TestClient, world: _World) -> None:
    created = world.create_plan(first_due_date=PAST_JALALI)
    body = _bnpl_receipt_body(invoice_id="INV-REST-1")
    response = api.post(
        f"/api/v1/installments/plans/{created['plan'].id}/receipts", json=body
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "INSTALLMENT_BNPL_PLAN_REQUIRED"


def test_reject_bnpl_receipt_returns_200_and_marks_rejected(
    api: TestClient, world: _World
) -> None:
    created = world.create_plan(**_bnpl_plan_body())
    plan_id = created["plan"].id
    receipt = api.post(
        f"/api/v1/installments/plans/{plan_id}/receipts",
        json=_bnpl_receipt_body(),
    ).json()
    response = api.post(
        f"/api/v1/installments/plans/{plan_id}/receipts/{receipt['id']}/reject-bnpl",
        json={"reason": "customer not eligible"},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["bnpl_status"] == "rejected"

    detail = api.get(f"/api/v1/installments/plans/{plan_id}")
    body = detail.json()
    assert [leg["status"] for leg in body["legs"]] == [
        "expected",
        "expected",
        "expected",
    ]
    assert body["gateway_receivable_irr"] == 0
    assert len(world.review_queue.items) == 1
    filed = next(iter(world.review_queue.items.values()))
    assert filed.subtype == "bnpl_gateway_rejection"


def test_reject_bnpl_receipt_twice_is_409(api: TestClient, world: _World) -> None:
    created = world.create_plan(**_bnpl_plan_body())
    plan_id = created["plan"].id
    receipt = api.post(
        f"/api/v1/installments/plans/{plan_id}/receipts",
        json=_bnpl_receipt_body(),
    ).json()
    url = f"/api/v1/installments/plans/{plan_id}/receipts/{receipt['id']}/reject-bnpl"
    first = api.post(url, json={"reason": "customer not eligible"})
    assert first.status_code == 200, first.text
    second = api.post(url, json={"reason": "again"})
    assert second.status_code == 409, second.text
    assert second.json()["code"] == "INSTALLMENT_BNPL_ALREADY_REJECTED"
