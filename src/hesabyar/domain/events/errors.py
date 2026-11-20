"""Domain errors for the canonical event platform (spec §10, US-501).

Stable codes follow the ``EVENT_*`` / ``IDEMPOTENCY_*`` taxonomy; the
REST layer maps them onto typed problem-details responses (see
``hesabyar.interfaces.rest.errors``).
"""

from __future__ import annotations

from typing import Any

from hesabyar.domain.common.errors import DomainError


class EventError(DomainError):
    """Base exception for event-platform domain failures."""


class InvalidEventEnvelopeError(EventError):
    """Raised when a canonical event envelope fails validation."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="EVENT_INVALID_ENVELOPE", details=details)


class UnsupportedSchemaVersionError(EventError):
    """Raised when an event carries a schema version this build cannot read."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="EVENT_UNSUPPORTED_SCHEMA_VERSION", details=details)


class UnknownEventTypeError(EventError):
    """Raised when an event type is not one of the nine canonical types (§10.2).

    New event names beyond the nine need ADR approval (ADR 0003 round-2
    update 8) — they are rejected, never silently accepted.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="EVENT_UNKNOWN_TYPE", details=details)


class PayloadHashMismatchError(EventError):
    """Raised when a supplied payload_hash does not match the payload."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="EVENT_PAYLOAD_HASH_MISMATCH", details=details)


class EventTypePendingAdrError(EventError):
    """Raised when a store webhook's canonical target needs ADR approval.

    The mapping is defined (see ``STORE_WEBHOOK_MAPPINGS``) but the
    target event name is not one of the nine canonical types of
    §10.2 yet, so the webhook cannot be ingested — it is rejected,
    never stored under an unapproved name (US-511).
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="EVENT_TYPE_PENDING_ADR_APPROVAL", details=details)


class WebhookVaultUnavailableError(EventError):
    """Raised when a store's webhook secret cannot be decrypted.

    The store HAS a secret configured but the credential vault is
    unavailable (no key configured) or the ciphertext cannot be
    decrypted — verification is impossible, so the ingest fails
    closed instead of silently skipping the signature check (US-511).
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="EVENT_WEBHOOK_VAULT_UNAVAILABLE", details=details)


class IdempotencyConflictError(EventError):
    """Raised when a key is reused with a different payload hash (§10.4).

    Never silently treated as the same operation: the caller gets a
    409 Conflict and must resolve the mismatch.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="IDEMPOTENCY_CONFLICT", details=details)


class MappingUnresolvedError(EventError):
    """Raised when a mapping the event needs cannot be resolved (§10.5).

    Fail-closed: the pipeline routes the event to the review queue
    instead of falling back to an arbitrary account.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="EVENT_MAPPING_UNRESOLVED", details=details)


class InvalidStoreAmountError(EventError):
    """Raised when a store amount cannot be converted to whole Rial.

    Fractional Rial input (from a store that claims Rial prices) is a
    data error, never silently rounded (spec §19 rule 7).
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="EVENT_INVALID_AMOUNT", details=details)


class EventSignatureError(EventError):
    """Raised when a webhook signature fails verification (§1.3)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="EVENT_SIGNATURE_INVALID", details=details)


class EventNotFoundError(EventError):
    """Raised when a referenced inbox event does not exist (or belongs to another tenant)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="INBOX_EVENT_NOT_FOUND", details=details)


class EventNotDeadLetteredError(EventError):
    """Raised when DLQ replay is requested for an event that is not dead-lettered."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="EVENT_NOT_DEAD_LETTERED", details=details)


class EventNotReprocessableError(EventError):
    """Raised when reprocessing is requested for an event in the wrong state.

    Only a dead-lettered or needs-review event can be reprocessed
    (US-552): a processed event is already done, and a
    received/processing event is already in flight — reprocessing
    either would be a second effect, not a retry.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="EVENT_NOT_REPROCESSABLE", details=details)


# ---------------------------------------------------------------------------
# Store reconciliation & backfill (US-512, S2)
# ---------------------------------------------------------------------------


class InvalidSyncCursorError(EventError):
    """Raised when a polling cursor cannot be decoded (malformed or wrong version)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="SYNC_INVALID_CURSOR", details=details)


class InvalidSyncRequestError(EventError):
    """Raised when a sync request is invalid (page size, backfill window, change row)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="SYNC_INVALID_REQUEST", details=details)


class StoreNotConfiguredError(EventError):
    """Raised when a store lacks the configuration a sync source needs.

    HTTP polling needs ``api_base_url`` + the scoped read-only API key;
    DB backfill needs the read-only DSN stored for the store.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="STORE_NOT_CONFIGURED", details=details)


class StoreUnreachableError(EventError):
    """Raised when the store's changes API cannot be reached (transport/timeout/5xx)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="STORE_UNREACHABLE", details=details)


class StoreSyncFailedError(EventError):
    """Raised when a sync fails for a reason that is not a plain outage.

    Malformed store responses, rows that violate the documented sort
    contract, or a connector returning another store's events all land
    here — the run stops, nothing is silently accepted.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="STORE_SYNC_FAILED", details=details)


class StoreDatabaseError(EventError):
    """Raised when the read-only store-DB read fails (connect/query)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="STORE_DB_ERROR", details=details)


# ---------------------------------------------------------------------------
# Dual-entry manual order/invoice/refund (US-513, S3)
# ---------------------------------------------------------------------------


class ManualEntryValidationError(EventError):
    """Raised when a manual entry fails validation (US-513).

    The operator typed something the pipeline cannot accept — a
    missing order number, an empty line list, a non-whole Toman
    total, a discount larger than the subtotal. The ``details``
    name the field, so the UI can point at it.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="MANUAL_ENTRY_INVALID", details=details)


class ManualEntryNotFoundError(EventError):
    """Raised when a referenced manual entry does not exist (or belongs to another tenant)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="MANUAL_ENTRY_NOT_FOUND", details=details)
