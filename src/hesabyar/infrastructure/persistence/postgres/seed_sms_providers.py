"""Seed the built-in SMS providers for one installation (US-544).

Seeds the built-in provider rows of the ADR 0003 amendment
(2026-11-18): Kavenegar + sms.ir (the first live providers) plus
the simulator. The seed is idempotent: re-running it returns the
same rows (keyed by ``(installation_id, provider_key)``) and
never duplicates them — an installation seeded before a new
built-in lands gains the new row on the next seed run.

These rows are the registry's initial DATA — they are NOT a
hard-coded provider list: the ``sms_providers`` table is the
source of truth, no behaviour in the domain, application or
interface layers branches on a provider key, and the reminder
service + dispatcher work off the registry (ADR 0003 item 12,
applied to SMS). This module is the ONLY place the built-in slugs
appear (plus tests), the same seed-data pattern as the
tax-policy and payment-channel registries.

Kavenegar + sms.ir are born DISABLED (``enabled=False``,
``status='setup'``): the operator enables a provider only after
attaching its credentials — fail-closed, so a seeded provider can
never send before it is configured. The simulator is born ENABLED
and DEFAULT: reminders work as logged simulations with zero
configuration, and no live send can happen before an operator
configures a live provider and marks it default.

Run as a module to seed the configured database for one installation::

    python -m hesabyar.infrastructure.persistence.postgres.seed_sms_providers \\
        --installation-id 00000000-0000-0000-0000-000000000001
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from hesabyar.domain.sms.models import (
    PROVIDER_KIND_KAVENEGAR,
    PROVIDER_KIND_SIMULATOR,
    PROVIDER_KIND_SMS_IR,
    SmsProvider,
    SmsProviderStatus,
)
from hesabyar.infrastructure.persistence.postgres.models import SmsProviderModel


@dataclass(frozen=True)
class SeedSmsProvider:
    """One built-in provider as seeded (registry data, not behaviour)."""

    provider_key: str
    display_name_fa: str
    provider_kind: str
    enabled: bool
    is_default: bool
    status: SmsProviderStatus


#: The built-in providers (ADR 0003 amendment 2026-11-18). The
#: simulator is the fail-safe default; the live providers are born
#: disabled until the operator configures them.
BUILTIN_SMS_PROVIDERS: tuple[SeedSmsProvider, ...] = (
    SeedSmsProvider(
        provider_key=PROVIDER_KIND_KAVENEGAR,
        display_name_fa="کاوه‌نگار",
        provider_kind=PROVIDER_KIND_KAVENEGAR,
        enabled=False,
        is_default=False,
        status=SmsProviderStatus.SETUP,
    ),
    SeedSmsProvider(
        provider_key=PROVIDER_KIND_SMS_IR,
        display_name_fa="sms.ir",
        provider_kind=PROVIDER_KIND_SMS_IR,
        enabled=False,
        is_default=False,
        status=SmsProviderStatus.SETUP,
    ),
    SeedSmsProvider(
        provider_key=PROVIDER_KIND_SIMULATOR,
        display_name_fa="شبیه‌ساز (توسعه و آزمون)",
        provider_kind=PROVIDER_KIND_SIMULATOR,
        enabled=True,
        is_default=True,
        status=SmsProviderStatus.ACTIVE,
    ),
)


def provider_from_row(row: SmsProviderModel) -> SmsProvider:
    """Convert a provider ORM row to the domain read model (no credential material)."""
    from decimal import Decimal

    return SmsProvider(
        id=row.id,
        installation_id=row.installation_id,
        provider_key=row.provider_key,
        display_name_fa=row.display_name_fa,
        provider_kind=row.provider_kind,
        is_builtin=row.is_builtin,
        enabled=row.enabled,
        is_default=row.is_default,
        status=SmsProviderStatus(row.status),
        line_number=row.line_number,
        price_per_sms_toman=(
            Decimal(row.price_per_sms_toman) if row.price_per_sms_toman is not None else None
        ),
        rate_limit_per_minute=row.rate_limit_per_minute,
        line_type=row.line_type,
        contract_required=row.contract_required,
        enmad_required=row.enmad_required,
        notes=row.notes,
        has_credentials=row.credentials_ciphertext is not None,
        credentials_key_id=row.credentials_key_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def seed_builtin_providers(session: Session, installation_id: str) -> list[SmsProvider]:
    """Create the built-in provider rows for one installation (idempotent).

    Existing rows are left untouched (an operator's edits are
    never overwritten by a re-seed); missing rows are inserted.
    The simulator is seeded as default ONLY when no default exists
    yet — an operator who deleted the simulator after marking
    another provider default must not get a second default (or an
    IntegrityError) on a re-seed. The caller owns the transaction.
    """
    default_exists = (
        session.execute(
            select(SmsProviderModel.id).where(
                SmsProviderModel.installation_id == installation_id,
                SmsProviderModel.is_default.is_(True),
            )
        ).scalar_one_or_none()
        is not None
    )
    providers: list[SmsProvider] = []
    for seed in BUILTIN_SMS_PROVIDERS:
        row = session.execute(
            select(SmsProviderModel).where(
                SmsProviderModel.installation_id == installation_id,
                SmsProviderModel.provider_key == seed.provider_key,
            )
        ).scalar_one_or_none()
        if row is None:
            row = SmsProviderModel(
                installation_id=installation_id,
                provider_key=seed.provider_key,
                display_name_fa=seed.display_name_fa,
                provider_kind=seed.provider_kind,
                is_builtin=True,
                enabled=seed.enabled,
                is_default=seed.is_default and not default_exists,
                status=seed.status.value,
            )
            session.add(row)
            session.flush()
            if row.is_default:
                default_exists = True
        providers.append(provider_from_row(row))
    return providers


def main(argv: list[str] | None = None) -> int:
    """Seed the built-in providers for one installation (CLI entry point)."""
    parser = argparse.ArgumentParser(description="Seed the built-in SMS providers.")
    parser.add_argument("--installation-id", required=True)
    parser.add_argument(
        "--database-url",
        default="postgresql+psycopg://hesabyar:hesabyar_pass@hesabyar-postgres:5432/hesabyar_test",
    )
    args = parser.parse_args(argv)
    engine = create_engine(args.database_url)
    session = Session(engine)
    try:
        rows = seed_builtin_providers(session, args.installation_id)
        session.commit()
    finally:
        session.close()
    for row in rows:
        print(f"{row.provider_key}: enabled={row.enabled} default={row.is_default}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
