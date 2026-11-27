"""PostgreSQL implementation of the standards registry port (US-401).

Implements ``domain/standards/ports.py::StandardsRegistryPort`` over the
``standard_sources`` / ``standard_revisions`` / ``standard_rules`` /
``validation_runs`` / ``validation_results`` tables. This is the ONLY
place that knows the registry's SQLAlchemy models — the application
layer talks to the port, never to these tables (spec §6.3).

Persistence contract (same as every other writer in the codebase):
``save_validation_run`` flushes so ids/timestamps exist but does NOT
commit — the caller (REST route) owns the transaction.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from hesabyar.domain.standards.models import (
    NotExecutedRuleRecord,
    RegistryCoverage,
    RuleId,
    RuleOutcome,
    RuleSeverity,
    RunCoverage,
    StandardCode,
    StandardRevision,
    StandardRule,
    StandardSource,
    ValidationResultRecord,
    ValidationRunRecord,
    VerificationStatus,
)
from hesabyar.domain.standards.ports import StandardsRegistryPort
from hesabyar.infrastructure.persistence.postgres.models import (
    StandardRevisionModel,
    StandardRuleModel,
    StandardSourceModel,
    ValidationResultModel,
    ValidationRunModel,
)


def _verification_status(value: str) -> VerificationStatus:
    return VerificationStatus(value)


def _revision_from_row(row: StandardRevisionModel, standard_code: str) -> StandardRevision:
    """Map a revision row onto the domain dataclass (full §12.1 provenance)."""
    return StandardRevision(
        standard_code=StandardCode(standard_code),
        title=row.title,
        revision_id=row.revision_id,
        published_at=row.published_at,
        effective_from=row.effective_from,
        effective_to=row.effective_to,
        status=row.status,
        supersedes_revision_id=row.supersedes_revision_id,
        source_authority=row.source_authority,
        source_url=row.source_url,
        snapshot_uri=row.snapshot_uri,
        sha256=row.sha256,
        verification_status=_verification_status(row.verification_status),
        verified_by=row.verified_by,
        verified_at=row.verified_at,
        notes=row.notes,
    )


def _rule_from_row(row: StandardRuleModel, revision: StandardRevisionModel, standard_code: str) -> StandardRule:
    """Map a rule row onto the domain dataclass.

    The rule inherits its revision's effective range and verification
    status, so the application can decide period applicability from the
    rule alone.
    """
    return StandardRule(
        rule_id=RuleId(row.rule_id),
        standard_code=StandardCode(standard_code),
        revision_id=revision.revision_id,
        category=row.category,
        description=row.description,
        dsl_formula=row.dsl_formula,
        rule_type=row.rule_type,
        severity=RuleSeverity(row.severity),
        standard_refs=tuple(row.standard_refs or []),
        is_executable=row.is_executable,
        ruleset_version=row.ruleset_version,
        verification_status=_verification_status(revision.verification_status),
        effective_from=revision.effective_from,
        effective_to=revision.effective_to,
    )


def _source_from_row(row: StandardSourceModel) -> StandardSource:
    """Map a source row (with revisions + rules preloaded) onto the domain."""
    revisions = tuple(
        _revision_from_row(revision, row.standard_code)
        for revision in sorted(
            row.revisions,
            key=lambda r: (r.effective_from is not None, r.effective_from),
            reverse=True,
        )
    )
    return StandardSource(
        standard_code=StandardCode(row.standard_code),
        title=row.title,
        title_en=row.title_en,
        ifrs_equivalent=row.ifrs_equivalent,
        phase=row.phase,
        slug=row.slug,
        source_authority=row.source_authority,
        source_url=row.source_url,
        revisions=revisions,
        rules_count=sum(len(revision.rules) for revision in row.revisions),
    )


def _run_from_row(row: ValidationRunModel, results: Sequence[ValidationResultModel]) -> ValidationRunRecord:
    """Map a run row (+ its result rows) onto the domain record."""
    ordered = sorted(results, key=lambda r: (int(r.standard_code), r.rule_code))
    return ValidationRunRecord(
        run_id=row.id,
        tenant_id=row.tenant_id,
        company_id=row.company_id,
        period_start=row.period_start,
        period_end=row.period_end,
        standard_codes=tuple(row.standard_codes),
        ruleset_version=row.ruleset_version,
        input_hash=row.input_hash,
        overall_outcome=RuleOutcome(row.overall_outcome),
        is_valid=row.is_valid,
        rules_executed=row.rules_executed,
        rules_passed=row.rules_passed,
        rules_failed=row.rules_failed,
        rules_not_applicable=row.rules_not_applicable,
        rules_errored=row.rules_errored,
        coverage=RunCoverage(
            corpus_standards=row.coverage_corpus_standards,
            standards_with_rules=row.coverage_standards_with_rules,
            rules_implemented=row.coverage_rules_implemented,
            rules_verified=row.coverage_rules_verified,
            rules_executed=row.coverage_rules_executed,
        ),
        results=tuple(
            ValidationResultRecord(
                rule_id=RuleId(result.rule_code),
                standard_code=StandardCode(result.standard_code),
                revision_id=result.revision_id,
                outcome=RuleOutcome(result.outcome),
                severity=RuleSeverity(result.severity),
                message=result.message,
                detail=result.detail or "",
                dsl_formula=result.dsl_formula,
            )
            for result in ordered
        ),
        not_executed=tuple(
            NotExecutedRuleRecord(
                rule_id=RuleId(str(item["rule_id"])),
                standard_code=StandardCode(str(item["standard_code"])),
                revision_id=str(item["revision_id"]),
                reason=str(item["reason"]),
            )
            for item in (row.not_executed_rules or [])
        ),
        created_at=row.created_at,
    )


class PostgresStandardsRegistry(StandardsRegistryPort):
    """The standards registry port backed by the HesabYar database."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_sources(self) -> Sequence[StandardSource]:
        """All registered standards, ordered by standard_code numerically."""
        stmt = select(StandardSourceModel).options(
            selectinload(StandardSourceModel.revisions).selectinload(StandardRevisionModel.rules)
        )
        rows = list(self._session.execute(stmt).scalars())
        rows.sort(key=lambda row: int(row.standard_code))
        return [_source_from_row(row) for row in rows]

    def get_source(self, standard_code: StandardCode) -> StandardSource | None:
        """One standard with its revisions, or None when unknown."""
        row = self._session.execute(
            select(StandardSourceModel)
            .options(selectinload(StandardSourceModel.revisions).selectinload(StandardRevisionModel.rules))
            .where(StandardSourceModel.standard_code == standard_code)
        ).scalar_one_or_none()
        if row is None:
            return None
        return _source_from_row(row)

    def get_rules(self, standard_codes: Sequence[StandardCode] | None = None) -> Sequence[StandardRule]:
        """Rules for the given standards (all standards when None)."""
        stmt = (
            select(StandardRuleModel, StandardRevisionModel, StandardSourceModel.standard_code)
            .join(StandardRevisionModel, StandardRuleModel.revision_id == StandardRevisionModel.id)
            .join(StandardSourceModel, StandardRevisionModel.source_id == StandardSourceModel.id)
        )
        if standard_codes is not None:
            stmt = stmt.where(StandardSourceModel.standard_code.in_(list(standard_codes)))
        rows = list(self._session.execute(stmt).all())
        rows.sort(key=lambda item: (int(item[2]), item[0].rule_id))
        return [_rule_from_row(rule, revision, code) for rule, revision, code in rows]

    def coverage(self) -> RegistryCoverage:
        """Registry coverage counters, reported separately (spec §12.1)."""
        corpus = self._session.execute(select(func.count()).select_from(StandardSourceModel)).scalar_one()
        standards_with_rules = self._session.execute(
            select(func.count(func.distinct(StandardSourceModel.id)))
            .select_from(StandardSourceModel)
            .join(
                StandardRevisionModel,
                StandardRevisionModel.source_id == StandardSourceModel.id,
            )
            .join(
                StandardRuleModel,
                StandardRuleModel.revision_id == StandardRevisionModel.id,
            )
        ).scalar_one()
        implemented = self._session.execute(select(func.count()).select_from(StandardRuleModel)).scalar_one()
        verified = self._session.execute(
            select(func.count())
            .select_from(StandardRuleModel)
            .join(
                StandardRevisionModel,
                StandardRuleModel.revision_id == StandardRevisionModel.id,
            )
            .where(StandardRevisionModel.verification_status == VerificationStatus.VERIFIED_TEXT.value)
        ).scalar_one()
        executable = self._session.execute(
            select(func.count()).select_from(StandardRuleModel).where(StandardRuleModel.is_executable.is_(True))
        ).scalar_one()
        return RegistryCoverage(
            corpus_standards=corpus,
            standards_with_rules=standards_with_rules,
            rules_implemented=implemented,
            rules_verified=verified,
            rules_executable=executable,
        )

    def ruleset_version(self) -> str:
        """Ruleset version of the latest seeded revision (e.g. '1.0.0-legacy')."""
        versions = (
            self._session.execute(
                select(StandardRuleModel.ruleset_version)
                .where(StandardRuleModel.ruleset_version.is_not(None))
                .distinct()
            )
            .scalars()
            .all()
        )
        versions = sorted(version for version in versions if version is not None)
        return versions[-1] if versions else "unknown"

    def save_validation_run(self, record: ValidationRunRecord) -> ValidationRunRecord:
        """Persist a run and its results; returns it with ``created_at`` set.

        Flushes so ids/timestamps exist; does NOT commit — the caller
        owns the transaction.
        """
        run = ValidationRunModel(
            id=record.run_id,
            tenant_id=record.tenant_id,
            company_id=record.company_id,
            period_start=record.period_start,
            period_end=record.period_end,
            standard_codes=list(record.standard_codes),
            ruleset_version=record.ruleset_version or "unknown",
            input_hash=record.input_hash,
            overall_outcome=record.overall_outcome.value,
            is_valid=record.is_valid,
            rules_executed=record.rules_executed,
            rules_passed=record.rules_passed,
            rules_failed=record.rules_failed,
            rules_not_applicable=record.rules_not_applicable,
            rules_errored=record.rules_errored,
            coverage_corpus_standards=record.coverage.corpus_standards,
            coverage_standards_with_rules=record.coverage.standards_with_rules,
            coverage_rules_implemented=record.coverage.rules_implemented,
            coverage_rules_verified=record.coverage.rules_verified,
            coverage_rules_executed=record.coverage.rules_executed,
            not_executed_rules=[
                {
                    "rule_id": item.rule_id,
                    "standard_code": item.standard_code,
                    "revision_id": item.revision_id,
                    "reason": item.reason,
                }
                for item in record.not_executed
            ],
        )
        self._session.add(run)
        self._session.flush()
        rule_row_ids = {
            (rule.rule_id, code): rule.id
            for rule, _, code in self._session.execute(
                select(StandardRuleModel, StandardRevisionModel, StandardSourceModel.standard_code)
                .join(StandardRevisionModel, StandardRuleModel.revision_id == StandardRevisionModel.id)
                .join(StandardSourceModel, StandardRevisionModel.source_id == StandardSourceModel.id)
                .where(StandardSourceModel.standard_code.in_(list(record.standard_codes)))
            ).all()
        }
        result_rows: list[ValidationResultModel] = []
        for result in record.results:
            result_rows.append(
                ValidationResultModel(
                    run_id=run.id,
                    rule_row_id=rule_row_ids.get((result.rule_id, result.standard_code)),
                    rule_code=result.rule_id,
                    standard_code=result.standard_code,
                    revision_id=result.revision_id,
                    outcome=result.outcome.value,
                    severity=result.severity.value,
                    message=result.message,
                    detail=result.detail or None,
                    dsl_formula=result.dsl_formula,
                )
            )
        self._session.add_all(result_rows)
        self._session.flush()
        return dataclasses.replace(record, created_at=run.created_at)

    def get_validation_run(self, run_id: str, tenant_id: str) -> ValidationRunRecord | None:
        """One run with its results for the owning tenant, or None."""
        run = self._session.execute(
            select(ValidationRunModel).where(
                ValidationRunModel.id == run_id,
                ValidationRunModel.tenant_id == tenant_id,
            )
        ).scalar_one_or_none()
        if run is None:
            return None
        rows = list(
            self._session.execute(select(ValidationResultModel).where(ValidationResultModel.run_id == run.id)).scalars()
        )
        return _run_from_row(run, rows)
