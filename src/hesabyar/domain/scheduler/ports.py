"""Ports for the job registry + the scheduler's inventory reads (US-560).

Two ports, split by concern:

- :class:`JobRegistryPort` owns the registry itself: definitions,
  enable/disable, and run records. The SQLAlchemy implementation
  flushes; the caller commits (the recorder commits on its own
  session so a run record survives its job's rollback).
- :class:`SchedulerInventoryPort` answers the cross-module reads the
  scheduled jobs need: which installations have installment plans
  (dunning), which stores are active (store sync), which tenants
  exist (failure review items), the established source-store
  identity per store, and the persisted sync cursors. The scheduler
  is the platform's cross-cutting orchestrator, so these reads live
  here rather than growing five repositories at once.
"""

from __future__ import annotations

import datetime
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass

from hesabyar.domain.integrations.models import Store
from hesabyar.domain.scheduler.models import JobDefinition, JobRun


@dataclass(frozen=True)
class SyncCursorState:
    """A store's persisted polling state (``integration_cursors`` row).

    ``cursor_value`` resumes a partial/max-pages run; ``last_synced_at``
    bounds the next fresh poll (``updated_since``) when no cursor is
    stored — so a completed run never re-polls the 7-day default
    lookback every 15 minutes.
    """

    cursor_value: str | None
    last_synced_at: datetime.datetime | None


class JobRegistryPort(ABC):
    """The job registry: definitions + run records."""

    @abstractmethod
    def ensure_defaults(self, defaults: Sequence[JobDefinition]) -> None:
        """Insert the seeded jobs that have no row yet (never overwrite)."""

    @abstractmethod
    def list_definitions(self) -> Sequence[JobDefinition]:
        """All registered jobs, in registry order."""

    @abstractmethod
    def get_definition(self, job_id: str) -> JobDefinition | None:
        """One job by id, or None."""

    @abstractmethod
    def set_enabled(self, job_id: str, enabled: bool) -> JobDefinition | None:
        """Enable/disable a job; None when the id is unknown."""

    @abstractmethod
    def record_run(self, run: JobRun) -> JobRun:
        """Persist one run record (flushes; the caller commits)."""

    @abstractmethod
    def latest_run(self, job_id: str) -> JobRun | None:
        """The job's most recent run, or None when it never ran."""

    @abstractmethod
    def list_runs(self, job_id: str, *, limit: int = 20) -> Sequence[JobRun]:
        """The job's recent runs, newest first."""


class SchedulerInventoryPort(ABC):
    """Cross-module reads for the scheduled jobs."""

    @abstractmethod
    def list_installations_with_plans(self) -> Sequence[tuple[str, str]]:
        """(tenant_id, installation_id) pairs with installment plans (dunning scope)."""

    @abstractmethod
    def list_active_stores(self) -> Sequence[Store]:
        """Every active store across all installations (store-sync scope)."""

    @abstractmethod
    def list_installation_tenants(self) -> Sequence[tuple[str, str]]:
        """(tenant_id, installation_id) pairs with API keys (failure-notify scope)."""

    @abstractmethod
    def latest_source_store_id(self, tenant_id: str, source_system: str) -> str | None:
        """The source_store_id of the tenant's newest inbox event for a connector.

        The established identity for a store: the scheduled sync must
        poll under the SAME identity the webhooks use, or the same
        order would be ingested twice under different idempotency
        keys. None when the store never sent an event.
        """

    @abstractmethod
    def get_sync_cursor(
        self,
        tenant_id: str,
        source_system: str,
        source_store_id: str,
        stream: str,
    ) -> SyncCursorState | None:
        """A store's persisted polling state, or None on first sync."""

    @abstractmethod
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
