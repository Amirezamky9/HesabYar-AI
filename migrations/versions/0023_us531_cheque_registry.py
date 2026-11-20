"""Cheque registry tables (US-531, Phase 5-4 ۵ث).

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-19

Phased design §5 §1.1/§2.1: the HesabYar-side cheque registry —
``cheques`` (the 24 payload fields + lifecycle, storage, bounce,
endorsement and link fields), ``cheque_custody_log`` (append-only
physical-leaf moves, never a journal), ``cheque_inquiry_receipts``
(structured Sayyad receipts — verdict/code/time only, never raw
PII), and ``cheque_settings`` (per-installation policy: 7-day
leaf-arrival deadline, bounce destination, red/brown policy).

The live-sayad_id uniqueness is a PARTIAL unique index: rows in
``expired_unreceived`` / ``rejected_mismatch`` are excluded, so a
leaf that expired or was rejected may be registered again as a NEW
record (the old one never revives, §2.1).

Chains after 0022 (US-524 gateway channel credentials).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "cheques",
        sa.Column("id", sa.String(36), nullable=False, comment="UUID primary key"),
        sa.Column(
            "tenant_id",
            sa.String(36),
            nullable=False,
            comment="Tenant id (from the authenticated API key)",
        ),
        sa.Column(
            "installation_id",
            sa.String(36),
            nullable=False,
            comment="Installation id (from the authenticated API key)",
        ),
        sa.Column(
            "company_engine_id",
            sa.Integer(),
            nullable=False,
            comment="Engine company id holding this cheque's books",
        ),
        sa.Column(
            "kind",
            sa.String(16),
            nullable=False,
            server_default="received",
            comment="Cheque kind: 'received' | 'issued'",
        ),
        sa.Column(
            "purpose",
            sa.String(16),
            nullable=False,
            server_default="payment",
            comment="Cheque purpose: 'payment' (commercial) | 'collateral' (memorandum only)",
        ),
        sa.Column(
            "sayad_id",
            sa.String(16),
            nullable=False,
            comment="16-digit Sayyad identifier (distinct from the leaf serial number)",
        ),
        sa.Column(
            "amount_irr",
            sa.BigInteger(),
            nullable=False,
            comment="Cheque amount in Rial (payload Toman x10 at the boundary)",
        ),
        sa.Column(
            "issue_date",
            sa.Date(),
            nullable=False,
            comment="Issue date on the leaf (Gregorian; payload is Jalali)",
        ),
        sa.Column(
            "due_date",
            sa.Date(),
            nullable=False,
            comment="Due date on the leaf (Gregorian; payload is Jalali)",
        ),
        sa.Column(
            "issuer_name",
            sa.String(256),
            nullable=False,
            comment="Issuer name (customer or third party)",
        ),
        sa.Column(
            "issuer_national_id",
            sa.String(11),
            nullable=False,
            comment="Issuer national id: 10 digits (natural) or 11 digits (legal)",
        ),
        sa.Column(
            "issuer_bank",
            sa.String(64),
            nullable=False,
            comment="Issuing bank: 3-digit code + name, e.g. '056 سامان'",
        ),
        sa.Column(
            "payee_name",
            sa.String(256),
            nullable=False,
            comment="Payee registered in Sayyad (must be the business)",
        ),
        sa.Column(
            "payee_national_id",
            sa.String(11),
            nullable=False,
            comment="Payee national id: 10 digits (natural) or 11 digits (legal)",
        ),
        sa.Column(
            "order_id",
            sa.String(128),
            nullable=False,
            comment="Site order id (correlation_id of the order event)",
        ),
        sa.Column(
            "customer_ref",
            sa.String(128),
            nullable=False,
            comment="Site customer reference (email/phone/customer code)",
        ),
        sa.Column(
            "sayad_registered",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="Site's claim that the issuer registered the leaf in Sayyad (verified by inquiry before held)",
        ),
        sa.Column(
            "source",
            sa.String(16),
            nullable=False,
            server_default="manual",
            comment="Registration source: 'manual' | 'site'",
        ),
        sa.Column(
            "state",
            sa.String(32),
            nullable=False,
            server_default="registered",
            comment="Accounting state (§5 §2.1); separate from sayad_status",
        ),
        sa.Column(
            "sayad_status",
            sa.String(16),
            nullable=False,
            server_default="unregistered",
            comment="Sayyad status: 'unregistered' | 'registered' | 'confirmed' | 'transferred'",
        ),
        sa.Column(
            "sayad_unconfirmed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="True when sayad_registered=false: 'do not accept an unregistered cheque' (ruling 870)",
        ),
        sa.Column(
            "leaf_number",
            sa.String(64),
            nullable=True,
            comment="Serial number printed on the leaf (for bank-statement matching)",
        ),
        sa.Column(
            "issuer_branch",
            sa.String(128),
            nullable=True,
            comment="Issuing bank branch name/code",
        ),
        sa.Column(
            "issuer_account_number",
            sa.String(64),
            nullable=True,
            comment="Issuer current-account number, when known",
        ),
        sa.Column(
            "reason",
            sa.Text(),
            nullable=True,
            comment="'بابت' — the reason for the cheque, as registered in Sayyad",
        ),
        sa.Column(
            "invoice_id",
            sa.String(128),
            nullable=True,
            comment="Linked HesabYar/Tryton invoice id, when the order was invoiced",
        ),
        sa.Column(
            "installment_leg_id",
            sa.String(128),
            nullable=True,
            comment="Linked installment leg id (the leg this cheque pays; ۵ج consumes the leg-status hook)",
        ),
        sa.Column(
            "applied_amount_irr",
            sa.BigInteger(),
            nullable=True,
            comment="Amount allocated to the linked leg when it differs from the cheque amount",
        ),
        sa.Column(
            "scan_url",
            sa.Text(),
            nullable=True,
            comment="Signed scan/photo URL from the site (downloaded into the filestore by US-533)",
        ),
        sa.Column(
            "scan_ref",
            sa.Text(),
            nullable=True,
            comment="Scan file reference in the site, when no scan_url was given",
        ),
        sa.Column(
            "registered_by",
            sa.String(128),
            nullable=True,
            comment="Who registered the cheque on the site (operator/customer)",
        ),
        sa.Column("notes", sa.Text(), nullable=True, comment="Free-form note"),
        sa.Column(
            "storage_location",
            sa.String(32),
            nullable=True,
            comment="Physical leaf location: 'cashbox' | 'vault' | 'bank' | 'with_endorsee' | 'returned_to_issuer'",
        ),
        sa.Column(
            "engine_id",
            sa.Integer(),
            nullable=True,
            comment="Engine account.check record id",
        ),
        sa.Column(
            "move_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default="{}",
            comment="Transition name -> engine move id (audit trail, report 17)",
        ),
        sa.Column(
            "destination_bank_account_id",
            sa.Integer(),
            nullable=True,
            comment="Destination bank account (engine account id) chosen at deposit",
        ),
        sa.Column(
            "destination_bank_name",
            sa.String(128),
            nullable=True,
            comment="Destination bank account display name",
        ),
        sa.Column(
            "issuer_color_code",
            sa.Integer(),
            nullable=True,
            comment="Issuer colour code 1-5 at pre-acceptance (point in time; last week's colour is not valid today)",
        ),
        sa.Column(
            "issuer_color_inquired_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the issuer colour was inquired",
        ),
        sa.Column(
            "issuer_back_cheques",
            sa.Integer(),
            nullable=True,
            comment="Issuer bounced-cheque count from the back-cheques inquiry",
        ),
        sa.Column(
            "issuer_in_transit_commitments_irr",
            sa.BigInteger(),
            nullable=True,
            comment="Issuer in-transit (Sayyad) commitments in Rial at pre-acceptance",
        ),
        sa.Column(
            "manager_approval_by",
            sa.String(128),
            nullable=True,
            comment="Manager who approved a red/brown (or high-commitment) cheque",
        ),
        sa.Column(
            "manager_approval_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the manager approval was recorded",
        ),
        sa.Column(
            "receipt_missing_warning",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="True when a transition was recorded without an inquiry receipt (warning; mismatch report)",
        ),
        sa.Column(
            "bounce_reason",
            sa.Text(),
            nullable=True,
            comment="Bounce reason recorded at the bounce transition",
        ),
        sa.Column(
            "bounce_date",
            sa.Date(),
            nullable=True,
            comment="Bounce date recorded at the bounce transition",
        ),
        sa.Column(
            "bounce_certificate_reference",
            sa.String(256),
            nullable=True,
            comment="Bank non-payment certificate / inquiry receipt reference",
        ),
        sa.Column(
            "dunning_notified_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the dunning notification for the bounce was filed",
        ),
        sa.Column(
            "endorsee_name",
            sa.String(256),
            nullable=True,
            comment="Endorsee name (supplier or other person)",
        ),
        sa.Column(
            "endorsee_type",
            sa.String(16),
            nullable=True,
            comment="Endorsee type: 'supplier' | 'other'",
        ),
        sa.Column(
            "sayad_transfer_reference",
            sa.String(256),
            nullable=True,
            comment="Sayyad transfer registration receipt for the endorsement",
        ),
        sa.Column(
            "journal_pending_review",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="True when a transition moved state but its journal is accountant-gated (§9.1 #12)",
        ),
        sa.Column(
            "installment_leg_status",
            sa.String(32),
            nullable=True,
            comment="Leg-status effect of the last transition ('paid' on clear, 'unpaid' on bounce; ۵ج)",
        ),
        sa.Column(
            "mismatch_detail",
            sa.Text(),
            nullable=True,
            comment="What mismatched (site vs leaf vs Sayyad) at rejection",
        ),
        sa.Column(
            "reminder_day3_sent_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the day-3 never-arrived reminder was filed (job idempotency)",
        ),
        sa.Column(
            "reminder_final_sent_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the day-(N-1) never-arrived reminder was filed (job idempotency)",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="Row creation timestamp (UTC)",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="Last-modified timestamp (UTC)",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_cheques_sayad_id_live",
        "cheques",
        ["sayad_id"],
        unique=True,
        postgresql_where=sa.text(
            "state NOT IN ('expired_unreceived', 'rejected_mismatch')"
        ),
    )
    op.create_index(
        "ix_cheques_installation_state", "cheques", ["installation_id", "state"]
    )
    op.create_index(
        "ix_cheques_installation_issuer",
        "cheques",
        ["installation_id", "issuer_national_id"],
    )
    op.create_index(
        "ix_cheques_installation_due_date", "cheques", ["installation_id", "due_date"]
    )

    op.create_table(
        "cheque_custody_log",
        sa.Column("id", sa.String(36), nullable=False, comment="UUID primary key"),
        sa.Column(
            "cheque_id",
            sa.String(36),
            sa.ForeignKey("cheques.id"),
            nullable=False,
            comment="Cheque this custody entry belongs to",
        ),
        sa.Column(
            "from_location",
            sa.String(32),
            nullable=True,
            comment="Previous physical location (NULL when the leaf first arrives)",
        ),
        sa.Column(
            "to_location",
            sa.String(32),
            nullable=False,
            comment="New physical location",
        ),
        sa.Column(
            "actor",
            sa.String(128),
            nullable=False,
            comment="Actor who moved the leaf",
        ),
        sa.Column("reason", sa.Text(), nullable=False, comment="Why the leaf moved"),
        sa.Column(
            "at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="When the leaf moved (UTC)",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_cheque_custody_log_cheque", "cheque_custody_log", ["cheque_id"])

    op.create_table(
        "cheque_inquiry_receipts",
        sa.Column("id", sa.String(36), nullable=False, comment="UUID primary key"),
        sa.Column(
            "cheque_id",
            sa.String(36),
            sa.ForeignKey("cheques.id"),
            nullable=False,
            comment="Cheque this receipt belongs to",
        ),
        sa.Column(
            "channel",
            sa.String(32),
            nullable=False,
            comment="Channel: 'sms' | 'cbi_website' | 'bank_app' | 'payment_app' | 'finnotech_api' | 'ehrazino_api'",
        ),
        sa.Column(
            "inquiry_type",
            sa.String(32),
            nullable=False,
            comment="Type: 'color'|'leaf_status'|'items_match'|'issuer_history'|'back_cheques'|'transfer_chain'",
        ),
        sa.Column(
            "result_code",
            sa.String(128),
            nullable=False,
            comment="Result code, e.g. 'color=3' or 'items_match=true' (verdict only, no PII)",
        ),
        sa.Column(
            "result_summary",
            sa.Text(),
            nullable=False,
            comment="Short summary of the result (never the full response)",
        ),
        sa.Column(
            "reference",
            sa.String(256),
            nullable=False,
            comment="Response tracking number / redacted reference (no names or full national ids)",
        ),
        sa.Column(
            "recorded_by",
            sa.String(128),
            nullable=False,
            comment="Who recorded the receipt",
        ),
        sa.Column(
            "cost_irr",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
            comment="Inquiry cost in Rial (SMS 350 Toman = 3,500 Rial)",
        ),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the inquiry was requested",
        ),
        sa.Column(
            "responded_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the inquiry response arrived",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="Row creation timestamp (UTC)",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_cheque_inquiry_receipts_cheque", "cheque_inquiry_receipts", ["cheque_id"]
    )

    op.create_table(
        "cheque_settings",
        sa.Column(
            "installation_id",
            sa.String(36),
            nullable=False,
            comment="Installation id (from the authenticated API key)",
        ),
        sa.Column(
            "leaf_arrival_deadline_days",
            sa.Integer(),
            nullable=False,
            server_default="7",
            comment="Days a site-registered leaf has to arrive before it expires (default 7, §9.1 #6)",
        ),
        sa.Column(
            "clearing_window_days",
            sa.Integer(),
            nullable=True,
            comment="Clearing window (days) — no hard-coded default (§9.1 #6); unset until set",
        ),
        sa.Column(
            "bounce_destination",
            sa.String(32),
            nullable=False,
            server_default="receivables",
            comment="Bounce claim destination: 'receivables' | 'bounced_receivables' (§9.1 #2)",
        ),
        sa.Column(
            "red_brown_policy",
            sa.String(32),
            nullable=False,
            server_default="manager_approval",
            comment="Red/brown-colour policy: 'reject' | 'manager_approval'",
        ),
        sa.Column(
            "max_in_transit_commitments_irr",
            sa.BigInteger(),
            nullable=True,
            comment="In-transit commitments threshold in Rial (NULL = no threshold)",
        ),
        sa.Column(
            "default_storage_location",
            sa.String(32),
            nullable=False,
            server_default="vault",
            comment="Default physical location recorded at held (§9.1 #14)",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="Row creation timestamp (UTC)",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="Last-modified timestamp (UTC)",
        ),
        sa.PrimaryKeyConstraint("installation_id"),
    )


def downgrade() -> None:
    op.drop_table("cheque_settings")
    op.drop_index("ix_cheque_inquiry_receipts_cheque", table_name="cheque_inquiry_receipts")
    op.drop_table("cheque_inquiry_receipts")
    op.drop_index("ix_cheque_custody_log_cheque", table_name="cheque_custody_log")
    op.drop_table("cheque_custody_log")
    op.drop_index("ix_cheques_installation_due_date", table_name="cheques")
    op.drop_index("ix_cheques_installation_issuer", table_name="cheques")
    op.drop_index("ix_cheques_installation_state", table_name="cheques")
    op.drop_index("uq_cheques_sayad_id_live", table_name="cheques")
    op.drop_table("cheques")
