"""MCP protocol conformance + transport smoke tests (US-601).

The conformance tests connect a real SDK ``ClientSession`` to the real
server over the SDK's in-memory streams: initialize, list tools /
resources / prompts, call a tool. The stdio tests spawn the real
server process (``python -m hesabyar.interfaces.mcp``) and
speak MCP to it over pipes. Streamable HTTP has its own module
(``test_mcp_http.py``).
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server.mcpserver import MCPServer
from sqlalchemy.orm import Session

from hesabyar.infrastructure.config import AppSettings
from hesabyar.interfaces.mcp import SERVER_NAME, create_mcp_server
from hesabyar.interfaces.mcp.context import McpContext
from hesabyar.interfaces.mcp.prompts import PROMPT_DEFINITIONS
from hesabyar.interfaces.mcp.resources import RESOURCE_DEFINITIONS
from hesabyar.interfaces.mcp.tools_core import CORE_TOOL_NAMES
from hesabyar.interfaces.mcp.tools_phase5 import PHASE5_TOOLS
from tests.mcp.conftest import TEST_DB_URL, World
from tests.mcp.harness import audit_rows, connected_client, make_server, tool_text

REPO_ROOT = Path(__file__).resolve().parents[2]

# The stdio subprocess must finish well inside the CI per-test budget.
STDIO_TIMEOUT_SECONDS = 60.0


# ---------------------------------------------------------------------------
# Conformance over in-memory streams
# ---------------------------------------------------------------------------


def test_initialize_handshake(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """Initialize succeeds; the server identifies itself as HesabYar."""

    async def _run() -> None:
        server, _ = make_server(
            world, session_factory, settings, bootstrap_secret=world.key_full_secret
        )
        async with connected_client(server) as session:
            assert session.server_info is not None
            assert session.server_info.name == SERVER_NAME
            assert session.protocol_version
            assert session.instructions

    asyncio.run(_run())


def test_list_tools_with_core_tools(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """US-602 wires the 14 read-only tools into the server (no echo tool here)."""

    async def _run() -> None:
        server, _ = make_server(
            world,
            session_factory,
            settings,
            bootstrap_secret=world.key_full_secret,
            with_tool=False,
        )
        async with connected_client(server) as session:
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            # Subset, not equality: US-603/US-604/US-605 wire more tools
            # onto the same server, and this test must not break then.
            assert CORE_TOOL_NAMES <= names

    asyncio.run(_run())


def test_list_tools_with_fixture_tool(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """A registered tool is listed with its narrow input schema."""

    async def _run() -> None:
        server, _ = make_server(
            world, session_factory, settings, bootstrap_secret=world.key_full_secret
        )
        async with connected_client(server) as session:
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            assert world.tool_name in names
            assert CORE_TOOL_NAMES <= names
            echo = next(tool for tool in tools.tools if tool.name == world.tool_name)
            schema = echo.input_schema
            assert set(schema["properties"]) == {"message", "company"}
            # The injected Context parameter is never part of the schema.
            assert "ctx" not in schema["properties"]

    asyncio.run(_run())


def test_list_resources_and_prompts_empty(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """US-601 registers no resources or prompts (they land in US-604)."""

    async def _run() -> None:
        server, _ = make_server(
            world, session_factory, settings, bootstrap_secret=world.key_full_secret
        )
        async with connected_client(server) as session:
            resources = await session.list_resources()
            assert resources.resources == []
            prompts = await session.list_prompts()
            assert prompts.prompts == []

    asyncio.run(_run())


def test_call_fixture_tool(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """A tool call round-trips and reaches the lifespan context."""

    async def _run() -> None:
        server, _ = make_server(
            world, session_factory, settings, bootstrap_secret=world.key_full_secret
        )
        async with connected_client(server) as session:
            result = await session.call_tool(
                world.tool_name, {"message": "ping", "company": world.company_a_id}
            )
            assert not result.is_error
            assert tool_text(result) == f"echo:ping:{world.company_a_id}:memory"

    asyncio.run(_run())
    rows = audit_rows(session_factory, world.tool_name)
    assert len(rows) == 1
    assert rows[0].outcome == "allowed"


def test_create_mcp_server_names_the_server(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """The factory builds an MCPServer named 'hesabyar' (no transport needed)."""
    context = McpContext(settings=settings, session_factory=session_factory)
    server = create_mcp_server(context)
    assert isinstance(server, MCPServer)
    assert server.name == SERVER_NAME
    tools = asyncio.run(server.list_tools())
    assert CORE_TOOL_NAMES <= {tool.name for tool in tools}


# ---------------------------------------------------------------------------
# stdio transport smoke (real subprocess)
# ---------------------------------------------------------------------------


def _stdio_params(secret: str) -> StdioServerParameters:
    """Server parameters for the real stdio entry point."""
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "hesabyar.interfaces.mcp", "--transport", "stdio"],
        env={
            "HESABYAR_DATABASE_URL": TEST_DB_URL,
            "HESABYAR_MCP__STDIO_API_KEY": secret,
        },
        cwd=str(REPO_ROOT),
    )


def test_stdio_smoke(world: World) -> None:
    """The stdio server starts, authenticates, and speaks MCP over pipes."""

    async def _run() -> None:
        async with stdio_client(_stdio_params(world.key_full_secret)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                assert session.server_info is not None
                assert session.server_info.name == SERVER_NAME
                tools = await session.list_tools()
                # Subset, not equality: US-605 wires more tools onto the
                # same server.
                assert CORE_TOOL_NAMES <= {tool.name for tool in tools.tools}
                assert {name for name, _ in PHASE5_TOOLS} <= {
                    tool.name for tool in tools.tools
                }
                templates = await session.list_resource_templates()
                assert {d.uri_template for d in RESOURCE_DEFINITIONS} <= {
                    t.uri_template for t in templates.resource_templates
                }
                prompts = await session.list_prompts()
                assert {d.name for d in PROMPT_DEFINITIONS} <= {
                    p.name for p in prompts.prompts
                }

    asyncio.run(asyncio.wait_for(_run(), timeout=STDIO_TIMEOUT_SECONDS))


def test_stdio_rejects_bad_key(world: World) -> None:
    """A stdio process with an invalid key fails fast (nonzero exit)."""

    async def _run() -> int:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "hesabyar.interfaces.mcp.server",
            "--transport",
            "stdio",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(REPO_ROOT),
            env={
                **os.environ,
                "HESABYAR_DATABASE_URL": TEST_DB_URL,
                "HESABYAR_MCP__STDIO_API_KEY": "hya_live_this-key-does-not-exist",
            },
        )
        return await asyncio.wait_for(proc.wait(), timeout=STDIO_TIMEOUT_SECONDS)

    assert asyncio.run(_run()) == 1


def test_stdio_rejects_missing_key() -> None:
    """A stdio process with no key at all fails fast (usage error)."""

    async def _run() -> int:
        env = dict(os.environ)
        env["HESABYAR_DATABASE_URL"] = TEST_DB_URL
        env.pop("HESABYAR_MCP__STDIO_API_KEY", None)
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "hesabyar.interfaces.mcp.server",
            "--transport",
            "stdio",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(REPO_ROOT),
            env=env,
        )
        return await asyncio.wait_for(proc.wait(), timeout=STDIO_TIMEOUT_SECONDS)

    assert asyncio.run(_run()) == 2
