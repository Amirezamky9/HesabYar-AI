"""Tests for the FastAPI skeleton: health / readiness / installation (US-101).

Two layers:

- Fake-service tests (no servers needed): the app's health-service and
  settings dependencies are overridden with canned values, so every
  status code, the X-Request-ID header and the problem-details envelope
  are exercised deterministically.
- Live tests (``test_live_*``): the real health service against the local
  PostgreSQL + trytond. They SKIP cleanly when either service is down,
  following the contract-test pattern in tests/contract/.

The live tests build fresh ``AppSettings()`` instead of the cached
``get_settings()`` so they always read the current environment.
"""

from __future__ import annotations

import ast
import re
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from hesabyar import __version__
from hesabyar.infrastructure.accounting.tryton.client import TrytonRestClient
from hesabyar.infrastructure.auth import ApiKeyCrypto, ApiKeyService
from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.infrastructure.config import (
    DEV_API_KEY_PEPPER,
    AppSettings,
    Environment,
)
from hesabyar.infrastructure.persistence.postgres.models import ApiClientModel, TenantModel
from hesabyar.interfaces.rest.app import create_app
from hesabyar.interfaces.rest.dependencies import (
    get_authenticated_client,
    get_health_service,
    get_settings_dep,
)
from hesabyar.interfaces.rest.schemas.health import (
    ComponentStatus,
    InstallationResponse,
    MigrationStatus,
    ReadinessResponse,
    TrytonModuleInfo,
    TrytonStatus,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

INSTALLATION_ID = "00000000-0000-0000-0000-000000000001"


def _fake_client(*, scopes: tuple[str, ...] = ("settings:manage",)) -> AuthenticatedClient:
    """Canned authenticated identity for tests that override auth."""
    return AuthenticatedClient(
        client_id="test-client",
        tenant_id="test-tenant",
        installation_id=INSTALLATION_ID,
        key_id="test-key",
        key_prefix="hya_live_test1234",
        client_name="test client",
        scopes=frozenset(scopes),
        engine_company_id=None,
    )


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeHealthService:
    """Stand-in for HealthService with canned probe results."""

    def __init__(self, *, db_ok: bool = True, tryton_ok: bool = True) -> None:
        self._db_ok = db_ok
        self._tryton_ok = tryton_ok

    def _checks(self) -> list[ComponentStatus]:
        return [
            ComponentStatus(
                name="hesabyar_database",
                status="ok" if self._db_ok else "error",
                detail=None if self._db_ok else "Database connection failed",
            ),
            ComponentStatus(
                name="tryton_adapter",
                status="ok" if self._tryton_ok else "error",
                detail=None if self._tryton_ok else "Tryton engine is unreachable",
            ),
        ]

    def readiness(self) -> ReadinessResponse:
        checks = self._checks()
        ready = all(check.status == "ok" for check in checks)
        return ReadinessResponse(status="ready" if ready else "not_ready", checks=checks)

    def installation_status(self) -> InstallationResponse:
        checks = self._checks()
        return InstallationResponse(
            environment="test",
            database=checks[0],
            migration=MigrationStatus(
                current_revision="0001",
                head_revision="0001",
                up_to_date=True,
                status="ok",
            ),
            tryton=TrytonStatus(
                status="ok" if self._tryton_ok else "error",
                release="8.2",
                database="hesabyar_tryton_test",
                installed_modules=[
                    TrytonModuleInfo(name="account", version="8.2.2"),
                    TrytonModuleInfo(name="ir", version=None),
                ],
            ),
        )


class ExplodingHealthService:
    """Health service that raises with secret-looking content in the message.

    Proves the 500 handler never leaks exception text into a response.
    """

    def readiness(self) -> ReadinessResponse:
        raise RuntimeError("boom: postgresql://user:hunter2@db.internal:5432/x /private/path")

    def installation_status(self) -> InstallationResponse:
        raise RuntimeError("boom: api_key=sk-live-secret /private/path")


def _client_with(
    *,
    service: object | None = None,
    settings: AppSettings | None = None,
    auth: AuthenticatedClient | None = None,
    raise_server_exceptions: bool = True,
) -> TestClient:
    """Build a TestClient with the given dependency overrides."""
    app = create_app()
    if service is not None:
        app.dependency_overrides[get_health_service] = lambda: service
    if settings is not None:
        app.dependency_overrides[get_settings_dep] = lambda: settings
    if auth is not None:
        app.dependency_overrides[get_authenticated_client] = lambda: auth
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def _settings(*, env: Environment = Environment.DEV) -> AppSettings:
    """Settings for the fake-service tests."""
    return AppSettings(env=env)


_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


def test_health_returns_ok_and_request_id() -> None:
    client = _client_with(settings=_settings(env=Environment.TEST))
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "hesabyar",
        "version": __version__,
        "environment": "test",
    }
    request_id = response.headers.get("X-Request-ID")
    assert request_id is not None
    assert _UUID_RE.match(request_id), f"expected a UUID request id, got {request_id!r}"
    # The generated id is a valid UUID.
    uuid.UUID(request_id)


def test_health_echoes_client_request_id() -> None:
    client = _client_with()
    response = client.get("/api/v1/health", headers={"X-Request-ID": "client-supplied-id"})
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "client-supplied-id"


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------


def test_readiness_ready_when_all_checks_ok() -> None:
    client = _client_with(service=FakeHealthService())
    response = client.get("/api/v1/readiness")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert [check["name"] for check in body["checks"]] == ["hesabyar_database", "tryton_adapter"]
    assert all(check["status"] == "ok" for check in body["checks"])
    assert response.headers.get("X-Request-ID")


def test_readiness_not_ready_when_database_down() -> None:
    client = _client_with(service=FakeHealthService(db_ok=False))
    response = client.get("/api/v1/readiness")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    checks = {check["name"]: check for check in body["checks"]}
    assert checks["hesabyar_database"]["status"] == "error"
    assert checks["tryton_adapter"]["status"] == "ok"
    # The error response still carries a request id.
    assert response.headers.get("X-Request-ID")


def test_readiness_not_ready_when_tryton_down() -> None:
    client = _client_with(service=FakeHealthService(tryton_ok=False))
    response = client.get("/api/v1/readiness")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    checks = {check["name"]: check for check in body["checks"]}
    assert checks["tryton_adapter"]["status"] == "error"


# ---------------------------------------------------------------------------
# Installation (admin-only: requires the settings:manage scope, US-103)
# ---------------------------------------------------------------------------


def test_installation_requires_authentication() -> None:
    client = _client_with(service=FakeHealthService(), settings=_settings())
    response = client.get("/api/v1/installation")
    assert response.status_code == 401
    body = response.json()
    assert body["code"] == "UNAUTHORIZED"
    assert body["status"] == 401
    assert response.headers["Content-Type"].startswith("application/problem+json")
    assert response.headers.get("X-Request-ID")


def test_installation_rejects_key_without_settings_scope() -> None:
    client = _client_with(
        service=FakeHealthService(),
        settings=_settings(),
        auth=_fake_client(scopes=("company:read",)),
    )
    response = client.get("/api/v1/installation")
    assert response.status_code == 403
    body = response.json()
    assert body["code"] == "FORBIDDEN"
    assert "settings:manage" in body["detail"]


def test_installation_accepts_key_with_settings_scope() -> None:
    client = _client_with(
        service=FakeHealthService(),
        settings=_settings(),
        auth=_fake_client(),
    )
    response = client.get("/api/v1/installation")
    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "hesabyar"
    assert body["version"] == __version__
    assert body["migration"] == {
        "current_revision": "0001",
        "head_revision": "0001",
        "up_to_date": True,
        "status": "ok",
    }
    assert body["tryton"]["release"] == "8.2"
    assert body["tryton"]["database"] == "hesabyar_tryton_test"
    assert {"name": "account", "version": "8.2.2"} in body["tryton"]["installed_modules"]
    assert response.headers.get("X-Request-ID")


# ---------------------------------------------------------------------------
# Error envelope
# ---------------------------------------------------------------------------


def test_unknown_route_returns_problem_details() -> None:
    client = _client_with()
    response = client.get("/api/v1/no-such-route")
    assert response.status_code == 404
    assert response.headers["Content-Type"].startswith("application/problem+json")
    body = response.json()
    assert body["status"] == 404
    assert body["code"] == "NOT_FOUND"
    assert body["title"] == "Not Found"
    assert body["detail"]
    assert body["type"].startswith("https://hesabyar.local/problems/")
    # The envelope carries the same request id as the header.
    assert body["request_id"] == response.headers["X-Request-ID"]
    assert "traceback" not in response.text.lower()


def test_unhandled_error_returns_generic_problem_without_leak() -> None:
    client = _client_with(service=ExplodingHealthService(), raise_server_exceptions=False)
    response = client.get("/api/v1/readiness")
    assert response.status_code == 500
    assert response.headers["Content-Type"].startswith("application/problem+json")
    body = response.json()
    assert body["status"] == 500
    assert body["code"] == "INTERNAL_ERROR"
    assert body["detail"] == "An unexpected error occurred. Please retry later."
    # Nothing from the exception (secret, URL, path) may leak.
    for leaked in ("boom", "hunter2", "postgresql://", "/private/path", "Traceback"):
        assert leaked not in response.text, f"leaked {leaked!r} in 500 response"
    assert response.headers.get("X-Request-ID")


# ---------------------------------------------------------------------------
# OpenAPI
# ---------------------------------------------------------------------------


def test_openapi_lists_health_endpoints() -> None:
    client = _client_with()
    response = client.get("/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/api/v1/health" in paths
    assert "/api/v1/readiness" in paths
    assert "/api/v1/installation" in paths
    assert response.json()["info"]["version"] == __version__


def test_docs_available() -> None:
    client = _client_with()
    response = client.get("/docs")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Settings (Tryton + admin token)
# ---------------------------------------------------------------------------


def test_tryton_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HESABYAR_TRYTON__BASE_URL", raising=False)
    monkeypatch.delenv("HESABYAR_TRYTON__DATABASE", raising=False)
    monkeypatch.delenv("HESABYAR_TRYTON__API_KEY", raising=False)
    monkeypatch.delenv("HESABYAR_AUTH__API_KEY_PEPPER", raising=False)
    settings = AppSettings()
    assert settings.tryton.base_url == "http://localhost:8000"
    assert settings.tryton.database == "hesabyar_tryton_test"
    assert settings.tryton.api_key == "hesabyar-dev-rest-key"
    assert settings.auth.api_key_pepper == DEV_API_KEY_PEPPER


def test_tryton_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HESABYAR_TRYTON__BASE_URL", "http://tryton.internal:8000")
    monkeypatch.setenv("HESABYAR_TRYTON__DATABASE", "hesabyar_prod")
    monkeypatch.setenv("HESABYAR_TRYTON__API_KEY", "prod-key")
    monkeypatch.setenv("HESABYAR_AUTH__API_KEY_PEPPER", "env-pepper")
    settings = AppSettings()
    assert settings.tryton.base_url == "http://tryton.internal:8000"
    assert settings.tryton.database == "hesabyar_prod"
    assert settings.tryton.api_key == "prod-key"
    assert settings.auth.api_key_pepper == "env-pepper"


# ---------------------------------------------------------------------------
# Architecture guard (spec §6.3): no framework imports in the domain layer
# ---------------------------------------------------------------------------

_FORBIDDEN_DOMAIN_IMPORTS = (
    "fastapi",
    "starlette",
    "uvicorn",
    "sqlalchemy",
    "alembic",
    "httpx",
    "psycopg",
)


def _imported_top_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    return modules


def _imported_internal_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return {name for name in modules if name == "hesabyar" or name.startswith("hesabyar.")}


def test_domain_layer_has_no_framework_or_infrastructure_imports() -> None:
    """Spec §6.3: domain never imports FastAPI/SQLAlchemy/Tryton/HTTP code."""
    package = REPO_ROOT / "src" / "hesabyar" / "domain"
    assert package.is_dir()
    for path in sorted(package.rglob("*.py")):
        forbidden = _imported_top_modules(path) & set(_FORBIDDEN_DOMAIN_IMPORTS)
        assert not forbidden, f"{path.relative_to(REPO_ROOT)} imports {sorted(forbidden)}"
        internal = _imported_internal_modules(path)
        outside_domain = {
            name
            for name in internal
            if not (name == "hesabyar" or name.startswith("hesabyar.domain"))
        }
        assert not outside_domain, (
            f"{path.relative_to(REPO_ROOT)} imports outside the domain layer: "
            f"{sorted(outside_domain)}"
        )


# ---------------------------------------------------------------------------
# Live tests (skip cleanly when PostgreSQL / trytond are down)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def live_services() -> None:
    """Skip the module's live tests unless both services answer."""
    settings = AppSettings()
    engine = create_engine(
        settings.database.sqlalchemy_url,
        poolclass=NullPool,
        connect_args={"connect_timeout": 3},
    )
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception:
        pytest.skip("HesabYar PostgreSQL is not reachable (start it per docs/dev-setup.md)")
    finally:
        engine.dispose()
    rest_client = TrytonRestClient(
        settings.tryton.base_url,
        settings.tryton.database,
        settings.tryton.api_key,
        timeout=5.0,
    )
    try:
        if not rest_client.ping():
            pytest.skip("trytond is not reachable at the configured URL")
    finally:
        rest_client.close()


def test_live_readiness_returns_ready(live_services: None) -> None:
    client = TestClient(create_app(settings=AppSettings()))
    response = client.get("/api/v1/readiness")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    checks = {check["name"]: check["status"] for check in body["checks"]}
    assert checks == {"hesabyar_database": "ok", "tryton_adapter": "ok"}


def test_live_installation_reports_real_state(live_services: None) -> None:
    settings = AppSettings()
    # Provision a real API key with the settings:manage scope, using the
    # same pepper the app's auth dependency reads from these settings.
    engine = create_engine(settings.database.sqlalchemy_url, poolclass=NullPool)
    tenant_id = str(uuid.uuid4())
    session = sessionmaker(bind=engine)()
    try:
        tenant = TenantModel(
            id=tenant_id,
            slug=f"live-install-{uuid.uuid4().hex[:8]}",
            name="Live Installation Test",
        )
        session.add(tenant)
        session.flush()
        client_row = ApiClientModel(
            id=str(uuid.uuid4()), tenant_id=tenant_id, name="live-install"
        )
        session.add(client_row)
        session.flush()
        service = ApiKeyService(ApiKeyCrypto(settings.auth.api_key_pepper))
        issued = service.issue_key(
            session,
            client=client_row,
            name="live-install key",
            scopes=["settings:manage"],
            installation_id=INSTALLATION_ID,
        )
        session.commit()
        secret = issued.secret
    finally:
        session.close()
    try:
        client = TestClient(create_app(settings=settings))
        # No key -> 401, even against the live app.
        assert client.get("/api/v1/installation").status_code == 401
        response = client.get(
            "/api/v1/installation", headers={"Authorization": f"Bearer {secret}"}
        )
        assert response.status_code == 200
        body = response.json()
        # Alembic state: revisions are strings; no exact value is asserted
        # because a parallel worker's migration may land mid-run.
        assert isinstance(body["migration"]["current_revision"], str)
        assert isinstance(body["migration"]["head_revision"], str)
        assert body["migration"]["status"] in ("ok", "degraded")
        # Tryton state comes from the live server.
        assert body["tryton"]["status"] == "ok"
        assert body["tryton"]["release"] == "8.2"
        assert body["tryton"]["database"] == settings.tryton.database
        module_names = {m["name"] for m in body["tryton"]["installed_modules"]}
        assert {"account", "company", "currency", "party", "product"} <= module_names
        account = next(m for m in body["tryton"]["installed_modules"] if m["name"] == "account")
        assert account["version"] == "8.2.2"
    finally:
        cleanup = sessionmaker(bind=engine)()
        try:
            cleanup.query(TenantModel).filter(TenantModel.id == tenant_id).delete(
                synchronize_session=False
            )
            cleanup.commit()
        finally:
            cleanup.close()
            engine.dispose()


def test_live_health_endpoint(live_services: None) -> None:
    client = TestClient(create_app(settings=AppSettings()))
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
