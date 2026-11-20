"""SMS domain errors (US-544, Phase 5-5 ۵ج).

Every error carries a stable machine-readable ``code`` so the REST
layer can map it to a problem-details response without string
matching, and a ``details`` mapping for the values a caller or an
operator needs to recover (never credential material, never a
phone number beyond what the caller supplied).

Fail-closed by design: a provider without credentials, a disabled
provider, a leg awaiting cheque clearing, and a plan without a
usable phone number are all typed refusals — never a silent
non-send, never a live call down a path that cannot succeed.
"""

from __future__ import annotations

from hesabyar.domain.common.errors import InvariantViolationError


class SmsError(InvariantViolationError):
    """Base class for SMS-domain failures."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message, code=code)
        self.details: dict[str, object] = details or {}


class SmsValidationError(SmsError):
    """A provider / settings / template payload failed validation (fail-closed).

    ``details["field_errors"]`` maps field name -> human-readable
    problem; the REST layer renders it as a 422.
    """

    def __init__(self, message: str, *, field_errors: dict[str, str]) -> None:
        super().__init__(
            message, code="SMS_VALIDATION_FAILED", details={"field_errors": field_errors}
        )
        self.field_errors = field_errors


class SmsProviderNotFoundError(SmsError):
    """No SMS provider with the requested id/key exists in this installation."""

    def __init__(self, provider_ref: str) -> None:
        super().__init__(
            f"SMS provider {provider_ref!r} does not exist.",
            code="SMS_PROVIDER_NOT_FOUND",
            details={"provider_ref": provider_ref},
        )


class SmsProviderKeyConflictError(SmsError):
    """A provider with this key already exists in this installation."""

    def __init__(self, provider_key: str) -> None:
        super().__init__(
            f"An SMS provider with key {provider_key!r} already exists in this installation.",
            code="SMS_PROVIDER_KEY_CONFLICT",
            details={"provider_key": provider_key},
        )


class SmsProviderInUseError(SmsError):
    """The provider cannot be deleted: it is the installation's default."""

    def __init__(self, provider_key: str, *, reason: str) -> None:
        super().__init__(
            f"SMS provider {provider_key!r} cannot be deleted: {reason}",
            code="SMS_PROVIDER_IN_USE",
            details={"provider_key": provider_key, "reason": reason},
        )


class SmsProviderNotConfiguredError(SmsError):
    """The provider has no usable credentials (or no sender line) — fail closed.

    A live provider call is never attempted without credentials
    (US-544 acceptance criterion 3): the send is refused with this
    typed error, logged as failed, and filed in the review queue.
    """

    def __init__(self, provider_key: str, *, reason: str) -> None:
        super().__init__(
            f"SMS provider {provider_key!r} is not configured: {reason}",
            code="SMS_PROVIDER_NOT_CONFIGURED",
            details={"provider_key": provider_key, "reason": reason},
        )


class SmsProviderNotEnabledError(SmsError):
    """The provider exists but is disabled — reminders never send through it."""

    def __init__(self, provider_key: str) -> None:
        super().__init__(
            f"SMS provider {provider_key!r} is disabled; enable it before sending.",
            code="SMS_PROVIDER_NOT_ENABLED",
            details={"provider_key": provider_key},
        )


class SmsProviderClientUnavailableError(SmsError):
    """No SMS client exists for this provider kind (custom providers).

    A custom registry row is data, not a live integration: until a
    client is built for its kind, sending through it is refused
    (501), never attempted against an unknown API shape.
    """

    def __init__(self, provider_kind: str) -> None:
        super().__init__(
            f"No SMS client is available for provider kind {provider_kind!r}.",
            code="SMS_PROVIDER_CLIENT_UNAVAILABLE",
            details={"provider_kind": provider_kind},
        )


class SmsProviderUnreachableError(SmsError):
    """The provider's API could not be reached (transport/timeout/5xx)."""

    def __init__(self, provider_key: str, *, reason: str) -> None:
        super().__init__(
            f"SMS provider {provider_key!r} could not be reached: {reason}",
            code="SMS_PROVIDER_UNREACHABLE",
            details={"provider_key": provider_key, "reason": reason},
        )


class SmsProviderAuthError(SmsError):
    """The provider rejected the stored credentials (HTTP 401/403 or its own auth code)."""

    def __init__(self, provider_key: str, *, reason: str) -> None:
        super().__init__(
            f"SMS provider {provider_key!r} rejected the stored credentials: {reason}",
            code="SMS_PROVIDER_AUTH_FAILED",
            details={"provider_key": provider_key, "reason": reason},
        )


class SmsProviderRejectedError(SmsError):
    """The provider refused the send (bad number, bad sender, no credit, ...)."""

    def __init__(self, provider_key: str, *, reason: str) -> None:
        super().__init__(
            f"SMS provider {provider_key!r} rejected the send: {reason}",
            code="SMS_PROVIDER_REJECTED",
            details={"provider_key": provider_key, "reason": reason},
        )


class SmsProviderInvalidResponseError(SmsError):
    """The provider answered with an unparseable or off-contract payload."""

    def __init__(self, provider_key: str, *, reason: str) -> None:
        super().__init__(
            f"SMS provider {provider_key!r} returned an invalid response: {reason}",
            code="SMS_PROVIDER_INVALID_RESPONSE",
            details={"provider_key": provider_key, "reason": reason},
        )


class SmsVaultUnavailableError(SmsError):
    """No credential-encryption key is configured, so secrets cannot be stored."""

    def __init__(self) -> None:
        super().__init__(
            "Cannot store SMS credentials: no credential-encryption key is configured "
            "(HESABYAR_AUTH__CREDENTIALS_ENCRYPTION_KEY).",
            code="SMS_CREDENTIAL_VAULT_UNAVAILABLE",
        )


class SmsNoProviderAvailableError(SmsError):
    """No enabled default provider exists to send through."""

    def __init__(self) -> None:
        super().__init__(
            "No enabled default SMS provider exists in this installation; "
            "enable a provider (or the simulator) and mark it default.",
            code="SMS_NO_PROVIDER_AVAILABLE",
        )


class SmsReminderNotOptedInError(SmsError):
    """The plan's contract has not opted in to automatic SMS reminders."""

    def __init__(self, *, contract_id: str | None, plan_id: str) -> None:
        super().__init__(
            "Automatic SMS reminders are not opted in for this plan's contract.",
            code="SMS_REMINDER_NOT_OPTED_IN",
            details={"contract_id": contract_id, "plan_id": plan_id},
        )


class SmsReminderAlreadySentError(SmsError):
    """A reminder for this leg + offset was already sent (dedup)."""

    def __init__(self, *, leg_id: str, offset_days: int) -> None:
        super().__init__(
            f"A reminder for leg {leg_id!r} at offset {offset_days} days was already sent.",
            code="SMS_REMINDER_ALREADY_SENT",
            details={"leg_id": leg_id, "offset_days": offset_days},
        )


class SmsRecipientPhoneMissingError(SmsError):
    """No usable Iranian mobile number exists for this plan's customer."""

    def __init__(self, *, plan_id: str) -> None:
        super().__init__(
            "No usable mobile number is on file for this plan's customer: "
            "pass recipient_phone or fix the plan's customer reference.",
            code="SMS_RECIPIENT_PHONE_MISSING",
            details={"plan_id": plan_id},
        )


class SmsLegNotFoundError(SmsError):
    """The named leg does not belong to this plan."""

    def __init__(self, *, leg_id: str, plan_id: str) -> None:
        super().__init__(
            f"Installment leg {leg_id!r} does not belong to plan {plan_id!r}.",
            code="SMS_LEG_NOT_FOUND",
            details={"leg_id": leg_id, "plan_id": plan_id},
        )


class SmsLegNotRemindableError(SmsError):
    """The leg cannot be reminded (paid, or awaiting cheque clearing)."""

    def __init__(self, *, leg_id: str, reason: str) -> None:
        super().__init__(
            f"Installment leg {leg_id!r} cannot be reminded: {reason}",
            code="SMS_LEG_NOT_REMINDABLE",
            details={"leg_id": leg_id, "reason": reason},
        )


class SmsNoUnpaidLegError(SmsError):
    """The plan has no unpaid leg to remind about."""

    def __init__(self, *, plan_id: str) -> None:
        super().__init__(
            f"Installment plan {plan_id!r} has no unpaid leg to remind about.",
            code="SMS_NO_UNPAID_LEG",
            details={"plan_id": plan_id},
        )
