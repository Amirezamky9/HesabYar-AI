"""Request/response schemas for the monitoring endpoints (US-704).

Responses map from the domain objects — no ORM object ever leaks.
Money is Rial integers (``digits=0``); the 5xx rate is a ratio.
Alert text is Persian; enum values stay English wire values.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from hesabyar.domain.monitoring.models import (
    Alert,
    AlertChannel,
    EvaluationReport,
    MonitoringSnapshot,
    ProcessHealth,
    TokenRecord,
)


class ProcessHealthResponse(BaseModel):
    """Health of one process (api / database / tryton / worker / mcp)."""

    name: str = Field(description="Process name: api, database, tryton, worker or mcp")
    status: str = Field(description="ok | degraded | error | unknown")
    detail: str | None = Field(description="Short failure hint; never internals")
    checked_at: datetime | None = Field(description="When this check ran (UTC)")

    @classmethod
    def from_health(cls, health: ProcessHealth) -> ProcessHealthResponse:
        """Map a domain process-health to its wire form."""
        return cls(
            name=health.name,
            status=health.status.value,
            detail=health.detail,
            checked_at=health.checked_at,
        )


class QueueDepthsResponse(BaseModel):
    """Queue depths at snapshot time."""

    inbox_received: int = Field(description="Inbox events awaiting processing")
    inbox_processing: int = Field(description="Inbox events being processed now")
    inbox_failed: int = Field(description="Inbox events in failed state")
    inbox_needs_review: int = Field(description="Inbox events awaiting review")
    inbox_dead_lettered: int = Field(description="Dead-lettered inbox events")
    outbox_pending: int = Field(description="Outbox events still awaiting delivery")
    procrastinate_todo: dict[str, int] | None = Field(
        description="Pending Procrastinate jobs per queue; null when the queue schema is absent"
    )


class TokenExpiryResponse(BaseModel):
    """One token approaching (or past) expiry."""

    source: str = Field(description="api_key (live api_keys row) or token_record (configured)")
    label: str = Field(description="Token label (API key name + prefix, or the record label)")
    kind: str = Field(description="api_key | fintech | other")
    expires_at: datetime = Field(description="Token expiry (UTC)")
    days_left: int = Field(description="Calendar days to expiry (negative past expiry)")
    severity: str | None = Field(
        description="Alert severity at this distance (warning/critical), or null when outside the window"
    )
    runbook_ref: str | None = Field(description="Rotation runbook reference, if any")
    record_id: str | None = Field(description="API key id or token-record id")


class BackupStatusResponse(BaseModel):
    """Latest backup state for one database (the US-703 contract)."""

    database: str = Field(description="hesabyar | tryton")
    status: str = Field(description="ok | failed | stale | unknown")
    last_success_at: datetime | None = Field(description="Last successful backup (UTC)")
    last_attempt_at: datetime | None = Field(description="Last backup attempt (UTC)")
    error: str | None = Field(description="Latest failure's short text, if any")
    source: str = Field(description="Status source ('none' until US-703 lands)")


class MonitoringOverviewResponse(BaseModel):
    """The monitoring snapshot: everything the operator panel shows."""

    generated_at: datetime = Field(description="Snapshot time (UTC)")
    processes: list[ProcessHealthResponse] = Field(description="Per-process health")
    queue_depths: QueueDepthsResponse = Field(description="Inbox/outbox/queue depths")
    stale_inbox_count: int = Field(description="Events stuck past the staleness threshold")
    oldest_unprocessed_age_minutes: int | None = Field(
        description="Age of the oldest stuck event in minutes; null when none is stuck"
    )
    worker_last_sweep_at: datetime | None = Field(
        description="Last recorded sweep run (the worker heartbeat); null when never run"
    )
    worker_lag_minutes: int | None = Field(description="Minutes since the last sweep; null when never run")
    failed_webhook_attempts_24h: int = Field(description="Failed integration attempts in the last 24 hours")
    open_drift_exceptions: int = Field(description="Open settlement drift exceptions")
    drift_total_irr: int = Field(description="Σ|expected − actual| of open drift exceptions (Rial)")
    requests_total: int = Field(description="Requests counted by this process since start")
    requests_5xx: int = Field(description="5xx responses counted by this process since start")
    error_rate_5xx: float | None = Field(
        description="5xx rate in this process; null when this process serves no HTTP (worker)"
    )
    token_expiries: list[TokenExpiryResponse] = Field(description="Tokens with an expiry, nearest first")
    backups: list[BackupStatusResponse] = Field(description="Backup status per database")

    @classmethod
    def from_snapshot(cls, snapshot: MonitoringSnapshot) -> MonitoringOverviewResponse:
        """Map a domain snapshot to its wire form."""
        return cls(
            generated_at=snapshot.generated_at,
            processes=[ProcessHealthResponse.from_health(process) for process in snapshot.processes],
            queue_depths=QueueDepthsResponse(
                inbox_received=snapshot.queue_depths.inbox_received,
                inbox_processing=snapshot.queue_depths.inbox_processing,
                inbox_failed=snapshot.queue_depths.inbox_failed,
                inbox_needs_review=snapshot.queue_depths.inbox_needs_review,
                inbox_dead_lettered=snapshot.queue_depths.inbox_dead_lettered,
                outbox_pending=snapshot.queue_depths.outbox_pending,
                procrastinate_todo=snapshot.queue_depths.procrastinate_todo,
            ),
            stale_inbox_count=snapshot.stale_inbox_count,
            oldest_unprocessed_age_minutes=snapshot.oldest_unprocessed_age_minutes,
            worker_last_sweep_at=snapshot.worker_last_sweep_at,
            worker_lag_minutes=snapshot.worker_lag_minutes,
            failed_webhook_attempts_24h=snapshot.failed_webhook_attempts_24h,
            open_drift_exceptions=snapshot.open_drift_exceptions,
            drift_total_irr=snapshot.drift_total_irr,
            requests_total=snapshot.requests_total,
            requests_5xx=snapshot.requests_5xx,
            error_rate_5xx=snapshot.error_rate_5xx,
            token_expiries=[
                TokenExpiryResponse(
                    source=expiry.source,
                    label=expiry.label,
                    kind=expiry.kind,
                    expires_at=expiry.expires_at,
                    days_left=expiry.days_left,
                    severity=expiry.severity.value if expiry.severity else None,
                    runbook_ref=expiry.runbook_ref,
                    record_id=expiry.record_id,
                )
                for expiry in snapshot.token_expiries
            ],
            backups=[
                BackupStatusResponse(
                    database=backup.database,
                    status=backup.status.value,
                    last_success_at=backup.last_success_at,
                    last_attempt_at=backup.last_attempt_at,
                    error=backup.error,
                    source=backup.source,
                )
                for backup in snapshot.backups
            ],
        )


class AlertResponse(BaseModel):
    """One fired alert."""

    id: str = Field(description="Alert id (UUID)")
    rule: str = Field(description="Alert rule")
    severity: str = Field(description="info | warning | critical")
    status: str = Field(description="open | acknowledged | resolved")
    title_fa: str = Field(description="Persian alert title")
    message_fa: str = Field(description="Persian alert message")
    dedup_key: str = Field(description="Stable dedup key per firing condition")
    runbook_ref: str | None = Field(description="Documentation reference, if any")
    context: dict[str, Any] = Field(description="Rule context (counts, Rial amounts, ids)")
    fired_at: datetime | None = Field(description="When the alert fired (UTC)")
    acknowledged_at: datetime | None = Field(description="Acknowledgement time (UTC)")
    acknowledged_by: str | None = Field(description="Operator that acknowledged")
    resolved_at: datetime | None = Field(description="Resolution time (UTC)")
    resolved_by: str | None = Field(description="Operator that resolved")

    @classmethod
    def from_alert(cls, alert: Alert) -> AlertResponse:
        """Map a domain alert to its wire form."""
        return cls(
            id=alert.id,
            rule=alert.rule.value,
            severity=alert.severity.value,
            status=alert.status.value,
            title_fa=alert.title_fa,
            message_fa=alert.message_fa,
            dedup_key=alert.dedup_key,
            runbook_ref=alert.runbook_ref,
            context=dict(alert.context),
            fired_at=alert.fired_at,
            acknowledged_at=alert.acknowledged_at,
            acknowledged_by=alert.acknowledged_by,
            resolved_at=alert.resolved_at,
            resolved_by=alert.resolved_by,
        )


class ChannelSendResultResponse(BaseModel):
    """Outcome of one alert dispatch down one channel."""

    channel_id: str = Field(description="Channel id (UUID)")
    kind: str = Field(description="sms | email | webhook")
    status: str = Field(description="sent | skipped | failed")
    detail: str | None = Field(description="Short outcome detail")


class EvaluationResponse(BaseModel):
    """What one evaluation run did."""

    generated_at: datetime = Field(description="Evaluation time (UTC)")
    fired: list[AlertResponse] = Field(description="Newly fired alerts")
    already_open: int = Field(description="Rules already firing (deduped, not re-fired)")
    dispatched: list[ChannelSendResultResponse] = Field(description="Dispatch outcomes for the newly fired alerts")

    @classmethod
    def from_report(cls, report: EvaluationReport) -> EvaluationResponse:
        """Map a domain evaluation report to its wire form."""
        return cls(
            generated_at=report.generated_at,
            fired=[AlertResponse.from_alert(alert) for alert in report.fired],
            already_open=report.already_open,
            dispatched=[
                ChannelSendResultResponse(
                    channel_id=result.channel_id,
                    kind=result.kind.value,
                    status=result.status,
                    detail=result.detail,
                )
                for result in report.dispatched
            ],
        )


class AlertChannelResponse(BaseModel):
    """One configured alert channel."""

    id: str = Field(description="Channel id (UUID)")
    kind: str = Field(description="sms | email | webhook")
    name: str = Field(description="Operator-facing channel name")
    target: str = Field(description="Mobile number, email address or webhook URL")
    enabled: bool = Field(description="False stops dispatch on this channel")
    min_severity: str = Field(description="Only alerts at or above this go out here")
    provider_key: str | None = Field(description="SMS registry key (SMS channels)")
    installation_id: str | None = Field(description="Installation whose SMS registry sends")

    @classmethod
    def from_channel(cls, channel: AlertChannel) -> AlertChannelResponse:
        """Map a domain channel to its wire form."""
        return cls(
            id=channel.id,
            kind=channel.kind.value,
            name=channel.name,
            target=channel.target,
            enabled=channel.enabled,
            min_severity=channel.min_severity.value,
            provider_key=channel.provider_key,
            installation_id=channel.installation_id,
        )


class AlertChannelRequest(BaseModel):
    """Create/replace an alert channel (full replace on update)."""

    kind: str = Field(description="sms | email | webhook")
    name: str = Field(description="Operator-facing channel name (1–128 chars)")
    target: str = Field(description="Mobile number (SMS), email address, or https URL (webhook)")
    enabled: bool = Field(default=True, description="False stops dispatch on this channel")
    min_severity: str = Field(default="warning", description="info | warning | critical")
    provider_key: str | None = Field(default=None, description="SMS registry key — required for SMS channels")
    installation_id: str | None = Field(
        default=None,
        description="Installation whose SMS registry sends — defaults to the caller's",
    )


class TokenRecordResponse(BaseModel):
    """One configured token record."""

    id: str = Field(description="Token record id (UUID)")
    kind: str = Field(description="fintech | other")
    label: str = Field(description="Operator-facing token label")
    expires_at: datetime = Field(description="Token expiry (UTC)")
    rotation_runbook_ref: str | None = Field(description="Rotation runbook reference")
    enabled: bool = Field(description="False stops expiry alerts for this token")
    notes: str | None = Field(description="Operator notes")

    @classmethod
    def from_record(cls, record: TokenRecord) -> TokenRecordResponse:
        """Map a domain token record to its wire form."""
        return cls(
            id=record.id,
            kind=record.kind.value,
            label=record.label,
            expires_at=record.expires_at,
            rotation_runbook_ref=record.rotation_runbook_ref,
            enabled=record.enabled,
            notes=record.notes,
        )


class TokenRecordRequest(BaseModel):
    """Create/replace a token record (full replace on update)."""

    kind: str = Field(description="fintech | other")
    label: str = Field(description="Operator-facing token label (1–128 chars)")
    expires_at: datetime = Field(description="Token expiry (ISO 8601, not in the past)")
    rotation_runbook_ref: str | None = Field(default=None, description="Rotation runbook reference (defaults per kind)")
    enabled: bool = Field(default=True, description="False stops expiry alerts")
    notes: str | None = Field(default=None, description="Operator notes (max 1024 chars)")
