"""Unit tests for bank auto-coding rules (US-565, Phase 5-7 ۵خ W3).

Pure matcher tests (pattern language, priority, Suggest → Auto),
service tests (CRUD validation, promotion at threshold 3, the undo /
correction path), and reconcile-integration tests over the
test_bank_services fakes (W3 auto-coding, suggestion recording, the
tie-out guard, and the no-post guarantee: reconcile only ever writes
line fields on a draft statement).

No DB, no servers: everything runs over in-memory fakes. The
SQLAlchemy repository + REST layer are covered by the migration tests
and tests/interfaces/test_rest_bank_rules.py.
"""

from __future__ import annotations

import dataclasses
import datetime
from collections.abc import Sequence
from typing import Any, cast

import pytest

from hesabyar.application.commands.bank_reconcile import BankReconcileService
from hesabyar.application.commands.bank_rules import BankRulesService
from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.domain.accounting.commands import UpdateBankStatementLine
from hesabyar.domain.accounting.ports import AccountingEnginePort
from hesabyar.domain.accounting.refs import BankStatementRef
from hesabyar.domain.bank.errors import (
    BankLineAlreadyMatchedError,
    BankRuleInvalidError,
    BankRuleNotFoundError,
    BankRuleNothingToUndoError,
    BankRuleNotPromotableError,
    BankRuleSuggestionNotFoundError,
    BankRuleSuggestionNotPendingError,
    BankStatementNotDraftError,
)
from hesabyar.domain.bank.matching import (
    BANK_RULE_CONFIDENCE,
    LineToMatch,
    bank_rule_matches,
    match_statement_line,
)
from hesabyar.domain.bank.models import (
    RULE_AUTO_MIN_CONFIRMATIONS,
    BankMatchRule,
    BankRule,
    BankRuleSuggestion,
    BankRuleSuggestionStatus,
    BankStatementMatch,
    MatchStatus,
    MatchTargetType,
    ReconcileOutcome,
)
from hesabyar.domain.bank.ports import BankRulesRepositoryPort
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.domain.integrations.models import SuggestionMode
from hesabyar.domain.integrations.ports import IntegrationsRegistryPort
from hesabyar.domain.review_queue.models import ReviewItemStatus
from tests.unit.test_bank_services import (
    COMPANY,
    COMPANY_ID,
    NOW,
    TENANT,
    FakeBankRepo,
    FakeEngine,
    FakeRegistry,
    FakeReviewQueue,
    _fd,
    _line,
)


class FakeBankRulesRepo(BankRulesRepositoryPort):
    """In-memory bank-rules port (delete emulates ON DELETE SET NULL)."""

    def __init__(self) -> None:
        self.rules: dict[str, BankRule] = {}
        self.suggestions: dict[str, BankRuleSuggestion] = {}

    def list_bank_rules(
        self,
        tenant_id: str,
        company_id: str | None = None,
        enabled: bool | None = None,
        mode: str | None = None,
    ) -> Sequence[BankRule]:
        rows = [r for r in self.rules.values() if r.tenant_id == tenant_id]
        if company_id is not None:
            rows = [r for r in rows if r.company_id == company_id]
        if enabled is not None:
            rows = [r for r in rows if r.enabled == enabled]
        if mode is not None:
            rows = [r for r in rows if r.mode.value == mode]
        rows.sort(
            key=lambda r: (
                r.priority,
                r.created_at or datetime.datetime.min.replace(tzinfo=datetime.UTC),
                r.id,
            )
        )
        return rows

    def get_bank_rule(self, rule_id: str, tenant_id: str) -> BankRule | None:
        rule = self.rules.get(rule_id)
        return rule if rule is not None and rule.tenant_id == tenant_id else None

    def save_bank_rule(self, rule: BankRule) -> BankRule:
        self.rules[rule.id] = rule
        return rule

    def update_bank_rule(self, rule: BankRule) -> BankRule:
        self.rules[rule.id] = rule
        return rule

    def delete_bank_rule(self, rule_id: str, tenant_id: str) -> bool:
        rule = self.rules.get(rule_id)
        if rule is None or rule.tenant_id != tenant_id:
            return False
        del self.rules[rule_id]
        for suggestion in list(self.suggestions.values()):
            if suggestion.rule_id == rule_id:
                self.suggestions[suggestion.id] = dataclasses.replace(suggestion, rule_id=None)
        return True

    def list_suggestions(
        self,
        tenant_id: str,
        rule_id: str | None = None,
        status: BankRuleSuggestionStatus | None = None,
        company_id: str | None = None,
    ) -> Sequence[BankRuleSuggestion]:
        rows = [s for s in self.suggestions.values() if s.tenant_id == tenant_id]
        if rule_id is not None:
            rows = [s for s in rows if s.rule_id == rule_id]
        if status is not None:
            rows = [s for s in rows if s.status == status]
        if company_id is not None:
            rows = [s for s in rows if s.company_id == company_id]
        rows.sort(
            key=lambda s: (
                s.created_at or datetime.datetime.min.replace(tzinfo=datetime.UTC),
                s.id,
            ),
            reverse=True,
        )
        return rows

    def get_suggestion(self, suggestion_id: str, tenant_id: str) -> BankRuleSuggestion | None:
        suggestion = self.suggestions.get(suggestion_id)
        return suggestion if suggestion is not None and suggestion.tenant_id == tenant_id else None

    def find_pending_suggestion(
        self, rule_id: str, tenant_id: str, statement_id: int, statement_line_id: int
    ) -> BankRuleSuggestion | None:
        for suggestion in self.suggestions.values():
            if (
                suggestion.tenant_id == tenant_id
                and suggestion.rule_id == rule_id
                and suggestion.statement_id == statement_id
                and suggestion.statement_line_id == statement_line_id
                and suggestion.status == BankRuleSuggestionStatus.PENDING
            ):
                return suggestion
        return None

    def save_suggestion(self, suggestion: BankRuleSuggestion) -> BankRuleSuggestion:
        self.suggestions[suggestion.id] = suggestion
        return suggestion

    def update_suggestion(self, suggestion: BankRuleSuggestion) -> BankRuleSuggestion:
        self.suggestions[suggestion.id] = suggestion
        return suggestion


class RecordingEngine(FakeEngine):
    """Fake engine that records every line write (the no-post proof)."""

    def __init__(self) -> None:
        super().__init__()
        self.line_updates: list[UpdateBankStatementLine] = []

    def update_bank_statement_line(self, command: UpdateBankStatementLine) -> None:
        self.line_updates.append(command)
        super().update_bank_statement_line(command)


def _rule(**overrides: Any) -> BankRule:
    base: dict[str, Any] = {
        "id": "rule-1",
        "tenant_id": TENANT,
        "company_id": COMPANY_ID,
        "bank_key": "",
        "name": "کارمزد اسنپ‌پی",
        "target_account_id": 601,
        "description_contains": "اسنپ",
        "direction": "out",
        "created_at": NOW,
        "updated_at": NOW,
    }
    base.update(overrides)
    return BankRule(**base)


def _match_line(**overrides: Any) -> LineToMatch:
    base: dict[str, Any] = {
        "line_id": 9001,
        "date": _fd(),
        "amount_irr": -50_000,
        "description": "پرداخت اسنپ‌پی بابت سفارش",
    }
    base.update(overrides)
    return LineToMatch(**base)


def _suggestion(rule: BankRule, **overrides: Any) -> BankRuleSuggestion:
    base: dict[str, Any] = {
        "id": "sugg-1",
        "tenant_id": TENANT,
        "rule_id": rule.id,
        "rule_name": rule.name,
        "company_id": COMPANY_ID,
        "statement_id": 1000,
        "statement_line_id": 9001,
        "line_description": "پرداخت اسنپ‌پی بابت سفارش",
        "line_amount_irr": -50_000,
        "target_account_id": rule.target_account_id,
        "target_party_id": rule.target_party_id,
        "created_at": NOW,
    }
    base.update(overrides)
    return BankRuleSuggestion(**base)


@pytest.fixture
def world() -> dict[str, Any]:
    """A fresh fake world: engine + repos + the bank-rules + reconcile services."""
    engine = RecordingEngine()
    bank = FakeBankRepo()
    bank_rules = FakeBankRulesRepo()
    queue = FakeReviewQueue()
    review = ReviewQueueCommandService(queue, queue, now=NOW)
    registry = FakeRegistry()
    engine_port = cast(AccountingEnginePort, engine)
    registry_port = cast(IntegrationsRegistryPort, registry)
    rules_service = BankRulesService(bank_rules, bank, engine_port, review=review, now=NOW)
    reconcile = BankReconcileService(engine_port, bank, review, queue, registry_port, bank_rules=bank_rules, now=NOW)
    return {
        "engine": engine,
        "bank": bank,
        "bank_rules": bank_rules,
        "queue": queue,
        "review": review,
        "rules": rules_service,
        "reconcile": reconcile,
    }


def _reconcile(world: dict[str, Any], statement_id: int) -> Any:
    return world["reconcile"].reconcile(
        tenant_id=TENANT,
        installation_id="install-1",
        company=COMPANY,
        company_id=COMPANY_ID,
        statement_id=statement_id,
        actor="operator",
    )


def _set_balances(world: dict[str, Any], statement_id: int, start: int, end: int) -> None:
    view = world["engine"].statements[statement_id]
    world["engine"].statements[statement_id] = dataclasses.replace(
        view, start_balance=MoneyIRR(start), end_balance=MoneyIRR(end)
    )


# -- the pattern language -----------------------------------------------------------


def test_bank_rule_matches_description_direction_and_range() -> None:
    rule = _rule(amount_min_irr=10_000, amount_max_irr=100_000)
    assert bank_rule_matches(rule, _match_line())
    # Wrong direction.
    assert not bank_rule_matches(rule, _match_line(amount_irr=50_000))
    # Description does not contain the pattern.
    assert not bank_rule_matches(rule, _match_line(description="واریز مشتری"))
    # Below the range / above the range (magnitude, not signed amount).
    assert not bank_rule_matches(rule, _match_line(amount_irr=-5_000))
    assert not bank_rule_matches(rule, _match_line(amount_irr=-500_000))
    # Range bounds are inclusive.
    assert bank_rule_matches(rule, _match_line(amount_irr=-10_000))
    assert bank_rule_matches(rule, _match_line(amount_irr=-100_000))


def test_bank_rule_matches_counterparty_in_description_or_party_name() -> None:
    rule = _rule(description_contains=None, counterparty_contains="اسنپ‌پی", direction="any")
    assert bank_rule_matches(rule, _match_line())
    assert bank_rule_matches(
        rule,
        _match_line(description="پرداخت بابت سفارش", counterparty="اسنپ‌پی"),
    )
    assert not bank_rule_matches(rule, _match_line(description="پرداخت بابت سفارش", counterparty="زرین‌پال"))
    assert not bank_rule_matches(rule, _match_line(description="پرداخت بابت سفارش"))


def test_bank_rule_matches_normalises_persian_text() -> None:
    # Arabic Yeh/Kaf + Persian digits + case fold, on both sides.
    rule = _rule(description_contains="اسنپ‌پي", direction="any")
    assert bank_rule_matches(rule, _match_line(description="پرداخت اسنپ‌پی"))
    rule_latin = _rule(description_contains="SNAPP", direction="any")
    assert bank_rule_matches(rule_latin, _match_line(description="پرداخت snapp"))


def test_bank_rule_without_conditions_matches_nothing() -> None:
    # Fail-closed: a condition-less row written outside the service
    # (which rejects one at creation) must not match every line.
    rule = _rule(
        description_contains=None,
        counterparty_contains=None,
        amount_min_irr=None,
        amount_max_irr=None,
        direction="any",
    )
    assert not rule.has_pattern
    assert not bank_rule_matches(rule, _match_line())


def test_match_statement_line_w3_auto_codes_before_learned_rules() -> None:
    w3 = _rule(mode=SuggestionMode.AUTO, confirmations=3)
    learned = BankMatchRule(
        id="learned-1",
        tenant_id=TENANT,
        company_id=COMPANY_ID,
        bank_key="",
        pattern="اسنپ",
        direction="out",
        target_account_id=501,
        mode=SuggestionMode.AUTO,
        confirmations=5,
    )
    result = match_statement_line(_match_line(), [], [learned], bank_rules=[w3])
    assert result.outcome == ReconcileOutcome.MATCHED
    assert result.target_type == MatchTargetType.RULE
    assert result.target_id == "rule-1"
    assert result.confidence == BANK_RULE_CONFIDENCE
    assert result.confidence == 90
    assert "کارمزد اسنپ‌پی" in (result.target_label or "")


def test_match_statement_line_w3_suggest_holds_the_line_for_review() -> None:
    w3 = _rule(mode=SuggestionMode.SUGGEST)
    learned = BankMatchRule(
        id="learned-1",
        tenant_id=TENANT,
        company_id=COMPANY_ID,
        bank_key="",
        pattern="اسنپ",
        direction="out",
        target_account_id=501,
        mode=SuggestionMode.AUTO,
        confirmations=5,
    )
    # The explicit user definition outranks the learned AUTO rule: the
    # line waits for a human instead of auto-coding.
    result = match_statement_line(_match_line(), [], [learned], bank_rules=[w3])
    assert result.outcome == ReconcileOutcome.SUGGESTED
    assert len(result.suggestions) == 1
    assert result.suggestions[0].target_id == "rule-1"
    assert result.suggestions[0].confidence == BANK_RULE_CONFIDENCE


def test_match_statement_line_w3_first_match_by_priority() -> None:
    first = _rule(id="rule-low", priority=10, target_account_id=601)
    second = _rule(id="rule-high", priority=20, target_account_id=501)
    result = match_statement_line(_match_line(), [], [], bank_rules=[second, first])
    assert result.target_id == "rule-low"


def test_match_statement_line_w3_disabled_rule_never_fires() -> None:
    w3 = _rule(mode=SuggestionMode.AUTO, enabled=False)
    result = match_statement_line(_match_line(), [], [], bank_rules=[w3])
    assert result.outcome == ReconcileOutcome.UNMATCHED


def test_match_statement_line_without_bank_rules_is_unchanged() -> None:
    # No bank_rules argument: the US-514 matcher behaves exactly as before.
    learned = BankMatchRule(
        id="learned-1",
        tenant_id=TENANT,
        company_id=COMPANY_ID,
        bank_key="",
        pattern="اسنپ",
        direction="out",
        target_account_id=501,
        mode=SuggestionMode.AUTO,
        confirmations=5,
    )
    result = match_statement_line(_match_line(), [], [learned])
    assert result.outcome == ReconcileOutcome.MATCHED
    assert result.target_id == "learned-1"
    assert result.confidence == 85


# -- service: CRUD + validation -------------------------------------------------------


def test_create_rule_is_born_suggest_with_zero_confirmations(world: dict) -> None:
    rule = world["rules"].create_rule(
        tenant_id=TENANT,
        company_id=COMPANY_ID,
        name="کارمزد اسنپ‌پی",
        target_account_id=601,
        description_contains="اسنپ",
        direction="out",
        actor="operator",
    )
    assert rule.mode == SuggestionMode.SUGGEST
    assert rule.confirmations == 0
    assert rule.auto_eligible is False
    assert rule.created_by == "operator"


def test_create_rule_normalises_pattern_fragments(world: dict) -> None:
    rule = world["rules"].create_rule(
        tenant_id=TENANT,
        company_id=COMPANY_ID,
        name="  کارمزد  ",
        target_account_id=601,
        description_contains="  اسنپ‌پي  ",
        counterparty_contains="   ",
    )
    assert rule.name == "کارمزد"
    assert rule.description_contains == "اسنپ‌پی"
    # A blank counterparty is no condition at all.
    assert rule.counterparty_contains is None


def test_create_rule_without_conditions_is_rejected(world: dict) -> None:
    with pytest.raises(BankRuleInvalidError):
        world["rules"].create_rule(
            tenant_id=TENANT,
            company_id=COMPANY_ID,
            name="همه‌چیز",
            target_account_id=601,
        )


def test_create_rule_rejects_bad_range_direction_and_target(world: dict) -> None:
    service = world["rules"]
    with pytest.raises(BankRuleInvalidError):
        service.create_rule(
            tenant_id=TENANT,
            company_id=COMPANY_ID,
            name="x",
            target_account_id=601,
            amount_min_irr=100_000,
            amount_max_irr=10_000,
        )
    with pytest.raises(BankRuleInvalidError):
        service.create_rule(
            tenant_id=TENANT,
            company_id=COMPANY_ID,
            name="x",
            target_account_id=601,
            description_contains="اسنپ",
            direction="sideways",
        )
    with pytest.raises(BankRuleInvalidError):
        service.create_rule(
            tenant_id=TENANT,
            company_id=COMPANY_ID,
            name="x",
            target_account_id=0,
            description_contains="اسنپ",
        )
    with pytest.raises(BankRuleInvalidError):
        service.create_rule(
            tenant_id=TENANT,
            company_id=COMPANY_ID,
            name="   ",
            target_account_id=601,
            description_contains="اسنپ",
        )


def test_get_unknown_rule_is_404(world: dict) -> None:
    with pytest.raises(BankRuleNotFoundError):
        world["rules"].get_rule(TENANT, "missing")


def test_list_rules_filters_and_firing_order(world: dict) -> None:
    service = world["rules"]
    service.create_rule(
        tenant_id=TENANT,
        company_id=COMPANY_ID,
        name="دوم",
        target_account_id=601,
        description_contains="اسنپ",
        priority=20,
    )
    service.create_rule(
        tenant_id=TENANT,
        company_id=COMPANY_ID,
        name="اول",
        target_account_id=601,
        description_contains="زرین",
        priority=5,
        enabled=False,
    )
    assert [r.name for r in service.list_rules(TENANT)] == ["اول", "دوم"]
    assert [r.name for r in service.list_rules(TENANT, enabled=True)] == ["دوم"]
    assert [r.name for r in service.list_rules(TENANT, mode="auto")] == []
    assert [r.name for r in service.list_rules(TENANT, company_id="other")] == []
    with pytest.raises(BankRuleInvalidError):
        service.list_rules(TENANT, mode="sometimes")


def test_update_rule_definition_change_resets_trust(world: dict) -> None:
    service = world["rules"]
    rule = service.create_rule(
        tenant_id=TENANT,
        company_id=COMPANY_ID,
        name="کارمزد",
        target_account_id=601,
        description_contains="اسنپ",
    )
    # Fake three confirmations + a promotion, then change the target.
    promoted = dataclasses.replace(rule, confirmations=3, mode=SuggestionMode.AUTO)
    world["bank_rules"].update_bank_rule(promoted)
    updated = service.update_rule(TENANT, rule.id, {"target_account_id": 501})
    assert updated.mode == SuggestionMode.SUGGEST
    assert updated.confirmations == 0
    assert updated.target_account_id == 501


def test_update_rule_name_or_priority_keeps_trust(world: dict) -> None:
    service = world["rules"]
    rule = service.create_rule(
        tenant_id=TENANT,
        company_id=COMPANY_ID,
        name="کارمزد",
        target_account_id=601,
        description_contains="اسنپ",
    )
    world["bank_rules"].update_bank_rule(dataclasses.replace(rule, confirmations=2))
    updated = service.update_rule(TENANT, rule.id, {"name": "کارمزد تازه", "priority": 5})
    assert updated.confirmations == 2
    assert updated.name == "کارمزد تازه"


def test_update_rule_can_clear_a_condition_but_not_the_last_one(world: dict) -> None:
    service = world["rules"]
    rule = service.create_rule(
        tenant_id=TENANT,
        company_id=COMPANY_ID,
        name="کارمزد",
        target_account_id=601,
        description_contains="اسنپ",
        amount_min_irr=10_000,
    )
    cleared = service.update_rule(TENANT, rule.id, {"description_contains": None})
    assert cleared.description_contains is None
    assert cleared.amount_min_irr == 10_000
    with pytest.raises(BankRuleInvalidError):
        service.update_rule(TENANT, rule.id, {"amount_min_irr": None})


def test_update_rule_unknown_field_is_rejected(world: dict) -> None:
    rule = world["rules"].create_rule(
        tenant_id=TENANT,
        company_id=COMPANY_ID,
        name="کارمزد",
        target_account_id=601,
        description_contains="اسنپ",
    )
    with pytest.raises(BankRuleInvalidError):
        world["rules"].update_rule(TENANT, rule.id, {"mode": "auto"})


def test_delete_rule_keeps_suggestions_with_null_rule(world: dict) -> None:
    service = world["rules"]
    rule = service.create_rule(
        tenant_id=TENANT,
        company_id=COMPANY_ID,
        name="کارمزد",
        target_account_id=601,
        description_contains="اسنپ",
    )
    world["bank_rules"].save_suggestion(_suggestion(rule))
    service.delete_rule(TENANT, rule.id)
    with pytest.raises(BankRuleNotFoundError):
        service.get_rule(TENANT, rule.id)
    suggestions = service.list_suggestions(TENANT)
    assert len(suggestions) == 1
    assert suggestions[0].rule_id is None
    assert suggestions[0].rule_name == "کارمزد"


def test_set_enabled(world: dict) -> None:
    rule = world["rules"].create_rule(
        tenant_id=TENANT,
        company_id=COMPANY_ID,
        name="کارمزد",
        target_account_id=601,
        description_contains="اسنپ",
    )
    disabled = world["rules"].set_enabled(TENANT, rule.id, False)
    assert disabled.enabled is False
    enabled = world["rules"].set_enabled(TENANT, rule.id, True)
    assert enabled.enabled is True


# -- Suggest → Auto promotion ----------------------------------------------------------


def test_promote_below_threshold_is_409(world: dict) -> None:
    assert RULE_AUTO_MIN_CONFIRMATIONS == 3
    rule = world["rules"].create_rule(
        tenant_id=TENANT,
        company_id=COMPANY_ID,
        name="کارمزد",
        target_account_id=601,
        description_contains="اسنپ",
    )
    world["bank_rules"].update_bank_rule(dataclasses.replace(rule, confirmations=2))
    with pytest.raises(BankRuleNotPromotableError):
        world["rules"].promote_rule(TENANT, rule.id)


def test_promote_at_threshold_keeps_the_count(world: dict) -> None:
    rule = world["rules"].create_rule(
        tenant_id=TENANT,
        company_id=COMPANY_ID,
        name="کارمزد",
        target_account_id=601,
        description_contains="اسنپ",
    )
    world["bank_rules"].update_bank_rule(dataclasses.replace(rule, confirmations=3))
    promoted = world["rules"].promote_rule(TENANT, rule.id)
    assert promoted.mode == SuggestionMode.AUTO
    # Promoting is not a confirmation: the count carries unchanged.
    assert promoted.confirmations == 3
    assert promoted.auto_eligible is False
    with pytest.raises(BankRuleNotPromotableError):
        world["rules"].promote_rule(TENANT, rule.id)


def test_demote_resets_confirmations_to_zero(world: dict) -> None:
    rule = world["rules"].create_rule(
        tenant_id=TENANT,
        company_id=COMPANY_ID,
        name="کارمزد",
        target_account_id=601,
        description_contains="اسنپ",
    )
    world["bank_rules"].update_bank_rule(dataclasses.replace(rule, confirmations=3, mode=SuggestionMode.AUTO))
    demoted = world["rules"].demote_rule(TENANT, rule.id)
    assert demoted.mode == SuggestionMode.SUGGEST
    assert demoted.confirmations == 0
    with pytest.raises(BankRuleNotPromotableError):
        world["rules"].demote_rule(TENANT, rule.id)


# -- suggestions: confirm / reject ------------------------------------------------------


def _seed_suggestion_world(world: dict[str, Any]) -> tuple[BankRule, BankRuleSuggestion, int]:
    """One SUGGEST rule + one pending suggestion on a draft statement's line."""
    rule = world["rules"].create_rule(
        tenant_id=TENANT,
        company_id=COMPANY_ID,
        name="کارمزد اسنپ‌پی",
        target_account_id=601,
        description_contains="اسنپ",
        direction="out",
    )
    statement_id = world["engine"].add_statement(
        lines=[_line(amount_irr=-50_000, description="پرداخت اسنپ‌پی بابت سفارش")]
    )
    suggestion = world["bank_rules"].save_suggestion(_suggestion(rule, statement_id=statement_id))
    return rule, suggestion, statement_id


def test_confirm_suggestion_applies_coding_and_counts(world: dict) -> None:
    rule, suggestion, statement_id = _seed_suggestion_world(world)
    result = world["rules"].confirm_suggestion(
        tenant_id=TENANT,
        suggestion_id=suggestion.id,
        company_id=COMPANY_ID,
        actor="operator",
    )
    assert result.match.status == MatchStatus.CONFIRMED
    assert result.match.target_type == MatchTargetType.RULE
    assert result.match.target_ref == rule.id
    assert result.rule is not None and result.rule.confirmations == 1
    assert result.rule.auto_eligible is False
    line = world["engine"].get_bank_statement(BankStatementRef(statement_id)).lines[0]
    assert line.account.engine_id == 601
    stored = world["bank_rules"].get_suggestion(suggestion.id, TENANT)
    assert stored is not None and stored.status == BankRuleSuggestionStatus.CONFIRMED
    assert stored.confirmed_by == "operator"
    # Confirming twice is a 409.
    with pytest.raises(BankRuleSuggestionNotPendingError):
        world["rules"].confirm_suggestion(
            tenant_id=TENANT,
            suggestion_id=suggestion.id,
            company_id=COMPANY_ID,
            actor="operator",
        )


def test_confirm_suggestion_resolves_the_review_item(world: dict) -> None:
    rule, suggestion, statement_id = _seed_suggestion_world(world)
    # Reconcile: the line gets a SUGGESTED match + a bank_unmatched
    # review item. The run records no second suggestion row — the
    # seeded pending row dedups it.
    _reconcile(world, statement_id)
    pending = world["rules"].list_suggestions(TENANT, status="pending")
    assert [s.id for s in pending] == [suggestion.id]
    result = world["rules"].confirm_suggestion(
        tenant_id=TENANT,
        suggestion_id=suggestion.id,
        company_id=COMPANY_ID,
        actor="operator",
    )
    assert result.match.review_item_id is not None
    item = world["queue"].get_item(result.match.review_item_id, TENANT)
    assert item is not None
    assert item.status == ReviewItemStatus.RESOLVED


def test_confirm_suggestion_on_matched_line_is_409(world: dict) -> None:
    rule, suggestion, statement_id = _seed_suggestion_world(world)
    world["bank"].save_match(
        BankStatementMatch(
            id="match-live",
            tenant_id=TENANT,
            company_id=COMPANY_ID,
            statement_id=statement_id,
            statement_line_id=9001,
            line_date=_fd(),
            line_amount_irr=-50_000,
            line_description="پرداخت اسنپ‌پی بابت سفارش",
            target_type=MatchTargetType.INVOICE,
            target_id=10,
            target_ref="10",
            target_label="فاکتور ۱۰",
            priority=1,
            confidence=100,
            status=MatchStatus.CONFIRMED,
        )
    )
    with pytest.raises(BankLineAlreadyMatchedError):
        world["rules"].confirm_suggestion(
            tenant_id=TENANT,
            suggestion_id=suggestion.id,
            company_id=COMPANY_ID,
            actor="operator",
        )


def test_confirm_suggestion_on_validated_statement_is_409(world: dict) -> None:
    rule, suggestion, statement_id = _seed_suggestion_world(world)
    view = world["engine"].statements[statement_id]
    world["engine"].statements[statement_id] = dataclasses.replace(view, state="validated")
    with pytest.raises(BankStatementNotDraftError):
        world["rules"].confirm_suggestion(
            tenant_id=TENANT,
            suggestion_id=suggestion.id,
            company_id=COMPANY_ID,
            actor="operator",
        )


def test_confirm_unknown_suggestion_is_404(world: dict) -> None:
    with pytest.raises(BankRuleSuggestionNotFoundError):
        world["rules"].confirm_suggestion(
            tenant_id=TENANT,
            suggestion_id="missing",
            company_id=COMPANY_ID,
            actor="operator",
        )


def test_confirm_suggestion_after_rule_deleted_still_applies(world: dict) -> None:
    rule, suggestion, statement_id = _seed_suggestion_world(world)
    world["rules"].delete_rule(TENANT, rule.id)
    result = world["rules"].confirm_suggestion(
        tenant_id=TENANT,
        suggestion_id=suggestion.id,
        company_id=COMPANY_ID,
        actor="operator",
    )
    assert result.match.status == MatchStatus.CONFIRMED
    assert result.rule is None
    line = world["engine"].get_bank_statement(BankStatementRef(statement_id)).lines[0]
    assert line.account.engine_id == 601


def test_reject_suggestion_rejects_the_suggested_match_too(world: dict) -> None:
    rule, suggestion, statement_id = _seed_suggestion_world(world)
    _reconcile(world, statement_id)
    rejected = world["rules"].reject_suggestion(tenant_id=TENANT, suggestion_id=suggestion.id, actor="operator")
    assert rejected.status == BankRuleSuggestionStatus.REJECTED
    matches = world["bank"].list_matches_for_statement(TENANT, statement_id)
    assert all(m.status == MatchStatus.REJECTED for m in matches)
    assert len(matches) == 1
    with pytest.raises(BankRuleSuggestionNotPendingError):
        world["rules"].reject_suggestion(tenant_id=TENANT, suggestion_id=suggestion.id, actor="operator")


def test_list_suggestions_filters(world: dict) -> None:
    rule, suggestion, _ = _seed_suggestion_world(world)
    other = world["bank_rules"].save_suggestion(
        _suggestion(rule, id="sugg-2", statement_line_id=9002, status=BankRuleSuggestionStatus.CONFIRMED)
    )
    assert len(world["rules"].list_suggestions(TENANT)) == 2
    assert [s.id for s in world["rules"].list_suggestions(TENANT, status="pending")] == [suggestion.id]
    # Newest first; the two rows share a timestamp, so the id breaks the tie.
    assert [s.id for s in world["rules"].list_suggestions(TENANT, rule_id=rule.id)] == [
        other.id,
        suggestion.id,
    ]
    with pytest.raises(BankRuleInvalidError):
        world["rules"].list_suggestions(TENANT, status="maybe")


# -- undo / correction --------------------------------------------------------------------


def _seed_auto_match(world: dict[str, Any], **match_overrides: Any) -> tuple[int, str]:
    """A draft statement with one AUTO_APPLIED rule match on its line."""
    statement_id = world["engine"].add_statement(
        lines=[_line(amount_irr=-50_000, description="پرداخت اسنپ‌پی بابت سفارش")]
    )
    base: dict[str, Any] = {
        "id": "match-auto",
        "tenant_id": TENANT,
        "company_id": COMPANY_ID,
        "statement_id": statement_id,
        "statement_line_id": 9001,
        "line_date": _fd(),
        "line_amount_irr": -50_000,
        "line_description": "پرداخت اسنپ‌پی بابت سفارش",
        "target_type": MatchTargetType.RULE,
        "target_id": None,
        "target_ref": "rule-1",
        "target_label": "قاعدهٔ بانکی «کارمزد اسنپ‌پی»",
        "priority": 3,
        "confidence": 90,
        "status": MatchStatus.AUTO_APPLIED,
        "matched_at": NOW,
        "matched_by": "reconcile",
    }
    base.update(match_overrides)
    world["bank"].save_match(BankStatementMatch(**base))
    return statement_id, base["id"]


def test_undo_auto_code_corrects_the_mapping(world: dict) -> None:
    statement_id, match_id = _seed_auto_match(world)
    result = world["rules"].undo_auto_code(
        tenant_id=TENANT,
        company_id=COMPANY_ID,
        statement_id=statement_id,
        line_id=9001,
        corrected_account_id=501,
        actor="operator",
    )
    assert result.undone_rule_id == "rule-1"
    assert result.undone_match_id == match_id
    assert result.match.status == MatchStatus.CONFIRMED
    assert result.match.target_type == MatchTargetType.MANUAL
    line = world["engine"].get_bank_statement(BankStatementRef(statement_id)).lines[0]
    assert line.account.engine_id == 501
    matches = world["bank"].list_matches_for_statement(TENANT, statement_id)
    statuses = sorted(m.status for m in matches)
    assert statuses == [MatchStatus.CONFIRMED, MatchStatus.REJECTED]
    # The statement is still a draft: undo never validates or posts.
    view = world["engine"].get_bank_statement(BankStatementRef(statement_id))
    assert view.state == "draft"


def test_undo_auto_code_without_a_live_auto_match_is_409(world: dict) -> None:
    statement_id = world["engine"].add_statement(
        lines=[_line(amount_irr=-50_000, description="پرداخت اسنپ‌پی بابت سفارش")]
    )
    with pytest.raises(BankRuleNothingToUndoError):
        world["rules"].undo_auto_code(
            tenant_id=TENANT,
            company_id=COMPANY_ID,
            statement_id=statement_id,
            line_id=9001,
            corrected_account_id=501,
            actor="operator",
        )


def test_undo_auto_code_refuses_non_rule_matches(world: dict) -> None:
    statement_id, _ = _seed_auto_match(world, target_type=MatchTargetType.INVOICE, target_id=10, target_ref="10")
    with pytest.raises(BankRuleNothingToUndoError):
        world["rules"].undo_auto_code(
            tenant_id=TENANT,
            company_id=COMPANY_ID,
            statement_id=statement_id,
            line_id=9001,
            corrected_account_id=501,
            actor="operator",
        )


def test_undo_auto_code_on_validated_statement_is_409(world: dict) -> None:
    statement_id, _ = _seed_auto_match(world)
    view = world["engine"].statements[statement_id]
    world["engine"].statements[statement_id] = dataclasses.replace(view, state="validated")
    with pytest.raises(BankStatementNotDraftError):
        world["rules"].undo_auto_code(
            tenant_id=TENANT,
            company_id=COMPANY_ID,
            statement_id=statement_id,
            line_id=9001,
            corrected_account_id=501,
            actor="operator",
        )


def test_undo_then_manual_match_is_possible_again(world: dict) -> None:
    # After undo, the line has a CONFIRMED manual match — a second undo
    # finds no live auto match (the correction stands, not re-undone).
    statement_id, _ = _seed_auto_match(world)
    world["rules"].undo_auto_code(
        tenant_id=TENANT,
        company_id=COMPANY_ID,
        statement_id=statement_id,
        line_id=9001,
        corrected_account_id=501,
        actor="operator",
    )
    with pytest.raises(BankRuleNothingToUndoError):
        world["rules"].undo_auto_code(
            tenant_id=TENANT,
            company_id=COMPANY_ID,
            statement_id=statement_id,
            line_id=9001,
            corrected_account_id=601,
            actor="operator",
        )


# -- reconcile integration: W3 auto-coding ---------------------------------------------------


def test_reconcile_applies_w3_auto_rule(world: dict) -> None:
    rule = _rule(mode=SuggestionMode.AUTO, confirmations=3)
    world["bank_rules"].save_bank_rule(rule)
    statement_id = world["engine"].add_statement(
        lines=[_line(amount_irr=-50_000, description="پرداخت اسنپ‌پی بابت سفارش")]
    )
    result = _reconcile(world, statement_id)
    assert result.matched_count == 1
    assert result.lines[0].target_id == "rule-1"
    line = world["engine"].get_bank_statement(BankStatementRef(statement_id)).lines[0]
    assert line.account.engine_id == 601
    matches = world["bank"].list_matches_for_statement(TENANT, statement_id)
    assert matches[0].status == MatchStatus.AUTO_APPLIED
    assert matches[0].target_ref == "rule-1"


def test_reconcile_records_suggestion_for_w3_suggest_rule(world: dict) -> None:
    rule = _rule(mode=SuggestionMode.SUGGEST)
    world["bank_rules"].save_bank_rule(rule)
    statement_id = world["engine"].add_statement(
        lines=[_line(amount_irr=-50_000, description="پرداخت اسنپ‌پی بابت سفارش")]
    )
    result = _reconcile(world, statement_id)
    assert result.suggested_count == 1
    assert result.review_items_filed == 1
    suggestions = world["bank_rules"].list_suggestions(TENANT)
    assert len(suggestions) == 1
    assert suggestions[0].rule_id == "rule-1"
    assert suggestions[0].status == BankRuleSuggestionStatus.PENDING
    assert suggestions[0].target_account_id == 601
    # The line is NOT coded: a suggestion never writes the engine.
    line = world["engine"].get_bank_statement(BankStatementRef(statement_id)).lines[0]
    assert line.account.engine_id == 900


def test_reconcile_suggestion_dedup_on_rerun(world: dict) -> None:
    rule = _rule(mode=SuggestionMode.SUGGEST)
    world["bank_rules"].save_bank_rule(rule)
    statement_id = world["engine"].add_statement(
        lines=[_line(amount_irr=-50_000, description="پرداخت اسنپ‌پی بابت سفارش")]
    )
    _reconcile(world, statement_id)
    second = _reconcile(world, statement_id)
    assert second.suggested_count == 1
    assert second.review_items_filed == 0
    assert len(world["bank_rules"].list_suggestions(TENANT)) == 1


def test_reconcile_full_loop_suggest_confirm_promote_auto(world: dict) -> None:
    """The US-565 loop end to end: 3 confirms → promote → the 4th line auto-codes."""
    rule = world["rules"].create_rule(
        tenant_id=TENANT,
        company_id=COMPANY_ID,
        name="کارمزد اسنپ‌پی",
        target_account_id=601,
        description_contains="اسنپ",
        direction="out",
    )
    for index in range(3):
        statement_id = world["engine"].add_statement(
            lines=[
                _line(
                    id=9100 + index,
                    amount_irr=-50_000,
                    description="پرداخت اسنپ‌پی بابت سفارش",
                )
            ]
        )
        result = _reconcile(world, statement_id)
        assert result.suggested_count == 1
        pending = world["rules"].list_suggestions(TENANT, status="pending")
        assert len(pending) == 1
        confirmed = world["rules"].confirm_suggestion(
            tenant_id=TENANT,
            suggestion_id=pending[0].id,
            company_id=COMPANY_ID,
            actor="operator",
        )
        assert confirmed.rule is not None
        assert confirmed.rule.confirmations == index + 1
    assert world["rules"].get_rule(TENANT, rule.id).auto_eligible is True
    promoted = world["rules"].promote_rule(TENANT, rule.id)
    assert promoted.mode == SuggestionMode.AUTO
    statement_id = world["engine"].add_statement(
        lines=[_line(id=9200, amount_irr=-50_000, description="پرداخت اسنپ‌پی بابت سفارش")]
    )
    result = _reconcile(world, statement_id)
    assert result.matched_count == 1
    assert result.lines[0].target_id == rule.id


# -- the tie-out guard + the no-post guarantee -------------------------------------------------


def test_reconcile_tieout_failure_degrades_auto_to_suggested(world: dict) -> None:
    rule = _rule(mode=SuggestionMode.AUTO, confirmations=3)
    world["bank_rules"].save_bank_rule(rule)
    statement_id = world["engine"].add_statement(
        lines=[_line(amount_irr=-50_000, description="پرداخت اسنپ‌پی بابت سفارش")]
    )
    # Start 1,000,000 + line (−50,000) = 950,000, but the file says
    # the end balance is 900,000: the statement does not tie out.
    _set_balances(world, statement_id, start=1_000_000, end=900_000)
    result = _reconcile(world, statement_id)
    assert result.matched_count == 0
    assert result.suggested_count == 1
    assert "تراز نیست" in (result.lines[0].suggestions[0].reason or "")
    line = world["engine"].get_bank_statement(BankStatementRef(statement_id)).lines[0]
    assert line.account.engine_id == 900
    assert world["engine"].line_updates == []
    # No bank-rule suggestion row for an AUTO rule: it has nothing to earn.
    assert world["bank_rules"].list_suggestions(TENANT) == []


def test_reconcile_tieout_failure_degrades_learned_auto_rules_too(world: dict) -> None:
    world["bank"].save_rule(
        BankMatchRule(
            id="learned-1",
            tenant_id=TENANT,
            company_id=COMPANY_ID,
            bank_key="",
            pattern="اسنپ",
            direction="out",
            target_account_id=601,
            mode=SuggestionMode.AUTO,
            confirmations=5,
        )
    )
    statement_id = world["engine"].add_statement(lines=[_line(amount_irr=-50_000, description="پرداخت اسنپ")])
    _set_balances(world, statement_id, start=1_000_000, end=900_000)
    result = _reconcile(world, statement_id)
    assert result.matched_count == 0
    assert result.suggested_count == 1


def test_reconcile_tieout_ok_auto_codes(world: dict) -> None:
    rule = _rule(mode=SuggestionMode.AUTO, confirmations=3)
    world["bank_rules"].save_bank_rule(rule)
    statement_id = world["engine"].add_statement(
        lines=[_line(amount_irr=-50_000, description="پرداخت اسنپ‌پی بابت سفارش")]
    )
    _set_balances(world, statement_id, start=1_000_000, end=950_000)
    result = _reconcile(world, statement_id)
    assert result.matched_count == 1


def test_reconcile_without_balances_auto_codes_as_before(world: dict) -> None:
    # No balances on the statement: the tie-out cannot be checked, so
    # reconcile behaves exactly as US-514 did (the statement stays a
    # draft for a human to validate in the engine).
    rule = _rule(mode=SuggestionMode.AUTO, confirmations=3)
    world["bank_rules"].save_bank_rule(rule)
    statement_id = world["engine"].add_statement(
        lines=[_line(amount_irr=-50_000, description="پرداخت اسنپ‌پی بابت سفارش")]
    )
    result = _reconcile(world, statement_id)
    assert result.matched_count == 1


def test_reconcile_never_validates_or_posts(world: dict) -> None:
    """The policy-gate + tie-out guarantee, structurally: reconcile's
    only engine write is the line-field update; the statement stays a
    draft and no validate/post call exists on the path."""
    rule = _rule(mode=SuggestionMode.AUTO, confirmations=3)
    world["bank_rules"].save_bank_rule(rule)
    statement_id = world["engine"].add_statement(
        lines=[_line(amount_irr=-50_000, description="پرداخت اسنپ‌پی بابت سفارش")]
    )
    _reconcile(world, statement_id)
    assert len(world["engine"].line_updates) == 1
    assert world["engine"].line_updates[0].account is not None
    assert world["engine"].line_updates[0].account.engine_id == 601
    view = world["engine"].get_bank_statement(BankStatementRef(statement_id))
    assert view.state == "draft"
    # The engine port has no validate/post method for statements at
    # all — reconcile cannot call what does not exist.
    assert not hasattr(world["engine"], "validate_bank_statement")
    assert not hasattr(world["engine"], "post_bank_statement")


def test_reconcile_without_bank_rules_repo_is_unchanged(world: dict) -> None:
    """A reconcile service built without the W3 repo ignores W3 rules."""
    engine = world["engine"]
    reconcile = BankReconcileService(
        cast(AccountingEnginePort, engine),
        world["bank"],
        world["review"],
        world["queue"],
        cast(IntegrationsRegistryPort, FakeRegistry()),
        now=NOW,
    )
    world["bank_rules"].save_bank_rule(_rule(mode=SuggestionMode.AUTO, confirmations=3))
    statement_id = engine.add_statement(lines=[_line(amount_irr=-50_000, description="پرداخت اسنپ‌پی بابت سفارش")])
    result = reconcile.reconcile(
        tenant_id=TENANT,
        installation_id="install-1",
        company=COMPANY,
        company_id=COMPANY_ID,
        statement_id=statement_id,
        actor="operator",
    )
    assert result.matched_count == 0
    assert result.unmatched_count == 1
