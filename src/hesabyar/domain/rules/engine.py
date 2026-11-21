"""Pure rule matching: facts, selection, dry-run description (US-561).

This module is pure (no I/O): given rules and an event, it decides
*which* rules match and in what order. The application layer
(:mod:`hesabyar.application.commands.rules`) performs the actions
through ports and records the executions.

Facts
-----
:func:`build_facts` merges the event's converted payload (amounts in
whole Rial — the pipeline already converted Toman at the boundary)
with the execution context into one flat lookup for the DSL:

- ``event_type`` — the canonical type string.
- ``payload`` — the converted payload (fields under ``payload.<path>``).
- ``amount_irr`` — the event's best-effort total (``total_amount``,
  ``gross_amount``, ``refund_amount``, ``amount`` — first integer
  found, mirroring ``NormalizedEvent.total_amount_rial``); absent
  when the payload carries no total.
- ``channel`` — the payment channel (``payment_channel`` payload
  field, else the context's channel key).
- ``party`` — the party id (``party_id`` / ``customer_id`` payload
  field).
- ``company`` — the engine company id as a string (from context).
- ``occurred_at`` — the event's ISO timestamp (from context/payload).

Selection
---------
Rules are filtered to the event's type and enabled rules, matched
against the facts, then ordered by (priority, created_at, id).
``first_match`` keeps the first; ``all_matches`` keeps all.
"""

from __future__ import annotations

import datetime
from collections.abc import Mapping, Sequence
from typing import Any

from hesabyar.domain.events.envelope import CanonicalEventType
from hesabyar.domain.rules.conditions import evaluate_conditions
from hesabyar.domain.rules.models import (
    Rule,
    RuleActionKind,
    RuleExecutionContext,
    RuleSetMode,
)

#: Payload keys that carry an event's total, in lookup order
#: (mirrors ``NormalizedEvent.total_amount_rial``).
_AMOUNT_KEYS: tuple[str, ...] = ("total_amount", "gross_amount", "refund_amount", "amount")

#: Payload keys that carry the event's party, in lookup order.
_PARTY_KEYS: tuple[str, ...] = ("party_id", "customer_id", "party")


def best_effort_total_rial(payload: Mapping[str, Any]) -> int | None:
    """Best-effort total for amount caps and conditions (whole Rial).

    Reads the converted payload's total field when present; None
    when the payload carries no total (the gate then fails closed
    on amount, per its own contract).
    """
    for key in _AMOUNT_KEYS:
        value = payload.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def build_facts(
    event_type: CanonicalEventType,
    payload: Mapping[str, Any],
    context: RuleExecutionContext | None = None,
) -> dict[str, Any]:
    """Build the DSL fact dict for one event (+ its context)."""
    facts: dict[str, Any] = {
        "event_type": event_type.value,
        "payload": dict(payload),
    }
    total = best_effort_total_rial(payload)
    if total is not None:
        facts["amount_irr"] = total
    channel = payload.get("payment_channel")
    if not isinstance(channel, str) or not channel.strip():
        channel = context.channel_key if context is not None else None
    if isinstance(channel, str) and channel.strip():
        facts["channel"] = channel.strip()
    for key in _PARTY_KEYS:
        party = payload.get(key)
        if isinstance(party, str) and party.strip():
            facts["party"] = party.strip()
            break
        if isinstance(party, int) and not isinstance(party, bool):
            facts["party"] = str(party)
            break
    if context is not None:
        if context.company_engine_id is not None:
            facts["company"] = str(context.company_engine_id)
        if context.event_date is not None:
            facts["occurred_at"] = context.event_date.isoformat()
    occurred_at = payload.get("occurred_at")
    if isinstance(occurred_at, str) and occurred_at.strip():
        facts["occurred_at"] = occurred_at.strip()
    return facts


def _rule_order(rule: Rule) -> tuple[int, datetime.datetime, str]:
    """Priority order: priority first, then creation time, then id."""
    created = rule.created_at or datetime.datetime.min.replace(tzinfo=datetime.UTC)
    return (rule.priority, created, rule.id)


def select_rules(
    rules: Sequence[Rule],
    event_type: CanonicalEventType,
    facts: Mapping[str, Any],
    mode: RuleSetMode = RuleSetMode.FIRST_MATCH,
) -> list[Rule]:
    """Select the rules that fire for one event, in priority order.

    Only enabled rules of the event's type whose conditions match
    the facts are selected. ``first_match`` (the Xero pattern)
    keeps the first in priority order; ``all_matches`` keeps every
    matching rule.
    """
    matched = [
        rule
        for rule in rules
        if rule.enabled and rule.event_type is event_type and evaluate_conditions(rule.conditions, facts)
    ]
    matched.sort(key=_rule_order)
    if mode is RuleSetMode.FIRST_MATCH:
        return matched[:1]
    return matched


def describe_action(rule: Rule, facts: Mapping[str, Any]) -> str:
    """Human-readable description of what a rule's action would do.

    Used by dry-run (and the UI) to show the action without
    performing it. Never calls a port.
    """
    action = rule.action
    params = action.params
    if action.kind is RuleActionKind.CREATE_DRAFT:
        lines = params.get("lines", [])
        return f"Create a draft journal ({len(lines)} lines) for event {facts.get('event_type')}."
    if action.kind is RuleActionKind.POST_IF_POLICY_ALLOWS:
        return (
            "Post the event's draft journal, but only if the 3-layer "
            "policy gate allows it; otherwise the draft stays a draft."
        )
    if action.kind is RuleActionKind.NOTIFY:
        recipients = ", ".join(str(r) for r in params.get("recipients", []))
        channels = ", ".join(str(c) for c in params.get("channels", []))
        return f"Notify {recipients} via {channels}."
    if action.kind is RuleActionKind.ASSIGN:
        return f"Assign the review item to {params.get('assignee')}."
    if action.kind is RuleActionKind.TAG:
        return f"Tag the review item with {params.get('tag')!r}."
    if action.kind is RuleActionKind.SET_FIELD:
        return f"Set {params.get('target')}.{params.get('field')} to {params.get('value')!r}."
    return f"Run action {action.kind.value}."  # pragma: no cover - closed set
