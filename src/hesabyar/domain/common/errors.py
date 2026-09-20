"""Domain exceptions and error hierarchy for HesabYar."""

from typing import Any


class HesabYarError(Exception):
    """Root exception for all HesabYar system, domain, and application errors."""

    def __init__(
        self,
        message: str,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or self.__class__.__name__
        self.details = details or {}

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(message={self.message!r}, code={self.code!r}, details={self.details!r})"


class DomainError(HesabYarError):
    """Base exception for domain logic, entity lifecycle, and business rule violations."""


class InvariantViolationError(DomainError):
    """Raised when an accounting invariant or entity integrity rule is violated."""


class TenantMismatchError(DomainError):
    """Raised when operations cross tenant isolation boundaries or tenant IDs mismatch."""


class InvalidMoneyError(DomainError):
    """Raised when money creation, arithmetic, or rounding violates financial rules."""


class InvalidDateError(DomainError):
    """Raised when date parsing, Jalali conversion, or fiscal period bounds are invalid."""


class InvalidIdentifierError(DomainError):
    """Raised when an entity or tenant identifier fails format or validation rules."""


class UnauthorizedScopeError(DomainError):
    """Raised when an actor lacks the required permission scope for a domain operation."""
