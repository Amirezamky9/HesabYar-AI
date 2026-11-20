"""Manual-import batch domain errors (US-554, Phase 5-6 ۵ح).

Codes follow the ``MANUAL_IMPORT_*`` namespace. The REST layer maps
these in ``interfaces.rest.errors``.
"""

from __future__ import annotations

from typing import Any

from hesabyar.domain.common.errors import DomainError


class ManualImportError(DomainError):
    """Base exception for the manual-import batch bounded context."""


class ManualImportNotFoundError(ManualImportError):
    """Raised when a batch id does not exist for the tenant."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="MANUAL_IMPORT_NOT_FOUND", details=details)


class ManualImportInvalidStateError(ManualImportError):
    """Raised when a batch transition is not allowed from its current state."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="MANUAL_IMPORT_INVALID_STATE", details=details)


class ManualImportFileError(ManualImportError):
    """Raised when an uploaded file cannot be detected/parsed at all.

    Per-row problems are NOT raised here — they land on the
    underlying import's rows and the batch preview shows them. This
    error is for whole-file failures: unknown format, unreadable
    content, no data rows, a header row that cannot be found.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="MANUAL_IMPORT_FILE_ERROR", details=details)


class ManualImportDuplicateError(ManualImportError):
    """Raised when the same file was already imported for the same source.

    The batch endpoints return the existing batch instead of raising
    (a re-upload is a no-op); this error is for callers that need the
    duplicate to fail loudly.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="MANUAL_IMPORT_DUPLICATE", details=details)


class SettlementColumnMappingNotFoundError(ManualImportError):
    """Raised when a saved settlement column mapping id does not exist."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="SETTLEMENT_MAPPING_NOT_FOUND", details=details)


class SettlementColumnMappingInvalidError(ManualImportError):
    """Raised when a settlement column mapping is missing a required field."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="SETTLEMENT_MAPPING_INVALID", details=details)
