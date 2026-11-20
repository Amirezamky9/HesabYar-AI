"""Ports for the gateway clients (US-524, Phase 5-3 Wave B).

Domain and application code depend only on these protocols. The
production implementations live in
``hesabyar.infrastructure.gateways`` (one httpx client per gateway
behind a factory keyed on the channel registry).

All ports are synchronous (there is no async infrastructure in the
repository yet). All money is integer IRR. No method on these ports
takes or returns credential material: the factory builds a client
from already-decrypted credentials, and the clients never log or
return them.

``payment_ref`` is the gateway-side reference the operation needs
— its meaning is per gateway, and each client's docstring names it:

- ZarinPal: the ``authority`` string (``A…``/``S…`` + 35 chars) for
  verify/inquiry/reverse; the GraphQL session id for refunds.
- SnappPay / TorobPay: the ``paymentToken``.
- DigiPay: the ticket's ``providerId`` / tracking code per call.
- Manual channels (Sadad, SalamPay, custom): no ref is accepted —
  every operation raises ``GatewayUnsupportedOperationError``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from hesabyar.domain.gateways.models import (
    GatewayCapabilities,
    GatewayInquiry,
    GatewayTransaction,
    RefundResult,
    VerifiedPayment,
)
from hesabyar.domain.integrations.models import PaymentChannel

__all__ = ["GatewayClientFactoryPort", "GatewayClientPort"]


class GatewayClientPort(Protocol):
    """One gateway's payment API, for status reconciliation.

    Clients that do not support an operation raise
    ``GatewayUnsupportedOperationError`` — the caller never has to
    guess from a None. ``verify_payment`` is the only call that
    proves a payment: a callback (store or gateway) is never trusted
    until this returns ``PAID`` for the expected amount.
    """

    @property
    def channel_key(self) -> str:
        """The registry channel key this client serves."""
        ...

    @property
    def capabilities(self) -> GatewayCapabilities:
        """What this channel can and cannot do (see the model)."""
        ...

    def verify_payment(
        self,
        payment_ref: str,
        *,
        expected_amount_irr: int | None = None,
    ) -> VerifiedPayment:
        """Verify a payment with the gateway and return the proof.

        Raises ``GatewayVerificationMismatchError`` when the
        gateway's amount disagrees with ``expected_amount_irr``.
        For SnappPay this performs verify + settle (settle is
        mandatory after a successful verify); for TorobPay the
        second settle call is per-contract (``requires_settle``).
        """
        ...

    def inquire_status(self, payment_ref: str) -> GatewayInquiry:
        """Ask the gateway for a payment's current status.

        The polling primitive: it fills the gap a lost callback
        leaves. Never records anything by itself.
        """
        ...

    def refund(
        self,
        payment_ref: str,
        amount_irr: int,
        *,
        reason: str | None = None,
    ) -> RefundResult:
        """Refund (or revert) a payment through the gateway.

        SnappPay reverts the FULL payment — a partial amount is
        refused rather than silently escalated to a full revert.
        DigiPay refunds accept partial amounts; a 200 there means
        only \"request registered\" — finality needs a refund
        inquiry (the DigiPay client's ``refund`` returns
        ``PENDING``-aware results; see its docstring).
        """
        ...

    def list_transactions(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[GatewayTransaction]:
        """List the gateway's transactions (ZarinPal GraphQL only).

        Every other client raises ``GatewayUnsupportedOperationError``.
        """
        ...

    def close(self) -> None:
        """Close the underlying HTTP client."""
        ...


class GatewayClientFactoryPort(Protocol):
    """Build the right client for a registry channel.

    The factory is the ONLY place that maps a channel key to a
    client implementation (the same seed-data pattern as the
    channel registry: keys are data, and the factory is the seam).
    Marketplace channels (``basalam_marketplace``,
    ``torob_marketplace``) are not payment channels — building a
    client for one raises ``GatewayUnsupportedOperationError``.
    Unknown/custom channel keys get the manual client (design §1.7:
    custom channels have no API, only manual capture + import).
    """

    def build(
        self,
        channel: PaymentChannel,
        credentials: Mapping[str, object],
    ) -> GatewayClientPort:
        """Build a client for ``channel`` from its credentials.

        ``credentials`` is the channel's decrypted credential
        payload (a JSON object: secrets plus optional non-secret
        overrides such as ``base_url``). Missing required fields
        raise ``GatewayNotConfiguredError`` naming the fields.
        """
        ...

    def requires_credentials(self, channel: PaymentChannel) -> bool:
        """True when the channel's client needs stored credentials.

        API channels do; manual channels (Sadad, SalamPay, custom)
        and marketplace channels do not.
        """
        ...

    def capabilities_for(self, channel: PaymentChannel) -> GatewayCapabilities:
        """Return a channel's capabilities without credentials.

        Capabilities are static per channel key — no HTTP, no
        secrets — so the UI can show them before a channel is
        configured.
        """
        ...
