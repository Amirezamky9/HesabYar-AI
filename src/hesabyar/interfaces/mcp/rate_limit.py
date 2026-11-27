"""Fixed-window rate limiting for Streamable HTTP (US-601, spec §13.8).

One in-memory counter per key (API-key prefix, or client IP for
unauthenticated requests) per 60-second window. Per-process state is
deliberate: the MCP server is a single-process deployment (one
installation per store, spec §15.1), so no shared store is needed.
A multi-worker deployment would need a shared limiter — flagged in
``docs/mcp-server.md``.

``allow`` is thread-safe (US-702): the check-and-increment is one
critical section, so a concurrent burst can never admit more than
``max_per_minute`` per key per window. The deployed server is a
single async event loop (no interleaving inside a synchronous
``allow``), so the lock is cheap insurance for embedded or
multi-threaded use, not a fix for a live race.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

#: Window length in seconds.
WINDOW_SECONDS = 60.0


class RateLimiter:
    """Fixed-window limiter: at most ``max_per_minute`` calls per key per minute."""

    def __init__(
        self,
        max_per_minute: int,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._max = max_per_minute
        self._clock = clock or time.monotonic
        # key -> (window start, count in this window)
        self._counts: dict[str, tuple[float, int]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        """True when ``key`` still has budget in the current window."""
        with self._lock:
            now = self._clock()
            window_start, count = self._counts.get(key, (now, 0))
            if now - window_start >= WINDOW_SECONDS:
                self._counts[key] = (now, 1)
                self._prune(now)
                return True
            if count >= self._max:
                return False
            self._counts[key] = (window_start, count + 1)
            return True

    def _prune(self, now: float) -> None:
        """Drop windows that ended more than one window ago."""
        stale = [
            key
            for key, (window_start, _) in self._counts.items()
            if now - window_start >= 2 * WINDOW_SECONDS
        ]
        for key in stale:
            del self._counts[key]
