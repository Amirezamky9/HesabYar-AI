"""Integration registry read use cases (US-503, Phase 5 F3).

REST, MCP and workers all call this service instead of querying the
registry tables directly, so integration reads have one
implementation and one error contract (design-eng spec §6.2/§6.3).

The service depends only on the
:class:`hesabyar.domain.integrations.ports.IntegrationsRegistryPort` —
never on a session, never on infrastructure (spec §6.3). Every read
is scoped by ``installation_id`` (the caller's installation, from the
authenticated API key): a row of another installation looks exactly
like a row that does not exist.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence

from hesabyar.application.errors import (
    IntegrationBankConnectionNotFoundError,
    IntegrationChannelNotFoundError,
    IntegrationContractNotFoundError,
    IntegrationMappingNotFoundError,
    IntegrationMappingSuggestionNotFoundError,
    IntegrationStoreNotFoundError,
)
from hesabyar.domain.integrations.holidays import HolidayCalendar
from hesabyar.domain.integrations.models import (
    BankConnection,
    GatewayContract,
    Holiday,
    InstallationSettings,
    IntegrationMapping,
    MappingKind,
    MappingSuggestion,
    PaymentChannel,
    PolicyGateConfig,
    Store,
    SuggestionStatus,
)
from hesabyar.domain.integrations.ports import IntegrationsRegistryPort


class IntegrationsQueryService:
    """Read-only integration registry queries over the repository port."""

    def __init__(self, repository: IntegrationsRegistryPort) -> None:
        self._repository = repository

    # -- payment channels -------------------------------------------------

    def list_channels(self, installation_id: str) -> Sequence[PaymentChannel]:
        """Return the installation's channels, ordered by channel key."""
        return self._repository.list_channels(installation_id)

    def get_channel(self, installation_id: str, channel_key: str) -> PaymentChannel:
        """Return one channel.

        Raises :class:`IntegrationChannelNotFoundError` for an unknown
        key in this installation.
        """
        channel = self._repository.get_channel(installation_id, channel_key)
        if channel is None:
            raise IntegrationChannelNotFoundError(
                f"Payment channel {channel_key!r} is not in this installation's registry.",
                details={"channel_key": channel_key},
            )
        return channel

    # -- gateway contracts ------------------------------------------------

    def list_contracts(self, installation_id: str, channel_key: str) -> Sequence[GatewayContract]:
        """Return a channel's contract versions, newest version first."""
        channel = self.get_channel(installation_id, channel_key)
        return self._repository.list_contracts(channel.id)

    def get_contract(
        self, installation_id: str, channel_key: str, version: int
    ) -> GatewayContract:
        """Return one contract version.

        Raises :class:`IntegrationContractNotFoundError` for an unknown
        version (or an unknown channel).
        """
        channel = self.get_channel(installation_id, channel_key)
        contract = self._repository.get_contract(channel.id, version)
        if contract is None:
            raise IntegrationContractNotFoundError(
                f"Contract version {version} for channel {channel_key!r} is not in the registry.",
                details={"channel_key": channel_key, "version": version},
            )
        return contract

    def get_active_contract(
        self, installation_id: str, channel_key: str
    ) -> GatewayContract:
        """Return a channel's active contract version.

        Raises :class:`IntegrationContractNotFoundError` when the
        channel has no active contract — a channel without a contract
        has no rate, no settlement model, and no auto-post policy.
        """
        channel = self.get_channel(installation_id, channel_key)
        contract = self._repository.get_active_contract(channel.id)
        if contract is None:
            raise IntegrationContractNotFoundError(
                f"Channel {channel_key!r} has no active contract.",
                details={"channel_key": channel_key},
            )
        return contract

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
        """Return mappings, optionally filtered."""
        return self._repository.list_mappings(
            installation_id,
            mapping_kind=mapping_kind,
            channel_key=channel_key,
            source_key=source_key,
            status=status,
        )

    def get_mapping(self, installation_id: str, mapping_id: str) -> IntegrationMapping:
        """Return one mapping by row id.

        Raises :class:`IntegrationMappingNotFoundError` for an unknown
        id — or one belonging to another installation.
        """
        mapping = self._repository.get_mapping(mapping_id)
        if mapping is None or mapping.installation_id != installation_id:
            raise IntegrationMappingNotFoundError(
                f"Mapping {mapping_id!r} is not in this installation's registry.",
                details={"mapping_id": mapping_id},
            )
        return mapping

    def list_suggestions(
        self,
        installation_id: str,
        *,
        status: SuggestionStatus | None = None,
    ) -> Sequence[MappingSuggestion]:
        """Return suggestions, optionally filtered by status."""
        return self._repository.list_suggestions(installation_id, status=status)

    def get_suggestion(
        self, installation_id: str, suggestion_id: str
    ) -> MappingSuggestion:
        """Return one suggestion by row id.

        Raises :class:`IntegrationMappingSuggestionNotFoundError` for
        an unknown id — or one belonging to another installation.
        """
        suggestion = self._repository.get_suggestion(suggestion_id)
        if suggestion is None or suggestion.installation_id != installation_id:
            raise IntegrationMappingSuggestionNotFoundError(
                f"Mapping suggestion {suggestion_id!r} is not in this installation's registry.",
                details={"suggestion_id": suggestion_id},
            )
        return suggestion

    # -- policy gate config ------------------------------------------------

    def list_policy_configs(self, installation_id: str) -> Sequence[PolicyGateConfig]:
        """Return the installation's policy rows, ordered by event type."""
        return self._repository.list_policy_configs(installation_id)

    def get_policy_config(
        self, installation_id: str, event_type: str
    ) -> PolicyGateConfig | None:
        """Return one policy row, or None when the event type has no policy.

        None is NOT an error here: a missing policy row is the gate's
        normal "deny" input (layer 1 fails closed).
        """
        return self._repository.get_policy_config(installation_id, event_type)

    # -- bank connections --------------------------------------------------

    def list_bank_connections(
        self, installation_id: str, *, company_id: str | None = None
    ) -> Sequence[BankConnection]:
        """Return the installation's bank connections, optionally per company."""
        return self._repository.list_bank_connections(installation_id, company_id=company_id)

    def get_bank_connection(
        self, installation_id: str, connection_id: str
    ) -> BankConnection:
        """Return one bank connection.

        Raises :class:`IntegrationBankConnectionNotFoundError` for an
        unknown id — or one belonging to another installation.
        """
        connection = self._repository.get_bank_connection(installation_id, connection_id)
        if connection is None:
            raise IntegrationBankConnectionNotFoundError(
                f"Bank connection {connection_id!r} is not in this installation's registry.",
                details={"connection_id": connection_id},
            )
        return connection

    # -- stores ------------------------------------------------------------

    def list_stores(self, installation_id: str) -> Sequence[Store]:
        """Return the installation's stores, ordered by store key."""
        return self._repository.list_stores(installation_id)

    def get_store(self, installation_id: str, store_key: str) -> Store:
        """Return one store.

        Raises :class:`IntegrationStoreNotFoundError` for an unknown
        key in this installation.
        """
        store = self._repository.get_store(installation_id, store_key)
        if store is None:
            raise IntegrationStoreNotFoundError(
                f"Store {store_key!r} is not in this installation's registry.",
                details={"store_key": store_key},
            )
        return store

    # -- installation settings + holidays ----------------------------------

    def get_settings(self, installation_id: str) -> InstallationSettings:
        """Return an installation's settings, or the defaults when unset.

        Unset settings are NOT an error: the defaults (Friday weekly
        holiday, 2-calendar-day matching window, auto-post OFF) are
        the fail-closed baseline.
        """
        settings = self._repository.get_installation_settings(installation_id)
        if settings is None:
            return InstallationSettings(installation_id=installation_id)
        return settings

    def list_holidays(self, installation_id: str) -> Sequence[Holiday]:
        """Return an installation's holidays, ordered by date."""
        return self._repository.list_holidays(installation_id)

    def get_holiday_calendar(self, installation_id: str) -> HolidayCalendar:
        """Build the installation's holiday calendar from the registry."""
        settings = self.get_settings(installation_id)
        holidays = frozenset(
            holiday.holiday_date for holiday in self._repository.list_holidays(installation_id)
        )
        return HolidayCalendar(weekly_holiday=settings.weekly_holiday, holidays=holidays)

    def is_holiday(self, installation_id: str, day: datetime.date) -> bool:
        """True when ``day`` is a holiday for this installation."""
        return self.get_holiday_calendar(installation_id).is_holiday(day)
