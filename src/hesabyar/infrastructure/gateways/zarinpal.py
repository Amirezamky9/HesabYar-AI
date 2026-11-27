"""ZarinPal gateway client (US-524).

Two APIs, both verified from the open-source SDKs (no official
GraphQL docs exist — ``developers.zarinpal.com`` was unreachable
during research, and the PyPI SDK publisher is not verifiably
ZarinPal itself, so the GraphQL surface is marked as SDK-verified,
not officially documented):

- REST v4 payment gateway — ``https://payment.zarinpal.com``
  (sandbox ``https://sandbox.zarinpal.com``):
  ``/pg/v4/payment/verify.json``,
  ``/pg/v4/payment/inquiry.json``,
  ``/pg/v4/payment/reverse.json``,
  ``/pg/v4/payment/unVerified.json``. Every call posts
  ``merchant_id`` (a UUID) with its own fields. Success is
  ``data.code`` 100 (verified) or 101 (already verified); anything
  else is a rejection carrying the provider's code.
- GraphQL transaction API —
  ``https://next.zarinpal.com/api/v4/graphql/`` (Bearer access token
  from the developer panel): the ``Session`` list query (terminal,
  filter, limit, offset) and the ``AddRefund`` mutation. Verified
  from ``zarinpal-py-sdk`` 0.1.1 + ``zarinpal-go-sdk`` +
  J2Commerce docs (three independent sources).

Settlement is T+1 lump (official blog) and comes from the merchant
panel + bank matching — the GraphQL settlement/payout query is
⚠️ UNVERIFIED and is NOT relied on anywhere here. A server webhook
is ⚠️ UNVERIFIED too: until confirmed, the client assumes none and
reconciliation runs on store capture + polling.

Credentials (encrypted in the channel registry): ``merchant_id``
(UUID, REST), ``access_token`` (GraphQL), ``terminal_id`` (GraphQL
list). Optional non-secret overrides in the same payload:
``rest_base_url``, ``graphql_url``.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from hesabyar.domain.gateways.errors import (
    GatewayInvalidResponseError,
    GatewayNotConfiguredError,
    GatewayRejectedError,
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

__all__ = ["ZarinPalClient"]

#: REST v4 base URLs (official docs; sandbox from the SDK).
REST_BASE_URL = "https://payment.zarinpal.com"
REST_SANDBOX_BASE_URL = "https://sandbox.zarinpal.com"

#: GraphQL endpoint (zarinpal-py-sdk 0.1.1 source; no official docs).
GRAPHQL_URL = "https://next.zarinpal.com/api/v4/graphql/"

#: Verify/inquiry success codes (100 = verified, 101 = already verified).
_SUCCESS_CODES = (100, 101)

#: GraphQL list filters (SDK validator vocabulary).
_LIST_FILTERS = ("PAID", "VERIFIED", "TRASH", "ACTIVE", "REFUNDED")

_LIST_TRANSACTIONS_QUERY = """
query GetTransactions($terminal_id: ID!, $filter: FilterEnum, $limit: Int, $offset: Int) {
    Session: Session(
        terminal_id: $terminal_id,
        filter: $filter,
        limit: $limit,
        offset: $offset
    ) {
        id,
        status,
        amount,
        description,
        created_at
    }
}
"""

_ADD_REFUND_MUTATION = """
mutation AddRefund($session_id: ID!, $amount: BigInteger!, $description: String,
                   $method: InstantPayoutActionTypeEnum, $reason: RefundReasonEnum){
    resource: AddRefund(
        session_id: $session_id,
        amount: $amount,
        description: $description,
        method: $method,
        reason: $reason
    ) {
        terminal_id,
        id,
        amount,
        timeline {
            refund_amount,
            refund_time,
            refund_status
        }
    }
}
"""


def _map_session_status(raw: str | None) -> GatewayTransactionStatus:
    """Map a GraphQL Session status onto the canonical vocabulary.

    Best-effort mapping from the filter vocabulary (PAID / VERIFIED /
    TRASH / ACTIVE / REFUNDED): an unmapped value is UNKNOWN, never a
    guess — the raw value travels on the transaction row.
    """
    if raw is None:
        return GatewayTransactionStatus.UNKNOWN
    normalized = raw.strip().upper()
    if normalized in ("PAID", "VERIFIED"):
        return GatewayTransactionStatus.PAID
    if normalized == "REFUNDED":
        return GatewayTransactionStatus.REFUNDED
    if normalized in ("TRASH", "CANCELLED", "CANCELED"):
        return GatewayTransactionStatus.CANCELLED
    if normalized in ("ACTIVE", "PENDING"):
        return GatewayTransactionStatus.PENDING
    return GatewayTransactionStatus.UNKNOWN


class _RestData(BaseModel):
    """The ``data`` half of a ZarinPal REST v4 answer."""

    model_config = ConfigDict(extra="ignore")

    code: int
    authority: str | None = None
    ref_id: int | str | None = None
    card_pan: str | None = None
    card_hash: str | None = None
    fee_type: str | None = None
    fee: int | None = None


class _RestError(BaseModel):
    """One entry of a REST v4 ``errors`` array."""

    model_config = ConfigDict(extra="ignore")

    code: int | None = None
    message: str | None = None


class _RestResponse(BaseModel):
    """A ZarinPal REST v4 answer (``data`` + ``errors``)."""

    model_config = ConfigDict(extra="ignore")

    data: _RestData | None = None
    errors: list[_RestError] = Field(default_factory=list)


class _SessionRow(BaseModel):
    """One row of the GraphQL ``Session`` list."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1)
    status: str | None = None
    amount: int
    description: str | None = None
    created_at: str | None = None


class _GraphQLError(BaseModel):
    """One entry of a GraphQL ``errors`` array."""

    model_config = ConfigDict(extra="ignore")

    message: str | None = None


class _GraphQLResponse(BaseModel):
    """A GraphQL answer (``data`` + ``errors``)."""

    model_config = ConfigDict(extra="ignore")

    data: dict[str, Any] | None = None
    errors: list[_GraphQLError] = Field(default_factory=list)


class _RefundTimeline(BaseModel):
    """The ``timeline`` half of an ``AddRefund`` answer."""

    model_config = ConfigDict(extra="ignore")

    refund_amount: int | None = None
    refund_time: str | None = None
    refund_status: str | None = None


class _RefundResource(BaseModel):
    """The ``resource`` half of an ``AddRefund`` answer."""

    model_config = ConfigDict(extra="ignore")

    terminal_id: str | None = None
    id: str = Field(min_length=1)
    amount: int
    timeline: _RefundTimeline | None = None


def _parse_created_at(raw: str | None) -> datetime.datetime | None:
    """Parse a GraphQL ``created_at`` leniently (None when unparseable)."""
    if not raw:
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return parsed


class ZarinPalClient(BaseGatewayHttpClient):
    """ZarinPal: REST v4 verify/inquiry/reverse + GraphQL list/refund."""

    def __init__(
        self,
        *,
        merchant_id: str,
        access_token: str | None = None,
        terminal_id: str | None = None,
        environment: str = "production",
        rest_base_url: str | None = None,
        graphql_url: str | None = None,
        timeout: float = DEFAULT_GATEWAY_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not merchant_id.strip():
            raise GatewayNotConfiguredError(
                "ZarinPal needs a merchant_id (UUID from the merchant panel).",
                details={"gateway": "zarinpal", "missing_field": "merchant_id"},
            )
        base = rest_base_url or (REST_SANDBOX_BASE_URL if environment == "stage" else REST_BASE_URL)
        super().__init__(
            gateway_name="zarinpal",
            base_url=base,
            timeout=timeout,
            transport=transport,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        self._merchant_id = merchant_id
        self._access_token = access_token
        self._terminal_id = terminal_id
        self._graphql_url = graphql_url or GRAPHQL_URL

    @property
    def channel_key(self) -> str:
        """The registry channel key this client serves."""
        return "zarinpal"

    @property
    def capabilities(self) -> GatewayCapabilities:
        """What ZarinPal can and cannot do (see the module docstring)."""
        return GatewayCapabilities(
            channel_key="zarinpal",
            has_payment_api=True,
            supports_verify=True,
            supports_inquiry=True,
            supports_refund=True,
            supports_list=True,
            has_settlement_api=False,
            has_server_webhook=False,
            settlement_source=SettlementSource.PORTAL_FILE,
            settlement_model_note=(
                "T+1 lump settlement (official blog); the schedule comes from the channel's contract, not from the API."
            ),
            open_questions=(
                "Settlement/payout query in GraphQL is UNVERIFIED — settlement "
                "comes from the merchant panel + bank matching.",
                "Server webhook is UNVERIFIED — reconciliation assumes none and relies on store capture + polling.",
            ),
        )

    # -- REST v4 ----------------------------------------------------------

    def _rest_call(self, path: str, payload: dict[str, Any], *, operation: str) -> _RestData:
        """One REST v4 call: merchant_id + payload, code-checked."""
        body = {"merchant_id": self._merchant_id, **payload}
        response = self._request("POST", path, operation=operation, json_body=body)
        parsed = self._json_or_raise(response, operation=operation)
        try:
            answer = _RestResponse.model_validate(parsed)
        except ValidationError as exc:
            raise GatewayInvalidResponseError(
                f"ZarinPal returned a {operation} answer that does not match the REST v4 contract.",
                details={"gateway": "zarinpal", "operation": operation, "error": str(exc)},
            ) from exc
        if answer.data is None or answer.data.code not in _SUCCESS_CODES:
            code = answer.data.code if answer.data is not None else None
            provider_message = next((err.message for err in answer.errors if err.message), None)
            raise GatewayRejectedError(
                f"ZarinPal rejected {operation}"
                + (f" (code {code})" if code is not None else "")
                + (f": {provider_message}" if provider_message else "."),
                details={
                    "gateway": "zarinpal",
                    "operation": operation,
                    "provider_code": code,
                    "provider_message": provider_message,
                },
            )
        return answer.data

    def verify_payment(
        self,
        payment_ref: str,
        *,
        expected_amount_irr: int | None = None,
    ) -> VerifiedPayment:
        """Verify a payment by its authority string (REST v4 verify).

        ``payment_ref`` is the ``authority`` (``A…``/``S…`` + 35
        chars). The verify answer carries no confirmation timestamp,
        so ``gateway_confirmed_at`` is the verification time — the
        exact ``created_at`` from the transaction list refines it
        during list reconciliation. ``expected_amount_irr`` is
        required here: verify without an amount cannot prove the
        payment matches the order.
        """
        if expected_amount_irr is None or expected_amount_irr <= 0:
            raise GatewayRejectedError(
                "ZarinPal verify needs the expected amount in rials: the "
                "gateway verifies (authority, amount) together.",
                details={"gateway": "zarinpal", "operation": "verify"},
            )
        data = self._rest_call(
            "/pg/v4/payment/verify.json",
            {"amount": expected_amount_irr, "authority": payment_ref},
            operation="verify",
        )
        ref_id = str(data.ref_id) if data.ref_id is not None else None
        return VerifiedPayment(
            gateway_transaction_id=payment_ref,
            gross_irr=expected_amount_irr,
            gateway_confirmed_at=datetime.datetime.now(datetime.UTC),
            status=GatewayTransactionStatus.PAID,
            tracking_ref=ref_id,
            raw={"code": data.code, "authority": payment_ref},
        )

    def inquire_status(self, payment_ref: str) -> GatewayInquiry:
        """Ask ZarinPal for a payment's status (REST v4 inquiry)."""
        data = self._rest_call(
            "/pg/v4/payment/inquiry.json",
            {"authority": payment_ref},
            operation="inquiry",
        )
        ref_id = str(data.ref_id) if data.ref_id is not None else None
        return GatewayInquiry(
            gateway_transaction_id=payment_ref,
            status=GatewayTransactionStatus.PAID,
            amount_irr=None,
            message=f"code {data.code}" + (f", ref_id {ref_id}" if ref_id else ""),
        )

    def reverse_payment(self, payment_ref: str) -> None:
        """Reverse an unverified payment (REST v4 reverse)."""
        self._rest_call(
            "/pg/v4/payment/reverse.json",
            {"authority": payment_ref},
            operation="reverse",
        )

    # -- GraphQL ----------------------------------------------------------

    def _graphql_call(self, query: str, variables: dict[str, Any], *, operation: str) -> dict[str, Any]:
        """One GraphQL call (Bearer access token), error-checked."""
        if not self._access_token:
            raise GatewayNotConfiguredError(
                f"ZarinPal {operation} needs an access_token (developer panel).",
                details={"gateway": "zarinpal", "missing_field": "access_token"},
            )
        response = self._request(
            "POST",
            self._graphql_url,
            operation=operation,
            json_body={"query": query, "variables": variables},
            headers={"Authorization": f"Bearer {self._access_token}"},
        )
        parsed = self._json_or_raise(response, operation=operation)
        try:
            answer = _GraphQLResponse.model_validate(parsed)
        except ValidationError as exc:
            raise GatewayInvalidResponseError(
                f"ZarinPal returned a {operation} answer that is not GraphQL.",
                details={"gateway": "zarinpal", "operation": operation, "error": str(exc)},
            ) from exc
        if answer.errors:
            message = next(
                (err.message for err in answer.errors if err.message),
                "GraphQL error",
            )
            raise GatewayRejectedError(
                f"ZarinPal rejected {operation}: {message}",
                details={
                    "gateway": "zarinpal",
                    "operation": operation,
                    "provider_message": message,
                },
            )
        if answer.data is None:
            raise GatewayInvalidResponseError(
                f"ZarinPal returned no data for {operation}.",
                details={"gateway": "zarinpal", "operation": operation},
            )
        return answer.data

    def list_transactions(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[GatewayTransaction]:
        """List transactions via GraphQL (filter PAID, paged).

        ``terminal_id`` is required (the list is per-terminal).
        The list is the reconciliation source for exact confirmation
        times (``created_at``) that verify/inquiry do not carry.
        """
        if not self._terminal_id:
            raise GatewayNotConfiguredError(
                "ZarinPal transaction listing needs a terminal_id.",
                details={"gateway": "zarinpal", "missing_field": "terminal_id"},
            )
        if limit <= 0 or offset < 0:
            raise GatewayRejectedError(
                f"Invalid pagination for the ZarinPal list: limit={limit}, offset={offset}.",
                details={"gateway": "zarinpal", "operation": "list"},
            )
        data = self._graphql_call(
            _LIST_TRANSACTIONS_QUERY,
            {
                "terminal_id": self._terminal_id,
                "filter": "PAID",
                "limit": limit,
                "offset": offset,
            },
            operation="list_transactions",
        )
        rows = data.get("Session")
        if not isinstance(rows, list):
            raise GatewayInvalidResponseError(
                "ZarinPal returned a transaction list without a Session array.",
                details={"gateway": "zarinpal", "operation": "list_transactions"},
            )
        transactions: list[GatewayTransaction] = []
        for row in rows:
            try:
                parsed = _SessionRow.model_validate(row)
            except ValidationError as exc:
                raise GatewayInvalidResponseError(
                    "ZarinPal returned a transaction row that does not match the GraphQL contract.",
                    details={
                        "gateway": "zarinpal",
                        "operation": "list_transactions",
                        "error": str(exc),
                    },
                ) from exc
            transactions.append(
                GatewayTransaction(
                    gateway_transaction_id=parsed.id,
                    status=_map_session_status(parsed.status),
                    amount_irr=parsed.amount,
                    confirmed_at=_parse_created_at(parsed.created_at),
                    description=parsed.description,
                )
            )
        return transactions

    def refund(
        self,
        payment_ref: str,
        amount_irr: int,
        *,
        reason: str | None = None,
    ) -> RefundResult:
        """Refund a payment via the GraphQL ``AddRefund`` mutation.

        ``payment_ref`` is the GraphQL session id (the transaction id
        from ``list_transactions``), NOT the REST authority.
        """
        if amount_irr <= 0:
            raise GatewayRejectedError(
                f"ZarinPal refund needs a positive amount, got {amount_irr}.",
                details={"gateway": "zarinpal", "operation": "refund"},
            )
        data = self._graphql_call(
            _ADD_REFUND_MUTATION,
            {
                "session_id": payment_ref,
                "amount": amount_irr,
                "description": reason,
                "method": None,
                "reason": None,
            },
            operation="refund",
        )
        resource = data.get("resource")
        try:
            parsed = _RefundResource.model_validate(resource)
        except ValidationError as exc:
            raise GatewayInvalidResponseError(
                "ZarinPal returned a refund answer that does not match the GraphQL contract.",
                details={"gateway": "zarinpal", "operation": "refund", "error": str(exc)},
            ) from exc
        return RefundResult(
            refund_id=parsed.id,
            amount_irr=parsed.amount,
            status=GatewayTransactionStatus.REFUNDED,
        )
