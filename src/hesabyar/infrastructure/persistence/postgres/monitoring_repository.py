"""SQLAlchemy monitoring repository (US-704, Phase 7).

Implements :class:`MonitoringRepositoryPort` over
``monitoring_alerts`` / ``monitoring_alert_channels`` /
``monitoring_token_records`` plus the cross-cutting metric reads
the snapshot needs (inbox/outbox counts, failed attempts, open
settlement drift, expiring API keys, the last sweep run, and the
Procrastinate queue depths). The repository never commits: the
caller (router / worker) owns the transaction.

The Procrastinate read is the only fragile one: the
``procrastinate`` schema is managed by Procrastinate itself and is
absent until the worker first runs, so the query runs inside a
SAVEPOINT (``session.begin_nested()``) — a missing-table error
rolls back only to the savepoint and the method returns None
(unknown) instead of poisoning the caller's transaction.
"""

from __future__ import annotations

import datetime
import logging
import re

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from hesabyar.domain.monitoring.errors import MonitoringAlertAlreadyOpenError
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
    TokenKind,
    TokenRecord,
)
from hesabyar.domain.monitoring.ports import ApiKeyExpiry, MonitoringRepositoryPort
from hesabyar.infrastructure.persistence.postgres.models import (
    ApiKeyModel,
    InboxEventModel,
    IntegrationAttemptModel,
    MonitoringAlertChannelModel,
    MonitoringAlertModel,
    MonitoringTokenRecordModel,
    OutboxEventModel,
    ScheduledJobRunModel,
    SettlementExceptionModel,
)

logger = logging.getLogger(__name__)

#: Drift exception types (settlement C6): an open row means the
#: tie-out difference already passed its tolerance.
_DRIFT_EXCEPTION_TYPES = ("amount_mismatch", "fee_mismatch")

#: Exception statuses that count as "still open" for the drift alert.
_DRIFT_OPEN_STATUSES = ("open", "in_review")

#: The sweep job id (the worker heartbeat — see the port docstring).
_SWEEP_JOB_ID = "events.sweep"

#: The backup job id (US-703): one run backs up BOTH databases.
_BACKUP_JOB_ID = "backup.databases"

#: Max length of a backup failure text shown in the operator panel.
_BACKUP_ERROR_MAX_LEN = 200

#: Inbox statuses that count as "unprocessed" for the staleness read.
_UNPROCESSED_INBOX_STATUSES = ("received", "processing")


def _alert_from_row(row: MonitoringAlertModel) -> Alert:
    return Alert(
        id=row.id,
        rule=AlertRule(row.rule),
        severity=AlertSeverity(row.severity),
        status=AlertStatus(row.status),
        title_fa=row.title_fa,
        message_fa=row.message_fa,
        dedup_key=row.dedup_key,
        runbook_ref=row.runbook_ref,
        context=dict(row.context or {}),
        fired_at=row.fired_at,
        acknowledged_at=row.acknowledged_at,
        acknowledged_by=row.acknowledged_by,
        resolved_at=row.resolved_at,
        resolved_by=row.resolved_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _channel_from_row(row: MonitoringAlertChannelModel) -> AlertChannel:
    return AlertChannel(
        id=row.id,
        kind=AlertChannelKind(row.kind),
        name=row.name,
        target=row.target,
        enabled=row.enabled,
        min_severity=AlertSeverity(row.min_severity),
        provider_key=row.provider_key,
        installation_id=row.installation_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _token_from_row(row: MonitoringTokenRecordModel) -> TokenRecord:
    return TokenRecord(
        id=row.id,
        kind=TokenKind(row.kind),
        label=row.label,
        expires_at=row.expires_at,
        rotation_runbook_ref=row.rotation_runbook_ref,
        enabled=row.enabled,
        notes=row.notes,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlAlchemyMonitoringRepository(MonitoringRepositoryPort):
    """Monitoring store + metric reads over SQLAlchemy (flushes, never commits)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- alerts ---------------------------------------------------------

    def save_alert(self, alert: Alert) -> Alert:
        """Insert a fired alert (dedup race → MonitoringAlertAlreadyOpenError)."""
        row = MonitoringAlertModel(
            id=alert.id,
            rule=alert.rule.value,
            severity=alert.severity.value,
            status=alert.status.value,
            title_fa=alert.title_fa,
            message_fa=alert.message_fa,
            dedup_key=alert.dedup_key,
            runbook_ref=alert.runbook_ref,
            context=dict(alert.context),
            fired_at=alert.fired_at or datetime.datetime.now(datetime.UTC),
            acknowledged_at=alert.acknowledged_at,
            acknowledged_by=alert.acknowledged_by,
            resolved_at=alert.resolved_at,
            resolved_by=alert.resolved_by,
        )
        self._session.add(row)
        try:
            # SAVEPOINT, not a full rollback: a dedup race must not
            # discard the other alerts this evaluation already saved.
            with self._session.begin_nested():
                self._session.flush()
        except IntegrityError as exc:
            # The partial unique index refused a second firing row for
            # this dedup key. Expunge the failed row so a later flush
            # cannot retry the same insert, and report the race.
            self._session.expunge(row)
            raise MonitoringAlertAlreadyOpenError(alert.dedup_key) from exc
        return _alert_from_row(row)

    def get_alert(self, alert_id: str) -> Alert | None:
        row = self._session.get(MonitoringAlertModel, alert_id)
        return _alert_from_row(row) if row is not None else None

    def list_alerts(
        self,
        *,
        status: AlertStatus | None = None,
        rule: AlertRule | None = None,
        limit: int = 50,
    ) -> list[Alert]:
        stmt = select(MonitoringAlertModel).order_by(MonitoringAlertModel.fired_at.desc())
        if status is not None:
            stmt = stmt.where(MonitoringAlertModel.status == status.value)
        if rule is not None:
            stmt = stmt.where(MonitoringAlertModel.rule == rule.value)
        rows = self._session.execute(stmt.limit(limit)).scalars().all()
        return [_alert_from_row(row) for row in rows]

    def find_open_alert(self, dedup_key: str) -> Alert | None:
        firing = [status.value for status in FIRING_STATUSES]
        stmt = (
            select(MonitoringAlertModel)
            .where(MonitoringAlertModel.dedup_key == dedup_key)
            .where(MonitoringAlertModel.status.in_(firing))
            .order_by(MonitoringAlertModel.fired_at.desc())
            .limit(1)
        )
        row = self._session.execute(stmt).scalars().first()
        return _alert_from_row(row) if row is not None else None

    def update_alert(self, alert: Alert) -> Alert:
        row = self._session.get(MonitoringAlertModel, alert.id)
        if row is None:
            raise ValueError(f"Monitoring alert {alert.id!r} does not exist.")
        row.status = alert.status.value
        row.acknowledged_at = alert.acknowledged_at
        row.acknowledged_by = alert.acknowledged_by
        row.resolved_at = alert.resolved_at
        row.resolved_by = alert.resolved_by
        self._session.flush()
        return _alert_from_row(row)

    # -- channels --------------------------------------------------------

    def list_channels(self) -> list[AlertChannel]:
        stmt = select(MonitoringAlertChannelModel).order_by(MonitoringAlertChannelModel.created_at.asc())
        rows = self._session.execute(stmt).scalars().all()
        return [_channel_from_row(row) for row in rows]

    def get_channel(self, channel_id: str) -> AlertChannel | None:
        row = self._session.get(MonitoringAlertChannelModel, channel_id)
        return _channel_from_row(row) if row is not None else None

    def save_channel(self, channel: AlertChannel) -> AlertChannel:
        row = self._session.get(MonitoringAlertChannelModel, channel.id)
        if row is None:
            row = MonitoringAlertChannelModel(
                id=channel.id,
                kind=channel.kind.value,
                name=channel.name,
                target=channel.target,
                enabled=channel.enabled,
                min_severity=channel.min_severity.value,
                provider_key=channel.provider_key,
                installation_id=channel.installation_id,
            )
            self._session.add(row)
        else:
            row.kind = channel.kind.value
            row.name = channel.name
            row.target = channel.target
            row.enabled = channel.enabled
            row.min_severity = channel.min_severity.value
            row.provider_key = channel.provider_key
            row.installation_id = channel.installation_id
        self._session.flush()
        return _channel_from_row(row)

    def delete_channel(self, channel_id: str) -> bool:
        row = self._session.get(MonitoringAlertChannelModel, channel_id)
        if row is None:
            return False
        self._session.delete(row)
        self._session.flush()
        return True

    # -- token records ---------------------------------------------------

    def list_token_records(self) -> list[TokenRecord]:
        stmt = select(MonitoringTokenRecordModel).order_by(MonitoringTokenRecordModel.created_at.asc())
        rows = self._session.execute(stmt).scalars().all()
        return [_token_from_row(row) for row in rows]

    def get_token_record(self, record_id: str) -> TokenRecord | None:
        row = self._session.get(MonitoringTokenRecordModel, record_id)
        return _token_from_row(row) if row is not None else None

    def save_token_record(self, record: TokenRecord) -> TokenRecord:
        row = self._session.get(MonitoringTokenRecordModel, record.id)
        if row is None:
            row = MonitoringTokenRecordModel(
                id=record.id,
                kind=record.kind.value,
                label=record.label,
                expires_at=record.expires_at,
                rotation_runbook_ref=record.rotation_runbook_ref,
                enabled=record.enabled,
                notes=record.notes,
            )
            self._session.add(row)
        else:
            row.kind = record.kind.value
            row.label = record.label
            row.expires_at = record.expires_at
            row.rotation_runbook_ref = record.rotation_runbook_ref
            row.enabled = record.enabled
            row.notes = record.notes
        self._session.flush()
        return _token_from_row(row)

    def delete_token_record(self, record_id: str) -> bool:
        row = self._session.get(MonitoringTokenRecordModel, record_id)
        if row is None:
            return False
        self._session.delete(row)
        self._session.flush()
        return True

    # -- metric reads ----------------------------------------------------

    def inbox_counts_by_status(self) -> dict[str, int]:
        stmt = select(InboxEventModel.status, func.count()).group_by(InboxEventModel.status)
        return {status: count for status, count in self._session.execute(stmt).all()}

    def outbox_pending_count(self) -> int:
        stmt = select(func.count()).where(OutboxEventModel.status == "pending")
        return int(self._session.execute(stmt).scalar() or 0)

    def stale_inbox(self, older_than: datetime.timedelta) -> tuple[int, datetime.datetime | None]:
        cutoff = datetime.datetime.now(datetime.UTC) - older_than
        base = (
            select(InboxEventModel)
            .where(InboxEventModel.status.in_(_UNPROCESSED_INBOX_STATUSES))
            .where(InboxEventModel.received_at < cutoff)
        )
        count = int(self._session.execute(select(func.count()).select_from(base.subquery())).scalar() or 0)
        oldest = self._session.execute(
            select(func.min(InboxEventModel.received_at))
            .where(InboxEventModel.status.in_(_UNPROCESSED_INBOX_STATUSES))
            .where(InboxEventModel.received_at < cutoff)
        ).scalar()
        return count, oldest

    def failed_webhook_attempts_since(self, since: datetime.datetime) -> int:
        stmt = (
            select(func.count())
            .select_from(IntegrationAttemptModel)
            .where(IntegrationAttemptModel.status == "failed")
            .where(IntegrationAttemptModel.started_at >= since)
        )
        return int(self._session.execute(stmt).scalar() or 0)

    def open_drift_exceptions(self) -> tuple[int, int]:
        stmt = (
            select(
                SettlementExceptionModel.expected_amount_irr,
                SettlementExceptionModel.actual_amount_irr,
            )
            .where(SettlementExceptionModel.exception_type.in_(_DRIFT_EXCEPTION_TYPES))
            .where(SettlementExceptionModel.status.in_(_DRIFT_OPEN_STATUSES))
        )
        rows = self._session.execute(stmt).all()
        total = 0
        for expected, actual in rows:
            if expected is None or actual is None:
                continue
            total += abs(expected - actual)
        return len(rows), total

    def api_keys_with_expiry(self) -> list[ApiKeyExpiry]:
        stmt = (
            select(ApiKeyModel)
            .where(ApiKeyModel.expires_at.is_not(None))
            .where(ApiKeyModel.revoked_at.is_(None))
            .order_by(ApiKeyModel.expires_at.asc())
        )
        rows = self._session.execute(stmt).scalars().all()
        return [
            ApiKeyExpiry(
                key_id=row.id,
                key_prefix=row.key_prefix,
                name=row.name,
                expires_at=row.expires_at,
            )
            for row in rows
            if row.expires_at is not None
        ]

    def latest_sweep_run_at(self) -> datetime.datetime | None:
        stmt = select(func.max(ScheduledJobRunModel.started_at)).where(ScheduledJobRunModel.job_id == _SWEEP_JOB_ID)
        return self._session.execute(stmt).scalar()

    def procrastinate_queue_depths(self) -> dict[str, int] | None:
        # Procrastinate manages its own schema (``procrastinate``) —
        # absent until the worker first runs. The SAVEPOINT keeps a
        # missing-table error from poisoning the caller's transaction.
        try:
            with self._session.begin_nested():
                rows = self._session.execute(
                    text(
                        "SELECT queue_name, COUNT(*) FROM "
                        "procrastinate.procrastinate_jobs "
                        "WHERE status = 'todo' GROUP BY queue_name"
                    )
                ).all()
        except SQLAlchemyError as exc:
            logger.debug("Procrastinate queue depths unavailable: %s", exc)
            return None
        return {queue: int(count) for queue, count in rows}


def _as_utc(value: datetime.datetime) -> datetime.datetime:
    """Treat a naive timestamp as UTC (Postgres returns aware ones;
    SQLite returns naive — the tests run on SQLite)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.UTC)
    return value


def _sanitize_backup_error(error: str | None) -> str | None:
    """Make a job-run failure text safe for the operator panel.

    The contract (ports.py) forbids credentials, URLs and internals
    in the rendered error. The backup job already scrubs the DSN
    password from pg_dump stderr (pg_tools), so this strips any
    remaining URL (an S3 endpoint in a client error, a file path
    with a scheme) and truncates to a short panel-safe length.
    """
    if error is None:
        return None
    cleaned = re.sub(r"[a-zA-Z][a-zA-Z0-9+.-]*://\S+", "<url>", error).strip()
    if len(cleaned) > _BACKUP_ERROR_MAX_LEN:
        cleaned = cleaned[: _BACKUP_ERROR_MAX_LEN - 1].rstrip() + "…"
    return cleaned or None


class JobRegistryBackupStatusSource:
    """BackupStatusSource reading the US-560 job registry.

    The US-703 + US-704 integration: the backup job
    (``backup.databases``, US-703) backs up BOTH databases in one
    run, so one status applies to both — the latest run of that job
    IS the backup status. The job recorder writes a row per run
    (US-560), so this is two indexed reads on
    ``scheduled_job_runs``: no S3 round-trip, no new table, no
    backup-specific storage to keep in sync.

    Status mapping (identical for both databases):
    - no run recorded -> UNKNOWN (never fabricated);
    - latest run failed -> FAILED, with the run's error text
      (sanitized — see :func:`_sanitize_backup_error`) and the
      last success when one exists (how old the last GOOD backup
      is);
    - latest success within ``max_age_hours`` -> OK;
    - otherwise -> STALE (no success within the cadence). A skipped
      latest run falls through to the success age; with no success
      at all the status is UNKNOWN, not STALE — staleness is about
      a success going missing, not about a job that never ran.
    """

    def __init__(self, session: Session, *, max_age_hours: int) -> None:
        self._session = session
        self._max_age = datetime.timedelta(hours=max_age_hours)

    def statuses(self, *, now: datetime.datetime) -> list[BackupStatus]:
        """Latest backup status for both databases (see class docstring)."""
        now = _as_utc(now)
        latest = self._latest_run()
        if latest is None:
            return [self._one(db, BackupStatusValue.UNKNOWN) for db in BACKUP_DATABASES]
        last_attempt_at = _as_utc(latest.started_at)
        if latest.outcome == "failed":
            # The last success is still useful here: the operator
            # sees how old the last GOOD backup is, not just that
            # the newest attempt failed.
            success = self._latest_success()
            last_success_at = (
                _as_utc(success.finished_at or success.started_at)
                if success is not None
                else None
            )
            return [
                self._one(
                    db,
                    BackupStatusValue.FAILED,
                    last_success_at=last_success_at,
                    last_attempt_at=last_attempt_at,
                    error=_sanitize_backup_error(latest.error),
                )
                for db in BACKUP_DATABASES
            ]
        success = self._latest_success()
        if success is None:
            return [
                self._one(db, BackupStatusValue.UNKNOWN, last_attempt_at=last_attempt_at)
                for db in BACKUP_DATABASES
            ]
        last_success_at = _as_utc(success.finished_at or success.started_at)
        status = (
            BackupStatusValue.OK
            if now - last_success_at <= self._max_age
            else BackupStatusValue.STALE
        )
        return [
            self._one(
                db,
                status,
                last_success_at=last_success_at,
                last_attempt_at=last_attempt_at,
            )
            for db in BACKUP_DATABASES
        ]

    def _latest_run(self) -> ScheduledJobRunModel | None:
        stmt = (
            select(ScheduledJobRunModel)
            .where(ScheduledJobRunModel.job_id == _BACKUP_JOB_ID)
            .order_by(ScheduledJobRunModel.started_at.desc())
            .limit(1)
        )
        return self._session.execute(stmt).scalars().first()

    def _latest_success(self) -> ScheduledJobRunModel | None:
        stmt = (
            select(ScheduledJobRunModel)
            .where(ScheduledJobRunModel.job_id == _BACKUP_JOB_ID)
            .where(ScheduledJobRunModel.outcome == "success")
            .order_by(ScheduledJobRunModel.started_at.desc())
            .limit(1)
        )
        return self._session.execute(stmt).scalars().first()

    @staticmethod
    def _one(
        database: str,
        status: BackupStatusValue,
        *,
        last_success_at: datetime.datetime | None = None,
        last_attempt_at: datetime.datetime | None = None,
        error: str | None = None,
    ) -> BackupStatus:
        return BackupStatus(
            database=database,
            status=status,
            last_success_at=last_success_at,
            last_attempt_at=last_attempt_at,
            error=error,
            source="job-registry",
        )
