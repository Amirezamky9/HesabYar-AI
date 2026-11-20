"""Clearing matching — Layer 3 (phased design §5 §1.5, US-534).

Pure functions over in-clearing cheques + statement lines: no I/O,
no database, no engine calls. The application service loads the
inputs through the ports and applies the decisions.

Matching keys, in strength order:

1. **Exact amount in Rial — MANDATORY, zero tolerance** (unlike
   gateway settlement, a cheque has no fee: a mismatched deposit is
   ``unmatched`` + manual follow-up, never a partial clear — partial
   clearing does not exist).
2. **Leaf number in the transaction description** — per-bank
   extraction rule (``LEAF_EXTRACTION_RULES``). For Bank Melli the
   rule is UNVERIFIED (§9.2 #7): a Melli description never yields an
   ``exact`` on the leaf key — it falls back to ``probable``.
3. **Date window — mandatory:** ``[deposit date, deposit date +
   clearing window]``. The window is configurable per installation
   (``CHEQUE_CLEARING_WINDOW_DAYS``); its standard duration is
   UNVERIFIED (§9.2 #6) so there is NO hard-coded default. While the
   setting is unset the window has no upper bound AND the confidence
   caps at ``probable`` — the run says so in its reasons, so the
   go-live checklist item stays visible instead of silently passing.
4. **Destination bank account — mandatory:** the statement journal's
   account must be the account the cheque was deposited to.
5. **Transaction direction — mandatory:** a deposit (money in)
   clears a received cheque; a withdrawal (money out) clears an
   issued cheque.

Four confidence levels:

- ``exact`` — all keys (auto-clear with the auto-post policy, else
  draft + one-click confirm);
- ``probable`` — no leaf number (review queue);
- ``ambiguous`` — several same-amount cheques (NEVER guess; review
  queue with the candidate list);
- ``unmatched`` — the cheque stays in ``in_clearing`` + the aging
  report.

NEVER auto-bounce: nothing in this module produces a bounce. An
``in_clearing -> bounced`` transition needs a bank document (the
non-payment certificate) or an inquiry, and a stale in-clearing
cheque only appears flagged in the aging report (report 4).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import StrEnum

from hesabyar.domain.bank.parsing import normalize_digits
from hesabyar.domain.common.dates import FiscalDate

#: Per-bank leaf-number extraction rules: bank key -> regex for the
#: digit runs in a statement description that may be a leaf number.
#: ``None`` means the bank's rule is UNVERIFIED — no leaf is ever
#: extracted for it (Melli, §9.2 #7) and matching falls back to
#: ``probable``. ``generic`` is the default for banks with no
#: recorded rule: digit runs of 4+ (a real leaf serial is longer
#: than a year or a small amount, the same floor the bank matcher
#: uses for reference tokens).
LEAF_EXTRACTION_RULES: dict[str, str | None] = {
    "generic": r"\d{4,}",
    "melli": None,
}

#: Bank keys whose descriptions are NOT trusted to carry a leaf
#: number (UNVERIFIED, §9.2 #7) — leaf evidence from them caps the
#: confidence at ``probable`` instead of ``exact``.
LEAF_UNVERIFIED_BANKS: frozenset[str] = frozenset({"melli"})


class ClearingConfidence(StrEnum):
    """The four confidence levels of a clearing match (§1.5)."""

    EXACT = "exact"
    PROBABLE = "probable"
    AMBIGUOUS = "ambiguous"
    UNMATCHED = "unmatched"


@dataclass(frozen=True, slots=True)
class ClearingCandidate:
    """One in-clearing cheque a statement line may clear."""

    cheque_id: str
    sayad_id: str
    leaf_number: str | None
    amount_irr: int
    deposit_date: FiscalDate | None
    destination_bank_account_id: int | None
    direction: str  # 'in' (received cheque, deposit clears it) | 'out' (issued cheque, withdrawal clears it)
    kind: str  # 'received' | 'issued'
    state: str = "in_clearing"


@dataclass(frozen=True, slots=True)
class ClearingLine:
    """One statement line presented to the clearing matcher."""

    line_id: int
    statement_id: int
    date: FiscalDate
    amount_irr: int  # signed: positive = deposit, negative = withdrawal
    description: str
    number: str | None = None
    destination_account_id: int | None = None
    bank_key: str | None = None

    @property
    def direction(self) -> str:
        """Money direction of the line ('in' for deposits)."""
        return "in" if self.amount_irr > 0 else "out"


@dataclass(frozen=True, slots=True)
class ClearingMatch:
    """The match decision for one statement line (pure result)."""

    line_id: int
    statement_id: int
    confidence: ClearingConfidence
    candidates: tuple[ClearingCandidate, ...] = ()
    leaf_matched: bool = False
    description_leaf_numbers: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    window_days: int | None = None
    window_unset: bool = False

    @property
    def cheque_id(self) -> str | None:
        """The single matched cheque (exact/probable only)."""
        if self.confidence in (ClearingConfidence.EXACT, ClearingConfidence.PROBABLE) and len(
            self.candidates
        ) == 1:
            return self.candidates[0].cheque_id
        return None


@dataclass(frozen=True, slots=True)
class ClearingAging:
    """One in-clearing cheque's age for the aging report (report 4)."""

    cheque_id: str
    sayad_id: str
    days_in_clearing: int
    stale: bool
    window_days: int | None
    window_unset: bool


def extract_leaf_numbers(description: str, bank_key: str | None = None) -> tuple[str, ...]:
    """Extract candidate leaf numbers from a statement description.

    Per-bank rule (§1.5): Melli's rule is UNVERIFIED, so a Melli
    description yields nothing — the caller falls back to
    ``probable`` instead of guessing from an unverified pattern.
    """
    key = (bank_key or "generic").lower()
    pattern = LEAF_EXTRACTION_RULES.get(key, LEAF_EXTRACTION_RULES["generic"])
    if pattern is None:
        return ()
    return tuple(dict.fromkeys(re.findall(pattern, normalize_digits(description))))


def leaf_number_in_description(leaf_number: str | None, description: str) -> bool:
    """True when the leaf number appears as a token in the description.

    Token equality after digit normalisation (Persian/Arabic digits
    -> Latin): a substring hit is not enough — ``123`` must not match
    inside ``12345``.
    """
    if leaf_number is None:
        return False
    wanted = re.sub(r"\D", "", normalize_digits(leaf_number))
    if not wanted:
        return False
    tokens = re.split(r"\s+", normalize_digits(description))
    return any(re.sub(r"\D", "", token) == wanted for token in tokens)


def _in_window(
    line_date: FiscalDate, deposit_date: FiscalDate, window_days: int | None
) -> tuple[bool, bool]:
    """Check the clearing date window.

    Returns ``(in_window, window_unset)``. With no configured window
    there is no upper bound — only the lower bound (a clearing cannot
    predate the deposit) — and the caller caps the confidence.
    """
    delta = (line_date.gregorian_date - deposit_date.gregorian_date).days
    if delta < 0:
        return False, window_days is None
    if window_days is None:
        return True, True
    return delta <= window_days, False


def match_clearing_line(
    line: ClearingLine,
    candidates: list[ClearingCandidate],
    *,
    clearing_window_days: int | None,
) -> ClearingMatch:
    """Decide one statement line's clearing match (pure).

    Amount (exact, zero tolerance), direction and destination are
    mandatory filters; the date window is mandatory when configured.
    A candidate with no recorded deposit date or destination can
    never auto-match — the window/destination keys cannot be
    verified for it, so it is excluded (the manual clear path
    remains).
    """
    base = ClearingMatch(
        line_id=line.line_id,
        statement_id=line.statement_id,
        confidence=ClearingConfidence.UNMATCHED,
        description_leaf_numbers=extract_leaf_numbers(line.description, line.bank_key),
        window_days=clearing_window_days,
        window_unset=clearing_window_days is None,
    )
    if line.amount_irr == 0:
        return replace(base, reasons=("Zero-amount line: nothing to clear.",))
    reasons: list[str] = []
    if clearing_window_days is None:
        reasons.append(
            "Clearing window is unset (CHEQUE_CLEARING_WINDOW_DAYS — §9.2 #6); "
            "confidence caps at probable until the installation sets it."
        )
    leaf_trusted = (line.bank_key or "generic").lower() not in LEAF_UNVERIFIED_BANKS
    if not leaf_trusted:
        reasons.append(
            "Leaf number in this bank's descriptions is UNVERIFIED (§9.2 #7); "
            "confidence caps at probable."
        )
    eligible: list[ClearingCandidate] = []
    for candidate in candidates:
        if candidate.direction != line.direction:
            continue
        if candidate.amount_irr != abs(line.amount_irr):
            continue
        if candidate.destination_bank_account_id is None:
            reasons.append(
                f"Cheque {candidate.sayad_id} has no recorded destination account; skipped."
            )
            continue
        if line.destination_account_id is None:
            reasons.append("Statement journal has no destination account; cannot verify.")
            continue
        if candidate.destination_bank_account_id != line.destination_account_id:
            continue
        if candidate.deposit_date is None:
            reasons.append(
                f"Cheque {candidate.sayad_id} has no recorded deposit date; skipped."
            )
            continue
        in_window, _ = _in_window(line.date, candidate.deposit_date, clearing_window_days)
        if not in_window:
            continue
        eligible.append(candidate)
    if not eligible:
        return replace(
            base,
            reasons=tuple(
                reasons
                or ("No in-clearing cheque matches amount, direction, account and window.",)
            ),
        )
    leaf_hits = [
        candidate
        for candidate in eligible
        if leaf_number_in_description(candidate.leaf_number, line.description)
    ]
    if len(leaf_hits) == 1 and leaf_trusted and clearing_window_days is not None:
        return replace(
            base,
            confidence=ClearingConfidence.EXACT,
            candidates=(leaf_hits[0],),
            leaf_matched=True,
            reasons=tuple(reasons + ["All keys match: amount, leaf, window, account, direction."]),
        )
    if len(leaf_hits) > 1:
        return replace(
            base,
            confidence=ClearingConfidence.AMBIGUOUS,
            candidates=tuple(leaf_hits),
            leaf_matched=True,
            reasons=tuple(
                reasons
                + ["Several cheques carry the same leaf number in this description — never guess."]
            ),
        )
    if len(eligible) == 1:
        # One amount/account/window candidate, no trusted leaf key:
        # probable — unless the leaf token matched on an untrusted
        # bank (still probable, recorded as evidence, never exact).
        only = eligible[0]
        matched_here = leaf_number_in_description(only.leaf_number, line.description)
        return replace(
            base,
            confidence=ClearingConfidence.PROBABLE,
            candidates=(only,),
            leaf_matched=matched_here,
            reasons=tuple(
                reasons
                + (
                    ["Leaf number present in the description but the bank's rule is unverified."]
                    if matched_here
                    else ["No leaf number in the description — human review decides."]
                )
            ),
        )
    return replace(
        base,
        confidence=ClearingConfidence.AMBIGUOUS,
        candidates=tuple(eligible),
        reasons=tuple(
            reasons
            + ["Several same-amount cheques fit amount, window, account and direction — never guess."]
        ),
    )


def match_clearing_statement(
    lines: list[ClearingLine],
    candidates: list[ClearingCandidate],
    *,
    clearing_window_days: int | None,
) -> tuple[ClearingMatch, ...]:
    """Match every line of one statement (pure; order preserved)."""
    return tuple(
        match_clearing_line(line, candidates, clearing_window_days=clearing_window_days)
        for line in lines
    )


def aging_for(
    candidate: ClearingCandidate, today: FiscalDate, *, clearing_window_days: int | None
) -> ClearingAging:
    """Age of one in-clearing cheque for the aging report (report 4).

    ``stale`` is True only when the configured window has passed —
    with no window configured nothing is called stale (the run flags
    the unset window instead); a stale cheque stays in_clearing and
    is only flagged, never auto-bounced.
    """
    if candidate.deposit_date is None:
        return ClearingAging(
            cheque_id=candidate.cheque_id,
            sayad_id=candidate.sayad_id,
            days_in_clearing=-1,
            stale=False,
            window_days=clearing_window_days,
            window_unset=clearing_window_days is None,
        )
    days = (today.gregorian_date - candidate.deposit_date.gregorian_date).days
    stale = clearing_window_days is not None and days > clearing_window_days
    return ClearingAging(
        cheque_id=candidate.cheque_id,
        sayad_id=candidate.sayad_id,
        days_in_clearing=max(days, 0),
        stale=stale,
        window_days=clearing_window_days,
        window_unset=clearing_window_days is None,
    )
