"""SQLAlchemy bank-rules repository (US-565, Phase 5-7 ۵خ W3).

Implements :class:`BankRulesRepositoryPort` over ``bank_rules`` /
``bank_rule_suggestions``. The repository never commits: the caller
(router / service) owns the transaction.

Ordering: rules list in firing order (priority, creation time, id) —
the order the matcher evaluates them in. Suggestions list newest
first — the operator works the freshest suggestions first.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from hesabyar.domain.bank.models import (
    BankRule,
    BankRuleSuggestion,
    BankRuleSuggestionStatus,
)
from hesabyar.domain.bank.ports import BankRulesRepositoryPort
from hesabyar.domain.integrations.models import SuggestionMode
from hesabyar.infrastructure.persistence.postgres.models import (
    BankRuleModel,
    BankRuleSuggestionModel,
)


def _rule_from_row(row: BankRuleModel) -> BankRule:
    return BankRule(
        id=row.id,
        tenant_id=row.tenant_id,
        company_id=row.company_id,
        bank_key=row.bank_key,
        name=row.name,
        target_account_id=row.target_account_id,
        description_contains=row.description_contains,
        counterparty_contains=row.counterparty_contains,
        amount_min_irr=row.amount_min_irr,
        amount_max_irr=row.amount_max_irr,
        direction=row.direction,
        target_party_id=row.target_party_id,
        target_kind=row.target_kind,
        mode=SuggestionMode(row.mode),
        confirmations=row.confirmations,
        priority=row.priority,
        enabled=row.enabled,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _suggestion_from_row(row: BankRuleSuggestionModel) -> BankRuleSuggestion:
    return BankRuleSuggestion(
        id=row.id,
        tenant_id=row.tenant_id,
        rule_id=row.rule_id,
        rule_name=row.rule_name,
        company_id=row.company_id,
        statement_id=row.statement_id,
        statement_line_id=row.statement_line_id,
        line_description=row.line_description,
        line_amount_irr=row.line_amount_irr,
        target_account_id=row.target_account_id,
        target_party_id=row.target_party_id,
        status=BankRuleSuggestionStatus(row.status),
        confirmed_by=row.confirmed_by,
        confirmed_at=row.confirmed_at,
        created_at=row.created_at,
    )


class SqlAlchemyBankRulesRepository(BankRulesRepositoryPort):
    """Bank rules + suggestions over SQLAlchemy (flushes, never commits)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- bank rules -----------------------------------------------------------

    def list_bank_rules(
        self,
        tenant_id: str,
        company_id: str | None = None,
        enabled: bool | None = None,
        mode: str | None = None,
    ) -> Sequence[BankRule]:
        """Bank rules of one tenant, in firing order (priority, created, id)."""
        stmt = select(BankRuleModel).where(BankRuleModel.tenant_id == tenant_id)
        if company_id is not None:
            stmt = stmt.where(BankRuleModel.company_id == company_id)
        if enabled is not None:
            stmt = stmt.where(BankRuleModel.enabled == enabled)
        if mode is not None:
            stmt = stmt.where(BankRuleModel.mode == mode)
        stmt = stmt.order_by(
            BankRuleModel.priority.asc(),
            BankRuleModel.created_at.asc(),
            BankRuleModel.id.asc(),
        )
        return [_rule_from_row(row) for row in self._session.scalars(stmt).all()]

    def get_bank_rule(self, rule_id: str, tenant_id: str) -> BankRule | None:
        """One bank rule for the owning tenant, or None."""
        row = self._session.get(BankRuleModel, rule_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        return _rule_from_row(row)

    def save_bank_rule(self, rule: BankRule) -> BankRule:
        """Insert a new bank rule; returns it with timestamps set."""
        now = datetime.datetime.now(datetime.UTC)
        row = BankRuleModel(
            id=rule.id,
            tenant_id=rule.tenant_id,
            company_id=rule.company_id,
            bank_key=rule.bank_key,
            name=rule.name,
            description_contains=rule.description_contains,
            counterparty_contains=rule.counterparty_contains,
            amount_min_irr=rule.amount_min_irr,
            amount_max_irr=rule.amount_max_irr,
            direction=rule.direction,
            target_account_id=rule.target_account_id,
            target_party_id=rule.target_party_id,
            target_kind=rule.target_kind,
            mode=rule.mode.value,
            confirmations=rule.confirmations,
            priority=rule.priority,
            enabled=rule.enabled,
            created_by=rule.created_by,
            created_at=rule.created_at or now,
            updated_at=rule.updated_at or now,
        )
        self._session.add(row)
        self._session.flush()
        return _rule_from_row(row)

    def update_bank_rule(self, rule: BankRule) -> BankRule:
        """Persist a changed bank rule; returns it."""
        row = self._session.get(BankRuleModel, rule.id)
        assert row is not None, f"Bank rule {rule.id} vanished mid-update."
        row.bank_key = rule.bank_key
        row.name = rule.name
        row.description_contains = rule.description_contains
        row.counterparty_contains = rule.counterparty_contains
        row.amount_min_irr = rule.amount_min_irr
        row.amount_max_irr = rule.amount_max_irr
        row.direction = rule.direction
        row.target_account_id = rule.target_account_id
        row.target_party_id = rule.target_party_id
        row.target_kind = rule.target_kind
        row.mode = rule.mode.value
        row.confirmations = rule.confirmations
        row.priority = rule.priority
        row.enabled = rule.enabled
        row.updated_at = rule.updated_at or datetime.datetime.now(datetime.UTC)
        self._session.flush()
        return _rule_from_row(row)

    def delete_bank_rule(self, rule_id: str, tenant_id: str) -> bool:
        """Delete a bank rule; its suggestions survive with rule_id NULL."""
        row = self._session.get(BankRuleModel, rule_id)
        if row is None or row.tenant_id != tenant_id:
            return False
        self._session.delete(row)
        self._session.flush()
        return True

    # -- bank-rule suggestions --------------------------------------------------

    def list_suggestions(
        self,
        tenant_id: str,
        rule_id: str | None = None,
        status: BankRuleSuggestionStatus | None = None,
        company_id: str | None = None,
    ) -> Sequence[BankRuleSuggestion]:
        """Suggestions of one tenant, newest first."""
        stmt = select(BankRuleSuggestionModel).where(BankRuleSuggestionModel.tenant_id == tenant_id)
        if rule_id is not None:
            stmt = stmt.where(BankRuleSuggestionModel.rule_id == rule_id)
        if status is not None:
            stmt = stmt.where(BankRuleSuggestionModel.status == status.value)
        if company_id is not None:
            stmt = stmt.where(BankRuleSuggestionModel.company_id == company_id)
        stmt = stmt.order_by(
            BankRuleSuggestionModel.created_at.desc(),
            BankRuleSuggestionModel.id.desc(),
        )
        return [_suggestion_from_row(row) for row in self._session.scalars(stmt).all()]

    def get_suggestion(self, suggestion_id: str, tenant_id: str) -> BankRuleSuggestion | None:
        """One suggestion for the owning tenant, or None."""
        row = self._session.get(BankRuleSuggestionModel, suggestion_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        return _suggestion_from_row(row)

    def find_pending_suggestion(
        self, rule_id: str, tenant_id: str, statement_id: int, statement_line_id: int
    ) -> BankRuleSuggestion | None:
        """The pending suggestion for one (rule, statement, line), if any."""
        stmt = (
            select(BankRuleSuggestionModel)
            .where(BankRuleSuggestionModel.tenant_id == tenant_id)
            .where(BankRuleSuggestionModel.rule_id == rule_id)
            .where(BankRuleSuggestionModel.statement_id == statement_id)
            .where(BankRuleSuggestionModel.statement_line_id == statement_line_id)
            .where(BankRuleSuggestionModel.status == BankRuleSuggestionStatus.PENDING.value)
        )
        row = self._session.scalars(stmt).first()
        return _suggestion_from_row(row) if row is not None else None

    def save_suggestion(self, suggestion: BankRuleSuggestion) -> BankRuleSuggestion:
        """Insert a new suggestion; returns it with timestamps set."""
        now = datetime.datetime.now(datetime.UTC)
        row = BankRuleSuggestionModel(
            id=suggestion.id,
            tenant_id=suggestion.tenant_id,
            rule_id=suggestion.rule_id,
            rule_name=suggestion.rule_name,
            company_id=suggestion.company_id,
            statement_id=suggestion.statement_id,
            statement_line_id=suggestion.statement_line_id,
            line_description=suggestion.line_description,
            line_amount_irr=suggestion.line_amount_irr,
            target_account_id=suggestion.target_account_id,
            target_party_id=suggestion.target_party_id,
            status=suggestion.status.value,
            confirmed_by=suggestion.confirmed_by,
            confirmed_at=suggestion.confirmed_at,
            created_at=suggestion.created_at or now,
        )
        self._session.add(row)
        self._session.flush()
        return _suggestion_from_row(row)

    def update_suggestion(self, suggestion: BankRuleSuggestion) -> BankRuleSuggestion:
        """Persist a changed suggestion (status/confirmed_by); returns it."""
        row = self._session.get(BankRuleSuggestionModel, suggestion.id)
        assert row is not None, f"Bank rule suggestion {suggestion.id} vanished mid-update."
        row.status = suggestion.status.value
        row.confirmed_by = suggestion.confirmed_by
        row.confirmed_at = suggestion.confirmed_at
        self._session.flush()
        return _suggestion_from_row(row)
