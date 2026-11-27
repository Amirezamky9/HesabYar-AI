"""Response schemas for the standards registry + validation endpoints (US-401).

Responses map from the domain views — no ORM object ever leaks. The
coverage block reports corpus / implemented / verified / executed
separately (spec §12.1); the run response also lists the rules that
were implemented but not executed, so the gap between "implemented"
and "executed" is always visible, never hidden.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator

from hesabyar.application.queries.standards import StandardDetail
from hesabyar.domain.standards.models import StandardSource, ValidationRunRecord


class StandardRevisionResponse(BaseModel):
    """One revision of a standard, with full §12.1 provenance."""

    revision_id: str = Field(description="Revision identifier of the bundled text")
    title: str = Field(description="Persian title of this revision")
    published_at: date | None = Field(description="Publication date, when known")
    effective_from: date | None = Field(description="First period-begin date the revision applies to")
    effective_to: date | None = Field(description="Last period-begin date the revision applies to")
    status: str = Field(description="Registry lifecycle state: 'active' or 'superseded'")
    supersedes_revision_id: str | None = Field(description="Revision id this revision supersedes")
    source_authority: str | None = Field(description="Issuing authority for this revision")
    source_url: str | None = Field(description="Source document URL for this revision")
    snapshot_uri: str | None = Field(description="Repo-relative path of the snapshotted text")
    sha256: str | None = Field(description="SHA-256 hex of the snapshotted source text bytes")
    verification_status: str = Field(description="Verification label (CURRENT_RULES_1405.md §1 + UNVERIFIED)")
    verified_by: str | None = Field(description="Who/what verified this revision")
    verified_at: datetime | None = Field(description="When the verification was recorded (UTC)")
    notes: str | None = Field(description="Supersession / applicability notes")


class StandardRuleResponse(BaseModel):
    """One machine-readable validation rule of a standard revision."""

    rule_id: str = Field(description="Stable rule identifier, e.g. 'BS001'")
    category: str = Field(description="Rule group, e.g. 'balance_sheet'")
    description: str = Field(description="Persian rule description")
    dsl_formula: str | None = Field(description="Boolean expression in the closed typed DSL (null when not executable)")
    rule_type: str = Field(description="Rule kind: 'equation', 'threshold' or 'requirement'")
    severity: str = Field(description="Rule severity: 'error' or 'warning'")
    standard_refs: list[str] = Field(description="Standard paragraph references, e.g. ['1.55']")
    is_executable: bool = Field(description="False when the rule has no DSL formula and cannot be evaluated")
    ruleset_version: str | None = Field(description="Source ruleset version the rule was ported from")
    verification_status: str = Field(description="Verification label of the rule")


class StandardListItemResponse(BaseModel):
    """One standard in the registry list, with its latest revision summarized."""

    standard_code: str = Field(description="Plain standard number as a string, e.g. '1', '43'")
    title: str = Field(description="Persian title")
    title_en: str | None = Field(description="English title")
    ifrs_equivalent: str | None = Field(description="IFRS/IAS equivalent, e.g. 'IAS 1'")
    phase: int | None = Field(description="Corpus phase (1-8)")
    slug: str = Field(description="URL slug, e.g. '01-presentation'")
    source_authority: str | None = Field(description="Issuing authority")
    source_url: str | None = Field(description="Canonical source document URL")
    latest_revision_id: str | None = Field(description="Revision id of the latest revision")
    latest_verification_status: str | None = Field(description="Verification label of the latest revision")
    latest_effective_from: date | None = Field(description="First period-begin date the latest revision applies to")
    latest_effective_to: date | None = Field(description="Last period-begin date the latest revision applies to")
    rules_count: int = Field(description="Number of machine-readable rules of this standard")

    @classmethod
    def from_source(cls, source: StandardSource) -> StandardListItemResponse:
        """Build a list item from a domain source view."""
        latest = source.latest_revision
        return cls(
            standard_code=source.standard_code,
            title=source.title,
            title_en=source.title_en,
            ifrs_equivalent=source.ifrs_equivalent,
            phase=source.phase,
            slug=source.slug,
            source_authority=source.source_authority,
            source_url=source.source_url,
            latest_revision_id=latest.revision_id if latest else None,
            latest_verification_status=latest.verification_status.value if latest else None,
            latest_effective_from=latest.effective_from if latest else None,
            latest_effective_to=latest.effective_to if latest else None,
            rules_count=source.rules_count,
        )


class StandardDetailResponse(BaseModel):
    """One standard with its revisions and its machine-readable rules."""

    standard_code: str = Field(description="Plain standard number as a string")
    title: str = Field(description="Persian title")
    title_en: str | None = Field(description="English title")
    ifrs_equivalent: str | None = Field(description="IFRS/IAS equivalent")
    phase: int | None = Field(description="Corpus phase (1-8)")
    slug: str = Field(description="URL slug")
    source_authority: str | None = Field(description="Issuing authority")
    source_url: str | None = Field(description="Canonical source document URL")
    revisions: list[StandardRevisionResponse] = Field(description="Revisions, newest-effective first")
    rules: list[StandardRuleResponse] = Field(description="Machine-readable rules of this standard")

    @classmethod
    def from_detail(cls, detail: StandardDetail) -> StandardDetailResponse:
        """Build a detail response from the service's detail view."""
        source = detail.source
        return cls(
            standard_code=source.standard_code,
            title=source.title,
            title_en=source.title_en,
            ifrs_equivalent=source.ifrs_equivalent,
            phase=source.phase,
            slug=source.slug,
            source_authority=source.source_authority,
            source_url=source.source_url,
            revisions=[
                StandardRevisionResponse(
                    revision_id=revision.revision_id,
                    title=revision.title,
                    published_at=revision.published_at,
                    effective_from=revision.effective_from,
                    effective_to=revision.effective_to,
                    status=revision.status,
                    supersedes_revision_id=revision.supersedes_revision_id,
                    source_authority=revision.source_authority,
                    source_url=revision.source_url,
                    snapshot_uri=revision.snapshot_uri,
                    sha256=revision.sha256,
                    verification_status=revision.verification_status.value,
                    verified_by=revision.verified_by,
                    verified_at=revision.verified_at,
                    notes=revision.notes,
                )
                for revision in source.revisions
            ],
            rules=[
                StandardRuleResponse(
                    rule_id=rule.rule_id,
                    category=rule.category,
                    description=rule.description,
                    dsl_formula=rule.dsl_formula,
                    rule_type=rule.rule_type,
                    severity=rule.severity.value,
                    standard_refs=list(rule.standard_refs),
                    is_executable=rule.is_executable,
                    ruleset_version=rule.ruleset_version,
                    verification_status=rule.verification_status.value,
                )
                for rule in detail.rules
            ],
        )


class ValidationResultResponse(BaseModel):
    """One per-rule outcome of a validation run."""

    rule_id: str = Field(description="Stable rule identifier, e.g. 'BS001'")
    standard_code: str = Field(description="Standard number the rule belongs to")
    revision_id: str = Field(description="Revision id of the rule executed")
    outcome: str = Field(description="Rule outcome: 'pass', 'fail', 'not_applicable' or 'error'")
    severity: str = Field(description="Rule severity: 'error' or 'warning'")
    message: str = Field(description="Human-readable outcome message")
    detail: str | None = Field(description="Extra outcome detail (e.g. evaluation error text)")


class NotExecutedRuleResponse(BaseModel):
    """A rule that was implemented but not executed by the run."""

    rule_id: str = Field(description="Stable rule identifier, e.g. 'CF005'")
    standard_code: str = Field(description="Standard number the rule belongs to")
    revision_id: str = Field(description="Revision id of the rule")
    reason: str = Field(description="Why the rule was not executed")


class RunCoverageResponse(BaseModel):
    """Coverage snapshot of a validation run (spec §12.1)."""

    corpus_standards: int = Field(description="Standards present in the registry at run time")
    standards_with_rules: int = Field(description="Standards with machine-readable rules")
    rules_implemented: int = Field(description="Rules seeded in the registry")
    rules_verified: int = Field(description="Rules whose revision is VERIFIED_TEXT")
    rules_executed: int = Field(description="Rules this run actually evaluated")


class ValidationRunResponse(BaseModel):
    """A validation run with its results and coverage snapshot."""

    run_id: str = Field(description="Validation run id")
    company_id: str | None = Field(description="API company id as supplied, when given")
    period_start: date | None = Field(description="Start of the validated financial period")
    period_end: date | None = Field(description="End of the validated financial period")
    standard_codes: list[str] = Field(description="Resolved standard codes the run covered")
    ruleset_version: str = Field(description="Ruleset version executed")
    overall_outcome: str = Field(description="Run outcome: 'pass', 'fail', 'error' or 'not_applicable'")
    is_valid: bool = Field(description="False when any ERROR-severity rule failed or errored")
    rules_executed: int = Field(description="Rules actually evaluated (PASS/FAIL/ERROR outcomes)")
    rules_passed: int = Field(description="Rules with outcome PASS")
    rules_failed: int = Field(description="Rules with outcome FAIL")
    rules_not_applicable: int = Field(description="Rules with outcome NOT_APPLICABLE")
    rules_errored: int = Field(description="Rules with outcome ERROR")
    coverage: RunCoverageResponse = Field(description="Coverage snapshot (spec §12.1)")
    results: list[ValidationResultResponse] = Field(description="Per-rule outcomes")
    not_executed: list[NotExecutedRuleResponse] = Field(description="Rules implemented but not executed, with reasons")
    created_at: datetime = Field(description="When the run was created (UTC)")

    @classmethod
    def from_record(cls, record: ValidationRunRecord) -> ValidationRunResponse:
        """Build a run response from the domain run record."""
        assert record.created_at is not None, "persisted validation run has no created_at"
        return cls(
            run_id=record.run_id,
            company_id=record.company_id,
            period_start=record.period_start,
            period_end=record.period_end,
            standard_codes=list(record.standard_codes),
            ruleset_version=record.ruleset_version or "unknown",
            overall_outcome=record.overall_outcome.value,
            is_valid=record.is_valid,
            rules_executed=record.rules_executed,
            rules_passed=record.rules_passed,
            rules_failed=record.rules_failed,
            rules_not_applicable=record.rules_not_applicable,
            rules_errored=record.rules_errored,
            coverage=RunCoverageResponse(
                corpus_standards=record.coverage.corpus_standards,
                standards_with_rules=record.coverage.standards_with_rules,
                rules_implemented=record.coverage.rules_implemented,
                rules_verified=record.coverage.rules_verified,
                rules_executed=record.coverage.rules_executed,
            ),
            results=[
                ValidationResultResponse(
                    rule_id=result.rule_id,
                    standard_code=result.standard_code,
                    revision_id=result.revision_id,
                    outcome=result.outcome.value,
                    severity=result.severity.value,
                    message=result.message,
                    detail=result.detail or None,
                )
                for result in record.results
            ],
            not_executed=[
                NotExecutedRuleResponse(
                    rule_id=item.rule_id,
                    standard_code=item.standard_code,
                    revision_id=item.revision_id,
                    reason=item.reason,
                )
                for item in record.not_executed
            ],
            created_at=record.created_at,
        )


class CreateValidationRunRequest(BaseModel):
    """Request to run the standards validation over supplied financial data."""

    company_id: str | None = Field(
        default=None,
        description="Optional API company id the validated data belongs to (recorded on the run)",
    )
    period_start: date | None = Field(
        default=None,
        description="Start of the validated financial period (drives effective-date applicability)",
    )
    period_end: date | None = Field(default=None, description="End of the validated financial period")
    standard_codes: list[str] | None = Field(
        default=None,
        description="Standard codes to validate (e.g. ['1']); null means all standards in the registry",
    )
    data: dict[str, Any] = Field(
        description=(
            "Financial data as a document of per-statement namespaces "
            "(balance_sheet, income_statement, comprehensive_income, "
            "equity_changes, cash_flow) plus optional top-level fields. "
            "Each rule evaluates against its own statement's slice merged "
            "over the document; cross-check rules use dotted paths "
            "(income_statement.net_profit). Numbers are JSON numbers; "
            "nested objects serve dotted fields; lists of {'amount': ...} "
            "serve sum(). Missing fields make the rules that need them "
            "ERROR, never PASS."
        )
    )

    @model_validator(mode="after")
    def _check_period_range(self) -> CreateValidationRunRequest:
        if self.period_start is not None and self.period_end is not None and self.period_end < self.period_start:
            raise ValueError("period_end must be on or after period_start.")
        return self
