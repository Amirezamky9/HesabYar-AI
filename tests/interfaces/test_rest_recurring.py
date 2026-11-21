"""Tests for the recurring-template REST endpoints (US-563).

Everything is real — the FastAPI app, scoped API-key auth, the
PostgreSQL database (templates + occurrences + idempotency records
+ audit events), and the company-resolution dependencies. No engine
is needed: the template service never calls the engine (generation
runs in the worker). Occurrence rows for the list endpoint are
inserted directly (the generator is covered by unit tests).

Provisioned rows are committed (the app's per-request sessions must
see them) and removed in teardown by deleting the tenant (cascades
to clients/keys/companies) plus the recurring rows by tenant id;
idempotency records carry no tenant FK, so they are deleted by
installation id.
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

from hesabyar.infrastructure.auth import ApiKeyCrypto, ApiKeyService
from hesabyar.infrastructure.config import AppSettings
from hesabyar.infrastructure.persistence.postgres.models import (
    ApiClientModel,
    Base,
    EngineCompanyModel,
    EngineConnectionModel,
    IdempotencyRecordModel,
    RecurringOccurrenceModel,
    RecurringTemplateModel,
    TenantModel,
)
from hesabyar.interfaces.rest.app import create_app

TEST_DB_URL = os.getenv(
    "HESABYAR_DATABASE_URL",
    "postgresql+psycopg://hesabyar:hesabyar_pass@hesabyar-postgres:5432/hesabyar_test",
)

INSTALLATION_ID = "00000000-0000-0000-0000-000000000563"

ENGINE_A = 101
ENGINE_B = 102
PARTY_ID = 201

FULL_SCOPES = [
    "invoice:read",
    "invoice:draft:create",
    "ledger:read",
    "ledger:draft:create",
]
INVOICE_SCOPES = ["invoice:read", "invoice:draft:create"]
JOURNAL_SCOPES = ["ledger:read", "ledger:draft:create"]
INVOICE_READ_ONLY = ["invoice:read"]
NO_RECURRING_SCOPES = ["banking:read"]


@dataclass
class World:
    tenant_id: str
    company_a_id: str
    company_b_id: str
    client_id: str
    key_full_secret: str
    key_invoice_secret: str
    key_journal_secret: str
    key_invoice_read_secret: str
    key_none_secret: str
    key_restricted_a_secret: str


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
    engine: Engine, settings: AppSettings
) -> Generator[World, None, None]:
    """Provision a tenant mapped to fake engine companies 101 + 102."""
    session = sessionmaker(bind=engine)()
    service = ApiKeyService(ApiKeyCrypto(settings.auth.api_key_pepper))
    tenant_id = ""
    connection_ids: list[str] = []
    try:
        tenant = TenantModel(
            id=str(uuid.uuid4()), slug=f"rest-563-{uuid.uuid4().hex[:8]}", name="563 Tenant"
        )
        session.add(tenant)
        session.flush()
        client = ApiClientModel(
            id=str(uuid.uuid4()), tenant_id=tenant.id, name="rest-563 client"
        )
        session.add(client)
        session.flush()

        def _company(external_id: str) -> EngineCompanyModel:
            # One company per (tenant, connection) — unique constraint.
            connection = EngineConnectionModel(
                id=str(uuid.uuid4()),
                name=f"conn-{uuid.uuid4().hex[:8]}",
                engine="tryton",
                base_url="http://localhost:8000",
                database_name=f"hesabyar_tryton_test_{uuid.uuid4().hex[:8]}",
            )
            session.add(connection)
            session.flush()
            connection_ids.append(connection.id)
            row = EngineCompanyModel(
                id=str(uuid.uuid4()),
                tenant_id=tenant.id,
                engine_connection_id=connection.id,
                external_company_id=external_id,
                company_name=f"Company {external_id}",
            )
            session.add(row)
            session.flush()
            return row

        row_a = _company(str(ENGINE_A))
        row_b = _company(str(ENGINE_B))

        def _key(scopes: list[str], company_id: str | None = None):
            return service.issue_key(
                session,
                client=client,
                name="rest-563 key",
                scopes=scopes,
                installation_id=INSTALLATION_ID,
                engine_company_id=company_id,
            )

        key_full = _key(FULL_SCOPES)
        key_invoice = _key(INVOICE_SCOPES)
        key_journal = _key(JOURNAL_SCOPES)
        key_invoice_read = _key(INVOICE_READ_ONLY)
        key_none = _key(NO_RECURRING_SCOPES)
        key_restricted_a = _key(FULL_SCOPES, company_id=row_a.id)
        session.commit()
        tenant_id = tenant.id
        yield World(
            tenant_id=tenant.id,
            company_a_id=row_a.id,
            company_b_id=row_b.id,
            client_id=client.id,
            key_full_secret=key_full.secret,
            key_invoice_secret=key_invoice.secret,
            key_journal_secret=key_journal.secret,
            key_invoice_read_secret=key_invoice_read.secret,
            key_none_secret=key_none.secret,
            key_restricted_a_secret=key_restricted_a.secret,
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
                template_ids = [
                    row[0]
                    for row in cleanup.query(RecurringTemplateModel.id)
                    .filter(RecurringTemplateModel.tenant_id == tenant_id)
                    .all()
                ]
                if template_ids:
                    cleanup.execute(
                        delete(RecurringOccurrenceModel).where(
                            RecurringOccurrenceModel.template_id.in_(template_ids)
                        )
                    )
                    cleanup.execute(
                        delete(RecurringTemplateModel).where(
                            RecurringTemplateModel.id.in_(template_ids)
                        )
                    )
                cleanup.query(TenantModel).filter(
                    TenantModel.id == tenant_id
                ).delete(synchronize_session=False)
            for connection_id in connection_ids:
                cleanup.query(EngineConnectionModel).filter(
                    EngineConnectionModel.id == connection_id
                ).delete(synchronize_session=False)
            cleanup.commit()
        finally:
            cleanup.close()


@pytest.fixture()
def api(settings: AppSettings) -> Generator[TestClient, None, None]:
    """The real app (the template service needs no engine override)."""
    app = create_app(settings=settings)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def _auth(secret: str, key: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {secret}"}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _invoice_body(world: World, **overrides) -> dict:
    body = {
        "company_id": world.company_a_id,
        "kind": "invoice",
        "name": "اشتراک ماهانه",
        "schedule": {"kind": "monthly", "jalali_day": 15, "start_date": "2026-10-23"},
        "auto_post": False,
        "invoice": {
            "invoice_type": "out",
            "party_id": PARTY_ID,
            "description": "صورت‌حساب ماهانه",
            "lines": [
                {
                    "account_id": 11,
                    "unit_price": 500_000,
                    "quantity": "1",
                    "description": "اشتراک ماهانه",
                }
            ],
        },
    }
    body.update(overrides)
    return body


def _journal_body(world: World, **overrides) -> dict:
    body = {
        "company_id": world.company_a_id,
        "kind": "journal",
        "name": "استهلاک ماهانه",
        "schedule": {"kind": "monthly", "jalali_day": 1, "start_date": "2026-10-23"},
        "auto_post": False,
        "journal": {
            "description": "استهلاک ماهانه",
            "lines": [
                {"account_id": 21, "debit": 1_000_000},
                {"account_id": 22, "credit": 1_000_000},
            ],
        },
    }
    body.update(overrides)
    return body


def _create(
    api: TestClient, world: World, body: dict, secret: str | None = None
) -> dict:
    response = api.post(
        "/api/v1/recurring-templates",
        json=body,
        headers=_auth(
            secret or world.key_full_secret, key=f"key-{uuid.uuid4().hex[:12]}"
        ),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _insert_occurrence(
    engine: Engine,
    *,
    template_id: str,
    number: int,
    scheduled_date: datetime.date,
    jalali_date: str,
    clamped: bool = False,
    status: str = "draft",
    total_amount_irr: int = 500_000,
    engine_document_id: int | None = 42,
) -> str:
    """Insert one occurrence row directly (the generator is unit-tested)."""
    session = sessionmaker(bind=engine)()
    try:
        row = RecurringOccurrenceModel(
            id=str(uuid.uuid4()),
            template_id=template_id,
            occurrence_number=number,
            scheduled_date=scheduled_date,
            jalali_date=jalali_date,
            clamped=clamped,
            requested_jalali_day=15,
            actual_jalali_day=15,
            status=status,
            total_amount_irr=total_amount_irr,
            snapshot={
                "kind": "invoice",
                "schedule": {
                    "kind": "monthly",
                    "jalali_day": 15,
                    "start_date": "2026-10-23",
                },
                "payload": {
                    "invoice_type": "out",
                    "party_engine_id": PARTY_ID,
                    "lines": [
                        {
                            "account_engine_id": 11,
                            "unit_price_irr": 500_000,
                            "quantity": "1",
                            "description": "اشتراک ماهانه",
                        }
                    ],
                    "description": "صورت‌حساب ماهانه",
                    "reference": "",
                },
                "total_amount_irr": total_amount_irr,
            },
            engine_document_id=engine_document_id,
            generated_at=datetime.datetime(2026, 11, 21, 7, 30, tzinfo=datetime.UTC),
        )
        session.add(row)
        session.commit()
        return row.id
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


def test_create_invoice_template(api: TestClient, world: World) -> None:
    response = api.post(
        "/api/v1/recurring-templates",
        json=_invoice_body(world),
        headers=_auth(world.key_full_secret, key="create-inv-1"),
    )
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["kind"] == "invoice"
    assert data["status"] == "active"
    assert data["name"] == "اشتراک ماهانه"
    assert data["company_id"] == world.company_a_id
    assert data["engine_company_id"] == ENGINE_A
    assert data["total_amount_irr"] == 500_000
    assert data["unit"] == "rial"
    assert data["auto_post"] is False
    assert data["schedule"]["kind"] == "monthly"
    assert data["schedule"]["jalali_day"] == 15
    assert data["schedule"]["start_date"] == "2026-10-23"
    assert data["invoice"]["invoice_type"] == "out"
    assert data["invoice"]["party_id"] == PARTY_ID
    assert data["invoice"]["lines"][0]["amount"] == 500_000
    assert data["journal"] is None


def test_create_journal_template(api: TestClient, world: World) -> None:
    response = api.post(
        "/api/v1/recurring-templates",
        json=_journal_body(world),
        headers=_auth(world.key_full_secret, key="create-jrnl-1"),
    )
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["kind"] == "journal"
    assert data["total_amount_irr"] == 1_000_000
    assert data["journal"]["lines"][0]["debit"] == 1_000_000
    assert data["invoice"] is None


def test_create_requires_idempotency_key(api: TestClient, world: World) -> None:
    response = api.post(
        "/api/v1/recurring-templates",
        json=_invoice_body(world),
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 400
    assert response.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_create_replays_same_key_same_payload(api: TestClient, world: World) -> None:
    body = _invoice_body(world)
    headers = _auth(world.key_full_secret, key="create-replay-1")
    first = api.post("/api/v1/recurring-templates", json=body, headers=headers)
    second = api.post("/api/v1/recurring-templates", json=body, headers=headers)
    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["id"] == first.json()["id"]
    listing = api.get(
        "/api/v1/recurring-templates", headers=_auth(world.key_full_secret)
    )
    assert len(listing.json()["items"]) == 1


def test_create_same_key_different_payload_conflicts(
    api: TestClient, world: World
) -> None:
    headers = _auth(world.key_full_secret, key="create-conflict-1")
    first = api.post(
        "/api/v1/recurring-templates", json=_invoice_body(world), headers=headers
    )
    assert first.status_code == 201
    second = api.post(
        "/api/v1/recurring-templates",
        json=_invoice_body(world, name="نام دیگر"),
        headers=headers,
    )
    assert second.status_code == 409
    assert second.json()["code"] == "IDEMPOTENCY_CONFLICT"


def test_create_rejects_wrong_scope(api: TestClient, world: World) -> None:
    response = api.post(
        "/api/v1/recurring-templates",
        json=_invoice_body(world),
        headers=_auth(world.key_journal_secret, key="create-scope-1"),
    )
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"
    response = api.post(
        "/api/v1/recurring-templates",
        json=_journal_body(world),
        headers=_auth(world.key_invoice_secret, key="create-scope-2"),
    )
    assert response.status_code == 403


def test_create_rejects_mixed_payload(api: TestClient, world: World) -> None:
    body = _invoice_body(world)
    body["journal"] = _journal_body(world)["journal"]
    response = api.post(
        "/api/v1/recurring-templates",
        json=body,
        headers=_auth(world.key_full_secret, key="create-mixed-1"),
    )
    assert response.status_code == 422


def test_create_rejects_unbalanced_journal(api: TestClient, world: World) -> None:
    body = _journal_body(world)
    body["journal"]["lines"][1]["credit"] = 999_999
    response = api.post(
        "/api/v1/recurring-templates",
        json=body,
        headers=_auth(world.key_full_secret, key="create-unbal-1"),
    )
    assert response.status_code == 400
    assert response.json()["code"] == "LEDGER_UNBALANCED"


def test_create_rejects_unknown_schedule_kind(api: TestClient, world: World) -> None:
    body = _invoice_body(world)
    body["schedule"] = {
        "kind": "weekly",
        "jalali_day": 15,
        "start_date": "2026-10-23",
    }
    response = api.post(
        "/api/v1/recurring-templates",
        json=body,
        headers=_auth(world.key_full_secret, key="create-badkind-1"),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "RECURRING_TEMPLATE_INVALID"


def test_create_rejects_yearly_without_month(api: TestClient, world: World) -> None:
    body = _invoice_body(world)
    body["schedule"] = {
        "kind": "yearly",
        "jalali_day": 1,
        "start_date": "2026-10-23",
    }
    response = api.post(
        "/api/v1/recurring-templates",
        json=body,
        headers=_auth(world.key_full_secret, key="create-badyr-1"),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "RECURRING_TEMPLATE_INVALID"


# ---------------------------------------------------------------------------
# Get / list
# ---------------------------------------------------------------------------


def test_get_template(api: TestClient, world: World) -> None:
    created = _create(api, world, _invoice_body(world))
    response = api.get(
        f"/api/v1/recurring-templates/{created['id']}",
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 200
    assert response.json()["id"] == created["id"]


def test_get_unknown_template_is_404(api: TestClient, world: World) -> None:
    response = api.get(
        f"/api/v1/recurring-templates/{uuid.uuid4()}",
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "RECURRING_TEMPLATE_NOT_FOUND"


def test_get_template_rejects_wrong_scope(api: TestClient, world: World) -> None:
    created = _create(api, world, _invoice_body(world))
    response = api.get(
        f"/api/v1/recurring-templates/{created['id']}",
        headers=_auth(world.key_journal_secret),
    )
    assert response.status_code == 403


def test_list_templates_filters_by_readable_kind(
    api: TestClient, world: World
) -> None:
    _create(api, world, _invoice_body(world))
    _create(api, world, _journal_body(world))
    full = api.get(
        "/api/v1/recurring-templates", headers=_auth(world.key_full_secret)
    )
    assert len(full.json()["items"]) == 2
    invoice_only = api.get(
        "/api/v1/recurring-templates", headers=_auth(world.key_invoice_secret)
    )
    items = invoice_only.json()["items"]
    assert len(items) == 1
    assert items[0]["kind"] == "invoice"
    journal_only = api.get(
        "/api/v1/recurring-templates", headers=_auth(world.key_journal_secret)
    )
    items = journal_only.json()["items"]
    assert len(items) == 1
    assert items[0]["kind"] == "journal"


def test_list_templates_requires_a_read_scope(api: TestClient, world: World) -> None:
    response = api.get(
        "/api/v1/recurring-templates", headers=_auth(world.key_none_secret)
    )
    assert response.status_code == 403


def test_list_templates_kind_and_company_filters(
    api: TestClient, world: World
) -> None:
    _create(api, world, _invoice_body(world))
    _create(api, world, _journal_body(world))
    _create(api, world, _invoice_body(world, company_id=world.company_b_id))
    kind = api.get(
        "/api/v1/recurring-templates?kind=journal",
        headers=_auth(world.key_full_secret),
    )
    assert [item["kind"] for item in kind.json()["items"]] == ["journal"]
    company = api.get(
        f"/api/v1/recurring-templates?company={world.company_b_id}",
        headers=_auth(world.key_full_secret),
    )
    items = company.json()["items"]
    assert len(items) == 1
    assert items[0]["company_id"] == world.company_b_id
    # A restricted key is forced to its own company.
    restricted = api.get(
        "/api/v1/recurring-templates",
        headers=_auth(world.key_restricted_a_secret),
    )
    items = restricted.json()["items"]
    assert len(items) == 2
    assert {item["company_id"] for item in items} == {world.company_a_id}


def test_list_templates_rejects_unknown_kind(api: TestClient, world: World) -> None:
    response = api.get(
        "/api/v1/recurring-templates?kind=weekly",
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_FILTER"


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------


def test_preview_unsaved_shows_clamps_and_saves_nothing(
    api: TestClient, world: World
) -> None:
    body = _invoice_body(world)
    body["schedule"] = {
        "kind": "monthly",
        "jalali_day": 31,
        "start_date": "2026-10-23",
    }
    response = api.post(
        "/api/v1/recurring-templates/preview?count=3",
        json=body,
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 200, response.text
    rows = response.json()
    assert [(row["scheduled_date"], row["jalali_date"], row["clamped"]) for row in rows] == [
        ("2026-11-21", "1405-08-30", True),
        ("2026-12-21", "1405-09-30", True),
        ("2027-01-20", "1405-10-30", True),
    ]
    assert all(row["total_amount_irr"] == 500_000 for row in rows)
    assert all(row["requested_jalali_day"] == 31 for row in rows)
    # Nothing was saved.
    listing = api.get(
        "/api/v1/recurring-templates", headers=_auth(world.key_full_secret)
    )
    assert listing.json()["items"] == []


def test_preview_unsaved_requires_write_scope(api: TestClient, world: World) -> None:
    response = api.post(
        "/api/v1/recurring-templates/preview",
        json=_invoice_body(world),
        headers=_auth(world.key_invoice_read_secret),
    )
    assert response.status_code == 403


def test_preview_existing_template(api: TestClient, world: World) -> None:
    body = _invoice_body(world)
    body["schedule"] = {
        "kind": "yearly",
        "jalali_day": 1,
        "jalali_month": 1,
        "start_date": "2030-03-21",
    }
    created = _create(api, world, body)
    response = api.get(
        f"/api/v1/recurring-templates/{created['id']}/preview?count=2",
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 200, response.text
    rows = response.json()
    assert [(row["occurrence_number"], row["scheduled_date"], row["jalali_date"]) for row in rows] == [
        (1, "2030-03-21", "1409-01-01"),
        (2, "2031-03-21", "1410-01-01"),
    ]
    assert all(not row["clamped"] for row in rows)
    assert all(row["total_amount_irr"] == 500_000 for row in rows)


# ---------------------------------------------------------------------------
# Update / pause / resume / delete
# ---------------------------------------------------------------------------


def test_update_template(api: TestClient, world: World) -> None:
    created = _create(api, world, _invoice_body(world))
    response = api.put(
        f"/api/v1/recurring-templates/{created['id']}",
        json={"name": "نام جدید", "auto_post": True},
        headers=_auth(world.key_full_secret, key="update-1"),
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["name"] == "نام جدید"
    assert data["auto_post"] is True
    assert data["total_amount_irr"] == 500_000


def test_update_requires_a_field(api: TestClient, world: World) -> None:
    created = _create(api, world, _invoice_body(world))
    response = api.put(
        f"/api/v1/recurring-templates/{created['id']}",
        json={},
        headers=_auth(world.key_full_secret, key="update-empty-1"),
    )
    assert response.status_code == 422


def test_update_requires_idempotency_key(api: TestClient, world: World) -> None:
    created = _create(api, world, _invoice_body(world))
    response = api.put(
        f"/api/v1/recurring-templates/{created['id']}",
        json={"name": "نام جدید"},
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 400
    assert response.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_update_rejects_wrong_kind_payload(api: TestClient, world: World) -> None:
    created = _create(api, world, _invoice_body(world))
    response = api.put(
        f"/api/v1/recurring-templates/{created['id']}",
        json={"journal": _journal_body(world)["journal"]},
        headers=_auth(world.key_full_secret, key="update-wrongkind-1"),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "RECURRING_TEMPLATE_INVALID"


def test_pause_and_resume(api: TestClient, world: World) -> None:
    created = _create(api, world, _invoice_body(world))
    url = f"/api/v1/recurring-templates/{created['id']}"
    paused = api.post(f"{url}/pause", headers=_auth(world.key_full_secret))
    assert paused.status_code == 200
    assert paused.json()["status"] == "paused"
    paused_again = api.post(f"{url}/pause", headers=_auth(world.key_full_secret))
    assert paused_again.status_code == 200
    assert paused_again.json()["status"] == "paused"
    resumed = api.post(f"{url}/resume", headers=_auth(world.key_full_secret))
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "active"


def test_delete_template(api: TestClient, world: World) -> None:
    created = _create(api, world, _invoice_body(world))
    response = api.delete(
        f"/api/v1/recurring-templates/{created['id']}",
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 204
    gone = api.get(
        f"/api/v1/recurring-templates/{created['id']}",
        headers=_auth(world.key_full_secret),
    )
    assert gone.status_code == 404


def test_delete_template_with_occurrences_is_blocked(
    api: TestClient, world: World, engine: Engine
) -> None:
    created = _create(api, world, _invoice_body(world))
    _insert_occurrence(
        engine,
        template_id=created["id"],
        number=1,
        scheduled_date=datetime.date(2026, 11, 6),
        jalali_date="1405-08-15",
    )
    response = api.delete(
        f"/api/v1/recurring-templates/{created['id']}",
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "RECURRING_TEMPLATE_IN_USE"


# ---------------------------------------------------------------------------
# Occurrences
# ---------------------------------------------------------------------------


def test_list_occurrences_empty(api: TestClient, world: World) -> None:
    created = _create(api, world, _invoice_body(world))
    response = api.get(
        f"/api/v1/recurring-templates/{created['id']}/occurrences",
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 200
    assert response.json()["items"] == []


def test_list_occurrences(api: TestClient, world: World, engine: Engine) -> None:
    created = _create(api, world, _invoice_body(world))
    _insert_occurrence(
        engine,
        template_id=created["id"],
        number=1,
        scheduled_date=datetime.date(2026, 11, 6),
        jalali_date="1405-08-15",
    )
    _insert_occurrence(
        engine,
        template_id=created["id"],
        number=2,
        scheduled_date=datetime.date(2026, 12, 6),
        jalali_date="1405-09-15",
        status="posted",
        engine_document_id=43,
    )
    response = api.get(
        f"/api/v1/recurring-templates/{created['id']}/occurrences",
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [item["occurrence_number"] for item in items] == [1, 2]
    assert items[0]["template_id"] == created["id"]
    assert items[0]["scheduled_date"] == "2026-11-06"
    assert items[0]["jalali_date"] == "1405-08-15"
    assert items[0]["status"] == "draft"
    assert items[0]["engine_document_id"] == 42
    assert items[0]["total_amount_irr"] == 500_000
    assert items[1]["status"] == "posted"
    assert items[1]["engine_document_id"] == 43


def test_list_occurrences_unknown_template_is_404(
    api: TestClient, world: World
) -> None:
    response = api.get(
        f"/api/v1/recurring-templates/{uuid.uuid4()}/occurrences",
        headers=_auth(world.key_full_secret),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "RECURRING_TEMPLATE_NOT_FOUND"


def test_list_occurrences_rejects_wrong_scope(api: TestClient, world: World) -> None:
    created = _create(api, world, _invoice_body(world))
    response = api.get(
        f"/api/v1/recurring-templates/{created['id']}/occurrences",
        headers=_auth(world.key_journal_secret),
    )
    assert response.status_code == 403
