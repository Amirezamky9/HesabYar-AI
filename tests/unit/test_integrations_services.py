"""Service tests for the integrations registries + policy gate (US-503).

DB-free: services run against an in-memory fake of the registry port,
a real Fernet vault (test-generated key), and a stub fiscal checker.
The SQLAlchemy repository has its own DB tests
(``tests/infrastructure/test_integrations_repository.py``).
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from hesabyar.application.commands.integrations import (
    BankConnectionService,
    ChannelRegistryService,
    GatewayContractService,
    InstallationSettingsService,
    MappingService,
    PolicyGateConfigService,
    PolicyGateService,
    StoreService,
)
from hesabyar.application.errors import (
    IntegrationBankConnectionConflictError,
    IntegrationChannelInUseError,
    IntegrationChannelKeyConflictError,
    IntegrationChannelNotFoundError,
    IntegrationChannelReservedError,
    IntegrationContractInvalidError,
    IntegrationCredentialsForbiddenError,
    IntegrationCredentialVaultUnavailableError,
    IntegrationHolidayNotFoundError,
    IntegrationMappingInvalidError,
    IntegrationMappingNotFoundError,
    IntegrationMappingNotPromotableError,
    IntegrationMappingSuggestionNotFoundError,
    IntegrationMappingSuggestionResolvedError,
    IntegrationSettingsInvalidError,
    IntegrationStoreKeyConflictError,
    IntegrationStoreNotFoundError,
)
from hesabyar.domain.accounting.refs import CompanyRef
from hesabyar.domain.accounting.value_objects import FiscalPeriodView
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.errors import HesabYarError
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
    SetStoreDbDsn,
    UpdateBankConnection,
    UpdatePaymentChannel,
    UpdateStore,
    UpsertInstallationSettings,
    UpsertPolicyGateConfig,
)
from hesabyar.domain.integrations.models import (
    MAPPING_AUTO_MIN_CONFIRMATIONS,
    AutoPostPolicy,
    BankConnection,
    ChannelStatus,
    ContractStatus,
    GatewayContract,
    Holiday,
    InstallationSettings,
    IntegrationMapping,
    MappingKind,
    MappingStatus,
    MappingSuggestion,
    PaymentChannel,
    PolicyGateConfig,
    Store,
    SuggestionMode,
    SuggestionStatus,
    TrancheLeg,
)
from hesabyar.infrastructure.accounting.tryton.fiscal_period_checker import (
    TrytonFiscalPeriodChecker,
)
from hesabyar.infrastructure.auth.credential_vault import (
    FernetCredentialVault,
    generate_encryption_key,
)
from hesabyar.infrastructure.persistence.postgres.seed_payment_channels import (
    BUILTIN_CHANNELS,
)

INSTALLATION_ID = "00000000-0000-0000-0000-000000000503"
OTHER_INSTALLATION_ID = "00000000-0000-0000-0000-000000000504"
COMPANY_ID = "00000000-0000-0000-0000-000000000505"
NOW = datetime.datetime(2026, 11, 18, 12, 0, tzinfo=datetime.UTC)

FOUR_TRANCHES = (
    TrancheLeg(1, 5, Decimal("25")),
    TrancheLeg(2, 5, Decimal("25")),
    TrancheLeg(3, 5, Decimal("25")),
    TrancheLeg(4, 5, Decimal("25")),
)


def _new_id() -> str:
    return str(uuid.uuid4())


@dataclass
class FakeIntegrationsRepository:
    """In-memory implementation of the registry port (for service tests)."""

    channels: dict[tuple[str, str], PaymentChannel] = field(default_factory=dict)
    contracts: dict[str, GatewayContract] = field(default_factory=dict)
    mappings: dict[str, IntegrationMapping] = field(default_factory=dict)
    suggestions: dict[str, MappingSuggestion] = field(default_factory=dict)
    policies: dict[tuple[str, str], PolicyGateConfig] = field(default_factory=dict)
    bank_connections: dict[str, BankConnection] = field(default_factory=dict)
    bank_ciphertexts: dict[str, str] = field(default_factory=dict)
    channel_ciphertexts: dict[str, str] = field(default_factory=dict)
    stores: dict[tuple[str, str], Store] = field(default_factory=dict)
    store_ciphertexts: dict[tuple[str, str], str] = field(default_factory=dict)
    settings: dict[str, InstallationSettings] = field(default_factory=dict)
    holidays: dict[tuple[str, datetime.date], Holiday] = field(default_factory=dict)

    # -- channels ---------------------------------------------------------

    def list_channels(self, installation_id: str) -> Sequence[PaymentChannel]:
        return sorted(
            (c for (inst, _), c in self.channels.items() if inst == installation_id),
            key=lambda c: c.channel_key,
        )

    def get_channel(self, installation_id: str, channel_key: str) -> PaymentChannel | None:
        return self.channels.get((installation_id, channel_key))

    def get_channel_by_id(self, channel_id: str) -> PaymentChannel | None:
        for channel in self.channels.values():
            if channel.id == channel_id:
                return channel
        return None

    def save_channel(self, command: CreatePaymentChannel) -> PaymentChannel:
        channel = PaymentChannel(
            id=_new_id(),
            installation_id=command.installation_id,
            channel_key=command.channel_key,
            display_name_fa=command.display_name_fa,
            channel_type=command.channel_type,
            is_builtin=False,
            enabled=False,
            status=ChannelStatus.SETUP,
            merchant_ref=command.merchant_ref,
            settlement_iban=command.settlement_iban,
            environment=command.environment,
            notes=command.notes,
            created_at=NOW,
            updated_at=NOW,
        )
        self.channels[(command.installation_id, command.channel_key)] = channel
        return channel

    def update_channel(self, command: UpdatePaymentChannel) -> PaymentChannel | None:
        channel = self.channels.get((command.installation_id, command.channel_key))
        if channel is None:
            return None
        updated = PaymentChannel(
            id=channel.id,
            installation_id=channel.installation_id,
            channel_key=channel.channel_key,
            display_name_fa=command.display_name_fa or channel.display_name_fa,
            channel_type=command.channel_type or channel.channel_type,
            is_builtin=channel.is_builtin,
            enabled=command.enabled if command.enabled is not None else channel.enabled,
            status=ChannelStatus(command.status) if command.status else channel.status,
            merchant_ref=command.merchant_ref or channel.merchant_ref,
            settlement_iban=command.settlement_iban or channel.settlement_iban,
            environment=command.environment or channel.environment,
            notes=command.notes or channel.notes,
            created_at=channel.created_at,
            updated_at=NOW,
        )
        self.channels[(command.installation_id, command.channel_key)] = updated
        return updated

    def delete_channel(self, installation_id: str, channel_key: str) -> bool:
        return self.channels.pop((installation_id, channel_key), None) is not None

    def rotate_channel_credentials(
        self, command: RotateChannelCredentials
    ) -> PaymentChannel | None:
        channel = self.channels.get((command.installation_id, command.channel_key))
        if channel is None:
            return None
        self.channel_ciphertexts[channel.id] = command.credentials_ciphertext
        updated = PaymentChannel(
            id=channel.id,
            installation_id=channel.installation_id,
            channel_key=channel.channel_key,
            display_name_fa=channel.display_name_fa,
            channel_type=channel.channel_type,
            is_builtin=channel.is_builtin,
            enabled=channel.enabled,
            status=channel.status,
            merchant_ref=channel.merchant_ref,
            settlement_iban=channel.settlement_iban,
            environment=channel.environment,
            notes=channel.notes,
            has_credentials=True,
            created_at=channel.created_at,
            updated_at=NOW,
        )
        self.channels[(command.installation_id, command.channel_key)] = updated
        return updated

    def get_channel_ciphertext(self, channel_id: str) -> str | None:
        return self.channel_ciphertexts.get(channel_id)

    def seed_builtin_channels(self, installation_id: str) -> Sequence[PaymentChannel]:
        for seed in BUILTIN_CHANNELS:
            key = (installation_id, seed.channel_key)
            if key not in self.channels:
                self.channels[key] = PaymentChannel(
                    id=_new_id(),
                    installation_id=installation_id,
                    channel_key=seed.channel_key,
                    display_name_fa=seed.display_name_fa,
                    channel_type=seed.channel_type,
                    is_builtin=True,
                    enabled=False,
                    status=ChannelStatus(seed.status),
                    notes=seed.notes,
                    created_at=NOW,
                    updated_at=NOW,
                )
        return self.list_channels(installation_id)

    # -- contracts --------------------------------------------------------

    def list_contracts(self, channel_id: str) -> Sequence[GatewayContract]:
        return sorted(
            (c for c in self.contracts.values() if c.channel_id == channel_id),
            key=lambda c: c.version,
            reverse=True,
        )

    def get_contract(self, channel_id: str, version: int) -> GatewayContract | None:
        for contract in self.contracts.values():
            if contract.channel_id == channel_id and contract.version == version:
                return contract
        return None

    def get_contract_by_id(self, contract_id: str) -> GatewayContract | None:
        return self.contracts.get(contract_id)

    def get_active_contract(self, channel_id: str) -> GatewayContract | None:
        actives = [
            c
            for c in self.contracts.values()
            if c.channel_id == channel_id and c.status == ContractStatus.ACTIVE
        ]
        return max(actives, key=lambda c: c.version) if actives else None

    def get_active_contract_for_channel_key(
        self, installation_id: str, channel_key: str
    ) -> GatewayContract | None:
        channel = self.get_channel(installation_id, channel_key)
        if channel is None:
            return None
        return self.get_active_contract(channel.id)

    def save_contract(self, command: CreateGatewayContract) -> GatewayContract:
        channel = self.get_channel_by_id(command.channel_id)
        assert channel is not None
        contract = GatewayContract(
            id=_new_id(),
            channel_id=command.channel_id,
            channel_key=channel.channel_key,
            version=command.version,
            valid_from=command.valid_from,
            valid_to=None,
            fee_percent=command.fee_percent,
            fee_vat_rate=command.fee_vat_rate,
            fee_vat_claimable=command.fee_vat_claimable,
            fee_timing=command.fee_timing,
            fee_accrual_policy=command.fee_accrual_policy,
            settlement_model=command.settlement_model,
            tranche_legs=command.tranche_legs,
            payout_window_start_day=command.payout_window_start_day,
            payout_window_end_day=command.payout_window_end_day,
            payout_calendar=command.payout_calendar,
            working_day_adjustment=command.working_day_adjustment,
            day_lag=command.day_lag,
            grace_days_after_due=command.grace_days_after_due,
            match_tolerance_irr=command.match_tolerance_irr,
            tieout_tolerance_irr=command.tieout_tolerance_irr,
            auto_post_policy=command.auto_post_policy,
            auto_post_max_amount_irr=command.auto_post_max_amount_irr,
            source_note=command.source_note,
            status=ContractStatus.ACTIVE,
            created_at=NOW,
            updated_at=NOW,
        )
        self.contracts[contract.id] = contract
        return contract

    def supersede_contract(self, contract_id: str, valid_to: datetime.date) -> None:
        contract = self.contracts.get(contract_id)
        if contract is None:
            return
        self.contracts[contract_id] = GatewayContract(
            **{**contract.__dict__, "status": ContractStatus.SUPERSEDED, "valid_to": valid_to}
        )

    def count_contracts(self, channel_id: str) -> int:
        return sum(1 for c in self.contracts.values() if c.channel_id == channel_id)

    # -- mappings ---------------------------------------------------------

    def list_mappings(
        self,
        installation_id: str,
        *,
        mapping_kind: MappingKind | None = None,
        channel_key: str | None = None,
        source_key: str | None = None,
        status: str | None = None,
    ) -> Sequence[IntegrationMapping]:
        out = [m for m in self.mappings.values() if m.installation_id == installation_id]
        if mapping_kind is not None:
            out = [m for m in out if m.mapping_kind == mapping_kind]
        if channel_key is not None:
            out = [m for m in out if m.channel_key == channel_key]
        if source_key is not None:
            out = [m for m in out if m.source_key == source_key]
        if status is not None:
            out = [m for m in out if m.status.value == status]
        return out

    def get_mapping(self, mapping_id: str) -> IntegrationMapping | None:
        return self.mappings.get(mapping_id)

    def resolve_mapping(
        self,
        installation_id: str,
        mapping_kind: MappingKind,
        channel_key: str,
        source_key: str,
    ) -> IntegrationMapping | None:
        actives = [
            m
            for m in self.mappings.values()
            if m.installation_id == installation_id
            and m.mapping_kind == mapping_kind
            and m.channel_key == channel_key
            and m.source_key == source_key
            and m.status == MappingStatus.ACTIVE
        ]
        return max(actives, key=lambda m: m.version) if actives else None

    def save_mapping(self, command: CreateIntegrationMapping) -> IntegrationMapping:
        mapping = IntegrationMapping(
            id=_new_id(),
            installation_id=command.installation_id,
            mapping_kind=command.mapping_kind,
            channel_key=command.channel_key,
            source_key=command.source_key,
            target_type=command.target_type,
            target_ref=command.target_ref,
            version=command.version,
            status=MappingStatus.ACTIVE,
            suggestion_mode=command.suggestion_mode,
            source=command.source,
            confidence=command.confidence,
            notes=command.notes,
            created_by=command.created_by,
            confirmations=command.confirmations,
            created_at=NOW,
            updated_at=NOW,
        )
        self.mappings[mapping.id] = mapping
        return mapping

    def supersede_mapping(self, mapping_id: str) -> None:
        mapping = self.mappings.get(mapping_id)
        if mapping is None:
            return
        self.mappings[mapping_id] = IntegrationMapping(
            **{**mapping.__dict__, "status": MappingStatus.SUPERSEDED}
        )

    def count_mappings_for_channel(self, installation_id: str, channel_key: str) -> int:
        return sum(
            1
            for m in self.mappings.values()
            if m.installation_id == installation_id and m.channel_key == channel_key
        )

    # -- suggestions ------------------------------------------------------

    def list_suggestions(
        self,
        installation_id: str,
        *,
        status: SuggestionStatus | None = None,
    ) -> Sequence[MappingSuggestion]:
        out = [s for s in self.suggestions.values() if s.installation_id == installation_id]
        if status is not None:
            out = [s for s in out if s.status == status]
        return out

    def get_suggestion(self, suggestion_id: str) -> MappingSuggestion | None:
        return self.suggestions.get(suggestion_id)

    def get_pending_suggestion(
        self,
        installation_id: str,
        mapping_kind: MappingKind,
        channel_key: str,
        source_key: str,
    ) -> MappingSuggestion | None:
        for suggestion in self.suggestions.values():
            if (
                suggestion.installation_id == installation_id
                and suggestion.mapping_kind == mapping_kind
                and suggestion.channel_key == channel_key
                and suggestion.source_key == source_key
                and suggestion.status == SuggestionStatus.PENDING
            ):
                return suggestion
        return None

    def save_suggestion(self, command: CreateMappingSuggestion) -> MappingSuggestion:
        suggestion = MappingSuggestion(
            id=_new_id(),
            installation_id=command.installation_id,
            mapping_kind=command.mapping_kind,
            channel_key=command.channel_key,
            source_key=command.source_key,
            suggested_target_type=command.suggested_target_type,
            suggested_target_ref=command.suggested_target_ref,
            confidence=command.confidence,
            basis=command.basis,
            status=SuggestionStatus.PENDING,
            created_at=NOW,
            updated_at=NOW,
        )
        self.suggestions[suggestion.id] = suggestion
        return suggestion

    def resolve_suggestion(
        self,
        suggestion_id: str,
        status: SuggestionStatus,
        resolved_by: str | None,
    ) -> MappingSuggestion | None:
        suggestion = self.suggestions.get(suggestion_id)
        if suggestion is None:
            return None
        resolved = MappingSuggestion(
            **{**suggestion.__dict__, "status": status, "resolved_by": resolved_by}
        )
        self.suggestions[suggestion_id] = resolved
        return resolved

    # -- policy gate config -----------------------------------------------

    def list_policy_configs(self, installation_id: str) -> Sequence[PolicyGateConfig]:
        return sorted(
            (p for (inst, _), p in self.policies.items() if inst == installation_id),
            key=lambda p: p.event_type,
        )

    def get_policy_config(
        self, installation_id: str, event_type: str
    ) -> PolicyGateConfig | None:
        return self.policies.get((installation_id, event_type))

    def save_policy_config(self, command: UpsertPolicyGateConfig) -> PolicyGateConfig:
        key = (command.installation_id, command.event_type)
        existing = self.policies.get(key)
        config = PolicyGateConfig(
            id=existing.id if existing else _new_id(),
            installation_id=command.installation_id,
            event_type=command.event_type,
            allowed=command.allowed,
            max_amount_irr=command.max_amount_irr,
            require_tieout=command.require_tieout,
            created_at=existing.created_at if existing else NOW,
            updated_at=NOW,
        )
        self.policies[key] = config
        return config

    # -- bank connections -------------------------------------------------

    def list_bank_connections(
        self, installation_id: str, *, company_id: str | None = None
    ) -> Sequence[BankConnection]:
        out = [
            c for c in self.bank_connections.values() if c.installation_id == installation_id
        ]
        if company_id is not None:
            out = [c for c in out if c.company_id == company_id]
        return sorted(out, key=lambda c: c.bank_key)

    def get_bank_connection(
        self, installation_id: str, connection_id: str
    ) -> BankConnection | None:
        connection = self.bank_connections.get(connection_id)
        if connection is None or connection.installation_id != installation_id:
            return None
        return connection

    def save_bank_connection(self, command: CreateBankConnection) -> BankConnection:
        connection = BankConnection(
            id=_new_id(),
            installation_id=command.installation_id,
            company_id=command.company_id,
            bank_key=command.bank_key,
            display_name=command.display_name,
            connection_kind=command.connection_kind,
            has_credentials=command.credentials_ciphertext is not None,
            credentials_key_id=command.credentials_key_id,
            status=command.status,
            notes=command.notes,
            created_at=NOW,
            updated_at=NOW,
        )
        self.bank_connections[connection.id] = connection
        if command.credentials_ciphertext is not None:
            self.bank_ciphertexts[connection.id] = command.credentials_ciphertext
        return connection

    def update_bank_connection(self, command: UpdateBankConnection) -> BankConnection | None:
        connection = self.get_bank_connection(command.installation_id, command.connection_id)
        if connection is None:
            return None
        updated = BankConnection(
            **{
                **connection.__dict__,
                "display_name": command.display_name or connection.display_name,
                "connection_kind": command.connection_kind or connection.connection_kind,
                "status": command.status or connection.status,
                "notes": command.notes or connection.notes,
            }
        )
        self.bank_connections[connection.id] = updated
        return updated

    def rotate_bank_connection_credentials(
        self, command: RotateBankConnectionCredentials
    ) -> BankConnection | None:
        connection = self.get_bank_connection(command.installation_id, command.connection_id)
        if connection is None:
            return None
        self.bank_ciphertexts[connection.id] = command.credentials_ciphertext
        updated = BankConnection(
            **{
                **connection.__dict__,
                "has_credentials": True,
                "credentials_key_id": command.credentials_key_id,
            }
        )
        self.bank_connections[connection.id] = updated
        return updated

    def delete_bank_connection(self, installation_id: str, connection_id: str) -> bool:
        connection = self.get_bank_connection(installation_id, connection_id)
        if connection is None:
            return False
        del self.bank_connections[connection_id]
        self.bank_ciphertexts.pop(connection_id, None)
        return True

    def get_bank_connection_ciphertext(self, connection_id: str) -> str | None:
        return self.bank_ciphertexts.get(connection_id)

    # -- stores -----------------------------------------------------------

    def list_stores(self, installation_id: str) -> Sequence[Store]:
        return sorted(
            (s for (inst, _), s in self.stores.items() if inst == installation_id),
            key=lambda s: s.store_key,
        )

    def get_store(self, installation_id: str, store_key: str) -> Store | None:
        return self.stores.get((installation_id, store_key))

    def save_store(self, command: CreateStore) -> Store:
        store = Store(
            id=_new_id(),
            installation_id=command.installation_id,
            store_key=command.store_key,
            display_name=command.display_name,
            store_type=command.store_type,
            prices_in_toman=command.prices_in_toman,
            has_webhook_secret=True,
            has_api_key=True,
            api_key_scopes=command.api_key_scopes,
            api_base_url=command.api_base_url,
            status=command.status,
            notes=command.notes,
            created_at=NOW,
            updated_at=NOW,
        )
        self.stores[(command.installation_id, command.store_key)] = store
        self.store_ciphertexts[(store.id, "webhook_secret")] = command.webhook_secret_ciphertext
        self.store_ciphertexts[(store.id, "api_key")] = command.api_key_ciphertext
        return store

    def update_store(self, command: UpdateStore) -> Store | None:
        store = self.stores.get((command.installation_id, command.store_key))
        if store is None:
            return None
        updated = Store(
            **{
                **store.__dict__,
                "display_name": command.display_name or store.display_name,
                "store_type": command.store_type or store.store_type,
                "prices_in_toman": (
                    command.prices_in_toman
                    if command.prices_in_toman is not None
                    else store.prices_in_toman
                ),
                "api_key_scopes": (
                    command.api_key_scopes
                    if command.api_key_scopes is not None
                    else store.api_key_scopes
                ),
                "api_base_url": command.api_base_url or store.api_base_url,
                "status": command.status or store.status,
                "notes": command.notes or store.notes,
            }
        )
        self.stores[(command.installation_id, command.store_key)] = updated
        return updated

    def rotate_store_webhook_secret(self, command: RotateStoreWebhookSecret) -> Store | None:
        store = self.stores.get((command.installation_id, command.store_key))
        if store is None:
            return None
        self.store_ciphertexts[(store.id, "webhook_secret")] = command.webhook_secret_ciphertext
        return store

    def rotate_store_api_key(self, command: RotateStoreApiKey) -> Store | None:
        store = self.stores.get((command.installation_id, command.store_key))
        if store is None:
            return None
        self.store_ciphertexts[(store.id, "api_key")] = command.api_key_ciphertext
        updated = Store(**{**store.__dict__, "api_key_scopes": command.api_key_scopes})
        self.stores[(command.installation_id, command.store_key)] = updated
        return updated

    def delete_store(self, installation_id: str, store_key: str) -> bool:
        return self.stores.pop((installation_id, store_key), None) is not None

    def get_store_ciphertext(self, store_id: str, kind: str) -> str | None:
        return self.store_ciphertexts.get((store_id, kind))

    def set_store_db_dsn(self, command: SetStoreDbDsn) -> Store | None:
        store = self.stores.get((command.installation_id, command.store_key))
        if store is None:
            return None
        self.store_ciphertexts[(store.id, "store_db_dsn")] = command.dsn_ciphertext
        updated = Store(**{**store.__dict__, "has_store_db_dsn": True})
        self.stores[(command.installation_id, command.store_key)] = updated
        return updated

    # -- settings + holidays ----------------------------------------------

    def get_installation_settings(self, installation_id: str) -> InstallationSettings | None:
        return self.settings.get(installation_id)

    def save_installation_settings(
        self, command: UpsertInstallationSettings
    ) -> InstallationSettings:
        settings = InstallationSettings(
            installation_id=command.installation_id,
            weekly_holiday=command.weekly_holiday,
            matching_window_days=command.matching_window_days,
            auto_post_enabled=command.auto_post_enabled,
            created_at=NOW,
            updated_at=NOW,
        )
        self.settings[command.installation_id] = settings
        return settings

    def list_holidays(self, installation_id: str) -> Sequence[Holiday]:
        return sorted(
            (h for (inst, _), h in self.holidays.items() if inst == installation_id),
            key=lambda h: h.holiday_date,
        )

    def add_holiday(self, command: AddHoliday) -> Holiday:
        holiday = Holiday(
            id=_new_id(),
            installation_id=command.installation_id,
            holiday_date=command.holiday_date,
            title=command.title,
            created_at=NOW,
        )
        self.holidays[(command.installation_id, command.holiday_date)] = holiday
        return holiday

    def delete_holiday(self, installation_id: str, holiday_date: datetime.date) -> bool:
        return self.holidays.pop((installation_id, holiday_date), None) is not None


class StubFiscalChecker:
    """Fiscal-period checker stub with a configurable verdict."""

    def __init__(self, period_open: bool = True) -> None:
        self.period_open = period_open
        self.calls: list[tuple[CompanyRef, datetime.date]] = []

    def is_period_open(self, company: CompanyRef, on: datetime.date) -> bool:
        self.calls.append((company, on))
        return self.period_open


@pytest.fixture()
def repo() -> FakeIntegrationsRepository:
    return FakeIntegrationsRepository()


@pytest.fixture()
def vault() -> FernetCredentialVault:
    return FernetCredentialVault(generate_encryption_key())


def _make_channel(
    repo: FakeIntegrationsRepository,
    key: str = "zarinpal",
    *,
    installation_id: str = INSTALLATION_ID,
) -> PaymentChannel:
    return ChannelRegistryService(repo).create_channel(
        installation_id, key, "زرین‌پال", "gateway"
    )


def _make_contract(
    repo: FakeIntegrationsRepository,
    channel_key: str = "zarinpal",
    *,
    installation_id: str = INSTALLATION_ID,
    valid_from: datetime.date = datetime.date(2026, 1, 1),
    fee_percent: Decimal = Decimal("2"),
    **overrides: object,
) -> GatewayContract:
    return GatewayContractService(repo).create_version(
        installation_id,
        channel_key,
        valid_from=valid_from,
        fee_percent=fee_percent,
        tranche_legs=FOUR_TRANCHES,
        **overrides,  # type: ignore[arg-type]
    )


# -- channel registry -----------------------------------------------------


class TestChannelRegistryService:
    def test_create_channel_is_born_disabled_in_setup(self, repo) -> None:  # type: ignore[no-untyped-def]
        channel = _make_channel(repo, "custompay")
        assert channel.channel_key == "custompay"
        assert channel.is_builtin is False
        assert channel.enabled is False
        assert channel.status == ChannelStatus.SETUP

    def test_create_duplicate_key_conflicts(self, repo) -> None:  # type: ignore[no-untyped-def]
        _make_channel(repo, "custompay")
        with pytest.raises(IntegrationChannelKeyConflictError):
            _make_channel(repo, "custompay")

    def test_same_key_in_another_installation_is_fine(self, repo) -> None:  # type: ignore[no-untyped-def]
        _make_channel(repo, "custompay", installation_id=INSTALLATION_ID)
        other = _make_channel(repo, "custompay", installation_id=OTHER_INSTALLATION_ID)
        assert other.installation_id == OTHER_INSTALLATION_ID

    def test_create_rejects_bad_key(self, repo) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(IntegrationMappingInvalidError):
            _make_channel(repo, "Bad Key!")

    def test_enable_without_contract_stays_in_setup(self, repo) -> None:  # type: ignore[no-untyped-def]
        _make_channel(repo)
        updated = ChannelRegistryService(repo).update_channel(
            INSTALLATION_ID, "zarinpal", enabled=True
        )
        assert updated.enabled is True
        assert updated.status == ChannelStatus.SETUP

    def test_enable_with_contract_becomes_active(self, repo) -> None:  # type: ignore[no-untyped-def]
        _make_channel(repo)
        _make_contract(repo)
        updated = ChannelRegistryService(repo).update_channel(
            INSTALLATION_ID, "zarinpal", enabled=True
        )
        assert updated.enabled is True
        assert updated.status == ChannelStatus.ACTIVE

    def test_disable_sets_disabled_status(self, repo) -> None:  # type: ignore[no-untyped-def]
        _make_channel(repo)
        _make_contract(repo)
        service = ChannelRegistryService(repo)
        service.update_channel(INSTALLATION_ID, "zarinpal", enabled=True)
        updated = service.update_channel(INSTALLATION_ID, "zarinpal", enabled=False)
        assert updated.enabled is False
        assert updated.status == ChannelStatus.DISABLED

    def test_update_unknown_channel_not_found(self, repo) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(IntegrationChannelNotFoundError):
            ChannelRegistryService(repo).update_channel(
                INSTALLATION_ID, "ghost", enabled=True
            )

    def test_delete_clean_channel(self, repo) -> None:  # type: ignore[no-untyped-def]
        _make_channel(repo)
        ChannelRegistryService(repo).delete_channel(INSTALLATION_ID, "zarinpal")
        assert repo.get_channel(INSTALLATION_ID, "zarinpal") is None

    def test_delete_channel_with_contract_refused(self, repo) -> None:  # type: ignore[no-untyped-def]
        _make_channel(repo)
        _make_contract(repo)
        with pytest.raises(IntegrationChannelInUseError) as excinfo:
            ChannelRegistryService(repo).delete_channel(INSTALLATION_ID, "zarinpal")
        assert excinfo.value.details["contracts"] == 1

    def test_delete_channel_with_mapping_refused(self, repo) -> None:  # type: ignore[no-untyped-def]
        _make_channel(repo)
        MappingService(repo).create_mapping(
            INSTALLATION_ID,
            mapping_kind=MappingKind.GATEWAY_JOURNAL,
            channel_key="zarinpal",
            source_key="zarinpal",
            target_type="journal",
            target_ref="J-1",
        )
        with pytest.raises(IntegrationChannelInUseError) as excinfo:
            ChannelRegistryService(repo).delete_channel(INSTALLATION_ID, "zarinpal")
        assert excinfo.value.details["mappings"] == 1

    def test_seed_creates_eight_builtin_channels(self, repo) -> None:  # type: ignore[no-untyped-def]
        channels = ChannelRegistryService(repo).seed_builtin_channels(INSTALLATION_ID)
        assert len(channels) == 8
        keys = {c.channel_key for c in channels}
        assert keys == {
            "zarinpal",
            "sadad",
            "snapppay",
            "digipay",
            "torobpay",
            "salampay",
            "basalam_marketplace",
            "torob_marketplace",
        }
        assert all(c.is_builtin for c in channels)
        assert all(not c.enabled for c in channels)

    def test_seed_torob_marketplace_is_reserved(self, repo) -> None:  # type: ignore[no-untyped-def]
        """torob_marketplace is RESERVED: seeded disabled, never enableable (US-524)."""
        channels = ChannelRegistryService(repo).seed_builtin_channels(INSTALLATION_ID)
        reserved = next(c for c in channels if c.channel_key == "torob_marketplace")
        assert reserved.status == ChannelStatus.DISABLED
        assert reserved.notes is not None and "RESERVED" in reserved.notes
        with pytest.raises(IntegrationChannelReservedError):
            ChannelRegistryService(repo).update_channel(
                INSTALLATION_ID, "torob_marketplace", enabled=True
            )

    def test_ilmaz_is_not_a_builtin_channel(self, repo) -> None:  # type: ignore[no-untyped-def]
        """Ilmaz is not a payment channel (ADR 0003 round-2 item 2)."""
        channels = ChannelRegistryService(repo).seed_builtin_channels(INSTALLATION_ID)
        keys = {c.channel_key for c in channels}
        assert "ilmaz" not in keys
        assert "emalls" not in keys

    def test_seed_is_idempotent(self, repo) -> None:  # type: ignore[no-untyped-def]
        first = ChannelRegistryService(repo).seed_builtin_channels(INSTALLATION_ID)
        second = ChannelRegistryService(repo).seed_builtin_channels(INSTALLATION_ID)
        assert len(second) == 8
        assert {c.id for c in first} == {c.id for c in second}


# -- gateway contracts ----------------------------------------------------


class TestGatewayContractService:
    def test_create_first_version(self, repo) -> None:  # type: ignore[no-untyped-def]
        _make_channel(repo)
        contract = _make_contract(repo)
        assert contract.version == 1
        assert contract.status == ContractStatus.ACTIVE
        assert contract.valid_to is None
        assert contract.fee_percent == Decimal("2")
        assert contract.tranche_legs == FOUR_TRANCHES

    def test_rate_change_creates_new_version_and_supersedes(self, repo) -> None:  # type: ignore[no-untyped-def]
        """A rate change is a NEW version; history is never rewritten."""
        _make_channel(repo)
        v1 = _make_contract(repo, fee_percent=Decimal("2"))
        v2 = _make_contract(
            repo,
            valid_from=datetime.date(2026, 7, 1),
            fee_percent=Decimal("2.5"),
        )
        assert v2.version == 2
        assert v2.fee_percent == Decimal("2.5")
        assert v2.status == ContractStatus.ACTIVE
        old = repo.get_contract(v1.channel_id, 1)
        assert old is not None
        assert old.status == ContractStatus.SUPERSEDED
        assert old.fee_percent == Decimal("2")  # history intact
        assert old.valid_to == datetime.date(2026, 6, 30)
        assert repo.get_active_contract(v1.channel_id).version == 2

    def test_create_for_unknown_channel_not_found(self, repo) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(IntegrationChannelNotFoundError):
            _make_contract(repo, channel_key="ghost")

    def test_invalid_tranche_legs_rejected(self, repo) -> None:  # type: ignore[no-untyped-def]
        _make_channel(repo)
        with pytest.raises(IntegrationContractInvalidError) as excinfo:
            GatewayContractService(repo).create_version(
                INSTALLATION_ID,
                "zarinpal",
                valid_from=datetime.date(2026, 1, 1),
                fee_percent=Decimal("2"),
                tranche_legs=(TrancheLeg(1, 5, Decimal("50")),),
            )
        assert "sum to exactly 100" in str(excinfo.value)

    def test_fee_percent_out_of_range_rejected(self, repo) -> None:  # type: ignore[no-untyped-def]
        _make_channel(repo)
        with pytest.raises(IntegrationContractInvalidError):
            _make_contract(repo, fee_percent=Decimal("101"))

    def test_negative_tolerance_rejected(self, repo) -> None:  # type: ignore[no-untyped-def]
        _make_channel(repo)
        with pytest.raises(IntegrationContractInvalidError):
            _make_contract(repo, match_tolerance_irr=-1)

    def test_valid_from_must_move_forward(self, repo) -> None:  # type: ignore[no-untyped-def]
        _make_channel(repo)
        _make_contract(repo, valid_from=datetime.date(2026, 7, 1))
        with pytest.raises(IntegrationContractInvalidError):
            _make_contract(repo, valid_from=datetime.date(2026, 7, 1))

    def test_payout_window_must_be_ordered(self, repo) -> None:  # type: ignore[no-untyped-def]
        _make_channel(repo)
        with pytest.raises(IntegrationContractInvalidError):
            _make_contract(
                repo, payout_window_start_day=5, payout_window_end_day=1
            )


# -- mapping center -------------------------------------------------------


class TestMappingService:
    def test_create_first_version(self, repo) -> None:  # type: ignore[no-untyped-def]
        mapping = MappingService(repo).create_mapping(
            INSTALLATION_ID,
            mapping_kind=MappingKind.REVENUE_CATEGORY,
            source_key="coffee",
            target_type="account",
            target_ref="4101",
        )
        assert mapping.version == 1
        assert mapping.status == MappingStatus.ACTIVE
        assert mapping.channel_key == ""

    def test_create_new_version_supersedes(self, repo) -> None:  # type: ignore[no-untyped-def]
        service = MappingService(repo)
        v1 = service.create_mapping(
            INSTALLATION_ID,
            mapping_kind=MappingKind.REVENUE_CATEGORY,
            source_key="coffee",
            target_type="account",
            target_ref="4101",
        )
        v2 = service.create_mapping(
            INSTALLATION_ID,
            mapping_kind=MappingKind.REVENUE_CATEGORY,
            source_key="coffee",
            target_type="account",
            target_ref="4102",
        )
        assert v2.version == 2
        assert repo.get_mapping(v1.id).status == MappingStatus.SUPERSEDED
        assert repo.resolve_mapping(
            INSTALLATION_ID, MappingKind.REVENUE_CATEGORY, "", "coffee"
        ).target_ref == "4102"

    def test_resolve_missing_mapping_not_found(self, repo) -> None:  # type: ignore[no-untyped-def]
        """Unknown mapping fails closed — the fail-closed signal for the pipeline."""
        with pytest.raises(IntegrationMappingNotFoundError):
            MappingService(repo).resolve_mapping(
                INSTALLATION_ID, MappingKind.REVENUE_CATEGORY, "", "ghost"
            )

    def test_mapping_to_unknown_channel_not_found(self, repo) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(IntegrationChannelNotFoundError):
            MappingService(repo).create_mapping(
                INSTALLATION_ID,
                mapping_kind=MappingKind.GATEWAY_JOURNAL,
                channel_key="ghost",
                source_key="ghost",
                target_type="journal",
                target_ref="J-1",
            )

    def test_bad_target_type_rejected(self, repo) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(IntegrationMappingInvalidError):
            MappingService(repo).create_mapping(
                INSTALLATION_ID,
                mapping_kind=MappingKind.REVENUE_CATEGORY,
                source_key="coffee",
                target_type="spaceship",
                target_ref="4101",
            )

    def test_suggestion_create_and_idempotent_recreate(self, repo) -> None:  # type: ignore[no-untyped-def]
        service = MappingService(repo)
        first = service.create_suggestion(
            INSTALLATION_ID,
            mapping_kind=MappingKind.EXTERNAL_PARTY,
            source_key="09120000000",
            suggested_target_type="party",
            suggested_target_ref="P-7",
            confidence=Decimal("0.9"),
            basis="learned_from_manual_mapping",
        )
        second = service.create_suggestion(
            INSTALLATION_ID,
            mapping_kind=MappingKind.EXTERNAL_PARTY,
            source_key="09120000000",
            suggested_target_type="party",
            suggested_target_ref="P-7",
            confidence=Decimal("0.9"),
            basis="learned_from_manual_mapping",
        )
        assert first.id == second.id
        assert len(repo.list_suggestions(INSTALLATION_ID)) == 1

    def test_accept_suggestion_creates_mapping_in_suggest_mode(self, repo) -> None:  # type: ignore[no-untyped-def]
        service = MappingService(repo)
        suggestion = service.create_suggestion(
            INSTALLATION_ID,
            mapping_kind=MappingKind.EXTERNAL_PARTY,
            source_key="09120000000",
            suggested_target_type="party",
            suggested_target_ref="P-7",
            confidence=Decimal("0.9"),
            basis="learned_from_manual_mapping",
        )
        mapping = service.accept_suggestion(INSTALLATION_ID, suggestion.id)
        assert mapping.source_key == "09120000000"
        assert mapping.target_ref == "P-7"
        assert mapping.suggestion_mode == SuggestionMode.SUGGEST
        assert mapping.source == "learned"
        assert mapping.confidence == Decimal("0.9")
        assert repo.get_suggestion(suggestion.id).status == SuggestionStatus.ACCEPTED

    def test_reject_suggestion(self, repo) -> None:  # type: ignore[no-untyped-def]
        service = MappingService(repo)
        suggestion = service.create_suggestion(
            INSTALLATION_ID,
            mapping_kind=MappingKind.EXTERNAL_PARTY,
            source_key="09120000000",
            suggested_target_type="party",
            suggested_target_ref="P-7",
            confidence=Decimal("0.4"),
            basis="name_match",
        )
        rejected = service.reject_suggestion(INSTALLATION_ID, suggestion.id)
        assert rejected.status == SuggestionStatus.REJECTED

    def test_double_resolve_rejected(self, repo) -> None:  # type: ignore[no-untyped-def]
        service = MappingService(repo)
        suggestion = service.create_suggestion(
            INSTALLATION_ID,
            mapping_kind=MappingKind.EXTERNAL_PARTY,
            source_key="09120000000",
            suggested_target_type="party",
            suggested_target_ref="P-7",
            confidence=Decimal("0.9"),
            basis="learned_from_manual_mapping",
        )
        service.reject_suggestion(INSTALLATION_ID, suggestion.id)
        with pytest.raises(IntegrationMappingSuggestionResolvedError):
            service.accept_suggestion(INSTALLATION_ID, suggestion.id)

    def test_accept_unknown_suggestion_not_found(self, repo) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(IntegrationMappingSuggestionNotFoundError):
            MappingService(repo).accept_suggestion(INSTALLATION_ID, "ghost")

    # -- US-552: confirmations + Suggest → Auto promotion ------------------

    def test_first_mapping_starts_with_one_confirmation(self, repo) -> None:  # type: ignore[no-untyped-def]
        mapping = MappingService(repo).create_mapping(
            INSTALLATION_ID,
            mapping_kind=MappingKind.REVENUE_CATEGORY,
            source_key="coffee",
            target_type="account",
            target_ref="4101",
        )
        assert mapping.confirmations == 1
        assert mapping.auto_eligible is False

    def test_same_target_reconfirmation_increments_confirmations(self, repo) -> None:  # type: ignore[no-untyped-def]
        service = MappingService(repo)
        versions = [
            service.create_mapping(
                INSTALLATION_ID,
                mapping_kind=MappingKind.REVENUE_CATEGORY,
                source_key="coffee",
                target_type="account",
                target_ref="4101",
            )
            for _ in range(3)
        ]
        assert [mapping.confirmations for mapping in versions] == [1, 2, 3]
        assert versions[-1].auto_eligible is True

    def test_changed_target_resets_confirmations(self, repo) -> None:  # type: ignore[no-untyped-def]
        service = MappingService(repo)
        service.create_mapping(
            INSTALLATION_ID,
            mapping_kind=MappingKind.REVENUE_CATEGORY,
            source_key="coffee",
            target_type="account",
            target_ref="4101",
        )
        service.create_mapping(
            INSTALLATION_ID,
            mapping_kind=MappingKind.REVENUE_CATEGORY,
            source_key="coffee",
            target_type="account",
            target_ref="4101",
        )
        changed = service.create_mapping(
            INSTALLATION_ID,
            mapping_kind=MappingKind.REVENUE_CATEGORY,
            source_key="coffee",
            target_type="account",
            target_ref="4102",
        )
        assert changed.confirmations == 1
        assert changed.auto_eligible is False

    def test_accept_suggestion_twice_increments_confirmations(self, repo) -> None:  # type: ignore[no-untyped-def]
        """Accepting the same learned mapping twice is a re-confirmation."""
        service = MappingService(repo)
        mapping = None
        for _ in range(2):
            suggestion = service.create_suggestion(
                INSTALLATION_ID,
                mapping_kind=MappingKind.EXTERNAL_PARTY,
                source_key="09120000000",
                suggested_target_type="party",
                suggested_target_ref="P-7",
                confidence=Decimal("0.9"),
                basis="learned_from_manual_mapping",
            )
            mapping = service.accept_suggestion(INSTALLATION_ID, suggestion.id)
        assert mapping is not None
        assert mapping.confirmations == 2
        assert mapping.suggestion_mode == SuggestionMode.SUGGEST

    def test_promote_at_threshold_creates_auto_version(self, repo) -> None:  # type: ignore[no-untyped-def]
        service = MappingService(repo)
        mapping = None
        for _ in range(MAPPING_AUTO_MIN_CONFIRMATIONS):
            mapping = service.create_mapping(
                INSTALLATION_ID,
                mapping_kind=MappingKind.REVENUE_CATEGORY,
                source_key="coffee",
                target_type="account",
                target_ref="4101",
            )
        assert mapping is not None
        assert mapping.auto_eligible is True
        promoted = service.promote_mapping(INSTALLATION_ID, mapping.id, promoted_by="operator")
        assert promoted.suggestion_mode == SuggestionMode.AUTO
        assert promoted.version == mapping.version + 1
        # Promotion is not a confirmation: the count carries unchanged.
        assert promoted.confirmations == MAPPING_AUTO_MIN_CONFIRMATIONS
        assert promoted.target_ref == "4101"
        assert promoted.created_by == "operator"
        assert repo.get_mapping(mapping.id).status == MappingStatus.SUPERSEDED
        assert (
            repo.resolve_mapping(INSTALLATION_ID, MappingKind.REVENUE_CATEGORY, "", "coffee").id
            == promoted.id
        )

    def test_promote_below_threshold_rejected(self, repo) -> None:  # type: ignore[no-untyped-def]
        service = MappingService(repo)
        mapping = service.create_mapping(
            INSTALLATION_ID,
            mapping_kind=MappingKind.REVENUE_CATEGORY,
            source_key="coffee",
            target_type="account",
            target_ref="4101",
        )
        with pytest.raises(IntegrationMappingNotPromotableError):
            service.promote_mapping(INSTALLATION_ID, mapping.id)
        # The rejection changed nothing: still the active Suggest mapping.
        assert repo.get_mapping(mapping.id).status == MappingStatus.ACTIVE
        assert repo.get_mapping(mapping.id).suggestion_mode == SuggestionMode.SUGGEST

    def test_promote_already_auto_rejected(self, repo) -> None:  # type: ignore[no-untyped-def]
        service = MappingService(repo)
        mapping = None
        for _ in range(MAPPING_AUTO_MIN_CONFIRMATIONS):
            mapping = service.create_mapping(
                INSTALLATION_ID,
                mapping_kind=MappingKind.REVENUE_CATEGORY,
                source_key="coffee",
                target_type="account",
                target_ref="4101",
            )
        assert mapping is not None
        promoted = service.promote_mapping(INSTALLATION_ID, mapping.id)
        with pytest.raises(IntegrationMappingNotPromotableError, match="already auto"):
            service.promote_mapping(INSTALLATION_ID, promoted.id)

    def test_promote_superseded_rejected(self, repo) -> None:  # type: ignore[no-untyped-def]
        service = MappingService(repo)
        old = service.create_mapping(
            INSTALLATION_ID,
            mapping_kind=MappingKind.REVENUE_CATEGORY,
            source_key="coffee",
            target_type="account",
            target_ref="4101",
        )
        service.create_mapping(
            INSTALLATION_ID,
            mapping_kind=MappingKind.REVENUE_CATEGORY,
            source_key="coffee",
            target_type="account",
            target_ref="4102",
        )
        assert repo.get_mapping(old.id).status == MappingStatus.SUPERSEDED
        with pytest.raises(IntegrationMappingNotPromotableError, match="not active"):
            service.promote_mapping(INSTALLATION_ID, old.id)

    def test_promote_unknown_mapping_not_found(self, repo) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(IntegrationMappingNotFoundError):
            MappingService(repo).promote_mapping(INSTALLATION_ID, "ghost")

    def test_promote_other_installation_not_found(self, repo) -> None:  # type: ignore[no-untyped-def]
        service = MappingService(repo)
        mapping = service.create_mapping(
            INSTALLATION_ID,
            mapping_kind=MappingKind.REVENUE_CATEGORY,
            source_key="coffee",
            target_type="account",
            target_ref="4101",
        )
        with pytest.raises(IntegrationMappingNotFoundError):
            service.promote_mapping("other-installation", mapping.id)


# -- policy gate ----------------------------------------------------------


def _gate_setup(
    repo: FakeIntegrationsRepository,
    *,
    auto_post_enabled: bool = True,
    allowed: bool = True,
    max_amount_irr: int | None = 10_000_000,
    channel_key: str | None = None,
    contract_cap: int | None = None,
    contract_policy: AutoPostPolicy = AutoPostPolicy.AUTO_POST_WITHIN_POLICY,
) -> None:
    """Provision the registry rows the gate needs for one scenario."""
    InstallationSettingsService(repo).upsert_settings(
        INSTALLATION_ID, auto_post_enabled=auto_post_enabled
    )
    PolicyGateConfigService(repo).upsert_config(
        INSTALLATION_ID,
        "payment.captured",
        allowed=allowed,
        max_amount_irr=max_amount_irr,
    )
    if channel_key is not None:
        _make_channel(repo, channel_key)
        if contract_cap is not None or contract_policy is not None:
            _make_contract(
                repo,
                channel_key,
                auto_post_policy=contract_policy,
                auto_post_max_amount_irr=contract_cap,
            )
        ChannelRegistryService(repo).update_channel(
            INSTALLATION_ID, channel_key, enabled=True
        )


def _gate_evaluate(
    repo: FakeIntegrationsRepository,
    checker: StubFiscalChecker,
    *,
    amount_irr: int = 1_000_000,
    channel_key: str | None = None,
    event_type: str = "payment.captured",
):  # type: ignore[no-untyped-def]
    return PolicyGateService(repo, checker).evaluate(
        INSTALLATION_ID,
        event_type=event_type,
        company=CompanyRef(1),
        event_date=datetime.date(2026, 11, 18),
        amount_irr=amount_irr,
        channel_key=channel_key,
    )


class TestPolicyGateService:
    def test_all_layers_pass_allows(self, repo) -> None:  # type: ignore[no-untyped-def]
        _gate_setup(repo)
        decision = _gate_evaluate(repo, StubFiscalChecker(period_open=True))
        assert decision.allowed
        assert decision.action == "auto_post"

    def test_unauthorized_event_never_auto_posts(self, repo) -> None:  # type: ignore[no-untyped-def]
        """No policy row for the event type → draft."""
        InstallationSettingsService(repo).upsert_settings(
            INSTALLATION_ID, auto_post_enabled=True
        )
        decision = _gate_evaluate(repo, StubFiscalChecker(period_open=True))
        assert not decision.allowed
        assert decision.action == "draft"

    def test_opt_in_off_never_auto_posts(self, repo) -> None:  # type: ignore[no-untyped-def]
        _gate_setup(repo, auto_post_enabled=False)
        decision = _gate_evaluate(repo, StubFiscalChecker(period_open=True))
        assert not decision.allowed

    def test_over_cap_never_auto_posts(self, repo) -> None:  # type: ignore[no-untyped-def]
        _gate_setup(repo)
        decision = _gate_evaluate(repo, StubFiscalChecker(period_open=True), amount_irr=20_000_000)
        assert not decision.allowed

    def test_closed_period_never_auto_posts(self, repo) -> None:  # type: ignore[no-untyped-def]
        _gate_setup(repo)
        decision = _gate_evaluate(repo, StubFiscalChecker(period_open=False))
        assert not decision.allowed
        layer = next(layer for layer in decision.layers if layer.layer == "period_lock")
        assert not layer.passed

    def test_unknown_channel_never_auto_posts(self, repo) -> None:  # type: ignore[no-untyped-def]
        _gate_setup(repo)
        decision = _gate_evaluate(
            repo, StubFiscalChecker(period_open=True), channel_key="ghost"
        )
        assert not decision.allowed
        layer = next(layer for layer in decision.layers if layer.layer == "channel")
        assert not layer.passed
        assert "not in the registry" in layer.detail

    def test_disabled_channel_never_auto_posts(self, repo) -> None:  # type: ignore[no-untyped-def]
        _gate_setup(repo)
        _make_channel(repo, "zarinpal")  # born disabled
        decision = _gate_evaluate(
            repo, StubFiscalChecker(period_open=True), channel_key="zarinpal"
        )
        assert not decision.allowed
        layer = next(layer for layer in decision.layers if layer.layer == "channel")
        assert "disabled" in layer.detail

    def test_channel_without_contract_never_auto_posts(self, repo) -> None:  # type: ignore[no-untyped-def]
        _gate_setup(repo)
        _make_channel(repo, "zarinpal")
        ChannelRegistryService(repo).update_channel(
            INSTALLATION_ID, "zarinpal", enabled=True
        )
        decision = _gate_evaluate(
            repo, StubFiscalChecker(period_open=True), channel_key="zarinpal"
        )
        assert not decision.allowed
        layer = next(layer for layer in decision.layers if layer.layer == "channel")
        assert "no active contract" in layer.detail

    def test_channel_with_contract_uses_contract_cap(self, repo) -> None:  # type: ignore[no-untyped-def]
        _gate_setup(repo, channel_key="zarinpal", contract_cap=2_000_000)
        over = _gate_evaluate(
            repo, StubFiscalChecker(period_open=True), amount_irr=3_000_000, channel_key="zarinpal"
        )
        assert not over.allowed
        under = _gate_evaluate(
            repo, StubFiscalChecker(period_open=True), amount_irr=1_500_000, channel_key="zarinpal"
        )
        assert under.allowed
        assert under.contract_version == 1

    def test_checker_receives_company_and_date(self, repo) -> None:  # type: ignore[no-untyped-def]
        _gate_setup(repo)
        checker = StubFiscalChecker(period_open=True)
        _gate_evaluate(repo, checker)
        assert checker.calls == [(CompanyRef(1), datetime.date(2026, 11, 18))]


class TestPolicyGateConfigService:
    def test_upsert_creates_then_replaces(self, repo) -> None:  # type: ignore[no-untyped-def]
        service = PolicyGateConfigService(repo)
        first = service.upsert_config(
            INSTALLATION_ID, "payment.captured", allowed=True, max_amount_irr=1_000_000
        )
        second = service.upsert_config(
            INSTALLATION_ID, "payment.captured", allowed=False, max_amount_irr=None
        )
        assert first.id == second.id
        assert second.allowed is False
        assert second.max_amount_irr is None
        assert len(repo.list_policy_configs(INSTALLATION_ID)) == 1

    def test_negative_cap_rejected(self, repo) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(IntegrationMappingInvalidError):
            PolicyGateConfigService(repo).upsert_config(
                INSTALLATION_ID, "payment.captured", allowed=True, max_amount_irr=-5
            )


# -- bank connections -----------------------------------------------------


class TestBankConnectionService:
    def test_create_without_credentials(self, repo, vault) -> None:  # type: ignore[no-untyped-def]
        connection = BankConnectionService(repo, vault).create_connection(
            INSTALLATION_ID,
            company_id=COMPANY_ID,
            bank_key="melli",
            display_name="بانک ملی",
            connection_kind="manual_import",
        )
        assert connection.has_credentials is False
        assert connection.credentials_key_id is None

    def test_create_with_credentials_stores_ciphertext(self, repo, vault) -> None:  # type: ignore[no-untyped-def]
        service = BankConnectionService(repo, vault)
        connection = service.create_connection(
            INSTALLATION_ID,
            company_id=COMPANY_ID,
            bank_key="fintech",
            display_name="فینوتک",
            connection_kind="api_oauth",
            credentials={"client_id": "abc", "client_secret": "shh"},
        )
        assert connection.has_credentials is True
        assert connection.credentials_key_id == vault.key_id
        ciphertext = repo.get_bank_connection_ciphertext(connection.id)
        assert ciphertext is not None
        assert "shh" not in ciphertext  # never stored in plaintext
        assert vault.decrypt(ciphertext) is not None
        import json

        assert json.loads(vault.decrypt(ciphertext)) == {
            "client_id": "abc",
            "client_secret": "shh",
        }

    @pytest.mark.parametrize(
        "field",
        ["password", "internet_banking_password", "bam_password", "Online_Banking_Password"],
    )
    def test_internet_banking_password_never_accepted(self, repo, vault, field) -> None:  # type: ignore[no-untyped-def]
        """ADR 0003 decision 10: never ask for or store the password."""
        with pytest.raises(IntegrationCredentialsForbiddenError) as excinfo:
            BankConnectionService(repo, vault).create_connection(
                INSTALLATION_ID,
                company_id=COMPANY_ID,
                bank_key="melli",
                display_name="بانک ملی",
                connection_kind="api_oauth",
                credentials={field: "secret-value"},
            )
        assert "secret-value" not in str(excinfo.value)  # the value is never echoed
        assert field in str(excinfo.value) or field.lower() in str(excinfo.value).lower()

    def test_credentials_without_vault_unavailable(self, repo) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(IntegrationCredentialVaultUnavailableError):
            BankConnectionService(repo, None).create_connection(
                INSTALLATION_ID,
                company_id=COMPANY_ID,
                bank_key="fintech",
                display_name="فینوتک",
                connection_kind="api_oauth",
                credentials={"client_id": "abc"},
            )

    def test_duplicate_bank_for_company_conflicts(self, repo, vault) -> None:  # type: ignore[no-untyped-def]
        service = BankConnectionService(repo, vault)
        service.create_connection(
            INSTALLATION_ID,
            company_id=COMPANY_ID,
            bank_key="melli",
            display_name="بانک ملی",
            connection_kind="manual_import",
        )
        with pytest.raises(IntegrationBankConnectionConflictError):
            service.create_connection(
                INSTALLATION_ID,
                company_id=COMPANY_ID,
                bank_key="melli",
                display_name="بانک ملی",
                connection_kind="manual_import",
            )

    def test_rotate_credentials_replaces_ciphertext(self, repo, vault) -> None:  # type: ignore[no-untyped-def]
        service = BankConnectionService(repo, vault)
        connection = service.create_connection(
            INSTALLATION_ID,
            company_id=COMPANY_ID,
            bank_key="fintech",
            display_name="فینوتک",
            connection_kind="api_oauth",
            credentials={"client_id": "old"},
        )
        old_ciphertext = repo.get_bank_connection_ciphertext(connection.id)
        rotated = service.rotate_credentials(
            INSTALLATION_ID, connection.id, {"client_id": "new"}
        )
        assert rotated.has_credentials is True
        new_ciphertext = repo.get_bank_connection_ciphertext(connection.id)
        assert new_ciphertext != old_ciphertext
        import json

        assert json.loads(vault.decrypt(new_ciphertext)) == {"client_id": "new"}

    def test_delete_connection(self, repo, vault) -> None:  # type: ignore[no-untyped-def]
        service = BankConnectionService(repo, vault)
        connection = service.create_connection(
            INSTALLATION_ID,
            company_id=COMPANY_ID,
            bank_key="melli",
            display_name="بانک ملی",
            connection_kind="manual_import",
        )
        service.delete_connection(INSTALLATION_ID, connection.id)
        assert repo.get_bank_connection(INSTALLATION_ID, connection.id) is None


# -- stores ---------------------------------------------------------------


class TestStoreService:
    def test_create_store_returns_secrets_once(self, repo, vault) -> None:  # type: ignore[no-untyped-def]
        created = StoreService(repo, vault).create_store(
            INSTALLATION_ID,
            store_key="hayat",
            display_name="حیات",
            store_type="custom",
        )
        assert created.store.store_key == "hayat"
        assert created.store.prices_in_toman is True
        assert created.store.has_webhook_secret is True
        assert created.store.has_api_key is True
        assert created.store.api_key_scopes == (
            "orders:read",
            "payments:read",
            "cheques:read",
        )
        assert created.webhook_secret
        assert created.api_key.startswith("hya_store_")
        assert created.webhook_secret != created.api_key
        # Stored encrypted, never plaintext.
        webhook_ct = repo.get_store_ciphertext(created.store.id, "webhook_secret")
        api_key_ct = repo.get_store_ciphertext(created.store.id, "api_key")
        assert webhook_ct is not None and created.webhook_secret not in webhook_ct
        assert api_key_ct is not None and created.api_key not in api_key_ct
        assert vault.decrypt(webhook_ct) == created.webhook_secret
        assert vault.decrypt(api_key_ct) == created.api_key

    def test_duplicate_store_key_conflicts(self, repo, vault) -> None:  # type: ignore[no-untyped-def]
        service = StoreService(repo, vault)
        service.create_store(INSTALLATION_ID, store_key="hayat", display_name="حیات", store_type="custom")
        with pytest.raises(IntegrationStoreKeyConflictError):
            service.create_store(
                INSTALLATION_ID, store_key="hayat", display_name="حیات", store_type="custom"
            )

    def test_store_scopes_must_be_read_only(self, repo, vault) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(IntegrationMappingInvalidError):
            StoreService(repo, vault).create_store(
                INSTALLATION_ID,
                store_key="hayat",
                display_name="حیات",
                store_type="custom",
                api_key_scopes=["orders:write"],
            )

    def test_create_without_vault_unavailable(self, repo) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(IntegrationCredentialVaultUnavailableError):
            StoreService(repo, None).create_store(
                INSTALLATION_ID, store_key="hayat", display_name="حیات", store_type="custom"
            )

    def test_rotate_webhook_secret(self, repo, vault) -> None:  # type: ignore[no-untyped-def]
        service = StoreService(repo, vault)
        created = service.create_store(
            INSTALLATION_ID, store_key="hayat", display_name="حیات", store_type="custom"
        )
        rotated = service.rotate_webhook_secret(INSTALLATION_ID, "hayat")
        assert rotated.webhook_secret
        assert rotated.webhook_secret != created.webhook_secret
        assert rotated.api_key == ""  # the API key did not change
        assert (
            vault.decrypt(repo.get_store_ciphertext(created.store.id, "webhook_secret"))
            == rotated.webhook_secret
        )

    def test_rotate_api_key_with_new_scopes(self, repo, vault) -> None:  # type: ignore[no-untyped-def]
        service = StoreService(repo, vault)
        created = service.create_store(
            INSTALLATION_ID, store_key="hayat", display_name="حیات", store_type="custom"
        )
        rotated = service.rotate_api_key(
            INSTALLATION_ID, "hayat", api_key_scopes=["orders:read"]
        )
        assert rotated.api_key.startswith("hya_store_")
        assert rotated.api_key != created.api_key
        assert rotated.webhook_secret == ""  # the webhook secret did not change
        assert rotated.store.api_key_scopes == ("orders:read",)

    def test_rotate_unknown_store_not_found(self, repo, vault) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(IntegrationStoreNotFoundError):
            StoreService(repo, vault).rotate_webhook_secret(INSTALLATION_ID, "ghost")

    def test_update_store(self, repo, vault) -> None:  # type: ignore[no-untyped-def]
        service = StoreService(repo, vault)
        service.create_store(
            INSTALLATION_ID, store_key="hayat", display_name="حیات", store_type="custom"
        )
        updated = service.update_store(
            INSTALLATION_ID, "hayat", prices_in_toman=False, status="disabled"
        )
        assert updated.prices_in_toman is False
        assert updated.status == "disabled"

    def test_delete_store(self, repo, vault) -> None:  # type: ignore[no-untyped-def]
        service = StoreService(repo, vault)
        service.create_store(
            INSTALLATION_ID, store_key="hayat", display_name="حیات", store_type="custom"
        )
        service.delete_store(INSTALLATION_ID, "hayat")
        assert repo.get_store(INSTALLATION_ID, "hayat") is None


# -- installation settings + holidays -------------------------------------


class TestInstallationSettingsService:
    def test_upsert_settings(self, repo) -> None:  # type: ignore[no-untyped-def]
        settings = InstallationSettingsService(repo).upsert_settings(
            INSTALLATION_ID, weekly_holiday=3, matching_window_days=5, auto_post_enabled=True
        )
        assert settings.weekly_holiday == 3
        assert settings.matching_window_days == 5
        assert settings.auto_post_enabled is True

    def test_bad_weekly_holiday_rejected(self, repo) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(IntegrationSettingsInvalidError):
            InstallationSettingsService(repo).upsert_settings(
                INSTALLATION_ID, weekly_holiday=7
            )

    def test_negative_matching_window_rejected(self, repo) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(IntegrationSettingsInvalidError):
            InstallationSettingsService(repo).upsert_settings(
                INSTALLATION_ID, matching_window_days=-1
            )

    def test_add_holiday_is_idempotent(self, repo) -> None:  # type: ignore[no-untyped-def]
        service = InstallationSettingsService(repo)
        first = service.add_holiday(INSTALLATION_ID, datetime.date(2026, 3, 21), "نوروز")
        second = service.add_holiday(INSTALLATION_ID, datetime.date(2026, 3, 21), "نوروز")
        assert first.id == second.id
        assert len(repo.list_holidays(INSTALLATION_ID)) == 1

    def test_remove_holiday(self, repo) -> None:  # type: ignore[no-untyped-def]
        service = InstallationSettingsService(repo)
        service.add_holiday(INSTALLATION_ID, datetime.date(2026, 3, 21), "نوروز")
        service.remove_holiday(INSTALLATION_ID, datetime.date(2026, 3, 21))
        assert repo.list_holidays(INSTALLATION_ID) == []

    def test_remove_missing_holiday_not_found(self, repo) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(IntegrationHolidayNotFoundError):
            InstallationSettingsService(repo).remove_holiday(
                INSTALLATION_ID, datetime.date(2026, 3, 21)
            )

    def test_calendar_built_from_registry(self, repo) -> None:  # type: ignore[no-untyped-def]
        service = InstallationSettingsService(repo)
        service.upsert_settings(INSTALLATION_ID, weekly_holiday=4)
        service.add_holiday(INSTALLATION_ID, datetime.date(2026, 3, 21), "نوروز")
        calendar = service.get_holiday_calendar(INSTALLATION_ID)
        assert calendar.is_holiday(datetime.date(2026, 3, 21))
        assert calendar.is_holiday(datetime.date(2026, 11, 20))  # Friday
        assert calendar.is_business_day(datetime.date(2026, 11, 18))  # Wednesday


# -- credential vault -----------------------------------------------------


class TestFernetCredentialVault:
    def test_round_trip(self, vault) -> None:  # type: ignore[no-untyped-def]
        ciphertext = vault.encrypt("client-secret-value")
        assert ciphertext != "client-secret-value"
        assert vault.decrypt(ciphertext) == "client-secret-value"

    def test_encryption_is_randomized(self, vault) -> None:  # type: ignore[no-untyped-def]
        assert vault.encrypt("same") != vault.encrypt("same")

    def test_wrong_key_cannot_decrypt(self, vault) -> None:  # type: ignore[no-untyped-def]
        ciphertext = vault.encrypt("secret")
        other = FernetCredentialVault(generate_encryption_key())
        with pytest.raises(ValueError, match="Cannot decrypt"):
            other.decrypt(ciphertext)

    def test_tampered_ciphertext_rejected(self, vault) -> None:  # type: ignore[no-untyped-def]
        ciphertext = vault.encrypt("secret")
        tampered = ("A" if ciphertext[0] != "A" else "B") + ciphertext[1:]
        with pytest.raises(ValueError, match="Cannot decrypt"):
            vault.decrypt(tampered)

    def test_bad_key_format_rejected(self) -> None:
        with pytest.raises(ValueError, match="Invalid credential-encryption key"):
            FernetCredentialVault("not-a-fernet-key")

    def test_key_id_is_stable_and_distinct(self, vault) -> None:  # type: ignore[no-untyped-def]
        assert vault.key_id
        other = FernetCredentialVault(generate_encryption_key())
        assert other.key_id != vault.key_id

    def test_generate_key_produces_usable_key(self) -> None:
        key = generate_encryption_key()
        vault = FernetCredentialVault(key)
        assert vault.decrypt(vault.encrypt("x")) == "x"


# -- fiscal period checker ------------------------------------------------


class FakeEnginePort:
    """Minimal AccountingEnginePort stand-in for the checker tests."""

    def __init__(
        self,
        periods: Sequence[FiscalPeriodView] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._periods = list(periods or [])
        self._error = error

    def list_periods(self, company: CompanyRef) -> Sequence[FiscalPeriodView]:
        if self._error is not None:
            raise self._error
        return self._periods


def _period(
    start: datetime.date, end: datetime.date, state: str
) -> FiscalPeriodView:
    return FiscalPeriodView(
        name=f"{start}..{end}",
        start_date=FiscalDate.from_gregorian(start),
        end_date=FiscalDate.from_gregorian(end),
        state=state,
    )


class TestTrytonFiscalPeriodChecker:
    def test_open_period_containing_the_date(self) -> None:
        port = FakeEnginePort(
            [_period(datetime.date(2026, 11, 1), datetime.date(2026, 11, 30), "open")]
        )
        checker = TrytonFiscalPeriodChecker(port)  # type: ignore[arg-type]
        assert checker.is_period_open(CompanyRef(1), datetime.date(2026, 11, 18)) is True

    def test_closed_period_containing_the_date(self) -> None:
        port = FakeEnginePort(
            [_period(datetime.date(2026, 11, 1), datetime.date(2026, 11, 30), "closed")]
        )
        checker = TrytonFiscalPeriodChecker(port)  # type: ignore[arg-type]
        assert checker.is_period_open(CompanyRef(1), datetime.date(2026, 11, 18)) is False

    def test_locked_period_containing_the_date(self) -> None:
        port = FakeEnginePort(
            [_period(datetime.date(2026, 11, 1), datetime.date(2026, 11, 30), "locked")]
        )
        checker = TrytonFiscalPeriodChecker(port)  # type: ignore[arg-type]
        assert checker.is_period_open(CompanyRef(1), datetime.date(2026, 11, 18)) is False

    def test_no_period_contains_the_date(self) -> None:
        port = FakeEnginePort(
            [_period(datetime.date(2026, 10, 1), datetime.date(2026, 10, 31), "open")]
        )
        checker = TrytonFiscalPeriodChecker(port)  # type: ignore[arg-type]
        assert checker.is_period_open(CompanyRef(1), datetime.date(2026, 11, 18)) is False

    def test_engine_error_fails_closed(self) -> None:
        port = FakeEnginePort(error=HesabYarError("engine down", code="ENGINE_UNAVAILABLE"))
        checker = TrytonFiscalPeriodChecker(port)  # type: ignore[arg-type]
        assert checker.is_period_open(CompanyRef(1), datetime.date(2026, 11, 18)) is False
