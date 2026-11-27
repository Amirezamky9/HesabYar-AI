"""User rules engine v1 (Phase 5-7 ۵خ W2, US-561; phased design §6.4).

``when <event> + <conditions> → do <action>`` over the twelve
canonical event types, with a closed safe action set, dry-run, and
a full audit log. The 3-layer policy gate (F3) is never bypassed:
no rule can auto-post without its verdict.
"""

from hesabyar.domain.rules.actions import NOTIFY_CHANNELS, parse_action
from hesabyar.domain.rules.conditions import evaluate_conditions, parse_conditions
from hesabyar.domain.rules.engine import build_facts, describe_action, select_rules
from hesabyar.domain.rules.errors import RuleDefinitionError, RuleError
from hesabyar.domain.rules.models import (
    ChannelDelivery,
    GateLayerVerdict,
    NotificationDelivery,
    Rule,
    RuleAction,
    RuleActionKind,
    RuleDraftLine,
    RuleExecution,
    RuleExecutionContext,
    RuleExecutionDecision,
    RuleExecutionMode,
    RuleGateVerdict,
    RuleSetConfig,
    RuleSetMode,
)
from hesabyar.domain.rules.ports import (
    DraftCreationPort,
    DraftPostingPort,
    NotificationPort,
    RulePolicyGatePort,
    RuleRepositoryPort,
)

__all__ = [
    "NOTIFY_CHANNELS",
    "ChannelDelivery",
    "DraftCreationPort",
    "DraftPostingPort",
    "GateLayerVerdict",
    "NotificationDelivery",
    "NotificationPort",
    "Rule",
    "RuleAction",
    "RuleActionKind",
    "RuleDefinitionError",
    "RuleDraftLine",
    "RuleError",
    "RuleExecution",
    "RuleExecutionContext",
    "RuleExecutionDecision",
    "RuleExecutionMode",
    "RuleGateVerdict",
    "RulePolicyGatePort",
    "RuleRepositoryPort",
    "RuleSetConfig",
    "RuleSetMode",
    "build_facts",
    "describe_action",
    "evaluate_conditions",
    "parse_action",
    "parse_conditions",
    "select_rules",
]
