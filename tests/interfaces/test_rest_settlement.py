"""Tests for the settlement REST endpoints (US-521).

Everything is real — the FastAPI app, scoped API-key auth, the
PostgreSQL database — except the fiscal-period checker, which the
settlement routes do not use (stubbed anyway for app parity with
the integrations tests).

Each test gets a FRESH installation id, so settlement rows never
leak between tests; the fixture deletes the installation's
settlement + registry rows, the provisioned tenants, and the engine
connection in teardown.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Generator
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from hesabyar.infrastructure.auth import ApiKeyCrypto, ApiKeyService
from hesabyar.infrastructure.auth.credential_vault import generate_encryption_key
from hesabyar.infrastructure.config import AppSettings, AuthSettings
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

TEST_DB_URL = os.getenv(
    "HESABYAR_DATABASE_URL",
    "postgresql+psycopg://hesabyar:hesabyar_pass@hesabyar-postgres:5432/hesabyar_test",
)

BASE = "/api/v1/settlements"
INTEGRATIONS = "/api/v1/integrations"
REVIEW_QUEUE = "/api/v1/review-queue"

FOUR_TRANCHES = [
    {"month_offset": 1, "payout_day": 5, "percent": "25"},
    {"month_offset": 2, "payout_day": 5, "percent": "25"},
    {"month_offset": 3, "payout_day": 5, "percent": "25"},
    {"month_offset": 4, "payout_day": 5, "percent": "25"},
]

#: Settlement tables keyed directly by installation id, for teardown.
#: (gateway_payments is deleted first: contracts RESTRICT on it.)
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
    return AppSettings(
        auth=AuthSettings(credentials_encryption_key=generate_encryption_key())
    )


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
        tenant = TenantModel(
            id=str(uuid.uuid4()), slug=f"rest-521-{uuid.uuid4().hex[:8]}", name="Tenant 1"
        )
        other_tenant = TenantModel(
            id=str(uuid.uuid4()), slug=f"rest-521-{uuid.uuid4().hex[:8]}", name="Tenant 2"
        )
        session.add_all([tenant, other_tenant])
        session.flush()
        client = ApiClientModel(id=str(uuid.uuid4()), tenant_id=tenant.id, name="rest-521 client")
        other_client = ApiClientModel(
            id=str(uuid.uuid4()), tenant_id=other_tenant.id, name="rest-521 other client"
        )
        session.add_all([client, other_client])
        session.flush()

        connection = EngineConnectionModel(
            id=str(uuid.uuid4()),
            name=f"conn-521-{uuid.uuid4().hex[:8]}",
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
                name="rest-521 key",
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
                # The tick-filing tests read the queue the tick files into.
                "review:read",
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
                    select(PaymentChannelModel.id).where(
                        PaymentChannelModel.installation_id == installation_id
                    )
                ).scalars()
            )
            if channel_ids:
                cleanup.execute(
                    delete(GatewayContractModel).where(
                        GatewayContractModel.channel_id.in_(channel_ids)
                    )
                )
            cleanup.execute(
                delete(PaymentChannelModel).where(
                    PaymentChannelModel.installation_id == installation_id
                )
            )
            if tenant_id is not None:
                cleanup.query(TenantModel).filter(TenantModel.id == tenant_id).delete(
                    synchronize_session=False
                )
            if other_tenant_id is not None:
                cleanup.query(TenantModel).filter(
                    TenantModel.id == other_tenant_id
                ).delete(synchronize_session=False)
            if connection_id is not None:
                cleanup.query(EngineConnectionModel).filter(
                    EngineConnectionModel.id == connection_id
                ).delete(synchronize_session=False)
            cleanup.commit()
        finally:
            cleanup.close()


@pytest.fixture()
def api(settings: AppSettings) -> TestClient:
    """The real app (settlement routes need no engine stub)."""
    app = create_app(settings=settings)
    return TestClient(app, raise_server_exceptions=False)


def _auth(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


def _setup_channel(api: TestClient, world: World, valid_from: str = "2026-10-23") -> None:
    """Create the snapppay channel + active 4-tranche contract via REST."""
    response = api.post(
        f"{INTEGRATIONS}/channels",
        json={
            "channel_key": "snapppay",
            "display_name_fa": "اسنپ‌پی",
            "channel_type": "bnpl",
        },
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 201, response.text
    response = api.post(
        f"{INTEGRATIONS}/channels/snapppay/contracts",
        json={
            "valid_from": valid_from,
            "fee_percent": "10",
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


def _payment_body(**overrides: object) -> dict:
    body: dict[str, object] = {
        "channel_key": "snapppay",
        "gateway_transaction_id": "TXN-1",
        "gross_irr": 100_000_000,
        "gateway_confirmed_at": "2026-11-06T12:00:00Z",
        "source": "store_webhook",
        "store_order_id": "ORD-1",
    }
    body.update(overrides)
    return body


def _record_payment(api: TestClient, world: World, **overrides: object) -> dict:
    response = api.post(
        f"{BASE}/payments",
        json=_payment_body(**overrides),
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 201, response.text
    return response.json()


# -- auth -------------------------------------------------------------------


def test_missing_auth_is_401(api: TestClient, world: World) -> None:
    response = api.get(f"{BASE}/payments")
    assert response.status_code == 401


def test_wrong_scope_is_403(api: TestClient, world: World) -> None:
    response = api.get(f"{BASE}/payments", headers=_auth(world.key_other_secret))
    assert response.status_code == 403


def test_manage_without_scope_is_403(api: TestClient, world: World) -> None:
    response = api.post(
        f"{BASE}/payments",
        json=_payment_body(),
        headers=_auth(world.key_read_secret),
    )
    assert response.status_code == 403


# -- record a payment -------------------------------------------------------


def test_record_payment_derives_snapppay_schedule(api: TestClient, world: World) -> None:
    _setup_channel(api, world)
    body = _record_payment(api, world)
    payment = body["payment"]
    assert payment["fee_irr"] == 10_000_000
    assert payment["fee_vat_irr"] == 1_000_000
    assert payment["net_irr"] == 89_000_000
    assert payment["sales_period_jalali"] == "1405/08"
    assert payment["status"] == "scheduled"
    assert payment["fee_rate_snapshot"] == "10"
    tranches = body["tranches"]
    assert len(tranches) == 4
    assert [t["net_irr"] for t in tranches] == [22_250_000] * 4
    assert [t["expected_date"] for t in tranches] == [
        "2026-11-26",
        "2026-12-26",
        "2027-01-25",
        "2027-02-24",
    ]
    assert all(t["state"] == "expected" for t in tranches)
    assert body["fee_record_id"]


def test_record_payment_duplicate_is_409(api: TestClient, world: World) -> None:
    _setup_channel(api, world)
    _record_payment(api, world)
    response = api.post(
        f"{BASE}/payments",
        json=_payment_body(),
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "SETTLEMENT_PAYMENT_EXISTS"


def test_record_payment_unknown_channel_is_404(api: TestClient, world: World) -> None:
    response = api.post(
        f"{BASE}/payments",
        json=_payment_body(),
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "INTEGRATION_CHANNEL_NOT_FOUND"


def test_record_payment_without_contract_is_422(api: TestClient, world: World) -> None:
    response = api.post(
        f"{INTEGRATIONS}/channels",
        json={
            "channel_key": "zarinpal",
            "display_name_fa": "زرین‌پال",
            "channel_type": "gateway",
        },
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 201
    response = api.post(
        f"{BASE}/payments",
        json=_payment_body(channel_key="zarinpal"),
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "SETTLEMENT_CONTRACT_MISSING"


def test_record_payment_bad_source_is_400(api: TestClient, world: World) -> None:
    _setup_channel(api, world)
    response = api.post(
        f"{BASE}/payments",
        json=_payment_body(source="smoke_signal"),
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 400


# -- payments reads ---------------------------------------------------------


def test_get_payment_schedule(api: TestClient, world: World) -> None:
    _setup_channel(api, world)
    body = _record_payment(api, world)
    payment_id = body["payment"]["id"]
    response = api.get(f"{BASE}/payments/{payment_id}", headers=_auth(world.key_read_secret))
    assert response.status_code == 200
    data = response.json()
    assert data["payment"]["id"] == payment_id
    assert len(data["tranches"]) == 4


def test_get_payment_unknown_is_404(api: TestClient, world: World) -> None:
    response = api.get(f"{BASE}/payments/{uuid.uuid4()}", headers=_auth(world.key_read_secret))
    assert response.status_code == 404
    assert response.json()["code"] == "SETTLEMENT_PAYMENT_NOT_FOUND"


def test_other_installation_is_invisible(api: TestClient, world: World) -> None:
    _setup_channel(api, world)
    body = _record_payment(api, world)
    payment_id = body["payment"]["id"]
    response = api.get(
        f"{BASE}/payments/{payment_id}", headers=_auth(world.key_other_tenant_secret)
    )
    assert response.status_code == 404
    response = api.get(f"{BASE}/payments", headers=_auth(world.key_other_tenant_secret))
    assert response.status_code == 200
    assert response.json()["items"] == []


def test_list_payments_filters_and_pagination(api: TestClient, world: World) -> None:
    _setup_channel(api, world)
    _record_payment(api, world, gateway_transaction_id="TXN-1")
    _record_payment(api, world, gateway_transaction_id="TXN-2")
    response = api.get(
        f"{BASE}/payments?sales_period=1405/08&limit=1",
        headers=_auth(world.key_read_secret),
    )
    assert response.status_code == 200
    data = response.json()
    assert len(data["items"]) == 1
    assert data["next_cursor"] is not None
    response = api.get(
        f"{BASE}/payments?sales_period=1405/08&limit=1&cursor={data['next_cursor']}",
        headers=_auth(world.key_read_secret),
    )
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
    assert response.json()["next_cursor"] is None
    response = api.get(
        f"{BASE}/payments?sales_period=1405/09",
        headers=_auth(world.key_read_secret),
    )
    assert response.json()["items"] == []


# -- cancel + reassign --------------------------------------------------------


def test_cancel_payment_cancels_open_tranches(api: TestClient, world: World) -> None:
    _setup_channel(api, world)
    body = _record_payment(api, world)
    payment_id = body["payment"]["id"]
    response = api.post(
        f"{BASE}/payments/{payment_id}/cancel",
        json={"reason": "customer refund"},
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 200
    data = response.json()
    assert data["payment"]["status"] == "cancelled"
    assert len(data["cancelled_tranche_ids"]) == 4
    assert data["refunds_deducted_irr"] == 0
    assert data["exception_id"] is None
    response = api.get(
        f"{BASE}/tranches?state=cancelled", headers=_auth(world.key_read_secret)
    )
    assert len(response.json()["items"]) == 4


def test_reassign_period_moves_open_tranches(api: TestClient, world: World) -> None:
    _setup_channel(api, world)
    body = _record_payment(api, world)
    payment_id = body["payment"]["id"]
    response = api.post(
        f"{BASE}/payments/{payment_id}/reassign-period",
        json={"new_sales_period_jalali": "1405/09", "reason": "gateway file shows Azar"},
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 200
    data = response.json()
    assert data["payment"]["sales_period_jalali"] == "1405/09"
    assert len(data["rederived_tranche_ids"]) == 4
    assert data["exception_id"] is not None
    response = api.get(
        f"{BASE}/exceptions/{data['exception_id']}",
        headers=_auth(world.key_read_secret),
    )
    assert response.json()["exception_type"] == "period_reassigned"
    assert response.json()["severity"] == "info"
    response = api.get(f"{BASE}/payments/{payment_id}", headers=_auth(world.key_read_secret))
    assert [t["expected_date"] for t in response.json()["tranches"]] == [
        "2026-12-26",
        "2027-01-25",
        "2027-02-24",
        "2027-03-25",
    ]


def test_bulk_rederive_requires_reason(api: TestClient, world: World) -> None:
    response = api.post(
        f"{BASE}/payments/bulk-rederive",
        json={"actor": "ops", "reason": ""},
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 422  # pydantic min_length on reason


def test_bulk_rederive_rederives_after_rate_change(api: TestClient, world: World) -> None:
    _setup_channel(api, world)
    body = _record_payment(api, world)
    payment_id = body["payment"]["id"]
    response = api.post(
        f"{INTEGRATIONS}/channels/snapppay/contracts",
        json={
            "valid_from": "2026-11-07",
            "fee_percent": "12",
            "fee_vat_rate": "10",
            "settlement_model": "tranches_4",
            "tranche_legs": FOUR_TRANCHES,
            "payout_window_start_day": 1,
            "payout_window_end_day": 5,
        },
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 201, response.text
    response = api.post(
        f"{BASE}/payments/bulk-rederive",
        json={"actor": "ops", "reason": "rate change to 12%"},
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 200
    data = response.json()
    assert data["rederived_payment_ids"] == [payment_id]
    assert data["skipped"] == []
    response = api.get(f"{BASE}/payments/{payment_id}", headers=_auth(world.key_read_secret))
    assert response.json()["payment"]["fee_irr"] == 12_000_000
    assert response.json()["payment"]["net_irr"] == 86_800_000


# -- tranches -----------------------------------------------------------------


def test_list_tranches_filters(api: TestClient, world: World) -> None:
    _setup_channel(api, world)
    _record_payment(api, world)
    response = api.get(
        f"{BASE}/tranches?state=expected&due_from=2026-12-01&due_to=2026-12-31",
        headers=_auth(world.key_read_secret),
    )
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["expected_date"] == "2026-12-26"
    response = api.get(
        f"{BASE}/tranches?state=bogus", headers=_auth(world.key_read_secret)
    )
    assert response.status_code == 400


def test_write_off_expected_tranche_is_422(api: TestClient, world: World) -> None:
    _setup_channel(api, world)
    body = _record_payment(api, world)
    tranche_id = body["tranches"][0]["id"]
    response = api.post(
        f"{BASE}/tranches/{tranche_id}/write-off",
        json={"approved_by": "ops", "reason": "gateway gone"},
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "SETTLEMENT_WRITE_OFF_NOT_ALLOWED"


# -- fee records + tick -------------------------------------------------------


def test_list_fee_records(api: TestClient, world: World) -> None:
    _setup_channel(api, world)
    body = _record_payment(api, world)
    payment_id = body["payment"]["id"]
    response = api.get(
        f"{BASE}/fee-records?gateway_payment_id={payment_id}",
        headers=_auth(world.key_read_secret),
    )
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["expected_fee_irr"] == 10_000_000
    assert items[0]["expected_vat_irr"] == 1_000_000
    assert items[0]["source"] == "calculated"


def test_tick_moves_due_tranches(api: TestClient, world: World) -> None:
    _setup_channel(api, world)
    _record_payment(api, world)
    # All four tranches are due 2026-11-26 … 2027-02-24; "today" in the
    # test run is 2026-11-19, so none is due yet — the tick is a no-op.
    response = api.post(f"{BASE}/tick", headers=_auth(world.key_manage_secret))
    assert response.status_code == 200
    data = response.json()
    assert data["moved_to_due"] == 0
    assert data["moved_to_overdue"] == 0


def test_tick_files_late_tranche_review_items(api: TestClient, world: World) -> None:
    """A tick that creates late tranches files review items (US-555)."""
    _setup_channel(api, world, valid_from="2026-01-01")
    # Confirmed 2026-02-06: all four tranches (due Mar–Jun 2026) are
    # long overdue whenever the suite runs.
    _record_payment(api, world, gateway_confirmed_at="2026-02-06T12:00:00Z")
    response = api.post(f"{BASE}/tick", headers=_auth(world.key_manage_secret))
    assert response.status_code == 200
    data = response.json()
    assert data["moved_to_overdue"] == 4
    assert data["exceptions_created"] == 4
    assert data["review_items_filed"] == 4

    response = api.get(
        f"{REVIEW_QUEUE}/items?type=settlement_variance",
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 4
    for item in items:
        assert item["subtype"] == "late_tranche"
        assert item["channel"] == "snapppay"
        assert item["amount_irr"] == 22_250_000
        assert item["status"] == "open"
        assert item["source_event_ref"].startswith("settlement-exception:")
        assert "snapppay" in item["reason"]
        assert "22,250,000" in item["reason"]
        assert item["suggested_action"]
    reasons = [item["reason"] for item in items]
    for index in range(1, 5):
        assert any(f"قسط {index} از 4" in reason for reason in reasons)


def test_tick_retick_files_no_duplicate_review_items(api: TestClient, world: World) -> None:
    """A re-run tick files nothing: one item per late tranche (US-555)."""
    _setup_channel(api, world, valid_from="2026-01-01")
    _record_payment(api, world, gateway_confirmed_at="2026-02-06T12:00:00Z")
    first = api.post(f"{BASE}/tick", headers=_auth(world.key_manage_secret))
    assert first.status_code == 200
    assert first.json()["review_items_filed"] == 4

    second = api.post(f"{BASE}/tick", headers=_auth(world.key_manage_secret))
    assert second.status_code == 200
    assert second.json()["exceptions_created"] == 0
    assert second.json()["review_items_filed"] == 0

    response = api.get(
        f"{REVIEW_QUEUE}/items?type=settlement_variance",
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 200
    assert len(response.json()["items"]) == 4
