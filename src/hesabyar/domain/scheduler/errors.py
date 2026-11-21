"""Domain errors for the scheduler (US-560)."""

from __future__ import annotations

from typing import Any

from hesabyar.domain.common.errors import HesabYarError

SCHEDULE_INVALID = "SCHEDULE_INVALID"


class InvalidScheduleError(HesabYarError):
    """Raised when a job schedule spec is malformed or unsatisfiable."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=SCHEDULE_INVALID, details=details)
