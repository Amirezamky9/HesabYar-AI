"""Alert rules (US-704) — pure evaluation over a snapshot.

``evaluate_rules`` takes a :class:`MonitoringSnapshot` + thresholds
and returns the alerts that SHOULD be firing right now. Persistence,
dedup and dispatch live in the application service; this module has
no I/O and no clock (``now`` is passed in only for the rate text —
rules never read the system time).

Severity policy:
- inbox stale, worker lag, settlement drift, backup failure and a
  high 5xx rate are CRITICAL (money or the pipeline is already hurt);
- a token inside the warning window, a stale backup and failed
  webhooks are WARNING (about to hurt);
- a token inside the critical window (or expired) is CRITICAL.
"""

from __future__ import annotations

from hesabyar.domain.monitoring import texts
from hesabyar.domain.monitoring.models import (
    AlertRule,
    AlertSeverity,
    BackupStatusValue,
    MonitoringSnapshot,
    MonitoringThresholds,
    NewAlert,
    TokenExpiry,
)
from hesabyar.domain.sms.templates import to_persian_digits


def expiry_severity(days_left: int, *, warning_days: int, critical_days: int) -> AlertSeverity | None:
    """Severity for a token ``days_left`` from expiry, or None when safe.

    Expired (negative) and inside the critical window → CRITICAL;
    inside the warning window → WARNING; beyond it → None.
    """
    if days_left <= critical_days:
        return AlertSeverity.CRITICAL
    if days_left <= warning_days:
        return AlertSeverity.WARNING
    return None


def _token_runbook(kind: str) -> str:
    if kind == "fintech":
        return texts.RUNBOOK_FINTECH_ROTATION
    if kind == "api_key":
        return texts.RUNBOOK_API_KEY_ROTATION
    return texts.RUNBOOK_FINTECH_ROTATION


def _token_alert(expiry: TokenExpiry, severity: AlertSeverity) -> NewAlert:
    title, message = texts.token_expiring_text(label=expiry.label, days_left=expiry.days_left, kind=expiry.kind)
    subject = expiry.record_id or expiry.label
    return NewAlert(
        rule=AlertRule.TOKEN_EXPIRING,
        severity=severity,
        title_fa=title,
        message_fa=message,
        dedup_key=f"token_expiring:{expiry.source}:{subject}",
        runbook_ref=expiry.runbook_ref or _token_runbook(expiry.kind),
        context={
            "source": expiry.source,
            "kind": expiry.kind,
            "label": expiry.label,
            "days_left": expiry.days_left,
            "expires_at": expiry.expires_at.isoformat(),
        },
    )


def evaluate_rules(
    snapshot: MonitoringSnapshot,
    thresholds: MonitoringThresholds,
) -> list[NewAlert]:
    """Evaluate every rule against the snapshot, in rule order."""
    alerts: list[NewAlert] = []

    # -- inbox staleness -------------------------------------------------
    if snapshot.stale_inbox_count > 0:
        title, message = texts.inbox_stale_text(
            count=snapshot.stale_inbox_count,
            hours=thresholds.inbox_stale_hours,
        )
        alerts.append(
            NewAlert(
                rule=AlertRule.INBOX_STALE,
                severity=AlertSeverity.CRITICAL,
                title_fa=title,
                message_fa=message,
                dedup_key="inbox_stale",
                runbook_ref=texts.RUNBOOK_INBOX_STALE,
                context={"stale_count": snapshot.stale_inbox_count},
            )
        )

    # -- worker lag ------------------------------------------------------
    if snapshot.worker_lag_minutes is None:
        title, message = texts.worker_never_ran_text()
        alerts.append(
            NewAlert(
                rule=AlertRule.WORKER_LAG,
                severity=AlertSeverity.WARNING,
                title_fa=title,
                message_fa=message,
                dedup_key="worker_lag",
                runbook_ref=texts.RUNBOOK_WORKER_LAG,
                context={"last_sweep_at": None},
            )
        )
    elif snapshot.worker_lag_minutes > thresholds.worker_lag_minutes:
        title, message = texts.worker_lag_text(lag_minutes=snapshot.worker_lag_minutes)
        alerts.append(
            NewAlert(
                rule=AlertRule.WORKER_LAG,
                severity=AlertSeverity.CRITICAL,
                title_fa=title,
                message_fa=message,
                dedup_key="worker_lag",
                runbook_ref=texts.RUNBOOK_WORKER_LAG,
                context={"lag_minutes": snapshot.worker_lag_minutes},
            )
        )

    # -- token expiry ----------------------------------------------------
    for expiry in snapshot.token_expiries:
        severity = expiry.severity
        if severity is None:
            continue
        alerts.append(_token_alert(expiry, severity))

    # -- settlement drift ------------------------------------------------
    if snapshot.open_drift_exceptions > 0:
        title, message = texts.settlement_drift_text(
            count=snapshot.open_drift_exceptions,
            total_irr=snapshot.drift_total_irr,
        )
        alerts.append(
            NewAlert(
                rule=AlertRule.SETTLEMENT_DRIFT,
                severity=AlertSeverity.CRITICAL,
                title_fa=title,
                message_fa=message,
                dedup_key="settlement_drift",
                runbook_ref=texts.RUNBOOK_SETTLEMENT_DRIFT,
                context={
                    "open_exceptions": snapshot.open_drift_exceptions,
                    "total_irr": snapshot.drift_total_irr,
                },
            )
        )

    # -- failed webhooks ---------------------------------------------------
    if snapshot.failed_webhook_attempts_24h >= thresholds.webhook_failures_24h:
        title, message = texts.webhook_failed_text(count=snapshot.failed_webhook_attempts_24h)
        alerts.append(
            NewAlert(
                rule=AlertRule.WEBHOOK_FAILED,
                severity=AlertSeverity.WARNING,
                title_fa=title,
                message_fa=message,
                dedup_key="webhook_failed",
                runbook_ref=texts.RUNBOOK_WEBHOOK_FAILED,
                context={"failed_24h": snapshot.failed_webhook_attempts_24h},
            )
        )

    # -- backups -----------------------------------------------------------
    for backup in snapshot.backups:
        if backup.status is BackupStatusValue.FAILED:
            title, message = texts.backup_failed_text(database=backup.database, error=backup.error)
            alerts.append(
                NewAlert(
                    rule=AlertRule.BACKUP_FAILED,
                    severity=AlertSeverity.CRITICAL,
                    title_fa=title,
                    message_fa=message,
                    dedup_key=f"backup_failed:{backup.database}",
                    runbook_ref=texts.RUNBOOK_BACKUP_FAILED,
                    context={"database": backup.database},
                )
            )
        elif backup.status is BackupStatusValue.STALE:
            title, message = texts.backup_stale_text(database=backup.database, hours=thresholds.backup_max_age_hours)
            alerts.append(
                NewAlert(
                    rule=AlertRule.BACKUP_FAILED,
                    severity=AlertSeverity.WARNING,
                    title_fa=title,
                    message_fa=message,
                    dedup_key=f"backup_failed:{backup.database}",
                    runbook_ref=texts.RUNBOOK_BACKUP_FAILED,
                    context={"database": backup.database},
                )
            )
        # UNKNOWN never alerts: before US-703 lands there is no backup
        # source at all, and an alert nobody can act on is noise.

    # -- 5xx rate ----------------------------------------------------------
    rate = snapshot.error_rate_5xx
    if (
        rate is not None
        and snapshot.requests_total >= thresholds.error_rate_min_requests
        and rate >= thresholds.error_rate_5xx
    ):
        # Persian decimal separator is «٫», not «.» — transliterate the
        # digits first, then swap the separator.
        percent = to_persian_digits(f"{rate * 100:.1f}").replace(".", "٫") + "٪"
        title, message = texts.error_rate_text(rate_percent=percent, total=snapshot.requests_total)
        alerts.append(
            NewAlert(
                rule=AlertRule.ERROR_RATE_5XX,
                severity=AlertSeverity.CRITICAL,
                title_fa=title,
                message_fa=message,
                dedup_key="error_rate_5xx",
                runbook_ref=texts.RUNBOOK_ERROR_RATE,
                context={
                    "rate": rate,
                    "total": snapshot.requests_total,
                    "server_errors": snapshot.requests_5xx,
                },
            )
        )

    return alerts
