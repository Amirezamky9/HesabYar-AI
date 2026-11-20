"""Settlement engine core tables (US-521, Phase 5-3 Wave A).

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-19

Phased design §3.2–§3.7: the settlement engine's data model — every
gateway payment gets an exact expected settlement schedule (one
``settlement_tranches`` row per leg of its contract), plus the
settlement events, bank deposits, many-to-many matches, the 11-type
exception queue, fee records, and the settlement-file import model
the matching stories (US-522/523) build on.

- ``gateway_payments``: one row per gateway transaction (UNIQUE on
  (payment_channel_id, gateway_transaction_id) — webhook + manual
  import of the same transaction converge on one row, §7.1).
  Amounts are integer IRR; the fee % and VAT rate actually applied
  are snapshotted on the row, so a later contract version never
  rewrites history.
- ``settlement_tranches``: one row per tranche per payment
  (UNIQUE on (gateway_payment_id, tranche_index)); the state
  machine of §3.3 lives in the ``state`` column; the Tryton
  receivable line's ``maturity_date`` is ``expected_date``.
- ``settlement_events``: gateway "we paid" events (C1).
- ``bank_deposits``: money that landed in the bank (C4).
- ``settlement_matches``: many-to-many tranche↔deposit/event links
  with allocated amounts (C5); at least one of the two targets must
  be set (CHECK).
- ``settlement_exceptions``: the 11 §3.7 exception types.
- ``settlement_fee_records``: expected vs actual fee + VAT (§3.6).
- ``settlement_file_imports`` + ``settlement_file_rows``: the
  gateway-file import model (§3.2ب); UNIQUE on
  (payment_channel_id, file_hash_sha256) — re-importing the same
  file is a no-op.

The versioned ``gateway_contracts`` table needs NO change here: it
already carries every §3.1 field (fee timing, accrual policy,
settlement model + ``tranche_legs``, payout window/calendar,
working-day adjustment, day lag, grace days, tolerances, auto-post
policy, source note) since migration 0010 (US-503). This story
derives from it; it does not extend it.

Chains after 0019 (US-516 cash-box engine company).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "gateway_payments",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key"),
        sa.Column(
            "installation_id",
            sa.String(36),
            nullable=False,
            comment="Installation id (from the authenticated API key)",
        ),
        sa.Column(
            "payment_channel_id",
            sa.String(36),
            sa.ForeignKey("payment_channels.id", ondelete="CASCADE"),
            nullable=False,
            comment="Owning payment channel row",
        ),
        sa.Column(
            "gateway_transaction_id",
            sa.String(512),
            nullable=False,
            comment="Gateway-side transaction id (unique per channel)",
        ),
        sa.Column(
            "store_order_id",
            sa.String(512),
            nullable=True,
            comment="Store order id, when the payment came from the store",
        ),
        sa.Column(
            "store_payment_id",
            sa.String(512),
            nullable=True,
            comment="Store payment id, when the payment came from the store",
        ),
        sa.Column(
            "tryton_invoice_id",
            sa.Integer(),
            nullable=True,
            comment="Tryton account.invoice id (external reference, no SQL FK into Tryton)",
        ),
        sa.Column(
            "tryton_journal_id",
            sa.Integer(),
            nullable=True,
            comment="Tryton account.move id of the sale journal (external reference)",
        ),
        sa.Column(
            "customer_party_id",
            sa.Integer(),
            nullable=True,
            comment="Tryton party id of the CUSTOMER (the settlement receivable is from the gateway)",
        ),
        sa.Column(
            "gross_irr",
            sa.BigInteger(),
            nullable=False,
            comment="Sale amount in whole rials",
        ),
        sa.Column(
            "fee_irr",
            sa.BigInteger(),
            nullable=False,
            comment="Gateway fee in whole rials (derived at recording time)",
        ),
        sa.Column(
            "fee_vat_irr",
            sa.BigInteger(),
            nullable=False,
            comment="VAT on the fee in whole rials (derived at recording time)",
        ),
        sa.Column(
            "net_irr",
            sa.BigInteger(),
            nullable=False,
            comment="Net receivable from the gateway in whole rials (gross - fee - fee VAT)",
        ),
        sa.Column(
            "contract_version_id",
            sa.String(36),
            sa.ForeignKey("gateway_contracts.id", ondelete="RESTRICT"),
            nullable=False,
            comment="Contract version the schedule was derived from (immutable once recorded)",
        ),
        sa.Column(
            "fee_rate_snapshot",
            sa.Numeric(8, 4),
            nullable=False,
            comment="Fee percent actually applied (10 means 10%) — the contract rate at recording time",
        ),
        sa.Column(
            "fee_vat_rate_snapshot",
            sa.Numeric(8, 4),
            nullable=True,
            comment="Fee VAT rate actually applied (10 means 10%) — NULL only when unknown",
        ),
        sa.Column(
            "sales_period_jalali",
            sa.String(7),
            nullable=False,
            comment="Jalali sales period 'YYYY/MM' from gateway_confirmed_at",
        ),
        sa.Column(
            "gateway_confirmed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="When the gateway confirmed the transaction (the schedule anchor)",
        ),
        sa.Column(
            "source",
            sa.String(32),
            nullable=False,
            comment="Payment origin: 'store_webhook' | 'gateway_poll' | 'manual_import' | 'manual_entry'",
        ),
        sa.Column(
            "canonical_event_id",
            sa.String(36),
            nullable=True,
            comment="Inbox event id this payment was recorded from, when any",
        ),
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default="scheduled",
            comment="Payment lifecycle: 'scheduled' | 'settling' | 'settled' | 'partially_cancelled' | 'cancelled'",
        ),
        sa.Column(
            "refunds_deducted_irr",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
            comment="Amount already-paid tranches contribute as a negative adjustment on later settlements",
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
            "payment_channel_id",
            "gateway_transaction_id",
            name="uq_gateway_payments_channel_transaction",
        ),
    )
    op.create_index(
        "ix_gateway_payments_installation_channel",
        "gateway_payments",
        ["installation_id", "payment_channel_id"],
    )
    op.create_index(
        "ix_gateway_payments_installation_period",
        "gateway_payments",
        ["installation_id", "sales_period_jalali"],
    )

    op.create_table(
        "settlement_tranches",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key"),
        sa.Column(
            "installation_id",
            sa.String(36),
            nullable=False,
            comment="Installation id (from the authenticated API key)",
        ),
        sa.Column(
            "gateway_payment_id",
            sa.String(36),
            sa.ForeignKey("gateway_payments.id", ondelete="CASCADE"),
            nullable=False,
            comment="Owning gateway payment row",
        ),
        sa.Column(
            "tranche_index",
            sa.Integer(),
            nullable=False,
            comment="1-based tranche number within the payment (قسط ۱..N)",
        ),
        sa.Column(
            "tranche_count",
            sa.Integer(),
            nullable=False,
            comment="Total tranches of the payment's schedule",
        ),
        sa.Column(
            "sales_period_jalali",
            sa.String(7),
            nullable=False,
            comment="Jalali sales period 'YYYY/MM' (copied from the payment; corrected on period reassignment)",
        ),
        sa.Column(
            "expected_window_start",
            sa.Date(),
            nullable=False,
            comment="First day of the payout window (Gregorian)",
        ),
        sa.Column(
            "expected_date",
            sa.Date(),
            nullable=False,
            comment="Due date (Gregorian) — also the maturity_date of the Tryton receivable line",
        ),
        sa.Column(
            "gross_irr",
            sa.BigInteger(),
            nullable=False,
            comment="Tranche gross share in whole rials",
        ),
        sa.Column(
            "fee_irr",
            sa.BigInteger(),
            nullable=False,
            comment="Tranche fee share in whole rials",
        ),
        sa.Column(
            "fee_vat_irr",
            sa.BigInteger(),
            nullable=False,
            comment="Tranche fee-VAT share in whole rials",
        ),
        sa.Column(
            "net_irr",
            sa.BigInteger(),
            nullable=False,
            comment="Tranche net share in whole rials (expected settlement amount)",
        ),
        sa.Column(
            "confirmed_net_irr",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
            comment="Net amount confirmed by the gateway file/portal so far",
        ),
        sa.Column(
            "received_net_irr",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
            comment="Net amount matched to bank deposits so far",
        ),
        sa.Column(
            "state",
            sa.String(32),
            nullable=False,
            server_default="expected",
            comment=(
                "Tranche state (§3.3): expected/due/confirmed/received/settled/"
                "partially_confirmed/partially_received/overdue/cancelled/exception/written_off"
            ),
        ),
        sa.Column(
            "tryton_receivable_line_id",
            sa.Integer(),
            nullable=True,
            comment="Tryton account.move.line id of the receivable (external reference; maturity_date = expected_date)",
        ),
        sa.Column(
            "variance_irr",
            sa.BigInteger(),
            nullable=True,
            comment="Signed variance (actual - expected) in rials, once a file/deposit disagrees",
        ),
        sa.Column(
            "variance_reason",
            sa.Text(),
            nullable=True,
            comment="Why the tranche differs from the schedule (never absorbed silently)",
        ),
        sa.Column(
            "match_method",
            sa.String(64),
            nullable=True,
            comment="C5 matching rule that matched this tranche (exact_reference/amount_and_date/amount_only/manual)",
        ),
        sa.Column(
            "match_confidence",
            sa.Numeric(5, 4),
            nullable=True,
            comment="Match confidence 0..1 (NULL when unmatched)",
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
            "gateway_payment_id", "tranche_index", name="uq_settlement_tranches_payment_index"
        ),
    )
    op.create_index(
        "ix_settlement_tranches_installation_state",
        "settlement_tranches",
        ["installation_id", "state"],
    )
    op.create_index(
        "ix_settlement_tranches_installation_due",
        "settlement_tranches",
        ["installation_id", "expected_date"],
    )
    op.create_index(
        "ix_settlement_tranches_payment",
        "settlement_tranches",
        ["gateway_payment_id"],
    )

    op.create_table(
        "settlement_events",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key"),
        sa.Column(
            "installation_id",
            sa.String(36),
            nullable=False,
            comment="Installation id (from the authenticated API key)",
        ),
        sa.Column(
            "payment_channel_id",
            sa.String(36),
            sa.ForeignKey("payment_channels.id", ondelete="CASCADE"),
            nullable=False,
            comment="Owning payment channel row",
        ),
        sa.Column(
            "payout_date",
            sa.Date(),
            nullable=False,
            comment="Gateway payout date (Gregorian)",
        ),
        sa.Column(
            "net_irr",
            sa.BigInteger(),
            nullable=False,
            comment="Net amount the gateway says it paid, in whole rials",
        ),
        sa.Column(
            "deductions_irr",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
            comment="Documented deductions of the period (prior-period debt/cancels from the statement)",
        ),
        sa.Column(
            "tracking_ref",
            sa.String(256),
            nullable=True,
            comment="Gateway tracking/reference number for the payout",
        ),
        sa.Column(
            "destination_iban",
            sa.String(64),
            nullable=True,
            comment="Destination IBAN the gateway paid to",
        ),
        sa.Column(
            "gateway_batch_ref",
            sa.String(256),
            nullable=True,
            comment="Gateway batch reference, when the payout is batched",
        ),
        sa.Column(
            "source",
            sa.String(32),
            nullable=False,
            comment="Event origin: 'payouts_file' | 'portal_manual' | 'detail_file_derived'",
        ),
        sa.Column(
            "import_batch_id",
            sa.String(36),
            nullable=True,
            comment="Import batch this event came from, when any",
        ),
        sa.Column(
            "canonical_event_id",
            sa.String(36),
            nullable=True,
            comment="Canonical payment.settlement.received event id, when published",
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
        "ix_settlement_events_installation_channel",
        "settlement_events",
        ["installation_id", "payment_channel_id"],
    )

    op.create_table(
        "bank_deposits",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key"),
        sa.Column(
            "installation_id",
            sa.String(36),
            nullable=False,
            comment="Installation id (from the authenticated API key)",
        ),
        sa.Column(
            "deposit_date",
            sa.Date(),
            nullable=False,
            comment="Bank deposit date (Gregorian)",
        ),
        sa.Column(
            "amount_irr",
            sa.BigInteger(),
            nullable=False,
            comment="Deposit amount in whole rials",
        ),
        sa.Column(
            "bank_account_ref",
            sa.String(256),
            nullable=True,
            comment="Bank account reference (IBAN or account number)",
        ),
        sa.Column(
            "description",
            sa.Text(),
            nullable=True,
            comment="Bank statement description, as imported",
        ),
        sa.Column(
            "tracking_ref",
            sa.String(256),
            nullable=True,
            comment="Gateway tracking number found in the description, when any",
        ),
        sa.Column(
            "bank_import_row_id",
            sa.String(36),
            sa.ForeignKey("bank_import_rows.id", ondelete="SET NULL"),
            nullable=True,
            comment="US-514 bank-statement import row this deposit came from (NULL for manual deposits)",
        ),
        sa.Column(
            "source",
            sa.String(32),
            nullable=False,
            server_default="statement_import",
            comment="Deposit origin: 'statement_import' | 'manual'",
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
        "ix_bank_deposits_installation_date",
        "bank_deposits",
        ["installation_id", "deposit_date"],
    )

    op.create_table(
        "settlement_matches",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key"),
        sa.Column(
            "installation_id",
            sa.String(36),
            nullable=False,
            comment="Installation id (from the authenticated API key)",
        ),
        sa.Column(
            "tranche_id",
            sa.String(36),
            sa.ForeignKey("settlement_tranches.id", ondelete="CASCADE"),
            nullable=False,
            comment="Matched tranche row",
        ),
        sa.Column(
            "bank_deposit_id",
            sa.String(36),
            sa.ForeignKey("bank_deposits.id", ondelete="CASCADE"),
            nullable=True,
            comment="Matched bank deposit (NULL when the match is event-only)",
        ),
        sa.Column(
            "settlement_event_id",
            sa.String(36),
            sa.ForeignKey("settlement_events.id", ondelete="CASCADE"),
            nullable=True,
            comment="Matched settlement event (NULL when the match is deposit-only)",
        ),
        sa.Column(
            "allocated_amount_irr",
            sa.BigInteger(),
            nullable=False,
            comment="Amount allocated by this link, in whole rials",
        ),
        sa.Column(
            "method",
            sa.String(64),
            nullable=False,
            comment="C5 matching rule: 'exact_reference' | 'amount_and_date' | 'amount_only' | 'manual'",
        ),
        sa.Column(
            "confidence",
            sa.Numeric(5, 4),
            nullable=True,
            comment="Match confidence 0..1 (NULL for manual matches)",
        ),
        sa.Column(
            "matched_by",
            sa.String(256),
            nullable=True,
            comment="Actor that matched (user id, or 'settlement-worker' for automatic matches)",
        ),
        sa.Column(
            "is_automatic",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
            comment="True for automatic matches, False for manual ones",
        ),
        sa.Column(
            "matched_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the match was made (UTC)",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Row creation timestamp (UTC)",
        ),
        sa.CheckConstraint(
            "bank_deposit_id IS NOT NULL OR settlement_event_id IS NOT NULL",
            name="ck_settlement_matches_has_target",
        ),
    )
    op.create_index(
        "ix_settlement_matches_tranche",
        "settlement_matches",
        ["tranche_id"],
    )

    op.create_table(
        "settlement_exceptions",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key"),
        sa.Column(
            "installation_id",
            sa.String(36),
            nullable=False,
            comment="Installation id (from the authenticated API key)",
        ),
        sa.Column(
            "exception_type",
            sa.String(64),
            nullable=False,
            comment=(
                "Exception type (§3.7): late_tranche/missing_deposit/partial_tranche/"
                "amount_mismatch/fee_mismatch/deposit_without_file/file_without_deposit/"
                "unknown_reference/overpayment/period_reassigned/cancelled_after_settlement"
            ),
        ),
        sa.Column(
            "severity",
            sa.String(32),
            nullable=False,
            comment="Severity: 'info' | 'warning' | 'critical'",
        ),
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default="open",
            comment="Lifecycle: 'open' | 'in_review' | 'resolved' | 'written_off'",
        ),
        sa.Column(
            "tranche_id",
            sa.String(36),
            sa.ForeignKey("settlement_tranches.id", ondelete="SET NULL"),
            nullable=True,
            comment="Related tranche, when the exception is about one",
        ),
        sa.Column(
            "settlement_event_id",
            sa.String(36),
            sa.ForeignKey("settlement_events.id", ondelete="SET NULL"),
            nullable=True,
            comment="Related settlement event, when any",
        ),
        sa.Column(
            "bank_deposit_id",
            sa.String(36),
            sa.ForeignKey("bank_deposits.id", ondelete="SET NULL"),
            nullable=True,
            comment="Related bank deposit, when any",
        ),
        sa.Column(
            "gateway_payment_id",
            sa.String(36),
            sa.ForeignKey("gateway_payments.id", ondelete="SET NULL"),
            nullable=True,
            comment="Related gateway payment, when any",
        ),
        sa.Column(
            "expected_amount_irr",
            sa.BigInteger(),
            nullable=True,
            comment="Expected amount in whole rials, when the exception is about an amount",
        ),
        sa.Column(
            "actual_amount_irr",
            sa.BigInteger(),
            nullable=True,
            comment="Actual amount in whole rials, when the exception is about an amount",
        ),
        sa.Column(
            "escalation_level",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="Overdue escalation level (0=none, 1=7d, 2=30d, 3=60d past due)",
        ),
        sa.Column(
            "resolution",
            sa.String(32),
            nullable=True,
            comment=(
                "How it was resolved: 'confirmed_ok' | 'adjusted' | 'write_off' | "
                "'escalated_to_gateway' | 'rescheduled'"
            ),
        ),
        sa.Column(
            "resolved_by",
            sa.String(256),
            nullable=True,
            comment="Actor that resolved the exception",
        ),
        sa.Column(
            "resolved_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the exception was resolved (UTC)",
        ),
        sa.Column(
            "note",
            sa.Text(),
            nullable=True,
            comment="Operator note (mandatory context on resolution)",
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
        "ix_settlement_exceptions_installation_status",
        "settlement_exceptions",
        ["installation_id", "status"],
    )
    op.create_index(
        "ix_settlement_exceptions_tranche",
        "settlement_exceptions",
        ["tranche_id"],
    )

    op.create_table(
        "settlement_fee_records",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key"),
        sa.Column(
            "installation_id",
            sa.String(36),
            nullable=False,
            comment="Installation id (from the authenticated API key)",
        ),
        sa.Column(
            "gateway_payment_id",
            sa.String(36),
            sa.ForeignKey("gateway_payments.id", ondelete="CASCADE"),
            nullable=False,
            comment="Owning gateway payment row",
        ),
        sa.Column(
            "tranche_id",
            sa.String(36),
            sa.ForeignKey("settlement_tranches.id", ondelete="SET NULL"),
            nullable=True,
            comment="Tranche this record is about (NULL = whole payment)",
        ),
        sa.Column(
            "contract_version_id",
            sa.String(36),
            sa.ForeignKey("gateway_contracts.id", ondelete="RESTRICT"),
            nullable=False,
            comment="Contract version the expected figures came from",
        ),
        sa.Column(
            "fee_percent_applied",
            sa.Numeric(8, 4),
            nullable=False,
            comment="Fee percent applied (10 means 10%)",
        ),
        sa.Column(
            "fee_vat_rate_applied",
            sa.Numeric(8, 4),
            nullable=True,
            comment="Fee VAT rate applied (10 means 10%)",
        ),
        sa.Column(
            "expected_fee_irr",
            sa.BigInteger(),
            nullable=False,
            comment="Expected fee in whole rials (from the contract)",
        ),
        sa.Column(
            "actual_fee_irr",
            sa.BigInteger(),
            nullable=True,
            comment="Actual fee in whole rials (from the gateway file/portal)",
        ),
        sa.Column(
            "fee_variance_irr",
            sa.BigInteger(),
            nullable=True,
            comment="Fee variance (actual - expected) in whole rials",
        ),
        sa.Column(
            "expected_vat_irr",
            sa.BigInteger(),
            nullable=False,
            comment="Expected fee VAT in whole rials (from the contract)",
        ),
        sa.Column(
            "actual_vat_irr",
            sa.BigInteger(),
            nullable=True,
            comment="Actual fee VAT in whole rials (from the gateway file/portal)",
        ),
        sa.Column(
            "vat_variance_irr",
            sa.BigInteger(),
            nullable=True,
            comment="Fee-VAT variance (actual - expected) in whole rials",
        ),
        sa.Column(
            "tryton_fee_journal_id",
            sa.Integer(),
            nullable=True,
            comment="Tryton account.move id of the fee journal (external reference)",
        ),
        sa.Column(
            "source",
            sa.String(32),
            nullable=False,
            server_default="calculated",
            comment="Figure origin: 'calculated' | 'file' | 'portal_statement'",
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
        "ix_settlement_fee_records_payment",
        "settlement_fee_records",
        ["gateway_payment_id"],
    )

    op.create_table(
        "settlement_file_imports",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key"),
        sa.Column(
            "installation_id",
            sa.String(36),
            nullable=False,
            comment="Installation id (from the authenticated API key)",
        ),
        sa.Column(
            "payment_channel_id",
            sa.String(36),
            sa.ForeignKey("payment_channels.id", ondelete="CASCADE"),
            nullable=False,
            comment="Owning payment channel row",
        ),
        sa.Column(
            "file_type",
            sa.String(32),
            nullable=False,
            comment="File kind: 'detail' | 'payouts' | 'statement'",
        ),
        sa.Column(
            "period_jalali",
            sa.String(7),
            nullable=False,
            comment="Jalali period the file covers ('YYYY/MM')",
        ),
        sa.Column(
            "file_hash_sha256",
            sa.String(64),
            nullable=False,
            comment="SHA-256 of the uploaded file bytes (dedup key)",
        ),
        sa.Column(
            "import_batch_id",
            sa.String(36),
            nullable=False,
            comment="Batch id grouping the whole import (reviewable/rollbackable as one unit)",
        ),
        sa.Column(
            "uploaded_by",
            sa.String(256),
            nullable=True,
            comment="Actor that uploaded the file",
        ),
        sa.Column(
            "uploaded_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the file was uploaded (UTC)",
        ),
        sa.Column(
            "row_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="Parsed rows in the file (excluding dropped Net Sale/total rows)",
        ),
        sa.Column(
            "matched_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="Rows matched to existing tranches",
        ),
        sa.Column(
            "new_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="Rows that created new payments",
        ),
        sa.Column(
            "error_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="Rows that failed to parse or match",
        ),
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default="imported",
            comment="Import lifecycle: 'imported' | 'reviewed' | 'rolled_back'",
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
            "payment_channel_id",
            "file_hash_sha256",
            name="uq_settlement_file_imports_channel_hash",
        ),
    )
    op.create_index(
        "ix_settlement_file_imports_installation_channel",
        "settlement_file_imports",
        ["installation_id", "payment_channel_id"],
    )

    op.create_table(
        "settlement_file_rows",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key"),
        sa.Column(
            "import_id",
            sa.String(36),
            sa.ForeignKey("settlement_file_imports.id", ondelete="CASCADE"),
            nullable=False,
            comment="Owning file import row",
        ),
        sa.Column(
            "row_no",
            sa.Integer(),
            nullable=False,
            comment="1-based row number in the file",
        ),
        sa.Column(
            "raw_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="Row exactly as parsed (amounts as strings to stay exact)",
        ),
        sa.Column(
            "gateway_transaction_id",
            sa.String(512),
            nullable=True,
            comment="Gateway transaction id from the row, when present",
        ),
        sa.Column(
            "tran_type",
            sa.String(64),
            nullable=True,
            comment="Row transaction type, as the file names it",
        ),
        sa.Column(
            "amount_irr",
            sa.BigInteger(),
            nullable=True,
            comment="Row gross amount in whole rials, when present",
        ),
        sa.Column(
            "fee_irr",
            sa.BigInteger(),
            nullable=True,
            comment="Row fee in whole rials, when present",
        ),
        sa.Column(
            "net_irr",
            sa.BigInteger(),
            nullable=True,
            comment="Row net amount in whole rials, when present",
        ),
        sa.Column(
            "transaction_date",
            sa.Date(),
            nullable=True,
            comment="Row transaction date (Gregorian), when present",
        ),
        sa.Column(
            "tracking_ref",
            sa.String(256),
            nullable=True,
            comment="Row tracking/reference number, when present",
        ),
        sa.Column(
            "matched_tranche_id",
            sa.String(36),
            sa.ForeignKey("settlement_tranches.id", ondelete="SET NULL"),
            nullable=True,
            comment="Tranche this row matched, when matched",
        ),
        sa.Column(
            "match_status",
            sa.String(32),
            nullable=False,
            server_default="unknown_reference",
            comment="Match outcome: 'matched' | 'new_payment' | 'unknown_reference' | 'duplicate' | 'amount_mismatch'",
        ),
        sa.Column(
            "note",
            sa.Text(),
            nullable=True,
            comment="Parse/match note for this row",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Row creation timestamp (UTC)",
        ),
    )
    op.create_index(
        "ix_settlement_file_rows_import",
        "settlement_file_rows",
        ["import_id", "row_no"],
    )


def downgrade() -> None:
    op.drop_table("settlement_file_rows")
    op.drop_table("settlement_file_imports")
    op.drop_table("settlement_fee_records")
    op.drop_table("settlement_exceptions")
    op.drop_table("settlement_matches")
    op.drop_table("bank_deposits")
    op.drop_table("settlement_events")
    op.drop_table("settlement_tranches")
    op.drop_table("gateway_payments")
