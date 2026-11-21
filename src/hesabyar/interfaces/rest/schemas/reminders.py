"""Request/response schemas for the reminder endpoints (US-564).

Responses map from the domain objects — no ORM object ever leaks.
The PUT body carries JSON types only; the domain validation
(``hesabyar.domain.reminders.validation``) owns every rule, so the
service and the REST layer can never disagree about what is valid.

SMS costs are informational Toman display values — no Toman→Rial
×10 conversion applies anywhere in this module (the US-544 rule).
"""

from __future__ import annotations

from datetime import date as _date
from datetime import datetime
from datetime import time as _time
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from hesabyar.domain.reminders.models import ReminderLogEntry, ReminderPreferences


def _clean_decimal(value: Decimal) -> Decimal:
    """Strip trailing zeros so the decimal-string form is clean.

    Same helper as the SMS + tax-policy schemas: ``format(...,
    "f")`` avoids the scientific notation ``normalize()`` alone
    would produce for large values.
    """
    return Decimal(format(value.normalize(), "f"))


class ReminderPreferencesRequest(BaseModel):
    """Replace the installation's reminder policy (PUT).

    Only ``enabled`` is required; every other field defaults when
    absent (all types + channels on, default leads, quiet hours
    22:00–08:00). ``enmad_expiry_date`` / ``notification_email``
    are nullable (absent = no eNamad reminders / no email
    recipient).
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(description="Whether automatic reminders run for this installation")
    cheque_due_soon_enabled: bool | None = Field(
        default=None, description="Cheque due-soon reminders on (default: true)"
    )
    installment_overdue_enabled: bool | None = Field(
        default=None, description="Overdue-installment reminders on (default: true)"
    )
    cashbox_uncounted_enabled: bool | None = Field(
        default=None, description="Uncounted-cashbox reminders on (default: true)"
    )
    enmad_expiry_enabled: bool | None = Field(
        default=None, description="eNamad-expiry reminders on (default: true)"
    )
    channel_in_app: bool | None = Field(
        default=None, description="In-app channel (review-queue item) on (default: true)"
    )
    channel_email: bool | None = Field(
        default=None, description="Email channel on (default: true; no email provider exists yet)"
    )
    channel_sms: bool | None = Field(
        default=None, description="SMS channel on (default: true; via the US-544 registry)"
    )
    cheque_lead_days: list[int] | None = Field(
        default=None,
        description="Cheque due-soon leads in days before the due date (default: [7, 3])",
    )
    installment_overdue_days: list[int] | None = Field(
        default=None,
        description="Installment-overdue offsets in days past the due date (default: [1, 7])",
    )
    cashbox_uncounted_days: int | None = Field(
        default=None,
        description="Uncounted-cashbox threshold in days (default: 7)",
    )
    enmad_lead_days: list[int] | None = Field(
        default=None,
        description="eNamad-expiry leads in days before expiry; 0 = the expiry day (default: [30, 7, 0])",
    )
    quiet_hours_enabled: bool | None = Field(
        default=None, description="Quiet hours on (default: true)"
    )
    quiet_hours_start: str | None = Field(
        default=None, description="Quiet-hours start, HH:MM Tehran wall time (default: 22:00)"
    )
    quiet_hours_end: str | None = Field(
        default=None, description="Quiet-hours end, HH:MM Tehran wall time (default: 08:00)"
    )
    enmad_expiry_date: _date | None = Field(
        default=None, description="The store's eNamad expiry date (null = no eNamad reminders)"
    )
    notification_email: str | None = Field(
        default=None, description="Operator address for email reminders (null = no email recipient)"
    )

    def payload_fields(self) -> dict[str, object]:
        """The domain validation payload (absent fields omitted → defaults)."""
        payload: dict[str, object] = {"enabled": self.enabled}
        for field_name in (
            "cheque_due_soon_enabled",
            "installment_overdue_enabled",
            "cashbox_uncounted_enabled",
            "enmad_expiry_enabled",
            "channel_in_app",
            "channel_email",
            "channel_sms",
            "cheque_lead_days",
            "installment_overdue_days",
            "cashbox_uncounted_days",
            "enmad_lead_days",
            "quiet_hours_enabled",
            "quiet_hours_start",
            "quiet_hours_end",
            "enmad_expiry_date",
            "notification_email",
        ):
            value = getattr(self, field_name)
            if value is not None:
                if isinstance(value, _date) and not isinstance(value, datetime):
                    payload[field_name] = value.isoformat()
                else:
                    payload[field_name] = value
        return payload


class ReminderPreferencesResponse(BaseModel):
    """The installation's reminder policy."""

    installation_id: str = Field(description="Installation this policy belongs to")
    tenant_id: str = Field(description="Tenant this policy belongs to")
    enabled: bool = Field(description="Whether automatic reminders run for this installation")
    cheque_due_soon_enabled: bool = Field(description="Cheque due-soon reminders on")
    installment_overdue_enabled: bool = Field(description="Overdue-installment reminders on")
    cashbox_uncounted_enabled: bool = Field(description="Uncounted-cashbox reminders on")
    enmad_expiry_enabled: bool = Field(description="eNamad-expiry reminders on")
    channel_in_app: bool = Field(description="In-app channel on")
    channel_email: bool = Field(description="Email channel on")
    channel_sms: bool = Field(description="SMS channel on")
    cheque_lead_days: list[int] = Field(description="Cheque due-soon leads in days before the due date")
    installment_overdue_days: list[int] = Field(description="Installment-overdue offsets in days past the due date")
    cashbox_uncounted_days: int = Field(description="Uncounted-cashbox threshold in days")
    enmad_lead_days: list[int] = Field(description="eNamad-expiry leads in days before expiry")
    quiet_hours_enabled: bool = Field(description="Quiet hours on")
    quiet_hours_start: _time = Field(description="Quiet-hours start (Tehran wall time)")
    quiet_hours_end: _time = Field(description="Quiet-hours end (Tehran wall time)")
    enmad_expiry_date: _date | None = Field(description="The store's eNamad expiry date")
    notification_email: str | None = Field(description="Operator address for email reminders")
    created_at: datetime | None = Field(description="Creation timestamp (UTC)")
    updated_at: datetime | None = Field(description="Last update timestamp (UTC)")

    @classmethod
    def from_domain(cls, prefs: ReminderPreferences) -> ReminderPreferencesResponse:
        return cls(
            installation_id=prefs.installation_id,
            tenant_id=prefs.tenant_id,
            enabled=prefs.enabled,
            cheque_due_soon_enabled=prefs.cheque_due_soon_enabled,
            installment_overdue_enabled=prefs.installment_overdue_enabled,
            cashbox_uncounted_enabled=prefs.cashbox_uncounted_enabled,
            enmad_expiry_enabled=prefs.enmad_expiry_enabled,
            channel_in_app=prefs.channel_in_app,
            channel_email=prefs.channel_email,
            channel_sms=prefs.channel_sms,
            cheque_lead_days=list(prefs.cheque_lead_days),
            installment_overdue_days=list(prefs.installment_overdue_days),
            cashbox_uncounted_days=prefs.cashbox_uncounted_days,
            enmad_lead_days=list(prefs.enmad_lead_days),
            quiet_hours_enabled=prefs.quiet_hours_enabled,
            quiet_hours_start=prefs.quiet_hours_start,
            quiet_hours_end=prefs.quiet_hours_end,
            enmad_expiry_date=prefs.enmad_expiry_date,
            notification_email=prefs.notification_email,
            created_at=prefs.created_at,
            updated_at=prefs.updated_at,
        )


class ReminderLogResponse(BaseModel):
    """One reminder log row (the DLQ + audit trail for reminders)."""

    id: str = Field(description="Log row id")
    tenant_id: str = Field(description="Tenant this reminder belongs to")
    installation_id: str = Field(description="Installation this reminder belongs to")
    reminder_type: str = Field(
        description="cheque_due_soon | installment_overdue | cashbox_uncounted | enmad_expiry"
    )
    subject_type: str = Field(description="cheque | installment_leg | cashbox | installation")
    subject_id: str = Field(description="Id of the subject this reminder is about")
    lead_days: int = Field(
        description="Lead/offset that fired (days before due, days overdue, or the cash-box N threshold)"
    )
    channel: str = Field(description="in_app | email | sms")
    status: str = Field(description="queued | sent | delivered | failed | skipped")
    idempotency_key: str = Field(description="Dedup key: {type}:{subject_id}:{event_ref}:{channel}")
    message_text: str = Field(description="The message that was (or would be) sent")
    recipient: str | None = Field(description="Where it went: mobile for SMS, email for email, null for in-app")
    provider_id: str | None = Field(description="SMS provider row id (null when the provider was deleted)")
    provider_key: str | None = Field(description="SMS provider key (denormalized — survives a provider delete)")
    provider_message_id: str | None = Field(description="Provider's message id (null for non-SMS rows and skips)")
    provider_status_raw: str | None = Field(description="Provider's raw status payload (verbatim)")
    cost_toman: Decimal | None = Field(
        description="SMS cost in Toman when the provider's unit is known (null otherwise — never guessed)"
    )
    provider_cost_raw: str | None = Field(description="Provider's raw cost value (verbatim)")
    provider_cost_unit: str | None = Field(description="Unit of provider_cost_raw: 'IRR', 'unverified', or null")
    error_code: str | None = Field(description="Typed error code for failed/skipped rows")
    error_detail: str | None = Field(description="Human-readable failure detail (never contains credentials)")
    sent_at: datetime | None = Field(description="When the reminder went out (UTC)")
    created_at: datetime | None = Field(description="Creation timestamp (UTC)")

    @classmethod
    def from_domain(cls, entry: ReminderLogEntry) -> ReminderLogResponse:
        return cls(
            id=entry.id,
            tenant_id=entry.tenant_id,
            installation_id=entry.installation_id,
            reminder_type=entry.reminder_type.value,
            subject_type=entry.subject_type,
            subject_id=entry.subject_id,
            lead_days=entry.lead_days,
            channel=entry.channel.value,
            status=entry.status.value,
            idempotency_key=entry.idempotency_key,
            message_text=entry.message_text,
            recipient=entry.recipient,
            provider_id=entry.provider_id,
            provider_key=entry.provider_key,
            provider_message_id=entry.provider_message_id,
            provider_status_raw=entry.provider_status_raw,
            cost_toman=(
                _clean_decimal(entry.cost_toman) if entry.cost_toman is not None else None
            ),
            provider_cost_raw=entry.provider_cost_raw,
            provider_cost_unit=entry.provider_cost_unit,
            error_code=entry.error_code,
            error_detail=entry.error_detail,
            sent_at=entry.sent_at,
            created_at=entry.created_at,
        )
