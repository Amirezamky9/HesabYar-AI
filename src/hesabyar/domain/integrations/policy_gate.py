"""The three-layer policy gate, as pure domain logic (US-503, Phase 5 F3).

Phased design §6.2: auto-post is allowed only when ALL layers pass —
and draft is always the safe fallback. The gate is fail-closed at
every layer: a missing policy row, a missing cap, a closed (or
unknown) fiscal period, or a failed tie-out all deny auto-post and
route the event to a draft / the review queue. A workflow rule can
never bypass the gate: posting always passes through it (§6.2).

Layers:

1. **Event type** — the installation must have opted in globally
   (``InstallationSettings.auto_post_enabled``) AND the event type
   must have an explicit policy row with ``allowed=True``. No row, no
   opt-in, or ``allowed=False`` → deny.
2. **Amount cap** — the event amount must be at or under the effective
   cap: the stricter of the policy row's ``max_amount_irr`` and the
   gateway contract's ``auto_post_max_amount_irr`` (when the event
   carries a channel). NO source recommends auto-post without a cap
   (§6.2), so a missing cap on BOTH sources denies — the go-live
   checklist (§1.8) requires a cap to be set.
3. **Fiscal-period lock** — the event date must fall in an OPEN fiscal
   period in the engine (checked through the accounting port, never
   by direct SQL — the Phase 4 port-bypass lesson). A closed, locked,
   or unknown period denies.

Plus the **settlement tie-out** check (§6.2, §3.4 C6): for
``payment.settlement.received`` events whose policy requires it, the
settlement total must equal the bank deposit (``tieout_ok``); a
mismatch denies auto-post even when the three layers pass.

The pure function :func:`evaluate_policy_gate` takes already-loaded
inputs and returns a :class:`PolicyGateDecision` carrying the
per-layer results plus the policy/contract versions — the audit-log
fields F1 records for every auto-post/draft decision (§6.7). The
application service
(:class:`hesabyar.application.commands.integrations.PolicyGateService`)
loads the inputs through the ports; this module never touches I/O.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from hesabyar.domain.integrations.models import (
    AutoPostPolicy,
    GatewayContract,
    PolicyGateConfig,
)

__all__ = [
    "GateLayerResult",
    "PolicyGateDecision",
    "evaluate_policy_gate",
]


@dataclass(frozen=True)
class GateLayerResult:
    """The outcome of one gate layer, for the decision audit log."""

    layer: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class PolicyGateDecision:
    """A policy-gate evaluation: allowed or denied, with the evidence.

    ``action`` is ``"auto_post"`` when every layer passed and
    ``"draft"`` otherwise — draft is the default, never an error.
    ``policy_config_id`` / ``contract_id`` / ``contract_version``
    snapshot the exact policy and contract versions the decision used,
    so the F1 decision audit log (§6.7) can record them.
    """

    allowed: bool
    action: str
    layers: tuple[GateLayerResult, ...]
    policy_config_id: str | None
    contract_id: str | None
    contract_version: int | None
    reason: str
    evaluated_at: datetime.datetime


def evaluate_policy_gate(
    *,
    event_type: str,
    amount_irr: int,
    policy: PolicyGateConfig | None,
    installation_auto_post_enabled: bool,
    contract: GatewayContract | None,
    period_open: bool,
    tieout_ok: bool | None = None,
    now: datetime.datetime | None = None,
) -> PolicyGateDecision:
    """Evaluate the three-layer gate (+ tie-out) for one event.

    Pure: no I/O, no port calls. ``period_open`` is the engine's
    verdict for the event's fiscal period (False when the period is
    closed, locked, unknown, or the engine could not be reached —
    fail-closed either way). ``tieout_ok`` applies only when the
    policy requires a tie-out; None means "not applicable".
    """
    evaluated_at = now or datetime.datetime.now(datetime.UTC)
    layers: list[GateLayerResult] = []

    # -- Layer 1: event type (+ global opt-in) ---------------------------
    if not installation_auto_post_enabled:
        layers.append(
            GateLayerResult(
                layer="event_type",
                passed=False,
                detail="Auto-post is not enabled for this installation (draft is the default).",
            )
        )
    elif policy is None:
        layers.append(
            GateLayerResult(
                layer="event_type",
                passed=False,
                detail=f"No auto-post policy is configured for event type {event_type!r}.",
            )
        )
    elif not policy.allowed:
        layers.append(
            GateLayerResult(
                layer="event_type",
                passed=False,
                detail=f"Auto-post is not allowed for event type {event_type!r} by policy.",
            )
        )
    else:
        layers.append(
            GateLayerResult(
                layer="event_type",
                passed=True,
                detail=f"Auto-post is allowed for event type {event_type!r} by policy.",
            )
        )

    # -- Layer 2: amount cap ---------------------------------------------
    caps: list[int] = []
    if policy is not None and policy.max_amount_irr is not None:
        caps.append(policy.max_amount_irr)
    if contract is not None and contract.auto_post_max_amount_irr is not None:
        caps.append(contract.auto_post_max_amount_irr)
    if contract is not None and contract.auto_post_policy == AutoPostPolicy.DRAFT_ONLY:
        layers.append(
            GateLayerResult(
                layer="amount_cap",
                passed=False,
                detail=(
                    f"The active contract (v{contract.version}) for this channel is draft-only; "
                    "it never auto-posts."
                ),
            )
        )
    elif not caps:
        layers.append(
            GateLayerResult(
                layer="amount_cap",
                passed=False,
                detail="No auto-post amount cap is configured (neither the policy nor the contract sets one).",
            )
        )
    else:
        effective_cap = min(caps)
        if amount_irr <= effective_cap:
            layers.append(
                GateLayerResult(
                    layer="amount_cap",
                    passed=True,
                    detail=f"Amount {amount_irr} IRR is within the auto-post cap of {effective_cap} IRR.",
                )
            )
        else:
            layers.append(
                GateLayerResult(
                    layer="amount_cap",
                    passed=False,
                    detail=f"Amount {amount_irr} IRR exceeds the auto-post cap of {effective_cap} IRR.",
                )
            )

    # -- Layer 3: fiscal-period lock --------------------------------------
    if period_open:
        layers.append(
            GateLayerResult(
                layer="period_lock",
                passed=True,
                detail="The event's fiscal period is open.",
            )
        )
    else:
        layers.append(
            GateLayerResult(
                layer="period_lock",
                passed=False,
                detail="The event's fiscal period is closed, locked, or unknown — never auto-post into it.",
            )
        )

    # -- Settlement tie-out (only when the policy requires it) ------------
    if policy is not None and policy.require_tieout:
        if tieout_ok is True:
            layers.append(
                GateLayerResult(
                    layer="tieout",
                    passed=True,
                    detail="Settlement tie-out holds: settlement total equals the bank deposit.",
                )
            )
        else:
            layers.append(
                GateLayerResult(
                    layer="tieout",
                    passed=False,
                    detail="Settlement tie-out failed or was not established: no auto-post before tie-out.",
                )
            )

    allowed = all(layer.passed for layer in layers)
    failed = [layer for layer in layers if not layer.passed]
    reason = (
        "All policy-gate layers passed; auto-post is allowed."
        if allowed
        else "; ".join(layer.detail for layer in failed)
    )
    return PolicyGateDecision(
        allowed=allowed,
        action="auto_post" if allowed else "draft",
        layers=tuple(layers),
        policy_config_id=policy.id if policy is not None else None,
        contract_id=contract.id if contract is not None else None,
        contract_version=contract.version if contract is not None else None,
        reason=reason,
        evaluated_at=evaluated_at,
    )
