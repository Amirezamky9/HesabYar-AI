"""Tests for the SQLAlchemy integrations repository (US-503).

Runs against the shared Supabase test DB with a transactional session
(rolled back after each test). The built-in channels are seeded inside
the transaction, so tests never depend on rows committed by other
modules. The services themselves are tested DB-free against an
in-memory fake (tests/unit/test_integrations_services.py); this module
verifies the repository implementation behind the port.
"""

from __future__ import annotations

import datetime
import os
import uuid
from collections.abc import Generator
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from hesabyar.domain.integrations.commands import (
    AddHoliday,
    CreateBankConnection,
    CreateGatewayContract,
    CreateIntegrationMapping,
    CreateMappingSuggestion,
    CreatePaymentChannel,
    CreateStore,
    RotateBankConnectionCredentials,
    RotateChannelCredentials,
    RotateStoreApiKey,
    RotateStoreWebhookSecret,
    UpdateBankConnection,
    UpdatePaymentChannel,
    UpdateStore,
    UpsertInstallationSettings,
    UpsertPolicyGateConfig,
)
from hesabyar.domain.integrations.models import (
    AutoPostPolicy,
    BankConnection,
    GatewayContract,
    IntegrationMapping,
    MappingKind,
    PaymentChannel,
    Store,
    SuggestionStatus,
    TrancheLeg,
)
from hesabyar.infrastructure.persistence.postgres.integrations_repository import (
    SqlAlchemyIntegrationsRepository,
)
from hesabyar.infrastructure.persistence.postgres.models import (
    Base,
    EngineCompanyModel,
    EngineConnectionModel,
    TenantModel,
)
from hesabyar.infrastructure.persistence.postgres.seed_payment_channels import (
    BUILTIN_CHANNELS,
    seed_builtin_channels,
)

TEST_DB_URL = os.getenv(
    "HESABYAR_DATABASE_URL",
    "postgresql+psycopg://hesabyar:hesabyar_pass@hesabyar-postgres:5432/hesabyar_test",
)

INSTALLATION_ID = "00000000-0000-0000-0000-000000000503"
OTHER_INSTALLATION_ID = "00000000-0000-0000-0000-000000000504"

FOUR_TRANCHES = (
    TrancheLeg(1, 5, Decimal("25")),
    TrancheLeg(2, 5, Decimal("25")),
    TrancheLeg(3, 5, Decimal("25")),
    TrancheLeg(4, 5, Decimal("25")),
)


@pytest.fixture(scope="module")
def engine() -> Generator[Engine, None, None]:
    eng = create_engine(TEST_DB_URL, echo=False)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def db(engine: Engine) -> Generator[Session, None, None]:
    connection = engine.connect()
    transaction = connection.begin()
    session = sessionmaker(bind=connection)()
    try:
        yield session
    finally:
        session.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()


@pytest.fixture()
def repository(db: Session) -> SqlAlchemyIntegrationsRepository:
    return SqlAlchemyIntegrationsRepository(db)


@pytest.fixture()
def company_id(db: Session) -> str:
    """A real engine-company row: bank_connections.company_id is an FK."""
    tenant = TenantModel(
        id=str(uuid.uuid4()),
        slug=f"int-repo-503-{uuid.uuid4().hex[:8]}",
        name="Integrations Repository Test Tenant",
    )
    db.add(tenant)
    db.flush()
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
    db.add(connection)
    db.flush()
    row = EngineCompanyModel(
        id=str(uuid.uuid4()),
        tenant_id=tenant.id,
        engine_connection_id=connection.id,
        external_company_id="1",
        company_name="Adapter Test",
    )
    db.add(row)
    db.flush()
    return row.id


def _make_channel(
    repository: SqlAlchemyIntegrationsRepository,
    channel_key: str = "zarinpal",
    installation_id: str = INSTALLATION_ID,
) -> PaymentChannel:
    return repository.save_channel(
        CreatePaymentChannel(
            installation_id=installation_id,
            channel_key=channel_key,
            display_name_fa="زرین‌پال",
            channel_type="gateway",
        )
    )


def _make_contract(
    repository: SqlAlchemyIntegrationsRepository,
    channel_id: str,
    *,
    version: int = 1,
    valid_from: datetime.date = datetime.date(2026, 11, 1),
    fee_percent: Decimal = Decimal("5"),
) -> GatewayContract:
    return repository.save_contract(
        CreateGatewayContract(
            channel_id=channel_id,
            version=version,
            valid_from=valid_from,
            fee_percent=fee_percent,
            settlement_model="tranches_4",
            tranche_legs=FOUR_TRANCHES,
            auto_post_policy=AutoPostPolicy.AUTO_POST_WITHIN_POLICY,
        )
    )


# -- seed -----------------------------------------------------------------


class TestSeedBuiltinChannels:
    def test_seed_creates_eight_disabled_channels(self, db, repository) -> None:  # type: ignore[no-untyped-def]
        rows = seed_builtin_channels(db, INSTALLATION_ID)
        assert len(rows) == 8
        assert {row.channel_key for row in rows} == {seed.channel_key for seed in BUILTIN_CHANNELS}
        assert all(row.enabled is False for row in rows)
        by_key = {row.channel_key: row for row in rows}
        assert by_key["torob_marketplace"].status == "disabled"  # RESERVED (US-524)
        assert by_key["torob_marketplace"].notes is not None
        assert "RESERVED" in by_key["torob_marketplace"].notes
        assert all(
            row.status == "setup" for key, row in by_key.items() if key != "torob_marketplace"
        )
        assert all(row.is_builtin is True for row in rows)
        channels = repository.list_channels(INSTALLATION_ID)
        assert len(channels) == 8
        reserved = repository.get_channel(INSTALLATION_ID, "torob_marketplace")
        assert reserved is not None
        assert reserved.status.value == "disabled"

    def test_seed_is_idempotent(self, db, repository) -> None:  # type: ignore[no-untyped-def]
        first = seed_builtin_channels(db, INSTALLATION_ID)
        second = seed_builtin_channels(db, INSTALLATION_ID)
        assert {row.id for row in first} == {row.id for row in second}
        assert len(repository.list_channels(INSTALLATION_ID)) == 8

    def test_ilmaz_is_not_seeded(self, db, repository) -> None:  # type: ignore[no-untyped-def]
        seed_builtin_channels(db, INSTALLATION_ID)
        keys = {channel.channel_key for channel in repository.list_channels(INSTALLATION_ID)}
        assert "ilmaz" not in keys
        assert repository.get_channel(INSTALLATION_ID, "ilmaz") is None

    def test_seed_creates_default_settings(self, db, repository) -> None:  # type: ignore[no-untyped-def]
        seed_builtin_channels(db, INSTALLATION_ID)
        settings = repository.get_installation_settings(INSTALLATION_ID)
        assert settings is not None
        assert settings.weekly_holiday == 4  # Friday
        assert settings.matching_window_days == 2
        assert settings.auto_post_enabled is False

    def test_seed_is_per_installation(self, db, repository) -> None:  # type: ignore[no-untyped-def]
        seed_builtin_channels(db, INSTALLATION_ID)
        assert repository.list_channels(OTHER_INSTALLATION_ID) == []
        assert repository.get_installation_settings(OTHER_INSTALLATION_ID) is None


# -- channels -------------------------------------------------------------


class TestChannelRepository:
    def test_save_and_get_round_trip(self, repository) -> None:  # type: ignore[no-untyped-def]
        saved = _make_channel(repository)
        assert saved.id
        by_key = repository.get_channel(INSTALLATION_ID, "zarinpal")
        assert by_key is not None and by_key.id == saved.id
        by_id = repository.get_channel_by_id(saved.id)
        assert by_id is not None and by_id.channel_key == "zarinpal"
        assert saved.enabled is False
        assert saved.status == "setup"

    def test_get_unknown_channel_returns_none(self, repository) -> None:  # type: ignore[no-untyped-def]
        assert repository.get_channel(INSTALLATION_ID, "ghost") is None
        assert repository.get_channel_by_id(str(uuid.uuid4())) is None

    def test_list_is_installation_scoped(self, repository) -> None:  # type: ignore[no-untyped-def]
        _make_channel(repository, "zarinpal", INSTALLATION_ID)
        _make_channel(repository, "sadad", OTHER_INSTALLATION_ID)
        assert [c.channel_key for c in repository.list_channels(INSTALLATION_ID)] == ["zarinpal"]
        assert [c.channel_key for c in repository.list_channels(OTHER_INSTALLATION_ID)] == ["sadad"]

    def test_update_channel(self, repository) -> None:  # type: ignore[no-untyped-def]
        saved = _make_channel(repository)
        updated = repository.update_channel(
            UpdatePaymentChannel(
                installation_id=INSTALLATION_ID,
                channel_key="zarinpal",
                enabled=True,
                status="active",
                merchant_ref="m-1",
            )
        )
        assert updated is not None
        assert updated.id == saved.id
        assert updated.enabled is True
        assert updated.status == "active"
        assert updated.merchant_ref == "m-1"
        assert updated.display_name_fa == saved.display_name_fa  # untouched fields kept

    def test_update_unknown_channel_returns_none(self, repository) -> None:  # type: ignore[no-untyped-def]
        assert (
            repository.update_channel(
                UpdatePaymentChannel(
                    installation_id=INSTALLATION_ID, channel_key="ghost", enabled=True
                )
            )
            is None
        )

    def test_rotate_channel_credentials_round_trip(self, repository) -> None:  # type: ignore[no-untyped-def]
        """US-524: credentials ciphertext persists; has_credentials flips on."""
        saved = _make_channel(repository)
        assert saved.has_credentials is False
        assert repository.get_channel_ciphertext(saved.id) is None
        rotated = repository.rotate_channel_credentials(
            RotateChannelCredentials(
                installation_id=INSTALLATION_ID,
                channel_key="zarinpal",
                credentials_ciphertext="cipher-1",
                credentials_key_id="key-1",
            )
        )
        assert rotated is not None and rotated.has_credentials is True
        assert repository.get_channel_ciphertext(saved.id) == "cipher-1"
        by_key = repository.get_channel(INSTALLATION_ID, "zarinpal")
        assert by_key is not None and by_key.has_credentials is True

    def test_rotate_credentials_unknown_channel_returns_none(self, repository) -> None:  # type: ignore[no-untyped-def]
        assert (
            repository.rotate_channel_credentials(
                RotateChannelCredentials(
                    installation_id=INSTALLATION_ID,
                    channel_key="ghost",
                    credentials_ciphertext="cipher-1",
                    credentials_key_id="key-1",
                )
            )
            is None
        )

    def test_delete_channel(self, repository) -> None:  # type: ignore[no-untyped-def]
        _make_channel(repository)
        assert repository.delete_channel(INSTALLATION_ID, "zarinpal") is True
        assert repository.get_channel(INSTALLATION_ID, "zarinpal") is None
        assert repository.delete_channel(INSTALLATION_ID, "zarinpal") is False

    def test_duplicate_channel_key_rejected_by_database(self, db, repository) -> None:  # type: ignore[no-untyped-def]
        _make_channel(repository)
        with pytest.raises(IntegrityError):
            repository.save_channel(
                CreatePaymentChannel(
                    installation_id=INSTALLATION_ID,
                    channel_key="zarinpal",
                    display_name_fa="زرین‌پال",
                    channel_type="gateway",
                )
            )


# -- contracts ------------------------------------------------------------


class TestContractRepository:
    def test_save_and_get_active(self, repository) -> None:  # type: ignore[no-untyped-def]
        channel = _make_channel(repository)
        contract = _make_contract(repository, channel.id)
        assert contract.version == 1
        assert contract.status == "active"
        assert contract.fee_percent == Decimal("5")
        assert contract.tranche_legs == FOUR_TRANCHES
        active = repository.get_active_contract(channel.id)
        assert active is not None and active.id == contract.id
        by_key = repository.get_active_contract_for_channel_key(INSTALLATION_ID, "zarinpal")
        assert by_key is not None and by_key.id == contract.id
        by_version = repository.get_contract(channel.id, 1)
        assert by_version is not None and by_version.id == contract.id

    def test_supersede_preserves_history(self, repository) -> None:  # type: ignore[no-untyped-def]
        channel = _make_channel(repository)
        v1 = _make_contract(repository, channel.id, version=1, fee_percent=Decimal("5"))
        repository.supersede_contract(v1.id, datetime.date(2026, 11, 30))
        v2 = _make_contract(
            repository,
            channel.id,
            version=2,
            valid_from=datetime.date(2026, 12, 1),
            fee_percent=Decimal("7"),
        )
        old = repository.get_contract(channel.id, 1)
        assert old is not None
        assert old.status == "superseded"
        assert old.valid_to == datetime.date(2026, 11, 30)
        assert old.fee_percent == Decimal("5")  # history is never rewritten
        active = repository.get_active_contract(channel.id)
        assert active is not None and active.id == v2.id
        assert repository.count_contracts(channel.id) == 2
        versions = [c.version for c in repository.list_contracts(channel.id)]
        assert versions == [2, 1]  # newest version first

    def test_no_active_contract_returns_none(self, repository) -> None:  # type: ignore[no-untyped-def]
        channel = _make_channel(repository)
        assert repository.get_active_contract(channel.id) is None
        assert (
            repository.get_active_contract_for_channel_key(INSTALLATION_ID, "zarinpal")
            is None
        )
        assert repository.get_contract(channel.id, 1) is None

    def test_decimal_fee_survives_round_trip(self, repository) -> None:  # type: ignore[no-untyped-def]
        channel = _make_channel(repository)
        _make_contract(repository, channel.id, fee_percent=Decimal("12.5"))
        fetched = repository.get_contract(channel.id, 1)
        assert fetched is not None
        assert fetched.fee_percent == Decimal("12.5")


# -- mappings + suggestions -----------------------------------------------


def _make_mapping(
    repository: SqlAlchemyIntegrationsRepository,
    *,
    version: int = 1,
    source_key: str = "SKU-1",
    target_ref: str = "tryton:account:42",
) -> IntegrationMapping:
    return repository.save_mapping(
        CreateIntegrationMapping(
            installation_id=INSTALLATION_ID,
            mapping_kind=MappingKind.PRODUCT_DIMENSION,
            channel_key="zarinpal",
            source_key=source_key,
            target_type="account",
            target_ref=target_ref,
            version=version,
        )
    )


class TestMappingRepository:
    def test_save_and_resolve(self, repository) -> None:  # type: ignore[no-untyped-def]
        mapping = _make_mapping(repository)
        resolved = repository.resolve_mapping(
            INSTALLATION_ID,
            mapping_kind=MappingKind.PRODUCT_DIMENSION,
            channel_key="zarinpal",
            source_key="SKU-1",
        )
        assert resolved is not None and resolved.id == mapping.id
        assert repository.get_mapping(mapping.id) is not None

    def test_resolve_misses_on_any_dimension(self, repository) -> None:  # type: ignore[no-untyped-def]
        _make_mapping(repository)
        base = dict(
            installation_id=INSTALLATION_ID,
            mapping_kind=MappingKind.PRODUCT_DIMENSION,
            channel_key="zarinpal",
            source_key="SKU-1",
        )
        assert repository.resolve_mapping(**{**base, "source_key": "SKU-2"}) is None
        assert repository.resolve_mapping(**{**base, "channel_key": "sadad"}) is None
        assert (
            repository.resolve_mapping(
                **{**base, "mapping_kind": MappingKind.EXTERNAL_PARTY}
            )
            is None
        )
        assert (
            repository.resolve_mapping(**{**base, "installation_id": OTHER_INSTALLATION_ID})
            is None
        )

    def test_supersede_leaves_old_version_resolvable_no_more(self, repository) -> None:  # type: ignore[no-untyped-def]
        v1 = _make_mapping(repository, version=1, target_ref="tryton:account:42")
        repository.supersede_mapping(v1.id)
        v2 = _make_mapping(repository, version=2, target_ref="tryton:account:43")
        resolved = repository.resolve_mapping(
            INSTALLATION_ID,
            mapping_kind=MappingKind.PRODUCT_DIMENSION,
            channel_key="zarinpal",
            source_key="SKU-1",
        )
        assert resolved is not None and resolved.id == v2.id
        old = repository.get_mapping(v1.id)
        assert old is not None and old.status == "superseded"
        assert old.target_ref == "tryton:account:42"  # history preserved

    def test_list_filters(self, repository) -> None:  # type: ignore[no-untyped-def]
        _make_mapping(repository, source_key="SKU-1")
        _make_mapping(repository, source_key="SKU-2")
        assert len(repository.list_mappings(INSTALLATION_ID)) == 2
        assert (
            len(
                repository.list_mappings(
                    INSTALLATION_ID, mapping_kind=MappingKind.EXTERNAL_PARTY
                )
            )
            == 0
        )
        assert len(repository.list_mappings(INSTALLATION_ID, source_key="SKU-1")) == 1
        assert len(repository.list_mappings(INSTALLATION_ID, channel_key="sadad")) == 0

    def test_count_mappings_for_channel(self, repository) -> None:  # type: ignore[no-untyped-def]
        _make_mapping(repository, source_key="SKU-1")
        _make_mapping(repository, source_key="SKU-2")
        assert repository.count_mappings_for_channel(INSTALLATION_ID, "zarinpal") == 2
        assert repository.count_mappings_for_channel(INSTALLATION_ID, "sadad") == 0

    def test_suggestion_lifecycle(self, repository) -> None:  # type: ignore[no-untyped-def]
        suggestion = repository.save_suggestion(
            CreateMappingSuggestion(
                installation_id=INSTALLATION_ID,
                mapping_kind=MappingKind.PRODUCT_DIMENSION,
                channel_key="zarinpal",
                source_key="SKU-9",
                suggested_target_type="account",
                suggested_target_ref="tryton:account:99",
                confidence=Decimal("0.8"),
                basis="same category as SKU-1",
            )
        )
        assert suggestion.status == "pending"
        pending = repository.get_pending_suggestion(
            INSTALLATION_ID,
            mapping_kind=MappingKind.PRODUCT_DIMENSION,
            channel_key="zarinpal",
            source_key="SKU-9",
        )
        assert pending is not None and pending.id == suggestion.id
        resolved = repository.resolve_suggestion(
            suggestion.id, status=SuggestionStatus.ACCEPTED, resolved_by="operator-1"
        )
        assert resolved is not None
        assert resolved.status == "accepted"
        assert resolved.resolved_by == "operator-1"
        assert (
            repository.get_pending_suggestion(
                INSTALLATION_ID,
                mapping_kind=MappingKind.PRODUCT_DIMENSION,
                channel_key="zarinpal",
                source_key="SKU-9",
            )
            is None
        )
        assert repository.list_suggestions(INSTALLATION_ID, status=SuggestionStatus.PENDING) == []
        assert len(repository.list_suggestions(INSTALLATION_ID)) == 1


# -- policy gate config ---------------------------------------------------


class TestPolicyConfigRepository:
    def test_save_and_get(self, repository) -> None:  # type: ignore[no-untyped-def]
        config = repository.save_policy_config(
            UpsertPolicyGateConfig(
                installation_id=INSTALLATION_ID,
                event_type="payment.captured",
                allowed=True,
                max_amount_irr=10_000_000,
            )
        )
        assert config.allowed is True
        assert config.max_amount_irr == 10_000_000
        assert repository.get_policy_config(INSTALLATION_ID, "payment.captured") is not None
        assert repository.get_policy_config(INSTALLATION_ID, "sales.order.completed") is None

    def test_save_replaces_existing_config(self, repository) -> None:  # type: ignore[no-untyped-def]
        first = repository.save_policy_config(
            UpsertPolicyGateConfig(
                installation_id=INSTALLATION_ID,
                event_type="payment.captured",
                allowed=True,
                max_amount_irr=10_000_000,
            )
        )
        second = repository.save_policy_config(
            UpsertPolicyGateConfig(
                installation_id=INSTALLATION_ID,
                event_type="payment.captured",
                allowed=False,
            )
        )
        assert first.id == second.id
        assert second.allowed is False
        assert second.max_amount_irr is None
        assert len(repository.list_policy_configs(INSTALLATION_ID)) == 1


# -- bank connections -----------------------------------------------------


def _make_bank_connection(
    repository: SqlAlchemyIntegrationsRepository,
    company_id: str,
    *,
    bank_key: str = "melli",
    ciphertext: str | None = None,
) -> BankConnection:
    return repository.save_bank_connection(
        CreateBankConnection(
            installation_id=INSTALLATION_ID,
            company_id=company_id,
            bank_key=bank_key,
            display_name="بانک ملی",
            connection_kind="manual_import",
            credentials_ciphertext=ciphertext,
            credentials_key_id="key-1" if ciphertext else None,
        )
    )


class TestBankConnectionRepository:
    def test_save_and_get(self, repository, company_id) -> None:  # type: ignore[no-untyped-def]
        connection = _make_bank_connection(repository, company_id, ciphertext="cipher-1")
        assert connection.has_credentials is True
        assert connection.credentials_key_id == "key-1"
        assert repository.get_bank_connection_ciphertext(connection.id) == "cipher-1"
        fetched = repository.get_bank_connection(INSTALLATION_ID, connection.id)
        assert fetched is not None and fetched.bank_key == "melli"
        assert len(repository.list_bank_connections(INSTALLATION_ID)) == 1
        assert len(repository.list_bank_connections(INSTALLATION_ID, company_id=company_id)) == 1
        assert (
            len(
                repository.list_bank_connections(
                    INSTALLATION_ID, company_id="00000000-0000-0000-0000-000000000999"
                )
            )
            == 0
        )

    def test_update_and_rotate_and_delete(self, repository, company_id) -> None:  # type: ignore[no-untyped-def]
        connection = _make_bank_connection(repository, company_id)
        updated = repository.update_bank_connection(
            UpdateBankConnection(
                installation_id=INSTALLATION_ID,
                connection_id=connection.id,
                status="disabled",
            )
        )
        assert updated is not None and updated.status == "disabled"
        rotated = repository.rotate_bank_connection_credentials(
            RotateBankConnectionCredentials(
                installation_id=INSTALLATION_ID,
                connection_id=connection.id,
                credentials_ciphertext="cipher-2",
                credentials_key_id="key-2",
            )
        )
        assert rotated is not None and rotated.has_credentials is True
        assert repository.get_bank_connection_ciphertext(connection.id) == "cipher-2"
        assert repository.delete_bank_connection(INSTALLATION_ID, connection.id) is True
        assert repository.get_bank_connection(INSTALLATION_ID, connection.id) is None

    def test_duplicate_bank_for_company_rejected_by_database(self, db, repository, company_id) -> None:  # type: ignore[no-untyped-def]
        _make_bank_connection(repository, company_id)
        with pytest.raises(IntegrityError):
            repository.save_bank_connection(
                CreateBankConnection(
                    installation_id=INSTALLATION_ID,
                    company_id=company_id,
                    bank_key="melli",
                    display_name="بانک ملی",
                    connection_kind="manual_import",
                )
            )


# -- stores ---------------------------------------------------------------


def _make_store(
    repository: SqlAlchemyIntegrationsRepository, *, store_key: str = "hayat"
) -> Store:
    return repository.save_store(
        CreateStore(
            installation_id=INSTALLATION_ID,
            store_key=store_key,
            display_name="حیات",
            store_type="custom",
            prices_in_toman=True,
            webhook_secret_ciphertext="webhook-cipher-1",
            api_key_ciphertext="api-key-cipher-1",
            api_key_scopes=("orders:read", "payments:read", "cheques:read"),
        )
    )


class TestStoreRepository:
    def test_save_and_get(self, repository) -> None:  # type: ignore[no-untyped-def]
        store = _make_store(repository)
        assert store.has_webhook_secret is True
        assert store.has_api_key is True
        # Scopes are normalized to sorted order on read.
        assert store.api_key_scopes == ("cheques:read", "orders:read", "payments:read")
        assert repository.get_store_ciphertext(store.id, "webhook_secret") == "webhook-cipher-1"
        assert repository.get_store_ciphertext(store.id, "api_key") == "api-key-cipher-1"
        fetched = repository.get_store(INSTALLATION_ID, "hayat")
        assert fetched is not None and fetched.id == store.id
        assert len(repository.list_stores(INSTALLATION_ID)) == 1

    def test_update_and_rotations(self, repository) -> None:  # type: ignore[no-untyped-def]
        store = _make_store(repository)
        updated = repository.update_store(
            UpdateStore(
                installation_id=INSTALLATION_ID,
                store_key="hayat",
                prices_in_toman=False,
                status="disabled",
            )
        )
        assert updated is not None
        assert updated.prices_in_toman is False
        assert updated.status == "disabled"
        rotated_secret = repository.rotate_store_webhook_secret(
            RotateStoreWebhookSecret(
                installation_id=INSTALLATION_ID,
                store_key="hayat",
                webhook_secret_ciphertext="webhook-cipher-2",
            )
        )
        assert rotated_secret is not None
        assert repository.get_store_ciphertext(store.id, "webhook_secret") == "webhook-cipher-2"
        assert repository.get_store_ciphertext(store.id, "api_key") == "api-key-cipher-1"
        rotated_key = repository.rotate_store_api_key(
            RotateStoreApiKey(
                installation_id=INSTALLATION_ID,
                store_key="hayat",
                api_key_ciphertext="api-key-cipher-2",
                api_key_scopes=("orders:read",),
            )
        )
        assert rotated_key is not None
        assert rotated_key.api_key_scopes == ("orders:read",)
        assert repository.get_store_ciphertext(store.id, "api_key") == "api-key-cipher-2"

    def test_delete_store(self, repository) -> None:  # type: ignore[no-untyped-def]
        _make_store(repository)
        assert repository.delete_store(INSTALLATION_ID, "hayat") is True
        assert repository.get_store(INSTALLATION_ID, "hayat") is None
        assert repository.delete_store(INSTALLATION_ID, "hayat") is False

    def test_duplicate_store_key_rejected_by_database(self, db, repository) -> None:  # type: ignore[no-untyped-def]
        _make_store(repository)
        with pytest.raises(IntegrityError):
            repository.save_store(
                CreateStore(
                    installation_id=INSTALLATION_ID,
                    store_key="hayat",
                    display_name="حیات",
                    store_type="custom",
                    prices_in_toman=True,
                    webhook_secret_ciphertext="x",
                    api_key_ciphertext="y",
                )
            )


# -- installation settings + holidays -------------------------------------


class TestSettingsRepository:
    def test_save_and_get(self, repository) -> None:  # type: ignore[no-untyped-def]
        assert repository.get_installation_settings(INSTALLATION_ID) is None
        settings = repository.save_installation_settings(
            UpsertInstallationSettings(
                installation_id=INSTALLATION_ID,
                weekly_holiday=3,
                matching_window_days=5,
                auto_post_enabled=True,
            )
        )
        assert settings.weekly_holiday == 3
        assert settings.matching_window_days == 5
        assert settings.auto_post_enabled is True
        again = repository.save_installation_settings(
            UpsertInstallationSettings(installation_id=INSTALLATION_ID)
        )
        assert again.weekly_holiday == 4  # defaults replace the row
        assert again.auto_post_enabled is False

    def test_holidays(self, repository) -> None:  # type: ignore[no-untyped-def]
        first = repository.add_holiday(
            AddHoliday(
                installation_id=INSTALLATION_ID,
                holiday_date=datetime.date(2026, 3, 21),
                title="نوروز",
            )
        )
        assert first.title == "نوروز"
        repository.add_holiday(
            AddHoliday(
                installation_id=INSTALLATION_ID,
                holiday_date=datetime.date(2026, 4, 2),
                title="سیزده بدر",
            )
        )
        holidays = repository.list_holidays(INSTALLATION_ID)
        assert [h.holiday_date for h in holidays] == [
            datetime.date(2026, 3, 21),
            datetime.date(2026, 4, 2),
        ]
        assert repository.list_holidays(OTHER_INSTALLATION_ID) == []
        assert repository.delete_holiday(INSTALLATION_ID, datetime.date(2026, 3, 21)) is True
        assert repository.delete_holiday(INSTALLATION_ID, datetime.date(2026, 3, 21)) is False
        assert len(repository.list_holidays(INSTALLATION_ID)) == 1

    def test_duplicate_holiday_rejected_by_database(self, db, repository) -> None:  # type: ignore[no-untyped-def]
        repository.add_holiday(
            AddHoliday(
                installation_id=INSTALLATION_ID,
                holiday_date=datetime.date(2026, 3, 21),
                title="نوروز",
            )
        )
        with pytest.raises(IntegrityError):
            repository.add_holiday(
                AddHoliday(
                    installation_id=INSTALLATION_ID,
                    holiday_date=datetime.date(2026, 3, 21),
                    title="نوروز",
                )
            )
