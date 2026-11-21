"""Domain errors for recurring invoices/journals (US-563, Phase 5-7 ۵خ W3)."""

from __future__ import annotations

from typing import Any

from hesabyar.domain.common.errors import HesabYarError

RECURRING_TEMPLATE_INVALID = "RECURRING_TEMPLATE_INVALID"
RECURRING_OCCURRENCE_CONFLICT = "RECURRING_OCCURRENCE_CONFLICT"


class InvalidRecurringTemplateError(HesabYarError):
    """Raised when a recurring template (or its schedule/payload) is invalid."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=RECURRING_TEMPLATE_INVALID, details=details)


class RecurringOccurrenceConflictError(HesabYarError):
    """Raised when an occurrence claim collides with an existing row.

    The (template, scheduled date) unique constraint is the
    idempotency backstop: two generator runs racing on the same due
    date both try to claim it, and the loser gets this error instead
    of a duplicate occurrence. The generator treats it as "already
    generated" and moves on — it is never a failure.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=RECURRING_OCCURRENCE_CONFLICT, details=details)
