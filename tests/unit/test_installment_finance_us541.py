"""Unit tests for the installment finance math (US-541, Phase 5-5 ۵ج).

Pure domain functions in ``hesabyar.domain.installments.finance``:
the principal / finance-income split, gradual recognition on
collection, the late fee, stepped dunning, the payoff quote,
re-amortisation, and the effective-version selection. No DB, no
engine — "today" is pinned explicitly in every test.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.domain.installments.errors import (
    InstallmentLinkageError,
    InstallmentPaymentMethodError,
    InstallmentValidationError,
)
from hesabyar.domain.installments.finance import (
    assess_leg,
    compute_late_fee,
    compute_payoff_quote,
    days_overdue,
    dunning_stage,
    held_advance,
    leg_finance_income,
    next_reminder_date,
    reamortise_balances,
    recognised_income,
    select_effective_contract,
)
from hesabyar.domain.installments.models import (
    EarlySettlementFeePolicy,
    EarlySettlementIncomePolicy,
    InstallmentSaleContract,
    LateFeeBasis,
    LegStatus,
)
from hesabyar.domain.installments.state_machine import LegView
from hesabyar.domain.installments.validation import (
    validate_contract_payload,
    validate_settlement_payload,
)

TODAY = datetime.date(2026, 11, 20)
PAST = datetime.date(2026, 10, 21)  # 30 days before TODAY
FUTURE = datetime.date(2026, 12, 20)  # 30 days after TODAY


def _leg_view(
    leg_id: str = "leg-1",
    sequence: int = 1,
    due: datetime.date = PAST,
    principal: int = 10_000_000,
    received: int = 0,
) -> LegView:
    return LegView(
        leg_id=leg_id,
        sequence=sequence,
        due_date=FiscalDate.from_gregorian(due),
        principal=MoneyIRR(principal),
        received=MoneyIRR(received),
    )


def _contract(**overrides: object) -> InstallmentSaleContract:
    fields: dict[str, object] = {
        "id": "contract-1",
        "tenant_id": "tenant-1",
        "installation_id": "installation-1",
        "company_engine_id": 541,
        "code": "STD-541",
        "name": "قرارداد تست",
        "version": 1,
        "effective_from": FiscalDate.from_gregorian(datetime.date(2026, 1, 1)),
    }
    fields.update(overrides)
    return InstallmentSaleContract(**fields)  # type: ignore[arg-type]


# -- finance income: split + recognition ---------------------------------------


def test_leg_income_is_principal_times_rate() -> None:
    assert leg_finance_income(MoneyIRR(10_000_000), Decimal("0.10")).amount == 1_000_000
    assert leg_finance_income(MoneyIRR(10_000_000), Decimal("0")).amount == 0
    assert leg_finance_income(MoneyIRR(0), Decimal("0.10")).amount == 0


def test_leg_income_rounds_half_up_to_whole_rial() -> None:
    # 105 Rial × 10% = 10.5 Rial → 11 (half-up).
    assert leg_finance_income(MoneyIRR(105), Decimal("0.10")).amount == 11


def test_recognised_income_is_pro_rata_on_the_collected_portion() -> None:
    assert recognised_income(MoneyIRR(4_000_000), Decimal("0.10")).amount == 400_000
    assert recognised_income(MoneyIRR(0), Decimal("0.10")).amount == 0
    assert recognised_income(MoneyIRR(4_000_000), Decimal("0")).amount == 0


def test_full_collection_recognises_exactly_the_leg_income() -> None:
    # The recognition hook and the split are the same multiplication,
    # so a fully collected leg recognises its whole income — never
    # a Rial more or less.
    principal = MoneyIRR(10_000_105)
    rate = Decimal("0.10")
    assert recognised_income(principal, rate) == leg_finance_income(principal, rate)


# -- late fee -------------------------------------------------------------------


def test_late_fee_daily_percent_uses_chargeable_days() -> None:
    # 10 days overdue, 3 grace days → 7 chargeable days at 0.1%/day.
    fee = compute_late_fee(
        MoneyIRR(10_000_000),
        basis=LateFeeBasis.DAILY_PERCENT,
        rate=Decimal("0.001"),
        grace_days=3,
        days_past_due=10,
    )
    assert fee.amount == 70_000


def test_late_fee_monthly_percent_counts_started_months() -> None:
    # 30 chargeable days = 1 started month; 31 = 2.
    base = {"basis": LateFeeBasis.MONTHLY_PERCENT, "rate": Decimal("0.02"), "grace_days": 0}
    assert (
        compute_late_fee(MoneyIRR(10_000_000), days_past_due=30, **base).amount == 200_000
    )
    assert (
        compute_late_fee(MoneyIRR(10_000_000), days_past_due=31, **base).amount == 400_000
    )
    assert (
        compute_late_fee(MoneyIRR(10_000_000), days_past_due=35, **base).amount == 400_000
    )


def test_late_fee_fixed_per_day() -> None:
    fee = compute_late_fee(
        MoneyIRR(10_000_000),
        basis=LateFeeBasis.FIXED_PER_DAY,
        rate=Decimal("5000"),
        grace_days=0,
        days_past_due=7,
    )
    assert fee.amount == 35_000


def test_late_fee_is_zero_when_nothing_is_chargeable() -> None:
    kwargs = {
        "basis": LateFeeBasis.DAILY_PERCENT,
        "rate": Decimal("0.001"),
        "grace_days": 0,
        "days_past_due": 10,
    }
    assert compute_late_fee(MoneyIRR(0), **kwargs).amount == 0
    assert (
        compute_late_fee(
            MoneyIRR(10_000_000), basis=LateFeeBasis.NONE, rate=Decimal("0"),
            grace_days=0, days_past_due=10,
        ).amount
        == 0
    )
    assert (
        compute_late_fee(
            MoneyIRR(10_000_000), basis=LateFeeBasis.DAILY_PERCENT,
            rate=Decimal("0.001"), grace_days=3, days_past_due=2,
        ).amount
        == 0
    )
    assert (
        compute_late_fee(
            MoneyIRR(10_000_000), basis=LateFeeBasis.DAILY_PERCENT,
            rate=Decimal("0.001"), grace_days=0, days_past_due=0,
        ).amount
        == 0
    )


def test_late_fee_base_is_the_unpaid_balance_not_the_leg_amount() -> None:
    # A half-paid leg accrues its fee on the remaining half only.
    fee = compute_late_fee(
        MoneyIRR(5_000_000),
        basis=LateFeeBasis.DAILY_PERCENT,
        rate=Decimal("0.001"),
        grace_days=0,
        days_past_due=10,
    )
    assert fee.amount == 50_000


def test_days_overdue_counts_whole_days_past_the_due_date() -> None:
    due = FiscalDate.from_gregorian(PAST)
    assert days_overdue(due, TODAY) == 30
    assert days_overdue(due, PAST) == 0
    assert days_overdue(FiscalDate.from_gregorian(FUTURE), TODAY) == 0


# -- stepped dunning --------------------------------------------------------------


def test_dunning_stage_counts_reached_offsets() -> None:
    offsets = (3, 7, 14, 30)
    assert dunning_stage(0, offsets) == 0
    assert dunning_stage(2, offsets) == 0
    assert dunning_stage(3, offsets) == 1
    assert dunning_stage(6, offsets) == 1
    assert dunning_stage(7, offsets) == 2
    assert dunning_stage(13, offsets) == 2
    assert dunning_stage(14, offsets) == 3
    assert dunning_stage(29, offsets) == 3
    assert dunning_stage(30, offsets) == 4
    assert dunning_stage(365, offsets) == 4
    assert dunning_stage(365, ()) == 0


def test_next_reminder_date_is_the_next_unreached_offset() -> None:
    due = FiscalDate.from_gregorian(PAST)
    offsets = (3, 7, 14)
    assert next_reminder_date(due, offsets, 0) == datetime.date(2026, 10, 24)
    assert next_reminder_date(due, offsets, 5) == datetime.date(2026, 10, 28)
    assert next_reminder_date(due, offsets, 14) is None
    assert next_reminder_date(due, (), 100) is None


# -- effective-version selection ----------------------------------------------------


def test_select_effective_contract_picks_the_latest_in_force_version() -> None:
    v1 = _contract(id="v1", version=1, effective_from=FiscalDate.from_gregorian(datetime.date(2026, 1, 1)))
    v2 = _contract(id="v2", version=2, effective_from=FiscalDate.from_gregorian(datetime.date(2026, 6, 1)))
    v3 = _contract(id="v3", version=3, effective_from=FiscalDate.from_gregorian(datetime.date(2027, 1, 1)))
    assert select_effective_contract([v1, v2, v3], TODAY) is v2
    assert select_effective_contract([v1, v2, v3], datetime.date(2026, 3, 1)) is v1
    assert select_effective_contract([v1, v2, v3], datetime.date(2025, 12, 31)) is None


def test_select_effective_contract_ignores_inactive_versions() -> None:
    v1 = _contract(id="v1", version=1, effective_from=FiscalDate.from_gregorian(datetime.date(2026, 1, 1)))
    v2 = _contract(
        id="v2",
        version=2,
        effective_from=FiscalDate.from_gregorian(datetime.date(2026, 6, 1)),
        is_active=False,
    )
    assert select_effective_contract([v1, v2], TODAY) is v1
    assert select_effective_contract([v2], TODAY) is None


def test_select_effective_contract_breaks_effective_date_ties_by_version() -> None:
    same_day = FiscalDate.from_gregorian(datetime.date(2026, 6, 1))
    v2 = _contract(id="v2", version=2, effective_from=same_day)
    v3 = _contract(id="v3", version=3, effective_from=same_day)
    assert select_effective_contract([v2, v3], TODAY) is v3


# -- per-leg assessment ---------------------------------------------------------------


def test_assess_leg_combines_split_recognition_fee_and_stage() -> None:
    contract = _contract(
        finance_income_rate=Decimal("0.10"),
        late_fee_basis=LateFeeBasis.DAILY_PERCENT,
        late_fee_rate=Decimal("0.001"),
        late_fee_grace_days=0,
        dunning_offsets=(3, 7, 14),
    )
    view = _leg_view(received=4_000_000)
    assessment = assess_leg(view, contract=contract, today=TODAY)
    assert assessment.status is LegStatus.OVERDUE
    assert assessment.days_past_due == 30
    assert assessment.finance_income.amount == 1_000_000
    assert assessment.income_recognised.amount == 400_000
    assert assessment.income_unrealised.amount == 600_000
    # Fee on the 6,000,000 balance: 6,000,000 × 0.001 × 30 days.
    assert assessment.late_fee_accrued.amount == 180_000
    assert assessment.dunning_stage == 3
    assert assessment.next_reminder is None


def test_assess_leg_without_a_contract_has_no_finance_terms() -> None:
    assessment = assess_leg(_leg_view(), contract=None, today=TODAY)
    assert assessment.status is LegStatus.OVERDUE
    assert assessment.finance_income.amount == 0
    assert assessment.income_recognised.amount == 0
    assert assessment.late_fee_accrued.amount == 0
    assert assessment.dunning_stage == 0
    assert assessment.next_reminder is None


# -- payoff quote -----------------------------------------------------------------------


def _three_leg_views(received_first: int = 0) -> list[LegView]:
    return [
        _leg_view("leg-1", 1, PAST, 10_000_000, received_first),
        _leg_view("leg-2", 2, TODAY, 10_000_000, 0),
        _leg_view("leg-3", 3, FUTURE, 10_000_000, 0),
    ]


def _full_contract(**overrides: object) -> InstallmentSaleContract:
    fields: dict[str, object] = {
        "finance_income_rate": Decimal("0.10"),
        "late_fee_basis": LateFeeBasis.DAILY_PERCENT,
        "late_fee_rate": Decimal("0.001"),
        "late_fee_grace_days": 0,
        "early_settlement_fee_policy": EarlySettlementFeePolicy.KEPT,
        "early_settlement_income_policy": EarlySettlementIncomePolicy.FULL,
    }
    fields.update(overrides)
    return _contract(**fields)


def test_payoff_quote_full_policy_charges_everything() -> None:
    quote = compute_payoff_quote(
        "plan-1",
        _three_leg_views(),
        contract=_full_contract(),
        advance_held=MoneyIRR.zero(),
        income_paid=MoneyIRR.zero(),
        today=TODAY,
    )
    assert quote.remaining_principal.amount == 30_000_000
    assert quote.income_realised.amount == 0
    assert quote.income_unrealised.amount == 3_000_000
    assert quote.income_due.amount == 3_000_000
    # Only leg 1 is overdue: 10,000,000 × 0.001 × 30 days.
    assert quote.late_fees_accrued.amount == 300_000
    assert quote.late_fees_due.amount == 300_000
    assert quote.payoff_total.amount == 33_300_000
    assert quote.open_leg_count == 3


def test_payoff_quote_charges_realised_but_unpaid_income_in_full() -> None:
    # Leg 1 collected in full: its 1,000,000 income is realised but
    # unpaid (ordinary receipts pay principal only) — the payoff
    # charges it whatever the policy says about unrealised income.
    quote = compute_payoff_quote(
        "plan-1",
        _three_leg_views(received_first=10_000_000),
        contract=_full_contract(
            early_settlement_income_policy=EarlySettlementIncomePolicy.WAIVED
        ),
        advance_held=MoneyIRR.zero(),
        income_paid=MoneyIRR.zero(),
        today=TODAY,
    )
    assert quote.remaining_principal.amount == 20_000_000
    assert quote.income_realised.amount == 1_000_000
    assert quote.income_realised_unpaid.amount == 1_000_000
    assert quote.income_unrealised.amount == 2_000_000
    # Waived unrealised, but the realised 1,000,000 is still due.
    assert quote.income_due.amount == 1_000_000
    assert quote.late_fees_due.amount == 0  # leg 1 is paid; no fee accrues
    assert quote.payoff_total.amount == 21_000_000


def test_payoff_quote_discounted_policy_discounts_only_unrealised_income() -> None:
    quote = compute_payoff_quote(
        "plan-1",
        _three_leg_views(received_first=10_000_000),
        contract=_full_contract(
            early_settlement_income_policy=EarlySettlementIncomePolicy.DISCOUNTED,
            early_settlement_discount_rate=Decimal("0.20"),
        ),
        advance_held=MoneyIRR.zero(),
        income_paid=MoneyIRR.zero(),
        today=TODAY,
    )
    # Unrealised 2,000,000 × 0.8 = 1,600,000, plus the realised-unpaid 1,000,000.
    assert quote.income_due.amount == 2_600_000
    assert quote.payoff_total.amount == 22_600_000


def test_payoff_quote_waived_fees_and_paid_income_reduce_the_payoff() -> None:
    quote = compute_payoff_quote(
        "plan-1",
        _three_leg_views(received_first=10_000_000),
        contract=_full_contract(
            early_settlement_fee_policy=EarlySettlementFeePolicy.WAIVED,
            early_settlement_income_policy=EarlySettlementIncomePolicy.FULL,
        ),
        advance_held=MoneyIRR.zero(),
        income_paid=MoneyIRR(1_000_000),
        today=TODAY,
    )
    # The realised 1,000,000 was already paid (an earlier settlement
    # receipt carried it), so only the unrealised 2,000,000 is due.
    assert quote.income_realised_unpaid.amount == 0
    assert quote.income_due.amount == 2_000_000
    assert quote.payoff_total.amount == 22_000_000


def test_payoff_quote_nets_the_held_advance_and_floors_at_zero() -> None:
    quote = compute_payoff_quote(
        "plan-1",
        _three_leg_views(),
        contract=_full_contract(),
        advance_held=MoneyIRR(5_000_000),
        income_paid=MoneyIRR.zero(),
        today=TODAY,
    )
    assert quote.payoff_total.amount == 28_300_000
    over_advanced = compute_payoff_quote(
        "plan-1",
        _three_leg_views(),
        contract=None,
        advance_held=MoneyIRR(99_000_000),
        income_paid=MoneyIRR.zero(),
        today=TODAY,
    )
    assert over_advanced.payoff_total.amount == 0


def test_payoff_quote_without_a_contract_is_principal_only() -> None:
    quote = compute_payoff_quote(
        "plan-1",
        _three_leg_views(),
        contract=None,
        advance_held=MoneyIRR.zero(),
        income_paid=MoneyIRR.zero(),
        today=TODAY,
    )
    assert quote.remaining_principal.amount == 30_000_000
    assert quote.income_due.amount == 0
    assert quote.late_fees_due.amount == 0
    assert quote.payoff_total.amount == 30_000_000
    assert quote.contract_id is None


# -- held advance + re-amortisation -------------------------------------------------------


def test_held_advance_subtracts_what_settlements_applied() -> None:
    assert held_advance(MoneyIRR(5_000_000), MoneyIRR(2_000_000)).amount == 3_000_000
    assert held_advance(MoneyIRR(5_000_000), MoneyIRR(5_000_000)).amount == 0
    assert held_advance(MoneyIRR(0), MoneyIRR(0)).amount == 0


def test_reamortise_splits_equally_with_the_remainder_on_the_last_leg() -> None:
    views = [
        _leg_view("leg-1", 1, FUTURE, 10_000_000, 0),
        _leg_view("leg-2", 2, FUTURE, 10_000_000, 0),
        _leg_view("leg-3", 3, FUTURE, 10_000_000, 0),
    ]
    result = reamortise_balances(views, MoneyIRR(25_000_000))
    assert result["leg-1"].amount == 8_333_333
    assert result["leg-2"].amount == 8_333_333
    assert result["leg-3"].amount == 8_333_334
    assert sum(v.amount for v in result.values()) == 25_000_000


def test_reamortise_keeps_received_amounts_and_never_shrinks_below_them() -> None:
    views = [
        _leg_view("leg-1", 1, FUTURE, 10_000_000, 2_000_000),
        _leg_view("leg-2", 2, FUTURE, 10_000_000, 0),
    ]
    result = reamortise_balances(views, MoneyIRR(10_000_000))
    # New balance 5,000,000 each; leg 1's new principal is 2,000,000 received + 5,000,000.
    assert result["leg-1"].amount == 7_000_000
    assert result["leg-2"].amount == 5_000_000


def test_reamortise_to_zero_closes_every_open_leg() -> None:
    views = [
        _leg_view("leg-1", 1, FUTURE, 10_000_000, 2_000_000),
        _leg_view("leg-2", 2, FUTURE, 10_000_000, 0),
    ]
    result = reamortise_balances(views, MoneyIRR(0))
    assert result["leg-1"].amount == 2_000_000
    assert result["leg-2"].amount == 0


def test_reamortise_empty_schedule_is_empty() -> None:
    assert reamortise_balances([], MoneyIRR(10_000_000)) == {}


# -- contract model invariants ------------------------------------------------------------


def test_contract_rejects_a_discount_rate_without_the_discounted_policy() -> None:
    with pytest.raises(ValueError, match="discount"):
        _contract(early_settlement_discount_rate=Decimal("0.20"))


def test_contract_requires_a_discount_rate_for_the_discounted_policy() -> None:
    with pytest.raises(ValueError, match="discount"):
        _contract(
            early_settlement_income_policy=EarlySettlementIncomePolicy.DISCOUNTED,
            early_settlement_discount_rate=Decimal("0"),
        )
    with pytest.raises(ValueError, match="discount"):
        _contract(
            early_settlement_income_policy=EarlySettlementIncomePolicy.DISCOUNTED,
            early_settlement_discount_rate=Decimal("1"),
        )


def test_contract_rejects_non_increasing_dunning_offsets() -> None:
    with pytest.raises(ValueError, match="dunning_offsets"):
        _contract(dunning_offsets=(7, 3))
    with pytest.raises(ValueError, match="dunning_offsets"):
        _contract(dunning_offsets=(3, 3))
    with pytest.raises(ValueError, match="dunning_offsets"):
        _contract(dunning_offsets=(0, 7))


def test_contract_rejects_a_negative_finance_income_rate() -> None:
    with pytest.raises(ValueError, match="finance_income_rate"):
        _contract(finance_income_rate=Decimal("-0.01"))


# -- contract + settlement payload validation -----------------------------------------------


def _contract_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "code": "STD-541",
        "name": "قرارداد تست",
        "version": 1,
        "effective_from": "1405-08-01",
    }
    payload.update(overrides)
    return payload


def test_contract_payload_accepts_the_us541_finance_terms() -> None:
    validated = validate_contract_payload(
        _contract_payload(
            finance_income_rate="0.10",
            dunning_offsets=[3, 7, 14, 30],
            receivable_account_engine_id=55,
            receivable_account_code="1.1.3",
            early_settlement_income_policy="discounted",
            early_settlement_discount_rate="0.20",
        )
    )
    assert validated.finance_income_rate == Decimal("0.10")
    assert validated.dunning_offsets == (3, 7, 14, 30)
    assert validated.receivable_account_engine_id == 55
    assert validated.early_settlement_discount_rate == Decimal("0.20")


def test_contract_payload_rejects_bad_dunning_offsets() -> None:
    with pytest.raises(InstallmentValidationError) as exc_info:
        validate_contract_payload(_contract_payload(dunning_offsets=[7, 3]))
    assert "dunning_offsets" in exc_info.value.field_errors
    with pytest.raises(InstallmentValidationError) as exc_info:
        validate_contract_payload(_contract_payload(dunning_offsets=[0]))
    assert "dunning_offsets[0]" in exc_info.value.field_errors
    with pytest.raises(InstallmentValidationError) as exc_info:
        validate_contract_payload(_contract_payload(dunning_offsets=list(range(1, 12))))
    assert "dunning_offsets" in exc_info.value.field_errors


def test_contract_payload_rejects_a_float_finance_rate() -> None:
    with pytest.raises(InstallmentValidationError) as exc_info:
        validate_contract_payload(_contract_payload(finance_income_rate=0.1))
    assert "finance_income_rate" in exc_info.value.field_errors


def test_contract_payload_requires_a_discount_rate_for_discounted() -> None:
    with pytest.raises(InstallmentValidationError) as exc_info:
        validate_contract_payload(
            _contract_payload(early_settlement_income_policy="discounted")
        )
    assert "early_settlement_discount_rate" in exc_info.value.field_errors
    with pytest.raises(InstallmentValidationError) as exc_info:
        validate_contract_payload(
            _contract_payload(early_settlement_discount_rate="0.20")
        )
    assert "early_settlement_discount_rate" in exc_info.value.field_errors


def _settlement_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "invoice_id": "INV-541-1",
        "journal_move_id": 42,
        "amount_toman": 1_000_000,
        "settlement_type": "full",
        "settlement_date": "1405-08-29",
    }
    payload.update(overrides)
    return payload


def test_settlement_payload_enforces_the_linkage_guard_first() -> None:
    with pytest.raises(InstallmentLinkageError) as exc_info:
        validate_settlement_payload(_settlement_payload(invoice_id="", journal_move_id=0))
    assert exc_info.value.missing == ("invoice_id", "journal_move_id")


def test_settlement_payload_rejects_an_unknown_type() -> None:
    with pytest.raises(InstallmentValidationError) as exc_info:
        validate_settlement_payload(_settlement_payload(settlement_type="half"))
    assert "settlement_type" in exc_info.value.field_errors


def test_settlement_payload_refuses_cheque() -> None:
    # A cheque settlement cannot exist: a settlement receipt
    # allocates across ALL open legs, while a cheque receipt is
    # single-leg, and settlement recognises income + fees at
    # settlement time while cheque money is collected only on clear.
    with pytest.raises(InstallmentPaymentMethodError):
        validate_settlement_payload(_settlement_payload(payment_method="cheque"))
