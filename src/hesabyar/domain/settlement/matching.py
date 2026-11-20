"""Pure three-way matching + tie-out for settlement (US-523, Phase 5-3 Wave B).

Pure functions over deposits + events + tranches — no I/O, no
database, no engine calls. The application service loads the inputs
through the ports and applies the decisions.

Matching order (phased design §3.4 C2–C7):

- **C2 — tranche↔event:** an exact ``transaction_id`` match when the
  file has detail; otherwise a batch match — due tranches of the same
  gateway + period whose net sums to the event net within tolerance,
  allocated FIFO by due date.
- **C5 — deposit↔tranches/events** (in confidence order):
  1. **Exact reference** — the event's tracking ref appears in the
     bank narrative (or the deposit carries it) → auto-match, high
     confidence. Opportunistic: whether Iranian bank narratives
     carry the gateway tracking number is UNVERIFIED — never the
     sole support.
  2. **Amount + date** — the deposit amount equals an event net or a
     tranche's outstanding net and the date is within
     ``match_window_days`` (±2 default, from installation settings)
     → auto-match when unique.
  3. **Amount only** — several candidates share the amount → the
     review queue with ranked suggestions; nothing auto-matches.
  4. **No candidate** → ``deposit_without_file`` exception.
- **C7 — reverse matching:** a deposit arriving before the gateway
  file attaches to expected tranches (``received`` without
  ``confirmed``); the later file confirms it. In this module that is
  a rule-2/3 match against a tranche with no event yet.
- **C6 — mandatory tie-out:** ``Σ confirmed tranche nets − Σ
  documented period deductions = Σ matched bank deposits`` within
  ``tieout_tolerance_irr``. Undocumented deductions are NOT
  deductions — they surface as the tie-out difference and become an
  ``amount_mismatch`` exception.
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from hesabyar.domain.bank.parsing import normalize_digits

__all__ = [
    "DepositMatchDecision",
    "DepositToMatch",
    "EventCandidate",
    "MatchAllocation",
    "MatchSuggestion",
    "TieOutResult",
    "TrancheCandidate",
    "batch_match_allocations",
    "compute_tieout",
    "match_deposit",
]

#: Minimum reference token length for a narrative-token rule-1 hit.
#: Short numbers (years, small amounts) appear in descriptions by
#: accident; a real tracking number is longer.
MIN_REFERENCE_TOKEN_LENGTH = 6

#: Rule-1 (exact reference) confidence: the highest the matcher gives.
CONFIDENCE_EXACT_REFERENCE = Decimal("1")

#: Rule-2 (amount + date, unique) confidence.
CONFIDENCE_AMOUNT_AND_DATE = Decimal("0.95")

#: Rule-3 suggestion confidence ceiling (a suggestion never auto-posts).
CONFIDENCE_SUGGESTION_MAX = Decimal("0.70")

#: Rule-3 suggestion floor: a candidate scoring below this is noise.
CONFIDENCE_SUGGESTION_MIN = Decimal("0.50")

#: How many ranked suggestions a rule-3 decision carries at most.
MAX_SUGGESTIONS = 3


@dataclass(frozen=True)
class TrancheCandidate:
    """One tranche a deposit or an event may match."""

    id: str
    gateway_payment_id: str
    net_irr: int
    outstanding_net_irr: int
    expected_date: datetime.date
    state: str
    sales_period_jalali: str
    tranche_index: int = 0


@dataclass(frozen=True)
class EventCandidate:
    """One settlement event a deposit may match."""

    id: str
    net_irr: int
    payout_date: datetime.date
    tracking_ref: str | None
    tranche_ids: tuple[str, ...] = ()
    """The event's confirmed tranches (FIFO order) — a deposit matched
    to the event allocates across them."""


@dataclass(frozen=True)
class DepositToMatch:
    """One bank deposit presented to the matcher."""

    id: str
    deposit_date: datetime.date
    amount_irr: int
    description: str = ""
    tracking_ref: str | None = None


@dataclass(frozen=True)
class MatchAllocation:
    """One tranche allocation inside a match decision."""

    tranche_id: str
    allocated_amount_irr: int
    settlement_event_id: str | None = None


@dataclass(frozen=True)
class MatchSuggestion:
    """One ranked suggestion for a rule-3 (amount-only) decision."""

    target_kind: str  # "event" | "tranche"
    target_id: str
    target_label: str
    confidence: Decimal
    reason: str


@dataclass(frozen=True)
class DepositMatchDecision:
    """The matcher’s decision for one deposit (pure).

    ``method`` is ``exact_reference`` / ``amount_and_date`` /
    ``amount_only`` / ``none`` / ``manual``. ``allocations`` is
    non-empty exactly when the deposit auto-matches (rules 1–2);
    ``suggestions`` is non-empty exactly for rule 3; ``overpayment_irr``
    is the deposit remainder above the matched tranches’ outstanding
    — parked as an unallocated receipt, never auto-allocated.
    """

    deposit_id: str
    method: str
    confidence: Decimal
    allocations: tuple[MatchAllocation, ...] = ()
    settlement_event_id: str | None = None
    suggestions: tuple[MatchSuggestion, ...] = ()
    overpayment_irr: int = 0

    @property
    def auto_matched(self) -> bool:
        """True when the deposit auto-matches (rules 1–2)."""
        return self.method in ("exact_reference", "amount_and_date") and bool(self.allocations)


@dataclass(frozen=True)
class TieOutResult:
    """The mandatory tie-out (C6) for one settlement batch.

    ``holds`` is True exactly when ``|expected − actual| <=
    tolerance``. ``difference_irr`` is signed (expected − actual):
    positive means money is missing, negative means the bank sent
    more than the file confirmed. A held tie-out with a non-zero
    difference inside tolerance is still a hold — the difference is
    recorded, never silently absorbed.
    """

    holds: bool
    confirmed_total_irr: int
    deductions_irr: int
    expected_deposits_irr: int
    matched_deposits_irr: int
    difference_irr: int
    tolerance_irr: int


def _normalise_reference(value: str) -> str:
    """Normalise a reference for exact comparison: digits + alphanumerics only."""
    return re.sub(r"[^0-9a-zA-Z]", "", normalize_digits(value).strip().lower())


def _reference_tokens(description: str) -> set[str]:
    """Digit/letter tokens of a description usable as reference lookups."""
    tokens = set()
    for token in re.split(r"\s+", normalize_digits(description)):
        cleaned = _normalise_reference(token)
        if len(cleaned) >= MIN_REFERENCE_TOKEN_LENGTH:
            tokens.add(cleaned)
    return tokens


def _deposit_refs(deposit: DepositToMatch) -> set[str]:
    """Every reference a deposit carries (explicit + narrative tokens)."""
    refs: set[str] = set()
    if deposit.tracking_ref:
        ref = _normalise_reference(deposit.tracking_ref)
        if ref:
            refs.add(ref)
    refs |= _reference_tokens(deposit.description)
    refs.discard("")
    return refs


def batch_match_allocations(
    event_net_irr: int,
    tranches: Sequence[TrancheCandidate],
    tolerance_irr: int = 0,
) -> tuple[tuple[MatchAllocation, ...], int]:
    """Allocate an event's net across due tranches, FIFO by due date.

    Tranches are taken in due-date order (then index); each takes
    ``min(outstanding, remaining)`` until the event net is exhausted.
    Returns ``(allocations, residual_irr)`` where the residual is the
    event net left unallocated (``> tolerance`` means the batch does
    not cover the event — the caller raises ``amount_mismatch``).
    A zero or negative event net allocates nothing.
    """
    ordered = sorted(tranches, key=lambda t: (t.expected_date, t.tranche_index, t.id))
    allocations: list[MatchAllocation] = []
    remaining = event_net_irr
    for tranche in ordered:
        if remaining <= 0:
            break
        if tranche.outstanding_net_irr <= 0:
            continue
        take = min(tranche.outstanding_net_irr, remaining)
        allocations.append(
            MatchAllocation(tranche_id=tranche.id, allocated_amount_irr=take)
        )
        remaining -= take
    return tuple(allocations), remaining


def compute_tieout(
    *,
    confirmed_total_irr: int,
    deductions_irr: int,
    matched_deposits_irr: int,
    tolerance_irr: int,
) -> TieOutResult:
    """Compute the mandatory tie-out (C6) for one batch.

    ``Σ confirmed tranche nets − Σ documented period deductions``
    must equal ``Σ matched bank deposits`` within tolerance.
    Undocumented deductions are NOT subtracted here — they surface
    as the difference and become an ``amount_mismatch`` exception.
    """
    expected = confirmed_total_irr - deductions_irr
    difference = expected - matched_deposits_irr
    return TieOutResult(
        holds=abs(difference) <= tolerance_irr,
        confirmed_total_irr=confirmed_total_irr,
        deductions_irr=deductions_irr,
        expected_deposits_irr=expected,
        matched_deposits_irr=matched_deposits_irr,
        difference_irr=difference,
        tolerance_irr=tolerance_irr,
    )


def match_deposit(
    deposit: DepositToMatch,
    events: Sequence[EventCandidate],
    tranches: Sequence[TrancheCandidate],
    event_tranches: Mapping[str, Sequence[TrancheCandidate]],
    *,
    window_days: int = 2,
) -> DepositMatchDecision:
    """Decide one deposit's match (C5, rules in confidence order).

    ``events`` are the installation's settlement events awaiting a
    deposit; ``tranches`` are open tranches with no event yet (the
    reverse-matching C7 candidates). ``event_tranches`` maps each
    event to its confirmed tranches (FIFO order) for allocation.
    """
    base = DepositMatchDecision(
        deposit_id=deposit.id, method="none", confidence=Decimal("0")
    )
    if deposit.amount_irr <= 0:
        return base

    # Rule 1 — exact reference: an event's tracking ref in the deposit.
    refs = _deposit_refs(deposit)
    if refs:
        for event in events:
            if not event.tracking_ref:
                continue
            if _normalise_reference(event.tracking_ref) in refs:
                allocations, overpayment = _allocate_to_event(
                    deposit.amount_irr, event, event_tranches.get(event.id, ())
                )
                if allocations:
                    return DepositMatchDecision(
                        deposit_id=deposit.id,
                        method="exact_reference",
                        confidence=CONFIDENCE_EXACT_REFERENCE,
                        allocations=allocations,
                        settlement_event_id=event.id,
                        overpayment_irr=overpayment,
                    )
                # The reference matched but the event's tranches are
                # already fully received: the deposit is an
                # overpayment on a known event, not an unknown one.
                return DepositMatchDecision(
                    deposit_id=deposit.id,
                    method="exact_reference",
                    confidence=CONFIDENCE_EXACT_REFERENCE,
                    settlement_event_id=event.id,
                    overpayment_irr=deposit.amount_irr,
                )

    # Rule 2 — amount + date within the window; auto-match when unique.
    rule2 = _rule2_candidates(deposit, events, tranches, window_days)
    if len(rule2) == 1:
        _kind, target = rule2[0]
        if isinstance(target, EventCandidate):
            event = target
            allocations, overpayment = _allocate_to_event(
                deposit.amount_irr, event, event_tranches.get(event.id, ())
            )
            return DepositMatchDecision(
                deposit_id=deposit.id,
                method="amount_and_date",
                confidence=CONFIDENCE_AMOUNT_AND_DATE,
                allocations=allocations,
                settlement_event_id=event.id,
                overpayment_irr=overpayment,
            )
        tranche = target
        take = min(tranche.outstanding_net_irr, deposit.amount_irr)
        return DepositMatchDecision(
            deposit_id=deposit.id,
            method="amount_and_date",
            confidence=CONFIDENCE_AMOUNT_AND_DATE,
            allocations=(
                MatchAllocation(tranche_id=tranche.id, allocated_amount_irr=take),
            ),
            overpayment_irr=deposit.amount_irr - take,
        )

    # Rule 3 — amount only: ranked suggestions, no auto-match.
    suggestions = _amount_only_suggestions(deposit, events, tranches, window_days)
    if suggestions:
        return DepositMatchDecision(
            deposit_id=deposit.id,
            method="amount_only",
            confidence=suggestions[0].confidence,
            suggestions=suggestions,
        )
    return base


def _allocate_to_event(
    amount_irr: int,
    event: EventCandidate,
    event_tranche_list: Sequence[TrancheCandidate],
) -> tuple[tuple[MatchAllocation, ...], int]:
    """Allocate a deposit across an event's tranches (FIFO by due date).

    The residual is the deposit remainder above the tranches'
    outstanding — an overpayment, parked as an unallocated receipt
    (never auto-allocated to future tranches). A partial allocation
    (deposit smaller than the outstanding) leaves residual 0: the
    rest of the tranche stays open, it is not an overpayment.
    """
    allocations, residual = batch_match_allocations(amount_irr, event_tranche_list)
    allocated = tuple(
        MatchAllocation(
            tranche_id=alloc.tranche_id,
            allocated_amount_irr=alloc.allocated_amount_irr,
            settlement_event_id=event.id,
        )
        for alloc in allocations
    )
    return allocated, residual


def _rule2_candidates(
    deposit: DepositToMatch,
    events: Sequence[EventCandidate],
    tranches: Sequence[TrancheCandidate],
    window_days: int,
) -> list[tuple[str, EventCandidate | TrancheCandidate]]:
    """Amount + date (±window) candidates, exact amount, no tolerance."""
    found: list[tuple[str, EventCandidate | TrancheCandidate]] = []
    for event in events:
        if event.net_irr != deposit.amount_irr:
            continue
        if abs((event.payout_date - deposit.deposit_date).days) > window_days:
            continue
        found.append(("event", event))
    for tranche in tranches:
        if tranche.outstanding_net_irr != deposit.amount_irr:
            continue
        if abs((tranche.expected_date - deposit.deposit_date).days) > window_days:
            continue
        found.append(("tranche", tranche))
    return found


def _suggestion_confidence(days_apart: int) -> Decimal:
    """Rule-3 confidence: 0.70 at the same date, down 0.05/day, floor 0.50."""
    return max(
        CONFIDENCE_SUGGESTION_MIN,
        CONFIDENCE_SUGGESTION_MAX - Decimal("0.05") * min(days_apart, 4),
    )


def _amount_only_suggestions(
    deposit: DepositToMatch,
    events: Sequence[EventCandidate],
    tranches: Sequence[TrancheCandidate],
    window_days: int,
) -> tuple[MatchSuggestion, ...]:
    """Rank amount-matching candidates by date proximity (rule 3).

    Only exact-amount candidates are suggested; a candidate outside
    the date window still appears (ranked lower) so the human sees
    the full picture — the window gates auto-matching, not suggestions.
    """
    scored: list[tuple[Decimal, int, MatchSuggestion]] = []
    for event in events:
        if event.net_irr != deposit.amount_irr:
            continue
        days = abs((event.payout_date - deposit.deposit_date).days)
        scored.append(
            (
                _suggestion_confidence(days),
                days,
                MatchSuggestion(
                    target_kind="event",
                    target_id=event.id,
                    target_label=f"رویداد تسویه {event.payout_date.isoformat()} — {event.net_irr:,} ریال",
                    confidence=_suggestion_confidence(days),
                    reason="مبلغ با خالص رویداد تسویه برابر است",
                ),
            )
        )
    for tranche in tranches:
        if tranche.outstanding_net_irr != deposit.amount_irr:
            continue
        days = abs((tranche.expected_date - deposit.deposit_date).days)
        scored.append(
            (
                _suggestion_confidence(days),
                days,
                MatchSuggestion(
                    target_kind="tranche",
                    target_id=tranche.id,
                    target_label=(
                        f"قسط {tranche.tranche_index} (موعد {tranche.expected_date.isoformat()})"
                        f" — {tranche.outstanding_net_irr:,} ریال"
                    ),
                    confidence=_suggestion_confidence(days),
                    reason="مبلغ با ماندهٔ قسط برابر است",
                ),
            )
        )
    scored.sort(key=lambda item: (-item[0], item[1]))
    return tuple(suggestion for _, _, suggestion in scored[:MAX_SUGGESTIONS])
