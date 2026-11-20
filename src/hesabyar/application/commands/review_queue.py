"""Review-queue write use cases (US-504, Phase 5 F4).

Filing items, performing actions, assigning, and SLA-policy updates.
Like the query service, this layer talks to persistence ONLY through
the domain ports — never infrastructure (spec §6.3).

Action semantics (phased design §7.2): an action is accepted only
when it is permitted for the item's type, and only while the item is
not terminal. The kind decides the lifecycle effect:

- ``resolve`` → ``resolved`` with the action's resolution;
- ``dismiss`` → ``resolved`` (``skipped``/``deferred``) or
  ``written_off`` (``write_off`` resolution);
- ``retry`` / ``escalate`` / ``note`` → ``in_review``, stays open.

The skeleton records the decision and the lifecycle change; the
type-specific effect (replaying a dead letter, rebuilding a mapping,
posting an adjustment journal) belongs to the owning module and is
requested through the audit trail — it is NOT faked here. Every
action appends an audit entry: no exception is ever resolved
silently.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid

from hesabyar.application.errors import (
    ReviewActionNotPermittedError,
    ReviewInvalidSlaPolicyError,
    ReviewInvalidStateError,
    ReviewItemNotFoundError,
)
from hesabyar.domain.review_queue.models import (
    ReviewActionKind,
    ReviewAuditEntry,
    ReviewItem,
    ReviewItemStatus,
    ReviewItemType,
    ReviewResolution,
    ReviewSeverity,
    SlaPolicy,
    permitted_actions,
)
from hesabyar.domain.review_queue.ports import ReviewAuditPort, ReviewQueuePort


class ReviewQueueCommandService:
    """Write use cases for the review queue over the domain ports."""

    def __init__(
        self,
        queue: ReviewQueuePort,
        audit: ReviewAuditPort,
        *,
        now: datetime.datetime | None = None,
    ) -> None:
        self._queue = queue
        self._audit = audit
        self._now = now

    def _clock(self) -> datetime.datetime:
        if self._now is not None:
            return self._now
        return datetime.datetime.now(datetime.UTC)

    def _require_item(self, item_id: str, tenant_id: str) -> ReviewItem:
        item = self._queue.get_item(item_id, tenant_id)
        if item is None:
            raise ReviewItemNotFoundError(
                f"Review item {item_id!r} does not exist.",
                details={"item_id": item_id},
            )
        return item

    def file_item(
        self,
        *,
        tenant_id: str,
        item_type: ReviewItemType,
        severity: ReviewSeverity,
        reason: str,
        suggested_action: str,
        company_id: str | None = None,
        subtype: str | None = None,
        channel: str | None = None,
        source_event_ref: str | None = None,
        amount_irr: int | None = None,
        assignee: str | None = None,
        actor: str,
    ) -> ReviewItem:
        """File a new exception item (status ``open``).

        Modules file items through this use case — never by writing
        the table directly — so every item is born with the same
        shape and a ``filed`` audit entry.
        """
        now = self._clock()
        item = ReviewItem(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            item_type=item_type,
            severity=severity,
            status=ReviewItemStatus.OPEN,
            reason=reason,
            suggested_action=suggested_action,
            created_at=now,
            company_id=company_id,
            subtype=subtype,
            channel=channel,
            source_event_ref=source_event_ref,
            amount_irr=amount_irr,
            assignee=assignee,
            updated_at=now,
        )
        saved = self._queue.save_item(item)
        self._audit.record(
            ReviewAuditEntry(
                id=str(uuid.uuid4()),
                item_id=saved.id,
                actor=actor,
                action="filed",
                decision="open",
                created_at=now,
            )
        )
        return saved

    def perform_action(
        self,
        *,
        item_id: str,
        tenant_id: str,
        action_code: str,
        actor: str,
        note: str | None = None,
    ) -> ReviewItem:
        """Perform a permitted action on an item.

        Raises :class:`ReviewItemNotFoundError` for an unknown item,
        :class:`ReviewInvalidStateError` for a terminal item, and
        :class:`ReviewActionNotPermittedError` for an action outside
        the type's §7.2 list. The audit entry records who, when,
        which action and which decision.
        """
        item = self._require_item(item_id, tenant_id)
        if item.is_terminal:
            raise ReviewInvalidStateError(
                f"Review item {item_id!r} is {item.status.value} and accepts no further actions.",
                details={"item_id": item_id, "status": item.status.value},
            )
        action = next(
            (candidate for candidate in permitted_actions(item.item_type) if candidate.code == action_code),
            None,
        )
        if action is None:
            raise ReviewActionNotPermittedError(
                f"Action {action_code!r} is not permitted for {item.item_type.value} items.",
                details={
                    "item_id": item_id,
                    "item_type": item.item_type.value,
                    "action": action_code,
                    "permitted": [candidate.code for candidate in permitted_actions(item.item_type)],
                },
            )
        now = self._clock()
        if action.kind is ReviewActionKind.RESOLVE:
            status = ReviewItemStatus.RESOLVED
        elif action.kind is ReviewActionKind.DISMISS:
            status = (
                ReviewItemStatus.WRITTEN_OFF
                if action.resolution is ReviewResolution.WRITE_OFF
                else ReviewItemStatus.RESOLVED
            )
        else:
            status = ReviewItemStatus.IN_REVIEW
        terminal = status in (ReviewItemStatus.RESOLVED, ReviewItemStatus.WRITTEN_OFF)
        updated = dataclasses.replace(
            item,
            status=status,
            resolution=action.resolution if terminal else item.resolution,
            resolved_by=actor if terminal else item.resolved_by,
            resolved_at=now if terminal else item.resolved_at,
            updated_at=now,
        )
        saved = self._queue.update_item(updated)
        decision = action.resolution.value if terminal and action.resolution is not None else status.value
        self._audit.record(
            ReviewAuditEntry(
                id=str(uuid.uuid4()),
                item_id=saved.id,
                actor=actor,
                action=action_code,
                decision=decision,
                note=note,
                created_at=now,
            )
        )
        return saved

    def update_item_figures(
        self,
        *,
        item_id: str,
        tenant_id: str,
        reason: str,
        suggested_action: str,
        amount_irr: int | None,
        severity: ReviewSeverity,
        actor: str,
        note: str | None = None,
    ) -> ReviewItem:
        """Refresh an in-review item's figures from its owning module.

        The owning module reports a new result for an exception the
        queue already holds — e.g. a cash-box recount (US-516) that
        re-measures the variance, or a settlement re-calculation. The
        item's reason, suggested action, amount and severity are
        updated, its status returns to ``open`` (a fresh result
        awaiting a decision), and the update is recorded in the audit
        trail. Only a non-terminal item can be refreshed: a resolved
        item's figures are history, and a new exception files a new
        item. Terminal items raise :class:`ReviewInvalidStateError`.
        """
        item = self._require_item(item_id, tenant_id)
        if item.is_terminal:
            raise ReviewInvalidStateError(
                f"Review item {item_id!r} is {item.status.value} and its figures are closed.",
                details={"item_id": item_id, "status": item.status.value},
            )
        now = self._clock()
        updated = dataclasses.replace(
            item,
            status=ReviewItemStatus.OPEN,
            reason=reason,
            suggested_action=suggested_action,
            amount_irr=amount_irr,
            severity=severity,
            updated_at=now,
        )
        saved = self._queue.update_item(updated)
        self._audit.record(
            ReviewAuditEntry(
                id=str(uuid.uuid4()),
                item_id=saved.id,
                actor=actor,
                action="figures_updated",
                decision="open",
                note=note,
                created_at=now,
            )
        )
        return saved

    def assign_item(
        self,
        *,
        item_id: str,
        tenant_id: str,
        assignee: str | None,
        actor: str,
    ) -> ReviewItem:
        """Assign (or unassign) an item; recorded in the audit trail."""
        item = self._require_item(item_id, tenant_id)
        if item.is_terminal:
            raise ReviewInvalidStateError(
                f"Review item {item_id!r} is {item.status.value} and cannot be reassigned.",
                details={"item_id": item_id, "status": item.status.value},
            )
        now = self._clock()
        saved = self._queue.update_item(dataclasses.replace(item, assignee=assignee, updated_at=now))
        self._audit.record(
            ReviewAuditEntry(
                id=str(uuid.uuid4()),
                item_id=saved.id,
                actor=actor,
                action="assign",
                decision=assignee or "unassigned",
                created_at=now,
            )
        )
        return saved

    def update_sla_policy(
        self,
        *,
        tenant_id: str,
        item_type: ReviewItemType,
        sla_hours: int,
        warn_hours: int,
    ) -> SlaPolicy:
        """Set one type's SLA policy (admin-configurable per type).

        ``warn_hours`` must be positive and smaller than ``sla_hours``;
        both must be positive. The defaults
        (:data:`DEFAULT_SLA_POLICIES`) apply until an admin configures
        a type.
        """
        if sla_hours <= 0 or warn_hours <= 0 or warn_hours >= sla_hours:
            raise ReviewInvalidSlaPolicyError(
                f"SLA policy requires 0 < warn_hours < sla_hours (got sla_hours={sla_hours}, warn_hours={warn_hours}).",
                details={
                    "item_type": item_type.value,
                    "sla_hours": sla_hours,
                    "warn_hours": warn_hours,
                },
            )
        policy = SlaPolicy(
            item_type=item_type,
            sla_hours=sla_hours,
            warn_hours=warn_hours,
            updated_at=self._clock(),
        )
        return self._queue.upsert_policy(tenant_id, policy)
