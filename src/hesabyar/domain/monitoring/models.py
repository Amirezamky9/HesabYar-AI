"""Monitoring domain models (US-704, Phase 7).

The alert surface of HesabYar: fired alerts, alert channels, configured
token records, and the monitoring snapshot the alert rules evaluate.

Token model (threat model §3, read literally): the only tokens with an
expiry to alert on are HesabYar API keys (``api_keys.expires_at``, read
live from that table) and *configured* token records (this module's
``TokenRecord`` — the Fintech OAuth token is the first: no live Fintech
token exists yet, the ۵ت remainder is deferred, so the alert fires from
a configured record, never from a live token). Tokens the threat model
marks "no expiry" (Tryton key, webhook secrets, SMS credentials, vault
key, pepper, Postgres credentials) have no expiry row and no alert.
Gateway OAuth access tokens live in memory only (~3600s, auto-refreshed)
and are not alertable from the database either.

Money: drift totals are Rial integers (``digits=0``); the 5xx error rate
is a ratio, not money, so a float is correct there.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from enum import StrEnum, unique


@unique
class AlertRule(StrEnum):
    """The seven alert rules (US-704 acceptance criterion 2)."""

    INBOX_STALE = "inbox_stale"
    WORKER_LAG = "worker_lag"
    TOKEN_EXPIRING = "token_expiring"
    SETTLEMENT_DRIFT = "settlement_drift"
    WEBHOOK_FAILED = "webhook_failed"
    BACKUP_FAILED = "backup_failed"
    ERROR_RATE_5XX = "error_rate_5xx"


@unique
class AlertSeverity(StrEnum):
    """Alert severity: critical pages the operator, warning waits."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@unique
class AlertStatus(StrEnum):
    """Fired-alert lifecycle: open → acknowledged → resolved."""

    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"


#: Statuses that count as "this alert is still firing" for dedup: a
#: rule whose alert is open OR acknowledged must not fire a second row.
FIRING_STATUSES: frozenset[AlertStatus] = frozenset({AlertStatus.OPEN, AlertStatus.ACKNOWLEDGED})


@unique
class AlertChannelKind(StrEnum):
    """Alert channel kinds (US-704 acceptance criterion 3).

    SMS goes through the existing pluggable SMS registry (Kavenegar +
    sms.ir first — US-544); email goes through ``EmailSenderPort``
    (no provider exists yet, so email sends are honestly skipped
    until the F3 email provider lands); webhook POSTs JSON to a
    configured URL. No provider is hard-coded anywhere.
    """

    SMS = "sms"
    EMAIL = "email"
    WEBHOOK = "webhook"


@unique
class TokenKind(StrEnum):
    """Kind of a configured token record.

    ``fintech`` is the Fintech OAuth token (configured record only —
    no live token exists). ``other`` covers any future configured
    token (a gateway credential with a known expiry, an eNamad-side
    token, ...). API keys are NOT token records: they are read live
    from ``api_keys`` (see :class:`TokenExpiry`).
    """

    FINTECH = "fintech"
    OTHER = "other"


@unique
class ProcessStatus(StrEnum):
    """Health of one process/component in the monitoring snapshot."""

    OK = "ok"
    DEGRADED = "degraded"
    ERROR = "error"
    UNKNOWN = "unknown"


@unique
class BackupStatusValue(StrEnum):
    """Status of one database's latest backup (the US-703 contract)."""

    OK = "ok"
    FAILED = "failed"
    STALE = "stale"
    UNKNOWN = "unknown"


#: The two databases US-703 backs up (HesabYar Postgres + Tryton Postgres).
BACKUP_DATABASES: tuple[str, str] = ("hesabyar", "tryton")


@dataclass(frozen=True)
class NewAlert:
    """One alert a rule wants to fire (before persistence/dedup).

    ``dedup_key`` is stable per firing condition (per rule, or per
    rule + subject for token alerts): while an alert with this key is
    open or acknowledged, the rule does not fire again. ``context``
    holds JSON-safe values only (str/int/float/bool/None/lists/dicts).
    ``runbook_ref`` is a documentation reference (``docs/monitoring.md``
    section), not a URL — there is no deployed docs site to link to.
    """

    rule: AlertRule
    severity: AlertSeverity
    title_fa: str
    message_fa: str
    dedup_key: str
    runbook_ref: str | None = None
    context: dict[str, object] = field(default_factory=dict)


@dataclass
class Alert:
    """One fired alert, persisted in ``monitoring_alerts``."""

    id: str
    rule: AlertRule
    severity: AlertSeverity
    status: AlertStatus
    title_fa: str
    message_fa: str
    dedup_key: str
    runbook_ref: str | None = None
    context: dict[str, object] = field(default_factory=dict)
    fired_at: datetime.datetime | None = None
    acknowledged_at: datetime.datetime | None = None
    acknowledged_by: str | None = None
    resolved_at: datetime.datetime | None = None
    resolved_by: str | None = None
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


@dataclass
class AlertChannel:
    """One configured alert channel (``monitoring_alert_channels``).

    ``target`` is the destination: a mobile number for SMS, an email
    address for email, an https URL for webhook. ``provider_key`` is
    the SMS registry key (``kavenegar`` / ``sms_ir`` / ``simulator``)
    for SMS channels and None otherwise. ``installation_id`` is the
    installation whose SMS registry sends for SMS channels (the
    registry is installation-scoped); it is filled from the
    operator's session at creation when omitted. ``min_severity``
    gates dispatch: only alerts at or above it go out on this channel.
    """

    id: str
    kind: AlertChannelKind
    name: str
    target: str
    enabled: bool = True
    min_severity: AlertSeverity = AlertSeverity.WARNING
    provider_key: str | None = None
    installation_id: str | None = None
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


@dataclass
class TokenRecord:
    """One configured token record (``monitoring_token_records``).

    A token the operator tells HesabYar about (the Fintech OAuth
    token, until the ۵ت remainder lands a live one): the alert fires
    from ``expires_at`` + the warning/critical day thresholds, and
    ``rotation_runbook_ref`` names the doc section that tells the
    operator how to rotate it.
    """

    id: str
    kind: TokenKind
    label: str
    expires_at: datetime.datetime
    rotation_runbook_ref: str | None = None
    enabled: bool = True
    notes: str | None = None
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


@dataclass(frozen=True)
class TokenExpiry:
    """One token approaching (or past) expiry, for the panel + rules.

    ``source`` is ``"api_key"`` (a live ``api_keys`` row) or
    ``"token_record"`` (a configured :class:`TokenRecord`).
    ``days_left`` is negative past expiry. ``severity`` is None when
    the token is outside the warning window (no alert); the panel
    still lists it so the operator sees the full expiry picture.
    """

    source: str
    label: str
    kind: str
    expires_at: datetime.datetime
    days_left: int
    severity: AlertSeverity | None
    runbook_ref: str | None = None
    record_id: str | None = None


@dataclass(frozen=True)
class BackupStatus:
    """Latest backup state for one database (the US-703 contract).

    Produced by a :class:`BackupStatusSource` (see ports). Before
    any backup run is recorded, the status is ``UNKNOWN`` with
    ``source="job-registry"`` — the panel shows "نامشخص", never a
    fabricated success.
    """

    database: str
    status: BackupStatusValue
    last_success_at: datetime.datetime | None = None
    last_attempt_at: datetime.datetime | None = None
    error: str | None = None
    source: str = "none"


@dataclass(frozen=True)
class QueueDepths:
    """Queue depths at snapshot time.

    ``procrastinate_todo`` maps queue name → pending (``todo``) job
    count; it is None when the Procrastinate schema is absent (the
    worker never ran) — unknown, not zero.
    """

    inbox_received: int = 0
    inbox_processing: int = 0
    inbox_failed: int = 0
    inbox_needs_review: int = 0
    inbox_dead_lettered: int = 0
    outbox_pending: int = 0
    procrastinate_todo: dict[str, int] | None = None


@dataclass(frozen=True)
class ProcessHealth:
    """Health of one process (API, worker, MCP server, Tryton adapter)."""

    name: str
    status: ProcessStatus
    detail: str | None = None
    checked_at: datetime.datetime | None = None


@dataclass(frozen=True)
class MonitoringThresholds:
    """Alert-rule thresholds (defaults match ``MonitoringSettings``).

    ``drift`` needs no amount threshold: an open
    ``amount_mismatch``/``fee_mismatch`` exception already means the
    tie-out difference passed its tolerance (settlement C6), so ANY
    open drift exception alerts.
    """

    inbox_stale_hours: int = 24
    worker_lag_minutes: int = 10
    token_warning_days: int = 14
    token_critical_days: int = 3
    webhook_failures_24h: int = 5
    error_rate_5xx: float = 0.05
    error_rate_min_requests: int = 100
    backup_max_age_hours: int = 26


@dataclass(frozen=True)
class MonitoringSnapshot:
    """Everything the rules evaluate + the panel shows, at one instant.

    ``error_rate_5xx`` is None when this process has no request
    metrics (the worker process serves no HTTP — its evaluation
    skips the 5xx rule; see docs/monitoring.md). ``worker_lag_minutes``
    is None when no sweep run was ever recorded (fresh install / the
    worker never ran) — the rule treats that as a warning, not as
    zero lag.
    """

    generated_at: datetime.datetime
    processes: list[ProcessHealth] = field(default_factory=list)
    queue_depths: QueueDepths = field(default_factory=QueueDepths)
    stale_inbox_count: int = 0
    oldest_unprocessed_age_minutes: int | None = None
    worker_last_sweep_at: datetime.datetime | None = None
    worker_lag_minutes: int | None = None
    failed_webhook_attempts_24h: int = 0
    open_drift_exceptions: int = 0
    drift_total_irr: int = 0
    requests_total: int = 0
    requests_5xx: int = 0
    error_rate_5xx: float | None = None
    token_expiries: list[TokenExpiry] = field(default_factory=list)
    backups: list[BackupStatus] = field(default_factory=list)


@dataclass(frozen=True)
class ChannelSendResult:
    """Outcome of one alert dispatch down one channel."""

    channel_id: str
    kind: AlertChannelKind
    status: str  # "sent" | "skipped" | "failed"
    detail: str | None = None


@dataclass(frozen=True)
class EvaluationReport:
    """What one evaluation run did (returned by POST /monitoring/evaluate)."""

    generated_at: datetime.datetime
    fired: list[Alert] = field(default_factory=list)
    already_open: int = 0
    dispatched: list[ChannelSendResult] = field(default_factory=list)
