"""Tests for the store webhook receiver REST endpoints (US-511).

The real FastAPI app + scoped API-key auth + the PostgreSQL
database. Store topics map to canonical events at ingest
(``refund.created`` fans out to two inbox rows); pending-ADR topics
are rejected 422; HMAC verification runs against the store
registry's encrypted secret. The worker-poll tests prove the store
pipeline wiring end to end through the PRODUCTION wiring
(``dependencies.py``): no mappings → needs_review (no engine calls —
the live Tryton server is NOT needed there); configured mappings →
an invoice draft in Tryton (needs the live server + test company,
skips cleanly otherwise).

Each test gets a FRESH installation id, so store rows never leak
between tests; teardown deletes the installation's stores +
mappings + idempotency records and the provisioned tenant (which
cascades to inbox events and audit decisions).
"""

from __future__ import annotations

import hashlib
import hmac
import json
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
    CompanyRef,
    PartyAddressRef,
    PartyRef,
    PaymentJournalRef,
)
from hesabyar.domain.integrations.commands import CreateIntegrationMapping
from hesabyar.domain.integrations.models import MappingKind
from hesabyar.infrastructure.accounting.tryton.adapter import TrytonAccountingAdapter
from hesabyar.infrastructure.accounting.tryton.client import TrytonRestClient
from hesabyar.infrastructure.auth import ApiKeyCrypto, ApiKeyService
from hesabyar.infrastructure.auth.credential_vault import generate_encryption_key
from hesabyar.infrastructure.config import AppSettings, AuthSettings
from hesabyar.infrastructure.persistence.postgres.integrations_repository import (
    SqlAlchemyIntegrationsRepository,
)
from hesabyar.infrastructure.persistence.postgres.models import (
    ApiClientModel,
    AutoPostPolicyModel,
    Base,
    IdempotencyRecordModel,
    InstallationSettingsModel,
    IntegrationMappingModel,
    StoreModel,
    TenantModel,
)
from hesabyar.interfaces.rest.app import create_app
from tests.event_fixtures import raw_envelope

TEST_DB_URL = os.getenv(
    "HESABYAR_DATABASE_URL",
    "postgresql+psycopg://hesabyar:hesabyar_pass@hesabyar-postgres:5432/hesabyar_test",
)
TRYTON_BASE_URL = os.environ.get("TRYTON_BASE_URL", "http://localhost:8000")
TRYTON_DATABASE = os.environ.get("TRYTON_DATABASE", "hesabyar_tryton_test")
TRYTON_API_KEY = os.environ.get("TRYTON_API_KEY", "hesabyar-dev-rest-key")
TEST_COMPANY_NAME = os.environ.get("TRYTON_TEST_COMPANY", "HesabYar Adapter Test")

ALL_INTEGRATIONS_SCOPES = ["integrations:read", "integrations:manage"]

CUSTOMER_ID = "cust-511-rest"
CUSTOMER_NAME = "HesabYar US-511 REST Customer"
PAYMENT_JOURNAL_NAME = "HesabYar US-511 REST Payments"
REVENUE_ACCOUNT_CODE = "4.1.1"
SHIPPING_ACCOUNT_CODE = "4.1.2"


@dataclass
class World:
    installation_id: str
    tenant_id: str
    key_full_secret: str
    key_read_secret: str
    source_system: str


@pytest.fixture(scope="module")
def engine() -> Generator[Engine, None, None]:
    eng = create_engine(TEST_DB_URL, echo=False)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def settings() -> AppSettings:
    """Fresh settings with a test-generated vault key (dev pepper)."""
    return AppSettings(
        auth=AuthSettings(credentials_encryption_key=generate_encryption_key())
    )


@pytest.fixture()
def world(engine: Engine, settings: AppSettings) -> Generator[World, None, None]:
    """Provision a tenant with integrations-scoped API keys."""
    session = sessionmaker(bind=engine)()
    service = ApiKeyService(ApiKeyCrypto(settings.auth.api_key_pepper))
    installation_id = str(uuid.uuid4())
    tenant_id = ""
    source_system = f"evt-rest-511-{uuid.uuid4().hex[:8]}"
    try:
        tenant = TenantModel(
            id=str(uuid.uuid4()), slug=f"rest-511-{uuid.uuid4().hex[:8]}", name="511 Tenant"
        )
        session.add(tenant)
        session.flush()
        client = ApiClientModel(
            id=str(uuid.uuid4()), tenant_id=tenant.id, name="rest-511 client"
        )
        session.add(client)
        session.flush()
        key_full = service.issue_key(
            session,
            client=client,
            name="rest-511 full",
            scopes=ALL_INTEGRATIONS_SCOPES,
            installation_id=installation_id,
        )
        key_read = service.issue_key(
            session,
            client=client,
            name="rest-511 read",
            scopes=["integrations:read"],
            installation_id=installation_id,
        )
        session.commit()
        tenant_id = tenant.id
        yield World(
            installation_id=installation_id,
            tenant_id=tenant.id,
            key_full_secret=key_full.secret,
            key_read_secret=key_read.secret,
            source_system=source_system,
        )
    finally:
        session.close()
        cleanup = sessionmaker(bind=engine)()
        try:
            cleanup.execute(
                delete(IdempotencyRecordModel).where(
                    IdempotencyRecordModel.installation_id == installation_id
                )
            )
            cleanup.execute(
                delete(StoreModel).where(StoreModel.installation_id == installation_id)
            )
            cleanup.execute(
                delete(IntegrationMappingModel).where(
                    IntegrationMappingModel.installation_id == installation_id
                )
            )
            cleanup.execute(
                delete(AutoPostPolicyModel).where(
                    AutoPostPolicyModel.installation_id == installation_id
                )
            )
            cleanup.execute(
                delete(InstallationSettingsModel).where(
                    InstallationSettingsModel.installation_id == installation_id
                )
            )
            if tenant_id:
                cleanup.query(TenantModel).filter(
                    TenantModel.id == tenant_id
                ).delete(synchronize_session=False)
            cleanup.commit()
        finally:
            cleanup.close()


@pytest.fixture()
def api(settings: AppSettings) -> TestClient:
    # Server exceptions surface as tracebacks (a 500 must never hide
    # behind a status assertion); 4xx responses still return normally.
    return TestClient(create_app(settings=settings), raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Live Tryton fixtures (only for the with-mappings worker test)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def tryton_client() -> Generator[TrytonRestClient, None, None]:
    rest_client = TrytonRestClient(TRYTON_BASE_URL, TRYTON_DATABASE, TRYTON_API_KEY, timeout=120.0)
    if not rest_client.ping():
        rest_client.close()
        pytest.skip(
            f"trytond server not reachable at {TRYTON_BASE_URL} "
            "(start it per docs/dev-setup.md §3 to run the mapped worker test)"
        )
    yield rest_client
    rest_client.close()


@pytest.fixture(scope="module")
def adapter(tryton_client: TrytonRestClient) -> TrytonAccountingAdapter:
    return TrytonAccountingAdapter(tryton_client)


@pytest.fixture(scope="module")
def company(adapter: TrytonAccountingAdapter) -> CompanyRef:
    for view in adapter.list_companies():
        if view.name == TEST_COMPANY_NAME:
            return view.ref
    pytest.skip(
        f"Test company {TEST_COMPANY_NAME!r} not found in Tryton DB {TRYTON_DATABASE!r}. "
        "Provision it with setup_adapter_test_company.py (see docs/adapter.md)."
    )


@pytest.fixture(scope="module")
def accounts(adapter: TrytonAccountingAdapter, company: CompanyRef):
    chart = {view.code: view for view in adapter.get_chart_of_accounts(company)}
    missing = [code for code in (REVENUE_ACCOUNT_CODE, SHIPPING_ACCOUNT_CODE) if code not in chart]
    if missing:
        pytest.skip(f"Chart accounts missing from test company: {missing}.")
    return {code: chart[code] for code in (REVENUE_ACCOUNT_CODE, SHIPPING_ACCOUNT_CODE)}


@pytest.fixture(scope="module")
def customer(tryton_client: TrytonRestClient):
    """A customer party with one address, created via REST."""
    party = tryton_client.create("party.party", {"name": CUSTOMER_NAME}, fields=["id"])
    party_id = int(party["id"])
    address = tryton_client.create(
        "party.address", {"party": party_id, "city": "Tehran"}, fields=["id"]
    )
    address_id = int(address["id"])
    yield PartyRef(party_id), PartyAddressRef(address_id)
    for model, rid in (("party.address", address_id), ("party.party", party_id)):
        try:
            tryton_client.delete(model, rid)
        except Exception:
            pass  # referenced by invoice move lines


@pytest.fixture(scope="module")
def payment_journal(
    tryton_client: TrytonRestClient, company: CompanyRef
) -> Generator[PaymentJournalRef, None, None]:
    records = tryton_client.search(
        "currency.currency", domain=[("code", "=", "IRR")], fields=["code"]
    )
    assert records, "IRR currency not found in the test DB"
    record = tryton_client.create(
        "account.payment.journal",
        {
            "name": PAYMENT_JOURNAL_NAME,
            "currency": int(records[0]["id"]),
            "company": company.engine_id,
            "process_method": "manual",
        },
        fields=["id"],
        context={"company": company.engine_id},
    )
    journal_id = int(record["id"])
    yield PaymentJournalRef(journal_id)
    try:
        tryton_client.delete(
            "account.payment.journal",
            journal_id,
            context={"company": company.engine_id},
        )
    except Exception:
        pass  # referenced by test payments


def _auth(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


def _ingest_body(world: World, **overrides: object) -> dict:
    return raw_envelope(source_system=world.source_system, **overrides)  # type: ignore[arg-type]


def _ingest_headers(world: World, event_type: str, signature: str | None = None) -> dict[str, str]:
    headers = {
        **_auth(world.key_full_secret),
        "Content-Type": "application/json",
        "X-HesabYar-Event-Type": event_type,
    }
    if signature is not None:
        headers["X-HesabYar-Signature"] = signature
    return headers


def _post_event(
    api: TestClient, world: World, event_type: str, body: dict, signature: str | None = None
):
    return api.post(
        f"/api/v1/integrations/{world.source_system}/events",
        content=json.dumps(body),
        headers=_ingest_headers(world, event_type, signature),
    )


def _create_store(api: TestClient, world: World) -> str:
    """Create a store via the API; return its (once-shown) webhook secret."""
    response = api.post(
        "/api/v1/integrations/stores",
        json={
            "store_key": world.source_system,
            "display_name": "فروشگاه تست",
            "store_type": "custom",
        },
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 201, response.text
    assert response.json()["store"]["has_webhook_secret"] is True
    return response.json()["webhook_secret"]


def _sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _inbox_types(api: TestClient, world: World) -> list[str]:
    listing = api.get("/api/v1/integrations/inbox", headers=_auth(world.key_read_secret))
    assert listing.status_code == 200, listing.text
    return [item["event_type"] for item in listing.json()["items"]]


# ---------------------------------------------------------------------------
# Store topics at ingest
# ---------------------------------------------------------------------------


def test_ingest_order_completed_topic_maps_to_the_canonical_type(
    api: TestClient, world: World
) -> None:
    response = _post_event(api, world, "order.completed", _ingest_body(world))
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "created"
    assert response.json()["derived_inbox_event_ids"] == []
    assert _inbox_types(api, world) == ["sales.order.completed"]


def test_ingest_refund_created_fans_out_to_two_inbox_rows(
    api: TestClient, world: World
) -> None:
    body = _ingest_body(
        world,
        source_event_id="webhook-refund-1",
        payload={
            "refund_id": "refund-1",
            "order_id": "order-1",
            "refund_amount": 50000,
            "payment_channel": "zarinpal",
            "gross_amount": 50000,
        },
        external_entity_type="refund",
        external_entity_id="refund-1",
    )
    response = _post_event(api, world, "refund.created", body)
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["status"] == "created"
    assert len(data["derived_inbox_event_ids"]) == 1
    assert data["derived_inbox_event_ids"][0] != data["inbox_event_id"]
    assert sorted(_inbox_types(api, world)) == ["payment.refunded", "sales.refund.created"]


def test_ingest_refund_created_twice_replays_both_legs_as_duplicates(
    api: TestClient, world: World
) -> None:
    body = _ingest_body(
        world,
        source_event_id="webhook-refund-1",
        payload={
            "refund_id": "refund-1",
            "order_id": "order-1",
            "refund_amount": 50000,
            "payment_channel": "zarinpal",
        },
        external_entity_type="refund",
        external_entity_id="refund-1",
    )
    first = _post_event(api, world, "refund.created", body)
    assert first.status_code == 201, first.text
    second = _post_event(api, world, "refund.created", body)
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "duplicate"
    assert second.json()["inbox_event_id"] == first.json()["inbox_event_id"]
    assert second.json()["derived_inbox_event_ids"] == first.json()["derived_inbox_event_ids"]
    assert len(_inbox_types(api, world)) == 2


@pytest.mark.parametrize(
    "topic",
    ["bank_transfer.recorded", "shipment.dispatched"],
    ids=["bank-transfer", "shipment"],
)
def test_ingest_pending_adr_topic_is_422(api: TestClient, world: World, topic: str) -> None:
    """Topics whose canonical names need ADR approval are never ingested.

    ``cheque.registered`` was pending until ADR 0004 (US-530) approved
    ``cheque.received.registered`` and US-533 wired it in — it now
    ingests (see test_ingest_cheque_registered_is_accepted), so it no
    longer belongs in this list.
    """
    response = _post_event(api, world, topic, _ingest_body(world))
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "EVENT_TYPE_PENDING_ADR_APPROVAL"
    assert _inbox_types(api, world) == []


# ---------------------------------------------------------------------------
# HMAC verification against the store registry
# ---------------------------------------------------------------------------


def test_ingest_with_a_valid_store_signature_verifies(api: TestClient, world: World) -> None:
    secret = _create_store(api, world)
    body = _ingest_body(world)
    raw = json.dumps(body).encode()
    response = _post_event(api, world, "order.completed", body, signature=_sign(raw, secret))
    assert response.status_code == 201, response.text
    assert response.json()["signature_verified"] is True


def test_ingest_with_a_bad_store_signature_is_401(api: TestClient, world: World) -> None:
    secret = _create_store(api, world)
    body = _ingest_body(world)
    raw = json.dumps(body).encode()
    response = _post_event(api, world, "order.completed", body, signature=_sign(raw, secret + "x"))
    assert response.status_code == 401, response.text
    assert response.json()["code"] == "EVENT_SIGNATURE_INVALID"


def test_ingest_with_a_missing_signature_is_401(api: TestClient, world: World) -> None:
    _create_store(api, world)
    response = _post_event(api, world, "order.completed", _ingest_body(world))
    assert response.status_code == 401, response.text
    assert response.json()["code"] == "EVENT_SIGNATURE_INVALID"


def test_ingest_without_a_store_skips_verification(api: TestClient, world: World) -> None:
    """No store row → no secret → skip + record (never silently verified)."""
    response = _post_event(api, world, "order.completed", _ingest_body(world))
    assert response.status_code == 201, response.text
    assert response.json()["signature_verified"] is False


# ---------------------------------------------------------------------------
# Worker poll through the real store pipeline
# ---------------------------------------------------------------------------


def test_worker_poll_routes_an_unmapped_store_event_to_review(
    api: TestClient, world: World
) -> None:
    """The store resolver fails closed (no registry rows) — review, no engine calls.

    The sweep is global (it claims due events across tenants), so the
    counts are lower bounds: a leftover pending event from another
    agent's run on the shared dev DB may ride along. The exact
    assertions are the test's own event status below.
    """
    created = _post_event(api, world, "order.completed", _ingest_body(world))
    assert created.status_code == 201, created.text
    event_id = created.json()["inbox_event_id"]
    response = api.post("/api/v1/integrations/worker/poll", headers=_auth(world.key_full_secret))
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["claimed"] >= 1, report
    assert report["needs_review"] >= 1, report
    fetched = api.get(f"/api/v1/integrations/inbox/{event_id}", headers=_auth(world.key_read_secret))
    assert fetched.json()["status"] == "needs_review"


def _seed_mappings(
    engine: Engine,
    world: World,
    company: CompanyRef,
    customer: tuple[PartyRef, PartyAddressRef],
    payment_journal: PaymentJournalRef,
) -> None:
    """Seed the registry mappings one store order needs."""
    session = sessionmaker(bind=engine)()
    try:
        registry = SqlAlchemyIntegrationsRepository(session)
        party_ref, _address_ref = customer

        def _mapping(kind: MappingKind, channel: str, source: str, ttype: str, tref: str):
            registry.save_mapping(
                CreateIntegrationMapping(
                    installation_id=world.installation_id,
                    mapping_kind=kind,
                    channel_key=channel,
                    source_key=source,
                    target_type=ttype,
                    target_ref=tref,
                    version=1,
                )
            )

        _mapping(
            MappingKind.STORE_COMPANY, "", f"{world.source_system}:store-1",
            "company", str(company.engine_id),
        )
        _mapping(
            MappingKind.EXTERNAL_PARTY, "", CUSTOMER_ID,
            "party", str(party_ref.engine_id),
        )
        _mapping(
            MappingKind.REVENUE_CATEGORY, "", "electronics",
            "account", REVENUE_ACCOUNT_CODE,
        )
        _mapping(
            MappingKind.REVENUE_CATEGORY, "", "default",
            "account", REVENUE_ACCOUNT_CODE,
        )
        _mapping(
            MappingKind.SHIPPING_CHARGE, "", "default",
            "account", SHIPPING_ACCOUNT_CODE,
        )
        _mapping(
            MappingKind.GATEWAY_JOURNAL, "zarinpal", "zarinpal",
            "journal", str(payment_journal.engine_id),
        )
        session.commit()
    finally:
        session.close()


def _order_payload(order_id: str) -> dict:
    """A valid order.completed payload (Toman): 2×10000 + 1×5000,
    shipping 3000, discount 1000, tax 500, total 27500."""
    return {
        "order_id": order_id,
        "customer": {"id": CUSTOMER_ID, "email": "a@b.ir"},
        "lines": [
            {
                "product_sku": "SKU-1",
                "quantity": 2,
                "unit_price": 10000,
                "line_total": 20000,
                "revenue_category": "electronics",
            },
            {"product_sku": "SKU-2", "quantity": 1, "unit_price": 5000},
        ],
        "subtotal_amount": 25000,
        "shipping_amount": 3000,
        "discount_amount": 1000,
        "tax_amount": 500,
        "total_amount": 27500,
        "status": "completed",
    }


def _audit_details(api: TestClient, world: World, event_id: str) -> list[dict]:
    """Audit-log entries for one event (the review reason lives here)."""
    response = api.get(
        f"/api/v1/integrations/audit-log?inbox_event_id={event_id}",
        headers=_auth(world.key_read_secret),
    )
    assert response.status_code == 200, response.text
    return response.json()["items"]


def test_worker_poll_with_configured_mappings_creates_an_invoice_draft(
    api: TestClient,
    engine: Engine,
    world: World,
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    accounts,
    customer: tuple[PartyRef, PartyAddressRef],
    payment_journal: PaymentJournalRef,
    tryton_client: TrytonRestClient,
) -> None:
    """The coordinator's check: an order with configured mappings
    produces a Sale/invoice draft end to end through the PRODUCTION
    wiring — HTTP ingest → worker poll → the registry-backed
    resolver + gate → a draft invoice in Tryton. No auto-post config
    exists, so the registry gate denies auto-post and the invoice
    stays a draft.
    """
    _seed_mappings(engine, world, company, customer, payment_journal)
    order_id = f"order-rest-511-{uuid.uuid4().hex[:8]}"
    body = _ingest_body(
        world,
        source_event_id=f"evt-{order_id}",
        payload=_order_payload(order_id),
        external_entity_type="order",
        external_entity_id=order_id,
    )
    created = _post_event(api, world, "order.completed", body)
    assert created.status_code == 201, created.text
    event_id = created.json()["inbox_event_id"]

    response = api.post("/api/v1/integrations/worker/poll", headers=_auth(world.key_full_secret))
    assert response.status_code == 200, response.text
    report = response.json()
    # The sweep is global — the counts are lower bounds (see the
    # unmapped test). The exact assertions are the event's own
    # status and the invoice draft below; the audit trail is
    # attached so a needs_review outcome shows its reason.
    assert report["claimed"] >= 1, report
    assert report["processed_draft"] >= 1, (
        report,
        _audit_details(api, world, event_id),
    )
    fetched = api.get(f"/api/v1/integrations/inbox/{event_id}", headers=_auth(world.key_read_secret))
    # The stored inbox status is "processed" (the draft-vs-posted
    # distinction lives in the worker report's processed_draft /
    # processed_posted counts, asserted above).
    assert fetched.json()["status"] == "processed"

    reference = f"{world.source_system}:{order_id}"
    matches = [
        view for view in adapter.list_invoices(company) if view.reference == reference
    ]
    assert len(matches) == 1, f"expected one invoice with reference {reference!r}"
    view = matches[0]
    assert view.state == "draft"
    assert view.invoice_type == "out"
    assert view.total_amount.amount == 280000  # products + shipping (Toman ×10)
    assert len(view.lines) == 3
    try:
        tryton_client.delete(
            "account.invoice", view.ref.engine_id, context={"company": company.engine_id}
        )
    except Exception:
        pass  # best-effort cleanup
