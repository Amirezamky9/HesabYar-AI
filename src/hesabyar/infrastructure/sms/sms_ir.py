"""sms.ir v1 REST client (US-544).

Integration shape VERIFIED from the official published OpenAPI
document (https://api.sms.ir/swagger/v1/swagger.json, fetched
2026-11-20):

- Server: ``https://api.sms.ir``; auth: API key in the
  ``x-api-key`` header (security scheme ``ApiKey``).
- Send: ``POST /v1/send/bulk``, JSON ``BulkSendModel``:
  ``{lineNumber: int64, messageText: string, mobiles: [string],
  sendDateTime?: int32}``. Response:
  ``{status: int32, message: string, data: {packId: uuid,
  messageIds: [int32], cost: double}}``.
- Status: ``GET /v1/send/{messageId}`` → ``data:
  ApiSentSmsStatusView {messageId, mobile, messageText,
  sendDateTime, lineNumber, cost, deliveryState: int32?,
  deliveryDateTime: int32?}``.

Two deliberate gaps (NOT guessed):

- The OpenAPI document does not publish the semantics of the
  envelope ``status`` code, so success is judged STRUCTURALLY:
  HTTP 200 + a ``data`` object carrying a non-empty ``messageIds``
  list. Anything else is a rejection carrying the provider's
  ``message``.
- The ``deliveryState`` values are not published anywhere
  authoritative, so ``get_status`` returns UNKNOWN with the raw
  value preserved — never a guessed mapping.
- The ``cost`` unit is not published either: it is stored verbatim
  (``cost_raw``) with unit ``"unverified"`` and NEVER converted
  to Toman.

The legacy token API (``ws.sms.ir``) is retired and is NOT used;
the legacy URL-send endpoint (credentials as query parameters) is
deliberately not implemented (credentials in URLs leak into logs
and proxies).

Live end-to-end verification (a real send against the API) is
still pending — it needs the user's sms.ir credentials, which do
not exist yet. Until then the simulator covers every reminder
flow in tests, and this client is exercised only against
``httpx.MockTransport`` with canned provider payloads.
"""

from __future__ import annotations

from typing import Any

import httpx

from hesabyar.domain.sms.errors import (
    SmsProviderInvalidResponseError,
    SmsProviderNotConfiguredError,
    SmsProviderRejectedError,
)
from hesabyar.domain.sms.models import PROVIDER_KIND_SMS_IR, SmsDeliveryStatus
from hesabyar.domain.sms.ports import SmsDeliveryUpdate, SmsSendResult
from hesabyar.infrastructure.sms.common import BaseSmsHttpClient

#: Default sms.ir API root (overridable via the credential payload's ``base_url``).
DEFAULT_SMS_IR_BASE_URL = "https://api.sms.ir"


class SmsIrSmsClient(BaseSmsHttpClient):
    """sms.ir v1 REST client (bulk send with one recipient)."""

    def __init__(
        self,
        *,
        api_key: str,
        line_number: int,
        base_url: str = DEFAULT_SMS_IR_BASE_URL,
        transport: httpx.BaseTransport | None = None,
        timeout: float | None = None,
    ) -> None:
        if not api_key.strip():
            raise SmsProviderNotConfiguredError(
                PROVIDER_KIND_SMS_IR, reason="no api_key was supplied."
            )
        kwargs: dict[str, Any] = {
            "provider_key": PROVIDER_KIND_SMS_IR,
            "base_url": base_url,
            "transport": transport,
            "headers": {"x-api-key": api_key},
        }
        if timeout is not None:
            kwargs["timeout"] = timeout
        super().__init__(**kwargs)
        self._line_number = line_number

    @property
    def provider_kind(self) -> str:
        return PROVIDER_KIND_SMS_IR

    def send(
        self, *, to: str, message: str, sender_line: str | None
    ) -> SmsSendResult:
        """Send one message via ``POST /v1/send/bulk``.

        ``sender_line`` is ignored: the line is fixed at client
        construction (the registry's ``line_number``), because the
        bulk endpoint takes a single int64 line number per call.
        """
        _ = sender_line
        response = self._request(
            "POST",
            "/v1/send/bulk",
            operation="send",
            json_body={
                "lineNumber": self._line_number,
                "messageText": message,
                "mobiles": [to],
            },
        )
        payload = self._json_or_raise(response, operation="send")
        data = payload.get("data")
        provider_message = payload.get("message")
        detail = (
            f" (provider message: {provider_message})"
            if isinstance(provider_message, str) and provider_message
            else ""
        )
        if not isinstance(data, dict):
            raise SmsProviderRejectedError(
                PROVIDER_KIND_SMS_IR,
                reason=f"the send response has no data object{detail}.",
            )
        message_ids = data.get("messageIds")
        if (
            not isinstance(message_ids, list)
            or not message_ids
            or not all(isinstance(item, int) for item in message_ids)
        ):
            raise SmsProviderRejectedError(
                PROVIDER_KIND_SMS_IR,
                reason=f"the send response carries no message ids{detail}.",
            )
        pack_id = data.get("packId")
        cost = data.get("cost")
        return SmsSendResult(
            provider_message_id=str(message_ids[0]),
            status=SmsDeliveryStatus.QUEUED,
            provider_status_raw=(
                f"packId={pack_id}" if isinstance(pack_id, str) and pack_id else None
            ),
            cost_toman=None,
            cost_raw=str(cost) if isinstance(cost, (int, float)) else None,
            cost_unit="unverified" if isinstance(cost, (int, float)) else None,
        )

    def get_status(self, provider_message_id: str) -> SmsDeliveryUpdate:
        """Look up a message via ``GET /v1/send/{messageId}``.

        ``deliveryState`` values are UNVERIFIED (not published in
        the OpenAPI document), so the result is always UNKNOWN with
        the raw value preserved — the caller records it, never a
        guessed mapping.
        """
        response = self._request(
            "GET",
            f"/v1/send/{provider_message_id}",
            operation="status",
        )
        payload = self._json_or_raise(response, operation="status")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise SmsProviderInvalidResponseError(
                PROVIDER_KIND_SMS_IR,
                reason="the status response has no data object.",
            )
        delivery_state = data.get("deliveryState")
        return SmsDeliveryUpdate(
            provider_message_id=str(data.get("messageId", provider_message_id)),
            status=SmsDeliveryStatus.UNKNOWN,
            provider_status_raw=(
                f"deliveryState={delivery_state}"
                if delivery_state is not None
                else None
            ),
        )
