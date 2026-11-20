"""Tests for the settlement import + matching + tie-out REST endpoints (US-523).

Everything is real — the FastAPI app, scoped API-key auth, the
PostgreSQL database, the real file reader (a real CSV is uploaded) —
except the engine: the tie-out tests run without a company (no gate
evaluation, no posting), so no Tryton call is made; the gate +
posting paths are covered at the service level with fakes.

Each test gets a FRESH installation id, so settlement rows never
leak between tests; the fixture deletes the installation's
settlement + registry rows, the provisioned tenants, and the engine
connection in teardown.
"""

from __future__ import annotations

import base64
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

CSV_HEADERS = "شماره تراکنش,تاریخ تراکنش,مبلغ ناخالص,کارمزد,مالیات,مبلغ خالص,نوع تراکنش"
CSV_COLUMNS = {
    "transaction_id": "شماره تراکنش",
    "date": "تاریخ تراکنش",
    "gross": "مبلغ ناخالص",
    "fee": "کارمزد",
    "vat": "مالیات",
    "net": "مبلغ خالص",
    "tran_type": "نوع تراکنش",
}

#: One tranche of a 100,000,000 gross payment (fee 10%, VAT 10%).
TRANCHE_NET = 22_250_000


def _csv(*rows: str) -> str:
    return "\n".join([CSV_HEADERS, *rows]) + "\n"


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _detail_row(txn: str = "TXN-1", *, net: int = TRANCHE_NET) -> str:
    gross = net + 2_500_000 + 250_000
    return f"{txn},1405/08/15,{gross},2500000,250000,{net},فروش"


@dataclass
class World:
    """Provisioned tenant, company, and keys for one test."""

    installation_id: str
    tenant_id: str
    company_id: str
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
            id=str(uuid.uuid4()), slug=f"rest-523-{uuid.uuid4().hex[:8]}", name="Tenant 1"
        )
        other_tenant = TenantModel(
            id=str(uuid.uuid4()), slug=f"rest-523-{uuid.uuid4().hex[:8]}", name="Tenant 2"
        )
        session.add_all([tenant, other_tenant])
        session.flush()
        client = ApiClientModel(id=str(uuid.uuid4()), tenant_id=tenant.id, name="rest-523 client")
        other_client = ApiClientModel(
            id=str(uuid.uuid4()), tenant_id=other_tenant.id, name="rest-523 other client"
        )
        session.add_all([client, other_client])
        session.flush()

        connection = EngineConnectionModel(
            id=str(uuid.uuid4()),
            name=f"conn-523-{uuid.uuid4().hex[:8]}",
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
                name="rest-523 key",
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
            company_id=company.id,
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


def _setup_channel(api: TestClient, world: World) -> None:
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
            "valid_from": "2026-10-23",
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


def _record_payment(api: TestClient, world: World, txn: str = "TXN-1") -> dict:
    response = api.post(
        f"{BASE}/payments",
        json={
            "channel_key": "snapppay",
            "gateway_transaction_id": txn,
            "gross_irr": 100_000_000,
            "gateway_confirmed_at": "2026-11-06T12:00:00Z",
            "source": "store_webhook",
            "store_order_id": "ORD-1",
        },
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _import_body(csv_text: str, **overrides: object) -> dict:
    body: dict[str, object] = {
        "filename": "snapppay-1405-08.csv",
        "content_base64": _b64(csv_text),
        "channel_key": "snapppay",
        "file_type": "detail",
        "period_jalali": "1405/08",
        "currency_unit": "rial",
        "columns": CSV_COLUMNS,
        "date_kind": "jalali",
    }
    body.update(overrides)
    return body


def _upload(api: TestClient, world: World, csv_text: str, **overrides: object) -> dict:
    response = api.post(
        f"{BASE}/imports",
        json=_import_body(csv_text, **overrides),
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _approved_import(api: TestClient, world: World, csv_text: str) -> dict:
    uploaded = _upload(api, world, csv_text)
    import_id = uploaded["file_import"]["id"]
    response = api.post(
        f"{BASE}/imports/{import_id}/approve",
        json={},
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 200, response.text
    return response.json()


# -- auth -------------------------------------------------------------------


def test_missing_auth_is_401(api: TestClient, world: World) -> None:
    response = api.get(f"{BASE}/imports")
    assert response.status_code == 401


def test_wrong_scope_is_403(api: TestClient, world: World) -> None:
    response = api.get(f"{BASE}/imports", headers=_auth(world.key_other_secret))
    assert response.status_code == 403


def test_upload_without_manage_scope_is_403(api: TestClient, world: World) -> None:
    response = api.post(
        f"{BASE}/imports",
        json=_import_body(_csv(_detail_row())),
        headers=_auth(world.key_read_secret),
    )
    assert response.status_code == 403


# -- imports ------------------------------------------------------------------


def test_upload_detail_file_returns_preview(api: TestClient, world: World) -> None:
    _setup_channel(api, world)
    _record_payment(api, world)
    body = _upload(api, world, _csv(_detail_row()))
    assert body["is_duplicate"] is False
    assert body["file_import"]["status"] == "awaiting_review"
    assert body["file_import"]["file_type"] == "detail"
    assert body["file_import"]["period_jalali"] == "1405/08"
    assert body["preview"]["matched_rows"] == 1
    assert body["preview"]["net_irr"] == TRANCHE_NET
    assert body["preview"]["expected_deposit_irr"] == TRANCHE_NET
    assert body["review_item_id"] is not None
    assert len(body["rows"]) == 1
    assert body["rows"][0]["match_status"] == "matched"
    assert body["rows"][0]["gateway_transaction_id"] == "TXN-1"


def test_reimport_same_file_is_duplicate(api: TestClient, world: World) -> None:
    _setup_channel(api, world)
    _record_payment(api, world)
    first = _upload(api, world, _csv(_detail_row()))
    second = _upload(api, world, _csv(_detail_row()))
    assert second["is_duplicate"] is True
    assert second["file_import"]["id"] == first["file_import"]["id"]
    assert second["review_item_id"] is None


def test_upload_bad_base64_is_422(api: TestClient, world: World) -> None:
    _setup_channel(api, world)
    response = api.post(
        f"{BASE}/imports",
        json=_import_body("", content_base64="!!!not-base64!!!"),
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 422


def test_upload_unknown_file_type_is_422(api: TestClient, world: World) -> None:
    _setup_channel(api, world)
    response = api.post(
        f"{BASE}/imports",
        json=_import_body(_csv(_detail_row()), file_type="weird"),
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "SETTLEMENT_IMPORT_INVALID"


def test_upload_unknown_channel_is_404(api: TestClient, world: World) -> None:
    _setup_channel(api, world)
    response = api.post(
        f"{BASE}/imports",
        json=_import_body(_csv(_detail_row()), channel_key="nosuch"),
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 404


def test_list_and_get_import(api: TestClient, world: World) -> None:
    _setup_channel(api, world)
    _record_payment(api, world)
    uploaded = _upload(api, world, _csv(_detail_row()))
    import_id = uploaded["file_import"]["id"]
    response = api.get(f"{BASE}/imports", headers=_auth(world.key_read_secret))
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
    response = api.get(f"{BASE}/imports/{import_id}", headers=_auth(world.key_read_secret))
    assert response.status_code == 200
    assert response.json()["file_import"]["id"] == import_id
    assert len(response.json()["rows"]) == 1


def test_get_unknown_import_is_404(api: TestClient, world: World) -> None:
    response = api.get(
        f"{BASE}/imports/{uuid.uuid4()}", headers=_auth(world.key_read_secret)
    )
    assert response.status_code == 404
    assert response.json()["code"] == "SETTLEMENT_FILE_IMPORT_NOT_FOUND"


def test_other_tenant_cannot_see_import(api: TestClient, world: World) -> None:
    _setup_channel(api, world)
    _record_payment(api, world)
    uploaded = _upload(api, world, _csv(_detail_row()))
    import_id = uploaded["file_import"]["id"]
    response = api.get(
        f"{BASE}/imports/{import_id}", headers=_auth(world.key_other_tenant_secret)
    )
    assert response.status_code == 404


# -- approve / reject ---------------------------------------------------------


def test_approve_confirms_tranche_and_creates_event(api: TestClient, world: World) -> None:
    _setup_channel(api, world)
    _record_payment(api, world)
    approved = _approved_import(api, world, _csv(_detail_row()))
    assert approved["settlement_event_id"]
    assert approved["canonical_event_id"]
    assert len(approved["confirmed_tranche_ids"]) == 1
    assert approved["net_irr"] == TRANCHE_NET
    # The tranche is confirmed; the event is listable.
    tranche_id = approved["confirmed_tranche_ids"][0]
    response = api.get(
        f"{BASE}/tranches/{tranche_id}", headers=_auth(world.key_read_secret)
    )
    assert response.status_code == 200
    assert response.json()["state"] == "confirmed"
    response = api.get(f"{BASE}/events", headers=_auth(world.key_read_secret))
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
    assert response.json()["items"][0]["net_irr"] == TRANCHE_NET
    # The match link ties tranche ↔ event.
    response = api.get(
        f"{BASE}/tranches/{tranche_id}/matches", headers=_auth(world.key_read_secret)
    )
    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["settlement_event_id"] == approved["settlement_event_id"]


def test_approve_twice_is_409(api: TestClient, world: World) -> None:
    _setup_channel(api, world)
    _record_payment(api, world)
    uploaded = _upload(api, world, _csv(_detail_row()))
    import_id = uploaded["file_import"]["id"]
    response = api.post(
        f"{BASE}/imports/{import_id}/approve",
        json={},
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 200
    response = api.post(
        f"{BASE}/imports/{import_id}/approve",
        json={},
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "SETTLEMENT_IMPORT_STATE"


def test_reject_import(api: TestClient, world: World) -> None:
    _setup_channel(api, world)
    _record_payment(api, world)
    uploaded = _upload(api, world, _csv(_detail_row()))
    import_id = uploaded["file_import"]["id"]
    response = api.post(
        f"{BASE}/imports/{import_id}/reject",
        json={"reason": "wrong month"},
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    response = api.get(f"{BASE}/events", headers=_auth(world.key_read_secret))
    assert response.json()["items"] == []


# -- deposits + tie-out ---------------------------------------------------------


def test_record_deposit_auto_matches(api: TestClient, world: World) -> None:
    _setup_channel(api, world)
    _record_payment(api, world)
    approved = _approved_import(api, world, _csv(_detail_row()))
    response = api.post(
        f"{BASE}/deposits",
        json={
            "amount_irr": TRANCHE_NET,
            "deposit_date": "2026-11-07",
            "source": "manual",
            "channel_key": "snapppay",
        },
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["decision_method"] == "amount_and_date"
    assert body["decision_confidence"] == "0.95"
    assert body["matched_tranche_ids"] == approved["confirmed_tranche_ids"]
    tranche_id = approved["confirmed_tranche_ids"][0]
    response = api.get(
        f"{BASE}/tranches/{tranche_id}", headers=_auth(world.key_read_secret)
    )
    assert response.json()["state"] == "received"


def test_record_deposit_no_candidate(api: TestClient, world: World) -> None:
    _setup_channel(api, world)
    _record_payment(api, world)
    _approved_import(api, world, _csv(_detail_row()))
    response = api.post(
        f"{BASE}/deposits",
        json={
            "amount_irr": 123_456_789,
            "deposit_date": "2026-11-07",
            "source": "manual",
            "channel_key": "snapppay",
        },
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["decision_method"] == "none"
    assert body["matched_tranche_ids"] == []
    assert len(body["exception_ids"]) == 1
    response = api.get(
        f"{BASE}/deposits?unmatched_only=true", headers=_auth(world.key_read_secret)
    )
    assert len(response.json()["items"]) == 1


def test_record_deposit_unknown_company_is_404(api: TestClient, world: World) -> None:
    _setup_channel(api, world)
    response = api.post(
        f"{BASE}/deposits",
        json={
            "amount_irr": TRANCHE_NET,
            "deposit_date": "2026-11-07",
            "source": "manual",
            "company_id": str(uuid.uuid4()),
        },
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 404


def test_manual_match(api: TestClient, world: World) -> None:
    _setup_channel(api, world)
    _record_payment(api, world)
    approved = _approved_import(api, world, _csv(_detail_row()))
    response = api.post(
        f"{BASE}/deposits",
        json={
            "amount_irr": 123_456_789,
            "deposit_date": "2026-11-07",
            "source": "manual",
            "channel_key": "snapppay",
        },
        headers=_auth(world.key_manage_secret),
    )
    deposit_id = response.json()["deposit"]["id"]
    tranche_id = approved["confirmed_tranche_ids"][0]
    response = api.post(
        f"{BASE}/deposits/{deposit_id}/match",
        json={"tranche_id": tranche_id, "allocated_amount_irr": TRANCHE_NET},
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 200, response.text
    assert response.json()["decision_method"] == "manual"
    assert response.json()["matched_tranche_ids"] == [tranche_id]


def test_tieout_without_company_holds_but_never_posts(
    api: TestClient, world: World
) -> None:
    _setup_channel(api, world)
    _record_payment(api, world)
    uploaded = _upload(api, world, _csv(_detail_row()))
    import_id = uploaded["file_import"]["id"]
    response = api.post(
        f"{BASE}/imports/{import_id}/approve",
        json={},
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 200
    response = api.post(
        f"{BASE}/deposits",
        json={
            "amount_irr": TRANCHE_NET,
            "deposit_date": "2026-11-07",
            "source": "manual",
            "channel_key": "snapppay",
        },
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 201, response.text
    response = api.post(
        f"{BASE}/imports/{import_id}/tie-out",
        json={},
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tieout"]["holds"] is True
    assert body["tieout"]["difference_irr"] == 0
    assert body["status"] == "approved"
    assert body["posted"] is False
    assert body["gate_allowed"] is False


def test_tieout_without_deposit_fails(api: TestClient, world: World) -> None:
    _setup_channel(api, world)
    _record_payment(api, world)
    uploaded = _upload(api, world, _csv(_detail_row()))
    import_id = uploaded["file_import"]["id"]
    response = api.post(
        f"{BASE}/imports/{import_id}/approve",
        json={},
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 200
    response = api.post(
        f"{BASE}/imports/{import_id}/tie-out",
        json={},
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tieout"]["holds"] is False
    assert body["tieout"]["difference_irr"] == TRANCHE_NET
    assert body["status"] == "exception"
    assert body["posted"] is False
    response = api.get(
        f"{BASE}/imports/{import_id}", headers=_auth(world.key_read_secret)
    )
    assert response.json()["file_import"]["status"] == "exception"
