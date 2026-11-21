"""REST load + concurrency tests (US-702, spec §17 phase 7 + §19.9).

The real FastAPI app + scoped API-key auth + PostgreSQL. No live
services except where a test is explicitly Tryton-gated (the
dashboard core batch reads through the engine; those tests skip
cleanly when trytond is unreachable, like the other Tryton-gated
REST tests).

Covered here:

- webhook ingestion burst: 1,000 distinct events, sequential and
  10-way concurrent — every event created exactly once, no 500s;
- duplicate concurrent ingest: 20 threads posting the SAME event —
  one 201, nineteen 200s, one inbox row, one idempotency record
  (§19.9: no duplicate posting from repeated external events);
- the dashboard read set under concurrent users: the Postgres-only
  reads the dashboard home issues on load (review-queue summary,
  cheques, inbox, settlement tranches, import batches) return
  identical results for every concurrent user;
- the dashboard core batch (drafts, invoices, payments, cash
  boxes) under concurrent users — Tryton-gated;
- concurrent cheque registration over HTTP with the same Sayyad
  number: one cheque, the losers get 409 (or the same cheque back),
  never a 500.

Latencies are recorded for ``docs/performance-baselines.md`` and
never asserted; the only time bound is a generous safety net on the
whole burst. Each test gets a FRESH installation id, so rows never
leak between tests; teardown deletes the installation's rows and
the provisioned tenant (which cascades to inbox events).
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections.abc import Generator
from dataclasses import dataclass
from typing import Any, cast

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from hesabyar.application.commands.cheques import ChequeCommandService
from hesabyar.domain.accounting.commands import CreateEngineCheque
from hesabyar.domain.accounting.ports import AccountingEnginePort
from hesabyar.domain.accounting.refs import ChequeEngineRef, CompanyRef
from hesabyar.domain.accounting.value_objects import ChequeEngineView
from hesabyar.domain.cheques.models import ChequeState
from hesabyar.infrastructure.accounting.tryton.client import TrytonRestClient
from hesabyar.infrastructure.auth import ApiKeyCrypto, ApiKeyService
from hesabyar.infrastructure.auth.credential_vault import generate_encryption_key
from hesabyar.infrastructure.config import AppSettings, AuthSettings
from hesabyar.infrastructure.persistence.postgres.cheques_repository import (
    SqlAlchemyChequeRepository,
)
from hesabyar.infrastructure.persistence.postgres.event_repositories import (
    SqlAlchemyEventInboxRepository,
)
from hesabyar.infrastructure.persistence.postgres.models import (
    ApiClientModel,
    AutoPostPolicyModel,
    Base,
    ChequeModel,
    EngineCompanyModel,
    EngineConnectionModel,
    IdempotencyRecordModel,
    InstallationSettingsModel,
    IntegrationMappingModel,
    StoreModel,
    TenantModel,
)
from hesabyar.interfaces.rest.app import create_app
from hesabyar.interfaces.rest.dependencies import (
    get_cheque_command_service,
    get_db_session,
)
from tests.event_fixtures import raw_envelope
from tests.load_support import LatencyRecorder, run_threads

TEST_DB_URL = os.getenv(
    "HESABYAR_DATABASE_URL",
    "postgresql+psycopg://hesabyar:hesabyar_pass@hesabyar-postgres:5432/hesabyar_test",
)
TRYTON_BASE_URL = os.environ.get("TRYTON_BASE_URL", "http://localhost:8000")
TRYTON_DATABASE = os.environ.get("TRYTON_DATABASE", "hesabyar_tryton_test")
TRYTON_API_KEY = os.environ.get("TRYTON_API_KEY", "hesabyar-dev-rest-key")
TEST_COMPANY_NAME = os.environ.get("TRYTON_TEST_COMPANY", "HesabYar Adapter Test")

ALL_SCOPES = [
    "integrations:manage",
    "integrations:read",
    "review:read",
    "cheques:read",
    "cheques:write",
    "settlement:read",
    "ledger:read",
    "invoice:read",
    "payment:read",
    "cashbox:read",
]

#: The webhook burst size (AC 1) + the safety net for the whole burst.
#: The net is ~10x the expected time: it catches a hung run, it is
#: not a performance claim.
BURST_SIZE = 1000
BURST_SAFETY_NET_SECONDS = 600.0

#: A Sayyad id that is not a real leaf (16 digits, unique per test).
FAKE_COMPANY_ENGINE_ID = 2


@dataclass
class World:
    """Provisioned tenant, company mapping and key for one test."""

    installation_id: str
    tenant_id: str
    company_row_id: str
    key_secret: str
    source_system: str


@pytest.fixture(scope="module")
def engine() -> Generator[Engine, None, None]:
    eng = create_engine(TEST_DB_URL, echo=False)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def test_company() -> CompanyRef:
    """Resolve the live Adapter Test company (skip when Tryton is down)."""
    client = TrytonRestClient(TRYTON_BASE_URL, TRYTON_DATABASE, TRYTON_API_KEY)
    try:
        if not client.ping():
            pytest.skip(f"trytond server not reachable at {TRYTON_BASE_URL}")
        for record in client.search("company.company", fields=["rec_name"]):
            if record["rec_name"] == TEST_COMPANY_NAME:
                return CompanyRef(engine_id=int(record["id"]))
    finally:
        client.close()
    pytest.skip(f"Test company {TEST_COMPANY_NAME!r} not found in Tryton DB {TRYTON_DATABASE!r}.")


@pytest.fixture()
def settings() -> AppSettings:
    """Fresh settings with a test-generated vault key (dev pepper)."""
    return AppSettings(
        auth=AuthSettings(credentials_encryption_key=generate_encryption_key())
    )


def _provision(
    engine: Engine, settings: AppSettings, *, engine_id: int
) -> tuple[World, list[str], list[str]]:
    """Provision a tenant + key + company row; return (world, tenant ids, connection ids)."""
    session = sessionmaker(bind=engine)()
    service = ApiKeyService(ApiKeyCrypto(settings.auth.api_key_pepper))
    installation_id = str(uuid.uuid4())
    source_system = f"evt-rest-702-{uuid.uuid4().hex[:8]}"
    tenant = TenantModel(
        id=str(uuid.uuid4()), slug=f"rest-702-{uuid.uuid4().hex[:8]}", name="702 Tenant"
    )
    session.add(tenant)
    session.flush()
    client = ApiClientModel(id=str(uuid.uuid4()), tenant_id=tenant.id, name="rest-702 client")
    session.add(client)
    session.flush()
    key = service.issue_key(
        session,
        client=client,
        name="rest-702 key",
        scopes=ALL_SCOPES,
        installation_id=installation_id,
    )
    connection = EngineConnectionModel(
        id=str(uuid.uuid4()),
        name=f"conn-{uuid.uuid4().hex[:8]}",
        engine="tryton",
        base_url=TRYTON_BASE_URL,
        database_name=f"hesabyar_tryton_test_{uuid.uuid4().hex[:8]}",
    )
    session.add(connection)
    session.flush()
    row = EngineCompanyModel(
        id=str(uuid.uuid4()),
        tenant_id=tenant.id,
        engine_connection_id=connection.id,
        external_company_id=str(engine_id),
        company_name=TEST_COMPANY_NAME,
    )
    session.add(row)
    session.flush()
    session.commit()
    session.close()
    return (
        World(
            installation_id=installation_id,
            tenant_id=tenant.id,
            company_row_id=row.id,
            key_secret=key.secret,
            source_system=source_system,
        ),
        [tenant.id],
        [connection.id],
    )


def _cleanup(engine: Engine, world: World, tenant_ids: list[str], connection_ids: list[str]) -> None:
    cleanup = sessionmaker(bind=engine)()
    try:
        cleanup.execute(
            delete(IdempotencyRecordModel).where(
                IdempotencyRecordModel.installation_id == world.installation_id
            )
        )
        cleanup.execute(
            delete(StoreModel).where(StoreModel.installation_id == world.installation_id)
        )
        cleanup.execute(
            delete(IntegrationMappingModel).where(
                IntegrationMappingModel.installation_id == world.installation_id
            )
        )
        cleanup.execute(
            delete(AutoPostPolicyModel).where(
                AutoPostPolicyModel.installation_id == world.installation_id
            )
        )
        cleanup.execute(
            delete(InstallationSettingsModel).where(
                InstallationSettingsModel.installation_id == world.installation_id
            )
        )
        cleanup.execute(
            delete(ChequeModel).where(ChequeModel.tenant_id == world.tenant_id)
        )
        if tenant_ids:
            cleanup.query(TenantModel).filter(TenantModel.id.in_(tenant_ids)).delete(
                synchronize_session=False
            )
        if connection_ids:
            cleanup.query(EngineConnectionModel).filter(
                EngineConnectionModel.id.in_(connection_ids)
            ).delete(synchronize_session=False)
        cleanup.commit()
    finally:
        cleanup.close()


@pytest.fixture()
def world(
    engine: Engine, settings: AppSettings
) -> Generator[World, None, None]:
    """Provision a tenant + key; the company row points at a fake engine id.

    Enough for the Postgres-only tests; the Tryton-gated test uses
    ``tryton_world`` instead (a company the engine really knows).
    """
    provisioned, tenant_ids, connection_ids = _provision(
        engine, settings, engine_id=FAKE_COMPANY_ENGINE_ID
    )
    yield provisioned
    _cleanup(engine, provisioned, tenant_ids, connection_ids)


@pytest.fixture()
def tryton_world(
    engine: Engine, settings: AppSettings, test_company: CompanyRef
) -> Generator[World, None, None]:
    """Like ``world`` but the company row points at the live test company."""
    provisioned, tenant_ids, connection_ids = _provision(
        engine, settings, engine_id=test_company.engine_id
    )
    yield provisioned
    _cleanup(engine, provisioned, tenant_ids, connection_ids)


@pytest.fixture()
def app(settings: AppSettings):
    return create_app(settings=settings)


def _auth(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


def _ingest_body(world: World, source_event_id: str) -> dict:
    return raw_envelope(
        source_system=world.source_system,
        source_event_id=source_event_id,
        payload={"total_amount": 175000},
    )


def _post_event(api: TestClient, world: World, body: dict):
    return api.post(
        f"/api/v1/integrations/{world.source_system}/events",
        content=json.dumps(body),
        headers={
            **_auth(world.key_secret),
            "Content-Type": "application/json",
            "X-HesabYar-Event-Type": "order.completed",
        },
    )


def _inbox_count(engine: Engine, world: World) -> int:
    session = sessionmaker(bind=engine)()
    try:
        return SqlAlchemyEventInboxRepository(session).count_events(
            world.tenant_id, source_system=world.source_system
        )
    finally:
        session.close()


def _idempotency_count(engine: Engine, world: World) -> int:
    session = sessionmaker(bind=engine)()
    try:
        return session.query(IdempotencyRecordModel).filter(
            IdempotencyRecordModel.installation_id == world.installation_id,
            IdempotencyRecordModel.operation == "events.ingest",
        ).count()
    finally:
        session.close()


# -- webhook ingestion burst -----------------------------------------------------


def test_webhook_ingestion_burst_1000_events(
    app, engine: Engine, world: World
) -> None:
    """1,000 distinct events, one after another: all created, none lost."""
    api = TestClient(app, raise_server_exceptions=True)
    recorder = LatencyRecorder()
    started = time.monotonic()
    statuses: list[int] = []
    for index in range(BURST_SIZE):
        post_started = time.monotonic()
        response = _post_event(api, world, _ingest_body(world, f"evt-{index:04d}"))
        recorder.record("webhook.ingest", time.monotonic() - post_started)
        statuses.append(response.status_code)
    elapsed = time.monotonic() - started
    assert statuses == [201] * BURST_SIZE
    assert _inbox_count(engine, world) == BURST_SIZE
    assert elapsed < BURST_SAFETY_NET_SECONDS
    print(recorder.report())
    recorder.maybe_write_json()


def test_webhook_ingestion_burst_concurrent(
    app, engine: Engine, world: World
) -> None:
    """1,000 distinct events from 10 concurrent threads: all created once."""
    recorder = LatencyRecorder()
    threads = 10
    per_thread = BURST_SIZE // threads

    def _burst(worker_index: int) -> list[int]:
        api = TestClient(app, raise_server_exceptions=True)
        statuses = []
        for index in range(per_thread):
            event_index = worker_index * per_thread + index
            post_started = time.monotonic()
            response = _post_event(
                api, world, _ingest_body(world, f"evt-{event_index:04d}")
            )
            recorder.record("webhook.ingest.concurrent", time.monotonic() - post_started)
            statuses.append(response.status_code)
        return statuses

    started = time.monotonic()
    results = run_threads(threads, _burst)
    elapsed = time.monotonic() - started
    assert [status for batch in results for status in batch] == [201] * BURST_SIZE
    assert _inbox_count(engine, world) == BURST_SIZE
    assert elapsed < BURST_SAFETY_NET_SECONDS
    print(recorder.report())
    recorder.maybe_write_json()


def test_duplicate_concurrent_events_ingest_exactly_once(
    app, engine: Engine, world: World
) -> None:
    """20 threads posting the SAME event: one 201, nineteen 200s, one row.

    The §19.9 guarantee at the HTTP boundary: a repeated external
    event is stored once and its idempotency record completes once —
    a retry storm can never double-post.
    """
    body = _ingest_body(world, "evt-duplicate-702")

    def _post(_index: int) -> int:
        api = TestClient(app, raise_server_exceptions=True)
        return _post_event(api, world, body).status_code

    statuses = run_threads(20, _post)
    assert statuses.count(201) == 1, statuses
    assert statuses.count(200) == 19, statuses
    assert _inbox_count(engine, world) == 1
    assert _idempotency_count(engine, world) == 1
    # A later sequential replay is still a duplicate, not a conflict.
    api = TestClient(app, raise_server_exceptions=True)
    assert _post_event(api, world, body).status_code == 200
    assert _inbox_count(engine, world) == 1


# -- dashboard read set under concurrent users -----------------------------------


def _fingerprint_list(payload: object) -> tuple[str, ...]:
    """Sorted ids of a list response (order-free: ties order arbitrarily).

    Most list items carry ``id``; inbox items carry ``inbox_event_id``.
    """
    if isinstance(payload, dict):
        items = payload.get("items", [])
    else:
        items = payload
    assert isinstance(items, list)
    ids = [
        str(item.get("id") or item.get("inbox_event_id"))
        for item in items
        if isinstance(item, dict)
    ]
    return tuple(sorted(ids))


def _read_set(api: TestClient, world: World, recorder: LatencyRecorder) -> dict[str, Any]:
    """The dashboard home's Postgres-only reads; returns one fingerprint per read."""
    fingerprints: dict[str, Any] = {}
    reads = [
        ("review_queue.summary", "/api/v1/review-queue/summary"),
        ("cheques.list", "/api/v1/cheques?limit=100"),
        (
            "integrations.inbox",
            f"/api/v1/integrations/inbox?limit=100&source_system={world.source_system}",
        ),
        ("settlements.tranches", "/api/v1/settlements/tranches?limit=100"),
        ("imports.batches", "/api/v1/imports/batches"),
    ]
    for name, path in reads:
        started = time.monotonic()
        response = api.get(path, headers=_auth(world.key_secret))
        recorder.record(f"dashboard.{name}", time.monotonic() - started)
        assert response.status_code == 200, (name, response.status_code, response.text[:500])
        if name == "review_queue.summary":
            # Aggregates: the whole body must agree across users.
            fingerprints[name] = response.json()
        else:
            fingerprints[name] = _fingerprint_list(response.json())
    return fingerprints


def test_dashboard_read_set_concurrent_users(
    app, engine: Engine, world: World
) -> None:
    """10 concurrent users loading the dashboard reads: identical results.

    Fifty inbox events are seeded first so the reads are non-empty;
    every user then issues the full Postgres-only read set, and all
    ten fingerprints must agree exactly — no partial reads, no 500s,
    no cross-user interference.
    """
    api = TestClient(app, raise_server_exceptions=True)
    for index in range(50):
        response = _post_event(api, world, _ingest_body(world, f"evt-seed-{index:02d}"))
        assert response.status_code == 201
    assert _inbox_count(engine, world) == 50

    recorder = LatencyRecorder()

    def _load(_index: int) -> dict[str, Any]:
        user_api = TestClient(app, raise_server_exceptions=True)
        return _read_set(user_api, world, recorder)

    fingerprints = run_threads(10, _load)
    first = fingerprints[0]
    for other in fingerprints[1:]:
        assert other == first
    assert len(first["integrations.inbox"]) == 50
    print(recorder.report())
    recorder.maybe_write_json()


def test_dashboard_core_batch_concurrent_users_tryton(
    app, tryton_world: World
) -> None:
    """5 concurrent users loading the Tryton-backed core batch: identical results.

    Drafts, invoices, payments and cash boxes — the reads the
    dashboard home hard-fails on — each through a fresh engine
    client per request. Skips when trytond is unreachable.
    """
    world = tryton_world
    recorder = LatencyRecorder()
    company = world.company_row_id

    def _load(_index: int) -> dict[str, Any]:
        user_api = TestClient(app, raise_server_exceptions=True)
        fingerprints: dict[str, Any] = {}
        reads = [
            ("journal_drafts", f"/api/v1/accounting/journal-drafts?company={company}"),
            ("invoices", f"/api/v1/invoices?company={company}"),
            ("payments", f"/api/v1/payments?company={company}"),
            ("cash_boxes", f"/api/v1/cash-boxes?company={company}"),
        ]
        for name, path in reads:
            started = time.monotonic()
            response = user_api.get(path, headers=_auth(world.key_secret))
            recorder.record(f"dashboard.{name}", time.monotonic() - started)
            assert response.status_code == 200, (name, response.status_code, response.text[:500])
            fingerprints[name] = _fingerprint_list(response.json())
        return fingerprints

    fingerprints = run_threads(5, _load)
    first = fingerprints[0]
    for other in fingerprints[1:]:
        assert other == first
    print(recorder.report())
    recorder.maybe_write_json()


# -- concurrent cheque registration over HTTP -------------------------------------


class _CountingFakeEngine:
    """Thread-safe fake engine: counts cheque creations, hands out ids."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_engine_id = 1000
        self.created_sayad_ids: list[str] = []

    def create_engine_cheque(self, command: CreateEngineCheque) -> ChequeEngineView:
        with self._lock:
            engine_id = self._next_engine_id
            self._next_engine_id += 1
            self.created_sayad_ids.append(command.sayad_id)
        return ChequeEngineView(
            ref=ChequeEngineRef(engine_id=engine_id),
            company=command.company,
            kind=command.kind,
            purpose=command.purpose,
            sayad_id=command.sayad_id,
            state=ChequeState.REGISTERED.value,
            sayad_status="unregistered",
            amount=command.amount,
            issue_date=command.issue_date,
            due_date=command.due_date,
            move_ids={},
        )


def _cheque_body(world: World, sayad_id: str) -> dict[str, object]:
    return {
        "company_id": world.company_row_id,
        "sayad_id": sayad_id,
        "amount_toman": 1750000,
        "amount_currency": "IRT",
        "issue_date": "1405-08-26",
        "due_date": "1405-10-15",
        "issuer_name": "مشتری نمونه",
        "issuer_national_id": "0012345678",
        "issuer_bank": "056 سامان",
        "payee_name": "کافه ایوان",
        "payee_national_id": "00123456789",
        "purpose": "payment",
        "order_id": "ord-702",
        "customer_ref": "cust-702",
        "sayad_registered": True,
    }


def test_concurrent_cheque_registration_http(
    app, engine: Engine, world: World
) -> None:
    """8 concurrent POSTs of one cheque: one 201, the rest 409 (or the same id).

    The engine is faked (the race is in HesabYar's registry, not in
    Tryton), so this runs in the plain CI job: the override builds a
    per-request service over the request's own session, like the
    production wiring. The losers never see a 500 — the typed
    duplicate error maps to 409, and a loser that ran its pre-check
    after the winner committed gets the existing cheque back.
    """
    sayad_id = str(10**15 + uuid.uuid4().int % (9 * 10**15))
    fake_engine = _CountingFakeEngine()

    def _service(
        session: Session = Depends(get_db_session),
    ) -> ChequeCommandService:
        return ChequeCommandService(
            SqlAlchemyChequeRepository(session),
            cast(AccountingEnginePort, fake_engine),
        )

    app.dependency_overrides[get_cheque_command_service] = _service
    try:
        body = _cheque_body(world, sayad_id)

        def _post(_index: int) -> tuple[int, str | None]:
            api = TestClient(app, raise_server_exceptions=True)
            response = api.post(
                "/api/v1/cheques", json=body, headers=_auth(world.key_secret)
            )
            cheque_id = None
            if response.status_code == 201:
                cheque_id = str(response.json()["id"])
            return response.status_code, cheque_id

        outcomes = run_threads(8, _post)
        statuses = [status for status, _ in outcomes]
        assert statuses.count(201) >= 1, statuses
        assert set(statuses) <= {201, 409}, statuses
        created_ids = {cheque_id for _, cheque_id in outcomes if cheque_id is not None}
        assert len(created_ids) == 1, outcomes
        assert fake_engine.created_sayad_ids == [sayad_id]

        session = sessionmaker(bind=engine)()
        try:
            count = session.query(ChequeModel).filter(
                ChequeModel.sayad_id == sayad_id,
                ChequeModel.state.notin_(
                    ("voided", "expired_unreceived", "rejected_mismatch")
                ),
            ).count()
            assert count == 1
        finally:
            session.close()
    finally:
        app.dependency_overrides.pop(get_cheque_command_service, None)
        session = sessionmaker(bind=engine)()
        try:
            session.execute(delete(ChequeModel).where(ChequeModel.sayad_id == sayad_id))
            session.commit()
        finally:
            session.close()
