"""Standards validation use cases (US-401).

Runs the existing closed DSL engine (``domain/standards``) over
supplied financial data, persists the run + per-rule results through
the registry port, and reports coverage separately (spec §12.1).

The service talks to the registry ONLY through the domain
``StandardsRegistryPort`` — it never imports infrastructure (spec
§6.3: application imports domain/application only). Persistence is
the port's job; this module owns the use-case logic: source gates,
per-statement rule scoping, DSL evaluation and outcome aggregation.

Fail-closed rules (spec §12.1, ARCHITECTURE.md §9.4):

- Outcomes are exactly PASS / FAIL / NOT_APPLICABLE / ERROR. Missing
  data or an unevaluable rule yields ERROR, never PASS — the DSL
  enforces this and the service preserves it.
- A rule whose standard revision is not applicable to the run's period
  (effective dates) yields NOT_APPLICABLE, not a silent skip.
- A rule with no DSL formula (e.g. the CF005 disclosure requirement)
  is NOT executed: it is listed in the run's ``not_executed`` with its
  reason, never given a fabricated outcome.
- A BLOCKED_SOURCE_GAP revision (Standard 15 for periods beginning
  1405/01/01+) blocks the whole run with
  :class:`StandardSourceBlockedError` — including when no period is
  given, because applicability then cannot be established.

JSON numbers are normalized at this boundary before they reach the
DSL: the DSL forbids binary floats, so integral floats become ints and
other floats become exact Decimals via ``str()``. The input hash is
taken over the normalized data, so a run is reproducible from its
record.

Persistence: the service hands the finished record to the registry
port, which flushes the run + results so ids exist but does NOT
commit — the caller (REST route) owns the transaction, like every
other service that writes through a request session.

Rule evaluation context (statement scoping):

- Financial data is a document of per-statement namespaces
  (``balance_sheet``, ``income_statement``, ``comprehensive_income``,
  ``equity_changes``, ``cash_flow``) plus optional top-level fields.
- A rule evaluates against its own category's statement slice merged
  over the document (the slice wins for flat field names), so
  ``owners_of_parent`` in an income-statement rule reads the income
  statement's value while the same name in a comprehensive-income rule
  reads that statement's value. Cross-check rules evaluate against
  the whole document and use dotted paths
  (``income_statement.net_profit``).
- This matches the legacy validator's ``validate_all`` (per-category
  contexts + a cross-check context) and is the only reading under
  which the 36 ported rules are mutually consistent.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from hesabyar.application.errors import (
    StandardNotFoundError,
    StandardSourceBlockedError,
    ValidationRunNotFoundError,
)
from hesabyar.domain.standards.dsl import evaluate_rule
from hesabyar.domain.standards.models import (
    NotExecutedRuleRecord,
    RuleOutcome,
    RuleSeverity,
    RunCoverage,
    StandardCode,
    StandardSource,
    ValidationResultRecord,
    ValidationRunRecord,
    VerificationStatus,
)
from hesabyar.domain.standards.ports import StandardsRegistryPort

#: Reason recorded for rules that are implemented but not executable.
NOT_EXECUTABLE_REASON = "Rule has no DSL formula (disclosure requirement); the closed DSL engine cannot evaluate it."

#: Statement namespace a rule category evaluates against (None means
#: the whole document — cross-checks use dotted paths). Mirrors the
#: legacy validator's per-category contexts (scripts/validator.py).
CATEGORY_NAMESPACE: dict[str, str | None] = {
    "balance_sheet": "balance_sheet",
    "income_statement": "income_statement",
    "comprehensive_income": "comprehensive_income",
    "equity_changes": "equity_changes",
    "cash_flow": "cash_flow",
    "cross_checks": None,
}


def _rule_context(data: Mapping[str, Any], category: str) -> dict[str, Any]:
    """Build the evaluation context for one rule category.

    The category's statement slice is merged over the document (the
    slice wins for flat field names); the nested namespaces stay
    available for dotted access. A missing or non-mapping slice leaves
    the document unchanged.
    """
    namespace = CATEGORY_NAMESPACE.get(category)
    if namespace is None:
        return dict(data)
    section = data.get(namespace)
    if not isinstance(section, Mapping):
        return dict(data)
    return {**data, **section}


def _normalize_value(value: Any) -> Any:
    """Recursively normalize supplied JSON data for the DSL.

    The DSL accepts only int / Decimal (and mappings / lists of them):
    integral floats become ints, other floats become exact Decimals
    via ``str()`` — never binary-float arithmetic. Booleans, strings
    and None pass through unchanged; a rule that arithmetically uses
    one of them fails closed with ERROR inside the DSL.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        return Decimal(str(value))
    if isinstance(value, Decimal):
        return value
    if isinstance(value, Mapping):
        return {str(key): _normalize_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_value(item) for item in value]
    return value


def _input_hash(normalized_data: Mapping[str, Any]) -> str:
    """SHA-256 of the canonical JSON of the normalized input data."""
    canonical = json.dumps(normalized_data, sort_keys=True, default=str, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _overall_outcome(passed: int, failed: int, errored: int) -> RuleOutcome:
    """Derive the run outcome from the counts (ERROR dominates)."""
    if errored > 0:
        return RuleOutcome.ERROR
    if failed > 0:
        return RuleOutcome.FAIL
    if passed > 0:
        return RuleOutcome.PASS
    return RuleOutcome.NOT_APPLICABLE


class StandardsValidationService:
    """Create and read standards validation runs over the registry port."""

    def __init__(self, registry: StandardsRegistryPort) -> None:
        self._registry = registry

    def _resolve_codes(self, standard_codes: Sequence[str] | None) -> list[StandardSource]:
        """Resolve the requested standard codes to registry sources.

        ``None`` means every standard in the registry. An unknown code
        raises :class:`StandardNotFoundError`.
        """
        sources = list(self._registry.list_sources())
        if standard_codes is None:
            return sources
        by_code = {source.standard_code: source for source in sources}
        resolved: list[StandardSource] = []
        for code in dict.fromkeys(standard_codes):
            source = by_code.get(StandardCode(code))
            if source is None:
                raise StandardNotFoundError(
                    f"Standard {code!r} is not in the registry.",
                    details={"standard_code": code},
                )
            resolved.append(source)
        resolved.sort(key=lambda source: int(source.standard_code))
        return resolved

    def _check_source_gate(
        self,
        sources: Sequence[StandardSource],
        period_start: date | None,
    ) -> None:
        """Enforce the BLOCKED_SOURCE_GAP gate (CURRENT_RULES_1405.md §2).

        A standard whose latest revision is BLOCKED_SOURCE_GAP blocks
        the run when the period is unknown (fail closed) or begins after
        the revision's effective_to. Historical periods the bundled
        revision still covers are allowed. The first blocked day is the
        day after the revision's effective_to.
        """
        for source in sources:
            latest = source.latest_revision
            if latest is None:
                continue
            if latest.verification_status != VerificationStatus.BLOCKED_SOURCE_GAP:
                continue
            blocked = (
                period_start is None
                or (latest.effective_to is not None and period_start > latest.effective_to)
                or latest.effective_to is None
            )
            if not blocked:
                continue
            blocked_from = latest.effective_to + timedelta(days=1) if latest.effective_to is not None else None
            blocked_from_text = blocked_from.isoformat() if blocked_from is not None else "an unknown date"
            raise StandardSourceBlockedError(
                f"Standard {source.standard_code} is BLOCKED_SOURCE_GAP: "
                "the bundled text is the pre-1404 revision and must never "
                f"validate for periods beginning {blocked_from_text} or later "
                "(or when no period is given) until the official revised-1404 "
                "text is imported and VERIFIED.",
                details={
                    "standard_code": source.standard_code,
                    "revision_id": latest.revision_id,
                    "blocked_from": blocked_from_text if blocked_from is not None else None,
                    "verification_status": latest.verification_status.value,
                },
            )

    def create_run(
        self,
        *,
        tenant_id: str,
        company_id: str | None = None,
        period_start: date | None = None,
        period_end: date | None = None,
        standard_codes: Sequence[str] | None = None,
        data: Mapping[str, Any],
    ) -> ValidationRunRecord:
        """Run the DSL engine over ``data`` and persist the run + results.

        The run covers the requested standards (default: all). Rules of
        a standard whose revision is not applicable to ``period_start``
        yield NOT_APPLICABLE; non-executable rules are listed as not
        executed. The port flushes the new rows; the caller commits.
        """
        if period_start is not None and period_end is not None and period_end < period_start:
            raise ValueError("period_end must be on or after period_start.")
        sources = self._resolve_codes(standard_codes)
        self._check_source_gate(sources, period_start)
        normalized = _normalize_value(data)
        if not isinstance(normalized, dict):
            raise ValueError("Financial data must be a JSON object.")
        coverage = self._registry.coverage()
        ruleset_version = self._registry.ruleset_version()

        # Load the covered standards' rules (port order: standard code,
        # then rule id). Non-executable rules are never evaluated.
        codes = [StandardCode(source.standard_code) for source in sources]
        rules = list(self._registry.get_rules(codes))
        not_executed: list[NotExecutedRuleRecord] = []
        executable = [rule for rule in rules if rule.is_executable]
        for rule in rules:
            if rule.is_executable:
                continue
            not_executed.append(
                NotExecutedRuleRecord(
                    rule_id=rule.rule_id,
                    standard_code=rule.standard_code,
                    revision_id=rule.revision_id,
                    reason=NOT_EXECUTABLE_REASON,
                )
            )

        results: list[ValidationResultRecord] = []
        for rule in executable:
            assert rule.dsl_formula is not None  # executable rules have one
            if period_start is not None and (
                (rule.effective_from is not None and period_start < rule.effective_from)
                or (rule.effective_to is not None and period_start > rule.effective_to)
            ):
                results.append(
                    ValidationResultRecord(
                        rule_id=rule.rule_id,
                        standard_code=rule.standard_code,
                        revision_id=rule.revision_id,
                        outcome=RuleOutcome.NOT_APPLICABLE,
                        severity=rule.severity,
                        message=(
                            f"Standard {rule.standard_code} revision {rule.revision_id} "
                            "does not apply to a period beginning "
                            f"{period_start.isoformat()}."
                        ),
                    )
                )
                continue
            evaluated = evaluate_rule(
                rule.rule_id,
                rule.dsl_formula,
                _rule_context(normalized, rule.category),
                severity=rule.severity,
            )
            results.append(
                ValidationResultRecord(
                    rule_id=evaluated.rule_id,
                    standard_code=rule.standard_code,
                    revision_id=rule.revision_id,
                    outcome=evaluated.outcome,
                    severity=evaluated.severity,
                    message=evaluated.message,
                    detail=evaluated.detail or "",
                    dsl_formula=rule.dsl_formula,
                )
            )

        passed = sum(1 for r in results if r.outcome == RuleOutcome.PASS)
        failed = sum(1 for r in results if r.outcome == RuleOutcome.FAIL)
        not_applicable = sum(1 for r in results if r.outcome == RuleOutcome.NOT_APPLICABLE)
        errored = sum(1 for r in results if r.outcome == RuleOutcome.ERROR)
        executed = passed + failed + errored
        # A report is valid iff no ERROR-severity rule failed or errored.
        is_valid = not any(
            r.severity == RuleSeverity.ERROR and r.outcome in (RuleOutcome.FAIL, RuleOutcome.ERROR) for r in results
        )
        resolved_codes = tuple(source.standard_code for source in sources)

        record = ValidationRunRecord(
            run_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            company_id=company_id,
            period_start=period_start,
            period_end=period_end,
            standard_codes=resolved_codes,
            ruleset_version=ruleset_version,
            input_hash=_input_hash(normalized),
            overall_outcome=_overall_outcome(passed, failed, errored),
            is_valid=is_valid,
            rules_executed=executed,
            rules_passed=passed,
            rules_failed=failed,
            rules_not_applicable=not_applicable,
            rules_errored=errored,
            coverage=RunCoverage(
                corpus_standards=coverage.corpus_standards,
                standards_with_rules=coverage.standards_with_rules,
                rules_implemented=coverage.rules_implemented,
                rules_verified=coverage.rules_verified,
                rules_executed=executed,
            ),
            results=tuple(results),
            not_executed=tuple(not_executed),
        )
        return self._registry.save_validation_run(record)

    def get_run(self, run_id: str, tenant_id: str) -> ValidationRunRecord:
        """Return one run with its results, or raise ValidationRunNotFoundError.

        A run of another tenant looks exactly like a run that does not exist.
        """
        record = self._registry.get_validation_run(run_id, tenant_id)
        if record is None:
            raise ValidationRunNotFoundError(
                f"Validation run {run_id!r} does not exist.",
                details={"validation_run_id": run_id},
            )
        return record
