"""Rules-engine domain model (Phase 5-7 ۵خ W2, US-561; phased design §6.4).

A rule is ``when <event> + <conditions> → do <action>``:

- ``event_type`` is one of the twelve canonical event types
  (``CanonicalEventType``) — never an ad-hoc name.
- ``conditions`` is a small safe DSL tree (JSONLogic-style; see
  :mod:`hesabyar.domain.rules.conditions`) over the event's facts —
  amount ranges, channel, party, company. No arbitrary user code:
  there is no ``eval`` anywhere in this package, and no action may
  execute code.
- ``action`` is one of the CLOSED SAFE SET
  (:class:`RuleActionKind`): ``create_draft``,
  ``post_if_policy_allows``, ``notify``, ``assign``, ``tag``,
  ``set_field``. An unknown action kind is rejected at validation
  time (see :mod:`hesabyar.domain.rules.actions`) and again at
  execution time — the closed set is a safety property, not a
  suggestion.
- ``priority`` orders rules within a rule set (lower runs first);
  ``match_mode`` on the rule set decides whether evaluation stops
  at the first match (the Xero pattern) or runs every matching rule.

Every dry-run and every live execution is audit-logged as a
:class:`RuleExecution` (§6.7): rule id, event id, decision, outcome,
plus the gate verdicts and effect ids in ``details``.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from enum import StrEnum, unique
from typing import Any

from hesabyar.domain.events.envelope import CanonicalEventType


@unique
class RuleActionKind(StrEnum):
    """The closed safe action set (phased design §6.4).

    No other action exists. In particular there is no
    "execute code" action, no DAG step, no BPMN task — adding a
    kind here is a design decision, not a configuration change.
    """

    CREATE_DRAFT = "create_draft"
    POST_IF_POLICY_ALLOWS = "post_if_policy_allows"
    NOTIFY = "notify"
    ASSIGN = "assign"
    TAG = "tag"
    SET_FIELD = "set_field"


@unique
class RuleSetMode(StrEnum):
    """How a rule set evaluates its matching rules.

    ``first_match`` (the Xero pattern) stops at the first matching
    rule in priority order; ``all_matches`` runs every matching
    rule. The mode is configured per rule set (one tenant's rules
    for one event type) and defaults to ``first_match``.
    """

    FIRST_MATCH = "first_match"
    ALL_MATCHES = "all_matches"


@unique
class RuleExecutionMode(StrEnum):
    """Whether an execution was live or a dry-run."""

    LIVE = "live"
    DRY_RUN = "dry_run"


@unique
class RuleExecutionDecision(StrEnum):
    """What the engine decided for one rule against one event.

    ``would_execute`` is the dry-run twin of ``executed``: the rule
    matched and the action is described, never performed.
    ``blocked_by_policy_gate`` is the post action denied by the
    3-layer gate (F3) — the draft stays a draft, exactly like a
    manual post blocked by the gate.
    """

    NOT_MATCHED = "not_matched"
    WOULD_EXECUTE = "would_execute"
    EXECUTED = "executed"
    BLOCKED_BY_GATE = "blocked_by_policy_gate"
    SKIPPED = "skipped"
    FAILED = "failed"


#: Machine-readable outcome codes recorded on a :class:`RuleExecution`.
#: ``outcome`` is a short string (not an enum) so new actions can add
#: outcomes without a model change; the values below are the closed
#: vocabulary v1 produces.
OUTCOME_NO_MATCH = "no_match"
OUTCOME_WOULD_EXECUTE = "would_execute"
OUTCOME_DRAFT_CREATED = "draft_created"
OUTCOME_POSTED = "posted"
OUTCOME_DRAFT_KEPT = "draft_kept"
OUTCOME_NOTIFIED = "notified"
OUTCOME_ASSIGNED = "assigned"
OUTCOME_TAGGED = "tagged"
OUTCOME_FIELD_SET = "field_set"
OUTCOME_NO_DRAFT = "no_draft"
OUTCOME_NO_REVIEW_ITEM = "no_review_item"
OUTCOME_NO_COMPANY_CONTEXT = "no_company_context"
OUTCOME_PORT_NOT_WIRED = "port_not_wired"
OUTCOME_ERROR = "error"


@dataclass(frozen=True)
class RuleAction:
    """One action from the closed set, with its validated parameters.

    ``params`` is validated per kind by
    :func:`hesabyar.domain.rules.actions.parse_action` — a
    ``RuleAction`` only ever exists in valid form.
    """

    kind: RuleActionKind
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Wire/storage form: ``{"kind": ..., "params": {...}}``."""
        return {"kind": self.kind.value, "params": dict(self.params)}


@dataclass(frozen=True)
class Rule:
    """One user rule: ``when <event> + <conditions> → do <action>``.

    ``conditions`` is the validated DSL tree (see
    :mod:`hesabyar.domain.rules.conditions`); an empty dict means
    "no conditions" (the rule fires on every event of its type).
    ``priority`` orders rules inside a set (lower runs first);
    ties break by creation time, then id.
    """

    id: str
    tenant_id: str
    installation_id: str | None
    name: str
    description: str
    event_type: CanonicalEventType
    conditions: dict[str, Any]
    action: RuleAction
    priority: int = 100
    enabled: bool = True
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None
    created_by: str | None = None


@dataclass(frozen=True)
class RuleSetConfig:
    """One rule set's match mode: a tenant's rules for one event type.

    No row means the default (:attr:`RuleSetMode.FIRST_MATCH`).
    """

    tenant_id: str
    event_type: CanonicalEventType
    match_mode: RuleSetMode = RuleSetMode.FIRST_MATCH
    updated_at: datetime.datetime | None = None


@dataclass(frozen=True)
class RuleExecution:
    """One audit-logged rule evaluation (§6.7 automation decision log).

    Recorded for every dry-run and every live execution of a matched
    rule (a live evaluation that matches nothing records nothing —
    there is no execution to audit; a dry-run records its verdict
    even when the rule does not match, so the test is reviewable).
    ``rule_id`` is None only for a dry-run of an unsaved rule
    definition, and for executions whose rule was deleted (the
    audit row survives the rule — ``ON DELETE SET NULL``).
    ``details`` carries the evidence: gate verdicts, created draft
    ids, posted refs, notification channel results.
    """

    id: str
    tenant_id: str
    rule_id: str | None
    event_id: str | None
    event_type: str
    mode: RuleExecutionMode
    decision: RuleExecutionDecision
    outcome: str
    action_kind: str | None
    details: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    executed_by: str = "system"
    executed_at: datetime.datetime | None = None


@dataclass(frozen=True)
class RuleDraftLine:
    """One line of a ``create_draft`` journal template.

    Amounts are whole Rial (``MoneyIRR`` digits=0); exactly one side
    carries an amount, like :class:`JournalDraftLine`. The amount
    may come from the event (``{"var": "payload.total_amount"}``
    in the rule params) — resolved by the executor before the port
    is called, so the port only ever sees literal Rial amounts.
    """

    account_code: str
    debit_irr: int | None = None
    credit_irr: int | None = None
    description: str = ""


@dataclass(frozen=True)
class GateLayerVerdict:
    """One policy-gate layer's verdict, recorded for the audit log."""

    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class RuleGateVerdict:
    """The 3-layer policy gate's verdict for a rule's post action."""

    allowed: bool
    policy_version: str
    layers: tuple[GateLayerVerdict, ...]
    reason: str


@dataclass(frozen=True)
class ChannelDelivery:
    """One notification channel's delivery result."""

    channel: str
    status: str  # delivered | recorded | skipped | unsupported | failed
    detail: str


@dataclass(frozen=True)
class NotificationDelivery:
    """Per-channel results of a ``notify`` action."""

    results: tuple[ChannelDelivery, ...] = ()


@dataclass(frozen=True)
class RuleExecutionContext:
    """What the executor needs beyond the event itself.

    The caller (the REST evaluate endpoint, or the future pipeline
    hook) builds this from the event's surroundings: the
    installation, the resolved company, the event's date and
    channel, and — when the event already produced one — the draft
    or review item the rule may act on. Missing company or
    installation context fails the post action closed (the gate
    denies), never open.
    """

    tenant_id: str
    installation_id: str | None = None
    company_engine_id: int | None = None
    event_date: datetime.date | None = None
    channel_key: str | None = None
    review_item_id: str | None = None
    draft_kind: str | None = None
    draft_engine_id: int | None = None
    actor: str = "system"
