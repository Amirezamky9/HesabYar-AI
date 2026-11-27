"""SMS query service (US-544, Phase 5-5 ۵ج).

Reads for the provider registry, the reminder policy, the
per-contract opt-ins, and the reminder log. Everything is
tenant/installation-scoped: a row of another installation answers
404 as if it did not exist.

The reminder policy read returns the installation's DEFAULTS when
no row was ever saved (disabled, default offsets/templates) — a
never-configured installation reads as "reminders off", never as
an error.
"""

from __future__ import annotations

from hesabyar.domain.sms.errors import SmsProviderNotFoundError
from hesabyar.domain.sms.models import (
    SmsContractReminderSettings,
    SmsLogStatus,
    SmsProvider,
    SmsReminderLogEntry,
    SmsReminderSettings,
    SmsReminderTrigger,
)
from hesabyar.domain.sms.ports import SmsRepositoryPort


class SmsQueryService:
    """Reads for providers, settings, opt-ins, and the reminder log."""

    def __init__(self, repository: SmsRepositoryPort) -> None:
        self._repository = repository

    # -- providers -----------------------------------------------------

    def list_providers(self, *, installation_id: str) -> list[SmsProvider]:
        """List the installation's providers, built-ins first, then by key."""
        return self._repository.list_providers(installation_id=installation_id)

    def get_provider(self, provider_id: str, *, installation_id: str) -> SmsProvider:
        """Return one provider (404 when missing / out of scope)."""
        provider = self._repository.get_provider(
            provider_id, installation_id=installation_id
        )
        if provider is None:
            raise SmsProviderNotFoundError(provider_id)
        return provider

    def get_default_provider(self, *, installation_id: str) -> SmsProvider | None:
        """Return the installation's default provider, or None."""
        return self._repository.get_default_provider(installation_id=installation_id)

    # -- reminder policy -------------------------------------------------

    def get_reminder_settings(
        self, *, tenant_id: str, installation_id: str
    ) -> SmsReminderSettings:
        """Return the installation's reminder policy (defaults when never saved)."""
        stored = self._repository.get_reminder_settings(installation_id=installation_id)
        if stored is not None:
            return stored
        return SmsReminderSettings.defaults(
            installation_id=installation_id, tenant_id=tenant_id
        )

    # -- per-contract opt-ins --------------------------------------------

    def get_contract_settings(
        self, *, installation_id: str, contract_id: str
    ) -> SmsContractReminderSettings | None:
        """Return a contract's reminder opt-in, or None when never configured."""
        return self._repository.get_contract_settings(
            installation_id=installation_id, contract_id=contract_id
        )

    # -- reminder log ------------------------------------------------------

    def list_log(
        self,
        *,
        installation_id: str,
        plan_id: str | None = None,
        leg_id: str | None = None,
        provider_key: str | None = None,
        status: SmsLogStatus | None = None,
        trigger: SmsReminderTrigger | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SmsReminderLogEntry]:
        """List reminder-log rows, newest first (all filters optional)."""
        return self._repository.list_log(
            installation_id=installation_id,
            plan_id=plan_id,
            leg_id=leg_id,
            provider_key=provider_key,
            status=status,
            trigger=trigger,
            limit=limit,
            offset=offset,
        )
