"""Reminder-preferences payload validation (US-564).

Pure functions: a bad payload is refused with a typed
:class:`ReminderValidationError` carrying one problem per field —
never a half-saved policy. The PUT is a full replace (like the
SMS reminder policy): every field is required except
``enmad_expiry_date`` and ``notification_email``, which are
nullable (absent = no eNamad reminders / no email recipient).

Bounds (chosen, not spec'd — the spec only says "user-configured"):
lead/offset lists hold 0–30 days (0 = the day itself), at most 5
entries each, deduplicated and sorted; the cash-box threshold is
1–90 days; quiet hours are HH:MM wall times with distinct bounds
(equal bounds would mean an empty window — refused, so "no quiet
hours" is expressed by ``quiet_hours_enabled: false`` instead).
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass

from hesabyar.domain.reminders.errors import ReminderValidationError
from hesabyar.domain.reminders.models import (
    DEFAULT_CASHBOX_UNCOUNTED_DAYS,
    DEFAULT_CHEQUE_LEAD_DAYS,
    DEFAULT_ENMAD_LEAD_DAYS,
    DEFAULT_INSTALLMENT_OVERDUE_DAYS,
    DEFAULT_QUIET_HOURS_END,
    DEFAULT_QUIET_HOURS_START,
)

#: Lead/offset days must stay in this range (0 = the day itself).
MAX_LEAD_DAYS = 30

#: At most this many leads per list (a longer list is a misconfiguration).
MAX_LEAD_ENTRIES = 5

#: Cash-box uncounted threshold bounds (days).
MIN_CASHBOX_DAYS = 1
MAX_CASHBOX_DAYS = 90

#: Quiet-hour bounds must be distinct (equal bounds = an empty window).
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass(frozen=True)
class ValidatedPreferences:
    """A preferences payload that passed validation (PUT semantics)."""

    enabled: bool
    cheque_due_soon_enabled: bool
    installment_overdue_enabled: bool
    cashbox_uncounted_enabled: bool
    enmad_expiry_enabled: bool
    channel_in_app: bool
    channel_email: bool
    channel_sms: bool
    cheque_lead_days: tuple[int, ...]
    installment_overdue_days: tuple[int, ...]
    cashbox_uncounted_days: int
    enmad_lead_days: tuple[int, ...]
    quiet_hours_enabled: bool
    quiet_hours_start: datetime.time
    quiet_hours_end: datetime.time
    enmad_expiry_date: datetime.date | None
    notification_email: str | None


def _require_lead_list(
    payload: dict[str, object],
    field_name: str,
    errors: dict[str, str],
    *,
    default: tuple[int, ...],
) -> tuple[int, ...]:
    value = payload.get(field_name, None)
    if value is None:
        return default
    if not isinstance(value, list):
        errors[field_name] = "must be a list of day counts."
        return default
    if len(value) > MAX_LEAD_ENTRIES:
        errors[field_name] = f"must have at most {MAX_LEAD_ENTRIES} entries."
        return default
    days: list[int] = []
    for item in value:
        if not isinstance(item, int) or isinstance(item, bool):
            errors[field_name] = "every entry must be an integer number of days."
            return default
        if item < 0 or item > MAX_LEAD_DAYS:
            errors[field_name] = f"every entry must be between 0 and {MAX_LEAD_DAYS}."
            return default
        days.append(item)
    return tuple(sorted(set(days)))


def _require_time(
    payload: dict[str, object],
    field_name: str,
    errors: dict[str, str],
    *,
    default: datetime.time,
) -> datetime.time:
    value = payload.get(field_name, None)
    if value is None:
        return default
    if not isinstance(value, str):
        errors[field_name] = "must be a HH:MM time string."
        return default
    try:
        parsed = datetime.time.fromisoformat(value.strip())
    except ValueError:
        errors[field_name] = "must be a HH:MM time string."
        return default
    return parsed.replace(second=0, microsecond=0)


def _optional_date(
    payload: dict[str, object], field_name: str, errors: dict[str, str]
) -> datetime.date | None:
    value = payload.get(field_name, None)
    if value is None:
        return None
    if not isinstance(value, str):
        errors[field_name] = "must be an ISO date string (YYYY-MM-DD) or null."
        return None
    try:
        return datetime.date.fromisoformat(value.strip())
    except ValueError:
        errors[field_name] = "must be an ISO date string (YYYY-MM-DD) or null."
        return None


def _optional_email(
    payload: dict[str, object], field_name: str, errors: dict[str, str]
) -> str | None:
    value = payload.get(field_name, None)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        errors[field_name] = "must be an email address or null."
        return None
    address = value.strip()
    if len(address) > 254 or not _EMAIL_RE.match(address):
        errors[field_name] = "must be a valid email address."
        return None
    return address


def validate_preferences_payload(
    payload: dict[str, object],
) -> ValidatedPreferences:
    """Validate a full-replace preferences payload (PUT semantics).

    ``enabled`` is required; the per-type and channel flags default
    to True when absent (a minimal payload enables everything and
    only tunes the leads); lead lists default to the model
    defaults; quiet hours default to 22:00–08:00. Unknown keys are
    ignored (the REST schema rejects them first).
    """
    errors: dict[str, str] = {}
    enabled = payload.get("enabled")
    if not isinstance(enabled, bool):
        errors["enabled"] = "must be a boolean."

    def _flag(field_name: str) -> bool:
        value = payload.get(field_name, True)
        if not isinstance(value, bool):
            errors[field_name] = "must be a boolean."
            return True
        return value

    cheque_enabled = _flag("cheque_due_soon_enabled")
    installment_enabled = _flag("installment_overdue_enabled")
    cashbox_enabled = _flag("cashbox_uncounted_enabled")
    enmad_enabled = _flag("enmad_expiry_enabled")
    channel_in_app = _flag("channel_in_app")
    channel_email = _flag("channel_email")
    channel_sms = _flag("channel_sms")
    quiet_enabled = _flag("quiet_hours_enabled")

    cheque_leads = _require_lead_list(
        payload, "cheque_lead_days", errors, default=DEFAULT_CHEQUE_LEAD_DAYS
    )
    installment_days = _require_lead_list(
        payload,
        "installment_overdue_days",
        errors,
        default=DEFAULT_INSTALLMENT_OVERDUE_DAYS,
    )
    enmad_leads = _require_lead_list(
        payload, "enmad_lead_days", errors, default=DEFAULT_ENMAD_LEAD_DAYS
    )

    cashbox_days = payload.get("cashbox_uncounted_days", DEFAULT_CASHBOX_UNCOUNTED_DAYS)
    if (
        not isinstance(cashbox_days, int)
        or isinstance(cashbox_days, bool)
        or cashbox_days < MIN_CASHBOX_DAYS
        or cashbox_days > MAX_CASHBOX_DAYS
    ):
        errors["cashbox_uncounted_days"] = (
            f"must be an integer between {MIN_CASHBOX_DAYS} and {MAX_CASHBOX_DAYS}."
        )
        cashbox_days = DEFAULT_CASHBOX_UNCOUNTED_DAYS

    quiet_start = _require_time(
        payload, "quiet_hours_start", errors, default=DEFAULT_QUIET_HOURS_START
    )
    quiet_end = _require_time(
        payload, "quiet_hours_end", errors, default=DEFAULT_QUIET_HOURS_END
    )
    if quiet_start == quiet_end:
        errors["quiet_hours_start"] = (
            "quiet-hours start and end must differ (use quiet_hours_enabled: false "
            "to disable quiet hours)."
        )

    enmad_expiry = _optional_date(payload, "enmad_expiry_date", errors)
    notification_email = _optional_email(payload, "notification_email", errors)

    if errors:
        raise ReminderValidationError(
            "The reminder preferences are invalid.", field_errors=errors
        )
    assert isinstance(enabled, bool)  # validated above
    assert isinstance(cashbox_days, int)
    return ValidatedPreferences(
        enabled=enabled,
        cheque_due_soon_enabled=cheque_enabled,
        installment_overdue_enabled=installment_enabled,
        cashbox_uncounted_enabled=cashbox_enabled,
        enmad_expiry_enabled=enmad_enabled,
        channel_in_app=channel_in_app,
        channel_email=channel_email,
        channel_sms=channel_sms,
        cheque_lead_days=cheque_leads,
        installment_overdue_days=installment_days,
        cashbox_uncounted_days=cashbox_days,
        enmad_lead_days=enmad_leads,
        quiet_hours_enabled=quiet_enabled,
        quiet_hours_start=quiet_start,
        quiet_hours_end=quiet_end,
        enmad_expiry_date=enmad_expiry,
        notification_email=notification_email,
    )
