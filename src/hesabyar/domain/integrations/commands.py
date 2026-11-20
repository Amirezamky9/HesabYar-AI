"""Commands for the integrations bounded context (US-503, Phase 5 F3).

A command is the already-validated input to a write: the application
service validates, resolves versions and builds the command, and the
repository persists it. Commands carry domain types only — no ORM, no
infrastructure. Credential material travels ONLY as ciphertext
(``*_ciphertext`` fields, produced by the credential vault); plaintext
secrets never enter a command.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from decimal import Decimal

from hesabyar.domain.integrations.models import (
    AutoPostPolicy,
    MappingKind,
    SuggestionMode,
    TrancheLeg,
)

__all__ = [
    "AddHoliday",
    "CreateBankConnection",
    "CreateGatewayContract",
    "CreateIntegrationMapping",
    "CreateMappingSuggestion",
    "CreatePaymentChannel",
    "CreateStore",
    "RotateBankConnectionCredentials",
    "RotateChannelCredentials",
    "RotateStoreApiKey",
    "RotateStoreWebhookSecret",
    "UpdateBankConnection",
    "UpdatePaymentChannel",
    "UpdateStore",
    "UpsertInstallationSettings",
    "UpsertPolicyGateConfig",
]


@dataclass(frozen=True)
class CreatePaymentChannel:
    """Create a channel row (built-in rows come from the seed, not this command)."""

    installation_id: str
    channel_key: str
    display_name_fa: str
    channel_type: str
    merchant_ref: str | None = None
    settlement_iban: str | None = None
    environment: str = "production"
    notes: str | None = None


@dataclass(frozen=True)
class UpdatePaymentChannel:
    """Update a channel's mutable fields (key + builtin flag never change).

    ``status`` is derived by the service from ``enabled`` + whether an
    active contract exists — it is not set directly.
    """

    installation_id: str
    channel_key: str
    display_name_fa: str | None = None
    channel_type: str | None = None
    enabled: bool | None = None
    status: str | None = None
    merchant_ref: str | None = None
    settlement_iban: str | None = None
    environment: str | None = None
    notes: str | None = None


@dataclass(frozen=True)
class RotateChannelCredentials:
    """Replace a channel's stored gateway credentials (already encrypted).

    US-524: the ciphertext is the Fernet encryption of the JSON
    credential payload (secrets + optional non-secret overrides).
    Plaintext secrets never enter a command.
    """

    installation_id: str
    channel_key: str
    credentials_ciphertext: str
    credentials_key_id: str


@dataclass(frozen=True)
class CreateGatewayContract:
    """Create a NEW contract version for a channel.

    The service assigns ``version`` (max + 1) and supersedes the
    previous active version; the repository never rewrites an existing
    version row. ``fee_percent`` is user-entered, never hard-coded.
    """

    channel_id: str
    version: int
    valid_from: datetime.date
    fee_percent: Decimal
    fee_vat_rate: Decimal | None = None
    fee_vat_claimable: bool = False
    fee_timing: str = "proportional"
    fee_accrual_policy: str = "at_sale"
    settlement_model: str = "tranches_4"
    tranche_legs: tuple[TrancheLeg, ...] = ()
    payout_window_start_day: int | None = None
    payout_window_end_day: int | None = None
    payout_calendar: str = "jalali"
    working_day_adjustment: bool = False
    day_lag: int | None = None
    grace_days_after_due: int = 0
    match_tolerance_irr: int = 0
    tieout_tolerance_irr: int = 0
    auto_post_policy: AutoPostPolicy = AutoPostPolicy.DRAFT_ONLY
    auto_post_max_amount_irr: int | None = None
    source_note: str | None = None


@dataclass(frozen=True)
class CreateIntegrationMapping:
    """Create a NEW mapping version (supersedes the active one, if any)."""

    installation_id: str
    mapping_kind: MappingKind
    channel_key: str
    source_key: str
    target_type: str
    target_ref: str
    version: int
    suggestion_mode: SuggestionMode = SuggestionMode.SUGGEST
    source: str = "manual"
    confidence: Decimal | None = None
    notes: str | None = None
    created_by: str | None = None


@dataclass(frozen=True)
class CreateMappingSuggestion:
    """Record a proposed mapping for the operator to accept or reject."""

    installation_id: str
    mapping_kind: MappingKind
    channel_key: str
    source_key: str
    suggested_target_type: str
    suggested_target_ref: str
    confidence: Decimal
    basis: str


@dataclass(frozen=True)
class UpsertPolicyGateConfig:
    """Create or replace one installation's policy row for an event type."""

    installation_id: str
    event_type: str
    allowed: bool
    max_amount_irr: int | None = None
    require_tieout: bool = False


@dataclass(frozen=True)
class CreateBankConnection:
    """Create a bank connection; credentials arrive already encrypted."""

    installation_id: str
    company_id: str
    bank_key: str
    display_name: str
    connection_kind: str
    credentials_ciphertext: str | None = None
    credentials_key_id: str | None = None
    status: str = "active"
    notes: str | None = None


@dataclass(frozen=True)
class UpdateBankConnection:
    """Update a bank connection's mutable fields (never its credentials)."""

    installation_id: str
    connection_id: str
    display_name: str | None = None
    connection_kind: str | None = None
    status: str | None = None
    last_checked_at: datetime.datetime | None = None
    notes: str | None = None


@dataclass(frozen=True)
class RotateBankConnectionCredentials:
    """Replace a connection's stored credentials (already encrypted)."""

    installation_id: str
    connection_id: str
    credentials_ciphertext: str
    credentials_key_id: str


@dataclass(frozen=True)
class CreateStore:
    """Create a store; the webhook secret + API key arrive already encrypted."""

    installation_id: str
    store_key: str
    display_name: str
    store_type: str
    prices_in_toman: bool
    webhook_secret_ciphertext: str
    api_key_ciphertext: str
    api_key_scopes: tuple[str, ...] = ()
    api_base_url: str | None = None
    status: str = "active"
    notes: str | None = None


@dataclass(frozen=True)
class UpdateStore:
    """Update a store's mutable fields (never its secrets)."""

    installation_id: str
    store_key: str
    display_name: str | None = None
    store_type: str | None = None
    prices_in_toman: bool | None = None
    api_key_scopes: tuple[str, ...] | None = None
    api_base_url: str | None = None
    status: str | None = None
    last_webhook_at: datetime.datetime | None = None
    last_poll_at: datetime.datetime | None = None
    notes: str | None = None


@dataclass(frozen=True)
class RotateStoreWebhookSecret:
    """Replace a store's webhook secret (already encrypted)."""

    installation_id: str
    store_key: str
    webhook_secret_ciphertext: str


@dataclass(frozen=True)
class RotateStoreApiKey:
    """Replace a store's read-only API key (already encrypted)."""

    installation_id: str
    store_key: str
    api_key_ciphertext: str
    api_key_scopes: tuple[str, ...] = ()


@dataclass(frozen=True)
class SetStoreDbDsn:
    """Store a store's read-only database DSN (already encrypted).

    The DSN is supplied by the store team (it authenticates as their
    read-only DB user — ADR 0003 decision 3) and is never returned by
    any read: the read model carries only ``has_store_db_dsn``.
    """

    installation_id: str
    store_key: str
    dsn_ciphertext: str


@dataclass(frozen=True)
class UpsertInstallationSettings:
    """Create or replace one installation's settings row."""

    installation_id: str
    weekly_holiday: int = 4
    matching_window_days: int = 2
    auto_post_enabled: bool = False


@dataclass(frozen=True)
class AddHoliday:
    """Add one holiday date to an installation's list."""

    installation_id: str
    holiday_date: datetime.date
    title: str | None = None
