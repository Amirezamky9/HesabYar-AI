"""Settlement-engine domain errors (US-521, Phase 5-3 Wave A).

Codes follow the ``SETTLEMENT_*`` namespace. The REST layer maps these
in ``interfaces.rest.errors``.
"""

from __future__ import annotations

from typing import Any

from hesabyar.domain.common.errors import DomainError


class SettlementError(DomainError):
    """Base exception for the settlement-engine bounded context."""


class SettlementPaymentNotFoundError(SettlementError):
    """Raised when a gateway-payment id does not exist for the installation."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="SETTLEMENT_PAYMENT_NOT_FOUND", details=details)


class SettlementPaymentExistsError(SettlementError):
    """Raised when a (channel, gateway_transaction_id) is recorded twice.

    The unique constraint on ``gateway_payments`` is the backstop; this
    error is the service-level signal (HTTP 409). Webhook + manual
    import of the same transaction converge on ONE row (phased design
    §7.1) — the import/matching stories (US-522/523) resolve duplicates
    by lookup instead of raising; direct recording raises.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="SETTLEMENT_PAYMENT_EXISTS", details=details)


class SettlementTrancheNotFoundError(SettlementError):
    """Raised when a settlement-tranche id does not exist for the installation."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="SETTLEMENT_TRANCHE_NOT_FOUND", details=details)


class SettlementInvalidTransitionError(SettlementError):
    """Raised when a tranche state transition is not in the §3.3 machine."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="SETTLEMENT_INVALID_TRANSITION", details=details)


class SettlementContractMissingError(SettlementError):
    """Raised when a channel has no ACTIVE contract version to derive from.

    Fail-closed: a payment can never be scheduled without a contract —
    the schedule is the contract's, and a channel with no contract
    cannot auto-post (phased design §3.1).
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="SETTLEMENT_CONTRACT_MISSING", details=details)


class SettlementFeeVatRateUnknownError(SettlementError):
    """Raised when no fee-VAT rate is available for a payment.

    The rate comes from the contract's ``fee_vat_rate`` snapshot (the
    operator-entered figure, from the active tax policy) or an explicit
    per-payment override. When neither exists the service fails closed
    instead of silently computing VAT as zero — a missing rate is a
    configuration gap, not a zero rate.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="SETTLEMENT_FEE_VAT_RATE_UNKNOWN", details=details)


class SettlementDerivationError(SettlementError):
    """Raised when a tranche schedule cannot be derived (bad legs, bad amounts)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="SETTLEMENT_DERIVATION_ERROR", details=details)


class SettlementPaymentInvalidError(SettlementError):
    """Raised when a gateway payment's inputs fail validation."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="SETTLEMENT_PAYMENT_INVALID", details=details)


class SettlementWriteOffNotAllowedError(SettlementError):
    """Raised when a tranche cannot be written off (wrong state / no approval)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="SETTLEMENT_WRITE_OFF_NOT_ALLOWED", details=details)


class SettlementExceptionNotFoundError(SettlementError):
    """Raised when a settlement-exception id does not exist for the installation."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="SETTLEMENT_EXCEPTION_NOT_FOUND", details=details)


class SettlementFileImportExistsError(SettlementError):
    """Raised when the same file (channel + sha256) is imported twice."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="SETTLEMENT_FILE_IMPORT_EXISTS", details=details)


class SettlementFileImportNotFoundError(SettlementError):
    """Raised when a settlement file-import id does not exist for the installation."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="SETTLEMENT_FILE_IMPORT_NOT_FOUND", details=details)


class SettlementPostingExistsError(SettlementError):
    """Raised when a posting already exists for a (payment/tranche, kind).

    Postings are idempotent by construction: a second attempt to post
    the same kind for the same payment/tranche is a conflict, never a
    duplicate journal.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="SETTLEMENT_POSTING_EXISTS", details=details)


class SettlementPostingPreconditionError(SettlementError):
    """Raised when a posting's preconditions fail (US-522).

    Wrong tranche state for the posting kind, a missing Tryton
    receivable line, a payment journal without clearing configuration,
    a zero amount — anything that must hold before the engine is
    touched. Fail-closed: nothing is posted.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="SETTLEMENT_POSTING_PRECONDITION", details=details)


class SettlementTieOutFailedError(SettlementError):
    """Raised when a bank receipt's mandatory tie-out does not hold (C6).

    Σ confirmed tranche nets − Σ documented deductions must equal
    Σ matched bank deposits within the contract's tolerance; an
    undocumented difference is not a deduction — the whole batch stays
    in exception and NO automatic posting happens.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="SETTLEMENT_TIE_OUT_FAILED", details=details)


class SettlementWiringUnresolvedError(SettlementError):
    """Raised when the mapping center cannot resolve a posting's wiring.

    A missing company/party/journal/account mapping fails closed into
    this error (listed per key), never to a default account.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="SETTLEMENT_WIRING_UNRESOLVED", details=details)


class SettlementReconciliationFailedError(SettlementError):
    """Raised when a tranche's receivable lines cannot be reconciled.

    The gathered lines do not sum to zero (a missing leg — e.g. the
    fee move was never posted — or a partial amount), so the service
    refuses to reconcile instead of leaving a half-reconciled set.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="SETTLEMENT_RECONCILIATION_FAILED", details=details)
class SettlementImportInvalidError(SettlementError):
    """Raised when a settlement file import's inputs fail validation."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="SETTLEMENT_IMPORT_INVALID", details=details)


class SettlementImportStateError(SettlementError):
    """Raised when an import is in the wrong state for an action (e.g. approving twice)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="SETTLEMENT_IMPORT_STATE", details=details)


class SettlementDepositNotFoundError(SettlementError):
    """Raised when a bank-deposit id does not exist for the installation."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="SETTLEMENT_DEPOSIT_NOT_FOUND", details=details)


class SettlementEventNotFoundError(SettlementError):
    """Raised when a settlement-event id does not exist for the installation."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="SETTLEMENT_EVENT_NOT_FOUND", details=details)


class SettlementMatchError(SettlementError):
    """Raised when a match cannot be applied (over-allocation, wrong state, ...)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="SETTLEMENT_MATCH_ERROR", details=details)


class SettlementPostingNotConfiguredError(SettlementError):
    """Raised when settlement posting has no engine implementation wired.

    Fail-closed: the tie-out service catches this, keeps the batch in
    exception and files a review item — a batch is never silently
    left unposted and never silently posted. US-522 wires the engine
    implementation of :class:`SettlementPostingPort`.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="SETTLEMENT_POSTING_NOT_CONFIGURED", details=details)


class SettlementPostingError(SettlementError):
    """Raised when the engine posting of a bank receipt fails."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="SETTLEMENT_POSTING_ERROR", details=details)
