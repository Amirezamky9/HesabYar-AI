"""Unit tests for the rules-engine service (US-561, Phase 5-7 ۵خ W2).

CRUD validation, enable/disable, rule-set match modes, dry-run
through the service (audit-logged), live evaluation (first-match
vs all-matches, one rule's failure never breaks the others), and
the execution history. In-memory fakes behind the ports — no
database, no engine.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from typing import Any

import pytest

from hesabyar.application.commands.rules import RuleActionExecutor, RuleEngineService
from hesabyar.application.errors import (
    RuleExecutionNotFoundError,
    RuleInvalidError,
    RuleNotFoundError,
)
from hesabyar.domain.events.envelope import CanonicalEventType
from hesabyar.domain.rules.models import (
    Rule,
    RuleExecution,
    RuleExecutionContext,
    RuleExecutionDecision,
    RuleExecutionMode,
    RuleSetConfig,
    RuleSetMode,
)
from hesabyar.domain.rules.ports import RuleRepositoryPort
from tests.unit.test_installment_services import INSTALLATION_ID, TENANT_ID
from tests.unit.test_rules_engine import FakeNotifications

NOW = datetime.datetime(2026, 11, 21, 10, 0, tzinfo=datetime.UTC)
EVENT_TYPE = "sales.order.completed"
PAYLOAD = {"total_amount": 5_000_000, "payment_channel": "zarinpal"}

NOTIFY_ACTION = {
    "kind": "notify",
    "params": {
        "recipients": ["role:accountant"],
        "channels": ["in_app"],
        "subject": "Order",
        "message": "Amount {{amount_irr}}",
    },
}


class FakeRuleRepository(RuleRepositoryPort):
    """In-memory rules repository (same semantics as the SQLAlchemy one)."""

    def __init__(self) -> None:
        self.rules: dict[str, Rule] = {}
        self.executions: list[RuleExecution] = []
        self.configs: dict[tuple[str, str], RuleSetConfig] = {}

    def save_rule(self, rule: Rule) -> Rule:
        self.rules[rule.id] = rule
        return rule

    def get_rule(self, tenant_id: str, rule_id: str) -> Rule | None:
        rule = self.rules.get(rule_id)
        if rule is None or rule.tenant_id != tenant_id:
            return None
        return rule

    def list_rules(
        self,
        tenant_id: str,
        *,
        installation_id: str | None = None,
        event_type: CanonicalEventType | None = None,
        enabled: bool | None = None,
    ) -> Sequence[Rule]:
        found = []
        for rule in self.rules.values():
            if rule.tenant_id != tenant_id:
                continue
            if installation_id is None:
                if rule.installation_id is not None:
                    continue
            elif rule.installation_id not in (installation_id, None):
                continue
            if event_type is not None and rule.event_type is not event_type:
                continue
            if enabled is not None and rule.enabled is not enabled:
                continue
            found.append(rule)
        found.sort(
            key=lambda r: (
                r.priority,
                r.created_at or datetime.datetime.min.replace(tzinfo=datetime.UTC),
                r.id,
            )
        )
        return found

    def delete_rule(self, tenant_id: str, rule_id: str) -> bool:
        rule = self.rules.get(rule_id)
        if rule is None or rule.tenant_id != tenant_id:
            return False
        del self.rules[rule_id]
        return True

    def save_execution(self, execution: RuleExecution) -> RuleExecution:
        self.executions.append(execution)
        return execution

    def get_execution(self, tenant_id: str, execution_id: str) -> RuleExecution | None:
        for execution in self.executions:
            if execution.id == execution_id and execution.tenant_id == tenant_id:
                return execution
        return None

    def list_executions(
        self,
        tenant_id: str,
        *,
        rule_id: str | None = None,
        event_id: str | None = None,
        limit: int = 50,
    ) -> Sequence[RuleExecution]:
        found = [e for e in self.executions if e.tenant_id == tenant_id]
        if rule_id is not None:
            found = [e for e in found if e.rule_id == rule_id]
        if event_id is not None:
            found = [e for e in found if e.event_id == event_id]
        return list(reversed(found))[:limit]

    def get_set_config(self, tenant_id: str, event_type: CanonicalEventType) -> RuleSetConfig | None:
        return self.configs.get((tenant_id, event_type.value))

    def save_set_config(self, config: RuleSetConfig) -> RuleSetConfig:
        self.configs[(config.tenant_id, config.event_type.value)] = config
        return config

    def list_set_configs(self, tenant_id: str) -> Sequence[RuleSetConfig]:
        return [c for (t, _), c in sorted(self.configs.items()) if t == tenant_id]


def _service(
    repository: FakeRuleRepository | None = None,
    notifications: FakeNotifications | None = None,
) -> tuple[RuleEngineService, FakeRuleRepository, FakeNotifications]:
    repository = repository or FakeRuleRepository()
    notifications = notifications or FakeNotifications()
    executor = RuleActionExecutor(notifications=notifications, now=NOW)
    return RuleEngineService(repository, executor=executor, now=NOW), repository, notifications


def _context(**overrides: Any) -> RuleExecutionContext:
    kwargs: dict[str, Any] = {
        "tenant_id": TENANT_ID,
        "installation_id": INSTALLATION_ID,
        "company_engine_id": 1,
        "event_date": datetime.date(2026, 11, 20),
        "actor": "test-client",
    }
    kwargs.update(overrides)
    return RuleExecutionContext(**kwargs)


def _create(
    service: RuleEngineService,
    *,
    name: str = "Notify on big orders",
    conditions: dict[str, Any] | None = None,
    action: dict[str, Any] | None = None,
    priority: int = 100,
    enabled: bool = True,
) -> Rule:
    return service.create_rule(
        TENANT_ID,
        INSTALLATION_ID,
        name=name,
        event_type=EVENT_TYPE,
        conditions=conditions if conditions is not None else {},
        action=action if action is not None else NOTIFY_ACTION,
        priority=priority,
        enabled=enabled,
        actor="test-client",
    )


# -- CRUD ----------------------------------------------------------------------


def test_create_and_get_rule() -> None:
    service, _, _ = _service()
    rule = _create(service, conditions={">=": [{"var": "amount_irr"}, 1_000_000]})
    assert rule.id
    assert rule.tenant_id == TENANT_ID
    assert rule.installation_id == INSTALLATION_ID
    assert rule.event_type is CanonicalEventType.SALES_ORDER_COMPLETED
    assert rule.conditions == {">=": [{"var": "amount_irr"}, 1_000_000]}
    assert rule.action.kind.value == "notify"
    assert rule.enabled is True
    assert service.get_rule(TENANT_ID, rule.id).id == rule.id


def test_create_rule_rejects_an_unknown_event_type() -> None:
    service, _, _ = _service()
    with pytest.raises(RuleInvalidError):
        service.create_rule(
            TENANT_ID,
            INSTALLATION_ID,
            name="Bad event",
            event_type="order.made_up",
            conditions={},
            action=NOTIFY_ACTION,
            actor="test-client",
        )


def test_create_rule_rejects_an_unknown_action() -> None:
    service, _, _ = _service()
    with pytest.raises(RuleInvalidError, match="Unknown action kind"):
        service.create_rule(
            TENANT_ID,
            INSTALLATION_ID,
            name="Bad action",
            event_type=EVENT_TYPE,
            conditions={},
            action={"kind": "execute_python_code", "params": {"code": "1"}},
            actor="test-client",
        )


def test_create_rule_rejects_bad_conditions_and_names() -> None:
    service, _, _ = _service()
    with pytest.raises(RuleInvalidError):
        _create(service, conditions={"xor": []})
    with pytest.raises(RuleInvalidError):
        _create(service, name="   ")
    with pytest.raises(RuleInvalidError):
        _create(service, priority=-1)


def test_update_rule_changes_only_given_fields() -> None:
    service, _, _ = _service()
    rule = _create(service)
    updated = service.update_rule(TENANT_ID, rule.id, name="Renamed", priority=5, enabled=False)
    assert updated.name == "Renamed"
    assert updated.priority == 5
    assert updated.enabled is False
    assert updated.event_type is CanonicalEventType.SALES_ORDER_COMPLETED
    assert updated.conditions == {}
    with pytest.raises(RuleInvalidError):
        service.update_rule(TENANT_ID, rule.id, action={"kind": "nope", "params": {}})


def test_get_update_delete_unknown_rule_is_404() -> None:
    service, _, _ = _service()
    with pytest.raises(RuleNotFoundError):
        service.get_rule(TENANT_ID, "missing")
    with pytest.raises(RuleNotFoundError):
        service.update_rule(TENANT_ID, "missing", name="x")
    with pytest.raises(RuleNotFoundError):
        service.delete_rule(TENANT_ID, "missing")
    with pytest.raises(RuleNotFoundError):
        service.set_enabled(TENANT_ID, "missing", enabled=True)


def test_rules_are_tenant_scoped() -> None:
    service, _, _ = _service()
    rule = _create(service)
    with pytest.raises(RuleNotFoundError):
        service.get_rule("other-tenant", rule.id)
    assert service.list_rules("other-tenant", INSTALLATION_ID) == []


def test_delete_rule_keeps_its_executions() -> None:
    service, repository, _ = _service()
    rule = _create(service)
    service.dry_run_rule(TENANT_ID, rule.id, event_type=EVENT_TYPE, payload=PAYLOAD, context=_context())
    assert len(repository.executions) == 1
    service.delete_rule(TENANT_ID, rule.id)
    with pytest.raises(RuleNotFoundError):
        service.get_rule(TENANT_ID, rule.id)
    # The audit row survives the rule.
    assert len(repository.executions) == 1


def test_enable_disable_round_trip() -> None:
    service, _, _ = _service()
    rule = _create(service)
    assert service.set_enabled(TENANT_ID, rule.id, enabled=False).enabled is False
    assert service.set_enabled(TENANT_ID, rule.id, enabled=True).enabled is True


def test_list_rules_filters_and_orders() -> None:
    service, _, _ = _service()
    _create(service, name="low", priority=200)
    _create(service, name="high", priority=10)
    _create(service, name="off", priority=5, enabled=False)
    rules = service.list_rules(TENANT_ID, INSTALLATION_ID)
    assert [r.name for r in rules] == ["off", "high", "low"]
    assert [r.name for r in service.list_rules(TENANT_ID, INSTALLATION_ID, enabled=True)] == [
        "high",
        "low",
    ]
    assert service.list_rules(TENANT_ID, INSTALLATION_ID, event_type="payment.captured") == []
    with pytest.raises(RuleInvalidError):
        service.list_rules(TENANT_ID, INSTALLATION_ID, event_type="not.a.type")


# -- rule-set match modes ---------------------------------------------------------


def test_match_mode_defaults_to_first_match() -> None:
    service, _, _ = _service()
    assert service.get_match_mode(TENANT_ID, CanonicalEventType.SALES_ORDER_COMPLETED) is RuleSetMode.FIRST_MATCH


def test_set_match_mode_round_trip() -> None:
    service, _, _ = _service()
    config = service.set_match_mode(TENANT_ID, event_type=EVENT_TYPE, match_mode="all_matches")
    assert config.match_mode is RuleSetMode.ALL_MATCHES
    assert service.get_match_mode(TENANT_ID, CanonicalEventType.SALES_ORDER_COMPLETED) is RuleSetMode.ALL_MATCHES
    assert len(service.list_set_configs(TENANT_ID)) == 1
    with pytest.raises(RuleInvalidError, match="match mode"):
        service.set_match_mode(TENANT_ID, event_type=EVENT_TYPE, match_mode="random")
    with pytest.raises(RuleInvalidError):
        service.set_match_mode(TENANT_ID, event_type="nope", match_mode="all_matches")


# -- dry-run -----------------------------------------------------------------------


def test_dry_run_is_audit_logged_and_has_no_side_effects() -> None:
    service, repository, notifications = _service()
    rule = _create(service)
    result = service.dry_run_rule(
        TENANT_ID,
        rule.id,
        event_type=EVENT_TYPE,
        payload=PAYLOAD,
        event_id="evt-1",
        context=_context(),
    )
    assert result.matched is True
    assert result.decision is RuleExecutionDecision.WOULD_EXECUTE
    assert result.would_do == "Notify role:accountant via in_app."
    assert result.execution_id
    assert notifications.sent == []  # no side effects
    assert len(repository.executions) == 1
    execution = repository.executions[0]
    assert execution.mode is RuleExecutionMode.DRY_RUN
    assert execution.rule_id == rule.id
    assert execution.event_id == "evt-1"


def test_dry_run_of_a_disabled_rule_still_works() -> None:
    service, _, notifications = _service()
    rule = _create(service, enabled=False)
    result = service.dry_run_rule(TENANT_ID, rule.id, event_type=EVENT_TYPE, payload=PAYLOAD, context=_context())
    assert result.matched is True  # test before activation (§6.4)
    assert notifications.sent == []


def test_dry_run_definition_validates_and_logs_without_a_rule_id() -> None:
    service, repository, _ = _service()
    result = service.dry_run_definition(
        TENANT_ID,
        name="Draft",
        event_type=EVENT_TYPE,
        conditions={">=": [{"var": "amount_irr"}, 1_000_000]},
        action=NOTIFY_ACTION,
        payload=PAYLOAD,
        context=_context(),
    )
    assert result.rule_id is None
    assert result.matched is True
    assert repository.executions[0].rule_id is None
    with pytest.raises(RuleInvalidError, match="Unknown action kind"):
        service.dry_run_definition(
            TENANT_ID,
            name="Draft",
            event_type=EVENT_TYPE,
            action={"kind": "nope", "params": {}},
            payload=PAYLOAD,
            context=_context(),
        )


# -- live evaluation -------------------------------------------------------------------


def test_evaluate_event_first_match_runs_one_rule() -> None:
    service, _, notifications = _service()
    _create(service, name="first", priority=10)
    _create(service, name="second", priority=20)
    executions = service.evaluate_event(
        TENANT_ID,
        event_type=EVENT_TYPE,
        payload=PAYLOAD,
        event_id="evt-1",
        context=_context(),
    )
    assert len(executions) == 1
    assert executions[0].mode is RuleExecutionMode.LIVE
    assert executions[0].decision is RuleExecutionDecision.EXECUTED
    assert len(notifications.sent) == 1


def test_evaluate_event_all_matches_runs_every_matching_rule() -> None:
    service, _, notifications = _service()
    service.set_match_mode(TENANT_ID, event_type=EVENT_TYPE, match_mode="all_matches")
    _create(service, name="first", priority=10)
    _create(service, name="second", priority=20)
    _create(
        service,
        name="no-match",
        priority=5,
        conditions={">=": [{"var": "amount_irr"}, 99_000_000]},
    )
    executions = service.evaluate_event(
        TENANT_ID,
        event_type=EVENT_TYPE,
        payload=PAYLOAD,
        event_id="evt-1",
        context=_context(),
    )
    assert len(executions) == 2  # the non-matching rule records nothing
    assert len(notifications.sent) == 2


def test_evaluate_event_mode_override() -> None:
    service, _, notifications = _service()
    _create(service, name="first", priority=10)
    _create(service, name="second", priority=20)
    executions = service.evaluate_event(
        TENANT_ID,
        event_type=EVENT_TYPE,
        payload=PAYLOAD,
        event_id="evt-1",
        context=_context(),
        match_mode="all_matches",
    )
    assert len(executions) == 2
    assert len(notifications.sent) == 2
    with pytest.raises(RuleInvalidError, match="match mode"):
        service.evaluate_event(
            TENANT_ID,
            event_type=EVENT_TYPE,
            payload=PAYLOAD,
            event_id="evt-1",
            context=_context(),
            match_mode="random",
        )


def test_evaluate_event_skips_disabled_rules() -> None:
    service, _, notifications = _service()
    _create(service, name="off", enabled=False)
    executions = service.evaluate_event(
        TENANT_ID,
        event_type=EVENT_TYPE,
        payload=PAYLOAD,
        event_id="evt-1",
        context=_context(),
    )
    assert executions == []
    assert notifications.sent == []


def test_one_failing_rule_does_not_break_the_others() -> None:
    class Exploding(FakeNotifications):
        def send(self, **kwargs: Any) -> Any:
            raise RuntimeError("boom")

    repository = FakeRuleRepository()
    executor = RuleActionExecutor(notifications=Exploding(), now=NOW)
    service = RuleEngineService(repository, executor=executor, now=NOW)
    service.set_match_mode(TENANT_ID, event_type=EVENT_TYPE, match_mode="all_matches")
    _create(service, name="first", priority=10)
    _create(service, name="second", priority=20)
    executions = service.evaluate_event(
        TENANT_ID,
        event_type=EVENT_TYPE,
        payload=PAYLOAD,
        event_id="evt-1",
        context=_context(),
    )
    assert len(executions) == 2
    assert all(e.decision is RuleExecutionDecision.FAILED for e in executions)


# -- execution history -----------------------------------------------------------------


def test_execution_history_filters_and_404() -> None:
    service, _, _ = _service()
    rule = _create(service)
    service.dry_run_rule(
        TENANT_ID,
        rule.id,
        event_type=EVENT_TYPE,
        payload=PAYLOAD,
        event_id="evt-1",
        context=_context(),
    )
    service.evaluate_event(
        TENANT_ID,
        event_type=EVENT_TYPE,
        payload=PAYLOAD,
        event_id="evt-2",
        context=_context(),
    )
    all_executions = service.list_executions(TENANT_ID)
    assert len(all_executions) == 2
    assert all_executions[0].mode is RuleExecutionMode.LIVE  # newest first
    assert len(service.list_executions(TENANT_ID, event_id="evt-1")) == 1
    assert len(service.list_executions(TENANT_ID, rule_id="other")) == 0
    execution = service.get_execution(TENANT_ID, all_executions[0].id)
    assert execution.id == all_executions[0].id
    with pytest.raises(RuleExecutionNotFoundError):
        service.get_execution(TENANT_ID, "missing")
    with pytest.raises(RuleInvalidError):
        service.list_executions(TENANT_ID, limit=0)
