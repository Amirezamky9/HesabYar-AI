"""Monitoring application service (US-704, Phase 7).

Builds the monitoring snapshot (processes, queues, tokens, backups,
5xx rate), evaluates the alert rules against it, persists newly
fired alerts (deduped by dedup key), and dispatches them down the
configured channels (SMS via the US-544 registry, email via
``EmailSenderPort``, webhook via POST).

The service never commits: the caller (router / worker) owns the
transaction. Dispatch never raises: one channel's failure is a
``failed``/``skipped`` result, never a broken evaluation.
"""

from __future__ import annotations

import datetime
import logging
import uuid

from hesabyar.application.commands.sms import (
    build_provider_client,
    provider_sendable_reason,
)
from hesabyar.domain.integrations.ports import CredentialVaultPort
from hesabyar.domain.monitoring import texts
from hesabyar.domain.monitoring.errors import (
    MonitoringAlertAlreadyOpenError,
    MonitoringNotFoundError,
    MonitoringValidationError,
)
from hesabyar.domain.monitoring.metrics import RequestMetrics
from hesabyar.domain.monitoring.models import (
    Alert,
    AlertChannel,
    AlertChannelKind,
    AlertRule,
    AlertSeverity,
    AlertStatus,
    ChannelSendResult,
    EvaluationReport,
    MonitoringSnapshot,
    MonitoringThresholds,
    ProcessHealth,
    ProcessStatus,
    QueueDepths,
    TokenExpiry,
    TokenRecord,
)
from hesabyar.domain.monitoring.ports import (
    AlertChannelSender,
    BackupStatusSource,
    HealthChecksPort,
    McpHealthProber,
    MonitoringRepositoryPort,
    NoBackupStatusSource,
)
from hesabyar.domain.monitoring.rules import evaluate_rules, expiry_severity
from hesabyar.domain.monitoring.validation import (
    validate_channel_payload,
    validate_token_payload,
)
from hesabyar.domain.reminders.ports import EmailSenderPort
from hesabyar.domain.sms.errors import SmsError
from hesabyar.domain.sms.models import SmsDeliveryStatus
from hesabyar.domain.sms.ports import SmsClientFactory, SmsRepositoryPort

logger = logging.getLogger(__name__)

#: Severity ordering for the per-channel minimum-severity gate.
_SEVERITY_ORDER = {
    AlertSeverity.INFO: 0,
    AlertSeverity.WARNING: 1,
    AlertSeverity.CRITICAL: 2,
}

#: Webhook-failure window for the failed-webhooks rule.
_WEBHOOK_WINDOW = datetime.timedelta(hours=24)


class MonitoringService:
    """Snapshot + evaluation + alert/channel/token management."""

    def __init__(
        self,
        repository: MonitoringRepositoryPort,
        *,
        thresholds: MonitoringThresholds,
        sms_repository: SmsRepositoryPort | None = None,
        client_factory: SmsClientFactory | None = None,
        vault: CredentialVaultPort | None = None,
        email: EmailSenderPort | None = None,
        webhook_sender: AlertChannelSender | None = None,
        backup_source: BackupStatusSource | None = None,
        mcp_prober: McpHealthProber | None = None,
        health_checks: HealthChecksPort | None = None,
        request_metrics: RequestMetrics | None = None,
    ) -> None:
        self._repository = repository
        self._thresholds = thresholds
        self._sms = sms_repository
        self._clients = client_factory
        self._vault = vault
        self._email = email
        self._webhook_sender = webhook_sender
        self._backup_source = backup_source or NoBackupStatusSource()
        self._mcp_prober = mcp_prober
        self._health_checks = health_checks
        self._request_metrics = request_metrics

    # -- snapshot ------------------------------------------------------

    def overview(self, *, now: datetime.datetime | None = None) -> MonitoringSnapshot:
        """Build the monitoring snapshot at ``now`` (UTC, default: current time)."""
        now = now or datetime.datetime.now(datetime.UTC)
        last_sweep = self._repository.latest_sweep_run_at()
        lag_minutes = int((now - last_sweep).total_seconds() // 60) if last_sweep is not None else None
        counts = self._repository.inbox_counts_by_status()
        stale_count, oldest_stale = self._repository.stale_inbox(
            datetime.timedelta(hours=self._thresholds.inbox_stale_hours)
        )
        oldest_age = int((now - oldest_stale).total_seconds() // 60) if oldest_stale is not None else None
        drift_count, drift_total = self._repository.open_drift_exceptions()
        if self._request_metrics is not None:
            metrics = self._request_metrics.snapshot()
            requests_total, requests_5xx, error_rate = (
                metrics.total,
                metrics.server_errors,
                metrics.error_rate,
            )
        else:
            # No metrics in this process (the worker serves no HTTP):
            # the 5xx rule skips (rate None), the panel shows "no data".
            requests_total, requests_5xx, error_rate = 0, 0, None
        return MonitoringSnapshot(
            generated_at=now,
            processes=[
                ProcessHealth(
                    name="api",
                    status=ProcessStatus.OK,
                    detail=None,
                    checked_at=now,
                ),
                self._database_health(now),
                self._tryton_health(now),
                self._worker_health(now, last_sweep, lag_minutes),
                self._mcp_health(now),
            ],
            queue_depths=QueueDepths(
                inbox_received=counts.get("received", 0),
                inbox_processing=counts.get("processing", 0),
                inbox_failed=counts.get("failed", 0),
                inbox_needs_review=counts.get("needs_review", 0),
                inbox_dead_lettered=counts.get("dead_lettered", 0),
                outbox_pending=self._repository.outbox_pending_count(),
                procrastinate_todo=self._repository.procrastinate_queue_depths(),
            ),
            stale_inbox_count=stale_count,
            oldest_unprocessed_age_minutes=oldest_age,
            worker_last_sweep_at=last_sweep,
            worker_lag_minutes=lag_minutes,
            failed_webhook_attempts_24h=self._repository.failed_webhook_attempts_since(now - _WEBHOOK_WINDOW),
            open_drift_exceptions=drift_count,
            drift_total_irr=drift_total,
            requests_total=requests_total,
            requests_5xx=requests_5xx,
            error_rate_5xx=error_rate,
            token_expiries=self._token_expiries(now),
            backups=self._backup_source.statuses(now=now),
        )

    def _database_health(self, now: datetime.datetime) -> ProcessHealth:
        if self._health_checks is None:
            return ProcessHealth(
                name="database",
                status=ProcessStatus.UNKNOWN,
                detail="No health probe configured",
                checked_at=now,
            )
        return self._health_checks.check_database()

    def _tryton_health(self, now: datetime.datetime) -> ProcessHealth:
        if self._health_checks is None:
            return ProcessHealth(
                name="tryton",
                status=ProcessStatus.UNKNOWN,
                detail="No health probe configured",
                checked_at=now,
            )
        return self._health_checks.check_tryton()

    def _worker_health(
        self,
        now: datetime.datetime,
        last_sweep: datetime.datetime | None,
        lag_minutes: int | None,
    ) -> ProcessHealth:
        if last_sweep is None or lag_minutes is None:
            return ProcessHealth(
                name="worker",
                status=ProcessStatus.UNKNOWN,
                detail="No sweep run recorded",
                checked_at=now,
            )
        if lag_minutes > self._thresholds.worker_lag_minutes:
            return ProcessHealth(
                name="worker",
                status=ProcessStatus.ERROR,
                detail=f"Last sweep {lag_minutes} minutes ago",
                checked_at=now,
            )
        return ProcessHealth(name="worker", status=ProcessStatus.OK, detail=None, checked_at=now)

    def _mcp_health(self, now: datetime.datetime) -> ProcessHealth:
        if self._mcp_prober is None:
            return ProcessHealth(
                name="mcp",
                status=ProcessStatus.UNKNOWN,
                detail="No MCP prober configured",
                checked_at=now,
            )
        return self._mcp_prober.probe()

    def _token_expiries(self, now: datetime.datetime) -> list[TokenExpiry]:
        """API keys (live) + configured token records, nearest expiry first."""
        expiries: list[TokenExpiry] = []
        for key in self._repository.api_keys_with_expiry():
            days_left = (key.expires_at.date() - now.date()).days
            expiries.append(
                TokenExpiry(
                    source="api_key",
                    label=f"{key.name} ({key.key_prefix})",
                    kind="api_key",
                    expires_at=key.expires_at,
                    days_left=days_left,
                    severity=expiry_severity(
                        days_left,
                        warning_days=self._thresholds.token_warning_days,
                        critical_days=self._thresholds.token_critical_days,
                    ),
                    runbook_ref=texts.RUNBOOK_API_KEY_ROTATION,
                    record_id=key.key_id,
                )
            )
        for record in self._repository.list_token_records():
            if not record.enabled:
                continue
            days_left = (record.expires_at.date() - now.date()).days
            expiries.append(
                TokenExpiry(
                    source="token_record",
                    label=record.label,
                    kind=record.kind.value,
                    expires_at=record.expires_at,
                    days_left=days_left,
                    severity=expiry_severity(
                        days_left,
                        warning_days=self._thresholds.token_warning_days,
                        critical_days=self._thresholds.token_critical_days,
                    ),
                    runbook_ref=record.rotation_runbook_ref
                    or (texts.RUNBOOK_FINTECH_ROTATION if record.kind.value == "fintech" else None),
                    record_id=record.id,
                )
            )
        expiries.sort(key=lambda expiry: expiry.expires_at)
        return expiries

    # -- evaluation ------------------------------------------------------

    def evaluate(self, *, now: datetime.datetime | None = None) -> EvaluationReport:
        """Evaluate the rules, persist newly fired alerts, dispatch them.

        An alert whose dedup key is already firing (open/acknowledged)
        is counted, not re-fired; a concurrent-evaluation race on the
        unique index counts the same way.
        """
        now = now or datetime.datetime.now(datetime.UTC)
        snapshot = self.overview(now=now)
        wanted = evaluate_rules(snapshot, self._thresholds)
        fired: list[Alert] = []
        already_open = 0
        dispatched: list[ChannelSendResult] = []
        for new_alert in wanted:
            if self._repository.find_open_alert(new_alert.dedup_key) is not None:
                already_open += 1
                continue
            alert = Alert(
                id=str(uuid.uuid4()),
                rule=new_alert.rule,
                severity=new_alert.severity,
                status=AlertStatus.OPEN,
                title_fa=new_alert.title_fa,
                message_fa=new_alert.message_fa,
                dedup_key=new_alert.dedup_key,
                runbook_ref=new_alert.runbook_ref,
                context=dict(new_alert.context),
                fired_at=now,
            )
            try:
                saved = self._repository.save_alert(alert)
            except MonitoringAlertAlreadyOpenError:
                already_open += 1
                continue
            fired.append(saved)
            dispatched.extend(self._dispatch(saved))
        return EvaluationReport(
            generated_at=now,
            fired=fired,
            already_open=already_open,
            dispatched=dispatched,
        )

    # -- dispatch ----------------------------------------------------------

    def _dispatch(self, alert: Alert) -> list[ChannelSendResult]:
        results: list[ChannelSendResult] = []
        for channel in self._repository.list_channels():
            if not channel.enabled:
                continue
            if _SEVERITY_ORDER[alert.severity] < _SEVERITY_ORDER[channel.min_severity]:
                continue
            results.append(self._dispatch_one(channel, alert))
        return results

    def _dispatch_one(self, channel: AlertChannel, alert: Alert) -> ChannelSendResult:
        try:
            if channel.kind is AlertChannelKind.SMS:
                return self._send_sms(channel, alert)
            if channel.kind is AlertChannelKind.EMAIL:
                return self._send_email(channel, alert)
            if self._webhook_sender is None:
                return ChannelSendResult(
                    channel_id=channel.id,
                    kind=channel.kind,
                    status="skipped",
                    detail="No webhook sender is configured.",
                )
            return self._webhook_sender.send(channel=channel, alert=alert)
        except Exception as exc:  # noqa: BLE001 - one channel must never break the evaluation
            logger.warning("Alert dispatch failed for channel %s: %s", channel.id, exc)
            return ChannelSendResult(
                channel_id=channel.id,
                kind=channel.kind,
                status="failed",
                detail=f"Unexpected dispatch error: {exc}",
            )

    def _send_sms(self, channel: AlertChannel, alert: Alert) -> ChannelSendResult:
        if self._sms is None or self._clients is None:
            return ChannelSendResult(
                channel_id=channel.id,
                kind=channel.kind,
                status="skipped",
                detail="SMS dispatch is not configured.",
            )
        if channel.provider_key is None or channel.installation_id is None:
            return ChannelSendResult(
                channel_id=channel.id,
                kind=channel.kind,
                status="skipped",
                detail="SMS channel has no provider or installation.",
            )
        provider = self._sms.get_provider_by_key(channel.provider_key, installation_id=channel.installation_id)
        if provider is None:
            return ChannelSendResult(
                channel_id=channel.id,
                kind=channel.kind,
                status="failed",
                detail=f"SMS provider {channel.provider_key!r} not found in this installation.",
            )
        if not provider.enabled:
            return ChannelSendResult(
                channel_id=channel.id,
                kind=channel.kind,
                status="skipped",
                detail="SMS provider is disabled.",
            )
        reason = provider_sendable_reason(provider)
        if reason is not None:
            return ChannelSendResult(
                channel_id=channel.id,
                kind=channel.kind,
                status="skipped",
                detail=f"SMS provider cannot send: {reason}",
            )
        try:
            client = build_provider_client(self._sms, self._clients, self._vault, provider)
            try:
                result = client.send(
                    to=channel.target,
                    message=f"{alert.title_fa}\n{alert.message_fa}",
                    sender_line=provider.line_number,
                )
            finally:
                client.close()
        except SmsError as exc:
            return ChannelSendResult(
                channel_id=channel.id,
                kind=channel.kind,
                status="failed",
                detail=f"SMS send failed ({exc.code}): {exc.message}",
            )
        if result.status is SmsDeliveryStatus.FAILED:
            return ChannelSendResult(
                channel_id=channel.id,
                kind=channel.kind,
                status="failed",
                detail="The provider reported the message as failed.",
            )
        return ChannelSendResult(
            channel_id=channel.id,
            kind=channel.kind,
            status="sent",
            detail=f"Provider message {result.provider_message_id}.",
        )

    def _send_email(self, channel: AlertChannel, alert: Alert) -> ChannelSendResult:
        if self._email is None:
            # No email provider exists yet (the F3 email provider was
            # never built — only SMS landed, US-544): skip honestly,
            # exactly like the reminder service does.
            return ChannelSendResult(
                channel_id=channel.id,
                kind=channel.kind,
                status="skipped",
                detail="No email provider is configured yet.",
            )
        try:
            self._email.send(to=channel.target, subject=alert.title_fa, body=alert.message_fa)
        except Exception as exc:  # noqa: BLE001 - a failed email is a failed send, not a crash
            return ChannelSendResult(
                channel_id=channel.id,
                kind=channel.kind,
                status="failed",
                detail=f"Email send failed: {exc}",
            )
        return ChannelSendResult(channel_id=channel.id, kind=channel.kind, status="sent")

    # -- alert lifecycle ---------------------------------------------------

    def list_alerts(
        self,
        *,
        status: AlertStatus | None = None,
        rule: AlertRule | None = None,
        limit: int = 50,
    ) -> list[Alert]:
        """Return alerts, newest first."""
        return self._repository.list_alerts(status=status, rule=rule, limit=limit)

    def acknowledge_alert(self, alert_id: str, *, actor: str, now: datetime.datetime | None = None) -> Alert:
        """Acknowledge an alert (404 when unknown)."""
        alert = self._repository.get_alert(alert_id)
        if alert is None:
            raise MonitoringNotFoundError("alert", alert_id)
        alert.status = AlertStatus.ACKNOWLEDGED
        alert.acknowledged_at = now or datetime.datetime.now(datetime.UTC)
        alert.acknowledged_by = actor
        return self._repository.update_alert(alert)

    def resolve_alert(self, alert_id: str, *, actor: str, now: datetime.datetime | None = None) -> Alert:
        """Resolve an alert (404 when unknown)."""
        alert = self._repository.get_alert(alert_id)
        if alert is None:
            raise MonitoringNotFoundError("alert", alert_id)
        alert.status = AlertStatus.RESOLVED
        alert.resolved_at = now or datetime.datetime.now(datetime.UTC)
        alert.resolved_by = actor
        return self._repository.update_alert(alert)

    # -- channels ----------------------------------------------------------

    def list_channels(self) -> list[AlertChannel]:
        """Return every configured channel, oldest first."""
        return self._repository.list_channels()

    def create_channel(self, payload: dict[str, object], *, installation_id: str | None = None) -> AlertChannel:
        """Validate + create a channel (SMS installation defaults to the caller's)."""
        validated = validate_channel_payload(payload)
        resolved_installation = validated.installation_id or installation_id
        if validated.kind is AlertChannelKind.SMS and resolved_installation is None:
            raise MonitoringValidationError(
                "Channel payload failed validation.",
                field_errors={"installation_id": "کانال پیامکی به شناسهٔ نصب نیاز دارد."},
            )
        return self._repository.save_channel(
            AlertChannel(
                id=str(uuid.uuid4()),
                kind=validated.kind,
                name=validated.name,
                target=validated.target,
                enabled=validated.enabled,
                min_severity=validated.min_severity,
                provider_key=validated.provider_key,
                installation_id=resolved_installation,
            )
        )

    def update_channel(
        self,
        channel_id: str,
        payload: dict[str, object],
        *,
        installation_id: str | None = None,
    ) -> AlertChannel:
        """Validate + replace a channel (404 when unknown; full replace like PUT)."""
        existing = self._repository.get_channel(channel_id)
        if existing is None:
            raise MonitoringNotFoundError("channel", channel_id)
        validated = validate_channel_payload(payload)
        resolved_installation = validated.installation_id or existing.installation_id or installation_id
        if validated.kind is AlertChannelKind.SMS and resolved_installation is None:
            raise MonitoringValidationError(
                "Channel payload failed validation.",
                field_errors={"installation_id": "کانال پیامکی به شناسهٔ نصب نیاز دارد."},
            )
        return self._repository.save_channel(
            AlertChannel(
                id=channel_id,
                kind=validated.kind,
                name=validated.name,
                target=validated.target,
                enabled=validated.enabled,
                min_severity=validated.min_severity,
                provider_key=validated.provider_key,
                installation_id=resolved_installation,
            )
        )

    def delete_channel(self, channel_id: str) -> None:
        """Delete a channel (404 when unknown)."""
        if not self._repository.delete_channel(channel_id):
            raise MonitoringNotFoundError("channel", channel_id)

    # -- token records -----------------------------------------------------

    def list_token_records(self) -> list[TokenRecord]:
        """Return every configured token record, oldest first."""
        return self._repository.list_token_records()

    def create_token_record(self, payload: dict[str, object], *, now: datetime.datetime | None = None) -> TokenRecord:
        """Validate + create a token record."""
        validated = validate_token_payload(payload, now=now or datetime.datetime.now(datetime.UTC))
        return self._repository.save_token_record(
            TokenRecord(
                id=str(uuid.uuid4()),
                kind=validated.kind,
                label=validated.label,
                expires_at=validated.expires_at,
                rotation_runbook_ref=validated.rotation_runbook_ref,
                enabled=validated.enabled,
                notes=validated.notes,
            )
        )

    def update_token_record(
        self,
        record_id: str,
        payload: dict[str, object],
        *,
        now: datetime.datetime | None = None,
    ) -> TokenRecord:
        """Validate + replace a token record (404 when unknown)."""
        if self._repository.get_token_record(record_id) is None:
            raise MonitoringNotFoundError("token record", record_id)
        validated = validate_token_payload(payload, now=now or datetime.datetime.now(datetime.UTC))
        return self._repository.save_token_record(
            TokenRecord(
                id=record_id,
                kind=validated.kind,
                label=validated.label,
                expires_at=validated.expires_at,
                rotation_runbook_ref=validated.rotation_runbook_ref,
                enabled=validated.enabled,
                notes=validated.notes,
            )
        )

    def delete_token_record(self, record_id: str) -> None:
        """Delete a token record (404 when unknown)."""
        if not self._repository.delete_token_record(record_id):
            raise MonitoringNotFoundError("token record", record_id)
