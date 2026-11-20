"""Typed API-key auth failures.

Messages here are generic by design: they never contain key material, and
the REST layer maps them to the same 401/403 problem-details envelope
regardless of the underlying reason, so a caller cannot probe whether a
key exists, is revoked, or is merely expired.
"""

from __future__ import annotations


class ApiKeyAuthError(Exception):
    """Base class for API-key authentication/authorization failures."""


class AuthenticationError(ApiKeyAuthError):
    """A presented API key could not be authenticated.

    ``reason`` is one of ``malformed`` (wrong format), ``unknown`` (no
    matching stored key), ``revoked``, ``expired`` or
    ``client_inactive``. It is logged server-side together with the key
    prefix only; clients always see the same generic 401.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"API key authentication failed: {reason}")
        self.reason = reason


class UnknownScopeError(ApiKeyAuthError, ValueError):
    """A scope was requested that is not in the spec §14.2 catalog."""

    def __init__(self, scope: str) -> None:
        super().__init__(f"Unknown API key scope: {scope!r}")
        self.scope = scope
