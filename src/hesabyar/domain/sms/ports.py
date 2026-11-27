"""SMS ports (US-544, Phase 5-5 ۵ج).

The application services depend only on these protocols; the
SQLAlchemy implementation lives in
``hesabyar.infrastructure.persistence.postgres.sms_repository``
and the provider clients in ``hesabyar.infrastructure.sms``.

Two ports, split on purpose:

- :class:`SmsRepositoryPort` owns the registry, the reminder
  settings, the per-contract opt-ins, and the reminder log.
- :class:`SmsProviderClient` is THE provider interface (ADR 0003
  amendment): one ``send`` every provider must satisfy, plus an
  optional status lookup. :class:`SmsClientFactory` builds the
  client for a provider kind from its (decrypted) credentials.

Tenant/installation scoping: providers and settings are
installation-scoped (like payment channels); log rows carry both
tenant and installation ids (like installment receipts). The
repository never commits: the caller (router / worker) owns the
transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from hesabyar.domain.sms.commands import (
    CreateSmsProvider,
    RotateSmsProviderCredentials,
    UpdateSmsProvider,
)
from hesabyar.domain.sms.models import (
    SmsContractReminderSettings,
    SmsDeliveryStatus,
    SmsLogStatus,
    SmsProvider,
    SmsReminderLogEntry,
    SmsReminderSettings,
    SmsReminderTrigger,
)


@dataclass(frozen=True, slots=True)
class SmsSendResult:
    """The outcome of one provider send, normalized across providers.

    ``cost_toman`` is informational and only set when the
    provider's reported cost has a KNOWN unit (Kavenegar reports
    Rial — ``cost / 10``); ``cost_raw`` carries the provider's
    verbatim figure and ``cost_unit`` its unit (``"IRR"`` for
    Kavenegar, ``"unverified"`` for sms.ir, ``None`` when the
    provider reports no cost). No guessed conversions, ever.
    """

    provider_message_id: str
    status: SmsDeliveryStatus
    provider_status_raw: str | None = None
    cost_toman: Decimal | None = None
    cost_raw: str | None = None
    cost_unit: str | None = None


@dataclass(frozen=True, slots=True)
class SmsDeliveryUpdate:
    """A delivery-status lookup result (the raw value is always kept)."""

    provider_message_id: str
    status: SmsDeliveryStatus
    provider_status_raw: str | None = None


class SmsProviderClient(Protocol):
    """THE provider interface (ADR 0003 amendment): one send, every provider.

    Implementations are thin HTTP wrappers (Kavenegar, sms.ir) or
    the simulator. A client is built per send from the provider's
    decrypted credentials and closed afterwards; it never reads a
    credential from anywhere else. Transport failures raise the
    typed ``SmsProvider*`` errors from
    ``hesabyar.domain.sms.errors`` — never a bare httpx error.
    """

    @property
    def provider_kind(self) -> str:
        """The provider kind this client speaks (``kavenegar`` / ``sms_ir`` / ``simulator``)."""
        ...

    def send(self, *, to: str, message: str, sender_line: str | None) -> SmsSendResult:
        """Send one message to one recipient (``to`` is a canonical ``09…`` mobile)."""
        ...

    def get_status(self, provider_message_id: str) -> SmsDeliveryUpdate:
        """Look up a sent message's delivery status at the provider.

        Providers whose delivery-state vocabulary is unverified
        return ``SmsDeliveryStatus.UNKNOWN`` with the raw value
        preserved — never a guessed mapping.
        """
        ...

    def close(self) -> None:
        """Release the client's resources (a no-op for the simulator)."""
        ...


class SmsClientFactory(Protocol):
    """Builds the client for a provider kind from its credentials."""

    def client_for(
        self,
        *,
        provider_kind: str,
        provider_key: str,
        credentials: dict[str, str],
        line_number: str | None,
    ) -> SmsProviderClient:
        """Return the client for ``provider_kind``.

        Raises ``SmsProviderNotConfiguredError`` when a live kind
        lacks its required credentials, and
        ``SmsProviderClientUnavailableError`` for kinds with no
        client (``custom``). The simulator needs no credentials.
        """
        ...


class SmsRepositoryPort(Protocol):
    """HesabYar-side SMS registry + settings + log persistence."""

    # -- providers -----------------------------------------------------

    def create_provider(self, command: CreateSmsProvider) -> SmsProvider:
        """Insert a provider row (the key must be unique per installation)."""
        ...

    def get_provider(
        self, provider_id: str, *, installation_id: str
    ) -> SmsProvider | None:
        """Return one provider, or None when missing / out of scope."""
        ...

    def get_provider_by_key(
        self, provider_key: str, *, installation_id: str
    ) -> SmsProvider | None:
        """Return the provider with this key, or None."""
        ...

    def get_default_provider(self, *, installation_id: str) -> SmsProvider | None:
        """Return the installation's default provider (``is_default``), or None."""
        ...

    def list_providers(self, *, installation_id: str) -> list[SmsProvider]:
        """List the installation's providers, built-ins first, then by key."""
        ...

    def update_provider(self, command: UpdateSmsProvider) -> SmsProvider | None:
        """Apply a patch; returns the updated provider, or None when missing."""
        ...

    def delete_provider(self, provider_id: str, *, installation_id: str) -> bool:
        """Delete a provider row; False when missing / out of scope.

        Log rows keep their denormalized ``provider_key``; their
        ``provider_id`` is set NULL (history survives a delete).
        """
        ...

    def rotate_provider_credentials(
        self, command: RotateSmsProviderCredentials
    ) -> SmsProvider | None:
        """Replace a provider's stored credential ciphertext; None when missing."""
        ...

    def load_provider_credentials(
        self, provider_id: str, *, installation_id: str
    ) -> str | None:
        """Return a provider's credential CIPHERTEXT, or None when unset.

        Plaintext never passes through this port: the service
        decrypts via the vault. Callers must never log the result.
        """
        ...

    def seed_builtin_providers(self, installation_id: str) -> list[SmsProvider]:
        """Create the built-in provider rows (idempotent — re-running returns the same rows)."""
        ...

    # -- reminder settings ----------------------------------------------

    def get_reminder_settings(
        self, *, installation_id: str
    ) -> SmsReminderSettings | None:
        """Return the installation's reminder policy, or None when never configured."""
        ...

    def save_reminder_settings(self, settings: SmsReminderSettings) -> SmsReminderSettings:
        """Insert or replace the installation's reminder policy."""
        ...

    def list_reminder_settings_enabled(self) -> list[SmsReminderSettings]:
        """All installations with automatic reminders enabled (the daily job's scan)."""
        ...

    # -- per-contract opt-ins --------------------------------------------

    def get_contract_settings(
        self, *, installation_id: str, contract_id: str
    ) -> SmsContractReminderSettings | None:
        """Return a contract's reminder opt-in, or None when never configured."""
        ...

    def save_contract_settings(
        self, settings: SmsContractReminderSettings
    ) -> SmsContractReminderSettings:
        """Insert or replace a contract's reminder opt-in."""
        ...

    def list_contract_settings(
        self, *, installation_id: str
    ) -> list[SmsContractReminderSettings]:
        """All opt-in rows of one installation (the daily job's lookup)."""
        ...

    # -- reminder log -----------------------------------------------------

    def insert_log(self, entry: SmsReminderLogEntry) -> SmsReminderLogEntry:
        """Insert one log row.

        Raises ``SmsReminderAlreadySentError`` when the row would
        violate the sent-dedup index (a sent/queued/delivered row
        already exists for this leg + offset).
        """
        ...

    def get_log_entry(
        self, log_id: str, *, installation_id: str
    ) -> SmsReminderLogEntry | None:
        """Return one log row, or None when missing / out of scope."""
        ...

    def find_sent_log(
        self, *, installation_id: str, leg_id: str, offset_days: int
    ) -> SmsReminderLogEntry | None:
        """Return the sent/queued/delivered row for a leg + offset, if any (dedup check)."""
        ...

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
        """List log rows, newest first (all filters optional, AND-combined)."""
        ...
