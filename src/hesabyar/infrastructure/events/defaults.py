"""Fail-closed default implementations of the event-platform ports (US-501).

These are the safe pre-configuration defaults the pipeline runs on
until the parallel Phase 5 streams land their production
implementations:

- :class:`DraftOnlyPolicyGate` — auto-post never allowed (F3 builds
  the registry-backed three-layer gate).
- :class:`FailClosedMappingResolver` / :class:`FailClosedDraftBuilder`
  — no mapping, no draft (F3 builds the mapping center). Every event
  routes to the review queue instead of an arbitrary account.
- :class:`NoWebhookSecretStore` — no secret configured (F3 builds
  the store registry that stores secrets).
- :class:`InboxReviewQueueAdapter` — the ``needs_review`` inbox row
  is the review item (F4 builds the ReviewItem-backed queue).
- :class:`InProcessOutboxPublisher` — dispatches outbox events to
  registered in-process handlers; with no handlers registered the
  event is published to zero consumers.

None of these is a stub that pretends to work: each one states
exactly what it does and what replaces it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from hesabyar.domain.common.money import MoneyIRR
from hesabyar.domain.events.errors import MappingUnresolvedError
from hesabyar.domain.events.models import (
    GateResult,
    NormalizedEvent,
    OutboxEvent,
    PolicyDecision,
    ResolvedMapping,
    ReviewItemDraft,
)
from hesabyar.domain.events.ports import EngineDocumentCommand


class DraftOnlyPolicyGate:
    """Policy gate that never auto-posts (F1 default; F3 replaces it).

    Draft is the default (ADR 0003 decision 1): until the
    installation explicitly opts in through the F3 policy gate, no
    event auto-posts — every draft waits for a human.
    """

    POLICY_VERSION = "draft-only-v1"

    def evaluate(
        self,
        event: NormalizedEvent,
        mapping: ResolvedMapping,
        draft_total: MoneyIRR,
    ) -> PolicyDecision:
        """Always draft: auto-post is not enabled in draft-only mode."""
        return PolicyDecision(
            allowed=False,
            policy_version=self.POLICY_VERSION,
            gate_results=(
                GateResult(
                    name="auto_post_enabled",
                    passed=False,
                    reason="Auto-post is not enabled: draft-only mode until the policy gate is configured (F3).",
                ),
            ),
            reason="Draft-only mode: the event stays a draft for a human.",
        )


class FailClosedMappingResolver:
    """Mapping resolver that always fails closed (F1 default; F3 replaces it).

    Without the mapping center there is no versioned mapping to
    resolve — so every event routes to the review queue (spec §10.5:
    unknown mapping must never fall back to an arbitrary account).
    """

    def resolve(self, event: NormalizedEvent) -> ResolvedMapping:
        """Raise: no mapping center is configured yet."""
        raise MappingUnresolvedError(
            "No mapping is configured for this event: the mapping center "
            "(F3) is not installed yet, so the event routes to review.",
            details={
                "event_type": event.event_type.value,
                "source_system": event.event.source_system,
                "source_store_id": event.event.source_store_id,
            },
        )


class FailClosedDraftBuilder:
    """Draft builder that always fails closed (F1 default; F3 replaces it).

    Unreachable in production — the fail-closed resolver raises
    before the builder runs — but the port needs a wired
    implementation, and failing loudly beats building a draft from
    no mapping.
    """

    def build(self, event: NormalizedEvent, mapping: ResolvedMapping) -> EngineDocumentCommand:
        """Raise: no draft builder is configured yet."""
        raise MappingUnresolvedError(
            "No draft builder is configured: the mapping center (F3) "
            "decides which accounts a draft uses.",
            details={"event_type": event.event_type.value},
        )


class NoWebhookSecretStore:
    """Webhook-secret lookup with no secrets configured (F1 default; F3 replaces it).

    Returns None for every store: signature verification is skipped
    and the skip is recorded in the ingest result — never silently
    assumed verified.
    """

    def get_secret(
        self,
        source_system: str,
        source_store_id: str,
        installation_id: str | None = None,
    ) -> str | None:
        """No secret is configured for any store yet."""
        return None


class InboxReviewQueueAdapter:
    """Review queue backed by the inbox row itself (F1 default; F4 replaces it).

    The pipeline already records the routing decision in the
    automation audit log and marks the inbox row ``needs_review``;
    the inbox row IS the review item until F4 persists ``ReviewItem``
    rows. ``enqueue`` therefore returns the source event's id as
    the review item id.
    """

    def enqueue(self, item: ReviewItemDraft) -> str:
        """Return the source event id as the review item id."""
        return item.source_event_ref


class InProcessOutboxPublisher:
    """Outbox publisher dispatching to registered in-process handlers.

    Handlers are callables taking the outbox event and returning True
    when they handled it. An event type with no handlers is published
    to zero consumers (returns True): the outbox is the durable
    record, and consumers subscribe as modules land.
    """

    def __init__(self, handlers: Mapping[str, Callable[[OutboxEvent], bool]] | None = None) -> None:
        self._handlers: dict[str, Callable[[OutboxEvent], bool]] = dict(handlers or {})

    def register(self, event_type: str, handler: Callable[[OutboxEvent], bool]) -> None:
        """Register a handler for one outbox event type."""
        self._handlers[event_type] = handler

    def publish(self, event: OutboxEvent) -> bool:
        """Publish one outbox event to its handler (or to zero consumers)."""
        handler = self._handlers.get(event.event_type)
        if handler is None:
            return True
        return bool(handler(event))
