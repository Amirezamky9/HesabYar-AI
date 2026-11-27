"""Pure derivation + state-machine tests for the settlement engine (US-521).

DB-free: the derivation functions and the §3.3 transition table are
pure. Includes the phased design §3.5 SnappPay worked example as a
regression test (100,000,000 IRR sale, 10% fee → 89,000,000 net in
4 tranches of 22,250,000).
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.integrations.holidays import HolidayCalendar
from hesabyar.domain.integrations.models import (
    ContractStatus,
    GatewayContract,
    TrancheLeg,
)
from hesabyar.domain.settlement.derivation import (
    derive_payment_amounts,
    derive_tranches,
    split_by_legs,
)
from hesabyar.domain.settlement.errors import (
    SettlementDerivationError,
    SettlementInvalidTransitionError,
)
from hesabyar.domain.settlement.models import TrancheState
from hesabyar.domain.settlement.state_machine import (
    ALLOWED_TRANSITIONS,
    escalation_level_for,
    is_terminal,
    require_transition,
)

FOUR_TRANCHES = (
    TrancheLeg(1, 5, Decimal("25")),
    TrancheLeg(2, 5, Decimal("25")),
    TrancheLeg(3, 5, Decimal("25")),
    TrancheLeg(4, 5, Decimal("25")),
)

CALENDAR = HolidayCalendar(weekly_holiday=4, holidays=frozenset())


def _contract(**overrides: object) -> GatewayContract:
    """Build a SnappPay-like 4-tranche contract (fee 10%, VAT 10%)."""
    base: dict[str, object] = {
        "id": "contract-1",
        "channel_id": "channel-1",
        "channel_key": "snapppay",
        "version": 1,
        "valid_from": datetime.date(2026, 10, 23),
        "fee_percent": Decimal("10"),
        "fee_vat_rate": Decimal("10"),
        "fee_timing": "proportional",
        "fee_accrual_policy": "at_sale",
        "settlement_model": "tranches_4",
        "tranche_legs": FOUR_TRANCHES,
        "payout_window_start_day": 1,
        "payout_window_end_day": 5,
        "payout_calendar": "jalali",
        "working_day_adjustment": False,
        "day_lag": None,
        "grace_days_after_due": 3,
        "status": ContractStatus.ACTIVE,
    }
    base.update(overrides)
    return GatewayContract(**base)  # type: ignore[arg-type]


def _jalali(day: datetime.date) -> tuple[int, int, int]:
    fiscal = FiscalDate(day)
    return fiscal.jalali_year, fiscal.jalali_month, fiscal.jalali_day


class TestSnappPayWorkedExample:
    """Phased design §3.5: the 100,000,000 IRR Aban sale, 4 tranches."""

    def test_worked_example_amounts_and_dates(self) -> None:
        # Sale confirmed 15 Aban 1405 (2026-11-06).
        confirmed = datetime.datetime(2026, 11, 6, 12, 0, tzinfo=datetime.UTC)
        schedule = derive_tranches(
            gross_irr=100_000_000,
            contract=_contract(),
            gateway_confirmed_at=confirmed,
            calendar=CALENDAR,
            fee_vat_rate=Decimal("10"),
        )
        assert schedule.fee_irr == 10_000_000
        assert schedule.fee_vat_irr == 1_000_000
        assert schedule.net_irr == 89_000_000
        assert len(schedule.tranches) == 4
        # Due 5 Azar / 5 Dey / 5 Bahman / 5 Esfand, window 1–5 each month.
        assert [_jalali(t.expected_date) for t in schedule.tranches] == [
            (1405, 9, 5),
            (1405, 10, 5),
            (1405, 11, 5),
            (1405, 12, 5),
        ]
        assert [_jalali(t.expected_window_start) for t in schedule.tranches] == [
            (1405, 9, 1),
            (1405, 10, 1),
            (1405, 11, 1),
            (1405, 12, 1),
        ]
        for tranche in schedule.tranches:
            assert tranche.net_irr == 22_250_000
            assert tranche.gross_irr == 25_000_000
            assert tranche.fee_irr == 2_500_000
            assert tranche.fee_vat_irr == 250_000
            assert tranche.sales_period_jalali == "1405/08"
            # Per-tranche identity: net = gross - fee - vat.
            assert tranche.net_irr == tranche.gross_irr - tranche.fee_irr - tranche.fee_vat_irr
        # The receivable from the gateway drains month by month: 89 → 0.
        remaining = schedule.net_irr
        for tranche in schedule.tranches:
            remaining -= tranche.net_irr
        assert remaining == 0


class TestMoneySplit:
    """Fee/VAT/net formulas + the last-tranche-is-remainder rule."""

    def test_derive_payment_amounts_half_up(self) -> None:
        # 10% of 100,000,005 = 10,000,000.5 → half-up 10,000,001.
        fee, vat, net = derive_payment_amounts(100_000_005, Decimal("10"), Decimal("10"))
        assert fee == 10_000_001
        assert vat == 1_000_000  # 10% of 10,000,001 = 1,000,000.1 → 1,000,000
        assert net == 100_000_005 - fee - vat

    def test_split_last_tranche_is_remainder(self) -> None:
        legs = (
            TrancheLeg(1, 5, Decimal("25")),
            TrancheLeg(2, 5, Decimal("25")),
            TrancheLeg(3, 5, Decimal("25")),
            TrancheLeg(4, 5, Decimal("25")),
        )
        shares = split_by_legs(89_000_001, legs)
        assert shares == (22_250_000, 22_250_000, 22_250_000, 22_250_001)
        assert sum(shares) == 89_000_001

    def test_uneven_split_sums_exactly(self) -> None:
        confirmed = datetime.datetime(2026, 11, 6, 12, 0, tzinfo=datetime.UTC)
        schedule = derive_tranches(
            gross_irr=100_000_001,
            contract=_contract(),
            gateway_confirmed_at=confirmed,
            calendar=CALENDAR,
            fee_vat_rate=Decimal("10"),
        )
        assert sum(t.net_irr for t in schedule.tranches) == schedule.net_irr
        assert sum(t.fee_irr for t in schedule.tranches) == schedule.fee_irr
        assert sum(t.fee_vat_irr for t in schedule.tranches) == schedule.fee_vat_irr
        assert sum(t.gross_irr for t in schedule.tranches) == 100_000_001
        # The last tranche absorbs the rounding remainder.
        assert schedule.tranches[-1].net_irr == 22_250_001

    def test_upfront_fee_timing_puts_all_fee_on_first_tranche(self) -> None:
        confirmed = datetime.datetime(2026, 11, 6, 12, 0, tzinfo=datetime.UTC)
        schedule = derive_tranches(
            gross_irr=100_000_000,
            contract=_contract(fee_timing="upfront_first_tranche"),
            gateway_confirmed_at=confirmed,
            calendar=CALENDAR,
            fee_vat_rate=Decimal("10"),
        )
        first = schedule.tranches[0]
        assert first.fee_irr == 10_000_000
        assert first.fee_vat_irr == 1_000_000
        assert first.net_irr == 22_250_000
        assert first.gross_irr == 22_250_000 + 10_000_000 + 1_000_000
        for tranche in schedule.tranches[1:]:
            assert tranche.fee_irr == 0
            assert tranche.fee_vat_irr == 0
            assert tranche.net_irr == 22_250_000
        assert sum(t.net_irr for t in schedule.tranches) == 89_000_000

    def test_zero_fee_contract(self) -> None:
        # The seller credit-line model (SalamPay): 0% fee, 0 VAT.
        confirmed = datetime.datetime(2026, 11, 6, 12, 0, tzinfo=datetime.UTC)
        schedule = derive_tranches(
            gross_irr=50_000_000,
            contract=_contract(
                fee_percent=Decimal("0"),
                tranche_legs=(TrancheLeg(1, 5, Decimal("100")),),
                settlement_model="monthly_lump",
            ),
            gateway_confirmed_at=confirmed,
            calendar=CALENDAR,
            fee_vat_rate=Decimal("10"),
        )
        assert schedule.fee_irr == 0
        assert schedule.fee_vat_irr == 0
        assert schedule.net_irr == 50_000_000
        assert len(schedule.tranches) == 1
        assert schedule.tranches[0].net_irr == 50_000_000


class TestDueDates:
    """Due-date algorithm: Jalali months, clamping, working days, day lag."""

    def test_payout_day_clamps_to_month_length(self) -> None:
        # Sale in Dey 1405; leg due in Bahman (30 days) on day 31 → 30 Bahman.
        confirmed = datetime.datetime(2026, 12, 26, 12, 0, tzinfo=datetime.UTC)
        assert _jalali(confirmed.date()) == (1405, 10, 5)
        schedule = derive_tranches(
            gross_irr=10_000_000,
            contract=_contract(tranche_legs=(TrancheLeg(1, 31, Decimal("100")),)),
            gateway_confirmed_at=confirmed,
            calendar=CALENDAR,
            fee_vat_rate=Decimal("10"),
        )
        assert _jalali(schedule.tranches[0].expected_date) == (1405, 11, 30)

    def test_gregorian_calendar_and_february_clamp(self) -> None:
        confirmed = datetime.datetime(2026, 1, 31, 12, 0, tzinfo=datetime.UTC)
        schedule = derive_tranches(
            gross_irr=10_000_000,
            contract=_contract(
                payout_calendar="gregorian",
                tranche_legs=(TrancheLeg(1, 31, Decimal("100")),),
            ),
            gateway_confirmed_at=confirmed,
            calendar=CALENDAR,
            fee_vat_rate=Decimal("10"),
        )
        assert schedule.tranches[0].expected_date == datetime.date(2026, 2, 28)

    def test_working_day_adjustment_moves_friday_due_to_saturday(self) -> None:
        # 2026-11-06 is a Friday; Gregorian contract due that day moves to Saturday.
        confirmed = datetime.datetime(2026, 10, 6, 12, 0, tzinfo=datetime.UTC)
        assert datetime.date(2026, 11, 6).weekday() == 4  # Friday
        schedule = derive_tranches(
            gross_irr=10_000_000,
            contract=_contract(
                payout_calendar="gregorian",
                working_day_adjustment=True,
                tranche_legs=(TrancheLeg(1, 6, Decimal("100")),),
            ),
            gateway_confirmed_at=confirmed,
            calendar=CALENDAR,
            fee_vat_rate=Decimal("10"),
        )
        assert schedule.tranches[0].expected_date == datetime.date(2026, 11, 7)

    def test_working_day_adjustment_skips_listed_holiday(self) -> None:
        # Due 2026-11-07 (Saturday) is a listed holiday → Sunday 2026-11-08.
        confirmed = datetime.datetime(2026, 10, 7, 12, 0, tzinfo=datetime.UTC)
        calendar = HolidayCalendar(
            weekly_holiday=4, holidays=frozenset({datetime.date(2026, 11, 7)})
        )
        schedule = derive_tranches(
            gross_irr=10_000_000,
            contract=_contract(
                payout_calendar="gregorian",
                working_day_adjustment=True,
                tranche_legs=(TrancheLeg(1, 7, Decimal("100")),),
            ),
            gateway_confirmed_at=confirmed,
            calendar=calendar,
            fee_vat_rate=Decimal("10"),
        )
        assert schedule.tranches[0].expected_date == datetime.date(2026, 11, 8)

    def test_no_adjustment_when_disabled(self) -> None:
        confirmed = datetime.datetime(2026, 10, 6, 12, 0, tzinfo=datetime.UTC)
        schedule = derive_tranches(
            gross_irr=10_000_000,
            contract=_contract(
                payout_calendar="gregorian",
                working_day_adjustment=False,
                tranche_legs=(TrancheLeg(1, 6, Decimal("100")),),
            ),
            gateway_confirmed_at=confirmed,
            calendar=CALENDAR,
            fee_vat_rate=Decimal("10"),
        )
        assert schedule.tranches[0].expected_date == datetime.date(2026, 11, 6)

    def test_day_lag_counts_business_days_from_confirmation(self) -> None:
        # SalamPay: 10 working days from confirmation (Friday 2026-11-06).
        confirmed = datetime.datetime(2026, 11, 6, 12, 0, tzinfo=datetime.UTC)
        schedule = derive_tranches(
            gross_irr=10_000_000,
            contract=_contract(
                settlement_model="working_days",
                tranche_legs=(TrancheLeg(0, 5, Decimal("100")),),
                day_lag=10,
            ),
            gateway_confirmed_at=confirmed,
            calendar=CALENDAR,
            fee_vat_rate=Decimal("10"),
        )
        expected = CALENDAR.add_business_days(datetime.date(2026, 11, 6), 10)
        assert schedule.tranches[0].expected_date == expected
        # A lag has no payout window: window starts on the due date.
        assert schedule.tranches[0].expected_window_start == expected

    def test_day_lag_requires_single_leg_with_zero_offset(self) -> None:
        confirmed = datetime.datetime(2026, 11, 6, 12, 0, tzinfo=datetime.UTC)
        with pytest.raises(SettlementDerivationError, match="exactly one tranche leg"):
            derive_tranches(
                gross_irr=10_000_000,
                contract=_contract(day_lag=10),
                gateway_confirmed_at=confirmed,
                calendar=CALENDAR,
                fee_vat_rate=Decimal("10"),
            )
        with pytest.raises(SettlementDerivationError, match="month_offset=0"):
            derive_tranches(
                gross_irr=10_000_000,
                contract=_contract(
                    tranche_legs=(TrancheLeg(1, 5, Decimal("100")),),
                    day_lag=1,
                ),
                gateway_confirmed_at=confirmed,
                calendar=CALENDAR,
                fee_vat_rate=Decimal("10"),
            )

    def test_invalid_legs_rejected(self) -> None:
        confirmed = datetime.datetime(2026, 11, 6, 12, 0, tzinfo=datetime.UTC)
        with pytest.raises(SettlementDerivationError, match="sum to exactly 100"):
            derive_tranches(
                gross_irr=10_000_000,
                contract=_contract(
                    tranche_legs=(
                        TrancheLeg(1, 5, Decimal("50")),
                        TrancheLeg(2, 5, Decimal("40")),
                    )
                ),
                gateway_confirmed_at=confirmed,
                calendar=CALENDAR,
                fee_vat_rate=Decimal("10"),
            )

    def test_non_positive_gross_rejected(self) -> None:
        confirmed = datetime.datetime(2026, 11, 6, 12, 0, tzinfo=datetime.UTC)
        with pytest.raises(SettlementDerivationError, match="must be positive"):
            derive_tranches(
                gross_irr=0,
                contract=_contract(),
                gateway_confirmed_at=confirmed,
                calendar=CALENDAR,
                fee_vat_rate=Decimal("10"),
            )


class TestStateMachine:
    """The §3.3 transition table."""

    def test_happy_path_transitions_allowed(self) -> None:
        for from_state, to_state in [
            (TrancheState.EXPECTED, TrancheState.DUE),
            (TrancheState.DUE, TrancheState.CONFIRMED),
            (TrancheState.CONFIRMED, TrancheState.RECEIVED),
            (TrancheState.RECEIVED, TrancheState.SETTLED),
        ]:
            require_transition(from_state, to_state)

    def test_reverse_match_due_to_received_allowed(self) -> None:
        require_transition(TrancheState.DUE, TrancheState.RECEIVED)

    def test_terminal_states_have_no_exits(self) -> None:
        for state in (TrancheState.SETTLED, TrancheState.CANCELLED, TrancheState.WRITTEN_OFF):
            assert is_terminal(state)
            assert ALLOWED_TRANSITIONS[state] == frozenset()
            with pytest.raises(SettlementInvalidTransitionError):
                require_transition(state, TrancheState.DUE)

    def test_forbidden_transitions_raise(self) -> None:
        for from_state, to_state in [
            (TrancheState.EXPECTED, TrancheState.SETTLED),
            (TrancheState.EXPECTED, TrancheState.RECEIVED),
            (TrancheState.CONFIRMED, TrancheState.CANCELLED),
            (TrancheState.RECEIVED, TrancheState.CANCELLED),
            (TrancheState.OVERDUE, TrancheState.DUE),
            (TrancheState.SETTLED, TrancheState.EXCEPTION),
        ]:
            with pytest.raises(SettlementInvalidTransitionError):
                require_transition(from_state, to_state)

    def test_exception_can_return_to_operational_states(self) -> None:
        for to_state in (
            TrancheState.DUE,
            TrancheState.OVERDUE,
            TrancheState.CONFIRMED,
            TrancheState.RECEIVED,
            TrancheState.CANCELLED,
            TrancheState.WRITTEN_OFF,
        ):
            require_transition(TrancheState.EXCEPTION, to_state)

    def test_escalation_levels(self) -> None:
        assert escalation_level_for(0) == 0
        assert escalation_level_for(6) == 0
        assert escalation_level_for(7) == 1
        assert escalation_level_for(29) == 1
        assert escalation_level_for(30) == 2
        assert escalation_level_for(59) == 2
        assert escalation_level_for(60) == 3
        assert escalation_level_for(120) == 3
