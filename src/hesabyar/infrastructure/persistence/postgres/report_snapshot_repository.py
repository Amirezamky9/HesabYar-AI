"""PostgreSQL implementation of the report-snapshot port (US-403).

Implements ``domain/reports/ports.py::ReportSnapshotPort`` over the
``report_snapshots`` table. This is the ONLY place that knows the
snapshot's SQLAlchemy model — the application layer talks to the port,
never to this table (spec §6.3).

Persistence contract (same as every other writer in the codebase):
``save_snapshot`` flushes so ids/timestamps exist but does NOT commit
— the caller (REST route) owns the transaction.
"""

from __future__ import annotations

import dataclasses

from sqlalchemy import select
from sqlalchemy.orm import Session

from hesabyar.domain.reports.models import ReportSnapshot
from hesabyar.domain.reports.ports import ReportSnapshotPort
from hesabyar.infrastructure.persistence.postgres.models import ReportSnapshotModel


def _snapshot_from_row(row: ReportSnapshotModel) -> ReportSnapshot:
    """Map a snapshot row onto the domain dataclass."""
    return ReportSnapshot(
        snapshot_id=row.id,
        tenant_id=row.tenant_id,
        company_id=row.company_id,
        validation_run_id=row.validation_run_id,
        period_start=row.period_start,
        period_end=row.period_end,
        as_of_date=row.as_of_date,
        source_endpoint=row.source_endpoint,
        ruleset_version=row.ruleset_version,
        payload_hash=row.payload_hash,
        payload=dict(row.payload),
        balanced=row.balanced,
        total_debit=row.total_debit,
        total_credit=row.total_credit,
        created_at=row.created_at,
    )


class SqlAlchemyReportSnapshotRepository(ReportSnapshotPort):
    """The report-snapshot port backed by the HesabYar database."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def save_snapshot(self, snapshot: ReportSnapshot) -> ReportSnapshot:
        """Persist a snapshot; returns it with ``created_at`` set.

        Flushes so ids/timestamps exist; does NOT commit — the caller
        owns the transaction.
        """
        row = ReportSnapshotModel(
            id=snapshot.snapshot_id,
            tenant_id=snapshot.tenant_id,
            company_id=snapshot.company_id,
            validation_run_id=snapshot.validation_run_id,
            period_start=snapshot.period_start,
            period_end=snapshot.period_end,
            as_of_date=snapshot.as_of_date,
            source_endpoint=snapshot.source_endpoint,
            ruleset_version=snapshot.ruleset_version,
            payload_hash=snapshot.payload_hash,
            payload=dict(snapshot.payload),
            balanced=snapshot.balanced,
            total_debit=snapshot.total_debit,
            total_credit=snapshot.total_credit,
        )
        self._session.add(row)
        self._session.flush()
        return dataclasses.replace(snapshot, created_at=row.created_at)

    def get_snapshot(self, snapshot_id: str, tenant_id: str) -> ReportSnapshot | None:
        """One snapshot for the owning tenant, or None."""
        row = self._session.execute(
            select(ReportSnapshotModel).where(
                ReportSnapshotModel.id == snapshot_id,
                ReportSnapshotModel.tenant_id == tenant_id,
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return _snapshot_from_row(row)

    def get_snapshot_for_run(self, validation_run_id: str, tenant_id: str) -> ReportSnapshot | None:
        """The snapshot joined to a validation run, for the owning tenant, or None."""
        row = self._session.execute(
            select(ReportSnapshotModel).where(
                ReportSnapshotModel.validation_run_id == validation_run_id,
                ReportSnapshotModel.tenant_id == tenant_id,
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return _snapshot_from_row(row)
