"""Ports for the backup domain (US-703).

The three seams the backup service depends on — object storage,
encryption, and the pg_dump/pg_restore tools — are protocols so the
unit tests run with fakes and production swaps backends by
configuration, never by code change. The S3 destination decision is
UNVERIFIED (phased design §1.3 phase 7): ``ObjectStorePort`` is
deliberately destination-agnostic — an S3-compatible endpoint, a
local directory, or a test fake are all the same port.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class DumpResult:
    """One pg_dump run: the archive bytes + what produced them."""

    data: bytes
    pg_dump_version: str
    database: str  # database NAME only — never a DSN


@dataclass(frozen=True)
class PgToolsInfo:
    """The pg_dump / pg_restore binaries found on this machine."""

    pg_dump_path: str
    pg_dump_version: str
    pg_restore_path: str
    pg_restore_version: str


class ObjectStorePort(Protocol):
    """Destination-agnostic object storage (S3-compatible or local).

    Keys are slash-separated object paths (``<prefix>/<target>/...``),
    never filesystem paths — the filesystem backend maps them under
    its root directory. ``get_object`` on a missing key raises
    ``ObjectStoreError`` with ``not_found=True``; ``delete_object``
    on a missing key is a no-op (S3 DELETE is idempotent).
    """

    def put_object(self, key: str, data: bytes, *, content_type: str) -> None:
        """Upload (or overwrite) one object."""
        ...

    def get_object(self, key: str) -> bytes:
        """Download one object (raises ``ObjectStoreError`` when missing)."""
        ...

    def list_objects(self, prefix: str) -> list[str]:
        """All object keys starting with ``prefix``, sorted."""
        ...

    def delete_object(self, key: str) -> None:
        """Delete one object (a no-op when it does not exist)."""
        ...


class BackupCipherPort(Protocol):
    """Symmetric encryption for backup archives (Fernet in production)."""

    @property
    def key_id(self) -> str:
        """The id of the encryption key (recorded in the manifest)."""
        ...

    def encrypt(self, plaintext: bytes) -> bytes:
        """Encrypt one dump's bytes."""
        ...

    def decrypt(self, ciphertext: bytes) -> bytes:
        """Decrypt one dump's bytes (raises on a wrong key / tampering)."""
        ...


class DatabaseDumperPort(Protocol):
    """PostgreSQL-native dump/restore + scratch-database administration.

    This is the OPERATIONS layer's one sanctioned use of direct
    database access to Tryton's database: pg_dump/pg_restore against
    the Tryton database, the same way the operator runs pg_dump by
    hand (spec §19.2 forbids HesabYar SQL access to Tryton's tables;
    dumping and restoring whole databases is backup, not application
    reads — see docs/backup-restore.md). No method here reads or
    writes Tryton TABLES; the drill's Tryton checks go through the
    REST adapter against a scratch trytond.

    Implementations must never put a password in argv or logs: the
    DSN password travels via the ``PGPASSWORD`` environment variable
    of the child process only.
    """

    def check_tools(self) -> PgToolsInfo:
        """Verify pg_dump + pg_restore exist and report their versions."""
        ...

    def dump(self, database_url: str) -> DumpResult:
        """Dump one database (``pg_dump -Fc``) to bytes."""
        ...

    def restore(self, database_url: str, data: bytes, *, no_owner: bool) -> None:
        """Restore a dump into an (empty) database with pg_restore.

        ``no_owner`` (scratch restores) drops ownership/privileges
        from the restore so a dump owned by another role still
        restores; a production restore keeps owners (``no_owner=False``)
        and the runbook covers creating the role first.
        """
        ...

    def create_database(self, admin_url: str, name: str) -> None:
        """CREATE DATABASE on the server ``admin_url`` points at.

        ``admin_url`` is a maintenance-database DSN (usually the
        ``postgres`` database) for a role with CREATEDB. ``name`` is
        validated as a safe identifier before use.
        """
        ...

    def drop_database(self, admin_url: str, name: str) -> None:
        """DROP DATABASE IF EXISTS (WITH FORCE) on ``admin_url``'s server."""
        ...
