"""Unit tests for the installment domain (US-540 + US-542, Phase 5-5 ۵ج).

Pure-domain tests — no DB, no engine, no clock: every status test
pins ``today`` explicitly, so the suite is green on any date.

Covers: the leg state machine (expected → due → partially_paid →
paid + overdue + pending_clearance), the derived plan status, the
receipt allocation rule (oldest overdue first, then earliest due;
overpayment becomes an advance, never prepays a future leg or
overpays a leg; a cheque receipt pays ONE leg), the receipt split
(cheque allocations follow their cheque's state), the schedule
generation (equal split, remainder on the last leg), the payload
validation, and THE LINKAGE GUARD (a receipt without both refs is
rejected with a typed error).
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from hesabyar.domain.cheques.models import ChequeState
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.domain.installments.errors import (
    InstallmentLinkageError,
    InstallmentValidationError,
)
from hesabyar.domain.installments.models import (
    InstallmentReceipt,
    InstallmentSaleContract,
    LegAllocation,
    LegStatus,
    PaymentMethod,
    PlanStatus,
)
from hesabyar.domain.installments.state_machine import (
    LegView,
    allocate_receipt,
    leg_status,
    leg_status_for,
    plan_status,
    split_allocations,
)
from hesabyar.domain.installments.validation import (
    generate_schedule,
    validate_contract_payload,
    validate_plan_payload,
    validate_receipt_payload,
)

TODAY = datetime.date(2026, 11, 20)
YESTERDAY = datetime.date(2026, 11, 19)
TOMORROW = datetime.date(2026, 11, 21)
LAST_MONTH = datetime.date(2026, 10, 21)
NEXT_MONTH = datetime.date(2026, 12, 20)


def _fd(day: datetime.date) -> FiscalDate:
    return FiscalDate.from_gregorian(day)


def _view(
    leg_id: str = "leg-1",
    *,
    sequence: int = 1,
    due: datetime.date = NEXT_MONTH,
    principal: int = 10_000_000,
    received: int = 0,
    pending_clearance: int = 0,
) -> LegView:
    return LegView(
        leg_id=leg_id,
        sequence=sequence,
        due_date=_fd(due),
        principal=MoneyIRR(principal),
        received=MoneyIRR(received),
        pending_clearance=MoneyIRR(pending_clearance),
    )


# -- leg state machine -------------------------------------------------------------


def test_leg_expected_before_due_date_with_nothing_received() -> None:
    assert (
        leg_status(_fd(NEXT_MONTH), MoneyIRR(10_000_000), MoneyIRR(0), TODAY)
        is LegStatus.EXPECTED
    )


def test_leg_due_on_its_due_date_with_nothing_received() -> None:
    assert (
        leg_status(_fd(TODAY), MoneyIRR(10_000_000), MoneyIRR(0), TODAY) is LegStatus.DUE
    )


def test_leg_overdue_after_due_date_with_nothing_received() -> None:
    assert (
        leg_status(_fd(YESTERDAY), MoneyIRR(10_000_000), MoneyIRR(0), TODAY)
        is LegStatus.OVERDUE
    )


def test_leg_partially_paid_before_due_date() -> None:
    assert (
        leg_status(_fd(NEXT_MONTH), MoneyIRR(10_000_000), MoneyIRR(4_000_000), TODAY)
        is LegStatus.PARTIALLY_PAID
    )


def test_leg_partially_paid_on_due_date_stays_partially_paid() -> None:
    assert (
        leg_status(_fd(TODAY), MoneyIRR(10_000_000), MoneyIRR(4_000_000), TODAY)
        is LegStatus.PARTIALLY_PAID
    )


def test_leg_partially_paid_then_past_due_is_overdue_not_partially_paid() -> None:
    # Overdue wins over partially_paid: a leg that was partly paid
    # and then passed its due date is overdue.
    assert (
        leg_status(_fd(YESTERDAY), MoneyIRR(10_000_000), MoneyIRR(4_000_000), TODAY)
        is LegStatus.OVERDUE
    )


def test_leg_paid_when_received_equals_principal() -> None:
    assert (
        leg_status(_fd(LAST_MONTH), MoneyIRR(10_000_000), MoneyIRR(10_000_000), TODAY)
        is LegStatus.PAID
    )


def test_leg_paid_even_when_past_due() -> None:
    # A fully paid leg is paid, never overdue.
    assert (
        leg_status(_fd(LAST_MONTH), MoneyIRR(10_000_000), MoneyIRR(10_000_000), TODAY)
        is LegStatus.PAID
    )


def test_leg_status_never_stored_just_recomputed() -> None:
    # The same leg, read on two days, reports two statuses — the
    # status is a function of the wall-clock date, not a stored field.
    due = _fd(TOMORROW)
    principal = MoneyIRR(10_000_000)
    assert leg_status(due, principal, MoneyIRR(0), TODAY) is LegStatus.EXPECTED
    assert leg_status(due, principal, MoneyIRR(0), datetime.date(2026, 11, 22)) is LegStatus.OVERDUE


# -- pending_clearance (US-542) -------------------------------------------------------


def test_leg_pending_clearance_when_fully_covered_by_cheque_in_flight() -> None:
    assert (
        leg_status(
            _fd(NEXT_MONTH),
            MoneyIRR(10_000_000),
            MoneyIRR(0),
            TODAY,
            pending_clearance=MoneyIRR(10_000_000),
        )
        is LegStatus.PENDING_CLEARANCE
    )


def test_leg_pending_clearance_wins_over_overdue() -> None:
    # The pending-clearance period is NOT counted as overdue (§5
    # §2.1): a leg past its due date, fully covered by a cheque in
    # flight, is pending_clearance — and no overdue dunning is sent.
    assert (
        leg_status(
            _fd(YESTERDAY),
            MoneyIRR(10_000_000),
            MoneyIRR(0),
            TODAY,
            pending_clearance=MoneyIRR(10_000_000),
        )
        is LegStatus.PENDING_CLEARANCE
    )


def test_leg_pending_clearance_with_partial_cash_plus_pending_cheque() -> None:
    assert (
        leg_status(
            _fd(YESTERDAY),
            MoneyIRR(10_000_000),
            MoneyIRR(4_000_000),
            TODAY,
            pending_clearance=MoneyIRR(6_000_000),
        )
        is LegStatus.PENDING_CLEARANCE
    )


def test_leg_paid_wins_over_pending_clearance() -> None:
    # Received alone covers the principal — the leg is paid even
    # with a pending cheque alongside (e.g. an over-covering cheque).
    assert (
        leg_status(
            _fd(YESTERDAY),
            MoneyIRR(10_000_000),
            MoneyIRR(10_000_000),
            TODAY,
            pending_clearance=MoneyIRR(5_000_000),
        )
        is LegStatus.PAID
    )


def test_leg_partly_pending_is_not_partially_paid() -> None:
    # Pending money is NOT received: a leg with nothing received
    # and a cheque covering half of it is still due (its pending
    # amount is reported alongside, not as received).
    assert (
        leg_status(
            _fd(TODAY),
            MoneyIRR(10_000_000),
            MoneyIRR(0),
            TODAY,
            pending_clearance=MoneyIRR(5_000_000),
        )
        is LegStatus.DUE
    )
    assert (
        leg_status(
            _fd(YESTERDAY),
            MoneyIRR(10_000_000),
            MoneyIRR(0),
            TODAY,
            pending_clearance=MoneyIRR(5_000_000),
        )
        is LegStatus.OVERDUE
    )


def test_leg_status_for_reads_pending_from_the_view() -> None:
    view = _view(due=YESTERDAY, pending_clearance=10_000_000)
    assert leg_status_for(view, TODAY) is LegStatus.PENDING_CLEARANCE
    assert view.balance.amount == 10_000_000  # pending does NOT reduce the balance
    assert view.covered.amount == 10_000_000


def test_plan_active_when_only_pending_clearance_legs_unpaid() -> None:
    legs = [
        _view("l1", due=LAST_MONTH, received=10_000_000),
        _view("l2", sequence=2, due=LAST_MONTH, pending_clearance=10_000_000),
    ]
    assert plan_status(legs, TODAY) is PlanStatus.ACTIVE


# -- the receipt split (US-542) -------------------------------------------------------


def _receipt(
    receipt_id: str,
    *,
    method: PaymentMethod = PaymentMethod.CASH,
    cheque_id: str | None = None,
    allocations: tuple[LegAllocation, ...] = (LegAllocation("leg-1", MoneyIRR(10_000_000)),),
) -> InstallmentReceipt:
    return InstallmentReceipt(
        id=receipt_id,
        plan_id="plan-1",
        tenant_id="t",
        installation_id="i",
        invoice_id="INV-1",
        journal_move_id=42,
        amount=MoneyIRR(10_000_000),
        received_date=_fd(TODAY),
        payment_method=method,
        allocations=allocations,
        cheque_id=cheque_id,
    )


def test_split_counts_cash_receipts_as_received() -> None:
    received, pending = split_allocations([_receipt("r1")], {})
    assert received == {"leg-1": 10_000_000}
    assert pending == {}


def test_split_counts_cleared_cheque_receipt_as_received() -> None:
    receipt = _receipt("r1", method=PaymentMethod.CHEQUE, cheque_id="chq-1")
    received, pending = split_allocations([receipt], {"chq-1": ChequeState.CLEARED})
    assert received == {"leg-1": 10_000_000}
    assert pending == {}


def test_split_counts_in_flight_cheque_receipt_as_pending() -> None:
    for state in (
        ChequeState.REGISTERED,
        ChequeState.HELD,
        ChequeState.IN_CLEARING,
    ):
        receipt = _receipt("r1", method=PaymentMethod.CHEQUE, cheque_id="chq-1")
        received, pending = split_allocations([receipt], {"chq-1": state})
        assert received == {}, state
        assert pending == {"leg-1": 10_000_000}, state


def test_split_voids_bounced_and_endorsed_cheque_receipts() -> None:
    for state in (
        ChequeState.BOUNCED,
        ChequeState.RETURNED,
        ChequeState.ENDORSED,
        ChequeState.VOIDED,
        ChequeState.EXPIRED_UNRECEIVED,
        ChequeState.REJECTED_MISMATCH,
    ):
        receipt = _receipt("r1", method=PaymentMethod.CHEQUE, cheque_id="chq-1")
        received, pending = split_allocations([receipt], {"chq-1": state})
        assert received == {}, state
        assert pending == {}, state


def test_split_treats_unknown_cheque_as_pending_not_received() -> None:
    # Fail-closed: a cheque receipt whose cheque cannot be resolved
    # is pending (never received, never silently dropped).
    receipt = _receipt("r1", method=PaymentMethod.CHEQUE, cheque_id="chq-1")
    received, pending = split_allocations([receipt], {})
    assert received == {}
    assert pending == {"leg-1": 10_000_000}


def test_split_mixes_cash_and_cheque_receipts_per_leg() -> None:
    receipts = [
        _receipt(
            "r1",
            allocations=(LegAllocation("leg-1", MoneyIRR(4_000_000)),),
        ),
        _receipt(
            "r2",
            method=PaymentMethod.CHEQUE,
            cheque_id="chq-1",
            allocations=(LegAllocation("leg-1", MoneyIRR(6_000_000)),),
        ),
        _receipt(
            "r3",
            method=PaymentMethod.CHEQUE,
            cheque_id="chq-2",
            allocations=(LegAllocation("leg-2", MoneyIRR(10_000_000)),),
        ),
    ]
    received, pending = split_allocations(
        receipts, {"chq-1": ChequeState.HELD, "chq-2": ChequeState.CLEARED}
    )
    assert received == {"leg-1": 4_000_000, "leg-2": 10_000_000}
    assert pending == {"leg-1": 6_000_000}


# -- plan status (derived) -----------------------------------------------------------


def test_plan_active_when_legs_expected_or_due() -> None:
    legs = [_view("l1", due=NEXT_MONTH), _view("l2", sequence=2, due=TODAY)]
    assert plan_status(legs, TODAY) is PlanStatus.ACTIVE


def test_plan_overdue_when_any_leg_overdue() -> None:
    legs = [_view("l1", due=LAST_MONTH), _view("l2", sequence=2, due=NEXT_MONTH)]
    assert plan_status(legs, TODAY) is PlanStatus.OVERDUE


def test_plan_paid_when_all_legs_paid() -> None:
    legs = [
        _view("l1", due=LAST_MONTH, received=10_000_000),
        _view("l2", sequence=2, due=NEXT_MONTH, received=10_000_000),
    ]
    assert plan_status(legs, TODAY) is PlanStatus.PAID


def test_plan_active_with_no_legs_is_defensive_default() -> None:
    assert plan_status([], TODAY) is PlanStatus.ACTIVE


# -- allocation rule -----------------------------------------------------------------


def test_allocation_pays_oldest_overdue_leg_first() -> None:
    legs = [
        _view("old-overdue", sequence=1, due=datetime.date(2026, 9, 21)),
        _view("new-overdue", sequence=2, due=datetime.date(2026, 10, 21)),
        _view("due", sequence=3, due=TODAY),
    ]
    result = allocate_receipt(MoneyIRR(10_000_000), legs, TODAY)
    assert [(a.leg_id, a.amount.amount) for a in result.allocations] == [
        ("old-overdue", 10_000_000)
    ]
    assert result.advance.amount == 0


def test_allocation_spills_from_overdue_to_due_in_order() -> None:
    legs = [
        _view("overdue", sequence=1, due=LAST_MONTH),
        _view("due-1", sequence=2, due=TODAY),
        _view("due-2", sequence=3, due=TODAY),
    ]
    result = allocate_receipt(MoneyIRR(25_000_000), legs, TODAY)
    assert [(a.leg_id, a.amount.amount) for a in result.allocations] == [
        ("overdue", 10_000_000),
        ("due-1", 10_000_000),
        ("due-2", 5_000_000),
    ]
    assert result.advance.amount == 0
    assert result.total.amount == 25_000_000


def test_allocation_never_overpays_a_leg() -> None:
    legs = [_view("leg", due=LAST_MONTH, received=9_000_000)]
    result = allocate_receipt(MoneyIRR(5_000_000), legs, TODAY)
    assert [(a.leg_id, a.amount.amount) for a in result.allocations] == [("leg", 1_000_000)]
    assert result.advance.amount == 4_000_000


def test_allocation_never_prepays_an_expected_leg() -> None:
    legs = [_view("future", due=NEXT_MONTH)]
    result = allocate_receipt(MoneyIRR(5_000_000), legs, TODAY)
    assert result.allocations == ()
    assert result.advance.amount == 5_000_000


def test_overpayment_becomes_advance_not_silent_overpayment() -> None:
    legs = [_view("only", due=LAST_MONTH)]
    result = allocate_receipt(MoneyIRR(30_000_000), legs, TODAY)
    assert [(a.leg_id, a.amount.amount) for a in result.allocations] == [("only", 10_000_000)]
    assert result.advance.amount == 20_000_000
    assert result.total.amount == 30_000_000


def test_allocation_skips_paid_legs() -> None:
    legs = [
        _view("paid", sequence=1, due=LAST_MONTH, received=10_000_000),
        _view("open", sequence=2, due=LAST_MONTH),
    ]
    result = allocate_receipt(MoneyIRR(3_000_000), legs, TODAY)
    assert [(a.leg_id, a.amount.amount) for a in result.allocations] == [("open", 3_000_000)]


def test_allocation_partially_paid_due_leg_takes_its_balance_first() -> None:
    legs = [
        _view("partial", sequence=1, due=TODAY, received=6_000_000),
        _view("fresh", sequence=2, due=TODAY),
    ]
    result = allocate_receipt(MoneyIRR(8_000_000), legs, TODAY)
    assert [(a.leg_id, a.amount.amount) for a in result.allocations] == [
        ("partial", 4_000_000),
        ("fresh", 4_000_000),
    ]


def test_allocation_refuses_non_positive_amount() -> None:
    with pytest.raises(ValueError, match="positive amount"):
        allocate_receipt(MoneyIRR(0), [_view(due=LAST_MONTH)], TODAY)


def test_allocation_single_leg_stops_after_the_first_leg() -> None:
    # A cheque receipt pays ONE leg (US-542): the remainder is
    # advance even when a second leg is due.
    legs = [
        _view("overdue", sequence=1, due=LAST_MONTH),
        _view("due", sequence=2, due=TODAY),
    ]
    result = allocate_receipt(MoneyIRR(25_000_000), legs, TODAY, single_leg=True)
    assert [(a.leg_id, a.amount.amount) for a in result.allocations] == [
        ("overdue", 10_000_000)
    ]
    assert result.advance.amount == 15_000_000
    assert result.total.amount == 25_000_000


def test_allocation_skips_pending_clearance_legs() -> None:
    # A leg fully covered by a cheque in flight takes no further
    # allocation — a second receipt must not double-pay it.
    legs = [
        _view("pending", sequence=1, due=LAST_MONTH, pending_clearance=10_000_000),
        _view("open", sequence=2, due=LAST_MONTH),
    ]
    result = allocate_receipt(MoneyIRR(3_000_000), legs, TODAY)
    assert [(a.leg_id, a.amount.amount) for a in result.allocations] == [("open", 3_000_000)]


def test_allocation_partly_pending_leg_still_takes_its_balance() -> None:
    # A leg only PARTLY covered by a pending cheque is still due /
    # overdue on its unpaid balance — a cash receipt may pay the
    # balance (the pending slice is not received, so the balance
    # is the full unpaid principal).
    legs = [_view("part", due=LAST_MONTH, pending_clearance=4_000_000)]
    result = allocate_receipt(MoneyIRR(6_000_000), legs, TODAY)
    assert [(a.leg_id, a.amount.amount) for a in result.allocations] == [("part", 6_000_000)]


# -- schedule generation -------------------------------------------------------------


def test_generate_schedule_equal_split_with_remainder_on_last_leg() -> None:
    legs = generate_schedule(
        MoneyIRR(10_000_001), 3, 30, _fd(datetime.date(2026, 12, 20))
    )
    assert [leg.principal.amount for leg in legs] == [3_333_333, 3_333_333, 3_333_335]
    assert sum(leg.principal.amount for leg in legs) == 10_000_001
    assert [leg.sequence for leg in legs] == [1, 2, 3]


def test_generate_schedule_due_dates_step_by_interval_days() -> None:
    legs = generate_schedule(
        MoneyIRR(30_000_000), 3, 30, _fd(datetime.date(2026, 12, 20))
    )
    assert [leg.due_date.gregorian_date for leg in legs] == [
        datetime.date(2026, 12, 20),
        datetime.date(2027, 1, 19),
        datetime.date(2027, 2, 18),
    ]


def test_generate_schedule_single_leg() -> None:
    legs = generate_schedule(MoneyIRR(5_000_000), 1, 30, _fd(TODAY))
    assert len(legs) == 1
    assert legs[0].principal.amount == 5_000_000
    assert legs[0].due_date.gregorian_date == TODAY


# -- plan validation -----------------------------------------------------------------


def _terms_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "invoice_id": "INV-1001",
        "total_principal_toman": 3_000_000,
        "leg_count": 3,
        "interval_days": 30,
        "first_due_date": "1405-09-29",
    }
    payload.update(overrides)
    return payload


def test_validate_terms_plan_converts_toman_to_rial_once() -> None:
    validated = validate_plan_payload(_terms_payload())
    assert validated.total_principal.amount == 30_000_000
    assert len(validated.legs) == 3
    assert [leg.principal.amount for leg in validated.legs] == [
        10_000_000,
        10_000_000,
        10_000_000,
    ]
    assert validated.interval_days == 30
    assert validated.first_due_date is not None
    assert validated.first_due_date.gregorian_date == datetime.date(2026, 12, 20)


def test_validate_explicit_legs_plan() -> None:
    validated = validate_plan_payload(
        {
            "invoice_id": "INV-1002",
            "legs": [
                {"due_date": "1405-09-29", "principal_toman": 1_000_000},
                {"due_date": "1405-10-29", "principal_toman": 2_500_000},
            ],
        }
    )
    assert validated.total_principal.amount == 35_000_000
    assert [leg.sequence for leg in validated.legs] == [1, 2]
    assert validated.interval_days is None
    assert validated.first_due_date is None


def test_validate_plan_rejects_both_shapes() -> None:
    with pytest.raises(InstallmentValidationError) as exc_info:
        validate_plan_payload(
            _terms_payload(legs=[{"due_date": "1405-09-29", "principal_toman": 1}])
        )
    assert "legs" in exc_info.value.field_errors


def test_validate_plan_rejects_neither_shape() -> None:
    with pytest.raises(InstallmentValidationError) as exc_info:
        validate_plan_payload({"invoice_id": "INV-1003"})
    assert "legs" in exc_info.value.field_errors


def test_validate_plan_rejects_bad_leg_count_and_zero_total() -> None:
    with pytest.raises(InstallmentValidationError) as exc_info:
        validate_plan_payload(_terms_payload(total_principal_toman=0, leg_count=0))
    assert "total_principal_toman" in exc_info.value.field_errors
    assert "leg_count" in exc_info.value.field_errors


def test_validate_plan_rejects_bad_jalali_date() -> None:
    with pytest.raises(InstallmentValidationError) as exc_info:
        validate_plan_payload(_terms_payload(first_due_date="1405-13-40"))
    assert "first_due_date" in exc_info.value.field_errors


def test_validate_plan_accepts_persian_digits_in_jalali_date() -> None:
    validated = validate_plan_payload(_terms_payload(first_due_date="۱۴۰۵-۰۹-۲۹"))
    assert validated.first_due_date is not None
    assert validated.first_due_date.gregorian_date == datetime.date(2026, 12, 20)


def test_validate_plan_rejects_missing_invoice() -> None:
    with pytest.raises(InstallmentValidationError) as exc_info:
        validate_plan_payload(_terms_payload(invoice_id="  "))
    assert "invoice_id" in exc_info.value.field_errors


# -- THE LINKAGE GUARD ----------------------------------------------------------------


def _receipt_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "invoice_id": "INV-1001",
        "journal_move_id": 42,
        "amount_toman": 1_000_000,
        "received_date": "1405-08-29",
        "payment_method": "cash",
    }
    payload.update(overrides)
    return payload


def test_guard_accepts_receipt_with_both_refs() -> None:
    validated = validate_receipt_payload(_receipt_payload())
    assert validated.invoice_id == "INV-1001"
    assert validated.journal_move_id == 42
    assert validated.amount.amount == 10_000_000


def test_guard_rejects_receipt_without_invoice_ref() -> None:
    with pytest.raises(InstallmentLinkageError) as exc_info:
        validate_receipt_payload(_receipt_payload(invoice_id=""))
    assert exc_info.value.code == "INSTALLMENT_RECEIPT_LINKAGE_REQUIRED"
    assert exc_info.value.missing == ("invoice_id",)


def test_guard_rejects_receipt_without_journal_ref() -> None:
    with pytest.raises(InstallmentLinkageError) as exc_info:
        validate_receipt_payload(_receipt_payload(journal_move_id=0))
    assert exc_info.value.missing == ("journal_move_id",)


def test_guard_rejects_receipt_without_both_refs_and_names_both() -> None:
    with pytest.raises(InstallmentLinkageError) as exc_info:
        validate_receipt_payload(_receipt_payload(invoice_id=None, journal_move_id=None))
    assert exc_info.value.missing == ("invoice_id", "journal_move_id")


def test_guard_runs_before_any_other_validation() -> None:
    # A receipt missing its links AND with a bad amount reports the
    # LINKAGE failure — the missing link is the finding, not the amount.
    with pytest.raises(InstallmentLinkageError):
        validate_receipt_payload(
            _receipt_payload(invoice_id="", journal_move_id=None, amount_toman=-5)
        )


def test_receipt_validation_rejects_non_positive_amount() -> None:
    with pytest.raises(InstallmentValidationError) as exc_info:
        validate_receipt_payload(_receipt_payload(amount_toman=0))
    assert "amount_toman" in exc_info.value.field_errors


def test_receipt_defaults_received_date_to_today() -> None:
    payload = _receipt_payload()
    del payload["received_date"]
    validated = validate_receipt_payload(payload)
    assert validated.received_date.gregorian_date == datetime.date.today()


def test_receipt_accepts_cheque_method_with_cheque_id_us542() -> None:
    validated = validate_receipt_payload(
        _receipt_payload(payment_method="cheque", cheque_id="00000000-0000-0000-0000-000000000542")
    )
    assert validated.payment_method.value == "cheque"
    assert validated.cheque_id == "00000000-0000-0000-0000-000000000542"


def test_receipt_rejects_cheque_method_without_cheque_id() -> None:
    with pytest.raises(InstallmentValidationError) as exc_info:
        validate_receipt_payload(_receipt_payload(payment_method="cheque"))
    assert "cheque_id" in exc_info.value.field_errors


def test_receipt_rejects_cheque_id_with_non_cheque_method() -> None:
    with pytest.raises(InstallmentValidationError) as exc_info:
        validate_receipt_payload(
            _receipt_payload(cheque_id="00000000-0000-0000-0000-000000000542")
        )
    assert "cheque_id" in exc_info.value.field_errors


def test_receipt_accepts_bnpl_method_with_gateway_fields() -> None:
    # US-543: BNPL receipts are accepted once they name the gateway
    # + the gateway's transaction id.
    validated = validate_receipt_payload(
        _receipt_payload(
            payment_method="bnpl",
            gateway="snapppay",
            gateway_reference="SP-543-001",
        )
    )
    assert validated.payment_method.value == "bnpl"
    assert validated.gateway_channel_key == "snapppay"
    assert validated.gateway_reference == "SP-543-001"


def test_receipt_bnpl_requires_gateway_and_reference() -> None:
    with pytest.raises(InstallmentValidationError) as exc_info:
        validate_receipt_payload(_receipt_payload(payment_method="bnpl"))
    assert "gateway" in exc_info.value.field_errors
    assert "gateway_reference" in exc_info.value.field_errors


def test_receipt_gateway_fields_forbidden_for_cash() -> None:
    # Fail-closed: a cash receipt naming a gateway is a caller
    # mistake, never silently ignored.
    with pytest.raises(InstallmentValidationError) as exc_info:
        validate_receipt_payload(
            _receipt_payload(payment_method="cash", gateway="snapppay")
        )
    assert "gateway" in exc_info.value.field_errors


def test_receipt_accepts_bank_transfer_and_other() -> None:
    assert validate_receipt_payload(_receipt_payload(payment_method="bank_transfer"))
    assert validate_receipt_payload(_receipt_payload(payment_method="other"))


# -- contract validation ---------------------------------------------------------------


def _contract_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "code": "STD-2026",
        "name": "قرارداد استاندارد فروش قسطی",
        "version": 1,
        "effective_from": "1405-08-01",
        "late_fee_basis": "daily_percent",
        "late_fee_rate": "0.02",
        "late_fee_grace_days": 3,
        "penalty_account_engine_id": 77,
        "penalty_account_code": "6.1.2",
    }
    payload.update(overrides)
    return payload


def test_validate_contract_keeps_exact_decimal_rate() -> None:
    validated = validate_contract_payload(_contract_payload())
    assert validated.late_fee_rate == Decimal("0.02")
    assert validated.late_fee_grace_days == 3
    assert validated.penalty_account_engine_id == 77


def test_validate_contract_rejects_float_rate() -> None:
    with pytest.raises(InstallmentValidationError) as exc_info:
        validate_contract_payload(_contract_payload(late_fee_rate=0.02))
    assert "late_fee_rate" in exc_info.value.field_errors


def test_validate_contract_rejects_rate_with_none_basis() -> None:
    with pytest.raises(InstallmentValidationError) as exc_info:
        validate_contract_payload(_contract_payload(late_fee_basis="none", late_fee_rate="0.02"))
    assert "late_fee_rate" in exc_info.value.field_errors


def test_validate_contract_rejects_zero_rate_with_percent_basis() -> None:
    with pytest.raises(InstallmentValidationError) as exc_info:
        validate_contract_payload(_contract_payload(late_fee_rate="0"))
    assert "late_fee_rate" in exc_info.value.field_errors


def test_validate_contract_none_basis_with_zero_rate_is_valid() -> None:
    validated = validate_contract_payload(
        _contract_payload(late_fee_basis="none", late_fee_rate="0")
    )
    assert validated.late_fee_rate == Decimal("0")


def test_contract_model_refuses_negative_grace_days() -> None:
    with pytest.raises(ValueError, match="grace_days"):
        InstallmentSaleContract(
            id="c1",
            tenant_id="t",
            installation_id="i",
            company_engine_id=1,
            code="X",
            name="X",
            version=1,
            effective_from=_fd(TODAY),
            late_fee_grace_days=-1,
        )
