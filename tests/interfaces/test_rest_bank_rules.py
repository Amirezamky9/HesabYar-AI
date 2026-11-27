"""REST tests for the bank auto-coding rules endpoints (US-565, W3).

Real app + the real BankRulesService over the in-memory fakes from
tests.unit.test_bank_services / test_bank_rules; the only mocked
pieces are auth (a canned client), the DB session (a stub whose
``execute()`` returns the canned engine_companies row — the company
resolution helpers only ever call
``session.execute(...).scalar_one_or_none()``), and the statement
query service (delegating to the same FakeEngine the service writes
to, so confirm/undo see the same statement).

The SQLAlchemy repository + migration live in the migration tests;
the full-stack import/reconcile flow is covered by
test_rest_bank_import.py.
"""

from __future__ import annotations

import dataclasses
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from hesabyar.application.commands.bank_rules import BankRulesService
from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.domain.accounting.ports import AccountingEnginePort
from hesabyar.domain.accounting.refs import BankStatementRef
from hesabyar.domain.accounting.value_objects import BankStatementView
from hesabyar.domain.bank.models import (
    BankRuleSuggestion,
    BankStatementMatch,
    MatchStatus,
    MatchTargetType,
)
from hesabyar.domain.common.errors import BankStatementNotFoundError
from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.infrastructure.config import AppSettings
from hesabyar.infrastructure.persistence.postgres.models import EngineCompanyModel
from hesabyar.interfaces.rest.app import create_app
from hesabyar.interfaces.rest.dependencies import (
    get_authenticated_client,
    get_bank_rules_service,
    get_bank_statement_query_service,
    get_db_session,
)
from tests.unit.test_bank_rules import FakeBankRulesRepo
from tests.unit.test_bank_services import (
    COMPANY_ID,
    NOW,
    TENANT,
    FakeBankRepo,
    FakeEngine,
    FakeReviewQueue,
    _fd,
    _line,
)
from tests.unit.test_installment_services import INSTALLATION_ID

FULL_SCOPES = ("rules:read", "rules:manage", "banking:reconcile")

RULE_BODY: dict[str, Any] = {
    "company_id": COMPANY_ID,
    "name": "کارمزد اسنپ‌پی",
    "target_account_id": 601,
    "description_contains": "اسنپ",
    "direction": "out",
    "priority": 10,
}


class _FakeResult:
    """The one thing the company helpers call on a query result."""

    def __init__(self, row: EngineCompanyModel | None) -> None:
        self._row = row

    def scalar_one_or_none(self) -> EngineCompanyModel | None:
        return self._row


class _FakeSession:
    """A session stub: company lookups resolve to the canned row."""

    def __init__(self) -> None:
        self.company_row = EngineCompanyModel(
            id=COMPANY_ID,
            tenant_id=TENANT,
            engine_connection_id="connection-1",
            external_company_id="1",
            company_name="شرکت آزمون",
        )

    def execute(self, stmt: Any) -> _FakeResult:
        return _FakeResult(self.company_row)

    def commit(self) -> None:
        return None

    def close(self) -> None:
        return None


class _FakeQueryService:
    """Statement reads delegated to the FakeEngine the service writes to."""

    def __init__(self, engine: FakeEngine) -> None:
        self._engine = engine

    def get_bank_statement(self, ref: BankStatementRef) -> BankStatementView:
        view = self._engine.get_bank_statement(ref)
        if view is None:
            raise BankStatementNotFoundError(
                f"Bank statement {ref.engine_id} does not exist in the engine.",
                details={"statement_id": ref.engine_id},
            )
        return view


def _client(*, scopes: tuple[str, ...] = FULL_SCOPES) -> AuthenticatedClient:
    return AuthenticatedClient(
        client_id="test-client",
        tenant_id=TENANT,
        installation_id=INSTALLATION_ID,
        key_id="test-key",
        key_prefix="hya_live_test1234",
        client_name="test client",
        scopes=frozenset(scopes),
        engine_company_id=None,
    )


@pytest.fixture()
def world() -> dict[str, Any]:
    engine = FakeEngine()
    bank = FakeBankRepo()
    bank_rules = FakeBankRulesRepo()
    queue = FakeReviewQueue()
    review = ReviewQueueCommandService(queue, queue, now=NOW)
    service = BankRulesService(
        bank_rules,
        bank,
        cast(AccountingEnginePort, engine),
        review=review,
        now=NOW,
    )
    return {
        "engine": engine,
        "bank": bank,
        "bank_rules": bank_rules,
        "queue": queue,
        "service": service,
    }


def _make_api(world: dict[str, Any], *, scopes: tuple[str, ...] = FULL_SCOPES) -> TestClient:
    app = create_app(settings=AppSettings())
    app.dependency_overrides[get_authenticated_client] = lambda: _client(scopes=scopes)
    app.dependency_overrides[get_db_session] = lambda: _FakeSession()
    app.dependency_overrides[get_bank_rules_service] = lambda: world["service"]
    app.dependency_overrides[get_bank_statement_query_service] = lambda: _FakeQueryService(world["engine"])
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def api(world: dict[str, Any]) -> TestClient:
    return _make_api(world)


@pytest.fixture()
def api_read_only(world: dict[str, Any]) -> TestClient:
    return _make_api(world, scopes=("rules:read",))


def _create_rule(api: TestClient, body: dict[str, Any] | None = None) -> dict[str, Any]:
    response = api.post("/api/v1/bank/rules", json=body or RULE_BODY)
    assert response.status_code == 201, response.text
    return response.json()


def _seed_suggestion(world: dict[str, Any], rule_id: str) -> tuple[int, str]:
    """A draft statement + one pending suggestion for its first line."""
    statement_id = world["engine"].add_statement(
        lines=[_line(amount_irr=-50_000, description="پرداخت اسنپ‌پی بابت سفارش")]
    )
    suggestion = BankRuleSuggestion(
        id="sugg-1",
        tenant_id=TENANT,
        rule_id=rule_id,
        rule_name="کارمزد اسنپ‌پی",
        company_id=COMPANY_ID,
        statement_id=statement_id,
        statement_line_id=9001,
        line_description="پرداخت اسنپ‌پی بابت سفارش",
        line_amount_irr=-50_000,
        target_account_id=601,
        created_at=NOW,
    )
    world["bank_rules"].save_suggestion(suggestion)
    return statement_id, suggestion.id


# -- CRUD ----------------------------------------------------------------------------


def test_create_and_get_rule(api: TestClient) -> None:
    created = _create_rule(api)
    assert created["name"] == "کارمزد اسنپ‌پی"
    assert created["mode"] == "suggest"
    assert created["confirmations"] == 0
    assert created["auto_eligible"] is False
    assert created["target_account_id"] == 601
    assert created["direction"] == "out"
    assert created["tenant_id"] == TENANT
    assert created["company_id"] == COMPANY_ID
    got = api.get(f"/api/v1/bank/rules/{created['id']}")
    assert got.status_code == 200
    assert got.json()["id"] == created["id"]


def test_create_rule_without_conditions_is_422(api: TestClient) -> None:
    body = dict(RULE_BODY, description_contains=None)
    response = api.post("/api/v1/bank/rules", json=body)
    assert response.status_code == 422
    assert response.json()["code"] == "BANK_RULE_INVALID"


def test_create_rule_with_bad_range_is_422(api: TestClient) -> None:
    body = dict(
        RULE_BODY,
        description_contains=None,
        amount_min_irr=100_000,
        amount_max_irr=10_000,
    )
    response = api.post("/api/v1/bank/rules", json=body)
    assert response.status_code == 422
    assert response.json()["code"] == "BANK_RULE_INVALID"


def test_create_rule_with_bad_direction_is_422(api: TestClient) -> None:
    body = dict(RULE_BODY, direction="sideways")
    response = api.post("/api/v1/bank/rules", json=body)
    assert response.status_code == 422
    assert response.json()["code"] == "BANK_RULE_INVALID"


def test_get_unknown_rule_is_404(api: TestClient) -> None:
    response = api.get("/api/v1/bank/rules/does-not-exist")
    assert response.status_code == 404
    assert response.json()["code"] == "BANK_RULE_NOT_FOUND"


def test_list_rules_and_filters(api: TestClient) -> None:
    _create_rule(api)
    _create_rule(
        api,
        dict(RULE_BODY, name="دوم", priority=5, enabled=False, description_contains="زرین"),
    )
    listed = api.get("/api/v1/bank/rules")
    assert listed.status_code == 200
    assert [r["name"] for r in listed.json()] == ["دوم", "کارمزد اسنپ‌پی"]
    enabled = api.get("/api/v1/bank/rules", params={"enabled": True})
    assert [r["name"] for r in enabled.json()] == ["کارمزد اسنپ‌پی"]
    auto = api.get("/api/v1/bank/rules", params={"mode": "auto"})
    assert auto.json() == []
    bad_mode = api.get("/api/v1/bank/rules", params={"mode": "sometimes"})
    assert bad_mode.status_code == 422
    assert bad_mode.json()["code"] == "BANK_RULE_INVALID"


def test_update_rule(api: TestClient) -> None:
    created = _create_rule(api)
    response = api.put(
        f"/api/v1/bank/rules/{created['id']}",
        json={"name": "تغییر نام", "priority": 1},
    )
    assert response.status_code == 200
    assert response.json()["name"] == "تغییر نام"
    assert response.json()["priority"] == 1
    assert response.json()["description_contains"] == "اسنپ"


def test_update_rule_can_clear_a_condition(api: TestClient) -> None:
    created = _create_rule(api, dict(RULE_BODY, amount_min_irr=10_000, amount_max_irr=100_000))
    response = api.put(
        f"/api/v1/bank/rules/{created['id']}",
        json={"description_contains": None},
    )
    assert response.status_code == 200
    assert response.json()["description_contains"] is None
    assert response.json()["amount_min_irr"] == 10_000


def test_update_unknown_rule_is_404(api: TestClient) -> None:
    response = api.put("/api/v1/bank/rules/does-not-exist", json={"name": "x"})
    assert response.status_code == 404
    assert response.json()["code"] == "BANK_RULE_NOT_FOUND"


def test_delete_rule(api: TestClient) -> None:
    created = _create_rule(api)
    response = api.delete(f"/api/v1/bank/rules/{created['id']}")
    assert response.status_code == 204
    assert response.content == b""
    gone = api.get(f"/api/v1/bank/rules/{created['id']}")
    assert gone.status_code == 404


def test_delete_unknown_rule_is_404(api: TestClient) -> None:
    response = api.delete("/api/v1/bank/rules/does-not-exist")
    assert response.status_code == 404
    assert response.json()["code"] == "BANK_RULE_NOT_FOUND"


# -- enable / promote / demote -------------------------------------------------------


def test_enable_and_disable_rule(api: TestClient) -> None:
    created = _create_rule(api, dict(RULE_BODY, enabled=False))
    assert created["enabled"] is False
    enabled = api.post(f"/api/v1/bank/rules/{created['id']}/enable")
    assert enabled.status_code == 200
    assert enabled.json()["enabled"] is True
    disabled = api.post(f"/api/v1/bank/rules/{created['id']}/disable")
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False


def test_promote_below_threshold_is_409(api: TestClient) -> None:
    created = _create_rule(api)
    response = api.post(f"/api/v1/bank/rules/{created['id']}/promote")
    assert response.status_code == 409
    assert response.json()["code"] == "BANK_RULE_NOT_PROMOTABLE"


def test_promote_at_threshold_and_demote(api: TestClient, world: dict[str, Any]) -> None:
    created = _create_rule(api)
    rule = world["bank_rules"].get_bank_rule(created["id"], TENANT)
    assert rule is not None
    world["bank_rules"].update_bank_rule(dataclasses.replace(rule, confirmations=3))
    promoted = api.post(f"/api/v1/bank/rules/{created['id']}/promote")
    assert promoted.status_code == 200
    assert promoted.json()["mode"] == "auto"
    assert promoted.json()["confirmations"] == 3
    demoted = api.post(f"/api/v1/bank/rules/{created['id']}/demote")
    assert demoted.status_code == 200
    assert demoted.json()["mode"] == "suggest"
    assert demoted.json()["confirmations"] == 0


def test_demote_suggest_rule_is_409(api: TestClient) -> None:
    created = _create_rule(api)
    response = api.post(f"/api/v1/bank/rules/{created['id']}/demote")
    assert response.status_code == 409
    assert response.json()["code"] == "BANK_RULE_NOT_PROMOTABLE"


# -- suggestions ----------------------------------------------------------------------


def test_list_suggestions_and_filters(api: TestClient, world: dict[str, Any]) -> None:
    created = _create_rule(api)
    _seed_suggestion(world, created["id"])
    listed = api.get("/api/v1/bank/rules/suggestions")
    assert listed.status_code == 200
    assert len(listed.json()) == 1
    assert listed.json()[0]["rule_name"] == "کارمزد اسنپ‌پی"
    assert listed.json()[0]["status"] == "pending"
    confirmed = api.get("/api/v1/bank/rules/suggestions", params={"status": "confirmed"})
    assert confirmed.json() == []
    bad_status = api.get("/api/v1/bank/rules/suggestions", params={"status": "maybe"})
    assert bad_status.status_code == 422
    assert bad_status.json()["code"] == "BANK_RULE_INVALID"


def test_confirm_suggestion_applies_coding(api: TestClient, world: dict[str, Any]) -> None:
    created = _create_rule(api)
    statement_id, suggestion_id = _seed_suggestion(world, created["id"])
    response = api.post(
        f"/api/v1/bank/rules/suggestions/{suggestion_id}/confirm",
        headers={"Idempotency-Key": "confirm-1"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["match"]["status"] == "confirmed"
    assert body["rule"]["confirmations"] == 1
    assert body["rule"]["auto_eligible"] is False
    line = world["engine"].get_bank_statement(BankStatementRef(statement_id)).lines[0]
    assert line.account.engine_id == 601
    again = api.post(
        f"/api/v1/bank/rules/suggestions/{suggestion_id}/confirm",
        headers={"Idempotency-Key": "confirm-2"},
    )
    assert again.status_code == 409
    assert again.json()["code"] == "BANK_RULE_SUGGESTION_NOT_PENDING"


def test_confirm_suggestion_needs_idempotency_key(api: TestClient, world: dict[str, Any]) -> None:
    created = _create_rule(api)
    _, suggestion_id = _seed_suggestion(world, created["id"])
    response = api.post(f"/api/v1/bank/rules/suggestions/{suggestion_id}/confirm")
    assert response.status_code == 400
    assert response.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_confirm_unknown_suggestion_is_404(api: TestClient) -> None:
    response = api.post(
        "/api/v1/bank/rules/suggestions/does-not-exist/confirm",
        headers={"Idempotency-Key": "confirm-x"},
    )
    assert response.status_code == 404
    assert response.json()["code"] == "BANK_RULE_SUGGESTION_NOT_FOUND"


def test_reject_suggestion(api: TestClient, world: dict[str, Any]) -> None:
    created = _create_rule(api)
    _, suggestion_id = _seed_suggestion(world, created["id"])
    response = api.post(f"/api/v1/bank/rules/suggestions/{suggestion_id}/reject")
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    again = api.post(f"/api/v1/bank/rules/suggestions/{suggestion_id}/reject")
    assert again.status_code == 409
    assert again.json()["code"] == "BANK_RULE_SUGGESTION_NOT_PENDING"


# -- undo -----------------------------------------------------------------------------


def test_undo_auto_code(api: TestClient, world: dict[str, Any]) -> None:
    statement_id = world["engine"].add_statement(
        lines=[_line(amount_irr=-50_000, description="پرداخت اسنپ‌پی بابت سفارش")]
    )
    world["bank"].save_match(
        BankStatementMatch(
            id="match-auto",
            tenant_id=TENANT,
            company_id=COMPANY_ID,
            statement_id=statement_id,
            statement_line_id=9001,
            line_date=_fd(),
            line_amount_irr=-50_000,
            line_description="پرداخت اسنپ‌پی بابت سفارش",
            target_type=MatchTargetType.RULE,
            target_id=None,
            target_ref="rule-1",
            target_label="قاعدهٔ بانکی «کارمزد اسنپ‌پی»",
            priority=3,
            confidence=90,
            status=MatchStatus.AUTO_APPLIED,
            matched_at=NOW,
            matched_by="reconcile",
        )
    )
    response = api.post(
        f"/api/v1/bank/statements/{statement_id}/lines/9001/undo-auto-code",
        json={"corrected_account_id": 501},
        headers={"Idempotency-Key": "undo-1"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["undone_rule_id"] == "rule-1"
    assert body["undone_match_id"] == "match-auto"
    assert body["match"]["status"] == "confirmed"
    line = world["engine"].get_bank_statement(BankStatementRef(statement_id)).lines[0]
    assert line.account.engine_id == 501


def test_undo_without_auto_match_is_409(api: TestClient, world: dict[str, Any]) -> None:
    statement_id = world["engine"].add_statement(
        lines=[_line(amount_irr=-50_000, description="پرداخت اسنپ‌پی بابت سفارش")]
    )
    response = api.post(
        f"/api/v1/bank/statements/{statement_id}/lines/9001/undo-auto-code",
        json={"corrected_account_id": 501},
        headers={"Idempotency-Key": "undo-2"},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "BANK_RULE_NOTHING_TO_UNDO"


def test_undo_needs_idempotency_key(api: TestClient, world: dict[str, Any]) -> None:
    statement_id = world["engine"].add_statement(lines=[_line()])
    response = api.post(
        f"/api/v1/bank/statements/{statement_id}/lines/9001/undo-auto-code",
        json={"corrected_account_id": 501},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"


# -- scopes + OpenAPI ------------------------------------------------------------------


def test_read_only_key_cannot_write(api_read_only: TestClient) -> None:
    response = api_read_only.post("/api/v1/bank/rules", json=RULE_BODY)
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"
    listed = api_read_only.get("/api/v1/bank/rules")
    assert listed.status_code == 200


def test_openapi_lists_bank_rule_paths(api: TestClient) -> None:
    spec = api.get("/openapi.json")
    assert spec.status_code == 200
    paths = spec.json()["paths"]
    for path in (
        "/api/v1/bank/rules",
        "/api/v1/bank/rules/{rule_id}",
        "/api/v1/bank/rules/{rule_id}/promote",
        "/api/v1/bank/rules/{rule_id}/demote",
        "/api/v1/bank/rules/suggestions",
        "/api/v1/bank/rules/suggestions/{suggestion_id}/confirm",
        "/api/v1/bank/rules/suggestions/{suggestion_id}/reject",
        "/api/v1/bank/statements/{statement_id}/lines/{line_id}/undo-auto-code",
    ):
        assert path in paths, path
