"""Domain models for the integration registries + policy gate (US-503, Phase 5 F3).

ADR 0003 item 12: payment channels are a configurable registry, not a
hard-coded list. Nothing in this package names a gateway: channel keys
(``zarinpal``, ``sadad``, …) are data rows in the ``payment_channels``
table, seeded per installation by
``hesabyar.infrastructure.persistence.postgres.seed_payment_channels``
(the same seed-data pattern as the tax-policy registry — the registry
table is the source of truth, and no behaviour in this package branches
on a channel key). Ilmaz is NOT a built-in channel (ADR 0003 round-2
item 2: Emalls moves no money); a store that genuinely uses something
called "Ilmaz" adds it as a custom channel with its own contract terms.

Fee percentages are user-entered per channel/contract (ADR 0003
round-2 item 1) and live only on versioned
:class:`GatewayContract` rows — never as constants in code. A rate
change creates a NEW contract version with an effective date; history
is never rewritten.

Money: all amounts are whole Iranian rials (``int`` — ``MoneyIRR``
digits=0); fee percentages are exact ``Decimal`` (``10`` means 10%,
never 0.10, never a binary float — the same convention as the
tax-policy registry).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum, unique

__all__ = [
    "AutoPostPolicy",
    "BankConnection",
    "ChannelStatus",
    "ContractStatus",
    "GatewayContract",
    "Holiday",
    "InstallationSettings",
    "IntegrationMapping",
    "MappingKind",
    "MappingStatus",
    "MappingSuggestion",
    "PaymentChannel",
    "PolicyGateConfig",
    "RESERVED_CHANNEL_KEYS",
    "Store",
    "SuggestionMode",
    "SuggestionStatus",
    "TrancheLeg",
    "validate_tranche_legs",
]


#: Reserved channel keys (US-524): seeded rows that must stay
#: disabled until their integration is verified. ``torob_marketplace``
#: claims the Torob marketplace key so it cannot be re-created as a
#: live channel by mistake; the Torob ORDER API is ⚠️ UNVERIFIED,
#: so the row stays disabled and is never mixed with the ``torobpay``
#: payment channel (phased design §1.5الف). The registry service
#: refuses to enable a reserved key. This set is DATA (like the
#: seed list), not a behaviour branch: it names rows, not logic.
RESERVED_CHANNEL_KEYS: frozenset[str] = frozenset({"torob_marketplace"})

#: Confirmations after which a learned mapping may be promoted
#: Suggest → Auto (US-552, phased design §7.2): a rule starts in
#: SUGGEST (one-click confirm) and only becomes AUTO once the same
#: target has been confirmed this many times. Mirrors the bank
#: rules' ``RULE_AUTO_MIN_CONFIRMATIONS`` — the same threshold for
#: both learning loops, so one number governs "trusted enough".
MAPPING_AUTO_MIN_CONFIRMATIONS = 3


@unique
class MappingKind(StrEnum):
    """The nine mapping screens of spec §10.5 (a closed vocabulary).

    These are schema-level kinds, not data: the mapping center has one
    screen per kind. ``channel_key`` on a mapping scopes it to one
    payment channel (empty string = installation-wide).
    """

    GATEWAY_JOURNAL = "gateway_journal"
    """Payment gateway / payment method → Tryton journal/account."""

    REVENUE_CATEGORY = "revenue_category"
    """Commerce revenue category → revenue account."""

    SHIPPING_CHARGE = "shipping_charge"
    """Shipping charge → configured account/tax treatment."""

    DISCOUNT = "discount"
    """Discount → discount account/policy."""

    REFUND_REASON = "refund_reason"
    """Refund reason → return/adjustment mapping."""

    TAX_CATEGORY = "tax_category"
    """Tax category → effective-dated tax policy."""

    STORE_COMPANY = "store_company"
    """Store ID → Tryton company."""

    EXTERNAL_PARTY = "external_party"
    """External customer/supplier → Tryton party."""

    PRODUCT_DIMENSION = "product_dimension"
    """(Optional) product/category → analytical dimension."""


@unique
class AutoPostPolicy(StrEnum):
    """Auto-post policy carried on a gateway contract (phased design §3.1)."""

    DRAFT_ONLY = "draft_only"
    """Draft only (the default): nothing auto-posts for this channel."""

    AUTO_POST_WITHIN_POLICY = "auto_post_within_policy"
    """Auto-post when the three-layer policy gate allows it (explicit opt-in)."""


@unique
class SuggestionMode(StrEnum):
    """How a mapping rule is applied (Xero bank-rules pattern, §1.4).

    Learned rules start in SUGGEST (one-click confirm) and move to AUTO
    only after the operator trusts them.
    """

    SUGGEST = "suggest"
    AUTO = "auto"


@unique
class MappingStatus(StrEnum):
    """Lifecycle of one mapping version."""

    ACTIVE = "active"
    SUPERSEDED = "superseded"


@unique
class ContractStatus(StrEnum):
    """Lifecycle of one gateway-contract version."""

    ACTIVE = "active"
    SUPERSEDED = "superseded"


@unique
class SuggestionStatus(StrEnum):
    """Lifecycle of one mapping suggestion."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


@unique
class ChannelStatus(StrEnum):
    """Operational status of a payment channel (phased design §2.1)."""

    ACTIVE = "active"
    DISABLED = "disabled"
    SETUP = "setup"
    ERROR = "error"


@dataclass(frozen=True)
class PaymentChannel:
    """One payment channel in an installation's registry.

    ``channel_key`` is the stable slug (data, never an enum — see the
    module docstring). ``is_builtin`` marks the rows the seed creates;
    a built-in channel is born disabled (``enabled=False``,
    ``status=SETUP``) until the operator enables it and attaches a
    contract — fail-closed: a channel with no contract can never
    auto-post. ``channel_type`` is a free string with conventional
    values (``gateway`` / ``bnpl`` / ``marketplace`` / ``custom``);
    it is deliberately NOT an enum, so a new channel kind needs no
    code change.
    """

    id: str
    installation_id: str
    channel_key: str
    display_name_fa: str
    channel_type: str
    is_builtin: bool
    enabled: bool
    status: ChannelStatus
    merchant_ref: str | None = None
    settlement_iban: str | None = None
    environment: str = "production"
    notes: str | None = None
    has_credentials: bool = False
    """True when gateway API credentials are stored (encrypted) for
    this channel. Manual channels (Sadad, SalamPay, custom) have
    none — and no credential material ever reaches this read model."""
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


@dataclass(frozen=True)
class TrancheLeg:
    """One leg of a settlement schedule (phased design §3.1).

    ``month_offset`` counts Jalali months from the sale month (the
    anchor); ``payout_day`` is the day of that month the tranche is
    due; ``percent`` is the tranche's share of the net amount. Legs
    must sum to exactly 100 — see :func:`validate_tranche_legs`.
    """

    month_offset: int
    payout_day: int
    percent: Decimal


def validate_tranche_legs(legs: tuple[TrancheLeg, ...] | list[TrancheLeg]) -> list[str]:
    """Validate a tranche schedule, returning one problem per defect.

    Pure and total: an empty list means the schedule is usable. The
    settlement engine derives one ``settlement_tranche`` row per leg,
    so a bad schedule must be rejected at contract-creation time, not
    discovered at settlement time.
    """
    problems: list[str] = []
    if not legs:
        return ["tranche_legs must contain at least one leg"]
    total = Decimal(0)
    for index, leg in enumerate(legs):
        label = f"leg {index}"
        if leg.month_offset < 0:
            problems.append(f"{label}: month_offset must be >= 0, got {leg.month_offset}")
        if leg.payout_day < 1 or leg.payout_day > 31:
            problems.append(f"{label}: payout_day must be 1..31, got {leg.payout_day}")
        if leg.percent <= 0 or leg.percent > 100:
            problems.append(f"{label}: percent must be in (0, 100], got {leg.percent}")
        total += leg.percent
    if total != Decimal(100):
        problems.append(f"tranche_legs percents must sum to exactly 100, got {total}")
    return problems


@dataclass(frozen=True)
class GatewayContract:
    """One versioned gateway contract (phased design §3.1, ADR 0003 item 1).

    Every change in rate or model is a NEW version with an effective
    date: ``version`` increments per channel, the previous active
    version is superseded (its ``valid_to`` closes the day before the
    new version's ``valid_from``), and the old row's figures are never
    rewritten — past settlements keep the old rate.

    ``settlement_model``, ``fee_timing``, ``fee_accrual_policy`` and
    ``payout_calendar`` are free strings with documented conventional
    values (``tranches_4`` / ``monthly_lump`` / ``daily`` / …;
    ``proportional`` / ``upfront_first_tranche``; ``at_sale`` /
    ``at_settlement``; ``jalali`` / ``gregorian``) — deliberately not
    enums, so a new contract model needs no code change. ``fee_percent``
    is user-entered, never hard-coded; ``fee_vat_claimable`` defaults
    to False until the gateway's official invoice/Moadian evidence is
    confirmed (phased design §3.6).
    """

    id: str
    channel_id: str
    channel_key: str
    version: int
    valid_from: datetime.date
    fee_percent: Decimal
    valid_to: datetime.date | None = None
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
    status: ContractStatus = ContractStatus.ACTIVE
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None

    def is_effective_on(self, on: datetime.date) -> bool:
        """True when this version covers ``on`` (inclusive both ends)."""
        if on < self.valid_from:
            return False
        if self.valid_to is not None and on > self.valid_to:
            return False
        return True


@dataclass(frozen=True)
class IntegrationMapping:
    """One versioned mapping in the mapping center (spec §10.5).

    ``source_key`` is the external key (store SKU, gateway slug,
    revenue-category name, …); ``target_type``/``target_ref`` name the
    Tryton target (``account``/``journal``/``party``/``tax_policy``/
    ``dimension``/``company`` + the engine reference, e.g. an account
    code). ``channel_key`` scopes the mapping to one channel (empty
    string = installation-wide). A new version supersedes the previous
    active one — mappings are versioned, previewable, testable and
    auditable (§10.5), and an unknown mapping fails closed into the
    review queue, never to a default account.

    ``confirmations`` counts how many times the SAME target has been
    confirmed for this (kind, channel, source) — re-creating the
    mapping with an unchanged target increments it; changing the
    target resets it to 1. Promotion Suggest → Auto requires
    ``MAPPING_AUTO_MIN_CONFIRMATIONS`` (US-552, phased design §7.2).
    """

    id: str
    installation_id: str
    mapping_kind: MappingKind
    channel_key: str
    source_key: str
    target_type: str
    target_ref: str
    version: int
    status: MappingStatus = MappingStatus.ACTIVE
    suggestion_mode: SuggestionMode = SuggestionMode.SUGGEST
    source: str = "manual"
    confidence: Decimal | None = None
    notes: str | None = None
    created_by: str | None = None
    confirmations: int = 1
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None

    @property
    def auto_eligible(self) -> bool:
        """True when this mapping may be promoted Suggest → Auto.

        An ACTIVE mapping in ``suggest`` mode with at least
        ``MAPPING_AUTO_MIN_CONFIRMATIONS`` confirmations. The
        promotion itself stays a human decision — this is only the
        eligibility floor.
        """
        return (
            self.status is MappingStatus.ACTIVE
            and self.suggestion_mode is SuggestionMode.SUGGEST
            and self.confirmations >= MAPPING_AUTO_MIN_CONFIRMATIONS
        )


@dataclass(frozen=True)
class MappingSuggestion:
    """A proposed mapping awaiting the operator's decision (§1.4).

    Produced when the worker (or an import) meets an unknown source key
    and can propose a target — e.g. learned from a previously confirmed
    manual mapping. Accepting creates/activates the mapping (in
    ``suggest`` mode first); rejecting records the decision so the
    same suggestion is not proposed again silently.
    """

    id: str
    installation_id: str
    mapping_kind: MappingKind
    channel_key: str
    source_key: str
    suggested_target_type: str
    suggested_target_ref: str
    confidence: Decimal
    basis: str
    status: SuggestionStatus = SuggestionStatus.PENDING
    resolved_by: str | None = None
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


@dataclass(frozen=True)
class PolicyGateConfig:
    """One installation's auto-post policy for one event type (§6.2).

    Layer 1 of the gate is this row: auto-post is allowed for an event
    type only when a config row exists with ``allowed=True`` AND the
    installation-level opt-in (``InstallationSettings.auto_post_enabled``)
    is on. ``max_amount_irr`` is the layer-2 cap for this event type
    (None = no cap at this layer — the gate still requires a cap from
    somewhere: see :mod:`hesabyar.domain.integrations.policy_gate`).
    ``require_tieout`` adds the settlement tie-out check for
    ``payment.settlement.received`` events.
    """

    id: str
    installation_id: str
    event_type: str
    allowed: bool
    max_amount_irr: int | None = None
    require_tieout: bool = False
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


@dataclass(frozen=True)
class BankConnection:
    """One bank connection in an installation's registry (ADR 0003 item 5).

    Per-company: a connection belongs to one HesabYar company mapping
    row (``company_id`` is the ``engine_companies`` row id). Credentials
    are stored ENCRYPTED (see
    :class:`hesabyar.domain.integrations.ports.CredentialVaultPort`) —
    this read model carries only ``has_credentials`` and the key id, so
    no credential material ever reaches a REST response or a log. An
    internet-banking password is NEVER accepted (ADR 0003 decision 10 /
    §8.3): the service rejects credential payloads carrying one.
    Fintech inquiry results are ephemeral (verdict/code/time only) and
    are never stored on this row.
    """

    id: str
    installation_id: str
    company_id: str
    bank_key: str
    display_name: str
    connection_kind: str
    has_credentials: bool
    credentials_key_id: str | None = None
    status: str = "active"
    last_checked_at: datetime.datetime | None = None
    notes: str | None = None
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


@dataclass(frozen=True)
class Store:
    """One store in an installation's registry (phased design §2.1).

    ``store_key`` is the connector name used in
    ``POST /api/v1/integrations/{connector}/events``. ``prices_in_toman``
    is the per-store Toman setting: when True, the connector boundary
    converts Toman → Rial (×10) before the amount enters the event
    envelope (ADR 0003 decision 4). The webhook secret and the scoped
    read-only API key are generated by HesabYar, stored ENCRYPTED, and
    shown to the operator exactly once at creation/rotation — this read
    model carries only ``has_webhook_secret`` / ``has_api_key``.
    ``api_key_scopes`` lists the read-only scopes the store key may
    use (e.g. ``orders:read``); the store key can never write.
    ``has_store_db_dsn`` says whether a read-only database DSN is
    stored for the store (ADR 0003 decision 3: reconciliation/backfill
    only, zero writes) — the DSN itself is never exposed.
    """

    id: str
    installation_id: str
    store_key: str
    display_name: str
    store_type: str
    prices_in_toman: bool
    has_webhook_secret: bool
    has_api_key: bool
    has_store_db_dsn: bool = False
    api_key_scopes: tuple[str, ...] = ()
    api_base_url: str | None = None
    status: str = "active"
    last_webhook_at: datetime.datetime | None = None
    last_poll_at: datetime.datetime | None = None
    notes: str | None = None
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


@dataclass(frozen=True)
class InstallationSettings:
    """Installation-level integration settings (one row per installation).

    ``weekly_holiday`` is the weekday number (Python convention:
    Monday=0 … Sunday=6); Friday (4) is the default weekly holiday in
    Iran. ``matching_window_days`` is the settlement/bank matching
    window in CALENDAR days — until a verified Iranian-holiday source
    is chosen, the window is calendar-based, not business-day-based
    (phased design §6.6 / §8.4). ``auto_post_enabled`` is the global
    auto-post opt-in: draft is the default, and auto-post needs this
    flag AND a per-event-type policy row (gate layer 1).
    """

    installation_id: str
    weekly_holiday: int = 4
    matching_window_days: int = 2
    auto_post_enabled: bool = False
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


@dataclass(frozen=True)
class Holiday:
    """One official holiday date for an installation (configurable list).

    No verified Iranian-holiday API was found (§8.4), so the list is
    operator-maintained until a source is chosen. Dates are Gregorian;
    the holiday calendar (see
    :mod:`hesabyar.domain.integrations.holidays`) combines these with
    the weekly holiday.
    """

    id: str
    installation_id: str
    holiday_date: datetime.date
    title: str | None = None
    created_at: datetime.datetime | None = None
