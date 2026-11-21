"""MCP security suite (US-701).

Proves the properties the threat model (docs/security/threat-model.md)
claims for the MCP boundary, over the in-memory conformance harness
(a real SDK ``ClientSession`` against a real ``MCPServer``):

- scope escalation: a ``reports:read`` key cannot call a
  ``banking:read`` tool (and vice versa) — denied at the middleware
  and audited as denied;
- token replay after rotation: the verifier rejects the old secret
  and accepts the replacement with the same grant;
- no secrets in logs over a full MCP cycle (allowed call, scope
  denial, company denial, unauthenticated call, verifier failures).

Cross-company denial for tools and resources already lives in
``test_mcp_auth.py`` (``test_company_restriction_enforced``,
``test_cross_tenant_company_denied``) and
``test_mcp_resources.py`` (``test_resource_cross_company_denied``,
``test_report_snapshot_cross_company_denied``) and is not
duplicated here.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import pytest
from mcp.shared.exceptions import MCPError
from sqlalchemy.orm import Session

from hesabyar.infrastructure.auth import ApiKeyCrypto, ApiKeyService
from hesabyar.infrastructure.config import AppSettings
from hesabyar.infrastructure.persistence.postgres.models import ApiClientModel, ApiKeyModel
from hesabyar.interfaces.mcp.auth import HesabYarTokenVerifier
from hesabyar.interfaces.mcp.errors import MCP_ERROR_FORBIDDEN, MCP_ERROR_UNAUTHENTICATED
from tests.mcp.conftest import INSTALLATION_ID, World
from tests.mcp.harness import audit_rows, connected_client, make_server, tool_text


def _verifier(
    session_factory: Callable[[], Session], settings: AppSettings
) -> HesabYarTokenVerifier:
    return HesabYarTokenVerifier(
        session_factory,
        ApiKeyCrypto(settings.auth.api_key_pepper),
        resource_url=settings.mcp.public_url,
    )


def _issue_key(
    session_factory: Callable[[], Session],
    settings: AppSettings,
    world: World,
    scopes: list[str],
) -> str:
    """Issue a fresh key for the world's client; return its secret."""
    session = session_factory()
    try:
        client = session.get(ApiClientModel, world.client_id)
        assert client is not None
        issued = ApiKeyService(ApiKeyCrypto(settings.auth.api_key_pepper)).issue_key(
            session,
            client=client,
            name="us701 test key",
            scopes=scopes,
            installation_id=INSTALLATION_ID,
        )
        session.commit()
        return issued.secret
    finally:
        session.close()


async def _call_tool_expect_denied(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    *,
    bootstrap_secret: str | None,
    tool_scopes: tuple[str, ...],
    arguments: dict[str, object] | None = None,
) -> int:
    """Call the fixture tool; return the MCPError code (the call must fail)."""
    server, _ = make_server(
        world,
        session_factory,
        settings,
        bootstrap_secret=bootstrap_secret,
        tool_scopes=tool_scopes,
    )
    async with connected_client(server) as session:
        with pytest.raises(MCPError) as excinfo:
            await session.call_tool(world.tool_name, arguments or {"message": "hi"})
        return excinfo.value.code


async def _call_tool_expect_allowed(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    *,
    bootstrap_secret: str,
    tool_scopes: tuple[str, ...],
    arguments: dict[str, object] | None = None,
) -> str:
    """Call the fixture tool; return the result text (the call must succeed)."""
    server, _ = make_server(
        world,
        session_factory,
        settings,
        bootstrap_secret=bootstrap_secret,
        tool_scopes=tool_scopes,
    )
    async with connected_client(server) as session:
        result = await session.call_tool(world.tool_name, arguments or {"message": "hi"})
        assert not result.is_error
        return tool_text(result)


# ---------------------------------------------------------------------------
# Scope escalation (the AC's named pair: reports:read vs banking:read)
# ---------------------------------------------------------------------------


def test_reports_key_cannot_call_banking_tool(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """A reports:read key calling a banking:read tool is forbidden + audited."""
    code = asyncio.run(
        _call_tool_expect_denied(
            world,
            session_factory,
            settings,
            bootstrap_secret=world.key_full_secret,  # mcp:connect + company:read + reports:read
            tool_scopes=("banking:read",),
        )
    )
    assert code == MCP_ERROR_FORBIDDEN
    denied = [
        row
        for row in audit_rows(session_factory, world.tool_name)
        if row.outcome == "denied" and row.error_code == "missing_scope"
    ]
    assert denied, "the scope-denied call must be audited as denied"


def test_banking_key_cannot_call_reports_tool(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """The reverse direction: a banking:read key cannot call a reports:read tool."""
    banking_secret = _issue_key(
        session_factory, settings, world, ["mcp:connect", "banking:read"]
    )
    code = asyncio.run(
        _call_tool_expect_denied(
            world,
            session_factory,
            settings,
            bootstrap_secret=banking_secret,
            tool_scopes=("reports:read",),
        )
    )
    assert code == MCP_ERROR_FORBIDDEN


def test_banking_key_passes_its_own_tool(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """Control: the denials above are scope denials, not auth failures."""
    banking_secret = _issue_key(
        session_factory, settings, world, ["mcp:connect", "banking:read"]
    )
    text = asyncio.run(
        _call_tool_expect_allowed(
            world,
            session_factory,
            settings,
            bootstrap_secret=banking_secret,
            tool_scopes=("banking:read",),
        )
    )
    assert text.startswith("echo:hi:")


# ---------------------------------------------------------------------------
# Token replay after rotation (per-request verifier path)
# ---------------------------------------------------------------------------


def test_rotated_key_fails_verification_but_replacement_verifies(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """The old secret verifies to None the moment rotation commits."""
    session = session_factory()
    try:
        service = ApiKeyService(ApiKeyCrypto(settings.auth.api_key_pepper))
        record = session.get(ApiKeyModel, world.key_full_id)
        assert record is not None
        rotated = service.rotate_key(session, key=record)
        session.commit()
        new_secret = rotated.secret
    finally:
        session.close()

    assert new_secret != world.key_full_secret
    verifier = _verifier(session_factory, settings)

    async def _run() -> None:
        assert await verifier.verify_token(world.key_full_secret) is None
        token = await verifier.verify_token(new_secret)
        assert token is not None
        # The replacement inherits the grant.
        assert token.scopes == ["company:read", "mcp:connect", "reports:read"]
        assert token.client_id == world.client_id
        # And the old secret stays dead (no grace window, no replay).
        assert await verifier.verify_token(world.key_full_secret) is None

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# No secrets in logs over a full MCP cycle
# ---------------------------------------------------------------------------


def test_no_secrets_in_logs_over_a_full_mcp_cycle(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    verifier = _verifier(session_factory, settings)

    async def _cycle() -> None:
        # Allowed call.
        await _call_tool_expect_allowed(
            world,
            session_factory,
            settings,
            bootstrap_secret=world.key_full_secret,
            tool_scopes=("reports:read",),
            arguments={"message": "hi", "company": world.company_a_id},
        )
        # Scope denial (no reports:read).
        assert (
            await _call_tool_expect_denied(
                world,
                session_factory,
                settings,
                bootstrap_secret=world.key_company_only_secret,
                tool_scopes=("reports:read",),
            )
            == MCP_ERROR_FORBIDDEN
        )
        # Company denial (restricted to A, calling with B).
        assert (
            await _call_tool_expect_denied(
                world,
                session_factory,
                settings,
                bootstrap_secret=world.key_restricted_a_secret,
                tool_scopes=("reports:read",),
                arguments={"message": "hi", "company": world.company_b_id},
            )
            == MCP_ERROR_FORBIDDEN
        )
        # Unauthenticated call (no identity at all).
        assert (
            await _call_tool_expect_denied(
                world,
                session_factory,
                settings,
                bootstrap_secret=None,
                tool_scopes=("reports:read",),
            )
            == MCP_ERROR_UNAUTHENTICATED
        )
        # Verifier failures: revoked, expired, unknown.
        assert await verifier.verify_token(world.key_revoked_secret) is None
        assert await verifier.verify_token(world.key_expired_secret) is None
        assert await verifier.verify_token("hya_live_no-such-key-000000000000") is None

    with caplog.at_level(logging.INFO, logger="hesabyar"):
        asyncio.run(_cycle())

    # The failure paths did log (the test is not vacuous).
    assert "reason=revoked" in caplog.text
    assert "reason=expired" in caplog.text
    for secret in (
        world.key_full_secret,
        world.key_no_mcp_secret,
        world.key_company_only_secret,
        world.key_restricted_a_secret,
        world.key_revoked_secret,
        world.key_expired_secret,
    ):
        assert secret not in caplog.text
        assert secret[len("hya_live_"):] not in caplog.text
