"""Review-queue write use cases (US-504, Phase 5 F4; completed US-552).

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

Type-specific effects (US-552): the skeleton recorded only the
decision and the lifecycle change; the queue now EXECUTES the two
effects it owns, through the owning modules' services (never by
rebuilding them):

- ``replay`` / ``fix_source_retry`` (``inbox_dead_letter``) →
  :class:`DlqReplayService <hesabyar.application.commands.events.DlqReplayService>`
  replays the item's source event with the SAME idempotency key (no
  duplicate effect). An item without a source event cannot be
  replayed — the action fails loudly (422
  ``REVIEW_REPLAY_NOT_POSSIBLE``) instead of recording a replay that
  never happened.
- ``complete_mapping_retry`` (``mapping``) → when the reviewer
  supplies the completed mapping, it is LEARNED as a mapping-center
  suggestion (Suggest → Auto loop, §7.2) via
  :class:`MappingService <hesabyar.application.commands.integrations.MappingService>`;
  then the source event is reprocessed best-effort through the
  replay service (a ``needs_review`` event whose mapping gap is now
  fixed). The reprocess is best-effort — an already-processed or
  in-flight event is noted in the audit trail, not an error — while
  the mapping completion is the action's real effect.

Every other action stays a decision record: its effect belongs to
the owning module (the bank module matches lines and learns bank
rules, the SMS module resends, the import module recalculates, …),
several of which depend on this service — so the queue cannot call
back into them without a cycle. Resolving an item NEVER auto-posts
a journal: draft is the default (ADR 0003 decision 1) and posting
goes through the owning module's policy gate.

Every action appends an audit entry (who, when, which action, which
decision, with which mapping version when the action learned one):
no exception is ever resolved silently.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from dataclasses import dataclass
from decimal import Decimal

from hesabyar.application.commands.events import DlqReplayService
from hesabyar.application.commands.integrations import MappingService
from hesabyar.application.errors import (
    IntegrationMappingInvalidError,
    ReviewActionNotPermittedError,
    ReviewInvalidSlaPolicyError,
    ReviewInvalidStateError,
    ReviewItemNotFoundError,
    ReviewReplayNotPossibleError,
)
from hesabyar.domain.events.errors import EventNotFoundError, EventNotReprocessableError
from hesabyar.domain.integrations.models import MappingKind, MappingSuggestion
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


@dataclass(frozen=True)
class MappingLearningInput:
    """The completed mapping a reviewer supplies with ``complete_mapping_retry``.

    The reviewer completed the mapping the event was waiting on; the
    queue learns it as a mapping-center suggestion (Suggest → Auto
    loop, phased design §7.2) instead of letting the knowledge die
    with the item. ``mapping_kind`` is one of the nine §10.5 kinds;
    ``target_type`` is one of ``account`` / ``journal`` / ``party`` /
    ``tax_policy`` / ``dimension`` / ``company``; ``channel_key`` is
    ``""`` for an installation-wide mapping.
    """

    mapping_kind: str
    source_key: str
    target_type: str
    target_ref: str
    channel_key: str = ""


class ReviewQueueCommandService:
    """Write use cases for the review queue over the domain ports.

    ``replay`` (the DLQ replay service) and ``mapping`` (the mapping
    service) are the US-552 effect seams: production DI always wires
    both; services built without them (unit tests, other modules'
    filing-only use) can still file items and perform decision-only
    actions, but a replay action fails loudly instead of pretending
    to replay.
    """

    def __init__(
        self,
        queue: ReviewQueuePort,
        audit: ReviewAuditPort,
        *,
        replay: DlqReplayService | None = None,
        mapping: MappingService | None = None,
        now: datetime.datetime | None = None,
    ) -> None:
        self._queue = queue
        self._audit = audit
        self._replay = replay
        self._mapping = mapping
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
        installation_id: str | None = None,
        mapping: MappingLearningInput | None = None,
    ) -> ReviewItem:
        """Perform a permitted action on an item.

        Raises :class:`ReviewItemNotFoundError` for an unknown item,
        :class:`ReviewInvalidStateError` for a terminal item, and
        :class:`ReviewActionNotPermittedError` for an action outside
        the type's §7.2 list (or mapping details supplied with any
        action other than ``complete_mapping_retry``). A ``replay`` /
        ``fix_source_retry`` on an item without a source event (or
        without a wired replay service) raises
        :class:`ReviewReplayNotPossibleError`.

        The type-specific effects run BEFORE the lifecycle change,
        so a failed effect aborts the whole action in the caller's
        transaction — the item is never left half-acted-on. The
        audit entry records who, when, which action and which
        decision, plus the learned suggestion's id (as the mapping
        version) when the action learned a mapping, and the
        reprocess outcome in the note when one was attempted.
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
        if mapping is not None and action_code != "complete_mapping_retry":
            raise ReviewActionNotPermittedError(
                f"Mapping details are only accepted with the complete_mapping_retry action, not {action_code!r}.",
                details={"item_id": item_id, "item_type": item.item_type.value, "action": action_code},
            )
        now = self._clock()
        mapping_version: str | None = None
        effect_notes: list[str] = []
        if action_code == "complete_mapping_retry":
            if mapping is not None:
                suggestion = self._learn_mapping(
                    item, mapping, installation_id=installation_id, actor=actor
                )
                mapping_version = f"suggestion:{suggestion.id}"
            outcome = self._reprocess_source_event(item, actor=actor)
            if outcome is not None:
                effect_notes.append(outcome)
        elif action_code in ("replay", "fix_source_retry"):
            self._replay_source_event(item, actor=actor)
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
        audit_note = note
        if effect_notes:
            suffix = "[" + "; ".join(effect_notes) + "]"
            audit_note = f"{note} {suffix}" if note else suffix
        self._audit.record(
            ReviewAuditEntry(
                id=str(uuid.uuid4()),
                item_id=saved.id,
                actor=actor,
                action=action_code,
                decision=decision,
                note=audit_note,
                mapping_version=mapping_version,
                created_at=now,
            )
        )
        return saved

    def _learn_mapping(
        self,
        item: ReviewItem,
        mapping: MappingLearningInput,
        *,
        installation_id: str | None,
        actor: str,
    ) -> MappingSuggestion:
        """Learn a confirmed manual mapping as a mapping-center suggestion.

        The suggestion starts PENDING (one-click confirm in the
        mapping center creates the SUGGEST-mode mapping); the basis
        names the review item it was learned from, and the
        confidence is 1 — a human confirmed this mapping, it was not
        guessed. Re-confirming the same mapping is idempotent (the
        mapping service returns the existing pending suggestion).
        """
        if self._mapping is None:
            raise ReviewInvalidStateError(
                "Mapping learning is not configured for this service.",
                details={"item_id": item.id},
            )
        if installation_id is None:
            raise ReviewInvalidStateError(
                f"Learning a mapping from item {item.id!r} needs an installation context.",
                details={"item_id": item.id},
            )
        try:
            kind = MappingKind(mapping.mapping_kind)
        except ValueError:
            raise IntegrationMappingInvalidError(
                f"Unknown mapping_kind {mapping.mapping_kind!r}.",
                details={"mapping_kind": mapping.mapping_kind},
            ) from None
        return self._mapping.create_suggestion(
            installation_id,
            mapping_kind=kind,
            channel_key=mapping.channel_key,
            source_key=mapping.source_key,
            suggested_target_type=mapping.target_type,
            suggested_target_ref=mapping.target_ref,
            confidence=Decimal("1"),
            basis=f"review_queue:{item.id}",
        )

    def _replay_source_event(self, item: ReviewItem, *, actor: str) -> None:
        """Replay a dead-letter item's source event (strict DLQ path).

        The replay IS the action: without a source event (or without
        a wired replay service) there is nothing to do, and the
        action fails loudly instead of recording a replay that never
        happened. A missing event raises ``EventNotFoundError`` (404)
        and a not-dead-lettered one ``EventNotDeadLetteredError``
        (409) — both translate at the REST layer.
        """
        if item.source_event_ref is None:
            raise ReviewReplayNotPossibleError(
                f"Review item {item.id!r} has no source event to replay.",
                details={"item_id": item.id},
            )
        if self._replay is None:
            raise ReviewReplayNotPossibleError(
                "DLQ replay is not configured for this service.",
                details={"item_id": item.id},
            )
        self._replay.replay(item.source_event_ref, actor=actor, tenant_id=item.tenant_id)

    def _reprocess_source_event(self, item: ReviewItem, *, actor: str) -> str | None:
        """Reprocess a mapping item's source event, best-effort.

        Returns an audit-note fragment describing the outcome, or
        None when nothing was attempted. The mapping completion is
        the action's real effect; the reprocess is a convenience —
        an already-processed or in-flight event is noted, not an
        error, and a missing event (a stale reference) is noted too.
        """
        if item.source_event_ref is None:
            return "no source event attached; nothing reprocessed"
        if self._replay is None:
            return "replay service not wired; nothing reprocessed"
        try:
            self._replay.reprocess(item.source_event_ref, actor=actor, tenant_id=item.tenant_id)
        except EventNotFoundError:
            return f"source event {item.source_event_ref!r} not found; nothing reprocessed"
        except EventNotReprocessableError as exc:
            status = (exc.details or {}).get("status", "unknown")
            return f"source event not reprocessed (status: {status}); already done or in flight"
        return f"reprocessed source event {item.source_event_ref}"

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
