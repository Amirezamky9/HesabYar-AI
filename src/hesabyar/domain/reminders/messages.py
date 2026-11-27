"""Reminder message text (US-564).

Fixed Persian text, built by pure functions — NOT user-editable
templates (unlike US-544's SMS templates). The eNamad warning in
particular must not be editable away: it states a compliance
fact (expiry disables gateway/BNPL service, phased design §8.3),
and the acceptance criteria require the text to say so plainly.

Rendering rules (the US-544 rules, reused):
- amounts and day counts render in Persian digits;
- dates render as Jalali ``YYYY/MM/DD`` in Persian digits;
- identifiers (sayad id, invoice id) render verbatim — their
  digits must stay exact.
"""

from __future__ import annotations

from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.sms.templates import to_persian_digits


def _jalali(value: FiscalDate) -> str:
    return to_persian_digits(value.to_jalali_str(delimiter="/"))


def cheque_due_soon_message(
    *,
    amount_toman: int,
    due_date: FiscalDate,
    days_remaining: int,
    sayad_id: str,
    issuer_name: str,
    kind: str,
) -> str:
    """The cheque due-soon reminder (customer-facing for received cheques).

    ``kind`` is ``received`` (the issuer is the customer — remind
    them to fund the cheque) or ``issued`` (the operator's own
    cheque — remind the operator to fund it). The two readings
    differ in voice, not in facts.
    """
    days = to_persian_digits(days_remaining)
    amount = to_persian_digits(amount_toman)
    due = _jalali(due_date)
    if kind == "received":
        return (
            f"یادآوری سررسید چک: چک صیادی {sayad_id} به مبلغ {amount} تومان "
            f"با سررسید {due}، {days} روز دیگر سررسید می‌شود. "
            f"صادرکننده: {issuer_name}. لطفاً موجودی حساب را تأمین فرمایید."
        )
    return (
        f"یادآوری سررسید چک صادره: چک صیادی {sayad_id} به مبلغ {amount} تومان "
        f"با سررسید {due}، {days} روز دیگر سررسید می‌شود. "
        f"گیرنده: {issuer_name}. لطفاً موجودی حساب را تأمین فرمایید."
    )


def installment_overdue_message(
    *,
    amount_toman: int,
    due_date: FiscalDate,
    days_overdue: int,
    invoice_id: str,
    leg_sequence: int,
) -> str:
    """The overdue-installment reminder (the remaining balance, not the principal)."""
    return (
        f"قسط شما به مبلغ {to_persian_digits(amount_toman)} تومان "
        f"با سررسید {_jalali(due_date)}، "
        f"{to_persian_digits(days_overdue)} روز معوق است. "
        f"فاکتور {invoice_id}، قسط {to_persian_digits(leg_sequence)}. "
        "لطفاً پرداخت فرمایید."
    )


def cashbox_uncounted_message(
    *,
    box_name: str,
    days_uncounted: int,
    last_count_jalali: str | None,
) -> str:
    """The uncounted-cashbox reminder (operator-facing, in-app + email only)."""
    last = (
        f"آخرین شمارش: {last_count_jalali}."
        if last_count_jalali is not None
        else "این صندوق تاکنون شمارش نشده است."
    )
    return (
        f"یادآوری شمارش صندوق: صندوق «{box_name}» "
        f"{to_persian_digits(days_uncounted)} روز است شمارش نشده است. {last} "
        "لطفاً شمارش صندوق را انجام دهید."
    )


def enmad_expiry_message(
    *,
    expiry_date: FiscalDate,
    days_remaining: int,
) -> str:
    """The eNamad-expiry warning (operator-facing, in-app + email only).

    The text states the §8.3 rule plainly: when the eNamad
    expires, gateway and BNPL service is DISABLED. At lead 0
    (the expiry day) the warning is in the present tense — the
    service is being disabled today.
    """
    expiry = _jalali(expiry_date)
    if days_remaining == 0:
        return (
            "هشدار: نماد اعتماد الکترونیکی (اینماد) شما امروز "
            f"({expiry}) منقضی می‌شود. پس از انقضا، سرویس درگاه پرداخت "
            "و BNPL غیرفعال می‌شود. لطفاً فوراً تمدید کنید."
        )
    return (
        "هشدار: نماد اعتماد الکترونیکی (اینماد) شما "
        f"{to_persian_digits(days_remaining)} روز دیگر "
        f"({expiry}) منقضی می‌شود. پس از انقضا، سرویس درگاه پرداخت "
        "و BNPL غیرفعال می‌شود. لطفاً پیش از انقضا تمدید کنید."
    )
