"""SMS client factory (US-544).

The ONLY place that maps a provider kind to a client
implementation (the same seed-data pattern as the gateway factory:
kinds are data, and the factory is the seam). The reminder service
builds clients through this factory, never by importing a client
class.

- ``kavenegar`` → :class:`KavenegarSmsClient` (needs ``api_key``)
- ``sms_ir`` → :class:`SmsIrSmsClient` (needs ``api_key`` + a
  numeric sender line from the provider row)
- ``simulator`` → :class:`SimulatorSmsClient` (no credentials)
- ``custom`` / any other kind → refused: no client exists for it

Fail-closed: a live kind without its required credentials raises
``SmsProviderNotConfiguredError`` here — no live call is ever
attempted down a path that cannot succeed. ``transport`` exists
for tests (``httpx.MockTransport``) — no live SMS call is ever
made from a test.
"""

from __future__ import annotations

from collections.abc import Mapping

import httpx

from hesabyar.domain.sms.credentials import require_credential_fields
from hesabyar.domain.sms.errors import (
    SmsProviderClientUnavailableError,
    SmsProviderNotConfiguredError,
)
from hesabyar.domain.sms.models import (
    PROVIDER_KIND_CUSTOM,
    PROVIDER_KIND_KAVENEGAR,
    PROVIDER_KIND_SIMULATOR,
    PROVIDER_KIND_SMS_IR,
)
from hesabyar.domain.sms.ports import SmsProviderClient
from hesabyar.infrastructure.sms.kavenegar import (
    DEFAULT_KAVENEGAR_BASE_URL,
    KavenegarSmsClient,
)
from hesabyar.infrastructure.sms.simulator import SimulatorSmsClient
from hesabyar.infrastructure.sms.sms_ir import DEFAULT_SMS_IR_BASE_URL, SmsIrSmsClient


def _base_url_override(credentials: Mapping[str, object], default: str) -> str:
    override = credentials.get("base_url")
    if isinstance(override, str) and override.strip():
        return override.strip()
    return default


class DefaultSmsClientFactory:
    """Builds the client for a provider kind from its credentials."""

    def __init__(self, *, transport: httpx.BaseTransport | None = None) -> None:
        self._transport = transport

    def client_for(
        self,
        *,
        provider_kind: str,
        provider_key: str,
        credentials: dict[str, str],
        line_number: str | None,
    ) -> SmsProviderClient:
        """Return the client for ``provider_kind`` (fail-closed when unconfigured)."""
        if provider_kind == PROVIDER_KIND_SIMULATOR:
            return SimulatorSmsClient()
        if provider_kind == PROVIDER_KIND_KAVENEGAR:
            secrets = require_credential_fields(
                credentials, provider_kind=provider_kind, provider_key=provider_key
            )
            return KavenegarSmsClient(
                api_key=secrets["api_key"],
                base_url=_base_url_override(credentials, DEFAULT_KAVENEGAR_BASE_URL),
                transport=self._transport,
            )
        if provider_kind == PROVIDER_KIND_SMS_IR:
            secrets = require_credential_fields(
                credentials, provider_kind=provider_kind, provider_key=provider_key
            )
            if line_number is None or not line_number.isdigit():
                raise SmsProviderNotConfiguredError(
                    provider_key,
                    reason="no numeric sender line is configured (sms.ir requires an int64 line number).",
                )
            return SmsIrSmsClient(
                api_key=secrets["api_key"],
                line_number=int(line_number),
                base_url=_base_url_override(credentials, DEFAULT_SMS_IR_BASE_URL),
                transport=self._transport,
            )
        if provider_kind == PROVIDER_KIND_CUSTOM:
            raise SmsProviderClientUnavailableError(provider_kind)
        raise SmsProviderClientUnavailableError(provider_kind)
