"""Unit tests for the in-process request metrics (US-704).

``RequestMetrics`` is the only source for the 5xx alert rule; it
counts in the API process only (the worker has no HTTP traffic).
"""

from __future__ import annotations

from datetime import UTC, datetime

from hesabyar.domain.monitoring.metrics import RequestMetrics


def test_empty_metrics_have_no_rate() -> None:
    metrics = RequestMetrics(started_at=datetime(2026, 11, 21, tzinfo=UTC))
    snapshot = metrics.snapshot()
    assert snapshot.total == 0
    assert snapshot.server_errors == 0
    assert snapshot.error_rate is None
    assert snapshot.started_at == datetime(2026, 11, 21, tzinfo=UTC)


def test_record_counts_by_status() -> None:
    metrics = RequestMetrics()
    metrics.record(200)
    metrics.record(201)
    metrics.record(404)
    metrics.record(500)
    metrics.record(503)
    snapshot = metrics.snapshot()
    assert snapshot.total == 5
    assert snapshot.server_errors == 2
    assert snapshot.error_rate == 0.4


def test_non_5xx_only_gives_zero_rate() -> None:
    metrics = RequestMetrics()
    metrics.record(200)
    metrics.record(301)
    assert metrics.snapshot().error_rate == 0.0


def test_reset_clears_counts() -> None:
    metrics = RequestMetrics()
    metrics.record(500)
    metrics.reset()
    snapshot = metrics.snapshot()
    assert snapshot.total == 0
    assert snapshot.server_errors == 0
    assert snapshot.error_rate is None
    assert snapshot.started_at is not None
