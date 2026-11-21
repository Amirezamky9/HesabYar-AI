"""Webhook alert channel (US-704).

POSTs one JSON object per alert to the channel's configured https
URL. The URL itself is the secret (like a Slack webhook URL) — the
channel stores no separate credential in v1; rotate by replacing
the channel. The payload carries the alert's Persian text, rule,
severity, fired time, runbook ref and context — never credential
material (alert contexts are counts/amounts/ids by construction).

Never raises: every failure becomes a ``failed`` result with a
short detail, so one dead webhook cannot break an evaluation run.
``transport`` exists for tests (``httpx.MockTransport``) — no live
call is ever made from a test.
"""

from __future__ import annotations

import logging

import httpx

from hesabyar.domain.monitoring.models import (
    Alert,
    AlertChannel,
    AlertChannelKind,
    ChannelSendResult,
)

logger = logging.getLogger(__name__)

#: Webhook POST timeout (the endpoint is operator-configured; a slow
#: receiver must not stall the evaluation job).
WEBHOOK_TIMEOUT_SECONDS = 10.0


class WebhookAlertSender:
    """POSTs alerts to a webhook channel's https URL."""

    def __init__(self, *, transport: httpx.BaseTransport | None = None) -> None:
        self._transport = transport

    def send(self, *, channel: AlertChannel, alert: Alert) -> ChannelSendResult:
        """POST the alert JSON; never raises."""
        if channel.kind is not AlertChannelKind.WEBHOOK:
            return ChannelSendResult(
                channel_id=channel.id,
                kind=channel.kind,
                status="skipped",
                detail="Not a webhook channel.",
            )
        payload = {
            "alert_id": alert.id,
            "rule": alert.rule.value,
            "severity": alert.severity.value,
            "title_fa": alert.title_fa,
            "message_fa": alert.message_fa,
            "dedup_key": alert.dedup_key,
            "runbook_ref": alert.runbook_ref,
            "context": alert.context,
            "fired_at": alert.fired_at.isoformat() if alert.fired_at else None,
        }
        try:
            with httpx.Client(
                timeout=WEBHOOK_TIMEOUT_SECONDS,
                transport=self._transport,
                trust_env=False,
            ) as client:
                response = client.post(
                    channel.target,
                    json=payload,
                    headers={
                        "User-Agent": "HesabYar-Monitoring/1.0",
                        "X-HesabYar-Alert-Rule": alert.rule.value,
                        "X-HesabYar-Alert-Severity": alert.severity.value,
                    },
                )
        except Exception as exc:  # noqa: BLE001 - a dead webhook is a failed send, not a crash
            logger.warning("Webhook alert send failed for channel %s: %s", channel.id, exc)
            return ChannelSendResult(
                channel_id=channel.id,
                kind=channel.kind,
                status="failed",
                detail=f"Webhook request failed: {exc}",
            )
        if 200 <= response.status_code < 300:
            return ChannelSendResult(channel_id=channel.id, kind=channel.kind, status="sent")
        return ChannelSendResult(
            channel_id=channel.id,
            kind=channel.kind,
            status="failed",
            detail=f"Webhook returned HTTP {response.status_code}.",
        )
