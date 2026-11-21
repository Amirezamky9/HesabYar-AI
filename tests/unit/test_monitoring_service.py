"""Unit tests for MonitoringService (US-704).

In-memory fakes only — no database, no HTTP, no clock beyond the
frozen ``NOW``. ``FakeMonitoringRepository`` is also reused by the
REST interface tests (``tests/interfaces/test_rest_monitoring.py``).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field

import pytest

from hesabyar.application.commands.monitoring import MonitoringService
from hesabyar.domain.monitoring.errors import (
    MonitoringAlertAlreadyOpenError,
    MonitoringNotFoundError,
    MonitoringValidationError,
)
from hesabyar.domain.monitoring.metrics import RequestMetrics
from hesabyar.domain.monitoring.models import (
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
    MonitoringThresholds,
    ProcessHealth,
    ProcessStatus,
    TokenKind,
    TokenRecord,
)
from hesabyar.domain.monitoring.ports import ApiKeyExpiry
from hesabyar.domain.reminders.ports import EmailSendResult
from hesabyar.domain.sms.errors import SmsProviderUnreachableError
from hesabyar.domain.sms.models import (
    SmsDeliveryStatus,
    SmsProvider,
    SmsProviderStatus,
)
from hesabyar.domain.sms.ports import SmsSendResult
from tests.unit.test_sms_services import (
    FakeSmsClient,
    FakeSmsClientFactory,
    FakeSmsRepository,
)

NOW = datetime.datetime(2026, 11, 21, 13, 0, tzinfo=datetime.UTC)
INSTALLATION_ID = "00000000-0000-0000-0000-000000000004"


class FakeMonitoringRepository:
    """In-memory monitoring repository (dedup semantics mirror the SQL one)."""

    def __init__(self) -> None:
        self.alerts: dict[str, Alert] = {}
        self.channels: dict[str, AlertChannel] = {}
        self.token_records: dict[str, TokenRecord] = {}
        self.api_keys: list[ApiKeyExpiry] = []
        # Metric reads, configurable per test.
        self.inbox_counts: dict[str, int] = {"received": 2, "processing": 1}
        self.outbox_pending = 3
        self.stale: tuple[int, datetime.datetime | None] = (0, None)
        self.failed_webhooks = 0
        self.drift: tuple[int, int] = (0, 0)
        self.last_sweep: datetime.datetime | None = NOW - datetime.timedelta(minutes=2)
        self.queue_depths: dict[str, int] | None = {"events": 1, "scheduler": 0}
        self.raise_on_save: bool = False

    # -- alerts -----------------------------------------------------

    def save_alert(self, alert: Alert) -> Alert:
        if self.raise_on_save:
            raise MonitoringAlertAlreadyOpenError(alert.dedup_key)
        for existing in self.alerts.values():
            if existing.dedup_key == alert.dedup_key and existing.status in FIRING_STATUSES:
                raise MonitoringAlertAlreadyOpenError(alert.dedup_key)
        self.alerts[alert.id] = alert
        return alert

    def get_alert(self, alert_id: str) -> Alert | None:
        return self.alerts.get(alert_id)

    def list_alerts(
        self,
        *,
        status: AlertStatus | None = None,
        rule: AlertRule | None = None,
        limit: int = 50,
    ) -> list[Alert]:
        found = [
            alert
            for alert in self.alerts.values()
            if (status is None or alert.status is status) and (rule is None or alert.rule is rule)
        ]
        found.sort(key=lambda a: a.fired_at or NOW, reverse=True)
        return found[:limit]

    def find_open_alert(self, dedup_key: str) -> Alert | None:
        for alert in self.alerts.values():
            if alert.dedup_key == dedup_key and alert.status in FIRING_STATUSES:
                return alert
        return None

    def update_alert(self, alert: Alert) -> Alert:
        self.alerts[alert.id] = alert
        return alert

    # -- channels ---------------------------------------------------

    def list_channels(self) -> list[AlertChannel]:
        return list(self.channels.values())

    def get_channel(self, channel_id: str) -> AlertChannel | None:
        return self.channels.get(channel_id)

    def save_channel(self, channel: AlertChannel) -> AlertChannel:
        self.channels[channel.id] = channel
        return channel

    def delete_channel(self, channel_id: str) -> bool:
        return self.channels.pop(channel_id, None) is not None

    # -- token records ----------------------------------------------

    def list_token_records(self) -> list[TokenRecord]:
        return list(self.token_records.values())

    def get_token_record(self, record_id: str) -> TokenRecord | None:
        return self.token_records.get(record_id)

    def save_token_record(self, record: TokenRecord) -> TokenRecord:
        self.token_records[record.id] = record
        return record

    def delete_token_record(self, record_id: str) -> bool:
        return self.token_records.pop(record_id, None) is not None

    # -- metric reads -----------------------------------------------

    def inbox_counts_by_status(self) -> dict[str, int]:
        return dict(self.inbox_counts)

    def outbox_pending_count(self) -> int:
        return self.outbox_pending

    def stale_inbox(self, older_than: datetime.timedelta) -> tuple[int, datetime.datetime | None]:
        return self.stale

    def failed_webhook_attempts_since(self, since: datetime.datetime) -> int:
        return self.failed_webhooks

    def open_drift_exceptions(self) -> tuple[int, int]:
        return self.drift

    def api_keys_with_expiry(self) -> list[ApiKeyExpiry]:
        return list(self.api_keys)

    def latest_sweep_run_at(self) -> datetime.datetime | None:
        return self.last_sweep

    def procrastinate_queue_depths(self) -> dict[str, int] | None:
        return dict(self.queue_depths) if self.queue_depths is not None else None


@dataclass
class FakeHealthChecks:
    """Canned DB + Tryton probes."""

    database: ProcessStatus = ProcessStatus.OK
    tryton: ProcessStatus = ProcessStatus.OK

    def check_database(self) -> ProcessHealth:
        return ProcessHealth(name="database", status=self.database, checked_at=NOW)

    def check_tryton(self) -> ProcessHealth:
        return ProcessHealth(name="tryton", status=self.tryton, checked_at=NOW)


@dataclass
class FakeMcpProber:
    """Canned MCP /health probe."""

    status: ProcessStatus = ProcessStatus.OK

    def probe(self) -> ProcessHealth:
        return ProcessHealth(name="mcp", status=self.status, checked_at=NOW)


@dataclass
class FakeBackupSource:
    """Canned backup statuses (the US-703 contract, faked)."""

    statuses_list: list[BackupStatus] = field(default_factory=list)

    def statuses(self, *, now: datetime.datetime) -> list[BackupStatus]:
        return list(self.statuses_list)


@dataclass
class FakeWebhookSender:
    """Records webhook sends; returns a canned result."""

    sends: list[dict[str, object]] = field(default_factory=list)
    status: str = "sent"

    def send(self, *, channel: AlertChannel, alert: Alert) -> ChannelSendResult:
        self.sends.append({"channel_id": channel.id, "alert_id": alert.id})
        return ChannelSendResult(channel_id=channel.id, kind=channel.kind, status=self.status)


@dataclass
class FakeEmailSender:
    """Records email sends; optionally raises."""

    sends: list[dict[str, str]] = field(default_factory=list)
    error: Exception | None = None

    def send(self, *, to: str, subject: str, body: str) -> EmailSendResult:
        if self.error is not None:
            raise self.error
        self.sends.append({"to": to, "subject": subject, "body": body})
        return EmailSendResult(provider_message_id="mail-1")


def _simulator_provider(*, enabled: bool = True) -> SmsProvider:
    return SmsProvider(
        id="prov-sim",
        installation_id=INSTALLATION_ID,
        provider_key="simulator",
        display_name_fa="شبیه‌ساز",
        provider_kind="simulator",
        is_builtin=True,
        enabled=enabled,
        is_default=True,
        status=SmsProviderStatus.ACTIVE if enabled else SmsProviderStatus.DISABLED,
        has_credentials=False,
    )


def _service(
    repo: FakeMonitoringRepository,
    *,
    sms_repo: FakeSmsRepository | None = None,
    client_factory: FakeSmsClientFactory | None = None,
    email: FakeEmailSender | None = None,
    webhook_sender: FakeWebhookSender | None = None,
    backup_source: FakeBackupSource | None = None,
    health_checks: FakeHealthChecks | None = None,
    mcp_prober: FakeMcpProber | None = None,
    request_metrics: RequestMetrics | None = None,
) -> MonitoringService:
    return MonitoringService(
        repo,
        thresholds=MonitoringThresholds(),
        sms_repository=sms_repo,
        client_factory=client_factory,
        vault=None,
        email=email,
        webhook_sender=webhook_sender,
        backup_source=backup_source or FakeBackupSource(),
        mcp_prober=mcp_prober if mcp_prober is not None else FakeMcpProber(),
        health_checks=health_checks if health_checks is not None else FakeHealthChecks(),
        request_metrics=request_metrics,
    )


def _channel(
    *,
    kind: AlertChannelKind = AlertChannelKind.WEBHOOK,
    target: str = "https://hooks.example.com/alerts",
    enabled: bool = True,
    min_severity: AlertSeverity = AlertSeverity.WARNING,
    provider_key: str | None = None,
    installation_id: str | None = None,
) -> AlertChannel:
    return AlertChannel(
        id="ch-1",
        kind=kind,
        name="کانال آزمون",
        target=target,
        enabled=enabled,
        min_severity=min_severity,
        provider_key=provider_key,
        installation_id=installation_id,
    )


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------


def test_overview_processes_and_queues() -> None:
    repo = FakeMonitoringRepository()
    service = _service(repo)
    snapshot = service.overview(now=NOW)
    assert snapshot.generated_at == NOW
    by_name = {process.name: process for process in snapshot.processes}
    assert set(by_name) == {"api", "database", "tryton", "worker", "mcp"}
    assert by_name["api"].status is ProcessStatus.OK
    assert by_name["worker"].status is ProcessStatus.OK
    assert snapshot.queue_depths.inbox_received == 2
    assert snapshot.queue_depths.inbox_processing == 1
    assert snapshot.queue_depths.outbox_pending == 3
    assert snapshot.queue_depths.procrastinate_todo == {"events": 1, "scheduler": 0}
    assert snapshot.worker_lag_minutes == 2
    # No request metrics in this service: the 5xx rate is honestly None.
    assert snapshot.error_rate_5xx is None
    assert snapshot.requests_total == 0


def test_overview_worker_error_when_lagging() -> None:
    repo = FakeMonitoringRepository()
    repo.last_sweep = NOW - datetime.timedelta(minutes=25)
    snapshot = _service(repo).overview(now=NOW)
    worker = next(p for p in snapshot.processes if p.name == "worker")
    assert worker.status is ProcessStatus.ERROR
    assert snapshot.worker_lag_minutes == 25


def test_overview_worker_unknown_when_never_ran() -> None:
    repo = FakeMonitoringRepository()
    repo.last_sweep = None
    snapshot = _service(repo).overview(now=NOW)
    worker = next(p for p in snapshot.processes if p.name == "worker")
    assert worker.status is ProcessStatus.UNKNOWN
    assert snapshot.worker_lag_minutes is None


def test_overview_unknown_probes_without_dependencies() -> None:
    repo = FakeMonitoringRepository()
    service = MonitoringService(repo, thresholds=MonitoringThresholds(), health_checks=None, mcp_prober=None)
    snapshot = service.overview(now=NOW)
    by_name = {process.name: process for process in snapshot.processes}
    assert by_name["database"].status is ProcessStatus.UNKNOWN
    assert by_name["tryton"].status is ProcessStatus.UNKNOWN
    assert by_name["mcp"].status is ProcessStatus.UNKNOWN


def test_overview_5xx_rate_from_metrics() -> None:
    repo = FakeMonitoringRepository()
    metrics = RequestMetrics()
    for _ in range(90):
        metrics.record(200)
    for _ in range(10):
        metrics.record(500)
    snapshot = _service(repo, request_metrics=metrics).overview(now=NOW)
    assert snapshot.requests_total == 100
    assert snapshot.requests_5xx == 10
    assert snapshot.error_rate_5xx == 0.1


def test_overview_token_expiries_merged_and_sorted() -> None:
    repo = FakeMonitoringRepository()
    repo.api_keys = [
        ApiKeyExpiry(
            key_id="key-1",
            key_prefix="hya_live_ab12",
            name="کلید انبار",
            expires_at=NOW + datetime.timedelta(days=20),
        ),
        ApiKeyExpiry(
            key_id="key-2",
            key_prefix="hya_live_cd34",
            name="کلید فروشگاه",
            expires_at=NOW + datetime.timedelta(days=2),
        ),
    ]
    repo.token_records["rec-fintech"] = TokenRecord(
        id="rec-fintech",
        kind=TokenKind.FINTECH,
        label="توکن فینوتک",
        expires_at=NOW + datetime.timedelta(days=10),
    )
    repo.token_records["rec-disabled"] = TokenRecord(
        id="rec-disabled",
        kind=TokenKind.OTHER,
        label="توکن غیرفعال",
        expires_at=NOW + datetime.timedelta(days=1),
        enabled=False,
    )
    snapshot = _service(repo).overview(now=NOW)
    labels = [expiry.label for expiry in snapshot.token_expiries]
    assert labels == [
        "کلید فروشگاه (hya_live_cd34)",  # 2 days, nearest first
        "توکن فینوتک",  # 10 days
        "کلید انبار (hya_live_ab12)",  # 20 days
    ]
    assert "توکن غیرفعال" not in labels  # disabled records are skipped
    severities = [expiry.severity for expiry in snapshot.token_expiries]
    assert severities == [
        AlertSeverity.CRITICAL,  # 2 days <= critical window
        AlertSeverity.WARNING,  # 10 days <= warning window
        None,  # 20 days, outside the window
    ]
    fintech = snapshot.token_expiries[1]
    assert fintech.runbook_ref is not None and "چرخش توکن فینوتک" in fintech.runbook_ref


def test_overview_backups_from_source() -> None:
    repo = FakeMonitoringRepository()
    source = FakeBackupSource(
        statuses_list=[
            BackupStatus(
                database="hesabyar",
                status=BackupStatusValue.OK,
                last_success_at=NOW - datetime.timedelta(hours=5),
                source="us703",
            ),
            BackupStatus(database="tryton", status=BackupStatusValue.UNKNOWN, source="none"),
        ]
    )
    snapshot = _service(repo, backup_source=source).overview(now=NOW)
    assert [b.database for b in snapshot.backups] == ["hesabyar", "tryton"]
    assert snapshot.backups[0].status is BackupStatusValue.OK
    assert snapshot.backups[1].status is BackupStatusValue.UNKNOWN


# ---------------------------------------------------------------------------
# Evaluation + dedup
# ---------------------------------------------------------------------------


def test_evaluate_fires_and_dedups() -> None:
    repo = FakeMonitoringRepository()
    repo.stale = (3, NOW - datetime.timedelta(hours=30))
    service = _service(repo)
    first = service.evaluate(now=NOW)
    assert len(first.fired) == 1
    assert first.fired[0].rule is AlertRule.INBOX_STALE
    assert first.fired[0].status is AlertStatus.OPEN
    assert first.already_open == 0
    second = service.evaluate(now=NOW)
    assert second.fired == []
    assert second.already_open == 1
    assert len(repo.alerts) == 1


def test_evaluate_dedup_race_counts_as_already_open() -> None:
    repo = FakeMonitoringRepository()
    repo.stale = (1, NOW - datetime.timedelta(hours=25))
    repo.raise_on_save = True
    report = _service(repo).evaluate(now=NOW)
    assert report.fired == []
    assert report.already_open == 1


def test_evaluate_resolved_alert_can_fire_again() -> None:
    repo = FakeMonitoringRepository()
    repo.stale = (1, NOW - datetime.timedelta(hours=25))
    service = _service(repo)
    first = service.evaluate(now=NOW)
    service.resolve_alert(first.fired[0].id, actor="op-1", now=NOW)
    second = service.evaluate(now=NOW)
    assert len(second.fired) == 1
    assert second.already_open == 0


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def _fire_one(repo: FakeMonitoringRepository, service: MonitoringService) -> Alert:
    repo.stale = (1, NOW - datetime.timedelta(hours=25))
    report = service.evaluate(now=NOW)
    assert len(report.fired) == 1
    return report.fired[0]


def test_dispatch_webhook_sent_and_recorded() -> None:
    repo = FakeMonitoringRepository()
    sender = FakeWebhookSender()
    repo.channels["ch-1"] = _channel()
    service = _service(repo, webhook_sender=sender)
    _fire_one(repo, service)
    assert len(sender.sends) == 1
    assert sender.sends[0]["channel_id"] == "ch-1"


def test_dispatch_skips_disabled_channel_and_severity_gate() -> None:
    repo = FakeMonitoringRepository()
    sender = FakeWebhookSender()
    repo.channels["ch-off"] = _channel(enabled=False)
    repo.channels["ch-crit"] = AlertChannel(
        id="ch-crit",
        kind=AlertChannelKind.WEBHOOK,
        name="بحرانی فقط",
        target="https://hooks.example.com/crit",
        min_severity=AlertSeverity.CRITICAL,
    )
    service = _service(repo, webhook_sender=sender)
    # The stale-inbox alert IS critical, so the critical-only channel
    # receives it; the disabled channel never does.
    _fire_one(repo, service)
    assert [send["channel_id"] for send in sender.sends] == ["ch-crit"]


def test_dispatch_warning_alert_skipped_by_critical_only_channel() -> None:
    repo = FakeMonitoringRepository()
    sender = FakeWebhookSender()
    repo.channels["ch-crit"] = AlertChannel(
        id="ch-crit",
        kind=AlertChannelKind.WEBHOOK,
        name="بحرانی فقط",
        target="https://hooks.example.com/crit",
        min_severity=AlertSeverity.CRITICAL,
    )
    repo.failed_webhooks = 5  # a WARNING alert
    service = _service(repo, webhook_sender=sender)
    report = service.evaluate(now=NOW)
    assert len(report.fired) == 1
    assert report.fired[0].severity is AlertSeverity.WARNING
    assert sender.sends == []
    assert report.dispatched == []


def test_dispatch_webhook_without_sender_is_skipped() -> None:
    repo = FakeMonitoringRepository()
    repo.channels["ch-1"] = _channel()
    service = _service(repo, webhook_sender=None)
    repo.stale = (1, NOW - datetime.timedelta(hours=25))
    report = service.evaluate(now=NOW)
    assert len(report.fired) == 1
    assert len(report.dispatched) == 1
    assert report.dispatched[0].status == "skipped"
    assert "webhook" in (report.dispatched[0].detail or "").lower()


def test_dispatch_sms_via_simulator() -> None:
    repo = FakeMonitoringRepository()
    sms_repo = FakeSmsRepository()
    sms_repo.providers["prov-sim"] = _simulator_provider()
    client = FakeSmsClient(provider_kind="simulator")
    factory = FakeSmsClientFactory(clients={"simulator": client})
    repo.channels["ch-sms"] = _channel(
        kind=AlertChannelKind.SMS,
        target="09123456789",
        provider_key="simulator",
        installation_id=INSTALLATION_ID,
    )
    service = _service(repo, sms_repo=sms_repo, client_factory=factory)
    alert = _fire_one(repo, service)
    assert len(client.sends) == 1
    assert client.sends[0]["to"] == "09123456789"
    assert alert.title_fa in str(client.sends[0]["message"])
    assert client.closed == 1


def test_dispatch_sms_provider_missing_is_failed() -> None:
    repo = FakeMonitoringRepository()
    sms_repo = FakeSmsRepository()  # no provider seeded
    repo.channels["ch-sms"] = _channel(
        kind=AlertChannelKind.SMS,
        target="09123456789",
        provider_key="simulator",
        installation_id=INSTALLATION_ID,
    )
    service = _service(repo, sms_repo=sms_repo, client_factory=FakeSmsClientFactory())
    # Fire one alert first (the _fire_one pattern): with no trigger
    # state, evaluate() fires nothing and dispatched is empty.
    repo.stale = (1, NOW - datetime.timedelta(hours=25))
    report = service.evaluate(now=NOW)
    assert report.dispatched[0].status == "failed"


def test_dispatch_sms_disabled_provider_is_skipped() -> None:
    repo = FakeMonitoringRepository()
    sms_repo = FakeSmsRepository()
    sms_repo.providers["prov-sim"] = _simulator_provider(enabled=False)
    repo.channels["ch-sms"] = _channel(
        kind=AlertChannelKind.SMS,
        target="09123456789",
        provider_key="simulator",
        installation_id=INSTALLATION_ID,
    )
    service = _service(repo, sms_repo=sms_repo, client_factory=FakeSmsClientFactory())
    # Fire one alert first (the _fire_one pattern): with no trigger
    # state, evaluate() fires nothing and dispatched is empty.
    repo.stale = (1, NOW - datetime.timedelta(hours=25))
    report = service.evaluate(now=NOW)
    assert report.dispatched[0].status == "skipped"


def test_dispatch_sms_live_provider_without_credentials_is_skipped() -> None:
    repo = FakeMonitoringRepository()
    sms_repo = FakeSmsRepository()
    sms_repo.providers["prov-kav"] = SmsProvider(
        id="prov-kav",
        installation_id=INSTALLATION_ID,
        provider_key="kavenegar",
        display_name_fa="کاوه‌نگار",
        provider_kind="kavenegar",
        is_builtin=True,
        enabled=True,
        is_default=False,
        status=SmsProviderStatus.SETUP,
        has_credentials=False,
    )
    repo.channels["ch-sms"] = _channel(
        kind=AlertChannelKind.SMS,
        target="09123456789",
        provider_key="kavenegar",
        installation_id=INSTALLATION_ID,
    )
    service = _service(repo, sms_repo=sms_repo, client_factory=FakeSmsClientFactory())
    # Fire one alert first (the _fire_one pattern): with no trigger
    # state, evaluate() fires nothing and dispatched is empty.
    repo.stale = (1, NOW - datetime.timedelta(hours=25))
    report = service.evaluate(now=NOW)
    assert report.dispatched[0].status == "skipped"
    assert "credentials" in (report.dispatched[0].detail or "")


def test_dispatch_sms_client_error_is_failed_not_raised() -> None:
    repo = FakeMonitoringRepository()
    sms_repo = FakeSmsRepository()
    sms_repo.providers["prov-sim"] = _simulator_provider()
    client = FakeSmsClient(
        provider_kind="simulator",
        error=SmsProviderUnreachableError("simulator", reason="boom"),
    )
    factory = FakeSmsClientFactory(clients={"simulator": client})
    repo.channels["ch-sms"] = _channel(
        kind=AlertChannelKind.SMS,
        target="09123456789",
        provider_key="simulator",
        installation_id=INSTALLATION_ID,
    )
    service = _service(repo, sms_repo=sms_repo, client_factory=factory)
    # Fire one alert first (the _fire_one pattern): with no trigger
    # state, evaluate() fires nothing and dispatched is empty.
    repo.stale = (1, NOW - datetime.timedelta(hours=25))
    report = service.evaluate(now=NOW)
    assert len(report.fired) == 1
    assert report.dispatched[0].status == "failed"


def test_dispatch_sms_failed_delivery_is_failed() -> None:
    repo = FakeMonitoringRepository()
    sms_repo = FakeSmsRepository()
    sms_repo.providers["prov-sim"] = _simulator_provider()
    client = FakeSmsClient(
        provider_kind="simulator",
        result=SmsSendResult(
            provider_message_id="sim-fail-1",
            status=SmsDeliveryStatus.FAILED,
            provider_status_raw="failed",
        ),
    )
    factory = FakeSmsClientFactory(clients={"simulator": client})
    repo.channels["ch-sms"] = _channel(
        kind=AlertChannelKind.SMS,
        target="09123456789",
        provider_key="simulator",
        installation_id=INSTALLATION_ID,
    )
    service = _service(repo, sms_repo=sms_repo, client_factory=factory)
    # Fire one alert first (the _fire_one pattern): with no trigger
    # state, evaluate() fires nothing and dispatched is empty.
    repo.stale = (1, NOW - datetime.timedelta(hours=25))
    report = service.evaluate(now=NOW)
    assert report.dispatched[0].status == "failed"


def test_dispatch_email_without_sender_is_skipped() -> None:
    repo = FakeMonitoringRepository()
    repo.channels["ch-mail"] = _channel(kind=AlertChannelKind.EMAIL, target="ops@example.com")
    service = _service(repo, email=None)
    # Fire one alert first (the _fire_one pattern): with no trigger
    # state, evaluate() fires nothing and dispatched is empty.
    repo.stale = (1, NOW - datetime.timedelta(hours=25))
    report = service.evaluate(now=NOW)
    assert report.dispatched[0].status == "skipped"
    assert "email" in (report.dispatched[0].detail or "").lower()


def test_dispatch_email_with_sender_is_sent() -> None:
    repo = FakeMonitoringRepository()
    email = FakeEmailSender()
    repo.channels["ch-mail"] = _channel(kind=AlertChannelKind.EMAIL, target="ops@example.com")
    service = _service(repo, email=email)
    alert = _fire_one(repo, service)
    assert len(email.sends) == 1
    assert email.sends[0]["to"] == "ops@example.com"
    assert email.sends[0]["subject"] == alert.title_fa
    assert email.sends[0]["body"] == alert.message_fa


def test_dispatch_email_error_is_failed_not_raised() -> None:
    repo = FakeMonitoringRepository()
    email = FakeEmailSender(error=RuntimeError("smtp down"))
    repo.channels["ch-mail"] = _channel(kind=AlertChannelKind.EMAIL, target="ops@example.com")
    service = _service(repo, email=email)
    # Fire one alert first (the _fire_one pattern): with no trigger
    # state, evaluate() fires nothing and dispatched is empty.
    repo.stale = (1, NOW - datetime.timedelta(hours=25))
    report = service.evaluate(now=NOW)
    assert len(report.fired) == 1
    assert report.dispatched[0].status == "failed"


# ---------------------------------------------------------------------------
# Alert lifecycle
# ---------------------------------------------------------------------------


def test_acknowledge_and_resolve() -> None:
    repo = FakeMonitoringRepository()
    service = _service(repo)
    alert = _fire_one(repo, service)
    acked = service.acknowledge_alert(alert.id, actor="op-1", now=NOW)
    assert acked.status is AlertStatus.ACKNOWLEDGED
    assert acked.acknowledged_by == "op-1"
    assert acked.acknowledged_at == NOW
    resolved = service.resolve_alert(alert.id, actor="op-2", now=NOW)
    assert resolved.status is AlertStatus.RESOLVED
    assert resolved.resolved_by == "op-2"


def test_acknowledge_unknown_alert_raises_not_found() -> None:
    with pytest.raises(MonitoringNotFoundError):
        _service(FakeMonitoringRepository()).acknowledge_alert("nope", actor="op-1")


def test_resolve_unknown_alert_raises_not_found() -> None:
    with pytest.raises(MonitoringNotFoundError):
        _service(FakeMonitoringRepository()).resolve_alert("nope", actor="op-1")


def test_list_alerts_filters() -> None:
    repo = FakeMonitoringRepository()
    service = _service(repo)
    alert = _fire_one(repo, service)
    assert len(service.list_alerts()) == 1
    assert len(service.list_alerts(status=AlertStatus.OPEN)) == 1
    assert service.list_alerts(status=AlertStatus.RESOLVED) == []
    assert len(service.list_alerts(rule=AlertRule.INBOX_STALE)) == 1
    assert service.list_alerts(rule=AlertRule.WORKER_LAG) == []
    assert service.list_alerts(limit=0) == []
    assert service.list_alerts()[0].id == alert.id


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------


def test_create_sms_channel_normalizes_persian_digits() -> None:
    service = _service(FakeMonitoringRepository())
    channel = service.create_channel(
        {
            "kind": "sms",
            "name": "پیامک مدیر",
            "target": "۰۹۱۲۳۴۵۶۷۸۹",
            "provider_key": "simulator",
        },
        installation_id=INSTALLATION_ID,
    )
    assert channel.target == "09123456789"
    assert channel.installation_id == INSTALLATION_ID
    assert channel.min_severity is AlertSeverity.WARNING
    assert channel.enabled is True


def test_create_sms_channel_without_installation_fails() -> None:
    service = _service(FakeMonitoringRepository())
    with pytest.raises(MonitoringValidationError) as excinfo:
        service.create_channel(
            {
                "kind": "sms",
                "name": "پیامک مدیر",
                "target": "09123456789",
                "provider_key": "simulator",
            },
            installation_id=None,
        )
    assert "installation_id" in excinfo.value.field_errors


def test_create_channel_validation_errors() -> None:
    service = _service(FakeMonitoringRepository())
    # Bad mobile.
    with pytest.raises(MonitoringValidationError) as excinfo:
        service.create_channel(
            {
                "kind": "sms",
                "name": "x",
                "target": "123",
                "provider_key": "simulator",
            },
            installation_id=INSTALLATION_ID,
        )
    assert "target" in excinfo.value.field_errors
    # http webhook refused (https only).
    with pytest.raises(MonitoringValidationError) as excinfo:
        service.create_channel({"kind": "webhook", "name": "x", "target": "http://hooks.example.com/x"})
    assert "target" in excinfo.value.field_errors
    # Bad email.
    with pytest.raises(MonitoringValidationError) as excinfo:
        service.create_channel({"kind": "email", "name": "x", "target": "not-an-email"})
    assert "target" in excinfo.value.field_errors
    # SMS without provider_key.
    with pytest.raises(MonitoringValidationError) as excinfo:
        service.create_channel(
            {"kind": "sms", "name": "x", "target": "09123456789"},
            installation_id=INSTALLATION_ID,
        )
    assert "provider_key" in excinfo.value.field_errors
    # Unknown kind.
    with pytest.raises(MonitoringValidationError) as excinfo:
        service.create_channel({"kind": "pigeon", "name": "x", "target": "y"})
    assert "kind" in excinfo.value.field_errors


def test_update_and_delete_channel() -> None:
    repo = FakeMonitoringRepository()
    service = _service(repo)
    channel = service.create_channel({"kind": "webhook", "name": "قدیم", "target": "https://hooks.example.com/a"})
    updated = service.update_channel(
        channel.id,
        {"kind": "webhook", "name": "جدید", "target": "https://hooks.example.com/b"},
    )
    assert updated.id == channel.id
    assert updated.name == "جدید"
    assert updated.target == "https://hooks.example.com/b"
    service.delete_channel(channel.id)
    assert service.list_channels() == []
    with pytest.raises(MonitoringNotFoundError):
        service.delete_channel(channel.id)
    with pytest.raises(MonitoringNotFoundError):
        service.update_channel(
            "nope",
            {"kind": "webhook", "name": "x", "target": "https://hooks.example.com/c"},
        )


# ---------------------------------------------------------------------------
# Token records
# ---------------------------------------------------------------------------


def test_create_token_record_accepts_iso_string() -> None:
    service = _service(FakeMonitoringRepository())
    record = service.create_token_record(
        {
            "kind": "fintech",
            "label": "توکن فینوتک",
            "expires_at": "2026-12-01T00:00:00+00:00",
        },
        now=NOW,
    )
    assert record.kind is TokenKind.FINTECH
    assert record.expires_at == datetime.datetime(2026, 12, 1, tzinfo=datetime.UTC)
    assert record.enabled is True


def test_create_token_record_past_expiry_fails() -> None:
    service = _service(FakeMonitoringRepository())
    with pytest.raises(MonitoringValidationError) as excinfo:
        service.create_token_record(
            {
                "kind": "fintech",
                "label": "توکن قدیمی",
                "expires_at": "2026-11-01T00:00:00+00:00",
            },
            now=NOW,
        )
    assert "expires_at" in excinfo.value.field_errors


def test_update_and_delete_token_record() -> None:
    repo = FakeMonitoringRepository()
    service = _service(repo)
    record = service.create_token_record(
        {
            "kind": "other",
            "label": "توکن",
            "expires_at": NOW + datetime.timedelta(days=30),
        },
        now=NOW,
    )
    updated = service.update_token_record(
        record.id,
        {
            "kind": "other",
            "label": "توکن جدید",
            "expires_at": NOW + datetime.timedelta(days=60),
        },
        now=NOW,
    )
    assert updated.id == record.id
    assert updated.label == "توکن جدید"
    service.delete_token_record(record.id)
    assert service.list_token_records() == []
    with pytest.raises(MonitoringNotFoundError):
        service.delete_token_record(record.id)
    with pytest.raises(MonitoringNotFoundError):
        service.update_token_record(
            "nope",
            {
                "kind": "other",
                "label": "x",
                "expires_at": NOW + datetime.timedelta(days=60),
            },
            now=NOW,
        )
