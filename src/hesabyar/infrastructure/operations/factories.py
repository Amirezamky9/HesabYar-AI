"""Build backup/drill services from settings (US-703).

One place constructs the operations services — the worker factory,
the scheduler tasks and the operator CLI all go through here, so a
settings change has exactly one construction site. Construction is
cheap and LAZY about credentials: the queue tasks build a fresh
service per run (a rotated key or endpoint takes effect without a
worker restart), and a missing key/destination raises
``BackupConfigurationError`` at run time — never at worker start.
"""

from __future__ import annotations

from hesabyar.domain.backup.errors import BackupConfigurationError
from hesabyar.domain.backup.models import RetentionPolicy
from hesabyar.domain.backup.ports import DatabaseDumperPort
from hesabyar.infrastructure.config import BackupSettings
from hesabyar.infrastructure.operations.backup_cipher import FernetBackupCipher
from hesabyar.infrastructure.operations.backup_service import BackupService
from hesabyar.infrastructure.operations.object_store import build_object_store
from hesabyar.infrastructure.operations.pg_tools import maintenance_url
from hesabyar.infrastructure.operations.restore_drill import (
    RestoreDrillService,
    ScratchEngineFactory,
)


def build_backup_cipher(settings: BackupSettings) -> FernetBackupCipher:
    """The backup cipher for the configured key (fail closed when unset)."""
    if not settings.encryption_key:
        raise BackupConfigurationError(
            "No backup encryption key is configured "
            "(HESABYAR_BACKUP__ENCRYPTION_KEY); refusing to write "
            "unencrypted backups."
        )
    return FernetBackupCipher(settings.encryption_key)


def build_backup_service(
    settings: BackupSettings, dumper: DatabaseDumperPort
) -> BackupService:
    """The backup service for the configured destination + key."""
    return BackupService(
        store=build_object_store(settings),
        cipher=build_backup_cipher(settings),
        dumper=dumper,
        retention=RetentionPolicy(
            daily_retention_days=settings.retention_days,
            monthly_keep_count=settings.monthly_keep_count,
        ),
        key_prefix=settings.key_prefix,
        max_dump_bytes=settings.max_dump_bytes,
    )


def build_drill_service(
    settings: BackupSettings,
    dumper: DatabaseDumperPort,
    engine_factory: ScratchEngineFactory | None,
    *,
    hesabyar_database_url: str,
) -> RestoreDrillService:
    """The restore-drill service for the configured destination + key.

    ``hesabyar_database_url`` is the RESOLVED HesabYar DSN (the
    backup setting, or the application database URL — the caller
    resolves it, because ``BackupSettings`` alone cannot see the
    application database URL). A missing Tryton DSN does NOT raise
    here: the drill service records the Tryton target as failed at
    run time, so a HesabYar-only drill still works.
    """
    return RestoreDrillService(
        store=build_object_store(settings),
        cipher=build_backup_cipher(settings),
        dumper=dumper,
        hesabyar_admin_url=settings.hesabyar_admin_database_url
        or maintenance_url(hesabyar_database_url),
        tryton_admin_url=(
            settings.tryton_admin_database_url
            or (
                maintenance_url(settings.tryton_database_url)
                if settings.tryton_database_url is not None
                else None
            )
        ),
        engine_factory=engine_factory,
        max_external_refs_checked=settings.max_external_refs_checked,
    )
