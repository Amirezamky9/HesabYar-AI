"""Seed the tax-policy registry with the 1405 parameter candidates (US-402).

Seeds the five annual parameters of docs/CURRENT_RULES_1405.md §3 as
CORROBORATED_CURRENT versions with their corroboration sources
recorded, plus the three Tasnim corroboration sources themselves. The
seed is idempotent: re-running it updates the same rows (keyed by
``source_key`` and ``(rule_key, version_id)``).

These values are the registry's versioned data — they are NOT timeless
constants: every figure carries its rule year, performance year,
filing year, effective dates and verification status, and the
production gate (see the decision service) blocks every one of them
until the official annual tax/budget source is pinned and the version
is promoted to VERIFIED_TEXT (CURRENT_RULES_1405.md §3, §12). No other
module may hard-code an annual VAT rate, exemption or threshold.

Year modelling (CURRENT_RULES_1405.md §3 — rule, performance and
filing years stay separate):

- VAT 10%, Article 84 and Article 101 exemptions: rule 1405,
  performance 1405, filing 1405.
- Article 100 ceiling (720,000,000,000 IRR): rule 1405, performance
  1404 (handled/filed in 1405 — NOT "1405 performance").
- 45% growth threshold: rule 1405, performance 1404 (compares
  declared income of performance 1404 with performance 1403), filing
  1405.

Run as a module to seed the configured database::

    python -m hesabyar.infrastructure.persistence.postgres.seed_tax_policy
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import jdatetime
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from hesabyar.domain.standards.models import VerificationStatus
from hesabyar.domain.tax_policy.models import ValueUnit
from hesabyar.infrastructure.persistence.postgres.models import (
    TaxPolicyVersionModel,
    TaxSourceModel,
)

#: Evidence citation recorded on every seeded 1405 version.
VERIFIED_BY = "CURRENT_RULES_1405.md (HYA-RULES-1405-01)"

#: The 1405 baseline document's "As of" date (1405/06/29 = 2026-09-20, UTC).
VERIFIED_AT = datetime(2026, 9, 20, tzinfo=UTC)

#: Jalali year 1405 in Gregorian dates (1405/01/01 = 2026-03-21;
#: 1405/12/29 = 2027-03-20, the day before 1406/01/01).
FY1405_EFFECTIVE_FROM: date = jdatetime.date(1405, 1, 1).togregorian()
FY1405_EFFECTIVE_TO: date = jdatetime.date(1406, 1, 1).togregorian() - timedelta(days=1)

#: Version id of every seeded 1405 parameter.
VERSION_1405 = "1405"

#: The corroboration sources are news reporting, not the official
#: legal text — recorded on each source so the registry never presents
#: corroboration as an official pin.
CORROBORATION_NOTE = (
    "Corroboration source (news reporting), not the official legal text. "
    "Production activation requires pinning the official annual tax/budget "
    "source to the legal-source registry and hashing the snapshot "
    "(CURRENT_RULES_1405.md §3, §12)."
)


@dataclass(frozen=True)
class SeedTaxSource:
    """One corroboration source as seeded."""

    source_key: str
    title: str
    authority: str
    source_url: str
    notes: str = CORROBORATION_NOTE


@dataclass(frozen=True)
class SeedTaxVersion:
    """One 1405 parameter candidate as seeded."""

    rule_key: str
    rule_name: str
    version_id: str
    parameter_value: Decimal
    value_unit: ValueUnit
    rule_year: int
    performance_year: int | None
    filing_year: int | None
    source_key: str
    corroboration_urls: tuple[str, ...]
    notes: str
    effective_from: date = FY1405_EFFECTIVE_FROM
    effective_to: date | None = FY1405_EFFECTIVE_TO
    verification_status: VerificationStatus = VerificationStatus.CORROBORATED_CURRENT


#: The three Tasnim corroboration sources of CURRENT_RULES_1405.md §3.
TAX_SOURCES: tuple[SeedTaxSource, ...] = (
    SeedTaxSource(
        source_key="tasnim-budget-1404-11-25",
        title="Parliament/budget reporting on the 1405 annual tax parameters (Tasnim, 1404/11/25)",
        authority="Tasnim News Agency",
        source_url="https://www.tasnimnews.ir/fa/news/1404/11/25/3516503/",
    ),
    SeedTaxSource(
        source_key="tasnim-art100-implementation-1405-06-12",
        title="Article 100 current implementation (Tasnim, 1405/06/12)",
        authority="Tasnim News Agency",
        source_url="https://www.tasnimnews.ir/fa/news/1405/06/12/3687803/",
    ),
    SeedTaxSource(
        source_key="tasnim-art100-announcement-1405-02-28",
        title="Article 100 public announcement (Tasnim, 1405/02/28)",
        authority="Tasnim News Agency",
        source_url="https://www.tasnimnews.ir/fa/news/1405/02/28/3593811/",
    ),
)

_BUDGET_URL = TAX_SOURCES[0].source_url
_ART100_IMPL_URL = TAX_SOURCES[1].source_url
_ART100_ANNOUNCE_URL = TAX_SOURCES[2].source_url

#: The five 1405 parameter candidates of CURRENT_RULES_1405.md §3.
TAX_1405_VERSIONS: tuple[SeedTaxVersion, ...] = (
    SeedTaxVersion(
        rule_key="vat_general_rate",
        rule_name="نرخ عمومی مالیات بر ارزش افزوده",
        version_id=VERSION_1405,
        parameter_value=Decimal("10"),
        value_unit=ValueUnit.PERCENT,
        rule_year=1405,
        performance_year=1405,
        filing_year=1405,
        source_key="tasnim-budget-1404-11-25",
        corroboration_urls=(_BUDGET_URL,),
        notes=(
            "9% statutory base + 1 percentage-point annual increase for the "
            "1405 budget mechanism. Exempt/special-rate items remain separate "
            "(CURRENT_RULES_1405.md §3)."
        ),
    ),
    SeedTaxVersion(
        rule_key="art84_salary_exemption",
        rule_name="معافیت سالانه مالیات حقوق (ماده ۸۴)",
        version_id=VERSION_1405,
        parameter_value=Decimal("4800000000"),
        value_unit=ValueUnit.IRR,
        rule_year=1405,
        performance_year=1405,
        filing_year=1405,
        source_key="tasnim-budget-1404-11-25",
        corroboration_urls=(_BUDGET_URL,),
        notes=(
            "Annual 1405 threshold; payroll brackets must be modeled "
            "separately if payroll is added (CURRENT_RULES_1405.md §3)."
        ),
    ),
    SeedTaxVersion(
        rule_key="art101_exemption",
        rule_name="معافیت سالانه ماده ۱۰۱",
        version_id=VERSION_1405,
        parameter_value=Decimal("2800000000"),
        value_unit=ValueUnit.IRR,
        rule_year=1405,
        performance_year=1405,
        filing_year=1405,
        source_key="tasnim-budget-1404-11-25",
        corroboration_urls=(_BUDGET_URL,),
        notes="1405 annual parameter (CURRENT_RULES_1405.md §3).",
    ),
    SeedTaxVersion(
        rule_key="art100_flat_tax_ceiling",
        rule_name="سقف فروش مالیات مقطوع ماده ۱۰۰",
        version_id=VERSION_1405,
        parameter_value=Decimal("720000000000"),
        value_unit=ValueUnit.IRR,
        rule_year=1405,
        performance_year=1404,
        filing_year=1405,
        source_key="tasnim-art100-implementation-1405-06-12",
        corroboration_urls=(_ART100_IMPL_URL, _ART100_ANNOUNCE_URL),
        notes=("Performance year 1404 handled/filed in 1405, not '1405 performance' (CURRENT_RULES_1405.md §3)."),
    ),
    SeedTaxVersion(
        rule_key="growth_threshold_art105_131",
        rule_name="آستانه رشد درآمد اظهاری (تبصره ۷ ماده ۱۰۵ / تبصره ماده ۱۳۱)",
        version_id=VERSION_1405,
        parameter_value=Decimal("45"),
        value_unit=ValueUnit.PERCENT,
        rule_year=1405,
        performance_year=1404,
        filing_year=1405,
        source_key="tasnim-budget-1404-11-25",
        corroboration_urls=(_BUDGET_URL,),
        notes=(
            "Declared-income growth threshold for incentives under Note 7 "
            "Art. 105 / Note Art. 131. Compares declared income of "
            "performance 1404 with performance 1403 "
            "(CURRENT_RULES_1405.md §3)."
        ),
    ),
)


def seed_tax_policy(session: Session) -> dict[str, int]:
    """Upsert the 1405 sources + versions into the registry. Returns created/updated counts.

    Idempotent: re-running with the same seed data updates the same
    rows. The caller owns the transaction (commit or roll back).
    """
    counts = {
        "sources_created": 0,
        "sources_updated": 0,
        "versions_created": 0,
        "versions_updated": 0,
    }
    source_rows: dict[str, TaxSourceModel] = {}
    for seed in TAX_SOURCES:
        source = session.execute(
            select(TaxSourceModel).where(TaxSourceModel.source_key == seed.source_key)
        ).scalar_one_or_none()
        if source is None:
            source = TaxSourceModel(
                source_key=seed.source_key,
                title=seed.title,
                authority=seed.authority,
                source_url=seed.source_url,
                notes=seed.notes,
            )
            session.add(source)
            counts["sources_created"] += 1
        else:
            source.title = seed.title
            source.authority = seed.authority
            source.source_url = seed.source_url
            source.notes = seed.notes
            counts["sources_updated"] += 1
        session.flush()
        source_rows[seed.source_key] = source

    for seed in TAX_1405_VERSIONS:
        source = source_rows[seed.source_key]
        version = session.execute(
            select(TaxPolicyVersionModel).where(
                TaxPolicyVersionModel.rule_key == seed.rule_key,
                TaxPolicyVersionModel.version_id == seed.version_id,
            )
        ).scalar_one_or_none()
        if version is None:
            version = TaxPolicyVersionModel(
                source_id=source.id,
                rule_key=seed.rule_key,
                rule_name=seed.rule_name,
                version_id=seed.version_id,
                parameter_value=seed.parameter_value,
                value_unit=seed.value_unit.value,
                rule_year=seed.rule_year,
                performance_year=seed.performance_year,
                filing_year=seed.filing_year,
                effective_from=seed.effective_from,
                effective_to=seed.effective_to,
                status="active",
                verification_status=seed.verification_status.value,
                verified_by=VERIFIED_BY,
                verified_at=VERIFIED_AT,
                source_authority=source.authority,
                source_url=source.source_url,
                corroboration_urls=list(seed.corroboration_urls),
                notes=seed.notes,
            )
            session.add(version)
            counts["versions_created"] += 1
        else:
            version.source_id = source.id
            version.rule_name = seed.rule_name
            version.parameter_value = seed.parameter_value
            version.value_unit = seed.value_unit.value
            version.rule_year = seed.rule_year
            version.performance_year = seed.performance_year
            version.filing_year = seed.filing_year
            version.effective_from = seed.effective_from
            version.effective_to = seed.effective_to
            version.verification_status = seed.verification_status.value
            version.verified_by = VERIFIED_BY
            version.verified_at = VERIFIED_AT
            version.source_authority = source.authority
            version.source_url = source.source_url
            version.corroboration_urls = list(seed.corroboration_urls)
            version.notes = seed.notes
            counts["versions_updated"] += 1
    session.flush()
    return counts


def main(argv: list[str] | None = None) -> int:
    """Seed the configured database from the command line."""
    parser = argparse.ArgumentParser(description="Seed the HesabYar tax-policy registry with the 1405 candidates.")
    parser.parse_args(argv)
    from hesabyar.infrastructure.config import get_settings

    settings = get_settings()
    engine = create_engine(settings.database.sqlalchemy_url)
    with Session(engine) as session:
        counts = seed_tax_policy(session)
        session.commit()
    print("Tax-policy registry seeded:")
    for key, value in counts.items():
        print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
