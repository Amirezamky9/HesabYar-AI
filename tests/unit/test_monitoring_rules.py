"""Unit tests for the monitoring alert rules (US-704).

Pure evaluation of ``MonitoringSnapshot`` -> ``NewAlert``; no I/O,
no database, no clock beyond the frozen ``NOW``.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from hesabyar.domain.monitoring.models import (
    AlertSeverity,
    BackupStatus,
    BackupStatusValue,
    MonitoringSnapshot,
    MonitoringThresholds,
    TokenExpiry,
)
from hesabyar.domain.monitoring.rules import evaluate_rules, expiry_severity

NOW = datetime(2026, 11, 21, 13, 0, tzinfo=UTC)
THRESHOLDS = MonitoringThresholds()


def _snapshot(**overrides: object) -> MonitoringSnapshot:
    base = MonitoringSnapshot(
        generated_at=NOW,
        stale_inbox_count=0,
        worker_last_sweep_at=NOW - timedelta(minutes=2),
        worker_lag_minutes=2,
        failed_webhook_attempts_24h=0,
        open_drift_exceptions=0,
        drift_total_irr=0,
        requests_total=0,
        requests_5xx=0,
        error_rate_5xx=None,
        token_expiries=[],
        backups=[
            BackupStatus(
                database="hesabyar",
                status=BackupStatusValue.OK,
                last_success_at=NOW - timedelta(hours=5),
                last_attempt_at=NOW - timedelta(hours=5),
                source="test",
            ),
            BackupStatus(
                database="tryton",
                status=BackupStatusValue.OK,
                last_success_at=NOW - timedelta(hours=5),
                last_attempt_at=NOW - timedelta(hours=5),
                source="test",
            ),
        ],
    )
    return replace(base, **overrides)


def test_clean_snapshot_fires_nothing() -> None:
    assert evaluate_rules(_snapshot(), THRESHOLDS) == []


def test_stale_inbox_is_critical() -> None:
    drafts = evaluate_rules(_snapshot(stale_inbox_count=3, oldest_unprocessed_age_minutes=1830), THRESHOLDS)
    assert len(drafts) == 1
    (draft,) = drafts
    assert draft.rule == "inbox_stale"
    assert draft.severity == AlertSeverity.CRITICAL
    assert draft.dedup_key == "inbox_stale"
    assert "۳" in draft.message_fa  # Persian digit
    assert "۲۴" in draft.message_fa  # the staleness threshold, in hours
    assert draft.runbook_ref is not None and "صندوق ورودی" in draft.runbook_ref


def test_worker_never_ran_is_warning() -> None:
    drafts = evaluate_rules(_snapshot(worker_last_sweep_at=None, worker_lag_minutes=None), THRESHOLDS)
    assert len(drafts) == 1
    (draft,) = drafts
    assert draft.rule == "worker_lag"
    assert draft.severity == AlertSeverity.WARNING
    assert "هیچ" in draft.message_fa


def test_worker_lag_at_threshold_is_quiet() -> None:
    # The rule fires on lag STRICTLY above the threshold (10 min).
    drafts = evaluate_rules(_snapshot(worker_lag_minutes=10), THRESHOLDS)
    assert drafts == []


def test_worker_lag_above_threshold_is_critical() -> None:
    drafts = evaluate_rules(_snapshot(worker_lag_minutes=11), THRESHOLDS)
    assert len(drafts) == 1
    assert drafts[0].rule == "worker_lag"
    assert drafts[0].severity == AlertSeverity.CRITICAL
    assert "۱۱" in drafts[0].message_fa


def test_expiry_severity_boundaries() -> None:
    assert expiry_severity(15, warning_days=14, critical_days=3) is None
    assert expiry_severity(14, warning_days=14, critical_days=3) is AlertSeverity.WARNING
    assert expiry_severity(4, warning_days=14, critical_days=3) is AlertSeverity.WARNING
    assert expiry_severity(3, warning_days=14, critical_days=3) is AlertSeverity.CRITICAL
    assert expiry_severity(0, warning_days=14, critical_days=3) is AlertSeverity.CRITICAL
    assert expiry_severity(-2, warning_days=14, critical_days=3) is AlertSeverity.CRITICAL


def _token(**overrides: object) -> TokenExpiry:
    base = TokenExpiry(
        source="token_record",
        label="توکن فینوتک",
        kind="fintech",
        expires_at=NOW + timedelta(days=10),
        days_left=10,
        severity=AlertSeverity.WARNING,
        runbook_ref=None,
        record_id="rec-fintech",
    )
    return replace(base, **overrides)


def test_token_outside_window_is_quiet() -> None:
    drafts = evaluate_rules(_snapshot(token_expiries=[_token(severity=None)]), THRESHOLDS)
    assert drafts == []


def test_token_in_warning_window() -> None:
    drafts = evaluate_rules(_snapshot(token_expiries=[_token()]), THRESHOLDS)
    assert len(drafts) == 1
    (draft,) = drafts
    assert draft.rule == "token_expiring"
    assert draft.severity == AlertSeverity.WARNING
    assert draft.dedup_key == "token_expiring:token_record:rec-fintech"
    assert "۱۰" in draft.message_fa
    assert draft.runbook_ref is not None and "چرخش توکن فینوتک" in draft.runbook_ref


def test_token_in_critical_window() -> None:
    drafts = evaluate_rules(
        _snapshot(token_expiries=[_token(days_left=3, severity=AlertSeverity.CRITICAL)]),
        THRESHOLDS,
    )
    assert len(drafts) == 1
    assert drafts[0].severity == AlertSeverity.CRITICAL


def test_expired_token_is_critical_and_says_expired() -> None:
    drafts = evaluate_rules(
        _snapshot(token_expiries=[_token(days_left=-2, severity=AlertSeverity.CRITICAL)]),
        THRESHOLDS,
    )
    assert len(drafts) == 1
    assert drafts[0].severity == AlertSeverity.CRITICAL
    assert "منقضی" in drafts[0].message_fa


def test_api_key_token_uses_api_key_runbook() -> None:
    drafts = evaluate_rules(
        _snapshot(
            token_expiries=[
                _token(
                    source="api_key",
                    label="کلید API انبار",
                    kind="api_key",
                    record_id=None,
                )
            ]
        ),
        THRESHOLDS,
    )
    assert len(drafts) == 1
    assert drafts[0].dedup_key == "token_expiring:api_key:کلید API انبار"
    assert drafts[0].runbook_ref is not None and "چرخش کلید API" in drafts[0].runbook_ref


def test_multiple_tokens_each_fire() -> None:
    drafts = evaluate_rules(
        _snapshot(
            token_expiries=[
                _token(),
                _token(
                    source="api_key",
                    label="کلید API",
                    kind="api_key",
                    days_left=2,
                    severity=AlertSeverity.CRITICAL,
                    record_id=None,
                ),
            ]
        ),
        THRESHOLDS,
    )
    assert len(drafts) == 2
    assert {draft.dedup_key for draft in drafts} == {
        "token_expiring:token_record:rec-fintech",
        "token_expiring:api_key:کلید API",
    }


def test_settlement_drift_is_critical_with_rial_total() -> None:
    drafts = evaluate_rules(_snapshot(open_drift_exceptions=2, drift_total_irr=1_250_000), THRESHOLDS)
    assert len(drafts) == 1
    (draft,) = drafts
    assert draft.rule == "settlement_drift"
    assert draft.severity == AlertSeverity.CRITICAL
    assert "۲" in draft.message_fa
    assert "۱,۲۵۰,۰۰۰" in draft.message_fa  # comma-grouped, Persian digits
    assert "ریال" in draft.message_fa


def test_webhook_failures_below_threshold_quiet() -> None:
    drafts = evaluate_rules(_snapshot(failed_webhook_attempts_24h=4), THRESHOLDS)
    assert drafts == []


def test_webhook_failures_at_threshold_is_warning() -> None:
    drafts = evaluate_rules(_snapshot(failed_webhook_attempts_24h=5), THRESHOLDS)
    assert len(drafts) == 1
    assert drafts[0].rule == "webhook_failed"
    assert drafts[0].severity == AlertSeverity.WARNING
    assert "۵" in drafts[0].message_fa


def _backup(database: str, status: BackupStatusValue, **overrides: object) -> BackupStatus:
    base = BackupStatus(
        database=database,
        status=status,
        last_success_at=NOW - timedelta(hours=5),
        last_attempt_at=NOW - timedelta(hours=5),
        source="test",
    )
    return replace(base, **overrides)


def test_backup_failure_is_critical() -> None:
    drafts = evaluate_rules(
        _snapshot(
            backups=[
                _backup("hesabyar", BackupStatusValue.FAILED, error="disk full"),
                _backup("tryton", BackupStatusValue.OK),
            ]
        ),
        THRESHOLDS,
    )
    assert len(drafts) == 1
    (draft,) = drafts
    assert draft.rule == "backup_failed"
    assert draft.severity == AlertSeverity.CRITICAL
    assert draft.dedup_key == "backup_failed:hesabyar"
    assert "disk full" in draft.message_fa
    assert "حسابیار" in draft.message_fa


def test_backup_stale_is_warning() -> None:
    drafts = evaluate_rules(
        _snapshot(
            backups=[
                _backup(
                    "tryton",
                    BackupStatusValue.STALE,
                    last_success_at=NOW - timedelta(hours=30),
                ),
                _backup("hesabyar", BackupStatusValue.OK),
            ]
        ),
        THRESHOLDS,
    )
    assert len(drafts) == 1
    assert drafts[0].severity == AlertSeverity.WARNING
    assert drafts[0].dedup_key == "backup_failed:tryton"
    assert "۲۶" in drafts[0].message_fa  # the max-age threshold, in hours
    assert "ترایتون" in drafts[0].message_fa


def test_backup_unknown_never_fires() -> None:
    drafts = evaluate_rules(
        _snapshot(
            backups=[
                _backup(
                    "hesabyar",
                    BackupStatusValue.UNKNOWN,
                    last_success_at=None,
                    last_attempt_at=None,
                ),
                _backup(
                    "tryton",
                    BackupStatusValue.UNKNOWN,
                    last_success_at=None,
                    last_attempt_at=None,
                ),
            ]
        ),
        THRESHOLDS,
    )
    assert drafts == []


def test_5xx_rate_unknown_is_quiet() -> None:
    drafts = evaluate_rules(_snapshot(error_rate_5xx=None, requests_total=10_000), THRESHOLDS)
    assert drafts == []


def test_5xx_rate_below_min_requests_is_quiet() -> None:
    drafts = evaluate_rules(_snapshot(error_rate_5xx=0.5, requests_total=99, requests_5xx=50), THRESHOLDS)
    assert drafts == []


def test_5xx_rate_below_threshold_is_quiet() -> None:
    drafts = evaluate_rules(
        _snapshot(error_rate_5xx=0.049, requests_total=1000, requests_5xx=49),
        THRESHOLDS,
    )
    assert drafts == []


def test_5xx_rate_at_threshold_is_critical() -> None:
    drafts = evaluate_rules(
        _snapshot(error_rate_5xx=0.05, requests_total=1000, requests_5xx=50),
        THRESHOLDS,
    )
    assert len(drafts) == 1
    (draft,) = drafts
    assert draft.rule == "error_rate_5xx"
    assert draft.severity == AlertSeverity.CRITICAL
    assert "۵٫۰٪" in draft.message_fa  # Persian decimal separator
    assert "۱۰۰۰" in draft.message_fa


def test_multiple_rules_fire_together_in_stable_order() -> None:
    drafts = evaluate_rules(
        _snapshot(
            stale_inbox_count=1,
            oldest_unprocessed_age_minutes=1500,
            worker_lag_minutes=30,
            failed_webhook_attempts_24h=7,
        ),
        THRESHOLDS,
    )
    assert [draft.rule for draft in drafts] == [
        "inbox_stale",
        "worker_lag",
        "webhook_failed",
    ]
