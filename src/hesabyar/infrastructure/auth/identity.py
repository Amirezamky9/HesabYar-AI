"""The identity a verified API key establishes.

An :class:`AuthenticatedClient` is what FastAPI dependencies return: the
API client, its tenant, the scopes its key carries, and the optional
company restriction. It contains no key material — only the visible
prefix, which is safe to log.
"""

from __future__ import annotations

from dataclasses import dataclass

from hesabyar.infrastructure.auth.scopes import missing_scopes


@dataclass(frozen=True)
class AuthenticatedClient:
    """Identity established by one authenticated API key."""

    client_id: str
    tenant_id: str
    installation_id: str
    key_id: str
    key_prefix: str
    client_name: str
    scopes: frozenset[str]
    # HesabYar-owned engine_companies.id the key is restricted to, or None
    # for an unrestricted key (may access any company of the tenant).
    engine_company_id: str | None

    def has_scope(self, scope: str) -> bool:
        """True when the key carries exactly ``scope`` (no wildcards)."""
        return scope in self.scopes

    def has_scopes(self, *scopes: str) -> bool:
        """True when the key carries every listed scope."""
        return not missing_scopes(self.scopes, scopes)

    def missing_scopes(self, *scopes: str) -> list[str]:
        """Required scopes the key does not carry (sorted, possibly empty)."""
        return missing_scopes(self.scopes, scopes)

    def can_access_company(self, engine_company_id: str) -> bool:
        """True when the key may access the given engine company.

        An unrestricted key (``engine_company_id is None``) may access
        any company of its tenant; a restricted key may access only the
        company it is bound to.
        """
        if self.engine_company_id is None:
            return True
        return self.engine_company_id == engine_company_id
