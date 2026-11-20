"""Request/response schemas for the SMS endpoints (US-544).

Responses map from the domain objects — no ORM object ever leaks,
and credentials never appear in any response (only
``has_credentials``). The create/update bodies carry JSON types
only; the domain validation
(``hesabyar.domain.sms.validation``) owns every rule, so the
service and the REST layer can never disagree about what is valid.

SMS prices are informational Toman display values, user-entered —
no Toman→Rial ×10 conversion applies anywhere in this module.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from hesabyar.domain.sms.models import (
    SmsContractReminderSettings,
    SmsProvider,
    SmsReminderLogEntry,
    SmsReminderSettings,
)


def _clean_decimal(value: Decimal) -> Decimal:
    """Strip trailing zeros so the decimal-string form is clean.

    ``Decimal("720000000000.0000")`` becomes
    ``Decimal("720000000000")`` (wire form ``"720000000000"``);
    ``Decimal("9.5000")`` becomes ``Decimal("9.5")``.
    ``format(..., "f")`` avoids the scientific notation
    ``normalize()`` alone would produce for large values
    (``7.2E+11``). Same helper as the tax-policy schemas.
    """
    return Decimal(format(value.normalize(), "f"))


class CreateSmsProviderRequest(BaseModel):
    """Create a provider row (a custom provider, or a re-created built-in).

    New providers are born disabled with status ``setup`` unless
    the payload explicitly enables them. ``credentials`` (e.g.
    ``{"api_key": "..."}``) is encrypted at rest and never returned;
    the shape each provider kind requires is validated by the domain.
    """

    model_config = ConfigDict(extra="forbid")

    provider_key: str = Field(description="Unique slug within the installation, e.g. 'kavenegar'")
    display_name_fa: str = Field(description="Persian display name")
    provider_kind: str = Field(description="Integration kind: 'kavenegar', 'sms_ir', 'simulator' or 'custom'")
    enabled: bool = Field(default=False, description="Whether the provider may send")
    is_default: bool = Field(default=False, description="Whether this is the installation's default provider")
    status: str | None = Field(default=None, description="setup | active | disabled | error (default follows enabled)")
    line_number: str | None = Field(default=None, description="Sender line number (sms.ir: digits only)")
    price_per_sms_toman: Decimal | None = Field(
        default=None, description="Informational price per SMS in Toman (user-entered, never converted)"
    )
    rate_limit_per_minute: int | None = Field(default=None, description="User-entered rate limit")
    line_type: str | None = Field(default=None, description="User-entered line type, e.g. 'advertising' / 'service'")
    contract_required: bool = Field(default=False, description="Whether the provider requires a contract")
    enmad_required: bool = Field(default=False, description="Whether the provider requires an eNamad")
    notes: str | None = Field(default=None, description="Operator notes")
    credentials: dict[str, str] | None = Field(
        default=None, description="Provider credentials (api_key, ...); encrypted at rest, never returned"
    )

    def payload_fields(self) -> dict[str, object]:
        """The domain validation payload (None values dropped)."""
        payload: dict[str, object] = {
            "provider_key": self.provider_key,
            "display_name_fa": self.display_name_fa,
            "provider_kind": self.provider_kind,
            "enabled": self.enabled,
            "is_default": self.is_default,
            "contract_required": self.contract_required,
            "enmad_required": self.enmad_required,
        }
        if self.status is not None:
            payload["status"] = self.status
        if self.line_number is not None:
            payload["line_number"] = self.line_number
        if self.price_per_sms_toman is not None:
            payload["price_per_sms_toman"] = self.price_per_sms_toman
        if self.rate_limit_per_minute is not None:
            payload["rate_limit_per_minute"] = self.rate_limit_per_minute
        if self.line_type is not None:
            payload["line_type"] = self.line_type
        if self.notes is not None:
            payload["notes"] = self.notes
        if self.credentials is not None:
            payload["credentials"] = self.credentials
        return payload


class UpdateSmsProviderRequest(BaseModel):
    """Patch a provider row (only the supplied fields change).

    ``provider_key`` / ``provider_kind`` are immutable. Setting a
    clearable field (``line_number``, ``price_per_sms_toman``,
    ``rate_limit_per_minute``, ``line_type``, ``notes``) to null
    clears it. ``credentials`` rotates the stored payload.
    """

    model_config = ConfigDict(extra="forbid")

    display_name_fa: str | None = Field(default=None, description="Persian display name")
    enabled: bool | None = Field(default=None, description="Whether the provider may send")
    is_default: bool | None = Field(default=None, description="Whether this is the installation's default provider")
    status: str | None = Field(default=None, description="setup | active | disabled | error")
    line_number: str | None = Field(default=None, description="Sender line number (null clears)")
    price_per_sms_toman: Decimal | None = Field(
        default=None, description="Informational price per SMS in Toman (null clears)"
    )
    rate_limit_per_minute: int | None = Field(default=None, description="User-entered rate limit (null clears)")
    line_type: str | None = Field(default=None, description="User-entered line type (null clears)")
    contract_required: bool | None = Field(default=None, description="Whether the provider requires a contract")
    enmad_required: bool | None = Field(default=None, description="Whether the provider requires an eNamad")
    notes: str | None = Field(default=None, description="Operator notes (null clears)")
    credentials: dict[str, str] | None = Field(
        default=None, description="Provider credentials (api_key, ...); rotates the stored payload"
    )

    def payload_fields(self) -> dict[str, object]:
        """The domain validation payload (only explicitly-set fields)."""
        return {
            name: value
            for name, value in self.model_dump().items()
            if name in self.model_fields_set
        }


class SmsProviderResponse(BaseModel):
    """One provider row (credentials never included)."""

    id: str = Field(description="Provider row id")
    installation_id: str = Field(description="Installation this provider belongs to")
    provider_key: str = Field(description="Unique slug within the installation")
    display_name_fa: str = Field(description="Persian display name")
    provider_kind: str = Field(description="Integration kind")
    is_builtin: bool = Field(description="Whether this is a built-in provider")
    enabled: bool = Field(description="Whether the provider may send")
    is_default: bool = Field(description="Whether this is the installation's default provider")
    status: str = Field(description="setup | active | disabled | error")
    line_number: str | None = Field(description="Sender line number")
    price_per_sms_toman: Decimal | None = Field(description="Informational price per SMS in Toman")
    rate_limit_per_minute: int | None = Field(description="User-entered rate limit")
    line_type: str | None = Field(description="User-entered line type")
    contract_required: bool = Field(description="Whether the provider requires a contract")
    enmad_required: bool = Field(description="Whether the provider requires an eNamad")
    notes: str | None = Field(description="Operator notes")
    has_credentials: bool = Field(description="Whether credentials are stored (the values are never returned)")
    created_at: datetime | None = Field(description="Creation timestamp (UTC)")
    updated_at: datetime | None = Field(description="Last update timestamp (UTC)")

    @classmethod
    def from_domain(cls, provider: SmsProvider) -> SmsProviderResponse:
        return cls(
            id=provider.id,
            installation_id=provider.installation_id,
            provider_key=provider.provider_key,
            display_name_fa=provider.display_name_fa,
            provider_kind=provider.provider_kind,
            is_builtin=provider.is_builtin,
            enabled=provider.enabled,
            is_default=provider.is_default,
            status=provider.status.value,
            line_number=provider.line_number,
            price_per_sms_toman=(
                _clean_decimal(provider.price_per_sms_toman)
                if provider.price_per_sms_toman is not None
                else None
            ),
            rate_limit_per_minute=provider.rate_limit_per_minute,
            line_type=provider.line_type,
            contract_required=provider.contract_required,
            enmad_required=provider.enmad_required,
            notes=provider.notes,
            has_credentials=provider.has_credentials,
            created_at=provider.created_at,
            updated_at=provider.updated_at,
        )


class ReminderSettingsRequest(BaseModel):
    """Replace the installation's reminder policy (PUT — all fields required)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(description="Whether automatic reminders run for this installation")
    offsets_days_before: list[int] = Field(
        description="Reminder offsets before the due date (0..30; 0 = on the day)"
    )
    offsets_days_overdue: list[int] = Field(
        description="Reminder offsets after the due date (1..30)"
    )
    template_before_due: str = Field(
        description="Message template for due-soon reminders (placeholders: {amount_toman}, {due_date_jalali}, "
        "{days_remaining}, {invoice_id}, {leg_sequence})"
    )
    template_overdue: str = Field(
        description="Message template for overdue reminders (placeholders: {amount_toman}, {due_date_jalali}, "
        "{days_overdue}, {invoice_id}, {leg_sequence})"
    )

    def payload_fields(self) -> dict[str, object]:
        """The domain validation payload."""
        return {
            "enabled": self.enabled,
            "offsets_days_before": self.offsets_days_before,
            "offsets_days_overdue": self.offsets_days_overdue,
            "template_before_due": self.template_before_due,
            "template_overdue": self.template_overdue,
        }


class ReminderSettingsResponse(BaseModel):
    """The installation's reminder policy."""

    installation_id: str = Field(description="Installation this policy belongs to")
    tenant_id: str = Field(description="Tenant this policy belongs to")
    enabled: bool = Field(description="Whether automatic reminders run for this installation")
    offsets_days_before: list[int] = Field(description="Reminder offsets before the due date")
    offsets_days_overdue: list[int] = Field(description="Reminder offsets after the due date")
    template_before_due: str = Field(description="Message template for due-soon reminders")
    template_overdue: str = Field(description="Message template for overdue reminders")
    created_at: datetime | None = Field(description="Creation timestamp (UTC)")
    updated_at: datetime | None = Field(description="Last update timestamp (UTC)")

    @classmethod
    def from_domain(cls, settings: SmsReminderSettings) -> ReminderSettingsResponse:
        return cls(
            installation_id=settings.installation_id,
            tenant_id=settings.tenant_id,
            enabled=settings.enabled,
            offsets_days_before=list(settings.offsets_days_before),
            offsets_days_overdue=list(settings.offsets_days_overdue),
            template_before_due=settings.template_before_due,
            template_overdue=settings.template_overdue,
            created_at=settings.created_at,
            updated_at=settings.updated_at,
        )


class ContractReminderSettingsRequest(BaseModel):
    """Set a contract's reminder opt-in (automatic sends only)."""

    model_config = ConfigDict(extra="forbid")

    reminders_enabled: bool = Field(
        description="Whether the daily job may send reminders for this contract's plans"
    )

    def payload_fields(self) -> dict[str, object]:
        """The domain validation payload."""
        return {"reminders_enabled": self.reminders_enabled}


class ContractReminderSettingsResponse(BaseModel):
    """A contract's reminder opt-in."""

    id: str | None = Field(
        description="Opt-in row id (null when never configured — the opt-in is disabled)"
    )
    installation_id: str = Field(description="Installation this opt-in belongs to")
    contract_id: str = Field(description="Sale contract id")
    reminders_enabled: bool = Field(description="Whether the daily job may send reminders for this contract's plans")
    created_at: datetime | None = Field(description="Creation timestamp (UTC)")
    updated_at: datetime | None = Field(description="Last update timestamp (UTC)")

    @classmethod
    def default_for(cls, installation_id: str, contract_id: str) -> ContractReminderSettingsResponse:
        """The effective opt-in for a contract that was never configured (disabled)."""
        return cls(
            id=None,
            installation_id=installation_id,
            contract_id=contract_id,
            reminders_enabled=False,
            created_at=None,
            updated_at=None,
        )

    @classmethod
    def from_domain(
        cls, settings: SmsContractReminderSettings
    ) -> ContractReminderSettingsResponse:
        return cls(
            id=settings.id,
            installation_id=settings.installation_id,
            contract_id=settings.contract_id,
            reminders_enabled=settings.reminders_enabled,
            created_at=settings.created_at,
            updated_at=settings.updated_at,
        )


class TriggerReminderRequest(BaseModel):
    """Manually trigger a reminder for one leg of one plan.

    The manual trigger is itself the opt-in for this one send (an
    explicit operator action) — the contract opt-in gates AUTOMATIC
    sends only. ``leg_id`` omitted means "the earliest unpaid leg".
    A dry run renders the message, logs a ``dry_run`` row, and sends
    nothing.
    """

    model_config = ConfigDict(extra="forbid")

    leg_id: str | None = Field(default=None, description="Leg id (default: the earliest unpaid leg)")
    provider_id: str | None = Field(default=None, description="Provider id (default: the installation's default)")
    recipient_phone: str | None = Field(
        default=None, description="Recipient mobile override (default: the plan's customer phone)"
    )
    dry_run: bool = Field(default=False, description="Preview the message without sending")


class ReminderLogResponse(BaseModel):
    """One reminder log row (the DLQ + audit trail for reminders)."""

    id: str = Field(description="Log row id")
    tenant_id: str = Field(description="Tenant this reminder belongs to")
    installation_id: str = Field(description="Installation this reminder belongs to")
    plan_id: str = Field(description="Installment plan id")
    leg_id: str = Field(description="Installment leg id")
    provider_id: str | None = Field(description="Provider row id (null when the provider was deleted)")
    provider_key: str = Field(description="Provider key (denormalized — survives a provider delete)")
    recipient_phone: str = Field(description="Recipient mobile (canonical 09xxxxxxxxx)")
    message_text: str = Field(description="The message that was (or would be) sent")
    offset_days: int = Field(description="Offset from the due date: negative before, 0 on the day, positive overdue")
    trigger: str = Field(description="scheduled | manual")
    status: str = Field(description="queued | sent | delivered | failed | skipped | dry_run")
    provider_message_id: str | None = Field(description="Provider's message id (null for dry runs and skips)")
    provider_status_raw: str | None = Field(description="Provider's raw status payload (verbatim)")
    cost_toman: Decimal | None = Field(
        description="Cost in Toman when the provider's unit is known (null otherwise — never guessed)"
    )
    provider_cost_raw: str | None = Field(description="Provider's raw cost value (verbatim)")
    provider_cost_unit: str | None = Field(description="Unit of provider_cost_raw: 'IRR', 'unverified', or null")
    error_code: str | None = Field(description="Typed error code for failed rows")
    error_detail: str | None = Field(description="Human-readable failure detail (never contains credentials)")
    sent_at: datetime | None = Field(description="When the provider accepted the message (UTC)")
    created_at: datetime | None = Field(description="Creation timestamp (UTC)")

    @classmethod
    def from_domain(cls, entry: SmsReminderLogEntry) -> ReminderLogResponse:
        return cls(
            id=entry.id,
            tenant_id=entry.tenant_id,
            installation_id=entry.installation_id,
            plan_id=entry.plan_id,
            leg_id=entry.leg_id,
            provider_id=entry.provider_id,
            provider_key=entry.provider_key,
            recipient_phone=entry.recipient_phone,
            message_text=entry.message_text,
            offset_days=entry.offset_days,
            trigger=entry.trigger.value,
            status=entry.status.value,
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
