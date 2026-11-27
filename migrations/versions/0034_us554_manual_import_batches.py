"""Unified manual-import batches + settlement column mappings (US-554, ۵ح).

Revision ID: 0034
Revises: 0031
Create Date: 2026-11-20

US-554 wraps the US-514 statement import and the US-523 settlement
import in ONE batch cycle (upload → mapping → validation + dedup →
preview → review item → approve → pipeline → variance report).
The batch is an orchestration record, not a second import engine:
it references the underlying import (``source_import_id``) and
adds the staged state machine, the stored accounting-effect
preview, rejection-with-reason + archiving, re-runs, and the batch
variance report.

- ``manual_import_batches`` — one row per manual import, both
  kinds, with a ``kind`` discriminator (``statement`` /
  ``settlement``). ``status`` is the staged cycle state
  (uploaded / mapped / validated / previewed / awaiting_review /
  approved / posted / rejected / archived). ``preview_json`` is
  the accounting-effect preview (gross, fee, tax, refunds, net,
  receivable before/after, expected deposit, row counts) computed
  by the same code as the real posting effect. ``stages_json`` is
  the audit trail of every state the batch passed through.
  ``column_mapping_id`` has NO foreign key: it references
  ``bank_column_mappings`` for statement batches and
  ``settlement_column_mappings`` for settlement batches, and the
  kind discriminator says which. File bytes are never stored —
  only the filename + SHA-256 hash (the unique constraint on
  (tenant, kind, source, hash) makes re-uploading the same file a
  no-op returning the existing batch).
- ``settlement_column_mappings`` — the settlement twin of
  ``bank_column_mappings``: saved per-channel column mappings so
  the next import of the same gateway reuses the mapping. Stored
  here, NOT in the US-503 mapping center: the center's nine kinds
  are entity mappings with a closed vocabulary, while a column
  mapping is file layout per source (see docs/manual-imports.md).

Chains after 0031 (US-544 SMS reminders).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_BATCH_STATUSES = (
    "'uploaded', 'mapped', 'validated', 'previewed', 'awaiting_review', "
    "'approved', 'posted', 'rejected', 'archived'"
)


def upgrade() -> None:
    op.create_table(
        "manual_import_batches",
        sa.Column("id", sa.String(36), nullable=False, comment="UUID primary key"),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            comment="Owning tenant FK (CASCADE delete)",
        ),
        sa.Column(
            "installation_id",
            sa.String(36),
            nullable=False,
            comment="Installation id (plain external reference, no FK)",
        ),
        sa.Column(
            "company_id",
            sa.String(128),
            nullable=False,
            comment="HesabYar company row id (plain external reference, no FK)",
        ),
        sa.Column(
            "kind",
            sa.String(16),
            nullable=False,
            comment="statement | settlement",
        ),
        sa.Column(
            "source_key",
            sa.String(64),
            nullable=False,
            comment="Bank key (statements) or channel key (settlements)",
        ),
        sa.Column(
            "filename",
            sa.String(512),
            nullable=False,
            comment="Uploaded filename (provenance only — file bytes are never stored)",
        ),
        sa.Column(
            "file_format",
            sa.String(16),
            nullable=False,
            comment="xlsx | csv | pdf",
        ),
        sa.Column(
            "file_hash_sha256",
            sa.String(64),
            nullable=False,
            comment="SHA-256 hex of the uploaded bytes (file-hash dedupe)",
        ),
        sa.Column(
            "currency_unit",
            sa.String(16),
            nullable=False,
            comment="rial | toman — the file's amount unit, as declared on import",
        ),
        sa.Column(
            "date_kind",
            sa.String(16),
            nullable=False,
            server_default="jalali",
            comment="jalali | gregorian — how the file's dates are written",
        ),
        sa.Column(
            "header_row",
            sa.Integer,
            nullable=False,
            server_default="0",
            comment="0-based index of the header row in the file grid",
        ),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="awaiting_review",
            comment=(
                "uploaded | mapped | validated | previewed | awaiting_review | "
                "approved | posted | rejected | archived"
            ),
        ),
        sa.Column(
            "attempt",
            sa.Integer,
            nullable=False,
            server_default="1",
            comment="Upload attempt number on this batch (1 = original; a re-run increments it)",
        ),
        sa.Column(
            "source_import_id",
            sa.String(36),
            nullable=True,
            comment="Underlying import id: bank_import_batches.id or settlement_file_imports.id",
        ),
        sa.Column(
            "source_import_batch_id",
            sa.String(36),
            nullable=True,
            comment="Settlement import_batch_id (the tie-out key); the batch id for statements",
        ),
        sa.Column(
            "column_mapping_id",
            sa.String(36),
            nullable=True,
            comment="Saved mapping used (bank_column_mappings or settlement_column_mappings by kind; NULL = inline)",
        ),
        sa.Column(
            "mapping_columns",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="Applied mapping: canonical field -> source column",
        ),
        sa.Column(
            "file_type",
            sa.String(16),
            nullable=True,
            comment="Settlement file kind: detail | payouts | statement (NULL for statements)",
        ),
        sa.Column(
            "period_jalali",
            sa.String(7),
            nullable=True,
            comment="Jalali period the settlement file covers (YYYY/MM; NULL for statements)",
        ),
        sa.Column(
            "statement_journal_id",
            sa.Integer,
            nullable=True,
            comment="Engine statement journal id (statements only; kept for re-run provenance)",
        ),
        sa.Column(
            "default_account_id",
            sa.Integer,
            nullable=True,
            comment="Engine account id for uncoded rows (statements only)",
        ),
        sa.Column(
            "preview_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="Accounting-effect preview (gross/fee/tax/refunds/net/receivable/deposit/row counts)",
        ),
        sa.Column(
            "review_item_id",
            sa.String(36),
            nullable=True,
            comment="manual_import_review queue item id for this batch",
        ),
        sa.Column(
            "rejection_reason",
            sa.Text(),
            nullable=True,
            comment="Why the batch was rejected (kept on the archived batch)",
        ),
        sa.Column(
            "auto_post_opt_in",
            sa.Boolean,
            nullable=False,
            server_default=sa.false(),
            comment="True when the operator explicitly opted this batch into auto-post (ADR 0003 decision 1)",
        ),
        sa.Column(
            "posting_status",
            sa.String(16),
            nullable=True,
            comment="draft_hold | held_gate | tieout_failed | posting_failed | posted (NULL before approval)",
        ),
        sa.Column(
            "posting_detail",
            sa.Text(),
            nullable=True,
            comment="Why the posting step held/failed, when it did",
        ),
        sa.Column(
            "engine_ref_id",
            sa.Integer,
            nullable=True,
            comment="Engine id written on posting (statement id / journal id)",
        ),
        sa.Column(
            "stages_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
            comment="Stage audit trail: [{stage, at, note}]",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="Creation timestamp (UTC)",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="Last update timestamp (UTC)",
        ),
        sa.Column(
            "approved_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Approval timestamp (UTC), when approved",
        ),
        sa.Column(
            "posted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Posting timestamp (UTC), when posted",
        ),
        sa.Column(
            "rejected_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Rejection timestamp (UTC), when rejected",
        ),
        sa.Column(
            "archived_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Archival timestamp (UTC), when archived",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "kind IN ('statement', 'settlement')",
            name="ck_manual_import_batches_kind",
        ),
        sa.CheckConstraint(
            "file_format IN ('xlsx', 'csv', 'pdf')",
            name="ck_manual_import_batches_file_format",
        ),
        sa.CheckConstraint(
            "currency_unit IN ('rial', 'toman')",
            name="ck_manual_import_batches_currency_unit",
        ),
        sa.CheckConstraint(
            "date_kind IN ('jalali', 'gregorian')",
            name="ck_manual_import_batches_date_kind",
        ),
        sa.CheckConstraint("header_row >= 0", name="ck_manual_import_batches_header_row"),
        sa.CheckConstraint(
            f"status IN ({_BATCH_STATUSES})",
            name="ck_manual_import_batches_status",
        ),
        sa.CheckConstraint("attempt >= 1", name="ck_manual_import_batches_attempt"),
        sa.CheckConstraint(
            "file_type IS NULL OR file_type IN ('detail', 'payouts', 'statement')",
            name="ck_manual_import_batches_file_type",
        ),
        sa.CheckConstraint(
            "posting_status IS NULL OR posting_status IN "
            "('draft_hold', 'held_gate', 'tieout_failed', 'posting_failed', 'posted')",
            name="ck_manual_import_batches_posting_status",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "kind",
            "source_key",
            "file_hash_sha256",
            name="uq_manual_import_batches_tenant_kind_source_hash",
        ),
        sa.Index(
            "ix_manual_import_batches_tenant_company",
            "tenant_id",
            "company_id",
            "created_at",
        ),
        sa.Index(
            "ix_manual_import_batches_tenant_kind_status",
            "tenant_id",
            "kind",
            "status",
        ),
        sa.Index(
            "ix_manual_import_batches_source_import",
            "source_import_id",
        ),
    )
    op.create_table(
        "settlement_column_mappings",
        sa.Column("id", sa.String(36), nullable=False, comment="UUID primary key"),
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
        sa.Column(
            "channel_key",
            sa.String(64),
            nullable=False,
            comment="Payment channel key (e.g. 'snapppay') — a registry key, not a hard-coded list",
        ),
        sa.Column(
            "name",
            sa.String(128),
            nullable=False,
            comment="Operator-visible mapping name",
        ),
        sa.Column(
            "file_format",
            sa.String(16),
            nullable=False,
            comment="xlsx | csv | pdf",
        ),
        sa.Column(
            "file_type",
            sa.String(16),
            nullable=False,
            comment="detail | payouts | statement — the settlement file kind this mapping fits",
        ),
        sa.Column(
            "columns",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="Canonical settlement field -> source column (header name or '#<index>')",
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
            comment="rial | toman — the file's amount unit (last used; the import request may override)",
        ),
        sa.Column(
            "header_row",
            sa.Integer,
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
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="Creation timestamp (UTC)",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="Last update timestamp (UTC)",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "file_format IN ('xlsx', 'csv', 'pdf')",
            name="ck_settlement_column_mappings_file_format",
        ),
        sa.CheckConstraint(
            "file_type IN ('detail', 'payouts', 'statement')",
            name="ck_settlement_column_mappings_file_type",
        ),
        sa.CheckConstraint(
            "date_kind IN ('jalali', 'gregorian')",
            name="ck_settlement_column_mappings_date_kind",
        ),
        sa.CheckConstraint(
            "currency_unit IN ('rial', 'toman')",
            name="ck_settlement_column_mappings_currency_unit",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'superseded')",
            name="ck_settlement_column_mappings_status",
        ),
        sa.CheckConstraint(
            "header_row >= 0", name="ck_settlement_column_mappings_header_row"
        ),
        sa.Index(
            "ix_settlement_column_mappings_tenant_channel",
            "tenant_id",
            "channel_key",
            "status",
        ),
    )


def downgrade() -> None:
    op.drop_table("settlement_column_mappings")
    op.drop_table("manual_import_batches")
