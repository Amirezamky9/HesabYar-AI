"""SQLAlchemy scheduler repository (US-560, Phase 5-7 ۵خ W1).

Implements :class:`JobRegistryPort` over ``scheduled_jobs`` /
``scheduled_job_runs`` and :class:`SchedulerInventoryPort` over the
cross-module reads the scheduled jobs need (installment plans,
stores, API keys, inbox events, integration cursors). The
repository never commits: the caller (router / worker / recorder)
owns the transaction.

Registry ordering: ``list_definitions`` returns jobs in seed order
(``DEFAULT_JOBS`` order), with any unknown rows (there should be
none — the registry is closed) sorted after them by id.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from hesabyar.domain.integrations.models import Store
from hesabyar.domain.scheduler.models import (
    DEFAULT_JOBS,
    JobDefinition,
    JobOutcome,
    JobRun,
    TriggeredBy,
)
from hesabyar.domain.scheduler.ports import (
    JobRegistryPort,
    SchedulerInventoryPort,
    SyncCursorState,
)
from hesabyar.domain.scheduler.schedules import JobSchedule
from hesabyar.infrastructure.persistence.postgres.integrations_repository import (
    _store_from_row,
)
from hesabyar.infrastructure.persistence.postgres.models import (
    ApiClientModel,
    ApiKeyModel,
    InboxEventModel,
    InstallmentPlanModel,
    IntegrationCursorModel,
    ScheduledJobModel,
    ScheduledJobRunModel,
    StoreModel,
)

#: Seed order for the registry listing (unknown rows sort after, by id).
_SEED_ORDER = {job.id: index for index, job in enumerate(DEFAULT_JOBS)}


def _definition_from_row(row: ScheduledJobModel) -> JobDefinition:
    return JobDefinition(
        id=row.id,
        queue=row.queue,
        schedule=JobSchedule.from_dict(dict(row.schedule)),
        enabled=row.enabled,
        description=row.description,
    )


def _run_from_row(row: ScheduledJobRunModel) -> JobRun:
    return JobRun(
        id=row.id,
        job_id=row.job_id,
        started_at=row.started_at,
        finished_at=row.finished_at,
        outcome=JobOutcome(row.outcome),
        summary=dict(row.summary or {}),
        error=row.error,
        triggered_by=TriggeredBy(row.triggered_by),
    )


class SqlAlchemySchedulerRepository(JobRegistryPort, SchedulerInventoryPort):
    """Job registry + scheduler inventory over SQLAlchemy (flushes, never commits)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- JobRegistryPort -------------------------------------------------------

    def ensure_defaults(self, defaults: Sequence[JobDefinition]) -> None:
        """Insert the seeded jobs that have no row yet (never overwrite)."""
        existing = {
            row[0]
            for row in self._session.execute(
                select(ScheduledJobModel.id)
            ).all()
        }
        for definition in defaults:
            if definition.id in existing:
                continue
            self._session.add(
                ScheduledJobModel(
                    id=definition.id,
                    queue=definition.queue,
                    schedule=definition.schedule.to_dict(),
                    enabled=definition.enabled,
                    description=definition.description,
                )
            )
        self._session.flush()

    def list_definitions(self) -> Sequence[JobDefinition]:
        """All registered jobs, in seed order."""
        rows = list(
            self._session.execute(select(ScheduledJobModel)).scalars()
        )
        definitions = [_definition_from_row(row) for row in rows]
        definitions.sort(key=lambda d: (_SEED_ORDER.get(d.id, len(_SEED_ORDER)), d.id))
        return definitions

    def get_definition(self, job_id: str) -> JobDefinition | None:
        """One job by id, or None."""
        row = self._session.get(ScheduledJobModel, job_id)
        return _definition_from_row(row) if row is not None else None

    def set_enabled(self, job_id: str, enabled: bool) -> JobDefinition | None:
        """Enable/disable a job; None when the id is unknown."""
        row = self._session.get(ScheduledJobModel, job_id)
        if row is None:
            return None
        row.enabled = enabled
        self._session.flush()
        return _definition_from_row(row)

    def record_run(self, run: JobRun) -> JobRun:
        """Persist one run record (flushes; the caller commits)."""
        row = ScheduledJobRunModel(
            id=run.id,
            job_id=run.job_id,
            started_at=run.started_at,
            finished_at=run.finished_at,
            outcome=run.outcome.value,
            summary=dict(run.summary),
            error=run.error,
            triggered_by=run.triggered_by.value,
        )
        self._session.add(row)
        self._session.flush()
        return run

    def latest_run(self, job_id: str) -> JobRun | None:
        """The job's most recent run, or None when it never ran."""
        row = self._session.execute(
            select(ScheduledJobRunModel)
            .where(ScheduledJobRunModel.job_id == job_id)
            .order_by(
                ScheduledJobRunModel.started_at.desc(), ScheduledJobRunModel.id.desc()
            )
            .limit(1)
        ).scalar_one_or_none()
        return _run_from_row(row) if row is not None else None

    def list_runs(self, job_id: str, *, limit: int = 20) -> Sequence[JobRun]:
        """The job's recent runs, newest first."""
        rows = list(
            self._session.execute(
                select(ScheduledJobRunModel)
                .where(ScheduledJobRunModel.job_id == job_id)
                .order_by(
                    ScheduledJobRunModel.started_at.desc(),
                    ScheduledJobRunModel.id.desc(),
                )
                .limit(limit)
            ).scalars()
        )
        return [_run_from_row(row) for row in rows]

    # -- SchedulerInventoryPort --------------------------------------------------

    def list_installations_with_plans(self) -> Sequence[tuple[str, str]]:
        """(tenant_id, installation_id) pairs with installment plans."""
        rows = self._session.execute(
            select(
                InstallmentPlanModel.tenant_id, InstallmentPlanModel.installation_id
            )
            .distinct()
            .order_by(
                InstallmentPlanModel.tenant_id, InstallmentPlanModel.installation_id
            )
        ).all()
        return [(row[0], row[1]) for row in rows]

    def list_active_stores(self) -> Sequence[Store]:
        """Every active store across all installations, ordered by key."""
        rows = list(
            self._session.execute(
                select(StoreModel)
                .where(StoreModel.status == "active")
                .order_by(StoreModel.installation_id, StoreModel.store_key)
            ).scalars()
        )
        return [_store_from_row(row) for row in rows]

    def list_installation_tenants(self) -> Sequence[tuple[str, str]]:
        """(tenant_id, installation_id) pairs with API keys."""
        rows = self._session.execute(
            select(ApiClientModel.tenant_id, ApiKeyModel.installation_id)
            .join(ApiKeyModel, ApiKeyModel.client_id == ApiClientModel.id)
            .distinct()
            .order_by(ApiClientModel.tenant_id, ApiKeyModel.installation_id)
        ).all()
        return [(row[0], row[1]) for row in rows]

    def latest_source_store_id(self, tenant_id: str, source_system: str) -> str | None:
        """The source_store_id of the tenant's newest inbox event for a connector."""
        return self._session.execute(
            select(InboxEventModel.source_store_id)
            .where(
                InboxEventModel.tenant_id == tenant_id,
                InboxEventModel.source_system == source_system,
            )
            .order_by(
                InboxEventModel.received_at.desc(), InboxEventModel.id.desc()
            )
            .limit(1)
        ).scalar_one_or_none()

    def get_sync_cursor(
        self,
        tenant_id: str,
        source_system: str,
        source_store_id: str,
        stream: str,
    ) -> SyncCursorState | None:
        """A store's persisted polling state, or None on first sync."""
        row = self._session.execute(
            select(IntegrationCursorModel).where(
                IntegrationCursorModel.tenant_id == tenant_id,
                IntegrationCursorModel.source_system == source_system,
                IntegrationCursorModel.source_store_id == source_store_id,
                IntegrationCursorModel.stream == stream,
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return SyncCursorState(
            cursor_value=row.cursor_value, last_synced_at=row.last_synced_at
        )

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
        """Persist a store's polling state (upsert; flushes)."""
        row = self._session.execute(
            select(IntegrationCursorModel).where(
                IntegrationCursorModel.tenant_id == tenant_id,
                IntegrationCursorModel.source_system == source_system,
                IntegrationCursorModel.source_store_id == source_store_id,
                IntegrationCursorModel.stream == stream,
            )
        ).scalar_one_or_none()
        if row is None:
            row = IntegrationCursorModel(
                tenant_id=tenant_id,
                source_system=source_system,
                source_store_id=source_store_id,
                stream=stream,
                cursor_value=cursor_value,
                last_synced_at=last_synced_at,
            )
            self._session.add(row)
        else:
            row.cursor_value = cursor_value
            row.last_synced_at = last_synced_at
        self._session.flush()
