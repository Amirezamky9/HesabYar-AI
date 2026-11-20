"""SnappPay gateway client (US-524).

REST v2.1 (official academy docs, ``academy.snapppay.ir/self-cms/``,
revision 2.1, last changed 2026-01-19): OAuth token, eligible,
payment token, verify, settle, revert, status, cancel, update.
All amounts are IRR. The base URL comes from the merchant portal
(``Production Base URL``) — it is not public; the default below is
payvand's (a secondary source) and the operator overrides it from
the portal when it differs.

Usage here is STATUS RECONCILIATION ONLY: verify (+ the mandatory
settle), status polling for lost callbacks, revert/cancel for
refunds. Settlement comes from the portal's finance section
(``Payments`` Excel + monthly statement) and the monthly emailed
detail Excel — there is no settlement API and no server webhook
(the only callback is the payer's POST return to ``returnURL``).

Four contractual settlement models exist (monthly 100%, lump after
2 months, 2 equal tranches, 4 equal tranches) — the model and the
fee % come from the channel's CONTRACT, never from this client.

Credentials (encrypted in the channel registry): ``client_id``,
``client_secret`` (one-time — regenerate in the portal and rotate
here immediately), ``username``, ``password``. Optional non-secret
overrides in the same payload: ``base_url``, ``scope``. The server's
IP must be whitelisted in SnappPay (max 3 IPs) or calls are refused.
"""

from __future__ import annotations

import httpx

from hesabyar.domain.gateways.errors import GatewayNotConfiguredError
from hesabyar.domain.gateways.models import GatewayCapabilities, SettlementSource
from hesabyar.infrastructure.gateways.common import DEFAULT_GATEWAY_TIMEOUT_SECONDS
from hesabyar.infrastructure.gateways.online_payment import OnlinePaymentClient

__all__ = ["SnappPayClient"]

#: Default base URL (payvand — secondary; the portal's value wins).
DEFAULT_BASE_URL = "https://api.snapppay.ir"


class SnappPayClient(OnlinePaymentClient):
    """SnappPay REST v2.1: verify (+ mandatory settle) + status polling."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        username: str,
        password: str,
        base_url: str | None = None,
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
                f"SnappPay needs {', '.join(missing)} (portal → technical access).",
                details={"gateway": "snapppay", "missing_fields": missing},
            )
        super().__init__(
            gateway_name="snapppay",
            base_url=base_url or DEFAULT_BASE_URL,
            client_id=client_id,
            client_secret=client_secret,
            username=username,
            password=password,
            requires_settle=True,
            scope=scope,
            timeout=timeout,
            transport=transport,
        )

    @property
    def channel_key(self) -> str:
        """The registry channel key this client serves."""
        return "snapppay"

    @property
    def capabilities(self) -> GatewayCapabilities:
        """What SnappPay can and cannot do (see the module docstring)."""
        return GatewayCapabilities(
            channel_key="snapppay",
            has_payment_api=True,
            supports_verify=True,
            supports_inquiry=True,
            supports_refund=True,
            supports_list=False,
            has_settlement_api=False,
            has_server_webhook=False,
            settlement_source=SettlementSource.PORTAL_FILE_AND_EMAIL,
            settlement_model_note=(
                "Four contractual models (monthly / lump after 2 months / "
                "2 tranches / 4 tranches) — the model and fee % come from "
                "the channel's contract; the sale month is the anchor."
            ),
            open_questions=(
                "The settlement file's amount unit (IRR vs TMN) is UNVERIFIED — confirm against the first real file.",
                "Some portal payouts carry no tracking number (group payouts) — those match on amount + date only.",
            ),
        )
