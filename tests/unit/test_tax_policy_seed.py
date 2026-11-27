"""Unit tests for the tax-policy seed data (US-402).

DB-free: the seed constants are pure data. The idempotent upsert
itself is covered by tests/infrastructure/test_tax_policy_registry.py.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

from hesabyar.domain.standards.models import VerificationStatus
from hesabyar.domain.tax_policy.models import ValueUnit
from hesabyar.infrastructure.persistence.postgres.seed_tax_policy import (
    FY1405_EFFECTIVE_FROM,
    FY1405_EFFECTIVE_TO,
    TAX_1405_VERSIONS,
    TAX_SOURCES,
)


def _by_rule_key(rule_key: str):
    return next(v for v in TAX_1405_VERSIONS if v.rule_key == rule_key)


def test_fy1405_effective_dates() -> None:
    """1405/01/01 = 2026-03-21; 1405/12/29 = 2027-03-20."""
    assert FY1405_EFFECTIVE_FROM == datetime.date(2026, 3, 21)
    assert FY1405_EFFECTIVE_TO == datetime.date(2027, 3, 20)
    assert FY1405_EFFECTIVE_TO > FY1405_EFFECTIVE_FROM


def test_five_1405_versions_seeded() -> None:
    assert len(TAX_1405_VERSIONS) == 5
    assert {v.rule_key for v in TAX_1405_VERSIONS} == {
        "vat_general_rate",
        "art84_salary_exemption",
        "art101_exemption",
        "art100_flat_tax_ceiling",
        "growth_threshold_art105_131",
    }


def test_all_versions_corroborated_current() -> None:
    """Every 1405 candidate is CORROBORATED_CURRENT — never VERIFIED.

    The official source snapshot is not pinned yet, so production use
    stays blocked (CURRENT_RULES_1405.md §3, §12).
    """
    for version in TAX_1405_VERSIONS:
        assert version.verification_status == VerificationStatus.CORROBORATED_CURRENT


def test_vat_rate_is_10_percent() -> None:
    vat = _by_rule_key("vat_general_rate")
    assert vat.parameter_value == Decimal("10")
    assert vat.value_unit == ValueUnit.PERCENT
    assert vat.rule_year == 1405
    assert vat.performance_year == 1405
    assert vat.filing_year == 1405


def test_art84_exemption() -> None:
    art84 = _by_rule_key("art84_salary_exemption")
    assert art84.parameter_value == Decimal("4800000000")
    assert art84.value_unit == ValueUnit.IRR
    assert art84.rule_year == 1405


def test_art101_exemption() -> None:
    art101 = _by_rule_key("art101_exemption")
    assert art101.parameter_value == Decimal("2800000000")
    assert art101.value_unit == ValueUnit.IRR
    assert art101.rule_year == 1405


def test_art100_ceiling_years_are_separate() -> None:
    """The Article 100 ceiling is the §3 trap: 1405 rule, 1404 performance.

    Rule year, performance year and filing year must never collapse
    into a single "1405" label.
    """
    art100 = _by_rule_key("art100_flat_tax_ceiling")
    assert art100.parameter_value == Decimal("720000000000")
    assert art100.value_unit == ValueUnit.IRR
    assert art100.rule_year == 1405
    assert art100.performance_year == 1404
    assert art100.filing_year == 1405


def test_growth_threshold_compares_1404_with_1403() -> None:
    threshold = _by_rule_key("growth_threshold_art105_131")
    assert threshold.parameter_value == Decimal("45")
    assert threshold.value_unit == ValueUnit.PERCENT
    assert threshold.rule_year == 1405
    assert threshold.performance_year == 1404
    assert threshold.filing_year == 1405


def test_every_version_has_corroboration_sources() -> None:
    """Each version records its corroboration URLs, and each URL belongs to a seeded source."""
    source_urls = {source.source_url for source in TAX_SOURCES}
    for version in TAX_1405_VERSIONS:
        assert version.corroboration_urls, f"{version.rule_key} has no corroboration URLs"
        for url in version.corroboration_urls:
            assert url in source_urls, f"{version.rule_key} cites {url}, which is not a seeded source"


def test_every_version_cites_a_seeded_source() -> None:
    source_keys = {source.source_key for source in TAX_SOURCES}
    assert len(TAX_SOURCES) == 3
    for version in TAX_1405_VERSIONS:
        assert version.source_key in source_keys


def test_rule_key_and_version_id_pairs_are_unique() -> None:
    pairs = {(v.rule_key, v.version_id) for v in TAX_1405_VERSIONS}
    assert len(pairs) == len(TAX_1405_VERSIONS)


def test_effective_ranges_are_valid() -> None:
    for version in TAX_1405_VERSIONS:
        assert version.effective_from is not None
        assert version.effective_to is None or version.effective_to >= version.effective_from
