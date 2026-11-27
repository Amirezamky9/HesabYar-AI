"""Shared HTTP plumbing for the SMS provider clients (US-544).

Every live SMS client is a thin httpx wrapper with the same
failure semantics (mirroring ``hesabyar.infrastructure.gateways.common``):

- transport error / timeout → ``SmsProviderUnreachableError`` (502,
  transient — retry later);
- HTTP 401/403 → ``SmsProviderAuthError`` (422 — re-issue credentials);
- other 4xx / a provider-level rejection → ``SmsProviderRejectedError``
  (422 — the provider said no, with its own code/message);
- HTTP 5xx → ``SmsProviderUnreachableError`` (the provider is down);
- unparseable or off-contract JSON → ``SmsProviderInvalidResponseError``
  (502 — never record a send from a response we cannot parse).

``trust_env=False`` on every client: SMS traffic must never be
intercepted by an environment proxy (the API key must not leak),
and malformed proxy env vars must not break sending (the
Tryton-client lesson, repeated by the store connectors and the
gateway clients). Request URLs are NEVER logged: Kavenegar embeds
its API key in the URL path.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

import httpx

from hesabyar.domain.sms.errors import (
    SmsProviderAuthError,
    SmsProviderInvalidResponseError,
    SmsProviderRejectedError,
    SmsProviderUnreachableError,
)

__all__ = ["DEFAULT_SMS_TIMEOUT_SECONDS", "BaseSmsHttpClient"]

#: Default HTTP timeout for an SMS provider call (mirrors the gateway clients).
DEFAULT_SMS_TIMEOUT_SECONDS = 30.0


class BaseSmsHttpClient:
    """httpx plumbing shared by the live SMS clients.

    Subclasses own their endpoints and payloads; this class owns
    transport, timeouts, and the error mapping. ``transport`` exists
    for tests (``httpx.MockTransport``) — production passes None.
    No live SMS call is ever made from a test.
    """

    def __init__(
        self,
        *,
        provider_key: str,
        base_url: str,
        timeout: float = DEFAULT_SMS_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise SmsProviderRejectedError(
                provider_key,
                reason=f"base URL {base_url!r} is not an http(s) URL.",
            )
        self._provider_key = provider_key
        self._base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self._base_url,
            timeout=timeout,
            transport=transport,
            trust_env=False,
            headers=headers or {},
        )

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    # -- low-level calls -------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        json_body: dict[str, Any] | None = None,
        form_body: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """One HTTP call with the shared transport-error mapping."""
        try:
            if form_body is not None:
                return self._client.request(
                    method, path, data=form_body, params=params, headers=headers
                )
            return self._client.request(
                method, path, json=json_body, params=params, headers=headers
            )
        except httpx.TimeoutException as exc:
            raise SmsProviderUnreachableError(
                self._provider_key,
                reason=f"{operation} timed out ({self._base_url}).",
            ) from exc
        except httpx.TransportError as exc:
            raise SmsProviderUnreachableError(
                self._provider_key,
                reason=f"could not reach the provider for {operation} ({self._base_url}).",
            ) from exc

    def _json_or_raise(self, response: httpx.Response, *, operation: str) -> dict[str, Any]:
        """Map an HTTP status to the shared errors; parse the JSON body."""
        if response.status_code in (401, 403):
            raise SmsProviderAuthError(
                self._provider_key,
                reason=(
                    f"the stored credentials were rejected during {operation} "
                    f"(HTTP {response.status_code}). Re-issue them in the provider's "
                    "portal and rotate them in the SMS registry."
                ),
            )
        if response.status_code >= 500:
            raise SmsProviderUnreachableError(
                self._provider_key,
                reason=f"{operation} failed with HTTP {response.status_code}.",
            )
        if response.status_code >= 400:
            raise SmsProviderRejectedError(
                self._provider_key,
                reason=f"{operation} was rejected with HTTP {response.status_code}.",
            )
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise SmsProviderInvalidResponseError(
                self._provider_key,
                reason=f"unparseable JSON for {operation}.",
            ) from exc
        if not isinstance(payload, dict):
            raise SmsProviderInvalidResponseError(
                self._provider_key,
                reason=f"a non-object JSON body for {operation}.",
            )
        return payload
