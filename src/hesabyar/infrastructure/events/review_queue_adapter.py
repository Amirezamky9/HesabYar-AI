"""Persistent review-queue adapter for the event pipeline (US-552, F4).

Replaces the F1 :class:`InboxReviewQueueAdapter
<hesabyar.infrastructure.events.defaults.InboxReviewQueueAdapter>`:
pipeline exceptions (mapping gaps, dead letters) now file real
``ReviewItem`` rows in the tenant's queue instead of leaving the
inbox row as the item. The queue is the one place a human sees every
exception — a mapping gap that only exists as an inbox row is
invisible there.
"""

from __future__ import annotations

from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.domain.events.models import ReviewItemDraft
from hesabyar.domain.review_queue.models import ReviewItemType, ReviewSeverity

#: The pipeline's severity strings are coarser than the queue's:
#: ``critical`` stays critical, ``high``/``warning`` map to WARNING,
#: ``info`` stays info. Anything unrecognised degrades to WARNING
#: rather than crashing the pipeline.
_SEVERITY_MAP = {
    "critical": ReviewSeverity.CRITICAL,
    "high": ReviewSeverity.WARNING,
    "warning": ReviewSeverity.WARNING,
    "info": ReviewSeverity.INFO,
}


class PersistentReviewQueueAdapter:
    """Enqueue pipeline exceptions as real review items (F4, US-552).

    Implements the events :class:`ReviewQueuePort
    <hesabyar.domain.events.ports.ReviewQueuePort>` through the
    review command service, so pipeline items are born with the same
    shape and the same ``filed`` audit entry as every other item.
    The subtype is the event type the item was filed for
    (``details["event_type"]``), when the pipeline supplied one.

    A tenantless draft (``tenant_id`` None — an event ingested before
    tenant scoping) cannot enter the tenant-scoped queue, so
    ``enqueue`` falls back to the F1 behaviour and returns the source
    event id as the review item id. An unknown item type is a
    pipeline bug, not a data problem, so it raises ``ValueError``
    loudly instead of filing a mis-typed item.
    """

    def __init__(self, commands: ReviewQueueCommandService) -> None:
        self._commands = commands

    def enqueue(self, item: ReviewItemDraft) -> str:
        """File the draft as a review item; return the item's id."""
        if item.tenant_id is None:
            return item.source_event_ref
        try:
            item_type = ReviewItemType(item.type)
        except ValueError:
            raise ValueError(f"Unknown review item type {item.type!r}.") from None
        severity = _SEVERITY_MAP.get(item.severity.lower(), ReviewSeverity.WARNING)
        subtype = item.details.get("event_type")
        filed = self._commands.file_item(
            tenant_id=item.tenant_id,
            item_type=item_type,
            severity=severity,
            reason=item.reason,
            suggested_action=item.suggested_action,
            subtype=subtype if isinstance(subtype, str) else None,
            source_event_ref=item.source_event_ref,
            amount_irr=item.amount_irr,
            actor="system",
        )
        return filed.id
