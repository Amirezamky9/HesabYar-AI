"""Gateway client factory (US-524).

The ONLY place that maps a channel key to a client implementation
(the same seed-data pattern as the channel registry: keys are data,
and the factory is the seam). The reconciliation service builds
clients through this factory, never by importing a client class.

- ``zarinpal`` → :class:`ZarinPalClient`
- ``snapppay`` → :class:`SnappPayClient`
- ``torobpay`` → :class:`TorobPayClient`
- ``digipay`` → :class:`DigiPayClient`
- ``sadad`` / ``salampay`` / any other key → manual client (custom
  channels have no API by design, phased design §1.7)
- ``basalam_marketplace`` / ``torob_marketplace`` → refused: a
  marketplace channel is not a payment channel. ``torob_marketplace``
  is RESERVED and stays disabled until the Torob order API is
  verified; it is never mixed with the ``torobpay`` payment channel.

``transport`` exists for tests (``httpx.MockTransport``) — no live
gateway call is ever made from a test.
"""

from __future__ import annotations

from collections.abc import Mapping

import httpx

from hesabyar.domain.gateways.credentials import (
    REQUIRED_CREDENTIAL_FIELDS,
    require_credential_fields,
)
from hesabyar.domain.gateways.errors import (
    GatewayNotConfiguredError,
    GatewayUnsupportedOperationError,
)
from hesabyar.domain.gateways.models import GatewayCapabilities
from hesabyar.domain.gateways.ports import GatewayClientPort
from hesabyar.domain.integrations.models import PaymentChannel
from hesabyar.infrastructure.gateways.digipay import DigiPayClient
from hesabyar.infrastructure.gateways.manual import (
    ManualGatewayClient,
    manual_capabilities_for,
)
from hesabyar.infrastructure.gateways.snapppay import SnappPayClient
from hesabyar.infrastructure.gateways.torobpay import TorobPayClient
from hesabyar.infrastructure.gateways.zarinpal import ZarinPalClient

__all__ = ["GatewayClientFactory", "MARKETPLACE_CHANNEL_KEYS"]

#: Channel keys that are marketplaces, never payment channels.
MARKETPLACE_CHANNEL_KEYS: frozenset[str] = frozenset({"basalam_marketplace", "torob_marketplace"})

#: Channel keys with an HTTP payment API.
API_CHANNEL_KEYS: frozenset[str] = frozenset({"zarinpal", "snapppay", "torobpay", "digipay"})


def _string_override(credentials: Mapping[str, object], name: str) -> str | None:
    """Read an optional non-secret string override (None when absent/blank)."""
    value = credentials.get(name)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _bool_override(credentials: Mapping[str, object], name: str, default: bool) -> bool:
    """Read an optional non-secret boolean override."""
    value = credentials.get(name)
    if isinstance(value, bool):
        return value
    return default


class GatewayClientFactory:
    """Build the right gateway client for a registry channel."""

    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        self._transport = transport

    def build(
        self,
        channel: PaymentChannel,
        credentials: Mapping[str, object],
    ) -> GatewayClientPort:
        """Build a client for ``channel`` from its credentials.

        Marketplace channels are refused outright. Manual channels
        need no credentials. API channels need their required secret
        fields (missing ones raise ``GatewayNotConfiguredError``
        naming the fields).
        """
        key = channel.channel_key
        if key in MARKETPLACE_CHANNEL_KEYS:
            raise GatewayUnsupportedOperationError(
                f"Channel {key!r} is a marketplace channel, not a payment channel — no gateway client exists for it.",
                details={"channel_key": key},
            )
        if key not in API_CHANNEL_KEYS:
            return ManualGatewayClient(key)
        require_credential_fields(
            credentials,
            REQUIRED_CREDENTIAL_FIELDS[key],
            channel_key=key,
        )
        return self._build_api_client(key, channel, credentials)

    def requires_credentials(self, channel: PaymentChannel) -> bool:
        """True when the channel's client needs stored credentials."""
        return channel.channel_key in API_CHANNEL_KEYS

    def capabilities_for(self, channel: PaymentChannel) -> GatewayCapabilities:
        """Return a channel's capabilities without credentials.

        Static per channel key — no HTTP, no secrets. Marketplace
        channels get manual-style capabilities (no payment API);
        unknown keys get the custom-channel capabilities.
        """
        key = channel.channel_key
        if key in MARKETPLACE_CHANNEL_KEYS or key not in API_CHANNEL_KEYS:
            return manual_capabilities_for(key)
        # Capabilities are static per gateway; build a throwaway
        # client with placeholder credentials to read them. The
        # constructors validate only non-emptiness, and no HTTP call
        # is made at construction time.
        placeholder: dict[str, object] = {name: "placeholder" for name in REQUIRED_CREDENTIAL_FIELDS[key]}
        client = self._build_api_client(key, channel, placeholder)
        try:
            return client.capabilities
        finally:
            client.close()

    def _build_api_client(
        self,
        key: str,
        channel: PaymentChannel,
        credentials: Mapping[str, object],
    ) -> GatewayClientPort:
        """Build one of the four HTTP clients (fields pre-validated)."""

        def secret(name: str) -> str:
            value = credentials.get(name)
            assert isinstance(value, str)  # validated by require_credential_fields
            return value

        transport = self._transport
        if key == "zarinpal":
            return ZarinPalClient(
                merchant_id=secret("merchant_id"),
                access_token=_string_override(credentials, "access_token"),
                terminal_id=_string_override(credentials, "terminal_id"),
                environment=channel.environment,
                rest_base_url=_string_override(credentials, "rest_base_url"),
                graphql_url=_string_override(credentials, "graphql_url"),
                transport=transport,
            )
        if key == "snapppay":
            return SnappPayClient(
                client_id=secret("client_id"),
                client_secret=secret("client_secret"),
                username=secret("username"),
                password=secret("password"),
                base_url=_string_override(credentials, "base_url"),
                scope=_string_override(credentials, "scope"),
                transport=transport,
            )
        if key == "torobpay":
            paths_value = credentials.get("paths")
            paths = dict(paths_value) if isinstance(paths_value, dict) else None
            paths_str: dict[str, str] | None = None
            if paths is not None:
                paths_str = {str(k): str(v) for k, v in paths.items()}
            return TorobPayClient(
                client_id=secret("client_id"),
                client_secret=secret("client_secret"),
                username=secret("username"),
                password=secret("password"),
                base_url=_string_override(credentials, "base_url"),
                requires_settle=_bool_override(credentials, "requires_settle", False),
                paths=paths_str,
                scope=_string_override(credentials, "scope"),
                transport=transport,
            )
        if key == "digipay":
            return DigiPayClient(
                client_id=secret("client_id"),
                client_secret=secret("client_secret"),
                username=secret("username"),
                password=secret("password"),
                base_url=_string_override(credentials, "base_url"),
                transport=transport,
            )
        raise GatewayNotConfiguredError(
            f"Channel {key!r} has no gateway client.",
            details={"channel_key": key},
        )
