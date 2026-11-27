"""API-key generation, hashing and verification (spec §7.5).

Key format
----------
``hya_live_<43 url-safe base64 chars>`` — 52 characters total. The
random part is 32 bytes from :mod:`secrets` (256 bits of entropy),
encoded with :func:`secrets.token_urlsafe`. The ``hya_live_`` scheme
prefix makes a HesabYar key recognisable in support tickets and lets
secret scanners match it; it carries no entropy.

Storage
-------
Only two things are ever stored (``api_keys`` table, US-102 schema):

- ``key_prefix``: the first 16 characters of the key (the scheme prefix
  plus 7 random characters, ~42 bits). It is the lookup key: a presented
  key is located by prefix, then verified by hash. Prefix collisions are
  possible in principle, so lookup returns *candidates* and every
  candidate hash is compared.
- ``key_hash``: ``v1:<hex>`` where ``<hex>`` is the hex-encoded
  HMAC-SHA-256 of the full secret, keyed with a server-side pepper. The
  ``v1:`` version tag lets a future scheme change without invalidating
  the column. The raw secret is never stored, logged or returned after
  issuance.

Comparison is constant-time via :func:`hmac.compare_digest`.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

# Recognisable scheme prefix for live HesabYar API keys.
KEY_SCHEME_PREFIX = "hya_live_"

# Random part: 32 bytes -> secrets.token_urlsafe(32) -> 43 characters.
_RANDOM_BYTES = 32

# Characters of the key stored (and shown) as the visible prefix. Matches
# the api_keys.key_prefix column width (String(16)).
KEY_PREFIX_LENGTH = 16

# Hash format version tag. Bump (and keep verifying the old one) if the
# scheme ever changes.
_HASH_VERSION = "v1"
_HASH_SEPARATOR = ":"


@dataclass(frozen=True)
class GeneratedApiKey:
    """A freshly generated key: secret + the two storable values.

    ``secret`` is shown exactly once, at issuance. Callers must hand it
    to the key owner and then drop it; it must never be persisted,
    logged, or placed in an error response.
    """

    secret: str
    key_prefix: str
    key_hash: str


def extract_key_prefix(secret: str) -> str | None:
    """Return the stored lookup prefix for a presented key, or None.

    Returns None for malformed input (wrong scheme prefix, too short),
    which the authenticator treats as an authentication failure — never
    as a lookup.
    """
    if not isinstance(secret, str):
        return None
    if len(secret) < KEY_PREFIX_LENGTH or not secret.startswith(KEY_SCHEME_PREFIX):
        return None
    return secret[:KEY_PREFIX_LENGTH]


class ApiKeyCrypto:
    """Generate keys and hash/verify them with a server-side pepper.

    The pepper comes from configuration (``AuthSettings.api_key_pepper``,
    env ``HESABYAR_AUTH__API_KEY_PEPPER``) — never hardcoded here. An
    empty pepper is refused: hashing without a pepper would let a
    database leak turn into offline brute-forcing of the (high-entropy,
    but still only hashed) secrets.
    """

    def __init__(self, pepper: str) -> None:
        if not pepper:
            raise ValueError("API-key pepper must not be empty.")
        self._pepper = pepper.encode("utf-8")

    def generate(self) -> GeneratedApiKey:
        """Generate a new high-entropy key and its storable values."""
        secret = KEY_SCHEME_PREFIX + secrets.token_urlsafe(_RANDOM_BYTES)
        return GeneratedApiKey(
            secret=secret,
            key_prefix=secret[:KEY_PREFIX_LENGTH],
            key_hash=self.hash_secret(secret),
        )

    def hash_secret(self, secret: str) -> str:
        """Return the versioned HMAC-SHA-256 hex digest of a secret."""
        digest = hmac.new(self._pepper, secret.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{_HASH_VERSION}{_HASH_SEPARATOR}{digest}"

    def verify(self, secret: str, stored_hash: str) -> bool:
        """Constant-time check of a presented secret against a stored hash.

        Unknown hash versions (or malformed stored values) fail closed.
        The comparison itself always runs through ``hmac.compare_digest``
        on equal-length hex digests.
        """
        if not isinstance(secret, str) or not secret:
            return False
        prefix = f"{_HASH_VERSION}{_HASH_SEPARATOR}"
        if not stored_hash.startswith(prefix):
            return False
        expected = self.hash_secret(secret)
        return hmac.compare_digest(expected, stored_hash)
