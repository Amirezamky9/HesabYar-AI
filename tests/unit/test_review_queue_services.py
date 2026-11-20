"""Unit tests for the review-queue services (US-504, Phase 5 F4).

No database: the ports are faked in memory, and the clock is fixed, so
ageing/SLA math is deterministic. The REST tests
(``tests/interfaces/test_rest_review_queue.py``) cover the real
repository + HTTP layer against PostgreSQL.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence

import pytest

from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.application.errors import (
    ReviewActionNotPermittedError,
    ReviewInvalidSlaPolicyError,
    ReviewInvalidStateError,
    ReviewItemNotFoundError,
)
from hesabyar.application.queries.review_queue import ReviewQueueQueryService
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
    """Fresh ports + services with a fixed clock."""
    queue = InMemoryReviewQueue()
    audit = InMemoryReviewAudit()
    query = ReviewQueueQueryService(queue, audit, now=NOW)
    cmd = ReviewQueueCommandService(queue, audit, now=NOW)
    return queue, audit, query, cmd


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
        channel=channel,
        amount_irr=amount_irr,
        updated_at=created,
    )


# ---------------------------------------------------------------------------
# Domain: the ten types, permitted actions, SLA defaults
# ---------------------------------------------------------------------------


def test_ten_item_types_exist():
    assert len(ReviewItemType) == 10
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


def test_summary_covers_all_ten_types_even_empty(world):
    _queue, _audit, query, _cmd = world
    summary = query.summary(TENANT)
    assert len(summary.counters) == 10
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


def test_retry_action_moves_to_in_review_and_stays_open(world):
    _queue, _audit, _query, cmd = world
    item = cmd.file_item(
        tenant_id=TENANT,
        item_type=ReviewItemType.INBOX_DEAD_LETTER,
        severity=ReviewSeverity.CRITICAL,
        reason="r",
        suggested_action="s",
        actor="api-client",
    )
    moved = cmd.perform_action(item_id=item.id, tenant_id=TENANT, action_code="replay", actor="operator")
    assert moved.status is ReviewItemStatus.IN_REVIEW
    assert moved.resolution is None
    assert moved.resolved_at is None


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
