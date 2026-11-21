"""Procrastinate wiring for the backup jobs (US-703, spec §15.4).

Two periodic jobs on the ``backup`` queue, on the SAME Procrastinate
app as the event platform (US-560 — no second scheduler):

- ``backup.databases`` (daily 02:00 Tehran, Friday included) — dump
  both databases, encrypt, upload, prune.
- ``backup.restore_drill`` (Jalali 1st, 03:00 Tehran) — restore the
  newest backup pair into scratch databases and reconcile. The
  Procrastinate cron fires daily; the recorder's Jalali-monthly gate
  runs it only on the 1st (the other days are recorded as skipped).

The services are built FRESH on every run from the handle's settings
(a rotated key or endpoint takes effect without a worker restart),
and construction failures are run-time failures: the job is
recorded as failed and files a review item, like any other job
failure. The backup tasks use no database session of their own
(they shell out to pg_dump), so the handle's commit/close are
no-ops kept for the uniform task shape.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import procrastinate

from hesabyar.domain.backup.errors import BackupError, DrillError
from hesabyar.domain.backup.models import BackupTarget
from hesabyar.domain.backup.ports import DatabaseDumperPort
from hesabyar.domain.scheduler.schedules import tehran_today
from hesabyar.infrastructure.config import BackupSettings
from hesabyar.infrastructure.events.scheduler_queue import (
    JobRunRecorder,
    run_recorded,
)
from hesabyar.infrastructure.operations.factories import (
    build_backup_service,
    build_drill_service,
)
from hesabyar.infrastructure.operations.restore_drill import ScratchEngineFactory

logger = logging.getLogger(__name__)

#: Queue name for backup jobs.
BACKUP_QUEUE = "backup"

#: Periodic job: back up both databases.
BACKUP_TASK = "backup.databases"

#: Periodic job: restore drill (newest pair into scratch databases + reconcile).
RESTORE_DRILL_TASK = "backup.restore_drill"

#: Default cadence: daily 02:00 Tehran (22:30 UTC the previous day —
#: Iran is UTC+3:30 year-round, no DST since 2022). Overridable via
#: create_event_app / HESABYAR_BACKUP__BACKUP_CRON.
BACKUP_CRON = "30 22 * * *"

#: Default cadence: daily 03:00 Tehran fire (23:30 UTC); the
#: registry's Jalali-monthly gate runs the drill on the 1st only.
#: Overridable via create_event_app / HESABYAR_BACKUP__RESTORE_DRILL_CRON.
RESTORE_DRILL_CRON = "30 23 * * *"


@dataclass
class BackupWorkerHandle:
    """Backup settings + tools bound to one job run.

    ``hesabyar_database_url`` is the RESOLVED HesabYar DSN (the
    backup setting, or the application database URL); the worker
    factory resolves it. ``tryton_database_url`` is None when the
    operator has not configured it — the backup job then fails
    loudly rather than backing up half the pair.
    """

    settings: BackupSettings
    dumper: DatabaseDumperPort
    engine_factory: ScratchEngineFactory | None
    hesabyar_database_url: str
    tryton_database_url: str | None
    commit: Callable[[], None]
    close: Callable[[], None]


#: Factory building a fresh backup handle per job run (the worker
#: script supplies it; it owns settings and the pg tools).
BackupWorkerFactory = Callable[[], BackupWorkerHandle]


def register_backup_tasks(
    app: procrastinate.App,
    backup_factory: BackupWorkerFactory,
    *,
    backup_cron: str = BACKUP_CRON,
    restore_drill_cron: str = RESTORE_DRILL_CRON,
    recorder: JobRunRecorder | None = None,
) -> None:
    """Register the backup tasks on a Procrastinate app (US-703)."""

    @app.periodic(cron=backup_cron)
    @app.task(queue=BACKUP_QUEUE, name=BACKUP_TASK, retry=False)
    def backup_databases(timestamp: int = 0) -> None:  # noqa: ARG001 - periodic passes the schedule timestamp
        handle = backup_factory()
        day = tehran_today(timestamp)
        try:

            def _do() -> dict[str, Any]:
                service = build_backup_service(handle.settings, handle.dumper)
                report = service.run_all(
                    handle.hesabyar_database_url, handle.tryton_database_url
                )
                logger.info(
                    "Backup done: ok=%s targets=%s pruned=%d",
                    report.ok,
                    sorted(t.value for t in report.manifests),
                    len(report.pruned_keys),
                )
                if not report.ok:
                    raise BackupError(
                        "Backup failed for: "
                        + "; ".join(
                            f"{t.value} ({e})" for t, e in report.failures.items()
                        )
                        + (
                            f"; succeeded: {', '.join(sorted(t.value for t in report.manifests))}"
                            if report.manifests
                            else "; nothing was backed up"
                        )
                    )
                return report.summary()

            run_recorded(recorder, BACKUP_TASK, _do, today=day)
            handle.commit()
        finally:
            handle.close()

    @app.periodic(cron=restore_drill_cron)
    @app.task(queue=BACKUP_QUEUE, name=RESTORE_DRILL_TASK, retry=False)
    def restore_drill(timestamp: int = 0) -> None:  # noqa: ARG001 - periodic passes the schedule timestamp
        handle = backup_factory()
        day = tehran_today(timestamp)
        try:

            def _do() -> dict[str, Any]:
                service = build_drill_service(
                    handle.settings,
                    handle.dumper,
                    handle.engine_factory,
                    hesabyar_database_url=handle.hesabyar_database_url,
                )
                manifests = {
                    target: service.latest_manifest(target)
                    for target in (BackupTarget.HESABYAR_DB, BackupTarget.TRYTON_DB)
                }
                missing = [t.value for t, m in manifests.items() if m is None]
                if missing:
                    raise BackupError(
                        "No backup found for: " + ", ".join(missing)
                        + " — run a backup before drilling."
                    )
                report = service.run(
                    {t: m for t, m in manifests.items() if m is not None}
                )
                logger.info(
                    "Restore drill done: passed=%s fully_verified=%s failed=%s skipped=%s",
                    report.passed,
                    report.fully_verified,
                    [c.name for c in report.failed_checks()],
                    [c.name for c in report.skipped_checks()],
                )
                if not report.passed:
                    raise DrillError(
                        "Restore drill failed: "
                        + "; ".join(
                            f"{c.name} ({c.detail})" for c in report.failed_checks()
                        )
                    )
                return report.summary()

            run_recorded(recorder, RESTORE_DRILL_TASK, _do, today=day)
            handle.commit()
        finally:
            handle.close()
