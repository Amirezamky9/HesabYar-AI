"""Unit tests for the tax-policy query + decision services (US-402).

DB-free: the services depend only on the
:class:`hesabyar.domain.tax_policy.ports.TaxPolicyRepositoryPort`, so
these tests run them against an in-memory fake seeded from the same
1405 seed constants the SQLAlchemy repository seeds into the database.
The repository implementation itself is covered by
tests/infrastructure/test_tax_policy_repository.py (real database).
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Sequence
from decimal import Decimal

import pytest

from hesabyar.application.commands.tax_policy import TaxDecisionService
from hesabyar.application.errors import (
    TaxDecisionNotFoundError,
    TaxPolicyNotFoundError,
    TaxPolicySourceBlockedError,
    TaxSourceNotFoundError,
)
from hesabyar.application.queries.tax_policy import TaxPolicyQueryService
from hesabyar.domain.standards.models import VerificationStatus
from hesabyar.domain.tax_policy.commands import RecordTaxDecision
from hesabyar.domain.tax_policy.models import (
    RuleKey,
    TaxDecision,
    TaxPolicyVersion,
    TaxPolicyVersionDetail,
    TaxSource,
    TaxSourceDetail,
    UsageContext,
    ValueUnit,
)
from hesabyar.infrastructure.persistence.postgres.seed_tax_policy import (
    FY1405_EFFECTIVE_FROM,
    FY1405_EFFECTIVE_TO,
    TAX_1405_VERSIONS,
    TAX_SOURCES,
    VERIFIED_AT,
    VERIFIED_BY,
)

# Effective-date boundaries of the seeded 1405 versions.
DAY_BEFORE_1405 = FY1405_EFFECTIVE_FROM - datetime.timedelta(days=1)
DAY_AFTER_1405 = FY1405_EFFECTIVE_TO + datetime.timedelta(days=1)
MID_1405 = datetime.date(2026, 9, 20)
HISTORICAL = datetime.date(2024, 6, 15)
FUTURE = datetime.date(2028, 1, 1)


class InMemoryTaxPolicyRepository:
    """In-memory fake of the TaxPolicyRepositoryPort, seeded from the 1405 constants."""

    def __init__(self) -> None:
        self.sources: dict[str, TaxSource] = {}
        self._source_ids: dict[str, str] = {}
        for seed in TAX_SOURCES:
            source_id = str(uuid.uuid4())
            self._source_ids[seed.source_key] = source_id
            self.sources[seed.source_key] = TaxSource(
                id=source_id,
                source_key=seed.source_key,
                title=seed.title,
                authority=seed.authority,
                source_url=seed.source_url,
                notes=seed.notes,
            )
        self.versions: list[TaxPolicyVersion] = []
        for seed in TAX_1405_VERSIONS:
            source = self.sources[seed.source_key]
            self.versions.append(
                TaxPolicyVersion(
                    id=str(uuid.uuid4()),
                    source_id=source.id,
                    source_key=seed.source_key,
                    rule_key=RuleKey(seed.rule_key),
                    rule_name=seed.rule_name,
                    version_id=seed.version_id,
                    parameter_value=seed.parameter_value,
                    value_unit=seed.value_unit,
                    rule_year=seed.rule_year,
                    performance_year=seed.performance_year,
                    filing_year=seed.filing_year,
                    effective_from=seed.effective_from,
                    effective_to=seed.effective_to,
                    status="active",
                    verification_status=seed.verification_status,
                    verified_by=VERIFIED_BY,
                    verified_at=VERIFIED_AT,
                    source_authority=source.authority,
                    source_url=source.source_url,
                    corroboration_urls=seed.corroboration_urls,
                    notes=seed.notes,
                )
            )
        self.decisions: list[TaxDecision] = []

    def add_version(self, version: TaxPolicyVersion) -> None:
        """Add an extra version (test-only setup, e.g. superseded or verified rows)."""
        self.versions.append(version)

    def _with_counts(self) -> list[TaxSource]:
        return [
            TaxSource(
                id=source.id,
                source_key=source.source_key,
                title=source.title,
                authority=source.authority,
                source_url=source.source_url,
                snapshot_uri=source.snapshot_uri,
                sha256=source.sha256,
                notes=source.notes,
                versions_count=sum(1 for v in self.versions if v.source_key == source.source_key),
            )
            for source in sorted(self.sources.values(), key=lambda s: s.source_key)
        ]

    @staticmethod
    def _sorted(versions: list[TaxPolicyVersion]) -> list[TaxPolicyVersion]:
        ordered = sorted(versions, key=lambda v: v.effective_from or datetime.date.min, reverse=True)
        ordered.sort(key=lambda v: v.rule_key)
        return ordered

    def list_sources(self) -> Sequence[TaxSource]:
        return self._with_counts()

    def get_source(self, source_key: str) -> TaxSourceDetail | None:
        matches = [s for s in self._with_counts() if s.source_key == source_key]
        if not matches:
            return None
        versions = self._sorted([v for v in self.versions if v.source_key == source_key])
        return TaxSourceDetail(source=matches[0], versions=tuple(versions))

    def list_versions(
        self,
        *,
        rule_key: str | None = None,
        verification_status: VerificationStatus | None = None,
        effective_on: datetime.date | None = None,
        rule_year: int | None = None,
        performance_year: int | None = None,
        filing_year: int | None = None,
    ) -> Sequence[TaxPolicyVersion]:
        versions = list(self.versions)
        if rule_key is not None:
            versions = [v for v in versions if v.rule_key == rule_key]
        if verification_status is not None:
            versions = [v for v in versions if v.verification_status == verification_status]
        if effective_on is not None:
            versions = [v for v in versions if v.is_effective_on(effective_on)]
        if rule_year is not None:
            versions = [v for v in versions if v.rule_year == rule_year]
        if performance_year is not None:
            versions = [v for v in versions if v.performance_year == performance_year]
        if filing_year is not None:
            versions = [v for v in versions if v.filing_year == filing_year]
        return self._sorted(versions)

    def get_version(self, version_row_id: str) -> TaxPolicyVersionDetail | None:
        for version in self.versions:
            if version.id == version_row_id:
                source = next(s for s in self._with_counts() if s.source_key == version.source_key)
                return TaxPolicyVersionDetail(version=version, source=source)
        return None

    def find_active_version(self, rule_key: str, on: datetime.date) -> TaxPolicyVersion | None:
        candidates = [
            v for v in self.versions if v.rule_key == rule_key and v.status == "active" and v.is_effective_on(on)
        ]
        if not candidates:
            return None
        # Newest-effective wins; insertion order breaks ties (last added wins).
        candidates.sort(key=lambda v: v.effective_from or datetime.date.min)
        return candidates[-1]

    def save_decision(self, command: RecordTaxDecision) -> TaxDecision:
        decision = TaxDecision(
            id=str(uuid.uuid4()),
            tenant_id=command.tenant_id,
            company_id=command.company_id,
            rule_key=command.rule_key,
            decision_date=command.decision_date,
            usage_context=command.usage_context,
            policy_version_id=command.policy_version_id,
            version_id=command.version_id,
            parameter_value=command.parameter_value,
            value_unit=command.value_unit,
            verification_status=command.verification_status,
            source_key=command.source_key,
            created_at=datetime.datetime.now(datetime.UTC),
        )
        self.decisions.append(decision)
        return decision

    def get_decision(self, decision_id: str, tenant_id: str) -> TaxDecision | None:
        for decision in self.decisions:
            if decision.id == decision_id and decision.tenant_id == tenant_id:
                return decision
        return None

    def decision_count(self, tenant_id: str) -> int:
        return sum(1 for d in self.decisions if d.tenant_id == tenant_id)


@pytest.fixture()
def repository() -> InMemoryTaxPolicyRepository:
    return InMemoryTaxPolicyRepository()


@pytest.fixture()
def queries(repository: InMemoryTaxPolicyRepository) -> TaxPolicyQueryService:
    return TaxPolicyQueryService(repository)


@pytest.fixture()
def decisions(repository: InMemoryTaxPolicyRepository) -> TaxDecisionService:
    return TaxDecisionService(repository)


@pytest.fixture()
def tenant_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture()
def other_tenant_id() -> str:
    return str(uuid.uuid4())


def _extra_version(
    repository: InMemoryTaxPolicyRepository,
    *,
    rule_key: str,
    version_id: str,
    parameter_value: Decimal,
    verification_status: VerificationStatus,
    status: str = "active",
    effective_from: datetime.date = FY1405_EFFECTIVE_FROM,
    effective_to: datetime.date | None = FY1405_EFFECTIVE_TO,
) -> TaxPolicyVersion:
    """Build an extra version against the budget source (test-only setup)."""
    source = repository.sources["tasnim-budget-1404-11-25"]
    return TaxPolicyVersion(
        id=str(uuid.uuid4()),
        source_id=source.id,
        source_key=source.source_key,
        rule_key=RuleKey(rule_key),
        rule_name="قاعده آزمایشی",
        version_id=version_id,
        parameter_value=parameter_value,
        value_unit=ValueUnit.PERCENT,
        rule_year=1405,
        performance_year=1405,
        filing_year=1405,
        effective_from=effective_from,
        effective_to=effective_to,
        status=status,
        verification_status=verification_status,
    )


# ---------------------------------------------------------------------------
# Query service: sources
# ---------------------------------------------------------------------------


def test_list_sources_returns_three_in_key_order(queries: TaxPolicyQueryService) -> None:
    sources = queries.list_sources()
    assert [s.source_key for s in sources] == [
        "tasnim-art100-announcement-1405-02-28",
        "tasnim-art100-implementation-1405-06-12",
        "tasnim-budget-1404-11-25",
    ]
    assert [s.versions_count for s in sources] == [0, 1, 4]
    budget = next(s for s in sources if s.source_key == "tasnim-budget-1404-11-25")
    assert budget.authority == "Tasnim News Agency"
    assert budget.sha256 is None  # corroboration only — no official snapshot pinned


def test_get_source_returns_versions(queries: TaxPolicyQueryService) -> None:
    detail = queries.get_source("tasnim-budget-1404-11-25")
    assert detail.source.source_key == "tasnim-budget-1404-11-25"
    assert [v.rule_key for v in detail.versions] == [
        "art101_exemption",
        "art84_salary_exemption",
        "growth_threshold_art105_131",
        "vat_general_rate",
    ]
    assert all(v.verification_status == VerificationStatus.CORROBORATED_CURRENT for v in detail.versions)


def test_get_source_unknown_key_raises(queries: TaxPolicyQueryService) -> None:
    with pytest.raises(TaxSourceNotFoundError) as exc_info:
        queries.get_source("no-such-source")
    assert exc_info.value.code == "TAX_SOURCE_NOT_FOUND"


# ---------------------------------------------------------------------------
# Query service: versions
# ---------------------------------------------------------------------------


def test_list_versions_returns_five(queries: TaxPolicyQueryService) -> None:
    versions = queries.list_versions()
    assert len(versions) == 5
    assert [v.rule_key for v in versions] == [
        "art100_flat_tax_ceiling",
        "art101_exemption",
        "art84_salary_exemption",
        "growth_threshold_art105_131",
        "vat_general_rate",
    ]


def test_list_versions_filter_by_rule_key(queries: TaxPolicyQueryService) -> None:
    versions = queries.list_versions(rule_key="vat_general_rate")
    assert len(versions) == 1
    vat = versions[0]
    assert vat.parameter_value == Decimal("10")
    assert vat.value_unit == ValueUnit.PERCENT
    assert vat.source_key == "tasnim-budget-1404-11-25"
    assert vat.corroboration_urls == ("https://www.tasnimnews.ir/fa/news/1404/11/25/3516503/",)


def test_list_versions_filter_by_verification_status(queries: TaxPolicyQueryService) -> None:
    assert len(queries.list_versions(verification_status=VerificationStatus.CORROBORATED_CURRENT)) == 5
    assert queries.list_versions(verification_status=VerificationStatus.VERIFIED_TEXT) == []


def test_list_versions_filter_by_effective_on(queries: TaxPolicyQueryService) -> None:
    assert len(queries.list_versions(effective_on=MID_1405)) == 5
    assert len(queries.list_versions(effective_on=FY1405_EFFECTIVE_FROM)) == 5
    assert len(queries.list_versions(effective_on=FY1405_EFFECTIVE_TO)) == 5
    assert queries.list_versions(effective_on=DAY_BEFORE_1405) == []
    assert queries.list_versions(effective_on=DAY_AFTER_1405) == []


def test_list_versions_filter_by_years(queries: TaxPolicyQueryService) -> None:
    assert len(queries.list_versions(rule_year=1405)) == 5
    assert len(queries.list_versions(rule_year=1404)) == 0
    # Only the Article 100 ceiling and the growth threshold govern performance 1404.
    performance_1404 = queries.list_versions(performance_year=1404)
    assert {v.rule_key for v in performance_1404} == {
        "art100_flat_tax_ceiling",
        "growth_threshold_art105_131",
    }
    assert len(queries.list_versions(performance_year=1405)) == 3
    assert len(queries.list_versions(filing_year=1405)) == 5


def test_get_version_returns_version_with_source(queries: TaxPolicyQueryService) -> None:
    version = queries.list_versions(rule_key="art100_flat_tax_ceiling")[0]
    detail = queries.get_version(version.id)
    assert detail.version.rule_key == "art100_flat_tax_ceiling"
    assert detail.version.parameter_value == Decimal("720000000000")
    assert detail.version.performance_year == 1404
    assert detail.version.filing_year == 1405
    assert detail.source.source_key == "tasnim-art100-implementation-1405-06-12"
    assert detail.source.title.startswith("Article 100 current implementation")


def test_get_version_unknown_id_raises(queries: TaxPolicyQueryService) -> None:
    with pytest.raises(TaxPolicyNotFoundError) as exc_info:
        queries.get_version(str(uuid.uuid4()))
    assert exc_info.value.code == "TAX_POLICY_NOT_FOUND"


# ---------------------------------------------------------------------------
# Decision service: resolution + boundaries
# ---------------------------------------------------------------------------


def test_resolve_reference_decision_mid_period(decisions: TaxDecisionService, tenant_id: str) -> None:
    decision = decisions.resolve_decision(
        tenant_id=tenant_id,
        company_id="company-1",
        rule_key="vat_general_rate",
        decision_date=MID_1405,
    )
    assert decision.rule_key == "vat_general_rate"
    assert decision.decision_date == MID_1405
    assert decision.usage_context == UsageContext.REFERENCE
    assert decision.parameter_value == Decimal("10")
    assert decision.value_unit == ValueUnit.PERCENT
    assert decision.version_id == "1405"
    assert decision.verification_status == VerificationStatus.CORROBORATED_CURRENT
    assert decision.source_key == "tasnim-budget-1404-11-25"
    assert decision.policy_version_id is not None
    assert decision.company_id == "company-1"


def test_resolve_on_effective_from_boundary(decisions: TaxDecisionService, tenant_id: str) -> None:
    """effective_from is inclusive: the first day resolves."""
    decision = decisions.resolve_decision(
        tenant_id=tenant_id, rule_key="art84_salary_exemption", decision_date=FY1405_EFFECTIVE_FROM
    )
    assert decision.parameter_value == Decimal("4800000000")


def test_resolve_on_effective_to_boundary(decisions: TaxDecisionService, tenant_id: str) -> None:
    """effective_to is inclusive: the last day resolves."""
    decision = decisions.resolve_decision(
        tenant_id=tenant_id, rule_key="art101_exemption", decision_date=FY1405_EFFECTIVE_TO
    )
    assert decision.parameter_value == Decimal("2800000000")


def test_resolve_day_before_effective_from_raises(decisions: TaxDecisionService, tenant_id: str) -> None:
    with pytest.raises(TaxPolicyNotFoundError) as exc_info:
        decisions.resolve_decision(tenant_id=tenant_id, rule_key="vat_general_rate", decision_date=DAY_BEFORE_1405)
    assert exc_info.value.code == "TAX_POLICY_NOT_FOUND"


def test_resolve_day_after_effective_to_raises(decisions: TaxDecisionService, tenant_id: str) -> None:
    with pytest.raises(TaxPolicyNotFoundError):
        decisions.resolve_decision(tenant_id=tenant_id, rule_key="vat_general_rate", decision_date=DAY_AFTER_1405)


def test_resolve_historical_date_raises(decisions: TaxDecisionService, tenant_id: str) -> None:
    """A historical date with no covering version never falls back to another year's parameter."""
    with pytest.raises(TaxPolicyNotFoundError):
        decisions.resolve_decision(tenant_id=tenant_id, rule_key="vat_general_rate", decision_date=HISTORICAL)


def test_resolve_future_date_raises(decisions: TaxDecisionService, tenant_id: str) -> None:
    with pytest.raises(TaxPolicyNotFoundError):
        decisions.resolve_decision(tenant_id=tenant_id, rule_key="vat_general_rate", decision_date=FUTURE)


def test_resolve_unknown_rule_raises(decisions: TaxDecisionService, tenant_id: str) -> None:
    with pytest.raises(TaxPolicyNotFoundError) as exc_info:
        decisions.resolve_decision(tenant_id=tenant_id, rule_key="no_such_rule", decision_date=MID_1405)
    assert exc_info.value.code == "TAX_POLICY_NOT_FOUND"


def test_superseded_version_is_never_resolved(
    repository: InMemoryTaxPolicyRepository, decisions: TaxDecisionService, tenant_id: str
) -> None:
    """A superseded version covering the date must not shadow the active one."""
    repository.add_version(
        _extra_version(
            repository,
            rule_key="vat_general_rate",
            version_id="1405-superseded",
            parameter_value=Decimal("99"),
            verification_status=VerificationStatus.VERIFIED_TEXT,
            status="superseded",
        )
    )
    decision = decisions.resolve_decision(tenant_id=tenant_id, rule_key="vat_general_rate", decision_date=MID_1405)
    assert decision.parameter_value == Decimal("10")
    assert decision.version_id == "1405"


def test_newest_effective_active_version_wins(
    repository: InMemoryTaxPolicyRepository, decisions: TaxDecisionService, tenant_id: str
) -> None:
    """When two active versions cover a date, the newest-effective one resolves."""
    repository.add_version(
        _extra_version(
            repository,
            rule_key="vat_general_rate",
            version_id="1405-rev2",
            parameter_value=Decimal("11"),
            verification_status=VerificationStatus.VERIFIED_TEXT,
            effective_from=datetime.date(2026, 9, 1),
        )
    )
    # Before the revision's effective date the original still resolves.
    early = decisions.resolve_decision(
        tenant_id=tenant_id, rule_key="vat_general_rate", decision_date=datetime.date(2026, 6, 1)
    )
    assert early.version_id == "1405"
    # On/after it, the newest-effective version resolves.
    late = decisions.resolve_decision(tenant_id=tenant_id, rule_key="vat_general_rate", decision_date=MID_1405)
    assert late.version_id == "1405-rev2"
    assert late.parameter_value == Decimal("11")


# ---------------------------------------------------------------------------
# Decision service: production gate
# ---------------------------------------------------------------------------


def test_production_resolution_of_corroborated_version_is_blocked(
    repository: InMemoryTaxPolicyRepository, decisions: TaxDecisionService, tenant_id: str
) -> None:
    """The CURRENT_RULES_1405.md gate: CORROBORATED_CURRENT can never drive production."""
    with pytest.raises(TaxPolicySourceBlockedError) as exc_info:
        decisions.resolve_decision(
            tenant_id=tenant_id,
            rule_key="vat_general_rate",
            decision_date=MID_1405,
            usage_context=UsageContext.PRODUCTION,
        )
    assert exc_info.value.code == "TAX_POLICY_SOURCE_BLOCKED"
    assert "official source" in exc_info.value.message
    assert "pin" in exc_info.value.message.lower()
    assert exc_info.value.details["verification_status"] == "CORROBORATED_CURRENT"
    # A blocked attempt records no decision.
    assert repository.decision_count(tenant_id) == 0


def test_production_gate_blocks_every_non_verified_status(
    repository: InMemoryTaxPolicyRepository, decisions: TaxDecisionService, tenant_id: str
) -> None:
    """UNVERIFIED / DISCOVERY_ONLY / BLOCKED_SOURCE_GAP / TRANSITIONAL all block, not just CORROBORATED."""
    for index, status in enumerate(
        [
            VerificationStatus.UNVERIFIED,
            VerificationStatus.DISCOVERY_ONLY,
            VerificationStatus.BLOCKED_SOURCE_GAP,
            VerificationStatus.TRANSITIONAL,
        ]
    ):
        repository.add_version(
            _extra_version(
                repository,
                rule_key=f"test_rule_{index}",
                version_id="1405",
                parameter_value=Decimal("1"),
                verification_status=status,
            )
        )
    for index in range(4):
        with pytest.raises(TaxPolicySourceBlockedError):
            decisions.resolve_decision(
                tenant_id=tenant_id,
                rule_key=f"test_rule_{index}",
                decision_date=MID_1405,
                usage_context=UsageContext.PRODUCTION,
            )
    assert repository.decision_count(tenant_id) == 0


def test_production_resolution_of_verified_version_succeeds(
    repository: InMemoryTaxPolicyRepository, decisions: TaxDecisionService, tenant_id: str
) -> None:
    """The gate is status-based, not a blanket block: VERIFIED_TEXT resolves for production."""
    repository.add_version(
        _extra_version(
            repository,
            rule_key="verified_test_rule",
            version_id="1405",
            parameter_value=Decimal("7"),
            verification_status=VerificationStatus.VERIFIED_TEXT,
        )
    )
    decision = decisions.resolve_decision(
        tenant_id=tenant_id,
        rule_key="verified_test_rule",
        decision_date=MID_1405,
        usage_context=UsageContext.PRODUCTION,
    )
    assert decision.usage_context == UsageContext.PRODUCTION
    assert decision.parameter_value == Decimal("7")
    assert decision.verification_status == VerificationStatus.VERIFIED_TEXT


# ---------------------------------------------------------------------------
# Decision service: get + tenant isolation
# ---------------------------------------------------------------------------


def test_get_decision_roundtrip(decisions: TaxDecisionService, tenant_id: str) -> None:
    created = decisions.resolve_decision(
        tenant_id=tenant_id, rule_key="art100_flat_tax_ceiling", decision_date=MID_1405
    )
    fetched = decisions.get_decision(created.id, tenant_id)
    assert fetched.id == created.id
    assert fetched.parameter_value == Decimal("720000000000")
    assert fetched.value_unit == ValueUnit.IRR
    assert fetched.source_key == "tasnim-art100-implementation-1405-06-12"


def test_get_decision_unknown_id_raises(decisions: TaxDecisionService, tenant_id: str) -> None:
    with pytest.raises(TaxDecisionNotFoundError) as exc_info:
        decisions.get_decision(str(uuid.uuid4()), tenant_id)
    assert exc_info.value.code == "TAX_DECISION_NOT_FOUND"


def test_get_decision_other_tenant_raises(decisions: TaxDecisionService, tenant_id: str, other_tenant_id: str) -> None:
    created = decisions.resolve_decision(tenant_id=tenant_id, rule_key="vat_general_rate", decision_date=MID_1405)
    with pytest.raises(TaxDecisionNotFoundError):
        decisions.get_decision(created.id, other_tenant_id)
