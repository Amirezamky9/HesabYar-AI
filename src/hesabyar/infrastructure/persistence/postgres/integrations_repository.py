"""SQLAlchemy implementation of the integrations registry port (US-503).

Implements :class:`hesabyar.domain.integrations.ports.IntegrationsRegistryPort`
over the ``payment_channels`` / ``gateway_contracts`` /
``integration_mappings`` / ``mapping_suggestions`` /
``auto_post_policies`` / ``bank_connections`` / ``stores`` /
``installation_settings`` / ``installation_holidays`` tables. The
repository speaks domain types only at its boundary: ORM rows are
mapped to domain dataclasses here, so the application layer never sees
SQLAlchemy.

Every read/write is scoped by ``installation_id`` (the caller's
installation, from the authenticated API key): a row of another
installation looks exactly like a row that does not exist.

Credential material: the repository stores ciphertext only
(``credentials_ciphertext``, ``webhook_secret_ciphertext``,
``api_key_ciphertext``). Plaintext never reaches this module — the
application service encrypts via the credential vault before calling
in. Read models expose only ``has_*`` flags; the ciphertext getters
exist for future workers (webhook receiver, bank sync) and are never
exposed through REST.

The repository flushes on save (so ids exist) but never commits —
the caller owns the transaction.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

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
from hesabyar.infrastructure.auth.scopes import format_scopes, parse_scopes
from hesabyar.infrastructure.persistence.postgres.models import (
    AutoPostPolicyModel,
    BankConnectionModel,
    GatewayContractModel,
    InstallationHolidayModel,
    InstallationSettingsModel,
    IntegrationMappingModel,
    MappingSuggestionModel,
    PaymentChannelModel,
    StoreModel,
)
from hesabyar.infrastructure.persistence.postgres.seed_payment_channels import (
    seed_builtin_channels as _seed_builtin_channels,
)


def _legs_to_json(legs: tuple[TrancheLeg, ...]) -> list[dict[str, object]]:
    """Serialize tranche legs for JSONB (percent as a string — exact Decimal)."""
    return [
        {
            "month_offset": leg.month_offset,
            "payout_day": leg.payout_day,
            "percent": str(leg.percent),
        }
        for leg in legs
    ]


def _legs_from_json(raw: object) -> tuple[TrancheLeg, ...]:
    """Parse tranche legs back from JSONB."""
    if not isinstance(raw, list):
        return ()
    legs: list[TrancheLeg] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        legs.append(
            TrancheLeg(
                month_offset=int(item["month_offset"]),
                payout_day=int(item["payout_day"]),
                percent=Decimal(str(item["percent"])),
            )
        )
    return tuple(legs)


def _channel_from_row(row: PaymentChannelModel) -> PaymentChannel:
    return PaymentChannel(
        id=row.id,
        installation_id=row.installation_id,
        channel_key=row.channel_key,
        display_name_fa=row.display_name_fa,
        channel_type=row.channel_type,
        is_builtin=row.is_builtin,
        enabled=row.enabled,
        status=ChannelStatus(row.status),
        merchant_ref=row.merchant_ref,
        settlement_iban=row.settlement_iban,
        environment=row.environment,
        notes=row.notes,
        has_credentials=row.credentials_ciphertext is not None,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _contract_from_row(row: GatewayContractModel) -> GatewayContract:
    return GatewayContract(
        id=row.id,
        channel_id=row.channel_id,
        channel_key=row.channel.channel_key,
        version=row.version,
        valid_from=row.valid_from,
        valid_to=row.valid_to,
        fee_percent=row.fee_percent,
        fee_vat_rate=row.fee_vat_rate,
        fee_vat_claimable=row.fee_vat_claimable,
        fee_timing=row.fee_timing,
        fee_accrual_policy=row.fee_accrual_policy,
        settlement_model=row.settlement_model,
        tranche_legs=_legs_from_json(row.tranche_legs),
        payout_window_start_day=row.payout_window_start_day,
        payout_window_end_day=row.payout_window_end_day,
        payout_calendar=row.payout_calendar,
        working_day_adjustment=row.working_day_adjustment,
        day_lag=row.day_lag,
        grace_days_after_due=row.grace_days_after_due,
        match_tolerance_irr=row.match_tolerance_irr,
        tieout_tolerance_irr=row.tieout_tolerance_irr,
        auto_post_policy=AutoPostPolicy(row.auto_post_policy),
        auto_post_max_amount_irr=row.auto_post_max_amount_irr,
        source_note=row.source_note,
        status=ContractStatus(row.status),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _mapping_from_row(row: IntegrationMappingModel) -> IntegrationMapping:
    return IntegrationMapping(
        id=row.id,
        installation_id=row.installation_id,
        mapping_kind=MappingKind(row.mapping_kind),
        channel_key=row.channel_key,
        source_key=row.source_key,
        target_type=row.target_type,
        target_ref=row.target_ref,
        version=row.version,
        status=MappingStatus(row.status),
        suggestion_mode=SuggestionMode(row.suggestion_mode),
        source=row.source,
        confidence=row.confidence,
        notes=row.notes,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _suggestion_from_row(row: MappingSuggestionModel) -> MappingSuggestion:
    return MappingSuggestion(
        id=row.id,
        installation_id=row.installation_id,
        mapping_kind=MappingKind(row.mapping_kind),
        channel_key=row.channel_key,
        source_key=row.source_key,
        suggested_target_type=row.suggested_target_type,
        suggested_target_ref=row.suggested_target_ref,
        confidence=row.confidence,
        basis=row.basis,
        status=SuggestionStatus(row.status),
        resolved_by=row.resolved_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _policy_from_row(row: AutoPostPolicyModel) -> PolicyGateConfig:
    return PolicyGateConfig(
        id=row.id,
        installation_id=row.installation_id,
        event_type=row.event_type,
        allowed=row.allowed,
        max_amount_irr=row.max_amount_irr,
        require_tieout=row.require_tieout,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _bank_connection_from_row(row: BankConnectionModel) -> BankConnection:
    return BankConnection(
        id=row.id,
        installation_id=row.installation_id,
        company_id=row.company_id,
        bank_key=row.bank_key,
        display_name=row.display_name,
        connection_kind=row.connection_kind,
        has_credentials=row.credentials_ciphertext is not None,
        credentials_key_id=row.credentials_key_id,
        status=row.status,
        last_checked_at=row.last_checked_at,
        notes=row.notes,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _store_from_row(row: StoreModel) -> Store:
    return Store(
        id=row.id,
        installation_id=row.installation_id,
        store_key=row.store_key,
        display_name=row.display_name,
        store_type=row.store_type,
        prices_in_toman=row.prices_in_toman,
        has_webhook_secret=row.webhook_secret_ciphertext is not None,
        has_api_key=row.api_key_ciphertext is not None,
        has_store_db_dsn=row.store_db_dsn_ciphertext is not None,
        api_key_scopes=tuple(sorted(parse_scopes(row.api_key_scopes))),
        api_base_url=row.api_base_url,
        status=row.status,
        last_webhook_at=row.last_webhook_at,
        last_poll_at=row.last_poll_at,
        notes=row.notes,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _settings_from_row(row: InstallationSettingsModel) -> InstallationSettings:
    return InstallationSettings(
        installation_id=row.installation_id,
        weekly_holiday=row.weekly_holiday,
        matching_window_days=row.matching_window_days,
        auto_post_enabled=row.auto_post_enabled,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _holiday_from_row(row: InstallationHolidayModel) -> Holiday:
    return Holiday(
        id=row.id,
        installation_id=row.installation_id,
        holiday_date=row.holiday_date,
        title=row.title,
        created_at=row.created_at,
    )


class SqlAlchemyIntegrationsRepository:
    """Integration registries persistence over a SQLAlchemy session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- payment channels -------------------------------------------------

    def list_channels(self, installation_id: str) -> Sequence[PaymentChannel]:
        """Return the installation's channels, ordered by channel key."""
        rows = list(
            self._session.execute(
                select(PaymentChannelModel)
                .where(PaymentChannelModel.installation_id == installation_id)
                .order_by(PaymentChannelModel.channel_key)
            ).scalars()
        )
        return [_channel_from_row(row) for row in rows]

    def get_channel(self, installation_id: str, channel_key: str) -> PaymentChannel | None:
        """Return one channel, or None for an unknown key in this installation."""
        row = self._session.execute(
            select(PaymentChannelModel).where(
                PaymentChannelModel.installation_id == installation_id,
                PaymentChannelModel.channel_key == channel_key,
            )
        ).scalar_one_or_none()
        return _channel_from_row(row) if row is not None else None

    def get_channel_by_id(self, channel_id: str) -> PaymentChannel | None:
        """Return one channel by row id, or None for an unknown id."""
        row = self._session.get(PaymentChannelModel, channel_id)
        return _channel_from_row(row) if row is not None else None

    def save_channel(self, command: CreatePaymentChannel) -> PaymentChannel:
        """Persist a new (custom) channel row and return it."""
        row = PaymentChannelModel(
            installation_id=command.installation_id,
            channel_key=command.channel_key,
            display_name_fa=command.display_name_fa,
            channel_type=command.channel_type,
            is_builtin=False,
            enabled=False,
            status=ChannelStatus.SETUP.value,
            merchant_ref=command.merchant_ref,
            settlement_iban=command.settlement_iban,
            environment=command.environment,
            notes=command.notes,
        )
        self._session.add(row)
        self._session.flush()
        return _channel_from_row(row)

    def update_channel(self, command: UpdatePaymentChannel) -> PaymentChannel | None:
        """Apply a channel update; None when the channel does not exist."""
        row = self._session.execute(
            select(PaymentChannelModel).where(
                PaymentChannelModel.installation_id == command.installation_id,
                PaymentChannelModel.channel_key == command.channel_key,
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        if command.display_name_fa is not None:
            row.display_name_fa = command.display_name_fa
        if command.channel_type is not None:
            row.channel_type = command.channel_type
        if command.enabled is not None:
            row.enabled = command.enabled
        if command.status is not None:
            row.status = command.status
        if command.merchant_ref is not None:
            row.merchant_ref = command.merchant_ref
        if command.settlement_iban is not None:
            row.settlement_iban = command.settlement_iban
        if command.environment is not None:
            row.environment = command.environment
        if command.notes is not None:
            row.notes = command.notes
        self._session.flush()
        return _channel_from_row(row)

    def rotate_channel_credentials(
        self, command: RotateChannelCredentials
    ) -> PaymentChannel | None:
        """Replace stored gateway credentials; None when the channel is missing."""
        row = self._session.execute(
            select(PaymentChannelModel).where(
                PaymentChannelModel.installation_id == command.installation_id,
                PaymentChannelModel.channel_key == command.channel_key,
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        row.credentials_ciphertext = command.credentials_ciphertext
        row.credentials_key_id = command.credentials_key_id
        self._session.flush()
        return _channel_from_row(row)

    def get_channel_ciphertext(self, channel_id: str) -> str | None:
        """Return a channel's stored ciphertext, or None."""
        row = self._session.get(PaymentChannelModel, channel_id)
        return row.credentials_ciphertext if row is not None else None

    def delete_channel(self, installation_id: str, channel_key: str) -> bool:
        """Delete a channel row; False when it does not exist."""
        row = self._session.execute(
            select(PaymentChannelModel).where(
                PaymentChannelModel.installation_id == installation_id,
                PaymentChannelModel.channel_key == channel_key,
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        self._session.delete(row)
        self._session.flush()
        return True

    def seed_builtin_channels(self, installation_id: str) -> Sequence[PaymentChannel]:
        """Idempotently create the built-in channel rows for an installation."""
        _seed_builtin_channels(self._session, installation_id)
        self._session.flush()
        return self.list_channels(installation_id)

    # -- gateway contracts ------------------------------------------------

    def list_contracts(self, channel_id: str) -> Sequence[GatewayContract]:
        """Return a channel's contract versions, newest version first."""
        rows = list(
            self._session.execute(
                select(GatewayContractModel)
                .options(selectinload(GatewayContractModel.channel))
                .where(GatewayContractModel.channel_id == channel_id)
                .order_by(GatewayContractModel.version.desc())
            ).scalars()
        )
        return [_contract_from_row(row) for row in rows]

    def get_contract(self, channel_id: str, version: int) -> GatewayContract | None:
        """Return one contract version, or None for an unknown version."""
        row = self._session.execute(
            select(GatewayContractModel)
            .options(selectinload(GatewayContractModel.channel))
            .where(
                GatewayContractModel.channel_id == channel_id,
                GatewayContractModel.version == version,
            )
        ).scalar_one_or_none()
        return _contract_from_row(row) if row is not None else None

    def get_contract_by_id(self, contract_id: str) -> GatewayContract | None:
        """Return one contract version by row id, or None for an unknown id."""
        row = self._session.execute(
            select(GatewayContractModel)
            .options(selectinload(GatewayContractModel.channel))
            .where(GatewayContractModel.id == contract_id)
        ).scalar_one_or_none()
        return _contract_from_row(row) if row is not None else None

    def get_active_contract(self, channel_id: str) -> GatewayContract | None:
        """Return the channel's active contract version, or None."""
        row = self._session.execute(
            select(GatewayContractModel)
            .options(selectinload(GatewayContractModel.channel))
            .where(
                GatewayContractModel.channel_id == channel_id,
                GatewayContractModel.status == ContractStatus.ACTIVE.value,
            )
            .order_by(GatewayContractModel.version.desc())
            .limit(1)
        ).scalar_one_or_none()
        return _contract_from_row(row) if row is not None else None

    def get_active_contract_for_channel_key(
        self, installation_id: str, channel_key: str
    ) -> GatewayContract | None:
        """Return the active contract for a channel key, or None."""
        channel = self._session.execute(
            select(PaymentChannelModel).where(
                PaymentChannelModel.installation_id == installation_id,
                PaymentChannelModel.channel_key == channel_key,
            )
        ).scalar_one_or_none()
        if channel is None:
            return None
        return self.get_active_contract(channel.id)

    def save_contract(self, command: CreateGatewayContract) -> GatewayContract:
        """Persist a new contract version row and return it."""
        row = GatewayContractModel(
            channel_id=command.channel_id,
            version=command.version,
            valid_from=command.valid_from,
            valid_to=None,
            fee_percent=command.fee_percent,
            fee_vat_rate=command.fee_vat_rate,
            fee_vat_claimable=command.fee_vat_claimable,
            fee_timing=command.fee_timing,
            fee_accrual_policy=command.fee_accrual_policy,
            settlement_model=command.settlement_model,
            tranche_legs=_legs_to_json(command.tranche_legs),
            payout_window_start_day=command.payout_window_start_day,
            payout_window_end_day=command.payout_window_end_day,
            payout_calendar=command.payout_calendar,
            working_day_adjustment=command.working_day_adjustment,
            day_lag=command.day_lag,
            grace_days_after_due=command.grace_days_after_due,
            match_tolerance_irr=command.match_tolerance_irr,
            tieout_tolerance_irr=command.tieout_tolerance_irr,
            auto_post_policy=command.auto_post_policy.value,
            auto_post_max_amount_irr=command.auto_post_max_amount_irr,
            source_note=command.source_note,
            status=ContractStatus.ACTIVE.value,
        )
        self._session.add(row)
        self._session.flush()
        # Re-read with the channel loaded for the domain mapping.
        saved = self.get_contract(command.channel_id, command.version)
        assert saved is not None
        return saved

    def supersede_contract(self, contract_id: str, valid_to: datetime.date) -> None:
        """Mark a contract superseded and close its effective range."""
        row = self._session.get(GatewayContractModel, contract_id)
        if row is None:
            return
        row.status = ContractStatus.SUPERSEDED.value
        row.valid_to = valid_to
        self._session.flush()

    def count_contracts(self, channel_id: str) -> int:
        """Return how many contract versions a channel has."""
        rows = list(
            self._session.execute(
                select(GatewayContractModel.id).where(
                    GatewayContractModel.channel_id == channel_id
                )
            ).scalars()
        )
        return len(rows)

    # -- mapping center ---------------------------------------------------

    def list_mappings(
        self,
        installation_id: str,
        *,
        mapping_kind: MappingKind | None = None,
        channel_key: str | None = None,
        source_key: str | None = None,
        status: str | None = None,
    ) -> Sequence[IntegrationMapping]:
        """Return mappings, optionally filtered (see the port)."""
        stmt = select(IntegrationMappingModel).where(
            IntegrationMappingModel.installation_id == installation_id
        )
        if mapping_kind is not None:
            stmt = stmt.where(IntegrationMappingModel.mapping_kind == mapping_kind.value)
        if channel_key is not None:
            stmt = stmt.where(IntegrationMappingModel.channel_key == channel_key)
        if source_key is not None:
            stmt = stmt.where(IntegrationMappingModel.source_key == source_key)
        if status is not None:
            stmt = stmt.where(IntegrationMappingModel.status == status)
        stmt = stmt.order_by(
            IntegrationMappingModel.mapping_kind,
            IntegrationMappingModel.channel_key,
            IntegrationMappingModel.source_key,
            IntegrationMappingModel.version.desc(),
        )
        rows = list(self._session.execute(stmt).scalars())
        return [_mapping_from_row(row) for row in rows]

    def get_mapping(self, mapping_id: str) -> IntegrationMapping | None:
        """Return one mapping by row id, or None for an unknown id."""
        row = self._session.get(IntegrationMappingModel, mapping_id)
        return _mapping_from_row(row) if row is not None else None

    def resolve_mapping(
        self,
        installation_id: str,
        mapping_kind: MappingKind,
        channel_key: str,
        source_key: str,
    ) -> IntegrationMapping | None:
        """Return the ACTIVE mapping for a (kind, channel, source), or None."""
        row = self._session.execute(
            select(IntegrationMappingModel)
            .where(
                IntegrationMappingModel.installation_id == installation_id,
                IntegrationMappingModel.mapping_kind == mapping_kind.value,
                IntegrationMappingModel.channel_key == channel_key,
                IntegrationMappingModel.source_key == source_key,
                IntegrationMappingModel.status == MappingStatus.ACTIVE.value,
            )
            .order_by(IntegrationMappingModel.version.desc())
            .limit(1)
        ).scalar_one_or_none()
        return _mapping_from_row(row) if row is not None else None

    def save_mapping(self, command: CreateIntegrationMapping) -> IntegrationMapping:
        """Persist a new mapping version row and return it."""
        row = IntegrationMappingModel(
            installation_id=command.installation_id,
            mapping_kind=command.mapping_kind.value,
            channel_key=command.channel_key,
            source_key=command.source_key,
            target_type=command.target_type,
            target_ref=command.target_ref,
            version=command.version,
            status=MappingStatus.ACTIVE.value,
            suggestion_mode=command.suggestion_mode.value,
            source=command.source,
            confidence=command.confidence,
            notes=command.notes,
            created_by=command.created_by,
        )
        self._session.add(row)
        self._session.flush()
        return _mapping_from_row(row)

    def supersede_mapping(self, mapping_id: str) -> None:
        """Mark a mapping version superseded (status change only)."""
        row = self._session.get(IntegrationMappingModel, mapping_id)
        if row is None:
            return
        row.status = MappingStatus.SUPERSEDED.value
        self._session.flush()

    def count_mappings_for_channel(self, installation_id: str, channel_key: str) -> int:
        """Return how many mappings reference a channel key."""
        rows = list(
            self._session.execute(
                select(IntegrationMappingModel.id).where(
                    IntegrationMappingModel.installation_id == installation_id,
                    IntegrationMappingModel.channel_key == channel_key,
                )
            ).scalars()
        )
        return len(rows)

    # -- mapping suggestions ----------------------------------------------

    def list_suggestions(
        self,
        installation_id: str,
        *,
        status: SuggestionStatus | None = None,
    ) -> Sequence[MappingSuggestion]:
        """Return suggestions, optionally filtered by status."""
        stmt = select(MappingSuggestionModel).where(
            MappingSuggestionModel.installation_id == installation_id
        )
        if status is not None:
            stmt = stmt.where(MappingSuggestionModel.status == status.value)
        stmt = stmt.order_by(MappingSuggestionModel.created_at.desc())
        rows = list(self._session.execute(stmt).scalars())
        return [_suggestion_from_row(row) for row in rows]

    def get_suggestion(self, suggestion_id: str) -> MappingSuggestion | None:
        """Return one suggestion by row id, or None for an unknown id."""
        row = self._session.get(MappingSuggestionModel, suggestion_id)
        return _suggestion_from_row(row) if row is not None else None

    def get_pending_suggestion(
        self,
        installation_id: str,
        mapping_kind: MappingKind,
        channel_key: str,
        source_key: str,
    ) -> MappingSuggestion | None:
        """Return the pending suggestion for a (kind, channel, source), or None."""
        row = self._session.execute(
            select(MappingSuggestionModel).where(
                MappingSuggestionModel.installation_id == installation_id,
                MappingSuggestionModel.mapping_kind == mapping_kind.value,
                MappingSuggestionModel.channel_key == channel_key,
                MappingSuggestionModel.source_key == source_key,
                MappingSuggestionModel.status == SuggestionStatus.PENDING.value,
            )
        ).scalar_one_or_none()
        return _suggestion_from_row(row) if row is not None else None

    def save_suggestion(self, command: CreateMappingSuggestion) -> MappingSuggestion:
        """Persist a new pending suggestion and return it."""
        row = MappingSuggestionModel(
            installation_id=command.installation_id,
            mapping_kind=command.mapping_kind.value,
            channel_key=command.channel_key,
            source_key=command.source_key,
            suggested_target_type=command.suggested_target_type,
            suggested_target_ref=command.suggested_target_ref,
            confidence=command.confidence,
            basis=command.basis,
            status=SuggestionStatus.PENDING.value,
        )
        self._session.add(row)
        self._session.flush()
        return _suggestion_from_row(row)

    def resolve_suggestion(
        self,
        suggestion_id: str,
        status: SuggestionStatus,
        resolved_by: str | None,
    ) -> MappingSuggestion | None:
        """Mark a suggestion accepted/rejected; None when it does not exist."""
        row = self._session.get(MappingSuggestionModel, suggestion_id)
        if row is None:
            return None
        row.status = status.value
        row.resolved_by = resolved_by
        self._session.flush()
        return _suggestion_from_row(row)

    # -- policy gate config ------------------------------------------------

    def list_policy_configs(self, installation_id: str) -> Sequence[PolicyGateConfig]:
        """Return the installation's policy rows, ordered by event type."""
        rows = list(
            self._session.execute(
                select(AutoPostPolicyModel)
                .where(AutoPostPolicyModel.installation_id == installation_id)
                .order_by(AutoPostPolicyModel.event_type)
            ).scalars()
        )
        return [_policy_from_row(row) for row in rows]

    def get_policy_config(self, installation_id: str, event_type: str) -> PolicyGateConfig | None:
        """Return one policy row, or None when the event type has no policy."""
        row = self._session.execute(
            select(AutoPostPolicyModel).where(
                AutoPostPolicyModel.installation_id == installation_id,
                AutoPostPolicyModel.event_type == event_type,
            )
        ).scalar_one_or_none()
        return _policy_from_row(row) if row is not None else None

    def save_policy_config(self, command: UpsertPolicyGateConfig) -> PolicyGateConfig:
        """Create or replace a policy row and return it."""
        row = self._session.execute(
            select(AutoPostPolicyModel).where(
                AutoPostPolicyModel.installation_id == command.installation_id,
                AutoPostPolicyModel.event_type == command.event_type,
            )
        ).scalar_one_or_none()
        if row is None:
            row = AutoPostPolicyModel(
                installation_id=command.installation_id,
                event_type=command.event_type,
                allowed=command.allowed,
                max_amount_irr=command.max_amount_irr,
                require_tieout=command.require_tieout,
            )
            self._session.add(row)
        else:
            row.allowed = command.allowed
            row.max_amount_irr = command.max_amount_irr
            row.require_tieout = command.require_tieout
        self._session.flush()
        return _policy_from_row(row)

    # -- bank connections --------------------------------------------------

    def list_bank_connections(
        self, installation_id: str, *, company_id: str | None = None
    ) -> Sequence[BankConnection]:
        """Return the installation's bank connections, optionally per company."""
        stmt = select(BankConnectionModel).where(
            BankConnectionModel.installation_id == installation_id
        )
        if company_id is not None:
            stmt = stmt.where(BankConnectionModel.company_id == company_id)
        stmt = stmt.order_by(BankConnectionModel.bank_key)
        rows = list(self._session.execute(stmt).scalars())
        return [_bank_connection_from_row(row) for row in rows]

    def get_bank_connection(
        self, installation_id: str, connection_id: str
    ) -> BankConnection | None:
        """Return one bank connection, or None for an unknown id."""
        row = self._session.execute(
            select(BankConnectionModel).where(
                BankConnectionModel.installation_id == installation_id,
                BankConnectionModel.id == connection_id,
            )
        ).scalar_one_or_none()
        return _bank_connection_from_row(row) if row is not None else None

    def save_bank_connection(self, command: CreateBankConnection) -> BankConnection:
        """Persist a new bank connection and return it (no credential material)."""
        row = BankConnectionModel(
            installation_id=command.installation_id,
            company_id=command.company_id,
            bank_key=command.bank_key,
            display_name=command.display_name,
            connection_kind=command.connection_kind,
            credentials_ciphertext=command.credentials_ciphertext,
            credentials_key_id=command.credentials_key_id,
            status=command.status,
            notes=command.notes,
        )
        self._session.add(row)
        self._session.flush()
        return _bank_connection_from_row(row)

    def update_bank_connection(self, command: UpdateBankConnection) -> BankConnection | None:
        """Apply a bank-connection update; None when it does not exist."""
        row = self._session.execute(
            select(BankConnectionModel).where(
                BankConnectionModel.installation_id == command.installation_id,
                BankConnectionModel.id == command.connection_id,
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        if command.display_name is not None:
            row.display_name = command.display_name
        if command.connection_kind is not None:
            row.connection_kind = command.connection_kind
        if command.status is not None:
            row.status = command.status
        if command.last_checked_at is not None:
            row.last_checked_at = command.last_checked_at
        if command.notes is not None:
            row.notes = command.notes
        self._session.flush()
        return _bank_connection_from_row(row)

    def rotate_bank_connection_credentials(
        self, command: RotateBankConnectionCredentials
    ) -> BankConnection | None:
        """Replace stored credentials; None when the connection does not exist."""
        row = self._session.execute(
            select(BankConnectionModel).where(
                BankConnectionModel.installation_id == command.installation_id,
                BankConnectionModel.id == command.connection_id,
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        row.credentials_ciphertext = command.credentials_ciphertext
        row.credentials_key_id = command.credentials_key_id
        self._session.flush()
        return _bank_connection_from_row(row)

    def delete_bank_connection(self, installation_id: str, connection_id: str) -> bool:
        """Delete a bank connection; False when it does not exist."""
        row = self._session.execute(
            select(BankConnectionModel).where(
                BankConnectionModel.installation_id == installation_id,
                BankConnectionModel.id == connection_id,
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        self._session.delete(row)
        self._session.flush()
        return True

    def get_bank_connection_ciphertext(self, connection_id: str) -> str | None:
        """Return a connection's stored ciphertext, or None."""
        row = self._session.get(BankConnectionModel, connection_id)
        return row.credentials_ciphertext if row is not None else None

    # -- stores ------------------------------------------------------------

    def list_stores(self, installation_id: str) -> Sequence[Store]:
        """Return the installation's stores, ordered by store key."""
        rows = list(
            self._session.execute(
                select(StoreModel)
                .where(StoreModel.installation_id == installation_id)
                .order_by(StoreModel.store_key)
            ).scalars()
        )
        return [_store_from_row(row) for row in rows]

    def get_store(self, installation_id: str, store_key: str) -> Store | None:
        """Return one store, or None for an unknown key in this installation."""
        row = self._session.execute(
            select(StoreModel).where(
                StoreModel.installation_id == installation_id,
                StoreModel.store_key == store_key,
            )
        ).scalar_one_or_none()
        return _store_from_row(row) if row is not None else None

    def save_store(self, command: CreateStore) -> Store:
        """Persist a new store and return it (no secret material)."""
        row = StoreModel(
            installation_id=command.installation_id,
            store_key=command.store_key,
            display_name=command.display_name,
            store_type=command.store_type,
            api_base_url=command.api_base_url,
            prices_in_toman=command.prices_in_toman,
            webhook_secret_ciphertext=command.webhook_secret_ciphertext,
            api_key_ciphertext=command.api_key_ciphertext,
            api_key_scopes=format_scopes(command.api_key_scopes),
            status=command.status,
            notes=command.notes,
        )
        self._session.add(row)
        self._session.flush()
        return _store_from_row(row)

    def update_store(self, command: UpdateStore) -> Store | None:
        """Apply a store update; None when the store does not exist."""
        row = self._session.execute(
            select(StoreModel).where(
                StoreModel.installation_id == command.installation_id,
                StoreModel.store_key == command.store_key,
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        if command.display_name is not None:
            row.display_name = command.display_name
        if command.store_type is not None:
            row.store_type = command.store_type
        if command.prices_in_toman is not None:
            row.prices_in_toman = command.prices_in_toman
        if command.api_key_scopes is not None:
            row.api_key_scopes = format_scopes(command.api_key_scopes)
        if command.api_base_url is not None:
            row.api_base_url = command.api_base_url
        if command.status is not None:
            row.status = command.status
        if command.last_webhook_at is not None:
            row.last_webhook_at = command.last_webhook_at
        if command.last_poll_at is not None:
            row.last_poll_at = command.last_poll_at
        if command.notes is not None:
            row.notes = command.notes
        self._session.flush()
        return _store_from_row(row)

    def rotate_store_webhook_secret(self, command: RotateStoreWebhookSecret) -> Store | None:
        """Replace a store's webhook secret; None when the store does not exist."""
        row = self._session.execute(
            select(StoreModel).where(
                StoreModel.installation_id == command.installation_id,
                StoreModel.store_key == command.store_key,
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        row.webhook_secret_ciphertext = command.webhook_secret_ciphertext
        self._session.flush()
        return _store_from_row(row)

    def rotate_store_api_key(self, command: RotateStoreApiKey) -> Store | None:
        """Replace a store's read-only API key; None when it does not exist."""
        row = self._session.execute(
            select(StoreModel).where(
                StoreModel.installation_id == command.installation_id,
                StoreModel.store_key == command.store_key,
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        row.api_key_ciphertext = command.api_key_ciphertext
        row.api_key_scopes = format_scopes(command.api_key_scopes)
        self._session.flush()
        return _store_from_row(row)

    def set_store_db_dsn(self, command: SetStoreDbDsn) -> Store | None:
        """Store a store's read-only DB DSN (encrypted); None when it does not exist."""
        row = self._session.execute(
            select(StoreModel).where(
                StoreModel.installation_id == command.installation_id,
                StoreModel.store_key == command.store_key,
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        row.store_db_dsn_ciphertext = command.dsn_ciphertext
        self._session.flush()
        return _store_from_row(row)

    def delete_store(self, installation_id: str, store_key: str) -> bool:
        """Delete a store; False when it does not exist."""
        row = self._session.execute(
            select(StoreModel).where(
                StoreModel.installation_id == installation_id,
                StoreModel.store_key == store_key,
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        self._session.delete(row)
        self._session.flush()
        return True

    def get_store_ciphertext(self, store_id: str, kind: str) -> str | None:
        """Return a store's stored ciphertext (``kind``: webhook_secret | api_key | store_db_dsn)."""
        row = self._session.get(StoreModel, store_id)
        if row is None:
            return None
        if kind == "webhook_secret":
            return row.webhook_secret_ciphertext
        if kind == "api_key":
            return row.api_key_ciphertext
        if kind == "store_db_dsn":
            return row.store_db_dsn_ciphertext
        return None

    # -- installation settings + holidays ----------------------------------

    def get_installation_settings(self, installation_id: str) -> InstallationSettings | None:
        """Return an installation's settings row, or None when unset."""
        row = self._session.get(InstallationSettingsModel, installation_id)
        return _settings_from_row(row) if row is not None else None

    def save_installation_settings(
        self, command: UpsertInstallationSettings
    ) -> InstallationSettings:
        """Create or replace an installation's settings row and return it."""
        row = self._session.get(InstallationSettingsModel, command.installation_id)
        if row is None:
            row = InstallationSettingsModel(
                installation_id=command.installation_id,
                weekly_holiday=command.weekly_holiday,
                matching_window_days=command.matching_window_days,
                auto_post_enabled=command.auto_post_enabled,
            )
            self._session.add(row)
        else:
            row.weekly_holiday = command.weekly_holiday
            row.matching_window_days = command.matching_window_days
            row.auto_post_enabled = command.auto_post_enabled
        self._session.flush()
        return _settings_from_row(row)

    def list_holidays(self, installation_id: str) -> Sequence[Holiday]:
        """Return an installation's holidays, ordered by date."""
        rows = list(
            self._session.execute(
                select(InstallationHolidayModel)
                .where(InstallationHolidayModel.installation_id == installation_id)
                .order_by(InstallationHolidayModel.holiday_date)
            ).scalars()
        )
        return [_holiday_from_row(row) for row in rows]

    def add_holiday(self, command: AddHoliday) -> Holiday:
        """Add a holiday date and return it."""
        row = InstallationHolidayModel(
            installation_id=command.installation_id,
            holiday_date=command.holiday_date,
            title=command.title,
        )
        self._session.add(row)
        self._session.flush()
        return _holiday_from_row(row)

    def delete_holiday(self, installation_id: str, holiday_date: datetime.date) -> bool:
        """Delete a holiday date; False when it was not listed."""
        row = self._session.execute(
            select(InstallationHolidayModel).where(
                InstallationHolidayModel.installation_id == installation_id,
                InstallationHolidayModel.holiday_date == holiday_date,
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        self._session.delete(row)
        self._session.flush()
        return True
