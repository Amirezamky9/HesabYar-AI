"""Standards registry read use cases (US-401).

REST, MCP and workers all call this service instead of querying the
registry tables directly, so standards reads have one implementation
and one error contract (design-eng spec §6.2/§6.3, §12.1).

The service talks to the registry ONLY through the domain
``StandardsRegistryPort`` — it never imports infrastructure (spec
§6.3: application imports domain/application only). Coverage is
reported separately per spec §12.1 — corpus vs implemented vs
verified vs executable — so a 35-standard corpus is never presented
as 35 standards of compliance coverage.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from hesabyar.application.errors import StandardNotFoundError
from hesabyar.domain.standards.models import (
    RegistryCoverage,
    StandardCode,
    StandardRule,
    StandardSource,
    VerificationStatus,
)
from hesabyar.domain.standards.ports import StandardsRegistryPort


@dataclass(frozen=True)
class StandardDetail:
    """One standard with its revisions and its machine-readable rules."""

    source: StandardSource
    rules: tuple[StandardRule, ...]


class StandardsQueryService:
    """Read-only standards registry queries over the registry port."""

    def __init__(self, registry: StandardsRegistryPort) -> None:
        self._registry = registry

    def list_standards(
        self,
        *,
        verification_status: VerificationStatus | None = None,
        phase: int | None = None,
        has_rules: bool | None = None,
    ) -> Sequence[StandardSource]:
        """Return the registry's standards, optionally filtered.

        ``verification_status`` matches a standard when ANY of its
        revisions carries that label; ``has_rules`` keeps only standards
        with (or without) machine-readable rules.
        """
        sources = list(self._registry.list_sources())
        if phase is not None:
            sources = [source for source in sources if source.phase == phase]
        if verification_status is not None:
            sources = [
                source
                for source in sources
                if any(revision.verification_status == verification_status for revision in source.revisions)
            ]
        if has_rules is not None:
            sources = [source for source in sources if (source.rules_count > 0) is has_rules]
        return sources

    def get_standard(self, standard_code: str) -> StandardDetail:
        """Return one standard with its revisions and rules.

        Raises :class:`StandardNotFoundError` for an unknown code.
        """
        code = StandardCode(standard_code)
        source = self._registry.get_source(code)
        if source is None:
            raise StandardNotFoundError(
                f"Standard {standard_code!r} is not in the registry.",
                details={"standard_code": standard_code},
            )
        rules = tuple(self._registry.get_rules([code]))
        return StandardDetail(source=source, rules=rules)

    def coverage(self) -> RegistryCoverage:
        """Return the registry coverage counters (spec §12.1).

        Each counter is reported separately: corpus size is not
        compliance coverage.
        """
        return self._registry.coverage()
