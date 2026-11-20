"""Phase 2 end-to-end verification (US-205, AC 3).

One scripted run walks the whole accounting cycle through the REST API
with real issued keys against live Tryton 8.2.3, using ONLY the
dedicated test company "HesabYar Adapter Test" (provisioned by
``~/workspace/projects/hesabyar-reference/setup_adapter_test_company.py``;
see docs/adapter.md). The company is looked up BY NAME — numeric engine
ids are never hardcoded.

The run: journal draft → validate → post → reverse, then invoice draft
→ validate, then payments/statements reads, the reconcile stub, and an
audit-trail query proving every mutating step was recorded. Every
mutating step is idempotent (replays return the stored response with no
second engine write). Validation steps are read-only and write no audit
event — the same convention as US-202 (a validate is a read, not a
mutation; the journal e2e test asserts the same).

Posted test moves accumulate in the test company by design (they
cannot be deleted); drafts, invoices, payments and statements the tests
leave behind are deleted in teardown.

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
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from hesabyar.domain.accounting.refs import (
    AccountRef,
    CompanyRef,
    InvoiceRef,
    JournalRef,
    PartyAddressRef,
    PartyRef,
    PostedJournalRef,
)
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

TRYTON_BASE_URL = os.environ.get("TRYTON_BASE_URL", "http://localhost:8000")
TRYTON_DATABASE = os.environ.get("TRYTON_DATABASE", "hesabyar_tryton_test")
TRYTON_API_KEY = os.environ.get("TRYTON_API_KEY", "hesabyar-dev-rest-key")
TEST_COMPANY_NAME = os.environ.get("TRYTON_TEST_COMPANY", "HesabYar Adapter Test")
TEST_JOURNAL_NAME = "HesabYar Adapter Test General Journal"

INSTALLATION_ID = "00000000-0000-0000-0000-000000000009"

# Chart accounts of the universal chart (per the US-201/US-204 contract tests).
DEBIT_ACCOUNT_CODE = "1.1.1"
CREDIT_ACCOUNT_CODE = "1.1.2"
RECEIVABLE_ACCOUNT_CODE = "1.2.1"
PAYABLE_ACCOUNT_CODE = "2.1.1"
REVENUE_ACCOUNT_CODE = "4.1.1"
SERVICES_ACCOUNT_CODE = "4.1.2"

CUSTOMER_NAME = "HesabYar E2E Customer"
PAYMENT_JOURNAL_NAME = "HesabYar E2E Payments"
STATEMENT_JOURNAL_NAME = "HesabYar E2E Bank"

# A date inside FY 2026's open November period.
MOVE_DATE = datetime.date(2026, 11, 17)

MISSING_ID = 999_999_999

ALL_SCOPES = [
    "ledger:read",
    "ledger:draft:create",
    "ledger:draft:update",
    "ledger:post",
    "ledger:reverse",
    "invoice:read",
    "invoice:draft:create",
    "payment:read",
    "banking:read",
    "banking:reconcile",
    "settings:manage",
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
    """The test company's general journal, looked up BY NAME."""
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
def revenue_journal(tryton_client: TrytonRestClient) -> JournalRef:
    """The global revenue journal, looked up BY TYPE."""
    records = tryton_client.search(
        "account.journal", domain=[("type", "=", "revenue")], fields=["name"]
    )
    if not records:
        pytest.skip("No account.journal of type 'revenue' in the test DB.")
    return JournalRef(int(records[0]["id"]))


@pytest.fixture(scope="module")
def accounts(
    adapter: TrytonAccountingAdapter, test_company: CompanyRef
) -> dict[str, AccountRef]:
    chart = {view.code: view for view in adapter.get_chart_of_accounts(test_company)}
    wanted = (
        DEBIT_ACCOUNT_CODE,
        CREDIT_ACCOUNT_CODE,
        RECEIVABLE_ACCOUNT_CODE,
        PAYABLE_ACCOUNT_CODE,
        REVENUE_ACCOUNT_CODE,
        SERVICES_ACCOUNT_CODE,
    )
    missing = [code for code in wanted if code not in chart]
    if missing:
        pytest.skip(f"Chart accounts missing from test company: {missing}.")
    return {code: chart[code].ref for code in wanted}


@pytest.fixture(scope="module")
def irr_currency_id(tryton_client: TrytonRestClient) -> int:
    records = tryton_client.search(
        "currency.currency", domain=[("code", "=", "IRR")], fields=["code"]
    )
    assert records, "IRR currency not found in the test DB"
    return int(records[0]["id"])


@pytest.fixture(scope="module")
def customer(
    tryton_client: TrytonRestClient,
) -> Generator[tuple[PartyRef, PartyAddressRef], None, None]:
    """A customer party with one address, created via REST."""
    party = tryton_client.create("party.party", {"name": CUSTOMER_NAME}, fields=["id"])
    party_id = int(party["id"])
    address = tryton_client.create(
        "party.address", {"party": party_id, "city": "Tehran"}, fields=["id"]
    )
    address_id = int(address["id"])
    yield PartyRef(party_id), PartyAddressRef(address_id)
    tryton_client.delete("party.address", address_id)
    tryton_client.delete("party.party", party_id)


@pytest.fixture(scope="module")
def payment_journal(
    tryton_client: TrytonRestClient, test_company: CompanyRef, irr_currency_id: int
) -> Generator[int, None, None]:
    """A manual payment journal, created via REST (deleted afterwards)."""
    record = tryton_client.create(
        "account.payment.journal",
        {
            "name": PAYMENT_JOURNAL_NAME,
            "currency": irr_currency_id,
            "company": test_company.engine_id,
            "process_method": "manual",
        },
        fields=["id"],
        context={"company": test_company.engine_id},
    )
    journal_id = int(record["id"])
    yield journal_id
    tryton_client.delete(
        "account.payment.journal",
        journal_id,
        context={"company": test_company.engine_id},
    )


@pytest.fixture(scope="module")
def statement_journal(
    tryton_client: TrytonRestClient,
    test_company: CompanyRef,
    irr_currency_id: int,
    accounts: dict[str, AccountRef],
) -> Generator[int, None, None]:
    """A balance-validated statement journal, created via REST.

    Uses the global 'Statement' account journal (type 'statement',
    created by module activation) and the cash account 1.1.1.
    """
    journals = tryton_client.search(
        "account.journal", domain=[("type", "=", "statement")], fields=["name"]
    )
    if not journals:
        pytest.skip("No account.journal of type 'statement' in the test DB.")
    record = tryton_client.create(
        "account.statement.journal",
        {
            "name": STATEMENT_JOURNAL_NAME,
            "journal": int(journals[0]["id"]),
            "currency": irr_currency_id,
            "company": test_company.engine_id,
            "validation": "balance",
            "account": accounts[DEBIT_ACCOUNT_CODE].engine_id,
        },
        fields=["id"],
        context={"company": test_company.engine_id},
    )
    journal_id = int(record["id"])
    yield journal_id
    tryton_client.delete(
        "account.statement.journal",
        journal_id,
        context={"company": test_company.engine_id},
    )


@pytest.fixture()
def created_moves(tryton_client: TrytonRestClient) -> Generator[list[int], None, None]:
    """Track moves created by a test and delete them afterwards (drafts only)."""
    ids: list[int] = []
    yield ids
    for move_id in ids:
        try:
            record = tryton_client.read("account.move", move_id, fields=["state"])
        except Exception:
            continue
        if record is not None and record.get("state") == "draft":
            tryton_client.delete("account.move", move_id)


@pytest.fixture()
def created_invoices(
    tryton_client: TrytonRestClient,
) -> Generator[list[int], None, None]:
    """Track invoices created by a test and delete them afterwards (drafts only)."""
    ids: list[int] = []
    yield ids
    for invoice_id in ids:
        try:
            tryton_client.delete("account.invoice", invoice_id)
        except Exception:
            continue


@pytest.fixture()
def created_payments(
    tryton_client: TrytonRestClient,
) -> Generator[list[int], None, None]:
    ids: list[int] = []
    yield ids
    for payment_id in ids:
        try:
            tryton_client.delete("account.payment", payment_id)
        except Exception:
            continue


@pytest.fixture()
def created_statements(
    tryton_client: TrytonRestClient,
) -> Generator[list[int], None, None]:
    ids: list[int] = []
    yield ids
    for statement_id in ids:
        try:
            tryton_client.delete("account.statement", statement_id)
        except Exception:
            continue


# ---------------------------------------------------------------------------
# Provisioned world (real DB, real keys, real app)
# ---------------------------------------------------------------------------


@dataclass
class World:
    """Provisioned tenant, company mapping and keys for one test."""

    tenant_id: str
    company_row_id: str
    client_id: str
    engine_id: int
    key_full_secret: str  # all Phase 2 scopes incl. settings:manage, unrestricted
    key_admin_secret: str  # settings:manage only
    key_read_secret: str  # invoice:read only


@pytest.fixture(scope="module")
def engine() -> Generator[Engine, None, None]:
    # pool_pre_ping: pooled connections sit idle for minutes while a
    # test talks to live Tryton, and Supabase drops idle SSL
    # connections — without pre-ping the teardown cleanup session
    # checks out the dead connection and dies on its first
    # statement (seen on the ~9-min full-cycle test).
    eng = create_engine(TEST_DB_URL, echo=False, pool_pre_ping=True)
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
            id=str(uuid.uuid4()), slug=f"rest-205-e2e-{uuid.uuid4().hex[:8]}", name="E2E Tenant"
        )
        session.add(tenant)
        session.flush()
        client = ApiClientModel(
            id=str(uuid.uuid4()), tenant_id=tenant.id, name="rest-205 e2e client"
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
                name="rest-205 e2e key",
                scopes=scopes,
                installation_id=INSTALLATION_ID,
            )

        key_full = _key(ALL_SCOPES)
        key_admin = _key(["settings:manage"])
        key_read = _key(["invoice:read"])
        session.commit()
        tenant_id = tenant.id
        world = World(
            tenant_id=tenant.id,
            company_row_id=row.id,
            client_id=client.id,
            engine_id=test_company.engine_id,
            key_full_secret=key_full.secret,
            key_admin_secret=key_admin.secret,
            key_read_secret=key_read.secret,
        )
        # Close the setup session BEFORE yielding: the test runs for
        # minutes against live Tryton while this session would sit
        # idle holding an open transaction (the post-commit attribute
        # reads above refresh expired instances) — Supabase drops the
        # idle SSL connection, and teardown's rollback then fails with
        # OperationalError (seen 3x on the ~9-min full-cycle test).
        # Nothing after this point uses the session: the test talks
        # to the app's own sessions, and teardown opens its own
        # cleanup session.
        session.close()
        yield world
    finally:
        session.close()  # idempotent: already closed above
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
    accounts: dict[str, AccountRef],
    journal: JournalRef,
    amount: int = 1_000_000,
) -> dict:
    return {
        "company_id": world.company_row_id,
        "date": MOVE_DATE.isoformat(),
        "description": "US-205 e2e draft",
        "journal_id": journal.engine_id,
        "lines": [
            {
                "account_id": accounts[DEBIT_ACCOUNT_CODE].engine_id,
                "debit": amount,
                "credit": 0,
            },
            {
                "account_id": accounts[CREDIT_ACCOUNT_CODE].engine_id,
                "debit": 0,
                "credit": amount,
            },
        ],
    }


def _invoice_body(
    world: World,
    accounts: dict[str, AccountRef],
    customer: tuple[PartyRef, PartyAddressRef],
    revenue_journal: JournalRef,
) -> dict:
    party, address = customer
    return {
        "company_id": world.company_row_id,
        "invoice_type": "out",
        "party_id": party.engine_id,
        "date": MOVE_DATE.isoformat(),
        "description": "US-205 e2e invoice",
        "journal_id": revenue_journal.engine_id,
        "account_id": accounts[RECEIVABLE_ACCOUNT_CODE].engine_id,
        "invoice_address_id": address.engine_id,
        "lines": [
            {
                "account_id": accounts[REVENUE_ACCOUNT_CODE].engine_id,
                "unit_price": 500_000,
                "quantity": 1,
                "description": "Goods",
            },
            {
                "account_id": accounts[SERVICES_ACCOUNT_CODE].engine_id,
                "unit_price": 125_000,
                "quantity": 2,
                "description": "Services",
            },
        ],
    }


def _raw_payment(
    client: TrytonRestClient,
    company: CompanyRef,
    payment_journal_id: int,
    party: PartyRef,
    amount: int = 500_000,
) -> int:
    record = client.create(
        "account.payment",
        {
            "company": company.engine_id,
            "journal": payment_journal_id,
            "kind": "receivable",
            "party": party.engine_id,
            "date": MOVE_DATE,
            "amount": Decimal(amount),
            "reference": "US-205 e2e payment",
        },
        fields=["id"],
        context={"company": company.engine_id},
    )
    return int(record["id"])


def _raw_statement(
    client: TrytonRestClient,
    company: CompanyRef,
    statement_journal_id: int,
    party: PartyRef,
    accounts: dict[str, AccountRef],
) -> int:
    record = client.create(
        "account.statement",
        {
            "name": "US-205 e2e statement",
            "company": company.engine_id,
            "journal": statement_journal_id,
            "date": MOVE_DATE,
            "start_balance": Decimal("0"),
            "end_balance": Decimal("750000"),
            "lines": [
                [
                    "create",
                    [
                        {
                            "date": MOVE_DATE,
                            "amount": Decimal("1000000"),
                            "account": accounts[RECEIVABLE_ACCOUNT_CODE].engine_id,
                            "party": party.engine_id,
                            "description": "Customer receipt",
                        },
                        {
                            "date": MOVE_DATE,
                            "amount": Decimal("-250000"),
                            "account": accounts[PAYABLE_ACCOUNT_CODE].engine_id,
                            "party": party.engine_id,
                            "description": "Supplier payment",
                        },
                    ],
                ]
            ],
        },
        fields=["id"],
        context={"company": company.engine_id},
    )
    return int(record["id"])


# ---------------------------------------------------------------------------
# The Phase 2 exit proof: full cycle through the API, live
# ---------------------------------------------------------------------------


def test_phase2_full_cycle_e2e(
    api: TestClient,
    world: World,
    accounts: dict[str, AccountRef],
    journal: JournalRef,
    revenue_journal: JournalRef,
    customer: tuple[PartyRef, PartyAddressRef],
    payment_journal: int,
    statement_journal: int,
    adapter: TrytonAccountingAdapter,
    tryton_client: TrytonRestClient,
    test_company: CompanyRef,
    created_moves: list[int],
    created_invoices: list[int],
    created_payments: list[int],
    created_statements: list[int],
) -> None:
    """The whole accounting cycle runs through the API on live Tryton.

    Journal draft → validate → post → reverse, then invoice draft →
    validate, then payments/statements reads, reconcile (US-514),
    and an audit-trail query proving every mutating step was recorded.
    Replays of the post and the invoice create return the stored
    response with no second engine write.
    """
    run = uuid.uuid4().hex[:8]
    headers = _auth(world.key_full_secret)

    # -- journal draft → validate → post → reverse -------------------------

    draft_response = api.post(
        "/api/v1/accounting/journal-drafts",
        json=_draft_body(world, accounts, journal),
        headers=_auth(world.key_full_secret, f"p2-{run}-create"),
    )
    assert draft_response.status_code == 201, draft_response.text
    draft = draft_response.json()
    assert draft["state"] == "draft"
    draft_id = draft["id"]
    created_moves.append(draft_id)

    validation = api.post(
        f"/api/v1/accounting/journal-drafts/{draft_id}/validate", headers=headers
    )
    assert validation.status_code == 200
    assert validation.json() == {"valid": True, "issues": []}

    post_headers = _auth(world.key_full_secret, f"p2-{run}-post")
    posted = api.post(
        f"/api/v1/accounting/journal-drafts/{draft_id}/post", headers=post_headers
    )
    assert posted.status_code == 200, posted.text
    posted_body = posted.json()
    assert posted_body["id"] == draft_id
    assert posted_body["number"], "the engine must assign a move number"

    # Replaying the post replays the stored response — no second posting.
    replayed_post = api.post(
        f"/api/v1/accounting/journal-drafts/{draft_id}/post", headers=post_headers
    )
    assert replayed_post.status_code == 200
    assert replayed_post.json() == posted_body

    reversal = api.post(
        f"/api/v1/accounting/journals/{draft_id}/reverse",
        headers=_auth(world.key_full_secret, f"p2-{run}-reverse"),
    )
    assert reversal.status_code == 201, reversal.text
    reversal_body = reversal.json()
    assert reversal_body["origin_id"] == draft_id
    reversal_id = reversal_body["id"]
    created_moves.append(reversal_id)

    # The reversal is visible in Tryton, linked to the original.
    reversal_view = adapter.get_journal(PostedJournalRef(reversal_id))
    assert reversal_view is not None
    assert reversal_view.origin == PostedJournalRef(draft_id)
    assert reversal_view.is_reversal

    # -- invoice draft → validate ------------------------------------------

    invoice_body = _invoice_body(world, accounts, customer, revenue_journal)
    create_headers = _auth(world.key_full_secret, f"p2-{run}-invoice")
    created = api.post(
        "/api/v1/invoices/drafts", json=invoice_body, headers=create_headers
    )
    assert created.status_code == 201, created.text
    invoice = created.json()
    assert invoice["state"] == "draft"
    assert invoice["invoice_type"] == "out"
    assert invoice["party_name"] == CUSTOMER_NAME
    assert invoice["total_amount"] == 750_000
    assert invoice["untaxed_amount"] == 750_000
    assert invoice["tax_amount"] == 0
    assert len(invoice["lines"]) == 2
    invoice_id = invoice["id"]
    created_invoices.append(invoice_id)

    # Replaying the invoice create replays the stored response — no
    # second invoice is created in the engine.
    replayed_create = api.post(
        "/api/v1/invoices/drafts", json=invoice_body, headers=create_headers
    )
    assert replayed_create.status_code == 201
    assert replayed_create.json() == invoice

    invoice_validation = api.post(
        f"/api/v1/invoices/{invoice_id}/validate", headers=headers
    )
    assert invoice_validation.status_code == 200, invoice_validation.text
    assert invoice_validation.json() == {"valid": True, "issues": []}

    # The draft is visible in Tryton with the engine-computed totals.
    invoice_view = adapter.get_invoice(InvoiceRef(invoice_id))
    assert invoice_view is not None
    assert invoice_view.state == "draft"
    assert invoice_view.total_amount.amount == 750_000

    # List/get through the API see it.
    listed = api.get(
        f"/api/v1/invoices?company={world.company_row_id}&type=out&state=draft",
        headers=headers,
    )
    assert listed.status_code == 200
    assert invoice_id in [item["id"] for item in listed.json()["items"]]
    got = api.get(f"/api/v1/invoices/{invoice_id}", headers=headers)
    assert got.status_code == 200
    assert got.json()["total_amount"] == 750_000

    # -- payments + statements through the API ------------------------------

    party, _address = customer
    payment_id = _raw_payment(
        tryton_client, test_company, payment_journal, party
    )
    created_payments.append(payment_id)
    payments = api.get(
        f"/api/v1/payments?company={world.company_row_id}&kind=receivable",
        headers=headers,
    )
    assert payments.status_code == 200
    assert payment_id in [item["id"] for item in payments.json()["items"]]
    got_payment = api.get(f"/api/v1/payments/{payment_id}", headers=headers)
    assert got_payment.status_code == 200
    assert got_payment.json()["amount"] == 500_000
    assert got_payment.json()["party_name"] == CUSTOMER_NAME

    statement_id = _raw_statement(
        tryton_client, test_company, statement_journal, party, accounts
    )
    created_statements.append(statement_id)
    statements = api.get(
        f"/api/v1/bank/statements?company={world.company_row_id}&state=draft",
        headers=headers,
    )
    assert statements.status_code == 200
    assert statement_id in [item["id"] for item in statements.json()["items"]]
    got_statement = api.get(
        f"/api/v1/bank/statements/{statement_id}", headers=headers
    )
    assert got_statement.status_code == 200
    assert got_statement.json()["end_balance"] == 750_000
    assert [line["amount"] for line in got_statement.json()["lines"]] == [
        1_000_000,
        -250_000,
    ]

    import_status = api.get(
        f"/api/v1/bank/statements/import-status?company={world.company_row_id}",
        headers=headers,
    )
    assert import_status.status_code == 200
    status_body = import_status.json()
    assert status_body["total_statements"] >= 1
    assert status_body["draft_count"] >= 1
    assert status_body["latest_statement_id"] == statement_id
    assert status_body["total_lines"] >= 2

    # Reconcile is implemented (US-514): the statement lines run
    # through three-priority matching. The exact split depends on
    # what the shared engine holds, and is covered by the US-514
    # REST tests; here we assert the endpoint works and accounts
    # for every line.
    reconcile = api.post(
        f"/api/v1/bank/statements/{statement_id}/reconcile",
        headers=_auth(world.key_full_secret, f"p2-{run}-reconcile"),
    )
    assert reconcile.status_code == 200, reconcile.text
    reconcile_body = reconcile.json()
    assert reconcile_body["statement_id"] == statement_id
    assert reconcile_body["total_lines"] == 2
    assert (
        reconcile_body["matched_count"]
        + reconcile_body["suggested_count"]
        + reconcile_body["ambiguous_count"]
        + reconcile_body["unmatched_count"]
        + reconcile_body["already_matched_count"]
        == 2
    )

    # -- audit trail: every mutating step recorded --------------------------

    admin_headers = _auth(world.key_admin_secret)
    trail = api.get(
        f"/api/v1/audit/events?company={world.company_row_id}",
        headers=admin_headers,
    )
    assert trail.status_code == 200, trail.text
    events = trail.json()["items"]
    # Exactly the four mutations (validates are reads and audit nothing;
    # replays replay without re-auditing).
    assert sorted(
        (event["action"], event["outcome"]) for event in events
    ) == sorted(
        [
            ("ledger.draft.create", "success"),
            ("ledger.post", "success"),
            ("ledger.reverse", "success"),
            ("invoice.draft.create", "success"),
        ]
    )
    # Newest first.
    occurred = [event["occurred_at"] for event in events]
    assert occurred == sorted(occurred, reverse=True)
    assert all(event["tryton_reference"] for event in events)
    assert all(event["api_client_id"] == world.client_id for event in events)

    # Filters narrow the trail.
    posts = api.get(
        "/api/v1/audit/events?action=ledger.post", headers=admin_headers
    )
    assert [event["action"] for event in posts.json()["items"]] == ["ledger.post"]
    by_actor = api.get(
        f"/api/v1/audit/events?actor={world.client_id}", headers=admin_headers
    )
    assert len(by_actor.json()["items"]) == 4
    now = datetime.datetime.now(datetime.UTC)
    window = api.get(
        "/api/v1/audit/events",
        params={
            "from": (now - datetime.timedelta(hours=1)).isoformat(),
            "to": (now + datetime.timedelta(hours=1)).isoformat(),
        },
        headers=admin_headers,
    )
    assert window.status_code == 200, window.text
    assert len(window.json()["items"]) == 4

    # Cursor pagination walks the whole trail without duplicates.
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(5):
        url = "/api/v1/audit/events?limit=2"
        if cursor is not None:
            url += f"&cursor={cursor}"
        page = api.get(url, headers=admin_headers)
        assert page.status_code == 200
        body = page.json()
        seen.extend(event["id"] for event in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert sorted(seen) == sorted(event["id"] for event in events)
    assert len(seen) == len(set(seen))


# ---------------------------------------------------------------------------
# Focused live checks
# ---------------------------------------------------------------------------


def test_e2e_invoice_create_conflict(
    api: TestClient,
    world: World,
    accounts: dict[str, AccountRef],
    customer: tuple[PartyRef, PartyAddressRef],
    revenue_journal: JournalRef,
    created_invoices: list[int],
) -> None:
    """A key reused with a different invoice payload is a 409, never a replay."""
    headers = _auth(world.key_full_secret, f"p2-conflict-{uuid.uuid4().hex[:8]}")
    body = _invoice_body(world, accounts, customer, revenue_journal)
    first = api.post("/api/v1/invoices/drafts", json=body, headers=headers)
    assert first.status_code == 201
    created_invoices.append(first.json()["id"])
    changed = dict(body, description="a different invoice")
    second = api.post("/api/v1/invoices/drafts", json=changed, headers=headers)
    assert second.status_code == 409
    assert second.json()["code"] == "IDEMPOTENCY_CONFLICT"


def test_e2e_invoice_unknown_party_is_404(
    api: TestClient,
    world: World,
    accounts: dict[str, AccountRef],
    customer: tuple[PartyRef, PartyAddressRef],
    revenue_journal: JournalRef,
) -> None:
    """An invoice for a party the engine does not know is a typed 404."""
    body = _invoice_body(world, accounts, customer, revenue_journal)
    body["party_id"] = MISSING_ID
    response = api.post(
        "/api/v1/invoices/drafts",
        json=body,
        headers=_auth(world.key_full_secret, f"p2-party-{uuid.uuid4().hex[:8]}"),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "LEDGER_PARTY_NOT_FOUND"


def test_e2e_unknown_documents_are_404(api: TestClient, world: World) -> None:
    """Unknown invoice/payment/statement ids are typed 404s."""
    headers = _auth(world.key_full_secret)
    response = api.get(f"/api/v1/invoices/{MISSING_ID}", headers=headers)
    assert response.status_code == 404
    assert response.json()["code"] == "LEDGER_INVOICE_NOT_FOUND"
    response = api.get(f"/api/v1/payments/{MISSING_ID}", headers=headers)
    assert response.status_code == 404
    assert response.json()["code"] == "LEDGER_PAYMENT_NOT_FOUND"
    response = api.get(f"/api/v1/bank/statements/{MISSING_ID}", headers=headers)
    assert response.status_code == 404
    assert response.json()["code"] == "LEDGER_STATEMENT_NOT_FOUND"


def test_e2e_invoice_filters_live(
    api: TestClient,
    world: World,
    accounts: dict[str, AccountRef],
    customer: tuple[PartyRef, PartyAddressRef],
    revenue_journal: JournalRef,
    created_invoices: list[int],
) -> None:
    """List filters show the created draft in the right lists only."""
    body = _invoice_body(world, accounts, customer, revenue_journal)
    created = api.post(
        "/api/v1/invoices/drafts",
        json=body,
        headers=_auth(world.key_full_secret, f"p2-filter-{uuid.uuid4().hex[:8]}"),
    )
    assert created.status_code == 201
    invoice_id = created.json()["id"]
    created_invoices.append(invoice_id)
    headers = _auth(world.key_full_secret)
    base = f"/api/v1/invoices?company={world.company_row_id}"
    assert invoice_id in [
        item["id"] for item in api.get(f"{base}&type=out", headers=headers).json()["items"]
    ]
    assert invoice_id in [
        item["id"]
        for item in api.get(f"{base}&state=draft", headers=headers).json()["items"]
    ]
    assert invoice_id not in [
        item["id"] for item in api.get(f"{base}&type=in", headers=headers).json()["items"]
    ]
    assert invoice_id not in [
        item["id"]
        for item in api.get(f"{base}&state=posted", headers=headers).json()["items"]
    ]


def test_e2e_audit_requires_admin(api: TestClient, world: World) -> None:
    """The audit trail is admin-only: 401 without a key, 403 without the scope."""
    response = api.get("/api/v1/audit/events")
    assert response.status_code == 401
    response = api.get(
        "/api/v1/audit/events", headers=_auth(world.key_read_secret)
    )
    assert response.status_code == 403
    assert "settings:manage" in response.json()["detail"]
