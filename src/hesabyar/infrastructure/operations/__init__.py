"""Operations layer: backups, restore drills, and other operator procedures.

Code here is OPERATIONS, not application logic: it shells out to
pg_dump/pg_restore and administers scratch databases. It is the one
place that may touch the Tryton database directly (whole-database
dump/restore only — never Tryton table reads; see
docs/backup-restore.md for the spec §19.2 operational exception).
"""

from hesabyar.infrastructure.operations.backup_cipher import FernetBackupCipher
from hesabyar.infrastructure.operations.backup_service import BackupService
from hesabyar.infrastructure.operations.factories import (
    build_backup_cipher,
    build_backup_service,
    build_drill_service,
)
from hesabyar.infrastructure.operations.object_store import (
    FilesystemObjectStore,
    S3CompatibleObjectStore,
    build_object_store,
)
from hesabyar.infrastructure.operations.pg_tools import (
    PgDumpRestoreRunner,
    maintenance_url,
    with_database_name,
)
from hesabyar.infrastructure.operations.restore_drill import (
    CHECK_EXTERNAL_REFS,
    CHECK_MIGRATIONS,
    CHECK_ORPHANS,
    CHECK_TRANCHES,
    CHECK_TRIAL_BALANCE,
    RestoreDrillService,
    ScratchEngineFactory,
    check_alembic,
    check_external_references,
    check_orphaned_documents,
    check_settlement_tranches,
    check_trial_balance,
)

__all__ = [
    "CHECK_EXTERNAL_REFS",
    "CHECK_MIGRATIONS",
    "CHECK_ORPHANS",
    "CHECK_TRANCHES",
    "CHECK_TRIAL_BALANCE",
    "BackupService",
    "FernetBackupCipher",
    "FilesystemObjectStore",
    "PgDumpRestoreRunner",
    "RestoreDrillService",
    "S3CompatibleObjectStore",
    "ScratchEngineFactory",
    "build_backup_cipher",
    "build_backup_service",
    "build_drill_service",
    "build_object_store",
    "check_alembic",
    "check_external_references",
    "check_orphaned_documents",
    "check_settlement_tranches",
    "check_trial_balance",
    "maintenance_url",
    "with_database_name",
]
