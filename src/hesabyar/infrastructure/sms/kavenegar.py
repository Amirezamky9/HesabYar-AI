"""Kavenegar SMS client (US-544).

Integration shape VERIFIED from the official REST documentation
(https://kavenegar.com/rest.html, fetched 2026-11-20) and the
official SDK (kavenegar/kavenegar-node):

- Send: ``POST https://api.kavenegar.com/v1/{api_key}/sms/send.json``,
  form-encoded params ``receptor`` (required, comma-separated),
  ``message`` (required), ``sender`` (optional — the account
  default when omitted). The API key travels in the URL path
  (never logged).
- Response: ``{"return": {"status": 200, "message": "…"}, "entries":
  [{"messageid", "status", "statustext", "sender", "receptor",
  "date", "cost"}]}``. ``cost`` is Rial (documented).
- Provider error codes (``return.status``): 400 incomplete params,
  401 inactive account, 402 operation failed, 403 invalid API key,
  411 invalid receptor, 412 invalid sender, 413 empty/too-long
  message, 414 too many recipients (max 200/call), 417 bad send
  date, 418 insufficient credit, 607 bad tag.
- Delivery statuses (docs Table 2): 1 queued, 2 scheduled, 4/5
  sent to telecom, 6 failed, 10 delivered, 11 undelivered, 13
  cancelled, 14 blocked, 100 invalid message id.
- Status lookup: ``POST /v1/{api_key}/sms/status.json`` with
  form param ``messageid`` (per the official SDK's request shape;
  the docs confirm the Status method takes the message id).

Live end-to-end verification (a real send against the API) is
still pending — it needs the user's Kavenegar credentials, which
do not exist yet. Until then the simulator covers every reminder
flow in tests, and this client is exercised only against
``httpx.MockTransport`` with canned provider payloads.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx

from hesabyar.domain.sms.errors import (
    SmsProviderAuthError,
    SmsProviderInvalidResponseError,
    SmsProviderNotConfiguredError,
    SmsProviderRejectedError,
)
from hesabyar.domain.sms.models import PROVIDER_KIND_KAVENEGAR, SmsDeliveryStatus
from hesabyar.domain.sms.ports import SmsDeliveryUpdate, SmsSendResult
from hesabyar.infrastructure.sms.common import BaseSmsHttpClient

#: Default Kavenegar API root (overridable via the credential payload's ``base_url``).
DEFAULT_KAVENEGAR_BASE_URL = "https://api.kavenegar.com"

#: Docs Table 2 → normalized delivery status (100 + unknown codes → UNKNOWN).
_STATUS_MAP: dict[int, SmsDeliveryStatus] = {
    1: SmsDeliveryStatus.QUEUED,
    2: SmsDeliveryStatus.QUEUED,
    4: SmsDeliveryStatus.SENT,
    5: SmsDeliveryStatus.SENT,
    6: SmsDeliveryStatus.FAILED,
    10: SmsDeliveryStatus.DELIVERED,
    11: SmsDeliveryStatus.FAILED,
    13: SmsDeliveryStatus.FAILED,
    14: SmsDeliveryStatus.FAILED,
}

#: Provider error codes that mean "the credentials are wrong" (vs a rejected send).
_AUTH_CODES = frozenset({401, 403})


class KavenegarSmsClient(BaseSmsHttpClient):
    """Kavenegar REST client (one send = one recipient)."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEFAULT_KAVENEGAR_BASE_URL,
        transport: httpx.BaseTransport | None = None,
        timeout: float | None = None,
    ) -> None:
        if not api_key.strip():
            raise SmsProviderNotConfiguredError(
                PROVIDER_KIND_KAVENEGAR, reason="no api_key was supplied."
            )
        kwargs: dict[str, Any] = {
            "provider_key": PROVIDER_KIND_KAVENEGAR,
            "base_url": base_url,
            "transport": transport,
        }
        if timeout is not None:
            kwargs["timeout"] = timeout
        super().__init__(**kwargs)
        self._api_key = api_key

    @property
    def provider_kind(self) -> str:
        return PROVIDER_KIND_KAVENEGAR

    def send(
        self, *, to: str, message: str, sender_line: str | None
    ) -> SmsSendResult:
        """Send one message via ``/v1/{api_key}/sms/send.json``."""
        form: dict[str, str] = {"receptor": to, "message": message}
        if sender_line:
            form["sender"] = sender_line
        response = self._request(
            "POST",
            f"/v1/{self._api_key}/sms/send.json",
            operation="send",
            form_body=form,
        )
        payload = self._json_or_raise(response, operation="send")
        entry = self._first_entry(payload, operation="send")
        status_code = entry.get("status")
        status_text = entry.get("statustext")
        delivery = _STATUS_MAP.get(status_code, SmsDeliveryStatus.UNKNOWN) if isinstance(
            status_code, int
        ) else SmsDeliveryStatus.UNKNOWN
        cost = entry.get("cost")
        cost_toman: Decimal | None = None
        cost_raw: str | None = None
        if isinstance(cost, int):
            cost_toman = Decimal(cost) / Decimal(10)
            cost_raw = str(cost)
        return SmsSendResult(
            provider_message_id=str(entry["messageid"]),
            status=delivery,
            provider_status_raw=(
                f"{status_code}:{status_text}"
                if status_code is not None
                else (str(status_text) if status_text is not None else None)
            ),
            cost_toman=cost_toman,
            cost_raw=cost_raw,
            cost_unit="IRR" if cost_raw is not None else None,
        )

    def get_status(self, provider_message_id: str) -> SmsDeliveryUpdate:
        """Look up a message via ``/v1/{api_key}/sms/status.json``."""
        response = self._request(
            "POST",
            f"/v1/{self._api_key}/sms/status.json",
            operation="status",
            form_body={"messageid": provider_message_id},
        )
        payload = self._json_or_raise(response, operation="status")
        entry = self._first_entry(payload, operation="status")
        status_code = entry.get("status")
        status_text = entry.get("statustext")
        delivery = _STATUS_MAP.get(status_code, SmsDeliveryStatus.UNKNOWN) if isinstance(
            status_code, int
        ) else SmsDeliveryStatus.UNKNOWN
        return SmsDeliveryUpdate(
            provider_message_id=str(entry.get("messageid", provider_message_id)),
            status=delivery,
            provider_status_raw=(
                f"{status_code}:{status_text}"
                if status_code is not None
                else (str(status_text) if status_text is not None else None)
            ),
        )

    def _first_entry(self, payload: dict[str, Any], *, operation: str) -> dict[str, Any]:
        """Validate the ``return`` envelope and return ``entries[0]``.

        Kavenegar reports most problems with HTTP 200 and a status
        code in the body, so the body status is checked first: 200
        means the call succeeded; 401/403 mean bad credentials;
        anything else is a provider rejection carrying its own
        message. A 200 without entries is an off-contract response.
        """
        returned = payload.get("return")
        if not isinstance(returned, dict):
            raise SmsProviderInvalidResponseError(
                PROVIDER_KIND_KAVENEGAR,
                reason=f"the {operation} response has no 'return' object.",
            )
        status = returned.get("status")
        message = returned.get("message")
        detail = f" (provider message: {message})" if isinstance(message, str) and message else ""
        if status != 200:
            if isinstance(status, int) and status in _AUTH_CODES:
                raise SmsProviderAuthError(
                    PROVIDER_KIND_KAVENEGAR,
                    reason=f"{operation} failed with provider status {status}{detail}.",
                )
            raise SmsProviderRejectedError(
                PROVIDER_KIND_KAVENEGAR,
                reason=f"{operation} failed with provider status {status}{detail}.",
            )
        entries = payload.get("entries")
        if not isinstance(entries, list) or not entries:
            raise SmsProviderInvalidResponseError(
                PROVIDER_KIND_KAVENEGAR,
                reason=f"the {operation} response has no entries.",
            )
        entry = entries[0]
        if not isinstance(entry, dict) or "messageid" not in entry:
            raise SmsProviderInvalidResponseError(
                PROVIDER_KIND_KAVENEGAR,
                reason=f"the {operation} response entry has no messageid.",
            )
        return entry
