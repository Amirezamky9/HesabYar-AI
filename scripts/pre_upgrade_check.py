"""Pre-upgrade checks CLI (US-705).

Runs the scriptable pre-upgrade checks from the upgrade runbook
(docs/runbook-upgrade-rollback.md §2) and exits non-zero unless
every check PASSED — a skipped check blocks the upgrade just as a
failed one does (an unverified backup is not a backup). Settings
come from the environment (``HESABYAR_*``); the backup status is
read from the job registry (the US-703/US-704 contract), never
from the object store directly.

Usage (from the repo root)::

    uv run python scripts/pre_upgrade_check.py

Exit codes: 0 every check passed, 1 any check failed or skipped
(details on stdout), 2 usage error. Nothing here ever prints a
secret: DSNs, keys and credentials stay out of stdout/stderr (only
database NAMES and check details are printed).

The checks this script CANNOT run are printed as a manual reminder
after the results — they are still required, they just need a
human (see the runbook §2).
"""

from __future__ import annotations

import argparse
import datetime
import logging
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from hesabyar.domain.backup.models import DrillCheckStatus
from hesabyar.infrastructure.accounting.tryton.adapter import TrytonAccountingAdapter
from hesabyar.infrastructure.accounting.tryton.client import TrytonRestClient
from hesabyar.infrastructure.config import AppSettings
from hesabyar.infrastructure.operations.pre_upgrade_checks import run_pre_upgrade_checks
from hesabyar.infrastructure.operations.restore_drill import resolve_migrations_dir
from hesabyar.infrastructure.persistence.postgres.monitoring_repository import (
    JobRegistryBackupStatusSource,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

#: Manual pre-upgrade steps no script can verify (runbook §2).
MANUAL_STEPS = (
    "Read the release notes (docs/release.md) for this version — "
    "note any manual step they call out.",
    "Announce the maintenance window to the store (upgrades stop the worker).",
    "Review the configuration diff (new HESABYAR_* settings in this release).",
    "Confirm the rollback decision-maker is reachable during the window.",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the scriptable pre-upgrade checks (US-705)."
    )
    parser.parse_args(argv)
    settings = AppSettings()
    engine = create_engine(settings.database.sqlalchemy_url)
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    tryton = settings.tryton
    client = TrytonRestClient(
        tryton.base_url,
        tryton.database,
        tryton.api_key,
        timeout=tryton.timeout_seconds,
    )
    try:
        backup_source = JobRegistryBackupStatusSource(
            session, max_age_hours=settings.monitoring.backup_max_age_hours
        )
        results = run_pre_upgrade_checks(
            hesabyar_engine=engine,
            migrations_dir=resolve_migrations_dir(),
            backup_source=backup_source,
            tryton_engine=TrytonAccountingAdapter(client),
            now=datetime.datetime.now(datetime.UTC),
        )
    finally:
        client.close()
        session.close()
        engine.dispose()
    for result in results:
        print(f"{result.status.value.upper():7} {result.name}: {result.detail}")
    all_passed = all(r.status is DrillCheckStatus.PASS for r in results)
    print()
    print("Manual steps (no script can verify these — do them by hand):")
    for step in MANUAL_STEPS:
        print(f"  [ ] {step}")
    if not all_passed:
        failed = [r.name for r in results if r.status is DrillCheckStatus.FAIL]
        skipped = [r.name for r in results if r.status is DrillCheckStatus.SKIPPED]
        print()
        if failed:
            print(f"BLOCKED: failed checks: {', '.join(failed)}", file=sys.stderr)
        if skipped:
            print(
                f"BLOCKED: unverified checks: {', '.join(skipped)} "
                "(resolve them before upgrading — see the runbook §2).",
                file=sys.stderr,
            )
        return 1
    print()
    print("All scriptable pre-upgrade checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
