"""Tests for the integrations registries + policy-gate REST endpoints (US-503).

Everything is real — the FastAPI app, scoped API-key auth, the
PostgreSQL database — except the fiscal-period checker, which the
evaluate endpoint reads through an overridden dependency (the real
checker is tested against a fake engine port in
tests/unit/test_integrations_services.py and the live engine is NOT
needed here).

Each test gets a FRESH installation id, so registry rows never leak
between tests; the fixture deletes the installation's registry rows,
the provisioned tenant, and the engine connection in teardown.
"""

from __future__ import annotations

import datetime
import os
import uuid
from collections.abc import Generator
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from hesabyar.domain.accounting.refs import CompanyRef
from hesabyar.domain.integrations.ports import FiscalPeriodCheckerPort
from hesabyar.infrastructure.auth import ApiKeyCrypto, ApiKeyService
from hesabyar.infrastructure.auth.credential_vault import generate_encryption_key
from hesabyar.infrastructure.config import AppSettings, AuthSettings
from hesabyar.infrastructure.persistence.postgres.models import (
    ApiClientModel,
    AutoPostPolicyModel,
    BankConnectionModel,
    Base,
    EngineCompanyModel,
    EngineConnectionModel,
    GatewayContractModel,
    InstallationHolidayModel,
    InstallationSettingsModel,
    IntegrationMappingModel,
    MappingSuggestionModel,
    PaymentChannelModel,
    StoreModel,
    TenantModel,
)
from hesabyar.interfaces.rest.app import create_app
from hesabyar.interfaces.rest.dependencies import get_fiscal_period_checker

TEST_DB_URL = os.getenv(
    "HESABYAR_DATABASE_URL",
    "postgresql+psycopg://hesabyar:hesabyar_pass@hesabyar-postgres:5432/hesabyar_test",
)

BASE = "/api/v1/integrations"

FOUR_TRANCHES = [
    {"month_offset": 1, "payout_day": 5, "percent": "25"},
    {"month_offset": 2, "payout_day": 5, "percent": "25"},
    {"month_offset": 3, "payout_day": 5, "percent": "25"},
    {"month_offset": 4, "payout_day": 5, "percent": "25"},
]

#: Integrations tables keyed directly by installation id, for teardown.
#: (gateway_contracts is keyed by channel id — cleaned separately below.)
_INTEGRATION_TABLES = (
    PaymentChannelModel,
    IntegrationMappingModel,
    MappingSuggestionModel,
    AutoPostPolicyModel,
    BankConnectionModel,
    StoreModel,
    InstallationSettingsModel,
    InstallationHolidayModel,
)


class StubFiscalChecker(FiscalPeriodCheckerPort):
    """Controllable fiscal-period checker for the evaluate endpoint."""

    def __init__(self) -> None:
        self.period_open = True

    def is_period_open(self, company: CompanyRef, on: datetime.date) -> bool:
        return self.period_open


STUB_CHECKER = StubFiscalChecker()


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
            id=str(uuid.uuid4()), slug=f"rest-503-{uuid.uuid4().hex[:8]}", name="Tenant 1"
        )
        other_tenant = TenantModel(
            id=str(uuid.uuid4()), slug=f"rest-503-{uuid.uuid4().hex[:8]}", name="Tenant 2"
        )
        session.add_all([tenant, other_tenant])
        session.flush()
        client = ApiClientModel(id=str(uuid.uuid4()), tenant_id=tenant.id, name="rest-503 client")
        other_client = ApiClientModel(
            id=str(uuid.uuid4()), tenant_id=other_tenant.id, name="rest-503 other client"
        )
        session.add_all([client, other_client])
        session.flush()

        connection = EngineConnectionModel(
            id=str(uuid.uuid4()),
            name=f"conn-503-{uuid.uuid4().hex[:8]}",
            engine="tryton",
            base_url="http://localhost:8010",
            # Unique per test: engine_connections has a unique constraint
            # on (engine, database_name), and the dev 'tryton-dev' row
            # already owns the bare 'hesabyar_tryton_test' name.
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
                name="rest-503 key",
                scopes=scopes,
                installation_id=key_installation_id,
            )

        key_read = _key(client, ["integrations:read"])
        key_manage = _key(client, ["integrations:read", "integrations:manage"])
        key_other = _key(client, ["company:read"])
        # A different installation id: the registry is installation-scoped.
        key_other_tenant = _key(
            other_client,
            ["integrations:read"],
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
            # Contracts are keyed by channel id: delete them via the
            # installation's channels before deleting the channels.
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
            for model in _INTEGRATION_TABLES:
                cleanup.execute(
                    delete(model).where(model.installation_id == installation_id)  # type: ignore[attr-defined]
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
    """The real app with the fiscal checker stubbed (open by default)."""
    STUB_CHECKER.period_open = True
    app = create_app(settings=settings)
    app.dependency_overrides[get_fiscal_period_checker] = lambda: STUB_CHECKER
    return TestClient(app, raise_server_exceptions=False)


def _auth(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


def _seed(api: TestClient, world: World) -> list[dict]:
    response = api.post(f"{BASE}/channels/seed", headers=_auth(world.key_manage_secret))
    assert response.status_code == 201, response.text
    return response.json()["items"]


def _contract_body(**overrides: object) -> dict:
    body: dict[str, object] = {
        "valid_from": "2026-11-01",
        "fee_percent": "5",
        "settlement_model": "tranches_4",
        "tranche_legs": FOUR_TRANCHES,
        "auto_post_policy": "auto_post_within_policy",
        "auto_post_max_amount_irr": 5_000_000,
    }
    body.update(overrides)
    return body


# -- auth -----------------------------------------------------------------


class TestAuth:
    def test_no_key_is_401(self, api, world) -> None:  # type: ignore[no-untyped-def]
        response = api.get(f"{BASE}/channels")
        assert response.status_code == 401
        assert response.json()["code"] == "UNAUTHORIZED"

    def test_wrong_scope_is_403(self, api, world) -> None:  # type: ignore[no-untyped-def]
        response = api.get(f"{BASE}/channels", headers=_auth(world.key_other_secret))
        assert response.status_code == 403
        assert response.json()["code"] == "FORBIDDEN"

    def test_read_key_cannot_write(self, api, world) -> None:  # type: ignore[no-untyped-def]
        response = api.post(
            f"{BASE}/channels",
            headers=_auth(world.key_read_secret),
            json={
                "channel_key": "zarinpal",
                "display_name_fa": "زرین‌پال",
                "channel_type": "gateway",
            },
        )
        assert response.status_code == 403
        assert response.json()["code"] == "FORBIDDEN"


# -- channels -------------------------------------------------------------


class TestChannelEndpoints:
    def test_seed_creates_eight_builtins_without_ilmaz(self, api, world) -> None:  # type: ignore[no-untyped-def]
        items = _seed(api, world)
        keys = {item["channel_key"] for item in items}
        assert keys == {
            "zarinpal",
            "sadad",
            "snapppay",
            "digipay",
            "torobpay",
            "salampay",
            "basalam_marketplace",
            # US-524: reserved row (born disabled; Torob order API UNVERIFIED).
            "torob_marketplace",
        }
        assert "ilmaz" not in keys
        assert all(item["enabled"] is False for item in items)
        by_key = {item["channel_key"]: item for item in items}
        # US-524: the reserved row is born DISABLED, not setup.
        assert by_key["torob_marketplace"]["status"] == "disabled"
        assert all(
            item["status"] == "setup"
            for key, item in by_key.items()
            if key != "torob_marketplace"
        )
        assert all(item["is_builtin"] is True for item in items)

    def test_seed_is_idempotent(self, api, world) -> None:  # type: ignore[no-untyped-def]
        first = _seed(api, world)
        second = _seed(api, world)
        assert {item["id"] for item in first} == {item["id"] for item in second}

    def test_create_custom_channel_crud(self, api, world) -> None:  # type: ignore[no-untyped-def]
        created = api.post(
            f"{BASE}/channels",
            headers=_auth(world.key_manage_secret),
            json={
                "channel_key": "ilmaz",
                "display_name_fa": "ایلماز",
                "channel_type": "custom",
                "notes": "custom channel added manually by the store",
            },
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["channel_key"] == "ilmaz"
        assert body["is_builtin"] is False
        assert body["enabled"] is False

        listed = api.get(f"{BASE}/channels", headers=_auth(world.key_read_secret))
        assert listed.status_code == 200
        assert [item["channel_key"] for item in listed.json()["items"]] == ["ilmaz"]

        fetched = api.get(f"{BASE}/channels/ilmaz", headers=_auth(world.key_read_secret))
        assert fetched.status_code == 200
        assert fetched.json()["display_name_fa"] == "ایلماز"

        updated = api.patch(
            f"{BASE}/channels/ilmaz",
            headers=_auth(world.key_manage_secret),
            json={"enabled": True},
        )
        assert updated.status_code == 200
        assert updated.json()["enabled"] is True

        deleted = api.delete(f"{BASE}/channels/ilmaz", headers=_auth(world.key_manage_secret))
        assert deleted.status_code == 204
        gone = api.get(f"{BASE}/channels/ilmaz", headers=_auth(world.key_read_secret))
        assert gone.status_code == 404
        assert gone.json()["code"] == "INTEGRATION_CHANNEL_NOT_FOUND"

    def test_duplicate_channel_key_conflicts(self, api, world) -> None:  # type: ignore[no-untyped-def]
        _seed(api, world)
        response = api.post(
            f"{BASE}/channels",
            headers=_auth(world.key_manage_secret),
            json={
                "channel_key": "zarinpal",
                "display_name_fa": "زرین‌پال",
                "channel_type": "gateway",
            },
        )
        assert response.status_code == 409
        assert response.json()["code"] == "INTEGRATION_CHANNEL_KEY_CONFLICT"

    def test_channel_in_use_cannot_be_deleted(self, api, world) -> None:  # type: ignore[no-untyped-def]
        _seed(api, world)
        contract = api.post(
            f"{BASE}/channels/zarinpal/contracts",
            headers=_auth(world.key_manage_secret),
            json=_contract_body(),
        )
        assert contract.status_code == 201, contract.text
        response = api.delete(
            f"{BASE}/channels/zarinpal", headers=_auth(world.key_manage_secret)
        )
        assert response.status_code == 409
        assert response.json()["code"] == "INTEGRATION_CHANNEL_IN_USE"

    def test_other_installation_cannot_see_channels(self, api, world) -> None:  # type: ignore[no-untyped-def]
        _seed(api, world)
        listed = api.get(
            f"{BASE}/channels", headers=_auth(world.key_other_tenant_secret)
        )
        # The other tenant's key carries a different installation id.
        assert listed.status_code == 200
        assert listed.json()["items"] == []


# -- contracts ------------------------------------------------------------


class TestContractEndpoints:
    def test_contract_versioning_preserves_history(self, api, world) -> None:  # type: ignore[no-untyped-def]
        _seed(api, world)
        v1 = api.post(
            f"{BASE}/channels/zarinpal/contracts",
            headers=_auth(world.key_manage_secret),
            json=_contract_body(),
        )
        assert v1.status_code == 201, v1.text
        assert v1.json()["version"] == 1
        assert v1.json()["status"] == "active"
        assert v1.json()["fee_percent"] == "5"

        v2 = api.post(
            f"{BASE}/channels/zarinpal/contracts",
            headers=_auth(world.key_manage_secret),
            json=_contract_body(valid_from="2026-12-01", fee_percent="7"),
        )
        assert v2.status_code == 201, v2.text
        assert v2.json()["version"] == 2

        active = api.get(
            f"{BASE}/channels/zarinpal/contracts/active",
            headers=_auth(world.key_read_secret),
        )
        assert active.status_code == 200
        assert active.json()["version"] == 2
        assert active.json()["fee_percent"] == "7"

        old = api.get(
            f"{BASE}/channels/zarinpal/contracts/1",
            headers=_auth(world.key_read_secret),
        )
        assert old.status_code == 200
        assert old.json()["status"] == "superseded"
        assert old.json()["valid_to"] == "2026-11-30"
        assert old.json()["fee_percent"] == "5"  # history is never rewritten

        listed = api.get(
            f"{BASE}/channels/zarinpal/contracts",
            headers=_auth(world.key_read_secret),
        )
        assert [item["version"] for item in listed.json()["items"]] == [1, 2]

    def test_contract_on_unknown_channel_is_404(self, api, world) -> None:  # type: ignore[no-untyped-def]
        response = api.post(
            f"{BASE}/channels/ghost/contracts",
            headers=_auth(world.key_manage_secret),
            json=_contract_body(),
        )
        assert response.status_code == 404
        assert response.json()["code"] == "INTEGRATION_CHANNEL_NOT_FOUND"

    def test_tranche_legs_must_sum_to_100(self, api, world) -> None:  # type: ignore[no-untyped-def]
        _seed(api, world)
        bad_legs = [
            {"month_offset": 1, "payout_day": 5, "percent": "50"},
            {"month_offset": 2, "payout_day": 5, "percent": "25"},
        ]
        response = api.post(
            f"{BASE}/channels/zarinpal/contracts",
            headers=_auth(world.key_manage_secret),
            json=_contract_body(tranche_legs=bad_legs),
        )
        assert response.status_code == 422
        assert response.json()["code"] == "INTEGRATION_CONTRACT_INVALID"

    def test_valid_from_must_move_forward(self, api, world) -> None:  # type: ignore[no-untyped-def]
        _seed(api, world)
        first = api.post(
            f"{BASE}/channels/zarinpal/contracts",
            headers=_auth(world.key_manage_secret),
            json=_contract_body(),
        )
        assert first.status_code == 201, first.text
        response = api.post(
            f"{BASE}/channels/zarinpal/contracts",
            headers=_auth(world.key_manage_secret),
            json=_contract_body(valid_from="2026-10-01", fee_percent="6"),
        )
        assert response.status_code == 422
        assert response.json()["code"] == "INTEGRATION_CONTRACT_INVALID"

    def test_no_active_contract_is_404(self, api, world) -> None:  # type: ignore[no-untyped-def]
        _seed(api, world)
        response = api.get(
            f"{BASE}/channels/zarinpal/contracts/active",
            headers=_auth(world.key_read_secret),
        )
        assert response.status_code == 404
        assert response.json()["code"] == "INTEGRATION_CONTRACT_NOT_FOUND"


# -- mappings + suggestions -----------------------------------------------


def _mapping_body(**overrides: object) -> dict:
    body: dict[str, object] = {
        "mapping_kind": "product_dimension",
        "channel_key": "zarinpal",
        "source_key": "SKU-1",
        "target_type": "account",
        "target_ref": "tryton:account:42",
    }
    body.update(overrides)
    return body


class TestMappingEndpoints:
    def test_mapping_crud_and_resolve(self, api, world) -> None:  # type: ignore[no-untyped-def]
        _seed(api, world)
        created = api.post(
            f"{BASE}/mappings",
            headers=_auth(world.key_manage_secret),
            json=_mapping_body(),
        )
        assert created.status_code == 201, created.text
        assert created.json()["version"] == 1
        assert created.json()["status"] == "active"

        resolved = api.get(
            f"{BASE}/mappings/resolve",
            headers=_auth(world.key_read_secret),
            params={
                "mapping_kind": "product_dimension",
                "channel_key": "zarinpal",
                "source_key": "SKU-1",
            },
        )
        assert resolved.status_code == 200
        assert resolved.json()["target_ref"] == "tryton:account:42"

        # A new version supersedes the old one; the old target is kept.
        v2 = api.post(
            f"{BASE}/mappings",
            headers=_auth(world.key_manage_secret),
            json=_mapping_body(target_ref="tryton:account:43"),
        )
        assert v2.status_code == 201, v2.text
        assert v2.json()["version"] == 2

        resolved2 = api.get(
            f"{BASE}/mappings/resolve",
            headers=_auth(world.key_read_secret),
            params={
                "mapping_kind": "product_dimension",
                "channel_key": "zarinpal",
                "source_key": "SKU-1",
            },
        )
        assert resolved2.json()["target_ref"] == "tryton:account:43"

        old = api.get(
            f"{BASE}/mappings/{created.json()['id']}",
            headers=_auth(world.key_read_secret),
        )
        assert old.json()["status"] == "superseded"
        assert old.json()["target_ref"] == "tryton:account:42"

    def test_resolve_unknown_mapping_is_404_fail_closed(self, api, world) -> None:  # type: ignore[no-untyped-def]
        response = api.get(
            f"{BASE}/mappings/resolve",
            headers=_auth(world.key_read_secret),
            params={
                "mapping_kind": "product_dimension",
                "channel_key": "zarinpal",
                "source_key": "SKU-UNKNOWN",
            },
        )
        assert response.status_code == 404
        assert response.json()["code"] == "INTEGRATION_MAPPING_NOT_FOUND"

    def test_bad_mapping_kind_is_400(self, api, world) -> None:  # type: ignore[no-untyped-def]
        """Unknown mapping kind → 400 INVALID_FILTER (router parses the kind)."""
        response = api.post(
            f"{BASE}/mappings",
            headers=_auth(world.key_manage_secret),
            json=_mapping_body(mapping_kind="not_a_kind"),
        )
        assert response.status_code == 400
        assert response.json()["code"] == "INVALID_FILTER"

    def test_suggestion_accept_and_reject(self, api, world) -> None:  # type: ignore[no-untyped-def]
        _seed(api, world)  # suggestions require the channel in the registry
        suggestion_body = {
            "mapping_kind": "product_dimension",
            "channel_key": "zarinpal",
            "source_key": "SKU-9",
            "suggested_target_type": "account",
            "suggested_target_ref": "tryton:account:99",
            "confidence": "0.8",
            "basis": "same category as SKU-1",
        }
        created = api.post(
            f"{BASE}/mapping-suggestions",
            headers=_auth(world.key_manage_secret),
            json=suggestion_body,
        )
        assert created.status_code == 201, created.text
        assert created.json()["status"] == "pending"

        # Re-creating the same pending suggestion is idempotent: the
        # service returns the existing row (at most one pending per
        # (kind, channel, source)), it does not create a second one.
        duplicate = api.post(
            f"{BASE}/mapping-suggestions",
            headers=_auth(world.key_manage_secret),
            json=suggestion_body,
        )
        assert duplicate.status_code == 201
        assert duplicate.json()["id"] == created.json()["id"]

        suggestion_id = created.json()["id"]
        accepted = api.post(
            f"{BASE}/mapping-suggestions/{suggestion_id}/accept",
            headers=_auth(world.key_manage_secret),
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["target_ref"] == "tryton:account:99"
        assert accepted.json()["suggestion_mode"] == "suggest"
        assert accepted.json()["source"] == "learned"

        # Already resolved → 409 on a second accept.
        again = api.post(
            f"{BASE}/mapping-suggestions/{suggestion_id}/accept",
            headers=_auth(world.key_manage_secret),
        )
        assert again.status_code == 409
        assert again.json()["code"] == "INTEGRATION_MAPPING_SUGGESTION_RESOLVED"

        # Rejection is recorded and the suggestion stays visible.
        other = api.post(
            f"{BASE}/mapping-suggestions",
            headers=_auth(world.key_manage_secret),
            json={**suggestion_body, "source_key": "SKU-10"},
        )
        assert other.status_code == 201, other.text
        rejected = api.post(
            f"{BASE}/mapping-suggestions/{other.json()['id']}/reject",
            headers=_auth(world.key_manage_secret),
        )
        assert rejected.status_code == 200
        assert rejected.json()["status"] == "rejected"

        listed = api.get(
            f"{BASE}/mapping-suggestions",
            headers=_auth(world.key_read_secret),
            params={"status": "rejected"},
        )
        assert len(listed.json()["items"]) == 1


# -- policy gate ----------------------------------------------------------


def _enable_auto_post(api: TestClient, world: World) -> None:
    response = api.put(
        f"{BASE}/settings",
        headers=_auth(world.key_manage_secret),
        json={"weekly_holiday": 4, "matching_window_days": 2, "auto_post_enabled": True},
    )
    assert response.status_code == 200, response.text


def _allow_event(api: TestClient, world: World, *, cap: int | None = 10_000_000) -> None:
    response = api.put(
        f"{BASE}/policy-gate/payment.captured",
        headers=_auth(world.key_manage_secret),
        json={"allowed": True, "max_amount_irr": cap, "require_tieout": False},
    )
    assert response.status_code == 200, response.text


def _evaluate(
    api: TestClient, world: World, *, amount_irr: int = 1_000_000, channel_key: str | None = None
) -> dict:
    response = api.post(
        f"{BASE}/policy-gate/evaluate",
        headers=_auth(world.key_read_secret),
        json={
            "event_type": "payment.captured",
            "company_id": world.company_id,
            "event_date": "2026-11-18",
            "amount_irr": amount_irr,
            "channel_key": channel_key,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


class TestPolicyGateEndpoints:
    def test_upsert_and_get_config(self, api, world) -> None:  # type: ignore[no-untyped-def]
        _allow_event(api, world)
        fetched = api.get(
            f"{BASE}/policy-gate/payment.captured", headers=_auth(world.key_read_secret)
        )
        assert fetched.status_code == 200
        assert fetched.json()["allowed"] is True
        assert fetched.json()["max_amount_irr"] == 10_000_000
        listed = api.get(f"{BASE}/policy-gate", headers=_auth(world.key_read_secret))
        assert len(listed.json()["items"]) == 1

        missing = api.get(
            f"{BASE}/policy-gate/sales.order.completed",
            headers=_auth(world.key_read_secret),
        )
        assert missing.status_code == 404

    def test_evaluate_allows_when_all_layers_pass(self, api, world) -> None:  # type: ignore[no-untyped-def]
        _enable_auto_post(api, world)
        _allow_event(api, world)
        decision = _evaluate(api, world)
        assert decision["allowed"] is True
        assert decision["action"] == "auto_post"
        assert all(layer["passed"] for layer in decision["layers"])

    def test_evaluate_denies_unauthorized_event(self, api, world) -> None:  # type: ignore[no-untyped-def]
        _enable_auto_post(api, world)
        decision = _evaluate(api, world)  # no policy row for payment.captured
        assert decision["allowed"] is False
        assert decision["action"] == "draft"

    def test_evaluate_denies_over_cap(self, api, world) -> None:  # type: ignore[no-untyped-def]
        _enable_auto_post(api, world)
        _allow_event(api, world)
        decision = _evaluate(api, world, amount_irr=20_000_000)
        assert decision["allowed"] is False
        layer = next(item for item in decision["layers"] if item["layer"] == "amount_cap")
        assert layer["passed"] is False

    def test_evaluate_denies_closed_period(self, api, world) -> None:  # type: ignore[no-untyped-def]
        _enable_auto_post(api, world)
        _allow_event(api, world)
        STUB_CHECKER.period_open = False
        decision = _evaluate(api, world)
        assert decision["allowed"] is False
        layer = next(item for item in decision["layers"] if item["layer"] == "period_lock")
        assert layer["passed"] is False

    def test_evaluate_denies_unknown_channel(self, api, world) -> None:  # type: ignore[no-untyped-def]
        _enable_auto_post(api, world)
        _allow_event(api, world)
        decision = _evaluate(api, world, channel_key="ghost")
        assert decision["allowed"] is False
        layer = next(item for item in decision["layers"] if item["layer"] == "channel")
        assert layer["passed"] is False

    def test_evaluate_unknown_company_is_404(self, api, world) -> None:  # type: ignore[no-untyped-def]
        response = api.post(
            f"{BASE}/policy-gate/evaluate",
            headers=_auth(world.key_read_secret),
            json={
                "event_type": "payment.captured",
                "company_id": str(uuid.uuid4()),
                "event_date": "2026-11-18",
                "amount_irr": 1_000_000,
            },
        )
        assert response.status_code == 404
        assert response.json()["code"] == "COMPANY_NOT_FOUND"


# -- bank connections -----------------------------------------------------


class TestBankConnectionEndpoints:
    def test_create_without_credentials(self, api, world) -> None:  # type: ignore[no-untyped-def]
        response = api.post(
            f"{BASE}/bank-connections",
            headers=_auth(world.key_manage_secret),
            json={
                "company_id": world.company_id,
                "bank_key": "melli",
                "display_name": "بانک ملی",
                "connection_kind": "manual_import",
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["has_credentials"] is False
        assert response.json()["credentials_key_id"] is None

    def test_create_with_credentials_never_returns_them(self, api, world) -> None:  # type: ignore[no-untyped-def]
        response = api.post(
            f"{BASE}/bank-connections",
            headers=_auth(world.key_manage_secret),
            json={
                "company_id": world.company_id,
                "bank_key": "fintech",
                "display_name": "فینوتک",
                "connection_kind": "api_oauth",
                "credentials": {"client_id": "abc", "client_secret": "shh-secret"},
            },
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["has_credentials"] is True
        assert body["credentials_key_id"]
        assert "shh-secret" not in response.text

        fetched = api.get(
            f"{BASE}/bank-connections/{body['id']}",
            headers=_auth(world.key_read_secret),
        )
        assert fetched.status_code == 200
        assert "shh-secret" not in fetched.text

    def test_internet_banking_password_rejected(self, api, world) -> None:  # type: ignore[no-untyped-def]
        response = api.post(
            f"{BASE}/bank-connections",
            headers=_auth(world.key_manage_secret),
            json={
                "company_id": world.company_id,
                "bank_key": "melli",
                "display_name": "بانک ملی",
                "connection_kind": "api_oauth",
                "credentials": {"internet_banking_password": "secret-value"},
            },
        )
        assert response.status_code == 422
        assert response.json()["code"] == "INTEGRATION_CREDENTIALS_FORBIDDEN"
        assert "secret-value" not in response.text

    def test_duplicate_bank_for_company_conflicts(self, api, world) -> None:  # type: ignore[no-untyped-def]
        body = {
            "company_id": world.company_id,
            "bank_key": "melli",
            "display_name": "بانک ملی",
            "connection_kind": "manual_import",
        }
        first = api.post(
            f"{BASE}/bank-connections", headers=_auth(world.key_manage_secret), json=body
        )
        assert first.status_code == 201, first.text
        second = api.post(
            f"{BASE}/bank-connections", headers=_auth(world.key_manage_secret), json=body
        )
        assert second.status_code == 409
        assert second.json()["code"] == "INTEGRATION_BANK_CONNECTION_CONFLICT"

    def test_rotate_patch_delete(self, api, world) -> None:  # type: ignore[no-untyped-def]
        created = api.post(
            f"{BASE}/bank-connections",
            headers=_auth(world.key_manage_secret),
            json={
                "company_id": world.company_id,
                "bank_key": "fintech",
                "display_name": "فینوتک",
                "connection_kind": "api_oauth",
                "credentials": {"client_id": "old"},
            },
        )
        assert created.status_code == 201, created.text
        connection_id = created.json()["id"]

        rotated = api.post(
            f"{BASE}/bank-connections/{connection_id}/credentials",
            headers=_auth(world.key_manage_secret),
            json={"credentials": {"client_id": "new"}},
        )
        assert rotated.status_code == 200, rotated.text
        assert rotated.json()["has_credentials"] is True

        updated = api.patch(
            f"{BASE}/bank-connections/{connection_id}",
            headers=_auth(world.key_manage_secret),
            json={"status": "disabled"},
        )
        assert updated.status_code == 200
        assert updated.json()["status"] == "disabled"

        deleted = api.delete(
            f"{BASE}/bank-connections/{connection_id}",
            headers=_auth(world.key_manage_secret),
        )
        assert deleted.status_code == 204
        gone = api.get(
            f"{BASE}/bank-connections/{connection_id}",
            headers=_auth(world.key_read_secret),
        )
        assert gone.status_code == 404

    def test_credentials_without_vault_key_is_503(self, world) -> None:  # type: ignore[no-untyped-def]
        """No HESABYAR_AUTH__CREDENTIALS_ENCRYPTION_KEY → storing fails closed."""
        app = create_app(settings=AppSettings())
        app.dependency_overrides[get_fiscal_period_checker] = lambda: STUB_CHECKER
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post(
            f"{BASE}/bank-connections",
            headers=_auth(world.key_manage_secret),
            json={
                "company_id": world.company_id,
                "bank_key": "fintech",
                "display_name": "فینوتک",
                "connection_kind": "api_oauth",
                "credentials": {"client_id": "abc"},
            },
        )
        assert response.status_code == 503
        assert response.json()["code"] == "INTEGRATION_CREDENTIAL_VAULT_UNAVAILABLE"


# -- stores ---------------------------------------------------------------


class TestStoreEndpoints:
    def test_create_store_returns_secrets_once(self, api, world) -> None:  # type: ignore[no-untyped-def]
        response = api.post(
            f"{BASE}/stores",
            headers=_auth(world.key_manage_secret),
            json={
                "store_key": "hayat",
                "display_name": "حیات",
                "store_type": "custom",
            },
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["webhook_secret"]
        assert body["api_key"].startswith("hya_store_")
        assert body["store"]["has_webhook_secret"] is True
        assert body["store"]["has_api_key"] is True
        # Scopes are normalized to sorted order on read.
        assert body["store"]["api_key_scopes"] == [
            "cheques:read",
            "orders:read",
            "payments:read",
        ]

        # No later read returns the secrets.
        fetched = api.get(f"{BASE}/stores/hayat", headers=_auth(world.key_read_secret))
        assert fetched.status_code == 200
        assert body["webhook_secret"] not in fetched.text
        assert body["api_key"] not in fetched.text

    def test_duplicate_store_key_conflicts(self, api, world) -> None:  # type: ignore[no-untyped-def]
        body = {"store_key": "hayat", "display_name": "حیات", "store_type": "custom"}
        first = api.post(
            f"{BASE}/stores", headers=_auth(world.key_manage_secret), json=body
        )
        assert first.status_code == 201, first.text
        second = api.post(
            f"{BASE}/stores", headers=_auth(world.key_manage_secret), json=body
        )
        assert second.status_code == 409
        assert second.json()["code"] == "INTEGRATION_STORE_KEY_CONFLICT"

    def test_write_scopes_rejected(self, api, world) -> None:  # type: ignore[no-untyped-def]
        response = api.post(
            f"{BASE}/stores",
            headers=_auth(world.key_manage_secret),
            json={
                "store_key": "hayat",
                "display_name": "حیات",
                "store_type": "custom",
                "api_key_scopes": ["orders:write"],
            },
        )
        assert response.status_code == 422

    def test_rotate_secrets(self, api, world) -> None:  # type: ignore[no-untyped-def]
        created = api.post(
            f"{BASE}/stores",
            headers=_auth(world.key_manage_secret),
            json={"store_key": "hayat", "display_name": "حیات", "store_type": "custom"},
        )
        assert created.status_code == 201, created.text
        old_secret = created.json()["webhook_secret"]
        old_key = created.json()["api_key"]

        rotated_secret = api.post(
            f"{BASE}/stores/hayat/rotate-webhook-secret",
            headers=_auth(world.key_manage_secret),
        )
        assert rotated_secret.status_code == 200, rotated_secret.text
        assert rotated_secret.json()["webhook_secret"]
        assert rotated_secret.json()["webhook_secret"] != old_secret
        assert rotated_secret.json()["api_key"] == ""

        rotated_key = api.post(
            f"{BASE}/stores/hayat/rotate-api-key",
            headers=_auth(world.key_manage_secret),
            json={"api_key_scopes": ["orders:read"]},
        )
        assert rotated_key.status_code == 200, rotated_key.text
        assert rotated_key.json()["api_key"].startswith("hya_store_")
        assert rotated_key.json()["api_key"] != old_key
        assert rotated_key.json()["webhook_secret"] == ""
        assert rotated_key.json()["store"]["api_key_scopes"] == ["orders:read"]

    def test_patch_and_delete_store(self, api, world) -> None:  # type: ignore[no-untyped-def]
        created = api.post(
            f"{BASE}/stores",
            headers=_auth(world.key_manage_secret),
            json={"store_key": "hayat", "display_name": "حیات", "store_type": "custom"},
        )
        assert created.status_code == 201, created.text
        updated = api.patch(
            f"{BASE}/stores/hayat",
            headers=_auth(world.key_manage_secret),
            json={"prices_in_toman": False, "status": "disabled"},
        )
        assert updated.status_code == 200
        assert updated.json()["prices_in_toman"] is False
        assert updated.json()["status"] == "disabled"
        deleted = api.delete(f"{BASE}/stores/hayat", headers=_auth(world.key_manage_secret))
        assert deleted.status_code == 204
        gone = api.get(f"{BASE}/stores/hayat", headers=_auth(world.key_read_secret))
        assert gone.status_code == 404
        assert gone.json()["code"] == "INTEGRATION_STORE_NOT_FOUND"


# -- settings + holidays --------------------------------------------------


class TestSettingsEndpoints:
    def test_get_settings_returns_defaults_when_unset(self, api, world) -> None:  # type: ignore[no-untyped-def]
        response = api.get(f"{BASE}/settings", headers=_auth(world.key_read_secret))
        assert response.status_code == 200
        body = response.json()
        assert body["weekly_holiday"] == 4  # Friday
        assert body["matching_window_days"] == 2
        assert body["auto_post_enabled"] is False

    def test_upsert_settings(self, api, world) -> None:  # type: ignore[no-untyped-def]
        response = api.put(
            f"{BASE}/settings",
            headers=_auth(world.key_manage_secret),
            json={"weekly_holiday": 3, "matching_window_days": 5, "auto_post_enabled": True},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["weekly_holiday"] == 3
        assert body["matching_window_days"] == 5
        assert body["auto_post_enabled"] is True

    def test_bad_weekly_holiday_rejected(self, api, world) -> None:  # type: ignore[no-untyped-def]
        response = api.put(
            f"{BASE}/settings",
            headers=_auth(world.key_manage_secret),
            json={"weekly_holiday": 7, "matching_window_days": 2, "auto_post_enabled": False},
        )
        assert response.status_code == 422
        assert response.json()["code"] == "INTEGRATION_SETTINGS_INVALID"

    def test_holiday_crud(self, api, world) -> None:  # type: ignore[no-untyped-def]
        added = api.post(
            f"{BASE}/holidays",
            headers=_auth(world.key_manage_secret),
            json={"holiday_date": "2026-03-21", "title": "نوروز"},
        )
        assert added.status_code == 201, added.text
        assert added.json()["title"] == "نوروز"

        # Idempotent: adding the same date again returns the same row.
        again = api.post(
            f"{BASE}/holidays",
            headers=_auth(world.key_manage_secret),
            json={"holiday_date": "2026-03-21", "title": "نوروز"},
        )
        assert again.status_code == 201
        assert again.json()["id"] == added.json()["id"]

        listed = api.get(f"{BASE}/holidays", headers=_auth(world.key_read_secret))
        assert listed.status_code == 200
        assert len(listed.json()) == 1  # bare list, not a paginated envelope

        deleted = api.delete(
            f"{BASE}/holidays/2026-03-21", headers=_auth(world.key_manage_secret)
        )
        assert deleted.status_code == 204
        missing = api.delete(
            f"{BASE}/holidays/2026-03-21", headers=_auth(world.key_manage_secret)
        )
        assert missing.status_code == 404
        assert missing.json()["code"] == "INTEGRATION_HOLIDAY_NOT_FOUND"
