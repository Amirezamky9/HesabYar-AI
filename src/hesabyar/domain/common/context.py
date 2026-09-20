"""Tenant and actor context models enforcing multi-tenant isolation and scope-based authorization."""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum

from hesabyar.domain.common.errors import (
    InvariantViolationError,
    TenantMismatchError,
    UnauthorizedScopeError,
)
from hesabyar.domain.common.ids import ActorId, TenantId


class Scope(StrEnum):
    """Canonical permission scopes for HesabYar."""

    READ = "hesabyar.read"
    LEDGER_DRAFT = "hesabyar.ledger.draft"
    LEDGER_POST = "hesabyar.ledger.post"
    TAX_DRAFT = "hesabyar.tax.draft"
    TAX_SUBMIT = "hesabyar.tax.submit"
    ADMIN = "hesabyar.admin"


ALL_SCOPES: frozenset[str] = frozenset(s.value for s in Scope)


class TenantContext:
    """Represents the active tenant execution context."""

    __slots__ = ("_is_active", "_name", "_tenant_id")

    def __init__(self, tenant_id: TenantId | str, name: str, is_active: bool = True) -> None:
        self._tenant_id = tenant_id if isinstance(tenant_id, TenantId) else TenantId(tenant_id)
        cleaned_name = name.strip()
        if not cleaned_name:
            raise ValueError("Tenant name cannot be blank.")
        self._name = cleaned_name
        self._is_active = is_active

    @property
    def tenant_id(self) -> TenantId:
        """Tenant strongly-typed identifier."""
        return self._tenant_id

    @property
    def name(self) -> str:
        """Organization or business legal/trading name."""
        return self._name

    @property
    def is_active(self) -> bool:
        """Whether the tenant is currently active and allowed to perform transactions."""
        return self._is_active

    def require_active(self) -> None:
        """Assert that the tenant is active; fail-closed with InvariantViolationError."""
        if not self._is_active:
            raise InvariantViolationError(
                f"Tenant {self._tenant_id} ('{self._name}') is inactive and cannot perform operations.",
                code="TENANT_INACTIVE",
            )

    def matches(self, other_tenant_id: TenantId | str) -> bool:
        """Check whether other_tenant_id matches this tenant context."""
        tid = other_tenant_id if isinstance(other_tenant_id, TenantId) else TenantId(other_tenant_id)
        return self._tenant_id == tid

    def require_matching_tenant(self, other_tenant_id: TenantId | str) -> None:
        """Assert that an entity's tenant matches this context; fail-closed with TenantMismatchError."""
        if not self.matches(other_tenant_id):
            raise TenantMismatchError(
                f"Context tenant {self._tenant_id} does not match target entity tenant {other_tenant_id}."
            )

    def __eq__(self, other: object) -> bool:
        if isinstance(other, TenantContext):
            return (
                self._tenant_id == other._tenant_id
                and self._name == other._name
                and self._is_active == other._is_active
            )
        return False

    def __hash__(self) -> int:
        return hash((self._tenant_id, self._name, self._is_active))

    def __repr__(self) -> str:
        return f"TenantContext(tenant_id={self._tenant_id!r}, name={self._name!r}, is_active={self._is_active})"


class ActorContext:
    """Represents an authenticated actor (user or automated agent) operating within a tenant context.

    Authorization principle:
      Capabilities must be explicitly granted.
      The 'hesabyar.admin' scope does NOT act as a wildcard granting operational
      scopes (such as 'hesabyar.ledger.post' or 'hesabyar.tax.submit').
    """

    __slots__ = ("_actor_id", "_scopes", "_tenant_id")

    def __init__(
        self,
        actor_id: ActorId | str,
        tenant_id: TenantId | str,
        scopes: Iterable[str | Scope] | None = None,
    ) -> None:
        self._actor_id = actor_id if isinstance(actor_id, ActorId) else ActorId(actor_id)
        self._tenant_id = tenant_id if isinstance(tenant_id, TenantId) else TenantId(tenant_id)

        resolved_scopes: set[str] = set()
        if scopes is not None:
            valid_scopes = {s.value for s in Scope}
            for s in scopes:
                scope_str = s.value if isinstance(s, Scope) else str(s).strip()
                if scope_str not in valid_scopes:
                    raise UnauthorizedScopeError(
                        f"Unknown or invalid scope {s!r}. Must be one of: {sorted(valid_scopes)}",
                        code="INVALID_SCOPE",
                    )
                resolved_scopes.add(scope_str)
        self._scopes = frozenset(resolved_scopes)

    @property
    def actor_id(self) -> ActorId:
        """Identifier of the calling actor."""
        return self._actor_id

    @property
    def tenant_id(self) -> TenantId:
        """Identifier of the tenant the actor is operating within."""
        return self._tenant_id

    @property
    def scopes(self) -> set[str]:
        """Return mutable set copy of granted permission scopes."""
        return set(self._scopes)

    @property
    def frozen_scopes(self) -> frozenset[str]:
        """Return immutable view of granted permission scopes."""
        return self._scopes

    @property
    def is_admin(self) -> bool:
        """True if the actor possesses the administrator scope."""
        return Scope.ADMIN.value in self._scopes

    def has_scope(self, scope: str | Scope) -> bool:
        """Check if actor possesses the specific scope.

        ADMIN scope does NOT imply operational capabilities.
        All scopes must be explicitly granted.
        """
        scope_str = scope.value if isinstance(scope, Scope) else str(scope).strip()
        return scope_str in self._scopes

    def require_scope(self, scope: str | Scope) -> None:
        """Enforce that the actor possesses the specified scope; fail-closed with UnauthorizedScopeError."""
        scope_str = scope.value if isinstance(scope, Scope) else str(scope).strip()
        if not self.has_scope(scope_str):
            raise UnauthorizedScopeError(
                f"Actor {self._actor_id} lacks required scope {scope_str!r}. Granted scopes: {sorted(self._scopes)}",
                code="UNAUTHORIZED_SCOPE",
            )

    def require_matching_tenant(self, entity_tenant_id: TenantId | str) -> None:
        """Enforce that the actor's tenant matches the entity's tenant; fail-closed with TenantMismatchError."""
        target_tid = entity_tenant_id if isinstance(entity_tenant_id, TenantId) else TenantId(entity_tenant_id)
        if self._tenant_id != target_tid:
            raise TenantMismatchError(
                f"Actor tenant {self._tenant_id} cannot access entity owned by tenant {target_tid}."
            )

    @classmethod
    def system_admin(
        cls,
        tenant_id: TenantId | str,
        actor_id: ActorId | str = "system",
        additional_scopes: Iterable[str | Scope] | None = None,
    ) -> ActorContext:
        """Create a system administrator context with ADMIN scope.

        Does not grant operational scopes (ledger post, tax submit) unless explicitly specified.
        """
        scopes: set[str | Scope] = {Scope.ADMIN}
        if additional_scopes:
            scopes.update(additional_scopes)
        return cls(actor_id=actor_id, tenant_id=tenant_id, scopes=scopes)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, ActorContext):
            return (
                self._actor_id == other._actor_id
                and self._tenant_id == other._tenant_id
                and self._scopes == other._scopes
            )
        return False

    def __hash__(self) -> int:
        return hash((self._actor_id, self._tenant_id, self._scopes))

    def __repr__(self) -> str:
        return (
            f"ActorContext(actor_id={self._actor_id!r}, "
            f"tenant_id={self._tenant_id!r}, "
            f"scopes={sorted(self._scopes)!r})"
        )
