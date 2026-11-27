"""API-key lifecycle service: issue / rotate / revoke / authenticate.

This is infrastructure code (it uses SQLAlchemy models and the
:class:`ApiKeyRepository`); it has no FastAPI imports, so workers and
management scripts can use it directly. The REST layer wraps it in
dependencies (``interfaces/rest/dependencies.py``).

Transaction ownership: every method flushes but never commits. The caller
owns the transaction — the REST dependency commits after authentication
(to persist ``last_used_at``), tests roll back.

Logging rule (spec §7.5): failures are logged with the key *prefix* and
a reason only. The raw secret never reaches a log line, an exception
message, or a return value other than the one-time ``IssuedApiKey.secret``.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from hesabyar.infrastructure.auth.errors import AuthenticationError
from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.infrastructure.auth.keys import ApiKeyCrypto, extract_key_prefix
from hesabyar.infrastructure.auth.scopes import format_scopes, parse_scopes, validate_scopes
from hesabyar.infrastructure.config import Clock, SystemClock
from hesabyar.infrastructure.persistence.postgres.models import ApiClientModel, ApiKeyModel
from hesabyar.infrastructure.persistence.postgres.repositories import ApiKeyRepository

logger = logging.getLogger(__name__)


@dataclass
class IssuedApiKey:
    """A newly issued key: the stored record plus the one-time secret.

    ``secret`` is shown exactly once — hand it to the key owner and drop
    it. The record itself stores only prefix + hash.
    """

    record: ApiKeyModel
    secret: str


class ApiKeyService:
    """Issue, rotate, revoke and authenticate scoped API keys."""

    def __init__(self, crypto: ApiKeyCrypto, clock: Clock | None = None) -> None:
        self._crypto = crypto
        self._clock = clock or SystemClock()

    # -- issuance ----------------------------------------------------------

    def issue_key(
        self,
        session: Session,
        *,
        client: ApiClientModel,
        name: str,
        scopes: list[str] | frozenset[str] | set[str],
        installation_id: str,
        engine_company_id: str | None = None,
        expires_at: datetime.datetime | None = None,
        created_by_actor_id: str | None = None,
    ) -> IssuedApiKey:
        """Issue a key for an active client. Returns the secret exactly once.

        Raises ``ValueError`` for an inactive client and
        ``UnknownScopeError`` for any scope outside the spec §14.2 catalog.
        """
        if not client.is_active:
            raise ValueError(f"Cannot issue a key for inactive API client {client.id!r}.")
        validated = validate_scopes(scopes)
        generated = self._crypto.generate()
        record = ApiKeyModel(
            client_id=client.id,
            installation_id=installation_id,
            name=name,
            key_prefix=generated.key_prefix,
            key_hash=generated.key_hash,
            scopes=format_scopes(validated),
            engine_company_id=engine_company_id,
            created_by_actor_id=created_by_actor_id,
            expires_at=expires_at,
        )
        ApiKeyRepository(session).save(record)
        logger.info(
            "Issued API key %s for client %s (prefix=%s, scopes=%s)",
            record.id,
            client.id,
            record.key_prefix,
            record.scopes,
        )
        return IssuedApiKey(record=record, secret=generated.secret)

    # -- rotation / revocation ---------------------------------------------

    def rotate_key(
        self,
        session: Session,
        *,
        key: ApiKeyModel,
        name: str | None = None,
        reason: str = "rotated",
    ) -> IssuedApiKey:
        """Revoke ``key`` and issue a replacement with identical attributes.

        The new key inherits the client, scopes, installation, company
        restriction and expiry of the old one; rotation never silently
        extends a key's lifetime. The old key stops working immediately.
        Raises ``ValueError`` when the key is already revoked.
        """
        if key.revoked_at is not None:
            raise ValueError(f"Cannot rotate already-revoked API key {key.id!r}.")
        self.revoke_key(session, key=key, reason=reason)
        return self.issue_key(
            session,
            client=key.client,
            name=name if name is not None else key.name,
            scopes=parse_scopes(key.scopes),
            installation_id=key.installation_id,
            engine_company_id=key.engine_company_id,
            expires_at=key.expires_at,
            created_by_actor_id=key.created_by_actor_id,
        )

    def revoke_key(self, session: Session, *, key: ApiKeyModel, reason: str) -> None:
        """Revoke a key immediately. Idempotent: a second revoke is a no-op."""
        if key.revoked_at is not None:
            return
        key.revoked_at = self._clock.now_utc()
        key.revoked_reason = reason
        ApiKeyRepository(session).save(key)
        logger.info(
            "Revoked API key %s (prefix=%s, reason=%s)",
            key.id,
            key.key_prefix,
            reason,
        )

    # -- authentication ----------------------------------------------------

    def authenticate(self, session: Session, *, secret: str) -> AuthenticatedClient:
        """Verify a presented key and return the identity it establishes.

        Records ``last_used_at`` (flushed, not committed — the caller owns
        the transaction). Raises :class:`AuthenticationError` for every
        failure mode; the reason is logged with the prefix only.
        """
        prefix = extract_key_prefix(secret)
        if prefix is None:
            logger.info("API key authentication failed: reason=malformed")
            raise AuthenticationError("malformed")
        candidates = ApiKeyRepository(session).find_by_prefix(prefix)
        matched: ApiKeyModel | None = None
        for candidate in candidates:
            if self._crypto.verify(secret, candidate.key_hash):
                matched = candidate
                break
        if matched is None:
            logger.info("API key authentication failed: reason=unknown prefix=%s", prefix)
            raise AuthenticationError("unknown")
        now = self._clock.now_utc()
        if matched.revoked_at is not None:
            logger.info("API key authentication failed: reason=revoked prefix=%s", prefix)
            raise AuthenticationError("revoked")
        if matched.expires_at is not None and matched.expires_at <= now:
            logger.info("API key authentication failed: reason=expired prefix=%s", prefix)
            raise AuthenticationError("expired")
        client = matched.client
        if not client.is_active:
            logger.info(
                "API key authentication failed: reason=client_inactive prefix=%s", prefix
            )
            raise AuthenticationError("client_inactive")
        matched.last_used_at = now
        session.flush()
        return AuthenticatedClient(
            client_id=client.id,
            tenant_id=client.tenant_id,
            installation_id=matched.installation_id,
            key_id=matched.id,
            key_prefix=matched.key_prefix,
            client_name=client.name,
            scopes=parse_scopes(matched.scopes),
            engine_company_id=matched.engine_company_id,
        )
