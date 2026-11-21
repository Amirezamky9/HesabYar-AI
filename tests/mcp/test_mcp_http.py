"""Streamable HTTP transport tests (US-601, spec §13.8).

The app is exercised in-process through ``httpx2.ASGITransport`` —
no socket, no uvicorn — with both raw JSON-RPC posts (auth gates,
Origin/Host validation, body-size and rate limits) and the real SDK
``streamable_http_client`` + ``ClientSession`` end to end.

The base URL (``http://127.0.0.1:8002``) matches the default test
settings' public URL and Host allow-list, so the DNS-rebinding
protection passes for the happy path.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from typing import Any

import httpx2
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from sqlalchemy.orm import Session
from starlette.types import ASGIApp, Message

from hesabyar.infrastructure.config import AppSettings, McpSettings
from hesabyar.interfaces.mcp import build_streamable_http_app
from hesabyar.interfaces.mcp.errors import MCP_ERROR_FORBIDDEN
from hesabyar.interfaces.mcp.tools_core import CORE_TOOL_NAMES
from tests.mcp.conftest import World
from tests.mcp.harness import audit_rows, make_server, tool_text

BASE_URL = "http://127.0.0.1:8002"
MCP_URL = f"{BASE_URL}/mcp"

_INITIALIZE_BODY: dict[str, Any] = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2026-07-28",
        "capabilities": {},
        "clientInfo": {"name": "hesabyar-http-test", "version": "0"},
    },
}

HTTP_TIMEOUT_SECONDS = 60.0


def _auth_headers(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


async def _raw_post(
    app: ASGIApp,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> httpx2.Response:
    """One raw JSON-RPC POST against the app; returns the response."""
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url=BASE_URL) as client:
        return await client.post(
            "/mcp",
            content=json.dumps(body if body is not None else _INITIALIZE_BODY),
            headers={"Content-Type": "application/json", **(headers or {})},
        )


@asynccontextmanager
async def _running_lifespan(app: ASGIApp) -> AsyncIterator[None]:
    """Drive the app's ASGI lifespan around one test's requests.

    httpx2.ASGITransport never sends lifespan messages, so the SDK
    session manager's task group — created by ``run()``, entered via
    the Starlette app's lifespan (``streamable_http_app`` wires
    ``lifespan=lambda app: session_manager.run()``) — is never
    initialized, and every request reaching session handling raises
    "Task group is not initialized". uvicorn runs the lifespan in
    production, so this is a test-harness gap, not a product bug. A
    manager's ``run()`` can be called only once per instance, so each
    test builds a fresh app and runs its lifespan exactly once.
    """
    receive_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    send_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def receive() -> dict[str, Any]:
        return await receive_queue.get()

    async def send(message: Message) -> None:
        await send_queue.put(dict(message))

    async def _serve_lifespan() -> None:
        await app({"type": "lifespan"}, receive, send)

    task = asyncio.create_task(_serve_lifespan())
    started = False
    try:
        await receive_queue.put({"type": "lifespan.startup"})
        startup = await send_queue.get()
        assert startup["type"] == "lifespan.startup.complete", startup
        started = True
        yield
    finally:
        if started:
            await receive_queue.put({"type": "lifespan.shutdown"})
            shutdown = await send_queue.get()
            assert shutdown["type"] == "lifespan.shutdown.complete", shutdown
            await task
        elif not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def _with_lifespan[T](app: ASGIApp, coro: Coroutine[Any, Any, T]) -> T:
    """Run ``coro`` with the app's lifespan active."""
    async with _running_lifespan(app):
        return await coro


def test_http_app_builds(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """The Streamable HTTP app builds and is a callable ASGI app."""
    server, _ = make_server(world, session_factory, settings, transport="streamable-http")
    app = build_streamable_http_app(server, settings)
    assert callable(app)


def test_http_missing_auth_returns_401(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    server, _ = make_server(world, session_factory, settings, transport="streamable-http")
    app = build_streamable_http_app(server, settings)
    response = asyncio.run(_with_lifespan(app, _raw_post(app)))
    assert response.status_code == 401
    assert response.json()["error"] == "invalid_token"


def test_http_invalid_key_returns_401(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    server, _ = make_server(world, session_factory, settings, transport="streamable-http")
    app = build_streamable_http_app(server, settings)
    response = asyncio.run(
        _with_lifespan(
            app, _raw_post(app, headers=_auth_headers("hya_live_no-such-key-000000000000"))
        )
    )
    assert response.status_code == 401


def test_http_revoked_key_returns_401(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    server, _ = make_server(world, session_factory, settings, transport="streamable-http")
    app = build_streamable_http_app(server, settings)
    response = asyncio.run(
        _with_lifespan(app, _raw_post(app, headers=_auth_headers(world.key_revoked_secret)))
    )
    assert response.status_code == 401


def test_http_key_without_mcp_connect_returns_403(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """A valid key without mcp:connect is refused at the transport (403)."""
    server, _ = make_server(world, session_factory, settings, transport="streamable-http")
    app = build_streamable_http_app(server, settings)
    response = asyncio.run(
        _with_lifespan(app, _raw_post(app, headers=_auth_headers(world.key_no_mcp_secret)))
    )
    assert response.status_code == 403
    assert response.json()["error"] == "insufficient_scope"


def test_http_bad_origin_returns_403(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """A present Origin outside the allow-list is refused (DNS rebinding)."""
    server, _ = make_server(world, session_factory, settings, transport="streamable-http")
    app = build_streamable_http_app(server, settings)
    response = asyncio.run(
        _with_lifespan(
            app,
            _raw_post(
                app,
                headers={**_auth_headers(world.key_full_secret), "Origin": "http://evil.example"},
            ),
        )
    )
    assert response.status_code == 403


def test_http_bad_host_returns_421(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """A Host outside the allow-list is refused (DNS rebinding)."""
    server, _ = make_server(world, session_factory, settings, transport="streamable-http")
    app = build_streamable_http_app(server, settings)
    response = asyncio.run(
        _with_lifespan(
            app,
            _raw_post(
                app,
                headers={**_auth_headers(world.key_full_secret), "Host": "evil.example"},
            ),
        )
    )
    assert response.status_code == 421


def test_http_oversized_body_returns_413(
    world: World, session_factory: Callable[[], Session]
) -> None:
    """A body over the configured limit is refused before dispatch."""
    small_body_settings = AppSettings(mcp=McpSettings(max_request_body_bytes=1024))
    server, _ = make_server(
        world, session_factory, small_body_settings, transport="streamable-http"
    )
    app = build_streamable_http_app(server, small_body_settings)
    body = dict(_INITIALIZE_BODY)
    body["params"] = {**_INITIALIZE_BODY["params"], "padding": "x" * 2048}
    response = asyncio.run(
        _with_lifespan(
            app, _raw_post(app, headers=_auth_headers(world.key_full_secret), body=body)
        )
    )
    assert response.status_code == 413


def test_http_rate_limit_returns_429(
    world: World, session_factory: Callable[[], Session]
) -> None:
    """The third request inside a minute (limit 2) gets 429 + Retry-After."""
    limited_settings = AppSettings(mcp=McpSettings(rate_limit_per_minute=2))
    server, _ = make_server(
        world, session_factory, limited_settings, transport="streamable-http"
    )
    app = build_streamable_http_app(server, limited_settings)

    async def _run() -> list[int]:
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url=BASE_URL) as client:
            statuses = []
            for _ in range(3):
                response = await client.post(
                    "/mcp",
                    content=json.dumps(_INITIALIZE_BODY),
                    headers={"Content-Type": "application/json"},
                )
                statuses.append(response.status_code)
            return statuses

    statuses = asyncio.run(_with_lifespan(app, _run()))
    assert statuses[0] == 401  # unauthenticated, but within the rate budget
    assert statuses[1] == 401
    assert statuses[2] == 429


def test_http_client_end_to_end(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """The real SDK Streamable HTTP client talks to the app end to end."""

    server, _ = make_server(world, session_factory, settings, transport="streamable-http")
    app = build_streamable_http_app(server, settings)

    async def _run() -> None:
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(
            transport=transport,
            base_url=BASE_URL,
            headers=_auth_headers(world.key_full_secret),
        ) as http_client:
            async with streamable_http_client(
                MCP_URL, http_client=http_client
            ) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    assert session.server_info is not None
                    assert session.server_info.name == "hesabyar"
                    tools = await session.list_tools()
                    names = {tool.name for tool in tools.tools}
                    assert world.tool_name in names
                    # Subset, not equality: US-603/US-604/US-605 wire
                    # more tools onto the same server.
                    assert CORE_TOOL_NAMES <= names
                    result = await session.call_tool(
                        world.tool_name,
                        {"message": "ping", "company": world.company_a_id},
                    )
                    assert not result.is_error
                    assert (
                        tool_text(result)
                        == f"echo:ping:{world.company_a_id}:streamable-http"
                    )

    asyncio.run(asyncio.wait_for(_with_lifespan(app, _run()), timeout=HTTP_TIMEOUT_SECONDS))
    rows = audit_rows(session_factory, world.tool_name)
    assert len(rows) == 1
    assert rows[0].outcome == "allowed"
    assert rows[0].transport == "streamable-http"
    assert rows[0].company_id == world.company_a_id


def test_http_company_restriction_enforced(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """The company SAFETY property holds over Streamable HTTP too."""

    server, _ = make_server(world, session_factory, settings, transport="streamable-http")
    app = build_streamable_http_app(server, settings)

    async def _run() -> None:
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(
            transport=transport,
            base_url=BASE_URL,
            headers=_auth_headers(world.key_restricted_a_secret),
        ) as http_client:
            async with streamable_http_client(
                MCP_URL, http_client=http_client
            ) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    with pytest.raises(MCPError) as excinfo:
                        await session.call_tool(
                            world.tool_name,
                            {"message": "hi", "company": world.company_b_id},
                        )
                    assert excinfo.value.code == MCP_ERROR_FORBIDDEN
                    assert "restricted to a different company" in str(excinfo.value)

    asyncio.run(asyncio.wait_for(_with_lifespan(app, _run()), timeout=HTTP_TIMEOUT_SECONDS))
    rows = audit_rows(session_factory, world.tool_name)
    assert len(rows) == 1
    assert rows[0].outcome == "denied"
    assert rows[0].error_code == "company_forbidden"
