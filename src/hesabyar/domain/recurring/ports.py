"""Port for recurring templates + occurrences (US-563, Phase 5-7 ۵خ W3).

The SQLAlchemy implementation flushes; the caller commits (the
router / worker / recorder owns the transaction). Template reads
are always tenant-scoped — a template of another tenant looks
exactly like a template that does not exist. The generator's
``list_active_templates`` is the one cross-tenant read: the
scheduled job runs across all installations, like the other
scheduler jobs.
"""

from __future__ import annotations

import datetime
from abc import ABC, abstractmethod
from collections.abc import Sequence

from hesabyar.domain.recurring.models import (
    RecurringOccurrence,
    RecurringTemplate,
    TemplateKind,
    TemplateStatus,
)


class RecurringRepositoryPort(ABC):
    """Recurring templates + occurrences over the HesabYar database."""

    @abstractmethod
    def save_template(self, template: RecurringTemplate) -> RecurringTemplate:
        """Insert or update a template by id (flushes; the caller commits)."""

    @abstractmethod
    def get_template(
        self, tenant_id: str, template_id: str
    ) -> RecurringTemplate | None:
        """One template of this tenant by id, or None."""

    @abstractmethod
    def list_templates(
        self,
        tenant_id: str,
        *,
        company_id: str | None = None,
        kind: TemplateKind | None = None,
        status: TemplateStatus | None = None,
    ) -> Sequence[RecurringTemplate]:
        """A tenant's templates, newest first, optionally filtered."""

    @abstractmethod
    def list_active_templates(self) -> Sequence[RecurringTemplate]:
        """Every ACTIVE template across all tenants (the generator's scope)."""

    @abstractmethod
    def delete_template(self, tenant_id: str, template_id: str) -> bool:
        """Delete a template with no occurrences; False when it does not exist."""

    @abstractmethod
    def count_occurrences(self, template_id: str) -> int:
        """How many occurrence rows a template has (any status)."""

    @abstractmethod
    def save_occurrence(self, occurrence: RecurringOccurrence) -> RecurringOccurrence:
        """Insert or update an occurrence by id (flushes; the caller commits).

        A (template, scheduled date) collision raises
        :class:`RecurringOccurrenceConflictError` — the idempotency
        backstop for two generator runs racing on the same due date.
        """

    @abstractmethod
    def get_occurrence_by_date(
        self, template_id: str, scheduled_date: datetime.date
    ) -> RecurringOccurrence | None:
        """A template's occurrence for one scheduled date, or None."""

    @abstractmethod
    def list_occurrences(
        self, template_id: str
    ) -> Sequence[RecurringOccurrence]:
        """A template's occurrences, in occurrence-number order."""
