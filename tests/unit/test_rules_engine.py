"""Unit tests for the rules-engine core (US-561, Phase 5-7 ۵خ W2).

Pure matching (facts, priority, first-match/all-matches), the
closed action set, dry-run's zero-side-effects guarantee, and the
policy-gate guarantee (no rule can auto-post without the gate).
All effects run through recording fakes — no database, no engine.
"""

from __future__ import annotations

import datetime
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from hesabyar.application.commands.rules import RuleActionExecutor, render_template
from hesabyar.domain.events.envelope import CanonicalEventType
from hesabyar.domain.review_queue.models import (
    ReviewAuditEntry,
    ReviewItem,
    ReviewItemStatus,
    ReviewItemType,
    ReviewSeverity,
    SlaPolicy,
)
from hesabyar.domain.review_queue.ports import (
    ReviewAuditPort,
    ReviewItemFilters,
    ReviewQueuePort,
)
from hesabyar.domain.rules.actions import parse_action
from hesabyar.domain.rules.engine import build_facts, select_rules
from hesabyar.domain.rules.errors import RuleDefinitionError
from hesabyar.domain.rules.models import (
    ChannelDelivery,
    GateLayerVerdict,
    NotificationDelivery,
    Rule,
    RuleAction,
    RuleActionKind,
    RuleDraftLine,
    RuleExecutionContext,
    RuleExecutionDecision,
    RuleExecutionMode,
    RuleGateVerdict,
    RuleSetMode,
)
from tests.unit.test_installment_services import INSTALLATION_ID, TENANT_ID

NOW = datetime.datetime(2026, 11, 21, 10, 0, tzinfo=datetime.UTC)
EVENT_TYPE = CanonicalEventType.SALES_ORDER_COMPLETED
PAYLOAD = {
    "total_amount": 5_000_000,
    "payment_channel": "zarinpal",
    "party_id": "party-7",
    "order_id": "order-1",
}


def _rule(
    *,
    rule_id: str = "rule-1",
    name: str = "Test rule",
    conditions: dict[str, Any] | None = None,
    action: RuleAction | None = None,
    priority: int = 100,
    enabled: bool = True,
    event_type: CanonicalEventType = EVENT_TYPE,
) -> Rule:
    return Rule(
        id=rule_id,
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        name=name,
        description="",
        event_type=event_type,
        conditions=conditions if conditions is not None else {},
        action=action
        or RuleAction(
            kind=RuleActionKind.NOTIFY,
            params={
                "recipients": ["role:accountant"],
                "channels": ["in_app"],
                "subject": "Order {{payload.order_id}}",
                "message": "Amount {{amount_irr}}",
            },
        ),
        priority=priority,
        enabled=enabled,
        created_at=NOW,
        updated_at=NOW,
    )


def _context(**overrides: Any) -> RuleExecutionContext:
    kwargs: dict[str, Any] = {
        "tenant_id": TENANT_ID,
        "installation_id": INSTALLATION_ID,
        "company_engine_id": 1,
        "event_date": datetime.date(2026, 11, 20),
        "channel_key": "zarinpal",
        "actor": "test-client",
    }
    kwargs.update(overrides)
    return RuleExecutionContext(**kwargs)


class FakeGate:
    """Recording policy gate (allowed/denied/exploding, by construction)."""

    def __init__(
        self,
        *,
        allowed: bool = True,
        error: Exception | None = None,
    ) -> None:
        self.allowed = allowed
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def evaluate(
        self,
        *,
        installation_id: str | None,
        event_type: str,
        company_engine_id: int | None,
        event_date: datetime.date | None,
        amount_irr: int | None,
        channel_key: str | None,
    ) -> RuleGateVerdict:
        self.calls.append(
            {
                "installation_id": installation_id,
                "event_type": event_type,
                "company_engine_id": company_engine_id,
                "event_date": event_date,
                "amount_irr": amount_irr,
                "channel_key": channel_key,
            }
        )
        if self.error is not None:
            raise self.error
        return RuleGateVerdict(
            allowed=self.allowed,
            policy_version="policy-gate:test",
            layers=(
                GateLayerVerdict(name="event_type", passed=True, detail="allowed"),
                GateLayerVerdict(name="amount_cap", passed=self.allowed, detail="cap"),
                GateLayerVerdict(name="period", passed=True, detail="open"),
            ),
            reason="allowed" if self.allowed else "amount cap exceeded",
        )


class FakeDrafts:
    """Recording draft creation + posting ports."""

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.posted: list[dict[str, Any]] = []

    def create_journal_draft(
        self,
        *,
        company_engine_id: int,
        date: datetime.date,
        description: str,
        lines: Sequence[RuleDraftLine],
    ) -> str:
        self.created.append(
            {
                "company_engine_id": company_engine_id,
                "date": date,
                "description": description,
                "lines": list(lines),
            }
        )
        return "journal-draft:99"

    def post_journal_draft(self, *, company_engine_id: int, draft_engine_id: int) -> str:
        self.posted.append({"company_engine_id": company_engine_id, "draft_engine_id": draft_engine_id})
        return f"posted-journal:{draft_engine_id}"


class FakeNotifications:
    """Recording notification port."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send(
        self,
        *,
        recipients: Sequence[str],
        channels: Sequence[str],
        subject: str,
        message: str,
        facts: Mapping[str, Any],
    ) -> NotificationDelivery:
        self.sent.append(
            {
                "recipients": list(recipients),
                "channels": list(channels),
                "subject": render_template(subject, facts),
                "message": render_template(message, facts),
            }
        )
        return NotificationDelivery(
            results=tuple(ChannelDelivery(channel=c, status="delivered", detail="ok") for c in channels)
        )


class FakeReviewQueue(ReviewQueuePort, ReviewAuditPort):
    """In-memory review queue + audit trail."""

    def __init__(self) -> None:
        self.items: dict[str, ReviewItem] = {}
        self.entries: list[ReviewAuditEntry] = []

    def list_items(self, tenant_id: str, filters: ReviewItemFilters) -> Sequence[ReviewItem]:
        return [i for i in self.items.values() if i.tenant_id == tenant_id]

    def get_item(self, item_id: str, tenant_id: str) -> ReviewItem | None:
        item = self.items.get(item_id)
        if item is None or item.tenant_id != tenant_id:
            return None
        return item

    def save_item(self, item: ReviewItem) -> ReviewItem:
        self.items[item.id] = item
        return item

    def update_item(self, item: ReviewItem) -> ReviewItem:
        self.items[item.id] = item
        return item

    def list_policies(self, tenant_id: str) -> Sequence[SlaPolicy]:
        return []

    def upsert_policy(self, tenant_id: str, policy: SlaPolicy) -> SlaPolicy:
        return policy

    def record(self, entry: ReviewAuditEntry) -> ReviewAuditEntry:
        self.entries.append(entry)
        return entry

    def list_for_item(self, item_id: str, tenant_id: str) -> Sequence[ReviewAuditEntry]:
        return [e for e in self.entries if e.item_id == item_id]


def _review_item(*, item_id: str = "item-1", status: ReviewItemStatus = ReviewItemStatus.OPEN) -> ReviewItem:
    return ReviewItem(
        id=item_id,
        tenant_id=TENANT_ID,
        item_type=ReviewItemType.MAPPING,
        severity=ReviewSeverity.WARNING,
        status=status,
        reason="Missing mapping",
        suggested_action="Complete the mapping",
        created_at=NOW,
        updated_at=NOW,
    )


# -- facts + selection -------------------------------------------------------


def test_build_facts_merges_payload_and_context() -> None:
    facts = build_facts(EVENT_TYPE, PAYLOAD, _context())
    assert facts["event_type"] == "sales.order.completed"
    assert facts["amount_irr"] == 5_000_000
    assert facts["channel"] == "zarinpal"
    assert facts["party"] == "party-7"
    assert facts["company"] == "1"
    assert facts["payload"]["total_amount"] == 5_000_000
    # Without a context, envelope facts are absent (conditions on them fail closed).
    facts = build_facts(EVENT_TYPE, PAYLOAD, None)
    assert "company" not in facts
    assert facts["channel"] == "zarinpal"  # from the payload itself


def test_select_rules_filters_type_enabled_and_conditions() -> None:
    matching = _rule(rule_id="r-match", priority=10)
    wrong_type = _rule(
        rule_id="r-type",
        priority=1,
        event_type=CanonicalEventType.PAYMENT_CAPTURED,
    )
    disabled = _rule(rule_id="r-disabled", priority=2, enabled=False)
    no_match = _rule(
        rule_id="r-conditions",
        priority=3,
        conditions={">=": [{"var": "amount_irr"}, 99_000_000]},
    )
    facts = build_facts(EVENT_TYPE, PAYLOAD, _context())
    selected = select_rules(
        [wrong_type, disabled, no_match, matching],
        EVENT_TYPE,
        facts,
        RuleSetMode.ALL_MATCHES,
    )
    assert [r.id for r in selected] == ["r-match"]


def test_select_rules_orders_by_priority_then_creation() -> None:
    first = _rule(rule_id="r-first", priority=10)
    second = _rule(rule_id="r-second", priority=20)
    third = _rule(rule_id="r-third", priority=10)
    facts = build_facts(EVENT_TYPE, PAYLOAD, _context())
    selected = select_rules([second, third, first], EVENT_TYPE, facts, RuleSetMode.ALL_MATCHES)
    assert [r.id for r in selected] == ["r-first", "r-third", "r-second"]


def test_first_match_stops_at_the_first_matching_rule() -> None:
    first = _rule(rule_id="r-first", priority=10)
    second = _rule(rule_id="r-second", priority=20)
    facts = build_facts(EVENT_TYPE, PAYLOAD, _context())
    selected = select_rules([first, second], EVENT_TYPE, facts, RuleSetMode.FIRST_MATCH)
    assert [r.id for r in selected] == ["r-first"]


# -- the closed action set -----------------------------------------------------


def test_unknown_action_kind_is_rejected_at_parse_time() -> None:
    with pytest.raises(RuleDefinitionError, match="Unknown action kind"):
        parse_action({"kind": "execute_python_code", "params": {"code": "1+1"}})
    with pytest.raises(RuleDefinitionError, match="Unknown action kind"):
        parse_action({"kind": "send_telegram", "params": {}})
    with pytest.raises(RuleDefinitionError, match="Unknown action kind"):
        parse_action({"kind": "EXECUTE", "params": {}})


def test_all_six_closed_actions_parse() -> None:
    parse_action(
        {
            "kind": "create_draft",
            "params": {
                "document": "journal",
                "lines": [
                    {"account_code": "1010", "debit": 1000},
                    {"account_code": "2020", "credit": 1000},
                ],
            },
        }
    )
    parse_action({"kind": "post_if_policy_allows", "params": {}})
    parse_action(
        {
            "kind": "notify",
            "params": {
                "recipients": ["role:accountant"],
                "subject": "Hi",
                "message": "Body",
            },
        }
    )
    parse_action({"kind": "assign", "params": {"assignee": "user-1"}})
    parse_action({"kind": "tag", "params": {"tag": "urgent"}})
    parse_action(
        {
            "kind": "set_field",
            "params": {"target": "review_item", "field": "severity", "value": "critical"},
        }
    )


def test_action_param_validation() -> None:
    # create_draft: one-sided lines, at least two, journal only.
    with pytest.raises(RuleDefinitionError, match="at least two"):
        parse_action({"kind": "create_draft", "params": {"lines": [{"account_code": "1", "debit": 5}]}})
    with pytest.raises(RuleDefinitionError, match="exactly one of"):
        parse_action(
            {
                "kind": "create_draft",
                "params": {
                    "lines": [
                        {"account_code": "1", "debit": 5, "credit": 5},
                        {"account_code": "2", "credit": 5},
                    ]
                },
            }
        )
    with pytest.raises(RuleDefinitionError, match="journal"):
        parse_action(
            {
                "kind": "create_draft",
                "params": {
                    "document": "invoice",
                    "lines": [
                        {"account_code": "1", "debit": 5},
                        {"account_code": "2", "credit": 5},
                    ],
                },
            }
        )
    # post: invoice posting is not in v1.
    with pytest.raises(RuleDefinitionError, match="journal"):
        parse_action({"kind": "post_if_policy_allows", "params": {"draft_kind": "invoice"}})
    # notify: recipients + channels.
    with pytest.raises(RuleDefinitionError, match="recipients"):
        parse_action({"kind": "notify", "params": {"recipients": [], "subject": "s", "message": "m"}})
    with pytest.raises(RuleDefinitionError, match="channels"):
        parse_action(
            {
                "kind": "notify",
                "params": {
                    "recipients": ["u"],
                    "channels": ["telegram"],
                    "subject": "s",
                    "message": "m",
                },
            }
        )
    # set_field: only review_item.severity in v1.
    with pytest.raises(RuleDefinitionError, match="target"):
        parse_action(
            {
                "kind": "set_field",
                "params": {"target": "draft", "field": "description", "value": "x"},
            }
        )
    with pytest.raises(RuleDefinitionError, match="value"):
        parse_action(
            {
                "kind": "set_field",
                "params": {"target": "review_item", "field": "severity", "value": "high"},
            }
        )


# -- dry-run: zero side effects -------------------------------------------------


def test_dry_run_calls_no_effect_port() -> None:
    gate = FakeGate(allowed=True)
    drafts = FakeDrafts()
    notifications = FakeNotifications()
    queue = FakeReviewQueue()
    queue.save_item(_review_item())
    executor = RuleActionExecutor(
        gate=gate,
        drafts=drafts,
        posting=drafts,
        notifications=notifications,
        review_queue=queue,
        review_audit=queue,
        now=NOW,
    )
    context = _context(review_item_id="item-1", draft_engine_id=42)
    actions = [
        RuleAction(
            kind=RuleActionKind.CREATE_DRAFT,
            params={
                "document": "journal",
                "lines": [
                    {"account_code": "1010", "debit": 1000},
                    {"account_code": "2020", "credit": 1000},
                ],
            },
        ),
        RuleAction(kind=RuleActionKind.POST_IF_POLICY_ALLOWS, params={}),
        RuleAction(
            kind=RuleActionKind.NOTIFY,
            params={
                "recipients": ["role:accountant"],
                "channels": ["in_app", "email"],
                "subject": "Hi",
                "message": "Body",
            },
        ),
        RuleAction(kind=RuleActionKind.ASSIGN, params={"assignee": "user-1"}),
        RuleAction(kind=RuleActionKind.TAG, params={"tag": "urgent"}),
        RuleAction(
            kind=RuleActionKind.SET_FIELD,
            params={"target": "review_item", "field": "severity", "value": "critical"},
        ),
    ]
    for index, action in enumerate(actions):
        rule = _rule(rule_id=f"r-{index}", action=action)
        execution = executor.describe_dry_run(
            rule, event_type=EVENT_TYPE, payload=PAYLOAD, event_id="evt-1", context=context
        )
        assert execution.mode is RuleExecutionMode.DRY_RUN
        assert execution.decision is RuleExecutionDecision.WOULD_EXECUTE
        assert execution.outcome == "would_execute"
        assert execution.details["would_do"]
    # The ONLY port a dry-run may touch is the (read-only) gate — and
    # only for the post action, once.
    assert len(gate.calls) == 1
    assert drafts.created == []
    assert drafts.posted == []
    assert notifications.sent == []
    assert queue.items["item-1"].assignee is None
    assert queue.items["item-1"].tags == ()
    assert queue.items["item-1"].severity is ReviewSeverity.WARNING
    assert queue.entries == []


def test_dry_run_reports_the_gate_verdict_for_a_post_action() -> None:
    executor = RuleActionExecutor(gate=FakeGate(allowed=False), now=NOW)
    rule = _rule(action=RuleAction(kind=RuleActionKind.POST_IF_POLICY_ALLOWS, params={}))
    execution = executor.describe_dry_run(
        rule,
        event_type=EVENT_TYPE,
        payload=PAYLOAD,
        event_id="evt-1",
        context=_context(draft_engine_id=42),
    )
    assert execution.decision is RuleExecutionDecision.WOULD_EXECUTE
    assert execution.details["gate_evaluated"] is True
    assert execution.details["would_post"] is False
    assert execution.details["gate_allowed"] is False
    assert execution.details["policy_version"] == "policy-gate:test"


def test_dry_run_of_a_non_matching_rule() -> None:
    executor = RuleActionExecutor(now=NOW)
    rule = _rule(conditions={">=": [{"var": "amount_irr"}, 99_000_000]})
    execution = executor.describe_dry_run(
        rule,
        event_type=EVENT_TYPE,
        payload=PAYLOAD,
        event_id="evt-1",
        context=_context(),
    )
    assert execution.decision is RuleExecutionDecision.NOT_MATCHED
    assert execution.outcome == "no_match"


# -- the policy gate is never bypassed --------------------------------------------


def _post_rule() -> Rule:
    return _rule(action=RuleAction(kind=RuleActionKind.POST_IF_POLICY_ALLOWS, params={}))


def test_post_action_goes_through_the_gate_when_allowed() -> None:
    gate = FakeGate(allowed=True)
    drafts = FakeDrafts()
    executor = RuleActionExecutor(gate=gate, posting=drafts, now=NOW)
    execution = executor.execute_live(
        _post_rule(),
        event_type=EVENT_TYPE,
        payload=PAYLOAD,
        event_id="evt-1",
        context=_context(draft_engine_id=42),
    )
    assert len(gate.calls) == 1
    assert gate.calls[0]["amount_irr"] == 5_000_000
    assert gate.calls[0]["event_type"] == "sales.order.completed"
    assert gate.calls[0]["company_engine_id"] == 1
    assert execution.decision is RuleExecutionDecision.EXECUTED
    assert execution.outcome == "posted"
    assert execution.details["posted_ref"] == "posted-journal:42"
    assert execution.details["gate_allowed"] is True
    assert drafts.posted == [{"company_engine_id": 1, "draft_engine_id": 42}]


def test_post_action_blocked_by_the_gate_keeps_the_draft() -> None:
    gate = FakeGate(allowed=False)
    drafts = FakeDrafts()
    executor = RuleActionExecutor(gate=gate, posting=drafts, now=NOW)
    execution = executor.execute_live(
        _post_rule(),
        event_type=EVENT_TYPE,
        payload=PAYLOAD,
        event_id="evt-1",
        context=_context(draft_engine_id=42),
    )
    assert execution.decision is RuleExecutionDecision.BLOCKED_BY_GATE
    assert execution.outcome == "draft_kept"
    assert execution.details["gate_allowed"] is False
    assert execution.details["draft_ref"] == "journal-draft:42"
    assert drafts.posted == []  # the gate denied: nothing was posted


def test_post_action_without_a_wired_gate_fails_closed() -> None:
    drafts = FakeDrafts()
    executor = RuleActionExecutor(posting=drafts, now=NOW)  # no gate at all
    execution = executor.execute_live(
        _post_rule(),
        event_type=EVENT_TYPE,
        payload=PAYLOAD,
        event_id="evt-1",
        context=_context(draft_engine_id=42),
    )
    assert execution.decision is RuleExecutionDecision.BLOCKED_BY_GATE
    assert execution.outcome == "draft_kept"
    assert drafts.posted == []


def test_post_action_with_a_failing_gate_fails_closed() -> None:
    gate = FakeGate(error=RuntimeError("Tryton is down"))
    drafts = FakeDrafts()
    executor = RuleActionExecutor(gate=gate, posting=drafts, now=NOW)
    execution = executor.execute_live(
        _post_rule(),
        event_type=EVENT_TYPE,
        payload=PAYLOAD,
        event_id="evt-1",
        context=_context(draft_engine_id=42),
    )
    assert execution.decision is RuleExecutionDecision.FAILED
    assert drafts.posted == []


def test_post_action_without_a_company_fails_closed() -> None:
    gate = FakeGate(allowed=True)
    drafts = FakeDrafts()
    executor = RuleActionExecutor(gate=gate, posting=drafts, now=NOW)
    execution = executor.execute_live(
        _post_rule(),
        event_type=EVENT_TYPE,
        payload=PAYLOAD,
        event_id="evt-1",
        context=_context(company_engine_id=None, draft_engine_id=42),
    )
    assert execution.decision is RuleExecutionDecision.BLOCKED_BY_GATE
    assert gate.calls == []  # the executor denies before the gate is even asked
    assert drafts.posted == []


def test_post_action_without_a_draft_is_skipped_not_invented() -> None:
    gate = FakeGate(allowed=True)
    drafts = FakeDrafts()
    executor = RuleActionExecutor(gate=gate, posting=drafts, now=NOW)
    execution = executor.execute_live(
        _post_rule(),
        event_type=EVENT_TYPE,
        payload=PAYLOAD,
        event_id="evt-1",
        context=_context(),  # no draft in the context
    )
    assert execution.decision is RuleExecutionDecision.SKIPPED
    assert execution.outcome == "no_draft"
    assert gate.calls == []
    assert drafts.posted == []


def test_post_action_resolves_the_draft_from_the_payload() -> None:
    gate = FakeGate(allowed=True)
    drafts = FakeDrafts()
    executor = RuleActionExecutor(gate=gate, posting=drafts, now=NOW)
    rule = _rule(
        action=RuleAction(
            kind=RuleActionKind.POST_IF_POLICY_ALLOWS,
            params={"draft_id_field": "payload.draft_id"},
        )
    )
    payload = dict(PAYLOAD, draft_id=77)
    execution = executor.execute_live(
        rule,
        event_type=EVENT_TYPE,
        payload=payload,
        event_id="evt-1",
        context=_context(),
    )
    assert execution.decision is RuleExecutionDecision.EXECUTED
    assert drafts.posted == [{"company_engine_id": 1, "draft_engine_id": 77}]


# -- the other actions ------------------------------------------------------------


def test_create_draft_resolves_amounts_from_the_event() -> None:
    drafts = FakeDrafts()
    executor = RuleActionExecutor(drafts=drafts, now=NOW)
    rule = _rule(
        action=RuleAction(
            kind=RuleActionKind.CREATE_DRAFT,
            params={
                "document": "journal",
                "description": "Fee for {{payload.order_id}}",
                "lines": [
                    {"account_code": "6010", "debit": {"var": "amount_irr"}},
                    {"account_code": "1010", "credit": {"var": "amount_irr"}},
                ],
            },
        )
    )
    execution = executor.execute_live(
        rule, event_type=EVENT_TYPE, payload=PAYLOAD, event_id="evt-1", context=_context()
    )
    assert execution.decision is RuleExecutionDecision.EXECUTED
    assert execution.outcome == "draft_created"
    assert execution.details["draft_ref"] == "journal-draft:99"
    created = drafts.created[0]
    assert created["description"] == "Fee for order-1"
    assert created["lines"][0].debit_irr == 5_000_000
    assert created["lines"][1].credit_irr == 5_000_000
    assert created["date"] == datetime.date(2026, 11, 20)


def test_create_draft_with_an_unresolvable_amount_fails() -> None:
    drafts = FakeDrafts()
    executor = RuleActionExecutor(drafts=drafts, now=NOW)
    rule = _rule(
        action=RuleAction(
            kind=RuleActionKind.CREATE_DRAFT,
            params={
                "document": "journal",
                "lines": [
                    {"account_code": "6010", "debit": {"var": "payload.missing"}},
                    {"account_code": "1010", "credit": 1000},
                ],
            },
        )
    )
    execution = executor.execute_live(
        rule, event_type=EVENT_TYPE, payload=PAYLOAD, event_id="evt-1", context=_context()
    )
    assert execution.decision is RuleExecutionDecision.FAILED
    assert drafts.created == []


def test_notify_renders_templates_and_records_channels() -> None:
    notifications = FakeNotifications()
    executor = RuleActionExecutor(notifications=notifications, now=NOW)
    execution = executor.execute_live(
        _rule(), event_type=EVENT_TYPE, payload=PAYLOAD, event_id="evt-1", context=_context()
    )
    assert execution.decision is RuleExecutionDecision.EXECUTED
    assert execution.outcome == "notified"
    sent = notifications.sent[0]
    assert sent["subject"] == "Order order-1"
    assert sent["message"] == "Amount 5000000"
    assert execution.details["channels"] == [{"channel": "in_app", "status": "delivered", "detail": "ok"}]


def test_assign_tag_and_set_field_mutate_the_review_item() -> None:
    queue = FakeReviewQueue()
    queue.save_item(_review_item())
    executor = RuleActionExecutor(review_queue=queue, review_audit=queue, now=NOW)
    context = _context(review_item_id="item-1")

    assign = executor.execute_live(
        _rule(action=RuleAction(kind=RuleActionKind.ASSIGN, params={"assignee": "user-9"})),
        event_type=EVENT_TYPE,
        payload=PAYLOAD,
        event_id="evt-1",
        context=context,
    )
    assert assign.decision is RuleExecutionDecision.EXECUTED
    assert assign.outcome == "assigned"
    assert queue.items["item-1"].assignee == "user-9"

    tag = executor.execute_live(
        _rule(action=RuleAction(kind=RuleActionKind.TAG, params={"tag": "urgent"})),
        event_type=EVENT_TYPE,
        payload=PAYLOAD,
        event_id="evt-1",
        context=context,
    )
    assert tag.outcome == "tagged"
    assert queue.items["item-1"].tags == ("urgent",)

    # Tagging twice does not duplicate.
    executor.execute_live(
        _rule(action=RuleAction(kind=RuleActionKind.TAG, params={"tag": "urgent"})),
        event_type=EVENT_TYPE,
        payload=PAYLOAD,
        event_id="evt-1",
        context=context,
    )
    assert queue.items["item-1"].tags == ("urgent",)

    set_field = executor.execute_live(
        _rule(
            action=RuleAction(
                kind=RuleActionKind.SET_FIELD,
                params={"target": "review_item", "field": "severity", "value": "critical"},
            )
        ),
        event_type=EVENT_TYPE,
        payload=PAYLOAD,
        event_id="evt-1",
        context=context,
    )
    assert set_field.outcome == "field_set"
    assert queue.items["item-1"].severity is ReviewSeverity.CRITICAL

    # Every mutation left a US-552 audit entry, attributed to the rule.
    assert len(queue.entries) == 4
    assert all(entry.actor == "rule:rule-1" for entry in queue.entries)
    assert [entry.action for entry in queue.entries] == [
        "assign",
        "tag",
        "tag",
        "set_field",
    ]


def test_review_item_actions_skip_without_a_target() -> None:
    queue = FakeReviewQueue()
    executor = RuleActionExecutor(review_queue=queue, review_audit=queue, now=NOW)
    execution = executor.execute_live(
        _rule(action=RuleAction(kind=RuleActionKind.ASSIGN, params={"assignee": "u"})),
        event_type=EVENT_TYPE,
        payload=PAYLOAD,
        event_id="evt-1",
        context=_context(),
    )
    assert execution.decision is RuleExecutionDecision.SKIPPED
    assert execution.outcome == "no_review_item"


def test_review_item_actions_skip_terminal_items() -> None:
    queue = FakeReviewQueue()
    queue.save_item(_review_item(status=ReviewItemStatus.RESOLVED))
    executor = RuleActionExecutor(review_queue=queue, review_audit=queue, now=NOW)
    execution = executor.execute_live(
        _rule(action=RuleAction(kind=RuleActionKind.TAG, params={"tag": "x"})),
        event_type=EVENT_TYPE,
        payload=PAYLOAD,
        event_id="evt-1",
        context=_context(review_item_id="item-1"),
    )
    assert execution.decision is RuleExecutionDecision.SKIPPED
    assert queue.items["item-1"].tags == ()


def test_a_failing_action_becomes_a_failed_execution_not_an_exception() -> None:
    class ExplodingNotifications(FakeNotifications):
        def send(self, **kwargs: Any) -> NotificationDelivery:
            raise RuntimeError("SMTP is down")

    executor = RuleActionExecutor(notifications=ExplodingNotifications(), now=NOW)
    execution = executor.execute_live(
        _rule(), event_type=EVENT_TYPE, payload=PAYLOAD, event_id="evt-1", context=_context()
    )
    assert execution.decision is RuleExecutionDecision.FAILED
    assert execution.outcome == "error"
    assert "SMTP is down" in (execution.error or "")


def test_render_template_missing_placeholder_is_empty() -> None:
    assert render_template("A {{payload.missing}} B", {"payload": {}}) == "A  B"
    assert render_template("{{channel}}", {"channel": "zarinpal"}) == "zarinpal"
    assert render_template("no placeholders", {}) == "no placeholders"
