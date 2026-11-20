"""Gateway clients: one client per payment gateway (US-524).

Status reconciliation only — settlement stays file-based for every
gateway. See :mod:`hesabyar.domain.gateways.models` for the
principles and :mod:`hesabyar.infrastructure.gateways` for the
implementations.
"""

from __future__ import annotations

from hesabyar.domain.gateways.credentials import (
    REQUIRED_CREDENTIAL_FIELDS,
    parse_gateway_credentials,
    require_credential_fields,
)
from hesabyar.domain.gateways.errors import (
    GatewayAuthError,
    GatewayError,
    GatewayInvalidResponseError,
    GatewayNotConfiguredError,
    GatewayRejectedError,
    GatewayUnreachableError,
    GatewayUnsupportedOperationError,
    GatewayVerificationMismatchError,
)
from hesabyar.domain.gateways.models import (
    GatewayCapabilities,
    GatewayInquiry,
    GatewayTransaction,
    GatewayTransactionStatus,
    RefundResult,
    SettlementSource,
    VerifiedPayment,
)
from hesabyar.domain.gateways.ports import GatewayClientFactoryPort, GatewayClientPort

__all__ = [
    "GatewayAuthError",
    "GatewayCapabilities",
    "GatewayClientFactoryPort",
    "GatewayClientPort",
    "GatewayError",
    "GatewayInquiry",
    "GatewayInvalidResponseError",
    "GatewayNotConfiguredError",
    "GatewayRejectedError",
    "GatewayTransaction",
    "GatewayTransactionStatus",
    "GatewayUnreachableError",
    "GatewayUnsupportedOperationError",
    "GatewayVerificationMismatchError",
    "REQUIRED_CREDENTIAL_FIELDS",
    "RefundResult",
    "SettlementSource",
    "VerifiedPayment",
    "parse_gateway_credentials",
    "require_credential_fields",
]
