"""Run the event-platform worker (Procrastinate) — US-501.

The worker is a separate process from the API (phased design §6.1):
it polls HesabYar's own inbox/outbox tables with ``FOR UPDATE SKIP
LOCKED`` and runs due events through the §10.3 pipeline.
Procrastinate only triggers the runs — one-shot jobs deferred on
ingest/replay plus a periodic sweep (every minute by default).

Usage (from the repo root)::

    uv run python scripts/run_event_worker.py [--once] [--skip-schema]

- Default: apply the Procrastinate schema (idempotent — safe to run
  on every start), then run the worker until interrupted.
- ``--once``: run a single sweep and exit (ops / smoke test).
- ``--skip-schema``: do not apply the Procrastinate schema (the
  deploy pipeline applied it already).

Settings come from the environment (``HESABYAR_*``); see
``docs/event-platform.md``. The worker commits per job run: a crash
mid-sweep loses at most the in-flight job, and the inbox claim
timeout (10 min) lets another worker reclaim it.
"""

from __future__ import annotations

import argparse
import logging
import sys
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from hesabyar.application.commands.cheque_intake import SiteChequeIntakeHandler
from hesabyar.application.commands.cheques import ChequeCommandService
from hesabyar.application.commands.events import (
    EventIngestService,
    EventPipelineService,
    EventWorker,
    StalenessService,
)
from hesabyar.application.commands.installments_finance import InstallmentFinanceService
from hesabyar.application.commands.integrations import PolicyGateService, StoreService
from hesabyar.application.commands.invoices import InvoiceDraftService
from hesabyar.application.commands.journals import JournalDraftService
from hesabyar.application.commands.monitoring import MonitoringService
from hesabyar.application.commands.packaging import PackagingCommandService
from hesabyar.application.commands.policy_gate import RegistryPolicyGate
from hesabyar.application.commands.recurring import RecurringGenerationService
from hesabyar.application.commands.reminders import ReminderService
from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.application.commands.scheduled_jobs import ScheduledJobsService
from hesabyar.application.commands.settlement import SettlementCommandService
from hesabyar.application.commands.sms import SmsReminderService
from hesabyar.application.commands.store_events import StoreDraftBuilder, StoreMappingResolver
from hesabyar.application.commands.sync import StoreSyncService
from hesabyar.domain.events.ports import JobQueuePort
from hesabyar.domain.monitoring.models import MonitoringThresholds
from hesabyar.infrastructure.accounting.tryton.adapter import TrytonAccountingAdapter
from hesabyar.infrastructure.accounting.tryton.client import TrytonRestClient
from hesabyar.infrastructure.accounting.tryton.fiscal_period_checker import (
    TrytonFiscalPeriodChecker,
)
from hesabyar.infrastructure.auth.credential_vault import FernetCredentialVault
from hesabyar.infrastructure.config import AppSettings
from hesabyar.infrastructure.events.backup_queue import (
    BACKUP_QUEUE,
    BackupWorkerFactory,
    BackupWorkerHandle,
)
from hesabyar.infrastructure.events.cheque_queue import (
    CHEQUE_QUEUE,
    ChequeWorkerFactory,
    ChequeWorkerHandle,
)
from hesabyar.infrastructure.events.defaults import (
    InboxReviewQueueAdapter,
    InProcessOutboxPublisher,
    NoWebhookSecretStore,
)
from hesabyar.infrastructure.events.monitoring_queue import (
    MonitoringWorkerFactory,
    MonitoringWorkerHandle,
)
from hesabyar.infrastructure.events.packaging_queue import (
    PACKAGING_QUEUE,
    PackagingCompany,
    PackagingWorkerFactory,
    PackagingWorkerHandle,
)
from hesabyar.infrastructure.events.reminders_queue import (
    REMINDERS_QUEUE,
    RemindersWorkerFactory,
    RemindersWorkerHandle,
)
from hesabyar.infrastructure.events.procrastinate_queue import (
    EVENTS_QUEUE,
    ProcrastinateJobQueue,
    WorkerFactory,
    WorkerHandle,
    create_connector,
    create_defer_app,
    create_event_app,
)
from hesabyar.infrastructure.events.scheduler_queue import (
    SCHEDULER_QUEUE,
    JobRunRecorder,
    RecorderFactory,
    RecorderHandle,
    SchedulerWorkerFactory,
    SchedulerWorkerHandle,
)
from hesabyar.infrastructure.events.settlement_queue import (
    SETTLEMENT_QUEUE,
    SettlementWorkerFactory,
    SettlementWorkerHandle,
)
from hesabyar.infrastructure.events.sms_queue import (
    SMS_QUEUE,
    SmsWorkerFactory,
    SmsWorkerHandle,
)
from hesabyar.infrastructure.files.local_scan_store import LocalChequeScanStore
from hesabyar.infrastructure.monitoring import HttpxMcpHealthProber, WebhookAlertSender
from hesabyar.infrastructure.operations.pg_tools import PgDumpRestoreRunner
from hesabyar.infrastructure.operations.restore_drill import ScratchEngineFactory
from hesabyar.infrastructure.persistence.postgres.cashbox_repository import (
    SqlAlchemyCashBoxRepository,
)
from hesabyar.infrastructure.persistence.postgres.cheques_repository import (
    SqlAlchemyChequeRepository,
)
from hesabyar.infrastructure.persistence.postgres.event_repositories import (
    SqlAlchemyDecisionAuditLogRepository,
    SqlAlchemyEventIdempotencyStore,
    SqlAlchemyEventInboxRepository,
    SqlAlchemyEventOutboxRepository,
    SqlAlchemyExternalIdMapRepository,
    SqlAlchemyStoreSettingsRepository,
)
from hesabyar.infrastructure.persistence.postgres.installments_repository import (
    SqlAlchemyInstallmentRepository,
)
from hesabyar.infrastructure.persistence.postgres.integrations_repository import (
    SqlAlchemyIntegrationsRepository,
)
from hesabyar.infrastructure.persistence.postgres.models import EngineCompanyModel
from hesabyar.infrastructure.persistence.postgres.monitoring_repository import (
    JobRegistryBackupStatusSource,
    SqlAlchemyMonitoringRepository,
)
from hesabyar.infrastructure.persistence.postgres.packaging_repository import (
    SqlAlchemyPackagingRepository,
)
from hesabyar.infrastructure.persistence.postgres.recurring_repository import (
    SqlAlchemyRecurringRepository,
)
from hesabyar.infrastructure.persistence.postgres.repositories import (
    SqlAlchemyInstallationTenantResolver,
)
from hesabyar.infrastructure.persistence.postgres.reminders_repository import (
    SqlAlchemyReminderRepository,
)
from hesabyar.infrastructure.persistence.postgres.review_queue_repository import (
    SqlAlchemyReviewQueueRepository,
)
from hesabyar.infrastructure.persistence.postgres.scheduler_repository import (
    SqlAlchemySchedulerRepository,
)
from hesabyar.infrastructure.persistence.postgres.settlement_repository import (
    SqlAlchemySettlementAudit,
    SqlAlchemySettlementRepository,
)
from hesabyar.infrastructure.persistence.postgres.sms_repository import (
    SqlAlchemySmsRepository,
)
from hesabyar.infrastructure.sms.factory import DefaultSmsClientFactory
from hesabyar.infrastructure.store_connectors.factory import StoreConnectorFactory

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("hesabyar.event_worker")


def build_worker_factory(settings: AppSettings) -> WorkerFactory:
    """Build a factory producing one worker handle per job run.

    One shared engine (pooled) for the process; each job run gets a
    fresh session and a fresh Tryton client, both closed afterwards.
    """
    engine = create_engine(
        settings.database.sqlalchemy_url,
        pool_size=settings.database.pool_size,
        max_overflow=settings.database.max_overflow,
        pool_timeout=settings.database.pool_timeout_seconds,
    )
    session_factory = sessionmaker(bind=engine)
    tryton = settings.tryton

    def factory() -> WorkerHandle:
        session = session_factory()
        client = TrytonRestClient(
            tryton.base_url,
            tryton.database,
            tryton.api_key,
            timeout=tryton.timeout_seconds,
        )
        adapter = TrytonAccountingAdapter(client)
        inbox = SqlAlchemyEventInboxRepository(session)
        outbox = SqlAlchemyEventOutboxRepository(session)
        external_ids = SqlAlchemyExternalIdMapRepository(session)
        registry = SqlAlchemyIntegrationsRepository(session)
        review_repo = SqlAlchemyReviewQueueRepository(session)
        cheque_commands = ChequeCommandService(
            SqlAlchemyChequeRepository(session),
            adapter,
            gate=PolicyGateService(registry, TrytonFiscalPeriodChecker(adapter)),
            review=ReviewQueueCommandService(review_repo, review_repo),
            scan_store=LocalChequeScanStore(settings.filestore.directory),
        )
        pipeline = EventPipelineService(
            inbox,
            outbox,
            external_ids,
            SqlAlchemyDecisionAuditLogRepository(session),
            SqlAlchemyStoreSettingsRepository(session),
            StoreMappingResolver(registry, external_ids, adapter),
            StoreDraftBuilder(adapter, external_ids),
            RegistryPolicyGate(
                PolicyGateService(registry, TrytonFiscalPeriodChecker(adapter))
            ),
            InboxReviewQueueAdapter(),
            adapter,
            SiteChequeIntakeHandler(cheque_commands),
        )
        worker = EventWorker(pipeline, inbox, outbox, InProcessOutboxPublisher())

        def commit() -> None:
            session.commit()

        def close() -> None:
            try:
                client.close()
            finally:
                session.close()

        return WorkerHandle(worker=worker, commit=commit, close=close)

    return factory


def build_packaging_factory(settings: AppSettings) -> PackagingWorkerFactory:
    """Build a factory producing one packaging handle per supply-check run.

    Shares the same pooled engine pattern as the event factory: each
    job run gets a fresh session and a fresh Tryton client, both
    closed afterwards. Companies in scope are the ones with saved
    packaging settings; each is resolved to its engine company id
    via the ``engine_companies`` mapping (a stale row is skipped
    with a warning, never fatal).
    """
    engine = create_engine(
        settings.database.sqlalchemy_url,
        pool_size=settings.database.pool_size,
        max_overflow=settings.database.max_overflow,
        pool_timeout=settings.database.pool_timeout_seconds,
    )
    session_factory = sessionmaker(bind=engine)
    tryton = settings.tryton

    def factory() -> PackagingWorkerHandle:
        session = session_factory()
        client = TrytonRestClient(
            tryton.base_url,
            tryton.database,
            tryton.api_key,
            timeout=tryton.timeout_seconds,
        )
        adapter = TrytonAccountingAdapter(client)
        packaging_repo = SqlAlchemyPackagingRepository(session)
        review_repo = SqlAlchemyReviewQueueRepository(session)
        commands = PackagingCommandService(
            adapter,
            packaging_repo,
            review_repo,
            ReviewQueueCommandService(review_repo, review_repo),
        )

        def companies() -> list[PackagingCompany]:
            found: list[PackagingCompany] = []
            for tenant_id, company_id in packaging_repo.list_configured_companies():
                row = session.get(EngineCompanyModel, company_id)
                if row is None:
                    logger.warning(
                        "Packaging settings exist for unknown company row %s; skipping.",
                        company_id,
                    )
                    continue
                try:
                    engine_id = int(row.external_company_id)
                except ValueError:
                    logger.warning(
                        "Company row %s has a non-integer engine id %r; skipping.",
                        company_id,
                        row.external_company_id,
                    )
                    continue
                found.append(
                    PackagingCompany(
                        tenant_id=tenant_id, company_id=company_id, engine_id=engine_id
                    )
                )
            return found

        def commit() -> None:
            session.commit()

        def close() -> None:
            try:
                client.close()
            finally:
                session.close()

        return PackagingWorkerHandle(
            commands=commands, companies=companies, commit=commit, close=close
        )

    return factory


def build_settlement_factory(settings: AppSettings) -> SettlementWorkerFactory:
    """Build a factory producing one settlement handle per tick run.

    Shares the same pooled engine pattern as the other factories:
    each job run gets a fresh session, closed afterwards. The tick
    needs no Tryton client — it only moves tranche states and opens
    exceptions in HesabYar's own database.
    """
    engine = create_engine(
        settings.database.sqlalchemy_url,
        pool_size=settings.database.pool_size,
        max_overflow=settings.database.max_overflow,
        pool_timeout=settings.database.pool_timeout_seconds,
    )
    session_factory = sessionmaker(bind=engine)

    def factory() -> SettlementWorkerHandle:
        session = session_factory()
        review_repo = SqlAlchemyReviewQueueRepository(session)
        commands = SettlementCommandService(
            SqlAlchemySettlementRepository(session),
            SqlAlchemyIntegrationsRepository(session),
            SqlAlchemySettlementAudit(session),
            # US-555: the worker tick files its new late_tranche
            # exceptions in the review queue, like the REST tick.
            review=ReviewQueueCommandService(review_repo, review_repo),
            tenant_resolver=SqlAlchemyInstallationTenantResolver(session),
        )

        def commit() -> None:
            session.commit()

        def close() -> None:
            session.close()

        return SettlementWorkerHandle(commands=commands, commit=commit, close=close)

    return factory


def build_cheque_factory(settings: AppSettings) -> ChequeWorkerFactory:
    """Build a factory producing one cheque handle per daily-job run.

    Shares the same pooled engine pattern as the other factories:
    each job run gets a fresh session and a fresh Tryton client, both
    closed afterwards. The daily job needs the engine (expiry moves
    the ``account.check`` record too) and the policy gate + review
    queue (expiry transitions pass the gate; reminders file items).
    """
    engine = create_engine(
        settings.database.sqlalchemy_url,
        pool_size=settings.database.pool_size,
        max_overflow=settings.database.max_overflow,
        pool_timeout=settings.database.pool_timeout_seconds,
    )
    session_factory = sessionmaker(bind=engine)
    tryton = settings.tryton

    def factory() -> ChequeWorkerHandle:
        session = session_factory()
        client = TrytonRestClient(
            tryton.base_url,
            tryton.database,
            tryton.api_key,
            timeout=tryton.timeout_seconds,
        )
        adapter = TrytonAccountingAdapter(client)
        registry = SqlAlchemyIntegrationsRepository(session)
        review_repo = SqlAlchemyReviewQueueRepository(session)
        commands = ChequeCommandService(
            SqlAlchemyChequeRepository(session),
            adapter,
            gate=PolicyGateService(registry, TrytonFiscalPeriodChecker(adapter)),
            review=ReviewQueueCommandService(review_repo, review_repo),
        )

        def commit() -> None:
            session.commit()

        def close() -> None:
            try:
                client.close()
            finally:
                session.close()

        return ChequeWorkerHandle(commands=commands, commit=commit, close=close)

    return factory


def build_sms_factory(settings: AppSettings) -> SmsWorkerFactory:
    """Build a factory producing one SMS handle per daily-job run.

    Shares the same pooled engine pattern as the other factories:
    each job run gets a fresh session, closed afterwards. The daily
    reminder job needs no Tryton client (it only reads installment
    schedules and sends SMS) but it does need the credential vault
    (to decrypt provider API keys) and the review queue (send
    failures file items). A missing key means no vault: live
    providers fail closed per-send, and the simulator still works.
    A malformed key is logged loudly and treated the same way —
    the worker must not die because one queue cannot decrypt.
    """
    engine = create_engine(
        settings.database.sqlalchemy_url,
        pool_size=settings.database.pool_size,
        max_overflow=settings.database.max_overflow,
        pool_timeout=settings.database.pool_timeout_seconds,
    )
    session_factory = sessionmaker(bind=engine)
    key = settings.auth.credentials_encryption_key
    vault: FernetCredentialVault | None = None
    if key is not None:
        try:
            vault = FernetCredentialVault(key)
        except ValueError:
            logger.error(
                "Invalid credential-encryption key: live SMS providers "
                "will fail closed until it is fixed."
            )

    def factory() -> SmsWorkerHandle:
        session = session_factory()
        review_repo = SqlAlchemyReviewQueueRepository(session)
        reminders = SmsReminderService(
            SqlAlchemySmsRepository(session),
            SqlAlchemyInstallmentRepository(session),
            client_factory=DefaultSmsClientFactory(),
            vault=vault,
            review=ReviewQueueCommandService(review_repo, review_repo),
            cheques=SqlAlchemyChequeRepository(session),
        )

        def commit() -> None:
            session.commit()

        def close() -> None:
            session.close()

        return SmsWorkerHandle(reminders=reminders, commit=commit, close=close)

    return factory


def build_reminders_factory(settings: AppSettings) -> RemindersWorkerFactory:
    """Build a factory producing one reminders handle per daily-job run.

    Shares the same pooled engine pattern as the other factories:
    each job run gets a fresh session, closed afterwards. The daily
    reminder job needs no Tryton client (it only reads cheque /
    installment / cash-box schedules and sends reminders) but it
    does need the credential vault (to decrypt SMS provider API
    keys) and the review queue (in-app reminders + send failures
    file items). A missing key means no vault: live SMS providers
    fail closed per-send, and the simulator still works. A
    malformed key is logged loudly and treated the same way.
    No email sender is wired yet (the F3 email provider is not
    built) — email attempts are logged as skipped.
    """
    engine = create_engine(
        settings.database.sqlalchemy_url,
        pool_size=settings.database.pool_size,
        max_overflow=settings.database.max_overflow,
        pool_timeout=settings.database.pool_timeout_seconds,
    )
    session_factory = sessionmaker(bind=engine)
    key = settings.auth.credentials_encryption_key
    vault: FernetCredentialVault | None = None
    if key is not None:
        try:
            vault = FernetCredentialVault(key)
        except ValueError:
            logger.error(
                "Invalid credential-encryption key: live SMS providers "
                "will fail closed until it is fixed."
            )

    def factory() -> RemindersWorkerHandle:
        session = session_factory()
        review_repo = SqlAlchemyReviewQueueRepository(session)
        reminders = ReminderService(
            SqlAlchemyReminderRepository(session),
            SqlAlchemyChequeRepository(session),
            SqlAlchemyInstallmentRepository(session),
            SqlAlchemyCashBoxRepository(session),
            SqlAlchemySmsRepository(session),
            client_factory=DefaultSmsClientFactory(),
            vault=vault,
            review=ReviewQueueCommandService(review_repo, review_repo),
            queue=review_repo,
            email=None,
        )

        def commit() -> None:
            session.commit()

        def close() -> None:
            session.close()

        return RemindersWorkerHandle(reminders=reminders, commit=commit, close=close)

    return factory


def build_scheduler_factory(
    settings: AppSettings, *, job_queue: JobQueuePort
) -> SchedulerWorkerFactory:
    """Build a factory producing one scheduler handle per job run (US-560).

    Shares the same pooled engine pattern as the other factories:
    each job run gets a fresh session and a fresh Tryton client, both
    closed afterwards. The dunning sweep journals late fees through
    the engine (draft by default); the store sync ingests polled
    events through the same ``EventIngestService`` the webhooks use
    (secrets are the F1 no-secrets default — ``ingest_verified``
    never consults them — and the job queue defers the one-shot
    process-event jobs so synced events process within seconds
    instead of waiting for the next sweep). The recurring generator
    (US-563) drafts due occurrences through the engine and
    auto-posts only through the registry policy gate (a missing or
    malformed credential key does not affect it — it needs no
    vault). A missing or malformed
    credential key means no vault: the connector factory is None
    and the store sync skips every store with a recorded reason
    instead of failing.
    """
    engine = create_engine(
        settings.database.sqlalchemy_url,
        pool_size=settings.database.pool_size,
        max_overflow=settings.database.max_overflow,
        pool_timeout=settings.database.pool_timeout_seconds,
    )
    session_factory = sessionmaker(bind=engine)
    tryton = settings.tryton
    key = settings.auth.credentials_encryption_key
    vault: FernetCredentialVault | None = None
    if key is not None:
        try:
            vault = FernetCredentialVault(key)
        except ValueError:
            logger.error(
                "Invalid credential-encryption key: store polling will "
                "skip every store until it is fixed."
            )

    def factory() -> SchedulerWorkerHandle:
        session = session_factory()
        client = TrytonRestClient(
            tryton.base_url,
            tryton.database,
            tryton.api_key,
            timeout=tryton.timeout_seconds,
        )
        adapter = TrytonAccountingAdapter(client)
        registry = SqlAlchemyIntegrationsRepository(session)
        review_repo = SqlAlchemyReviewQueueRepository(session)
        review = ReviewQueueCommandService(review_repo, review_repo)
        scheduler_repo = SqlAlchemySchedulerRepository(session)
        ingest = EventIngestService(
            SqlAlchemyEventInboxRepository(session),
            SqlAlchemyEventIdempotencyStore(session),
            NoWebhookSecretStore(),
            job_queue,
        )
        connector_factory = (
            StoreConnectorFactory(registry, vault) if vault is not None else None
        )
        jobs = ScheduledJobsService(
            finance=InstallmentFinanceService(
                SqlAlchemyInstallmentRepository(session),
                review=review,
                queue=review_repo,
                journals=JournalDraftService(adapter),
                cheques=SqlAlchemyChequeRepository(session),
            ),
            inventory=scheduler_repo,
            sync=StoreSyncService(ingest),
            connector_factory=connector_factory,
            store_service=StoreService(registry, vault),
            staleness=StalenessService(SqlAlchemyEventInboxRepository(session)),
            review=review,
            queue=review_repo,
            tenant_resolver=SqlAlchemyInstallationTenantResolver(session),
            recurring=RecurringGenerationService(
                SqlAlchemyRecurringRepository(session),
                invoices=InvoiceDraftService(adapter),
                journals=JournalDraftService(adapter),
                gate=PolicyGateService(
                    registry, TrytonFiscalPeriodChecker(adapter)
                ),
                review=review,
                queue=review_repo,
            ),
        )

        def commit() -> None:
            session.commit()

        def close() -> None:
            try:
                client.close()
            finally:
                session.close()

        return SchedulerWorkerHandle(jobs=jobs, commit=commit, close=close)

    return factory


def build_backup_factory(settings: AppSettings) -> BackupWorkerFactory:
    """Build a factory producing one backup handle per job run (US-703).

    The handle carries settings + the pg tools; the backup/drill
    services are built fresh on every run (a rotated key or endpoint
    takes effect without a worker restart). The engine factory opens
    a REST client for a SCRATCH Tryton database by name — the same
    trytond serves it, and the restored database already contains
    the REST application key, so the same API key works. The client
    is closed when the drill's check leaves the context.
    """
    backup = settings.backup
    hesabyar_url = backup.hesabyar_database_url or settings.database.url
    dumper = PgDumpRestoreRunner(
        pg_dump_path=backup.pg_dump_path,
        pg_restore_path=backup.pg_restore_path,
        timeout_seconds=backup.command_timeout_seconds,
    )
    tryton = settings.tryton

    @contextmanager
    def engine_factory(database: str) -> Iterator[TrytonAccountingAdapter]:
        client = TrytonRestClient(
            tryton.base_url,
            database,
            tryton.api_key,
            timeout=tryton.timeout_seconds,
        )
        try:
            yield TrytonAccountingAdapter(client)
        finally:
            client.close()

    factory: ScratchEngineFactory = engine_factory

    def backup_factory() -> BackupWorkerHandle:
        return BackupWorkerHandle(
            settings=backup,
            dumper=dumper,
            engine_factory=factory,
            hesabyar_database_url=hesabyar_url,
            tryton_database_url=backup.tryton_database_url,
            commit=lambda: None,
            close=lambda: None,
        )

    return backup_factory


def build_job_run_recorder(settings: AppSettings) -> JobRunRecorder:
    """Build the run recorder (US-560): one fresh session per registry op.

    The recorder's sessions are separate from the job's session, so a
    run record (and its failure review items) commits even when the
    job's own transaction rolls back.
    """
    engine = create_engine(
        settings.database.sqlalchemy_url,
        pool_size=settings.database.pool_size,
        max_overflow=settings.database.max_overflow,
        pool_timeout=settings.database.pool_timeout_seconds,
    )
    session_factory = sessionmaker(bind=engine)

    def recorder_factory() -> RecorderHandle:
        session = session_factory()
        review_repo = SqlAlchemyReviewQueueRepository(session)
        scheduler_repo = SqlAlchemySchedulerRepository(session)

        def commit() -> None:
            session.commit()

        def close() -> None:
            session.close()

        return RecorderHandle(
            registry=scheduler_repo,
            inventory=scheduler_repo,
            review=ReviewQueueCommandService(review_repo, review_repo),
            queue=review_repo,
            commit=commit,
            close=close,
        )

    factory: RecorderFactory = recorder_factory
    return JobRunRecorder(factory)


def build_monitoring_factory(settings: AppSettings) -> MonitoringWorkerFactory:
    """Build a factory producing one monitoring handle per evaluation run (US-704).

    Shares the same pooled engine pattern as the other factories:
    each job run gets a fresh session, closed afterwards. The
    evaluation needs the credential vault (to decrypt SMS provider
    API keys for alert dispatch) and the MCP prober (the worker can
    probe the MCP server's /health); it has no health checks (the
    DB/Tryton probes live in the REST layer) and no request metrics
    (the 5xx counters live in the API process) — see
    ``hesabyar.infrastructure.events.monitoring_queue``. Thresholds
    mirror ``get_monitoring_service`` (the REST wiring); keep both
    mappings in sync. No email sender is wired yet (the F3 email
    provider is not built) — email attempts are logged as skipped.
    """
    engine = create_engine(
        settings.database.sqlalchemy_url,
        pool_size=settings.database.pool_size,
        max_overflow=settings.database.max_overflow,
        pool_timeout=settings.database.pool_timeout_seconds,
    )
    session_factory = sessionmaker(bind=engine)
    key = settings.auth.credentials_encryption_key
    vault: FernetCredentialVault | None = None
    if key is not None:
        try:
            vault = FernetCredentialVault(key)
        except ValueError:
            logger.error(
                "Invalid credential-encryption key: SMS alert dispatch "
                "will fail closed until it is fixed."
            )
    monitoring = settings.monitoring
    thresholds = MonitoringThresholds(
        inbox_stale_hours=settings.events.inbox_stale_hours,
        worker_lag_minutes=monitoring.worker_lag_minutes,
        token_warning_days=monitoring.token_warning_days,
        token_critical_days=monitoring.token_critical_days,
        webhook_failures_24h=monitoring.webhook_failures_24h,
        error_rate_5xx=monitoring.error_rate_5xx,
        error_rate_min_requests=monitoring.error_rate_min_requests,
        backup_max_age_hours=monitoring.backup_max_age_hours,
    )

    def factory() -> MonitoringWorkerHandle:
        session = session_factory()
        service = MonitoringService(
            SqlAlchemyMonitoringRepository(session),
            thresholds=thresholds,
            sms_repository=SqlAlchemySmsRepository(session),
            client_factory=DefaultSmsClientFactory(),
            vault=vault,
            email=None,
            webhook_sender=WebhookAlertSender(),
            backup_source=JobRegistryBackupStatusSource(
                session, max_age_hours=monitoring.backup_max_age_hours
            ),
            mcp_prober=HttpxMcpHealthProber(
                settings.mcp.public_url,
                timeout_seconds=monitoring.mcp_probe_timeout_seconds,
            ),
            health_checks=None,
            request_metrics=None,
        )

        def commit() -> None:
            session.commit()

        def close() -> None:
            session.close()

        return MonitoringWorkerHandle(service=service, commit=commit, close=close)

    return factory


def main(argv: list[str] | None = None) -> int:
    """Run the worker (or one sweep with --once)."""
    parser = argparse.ArgumentParser(description="HesabYar event-platform worker (US-501).")
    parser.add_argument("--once", action="store_true", help="Run a single sweep and exit.")
    parser.add_argument(
        "--skip-schema",
        action="store_true",
        help="Do not apply the Procrastinate schema before starting.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Max events to claim per sweep (default: 50).",
    )
    args = parser.parse_args(argv)

    settings = AppSettings()
    factory = build_worker_factory(settings)

    if args.once:
        handle = factory()
        try:
            report = handle.worker.poll_once(limit=args.limit)
            handle.commit()
        finally:
            handle.close()
        logger.info(
            "Sweep done: claimed=%d draft=%d posted=%d review=%d dead_lettered=%d "
            "retries=%d outbox_published=%d outbox_failed=%d errors=%d",
            report.claimed,
            report.processed_draft,
            report.processed_posted,
            report.needs_review,
            report.dead_lettered,
            report.retries_scheduled,
            report.outbox_published,
            report.outbox_failed,
            len(report.errors),
        )
        for error in report.errors:
            logger.error("Sweep error: %s", error)
        return 0

    connector = create_connector(settings.database.sqlalchemy_url)
    # The defer app lets the scheduled store sync trigger one-shot
    # process-event jobs for the events it ingests (opened below, next
    # to the worker app — an unopened app cannot defer, and the defers
    # would fail soft instead).
    defer_app = create_defer_app(connector)
    job_queue = ProcrastinateJobQueue(defer_app)
    app = create_event_app(
        connector,
        factory,
        sweep_cron=settings.events.worker_sweep_cron,
        packaging_factory=build_packaging_factory(settings),
        supply_check_cron=settings.packaging.supply_check_cron,
        settlement_factory=build_settlement_factory(settings),
        settlement_tick_cron=settings.settlement.tick_cron,
        cheque_factory=build_cheque_factory(settings),
        sms_factory=build_sms_factory(settings),
        reminders_factory=build_reminders_factory(settings),
        scheduler_factory=build_scheduler_factory(settings, job_queue=job_queue),
        dunning_sweep_cron=settings.scheduler.dunning_sweep_cron,
        store_sync_cron=settings.scheduler.store_sync_cron,
        staleness_cron=settings.scheduler.staleness_cron,
        recurring_generation_cron=settings.scheduler.recurring_generation_cron,
        backup_factory=build_backup_factory(settings),
        backup_cron=settings.backup.backup_cron,
        restore_drill_cron=settings.backup.restore_drill_cron,
        monitoring_factory=build_monitoring_factory(settings),
        monitoring_evaluation_cron=settings.monitoring.evaluation_cron,
        recorder=build_job_run_recorder(settings),
    )
    with app.open():
        if not args.skip_schema:
            logger.info("Applying Procrastinate schema (idempotent).")
            app.schema_manager.apply_schema()
        try:
            defer_app.open()
        except Exception:
            logger.warning(
                "Could not open the Procrastinate defer app; synced events "
                "will wait for the next sweep instead of processing at once.",
                exc_info=True,
            )
        logger.info(
            "Starting event worker (queues=%s, sweep=%s, supply_check=%s, settlement_tick=%s).",
            [
                EVENTS_QUEUE,
                PACKAGING_QUEUE,
                SETTLEMENT_QUEUE,
                CHEQUE_QUEUE,
                SMS_QUEUE,
                REMINDERS_QUEUE,
                SCHEDULER_QUEUE,
                BACKUP_QUEUE,
            ],
            settings.events.worker_sweep_cron,
            settings.packaging.supply_check_cron,
            settings.settlement.tick_cron,
        )
        app.run_worker(
            queues=[
                EVENTS_QUEUE,
                PACKAGING_QUEUE,
                SETTLEMENT_QUEUE,
                CHEQUE_QUEUE,
                SMS_QUEUE,
                REMINDERS_QUEUE,
                SCHEDULER_QUEUE,
                BACKUP_QUEUE,
            ]
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
