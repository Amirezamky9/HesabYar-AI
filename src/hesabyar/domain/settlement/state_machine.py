"""The tranche state machine (US-521, phased design §3.3).

::

    expected ──► due ──► confirmed ──► received ──► settled
       │           │          │            │
       │           │          └─ partial variants ─► partially_confirmed / partially_received
       │           └─ overdue (past due + grace, unconfirmed)
       │           └─ - - ► received (reverse match C7 — bank deposit before the gateway file)
       └─ cancelled (refund/cancel before settlement)
       └─ exception (variance/unknown reference — linked to settlement_exception)
       └─ written_off (human approval only, within policy cap, audit-logged)

Terminal states: ``settled``, ``cancelled``, ``written_off`` — nothing
leaves them (a settled tranche that is later refunded stays settled;
the refund is a ``cancelled_after_settlement`` exception + a negative
adjustment on a later settlement, never a state change).

``overdue`` is entered only from ``due`` (by the daily tick, past due
+ grace) and left only by resolution (confirmed / received / partial /
cancelled / exception / written_off) — it never goes back to ``due``.

``exception`` is left only by an explicit resolution: back to an
operational state (``due`` / ``overdue`` / ``confirmed`` /
``received``), or to ``cancelled`` / ``written_off``. The resolution
itself is recorded on the linked ``settlement_exception`` row.
"""

from __future__ import annotations

from hesabyar.domain.settlement.errors import SettlementInvalidTransitionError
from hesabyar.domain.settlement.models import TrancheState

__all__ = [
    "ALLOWED_TRANSITIONS",
    "ESCALATION_THRESHOLDS_DAYS",
    "TERMINAL_STATES",
    "WRITE_OFF_ELIGIBLE_STATES",
    "escalation_level_for",
    "is_terminal",
    "require_transition",
]

#: Overdue escalation thresholds in days past due (phased design §3.7
#: exception #1: escalate at 7 / 30 / 60 days). Level 0 = not escalated.
ESCALATION_THRESHOLDS_DAYS: tuple[int, ...] = (7, 30, 60)

#: States no transition leaves.
TERMINAL_STATES: frozenset[TrancheState] = frozenset(
    {TrancheState.SETTLED, TrancheState.CANCELLED, TrancheState.WRITTEN_OFF}
)

#: States a write-off may start from: money at risk or known short.
#: Not ``expected`` (nothing is due yet — cancel instead), not
#: ``confirmed``/``received``/``settled`` (money is coming or in —
#: use an adjustment, not a write-off).
WRITE_OFF_ELIGIBLE_STATES: frozenset[TrancheState] = frozenset(
    {
        TrancheState.DUE,
        TrancheState.OVERDUE,
        TrancheState.EXCEPTION,
        TrancheState.PARTIALLY_CONFIRMED,
        TrancheState.PARTIALLY_RECEIVED,
    }
)

#: The full transition table (§3.3). Every edge the engine may take is
#: listed here; anything else raises
#: :class:`SettlementInvalidTransitionError`.
ALLOWED_TRANSITIONS: dict[TrancheState, frozenset[TrancheState]] = {
    TrancheState.EXPECTED: frozenset(
        {
            TrancheState.DUE,
            TrancheState.CANCELLED,
            TrancheState.EXCEPTION,
        }
    ),
    TrancheState.DUE: frozenset(
        {
            TrancheState.CONFIRMED,
            TrancheState.RECEIVED,  # reverse match C7
            TrancheState.PARTIALLY_CONFIRMED,
            TrancheState.PARTIALLY_RECEIVED,
            TrancheState.OVERDUE,
            TrancheState.CANCELLED,
            TrancheState.EXCEPTION,
            TrancheState.WRITTEN_OFF,
        }
    ),
    TrancheState.OVERDUE: frozenset(
        {
            TrancheState.CONFIRMED,
            TrancheState.RECEIVED,
            TrancheState.PARTIALLY_CONFIRMED,
            TrancheState.PARTIALLY_RECEIVED,
            TrancheState.CANCELLED,
            TrancheState.EXCEPTION,
            TrancheState.WRITTEN_OFF,
        }
    ),
    TrancheState.CONFIRMED: frozenset(
        {
            TrancheState.RECEIVED,
            TrancheState.PARTIALLY_RECEIVED,
            TrancheState.EXCEPTION,
        }
    ),
    TrancheState.RECEIVED: frozenset(
        {
            TrancheState.SETTLED,
            TrancheState.EXCEPTION,
        }
    ),
    TrancheState.PARTIALLY_CONFIRMED: frozenset(
        {
            TrancheState.CONFIRMED,
            TrancheState.RECEIVED,
            TrancheState.PARTIALLY_RECEIVED,
            TrancheState.CANCELLED,
            TrancheState.EXCEPTION,
            TrancheState.WRITTEN_OFF,
        }
    ),
    TrancheState.PARTIALLY_RECEIVED: frozenset(
        {
            TrancheState.RECEIVED,
            TrancheState.SETTLED,
            TrancheState.EXCEPTION,
            TrancheState.WRITTEN_OFF,
        }
    ),
    TrancheState.EXCEPTION: frozenset(
        {
            TrancheState.DUE,
            TrancheState.OVERDUE,
            TrancheState.CONFIRMED,
            TrancheState.RECEIVED,
            TrancheState.CANCELLED,
            TrancheState.WRITTEN_OFF,
        }
    ),
    TrancheState.SETTLED: frozenset(),
    TrancheState.CANCELLED: frozenset(),
    TrancheState.WRITTEN_OFF: frozenset(),
}


def is_terminal(state: TrancheState) -> bool:
    """True when no transition leaves ``state``."""
    return state in TERMINAL_STATES


def escalation_level_for(days_overdue: int) -> int:
    """Return the overdue escalation level for a days-past-due count.

    0 below 7 days, 1 at 7+, 2 at 30+, 3 at 60+ (phased design §3.7).
    """
    level = 0
    for threshold in ESCALATION_THRESHOLDS_DAYS:
        if days_overdue >= threshold:
            level += 1
    return level


def require_transition(from_state: TrancheState, to_state: TrancheState) -> None:
    """Raise unless ``from_state → to_state`` is in the §3.3 table."""
    if to_state not in ALLOWED_TRANSITIONS[from_state]:
        raise SettlementInvalidTransitionError(
            f"Tranche cannot move from {from_state.value} to {to_state.value}.",
            details={"from_state": from_state.value, "to_state": to_state.value},
        )
