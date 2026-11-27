"""Port (interface) for the standards validation bounded context.

Defines the abstract boundary that adapters must satisfy. Following the
codebase-design principle: one interface, deep implementation behind it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any

from hesabyar.domain.standards.models import (
    RegistryCoverage,
    StandardCode,
    StandardRule,
    StandardSource,
    ValidationReport,
    ValidationRunRecord,
)


class StandardsValidatorPort(ABC):
    """Abstract port for validating financial data against accounting standards.

    Implementations may load rules from YAML files, databases, or in-memory
    registries. The port's interface is intentionally small: callers provide
    financial data as a dict of field names to values, and receive a
    ValidationReport summarizing all rule outcomes.
    """

    @abstractmethod
    def validate(self, data: dict[str, Any]) -> ValidationReport:
        """Run all applicable rules against the supplied financial data.

        Args:
            data: Field name → numeric value mapping representing a financial
                  statement or subset thereof.

        Returns:
            A ValidationReport containing the outcome of every evaluated rule.
        """
        ...

    @abstractmethod
    def validate_rule(self, rule_id: str, data: dict[str, Any]) -> ValidationReport:
        """Run a single rule (identified by rule_id) against the supplied data.

        Args:
            rule_id: Stable rule identifier (e.g. 'BS001').
            data: Field name → numeric value mapping.

        Returns:
            A ValidationReport with exactly one RuleResult.
        """
        ...


class StandardsRegistryPort(ABC):
    """Abstract port for the persisted standards registry (spec §7.4/§12.1).

    The application layer owns the use-case logic (source gates,
    per-statement rule scoping, DSL evaluation, outcome aggregation);
    this port owns persistence. Implementations read/write the
    ``standard_sources`` / ``standard_revisions`` / ``standard_rules`` /
    ``validation_runs`` / ``validation_results`` tables. Rules returned
    carry their revision's effective range and verification status so the
    application can decide applicability without touching persistence.
    """

    @abstractmethod
    def list_sources(self) -> Sequence[StandardSource]:
        """All registered standards, ordered by standard_code numerically."""
        ...

    @abstractmethod
    def get_source(self, standard_code: StandardCode) -> StandardSource | None:
        """One standard with its revisions, or None when unknown."""
        ...

    @abstractmethod
    def get_rules(self, standard_codes: Sequence[StandardCode] | None = None) -> Sequence[StandardRule]:
        """Rules for the given standards (all standards when None).

        Rules are ordered by rule_id within each standard; standards are
        ordered by standard_code numerically.
        """
        ...

    @abstractmethod
    def coverage(self) -> RegistryCoverage:
        """Registry coverage, reported separately (spec §12.1)."""
        ...

    @abstractmethod
    def ruleset_version(self) -> str:
        """Ruleset version of the latest seeded revision (e.g. '1.0.0-legacy')."""
        ...

    @abstractmethod
    def save_validation_run(self, record: ValidationRunRecord) -> ValidationRunRecord:
        """Persist a run and its results; returns it with ``created_at`` set.

        The record's ``run_id`` is supplied by the caller; the
        implementation must not silently drop results or not-executed rows.
        """
        ...

    @abstractmethod
    def get_validation_run(self, run_id: str, tenant_id: str) -> ValidationRunRecord | None:
        """One run with its results for the owning tenant, or None."""
        ...
