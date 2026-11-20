"""Request/response schemas for the integrations endpoints (US-501 + US-503).

Event-platform schemas (US-501) + registry/policy-gate schemas (US-503),
one module: the two stories share the ``/integrations`` prefix.

Responses map from the domain views — no ORM object ever leaks. The
ingest request takes the raw envelope as a JSON object: the domain
validator owns the envelope contract (§10.2), not pydantic, so the
endpoint can return the typed ``EVENT_*`` problems.

No credential material ever appears in a response: bank-connection
and store responses carry only ``has_*`` flags. The two exceptions
are the store create/rotate responses, which return the freshly
generated secrets in plaintext EXACTLY ONCE (the operator must copy
them then; no later read returns them).

``fee_percent`` / ``fee_vat_rate`` / ``confidence`` follow the §9.3
contract: Decimals that serialize as decimal strings (never a binary
float — see the tax-policy schemas). Trailing zeros are stripped at
the mapping boundary.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, model_validator

from hesabyar.application.commands.events import IngestResult, WorkerReport
from hesabyar.application.commands.sync import SyncReport
from hesabyar.domain.events.models import (
    AutomationDecision,
    InboxEvent,
    OutboxEvent,
    StoreSettings,
)
from hesabyar.domain.integrations.models import (
    BankConnection,
    GatewayContract,
    Holiday,
    InstallationSettings,
    IntegrationMapping,
    MappingSuggestion,
    PaymentChannel,
    PolicyGateConfig,
    Store,
    TrancheLeg,
)
from hesabyar.domain.integrations.policy_gate import PolicyGateDecision


def _clean_decimal(value: Decimal) -> Decimal:
    """Strip trailing zeros so the §9.3 decimal-string form is clean."""
    return Decimal(format(value.normalize(), "f"))


def _clean_optional_decimal(value: Decimal | None) -> Decimal | None:
    """Strip trailing zeros, preserving None."""
    return _clean_decimal(value) if value is not None else None


# -- payment channels -----------------------------------------------------


class PaymentChannelResponse(BaseModel):
    """One payment channel in the installation's registry."""

    id: str = Field(description="Registry row id of this channel")
    channel_key: str = Field(description="Stable channel slug, e.g. 'zarinpal'")
    display_name_fa: str = Field(description="Persian display name")
    channel_type: str = Field(description="Channel kind: 'gateway' | 'bnpl' | 'marketplace' | 'custom'")
    is_builtin: bool = Field(description="True for the rows the built-in seed creates")
    enabled: bool = Field(description="Operator toggle: a disabled channel never auto-posts")
    status: str = Field(description="Operational status: 'active' | 'disabled' | 'setup' | 'error'")
    merchant_ref: str | None = Field(description="Merchant/terminal identifier (non-secret)")
    settlement_iban: str | None = Field(description="Destination IBAN for settlements")
    environment: str = Field(description="Gateway environment: 'stage' | 'production'")
    notes: str | None = Field(description="Operator notes")
    has_credentials: bool = Field(
        description="True when gateway API credentials are stored (encrypted) for this channel"
    )
    created_at: datetime | None = Field(description="Row creation timestamp (UTC)")
    updated_at: datetime | None = Field(description="Last-modified timestamp (UTC)")

    @classmethod
    def from_channel(cls, channel: PaymentChannel) -> PaymentChannelResponse:
        """Build a channel response from the domain view."""
        return cls(
            id=channel.id,
            channel_key=channel.channel_key,
            display_name_fa=channel.display_name_fa,
            channel_type=channel.channel_type,
            is_builtin=channel.is_builtin,
            enabled=channel.enabled,
            status=channel.status.value,
            merchant_ref=channel.merchant_ref,
            settlement_iban=channel.settlement_iban,
            environment=channel.environment,
            notes=channel.notes,
            has_credentials=channel.has_credentials,
            created_at=channel.created_at,
            updated_at=channel.updated_at,
        )


class CreatePaymentChannelRequest(BaseModel):
    """Create a custom payment channel (built-ins come from the seed)."""

    channel_key: str = Field(description="Stable channel slug (lowercase, digits, '-' or '_')")
    display_name_fa: str = Field(description="Persian display name")
    channel_type: str = Field(description="Channel kind: 'gateway' | 'bnpl' | 'marketplace' | 'custom'")
    merchant_ref: str | None = Field(default=None, description="Merchant/terminal identifier (non-secret)")
    settlement_iban: str | None = Field(default=None, description="Destination IBAN for settlements")
    environment: str = Field(default="production", description="Gateway environment")
    notes: str | None = Field(default=None, description="Operator notes")


class UpdatePaymentChannelRequest(BaseModel):
    """Update a channel's mutable fields (null = unchanged)."""

    display_name_fa: str | None = Field(default=None, description="Persian display name")
    channel_type: str | None = Field(default=None, description="Channel kind")
    enabled: bool | None = Field(default=None, description="Enable/disable the channel")
    merchant_ref: str | None = Field(default=None, description="Merchant/terminal identifier")
    settlement_iban: str | None = Field(default=None, description="Destination IBAN for settlements")
    environment: str | None = Field(default=None, description="Gateway environment")
    notes: str | None = Field(default=None, description="Operator notes")


# -- gateway contracts ----------------------------------------------------


class TrancheLegSchema(BaseModel):
    """One leg of a settlement schedule."""

    month_offset: int = Field(description="Jalali months from the sale month (the anchor), >= 0")
    payout_day: int = Field(description="Day of the payout month the tranche is due (1..31)")
    percent: Decimal = Field(description="Tranche's share of the net amount; legs must sum to 100")

    def to_domain(self) -> TrancheLeg:
        """Build the domain leg."""
        return TrancheLeg(
            month_offset=self.month_offset, payout_day=self.payout_day, percent=self.percent
        )

    @classmethod
    def from_domain(cls, leg: TrancheLeg) -> TrancheLegSchema:
        """Build a leg schema from the domain leg."""
        return cls(
            month_offset=leg.month_offset,
            payout_day=leg.payout_day,
            percent=_clean_decimal(leg.percent),
        )


class GatewayContractResponse(BaseModel):
    """One versioned gateway contract."""

    id: str = Field(description="Registry row id of this version")
    channel_key: str = Field(description="Owning channel's slug")
    version: int = Field(description="Contract version number (1-based, incrementing)")
    valid_from: date = Field(description="First effective date of this version (inclusive)")
    valid_to: date | None = Field(description="Last effective date (inclusive); null while active")
    fee_percent: Decimal = Field(description="User-entered fee percent (10 means 10%)")
    fee_vat_rate: Decimal | None = Field(description="VAT rate on the fee (null = unknown)")
    fee_vat_claimable: bool = Field(description="Whether fee VAT is claimable as input tax")
    fee_timing: str = Field(description="Fee timing: 'proportional' | 'upfront_first_tranche'")
    fee_accrual_policy: str = Field(description="Fee accrual: 'at_sale' | 'at_settlement'")
    settlement_model: str = Field(description="Settlement model, e.g. 'tranches_4' | 'monthly_lump'")
    tranche_legs: list[TrancheLegSchema] = Field(description="Settlement schedule legs")
    payout_window_start_day: int | None = Field(description="First day of the payout window (1..31)")
    payout_window_end_day: int | None = Field(description="Last day of the payout window / due date (1..31)")
    payout_calendar: str = Field(description="Payout calendar: 'jalali' | 'gregorian'")
    working_day_adjustment: bool = Field(description="Move a holiday due date to the next business day")
    day_lag: int | None = Field(description="Day-lag models: business days after confirmation")
    grace_days_after_due: int = Field(description="Grace days after the due date before overdue")
    match_tolerance_irr: int = Field(description="Amount-matching tolerance in rials")
    tieout_tolerance_irr: int = Field(description="Settlement tie-out tolerance in rials")
    auto_post_policy: str = Field(description="Auto-post policy: 'draft_only' | 'auto_post_within_policy'")
    auto_post_max_amount_irr: int | None = Field(description="Auto-post amount cap in rials (null = no cap)")
    source_note: str | None = Field(description="Reference for the contract figures")
    status: str = Field(description="Version lifecycle: 'active' | 'superseded'")
    created_at: datetime | None = Field(description="Row creation timestamp (UTC)")
    updated_at: datetime | None = Field(description="Last-modified timestamp (UTC)")

    @classmethod
    def from_contract(cls, contract: GatewayContract) -> GatewayContractResponse:
        """Build a contract response from the domain view."""
        return cls(
            id=contract.id,
            channel_key=contract.channel_key,
            version=contract.version,
            valid_from=contract.valid_from,
            valid_to=contract.valid_to,
            fee_percent=_clean_decimal(contract.fee_percent),
            fee_vat_rate=_clean_optional_decimal(contract.fee_vat_rate),
            fee_vat_claimable=contract.fee_vat_claimable,
            fee_timing=contract.fee_timing,
            fee_accrual_policy=contract.fee_accrual_policy,
            settlement_model=contract.settlement_model,
            tranche_legs=[TrancheLegSchema.from_domain(leg) for leg in contract.tranche_legs],
            payout_window_start_day=contract.payout_window_start_day,
            payout_window_end_day=contract.payout_window_end_day,
            payout_calendar=contract.payout_calendar,
            working_day_adjustment=contract.working_day_adjustment,
            day_lag=contract.day_lag,
            grace_days_after_due=contract.grace_days_after_due,
            match_tolerance_irr=contract.match_tolerance_irr,
            tieout_tolerance_irr=contract.tieout_tolerance_irr,
            auto_post_policy=contract.auto_post_policy.value,
            auto_post_max_amount_irr=contract.auto_post_max_amount_irr,
            source_note=contract.source_note,
            status=contract.status.value,
            created_at=contract.created_at,
            updated_at=contract.updated_at,
        )


class CreateGatewayContractRequest(BaseModel):
    """Create a NEW contract version (a rate change is always a new version)."""

    valid_from: date = Field(description="First effective date of the new version (inclusive)")
    fee_percent: Decimal = Field(description="User-entered fee percent (10 means 10%)")
    fee_vat_rate: Decimal | None = Field(default=None, description="VAT rate on the fee")
    fee_vat_claimable: bool = Field(default=False, description="Whether fee VAT is claimable")
    fee_timing: str = Field(default="proportional", description="Fee timing")
    fee_accrual_policy: str = Field(default="at_sale", description="Fee accrual policy")
    settlement_model: str = Field(default="tranches_4", description="Settlement model")
    tranche_legs: list[TrancheLegSchema] = Field(description="Settlement schedule legs (must sum to 100)")
    payout_window_start_day: int | None = Field(default=None, description="First day of the payout window")
    payout_window_end_day: int | None = Field(default=None, description="Last day of the payout window")
    payout_calendar: str = Field(default="jalali", description="Payout calendar")
    working_day_adjustment: bool = Field(default=False, description="Move a holiday due date forward")
    day_lag: int | None = Field(default=None, description="Day-lag for day-based models")
    grace_days_after_due: int = Field(default=0, description="Grace days after the due date")
    match_tolerance_irr: int = Field(default=0, description="Amount-matching tolerance in rials")
    tieout_tolerance_irr: int = Field(default=0, description="Settlement tie-out tolerance in rials")
    auto_post_policy: str = Field(default="draft_only", description="Auto-post policy")
    auto_post_max_amount_irr: int | None = Field(default=None, description="Auto-post amount cap in rials")
    source_note: str | None = Field(default=None, description="Reference for the contract figures")


# -- mapping center -------------------------------------------------------


class IntegrationMappingResponse(BaseModel):
    """One versioned mapping in the mapping center."""

    id: str = Field(description="Registry row id of this version")
    mapping_kind: str = Field(description="Mapping kind: one of the nine spec §10.5 kinds")
    channel_key: str = Field(description="Owning channel key, or '' for installation-wide")
    source_key: str = Field(description="External key (store SKU, gateway slug, category name, ...)")
    target_type: str = Field(description="Tryton target kind")
    target_ref: str = Field(description="Engine reference of the target")
    version: int = Field(description="Mapping version number (1-based, incrementing)")
    status: str = Field(description="Version lifecycle: 'active' | 'superseded'")
    suggestion_mode: str = Field(description="How the rule applies: 'suggest' | 'auto'")
    source: str = Field(description="Mapping origin: 'manual' | 'learned' | 'imported'")
    confidence: Decimal | None = Field(description="Confidence 0..1 for learned mappings")
    notes: str | None = Field(description="Operator notes")
    created_by: str | None = Field(description="Actor that created this version")
    created_at: datetime | None = Field(description="Row creation timestamp (UTC)")
    updated_at: datetime | None = Field(description="Last-modified timestamp (UTC)")

    @classmethod
    def from_mapping(cls, mapping: IntegrationMapping) -> IntegrationMappingResponse:
        """Build a mapping response from the domain view."""
        return cls(
            id=mapping.id,
            mapping_kind=mapping.mapping_kind.value,
            channel_key=mapping.channel_key,
            source_key=mapping.source_key,
            target_type=mapping.target_type,
            target_ref=mapping.target_ref,
            version=mapping.version,
            status=mapping.status.value,
            suggestion_mode=mapping.suggestion_mode.value,
            source=mapping.source,
            confidence=_clean_optional_decimal(mapping.confidence),
            notes=mapping.notes,
            created_by=mapping.created_by,
            created_at=mapping.created_at,
            updated_at=mapping.updated_at,
        )


class CreateIntegrationMappingRequest(BaseModel):
    """Create a NEW mapping version (supersedes the active one, if any)."""

    mapping_kind: str = Field(description="Mapping kind: one of the nine spec §10.5 kinds")
    channel_key: str = Field(default="", description="Owning channel key, or '' for installation-wide")
    source_key: str = Field(description="External key to map")
    target_type: str = Field(description="Tryton target kind")
    target_ref: str = Field(description="Engine reference of the target")
    suggestion_mode: str = Field(default="suggest", description="How the rule applies: 'suggest' | 'auto'")
    source: str = Field(default="manual", description="Mapping origin")
    confidence: Decimal | None = Field(default=None, description="Confidence 0..1 for learned mappings")
    notes: str | None = Field(default=None, description="Operator notes")


class MappingSuggestionResponse(BaseModel):
    """A proposed mapping awaiting the operator's decision."""

    id: str = Field(description="Registry row id of this suggestion")
    mapping_kind: str = Field(description="Mapping kind: one of the nine spec §10.5 kinds")
    channel_key: str = Field(description="Owning channel key, or '' for installation-wide")
    source_key: str = Field(description="External key the suggestion is about")
    suggested_target_type: str = Field(description="Proposed Tryton target kind")
    suggested_target_ref: str = Field(description="Proposed engine reference of the target")
    confidence: Decimal = Field(description="Suggestion confidence 0..1")
    basis: str = Field(description="Why this target was suggested")
    status: str = Field(description="Suggestion lifecycle: 'pending' | 'accepted' | 'rejected'")
    resolved_by: str | None = Field(description="Actor that accepted/rejected the suggestion")
    created_at: datetime | None = Field(description="Row creation timestamp (UTC)")
    updated_at: datetime | None = Field(description="Last-modified timestamp (UTC)")

    @classmethod
    def from_suggestion(cls, suggestion: MappingSuggestion) -> MappingSuggestionResponse:
        """Build a suggestion response from the domain view."""
        return cls(
            id=suggestion.id,
            mapping_kind=suggestion.mapping_kind.value,
            channel_key=suggestion.channel_key,
            source_key=suggestion.source_key,
            suggested_target_type=suggestion.suggested_target_type,
            suggested_target_ref=suggestion.suggested_target_ref,
            confidence=_clean_decimal(suggestion.confidence),
            basis=suggestion.basis,
            status=suggestion.status.value,
            resolved_by=suggestion.resolved_by,
            created_at=suggestion.created_at,
            updated_at=suggestion.updated_at,
        )


class CreateMappingSuggestionRequest(BaseModel):
    """Record a proposed mapping (idempotent per pending suggestion)."""

    mapping_kind: str = Field(description="Mapping kind: one of the nine spec §10.5 kinds")
    channel_key: str = Field(default="", description="Owning channel key, or '' for installation-wide")
    source_key: str = Field(description="External key the suggestion is about")
    suggested_target_type: str = Field(description="Proposed Tryton target kind")
    suggested_target_ref: str = Field(description="Proposed engine reference of the target")
    confidence: Decimal = Field(description="Suggestion confidence 0..1")
    basis: str = Field(description="Why this target was suggested")


# -- policy gate ----------------------------------------------------------


class PolicyGateConfigResponse(BaseModel):
    """One installation's auto-post policy for one event type."""

    id: str = Field(description="Registry row id of this policy")
    event_type: str = Field(description="Canonical event type, e.g. 'payment.captured'")
    allowed: bool = Field(description="Whether this event type may auto-post")
    max_amount_irr: int | None = Field(description="Layer-2 amount cap in rials (null = no policy cap)")
    require_tieout: bool = Field(description="Whether settlement tie-out must hold before auto-post")
    created_at: datetime | None = Field(description="Row creation timestamp (UTC)")
    updated_at: datetime | None = Field(description="Last-modified timestamp (UTC)")

    @classmethod
    def from_config(cls, config: PolicyGateConfig) -> PolicyGateConfigResponse:
        """Build a policy response from the domain view."""
        return cls(
            id=config.id,
            event_type=config.event_type,
            allowed=config.allowed,
            max_amount_irr=config.max_amount_irr,
            require_tieout=config.require_tieout,
            created_at=config.created_at,
            updated_at=config.updated_at,
        )


class UpsertPolicyGateConfigRequest(BaseModel):
    """Create or replace one installation's policy row for an event type."""

    allowed: bool = Field(description="Whether this event type may auto-post")
    max_amount_irr: int | None = Field(default=None, description="Layer-2 amount cap in rials")
    require_tieout: bool = Field(default=False, description="Whether tie-out must hold before auto-post")


class PolicyGateEvaluateRequest(BaseModel):
    """Dry-run the policy gate for one event (no posting happens)."""

    event_type: str = Field(description="Canonical event type, e.g. 'payment.captured'")
    company_id: str = Field(description="API company id (HesabYar row id or engine company id)")
    event_date: date = Field(description="Event date (drives the fiscal-period lock)")
    amount_irr: int = Field(description="Event amount in whole rials (drives the amount cap)")
    channel_key: str | None = Field(default=None, description="Payment channel key, when the event has one")
    tieout_ok: bool | None = Field(
        default=None, description="Settlement tie-out result (only when the policy requires it)"
    )


class GateLayerResultResponse(BaseModel):
    """The outcome of one gate layer."""

    layer: str = Field(description="Layer name: 'event_type' | 'amount_cap' | 'period_lock' | 'tieout' | 'channel'")
    passed: bool = Field(description="Whether the layer passed")
    detail: str = Field(description="Human-readable explanation of the layer's outcome")


class PolicyGateDecisionResponse(BaseModel):
    """A policy-gate evaluation: allowed or denied, with the evidence."""

    allowed: bool = Field(description="Whether auto-post is allowed for this event")
    action: str = Field(description="'auto_post' when allowed, 'draft' otherwise")
    layers: list[GateLayerResultResponse] = Field(description="Per-layer results")
    policy_config_id: str | None = Field(description="Policy row id the decision used (null = no policy)")
    contract_id: str | None = Field(description="Contract row id the decision used (null = no contract)")
    contract_version: int | None = Field(description="Contract version the decision used")
    reason: str = Field(description="Human-readable summary of the decision")
    evaluated_at: datetime = Field(description="Evaluation timestamp (UTC)")

    @classmethod
    def from_decision(cls, decision: PolicyGateDecision) -> PolicyGateDecisionResponse:
        """Build a decision response from the domain decision."""
        return cls(
            allowed=decision.allowed,
            action=decision.action,
            layers=[
                GateLayerResultResponse(layer=layer.layer, passed=layer.passed, detail=layer.detail)
                for layer in decision.layers
            ],
            policy_config_id=decision.policy_config_id,
            contract_id=decision.contract_id,
            contract_version=decision.contract_version,
            reason=decision.reason,
            evaluated_at=decision.evaluated_at,
        )


# -- bank connections -----------------------------------------------------


class BankConnectionResponse(BaseModel):
    """One bank connection (no credential material — only flags)."""

    id: str = Field(description="Registry row id of this connection")
    company_id: str = Field(description="HesabYar engine_companies row id this connection belongs to")
    bank_key: str = Field(description="Stable bank slug, e.g. 'melli'")
    display_name: str = Field(description="Persian display name")
    connection_kind: str = Field(description="Connection kind: 'manual_import' | 'api_oauth' | 'aggregator' | ...")
    has_credentials: bool = Field(description="Whether encrypted credentials are stored")
    credentials_key_id: str | None = Field(description="Id of the encryption key version in use")
    status: str = Field(description="Connection status: 'active' | 'disabled' | 'error'")
    last_checked_at: datetime | None = Field(description="Last successful connectivity check (UTC)")
    notes: str | None = Field(description="Operator notes")
    created_at: datetime | None = Field(description="Row creation timestamp (UTC)")
    updated_at: datetime | None = Field(description="Last-modified timestamp (UTC)")

    @classmethod
    def from_connection(cls, connection: BankConnection) -> BankConnectionResponse:
        """Build a connection response from the domain view."""
        return cls(
            id=connection.id,
            company_id=connection.company_id,
            bank_key=connection.bank_key,
            display_name=connection.display_name,
            connection_kind=connection.connection_kind,
            has_credentials=connection.has_credentials,
            credentials_key_id=connection.credentials_key_id,
            status=connection.status,
            last_checked_at=connection.last_checked_at,
            notes=connection.notes,
            created_at=connection.created_at,
            updated_at=connection.updated_at,
        )


class CreateBankConnectionRequest(BaseModel):
    """Create a bank connection (credentials are encrypted at rest).

    ``credentials`` holds OAuth/API credential fields (client id,
    client secret, tokens). An internet-banking password is NEVER
    accepted — any field containing "password" is rejected with 422.
    """

    company_id: str = Field(description="API company id (HesabYar row id or engine company id)")
    bank_key: str = Field(description="Stable bank slug, e.g. 'melli'")
    display_name: str = Field(description="Persian display name")
    connection_kind: str = Field(description="Connection kind: 'manual_import' | 'api_oauth' | 'aggregator' | ...")
    credentials: dict[str, str] | None = Field(
        default=None, description="Credential fields to store encrypted (never a password)"
    )
    notes: str | None = Field(default=None, description="Operator notes")


class UpdateBankConnectionRequest(BaseModel):
    """Update a connection's mutable fields (null = unchanged; never credentials)."""

    display_name: str | None = Field(default=None, description="Persian display name")
    connection_kind: str | None = Field(default=None, description="Connection kind")
    status: str | None = Field(default=None, description="Connection status")
    notes: str | None = Field(default=None, description="Operator notes")


class RotateBankCredentialsRequest(BaseModel):
    """Replace a connection's stored credentials (encrypted at rest)."""

    credentials: dict[str, str] = Field(description="New credential fields (never a password)")


# -- stores ---------------------------------------------------------------


class StoreResponse(BaseModel):
    """One store (no secret material — only flags)."""

    id: str = Field(description="Registry row id of this store")
    store_key: str = Field(description="Stable store slug, used as {connector} in the events endpoint")
    display_name: str = Field(description="Persian display name")
    store_type: str = Field(description="Store kind: 'custom' | 'woocommerce' | 'sazito' | ...")
    api_base_url: str | None = Field(description="Store API base URL for reconciliation polling")
    prices_in_toman: bool = Field(description="Per-store Toman setting (True = convert Toman to Rial x10)")
    has_webhook_secret: bool = Field(description="Whether an encrypted webhook secret is stored")
    has_api_key: bool = Field(description="Whether an encrypted read-only API key is stored")
    has_store_db_dsn: bool = Field(description="True when a read-only store-database DSN is stored")
    api_key_scopes: list[str] = Field(description="Read-only scopes of the store API key")
    status: str = Field(description="Store status: 'active' | 'disabled' | 'error'")
    last_webhook_at: datetime | None = Field(description="Last webhook received from this store (UTC)")
    last_poll_at: datetime | None = Field(description="Last successful reconciliation poll (UTC)")
    notes: str | None = Field(description="Operator notes")
    created_at: datetime | None = Field(description="Row creation timestamp (UTC)")
    updated_at: datetime | None = Field(description="Last-modified timestamp (UTC)")

    @classmethod
    def from_store(cls, store: Store) -> StoreResponse:
        """Build a store response from the domain view."""
        return cls(
            id=store.id,
            store_key=store.store_key,
            display_name=store.display_name,
            store_type=store.store_type,
            api_base_url=store.api_base_url,
            prices_in_toman=store.prices_in_toman,
            has_webhook_secret=store.has_webhook_secret,
            has_api_key=store.has_api_key,
            has_store_db_dsn=store.has_store_db_dsn,
            api_key_scopes=list(store.api_key_scopes),
            status=store.status,
            last_webhook_at=store.last_webhook_at,
            last_poll_at=store.last_poll_at,
            notes=store.notes,
            created_at=store.created_at,
            updated_at=store.updated_at,
        )


class CreateStoreRequest(BaseModel):
    """Create a store (HesabYar generates its webhook secret + API key)."""

    store_key: str = Field(description="Stable store slug (lowercase, digits, '-' or '_')")
    display_name: str = Field(description="Persian display name")
    store_type: str = Field(description="Store kind: 'custom' | 'woocommerce' | 'sazito' | ...")
    prices_in_toman: bool = Field(default=True, description="Per-store Toman setting")
    api_key_scopes: list[str] | None = Field(
        default=None, description="Read-only scopes for the store API key (default: orders/payments/cheques)"
    )
    api_base_url: str | None = Field(default=None, description="Store API base URL for polling")
    notes: str | None = Field(default=None, description="Operator notes")


class UpdateStoreRequest(BaseModel):
    """Update a store's mutable fields (null = unchanged; never secrets)."""

    display_name: str | None = Field(default=None, description="Persian display name")
    store_type: str | None = Field(default=None, description="Store kind")
    prices_in_toman: bool | None = Field(default=None, description="Per-store Toman setting")
    api_key_scopes: list[str] | None = Field(default=None, description="Read-only scopes for the store API key")
    api_base_url: str | None = Field(default=None, description="Store API base URL for polling")
    status: str | None = Field(default=None, description="Store status")
    notes: str | None = Field(default=None, description="Operator notes")


class StoreSecretsResponse(BaseModel):
    """A store plus freshly generated secrets — returned EXACTLY ONCE.

    The plaintext secrets exist only in this response: the operator
    must copy them now. No later read returns them. ``webhook_secret``
    is empty on an API-key rotation (and vice versa).
    """

    store: StoreResponse = Field(description="The store row")
    webhook_secret: str = Field(description="Plaintext HMAC webhook secret (copy now — never shown again)")
    api_key: str = Field(description="Plaintext scoped read-only API key (copy now — never shown again)")


class RotateStoreApiKeyRequest(BaseModel):
    """Rotate a store's read-only API key (optionally changing its scopes)."""

    api_key_scopes: list[str] | None = Field(
        default=None, description="New read-only scopes (null = keep the current scopes)"
    )


class SetStoreDbCredentialsRequest(BaseModel):
    """Store a store's read-only database DSN (encrypted, never returned).

    The DSN is supplied by the store team: it authenticates as their
    read-only DB user (ADR 0003 decision 3 — reconciliation/backfill
    only, zero writes). Accepted schemes: ``postgresql://`` and
    ``sqlite://`` (sqlite is dev/test only).
    """

    dsn: str = Field(description="Read-only database DSN, e.g. postgresql://hesabyar_ro:***@host:5432/store")


# -- installation settings + holidays -------------------------------------


class InstallationSettingsResponse(BaseModel):
    """Installation-level integration settings."""

    installation_id: str = Field(description="Installation id")
    weekly_holiday: int = Field(description="Weekly holiday weekday number (Monday=0 … Sunday=6; Friday=4)")
    matching_window_days: int = Field(description="Settlement/bank matching window in CALENDAR days (±N days)")
    auto_post_enabled: bool = Field(description="Global auto-post opt-in (False = draft only)")
    created_at: datetime | None = Field(description="Row creation timestamp (UTC)")
    updated_at: datetime | None = Field(description="Last-modified timestamp (UTC)")

    @classmethod
    def from_settings(cls, settings: InstallationSettings) -> InstallationSettingsResponse:
        """Build a settings response from the domain view."""
        return cls(
            installation_id=settings.installation_id,
            weekly_holiday=settings.weekly_holiday,
            matching_window_days=settings.matching_window_days,
            auto_post_enabled=settings.auto_post_enabled,
            created_at=settings.created_at,
            updated_at=settings.updated_at,
        )


class UpsertInstallationSettingsRequest(BaseModel):
    """Create or replace one installation's settings row."""

    weekly_holiday: int = Field(default=4, description="Weekly holiday weekday number (Monday=0 … Sunday=6)")
    matching_window_days: int = Field(default=2, description="Matching window in CALENDAR days (±N days)")
    auto_post_enabled: bool = Field(default=False, description="Global auto-post opt-in")


class HolidayResponse(BaseModel):
    """One official holiday date for an installation."""

    id: str = Field(description="Registry row id of this holiday")
    holiday_date: date = Field(description="Holiday date (Gregorian)")
    title: str | None = Field(description="Holiday name, e.g. 'نوروز'")
    created_at: datetime | None = Field(description="Row creation timestamp (UTC)")

    @classmethod
    def from_holiday(cls, holiday: Holiday) -> HolidayResponse:
        """Build a holiday response from the domain view."""
        return cls(
            id=holiday.id,
            holiday_date=holiday.holiday_date,
            title=holiday.title,
            created_at=holiday.created_at,
        )


class AddHolidayRequest(BaseModel):
    """Add a holiday date to an installation's list (idempotent)."""

    holiday_date: date = Field(description="Holiday date (Gregorian)")
    title: str | None = Field(default=None, description="Holiday name, e.g. 'نوروز'")

# ---------------------------------------------------------------------------
# Event platform (US-501) — intake + inbox/outbox/audit/settings schemas
# ---------------------------------------------------------------------------


class InboxEventResponse(BaseModel):
    """One inbox event (the stored raw envelope + processing state)."""

    inbox_event_id: str = Field(description="Inbox event id")
    schema_version: int = Field(description="Canonical event schema version")
    source_system: str = Field(description="Source system key, e.g. 'hayat'")
    source_store_id: str = Field(description="Source store id within the source system")
    source_event_id: str = Field(description="Source event id — external reference")
    event_type: str = Field(description="Canonical event type, e.g. 'sales.order.completed'")
    occurred_at: datetime = Field(description="When the event occurred in the source system (UTC)")
    received_at: datetime = Field(description="When HesabYar received the event (UTC)")
    company_mapping: dict[str, Any] | None = Field(description="Resolved company mapping, if any")
    currency: str | None = Field(description="Event currency code, e.g. 'IRR'")
    external_entity_type: str | None = Field(description="Source entity type, e.g. 'order'")
    external_entity_id: str | None = Field(description="Source entity id — external reference")
    correlation_id: str | None = Field(description="Correlation id across services/events")
    idempotency_key: str | None = Field(description="Business idempotency key supplied by the source")
    payload: dict[str, Any] = Field(description="Raw event payload (as received, unconverted)")
    payload_hash: str = Field(description="SHA-256 hex of the canonical raw payload")
    status: str = Field(
        description="Processing status: received/processing/processed/failed/needs_review/dead_lettered"
    )
    attempts: int = Field(description="Number of processing attempts so far")
    last_attempt_at: datetime | None = Field(description="Last processing attempt (UTC)")
    next_attempt_at: datetime | None = Field(description="Next scheduled retry (UTC)")
    last_error: str | None = Field(description="Last processing error message")
    processed_at: datetime | None = Field(description="Successful processing timestamp (UTC)")
    dead_lettered_at: datetime | None = Field(description="Dead-letter timestamp (UTC)")
    created_at: datetime = Field(description="Row creation timestamp (UTC)")
    updated_at: datetime = Field(description="Last-modified timestamp (UTC)")

    @classmethod
    def from_event(cls, event: InboxEvent) -> InboxEventResponse:
        """Map a domain inbox event onto the response."""
        return cls(
            inbox_event_id=event.id,
            schema_version=event.schema_version,
            source_system=event.source_system,
            source_store_id=event.source_store_id,
            source_event_id=event.source_event_id,
            event_type=event.event_type,
            occurred_at=event.occurred_at,
            received_at=event.received_at,
            company_mapping=dict(event.company_mapping) if event.company_mapping is not None else None,
            currency=event.currency,
            external_entity_type=event.external_entity_type,
            external_entity_id=event.external_entity_id,
            correlation_id=event.correlation_id,
            idempotency_key=event.idempotency_key,
            payload=dict(event.payload),
            payload_hash=event.payload_hash,
            status=event.status.value,
            attempts=event.attempts,
            last_attempt_at=event.last_attempt_at,
            next_attempt_at=event.next_attempt_at,
            last_error=event.last_error,
            processed_at=event.processed_at,
            dead_lettered_at=event.dead_lettered_at,
            created_at=event.created_at,
            updated_at=event.updated_at,
        )


class IngestEventResponse(BaseModel):
    """Result of ingesting one event through the inbound API."""

    status: str = Field(description="'created' for a new event, 'duplicate' for a re-send with the same hash")
    inbox_event_id: str = Field(description="Inbox event id")
    inbox_status: str = Field(description="Inbox processing status")
    signature_verified: bool = Field(description="True when the HMAC signature was verified")
    signature_note: str = Field(description="How the signature was (or was not) verified")
    derived_inbox_event_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Inbox ids of the canonical events a store webhook fanned out to "
            "(US-511: refund.created ingests sales.refund.created + payment.refunded); "
            "empty when the webhook mapped to a single event"
        ),
    )

    @classmethod
    def from_result(cls, result: IngestResult) -> IngestEventResponse:
        """Map an ingest result onto the response."""
        return cls(
            status=result.status,
            inbox_event_id=result.event.id,
            inbox_status=result.event.status.value,
            signature_verified=result.signature_verified,
            signature_note=result.signature_note,
            derived_inbox_event_ids=[event.id for event in result.derived_events],
        )


class SyncStoreRequest(BaseModel):
    """Poll one store's changes and ingest the missed events (US-512, S2).

    ``updated_since`` and ``backfill_days`` are mutually exclusive:
    give at most one. With neither (and no ``cursor``), the run looks
    back 7 days. ``cursor`` resumes a previous run from its
    ``next_cursor``. ``source`` selects the transport: ``api`` polls
    the store's HTTP changes API (needs ``api_base_url`` + the
    read-only API key); ``db`` reads the store database directly,
    read-only (needs a stored read-only DSN).
    """

    source_store_id: str = Field(description="Store id within the source system")
    updated_since: datetime | None = Field(
        default=None, description="Only changes at or after this time (ISO 8601)"
    )
    backfill_days: int | None = Field(
        default=None, ge=1, le=365, description="Backfill window in days (1-365); alternative to updated_since"
    )
    cursor: str | None = Field(default=None, description="Opaque cursor from a previous run's next_cursor")
    page_size: int = Field(default=100, ge=1, le=500, description="Rows per polling page (1-500)")
    source: str = Field(
        default="api", description="Sync transport: 'api' (HTTP changes API) or 'db' (read-only DB read)"
    )
    max_pages: int = Field(default=10, ge=1, le=50, description="Max pages fetched in this run (1-50)")

    @model_validator(mode="after")
    def _exclusive_window(self) -> SyncStoreRequest:
        if self.updated_since is not None and self.backfill_days is not None:
            raise ValueError("updated_since and backfill_days are mutually exclusive; give at most one.")
        return self


class SyncItemErrorResponse(BaseModel):
    """One polled event that could not be ingested (recorded, run continued)."""

    source_event_id: str = Field(description="Store event id of the failed event")
    code: str = Field(description="Typed error code, e.g. 'IDEMPOTENCY_CONFLICT'")
    message: str = Field(description="Human-readable failure reason")


class SyncStoreResponse(BaseModel):
    """Outcome of one sync run over one store."""

    source_system: str = Field(description="Connector key that was synced")
    source_store_id: str = Field(description="Store id that was synced")
    connector_kind: str = Field(description="Transport used: 'api' or 'db'")
    status: str = Field(description="'completed' when the run finished; 'partial' when a later page failed")
    pages_fetched: int = Field(description="Pages fetched in this run")
    events_created: int = Field(description="Polled events newly ingested into the inbox")
    events_duplicate: int = Field(description="Polled events already in the inbox (same idempotency key)")
    changes_unmapped: int = Field(
        description="Changes listed but not ingested (no canonical event type yet, e.g. cheques)"
    )
    events_by_type: dict[str, int] = Field(description="Ingested events counted by canonical event type")
    errors: list[SyncItemErrorResponse] = Field(description="Per-event failures (first 20)")
    error_count: int = Field(description="Total per-event failures in the run")
    error: str | None = Field(description="Run-level failure (partial runs only); retry from next_cursor")
    next_cursor: str | None = Field(description="Opaque cursor to resume with (null when the run is complete)")
    has_more: bool = Field(description="True when more pages remain (resume with next_cursor)")
    started_at: datetime | None = Field(description="Run start (UTC)")
    finished_at: datetime | None = Field(description="Run end (UTC)")

    @classmethod
    def from_report(cls, report: SyncReport) -> SyncStoreResponse:
        """Map a sync report onto the response."""
        return cls(
            source_system=report.source_system,
            source_store_id=report.source_store_id,
            connector_kind=report.connector_kind,
            status=report.status,
            pages_fetched=report.pages_fetched,
            events_created=report.events_created,
            events_duplicate=report.events_duplicate,
            changes_unmapped=report.changes_unmapped,
            events_by_type=dict(report.events_by_type),
            errors=[
                SyncItemErrorResponse(
                    source_event_id=item.source_event_id,
                    code=item.code,
                    message=item.message,
                )
                for item in report.errors
            ],
            error_count=report.error_count,
            error=report.error,
            next_cursor=report.next_cursor,
            has_more=report.has_more,
            started_at=report.started_at,
            finished_at=report.finished_at,
        )


class OutboxEventResponse(BaseModel):
    """One transactional-outbox event."""

    outbox_event_id: str = Field(description="Outbox event id")
    aggregate_type: str = Field(description="Aggregate type, e.g. 'inbox_event'")
    aggregate_id: str = Field(description="Aggregate id the event belongs to")
    event_type: str = Field(description="Event type, e.g. 'events.processed'")
    payload: dict[str, Any] = Field(description="Event payload")
    payload_hash: str | None = Field(description="SHA-256 hex of the canonical payload")
    status: str = Field(description="Delivery status: pending/published/failed")
    attempts: int = Field(description="Number of delivery attempts so far")
    last_attempt_at: datetime | None = Field(description="Last delivery attempt (UTC)")
    last_error: str | None = Field(description="Last delivery error message")
    published_at: datetime | None = Field(description="Successful publication timestamp (UTC)")
    correlation_id: str | None = Field(description="Correlation id across services/events")
    created_at: datetime = Field(description="Row creation timestamp (UTC)")
    updated_at: datetime = Field(description="Last-modified timestamp (UTC)")

    @classmethod
    def from_event(cls, event: OutboxEvent) -> OutboxEventResponse:
        """Map a domain outbox event onto the response."""
        return cls(
            outbox_event_id=event.id,
            aggregate_type=event.aggregate_type,
            aggregate_id=event.aggregate_id,
            event_type=event.event_type,
            payload=dict(event.payload),
            payload_hash=event.payload_hash,
            status=event.status.value,
            attempts=event.attempts,
            last_attempt_at=event.last_attempt_at,
            last_error=event.last_error,
            published_at=event.published_at,
            correlation_id=event.correlation_id,
            created_at=event.created_at,
            updated_at=event.updated_at,
        )


class GateResultResponse(BaseModel):
    """One policy-gate verdict."""

    name: str = Field(description="Gate name, e.g. 'event_type' / 'amount_cap' / 'period_lock'")
    passed: bool = Field(description="Whether the gate passed")
    reason: str = Field(description="Human-readable verdict reason")


class AutomationDecisionResponse(BaseModel):
    """One automation-decision audit-log entry."""

    decision_id: str = Field(description="Decision id")
    inbox_event_id: str = Field(description="Inbox event the decision was made for")
    event_type: str = Field(description="Canonical event type")
    decision: str = Field(description="Decision: draft/auto_post/needs_review/dead_letter/replayed")
    policy_version: str = Field(description="Policy version the decision used")
    mapping_version: str | None = Field(description="Mapping version the decision used")
    gate_results: list[GateResultResponse] = Field(description="Per-gate verdicts")
    decided_by: str = Field(description="Decision actor: 'system' or an API client id")
    decided_at: datetime = Field(description="Decision timestamp (UTC)")
    correlation_id: str | None = Field(description="Correlation id across services/events")
    details: dict[str, Any] = Field(description="Decision details: reason, refs, error codes")

    @classmethod
    def from_decision(cls, decision: AutomationDecision) -> AutomationDecisionResponse:
        """Map a domain decision onto the response."""
        return cls(
            decision_id=decision.id,
            inbox_event_id=decision.inbox_event_id,
            event_type=decision.event_type,
            decision=decision.decision.value,
            policy_version=decision.policy_version,
            mapping_version=decision.mapping_version,
            gate_results=[
                GateResultResponse(name=gate.name, passed=gate.passed, reason=gate.reason)
                for gate in decision.gate_results
            ],
            decided_by=decision.decided_by,
            decided_at=decision.decided_at,
            correlation_id=decision.correlation_id,
            details=dict(decision.details),
        )


class StoreSettingsRequest(BaseModel):
    """Per-store connector settings (ADR 0003 decision 4)."""

    prices_in_toman: bool = Field(
        description="True when the store's prices are in Toman (convert ×10 at the boundary)"
    )
    currency: str = Field(default="IRR", description="Store currency code, e.g. 'IRR'")


class StoreSettingsResponse(BaseModel):
    """A store's connector settings."""

    source_system: str = Field(description="Source system key, e.g. 'hayat'")
    source_store_id: str = Field(description="Source store id within the source system")
    prices_in_toman: bool = Field(description="True when the store's prices are in Toman")
    currency: str = Field(description="Store currency code")
    updated_at: datetime | None = Field(description="Last-modified timestamp (UTC)")

    @classmethod
    def from_settings(cls, settings: StoreSettings) -> StoreSettingsResponse:
        """Map domain store settings onto the response."""
        return cls(
            source_system=settings.source_system,
            source_store_id=settings.source_store_id,
            prices_in_toman=settings.prices_in_toman,
            currency=settings.currency,
            updated_at=settings.updated_at,
        )


class ReplayResponse(BaseModel):
    """Result of a manual DLQ replay."""

    inbox_event_id: str = Field(description="Inbox event id")
    status: str = Field(description="Inbox status after the replay ('received')")
    attempts: int = Field(description="Attempt count after the replay (reset to 0)")

    @classmethod
    def from_event(cls, event: InboxEvent) -> ReplayResponse:
        """Map a replayed inbox event onto the response."""
        return cls(inbox_event_id=event.id, status=event.status.value, attempts=event.attempts)


class StaleInboxResponse(BaseModel):
    """Inbox staleness alert: events stuck longer than the threshold."""

    stale_after_hours: int = Field(description="Staleness threshold in hours")
    stale_count: int = Field(description="Number of stuck inbox events")
    events: list[InboxEventResponse] = Field(description="Stuck events, oldest first")


class WorkerPollResponse(BaseModel):
    """Outcome of one manual worker sweep."""

    claimed: int = Field(description="Inbox events claimed in this sweep")
    processed_draft: int = Field(description="Events processed to a draft")
    processed_posted: int = Field(description="Events processed and auto-posted")
    needs_review: int = Field(description="Events routed to the review queue")
    dead_lettered: int = Field(description="Events dead-lettered in this sweep")
    retries_scheduled: int = Field(description="Events scheduled for retry")
    outbox_published: int = Field(description="Outbox events published")
    outbox_failed: int = Field(description="Outbox events that failed to publish")
    errors: list[str] = Field(description="Unexpected per-event errors (the pipeline handles its own)")

    @classmethod
    def from_report(cls, report: WorkerReport) -> WorkerPollResponse:
        """Map a worker report onto the response."""
        return cls(
            claimed=report.claimed,
            processed_draft=report.processed_draft,
            processed_posted=report.processed_posted,
            needs_review=report.needs_review,
            dead_lettered=report.dead_lettered,
            retries_scheduled=report.retries_scheduled,
            outbox_published=report.outbox_published,
            outbox_failed=report.outbox_failed,
            errors=list(report.errors),
        )
