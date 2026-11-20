"""Gateway-client domain errors (US-524, Phase 5-3 Wave B).

Codes follow the ``GATEWAY_*`` namespace. The REST layer maps these
in ``interfaces.rest.errors``.

No error in this module ever carries credential material: details
name fields and gateway error codes, never secrets.
"""

from __future__ import annotations

from typing import Any

from hesabyar.domain.common.errors import DomainError


class GatewayError(DomainError):
    """Base exception for the gateway-client bounded context."""


class GatewayUnreachableError(GatewayError):
    """Raised when the gateway cannot be reached (timeout, DNS, 5xx).

    Transient by nature: the caller may retry later. HTTP 502.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="GATEWAY_UNREACHABLE", details=details)


class GatewayAuthError(GatewayError):
    """Raised when the gateway rejects the stored credentials (401/403).

    A configuration problem, not a transient one: the operator must
    re-issue the credentials in the gateway's portal and rotate them
    in the channel registry. HTTP 422.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="GATEWAY_AUTH_FAILED", details=details)


class GatewayRejectedError(GatewayError):
    """Raised when the gateway rejects a call for a business reason.

    The gateway answered, understood the call, and said no (a bad
    token, an already-reverted payment, an invalid amount, …). The
    provider's own error code/message travel in ``details`` (never
    any secret). HTTP 422.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="GATEWAY_REJECTED", details=details)


class GatewayInvalidResponseError(GatewayError):
    """Raised when the gateway's answer does not match its contract.

    Malformed JSON, a missing required field, or an amount that is
    not an integer number of rials. The payment is NOT recorded on a
    response this client cannot parse — fail closed. HTTP 502.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="GATEWAY_INVALID_RESPONSE", details=details)


class GatewayNotConfiguredError(GatewayError):
    """Raised when a channel has no usable gateway configuration.

    Covers: no credentials stored, a required credential field
    missing, a channel that is disabled, and a reserved channel
    (``torob_marketplace``) used as a payment channel. HTTP 422.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="GATEWAY_NOT_CONFIGURED", details=details)


class GatewayUnsupportedOperationError(GatewayError):
    """Raised when a channel has no API for the requested operation.

    Sadad and SalamPay have no inquiry/refund API at all (store-side
    capture + manual import only); custom channels have no API by
    design (phased design §1.7); marketplace channels are not
    payment channels. HTTP 422.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="GATEWAY_OPERATION_UNSUPPORTED", details=details)


class GatewayVerificationMismatchError(GatewayError):
    """Raised when the gateway's answer disagrees with what was expected.

    The amount the gateway reports differs from the amount HesabYar
    expected, or a verify call returns a transaction for a different
    order. The payment is NOT recorded — the mismatch goes to the
    operator, never silently absorbed. HTTP 422.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="GATEWAY_VERIFICATION_MISMATCH", details=details)
