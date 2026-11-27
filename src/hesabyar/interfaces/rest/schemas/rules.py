"""Request/response schemas for the rules-engine endpoints (US-561).

Responses map from the domain objects — no ORM object ever leaks.
Conditions and action params travel as raw JSON (validated by the
domain, never by pydantic field types — the DSL grammar and the
closed action set live in ``hesabyar.domain.rules``).
"""

from __future__ import annotations

from datetime import date as _date
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from hesabyar.application.commands.rules import DryRunResult
from hesabyar.domain.rules.models import (
    Rule,
    RuleExecution,
    RuleSetConfig,
)


class RuleActionSchema(BaseModel):
    """One action from the closed set, with its parameters."""

    kind: str = Field(
        description="Action kind: create_draft | post_if_policy_allows | notify | assign | tag | set_field"
    )
    params: dict[str, Any] = Field(default_factory=dict, description="Per-kind parameters (validated by the domain)")


class RuleCreateRequest(BaseModel):
    """Create a rule: ``when <event> + <conditions> → do <action>``."""

    name: str = Field(description="Operator-facing rule name")
    description: str = Field(default="", description="What the rule does")
    event_type: str = Field(description="Canonical event type that triggers the rule")
    conditions: dict[str, Any] = Field(
        default_factory=dict,
        description="Condition DSL tree (JSONLogic-style); {} = no conditions",
    )
    action: RuleActionSchema = Field(description="The action, from the closed set")
    priority: int = Field(default=100, ge=0, description="Order inside a rule set (lower runs first)")
    enabled: bool = Field(default=True, description="False stops the rule from firing live")


class RuleUpdateRequest(BaseModel):
    """Update a rule (only the given fields change)."""

    name: str | None = Field(default=None, description="Operator-facing rule name")
    description: str | None = Field(default=None, description="What the rule does")
    event_type: str | None = Field(default=None, description="Canonical event type")
    conditions: dict[str, Any] | None = Field(default=None, description="Condition DSL tree ({} = no conditions)")
    action: RuleActionSchema | None = Field(default=None, description="The action")
    priority: int | None = Field(default=None, ge=0, description="Order inside a rule set (lower runs first)")
    enabled: bool | None = Field(default=None, description="False stops live firing")


class RuleResponse(BaseModel):
    """One rule, as stored."""

    id: str = Field(description="Rule id (UUID)")
    tenant_id: str = Field(description="Owning tenant")
    installation_id: str | None = Field(description="Owning installation (null = tenant-wide rule)")
    name: str = Field(description="Operator-facing rule name")
    description: str = Field(description="What the rule does")
    event_type: str = Field(description="Canonical event type that triggers the rule")
    conditions: dict[str, Any] = Field(description="Condition DSL tree")
    action: RuleActionSchema = Field(description="The action, from the closed set")
    priority: int = Field(description="Order inside a rule set (lower runs first)")
    enabled: bool = Field(description="False stops the rule from firing live")
    created_by: str | None = Field(description="Actor that created the rule")
    created_at: datetime | None = Field(description="Creation timestamp (UTC)")
    updated_at: datetime | None = Field(description="Last-modified timestamp (UTC)")

    @classmethod
    def from_rule(cls, rule: Rule) -> RuleResponse:
        """Map a domain rule to its wire form."""
        return cls(
            id=rule.id,
            tenant_id=rule.tenant_id,
            installation_id=rule.installation_id,
            name=rule.name,
            description=rule.description,
            event_type=rule.event_type.value,
            conditions=dict(rule.conditions),
            action=RuleActionSchema(kind=rule.action.kind.value, params=dict(rule.action.params)),
            priority=rule.priority,
            enabled=rule.enabled,
            created_by=rule.created_by,
            created_at=rule.created_at,
            updated_at=rule.updated_at,
        )


class RuleExecutionResponse(BaseModel):
    """One audit-logged rule evaluation (§6.7)."""

    id: str = Field(description="Execution id (UUID)")
    tenant_id: str = Field(description="Owning tenant")
    rule_id: str | None = Field(description="Rule id (null for a dry-run of an unsaved definition or a deleted rule)")
    event_id: str | None = Field(description="Inbox event id (null for sample payloads)")
    event_type: str = Field(description="Canonical event type that was evaluated")
    mode: str = Field(description="live | dry_run")
    decision: str = Field(
        description="not_matched | would_execute | executed | blocked_by_policy_gate | skipped | failed"
    )
    outcome: str = Field(description="Machine-readable outcome code")
    action_kind: str | None = Field(description="Action kind that ran (null when not matched)")
    details: dict[str, Any] = Field(description="Evidence: gate verdicts, refs, channel results")
    error: str | None = Field(description="Failure text when the decision is failed")
    executed_by: str = Field(description="Actor that triggered the evaluation")
    executed_at: datetime | None = Field(description="Execution timestamp (UTC)")

    @classmethod
    def from_execution(cls, execution: RuleExecution) -> RuleExecutionResponse:
        """Map a domain execution to its wire form."""
        return cls(
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
            executed_at=execution.executed_at,
        )


class DryRunRequest(BaseModel):
    """Test a rule against a past event or a sample payload.

    Give either ``event_id`` (a past inbox event — the payload is
    loaded and normalized like the pipeline sees it) or
    ``event_type`` + ``payload`` (a sample payload). The context
    fields let the dry-run evaluate the policy gate for a post
    action and resolve the draft/review item the rule would act on.
    """

    event_id: str | None = Field(default=None, description="Past inbox event id (payload loaded from the inbox)")
    event_type: str | None = Field(default=None, description="Canonical event type (required with a sample payload)")
    payload: dict[str, Any] | None = Field(default=None, description="Sample payload (converted amounts in whole Rial)")
    company_engine_id: int | None = Field(default=None, description="Engine company id (for the policy gate + drafts)")
    event_date: _date | None = Field(default=None, description="Event date (for the gate's fiscal-period check)")
    channel_key: str | None = Field(default=None, description="Payment channel key")
    review_item_id: str | None = Field(default=None, description="Review item the rule would act on")
    draft_kind: str | None = Field(default=None, description="Draft kind (journal)")
    draft_engine_id: int | None = Field(default=None, description="Engine draft id the rule would post")


class DryRunResponse(BaseModel):
    """What a rule WOULD do against one event (no side effects)."""

    rule_id: str | None = Field(description="Rule id (null for an unsaved definition)")
    rule_name: str = Field(description="Rule name")
    event_type: str = Field(description="Canonical event type that was evaluated")
    matched: bool = Field(description="True when the rule matched the event")
    decision: str = Field(description="would_execute | not_matched")
    outcome: str = Field(description="Machine-readable outcome code")
    would_do: str = Field(description="Human-readable description of the action")
    details: dict[str, Any] = Field(description="Evidence (gate verdict for a post action, channel results, ...)")
    execution_id: str = Field(description="Audit-log execution id")

    @classmethod
    def from_result(cls, result: DryRunResult) -> DryRunResponse:
        """Map a dry-run result to its wire form."""
        return cls(
            rule_id=result.rule_id,
            rule_name=result.rule_name,
            event_type=result.event_type,
            matched=result.matched,
            decision=result.decision.value,
            outcome=result.outcome,
            would_do=result.would_do,
            details=dict(result.details),
            execution_id=result.execution_id,
        )


class DryRunDefinitionRequest(DryRunRequest):
    """Test an unsaved rule definition (the UI's test-before-save)."""

    name: str = Field(description="Draft rule name")
    conditions: dict[str, Any] = Field(default_factory=dict, description="Condition DSL tree ({} = no conditions)")
    action: RuleActionSchema = Field(description="The action, from the closed set")


class EvaluateRequest(BaseModel):
    """Evaluate the tenant's enabled rules against one live event.

    Give either ``event_id`` (a past inbox event) or ``event_type``
    + ``payload`` (a sample payload). This is the live trigger:
    matched rules EXECUTE their actions (through the policy gate
    for posts), and every execution is audit-logged.
    """

    event_id: str | None = Field(default=None, description="Past inbox event id (payload loaded from the inbox)")
    event_type: str | None = Field(default=None, description="Canonical event type (required with a sample payload)")
    payload: dict[str, Any] | None = Field(default=None, description="Sample payload (converted amounts in whole Rial)")
    match_mode: str | None = Field(
        default=None,
        description="Override the rule set's configured mode: first_match | all_matches",
    )
    company_engine_id: int | None = Field(default=None, description="Engine company id (for the policy gate + drafts)")
    event_date: _date | None = Field(default=None, description="Event date (for the gate's fiscal-period check)")
    channel_key: str | None = Field(default=None, description="Payment channel key")
    review_item_id: str | None = Field(default=None, description="Review item the rules may act on")
    draft_kind: str | None = Field(default=None, description="Draft kind (journal)")
    draft_engine_id: int | None = Field(default=None, description="Engine draft id the rules may post")


class EvaluateResponse(BaseModel):
    """The executions one live evaluation produced."""

    executions: list[RuleExecutionResponse] = Field(description="One execution per matched rule, in priority order")
    matched_rules: int = Field(description="How many rules matched and executed")


class RuleSetConfigRequest(BaseModel):
    """Configure a rule set's match mode."""

    match_mode: str = Field(description="first_match (Xero pattern) | all_matches")


class RuleSetConfigResponse(BaseModel):
    """One rule set's match mode."""

    tenant_id: str = Field(description="Owning tenant")
    event_type: str = Field(description="Canonical event type the set covers")
    match_mode: str = Field(description="first_match | all_matches")
    updated_at: datetime | None = Field(description="Last-modified timestamp (UTC)")

    @classmethod
    def from_config(cls, config: RuleSetConfig) -> RuleSetConfigResponse:
        """Map a domain config to its wire form."""
        return cls(
            tenant_id=config.tenant_id,
            event_type=config.event_type.value,
            match_mode=config.match_mode.value,
            updated_at=config.updated_at,
        )
