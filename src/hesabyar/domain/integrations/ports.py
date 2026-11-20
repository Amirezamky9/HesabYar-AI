"""Ports for the integrations bounded context (US-503, Phase 5 F3).

Domain and application code depend only on these protocols. The
production implementations live in infrastructure:

- :class:`IntegrationsRegistryPort` →
  ``hesabyar.infrastructure.persistence.postgres.integrations_repository``
  (SQLAlchemy over the registry tables; the ONLY place SQL appears).
- :class:`FiscalPeriodCheckerPort` →
  ``hesabyar.infrastructure.accounting.tryton.fiscal_period_checker``
  (reads fiscal periods through the ``AccountingEnginePort`` — no
  direct SQL to the Tryton database, ever; the Phase 4 port-bypass
  lesson).
- :class:`CredentialVaultPort` →
  ``hesabyar.infrastructure.auth.credential_vault`` (Fernet symmetric
  encryption for stored credentials).

All ports speak domain types only — no ORM models, no sessions — so
the application layer never imports infrastructure. All ports are
synchronous (there is no async infrastructure in the repository yet).
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from typing import Protocol

from hesabyar.domain.accounting.refs import CompanyRef
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


class IntegrationsRegistryPort(Protocol):
    """Persistence boundary for the integration registries.

    Every read/write is scoped by ``installation_id`` (the caller's
    installation, from the authenticated API key): a row of another
    installation looks exactly like a row that does not exist.
    Repositories flush on save (so ids exist) but never commit — the
    caller owns the transaction.
    """

    # -- payment channels -------------------------------------------------

    def list_channels(self, installation_id: str) -> Sequence[PaymentChannel]:
        """Return the installation's channels, ordered by channel key."""
        ...

    def get_channel(self, installation_id: str, channel_key: str) -> PaymentChannel | None:
        """Return one channel, or None for an unknown key in this installation."""
        ...

    def get_channel_by_id(self, channel_id: str) -> PaymentChannel | None:
        """Return one channel by row id, or None for an unknown id."""
        ...

    def save_channel(self, command: CreatePaymentChannel) -> PaymentChannel:
        """Persist a new (custom) channel row and return it."""
        ...

    def update_channel(self, command: UpdatePaymentChannel) -> PaymentChannel | None:
        """Apply a channel update; None when the channel does not exist."""
        ...

    def rotate_channel_credentials(
        self, command: RotateChannelCredentials
    ) -> PaymentChannel | None:
        """Replace a channel's stored gateway credentials; None when missing.

        US-524: the ciphertext is already encrypted (Fernet, via the
        credential vault); the repository stores it verbatim.
        """
        ...

    def get_channel_ciphertext(self, channel_id: str) -> str | None:
        """Return a channel's stored ciphertext, or None.

        For the gateway reconciliation worker only — never exposed
        through REST. The caller decrypts via the credential vault.
        """
        ...

    def delete_channel(self, installation_id: str, channel_key: str) -> bool:
        """Delete a channel row; False when it does not exist."""
        ...

    def seed_builtin_channels(self, installation_id: str) -> Sequence[PaymentChannel]:
        """Idempotently create the built-in channel rows for an installation.

        The built-in list is seed DATA (see
        ``seed_payment_channels``); this method is the only place the
        application reaches it, and no behaviour branches on it.
        Re-running returns the same rows (keyed by channel key).
        """
        ...

    # -- gateway contracts ------------------------------------------------

    def list_contracts(self, channel_id: str) -> Sequence[GatewayContract]:
        """Return a channel's contract versions, newest version first."""
        ...

    def get_contract(self, channel_id: str, version: int) -> GatewayContract | None:
        """Return one contract version, or None for an unknown version."""
        ...

    def get_contract_by_id(self, contract_id: str) -> GatewayContract | None:
        """Return one contract version by row id, or None for an unknown id.

        The settlement engine re-derives a payment's schedule from the
        version the payment was BUILT from (``gateway_payment
        .contract_version_id``) — never from the active version — so
        it resolves contracts by id, not by (channel, version).
        """
        ...

    def get_active_contract(self, channel_id: str) -> GatewayContract | None:
        """Return the channel's active contract version, or None."""
        ...

    def get_active_contract_for_channel_key(
        self, installation_id: str, channel_key: str
    ) -> GatewayContract | None:
        """Return the active contract for a channel key, or None.

        None covers both "unknown channel" and "channel without a
        contract" — the policy gate treats both as "no contract".
        """
        ...

    def save_contract(self, command: CreateGatewayContract) -> GatewayContract:
        """Persist a new contract version row and return it."""
        ...

    def supersede_contract(self, contract_id: str, valid_to: datetime.date) -> None:
        """Mark a contract superseded and close its effective range.

        Only the status + ``valid_to`` change; the version's figures
        are never rewritten.
        """
        ...

    def count_contracts(self, channel_id: str) -> int:
        """Return how many contract versions a channel has."""
        ...

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
        """Return mappings, optionally filtered (see the service)."""
        ...

    def get_mapping(self, mapping_id: str) -> IntegrationMapping | None:
        """Return one mapping by row id, or None for an unknown id."""
        ...

    def resolve_mapping(
        self,
        installation_id: str,
        mapping_kind: MappingKind,
        channel_key: str,
        source_key: str,
    ) -> IntegrationMapping | None:
        """Return the ACTIVE mapping for a (kind, channel, source), or None.

        None means "unknown mapping" — the caller fails closed into
        the review queue, never to a default account (spec §10.5).
        """
        ...

    def save_mapping(self, command: CreateIntegrationMapping) -> IntegrationMapping:
        """Persist a new mapping version row and return it."""
        ...

    def supersede_mapping(self, mapping_id: str) -> None:
        """Mark a mapping version superseded (status change only)."""
        ...

    def count_mappings_for_channel(self, installation_id: str, channel_key: str) -> int:
        """Return how many mappings reference a channel key."""
        ...

    # -- mapping suggestions ----------------------------------------------

    def list_suggestions(
        self,
        installation_id: str,
        *,
        status: SuggestionStatus | None = None,
    ) -> Sequence[MappingSuggestion]:
        """Return suggestions, optionally filtered by status."""
        ...

    def get_suggestion(self, suggestion_id: str) -> MappingSuggestion | None:
        """Return one suggestion by row id, or None for an unknown id."""
        ...

    def get_pending_suggestion(
        self,
        installation_id: str,
        mapping_kind: MappingKind,
        channel_key: str,
        source_key: str,
    ) -> MappingSuggestion | None:
        """Return the pending suggestion for a (kind, channel, source), or None.

        At most one pending suggestion exists per (kind, channel,
        source) — recording the same suggestion twice returns the
        existing one instead of spamming duplicates.
        """
        ...

    def save_suggestion(self, command: CreateMappingSuggestion) -> MappingSuggestion:
        """Persist a new pending suggestion and return it."""
        ...

    def resolve_suggestion(
        self,
        suggestion_id: str,
        status: SuggestionStatus,
        resolved_by: str | None,
    ) -> MappingSuggestion | None:
        """Mark a suggestion accepted/rejected; None when it does not exist."""
        ...

    # -- policy gate config ------------------------------------------------

    def list_policy_configs(self, installation_id: str) -> Sequence[PolicyGateConfig]:
        """Return the installation's policy rows, ordered by event type."""
        ...

    def get_policy_config(self, installation_id: str, event_type: str) -> PolicyGateConfig | None:
        """Return one policy row, or None when the event type has no policy."""
        ...

    def save_policy_config(self, command: UpsertPolicyGateConfig) -> PolicyGateConfig:
        """Create or replace a policy row and return it."""
        ...

    # -- bank connections --------------------------------------------------

    def list_bank_connections(
        self, installation_id: str, *, company_id: str | None = None
    ) -> Sequence[BankConnection]:
        """Return the installation's bank connections, optionally per company."""
        ...

    def get_bank_connection(
        self, installation_id: str, connection_id: str
    ) -> BankConnection | None:
        """Return one bank connection, or None for an unknown id."""
        ...

    def save_bank_connection(self, command: CreateBankConnection) -> BankConnection:
        """Persist a new bank connection and return it (no credential material)."""
        ...

    def update_bank_connection(self, command: UpdateBankConnection) -> BankConnection | None:
        """Apply a bank-connection update; None when it does not exist."""
        ...

    def rotate_bank_connection_credentials(
        self, command: RotateBankConnectionCredentials
    ) -> BankConnection | None:
        """Replace stored credentials; None when the connection does not exist."""
        ...

    def delete_bank_connection(self, installation_id: str, connection_id: str) -> bool:
        """Delete a bank connection; False when it does not exist."""
        ...

    def get_bank_connection_ciphertext(self, connection_id: str) -> str | None:
        """Return a connection's stored ciphertext, or None.

        For future bank-sync workers only — never exposed through REST.
        The caller decrypts via the credential vault.
        """
        ...

    # -- stores ------------------------------------------------------------

    def list_stores(self, installation_id: str) -> Sequence[Store]:
        """Return the installation's stores, ordered by store key."""
        ...

    def get_store(self, installation_id: str, store_key: str) -> Store | None:
        """Return one store, or None for an unknown key in this installation."""
        ...

    def save_store(self, command: CreateStore) -> Store:
        """Persist a new store and return it (no secret material)."""
        ...

    def update_store(self, command: UpdateStore) -> Store | None:
        """Apply a store update; None when the store does not exist."""
        ...

    def rotate_store_webhook_secret(self, command: RotateStoreWebhookSecret) -> Store | None:
        """Replace a store's webhook secret; None when the store does not exist."""
        ...

    def rotate_store_api_key(self, command: RotateStoreApiKey) -> Store | None:
        """Replace a store's read-only API key; None when it does not exist."""
        ...

    def set_store_db_dsn(self, command: SetStoreDbDsn) -> Store | None:
        """Store a store's read-only DB DSN (encrypted); None when it does not exist."""
        ...

    def delete_store(self, installation_id: str, store_key: str) -> bool:
        """Delete a store; False when it does not exist."""
        ...

    def get_store_ciphertext(self, store_id: str, kind: str) -> str | None:
        """Return a store's stored ciphertext (``kind``: webhook_secret | api_key | store_db_dsn).

        For the webhook receiver / polling worker only — never exposed
        through REST. The caller decrypts via the credential vault.
        """
        ...

    # -- installation settings + holidays ----------------------------------

    def get_installation_settings(self, installation_id: str) -> InstallationSettings | None:
        """Return an installation's settings row, or None when unset."""
        ...

    def save_installation_settings(self, command: UpsertInstallationSettings) -> InstallationSettings:
        """Create or replace an installation's settings row and return it."""
        ...

    def list_holidays(self, installation_id: str) -> Sequence[Holiday]:
        """Return an installation's holidays, ordered by date."""
        ...

    def add_holiday(self, command: AddHoliday) -> Holiday:
        """Add a holiday date and return it."""
        ...

    def delete_holiday(self, installation_id: str, holiday_date: datetime.date) -> bool:
        """Delete a holiday date; False when it was not listed."""
        ...


class FiscalPeriodCheckerPort(Protocol):
    """Engine-backed fiscal-period state for the policy gate (layer 3).

    The implementation reads periods through the
    ``AccountingEnginePort`` (``list_periods``) — no direct SQL to the
    Tryton database. Fail-closed: any uncertainty (no period contains
    the date, the engine is unreachable, the read fails) answers
    False — the gate never auto-posts into a period it cannot prove
    open.
    """

    def is_period_open(self, company: CompanyRef, on: datetime.date) -> bool:
        """True only when ``on`` falls in an OPEN fiscal period of ``company``."""
        ...


class CredentialVaultPort(Protocol):
    """Symmetric encryption for stored credentials (bank/store secrets).

    The implementation is Fernet (AES-128-CBC + HMAC-SHA256) over a
    key from settings — production secrets live in the Vault, never in
    the repository (ADR 0003 decision 10). ``key_id`` names the key
    version so a future rotation can keep decrypting old rows.
    """

    @property
    def key_id(self) -> str:
        """The id of the encryption key this vault uses."""
        ...

    def encrypt(self, plaintext: str) -> str:
        """Encrypt ``plaintext`` and return the ciphertext (URL-safe text)."""
        ...

    def decrypt(self, ciphertext: str) -> str:
        """Decrypt ``ciphertext`` back to plaintext.

        Raises ``ValueError`` for a ciphertext this vault cannot
        decrypt (wrong key, tampered data) — callers fail closed.
        """
        ...
