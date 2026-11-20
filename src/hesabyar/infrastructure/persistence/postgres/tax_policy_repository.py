"""SQLAlchemy implementation of the tax-policy repository port (US-402).

Implements :class:`hesabyar.domain.tax_policy.ports.TaxPolicyRepositoryPort`
over the ``tax_sources`` / ``tax_policy_versions`` / ``tax_decisions``
tables. The repository speaks domain types only at its boundary: ORM
rows are mapped to domain dataclasses here, so the application layer
never sees SQLAlchemy.

Deliberately NOT tenant-scoped the way ActorRepository is: the
registry (sources + versions) is shared reference data, and decisions
are tenant-scoped by explicit argument (``get_decision`` takes the
tenant id; a decision of another tenant looks exactly like one that
does not exist). The tenant boundary is enforced one layer up, by
AuthenticatedClient, like the API-key repositories.

The repository flushes on save (so ids exist) but never commits —
the caller owns the transaction.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

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
from hesabyar.infrastructure.persistence.postgres.models import (
    TaxDecisionModel,
    TaxPolicyVersionModel,
    TaxSourceModel,
)


def _verification_status(value: str) -> VerificationStatus:
    return VerificationStatus(value)


def _version_from_row(row: TaxPolicyVersionModel, source_key: str) -> TaxPolicyVersion:
    """Map a version row onto the domain dataclass (full §12.2 provenance)."""
    return TaxPolicyVersion(
        id=row.id,
        source_id=row.source_id,
        source_key=source_key,
        rule_key=RuleKey(row.rule_key),
        rule_name=row.rule_name,
        version_id=row.version_id,
        parameter_value=row.parameter_value,
        value_unit=ValueUnit(row.value_unit),
        rule_year=row.rule_year,
        performance_year=row.performance_year,
        filing_year=row.filing_year,
        effective_from=row.effective_from,
        effective_to=row.effective_to,
        status=row.status,
        verification_status=_verification_status(row.verification_status),
        verified_by=row.verified_by,
        verified_at=row.verified_at,
        source_authority=row.source_authority,
        source_url=row.source_url,
        snapshot_uri=row.snapshot_uri,
        sha256=row.sha256,
        corroboration_urls=tuple(row.corroboration_urls or []),
        notes=row.notes,
    )


def _source_from_row(row: TaxSourceModel) -> TaxSource:
    """Map a source row onto the domain dataclass."""
    return TaxSource(
        id=row.id,
        source_key=row.source_key,
        title=row.title,
        authority=row.authority,
        source_url=row.source_url,
        snapshot_uri=row.snapshot_uri,
        sha256=row.sha256,
        notes=row.notes,
        versions_count=len(row.versions),
    )


def _decision_from_row(row: TaxDecisionModel) -> TaxDecision:
    """Map a decision row onto the domain dataclass."""
    return TaxDecision(
        id=row.id,
        tenant_id=row.tenant_id,
        company_id=row.company_id,
        rule_key=RuleKey(row.rule_key),
        decision_date=row.decision_date,
        usage_context=UsageContext(row.usage_context),
        policy_version_id=row.policy_version_id,
        version_id=row.version_id,
        parameter_value=row.parameter_value,
        value_unit=ValueUnit(row.value_unit),
        verification_status=VerificationStatus(row.verification_status),
        source_key=row.source_key,
        created_at=row.created_at,
    )


def _sort_versions_newest_effective_first(
    rows: list[TaxPolicyVersionModel],
) -> list[TaxPolicyVersionModel]:
    """Rule key ascending, newest-effective first within a rule.

    Two stable sorts — one reverse=True would flip the rule key too.
    """
    rows.sort(key=lambda row: row.effective_from, reverse=True)
    rows.sort(key=lambda row: row.rule_key)
    return rows


class SqlAlchemyTaxPolicyRepository:
    """Tax-policy registry persistence over a SQLAlchemy session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_sources(self) -> Sequence[TaxSource]:
        """Return the registry's tax sources, ordered by source key."""
        rows = list(
            self._session.execute(select(TaxSourceModel).options(selectinload(TaxSourceModel.versions))).scalars()
        )
        rows.sort(key=lambda row: row.source_key)
        return [_source_from_row(row) for row in rows]

    def get_source(self, source_key: str) -> TaxSourceDetail | None:
        """Return one source with its versions, or None for an unknown key."""
        row = self._session.execute(
            select(TaxSourceModel)
            .options(selectinload(TaxSourceModel.versions))
            .where(TaxSourceModel.source_key == source_key)
        ).scalar_one_or_none()
        if row is None:
            return None
        ordered = _sort_versions_newest_effective_first(list(row.versions))
        return TaxSourceDetail(
            source=_source_from_row(row),
            versions=tuple(_version_from_row(version, row.source_key) for version in ordered),
        )

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
        """Return policy versions, optionally filtered (see the port)."""
        stmt = select(TaxPolicyVersionModel).options(selectinload(TaxPolicyVersionModel.source))
        if rule_key is not None:
            stmt = stmt.where(TaxPolicyVersionModel.rule_key == rule_key)
        if verification_status is not None:
            stmt = stmt.where(TaxPolicyVersionModel.verification_status == verification_status.value)
        if effective_on is not None:
            stmt = stmt.where(TaxPolicyVersionModel.effective_from <= effective_on).where(
                (TaxPolicyVersionModel.effective_to.is_(None)) | (TaxPolicyVersionModel.effective_to >= effective_on)
            )
        if rule_year is not None:
            stmt = stmt.where(TaxPolicyVersionModel.rule_year == rule_year)
        if performance_year is not None:
            stmt = stmt.where(TaxPolicyVersionModel.performance_year == performance_year)
        if filing_year is not None:
            stmt = stmt.where(TaxPolicyVersionModel.filing_year == filing_year)
        rows = _sort_versions_newest_effective_first(list(self._session.execute(stmt).scalars()))
        return [_version_from_row(row, row.source.source_key) for row in rows]

    def get_version(self, version_row_id: str) -> TaxPolicyVersionDetail | None:
        """Return one version with its source, or None for an unknown row id."""
        row = self._session.execute(
            select(TaxPolicyVersionModel)
            .options(selectinload(TaxPolicyVersionModel.source))
            .where(TaxPolicyVersionModel.id == version_row_id)
        ).scalar_one_or_none()
        if row is None:
            return None
        return TaxPolicyVersionDetail(
            version=_version_from_row(row, row.source.source_key),
            source=_source_from_row(row.source),
        )

    def find_active_version(self, rule_key: str, on: datetime.date) -> TaxPolicyVersion | None:
        """Return the newest-effective active version covering ``on``, or None."""
        stmt = (
            select(TaxPolicyVersionModel)
            .options(selectinload(TaxPolicyVersionModel.source))
            .where(
                TaxPolicyVersionModel.rule_key == rule_key,
                TaxPolicyVersionModel.status == "active",
                TaxPolicyVersionModel.effective_from <= on,
                (TaxPolicyVersionModel.effective_to.is_(None)) | (TaxPolicyVersionModel.effective_to >= on),
            )
            .order_by(
                TaxPolicyVersionModel.effective_from.desc(),
                TaxPolicyVersionModel.created_at.desc(),
            )
            .limit(1)
        )
        row = self._session.execute(stmt).scalar_one_or_none()
        if row is None:
            return None
        return _version_from_row(row, row.source.source_key)

    def save_decision(self, command: RecordTaxDecision) -> TaxDecision:
        """Persist a resolved decision and return it (with id + timestamp)."""
        decision = TaxDecisionModel(
            tenant_id=command.tenant_id,
            company_id=command.company_id,
            rule_key=command.rule_key,
            decision_date=command.decision_date,
            usage_context=command.usage_context.value,
            policy_version_id=command.policy_version_id,
            version_id=command.version_id,
            parameter_value=command.parameter_value,
            value_unit=command.value_unit.value,
            verification_status=command.verification_status.value,
            source_key=command.source_key,
        )
        self._session.add(decision)
        self._session.flush()
        return _decision_from_row(decision)

    def get_decision(self, decision_id: str, tenant_id: str) -> TaxDecision | None:
        """Return one decision, or None when it does not exist for this tenant."""
        row = self._session.execute(
            select(TaxDecisionModel).where(
                TaxDecisionModel.id == decision_id,
                TaxDecisionModel.tenant_id == tenant_id,
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return _decision_from_row(row)
