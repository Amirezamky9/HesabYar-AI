"""The jobs US-560/US-563 put on schedule (Phase 5-7 ۵خ W1/W3).

Each method runs one job across ALL installations/stores and returns
a report; the Procrastinate task (``scheduler_queue``) and the REST
trigger (``SchedulerService``) both call these methods, so a
scheduled run and a manual trigger are the same code path. The
on-demand endpoints keep working — the schedule ADDS to them.

- :meth:`ScheduledJobsService.run_dunning_sweep_all` — the ۵ج
  dunning sweep (US-541), previously on-demand only
  (``POST /installments/dunning/run``). One installation's failure
  never blocks the others: it is recorded on the report and filed
  as an ``inbox_dead_letter`` review item for that tenant.
- :meth:`ScheduledJobsService.run_store_sync_all` — the store-sync
  endpoint (US-512/US-533) on a 15-minute cadence, with a persisted
  cursor per store (``integration_cursors``). A store that is not
  configured for polling (no API URL / no DB DSN) is skipped and
  counted, never failed — webhook-only stores are normal. A store
  whose poll fails is recorded + filed, and the next run resumes
  from its saved cursor.
- :meth:`ScheduledJobsService.run_staleness_alert` — the inbox
  staleness alert (US-501), previously a GET endpoint only. Each
  newly-stale event files ONE ``inbox_dead_letter`` review item
  (deduplicated by ``source_event_ref``), so the queue never spams.
- :meth:`ScheduledJobsService.run_recurring_generation_all` — the
  recurring generator (US-563): every due recurring occurrence
  becomes a draft in the engine (auto-post only through the policy
  gate). A thin delegate over
  :class:`RecurringGenerationService`, so the Procrastinate task
  and the REST trigger share one path like the other jobs.

Source-store identity (the duplicate hazard): the scheduled sync
must poll under the SAME ``source_store_id`` the webhooks use, or
the same order would be ingested twice under different idempotency
keys. The identity is resolved per store from the newest inbox
event for the connector; a store that never sent an event polls
under its ``store_key`` (recorded in the run summary).
"""

from __future__ import annotations

import datetime
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from hesabyar.application.commands.cheques import ChequeCommandService
from hesabyar.application.commands.events import EventWorker, StalenessService
from hesabyar.application.commands.installments_finance import InstallmentFinanceService
from hesabyar.application.commands.integrations import StoreService
from hesabyar.application.commands.packaging import PackagingCommandService
from hesabyar.application.commands.recurring import (
    RecurringGenerationReport,
    RecurringGenerationService,
)
from hesabyar.application.commands.reminders import ReminderService
from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.application.commands.scheduler import (
    JobRunContext,
    JobRunner,
    file_job_failure_item,
)
from hesabyar.application.commands.settlement import SettlementCommandService
from hesabyar.application.commands.sms import SmsReminderService
from hesabyar.application.commands.sync import StoreSyncService, SyncReport
from hesabyar.application.errors import ScheduledJobNotRunnableError
from hesabyar.domain.accounting.refs import CompanyRef
from hesabyar.domain.events.errors import StoreNotConfiguredError
from hesabyar.domain.events.pipeline import DEFAULT_STALE_AFTER
from hesabyar.domain.events.ports import CommerceConnectorPort
from hesabyar.domain.events.sync import SyncCursor
from hesabyar.domain.integrations.models import Store
from hesabyar.domain.review_queue.models import (
    ReviewItemStatus,
    ReviewItemType,
    ReviewSeverity,
)
from hesabyar.domain.review_queue.ports import ReviewItemFilters, ReviewQueuePort
from hesabyar.domain.scheduler.ports import SchedulerInventoryPort
from hesabyar.domain.settlement.ports import InstallationTenantResolver

#: Actor recorded on review items filed by the scheduled jobs.
SCHEDULER_ACTOR = "scheduler"

#: The ``integration_cursors`` stream for store polling (the generic
#: changes API returns mixed entity types; the stream is "changes",
#: not one entity kind).
SYNC_STREAM = "changes"

#: Max pages fetched per store per scheduled run (bounds one 15-minute
#: run; the saved cursor resumes the rest on the next run).
SCHEDULED_SYNC_MAX_PAGES = 10


class StoreConnectorFactoryPort(Protocol):
    """Build a store connector for one store + sync source.

    The application layer owns this shape; the infrastructure
    ``StoreConnectorFactory`` satisfies it structurally (the
    application never imports infrastructure).
    """

    def build(
        self, store: Store, *, source_store_id: str, source: str
    ) -> CommerceConnectorPort:
        """Build the connector for one store + sync source (``api`` / ``db``)."""
        ...

    def close(self) -> None:
        """Close every connector this factory built."""
        ...


@dataclass(frozen=True)
class UnitFailure:
    """One unit (installation/store) that failed inside a job run."""

    unit: str
    tenant_id: str | None
    error: str
    review_item_id: str | None = None

    def summary(self) -> dict[str, object]:
        """JSON-serializable form for the run record."""
        return {
            "unit": self.unit,
            "tenant_id": self.tenant_id,
            "error": self.error,
            "review_item_id": self.review_item_id,
        }


@dataclass(frozen=True)
class StoreSkip:
    """One store skipped by the scheduled sync (config gap, not a failure)."""

    store_key: str
    reason: str

    def summary(self) -> dict[str, object]:
        return {"store_key": self.store_key, "reason": self.reason}


@dataclass(frozen=True)
class DunningSweepAllReport:
    """Outcome of one scheduled dunning sweep across all installations."""

    as_of: datetime.date
    installations_scanned: int
    plans_scanned: int
    overdue_legs: int
    items_filed: int
    fees_assessed_irr: int
    journals_created: int
    failures: tuple[UnitFailure, ...] = ()

    def summary(self) -> dict[str, object]:
        """JSON-serializable form for the run record."""
        return {
            "as_of": self.as_of.isoformat(),
            "installations_scanned": self.installations_scanned,
            "plans_scanned": self.plans_scanned,
            "overdue_legs": self.overdue_legs,
            "items_filed": self.items_filed,
            "fees_assessed_irr": self.fees_assessed_irr,
            "journals_created": self.journals_created,
            "failures": [failure.summary() for failure in self.failures],
        }


@dataclass(frozen=True)
class StoreSyncAllReport:
    """Outcome of one scheduled store-sync run across all active stores."""

    stores_scanned: int
    stores_synced: int
    stores_skipped: int
    events_created: int
    events_duplicate: int
    changes_unmapped: int
    pages_fetched: int
    skipped: tuple[StoreSkip, ...] = ()
    failures: tuple[UnitFailure, ...] = ()

    def summary(self) -> dict[str, object]:
        """JSON-serializable form for the run record."""
        return {
            "stores_scanned": self.stores_scanned,
            "stores_synced": self.stores_synced,
            "stores_skipped": self.stores_skipped,
            "events_created": self.events_created,
            "events_duplicate": self.events_duplicate,
            "changes_unmapped": self.changes_unmapped,
            "pages_fetched": self.pages_fetched,
            "skipped": [skip.summary() for skip in self.skipped],
            "failures": [failure.summary() for failure in self.failures],
        }


@dataclass(frozen=True)
class StalenessAlertReport:
    """Outcome of one inbox-staleness alert run."""

    stale_count: int
    items_filed: int
    already_filed: int
    skipped_no_tenant: int

    def summary(self) -> dict[str, object]:
        """JSON-serializable form for the run record."""
        return {
            "stale_count": self.stale_count,
            "items_filed": self.items_filed,
            "already_filed": self.already_filed,
            "skipped_no_tenant": self.skipped_no_tenant,
        }


@dataclass(frozen=True)
class SupplyCheckCompany:
    """One company in scope for the scheduled supply check."""

    tenant_id: str
    company_id: str  # HesabYar engine_companies row id
    engine_id: int  # Tryton company.company id


class ScheduledJobsService:
    """Run the scheduled jobs (dunning, store sync, staleness, recurring)."""

    def __init__(
        self,
        *,
        finance: InstallmentFinanceService,
        inventory: SchedulerInventoryPort,
        sync: StoreSyncService,
        connector_factory: StoreConnectorFactoryPort | None,
        store_service: StoreService | None = None,
        staleness: StalenessService,
        review: ReviewQueueCommandService | None = None,
        queue: ReviewQueuePort | None = None,
        tenant_resolver: InstallationTenantResolver | None = None,
        recurring: RecurringGenerationService | None = None,
        now: datetime.datetime | None = None,
    ) -> None:
        self._finance = finance
        self._inventory = inventory
        self._sync = sync
        self._connector_factory = connector_factory
        self._store_service = store_service
        self._staleness = staleness
        self._review = review
        self._queue = queue
        self._tenant_resolver = tenant_resolver
        self._recurring = recurring
        self._now = now

    @property
    def has_recurring_generation(self) -> bool:
        """True when the recurring generator is wired (US-563).

        :func:`build_job_runners` only registers the recurring runner
        when this is True — otherwise a trigger answers 503
        ``SCHEDULED_JOB_NOT_RUNNABLE`` instead of recording a
        failure for a job this process cannot run.
        """
        return self._recurring is not None

    def _clock(self) -> datetime.datetime:
        if self._now is not None:
            return self._now
        return datetime.datetime.now(datetime.UTC)

    def _file_unit_failure(
        self,
        *,
        job_id: str,
        tenant_id: str,
        unit: str,
        error: str,
        today: datetime.date,
    ) -> str | None:
        """File one inbox_dead_letter item for a failed unit (deduped per day)."""
        if self._review is None or self._queue is None:
            return None
        return file_job_failure_item(
            review=self._review,
            queue=self._queue,
            tenant_id=tenant_id,
            job_id=job_id,
            ref=f"scheduled-job:{job_id}:{unit}:{today.isoformat()}",
            reason=f"Scheduled job {job_id} failed for {unit}: {error}",
            actor=SCHEDULER_ACTOR,
        )

    # -- dunning sweep ---------------------------------------------------------

    def run_dunning_sweep_all(
        self,
        *,
        today: datetime.date | None = None,
        actor: str = SCHEDULER_ACTOR,
    ) -> DunningSweepAllReport:
        """Run the dunning sweep for every installation with plans.

        One installation's failure never blocks the others: it is
        recorded on the report and filed as an ``inbox_dead_letter``
        review item for that installation's tenant.
        """
        day = today or self._clock().date()
        installations_scanned = 0
        plans_scanned = 0
        overdue_legs = 0
        items_filed = 0
        fees_assessed_irr = 0
        journals_created = 0
        failures: list[UnitFailure] = []
        for tenant_id, installation_id in self._inventory.list_installations_with_plans():
            installations_scanned += 1
            try:
                result = self._finance.run_dunning(
                    tenant_id=tenant_id,
                    installation_id=installation_id,
                    today=day,
                    actor=actor,
                )
            except Exception as exc:  # one installation never blocks the rest
                error = str(exc) or exc.__class__.__name__
                item_id = self._file_unit_failure(
                    job_id="scheduler.dunning_sweep",
                    tenant_id=tenant_id,
                    unit=installation_id,
                    error=error,
                    today=day,
                )
                failures.append(
                    UnitFailure(
                        unit=installation_id,
                        tenant_id=tenant_id,
                        error=error,
                        review_item_id=item_id,
                    )
                )
                continue
            plans_scanned += result.plans_scanned
            overdue_legs += result.overdue_legs
            items_filed += len(result.items_filed)
            fees_assessed_irr += result.fees_assessed_irr
            journals_created += len(result.journals_created)
        return DunningSweepAllReport(
            as_of=day,
            installations_scanned=installations_scanned,
            plans_scanned=plans_scanned,
            overdue_legs=overdue_legs,
            items_filed=items_filed,
            fees_assessed_irr=fees_assessed_irr,
            journals_created=journals_created,
            failures=tuple(failures),
        )

    # -- store sync ------------------------------------------------------------

    def run_store_sync_all(
        self,
        *,
        today: datetime.date | None = None,
    ) -> StoreSyncAllReport:
        """Poll every active store's changes (15-minute cadence).

        Each store syncs under its established source-store identity
        (newest inbox event for the connector, else the store key),
        resuming from its saved cursor or, on a fresh poll, from its
        last successful sync time. A store that fails is recorded +
        filed; its saved cursor is left untouched so the next run
        retries from the same point.
        """
        day = today or self._clock().date()
        now = self._clock()
        stores_scanned = 0
        stores_synced = 0
        events_created = 0
        events_duplicate = 0
        changes_unmapped = 0
        pages_fetched = 0
        skipped: list[StoreSkip] = []
        failures: list[UnitFailure] = []
        try:
            for store in self._inventory.list_active_stores():
                stores_scanned += 1
                outcome = self._sync_one_store(store, day=day, now=now)
                if isinstance(outcome, StoreSkip):
                    skipped.append(outcome)
                    continue
                if isinstance(outcome, UnitFailure):
                    failures.append(outcome)
                    continue
                stores_synced += 1
                events_created += outcome.events_created
                events_duplicate += outcome.events_duplicate
                changes_unmapped += outcome.changes_unmapped
                pages_fetched += outcome.pages_fetched
        finally:
            if self._connector_factory is not None:
                self._connector_factory.close()
        return StoreSyncAllReport(
            stores_scanned=stores_scanned,
            stores_synced=stores_synced,
            stores_skipped=len(skipped),
            events_created=events_created,
            events_duplicate=events_duplicate,
            changes_unmapped=changes_unmapped,
            pages_fetched=pages_fetched,
            skipped=tuple(skipped),
            failures=tuple(failures),
        )

    def _sync_one_store(
        self, store: Store, *, day: datetime.date, now: datetime.datetime
    ) -> StoreSkip | UnitFailure | SyncReport:
        """Sync one store: a skip, a failure, or the sync report."""
        store_key = store.store_key
        installation_id = store.installation_id
        if self._connector_factory is None:
            return StoreSkip(
                store_key=store_key,
                reason="No credential vault is configured; polling needs one.",
            )
        if not store.api_base_url and not store.has_store_db_dsn:
            return StoreSkip(
                store_key=store_key,
                reason="Store is not configured for polling (no API URL, no store-DB DSN).",
            )
        tenant_id: str | None = None
        if self._tenant_resolver is not None:
            tenant_id = self._tenant_resolver.tenant_id_for_installation(installation_id)
        if tenant_id is None:
            return StoreSkip(
                store_key=store_key,
                reason="No tenant could be resolved for the store's installation.",
            )
        source_store_id = self._inventory.latest_source_store_id(
            tenant_id, store_key
        ) or store_key
        state = self._inventory.get_sync_cursor(
            tenant_id, store_key, source_store_id, SYNC_STREAM
        )
        cursor = (
            SyncCursor(value=state.cursor_value)
            if state is not None and state.cursor_value
            else None
        )
        updated_since = (
            state.last_synced_at
            if cursor is None and state is not None and state.last_synced_at is not None
            else None
        )
        source = "api" if store.api_base_url else "db"
        try:
            connector = self._connector_factory.build(
                store, source_store_id=source_store_id, source=source
            )
        except StoreNotConfiguredError as exc:
            # A store with an API URL but no stored key is a config
            # gap, not a job failure: skip it and say why.
            return StoreSkip(store_key=store_key, reason=str(exc))
        try:
            report: SyncReport = self._sync.sync(
                connector,
                source_system=store_key,
                source_store_id=source_store_id,
                connector_kind=source,
                tenant_id=tenant_id,
                installation_id=installation_id,
                updated_since=updated_since,
                cursor=cursor,
                max_pages=SCHEDULED_SYNC_MAX_PAGES,
            )
        except Exception as exc:  # the cursor stays: the next run retries here
            error = str(exc) or exc.__class__.__name__
            item_id = self._file_unit_failure(
                job_id="scheduler.store_sync",
                tenant_id=tenant_id,
                unit=store_key,
                error=error,
                today=day,
            )
            return UnitFailure(
                unit=store_key, tenant_id=tenant_id, error=error, review_item_id=item_id
            )
        # The run consumed its input cursor: persist the resume cursor
        # when there is more to fetch, else clear it and bound the
        # next fresh poll by this run's time.
        self._inventory.save_sync_cursor(
            tenant_id,
            store_key,
            source_store_id,
            SYNC_STREAM,
            cursor_value=report.next_cursor if report.has_more else None,
            last_synced_at=now,
        )
        if self._store_service is not None:
            self._store_service.record_successful_poll(installation_id, store_key)
        return report

    # -- inbox staleness ---------------------------------------------------------

    def run_staleness_alert(
        self,
        *,
        stale_after: datetime.timedelta = DEFAULT_STALE_AFTER,
    ) -> StalenessAlertReport:
        """File one review item per newly-stale inbox event.

        Stale = stuck in ``received``/``processing`` longer than
        ``stale_after`` (default 24h). An event that already has an
        open ``inbox_dead_letter`` item is not re-filed. Tenantless
        events (tenant mapping unresolved) cannot be filed — the
        review item needs an owning tenant — so they are counted, not
        silently dropped.
        """
        stale = self._staleness.list_stale(older_than=stale_after)
        items_filed = 0
        already_filed = 0
        skipped_no_tenant = 0
        existing_refs_by_tenant: dict[str, set[str]] = {}
        for event in stale:
            if event.tenant_id is None:
                skipped_no_tenant += 1
                continue
            tenant_id = event.tenant_id
            ref = f"inbox-event:{event.id}:stale"
            if tenant_id not in existing_refs_by_tenant:
                existing_refs_by_tenant[tenant_id] = self._open_dead_letter_refs(tenant_id)
            if ref in existing_refs_by_tenant[tenant_id]:
                already_filed += 1
                continue
            if self._review is None:
                # No review queue wired: counted as already-filed is a
                # lie; count it as filed-nowhere by skipping the file
                # and leaving the event for the next run.
                continue
            self._review.file_item(
                tenant_id=tenant_id,
                item_type=ReviewItemType.INBOX_DEAD_LETTER,
                severity=ReviewSeverity.WARNING,
                reason=(
                    f"Inbox event {event.id} ({event.event_type}) is stuck in "
                    f"{event.status.value} since {event.received_at.isoformat()}."
                ),
                suggested_action=(
                    "Replay the event from the review queue, or investigate "
                    "why the worker is not processing it."
                ),
                channel=event.source_system,
                subtype="stale",
                source_event_ref=ref,
                actor=SCHEDULER_ACTOR,
            )
            existing_refs_by_tenant[tenant_id].add(ref)
            items_filed += 1
        return StalenessAlertReport(
            stale_count=len(stale),
            items_filed=items_filed,
            already_filed=already_filed,
            skipped_no_tenant=skipped_no_tenant,
        )

    def _open_dead_letter_refs(self, tenant_id: str) -> set[str]:
        """Source refs of the tenant's open/in-review dead-letter items."""
        if self._queue is None:
            return set()
        return {
            item.source_event_ref
            for item in self._queue.list_items(
                tenant_id,
                ReviewItemFilters(item_type=ReviewItemType.INBOX_DEAD_LETTER),
            )
            if item.status in (ReviewItemStatus.OPEN, ReviewItemStatus.IN_REVIEW)
            and item.source_event_ref is not None
        }

    # -- recurring generation ----------------------------------------------------

    def run_recurring_generation_all(
        self,
        *,
        today: datetime.date | None = None,
        actor: str = SCHEDULER_ACTOR,
    ) -> RecurringGenerationReport:
        """Generate every due recurring occurrence (US-563).

        A thin delegate over :class:`RecurringGenerationService`,
        so the Procrastinate task and the REST trigger share one
        path like the other jobs. Raises
        :class:`ScheduledJobNotRunnableError` when no generation
        service is wired in this process.
        """
        if self._recurring is None:
            raise ScheduledJobNotRunnableError(
                "Scheduled job 'scheduler.recurring_generation' has no "
                "generation service wired in this process.",
                details={"job_id": "scheduler.recurring_generation"},
            )
        return self._recurring.generate_due(today=today, actor=actor)


# -- runners (the REST trigger + the worker tasks share these) -------------------


class _FunctionRunner:
    """A :class:`JobRunner` built from one callable (keeps the dict small)."""

    def __init__(self, func: Callable[[JobRunContext], dict[str, Any]]) -> None:
        self._func = func

    def run(self, ctx: JobRunContext) -> dict[str, Any]:
        return self._func(ctx)


def build_job_runners(
    *,
    worker: EventWorker | None = None,
    settlement: SettlementCommandService | None = None,
    packaging: PackagingCommandService | None = None,
    packaging_companies: Callable[[], Sequence[SupplyCheckCompany]] | None = None,
    cheques: ChequeCommandService | None = None,
    sms: SmsReminderService | None = None,
    reminders: ReminderService | None = None,
    scheduled_jobs: ScheduledJobsService | None = None,
) -> dict[str, JobRunner]:
    """Build the trigger runners for the jobs this process can run.

    A job with no service wired here has no runner: triggering it
    raises :class:`ScheduledJobNotRunnableError` (503) instead of
    pretending to run. The API process wires all of them; the worker
    process wires the same set through its factories.
    """
    runners: dict[str, JobRunner] = {}
    if worker is not None:

        def _sweep(ctx: JobRunContext) -> dict[str, Any]:
            report = worker.poll_once()
            return {
                "claimed": report.claimed,
                "processed_draft": report.processed_draft,
                "processed_posted": report.processed_posted,
                "needs_review": report.needs_review,
                "dead_lettered": report.dead_lettered,
                "retries_scheduled": report.retries_scheduled,
                "outbox_published": report.outbox_published,
                "outbox_failed": report.outbox_failed,
                "errors": list(report.errors),
            }

        runners["events.sweep"] = _FunctionRunner(_sweep)
    if settlement is not None:

        def _tick(ctx: JobRunContext) -> dict[str, Any]:
            report = settlement.run_daily_tick(ctx.today)
            return {
                "moved_to_due": report.moved_to_due,
                "moved_to_overdue": report.moved_to_overdue,
                "exceptions_created": report.exceptions_created,
                "exceptions_escalated": report.exceptions_escalated,
                "review_items_filed": report.review_items_filed,
            }

        runners["settlement.tick"] = _FunctionRunner(_tick)
    if packaging is not None and packaging_companies is not None:

        def _supply_check(ctx: JobRunContext) -> dict[str, Any]:
            checked = 0
            new_requests = 0
            items_filed = 0
            skipped_open = 0
            errors: list[str] = []
            for company in packaging_companies():
                try:
                    report = packaging.run_supply_check(
                        tenant_id=company.tenant_id,
                        company=CompanyRef(company.engine_id),
                        company_id=company.company_id,
                        actor=SCHEDULER_ACTOR,
                    )
                except Exception as exc:  # one company never blocks the rest
                    errors.append(f"{company.company_id}: {exc}")
                    continue
                checked += report.materials_checked
                new_requests += report.purchase_requests_new
                items_filed += report.items_filed
                skipped_open += report.items_skipped_open
                errors.extend(report.errors)
            return {
                "materials_checked": checked,
                "purchase_requests_new": new_requests,
                "items_filed": items_filed,
                "items_skipped_open": skipped_open,
                "errors": errors,
            }

        runners["packaging.supply_check"] = _FunctionRunner(_supply_check)
    if cheques is not None:

        def _cheque_daily(ctx: JobRunContext) -> dict[str, Any]:
            report = cheques.run_daily_cheque_job(ctx.today)
            return {
                "reminders_sent": report.reminders_sent,
                "expired": report.expired,
                "skipped": report.skipped,
            }

        runners["cheque.daily"] = _FunctionRunner(_cheque_daily)
    if sms is not None:

        def _sms_daily(ctx: JobRunContext) -> dict[str, Any]:
            report = sms.run_daily_reminder_job(today=ctx.today)
            return {
                "installations_scanned": report.installations_scanned,
                "plans_scanned": report.plans_scanned,
                "reminders_due": report.reminders_due,
                "sent": report.sent,
                "failed": report.failed,
                "skipped_no_phone": report.skipped_no_phone,
                "skipped_not_opted_in": report.skipped_not_opted_in,
                "skipped_no_provider": report.skipped_no_provider,
                "skipped_pending_clearance": report.skipped_pending_clearance,
                "skipped_already_sent": report.skipped_already_sent,
                "errors": list(report.errors),
            }

        runners["sms.daily_reminders"] = _FunctionRunner(_sms_daily)
    if reminders is not None:

        def _reminders_daily(ctx: JobRunContext) -> dict[str, Any]:
            report = reminders.run_daily_reminder_job(today=ctx.today)
            return report.summary()

        runners["reminders.daily"] = _FunctionRunner(_reminders_daily)
    if scheduled_jobs is not None:
        runners["scheduler.dunning_sweep"] = _FunctionRunner(
            lambda ctx: scheduled_jobs.run_dunning_sweep_all(
                today=ctx.today, actor=ctx.actor
            ).summary()
        )
        runners["scheduler.store_sync"] = _FunctionRunner(
            lambda ctx: scheduled_jobs.run_store_sync_all(today=ctx.today).summary()
        )
        runners["scheduler.inbox_staleness"] = _FunctionRunner(
            lambda ctx: scheduled_jobs.run_staleness_alert().summary()
        )
        if scheduled_jobs.has_recurring_generation:
            runners["scheduler.recurring_generation"] = _FunctionRunner(
                lambda ctx: scheduled_jobs.run_recurring_generation_all(
                    today=ctx.today, actor=ctx.actor
                ).summary()
            )
    return runners
