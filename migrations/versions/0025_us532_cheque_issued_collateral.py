"""Issued + collateral cheque registry fields (US-532, Phase 5-4 ۵ث).

Revision ID: 0025
Revises: 0024
Create Date: 2026-11-19

Phased design §5 §2.2/§2.3: the HesabYar-side registry gains the
fields US-532's transitions need —

- ``secured_obligation``: which obligation a collateral cheque
  secures (the report 6 dimension);
- ``checkbook_engine_id``: which checkbook an issued leaf was
  drawn from;
- ``call_in_approved_by`` / ``call_in_approved_at``: the
  accountant's per-case call-in approval (§9.1 #4, §5 §4);
- ``void_reason``: why an issued cheque was voided (سند ۱۳);
- ``unexpected_call_in`` / ``call_in_statement_reference``: the
  counterparty presented our issued collateral cheque at the bank
  without notice (withdrawal matched by leaf number).

All columns are nullable (or defaulted) — existing rows keep
working. Two indexes support the segregation filters (commercial
reports list ``purpose=payment`` only; report 6 lists
``purpose=collateral``).

Chains after 0024 (US-534 Sayyad reconciliation).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "cheques",
        sa.Column(
            "secured_obligation",
            sa.String(256),
            nullable=True,
            comment="Obligation a collateral cheque secures — report 6 dimension (US-532)",
        ),
    )
    op.add_column(
        "cheques",
        sa.Column(
            "checkbook_engine_id",
            sa.Integer(),
            nullable=True,
            comment="Engine checkbook id the issued leaf was drawn from (US-532)",
        ),
    )
    op.add_column(
        "cheques",
        sa.Column(
            "call_in_approved_by",
            sa.String(128),
            nullable=True,
            comment="Accountant who approved this case's call-in (§9.1 #4, §5 §4; US-532)",
        ),
    )
    op.add_column(
        "cheques",
        sa.Column(
            "call_in_approved_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the call-in was approved",
        ),
    )
    op.add_column(
        "cheques",
        sa.Column(
            "void_reason",
            sa.String(512),
            nullable=True,
            comment="Why an issued cheque was voided (سند ۱۳ audit trail; US-532)",
        ),
    )
    op.add_column(
        "cheques",
        sa.Column(
            "unexpected_call_in",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="True when an issued collateral cheque was presented at the bank without notice (US-532)",
        ),
    )
    op.add_column(
        "cheques",
        sa.Column(
            "call_in_statement_reference",
            sa.String(128),
            nullable=True,
            comment="Bank-statement withdrawal reference matched by leaf number for an unexpected call-in (US-532)",
        ),
    )
    op.create_index(
        "ix_cheques_installation_purpose",
        "cheques",
        ["installation_id", "purpose"],
    )
    op.create_index(
        "ix_cheques_installation_kind",
        "cheques",
        ["installation_id", "kind"],
    )


def downgrade() -> None:
    op.drop_index("ix_cheques_installation_kind", table_name="cheques")
    op.drop_index("ix_cheques_installation_purpose", table_name="cheques")
    op.drop_column("cheques", "call_in_statement_reference")
    op.drop_column("cheques", "unexpected_call_in")
    op.drop_column("cheques", "void_reason")
    op.drop_column("cheques", "call_in_approved_at")
    op.drop_column("cheques", "call_in_approved_by")
    op.drop_column("cheques", "checkbook_engine_id")
    op.drop_column("cheques", "secured_obligation")
