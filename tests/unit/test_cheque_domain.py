"""Domain tests for the cheque lifecycle (US-531 + US-532).

DB-free and engine-free: the state machine, the §2.4 journal
designs, the 24-field payload validation and the pre-acceptance
verdict are all pure functions.
"""

from __future__ import annotations

import pytest

from hesabyar.domain.cheques.errors import (
    ChequeTransitionNotAllowedError,
    ChequeValidationError,
)
from hesabyar.domain.cheques.models import (
    ChequeKind,
    ChequePurpose,
    ChequeSettings,
    ChequeState,
    InstallmentLegStatus,
    RedBrownPolicy,
)
from hesabyar.domain.cheques.state_machine import (
    COLLATERAL_JOURNAL_DESIGNS,
    ISSUED_JOURNAL_DESIGNS,
    JOURNAL_DESIGNS,
    NO_JOURNAL_TRANSITIONS,
    TRANSITION_BOUNCE,
    TRANSITION_CALL_IN,
    TRANSITION_CLEAR,
    TRANSITION_CONFIRM_RECEIPT,
    TRANSITION_DEPOSIT,
    TRANSITION_ENDORSE,
    TRANSITION_ENDORSED_BOUNCE,
    TRANSITION_EXPIRE,
    TRANSITION_ISSUE,
    TRANSITION_REJECT_MISMATCH,
    TRANSITION_RETURN,
    TRANSITION_UNEXPECTED_CALL_IN,
    TRANSITION_VOID,
    PreAcceptanceInput,
    assert_transition_scope,
    evaluate_pre_acceptance,
    journal_design_for,
    leg_status_effect,
    target_state,
)
from hesabyar.domain.cheques.validation import validate_registration_payload


def _payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "sayad_id": "1234567890123456",
        "amount_toman": 1750000,
        "amount_currency": "IRT",
        "issue_date": "1405-08-26",
        "due_date": "1405-10-15",
        "issuer_name": "مشتری نمونه",
        "issuer_national_id": "0012345678",
        "issuer_bank": "056 سامان",
        "payee_name": "کافه ایوان",
        "payee_national_id": "00123456789",
        "purpose": "payment",
        "order_id": "ord-1",
        "customer_ref": "cust-1",
        "sayad_registered": True,
    }
    base.update(overrides)
    return base


# -- 24-field validation ------------------------------------------------------


def test_valid_payload_converts_toman_to_rial_x10():
    reg = validate_registration_payload(_payload())
    assert reg.amount_irr == 17_500_000
    assert reg.amount_toman == 1_750_000
    assert reg.due_date.date > reg.issue_date.date


def test_irr_currency_is_not_converted():
    reg = validate_registration_payload(_payload(amount_currency="IRR"))
    assert reg.amount_irr == 1_750_000


def test_sayad_id_must_be_exactly_16_digits():
    with pytest.raises(ChequeValidationError) as exc:
        validate_registration_payload(_payload(sayad_id="1234 5678 9012 3456"))
    assert "sayad_id" in exc.value.field_errors
    with pytest.raises(ChequeValidationError):
        validate_registration_payload(_payload(sayad_id="123456789012345"))


def test_national_id_must_be_10_or_11_digits():
    with pytest.raises(ChequeValidationError) as exc:
        validate_registration_payload(_payload(issuer_national_id="123456789"))
    assert "issuer_national_id" in exc.value.field_errors
    validate_registration_payload(_payload(issuer_national_id="00123456789"))


def test_due_before_issue_is_rejected():
    with pytest.raises(ChequeValidationError) as exc:
        validate_registration_payload(
            _payload(issue_date="1405-10-15", due_date="1405-08-26")
        )
    assert "due_date" in exc.value.field_errors


def test_zero_amount_is_rejected():
    with pytest.raises(ChequeValidationError) as exc:
        validate_registration_payload(_payload(amount_toman=0))
    assert "amount_toman" in exc.value.field_errors


def test_missing_mandatory_field_is_reported():
    payload = _payload()
    del payload["order_id"]
    with pytest.raises(ChequeValidationError) as exc:
        validate_registration_payload(payload)
    assert "order_id" in exc.value.field_errors


def test_unknown_field_is_rejected_fail_closed():
    with pytest.raises(ChequeValidationError) as exc:
        validate_registration_payload(_payload(surprise_field="x"))
    assert "__unknown__" in exc.value.field_errors


def test_all_field_errors_are_reported_at_once():
    with pytest.raises(ChequeValidationError) as exc:
        validate_registration_payload(
            _payload(sayad_id="bad", amount_toman=-5, purpose="gift")
        )
    assert {"sayad_id", "amount_toman", "purpose"} <= set(exc.value.field_errors)


def test_optional_fields_pass_through():
    reg = validate_registration_payload(
        _payload(
            leaf_number="847291",
            issuer_branch="شعبه مرکزی",
            issuer_account_number="1234567890",
            reason="بابت سفارش",
            invoice_id="inv-9",
            installment_leg_id="leg-1",
            scan_url="https://example.invalid/scan.jpg",
            scan_ref="scan-1",
            registered_by="operator",
            notes="note",
        )
    )
    assert reg.leaf_number == "847291"
    assert reg.installment_leg_id == "leg-1"
    assert reg.invoice_id == "inv-9"


def test_invalid_jalali_date_is_rejected():
    with pytest.raises(ChequeValidationError) as exc:
        validate_registration_payload(_payload(issue_date="1405-13-01"))
    assert "issue_date" in exc.value.field_errors


# -- state machine (§2.1) -------------------------------------------------------


def test_full_commercial_lifecycle_paths():
    assert target_state(ChequeState.REGISTERED, TRANSITION_CONFIRM_RECEIPT) is ChequeState.HELD
    assert target_state(ChequeState.HELD, TRANSITION_DEPOSIT) is ChequeState.IN_CLEARING
    assert target_state(ChequeState.IN_CLEARING, TRANSITION_CLEAR) is ChequeState.CLEARED
    assert target_state(ChequeState.IN_CLEARING, TRANSITION_BOUNCE) is ChequeState.BOUNCED
    assert target_state(ChequeState.BOUNCED, TRANSITION_RETURN) is ChequeState.RETURNED
    assert target_state(ChequeState.HELD, TRANSITION_ENDORSE) is ChequeState.ENDORSED
    assert target_state(ChequeState.HELD, TRANSITION_RETURN) is ChequeState.RETURNED
    assert target_state(ChequeState.REGISTERED, TRANSITION_EXPIRE) is ChequeState.EXPIRED_UNRECEIVED
    assert target_state(ChequeState.REGISTERED, TRANSITION_REJECT_MISMATCH) is ChequeState.REJECTED_MISMATCH


def test_illegal_transition_is_refused():
    with pytest.raises(ChequeTransitionNotAllowedError):
        target_state(ChequeState.REGISTERED, TRANSITION_CLEAR)
    with pytest.raises(ChequeTransitionNotAllowedError):
        target_state(ChequeState.CLEARED, TRANSITION_RETURN)


def test_bounced_representment_stays_disabled():
    """§9.1 #5: bounced -> in_clearing is DISABLED, with the design reason."""
    with pytest.raises(ChequeTransitionNotAllowedError) as exc:
        target_state(ChequeState.BOUNCED, TRANSITION_DEPOSIT)
    assert "DISABLED" in str(exc.value)
    assert "9.1" in str(exc.value)


def test_journal_designs_match_section_2_4():
    design = journal_design_for(TRANSITION_CONFIRM_RECEIPT)
    assert design is not None and design.number == "۱"
    assert design.debit_role == "اسناد دریافتنی نزد صندوق"
    assert design.credit_role == "حساب‌های دریافتنی"
    assert design.credit_alternative_role == "پیش‌دریافت مشتری"

    design = journal_design_for(TRANSITION_DEPOSIT)
    assert design is not None and design.number == "۲"
    assert design.debit_role == "اسناد دریافتنی در جریان وصول"
    assert design.credit_role == "اسناد دریافتنی نزد صندوق"

    design = journal_design_for(TRANSITION_CLEAR)
    assert design is not None and design.number == "۳"
    assert design.debit_role == "موجودی نقد و بانک"

    design = journal_design_for(TRANSITION_BOUNCE)
    assert design is not None and design.number == "۴"
    assert design.debit_role == "حساب‌های دریافتنی"
    assert design.debit_alternative_role == "چک‌های برگشتی/مطالبات غیرجاری"
    assert design.current_period_only is True

    design = journal_design_for(TRANSITION_ENDORSE)
    assert design is not None and design.number == "۵"
    assert design.debit_role == "حساب‌های پرداختنی"

    design = journal_design_for(TRANSITION_RETURN)
    assert design is not None and design.number == "۶"
    assert design.debit_role == "حساب‌های دریافتنی"

    assert len(JOURNAL_DESIGNS) == 10


def test_no_journal_transitions():
    assert journal_design_for(TRANSITION_EXPIRE) is None
    assert journal_design_for(TRANSITION_REJECT_MISMATCH) is None
    assert TRANSITION_EXPIRE in NO_JOURNAL_TRANSITIONS
    assert TRANSITION_REJECT_MISMATCH in NO_JOURNAL_TRANSITIONS


def test_collateral_purpose_refuses_commercial_transitions():
    # A collateral cheque never takes the commercial deposit (it
    # has no commercial claim until called in).
    with pytest.raises(ChequeTransitionNotAllowedError):
        assert_transition_scope(
            ChequeKind.RECEIVED,
            ChequePurpose.COLLATERAL,
            ChequeState.HELD,
            TRANSITION_DEPOSIT,
        )
    # But confirm_receipt IS valid for collateral: registered ->
    # held takes the memorandum branch (سند ۷), not the commercial one.
    assert_transition_scope(
        ChequeKind.RECEIVED,
        ChequePurpose.COLLATERAL,
        ChequeState.REGISTERED,
        TRANSITION_CONFIRM_RECEIPT,
    )
    # Commercial purpose passes the same transitions.
    assert_transition_scope(
        ChequeKind.RECEIVED,
        ChequePurpose.PAYMENT,
        ChequeState.HELD,
        TRANSITION_DEPOSIT,
    )


# -- US-532: issued + collateral state machine (§2.2 / §2.3) -------------------


def test_issued_lifecycle_paths():
    assert target_state(ChequeState.REGISTERED, TRANSITION_ISSUE) == ChequeState.ISSUED
    assert target_state(ChequeState.ISSUED, TRANSITION_CLEAR) == ChequeState.CLEARED
    assert target_state(ChequeState.ISSUED, TRANSITION_BOUNCE) == ChequeState.BOUNCED
    assert target_state(ChequeState.ISSUED, TRANSITION_VOID) == ChequeState.VOIDED
    assert target_state(ChequeState.ISSUED, TRANSITION_RETURN) == ChequeState.RETURNED
    assert target_state(ChequeState.ISSUED, TRANSITION_UNEXPECTED_CALL_IN) == ChequeState.CALLED_IN
    assert target_state(ChequeState.CALLED_IN, TRANSITION_CLEAR) == ChequeState.CLEARED


def test_collateral_lifecycle_paths():
    assert target_state(ChequeState.REGISTERED, TRANSITION_CONFIRM_RECEIPT) == ChequeState.HELD
    assert target_state(ChequeState.HELD, TRANSITION_CALL_IN) == ChequeState.CALLED_IN
    assert target_state(ChequeState.HELD, TRANSITION_RETURN) == ChequeState.RETURNED
    assert target_state(ChequeState.CALLED_IN, TRANSITION_DEPOSIT) == ChequeState.IN_CLEARING
    assert target_state(ChequeState.CALLED_IN, TRANSITION_ENDORSE) == ChequeState.ENDORSED
    assert target_state(ChequeState.CALLED_IN, TRANSITION_RETURN) == ChequeState.RETURNED
    assert target_state(ChequeState.ENDORSED, TRANSITION_ENDORSED_BOUNCE) == ChequeState.BOUNCED


def test_issued_scope_refusals():
    # A received cheque can never be issued, voided or unexpectedly called in.
    for transition in (TRANSITION_ISSUE, TRANSITION_VOID, TRANSITION_UNEXPECTED_CALL_IN):
        with pytest.raises(ChequeTransitionNotAllowedError):
            assert_transition_scope(
                ChequeKind.RECEIVED,
                ChequePurpose.PAYMENT,
                ChequeState.REGISTERED,
                transition,
            )
    # An issued cheque can never be deposited or confirmed as received.
    for transition in (TRANSITION_DEPOSIT, TRANSITION_CONFIRM_RECEIPT):
        with pytest.raises(ChequeTransitionNotAllowedError):
            assert_transition_scope(
                ChequeKind.ISSUED,
                ChequePurpose.PAYMENT,
                ChequeState.REGISTERED,
                transition,
            )
    # A collateral cheque can never be issued commercially — only the
    # memorandum direction; call_in is the collateral-only commercialisation.
    with pytest.raises(ChequeTransitionNotAllowedError):
        assert_transition_scope(
            ChequeKind.RECEIVED,
            ChequePurpose.COLLATERAL,
            ChequeState.REGISTERED,
            TRANSITION_ISSUE,
        )
    with pytest.raises(ChequeTransitionNotAllowedError):
        assert_transition_scope(
            ChequeKind.ISSUED,
            ChequePurpose.PAYMENT,
            ChequeState.ISSUED,
            TRANSITION_CALL_IN,
        )


def test_issued_journal_designs():
    design = journal_design_for(TRANSITION_ISSUE, ChequeKind.ISSUED, ChequePurpose.PAYMENT)
    assert design is not None and design.number == "۱۰"
    assert design.debit_role == "حساب‌های پرداختنی"
    assert design.credit_role == "اسناد پرداختنی"

    design = journal_design_for(TRANSITION_CLEAR, ChequeKind.ISSUED, ChequePurpose.PAYMENT)
    assert design is not None and design.number == "۱۱"
    assert design.debit_role == "اسناد پرداختنی"
    assert design.credit_role == "موجودی نقد و بانک"

    design = journal_design_for(TRANSITION_BOUNCE, ChequeKind.ISSUED, ChequePurpose.PAYMENT)
    assert design is not None and design.number == "۱۲"
    assert design.debit_role == "اسناد پرداختنی"
    assert design.credit_role == "حساب‌های پرداختنی"

    design = journal_design_for(TRANSITION_VOID, ChequeKind.ISSUED, ChequePurpose.PAYMENT)
    assert design is not None and design.number == "۱۳"
    assert design.debit_role == "اسناد پرداختنی"
    assert design.credit_role == "حساب‌های پرداختنی"

    # Issue (۱۰) and void (۱۳) live in the main table; only the
    # designs that DIFFER from the received ones (۱۱/۱۲) are in the
    # issued table.
    assert len(ISSUED_JOURNAL_DESIGNS) == 2


def test_collateral_journal_designs_are_memorandum_only():
    # سند ۷ / ۸ (received) and their issued-direction mirrors use
    # ONLY memorandum roles (اسناد تضمینی / طرف حساب انتظامی) —
    # never a commercial role. The engine maps these roles to the
    # 7.x memorandum accounts; the contract test asserts the codes.
    memorandum_roles = {"اسناد تضمینی دریافتی", "طرف حساب‌های انتظامی",
                        "اسناد تضمینی نزد دیگران",
                        "طرف حساب اسناد تضمینی نزد دیگران"}
    for design in COLLATERAL_JOURNAL_DESIGNS.values():
        assert design.debit_role in memorandum_roles
        assert design.credit_role in memorandum_roles
    held = journal_design_for(
        TRANSITION_CONFIRM_RECEIPT, ChequeKind.RECEIVED, ChequePurpose.COLLATERAL
    )
    assert held is not None and held.number == "۷"
    assert held.debit_role == "اسناد تضمینی دریافتی"
    assert held.credit_role == "طرف حساب‌های انتظامی"

    returned = journal_design_for(
        TRANSITION_RETURN, ChequeKind.RECEIVED, ChequePurpose.COLLATERAL
    )
    assert returned is not None and returned.number == "۸"
    assert returned.debit_role == "طرف حساب‌های انتظامی"
    assert returned.credit_role == "اسناد تضمینی دریافتی"

    issued = journal_design_for(
        TRANSITION_ISSUE, ChequeKind.ISSUED, ChequePurpose.COLLATERAL
    )
    assert issued is not None and issued.number == "۷"
    assert issued.debit_role == "اسناد تضمینی نزد دیگران"
    assert issued.credit_role == "طرف حساب اسناد تضمینی نزد دیگران"

    issued_return = journal_design_for(
        TRANSITION_RETURN, ChequeKind.ISSUED, ChequePurpose.COLLATERAL
    )
    assert issued_return is not None and issued_return.number == "۸"
    assert issued_return.debit_role == "طرف حساب اسناد تضمینی نزد دیگران"
    assert issued_return.credit_role == "اسناد تضمینی نزد دیگران"

    assert len(COLLATERAL_JOURNAL_DESIGNS) == 4


def test_call_in_designs_carry_the_companion():
    design = journal_design_for(
        TRANSITION_CALL_IN, ChequeKind.RECEIVED, ChequePurpose.COLLATERAL
    )
    assert design is not None and design.number == "۹ب"
    assert design.companion_number == "۹الف"
    assert design.companion_debit_role == "طرف حساب‌های انتظامی"
    assert design.companion_credit_role == "اسناد تضمینی دریافتی"
    assert design.debit_role == "اسناد دریافتنی نزد صندوق"
    assert design.credit_role == "حساب‌های دریافتنی"

    # Unexpected call-in of an ISSUED collateral cheque reverses the
    # memorandum only — the commercial journal is accountant-gated.
    design = journal_design_for(
        TRANSITION_UNEXPECTED_CALL_IN, ChequeKind.ISSUED, ChequePurpose.COLLATERAL
    )
    assert design is not None and design.number == "۷-معکوس"
    assert design.debit_role == "طرف حساب اسناد تضمینی نزد دیگران"
    assert design.credit_role == "اسناد تضمینی نزد دیگران"
    assert design.gated_note is not None and "9.1" in design.gated_note


def test_endorsed_bounce_has_no_journal():
    assert journal_design_for(TRANSITION_ENDORSED_BOUNCE) is None
    assert TRANSITION_ENDORSED_BOUNCE in NO_JOURNAL_TRANSITIONS


def test_endorse_design_debits_trade_payables():
    # سند ۵ debits Trade Payables (حساب‌های پرداختنی). The engine
    # maps that role to 2.1.1 — US-532 found US-531 had mapped it to
    # 2.1.5 (Notes Payable); the contract test asserts the live codes.
    design = journal_design_for(TRANSITION_ENDORSE)
    assert design is not None and design.number == "۵"
    assert design.debit_role == "حساب‌های پرداختنی"
    assert design.credit_role == "اسناد دریافتنی نزد صندوق"


def test_issue_from_held_or_cleared_is_refused():
    with pytest.raises(ChequeTransitionNotAllowedError):
        target_state(ChequeState.HELD, TRANSITION_ISSUE)
    with pytest.raises(ChequeTransitionNotAllowedError):
        target_state(ChequeState.CLEARED, TRANSITION_VOID)
    with pytest.raises(ChequeTransitionNotAllowedError):
        target_state(ChequeState.VOIDED, TRANSITION_CLEAR)


def test_terminal_states_include_voided_not_called_in():
    assert ChequeState.VOIDED in ChequeState.__members__.values()
    from hesabyar.domain.cheques.models import TERMINAL_STATES

    assert ChequeState.VOIDED in TERMINAL_STATES
    assert ChequeState.CALLED_IN not in TERMINAL_STATES
    assert ChequeState.ISSUED not in TERMINAL_STATES


# -- pre-acceptance (§2.1) -------------------------------------------------------


def test_mismatch_is_never_confirmed():
    verdict = evaluate_pre_acceptance(
        PreAcceptanceInput(items_matched=False),
        red_brown_policy=RedBrownPolicy.MANAGER_APPROVAL,
        max_in_transit_commitments_irr=None,
    )
    assert verdict.decision == "reject"


def test_white_colour_accepts_with_a_warning():
    verdict = evaluate_pre_acceptance(
        PreAcceptanceInput(items_matched=True, color_code=1),
        red_brown_policy=RedBrownPolicy.MANAGER_APPROVAL,
        max_in_transit_commitments_irr=None,
    )
    assert verdict.accepted
    assert any("سفید" in warning for warning in verdict.warnings)


def test_red_colour_follows_the_installation_policy():
    red = PreAcceptanceInput(items_matched=True, color_code=5)
    verdict = evaluate_pre_acceptance(
        red,
        red_brown_policy=RedBrownPolicy.REJECT,
        max_in_transit_commitments_irr=None,
    )
    assert verdict.decision == "reject"
    verdict = evaluate_pre_acceptance(
        red,
        red_brown_policy=RedBrownPolicy.MANAGER_APPROVAL,
        max_in_transit_commitments_irr=None,
    )
    assert verdict.decision == "manager_approval_required"


def test_brown_colour_is_treated_like_red():
    verdict = evaluate_pre_acceptance(
        PreAcceptanceInput(items_matched=True, color_code=4),
        red_brown_policy=RedBrownPolicy.MANAGER_APPROVAL,
        max_in_transit_commitments_irr=None,
    )
    assert verdict.decision == "manager_approval_required"


def test_high_in_transit_commitments_trigger_the_policy():
    verdict = evaluate_pre_acceptance(
        PreAcceptanceInput(items_matched=True, in_transit_commitments_irr=900_000_000),
        red_brown_policy=RedBrownPolicy.MANAGER_APPROVAL,
        max_in_transit_commitments_irr=500_000_000,
    )
    assert verdict.decision == "manager_approval_required"
    verdict = evaluate_pre_acceptance(
        PreAcceptanceInput(items_matched=True, in_transit_commitments_irr=100_000_000),
        red_brown_policy=RedBrownPolicy.MANAGER_APPROVAL,
        max_in_transit_commitments_irr=500_000_000,
    )
    assert verdict.accepted


def test_invalid_colour_code_is_rejected():
    verdict = evaluate_pre_acceptance(
        PreAcceptanceInput(items_matched=True, color_code=9),
        red_brown_policy=RedBrownPolicy.MANAGER_APPROVAL,
        max_in_transit_commitments_irr=None,
    )
    assert verdict.decision == "reject"


def test_back_cheques_only_warn():
    verdict = evaluate_pre_acceptance(
        PreAcceptanceInput(items_matched=True, color_code=2, back_cheques_count=2),
        red_brown_policy=RedBrownPolicy.MANAGER_APPROVAL,
        max_in_transit_commitments_irr=None,
    )
    assert verdict.accepted
    assert verdict.warnings


# -- installment-leg hook --------------------------------------------------------


def test_leg_status_effects():
    assert leg_status_effect(TRANSITION_CLEAR) is InstallmentLegStatus.PAID
    assert leg_status_effect(TRANSITION_BOUNCE) is InstallmentLegStatus.UNPAID
    assert leg_status_effect(TRANSITION_DEPOSIT) is None
    assert leg_status_effect(TRANSITION_CONFIRM_RECEIPT) is None


def test_settings_defaults_are_the_section_9_1_defaults():
    settings = ChequeSettings(installation_id="inst-1")
    assert settings.leaf_arrival_deadline_days == 7
    assert settings.bounce_destination.value == "receivables"
    assert settings.red_brown_policy is RedBrownPolicy.MANAGER_APPROVAL
    assert settings.default_storage_location.value == "vault"
