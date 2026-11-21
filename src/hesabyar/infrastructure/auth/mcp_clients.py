"""MCP authorized-client lifecycle service (US-606, spec §13.8 + §14.1).

The registry (``mcp_authorized_clients``) is the MANAGEMENT record
for one AI client's MCP access — not a second credential system.
Creating a client provisions an ``api_clients`` row and issues an
ordinary API key through :class:`ApiKeyService` (``hya_live_``,
HMAC-SHA-256 + pepper, the same rules as REST keys); the MCP server
keeps authenticating that key and never consults the registry.
Rotation rotates the underlying key (the registry row's ``key_id``
moves to the replacement); revocation revokes the key and marks the
row. The secret is returned exactly once, at creation/rotation, and
is never stored, logged or returned again.

Transaction ownership mirrors :class:`ApiKeyService`: every method
flushes but never commits. The caller owns the transaction — the
REST router commits after the mutation (and its audit events).

Logging rule (spec §7.5): failures are logged with the key *prefix*
and a reason only. The raw secret never reaches a log line, an
exception message, or a return value other than the one-time
``IssuedMcpClient.secret``.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from hesabyar.infrastructure.auth.keys import ApiKeyCrypto
from hesabyar.infrastructure.auth.scopes import format_scopes, validate_scopes
from hesabyar.infrastructure.auth.service import ApiKeyService
from hesabyar.infrastructure.config import Clock, SystemClock
from hesabyar.infrastructure.persistence.postgres.models import (
    ApiClientModel,
    EngineCompanyModel,
    McpAuthorizedClientModel,
)

logger = logging.getLogger(__name__)

#: The base scope every MCP client must carry — without it the key
#: cannot connect to the MCP server at all (middleware + HTTP layer).
MCP_CLIENT_BASE_SCOPE = "mcp:connect"


class McpClientNameTakenError(ValueError):
    """A client with this name already exists in the tenant."""


class McpClientRevokedError(ValueError):
    """The client is revoked; it cannot be rotated."""


class McpClientCompanyNotFoundError(ValueError):
    """The company grant names no company of this tenant."""


@dataclass
class IssuedMcpClient:
    """A newly created/rotated MCP client: the record plus the one-time secret.

    ``secret`` is shown exactly once — hand it to the client owner and
    drop it. The record itself stores only prefix + hash (on the key).
    """

    record: McpAuthorizedClientModel
    secret: str


class McpClientService:
    """Create, rotate, revoke and list authorized MCP clients."""

    def __init__(self, crypto: ApiKeyCrypto, clock: Clock | None = None) -> None:
        self._crypto = crypto
        self._clock = clock or SystemClock()
        self._keys = ApiKeyService(crypto, clock=self._clock)

    # -- reads -------------------------------------------------------------

    def get_client(
        self, session: Session, *, tenant_id: str, client_id: str
    ) -> McpAuthorizedClientModel | None:
        """Return one registry row of ``tenant_id``, or None.

        A row of another tenant is invisible — the lookup is always
        tenant-scoped, so it looks exactly like a row that does not
        exist.
        """
        stmt = (
            select(McpAuthorizedClientModel)
            .options(selectinload(McpAuthorizedClientModel.key))
            .where(
                McpAuthorizedClientModel.tenant_id == tenant_id,
                McpAuthorizedClientModel.id == client_id,
            )
        )
        return session.execute(stmt).scalar_one_or_none()

    def list_clients(
        self, session: Session, *, tenant_id: str, include_revoked: bool = True
    ) -> list[McpAuthorizedClientModel]:
        """Return the tenant's registry rows, newest first."""
        stmt = (
            select(McpAuthorizedClientModel)
            .options(selectinload(McpAuthorizedClientModel.key))
            .where(McpAuthorizedClientModel.tenant_id == tenant_id)
        )
        if not include_revoked:
            stmt = stmt.where(McpAuthorizedClientModel.revoked_at.is_(None))
        stmt = stmt.order_by(
            McpAuthorizedClientModel.created_at.desc(),
            McpAuthorizedClientModel.id.desc(),
        )
        return list(session.execute(stmt).scalars().all())

    # -- lifecycle -----------------------------------------------------------

    def create_client(
        self,
        session: Session,
        *,
        tenant_id: str,
        name: str,
        description: str | None,
        scopes: list[str] | frozenset[str] | set[str],
        installation_id: str,
        company_id: str | None = None,
        expires_at: datetime.datetime | None = None,
        created_by_actor_id: str | None = None,
    ) -> IssuedMcpClient:
        """Create an MCP client: api_client row + API key + registry row.

        ``company_id`` is the HesabYar row id or the engine company id
        of the company grant (None = every company of the tenant),
        resolved tenant-scoped. Raises ``McpClientNameTakenError``
        for a duplicate name, ``McpClientCompanyNotFoundError`` for
        an unknown company, ``UnknownScopeError`` for a scope outside
        the §14.2 catalog, and ``ValueError`` when the scopes omit
        ``mcp:connect``.
        """
        validated = validate_scopes(scopes)
        if MCP_CLIENT_BASE_SCOPE not in validated:
            raise ValueError(
                "An MCP client's scopes must include "
                f"{MCP_CLIENT_BASE_SCOPE!r} (without it the key cannot connect)."
            )
        company_row = self._resolve_company(session, tenant_id, company_id)
        existing = session.execute(
            select(ApiClientModel).where(
                ApiClientModel.tenant_id == tenant_id,
                ApiClientModel.name == name,
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise McpClientNameTakenError(
                f"An API client named {name!r} already exists in this tenant."
            )
        # Flush parents before children (SQLAlchemy does not order a
        # flush by bare-FK dependency without a relationship): the
        # client row first, then the key, then the registry row.
        client = ApiClientModel(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            name=name,
            description=description,
        )
        session.add(client)
        session.flush()
        issued = self._keys.issue_key(
            session,
            client=client,
            name=name,
            scopes=validated,
            installation_id=installation_id,
            engine_company_id=company_row.id if company_row is not None else None,
            expires_at=expires_at,
            created_by_actor_id=created_by_actor_id,
        )
        record = McpAuthorizedClientModel(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            client_id=client.id,
            key_id=issued.record.id,
            name=name,
            description=description,
            scopes=format_scopes(validated),
            engine_company_id=company_row.id if company_row is not None else None,
        )
        session.add(record)
        session.flush()
        logger.info(
            "Created MCP authorized client %s for tenant %s (prefix=%s, scopes=%s)",
            record.id,
            tenant_id,
            issued.record.key_prefix,
            record.scopes,
        )
        return IssuedMcpClient(record=record, secret=issued.secret)

    def rotate_client(
        self,
        session: Session,
        *,
        record: McpAuthorizedClientModel,
        reason: str = "rotated",
    ) -> IssuedMcpClient:
        """Revoke the client's key and issue a replacement.

        The new key inherits the client, scopes, installation, company
        grant and expiry of the old one; rotation never silently
        extends a key's lifetime, and never changes the registry row's
        scopes or grant. The old key stops working immediately.
        Raises ``McpClientRevokedError`` for a revoked client.
        """
        if record.revoked_at is not None:
            raise McpClientRevokedError(
                f"Cannot rotate revoked MCP client {record.id!r}."
            )
        issued = self._keys.rotate_key(session, key=record.key, reason=reason)
        record.key_id = issued.record.id
        session.flush()
        logger.info(
            "Rotated MCP authorized client %s (new prefix=%s)",
            record.id,
            issued.record.key_prefix,
        )
        return IssuedMcpClient(record=record, secret=issued.secret)

    def revoke_client(
        self, session: Session, *, record: McpAuthorizedClientModel, reason: str
    ) -> bool:
        """Revoke a client immediately (its key too). Returns False when
        the client was already revoked (idempotent no-op)."""
        if record.revoked_at is not None:
            return False
        self._keys.revoke_key(session, key=record.key, reason=reason)
        record.revoked_at = self._clock.now_utc()
        record.revoked_reason = reason
        session.flush()
        logger.info(
            "Revoked MCP authorized client %s (prefix=%s, reason=%s)",
            record.id,
            record.key.key_prefix,
            reason,
        )
        return True

    # -- helpers -------------------------------------------------------------

    def _resolve_company(
        self, session: Session, tenant_id: str, company_id: str | None
    ) -> EngineCompanyModel | None:
        """Resolve the company grant tenant-scoped (row id or engine id).

        None means no grant (every company of the tenant). A value
        naming no row of this tenant — including a row of another
        tenant — raises ``McpClientCompanyNotFoundError``.
        """
        if company_id is None:
            return None
        stmt = select(EngineCompanyModel).where(
            EngineCompanyModel.tenant_id == tenant_id,
            or_(
                EngineCompanyModel.id == company_id,
                EngineCompanyModel.external_company_id == company_id,
            ),
        )
        row = session.execute(stmt).scalar_one_or_none()
        if row is None:
            raise McpClientCompanyNotFoundError(
                f"Company {company_id!r} does not exist."
            )
        return row
