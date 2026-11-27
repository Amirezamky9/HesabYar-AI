"""Fernet encryption for backup archives (US-703).

Reuses the credential vault's pattern
(``hesabyar.infrastructure.auth.credential_vault``): the same key
format (URL-safe base64, 32 bytes), the same ``key_id`` convention
(first 8 hex of the key's SHA-256, recorded in the manifest so a
restore knows which key encrypted a backup), and the same
fail-closed behaviour (a ciphertext this key cannot decrypt raises
instead of falling back to anything).

Differences from the vault: this cipher works on BYTES (a pg_dump
archive is binary), and the key is a SEPARATE key from the
credential vault's (``HESABYAR_BACKUP__ENCRYPTION_KEY``) — a backup
key and a credential key have different rotation stories and must
not be the same secret. Key generation reuses the vault's
``generate_encryption_key`` so operators have one way to mint keys.

Rotation has no tooling (threat model A6 covers the vault; the
backup key is the same story): the manual procedure is documented
in docs/backup-restore.md — generate a new key, update config,
restart the worker, keep the old key until every backup it
encrypted has aged out of retention (or re-encrypt them by hand).
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from hesabyar.domain.backup.errors import (
    BackupConfigurationError,
    BackupIntegrityError,
)


class FernetBackupCipher:
    """Fernet-backed backup cipher (see the module docstring)."""

    def __init__(self, key: str) -> None:
        try:
            raw_key = base64.urlsafe_b64decode(key.encode("ascii"))
        except (ValueError, TypeError) as exc:
            raise BackupConfigurationError(
                "Invalid backup-encryption key: expected a URL-safe "
                "base64-encoded 32-byte Fernet key."
            ) from exc
        if len(raw_key) != 32:
            raise BackupConfigurationError(
                "Invalid backup-encryption key: expected 32 bytes, "
                f"got {len(raw_key)}."
            )
        self._fernet = Fernet(key.encode("ascii"))
        self._key_id = hashlib.sha256(raw_key).hexdigest()[:8]

    @property
    def key_id(self) -> str:
        """The id of the encryption key this cipher uses."""
        return self._key_id

    def encrypt(self, plaintext: bytes) -> bytes:
        """Encrypt one dump's bytes."""
        return self._fernet.encrypt(plaintext)

    def decrypt(self, ciphertext: bytes) -> bytes:
        """Decrypt one dump's bytes.

        Raises ``BackupIntegrityError`` for a ciphertext this key
        cannot decrypt (wrong key, tampered data) — the drill treats
        that as a failed backup, never as a retry-with-plaintext.
        """
        try:
            return self._fernet.decrypt(ciphertext)
        except (InvalidToken, ValueError) as exc:
            raise BackupIntegrityError(
                "Cannot decrypt this backup with the configured key "
                "(wrong key, or the stored bytes were tampered with)."
            ) from exc
