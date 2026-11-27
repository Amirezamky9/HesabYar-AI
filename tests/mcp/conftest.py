"""Shared fixtures for the MCP tests (US-601).

Follows the REST auth tests' provisioning pattern
(``tests/interfaces/test_rest_auth.py``): two tenants, three companies,
five keys. Provisioned rows are committed (the MCP server's sessions
must see them) and removed in teardown by deleting the tenants
(cascades to clients/keys/companies) plus the connections and the
world's audit rows.

Every world gets a UNIQUE fixture-tool name (``mcp_echo_<random>``):
audit assertions filter on it, so parallel CI jobs sharing the test
database cannot see each other's audit rows.
"""

from __future__ import annotations

import datetime
import os
import uuid
from collections.abc import Callable, Generator
from dataclasses import dataclass

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from hesabyar.infrastructure.auth import ApiKeyCrypto, ApiKeyService
from hesabyar.infrastructure.config import AppSettings
from hesabyar.infrastructure.persistence.postgres.models import (
    AiAccessAuditModel,
    ApiClientModel,
    Base,
    EngineCompanyModel,
    EngineConnectionModel,
    TenantModel,
)

TEST_DB_URL = os.getenv(
    "HESABYAR_DATABASE_URL",
    "postgresql+psycopg://hesabyar:hesabyar_pass@hesabyar-postgres:5432/hesabyar_test",
)

INSTALLATION_ID = "00000000-0000-0000-0000-000000000002"


@dataclass
class World:
    """Provisioned tenants, companies and keys for one test."""

    tenant_id: str
    other_tenant_id: str
    client_id: str
    company_a_id: str
    company_a_external_id: str
    company_b_id: str
    company_b_external_id: str
    company_c_id: str  # belongs to the OTHER tenant
    tool_name: str  # unique per world; audit assertions filter on it
    key_full_secret: str  # mcp:connect + company:read + reports:read, unrestricted
    key_no_mcp_secret: str  # company:read + reports:read, NO mcp:connect
    key_company_only_secret: str  # mcp:connect + company:read (no reports:read)
    key_restricted_a_secret: str  # mcp:connect + company:read + reports:read, restricted to company A
    key_revoked_secret: str
    key_expired_secret: str
    key_full_id: str
    key_restricted_a_id: str


@pytest.fixture(scope="module")
def engine() -> Generator[Engine, None, None]:
    """Module engine; creates all tables (including ai_access_audit)."""
    eng = create_engine(TEST_DB_URL, echo=False)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def settings() -> AppSettings:
    """Fresh settings (dev pepper) — the same pepper the server uses."""
    return AppSettings()


@pytest.fixture()
def session_factory(engine: Engine) -> Callable[[], Session]:
    """Session factory bound to the test engine (what McpContext takes)."""
    return sessionmaker(bind=engine)


@pytest.fixture()
def world(
    engine: Engine, settings: AppSettings
) -> Generator[World, None, None]:
    """Provision two tenants, three companies and six keys; clean up after."""
    session = sessionmaker(bind=engine)()
    service = ApiKeyService(ApiKeyCrypto(settings.auth.api_key_pepper))
    tenant: TenantModel | None = None
    other_tenant: TenantModel | None = None
    client: ApiClientModel | None = None
    connection_ids: list[str] = []
    tool_name = f"mcp_echo_{uuid.uuid4().hex[:8]}"
    try:
        tenant = TenantModel(
            id=str(uuid.uuid4()), slug=f"mcp-{uuid.uuid4().hex[:8]}", name="MCP Tenant 1"
        )
        other_tenant = TenantModel(
            id=str(uuid.uuid4()), slug=f"mcp-{uuid.uuid4().hex[:8]}", name="MCP Tenant 2"
        )
        session.add_all([tenant, other_tenant])
        session.flush()
        client = ApiClientModel(
            id=str(uuid.uuid4()), tenant_id=tenant.id, name="mcp test client"
        )
        session.add(client)
        session.flush()

        def _company(tenant_id: str, external_id: str) -> EngineCompanyModel:
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

        def _key(scopes: list[str], **kwargs: object):
            return service.issue_key(
                session,
                client=client,
                name="mcp test key",
                scopes=scopes,
                installation_id=INSTALLATION_ID,
                **kwargs,  # type: ignore[arg-type]
            )

        key_full = _key(["mcp:connect", "company:read", "reports:read"])
        key_no_mcp = _key(["company:read", "reports:read"])
        key_company_only = _key(["mcp:connect", "company:read"])
        key_restricted_a = _key(
            ["mcp:connect", "company:read", "reports:read"],
            engine_company_id=company_a.id,
        )
        key_revoked = _key(["mcp:connect", "company:read", "reports:read"])
        service.revoke_key(session, key=key_revoked.record, reason="test")
        key_expired = _key(
            ["mcp:connect", "company:read", "reports:read"],
            expires_at=datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=1),
        )
        session.commit()
        yield World(
            tenant_id=tenant.id,
            other_tenant_id=other_tenant.id,
            client_id=client.id,
            company_a_id=company_a.id,
            company_a_external_id=company_a.external_company_id,
            company_b_id=company_b.id,
            company_b_external_id=company_b.external_company_id,
            company_c_id=company_c.id,
            tool_name=tool_name,
            key_full_secret=key_full.secret,
            key_no_mcp_secret=key_no_mcp.secret,
            key_company_only_secret=key_company_only.secret,
            key_restricted_a_secret=key_restricted_a.secret,
            key_revoked_secret=key_revoked.secret,
            key_expired_secret=key_expired.secret,
            key_full_id=key_full.record.id,
            key_restricted_a_id=key_restricted_a.record.id,
        )
    finally:
        session.close()
        cleanup = sessionmaker(bind=engine)()
        try:
            tenant_ids = [t.id for t in (tenant, other_tenant) if t is not None]
            if tenant_ids:
                cleanup.query(TenantModel).filter(
                    TenantModel.id.in_(tenant_ids)
                ).delete(synchronize_session=False)
            if connection_ids:
                cleanup.query(EngineConnectionModel).filter(
                    EngineConnectionModel.id.in_(connection_ids)
                ).delete(synchronize_session=False)
            # The world's audit rows (including NULL-client rows from
            # unauthenticated attempts, which no tenant cascade covers).
            cleanup.query(AiAccessAuditModel).filter(
                AiAccessAuditModel.tool == tool_name
            ).delete(synchronize_session=False)
            cleanup.commit()
        finally:
            cleanup.close()
