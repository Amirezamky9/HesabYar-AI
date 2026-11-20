"""REST tests for the cheque routes (US-531 + US-532).

No servers needed: the app is built with dependency overrides —
a canned authenticated client, the in-memory fakes from
``tests.unit.test_cheque_services`` behind the command/query
services, and a stub session that resolves the company mapping.
The full-stack path (real DB + real engine) is covered by the
contract tests once US-530 installs ``account_check_ir``.
"""

from __future__ import annotations

from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hesabyar.application.commands.cheques import ChequeCommandService
from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.application.queries.cheques import ChequeQueryService
from hesabyar.domain.accounting.ports import AccountingEnginePort
from hesabyar.domain.accounting.refs import CheckbookEngineRef, CompanyRef
from hesabyar.domain.accounting.value_objects import CheckbookEngineView
from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.infrastructure.config import AppSettings
from hesabyar.infrastructure.persistence.postgres.models import EngineCompanyModel
from hesabyar.interfaces.rest.app import create_app
from hesabyar.interfaces.rest.dependencies import (
    get_authenticated_client,
    get_cheque_command_service,
    get_cheque_query_service,
    get_db_session,
)
from tests.unit.test_cheque_services import (
    COMPANY_ENGINE_ID,
    INSTALLATION_ID,
    TENANT_ID,
    FakeChequeRepository,
    FakeEngine,
    RecordingReview,
    _collateral_held,
    _held,
    _in_clearing,
    _issued,
    _payload,
    _register,
    _register_issued,
)

COMPANY_ROW_ID = "00000000-0000-0000-0000-000000000531"
ALL_CHEQUE_SCOPES = ("cheques:read", "cheques:write", "cheques:transition")


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


def _client(*, scopes: tuple[str, ...] = ALL_CHEQUE_SCOPES) -> AuthenticatedClient:
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


class _World:
    def __init__(self) -> None:
        self.repo = FakeChequeRepository()
        self.engine = FakeEngine()
        self.review = RecordingReview()
        # Partial fakes (see test_cheque_services._service for why).
        self.commands = ChequeCommandService(
            self.repo,
            cast(AccountingEnginePort, self.engine),
            review=cast(ReviewQueueCommandService, self.review),
        )
        self.queries = ChequeQueryService(
            self.repo, cast(AccountingEnginePort, self.engine)
        )

    def held(self, **overrides):
        return _held(self.commands, **overrides)

    def in_clearing(self, **overrides):
        return _in_clearing(self.commands, **overrides)

    def issued(self, **overrides):
        return _issued(self.commands, **overrides)

    def collateral_held(self, **overrides):
        return _collateral_held(self.commands, **overrides)

    def seed_checkbook(self) -> None:
        self.engine.seed_checkbook(
            CheckbookEngineView(
                ref=CheckbookEngineRef(engine_id=7),
                company=CompanyRef(engine_id=COMPANY_ENGINE_ID),
                name="دسته‌چک سامان",
                bank_account_engine_id=9,
                first_number=100,
                last_number=109,
                total_leaves=10,
                used_leaves=3,
                voided_leaves=1,
                remaining_leaves=6,
            )
        )


@pytest.fixture()
def world() -> _World:
    return _World()


@pytest.fixture()
def api(world: _World, request: pytest.FixtureRequest) -> TestClient:
    app = create_app(settings=AppSettings())
    scopes = getattr(request, "param", ALL_CHEQUE_SCOPES)
    app.dependency_overrides[get_authenticated_client] = lambda: _client(scopes=scopes)
    app.dependency_overrides[get_db_session] = lambda: _FakeSession(_company_row())
    app.dependency_overrides[get_cheque_command_service] = lambda: world.commands
    app.dependency_overrides[get_cheque_query_service] = lambda: world.queries
    return TestClient(app, raise_server_exceptions=False)


def _create_body(**overrides) -> dict:
    body = {
        "company_id": COMPANY_ROW_ID,
        **_payload(),
    }
    body.update(overrides)
    return body


# -- registration -------------------------------------------------------------------


def test_create_returns_201_registered_with_no_journal(api: TestClient) -> None:
    response = api.post("/api/v1/cheques", json=_create_body())
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["state"] == "registered"
    assert data["amount_irr"] == 17_500_000
    assert data["amount_toman"] == 1_750_000
    assert data["move_ids"] == {}
    assert data["sayad_id"] == "1234567890123456"


def test_create_accepts_the_engine_company_id_too(api: TestClient) -> None:
    response = api.post(
        "/api/v1/cheques", json=_create_body(company_id=str(COMPANY_ENGINE_ID))
    )
    assert response.status_code == 201, response.text


def test_create_duplicate_sayad_is_409(api: TestClient) -> None:
    """US-533 convergence: an identical re-registration is idempotent
    OK (the same record); only a CONTRADICTING payload conflicts."""
    first = api.post("/api/v1/cheques", json=_create_body())
    assert first.status_code == 201
    again = api.post("/api/v1/cheques", json=_create_body())
    assert again.status_code == 201, again.text  # identical -> same record
    assert again.json()["id"] == first.json()["id"]
    response = api.post("/api/v1/cheques", json=_create_body(amount_toman=9990000))
    assert response.status_code == 409, response.text


def test_create_invalid_payload_is_422(api: TestClient) -> None:
    response = api.post("/api/v1/cheques", json=_create_body(sayad_id="bad"))
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("api", [("cheques:read",)], indirect=True)
def test_create_without_write_scope_is_403(api: TestClient) -> None:
    response = api.post("/api/v1/cheques", json=_create_body())
    assert response.status_code == 403, response.text


def test_create_with_unknown_company_is_404(api: TestClient, world: _World) -> None:
    cast(FastAPI, api.app).dependency_overrides[get_db_session] = lambda: _FakeSession(None)
    response = api.post("/api/v1/cheques", json=_create_body())
    assert response.status_code == 404, response.text


# -- reads ------------------------------------------------------------------------------


def test_get_cheque_detail_carries_custody_and_receipts(
    api: TestClient, world: _World
) -> None:
    cheque = world.held()
    response = api.get(f"/api/v1/cheques/{cheque.id}")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["state"] == "held"
    assert data["move_ids"] == {"confirm_receipt": 5000}
    assert len(data["custody_entries"]) == 1
    assert data["custody_entries"][0]["to_location"] == "vault"


def test_get_unknown_cheque_is_404(api: TestClient) -> None:
    response = api.get("/api/v1/cheques/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404, response.text


def test_list_cheques_filters_by_state(api: TestClient, world: _World) -> None:
    world.held()
    _register(world.commands, sayad_id="2222222222222222")  # a second, still-registered cheque
    response = api.get("/api/v1/cheques?state=held")
    assert response.status_code == 200, response.text
    assert len(response.json()) == 1
    assert response.json()[0]["state"] == "held"
    response = api.get("/api/v1/cheques?state=bogus")
    assert response.status_code == 422, response.text


def test_get_settings_returns_the_section_9_1_defaults(
    api: TestClient,
) -> None:
    response = api.get("/api/v1/cheques/settings")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["leaf_arrival_deadline_days"] == 7
    assert data["bounce_destination"] == "receivables"
    assert data["red_brown_policy"] == "manager_approval"
    assert data["default_storage_location"] == "vault"


# -- transitions ---------------------------------------------------------------------------


def test_confirm_receipt_moves_to_held(api: TestClient, world: _World) -> None:
    cheque = _register(world.commands)
    response = api.post(
        f"/api/v1/cheques/{cheque.id}/confirm-receipt",
        json={"items_matched": True, "sayad_accepted": True, "color_code": 2},
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "held"
    assert response.json()["move_ids"] == {"confirm_receipt": 5000}


def test_confirm_receipt_mismatch_is_422(api: TestClient, world: _World) -> None:
    cheque = _register(world.commands)
    response = api.post(
        f"/api/v1/cheques/{cheque.id}/confirm-receipt",
        json={"items_matched": False, "sayad_accepted": True},
    )
    assert response.status_code == 422, response.text


def test_deposit_clear_and_bounce_walk_the_lifecycle(
    api: TestClient, world: _World
) -> None:
    cheque = world.held()
    response = api.post(
        f"/api/v1/cheques/{cheque.id}/deposit",
        json={"destination_account_id": 42, "destination_bank_name": "بانک سامان"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "in_clearing"
    assert response.json()["storage_location"] == "bank"

    response = api.post(
        f"/api/v1/cheques/{cheque.id}/bounce",
        json={
            "bounce_reason": "عدم کفایت موجودی",
            "bounce_date": "1405-10-20",
            "certificate_reference": "cert-1",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "bounced"
    assert response.json()["installment_leg_status"] is None

    response = api.post(f"/api/v1/cheques/{cheque.id}/return", json={})
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "returned"


def test_clear_marks_the_leg_paid(api: TestClient, world: _World) -> None:
    cheque = world.in_clearing(installment_leg_id="leg-7")
    response = api.post(
        f"/api/v1/cheques/{cheque.id}/clear",
        json={"statement_reference": "stmt-1"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "cleared"
    assert response.json()["installment_leg_status"] == "paid"


def test_transition_without_transition_scope_is_403(
    api: TestClient, world: _World
) -> None:
    cheque = _register(world.commands)
    cast(FastAPI, api.app).dependency_overrides[get_authenticated_client] = (
        lambda: _client(scopes=("cheques:read", "cheques:write"))
    )
    response = api.post(
        f"/api/v1/cheques/{cheque.id}/confirm-receipt",
        json={"items_matched": True, "sayad_accepted": True},
    )
    assert response.status_code == 403, response.text


def test_illegal_transition_is_409(api: TestClient, world: _World) -> None:
    cheque = _register(world.commands)
    response = api.post(
        f"/api/v1/cheques/{cheque.id}/clear",
        json={},
    )
    assert response.status_code == 409, response.text


def test_reject_mismatch_is_a_no_journal_terminal_state(
    api: TestClient, world: _World
) -> None:
    cheque = _register(world.commands)
    response = api.post(
        f"/api/v1/cheques/{cheque.id}/reject-mismatch",
        json={"mismatch_detail": "مبلغ چک با سامانه صیاد تطابق ندارد"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "rejected_mismatch"
    assert response.json()["move_ids"] == {}


def test_record_receipt_returns_201(api: TestClient, world: _World) -> None:
    cheque = _register(world.commands)
    response = api.post(
        f"/api/v1/cheques/{cheque.id}/receipts",
        json={
            "channel": "finnotech_api",
            "inquiry_type": "color",
            "result_code": "white",
            "result_summary": "رنگ سفید — تضمین نیست",
            "reference": "inq-2",
            "cost_irr": 3500,
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["result_code"] == "white"
    assert response.json()["cost_irr"] == 3500


def test_endorse_to_other_defers_the_journal(api: TestClient, world: _World) -> None:
    cheque = world.held()
    response = api.post(
        f"/api/v1/cheques/{cheque.id}/endorse",
        json={
            "endorsee_name": "شخص ثالث",
            "endorsee_type": "other",
            "sayad_transfer_reference": "transfer-2",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "endorsed"
    assert response.json()["journal_pending_review"] is True
    assert response.json()["move_ids"] == {"confirm_receipt": 5000}


# -- US-532: issued / collateral ---------------------------------------------------


def test_create_issued_requires_a_checkbook(api: TestClient) -> None:
    response = api.post("/api/v1/cheques", json=_create_body(kind="issued"))
    assert response.status_code == 422


def test_create_issued_records_checkbook_and_obligation(
    api: TestClient,
) -> None:
    response = api.post(
        "/api/v1/cheques",
        json=_create_body(
            kind="issued",
            purpose="collateral",
            checkbook_id=7,
            secured_obligation="قرارداد اجاره ۱۴۰۵/۰۳",
        ),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["kind"] == "issued"
    assert body["purpose"] == "collateral"
    assert body["checkbook_engine_id"] == 7
    assert body["secured_obligation"] == "قرارداد اجاره ۱۴۰۵/۰۳"


def test_issue_then_void_walks_the_issued_lifecycle(
    api: TestClient, world: _World
) -> None:
    cheque = _register_issued(world.commands)
    response = api.post(
        f"/api/v1/cheques/{cheque.id}/issue",
        json={"destination_account_id": 42, "destination_bank_name": "بانک سامان"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "issued"
    assert response.json()["destination_bank_account_id"] == 42
    response = api.post(
        f"/api/v1/cheques/{cheque.id}/void", json={"void_reason": "اشتباه در مبلغ"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "voided"
    assert body["void_reason"] == "اشتباه در مبلغ"


def test_issue_then_clear_and_bounce(api: TestClient, world: _World) -> None:
    cleared = world.issued(sayad_id="1111111111111111")
    response = api.post(f"/api/v1/cheques/{cleared.id}/clear", json={})
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "cleared"
    # An issued bounce needs reason + date only — no certificate.
    bounced = world.issued(sayad_id="2222222222222222")
    response = api.post(
        f"/api/v1/cheques/{bounced.id}/bounce",
        json={"bounce_reason": "عدم کفایت موجودی", "bounce_date": "1405-10-20"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "bounced"


def test_call_in_requires_approval_then_flips_to_commercial(
    api: TestClient, world: _World
) -> None:
    cheque = world.collateral_held()
    response = api.post(
        f"/api/v1/cheques/{cheque.id}/call-in", json={"accountant_approval": "  "}
    )
    assert response.status_code == 422
    response = api.post(
        f"/api/v1/cheques/{cheque.id}/call-in",
        json={"accountant_approval": "حسابدار ارشد"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "called_in"
    assert body["purpose"] == "payment"
    assert "call_in_reversal" in body["move_ids"]  # سند ۹الف
    assert "call_in" in body["move_ids"]  # سند ۹ب
    assert body["call_in_approved_by"] == "حسابدار ارشد"


def test_unexpected_call_in_reverses_the_memorandum_only(
    api: TestClient, world: _World
) -> None:
    cheque = world.issued(purpose="collateral")
    response = api.post(
        f"/api/v1/cheques/{cheque.id}/unexpected-call-in",
        json={"statement_reference": "stmt-1"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "called_in"
    assert body["unexpected_call_in"] is True
    assert body["journal_pending_review"] is True
    assert "unexpected_call_in" in body["move_ids"]
    assert "call_in" not in body["move_ids"]  # the commercial journal is gated
    assert any("بدون اطلاع" in warning for warning in body["warnings"])


def test_endorsed_bounce_posts_no_journal(api: TestClient, world: _World) -> None:
    cheque = world.held()
    endorsed = api.post(
        f"/api/v1/cheques/{cheque.id}/endorse",
        json={
            "endorsee_name": "تامین‌کننده الف",
            "endorsee_type": "supplier",
            "sayad_transfer_reference": "trf-1",
        },
    )
    assert endorsed.status_code == 200, endorsed.text
    moves_before = dict(endorsed.json()["move_ids"])
    response = api.post(
        f"/api/v1/cheques/{cheque.id}/endorsed-bounce",
        json={
            "bounce_reason": "عدم کفایت موجودی",
            "bounce_date": "1405-10-20",
            "certificate_reference": "cert-9",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "bounced"
    assert body["move_ids"] == moves_before  # §9.1 #3: no journal guessed
    assert body["journal_pending_review"] is True


def test_collateral_summary_and_list_filters_segregate(
    api: TestClient, world: _World
) -> None:
    world.held(sayad_id="1111111111111111")
    world.collateral_held(sayad_id="2222222222222222")
    response = api.get("/api/v1/cheques/collateral-summary")
    assert response.status_code == 200, response.text
    body = response.json()
    assert [c["sayad_id"] for c in body["cheques"]] == ["2222222222222222"]
    assert body["received_total_irr"] == 17_500_000
    assert body["issued_total_irr"] == 0
    response = api.get("/api/v1/cheques", params={"purpose": "collateral"})
    assert response.status_code == 200, response.text
    assert [c["sayad_id"] for c in response.json()] == ["2222222222222222"]
    response = api.get("/api/v1/cheques", params={"kind": "issued"})
    assert response.status_code == 200, response.text
    assert response.json() == []


def test_endorsement_summary_splits_supplier_from_other(
    api: TestClient, world: _World
) -> None:
    first = world.held(sayad_id="1111111111111111")
    api.post(
        f"/api/v1/cheques/{first.id}/endorse",
        json={
            "endorsee_name": "تامین‌کننده الف",
            "endorsee_type": "supplier",
            "sayad_transfer_reference": "trf-1",
        },
    )
    second = world.held(sayad_id="2222222222222222")
    api.post(
        f"/api/v1/cheques/{second.id}/endorse",
        json={
            "endorsee_name": "شخص ثالث",
            "endorsee_type": "other",
            "sayad_transfer_reference": "trf-2",
        },
    )
    response = api.get("/api/v1/cheques/endorsement-summary")
    assert response.status_code == 200, response.text
    body = response.json()
    assert [c["sayad_id"] for c in body["supplier"]] == ["1111111111111111"]
    assert [c["sayad_id"] for c in body["other"]] == ["2222222222222222"]


def test_checkbooks_list_and_get(api: TestClient, world: _World) -> None:
    world.seed_checkbook()
    response = api.get("/api/v1/cheques/checkbooks", params={"company_id": COMPANY_ENGINE_ID})
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body) == 1
    row = body[0]
    assert row["checkbook_engine_id"] == 7
    assert row["name"] == "دسته‌چک سامان"
    assert row["total_leaves"] == 10
    assert row["used_leaves"] == 3
    assert row["voided_leaves"] == 1
    assert row["remaining_leaves"] == 6
    assert row["leaves_registered_in_sayad"] is None  # simulator never invents it
    assert row["inquiry_simulated"] is True
    response = api.get(
        "/api/v1/cheques/checkbooks/7", params={"company_id": COMPANY_ENGINE_ID}
    )
    assert response.status_code == 200, response.text
    assert response.json()["checkbook_engine_id"] == 7
    response = api.get(
        "/api/v1/cheques/checkbooks/9999", params={"company_id": COMPANY_ENGINE_ID}
    )
    assert response.status_code == 404
