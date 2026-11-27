"""Rate limiter unit tests (US-601, spec §13.8). Pure — no DB, no network."""

from __future__ import annotations

from hesabyar.interfaces.mcp.rate_limit import WINDOW_SECONDS, RateLimiter


class _Clock:
    """Controllable monotonic clock."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_allows_up_to_the_limit() -> None:
    limiter = RateLimiter(3, clock=_Clock())
    assert limiter.allow("key:abc")
    assert limiter.allow("key:abc")
    assert limiter.allow("key:abc")
    assert not limiter.allow("key:abc")


def test_window_resets_after_sixty_seconds() -> None:
    clock = _Clock()
    limiter = RateLimiter(1, clock=clock)
    assert limiter.allow("key:abc")
    assert not limiter.allow("key:abc")
    clock.now += WINDOW_SECONDS
    assert limiter.allow("key:abc")


def test_keys_are_independent() -> None:
    limiter = RateLimiter(1, clock=_Clock())
    assert limiter.allow("key:aaa")
    assert not limiter.allow("key:aaa")
    assert limiter.allow("key:bbb")
    assert limiter.allow("ip:1.2.3.4")
