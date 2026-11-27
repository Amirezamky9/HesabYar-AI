"""Granular API-key scopes (design-eng spec §14.2).

Capabilities are exact strings such as ``company:read``. There is no
wildcard and no ``admin`` scope: an ``admin`` label must not silently
imply sensitive posting, reversal or tax-submission capabilities, so
every operation requires the exact permission it needs.
"""

from __future__ import annotations

from collections.abc import Iterable

from hesabyar.infrastructure.auth.errors import UnknownScopeError

# The full spec §14.2 permission catalog. Keys are issued only with
# scopes from this set (see validate_scopes).
KNOWN_SCOPES: frozenset[str] = frozenset(
    {
        "company:read",
        "account:read",
        "ledger:read",
        "ledger:draft:create",
        "ledger:draft:update",
        "ledger:post",
        "ledger:reverse",
        "invoice:read",
        "invoice:draft:create",
        "invoice:finalize",
        "payment:read",
        "banking:read",
        "banking:import",
        "banking:reconcile",
        "reports:read",
        "standards:read",
        "standards:validate",
        "tax:read",
        "tax:decide",
        "moadian:read",
        "moadian:submit",
        "compliance:read",
        "compliance:draft:create",
        "compliance:submit",
        "review:read",
        "review:resolve",
        "review:file",
        # Phase 6 (US-602): the MCP list_pending_review_items tool reads
        # the review queue through the workflow scope (spec §13.4).
        "workflow:read",
        "packaging:read",
        "packaging:manage",
        "cashbox:read",
        "cashbox:count",
        "cashbox:manage",
        "integrations:read",
        "integrations:manage",
        "settlement:read",
        "settlement:manage",
        "cheques:read",
        "cheques:write",
        "cheques:transition",
        "installments:read",
        "installments:write",
        "sms:manage",
        "reminders:manage",
        "jobs:manage",
        "rules:read",
        "rules:manage",
        "api_keys:manage",
        "users:manage",
        "settings:manage",
        "mcp:connect",
        # Phase 7 (US-704): the operator monitoring panel reads the
        # snapshot/alerts with monitoring:read and manages channels,
        # token records and alert lifecycle with monitoring:manage.
        "monitoring:read",
        "monitoring:manage",
    }
)


def parse_scopes(raw: str | None) -> frozenset[str]:
    """Parse the comma-separated scope column into a set.

    Lenient on purpose: stored values are split, stripped and deduplicated
    without catalog validation, so a key issued by a newer deployment
    (with scopes this build does not know yet) still authenticates and
    its known scopes still enforce.
    """
    if not raw:
        return frozenset()
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def format_scopes(scopes: Iterable[str]) -> str:
    """Render scopes for storage: sorted, comma-separated, deduplicated."""
    return ",".join(sorted(set(scopes)))


def validate_scopes(scopes: Iterable[str]) -> frozenset[str]:
    """Validate scopes against the catalog, failing closed on unknowns.

    Used at key issuance: a typo'd scope must be rejected loudly at
    issuance time, not silently granted nothing at request time.
    """
    validated: set[str] = set()
    for scope in scopes:
        scope = scope.strip()
        if not scope:
            continue
        if scope not in KNOWN_SCOPES:
            raise UnknownScopeError(scope)
        validated.add(scope)
    return frozenset(validated)


def missing_scopes(granted: Iterable[str], required: Iterable[str]) -> list[str]:
    """Return the required scopes not present in ``granted`` (sorted)."""
    granted_set = set(granted)
    return sorted(scope for scope in required if scope not in granted_set)
