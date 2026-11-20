"""Shared HTTP plumbing for the gateway clients (US-524).

Every gateway client is a thin httpx wrapper with the same failure
semantics, so the reconciliation service can treat them alike:

- transport error / timeout → ``GatewayUnreachableError`` (502,
  transient — retry later);
- HTTP 401/403 → ``GatewayAuthError`` (422 — re-issue credentials);
- other 4xx / a provider-level rejection → ``GatewayRejectedError``
  (422 — the gateway said no, with its own code/message);
- HTTP 5xx → ``GatewayUnreachableError`` (the gateway is down);
- unparseable or off-contract JSON → ``GatewayInvalidResponseError``
  (502 — never record a payment from a response we cannot parse).

``trust_env=False`` on every client: gateway traffic must never be
intercepted by an environment proxy (the Bearer <redacted> must not
leak), and malformed proxy env vars must not break reconciliation
(the Tryton-client lesson, repeated by the store connectors).
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

import httpx

from hesabyar.domain.gateways.errors import (
    GatewayAuthError,
    GatewayInvalidResponseError,
    GatewayRejectedError,
    GatewayUnreachableError,
)

__all__ = ["DEFAULT_GATEWAY_TIMEOUT_SECONDS", "BaseGatewayHttpClient"]

#: Default HTTP timeout for a gateway call (mirrors the store poller).
DEFAULT_GATEWAY_TIMEOUT_SECONDS = 30.0


class BaseGatewayHttpClient:
    """httpx plumbing shared by the gateway clients.

    Subclasses own their endpoints and payloads; this class owns
    transport, timeouts, and the error mapping. ``transport`` exists
    for tests (``httpx.MockTransport``) — production passes None.
    No live gateway call is ever made from a test.
    """

    def __init__(
        self,
        *,
        gateway_name: str,
        base_url: str,
        timeout: float = DEFAULT_GATEWAY_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise GatewayRejectedError(
                f"Gateway {gateway_name!r} base URL {base_url!r} is not an http(s) URL.",
                details={"gateway": gateway_name, "base_url": base_url},
            )
        self._gateway_name = gateway_name
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
                return self._client.request(method, path, data=form_body, params=params, headers=headers)
            return self._client.request(method, path, json=json_body, params=params, headers=headers)
        except httpx.TimeoutException as exc:
            raise GatewayUnreachableError(
                f"{self._gateway_name} {operation} timed out ({self._base_url}).",
                details={"gateway": self._gateway_name, "operation": operation},
            ) from exc
        except httpx.TransportError as exc:
            raise GatewayUnreachableError(
                f"Could not reach {self._gateway_name} for {operation} ({self._base_url}).",
                details={"gateway": self._gateway_name, "operation": operation},
            ) from exc

    def _json_or_raise(self, response: httpx.Response, *, operation: str) -> dict[str, Any]:
        """Map an HTTP status to the shared errors; parse the JSON body."""
        if response.status_code in (401, 403):
            raise GatewayAuthError(
                f"{self._gateway_name} rejected the stored credentials during "
                f"{operation} (HTTP {response.status_code}). Re-issue them in "
                "the gateway's portal and rotate them in the channel registry.",
                details={
                    "gateway": self._gateway_name,
                    "operation": operation,
                    "status_code": response.status_code,
                },
            )
        if response.status_code >= 500:
            raise GatewayUnreachableError(
                f"{self._gateway_name} failed {operation} with HTTP {response.status_code}.",
                details={
                    "gateway": self._gateway_name,
                    "operation": operation,
                    "status_code": response.status_code,
                },
            )
        if response.status_code >= 400:
            raise GatewayRejectedError(
                f"{self._gateway_name} rejected {operation} with HTTP {response.status_code}.",
                details={
                    "gateway": self._gateway_name,
                    "operation": operation,
                    "status_code": response.status_code,
                },
            )
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise GatewayInvalidResponseError(
                f"{self._gateway_name} returned unparseable JSON for {operation}.",
                details={"gateway": self._gateway_name, "operation": operation},
            ) from exc
        if not isinstance(payload, dict):
            raise GatewayInvalidResponseError(
                f"{self._gateway_name} returned a non-object JSON body for {operation}.",
                details={"gateway": self._gateway_name, "operation": operation},
            )
        return payload
