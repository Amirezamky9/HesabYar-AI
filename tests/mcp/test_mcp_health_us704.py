"""MCP /health liveness probe tests (US-704).

``GET /health`` is the MCP server's liveness endpoint (threat model
A8: unauthenticated, like the REST /health) and the target of the
monitoring prober (``HttpxMcpHealthProber``). The middleware is
exercised directly over ``httpx2.ASGITransport`` with a dummy inner
app, plus one wired-app test proving the real app serves it without
auth and outside the rate limiter.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import httpx2
import pytest
from sqlalchemy.orm import Session
from starlette.types import ASGIApp, Receive, Scope, Send

from hesabyar import __version__
from hesabyar.infrastructure.config import AppSettings
from hesabyar.interfaces.mcp import build_streamable_http_app
from hesabyar.interfaces.mcp.http import HealthCheckMiddleware
from tests.mcp.conftest import World
from tests.mcp.harness import make_server

BASE_URL = "http://127.0.0.1:8002"


async def _dummy_app(scope: Scope, receive: Receive, send: Send) -> None:
    """An inner app that always 404s (proves pass-through)."""
    await send(
        {
            "type": "http.response.start",
            "status": 404,
            "headers": [(b"content-type", b"text/plain")],
        }
    )
    await send({"type": "http.response.body", "body": b"not found"})


async def _get(app: ASGIApp, path: str, *, method: str = "GET") -> httpx2.Response:
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url=BASE_URL) as client:
        return await client.request(method, path)


def test_health_returns_ok_without_auth() -> None:
    app = HealthCheckMiddleware(_dummy_app)
    response = asyncio.run(_get(app, "/health"))
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "hesabyar-mcp",
        "version": __version__,
    }
    assert response.headers["content-type"] == "application/json"


def test_health_only_answers_get() -> None:
    app = HealthCheckMiddleware(_dummy_app)
    response = asyncio.run(_get(app, "/health", method="POST"))
    assert response.status_code == 404  # passed through to the inner app


def test_other_paths_pass_through() -> None:
    app = HealthCheckMiddleware(_dummy_app)
    response = asyncio.run(_get(app, "/mcp"))
    assert response.status_code == 404


def test_wired_app_serves_health_without_auth(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """The real Streamable HTTP app answers /health with no credentials."""
    server, _ = make_server(world, session_factory, settings, transport="streamable-http")
    app = build_streamable_http_app(server, settings)
    response = asyncio.run(_get(app, "/health"))
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.parametrize("path", ["/health/", "/healthz", "/HEALTH"])
def test_health_path_is_exact(path: str) -> None:
    app = HealthCheckMiddleware(_dummy_app)
    response = asyncio.run(_get(app, path))
    assert response.status_code == 404
