"""Rules-engine use cases + action adapters (US-561, Phase 5-7 ۵خ W2).

:class:`RuleEngineService` is the engine's front door: rule CRUD,
enable/disable, rule-set match modes, dry-run, live evaluation, and
the execution audit log. :class:`RuleActionExecutor` runs one
matched rule's action through the ports; the adapters at the bottom
bind the ports to the real services (policy gate, engine drafts,
review queue, notifications).

Safety properties (each has a test in ``tests/unit/test_rules_engine.py``)
------------------------------------------------------------------------
- **Closed action set.** The executor re-validates the action kind
  against :class:`RuleActionKind` before running anything — a row
  written by hand with an unknown kind cannot execute.
- **No post without the gate.** ``post_if_policy_allows`` evaluates
  the 3-layer policy gate (F3) first; a denial, a missing gate, a
  missing company/installation/amount context, or a gate error all
  leave the draft a draft. Draft is the default, always.
- **Dry-run has no side effects.** The dry-run path never calls an
  effect port (draft creation/posting, notifications, review-queue
  writes). It MAY evaluate the policy gate — gate evaluation is a
  read-only decision — and reports the verdict in the result.
- **One rule's failure never breaks the others.** An action that
  raises is recorded as a ``failed`` execution; evaluation of the
  remaining rules continues.
- **No arbitrary code.** Conditions are a JSON tree walked by the
  DSL evaluator; templates are ``{{path}}`` substitution only.
"""

from __future__ import annotations

import dataclasses
import datetime
import logging
import re
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from hesabyar.application.commands.integrations import PolicyGateService
from hesabyar.application.commands.journals import JournalDraftService
from hesabyar.application.errors import (
    RuleExecutionNotFoundError,
    RuleInvalidError,
    RuleNotFoundError,
)
from hesabyar.application.queries.accounts import AccountQueryService
from hesabyar.domain.accounting.commands import JournalDraftLine
from hesabyar.domain.accounting.refs import CompanyRef, JournalDraftRef
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.errors import AccountNotFoundError, HesabYarError
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.domain.events.envelope import CanonicalEventType, parse_event_type
from hesabyar.domain.review_queue.models import (
    ReviewAuditEntry,
    ReviewItem,
    ReviewSeverity,
)
from hesabyar.domain.review_queue.ports import ReviewAuditPort, ReviewQueuePort
from hesabyar.domain.rules.actions import parse_action
from hesabyar.domain.rules.conditions import (
    FIELD_MISSING,
    evaluate_conditions,
    parse_conditions,
    resolve_field,
)
from hesabyar.domain.rules.engine import (
    best_effort_total_rial,
    build_facts,
    describe_action,
    select_rules,
)
from hesabyar.domain.rules.errors import RuleDefinitionError
from hesabyar.domain.rules.models import (
    OUTCOME_ASSIGNED,
    OUTCOME_DRAFT_CREATED,
    OUTCOME_DRAFT_KEPT,
    OUTCOME_ERROR,
    OUTCOME_FIELD_SET,
    OUTCOME_NO_COMPANY_CONTEXT,
    OUTCOME_NO_DRAFT,
    OUTCOME_NO_MATCH,
    OUTCOME_NO_REVIEW_ITEM,
    OUTCOME_NOTIFIED,
    OUTCOME_PORT_NOT_WIRED,
    OUTCOME_POSTED,
    OUTCOME_TAGGED,
    OUTCOME_WOULD_EXECUTE,
    ChannelDelivery,
    GateLayerVerdict,
    NotificationDelivery,
    Rule,
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

logger = logging.getLogger("hesabyar.application.commands.rules")

#: Max characters of an error kept on an execution record (the full
#: text lives in the server log).
_MAX_ERROR_CHARS = 500

#: ``{{dotted.path}}`` placeholders in notify templates.
_TEMPLATE_PATTERN = re.compile(r"\{\{\s*([A-Za-z0-9_.\-]+)\s*\}\}")


def render_template(text: str, facts: Mapping[str, Any]) -> str:
    """Render ``{{path}}`` placeholders from the facts (missing → empty).

    Plain string substitution — no expression language, no eval.
    """

    def _replace(match: re.Match[str]) -> str:
        value = resolve_field(facts, match.group(1))
        if value is FIELD_MISSING or value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    return _TEMPLATE_PATTERN.sub(_replace, text)


def _gate_verdict_details(verdict: RuleGateVerdict) -> dict[str, Any]:
    """Serialize a gate verdict for an execution's ``details``."""
    return {
        "policy_version": verdict.policy_version,
        "gate_allowed": verdict.allowed,
        "gate_reason": verdict.reason,
        "gate_layers": [
            {"name": layer.name, "passed": layer.passed, "reason": layer.detail} for layer in verdict.layers
        ],
    }


class PolicyGateServiceAdapter:
    """``RulePolicyGatePort`` backed by the US-503 registry gate (F3).

    Only translation — all gate logic lives in the US-503 service
    and the pure domain function. Fail-closed at this layer too:
    no installation context, no company, no event date, or no
    amount all deny (mirroring :class:`RegistryPolicyGate`).
    """

    POLICY_VERSION_FALLBACK = "registry-v1"

    def __init__(self, gate_service: PolicyGateService) -> None:
        self._gate_service = gate_service

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
        """Evaluate the registry gate for one rule's post action."""
        if installation_id is None:
            return self._denied(
                "installation_context",
                "No installation context: auto-post needs the registry policy.",
            )
        if company_engine_id is None:
            return self._denied(
                "company_context",
                "No company resolved for the event: auto-post needs the fiscal-period check.",
            )
        if event_date is None:
            return self._denied(
                "event_date",
                "No event date: auto-post needs the fiscal-period check.",
            )
        if amount_irr is None:
            return self._denied(
                "amount_context",
                "No amount on the event: auto-post needs the amount-cap check.",
            )
        decision = self._gate_service.evaluate(
            installation_id,
            event_type=event_type,
            company=CompanyRef(company_engine_id),
            event_date=event_date,
            amount_irr=amount_irr,
            channel_key=channel_key,
            tieout_ok=None,
        )
        return RuleGateVerdict(
            allowed=decision.allowed,
            policy_version=(
                f"policy-gate:{decision.policy_config_id}"
                if decision.policy_config_id is not None
                else self.POLICY_VERSION_FALLBACK
            ),
            layers=tuple(
                GateLayerVerdict(name=layer.layer, passed=layer.passed, detail=layer.detail)
                for layer in decision.layers
            ),
            reason=decision.reason,
        )

    def _denied(self, layer: str, reason: str) -> RuleGateVerdict:
        """A denied verdict for inputs the adapter cannot supply."""
        return RuleGateVerdict(
            allowed=False,
            policy_version=self.POLICY_VERSION_FALLBACK,
            layers=(GateLayerVerdict(name=layer, passed=False, detail=reason),),
            reason=reason,
        )


class EngineDraftAdapter:
    """Draft creation + posting over the engine's draft services.

    ``create_draft`` resolves account codes through the company's
    chart of accounts (a code with no account fails the execution,
    never a default account). ``post_draft`` verifies the draft
    belongs to the rule's company before posting — a rule must
    never post another company's draft.
    """

    def __init__(
        self,
        journals: JournalDraftService,
        accounts: AccountQueryService,
    ) -> None:
        self._journals = journals
        self._accounts = accounts

    def create_journal_draft(
        self,
        *,
        company_engine_id: int,
        date: datetime.date,
        description: str,
        lines: Sequence[RuleDraftLine],
    ) -> str:
        """Create a draft journal; return its reference string."""
        company = CompanyRef(company_engine_id)
        chart = {view.code: view.ref for view in self._accounts.get_chart_of_accounts(company)}
        draft_lines: list[JournalDraftLine] = []
        for line in lines:
            ref = chart.get(line.account_code)
            if ref is None:
                raise AccountNotFoundError(
                    f"Account code {line.account_code!r} does not exist for company {company_engine_id}.",
                    details={"account_code": line.account_code},
                )
            draft_lines.append(
                JournalDraftLine(
                    account=ref,
                    debit=MoneyIRR(line.debit_irr or 0),
                    credit=MoneyIRR(line.credit_irr or 0),
                    description=line.description,
                )
            )
        created = self._journals.create_draft(
            company,
            FiscalDate.from_gregorian(date),
            draft_lines,
            description=description,
        )
        return str(created)

    def post_journal_draft(
        self,
        *,
        company_engine_id: int,
        draft_engine_id: int,
    ) -> str:
        """Post a draft journal (company-verified); return the posted ref."""
        draft = JournalDraftRef(draft_engine_id)
        view = self._journals.get_draft(draft)
        if view.company.engine_id != company_engine_id:
            raise AccountNotFoundError(
                f"Draft journal {draft_engine_id} belongs to company "
                f"{view.company.engine_id}, not {company_engine_id}; refusing to post.",
                details={
                    "draft_id": draft_engine_id,
                    "company_engine_id": company_engine_id,
                },
            )
        return str(self._journals.post_draft(draft))


class LoggingNotificationAdapter:
    """``NotificationPort`` for v1: records, delivers what it can.

    - ``in_app`` — recorded: the execution log is the durable
      in-app record until the rules UI (US-562) surfaces a
      notifications feed.
    - ``email`` — skipped: no SMTP backend exists yet (phased
      design §2.6). The notification is recorded, never claimed
      sent.
    - ``sms`` — unsupported: the channel lights up with US-564
      through this same port.

    The per-channel results always land on the execution record,
    so a skipped channel is visible, never silent.
    """

    def send(
        self,
        *,
        recipients: Sequence[str],
        channels: Sequence[str],
        subject: str,
        message: str,
        facts: Mapping[str, Any],
    ) -> NotificationDelivery:
        """Record the notification; report per-channel delivery."""
        rendered_subject = render_template(subject, facts)
        # The message renders at delivery time (per channel); v1
        # delivers nothing out-of-band, so only the subject is logged.
        results: list[ChannelDelivery] = []
        for channel in channels:
            if channel == "in_app":
                results.append(
                    ChannelDelivery(
                        channel="in_app",
                        status="recorded",
                        detail="Recorded in the rule execution log.",
                    )
                )
            elif channel == "email":
                results.append(
                    ChannelDelivery(
                        channel="email",
                        status="skipped",
                        detail="No SMTP backend is configured; no email was sent.",
                    )
                )
            elif channel == "sms":
                results.append(
                    ChannelDelivery(
                        channel="sms",
                        status="unsupported",
                        detail="SMS delivery lands with US-564.",
                    )
                )
            else:
                results.append(
                    ChannelDelivery(
                        channel=channel,
                        status="unsupported",
                        detail=f"Unknown notification channel {channel!r}.",
                    )
                )
        logger.info(
            "Rule notification to %s via %s: %s",
            ", ".join(recipients),
            ", ".join(channels),
            rendered_subject,
        )
        return NotificationDelivery(
            results=tuple(results),
        )


class RuleActionExecutor:
    """Run one matched rule's action through the ports.

    The executor owns the safety properties: the closed action set
    (re-validated here), the policy gate for posts, and the
    per-action failure capture (a raising action becomes a
    ``failed`` execution, never an exception to the caller).
    """

    def __init__(
        self,
        *,
        gate: RulePolicyGatePort | None = None,
        drafts: DraftCreationPort | None = None,
        posting: DraftPostingPort | None = None,
        notifications: NotificationPort | None = None,
        review_queue: ReviewQueuePort | None = None,
        review_audit: ReviewAuditPort | None = None,
        now: datetime.datetime | None = None,
    ) -> None:
        self._gate = gate
        self._drafts = drafts
        self._posting = posting
        self._notifications = notifications
        self._review_queue = review_queue
        self._review_audit = review_audit
        self._now = now

    def _clock(self) -> datetime.datetime:
        if self._now is not None:
            return self._now
        return datetime.datetime.now(datetime.UTC)

    # -- live execution -----------------------------------------------------

    def execute_live(
        self,
        rule: Rule,
        *,
        event_type: CanonicalEventType,
        payload: Mapping[str, Any],
        event_id: str | None,
        context: RuleExecutionContext,
    ) -> RuleExecution:
        """Execute one matched rule's action; never raises.

        The caller guarantees the rule matched (event type +
        conditions). The returned execution is NOT persisted — the
        service saves it.
        """
        facts = build_facts(event_type, payload, context)
        try:
            kind = rule.action.kind  # re-validated: the closed set, defense in depth
            if kind is RuleActionKind.CREATE_DRAFT:
                decision, outcome, details = self._create_draft(rule, facts, context)
            elif kind is RuleActionKind.POST_IF_POLICY_ALLOWS:
                decision, outcome, details = self._post_if_allowed(rule, event_type, facts, context)
            elif kind is RuleActionKind.NOTIFY:
                decision, outcome, details = self._notify(rule, facts)
            elif kind is RuleActionKind.ASSIGN:
                decision, outcome, details = self._assign(rule, context)
            elif kind is RuleActionKind.TAG:
                decision, outcome, details = self._tag(rule, context)
            elif kind is RuleActionKind.SET_FIELD:
                decision, outcome, details = self._set_field(rule, context)
            else:  # pragma: no cover - the closed set has no other kind
                return self._failed(
                    rule,
                    event_type,
                    event_id,
                    context,
                    f"Unknown action kind {kind!r}: refusing to execute.",
                )
            return self._execution(
                rule,
                event_type,
                event_id,
                context,
                mode=RuleExecutionMode.LIVE,
                decision=decision,
                outcome=outcome,
                details=details,
            )
        except HesabYarError as exc:
            return self._failed(rule, event_type, event_id, context, exc.message, code=exc.code)
        except Exception as exc:  # noqa: BLE001 - one rule must never break the rest
            return self._failed(
                rule,
                event_type,
                event_id,
                context,
                f"{exc.__class__.__name__}: {exc}",
            )

    # -- dry-run -------------------------------------------------------------

    def describe_dry_run(
        self,
        rule: Rule,
        *,
        event_type: CanonicalEventType,
        payload: Mapping[str, Any],
        event_id: str | None,
        context: RuleExecutionContext,
    ) -> RuleExecution:
        """Describe what a rule WOULD do — with zero side effects.

        No effect port is ever called here (no drafts, no posts, no
        notifications, no review-queue writes). The policy gate MAY
        be evaluated for a post action — gate evaluation is a
        read-only decision — and its verdict is reported in the
        details, so the dry-run shows whether the post would be
        allowed or blocked.
        """
        facts = build_facts(event_type, payload, context)
        matched = rule.event_type is event_type and evaluate_conditions(rule.conditions, facts)
        if not matched:
            return self._execution(
                rule,
                event_type,
                event_id,
                context,
                mode=RuleExecutionMode.DRY_RUN,
                decision=RuleExecutionDecision.NOT_MATCHED,
                outcome=OUTCOME_NO_MATCH,
                details={"would_do": "Nothing: the rule does not match this event."},
            )
        details: dict[str, Any] = {"would_do": describe_action(rule, facts)}
        if rule.action.kind is RuleActionKind.POST_IF_POLICY_ALLOWS:
            details.update(self._dry_run_gate(event_type, facts, context))
        return self._execution(
            rule,
            event_type,
            event_id,
            context,
            mode=RuleExecutionMode.DRY_RUN,
            decision=RuleExecutionDecision.WOULD_EXECUTE,
            outcome=OUTCOME_WOULD_EXECUTE,
            details=details,
        )

    def _dry_run_gate(
        self,
        event_type: CanonicalEventType,
        facts: Mapping[str, Any],
        context: RuleExecutionContext,
    ) -> dict[str, Any]:
        """Evaluate the gate read-only for a dry-run post action."""
        if self._gate is None:
            return {
                "gate_evaluated": False,
                "gate_reason": "No policy gate is wired: the post would fail closed.",
            }
        try:
            verdict = self._gate.evaluate(
                installation_id=context.installation_id,
                event_type=event_type.value,
                company_engine_id=context.company_engine_id,
                event_date=context.event_date,
                amount_irr=best_effort_total_rial(facts.get("payload", {})),
                channel_key=context.channel_key,
            )
        except Exception as exc:  # noqa: BLE001 - a gate error fails closed
            return {
                "gate_evaluated": False,
                "gate_reason": f"Gate evaluation failed ({exc}); the post would fail closed.",
            }
        details: dict[str, Any] = {"gate_evaluated": True}
        details.update(_gate_verdict_details(verdict))
        details["would_post"] = verdict.allowed
        return details

    # -- actions -------------------------------------------------------------

    def _create_draft(
        self,
        rule: Rule,
        facts: Mapping[str, Any],
        context: RuleExecutionContext,
    ) -> tuple[RuleExecutionDecision, str, dict[str, Any]]:
        if context.company_engine_id is None:
            return (
                RuleExecutionDecision.SKIPPED,
                OUTCOME_NO_COMPANY_CONTEXT,
                {"reason": "No company in the execution context; no draft was created."},
            )
        if self._drafts is None:
            return (
                RuleExecutionDecision.SKIPPED,
                OUTCOME_PORT_NOT_WIRED,
                {"reason": "No draft-creation port is wired; no draft was created."},
            )
        params = rule.action.params
        lines: list[RuleDraftLine] = []
        for raw_line in params.get("lines", []):
            debit = self._resolve_line_amount(raw_line.get("debit"), facts)
            credit = self._resolve_line_amount(raw_line.get("credit"), facts)
            lines.append(
                RuleDraftLine(
                    account_code=str(raw_line["account_code"]),
                    debit_irr=debit,
                    credit_irr=credit,
                    description=str(raw_line.get("description", "")),
                )
            )
        description = render_template(str(params.get("description", "")), facts)
        draft_date = context.event_date or self._clock().date()
        draft_ref = self._drafts.create_journal_draft(
            company_engine_id=context.company_engine_id,
            date=draft_date,
            description=description,
            lines=lines,
        )
        return (
            RuleExecutionDecision.EXECUTED,
            OUTCOME_DRAFT_CREATED,
            {"draft_ref": draft_ref, "company_engine_id": context.company_engine_id},
        )

    def _resolve_line_amount(self, value: Any, facts: Mapping[str, Any]) -> int | None:
        """Resolve a draft-line amount: literal int or a ``var`` lookup."""
        if value is None:
            return None
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, dict) and list(value.keys()) == ["var"]:
            resolved = resolve_field(facts, str(value["var"]))
            if isinstance(resolved, int) and not isinstance(resolved, bool) and resolved > 0:
                return resolved
            raise RuleDefinitionError(
                f'Draft-line amount {{"var": {value["var"]!r}}} did not resolve to a positive integer (Rial).',
                details={"var": value["var"]},
            )
        raise RuleDefinitionError(
            f'A draft-line amount must be a positive integer (Rial) or {{"var": "path"}}; got {value!r}.',
            details={"value": value},
        )

    def _post_if_allowed(
        self,
        rule: Rule,
        event_type: CanonicalEventType,
        facts: Mapping[str, Any],
        context: RuleExecutionContext,
    ) -> tuple[RuleExecutionDecision, str, dict[str, Any]]:
        params = rule.action.params
        draft_engine_id = self._resolve_draft_id(params, facts, context)
        if draft_engine_id is None:
            return (
                RuleExecutionDecision.SKIPPED,
                OUTCOME_NO_DRAFT,
                {"reason": "No draft resolves for this event; nothing to post."},
            )
        if context.company_engine_id is None:
            return (
                RuleExecutionDecision.BLOCKED_BY_GATE,
                OUTCOME_DRAFT_KEPT,
                {
                    "draft_ref": f"journal-draft:{draft_engine_id}",
                    "reason": "No company in the execution context: the gate denies.",
                },
            )
        if self._gate is None:
            return (
                RuleExecutionDecision.BLOCKED_BY_GATE,
                OUTCOME_DRAFT_KEPT,
                {
                    "draft_ref": f"journal-draft:{draft_engine_id}",
                    "reason": "No policy gate is wired: the post fails closed.",
                },
            )
        try:
            verdict = self._gate.evaluate(
                installation_id=context.installation_id,
                event_type=event_type.value,
                company_engine_id=context.company_engine_id,
                event_date=context.event_date,
                amount_irr=best_effort_total_rial(facts.get("payload", {})),
                channel_key=context.channel_key,
            )
        except Exception as exc:  # noqa: BLE001 - a gate error fails closed
            return (
                RuleExecutionDecision.FAILED,
                OUTCOME_ERROR,
                {
                    "draft_ref": f"journal-draft:{draft_engine_id}",
                    "reason": f"Gate evaluation failed ({exc}); the draft was not posted.",
                },
            )
        details: dict[str, Any] = {
            "draft_ref": f"journal-draft:{draft_engine_id}",
        }
        details.update(_gate_verdict_details(verdict))
        if not verdict.allowed:
            return (
                RuleExecutionDecision.BLOCKED_BY_GATE,
                OUTCOME_DRAFT_KEPT,
                details,
            )
        if self._posting is None:
            return (
                RuleExecutionDecision.SKIPPED,
                OUTCOME_PORT_NOT_WIRED,
                {
                    **details,
                    "reason": "The gate allowed the post, but no posting port is wired.",
                },
            )
        posted_ref = self._posting.post_journal_draft(
            company_engine_id=context.company_engine_id,
            draft_engine_id=draft_engine_id,
        )
        details["posted_ref"] = posted_ref
        return (RuleExecutionDecision.EXECUTED, OUTCOME_POSTED, details)

    def _resolve_draft_id(
        self,
        params: Mapping[str, Any],
        facts: Mapping[str, Any],
        context: RuleExecutionContext,
    ) -> int | None:
        """Resolve the draft to post: params, payload field, or context."""
        draft_id = params.get("draft_id")
        if draft_id is not None:
            return int(draft_id)
        draft_id_field = params.get("draft_id_field")
        if draft_id_field is not None:
            resolved = resolve_field(facts, str(draft_id_field))
            if isinstance(resolved, int) and not isinstance(resolved, bool) and resolved > 0:
                return resolved
            return None
        if context.draft_engine_id is not None and context.draft_kind in (None, "journal"):
            return context.draft_engine_id
        return None

    def _notify(
        self,
        rule: Rule,
        facts: Mapping[str, Any],
    ) -> tuple[RuleExecutionDecision, str, dict[str, Any]]:
        if self._notifications is None:
            return (
                RuleExecutionDecision.SKIPPED,
                OUTCOME_PORT_NOT_WIRED,
                {"reason": "No notification port is wired; nothing was sent."},
            )
        params = rule.action.params
        delivery = self._notifications.send(
            recipients=[str(r) for r in params.get("recipients", [])],
            channels=[str(c) for c in params.get("channels", [])],
            subject=str(params.get("subject", "")),
            message=str(params.get("message", "")),
            facts=facts,
        )
        return (
            RuleExecutionDecision.EXECUTED,
            OUTCOME_NOTIFIED,
            {
                "channels": [
                    {
                        "channel": result.channel,
                        "status": result.status,
                        "detail": result.detail,
                    }
                    for result in delivery.results
                ],
            },
        )

    def _resolve_review_item(
        self,
        rule: Rule,
        context: RuleExecutionContext,
    ) -> tuple[ReviewItem | None, str | None]:
        """Resolve the review item an action targets (params, then context)."""
        item_id = rule.action.params.get("review_item_id") or context.review_item_id
        if item_id is None:
            return None, "No review item in the action or the execution context."
        if self._review_queue is None:
            return None, "No review-queue port is wired."
        item = self._review_queue.get_item(str(item_id), context.tenant_id)
        if item is None:
            return None, f"Review item {item_id!r} does not exist in this tenant."
        if item.is_terminal:
            return None, f"Review item {item_id!r} is {item.status.value}; it cannot change."
        return item, None

    def _record_review_audit(
        self,
        rule: Rule,
        item: ReviewItem,
        *,
        action: str,
        decision: str,
        note: str,
    ) -> None:
        """Append the US-552 audit entry for a rule's review-queue action."""
        if self._review_audit is None:
            return
        self._review_audit.record(
            ReviewAuditEntry(
                id=str(uuid.uuid4()),
                item_id=item.id,
                actor=f"rule:{rule.id}",
                action=action,
                decision=decision,
                note=note,
            )
        )

    def _assign(
        self,
        rule: Rule,
        context: RuleExecutionContext,
    ) -> tuple[RuleExecutionDecision, str, dict[str, Any]]:
        item, problem = self._resolve_review_item(rule, context)
        if item is None or self._review_queue is None:
            return (
                RuleExecutionDecision.SKIPPED,
                OUTCOME_NO_REVIEW_ITEM if self._review_queue is not None else OUTCOME_PORT_NOT_WIRED,
                {"reason": problem or "No review-queue port is wired."},
            )
        assignee = str(rule.action.params["assignee"])
        updated = dataclasses.replace(item, assignee=assignee, updated_at=self._clock())
        self._review_queue.update_item(updated)
        self._record_review_audit(
            rule,
            item,
            action="assign",
            decision="assigned",
            note=f"Assigned to {assignee} by rule {rule.name!r}.",
        )
        return (
            RuleExecutionDecision.EXECUTED,
            OUTCOME_ASSIGNED,
            {"review_item_id": item.id, "assignee": assignee},
        )

    def _tag(
        self,
        rule: Rule,
        context: RuleExecutionContext,
    ) -> tuple[RuleExecutionDecision, str, dict[str, Any]]:
        item, problem = self._resolve_review_item(rule, context)
        if item is None or self._review_queue is None:
            return (
                RuleExecutionDecision.SKIPPED,
                OUTCOME_NO_REVIEW_ITEM if self._review_queue is not None else OUTCOME_PORT_NOT_WIRED,
                {"reason": problem or "No review-queue port is wired."},
            )
        tag = str(rule.action.params["tag"])
        tags = tuple(item.tags)
        if tag not in tags:
            tags = (*tags, tag)
        updated = dataclasses.replace(item, tags=tags, updated_at=self._clock())
        self._review_queue.update_item(updated)
        self._record_review_audit(
            rule,
            item,
            action="tag",
            decision="tagged",
            note=f"Tagged {tag!r} by rule {rule.name!r}.",
        )
        return (
            RuleExecutionDecision.EXECUTED,
            OUTCOME_TAGGED,
            {"review_item_id": item.id, "tag": tag, "tags": list(tags)},
        )

    def _set_field(
        self,
        rule: Rule,
        context: RuleExecutionContext,
    ) -> tuple[RuleExecutionDecision, str, dict[str, Any]]:
        item, problem = self._resolve_review_item(rule, context)
        if item is None or self._review_queue is None:
            return (
                RuleExecutionDecision.SKIPPED,
                OUTCOME_NO_REVIEW_ITEM if self._review_queue is not None else OUTCOME_PORT_NOT_WIRED,
                {"reason": problem or "No review-queue port is wired."},
            )
        field_name = str(rule.action.params["field"])
        value = str(rule.action.params["value"])
        if field_name != "severity":  # pragma: no cover - the whitelist has one field in v1
            return (
                RuleExecutionDecision.FAILED,
                OUTCOME_ERROR,
                {"reason": f"set_field on review_item.{field_name} is not supported in v1."},
            )
        updated = dataclasses.replace(item, severity=ReviewSeverity(value), updated_at=self._clock())
        self._review_queue.update_item(updated)
        self._record_review_audit(
            rule,
            item,
            action="set_field",
            decision="field_set",
            note=f"Severity set to {value} by rule {rule.name!r}.",
        )
        return (
            RuleExecutionDecision.EXECUTED,
            OUTCOME_FIELD_SET,
            {"review_item_id": item.id, "field": "severity", "value": value},
        )

    # -- execution records ----------------------------------------------------

    def _execution(
        self,
        rule: Rule,
        event_type: CanonicalEventType,
        event_id: str | None,
        context: RuleExecutionContext,
        *,
        mode: RuleExecutionMode,
        decision: RuleExecutionDecision,
        outcome: str,
        details: dict[str, Any],
        error: str | None = None,
    ) -> RuleExecution:
        return RuleExecution(
            id=str(uuid.uuid4()),
            tenant_id=context.tenant_id,
            rule_id=rule.id,
            event_id=event_id,
            event_type=event_type.value,
            mode=mode,
            decision=decision,
            outcome=outcome,
            action_kind=rule.action.kind.value,
            details=details,
            error=error,
            executed_by=context.actor,
            executed_at=self._clock(),
        )

    def _failed(
        self,
        rule: Rule,
        event_type: CanonicalEventType,
        event_id: str | None,
        context: RuleExecutionContext,
        message: str,
        *,
        code: str | None = None,
    ) -> RuleExecution:
        details: dict[str, Any] = {}
        if code is not None:
            details["error_code"] = code
        return self._execution(
            rule,
            event_type,
            event_id,
            context,
            mode=RuleExecutionMode.LIVE,
            decision=RuleExecutionDecision.FAILED,
            outcome=OUTCOME_ERROR,
            details=details,
            error=message[:_MAX_ERROR_CHARS],
        )


@dataclasses.dataclass(frozen=True)
class DryRunResult:
    """The result of testing one rule against one event (no side effects)."""

    rule_id: str | None
    rule_name: str
    event_type: str
    matched: bool
    decision: RuleExecutionDecision
    outcome: str
    would_do: str
    details: dict[str, Any]
    execution_id: str


class RuleEngineService:
    """Rule CRUD, dry-run, live evaluation, and the audit log."""

    def __init__(
        self,
        repository: RuleRepositoryPort,
        *,
        executor: RuleActionExecutor | None = None,
        now: datetime.datetime | None = None,
    ) -> None:
        self._repository = repository
        self._executor = executor or RuleActionExecutor()
        self._now = now

    def _clock(self) -> datetime.datetime:
        if self._now is not None:
            return self._now
        return datetime.datetime.now(datetime.UTC)

    # -- validation ------------------------------------------------------------

    def _parse_event_type(self, raw: Any) -> CanonicalEventType:
        """Parse + validate an event type (422 on anything unknown)."""
        try:
            if not isinstance(raw, str):
                raise RuleDefinitionError(
                    f"An event type must be a string; got {type(raw).__name__}.",
                    details={"event_type": raw},
                )
            return parse_event_type(raw)
        except HesabYarError as exc:
            raise RuleInvalidError(exc.message, details=dict(exc.details)) from exc

    def _parse_definition(
        self,
        *,
        name: Any,
        event_type_raw: Any,
        conditions_raw: Any,
        action_raw: Any,
        priority: Any,
    ) -> tuple[str, CanonicalEventType, dict[str, Any], Any, int]:
        """Validate a rule definition's fields (422 on any problem)."""
        try:
            if not isinstance(name, str) or not name.strip():
                raise RuleDefinitionError(
                    "A rule needs a non-empty string name.",
                    details={"name": name},
                )
            if len(name.strip()) > 200:
                raise RuleDefinitionError(
                    "A rule name must be at most 200 characters.",
                    details={"name": name},
                )
            if isinstance(priority, bool) or not isinstance(priority, int) or priority < 0:
                raise RuleDefinitionError(
                    f"A rule priority must be a non-negative integer; got {priority!r}.",
                    details={"priority": priority},
                )
            event_type = self._parse_event_type(event_type_raw)
            conditions = parse_conditions(conditions_raw)
            action = parse_action(action_raw)
            return name.strip(), event_type, conditions, action, priority
        except RuleInvalidError:
            raise
        except HesabYarError as exc:
            raise RuleInvalidError(exc.message, details=dict(exc.details)) from exc

    # -- CRUD -------------------------------------------------------------------

    def create_rule(
        self,
        tenant_id: str,
        installation_id: str | None,
        *,
        name: str,
        description: str = "",
        event_type: str,
        conditions: Any = None,
        action: Any,
        priority: int = 100,
        enabled: bool = True,
        actor: str,
    ) -> Rule:
        """Create a rule (validated before it is stored)."""
        if not isinstance(description, str) or len(description) > 2000:
            raise RuleInvalidError(
                "A rule description must be a string of at most 2000 characters.",
                details={"description": description},
            )
        clean_name, clean_event_type, clean_conditions, clean_action, clean_priority = self._parse_definition(
            name=name,
            event_type_raw=event_type,
            conditions_raw=conditions,
            action_raw=action,
            priority=priority,
        )
        now = self._clock()
        rule = Rule(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            installation_id=installation_id,
            name=clean_name,
            description=description,
            event_type=clean_event_type,
            conditions=clean_conditions,
            action=clean_action,
            priority=clean_priority,
            enabled=bool(enabled),
            created_at=now,
            updated_at=now,
            created_by=actor,
        )
        return self._repository.save_rule(rule)

    def update_rule(
        self,
        tenant_id: str,
        rule_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        event_type: str | None = None,
        conditions: Any = None,
        action: Any = None,
        priority: int | None = None,
        enabled: bool | None = None,
    ) -> Rule:
        """Update a rule's fields (only the given ones change)."""
        existing = self._require_rule(tenant_id, rule_id)
        new_name = existing.name if name is None else name
        new_event_type = existing.event_type.value if event_type is None else event_type
        new_conditions = existing.conditions if conditions is None else conditions
        new_action = existing.action.to_dict() if action is None else action
        new_priority = existing.priority if priority is None else priority
        if description is not None and (not isinstance(description, str) or len(description) > 2000):
            raise RuleInvalidError(
                "A rule description must be a string of at most 2000 characters.",
                details={"description": description},
            )
        clean_name, clean_event_type, clean_conditions, clean_action, clean_priority = self._parse_definition(
            name=new_name,
            event_type_raw=new_event_type,
            conditions_raw=new_conditions,
            action_raw=new_action,
            priority=new_priority,
        )
        updated = dataclasses.replace(
            existing,
            name=clean_name,
            description=existing.description if description is None else description,
            event_type=clean_event_type,
            conditions=clean_conditions,
            action=clean_action,
            priority=clean_priority,
            enabled=existing.enabled if enabled is None else bool(enabled),
            updated_at=self._clock(),
        )
        return self._repository.save_rule(updated)

    def get_rule(self, tenant_id: str, rule_id: str) -> Rule:
        """One rule, or 404 when it is not in this tenant."""
        return self._require_rule(tenant_id, rule_id)

    def list_rules(
        self,
        tenant_id: str,
        installation_id: str | None = None,
        *,
        event_type: str | None = None,
        enabled: bool | None = None,
    ) -> Sequence[Rule]:
        """A tenant's rules in priority order (see the port's semantics)."""
        parsed_type = self._parse_event_type(event_type) if event_type is not None else None
        return self._repository.list_rules(
            tenant_id,
            installation_id=installation_id,
            event_type=parsed_type,
            enabled=enabled,
        )

    def delete_rule(self, tenant_id: str, rule_id: str) -> None:
        """Delete a rule (its executions survive — the audit log is append-only)."""
        if not self._repository.delete_rule(tenant_id, rule_id):
            raise RuleNotFoundError(
                f"Rule {rule_id!r} does not exist in this tenant.",
                details={"rule_id": rule_id},
            )

    def set_enabled(self, tenant_id: str, rule_id: str, *, enabled: bool) -> Rule:
        """Enable or disable a rule (a disabled rule never fires live)."""
        existing = self._require_rule(tenant_id, rule_id)
        return self._repository.save_rule(
            dataclasses.replace(existing, enabled=bool(enabled), updated_at=self._clock())
        )

    def _require_rule(self, tenant_id: str, rule_id: str) -> Rule:
        rule = self._repository.get_rule(tenant_id, rule_id)
        if rule is None:
            raise RuleNotFoundError(
                f"Rule {rule_id!r} does not exist in this tenant.",
                details={"rule_id": rule_id},
            )
        return rule

    # -- rule-set match modes ----------------------------------------------------

    def get_match_mode(self, tenant_id: str, event_type: CanonicalEventType) -> RuleSetMode:
        """A rule set's configured mode (``first_match`` when unconfigured)."""
        config = self._repository.get_set_config(tenant_id, event_type)
        if config is None:
            return RuleSetMode.FIRST_MATCH
        return config.match_mode

    def set_match_mode(self, tenant_id: str, *, event_type: str, match_mode: str) -> RuleSetConfig:
        """Configure a rule set's match mode (explicit, per §6.4)."""
        parsed_type = self._parse_event_type(event_type)
        try:
            mode = RuleSetMode(match_mode)
        except ValueError as exc:
            raise RuleInvalidError(
                f"Unknown match mode {match_mode!r}; expected one of {sorted(m.value for m in RuleSetMode)!r}.",
                details={"match_mode": match_mode},
            ) from exc
        return self._repository.save_set_config(
            RuleSetConfig(
                tenant_id=tenant_id,
                event_type=parsed_type,
                match_mode=mode,
                updated_at=self._clock(),
            )
        )

    def list_set_configs(self, tenant_id: str) -> Sequence[RuleSetConfig]:
        """A tenant's stored rule-set configs (unconfigured sets use the default)."""
        return self._repository.list_set_configs(tenant_id)

    # -- dry-run -------------------------------------------------------------------

    def dry_run_rule(
        self,
        tenant_id: str,
        rule_id: str,
        *,
        event_type: str,
        payload: Mapping[str, Any],
        event_id: str | None = None,
        context: RuleExecutionContext,
    ) -> DryRunResult:
        """Test a stored rule against one event (or sample payload).

        A disabled rule can be dry-run — that is the point: test
        before activation (§6.4). The dry-run is audit-logged like
        a live execution, with ``mode=dry_run``.
        """
        rule = self._require_rule(tenant_id, rule_id)
        parsed_type = self._parse_event_type(event_type)
        execution = self._executor.describe_dry_run(
            rule,
            event_type=parsed_type,
            payload=payload,
            event_id=event_id,
            context=context,
        )
        saved = self._repository.save_execution(execution)
        return DryRunResult(
            rule_id=rule.id,
            rule_name=rule.name,
            event_type=parsed_type.value,
            matched=execution.decision is RuleExecutionDecision.WOULD_EXECUTE,
            decision=execution.decision,
            outcome=execution.outcome,
            would_do=str(execution.details.get("would_do", "")),
            details=dict(execution.details),
            execution_id=saved.id,
        )

    def dry_run_definition(
        self,
        tenant_id: str,
        *,
        name: str,
        event_type: str,
        conditions: Any = None,
        action: Any,
        payload: Mapping[str, Any],
        event_id: str | None = None,
        context: RuleExecutionContext,
    ) -> DryRunResult:
        """Test an unsaved rule definition (the UI's test-before-save).

        The definition is validated exactly like a stored rule; the
        dry-run is audit-logged with ``rule_id=None``.
        """
        clean_name, clean_event_type, clean_conditions, clean_action, _ = self._parse_definition(
            name=name,
            event_type_raw=event_type,
            conditions_raw=conditions,
            action_raw=action,
            priority=100,
        )
        draft = Rule(
            id="draft",
            tenant_id=tenant_id,
            installation_id=context.installation_id,
            name=clean_name,
            description="",
            event_type=clean_event_type,
            conditions=clean_conditions,
            action=clean_action,
        )
        parsed_type = self._parse_event_type(event_type)
        execution = self._executor.describe_dry_run(
            draft,
            event_type=parsed_type,
            payload=payload,
            event_id=event_id,
            context=context,
        )
        # The draft has no stored row: the audit record carries no rule id.
        execution = dataclasses.replace(execution, rule_id=None)
        saved = self._repository.save_execution(execution)
        return DryRunResult(
            rule_id=None,
            rule_name=draft.name,
            event_type=parsed_type.value,
            matched=execution.decision is RuleExecutionDecision.WOULD_EXECUTE,
            decision=execution.decision,
            outcome=execution.outcome,
            would_do=str(execution.details.get("would_do", "")),
            details=dict(execution.details),
            execution_id=saved.id,
        )

    # -- live evaluation -----------------------------------------------------------

    def evaluate_event(
        self,
        tenant_id: str,
        *,
        event_type: str,
        payload: Mapping[str, Any],
        event_id: str | None,
        context: RuleExecutionContext,
        match_mode: str | None = None,
    ) -> list[RuleExecution]:
        """Evaluate the tenant's enabled rules against one live event.

        The integration seam for live execution: the REST evaluate
        endpoint calls this against a past inbox event (or a sample
        payload), and the future pipeline hook will call it after
        the pipeline finishes an event (see ``docs/rules-engine.md``).
        Every matched rule's execution is audit-logged; rules that
        do not match record nothing.
        """
        parsed_type = self._parse_event_type(event_type)
        if match_mode is not None:
            try:
                mode = RuleSetMode(match_mode)
            except ValueError as exc:
                raise RuleInvalidError(
                    f"Unknown match mode {match_mode!r}; expected one of {sorted(m.value for m in RuleSetMode)!r}.",
                    details={"match_mode": match_mode},
                ) from exc
        else:
            mode = self.get_match_mode(tenant_id, parsed_type)
        rules = self._repository.list_rules(
            tenant_id,
            installation_id=context.installation_id,
            event_type=parsed_type,
            enabled=True,
        )
        facts = build_facts(parsed_type, payload, context)
        selected = select_rules(rules, parsed_type, facts, mode)
        executions: list[RuleExecution] = []
        for rule in selected:
            execution = self._executor.execute_live(
                rule,
                event_type=parsed_type,
                payload=payload,
                event_id=event_id,
                context=context,
            )
            executions.append(self._repository.save_execution(execution))
        return executions

    # -- execution history -----------------------------------------------------------

    def list_executions(
        self,
        tenant_id: str,
        *,
        rule_id: str | None = None,
        event_id: str | None = None,
        limit: int = 50,
    ) -> Sequence[RuleExecution]:
        """A tenant's executions, newest first (the §6.7 audit log)."""
        if limit < 1 or limit > 200:
            raise RuleInvalidError(
                f"Execution list limit must be 1..200; got {limit}.",
                details={"limit": limit},
            )
        return self._repository.list_executions(tenant_id, rule_id=rule_id, event_id=event_id, limit=limit)

    def get_execution(self, tenant_id: str, execution_id: str) -> RuleExecution:
        """One execution, or 404 when it is not in this tenant."""
        execution = self._repository.get_execution(tenant_id, execution_id)
        if execution is None:
            raise RuleExecutionNotFoundError(
                f"Rule execution {execution_id!r} does not exist in this tenant.",
                details={"execution_id": execution_id},
            )
        return execution
