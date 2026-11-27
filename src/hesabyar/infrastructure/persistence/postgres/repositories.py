"""PostgreSQL tenant-scoped repository implementations enforcing multi-tenant isolation.

Repositories follow the Architecture Authority (ARCHITECTURE.md §7) tenant isolation rules:
- Every business entity belongs to a tenant.
- All database queries and operations are strictly scoped to the active TenantContext.
- Inactive tenant contexts fail closed with InvariantViolationError.
- Cross-tenant access or modification attempts raise TenantMismatchError or return None.
"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from hesabyar.domain.common.context import TenantContext
from hesabyar.domain.common.errors import InvariantViolationError, TenantMismatchError
from hesabyar.domain.common.ids import ActorId, TenantId
from hesabyar.infrastructure.persistence.postgres.models import (
    ActorModel,
    ApiClientModel,
    ApiKeyModel,
    TenantModel,
)


class ActorRepository:
    """Tenant-scoped repository for ActorModel entities.

    Enforces multi-tenant isolation at the data access layer:
    - Rejects missing or inactive tenant contexts fail-closed.
    - All queries, updates, and deletes are scoped to tenant_context.tenant_id.
    - Cross-tenant mutations raise TenantMismatchError.
    """

    def __init__(self, session: Session, tenant_context: TenantContext | None) -> None:
        if tenant_context is None:
            raise InvariantViolationError(
                "Tenant context is required for ActorRepository.",
                code="TENANT_CONTEXT_REQUIRED",
            )
        if not isinstance(tenant_context, TenantContext):
            raise InvariantViolationError(
                f"Expected TenantContext instance, got {type(tenant_context).__name__}.",
                code="INVALID_TENANT_CONTEXT",
            )
        self.session = session
        self.tenant_context = tenant_context

    def _ensure_active(self) -> None:
        """Enforce that the tenant context is active; fail-closed if not."""
        self.tenant_context.require_active()

    @property
    def tenant_id_str(self) -> str:
        return str(self.tenant_context.tenant_id)

    def get_by_id(self, actor_id: ActorId | str) -> ActorModel | None:
        """Fetch an actor by primary key within the current tenant context.

        Returns None if not found or if the actor belongs to another tenant.
        """
        self._ensure_active()
        aid = str(actor_id)
        stmt = (
            select(ActorModel)
            .where(ActorModel.id == aid)
            .where(ActorModel.tenant_id == self.tenant_id_str)
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def get_by_username(self, username: str) -> ActorModel | None:
        """Fetch an actor by username within the current tenant context."""
        self._ensure_active()
        stmt = (
            select(ActorModel)
            .where(ActorModel.username == username)
            .where(ActorModel.tenant_id == self.tenant_id_str)
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def list_all(self) -> list[ActorModel]:
        """List all actors belonging to the current tenant."""
        self._ensure_active()
        stmt = (
            select(ActorModel)
            .where(ActorModel.tenant_id == self.tenant_id_str)
            .order_by(ActorModel.username)
        )
        return list(self.session.execute(stmt).scalars().all())

    def save(self, actor: ActorModel) -> None:
        """Persist or update an actor entity within the current tenant.

        Raises TenantMismatchError if actor.tenant_id does not match the context.
        """
        self._ensure_active()
        if actor.tenant_id != self.tenant_id_str:
            raise TenantMismatchError(
                f"Cannot save actor for tenant {actor.tenant_id} within tenant context {self.tenant_id_str}."
            )
        self.session.add(actor)
        self.session.flush()

    def delete(self, actor_id: ActorId | str) -> bool:
        """Delete an actor by ID within the current tenant context.

        Returns True if deleted, False if not found or cross-tenant.
        """
        self._ensure_active()
        aid = str(actor_id)
        stmt = (
            delete(ActorModel)
            .where(ActorModel.id == aid)
            .where(ActorModel.tenant_id == self.tenant_id_str)
        )
        result = self.session.execute(stmt)
        self.session.flush()
        if isinstance(result, CursorResult):
            return result.rowcount > 0
        return False


class TenantRepository:
    """Repository for TenantModel entities scoped to tenant context."""

    def __init__(self, session: Session, tenant_context: TenantContext | None) -> None:
        if tenant_context is None:
            raise InvariantViolationError(
                "Tenant context is required for TenantRepository.",
                code="TENANT_CONTEXT_REQUIRED",
            )
        if not isinstance(tenant_context, TenantContext):
            raise InvariantViolationError(
                f"Expected TenantContext instance, got {type(tenant_context).__name__}.",
                code="INVALID_TENANT_CONTEXT",
            )
        self.session = session
        self.tenant_context = tenant_context

    def _ensure_active(self) -> None:
        self.tenant_context.require_active()

    @property
    def tenant_id_str(self) -> str:
        return str(self.tenant_context.tenant_id)

    def get_current(self) -> TenantModel | None:
        """Fetch the current tenant record."""
        self._ensure_active()
        stmt = select(TenantModel).where(TenantModel.id == self.tenant_id_str)
        return self.session.execute(stmt).scalar_one_or_none()

    def get_by_id(self, tenant_id: TenantId | str) -> TenantModel | None:
        """Fetch a tenant record matching the current tenant context.

        Returns None if requesting a different tenant.
        """
        self._ensure_active()
        tid = str(tenant_id)
        if tid != self.tenant_id_str:
            return None
        stmt = select(TenantModel).where(TenantModel.id == self.tenant_id_str)
        return self.session.execute(stmt).scalar_one_or_none()

    def save(self, tenant: TenantModel) -> None:
        """Save changes to the current tenant record.

        Raises TenantMismatchError if attempting to modify a different tenant.
        """
        self._ensure_active()
        if tenant.id != self.tenant_id_str:
            raise TenantMismatchError(
                f"Cannot save tenant {tenant.id} within tenant context {self.tenant_id_str}."
            )
        self.session.add(tenant)
        self.session.flush()


# ---------------------------------------------------------------------------
# API-key repositories (US-103)
#
# Deliberately NOT tenant-scoped: API-key authentication *establishes* the
# tenant context (the key itself is the credential), so these repositories
# cannot take a TenantContext the way ActorRepository does. The tenant
# boundary is enforced one layer up, by AuthenticatedClient: a verified
# key only ever yields its own tenant_id, and company access is checked
# against that tenant (see interfaces/rest/dependencies.py).
#
# These repositories never see a raw secret: issuance stores prefix + hash
# only, and authentication looks keys up by prefix.
# ---------------------------------------------------------------------------


class ApiClientRepository:
    """Repository for ApiClientModel entities (no tenant context — see above)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_by_id(self, client_id: str) -> ApiClientModel | None:
        """Fetch an API client by primary key."""
        stmt = select(ApiClientModel).where(ApiClientModel.id == str(client_id))
        return self.session.execute(stmt).scalar_one_or_none()

    def list_for_tenant(self, tenant_id: str) -> list[ApiClientModel]:
        """List all API clients belonging to one tenant."""
        stmt = (
            select(ApiClientModel)
            .where(ApiClientModel.tenant_id == str(tenant_id))
            .order_by(ApiClientModel.name)
        )
        return list(self.session.execute(stmt).scalars().all())

    def save(self, client: ApiClientModel) -> None:
        """Persist or update an API client."""
        self.session.add(client)
        self.session.flush()


class ApiKeyRepository:
    """Repository for ApiKeyModel entities (no tenant context — see above)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def find_by_prefix(self, key_prefix: str) -> list[ApiKeyModel]:
        """Fetch candidate keys sharing a visible prefix.

        Prefixes are not unique by construction (16 chars, ~42 random
        bits), so callers must verify every candidate's hash.
        """
        stmt = select(ApiKeyModel).where(ApiKeyModel.key_prefix == key_prefix)
        return list(self.session.execute(stmt).scalars().all())

    def get_by_id(self, key_id: str) -> ApiKeyModel | None:
        """Fetch an API key by primary key."""
        stmt = select(ApiKeyModel).where(ApiKeyModel.id == str(key_id))
        return self.session.execute(stmt).scalar_one_or_none()

    def list_for_client(self, client_id: str) -> list[ApiKeyModel]:
        """List all keys (including revoked) belonging to one client."""
        stmt = (
            select(ApiKeyModel)
            .where(ApiKeyModel.client_id == str(client_id))
            .order_by(ApiKeyModel.created_at)
        )
        return list(self.session.execute(stmt).scalars().all())

    def save(self, key: ApiKeyModel) -> None:
        """Persist or update an API key."""
        self.session.add(key)
        self.session.flush()


class SqlAlchemyInstallationTenantResolver:
    """Resolve an installation's tenant through the API-key store (US-555).

    Implements
    :class:`hesabyar.domain.settlement.ports.InstallationTenantResolver`:
    a key is issued for one client of one tenant under one
    installation, so the oldest key for an installation identifies
    its tenant. Revoked/expired keys still identify the same client,
    so they are not filtered out — the lookup is about ownership,
    not about whether the key can still authenticate. Returns None
    when no key exists for the installation.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def tenant_id_for_installation(self, installation_id: str) -> str | None:
        """Return the tenant owning this installation, or None."""
        stmt = (
            select(ApiClientModel.tenant_id)
            .join(ApiKeyModel, ApiKeyModel.client_id == ApiClientModel.id)
            .where(ApiKeyModel.installation_id == installation_id)
            .order_by(ApiKeyModel.created_at.asc(), ApiKeyModel.id.asc())
            .limit(1)
        )
        return self.session.execute(stmt).scalar_one_or_none()
