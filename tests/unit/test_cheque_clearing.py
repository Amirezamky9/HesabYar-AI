"""Unit tests for the clearing matcher (US-534, phased design §5 §1.5).

Pure-domain tests: no I/O, no engine, no database. They pin the
matching keys in strength order (exact amount zero-tolerance,
leaf number, date window, destination account, direction), the
four confidence levels, the Melli leaf fallback (§9.2 #7), the
unset-window cap (§9.2 #6), issued-cheque withdrawals, and the
aging/stale rules (never auto-bounce — a stale cheque is only
flagged).
"""

from __future__ import annotations

import datetime

from hesabyar.domain.cheques.clearing import (
    ClearingCandidate,
    ClearingConfidence,
    ClearingLine,
    aging_for,
    extract_leaf_numbers,
    leaf_number_in_description,
    match_clearing_line,
)
from hesabyar.domain.common.dates import FiscalDate

DEPOSIT = FiscalDate(datetime.date(2026, 11, 10))
COMPANY_ACCOUNT = 100
OTHER_ACCOUNT = 200
AMOUNT = 1_500_000  # 150,000 Toman in Rial


def _candidate(**overrides: object) -> ClearingCandidate:
    base: dict[str, object] = {
        "cheque_id": "cheque-1",
        "sayad_id": "1111222233334444",
        "leaf_number": "987654",
        "amount_irr": AMOUNT,
        "deposit_date": DEPOSIT,
        "destination_bank_account_id": COMPANY_ACCOUNT,
        "direction": "in",
        "kind": "received",
    }
    base.update(overrides)
    return ClearingCandidate(**base)  # type: ignore[arg-type]


def _line(**overrides: object) -> ClearingLine:
    base: dict[str, object] = {
        "line_id": 9001,
        "statement_id": 700,
        "date": FiscalDate(datetime.date(2026, 11, 12)),
        "amount_irr": AMOUNT,
        "description": "وصول چک 987654",
        "destination_account_id": COMPANY_ACCOUNT,
    }
    base.update(overrides)
    return ClearingLine(**base)  # type: ignore[arg-type]


def test_exact_when_all_keys_match() -> None:
    match = match_clearing_line(_line(), [_candidate()], clearing_window_days=7)
    assert match.confidence == ClearingConfidence.EXACT
    assert match.cheque_id == "cheque-1"
    assert match.leaf_matched is True


def test_probable_without_leaf_number() -> None:
    match = match_clearing_line(
        _line(description="واریز وجه"), [_candidate()], clearing_window_days=7
    )
    assert match.confidence == ClearingConfidence.PROBABLE
    assert match.cheque_id == "cheque-1"
    assert match.leaf_matched is False


def test_ambiguous_with_several_same_amount_cheques() -> None:
    candidates = [
        _candidate(cheque_id="cheque-1", sayad_id="1111222233334444"),
        _candidate(cheque_id="cheque-2", sayad_id="5555666677778888", leaf_number=None),
    ]
    match = match_clearing_line(
        _line(description="واریز وجه"), candidates, clearing_window_days=7
    )
    assert match.confidence == ClearingConfidence.AMBIGUOUS
    assert match.cheque_id is None
    assert {c.cheque_id for c in match.candidates} == {"cheque-1", "cheque-2"}


def test_unmatched_when_amount_differs_by_one_rial() -> None:
    """Zero tolerance: a one-Rial difference is unmatched, never a partial clear."""
    match = match_clearing_line(
        _line(amount_irr=AMOUNT + 1), [_candidate()], clearing_window_days=7
    )
    assert match.confidence == ClearingConfidence.UNMATCHED


def test_unmatched_when_direction_is_wrong() -> None:
    match = match_clearing_line(
        _line(amount_irr=-AMOUNT), [_candidate()], clearing_window_days=7
    )
    assert match.confidence == ClearingConfidence.UNMATCHED


def test_unmatched_when_destination_account_differs() -> None:
    match = match_clearing_line(
        _line(destination_account_id=OTHER_ACCOUNT),
        [_candidate()],
        clearing_window_days=7,
    )
    assert match.confidence == ClearingConfidence.UNMATCHED


def test_unmatched_before_deposit_and_after_window() -> None:
    before = match_clearing_line(
        _line(date=FiscalDate(datetime.date(2026, 11, 9))),
        [_candidate()],
        clearing_window_days=7,
    )
    assert before.confidence == ClearingConfidence.UNMATCHED
    after = match_clearing_line(
        _line(date=FiscalDate(datetime.date(2026, 11, 18))),
        [_candidate()],
        clearing_window_days=7,
    )
    assert after.confidence == ClearingConfidence.UNMATCHED
    last_day = match_clearing_line(
        _line(date=FiscalDate(datetime.date(2026, 11, 17))),
        [_candidate()],
        clearing_window_days=7,
    )
    assert last_day.confidence == ClearingConfidence.EXACT


def test_unset_window_caps_confidence_at_probable() -> None:
    """No hard-coded window (§9.2 #6): unset means unbounded above AND capped at probable."""
    late = match_clearing_line(
        _line(date=FiscalDate(datetime.date(2027, 1, 1))),
        [_candidate()],
        clearing_window_days=None,
    )
    assert late.confidence == ClearingConfidence.PROBABLE
    assert late.window_unset is True
    assert any("unset" in reason for reason in late.reasons)


def test_melli_caps_confidence_at_probable_even_with_leaf() -> None:
    """Melli leaf-in-description is UNVERIFIED (§9.2 #7): never exact on it."""
    match = match_clearing_line(
        _line(bank_key="melli"), [_candidate()], clearing_window_days=7
    )
    assert match.confidence == ClearingConfidence.PROBABLE
    assert match.leaf_matched is True  # evidence recorded, never trusted to exact
    assert any("§9.2 #7" in reason for reason in match.reasons)


def test_issued_cheque_matches_withdrawal() -> None:
    issued = _candidate(
        cheque_id="cheque-issued",
        sayad_id="9999888877776666",
        direction="out",
        kind="issued",
    )
    match = match_clearing_line(
        _line(amount_irr=-AMOUNT, description="پاس چک 987654"),
        [issued],
        clearing_window_days=7,
    )
    assert match.confidence == ClearingConfidence.EXACT
    assert match.cheque_id == "cheque-issued"


def test_candidate_without_deposit_date_or_destination_never_matches() -> None:
    no_date = match_clearing_line(
        _line(), [_candidate(deposit_date=None)], clearing_window_days=7
    )
    assert no_date.confidence == ClearingConfidence.UNMATCHED
    no_account = match_clearing_line(
        _line(), [_candidate(destination_bank_account_id=None)], clearing_window_days=7
    )
    assert no_account.confidence == ClearingConfidence.UNMATCHED


def test_zero_amount_line_is_unmatched() -> None:
    match = match_clearing_line(
        _line(amount_irr=0), [_candidate()], clearing_window_days=7
    )
    assert match.confidence == ClearingConfidence.UNMATCHED


def test_leaf_number_needs_token_equality() -> None:
    assert leaf_number_in_description("987654", "وصول چک 987654") is True
    assert leaf_number_in_description("987", "وصول چک 987654") is False
    assert leaf_number_in_description(None, "وصول چک 987654") is False
    # Persian digits normalise.
    assert leaf_number_in_description("987654", "وصول چک ۹۸۷۶۵۴") is True


def test_extract_leaf_numbers_per_bank() -> None:
    assert extract_leaf_numbers("وصول چک 987654 مورخ 1405/08/20") == ("987654", "1405")
    assert extract_leaf_numbers("وصول چک 987654", bank_key="melli") == ()
    assert "987654" in extract_leaf_numbers("وصول چک 987654", bank_key="generic")


def test_aging_stale_only_when_window_passed() -> None:
    today = FiscalDate(datetime.date(2026, 11, 20))
    aged = aging_for(_candidate(), today, clearing_window_days=7)
    assert aged.days_in_clearing == 10
    assert aged.stale is True
    fresh = aging_for(_candidate(), FiscalDate(datetime.date(2026, 11, 15)), clearing_window_days=7)
    assert fresh.stale is False
    unset = aging_for(_candidate(), today, clearing_window_days=None)
    assert unset.stale is False
    assert unset.window_unset is True
    no_date = aging_for(_candidate(deposit_date=None), today, clearing_window_days=7)
    assert no_date.days_in_clearing == -1
    assert no_date.stale is False
