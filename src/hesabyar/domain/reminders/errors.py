"""Reminder domain errors (US-564, Phase 5-7 ۵خ W3).

Every error carries a stable machine-readable ``code`` so the REST
layer can map it to a problem-details response without string
matching, and a ``details`` mapping for the values a caller or an
operator needs to recover (never credential material, never a
phone number beyond what the caller supplied).
"""

from __future__ import annotations

from hesabyar.domain.common.errors import InvariantViolationError


class ReminderError(InvariantViolationError):
    """Base class for reminder-domain failures."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message, code=code)
        self.details: dict[str, object] = details or {}


class ReminderValidationError(ReminderError):
    """A reminder-preferences payload failed validation (fail-closed).

    ``details["field_errors"]`` maps field name -> human-readable
    problem; the REST layer renders it as a 422.
    """

    def __init__(self, message: str, *, field_errors: dict[str, str]) -> None:
        super().__init__(
            message,
            code="REMINDER_VALIDATION_FAILED",
            details={"field_errors": field_errors},
        )
        self.field_errors = field_errors


class ReminderAlreadySentError(ReminderError):
    """A reminder for this idempotency key was already sent (dedup).

    Raised by the repository when an insert would violate the
    sent-dedup index. The service treats it as "already sent" —
    the job never crashes on a race, it just skips.
    """

    def __init__(self, *, idempotency_key: str) -> None:
        super().__init__(
            f"A reminder with idempotency key {idempotency_key!r} was already sent.",
            code="REMINDER_ALREADY_SENT",
            details={"idempotency_key": idempotency_key},
        )
