"""Gateway credential storage on payment channels (US-524, Phase 5-3 Wave B).

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-19

Phased design §3 §1 (per-gateway checklists): each gateway's API
credentials live ENCRYPTED on its channel row (the same F3
Fernet store as bank/store secrets — ``credentials_ciphertext`` +
``credentials_key_id``, following the bank-connection pattern from
migration 0010). The decrypted payload is one JSON object: the
gateway's secrets plus optional non-secret overrides (``base_url``,
``requires_settle``, …). No credential material ever appears in a
REST response, a log, or a domain model — the domain carries only
``has_credentials``.

Two columns on ``payment_channels`` (both nullable — manual
channels such as Sadad and SalamPay have no credentials at all):

- ``credentials_ciphertext``: Fernet ciphertext of the JSON payload.
- ``credentials_key_id``: id of the key version that produced it.

Chains after 0021 (US-522 settlement posting ledger).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "payment_channels",
        sa.Column(
            "credentials_ciphertext",
            sa.Text(),
            nullable=True,
            comment=(
                "Fernet ciphertext of the channel's gateway credential "
                "payload (JSON object: secrets + optional non-secret "
                "overrides). NULL for manual channels with no API."
            ),
        ),
    )
    op.add_column(
        "payment_channels",
        sa.Column(
            "credentials_key_id",
            sa.String(64),
            nullable=True,
            comment="Id of the encryption key version that produced the ciphertext",
        ),
    )


def downgrade() -> None:
    op.drop_column("payment_channels", "credentials_key_id")
    op.drop_column("payment_channels", "credentials_ciphertext")
