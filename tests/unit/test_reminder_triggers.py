"""Unit tests for the reminder triggers + messages + validation (US-564).

Pure functions — no I/O. "Today" is pinned explicitly (2026-11-20 =
1405/08/29, a Friday — the Friday skip belongs to the scheduler,
not to these triggers, so the date choice is incidental here).
"""

from __future__ import annotations

import datetime

import pytest

from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.reminders.errors import ReminderValidationError
from hesabyar.domain.reminders.messages import (
    cashbox_uncounted_message,
    cheque_due_soon_message,
    enmad_expiry_message,
    installment_overdue_message,
)
from hesabyar.domain.reminders.models import (
    ReminderChannel,
    ReminderType,
    build_idempotency_key,
    is_quiet_hour,
)
from hesabyar.domain.reminders.triggers import (
    CashboxCountView,
    ChequeDueView,
    cashbox_uncounted,
    cheque_due_soon,
    enmad_expiry,
)
from hesabyar.domain.reminders.validation import validate_preferences_payload

TODAY = datetime.date(2026, 11, 20)  # 1405/08/29


def _fiscal(day: datetime.date) -> FiscalDate:
    return FiscalDate(day)


def _cheque(
    cheque_id: str,
    due: datetime.date,
    *,
    state: str = "held",
    purpose: str = "payment",
    kind: str = "received",
) -> ChequeDueView:
    return ChequeDueView(
        cheque_id=cheque_id,
        due_date=_fiscal(due),
        state=state,
        purpose=purpose,
        kind=kind,
    )


# ---------------------------------------------------------------------------
# Cheque due-soon triggers
# ---------------------------------------------------------------------------


def test_cheque_fires_at_7_and_3_days_before_due() -> None:
    cheques = [
        _cheque("c7", TODAY + datetime.timedelta(days=7)),
        _cheque("c3", TODAY + datetime.timedelta(days=3)),
        _cheque("c5", TODAY + datetime.timedelta(days=5)),
        _cheque("c0", TODAY),
    ]
    due = cheque_due_soon(cheques, TODAY, (7, 3))
    assert [(item.cheque_id, item.lead_days) for item in due] == [
        ("c3", 3),
        ("c7", 7),
    ]


def test_cheque_due_date_ordering_most_urgent_first() -> None:
    cheques = [
        _cheque("c7", TODAY + datetime.timedelta(days=7)),
        _cheque("c3", TODAY + datetime.timedelta(days=3)),
    ]
    due = cheque_due_soon(cheques, TODAY, (7, 3))
    assert due[0].cheque_id == "c3"


def test_cheque_collateral_never_fires() -> None:
    cheques = [_cheque("c1", TODAY + datetime.timedelta(days=3), purpose="collateral")]
    assert cheque_due_soon(cheques, TODAY, (7, 3)) == []


def test_cheque_in_clearing_never_fires() -> None:
    # The bank has the leaf; the due date is the bank's problem now.
    cheques = [_cheque("c1", TODAY + datetime.timedelta(days=3), state="in_clearing")]
    assert cheque_due_soon(cheques, TODAY, (7, 3)) == []


def test_cheque_registered_never_fires() -> None:
    # The leaf never arrived — US-531's job owns those reminders.
    cheques = [_cheque("c1", TODAY + datetime.timedelta(days=3), state="registered")]
    assert cheque_due_soon(cheques, TODAY, (7, 3)) == []


def test_cheque_terminal_states_never_fire() -> None:
    for state in ("cleared", "bounced", "returned", "endorsed", "voided"):
        cheques = [_cheque("c1", TODAY + datetime.timedelta(days=3), state=state)]
        assert cheque_due_soon(cheques, TODAY, (7, 3)) == []


def test_cheque_issued_and_called_in_fire() -> None:
    cheques = [
        _cheque("c-issued", TODAY + datetime.timedelta(days=3), state="issued", kind="issued"),
        _cheque("c-called", TODAY + datetime.timedelta(days=7), state="called_in"),
    ]
    due = cheque_due_soon(cheques, TODAY, (7, 3))
    assert {item.cheque_id for item in due} == {"c-issued", "c-called"}


def test_cheque_overdue_never_fires() -> None:
    cheques = [_cheque("c1", TODAY - datetime.timedelta(days=1))]
    assert cheque_due_soon(cheques, TODAY, (7, 3)) == []


# ---------------------------------------------------------------------------
# Cash-box triggers
# ---------------------------------------------------------------------------


def _box(
    box_id: str,
    reference: datetime.date | None,
    *,
    never_counted: bool = False,
    has_open_session: bool = False,
) -> CashboxCountView:
    return CashboxCountView(
        box_id=box_id,
        box_name=f"صندوق {box_id}",
        reference_date=reference,
        never_counted=never_counted,
        has_open_session=has_open_session,
    )


def test_cashbox_fires_when_uncounted_more_than_n_days() -> None:
    boxes = [
        _box("old", TODAY - datetime.timedelta(days=8)),
        _box("exact", TODAY - datetime.timedelta(days=7)),
        _box("fresh", TODAY - datetime.timedelta(days=2)),
    ]
    due = cashbox_uncounted(boxes, TODAY, 7)
    assert [item.box_id for item in due] == ["old"]
    assert due[0].days_uncounted == 8


def test_cashbox_threshold_is_user_configured() -> None:
    boxes = [_box("b1", TODAY - datetime.timedelta(days=4))]
    assert cashbox_uncounted(boxes, TODAY, 7) == []
    assert [item.box_id for item in cashbox_uncounted(boxes, TODAY, 3)] == ["b1"]


def test_cashbox_open_session_never_fires() -> None:
    boxes = [
        _box("b1", TODAY - datetime.timedelta(days=30), has_open_session=True),
    ]
    assert cashbox_uncounted(boxes, TODAY, 7) == []


def test_cashbox_never_counted_uses_creation_date() -> None:
    boxes = [_box("b1", TODAY - datetime.timedelta(days=10), never_counted=True)]
    due = cashbox_uncounted(boxes, TODAY, 7)
    assert len(due) == 1
    assert due[0].never_counted is True


def test_cashbox_no_reference_date_never_fires() -> None:
    boxes = [_box("b1", None, never_counted=True)]
    assert cashbox_uncounted(boxes, TODAY, 7) == []


def test_cashbox_longest_uncounted_first() -> None:
    boxes = [
        _box("b8", TODAY - datetime.timedelta(days=8)),
        _box("b30", TODAY - datetime.timedelta(days=30)),
    ]
    due = cashbox_uncounted(boxes, TODAY, 7)
    assert [item.box_id for item in due] == ["b30", "b8"]


# ---------------------------------------------------------------------------
# eNamad triggers
# ---------------------------------------------------------------------------


def test_enmad_fires_at_configured_leads() -> None:
    assert enmad_expiry(_fiscal(TODAY + datetime.timedelta(days=30)), TODAY, (30, 7, 0)) is not None
    assert enmad_expiry(_fiscal(TODAY + datetime.timedelta(days=7)), TODAY, (30, 7, 0)) is not None
    assert enmad_expiry(_fiscal(TODAY), TODAY, (30, 7, 0)) is not None
    assert enmad_expiry(_fiscal(TODAY + datetime.timedelta(days=8)), TODAY, (30, 7, 0)) is None


def test_enmad_no_date_never_fires() -> None:
    assert enmad_expiry(None, TODAY, (30, 7, 0)) is None


def test_enmad_expired_never_fires() -> None:
    # The service is already disabled; a reminder cannot undo that.
    assert enmad_expiry(_fiscal(TODAY - datetime.timedelta(days=1)), TODAY, (30, 7, 0)) is None


def test_enmad_lead_days_reported() -> None:
    firing = enmad_expiry(_fiscal(TODAY + datetime.timedelta(days=7)), TODAY, (30, 7, 0))
    assert firing is not None
    assert firing.lead_days == 7


# ---------------------------------------------------------------------------
# Idempotency keys
# ---------------------------------------------------------------------------


def test_idempotency_key_shape() -> None:
    key = build_idempotency_key(
        reminder_type=ReminderType.CHEQUE_DUE_SOON,
        subject_id="cheque-1",
        event_ref="2026-11-27",
        channel=ReminderChannel.SMS,
    )
    assert key == "cheque_due_soon:cheque-1:2026-11-27:sms"


def test_idempotency_key_differs_per_channel_lead_and_event() -> None:
    sms = build_idempotency_key(
        reminder_type=ReminderType.CHEQUE_DUE_SOON,
        subject_id="cheque-1",
        event_ref="2026-11-27",
        channel=ReminderChannel.SMS,
    )
    in_app = build_idempotency_key(
        reminder_type=ReminderType.CHEQUE_DUE_SOON,
        subject_id="cheque-1",
        event_ref="2026-11-27",
        channel=ReminderChannel.IN_APP,
    )
    other_date = build_idempotency_key(
        reminder_type=ReminderType.CHEQUE_DUE_SOON,
        subject_id="cheque-1",
        event_ref="2026-11-28",
        channel=ReminderChannel.SMS,
    )
    assert sms != in_app
    assert sms != other_date


# ---------------------------------------------------------------------------
# Quiet hours
# ---------------------------------------------------------------------------


def test_quiet_hours_overnight_window() -> None:
    start = datetime.time(22, 0)
    end = datetime.time(8, 0)
    assert is_quiet_hour(datetime.datetime(2026, 11, 20, 23, 30), start=start, end=end) is True
    assert is_quiet_hour(datetime.datetime(2026, 11, 21, 2, 0), start=start, end=end) is True
    assert is_quiet_hour(datetime.datetime(2026, 11, 21, 7, 59), start=start, end=end) is True
    assert is_quiet_hour(datetime.datetime(2026, 11, 21, 8, 0), start=start, end=end) is False
    assert is_quiet_hour(datetime.datetime(2026, 11, 20, 21, 59), start=start, end=end) is False
    assert is_quiet_hour(datetime.datetime(2026, 11, 20, 12, 0), start=start, end=end) is False


def test_quiet_hours_daytime_window() -> None:
    start = datetime.time(13, 0)
    end = datetime.time(15, 0)
    assert is_quiet_hour(datetime.datetime(2026, 11, 20, 14, 0), start=start, end=end) is True
    assert is_quiet_hour(datetime.datetime(2026, 11, 20, 15, 0), start=start, end=end) is False


def test_quiet_hours_equal_bounds_is_empty() -> None:
    start = end = datetime.time(8, 0)
    assert is_quiet_hour(datetime.datetime(2026, 11, 20, 8, 0), start=start, end=end) is False


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def test_cheque_message_uses_persian_digits_and_jalali_date() -> None:
    message = cheque_due_soon_message(
        amount_toman=12500,
        due_date=_fiscal(datetime.date(2026, 11, 27)),
        days_remaining=7,
        sayad_id="1234567890123456",
        issuer_name="علی رضایی",
        kind="received",
    )
    assert "۱۲۵۰۰" in message  # Persian digits, ungrouped
    assert "۱۴۰۵/۰۹/۰۶" in message  # Jalali due date
    assert "۷ روز" in message
    assert "1234567890123456" in message  # identifier verbatim
    assert "علی رضایی" in message


def test_cheque_issued_message_names_the_payee() -> None:
    message = cheque_due_soon_message(
        amount_toman=1000,
        due_date=_fiscal(datetime.date(2026, 11, 23)),
        days_remaining=3,
        sayad_id="1234567890123456",
        issuer_name="فروشنده",
        kind="issued",
    )
    assert "صادره" in message
    assert "فروشنده" in message


def test_installment_overdue_message_states_balance_and_days() -> None:
    message = installment_overdue_message(
        amount_toman=2500,
        due_date=_fiscal(datetime.date(2026, 11, 19)),
        days_overdue=1,
        invoice_id="INV-9",
        leg_sequence=2,
    )
    assert "۲۵۰۰" in message
    assert "۱ روز معوق" in message
    assert "INV-9" in message
    assert "قسط ۲" in message


def test_cashbox_message_states_days_and_last_count() -> None:
    message = cashbox_uncounted_message(
        box_name="صندوق اصلی",
        days_uncounted=9,
        last_count_jalali="۱۴۰۵/۰۸/۲۰",
    )
    assert "صندوق «صندوق اصلی»" in message
    assert "۹ روز" in message
    assert "۱۴۰۵/۰۸/۲۰" in message


def test_cashbox_never_counted_message_says_so() -> None:
    message = cashbox_uncounted_message(
        box_name="صندوق فرعی",
        days_uncounted=12,
        last_count_jalali=None,
    )
    assert "تاکنون شمارش نشده است" in message


def test_enmad_message_states_the_service_is_disabled() -> None:
    message = enmad_expiry_message(
        expiry_date=_fiscal(datetime.date(2026, 11, 27)),
        days_remaining=7,
    )
    # §8.3: expiry DISABLES gateway/BNPL service — the warning must say so plainly.
    assert "غیرفعال می‌شود" in message
    assert "درگاه پرداخت" in message
    assert "BNPL" in message
    assert "۷ روز دیگر" in message


def test_enmad_expiry_day_message_is_present_tense() -> None:
    message = enmad_expiry_message(
        expiry_date=_fiscal(TODAY),
        days_remaining=0,
    )
    assert "امروز" in message
    assert "غیرفعال می‌شود" in message


# ---------------------------------------------------------------------------
# Preferences validation
# ---------------------------------------------------------------------------


def test_minimal_payload_uses_defaults() -> None:
    validated = validate_preferences_payload({"enabled": True})
    assert validated.enabled is True
    assert validated.cheque_due_soon_enabled is True
    assert validated.channel_sms is True
    assert validated.cheque_lead_days == (7, 3)
    assert validated.installment_overdue_days == (1, 7)
    assert validated.cashbox_uncounted_days == 7
    assert validated.enmad_lead_days == (30, 7, 0)
    assert validated.quiet_hours_enabled is True
    assert validated.quiet_hours_start == datetime.time(22, 0)
    assert validated.quiet_hours_end == datetime.time(8, 0)
    assert validated.enmad_expiry_date is None
    assert validated.notification_email is None


def test_lead_lists_are_deduplicated_and_sorted() -> None:
    validated = validate_preferences_payload(
        {"enabled": True, "cheque_lead_days": [3, 7, 3, 1]}
    )
    assert validated.cheque_lead_days == (1, 3, 7)


def test_enabled_is_required() -> None:
    with pytest.raises(ReminderValidationError) as exc_info:
        validate_preferences_payload({})
    assert "enabled" in exc_info.value.field_errors


def test_lead_days_out_of_range_rejected() -> None:
    with pytest.raises(ReminderValidationError) as exc_info:
        validate_preferences_payload({"enabled": True, "cheque_lead_days": [31]})
    assert "cheque_lead_days" in exc_info.value.field_errors
    with pytest.raises(ReminderValidationError) as exc_info:
        validate_preferences_payload({"enabled": True, "cheque_lead_days": [-1]})
    assert "cheque_lead_days" in exc_info.value.field_errors


def test_too_many_leads_rejected() -> None:
    with pytest.raises(ReminderValidationError):
        validate_preferences_payload(
            {"enabled": True, "cheque_lead_days": [1, 2, 3, 4, 5, 6]}
        )


def test_cashbox_threshold_bounds() -> None:
    with pytest.raises(ReminderValidationError):
        validate_preferences_payload({"enabled": True, "cashbox_uncounted_days": 0})
    with pytest.raises(ReminderValidationError):
        validate_preferences_payload({"enabled": True, "cashbox_uncounted_days": 91})
    assert (
        validate_preferences_payload({"enabled": True, "cashbox_uncounted_days": 30}).cashbox_uncounted_days
        == 30
    )


def test_quiet_hours_equal_bounds_rejected() -> None:
    with pytest.raises(ReminderValidationError) as exc_info:
        validate_preferences_payload(
            {
                "enabled": True,
                "quiet_hours_start": "08:00",
                "quiet_hours_end": "08:00",
            }
        )
    assert "quiet_hours_start" in exc_info.value.field_errors


def test_quiet_hours_bad_format_rejected() -> None:
    with pytest.raises(ReminderValidationError):
        validate_preferences_payload(
            {"enabled": True, "quiet_hours_start": "25:00"}
        )


def test_enmad_expiry_date_parsed() -> None:
    validated = validate_preferences_payload(
        {"enabled": True, "enmad_expiry_date": "2026-12-20"}
    )
    assert validated.enmad_expiry_date == datetime.date(2026, 12, 20)
    with pytest.raises(ReminderValidationError):
        validate_preferences_payload(
            {"enabled": True, "enmad_expiry_date": "20/12/2026"}
        )


def test_notification_email_validated() -> None:
    validated = validate_preferences_payload(
        {"enabled": True, "notification_email": "ops@example.com"}
    )
    assert validated.notification_email == "ops@example.com"
    with pytest.raises(ReminderValidationError):
        validate_preferences_payload(
            {"enabled": True, "notification_email": "not-an-email"}
        )


def test_all_field_errors_reported_at_once() -> None:
    with pytest.raises(ReminderValidationError) as exc_info:
        validate_preferences_payload(
            {
                "cheque_lead_days": [99],
                "cashbox_uncounted_days": 0,
                "notification_email": "bad",
            }
        )
    assert set(exc_info.value.field_errors) == {
        "enabled",
        "cheque_lead_days",
        "cashbox_uncounted_days",
        "notification_email",
    }
