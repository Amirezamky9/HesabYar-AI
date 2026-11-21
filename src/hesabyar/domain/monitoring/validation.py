"""Channel + token-record payload validation (US-704).

Pure functions: a bad payload is refused with a typed
:class:`MonitoringValidationError` carrying one problem per field —
never a half-saved channel. The service validates BEFORE it touches
the repository, so a 422 leaves no row behind.

Bounds (chosen, not spec'd):
- channel name 1–128 chars; target per kind (SMS = Iranian mobile,
  11 digits starting 09, Persian/Arabic digits normalized; email =
  a simple ``local@domain.tld`` shape; webhook = an https URL —
  http is refused: alert payloads carry operational detail and
  must not cross a network in cleartext);
- an SMS channel needs a ``provider_key`` (a registry key such as
  ``kavenegar`` / ``sms_ir`` / ``simulator`` — the provider itself
  is looked up at dispatch, so a typo'd key fails closed then, not
  here);
- a token record's ``expires_at`` must be a future-or-present
  datetime (a record for an already-expired token is pointless —
  the alert would fire critical immediately; the operator records
  the NEW token's expiry after rotating).
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from hesabyar.domain.common.dates import normalize_digits
from hesabyar.domain.monitoring.errors import MonitoringValidationError
from hesabyar.domain.monitoring.models import (
    AlertChannelKind,
    AlertSeverity,
    TokenKind,
)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MOBILE_RE = re.compile(r"^09\d{9}$")

MAX_NAME_LENGTH = 128
MAX_LABEL_LENGTH = 128
MAX_NOTES_LENGTH = 1024


@dataclass(frozen=True)
class ValidatedChannel:
    """A channel payload that passed validation."""

    kind: AlertChannelKind
    name: str
    target: str
    enabled: bool
    min_severity: AlertSeverity
    provider_key: str | None
    installation_id: str | None


@dataclass(frozen=True)
class ValidatedTokenRecord:
    """A token-record payload that passed validation."""

    kind: TokenKind
    label: str
    expires_at: datetime.datetime
    rotation_runbook_ref: str | None
    enabled: bool
    notes: str | None


def _str_field(
    payload: dict[str, object],
    field_name: str,
    errors: dict[str, str],
    *,
    required: bool = True,
    max_length: int | None = None,
) -> str | None:
    value = payload.get(field_name)
    if value is None:
        if required:
            errors[field_name] = "این فیلد الزامی است."
        return None
    if not isinstance(value, str) or not value.strip():
        errors[field_name] = "باید یک رشتهٔ غیرخالی باشد."
        return None
    cleaned = value.strip()
    if max_length is not None and len(cleaned) > max_length:
        errors[field_name] = f"حداکثر {max_length} نویسه مجاز است."
        return None
    return cleaned


def _bool_field(
    payload: dict[str, object],
    field_name: str,
    errors: dict[str, str],
    *,
    default: bool,
) -> bool:
    value = payload.get(field_name, default)
    if not isinstance(value, bool):
        errors[field_name] = "باید true یا false باشد."
        return default
    return value


def validate_channel_payload(
    payload: dict[str, object],
) -> ValidatedChannel:
    """Validate a create-channel payload (all fields required except flags)."""
    errors: dict[str, str] = {}
    kind_raw = _str_field(payload, "kind", errors)
    kind: AlertChannelKind | None = None
    if kind_raw is not None:
        try:
            kind = AlertChannelKind(kind_raw)
        except ValueError:
            errors["kind"] = "نوع کانال معتبر نیست (sms، email یا webhook)."
    name = _str_field(payload, "name", errors, max_length=MAX_NAME_LENGTH)
    target_raw = _str_field(payload, "target", errors)
    target: str | None = None
    if target_raw is not None and kind is not None:
        target = _validate_target(kind, target_raw, errors)
    severity_raw = payload.get("min_severity", AlertSeverity.WARNING.value)
    severity = AlertSeverity.WARNING
    if isinstance(severity_raw, str):
        try:
            severity = AlertSeverity(severity_raw)
        except ValueError:
            errors["min_severity"] = "سطح هشدار معتبر نیست (info، warning یا critical)."
    else:
        errors["min_severity"] = "باید یک رشته باشد."
    enabled = _bool_field(payload, "enabled", errors, default=True)
    provider_key = _str_field(payload, "provider_key", errors, required=False)
    installation_id = _str_field(payload, "installation_id", errors, required=False)
    if kind is AlertChannelKind.SMS and not provider_key:
        errors["provider_key"] = "کانال پیامکی به کلید ارائه‌دهنده نیاز دارد."
    if errors:
        raise MonitoringValidationError("Channel payload failed validation.", field_errors=errors)
    assert kind is not None and name is not None and target is not None
    return ValidatedChannel(
        kind=kind,
        name=name,
        target=target,
        enabled=enabled,
        min_severity=severity,
        provider_key=provider_key,
        installation_id=installation_id,
    )


def _validate_target(kind: AlertChannelKind, target: str, errors: dict[str, str]) -> str | None:
    if kind is AlertChannelKind.SMS:
        digits = normalize_digits(target).replace(" ", "").replace("-", "")
        if not _MOBILE_RE.match(digits):
            errors["target"] = "شماره موبایل باید ۱۱ رقم باشد و با ۰۹ شروع شود."
            return None
        return digits
    if kind is AlertChannelKind.EMAIL:
        if not _EMAIL_RE.match(target):
            errors["target"] = "نشانی ایمیل معتبر نیست."
            return None
        return target
    # WEBHOOK: https only, with a host.
    parsed = urlparse(target)
    if parsed.scheme != "https" or not parsed.netloc:
        errors["target"] = "نشانی وب‌هوک باید یک URL با https باشد."
        return None
    return target


def validate_token_payload(
    payload: dict[str, object],
    *,
    now: datetime.datetime,
) -> ValidatedTokenRecord:
    """Validate a create-token-record payload."""
    errors: dict[str, str] = {}
    kind_raw = _str_field(payload, "kind", errors)
    kind: TokenKind | None = None
    if kind_raw is not None:
        try:
            kind = TokenKind(kind_raw)
        except ValueError:
            errors["kind"] = "نوع توکن معتبر نیست (fintech یا other)."
    label = _str_field(payload, "label", errors, max_length=MAX_LABEL_LENGTH)
    expires_at: datetime.datetime | None = None
    raw_expires = payload.get("expires_at")
    if raw_expires is None:
        errors["expires_at"] = "این فیلد الزامی است."
    elif isinstance(raw_expires, datetime.datetime):
        expires_at = raw_expires
    elif isinstance(raw_expires, str):
        try:
            expires_at = datetime.datetime.fromisoformat(raw_expires)
        except ValueError:
            errors["expires_at"] = "تاریخ انقضا باید ISO 8601 باشد."
    else:
        errors["expires_at"] = "تاریخ انقضا باید ISO 8601 باشد."
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=datetime.UTC)
        if expires_at < now:
            errors["expires_at"] = "تاریخ انقضا نمی‌تواند در گذشته باشد."
    runbook = _str_field(payload, "rotation_runbook_ref", errors, required=False)
    notes = _str_field(payload, "notes", errors, required=False, max_length=MAX_NOTES_LENGTH)
    enabled = _bool_field(payload, "enabled", errors, default=True)
    if errors:
        raise MonitoringValidationError("Token record payload failed validation.", field_errors=errors)
    assert kind is not None and label is not None and expires_at is not None
    return ValidatedTokenRecord(
        kind=kind,
        label=label,
        expires_at=expires_at,
        rotation_runbook_ref=runbook,
        enabled=enabled,
        notes=notes,
    )
