"""Bank statement import + reconciliation (US-514, 5ت B1/B2).

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-18

Phase 5 wave 2 story US-514 (phased design §4 / §1.3 5ت, ADR 0003
decision 5): manual statement import is the first bank capability
(Bank Melli has no public statement API). Five tables:

- ``bank_column_mappings`` — saved per-bank column mappings.
- ``bank_import_batches`` — one row per uploaded file (the batch id
  IS the ``import_batch_id``); totals cover new valid rows only.
- ``bank_import_rows`` — parsed rows with dedup state. Dedup layer 1
  is the partial unique index on (tenant, company, bank, bank_ref,
  date) for rows WITH a bank reference; layer 2 is the ``match_key``
  index (sha256 of date|amount|description) for files without one.
  File bytes are never stored — only the filename for provenance.
- ``bank_match_rules`` — learned coding rules (B2 owns them); the
  unique constraint makes re-confirming the same mapping increment
  ``confirmations`` instead of duplicating the rule.
- ``bank_statement_matches`` — durable reconcile decisions per
  statement line (priority, confidence, destination account/Shaba,
  linked review item).

Chains after 0011 (US-504 review queue). The coordinator re-chains
migrations at merge time.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "bank_column_mappings",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key (column mapping id)"),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            comment="Owning tenant FK (CASCADE delete)",
        ),
        sa.Column(
            "company_id",
            sa.String(128),
            nullable=True,
            comment="HesabYar company row id when company-scoped; NULL = shared across companies",
        ),
        sa.Column("bank_key", sa.String(64), nullable=False, comment="Bank key (e.g. 'melli')"),
        sa.Column("name", sa.String(128), nullable=False, comment="Operator-visible mapping name"),
        sa.Column("file_format", sa.String(16), nullable=False, comment="xlsx | csv | pdf"),
        sa.Column(
            "columns",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="Canonical field -> source column (header name or '#<index>')",
        ),
        sa.Column(
            "date_kind",
            sa.String(16),
            nullable=False,
            server_default="jalali",
            comment="jalali | gregorian — how the file's dates are written",
        ),
        sa.Column(
            "currency_unit",
            sa.String(16),
            nullable=False,
            server_default="rial",
            comment="rial | toman — the file's amount unit",
        ),
        sa.Column(
            "header_row",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="0-based index of the header row in the file grid",
        ),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="active",
            comment="active | superseded",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, comment="Creation timestamp (UTC)"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, comment="Last update timestamp (UTC)"),
        sa.CheckConstraint("file_format IN ('xlsx', 'csv', 'pdf')", name="ck_bank_column_mappings_file_format"),
        sa.CheckConstraint("date_kind IN ('jalali', 'gregorian')", name="ck_bank_column_mappings_date_kind"),
        sa.CheckConstraint("currency_unit IN ('rial', 'toman')", name="ck_bank_column_mappings_currency_unit"),
        sa.CheckConstraint("status IN ('active', 'superseded')", name="ck_bank_column_mappings_status"),
        sa.CheckConstraint("header_row >= 0", name="ck_bank_column_mappings_header_row"),
    )
    op.create_index(
        "ix_bank_column_mappings_tenant_bank",
        "bank_column_mappings",
        ["tenant_id", "bank_key", "status"],
    )

    op.create_table(
        "bank_import_batches",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key (import_batch_id)"),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            comment="Owning tenant FK (CASCADE delete)",
        ),
        sa.Column(
            "company_id",
            sa.String(128),
            nullable=False,
            comment="HesabYar company row id (plain external reference, no FK)",
        ),
        sa.Column("bank_key", sa.String(64), nullable=False, comment="Bank key (e.g. 'melli')"),
        sa.Column(
            "statement_journal_id",
            sa.Integer(),
            nullable=False,
            comment="Engine account.statement.journal id the statement is created under",
        ),
        sa.Column(
            "filename",
            sa.String(512),
            nullable=False,
            comment="Uploaded filename (provenance only — file bytes are never stored)",
        ),
        sa.Column("file_format", sa.String(16), nullable=False, comment="xlsx | csv | pdf"),
        sa.Column(
            "currency_unit",
            sa.String(16),
            nullable=False,
            comment="rial | toman — the file's amount unit, as declared on import",
        ),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="awaiting_review",
            comment="awaiting_review | confirmed | cancelled",
        ),
        sa.Column("total_rows", sa.Integer(), nullable=False, comment="Data rows in the file"),
        sa.Column(
            "new_rows",
            sa.Integer(),
            nullable=False,
            comment="Valid, non-duplicate rows (will be imported)",
        ),
        sa.Column(
            "duplicate_rows",
            sa.Integer(),
            nullable=False,
            comment="Rows skipped as duplicates (either dedup layer)",
        ),
        sa.Column(
            "invalid_rows",
            sa.Integer(),
            nullable=False,
            comment="Rows that failed parsing (shown on the preview)",
        ),
        sa.Column(
            "total_deposits_irr",
            sa.BigInteger(),
            nullable=False,
            comment="Deposits over new rows, whole Rial",
        ),
        sa.Column(
            "total_withdrawals_irr",
            sa.BigInteger(),
            nullable=False,
            comment="Withdrawals over new rows, whole Rial (positive)",
        ),
        sa.Column(
            "default_account_id",
            sa.Integer(),
            nullable=False,
            comment="Engine account id used for uncoded rows (Tryton requires an account per line)",
        ),
        sa.Column(
            "column_mapping_id",
            sa.String(36),
            sa.ForeignKey("bank_column_mappings.id", ondelete="SET NULL"),
            nullable=True,
            comment="Saved mapping used, when one was used (NULL = inline mapping)",
        ),
        sa.Column(
            "start_balance_irr",
            sa.BigInteger(),
            nullable=True,
            comment="Opening balance from the file's balance column, if any",
        ),
        sa.Column(
            "end_balance_irr",
            sa.BigInteger(),
            nullable=True,
            comment="Closing balance from the file's balance column, if any",
        ),
        sa.Column(
            "engine_statement_id",
            sa.Integer(),
            nullable=True,
            comment="Engine account.statement id, set on confirm",
        ),
        sa.Column(
            "review_item_id",
            sa.String(36),
            nullable=True,
            comment="manual_import_review queue item id for this batch",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, comment="Creation timestamp (UTC)"),
        sa.Column(
            "confirmed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Confirmation timestamp (UTC), when confirmed",
        ),
        sa.Column(
            "cancelled_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Cancellation timestamp (UTC), when cancelled",
        ),
        sa.CheckConstraint("file_format IN ('xlsx', 'csv', 'pdf')", name="ck_bank_import_batches_file_format"),
        sa.CheckConstraint("currency_unit IN ('rial', 'toman')", name="ck_bank_import_batches_currency_unit"),
        sa.CheckConstraint(
            "status IN ('awaiting_review', 'confirmed', 'cancelled')",
            name="ck_bank_import_batches_status",
        ),
        sa.CheckConstraint("total_rows >= 0", name="ck_bank_import_batches_total_rows"),
        sa.CheckConstraint("new_rows >= 0", name="ck_bank_import_batches_new_rows"),
        sa.CheckConstraint("duplicate_rows >= 0", name="ck_bank_import_batches_duplicate_rows"),
        sa.CheckConstraint("invalid_rows >= 0", name="ck_bank_import_batches_invalid_rows"),
        sa.CheckConstraint(
            "total_deposits_irr >= 0", name="ck_bank_import_batches_deposits_non_negative"
        ),
        sa.CheckConstraint(
            "total_withdrawals_irr >= 0", name="ck_bank_import_batches_withdrawals_non_negative"
        ),
    )
    op.create_index(
        "ix_bank_import_batches_tenant_company",
        "bank_import_batches",
        ["tenant_id", "company_id", "created_at"],
    )

    op.create_table(
        "bank_import_rows",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key (import row id)"),
        sa.Column(
            "batch_id",
            sa.String(36),
            sa.ForeignKey("bank_import_batches.id", ondelete="CASCADE"),
            nullable=False,
            comment="Owning batch FK (CASCADE delete)",
        ),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            comment="Owning tenant FK (CASCADE delete, denormalised for dedup)",
        ),
        sa.Column(
            "company_id",
            sa.String(128),
            nullable=False,
            comment="HesabYar company row id (denormalised for dedup)",
        ),
        sa.Column("bank_key", sa.String(64), nullable=False, comment="Bank key (denormalised for dedup)"),
        sa.Column(
            "row_number",
            sa.Integer(),
            nullable=False,
            comment="1-based row number in the file (excluding the header)",
        ),
        sa.Column(
            "row_date",
            sa.Date(),
            nullable=True,
            comment="Gregorian transaction date (NULL when unparseable)",
        ),
        sa.Column(
            "amount_irr",
            sa.BigInteger(),
            nullable=True,
            comment="Signed amount, whole Rial (NULL when unparseable)",
        ),
        sa.Column(
            "description",
            sa.Text(),
            nullable=False,
            server_default="",
            comment="Transaction description as parsed",
        ),
        sa.Column(
            "bank_ref",
            sa.String(128),
            nullable=True,
            comment="Bank tracking/reference number, when the file has one",
        ),
        sa.Column(
            "balance_irr",
            sa.BigInteger(),
            nullable=True,
            comment="Running balance after the row, whole Rial, if mapped",
        ),
        sa.Column(
            "match_key",
            sa.String(64),
            nullable=True,
            comment="sha256(date|amount|description) — dedup layer 2 (NULL for invalid rows)",
        ),
        sa.Column(
            "is_duplicate",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="True when dedup skipped this row",
        ),
        sa.Column(
            "duplicate_reason",
            sa.String(128),
            nullable=True,
            comment="Which dedup layer caught it ('bank_ref' | 'match_key')",
        ),
        sa.Column(
            "parse_errors",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
            comment="Per-row parse problems (empty when the row parsed cleanly)",
        ),
        sa.Column(
            "was_rounded",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="True when a Toman amount rounded HALF_UP to a whole Rial",
        ),
        sa.Column(
            "suggested_account_id",
            sa.Integer(),
            nullable=True,
            comment="Suggested counterpart engine account id (preview)",
        ),
        sa.Column(
            "suggested_party_id",
            sa.Integer(),
            nullable=True,
            comment="Suggested party engine id (preview)",
        ),
        sa.Column(
            "suggested_invoice_id",
            sa.Integer(),
            nullable=True,
            comment="Suggested invoice engine id (preview)",
        ),
        sa.Column(
            "suggestion_source",
            sa.String(32),
            nullable=True,
            comment="Where the suggestion came from ('rule' | 'invoice' | 'payment' | 'default')",
        ),
        sa.CheckConstraint(
            "(is_duplicate = FALSE) OR (duplicate_reason IS NOT NULL)",
            name="ck_bank_import_rows_duplicate_reason",
        ),
    )
    op.create_index("ix_bank_import_rows_batch", "bank_import_rows", ["batch_id", "row_number"])
    op.create_index(
        "ix_bank_import_rows_match_key",
        "bank_import_rows",
        ["tenant_id", "company_id", "bank_key", "match_key"],
    )
    # Dedup layer 1 backstop: one (bank_ref, date) per tenant/company/bank
    # among NEW rows. Rows flagged is_duplicate (a re-uploaded file's
    # rows, stored so the preview can show them) are excluded, or the
    # second upload of the same file would violate the constraint.
    op.create_index(
        "uq_bank_import_rows_bank_ref",
        "bank_import_rows",
        ["tenant_id", "company_id", "bank_key", "bank_ref", "row_date"],
        unique=True,
        postgresql_where=sa.text("bank_ref IS NOT NULL AND is_duplicate = FALSE"),
    )

    op.create_table(
        "bank_match_rules",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key (match rule id)"),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            comment="Owning tenant FK (CASCADE delete)",
        ),
        sa.Column(
            "company_id",
            sa.String(128),
            nullable=False,
            comment="HesabYar company row id (plain external reference, no FK)",
        ),
        sa.Column(
            "bank_key",
            sa.String(64),
            nullable=False,
            server_default="",
            comment="Bank key the rule applies to; empty = every bank",
        ),
        sa.Column(
            "pattern",
            sa.String(256),
            nullable=False,
            comment="Normalised description substring the rule matches on",
        ),
        sa.Column(
            "direction",
            sa.String(8),
            nullable=False,
            comment="in | out | any — money direction the rule applies to",
        ),
        sa.Column(
            "target_account_id",
            sa.Integer(),
            nullable=False,
            comment="Engine account id the rule codes to",
        ),
        sa.Column(
            "target_party_id",
            sa.Integer(),
            nullable=True,
            comment="Engine party id the rule codes to, when known",
        ),
        sa.Column(
            "target_kind",
            sa.String(32),
            nullable=False,
            server_default="account",
            comment="account | misc_income — what the target means",
        ),
        sa.Column(
            "mode",
            sa.String(16),
            nullable=False,
            server_default="suggest",
            comment="suggest | auto — Suggest first, Auto only after operator trust",
        ),
        sa.Column(
            "confirmations",
            sa.Integer(),
            nullable=False,
            server_default="1",
            comment="How many confirmed manual mappings back this rule",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, comment="Creation timestamp (UTC)"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, comment="Last update timestamp (UTC)"),
        sa.UniqueConstraint(
            "tenant_id",
            "company_id",
            "bank_key",
            "pattern",
            "direction",
            "target_account_id",
            name="uq_bank_match_rules_pattern_target",
        ),
        sa.CheckConstraint("direction IN ('in', 'out', 'any')", name="ck_bank_match_rules_direction"),
        sa.CheckConstraint("mode IN ('suggest', 'auto')", name="ck_bank_match_rules_mode"),
        sa.CheckConstraint("confirmations >= 1", name="ck_bank_match_rules_confirmations"),
    )
    op.create_index(
        "ix_bank_match_rules_tenant_company",
        "bank_match_rules",
        ["tenant_id", "company_id"],
    )

    op.create_table(
        "bank_statement_matches",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key (match id)"),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            comment="Owning tenant FK (CASCADE delete)",
        ),
        sa.Column(
            "company_id",
            sa.String(128),
            nullable=False,
            comment="HesabYar company row id (plain external reference, no FK)",
        ),
        sa.Column("statement_id", sa.Integer(), nullable=False, comment="Engine account.statement id"),
        sa.Column(
            "statement_line_id",
            sa.Integer(),
            nullable=False,
            comment="Engine account.statement.line id",
        ),
        sa.Column("line_date", sa.Date(), nullable=False, comment="Gregorian date of the matched line"),
        sa.Column(
            "line_amount_irr",
            sa.BigInteger(),
            nullable=False,
            comment="Signed line amount, whole Rial",
        ),
        sa.Column(
            "line_description",
            sa.Text(),
            nullable=False,
            server_default="",
            comment="Line description at match time",
        ),
        sa.Column(
            "target_type",
            sa.String(16),
            nullable=False,
            comment="invoice | payment | rule | manual",
        ),
        sa.Column(
            "target_id",
            sa.Integer(),
            nullable=True,
            comment="Engine target id (NULL for rule targets)",
        ),
        sa.Column(
            "target_ref",
            sa.String(64),
            nullable=False,
            comment="Stable target key: engine id as text, or the rule UUID",
        ),
        sa.Column(
            "target_label",
            sa.String(256),
            nullable=False,
            comment="Operator-visible target label",
        ),
        sa.Column(
            "priority",
            sa.Integer(),
            nullable=False,
            comment="Matching priority that produced this (1 | 2 | 3)",
        ),
        sa.Column("confidence", sa.Integer(), nullable=False, comment="Match confidence 0–100"),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            comment="auto_applied | suggested | confirmed | rejected | unmatched",
        ),
        sa.Column(
            "outcome",
            sa.String(16),
            nullable=True,
            comment="Reconcile outcome that produced the record (NULL for manual matches)",
        ),
        sa.Column(
            "destination_account_id",
            sa.Integer(),
            nullable=True,
            comment="Statement journal's bank account id (the money's destination)",
        ),
        sa.Column(
            "destination_shaba",
            sa.String(32),
            nullable=True,
            comment="Destination Shaba (IBAN) when the journal carries a bank account",
        ),
        sa.Column(
            "review_item_id",
            sa.String(36),
            nullable=True,
            comment="bank_unmatched queue item id, when one was filed for this line",
        ),
        sa.Column("matched_at", sa.DateTime(timezone=True), nullable=False, comment="Match timestamp (UTC)"),
        sa.Column(
            "matched_by",
            sa.String(128),
            nullable=True,
            comment="Actor that confirmed/applied the match (NULL for automatic matches)",
        ),
        sa.CheckConstraint(
            "target_type IN ('invoice', 'payment', 'rule', 'manual')",
            name="ck_bank_statement_matches_target_type",
        ),
        sa.CheckConstraint(
            "status IN ('auto_applied', 'suggested', 'confirmed', 'rejected', 'unmatched')",
            name="ck_bank_statement_matches_status",
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN "
            "('already_matched', 'matched', 'suggested', 'ambiguous', 'unmatched', 'skipped_zero')",
            name="ck_bank_statement_matches_outcome",
        ),
        sa.CheckConstraint("priority IN (1, 2, 3)", name="ck_bank_statement_matches_priority"),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 100",
            name="ck_bank_statement_matches_confidence",
        ),
    )
    op.create_index(
        "ix_bank_statement_matches_statement",
        "bank_statement_matches",
        ["tenant_id", "statement_id"],
    )
    op.create_index(
        "ix_bank_statement_matches_company",
        "bank_statement_matches",
        ["tenant_id", "company_id"],
    )
    op.create_index(
        "ix_bank_statement_matches_line",
        "bank_statement_matches",
        ["tenant_id", "statement_id", "statement_line_id"],
    )


def downgrade() -> None:
    op.drop_table("bank_statement_matches")
    op.drop_table("bank_match_rules")
    op.drop_table("bank_import_rows")
    op.drop_table("bank_import_batches")
    op.drop_table("bank_column_mappings")
