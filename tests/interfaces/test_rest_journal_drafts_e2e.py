"""End-to-end tests for the journal endpoints (US-202).

The Phase 2 exit proof for journal workflows: a scoped client drafts,
validates, posts and reverses a journal through HesabYar — the real
FastAPI app, a real issued API key against the real database, and the
LIVE Tryton 8.2.3 server, using ONLY the dedicated test company
"HesabYar Adapter Test" (provisioned by
``~/workspace/projects/hesabyar-reference/setup_adapter_test_company.py``;
see docs/adapter.md). The company is looked up BY NAME — numeric engine
ids are never hardcoded.

Posted test moves accumulate in the test company by design (they
cannot be deleted); drafts the tests leave behind are deleted in
teardown.

When trytond is not reachable (or the test company has not been
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

from hesabyar.domain.accounting.refs import (
    AccountRef,
    CompanyRef,
    JournalDraftRef,
    JournalRef,
    PostedJournalRef,
)
from hesabyar.infrastructure.accounting.tryton.adapter import TrytonAccountingAdapter
from hesabyar.infrastructure.accounting.tryton.client import TrytonRestClient
from hesabyar.infrastructure.auth import ApiKeyCrypto, ApiKeyService
from hesabyar.infrastructure.config import AppSettings
from hesabyar.infrastructure.persistence.postgres.models import (
    ApiClientModel,
    AuditEventModel,
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

TRYTON_BASE_URL = os.environ.get("TRYTON_BASE_URL", "http://localhost:8000")
TRYTON_DATABASE = os.environ.get("TRYTON_DATABASE", "hesabyar_tryton_test")
TRYTON_API_KEY = os.environ.get("TRYTON_API_KEY", "hesabyar-dev-rest-key")
TEST_COMPANY_NAME = os.environ.get("TRYTON_TEST_COMPANY", "HesabYar Adapter Test")
TEST_JOURNAL_NAME = "HesabYar Adapter Test General Journal"

INSTALLATION_ID = "00000000-0000-0000-0000-000000000006"

# Two postable leaf accounts of the universal chart (cash + investments),
# per the US-201 contract tests.
DEBIT_ACCOUNT_CODE = "1.1.1"
CREDIT_ACCOUNT_CODE = "1.1.2"

# A date inside FY 2026's open November period.
MOVE_DATE = datetime.date(2026, 11, 17)

MISSING_ID = 999_999_999

ALL_LEDGER_SCOPES = [
    "ledger:read",
    "ledger:draft:create",
    "ledger:draft:update",
    "ledger:post",
    "ledger:reverse",
]


# ---------------------------------------------------------------------------
# Live Tryton fixtures (skip cleanly when the server is down)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def tryton_client() -> Generator[TrytonRestClient, None, None]:
    rest_client = TrytonRestClient(TRYTON_BASE_URL, TRYTON_DATABASE, TRYTON_API_KEY)
    if not rest_client.ping():
        rest_client.close()
        pytest.skip(
            f"trytond server not reachable at {TRYTON_BASE_URL} "
            "(start it per docs/dev-setup.md §3 to run the e2e tests)"
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
        f"Test company {TEST_COMPANY_NAME!r} not found in Tryton DB {TRYTON_DATABASE!r}. "
        "Provision it with setup_adapter_test_company.py (see docs/adapter.md)."
    )


@pytest.fixture(scope="module")
def journal(tryton_client: TrytonRestClient) -> JournalRef:
    """The test company's general journal, looked up BY NAME.

    Journals are global in Tryton (no company field), and the engine
    now holds several general journals, so the adapter requires an
    explicit journal instead of auto-resolving one.
    """
    records = tryton_client.search(
        "account.journal",
        domain=[("name", "=", TEST_JOURNAL_NAME), ("type", "=", "general")],
        fields=["name"],
    )
    if not records:
        pytest.skip(
            f"Test journal {TEST_JOURNAL_NAME!r} not found. "
            "Provision it with setup_adapter_test_company.py (see docs/adapter.md)."
        )
    return JournalRef(int(records[0]["id"]))


@pytest.fixture(scope="module")
def accounts(
    adapter: TrytonAccountingAdapter, test_company: CompanyRef
) -> tuple[AccountRef, AccountRef]:
    chart = {view.code: view for view in adapter.get_chart_of_accounts(test_company)}
    missing = [
        code for code in (DEBIT_ACCOUNT_CODE, CREDIT_ACCOUNT_CODE) if code not in chart
    ]
    if missing:
        pytest.skip(f"Chart accounts missing from test company: {missing}.")
    return chart[DEBIT_ACCOUNT_CODE].ref, chart[CREDIT_ACCOUNT_CODE].ref


@pytest.fixture()
def created_moves(tryton_client: TrytonRestClient) -> Generator[list[int], None, None]:
    """Track moves created by a test and delete them afterwards (drafts only).

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


# ---------------------------------------------------------------------------
# Provisioned world (real DB, real keys, real app)
# ---------------------------------------------------------------------------


@dataclass
class World:
    """Provisioned tenant, company mapping and keys for one test."""

    tenant_id: str
    company_row_id: str
    engine_id: int
    key_full_secret: str  # all ledger scopes, unrestricted
    key_read_secret: str  # ledger:read only


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
def world(
    engine: Engine, settings: AppSettings, test_company: CompanyRef
) -> Generator[World, None, None]:
    """Provision a tenant mapped to the live Adapter Test company."""
    session = sessionmaker(bind=engine)()
    service = ApiKeyService(ApiKeyCrypto(settings.auth.api_key_pepper))
    tenant_id = ""
    connection_id = ""
    try:
        tenant = TenantModel(
            id=str(uuid.uuid4()), slug=f"rest-202-e2e-{uuid.uuid4().hex[:8]}", name="E2E Tenant"
        )
        session.add(tenant)
        session.flush()
        client = ApiClientModel(
            id=str(uuid.uuid4()), tenant_id=tenant.id, name="rest-202 e2e client"
        )
        session.add(client)
        session.flush()
        connection = EngineConnectionModel(
            id=str(uuid.uuid4()),
            name=f"conn-{uuid.uuid4().hex[:8]}",
            engine="tryton",
            base_url=TRYTON_BASE_URL,
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

        def _key(scopes: list[str]):
            return service.issue_key(
                session,
                client=client,
                name="rest-202 e2e key",
                scopes=scopes,
                installation_id=INSTALLATION_ID,
            )

        key_full = _key(ALL_LEDGER_SCOPES)
        key_read = _key(["ledger:read"])
        session.commit()
        tenant_id = tenant.id
        yield World(
            tenant_id=tenant.id,
            company_row_id=row.id,
            engine_id=test_company.engine_id,
            key_full_secret=key_full.secret,
            key_read_secret=key_read.secret,
        )
    finally:
        session.close()
        cleanup = sessionmaker(bind=engine)()
        try:
            cleanup.execute(
                delete(IdempotencyRecordModel).where(
                    IdempotencyRecordModel.installation_id == INSTALLATION_ID
                )
            )
            if tenant_id:
                cleanup.query(TenantModel).filter(
                    TenantModel.id == tenant_id
                ).delete(synchronize_session=False)
            if connection_id:
                cleanup.query(EngineConnectionModel).filter(
                    EngineConnectionModel.id == connection_id
                ).delete(synchronize_session=False)
            cleanup.commit()
        finally:
            cleanup.close()


@pytest.fixture()
def api(settings: AppSettings) -> TestClient:
    """The real app with real wiring (no overrides at all)."""
    return TestClient(create_app(settings=settings), raise_server_exceptions=False)


def _auth(secret: str, key: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {secret}"}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _draft_body(
    world: World,
    accounts: tuple[AccountRef, AccountRef],
    journal: JournalRef,
    amount: int = 1_000_000,
) -> dict:
    debit, credit = accounts
    return {
        "company_id": world.company_row_id,
        "date": MOVE_DATE.isoformat(),
        "description": "US-202 e2e draft",
        "journal_id": journal.engine_id,
        "lines": [
            {"account_id": debit.engine_id, "debit": amount, "credit": 0},
            {"account_id": credit.engine_id, "debit": 0, "credit": amount},
        ],
    }


def _create_draft(
    api: TestClient,
    world: World,
    accounts: tuple[AccountRef, AccountRef],
    journal: JournalRef,
    created_moves: list[int],
    key: str | None = None,
) -> dict:
    response = api.post(
        "/api/v1/accounting/journal-drafts",
        json=_draft_body(world, accounts, journal),
        headers=_auth(world.key_full_secret, key or f"e2e-create-{uuid.uuid4().hex}"),
    )
    assert response.status_code == 201, response.text
    draft = response.json()
    created_moves.append(draft["id"])
    return draft


def _audit_events(engine: Engine, tenant_id: str) -> list[AuditEventModel]:
    session = sessionmaker(bind=engine)()
    try:
        return list(
            session.query(AuditEventModel)
            .filter(AuditEventModel.tenant_id == tenant_id)
            .order_by(AuditEventModel.occurred_at)
            .all()
        )
    finally:
        session.close()


# ---------------------------------------------------------------------------
# The Phase 2 exit proof: draft -> validate -> post -> reverse, live
# ---------------------------------------------------------------------------


def test_e2e_draft_validate_post_reverse(
    api: TestClient,
    world: World,
    accounts: tuple[AccountRef, AccountRef],
    journal: JournalRef,
    adapter: TrytonAccountingAdapter,
    created_moves: list[int],
    engine: Engine,
) -> None:
    """The full journal workflow runs through the API on live Tryton."""
    headers = _auth(world.key_full_secret)

    # Create the draft.
    draft = _create_draft(api, world, accounts, journal, created_moves, key="e2e-flow-create")
    assert draft["state"] == "draft"
    assert draft["company_id"] == world.company_row_id
    assert draft["total_debit"] == draft["total_credit"] == 1_000_000
    draft_id = draft["id"]

    # The draft is visible in Tryton.
    draft_view = adapter.get_journal_draft(JournalDraftRef(draft_id))
    assert draft_view is not None
    assert draft_view.state == "draft"
    assert draft_view.company.engine_id == world.engine_id

    # Validate it.
    validation = api.post(
        f"/api/v1/accounting/journal-drafts/{draft_id}/validate", headers=headers
    )
    assert validation.status_code == 200
    assert validation.json() == {"valid": True, "issues": []}

    # Post it.
    posted = api.post(
        f"/api/v1/accounting/journal-drafts/{draft_id}/post",
        headers=_auth(world.key_full_secret, "e2e-flow-post"),
    )
    assert posted.status_code == 200, posted.text
    posted_body = posted.json()
    assert posted_body["id"] == draft_id
    assert posted_body["number"], "the engine must assign a move number"
    # post_date is the engine's posting date (server date), not the move
    # date — check it is present and consistent with Tryton's read-back.
    assert posted_body["post_date"]

    # The posted move is visible in Tryton with the same number.
    posted_view = adapter.get_journal(PostedJournalRef(draft_id))
    assert posted_view is not None
    assert posted_view.number == posted_body["number"]
    assert posted_view.post_date is not None
    assert posted_view.post_date.gregorian_date.isoformat() == posted_body["post_date"]
    assert posted_view.is_balanced

    # Re-posting with a fresh key is a conflict, not a second posting.
    repost = api.post(
        f"/api/v1/accounting/journal-drafts/{draft_id}/post",
        headers=_auth(world.key_full_secret, "e2e-flow-repost"),
    )
    assert repost.status_code == 409
    assert repost.json()["code"] == "LEDGER_POSTED_IMMUTABLE"

    # Reverse it.
    reversal = api.post(
        f"/api/v1/accounting/journals/{draft_id}/reverse",
        headers=_auth(world.key_full_secret, "e2e-flow-reverse"),
    )
    assert reversal.status_code == 201, reversal.text
    reversal_body = reversal.json()
    assert reversal_body["origin_id"] == draft_id
    assert reversal_body["number"]
    reversal_id = reversal_body["id"]
    created_moves.append(reversal_id)

    # The reversal is visible in Tryton, linked to the original, with
    # swapped lines that cancel it (matched by account: Tryton does not
    # guarantee line order on read).
    reversal_view = adapter.get_journal(PostedJournalRef(reversal_id))
    assert reversal_view is not None
    assert reversal_view.origin == PostedJournalRef(draft_id)
    assert reversal_view.is_reversal
    original_amounts = {
        line.account.engine_id: (line.debit.amount, line.credit.amount)
        for line in posted_view.lines
    }
    reversal_amounts = {
        line.account.engine_id: (line.debit.amount, line.credit.amount)
        for line in reversal_view.lines
    }
    assert reversal_amounts == {
        account: (credit, debit)
        for account, (debit, credit) in original_amounts.items()
    }

    # Every mutation wrote an audit event — including the failed re-post.
    events = _audit_events(engine, world.tenant_id)
    assert [(event.action, event.outcome) for event in events] == [
        ("ledger.draft.create", "success"),
        ("ledger.post", "success"),
        ("ledger.post", "error"),
        ("ledger.reverse", "success"),
    ]
    assert events[2].error_code == "LEDGER_POSTED_IMMUTABLE"
    assert all(event.tryton_reference is not None for event in events)
    assert events[0].tryton_reference == f"account.move:{draft_id}"
    assert events[3].tryton_reference == f"account.move:{reversal_id}"
    assert events[3].metadata_after is not None
    assert events[3].metadata_after["original_journal_id"] == draft_id


def test_e2e_post_replay_does_not_post_twice(
    api: TestClient,
    world: World,
    accounts: tuple[AccountRef, AccountRef],
    journal: JournalRef,
    adapter: TrytonAccountingAdapter,
    created_moves: list[int],
) -> None:
    """Repeating a post with the same key replays; the move posts once."""
    draft = _create_draft(api, world, accounts, journal, created_moves)
    headers = _auth(world.key_full_secret, "e2e-replay-post")
    first = api.post(
        f"/api/v1/accounting/journal-drafts/{draft['id']}/post", headers=headers
    )
    assert first.status_code == 200
    second = api.post(
        f"/api/v1/accounting/journal-drafts/{draft['id']}/post", headers=headers
    )
    assert second.status_code == 200
    assert second.json() == first.json()
    # One posted move, with the number from the first (only) posting.
    view = adapter.get_journal(PostedJournalRef(draft["id"]))
    assert view is not None
    assert view.number == first.json()["number"]


def test_e2e_create_replay_returns_same_draft(
    api: TestClient,
    world: World,
    accounts: tuple[AccountRef, AccountRef],
    journal: JournalRef,
    created_moves: list[int],
) -> None:
    """Repeating a draft create with the same key replays the same draft."""
    headers = _auth(world.key_full_secret, "e2e-replay-create")
    first = api.post(
        "/api/v1/accounting/journal-drafts",
        json=_draft_body(world, accounts, journal),
        headers=headers,
    )
    assert first.status_code == 201
    created_moves.append(first.json()["id"])
    second = api.post(
        "/api/v1/accounting/journal-drafts",
        json=_draft_body(world, accounts, journal),
        headers=headers,
    )
    assert second.status_code == 201
    assert second.json() == first.json()


def test_e2e_same_key_different_payload_conflicts(
    api: TestClient,
    world: World,
    accounts: tuple[AccountRef, AccountRef],
    journal: JournalRef,
    created_moves: list[int],
) -> None:
    """A key reused with a different payload is a 409, never a replay."""
    headers = _auth(world.key_full_secret, "e2e-conflict")
    first = api.post(
        "/api/v1/accounting/journal-drafts",
        json=_draft_body(world, accounts, journal),
        headers=headers,
    )
    assert first.status_code == 201
    created_moves.append(first.json()["id"])
    changed = _draft_body(world, accounts, journal, amount=2_000_000)
    second = api.post(
        "/api/v1/accounting/journal-drafts", json=changed, headers=headers
    )
    assert second.status_code == 409
    assert second.json()["code"] == "IDEMPOTENCY_CONFLICT"


def test_e2e_unbalanced_create_rejected_before_tryton(
    api: TestClient,
    world: World,
    accounts: tuple[AccountRef, AccountRef],
    journal: JournalRef,
) -> None:
    """An unbalanced draft fails with a stable ledger error, no engine write."""
    body = _draft_body(world, accounts, journal)
    body["lines"][1]["credit"] = 999
    response = api.post(
        "/api/v1/accounting/journal-drafts",
        json=body,
        headers=_auth(world.key_full_secret, "e2e-unbalanced"),
    )
    assert response.status_code == 400
    assert response.json()["code"] == "LEDGER_UNBALANCED"


def test_e2e_reverse_draft_returns_400(
    api: TestClient,
    world: World,
    accounts: tuple[AccountRef, AccountRef],
    journal: JournalRef,
    created_moves: list[int],
) -> None:
    """Only a posted journal can be reversed."""
    draft = _create_draft(api, world, accounts, journal, created_moves)
    response = api.post(
        f"/api/v1/accounting/journals/{draft['id']}/reverse",
        headers=_auth(world.key_full_secret, "e2e-reverse-draft"),
    )
    assert response.status_code == 400
    assert response.json()["code"] == "LEDGER_INVALID_JOURNAL"


def test_e2e_unknown_draft_returns_404(api: TestClient, world: World) -> None:
    response = api.get(
        f"/api/v1/accounting/journal-drafts/{MISSING_ID}",
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "LEDGER_JOURNAL_NOT_FOUND"


def test_e2e_auth_scopes_enforced(
    api: TestClient,
    world: World,
    accounts: tuple[AccountRef, AccountRef],
    journal: JournalRef,
    created_moves: list[int],
) -> None:
    """401 without a key; 403 when the key lacks the posting scope."""
    no_key = api.get(
        f"/api/v1/accounting/journal-drafts?company={world.company_row_id}"
    )
    assert no_key.status_code == 401
    assert no_key.json()["code"] == "UNAUTHORIZED"

    draft = _create_draft(api, world, accounts, journal, created_moves)
    forbidden = api.post(
        f"/api/v1/accounting/journal-drafts/{draft['id']}/post",
        headers=_auth(world.key_read_secret, "e2e-no-scope"),
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "FORBIDDEN"
    assert "ledger:post" in forbidden.json()["detail"]


# ---------------------------------------------------------------------------
# US-202 follow-up: list + update, live
# ---------------------------------------------------------------------------


def test_e2e_list_drafts_returns_created_drafts(
    api: TestClient,
    world: World,
    accounts: tuple[AccountRef, AccountRef],
    journal: JournalRef,
    created_moves: list[int],
) -> None:
    first = _create_draft(api, world, accounts, journal, created_moves)
    second = _create_draft(api, world, accounts, journal, created_moves)
    listed = api.get(
        f"/api/v1/accounting/journal-drafts?company={world.company_row_id}",
        headers=_auth(world.key_read_secret),
    )
    assert listed.status_code == 200
    ids = [item["id"] for item in listed.json()["items"]]
    assert first["id"] in ids
    assert second["id"] in ids
    assert all(item["state"] == "draft" for item in listed.json()["items"])
    assert all(
        item["engine_company_id"] == world.engine_id
        for item in listed.json()["items"]
    )

    # Posting one draft removes it from the list.
    posted = api.post(
        f"/api/v1/accounting/journal-drafts/{first['id']}/post",
        headers=_auth(world.key_full_secret, f"e2e-list-post-{uuid.uuid4().hex}"),
    )
    assert posted.status_code == 200
    relisted = api.get(
        f"/api/v1/accounting/journal-drafts?company={world.company_row_id}",
        headers=_auth(world.key_read_secret),
    )
    assert relisted.status_code == 200
    ids = [item["id"] for item in relisted.json()["items"]]
    assert first["id"] not in ids
    assert second["id"] in ids


def test_e2e_patch_draft_updates_lines_live(
    api: TestClient,
    world: World,
    accounts: tuple[AccountRef, AccountRef],
    journal: JournalRef,
    adapter: TrytonAccountingAdapter,
    created_moves: list[int],
) -> None:
    draft = _create_draft(api, world, accounts, journal, created_moves)
    debit, credit = accounts
    response = api.patch(
        f"/api/v1/accounting/journal-drafts/{draft['id']}",
        json={
            "description": "US-202 e2e draft (patched)",
            "lines": [
                {"account_id": debit.engine_id, "debit": 3_000_000, "credit": 0},
                {"account_id": credit.engine_id, "debit": 0, "credit": 3_000_000},
            ],
        },
        headers=_auth(world.key_full_secret, f"e2e-patch-{uuid.uuid4().hex}"),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == draft["id"]
    assert body["description"] == "US-202 e2e draft (patched)"
    assert body["total_debit"] == body["total_credit"] == 3_000_000
    # The engine itself holds the rewritten lines (not just the response).
    view = adapter.get_journal_draft(JournalDraftRef(draft["id"]))
    assert view is not None
    assert view.description == "US-202 e2e draft (patched)"
    assert view.total_debit.amount == 3_000_000
    assert adapter.validate_journal_draft(JournalDraftRef(draft["id"])).valid is True


def test_e2e_patch_posted_draft_returns_409(
    api: TestClient,
    world: World,
    accounts: tuple[AccountRef, AccountRef],
    journal: JournalRef,
    created_moves: list[int],
) -> None:
    draft = _create_draft(api, world, accounts, journal, created_moves)
    posted = api.post(
        f"/api/v1/accounting/journal-drafts/{draft['id']}/post",
        headers=_auth(world.key_full_secret, f"e2e-patch409-post-{uuid.uuid4().hex}"),
    )
    assert posted.status_code == 200
    response = api.patch(
        f"/api/v1/accounting/journal-drafts/{draft['id']}",
        json={"description": "too late"},
        headers=_auth(world.key_full_secret, f"e2e-patch409-{uuid.uuid4().hex}"),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "LEDGER_POSTED_IMMUTABLE"
