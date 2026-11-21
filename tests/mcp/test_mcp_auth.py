"""MCP authentication + authorization tests (US-601, spec §13.8).

Two layers: the token verifier directly (valid/revoked/expired/unknown
keys, claim round-trip, fail-closed on missing claims), and the
middleware end to end over in-memory streams (unauthenticated calls,
missing ``mcp:connect``, missing tool scopes, and the SAFETY property:
a caller-provided company id is checked against the grant and a
company outside the grant fails closed).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest
from mcp.shared.exceptions import MCPError
from sqlalchemy.orm import Session

from hesabyar.infrastructure.auth.keys import ApiKeyCrypto
from hesabyar.infrastructure.config import AppSettings
from hesabyar.infrastructure.persistence.postgres.models import AiAccessAuditModel
from hesabyar.interfaces.mcp.auth import (
    CLAIM_TENANT_ID,
    MCP_BASE_SCOPE,
    HesabYarTokenVerifier,
    client_from_access_token,
)
from hesabyar.interfaces.mcp.errors import MCP_ERROR_FORBIDDEN, MCP_ERROR_UNAUTHENTICATED
from tests.mcp.conftest import World
from tests.mcp.harness import audit_rows, connected_client, make_server, tool_text

# ---------------------------------------------------------------------------
# Token verifier (direct)
# ---------------------------------------------------------------------------


def test_verify_token_valid_key(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """A valid key verifies: identity, scopes, claims, resource binding."""
    verifier = HesabYarTokenVerifier(
        session_factory,
        ApiKeyCrypto(settings.auth.api_key_pepper),
        resource_url=settings.mcp.public_url,
    )
    token = asyncio.run(verifier.verify_token(world.key_full_secret))
    assert token is not None
    assert token.client_id == world.client_id
    assert token.scopes == ["company:read", "mcp:connect", "reports:read"]
    assert token.resource == settings.mcp.public_url
    # The token field holds the visible prefix, never the secret.
    assert token.token != world.key_full_secret
    assert world.key_full_secret not in token.model_dump_json()
    assert token.claims is not None
    assert token.claims[CLAIM_TENANT_ID] == world.tenant_id


def test_verify_token_rejects_revoked_expired_unknown(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """Revoked, expired and unknown keys all verify to None (one generic 401)."""
    verifier = HesabYarTokenVerifier(
        session_factory,
        ApiKeyCrypto(settings.auth.api_key_pepper),
        resource_url=settings.mcp.public_url,
    )

    async def _run() -> None:
        assert await verifier.verify_token(world.key_revoked_secret) is None
        assert await verifier.verify_token(world.key_expired_secret) is None
        assert await verifier.verify_token("hya_live_no-such-key-000000000000") is None
        assert await verifier.verify_token("not-a-key") is None

    asyncio.run(_run())


def test_client_from_access_token_round_trip(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """The claims rebuild the exact identity REST would have built."""
    verifier = HesabYarTokenVerifier(
        session_factory,
        ApiKeyCrypto(settings.auth.api_key_pepper),
        resource_url=settings.mcp.public_url,
    )
    token = asyncio.run(verifier.verify_token(world.key_restricted_a_secret))
    assert token is not None
    client = client_from_access_token(token)
    assert client is not None
    assert client.client_id == world.client_id
    assert client.tenant_id == world.tenant_id
    assert client.key_id == world.key_restricted_a_id
    assert client.scopes == frozenset({"mcp:connect", "company:read", "reports:read"})
    assert client.engine_company_id == world.company_a_id
    assert client.has_scope(MCP_BASE_SCOPE)


def test_client_from_access_token_missing_claims_fails_closed(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """A token without HesabYar claims establishes no identity."""
    verifier = HesabYarTokenVerifier(
        session_factory,
        ApiKeyCrypto(settings.auth.api_key_pepper),
        resource_url=settings.mcp.public_url,
    )
    token = asyncio.run(verifier.verify_token(world.key_full_secret))
    assert token is not None
    token.claims = {}
    assert client_from_access_token(token) is None
    token.claims = None
    assert client_from_access_token(token) is None


# ---------------------------------------------------------------------------
# Middleware enforcement over in-memory streams
# ---------------------------------------------------------------------------


def _call_tool_denied_code(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    *,
    bootstrap_secret: str | None,
    arguments: dict[str, object] | None = None,
) -> int:
    """Call the fixture tool; return the MCPError code (the call must fail)."""

    async def _run() -> int:
        server, _ = make_server(
            world, session_factory, settings, bootstrap_secret=bootstrap_secret
        )
        async with connected_client(server) as session:
            with pytest.raises(MCPError) as excinfo:
                await session.call_tool(world.tool_name, arguments or {"message": "hi"})
            return excinfo.value.code

    return asyncio.run(_run())


def test_call_without_identity_fails_unauthenticated(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """No bootstrap client and no bearer token: the call is refused."""
    code = _call_tool_denied_code(
        world, session_factory, settings, bootstrap_secret=None
    )
    assert code == MCP_ERROR_UNAUTHENTICATED
    rows = audit_rows(session_factory, world.tool_name)
    assert len(rows) == 1
    assert rows[0].outcome == "denied"
    assert rows[0].error_code == "unauthenticated"
    assert rows[0].client_id is None


def test_list_tools_without_identity_fails(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """Even tools/list needs an authenticated identity."""

    async def _run() -> int:
        server, _ = make_server(world, session_factory, settings, bootstrap_secret=None)
        async with connected_client(server) as session:
            with pytest.raises(MCPError) as excinfo:
                await session.list_tools()
            return excinfo.value.code

    assert asyncio.run(_run()) == MCP_ERROR_UNAUTHENTICATED


def test_key_without_mcp_connect_denied(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """A key without the mcp:connect base scope cannot call any tool."""
    code = _call_tool_denied_code(
        world, session_factory, settings, bootstrap_secret=world.key_no_mcp_secret
    )
    assert code == MCP_ERROR_FORBIDDEN
    rows = audit_rows(session_factory, world.tool_name)
    assert len(rows) == 1
    assert rows[0].outcome == "denied"
    assert rows[0].error_code == "missing_scope"


def test_tool_scope_enforced(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """The fixture tool needs reports:read; a key without it is denied."""
    code = _call_tool_denied_code(
        world, session_factory, settings, bootstrap_secret=world.key_company_only_secret
    )
    assert code == MCP_ERROR_FORBIDDEN
    rows = audit_rows(session_factory, world.tool_name)
    assert len(rows) == 1
    assert rows[0].outcome == "denied"
    assert rows[0].error_code == "missing_scope"


def test_company_restriction_enforced(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """SAFETY: a key restricted to company A cannot call with company B."""
    code = _call_tool_denied_code(
        world,
        session_factory,
        settings,
        bootstrap_secret=world.key_restricted_a_secret,
        arguments={"message": "hi", "company": world.company_b_id},
    )
    assert code == MCP_ERROR_FORBIDDEN
    rows = audit_rows(session_factory, world.tool_name)
    assert len(rows) == 1
    assert rows[0].outcome == "denied"
    assert rows[0].error_code == "company_forbidden"


def test_company_restriction_allows_own_company(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """The same restricted key calling with company A succeeds."""

    async def _run() -> None:
        server, _ = make_server(
            world,
            session_factory,
            settings,
            bootstrap_secret=world.key_restricted_a_secret,
        )
        async with connected_client(server) as session:
            result = await session.call_tool(
                world.tool_name, {"message": "hi", "company": world.company_a_id}
            )
            assert not result.is_error
            assert tool_text(result) == f"echo:hi:{world.company_a_id}:memory"

    asyncio.run(_run())
    rows = audit_rows(session_factory, world.tool_name)
    assert len(rows) == 1
    assert rows[0].outcome == "allowed"
    assert rows[0].company_id == world.company_a_id


def test_unknown_company_denied(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """A company id naming no row of the tenant is denied (not trusted)."""
    code = _call_tool_denied_code(
        world,
        session_factory,
        settings,
        bootstrap_secret=world.key_full_secret,
        arguments={"message": "hi", "company": "00000000-0000-0000-0000-000000000000"},
    )
    assert code == MCP_ERROR_FORBIDDEN
    rows = audit_rows(session_factory, world.tool_name)
    assert len(rows) == 1
    assert rows[0].outcome == "denied"
    assert rows[0].error_code == "company_not_found"


def test_cross_tenant_company_denied(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """A company of another tenant looks exactly like a nonexistent one."""
    code = _call_tool_denied_code(
        world,
        session_factory,
        settings,
        bootstrap_secret=world.key_full_secret,
        arguments={"message": "hi", "company": world.company_c_id},
    )
    assert code == MCP_ERROR_FORBIDDEN
    rows = audit_rows(session_factory, world.tool_name)
    assert len(rows) == 1
    assert rows[0].outcome == "denied"
    assert rows[0].error_code == "company_not_found"


def test_company_resolves_by_engine_id(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """The company may be named by its engine id, like REST allows."""

    async def _run() -> None:
        server, _ = make_server(
            world, session_factory, settings, bootstrap_secret=world.key_full_secret
        )
        async with connected_client(server) as session:
            result = await session.call_tool(
                world.tool_name,
                {"message": "hi", "company": world.company_b_external_id},
            )
            assert not result.is_error

    asyncio.run(_run())
    rows = audit_rows(session_factory, world.tool_name)
    assert len(rows) == 1
    assert rows[0].outcome == "allowed"
    # The audit records the canonical row id, not the caller's spelling.
    assert rows[0].company_id == world.company_b_id


def test_unknown_tool_call_audited_as_error(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """Calling a tool that does not exist fails and is audited as an error.

    The SDK returns an unknown tool as a CallToolResult with
    ``is_error=True`` — NOT a protocol error (only MCPError
    propagates; ToolError becomes an isError result). The audit
    requirement stands: the middleware audits the error result as
    error/tool_error.
    """

    async def _run() -> None:
        server, _ = make_server(
            world, session_factory, settings, bootstrap_secret=world.key_full_secret
        )
        async with connected_client(server) as session:
            result = await session.call_tool("no_such_tool", {"message": "hi"})
            assert result.is_error
            assert tool_text(result) == "Unknown tool: no_such_tool"

    asyncio.run(_run())
    rows = audit_rows(session_factory, "no_such_tool")
    assert len(rows) == 1
    assert rows[0].outcome == "error"
    assert rows[0].error_code == "tool_error"
    assert rows[0].client_id == world.client_id
    # The world teardown only removes this world's tool rows; remove
    # this one explicitly so it does not leak into the shared test DB.
    session = session_factory()
    try:
        session.query(AiAccessAuditModel).filter(
            AiAccessAuditModel.tool == "no_such_tool",
            AiAccessAuditModel.client_id == world.client_id,
        ).delete(synchronize_session=False)
        session.commit()
    finally:
        session.close()
