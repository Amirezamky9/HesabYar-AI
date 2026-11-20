"""Manual gateway client (US-524): channels with no payment API.

Sadad (Melli IPG) and SalamPay (in-person QR/link) have no
inquiry/refund API at all — payments are captured store-side and
reconciled from the portal/SMS/notification by hand, and
settlement data comes from a manual import + bank matching.
Custom channels (design §1.7) have no API by definition.

This client is the honest shape of that: every API operation
raises ``GatewayUnsupportedOperationError`` naming the manual path
instead, so no caller can mistake a manual channel for a connected
one. The capabilities carry each channel's settlement shape and
open questions.
"""

from __future__ import annotations

from collections.abc import Sequence

from hesabyar.domain.gateways.errors import GatewayUnsupportedOperationError
from hesabyar.domain.gateways.models import (
    GatewayCapabilities,
    GatewayInquiry,
    GatewayTransaction,
    RefundResult,
    SettlementSource,
    VerifiedPayment,
)

__all__ = ["ManualGatewayClient"]

#: Per-channel capability facts for the manual channels.
_MANUAL_CAPABILITIES: dict[str, GatewayCapabilities] = {
    "sadad": GatewayCapabilities(
        channel_key="sadad",
        has_payment_api=False,
        supports_verify=False,
        supports_inquiry=False,
        supports_refund=False,
        supports_list=False,
        has_settlement_api=False,
        has_server_webhook=False,
        settlement_source=SettlementSource.MANUAL_IMPORT,
        settlement_model_note=(
            "Daily lump settlement through the Shaparak/Paya cycle; the exact timing comes from the channel's contract."
        ),
        open_questions=(
            "Panel Excel output is UNVERIFIED — check the Sadad merchant panel for an export.",
            "Test-environment existence is UNVERIFIED — ask Sadad support.",
            "The '24-hour settlement' figure belongs to BehPardakht Mellat, not Sadad — never attribute it here.",
        ),
    ),
    "salampay": GatewayCapabilities(
        channel_key="salampay",
        has_payment_api=False,
        supports_verify=False,
        supports_inquiry=False,
        supports_refund=False,
        supports_list=False,
        has_settlement_api=False,
        has_server_webhook=False,
        settlement_source=SettlementSource.MANUAL_IMPORT,
        settlement_model_note=(
            "TWO contract models — read from the contract, never assumed: "
            "(1) standard: 10-working-day lump at 5% fee; (2) seller "
            "credit-line: monthly settlement at installment due dates at "
            "zero fee (the seller funds the credit and carries the risk). "
            "The Mellat-facility path's settlement timing is UNVERIFIED."
        ),
        open_questions=(
            "Panel export format is UNVERIFIED.",
            "Fee VAT treatment is UNVERIFIED (no mention in the official article).",
            "Mellat-facility settlement timing is UNVERIFIED.",
        ),
    ),
}


def _custom_capabilities(channel_key: str) -> GatewayCapabilities:
    """Capabilities for a custom (operator-defined) channel."""
    return GatewayCapabilities(
        channel_key=channel_key,
        has_payment_api=False,
        supports_verify=False,
        supports_inquiry=False,
        supports_refund=False,
        supports_list=False,
        has_settlement_api=False,
        has_server_webhook=False,
        settlement_source=SettlementSource.MANUAL_IMPORT,
        settlement_model_note=(
            "Custom channel: the settlement model and fee % are the ones "
            "the operator entered on the channel's contract."
        ),
        open_questions=(),
    )


def manual_capabilities_for(channel_key: str) -> GatewayCapabilities:
    """Return the manual capabilities for a channel key."""
    return _MANUAL_CAPABILITIES.get(channel_key) or _custom_capabilities(channel_key)


class ManualGatewayClient:
    """A channel with no API: store-side capture + manual import only."""

    def __init__(self, channel_key: str) -> None:
        self._channel_key = channel_key

    @property
    def channel_key(self) -> str:
        """The registry channel key this client serves."""
        return self._channel_key

    @property
    def capabilities(self) -> GatewayCapabilities:
        """What this channel can and cannot do (manual only)."""
        return manual_capabilities_for(self._channel_key)

    def _unsupported(self, operation: str) -> GatewayUnsupportedOperationError:
        return GatewayUnsupportedOperationError(
            f"Channel {self._channel_key!r} has no payment API: {operation} "
            "is not available. Record the payment from the store capture "
            "or a manual import instead (settlement stays file-based).",
            details={"gateway": self._channel_key, "operation": operation},
        )

    def verify_payment(
        self,
        payment_ref: str,
        *,
        expected_amount_irr: int | None = None,
    ) -> VerifiedPayment:
        """Not available: no verification API exists for this channel."""
        raise self._unsupported("verify")

    def inquire_status(self, payment_ref: str) -> GatewayInquiry:
        """Not available: no inquiry API exists for this channel."""
        raise self._unsupported("inquiry")

    def refund(
        self,
        payment_ref: str,
        amount_irr: int,
        *,
        reason: str | None = None,
    ) -> RefundResult:
        """Not available: no refund API exists for this channel."""
        raise self._unsupported("refund")

    def list_transactions(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[GatewayTransaction]:
        """Not available: no transaction-list API exists for this channel."""
        raise self._unsupported("list_transactions")

    def close(self) -> None:
        """No HTTP client to close."""
        return None
