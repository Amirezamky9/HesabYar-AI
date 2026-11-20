"""Reminder message templates (US-544).

Persian message text with ``{placeholders}``, rendered with
Persian digits and Jalali due dates. The template is user-editable
per installation (stored on ``SmsReminderSettings``) — never
hard-coded marketing copy. The placeholder set is CLOSED: an
unknown placeholder is a validation error at save time, never a
surprise at send time.

Rendering rules:
- amounts and day counts render in Persian digits (۱۲٬۵۰۰ stays
  ungrouped — grouping separators inside an SMS are the operator's
  choice, so the renderer only transliterates digits);
- the due date renders as Jalali ``YYYY/MM/DD`` in Persian digits
  (e.g. ``۱۴۰۵/۰۹/۰۱``);
- ``invoice_id`` renders verbatim (it is an identifier, not a
  number — its digits must stay exact).
"""

from __future__ import annotations

import string
from dataclasses import dataclass

from hesabyar.domain.common.dates import FiscalDate

_ASCII_DIGITS = "0123456789"
_PERSIAN_DIGITS = "۰۱۲۳۴۵۶۷۸۹"
_TO_PERSIAN = str.maketrans(_ASCII_DIGITS, _PERSIAN_DIGITS)

#: The closed placeholder vocabulary for reminder templates.
TEMPLATE_PLACEHOLDERS: frozenset[str] = frozenset(
    {
        "amount_toman",
        "due_date_jalali",
        "days_remaining",
        "days_overdue",
        "invoice_id",
        "leg_sequence",
    }
)


def to_persian_digits(value: str | int) -> str:
    """Transliterate ASCII digits to Persian digits (other chars unchanged)."""
    return str(value).translate(_TO_PERSIAN)


@dataclass(frozen=True)
class ReminderTemplateContext:
    """Everything a reminder template may reference.

    ``amount_toman`` is the leg's REMAINING balance in Toman
    (dunning continues on the balance, not the original principal).
    ``days_remaining`` is >= 0 for a before-due reminder (0 = due
    today); ``days_overdue`` is > 0 for an overdue reminder. Both
    are always present so either template may use either value.
    """

    amount_toman: int
    due_date: FiscalDate
    days_remaining: int
    days_overdue: int
    invoice_id: str
    leg_sequence: int

    def as_mapping(self) -> dict[str, str]:
        """Render every placeholder to its Persian display form."""
        return {
            "amount_toman": to_persian_digits(self.amount_toman),
            "due_date_jalali": to_persian_digits(self.due_date.to_jalali_str(delimiter="/")),
            "days_remaining": to_persian_digits(self.days_remaining),
            "days_overdue": to_persian_digits(self.days_overdue),
            "invoice_id": self.invoice_id,
            "leg_sequence": to_persian_digits(self.leg_sequence),
        }


def validate_template(template: str) -> list[str]:
    """Validate one template, returning one problem per defect.

    Pure and total: an empty list means the template is usable.
    Unknown placeholders and unbalanced braces are defects — the
    template is refused at save time, never at send time.
    """
    problems: list[str] = []
    try:
        fields = [
            field_name
            for _, field_name, _, _ in string.Formatter().parse(template)
            if field_name is not None
        ]
    except ValueError as exc:
        return [f"unbalanced braces in template: {exc}"]
    for field_name in fields:
        # Only bare names are supported (no attribute/index access).
        if field_name not in TEMPLATE_PLACEHOLDERS:
            problems.append(
                f"unknown placeholder {{{field_name}}}; allowed: "
                + ", ".join("{" + name + "}" for name in sorted(TEMPLATE_PLACEHOLDERS))
                + "."
            )
    return problems


def render_reminder_template(template: str, context: ReminderTemplateContext) -> str:
    """Render a validated template with its context.

    Raises ``KeyError`` only for a template that was never
    validated (a programming error — the service validates at save
    time, so a stored template always renders).
    """
    return template.format_map(context.as_mapping())
