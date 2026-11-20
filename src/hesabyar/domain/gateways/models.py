"""Domain models for the gateway clients (US-524, Phase 5-3 Wave B).

One client per gateway, each behind the channel registry, for STATUS
RECONCILIATION only. Settlement itself stays file-based for ALL of
them — no gateway is assumed to have a settlement API or a server
webhook (phased design §3, fixed principles; re-verified
2026-11-17). The one documented exception is SalamAPI's webhook for
marketplace order/payment/wallet events, which is the
``basalam_marketplace`` channel's business, not a settlement feed.

Money: all amounts are whole Iranian rials (``int`` — ``MoneyIRR``
digits=0). Every gateway in this story reports rials natively
(SnappPay and DigiPay explicitly; ZarinPal amounts are rials in
both its REST and GraphQL APIs), so no conversion happens at this
boundary. Fee percentages never appear here at all: the fee % is
user-entered per contract version (ADR 0003 round-2 item 1) and
the settlement engine applies it when a payment is recorded.

A gateway-verified payment becomes a ``gateway_payment`` row
(US-521) through the reconciliation service — the receivable is
from the GATEWAY, never the customer (BNPL moves the customer's
debt to the gateway at confirmation; the customer pays the gateway
in instalments, the gateway settles the merchant per contract).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from enum import StrEnum, unique

__all__ = [
    "GatewayCapabilities",
    "GatewayInquiry",
    "GatewayTransaction",
    "GatewayTransactionStatus",
    "RefundResult",
    "SettlementSource",
    "VerifiedPayment",
]


@unique
class GatewayTransactionStatus(StrEnum):
    """Canonical payment status, normalized across gateways.

    Each client maps its gateway's native status vocabulary onto
    this one; an unmapped native status becomes ``UNKNOWN`` (never a
    guess) and the raw value travels in the inquiry's ``message``.
    """

    PAID = "paid"
    """The gateway confirms the money: recordable as a payment."""

    PENDING = "pending"
    """Not final yet (authorized, verified-but-unsettled, awaiting).

    SnappPay's ``VERIFY`` (verified, settle not yet called) and
    ``PENDING`` both land here; DigiPay's refund-inquiry ``Pending``
    is a refund state, not a payment state.
    """

    CANCELLED = "cancelled"
    """Cancelled before settlement (by the payer or the merchant)."""

    REVERTED = "reverted"
    """Reversed by the gateway (e.g. a SnappPay verify without a
    settle, or a DigiPay auto-reverse past the 3-minute window)."""

    REFUNDED = "refunded"
    """Refunded after settlement (a refund the gateway accepted)."""

    FAILED = "failed"
    """The payment failed at the gateway (declined, expired)."""

    UNKNOWN = "unknown"
    """The gateway answered with a status this client cannot map."""


@unique
class SettlementSource(StrEnum):
    """Where a channel's settlement data comes from (never an API)."""

    PORTAL_FILE = "portal_file"
    """Downloaded from the gateway's merchant portal (Excel/CSV)."""

    PORTAL_FILE_AND_EMAIL = "portal_file_and_email"
    """Portal file plus a periodic emailed detail file (SnappPay)."""

    MANUAL_IMPORT = "manual_import"
    """Hand-entered from the portal/SMS/notification (Sadad, SalamPay)."""


@dataclass(frozen=True)
class VerifiedPayment:
    """One payment the gateway itself confirmed.

    This is the reconciliation service's input to
    ``SettlementCommandService.record_payment``: the gateway is the
    authority for the amount and the confirmation time, so both come
    from the gateway's answer, never from the store's claim.
    ``tracking_ref`` is the gateway's tracking/reference number when
    the gateway reports one (ZarinPal ``ref_id``, SnappPay
    ``referenceNumber``, DigiPay ``trackingCode``) — it is the
    bank-matching hint of US-522/523, never the payment's identity:
    identity is ``(channel, gateway_transaction_id)``.
    """

    gateway_transaction_id: str
    gross_irr: int
    gateway_confirmed_at: datetime.datetime
    status: GatewayTransactionStatus = GatewayTransactionStatus.PAID
    store_order_id: str | None = None
    tracking_ref: str | None = None
    raw: dict[str, object] = field(default_factory=dict)
    """The gateway's own fields for audit/debug (never credentials)."""


@dataclass(frozen=True)
class GatewayInquiry:
    """The answer of a status inquiry for one payment."""

    gateway_transaction_id: str
    status: GatewayTransactionStatus
    amount_irr: int | None = None
    """The amount the gateway reports (None when it reports none)."""

    message: str | None = None
    """The gateway's native status/message, for the operator."""


@dataclass(frozen=True)
class RefundResult:
    """The answer of a refund/revert call."""

    refund_id: str
    amount_irr: int
    status: GatewayTransactionStatus = GatewayTransactionStatus.REFUNDED


@dataclass(frozen=True)
class GatewayTransaction:
    """One row of a gateway transaction list (ZarinPal GraphQL).

    ``confirmed_at`` is the gateway's ``created_at``; the settlement
    engine re-derives the sales period from it. Only ZarinPal
    exposes a list API — every other client raises
    ``GatewayUnsupportedOperationError`` for listing.
    """

    gateway_transaction_id: str
    status: GatewayTransactionStatus
    amount_irr: int
    confirmed_at: datetime.datetime | None = None
    description: str | None = None


@dataclass(frozen=True)
class GatewayCapabilities:
    """What one channel can and cannot do (phased design §3 §1).

    This is the honest surface the UI and the operator see: an API
    channel names its operations; a manual channel says so plainly
    and names its settlement source. ``has_settlement_api`` is
    False for every gateway today — the field exists so a future
    verified settlement API can be switched on per channel without
    a code change, and so no code anywhere can silently assume one
    exists. ``open_questions`` carries the live UNVERIFIED items
    for the channel (DigiPay's settlement output, Sadad's Excel
    output, …) — recorded, never invented.
    """

    channel_key: str
    has_payment_api: bool
    supports_verify: bool
    supports_inquiry: bool
    supports_refund: bool
    supports_list: bool
    has_settlement_api: bool = False
    has_server_webhook: bool = False
    settlement_source: SettlementSource = SettlementSource.PORTAL_FILE
    settlement_model_note: str = ""
    """The contract-derived settlement shape in words (never numbers
    the contract does not state — e.g. SalamPay's TWO models)."""

    open_questions: tuple[str, ...] = ()
    """UNVERIFIED items for this channel, as operator-facing text."""
