"""Application-layer errors for HesabYar use cases.

The application layer depends only on the domain, so it cannot import the
Tryton adapter's error types. Engine failures therefore arrive through the
port as :class:`hesabyar.domain.common.errors.HesabYarError` with a stable
``ENGINE_*`` code (the adapter taxonomy, documented in ``docs/adapter.md``),
and :func:`translate_engine_error` maps them onto application error types
with the same stable codes. No engine type, traceback or internal detail
ever escapes a use case.

Domain ledger errors (``LEDGER_*`` codes, e.g. ``AccountNotFoundError``)
are already engine-neutral and pass through unchanged.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from hesabyar.domain.common.errors import HesabYarError

# Stable application error codes. The ENGINE_* codes mirror the adapter
# taxonomy (docs/adapter.md) so a failure keeps one code from the engine
# call up to the REST/MCP response.
COMPANY_NOT_FOUND = "COMPANY_NOT_FOUND"
STANDARD_NOT_FOUND = "STANDARD_NOT_FOUND"
STANDARD_SOURCE_BLOCKED = "STANDARD_SOURCE_BLOCKED"
VALIDATION_RUN_NOT_FOUND = "VALIDATION_RUN_NOT_FOUND"
REPORT_VALIDATION_RUN_NOT_FOUND = "REPORT_VALIDATION_RUN_NOT_FOUND"
TRIAL_BALANCE_UNBALANCED = "TRIAL_BALANCE_UNBALANCED"
TAX_SOURCE_NOT_FOUND = "TAX_SOURCE_NOT_FOUND"
TAX_POLICY_NOT_FOUND = "TAX_POLICY_NOT_FOUND"
TAX_POLICY_SOURCE_BLOCKED = "TAX_POLICY_SOURCE_BLOCKED"
TAX_DECISION_NOT_FOUND = "TAX_DECISION_NOT_FOUND"
MOADIAN_PROFILE_NOT_FOUND = "MOADIAN_PROFILE_NOT_FOUND"
MOADIAN_DOCUMENT_NOT_FOUND = "MOADIAN_DOCUMENT_NOT_FOUND"
MOADIAN_SUBMISSION_NOT_FOUND = "MOADIAN_SUBMISSION_NOT_FOUND"
MOADIAN_INVALID_STATE = "MOADIAN_INVALID_STATE"
MOADIAN_INVALID_DOCUMENT = "MOADIAN_INVALID_DOCUMENT"
MOADIAN_PRODUCTION_NOT_CONNECTED = "MOADIAN_PRODUCTION_NOT_CONNECTED"
MOADIAN_PROFILE_SLUG_CONFLICT = "MOADIAN_PROFILE_SLUG_CONFLICT"
MOADIAN_DUPLICATE_TAXID = "MOADIAN_DUPLICATE_TAXID"
INTEGRATION_CHANNEL_NOT_FOUND = "INTEGRATION_CHANNEL_NOT_FOUND"
INTEGRATION_CHANNEL_KEY_CONFLICT = "INTEGRATION_CHANNEL_KEY_CONFLICT"
INTEGRATION_CHANNEL_IN_USE = "INTEGRATION_CHANNEL_IN_USE"
INTEGRATION_CHANNEL_RESERVED = "INTEGRATION_CHANNEL_RESERVED"
INTEGRATION_CONTRACT_NOT_FOUND = "INTEGRATION_CONTRACT_NOT_FOUND"
INTEGRATION_CONTRACT_INVALID = "INTEGRATION_CONTRACT_INVALID"
INTEGRATION_MAPPING_NOT_FOUND = "INTEGRATION_MAPPING_NOT_FOUND"
INTEGRATION_MAPPING_INVALID = "INTEGRATION_MAPPING_INVALID"
INTEGRATION_MAPPING_SUGGESTION_NOT_FOUND = "INTEGRATION_MAPPING_SUGGESTION_NOT_FOUND"
INTEGRATION_MAPPING_SUGGESTION_RESOLVED = "INTEGRATION_MAPPING_SUGGESTION_RESOLVED"
INTEGRATION_BANK_CONNECTION_NOT_FOUND = "INTEGRATION_BANK_CONNECTION_NOT_FOUND"
INTEGRATION_BANK_CONNECTION_CONFLICT = "INTEGRATION_BANK_CONNECTION_CONFLICT"
INTEGRATION_CREDENTIALS_FORBIDDEN = "INTEGRATION_CREDENTIALS_FORBIDDEN"
INTEGRATION_CREDENTIAL_VAULT_UNAVAILABLE = "INTEGRATION_CREDENTIAL_VAULT_UNAVAILABLE"
INTEGRATION_STORE_NOT_FOUND = "INTEGRATION_STORE_NOT_FOUND"
INTEGRATION_STORE_KEY_CONFLICT = "INTEGRATION_STORE_KEY_CONFLICT"
INTEGRATION_HOLIDAY_NOT_FOUND = "INTEGRATION_HOLIDAY_NOT_FOUND"
INTEGRATION_SETTINGS_INVALID = "INTEGRATION_SETTINGS_INVALID"
REVIEW_ITEM_NOT_FOUND = "REVIEW_ITEM_NOT_FOUND"
REVIEW_ACTION_NOT_PERMITTED = "REVIEW_ACTION_NOT_PERMITTED"
REVIEW_INVALID_STATE = "REVIEW_INVALID_STATE"
REVIEW_INVALID_SLA_POLICY = "REVIEW_INVALID_SLA_POLICY"
CASHBOX_NOT_FOUND = "CASHBOX_NOT_FOUND"
CASHBOX_JOURNAL_CONFLICT = "CASHBOX_JOURNAL_CONFLICT"
CASHBOX_INVALID = "CASHBOX_INVALID"
CASHBOX_SETTINGS_INVALID = "CASHBOX_SETTINGS_INVALID"
COUNT_SESSION_NOT_FOUND = "COUNT_SESSION_NOT_FOUND"
COUNT_SESSION_INVALID_STATE = "COUNT_SESSION_INVALID_STATE"
COUNT_SESSION_ALREADY_OPEN = "COUNT_SESSION_ALREADY_OPEN"
COUNT_SUPERVISOR_MUST_DIFFER = "COUNT_SUPERVISOR_MUST_DIFFER"
COUNT_MINUTES_REQUIRED = "COUNT_MINUTES_REQUIRED"
COUNT_REVIEW_NOT_APPROVED = "COUNT_REVIEW_NOT_APPROVED"
COUNT_DENOMINATIONS_MISMATCH = "COUNT_DENOMINATIONS_MISMATCH"
COUNT_RECOUNT_NOT_REQUESTED = "COUNT_RECOUNT_NOT_REQUESTED"
COUNT_WRONG_ACTOR = "COUNT_WRONG_ACTOR"
ENGINE_UNAVAILABLE = "ENGINE_UNAVAILABLE"
ENGINE_AUTHENTICATION_FAILED = "ENGINE_AUTHENTICATION_FAILED"
ENGINE_FORBIDDEN = "ENGINE_FORBIDDEN"
ENGINE_NOT_FOUND = "ENGINE_NOT_FOUND"
ENGINE_VALIDATION_FAILED = "ENGINE_VALIDATION_FAILED"
ENGINE_ERROR = "ENGINE_ERROR"
NOT_IMPLEMENTED = "NOT_IMPLEMENTED"


class ApplicationError(HesabYarError):
    """Base exception for application-layer (use case) failures."""

    def __init__(
        self,
        message: str,
        code: str | None = None,
        details: dict[str, Any] | None = None,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(message, code=code, details=details)
        self.retryable = retryable


class CompanyNotFoundError(ApplicationError):
    """Raised when a referenced company does not exist in the engine."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=COMPANY_NOT_FOUND, details=details)


class StandardNotFoundError(ApplicationError):
    """Raised when a referenced accounting standard is not in the registry."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=STANDARD_NOT_FOUND, details=details)


class StandardSourceBlockedError(ApplicationError):
    """Raised when a validation would use a BLOCKED_SOURCE_GAP revision.

    Per docs/CURRENT_RULES_1405.md §2, the bundled Standard 15 text is
    the pre-1404 revision and must never validate (or post) for periods
    beginning 1405/01/01 or later until the official revised-1404 text
    is imported and VERIFIED. Without a period the gate also blocks:
    applicability cannot be established, so the run fails closed.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=STANDARD_SOURCE_BLOCKED, details=details)


class ValidationRunNotFoundError(ApplicationError):
    """Raised when a validation run does not exist (or belongs to another tenant)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=VALIDATION_RUN_NOT_FOUND, details=details)


class ReportValidationRunNotFoundError(ApplicationError):
    """Raised when a report validation run has no snapshot (or belongs to another tenant).

    The validation run itself may exist in ``validation_runs`` (created
    by the plain standards endpoint), but without a trial-balance
    snapshot it is not a report validation run — it answers 404 as if
    it did not exist.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=REPORT_VALIDATION_RUN_NOT_FOUND, details=details)


class UnbalancedTrialBalanceError(ApplicationError):
    """Raised when the engine's posted moves do not balance.

    Posted moves are balanced by the engine, so an unbalanced trial
    balance means the engine data is corrupt (or the read was partial)
    — no validation run is ever built on such figures.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=TRIAL_BALANCE_UNBALANCED, details=details)


class TaxSourceNotFoundError(ApplicationError):
    """Raised when a referenced tax source is not in the registry."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=TAX_SOURCE_NOT_FOUND, details=details)


class TaxPolicyNotFoundError(ApplicationError):
    """Raised when no active tax-policy version resolves for a rule/date."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=TAX_POLICY_NOT_FOUND, details=details)


class TaxPolicySourceBlockedError(ApplicationError):
    """Raised when a production decision would use an unverified version.

    Per docs/CURRENT_RULES_1405.md §3 and §12, a version that is not
    VERIFIED_TEXT — CORROBORATED_CURRENT, UNVERIFIED, DISCOVERY_ONLY,
    BLOCKED_SOURCE_GAP or TRANSITIONAL — must never drive production
    use until the official source snapshot is pinned (immutable
    snapshot stored, SHA-256 recorded) and the version is promoted to
    VERIFIED. The message names the missing official source pin.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=TAX_POLICY_SOURCE_BLOCKED, details=details)


class TaxDecisionNotFoundError(ApplicationError):
    """Raised when a tax decision does not exist (or belongs to another tenant)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=TAX_DECISION_NOT_FOUND, details=details)


class MoadianProfileNotFoundError(ApplicationError):
    """Raised when a Moadian profile does not exist (or belongs to another tenant)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=MOADIAN_PROFILE_NOT_FOUND, details=details)


class MoadianDocumentNotFoundError(ApplicationError):
    """Raised when a Moadian document does not exist (or belongs to another tenant)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=MOADIAN_DOCUMENT_NOT_FOUND, details=details)


class MoadianSubmissionNotFoundError(ApplicationError):
    """Raised when a Moadian submission does not exist (or belongs to another tenant)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=MOADIAN_SUBMISSION_NOT_FOUND, details=details)


class MoadianInvalidStateError(ApplicationError):
    """Raised when a submission transition is illegal in its current state.

    E.g. approving a submission that is not a draft, or queueing one
    that has no recorded approval.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=MOADIAN_INVALID_STATE, details=details)


class MoadianInvalidDocumentError(ApplicationError):
    """Raised when an invoice payload fails Table 7 validation.

    The ``details`` carry the full list of validation problems; the
    document is never created with an invalid payload.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=MOADIAN_INVALID_DOCUMENT, details=details)


class MoadianProductionNotConnectedError(ApplicationError):
    """Raised when a production submission is queued before the connector exists.

    Production connectivity is not built in this phase (spec §12.3:
    simulator first). Production profiles may hold drafts and recorded
    approvals, but queueing is refused until the production connector
    lands in a later phase.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=MOADIAN_PRODUCTION_NOT_CONNECTED, details=details)


class MoadianProfileSlugConflictError(ApplicationError):
    """Raised when a profile slug is already used by the tenant."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=MOADIAN_PROFILE_SLUG_CONFLICT, details=details)


class MoadianDuplicateTaxIdError(ApplicationError):
    """Raised when a document's taxid is already used by the tenant."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=MOADIAN_DUPLICATE_TAXID, details=details)


class IntegrationChannelNotFoundError(ApplicationError):
    """Raised when a payment channel is not in the installation's registry."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=INTEGRATION_CHANNEL_NOT_FOUND, details=details)


class IntegrationChannelKeyConflictError(ApplicationError):
    """Raised when a channel key is already used by the installation."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=INTEGRATION_CHANNEL_KEY_CONFLICT, details=details)


class IntegrationChannelInUseError(ApplicationError):
    """Raised when a channel cannot be deleted (contracts/mappings reference it)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=INTEGRATION_CHANNEL_IN_USE, details=details)


class IntegrationChannelReservedError(ApplicationError):
    """Raised when a RESERVED channel is enabled (US-524).

    ``torob_marketplace`` stays disabled until the Torob order API
    is verified — enabling it is refused, never silently allowed.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=INTEGRATION_CHANNEL_RESERVED, details=details)


class IntegrationContractNotFoundError(ApplicationError):
    """Raised when a gateway-contract version is not in the registry."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=INTEGRATION_CONTRACT_NOT_FOUND, details=details)


class IntegrationContractInvalidError(ApplicationError):
    """Raised when a contract version fails validation (bad legs, bad rate, …)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=INTEGRATION_CONTRACT_INVALID, details=details)


class IntegrationMappingNotFoundError(ApplicationError):
    """Raised when a mapping is not in the registry — the fail-closed signal.

    The pipeline treats this as "unknown mapping": the event goes to
    the review queue, never to a default account (spec §10.5).
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=INTEGRATION_MAPPING_NOT_FOUND, details=details)


class IntegrationMappingInvalidError(ApplicationError):
    """Raised when a mapping fails validation (unknown kind, empty keys, …)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=INTEGRATION_MAPPING_INVALID, details=details)


class IntegrationMappingSuggestionNotFoundError(ApplicationError):
    """Raised when a mapping suggestion does not exist (or belongs to another installation)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=INTEGRATION_MAPPING_SUGGESTION_NOT_FOUND, details=details)


class IntegrationMappingSuggestionResolvedError(ApplicationError):
    """Raised when a suggestion is already accepted/rejected."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=INTEGRATION_MAPPING_SUGGESTION_RESOLVED, details=details)


class IntegrationBankConnectionNotFoundError(ApplicationError):
    """Raised when a bank connection is not in the installation's registry."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=INTEGRATION_BANK_CONNECTION_NOT_FOUND, details=details)


class IntegrationBankConnectionConflictError(ApplicationError):
    """Raised when a bank key is already connected for the company."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=INTEGRATION_BANK_CONNECTION_CONFLICT, details=details)


class IntegrationCredentialsForbiddenError(ApplicationError):
    """Raised when a credential payload carries a forbidden secret.

    An internet-banking password is never accepted (ADR 0003 decision
    10 / §8.3): the correct model is OAuth/consent, not password
    sharing. The message names the rejected field; the value is never
    echoed.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=INTEGRATION_CREDENTIALS_FORBIDDEN, details=details)


class IntegrationCredentialVaultUnavailableError(ApplicationError):
    """Raised when credentials cannot be stored (no encryption key configured)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=INTEGRATION_CREDENTIAL_VAULT_UNAVAILABLE, details=details)


class IntegrationStoreNotFoundError(ApplicationError):
    """Raised when a store is not in the installation's registry."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=INTEGRATION_STORE_NOT_FOUND, details=details)


class IntegrationStoreKeyConflictError(ApplicationError):
    """Raised when a store key is already used by the installation."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=INTEGRATION_STORE_KEY_CONFLICT, details=details)


class IntegrationHolidayNotFoundError(ApplicationError):
    """Raised when a holiday date is not on the installation's list."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=INTEGRATION_HOLIDAY_NOT_FOUND, details=details)


class IntegrationSettingsInvalidError(ApplicationError):
    """Raised when installation settings fail validation (bad weekday, …)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=INTEGRATION_SETTINGS_INVALID, details=details)
class ReviewItemNotFoundError(ApplicationError):
    """Raised when a review item does not exist (or belongs to another tenant)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=REVIEW_ITEM_NOT_FOUND, details=details)


class ReviewActionNotPermittedError(ApplicationError):
    """Raised when an action is not permitted for the item's type.

    The permitted actions per type come from phased design §7.2; an
    action code outside that list is rejected, never silently mapped
    onto another action.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=REVIEW_ACTION_NOT_PERMITTED, details=details)


class ReviewInvalidStateError(ApplicationError):
    """Raised when an action is illegal in the item's current state.

    E.g. acting on a resolved or written-off item, which is terminal.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=REVIEW_INVALID_STATE, details=details)


class ReviewInvalidSlaPolicyError(ApplicationError):
    """Raised when an SLA policy update is invalid (e.g. warn >= sla hours)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=REVIEW_INVALID_SLA_POLICY, details=details)


class CashBoxNotFoundError(ApplicationError):
    """Raised when a cash box does not exist (or belongs to another tenant)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=CASHBOX_NOT_FOUND, details=details)


class CashBoxJournalConflictError(ApplicationError):
    """Raised when a Statement Journal already has a cash box registered."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=CASHBOX_JOURNAL_CONFLICT, details=details)


class CashBoxInvalidError(ApplicationError):
    """Raised when a cash-box registration is invalid (e.g. a petty-cash box without a ceiling)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=CASHBOX_INVALID, details=details)


class CashBoxSettingsInvalidError(ApplicationError):
    """Raised when a cash-box settings update is invalid (bad policy, bad threshold, …)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=CASHBOX_SETTINGS_INVALID, details=details)


class CountSessionNotFoundError(ApplicationError):
    """Raised when a counting session does not exist (or belongs to another tenant)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=COUNT_SESSION_NOT_FOUND, details=details)


class CountSessionInvalidStateError(ApplicationError):
    """Raised when a session transition is illegal in its current state.

    E.g. entering a count on an archived session, or posting a variance
    journal for a session that is still being counted.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=COUNT_SESSION_INVALID_STATE, details=details)


class CountSessionAlreadyOpenError(ApplicationError):
    """Raised when a session is started while the box already has an open one."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=COUNT_SESSION_ALREADY_OPEN, details=details)


class CountSupervisorMustDifferError(ApplicationError):
    """Raised when the supervisor is the same user as the cashier (internal control)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=COUNT_SUPERVISOR_MUST_DIFFER, details=details)


class CountMinutesRequiredError(ApplicationError):
    """Raised when a variance session cannot complete without signed minutes."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=COUNT_MINUTES_REQUIRED, details=details)


class CountReviewNotApprovedError(ApplicationError):
    """Raised when a variance journal is posted before the manager approved it in the review queue."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=COUNT_REVIEW_NOT_APPROVED, details=details)


class CountDenominationsMismatchError(ApplicationError):
    """Raised when a denomination breakdown does not sum to the counted amount."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=COUNT_DENOMINATIONS_MISMATCH, details=details)


class CountRecountNotRequestedError(ApplicationError):
    """Raised when a session is reopened for recount without a manager's recount request."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=COUNT_RECOUNT_NOT_REQUESTED, details=details)


class CountWrongActorError(ApplicationError):
    """Raised when the actor of a counting step is not the session's holder of that role.

    E.g. someone other than the session's cashier entering the count, or
    someone other than the named supervisor recording attendance.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=COUNT_WRONG_ACTOR, details=details)


class EngineUnavailableError(ApplicationError):
    """Raised when the accounting engine cannot be reached (retryable)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=ENGINE_UNAVAILABLE, details=details, retryable=True)


class EngineAuthenticationError(ApplicationError):
    """Raised when the engine rejects the configured credentials."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=ENGINE_AUTHENTICATION_FAILED, details=details)


class EngineForbiddenError(ApplicationError):
    """Raised when the engine refuses an operation (access forbidden)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=ENGINE_FORBIDDEN, details=details)


class EngineNotFoundError(ApplicationError):
    """Raised when the engine reports a resource as not found."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=ENGINE_NOT_FOUND, details=details)


class EngineValidationError(ApplicationError):
    """Raised when the engine rejects a request as invalid."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=ENGINE_VALIDATION_FAILED, details=details)


class EngineError(ApplicationError):
    """Raised for any other engine failure."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=ENGINE_ERROR, details=details)


class OperationNotSupportedError(ApplicationError):
    """Raised when a use case has no engine-port operation to call.

    The port does not (yet) expose the operation — e.g. journal-draft
    update and draft listing in US-202 — so the use case fails with a
    typed 501 instead of inventing engine behaviour. The REST layer
    maps this to ``NOT_IMPLEMENTED``.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=NOT_IMPLEMENTED, details=details)


_ENGINE_ERROR_TYPES: dict[str, type[ApplicationError]] = {
    ENGINE_UNAVAILABLE: EngineUnavailableError,
    ENGINE_AUTHENTICATION_FAILED: EngineAuthenticationError,
    ENGINE_FORBIDDEN: EngineForbiddenError,
    ENGINE_NOT_FOUND: EngineNotFoundError,
    ENGINE_VALIDATION_FAILED: EngineValidationError,
    ENGINE_ERROR: EngineError,
}


def translate_engine_error(error: HesabYarError) -> HesabYarError:
    """Translate an engine failure into the matching application error.

    Returns a new application error for ``ENGINE_*`` codes (same code and
    message, engine-neutral type); returns the input unchanged for every
    other error — domain ledger errors and application errors are already
    engine-neutral and must not be re-wrapped.
    """
    translated_type = _ENGINE_ERROR_TYPES.get(error.code)
    if translated_type is None:
        return error
    return translated_type(error.message, details=dict(error.details))


@contextmanager
def translate_port_errors() -> Generator[None, None, None]:
    """Translate engine failures raised inside the block into application errors."""
    try:
        yield
    except HesabYarError as exc:
        translated = translate_engine_error(exc)
        if translated is exc:
            raise
        raise translated from exc
