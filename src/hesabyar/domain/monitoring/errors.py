"""Monitoring domain errors (US-704).

Every error carries a stable machine-readable ``code`` so the REST
layer can map it to a problem-details response without string
matching (see ``hesabyar.interfaces.rest.errors``).
"""

from __future__ import annotations

from hesabyar.domain.common.errors import InvariantViolationError


class MonitoringError(InvariantViolationError):
    """Base class for monitoring-domain failures."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message, code=code)
        self.details: dict[str, object] = details or {}


class MonitoringValidationError(MonitoringError):
    """A channel / token payload failed validation (fail-closed).

    ``details["field_errors"]`` maps field name -> human-readable
    problem; the REST layer renders it as a 422.
    """

    def __init__(self, message: str, *, field_errors: dict[str, str]) -> None:
        super().__init__(
            message,
            code="MONITORING_VALIDATION_FAILED",
            details={"field_errors": field_errors},
        )
        self.field_errors = field_errors


class MonitoringNotFoundError(MonitoringError):
    """No alert / channel / token record with the requested id exists."""

    def __init__(self, kind: str, ref: str) -> None:
        super().__init__(
            f"Monitoring {kind} {ref!r} does not exist.",
            code="MONITORING_NOT_FOUND",
            details={"kind": kind, "ref": ref},
        )


class MonitoringAlertAlreadyOpenError(MonitoringError):
    """A firing alert with this dedup key already exists (race backstop).

    The unique partial index on ``monitoring_alerts`` raises this
    when two evaluations fire the same rule concurrently; the
    service treats it as "already open", never as a failure.
    """

    def __init__(self, dedup_key: str) -> None:
        super().__init__(
            f"An open alert with dedup key {dedup_key!r} already exists.",
            code="MONITORING_ALERT_ALREADY_OPEN",
            details={"dedup_key": dedup_key},
        )
