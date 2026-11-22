"""Tests for the gateway reconciliation REST endpoints (US-524).

Everything is real — the FastAPI app, scoped API-key auth, the
PostgreSQL database, the REAL gateway clients — except the gateway
HTTP itself, which is answered by an ``httpx.MockTransport`` (no live
gateway call is ever made from a test). The transport is injected by
overriding ``get_gateway_client_factory``.

Each test gets a FRESH installation id, so gateway/settlement rows
never leak between tests; the fixture deletes the installation's
settlement + registry rows, the provisioned tenants, and the engine
connection in teardown.
"""

from __future__ import annotations

import datetime
import os
import uuid
from collections.abc import Generator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from hesabyar.domain.settlement.models import sales_period_from
from hesabyar.infrastructure.auth import ApiKeyCrypto, ApiKeyService
from hesabyar.infrastructure.auth.credential_vault import generate_encryption_key
from hesabyar.infrastructure.config import AppSettings, AuthSettings
from hesabyar.infrastructure.gateways.factory import GatewayClientFactory
from hesabyar.infrastructure.persistence.postgres.models import (
    ApiClientModel,
    BankDepositModel,
    Base,
    EngineCompanyModel,
    EngineConnectionModel,
    GatewayContractModel,
    GatewayPaymentModel,
    PaymentChannelModel,
    SettlementEventModel,
    SettlementExceptionModel,
    SettlementFeeRecordModel,
    SettlementFileImportModel,
    SettlementMatchModel,
    SettlementTrancheModel,
    TenantModel,
)
from hesabyar.interfaces.rest.app import create_app
from hesabyar.interfaces.rest.dependencies import get_gateway_client_factory

TEST_DB_URL = os.getenv(
    "HESABYAR_DATABASE_URL",
    "postgresql+psycopg://hesabyar:hesabyar_pass@hesabyar-postgres:5432/hesabyar_test",
)

BASE = "/api/v1/gateways"
INTEGRATIONS = "/api/v1/integrations"

FOUR_TRANCHES = [
    {"month_offset": 1, "payout_day": 5, "percent": "25"},
    {"month_offset": 2, "payout_day": 5, "percent": "25"},
    {"month_offset": 3, "payout_day": 5, "percent": "25"},
    {"month_offset": 4, "payout_day": 5, "percent": "25"},
]

#: Settlement tables keyed directly by installation id, for teardown.
_SETTLEMENT_TABLES = (
    SettlementMatchModel,
    SettlementExceptionModel,
    SettlementFeeRecordModel,
    SettlementFileImportModel,
    SettlementTrancheModel,
    SettlementEventModel,
    BankDepositModel,
    GatewayPaymentModel,
)

SNAPPPAY_CREDENTIALS = {
    "client_id": "cid",
    "client_secret": "csecret-test-only",
    "username": "user",
    "password": "pass-test-only",
}

GROSS_IRR = 100_000_000


@dataclass
class World:
    """Provisioned tenant, company, and keys for one test."""

    installation_id: str
    tenant_id: str
    key_read_secret: str
    key_manage_secret: str
    key_other_secret: str
    key_other_tenant_secret: str


@pytest.fixture(scope="module")
def engine() -> Generator[Engine, None, None]:
    eng = create_engine(TEST_DB_URL, echo=False)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def settings() -> AppSettings:
    """Fresh settings with a test-generated vault key (dev pepper)."""
    return AppSettings(auth=AuthSettings(credentials_encryption_key=generate_encryption_key()))


@pytest.fixture()
def world(engine: Engine, settings: AppSettings) -> Generator[World, None, None]:
    """Provision a tenant, one company row, and four keys; clean up after."""
    session = sessionmaker(bind=engine)()
    service = ApiKeyService(ApiKeyCrypto(settings.auth.api_key_pepper))
    installation_id = str(uuid.uuid4())
    tenant_id: str | None = None
    other_tenant_id: str | None = None
    connection_id: str | None = None
    try:
        tenant = TenantModel(id=str(uuid.uuid4()), slug=f"rest-524-{uuid.uuid4().hex[:8]}", name="Tenant 1")
        other_tenant = TenantModel(id=str(uuid.uuid4()), slug=f"rest-524-{uuid.uuid4().hex[:8]}", name="Tenant 2")
        session.add_all([tenant, other_tenant])
        session.flush()
        client = ApiClientModel(id=str(uuid.uuid4()), tenant_id=tenant.id, name="rest-524 client")
        other_client = ApiClientModel(id=str(uuid.uuid4()), tenant_id=other_tenant.id, name="rest-524 other client")
        session.add_all([client, other_client])
        session.flush()

        connection = EngineConnectionModel(
            id=str(uuid.uuid4()),
            name=f"conn-524-{uuid.uuid4().hex[:8]}",
            engine="tryton",
            base_url="http://localhost:8011",
            database_name=f"hesabyar_tryton_test_{uuid.uuid4().hex[:8]}",
        )
        session.add(connection)
        session.flush()
        company = EngineCompanyModel(
            id=str(uuid.uuid4()),
            tenant_id=tenant.id,
            engine_connection_id=connection.id,
            external_company_id="1",
            company_name="Adapter Test",
        )
        session.add(company)
        session.flush()

        def _key(
            client_row: ApiClientModel,
            scopes: list[str],
            *,
            key_installation_id: str = installation_id,
        ):
            return service.issue_key(
                session,
                client=client_row,
                name="rest-524 key",
                scopes=scopes,
                installation_id=key_installation_id,
            )

        key_read = _key(client, ["settlement:read"])
        key_manage = _key(
            client,
            [
                "settlement:read",
                "settlement:manage",
                "integrations:read",
                "integrations:manage",
            ],
        )
        key_other = _key(client, ["company:read"])
        key_other_tenant = _key(
            other_client,
            ["settlement:read"],
            key_installation_id=str(uuid.uuid4()),
        )
        session.commit()
        tenant_id = tenant.id
        other_tenant_id = other_tenant.id
        connection_id = connection.id
        yield World(
            installation_id=installation_id,
            tenant_id=tenant.id,
            key_read_secret=key_read.secret,
            key_manage_secret=key_manage.secret,
            key_other_secret=key_other.secret,
            key_other_tenant_secret=key_other_tenant.secret,
        )
    finally:
        session.close()
        cleanup = sessionmaker(bind=engine)()
        try:
            for model in _SETTLEMENT_TABLES:
                cleanup.execute(
                    delete(model).where(model.installation_id == installation_id)  # type: ignore[attr-defined]
                )
            channel_ids = list(
                cleanup.execute(
                    select(PaymentChannelModel.id).where(PaymentChannelModel.installation_id == installation_id)
                ).scalars()
            )
            if channel_ids:
                cleanup.execute(delete(GatewayContractModel).where(GatewayContractModel.channel_id.in_(channel_ids)))
            cleanup.execute(delete(PaymentChannelModel).where(PaymentChannelModel.installation_id == installation_id))
            if tenant_id is not None:
                cleanup.query(TenantModel).filter(TenantModel.id == tenant_id).delete(synchronize_session=False)
            if other_tenant_id is not None:
                cleanup.query(TenantModel).filter(TenantModel.id == other_tenant_id).delete(synchronize_session=False)
            if connection_id is not None:
                cleanup.query(EngineConnectionModel).filter(EngineConnectionModel.id == connection_id).delete(
                    synchronize_session=False
                )
            cleanup.commit()
        finally:
            cleanup.close()


@pytest.fixture()
def gateway_state() -> dict[str, Any]:
    """Mutable state for the mocked SnappPay gateway (status flips per test)."""
    return {"status": "SETTLE", "amount": GROSS_IRR, "transaction_id": "TXN-1"}


@pytest.fixture()
def api(settings: AppSettings, gateway_state: dict[str, Any]) -> TestClient:
    """The real app with the gateway factory wired to a MockTransport."""
    state = gateway_state

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/oauth/token"):
            return httpx.Response(
                200,
                json={
                    "access_token": "mock-token",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )
        if path.endswith("/payment/v1/verify"):
            return httpx.Response(
                200,
                json={
                    "successful": True,
                    "response": {
                        "transactionId": state["transaction_id"],
                        "amount": state["amount"],
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
                    "response": {
                        "status": state["status"],
                        "transactionId": state["transaction_id"],
                        "amount": state["amount"],
                    },
                },
            )
        if path.endswith("/payment/v1/revert"):
            return httpx.Response(
                200,
                json={
                    "successful": True,
                    "response": {
                        "transactionId": state["transaction_id"],
                        "amount": state["amount"],
                    },
                },
            )
        return httpx.Response(404, json={})

    app = create_app(settings=settings)
    app.dependency_overrides[get_gateway_client_factory] = lambda: GatewayClientFactory(
        transport=httpx.MockTransport(handler)
    )
    return TestClient(app, raise_server_exceptions=False)


def _auth(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


def _setup_channel(
    api: TestClient,
    world: World,
    *,
    channel_key: str = "snapppay",
    channel_type: str = "bnpl",
    fee_percent: str = "10",
    credentials: dict[str, str] | None = SNAPPPAY_CREDENTIALS,
    enable: bool = True,
) -> None:
    """Create a channel + active contract via REST; optionally enable + credential it."""
    response = api.post(
        f"{INTEGRATIONS}/channels",
        json={
            "channel_key": channel_key,
            "display_name_fa": "کانال آزمون",
            "channel_type": channel_type,
        },
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 201, response.text
    response = api.post(
        f"{INTEGRATIONS}/channels/{channel_key}/contracts",
        json={
            "valid_from": "2026-10-23",
            "fee_percent": fee_percent,
            "fee_vat_rate": "10",
            "settlement_model": "tranches_4",
            "tranche_legs": FOUR_TRANCHES,
            "payout_window_start_day": 1,
            "payout_window_end_day": 5,
            "grace_days_after_due": 3,
        },
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 201, response.text
    if credentials is not None:
        response = api.put(
            f"{INTEGRATIONS}/channels/{channel_key}/credentials",
            json={"credentials": credentials},
            headers=_auth(world.key_manage_secret),
        )
        assert response.status_code == 200, response.text
    if enable:
        response = api.patch(
            f"{INTEGRATIONS}/channels/{channel_key}",
            json={"enabled": True},
            headers=_auth(world.key_manage_secret),
        )
        assert response.status_code == 200, response.text


def _verify_and_record(api: TestClient, world: World, **overrides: object) -> dict:
    body: dict[str, object] = {
        "payment_ref": "tok-1",
        "expected_amount_irr": GROSS_IRR,
        "store_order_id": "ORD-1",
    }
    body.update(overrides)
    response = api.post(
        f"{BASE}/snapppay/verify-and-record",
        json=body,
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code in (200, 201), response.text
    return response.json()


# -- auth -------------------------------------------------------------------


def test_missing_auth_is_401(api: TestClient, world: World) -> None:
    response = api.get(f"{BASE}/snapppay/capabilities")
    assert response.status_code == 401


def test_wrong_scope_is_403(api: TestClient, world: World) -> None:
    response = api.get(f"{BASE}/snapppay/capabilities", headers=_auth(world.key_other_secret))
    assert response.status_code == 403


def test_verify_without_manage_scope_is_403(api: TestClient, world: World) -> None:
    response = api.post(
        f"{BASE}/snapppay/verify-and-record",
        json={"payment_ref": "tok-1"},
        headers=_auth(world.key_read_secret),
    )
    assert response.status_code == 403


# -- capabilities -----------------------------------------------------------


def test_capabilities_snapppay(api: TestClient, world: World) -> None:
    _setup_channel(api, world)
    response = api.get(f"{BASE}/snapppay/capabilities", headers=_auth(world.key_read_secret))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["channel_key"] == "snapppay"
    assert body["has_payment_api"] is True
    assert body["supports_verify"] is True
    assert body["supports_inquiry"] is True
    assert body["supports_refund"] is True
    assert body["supports_list"] is False
    assert body["has_settlement_api"] is False  # settlement is file-based, always
    assert body["has_server_webhook"] is False
    assert body["settlement_source"] == "portal_file_and_email"  # portal Excel + monthly email


def test_capabilities_zarinpal_supports_list(api: TestClient, world: World) -> None:
    _setup_channel(api, world, channel_key="zarinpal", channel_type="gateway", credentials=None)
    response = api.get(f"{BASE}/zarinpal/capabilities", headers=_auth(world.key_read_secret))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["supports_list"] is True  # GraphQL list, ZarinPal only
    assert body["has_settlement_api"] is False


def test_capabilities_manual_channel(api: TestClient, world: World) -> None:
    _setup_channel(api, world, channel_key="sadad", channel_type="gateway", credentials=None)
    response = api.get(f"{BASE}/sadad/capabilities", headers=_auth(world.key_read_secret))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["has_payment_api"] is False
    assert body["supports_verify"] is False
    assert body["supports_inquiry"] is False
    assert body["supports_refund"] is False
    assert body["settlement_source"] == "manual_import"


def test_capabilities_unknown_channel_is_404(api: TestClient, world: World) -> None:
    response = api.get(f"{BASE}/ghost/capabilities", headers=_auth(world.key_read_secret))
    assert response.status_code == 404


def test_capabilities_other_tenant_cannot_see_channel(api: TestClient, world: World) -> None:
    _setup_channel(api, world)
    response = api.get(f"{BASE}/snapppay/capabilities", headers=_auth(world.key_other_tenant_secret))
    assert response.status_code == 404


# -- verify-and-record ------------------------------------------------------


def test_verify_and_record_creates_payment(api: TestClient, world: World) -> None:
    _setup_channel(api, world)
    response = api.post(
        f"{BASE}/snapppay/verify-and-record",
        json={
            "payment_ref": "tok-1",
            "expected_amount_irr": GROSS_IRR,
            "store_order_id": "ORD-1",
        },
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["created"] is True
    assert body["payment"]["gateway_transaction_id"] == "TXN-1"
    assert body["payment"]["gross_irr"] == GROSS_IRR
    assert body["payment"]["net_irr"] == 89_000_000  # 10% fee + 10% VAT on fee
    assert body["payment"]["source"] == "gateway_poll"
    assert body["payment"]["store_order_id"] == "ORD-1"
    # The mock verify carries no confirmation timestamp, so the product
    # stamps the reconciliation time (gateways.py) — the period is the
    # CURRENT Jalali month, computed with the product's own helper.
    assert body["payment"]["sales_period_jalali"] == sales_period_from(
        datetime.datetime.now(datetime.UTC)
    )
    assert len(body["tranches"]) == 4


def test_verify_and_record_replay_is_200(api: TestClient, world: World) -> None:
    _setup_channel(api, world)
    first = _verify_and_record(api, world)
    response = api.post(
        f"{BASE}/snapppay/verify-and-record",
        json={"payment_ref": "tok-1", "expected_amount_irr": GROSS_IRR},
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["created"] is False
    assert body["payment"]["id"] == first["payment"]["id"]


def test_verify_mismatch_records_nothing(api: TestClient, world: World) -> None:
    _setup_channel(api, world)
    response = api.post(
        f"{BASE}/snapppay/verify-and-record",
        json={"payment_ref": "tok-1", "expected_amount_irr": 1},
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 422, response.text
    assert "GATEWAY_VERIFICATION_MISMATCH" in response.text


def test_verify_without_credentials_is_422(api: TestClient, world: World) -> None:
    _setup_channel(api, world, credentials=None)
    response = api.post(
        f"{BASE}/snapppay/verify-and-record",
        json={"payment_ref": "tok-1"},
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 422, response.text
    assert "GATEWAY_NOT_CONFIGURED" in response.text


def test_verify_disabled_channel_is_422(api: TestClient, world: World) -> None:
    _setup_channel(api, world, enable=False)
    response = api.post(
        f"{BASE}/snapppay/verify-and-record",
        json={"payment_ref": "tok-1"},
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 422, response.text
    assert "GATEWAY_NOT_CONFIGURED" in response.text


def test_verify_manual_channel_is_422(api: TestClient, world: World) -> None:
    _setup_channel(api, world, channel_key="sadad", channel_type="gateway", credentials=None)
    response = api.post(
        f"{BASE}/sadad/verify-and-record",
        json={"payment_ref": "tok-1"},
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 422, response.text
    assert "GATEWAY_OPERATION_UNSUPPORTED" in response.text


# -- callbacks --------------------------------------------------------------


def test_callback_creates_payment_and_replay_is_200(api: TestClient, world: World) -> None:
    _setup_channel(api, world, channel_key="sadad", channel_type="gateway", credentials=None)
    body = {
        "gateway_transaction_id": "SAD-1",
        "gross_irr": 50_000_000,
        "gateway_confirmed_at": "2026-11-06T12:00:00Z",
        "source": "store_webhook",
        "store_order_id": "ORD-7",
    }
    response = api.post(f"{BASE}/sadad/callbacks", json=body, headers=_auth(world.key_manage_secret))
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["created"] is True
    assert created["payment"]["source"] == "store_webhook"
    assert created["payment"]["store_order_id"] == "ORD-7"
    response = api.post(f"{BASE}/sadad/callbacks", json=body, headers=_auth(world.key_manage_secret))
    assert response.status_code == 200, response.text
    assert response.json()["payment"]["id"] == created["payment"]["id"]


def test_callback_rejects_gateway_poll_source(api: TestClient, world: World) -> None:
    _setup_channel(api, world, channel_key="sadad", channel_type="gateway", credentials=None)
    response = api.post(
        f"{BASE}/sadad/callbacks",
        json={
            "gateway_transaction_id": "SAD-1",
            "gross_irr": 50_000_000,
            "gateway_confirmed_at": "2026-11-06T12:00:00Z",
            "source": "gateway_poll",
        },
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 400, response.text


# -- reconcile --------------------------------------------------------------


def test_reconcile_paid_records_payment(api: TestClient, world: World) -> None:
    _setup_channel(api, world)
    response = api.post(
        f"{BASE}/snapppay/reconcile",
        json={"gateway_transaction_id": "TXN-1"},
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "recorded"
    assert body["inquiry"]["status"] == "paid"
    assert body["payment"]["gross_irr"] == GROSS_IRR
    assert len(body["tranches"]) == 4


def test_reconcile_paid_twice_is_already_recorded(api: TestClient, world: World) -> None:
    _setup_channel(api, world)
    _verify_and_record(api, world)
    response = api.post(
        f"{BASE}/snapppay/reconcile",
        json={"gateway_transaction_id": "TXN-1"},
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 200, response.text
    assert response.json()["outcome"] == "already_recorded"


def test_reconcile_cancelled_cancels_payment(api: TestClient, world: World, gateway_state: dict[str, Any]) -> None:
    _setup_channel(api, world)
    _verify_and_record(api, world)
    gateway_state["status"] = "CANCEL"
    response = api.post(
        f"{BASE}/snapppay/reconcile",
        json={"gateway_transaction_id": "TXN-1"},
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "cancelled"
    assert body["payment"]["status"] == "cancelled"


def test_reconcile_pending_writes_nothing(api: TestClient, world: World, gateway_state: dict[str, Any]) -> None:
    _setup_channel(api, world)
    gateway_state["status"] = "PENDING"
    response = api.post(
        f"{BASE}/snapppay/reconcile",
        json={"gateway_transaction_id": "TXN-1"},
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "pending"
    assert body["payment"] is None


def test_reconcile_manual_channel_is_422(api: TestClient, world: World) -> None:
    _setup_channel(api, world, channel_key="sadad", channel_type="gateway", credentials=None)
    response = api.post(
        f"{BASE}/sadad/reconcile",
        json={"gateway_transaction_id": "SAD-1"},
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 422, response.text
    assert "GATEWAY_OPERATION_UNSUPPORTED" in response.text


# -- refunds ----------------------------------------------------------------


def test_refund_full_cancels_payment(api: TestClient, world: World) -> None:
    _setup_channel(api, world)
    recorded = _verify_and_record(api, world)
    response = api.post(
        f"{BASE}/snapppay/refunds",
        json={
            "payment_id": recorded["payment"]["id"],
            "amount_irr": GROSS_IRR,
            "reason": "customer return",
        },
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["amount_irr"] == GROSS_IRR
    assert body["refund_id"]
    assert body["payment"]["status"] == "cancelled"
    assert len(body["cancelled_tranche_ids"]) == 4


def test_refund_partial_is_422(api: TestClient, world: World) -> None:
    _setup_channel(api, world)
    recorded = _verify_and_record(api, world)
    response = api.post(
        f"{BASE}/snapppay/refunds",
        json={"payment_id": recorded["payment"]["id"], "amount_irr": 1_000_000},
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 422, response.text


# -- channel credentials + reserved marketplace -----------------------------


def test_credentials_rotation_sets_has_credentials_and_never_echoes(api: TestClient, world: World) -> None:
    _setup_channel(api, world, credentials=None)
    before = api.get(f"{INTEGRATIONS}/channels/snapppay", headers=_auth(world.key_manage_secret))
    assert before.status_code == 200, before.text
    assert before.json()["has_credentials"] is False
    response = api.put(
        f"{INTEGRATIONS}/channels/snapppay/credentials",
        json={"credentials": SNAPPPAY_CREDENTIALS},
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 200, response.text
    assert "csecret-test-only" not in response.text
    assert "pass-test-only" not in response.text
    assert response.json()["has_credentials"] is True
    after = api.get(f"{INTEGRATIONS}/channels/snapppay", headers=_auth(world.key_manage_secret))
    assert after.json()["has_credentials"] is True
    assert "csecret-test-only" not in after.text


def test_seed_creates_eight_channels_with_reserved_torob_marketplace(api: TestClient, world: World) -> None:
    response = api.post(f"{INTEGRATIONS}/channels/seed", headers=_auth(world.key_manage_secret))
    assert response.status_code == 201, response.text
    channels = response.json()["items"]
    assert len(channels) == 8
    by_key = {c["channel_key"]: c for c in channels}
    assert "torob_marketplace" in by_key
    assert by_key["torob_marketplace"]["status"] == "disabled"
    assert by_key["torob_marketplace"]["enabled"] is False
    assert "torobpay" in by_key
    assert by_key["torobpay"]["channel_key"] != by_key["torob_marketplace"]["channel_key"]


def test_enabling_reserved_torob_marketplace_is_422(api: TestClient, world: World) -> None:
    response = api.post(f"{INTEGRATIONS}/channels/seed", headers=_auth(world.key_manage_secret))
    assert response.status_code == 201, response.text
    response = api.patch(
        f"{INTEGRATIONS}/channels/torob_marketplace",
        json={"enabled": True},
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 422, response.text
    assert "INTEGRATION_CHANNEL_RESERVED" in response.text
