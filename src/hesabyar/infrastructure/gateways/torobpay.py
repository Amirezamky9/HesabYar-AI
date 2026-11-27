"""TorobPay gateway client (US-524).

OAuth REST payment API (Verify/Refund/Inquiry/Callback) — verified
via the open-source payvand client (MIT, v1.4.0) and the official
WooCommerce plugin (v4.8.17). There are NO public docs: the base
URL and the endpoint paths below come from payvand and are
⚠️ UNVERIFIED against TorobPay's official merchant docs. The
operator MUST confirm them against the docs TorobPay issues after
merchant approval and override them in the channel configuration
when they differ — nothing here is hard-coded as fact.

Per payvand, TorobPay serves the same ``/api/online/...`` paths as
SnappPay, with OAuth client id/secret + merchant username/password
(the SnappPay pattern), a mandatory basket, and NO public sandbox.
Whether a second settle call is needed after verify is
PER-CONTRACT (payvand's warning: a missing settle only surfaces as
a reversal once the window closes) — ``requires_settle`` comes from
the channel's configuration and defaults to False.

Settlement stays file + bank matching: no settlement API exists in
any source, and the panel's Excel/CSV output is ⚠️ UNVERIFIED.
The Torob ORDER API (marketplace sync) is a different integration
entirely — see the RESERVED ``torob_marketplace`` channel, which
stays disabled until that API is verified and is never mixed with
this payment channel.

Credentials (encrypted in the channel registry): ``client_id``,
``client_secret``, ``username``, ``password``. Optional non-secret
overrides in the same payload: ``base_url``, ``requires_settle``,
``paths`` (per-endpoint overrides), ``scope``.
"""

from __future__ import annotations

import httpx

from hesabyar.domain.gateways.errors import GatewayNotConfiguredError
from hesabyar.domain.gateways.models import GatewayCapabilities, SettlementSource
from hesabyar.infrastructure.gateways.common import DEFAULT_GATEWAY_TIMEOUT_SECONDS
from hesabyar.infrastructure.gateways.online_payment import OnlinePaymentClient

__all__ = ["TorobPayClient"]

#: Default base URL (payvand — UNVERIFIED; the merchant docs win).
DEFAULT_BASE_URL = "https://api.torobpay.com"


class TorobPayClient(OnlinePaymentClient):
    """TorobPay OAuth REST: verify (+ optional settle) + status polling."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        username: str,
        password: str,
        base_url: str | None = None,
        requires_settle: bool = False,
        paths: dict[str, str] | None = None,
        scope: str | None = None,
        timeout: float = DEFAULT_GATEWAY_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        missing = [
            name
            for name, value in (
                ("client_id", client_id),
                ("client_secret", client_secret),
                ("username", username),
                ("password", password),
            )
            if not value.strip()
        ]
        if missing:
            raise GatewayNotConfiguredError(
                f"TorobPay needs {', '.join(missing)} (merchant docs, after approval).",
                details={"gateway": "torobpay", "missing_fields": missing},
            )
        super().__init__(
            gateway_name="torobpay",
            base_url=base_url or DEFAULT_BASE_URL,
            client_id=client_id,
            client_secret=client_secret,
            username=username,
            password=password,
            requires_settle=requires_settle,
            paths=paths,
            scope=scope,
            timeout=timeout,
            transport=transport,
        )

    @property
    def channel_key(self) -> str:
        """The registry channel key this client serves."""
        return "torobpay"

    @property
    def capabilities(self) -> GatewayCapabilities:
        """What TorobPay can and cannot do (see the module docstring)."""
        return GatewayCapabilities(
            channel_key="torobpay",
            has_payment_api=True,
            supports_verify=True,
            supports_inquiry=True,
            supports_refund=True,
            supports_list=False,
            has_settlement_api=False,
            has_server_webhook=False,
            settlement_source=SettlementSource.PORTAL_FILE,
            settlement_model_note=(
                "Settlement speed is priced per contract (the public 7/12/17% "
                "figures are single-source and non-binding) — the model and "
                "fee % come from the channel's contract only."
            ),
            open_questions=(
                "Base URL and endpoint paths are UNVERIFIED (payvand only) — "
                "confirm against TorobPay's merchant docs after approval.",
                "Whether a second settle call is needed is per-contract — set requires_settle from the contract.",
                "Panel settlement output (Excel/CSV) is UNVERIFIED.",
                "Server webhook existence is UNVERIFIED — reconciliation assumes none.",
            ),
        )
