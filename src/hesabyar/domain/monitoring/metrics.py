"""In-process request metrics (US-704) — the 5xx-rate source.

A thread-safe counter the REST middleware increments per response.
It is deliberately in-memory and per-process:

- the API process is the only process that serves HTTP, so its
  counters ARE the 5xx rate for a single-worker deployment;
- a restart resets the counters (``started_at`` says so honestly);
- the worker process has no HTTP traffic, so its evaluation reads
  an empty counter and skips the 5xx rule (see
  ``MonitoringSnapshot.error_rate_5xx``).

A multi-worker deployment needs a shared metrics store (a future
story); until then the panel shows the rate for the process that
answered, and the docs say so.
"""

from __future__ import annotations

import datetime
import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class RequestMetricsSnapshot:
    """One read of the counters (``error_rate`` is None below 1 request)."""

    total: int
    server_errors: int
    started_at: datetime.datetime
    error_rate: float | None


class RequestMetrics:
    """Thread-safe total + 5xx counters for one process."""

    def __init__(self, *, started_at: datetime.datetime | None = None) -> None:
        self._lock = threading.Lock()
        self._total = 0
        self._server_errors = 0
        self._started_at = started_at or datetime.datetime.now(datetime.UTC)

    def record(self, status_code: int) -> None:
        """Count one response (5xx = status >= 500)."""
        with self._lock:
            self._total += 1
            if status_code >= 500:
                self._server_errors += 1

    def snapshot(self) -> RequestMetricsSnapshot:
        """Read the counters (a consistent pair under the lock)."""
        with self._lock:
            total = self._total
            errors = self._server_errors
        rate = (errors / total) if total > 0 else None
        return RequestMetricsSnapshot(
            total=total,
            server_errors=errors,
            started_at=self._started_at,
            error_rate=rate,
        )

    def reset(self) -> None:
        """Zero the counters (tests + operator reset; restarts the window)."""
        with self._lock:
            self._total = 0
            self._server_errors = 0
            self._started_at = datetime.datetime.now(datetime.UTC)


#: The process-wide counters: the REST middleware records into this
#: instance and the monitoring service reads it. Tests build their
#: own instance and pass it in — never touch this one.
request_metrics = RequestMetrics()
