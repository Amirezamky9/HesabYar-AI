"""Monitoring ports (US-704).

The application service depends only on these protocols; the
SQLAlchemy implementation lives in
``hesabyar.infrastructure.persistence.postgres.monitoring_repository``,
the webhook sender in ``hesabyar.infrastructure.monitoring``.

Two contracts matter for sibling stories:

- :class:`BackupStatusSource` is THE US-703 interface: the backup
  job's runs are read from the US-560 job registry by
  ``JobRegistryBackupStatusSource`` (in
  ``hesabyar.infrastructure.persistence.postgres.monitoring_repository``)
  — the ``backup.databases`` job backs up both databases in one
  run, so its latest run IS the backup status. Until any run is
  recorded, :class:`NoBackupStatusSource`'s UNKNOWN is what the
  registry source itself returns. See docs/monitoring.md
  ("قرارداد وضعیت پشتیبان‌گیری") for the exact fields.
- :class:`HealthChecksPort` is the seam to the existing
  ``HealthService`` (DB + Tryton probes): the monitoring service
  takes the probes as a dependency instead of re-implementing them.

The repository never commits: the caller (router / worker) owns the
transaction.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Protocol

from hesabyar.domain.monitoring.models import (
    BACKUP_DATABASES,
    Alert,
    AlertChannel,
    AlertRule,
    AlertStatus,
    BackupStatus,
    BackupStatusValue,
    ChannelSendResult,
    ProcessHealth,
    TokenRecord,
)


@dataclass(frozen=True)
class ApiKeyExpiry:
    """One API key with an expiry (read live from ``api_keys``)."""

    key_id: str
    key_prefix: str
    name: str
    expires_at: datetime.datetime


class MonitoringRepositoryPort(Protocol):
    """Alerts + channels + token records + cross-cutting metric reads."""

    # -- alerts ---------------------------------------------------------

    def save_alert(self, alert: Alert) -> Alert:
        """Insert a fired alert (raises MonitoringAlertAlreadyOpenError on a dedup race)."""
        ...

    def get_alert(self, alert_id: str) -> Alert | None:
        """Return one alert, or None for an unknown id."""
        ...

    def list_alerts(
        self,
        *,
        status: AlertStatus | None = None,
        rule: AlertRule | None = None,
        limit: int = 50,
    ) -> list[Alert]:
        """Return alerts, newest first."""
        ...

    def find_open_alert(self, dedup_key: str) -> Alert | None:
        """Return the firing (open/acknowledged) alert for a dedup key, if any."""
        ...

    def update_alert(self, alert: Alert) -> Alert:
        """Persist an alert's status/actor changes (ack/resolve)."""
        ...

    # -- channels --------------------------------------------------------

    def list_channels(self) -> list[AlertChannel]:
        """Return every configured channel, oldest first."""
        ...

    def get_channel(self, channel_id: str) -> AlertChannel | None:
        """Return one channel, or None for an unknown id."""
        ...

    def save_channel(self, channel: AlertChannel) -> AlertChannel:
        """Insert or replace a channel."""
        ...

    def delete_channel(self, channel_id: str) -> bool:
        """Delete a channel; False when it did not exist."""
        ...

    # -- token records ---------------------------------------------------

    def list_token_records(self) -> list[TokenRecord]:
        """Return every configured token record, oldest first."""
        ...

    def get_token_record(self, record_id: str) -> TokenRecord | None:
        """Return one token record, or None for an unknown id."""
        ...

    def save_token_record(self, record: TokenRecord) -> TokenRecord:
        """Insert or replace a token record."""
        ...

    def delete_token_record(self, record_id: str) -> bool:
        """Delete a token record; False when it did not exist."""
        ...

    # -- metric reads (operator-level, across installations) -------------

    def inbox_counts_by_status(self) -> dict[str, int]:
        """Count inbox events per status ('received', 'processing', ...)."""
        ...

    def outbox_pending_count(self) -> int:
        """Count outbox events still awaiting delivery."""
        ...

    def stale_inbox(self, older_than: datetime.timedelta) -> tuple[int, datetime.datetime | None]:
        """(count, oldest received_at) of events stuck past the threshold."""
        ...

    def failed_webhook_attempts_since(self, since: datetime.datetime) -> int:
        """Count failed integration attempts (inbox + outbox) since ``since``."""
        ...

    def open_drift_exceptions(self) -> tuple[int, int]:
        """(count, Σ|expected − actual| in Rial) of OPEN drift exceptions.

        Drift = ``amount_mismatch`` / ``fee_mismatch`` exceptions with
        status open/in_review — the tie-out already failed its
        tolerance (settlement C6), so any open row is a live drift.
        """
        ...

    def api_keys_with_expiry(self) -> list[ApiKeyExpiry]:
        """Every non-revoked API key with an expiry set (nearest expiry first).

        The service computes the warning/critical severity per key —
        the panel lists them all so the operator sees the full expiry
        picture, not just the ones already alerting.
        """
        ...

    def latest_sweep_run_at(self) -> datetime.datetime | None:
        """Last recorded ``events.sweep`` run start (the worker heartbeat).

        The sweep runs every minute and the job recorder writes a row
        per run (US-560); the freshest row IS the worker's heartbeat —
        no separate heartbeat table is needed.
        """
        ...

    def procrastinate_queue_depths(self) -> dict[str, int] | None:
        """Pending (``todo``) Procrastinate jobs per queue.

        None when the Procrastinate schema is absent (the worker never
        ran) — unknown, not zero. Reads Procrastinate's own tables in
        HesabYar's database (queue state, not Tryton data).
        """
        ...


class BackupStatusSource(Protocol):
    """THE US-703 contract: latest backup status per database.

    Implemented by ``JobRegistryBackupStatusSource`` (in
    ``hesabyar.infrastructure.persistence.postgres.monitoring_repository``),
    which reads the US-560 job registry: the ``backup.databases``
    job backs up both databases in one run, so its latest run is
    the status for each of ``hesabyar`` and ``tryton``:

    - ``status``: ``ok`` (a successful backup within the expected
      cadence), ``failed`` (the latest attempt failed), ``stale``
      (no success within the cadence), ``unknown`` (no information);
    - ``last_success_at`` / ``last_attempt_at`` (UTC, None when none);
    - ``error``: the latest failure's short text (no credentials,
      no URLs, no internals — it renders in the operator panel).

    The monitoring service calls :meth:`statuses` once per snapshot
    and once per evaluation; implementations must be cheap (a single
    indexed read, no S3 round-trip per call — cache or read the
    recorded status).
    """

    def statuses(self, *, now: datetime.datetime) -> list[BackupStatus]:
        """Latest backup status for every database US-703 backs up."""
        ...


class NoBackupStatusSource:
    """The pre-US-703 source: every database reports UNKNOWN.

    Honest by construction — the panel shows "نامشخص" and the backup
    rule never fires from an absence of information.
    """

    def statuses(self, *, now: datetime.datetime) -> list[BackupStatus]:
        """UNKNOWN for both databases (no backup job exists yet)."""
        return [BackupStatus(database=db, status=BackupStatusValue.UNKNOWN, source="none") for db in BACKUP_DATABASES]


class McpHealthProber(Protocol):
    """Probe the MCP server's own health endpoint."""

    def probe(self) -> ProcessHealth:
        """GET the MCP ``/health`` endpoint; ERROR/UNKNOWN on failure."""
        ...


class HealthChecksPort(Protocol):
    """The existing DB + Tryton probes (``HealthService`` satisfies this)."""

    def check_database(self) -> ProcessHealth:
        """Probe the HesabYar database (SELECT 1)."""
        ...

    def check_tryton(self) -> ProcessHealth:
        """Probe the Tryton adapter (minimal REST read)."""
        ...


class AlertChannelSender(Protocol):
    """Send one alert down one channel (SMS / email / webhook)."""

    def send(self, *, channel: AlertChannel, alert: Alert) -> ChannelSendResult:
        """Dispatch the alert; never raises (failures become 'failed'/'skipped')."""
        ...
