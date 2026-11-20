"""Unit tests for settlement matching + tie-out (US-523).

Pure: batch FIFO allocation, the mandatory three-way tie-out, and
the C5 deposit matcher in confidence order (exact reference →
amount+date → amount-only suggestions → no candidate) plus reverse
matching (C7) and overpayment parking.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

from hesabyar.domain.settlement.matching import (
    DepositToMatch,
    EventCandidate,
    TrancheCandidate,
    batch_match_allocations,
    compute_tieout,
    match_deposit,
)

D = datetime.date


def _tranche(
    tranche_id: str,
    net: int,
    due: datetime.date,
    *,
    outstanding: int | None = None,
    index: int = 1,
    state: str = "due",
    period: str = "1405/07",
) -> TrancheCandidate:
    return TrancheCandidate(
        id=tranche_id,
        gateway_payment_id=f"pay-{tranche_id}",
        net_irr=net,
        outstanding_net_irr=net if outstanding is None else outstanding,
        expected_date=due,
        state=state,
        sales_period_jalali=period,
        tranche_index=index,
    )


def _event(
    event_id: str, net: int, payout: datetime.date, *, tracking_ref: str | None = None
) -> EventCandidate:
    return EventCandidate(
        id=event_id,
        net_irr=net,
        payout_date=payout,
        tracking_ref=tracking_ref,
    )


class TestBatchMatch:
    """batch_match_allocations: FIFO by due date, residual reported."""

    def test_fifo_by_due_date(self) -> None:
        """The earliest-due tranche is filled first, then the next."""
        late = _tranche("t-late", 100_000, D(2026, 11, 20), index=2)
        early = _tranche("t-early", 100_000, D(2026, 11, 10), index=1)
        allocations, residual = batch_match_allocations(
            150_000, [late, early]
        )
        assert [a.tranche_id for a in allocations] == ["t-early", "t-late"]
        assert allocations[0].allocated_amount_irr == 100_000
        assert allocations[1].allocated_amount_irr == 50_000
        assert residual == 0

    def test_partial_allocation_leaves_residual(self) -> None:
        """An event net bigger than the tranches leaves a residual."""
        allocations, residual = batch_match_allocations(
            250_000, [_tranche("t1", 100_000, D(2026, 11, 10))]
        )
        assert allocations[0].allocated_amount_irr == 100_000
        assert residual == 150_000

    def test_residual_reported_raw_for_caller_tolerance_check(self) -> None:
        """The residual is reported raw — the caller compares it with tolerance."""
        allocations, residual = batch_match_allocations(
            100_050, [_tranche("t1", 100_000, D(2026, 11, 10))], tolerance_irr=100
        )
        assert allocations[0].allocated_amount_irr == 100_000
        assert residual == 50

    def test_zero_event_net_allocates_nothing(self) -> None:
        """A zero or negative event net allocates nothing."""
        allocations, residual = batch_match_allocations(
            0, [_tranche("t1", 100_000, D(2026, 11, 10))]
        )
        assert allocations == ()
        assert residual == 0

    def test_partially_confirmed_tranche_uses_outstanding(self) -> None:
        """Allocation uses the outstanding net, not the full tranche net."""
        allocations, residual = batch_match_allocations(
            80_000,
            [_tranche("t1", 100_000, D(2026, 11, 10), outstanding=60_000)],
        )
        assert allocations[0].allocated_amount_irr == 60_000
        assert residual == 20_000


class TestTieOut:
    """compute_tieout: the mandatory three-way comparison (C6)."""

    def test_exact_hold(self) -> None:
        """Confirmed − deductions == deposits holds with zero difference."""
        result = compute_tieout(
            confirmed_total_irr=1_000_000,
            deductions_irr=50_000,
            matched_deposits_irr=950_000,
            tolerance_irr=0,
        )
        assert result.holds is True
        assert result.expected_deposits_irr == 950_000
        assert result.difference_irr == 0

    def test_missing_money_fails(self) -> None:
        """Deposits short of expected fail; the difference is positive (missing)."""
        result = compute_tieout(
            confirmed_total_irr=1_000_000,
            deductions_irr=0,
            matched_deposits_irr=900_000,
            tolerance_irr=0,
        )
        assert result.holds is False
        assert result.difference_irr == 100_000

    def test_extra_money_fails(self) -> None:
        """Deposits above expected fail; the difference is negative (extra)."""
        result = compute_tieout(
            confirmed_total_irr=1_000_000,
            deductions_irr=0,
            matched_deposits_irr=1_100_000,
            tolerance_irr=0,
        )
        assert result.holds is False
        assert result.difference_irr == -100_000

    def test_difference_within_tolerance_holds(self) -> None:
        """A difference inside tolerance still holds — and is recorded, not absorbed."""
        result = compute_tieout(
            confirmed_total_irr=1_000_000,
            deductions_irr=0,
            matched_deposits_irr=999_900,
            tolerance_irr=1_000,
        )
        assert result.holds is True
        assert result.difference_irr == 100

    def test_undocumented_deductions_are_not_subtracted(self) -> None:
        """Only documented deductions reduce the expected deposits."""
        # The gateway kept 30,000 with no documented deduction row:
        # the tie-out fails on exactly that amount.
        result = compute_tieout(
            confirmed_total_irr=1_000_000,
            deductions_irr=0,
            matched_deposits_irr=970_000,
            tolerance_irr=0,
        )
        assert result.holds is False
        assert result.difference_irr == 30_000


class TestDepositMatching:
    """match_deposit: the four C5 confidence levels + reverse matching."""

    def test_rule1_exact_reference_auto_matches(self) -> None:
        """An event tracking ref in the deposit narrative auto-matches at 1.0."""
        event = _event("e1", 500_000, D(2026, 11, 10), tracking_ref="TRK-987654")
        tranche = _tranche("t1", 500_000, D(2026, 11, 10))
        deposit = DepositToMatch(
            id="d1",
            deposit_date=D(2026, 11, 11),
            amount_irr=500_000,
            description="واریز اسنپ‌پی TRK-987654",
        )
        decision = match_deposit(
            deposit, [event], [], {"e1": [tranche]}
        )
        assert decision.method == "exact_reference"
        assert decision.confidence == Decimal("1")
        assert decision.settlement_event_id == "e1"
        assert decision.auto_matched is True
        assert decision.allocations[0].tranche_id == "t1"
        assert decision.allocations[0].allocated_amount_irr == 500_000
        assert decision.overpayment_irr == 0

    def test_rule1_short_tokens_do_not_match(self) -> None:
        """Short numbers in the narrative (years, small amounts) are not references."""
        event = _event("e1", 500_000, D(2026, 11, 10), tracking_ref="2026")
        tranche = _tranche("t1", 500_000, D(2026, 11, 10))
        deposit = DepositToMatch(
            id="d1",
            deposit_date=D(2026, 11, 11),
            amount_irr=500_000,
            description="واریز 2026",
        )
        decision = match_deposit(deposit, [event], [], {"e1": [tranche]})
        # The 4-digit token is below the minimum reference length:
        # rule 1 must not fire (rule 2 may, on amount+date).
        assert decision.method != "exact_reference"

    def test_rule2_amount_and_date_unique_auto_matches(self) -> None:
        """Amount + date within the window auto-matches at 0.95 when unique."""
        event = _event("e1", 500_000, D(2026, 11, 10))
        tranche = _tranche("t1", 500_000, D(2026, 11, 10))
        deposit = DepositToMatch(
            id="d1", deposit_date=D(2026, 11, 11), amount_irr=500_000
        )
        decision = match_deposit(deposit, [event], [], {"e1": [tranche]})
        assert decision.method == "amount_and_date"
        assert decision.confidence == Decimal("0.95")
        assert decision.settlement_event_id == "e1"
        assert decision.auto_matched is True

    def test_rule2_outside_window_does_not_auto_match(self) -> None:
        """Amount + date outside the ±2-day window does not auto-match."""
        event = _event("e1", 500_000, D(2026, 11, 10))
        tranche = _tranche("t1", 500_000, D(2026, 11, 10))
        deposit = DepositToMatch(
            id="d1", deposit_date=D(2026, 11, 20), amount_irr=500_000
        )
        decision = match_deposit(deposit, [event], [], {"e1": [tranche]})
        assert decision.method != "amount_and_date"

    def test_rule3_amount_only_suggests_without_auto_match(self) -> None:
        """Amount-only candidates become ranked suggestions (≤0.70), never auto-matched."""
        event = _event("e1", 500_000, D(2026, 11, 10))
        tranche = _tranche("t1", 500_000, D(2026, 11, 10))
        deposit = DepositToMatch(
            id="d1", deposit_date=D(2026, 11, 20), amount_irr=500_000
        )
        decision = match_deposit(deposit, [event], [], {"e1": [tranche]})
        assert decision.method == "amount_only"
        assert decision.auto_matched is False
        assert decision.allocations == ()
        assert len(decision.suggestions) >= 1
        assert all(s.confidence <= Decimal("0.70") for s in decision.suggestions)
        assert decision.suggestions[0].target_id == "e1"

    def test_rule4_no_candidate(self) -> None:
        """No candidate at any level is method 'none' at confidence 0."""
        deposit = DepositToMatch(
            id="d1", deposit_date=D(2026, 11, 11), amount_irr=123_456
        )
        decision = match_deposit(deposit, [], [], {})
        assert decision.method == "none"
        assert decision.confidence == Decimal("0")
        assert decision.allocations == ()
        assert decision.suggestions == ()

    def test_reverse_match_deposit_before_file(self) -> None:
        """C7: a deposit arriving before the file attaches to an expected tranche."""
        tranche = _tranche("t1", 500_000, D(2026, 11, 10), state="expected")
        deposit = DepositToMatch(
            id="d1", deposit_date=D(2026, 11, 11), amount_irr=500_000
        )
        decision = match_deposit(deposit, [], [tranche], {})
        assert decision.method == "amount_and_date"
        assert decision.settlement_event_id is None
        assert decision.allocations[0].tranche_id == "t1"
        assert decision.allocations[0].allocated_amount_irr == 500_000

    def test_overpayment_parks_as_unallocated(self) -> None:
        """A deposit above the matched tranches' outstanding parks the remainder."""
        event = _event("e1", 500_000, D(2026, 11, 10), tracking_ref="TRK-111111")
        tranche = _tranche("t1", 500_000, D(2026, 11, 10))
        deposit = DepositToMatch(
            id="d1",
            deposit_date=D(2026, 11, 11),
            amount_irr=600_000,
            description="واریز TRK-111111",
        )
        decision = match_deposit(deposit, [event], [], {"e1": [tranche]})
        assert decision.method == "exact_reference"
        assert decision.allocations[0].allocated_amount_irr == 500_000
        assert decision.overpayment_irr == 100_000

    def test_partial_deposit_is_not_overpayment(self) -> None:
        """A deposit smaller than the outstanding is partial, not an overpayment."""
        event = _event("e1", 500_000, D(2026, 11, 10), tracking_ref="TRK-222222")
        tranche = _tranche("t1", 500_000, D(2026, 11, 10))
        deposit = DepositToMatch(
            id="d1",
            deposit_date=D(2026, 11, 11),
            amount_irr=300_000,
            description="واریز TRK-222222",
        )
        decision = match_deposit(deposit, [event], [], {"e1": [tranche]})
        assert decision.method == "exact_reference"
        assert decision.allocations[0].allocated_amount_irr == 300_000
        assert decision.overpayment_irr == 0
