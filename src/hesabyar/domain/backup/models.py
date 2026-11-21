"""Backup + restore-drill domain models (US-703, spec §15.4 / §17 phase 7).

The manifest is the durable record of a backup (spec §15.4: "record
backup completion, object checksum, retention expiry and encryption
key reference"). It travels WITH the backup as ``<key>.manifest.json``
in the object store, so a backup is self-describing even if the
HesabYar database that scheduled it is gone — the runbook's restore
procedure reads the manifest, not the (possibly lost) job registry.

No credentials live here: the manifest records the database NAME
only, never a host or a connection string, and the encryption key
only by its ``key_id`` (the first 8 hex of the key's SHA-256, the
same convention as the Fernet credential vault).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from enum import StrEnum, unique
from typing import Any

from hesabyar.domain.backup.errors import BackupError
from hesabyar.domain.common.dates import FiscalDate

#: Manifest format version. Bump when ``to_dict`` / ``from_dict``
#: change shape; the drill refuses manifests it cannot parse.
MANIFEST_VERSION = 1

#: Suffix of a manifest object relative to its backup object.
MANIFEST_SUFFIX = ".manifest.json"


@unique
class BackupTarget(StrEnum):
    """Which database a backup (or drill step) covers."""

    HESABYAR_DB = "hesabyar"
    TRYTON_DB = "tryton"


def object_key_for(
    key_prefix: str, target: BackupTarget, now: datetime.datetime
) -> str:
    """The object-store key for a backup taken at ``now`` (UTC).

    ``<prefix>/<target>/YYYY-MM-DD/HHMMSS-<target>.dump.enc`` —
    date-partitioned so a prefix listing per target per day is cheap,
    and lexicographically sortable within a day. The ``.dump.enc``
    suffix says what the bytes are: a pg_dump custom-format archive,
    Fernet-encrypted.
    """
    moment = now.astimezone(datetime.UTC)
    return (
        f"{key_prefix}/{target.value}/{moment:%Y-%m-%d}/"
        f"{moment:%H%M%S}-{target.value}.dump.enc"
    )


def manifest_key_for(object_key: str) -> str:
    """The manifest key for a backup object key."""
    return object_key + MANIFEST_SUFFIX


@dataclass(frozen=True)
class BackupManifest:
    """The durable record of one backup (see the module docstring)."""

    target: BackupTarget
    object_key: str
    database: str  # database NAME only — never a host or DSN
    created_at: datetime.datetime  # aware UTC
    sha256: str  # hex SHA-256 of the STORED (ciphertext) bytes
    plaintext_sha256: str  # hex SHA-256 of the pg_dump bytes
    size_bytes: int  # stored (ciphertext) size
    plaintext_size_bytes: int  # pg_dump size
    encryption_key_id: str
    retention_expires_at: datetime.datetime  # aware UTC
    pg_dump_version: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": MANIFEST_VERSION,
            "target": self.target.value,
            "object_key": self.object_key,
            "database": self.database,
            "created_at": self.created_at.isoformat(),
            "sha256": self.sha256,
            "plaintext_sha256": self.plaintext_sha256,
            "size_bytes": self.size_bytes,
            "plaintext_size_bytes": self.plaintext_size_bytes,
            "encryption_key_id": self.encryption_key_id,
            "retention_expires_at": self.retention_expires_at.isoformat(),
            "pg_dump_version": self.pg_dump_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BackupManifest:
        """Parse a manifest dict (fail closed on bad input)."""
        if not isinstance(data, dict):
            raise BackupError(f"A backup manifest must be an object, got {type(data).__name__}.")
        if data.get("version") != MANIFEST_VERSION:
            raise BackupError(
                f"Unsupported backup manifest version {data.get('version')!r}; "
                f"expected {MANIFEST_VERSION}.",
                details={"version": data.get("version")},
            )
        try:
            target = BackupTarget(str(data["target"]))
            created_at = datetime.datetime.fromisoformat(str(data["created_at"]))
            retention_expires_at = datetime.datetime.fromisoformat(
                str(data["retention_expires_at"])
            )
            size_bytes = int(data["size_bytes"])  # type: ignore[arg-type]
            plaintext_size_bytes = int(data["plaintext_size_bytes"])  # type: ignore[arg-type]
        except (KeyError, ValueError, TypeError) as exc:
            raise BackupError(
                f"Malformed backup manifest: {exc}",
                details={"object_key": data.get("object_key")},
            ) from exc
        if created_at.tzinfo is None or retention_expires_at.tzinfo is None:
            raise BackupError(
                "Backup manifest timestamps must carry a timezone.",
                details={"object_key": data.get("object_key")},
            )
        for field_name in (
            "object_key",
            "database",
            "sha256",
            "plaintext_sha256",
            "encryption_key_id",
            "pg_dump_version",
        ):
            value = data.get(field_name)
            if not isinstance(value, str) or not value:
                raise BackupError(
                    f"Backup manifest field {field_name!r} must be a non-empty string.",
                    details={"object_key": data.get("object_key")},
                )
        if size_bytes < 0 or plaintext_size_bytes < 0:
            raise BackupError(
                "Backup manifest sizes must be >= 0.",
                details={"object_key": data.get("object_key")},
            )
        return cls(
            target=target,
            object_key=str(data["object_key"]),
            database=str(data["database"]),
            created_at=created_at,
            sha256=str(data["sha256"]),
            plaintext_sha256=str(data["plaintext_sha256"]),
            size_bytes=size_bytes,
            plaintext_size_bytes=plaintext_size_bytes,
            encryption_key_id=str(data["encryption_key_id"]),
            retention_expires_at=retention_expires_at,
            pg_dump_version=str(data["pg_dump_version"]),
        )


@dataclass(frozen=True)
class RetentionPolicy:
    """Which backups pruning may delete (pure: no I/O, unit-testable).

    - Every backup younger than ``daily_retention_days`` is kept.
    - Of the older backups, the NEWEST backup of each Jalali month is
      kept for the ``monthly_keep_count`` most recent months (a
      monthly restore point survives the daily window).
    - Everything else is expired.

    The monthly grouping uses the backup's Jalali month (the
    installation's calendar), consistent with the scheduler's
    Jalali-aware schedules. ``monthly_keep_count=0`` keeps no
    monthly restore points.
    """

    daily_retention_days: int = 30
    monthly_keep_count: int = 12

    def select_expired(
        self, manifests: list[BackupManifest], *, now: datetime.datetime
    ) -> list[BackupManifest]:
        """The manifests pruning may delete, sorted by object key."""
        moment = now.astimezone(datetime.UTC)
        cutoff = moment - datetime.timedelta(days=self.daily_retention_days)
        old = [m for m in manifests if m.created_at <= cutoff]
        by_month: dict[tuple[int, int], list[BackupManifest]] = {}
        for manifest in old:
            fiscal = FiscalDate(manifest.created_at.date())
            by_month.setdefault((fiscal.jalali_year, fiscal.jalali_month), []).append(manifest)
        keep_months = set(sorted(by_month, reverse=True)[: self.monthly_keep_count])
        expired: list[BackupManifest] = []
        for month, group in by_month.items():
            newest_first = sorted(
                group, key=lambda m: (m.created_at, m.object_key), reverse=True
            )
            if month in keep_months:
                expired.extend(newest_first[1:])  # keep the month's newest
            else:
                expired.extend(newest_first)
        return sorted(expired, key=lambda m: m.object_key)


@unique
class DrillCheckStatus(StrEnum):
    """The outcome of one restore-drill check."""

    PASS = "pass"
    FAIL = "fail"
    SKIPPED = "skipped"  # prerequisite missing — reported, never silent


@dataclass(frozen=True)
class DrillCheckResult:
    """One check of a restore drill: a stable name + outcome + detail."""

    name: str
    status: DrillCheckStatus
    detail: str = ""


@dataclass(frozen=True)
class DrillReport:
    """The outcome of one restore drill (the run record + the CLI print it).

    ``passed`` means no check FAILED — the pair restored and
    reconciled. ``fully_verified`` additionally requires no check
    SKIPPED: a drill that skipped the trial-balance check (no scratch
    engine available) passed what it ran, but did not fully verify
    the backup, and the report says so plainly instead of calling it
    a clean pass.
    """

    started_at: datetime.datetime  # aware UTC
    finished_at: datetime.datetime  # aware UTC
    checks: tuple[DrillCheckResult, ...] = ()
    hesabyar_manifest: BackupManifest | None = None
    tryton_manifest: BackupManifest | None = None
    scratch_databases: tuple[str, ...] = ()
    cleanup_errors: tuple[str, ...] = ()
    keep_scratch: bool = False

    @property
    def passed(self) -> bool:
        return all(c.status is not DrillCheckStatus.FAIL for c in self.checks)

    @property
    def fully_verified(self) -> bool:
        return self.passed and all(
            c.status is DrillCheckStatus.PASS for c in self.checks
        )

    def failed_checks(self) -> tuple[DrillCheckResult, ...]:
        return tuple(c for c in self.checks if c.status is DrillCheckStatus.FAIL)

    def skipped_checks(self) -> tuple[DrillCheckResult, ...]:
        return tuple(c for c in self.checks if c.status is DrillCheckStatus.SKIPPED)

    def summary(self) -> dict[str, Any]:
        """JSON-safe summary for the job run record (no secrets)."""
        return {
            "passed": self.passed,
            "fully_verified": self.fully_verified,
            "checks": {c.name: c.status.value for c in self.checks},
            "failed": [c.name for c in self.failed_checks()],
            "skipped": [c.name for c in self.skipped_checks()],
            "hesabyar_backup": (
                self.hesabyar_manifest.object_key if self.hesabyar_manifest else None
            ),
            "tryton_backup": (
                self.tryton_manifest.object_key if self.tryton_manifest else None
            ),
            "scratch_databases": list(self.scratch_databases),
            "scratch_kept": self.keep_scratch,
            "cleanup_errors": list(self.cleanup_errors),
            "duration_seconds": round(
                (self.finished_at - self.started_at).total_seconds(), 1
            ),
        }


@dataclass(frozen=True)
class BackupRunReport:
    """The outcome of one backup run over both databases (per-target results).

    The two databases are independent restore points (spec §15.4), so
    one target failing never blocks the other — but a run with any
    failure is NOT ok: the pair is the unit the drill verifies, and
    the scheduler job fails (recorded + review item) until both
    targets back up again.
    """

    manifests: dict[BackupTarget, BackupManifest] = field(default_factory=dict)
    failures: dict[BackupTarget, str] = field(default_factory=dict)
    pruned_keys: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failures

    def summary(self) -> dict[str, Any]:
        """JSON-safe summary for the job run record (no secrets)."""
        return {
            "ok": self.ok,
            "backups": {
                target.value: {
                    "object_key": manifest.object_key,
                    "sha256": manifest.sha256,
                    "size_bytes": manifest.size_bytes,
                    "encryption_key_id": manifest.encryption_key_id,
                    "retention_expires_at": manifest.retention_expires_at.isoformat(),
                }
                for target, manifest in sorted(
                    self.manifests.items(), key=lambda item: item[0].value
                )
            },
            "failures": {target.value: error for target, error in self.failures.items()},
            "pruned_keys": list(self.pruned_keys),
        }
