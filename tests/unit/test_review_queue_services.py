"""Unit tests for the review-queue services (US-504, Phase 5 F4; US-552).

No database: the ports are faked in memory, and the clock is fixed, so
ageing/SLA math is deterministic. The REST tests
(``tests/interfaces/test_rest_review_queue.py``) cover the real
repository + HTTP layer against PostgreSQL.

US-552 wires the command service to the real DLQ replay service and
the real mapping service (both over their in-memory port fakes), so
replay and mapping learning are tested through the services that
actually execute them — not through mocks of the seam.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from decimal import Decimal

import pytest

from hesabyar.application.commands.events import DlqReplayService
from hesabyar.application.commands.integrations import MappingService
from hesabyar.application.commands.review_queue import MappingLearningInput, ReviewQueueCommandService
from hesabyar.application.errors import (
    IntegrationMappingInvalidError,
    ReviewActionNotPermittedError,
    ReviewInvalidSlaPolicyError,
    ReviewInvalidStateError,
    ReviewItemNotFoundError,
    ReviewReplayNotPossibleError,
)
from hesabyar.application.queries.review_queue import ReviewQueueQueryService
from hesabyar.domain.events.errors import EventNotDeadLetteredError, EventNotFoundError
from hesabyar.domain.events.models import InboxEventStatus, ReviewItemDraft
from hesabyar.domain.review_queue.models import (
    DEFAULT_SLA_POLICIES,
    PERMITTED_ACTIONS,
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
from hesabyar.domain.review_queue.ports import (
    ReviewAuditPort,
    ReviewItemFilters,
    ReviewQueuePort,
)
from hesabyar.domain.review_queue.suggestions import derive_suggestion
from hesabyar.infrastructure.events.in_memory_queue import InMemoryJobQueue
from hesabyar.infrastructure.events.review_queue_adapter import PersistentReviewQueueAdapter
from tests.event_fixtures import INSTALLATION_ID, FakeAuditLog, FakeInboxRepository, make_inbox_event
from tests.unit.test_integrations_services import FakeIntegrationsRepository

NOW = datetime.datetime(2026, 11, 18, 12, 0, tzinfo=datetime.UTC)
TENANT = "tenant-504"


class InMemoryReviewQueue(ReviewQueuePort):
    """In-memory port fake: stored-column filters only, like the real repo."""

    def __init__(self) -> None:
        self.items: dict[str, ReviewItem] = {}
        self.policies: dict[ReviewItemType, SlaPolicy] = {}

    def list_items(self, tenant_id: str, filters: ReviewItemFilters) -> Sequence[ReviewItem]:
        out = [item for item in self.items.values() if item.tenant_id == tenant_id]
        if filters.item_type is not None:
            out = [item for item in out if item.item_type == filters.item_type]
        if filters.severity is not None:
            out = [item for item in out if item.severity == filters.severity]
        if filters.status is not None:
            out = [item for item in out if item.status == filters.status]
        if filters.channel is not None:
            out = [item for item in out if item.channel == filters.channel]
        if filters.company_id is not None:
            out = [item for item in out if item.company_id == filters.company_id]
        if filters.assignee is not None:
            out = [item for item in out if item.assignee == filters.assignee]
        if filters.min_amount_irr is not None:
            out = [item for item in out if item.amount_irr is not None and item.amount_irr >= filters.min_amount_irr]
        if filters.max_amount_irr is not None:
            out = [item for item in out if item.amount_irr is not None and item.amount_irr <= filters.max_amount_irr]
        return out

    def get_item(self, item_id: str, tenant_id: str) -> ReviewItem | None:
        item = self.items.get(item_id)
        return item if item is not None and item.tenant_id == tenant_id else None

    def save_item(self, item: ReviewItem) -> ReviewItem:
        self.items[item.id] = item
        return item

    def update_item(self, item: ReviewItem) -> ReviewItem:
        self.items[item.id] = item
        return item

    def list_policies(self, tenant_id: str) -> Sequence[SlaPolicy]:
        return list(self.policies.values())

    def upsert_policy(self, tenant_id: str, policy: SlaPolicy) -> SlaPolicy:
        self.policies[policy.item_type] = policy
        return policy


class InMemoryReviewAudit(ReviewAuditPort):
    """In-memory audit fake: append-only, oldest first."""

    def __init__(self) -> None:
        self.entries: list[ReviewAuditEntry] = []

    def record(self, entry: ReviewAuditEntry) -> ReviewAuditEntry:
        self.entries.append(entry)
        return entry

    def list_for_item(self, item_id: str, tenant_id: str) -> Sequence[ReviewAuditEntry]:
        return [entry for entry in self.entries if entry.item_id == item_id]


@pytest.fixture()
def world() -> tuple[InMemoryReviewQueue, InMemoryReviewAudit, ReviewQueueQueryService, ReviewQueueCommandService]:
    """Fresh ports + services with a fixed clock (effect seams unwired)."""
    queue = InMemoryReviewQueue()
    audit = InMemoryReviewAudit()
    query = ReviewQueueQueryService(queue, audit, now=NOW)
    cmd = ReviewQueueCommandService(queue, audit, now=NOW)
    return queue, audit, query, cmd


@pytest.fixture()
def wired_world() -> tuple[
    InMemoryReviewQueue,
    InMemoryReviewAudit,
    FakeInboxRepository,
    FakeIntegrationsRepository,
    DlqReplayService,
    MappingService,
    ReviewQueueCommandService,
]:
    """Fresh ports + services with the US-552 effect seams wired.

    The replay service and the mapping service are the REAL services
    over in-memory port fakes, so a replay really resets an inbox
    event and a learning action really creates a suggestion.
    """
    queue = InMemoryReviewQueue()
    audit = InMemoryReviewAudit()
    inbox = FakeInboxRepository()
    registry = FakeIntegrationsRepository()
    replay = DlqReplayService(inbox, FakeAuditLog(), InMemoryJobQueue())
    mapping = MappingService(registry)
    cmd = ReviewQueueCommandService(queue, audit, replay=replay, mapping=mapping, now=NOW)
    return queue, audit, inbox, registry, replay, mapping, cmd


def _learning_input(**overrides: str) -> MappingLearningInput:
    """A complete mapping-learning payload (overrides per test)."""
    return MappingLearningInput(
        mapping_kind=overrides.get("mapping_kind", "gateway_journal"),
        source_key=overrides.get("source_key", "zarinpal"),
        target_type=overrides.get("target_type", "journal"),
        target_ref=overrides.get("target_ref", "journal-1"),
        channel_key=overrides.get("channel_key", ""),
    )


def _item(
    item_id: str,
    *,
    item_type: ReviewItemType = ReviewItemType.MAPPING,
    severity: ReviewSeverity = ReviewSeverity.WARNING,
    status: ReviewItemStatus = ReviewItemStatus.OPEN,
    age_hours: float = 1.0,
    amount_irr: int | None = None,
    channel: str | None = None,
    company_id: str | None = None,
    subtype: str | None = None,
    source_event_ref: str | None = None,
    tenant_id: str = TENANT,
) -> ReviewItem:
    created = NOW - datetime.timedelta(hours=age_hours)
    return ReviewItem(
        id=item_id,
        tenant_id=tenant_id,
        item_type=item_type,
        severity=severity,
        status=status,
        reason="reason",
        suggested_action="suggested action",
        created_at=created,
        company_id=company_id,
        subtype=subtype,
        channel=channel,
        source_event_ref=source_event_ref,
        amount_irr=amount_irr,
        updated_at=created,
    )


# ---------------------------------------------------------------------------
# Domain: the eleven types, permitted actions, SLA defaults
# ---------------------------------------------------------------------------


def test_eleven_item_types_exist():
    assert len(ReviewItemType) == 11
    assert {t.value for t in ReviewItemType} == {
        "mapping",
        "settlement_variance",
        "cheque_exception",
        "bank_unmatched",
        "packaging_reorder",
        "installment_overdue",
        "manual_import_review",
        "cashbox_variance",
        "inbox_dead_letter",
        "sms_delivery_failed",
        "reminder",
    }


def test_every_type_has_permitted_actions():
    for item_type in ReviewItemType:
        actions = permitted_actions(item_type)
        assert len(actions) >= 1, f"{item_type} has no permitted actions"
        codes = [action.code for action in actions]
        assert len(codes) == len(set(codes)), f"{item_type} has duplicate action codes"


def test_resolve_and_dismiss_actions_carry_a_resolution():
    for item_type, actions in PERMITTED_ACTIONS.items():
        for action in actions:
            if action.kind in (ReviewActionKind.RESOLVE, ReviewActionKind.DISMISS):
                assert action.resolution is not None, f"{item_type}/{action.code} closes without a resolution"


def test_sla_defaults_cover_all_types_with_valid_windows():
    assert set(DEFAULT_SLA_POLICIES) == set(ReviewItemType)
    for item_type, policy in DEFAULT_SLA_POLICIES.items():
        assert 0 < policy.warn_hours < policy.sla_hours, f"{item_type} has an invalid default window"


def test_age_and_sla_math():
    item = _item("a", item_type=ReviewItemType.CHEQUE_EXCEPTION, age_hours=30.0)
    policy = DEFAULT_SLA_POLICIES[ReviewItemType.CHEQUE_EXCEPTION]  # 24h SLA, 6h warn
    assert item.age_hours(NOW) == pytest.approx(30.0)
    assert item.sla_deadline(policy) == item.created_at + datetime.timedelta(hours=24)
    assert item.sla_breached(NOW, policy) is True
    assert item.sla_warn(NOW, policy) is False  # breached items do not warn


def test_warn_window_before_deadline():
    item = _item("a", item_type=ReviewItemType.CHEQUE_EXCEPTION, age_hours=20.0)
    policy = DEFAULT_SLA_POLICIES[ReviewItemType.CHEQUE_EXCEPTION]
    assert item.sla_breached(NOW, policy) is False
    assert item.sla_warn(NOW, policy) is True  # 4h left, warn window is 6h


def test_terminal_items_never_breach():
    item = _item(
        "a",
        item_type=ReviewItemType.CHEQUE_EXCEPTION,
        status=ReviewItemStatus.RESOLVED,
        age_hours=100.0,
    )
    policy = DEFAULT_SLA_POLICIES[ReviewItemType.CHEQUE_EXCEPTION]
    assert item.sla_breached(NOW, policy) is False
    assert item.sla_warn(NOW, policy) is False


# ---------------------------------------------------------------------------
# Query service: default sort, filters, summary
# ---------------------------------------------------------------------------


def test_default_sort_is_critical_first_then_oldest(world):
    queue, _audit, query, _cmd = world
    queue.save_item(_item("info-old", severity=ReviewSeverity.INFO, age_hours=100))
    queue.save_item(_item("warn-mid", severity=ReviewSeverity.WARNING, age_hours=50))
    queue.save_item(_item("crit-new", severity=ReviewSeverity.CRITICAL, age_hours=1))
    queue.save_item(_item("crit-old", severity=ReviewSeverity.CRITICAL, age_hours=10))
    queue.save_item(_item("warn-old", severity=ReviewSeverity.WARNING, age_hours=90))
    ids = [view.item.id for view in query.list_items(TENANT, ReviewItemFilters())]
    assert ids == ["crit-old", "crit-new", "warn-old", "warn-mid", "info-old"]


def test_list_filters_type_severity_channel_amount(world):
    queue, _audit, query, _cmd = world
    queue.save_item(
        _item(
            "a",
            item_type=ReviewItemType.BANK_UNMATCHED,
            severity=ReviewSeverity.CRITICAL,
            channel="melli",
            amount_irr=5000,
        )
    )
    queue.save_item(
        _item(
            "b",
            item_type=ReviewItemType.BANK_UNMATCHED,
            severity=ReviewSeverity.INFO,
            channel="zarinpal",
            amount_irr=100,
        )
    )
    queue.save_item(_item("c", item_type=ReviewItemType.MAPPING, channel="melli", amount_irr=200))

    bank = ReviewItemFilters(item_type=ReviewItemType.BANK_UNMATCHED)
    assert [v.item.id for v in query.list_items(TENANT, bank)] == ["a", "b"]
    critical = ReviewItemFilters(severity=ReviewSeverity.CRITICAL)
    assert [v.item.id for v in query.list_items(TENANT, critical)] == ["a"]
    assert [v.item.id for v in query.list_items(TENANT, ReviewItemFilters(channel="melli"))] == ["a", "c"]
    assert [v.item.id for v in query.list_items(TENANT, ReviewItemFilters(min_amount_irr=1000))] == ["a"]
    assert [v.item.id for v in query.list_items(TENANT, ReviewItemFilters(max_amount_irr=150))] == ["b"]


def test_list_filters_age_and_overdue(world):
    queue, _audit, query, _cmd = world
    # Cheque SLA is 24h: 30h old is overdue, 2h old is not.
    queue.save_item(_item("old", item_type=ReviewItemType.CHEQUE_EXCEPTION, age_hours=30))
    queue.save_item(_item("new", item_type=ReviewItemType.CHEQUE_EXCEPTION, age_hours=2))
    assert [v.item.id for v in query.list_items(TENANT, ReviewItemFilters(min_age_hours=10))] == ["old"]
    assert [v.item.id for v in query.list_items(TENANT, ReviewItemFilters(max_age_hours=10))] == ["new"]
    assert [v.item.id for v in query.list_items(TENANT, ReviewItemFilters(overdue_only=True))] == ["old"]


def test_list_is_tenant_scoped(world):
    queue, _audit, query, _cmd = world
    queue.save_item(_item("mine"))
    queue.save_item(_item("theirs", tenant_id="other-tenant"))
    assert [v.item.id for v in query.list_items(TENANT, ReviewItemFilters())] == ["mine"]


def test_get_item_unknown_raises(world):
    _queue, _audit, query, _cmd = world
    with pytest.raises(ReviewItemNotFoundError):
        query.get_item("missing", TENANT)


def test_get_item_carries_audit_trail(world):
    queue, audit, query, _cmd = world
    queue.save_item(_item("a"))
    audit.record(ReviewAuditEntry(id="e1", item_id="a", actor="alice", action="filed", created_at=NOW))
    view = query.get_item("a", TENANT)
    assert [entry.action for entry in view.item.audit] == ["filed"]
    assert view.age_hours == pytest.approx(1.0)
    assert view.sla_breached is False


def test_summary_covers_all_eleven_types_even_empty(world):
    _queue, _audit, query, _cmd = world
    summary = query.summary(TENANT)
    assert len(summary.counters) == 11
    assert {counter.item_type for counter in summary.counters} == set(ReviewItemType)
    assert summary.total_open == 0
    assert summary.total_overdue == 0
    assert summary.critical_overdue_count == 0
    assert summary.oldest_open_age_hours is None
    assert summary.admin_notifications == ()


def test_summary_counts_overdue_and_notifies_only_critical(world):
    queue, _audit, query, _cmd = world
    # Cheque SLA 24h: critical 30h old is overdue + notifiable; warning 30h old is overdue only.
    queue.save_item(
        _item("crit", item_type=ReviewItemType.CHEQUE_EXCEPTION, severity=ReviewSeverity.CRITICAL, age_hours=30)
    )
    queue.save_item(
        _item("warn", item_type=ReviewItemType.CHEQUE_EXCEPTION, severity=ReviewSeverity.WARNING, age_hours=30)
    )
    queue.save_item(_item("fresh", item_type=ReviewItemType.MAPPING, age_hours=1))
    summary = query.summary(TENANT)
    assert summary.total_open == 3
    assert summary.total_overdue == 2
    assert summary.critical_overdue_count == 1
    assert len(summary.admin_notifications) == 1
    note = summary.admin_notifications[0]
    assert note.item_id == "crit"
    assert note.severity is ReviewSeverity.CRITICAL
    assert note.overdue_hours == pytest.approx(6.0)
    cheque = next(c for c in summary.counters if c.item_type is ReviewItemType.CHEQUE_EXCEPTION)
    assert cheque.open_count == 2
    assert cheque.overdue_count == 2
    assert cheque.oldest_open_age_hours == pytest.approx(30.0)
    assert summary.oldest_open_age_hours == pytest.approx(30.0)


def test_summary_respects_company_filter(world):
    queue, _audit, query, _cmd = world
    queue.save_item(_item("a", company_id="co-1"))
    queue.save_item(_item("b", company_id="co-2"))
    assert query.summary(TENANT, company_id="co-1").total_open == 1
    assert query.summary(TENANT).total_open == 2


def test_sla_policies_default_then_configured(world):
    queue, _audit, query, cmd = world
    defaults = {policy.item_type: policy for policy in query.sla_policies(TENANT)}
    assert defaults[ReviewItemType.MAPPING].sla_hours == 48
    assert defaults[ReviewItemType.MAPPING].updated_at is None
    cmd.update_sla_policy(tenant_id=TENANT, item_type=ReviewItemType.MAPPING, sla_hours=12, warn_hours=3)
    configured = {policy.item_type: policy for policy in query.sla_policies(TENANT)}
    assert configured[ReviewItemType.MAPPING].sla_hours == 12
    assert configured[ReviewItemType.MAPPING].warn_hours == 3
    assert configured[ReviewItemType.MAPPING].updated_at == NOW
    # Other types keep their defaults.
    assert configured[ReviewItemType.CHEQUE_EXCEPTION].sla_hours == 24


# ---------------------------------------------------------------------------
# Command service: filing, actions, assignment, SLA updates
# ---------------------------------------------------------------------------


def test_file_item_creates_open_item_with_filed_entry(world):
    _queue, audit, _query, cmd = world
    item = cmd.file_item(
        tenant_id=TENANT,
        item_type=ReviewItemType.BANK_UNMATCHED,
        severity=ReviewSeverity.WARNING,
        reason="سطر بانکی بی‌همتا",
        suggested_action="نگاشت دستی",
        channel="melli",
        amount_irr=1_500_000,
        actor="api-client",
    )
    assert item.status is ReviewItemStatus.OPEN
    assert item.channel == "melli"
    assert item.amount_irr == 1_500_000
    assert [entry.action for entry in audit.list_for_item(item.id, TENANT)] == ["filed"]


def test_resolve_action_closes_with_resolution(world):
    _queue, audit, query, cmd = world
    item = cmd.file_item(
        tenant_id=TENANT,
        item_type=ReviewItemType.MAPPING,
        severity=ReviewSeverity.WARNING,
        reason="r",
        suggested_action="s",
        actor="api-client",
    )
    closed = cmd.perform_action(
        item_id=item.id, tenant_id=TENANT, action_code="auto_create_party_item", actor="accountant", note="done"
    )
    assert closed.status is ReviewItemStatus.RESOLVED
    assert closed.resolution is ReviewResolution.ADJUSTED
    assert closed.resolved_by == "accountant"
    assert closed.resolved_at == NOW
    entries = audit.list_for_item(item.id, TENANT)
    assert [entry.action for entry in entries] == ["filed", "auto_create_party_item"]
    assert entries[1].decision == "adjusted"
    assert entries[1].note == "done"
    # The detail view carries the trail.
    assert len(query.get_item(item.id, TENANT).item.audit) == 2


def test_retry_action_moves_to_in_review_and_stays_open(wired_world):
    _queue, _audit, inbox, _registry, _replay, _mapping, cmd = wired_world
    event = make_inbox_event(status=InboxEventStatus.DEAD_LETTERED, tenant_id=TENANT, attempts=5)
    inbox.events[event.id] = event
    item = cmd.file_item(
        tenant_id=TENANT,
        item_type=ReviewItemType.INBOX_DEAD_LETTER,
        severity=ReviewSeverity.CRITICAL,
        reason="r",
        suggested_action="s",
        source_event_ref=event.id,
        actor="api-client",
    )
    moved = cmd.perform_action(item_id=item.id, tenant_id=TENANT, action_code="replay", actor="operator")
    assert moved.status is ReviewItemStatus.IN_REVIEW
    assert moved.resolution is None
    assert moved.resolved_at is None
    # US-552: the replay really happened — the event is back to
    # received with a fresh retry budget and the SAME idempotency key.
    replayed = inbox.events[event.id]
    assert replayed.status is InboxEventStatus.RECEIVED
    assert replayed.attempts == 0
    assert replayed.idempotency_key == event.idempotency_key


def test_note_action_moves_to_in_review(world):
    _queue, _audit, _query, cmd = world
    item = cmd.file_item(
        tenant_id=TENANT,
        item_type=ReviewItemType.CHEQUE_EXCEPTION,
        severity=ReviewSeverity.WARNING,
        reason="r",
        suggested_action="s",
        actor="api-client",
    )
    moved = cmd.perform_action(item_id=item.id, tenant_id=TENANT, action_code="contact_customer", actor="accountant")
    assert moved.status is ReviewItemStatus.IN_REVIEW
    assert moved.resolution is None


def test_dismiss_action_closes_as_skipped(world):
    _queue, _audit, _query, cmd = world
    item = cmd.file_item(
        tenant_id=TENANT,
        item_type=ReviewItemType.MAPPING,
        severity=ReviewSeverity.INFO,
        reason="r",
        suggested_action="s",
        actor="api-client",
    )
    closed = cmd.perform_action(item_id=item.id, tenant_id=TENANT, action_code="skip", actor="accountant")
    assert closed.status is ReviewItemStatus.RESOLVED
    assert closed.resolution is ReviewResolution.SKIPPED


def test_action_of_another_type_is_not_permitted(world):
    _queue, _audit, _query, cmd = world
    item = cmd.file_item(
        tenant_id=TENANT,
        item_type=ReviewItemType.MAPPING,
        severity=ReviewSeverity.WARNING,
        reason="r",
        suggested_action="s",
        actor="api-client",
    )
    with pytest.raises(ReviewActionNotPermittedError):
        cmd.perform_action(item_id=item.id, tenant_id=TENANT, action_code="replay", actor="accountant")


def test_unknown_action_is_not_permitted(world):
    _queue, _audit, _query, cmd = world
    item = cmd.file_item(
        tenant_id=TENANT,
        item_type=ReviewItemType.MAPPING,
        severity=ReviewSeverity.WARNING,
        reason="r",
        suggested_action="s",
        actor="api-client",
    )
    with pytest.raises(ReviewActionNotPermittedError):
        cmd.perform_action(item_id=item.id, tenant_id=TENANT, action_code="explode", actor="accountant")


def test_action_on_terminal_item_is_rejected(world):
    _queue, _audit, _query, cmd = world
    item = cmd.file_item(
        tenant_id=TENANT,
        item_type=ReviewItemType.MAPPING,
        severity=ReviewSeverity.WARNING,
        reason="r",
        suggested_action="s",
        actor="api-client",
    )
    cmd.perform_action(item_id=item.id, tenant_id=TENANT, action_code="skip", actor="accountant")
    with pytest.raises(ReviewInvalidStateError):
        cmd.perform_action(item_id=item.id, tenant_id=TENANT, action_code="skip", actor="accountant")


def test_action_on_unknown_item_raises(world):
    _queue, _audit, _query, cmd = world
    with pytest.raises(ReviewItemNotFoundError):
        cmd.perform_action(item_id="missing", tenant_id=TENANT, action_code="skip", actor="accountant")


def test_assign_item_records_audit(world):
    _queue, audit, _query, cmd = world
    item = cmd.file_item(
        tenant_id=TENANT,
        item_type=ReviewItemType.MAPPING,
        severity=ReviewSeverity.WARNING,
        reason="r",
        suggested_action="s",
        actor="api-client",
    )
    assigned = cmd.assign_item(item_id=item.id, tenant_id=TENANT, assignee="accountant-1", actor="manager")
    assert assigned.assignee == "accountant-1"
    assert [entry.action for entry in audit.list_for_item(item.id, TENANT)] == ["filed", "assign"]
    unassigned = cmd.assign_item(item_id=item.id, tenant_id=TENANT, assignee=None, actor="manager")
    assert unassigned.assignee is None


def test_assign_terminal_item_is_rejected(world):
    _queue, _audit, _query, cmd = world
    item = cmd.file_item(
        tenant_id=TENANT,
        item_type=ReviewItemType.MAPPING,
        severity=ReviewSeverity.WARNING,
        reason="r",
        suggested_action="s",
        actor="api-client",
    )
    cmd.perform_action(item_id=item.id, tenant_id=TENANT, action_code="skip", actor="accountant")
    with pytest.raises(ReviewInvalidStateError):
        cmd.assign_item(item_id=item.id, tenant_id=TENANT, assignee="someone", actor="manager")


def test_update_sla_policy_rejects_invalid_windows(world):
    _queue, _audit, _query, cmd = world
    with pytest.raises(ReviewInvalidSlaPolicyError):
        cmd.update_sla_policy(tenant_id=TENANT, item_type=ReviewItemType.MAPPING, sla_hours=12, warn_hours=12)
    with pytest.raises(ReviewInvalidSlaPolicyError):
        cmd.update_sla_policy(tenant_id=TENANT, item_type=ReviewItemType.MAPPING, sla_hours=12, warn_hours=13)
    with pytest.raises(ReviewInvalidSlaPolicyError):
        cmd.update_sla_policy(tenant_id=TENANT, item_type=ReviewItemType.MAPPING, sla_hours=0, warn_hours=1)
    with pytest.raises(ReviewInvalidSlaPolicyError):
        cmd.update_sla_policy(tenant_id=TENANT, item_type=ReviewItemType.MAPPING, sla_hours=12, warn_hours=0)


# ---------------------------------------------------------------------------
# US-552: derived suggestions (type + payload, never hard-coded per item)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("item_type", "severity", "subtype", "action", "reason_key"),
    [
        (ReviewItemType.MAPPING, ReviewSeverity.WARNING, None, "complete_mapping_retry", "mapping_blocks_event"),
        (
            ReviewItemType.SETTLEMENT_VARIANCE,
            ReviewSeverity.WARNING,
            "fee_mismatch",
            "fix_fee_mapping",
            "variance_is_fee_mismatch",
        ),
        (
            ReviewItemType.SETTLEMENT_VARIANCE,
            ReviewSeverity.CRITICAL,
            "amount_mismatch",
            "escalate_to_gateway",
            "large_variance_needs_gateway",
        ),
        (
            ReviewItemType.SETTLEMENT_VARIANCE,
            ReviewSeverity.WARNING,
            "amount_mismatch",
            "accept_variance",
            "small_variance_accept",
        ),
        (
            ReviewItemType.CHEQUE_EXCEPTION,
            ReviewSeverity.WARNING,
            "cheque_bounced",
            "record_bounce",
            "cheque_bounced",
        ),
        (
            ReviewItemType.CHEQUE_EXCEPTION,
            ReviewSeverity.WARNING,
            "issued_cheque_bounced",
            "record_bounce",
            "cheque_bounced",
        ),
        (
            ReviewItemType.CHEQUE_EXCEPTION,
            ReviewSeverity.WARNING,
            "stale_in_clearing",
            "contact_customer",
            "cheque_needs_customer",
        ),
        (ReviewItemType.BANK_UNMATCHED, ReviewSeverity.WARNING, None, "manual_match", "line_needs_match"),
        (
            ReviewItemType.PACKAGING_REORDER,
            ReviewSeverity.WARNING,
            None,
            "approve_purchase",
            "stock_below_reorder",
        ),
        (
            ReviewItemType.INSTALLMENT_OVERDUE,
            ReviewSeverity.WARNING,
            None,
            "send_reminder",
            "installment_overdue_remind",
        ),
        (
            ReviewItemType.MANUAL_IMPORT_REVIEW,
            ReviewSeverity.WARNING,
            None,
            "approve_preview",
            "batch_preview_ready",
        ),
        (
            ReviewItemType.CASHBOX_VARIANCE,
            ReviewSeverity.CRITICAL,
            None,
            "escalate_to_manager",
            "large_variance_needs_manager",
        ),
        (
            ReviewItemType.CASHBOX_VARIANCE,
            ReviewSeverity.WARNING,
            None,
            "approve_variance_entry",
            "variance_needs_approval",
        ),
        (ReviewItemType.INBOX_DEAD_LETTER, ReviewSeverity.CRITICAL, None, "replay", "event_dead_lettered"),
        (
            ReviewItemType.SMS_DELIVERY_FAILED,
            ReviewSeverity.WARNING,
            None,
            "retry_reminder",
            "sms_send_failed",
        ),
    ],
)
def test_derive_suggestion_rules(item_type, severity, subtype, action, reason_key):
    suggestion = derive_suggestion(_item("a", item_type=item_type, severity=severity, subtype=subtype))
    assert suggestion.action_code == action
    assert suggestion.reason_key == reason_key


def test_derive_suggestion_is_always_a_permitted_action():
    """The derived action must be one the operator can actually perform."""
    subtypes = [
        None,
        "fee_mismatch",
        "amount_mismatch",
        "cheque_bounced",
        "issued_cheque_bounced",
        "stale_in_clearing",
        "duplicate_sayad_id",
        "unknown_subtype",
    ]
    for item_type in ReviewItemType:
        permitted = {action.code for action in permitted_actions(item_type)}
        for severity in ReviewSeverity:
            for subtype in subtypes:
                suggestion = derive_suggestion(
                    _item("a", item_type=item_type, severity=severity, subtype=subtype)
                )
                assert suggestion.action_code in permitted, (
                    f"{item_type.value}/{severity.value}/{subtype} suggests "
                    f"{suggestion.action_code}, which is not permitted"
                )


def test_view_carries_derived_suggestion(world):
    queue, _audit, query, _cmd = world
    queue.save_item(_item("a", item_type=ReviewItemType.MAPPING))
    queue.save_item(
        _item(
            "b",
            item_type=ReviewItemType.SETTLEMENT_VARIANCE,
            severity=ReviewSeverity.CRITICAL,
            subtype="amount_mismatch",
        )
    )
    assert query.get_item("a", TENANT).suggested_action_code == "complete_mapping_retry"
    assert query.get_item("a", TENANT).suggested_reason_key == "mapping_blocks_event"
    assert query.get_item("b", TENANT).suggested_action_code == "escalate_to_gateway"
    assert query.get_item("b", TENANT).suggested_reason_key == "large_variance_needs_gateway"
    listed = {view.item.id: view for view in query.list_items(TENANT, ReviewItemFilters())}
    assert listed["a"].suggested_action_code == "complete_mapping_retry"
    assert listed["b"].suggested_action_code == "escalate_to_gateway"


# ---------------------------------------------------------------------------
# US-552: DLQ replay through the review queue
# ---------------------------------------------------------------------------


def test_replay_without_source_event_fails_loudly(world):
    """No source event → the action fails instead of recording a replay that never happened."""
    queue, _audit, _query, cmd = world
    item = cmd.file_item(
        tenant_id=TENANT,
        item_type=ReviewItemType.INBOX_DEAD_LETTER,
        severity=ReviewSeverity.CRITICAL,
        reason="r",
        suggested_action="s",
        actor="api-client",
    )
    with pytest.raises(ReviewReplayNotPossibleError):
        cmd.perform_action(item_id=item.id, tenant_id=TENANT, action_code="replay", actor="operator")
    assert queue.get_item(item.id, TENANT).status is ReviewItemStatus.OPEN


def test_replay_without_wired_service_fails_loudly(world):
    """A service built without the replay seam cannot replay, even with a source event."""
    queue, _audit, _query, cmd = world
    item = cmd.file_item(
        tenant_id=TENANT,
        item_type=ReviewItemType.INBOX_DEAD_LETTER,
        severity=ReviewSeverity.CRITICAL,
        reason="r",
        suggested_action="s",
        source_event_ref="evt-1",
        actor="api-client",
    )
    with pytest.raises(ReviewReplayNotPossibleError):
        cmd.perform_action(item_id=item.id, tenant_id=TENANT, action_code="replay", actor="operator")
    assert queue.get_item(item.id, TENANT).status is ReviewItemStatus.OPEN


def test_replay_of_missing_event_raises_not_found(wired_world):
    _queue, _audit, _inbox, _registry, _replay, _mapping, cmd = wired_world
    item = cmd.file_item(
        tenant_id=TENANT,
        item_type=ReviewItemType.INBOX_DEAD_LETTER,
        severity=ReviewSeverity.CRITICAL,
        reason="r",
        suggested_action="s",
        source_event_ref="missing-event",
        actor="api-client",
    )
    with pytest.raises(EventNotFoundError):
        cmd.perform_action(item_id=item.id, tenant_id=TENANT, action_code="replay", actor="operator")


def test_replay_of_received_event_raises_not_dead_lettered(wired_world):
    _queue, _audit, inbox, _registry, _replay, _mapping, cmd = wired_world
    event = make_inbox_event(status=InboxEventStatus.RECEIVED, tenant_id=TENANT)
    inbox.events[event.id] = event
    item = cmd.file_item(
        tenant_id=TENANT,
        item_type=ReviewItemType.INBOX_DEAD_LETTER,
        severity=ReviewSeverity.CRITICAL,
        reason="r",
        suggested_action="s",
        source_event_ref=event.id,
        actor="api-client",
    )
    with pytest.raises(EventNotDeadLetteredError):
        cmd.perform_action(item_id=item.id, tenant_id=TENANT, action_code="replay", actor="operator")


def test_fix_source_retry_also_replays(wired_world):
    _queue, audit, inbox, _registry, _replay, _mapping, cmd = wired_world
    event = make_inbox_event(status=InboxEventStatus.DEAD_LETTERED, tenant_id=TENANT, attempts=3)
    inbox.events[event.id] = event
    item = cmd.file_item(
        tenant_id=TENANT,
        item_type=ReviewItemType.INBOX_DEAD_LETTER,
        severity=ReviewSeverity.CRITICAL,
        reason="r",
        suggested_action="s",
        source_event_ref=event.id,
        actor="api-client",
    )
    moved = cmd.perform_action(
        item_id=item.id, tenant_id=TENANT, action_code="fix_source_retry", actor="operator"
    )
    assert moved.status is ReviewItemStatus.IN_REVIEW
    assert inbox.events[event.id].status is InboxEventStatus.RECEIVED
    entries = audit.list_for_item(item.id, TENANT)
    assert entries[-1].action == "fix_source_retry"
    assert entries[-1].decision == "in_review"


# ---------------------------------------------------------------------------
# US-552: mapping learning (Suggest → Auto loop) + best-effort reprocess
# ---------------------------------------------------------------------------


def test_complete_mapping_retry_learns_mapping_and_reprocesses(wired_world):
    _queue, audit, inbox, registry, _replay, _mapping, cmd = wired_world
    event = make_inbox_event(status=InboxEventStatus.NEEDS_REVIEW, tenant_id=TENANT)
    inbox.events[event.id] = event
    item = cmd.file_item(
        tenant_id=TENANT,
        item_type=ReviewItemType.MAPPING,
        severity=ReviewSeverity.WARNING,
        reason="r",
        suggested_action="s",
        source_event_ref=event.id,
        actor="api-client",
    )
    moved = cmd.perform_action(
        item_id=item.id,
        tenant_id=TENANT,
        action_code="complete_mapping_retry",
        actor="operator",
        installation_id=INSTALLATION_ID,
        mapping=_learning_input(),
    )
    assert moved.status is ReviewItemStatus.IN_REVIEW
    suggestions = registry.list_suggestions(INSTALLATION_ID)
    assert len(suggestions) == 1
    suggestion = suggestions[0]
    assert suggestion.mapping_kind.value == "gateway_journal"
    assert suggestion.source_key == "zarinpal"
    assert suggestion.suggested_target_type == "journal"
    assert suggestion.suggested_target_ref == "journal-1"
    assert suggestion.confidence == Decimal("1")
    assert suggestion.basis == f"review_queue:{item.id}"
    entries = audit.list_for_item(item.id, TENANT)
    assert entries[-1].mapping_version == f"suggestion:{suggestion.id}"
    assert f"reprocessed source event {event.id}" in (entries[-1].note or "")
    assert inbox.events[event.id].status is InboxEventStatus.RECEIVED


def test_complete_mapping_retry_without_mapping_details_only_reprocesses(wired_world):
    _queue, audit, inbox, registry, _replay, _mapping, cmd = wired_world
    event = make_inbox_event(status=InboxEventStatus.NEEDS_REVIEW, tenant_id=TENANT)
    inbox.events[event.id] = event
    item = cmd.file_item(
        tenant_id=TENANT,
        item_type=ReviewItemType.MAPPING,
        severity=ReviewSeverity.WARNING,
        reason="r",
        suggested_action="s",
        source_event_ref=event.id,
        actor="api-client",
    )
    moved = cmd.perform_action(
        item_id=item.id,
        tenant_id=TENANT,
        action_code="complete_mapping_retry",
        actor="operator",
        installation_id=INSTALLATION_ID,
    )
    assert moved.status is ReviewItemStatus.IN_REVIEW
    assert registry.list_suggestions(INSTALLATION_ID) == []
    assert audit.list_for_item(item.id, TENANT)[-1].mapping_version is None
    assert inbox.events[event.id].status is InboxEventStatus.RECEIVED


def test_complete_mapping_retry_with_processed_event_still_succeeds(wired_world):
    """The reprocess is best-effort: an already-processed event is noted, not an error."""
    _queue, audit, inbox, registry, _replay, _mapping, cmd = wired_world
    event = make_inbox_event(status=InboxEventStatus.PROCESSED, tenant_id=TENANT)
    inbox.events[event.id] = event
    item = cmd.file_item(
        tenant_id=TENANT,
        item_type=ReviewItemType.MAPPING,
        severity=ReviewSeverity.WARNING,
        reason="r",
        suggested_action="s",
        source_event_ref=event.id,
        actor="api-client",
    )
    moved = cmd.perform_action(
        item_id=item.id,
        tenant_id=TENANT,
        action_code="complete_mapping_retry",
        actor="operator",
        installation_id=INSTALLATION_ID,
        mapping=_learning_input(),
    )
    assert moved.status is ReviewItemStatus.IN_REVIEW
    assert len(registry.list_suggestions(INSTALLATION_ID)) == 1
    assert "not reprocessed" in (audit.list_for_item(item.id, TENANT)[-1].note or "")


def test_complete_mapping_retry_with_missing_event_still_succeeds(wired_world):
    _queue, audit, _inbox, _registry, _replay, _mapping, cmd = wired_world
    item = cmd.file_item(
        tenant_id=TENANT,
        item_type=ReviewItemType.MAPPING,
        severity=ReviewSeverity.WARNING,
        reason="r",
        suggested_action="s",
        source_event_ref="missing-event",
        actor="api-client",
    )
    moved = cmd.perform_action(
        item_id=item.id, tenant_id=TENANT, action_code="complete_mapping_retry", actor="operator"
    )
    assert moved.status is ReviewItemStatus.IN_REVIEW
    assert "not found" in (audit.list_for_item(item.id, TENANT)[-1].note or "")


def test_complete_mapping_retry_without_source_event_notes_it(wired_world):
    _queue, audit, _inbox, _registry, _replay, _mapping, cmd = wired_world
    item = cmd.file_item(
        tenant_id=TENANT,
        item_type=ReviewItemType.MAPPING,
        severity=ReviewSeverity.WARNING,
        reason="r",
        suggested_action="s",
        actor="api-client",
    )
    moved = cmd.perform_action(
        item_id=item.id, tenant_id=TENANT, action_code="complete_mapping_retry", actor="operator"
    )
    assert moved.status is ReviewItemStatus.IN_REVIEW
    assert "no source event attached" in (audit.list_for_item(item.id, TENANT)[-1].note or "")


def test_mapping_details_on_other_action_rejected(wired_world):
    _queue, _audit, _inbox, _registry, _replay, _mapping, cmd = wired_world
    item = cmd.file_item(
        tenant_id=TENANT,
        item_type=ReviewItemType.MAPPING,
        severity=ReviewSeverity.WARNING,
        reason="r",
        suggested_action="s",
        actor="api-client",
    )
    with pytest.raises(ReviewActionNotPermittedError):
        cmd.perform_action(
            item_id=item.id,
            tenant_id=TENANT,
            action_code="skip",
            actor="operator",
            installation_id=INSTALLATION_ID,
            mapping=_learning_input(),
        )


def test_mapping_learning_without_installation_rejected(wired_world):
    _queue, _audit, _inbox, _registry, _replay, _mapping, cmd = wired_world
    item = cmd.file_item(
        tenant_id=TENANT,
        item_type=ReviewItemType.MAPPING,
        severity=ReviewSeverity.WARNING,
        reason="r",
        suggested_action="s",
        actor="api-client",
    )
    with pytest.raises(ReviewInvalidStateError):
        cmd.perform_action(
            item_id=item.id,
            tenant_id=TENANT,
            action_code="complete_mapping_retry",
            actor="operator",
            mapping=_learning_input(),
        )


def test_mapping_learning_with_unknown_kind_rejected(wired_world):
    _queue, _audit, _inbox, _registry, _replay, _mapping, cmd = wired_world
    item = cmd.file_item(
        tenant_id=TENANT,
        item_type=ReviewItemType.MAPPING,
        severity=ReviewSeverity.WARNING,
        reason="r",
        suggested_action="s",
        actor="api-client",
    )
    with pytest.raises(IntegrationMappingInvalidError):
        cmd.perform_action(
            item_id=item.id,
            tenant_id=TENANT,
            action_code="complete_mapping_retry",
            actor="operator",
            installation_id=INSTALLATION_ID,
            mapping=_learning_input(mapping_kind="not_a_kind"),
        )


def test_mapping_learning_without_wired_service_rejected(world):
    _queue, _audit, _query, cmd = world
    item = cmd.file_item(
        tenant_id=TENANT,
        item_type=ReviewItemType.MAPPING,
        severity=ReviewSeverity.WARNING,
        reason="r",
        suggested_action="s",
        actor="api-client",
    )
    with pytest.raises(ReviewInvalidStateError):
        cmd.perform_action(
            item_id=item.id,
            tenant_id=TENANT,
            action_code="complete_mapping_retry",
            actor="operator",
            installation_id=INSTALLATION_ID,
            mapping=_learning_input(),
        )


# ---------------------------------------------------------------------------
# US-552: the persistent pipeline adapter (F4)
# ---------------------------------------------------------------------------


def test_pipeline_adapter_files_real_items(world):
    queue, audit, _query, cmd = world
    adapter = PersistentReviewQueueAdapter(cmd)
    item_id = adapter.enqueue(
        ReviewItemDraft(
            type="mapping",
            severity="high",
            source_event_ref="evt-1",
            reason="No mapping for gateway zarinpal",
            suggested_action="Complete the mapping",
            details={"event_type": "sales.order.completed"},
            tenant_id=TENANT,
        )
    )
    item = queue.get_item(item_id, TENANT)
    assert item is not None
    assert item.item_type is ReviewItemType.MAPPING
    assert item.severity is ReviewSeverity.WARNING
    assert item.subtype == "sales.order.completed"
    assert item.source_event_ref == "evt-1"
    assert item.status is ReviewItemStatus.OPEN
    assert [entry.action for entry in audit.list_for_item(item_id, TENANT)] == ["filed"]


def test_pipeline_adapter_maps_critical_severity(world):
    queue, _audit, _query, cmd = world
    adapter = PersistentReviewQueueAdapter(cmd)
    item_id = adapter.enqueue(
        ReviewItemDraft(
            type="inbox_dead_letter",
            severity="critical",
            source_event_ref="evt-2",
            reason="Event failed permanently",
            suggested_action="Replay the event",
            tenant_id=TENANT,
        )
    )
    assert queue.get_item(item_id, TENANT).severity is ReviewSeverity.CRITICAL


def test_pipeline_adapter_tenantless_falls_back_to_inbox_behaviour(world):
    queue, _audit, _query, cmd = world
    adapter = PersistentReviewQueueAdapter(cmd)
    assert (
        adapter.enqueue(
            ReviewItemDraft(
                type="mapping",
                severity="high",
                source_event_ref="evt-3",
                reason="r",
                suggested_action="s",
                tenant_id=None,
            )
        )
        == "evt-3"
    )
    assert queue.items == {}


def test_pipeline_adapter_unknown_type_raises(world):
    _queue, _audit, _query, cmd = world
    adapter = PersistentReviewQueueAdapter(cmd)
    with pytest.raises(ValueError, match="not_a_type"):
        adapter.enqueue(
            ReviewItemDraft(
                type="not_a_type",
                severity="high",
                source_event_ref="evt-4",
                reason="r",
                suggested_action="s",
                tenant_id=TENANT,
            )
        )
