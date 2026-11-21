"""Operator CLI for backups + restore drills (US-703).

The same services the scheduler jobs run, on demand — for manual
backups, manual drills, listing backups, and pruning. Settings come
from the environment (``HESABYAR_*``); see docs/backup-restore.md.

Usage (from the repo root)::

    uv run python scripts/backup.py backup            # both databases
    uv run python scripts/backup.py backup --target hesabyar
    uv run python scripts/backup.py drill             # newest pair, scratch DBs
    uv run python scripts/backup.py drill --keep-scratch
    uv run python scripts/backup.py list              # newest manifest per target
    uv run python scripts/backup.py list --all
    uv run python scripts/backup.py prune --dry-run   # show what pruning would delete
    uv run python scripts/backup.py prune             # delete expired backups
    uv run python scripts/backup.py gen-key           # print a fresh Fernet key

Exit codes: 0 ok, 1 the operation failed (details on stderr), 2 usage
error. Nothing here ever prints a secret: DSNs, keys and S3
credentials stay out of stdout/stderr (only database NAMES and
object keys are printed).
"""

from __future__ import annotations

import argparse
import logging
import sys
from contextlib import contextmanager
from typing import Iterator

from hesabyar.domain.backup.errors import BackupError
from hesabyar.domain.backup.models import BackupTarget
from hesabyar.infrastructure.accounting.tryton.adapter import TrytonAccountingAdapter
from hesabyar.infrastructure.accounting.tryton.client import TrytonRestClient
from hesabyar.infrastructure.auth.credential_vault import generate_encryption_key
from hesabyar.infrastructure.config import AppSettings
from hesabyar.infrastructure.operations.factories import (
    build_backup_service,
    build_drill_service,
)
from hesabyar.infrastructure.operations.pg_tools import PgDumpRestoreRunner
from hesabyar.infrastructure.operations.restore_drill import ScratchEngineFactory

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

TARGETS = {
    "hesabyar": BackupTarget.HESABYAR_DB,
    "tryton": BackupTarget.TRYTON_DB,
}


def _dumper(settings: AppSettings) -> PgDumpRestoreRunner:
    backup = settings.backup
    return PgDumpRestoreRunner(
        pg_dump_path=backup.pg_dump_path,
        pg_restore_path=backup.pg_restore_path,
        timeout_seconds=backup.command_timeout_seconds,
    )


def _hesabyar_url(settings: AppSettings) -> str:
    return settings.backup.hesabyar_database_url or settings.database.url


def _engine_factory(settings: AppSettings) -> ScratchEngineFactory:
    tryton = settings.tryton

    @contextmanager
    def factory(database: str) -> Iterator[TrytonAccountingAdapter]:
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

    return factory


def _cmd_backup(settings: AppSettings, target: str | None) -> int:
    service = build_backup_service(settings.backup, _dumper(settings))
    if target is None:
        report = service.run_all(_hesabyar_url(settings), settings.backup.tryton_database_url)
        for t, manifest in sorted(report.manifests.items(), key=lambda item: item[0].value):
            print(f"OK   {t.value}: {manifest.object_key} ({manifest.size_bytes} bytes)")
        for t, error in report.failures.items():
            print(f"FAIL {t.value}: {error}", file=sys.stderr)
        if report.pruned_keys:
            print(f"pruned {len(report.pruned_keys)} expired backup(s)")
        return 0 if report.ok else 1
    t = TARGETS[target]
    url = _hesabyar_url(settings) if t is BackupTarget.HESABYAR_DB else settings.backup.tryton_database_url
    if url is None:
        print(
            "No Tryton database URL configured (HESABYAR_BACKUP__TRYTON_DATABASE_URL).",
            file=sys.stderr,
        )
        return 1
    manifest = service.run_backup(t, url)
    print(f"OK   {t.value}: {manifest.object_key} ({manifest.size_bytes} bytes)")
    pruned = service.prune(t)
    if pruned:
        print(f"pruned {len(pruned)} expired backup(s)")
    return 0


def _cmd_drill(settings: AppSettings, *, keep_scratch: bool, target: str | None) -> int:
    service = build_drill_service(
        settings.backup,
        _dumper(settings),
        _engine_factory(settings),
        hesabyar_database_url=_hesabyar_url(settings),
    )
    targets = (TARGETS[target],) if target else (BackupTarget.HESABYAR_DB, BackupTarget.TRYTON_DB)
    manifests = {}
    for t in targets:
        manifest = service.latest_manifest(t)
        if manifest is None:
            print(f"No backup found for {t.value} — run a backup first.", file=sys.stderr)
            return 1
        manifests[t] = manifest
        print(f"drilling {t.value}: {manifest.object_key}")
    report = service.run(manifests, targets=targets, keep_scratch=keep_scratch)
    for check in report.checks:
        print(f"{check.status.value.upper():7} {check.name}: {check.detail}")
    print(f"passed={report.passed} fully_verified={report.fully_verified}")
    if report.cleanup_errors:
        for error in report.cleanup_errors:
            print(f"cleanup error: {error}", file=sys.stderr)
    if keep_scratch:
        print("scratch databases kept: " + ", ".join(report.scratch_databases))
    return 0 if report.passed else 1


def _cmd_list(settings: AppSettings, *, all_manifests: bool) -> int:
    service = build_backup_service(settings.backup, _dumper(settings))
    for target in (BackupTarget.HESABYAR_DB, BackupTarget.TRYTON_DB):
        manifests = service.list_manifests(target)
        if not manifests:
            print(f"{target.value}: no backups")
            continue
        shown = manifests if all_manifests else manifests[:1]
        for manifest in shown:
            print(
                f"{target.value}: {manifest.object_key} "
                f"({manifest.size_bytes} bytes, created {manifest.created_at.isoformat()}, "
                f"key {manifest.encryption_key_id}, "
                f"retention expires {manifest.retention_expires_at.date().isoformat()})"
            )
    return 0


def _cmd_prune(settings: AppSettings, *, dry_run: bool) -> int:
    service = build_backup_service(settings.backup, _dumper(settings))
    deleted = 0
    for target in (BackupTarget.HESABYAR_DB, BackupTarget.TRYTON_DB):
        if dry_run:
            from hesabyar.domain.backup.models import RetentionPolicy

            policy = RetentionPolicy(
                daily_retention_days=settings.backup.retention_days,
                monthly_keep_count=settings.backup.monthly_keep_count,
            )
            import datetime

            expired = policy.select_expired(
                service.list_manifests(target), now=datetime.datetime.now(datetime.UTC)
            )
            for manifest in expired:
                print(f"would delete {manifest.object_key}")
            deleted += len(expired)
        else:
            keys = service.prune(target)
            for key in keys:
                print(f"deleted {key}")
            deleted += len(keys)
    print(f"{'would delete' if dry_run else 'deleted'} {deleted} backup(s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HesabYar backup + restore-drill CLI (US-703).")
    sub = parser.add_subparsers(dest="command", required=True)

    backup_parser = sub.add_parser("backup", help="Back up both databases (or one with --target).")
    backup_parser.add_argument("--target", choices=sorted(TARGETS), default=None)

    drill_parser = sub.add_parser("drill", help="Restore-drill the newest backup pair.")
    drill_parser.add_argument("--keep-scratch", action="store_true",
                              help="Keep the scratch databases for debugging.")
    drill_parser.add_argument("--target", choices=sorted(TARGETS), default=None,
                              help="Drill one target only (the cross-database check is skipped).")

    list_parser = sub.add_parser("list", help="List backups (newest per target).")
    list_parser.add_argument("--all", action="store_true", dest="all_manifests",
                             help="List every manifest, not just the newest per target.")

    prune_parser = sub.add_parser("prune", help="Delete expired backups per the retention policy.")
    prune_parser.add_argument("--dry-run", action="store_true",
                              help="Show what would be deleted without deleting it.")

    sub.add_parser("gen-key", help="Print a fresh Fernet backup-encryption key.")

    args = parser.parse_args(argv)
    if args.command == "gen-key":
        print(generate_encryption_key())
        return 0
    try:
        settings = AppSettings()
        if args.command == "backup":
            return _cmd_backup(settings, args.target)
        if args.command == "drill":
            return _cmd_drill(settings, keep_scratch=args.keep_scratch, target=args.target)
        if args.command == "list":
            return _cmd_list(settings, all_manifests=args.all_manifests)
        if args.command == "prune":
            return _cmd_prune(settings, dry_run=args.dry_run)
    except BackupError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
