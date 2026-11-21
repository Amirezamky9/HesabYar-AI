"""Monitoring domain (US-704, Phase 7): alerts, channels, token records.

- :mod:`models` — alerts, channels, token records, the snapshot;
- :mod:`rules` — pure alert-rule evaluation over a snapshot;
- :mod:`texts` — fixed Persian alert texts;
- :mod:`validation` — channel + token payload validation;
- :mod:`metrics` — in-process request counters (the 5xx-rate source);
- :mod:`ports` — repository, backup-status source (the US-703
  contract), MCP prober, health checks, channel sender;
- :mod:`errors` — typed monitoring errors.
"""

from hesabyar.domain.monitoring.errors import (
    MonitoringAlertAlreadyOpenError,
    MonitoringError,
    MonitoringNotFoundError,
    MonitoringValidationError,
)
from hesabyar.domain.monitoring.metrics import (
    RequestMetrics,
    RequestMetricsSnapshot,
    request_metrics,
)
from hesabyar.domain.monitoring.models import (
    BACKUP_DATABASES,
    FIRING_STATUSES,
    Alert,
    AlertChannel,
    AlertChannelKind,
    AlertRule,
    AlertSeverity,
    AlertStatus,
    BackupStatus,
    BackupStatusValue,
    ChannelSendResult,
    EvaluationReport,
    MonitoringSnapshot,
    MonitoringThresholds,
    NewAlert,
    ProcessHealth,
    ProcessStatus,
    QueueDepths,
    TokenExpiry,
    TokenKind,
    TokenRecord,
)
from hesabyar.domain.monitoring.ports import (
    AlertChannelSender,
    ApiKeyExpiry,
    BackupStatusSource,
    HealthChecksPort,
    McpHealthProber,
    MonitoringRepositoryPort,
    NoBackupStatusSource,
)
from hesabyar.domain.monitoring.rules import evaluate_rules, expiry_severity
from hesabyar.domain.monitoring.validation import (
    ValidatedChannel,
    ValidatedTokenRecord,
    validate_channel_payload,
    validate_token_payload,
)

__all__ = [
    "BACKUP_DATABASES",
    "FIRING_STATUSES",
    "Alert",
    "AlertChannel",
    "AlertChannelKind",
    "AlertChannelSender",
    "AlertRule",
    "AlertSeverity",
    "AlertStatus",
    "ApiKeyExpiry",
    "BackupStatus",
    "BackupStatusSource",
    "BackupStatusValue",
    "ChannelSendResult",
    "EvaluationReport",
    "HealthChecksPort",
    "McpHealthProber",
    "MonitoringAlertAlreadyOpenError",
    "MonitoringError",
    "MonitoringNotFoundError",
    "MonitoringRepositoryPort",
    "MonitoringSnapshot",
    "MonitoringThresholds",
    "MonitoringValidationError",
    "NewAlert",
    "NoBackupStatusSource",
    "ProcessHealth",
    "ProcessStatus",
    "QueueDepths",
    "RequestMetrics",
    "RequestMetricsSnapshot",
    "TokenExpiry",
    "TokenKind",
    "TokenRecord",
    "ValidatedChannel",
    "ValidatedTokenRecord",
    "evaluate_rules",
    "expiry_severity",
    "request_metrics",
    "validate_channel_payload",
    "validate_token_payload",
]
