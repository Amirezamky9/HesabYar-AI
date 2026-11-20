"""Fernet credential vault for stored bank/store secrets (US-503).

Implements :class:`hesabyar.domain.integrations.ports.CredentialVaultPort`
with Fernet symmetric encryption (AES-128-CBC + HMAC-SHA256, from the
``cryptography`` package). The key comes from settings
(``HESABYAR_AUTH__CREDENTIALS_ENCRYPTION_KEY``) — production secrets
live in the Vault, never in the repository (ADR 0003 decision 10).

Key format: a URL-safe base64-encoded 32-byte key, exactly as
``cryptography.fernet.Fernet.generate_key()`` produces. ``key_id`` is
the first 8 hex characters of the key's SHA-256 — a stable id for the
key version, so a future rotation can keep decrypting old rows (the
row records which key id produced its ciphertext). The key id is
derived from the key, never stored alongside the key itself.

Fail-closed: a ciphertext this vault cannot decrypt (wrong key,
tampered data) raises ``ValueError`` — callers never fall back to
plaintext.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken


def generate_encryption_key() -> str:
    """Generate a fresh Fernet key (URL-safe base64, 32 bytes).

    For operators provisioning a new installation: put the returned
    value in ``HESABYAR_AUTH__CREDENTIALS_ENCRYPTION_KEY``.
    """
    return Fernet.generate_key().decode("ascii")


class FernetCredentialVault:
    """Fernet-backed credential vault (see the module docstring)."""

    def __init__(self, key: str) -> None:
        try:
            raw_key = base64.urlsafe_b64decode(key.encode("ascii"))
        except (ValueError, TypeError) as exc:
            raise ValueError(
                "Invalid credential-encryption key: expected a URL-safe "
                "base64-encoded 32-byte Fernet key."
            ) from exc
        if len(raw_key) != 32:
            raise ValueError(
                "Invalid credential-encryption key: expected 32 bytes, "
                f"got {len(raw_key)}."
            )
        self._fernet = Fernet(key.encode("ascii"))
        self._key_id = hashlib.sha256(raw_key).hexdigest()[:8]

    @property
    def key_id(self) -> str:
        """The id of the encryption key this vault uses."""
        return self._key_id

    def encrypt(self, plaintext: str) -> str:
        """Encrypt ``plaintext`` and return the ciphertext (URL-safe text)."""
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        """Decrypt ``ciphertext`` back to plaintext.

        Raises ``ValueError`` for a ciphertext this vault cannot
        decrypt (wrong key, tampered data).
        """
        try:
            return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError) as exc:
            raise ValueError("Cannot decrypt this ciphertext with the configured key.") from exc
