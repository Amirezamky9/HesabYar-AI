"""Tests for scoped API-key auth (US-103): generation, hashing, service,
repository, scopes, identity and the never-in-logs rule.

Two layers:

- Pure unit tests (no database): key format/entropy, HMAC hashing with
  the pepper, constant-time comparison, scope parsing/validation and the
  AuthenticatedClient helpers.
- Database tests: issue / rotate / revoke / authenticate through
  ApiKeyService against the shared hesabyar_test DB, in the same style
  as test_phase1_schema.py — each test runs in a rolled-back transaction
  (the service flushes but never commits).
"""

from __future__ import annotations

import datetime
import hmac
import logging
import os
import re
import uuid
from collections.abc import Generator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from hesabyar.infrastructure.auth import (
    KEY_PREFIX_LENGTH,
    KEY_SCHEME_PREFIX,
    ApiKeyCrypto,
    ApiKeyService,
    AuthenticatedClient,
    AuthenticationError,
    UnknownScopeError,
    extract_key_prefix,
    format_scopes,
    parse_scopes,
    validate_scopes,
)
from hesabyar.infrastructure.auth.scopes import KNOWN_SCOPES, missing_scopes
from hesabyar.infrastructure.config import FixedClock
from hesabyar.infrastructure.persistence.postgres.models import (
    ApiClientModel,
    Base,
    EngineCompanyModel,
    EngineConnectionModel,
    TenantModel,
)
from hesabyar.infrastructure.persistence.postgres.repositories import (
    ApiClientRepository,
    ApiKeyRepository,
    SqlAlchemyInstallationTenantResolver,
)

TEST_DB_URL = os.getenv(
    "HESABYAR_DATABASE_URL",
    "postgresql+psycopg://hesabyar:hesabyar_pass@hesabyar-postgres:5432/hesabyar_test",
)

INSTALLATION_ID = "00000000-0000-0000-0000-000000000001"
PEPPER = "test-pepper-for-us-103"
NOW = datetime.datetime(2026, 11, 16, 12, 0, 0, tzinfo=datetime.UTC)

_URLSAFE_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_HEX64_RE = re.compile(r"^v1:[0-9a-f]{64}$")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def engine() -> Generator[Engine, None, None]:
    """Engine for the shared test DB (tables already exist via Alembic)."""
    eng = create_engine(TEST_DB_URL, echo=False)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def db(engine: Engine) -> Generator[Session, None, None]:
    """Transactional session rolled back after each test."""
    connection = engine.connect()
    transaction = connection.begin()
    session = sessionmaker(bind=connection)()
    try:
        yield session
    finally:
        session.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()


@pytest.fixture()
def service() -> ApiKeyService:
    """ApiKeyService on a fixed clock (deterministic timestamps)."""
    return ApiKeyService(ApiKeyCrypto(PEPPER), clock=FixedClock(NOW))


def _make_tenant(db: Session) -> TenantModel:
    tenant = TenantModel(
        id=str(uuid.uuid4()), slug=f"auth-test-{uuid.uuid4().hex[:8]}", name="Auth Test Tenant"
    )
    db.add(tenant)
    db.flush()
    return tenant


def _make_client(db: Session, tenant_id: str, *, active: bool = True) -> ApiClientModel:
    client = ApiClientModel(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        name=f"client-{uuid.uuid4().hex[:8]}",
        is_active=active,
    )
    db.add(client)
    db.flush()
    return client


def _make_company(db: Session, tenant_id: str, external_id: str) -> EngineCompanyModel:
    connection = EngineConnectionModel(
        id=str(uuid.uuid4()),
        name=f"conn-{uuid.uuid4().hex[:8]}",
        engine="tryton",
        base_url="http://localhost:8000",
        database_name=f"hesabyar_tryton_test_{uuid.uuid4().hex[:8]}",
    )
    db.add(connection)
    db.flush()
    company = EngineCompanyModel(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        engine_connection_id=connection.id,
        external_company_id=external_id,
        company_name=f"Company {external_id}",
    )
    db.add(company)
    db.flush()
    return company


def _issue(
    service: ApiKeyService,
    db: Session,
    client: ApiClientModel,
    *,
    scopes: list[str] | None = None,
    engine_company_id: str | None = None,
    expires_at: datetime.datetime | None = None,
):
    return service.issue_key(
        db,
        client=client,
        name="test key",
        scopes=scopes or ["company:read"],
        installation_id=INSTALLATION_ID,
        engine_company_id=engine_company_id,
        expires_at=expires_at,
    )


# ---------------------------------------------------------------------------
# Key generation
# ---------------------------------------------------------------------------


def test_generated_key_format_and_entropy() -> None:
    crypto = ApiKeyCrypto(PEPPER)
    generated = crypto.generate()
    assert generated.secret.startswith(KEY_SCHEME_PREFIX)
    # 9-char scheme prefix + 43 chars of base64url (32 random bytes).
    assert len(generated.secret) == len(KEY_SCHEME_PREFIX) + 43
    random_part = generated.secret[len(KEY_SCHEME_PREFIX):]
    assert _URLSAFE_RE.match(random_part), "random part must be URL-safe base64"
    # The visible prefix is the first 16 characters of the secret.
    assert generated.key_prefix == generated.secret[:KEY_PREFIX_LENGTH]
    assert len(generated.key_prefix) == KEY_PREFIX_LENGTH
    # Stored hash is a versioned 64-hex HMAC digest — never the secret.
    assert _HEX64_RE.match(generated.key_hash)
    assert generated.secret not in generated.key_hash


def test_generated_keys_are_unique() -> None:
    crypto = ApiKeyCrypto(PEPPER)
    generated = [crypto.generate() for _ in range(200)]
    secrets = {g.secret for g in generated}
    prefixes = {g.key_prefix for g in generated}
    hashes = {g.key_hash for g in generated}
    assert len(secrets) == 200
    assert len(prefixes) == 200
    assert len(hashes) == 200


def test_extract_key_prefix() -> None:
    crypto = ApiKeyCrypto(PEPPER)
    generated = crypto.generate()
    assert extract_key_prefix(generated.secret) == generated.key_prefix
    assert extract_key_prefix("short") is None
    assert extract_key_prefix("") is None
    assert extract_key_prefix("sk_live_" + "x" * 43) is None
    assert extract_key_prefix("hya_live_" + "x" * 5) is None  # too short


# ---------------------------------------------------------------------------
# Hashing / verification
# ---------------------------------------------------------------------------


def test_empty_pepper_is_refused() -> None:
    with pytest.raises(ValueError, match="pepper must not be empty"):
        ApiKeyCrypto("")


def test_hash_is_deterministic_and_pepper_bound() -> None:
    crypto = ApiKeyCrypto(PEPPER)
    other_pepper = ApiKeyCrypto("a-different-pepper")
    generated = crypto.generate()
    assert crypto.hash_secret(generated.secret) == generated.key_hash
    assert other_pepper.hash_secret(generated.secret) != generated.key_hash
    # A wrong pepper rejects a valid key.
    assert not other_pepper.verify(generated.secret, generated.key_hash)
    # Verification succeeds only with the right pepper + secret.
    assert crypto.verify(generated.secret, generated.key_hash)
    assert not crypto.verify(generated.secret + "x", generated.key_hash)
    assert not crypto.verify("hya_live_" + "y" * 43, generated.key_hash)
    assert not crypto.verify("", generated.key_hash)


def test_verify_fails_closed_on_unknown_hash_version() -> None:
    crypto = ApiKeyCrypto(PEPPER)
    generated = crypto.generate()
    assert not crypto.verify(generated.secret, "v2:" + "0" * 64)
    assert not crypto.verify(generated.secret, "not-a-hash")
    assert not crypto.verify(generated.secret, "")


def test_verify_uses_constant_time_comparison(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []
    original = hmac.compare_digest

    def spy(a: str, b: str) -> bool:
        calls.append((a, b))
        return original(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy)
    crypto = ApiKeyCrypto(PEPPER)
    generated = crypto.generate()
    assert crypto.verify(generated.secret, generated.key_hash)
    assert len(calls) == 1
    # The compared values are hashes — the secret itself is never compared.
    for left, right in calls:
        assert generated.secret not in left
        assert generated.secret not in right
        assert left.startswith("v1:") and right.startswith("v1:")


# ---------------------------------------------------------------------------
# Scopes
# ---------------------------------------------------------------------------


def test_scope_catalog_matches_spec() -> None:
    # Spec §14.2 lists 25 granular capabilities; Phase 4 adds 4 more
    # (US-402 tax:read/tax:decide, US-404 moadian:read/moadian:submit);
    # Phase 5 US-504 adds 3 review scopes
    # (review:read/review:resolve/review:file); Phase 5-2 adds 6
    # (US-514 banking:import, US-515 packaging:read/packaging:manage,
    # US-516 cashbox:read/cashbox:count/cashbox:manage); Phase 5-3
    # US-521 adds 2 (settlement:read/settlement:manage); Phase 5-4
    # US-531 adds 3 (cheques:read/cheques:write/cheques:transition);
    # Phase 5-5 US-540 adds 2 (installments:read/installments:write)
    # and US-544 adds 1 (sms:manage); Phase 5-7 US-560 adds 1
    # (jobs:manage); Phase 5-7 US-561 adds 2
    # (rules:read/rules:manage); Phase 5-7 US-564 adds 1
    # (reminders:manage).
    # No wildcard, no admin.
    assert len(KNOWN_SCOPES) == 50
    assert "company:read" in KNOWN_SCOPES
    assert "account:read" in KNOWN_SCOPES
    assert "ledger:post" in KNOWN_SCOPES
    assert "settings:manage" in KNOWN_SCOPES
    assert "api_keys:manage" in KNOWN_SCOPES
    assert "tax:read" in KNOWN_SCOPES
    assert "tax:decide" in KNOWN_SCOPES
    assert "moadian:read" in KNOWN_SCOPES
    assert "moadian:submit" in KNOWN_SCOPES
    assert "banking:import" in KNOWN_SCOPES
    assert "packaging:read" in KNOWN_SCOPES
    assert "packaging:manage" in KNOWN_SCOPES
    assert "cashbox:read" in KNOWN_SCOPES
    assert "cashbox:count" in KNOWN_SCOPES
    assert "cashbox:manage" in KNOWN_SCOPES
    assert "settlement:read" in KNOWN_SCOPES
    assert "settlement:manage" in KNOWN_SCOPES
    assert "installments:read" in KNOWN_SCOPES
    assert "installments:write" in KNOWN_SCOPES
    assert "jobs:manage" in KNOWN_SCOPES
    assert "rules:read" in KNOWN_SCOPES
    assert "rules:manage" in KNOWN_SCOPES
    assert "reminders:manage" in KNOWN_SCOPES
    assert "sms:manage" in KNOWN_SCOPES
    assert "admin" not in KNOWN_SCOPES
    assert "*" not in KNOWN_SCOPES


def test_parse_and_format_scopes() -> None:
    assert parse_scopes("company:read,account:read") == {"company:read", "account:read"}
    assert parse_scopes(" company:read , account:read , company:read ") == {
        "company:read",
        "account:read",
    }
    assert parse_scopes("") == frozenset()
    assert parse_scopes(None) == frozenset()
    assert format_scopes({"account:read", "company:read"}) == "account:read,company:read"
    assert format_scopes([]) == ""


def test_validate_scopes_rejects_unknown() -> None:
    assert validate_scopes(["company:read", "account:read"]) == {
        "company:read",
        "account:read",
    }
    with pytest.raises(UnknownScopeError, match="admin"):
        validate_scopes(["company:read", "admin"])
    with pytest.raises(UnknownScopeError, match="company:write"):
        validate_scopes(["company:write"])


def test_missing_scopes() -> None:
    assert missing_scopes({"company:read"}, ["company:read"]) == []
    assert missing_scopes({"company:read"}, ["company:read", "account:read"]) == ["account:read"]
    assert missing_scopes(set(), ["ledger:post", "account:read"]) == [
        "account:read",
        "ledger:post",
    ]


# ---------------------------------------------------------------------------
# AuthenticatedClient (identity)
# ---------------------------------------------------------------------------


def _identity(**overrides: object) -> AuthenticatedClient:
    defaults: dict[str, object] = {
        "client_id": "client-1",
        "tenant_id": "tenant-1",
        "installation_id": INSTALLATION_ID,
        "key_id": "key-1",
        "key_prefix": "hya_live_abc12345",
        "client_name": "test client",
        "scopes": frozenset({"company:read"}),
        "engine_company_id": None,
    }
    defaults.update(overrides)
    return AuthenticatedClient(**defaults)  # type: ignore[arg-type]


def test_identity_scope_checks_are_exact() -> None:
    identity = _identity(scopes=frozenset({"company:read", "account:read"}))
    assert identity.has_scope("company:read")
    assert not identity.has_scope("company:write")
    assert not identity.has_scope("admin")
    assert identity.has_scopes("company:read", "account:read")
    assert not identity.has_scopes("company:read", "ledger:post")
    assert identity.missing_scopes("company:read", "ledger:post") == ["ledger:post"]


def test_identity_company_restriction() -> None:
    unrestricted = _identity(engine_company_id=None)
    assert unrestricted.can_access_company("any-company-id")
    restricted = _identity(engine_company_id="company-a")
    assert restricted.can_access_company("company-a")
    assert not restricted.can_access_company("company-b")


# ---------------------------------------------------------------------------
# Issuance (service + DB)
# ---------------------------------------------------------------------------


def test_issue_key_stores_only_prefix_and_hash(db: Session, service: ApiKeyService) -> None:
    tenant = _make_tenant(db)
    client = _make_client(db, tenant.id)
    issued = _issue(service, db, client, scopes=["company:read", "account:read"])

    # The secret is returned exactly once, at issuance.
    assert issued.secret.startswith(KEY_SCHEME_PREFIX)
    assert len(issued.secret) == 52

    # The stored record holds prefix + hash only — never the secret.
    record = ApiKeyRepository(db).get_by_id(issued.record.id)
    assert record is not None
    assert record.key_prefix == issued.secret[:KEY_PREFIX_LENGTH]
    assert _HEX64_RE.match(record.key_hash)
    assert issued.secret not in record.key_hash
    assert record.scopes == "account:read,company:read"
    assert record.scope_list == ["account:read", "company:read"]
    assert record.client_id == client.id
    assert record.installation_id == INSTALLATION_ID
    assert record.revoked_at is None
    assert record.last_used_at is None
    # The model repr never leaks the hash, let alone the secret.
    assert issued.secret not in repr(record)
    assert record.key_hash not in repr(record)


def test_issue_key_rejects_unknown_scope(db: Session, service: ApiKeyService) -> None:
    tenant = _make_tenant(db)
    client = _make_client(db, tenant.id)
    with pytest.raises(UnknownScopeError, match="admin"):
        service.issue_key(
            db,
            client=client,
            name="bad key",
            scopes=["admin"],
            installation_id=INSTALLATION_ID,
        )


def test_issue_key_rejects_inactive_client(db: Session, service: ApiKeyService) -> None:
    tenant = _make_tenant(db)
    client = _make_client(db, tenant.id, active=False)
    with pytest.raises(ValueError, match="inactive"):
        _issue(service, db, client)


def test_issue_key_with_company_restriction(db: Session, service: ApiKeyService) -> None:
    tenant = _make_tenant(db)
    client = _make_client(db, tenant.id)
    company = _make_company(db, tenant.id, "101")
    issued = _issue(service, db, client, engine_company_id=company.id)
    assert issued.record.engine_company_id == company.id


# ---------------------------------------------------------------------------
# Authentication (service + DB)
# ---------------------------------------------------------------------------


def test_authenticate_valid_key(db: Session, service: ApiKeyService) -> None:
    tenant = _make_tenant(db)
    client = _make_client(db, tenant.id)
    company = _make_company(db, tenant.id, "101")
    issued = _issue(
        service,
        db,
        client,
        scopes=["company:read", "account:read"],
        engine_company_id=company.id,
    )
    identity = service.authenticate(db, secret=issued.secret)
    assert identity.client_id == client.id
    assert identity.tenant_id == tenant.id
    assert identity.installation_id == INSTALLATION_ID
    assert identity.key_id == issued.record.id
    assert identity.key_prefix == issued.record.key_prefix
    assert identity.client_name == client.name
    assert identity.scopes == {"company:read", "account:read"}
    assert identity.engine_company_id == company.id


def test_authenticate_records_last_used(db: Session, service: ApiKeyService) -> None:
    tenant = _make_tenant(db)
    client = _make_client(db, tenant.id)
    issued = _issue(service, db, client)
    assert issued.record.last_used_at is None
    service.authenticate(db, secret=issued.secret)
    assert issued.record.last_used_at == NOW
    # A later authentication advances last-used.
    later_service = ApiKeyService(
        ApiKeyCrypto(PEPPER),
        clock=FixedClock(NOW + datetime.timedelta(hours=2)),
    )
    later_service.authenticate(db, secret=issued.secret)
    assert issued.record.last_used_at == NOW + datetime.timedelta(hours=2)


def test_authenticate_rejects_unknown_key(db: Session, service: ApiKeyService) -> None:
    tenant = _make_tenant(db)
    client = _make_client(db, tenant.id)
    issued = _issue(service, db, client)
    # Well-formed but never issued.
    other = ApiKeyCrypto(PEPPER).generate()
    assert other.secret != issued.secret
    with pytest.raises(AuthenticationError) as exc_info:
        service.authenticate(db, secret=other.secret)
    assert exc_info.value.reason == "unknown"


def test_authenticate_rejects_malformed_key(db: Session, service: ApiKeyService) -> None:
    for malformed in ("", "short", "sk_live_" + "x" * 43, "hya_live_" + "x" * 5):
        with pytest.raises(AuthenticationError) as exc_info:
            service.authenticate(db, secret=malformed)
        assert exc_info.value.reason == "malformed"


def test_authenticate_rejects_revoked_key(db: Session, service: ApiKeyService) -> None:
    tenant = _make_tenant(db)
    client = _make_client(db, tenant.id)
    issued = _issue(service, db, client)
    service.revoke_key(db, key=issued.record, reason="compromised")
    assert issued.record.revoked_at == NOW
    assert issued.record.revoked_reason == "compromised"
    with pytest.raises(AuthenticationError) as exc_info:
        service.authenticate(db, secret=issued.secret)
    assert exc_info.value.reason == "revoked"


def test_authenticate_rejects_expired_key(db: Session, service: ApiKeyService) -> None:
    tenant = _make_tenant(db)
    client = _make_client(db, tenant.id)
    expired = _issue(
        service, db, client, expires_at=NOW - datetime.timedelta(seconds=1)
    )
    with pytest.raises(AuthenticationError) as exc_info:
        service.authenticate(db, secret=expired.secret)
    assert exc_info.value.reason == "expired"
    # A key expiring in the future still works.
    live = _issue(service, db, client, expires_at=NOW + datetime.timedelta(days=30))
    assert service.authenticate(db, secret=live.secret).key_id == live.record.id


def test_authenticate_rejects_inactive_client(db: Session, service: ApiKeyService) -> None:
    tenant = _make_tenant(db)
    client = _make_client(db, tenant.id)
    issued = _issue(service, db, client)
    client.is_active = False
    db.flush()
    with pytest.raises(AuthenticationError) as exc_info:
        service.authenticate(db, secret=issued.secret)
    assert exc_info.value.reason == "client_inactive"


def test_authenticate_failure_does_not_record_last_used(
    db: Session, service: ApiKeyService
) -> None:
    tenant = _make_tenant(db)
    client = _make_client(db, tenant.id)
    issued = _issue(service, db, client)
    service.revoke_key(db, key=issued.record, reason="test")
    with pytest.raises(AuthenticationError):
        service.authenticate(db, secret=issued.secret)
    assert issued.record.last_used_at is None


# ---------------------------------------------------------------------------
# Rotation / revocation
# ---------------------------------------------------------------------------


def test_rotate_key_replaces_old_with_new(db: Session, service: ApiKeyService) -> None:
    tenant = _make_tenant(db)
    client = _make_client(db, tenant.id)
    company = _make_company(db, tenant.id, "101")
    expires = NOW + datetime.timedelta(days=30)
    old = _issue(
        service,
        db,
        client,
        scopes=["company:read", "account:read"],
        engine_company_id=company.id,
        expires_at=expires,
    )
    rotated = service.rotate_key(db, key=old.record)

    # Old key is revoked immediately and stops working.
    assert old.record.revoked_at == NOW
    assert old.record.revoked_reason == "rotated"
    with pytest.raises(AuthenticationError) as exc_info:
        service.authenticate(db, secret=old.secret)
    assert exc_info.value.reason == "revoked"

    # New key works, carries the same attributes, and has a fresh secret.
    assert rotated.secret != old.secret
    assert rotated.record.id != old.record.id
    assert rotated.record.scopes == "account:read,company:read"
    assert rotated.record.engine_company_id == company.id
    assert rotated.record.installation_id == INSTALLATION_ID
    assert rotated.record.expires_at == expires  # rotation never extends lifetime
    assert rotated.record.revoked_at is None
    identity = service.authenticate(db, secret=rotated.secret)
    assert identity.key_id == rotated.record.id
    assert identity.scopes == {"company:read", "account:read"}


def test_rotate_revoked_key_is_refused(db: Session, service: ApiKeyService) -> None:
    tenant = _make_tenant(db)
    client = _make_client(db, tenant.id)
    issued = _issue(service, db, client)
    service.revoke_key(db, key=issued.record, reason="test")
    with pytest.raises(ValueError, match="already-revoked"):
        service.rotate_key(db, key=issued.record)


def test_revoke_is_idempotent(db: Session, service: ApiKeyService) -> None:
    tenant = _make_tenant(db)
    client = _make_client(db, tenant.id)
    issued = _issue(service, db, client)
    service.revoke_key(db, key=issued.record, reason="first")
    service.revoke_key(db, key=issued.record, reason="second")
    assert issued.record.revoked_at == NOW
    assert issued.record.revoked_reason == "first"


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


def test_repository_find_by_prefix_and_list(db: Session, service: ApiKeyService) -> None:
    tenant = _make_tenant(db)
    client = _make_client(db, tenant.id)
    other_client = _make_client(db, tenant.id)
    first = _issue(service, db, client)
    second = _issue(service, db, client)
    _issue(service, db, other_client)

    repo = ApiKeyRepository(db)
    assert [k.id for k in repo.find_by_prefix(first.record.key_prefix)] == [first.record.id]
    assert repo.find_by_prefix("hya_live_nope123") == []
    assert {k.id for k in repo.list_for_client(client.id)} == {
        first.record.id,
        second.record.id,
    }
    assert repo.get_by_id("00000000-0000-0000-0000-000000000000") is None

    client_repo = ApiClientRepository(db)
    assert client_repo.get_by_id(client.id) is not None
    assert client_repo.get_by_id("00000000-0000-0000-0000-000000000000") is None
    assert {c.id for c in client_repo.list_for_tenant(tenant.id)} == {
        client.id,
        other_client.id,
    }


# ---------------------------------------------------------------------------
# Never in logs (spec §7.5)
# ---------------------------------------------------------------------------


def test_keys_never_appear_in_logs(
    db: Session, service: ApiKeyService, caplog: pytest.LogCaptureFixture
) -> None:
    tenant = _make_tenant(db)
    client = _make_client(db, tenant.id)
    issued = _issue(service, db, client)
    unknown = ApiKeyCrypto(PEPPER).generate()

    with caplog.at_level(logging.INFO, logger="hesabyar.infrastructure.auth.service"):
        with pytest.raises(AuthenticationError):
            service.authenticate(db, secret=unknown.secret)
        service.authenticate(db, secret=issued.secret)
        service.revoke_key(db, key=issued.record, reason="test")
        with pytest.raises(AuthenticationError):
            service.authenticate(db, secret=issued.secret)

    assert caplog.text  # logging actually happened
    assert issued.secret not in caplog.text
    assert unknown.secret not in caplog.text
    # The prefix (safe to log) does appear for the unknown-key failure.
    assert unknown.key_prefix in caplog.text


# ---------------------------------------------------------------------------
# Installation → tenant resolver (US-555)
# ---------------------------------------------------------------------------


def test_resolver_returns_tenant_for_installation(db: Session, service: ApiKeyService) -> None:
    """The tick's lookup: the oldest key for an installation names its tenant."""
    tenant = _make_tenant(db)
    client = _make_client(db, tenant.id)
    installation_id = str(uuid.uuid4())
    service.issue_key(
        db,
        client=client,
        name="tick key",
        scopes=["settlement:read"],
        installation_id=installation_id,
    )
    resolver = SqlAlchemyInstallationTenantResolver(db)
    assert resolver.tenant_id_for_installation(installation_id) == tenant.id


def test_resolver_returns_none_for_unknown_installation(db: Session) -> None:
    resolver = SqlAlchemyInstallationTenantResolver(db)
    assert resolver.tenant_id_for_installation(str(uuid.uuid4())) is None
