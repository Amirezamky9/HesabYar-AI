"""Moadian test profiles + simulator records (US-404).

Revision ID: 0007
Revises: 0005
Create Date: 2026-11-17

Design-eng spec §7.4 (Moadian group) and §12.3 (Moadian boundary):
versioned protocol profiles, invoice documents, submissions with a
recorded human approval step, and official status evidence. Secrets
are never stored here — profiles carry only a key *reference* (an
env-var name for test keys, a secret-manager path for production
keys) and the SHA-256 fingerprint of the public key.

NOTE on the revision chain: this worktree is based on US-401 (0005);
US-402 owns revision 0006 on a parallel branch. The coordinator
re-chains this revision after 0006 at merge time.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

# Same verification vocabulary as the standards registry (0005):
# docs/CURRENT_RULES_1405.md §1 + UNVERIFIED.
VERIFICATION_STATUSES = (
    "VERIFIED_TEXT",
    "CORROBORATED_CURRENT",
    "DISCOVERY_ONLY",
    "BLOCKED_SOURCE_GAP",
    "TRANSITIONAL",
    "UNVERIFIED",
)

MOADIAN_ENVIRONMENTS = ("test", "production")
SENDER_MODES = ("self-tsp", "tsp")
DOCUMENT_STATUSES = ("draft", "submitted", "accepted", "rejected")
SUBMISSION_STATUSES = (
    "draft",
    "approved",
    "queued",
    "submitted",
    "accepted",
    "rejected",
    "failed",
)
SIMULATION_SCENARIOS = ("accept", "reject")
# Official inquiry statuses (instruction: PENDING, FAILED; public SDK
# corroboration: SUCCESS, IN_PROGRESS, NOT_FOUND, TIMEOUT).
OFFICIAL_STATUSES = (
    "PENDING",
    "SUCCESS",
    "FAILED",
    "IN_PROGRESS",
    "NOT_FOUND",
    "TIMEOUT",
)
STATUS_EVENT_SOURCES = ("simulator", "operator", "system")


def upgrade() -> None:
    op.create_table(
        "moadian_profiles",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key"),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            comment="Owning tenant FK (CASCADE delete)",
        ),
        sa.Column(
            "name",
            sa.String(256),
            nullable=False,
            comment="Human-readable profile name, e.g. 'Eyvan — test fiscal memory'",
        ),
        sa.Column(
            "slug",
            sa.String(128),
            nullable=False,
            comment="URL-safe profile key, unique per tenant",
        ),
        sa.Column(
            "environment",
            sa.String(16),
            nullable=False,
            comment="Target environment: 'test' (simulator) or 'production' (connector not built yet)",
        ),
        sa.Column(
            "sender_mode",
            sa.String(16),
            server_default=sa.text("'self-tsp'"),
            nullable=False,
            comment="Who sends: 'self-tsp' (taxpayer directly) or 'tsp' (trusted company)",
        ),
        sa.Column(
            "fiscal_id",
            sa.String(6),
            nullable=True,
            comment="Fiscal memory unique id (6 characters, issued in the taxpayer's کارپوشه)",
        ),
        sa.Column(
            "economic_code",
            sa.String(14),
            nullable=True,
            comment="Seller economic code (10 digits per Table 7, or 14 for the new economic code)",
        ),
        sa.Column(
            "packet_type",
            sa.String(32),
            server_default=sa.text("'INVOICE.V01'"),
            nullable=False,
            comment="Packet type, e.g. 'INVOICE.V01' (instruction: INVOICE + version)",
        ),
        sa.Column(
            "api_base_url",
            sa.String(512),
            server_default=sa.text("'https://tp.tax.gov.ir/req/'"),
            nullable=False,
            comment="Official API base URL (profile metadata only — no code in this phase calls it)",
        ),
        sa.Column(
            "protocol_version",
            sa.String(64),
            server_default=sa.text("'1401-07-26'"),
            nullable=False,
            comment="Version of the official technical instruction this profile was modelled from",
        ),
        sa.Column(
            "source_authority",
            sa.String(256),
            nullable=True,
            comment="Issuing authority, e.g. 'Iranian National Tax Administration (INTA)'",
        ),
        sa.Column(
            "source_url",
            sa.String(1024),
            nullable=True,
            comment="Official source document URL (INTA portal)",
        ),
        sa.Column(
            "snapshot_uri",
            sa.String(1024),
            nullable=True,
            comment="Repo-relative path of the snapshotted source, when pinned",
        ),
        sa.Column(
            "verification_status",
            sa.String(32),
            server_default=sa.text("'UNVERIFIED'"),
            nullable=False,
            comment="Verification label (CURRENT_RULES_1405.md §1 + UNVERIFIED)",
        ),
        sa.Column(
            "key_reference",
            sa.String(512),
            nullable=True,
            comment="Where the signing key lives (env-var name for test keys, "
            "secret-manager path for production) — a reference only, NEVER key material",
        ),
        sa.Column(
            "key_fingerprint",
            sa.String(64),
            nullable=True,
            comment="SHA-256 hex of the public key, when known (never the private key)",
        ),
        sa.Column(
            "is_active",
            sa.Boolean,
            server_default=sa.text("true"),
            nullable=False,
            comment="Whether the profile may be used for new submissions",
        ),
        sa.Column(
            "notes",
            sa.Text,
            nullable=True,
            comment="Free-form notes, e.g. documented assumptions about the profile",
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
        sa.UniqueConstraint("tenant_id", "slug", name="uq_moadian_profiles_tenant_slug"),
        sa.CheckConstraint(
            "environment IN (" + ", ".join(f"'{value}'" for value in MOADIAN_ENVIRONMENTS) + ")",
            name="ck_moadian_profiles_environment",
        ),
        sa.CheckConstraint(
            "sender_mode IN (" + ", ".join(f"'{value}'" for value in SENDER_MODES) + ")",
            name="ck_moadian_profiles_sender_mode",
        ),
        sa.CheckConstraint(
            "verification_status IN (" + ", ".join(f"'{status}'" for status in VERIFICATION_STATUSES) + ")",
            name="ck_moadian_profiles_verification_status",
        ),
        sa.CheckConstraint(
            "(fiscal_id IS NULL OR length(fiscal_id) = 6)",
            name="ck_moadian_profiles_fiscal_id_length",
        ),
        sa.CheckConstraint(
            "(economic_code IS NULL OR length(economic_code) IN (10, 14))",
            name="ck_moadian_profiles_economic_code_length",
        ),
    )
    op.create_index("ix_moadian_profiles_tenant_id", "moadian_profiles", ["tenant_id"])

    op.create_table(
        "moadian_documents",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key"),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            comment="Owning tenant FK (CASCADE delete)",
        ),
        sa.Column(
            "profile_id",
            sa.String(36),
            sa.ForeignKey("moadian_profiles.id", ondelete="CASCADE"),
            nullable=False,
            comment="Owning Moadian profile FK (CASCADE delete)",
        ),
        sa.Column(
            "invoice_id",
            sa.String(128),
            nullable=True,
            comment="HesabYar/Tryton invoice this document was built from — external reference, no cross-DB FK",
        ),
        sa.Column(
            "taxid",
            sa.String(22),
            nullable=True,
            comment="Unique tax number (22 characters), when assigned",
        ),
        sa.Column(
            "serial",
            sa.String(10),
            nullable=True,
            comment="Invoice serial (inno, 10 characters), when assigned",
        ),
        sa.Column(
            "invoice_type",
            sa.Integer,
            nullable=False,
            comment="Invoice type (inty, 1-3)",
        ),
        sa.Column(
            "invoice_pattern",
            sa.Integer,
            nullable=False,
            comment="Invoice pattern (inp, 1-6)",
        ),
        sa.Column(
            "invoice_subject",
            sa.Integer,
            nullable=False,
            comment="Invoice subject (ins, 1-4)",
        ),
        sa.Column(
            "payload",
            postgresql.JSONB,
            nullable=False,
            comment="Moadian invoice JSON (header/body/payment per instruction Table 7)",
        ),
        sa.Column(
            "payload_hash",
            sa.String(64),
            nullable=False,
            comment="SHA-256 hex of the canonical invoice payload",
        ),
        sa.Column(
            "status",
            sa.String(32),
            server_default=sa.text("'draft'"),
            nullable=False,
            comment="Document lifecycle: 'draft', 'submitted', 'accepted' or 'rejected'",
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
        sa.UniqueConstraint("tenant_id", "taxid", name="uq_moadian_documents_tenant_taxid"),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{value}'" for value in DOCUMENT_STATUSES) + ")",
            name="ck_moadian_documents_status",
        ),
        sa.CheckConstraint(
            "invoice_type BETWEEN 1 AND 3",
            name="ck_moadian_documents_invoice_type",
        ),
        sa.CheckConstraint(
            "invoice_pattern BETWEEN 1 AND 6",
            name="ck_moadian_documents_invoice_pattern",
        ),
        sa.CheckConstraint(
            "invoice_subject BETWEEN 1 AND 4",
            name="ck_moadian_documents_invoice_subject",
        ),
        sa.CheckConstraint(
            "(taxid IS NULL OR length(taxid) = 22)",
            name="ck_moadian_documents_taxid_length",
        ),
        sa.CheckConstraint(
            "(serial IS NULL OR length(serial) = 10)",
            name="ck_moadian_documents_serial_length",
        ),
    )
    op.create_index("ix_moadian_documents_tenant_id", "moadian_documents", ["tenant_id"])
    op.create_index("ix_moadian_documents_profile_id", "moadian_documents", ["profile_id"])

    op.create_table(
        "moadian_submissions",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key"),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            comment="Owning tenant FK (CASCADE delete)",
        ),
        sa.Column(
            "document_id",
            sa.String(36),
            sa.ForeignKey("moadian_documents.id", ondelete="CASCADE"),
            nullable=False,
            comment="Owning document FK (CASCADE delete)",
        ),
        sa.Column(
            "profile_id",
            sa.String(36),
            sa.ForeignKey("moadian_profiles.id", ondelete="CASCADE"),
            nullable=False,
            comment="Profile used for this submission FK (CASCADE delete; denormalized from the document)",
        ),
        sa.Column(
            "uid",
            sa.String(36),
            nullable=False,
            comment="Unique submission id (uuid3, client-generated, sent unencrypted)",
        ),
        sa.Column(
            "reference_number",
            sa.String(36),
            nullable=True,
            comment="Unique receipt id (uuid) issued by the (simulated) server on acceptance",
        ),
        sa.Column(
            "correlation_id",
            sa.String(64),
            nullable=False,
            comment="Correlation id across the submission, outbox event and status events",
        ),
        sa.Column(
            "environment",
            sa.String(16),
            nullable=False,
            comment="Target environment denormalized from the profile: 'test' or 'production'",
        ),
        sa.Column(
            "scenario",
            sa.String(16),
            server_default=sa.text("'accept'"),
            nullable=False,
            comment="Simulator fixture outcome: 'accept' or 'reject' (test environment only)",
        ),
        sa.Column(
            "status",
            sa.String(32),
            server_default=sa.text("'draft'"),
            nullable=False,
            comment="Submission lifecycle: draft → approved → queued → submitted → accepted/rejected/failed",
        ),
        sa.Column(
            "retry",
            sa.Boolean,
            server_default=sa.text("false"),
            nullable=False,
            comment="True when this is a resubmission of a document that already "
            "has a submission (instruction: retry=true on resend)",
        ),
        sa.Column(
            "approved_by",
            sa.String(128),
            nullable=True,
            comment="API client id that approved the submission (the recorded human approval)",
        ),
        sa.Column(
            "approved_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the submission was approved (UTC); NULL until approved",
        ),
        sa.Column(
            "approval_note",
            sa.Text,
            nullable=True,
            comment="Optional note recorded with the approval",
        ),
        sa.Column(
            "request_hash",
            sa.String(64),
            nullable=True,
            comment="SHA-256 hex of the canonical submitted packet",
        ),
        sa.Column(
            "response_hash",
            sa.String(64),
            nullable=True,
            comment="SHA-256 hex of the canonical (simulated) server response",
        ),
        sa.Column(
            "request_payload",
            postgresql.JSONB,
            nullable=True,
            comment="Submitted packet envelope (packet + simulator marker + scenario)",
        ),
        sa.Column(
            "response_payload",
            postgresql.JSONB,
            nullable=True,
            comment="Latest (simulated) server response (submit receipt or inquiry item)",
        ),
        sa.Column(
            "outbox_event_id",
            sa.String(36),
            sa.ForeignKey("outbox_events.id", ondelete="SET NULL"),
            nullable=True,
            comment="Outbox event that dispatched this submission (SET NULL on delete)",
        ),
        sa.Column(
            "last_error_code",
            sa.String(64),
            nullable=True,
            comment="Stable error code of the last failure, when any",
        ),
        sa.Column(
            "last_error_detail",
            sa.Text,
            nullable=True,
            comment="Detail of the last failure, when any",
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
        sa.UniqueConstraint("uid", name="uq_moadian_submissions_uid"),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{value}'" for value in SUBMISSION_STATUSES) + ")",
            name="ck_moadian_submissions_status",
        ),
        sa.CheckConstraint(
            "environment IN (" + ", ".join(f"'{value}'" for value in MOADIAN_ENVIRONMENTS) + ")",
            name="ck_moadian_submissions_environment",
        ),
        sa.CheckConstraint(
            "scenario IN (" + ", ".join(f"'{value}'" for value in SIMULATION_SCENARIOS) + ")",
            name="ck_moadian_submissions_scenario",
        ),
        sa.CheckConstraint(
            "(status = 'draft') = (approved_at IS NULL)",
            name="ck_moadian_submissions_approval_state",
        ),
    )
    op.create_index("ix_moadian_submissions_tenant_id", "moadian_submissions", ["tenant_id"])
    op.create_index("ix_moadian_submissions_document_id", "moadian_submissions", ["document_id"])
    op.create_index(
        "ix_moadian_submissions_status",
        "moadian_submissions",
        ["tenant_id", "status"],
    )

    op.create_table(
        "moadian_status_events",
        sa.Column("id", sa.String(36), primary_key=True, comment="UUID primary key"),
        sa.Column(
            "submission_id",
            sa.String(36),
            sa.ForeignKey("moadian_submissions.id", ondelete="CASCADE"),
            nullable=False,
            comment="Owning submission FK (CASCADE delete)",
        ),
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            comment="Official status reported: PENDING, SUCCESS, FAILED, IN_PROGRESS, NOT_FOUND or TIMEOUT",
        ),
        sa.Column(
            "source",
            sa.String(32),
            server_default=sa.text("'simulator'"),
            nullable=False,
            comment="Where the evidence came from: 'simulator', 'operator' or 'system'",
        ),
        sa.Column(
            "evidence",
            postgresql.JSONB,
            nullable=True,
            comment="Inquiry response item verbatim ({uid, referenceNumber, status, data, packetType, fiscalId})",
        ),
        sa.Column(
            "correlation_id",
            sa.String(64),
            nullable=True,
            comment="Correlation id of the submission this event belongs to",
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="When the status was observed (UTC)",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Row creation timestamp (UTC)",
        ),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{value}'" for value in OFFICIAL_STATUSES) + ")",
            name="ck_moadian_status_events_status",
        ),
        sa.CheckConstraint(
            "source IN (" + ", ".join(f"'{value}'" for value in STATUS_EVENT_SOURCES) + ")",
            name="ck_moadian_status_events_source",
        ),
    )
    op.create_index(
        "ix_moadian_status_events_submission_occurred",
        "moadian_status_events",
        ["submission_id", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_table("moadian_status_events")
    op.drop_table("moadian_submissions")
    op.drop_table("moadian_documents")
    op.drop_table("moadian_profiles")
