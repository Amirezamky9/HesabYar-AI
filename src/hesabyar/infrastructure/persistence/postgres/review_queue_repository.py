"""PostgreSQL implementation of the review-queue ports (US-504).

Implements ``domain/review_queue/ports.py`` (``ReviewQueuePort`` +
``ReviewAuditPort``) over the ``review_items`` /
``review_item_audit`` / ``review_sla_policies`` tables. This is the
ONLY place that knows the queue's SQLAlchemy models — the application
layer talks to the ports, never to these tables (spec §6.3).

Persistence contract (same as every other writer in the codebase):
``save_item`` / ``update_item`` / ``record`` / ``upsert_policy``
flush so ids/timestamps exist but do NOT commit — the caller (REST
route) owns the transaction.

Age/overdue filtering lives in the service, not here: the repository
applies only stored-column filters (type, severity, status, channel,
company, assignee, amount range). The service evaluates age and SLA
against the type's policy on the returned items.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from hesabyar.domain.review_queue.models import (
    ReviewAuditEntry,
    ReviewItem,
    ReviewItemStatus,
    ReviewItemType,
    ReviewResolution,
    ReviewSeverity,
    SlaPolicy,
)
from hesabyar.domain.review_queue.ports import (
    ReviewAuditPort,
    ReviewItemFilters,
    ReviewQueuePort,
)
from hesabyar.infrastructure.persistence.postgres.models import (
    ReviewItemAuditModel,
    ReviewItemModel,
    ReviewSlaPolicyModel,
)


def _item_from_row(row: ReviewItemModel) -> ReviewItem:
    """Map a review item row onto the domain dataclass (no audit — the query service attaches it)."""
    return ReviewItem(
        id=row.id,
        tenant_id=row.tenant_id,
        item_type=ReviewItemType(row.item_type),
        severity=ReviewSeverity(row.severity),
        status=ReviewItemStatus(row.status),
        reason=row.reason,
        suggested_action=row.suggested_action,
        created_at=row.created_at,
        company_id=row.company_id,
        subtype=row.subtype,
        channel=row.channel,
        source_event_ref=row.source_event_ref,
        amount_irr=row.amount_irr,
        assignee=row.assignee,
        resolution=ReviewResolution(row.resolution) if row.resolution is not None else None,
        resolved_by=row.resolved_by,
        resolved_at=row.resolved_at,
        updated_at=row.updated_at,
    )


def _entry_from_row(row: ReviewItemAuditModel) -> ReviewAuditEntry:
    """Map an audit row onto the domain dataclass."""
    return ReviewAuditEntry(
        id=row.id,
        item_id=row.item_id,
        actor=row.actor,
        action=row.action,
        decision=row.decision,
        note=row.note,
        policy_version=row.policy_version,
        mapping_version=row.mapping_version,
        created_at=row.created_at,
    )


def _policy_from_row(row: ReviewSlaPolicyModel) -> SlaPolicy:
    """Map an SLA policy row onto the domain dataclass."""
    return SlaPolicy(
        item_type=ReviewItemType(row.item_type),
        sla_hours=row.sla_hours,
        warn_hours=row.warn_hours,
        updated_at=row.updated_at,
    )


class SqlAlchemyReviewQueueRepository(ReviewQueuePort, ReviewAuditPort):
    """SQLAlchemy implementation of both review-queue ports."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- ReviewQueuePort ---------------------------------------------------

    def list_items(self, tenant_id: str, filters: ReviewItemFilters) -> Sequence[ReviewItem]:
        stmt = select(ReviewItemModel).where(ReviewItemModel.tenant_id == tenant_id)
        if filters.item_type is not None:
            stmt = stmt.where(ReviewItemModel.item_type == filters.item_type.value)
        if filters.severity is not None:
            stmt = stmt.where(ReviewItemModel.severity == filters.severity.value)
        if filters.status is not None:
            stmt = stmt.where(ReviewItemModel.status == filters.status.value)
        if filters.channel is not None:
            stmt = stmt.where(ReviewItemModel.channel == filters.channel)
        if filters.company_id is not None:
            stmt = stmt.where(ReviewItemModel.company_id == filters.company_id)
        if filters.assignee is not None:
            stmt = stmt.where(ReviewItemModel.assignee == filters.assignee)
        if filters.min_amount_irr is not None:
            stmt = stmt.where(ReviewItemModel.amount_irr >= filters.min_amount_irr)
        if filters.max_amount_irr is not None:
            stmt = stmt.where(ReviewItemModel.amount_irr <= filters.max_amount_irr)
        rows = self._session.execute(stmt).scalars().all()
        return [_item_from_row(row) for row in rows]

    def get_item(self, item_id: str, tenant_id: str) -> ReviewItem | None:
        stmt = select(ReviewItemModel).where(
            ReviewItemModel.id == item_id,
            ReviewItemModel.tenant_id == tenant_id,
        )
        row = self._session.execute(stmt).scalar_one_or_none()
        return _item_from_row(row) if row is not None else None

    def save_item(self, item: ReviewItem) -> ReviewItem:
        row = ReviewItemModel(
            id=item.id,
            tenant_id=item.tenant_id,
            company_id=item.company_id,
            item_type=item.item_type.value,
            subtype=item.subtype,
            severity=item.severity.value,
            status=item.status.value,
            channel=item.channel,
            source_event_ref=item.source_event_ref,
            reason=item.reason,
            suggested_action=item.suggested_action,
            amount_irr=item.amount_irr,
            assignee=item.assignee,
            resolution=item.resolution.value if item.resolution is not None else None,
            resolved_by=item.resolved_by,
            resolved_at=item.resolved_at,
            created_at=item.created_at,
            updated_at=item.updated_at or item.created_at,
        )
        self._session.add(row)
        self._session.flush()
        return _item_from_row(row)

    def update_item(self, item: ReviewItem) -> ReviewItem:
        stmt = select(ReviewItemModel).where(
            ReviewItemModel.id == item.id,
            ReviewItemModel.tenant_id == item.tenant_id,
        )
        row = self._session.execute(stmt).scalar_one_or_none()
        if row is None:
            # The service always loads before updating; a missing row
            # here means the item was deleted concurrently — surface
            # it as a missing item rather than silently inserting.
            raise ValueError(f"Review item {item.id!r} does not exist.")
        row.status = item.status.value
        row.assignee = item.assignee
        row.resolution = item.resolution.value if item.resolution is not None else None
        row.resolved_by = item.resolved_by
        row.resolved_at = item.resolved_at
        # Figures (US-516): an owning module may refresh an in-review
        # item's figures after a recount/re-calculation. Action and
        # assignment flows never change these fields, so persisting
        # them is a no-op for those paths.
        row.reason = item.reason
        row.suggested_action = item.suggested_action
        row.amount_irr = item.amount_irr
        row.severity = item.severity.value
        row.updated_at = item.updated_at or datetime.datetime.now(datetime.UTC)
        self._session.flush()
        return _item_from_row(row)

    def list_policies(self, tenant_id: str) -> Sequence[SlaPolicy]:
        stmt = select(ReviewSlaPolicyModel).where(ReviewSlaPolicyModel.tenant_id == tenant_id)
        rows = self._session.execute(stmt).scalars().all()
        return [_policy_from_row(row) for row in rows]

    def upsert_policy(self, tenant_id: str, policy: SlaPolicy) -> SlaPolicy:
        stmt = select(ReviewSlaPolicyModel).where(
            ReviewSlaPolicyModel.tenant_id == tenant_id,
            ReviewSlaPolicyModel.item_type == policy.item_type.value,
        )
        row = self._session.execute(stmt).scalar_one_or_none()
        now = policy.updated_at or datetime.datetime.now(datetime.UTC)
        if row is None:
            row = ReviewSlaPolicyModel(
                id=str(uuid.uuid4()),
                tenant_id=tenant_id,
                item_type=policy.item_type.value,
                sla_hours=policy.sla_hours,
                warn_hours=policy.warn_hours,
                updated_at=now,
            )
            self._session.add(row)
        else:
            row.sla_hours = policy.sla_hours
            row.warn_hours = policy.warn_hours
            row.updated_at = now
        self._session.flush()
        return _policy_from_row(row)

    # -- ReviewAuditPort ---------------------------------------------------

    def record(self, entry: ReviewAuditEntry) -> ReviewAuditEntry:
        # The tenant is denormalized on the audit row so tenant-scoped
        # reads never need a join; resolve it from the item row.
        item = self._session.get(ReviewItemModel, entry.item_id)
        if item is None:
            raise ValueError(f"Review item {entry.item_id!r} does not exist.")
        row = ReviewItemAuditModel(
            id=entry.id,
            item_id=entry.item_id,
            tenant_id=item.tenant_id,
            actor=entry.actor,
            action=entry.action,
            decision=entry.decision,
            note=entry.note,
            policy_version=entry.policy_version,
            mapping_version=entry.mapping_version,
            created_at=entry.created_at or datetime.datetime.now(datetime.UTC),
        )
        self._session.add(row)
        self._session.flush()
        return _entry_from_row(row)

    def list_for_item(self, item_id: str, tenant_id: str) -> Sequence[ReviewAuditEntry]:
        stmt = (
            select(ReviewItemAuditModel)
            .where(
                ReviewItemAuditModel.item_id == item_id,
                ReviewItemAuditModel.tenant_id == tenant_id,
            )
            .order_by(ReviewItemAuditModel.created_at, ReviewItemAuditModel.id)
        )
        rows = self._session.execute(stmt).scalars().all()
        return [_entry_from_row(row) for row in rows]
