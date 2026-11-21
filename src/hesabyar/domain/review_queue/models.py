"""Uniform review-queue data model (Phase 5, F4 skeleton).

Phased design §1.2 (F4) and §7.2: every exception — from any module —
is a ``ReviewItem`` with the same fields: ``id, type, severity,
source_event_ref, reason, suggested_action, amount_irr, age +
sla_deadline, actions[], assignee, audit[]``. The eleven item types and
their permitted actions come from §7.2 (the tenth and eleventh types
arrived later — see :class:`ReviewItemType`); the Persian labels live in the
frontend (``features/review-queue``) — this layer stays English.

Ageing and SLA
--------------
``age`` and ``sla_deadline`` are computed, never stored: age is
``now - created_at`` and the deadline is ``created_at + sla_hours``
from the type's SLA policy (configurable per type; see
:class:`SlaPolicy` and :data:`DEFAULT_SLA_POLICIES`). A resolved item
stops ageing for SLA purposes — ``sla_breached`` is only meaningful
while the item is open or in review.

Audit trail
-----------
Every action on an item appends a :class:`ReviewAuditEntry` (who,
when, what decision, with which policy/mapping version). The entries
are served through the ``ReviewAuditPort`` — when F1's decision audit
log lands, that log becomes the audit source and this port's
implementation reads from it instead (the seam is the port, not the
model). No exception is ever resolved silently.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from enum import StrEnum, unique


@unique
class ReviewItemType(StrEnum):
    """The eleven exception types of phased design §7.2, in table order.

    The tenth type (``SMS_DELIVERY_FAILED``) arrived with US-544
    (Phase 5-5 ۵ج): an SMS reminder whose send failed. The
    eleventh type (``REMINDER``) arrived with US-564 (Phase 5-7
    ۵خ W3): a reminder's in-app delivery — the review queue is
    the operator's in-app notification surface, so a due cheque,
    an overdue installment, an uncounted cash box, or an expiring
    eNamad lands here as its own type (not shoehorned into the
    exception types). The §7.2 table predates both; the types
    follow the same uniform shape as the other nine.
    """

    MAPPING = "mapping"
    SETTLEMENT_VARIANCE = "settlement_variance"
    CHEQUE_EXCEPTION = "cheque_exception"
    BANK_UNMATCHED = "bank_unmatched"
    PACKAGING_REORDER = "packaging_reorder"
    INSTALLMENT_OVERDUE = "installment_overdue"
    MANUAL_IMPORT_REVIEW = "manual_import_review"
    CASHBOX_VARIANCE = "cashbox_variance"
    INBOX_DEAD_LETTER = "inbox_dead_letter"
    SMS_DELIVERY_FAILED = "sms_delivery_failed"
    REMINDER = "reminder"


@unique
class ReviewSeverity(StrEnum):
    """How urgently an item needs attention (phased design §7.2)."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@unique
class ReviewItemStatus(StrEnum):
    """Lifecycle of a review item.

    ``open`` → ``in_review`` → ``resolved`` / ``written_off``. A
    resolved or written-off item is terminal: no further action may be
    performed on it (a new exception files a new item).
    """

    OPEN = "open"
    IN_REVIEW = "in_review"
    RESOLVED = "resolved"
    WRITTEN_OFF = "written_off"


@unique
class ReviewResolution(StrEnum):
    """How an item was closed (phased design §3.7 resolution vocabulary)."""

    CONFIRMED_OK = "confirmed_ok"
    ADJUSTED = "adjusted"
    WRITE_OFF = "write_off"
    ESCALATED = "escalated"
    RESCHEDULED = "rescheduled"
    SKIPPED = "skipped"
    DEFERRED = "deferred"


@unique
class ReviewActionKind(StrEnum):
    """What an action does to the item's lifecycle.

    - ``resolve``: closes the item (status → ``resolved``) with the
      action's resolution.
    - ``retry``: reprocessing was requested — the item moves to
      ``in_review`` and stays open until the owning module reports the
      retry outcome. The skeleton records the request; the module
      executes it.
    - ``escalate``: the item moves to ``in_review`` and is flagged for
      a manager; it stays open.
    - ``dismiss``: closes the item without a fix (status →
      ``resolved`` with resolution ``skipped``/``deferred``, or
      ``written_off`` when the action says so).
    - ``note``: records an action taken outside the queue (e.g. a
      phone call, a reminder sent) — the item moves to ``in_review``
      and stays open.
    """

    RESOLVE = "resolve"
    RETRY = "retry"
    ESCALATE = "escalate"
    DISMISS = "dismiss"
    NOTE = "note"


@dataclass(frozen=True)
class ReviewAction:
    """One permitted action on a review item (§7.2, per type).

    ``requires_manager`` marks actions §7.2 assigns to a manager
    (large settlement variances, collateral-cheque activation,
    cash-box sign-off). The API exposes the flag; role enforcement
    lands with module 12 (users & access) — until then the flag is
    advisory and the audit entry records who actually performed it.
    """

    code: str
    kind: ReviewActionKind
    resolution: ReviewResolution | None = None
    requires_manager: bool = False


#: Permitted actions per item type (phased design §7.2, verbatim).
#: A type's actions are the ONLY actions the API accepts for its items.
PERMITTED_ACTIONS: dict[ReviewItemType, tuple[ReviewAction, ...]] = {
    ReviewItemType.MAPPING: (
        ReviewAction("complete_mapping_retry", ReviewActionKind.RETRY),
        ReviewAction("auto_create_party_item", ReviewActionKind.RESOLVE, ReviewResolution.ADJUSTED),
        ReviewAction("skip", ReviewActionKind.DISMISS, ReviewResolution.SKIPPED),
    ),
    ReviewItemType.SETTLEMENT_VARIANCE: (
        ReviewAction("accept_variance", ReviewActionKind.RESOLVE, ReviewResolution.ADJUSTED),
        ReviewAction("escalate_to_gateway", ReviewActionKind.ESCALATE, ReviewResolution.ESCALATED),
        ReviewAction("fix_fee_mapping", ReviewActionKind.RESOLVE, ReviewResolution.ADJUSTED),
    ),
    ReviewItemType.CHEQUE_EXCEPTION: (
        ReviewAction("record_bounce", ReviewActionKind.RESOLVE, ReviewResolution.ADJUSTED),
        ReviewAction("contact_customer", ReviewActionKind.NOTE),
        ReviewAction("record_alternative_payment", ReviewActionKind.RESOLVE, ReviewResolution.ADJUSTED),
    ),
    ReviewItemType.BANK_UNMATCHED: (
        ReviewAction("manual_match", ReviewActionKind.RESOLVE, ReviewResolution.ADJUSTED),
        ReviewAction("record_misc_income", ReviewActionKind.RESOLVE, ReviewResolution.ADJUSTED),
        ReviewAction("snooze", ReviewActionKind.DISMISS, ReviewResolution.DEFERRED),
    ),
    ReviewItemType.PACKAGING_REORDER: (
        ReviewAction(
            "approve_purchase",
            ReviewActionKind.RESOLVE,
            ReviewResolution.CONFIRMED_OK,
            requires_manager=True,
        ),
        ReviewAction("change_reorder_point", ReviewActionKind.RESOLVE, ReviewResolution.ADJUSTED),
        ReviewAction("cash_purchase", ReviewActionKind.RESOLVE, ReviewResolution.ADJUSTED),
    ),
    ReviewItemType.INSTALLMENT_OVERDUE: (
        ReviewAction("send_reminder", ReviewActionKind.NOTE),
        ReviewAction("extend_due_date", ReviewActionKind.RESOLVE, ReviewResolution.RESCHEDULED),
        ReviewAction(
            "activate_collateral_cheque",
            ReviewActionKind.RESOLVE,
            ReviewResolution.ADJUSTED,
            requires_manager=True,
        ),
    ),
    ReviewItemType.MANUAL_IMPORT_REVIEW: (
        ReviewAction("approve_preview", ReviewActionKind.RESOLVE, ReviewResolution.CONFIRMED_OK),
        ReviewAction("fix_rows_recalc", ReviewActionKind.RETRY),
        ReviewAction("cancel_batch", ReviewActionKind.DISMISS, ReviewResolution.SKIPPED),
    ),
    ReviewItemType.CASHBOX_VARIANCE: (
        ReviewAction(
            "approve_variance_entry",
            ReviewActionKind.RESOLVE,
            ReviewResolution.ADJUSTED,
            requires_manager=True,
        ),
        ReviewAction("recount", ReviewActionKind.RETRY),
        ReviewAction("escalate_to_manager", ReviewActionKind.ESCALATE, ReviewResolution.ESCALATED),
    ),
    ReviewItemType.INBOX_DEAD_LETTER: (
        ReviewAction("replay", ReviewActionKind.RETRY),
        ReviewAction("fix_source_retry", ReviewActionKind.RETRY),
        ReviewAction("close_with_reason", ReviewActionKind.DISMISS, ReviewResolution.SKIPPED),
    ),
    ReviewItemType.SMS_DELIVERY_FAILED: (
        ReviewAction("retry_reminder", ReviewActionKind.RETRY),
        ReviewAction("contact_customer", ReviewActionKind.NOTE),
        ReviewAction("close_with_reason", ReviewActionKind.DISMISS, ReviewResolution.SKIPPED),
    ),
    # The eleventh type (US-564, Phase 5-7 ۵خ W3) — not in the §7.2
    # table, which predates it (same as SMS_DELIVERY_FAILED). A
    # reminder is an in-app notification, not an exception: the
    # operator acknowledges it once acted on, snoozes it, or records
    # following up with the customer (cheque / installment subtypes).
    ReviewItemType.REMINDER: (
        ReviewAction("acknowledge", ReviewActionKind.RESOLVE, ReviewResolution.CONFIRMED_OK),
        ReviewAction("contact_customer", ReviewActionKind.NOTE),
        ReviewAction("snooze", ReviewActionKind.DISMISS, ReviewResolution.DEFERRED),
    ),
}


def permitted_actions(item_type: ReviewItemType) -> tuple[ReviewAction, ...]:
    """Return the permitted actions for a type (empty for unknown types)."""
    return PERMITTED_ACTIONS.get(item_type, ())


@dataclass(frozen=True)
class SlaPolicy:
    """Ageing/SLA policy for one item type (configurable per type).

    ``sla_hours`` is the deadline after filing; ``warn_hours`` is the
    window before the deadline in which the item shows a warning. Both
    are whole hours; ``warn_hours`` must be smaller than ``sla_hours``.
    """

    item_type: ReviewItemType
    sla_hours: int
    warn_hours: int
    updated_at: datetime.datetime | None = None


#: Skeleton defaults — every type has a policy even before an admin
#: configures one. These are starting values, not verified business
#: rules; the PATCH endpoint exists precisely so an admin can set the
#: real ones per installation.
DEFAULT_SLA_POLICIES: dict[ReviewItemType, SlaPolicy] = {
    ReviewItemType.MAPPING: SlaPolicy(ReviewItemType.MAPPING, sla_hours=48, warn_hours=12),
    ReviewItemType.SETTLEMENT_VARIANCE: SlaPolicy(ReviewItemType.SETTLEMENT_VARIANCE, sla_hours=72, warn_hours=24),
    ReviewItemType.CHEQUE_EXCEPTION: SlaPolicy(ReviewItemType.CHEQUE_EXCEPTION, sla_hours=24, warn_hours=6),
    ReviewItemType.BANK_UNMATCHED: SlaPolicy(ReviewItemType.BANK_UNMATCHED, sla_hours=72, warn_hours=24),
    ReviewItemType.PACKAGING_REORDER: SlaPolicy(ReviewItemType.PACKAGING_REORDER, sla_hours=168, warn_hours=48),
    ReviewItemType.INSTALLMENT_OVERDUE: SlaPolicy(ReviewItemType.INSTALLMENT_OVERDUE, sla_hours=48, warn_hours=12),
    ReviewItemType.MANUAL_IMPORT_REVIEW: SlaPolicy(ReviewItemType.MANUAL_IMPORT_REVIEW, sla_hours=48, warn_hours=12),
    ReviewItemType.CASHBOX_VARIANCE: SlaPolicy(ReviewItemType.CASHBOX_VARIANCE, sla_hours=24, warn_hours=6),
    ReviewItemType.INBOX_DEAD_LETTER: SlaPolicy(ReviewItemType.INBOX_DEAD_LETTER, sla_hours=24, warn_hours=6),
    ReviewItemType.SMS_DELIVERY_FAILED: SlaPolicy(ReviewItemType.SMS_DELIVERY_FAILED, sla_hours=24, warn_hours=6),
    ReviewItemType.REMINDER: SlaPolicy(ReviewItemType.REMINDER, sla_hours=24, warn_hours=6),
}


@dataclass(frozen=True)
class ReviewAuditEntry:
    """One audit entry on a review item (phased design §6.7/§7.2).

    Every action on every item leaves an entry: who, when, which
    action, which decision, with which policy/mapping version. The
    versions are None until F1's decision audit log supplies them.
    """

    id: str
    item_id: str
    actor: str
    action: str
    decision: str | None = None
    note: str | None = None
    policy_version: str | None = None
    mapping_version: str | None = None
    created_at: datetime.datetime | None = None


@dataclass(frozen=True)
class ReviewItem:
    """One uniform review-queue item (phased design §1.2 F4).

    ``age_hours`` / ``sla_deadline`` / ``sla_breached`` / ``sla_warn``
    are computed from ``created_at`` + the type's SLA policy at read
    time — they are never stored, so a policy change re-ages the queue
    immediately. ``audit`` is populated by the query service from the
    audit port; the list endpoint serves items without audit entries
    (the detail endpoint includes them).
    """

    id: str
    tenant_id: str
    item_type: ReviewItemType
    severity: ReviewSeverity
    status: ReviewItemStatus
    reason: str
    suggested_action: str
    created_at: datetime.datetime
    company_id: str | None = None
    subtype: str | None = None
    channel: str | None = None
    source_event_ref: str | None = None
    amount_irr: int | None = None
    assignee: str | None = None
    tags: tuple[str, ...] = ()
    resolution: ReviewResolution | None = None
    resolved_by: str | None = None
    resolved_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None
    audit: tuple[ReviewAuditEntry, ...] = ()

    @property
    def is_terminal(self) -> bool:
        """True once resolved or written off — no further action allowed."""
        return self.status in (ReviewItemStatus.RESOLVED, ReviewItemStatus.WRITTEN_OFF)

    def age_hours(self, now: datetime.datetime) -> float:
        """Age in hours (fractional) at ``now``."""
        return max(0.0, (now - self.created_at).total_seconds() / 3600.0)

    def sla_deadline(self, policy: SlaPolicy) -> datetime.datetime:
        """SLA deadline under ``policy`` (created_at + sla_hours)."""
        return self.created_at + datetime.timedelta(hours=policy.sla_hours)

    def sla_breached(self, now: datetime.datetime, policy: SlaPolicy) -> bool:
        """True when open/in-review and past the SLA deadline."""
        if self.is_terminal:
            return False
        return now > self.sla_deadline(policy)

    def sla_warn(self, now: datetime.datetime, policy: SlaPolicy) -> bool:
        """True when open/in-review, not breached, inside the warn window."""
        if self.is_terminal or self.sla_breached(now, policy):
            return False
        warn_start = self.sla_deadline(policy) - datetime.timedelta(hours=policy.warn_hours)
        return now >= warn_start


@dataclass(frozen=True)
class ReviewTypeCounter:
    """Per-type counters for the dashboard (phased design §7.3).

    All eleven types are always present in the summary — even with zero
    items — so the dashboard never has to guess which types exist.
    """

    item_type: ReviewItemType
    open_count: int
    in_review_count: int
    resolved_count: int
    overdue_count: int
    oldest_open_age_hours: float | None
    oldest_open_item_id: str | None

    @property
    def needs_attention_count(self) -> int:
        """Open + in-review: everything still waiting on a human."""
        return self.open_count + self.in_review_count


@dataclass(frozen=True)
class AdminNotification:
    """An admin notification for a critical item past its SLA.

    The skeleton surfaces notifications through the summary endpoint
    (the dashboard and the queue page render them as an alert); no
    delivery channel (email/SMS) exists yet — delivery lands with the
    unified panel (phase 5ح). ``overdue_hours`` is fractional.
    """

    item_id: str
    item_type: ReviewItemType
    severity: ReviewSeverity
    message: str
    overdue_hours: float
    created_at: datetime.datetime


@dataclass(frozen=True)
class ReviewQueueSummary:
    """Queue summary for the dashboard: counters + ageing + alerts."""

    counters: tuple[ReviewTypeCounter, ...]
    total_open: int
    total_in_review: int
    total_overdue: int
    critical_overdue_count: int
    oldest_open_age_hours: float | None
    oldest_open_item_id: str | None
    admin_notifications: tuple[AdminNotification, ...] = ()
