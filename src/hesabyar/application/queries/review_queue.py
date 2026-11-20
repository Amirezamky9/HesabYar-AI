"""Review-queue read use cases (US-504, Phase 5 F4).

REST and (later) MCP call this service instead of querying the review
tables directly, so queue reads have one implementation and one error
contract (design-eng spec §6.2/§6.3).

The service talks to persistence ONLY through the domain ports —
``ReviewQueuePort`` for items/policies, ``ReviewAuditPort`` for the
audit trail — it never imports infrastructure (spec §6.3). Sorting,
ageing and SLA evaluation live here, not in SQL: the default sort is
critical first, then oldest (phased design §7.2), and SLA deadlines
are computed from ``created_at`` + the type's policy at read time, so
a policy change re-ages the queue immediately.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from dataclasses import dataclass

from hesabyar.application.errors import ReviewItemNotFoundError
from hesabyar.domain.review_queue.models import (
    DEFAULT_SLA_POLICIES,
    AdminNotification,
    ReviewAuditEntry,
    ReviewItem,
    ReviewItemStatus,
    ReviewItemType,
    ReviewQueueSummary,
    ReviewSeverity,
    ReviewTypeCounter,
    SlaPolicy,
)
from hesabyar.domain.review_queue.ports import (
    ReviewAuditPort,
    ReviewItemFilters,
    ReviewQueuePort,
)

#: Default sort: severity rank first (critical 0, warning 1, info 2),
#: then oldest first. The rank is part of the service, not the port —
#: every caller sees the same queue order.
_SEVERITY_RANK = {
    ReviewSeverity.CRITICAL: 0,
    ReviewSeverity.WARNING: 1,
    ReviewSeverity.INFO: 2,
}


@dataclass(frozen=True)
class ReviewItemView:
    """A review item with its computed ageing/SLA fields at read time."""

    item: ReviewItem
    age_hours: float
    sla_deadline: datetime.datetime
    sla_breached: bool
    sla_warn: bool


class ReviewQueueQueryService:
    """Read-only review-queue queries over the domain ports."""

    def __init__(
        self,
        queue: ReviewQueuePort,
        audit: ReviewAuditPort,
        *,
        now: datetime.datetime | None = None,
    ) -> None:
        self._queue = queue
        self._audit = audit
        # ``now`` is injectable for tests; production passes None and
        # the clock is read per call.
        self._now = now

    def _clock(self) -> datetime.datetime:
        if self._now is not None:
            return self._now
        return datetime.datetime.now(datetime.UTC)

    def _policies(self, tenant_id: str) -> dict[ReviewItemType, SlaPolicy]:
        """Effective SLA policy per type: configured rows over defaults."""
        policies = dict(DEFAULT_SLA_POLICIES)
        for policy in self._queue.list_policies(tenant_id):
            policies[policy.item_type] = policy
        return policies

    def _view(
        self, item: ReviewItem, policies: dict[ReviewItemType, SlaPolicy], now: datetime.datetime
    ) -> ReviewItemView:
        policy = policies[item.item_type]
        return ReviewItemView(
            item=item,
            age_hours=item.age_hours(now),
            sla_deadline=item.sla_deadline(policy),
            sla_breached=item.sla_breached(now, policy),
            sla_warn=item.sla_warn(now, policy),
        )

    def list_items(
        self,
        tenant_id: str,
        filters: ReviewItemFilters,
        *,
        now: datetime.datetime | None = None,
    ) -> Sequence[ReviewItemView]:
        """Return the tenant's items matching the filters, default-sorted.

        Stored-column filters run in the repository; age/overdue
        filters run here (they need the SLA policy, which is a service
        concern). The default sort is critical first, then oldest —
        callers that need another order sort the returned views.
        """
        current = now or self._clock()
        policies = self._policies(tenant_id)
        stored = ReviewItemFilters(
            item_type=filters.item_type,
            severity=filters.severity,
            status=filters.status,
            channel=filters.channel,
            company_id=filters.company_id,
            assignee=filters.assignee,
            min_amount_irr=filters.min_amount_irr,
            max_amount_irr=filters.max_amount_irr,
        )
        views = [self._view(item, policies, current) for item in self._queue.list_items(tenant_id, stored)]
        if filters.min_age_hours is not None:
            views = [view for view in views if view.age_hours >= filters.min_age_hours]
        if filters.max_age_hours is not None:
            views = [view for view in views if view.age_hours <= filters.max_age_hours]
        if filters.overdue_only:
            views = [view for view in views if view.sla_breached]
        views.sort(key=lambda view: (_SEVERITY_RANK[view.item.severity], view.item.created_at, view.item.id))
        return views

    def get_item(
        self,
        item_id: str,
        tenant_id: str,
        *,
        now: datetime.datetime | None = None,
    ) -> ReviewItemView:
        """Return one item with its audit trail.

        Raises :class:`ReviewItemNotFoundError` for an unknown id (or
        another tenant's item — it looks exactly like a missing one).
        """
        current = now or self._clock()
        policies = self._policies(tenant_id)
        item = self._queue.get_item(item_id, tenant_id)
        if item is None:
            raise ReviewItemNotFoundError(
                f"Review item {item_id!r} does not exist.",
                details={"item_id": item_id},
            )
        audit = tuple(self._audit.list_for_item(item_id, tenant_id))
        item = _with_audit(item, audit)
        return self._view(item, policies, current)

    def sla_policies(self, tenant_id: str) -> Sequence[SlaPolicy]:
        """Effective SLA policy for all ten types (configured over default)."""
        policies = self._policies(tenant_id)
        return tuple(policies[item_type] for item_type in ReviewItemType)

    def summary(
        self,
        tenant_id: str,
        *,
        company_id: str | None = None,
        now: datetime.datetime | None = None,
    ) -> ReviewQueueSummary:
        """Per-type counters + ageing + admin notifications for the dashboard.

        All ten types are always present (even with zero items). A
        critical item past its SLA produces one admin notification —
        the dashboard and the queue page surface it as an alert.
        """
        current = now or self._clock()
        policies = self._policies(tenant_id)
        items = self._queue.list_items(tenant_id, ReviewItemFilters(company_id=company_id))
        views = [self._view(item, policies, current) for item in items]

        counters: list[ReviewTypeCounter] = []
        notifications: list[AdminNotification] = []
        oldest_age: float | None = None
        oldest_id: str | None = None
        for item_type in ReviewItemType:
            typed = [view for view in views if view.item.item_type == item_type]
            open_views = [view for view in typed if view.item.status == ReviewItemStatus.OPEN]
            in_review = [view for view in typed if view.item.status == ReviewItemStatus.IN_REVIEW]
            resolved = [
                view for view in typed if view.item.status in (ReviewItemStatus.RESOLVED, ReviewItemStatus.WRITTEN_OFF)
            ]
            overdue = [view for view in typed if view.sla_breached]
            waiting = open_views + in_review
            type_oldest_age: float | None = None
            type_oldest_id: str | None = None
            if waiting:
                oldest = max(waiting, key=lambda view: view.age_hours)
                type_oldest_age = oldest.age_hours
                type_oldest_id = oldest.item.id
                if oldest_age is None or oldest.age_hours > oldest_age:
                    oldest_age = oldest.age_hours
                    oldest_id = oldest.item.id
            counters.append(
                ReviewTypeCounter(
                    item_type=item_type,
                    open_count=len(open_views),
                    in_review_count=len(in_review),
                    resolved_count=len(resolved),
                    overdue_count=len(overdue),
                    oldest_open_age_hours=type_oldest_age,
                    oldest_open_item_id=type_oldest_id,
                )
            )
            for view in overdue:
                if view.item.severity is not ReviewSeverity.CRITICAL:
                    continue
                overdue_hours = view.age_hours - policies[item_type].sla_hours
                notifications.append(
                    AdminNotification(
                        item_id=view.item.id,
                        item_type=item_type,
                        severity=view.item.severity,
                        message=(
                            f"Critical {item_type.value} item {view.item.id} is {overdue_hours:.1f} hours past its SLA."
                        ),
                        overdue_hours=overdue_hours,
                        created_at=current,
                    )
                )
        notifications.sort(key=lambda note: (-note.overdue_hours, note.item_id))
        return ReviewQueueSummary(
            counters=tuple(counters),
            total_open=sum(counter.open_count for counter in counters),
            total_in_review=sum(counter.in_review_count for counter in counters),
            total_overdue=sum(counter.overdue_count for counter in counters),
            critical_overdue_count=len(notifications),
            oldest_open_age_hours=oldest_age,
            oldest_open_item_id=oldest_id,
            admin_notifications=tuple(notifications),
        )


def _with_audit(item: ReviewItem, audit: tuple[ReviewAuditEntry, ...]) -> ReviewItem:
    """Return a copy of the item carrying its audit trail."""
    return ReviewItem(
        id=item.id,
        tenant_id=item.tenant_id,
        item_type=item.item_type,
        severity=item.severity,
        status=item.status,
        reason=item.reason,
        suggested_action=item.suggested_action,
        created_at=item.created_at,
        company_id=item.company_id,
        subtype=item.subtype,
        channel=item.channel,
        source_event_ref=item.source_event_ref,
        amount_irr=item.amount_irr,
        assignee=item.assignee,
        resolution=item.resolution,
        resolved_by=item.resolved_by,
        resolved_at=item.resolved_at,
        updated_at=item.updated_at,
        audit=audit,
    )
