"""Port (interface) for report-snapshot persistence (US-403).

The application layer owns the use-case logic (fetching the live trial
balance, building the validation document, running the rules); this
port owns persistence of the ``report_snapshots`` table. Implementations
read/write the snapshot rows; the application never touches the table.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from hesabyar.domain.reports.models import ReportSnapshot


class ReportSnapshotPort(ABC):
    """Abstract port for persisted report snapshots (spec §7.4).

    The port flushes on save but does NOT commit — the caller (REST
    route) owns the transaction, like every other writer in the
    codebase.
    """

    @abstractmethod
    def save_snapshot(self, snapshot: ReportSnapshot) -> ReportSnapshot:
        """Persist a snapshot; returns it with ``created_at`` set.

        The snapshot's ``snapshot_id`` is supplied by the caller.
        """
        ...

    @abstractmethod
    def get_snapshot(self, snapshot_id: str, tenant_id: str) -> ReportSnapshot | None:
        """One snapshot for the owning tenant, or None."""
        ...

    @abstractmethod
    def get_snapshot_for_run(self, validation_run_id: str, tenant_id: str) -> ReportSnapshot | None:
        """The snapshot joined to a validation run, for the owning tenant, or None."""
        ...
