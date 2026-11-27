"""Scriptable pre-upgrade checks (US-705).

The upgrade runbook (``docs/runbook-upgrade-rollback.md`` §2) lists
the checks an operator runs before every upgrade. The checks that
can be scripted live here, so the operator runs a command
(``scripts/pre_upgrade_check.py``) instead of trusting prose — and
so CI can pin their behaviour. The checks that cannot be scripted
(the release notes were read, the maintenance window was announced,
the config diff was reviewed) stay prose in the runbook, listed
explicitly as manual steps the script prints as a reminder.

The checks reuse the restore drill's result type
(:class:`~hesabyar.domain.backup.models.DrillCheckResult` — a stable
name + PASS/FAIL/SKIPPED + detail) and its conventions:

- a check that cannot verify what it must verify FAILS or SKIPS —
  it never reports a fabricated PASS;
- an unexpected error inside one check FAILS that check and never
  aborts the others (the drill's ``_run_check`` pattern);
- SKIPPED means "no information" (e.g. no backup has ever run),
  which still BLOCKS the upgrade: the CLI exits non-zero unless
  every check PASSED, because upgrading without a fresh backup is
  exactly what the runbook forbids.

Checks:

- ``hesabyar.migrations`` — the database is at the single Alembic
  head (reused from
  :mod:`hesabyar.infrastructure.operations.restore_drill` — the
  same check the drill runs, not a second implementation).
- ``pre_upgrade.backup_fresh`` — both databases have a fresh
  successful backup, read through the US-703/US-704
  :class:`~hesabyar.domain.monitoring.ports.BackupStatusSource`
  contract (the job registry's latest ``backup.databases`` run).
  The ok-vs-stale cadence decision belongs to the source, as in
  monitoring; this check only maps the reported status.
- ``pre_upgrade.tryton_reachable`` — the Tryton engine answers a
  minimal read through the engine port (``list_companies``). An
  upgrade that cannot reach Tryton cannot verify module versions
  afterwards, so an unreachable engine fails the gate.
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine

from hesabyar.domain.accounting.ports import AccountingEnginePort
from hesabyar.domain.backup.models import DrillCheckResult, DrillCheckStatus
from hesabyar.domain.monitoring.models import BackupStatusValue
from hesabyar.domain.monitoring.ports import BackupStatusSource
from hesabyar.infrastructure.operations.restore_drill import (
    CHECK_MIGRATIONS,
    check_alembic,
)

logger = logging.getLogger(__name__)

#: Check names (stable strings — the runbook, the CLI and the tests use them).
CHECK_BACKUP_FRESH = "pre_upgrade.backup_fresh"
CHECK_TRYTON_REACHABLE = "pre_upgrade.tryton_reachable"


def _pass(name: str, detail: str = "") -> DrillCheckResult:
    return DrillCheckResult(name=name, status=DrillCheckStatus.PASS, detail=detail)


def _fail(name: str, detail: str) -> DrillCheckResult:
    return DrillCheckResult(name=name, status=DrillCheckStatus.FAIL, detail=detail)


def _skip(name: str, detail: str) -> DrillCheckResult:
    return DrillCheckResult(name=name, status=DrillCheckStatus.SKIPPED, detail=detail)


def check_backup_fresh(
    source: BackupStatusSource,
    *,
    now: datetime.datetime,
) -> DrillCheckResult:
    """Both databases have a fresh successful backup (US-703 contract).

    The source already applies the cadence (ok vs stale); this check
    maps the reported status: ``failed`` or ``stale`` FAILS (the last
    good backup is missing or old — take a backup before upgrading),
    ``unknown`` SKIPS (no backup information at all — the upgrade is
    still blocked, by the CLI's all-pass gate, but the report says
    honestly that it could not verify rather than claiming a failure
    it did not observe). No statuses at all is a SKIP, not a PASS.
    """
    try:
        statuses = source.statuses(now=now)
    except Exception as exc:
        return _fail(CHECK_BACKUP_FRESH, f"Could not read the backup status: {exc}")
    if not statuses:
        return _skip(
            CHECK_BACKUP_FRESH,
            "The backup status source reported no databases — "
            "take a backup and verify it before upgrading.",
        )
    details: list[str] = []
    worst = DrillCheckStatus.PASS
    for status in statuses:
        if status.status is BackupStatusValue.OK:
            details.append(f"{status.database}: ok")
        elif status.status is BackupStatusValue.FAILED:
            worst = DrillCheckStatus.FAIL
            error = f" ({status.error})" if status.error else ""
            details.append(f"{status.database}: latest backup FAILED{error}")
        elif status.status is BackupStatusValue.STALE:
            worst = DrillCheckStatus.FAIL
            last = (
                status.last_success_at.isoformat()
                if status.last_success_at is not None
                else "never"
            )
            details.append(
                f"{status.database}: no successful backup within the cadence "
                f"(last success: {last})"
            )
        else:  # UNKNOWN — no information, reported never silent.
            if worst is not DrillCheckStatus.FAIL:
                worst = DrillCheckStatus.SKIPPED
            details.append(f"{status.database}: unknown (no backup run recorded)")
    detail = "; ".join(details)
    if worst is DrillCheckStatus.FAIL:
        return _fail(
            CHECK_BACKUP_FRESH,
            f"{detail} — take a fresh backup of BOTH databases before upgrading "
            "(see docs/backup-restore.md §7).",
        )
    if worst is DrillCheckStatus.SKIPPED:
        return _skip(
            CHECK_BACKUP_FRESH,
            f"{detail} — no backup to fall back on; take one before upgrading.",
        )
    return _pass(CHECK_BACKUP_FRESH, detail)


def check_tryton_reachable(engine: AccountingEnginePort) -> DrillCheckResult:
    """The Tryton engine answers a minimal read (``list_companies``).

    A company count of zero still passes: the engine answered, and
    an empty installation is a provisioning fact, not a connectivity
    failure. An engine that raises (or times out) fails the gate —
    the post-upgrade module verification needs a reachable engine.
    """
    try:
        companies = engine.list_companies()
    except Exception as exc:
        return _fail(
            CHECK_TRYTON_REACHABLE,
            f"Could not reach the Tryton engine: {exc}",
        )
    return _pass(
        CHECK_TRYTON_REACHABLE,
        f"Tryton answered ({len(companies)} compan{'y' if len(companies) == 1 else 'ies'}).",
    )


def _run_check(
    name: str, func: Callable[..., DrillCheckResult], *args: Any, **kwargs: Any
) -> DrillCheckResult:
    """Run one check; an unexpected error FAILS it (never aborts the run).

    The restore drill's ``_run_check`` pattern, duplicated here so
    this module does not depend on the drill service class.
    """
    try:
        return func(*args, **kwargs)
    except Exception as exc:
        logger.exception("Pre-upgrade check %s raised.", name)
        return _fail(name, f"Check raised unexpectedly: {exc}")


def run_pre_upgrade_checks(
    *,
    hesabyar_engine: Engine,
    migrations_dir: Path,
    backup_source: BackupStatusSource,
    tryton_engine: AccountingEnginePort,
    now: datetime.datetime,
) -> list[DrillCheckResult]:
    """Run every scriptable pre-upgrade check, in stable order.

    The caller decides the gate: the CLI exits non-zero unless every
    result is PASS (a SKIP blocks the upgrade just as a FAIL does —
    an unverified backup is not a backup). The manual steps the
    script cannot check are printed by the CLI as a reminder, not
    folded into these results.
    """
    return [
        _run_check(CHECK_MIGRATIONS, check_alembic, hesabyar_engine, migrations_dir),
        _run_check(CHECK_BACKUP_FRESH, check_backup_fresh, backup_source, now=now),
        _run_check(CHECK_TRYTON_REACHABLE, check_tryton_reachable, tryton_engine),
    ]
