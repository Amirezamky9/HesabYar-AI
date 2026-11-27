"""SMS payload validation + Iranian mobile normalization (US-544).

Pure functions — no I/O. The application service calls these
before any repository write, so a bad payload is refused
fail-closed with field-level problems (typed 422) and nothing is
stored.

Phone numbers: the canonical form is the 11-digit Iranian mobile
``09xxxxxxxxx``. Inputs in the common written forms
(``+989…``, ``00989…``, ``989…``, Persian/Arabic digits, spaces)
normalize to it; anything else is not a mobile and is refused —
never silently stored in a form a provider would reject.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from hesabyar.domain.sms.credentials import REQUIRED_CREDENTIAL_FIELDS
from hesabyar.domain.sms.errors import SmsValidationError
from hesabyar.domain.sms.models import PROVIDER_KINDS, SmsProviderStatus
from hesabyar.domain.sms.templates import validate_template

_PERSIAN_ARABIC_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

#: Provider keys are URL-safe slugs (the same rule as channel keys).
_PROVIDER_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

#: Canonical Iranian mobile: 11 digits starting with 09.
_MOBILE_RE = re.compile(r"^09\d{9}$")

#: Max length for free-text registry fields.
_MAX_TEXT = 256
_MAX_NOTES = 2000
_MAX_TEMPLATE = 2000

#: Offset bounds (days): before-due 0..30, overdue 1..30.
_MAX_OFFSET_DAYS = 30


def normalize_digits(value: str) -> str:
    """Normalize Persian and Arabic-Indic digits to ASCII digits."""
    return value.translate(_PERSIAN_ARABIC_DIGITS)


def normalize_iranian_mobile(raw: str | None) -> str | None:
    """Normalize an Iranian mobile number to canonical ``09xxxxxxxxx``.

    Accepts ``09…`` (11 digits), ``+989…`` / ``00989…`` /
    ``989…`` (international forms), Persian/Arabic digits, and
    surrounding whitespace. Returns None when the value is not a
    plausible Iranian mobile — the caller decides whether that is
    an error (manual trigger) or a skip (the daily job).
    """
    if raw is None:
        return None
    cleaned = normalize_digits(raw).strip().replace(" ", "").replace("-", "")
    if cleaned.startswith("+"):
        cleaned = cleaned[1:]
    if cleaned.startswith("0098"):
        cleaned = "0" + cleaned[4:]
    elif cleaned.startswith("98") and len(cleaned) == 12:
        cleaned = "0" + cleaned[2:]
    if _MOBILE_RE.match(cleaned):
        return cleaned
    return None


def _require_non_empty(raw: object, field_name: str, errors: dict[str, str]) -> str | None:
    if not isinstance(raw, str) or not raw.strip():
        errors[field_name] = "must be a non-empty string."
        return None
    return raw.strip()


def _optional_text(
    raw: object, field_name: str, errors: dict[str, str], *, max_length: int
) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        errors[field_name] = "must be a string."
        return None
    value = raw.strip()
    if len(value) > max_length:
        errors[field_name] = f"must be at most {max_length} characters."
        return None
    return value or None


def _optional_decimal(
    raw: object, field_name: str, errors: dict[str, str]
) -> Decimal | None:
    if raw is None:
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        errors[field_name] = "must be a decimal number."
        return None
    if not value.is_finite():
        errors[field_name] = "must be a finite number."
        return None
    if value < 0:
        errors[field_name] = "must be >= 0."
        return None
    return value


def _optional_int(
    raw: object, field_name: str, errors: dict[str, str], *, minimum: int
) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        errors[field_name] = "must be an integer."
        return None
    if raw < minimum:
        errors[field_name] = f"must be >= {minimum}."
        return None
    return raw


def _optional_bool(raw: object, field_name: str, errors: dict[str, str]) -> bool | None:
    if raw is None:
        return None
    if not isinstance(raw, bool):
        errors[field_name] = "must be a boolean."
        return None
    return raw


@dataclass(frozen=True)
class ValidatedProvider:
    """A validated provider payload (create or patch)."""

    provider_key: str | None = None
    display_name_fa: str | None = None
    provider_kind: str | None = None
    enabled: bool | None = None
    is_default: bool | None = None
    status: SmsProviderStatus | None = None
    line_number: str | None = None
    price_per_sms_toman: Decimal | None = None
    rate_limit_per_minute: int | None = None
    line_type: str | None = None
    contract_required: bool | None = None
    enmad_required: bool | None = None
    notes: str | None = None
    credentials: dict[str, str] | None = None


def _validate_credentials(
    raw: object, provider_kind: str | None, errors: dict[str, str]
) -> dict[str, str] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or not raw:
        errors["credentials"] = "must be a non-empty object of string fields."
        return None
    cleaned: dict[str, str] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not name.strip():
            errors["credentials"] = "credential field names must be non-empty strings."
            return None
        if not isinstance(value, str) or not value.strip():
            errors["credentials"] = f"credential field {name!r} must be a non-empty string."
            return None
        cleaned[name.strip()] = value
    if provider_kind is not None:
        required = REQUIRED_CREDENTIAL_FIELDS.get(provider_kind, ())
        missing = [name for name in required if name not in cleaned]
        if missing:
            errors["credentials"] = (
                f"missing required credential field(s) for {provider_kind!r}: "
                + ", ".join(sorted(missing))
                + "."
            )
            return None
    return cleaned


def validate_provider_payload(
    payload: dict[str, object],
    *,
    partial: bool,
    provider_kind: str | None = None,
) -> ValidatedProvider:
    """Validate a provider create (``partial=False``) or patch payload.

    Create requires ``provider_key`` + ``display_name_fa`` +
    ``provider_kind``; a patch validates only the fields present.
    A supplied ``credentials`` object must satisfy the kind's
    required fields (field NAMES travel in errors, never values);
    on a patch the caller passes the stored kind via
    ``provider_kind`` (the kind itself is immutable). An sms.ir
    provider's line number must be all digits (the API takes an
    int64 line number); other kinds keep a free-form line.
    """
    errors: dict[str, str] = {}
    key = _require_non_empty(payload.get("provider_key"), "provider_key", errors) if not partial else None
    if not partial and key is not None and not _PROVIDER_KEY_RE.match(key):
        errors["provider_key"] = (
            "must be a slug: lowercase letters, digits, '-' or '_', starting with a letter or digit."
        )
    display = (
        _require_non_empty(payload.get("display_name_fa"), "display_name_fa", errors)
        if not partial or "display_name_fa" in payload
        else None
    )
    if display is not None and len(display) > _MAX_TEXT:
        errors["display_name_fa"] = f"must be at most {_MAX_TEXT} characters."
    kind: str | None = provider_kind
    if not partial or "provider_kind" in payload:
        kind = _require_non_empty(payload.get("provider_kind"), "provider_kind", errors)
        if kind is not None and kind not in PROVIDER_KINDS:
            errors["provider_kind"] = f"must be one of: {', '.join(sorted(PROVIDER_KINDS))}."
    status: SmsProviderStatus | None = None
    if "status" in payload and payload["status"] is not None:
        raw_status = payload["status"]
        if not isinstance(raw_status, str):
            errors["status"] = "must be a string."
        else:
            try:
                status = SmsProviderStatus(raw_status)
            except ValueError:
                errors["status"] = (
                    "must be one of: " + ", ".join(s.value for s in SmsProviderStatus) + "."
                )
    line = (
        _optional_text(payload.get("line_number"), "line_number", errors, max_length=32)
        if "line_number" in payload
        else None
    )
    if line is not None and kind == "sms_ir" and not line.isdigit():
        errors["line_number"] = "must be all digits for an sms.ir provider (the API takes an int64 line number)."
    price = (
        _optional_decimal(payload.get("price_per_sms_toman"), "price_per_sms_toman", errors)
        if "price_per_sms_toman" in payload
        else None
    )
    rate_limit = (
        _optional_int(payload.get("rate_limit_per_minute"), "rate_limit_per_minute", errors, minimum=1)
        if "rate_limit_per_minute" in payload
        else None
    )
    line_type = (
        _optional_text(payload.get("line_type"), "line_type", errors, max_length=32)
        if "line_type" in payload
        else None
    )
    enabled = _optional_bool(payload.get("enabled"), "enabled", errors) if "enabled" in payload else None
    is_default = (
        _optional_bool(payload.get("is_default"), "is_default", errors)
        if "is_default" in payload
        else None
    )
    contract_required = (
        _optional_bool(payload.get("contract_required"), "contract_required", errors)
        if "contract_required" in payload
        else None
    )
    enmad_required = (
        _optional_bool(payload.get("enmad_required"), "enmad_required", errors)
        if "enmad_required" in payload
        else None
    )
    notes = (
        _optional_text(payload.get("notes"), "notes", errors, max_length=_MAX_NOTES)
        if "notes" in payload
        else None
    )
    credentials = (
        _validate_credentials(payload.get("credentials"), kind, errors)
        if "credentials" in payload
        else None
    )
    if errors:
        raise SmsValidationError("The SMS provider payload is invalid.", field_errors=errors)
    return ValidatedProvider(
        provider_key=key,
        display_name_fa=display,
        provider_kind=kind,
        enabled=enabled,
        is_default=is_default,
        status=status,
        line_number=line,
        price_per_sms_toman=price,
        rate_limit_per_minute=rate_limit,
        line_type=line_type,
        contract_required=contract_required,
        enmad_required=enmad_required,
        notes=notes,
        credentials=credentials,
    )


@dataclass(frozen=True)
class ValidatedReminderSettings:
    """A validated reminder-settings payload (PUT replaces the whole row)."""

    enabled: bool
    offsets_days_before: tuple[int, ...]
    offsets_days_overdue: tuple[int, ...]
    template_before_due: str
    template_overdue: str


def _validate_offsets(
    raw: object, field_name: str, *, minimum: int, errors: dict[str, str]
) -> tuple[int, ...]:
    if not isinstance(raw, list):
        errors[field_name] = "must be a list of integers."
        return ()
    values: list[int] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, int):
            errors[field_name] = "must be a list of integers."
            return ()
        if item < minimum or item > _MAX_OFFSET_DAYS:
            errors[field_name] = (
                f"each offset must be in {minimum}..{_MAX_OFFSET_DAYS}."
            )
            return ()
        values.append(item)
    if len(set(values)) != len(values):
        errors[field_name] = "must not contain duplicates."
        return ()
    return tuple(sorted(values))


def _validate_template_field(
    raw: object, field_name: str, errors: dict[str, str]
) -> str | None:
    text = _require_non_empty(raw, field_name, errors)
    if text is None:
        return None
    if len(text) > _MAX_TEMPLATE:
        errors[field_name] = f"must be at most {_MAX_TEMPLATE} characters."
        return None
    problems = validate_template(text)
    if problems:
        errors[field_name] = "; ".join(problems)
        return None
    return text


def validate_reminder_settings_payload(payload: dict[str, object]) -> ValidatedReminderSettings:
    """Validate a reminder-settings PUT payload (all fields required)."""
    errors: dict[str, str] = {}
    enabled = payload.get("enabled")
    if not isinstance(enabled, bool):
        errors["enabled"] = "must be a boolean."
        enabled = False
    before = _validate_offsets(
        payload.get("offsets_days_before"), "offsets_days_before", minimum=0, errors=errors
    )
    overdue = _validate_offsets(
        payload.get("offsets_days_overdue"), "offsets_days_overdue", minimum=1, errors=errors
    )
    if not before and not overdue and "offsets_days_before" not in errors and "offsets_days_overdue" not in errors:
        errors["offsets_days_before"] = (
            "at least one reminder offset is required across offsets_days_before / offsets_days_overdue."
        )
    template_before = _validate_template_field(
        payload.get("template_before_due"), "template_before_due", errors
    )
    template_overdue = _validate_template_field(
        payload.get("template_overdue"), "template_overdue", errors
    )
    if errors:
        raise SmsValidationError("The SMS reminder settings are invalid.", field_errors=errors)
    assert template_before is not None and template_overdue is not None
    return ValidatedReminderSettings(
        enabled=enabled,
        offsets_days_before=before,
        offsets_days_overdue=overdue,
        template_before_due=template_before,
        template_overdue=template_overdue,
    )
