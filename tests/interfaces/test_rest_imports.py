"""REST tests for the unified manual-import batch endpoints (US-554).

Everything is real — the FastAPI app, scoped API-key auth, the
PostgreSQL database, the real file readers (real CSVs are uploaded)
— except the engine, which is the in-memory fake from the bank
import REST tests (statement approve writes a draft statement to
it). Settlement approve runs WITHOUT auto-post (draft by default),
so no Tryton call is made on the settlement path; the tie-out +
auto-post paths are covered at the service level with fakes.

Each test gets a FRESH installation id, so settlement rows never
leak between tests; the fixture deletes the installation's
settlement + registry rows, the tenant's batch/bank/review rows,
the provisioned tenants, and the engine connection in teardown.
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
    BankColumnMappingModel,
    BankDepositModel,
    BankImportBatchModel,
    BankImportRowModel,
    Base,
    EngineCompanyModel,
    EngineConnectionModel,
    GatewayContractModel,
    GatewayPaymentModel,
    IdempotencyRecordModel,
    ManualImportBatchModel,
    PaymentChannelModel,
    ReviewItemModel,
    SettlementColumnMappingModel,
    SettlementEventModel,
    SettlementExceptionModel,
    SettlementFeeRecordModel,
    SettlementFileImportModel,
    SettlementMatchModel,
    SettlementTrancheModel,
    TenantModel,
)
from hesabyar.interfaces.rest.app import create_app
from hesabyar.interfaces.rest.dependencies import get_engine_adapter
from tests.interfaces.test_rest_bank_import import (
    COLUMNS as BANK_COLUMNS,
)
from tests.interfaces.test_rest_bank_import import (
    CSV_B64,
    JOURNAL_ID,
    SUSPENSE_ID,
    FakeBankEngine,
)

TEST_DB_URL = os.getenv(
    "HESABYAR_DATABASE_URL",
    "postgresql+psycopg://hesabyar:hesabyar_pass@hesabyar-postgres:5432/hesabyar_test",
)

BASE = "/api/v1/imports"
INTEGRATIONS = "/api/v1/integrations"
SETTLEMENTS = "/api/v1/settlements"

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

#: Batch/bank/review tables keyed by tenant id, for teardown.
#: (import rows go before their batches.)
_TENANT_TABLES = (
    BankImportRowModel,
    BankImportBatchModel,
    BankColumnMappingModel,
    ManualImportBatchModel,
    SettlementColumnMappingModel,
    ReviewItemModel,
)

SETTLEMENT_CSV_HEADERS = (
    "شماره تراکنش,تاریخ تراکنش,مبلغ ناخالص,کارمزد,مالیات,مبلغ خالص,نوع تراکنش"
)
SETTLEMENT_COLUMNS = {
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


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _settlement_csv(txn: str = "TXN-1", *, net: int = TRANCHE_NET) -> str:
    gross = net + 2_500_000 + 250_000
    return (
        f"{SETTLEMENT_CSV_HEADERS}\n"
        f"{txn},1405/08/15,{gross},2500000,250000,{net},فروش\n"
    )


@dataclass
class World:
    """Provisioned tenant, company, and keys for one test."""

    installation_id: str
    tenant_id: str
    company_id: str
    bank_read_key: str
    bank_key: str
    settlement_read_key: str
    settlement_key: str
    both_key: str
    other_key: str
    other_tenant_key: str


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
def fake_engine() -> FakeBankEngine:
    return FakeBankEngine()


@pytest.fixture()
def world(engine: Engine, settings: AppSettings) -> Generator[World, None, None]:
    """Provision a tenant, one company row, and seven keys; clean up after."""
    session = sessionmaker(bind=engine)()
    service = ApiKeyService(ApiKeyCrypto(settings.auth.api_key_pepper))
    installation_id = str(uuid.uuid4())
    tenant_id: str | None = None
    other_tenant_id: str | None = None
    connection_id: str | None = None
    try:
        tenant = TenantModel(
            id=str(uuid.uuid4()), slug=f"rest-554-{uuid.uuid4().hex[:8]}", name="Tenant 1"
        )
        other_tenant = TenantModel(
            id=str(uuid.uuid4()), slug=f"rest-554-{uuid.uuid4().hex[:8]}", name="Tenant 2"
        )
        session.add_all([tenant, other_tenant])
        session.flush()
        client = ApiClientModel(id=str(uuid.uuid4()), tenant_id=tenant.id, name="rest-554 client")
        other_client = ApiClientModel(
            id=str(uuid.uuid4()), tenant_id=other_tenant.id, name="rest-554 other client"
        )
        session.add_all([client, other_client])
        session.flush()

        connection = EngineConnectionModel(
            id=str(uuid.uuid4()),
            name=f"conn-554-{uuid.uuid4().hex[:8]}",
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
                name="rest-554 key",
                scopes=scopes,
                installation_id=key_installation_id,
            )

        key_bank_read = _key(client, ["banking:read"])
        key_bank = _key(client, ["banking:read", "banking:import"])
        key_settlement_read = _key(client, ["settlement:read"])
        key_settlement = _key(
            client,
            [
                "settlement:read",
                "settlement:manage",
                "integrations:read",
                "integrations:manage",
            ],
        )
        key_both = _key(
            client,
            [
                "banking:read",
                "banking:import",
                "settlement:read",
                "settlement:manage",
                "integrations:read",
                "integrations:manage",
            ],
        )
        key_other = _key(client, ["company:read"])
        key_other_tenant = _key(
            other_client,
            ["settlement:read", "banking:read"],
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
            bank_read_key=key_bank_read.secret,
            bank_key=key_bank.secret,
            settlement_read_key=key_settlement_read.secret,
            settlement_key=key_settlement.secret,
            both_key=key_both.secret,
            other_key=key_other.secret,
            other_tenant_key=key_other_tenant.secret,
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
            cleanup.execute(
                delete(IdempotencyRecordModel).where(
                    IdempotencyRecordModel.installation_id == installation_id
                )
            )
            if tenant_id is not None:
                for model in _TENANT_TABLES:
                    cleanup.execute(
                        delete(model).where(model.tenant_id == tenant_id)  # type: ignore[attr-defined]
                    )
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
def api(
    settings: AppSettings, fake_engine: FakeBankEngine
) -> Generator[TestClient, None, None]:
    """The real app with the engine faked (auth + DB are real)."""
    app = create_app(settings=settings)
    app.dependency_overrides[get_engine_adapter] = lambda: fake_engine
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def _auth(secret: str, key: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {secret}"}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _new_key() -> str:
    return str(uuid.uuid4())


def _setup_channel(api: TestClient, world: World) -> None:
    """Create the snapppay channel + active 4-tranche contract via REST."""
    response = api.post(
        f"{INTEGRATIONS}/channels",
        json={
            "channel_key": "snapppay",
            "display_name_fa": "اسنپ‌پی",
            "channel_type": "bnpl",
        },
        headers=_auth(world.settlement_key, _new_key()),
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
        headers=_auth(world.settlement_key, _new_key()),
    )
    assert response.status_code == 201, response.text


def _record_payment(api: TestClient, world: World, txn: str = "TXN-1") -> dict:
    response = api.post(
        f"{SETTLEMENTS}/payments",
        json={
            "channel_key": "snapppay",
            "gateway_transaction_id": txn,
            "gross_irr": 100_000_000,
            "gateway_confirmed_at": "2026-11-06T12:00:00Z",
            "source": "store_webhook",
            "store_order_id": "ORD-1",
        },
        headers=_auth(world.settlement_key, _new_key()),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _statement_body(world: World, **overrides: object) -> dict:
    body: dict[str, object] = {
        "kind": "statement",
        "source_key": "melli",
        "company": world.company_id,
        "filename": "baam.csv",
        "content_base64": CSV_B64,
        "currency_unit": "rial",
        "date_kind": "jalali",
        "header_row": 0,
        "columns": BANK_COLUMNS,
        "statement_journal_id": JOURNAL_ID,
        "default_account_id": SUSPENSE_ID,
    }
    body.update(overrides)
    return body


def _settlement_body(world: World, **overrides: object) -> dict:
    body: dict[str, object] = {
        "kind": "settlement",
        "source_key": "snapppay",
        "company": world.company_id,
        "filename": "snapppay-1405-08.csv",
        "content_base64": _b64(_settlement_csv()),
        "currency_unit": "rial",
        "date_kind": "jalali",
        "header_row": 0,
        "columns": SETTLEMENT_COLUMNS,
        "file_type": "detail",
        "period_jalali": "1405/08",
    }
    body.update(overrides)
    return body


def _upload_statement(api: TestClient, world: World, **overrides: object) -> dict:
    response = api.post(
        f"{BASE}/batches",
        json=_statement_body(world, **overrides),
        headers=_auth(world.bank_key, _new_key()),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _upload_settlement(api: TestClient, world: World, **overrides: object) -> dict:
    response = api.post(
        f"{BASE}/batches",
        json=_settlement_body(world, **overrides),
        headers=_auth(world.settlement_key, _new_key()),
    )
    assert response.status_code == 201, response.text
    return response.json()


# -- detection -------------------------------------------------------------------


def test_detect_statement_suggests_mapping(api: TestClient, world: World) -> None:
    response = api.post(
        f"{BASE}/batches/detect",
        json={
            "kind": "statement",
            "filename": "baam.csv",
            "content_base64": CSV_B64,
        },
        headers=_auth(world.bank_key),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["file_format"] == "csv"
    assert body["suggested_mapping"]["date"] == "تاریخ"
    assert body["missing_fields"] == []


def test_detect_settlement_suggests_mapping(api: TestClient, world: World) -> None:
    response = api.post(
        f"{BASE}/batches/detect",
        json={
            "kind": "settlement",
            "filename": "snapppay-1405-08.csv",
            "content_base64": _b64(_settlement_csv()),
        },
        headers=_auth(world.settlement_key),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["file_format"] == "csv"
    assert body["suggested_mapping"]["transaction_id"] == "شماره تراکنش"
    assert body["suggested_mapping"]["net"] == "مبلغ خالص"
    assert body["missing_fields"] == []


def test_detect_wrong_kind_scope_is_forbidden(api: TestClient, world: World) -> None:
    # A banking-only key cannot detect a settlement file.
    response = api.post(
        f"{BASE}/batches/detect",
        json={
            "kind": "settlement",
            "filename": "snapppay-1405-08.csv",
            "content_base64": _b64(_settlement_csv()),
        },
        headers=_auth(world.bank_key),
    )
    assert response.status_code == 403, response.text


def test_detect_unknown_kind_is_422(api: TestClient, world: World) -> None:
    response = api.post(
        f"{BASE}/batches/detect",
        json={
            "kind": "invoice",
            "filename": "x.csv",
            "content_base64": CSV_B64,
        },
        headers=_auth(world.bank_key),
    )
    assert response.status_code == 422, response.text


# -- upload ----------------------------------------------------------------------


def test_upload_statement_batch(api: TestClient, world: World) -> None:
    result = _upload_statement(api, world)
    assert result["duplicate"] is False
    batch = result["batch"]
    assert batch["kind"] == "statement"
    assert batch["status"] == "awaiting_review"
    assert batch["attempt"] == 1
    assert batch["source_import_id"] is not None
    assert batch["review_item_id"] is not None
    assert [stage["stage"] for stage in batch["stages"]] == [
        "uploaded",
        "mapped",
        "validated",
        "previewed",
        "awaiting_review",
    ]
    preview = result["preview"]
    assert preview["gross_irr"] == 1_250_000
    assert preview["net_irr"] == 750_000
    assert preview["expected_deposit_irr"] == 1_000_000
    assert preview["fee_irr"] == 0
    assert preview["receivable_before_irr"] is None
    assert preview["new_rows"] == 2


def test_upload_statement_duplicate_is_noop(api: TestClient, world: World) -> None:
    first = _upload_statement(api, world)
    # A NEW idempotency key: the second call really re-runs and hits
    # the file-hash dedupe (the same key would replay the response).
    second = _upload_statement(api, world)
    assert second["duplicate"] is True
    assert second["batch"]["id"] == first["batch"]["id"]


def test_upload_requires_idempotency_key(api: TestClient, world: World) -> None:
    response = api.post(
        f"{BASE}/batches",
        json=_statement_body(world),
        headers=_auth(world.bank_key),  # no Idempotency-Key
    )
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_upload_statement_missing_journal_is_422(api: TestClient, world: World) -> None:
    body = _statement_body(world)
    del body["statement_journal_id"]
    response = api.post(
        f"{BASE}/batches",
        json=body,
        headers=_auth(world.bank_key, _new_key()),
    )
    assert response.status_code == 422, response.text


def test_upload_settlement_batch(api: TestClient, world: World) -> None:
    _setup_channel(api, world)
    _record_payment(api, world)
    result = _upload_settlement(api, world)
    assert result["duplicate"] is False
    batch = result["batch"]
    assert batch["kind"] == "settlement"
    assert batch["status"] == "awaiting_review"
    assert batch["source_import_batch_id"] is not None
    preview = result["preview"]
    assert preview["gross_irr"] == 25_000_000
    assert preview["fee_irr"] == 2_500_000
    assert preview["tax_irr"] == 250_000
    assert preview["net_irr"] == TRANCHE_NET
    assert preview["expected_deposit_irr"] == TRANCHE_NET
    assert preview["receivable_before_irr"] == 4 * TRANCHE_NET
    assert preview["receivable_after_irr"] == 3 * TRANCHE_NET
    assert preview["matched_rows"] == 1


def test_upload_settlement_wrong_kind_scope_is_forbidden(
    api: TestClient, world: World
) -> None:
    response = api.post(
        f"{BASE}/batches",
        json=_settlement_body(world),
        headers=_auth(world.bank_key, _new_key()),
    )
    assert response.status_code == 403, response.text


def test_upload_settlement_missing_period_is_422(api: TestClient, world: World) -> None:
    body = _settlement_body(world)
    del body["period_jalali"]
    response = api.post(
        f"{BASE}/batches",
        json=body,
        headers=_auth(world.settlement_key, _new_key()),
    )
    assert response.status_code == 422, response.text


def test_upload_settlement_with_save_mapping(api: TestClient, world: World) -> None:
    _setup_channel(api, world)
    _record_payment(api, world)
    result = _upload_settlement(api, world, save_mapping=True, mapping_name="SnappPay")
    assert result["batch"]["column_mapping_id"] is not None
    response = api.get(
        f"{BASE}/settlement-column-mappings/{result['batch']['column_mapping_id']}",
        headers=_auth(world.settlement_read_key),
    )
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "SnappPay"
    assert response.json()["channel_key"] == "snapppay"


# -- reads -----------------------------------------------------------------------


def test_get_batch_and_scope_guards(api: TestClient, world: World) -> None:
    result = _upload_statement(api, world)
    batch_id = result["batch"]["id"]
    response = api.get(
        f"{BASE}/batches/{batch_id}",
        headers=_auth(world.bank_read_key),
    )
    assert response.status_code == 200, response.text
    assert response.json()["preview"]["net_irr"] == 750_000
    # A settlement-only key cannot read a statement batch.
    response = api.get(
        f"{BASE}/batches/{batch_id}",
        headers=_auth(world.settlement_read_key),
    )
    assert response.status_code == 403, response.text
    # Another tenant cannot see it at all.
    response = api.get(
        f"{BASE}/batches/{batch_id}",
        headers=_auth(world.other_tenant_key),
    )
    assert response.status_code == 404, response.text


def test_get_missing_batch_is_404(api: TestClient, world: World) -> None:
    response = api.get(
        f"{BASE}/batches/{uuid.uuid4()}",
        headers=_auth(world.bank_read_key),
    )
    assert response.status_code == 404, response.text
    assert response.json()["code"] == "MANUAL_IMPORT_NOT_FOUND"


def test_list_batches_filters_by_kind_and_scope(
    api: TestClient, world: World
) -> None:
    _setup_channel(api, world)
    _record_payment(api, world)
    statement = _upload_statement(api, world)
    settlement = _upload_settlement(api, world)
    # A key with both scopes sees both.
    response = api.get(
        f"{BASE}/batches", headers=_auth(world.both_key)
    )
    assert response.status_code == 200, response.text
    assert {item["id"] for item in response.json()} == {
        statement["batch"]["id"],
        settlement["batch"]["id"],
    }
    # A banking-only key sees only the statement batch.
    response = api.get(
        f"{BASE}/batches", headers=_auth(world.bank_read_key)
    )
    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()] == [statement["batch"]["id"]]
    # A kind filter the key cannot read is a 403.
    response = api.get(
        f"{BASE}/batches?kind=settlement",
        headers=_auth(world.bank_read_key),
    )
    assert response.status_code == 403, response.text
    # Status filter.
    response = api.get(
        f"{BASE}/batches?status=posted", headers=_auth(world.both_key)
    )
    assert response.status_code == 200, response.text
    assert response.json() == []
    # Source-import filter.
    response = api.get(
        f"{BASE}/batches?source_import_id={statement['batch']['source_import_id']}",
        headers=_auth(world.both_key),
    )
    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()] == [statement["batch"]["id"]]
    # A key with neither read scope is a 403.
    response = api.get(f"{BASE}/batches", headers=_auth(world.other_key))
    assert response.status_code == 403, response.text


def test_preview_endpoint(api: TestClient, world: World) -> None:
    result = _upload_statement(api, world)
    response = api.get(
        f"{BASE}/batches/{result['batch']['id']}/preview",
        headers=_auth(world.bank_read_key),
    )
    assert response.status_code == 200, response.text
    assert response.json()["net_irr"] == 750_000
    assert response.json()["expected_deposit_irr"] == 1_000_000


def test_variance_endpoint_awaiting_review(api: TestClient, world: World) -> None:
    result = _upload_statement(api, world)
    response = api.get(
        f"{BASE}/batches/{result['batch']['id']}/variance",
        headers=_auth(world.bank_read_key),
    )
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["expected_net_irr"] == 750_000
    assert report["imported_net_irr"] == 750_000
    assert report["posted_net_irr"] == 0
    assert report["difference_irr"] == 750_000
    assert report["tieout_holds"] is None


# -- approve / reject / re-run -----------------------------------------------------


def test_approve_statement_posts_draft_statement(
    api: TestClient, world: World
) -> None:
    result = _upload_statement(api, world)
    response = api.post(
        f"{BASE}/batches/{result['batch']['id']}/approve",
        json={},
        headers=_auth(world.bank_key, _new_key()),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["posted"] is True
    assert body["batch"]["status"] == "posted"
    assert body["batch"]["posting_status"] == "posted"
    assert body["batch"]["engine_ref_id"] is not None
    assert body["tryton_journal_id"] == body["batch"]["engine_ref_id"]
    report = api.get(
        f"{BASE}/batches/{result['batch']['id']}/variance",
        headers=_auth(world.bank_read_key),
    )
    assert report.status_code == 200, report.text
    assert report.json()["posted_net_irr"] == 750_000
    assert report.json()["difference_irr"] == 0


def test_approve_settlement_without_opt_in_is_draft_hold(
    api: TestClient, world: World
) -> None:
    _setup_channel(api, world)
    _record_payment(api, world)
    result = _upload_settlement(api, world)
    response = api.post(
        f"{BASE}/batches/{result['batch']['id']}/approve",
        json={},
        headers=_auth(world.settlement_key, _new_key()),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["posted"] is False
    assert body["batch"]["status"] == "approved"
    assert body["batch"]["posting_status"] == "draft_hold"
    assert body["batch"]["auto_post_opt_in"] is False
    assert body["settlement_event_id"] is not None
    assert len(body["confirmed_tranche_ids"]) == 1


def test_approve_twice_is_conflict(api: TestClient, world: World) -> None:
    result = _upload_statement(api, world)
    first = api.post(
        f"{BASE}/batches/{result['batch']['id']}/approve",
        json={},
        headers=_auth(world.bank_key, _new_key()),
    )
    assert first.status_code == 200, first.text
    second = api.post(
        f"{BASE}/batches/{result['batch']['id']}/approve",
        json={},
        headers=_auth(world.bank_key, _new_key()),
    )
    assert second.status_code == 409, second.text
    assert second.json()["code"] == "MANUAL_IMPORT_INVALID_STATE"


def test_reject_archives_and_rerun_reopens(api: TestClient, world: World) -> None:
    _setup_channel(api, world)
    _record_payment(api, world)
    result = _upload_settlement(api, world)
    batch_id = result["batch"]["id"]
    response = api.post(
        f"{BASE}/batches/{batch_id}/reject",
        json={"reason": "Wrong period — re-export from the gateway."},
        headers=_auth(world.settlement_key, _new_key()),
    )
    assert response.status_code == 200, response.text
    rejected = response.json()
    assert rejected["status"] == "archived"
    assert rejected["rejection_reason"] == "Wrong period — re-export from the gateway."
    assert rejected["rejected_at"] is not None
    assert rejected["archived_at"] is not None
    response = api.post(
        f"{BASE}/batches/{batch_id}/re-run",
        headers=_auth(world.settlement_key, _new_key()),
    )
    assert response.status_code == 200, response.text
    rerun = response.json()
    assert rerun["status"] == "awaiting_review"
    assert rerun["attempt"] == 2
    assert rerun["rejection_reason"] is None
    assert rerun["preview"]["net_irr"] == TRANCHE_NET
    assert rerun["review_item_id"] is not None
    assert rerun["review_item_id"] != result["batch"]["review_item_id"]


def test_reject_requires_reason(api: TestClient, world: World) -> None:
    result = _upload_statement(api, world)
    response = api.post(
        f"{BASE}/batches/{result['batch']['id']}/reject",
        json={"reason": ""},
        headers=_auth(world.bank_key, _new_key()),
    )
    assert response.status_code == 422, response.text


def test_rerun_non_archived_batch_is_conflict(api: TestClient, world: World) -> None:
    result = _upload_statement(api, world)
    response = api.post(
        f"{BASE}/batches/{result['batch']['id']}/re-run",
        headers=_auth(world.bank_key, _new_key()),
    )
    assert response.status_code == 409, response.text


def test_retry_posting_guards(api: TestClient, world: World) -> None:
    # A statement batch has no posting step to retry.
    statement = _upload_statement(api, world)
    response = api.post(
        f"{BASE}/batches/{statement['batch']['id']}/retry-posting",
        headers=_auth(world.bank_key, _new_key()),
    )
    assert response.status_code == 409, response.text
    # An unapproved settlement batch cannot retry posting either.
    _setup_channel(api, world)
    _record_payment(api, world)
    settlement = _upload_settlement(api, world)
    response = api.post(
        f"{BASE}/batches/{settlement['batch']['id']}/retry-posting",
        headers=_auth(world.settlement_key, _new_key()),
    )
    assert response.status_code == 409, response.text


# -- settlement column mappings ------------------------------------------------------


def _mapping_body(**overrides: object) -> dict:
    body: dict[str, object] = {
        "channel_key": "snapppay",
        "name": "SnappPay detail",
        "file_format": "csv",
        "file_type": "detail",
        "columns": SETTLEMENT_COLUMNS,
        "date_kind": "jalali",
        "currency_unit": "rial",
        "header_row": 0,
    }
    body.update(overrides)
    return body


def test_settlement_mapping_crud(api: TestClient, world: World) -> None:
    created = api.post(
        f"{BASE}/settlement-column-mappings",
        json=_mapping_body(),
        headers=_auth(world.settlement_key, _new_key()),
    )
    assert created.status_code == 201, created.text
    mapping = created.json()
    assert mapping["status"] == "active"
    assert mapping["channel_key"] == "snapppay"
    assert mapping["columns"] == SETTLEMENT_COLUMNS
    listed = api.get(
        f"{BASE}/settlement-column-mappings?channel_key=snapppay",
        headers=_auth(world.settlement_read_key),
    )
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()] == [mapping["id"]]
    superseded = api.post(
        f"{BASE}/settlement-column-mappings/{mapping['id']}/supersede",
        headers=_auth(world.settlement_key, _new_key()),
    )
    assert superseded.status_code == 200, superseded.text
    assert superseded.json()["status"] == "superseded"
    fetched = api.get(
        f"{BASE}/settlement-column-mappings/{mapping['id']}",
        headers=_auth(world.settlement_read_key),
    )
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["status"] == "superseded"


def test_settlement_mapping_invalid_columns_is_422(
    api: TestClient, world: World
) -> None:
    response = api.post(
        f"{BASE}/settlement-column-mappings",
        json=_mapping_body(
            columns={"transaction_id": "شماره تراکنش", "date": "تاریخ تراکنش"}
        ),
        headers=_auth(world.settlement_key, _new_key()),
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "SETTLEMENT_MAPPING_INVALID"


def test_settlement_mapping_missing_is_404(api: TestClient, world: World) -> None:
    response = api.get(
        f"{BASE}/settlement-column-mappings/{uuid.uuid4()}",
        headers=_auth(world.settlement_read_key),
    )
    assert response.status_code == 404, response.text
    assert response.json()["code"] == "SETTLEMENT_MAPPING_NOT_FOUND"


def test_settlement_mapping_wrong_scope_is_forbidden(
    api: TestClient, world: World
) -> None:
    response = api.post(
        f"{BASE}/settlement-column-mappings",
        json=_mapping_body(),
        headers=_auth(world.bank_key, _new_key()),
    )
    assert response.status_code == 403, response.text
