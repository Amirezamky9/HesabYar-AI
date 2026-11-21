"""Unit tests for the backup service (US-703).

Fake dumper + in-memory store + the real Fernet cipher: these tests
pin the orchestration — manifest contents, checksum recording,
per-target failure isolation, pruning, manifest listing, and the
download-verify-decrypt read path the drill depends on.
"""

from __future__ import annotations

import datetime
import hashlib
import json

import pytest

from hesabyar.domain.backup.errors import (
    BackupConfigurationError,
    BackupIntegrityError,
    BackupToolError,
    ObjectStoreError,
)
from hesabyar.domain.backup.models import (
    BackupTarget,
    RetentionPolicy,
    manifest_key_for,
)
from hesabyar.domain.backup.ports import DumpResult, PgToolsInfo
from hesabyar.infrastructure.auth.credential_vault import generate_encryption_key
from hesabyar.infrastructure.operations.backup_cipher import FernetBackupCipher
from hesabyar.infrastructure.operations.backup_service import BackupService

NOW = datetime.datetime(2026, 11, 21, 22, 30, tzinfo=datetime.UTC)
HESABYAR_URL = "postgresql://hesabyar:pw@db:5432/hesabyar_test"
TRYTON_URL = "postgresql://tryton:pw@db:5432/hesabyar_tryton_test"


class FakeStore:
    """In-memory object store."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_object(self, key: str, data: bytes, *, content_type: str) -> None:
        self.objects[key] = data

    def get_object(self, key: str) -> bytes:
        try:
            return self.objects[key]
        except KeyError as exc:
            raise ObjectStoreError(f"missing {key}", not_found=True) from exc

    def list_objects(self, prefix: str) -> list[str]:
        return sorted(k for k in self.objects if k.startswith(prefix))

    def delete_object(self, key: str) -> None:
        self.objects.pop(key, None)


class FakeDumper:
    """Returns fixed dump bytes per database; optionally fails one target."""

    def __init__(self, *, fail_tryton: bool = False) -> None:
        self.dump_calls: list[str] = []
        self.fail_tryton = fail_tryton

    def check_tools(self) -> PgToolsInfo:
        return PgToolsInfo(
            pg_dump_path="pg_dump",
            pg_dump_version="16.4",
            pg_restore_path="pg_restore",
            pg_restore_version="16.4",
        )

    def dump(self, database_url: str) -> DumpResult:
        self.dump_calls.append(database_url)
        if self.fail_tryton and "tryton" in database_url:
            raise BackupToolError("pg_dump failed for the Tryton database")
        database = database_url.rsplit("/", 1)[-1]
        return DumpResult(
            data=f"pg_dump of {database}".encode(),
            pg_dump_version="16.4",
            database=database,
        )

    def restore(self, database_url: str, data: bytes, *, no_owner: bool) -> None:
        raise NotImplementedError

    def create_database(self, admin_url: str, name: str) -> None:
        raise NotImplementedError

    def drop_database(self, admin_url: str, name: str) -> None:
        raise NotImplementedError


def _service(
    store: FakeStore,
    dumper: FakeDumper,
    *,
    max_dump_bytes: int = 512 * 1024 * 1024,
) -> BackupService:
    return BackupService(
        store=store,  # type: ignore[arg-type]
        cipher=FernetBackupCipher(generate_encryption_key()),
        dumper=dumper,  # type: ignore[arg-type]
        retention=RetentionPolicy(daily_retention_days=30, monthly_keep_count=12),
        max_dump_bytes=max_dump_bytes,
        clock=lambda: NOW,
    )


# ---------------------------------------------------------------------------
# run_backup / run_all
# ---------------------------------------------------------------------------


def test_run_backup_uploads_object_and_manifest() -> None:
    store = FakeStore()
    service = _service(store, FakeDumper())
    manifest = service.run_backup(BackupTarget.HESABYAR_DB, HESABYAR_URL)
    assert manifest.target is BackupTarget.HESABYAR_DB
    assert manifest.database == "hesabyar_test"
    assert manifest.object_key == (
        "hesabyar-backups/hesabyar/2026-11-21/223000-hesabyar.dump.enc"
    )
    assert manifest.created_at == NOW
    assert manifest.retention_expires_at == NOW + datetime.timedelta(days=30)
    assert manifest.pg_dump_version == "16.4"
    assert manifest.plaintext_sha256 == hashlib.sha256(b"pg_dump of hesabyar_test").hexdigest()
    assert manifest.plaintext_size_bytes == len(b"pg_dump of hesabyar_test")
    assert manifest.sha256 == hashlib.sha256(store.objects[manifest.object_key]).hexdigest()
    stored_manifest = json.loads(store.objects[manifest_key_for(manifest.object_key)].decode())
    assert stored_manifest["object_key"] == manifest.object_key
    assert stored_manifest["encryption_key_id"] == manifest.encryption_key_id


def test_run_all_backs_up_both_databases() -> None:
    store = FakeStore()
    dumper = FakeDumper()
    service = _service(store, dumper)
    report = service.run_all(HESABYAR_URL, TRYTON_URL)
    assert report.ok is True
    assert set(report.manifests) == {BackupTarget.HESABYAR_DB, BackupTarget.TRYTON_DB}
    assert dumper.dump_calls == [HESABYAR_URL, TRYTON_URL]


def test_run_all_isolates_a_failing_target() -> None:
    store = FakeStore()
    service = _service(store, FakeDumper(fail_tryton=True))
    report = service.run_all(HESABYAR_URL, TRYTON_URL)
    assert report.ok is False
    assert set(report.manifests) == {BackupTarget.HESABYAR_DB}
    assert "pg_dump failed" in report.failures[BackupTarget.TRYTON_DB]


def test_run_all_without_a_tryton_url_fails_that_target() -> None:
    store = FakeStore()
    service = _service(store, FakeDumper())
    report = service.run_all(HESABYAR_URL, None)
    assert report.ok is False
    assert set(report.manifests) == {BackupTarget.HESABYAR_DB}
    assert "TRYTON_DATABASE_URL" in report.failures[BackupTarget.TRYTON_DB]


def test_oversized_dump_fails_closed() -> None:
    store = FakeStore()
    service = _service(store, FakeDumper(), max_dump_bytes=10)
    with pytest.raises(BackupConfigurationError, match="over the 10-byte limit"):
        service.run_backup(BackupTarget.HESABYAR_DB, HESABYAR_URL)
    assert store.objects == {}


# ---------------------------------------------------------------------------
# manifests + pruning
# ---------------------------------------------------------------------------


def _seed_backup(
    service: BackupService,
    target: BackupTarget,
    url: str,
    *,
    now: datetime.datetime,
) -> None:
    service.run_backup(target, url, now=now)


def test_latest_manifest_and_pruning() -> None:
    store = FakeStore()
    service = _service(store, FakeDumper())
    # Three HesabYar backups: two recent, one 40 days old (expired,
    # and the only backup in its month — but monthly keep saves the
    # newest per month for the 12 newest months, so it survives).
    _seed_backup(service, BackupTarget.HESABYAR_DB, HESABYAR_URL, now=NOW - datetime.timedelta(days=40))
    _seed_backup(service, BackupTarget.HESABYAR_DB, HESABYAR_URL, now=NOW - datetime.timedelta(days=1))
    newest = service.run_backup(BackupTarget.HESABYAR_DB, HESABYAR_URL, now=NOW)
    assert service.latest_manifest(BackupTarget.HESABYAR_DB) == newest
    assert service.latest_manifest(BackupTarget.TRYTON_DB) is None
    assert len(service.list_manifests(BackupTarget.HESABYAR_DB)) == 3
    # Nothing is prunable yet (the 40-day-old backup is its month's newest).
    assert service.prune(BackupTarget.HESABYAR_DB) == []
    # A second backup in the same old month makes the older one prunable.
    _seed_backup(service, BackupTarget.HESABYAR_DB, HESABYAR_URL, now=NOW - datetime.timedelta(days=39))
    pruned = service.prune(BackupTarget.HESABYAR_DB)
    assert len(pruned) == 1
    assert pruned[0].endswith("hesabyar.dump.enc")
    assert pruned[0] not in store.objects
    assert manifest_key_for(pruned[0]) not in store.objects


def test_list_manifests_skips_unparseable_ones() -> None:
    store = FakeStore()
    service = _service(store, FakeDumper())
    manifest = service.run_backup(BackupTarget.HESABYAR_DB, HESABYAR_URL)
    store.put_object(
        "hesabyar-backups/hesabyar/2026-11-21/broken.dump.enc.manifest.json",
        b"{not json",
        content_type="application/json",
    )
    assert service.list_manifests(BackupTarget.HESABYAR_DB) == [manifest]
    # Pruning never deletes what it cannot parse.
    assert service.prune(BackupTarget.HESABYAR_DB) == []
    assert "hesabyar-backups/hesabyar/2026-11-21/broken.dump.enc.manifest.json" in store.objects


# ---------------------------------------------------------------------------
# download + verify
# ---------------------------------------------------------------------------


def test_download_backup_round_trip() -> None:
    store = FakeStore()
    service = _service(store, FakeDumper())
    manifest = service.run_backup(BackupTarget.HESABYAR_DB, HESABYAR_URL)
    assert service.download_backup(manifest) == b"pg_dump of hesabyar_test"


def test_download_backup_rejects_tampered_bytes() -> None:
    store = FakeStore()
    service = _service(store, FakeDumper())
    manifest = service.run_backup(BackupTarget.HESABYAR_DB, HESABYAR_URL)
    tampered = bytearray(store.objects[manifest.object_key])
    tampered[0] ^= 0xFF
    store.objects[manifest.object_key] = bytes(tampered)
    with pytest.raises(BackupIntegrityError, match="Checksum mismatch"):
        service.download_backup(manifest)


def test_download_backup_rejects_a_wrong_key() -> None:
    store = FakeStore()
    service = _service(store, FakeDumper())
    manifest = service.run_backup(BackupTarget.HESABYAR_DB, HESABYAR_URL)
    other_service = _service(store, FakeDumper())  # a different generated key
    with pytest.raises(BackupIntegrityError, match="Cannot decrypt"):
        other_service.download_backup(manifest)
