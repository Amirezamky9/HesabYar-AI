"""Webhook security suite (US-701).

Proves the properties the threat model (docs/security/threat-model.md)
claims for the webhook boundary, end to end through the real FastAPI
app + scoped API-key auth + the PostgreSQL database:

- signature forgery: a signature made with the wrong secret, or a
  signature for a different body, is a 401 — and neither the webhook
  secret nor the API key leaks into the response or the logs;
- replay: the identical signed payload re-sent is a 200 duplicate
  with exactly one inbox row (no second accounting effect);
- replay window (US-701): a NEW event older than the window is a 422
  ``EVENT_TOO_OLD`` — while a re-delivery of an already-stored event
  still replays as a duplicate, whatever its age;
- unsigned ingest: a store with no secret configured still ingests
  (the endpoint requires an ``integrations:manage`` API key), but
  the skip is now logged instead of silent.

Basic HMAC behaviour already lives in
``test_rest_store_webhooks.py`` (valid signature verifies, bad or
missing signature 401s, no-store skips verification) and is not
duplicated here.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import logging
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
from hesabyar.infrastructure.auth.credential_vault import generate_encryption_key
from hesabyar.infrastructure.config import AppSettings, AuthSettings, EventPlatformSettings
from hesabyar.infrastructure.persistence.postgres.models import (
    ApiClientModel,
    Base,
    IdempotencyRecordModel,
    StoreModel,
    TenantModel,
)
from hesabyar.interfaces.rest.app import create_app
from tests.event_fixtures import raw_envelope

TEST_DB_URL = os.getenv(
    "HESABYAR_DATABASE_URL",
    "postgresql+psycopg://hesabyar:hesabyar_pass@hesabyar-postgres:5432/hesabyar_test",
)

ALL_INTEGRATIONS_SCOPES = ["integrations:read", "integrations:manage"]


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
def vault_key() -> str:
    """One Fernet key per test, shared by every app instance it builds."""
    return generate_encryption_key()


@pytest.fixture()
def settings(vault_key: str) -> AppSettings:
    """Fresh settings with a test-generated vault key (dev pepper)."""
    return AppSettings(auth=AuthSettings(credentials_encryption_key=vault_key))


@pytest.fixture()
def world(engine: Engine, settings: AppSettings) -> Generator[World, None, None]:
    """Provision a tenant with integrations-scoped API keys."""
    session = sessionmaker(bind=engine)()
    service = ApiKeyService(ApiKeyCrypto(settings.auth.api_key_pepper))
    installation_id = str(uuid.uuid4())
    tenant_id = ""
    source_system = f"evt-sec-701-{uuid.uuid4().hex[:8]}"
    try:
        tenant = TenantModel(
            id=str(uuid.uuid4()), slug=f"sec-701-{uuid.uuid4().hex[:8]}", name="701 Tenant"
        )
        session.add(tenant)
        session.flush()
        client = ApiClientModel(
            id=str(uuid.uuid4()), tenant_id=tenant.id, name="sec-701 client"
        )
        session.add(client)
        session.flush()
        key_full = service.issue_key(
            session,
            client=client,
            name="sec-701 full",
            scopes=ALL_INTEGRATIONS_SCOPES,
            installation_id=installation_id,
        )
        key_read = service.issue_key(
            session,
            client=client,
            name="sec-701 read",
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
            if tenant_id:
                cleanup.query(TenantModel).filter(
                    TenantModel.id == tenant_id
                ).delete(synchronize_session=False)
            cleanup.commit()
        finally:
            cleanup.close()


@pytest.fixture()
def api(settings: AppSettings) -> TestClient:
    return TestClient(create_app(settings=settings), raise_server_exceptions=True)


def _auth(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


def _fresh_now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def _ingest_body(world: World, **overrides: object) -> dict:
    # Fresh by default: the production wiring enforces the 168h replay
    # window, so the shared fixture's fixed 2026-11-17 occurred_at
    # would age out of it.
    overrides.setdefault("occurred_at", _fresh_now())
    return raw_envelope(source_system=world.source_system, **overrides)  # type: ignore[arg-type]


def _post_event(
    api: TestClient, world: World, body: dict, signature: str | None = None
):
    raw_body = json.dumps(body).encode("utf-8")
    headers = {
        **_auth(world.key_full_secret),
        "Content-Type": "application/json",
        "X-HesabYar-Event-Type": "sales.order.completed",
    }
    if signature is not None:
        headers["X-HesabYar-Signature"] = signature
    return api.post(
        f"/api/v1/integrations/{world.source_system}/events",
        content=raw_body,
        headers=headers,
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


def _inbox_count(api: TestClient, world: World) -> int:
    listing = api.get("/api/v1/integrations/inbox", headers=_auth(world.key_read_secret))
    assert listing.status_code == 200, listing.text
    return len(listing.json()["items"])


# ---------------------------------------------------------------------------
# Signature forgery + tampering
# ---------------------------------------------------------------------------


def test_signature_with_the_wrong_secret_is_401(
    api: TestClient, world: World, caplog: pytest.LogCaptureFixture
) -> None:
    webhook_secret = _create_store(api, world)
    body = _ingest_body(world)
    forged = _sign(json.dumps(body).encode("utf-8"), "attacker-controlled-secret")
    with caplog.at_level(logging.INFO, logger="hesabyar"):
        response = _post_event(api, world, body, signature=forged)
    assert response.status_code == 401, response.text
    assert response.json()["code"] == "EVENT_SIGNATURE_INVALID"
    assert _inbox_count(api, world) == 0
    # Neither secret leaks into the error response or the logs.
    assert webhook_secret not in response.text
    assert webhook_secret not in caplog.text
    assert world.key_full_secret not in response.text
    assert world.key_full_secret not in caplog.text


def test_signature_for_another_body_is_401(api: TestClient, world: World) -> None:
    """A valid signature does not transfer to a tampered body."""
    webhook_secret = _create_store(api, world)
    original = _ingest_body(world)
    signature = _sign(json.dumps(original).encode("utf-8"), webhook_secret)
    tampered = _ingest_body(world, payload={"total_amount": 999000})
    response = _post_event(api, world, tampered, signature=signature)
    assert response.status_code == 401, response.text
    assert response.json()["code"] == "EVENT_SIGNATURE_INVALID"
    assert _inbox_count(api, world) == 0


# ---------------------------------------------------------------------------
# Replay: identical payloads duplicate, never double-post
# ---------------------------------------------------------------------------


def test_replayed_payload_is_a_duplicate_with_one_inbox_row(
    api: TestClient, world: World
) -> None:
    webhook_secret = _create_store(api, world)
    body = _ingest_body(world)
    signature = _sign(json.dumps(body).encode("utf-8"), webhook_secret)
    first = _post_event(api, world, body, signature=signature)
    assert first.status_code == 201, first.text
    assert first.json()["status"] == "created"
    assert first.json()["signature_verified"] is True
    second = _post_event(api, world, body, signature=signature)
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "duplicate"
    assert second.json()["inbox_event_id"] == first.json()["inbox_event_id"]
    assert _inbox_count(api, world) == 1


# ---------------------------------------------------------------------------
# Replay window (US-701)
# ---------------------------------------------------------------------------


def test_stale_new_event_is_rejected(api: TestClient, world: World) -> None:
    """A never-delivered event older than the window cannot be replayed in."""
    webhook_secret = _create_store(api, world)
    stale_at = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=8)).isoformat()
    body = _ingest_body(world, occurred_at=stale_at)
    signature = _sign(json.dumps(body).encode("utf-8"), webhook_secret)
    response = _post_event(api, world, body, signature=signature)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "EVENT_TOO_OLD"
    assert "replay window" in response.json()["detail"]
    assert _inbox_count(api, world) == 0


def test_stale_redelivery_of_a_stored_event_still_replays(
    api: TestClient, world: World, vault_key: str
) -> None:
    """The window punishes late FIRST delivery, never legitimate redelivery.

    The event is stored when fresh (through an app instance with the
    window disabled, simulating the original delivery 8 days ago);
    the identical bytes re-posted through the production-wired app
    still replay as a duplicate instead of a 422.
    """
    webhook_secret = _create_store(api, world)
    stale_at = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=8)).isoformat()
    body = _ingest_body(world, occurred_at=stale_at)
    signature = _sign(json.dumps(body).encode("utf-8"), webhook_secret)

    window_off = AppSettings(
        auth=AuthSettings(credentials_encryption_key=vault_key),
        events=EventPlatformSettings(webhook_max_event_age_hours=0),
    )
    old_api = TestClient(create_app(settings=window_off), raise_server_exceptions=True)
    first = _post_event(old_api, world, body, signature=signature)
    assert first.status_code == 201, first.text

    second = _post_event(api, world, body, signature=signature)
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "duplicate"
    assert second.json()["inbox_event_id"] == first.json()["inbox_event_id"]
    assert _inbox_count(api, world) == 1


# ---------------------------------------------------------------------------
# Unsigned ingest is logged, not silent (US-701)
# ---------------------------------------------------------------------------


def test_unsigned_ingest_is_logged_and_flags_the_skip(
    api: TestClient, world: World, caplog: pytest.LogCaptureFixture
) -> None:
    # No store exists for this source system, so no secret is
    # configured and the signature check is skipped — the endpoint
    # still requires the integrations:manage API key.
    body = _ingest_body(world)
    with caplog.at_level(logging.INFO, logger="hesabyar"):
        response = _post_event(api, world, body)
    assert response.status_code == 201, response.text
    assert response.json()["signature_verified"] is False
    assert "signature check skipped" in response.json()["signature_note"]
    assert "no webhook secret is configured" in caplog.text
    assert world.key_full_secret not in caplog.text
    assert world.key_full_secret not in response.text
