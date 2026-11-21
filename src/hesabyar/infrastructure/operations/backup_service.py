"""Backup orchestration: dump -> encrypt -> upload -> manifest -> prune (US-703).

One :class:`BackupService` backs up BOTH databases (spec §15.4: "use
PostgreSQL-native consistent backups for both databases"). The flow
per target:

1. ``pg_dump -Fc`` (via the injected dumper — no SQL here, and no
   Tryton table access anywhere in this module).
2. Size guard (Fernet encrypts whole buffers; a dump over
   ``max_dump_bytes`` fails closed instead of exhausting memory).
3. SHA-256 of the plaintext dump, Fernet-encrypt, SHA-256 of the
   ciphertext.
4. Upload the ciphertext + its manifest (the manifest is the
   durable record: checksums, retention expiry, encryption key id).
5. Prune expired backups for the target (a pruning failure is
   logged + reported, never fatal — old backups piling up costs
   money, it does not lose data).

The two targets are independent: one failing never blocks the other
(the report carries per-target failures, and the scheduler job fails
until BOTH back up again — the pair is the unit the drill verifies).
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
from collections.abc import Callable

from hesabyar.domain.backup.errors import (
    BackupConfigurationError,
    BackupError,
    BackupIntegrityError,
)
from hesabyar.domain.backup.models import (
    MANIFEST_SUFFIX,
    BackupManifest,
    BackupRunReport,
    BackupTarget,
    RetentionPolicy,
    manifest_key_for,
    object_key_for,
)
from hesabyar.domain.backup.ports import (
    BackupCipherPort,
    DatabaseDumperPort,
    ObjectStorePort,
)

logger = logging.getLogger(__name__)

#: Content type for the encrypted dump objects.
_DUMP_CONTENT_TYPE = "application/octet-stream"

#: Content type for the manifest objects.
_MANIFEST_CONTENT_TYPE = "application/json"


def sha256_hex(data: bytes) -> str:
    """Hex SHA-256 of ``data``."""
    return hashlib.sha256(data).hexdigest()


class BackupService:
    """Dump + encrypt + upload + prune for both databases."""

    def __init__(
        self,
        *,
        store: ObjectStorePort,
        cipher: BackupCipherPort,
        dumper: DatabaseDumperPort,
        retention: RetentionPolicy | None = None,
        key_prefix: str = "hesabyar-backups",
        max_dump_bytes: int = 512 * 1024 * 1024,
        clock: Callable[[], datetime.datetime] | None = None,
    ) -> None:
        self._store = store
        self._cipher = cipher
        self._dumper = dumper
        self._retention = retention or RetentionPolicy()
        self._key_prefix = key_prefix
        self._max_dump_bytes = max_dump_bytes
        self._clock = clock or (lambda: datetime.datetime.now(datetime.UTC))

    # -- one backup --------------------------------------------------------------

    def run_backup(
        self,
        target: BackupTarget,
        database_url: str,
        *,
        now: datetime.datetime | None = None,
    ) -> BackupManifest:
        """Back up one database; returns its manifest."""
        moment = (now or self._clock()).astimezone(datetime.UTC)
        dump = self._dumper.dump(database_url)
        if len(dump.data) > self._max_dump_bytes:
            raise BackupConfigurationError(
                f"The {target.value} dump is {len(dump.data)} bytes, over the "
                f"{self._max_dump_bytes}-byte limit (streaming encryption is "
                "not implemented yet — raise HESABYAR_BACKUP__MAX_DUMP_BYTES "
                "only if the worker has the memory for it)."
            )
        plaintext_sha256 = sha256_hex(dump.data)
        ciphertext = self._cipher.encrypt(dump.data)
        object_key = object_key_for(self._key_prefix, target, moment)
        manifest = BackupManifest(
            target=target,
            object_key=object_key,
            database=dump.database,
            created_at=moment,
            sha256=sha256_hex(ciphertext),
            plaintext_sha256=plaintext_sha256,
            size_bytes=len(ciphertext),
            plaintext_size_bytes=len(dump.data),
            encryption_key_id=self._cipher.key_id,
            retention_expires_at=moment
            + datetime.timedelta(days=self._retention.daily_retention_days),
            pg_dump_version=dump.pg_dump_version,
        )
        self._store.put_object(object_key, ciphertext, content_type=_DUMP_CONTENT_TYPE)
        self._store.put_object(
            manifest_key_for(object_key),
            json.dumps(manifest.to_dict(), indent=2, sort_keys=True).encode("utf-8"),
            content_type=_MANIFEST_CONTENT_TYPE,
        )
        logger.info(
            "Backup done: target=%s object=%s size=%d sha256=%s key_id=%s",
            target.value,
            object_key,
            manifest.size_bytes,
            manifest.sha256,
            manifest.encryption_key_id,
        )
        return manifest

    # -- both databases -----------------------------------------------------------

    def run_all(
        self,
        hesabyar_database_url: str,
        tryton_database_url: str | None,
        *,
        now: datetime.datetime | None = None,
    ) -> BackupRunReport:
        """Back up both databases; one target failing never blocks the other.

        ``tryton_database_url=None`` fails the Tryton target (recorded
        in the report) — a pair with only the HesabYar backup is not
        a passing run. Pruning runs per target after its backup and
        never fails the run (see the module docstring).
        """
        moment = (now or self._clock()).astimezone(datetime.UTC)
        manifests: dict[BackupTarget, BackupManifest] = {}
        failures: dict[BackupTarget, str] = {}
        pruned: list[str] = []
        for target, url in (
            (BackupTarget.HESABYAR_DB, hesabyar_database_url),
            (BackupTarget.TRYTON_DB, tryton_database_url),
        ):
            if url is None:
                failures[target] = (
                    "No database URL configured for the Tryton database "
                    "(HESABYAR_BACKUP__TRYTON_DATABASE_URL); refusing to "
                    "back up only half the pair."
                )
                continue
            try:
                manifests[target] = self.run_backup(target, url, now=moment)
            except Exception as exc:
                failures[target] = str(exc) or exc.__class__.__name__
                logger.exception("Backup failed for %s.", target.value)
                continue
            try:
                pruned.extend(self.prune(target, now=moment))
            except Exception:
                logger.exception(
                    "Pruning failed for %s (the backup itself succeeded).", target.value
                )
        return BackupRunReport(manifests=manifests, failures=failures, pruned_keys=tuple(pruned))

    # -- manifests + pruning ------------------------------------------------------

    def list_manifests(self, target: BackupTarget) -> list[BackupManifest]:
        """All parseable manifests for a target, newest first.

        An unparseable manifest is logged and SKIPPED (a corrupt
        manifest must not hide the good backups — but it is never
        silently deleted either: pruning only deletes manifests it
        parsed, so a corrupt manifest survives for the operator to
        inspect).
        """
        prefix = f"{self._key_prefix}/{target.value}/"
        manifests: list[BackupManifest] = []
        for key in self._store.list_objects(prefix):
            if not key.endswith(MANIFEST_SUFFIX):
                continue
            try:
                raw = self._store.get_object(key)
                data = json.loads(raw.decode("utf-8"))
                manifest = BackupManifest.from_dict(data)
            except (BackupError, ValueError, UnicodeDecodeError) as exc:
                logger.warning("Skipping unparseable backup manifest %s: %s", key, exc)
                continue
            if manifest.target is not target:
                logger.warning(
                    "Skipping manifest %s: it names target %s, not %s.",
                    key,
                    manifest.target.value,
                    target.value,
                )
                continue
            manifests.append(manifest)
        manifests.sort(key=lambda m: (m.created_at, m.object_key), reverse=True)
        return manifests

    def latest_manifest(self, target: BackupTarget) -> BackupManifest | None:
        """The newest manifest for a target, or None when it has no backups."""
        manifests = self.list_manifests(target)
        return manifests[0] if manifests else None

    def prune(self, target: BackupTarget, *, now: datetime.datetime | None = None) -> list[str]:
        """Delete expired backups (object + manifest) for a target.

        Returns the deleted object keys. Only manifests the retention
        policy selected are deleted; unparseable manifests are left
        alone (see :meth:`list_manifests`).
        """
        moment = (now or self._clock()).astimezone(datetime.UTC)
        manifests = self.list_manifests(target)
        expired = self._retention.select_expired(manifests, now=moment)
        deleted: list[str] = []
        for manifest in expired:
            self._store.delete_object(manifest.object_key)
            self._store.delete_object(manifest_key_for(manifest.object_key))
            deleted.append(manifest.object_key)
            logger.info("Pruned expired backup %s.", manifest.object_key)
        return deleted

    # -- download + verify (the drill's read path) ----------------------------------

    def download_backup(self, manifest: BackupManifest) -> bytes:
        """Download one backup, verify its checksum, decrypt it.

        The checksum is verified on the STORED bytes before
        decryption (a mismatch means the object changed after upload);
        the plaintext checksum is verified after decryption (a
        mismatch means the key is wrong or the ciphertext was
        tampered with — Fernet usually raises first, this is the
        second line). Either failure raises ``BackupIntegrityError``.
        """
        ciphertext = self._store.get_object(manifest.object_key)
        if sha256_hex(ciphertext) != manifest.sha256:
            raise BackupIntegrityError(
                f"Checksum mismatch for {manifest.object_key}: the stored "
                "object is not the bytes that were uploaded.",
                details={"object_key": manifest.object_key},
            )
        plaintext = self._cipher.decrypt(ciphertext)
        if sha256_hex(plaintext) != manifest.plaintext_sha256:
            raise BackupIntegrityError(
                f"Plaintext checksum mismatch for {manifest.object_key} after "
                "decryption.",
                details={"object_key": manifest.object_key},
            )
        if len(plaintext) != manifest.plaintext_size_bytes:
            raise BackupIntegrityError(
                f"Size mismatch for {manifest.object_key}: decrypted "
                f"{len(plaintext)} bytes, manifest says "
                f"{manifest.plaintext_size_bytes}.",
                details={"object_key": manifest.object_key},
            )
        return plaintext
