"""Unit tests for the Fernet backup cipher (US-703).

The cipher reuses the credential vault's key format + key_id
convention; these tests pin the byte-level behaviour (round-trip,
wrong-key / tampered-ciphertext failure) and the key validation.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from hesabyar.domain.backup.errors import (
    BackupConfigurationError,
    BackupIntegrityError,
)
from hesabyar.infrastructure.auth.credential_vault import generate_encryption_key
from hesabyar.infrastructure.operations.backup_cipher import FernetBackupCipher


def test_round_trip_bytes() -> None:
    cipher = FernetBackupCipher(generate_encryption_key())
    plaintext = bytes(range(256)) * 100  # binary dump-like bytes
    assert cipher.decrypt(cipher.encrypt(plaintext)) == plaintext


def test_key_id_is_stable_and_8_hex_chars() -> None:
    key = generate_encryption_key()
    first = FernetBackupCipher(key)
    second = FernetBackupCipher(key)
    assert first.key_id == second.key_id
    assert len(first.key_id) == 8
    int(first.key_id, 16)  # must parse as hex
    assert FernetBackupCipher(generate_encryption_key()).key_id != first.key_id


def test_invalid_keys_fail_closed() -> None:
    with pytest.raises(BackupConfigurationError, match="base64-encoded 32-byte"):
        FernetBackupCipher("not-a-fernet-key!!")
    short = Fernet.generate_key().decode("ascii")[:20]
    with pytest.raises(BackupConfigurationError, match="expected 32 bytes"):
        FernetBackupCipher(short)


def test_wrong_key_cannot_decrypt() -> None:
    cipher = FernetBackupCipher(generate_encryption_key())
    other = FernetBackupCipher(generate_encryption_key())
    ciphertext = cipher.encrypt(b"a pg_dump archive")
    with pytest.raises(BackupIntegrityError, match="Cannot decrypt"):
        other.decrypt(ciphertext)


def test_tampered_ciphertext_cannot_decrypt() -> None:
    cipher = FernetBackupCipher(generate_encryption_key())
    ciphertext = bytearray(cipher.encrypt(b"a pg_dump archive"))
    ciphertext[-1] ^= 0xFF
    with pytest.raises(BackupIntegrityError, match="Cannot decrypt"):
        cipher.decrypt(bytes(ciphertext))
