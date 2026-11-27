"""US-533: cheque intake columns + voided sayad_id reuse.

Revision ID: 0026
Revises: 0025
Chains after 0025 (US-532 issued + collateral).

Adds the intake traceability/scan/stale-filing columns to
``cheques`` and widens the live-sayad partial unique index to
exclude ``voided`` rows too: a voided intake frees its sayad_id
for a new registration (the voided record never revives).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cheques",
        sa.Column("source_event_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "cheques",
        sa.Column("scan_filestore_ref", sa.Text(), nullable=True),
    )
    op.add_column(
        "cheques",
        sa.Column("stale_in_clearing_filed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.drop_index("uq_cheques_sayad_id_live", table_name="cheques")
    op.create_index(
        "uq_cheques_sayad_id_live",
        "cheques",
        ["sayad_id"],
        unique=True,
        postgresql_where=text(
            "state NOT IN ('voided', 'expired_unreceived', 'rejected_mismatch')"
        ),
    )


def downgrade() -> None:
    op.drop_index("uq_cheques_sayad_id_live", table_name="cheques")
    op.create_index(
        "uq_cheques_sayad_id_live",
        "cheques",
        ["sayad_id"],
        unique=True,
        postgresql_where=text(
            "state NOT IN ('expired_unreceived', 'rejected_mismatch')"
        ),
    )
    op.drop_column("cheques", "stale_in_clearing_filed_at")
    op.drop_column("cheques", "scan_filestore_ref")
    op.drop_column("cheques", "source_event_id")
