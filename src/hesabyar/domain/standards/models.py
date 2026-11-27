"""Domain models for the accounting standards validation bounded context.

Follows ARCHITECTURE.md §9: each rule carries severity, standard references,
and a closed result set (PASS | FAIL | NOT_APPLICABLE | ERROR).  An unknown
field or unevaluable formula MUST yield ERROR, never PASS.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from enum import StrEnum, unique
from typing import NewType

RuleId = NewType("RuleId", str)
"""Stable, human-readable rule identifier (e.g. 'BS001')."""

StandardCode = NewType("StandardCode", str)
"""Accounting standard number or code (e.g. 'IAS1', 'IACPA-01')."""


@unique
class VerificationStatus(StrEnum):
    """Verification label of a standard revision / rule (docs/CURRENT_RULES_1405.md §1).

    - VERIFIED_TEXT: supported by the text already bundled in this
      repository or a consolidated legal text with amendment history.
    - CORROBORATED_CURRENT: supported by current authority
      circular/reporting, but the official source snapshot still needs
      pinning before production activation.
    - DISCOVERY_ONLY: useful signal for locating the official document;
      never executable.
    - BLOCKED_SOURCE_GAP: the repository source is materially out of
      date or missing; production/current-period use is prohibited until
      the correct official revision is imported and verified.
    - TRANSITIONAL: enacted/current rule whose operational effect depends
      on rollout, system readiness, later notices or effective dates.
    - UNVERIFIED: HesabYar extension of the document's vocabulary —
      bundled text is present, but no official source snapshot has been
      pinned and verified yet, so the revision must not be presented as
      verified. It is the honest default for bundled revisions the
      1405 baseline does not label.
    """

    VERIFIED_TEXT = "VERIFIED_TEXT"
    CORROBORATED_CURRENT = "CORROBORATED_CURRENT"
    DISCOVERY_ONLY = "DISCOVERY_ONLY"
    BLOCKED_SOURCE_GAP = "BLOCKED_SOURCE_GAP"
    TRANSITIONAL = "TRANSITIONAL"
    UNVERIFIED = "UNVERIFIED"


@unique
class RuleSeverity(StrEnum):
    """How a rule failure affects the overall validation report."""

    ERROR = "error"
    WARNING = "warning"


@unique
class RuleOutcome(StrEnum):
    """Result of evaluating a single rule against financial data.

    - PASS:           the rule's assertion held.
    - FAIL:           the rule's assertion did not hold.
    - NOT_APPLICABLE: the rule does not apply to the given data context.
    - ERROR:          the rule could not be evaluated (missing field, type error, etc.).
                      For mandatory rules, ERROR invalidates the entire report.
    """

    PASS = "pass"
    FAIL = "fail"
    NOT_APPLICABLE = "not_applicable"
    ERROR = "error"


@dataclass(frozen=True)
class StandardRevision:
    """A specific revision of an accounting standard.

    Carries the full provenance of spec §12.1: every field below is
    persisted on ``standard_revisions`` so a validation can always be
    traced to the exact source text, its hash and its verification
    state. ``verification_status`` uses :class:`VerificationStatus`;
    ``status`` is the registry lifecycle state (``active`` / ``superseded``).
    """

    standard_code: StandardCode
    title: str
    revision_id: str
    published_at: datetime.date | None = None
    effective_from: datetime.date | None = None
    effective_to: datetime.date | None = None
    status: str = "active"
    supersedes_revision_id: str | None = None
    source_authority: str | None = None
    source_url: str | None = None
    snapshot_uri: str | None = None
    sha256: str | None = None
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED
    verified_by: str | None = None
    verified_at: datetime.datetime | None = None
    notes: str | None = None


@dataclass(frozen=True)
class StandardSource:
    """One accounting standard in the registry with its revisions.

    ``standard_code`` is the plain standard number as a string
    (``"1"``, ``"43"``); the registry holds the 35 standards of
    ``metadata.json``. ``revisions`` are ordered newest-effective first.
    """

    standard_code: StandardCode
    title: str
    title_en: str | None
    ifrs_equivalent: str | None
    phase: int | None
    slug: str
    source_authority: str | None
    source_url: str | None
    revisions: tuple[StandardRevision, ...] = ()
    rules_count: int = 0

    @property
    def latest_revision(self) -> StandardRevision | None:
        """The revision with the greatest effective_from (NULL counts as oldest)."""
        if not self.revisions:
            return None
        return max(
            self.revisions,
            key=lambda r: (r.effective_from is not None, r.effective_from or datetime.date.min),
        )


@dataclass(frozen=True)
class StandardRule:
    """One machine-readable validation rule of a standard revision.

    ``dsl_formula`` is a boolean expression in the closed typed DSL
    (``domain/standards/dsl.py``) — never arbitrary code. Rules with no
    formula (e.g. a disclosure requirement such as CF005) have
    ``is_executable`` False: they are implemented in the registry but the
    DSL engine cannot evaluate them, so a validation run records them as
    not executed instead of inventing a PASS/FAIL for them.
    """

    rule_id: RuleId
    standard_code: StandardCode
    revision_id: str
    category: str
    description: str
    dsl_formula: str | None
    rule_type: str
    severity: RuleSeverity
    standard_refs: tuple[str, ...] = ()
    is_executable: bool = True
    ruleset_version: str | None = None
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED
    # Effective range of the rule's revision, denormalized so a rule can be
    # checked for period applicability without joining its revision.
    effective_from: datetime.date | None = None
    effective_to: datetime.date | None = None


@dataclass(frozen=True)
class RegistryCoverage:
    """Coverage of the standards registry, reported separately (spec §12.1).

    Having a corpus is NOT compliance coverage: ``corpus_standards``
    counts standards present in the registry, ``standards_with_rules``
    those with machine-readable rules, ``rules_implemented`` the rules
    seeded, ``rules_verified`` the rules whose revision is
    VERIFIED_TEXT, and ``rules_executable`` the rules the DSL engine
    can evaluate. A validation run reports ``rules_executed`` separately
    (how many it actually evaluated) — see the run record.
    """

    corpus_standards: int
    standards_with_rules: int
    rules_implemented: int
    rules_verified: int
    rules_executable: int


@dataclass(frozen=True)
class ValidationResultRecord:
    """One persisted per-rule result of a validation run.

    Mirrors the ``validation_results`` row: the rule's denormalized
    identity (rule_code / standard_code / revision_id) is stored alongside
    the outcome so a run stays readable even if the rule is later
    superseded. ``dsl_formula`` is the formula that was executed (None
    for NOT_APPLICABLE rows, which were never evaluated).
    """

    rule_id: RuleId
    standard_code: StandardCode
    revision_id: str
    outcome: RuleOutcome
    severity: RuleSeverity
    message: str = ""
    detail: str = ""
    dsl_formula: str | None = None


@dataclass(frozen=True)
class NotExecutedRuleRecord:
    """A rule that was part of a run but never evaluated.

    Only non-executable rules land here (e.g. CF005, a disclosure
    requirement with no DSL formula). The reason is recorded so the run
    never implies a fabricated outcome for them.
    """

    rule_id: RuleId
    standard_code: StandardCode
    revision_id: str
    reason: str


@dataclass(frozen=True)
class RunCoverage:
    """Coverage snapshot stored on a validation run (spec §12.1).

    The registry counts are copied from ``RegistryCoverage`` at run
    time; ``rules_executed`` is how many rules this run actually
    evaluated (PASS+FAIL+ERROR — NOT_APPLICABLE rows are recorded but
    not counted as executed).
    """

    corpus_standards: int
    standards_with_rules: int
    rules_implemented: int
    rules_verified: int
    rules_executed: int


@dataclass(frozen=True)
class ValidationRunRecord:
    """A persisted validation run (``validation_runs`` row + results).

    ``created_at`` is None until the registry port has stored the run.
    """

    run_id: str
    tenant_id: str
    company_id: str | None
    period_start: datetime.date | None
    period_end: datetime.date | None
    standard_codes: tuple[str, ...]
    ruleset_version: str | None
    input_hash: str
    overall_outcome: RuleOutcome
    is_valid: bool
    rules_executed: int
    rules_passed: int
    rules_failed: int
    rules_not_applicable: int
    rules_errored: int
    coverage: RunCoverage
    results: tuple[ValidationResultRecord, ...] = ()
    not_executed: tuple[NotExecutedRuleRecord, ...] = ()
    created_at: datetime.datetime | None = None


@dataclass(frozen=True)
class RuleResult:
    """The outcome of evaluating a single validation rule."""

    rule_id: RuleId
    outcome: RuleOutcome
    severity: RuleSeverity
    message: str = ""
    detail: str = ""

    @property
    def is_pass(self) -> bool:
        return self.outcome == RuleOutcome.PASS

    @property
    def is_failure(self) -> bool:
        return self.outcome in (RuleOutcome.FAIL, RuleOutcome.ERROR)


@dataclass
class ValidationReport:
    """Aggregated result of running all applicable rules against a dataset.

    Coverage counters follow ARCHITECTURE.md §9.5 — each category is reported
    separately so no misleading "N standards validated" claims can be made.
    """

    results: list[RuleResult] = field(default_factory=list)

    def add(self, result: RuleResult) -> None:
        self.results.append(result)

    @property
    def is_valid(self) -> bool:
        """A report is valid iff no ERROR-severity rule failed or errored."""
        return not any(
            r.severity == RuleSeverity.ERROR and r.outcome in (RuleOutcome.FAIL, RuleOutcome.ERROR)
            for r in self.results
        )

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.outcome == RuleOutcome.PASS)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.outcome == RuleOutcome.FAIL)

    @property
    def errors(self) -> int:
        return sum(1 for r in self.results if r.outcome == RuleOutcome.ERROR)

    @property
    def not_applicable(self) -> int:
        return sum(1 for r in self.results if r.outcome == RuleOutcome.NOT_APPLICABLE)

    @property
    def total(self) -> int:
        return len(self.results)
