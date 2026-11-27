"""End-to-end tests for the cash-box endpoints (US-516, Phase 5 wave 2, K2).

The real FastAPI app, real issued API keys against the real database,
and the LIVE Tryton 8.2.3 server, using ONLY the dedicated test company
"HesabYar Adapter Test" and the statement journals provisioned by
``~/workspace/coord-logs/us516-provision-cashboxes.py`` (looked up BY
NAME — numeric engine ids are never hardcoded).

The Tryton connection comes from AppSettings (HESABYAR_TRYTON__*
env, i.e. the Supabase trytond on :8010) — the same connection the
app under test uses — so these tests run wherever the app runs.

Posted variance moves accumulate in the test company by design (they
cannot be deleted); drafts the tests leave behind are deleted in
teardown. Balances are NEVER hardcoded: every balance assertion
compares against the adapter's live read at test time, so the suite
stays green as other stories post moves into the shared company.

When trytond is not reachable (or the test company / statement
journals have not been provisioned yet), every test SKIPS cleanly with
a clear message — the suite stays green without a server. With the
server up, the tests must PASS.
"""

from __future__ import annotations

import datetime
import os
import uuid
from collections.abc import Generator
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from hesabyar.domain.accounting.refs import AccountRef, CompanyRef
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.infrastructure.accounting.tryton.adapter import TrytonAccountingAdapter
from hesabyar.infrastructure.accounting.tryton.client import TrytonRestClient
from hesabyar.infrastructure.auth import ApiKeyCrypto, ApiKeyService
from hesabyar.infrastructure.config import AppSettings
from hesabyar.infrastructure.persistence.postgres.models import (
    ApiClientModel,
    Base,
    EngineCompanyModel,
    EngineConnectionModel,
    IdempotencyRecordModel,
    TenantModel,
)
from hesabyar.interfaces.rest.app import create_app

TEST_DB_URL = os.getenv(
    "HESABYAR_DATABASE_URL",
    "postgresql+psycopg://hesabyar:hesabyar_pass@hesabyar-postgres:5432/hesabyar_test",
)

TEST_COMPANY_NAME = os.environ.get("TRYTON_TEST_COMPANY", "HesabYar Adapter Test")
STORE_JOURNAL_NAME = "HesabYar US-516 Store Cash Box"
PETTY_JOURNAL_NAME = "HesabYar US-516 Petty Cash Box"
STORE_ACCOUNT_CODE = "1.1.1"
PETTY_ACCOUNT_CODE = "1.1.5"

INSTALLATION_ID = "00000000-0000-0000-0000-000000000516"

CASHIER = "صندوقدار آزمون"
SUPERVISOR = "ناظر آزمون"
MANAGER = "مدیر آزمون"
MINUTES_TEXT = "صورتجلسهٔ شمارش آزمون: کسری به مبلغ مشخص در صندوق مشاهده شد."

MISSING_ID = 999_999_999


# ---------------------------------------------------------------------------
# Live Tryton fixtures (skip cleanly when the server is down)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def tryton_client() -> Generator[TrytonRestClient, None, None]:
    """The Tryton connection the app itself uses (AppSettings)."""
    t = AppSettings().tryton
    rest_client = TrytonRestClient(t.base_url, t.database, t.api_key)
    if not rest_client.ping():
        rest_client.close()
        pytest.skip(
            f"trytond server not reachable at {t.base_url} (start it per docs/dev-setup.md §3 to run the e2e tests)"
        )
    yield rest_client
    rest_client.close()


@pytest.fixture(scope="module")
def adapter(tryton_client: TrytonRestClient) -> TrytonAccountingAdapter:
    return TrytonAccountingAdapter(tryton_client)


@pytest.fixture(scope="module")
def test_company(adapter: TrytonAccountingAdapter) -> CompanyRef:
    """The Adapter Test company's engine ref, looked up BY NAME."""
    for view in adapter.list_companies():
        if view.name == TEST_COMPANY_NAME:
            return view.ref
    pytest.skip(
        f"Test company {TEST_COMPANY_NAME!r} not found in the Tryton DB. "
        "Provision it with setup_adapter_test_company.py (see docs/adapter.md)."
    )


@dataclass
class StatementJournals:
    """The provisioned US-516 statement journals, looked up BY NAME."""

    store_id: int
    petty_id: int
    store_account_id: int
    petty_account_id: int


@pytest.fixture(scope="module")
def statement_journals(adapter: TrytonAccountingAdapter, test_company: CompanyRef) -> StatementJournals:
    journals = {j.name: j for j in adapter.list_statement_journals(test_company)}
    missing = [n for n in (STORE_JOURNAL_NAME, PETTY_JOURNAL_NAME) if n not in journals]
    if missing:
        pytest.skip(
            f"Statement journals missing from the test company: {missing}. "
            "Provision them with ~/workspace/coord-logs/us516-provision-cashboxes.py."
        )
    store = journals[STORE_JOURNAL_NAME]
    petty = journals[PETTY_JOURNAL_NAME]
    assert store.account_code == STORE_ACCOUNT_CODE
    assert petty.account_code == PETTY_ACCOUNT_CODE
    return StatementJournals(
        store_id=store.ref.engine_id,
        petty_id=petty.ref.engine_id,
        store_account_id=store.account.engine_id,
        petty_account_id=petty.account.engine_id,
    )


@pytest.fixture()
def created_drafts(tryton_client: TrytonRestClient) -> Generator[list[int], None, None]:
    """Track draft moves a test creates and delete them afterwards.

    Posted moves cannot be deleted (they accumulate by design); the
    teardown reads each move's state first and deletes only drafts.
    """
    ids: list[int] = []
    yield ids
    for move_id in ids:
        try:
            record = tryton_client.read("account.move", move_id, fields=["state"])
        except Exception:
            continue
        if record is not None and record.get("state") == "draft":
            tryton_client.delete("account.move", move_id)


def _live_balance(adapter: TrytonAccountingAdapter, company: CompanyRef, account_id: int) -> int:
    """The engine's current posted balance for one account (never hardcoded)."""
    view = adapter.get_account_balance(
        company, AccountRef(account_id), FiscalDate.from_gregorian(datetime.date.today())
    )
    return view.balance.amount


# ---------------------------------------------------------------------------
# Provisioned world (real DB, real keys, real app)
# ---------------------------------------------------------------------------


@dataclass
class World:
    """Provisioned tenant, company mapping and keys for one test."""

    tenant_id: str
    company_row_id: str
    engine_id: int
    read_secret: str
    count_secret: str
    manage_secret: str
    company_secret: str
    other_read_secret: str


@pytest.fixture(scope="module")
def engine() -> Generator[Engine, None, None]:
    eng = create_engine(TEST_DB_URL, echo=False)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def settings() -> AppSettings:
    return AppSettings()


@pytest.fixture()
def world(engine: Engine, settings: AppSettings, test_company: CompanyRef) -> Generator[World, None, None]:
    """Provision a tenant mapped to the live Adapter Test company."""
    session = sessionmaker(bind=engine)()
    service = ApiKeyService(ApiKeyCrypto(settings.auth.api_key_pepper))
    tenant_id = ""
    other_tenant_id = ""
    connection_id = ""
    try:
        tenant = TenantModel(id=str(uuid.uuid4()), slug=f"rest-516-{uuid.uuid4().hex[:8]}", name="Cashbox Tenant")
        other_tenant = TenantModel(id=str(uuid.uuid4()), slug=f"rest-516-{uuid.uuid4().hex[:8]}", name="Other Tenant")
        session.add_all([tenant, other_tenant])
        session.flush()
        client = ApiClientModel(id=str(uuid.uuid4()), tenant_id=tenant.id, name="rest-516 client")
        other_client = ApiClientModel(id=str(uuid.uuid4()), tenant_id=other_tenant.id, name="rest-516 other client")
        session.add_all([client, other_client])
        session.flush()
        connection = EngineConnectionModel(
            id=str(uuid.uuid4()),
            name=f"conn-{uuid.uuid4().hex[:8]}",
            engine="tryton",
            base_url=AppSettings().tryton.base_url,
            database_name=f"hesabyar_tryton_test_{uuid.uuid4().hex[:8]}",
        )
        session.add(connection)
        session.flush()
        connection_id = connection.id
        row = EngineCompanyModel(
            id=str(uuid.uuid4()),
            tenant_id=tenant.id,
            engine_connection_id=connection.id,
            external_company_id=str(test_company.engine_id),
            company_name=TEST_COMPANY_NAME,
        )
        session.add(row)
        session.flush()

        def _key(client_row: ApiClientModel, scopes: list[str]):
            return service.issue_key(
                session,
                client=client_row,
                name="rest-516 key",
                scopes=scopes,
                installation_id=INSTALLATION_ID,
            )

        key_read = _key(client, ["cashbox:read"])
        key_count = _key(client, ["cashbox:read", "cashbox:count"])
        key_manage = _key(
            client,
            [
                "cashbox:read",
                "cashbox:count",
                "cashbox:manage",
                "review:read",
                "review:resolve",
                "review:file",
                "ledger:post",
            ],
        )
        key_company = _key(client, ["company:read"])
        key_other_read = _key(other_client, ["cashbox:read"])
        session.commit()
        tenant_id = tenant.id
        other_tenant_id = other_tenant.id
        yield World(
            tenant_id=tenant.id,
            company_row_id=row.id,
            engine_id=test_company.engine_id,
            read_secret=key_read.secret,
            count_secret=key_count.secret,
            manage_secret=key_manage.secret,
            company_secret=key_company.secret,
            other_read_secret=key_other_read.secret,
        )
    finally:
        session.close()
        cleanup = sessionmaker(bind=engine)()
        try:
            cleanup.execute(
                delete(IdempotencyRecordModel).where(IdempotencyRecordModel.installation_id == INSTALLATION_ID)
            )
            for tid in (tenant_id, other_tenant_id):
                if tid:
                    cleanup.query(TenantModel).filter(TenantModel.id == tid).delete(synchronize_session=False)
            if connection_id:
                cleanup.query(EngineConnectionModel).filter(EngineConnectionModel.id == connection_id).delete(
                    synchronize_session=False
                )
            cleanup.commit()
        finally:
            cleanup.close()


@pytest.fixture()
def api(settings: AppSettings) -> TestClient:
    """The real app with real wiring (no overrides at all)."""
    return TestClient(create_app(settings=settings), raise_server_exceptions=False)


def _auth(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


def _register_box(
    api: TestClient,
    world: World,
    journal_id: int,
    *,
    name: str = "صندوق آزمون",
    kind: str = "store",
    ceiling_irr: int | None = None,
    secret: str | None = None,
) -> dict:
    body: dict = {
        "company_id": world.company_row_id,
        "name_fa": name,
        "kind": kind,
        "statement_journal_id": journal_id,
    }
    if ceiling_irr is not None:
        body["ceiling_irr"] = ceiling_irr
    response = api.post("/api/v1/cash-boxes", json=body, headers=_auth(secret or world.manage_secret))
    assert response.status_code == 201, response.text
    return response.json()


def _start_session(api: TestClient, world: World, box_id: str) -> dict:
    response = api.post(
        f"/api/v1/cash-boxes/{box_id}/count-sessions",
        json={"company_id": world.company_row_id, "cashier": CASHIER, "supervisor": SUPERVISOR},
        headers=_auth(world.manage_secret),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _enter_count(api: TestClient, world: World, session_id: str, amount_irr: int) -> dict:
    response = api.post(
        f"/api/v1/count-sessions/{session_id}/count",
        json={"counted_amount_irr": amount_irr, "actor": CASHIER},
        headers=_auth(world.manage_secret),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _sign(api: TestClient, world: World, session_id: str, role: str, actor: str, minutes: str | None = None) -> dict:
    body: dict = {"role": role, "actor": actor}
    if minutes is not None:
        body["minutes_text"] = minutes
    response = api.post(
        f"/api/v1/count-sessions/{session_id}/sign",
        json=body,
        headers=_auth(world.manage_secret),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _sign_all_minutes(api: TestClient, world: World, session_id: str) -> dict:
    """Cashier + supervisor + manager sign the minutes (variance flow)."""
    _sign(api, world, session_id, "cashier", CASHIER, MINUTES_TEXT)
    _sign(api, world, session_id, "supervisor", SUPERVISOR)
    return _sign(api, world, session_id, "manager", MANAGER)


# ---------------------------------------------------------------------------
# Auth and scopes
# ---------------------------------------------------------------------------


def test_list_boxes_requires_auth(api: TestClient, world: World) -> None:
    response = api.get(f"/api/v1/cash-boxes?company={world.company_row_id}")
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"


def test_list_boxes_requires_cashbox_read_scope(api: TestClient, world: World) -> None:
    response = api.get(f"/api/v1/cash-boxes?company={world.company_row_id}", headers=_auth(world.company_secret))
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


def test_create_box_requires_manage_scope(api: TestClient, world: World, statement_journals: StatementJournals) -> None:
    response = api.post(
        "/api/v1/cash-boxes",
        json={
            "company_id": world.company_row_id,
            "name_fa": "صندوق آزمون",
            "kind": "store",
            "statement_journal_id": statement_journals.store_id,
        },
        headers=_auth(world.count_secret),
    )
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


def test_post_variance_requires_ledger_post_scope(
    api: TestClient, world: World, statement_journals: StatementJournals
) -> None:
    # A manage key WITHOUT ledger:post cannot post (AND semantics).
    box = _register_box(api, world, statement_journals.store_id)
    session = _start_session(api, world, box["id"])
    response = api.post(
        f"/api/v1/count-sessions/{session['id']}/post-variance-journal",
        json={"actor": MANAGER},
        headers=_auth(world.count_secret),
    )
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_get_settings_returns_defaults(api: TestClient, world: World) -> None:
    response = api.get("/api/v1/cash-boxes/settings", headers=_auth(world.read_secret))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["variance_policy"] == "expense"
    assert body["material_variance_threshold_irr"] is None
    assert body["count_reminder_days"] == 7
    assert body["count_schedule"] == "daily"
    assert body["supervisor_required"] is True
    assert body["shortage_settlement_hours"] == 24


def test_update_settings_round_trip(api: TestClient, world: World) -> None:
    response = api.patch(
        "/api/v1/cash-boxes/settings",
        json={
            "variance_policy": "receivable_from_cashier",
            "material_variance_threshold_irr": 5_000_000,
            "count_reminder_days": 3,
            "count_schedule": "weekly",
            "supervisor_required": False,
            "shortage_settlement_hours": 48,
        },
        headers=_auth(world.manage_secret),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["variance_policy"] == "receivable_from_cashier"
    assert body["material_variance_threshold_irr"] == 5_000_000
    assert body["count_reminder_days"] == 3
    assert body["count_schedule"] == "weekly"
    assert body["supervisor_required"] is False
    assert body["shortage_settlement_hours"] == 48
    again = api.get("/api/v1/cash-boxes/settings", headers=_auth(world.read_secret))
    assert again.json()["variance_policy"] == "receivable_from_cashier"


def test_update_settings_rejects_invalid(api: TestClient, world: World) -> None:
    # An unknown policy value is a bad enum, like everywhere else in
    # the API (review-queue item_type included): 400 INVALID_FILTER.
    response = api.patch(
        "/api/v1/cash-boxes/settings",
        json={"variance_policy": "wrong"},
        headers=_auth(world.manage_secret),
    )
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "INVALID_FILTER"
    # Invalid thresholds are a settings validation failure: 422.
    for bad in (
        {"variance_policy": "expense", "material_variance_threshold_irr": -1},
        {"variance_policy": "expense", "count_reminder_days": 0},
        {"variance_policy": "expense", "count_schedule": "monthly"},
        {"variance_policy": "expense", "shortage_settlement_hours": 0},
    ):
        response = api.patch("/api/v1/cash-boxes/settings", json=bad, headers=_auth(world.manage_secret))
        assert response.status_code == 422, (bad, response.text)
        assert response.json()["code"] == "CASHBOX_SETTINGS_INVALID"


# ---------------------------------------------------------------------------
# Statement journals
# ---------------------------------------------------------------------------


def test_list_statement_journals(api: TestClient, world: World, statement_journals: StatementJournals) -> None:
    response = api.get(
        f"/api/v1/cash-boxes/statement-journals?company={world.company_row_id}",
        headers=_auth(world.read_secret),
    )
    assert response.status_code == 200, response.text
    by_name = {j["name"]: j for j in response.json()}
    assert STORE_JOURNAL_NAME in by_name
    assert PETTY_JOURNAL_NAME in by_name
    store = by_name[STORE_JOURNAL_NAME]
    assert store["id"] == statement_journals.store_id
    assert store["account_code"] == STORE_ACCOUNT_CODE
    assert store["currency_code"] == "IRR"
    assert store["validation"] == "balance"
    assert store["active"] is True
    petty = by_name[PETTY_JOURNAL_NAME]
    assert petty["account_code"] == PETTY_ACCOUNT_CODE


# ---------------------------------------------------------------------------
# Cash boxes
# ---------------------------------------------------------------------------


def test_register_store_box(
    api: TestClient,
    world: World,
    adapter: TrytonAccountingAdapter,
    test_company: CompanyRef,
    statement_journals: StatementJournals,
) -> None:
    box = _register_box(api, world, statement_journals.store_id)
    assert box["company_id"] == world.company_row_id
    assert box["name_fa"] == "صندوق آزمون"
    assert box["kind"] == "store"
    assert box["statement_journal_id"] == statement_journals.store_id
    assert box["cash_account_id"] == statement_journals.store_account_id
    assert box["cash_account_code"] == STORE_ACCOUNT_CODE
    assert box["ceiling_irr"] is None
    assert box["active"] is True
    # Live balance from the engine — never hardcoded.
    assert box["book_balance_irr"] == _live_balance(adapter, test_company, statement_journals.store_account_id)
    assert box["days_since_last_count"] is None
    assert box["uncounted_alert"] is False

    # Tenant isolation: the other tenant cannot even see this tenant's
    # company (its id 404s), let alone its boxes.
    other = api.get(
        f"/api/v1/cash-boxes?company={world.company_row_id}",
        headers=_auth(world.other_read_secret),
    )
    assert other.status_code == 404, other.text
    assert other.json()["code"] == "COMPANY_NOT_FOUND"

    # And the other tenant cannot read this box by id.
    missing = api.get(f"/api/v1/cash-boxes/{box['id']}", headers=_auth(world.other_read_secret))
    assert missing.status_code == 404
    assert missing.json()["code"] == "CASHBOX_NOT_FOUND"


def test_register_duplicate_journal_conflicts(
    api: TestClient, world: World, statement_journals: StatementJournals
) -> None:
    _register_box(api, world, statement_journals.store_id)
    response = api.post(
        "/api/v1/cash-boxes",
        json={
            "company_id": world.company_row_id,
            "name_fa": "صندوق دوم",
            "kind": "other",
            "statement_journal_id": statement_journals.store_id,
        },
        headers=_auth(world.manage_secret),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "CASHBOX_JOURNAL_CONFLICT"


def test_register_unknown_journal_is_invalid(api: TestClient, world: World) -> None:
    response = api.post(
        "/api/v1/cash-boxes",
        json={
            "company_id": world.company_row_id,
            "name_fa": "صندوق آزمون",
            "kind": "store",
            "statement_journal_id": MISSING_ID,
        },
        headers=_auth(world.manage_secret),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "CASHBOX_INVALID"


def test_register_petty_box_ceiling_rules(api: TestClient, world: World, statement_journals: StatementJournals) -> None:
    # Petty cash without a ceiling is refused.
    response = api.post(
        "/api/v1/cash-boxes",
        json={
            "company_id": world.company_row_id,
            "name_fa": "تنخواه آزمون",
            "kind": "petty_cash",
            "statement_journal_id": statement_journals.petty_id,
        },
        headers=_auth(world.manage_secret),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "CASHBOX_INVALID"

    # A ceiling on a store box is refused too.
    response = api.post(
        "/api/v1/cash-boxes",
        json={
            "company_id": world.company_row_id,
            "name_fa": "صندوق آزمون",
            "kind": "store",
            "statement_journal_id": statement_journals.store_id,
            "ceiling_irr": 1_000_000,
        },
        headers=_auth(world.manage_secret),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "CASHBOX_INVALID"

    # Petty cash WITH a ceiling registers.
    box = _register_box(
        api,
        world,
        statement_journals.petty_id,
        name="تنخواه آزمون",
        kind="petty_cash",
        ceiling_irr=2_000_000,
    )
    assert box["ceiling_irr"] == 2_000_000
    assert box["cash_account_code"] == PETTY_ACCOUNT_CODE


def test_update_and_get_box(api: TestClient, world: World, statement_journals: StatementJournals) -> None:
    box = _register_box(api, world, statement_journals.store_id)
    response = api.patch(
        f"/api/v1/cash-boxes/{box['id']}",
        json={
            "name_fa": "صندوق بازنامی‌شده",
            "kind": "store",
            "ceiling_irr": None,
            "active": False,
        },
        headers=_auth(world.manage_secret),
    )
    assert response.status_code == 200, response.text
    assert response.json()["name_fa"] == "صندوق بازنامی‌شده"
    assert response.json()["active"] is False

    # An inactive box cannot start a session.
    start = api.post(
        f"/api/v1/cash-boxes/{box['id']}/count-sessions",
        json={"company_id": world.company_row_id, "cashier": CASHIER, "supervisor": SUPERVISOR},
        headers=_auth(world.manage_secret),
    )
    assert start.status_code == 409
    assert start.json()["code"] == "COUNT_SESSION_INVALID_STATE"

    missing = api.get(f"/api/v1/cash-boxes/{uuid.uuid4()}", headers=_auth(world.read_secret))
    assert missing.status_code == 404
    assert missing.json()["code"] == "CASHBOX_NOT_FOUND"


# ---------------------------------------------------------------------------
# Counting sessions
# ---------------------------------------------------------------------------


def test_start_session_snapshots_book_balance(
    api: TestClient,
    world: World,
    adapter: TrytonAccountingAdapter,
    test_company: CompanyRef,
    statement_journals: StatementJournals,
) -> None:
    box = _register_box(api, world, statement_journals.store_id)
    session = _start_session(api, world, box["id"])
    assert session["status"] == "in_progress"
    assert session["cashier"] == CASHIER
    assert session["supervisor"] == SUPERVISOR
    assert session["counted_amount_irr"] is None
    assert session["variance_irr"] is None
    assert session["book_balance_irr"] == _live_balance(adapter, test_company, statement_journals.store_account_id)

    # A second open session on the same box is refused.
    again = api.post(
        f"/api/v1/cash-boxes/{box['id']}/count-sessions",
        json={"company_id": world.company_row_id, "cashier": CASHIER, "supervisor": SUPERVISOR},
        headers=_auth(world.manage_secret),
    )
    assert again.status_code == 409
    assert again.json()["code"] == "COUNT_SESSION_ALREADY_OPEN"


def test_start_session_requires_different_supervisor(
    api: TestClient, world: World, statement_journals: StatementJournals
) -> None:
    box = _register_box(api, world, statement_journals.store_id)
    response = api.post(
        f"/api/v1/cash-boxes/{box['id']}/count-sessions",
        json={"company_id": world.company_row_id, "cashier": CASHIER, "supervisor": CASHIER},
        headers=_auth(world.manage_secret),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "COUNT_SUPERVISOR_MUST_DIFFER"


def test_balanced_count_archives_with_supervisor_attendance(
    api: TestClient, world: World, statement_journals: StatementJournals
) -> None:
    box = _register_box(api, world, statement_journals.store_id)
    session = _start_session(api, world, box["id"])
    counted = _enter_count(api, world, session["id"], session["book_balance_irr"])
    assert counted["status"] == "awaiting_supervisor"
    assert counted["variance_irr"] == 0
    assert counted["minutes_required"] is False
    # A balanced count has no minutes to sign.
    refused = api.post(
        f"/api/v1/count-sessions/{session['id']}/sign",
        json={"role": "cashier", "actor": CASHIER, "minutes_text": MINUTES_TEXT},
        headers=_auth(world.manage_secret),
    )
    assert refused.status_code == 409
    assert refused.json()["code"] == "COUNT_SESSION_INVALID_STATE"
    # Supervisor attendance alone archives the session.
    attended = _sign(api, world, session["id"], "supervisor", SUPERVISOR)
    assert attended["status"] == "archived"
    assert attended["witnessed_by"] == SUPERVISOR
    assert attended["review_item_id"] is None
    assert attended["draft_journal_id"] is None


def test_enter_count_denominations(api: TestClient, world: World, statement_journals: StatementJournals) -> None:
    box = _register_box(api, world, statement_journals.store_id)
    session = _start_session(api, world, box["id"])
    book = session["book_balance_irr"]
    # A breakdown that does not sum to the counted amount is refused.
    response = api.post(
        f"/api/v1/count-sessions/{session['id']}/count",
        json={
            "counted_amount_irr": book,
            "denominations": [{"denomination_irr": 1_000_000, "count": 1}],
            "actor": CASHIER,
        },
        headers=_auth(world.manage_secret),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "COUNT_DENOMINATIONS_MISMATCH"
    # A matching breakdown is accepted (one line for the whole amount).
    response = api.post(
        f"/api/v1/count-sessions/{session['id']}/count",
        json={
            "counted_amount_irr": book,
            "denominations": [{"denomination_irr": book, "count": 1}] if book > 0 else [],
            "actor": CASHIER,
        },
        headers=_auth(world.manage_secret),
    )
    assert response.status_code == 200, response.text
    assert response.json()["variance_irr"] == 0


def test_count_locked_after_signature(api: TestClient, world: World, statement_journals: StatementJournals) -> None:
    box = _register_box(api, world, statement_journals.store_id)
    session = _start_session(api, world, box["id"])
    _enter_count(api, world, session["id"], session["book_balance_irr"])
    _sign(api, world, session["id"], "supervisor", SUPERVISOR)
    response = api.post(
        f"/api/v1/count-sessions/{session['id']}/count",
        json={"counted_amount_irr": 0, "actor": CASHIER},
        headers=_auth(world.manage_secret),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "COUNT_SESSION_INVALID_STATE"


def test_sign_wrong_actor_is_refused(api: TestClient, world: World, statement_journals: StatementJournals) -> None:
    box = _register_box(api, world, statement_journals.store_id)
    session = _start_session(api, world, box["id"])
    _enter_count(api, world, session["id"], session["book_balance_irr"] - 50_000)
    response = api.post(
        f"/api/v1/count-sessions/{session['id']}/sign",
        json={"role": "cashier", "actor": "شخص دیگر", "minutes_text": MINUTES_TEXT},
        headers=_auth(world.manage_secret),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "COUNT_WRONG_ACTOR"
    # The manager must differ from the cashier and the supervisor.
    response = api.post(
        f"/api/v1/count-sessions/{session['id']}/sign",
        json={"role": "manager", "actor": CASHIER},
        headers=_auth(world.manage_secret),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "COUNT_WRONG_ACTOR"


# ---------------------------------------------------------------------------
# The variance flow: minutes -> DRAFT -> review approval -> post
# ---------------------------------------------------------------------------


def test_variance_flow_to_posted_journal(
    api: TestClient,
    world: World,
    tryton_client: TrytonRestClient,
    created_drafts: list[int],
    statement_journals: StatementJournals,
) -> None:
    """A 50,000 IRR shortage: minutes, DRAFT, review approval, post.

    The posted move accumulates in the shared test company by design
    (like the US-202 e2e tests); the amount is small and the balance
    assertions elsewhere read the live balance, never a hardcoded one.
    """
    box = _register_box(api, world, statement_journals.store_id)
    session = _start_session(api, world, box["id"])
    book = session["book_balance_irr"]
    counted = _enter_count(api, world, session["id"], book - 50_000)
    assert counted["variance_irr"] == -50_000
    assert counted["minutes_required"] is True
    # The cashier's signature must carry the minutes text.
    refused = api.post(
        f"/api/v1/count-sessions/{session['id']}/sign",
        json={"role": "cashier", "actor": CASHIER},
        headers=_auth(world.manage_secret),
    )
    assert refused.status_code == 422
    assert refused.json()["code"] == "COUNT_MINUTES_REQUIRED"

    completed = _sign_all_minutes(api, world, session["id"])
    assert completed["status"] == "awaiting_manager"
    assert completed["minutes_signed"] is True
    assert completed["draft_journal_id"] is not None
    assert completed["review_item_id"] is not None
    created_drafts.append(completed["draft_journal_id"])

    # The DRAFT exists in Tryton and is still a draft.
    draft = tryton_client.read("account.move", completed["draft_journal_id"], fields=["state", "description"])
    assert draft is not None and draft["state"] == "draft"

    # The review item is filed as a cashbox_variance item, open.
    item = api.get(
        f"/api/v1/review-queue/items/{completed['review_item_id']}",
        headers=_auth(world.manage_secret),
    )
    assert item.status_code == 200, item.text
    assert item.json()["item_type"] == "cashbox_variance"
    assert item.json()["status"] == "open"
    assert item.json()["amount_irr"] == 50_000

    # Posting BEFORE the manager approves is refused — a cash
    # shortage is never auto-posted.
    refused = api.post(
        f"/api/v1/count-sessions/{session['id']}/post-variance-journal",
        json={"actor": MANAGER},
        headers=_auth(world.manage_secret),
    )
    assert refused.status_code == 409
    assert refused.json()["code"] == "COUNT_REVIEW_NOT_APPROVED"

    # The manager approves the variance in the review queue.
    action = api.post(
        f"/api/v1/review-queue/items/{completed['review_item_id']}/actions",
        json={"action": "approve_variance_entry", "note": "صورتجلسه بررسی و تأیید شد."},
        headers=_auth(world.manage_secret),
    )
    assert action.status_code == 200, action.text
    assert action.json()["status"] == "resolved"
    assert action.json()["resolution"] == "adjusted"

    # Now the journal posts and the session archives.
    posted = api.post(
        f"/api/v1/count-sessions/{session['id']}/post-variance-journal",
        json={"actor": MANAGER},
        headers=_auth(world.manage_secret),
    )
    assert posted.status_code == 200, posted.text
    assert posted.json()["status"] == "archived"
    assert posted.json()["posted_journal_id"] == completed["draft_journal_id"]
    move = tryton_client.read("account.move", completed["draft_journal_id"], fields=["state"])
    assert move is not None and move["state"] == "posted"


def test_recount_flow(
    api: TestClient,
    world: World,
    created_drafts: list[int],
    statement_journals: StatementJournals,
) -> None:
    """Manager requests a recount; the cashier recounts balanced.

    The superseded DRAFT is deleted, the same review item's figures
    are refreshed to zero, and the session archives on the recount.
    """
    box = _register_box(api, world, statement_journals.store_id)
    session = _start_session(api, world, box["id"])
    book = session["book_balance_irr"]
    _enter_count(api, world, session["id"], book - 50_000)
    completed = _sign_all_minutes(api, world, session["id"])
    assert completed["status"] == "awaiting_manager"
    first_draft = completed["draft_journal_id"]
    assert first_draft is not None
    created_drafts.append(first_draft)
    review_item_id = completed["review_item_id"]
    assert review_item_id is not None

    # A recount without the manager's request is refused.
    refused = api.post(
        f"/api/v1/count-sessions/{session['id']}/recount",
        json={"actor": CASHIER},
        headers=_auth(world.manage_secret),
    )
    assert refused.status_code == 409
    assert refused.json()["code"] == "COUNT_RECOUNT_NOT_REQUESTED"

    # The manager asks for a recount from the review queue.
    action = api.post(
        f"/api/v1/review-queue/items/{review_item_id}/actions",
        json={"action": "recount", "note": "لطفاً دوباره بشمارید."},
        headers=_auth(world.manage_secret),
    )
    assert action.status_code == 200, action.text
    assert action.json()["status"] == "in_review"

    reopened = api.post(
        f"/api/v1/count-sessions/{session['id']}/recount",
        json={"actor": CASHIER},
        headers=_auth(world.manage_secret),
    )
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["status"] == "in_progress"
    assert reopened.json()["counted_amount_irr"] is None
    assert reopened.json()["draft_journal_id"] is None
    assert reopened.json()["review_item_id"] == review_item_id

    # This time the count matches the books.
    _enter_count(api, world, session["id"], book)
    archived = _sign(api, world, session["id"], "supervisor", SUPERVISOR)
    assert archived["status"] == "archived"
    assert archived["variance_irr"] == 0

    # The SAME review item now carries the recount's figures.
    item = api.get(
        f"/api/v1/review-queue/items/{review_item_id}",
        headers=_auth(world.manage_secret),
    )
    assert item.status_code == 200, item.text
    assert item.json()["amount_irr"] == 0
    assert item.json()["severity"] == "info"
    assert item.json()["status"] == "open"


# ---------------------------------------------------------------------------
# Sessions list and report
# ---------------------------------------------------------------------------


def test_list_sessions_filters(api: TestClient, world: World, statement_journals: StatementJournals) -> None:
    box = _register_box(api, world, statement_journals.store_id)
    session = _start_session(api, world, box["id"])
    response = api.get(
        f"/api/v1/count-sessions?company={world.company_row_id}&cash_box_id={box['id']}",
        headers=_auth(world.read_secret),
    )
    assert response.status_code == 200, response.text
    ids = [s["id"] for s in response.json()["items"]]
    assert session["id"] in ids
    by_status = api.get(
        f"/api/v1/count-sessions?company={world.company_row_id}&status=archived",
        headers=_auth(world.read_secret),
    )
    assert by_status.status_code == 200, by_status.text
    assert all(s["status"] == "archived" for s in by_status.json()["items"])
    assert session["id"] not in [s["id"] for s in by_status.json()["items"]]


def test_cashbox_report(
    api: TestClient,
    world: World,
    created_drafts: list[int],
    statement_journals: StatementJournals,
) -> None:
    box = _register_box(api, world, statement_journals.store_id)
    session = _start_session(api, world, box["id"])
    book = session["book_balance_irr"]
    _enter_count(api, world, session["id"], book - 50_000)
    completed = _sign_all_minutes(api, world, session["id"])
    assert completed["draft_journal_id"] is not None
    created_drafts.append(completed["draft_journal_id"])

    today = datetime.date.today()
    response = api.get(
        f"/api/v1/cash-boxes/report?company={world.company_row_id}"
        f"&date_from={today.isoformat()}&date_to={today.isoformat()}",
        headers=_auth(world.read_secret),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["company_id"] == world.company_row_id
    assert body["total_shortage_irr"] == 50_000
    assert body["total_overage_irr"] == 0
    assert body["total_possible_unrecorded_sales_irr"] == 0
    lines = {line["cash_box_id"]: line for line in body["lines"]}
    assert box["id"] in lines
    line = lines[box["id"]]
    assert line["cash_account_code"] == STORE_ACCOUNT_CODE
    assert line["session_count"] == 1
    assert line["shortage_irr"] == 50_000
    assert line["net_variance_irr"] == -50_000

    # A reversed period is refused.
    reversed_dates = api.get(
        f"/api/v1/cash-boxes/report?company={world.company_row_id}"
        f"&date_from={today.isoformat()}&date_to={(today - datetime.timedelta(days=1)).isoformat()}",
        headers=_auth(world.read_secret),
    )
    assert reversed_dates.status_code == 422
    assert reversed_dates.json()["code"] == "CASHBOX_INVALID"
