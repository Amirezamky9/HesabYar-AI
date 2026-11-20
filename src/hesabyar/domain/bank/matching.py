"""Three-priority bank reconciliation matching (US-514, B2).

Pure functions over statement lines + engine candidates + learned
rules — no I/O, no database, no engine calls. The application service
loads the inputs through the ports and applies the decisions.

Priorities (phased design §4 §2.3):

1. **Exact reference** — the line's bank reference (``number``) or a
   reference token in its description equals the invoice/payment
   reference or number. Confidence 100; auto-applied.
2. **Amount + date + direction** — exact amount, date within
   ±``match_window_days`` (default 2, from installation settings),
   same money direction (deposit ↔ customer invoice / receivable
   payment; withdrawal ↔ supplier invoice / payable payment). Unique
   candidate → auto-applied (confidence 95); several → ``ambiguous``
   (a human picks — never guessed).
3. **Smart suggestion** — scored on amount, date proximity, party
   name in the description, and learned rules. Suggestions carry a
   confidence and a human approves; nothing auto-posts from a
   suggestion except an AUTO-mode learned rule coding the line to its
   account (the rule's mode IS the operator's trust decision).

Destination: every decision records the statement journal's bank
account (the destination the money went to). Invoices and payments
carry no destination Shaba in Tryton 8.2, so no per-candidate
destination check is invented here — settlement tranches (5پ) and
cheque clearing (5ث) carry the destination and add that filter when
they become match candidates.

Unmatched deposits NEVER auto-post to revenue: an unmatched line
yields a ``bank_unmatched`` review item with its mapping suggestions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from hesabyar.domain.bank.models import (
    BankMatchRule,
    MatchSuggestion,
    MatchTargetType,
    ReconcileLineResult,
    ReconcileOutcome,
)
from hesabyar.domain.bank.parsing import normalize_digits, normalize_persian_text
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.integrations.models import SuggestionMode

#: Minimum reference token length for a description-token P1 hit.
#: Short numbers (years, small amounts) appear in descriptions by
#: accident; a real tracking number is longer.
MIN_REFERENCE_TOKEN_LENGTH = 6

#: P3 suggestion threshold: a candidate scoring below this is not
#: suggested at all (noise, not help).
SUGGESTION_MIN_SCORE = 50

#: P3 score weights (sum to 100; a perfect P3 scores 95, never 100 —
#: only an exact reference earns 100).
_SCORE_AMOUNT = 40
_SCORE_DATE = 20
_SCORE_PARTY = 25
_SCORE_REFERENCE_PARTIAL = 15


@dataclass(frozen=True)
class MatchCandidate:
    """One engine record a statement line may match (invoice/payment)."""

    kind: MatchTargetType
    id: int
    reference: str
    number: str | None
    amount_irr: int  # magnitude (always positive); direction carries the sign
    date: FiscalDate
    direction: str  # 'in' (money in) | 'out' (money out)
    party_id: int | None
    party_name: str
    account_id: int | None
    label: str


@dataclass(frozen=True)
class LineToMatch:
    """One statement line presented to the matcher."""

    line_id: int
    date: FiscalDate
    amount_irr: int
    description: str
    number: str | None = None
    related_invoice_id: int | None = None

    @property
    def direction(self) -> str:
        """Money direction of the line ('in' for deposits)."""
        return "in" if self.amount_irr > 0 else "out"


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


def _priority1_match(
    line: LineToMatch, candidates: list[MatchCandidate]
) -> MatchCandidate | None:
    """Exact reference: line number or a description token equals a candidate reference/number."""
    line_refs = set()
    if line.number:
        ref = _normalise_reference(line.number)
        if ref:
            line_refs.add(ref)
    line_refs |= _reference_tokens(line.description)
    if not line_refs:
        return None
    for candidate in candidates:
        candidate_refs = {_normalise_reference(candidate.reference)}
        if candidate.number:
            candidate_refs.add(_normalise_reference(candidate.number))
        candidate_refs.discard("")
        if line_refs & candidate_refs:
            return candidate
    return None


def _priority2_candidates(
    line: LineToMatch, candidates: list[MatchCandidate], window_days: int
) -> list[MatchCandidate]:
    """Amount + date (±window) + direction, exact amount, no tolerance."""
    matches = []
    for candidate in candidates:
        if candidate.direction != line.direction:
            continue
        if candidate.amount_irr != abs(line.amount_irr):
            continue
        if abs((candidate.date.gregorian_date - line.date.gregorian_date).days) > window_days:
            continue
        matches.append(candidate)
    return matches


def _score_candidate(line: LineToMatch, candidate: MatchCandidate, window_days: int) -> int:
    """P3 score (0–95) for one candidate against one line.

    A wrong-direction candidate scores 0 — a deposit is never
    suggested a supplier invoice (and vice versa), however close the
    amount and date are.
    """
    if candidate.direction != line.direction:
        return 0
    score = 0
    reasons = []
    if candidate.amount_irr == abs(line.amount_irr):
        score += _SCORE_AMOUNT
        reasons.append("amount")
    if abs((candidate.date.gregorian_date - line.date.gregorian_date).days) <= window_days:
        score += _SCORE_DATE
        reasons.append("date")
    party = normalize_persian_text(candidate.party_name)
    if party and len(party) >= 3 and party in normalize_persian_text(line.description):
        score += _SCORE_PARTY
        reasons.append("party")
    candidate_refs = {_normalise_reference(candidate.reference)}
    if candidate.number:
        candidate_refs.add(_normalise_reference(candidate.number))
    candidate_refs.discard("")
    line_tokens = _reference_tokens(line.description)
    if line.number:
        line_tokens.add(_normalise_reference(line.number))
    line_tokens.discard("")
    long_refs = [ref for ref in candidate_refs if len(ref) >= 8]
    long_tokens = [token for token in line_tokens if len(token) >= 8]
    if any(
        ref[:8] in token or token[:8] in ref for ref in long_refs for token in long_tokens
    ):
        score += _SCORE_REFERENCE_PARTIAL
        reasons.append("reference")
    return min(score, 95)


def _rule_hit(
    line: LineToMatch, rules: list[BankMatchRule]
) -> tuple[BankMatchRule, int] | None:
    """Best learned-rule hit for a line (longest pattern wins)."""
    description = normalize_persian_text(line.description)
    best: tuple[BankMatchRule, int] | None = None
    for rule in rules:
        pattern = normalize_persian_text(rule.pattern)
        if not pattern or pattern not in description:
            continue
        if rule.direction not in ("any", line.direction):
            continue
        if best is None or len(pattern) > len(normalize_persian_text(best[0].pattern)):
            best = (rule, 85)
    return best


def match_statement_line(
    line: LineToMatch,
    candidates: list[MatchCandidate],
    rules: list[BankMatchRule],
    window_days: int = 2,
) -> ReconcileLineResult:
    """Decide one statement line's reconcile outcome (pure).

    The caller applies ``matched`` decisions (writes the line's
    ``related_to``/account/party through the engine port) and files
    review items for ``unmatched``/``ambiguous`` lines; ``suggested``
    decisions wait for a human on the reconcile page.
    """
    base = ReconcileLineResult(
        line_id=line.line_id,
        line_date=line.date,
        amount_irr=line.amount_irr,
        description=line.description,
        outcome=ReconcileOutcome.UNMATCHED,
    )
    if line.related_invoice_id is not None:
        return replace(
            base,
            outcome=ReconcileOutcome.ALREADY_MATCHED,
            target_type=MatchTargetType.INVOICE,
            target_id=line.related_invoice_id,
            target_label=f"فاکتور {line.related_invoice_id}",
        )
    if line.amount_irr == 0:
        return replace(base, outcome=ReconcileOutcome.SKIPPED_ZERO)

    # Priority 1 — exact reference.
    exact = _priority1_match(line, candidates)
    if exact is not None:
        return replace(
            base,
            outcome=ReconcileOutcome.MATCHED,
            priority=1,
            confidence=100,
            target_type=exact.kind,
            target_id=exact.id,
            target_label=exact.label,
        )

    # Priority 2 — amount + date + direction.
    p2 = _priority2_candidates(line, candidates, window_days)
    if len(p2) == 1:
        only = p2[0]
        return replace(
            base,
            outcome=ReconcileOutcome.MATCHED,
            priority=2,
            confidence=95,
            target_type=only.kind,
            target_id=only.id,
            target_label=only.label,
        )
    if len(p2) > 1:
        return replace(
            base,
            outcome=ReconcileOutcome.AMBIGUOUS,
            priority=2,
            confidence=70,
            suggestions=tuple(
                MatchSuggestion(
                    target_type=c.kind,
                    target_id=c.id,
                    target_label=c.label,
                    confidence=70,
                    reason="مبلغ و تاریخ با چند رکورد هم‌خوانی دارد؛ انسان انتخاب می‌کند",
                )
                for c in p2
            ),
        )

    # Priority 3 — learned rules first (an AUTO rule codes the line),
    # then scored suggestions from the candidates.
    rule = _rule_hit(line, rules)
    if rule is not None:
        matched_rule, confidence = rule
        if matched_rule.mode == SuggestionMode.AUTO:
            return replace(
                base,
                outcome=ReconcileOutcome.MATCHED,
                priority=3,
                confidence=confidence,
                target_type=MatchTargetType.RULE,
                target_id=matched_rule.id,
                target_label=f"قاعدهٔ «{matched_rule.pattern}»",
            )
        return replace(
            base,
            outcome=ReconcileOutcome.SUGGESTED,
            priority=3,
            confidence=confidence,
            suggestions=(
                MatchSuggestion(
                    target_type=MatchTargetType.RULE,
                    target_id=matched_rule.id,
                    target_label=f"قاعدهٔ «{matched_rule.pattern}»",
                    confidence=confidence,
                    reason="شرح با یک نگاشت یادگرفته‌شده هم‌خوانی دارد",
                ),
            ),
        )
    scored = [
        (candidate, _score_candidate(line, candidate, window_days)) for candidate in candidates
    ]
    scored = [(c, s) for c, s in scored if s >= SUGGESTION_MIN_SCORE]
    scored.sort(key=lambda item: item[1], reverse=True)
    if scored:
        return replace(
            base,
            outcome=ReconcileOutcome.SUGGESTED,
            priority=3,
            confidence=scored[0][1],
            suggestions=tuple(
                MatchSuggestion(
                    target_type=c.kind,
                    target_id=c.id,
                    target_label=c.label,
                    confidence=s,
                    reason="پیشنهاد هوشمند بر پایهٔ مبلغ، تاریخ و شرح",
                )
                for c, s in scored[:3]
            ),
        )
    return replace(base, outcome=ReconcileOutcome.UNMATCHED)
