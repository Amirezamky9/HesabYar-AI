"""Unit tests for the three-priority bank matcher (US-514, B2).

Pure: no I/O, no database. Candidate amounts are magnitudes (always
positive); the direction field carries the sign.
"""

from __future__ import annotations

import datetime

from hesabyar.domain.bank.matching import LineToMatch, MatchCandidate, match_statement_line
from hesabyar.domain.bank.models import BankMatchRule, MatchTargetType, ReconcileOutcome
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.integrations.models import SuggestionMode

DAY = datetime.date(2026, 11, 17)


def _date(days: int = 0) -> FiscalDate:
    return FiscalDate(DAY + datetime.timedelta(days=days))


def _line(**overrides) -> LineToMatch:
    base = {
        "line_id": 1,
        "date": _date(),
        "amount_irr": 1_000_000,
        "description": "واریز مشتری",
    }
    base.update(overrides)
    return LineToMatch(**base)


def _candidate(**overrides) -> MatchCandidate:
    base = {
        "kind": MatchTargetType.INVOICE,
        "id": 10,
        "reference": "INV-100",
        "number": "INV-100",
        "amount_irr": 1_000_000,
        "date": _date(),
        "direction": "in",
        "party_id": 5,
        "party_name": "مشتری نمونه",
        "account_id": 501,
        "label": "فاکتور INV-100 — مشتری نمونه",
    }
    base.update(overrides)
    return MatchCandidate(**base)


def _rule(**overrides) -> BankMatchRule:
    base = {
        "id": "rule-1",
        "tenant_id": "t1",
        "company_id": "c1",
        "bank_key": "",
        "pattern": "اسنپ",
        "direction": "out",
        "target_account_id": 601,
        "target_party_id": None,
        "target_kind": "account",
        "mode": SuggestionMode.SUGGEST,
        "confirmations": 1,
    }
    base.update(overrides)
    return BankMatchRule(**base)


# -- short-circuits ------------------------------------------------------------------


def test_already_matched_line_short_circuits() -> None:
    result = match_statement_line(
        _line(related_invoice_id=10), [_candidate()], [], 2
    )
    assert result.outcome == ReconcileOutcome.ALREADY_MATCHED
    assert result.target_id == 10


def test_zero_amount_line_is_skipped() -> None:
    result = match_statement_line(_line(amount_irr=0), [_candidate()], [], 2)
    assert result.outcome == ReconcileOutcome.SKIPPED_ZERO


# -- priority 1: exact reference ------------------------------------------------------


def test_p1_exact_reference_on_line_number() -> None:
    result = match_statement_line(
        _line(number="INV-100", amount_irr=999), [_candidate()], [], 2
    )
    assert result.outcome == ReconcileOutcome.MATCHED
    assert result.priority == 1
    assert result.confidence == 100
    assert result.target_id == 10


def test_p1_exact_reference_from_description_token() -> None:
    result = match_statement_line(
        _line(description="واریز بابت فاکتور INV-100", amount_irr=999),
        [_candidate()],
        [],
        2,
    )
    assert result.outcome == ReconcileOutcome.MATCHED
    assert result.priority == 1


def test_p1_reference_is_normalised() -> None:
    # Persian digits + dashes in the reference still match.
    result = match_statement_line(
        _line(number="INV_۱۰۰", amount_irr=999),
        [_candidate(reference="INV100", number=None)],
        [],
        2,
    )
    assert result.outcome == ReconcileOutcome.MATCHED


def test_p1_short_tokens_do_not_match() -> None:
    # A 5-digit token is too short to be a tracking number.
    result = match_statement_line(
        _line(description="واریز 12345", amount_irr=999),
        [_candidate(reference="12345", number=None)],
        [],
        2,
    )
    assert result.outcome != ReconcileOutcome.MATCHED or result.priority != 1


def test_p1_beats_p2() -> None:
    # P1 wins even when a different candidate would P2-match.
    p2_candidate = _candidate(id=20, reference="OTHER", number="OTHER")
    result = match_statement_line(
        _line(number="INV-100"), [_candidate(), p2_candidate], [], 2
    )
    assert result.outcome == ReconcileOutcome.MATCHED
    assert result.priority == 1
    assert result.target_id == 10


# -- priority 2: amount + date + direction ----------------------------------------------


def test_p2_unique_candidate_auto_matches() -> None:
    result = match_statement_line(_line(), [_candidate()], [], 2)
    assert result.outcome == ReconcileOutcome.MATCHED
    assert result.priority == 2
    assert result.confidence == 95
    assert result.target_id == 10


def test_p2_respects_direction() -> None:
    # A deposit never matches a supplier invoice (money out).
    result = match_statement_line(
        _line(), [_candidate(direction="out")], [], 2
    )
    assert result.outcome != ReconcileOutcome.MATCHED


def test_p2_respects_window() -> None:
    result = match_statement_line(_line(), [_candidate(date=_date(2))], [], 2)
    assert result.outcome == ReconcileOutcome.MATCHED  # exactly at the window edge
    result = match_statement_line(_line(), [_candidate(date=_date(3))], [], 2)
    assert result.outcome != ReconcileOutcome.MATCHED


def test_p2_exact_amount_no_tolerance() -> None:
    result = match_statement_line(_line(), [_candidate(amount_irr=1_000_001)], [], 2)
    assert result.outcome != ReconcileOutcome.MATCHED


def test_p2_withdrawal_matches_supplier_invoice() -> None:
    line = _line(amount_irr=-500_000, description="پرداخت تأمین‌کننده")
    candidate = _candidate(
        kind=MatchTargetType.INVOICE,
        id=30,
        reference="SUP-1",
        number="SUP-1",
        amount_irr=500_000,
        direction="out",
        party_name="تأمین‌کننده",
    )
    result = match_statement_line(line, [candidate], [], 2)
    assert result.outcome == ReconcileOutcome.MATCHED
    assert result.priority == 2
    assert result.target_id == 30


def test_p2_multiple_candidates_is_ambiguous() -> None:
    candidates = [
        _candidate(id=10),
        _candidate(id=20, reference="INV-200", number="INV-200", date=_date(1)),
    ]
    result = match_statement_line(_line(), candidates, [], 2)
    assert result.outcome == ReconcileOutcome.AMBIGUOUS
    assert result.confidence == 70
    assert len(result.suggestions) == 2
    assert {s.target_id for s in result.suggestions} == {10, 20}


# -- priority 3: rules + scored suggestions -----------------------------------------------


def test_p3_auto_rule_codes_the_line() -> None:
    line = _line(amount_irr=-50_000, description="پرداخت اسنپ فود")
    result = match_statement_line(line, [], [_rule(mode=SuggestionMode.AUTO)], 2)
    assert result.outcome == ReconcileOutcome.MATCHED
    assert result.priority == 3
    assert result.confidence == 85
    assert result.target_type == MatchTargetType.RULE
    assert result.target_id == "rule-1"


def test_p3_suggest_rule_only_suggests() -> None:
    line = _line(amount_irr=-50_000, description="پرداخت اسنپ فود")
    result = match_statement_line(line, [], [_rule()], 2)
    assert result.outcome == ReconcileOutcome.SUGGESTED
    assert result.priority == 3
    assert len(result.suggestions) == 1
    assert result.suggestions[0].target_type == MatchTargetType.RULE


def test_p3_rule_respects_direction() -> None:
    line = _line(amount_irr=50_000, description="واریز اسنپ")  # money in, rule is 'out'
    result = match_statement_line(line, [], [_rule()], 2)
    assert result.outcome == ReconcileOutcome.UNMATCHED


def test_p3_longest_rule_pattern_wins() -> None:
    line = _line(amount_irr=-50_000, description="پرداخت اسنپ فود تهران")
    short = _rule(id="short", pattern="اسنپ")
    long = _rule(id="long", pattern="اسنپ فود")
    result = match_statement_line(line, [], [short, long], 2)
    assert result.suggestions[0].target_id == "long"


def test_p3_scored_suggestion_amount_plus_party() -> None:
    # Amount (40) + date (20) + party name in description (25) = 85.
    # (Not a P2 match: the date is outside the window.)
    line = _line(description="واریز مشتری نمونه", date=_date(10))
    candidate = _candidate(date=_date(0))
    result = match_statement_line(line, [candidate], [], 2)
    assert result.outcome == ReconcileOutcome.SUGGESTED
    assert result.suggestions[0].confidence == 65  # amount 40 + party 25
    assert result.suggestions[0].target_id == 10


def test_p3_below_threshold_is_unmatched() -> None:
    # Only the date matches (20 < 50 threshold) — noise, not help.
    line = _line(description="واریز ناشناس", amount_irr=777)
    result = match_statement_line(line, [_candidate()], [], 2)
    assert result.outcome == ReconcileOutcome.UNMATCHED
    assert result.suggestions == ()


def test_p3_top_three_suggestions_sorted_by_score() -> None:
    # No P2 match is possible here (every candidate's date is outside
    # the window or its amount differs), so P3 scores decide.
    line = _line(
        description="واریز مشتری نمونه پیگیری 12345678", date=_date(10)
    )
    candidates = [
        # amount 40 + party 25 = 65
        _candidate(id=10, amount_irr=1_000_000, date=_date(0)),
        # amount only = 40 < 50: excluded
        _candidate(id=20, amount_irr=1_000_000, date=_date(0), party_name="غریبه"),
        # date 20 + party 25 = 45 < 50: excluded
        _candidate(id=30, amount_irr=2_000_000, date=_date(10)),
        # amount 40 + party 25 + partial reference 15 = 80
        _candidate(
            id=40,
            amount_irr=1_000_000,
            date=_date(0),
            reference="12345678XX",
            number=None,
        ),
        # amount 40 + partial reference 15 = 55
        _candidate(
            id=50,
            amount_irr=1_000_000,
            date=_date(0),
            party_name="غریبه",
            reference="12345678YY",
            number=None,
        ),
    ]
    result = match_statement_line(line, candidates, [], 2)
    assert result.outcome == ReconcileOutcome.SUGGESTED
    assert [s.target_id for s in result.suggestions] == [40, 10, 50]
    assert [s.confidence for s in result.suggestions] == [80, 65, 55]


def test_p3_wrong_direction_never_suggested() -> None:
    # Same amount, same date, party in the description — but a
    # supplier invoice is never suggested for a deposit.
    line = _line(description="واریز مشتری نمونه")
    candidate = _candidate(direction="out")
    result = match_statement_line(line, [candidate], [], 2)
    assert result.outcome == ReconcileOutcome.UNMATCHED


def test_unmatched_when_no_candidates_and_no_rules() -> None:
    result = match_statement_line(_line(), [], [], 2)
    assert result.outcome == ReconcileOutcome.UNMATCHED
