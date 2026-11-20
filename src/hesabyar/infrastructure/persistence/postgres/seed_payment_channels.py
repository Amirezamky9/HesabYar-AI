"""Seed the built-in payment channels for one installation (US-503).

Seeds the built-in channel rows of the phased design §1.2 F3 exit
criteria (six gateways + the Basalam marketplace as a separate
channel) plus the RESERVED ``torob_marketplace`` row (US-524) for a
given installation. The seed is idempotent: re-running it returns
the same rows (keyed by ``(installation_id, channel_key)``) and
never duplicates them — an installation seeded before US-524 gains
the reserved row on the next seed run.

These rows are the registry's initial DATA — they are NOT a
hard-coded channel list: the ``payment_channels`` table is the source
of truth, no behaviour in the domain, application or interface layers
branches on a channel key, and the settlement engine, webhook routing
and import mapping all work off the registry (ADR 0003 item 12). This
module is the ONLY place the built-in slugs appear (plus tests), the
same seed-data pattern as the tax-policy registry.

Ilmaz is NOT in the built-in list (ADR 0003 round-2 item 2: Emalls is
a price-comparison search engine, not a payment channel). A store
that genuinely uses something called "Ilmaz" adds it as a custom
channel with its own contract terms.

``torob_marketplace`` is RESERVED, not operable: the Torob ORDER API
(marketplace sync) is ⚠️ UNVERIFIED, so the row is born DISABLED
and stays disabled until that API is verified — it is never mixed
with the ``torobpay`` payment channel (phased design §1.5الف).
Enabling it is refused by ``ChannelRegistryService`` (see
``RESERVED_CHANNEL_KEYS``).

Built-in channels are born DISABLED (``enabled=False``,
``status='setup'``): the operator enables a channel only after
attaching a contract and its mappings — fail-closed, so a seeded
channel can never auto-post before it is configured.

Run as a module to seed the configured database for one installation::

    python -m hesabyar.infrastructure.persistence.postgres.seed_payment_channels \\
        --installation-id 00000000-0000-0000-0000-000000000001
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from hesabyar.domain.integrations.models import RESERVED_CHANNEL_KEYS, ChannelStatus
from hesabyar.infrastructure.persistence.postgres.models import (
    InstallationSettingsModel,
    PaymentChannelModel,
)


@dataclass(frozen=True)
class SeedPaymentChannel:
    """One built-in channel as seeded (registry data, not behaviour)."""

    channel_key: str
    display_name_fa: str
    channel_type: str
    status: str = ChannelStatus.SETUP.value
    notes: str | None = None


#: Reserved channel keys, re-exported from the domain (see
#: ``hesabyar.domain.integrations.models.RESERVED_CHANNEL_KEYS``):
#: seeded rows that must stay disabled until their integration is
#: verified. Enabling one is refused by the registry service.
__all__ = ["BUILTIN_CHANNELS", "RESERVED_CHANNEL_KEYS", "SeedPaymentChannel", "seed_builtin_channels"]

#: The built-in channels (phased design §2.1 / §1.2 F3 exit criteria).
#: Six gateways + the Basalam marketplace as a separate channel +
#: the RESERVED Torob marketplace row (US-524).
#: Ilmaz is deliberately absent (see the module docstring).
BUILTIN_CHANNELS: tuple[SeedPaymentChannel, ...] = (
    SeedPaymentChannel("zarinpal", "زرین‌پال", "gateway"),
    SeedPaymentChannel("sadad", "سداد (بانک ملی)", "gateway"),
    SeedPaymentChannel("snapppay", "اسنپ‌پی", "bnpl"),
    SeedPaymentChannel("digipay", "دیجی‌پی", "bnpl"),
    SeedPaymentChannel("torobpay", "ترب‌پی", "bnpl"),
    SeedPaymentChannel("salampay", "سلام‌پی", "bnpl"),
    SeedPaymentChannel("basalam_marketplace", "بازار باسلام", "marketplace"),
    SeedPaymentChannel(
        "torob_marketplace",
        "ترب مارکت‌پلیس",
        "marketplace",
        status=ChannelStatus.DISABLED.value,
        notes=(
            "RESERVED — the Torob order API is UNVERIFIED; this channel "
            "stays disabled until it is verified and is never mixed with "
            "the torobpay payment channel."
        ),
    ),
)


def seed_builtin_channels(
    session: Session, installation_id: str
) -> tuple[PaymentChannelModel, ...]:
    """Idempotently seed the built-in channels for one installation.

    Also seeds the installation's default settings row (Friday weekly
    holiday, 2-calendar-day matching window, auto-post OFF) when it
    does not exist yet. Flushes; the caller owns the transaction.
    """
    settings = session.get(InstallationSettingsModel, installation_id)
    if settings is None:
        session.add(InstallationSettingsModel(installation_id=installation_id))
    rows: list[PaymentChannelModel] = []
    for seed in BUILTIN_CHANNELS:
        row = session.execute(
            select(PaymentChannelModel).where(
                PaymentChannelModel.installation_id == installation_id,
                PaymentChannelModel.channel_key == seed.channel_key,
            )
        ).scalar_one_or_none()
        if row is None:
            row = PaymentChannelModel(
                installation_id=installation_id,
                channel_key=seed.channel_key,
                display_name_fa=seed.display_name_fa,
                channel_type=seed.channel_type,
                is_builtin=True,
                enabled=False,
                status=seed.status,
                environment="production",
                notes=seed.notes,
            )
            session.add(row)
        rows.append(row)
    session.flush()
    return tuple(rows)


def main() -> int:
    """Seed the built-in channels for one installation (CLI entry point)."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--installation-id",
        required=True,
        help="Installation id to seed the built-in channels for",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="PostgreSQL URL (default: HESABYAR_DATABASE_URL or the dev default)",
    )
    args = parser.parse_args()

    from hesabyar.infrastructure.config import get_settings

    url = args.database_url or get_settings().database.sqlalchemy_url
    engine = create_engine(url, echo=False)
    session = Session(engine)
    try:
        rows = seed_builtin_channels(session, args.installation_id)
        session.commit()
    finally:
        session.close()
        engine.dispose()
    print(f"Seeded {len(rows)} built-in channels for installation {args.installation_id}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
