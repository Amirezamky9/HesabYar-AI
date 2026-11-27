"""Gateway clients: one httpx client per payment gateway (US-524).

Status reconciliation only — settlement stays file-based for every
gateway. Build clients through
:class:`hesabyar.infrastructure.gateways.factory.GatewayClientFactory`,
never by importing a client class directly.
"""

from __future__ import annotations

from hesabyar.infrastructure.gateways.digipay import DigiPayClient
from hesabyar.infrastructure.gateways.factory import GatewayClientFactory
from hesabyar.infrastructure.gateways.manual import ManualGatewayClient
from hesabyar.infrastructure.gateways.snapppay import SnappPayClient
from hesabyar.infrastructure.gateways.torobpay import TorobPayClient
from hesabyar.infrastructure.gateways.zarinpal import ZarinPalClient

__all__ = [
    "DigiPayClient",
    "GatewayClientFactory",
    "ManualGatewayClient",
    "SnappPayClient",
    "TorobPayClient",
    "ZarinPalClient",
]
