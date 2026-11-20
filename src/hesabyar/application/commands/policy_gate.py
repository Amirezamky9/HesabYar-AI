"""The registry-backed policy gate, as a pipeline ``PolicyGatePort`` (US-511).

The F1 pipeline shipped with :class:`DraftOnlyPolicyGate` (never
auto-post) because the F3 registry gate did not exist yet. US-503
built the real gate — :class:`PolicyGateService` over the registry's
installation settings, per-event-type policy rows, gateway contracts
and the engine's fiscal-period state — and this adapter binds it into
the event pipeline, replacing the F1 default in production wiring.

The adapter is only translation: it turns the pipeline's
``(event, mapping, draft_total)`` into the service's inputs and the
service's :class:`PolicyGateDecision` back into a
:class:`PolicyDecision`. All gate logic lives in the US-503 service
and the pure domain function — nothing is re-implemented here.

Fail-closed, at the adapter's own layer too:

- an event without installation context (an inbox row that predates
  migration 0012) cannot be evaluated against any registry policy,
  so it is denied — draft, never auto-post;
- a mapping without a company cannot be period-checked, so it is
  denied;
- the payment channel travels with the event payload
  (``payment_channel``) when the event carries one; the service then
  requires that channel to exist, be enabled, and hold an active
  contract before auto-post is even considered;
- ``tieout_ok`` is always None here: store events carry no settlement
  tie-out (that arrives with the 5پ settlement engine), so a policy
  that requires a tie-out denies, exactly as the domain function
  specifies.
"""

from __future__ import annotations

from hesabyar.application.commands.integrations import PolicyGateService
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.domain.events.models import (
    GateResult,
    NormalizedEvent,
    PolicyDecision,
    ResolvedMapping,
)

__all__ = ["RegistryPolicyGate"]


class RegistryPolicyGate:
    """``PolicyGatePort`` backed by the US-503 registry gate (F3).

    Draft stays the default: the service denies auto-post unless the
    installation opted in globally, the event type has an explicit
    allowed policy row with a cap, and the event's fiscal period is
    open — and this adapter denies on top of that whenever it cannot
    supply the service's inputs.
    """

    POLICY_VERSION_FALLBACK = "registry-v1"

    def __init__(self, gate_service: PolicyGateService) -> None:
        self._gate_service = gate_service

    def evaluate(
        self,
        event: NormalizedEvent,
        mapping: ResolvedMapping,
        draft_total: MoneyIRR,
    ) -> PolicyDecision:
        """Evaluate the registry gate for one pipeline event."""
        installation_id = event.installation_id
        if installation_id is None:
            return self._denied(
                "installation_context",
                "No installation context on the event: auto-post needs "
                "the registry policy, so the draft stays a draft.",
            )
        company = mapping.company
        if company is None:
            return self._denied(
                "company_context",
                "No company resolved for the event: auto-post needs the "
                "fiscal-period check, so the draft stays a draft.",
            )
        channel_key = event.converted_payload.get("payment_channel")
        if not isinstance(channel_key, str) or not channel_key.strip():
            channel_key = None
        decision = self._gate_service.evaluate(
            installation_id,
            event_type=event.event_type.value,
            company=company,
            event_date=event.event.occurred_at.date(),
            amount_irr=draft_total.amount,
            channel_key=channel_key,
            tieout_ok=None,
        )
        return PolicyDecision(
            allowed=decision.allowed,
            policy_version=(
                f"policy-gate:{decision.policy_config_id}"
                if decision.policy_config_id is not None
                else self.POLICY_VERSION_FALLBACK
            ),
            gate_results=tuple(
                GateResult(name=layer.layer, passed=layer.passed, reason=layer.detail)
                for layer in decision.layers
            ),
            reason=decision.reason,
        )

    def _denied(self, layer: str, reason: str) -> PolicyDecision:
        """A denied verdict for inputs the adapter cannot supply."""
        return PolicyDecision(
            allowed=False,
            policy_version=self.POLICY_VERSION_FALLBACK,
            gate_results=(GateResult(name=layer, passed=False, reason=reason),),
            reason=reason,
        )
