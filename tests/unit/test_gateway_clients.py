"""Unit tests for the gateway clients (US-524).

Every test runs against ``httpx.MockTransport`` — no live gateway
call is ever made from a test. Each client is covered on its
success path, its rejection path, and its timeout path.
"""

from __future__ import annotations

import json

import httpx
import pytest

from hesabyar.domain.gateways.errors import (
    GatewayInvalidResponseError,
    GatewayNotConfiguredError,
    GatewayRejectedError,
    GatewayUnreachableError,
    GatewayUnsupportedOperationError,
    GatewayVerificationMismatchError,
)
from hesabyar.domain.gateways.models import GatewayTransactionStatus
from hesabyar.domain.integrations.models import ChannelStatus, PaymentChannel
from hesabyar.infrastructure.gateways.digipay import DigiPayClient
from hesabyar.infrastructure.gateways.factory import GatewayClientFactory
from hesabyar.infrastructure.gateways.manual import ManualGatewayClient
from hesabyar.infrastructure.gateways.snapppay import SnappPayClient
from hesabyar.infrastructure.gateways.torobpay import TorobPayClient
from hesabyar.infrastructure.gateways.zarinpal import ZarinPalClient

AUTHORITY = "A0000000000000000000000000006qpmlj9d"
MERCHANT_ID = "123e4567-e89b-12d3-a456-426614174000"


def _mock(transport_fn) -> httpx.MockTransport:
    return httpx.MockTransport(transport_fn)


def _channel(channel_key: str) -> PaymentChannel:
    return PaymentChannel(
        id="00000000-0000-0000-0000-000000000001",
        installation_id="00000000-0000-0000-0000-000000000002",
        channel_key=channel_key,
        display_name_fa="test",
        channel_type="gateway",
        is_builtin=True,
        enabled=True,
        status=ChannelStatus.ACTIVE,
    )


# ---------------------------------------------------------------------------
# ZarinPal
# ---------------------------------------------------------------------------


def _zarinpal_rest(handler):
    return ZarinPalClient(
        merchant_id=MERCHANT_ID,
        access_token="tok",
        terminal_id="term-1",
        transport=_mock(handler),
    )


def test_zarinpal_verify_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/pg/v4/payment/verify.json"
        body = json.loads(request.content)
        assert body["merchant_id"] == MERCHANT_ID
        assert body["authority"] == AUTHORITY
        assert body["amount"] == 1_000_000
        return httpx.Response(
            200,
            json={"data": {"code": 100, "ref_id": 123456789, "authority": AUTHORITY}, "errors": []},
        )

    client = _zarinpal_rest(handler)
    verified = client.verify_payment(AUTHORITY, expected_amount_irr=1_000_000)
    assert verified.status == GatewayTransactionStatus.PAID
    assert verified.gross_irr == 1_000_000
    assert verified.tracking_ref == "123456789"
    client.close()


def test_zarinpal_verify_already_verified_code_101() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"code": 101, "ref_id": 1, "authority": AUTHORITY}, "errors": []})

    client = _zarinpal_rest(handler)
    verified = client.verify_payment(AUTHORITY, expected_amount_irr=100)
    assert verified.status == GatewayTransactionStatus.PAID
    client.close()


def test_zarinpal_verify_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": {"code": -51, "authority": AUTHORITY}, "errors": [{"code": -51, "message": "not paid"}]},
        )

    client = _zarinpal_rest(handler)
    with pytest.raises(GatewayRejectedError):
        client.verify_payment(AUTHORITY, expected_amount_irr=1_000_000)
    client.close()


def test_zarinpal_verify_needs_amount() -> None:
    client = _zarinpal_rest(lambda r: httpx.Response(200, json={}))
    with pytest.raises(GatewayRejectedError, match="expected amount"):
        client.verify_payment(AUTHORITY)
    client.close()


def test_zarinpal_verify_timeout_is_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    client = _zarinpal_rest(handler)
    with pytest.raises(GatewayUnreachableError):
        client.verify_payment(AUTHORITY, expected_amount_irr=1_000_000)
    client.close()


def test_zarinpal_inquiry_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/pg/v4/payment/inquiry.json"
        return httpx.Response(200, json={"data": {"code": 100, "ref_id": 55, "authority": AUTHORITY}, "errors": []})

    client = _zarinpal_rest(handler)
    inquiry = client.inquire_status(AUTHORITY)
    assert inquiry.status == GatewayTransactionStatus.PAID
    client.close()


def test_zarinpal_list_transactions_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v4/graphql/"
        body = json.loads(request.content)
        assert body["variables"]["terminal_id"] == "term-1"
        assert body["variables"]["filter"] == "PAID"
        return httpx.Response(
            200,
            json={
                "data": {
                    "Session": [
                        {
                            "id": "sess-1",
                            "status": "PAID",
                            "amount": 2_000_000,
                            "description": "order",
                            "created_at": "2026-11-06T12:00:00Z",
                        },
                        {"id": "sess-2", "status": "REFUNDED", "amount": 500_000},
                    ]
                }
            },
        )

    client = _zarinpal_rest(handler)
    rows = client.list_transactions(limit=10, offset=0)
    assert len(rows) == 2
    assert rows[0].gateway_transaction_id == "sess-1"
    assert rows[0].status == GatewayTransactionStatus.PAID
    assert rows[0].amount_irr == 2_000_000
    assert rows[0].confirmed_at is not None
    assert rows[1].status == GatewayTransactionStatus.REFUNDED
    client.close()


def test_zarinpal_list_graphql_error_is_rejection() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errors": [{"message": "bad terminal"}]})

    client = _zarinpal_rest(handler)
    with pytest.raises(GatewayRejectedError, match="bad terminal"):
        client.list_transactions()
    client.close()


def test_zarinpal_list_needs_terminal_and_token() -> None:
    no_terminal = ZarinPalClient(
        merchant_id=MERCHANT_ID, access_token="tok", transport=_mock(lambda r: httpx.Response(200, json={}))
    )
    with pytest.raises(GatewayNotConfiguredError, match="terminal_id"):
        no_terminal.list_transactions()
    no_terminal.close()
    no_token = ZarinPalClient(
        merchant_id=MERCHANT_ID, terminal_id="t", transport=_mock(lambda r: httpx.Response(200, json={}))
    )
    with pytest.raises(GatewayNotConfiguredError, match="access_token"):
        no_token.list_transactions()
    no_token.close()


def test_zarinpal_refund_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert "AddRefund" in body["query"]
        assert body["variables"]["session_id"] == "sess-1"
        assert body["variables"]["amount"] == 2_000_000
        return httpx.Response(
            200,
            json={"data": {"resource": {"terminal_id": "term-1", "id": "ref-1", "amount": 2_000_000}}},
        )

    client = _zarinpal_rest(handler)
    refund = client.refund("sess-1", 2_000_000)
    assert refund.refund_id == "ref-1"
    assert refund.status == GatewayTransactionStatus.REFUNDED
    client.close()


def test_zarinpal_capabilities() -> None:
    client = ZarinPalClient(merchant_id=MERCHANT_ID)
    caps = client.capabilities
    assert caps.has_payment_api is True
    assert caps.supports_list is True
    assert caps.has_settlement_api is False
    assert caps.has_server_webhook is False
    assert any("UNVERIFIED" in q for q in caps.open_questions)
    client.close()


# ---------------------------------------------------------------------------
# SnappPay / TorobPay (shared /api/online shape)
# ---------------------------------------------------------------------------

OAUTH_TOKEN = {"access_token": "jwt", "token_type": "bearer", "expires_in": 3600}


def _online_handler(calls: list[str], *, verify_amount: int = 5_000_000, status: str = "SETTLE"):
    """Route OAuth + payment calls for the /api/online gateways."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        if path.endswith("/oauth/token"):
            return httpx.Response(200, json=OAUTH_TOKEN)
        if path.endswith("/payment/v1/verify"):
            return httpx.Response(
                200,
                json={
                    "successful": True,
                    "response": {
                        "transactionId": "ORD-1",
                        "amount": verify_amount,
                        "status": "VERIFY",
                        "referenceNumber": "REF-1",
                    },
                },
            )
        if path.endswith("/payment/v1/settle"):
            return httpx.Response(200, json={"successful": True, "response": {}})
        if path.endswith("/payment/v1/status"):
            return httpx.Response(
                200,
                json={
                    "successful": True,
                    "response": {"status": status, "transactionId": "ORD-1", "amount": verify_amount},
                },
            )
        if path.endswith("/payment/v1/revert"):
            return httpx.Response(
                200,
                json={
                    "successful": True,
                    "response": {"transactionId": "ORD-1", "amount": verify_amount},
                },
            )
        return httpx.Response(404, json={})

    return handler


def test_snapppay_verify_settles_and_caches_token() -> None:
    calls: list[str] = []
    client = SnappPayClient(
        client_id="cid",
        client_secret="csec",
        username="user",
        password="pw",
        transport=_mock(_online_handler(calls)),
    )
    verified = client.verify_payment("tok-1", expected_amount_irr=5_000_000)
    assert verified.status == GatewayTransactionStatus.PAID
    assert verified.gateway_transaction_id == "ORD-1"
    assert verified.tracking_ref == "REF-1"
    # OAuth once, verify once, settle once (mandatory after verify).
    assert calls.count("/api/online/v1/oauth/token") == 1
    assert "/api/online/payment/v1/verify" in calls
    assert "/api/online/payment/v1/settle" in calls
    # A second verify reuses the cached token (no second OAuth call).
    client.verify_payment("tok-2", expected_amount_irr=5_000_000)
    assert calls.count("/api/online/v1/oauth/token") == 1
    client.close()


def test_snapppay_verify_amount_mismatch() -> None:
    calls: list[str] = []
    client = SnappPayClient(
        client_id="cid",
        client_secret="csec",
        username="user",
        password="pw",
        transport=_mock(_online_handler(calls, verify_amount=4_000_000)),
    )
    with pytest.raises(GatewayVerificationMismatchError):
        client.verify_payment("tok-1", expected_amount_irr=5_000_000)
    # No settle ran: a mismatched payment must never settle.
    assert "/api/online/payment/v1/settle" not in calls
    client.close()


@pytest.mark.parametrize(
    "native,expected",
    [
        ("SETTLE", GatewayTransactionStatus.PAID),
        ("CANCEL", GatewayTransactionStatus.CANCELLED),
        ("VERIFY", GatewayTransactionStatus.PENDING),
        ("PENDING", GatewayTransactionStatus.PENDING),
        ("REVERT", GatewayTransactionStatus.REVERTED),
        ("SOMETHING_NEW", GatewayTransactionStatus.UNKNOWN),
    ],
)
def test_snapppay_status_mapping(native: str, expected: GatewayTransactionStatus) -> None:
    calls: list[str] = []
    client = SnappPayClient(
        client_id="cid",
        client_secret="csec",
        username="user",
        password="pw",
        transport=_mock(_online_handler(calls, status=native)),
    )
    inquiry = client.inquire_status("tok-1")
    assert inquiry.status == expected
    assert inquiry.amount_irr == 5_000_000
    client.close()


def test_snapppay_envelope_rejection() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/token"):
            return httpx.Response(200, json=OAUTH_TOKEN)
        return httpx.Response(
            200,
            json={"successful": False, "errorData": {"errorCode": 42, "message": "bad token"}},
        )

    client = SnappPayClient(
        client_id="cid",
        client_secret="csec",
        username="user",
        password="pw",
        transport=_mock(handler),
    )
    with pytest.raises(GatewayRejectedError, match="bad token"):
        client.inquire_status("tok-1")
    client.close()


def test_snapppay_oauth_refusal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "invalid_grant", "error_description": "bad password"})

    client = SnappPayClient(
        client_id="cid",
        client_secret="csec",
        username="user",
        password="pw",
        transport=_mock(handler),
    )
    with pytest.raises(GatewayRejectedError, match="bad password"):
        client.inquire_status("tok-1")
    client.close()


def test_snapppay_timeout_is_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    client = SnappPayClient(
        client_id="cid",
        client_secret="csec",
        username="user",
        password="pw",
        transport=_mock(handler),
    )
    with pytest.raises(GatewayUnreachableError):
        client.inquire_status("tok-1")
    client.close()


def test_snapppay_401_refreshes_token_once() -> None:
    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/oauth/token"):
            attempts.append("oauth")
            return httpx.Response(200, json=OAUTH_TOKEN)
        attempts.append("status")
        if attempts.count("status") == 1:
            return httpx.Response(401, json={})
        return httpx.Response(
            200,
            json={"successful": True, "response": {"status": "SETTLE", "transactionId": "ORD-1"}},
        )

    client = SnappPayClient(
        client_id="cid",
        client_secret="csec",
        username="user",
        password="pw",
        transport=_mock(handler),
    )
    inquiry = client.inquire_status("tok-1")
    assert inquiry.status == GatewayTransactionStatus.PAID
    assert attempts.count("oauth") == 2  # initial + refresh after the 401
    client.close()


def test_snapppay_refund_full_only() -> None:
    calls: list[str] = []
    client = SnappPayClient(
        client_id="cid",
        client_secret="csec",
        username="user",
        password="pw",
        transport=_mock(_online_handler(calls)),
    )
    refund = client.refund("tok-1", 5_000_000)
    assert refund.status == GatewayTransactionStatus.REFUNDED
    with pytest.raises(GatewayVerificationMismatchError):
        client.refund("tok-1", 1_000_000)  # partial: the provider reverts in full
    client.close()


def test_snapppay_list_unsupported() -> None:
    client = SnappPayClient(client_id="c", client_secret="s", username="u", password="p")
    with pytest.raises(GatewayUnsupportedOperationError):
        client.list_transactions()
    client.close()


def test_torobpay_verify_without_settle_by_default() -> None:
    calls: list[str] = []
    client = TorobPayClient(
        client_id="cid",
        client_secret="csec",
        username="user",
        password="pw",
        transport=_mock(_online_handler(calls)),
    )
    client.verify_payment("tok-1", expected_amount_irr=5_000_000)
    assert "/api/online/payment/v1/verify" in calls
    assert "/api/online/payment/v1/settle" not in calls  # per-contract, default off
    client.close()


def test_torobpay_verify_with_settle_when_contract_requires() -> None:
    calls: list[str] = []
    client = TorobPayClient(
        client_id="cid",
        client_secret="csec",
        username="user",
        password="pw",
        requires_settle=True,
        transport=_mock(_online_handler(calls)),
    )
    client.verify_payment("tok-1", expected_amount_irr=5_000_000)
    assert "/api/online/payment/v1/settle" in calls
    client.close()


def test_torobpay_capabilities_flag_unverified() -> None:
    client = TorobPayClient(client_id="c", client_secret="s", username="u", password="p")
    assert any("UNVERIFIED" in q for q in client.capabilities.open_questions)
    assert client.capabilities.has_settlement_api is False
    client.close()


# ---------------------------------------------------------------------------
# DigiPay
# ---------------------------------------------------------------------------


def _digipay_handler(calls: list[str], *, refund_status: int = 0):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        if path.endswith("/oauth/token"):
            return httpx.Response(
                200,
                json={"access_token": "jwt", "refresh_token": "rjwt", "expires_in": 3600},
            )
        if path.endswith("/purchases/verify"):
            return httpx.Response(
                200,
                json={
                    "amount": 3_000_000,
                    "trackingCode": "TRK-1",
                    "providerId": "ORD-9",
                    "paymentGateway": 10,
                    "creditProvider": "DIGIPAY",
                },
            )
        if path.endswith("/purchases/deliver"):
            return httpx.Response(200, json={"success": True})
        if path.endswith("/refunds"):
            return httpx.Response(200, json={"inquiryId": "inq-1"})
        if "/refunds/" in path:
            return httpx.Response(200, json={"status": refund_status})
        if path.endswith("/tickets/business"):
            return httpx.Response(200, json={"ticket": "tkt-1", "redirectUrl": "https://pay/x"})
        return httpx.Response(404, json={})

    return handler


def test_digipay_verify_success() -> None:
    calls: list[str] = []
    client = DigiPayClient(
        client_id="cid",
        client_secret="csec",
        username="user",
        password="pw",
        transport=_mock(_digipay_handler(calls)),
    )
    verified = client.verify_payment("ORD-9", expected_amount_irr=3_000_000)
    assert verified.status == GatewayTransactionStatus.PAID
    assert verified.gateway_transaction_id == "ORD-9"
    assert verified.tracking_ref == "TRK-1"
    client.close()


def test_digipay_verify_amount_mismatch() -> None:
    calls: list[str] = []
    client = DigiPayClient(
        client_id="cid",
        client_secret="csec",
        username="user",
        password="pw",
        transport=_mock(_digipay_handler(calls)),
    )
    with pytest.raises(GatewayVerificationMismatchError):
        client.verify_payment("ORD-9", expected_amount_irr=1_000_000)
    client.close()


def test_digipay_verify_timeout_is_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    client = DigiPayClient(
        client_id="cid",
        client_secret="csec",
        username="user",
        password="pw",
        transport=_mock(handler),
    )
    with pytest.raises(GatewayUnreachableError):
        client.verify_payment("ORD-9")
    client.close()


def test_digipay_deliver_posts() -> None:
    calls: list[str] = []
    client = DigiPayClient(
        client_id="cid",
        client_secret="csec",
        username="user",
        password="pw",
        transport=_mock(_digipay_handler(calls)),
    )
    client.deliver("ORD-9", invoice_number="INV-1", tracking_code="TRK-1")
    assert any(p.endswith("/purchases/deliver") for p in calls)
    client.close()


def test_digipay_refund_pending_until_inquiry() -> None:
    calls: list[str] = []
    client = DigiPayClient(
        client_id="cid",
        client_secret="csec",
        username="user",
        password="pw",
        transport=_mock(_digipay_handler(calls)),
    )
    refund = client.refund("ORD-9", 1_000_000)
    assert refund.refund_id == "inq-1"
    assert refund.status == GatewayTransactionStatus.PENDING  # 200 = registered only
    assert client.inquire_refund("inq-1") == GatewayTransactionStatus.REFUNDED
    client.close()


@pytest.mark.parametrize(
    "code,expected",
    [
        (0, GatewayTransactionStatus.REFUNDED),
        (1, GatewayTransactionStatus.FAILED),
        (2, GatewayTransactionStatus.PENDING),
        (9, GatewayTransactionStatus.UNKNOWN),
    ],
)
def test_digipay_refund_inquiry_statuses(code: int, expected: GatewayTransactionStatus) -> None:
    calls: list[str] = []
    client = DigiPayClient(
        client_id="cid",
        client_secret="csec",
        username="user",
        password="pw",
        transport=_mock(_digipay_handler(calls, refund_status=code)),
    )
    assert client.inquire_refund("inq-1") == expected
    client.close()


def test_digipay_create_ticket() -> None:
    calls: list[str] = []
    client = DigiPayClient(
        client_id="cid",
        client_secret="csec",
        username="user",
        password="pw",
        transport=_mock(_digipay_handler(calls)),
    )
    ticket = client.create_ticket(amount_irr=3_000_000, cell_number="09123456789", callback_url="https://shop/cb")
    assert ticket["ticket"] == "tkt-1"
    client.close()


def test_digipay_capabilities_flag_open_questions() -> None:
    client = DigiPayClient(client_id="c", client_secret="s", username="u", password="p")
    assert any("UNVERIFIED" in q for q in client.capabilities.open_questions)
    assert client.capabilities.has_server_webhook is False
    client.close()


# ---------------------------------------------------------------------------
# Manual channels + factory
# ---------------------------------------------------------------------------


def test_manual_client_rejects_all_api_ops() -> None:
    client = ManualGatewayClient("sadad")
    with pytest.raises(GatewayUnsupportedOperationError):
        client.verify_payment("x")
    with pytest.raises(GatewayUnsupportedOperationError):
        client.inquire_status("x")
    with pytest.raises(GatewayUnsupportedOperationError):
        client.refund("x", 100)
    with pytest.raises(GatewayUnsupportedOperationError):
        client.list_transactions()
    assert client.capabilities.has_payment_api is False


def test_manual_salampay_names_two_contract_models() -> None:
    client = ManualGatewayClient("salampay")
    note = client.capabilities.settlement_model_note
    assert "10-working-day" in note
    assert "credit-line" in note


def test_factory_builds_each_api_client() -> None:
    factory = GatewayClientFactory(transport=_mock(lambda r: httpx.Response(200, json={})))
    cases = {
        "zarinpal": {"merchant_id": MERCHANT_ID},
        "snapppay": {"client_id": "c", "client_secret": "s", "username": "u", "password": "p"},
        "torobpay": {"client_id": "c", "client_secret": "s", "username": "u", "password": "p"},
        "digipay": {"client_id": "c", "client_secret": "s", "username": "u", "password": "p"},
    }
    for key, creds in cases.items():
        client = factory.build(_channel(key), creds)
        assert client.channel_key == key
        assert client.capabilities.has_payment_api is True
        client.close()


def test_factory_manual_for_sadad_salampay_and_custom() -> None:
    factory = GatewayClientFactory()
    for key in ("sadad", "salampay", "some_custom_channel"):
        client = factory.build(_channel(key), {})
        assert isinstance(client, ManualGatewayClient)
        assert client.capabilities.has_payment_api is False
        client.close()


def test_factory_refuses_marketplace_channels() -> None:
    factory = GatewayClientFactory()
    for key in ("basalam_marketplace", "torob_marketplace"):
        with pytest.raises(GatewayUnsupportedOperationError):
            factory.build(_channel(key), {})


def test_factory_missing_credentials() -> None:
    factory = GatewayClientFactory()
    with pytest.raises(GatewayNotConfiguredError, match="password"):
        factory.build(
            _channel("snapppay"),
            {"client_id": "c", "client_secret": "s", "username": "u"},
        )


def test_factory_capabilities_need_no_http() -> None:
    def explode(request: httpx.Request) -> httpx.Response:
        raise AssertionError("capabilities must not call the network")

    factory = GatewayClientFactory(transport=_mock(explode))
    caps = factory.capabilities_for(_channel("zarinpal"))
    assert caps.channel_key == "zarinpal"
    assert caps.supports_list is True
    reserved = factory.capabilities_for(_channel("torob_marketplace"))
    assert reserved.has_payment_api is False
    assert factory.requires_credentials(_channel("snapppay")) is True
    assert factory.requires_credentials(_channel("sadad")) is False


def test_factory_invalid_base_url_rejected() -> None:
    factory = GatewayClientFactory()
    with pytest.raises(GatewayRejectedError, match="not an http"):
        factory.build(
            _channel("snapppay"),
            {
                "client_id": "c",
                "client_secret": "s",
                "username": "u",
                "password": "p",
                "base_url": "ftp://example.com",
            },
        )


def test_gateway_invalid_json_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    client = _zarinpal_rest(handler)
    with pytest.raises(GatewayInvalidResponseError):
        client.verify_payment(AUTHORITY, expected_amount_irr=1_000_000)
    client.close()
