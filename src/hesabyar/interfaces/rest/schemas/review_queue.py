"""Request/response schemas for the review-queue endpoints (US-504).

Responses map from the domain views — no ORM object ever leaks. The
list item carries the computed ageing/SLA fields (``age_hours``,
``sla_deadline``, ``sla_breached``, ``sla_warn``); the detail
response additionally carries the ``audit`` trail. Money is IRR
integers; the frontend renders them as تومان.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from hesabyar.application.queries.review_queue import ReviewItemView
from hesabyar.domain.review_queue.models import (
    PERMITTED_ACTIONS,
    AdminNotification,
    ReviewAuditEntry,
    ReviewItemType,
    ReviewQueueSummary,
    ReviewTypeCounter,
    SlaPolicy,
)


class ReviewActionResponse(BaseModel):
    """One permitted action on a review item (phased design §7.2)."""

    code: str = Field(description="Action code, e.g. 'replay'")
    kind: str = Field(description="Action kind: resolve | retry | escalate | dismiss | note")
    resolution: str | None = Field(description="Resolution the action records when it closes the item")
    requires_manager: bool = Field(description="True when §7.2 assigns the action to a manager")


class ReviewAuditEntryResponse(BaseModel):
    """One audit entry on a review item (who, when, what decision)."""

    id: str = Field(description="Audit entry id")
    actor: str = Field(description="Actor who performed the action")
    action: str = Field(description="Action code performed")
    decision: str | None = Field(description="Decision recorded by the action")
    note: str | None = Field(description="Human note attached to the action")
    policy_version: str | None = Field(description="Policy version the decision used")
    mapping_version: str | None = Field(description="Mapping version the decision used")
    created_at: datetime | None = Field(description="When the entry was recorded (UTC)")

    @classmethod
    def from_entry(cls, entry: ReviewAuditEntry) -> ReviewAuditEntryResponse:
        """Build a response from a domain audit entry."""
        return cls(
            id=entry.id,
            actor=entry.actor,
            action=entry.action,
            decision=entry.decision,
            note=entry.note,
            policy_version=entry.policy_version,
            mapping_version=entry.mapping_version,
            created_at=entry.created_at,
        )


class ReviewItemResponse(BaseModel):
    """One uniform review-queue item with computed ageing/SLA fields."""

    id: str = Field(description="Review item id")
    item_type: str = Field(description="Exception type — one of the nine §7.2 types")
    subtype: str | None = Field(description="Subtype for filtering/stats")
    severity: str = Field(description="Severity: info | warning | critical")
    status: str = Field(description="Status: open | in_review | resolved | written_off")
    channel: str | None = Field(description="Payment channel / gateway key")
    company_id: str | None = Field(description="API company id, when the item is company-scoped")
    source_event_ref: str | None = Field(description="Reference to the originating event")
    reason: str = Field(description="Why the item needs review")
    suggested_action: str = Field(description="System-suggested next step")
    amount_irr: int | None = Field(description="Amount at stake in IRR (null when the item carries no money)")
    assignee: str | None = Field(description="Actor assigned to the item")
    resolution: str | None = Field(description="How the item was closed (null while open)")
    resolved_by: str | None = Field(description="Actor who closed the item")
    resolved_at: datetime | None = Field(description="When the item was closed (UTC)")
    created_at: datetime = Field(description="When the item was filed (UTC) — the age/SLA anchor")
    updated_at: datetime | None = Field(description="Last update (UTC)")
    age_hours: float = Field(description="Age in hours (fractional) at read time")
    sla_deadline: datetime = Field(description="SLA deadline under the type's policy (UTC)")
    sla_breached: bool = Field(description="True when open/in-review and past the SLA deadline")
    sla_warn: bool = Field(description="True when open/in-review and inside the warn window")
    actions: list[ReviewActionResponse] = Field(description="Permitted actions for this item's type (§7.2)")
    audit: list[ReviewAuditEntryResponse] = Field(
        description="Audit trail (detail endpoint only; empty on the list endpoint)"
    )

    @classmethod
    def from_view(cls, view: ReviewItemView, *, include_audit: bool = False) -> ReviewItemResponse:
        """Build a response from the service's item view."""
        item = view.item
        return cls(
            id=item.id,
            item_type=item.item_type.value,
            subtype=item.subtype,
            severity=item.severity.value,
            status=item.status.value,
            channel=item.channel,
            company_id=item.company_id,
            source_event_ref=item.source_event_ref,
            reason=item.reason,
            suggested_action=item.suggested_action,
            amount_irr=item.amount_irr,
            assignee=item.assignee,
            resolution=item.resolution.value if item.resolution is not None else None,
            resolved_by=item.resolved_by,
            resolved_at=item.resolved_at,
            created_at=item.created_at,
            updated_at=item.updated_at,
            age_hours=view.age_hours,
            sla_deadline=view.sla_deadline,
            sla_breached=view.sla_breached,
            sla_warn=view.sla_warn,
            actions=[
                ReviewActionResponse(
                    code=action.code,
                    kind=action.kind.value,
                    resolution=action.resolution.value if action.resolution is not None else None,
                    requires_manager=action.requires_manager,
                )
                for action in PERMITTED_ACTIONS[item.item_type]
            ],
            audit=([ReviewAuditEntryResponse.from_entry(entry) for entry in item.audit] if include_audit else []),
        )


class CreateReviewItemRequest(BaseModel):
    """File a new exception item (modules file through this endpoint)."""

    item_type: str = Field(description="Exception type — one of the nine §7.2 types")
    severity: str = Field(description="Severity: info | warning | critical")
    reason: str = Field(description="Why the item needs review (Persian, human-readable)", min_length=1)
    suggested_action: str = Field(description="System-suggested next step", min_length=1)
    company_id: str | None = Field(default=None, description="API company id, when company-scoped")
    subtype: str | None = Field(default=None, description="Subtype for filtering/stats")
    channel: str | None = Field(default=None, description="Payment channel / gateway key")
    source_event_ref: str | None = Field(default=None, description="Reference to the originating event")
    amount_irr: int | None = Field(default=None, ge=0, description="Amount at stake in IRR")
    assignee: str | None = Field(default=None, description="Actor to assign the item to")


class PerformReviewActionRequest(BaseModel):
    """Perform a permitted action on a review item."""

    action: str = Field(description="Action code — must be permitted for the item's type", min_length=1)
    note: str | None = Field(default=None, description="Human note recorded with the action")


class AssignReviewItemRequest(BaseModel):
    """Assign (or unassign, with null) a review item."""

    assignee: str | None = Field(default=None, description="Actor id/name, or null to unassign")


class ReviewTypeCounterResponse(BaseModel):
    """Per-type counters for the dashboard (all nine types always present)."""

    item_type: str = Field(description="Exception type")
    open_count: int = Field(description="Open items of this type")
    in_review_count: int = Field(description="In-review items of this type")
    resolved_count: int = Field(description="Resolved + written-off items of this type")
    overdue_count: int = Field(description="Open/in-review items past their SLA deadline")
    oldest_open_age_hours: float | None = Field(description="Age of the oldest waiting item (null when none)")
    oldest_open_item_id: str | None = Field(description="Id of the oldest waiting item (null when none)")

    @classmethod
    def from_counter(cls, counter: ReviewTypeCounter) -> ReviewTypeCounterResponse:
        """Build a response from a domain counter."""
        return cls(
            item_type=counter.item_type.value,
            open_count=counter.open_count,
            in_review_count=counter.in_review_count,
            resolved_count=counter.resolved_count,
            overdue_count=counter.overdue_count,
            oldest_open_age_hours=counter.oldest_open_age_hours,
            oldest_open_item_id=counter.oldest_open_item_id,
        )


class AdminNotificationResponse(BaseModel):
    """An admin notification for a critical item past its SLA."""

    item_id: str = Field(description="Review item id")
    item_type: str = Field(description="Exception type")
    severity: str = Field(description="Severity (always 'critical')")
    message: str = Field(description="Human-readable notification message")
    overdue_hours: float = Field(description="Hours past the SLA deadline (fractional)")
    created_at: datetime = Field(description="When the notification was computed (UTC)")

    @classmethod
    def from_notification(cls, note: AdminNotification) -> AdminNotificationResponse:
        """Build a response from a domain notification."""
        return cls(
            item_id=note.item_id,
            item_type=note.item_type.value,
            severity=note.severity.value,
            message=note.message,
            overdue_hours=note.overdue_hours,
            created_at=note.created_at,
        )


class ReviewQueueSummaryResponse(BaseModel):
    """Queue summary for the dashboard: counters + ageing + alerts."""

    counters: list[ReviewTypeCounterResponse] = Field(description="Per-type counters (all nine types)")
    total_open: int = Field(description="Total open items")
    total_in_review: int = Field(description="Total in-review items")
    total_overdue: int = Field(description="Total open/in-review items past SLA")
    critical_overdue_count: int = Field(description="Critical items past SLA (== len(admin_notifications))")
    oldest_open_age_hours: float | None = Field(description="Age of the oldest waiting item (null when none)")
    oldest_open_item_id: str | None = Field(description="Id of the oldest waiting item (null when none)")
    admin_notifications: list[AdminNotificationResponse] = Field(
        description="One notification per critical item past SLA"
    )

    @classmethod
    def from_summary(cls, summary: ReviewQueueSummary) -> ReviewQueueSummaryResponse:
        """Build a response from the domain summary."""
        return cls(
            counters=[ReviewTypeCounterResponse.from_counter(c) for c in summary.counters],
            total_open=summary.total_open,
            total_in_review=summary.total_in_review,
            total_overdue=summary.total_overdue,
            critical_overdue_count=summary.critical_overdue_count,
            oldest_open_age_hours=summary.oldest_open_age_hours,
            oldest_open_item_id=summary.oldest_open_item_id,
            admin_notifications=[
                AdminNotificationResponse.from_notification(note) for note in summary.admin_notifications
            ],
        )


class SlaPolicyResponse(BaseModel):
    """One type's effective SLA policy (configured row over the skeleton default)."""

    item_type: str = Field(description="Exception type")
    sla_hours: int = Field(description="SLA deadline in whole hours after filing")
    warn_hours: int = Field(description="Warn window in whole hours before the deadline")
    updated_at: datetime | None = Field(description="When the policy was last configured (null when default)")

    @classmethod
    def from_policy(cls, policy: SlaPolicy) -> SlaPolicyResponse:
        """Build a response from a domain policy."""
        return cls(
            item_type=policy.item_type.value,
            sla_hours=policy.sla_hours,
            warn_hours=policy.warn_hours,
            updated_at=policy.updated_at,
        )


class UpdateSlaPolicyRequest(BaseModel):
    """Set one type's SLA policy (0 < warn_hours < sla_hours)."""

    sla_hours: int = Field(description="SLA deadline in whole hours after filing", gt=0)
    warn_hours: int = Field(description="Warn window in whole hours before the deadline", gt=0)


class ReviewItemTypeResponse(BaseModel):
    """One of the nine item types with its permitted actions (§7.2)."""

    item_type: str = Field(description="Exception type")
    actions: list[ReviewActionResponse] = Field(description="Permitted actions for this type")

    @classmethod
    def from_type(cls, item_type: ReviewItemType) -> ReviewItemTypeResponse:
        """Build a response from a domain item type."""
        return cls(
            item_type=item_type.value,
            actions=[
                ReviewActionResponse(
                    code=action.code,
                    kind=action.kind.value,
                    resolution=action.resolution.value if action.resolution is not None else None,
                    requires_manager=action.requires_manager,
                )
                for action in PERMITTED_ACTIONS[item_type]
            ],
        )
