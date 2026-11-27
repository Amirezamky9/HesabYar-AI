"""Schemas for the gateway reconciliation routes (US-524).

No credential material ever appears in a request or a response:
credentials are rotated under ``/integrations`` and only
``has_credentials`` is visible. Amounts are integer IRR.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from hesabyar.domain.gateways.models import GatewayCapabilities, GatewayInquiry
from hesabyar.interfaces.rest.schemas.settlement import (
    GatewayPaymentResponse,
    SettlementTrancheResponse,
)

__all__ = [
    "GatewayCapabilitiesResponse",
    "GatewayInquiryResponse",
    "ReconcileRequest",
    "ReconcileResponse",
    "RecordCallbackRequest",
    "RefundPaymentRequest",
    "RefundPaymentResponse",
    "RotateChannelCredentialsRequest",
    "VerifiedCallbackResponse",
    "VerifyAndRecordRequest",
]


class GatewayCapabilitiesResponse(BaseModel):
    """What one channel can and cannot do (no secrets, no HTTP)."""

    channel_key: str = Field(description="Registry channel slug")
    has_payment_api: bool = Field(description="True when the channel has a payment API")
    supports_verify: bool = Field(description="Verify with the gateway is available")
    supports_inquiry: bool = Field(description="Status inquiry is available")
    supports_refund: bool = Field(description="Refund through the gateway is available")
    supports_list: bool = Field(description="Transaction listing is available (ZarinPal only)")
    has_settlement_api: bool = Field(description="False for every gateway today")
    has_server_webhook: bool = Field(description="False for every gateway today")
    settlement_source: str = Field(description="Where settlement data comes from (never an API)")
    settlement_model_note: str = Field(description="The contract-derived settlement shape, in words")
    open_questions: list[str] = Field(description="UNVERIFIED items for this channel")

    @classmethod
    def from_capabilities(cls, capabilities: GatewayCapabilities) -> GatewayCapabilitiesResponse:
        """Map domain capabilities to their response."""
        return cls(
            channel_key=capabilities.channel_key,
            has_payment_api=capabilities.has_payment_api,
            supports_verify=capabilities.supports_verify,
            supports_inquiry=capabilities.supports_inquiry,
            supports_refund=capabilities.supports_refund,
            supports_list=capabilities.supports_list,
            has_settlement_api=capabilities.has_settlement_api,
            has_server_webhook=capabilities.has_server_webhook,
            settlement_source=capabilities.settlement_source.value,
            settlement_model_note=capabilities.settlement_model_note,
            open_questions=list(capabilities.open_questions),
        )


class GatewayInquiryResponse(BaseModel):
    """The answer of a status inquiry for one payment."""

    gateway_transaction_id: str = Field(description="Gateway-side transaction id")
    status: str = Field(description="Canonical status: paid/pending/cancelled/reverted/refunded/failed/unknown")
    amount_irr: int | None = Field(description="Amount the gateway reports, when any")
    message: str | None = Field(description="The gateway's native status/message")

    @classmethod
    def from_inquiry(cls, inquiry: GatewayInquiry) -> GatewayInquiryResponse:
        """Map a domain inquiry to its response."""
        return cls(
            gateway_transaction_id=inquiry.gateway_transaction_id,
            status=inquiry.status.value,
            amount_irr=inquiry.amount_irr,
            message=inquiry.message,
        )


class VerifyAndRecordRequest(BaseModel):
    """Verify a payment with the gateway, then record it."""

    payment_ref: str = Field(
        description="Gateway-side reference: ZarinPal authority, SnappPay/TorobPay payment token, DigiPay providerId"
    )
    expected_amount_irr: int | None = Field(
        default=None,
        description="Expected amount in rials; a gateway answer that disagrees records nothing",
    )
    store_order_id: str | None = Field(default=None, description="Store order id, when known")
    store_payment_id: str | None = Field(default=None, description="Store payment id, when known")


class RecordCallbackRequest(BaseModel):
    """Record a callback the store already verified with the gateway."""

    gateway_transaction_id: str = Field(description="Gateway-side transaction id")
    gross_irr: int = Field(description="Sale amount in whole rials", gt=0)
    gateway_confirmed_at: datetime = Field(description="When the gateway confirmed the transaction")
    source: str = Field(description="store_webhook (store-verified), manual_entry, or manual_import")
    store_order_id: str | None = Field(default=None, description="Store order id, when known")
    store_payment_id: str | None = Field(default=None, description="Store payment id, when known")
    tracking_ref: str | None = Field(default=None, description="Gateway tracking/reference number, when reported")


class ReconcileRequest(BaseModel):
    """Inquire one payment's status and converge the recorded row."""

    gateway_transaction_id: str = Field(description="Gateway-side transaction id")
    payment_ref: str | None = Field(
        default=None,
        description=(
            "Gateway reference for the inquiry when it differs from the transaction id (e.g. ZarinPal authority)"
        ),
    )
    expected_amount_irr: int | None = Field(
        default=None,
        description="Expected amount in rials; used when the gateway reports none, and reconciled when it reports one",
    )
    store_order_id: str | None = Field(default=None, description="Store order id, when known")


class RefundPaymentRequest(BaseModel):
    """Refund a payment through its gateway, then cancel the row (full refunds only)."""

    payment_id: str = Field(description="Recorded gateway-payment row id")
    amount_irr: int = Field(description="Refund amount in rials (must equal the payment's gross)")
    reason: str | None = Field(default=None, description="Refund reason (recorded on the cancel)")


class VerifiedCallbackResponse(BaseModel):
    """A recorded (or already-recorded) gateway-verified payment."""

    payment: GatewayPaymentResponse = Field(description="The gateway payment row")
    tranches: list[SettlementTrancheResponse] = Field(description="Its derived tranche schedule")
    created: bool = Field(description="False when the payment already existed (idempotent replay)")


class ReconcileResponse(BaseModel):
    """One reconciliation run: what the gateway said, what changed."""

    outcome: str = Field(
        description="recorded | already_recorded | cancelled | already_cancelled | pending | failed | unknown"
    )
    inquiry: GatewayInquiryResponse = Field(description="The gateway's answer")
    payment: GatewayPaymentResponse | None = Field(
        default=None, description="The recorded payment row, when one exists"
    )
    tranches: list[SettlementTrancheResponse] = Field(
        default_factory=list, description="The payment's tranche schedule, when recorded"
    )


class RefundPaymentResponse(BaseModel):
    """A gateway refund + the settlement cancel that followed it."""

    refund_id: str = Field(description="Gateway-side refund id")
    amount_irr: int = Field(description="Refunded amount in whole rials")
    status: str = Field(description="Refund status (refunded, or pending for DigiPay until inquiry confirms)")
    payment: GatewayPaymentResponse = Field(description="The cancelled payment row")
    cancelled_tranche_ids: list[str] = Field(description="Open tranches that were cancelled")


class RotateChannelCredentialsRequest(BaseModel):
    """Replace a channel's stored gateway credentials (encrypted at rest)."""

    credentials: dict[str, Any] = Field(
        description=(
            "Gateway credential payload: secrets (client_id, client_secret, username, "
            "password, merchant_id, access_token, terminal_id — per gateway) plus "
            "optional non-secret overrides (base_url, requires_settle, paths, scope). "
            "Never echoed back."
        )
    )
