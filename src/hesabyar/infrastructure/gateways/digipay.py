"""DigiPay gateway client (US-524).

UPG — the unified payment gateway (official docs,
``mydigipay.com/developers/docs/upg/``, 2,533 lines read in full
during research): base ``https://api.mydigipay.com/digipay/api``
(must be called by FQDN — the CDN forbids direct IP calls).

- OAuth2: ``POST oauth/token`` — ``grant_type=password`` +
  ``username``/``password`` (form-data) with ``Authorization:
  Basic base64(client_id:client_secret)``. ``access_token`` (JWT,
  ~3600s) + ``refresh_token``; refresh with
  ``grant_type=refresh_token``.
- ``POST tickets/business?type=11`` — create a purchase ticket
  (amount in IRR, cellNumber, callbackUrl); answers ``ticket`` +
  ``redirectUrl``.
- ``POST purchases/verify?type={type}`` — verify a payment. THE
  3-MINUTE WINDOW: if verify is not called within 3 minutes of a
  successful payment, DigiPay AUTO-REVERSES the transaction. Verify
  is idempotent (a repeat returns the earlier success). The window
  is enforced by DigiPay, not by this client — the reconciliation
  service must call verify immediately on the callback, never
  through a delayed queue.
- ``POST purchases/deliver?type={type}`` — announce delivery.
  MANDATORY for CREDIT and BNPL purchases (deliveryDate epoch ms,
  invoiceNumber, trackingCode, products).
- ``POST refunds?type={ticket-type}`` — refund (partial supported).
  A 200 means only \"request registered\", NOT finality.
- ``GET refunds/{inquiryId}?type={ticket-type}`` — refund inquiry:
  ``0=Success, 1=Failed, 2=Pending`` (Pending is unknown — inquire
  again later).

Purchase types: WALLET=11, CREDIT=5, BNPL=13, CREDIT-CARD=24.
Refund types: Debit=0, Wallet=11, Credit=5, BNPL=13.

No server webhook exists (the full UPG docs contain zero mentions
of ``webhook``) — only the callback to the ticket's ``callbackUrl``
(success is usually POST; cancel may be GET with an empty body).
Receiving a callback alone proves NOTHING — it must be verified.

Settlement: there is NO report/settlement endpoint in UPG, and the
merchant panel's output (CSV/Excel) is ⚠️ UNVERIFIED — it must be
asked of DigiPay support (recorded as an open question, never
invented). The settlement cycle (~14–30 days, lump) is secondary
and comes from the channel's contract.

Credentials (encrypted in the channel registry): ``client_id``,
``client_secret``, ``username``, ``password``. Optional non-secret
override in the same payload: ``base_url``.
"""

from __future__ import annotations

import base64
import datetime
from collections.abc import Sequence
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from hesabyar.domain.gateways.errors import (
    GatewayInvalidResponseError,
    GatewayNotConfiguredError,
    GatewayRejectedError,
    GatewayUnsupportedOperationError,
    GatewayVerificationMismatchError,
)
from hesabyar.domain.gateways.models import (
    GatewayCapabilities,
    GatewayInquiry,
    GatewayTransaction,
    GatewayTransactionStatus,
    RefundResult,
    SettlementSource,
    VerifiedPayment,
)
from hesabyar.infrastructure.gateways.common import (
    DEFAULT_GATEWAY_TIMEOUT_SECONDS,
    BaseGatewayHttpClient,
)

__all__ = ["DigiPayClient"]

#: UPG base URL (verified in the official UPG docs).
DEFAULT_BASE_URL = "https://api.mydigipay.com/digipay/api"

#: Purchase types (UPG docs).
PURCHASE_TYPE_WALLET = "11"
PURCHASE_TYPE_CREDIT = "5"
PURCHASE_TYPE_BNPL = "13"
PURCHASE_TYPE_CREDIT_CARD = "24"

#: Refund-inquiry statuses (UPG docs): 0=Success, 1=Failed, 2=Pending.
_REFUND_STATUS_MAP: dict[int, GatewayTransactionStatus] = {
    0: GatewayTransactionStatus.REFUNDED,
    1: GatewayTransactionStatus.FAILED,
    2: GatewayTransactionStatus.PENDING,
}

#: Refresh the cached token this far before its expiry.
_TOKEN_REFRESH_MARGIN_SECONDS = 60


class _OAuthResponse(BaseModel):
    """The UPG token answer (plain OAuth2, no envelope)."""

    model_config = ConfigDict(extra="ignore")

    access_token: str | None = None
    refresh_token: str | None = None
    token_type: str | None = None
    expires_in: int | None = None
    error: str | None = None
    error_description: str | None = None


class _TicketResponse(BaseModel):
    """The answer of ``tickets/business``."""

    model_config = ConfigDict(extra="ignore")

    ticket: str | None = None
    redirectUrl: str | None = None


class _VerifyResponse(BaseModel):
    """The answer of ``purchases/verify`` (documented fields only)."""

    model_config = ConfigDict(extra="ignore")

    amount: int | None = None
    trackingCode: str | None = None
    providerId: str | None = None
    paymentGateway: int | None = None
    creditProvider: str | None = None
    repaymentOption: int | str | None = None
    rrn: str | None = None
    psp: str | None = None
    finalizeDate: int | None = None


class _RefundResponse(BaseModel):
    """The answer of ``refunds`` (200 = request registered)."""

    model_config = ConfigDict(extra="ignore")

    inquiryId: str | None = None
    id: str | None = None


class DigiPayClient(BaseGatewayHttpClient):
    """DigiPay UPG: verify (3-minute window) + deliver + refunds."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        username: str,
        password: str,
        base_url: str | None = None,
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
                f"DigiPay needs {', '.join(missing)} (from DigiPay support/panel).",
                details={"gateway": "digipay", "missing_fields": missing},
            )
        super().__init__(
            gateway_name="digipay",
            base_url=base_url or DEFAULT_BASE_URL,
            timeout=timeout,
            transport=transport,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        self._client_id = client_id
        self._client_secret = client_secret
        self._username = username
        self._password = password
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._token_expires_at: datetime.datetime | None = None

    @property
    def channel_key(self) -> str:
        """The registry channel key this client serves."""
        return "digipay"

    @property
    def capabilities(self) -> GatewayCapabilities:
        """What DigiPay can and cannot do (see the module docstring)."""
        return GatewayCapabilities(
            channel_key="digipay",
            has_payment_api=True,
            supports_verify=True,
            supports_inquiry=True,
            supports_refund=True,
            supports_list=False,
            has_settlement_api=False,
            has_server_webhook=False,
            settlement_source=SettlementSource.PORTAL_FILE,
            settlement_model_note=(
                "Lump settlement in a ~14–30 day cycle (secondary sources) — "
                "the model and fee % come from the channel's contract only."
            ),
            open_questions=(
                "Merchant-panel settlement output (CSV/Excel) is UNVERIFIED — "
                "ask DigiPay support whether the panel exports reports.",
                "Merchant fee % is UNVERIFIED — read it from the contract.",
                "Sandbox existence is UNVERIFIED — ask DigiPay support.",
            ),
        )

    # -- OAuth ------------------------------------------------------------

    def _basic_auth(self) -> str:
        """Basic auth header value for client_id:client_secret."""
        return "Basic " + base64.b64encode(f"{self._client_id}:{self._client_secret}".encode()).decode("ascii")

    def _fetch_token(self, *, use_refresh: bool = False) -> None:
        """Run the OAuth grant (password, or refresh when available)."""
        form: dict[str, str]
        if use_refresh and self._refresh_token:
            form = {
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
            }
        else:
            form = {
                "grant_type": "password",
                "username": self._username,
                "password": self._password,
            }
        response = self._request(
            "POST",
            "oauth/token",
            operation="oauth_token",
            form_body=form,
            headers={"Authorization": self._basic_auth()},
        )
        parsed = self._json_or_raise(response, operation="oauth_token")
        try:
            answer = _OAuthResponse.model_validate(parsed)
        except ValidationError as exc:
            raise GatewayInvalidResponseError(
                "DigiPay returned a token answer that is not OAuth2.",
                details={"gateway": "digipay", "operation": "oauth_token"},
            ) from exc
        if not answer.access_token:
            raise GatewayRejectedError(
                "DigiPay refused the merchant credentials: "
                f"{answer.error_description or answer.error or 'no access_token'}.",
                details={
                    "gateway": "digipay",
                    "operation": "oauth_token",
                    "provider_code": answer.error,
                },
            )
        self._access_token = answer.access_token
        if answer.refresh_token:
            self._refresh_token = answer.refresh_token
        expires_in = answer.expires_in or 3600
        self._token_expires_at = datetime.datetime.now(datetime.UTC) + datetime.timedelta(
            seconds=max(expires_in - _TOKEN_REFRESH_MARGIN_SECONDS, 0)
        )

    def _auth_headers(self) -> dict[str, str]:
        """Bearer headers, refreshing the token when it is stale."""
        now = datetime.datetime.now(datetime.UTC)
        if self._access_token is None or self._token_expires_at is None or now >= self._token_expires_at:
            try:
                self._fetch_token(use_refresh=True)
            except GatewayRejectedError:
                # A dead refresh token falls back to the password grant.
                self._fetch_token(use_refresh=False)
        assert self._access_token is not None  # _fetch_token raises otherwise
        return {"Authorization": f"Bearer {self._access_token}"}

    def _authed_call(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """One authed call; a 401 refreshes the token once and retries."""
        response = self._request(
            method,
            path,
            operation=operation,
            json_body=json_body,
            params=params,
            headers=self._auth_headers(),
        )
        if response.status_code in (401, 403):
            self._access_token = None
            self._token_expires_at = None
            response = self._request(
                method,
                path,
                operation=operation,
                json_body=json_body,
                params=params,
                headers=self._auth_headers(),
            )
        return self._json_or_raise(response, operation=operation)

    # -- reconciliation operations ------------------------------------------

    def verify_payment(
        self,
        payment_ref: str,
        *,
        expected_amount_irr: int | None = None,
        purchase_type: str = PURCHASE_TYPE_BNPL,
    ) -> VerifiedPayment:
        """Verify a payment (``purchases/verify``) — within 3 minutes.

        ``payment_ref`` is the ticket's ``providerId`` (the store's
        order reference registered on the ticket). Call this
        IMMEDIATELY on the callback: past 3 minutes DigiPay
        auto-reverses the transaction. Idempotent — a repeat returns
        the earlier success.
        """
        payload = self._authed_call(
            "POST",
            "purchases/verify",
            operation="verify",
            json_body={"providerId": payment_ref},
            params={"type": purchase_type},
        )
        try:
            answer = _VerifyResponse.model_validate(payload)
        except ValidationError as exc:
            raise GatewayInvalidResponseError(
                "DigiPay returned a verify answer that does not match the UPG contract.",
                details={"gateway": "digipay", "operation": "verify", "error": str(exc)},
            ) from exc
        if answer.trackingCode is None and answer.providerId is None:
            raise GatewayInvalidResponseError(
                "DigiPay verified nothing: the answer carries neither a trackingCode nor a providerId.",
                details={"gateway": "digipay", "operation": "verify"},
            )
        if expected_amount_irr is not None and answer.amount is not None and answer.amount != expected_amount_irr:
            raise GatewayVerificationMismatchError(
                f"DigiPay verified {answer.providerId or payment_ref} for "
                f"{answer.amount} IRR, but {expected_amount_irr} IRR was expected.",
                details={
                    "gateway": "digipay",
                    "operation": "verify",
                    "expected_amount_irr": expected_amount_irr,
                    "actual_amount_irr": answer.amount,
                },
            )
        amount = answer.amount
        if amount is None:
            amount = expected_amount_irr
        if amount is None:
            raise GatewayInvalidResponseError(
                "DigiPay verified the payment but reported no amount, and no expected amount was given.",
                details={"gateway": "digipay", "operation": "verify"},
            )
        return VerifiedPayment(
            gateway_transaction_id=answer.providerId or payment_ref,
            gross_irr=amount,
            gateway_confirmed_at=datetime.datetime.now(datetime.UTC),
            status=GatewayTransactionStatus.PAID,
            store_order_id=answer.providerId,
            tracking_ref=answer.trackingCode,
            raw={
                "paymentGateway": answer.paymentGateway,
                "creditProvider": answer.creditProvider,
                "repaymentOption": answer.repaymentOption,
                "rrn": answer.rrn,
            },
        )

    def inquire_status(self, payment_ref: str) -> GatewayInquiry:
        """Re-verify a payment (UPG has no separate status endpoint).

        ``purchases/verify`` is idempotent, so re-verifying is the
        status inquiry: a repeat returns the earlier success. A
        payment DigiPay auto-reversed (past the 3-minute window) is
        rejected here and reported as REVERTED.
        """
        try:
            verified = self.verify_payment(payment_ref)
        except GatewayRejectedError as exc:
            return GatewayInquiry(
                gateway_transaction_id=payment_ref,
                status=GatewayTransactionStatus.REVERTED,
                amount_irr=None,
                message=f"DigiPay rejected the re-verify: {exc.message}",
            )
        return GatewayInquiry(
            gateway_transaction_id=verified.gateway_transaction_id,
            status=GatewayTransactionStatus.PAID,
            amount_irr=verified.gross_irr,
            message="verified",
        )

    def deliver(
        self,
        payment_ref: str,
        *,
        purchase_type: str = PURCHASE_TYPE_BNPL,
        delivery_date_ms: int | None = None,
        invoice_number: str | None = None,
        tracking_code: str | None = None,
        products: list[dict[str, Any]] | None = None,
    ) -> None:
        """Announce delivery (mandatory for CREDIT/BNPL purchases).

        ``payment_ref`` is the ticket's ``providerId``.
        ``delivery_date_ms`` is epoch milliseconds (now when omitted).
        """
        if delivery_date_ms is None:
            delivery_date_ms = int(datetime.datetime.now(datetime.UTC).timestamp() * 1000)
        self._authed_call(
            "POST",
            "purchases/deliver",
            operation="deliver",
            json_body={
                "providerId": payment_ref,
                "deliveryDate": delivery_date_ms,
                "invoiceNumber": invoice_number,
                "trackingCode": tracking_code,
                "products": products or [],
            },
            params={"type": purchase_type},
        )

    def refund(
        self,
        payment_ref: str,
        amount_irr: int,
        *,
        reason: str | None = None,
        purchase_type: str = PURCHASE_TYPE_BNPL,
        sale_tracking_code: str | None = None,
    ) -> RefundResult:
        """Request a refund (partial supported; 200 = registered only).

        ``payment_ref`` is the ticket's ``providerId``. Finality
        needs :meth:`inquire_refund` — the returned status is
        PENDING until the inquiry confirms it.
        """
        if amount_irr <= 0:
            raise GatewayRejectedError(
                f"DigiPay refund needs a positive amount, got {amount_irr}.",
                details={"gateway": "digipay", "operation": "refund"},
            )
        payload = self._authed_call(
            "POST",
            "refunds",
            operation="refund",
            json_body={
                "providerId": payment_ref,
                "amount": amount_irr,
                "saleTrackingCode": sale_tracking_code,
            },
            params={"type": purchase_type},
        )
        try:
            answer = _RefundResponse.model_validate(payload)
        except ValidationError as exc:
            raise GatewayInvalidResponseError(
                "DigiPay returned a refund answer that does not match the UPG contract.",
                details={"gateway": "digipay", "operation": "refund", "error": str(exc)},
            ) from exc
        inquiry_id = answer.inquiryId or answer.id or payment_ref
        return RefundResult(
            refund_id=inquiry_id,
            amount_irr=amount_irr,
            status=GatewayTransactionStatus.PENDING,
        )

    def inquire_refund(
        self,
        inquiry_id: str,
        *,
        purchase_type: str = PURCHASE_TYPE_BNPL,
    ) -> GatewayTransactionStatus:
        """Follow up a refund's final state (0/1/2 → refunded/failed/pending)."""
        payload = self._authed_call(
            "GET",
            f"refunds/{inquiry_id}",
            operation="refund_inquiry",
            params={"type": purchase_type},
        )
        raw_status: object = None
        for key in ("status", "refundStatus", "result"):
            if key in payload:
                raw_status = payload[key]
                break
        try:
            code = int(raw_status)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise GatewayInvalidResponseError(
                "DigiPay returned a refund-inquiry answer without a numeric status.",
                details={"gateway": "digipay", "operation": "refund_inquiry"},
            ) from None
        status = _REFUND_STATUS_MAP.get(code)
        if status is None:
            return GatewayTransactionStatus.UNKNOWN
        return status

    def create_ticket(
        self,
        *,
        amount_irr: int,
        cell_number: str,
        callback_url: str,
        purchase_type: str = PURCHASE_TYPE_WALLET,
    ) -> dict[str, str | None]:
        """Create a purchase ticket (the checkout's first step)."""
        payload = self._authed_call(
            "POST",
            "tickets/business",
            operation="create_ticket",
            json_body={
                "amount": amount_irr,
                "cellNumber": cell_number,
                "callbackUrl": callback_url,
            },
            params={"type": purchase_type},
        )
        try:
            answer = _TicketResponse.model_validate(payload)
        except ValidationError as exc:
            raise GatewayInvalidResponseError(
                "DigiPay returned a ticket answer that does not match the UPG contract.",
                details={"gateway": "digipay", "operation": "create_ticket"},
            ) from exc
        if not answer.ticket:
            raise GatewayInvalidResponseError(
                "DigiPay returned a ticket answer without a ticket.",
                details={"gateway": "digipay", "operation": "create_ticket"},
            )
        return {"ticket": answer.ticket, "redirect_url": answer.redirectUrl}

    def list_transactions(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[GatewayTransaction]:
        """Not supported: UPG has no transaction-list endpoint."""
        raise GatewayUnsupportedOperationError(
            "DigiPay has no transaction-list API; reconciliation runs on store capture + per-payment verify.",
            details={"gateway": "digipay", "operation": "list_transactions"},
        )
