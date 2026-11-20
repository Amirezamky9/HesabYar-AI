"""Ports (interfaces) for the review-queue bounded context.

The application layer talks to persistence ONLY through these ports —
it never imports infrastructure (spec §6.3; the Phase 4 architecture
guards enforce it). Two ports, split on purpose:

- :class:`ReviewQueuePort` owns the items and the SLA policies.
- :class:`ReviewAuditPort` owns the per-item audit trail. It is the
  seam to F1's decision audit log: today the SQLAlchemy implementation
  stores entries in ``review_item_audit``; when F1's log lands, the
  implementation reads entries from that log instead (and keeps
  writing queue actions into it), with no change to the application
  layer or the API.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass

from hesabyar.domain.review_queue.models import (
    ReviewAuditEntry,
    ReviewItem,
    ReviewItemStatus,
    ReviewItemType,
    ReviewSeverity,
    SlaPolicy,
)


@dataclass(frozen=True)
class ReviewItemFilters:
    """List filters (all optional; combined with AND).

    Stored-column filters (type, severity, status, channel, company,
    assignee, amount range) run in the repository. Age/overdue
    filters (``min_age_hours`` / ``max_age_hours`` / ``overdue_only``)
    need the type's SLA policy, which is a service concern — the
    repository ignores them and the service applies them on the
    returned items.
    """

    item_type: ReviewItemType | None = None
    severity: ReviewSeverity | None = None
    status: ReviewItemStatus | None = None
    channel: str | None = None
    company_id: str | None = None
    assignee: str | None = None
    min_amount_irr: int | None = None
    max_amount_irr: int | None = None
    min_age_hours: float | None = None
    max_age_hours: float | None = None
    overdue_only: bool = False


class ReviewQueuePort(ABC):
    """Abstract port for review items and SLA policies."""

    @abstractmethod
    def list_items(self, tenant_id: str, filters: ReviewItemFilters) -> Sequence[ReviewItem]:
        """Items of one tenant matching the filters (any order).

        The service owns sorting (default: critical first, then
        oldest) and SLA evaluation; the repository applies only the
        filters it can answer from stored columns. Age/overdue
        filters are applied by the service on the returned items.
        """
        ...

    @abstractmethod
    def get_item(self, item_id: str, tenant_id: str) -> ReviewItem | None:
        """One item for the owning tenant, or None (also when another tenant's)."""
        ...

    @abstractmethod
    def save_item(self, item: ReviewItem) -> ReviewItem:
        """Insert a new item; returns it with ``created_at``/``updated_at`` set."""
        ...

    @abstractmethod
    def update_item(self, item: ReviewItem) -> ReviewItem:
        """Persist a changed item; returns it.

        Persists status, assignee, resolution — and the item's
        figures (reason, suggested action, amount, severity), so an
        owning module can refresh an in-review item after a recount
        or re-calculation (see
        ``ReviewQueueCommandService.update_item_figures``). Identity
        fields (id, tenant, type, created_at) never change.
        """
        ...

    @abstractmethod
    def list_policies(self, tenant_id: str) -> Sequence[SlaPolicy]:
        """Configured SLA policies for the tenant (may be empty — defaults apply)."""
        ...

    @abstractmethod
    def upsert_policy(self, tenant_id: str, policy: SlaPolicy) -> SlaPolicy:
        """Insert or replace one type's SLA policy for the tenant."""
        ...


class ReviewAuditPort(ABC):
    """Abstract port for the per-item audit trail (the F1 seam).

    Entries are append-only: nothing ever updates or deletes one.
    """

    @abstractmethod
    def record(self, entry: ReviewAuditEntry) -> ReviewAuditEntry:
        """Append one entry; returns it with ``created_at`` set."""
        ...

    @abstractmethod
    def list_for_item(self, item_id: str, tenant_id: str) -> Sequence[ReviewAuditEntry]:
        """Entries of one item, oldest first (empty when the item has none)."""
        ...
