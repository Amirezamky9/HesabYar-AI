"""Shared client for the ``/api/online/...`` gateways (US-524).

SnappPay (REST v2.1, official academy docs) and TorobPay (OAuth
REST, verified via the payvand client + the official WooCommerce
plugin) share the same endpoint shapes — payvand's claim that
TorobPay serves SnappPay's paths is ⚠️ UNVERIFIED against
TorobPay's official docs, so the paths live here as the shared
default and every TorobPay deployment takes its base URL (and, if
the merchant docs disagree, its paths) from its own configuration.

Common shape (SnappPay academy + payvand):

- OAuth: ``POST /api/online/v1/oauth/token`` — form password grant
  (``grant_type=password`` + ``username``/``password``/``scope``)
  with ``Authorization: Basic base64(client_id:client_secret)``.
  The JWT lives 3600s; the client caches it until shortly before
  expiry (SnappPay's docs say not to cache the token — that advice
  targets the payment flow's per-request freshness; this client
  caches only within one reconciliation run and refreshes on any
  401, so a stale token can never block a verify).
- Envelope: every answer wraps in ``{successful, errorData, response}``.
- Follow-up calls post ``{paymentToken}`` to
  ``/api/online/payment/v1/{verify,settle,revert,cancel}``;
  status is ``GET /api/online/payment/v1/status?paymentToken=…``;
  eligibility is ``GET /api/online/offer/v1/eligible?amount=…``;
  update posts the new basket + amount to
  ``/api/online/payment/v1/update``.
- Statuses: ``SETTLE`` / ``CANCEL`` / ``VERIFY`` / ``PENDING`` /
  ``REVERT``.

Verify + settle: SnappPay REQUIRES settle after a successful
verify (an unverified-but-unsettled payment is reverted). TorobPay
is per-contract — ``requires_settle`` comes from the channel's
configuration (default False: ask TorobPay which the contract is,
per payvand's warning). Both gateways settle the merchant per
contract — the API never reports settlement; settlement data comes
from the portal file (+ monthly email for SnappPay).
"""

from __future__ import annotations

import base64
import datetime
from collections.abc import Sequence
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from hesabyar.domain.gateways.errors import (
    GatewayInvalidResponseError,
    GatewayRejectedError,
    GatewayUnsupportedOperationError,
    GatewayVerificationMismatchError,
)
from hesabyar.domain.gateways.models import (
    GatewayInquiry,
    GatewayTransaction,
    GatewayTransactionStatus,
    RefundResult,
    VerifiedPayment,
)
from hesabyar.infrastructure.gateways.common import (
    DEFAULT_GATEWAY_TIMEOUT_SECONDS,
    BaseGatewayHttpClient,
)

__all__ = ["OnlinePaymentClient"]

#: OAuth paths + payment paths (SnappPay v2.1; TorobPay per payvand).
OAUTH_PATH = "/api/online/v1/oauth/token"
ELIGIBLE_PATH = "/api/online/offer/v1/eligible"
TOKEN_PATH = "/api/online/payment/v1/token"
VERIFY_PATH = "/api/online/payment/v1/verify"
SETTLE_PATH = "/api/online/payment/v1/settle"
REVERT_PATH = "/api/online/payment/v1/revert"
STATUS_PATH = "/api/online/payment/v1/status"
CANCEL_PATH = "/api/online/payment/v1/cancel"
UPDATE_PATH = "/api/online/payment/v1/update"

#: Refresh the cached token this far before its expiry.
_TOKEN_REFRESH_MARGIN_SECONDS = 60

#: Canonical status mapping for the ``/status`` vocabulary.
_STATUS_MAP: dict[str, GatewayTransactionStatus] = {
    "SETTLE": GatewayTransactionStatus.PAID,
    "CANCEL": GatewayTransactionStatus.CANCELLED,
    "VERIFY": GatewayTransactionStatus.PENDING,
    "PENDING": GatewayTransactionStatus.PENDING,
    "REVERT": GatewayTransactionStatus.REVERTED,
}


class _ErrorData(BaseModel):
    """The failure half of the provider envelope."""

    model_config = ConfigDict(extra="ignore")

    errorCode: int | str | None = None
    message: str | None = None


class _Envelope(BaseModel):
    """The wrapper the provider puts around every answer."""

    model_config = ConfigDict(extra="ignore")

    successful: bool = False
    errorData: _ErrorData | None = None


class _OAuthResponse(BaseModel):
    """The token endpoint's answer (plain OAuth2, no envelope)."""

    model_config = ConfigDict(extra="ignore")

    access_token: str | None = None
    token_type: str | None = None
    expires_in: int | None = None
    scope: str | None = None
    error: str | None = None
    error_description: str | None = None


class _VerifyData(BaseModel):
    """What the provider reports about a completed payment."""

    model_config = ConfigDict(extra="ignore")

    transactionId: str | None = None
    amount: int | None = None
    originalAmount: int | None = None
    status: str | None = None
    referenceNumber: str | None = None
    electronicCode: str | None = None
    installmentType: str | None = None


class _VerifyResponse(_Envelope):
    """The answer of verify / settle / revert / update."""

    response: _VerifyData | None = None


class _StatusData(BaseModel):
    """The lifecycle state of a payment."""

    model_config = ConfigDict(extra="ignore")

    status: str = Field(min_length=1)
    transactionId: str | None = None
    amount: int | None = None


class _StatusResponse(_Envelope):
    """The answer of the status endpoint."""

    response: _StatusData | None = None


class _TokenData(BaseModel):
    """The payment token + the page the payer is sent to."""

    model_config = ConfigDict(extra="ignore")

    paymentToken: str | None = None
    paymentPageUrl: str | None = None


class _TokenResponse(_Envelope):
    """The answer of the payment-token endpoint."""

    response: _TokenData | None = None


class _Instalment(BaseModel):
    """One repayment plan offered to the payer."""

    model_config = ConfigDict(extra="ignore")

    count: int = 0
    amount: int = 0
    wage: int = 0


class _EligibleData(BaseModel):
    """The eligibility answer for an amount."""

    model_config = ConfigDict(extra="ignore")

    eligible: bool = False
    title: str | None = None
    description: str | None = None
    minEligibleAmount: int | None = None
    maxEligibleAmount: int | None = None
    installmentList: list[_Instalment] = Field(default_factory=list)


class _EligibleResponse(_Envelope):
    """The answer of the eligibility endpoint."""

    response: _EligibleData | None = None


class OnlinePaymentClient(BaseGatewayHttpClient):
    """One ``/api/online/...`` gateway (SnappPay or TorobPay).

    Subclasses supply the channel key, the default base URL, the
    capabilities, and whether settle-after-verify is required.
    Payment initiation (``request_token`` / ``eligible``) exists
    for completeness — the store normally runs the checkout; HesabYar
    reconciles what the store captured.
    """

    def __init__(
        self,
        *,
        gateway_name: str,
        base_url: str,
        client_id: str,
        client_secret: str,
        username: str,
        password: str,
        requires_settle: bool,
        paths: dict[str, str] | None = None,
        scope: str | None = None,
        timeout: float = DEFAULT_GATEWAY_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        super().__init__(
            gateway_name=gateway_name,
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        self._client_id = client_id
        self._client_secret = client_secret
        self._username = username
        self._password = password
        self._requires_settle = requires_settle
        self._scope = scope
        self._paths = {
            "oauth": OAUTH_PATH,
            "eligible": ELIGIBLE_PATH,
            "token": TOKEN_PATH,
            "verify": VERIFY_PATH,
            "settle": SETTLE_PATH,
            "revert": REVERT_PATH,
            "status": STATUS_PATH,
            "cancel": CANCEL_PATH,
            "update": UPDATE_PATH,
            **(paths or {}),
        }
        self._access_token: str | None = None
        self._token_expires_at: datetime.datetime | None = None

    # -- OAuth ------------------------------------------------------------

    def _fetch_token(self) -> None:
        """Run the OAuth password grant and cache the JWT."""
        basic = base64.b64encode(f"{self._client_id}:{self._client_secret}".encode()).decode("ascii")
        form: dict[str, str] = {
            "grant_type": "password",
            "username": self._username,
            "password": self._password,
        }
        if self._scope:
            form["scope"] = self._scope
        response = self._request(
            "POST",
            self._paths["oauth"],
            operation="oauth_token",
            form_body=form,
            headers={"Authorization": f"Basic {basic}"},
        )
        parsed = self._json_or_raise(response, operation="oauth_token")
        try:
            answer = _OAuthResponse.model_validate(parsed)
        except ValidationError as exc:
            raise GatewayInvalidResponseError(
                f"{self._gateway_name} returned a token answer that is not OAuth2.",
                details={"gateway": self._gateway_name, "operation": "oauth_token"},
            ) from exc
        if not answer.access_token:
            raise GatewayRejectedError(
                f"{self._gateway_name} refused the merchant credentials: "
                f"{answer.error_description or answer.error or 'no access_token'}.",
                details={
                    "gateway": self._gateway_name,
                    "operation": "oauth_token",
                    "provider_code": answer.error,
                },
            )
        self._access_token = answer.access_token
        expires_in = answer.expires_in or 3600
        self._token_expires_at = datetime.datetime.now(datetime.UTC) + datetime.timedelta(
            seconds=max(expires_in - _TOKEN_REFRESH_MARGIN_SECONDS, 0)
        )

    def _auth_headers(self) -> dict[str, str]:
        """Bearer headers, refreshing the token when it is stale."""
        now = datetime.datetime.now(datetime.UTC)
        if self._access_token is None or self._token_expires_at is None or now >= self._token_expires_at:
            self._fetch_token()
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
            # The token may have expired between the cache check and
            # the call — refresh once and retry before giving up.
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

    # -- envelope handling --------------------------------------------------

    def _envelope_or_raise(
        self,
        payload: dict[str, Any],
        model: type[_Envelope],
        *,
        operation: str,
    ) -> _Envelope:
        """Validate an envelope answer; a failed one becomes a rejection."""
        try:
            answer = model.model_validate(payload)
        except ValidationError as exc:
            raise GatewayInvalidResponseError(
                f"{self._gateway_name} returned a {operation} answer that does not match the provider envelope.",
                details={
                    "gateway": self._gateway_name,
                    "operation": operation,
                    "error": str(exc),
                },
            ) from exc
        if not answer.successful:
            error = answer.errorData
            code = str(error.errorCode) if error and error.errorCode is not None else None
            message = error.message if error and error.message else None
            raise GatewayRejectedError(
                f"{self._gateway_name} rejected {operation}"
                + (f" (code {code})" if code else "")
                + (f": {message}" if message else "."),
                details={
                    "gateway": self._gateway_name,
                    "operation": operation,
                    "provider_code": code,
                    "provider_message": message,
                },
            )
        return answer

    def _token_call(self, path_key: str, payment_token: str, *, operation: str) -> _VerifyData:
        """One ``{paymentToken}`` POST (verify/settle/revert/cancel)."""
        payload = self._authed_call(
            "POST",
            self._paths[path_key],
            operation=operation,
            json_body={"paymentToken": payment_token},
        )
        answer = self._envelope_or_raise(payload, _VerifyResponse, operation=operation)
        assert isinstance(answer, _VerifyResponse)
        return answer.response or _VerifyData()

    # -- reconciliation operations ------------------------------------------

    def verify_payment(
        self,
        payment_ref: str,
        *,
        expected_amount_irr: int | None = None,
    ) -> VerifiedPayment:
        """Verify a payment token; settle it when the contract requires.

        ``payment_ref`` is the ``paymentToken``. The amount the
        gateway reports is reconciled against ``expected_amount_irr``
        BEFORE settle runs (a mismatch must never settle a wrong
        payment). The gateway reports no confirmation timestamp, so
        ``gateway_confirmed_at`` is the verification time.
        """
        verified = self._token_call("verify", payment_ref, operation="verify")
        if expected_amount_irr is not None and verified.amount is not None and verified.amount != expected_amount_irr:
            raise GatewayVerificationMismatchError(
                f"{self._gateway_name} verified {verified.transactionId or payment_ref} "
                f"for {verified.amount} IRR, but {expected_amount_irr} IRR was expected.",
                details={
                    "gateway": self._gateway_name,
                    "operation": "verify",
                    "expected_amount_irr": expected_amount_irr,
                    "actual_amount_irr": verified.amount,
                },
            )
        if self._requires_settle:
            settled = self._token_call("settle", payment_ref, operation="settle")
            # The settle answer is thinner than verify's: it only adds
            # fields verify did not already report.
            if verified.referenceNumber is None:
                verified = verified.model_copy(update={"referenceNumber": settled.referenceNumber})
            if verified.transactionId is None:
                verified = verified.model_copy(update={"transactionId": settled.transactionId})
        amount = verified.amount
        if amount is None:
            amount = expected_amount_irr
        if amount is None:
            raise GatewayInvalidResponseError(
                f"{self._gateway_name} verified the payment but reported no amount, and no expected amount was given.",
                details={"gateway": self._gateway_name, "operation": "verify"},
            )
        return VerifiedPayment(
            gateway_transaction_id=verified.transactionId or payment_ref,
            gross_irr=amount,
            gateway_confirmed_at=datetime.datetime.now(datetime.UTC),
            status=GatewayTransactionStatus.PAID,
            store_order_id=verified.transactionId,
            tracking_ref=verified.referenceNumber,
            raw={
                "status": verified.status,
                "installmentType": verified.installmentType,
            },
        )

    def inquire_status(self, payment_ref: str) -> GatewayInquiry:
        """Ask the provider for a payment token's current status."""
        payload = self._authed_call(
            "GET",
            self._paths["status"],
            operation="status",
            params={"paymentToken": payment_ref},
        )
        answer = self._envelope_or_raise(payload, _StatusResponse, operation="status")
        assert isinstance(answer, _StatusResponse)
        data = answer.response
        if data is None:
            raise GatewayInvalidResponseError(
                f"{self._gateway_name} returned a status answer without a response.",
                details={"gateway": self._gateway_name, "operation": "status"},
            )
        status = _STATUS_MAP.get(data.status.strip().upper(), GatewayTransactionStatus.UNKNOWN)
        return GatewayInquiry(
            gateway_transaction_id=data.transactionId or payment_ref,
            status=status,
            amount_irr=data.amount,
            message=data.status,
        )

    def refund(
        self,
        payment_ref: str,
        amount_irr: int,
        *,
        reason: str | None = None,
    ) -> RefundResult:
        """Revert a payment through the provider (full amount only).

        The provider reverts the WHOLE payment — a partial amount is
        refused rather than silently escalated to a full revert. The
        caller passes the full payment amount; the provider's answer
        is reconciled against it.
        """
        if amount_irr <= 0:
            raise GatewayRejectedError(
                f"{self._gateway_name} refund needs a positive amount, got {amount_irr}.",
                details={"gateway": self._gateway_name, "operation": "refund"},
            )
        reverted = self._token_call("revert", payment_ref, operation="refund")
        if reverted.amount is not None and reverted.amount != amount_irr:
            raise GatewayVerificationMismatchError(
                f"{self._gateway_name} reverted {reverted.amount} IRR, but a "
                f"refund of {amount_irr} IRR was requested (the provider "
                "reverts the full payment only).",
                details={
                    "gateway": self._gateway_name,
                    "operation": "refund",
                    "expected_amount_irr": amount_irr,
                    "actual_amount_irr": reverted.amount,
                },
            )
        return RefundResult(
            refund_id=reverted.transactionId or payment_ref,
            amount_irr=reverted.amount or amount_irr,
            status=GatewayTransactionStatus.REFUNDED,
        )

    def cancel_payment(self, payment_ref: str) -> None:
        """Cancel a settled payment (irreversible — needs admin approval)."""
        self._token_call("cancel", payment_ref, operation="cancel")

    # -- checkout helpers (the store's job; exposed for completeness) -------

    def check_eligible(self, amount_irr: int) -> dict[str, Any]:
        """Check whether an amount may be paid in instalments."""
        payload = self._authed_call(
            "GET",
            self._paths["eligible"],
            operation="eligible",
            params={"amount": str(amount_irr)},
        )
        answer = self._envelope_or_raise(payload, _EligibleResponse, operation="eligible")
        assert isinstance(answer, _EligibleResponse)
        data = answer.response or _EligibleData()
        return {
            "eligible": data.eligible,
            "title": data.title,
            "description": data.description,
            "min_eligible_amount_irr": data.minEligibleAmount,
            "max_eligible_amount_irr": data.maxEligibleAmount,
            "plans": [
                {"count": plan.count, "amount_irr": plan.amount, "wage_irr": plan.wage} for plan in data.installmentList
            ],
        }

    def request_token(
        self,
        *,
        amount_irr: int,
        transaction_id: str,
        return_url: str,
        mobile: str | None = None,
        cart_list: list[dict[str, Any]] | None = None,
    ) -> dict[str, str | None]:
        """Create a payment token (the checkout's first step)."""
        payload = self._authed_call(
            "POST",
            self._paths["token"],
            operation="payment_token",
            json_body={
                "amount": amount_irr,
                "mobile": mobile,
                "transactionId": transaction_id,
                "returnURL": return_url,
                "cartList": cart_list or [],
            },
        )
        answer = self._envelope_or_raise(payload, _TokenResponse, operation="payment_token")
        assert isinstance(answer, _TokenResponse)
        data = answer.response or _TokenData()
        if not data.paymentToken:
            raise GatewayInvalidResponseError(
                f"{self._gateway_name} returned a payment-token answer without a token.",
                details={"gateway": self._gateway_name, "operation": "payment_token"},
            )
        return {"payment_token": data.paymentToken, "payment_page_url": data.paymentPageUrl}

    def update_payment(
        self,
        payment_ref: str,
        *,
        amount_irr: int,
        cart_list: list[dict[str, Any]] | None = None,
    ) -> None:
        """Change a payment's amount/basket after settle (new < total)."""
        payload = self._authed_call(
            "POST",
            self._paths["update"],
            operation="update",
            json_body={
                "amount": amount_irr,
                "paymentToken": payment_ref,
                "cartList": cart_list or [],
            },
        )
        self._envelope_or_raise(payload, _VerifyResponse, operation="update")

    def list_transactions(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[GatewayTransaction]:
        """Not supported: neither provider exposes a transaction list."""
        raise GatewayUnsupportedOperationError(
            f"{self._gateway_name} has no transaction-list API; reconciliation "
            "runs on store capture + per-payment status polling.",
            details={"gateway": self._gateway_name, "operation": "list_transactions"},
        )
