"""Tax decision use cases (US-402).

Resolves the active policy version for a given date + company + rule
and records the decision with its source version (spec §12.2).

The service depends only on the
:class:`hesabyar.domain.tax_policy.ports.TaxPolicyRepositoryPort` —
never on a session, never on infrastructure (spec §6.3).

Fail-closed rules (spec §12.2, docs/CURRENT_RULES_1405.md §3, §12):

- Resolution is effective-dated (inclusive both ends) and picks the
  newest-effective ``active`` version covering the decision date. A
  superseded version is never resolved, and a date no version covers
  (historical or future) raises :class:`TaxPolicyNotFoundError` —
  never a silent fallback to another year's parameter.
- Production use is gated: resolving for ``usage_context="production"``
  with a version that is not ``VERIFIED_TEXT`` raises
  :class:`TaxPolicySourceBlockedError`, naming the missing official
  source pin. No decision row is recorded for a blocked attempt — a
  decision is a successful resolution. Reference lookups resolve any
  active version but record its verification status, so callers can
  see the parameter is unverified.
- The decision snapshots the resolving version (row id, version id,
  parameter value/unit, verification status, source key) so it stays
  auditable even if the version row is later deleted or superseded.

Persistence: the repository flushes the new row so its id exists, but
no layer here commits — the caller (REST route) owns the transaction,
like every other service that writes through a request session.

Tax CALCULATION on invoices is out of scope for this story — the
registry + decisions only.
"""

from __future__ import annotations

import datetime

from hesabyar.application.errors import (
    TaxDecisionNotFoundError,
    TaxPolicyNotFoundError,
    TaxPolicySourceBlockedError,
)
from hesabyar.domain.standards.models import VerificationStatus
from hesabyar.domain.tax_policy.commands import RecordTaxDecision
from hesabyar.domain.tax_policy.models import (
    TaxDecision,
    TaxPolicyVersion,
    UsageContext,
)
from hesabyar.domain.tax_policy.ports import TaxPolicyRepositoryPort


class TaxDecisionService:
    """Resolve and record tax-policy decisions over the repository port."""

    def __init__(self, repository: TaxPolicyRepositoryPort) -> None:
        self._repository = repository

    def _check_production_gate(self, version: TaxPolicyVersion, rule_key: str) -> None:
        """Enforce the CURRENT_RULES_1405.md production gate.

        Only a VERIFIED_TEXT version may drive production use. Anything
        else — CORROBORATED_CURRENT, UNVERIFIED, DISCOVERY_ONLY,
        BLOCKED_SOURCE_GAP, TRANSITIONAL — is blocked with a typed
        error naming the missing official source pin.
        """
        if version.verification_status == VerificationStatus.VERIFIED_TEXT:
            return
        raise TaxPolicySourceBlockedError(
            f"Tax policy version {version.version_id!r} of rule {rule_key!r} is "
            f"{version.verification_status.value} and cannot be used for production: "
            "the official source snapshot has not been pinned. Pin the official "
            "annual tax/budget source to the legal-source registry, hash the "
            "snapshot and promote the version to VERIFIED before production "
            "activation (docs/CURRENT_RULES_1405.md §3, §12).",
            details={
                "rule_key": rule_key,
                "version_id": version.version_id,
                "verification_status": version.verification_status.value,
                "source_key": version.source_key,
            },
        )

    def resolve_decision(
        self,
        *,
        tenant_id: str,
        company_id: str | None = None,
        rule_key: str,
        decision_date: datetime.date,
        usage_context: UsageContext = UsageContext.REFERENCE,
    ) -> TaxDecision:
        """Resolve the active version for the date + rule and record the decision.

        ``usage_context`` is ``reference`` (default) or ``production``.
        A production resolution with a non-VERIFIED_TEXT version raises
        :class:`TaxPolicySourceBlockedError` and records nothing.
        """
        version = self._repository.find_active_version(rule_key, decision_date)
        if version is None:
            raise TaxPolicyNotFoundError(
                f"No active tax-policy version for rule {rule_key!r} covers {decision_date.isoformat()}.",
                details={"rule_key": rule_key, "decision_date": decision_date.isoformat()},
            )
        if usage_context == UsageContext.PRODUCTION:
            self._check_production_gate(version, rule_key)
        return self._repository.save_decision(
            RecordTaxDecision(
                tenant_id=tenant_id,
                company_id=company_id,
                rule_key=version.rule_key,
                decision_date=decision_date,
                usage_context=usage_context,
                policy_version_id=version.id,
                version_id=version.version_id,
                parameter_value=version.parameter_value,
                value_unit=version.value_unit,
                verification_status=version.verification_status,
                source_key=version.source_key,
            )
        )

    def get_decision(self, decision_id: str, tenant_id: str) -> TaxDecision:
        """Return one decision, or raise TaxDecisionNotFoundError.

        A decision of another tenant looks exactly like a decision
        that does not exist.
        """
        decision = self._repository.get_decision(decision_id, tenant_id)
        if decision is None:
            raise TaxDecisionNotFoundError(
                f"Tax decision {decision_id!r} does not exist.",
                details={"tax_decision_id": decision_id},
            )
        return decision
