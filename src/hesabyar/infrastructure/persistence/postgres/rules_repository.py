"""SQLAlchemy rules repository (US-561, Phase 5-7 ۵خ W2).

Implements :class:`RuleRepositoryPort` over ``rules`` /
``rule_executions`` / ``rule_set_configs``. The repository never
commits: the caller (router / service) owns the transaction.

Installation scoping: a rule with a NULL ``installation_id`` is
tenant-wide — it fires for every installation of the tenant. Listing
with an installation id returns that installation's rules PLUS the
tenant-wide ones, in priority order; listing with
``installation_id=None`` returns only the tenant-wide rules.

Stored rules are re-validated on read (conditions + action): a row
written outside the service (or by a future build with a new action
kind) raises :class:`RuleDefinitionError` instead of executing an
action this build does not know.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from hesabyar.domain.events.envelope import CanonicalEventType, parse_event_type
from hesabyar.domain.rules.actions import parse_action
from hesabyar.domain.rules.conditions import parse_conditions
from hesabyar.domain.rules.models import (
    Rule,
    RuleExecution,
    RuleExecutionDecision,
    RuleExecutionMode,
    RuleSetConfig,
    RuleSetMode,
)
from hesabyar.domain.rules.ports import RuleRepositoryPort
from hesabyar.infrastructure.persistence.postgres.models import (
    RuleExecutionModel,
    RuleModel,
    RuleSetConfigModel,
)


def _rule_from_row(row: RuleModel) -> Rule:
    return Rule(
        id=row.id,
        tenant_id=row.tenant_id,
        installation_id=row.installation_id,
        name=row.name,
        description=row.description,
        event_type=parse_event_type(row.event_type),
        conditions=parse_conditions(dict(row.conditions or {})),
        action=parse_action(dict(row.action)),
        priority=row.priority,
        enabled=row.enabled,
        created_at=row.created_at,
        updated_at=row.updated_at,
        created_by=row.created_by,
    )


def _execution_from_row(row: RuleExecutionModel) -> RuleExecution:
    return RuleExecution(
        id=row.id,
        tenant_id=row.tenant_id,
        rule_id=row.rule_id,
        event_id=row.event_id,
        event_type=row.event_type,
        mode=RuleExecutionMode(row.mode),
        decision=RuleExecutionDecision(row.decision),
        outcome=row.outcome,
        action_kind=row.action_kind,
        details=dict(row.details or {}),
        error=row.error,
        executed_by=row.executed_by,
        executed_at=row.executed_at,
    )


def _config_from_row(row: RuleSetConfigModel) -> RuleSetConfig:
    return RuleSetConfig(
        tenant_id=row.tenant_id,
        event_type=parse_event_type(row.event_type),
        match_mode=RuleSetMode(row.match_mode),
        updated_at=row.updated_at,
    )


class SqlAlchemyRulesRepository(RuleRepositoryPort):
    """Rules + executions + set configs over SQLAlchemy (flushes, never commits)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- rules --------------------------------------------------------------

    def save_rule(self, rule: Rule) -> Rule:
        """Insert or update a rule (flushes; the caller commits)."""
        row = self._session.get(RuleModel, rule.id)
        now = datetime.datetime.now(datetime.UTC)
        if row is None:
            row = RuleModel(
                id=rule.id,
                tenant_id=rule.tenant_id,
                installation_id=rule.installation_id,
                name=rule.name,
                description=rule.description,
                event_type=rule.event_type.value,
                conditions=dict(rule.conditions),
                action=rule.action.to_dict(),
                priority=rule.priority,
                enabled=rule.enabled,
                created_by=rule.created_by,
                created_at=rule.created_at or now,
                updated_at=rule.updated_at or now,
            )
            self._session.add(row)
        else:
            row.tenant_id = rule.tenant_id
            row.installation_id = rule.installation_id
            row.name = rule.name
            row.description = rule.description
            row.event_type = rule.event_type.value
            row.conditions = dict(rule.conditions)
            row.action = rule.action.to_dict()
            row.priority = rule.priority
            row.enabled = rule.enabled
            row.updated_at = rule.updated_at or now
        self._session.flush()
        return _rule_from_row(row)

    def get_rule(self, tenant_id: str, rule_id: str) -> Rule | None:
        """One rule of a tenant, or None (also when another tenant's)."""
        row = self._session.get(RuleModel, rule_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        return _rule_from_row(row)

    def list_rules(
        self,
        tenant_id: str,
        *,
        installation_id: str | None = None,
        event_type: CanonicalEventType | None = None,
        enabled: bool | None = None,
    ) -> Sequence[Rule]:
        """A tenant's rules, in priority order (ties: created_at, id).

        An installation id lists that installation's rules plus the
        tenant-wide ones (NULL installation); None lists only the
        tenant-wide rules.
        """
        stmt = select(RuleModel).where(RuleModel.tenant_id == tenant_id)
        if installation_id is None:
            stmt = stmt.where(RuleModel.installation_id.is_(None))
        else:
            stmt = stmt.where(
                or_(
                    RuleModel.installation_id == installation_id,
                    RuleModel.installation_id.is_(None),
                )
            )
        if event_type is not None:
            stmt = stmt.where(RuleModel.event_type == event_type.value)
        if enabled is not None:
            stmt = stmt.where(RuleModel.enabled == enabled)
        stmt = stmt.order_by(
            RuleModel.priority.asc(),
            RuleModel.created_at.asc(),
            RuleModel.id.asc(),
        )
        rows = list(self._session.execute(stmt).scalars())
        return [_rule_from_row(row) for row in rows]

    def delete_rule(self, tenant_id: str, rule_id: str) -> bool:
        """Delete a rule; False when it does not exist in this tenant."""
        row = self._session.get(RuleModel, rule_id)
        if row is None or row.tenant_id != tenant_id:
            return False
        self._session.delete(row)
        self._session.flush()
        return True

    # -- executions -----------------------------------------------------------

    def save_execution(self, execution: RuleExecution) -> RuleExecution:
        """Append one execution record (flushes; the caller commits)."""
        row = RuleExecutionModel(
            id=execution.id,
            tenant_id=execution.tenant_id,
            rule_id=execution.rule_id,
            event_id=execution.event_id,
            event_type=execution.event_type,
            mode=execution.mode.value,
            decision=execution.decision.value,
            outcome=execution.outcome,
            action_kind=execution.action_kind,
            details=dict(execution.details),
            error=execution.error,
            executed_by=execution.executed_by,
            executed_at=execution.executed_at or datetime.datetime.now(datetime.UTC),
        )
        self._session.add(row)
        self._session.flush()
        return _execution_from_row(row)

    def get_execution(self, tenant_id: str, execution_id: str) -> RuleExecution | None:
        """One execution of a tenant, or None."""
        row = self._session.get(RuleExecutionModel, execution_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        return _execution_from_row(row)

    def list_executions(
        self,
        tenant_id: str,
        *,
        rule_id: str | None = None,
        event_id: str | None = None,
        limit: int = 50,
    ) -> Sequence[RuleExecution]:
        """A tenant's executions, newest first."""
        stmt = select(RuleExecutionModel).where(RuleExecutionModel.tenant_id == tenant_id)
        if rule_id is not None:
            stmt = stmt.where(RuleExecutionModel.rule_id == rule_id)
        if event_id is not None:
            stmt = stmt.where(RuleExecutionModel.event_id == event_id)
        stmt = stmt.order_by(
            RuleExecutionModel.executed_at.desc(),
            RuleExecutionModel.id.desc(),
        ).limit(limit)
        rows = list(self._session.execute(stmt).scalars())
        return [_execution_from_row(row) for row in rows]

    # -- rule-set configs -------------------------------------------------------

    def get_set_config(self, tenant_id: str, event_type: CanonicalEventType) -> RuleSetConfig | None:
        """A rule set's stored config, or None (the default applies)."""
        row = self._session.get(RuleSetConfigModel, (tenant_id, event_type.value))
        if row is None:
            return None
        return _config_from_row(row)

    def save_set_config(self, config: RuleSetConfig) -> RuleSetConfig:
        """Insert or replace a rule set's config (flushes; the caller commits)."""
        row = self._session.get(RuleSetConfigModel, (config.tenant_id, config.event_type.value))
        now = datetime.datetime.now(datetime.UTC)
        if row is None:
            row = RuleSetConfigModel(
                tenant_id=config.tenant_id,
                event_type=config.event_type.value,
                match_mode=config.match_mode.value,
                updated_at=config.updated_at or now,
            )
            self._session.add(row)
        else:
            row.match_mode = config.match_mode.value
            row.updated_at = config.updated_at or now
        self._session.flush()
        return _config_from_row(row)

    def list_set_configs(self, tenant_id: str) -> Sequence[RuleSetConfig]:
        """A tenant's stored rule-set configs, ordered by event type."""
        rows = list(
            self._session.execute(
                select(RuleSetConfigModel)
                .where(RuleSetConfigModel.tenant_id == tenant_id)
                .order_by(RuleSetConfigModel.event_type.asc())
            ).scalars()
        )
        return [_config_from_row(row) for row in rows]
