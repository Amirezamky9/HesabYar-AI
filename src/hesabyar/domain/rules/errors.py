"""Domain errors for the user rules engine (Phase 5-7 ۵خ W2, US-561).

Stable codes follow the ``RULE_*`` taxonomy; the REST layer maps
them onto typed problem-details responses (see
``hesabyar.interfaces.rest.errors``). Every error here is a
definition-time failure — a rule that cannot be validated is never
stored, and a rule that cannot be executed is recorded as a failed
execution, never silently skipped.
"""

from __future__ import annotations

from typing import Any

from hesabyar.domain.common.errors import DomainError


class RuleError(DomainError):
    """Base exception for rules-engine domain failures."""


class RuleDefinitionError(RuleError):
    """Raised when a rule definition fails validation.

    Covers the closed action set (an unknown action kind), the
    condition DSL (an unknown operator, a malformed tree, a bad
    field path, a node/depth limit), an unknown event type, and
    invalid per-action parameters. The message names the problem;
    it never echoes user code (there is none — the DSL has no eval).
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="RULE_INVALID", details=details)
