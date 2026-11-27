"""Domain errors for backups + restore drills (US-703, spec §15.4 / §17 phase 7).

Every failure in the backup path is typed and fail-closed: a backup
that cannot be configured, dumped, encrypted, uploaded, checksummed
or restored raises instead of silently producing a backup that looks
fine and restores to nothing. Error messages never carry credentials
or connection strings — see ``pg_tools._redact`` for the one place a
password could leak (pg_dump stderr) and how it is scrubbed.
"""

from __future__ import annotations

from typing import Any

from hesabyar.domain.common.errors import HesabYarError

BACKUP_ERROR = "BACKUP_ERROR"
BACKUP_NOT_CONFIGURED = "BACKUP_NOT_CONFIGURED"
BACKUP_TOOL_ERROR = "BACKUP_TOOL_ERROR"
BACKUP_INTEGRITY_ERROR = "BACKUP_INTEGRITY_ERROR"
OBJECT_STORE_ERROR = "OBJECT_STORE_ERROR"
RESTORE_DRILL_FAILED = "RESTORE_DRILL_FAILED"


class BackupError(HesabYarError):
    """Base error for the backup/restore-drill domain."""

    def __init__(
        self,
        message: str,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, code=code or BACKUP_ERROR, details=details)


class BackupConfigurationError(BackupError):
    """The backup is not configured (no destination, no key, no DSN).

    Raised at run time — never at import or worker start — so a worker
    without backup configuration still starts and the daily job fails
    loudly (recorded + review item) instead of the whole worker
    refusing to boot.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=BACKUP_NOT_CONFIGURED, details=details)


class BackupToolError(BackupError):
    """pg_dump / pg_restore is missing or failed (incl. version mismatch)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=BACKUP_TOOL_ERROR, details=details)


class BackupIntegrityError(BackupError):
    """A backup failed its integrity check (checksum or decryption).

    A checksum mismatch means the stored object is not the bytes that
    were uploaded; a decryption failure means the configured key is
    not the key that encrypted it (or the ciphertext was tampered
    with). Both fail the drill — a backup that cannot be verified
    cannot be trusted, per spec §15.4 ("a backup is not considered
    valid until it has been restored and checked").
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=BACKUP_INTEGRITY_ERROR, details=details)


class ObjectStoreError(BackupError):
    """The object store (S3-compatible or local) rejected an operation."""

    def __init__(
        self,
        message: str,
        details: dict[str, Any] | None = None,
        *,
        not_found: bool = False,
    ) -> None:
        super().__init__(message, code=OBJECT_STORE_ERROR, details=details)
        self.not_found = not_found


class DrillError(BackupError):
    """The restore drill failed (a check failed, or the drill aborted)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=RESTORE_DRILL_FAILED, details=details)
