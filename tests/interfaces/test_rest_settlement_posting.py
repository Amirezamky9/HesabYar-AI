"""Tests for the settlement posting REST endpoints (US-522).

Everything is real — the FastAPI app, scoped API-key auth, the
PostgreSQL database — except the accounting engine, which is the
in-memory fake from the unit tests (the live Tryton behaviour is
covered by ``tests/contract/test_settlement_posting.py``). The
posting service is overridden per-test with a service built on the
real repository/registry/audit and the fake engine.

Each test gets a FRESH installation id, so settlement rows never
leak between tests; the fixture deletes the installation's
settlement + registry + mapping rows, the provisioned tenants, and
the engine connection in teardown.

Tranche state transitions, invoice-line links, and settlement
events/deposits/matches have no REST endpoints yet (US-523 owns the
matching flow) — the tests set them up with direct DB writes.
"""

from __future__ import annotations

import datetime
import os
import uuid
from collections.abc import Generator
from dataclasses import dataclass
from typing import Any, cast

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from hesabyar.application.commands.settlement_posting import SettlementPostingService
from hesabyar.domain.accounting.ports import AccountingEnginePort
from hesabyar.domain.settlement.models import SettlementTranche
from hesabyar.infrastructure.auth import ApiKeyCrypto, ApiKeyService
from hesabyar.infrastructure.auth.credential_vault import generate_encryption_key
from hesabyar.infrastructure.config import AppSettings, AuthSettings
from hesabyar.infrastructure.persistence.postgres.integrations_repository import (
    SqlAlchemyIntegrationsRepository,
)
from hesabyar.infrastructure.persistence.postgres.models import (
    ApiClientModel,
    BankDepositModel,
    Base,
    EngineCompanyModel,
    EngineConnectionModel,
    GatewayContractModel,
    GatewayPaymentModel,
    IntegrationMappingModel,
    PaymentChannelModel,
    SettlementEventModel,
    SettlementExceptionModel,
    SettlementFeeRecordModel,
    SettlementFileImportModel,
    SettlementMatchModel,
    SettlementPostingModel,
    SettlementTrancheModel,
    TenantModel,
)
from hesabyar.infrastructure.persistence.postgres.settlement_repository import (
    SqlAlchemySettlementAudit,
    SqlAlchemySettlementRepository,
)
from hesabyar.interfaces.rest.app import create_app
from hesabyar.interfaces.rest.dependencies import (
    get_integrations_repository,
    get_settlement_audit,
    get_settlement_posting_service,
    get_settlement_repository,
)
from tests.unit.test_settlement_posting import FakeEngine

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
_SETTLEMENT_TABLES = (
    SettlementPostingModel,
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
            id=str(uuid.uuid4()), slug=f"rest-522-{uuid.uuid4().hex[:8]}", name="Tenant 1"
        )
        other_tenant = TenantModel(
            id=str(uuid.uuid4()), slug=f"rest-522-{uuid.uuid4().hex[:8]}", name="Tenant 2"
        )
        session.add_all([tenant, other_tenant])
        session.flush()
        client = ApiClientModel(id=str(uuid.uuid4()), tenant_id=tenant.id, name="rest-522 client")
        other_client = ApiClientModel(
            id=str(uuid.uuid4()), tenant_id=other_tenant.id, name="rest-522 other client"
        )
        session.add_all([client, other_client])
        session.flush()

        connection = EngineConnectionModel(
            id=str(uuid.uuid4()),
            name=f"conn-522-{uuid.uuid4().hex[:8]}",
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
                name="rest-522 key",
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
            cleanup.execute(
                delete(IntegrationMappingModel).where(
                    IntegrationMappingModel.installation_id == installation_id
                )
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
def fake_engine() -> FakeEngine:
    return FakeEngine()


@pytest.fixture()
def api(settings: AppSettings, fake_engine: FakeEngine) -> TestClient:
    """The real app, with the posting service's engine faked."""
    app = create_app(settings=settings)

    def _posting_service(
        repository: SqlAlchemySettlementRepository = Depends(get_settlement_repository),
        registry: SqlAlchemyIntegrationsRepository = Depends(get_integrations_repository),
        audit: SqlAlchemySettlementAudit = Depends(get_settlement_audit),
    ) -> SettlementPostingService:
        return SettlementPostingService(
            repository, registry, cast(AccountingEnginePort, fake_engine), audit
        )

    app.dependency_overrides[get_settlement_posting_service] = _posting_service
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


def _seed_mappings(engine: Engine, installation_id: str) -> None:
    """Seed the mapping-center rows the wiring resolver needs."""
    session = sessionmaker(bind=engine)()
    try:
        rows = [
            ("external_party", "snapppay", "snapppay", "party", "10"),
            ("gateway_journal", "snapppay", "snapppay", "journal", "20"),
            ("gateway_journal", "snapppay", "receivable", "account", "1.2.8"),
            ("gateway_journal", "snapppay", "clearing", "account", "1.1.6"),
            ("gateway_journal", "snapppay", "bank", "account", "1.1.1"),
            ("gateway_journal", "snapppay", "fee_expense", "account", "5.2.4"),
            ("gateway_journal", "snapppay", "fee_vat", "account", "6.3.6"),
            ("gateway_journal", "snapppay", "revenue", "account", "4.1.1"),
        ]
        for kind, channel_key, source_key, target_type, target_ref in rows:
            session.add(
                IntegrationMappingModel(
                    id=str(uuid.uuid4()),
                    installation_id=installation_id,
                    mapping_kind=kind,
                    channel_key=channel_key,
                    source_key=source_key,
                    target_type=target_type,
                    target_ref=target_ref,
                    version=1,
                    status="active",
                    suggestion_mode="suggest",
                    source="manual",
                )
            )
        session.commit()
    finally:
        session.close()


def _record_payment(api: TestClient, world: World, **overrides: object) -> dict:
    body: dict[str, object] = {
        "channel_key": "snapppay",
        "gateway_transaction_id": "TXN-1",
        "gross_irr": 100_000_000,
        "gateway_confirmed_at": "2026-11-06T12:00:00Z",
        "source": "store_webhook",
        "store_order_id": "ORD-1",
    }
    body.update(overrides)
    response = api.post(
        f"{BASE}/payments",
        json=body,
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _tranche_domain(tranche_json: dict[str, Any], installation_id: str) -> SettlementTranche:
    """Build a domain tranche from a REST tranche payload (for the fake engine)."""
    return SettlementTranche(
        id=tranche_json["id"],
        installation_id=installation_id,
        gateway_payment_id=tranche_json["gateway_payment_id"],
        tranche_index=tranche_json["tranche_index"],
        tranche_count=tranche_json["tranche_count"],
        sales_period_jalali=tranche_json["sales_period_jalali"],
        expected_window_start=datetime.date.fromisoformat(
            tranche_json["expected_window_start"]
        ),
        expected_date=datetime.date.fromisoformat(tranche_json["expected_date"]),
        gross_irr=tranche_json["gross_irr"],
        fee_irr=tranche_json["fee_irr"],
        fee_vat_irr=tranche_json["fee_vat_irr"],
        net_irr=tranche_json["net_irr"],
    )


def _link_invoice_lines(
    engine: Engine,
    fake_engine: FakeEngine,
    installation_id: str,
    tranches_json: list[dict[str, Any]],
) -> None:
    """Seed the fake engine's invoice lines and link them to the tranches."""
    tranches = [
        _tranche_domain(t, installation_id) for t in tranches_json
    ]
    line_ids = fake_engine.seed_invoice_lines(tranches)
    session = sessionmaker(bind=engine)()
    try:
        for tranche in tranches:
            row = session.get(SettlementTrancheModel, tranche.id)
            assert row is not None
            row.tryton_receivable_line_id = line_ids[tranche.id]
        session.commit()
    finally:
        session.close()


def _set_tranche_state(
    engine: Engine,
    tranche_id: str,
    state: str,
    *,
    confirmed_net_irr: int | None = None,
    received_net_irr: int | None = None,
) -> None:
    session = sessionmaker(bind=engine)()
    try:
        row = session.get(SettlementTrancheModel, tranche_id)
        assert row is not None
        row.state = state
        if confirmed_net_irr is not None:
            row.confirmed_net_irr = confirmed_net_irr
        if received_net_irr is not None:
            row.received_net_irr = received_net_irr
        session.commit()
    finally:
        session.close()


def _setup_event_with_deposit(
    engine: Engine,
    installation_id: str,
    channel_id: str,
    tranche_id: str,
    *,
    deposit_irr: int,
    deductions_irr: int = 0,
) -> str:
    session = sessionmaker(bind=engine)()
    try:
        event = SettlementEventModel(
            id=str(uuid.uuid4()),
            installation_id=installation_id,
            payment_channel_id=channel_id,
            payout_date=datetime.date(2026, 11, 26),
            net_irr=deposit_irr,
            source="file",
            deductions_irr=deductions_irr,
        )
        deposit = BankDepositModel(
            id=str(uuid.uuid4()),
            installation_id=installation_id,
            deposit_date=datetime.date(2026, 11, 26),
            amount_irr=deposit_irr,
            source="statement_import",
        )
        session.add_all([event, deposit])
        session.flush()
        session.add(
            SettlementMatchModel(
                id=str(uuid.uuid4()),
                installation_id=installation_id,
                tranche_id=tranche_id,
                allocated_amount_irr=deposit_irr,
                method="exact",
                bank_deposit_id=deposit.id,
                settlement_event_id=event.id,
                is_automatic=True,
                matched_at=datetime.datetime.now(datetime.UTC),
            )
        )
        session.commit()
        return event.id
    finally:
        session.close()


def _post_sale_fee(api: TestClient, world: World, payment_id: str) -> dict:
    response = api.post(
        f"{BASE}/payments/{payment_id}/post-sale-fee",
        json={"company_id": 1, "date": "2026-11-19"},
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 200, response.text
    return response.json()


# -- post-sale-fee ------------------------------------------------------------


def test_post_sale_fee_posts_and_lists(
    api: TestClient, world: World, engine: Engine
) -> None:
    _setup_channel(api, world)
    _seed_mappings(engine, world.installation_id)
    recorded = _record_payment(api, world)
    payment_id = recorded["payment"]["id"]

    fee = _post_sale_fee(api, world, payment_id)
    assert fee["fee_irr"] == 10_000_000
    assert fee["fee_vat_irr"] == 1_000_000
    assert fee["posting"]["kind"] == "fee_at_sale"
    assert fee["posting"]["amount_irr"] == 11_000_000
    assert fee["tryton_move_id"] is not None

    response = api.get(
        f"{BASE}/payments/{payment_id}/postings",
        headers=_auth(world.key_read_secret),
    )
    assert response.status_code == 200, response.text
    postings = response.json()
    assert len(postings) == 1
    assert postings[0]["kind"] == "fee_at_sale"


def test_post_sale_fee_twice_is_409(
    api: TestClient, world: World, engine: Engine
) -> None:
    _setup_channel(api, world)
    _seed_mappings(engine, world.installation_id)
    recorded = _record_payment(api, world)
    payment_id = recorded["payment"]["id"]
    _post_sale_fee(api, world, payment_id)

    response = api.post(
        f"{BASE}/payments/{payment_id}/post-sale-fee",
        json={"company_id": 1, "date": "2026-11-19"},
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "SETTLEMENT_POSTING_EXISTS"


def test_post_sale_fee_unresolved_wiring_is_422(
    api: TestClient, world: World
) -> None:
    _setup_channel(api, world)  # no mappings seeded
    recorded = _record_payment(api, world)
    response = api.post(
        f"{BASE}/payments/{recorded['payment']['id']}/post-sale-fee",
        json={"company_id": 1, "date": "2026-11-19"},
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "SETTLEMENT_WIRING_UNRESOLVED"


def test_post_sale_fee_wrong_scope_is_403(
    api: TestClient, world: World, engine: Engine
) -> None:
    _setup_channel(api, world)
    _seed_mappings(engine, world.installation_id)
    recorded = _record_payment(api, world)
    response = api.post(
        f"{BASE}/payments/{recorded['payment']['id']}/post-sale-fee",
        json={"company_id": 1, "date": "2026-11-19"},
        headers=_auth(world.key_read_secret),
    )
    assert response.status_code == 403, response.text


def test_postings_unknown_payment_is_404(api: TestClient, world: World) -> None:
    response = api.get(
        f"{BASE}/payments/{uuid.uuid4()}/postings",
        headers=_auth(world.key_read_secret),
    )
    assert response.status_code == 404, response.text
    assert response.json()["code"] == "SETTLEMENT_PAYMENT_NOT_FOUND"


# -- clearing + bank receipt --------------------------------------------------


def test_post_clearing_full_flow(
    api: TestClient, world: World, engine: Engine, fake_engine: FakeEngine
) -> None:
    _setup_channel(api, world)
    _seed_mappings(engine, world.installation_id)
    recorded = _record_payment(api, world)
    payment_id = recorded["payment"]["id"]
    tranches = recorded["tranches"]
    _link_invoice_lines(engine, fake_engine, world.installation_id, tranches)
    _post_sale_fee(api, world, payment_id)

    first = tranches[0]
    _set_tranche_state(
        engine, first["id"], "confirmed", confirmed_net_irr=first["net_irr"]
    )
    response = api.post(
        f"{BASE}/tranches/{first['id']}/post-clearing",
        json={"company_id": 1, "payout_date": "2026-11-26"},
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["amount_irr"] == 22_250_000
    assert body["payment_state"] == "succeeded"
    assert body["reconciliation_id"] is not None
    assert body["posting"]["kind"] == "clearing"


def test_post_clearing_unconfirmed_tranche_is_422(
    api: TestClient, world: World, engine: Engine, fake_engine: FakeEngine
) -> None:
    _setup_channel(api, world)
    _seed_mappings(engine, world.installation_id)
    recorded = _record_payment(api, world)
    _link_invoice_lines(
        engine, fake_engine, world.installation_id, recorded["tranches"]
    )
    _post_sale_fee(api, world, recorded["payment"]["id"])
    first = recorded["tranches"][0]
    response = api.post(
        f"{BASE}/tranches/{first['id']}/post-clearing",
        json={"company_id": 1, "payout_date": "2026-11-26"},
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "SETTLEMENT_POSTING_PRECONDITION"


def test_post_bank_receipt_tie_out_failure_is_422(
    api: TestClient, world: World, engine: Engine, fake_engine: FakeEngine
) -> None:
    _setup_channel(api, world)
    _seed_mappings(engine, world.installation_id)
    recorded = _record_payment(api, world)
    payment_id = recorded["payment"]["id"]
    tranches = recorded["tranches"]
    _link_invoice_lines(engine, fake_engine, world.installation_id, tranches)
    _post_sale_fee(api, world, payment_id)
    first, second = tranches[0], tranches[1]
    _set_tranche_state(
        engine, first["id"], "confirmed", confirmed_net_irr=first["net_irr"]
    )
    response = api.post(
        f"{BASE}/tranches/{first['id']}/post-clearing",
        json={"company_id": 1, "payout_date": "2026-11-26"},
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 200, response.text
    _set_tranche_state(
        engine, first["id"], "received", received_net_irr=first["net_irr"]
    )
    # The batch confirms TWO tranches but the bank delivered one —
    # the tie-out fails even though tranche 1's deposits cover it.
    _set_tranche_state(
        engine, second["id"], "confirmed", confirmed_net_irr=second["net_irr"]
    )
    event_id = _setup_event_with_deposit(
        engine,
        world.installation_id,
        recorded["payment"]["payment_channel_id"],
        first["id"],
        deposit_irr=22_250_000,
    )
    session = sessionmaker(bind=engine)()
    try:
        session.add(
            SettlementMatchModel(
                id=str(uuid.uuid4()),
                installation_id=world.installation_id,
                tranche_id=second["id"],
                allocated_amount_irr=0,
                method="exact",
                settlement_event_id=event_id,
                is_automatic=True,
                matched_at=datetime.datetime.now(datetime.UTC),
            )
        )
        session.commit()
    finally:
        session.close()
    response = api.post(
        f"{BASE}/tranches/{first['id']}/post-bank-receipt",
        json={
            "company_id": 1,
            "settlement_event_id": event_id,
            "deposit_date": "2026-11-26",
        },
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "SETTLEMENT_TIE_OUT_FAILED"


# -- fee variance + refund ----------------------------------------------------


def test_post_fee_variance(
    api: TestClient, world: World, engine: Engine
) -> None:
    _setup_channel(api, world)
    _seed_mappings(engine, world.installation_id)
    recorded = _record_payment(api, world)
    payment_id = recorded["payment"]["id"]
    _post_sale_fee(api, world, payment_id)
    response = api.post(
        f"{BASE}/payments/{payment_id}/post-fee-variance",
        json={
            "company_id": 1,
            "actual_fee_irr": 11_000_000,
            "actual_vat_irr": 1_100_000,
            "reason": "Gateway file shows 11% fee for Aban",
            "date": "2026-11-26",
            "event_ref": "file:snapppay-aban",
        },
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["fee_variance_irr"] == 1_000_000
    assert body["vat_variance_irr"] == 100_000
    assert body["posting"]["kind"] == "fee_variance"
    assert body["exception_id"] is not None
    assert len(body["allocations"]) == 4


def test_apply_refund(
    api: TestClient, world: World, engine: Engine
) -> None:
    _setup_channel(api, world)
    _seed_mappings(engine, world.installation_id)
    recorded = _record_payment(api, world)
    payment_id = recorded["payment"]["id"]
    tranches = recorded["tranches"]
    _post_sale_fee(api, world, payment_id)
    response = api.post(
        f"{BASE}/payments/{payment_id}/apply-refund",
        json={
            "company_id": 1,
            "refund_amount_irr": 30_000_000,
            "reason": "Customer returned part of the order",
            "date": "2026-11-20",
            "event_ref": "refund:evt-1",
        },
        headers=_auth(world.key_manage_secret),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["refund_amount_irr"] == 30_000_000
    assert body["excess_irr"] == 0
    # Last open tranche first: tranche 4 (22.25M), then tranche 3 (7.75M).
    assert [a["tranche_id"] for a in body["allocations"]] == [
        tranches[3]["id"],
        tranches[2]["id"],
    ]
    assert [a["amount_irr"] for a in body["allocations"]] == [22_250_000, 7_750_000]
    assert body["posting"]["kind"] == "refund"
    assert body["exception_id"] is None
