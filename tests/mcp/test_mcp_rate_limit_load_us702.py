"""Rate limiting under load (US-702, spec §13.8 + §17 phase 7).

The REST API has no rate limiter; the MCP Streamable HTTP transport
is the only rate-limited surface, so these tests exercise it:

- :class:`RateLimiter` under concurrent threads: a burst larger than
  the limit admits exactly ``max_per_minute`` per key — never more
  (the US-702 lock makes the check-and-increment atomic), never
  fewer, and keys stay independent under load.
- :class:`RateLimitMiddleware` under concurrent async requests (the
  production shape: one event loop, many in-flight requests): the
  requests inside the budget pass through, every request past it
  gets 429 + ``Retry-After``, and nothing becomes a 500.

Pure — no DB, no network, no MCP server: the middleware test wraps a
trivial ASGI app that always answers 200, so any 429 is the
limiter's doing and any 500 would be the middleware's.
"""

from __future__ import annotations

import asyncio
from collections import Counter

import httpx2
import pytest
from starlette.types import ASGIApp, Receive, Scope, Send

from hesabyar.interfaces.mcp.http import RateLimitMiddleware
from hesabyar.interfaces.mcp.rate_limit import WINDOW_SECONDS, RateLimiter
from tests.load_support import LatencyRecorder, run_threads

BASE_URL = "http://127.0.0.1:8002"


async def _ok_app(scope: Scope, receive: Receive, send: Send) -> None:
    """Trivial downstream app: every HTTP request gets 200."""
    assert scope["type"] == "http"
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": b"{}"})


def test_limiter_admits_exactly_the_limit_under_concurrent_burst() -> None:
    """10 threads x 50 calls, limit 100: exactly 100 allowed, 400 denied."""
    limiter = RateLimiter(100)
    attempts_per_thread = 50

    def _burst(_index: int) -> int:
        return sum(1 for _ in range(attempts_per_thread) if limiter.allow("key:burst"))

    admitted = run_threads(10, _burst)
    assert sum(admitted) == 100
    # The window is spent: one more call is denied.
    assert not limiter.allow("key:burst")


def test_limiter_keys_stay_independent_under_concurrent_load() -> None:
    """100 keys x 10 concurrent calls, limit 5: each key admits exactly 5."""
    limiter = RateLimiter(5)
    keys = [f"key:tenant-{index:03d}" for index in range(100)]

    def _hammer(index: int) -> int:
        key = keys[index]
        return sum(1 for _ in range(10) if limiter.allow(key))

    admitted = run_threads(100, _hammer)
    assert admitted == [5] * 100


def test_limiter_window_rollover_under_load() -> None:
    """A burst that straddles the window boundary gets a fresh budget."""
    now = [0.0]

    def _clock() -> float:
        return now[0]

    limiter = RateLimiter(2, clock=_clock)
    assert limiter.allow("key:rollover")
    assert limiter.allow("key:rollover")
    assert not limiter.allow("key:rollover")
    now[0] += WINDOW_SECONDS
    assert limiter.allow("key:rollover")
    assert limiter.allow("key:rollover")
    assert not limiter.allow("key:rollover")


@pytest.mark.parametrize("limit", [1, 20])
def test_middleware_concurrent_requests_get_429_past_the_limit(limit: int) -> None:
    """Concurrent requests past the budget get 429 + Retry-After, never 500.

    One shared API-key prefix (one bucket), ``limit`` requests of
    budget, 5x that many concurrent requests: exactly ``limit`` pass
    through to the app (200) and the rest are 429s carrying
    ``Retry-After: 60``. No request is dropped silently and none
    becomes a 5xx.
    """
    total = limit * 5
    app: ASGIApp = RateLimitMiddleware(_ok_app, RateLimiter(limit))
    recorder = LatencyRecorder()

    async def _run() -> list[httpx2.Response]:
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url=BASE_URL) as client:

            async def _one() -> httpx2.Response:
                loop = asyncio.get_running_loop()
                started = loop.time()
                response = await client.post(
                    "/mcp",
                    content=b"{}",
                    headers={
                        "Content-Type": "application/json",
                        # A well-formed key shape (hya_live_ + 7 chars =
                        # the 16-char lookup prefix): one bucket for all
                        # of these requests.
                        "Authorization": "Bearer hya_live_aaaa1111secret",
                    },
                )
                recorder.record("mcp.rate_limit_middleware", loop.time() - started)
                return response

            return list(await asyncio.gather(*(_one() for _ in range(total))))

    responses = asyncio.run(_run())
    statuses = Counter(response.status_code for response in responses)
    assert statuses[200] == limit, statuses
    assert statuses[429] == total - limit, statuses
    assert not any(status >= 500 for status in statuses), statuses
    for response in responses:
        if response.status_code == 429:
            assert response.headers["retry-after"] == str(int(WINDOW_SECONDS))
            assert response.json()["error"] == "rate_limited"
    print(recorder.report())
    recorder.maybe_write_json()


def test_middleware_distinct_keys_get_independent_budgets() -> None:
    """Two key prefixes hammering concurrently do not share a budget."""
    app: ASGIApp = RateLimitMiddleware(_ok_app, RateLimiter(3))

    async def _run() -> Counter[int]:
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url=BASE_URL) as client:

            async def _one(prefix: str) -> int:
                response = await client.post(
                    "/mcp",
                    content=b"{}",
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer hya_live_{prefix}secret",
                    },
                )
                return response.status_code

            # The 16-char prefixes differ (aaaa111 vs bbbb222).
            results = await asyncio.gather(
                *(_one("aaaa111") for _ in range(5)),
                *(_one("bbbb222") for _ in range(5)),
            )
            return Counter(results)

    statuses = asyncio.run(_run())
    # Each key admits 3 and refuses 2.
    assert statuses[200] == 6, statuses
    assert statuses[429] == 4, statuses


def test_middleware_rate_limit_key_uses_client_ip_without_a_key() -> None:
    """Unauthenticated requests are bucketed by client IP (ASGI scope)."""
    limiter = RateLimiter(1)
    app: ASGIApp = RateLimitMiddleware(_ok_app, limiter)

    async def _run() -> list[int]:
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url=BASE_URL) as client:
            statuses = []
            for _ in range(2):
                response = await client.post(
                    "/mcp",
                    content=b"{}",
                    headers={"Content-Type": "application/json"},
                )
                statuses.append(response.status_code)
            return statuses

    assert asyncio.run(_run()) == [200, 429]
