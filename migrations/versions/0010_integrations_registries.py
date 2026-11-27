"""Integration registries + policy gate (US-503, Phase 5 F3).

Revision ID: 0010
Revises: 0008
Create Date: 2026-11-18

ADR 0003 item 12 + phased design §1.2 F3: payment channels, versioned
gateway contracts, the mapping center (spec §10.5), the three-layer
policy gate, the bank-connection and store registries, and the
Iranian holiday calendar — all as configurable registries, so nothing
about a gateway or a rate is ever hard-coded.

- ``payment_channels``: one row per channel per installation
  (``channel_key`` is data, never an enum; the built-in rows come
  from the seed, not from this migration). Ilmaz is NOT a built-in
  channel (ADR 0003 round-2 item 2).
- ``gateway_contracts``: versioned contracts (fee %, settlement
  model ``tranche_legs``, payment window, tolerances, auto-post
  policy). A rate change is a NEW version; history is never
  rewritten.
- ``integration_mappings`` + ``mapping_suggestions``: the mapping
  center (§10.5) with versioning and Suggest → Auto suggestions.
  At most one PENDING suggestion per (kind, channel, source)
  (partial unique index).
- ``auto_post_policies``: the policy-gate config (one row per
  installation + event type).
- ``bank_connections``: per-company bank connections with ENCRYPTED
  credentials (ciphertext only — never an internet-banking password).
- ``stores``: per-installation stores with encrypted webhook secret +
  scoped read-only API key.
- ``installation_settings`` + ``installation_holidays``: the
  installation-level settings (Friday weekly holiday by default,
  calendar-day matching window) and the operator-maintained holiday
  list (no verified holiday API exists yet — §8.4).

Chains after 0009 (US-501 event platform).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "payment_channels",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key"),
        sa.Column(
            "installation_id",
            sa.String(36),
            nullable=False,
            comment="Installation id (from the authenticated API key); the registry is per-installation",
        ),
        sa.Column(
            "channel_key",
            sa.String(64),
            nullable=False,
            comment="Stable channel slug, e.g. 'zarinpal' (data — never an enum)",
        ),
        sa.Column(
            "display_name_fa",
            sa.String(256),
            nullable=False,
            comment="Persian display name, e.g. 'زرین‌پال'",
        ),
        sa.Column(
            "channel_type",
            sa.String(64),
            nullable=False,
            comment="Channel kind as a free string: 'gateway' | 'bnpl' | 'marketplace' | 'custom'",
        ),
        sa.Column(
            "is_builtin",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="True for the rows the built-in seed creates",
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="Operator toggle: a disabled channel never auto-posts",
        ),
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default="setup",
            comment="Operational status: 'active' | 'disabled' | 'setup' | 'error'",
        ),
        sa.Column(
            "merchant_ref",
            sa.String(256),
            nullable=True,
            comment="Merchant/terminal identifier (non-secret)",
        ),
        sa.Column(
            "settlement_iban",
            sa.String(64),
            nullable=True,
            comment="Destination IBAN for settlements (in the merchant's name)",
        ),
        sa.Column(
            "environment",
            sa.String(32),
            nullable=False,
            server_default="production",
            comment="Gateway environment: 'stage' | 'production'",
        ),
        sa.Column("notes", sa.Text(), nullable=True, comment="Operator notes"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Row creation timestamp (UTC)",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Last-modified timestamp (UTC)",
        ),
        sa.UniqueConstraint(
            "installation_id", "channel_key", name="uq_payment_channels_installation_key"
        ),
    )
    op.create_index(
        "ix_payment_channels_installation", "payment_channels", ["installation_id"]
    )

    op.create_table(
        "gateway_contracts",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key"),
        sa.Column(
            "channel_id",
            sa.String(36),
            sa.ForeignKey("payment_channels.id", ondelete="CASCADE"),
            nullable=False,
            comment="Owning payment channel row",
        ),
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            comment="Contract version number, incrementing per channel (1-based)",
        ),
        sa.Column(
            "valid_from",
            sa.Date(),
            nullable=False,
            comment="First effective date of this version (inclusive)",
        ),
        sa.Column(
            "valid_to",
            sa.Date(),
            nullable=True,
            comment="Last effective date (inclusive); NULL while the version is active",
        ),
        sa.Column(
            "fee_percent",
            sa.Numeric(8, 4),
            nullable=False,
            comment="User-entered fee percent (10 means 10%) — never hard-coded",
        ),
        sa.Column(
            "fee_vat_rate",
            sa.Numeric(8, 4),
            nullable=True,
            comment="VAT rate on the fee, from the active tax policy (NULL = unknown)",
        ),
        sa.Column(
            "fee_vat_claimable",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="Whether fee VAT is claimable as input tax (False until official evidence)",
        ),
        sa.Column(
            "fee_timing",
            sa.String(32),
            nullable=False,
            server_default="proportional",
            comment="Fee timing: 'proportional' (per tranche) | 'upfront_first_tranche'",
        ),
        sa.Column(
            "fee_accrual_policy",
            sa.String(32),
            nullable=False,
            server_default="at_sale",
            comment="Fee accrual: 'at_sale' (accrual, default) | 'at_settlement' (cash-basis)",
        ),
        sa.Column(
            "settlement_model",
            sa.String(64),
            nullable=False,
            server_default="tranches_4",
            comment="Settlement model as a free string, e.g. 'tranches_4' | 'monthly_lump' | 'daily'",
        ),
        sa.Column(
            "tranche_legs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
            comment="Settlement schedule: [{month_offset, payout_day, percent}] (percent as string)",
        ),
        sa.Column(
            "payout_window_start_day",
            sa.Integer(),
            nullable=True,
            comment="First day of the payout window in the payout month (1..31)",
        ),
        sa.Column(
            "payout_window_end_day",
            sa.Integer(),
            nullable=True,
            comment="Last day of the payout window (the due date) in the payout month (1..31)",
        ),
        sa.Column(
            "payout_calendar",
            sa.String(32),
            nullable=False,
            server_default="jalali",
            comment="Payout calendar: 'jalali' | 'gregorian'",
        ),
        sa.Column(
            "working_day_adjustment",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="When True, a due date falling on a holiday moves to the next business day",
        ),
        sa.Column(
            "day_lag",
            sa.Integer(),
            nullable=True,
            comment="Day-lag settlement models: business days after confirmation (NULL = month-based)",
        ),
        sa.Column(
            "grace_days_after_due",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="Grace days after the due date before a tranche counts overdue",
        ),
        sa.Column(
            "match_tolerance_irr",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
            comment="Amount-matching tolerance in rials",
        ),
        sa.Column(
            "tieout_tolerance_irr",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
            comment="Settlement tie-out tolerance in rials",
        ),
        sa.Column(
            "auto_post_policy",
            sa.String(32),
            nullable=False,
            server_default="draft_only",
            comment="Auto-post policy: 'draft_only' (default) | 'auto_post_within_policy'",
        ),
        sa.Column(
            "auto_post_max_amount_irr",
            sa.BigInteger(),
            nullable=True,
            comment="Auto-post amount cap for this contract in rials (NULL = no contract cap)",
        ),
        sa.Column(
            "source_note",
            sa.Text(),
            nullable=True,
            comment="Reference for the contract figures (contract number, signature date, file name)",
        ),
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default="active",
            comment="Version lifecycle: 'active' | 'superseded'",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Row creation timestamp (UTC)",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Last-modified timestamp (UTC)",
        ),
        sa.UniqueConstraint(
            "channel_id", "version", name="uq_gateway_contracts_channel_version"
        ),
    )
    op.create_index(
        "ix_gateway_contracts_channel_status", "gateway_contracts", ["channel_id", "status"]
    )

    op.create_table(
        "integration_mappings",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key"),
        sa.Column(
            "installation_id",
            sa.String(36),
            nullable=False,
            comment="Installation id (from the authenticated API key)",
        ),
        sa.Column(
            "mapping_kind",
            sa.String(64),
            nullable=False,
            comment="Mapping kind: one of the nine spec §10.5 kinds",
        ),
        sa.Column(
            "channel_key",
            sa.String(64),
            nullable=False,
            server_default="",
            comment="Owning channel key, or '' for an installation-wide mapping",
        ),
        sa.Column(
            "source_key",
            sa.String(512),
            nullable=False,
            comment="External key (store SKU, gateway slug, revenue-category name, ...)",
        ),
        sa.Column(
            "target_type",
            sa.String(64),
            nullable=False,
            comment="Tryton target kind: 'account' | 'journal' | 'party' | 'tax_policy' | 'dimension' | 'company'",
        ),
        sa.Column(
            "target_ref",
            sa.String(512),
            nullable=False,
            comment="Engine reference of the target (e.g. an account code)",
        ),
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            comment="Mapping version number, incrementing per (kind, channel, source) (1-based)",
        ),
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default="active",
            comment="Version lifecycle: 'active' | 'superseded'",
        ),
        sa.Column(
            "suggestion_mode",
            sa.String(32),
            nullable=False,
            server_default="suggest",
            comment="How the rule applies: 'suggest' (confirm first) | 'auto'",
        ),
        sa.Column(
            "source",
            sa.String(32),
            nullable=False,
            server_default="manual",
            comment="Mapping origin: 'manual' | 'learned' | 'imported'",
        ),
        sa.Column(
            "confidence",
            sa.Numeric(5, 4),
            nullable=True,
            comment="Confidence 0..1 for learned mappings (NULL for manual ones)",
        ),
        sa.Column("notes", sa.Text(), nullable=True, comment="Operator notes"),
        sa.Column(
            "created_by",
            sa.String(256),
            nullable=True,
            comment="Actor that created this version (user id or key name)",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Row creation timestamp (UTC)",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Last-modified timestamp (UTC)",
        ),
        sa.UniqueConstraint(
            "installation_id",
            "mapping_kind",
            "channel_key",
            "source_key",
            "version",
            name="uq_integration_mappings_identity_version",
        ),
    )
    op.create_index(
        "ix_integration_mappings_resolve",
        "integration_mappings",
        ["installation_id", "mapping_kind", "channel_key", "source_key", "status"],
    )

    op.create_table(
        "mapping_suggestions",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key"),
        sa.Column(
            "installation_id",
            sa.String(36),
            nullable=False,
            comment="Installation id (from the authenticated API key)",
        ),
        sa.Column(
            "mapping_kind",
            sa.String(64),
            nullable=False,
            comment="Mapping kind: one of the nine spec §10.5 kinds",
        ),
        sa.Column(
            "channel_key",
            sa.String(64),
            nullable=False,
            server_default="",
            comment="Owning channel key, or '' for an installation-wide mapping",
        ),
        sa.Column(
            "source_key",
            sa.String(512),
            nullable=False,
            comment="External key the suggestion is about",
        ),
        sa.Column(
            "suggested_target_type",
            sa.String(64),
            nullable=False,
            comment="Proposed Tryton target kind",
        ),
        sa.Column(
            "suggested_target_ref",
            sa.String(512),
            nullable=False,
            comment="Proposed engine reference of the target",
        ),
        sa.Column(
            "confidence",
            sa.Numeric(5, 4),
            nullable=False,
            comment="Suggestion confidence 0..1",
        ),
        sa.Column(
            "basis",
            sa.String(256),
            nullable=False,
            comment="Why this target was suggested (e.g. 'learned_from_manual_mapping')",
        ),
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default="pending",
            comment="Suggestion lifecycle: 'pending' | 'accepted' | 'rejected'",
        ),
        sa.Column(
            "resolved_by",
            sa.String(256),
            nullable=True,
            comment="Actor that accepted/rejected the suggestion",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Row creation timestamp (UTC)",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Last-modified timestamp (UTC)",
        ),
    )
    op.create_index(
        "uq_mapping_suggestions_pending",
        "mapping_suggestions",
        ["installation_id", "mapping_kind", "channel_key", "source_key"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_mapping_suggestions_installation_status",
        "mapping_suggestions",
        ["installation_id", "status"],
    )

    op.create_table(
        "auto_post_policies",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key"),
        sa.Column(
            "installation_id",
            sa.String(36),
            nullable=False,
            comment="Installation id (from the authenticated API key)",
        ),
        sa.Column(
            "event_type",
            sa.String(128),
            nullable=False,
            comment="Canonical event type, e.g. 'payment.captured'",
        ),
        sa.Column(
            "allowed",
            sa.Boolean(),
            nullable=False,
            comment="Whether this event type may auto-post (explicit opt-in per type)",
        ),
        sa.Column(
            "max_amount_irr",
            sa.BigInteger(),
            nullable=True,
            comment="Layer-2 amount cap for this event type in rials (NULL = no policy cap)",
        ),
        sa.Column(
            "require_tieout",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="Whether settlement tie-out must hold before auto-post (settlement events)",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Row creation timestamp (UTC)",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Last-modified timestamp (UTC)",
        ),
        sa.UniqueConstraint(
            "installation_id", "event_type", name="uq_auto_post_policies_installation_event"
        ),
    )

    op.create_table(
        "bank_connections",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key"),
        sa.Column(
            "installation_id",
            sa.String(36),
            nullable=False,
            comment="Installation id (from the authenticated API key)",
        ),
        sa.Column(
            "company_id",
            sa.String(36),
            sa.ForeignKey("engine_companies.id", ondelete="CASCADE"),
            nullable=False,
            comment="HesabYar engine_companies row id this connection belongs to",
        ),
        sa.Column(
            "bank_key",
            sa.String(64),
            nullable=False,
            comment="Stable bank slug, e.g. 'melli' (data — never an enum)",
        ),
        sa.Column(
            "display_name",
            sa.String(256),
            nullable=False,
            comment="Persian display name, e.g. 'بانک ملی'",
        ),
        sa.Column(
            "connection_kind",
            sa.String(64),
            nullable=False,
            comment="Connection kind as a free string: 'manual_import' | 'api_oauth' | 'aggregator' | ...",
        ),
        sa.Column(
            "credentials_ciphertext",
            sa.Text(),
            nullable=True,
            comment="Encrypted credential JSON (Fernet); NULL for credential-less connections",
        ),
        sa.Column(
            "credentials_key_id",
            sa.String(64),
            nullable=True,
            comment="Id of the encryption key version that produced the ciphertext",
        ),
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default="active",
            comment="Connection status: 'active' | 'disabled' | 'error'",
        ),
        sa.Column(
            "last_checked_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Last successful connectivity check (UTC)",
        ),
        sa.Column("notes", sa.Text(), nullable=True, comment="Operator notes"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Row creation timestamp (UTC)",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Last-modified timestamp (UTC)",
        ),
        sa.UniqueConstraint(
            "installation_id",
            "company_id",
            "bank_key",
            name="uq_bank_connections_installation_company_bank",
        ),
    )
    op.create_index(
        "ix_bank_connections_installation_company",
        "bank_connections",
        ["installation_id", "company_id"],
    )

    op.create_table(
        "stores",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key"),
        sa.Column(
            "installation_id",
            sa.String(36),
            nullable=False,
            comment="Installation id (from the authenticated API key)",
        ),
        sa.Column(
            "store_key",
            sa.String(64),
            nullable=False,
            comment="Stable store slug, used as {connector} in the events endpoint",
        ),
        sa.Column(
            "display_name",
            sa.String(256),
            nullable=False,
            comment="Persian display name",
        ),
        sa.Column(
            "store_type",
            sa.String(64),
            nullable=False,
            comment="Store kind as a free string: 'custom' | 'woocommerce' | 'sazito' | ...",
        ),
        sa.Column(
            "api_base_url",
            sa.String(1024),
            nullable=True,
            comment="Store API base URL for reconciliation polling (non-secret)",
        ),
        sa.Column(
            "prices_in_toman",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
            comment="Per-store Toman setting: True converts Toman to Rial (x10) at the connector boundary",
        ),
        sa.Column(
            "webhook_secret_ciphertext",
            sa.Text(),
            nullable=True,
            comment="Encrypted HMAC webhook secret (Fernet)",
        ),
        sa.Column(
            "api_key_ciphertext",
            sa.Text(),
            nullable=True,
            comment="Encrypted scoped read-only API key for polling (Fernet)",
        ),
        sa.Column(
            "api_key_scopes",
            sa.Text(),
            nullable=True,
            comment="Comma-separated read-only scopes of the store API key",
        ),
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default="active",
            comment="Store status: 'active' | 'disabled' | 'error'",
        ),
        sa.Column(
            "last_webhook_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Last webhook received from this store (UTC)",
        ),
        sa.Column(
            "last_poll_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Last successful reconciliation poll of this store (UTC)",
        ),
        sa.Column("notes", sa.Text(), nullable=True, comment="Operator notes"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Row creation timestamp (UTC)",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Last-modified timestamp (UTC)",
        ),
        sa.UniqueConstraint(
            "installation_id", "store_key", name="uq_stores_installation_key"
        ),
    )
    op.create_index("ix_stores_installation", "stores", ["installation_id"])

    op.create_table(
        "installation_settings",
        sa.Column(
            "installation_id",
            sa.String(36),
            primary_key=True,
            comment="Installation id (from the authenticated API key)",
        ),
        sa.Column(
            "weekly_holiday",
            sa.Integer(),
            nullable=False,
            server_default="4",
            comment="Weekly holiday weekday number (Monday=0 … Sunday=6); Friday=4 is the default",
        ),
        sa.Column(
            "matching_window_days",
            sa.Integer(),
            nullable=False,
            server_default="2",
            comment="Settlement/bank matching window in CALENDAR days (±N days)",
        ),
        sa.Column(
            "auto_post_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="Global auto-post opt-in (False = draft only)",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Row creation timestamp (UTC)",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Last-modified timestamp (UTC)",
        ),
    )

    op.create_table(
        "installation_holidays",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key"),
        sa.Column(
            "installation_id",
            sa.String(36),
            nullable=False,
            comment="Installation id (from the authenticated API key)",
        ),
        sa.Column(
            "holiday_date",
            sa.Date(),
            nullable=False,
            comment="Holiday date (Gregorian)",
        ),
        sa.Column(
            "title",
            sa.String(256),
            nullable=True,
            comment="Holiday name, e.g. 'نوروز'",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Row creation timestamp (UTC)",
        ),
        sa.UniqueConstraint(
            "installation_id", "holiday_date", name="uq_installation_holidays_installation_date"
        ),
    )
    op.create_index(
        "ix_installation_holidays_installation", "installation_holidays", ["installation_id"]
    )


def downgrade() -> None:
    op.drop_table("installation_holidays")
    op.drop_table("installation_settings")
    op.drop_table("stores")
    op.drop_table("bank_connections")
    op.drop_table("auto_post_policies")
    op.drop_table("mapping_suggestions")
    op.drop_table("integration_mappings")
    op.drop_table("gateway_contracts")
    op.drop_table("payment_channels")
