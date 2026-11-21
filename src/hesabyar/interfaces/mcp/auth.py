"""MCP authentication + authorization (US-601, spec §13.8).

The MCP server uses the SAME API-key + scope system as REST
(``hya_live_`` keys, HMAC-SHA-256 + pepper, Bearer scheme) — no
second credential system, no second scope catalog.

- :class:`HesabYarTokenVerifier` is the SDK v2 ``TokenVerifier`` for
  Streamable HTTP: the SDK's ``BearerAuthBackend`` calls it once per
  HTTP request, and ``RequireAuthMiddleware`` rejects unauthenticated
  requests (401) and keys without ``mcp:connect`` (403) before any MCP
  handler runs. The verified identity travels in the ``AccessToken``
  claims (tenant, key id, key prefix, client name, installation,
  company restriction) so tool handlers can rebuild the exact
  :class:`AuthenticatedClient` REST would have built.
- :func:`client_from_access_token` rebuilds that identity. Claims that
  are missing or malformed fail closed (None) — a token that does not
  prove a HesabYar identity establishes no identity at all.
- :func:`resolve_company_row` is the company check, mirroring REST's
  ``_require_company_row`` (``interfaces/rest/dependencies.py``): the
  company id is looked up tenant-scoped (row id or engine id), and the
  key's company restriction is enforced. A caller-provided company id
  is NEVER proof of authorization — it is only ever checked against
  the authenticated grant.

Security properties (spec §13.8):

- The raw key secret is never retained past verification: the
  ``AccessToken.token`` field holds the key's visible prefix, not the
  secret (nothing in the SDK reads that field after verification —
  session ownership uses the client/issuer/subject triple).
- OAuth access tokens, if ever used, are bound to the intended MCP
  resource/audience: the verifier sets ``AccessToken.resource`` to the
  configured public URL and ``AuthSettings.validate_token_resource``
  makes the backend refuse tokens issued for another resource.
- One service's bearer <redacted> is never forwarded to another service:
  the MCP server authenticates the caller, then calls Tryton with its
  own configured engine credential — the caller's key never leaves
  the verification step.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from collections.abc import Callable

from mcp.server.auth.provider import AccessToken
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from hesabyar.infrastructure.auth.errors import AuthenticationError
from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.infrastructure.auth.keys import ApiKeyCrypto
from hesabyar.infrastructure.auth.scopes import format_scopes, parse_scopes
from hesabyar.infrastructure.auth.service import ApiKeyService
from hesabyar.infrastructure.persistence.postgres.models import EngineCompanyModel
from hesabyar.interfaces.mcp.errors import CompanyAccessError

logger = logging.getLogger(__name__)

#: Base scope for MCP access (spec §14.2 catalog — already exists; every
#: MCP key needs it in addition to the scopes of the tools it calls).
MCP_BASE_SCOPE = "mcp:connect"

# AccessToken claim keys carrying the HesabYar identity. The claims are
# the only channel between the HTTP bearer verification (which runs in
# the Starlette middleware) and the MCP handlers (which run in the
# session tasks), so every identity field the handlers need travels here.
CLAIM_TENANT_ID = "hesabyar_tenant_id"
CLAIM_KEY_ID = "hesabyar_key_id"
CLAIM_KEY_PREFIX = "hesabyar_key_prefix"
CLAIM_CLIENT_NAME = "hesabyar_client_name"
CLAIM_INSTALLATION_ID = "hesabyar_installation_id"
CLAIM_ENGINE_COMPANY_ID = "hesabyar_engine_company_id"


class HesabYarTokenVerifier:
    """SDK v2 ``TokenVerifier`` backed by HesabYar's API-key service."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        crypto: ApiKeyCrypto,
        *,
        resource_url: str,
    ) -> None:
        self._session_factory = session_factory
        self._crypto = crypto
        self._resource_url = resource_url

    async def verify_token(self, token: str) -> AccessToken | None:
        """Verify a presented API key; None for every failure mode.

        The sync DB lookup runs in a thread (the SDK calls this from
        the event loop). ``last_used_at`` is committed on success, like
        REST's ``get_authenticated_client``. The failure reason is
        logged server-side with the prefix only; the caller sees one
        generic 401.
        """
        verified = await asyncio.to_thread(self._verify_sync, token)
        if verified is None:
            return None
        client, expires_at = verified
        return AccessToken(
            # The visible prefix, NOT the secret: nothing in the SDK
            # reads this field after verification, and retaining the
            # raw secret in the request context would widen exposure.
            token=client.key_prefix,
            client_id=client.client_id,
            scopes=sorted(client.scopes),
            expires_at=int(expires_at.timestamp()) if expires_at is not None else None,
            # RFC 8707 resource indicator: this token is bound to the
            # MCP server's own public URL (its audience).
            resource=self._resource_url,
            claims={
                CLAIM_TENANT_ID: client.tenant_id,
                CLAIM_KEY_ID: client.key_id,
                CLAIM_KEY_PREFIX: client.key_prefix,
                CLAIM_CLIENT_NAME: client.client_name,
                CLAIM_INSTALLATION_ID: client.installation_id,
                CLAIM_ENGINE_COMPANY_ID: client.engine_company_id,
            },
        )

    def _verify_sync(self, token: str) -> tuple[AuthenticatedClient, datetime.datetime | None] | None:
        """Authenticate ``token``; (client, key expiry) or None."""
        session = self._session_factory()
        try:
            service = ApiKeyService(self._crypto)
            try:
                client = service.authenticate(session, secret=token)
            except AuthenticationError:
                session.rollback()
                return None
            # The key's own expiry, for AccessToken.expires_at (the
            # backend re-checks it per request; the service already
            # rejected expired keys above).
            from hesabyar.infrastructure.persistence.postgres.repositories import (
                ApiKeyRepository,
            )

            record = ApiKeyRepository(session).get_by_id(client.key_id)
            expires_at = record.expires_at if record is not None else None
            session.commit()
            return client, expires_at
        finally:
            session.close()


def client_from_access_token(token: AccessToken) -> AuthenticatedClient | None:
    """Rebuild the HesabYar identity from a verified token's claims.

    Returns None when the claims do not carry a complete HesabYar
    identity — a token from another issuer (or a future OAuth token
    without these claims) establishes no HesabYar identity.
    """
    claims = token.claims or {}
    tenant_id = claims.get(CLAIM_TENANT_ID)
    key_id = claims.get(CLAIM_KEY_ID)
    key_prefix = claims.get(CLAIM_KEY_PREFIX)
    client_name = claims.get(CLAIM_CLIENT_NAME)
    installation_id = claims.get(CLAIM_INSTALLATION_ID)
    engine_company_id = claims.get(CLAIM_ENGINE_COMPANY_ID)
    if not (
        isinstance(tenant_id, str)
        and tenant_id
        and isinstance(key_id, str)
        and key_id
        and isinstance(key_prefix, str)
        and key_prefix
        and isinstance(client_name, str)
        and isinstance(installation_id, str)
        and installation_id
        and (engine_company_id is None or isinstance(engine_company_id, str))
    ):
        logger.warning(
            "Access token for client %s carries no usable HesabYar identity claims",
            token.client_id,
        )
        return None
    return AuthenticatedClient(
        client_id=token.client_id,
        tenant_id=tenant_id,
        installation_id=installation_id,
        key_id=key_id,
        key_prefix=key_prefix,
        client_name=client_name,
        scopes=parse_scopes(format_scopes(token.scopes)),
        engine_company_id=engine_company_id,
    )


def find_company_row(
    session: Session, tenant_id: str, company_id: str
) -> EngineCompanyModel | None:
    """Find a tenant's ``engine_companies`` row by row id or engine id.

    Mirrors REST's ``_find_company_row``: a row belonging to another
    tenant is invisible — the lookup is always tenant-scoped, so it
    looks exactly like a row that does not exist.

    This is a LOOKUP, not an authorization check: it does not enforce
    the key's company restriction. Callers that authorize use
    :func:`resolve_company_row`; this function exists for the
    middleware's audit record, which names the company a call touched
    even when the call itself was denied.
    """
    stmt = select(EngineCompanyModel).where(
        EngineCompanyModel.tenant_id == tenant_id,
        or_(
            EngineCompanyModel.id == company_id,
            EngineCompanyModel.external_company_id == company_id,
        ),
    )
    return session.execute(stmt).scalar_one_or_none()


def resolve_company_row(
    session: Session, client: AuthenticatedClient, company_id: str
) -> EngineCompanyModel:
    """Resolve a caller-supplied company id against the grant.

    This is the SAFETY property of spec §13.4: the company id is
    checked against the authenticated grant, never trusted as proof of
    authorization. Unknown company (or a company of another tenant)
    raises :class:`CompanyAccessError` (``company_not_found``); a key
    restricted to a different company raises it with
    ``company_forbidden``. Both fail closed; the middleware translates
    the exception to an ``MCP_ERROR_FORBIDDEN`` protocol error.
    """
    row = find_company_row(session, client.tenant_id, company_id)
    if row is None:
        raise CompanyAccessError("company_not_found", f"Company {company_id!r} does not exist.")
    if not client.can_access_company(row.id):
        raise CompanyAccessError(
            "company_forbidden", "The API key is restricted to a different company."
        )
    return row
