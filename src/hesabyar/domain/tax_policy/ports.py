"""Tax-policy repository port (US-402, spec §6.3).

Domain and application code depend only on this protocol. The
production implementation is the SQLAlchemy repository
(``hesabyar.infrastructure.persistence.postgres.tax_policy_repository``);
tests use an in-memory fake. The port speaks domain types only — no
ORM models, no sessions — so the application layer never imports
infrastructure.

The port is synchronous: there is no async infrastructure in the
repository yet. A later story may make it async when the MCP layer
needs it.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from typing import Protocol

from hesabyar.domain.standards.models import VerificationStatus
from hesabyar.domain.tax_policy.commands import RecordTaxDecision
from hesabyar.domain.tax_policy.models import (
    TaxDecision,
    TaxPolicyVersion,
    TaxPolicyVersionDetail,
    TaxSource,
    TaxSourceDetail,
)


class TaxPolicyRepositoryPort(Protocol):
    """Persistence boundary for the tax-policy registry."""

    def list_sources(self) -> Sequence[TaxSource]:
        """Return the registry's tax sources, ordered by source key."""
        ...

    def get_source(self, source_key: str) -> TaxSourceDetail | None:
        """Return one source with its versions, or None for an unknown key."""
        ...

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
        """Return policy versions, optionally filtered.

        ``effective_on`` keeps versions whose effective range covers
        that date (inclusive both ends; NULL ``effective_to`` is
        open-ended). Results are ordered by rule key, newest-effective
        first.
        """
        ...

    def get_version(self, version_row_id: str) -> TaxPolicyVersionDetail | None:
        """Return one version with its source, or None for an unknown row id."""
        ...

    def find_active_version(self, rule_key: str, on: datetime.date) -> TaxPolicyVersion | None:
        """Return the newest-effective active version covering ``on``, or None.

        A superseded version is never returned, and a date no version
        covers yields None — never a silent fallback to another year's
        parameter.
        """
        ...

    def save_decision(self, command: RecordTaxDecision) -> TaxDecision:
        """Persist a resolved decision and return it (with id + timestamp)."""
        ...

    def get_decision(self, decision_id: str, tenant_id: str) -> TaxDecision | None:
        """Return one decision, or None when it does not exist for this tenant.

        A decision of another tenant looks exactly like a decision
        that does not exist.
        """
        ...
