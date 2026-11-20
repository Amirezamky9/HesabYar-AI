"""Response schemas for the tax-policy registry + decision endpoints (US-402).

Responses map from the domain views — no ORM object ever leaks. Every
version response carries the full §12.2 provenance: rule year,
performance year, filing year and effective dates as separate fields,
the exact parameter value with its unit, and the verification label
so a caller can always see whether a parameter is production-ready.

``parameter_value`` follows the §9.3 contract: a Decimal that
serializes as a decimal string (never a binary float — see the
invoice schemas). Trailing zeros are stripped at the mapping
boundary, so the wire form is ``"720000000000"``, not
``"720000000000.0000"``.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from hesabyar.domain.tax_policy.models import (
    TaxDecision,
    TaxPolicyVersion,
    TaxPolicyVersionDetail,
    TaxSource,
    TaxSourceDetail,
)


def _clean_decimal(value: Decimal) -> Decimal:
    """Strip trailing zeros so the §9.3 decimal-string form is clean.

    ``Decimal("720000000000.0000")`` becomes ``Decimal("720000000000")``
    (wire form ``"720000000000"``); ``Decimal("9.5000")`` becomes
    ``Decimal("9.5")``. ``format(..., "f")`` avoids the scientific
    notation ``normalize()`` alone would produce for large values
    (``7.2E+11``).
    """
    return Decimal(format(value.normalize(), "f"))


class TaxPolicyVersionResponse(BaseModel):
    """One versioned tax rule with its annual parameter."""

    id: str = Field(description="Registry row id of this version")
    source_key: str = Field(description="Source key of the version's primary source")
    rule_key: str = Field(description="Stable rule identifier, e.g. 'vat_general_rate'")
    rule_name: str = Field(description="Persian name of the rule")
    version_id: str = Field(description="Version identifier of the rule, e.g. '1405'")
    parameter_value: Decimal = Field(
        description=(
            "Annual parameter value (percent figures as written: 10 means 10%; "
            "serializes as a decimal string, spec §9.3)"
        )
    )
    value_unit: str = Field(description="Unit of parameter_value: 'percent' or 'irr'")
    rule_year: int = Field(description="Jalali year of the rule")
    performance_year: int | None = Field(description="Jalali performance year the rule governs")
    filing_year: int | None = Field(description="Jalali year the rule is filed/administered in")
    effective_from: date | None = Field(description="First date the version applies to (inclusive)")
    effective_to: date | None = Field(description="Last date the version applies to (inclusive); null is open-ended")
    status: str = Field(description="Registry lifecycle state: 'active' or 'superseded'")
    verification_status: str = Field(description="Verification label (CURRENT_RULES_1405.md §1 + UNVERIFIED)")
    verified_by: str | None = Field(description="Who/what verified this version")
    verified_at: datetime | None = Field(description="When the verification was recorded (UTC)")
    source_authority: str | None = Field(description="Issuing authority for this version")
    source_url: str | None = Field(description="Source document URL for this version")
    snapshot_uri: str | None = Field(description="Repo-relative path of the snapshotted text")
    sha256: str | None = Field(description="SHA-256 hex of the snapshotted source text bytes")
    corroboration_urls: list[str] = Field(description="Corroborating source URLs for this version")
    notes: str | None = Field(description="Scope notes, e.g. the Article 100 performance-1404 distinction")

    @classmethod
    def from_version(cls, version: TaxPolicyVersion) -> TaxPolicyVersionResponse:
        """Build a version response from the domain view."""
        return cls(
            id=version.id,
            source_key=version.source_key,
            rule_key=version.rule_key,
            rule_name=version.rule_name,
            version_id=version.version_id,
            parameter_value=_clean_decimal(version.parameter_value),
            value_unit=version.value_unit.value,
            rule_year=version.rule_year,
            performance_year=version.performance_year,
            filing_year=version.filing_year,
            effective_from=version.effective_from,
            effective_to=version.effective_to,
            status=version.status,
            verification_status=version.verification_status.value,
            verified_by=version.verified_by,
            verified_at=version.verified_at,
            source_authority=version.source_authority,
            source_url=version.source_url,
            snapshot_uri=version.snapshot_uri,
            sha256=version.sha256,
            corroboration_urls=list(version.corroboration_urls),
            notes=version.notes,
        )


class TaxSourceResponse(BaseModel):
    """One tax source in the registry list, with its version count."""

    id: str = Field(description="Registry row id of this source")
    source_key: str = Field(description="Stable source slug, e.g. 'tasnim-budget-1404-11-25'")
    title: str = Field(description="Title of the source document")
    authority: str | None = Field(description="Issuing/reporting authority")
    source_url: str | None = Field(description="Canonical source document URL")
    snapshot_uri: str | None = Field(description="Repo-relative path of the snapshotted text")
    sha256: str | None = Field(description="SHA-256 hex of the snapshotted source bytes")
    notes: str | None = Field(description="Notes, e.g. that the source is corroboration only")
    versions_count: int = Field(description="Number of policy versions citing this source")

    @classmethod
    def from_source(cls, source: TaxSource) -> TaxSourceResponse:
        """Build a list item from the domain view."""
        return cls(
            id=source.id,
            source_key=source.source_key,
            title=source.title,
            authority=source.authority,
            source_url=source.source_url,
            snapshot_uri=source.snapshot_uri,
            sha256=source.sha256,
            notes=source.notes,
            versions_count=source.versions_count,
        )


class TaxSourceDetailResponse(BaseModel):
    """One tax source with its policy versions."""

    id: str = Field(description="Registry row id of this source")
    source_key: str = Field(description="Stable source slug")
    title: str = Field(description="Title of the source document")
    authority: str | None = Field(description="Issuing/reporting authority")
    source_url: str | None = Field(description="Canonical source document URL")
    snapshot_uri: str | None = Field(description="Repo-relative path of the snapshotted text")
    sha256: str | None = Field(description="SHA-256 hex of the snapshotted source bytes")
    notes: str | None = Field(description="Notes, e.g. that the source is corroboration only")
    versions: list[TaxPolicyVersionResponse] = Field(description="Policy versions citing this source")

    @classmethod
    def from_detail(cls, detail: TaxSourceDetail) -> TaxSourceDetailResponse:
        """Build a detail response from the service's detail view."""
        source = detail.source
        return cls(
            id=source.id,
            source_key=source.source_key,
            title=source.title,
            authority=source.authority,
            source_url=source.source_url,
            snapshot_uri=source.snapshot_uri,
            sha256=source.sha256,
            notes=source.notes,
            versions=[TaxPolicyVersionResponse.from_version(version) for version in detail.versions],
        )


class TaxPolicyVersionDetailResponse(BaseModel):
    """One policy version with its source."""

    version: TaxPolicyVersionResponse = Field(description="The policy version")
    source: TaxSourceResponse = Field(description="The version's primary source")

    @classmethod
    def from_detail(cls, detail: TaxPolicyVersionDetail) -> TaxPolicyVersionDetailResponse:
        """Build a detail response from the service's detail view."""
        return cls(
            version=TaxPolicyVersionResponse.from_version(detail.version),
            source=TaxSourceResponse.from_source(detail.source),
        )


class TaxDecisionResponse(BaseModel):
    """A recorded tax-policy resolution for a date + company + rule."""

    id: str = Field(description="Tax decision id")
    company_id: str | None = Field(description="API company id as supplied, when given")
    rule_key: str = Field(description="Stable rule identifier that was resolved")
    decision_date: date = Field(description="Date the policy was resolved for")
    usage_context: str = Field(description="Why the decision was resolved: 'reference' or 'production'")
    policy_version_id: str | None = Field(description="Registry row id of the resolving version (null if deleted)")
    version_id: str = Field(description="Version id of the resolving version")
    parameter_value: Decimal = Field(
        description="Parameter value of the resolving version (serializes as a decimal string, spec §9.3)"
    )
    value_unit: str = Field(description="Unit of parameter_value: 'percent' or 'irr'")
    verification_status: str = Field(description="Verification label of the resolving version at decision time")
    source_key: str = Field(description="Source key of the resolving version")
    created_at: datetime = Field(description="When the decision was recorded (UTC)")

    @classmethod
    def from_decision(cls, decision: TaxDecision) -> TaxDecisionResponse:
        """Build a decision response from the domain view."""
        return cls(
            id=decision.id,
            company_id=decision.company_id,
            rule_key=decision.rule_key,
            decision_date=decision.decision_date,
            usage_context=decision.usage_context.value,
            policy_version_id=decision.policy_version_id,
            version_id=decision.version_id,
            parameter_value=_clean_decimal(decision.parameter_value),
            value_unit=decision.value_unit.value,
            verification_status=decision.verification_status.value,
            source_key=decision.source_key,
            created_at=decision.created_at,
        )


class ResolveTaxDecisionRequest(BaseModel):
    """Request to resolve a tax policy for a date + company + rule."""

    rule_key: str = Field(description="Stable rule identifier, e.g. 'vat_general_rate'")
    decision_date: date = Field(description="Date to resolve the policy for (effective dates are inclusive)")
    company_id: str | None = Field(
        default=None,
        description="Optional API company id the decision belongs to (recorded on the decision)",
    )
    usage_context: Literal["reference", "production"] = Field(
        default="reference",
        description=(
            "'reference' resolves any active version (the decision records its "
            "verification status); 'production' resolves only a VERIFIED_TEXT "
            "version and is blocked (422 TAX_POLICY_SOURCE_BLOCKED) otherwise."
        ),
    )
