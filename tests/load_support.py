"""Shared helpers for the US-702 load + concurrency tests (Phase 7).

Two rules govern everything here:

- Latencies are MEASURED, never asserted: the tests assert on outcomes
  + counts (exactly-once counts, no duplicate numbers, 429 counts).
  The recorder only produces the numbers ``docs/performance-baselines.md``
  documents. A time bound appears only as a generous safety net
  against a hung run, never as a pass/fail performance claim.
- Concurrency runs through :func:`run_threads`, which starts every
  worker behind one barrier (so the race is real, not sequential),
  joins with a timeout, and re-raises the first worker failure in the
  calling thread — a worker exception must fail the test, never be
  swallowed by the thread boundary.
"""

from __future__ import annotations

import datetime
import json
import math
import os
import platform
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import cast

#: Opt-in env var: when ``HESABYAR_RECORD_BASELINES=1``, a recorder
#: writes its measurements as JSON (path from
#: ``HESABYAR_BASELINES_PATH``, default ``performance-baselines.json``
#: in the working directory). Without it the numbers only go to
#: stdout, so a normal CI run leaves no files behind.
RECORD_BASELINES_ENV = "HESABYAR_RECORD_BASELINES"
BASELINES_PATH_ENV = "HESABYAR_BASELINES_PATH"
DEFAULT_BASELINES_PATH = "performance-baselines.json"

_MISSING = object()


def percentile(samples: Sequence[float], q: float) -> float:
    """Nearest-rank percentile (``q`` in 0..1); 0.0 for an empty sample."""
    if not samples:
        return 0.0
    ordered = sorted(samples)
    rank = max(1, math.ceil(q * len(ordered)))
    return ordered[rank - 1]


@dataclass(frozen=True)
class LatencySummary:
    """Percentiles of one measured path, in milliseconds."""

    n: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float
    mean_ms: float

    def as_dict(self) -> dict[str, float | int]:
        return {
            "n": self.n,
            "p50_ms": round(self.p50_ms, 2),
            "p95_ms": round(self.p95_ms, 2),
            "p99_ms": round(self.p99_ms, 2),
            "max_ms": round(self.max_ms, 2),
            "mean_ms": round(self.mean_ms, 2),
        }


class LatencyRecorder:
    """Thread-safe collector of per-path latencies (measurement only)."""

    def __init__(self) -> None:
        self._samples: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def record(self, name: str, seconds: float) -> None:
        with self._lock:
            self._samples.setdefault(name, []).append(seconds)

    def summaries(self) -> dict[str, LatencySummary]:
        with self._lock:
            snapshot = {name: list(values) for name, values in self._samples.items()}
        result: dict[str, LatencySummary] = {}
        for name, values in snapshot.items():
            millis = [value * 1000.0 for value in values]
            result[name] = LatencySummary(
                n=len(millis),
                p50_ms=percentile(millis, 0.50),
                p95_ms=percentile(millis, 0.95),
                p99_ms=percentile(millis, 0.99),
                max_ms=max(millis) if millis else 0.0,
                mean_ms=(sum(millis) / len(millis)) if millis else 0.0,
            )
        return result

    def report(self) -> str:
        """A printable percentile table (goes to the CI log)."""
        lines = ["load baselines (measured, not asserted):"]
        for name, summary in sorted(self.summaries().items()):
            lines.append(
                f"  {name}: n={summary.n} p50={summary.p50_ms:.1f}ms "
                f"p95={summary.p95_ms:.1f}ms p99={summary.p99_ms:.1f}ms "
                f"max={summary.max_ms:.1f}ms mean={summary.mean_ms:.1f}ms"
            )
        return "\n".join(lines)

    def maybe_write_json(self) -> str | None:
        """Merge the measurements into the JSON baselines file, when opted in.

        Returns the path written, or None when recording is off. Each
        test owns its own recorder, so the file is read back and
        merged (new measurements overwrite same-named old ones) —
        a full run leaves one file with every path's numbers, not
        just the last test's.
        """
        if os.environ.get(RECORD_BASELINES_ENV) != "1":
            return None
        path = os.environ.get(BASELINES_PATH_ENV, DEFAULT_BASELINES_PATH)
        payload: dict[str, object] = {}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict):
                    payload = loaded
            except (OSError, json.JSONDecodeError):
                payload = {}
        measurements = payload.get("measurements")
        merged: dict[str, object] = dict(measurements) if isinstance(measurements, dict) else {}
        merged.update(
            {
                name: summary.as_dict()
                for name, summary in sorted(self.summaries().items())
            }
        )
        payload.update(
            {
                "generated_at_utc": datetime.datetime.now(datetime.UTC).isoformat(),
                "platform": platform.platform(),
                "python": sys.version.split()[0],
                "cpus": os.cpu_count(),
                "measurements": merged,
            }
        )
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        return path


def run_threads[T](
    count: int,
    target: Callable[[int], T],
    *,
    timeout_seconds: float = 300.0,
    barrier_timeout_seconds: float = 60.0,
) -> list[T]:
    """Run ``target(worker_index)`` on ``count`` threads, all starting together.

    Every worker waits on one barrier before calling ``target``, so
    the threads genuinely race instead of running one after another.
    Returns the workers' results in worker order. Raises the first
    worker exception (in the calling thread) and fails when any
    worker does not finish within ``timeout_seconds``.
    """
    barrier = threading.Barrier(count)
    results: list[T | object] = [_MISSING] * count
    errors: list[BaseException | None] = [None] * count

    def _run(index: int) -> None:
        try:
            barrier.wait(timeout=barrier_timeout_seconds)
            results[index] = target(index)
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller
            errors[index] = exc

    threads = [
        threading.Thread(target=_run, args=(index,), name=f"load-worker-{index}")
        for index in range(count)
    ]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout_seconds
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
    alive = [thread.name for thread in threads if thread.is_alive()]
    assert not alive, f"workers did not finish within {timeout_seconds}s: {alive}"
    for error in errors:
        if error is not None:
            raise error
    assert all(result is not _MISSING for result in results), "a worker produced no result"
    return cast("list[T]", results)
