"""Tests for the Moadian REST endpoints (US-404).

Everything is real — the FastAPI app, scoped API-key auth, the
PostgreSQL database, the deterministic simulator — except the engine,
which these endpoints never touch. The live Tryton server is NOT
needed here.

Provisioned tenants/keys are removed in teardown by deleting the
tenant (cascades to clients/keys/profiles/documents/submissions).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Generator
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from hesabyar.infrastructure.auth import ApiKeyCrypto, ApiKeyService
from hesabyar.infrastructure.config import AppSettings
from hesabyar.infrastructure.persistence.postgres.models import (
    ApiClientModel,
    Base,
    TenantModel,
)
from hesabyar.interfaces.rest.app import create_app
from tests.moadian_fixtures import taxid_for, valid_invoice_payload

TEST_DB_URL = os.getenv(
    "HESABYAR_DATABASE_URL",
    "postgresql+psycopg://hesabyar:hesabyar_pass@hesabyar-postgres:5432/hesabyar_test",
)

INSTALLATION_ID = "00000000-0000-0000-0000-000000000404"


@dataclass
class World:
    """Provisioned tenant and keys for one test."""

    tenant_id: str
    other_tenant_id: str
    key_read_secret: str
    key_submit_secret: str
    key_company_secret: str
    key_other_read_secret: str


@pytest.fixture(scope="module")
def engine() -> Generator[Engine, None, None]:
    eng = create_engine(TEST_DB_URL, echo=False)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def settings() -> AppSettings:
    """Fresh settings (dev pepper) — the same pepper the app dependency uses."""
    return AppSettings()


@pytest.fixture()
def world(engine: Engine, settings: AppSettings) -> Generator[World, None, None]:
    """Provision two tenants and four keys; clean up after."""
    session = sessionmaker(bind=engine)()
    service = ApiKeyService(ApiKeyCrypto(settings.auth.api_key_pepper))
    tenant_ids: list[str] = []
    try:
        tenant = TenantModel(id=str(uuid.uuid4()), slug=f"rest-404-{uuid.uuid4().hex[:8]}", name="Tenant 1")
        other_tenant = TenantModel(id=str(uuid.uuid4()), slug=f"rest-404-{uuid.uuid4().hex[:8]}", name="Tenant 2")
        session.add_all([tenant, other_tenant])
        session.flush()
        client = ApiClientModel(id=str(uuid.uuid4()), tenant_id=tenant.id, name="rest-404 client")
        other_client = ApiClientModel(id=str(uuid.uuid4()), tenant_id=other_tenant.id, name="rest-404 other client")
        session.add_all([client, other_client])
        session.flush()

        def _key(client_row: ApiClientModel, scopes: list[str]):
            return service.issue_key(
                session,
                client=client_row,
                name="rest-404 key",
                scopes=scopes,
                installation_id=INSTALLATION_ID,
            )

        key_read = _key(client, ["moadian:read"])
        key_submit = _key(client, ["moadian:read", "moadian:submit"])
        key_company = _key(client, ["company:read"])
        key_other_read = _key(other_client, ["moadian:read"])
        session.commit()
        tenant_ids = [tenant.id, other_tenant.id]
        yield World(
            tenant_id=tenant.id,
            other_tenant_id=other_tenant.id,
            key_read_secret=key_read.secret,
            key_submit_secret=key_submit.secret,
            key_company_secret=key_company.secret,
            key_other_read_secret=key_other_read.secret,
        )
    finally:
        session.close()
        cleanup = sessionmaker(bind=engine)()
        try:
            if tenant_ids:
                cleanup.query(TenantModel).filter(TenantModel.id.in_(tenant_ids)).delete(synchronize_session=False)
            cleanup.commit()
        finally:
            cleanup.close()


def _test_client(settings: AppSettings) -> TestClient:
    """Real app (Moadian endpoints need no engine adapter)."""
    app = create_app(settings=settings)
    return TestClient(app, raise_server_exceptions=False)


def _auth(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


def _profile_body(slug: str, **overrides) -> dict:
    body = {
        "name": "Eyvan test fiscal memory",
        "slug": slug,
        "environment": "test",
        "sender_mode": "self-tsp",
        "fiscal_id": "AA56CD",
        "economic_code": "32652362589632",
        "source_authority": "Iranian National Tax Administration (INTA)",
        "source_url": "https://www.intamedia.ir/The-regulation-of-store-terminals",
        "key_reference": "MOADIAN_TEST_PRIVATE_KEY",
    }
    body.update(overrides)
    return body


def _create_profile(client: TestClient, secret: str, slug: str, **overrides) -> dict:
    response = client.post("/api/v1/moadian/profiles", json=_profile_body(slug, **overrides), headers=_auth(secret))
    assert response.status_code == 201, response.text
    return response.json()


def _create_document(client: TestClient, secret: str, profile_id: str, *, n: int) -> dict:
    response = client.post(
        "/api/v1/moadian/documents",
        json={"profile_id": profile_id, "payload": valid_invoice_payload(taxid=taxid_for(n))},
        headers=_auth(secret),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_submission(client: TestClient, secret: str, document_id: str, **overrides) -> dict:
    body = {"document_id": document_id}
    body.update(overrides)
    response = client.post("/api/v1/moadian/submissions", json=body, headers=_auth(secret))
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------------------
# Auth + scopes
# ---------------------------------------------------------------------------


def test_profiles_require_auth(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get("/api/v1/moadian/profiles")
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"


def test_profiles_require_moadian_read_scope(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get("/api/v1/moadian/profiles", headers=_auth(world.key_company_secret))
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


def test_create_profile_requires_moadian_submit_scope(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.post(
        "/api/v1/moadian/profiles",
        json=_profile_body("scope-test"),
        headers=_auth(world.key_read_secret),
    )
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


def test_create_and_get_profile(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    created = _create_profile(client, world.key_submit_secret, "eyvan-test")
    assert created["slug"] == "eyvan-test"
    assert created["environment"] == "test"
    assert created["packet_type"] == "INVOICE.V01"
    assert created["api_base_url"] == "https://tp.tax.gov.ir/req/"
    assert created["protocol_version"] == "1401-07-26"
    assert created["verification_status"] == "UNVERIFIED"
    assert created["is_active"] is True
    assert created["key_reference"] == "MOADIAN_TEST_PRIVATE_KEY"
    assert created["key_fingerprint"] is None
    # No key material anywhere in the response: only the reference.
    assert not any(
        isinstance(value, str) and "BEGIN" in value for value in created.values()
    )
    fetched = client.get(
        f"/api/v1/moadian/profiles/{created['profile_id']}",
        headers=_auth(world.key_read_secret),
    )
    assert fetched.status_code == 200
    assert fetched.json() == created


def test_create_profile_duplicate_slug_conflicts(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    _create_profile(client, world.key_submit_secret, "dup-slug")
    response = client.post(
        "/api/v1/moadian/profiles",
        json=_profile_body("dup-slug"),
        headers=_auth(world.key_submit_secret),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "MOADIAN_PROFILE_SLUG_CONFLICT"


def test_create_profile_rejects_key_material(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.post(
        "/api/v1/moadian/profiles",
        json=_profile_body("pem-slug", key_reference="-----BEGIN PRIVATE KEY-----\nMIIE..."),
        headers=_auth(world.key_submit_secret),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_create_profile_rejects_bad_fiscal_id(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.post(
        "/api/v1/moadian/profiles",
        json=_profile_body("bad-fiscal", fiscal_id="TOOLONG"),
        headers=_auth(world.key_submit_secret),
    )
    assert response.status_code == 422


def test_list_profiles_filters_and_pagination(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    _create_profile(client, world.key_submit_secret, "a-test")
    _create_profile(client, world.key_submit_secret, "b-prod", environment="production")
    _create_profile(client, world.key_submit_secret, "c-test")
    response = client.get("/api/v1/moadian/profiles", headers=_auth(world.key_read_secret))
    assert response.status_code == 200
    body = response.json()
    assert [item["slug"] for item in body["items"]] == ["a-test", "b-prod", "c-test"]
    assert body["next_cursor"] is None
    response = client.get(
        "/api/v1/moadian/profiles?environment=production",
        headers=_auth(world.key_read_secret),
    )
    assert [item["slug"] for item in response.json()["items"]] == ["b-prod"]
    response = client.get("/api/v1/moadian/profiles?limit=2", headers=_auth(world.key_read_secret))
    body = response.json()
    assert [item["slug"] for item in body["items"]] == ["a-test", "b-prod"]
    assert body["next_cursor"] is not None
    response = client.get(
        f"/api/v1/moadian/profiles?limit=2&cursor={body['next_cursor']}",
        headers=_auth(world.key_read_secret),
    )
    assert [item["slug"] for item in response.json()["items"]] == ["c-test"]
    assert response.json()["next_cursor"] is None


def test_list_profiles_rejects_unknown_filter(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get(
        "/api/v1/moadian/profiles?environment=sandbox",
        headers=_auth(world.key_read_secret),
    )
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_FILTER"


def test_update_profile(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    created = _create_profile(client, world.key_submit_secret, "to-update")
    response = client.patch(
        f"/api/v1/moadian/profiles/{created['profile_id']}",
        json={"name": "Renamed", "is_active": False},
        headers=_auth(world.key_submit_secret),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Renamed"
    assert body["is_active"] is False
    assert body["slug"] == "to-update"
    assert body["environment"] == "test"


def test_profile_of_another_tenant_is_not_found(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    created = _create_profile(client, world.key_submit_secret, "tenant-one")
    response = client.get(
        f"/api/v1/moadian/profiles/{created['profile_id']}",
        headers=_auth(world.key_other_read_secret),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "MOADIAN_PROFILE_NOT_FOUND"


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


def test_create_document_and_get(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    profile = _create_profile(client, world.key_submit_secret, "doc-profile")
    document = _create_document(client, world.key_submit_secret, profile["profile_id"], n=101)
    assert document["taxid"] == taxid_for(101)
    assert document["serial"] == "0002F2B4E7"
    assert document["invoice_type"] == 1
    assert document["status"] == "draft"
    assert len(document["payload_hash"]) == 64
    fetched = client.get(
        f"/api/v1/moadian/documents/{document['document_id']}",
        headers=_auth(world.key_read_secret),
    )
    assert fetched.status_code == 200
    assert fetched.json() == document


def test_create_document_invalid_payload_422_with_problems(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    profile = _create_profile(client, world.key_submit_secret, "bad-doc-profile")
    payload = valid_invoice_payload(taxid=taxid_for(102))
    del payload["header"]["tins"]
    payload["body"][0]["sstid"] = 123
    response = client.post(
        "/api/v1/moadian/documents",
        json={"profile_id": profile["profile_id"], "payload": payload},
        headers=_auth(world.key_submit_secret),
    )
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "MOADIAN_INVALID_DOCUMENT"
    assert "header.tins is required." in body["detail"]
    assert "body[0].sstid must be 13 digits." in body["detail"]


def test_create_document_duplicate_taxid_409(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    profile = _create_profile(client, world.key_submit_secret, "dup-taxid-profile")
    _create_document(client, world.key_submit_secret, profile["profile_id"], n=103)
    response = client.post(
        "/api/v1/moadian/documents",
        json={"profile_id": profile["profile_id"], "payload": valid_invoice_payload(taxid=taxid_for(103))},
        headers=_auth(world.key_submit_secret),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "MOADIAN_DUPLICATE_TAXID"


def test_create_document_unknown_profile_404(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.post(
        "/api/v1/moadian/documents",
        json={"profile_id": str(uuid.uuid4()), "payload": valid_invoice_payload(taxid=taxid_for(104))},
        headers=_auth(world.key_submit_secret),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "MOADIAN_PROFILE_NOT_FOUND"


# ---------------------------------------------------------------------------
# Submissions: full lifecycle over HTTP
# ---------------------------------------------------------------------------


def test_full_lifecycle_accept_via_http(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    profile = _create_profile(client, world.key_submit_secret, "lifecycle-profile")
    document = _create_document(client, world.key_submit_secret, profile["profile_id"], n=111)
    submission = _create_submission(client, world.key_submit_secret, document["document_id"])
    assert submission["status"] == "draft"
    assert submission["retry"] is False
    submission_id = submission["submission_id"]

    # Queueing a draft is a 409: approval is required first.
    response = client.post(
        f"/api/v1/moadian/submissions/{submission_id}/queue",
        headers=_auth(world.key_submit_secret),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "MOADIAN_INVALID_STATE"

    # Approve: the recorded human approval.
    response = client.post(
        f"/api/v1/moadian/submissions/{submission_id}/approve",
        json={"note": "reviewed by accountant"},
        headers=_auth(world.key_submit_secret),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "approved"
    assert body["approved_by"] is not None
    assert body["approved_at"] is not None
    assert body["approval_note"] == "reviewed by accountant"

    # Approving again is a 409.
    response = client.post(
        f"/api/v1/moadian/submissions/{submission_id}/approve",
        json={},
        headers=_auth(world.key_submit_secret),
    )
    assert response.status_code == 409

    # Queue: writes the outbox event.
    response = client.post(
        f"/api/v1/moadian/submissions/{submission_id}/queue",
        headers=_auth(world.key_submit_secret),
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "queued"
    assert response.json()["outbox_event_id"] is not None

    # First reconcile: dispatch + PENDING.
    response = client.post("/api/v1/moadian/reconcile", headers=_auth(world.key_submit_secret))
    assert response.status_code == 200, response.text
    report = response.json()
    assert report == {
        "dispatched": 1,
        "submit_failed": 0,
        "skipped": 0,
        "polled": 1,
        "accepted": 0,
        "rejected": 0,
        "still_pending": 1,
    }
    fetched = client.get(
        f"/api/v1/moadian/submissions/{submission_id}",
        headers=_auth(world.key_read_secret),
    )
    assert fetched.json()["status"] == "submitted"
    assert fetched.json()["reference_number"] is not None
    assert fetched.json()["request_hash"] is not None
    assert fetched.json()["response_hash"] is not None
    events = client.get(
        f"/api/v1/moadian/submissions/{submission_id}/status-events",
        headers=_auth(world.key_read_secret),
    )
    assert events.status_code == 200
    items = events.json()["items"]
    assert [item["status"] for item in items] == ["PENDING"]
    assert items[0]["source"] == "simulator"
    assert items[0]["evidence"]["referenceNumber"] == fetched.json()["reference_number"]

    # Second reconcile: SUCCESS → accepted.
    response = client.post("/api/v1/moadian/reconcile", headers=_auth(world.key_submit_secret))
    assert response.json()["accepted"] == 1
    assert response.json()["polled"] == 1
    fetched = client.get(
        f"/api/v1/moadian/submissions/{submission_id}",
        headers=_auth(world.key_read_secret),
    )
    assert fetched.json()["status"] == "accepted"
    doc = client.get(
        f"/api/v1/moadian/documents/{document['document_id']}",
        headers=_auth(world.key_read_secret),
    )
    assert doc.json()["status"] == "accepted"
    events = client.get(
        f"/api/v1/moadian/submissions/{submission_id}/status-events",
        headers=_auth(world.key_read_secret),
    )
    assert [item["status"] for item in events.json()["items"]] == ["PENDING", "SUCCESS"]


def test_full_lifecycle_reject_via_http(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    profile = _create_profile(client, world.key_submit_secret, "reject-profile")
    document = _create_document(client, world.key_submit_secret, profile["profile_id"], n=112)
    submission = _create_submission(
        client, world.key_submit_secret, document["document_id"], scenario="reject"
    )
    submission_id = submission["submission_id"]
    client.post(
        f"/api/v1/moadian/submissions/{submission_id}/approve",
        json={},
        headers=_auth(world.key_submit_secret),
    )
    client.post(
        f"/api/v1/moadian/submissions/{submission_id}/queue",
        headers=_auth(world.key_submit_secret),
    )
    client.post("/api/v1/moadian/reconcile", headers=_auth(world.key_submit_secret))
    response = client.post("/api/v1/moadian/reconcile", headers=_auth(world.key_submit_secret))
    assert response.json()["rejected"] == 1
    fetched = client.get(
        f"/api/v1/moadian/submissions/{submission_id}",
        headers=_auth(world.key_read_secret),
    )
    assert fetched.json()["status"] == "rejected"
    assert fetched.json()["last_error_code"] == "SIMULATED_REJECTION"
    doc = client.get(
        f"/api/v1/moadian/documents/{document['document_id']}",
        headers=_auth(world.key_read_secret),
    )
    assert doc.json()["status"] == "rejected"


def test_production_submission_queue_is_501(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    profile = _create_profile(client, world.key_submit_secret, "prod-profile", environment="production")
    document = _create_document(client, world.key_submit_secret, profile["profile_id"], n=113)
    submission = _create_submission(client, world.key_submit_secret, document["document_id"])
    submission_id = submission["submission_id"]
    # Draft creation and approval work for production profiles…
    response = client.post(
        f"/api/v1/moadian/submissions/{submission_id}/approve",
        json={"note": "approved for production"},
        headers=_auth(world.key_submit_secret),
    )
    assert response.status_code == 200
    # …but queueing is refused: no production connector exists yet.
    response = client.post(
        f"/api/v1/moadian/submissions/{submission_id}/queue",
        headers=_auth(world.key_submit_secret),
    )
    assert response.status_code == 501
    assert response.json()["code"] == "MOADIAN_PRODUCTION_NOT_CONNECTED"


def test_submission_of_another_tenant_is_not_found(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    profile = _create_profile(client, world.key_submit_secret, "iso-profile")
    document = _create_document(client, world.key_submit_secret, profile["profile_id"], n=114)
    submission = _create_submission(client, world.key_submit_secret, document["document_id"])
    response = client.get(
        f"/api/v1/moadian/submissions/{submission['submission_id']}",
        headers=_auth(world.key_other_read_secret),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "MOADIAN_SUBMISSION_NOT_FOUND"
    response = client.get(
        f"/api/v1/moadian/submissions/{submission['submission_id']}/status-events",
        headers=_auth(world.key_other_read_secret),
    )
    assert response.status_code == 404


def test_reconcile_requires_submit_scope(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.post("/api/v1/moadian/reconcile", headers=_auth(world.key_read_secret))
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


def test_openapi_includes_moadian_paths(settings: AppSettings, world: World) -> None:
    client = _test_client(settings)
    response = client.get("/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]
    for path in (
        "/api/v1/moadian/profiles",
        "/api/v1/moadian/profiles/{profile_id}",
        "/api/v1/moadian/documents",
        "/api/v1/moadian/documents/{document_id}",
        "/api/v1/moadian/submissions",
        "/api/v1/moadian/submissions/{submission_id}",
        "/api/v1/moadian/submissions/{submission_id}/approve",
        "/api/v1/moadian/submissions/{submission_id}/queue",
        "/api/v1/moadian/submissions/{submission_id}/status-events",
        "/api/v1/moadian/reconcile",
    ):
        assert path in paths, f"{path} missing from OpenAPI"
