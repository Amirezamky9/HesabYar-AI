"""Tax-policy registry read use cases (US-402).

REST, MCP and workers all call this service instead of querying the
registry tables directly, so tax-policy reads have one implementation
and one error contract (design-eng spec §6.2/§6.3, §12.2).

The service depends only on the
:class:`hesabyar.domain.tax_policy.ports.TaxPolicyRepositoryPort` —
never on a session, never on infrastructure (spec §6.3). It reads
the registry only; it never touches the engine and never resolves a
decision (that is the decision service, which owns the production
gate).
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence

from hesabyar.application.errors import (
    TaxPolicyNotFoundError,
    TaxSourceNotFoundError,
)
from hesabyar.domain.standards.models import VerificationStatus
from hesabyar.domain.tax_policy.models import (
    TaxPolicyVersion,
    TaxPolicyVersionDetail,
    TaxSource,
    TaxSourceDetail,
)
from hesabyar.domain.tax_policy.ports import TaxPolicyRepositoryPort


class TaxPolicyQueryService:
    """Read-only tax-policy registry queries over the repository port."""

    def __init__(self, repository: TaxPolicyRepositoryPort) -> None:
        self._repository = repository

    def list_sources(self) -> Sequence[TaxSource]:
        """Return the registry's tax sources, ordered by source key."""
        return self._repository.list_sources()

    def get_source(self, source_key: str) -> TaxSourceDetail:
        """Return one source with its versions.

        Raises :class:`TaxSourceNotFoundError` for an unknown key.
        """
        detail = self._repository.get_source(source_key)
        if detail is None:
            raise TaxSourceNotFoundError(
                f"Tax source {source_key!r} is not in the registry.",
                details={"source_key": source_key},
            )
        return detail

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
        return self._repository.list_versions(
            rule_key=rule_key,
            verification_status=verification_status,
            effective_on=effective_on,
            rule_year=rule_year,
            performance_year=performance_year,
            filing_year=filing_year,
        )

    def get_version(self, version_row_id: str) -> TaxPolicyVersionDetail:
        """Return one version with its source.

        Raises :class:`TaxPolicyNotFoundError` for an unknown row id.
        """
        detail = self._repository.get_version(version_row_id)
        if detail is None:
            raise TaxPolicyNotFoundError(
                f"Tax policy version {version_row_id!r} is not in the registry.",
                details={"version_id": version_row_id},
            )
        return detail
