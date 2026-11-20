"""REST tests for the US-534 Sayyad + clearing routes.

No servers needed: the app is built with dependency overrides —
a canned authenticated client, the in-memory fakes from
``tests.unit.test_cheque_services`` / ``test_cheque_clearing_service``
behind the services, and a stub session that resolves the company
mapping. The full-stack path (real DB + real engine) is covered by
the contract tests.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from hesabyar.application.commands.cheque_clearing import ChequeClearingService
from hesabyar.application.commands.cheques import ChequeCommandService
from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.application.commands.sayyad import SayyadInquiryService
from hesabyar.application.queries.cheques import ChequeQueryService
from hesabyar.domain.accounting.ports import AccountingEnginePort
from hesabyar.infrastructure.config import AppSettings
from hesabyar.infrastructure.sayyad.simulator import (
    DeterministicEhrazinoSimulator,
    DeterministicFintechSayyadSimulator,
)
from hesabyar.interfaces.rest.app import create_app
from hesabyar.interfaces.rest.dependencies import (
    get_authenticated_client,
    get_cheque_clearing_service,
    get_cheque_command_service,
    get_cheque_query_service,
    get_db_session,
    get_sayyad_inquiry_service,
)
from tests.interfaces.test_rest_cheques import (
    ALL_CHEQUE_SCOPES,
    _client,
    _company_row,
    _FakeSession,
)
from tests.unit.test_cheque_clearing_service import (
    ClearingFakeEngine,
    FakeBankRepo,
    RecordingReviewQueue,
)
from tests.unit.test_cheque_services import (
    FakeChequeRepository,
    _held,
    _in_clearing,
)


class _World:
    def __init__(self) -> None:
        self.repo = FakeChequeRepository()
        self.engine = ClearingFakeEngine()
        self.bank = FakeBankRepo()
        self.review = RecordingReviewQueue()
        self.commands = ChequeCommandService(
            self.repo,
            cast(AccountingEnginePort, self.engine),
            review=cast(ReviewQueueCommandService, self.review),
        )
        self.queries = ChequeQueryService(self.repo)
        self.sayyad = SayyadInquiryService(
            cast(Any, self.repo),
            finnotech=DeterministicFintechSayyadSimulator(),
            ehrazino=DeterministicEhrazinoSimulator(),
            review=cast(ReviewQueueCommandService, self.review),
        )
        self.clearing = ChequeClearingService(
            cast(AccountingEnginePort, self.engine),
            cast(Any, self.repo),
            cast(Any, self.bank),
            self.commands,
            cast(ReviewQueueCommandService, self.review),
            None,
        )

    def held(self, **overrides):  # type: ignore[no-untyped-def]
        return _held(self.commands, **overrides)

    def in_clearing(self, **overrides):  # type: ignore[no-untyped-def]
        return _in_clearing(self.commands, **overrides)


@pytest.fixture()
def world() -> _World:
    return _World()


@pytest.fixture()
def api(world: _World) -> TestClient:
    app = create_app(settings=AppSettings())
    app.dependency_overrides[get_authenticated_client] = lambda: _client(scopes=ALL_CHEQUE_SCOPES)
    app.dependency_overrides[get_db_session] = lambda: _FakeSession(_company_row())
    app.dependency_overrides[get_cheque_command_service] = lambda: world.commands
    app.dependency_overrides[get_cheque_query_service] = lambda: world.queries
    app.dependency_overrides[get_sayyad_inquiry_service] = lambda: world.sayyad
    app.dependency_overrides[get_cheque_clearing_service] = lambda: world.clearing
    return TestClient(app, raise_server_exceptions=False)


def test_sayyad_inquiry_returns_201_with_verdict_receipt(
    api: TestClient, world: _World
) -> None:
    cheque = world.held()
    response = api.post(
        f"/api/v1/cheques/{cheque.id}/sayyad-inquiries",
        json={"scope": "credit:cheque-color-inquiry:get", "scenario": "clean"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["channel"] == "finnotech_api"
    assert body["inquiry_type"] == "color"
    assert body["result_code"] == "color=1"


def test_sayyad_inquiry_unknown_scope_is_422(api: TestClient, world: _World) -> None:
    cheque = world.held()
    response = api.post(
        f"/api/v1/cheques/{cheque.id}/sayyad-inquiries",
        json={"scope": "credit:not-a-scope:get"},
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "SAYYAD_INQUIRY_FAILED"


def test_ehrazino_color_inquiry_returns_201_with_cost(
    api: TestClient, world: _World
) -> None:
    cheque = world.held()
    response = api.post(
        f"/api/v1/cheques/{cheque.id}/ehrazino-inquiries",
        json={"kind": "color", "scenario": "clean"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["channel"] == "ehrazino_api"
    assert body["result_code"] == "color=1"
    assert body["cost_irr"] == 17_000


def test_ehrazino_unknown_kind_is_422(api: TestClient, world: _World) -> None:
    cheque = world.held()
    response = api.post(
        f"/api/v1/cheques/{cheque.id}/ehrazino-inquiries", json={"kind": "bogus"}
    )
    assert response.status_code == 422, response.text


def test_list_inquiries_returns_receipts(api: TestClient, world: _World) -> None:
    cheque = world.held()
    api.post(
        f"/api/v1/cheques/{cheque.id}/sayyad-inquiries",
        json={"scope": "credit:cheque-color-inquiry:get", "scenario": "clean"},
    )
    response = api.get(f"/api/v1/cheques/{cheque.id}/inquiries")
    assert response.status_code == 200, response.text
    assert len(response.json()) == 1


def test_in_clearing_aging_lists_in_clearing_cheques(
    api: TestClient, world: _World
) -> None:
    cheque = world.in_clearing()
    response = api.get("/api/v1/cheques/reports/in-clearing-aging")
    assert response.status_code == 200, response.text
    rows = response.json()
    assert len(rows) == 1
    assert rows[0]["cheque_id"] == cheque.id
    assert rows[0]["stale"] is False


def test_inquiry_ledger_aggregates_costs(api: TestClient, world: _World) -> None:
    cheque = world.held()
    api.post(
        f"/api/v1/cheques/{cheque.id}/ehrazino-inquiries",
        json={"kind": "color", "scenario": "clean"},
    )
    response = api.get("/api/v1/cheques/reports/inquiry-ledger")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total_receipts"] == 1
    assert body["by_channel"] == {"ehrazino_api": 1}
    assert body["total_cost_irr"] == 17_000


def test_receipt_rule_audit_files_missing_receipt_items(
    api: TestClient, world: _World
) -> None:
    world.held()  # held without any inquiry receipt
    response = api.post("/api/v1/cheques/reports/receipt-rule-audit")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["checked"] == 1
    assert body["missing"] == 1
    assert body["items_filed"] == 1


def test_clearing_confirm_without_suggested_match_is_422(
    api: TestClient, world: _World
) -> None:
    cheque = world.in_clearing()
    response = api.post(
        f"/api/v1/cheques/{cheque.id}/clearing/confirm",
        json={"statement_id": 700, "line_id": 9001},
    )
    assert response.status_code == 422, response.text
