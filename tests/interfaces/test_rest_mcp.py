"""REST tests for the MCP client-management routes (US-606).

Real-DB tests (like the MCP tests): the app runs against the test
Postgres with dependency overrides for auth (a canned operator
client), the DB session and settings — the service, the routers and
both audit writers run for real. The secret is verified end to end:
a created client's secret authenticates through ``ApiKeyService``,
and a rotated/revoked key stops authenticating.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from hesabyar.infrastructure.auth import ApiKeyCrypto, ApiKeyService
from hesabyar.infrastructure.auth.errors import AuthenticationError
from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.infrastructure.config import AppSettings
from hesabyar.infrastructure.persistence.postgres.models import (
    AiAccessAuditModel,
    ApiClientModel,
    AuditEventModel,
    Base,
    EngineCompanyModel,
    EngineConnectionModel,
    TenantModel,
)
from hesabyar.interfaces.rest.app import create_app
from hesabyar.interfaces.rest.dependencies import (
    get_authenticated_client,
    get_db_session,
    get_settings_dep,
)
from tests.mcp.conftest import TEST_DB_URL

INSTALLATION_ID = "00000000-0000-0000-0000-000000000606"


@dataclass
class World:
    """Provisioned tenant, operator client and companies for one test."""

    tenant_id: str
    other_tenant_id: str
    operator_client_id: str
    company_a_id: str
    company_a_external_id: str
    company_b_id: str
    company_c_id: str  # belongs to the OTHER tenant


@pytest.fixture(scope="module")
def engine() -> Generator[Engine, None, None]:
    """Module engine; creates all tables (including mcp_authorized_clients)."""
    eng = create_engine(TEST_DB_URL, echo=False)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def settings() -> AppSettings:
    """Fresh settings (dev pepper) — the same pepper the service uses."""
    return AppSettings()


@pytest.fixture()
def world(engine: Engine) -> Generator[World, None, None]:
    """Provision two tenants, an operator client and three companies."""
    session = sessionmaker(bind=engine)()
    tenant: TenantModel | None = None
    other_tenant: TenantModel | None = None
    operator: ApiClientModel | None = None
    connection_ids: list[str] = []
    try:
        tenant = TenantModel(
            id=str(uuid.uuid4()), slug=f"mcp-rest-{uuid.uuid4().hex[:8]}", name="MCP REST Tenant"
        )
        other_tenant = TenantModel(
            id=str(uuid.uuid4()),
            slug=f"mcp-rest-{uuid.uuid4().hex[:8]}",
            name="MCP REST Other Tenant",
        )
        session.add_all([tenant, other_tenant])
        session.flush()
        operator = ApiClientModel(
            id=str(uuid.uuid4()), tenant_id=tenant.id, name="mcp rest operator"
        )
        session.add(operator)
        session.flush()

        def _company(tenant_id: str, external_id: str) -> EngineCompanyModel:
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
                tenant_id=tenant_id,
                engine_connection_id=connection.id,
                external_company_id=external_id,
                company_name=f"Company {external_id}",
            )
            session.add(row)
            session.flush()
            return row

        company_a = _company(tenant.id, "301")
        company_b = _company(tenant.id, "302")
        company_c = _company(other_tenant.id, "303")
        session.commit()
        yield World(
            tenant_id=tenant.id,
            other_tenant_id=other_tenant.id,
            operator_client_id=operator.id,
            company_a_id=company_a.id,
            company_a_external_id=company_a.external_company_id,
            company_b_id=company_b.id,
            company_c_id=company_c.id,
        )
    finally:
        session.close()
        cleanup = sessionmaker(bind=engine)()
        try:
            tenant_ids = [t.id for t in (tenant, other_tenant) if t is not None]
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


def _operator(world: World, scopes: tuple[str, ...] = ("api_keys:manage",)) -> AuthenticatedClient:
    """The canned operator identity for the dependency override."""
    return AuthenticatedClient(
        client_id=world.operator_client_id,
        tenant_id=world.tenant_id,
        installation_id=INSTALLATION_ID,
        key_id="operator-key",
        key_prefix="hya_live_operator",
        client_name="mcp rest operator",
        scopes=frozenset(scopes),
        engine_company_id=None,
    )


@pytest.fixture()
def api(
    engine: Engine, settings: AppSettings, world: World
) -> Generator[TestClient, None, None]:
    """TestClient with auth + session + settings overridden (operator key)."""
    app = create_app()

    def _session_override() -> Generator[Session, None, None]:
        session = sessionmaker(bind=engine)()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_authenticated_client] = lambda: _operator(world)
    app.dependency_overrides[get_db_session] = _session_override
    app.dependency_overrides[get_settings_dep] = lambda: settings
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def _api_with_scopes(
    engine: Engine, settings: AppSettings, world: World, scopes: tuple[str, ...]
) -> TestClient:
    """A TestClient whose operator key carries ``scopes``."""
    app = create_app()

    def _session_override() -> Generator[Session, None, None]:
        session = sessionmaker(bind=engine)()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_authenticated_client] = lambda: _operator(world, scopes)
    app.dependency_overrides[get_db_session] = _session_override
    app.dependency_overrides[get_settings_dep] = lambda: settings
    return TestClient(app)


def _api_without_auth_override(engine: Engine, settings: AppSettings) -> TestClient:
    """A TestClient with the REAL auth dependency (for the 401 test)."""
    app = create_app()

    def _session_override() -> Generator[Session, None, None]:
        session = sessionmaker(bind=engine)()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db_session] = _session_override
    app.dependency_overrides[get_settings_dep] = lambda: settings
    return TestClient(app)


def _create(
    api: TestClient, name: str = "claude-desktop", **overrides: object
) -> dict[str, object]:
    """Create a client; assert 201 and return the body."""
    body: dict[str, object] = {
        "name": name,
        "description": "The team's Claude Desktop",
        "scopes": ["mcp:connect", "company:read", "reports:read"],
    }
    body.update(overrides)
    response = api.post("/api/v1/mcp/clients", json=body)
    assert response.status_code == 201, response.text
    data: dict[str, object] = response.json()
    return data


def _audit_events(session_factory: sessionmaker[Session], tenant_id: str) -> list[AuditEventModel]:
    session = session_factory()
    try:
        return list(
            session.execute(
                select(AuditEventModel)
                .where(AuditEventModel.tenant_id == tenant_id)
                .order_by(AuditEventModel.occurred_at)
            )
            .scalars()
            .all()
        )
    finally:
        session.close()


def _ai_audit_rows(
    session_factory: sessionmaker[Session], tenant_id: str
) -> list[AiAccessAuditModel]:
    session = session_factory()
    try:
        return list(
            session.execute(
                select(AiAccessAuditModel)
                .where(AiAccessAuditModel.tenant_id == tenant_id)
                .order_by(AiAccessAuditModel.created_at)
            )
            .scalars()
            .all()
        )
    finally:
        session.close()


def _authenticate(session_factory: sessionmaker[Session], settings: AppSettings, secret: str) -> AuthenticatedClient:
    """Authenticate a secret through the real API-key service."""
    session = session_factory()
    try:
        return ApiKeyService(ApiKeyCrypto(settings.auth.api_key_pepper)).authenticate(
            session, secret=secret
        )
    finally:
        session.close()


# -- create ----------------------------------------------------------------------


def test_create_client_returns_secret_once(
    api: TestClient, engine: Engine, settings: AppSettings, world: World
) -> None:
    """Create returns the secret exactly once; later reads never do."""
    session_factory = sessionmaker(bind=engine)
    data = _create(api)
    secret = data["secret"]
    assert isinstance(secret, str) and secret.startswith("hya_live_")
    assert data["key_prefix"] == secret[:16]
    assert data["name"] == "claude-desktop"
    assert data["scopes"] == ["company:read", "mcp:connect", "reports:read"]
    assert data["company_id"] is None
    assert data["status"] == "active"
    assert data["revoked_at"] is None

    # The secret is a real API key: it authenticates with the grant.
    identity = _authenticate(session_factory, settings, secret)
    assert identity.tenant_id == world.tenant_id
    assert identity.scopes == frozenset({"mcp:connect", "company:read", "reports:read"})
    assert identity.engine_company_id is None
    # The api client id is what the audit log records (the UI filters by it).
    assert data["api_client_id"] == identity.client_id

    # A later read never returns the secret.
    response = api.get(f"/api/v1/mcp/clients/{data['id']}")
    assert response.status_code == 200
    assert "secret" not in response.json()
    response = api.get("/api/v1/mcp/clients")
    assert response.status_code == 200
    assert all("secret" not in item for item in response.json())

    # Both audit records exist: the REST event (who acted) and the
    # management row (what changed).
    events = _audit_events(session_factory, world.tenant_id)
    assert len(events) == 1
    assert events[0].action == "mcp.client.create"
    assert events[0].resource_type == "mcp_client"
    assert events[0].resource_id == data["id"]
    assert events[0].api_client_id == world.operator_client_id
    assert events[0].outcome == "success"
    rows = _ai_audit_rows(session_factory, world.tenant_id)
    assert len(rows) == 1
    assert rows[0].tool == "mcp.client.create"
    assert rows[0].kind == "management"
    assert rows[0].transport == "rest"
    assert rows[0].outcome == "allowed"


def test_create_client_with_company_grant(
    api: TestClient, engine: Engine, settings: AppSettings, world: World
) -> None:
    """A company grant (by engine id) binds the key to that company."""
    session_factory = sessionmaker(bind=engine)
    data = _create(api, company_id=world.company_a_external_id)
    assert data["company_id"] == world.company_a_id
    assert data["company_name"] == "Company 301"
    secret = data["secret"]
    assert isinstance(secret, str)
    identity = _authenticate(session_factory, settings, secret)
    assert identity.engine_company_id == world.company_a_id


def test_create_client_duplicate_name_409(api: TestClient) -> None:
    """A second client with the same name is a 409."""
    _create(api, name="dup")
    response = api.post(
        "/api/v1/mcp/clients",
        json={"name": "dup", "scopes": ["mcp:connect"]},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "MCP_CLIENT_NAME_TAKEN"


def test_create_client_unknown_scope_400(api: TestClient) -> None:
    """A scope outside the catalog is a 400."""
    response = api.post(
        "/api/v1/mcp/clients",
        json={"name": "bad-scope", "scopes": ["mcp:connect", "nope:read"]},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "UNKNOWN_SCOPE"


def test_create_client_without_mcp_connect_400(api: TestClient) -> None:
    """Scopes without mcp:connect cannot connect — rejected at creation."""
    response = api.post(
        "/api/v1/mcp/clients",
        json={"name": "no-mcp", "scopes": ["reports:read"]},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_SCOPES"


def test_create_client_unknown_company_404(api: TestClient) -> None:
    """A company grant naming no company of the tenant is a 404."""
    response = api.post(
        "/api/v1/mcp/clients",
        json={"name": "bad-company", "scopes": ["mcp:connect"], "company_id": "no-such-company"},
    )
    assert response.status_code == 404
    assert response.json()["code"] == "COMPANY_NOT_FOUND"


def test_create_client_other_tenant_company_404(api: TestClient, world: World) -> None:
    """A company of another tenant looks exactly like a nonexistent one."""
    response = api.post(
        "/api/v1/mcp/clients",
        json={"name": "foreign", "scopes": ["mcp:connect"], "company_id": world.company_c_id},
    )
    assert response.status_code == 404
    assert response.json()["code"] == "COMPANY_NOT_FOUND"


# -- list / get ------------------------------------------------------------------


def test_list_clients_newest_first_and_revoked_filter(api: TestClient) -> None:
    """List returns newest first; include_revoked=false hides revoked rows."""
    first = _create(api, name="first")
    _create(api, name="second")
    response = api.post(f"/api/v1/mcp/clients/{first['id']}/revoke", json={})
    assert response.status_code == 200
    response = api.get("/api/v1/mcp/clients")
    assert response.status_code == 200
    names = [item["name"] for item in response.json()]
    assert names == ["second", "first"]
    statuses = {item["name"]: item["status"] for item in response.json()}
    assert statuses == {"first": "revoked", "second": "active"}
    response = api.get("/api/v1/mcp/clients?include_revoked=false")
    assert [item["name"] for item in response.json()] == ["second"]


def test_get_client_unknown_404(api: TestClient) -> None:
    """An unknown client id is a 404."""
    response = api.get("/api/v1/mcp/clients/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404
    assert response.json()["code"] == "MCP_CLIENT_NOT_FOUND"


# -- rotate / revoke -------------------------------------------------------------


def test_rotate_client_replaces_key(
    api: TestClient, engine: Engine, settings: AppSettings, world: World
) -> None:
    """Rotate issues a new secret; the old key stops working at once."""
    session_factory = sessionmaker(bind=engine)
    data = _create(api, company_id=world.company_a_external_id)
    old_secret = data["secret"]
    assert isinstance(old_secret, str)
    old_prefix = data["key_prefix"]
    response = api.post(f"/api/v1/mcp/clients/{data['id']}/rotate")
    assert response.status_code == 200
    rotated = response.json()
    new_secret = rotated["secret"]
    assert isinstance(new_secret, str) and new_secret.startswith("hya_live_")
    assert new_secret != old_secret
    assert rotated["key_prefix"] != old_prefix
    assert rotated["key_prefix"] == new_secret[:16]
    assert rotated["scopes"] == data["scopes"]
    assert rotated["company_id"] == world.company_a_id
    assert rotated["status"] == "active"

    with pytest.raises(AuthenticationError):
        _authenticate(session_factory, settings, old_secret)
    identity = _authenticate(session_factory, settings, new_secret)
    assert identity.engine_company_id == world.company_a_id
    assert identity.scopes == frozenset({"mcp:connect", "company:read", "reports:read"})

    rows = _ai_audit_rows(session_factory, world.tenant_id)
    assert [row.tool for row in rows] == ["mcp.client.create", "mcp.client.rotate"]


def test_rotate_revoked_client_409(api: TestClient) -> None:
    """A revoked client cannot rotate."""
    data = _create(api)
    response = api.post(f"/api/v1/mcp/clients/{data['id']}/revoke", json={})
    assert response.status_code == 200
    response = api.post(f"/api/v1/mcp/clients/{data['id']}/rotate")
    assert response.status_code == 409
    assert response.json()["code"] == "MCP_CLIENT_REVOKED"


def test_revoke_client_is_idempotent(
    api: TestClient, engine: Engine, settings: AppSettings, world: World
) -> None:
    """Revoke stops the key at once; a second revoke is a no-op."""
    session_factory = sessionmaker(bind=engine)
    data = _create(api)
    secret = data["secret"]
    assert isinstance(secret, str)
    response = api.post(
        f"/api/v1/mcp/clients/{data['id']}/revoke", json={"reason": "host decommissioned"}
    )
    assert response.status_code == 200
    revoked = response.json()
    assert revoked["status"] == "revoked"
    assert revoked["revoked_at"] is not None
    assert revoked["revoked_reason"] == "host decommissioned"
    with pytest.raises(AuthenticationError):
        _authenticate(session_factory, settings, secret)

    # A second revoke returns the same state and writes no second row.
    response = api.post(f"/api/v1/mcp/clients/{data['id']}/revoke", json={})
    assert response.status_code == 200
    assert response.json()["status"] == "revoked"
    rows = _ai_audit_rows(session_factory, world.tenant_id)
    assert [row.tool for row in rows] == ["mcp.client.create", "mcp.client.revoke"]


def test_revoke_unknown_client_404(api: TestClient) -> None:
    """Revoking an unknown client is a 404."""
    response = api.post(
        "/api/v1/mcp/clients/00000000-0000-0000-0000-000000000000/revoke", json={}
    )
    assert response.status_code == 404
    assert response.json()["code"] == "MCP_CLIENT_NOT_FOUND"


# -- audit list ------------------------------------------------------------------


def test_mcp_audit_list_filters_and_pagination(api: TestClient, world: World) -> None:
    """The AI access log lists lifecycle rows, filters and paginates."""
    first = _create(api, name="audit-a")
    _create(api, name="audit-b")
    response = api.post(f"/api/v1/mcp/clients/{first['id']}/revoke", json={})
    assert response.status_code == 200

    response = api.get("/api/v1/mcp/audit")
    assert response.status_code == 200
    page = response.json()
    assert page["next_cursor"] is None
    assert len(page["items"]) == 3
    assert {item["tool"] for item in page["items"]} == {"mcp.client.create", "mcp.client.revoke"}
    assert all(item["kind"] == "management" for item in page["items"])
    assert all(item["transport"] == "rest" for item in page["items"])
    assert all(item["outcome"] == "allowed" for item in page["items"])

    response = api.get("/api/v1/mcp/audit?tool=mcp.client.revoke")
    assert len(response.json()["items"]) == 1
    # Filtering by the first client's api client id keeps its two rows
    # (create + revoke) and drops the second client's create row.
    response = api.get(f"/api/v1/mcp/audit?client={first['api_client_id']}")
    assert len(response.json()["items"]) == 2
    response = api.get("/api/v1/mcp/audit?kind=tool")
    assert response.json()["items"] == []
    response = api.get("/api/v1/mcp/audit?kind=bogus")
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_FILTER"
    response = api.get("/api/v1/mcp/audit?cursor=not-a-cursor")
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_CURSOR"

    # Keyset pagination: limit=1 walks the three rows in order.
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(3):
        url = "/api/v1/mcp/audit?limit=1"
        if cursor is not None:
            url += f"&cursor={cursor}"
        response = api.get(url)
        assert response.status_code == 200
        page = response.json()
        assert len(page["items"]) == 1
        seen.append(str(page["items"][0]["id"]))
        cursor = page["next_cursor"]
    assert len(set(seen)) == 3
    assert cursor is None


# -- authorization ---------------------------------------------------------------


def test_endpoints_require_api_keys_manage_scope(
    engine: Engine, settings: AppSettings, world: World
) -> None:
    """A key without api_keys:manage gets 403 on every MCP endpoint."""
    client = _api_with_scopes(engine, settings, world, ("reports:read",))
    assert client.get("/api/v1/mcp/clients").status_code == 403
    assert (
        client.post(
            "/api/v1/mcp/clients", json={"name": "x", "scopes": ["mcp:connect"]}
        ).status_code
        == 403
    )
    assert client.get("/api/v1/mcp/audit").status_code == 403


def test_endpoints_require_authentication(engine: Engine, settings: AppSettings) -> None:
    """No API key at all is a 401 (real auth path, no override)."""
    client = _api_without_auth_override(engine, settings)
    response = client.get("/api/v1/mcp/clients")
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"


def test_other_tenant_client_is_invisible(
    api: TestClient, engine: Engine, settings: AppSettings, world: World
) -> None:
    """A client of another tenant looks exactly like a nonexistent one."""
    # Create a client, then re-point the operator at the other tenant
    # (same app shape, different tenant in the canned identity).
    data = _create(api)
    other_app = create_app()

    def _session_override() -> Generator[Session, None, None]:
        session = sessionmaker(bind=engine)()
        try:
            yield session
        finally:
            session.close()

    other_app.dependency_overrides[get_authenticated_client] = lambda: AuthenticatedClient(
        client_id=world.operator_client_id,
        tenant_id=world.other_tenant_id,
        installation_id=INSTALLATION_ID,
        key_id="operator-key",
        key_prefix="hya_live_operator",
        client_name="mcp rest operator",
        scopes=frozenset({"api_keys:manage"}),
        engine_company_id=None,
    )
    other_app.dependency_overrides[get_db_session] = _session_override
    other_app.dependency_overrides[get_settings_dep] = lambda: settings
    other_api = TestClient(other_app)
    response = other_api.get(f"/api/v1/mcp/clients/{data['id']}")
    assert response.status_code == 404
    assert response.json()["code"] == "MCP_CLIENT_NOT_FOUND"
    response = other_api.get("/api/v1/mcp/clients")
    assert response.json() == []
    response = other_api.get("/api/v1/mcp/audit")
    assert response.json()["items"] == []
