"""Bank auto-coding rules use cases (US-565, Phase 5-7 ۵خ W3).

User-defined Xero-style bank rules: ``when <statement-line pattern>
→ suggest <account + mapping>``. Rules are born in SUGGEST mode and
become AUTO only through a human promotion, once
``RULE_AUTO_MIN_CONFIRMATIONS`` (3) of their suggestions have been
confirmed — the same Suggest → Auto pattern as the US-552
mapping-learning promotion. B2 learned mappings (``bank_match_rules``)
never enter here as rules: they stay suggestions in the reconcile
flow (see ``BankReconcileService``).

Boundaries, documented in docs/bank-rules.md:

- Auto-coding never posts anything: reconcile only writes line
  fields (account/party) on DRAFT statements, and the statement is
  validated (posted) by a human in the engine, where Tryton's own
  balance check is the tie-out. A statement that does not tie out
  never auto-codes at all (the reconcile service degrades AUTO
  decisions to suggestions — see ``bank_reconcile``).
- A wrong auto-code can be undone while the statement is a draft:
  :meth:`BankRulesService.undo_auto_code` rejects the AUTO_APPLIED
  match and applies the corrected coding (the mapping is corrected;
  the journal stays a draft until approved).
- Confirming a suggestion applies the coding to the line (the engine
  write) AND counts toward promotion; rejecting one rejects the
  reconcile's suggested match too, so the line never re-suggests
  from that rule.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.application.errors import ReviewInvalidStateError
from hesabyar.domain.accounting.commands import UpdateBankStatementLine
from hesabyar.domain.accounting.ports import AccountingEnginePort
from hesabyar.domain.accounting.refs import (
    AccountRef,
    BankStatementLineRef,
    BankStatementRef,
    PartyRef,
)
from hesabyar.domain.bank.errors import (
    BankLineAlreadyMatchedError,
    BankRuleInvalidError,
    BankRuleNotFoundError,
    BankRuleNothingToUndoError,
    BankRuleNotPromotableError,
    BankRuleSuggestionNotFoundError,
    BankRuleSuggestionNotPendingError,
    BankStatementLineNotFoundError,
    BankStatementNotDraftError,
)
from hesabyar.domain.bank.matching import BANK_RULE_CONFIDENCE
from hesabyar.domain.bank.models import (
    RULE_AUTO_MIN_CONFIRMATIONS,
    BankRule,
    BankRuleSuggestion,
    BankRuleSuggestionStatus,
    BankStatementMatch,
    MatchStatus,
    MatchTargetType,
)
from hesabyar.domain.bank.parsing import normalize_persian_text
from hesabyar.domain.bank.ports import BankRepositoryPort, BankRulesRepositoryPort
from hesabyar.domain.common.errors import BankStatementNotFoundError
from hesabyar.domain.integrations.models import SuggestionMode

#: Fields whose change resets a rule's trust (confirmations + mode):
#: the confirmations were earned for the OLD definition, not the new
#: one. Name, priority and enabled are not trust-bearing.
_DEFINITION_FIELDS = frozenset(
    {
        "bank_key",
        "description_contains",
        "counterparty_contains",
        "amount_min_irr",
        "amount_max_irr",
        "direction",
        "target_account_id",
        "target_party_id",
        "target_kind",
    }
)

#: Fields an update may carry (anything else is a 422).
_UPDATABLE_FIELDS = _DEFINITION_FIELDS | {"name", "priority", "enabled"}

#: Maximum rule-name length (mirrors the column).
_MAX_NAME_LENGTH = 200


@dataclass(frozen=True)
class ConfirmSuggestionResult:
    """What confirming a suggestion produced: the match + the rule's new state.

    ``rule`` is None when the rule was deleted after the suggestion
    was recorded — the coding still applies (from the suggestion's
    snapshot), but there is no rule left to credit the confirmation to.
    """

    match: BankStatementMatch
    rule: BankRule | None


@dataclass(frozen=True)
class UndoAutoCodeResult:
    """What undoing an auto-code produced: the corrected match + the undone rule."""

    match: BankStatementMatch
    undone_rule_id: str
    undone_match_id: str


def _normalised_or_none(value: str | None) -> str | None:
    """Normalise a pattern fragment; a blank fragment is no condition."""
    if value is None:
        return None
    normalised = normalize_persian_text(value)
    return normalised or None


def _validate_definition(
    *,
    name: str,
    description_contains: str | None,
    counterparty_contains: str | None,
    amount_min_irr: int | None,
    amount_max_irr: int | None,
    direction: str,
    target_account_id: int,
    target_party_id: int | None,
) -> None:
    """Validate a bank-rule definition (raises ``BankRuleInvalidError``)."""
    if not name.strip():
        raise BankRuleInvalidError("A bank rule needs a name.", details={"field": "name"})
    if len(name) > _MAX_NAME_LENGTH:
        raise BankRuleInvalidError(
            f"A bank rule name is at most {_MAX_NAME_LENGTH} characters.",
            details={"field": "name"},
        )
    if (
        description_contains is None
        and counterparty_contains is None
        and amount_min_irr is None
        and amount_max_irr is None
    ):
        raise BankRuleInvalidError(
            "A bank rule needs at least one pattern condition "
            "(description, counterparty, or an amount bound) — a rule "
            "with no condition would match every line.",
            details={"field": "pattern"},
        )
    if direction not in ("in", "out", "any"):
        raise BankRuleInvalidError(
            f"Unknown direction {direction!r} (expected 'in', 'out' or 'any').",
            details={"field": "direction"},
        )
    if amount_min_irr is not None and amount_min_irr < 0:
        raise BankRuleInvalidError(
            "The minimum amount must not be negative.",
            details={"field": "amount_min_irr"},
        )
    if amount_max_irr is not None and amount_max_irr < 0:
        raise BankRuleInvalidError(
            "The maximum amount must not be negative.",
            details={"field": "amount_max_irr"},
        )
    if amount_min_irr is not None and amount_max_irr is not None and amount_min_irr > amount_max_irr:
        raise BankRuleInvalidError(
            "The minimum amount must not exceed the maximum amount.",
            details={"field": "amount_min_irr"},
        )
    if target_account_id <= 0:
        raise BankRuleInvalidError(
            "A bank rule needs a positive target account id.",
            details={"field": "target_account_id"},
        )
    if target_party_id is not None and target_party_id <= 0:
        raise BankRuleInvalidError(
            "A target party id must be positive.",
            details={"field": "target_party_id"},
        )


class BankRulesService:
    """Bank auto-coding rules: CRUD, Suggest → Auto, suggestions, undo."""

    def __init__(
        self,
        bank_rules: BankRulesRepositoryPort,
        bank: BankRepositoryPort,
        engine: AccountingEnginePort,
        *,
        review: ReviewQueueCommandService | None = None,
        now: datetime.datetime | None = None,
    ) -> None:
        self._bank_rules = bank_rules
        self._bank = bank
        self._engine = engine
        self._review = review
        self._now = now

    def _clock(self) -> datetime.datetime:
        if self._now is not None:
            return self._now
        return datetime.datetime.now(datetime.UTC)

    # -- rules: CRUD ----------------------------------------------------------

    def list_rules(
        self,
        tenant_id: str,
        company_id: str | None = None,
        enabled: bool | None = None,
        mode: str | None = None,
    ) -> Sequence[BankRule]:
        """Bank rules of one tenant, in firing order."""
        if mode is not None and mode not in ("suggest", "auto"):
            raise BankRuleInvalidError(
                f"Unknown mode filter {mode!r} (expected 'suggest' or 'auto').",
                details={"field": "mode"},
            )
        return self._bank_rules.list_bank_rules(tenant_id, company_id=company_id, enabled=enabled, mode=mode)

    def create_rule(
        self,
        *,
        tenant_id: str,
        company_id: str,
        name: str,
        target_account_id: int,
        bank_key: str = "",
        description_contains: str | None = None,
        counterparty_contains: str | None = None,
        amount_min_irr: int | None = None,
        amount_max_irr: int | None = None,
        direction: str = "any",
        target_party_id: int | None = None,
        target_kind: str = "account",
        priority: int = 100,
        enabled: bool = True,
        actor: str | None = None,
    ) -> BankRule:
        """Create a bank rule — always born in SUGGEST mode.

        There is no way to create an AUTO rule directly: a rule
        becomes automatic only through a human promotion after the
        confirmation threshold, never at creation.
        """
        description = _normalised_or_none(description_contains)
        counterparty = _normalised_or_none(counterparty_contains)
        _validate_definition(
            name=name,
            description_contains=description,
            counterparty_contains=counterparty,
            amount_min_irr=amount_min_irr,
            amount_max_irr=amount_max_irr,
            direction=direction,
            target_account_id=target_account_id,
            target_party_id=target_party_id,
        )
        now = self._clock()
        return self._bank_rules.save_bank_rule(
            BankRule(
                id=str(uuid.uuid4()),
                tenant_id=tenant_id,
                company_id=company_id,
                bank_key=bank_key,
                name=name.strip(),
                target_account_id=target_account_id,
                description_contains=description,
                counterparty_contains=counterparty,
                amount_min_irr=amount_min_irr,
                amount_max_irr=amount_max_irr,
                direction=direction,
                target_party_id=target_party_id,
                target_kind=target_kind,
                mode=SuggestionMode.SUGGEST,
                confirmations=0,
                priority=priority,
                enabled=enabled,
                created_by=actor,
                created_at=now,
                updated_at=now,
            )
        )

    def get_rule(self, tenant_id: str, rule_id: str) -> BankRule:
        """One bank rule (404 when it is not in this tenant)."""
        rule = self._bank_rules.get_bank_rule(rule_id, tenant_id)
        if rule is None:
            raise BankRuleNotFoundError(
                f"Bank rule {rule_id!r} does not exist.",
                details={"rule_id": rule_id},
            )
        return rule

    def update_rule(self, tenant_id: str, rule_id: str, fields: dict[str, Any]) -> BankRule:
        """Update a bank rule (only the given fields change).

        ``fields`` carries exactly the fields the caller set (explicit
        nulls clear a pattern condition). Changing the pattern or the
        target resets the confirmations and drops an AUTO rule back to
        SUGGEST — the trust was earned for the old definition.
        """
        unknown = set(fields) - _UPDATABLE_FIELDS
        if unknown:
            raise BankRuleInvalidError(
                f"Unknown bank-rule fields: {', '.join(sorted(unknown))}.",
                details={"fields": sorted(unknown)},
            )
        current = self.get_rule(tenant_id, rule_id)
        changes: dict[str, Any] = {}
        for key, value in fields.items():
            if key in ("description_contains", "counterparty_contains"):
                changes[key] = _normalised_or_none(value)
            elif key == "name" and isinstance(value, str):
                changes[key] = value.strip()
            else:
                changes[key] = value
        merged = dataclasses.replace(current, **changes)
        _validate_definition(
            name=merged.name,
            description_contains=merged.description_contains,
            counterparty_contains=merged.counterparty_contains,
            amount_min_irr=merged.amount_min_irr,
            amount_max_irr=merged.amount_max_irr,
            direction=merged.direction,
            target_account_id=merged.target_account_id,
            target_party_id=merged.target_party_id,
        )
        definition_changed = any(getattr(merged, key) != getattr(current, key) for key in _DEFINITION_FIELDS)
        if definition_changed:
            merged = dataclasses.replace(merged, mode=SuggestionMode.SUGGEST, confirmations=0)
        return self._bank_rules.update_bank_rule(dataclasses.replace(merged, updated_at=self._clock()))

    def delete_rule(self, tenant_id: str, rule_id: str) -> None:
        """Delete a bank rule (its suggestions survive, rule_id NULL)."""
        self.get_rule(tenant_id, rule_id)
        self._bank_rules.delete_bank_rule(rule_id, tenant_id)

    def set_enabled(self, tenant_id: str, rule_id: str, enabled: bool) -> BankRule:
        """Enable/disable a bank rule (a disabled rule never fires)."""
        rule = self.get_rule(tenant_id, rule_id)
        return self._bank_rules.update_bank_rule(dataclasses.replace(rule, enabled=enabled, updated_at=self._clock()))

    # -- Suggest → Auto --------------------------------------------------------

    def promote_rule(self, tenant_id: str, rule_id: str) -> BankRule:
        """Promote a SUGGEST rule to AUTO — a human decision, at threshold.

        Promotion needs SUGGEST mode and at least
        ``RULE_AUTO_MIN_CONFIRMATIONS`` confirmed suggestions.
        Promoting is not a confirmation: the count carries unchanged.
        """
        rule = self.get_rule(tenant_id, rule_id)
        if rule.mode != SuggestionMode.SUGGEST:
            raise BankRuleNotPromotableError(
                f"Bank rule {rule_id!r} is already {rule.mode.value}; only a SUGGEST rule can be promoted.",
                details={"rule_id": rule_id, "mode": rule.mode.value},
            )
        if rule.confirmations < RULE_AUTO_MIN_CONFIRMATIONS:
            raise BankRuleNotPromotableError(
                f"Bank rule {rule_id!r} has {rule.confirmations} confirmed "
                f"suggestions; promotion needs {RULE_AUTO_MIN_CONFIRMATIONS}.",
                details={
                    "rule_id": rule_id,
                    "confirmations": rule.confirmations,
                    "required": RULE_AUTO_MIN_CONFIRMATIONS,
                },
            )
        return self._bank_rules.update_bank_rule(
            dataclasses.replace(rule, mode=SuggestionMode.AUTO, updated_at=self._clock())
        )

    def demote_rule(self, tenant_id: str, rule_id: str) -> BankRule:
        """Demote an AUTO rule back to SUGGEST — the trust is re-earned.

        The confirmations reset to zero: a rule demoted after a wrong
        auto-code must earn its three confirmations again before it
        can be re-promoted.
        """
        rule = self.get_rule(tenant_id, rule_id)
        if rule.mode != SuggestionMode.AUTO:
            raise BankRuleNotPromotableError(
                f"Bank rule {rule_id!r} is {rule.mode.value}; only an AUTO rule can be demoted.",
                details={"rule_id": rule_id, "mode": rule.mode.value},
            )
        return self._bank_rules.update_bank_rule(
            dataclasses.replace(
                rule,
                mode=SuggestionMode.SUGGEST,
                confirmations=0,
                updated_at=self._clock(),
            )
        )

    # -- suggestions ------------------------------------------------------------

    def list_suggestions(
        self,
        tenant_id: str,
        rule_id: str | None = None,
        status: str | None = None,
        company_id: str | None = None,
    ) -> Sequence[BankRuleSuggestion]:
        """Suggestions of one tenant, newest first."""
        wanted: BankRuleSuggestionStatus | None = None
        if status is not None:
            try:
                wanted = BankRuleSuggestionStatus(status)
            except ValueError:
                raise BankRuleInvalidError(
                    f"Unknown suggestion status {status!r} (expected 'pending', 'confirmed' or 'rejected').",
                    details={"field": "status"},
                ) from None
        return self._bank_rules.list_suggestions(tenant_id, rule_id=rule_id, status=wanted, company_id=company_id)

    def get_suggestion(self, tenant_id: str, suggestion_id: str) -> BankRuleSuggestion:
        """One suggestion (404 when it is not in this tenant)."""
        suggestion = self._bank_rules.get_suggestion(suggestion_id, tenant_id)
        if suggestion is None:
            raise BankRuleSuggestionNotFoundError(
                f"Bank rule suggestion {suggestion_id!r} does not exist.",
                details={"suggestion_id": suggestion_id},
            )
        return suggestion

    def confirm_suggestion(
        self, *, tenant_id: str, suggestion_id: str, company_id: str, actor: str
    ) -> ConfirmSuggestionResult:
        """Confirm a suggestion: apply the coding + count toward promotion.

        The line's account/party are written through the engine port
        (the statement must still be a draft; a line with a live match
        is refused), the match is recorded CONFIRMED, the suggestion
        becomes confirmed, and the rule's confirmation count grows by
        one. The linked ``bank_unmatched`` review item (when the
        reconcile filed one) is resolved with ``manual_match``.
        """
        suggestion = self.get_suggestion(tenant_id, suggestion_id)
        if suggestion.status != BankRuleSuggestionStatus.PENDING:
            raise BankRuleSuggestionNotPendingError(
                f"Bank rule suggestion {suggestion_id!r} is already {suggestion.status.value}.",
                details={"suggestion_id": suggestion_id, "status": suggestion.status.value},
            )
        statement_id = suggestion.statement_id
        line_id = suggestion.statement_line_id
        view = self._engine.get_bank_statement(BankStatementRef(statement_id))
        if view is None:
            raise BankStatementNotFoundError(
                f"Bank statement {statement_id} does not exist in the engine.",
                details={"statement_id": statement_id},
            )
        if view.state != "draft":
            raise BankStatementNotDraftError(
                f"Bank statement {statement_id} is {view.state}, not a draft; its lines are read-only.",
                details={"statement_id": statement_id, "state": view.state},
            )
        line = next((item for item in view.lines if item.id == line_id), None)
        if line is None:
            raise BankStatementLineNotFoundError(
                f"Statement line {line_id} is not on statement {statement_id}.",
                details={"statement_id": statement_id, "line_id": line_id},
            )
        existing = self._bank.get_match_for_line(tenant_id, statement_id, line_id)
        if existing is not None and existing.status in (
            MatchStatus.AUTO_APPLIED,
            MatchStatus.CONFIRMED,
        ):
            raise BankLineAlreadyMatchedError(
                f"Statement line {line_id} already has a {existing.status.value} match.",
                details={"statement_id": statement_id, "line_id": line_id},
            )
        self._engine.update_bank_statement_line(
            UpdateBankStatementLine(
                statement=BankStatementRef(statement_id),
                line=BankStatementLineRef(line_id),
                account=AccountRef(suggestion.target_account_id),
                party=(PartyRef(suggestion.target_party_id) if suggestion.target_party_id else None),
            )
        )
        # A prior suggestion for this line is superseded by the human
        # decision; the destination + review item carry onto the new match.
        prior_suggested: BankStatementMatch | None = None
        for prior in self._bank.list_matches_for_statement(tenant_id, statement_id):
            if prior.statement_line_id == line_id and prior.status == MatchStatus.SUGGESTED:
                self._bank.update_match(dataclasses.replace(prior, status=MatchStatus.REJECTED))
                if prior_suggested is None:
                    prior_suggested = prior
        now = self._clock()
        match = self._bank.save_match(
            BankStatementMatch(
                id=str(uuid.uuid4()),
                tenant_id=tenant_id,
                company_id=company_id,
                statement_id=statement_id,
                statement_line_id=line_id,
                line_date=line.date,
                line_amount_irr=line.amount.amount,
                line_description=line.description,
                target_type=MatchTargetType.RULE,
                target_id=None,
                target_ref=(suggestion.rule_id if suggestion.rule_id is not None else f"suggestion:{suggestion.id}"),
                target_label=f"قاعدهٔ بانکی «{suggestion.rule_name}»",
                priority=3,
                confidence=BANK_RULE_CONFIDENCE,
                status=MatchStatus.CONFIRMED,
                destination_account_id=(prior_suggested.destination_account_id if prior_suggested else None),
                destination_shaba=(prior_suggested.destination_shaba if prior_suggested else None),
                review_item_id=(prior_suggested.review_item_id if prior_suggested else None),
                matched_at=now,
                matched_by=actor,
            )
        )
        self._bank_rules.update_suggestion(
            dataclasses.replace(
                suggestion,
                status=BankRuleSuggestionStatus.CONFIRMED,
                confirmed_by=actor,
                confirmed_at=now,
            )
        )
        rule: BankRule | None = None
        if suggestion.rule_id is not None:
            rule = self._bank_rules.get_bank_rule(suggestion.rule_id, tenant_id)
            if rule is not None:
                rule = self._bank_rules.update_bank_rule(
                    dataclasses.replace(rule, confirmations=rule.confirmations + 1, updated_at=now)
                )
        if self._review is not None and prior_suggested is not None and prior_suggested.review_item_id is not None:
            try:
                self._review.perform_action(
                    item_id=prior_suggested.review_item_id,
                    tenant_id=tenant_id,
                    action_code="manual_match",
                    actor=actor,
                    note="پیشنهاد قاعدهٔ بانکی تأیید شد.",
                )
            except ReviewInvalidStateError:
                pass
        return ConfirmSuggestionResult(match=match, rule=rule)

    def reject_suggestion(self, *, tenant_id: str, suggestion_id: str, actor: str) -> BankRuleSuggestion:
        """Reject a suggestion: the line never re-suggests from that rule.

        The reconcile's suggested match for the line is rejected too
        (a rejected suggestion never re-suggests for that line); the
        rule's confirmations are untouched — a rejection is not a
        negative confirmation, it is one line's decision.
        """
        suggestion = self.get_suggestion(tenant_id, suggestion_id)
        if suggestion.status != BankRuleSuggestionStatus.PENDING:
            raise BankRuleSuggestionNotPendingError(
                f"Bank rule suggestion {suggestion_id!r} is already {suggestion.status.value}.",
                details={"suggestion_id": suggestion_id, "status": suggestion.status.value},
            )
        for prior in self._bank.list_matches_for_statement(tenant_id, suggestion.statement_id):
            if (
                prior.statement_line_id == suggestion.statement_line_id
                and prior.status == MatchStatus.SUGGESTED
                and prior.target_type == MatchTargetType.RULE
                and suggestion.rule_id is not None
                and prior.target_ref == suggestion.rule_id
            ):
                self._bank.update_match(dataclasses.replace(prior, status=MatchStatus.REJECTED))
        return self._bank_rules.update_suggestion(
            dataclasses.replace(
                suggestion,
                status=BankRuleSuggestionStatus.REJECTED,
                confirmed_by=actor,
                confirmed_at=self._clock(),
            )
        )

    # -- undo ---------------------------------------------------------------------

    def undo_auto_code(
        self,
        *,
        tenant_id: str,
        company_id: str,
        statement_id: int,
        line_id: int,
        corrected_account_id: int,
        corrected_party_id: int | None = None,
        actor: str,
    ) -> UndoAutoCodeResult:
        """Undo a wrong auto-code: reject it, apply the corrected coding.

        Only a live AUTO_APPLIED match whose target is a rule can be
        undone, and only while the statement is a draft (a validated
        statement's lines are read-only in Tryton — the correction
        then belongs to the engine, not here). The corrected account
        is required: the engine line cannot be left uncoded (Tryton
        requires an account on every statement line), so "undo"
        always means "correct the mapping", never "clear it". The
        journal stays a draft until the accountant approves it — undo
        writes line fields only, never validates or posts.
        """
        if corrected_account_id <= 0:
            raise BankRuleInvalidError(
                "Undo needs a positive corrected account id.",
                details={"field": "corrected_account_id"},
            )
        if corrected_party_id is not None and corrected_party_id <= 0:
            raise BankRuleInvalidError(
                "A corrected party id must be positive.",
                details={"field": "corrected_party_id"},
            )
        view = self._engine.get_bank_statement(BankStatementRef(statement_id))
        if view is None:
            raise BankStatementNotFoundError(
                f"Bank statement {statement_id} does not exist in the engine.",
                details={"statement_id": statement_id},
            )
        if view.state != "draft":
            raise BankStatementNotDraftError(
                f"Bank statement {statement_id} is {view.state}, not a draft; its lines are read-only.",
                details={"statement_id": statement_id, "state": view.state},
            )
        line = next((item for item in view.lines if item.id == line_id), None)
        if line is None:
            raise BankStatementLineNotFoundError(
                f"Statement line {line_id} is not on statement {statement_id}.",
                details={"statement_id": statement_id, "line_id": line_id},
            )
        live = self._bank.get_match_for_line(tenant_id, statement_id, line_id)
        if live is None or live.status != MatchStatus.AUTO_APPLIED or live.target_type != MatchTargetType.RULE:
            raise BankRuleNothingToUndoError(
                f"Statement line {line_id} has no live auto-coded match to undo.",
                details={"statement_id": statement_id, "line_id": line_id},
            )
        self._engine.update_bank_statement_line(
            UpdateBankStatementLine(
                statement=BankStatementRef(statement_id),
                line=BankStatementLineRef(line_id),
                account=AccountRef(corrected_account_id),
                party=PartyRef(corrected_party_id) if corrected_party_id else None,
            )
        )
        self._bank.update_match(dataclasses.replace(live, status=MatchStatus.REJECTED))
        now = self._clock()
        match = self._bank.save_match(
            BankStatementMatch(
                id=str(uuid.uuid4()),
                tenant_id=tenant_id,
                company_id=company_id,
                statement_id=statement_id,
                statement_line_id=line_id,
                line_date=line.date,
                line_amount_irr=line.amount.amount,
                line_description=line.description,
                target_type=MatchTargetType.MANUAL,
                target_id=None,
                target_ref="manual",
                target_label="اصلاح کدینگ خودکار",
                priority=3,
                confidence=100,
                status=MatchStatus.CONFIRMED,
                destination_account_id=live.destination_account_id,
                destination_shaba=live.destination_shaba,
                matched_at=now,
                matched_by=actor,
            )
        )
        return UndoAutoCodeResult(match=match, undone_rule_id=live.target_ref, undone_match_id=live.id)
