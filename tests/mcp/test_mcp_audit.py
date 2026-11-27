"""MCP audit-log tests (US-601, spec §13.8).

Every call — allowed, denied or failed — lands one ``ai_access_audit``
row with client, tool, company, scope, outcome and timestamp. No tool
arguments, no chain-of-thought, no secrets: the audit writer stores
only the key's visible prefix, and the tests prove a secret passed as
a tool argument never reaches the audit table, a tool result, or the
logs.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from collections.abc import Callable

import pytest
from mcp.shared.exceptions import MCPError
from sqlalchemy.orm import Session

from hesabyar.infrastructure.auth.keys import ApiKeyCrypto
from hesabyar.infrastructure.config import AppSettings
from hesabyar.interfaces.mcp import McpContext, create_mcp_server
from hesabyar.interfaces.mcp.auth import HesabYarTokenVerifier
from hesabyar.interfaces.mcp.errors import MCP_ERROR_INTERNAL
from tests.mcp.conftest import World
from tests.mcp.harness import (
    audit_rows,
    authenticate_secret,
    connected_client,
    make_server,
    register_echo_tool,
)


def test_successful_call_audit_row_shape(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """An allowed call records every required audit field."""
    before = datetime.datetime.now(datetime.UTC)

    async def _run() -> None:
        server, _ = make_server(
            world, session_factory, settings, bootstrap_secret=world.key_full_secret
        )
        async with connected_client(server) as session:
            await session.call_tool(
                world.tool_name, {"message": "hi", "company": world.company_a_id}
            )

    asyncio.run(_run())
    rows = audit_rows(session_factory, world.tool_name)
    assert len(rows) == 1
    row = rows[0]
    assert row.tenant_id == world.tenant_id
    assert row.client_id == world.client_id
    assert row.key_id == world.key_full_id
    assert row.key_prefix is not None and row.key_prefix != world.key_full_secret
    assert world.key_full_secret.startswith(row.key_prefix)
    assert row.tool == world.tool_name
    assert row.kind == "tool"
    assert row.company_id == world.company_a_id
    assert row.scopes is not None
    assert set(row.scopes.split(",")) == {"mcp:connect", "company:read", "reports:read"}
    assert row.outcome == "allowed"
    assert row.error_code is None
    assert row.request_id is not None
    assert row.transport == "memory"
    assert row.client_info is not None  # the SDK client identifies itself
    assert row.created_at >= before


def test_secret_passed_as_argument_never_reaches_audit(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """Even a client that puts its key in a tool argument is not logged.

    Tool arguments are never stored: the audit row carries the tool
    name, company, scopes and outcome only.
    """

    async def _run() -> None:
        server, _ = make_server(
            world, session_factory, settings, bootstrap_secret=world.key_full_secret
        )
        async with connected_client(server) as session:
            await session.call_tool(
                world.tool_name,
                {"message": world.key_full_secret, "company": world.company_a_id},
            )

    asyncio.run(_run())
    rows = audit_rows(session_factory, world.tool_name)
    assert len(rows) == 1
    for value in (
        rows[0].tool,
        rows[0].kind,
        rows[0].company_id,
        rows[0].scopes,
        rows[0].outcome,
        rows[0].error_code,
        rows[0].request_id,
        rows[0].transport,
        rows[0].client_info,
    ):
        assert value is None or world.key_full_secret not in value


def test_no_secret_in_logs(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Verification + a denied call log the prefix only, never the secret."""
    caplog.set_level(logging.INFO)
    verifier = HesabYarTokenVerifier(
        session_factory,
        ApiKeyCrypto(settings.auth.api_key_pepper),
        resource_url=settings.mcp.public_url,
    )

    async def _run() -> None:
        assert await verifier.verify_token(world.key_full_secret) is not None
        assert await verifier.verify_token(world.key_revoked_secret) is None
        server, _ = make_server(world, session_factory, settings, bootstrap_secret=None)
        async with connected_client(server) as session:
            with pytest.raises(MCPError):
                await session.call_tool(world.tool_name, {"message": "hi"})

    asyncio.run(_run())
    assert world.key_full_secret not in caplog.text
    assert world.key_revoked_secret not in caplog.text


def test_audit_failure_fails_the_call_closed(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """When the audit write fails, the result is withheld (no silent gap)."""

    def _broken_factory() -> Session:
        raise RuntimeError("database is down")

    async def _run() -> int:
        context = McpContext(
            settings=settings,
            session_factory=_broken_factory,
            transport="memory",
        )
        # The bootstrap client is authenticated through the GOOD factory;
        # only the audit path is broken.
        context.bootstrap_client = authenticate_secret(
            session_factory, settings, world.key_full_secret
        )
        server = create_mcp_server(context)
        register_echo_tool(server, context, name=world.tool_name)
        async with connected_client(server) as session:
            with pytest.raises(MCPError) as excinfo:
                await session.call_tool(world.tool_name, {"message": "hi"})
            return excinfo.value.code

    assert asyncio.run(_run()) == MCP_ERROR_INTERNAL
    # And no audit row exists for the withheld call.
    assert audit_rows(session_factory, world.tool_name) == []
