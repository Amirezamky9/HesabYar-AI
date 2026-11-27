"""Unit tests for the webhook sender + MCP prober (US-704).

All HTTP runs against ``httpx.MockTransport`` — no live call is ever
made from a test.
"""

from __future__ import annotations

import datetime
import json

import httpx
import pytest

from hesabyar.domain.monitoring.models import (
    Alert,
    AlertChannel,
    AlertChannelKind,
    AlertRule,
    AlertSeverity,
    AlertStatus,
    ProcessStatus,
)
from hesabyar.infrastructure.monitoring import (
    HttpxMcpHealthProber,
    WebhookAlertSender,
)
from hesabyar.infrastructure.monitoring.mcp_probe import mcp_health_url

NOW = datetime.datetime(2026, 11, 21, 13, 0, tzinfo=datetime.UTC)


def _alert() -> Alert:
    return Alert(
        id="alert-1",
        rule=AlertRule.INBOX_STALE,
        severity=AlertSeverity.CRITICAL,
        status=AlertStatus.OPEN,
        title_fa="صندوق ورودی گیرکرده است",
        message_fa="۳ رویداد بیش از ۲۴ ساعت در صندوق ورودی پردازش نشده‌اند.",
        dedup_key="inbox_stale",
        runbook_ref="docs/monitoring.md — بخش «صندوق ورودی گیرکرده»",
        context={"stale_count": 3},
        fired_at=NOW,
    )


def _webhook_channel() -> AlertChannel:
    return AlertChannel(
        id="ch-web",
        kind=AlertChannelKind.WEBHOOK,
        name="وب‌هوک",
        target="https://hooks.example.com/alerts",
    )


# ---------------------------------------------------------------------------
# Webhook sender
# ---------------------------------------------------------------------------


def test_webhook_send_posts_persian_payload() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    sender = WebhookAlertSender(transport=httpx.MockTransport(handler))
    result = sender.send(channel=_webhook_channel(), alert=_alert())
    assert result.status == "sent"
    assert len(seen) == 1
    assert seen[0].url == httpx.URL("https://hooks.example.com/alerts")
    payload = json.loads(seen[0].content)
    assert payload["alert_id"] == "alert-1"
    assert payload["rule"] == "inbox_stale"
    assert payload["severity"] == "critical"
    assert payload["title_fa"] == "صندوق ورودی گیرکرده است"
    assert payload["dedup_key"] == "inbox_stale"
    assert payload["context"] == {"stale_count": 3}
    assert payload["fired_at"] == NOW.isoformat()
    assert seen[0].headers["X-HesabYar-Alert-Rule"] == "inbox_stale"
    assert seen[0].headers["X-HesabYar-Alert-Severity"] == "critical"


def test_webhook_send_non_2xx_is_failed() -> None:
    sender = WebhookAlertSender(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    result = sender.send(channel=_webhook_channel(), alert=_alert())
    assert result.status == "failed"
    assert "500" in (result.detail or "")


def test_webhook_send_transport_error_is_failed_not_raised() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    sender = WebhookAlertSender(transport=httpx.MockTransport(handler))
    result = sender.send(channel=_webhook_channel(), alert=_alert())
    assert result.status == "failed"


def test_webhook_send_non_webhook_channel_is_skipped() -> None:
    sender = WebhookAlertSender(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    channel = AlertChannel(id="ch-sms", kind=AlertChannelKind.SMS, name="پیامک", target="09123456789")
    result = sender.send(channel=channel, alert=_alert())
    assert result.status == "skipped"


# ---------------------------------------------------------------------------
# MCP health URL + prober
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("public_url", "expected"),
    [
        ("http://127.0.0.1:8002/mcp", "http://127.0.0.1:8002/health"),
        ("http://127.0.0.1:8002/mcp/", "http://127.0.0.1:8002/health"),
        ("https://mcp.example.com/mcp", "https://mcp.example.com/health"),
        ("https://mcp.example.com", "https://mcp.example.com/health"),
        ("http://127.0.0.1:8002/mcp?x=1", "http://127.0.0.1:8002/health"),
        ("not-a-url", "not-a-url"),  # returned unchanged; the probe fails honestly
    ],
)
def test_mcp_health_url(public_url: str, expected: str) -> None:
    assert mcp_health_url(public_url) == expected


def test_mcp_probe_ok() -> None:
    prober = HttpxMcpHealthProber(
        "http://127.0.0.1:8002/mcp",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"status": "ok"})),
    )
    health = prober.probe()
    assert health.name == "mcp"
    assert health.status is ProcessStatus.OK
    assert health.detail is None


def test_mcp_probe_http_error_is_error() -> None:
    prober = HttpxMcpHealthProber(
        "http://127.0.0.1:8002/mcp",
        transport=httpx.MockTransport(lambda request: httpx.Response(503)),
    )
    health = prober.probe()
    assert health.status is ProcessStatus.ERROR
    assert "503" in (health.detail or "")


def test_mcp_probe_unreachable_is_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    prober = HttpxMcpHealthProber("http://127.0.0.1:8002/mcp", transport=httpx.MockTransport(handler))
    health = prober.probe()
    assert health.status is ProcessStatus.ERROR


def test_mcp_probe_non_json_is_unknown() -> None:
    prober = HttpxMcpHealthProber(
        "http://127.0.0.1:8002/mcp",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"not json")),
    )
    health = prober.probe()
    assert health.status is ProcessStatus.UNKNOWN


def test_mcp_probe_unexpected_body_is_unknown() -> None:
    prober = HttpxMcpHealthProber(
        "http://127.0.0.1:8002/mcp",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"status": "degraded"})),
    )
    health = prober.probe()
    assert health.status is ProcessStatus.UNKNOWN
