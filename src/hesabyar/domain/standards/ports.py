"""Port (interface) for the standards validation bounded context.

Defines the abstract boundary that adapters must satisfy. Following the
codebase-design principle: one interface, deep implementation behind it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from hesabyar.domain.standards.models import ValidationReport


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
