"""Unit tests for the three US-560 scheduled jobs.

In-memory fakes behind the ports — no database, no HTTP, no engine.
Each job runs across ALL installations/stores; one unit's failure
never blocks the others, and every failure is recorded + filed as
an ``inbox_dead_letter`` review item, never silent.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from typing import Any

import pytest

from hesabyar.application.commands.events import StalenessService
from hesabyar.application.commands.recurring import RecurringGenerationReport
from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.application.commands.scheduled_jobs import (
    SYNC_STREAM,
    ScheduledJobsService,
    build_job_runners,
)
from hesabyar.application.commands.sync import SyncReport
from hesabyar.application.errors import ScheduledJobNotRunnableError
from hesabyar.domain.events.errors import StoreNotConfiguredError
from hesabyar.domain.events.models import InboxEventStatus
from hesabyar.domain.events.sync import SyncCursor
from hesabyar.domain.integrations.models import Store
from hesabyar.domain.review_queue.models import ReviewItemType
from hesabyar.domain.scheduler.ports import SchedulerInventoryPort, SyncCursorState
from tests.event_fixtures import (
    TENANT_ID,
    FakeInboxRepository,
    make_inbox_event,
)
from tests.unit.test_scheduler_service import FakeAudit, FakeReviewQueue

NOW = datetime.datetime(2026, 11, 21, 10, 0, tzinfo=datetime.UTC)  # Saturday
TODAY = datetime.date(2026, 11, 21)
INSTALLATION_ID = "00000000-0000-0000-0000-000000000001"
INSTALLATION_2 = "00000000-0000-0000-0000-000000000002"
TENANT_2 = "00000000-0000-0000-0000-000000000002"


class FakeFinance:
    """Dunning stub: per-installation results or errors."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.errors: dict[str, str] = {}

    def run_dunning(self, *, tenant_id: str, installation_id: str, today=None, actor="scheduler"):  # type: ignore[no-untyped-def]
        from hesabyar.application.commands.installments_finance import DunningRunResult

        self.calls.append((tenant_id, installation_id))
        if installation_id in self.errors:
            raise RuntimeError(self.errors[installation_id])
        return DunningRunResult(
            as_of=today or TODAY,
            plans_scanned=2,
            overdue_legs=1,
            items_filed=("item-1",),
            fees_assessed_irr=50_000,
            journals_created=(7,),
            legs_skipped_no_account=(),
        )


class FakeSync:
    """Store-sync stub: per-store reports or errors, records its inputs."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.errors: dict[str, str] = {}
        self.reports: dict[str, SyncReport] = {}

    def sync(self, connector, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(dict(kwargs))
        store_key = kwargs["source_system"]
        if store_key in self.errors:
            raise RuntimeError(self.errors[store_key])
        return self.reports.get(
            store_key,
            SyncReport(
                source_system=store_key,
                source_store_id=kwargs["source_store_id"],
                connector_kind=kwargs["connector_kind"],
                status="completed",
                pages_fetched=1,
                events_created=3,
                events_duplicate=1,
                changes_unmapped=0,
            ),
        )


class FakeConnector:
    """A built connector (the fake sync never calls it)."""

    def __init__(self, store_key: str) -> None:
        self.store_key = store_key


class FakeConnectorFactory:
    """Connector factory stub: raises StoreNotConfiguredError for keyless stores."""

    def __init__(self, *, keyless: Sequence[str] = ()) -> None:
        self.keyless = set(keyless)
        self.built: list[str] = []
        self.closed = False

    def build(self, store, *, source_store_id: str, source: str):  # type: ignore[no-untyped-def]
        if store.store_key in self.keyless:
            raise StoreNotConfiguredError(
                f"Store {store.store_key!r} has no read-only API key stored.",
                details={"store_key": store.store_key},
            )
        self.built.append(store.store_key)
        return FakeConnector(store.store_key)

    def close(self) -> None:
        self.closed = True


class FakeStoreService:
    """Store service stub: records successful polls."""

    def __init__(self) -> None:
        self.polls: list[tuple[str, str]] = []

    def record_successful_poll(self, installation_id: str, store_key: str):  # type: ignore[no-untyped-def]
        self.polls.append((installation_id, store_key))
        return None


class FakeInventory(SchedulerInventoryPort):
    """Configurable scheduler inventory."""

    def __init__(self) -> None:
        self.plans: list[tuple[str, str]] = []
        self.stores: list[Store] = []
        self.tenants: list[tuple[str, str]] = []
        self.source_ids: dict[tuple[str, str], str] = {}
        self.cursors: dict[tuple[str, str, str, str], SyncCursorState] = {}

    def list_installations_with_plans(self) -> Sequence[tuple[str, str]]:
        return list(self.plans)

    def list_active_stores(self) -> Sequence[Store]:
        return list(self.stores)

    def list_installation_tenants(self) -> Sequence[tuple[str, str]]:
        return list(self.tenants)

    def latest_source_store_id(self, tenant_id: str, source_system: str) -> str | None:
        return self.source_ids.get((tenant_id, source_system))

    def get_sync_cursor(
        self, tenant_id: str, source_system: str, source_store_id: str, stream: str
    ) -> SyncCursorState | None:
        return self.cursors.get((tenant_id, source_system, source_store_id, stream))

    def save_sync_cursor(
        self,
        tenant_id: str,
        source_system: str,
        source_store_id: str,
        stream: str,
        *,
        cursor_value: str | None,
        last_synced_at: datetime.datetime,
    ) -> None:
        self.cursors[(tenant_id, source_system, source_store_id, stream)] = (
            SyncCursorState(cursor_value=cursor_value, last_synced_at=last_synced_at)
        )


class FakeTenantResolver:
    """Installation -> tenant stub."""

    def __init__(self, mapping: dict[str, str | None]) -> None:
        self.mapping = mapping

    def tenant_id_for_installation(self, installation_id: str) -> str | None:
        return self.mapping.get(installation_id)


def _store(
    store_key: str,
    installation_id: str = INSTALLATION_ID,
    *,
    api_base_url: str | None = "https://store.example.com/api",
    has_store_db_dsn: bool = False,
) -> Store:
    return Store(
        id=f"store-{store_key}",
        installation_id=installation_id,
        store_key=store_key,
        display_name=store_key,
        store_type="custom",
        prices_in_toman=True,
        has_webhook_secret=True,
        has_api_key=True,
        has_store_db_dsn=has_store_db_dsn,
        api_base_url=api_base_url,
    )


class FakeRecurring:
    """Recurring-generation stub: records the run, returns a fixed report."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def generate_due(self, *, today=None, actor="scheduler"):  # type: ignore[no-untyped-def]
        self.calls.append({"today": today, "actor": actor})
        return RecurringGenerationReport(
            as_of=today or TODAY,
            templates_scanned=2,
            occurrences_due=3,
            generated=2,
            posted=1,
            skipped_existing=1,
            failed=0,
            completed_templates=0,
        )


def _service(
    *,
    finance: FakeFinance | None = None,
    inventory: FakeInventory | None = None,
    sync: FakeSync | None = None,
    connector_factory: FakeConnectorFactory | None = None,
    store_service: FakeStoreService | None = None,
    inbox: FakeInboxRepository | None = None,
    review: ReviewQueueCommandService | None = None,
    queue: FakeReviewQueue | None = None,
    tenant_resolver: FakeTenantResolver | None = None,
    recurring: FakeRecurring | None = None,
) -> ScheduledJobsService:
    return ScheduledJobsService(
        finance=finance or FakeFinance(),  # type: ignore[arg-type]
        inventory=inventory or FakeInventory(),
        sync=sync or FakeSync(),  # type: ignore[arg-type]
        connector_factory=connector_factory,  # type: ignore[arg-type]
        store_service=store_service,  # type: ignore[arg-type]
        staleness=StalenessService(inbox or FakeInboxRepository()),
        review=review,
        queue=queue,
        tenant_resolver=tenant_resolver,
        recurring=recurring,  # type: ignore[arg-type]
        now=NOW,
    )


# -- dunning sweep --------------------------------------------------------------


def test_dunning_sweep_scans_every_installation_with_plans() -> None:
    finance = FakeFinance()
    inventory = FakeInventory()
    inventory.plans = [(TENANT_ID, INSTALLATION_ID), (TENANT_2, INSTALLATION_2)]
    service = _service(finance=finance, inventory=inventory)
    report = service.run_dunning_sweep_all(today=TODAY)
    assert report.installations_scanned == 2
    assert report.plans_scanned == 4
    assert report.overdue_legs == 2
    assert report.items_filed == 2
    assert report.fees_assessed_irr == 100_000
    assert report.journals_created == 2
    assert report.failures == ()
    assert finance.calls == [(TENANT_ID, INSTALLATION_ID), (TENANT_2, INSTALLATION_2)]


def test_dunning_sweep_failure_is_recorded_and_filed_not_fatal() -> None:
    finance = FakeFinance()
    finance.errors = {INSTALLATION_2: "engine down"}
    inventory = FakeInventory()
    inventory.plans = [(TENANT_ID, INSTALLATION_ID), (TENANT_2, INSTALLATION_2)]
    queue = FakeReviewQueue()
    review = ReviewQueueCommandService(queue, FakeAudit())
    service = _service(finance=finance, inventory=inventory, review=review, queue=queue)
    report = service.run_dunning_sweep_all(today=TODAY)
    assert report.installations_scanned == 2
    assert report.plans_scanned == 2  # the healthy installation still ran
    assert len(report.failures) == 1
    assert report.failures[0].unit == INSTALLATION_2
    assert report.failures[0].tenant_id == TENANT_2
    assert report.failures[0].error == "engine down"
    assert report.failures[0].review_item_id is not None
    assert len(queue.items) == 1
    item = queue.items[0]
    assert item.item_type is ReviewItemType.INBOX_DEAD_LETTER
    assert item.tenant_id == TENANT_2
    assert (
        item.source_event_ref
        == f"scheduled-job:scheduler.dunning_sweep:{INSTALLATION_2}:2026-11-21"
    )
    # A second run the same day does not file a second item.
    service.run_dunning_sweep_all(today=TODAY)
    assert len(queue.items) == 1


# -- store sync ------------------------------------------------------------------


def _sync_service(
    inventory: FakeInventory,
    sync: FakeSync,
    factory: FakeConnectorFactory | None = None,
    queue: FakeReviewQueue | None = None,
    review: ReviewQueueCommandService | None = None,
) -> ScheduledJobsService:
    return _service(
        inventory=inventory,
        sync=sync,
        connector_factory=factory if factory is not None else FakeConnectorFactory(),
        store_service=FakeStoreService(),
        review=review,
        queue=queue,
        tenant_resolver=FakeTenantResolver({INSTALLATION_ID: TENANT_ID}),
    )


def test_store_sync_uses_established_source_store_id_and_cursor() -> None:
    inventory = FakeInventory()
    inventory.stores = [_store("hayat")]
    inventory.source_ids = {(TENANT_ID, "hayat"): "store-1"}
    inventory.cursors = {
        (TENANT_ID, "hayat", "store-1", SYNC_STREAM): SyncCursorState(
            cursor_value="cursor-42",
            last_synced_at=datetime.datetime(2026, 11, 21, 9, 45, tzinfo=datetime.UTC),
        )
    }
    sync = FakeSync()
    service = _sync_service(inventory, sync)
    report = service.run_store_sync_all(today=TODAY)
    assert report.stores_synced == 1
    assert report.events_created == 3
    assert report.events_duplicate == 1
    assert len(sync.calls) == 1
    call = sync.calls[0]
    assert call["source_system"] == "hayat"
    assert call["source_store_id"] == "store-1"  # the webhook identity, not the key
    assert call["cursor"] == SyncCursor(value="cursor-42")
    assert call["updated_since"] is None  # the cursor drives a resume run
    assert call["connector_kind"] == "api"
    # A completed run clears the cursor and stamps the sync time.
    state = inventory.cursors[(TENANT_ID, "hayat", "store-1", SYNC_STREAM)]
    assert state.cursor_value is None
    assert state.last_synced_at == NOW


def test_store_sync_falls_back_to_store_key_and_last_sync_time() -> None:
    inventory = FakeInventory()
    inventory.stores = [_store("hayat")]
    last = datetime.datetime(2026, 11, 21, 9, 30, tzinfo=datetime.UTC)
    inventory.cursors = {
        (TENANT_ID, "hayat", "hayat", SYNC_STREAM): SyncCursorState(
            cursor_value=None, last_synced_at=last
        )
    }
    sync = FakeSync()
    service = _sync_service(inventory, sync)
    service.run_store_sync_all(today=TODAY)
    call = sync.calls[0]
    assert call["source_store_id"] == "hayat"  # no webhook identity yet: the key
    assert call["cursor"] is None
    assert call["updated_since"] == last  # bounded fresh poll, not the 7-day lookback


def test_store_sync_persists_resume_cursor_on_partial_run() -> None:
    inventory = FakeInventory()
    inventory.stores = [_store("hayat")]
    sync = FakeSync()
    sync.reports["hayat"] = SyncReport(
        source_system="hayat",
        source_store_id="hayat",
        connector_kind="api",
        status="partial",
        pages_fetched=10,
        events_created=50,
        events_duplicate=0,
        changes_unmapped=0,
        next_cursor="cursor-next",
        has_more=True,
    )
    service = _sync_service(inventory, sync)
    service.run_store_sync_all(today=TODAY)
    state = inventory.cursors[(TENANT_ID, "hayat", "hayat", SYNC_STREAM)]
    assert state.cursor_value == "cursor-next"
    assert state.last_synced_at == NOW


def test_store_sync_skips_unconfigured_stores_without_failing() -> None:
    inventory = FakeInventory()
    inventory.stores = [
        _store("webhook-only", api_base_url=None),
        _store("keyless"),
        _store("hayat"),
    ]
    sync = FakeSync()
    factory = FakeConnectorFactory(keyless=["keyless"])
    service = _sync_service(inventory, sync, factory=factory)
    report = service.run_store_sync_all(today=TODAY)
    assert report.stores_scanned == 3
    assert report.stores_synced == 1
    assert report.stores_skipped == 2
    assert {skip.store_key for skip in report.skipped} == {"webhook-only", "keyless"}
    assert report.failures == ()
    assert factory.closed  # the factory is always closed after the run


def test_store_sync_without_vault_skips_every_store() -> None:
    inventory = FakeInventory()
    inventory.stores = [_store("hayat")]
    sync = FakeSync()
    service = _service(
        inventory=inventory,
        sync=sync,
        connector_factory=None,
        tenant_resolver=FakeTenantResolver({INSTALLATION_ID: TENANT_ID}),
    )
    report = service.run_store_sync_all(today=TODAY)
    assert report.stores_synced == 0
    assert report.stores_skipped == 1
    assert report.failures == ()
    assert sync.calls == []


def test_store_sync_failure_is_recorded_filed_and_cursor_kept() -> None:
    inventory = FakeInventory()
    inventory.stores = [_store("hayat"), _store("other")]
    inventory.cursors = {
        (TENANT_ID, "hayat", "hayat", SYNC_STREAM): SyncCursorState(
            cursor_value="cursor-9",
            last_synced_at=datetime.datetime(2026, 11, 21, 9, 0, tzinfo=datetime.UTC),
        )
    }
    sync = FakeSync()
    sync.errors = {"hayat": "store unreachable"}
    queue = FakeReviewQueue()
    review = ReviewQueueCommandService(queue, FakeAudit())
    service = _sync_service(inventory, sync, queue=queue, review=review)
    report = service.run_store_sync_all(today=TODAY)
    assert report.stores_synced == 1  # the healthy store still ran
    assert len(report.failures) == 1
    assert report.failures[0].unit == "hayat"
    assert report.failures[0].review_item_id is not None
    assert len(queue.items) == 1
    assert queue.items[0].item_type is ReviewItemType.INBOX_DEAD_LETTER
    # The failed store's cursor is untouched: the next run retries here.
    state = inventory.cursors[(TENANT_ID, "hayat", "hayat", SYNC_STREAM)]
    assert state.cursor_value == "cursor-9"


# -- inbox staleness --------------------------------------------------------------


def _add_stale(
    inbox: FakeInboxRepository,
    *,
    tenant_id: str | None = TENANT_ID,
    hours_old: int = 30,
    status: InboxEventStatus = InboxEventStatus.RECEIVED,
) -> str:
    # Relative to the REAL now: the fake inbox's list_stale compares
    # against the wall clock, so a fixed past date would make the
    # "fresh" event stale on any later CI run.
    event = make_inbox_event(
        tenant_id=tenant_id,
        status=status,
        received_at=datetime.datetime.now(datetime.UTC)
        - datetime.timedelta(hours=hours_old),
    )
    inbox.events[event.id] = event
    return event.id


def test_staleness_alert_files_one_item_per_newly_stale_event() -> None:
    inbox = FakeInboxRepository()
    first = _add_stale(inbox)
    second = _add_stale(inbox, status=InboxEventStatus.PROCESSING)
    _add_stale(inbox, hours_old=2)  # not stale yet
    queue = FakeReviewQueue()
    review = ReviewQueueCommandService(queue, FakeAudit())
    service = _service(inbox=inbox, review=review, queue=queue)
    report = service.run_staleness_alert()
    assert report.stale_count == 2
    assert report.items_filed == 2
    assert report.already_filed == 0
    assert report.skipped_no_tenant == 0
    assert {item.source_event_ref for item in queue.items} == {
        f"inbox-event:{first}:stale",
        f"inbox-event:{second}:stale",
    }
    assert all(item.item_type is ReviewItemType.INBOX_DEAD_LETTER for item in queue.items)
    # A second run files nothing new (dedup by source_event_ref).
    report = service.run_staleness_alert()
    assert report.items_filed == 0
    assert report.already_filed == 2
    assert len(queue.items) == 2


def test_staleness_alert_counts_tenantless_events_without_filing() -> None:
    inbox = FakeInboxRepository()
    _add_stale(inbox, tenant_id=None)
    queue = FakeReviewQueue()
    review = ReviewQueueCommandService(queue, FakeAudit())
    service = _service(inbox=inbox, review=review, queue=queue)
    report = service.run_staleness_alert()
    assert report.stale_count == 1
    assert report.items_filed == 0
    assert report.skipped_no_tenant == 1
    assert queue.items == []


def test_staleness_alert_without_review_queue_reports_only() -> None:
    inbox = FakeInboxRepository()
    _add_stale(inbox)
    service = _service(inbox=inbox)  # no review wired
    report = service.run_staleness_alert()
    assert report.stale_count == 1
    assert report.items_filed == 0


# -- recurring generation (US-563) -----------------------------------------------


def test_recurring_generation_delegates_to_the_generator() -> None:
    recurring = FakeRecurring()
    service = _service(recurring=recurring)
    assert service.has_recurring_generation is True
    report = service.run_recurring_generation_all(today=TODAY, actor="operator")
    assert report.generated == 2
    assert report.posted == 1
    assert report.skipped_existing == 1
    assert recurring.calls == [{"today": TODAY, "actor": "operator"}]


def test_recurring_generation_unwired_raises_not_runnable() -> None:
    service = _service()  # no recurring wired
    assert service.has_recurring_generation is False
    with pytest.raises(
        ScheduledJobNotRunnableError, match="scheduler.recurring_generation"
    ):
        service.run_recurring_generation_all(today=TODAY)


def test_recurring_runner_registers_only_when_wired() -> None:
    wired = _service(recurring=FakeRecurring())
    runners = build_job_runners(scheduled_jobs=wired)
    assert "scheduler.recurring_generation" in runners
    assert "scheduler.dunning_sweep" in runners
    unwired = _service()
    runners = build_job_runners(scheduled_jobs=unwired)
    assert "scheduler.recurring_generation" not in runners
    assert "scheduler.dunning_sweep" in runners
