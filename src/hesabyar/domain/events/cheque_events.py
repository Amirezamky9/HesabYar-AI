"""Cheque event names approved by ADR 0004 (US-530, Phase 5-4 / ۵ث).

The two canonical names extend the nine §10.2 types to eleven; the
internal cycle names are dashboard-only and never canonical. This
module records the approved names as constants. US-533 wired the
canonical names into the ingest enum (``CanonicalEventType``), the
store-webhook mapping and the polling map, so they are ingestible;
internal names are still never ingestible (the ingest endpoint
rejects them with ``EVENT_UNKNOWN_TYPE``).

Canonical events travel in the 15-field envelope (§10.2) with the
event type alongside in the ``X-HesabYar-Event-Type`` header
(US-501 contract). Internal events never travel in an envelope,
never arrive in the header, and never enter the store inbox.
"""

from __future__ import annotations

#: Canonical cheque event types approved by ADR 0004 (beyond the
#: nine in spec §10.2). ``cheque.received.registered`` is intake
#: only — one ``account.check`` in state ``registered`` with NO
#: journal. ``cheque.received.cancelled`` voids the intake before
#: ``held`` and goes to the review queue after ``held``.
CHEQUE_CANONICAL_EVENT_TYPES: tuple[str, ...] = (
    "cheque.received.registered",
    "cheque.received.cancelled",
)

#: Internal cheque cycle events approved by ADR 0004 for dashboard
#: widgets, the cheque card timeline and review-queue filing only.
#: One per §5 §2 lifecycle transition. Non-canonical: never in a
#: §10.2 envelope, never in the ingest header, never in the store
#: inbox. Renaming an internal event needs no new ADR.
CHEQUE_INTERNAL_EVENT_TYPES: tuple[str, ...] = (
    "cheque.held",
    "cheque.in_clearing",
    "cheque.cleared",
    "cheque.bounced",
    "cheque.returned",
    "cheque.endorsed",
    "cheque.issued",
    "cheque.voided",
    "cheque.expired_unreceived",
    "cheque.rejected_mismatch",
    "cheque.called_in",
)
