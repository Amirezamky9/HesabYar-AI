"""One consistent typed error envelope for the REST API (spec §9.1).

Every error response is an RFC 9457 problem-details document
(``application/problem+json``) with a stable HesabYar ``code`` and the
request's ``request_id`` for correlation. Error responses never contain
stack traces, database URLs, credentials, or raw Tryton internals:
unhandled exceptions are logged server-side and answered with a generic
message.
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from contextlib import contextmanager
from typing import cast

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from hesabyar.application.errors import (
    CashBoxInvalidError,
    CashBoxJournalConflictError,
    CashBoxNotFoundError,
    CashBoxSettingsInvalidError,
    CompanyNotFoundError,
    CountDenominationsMismatchError,
    CountMinutesRequiredError,
    CountRecountNotRequestedError,
    CountReviewNotApprovedError,
    CountSessionAlreadyOpenError,
    CountSessionInvalidStateError,
    CountSessionNotFoundError,
    CountSupervisorMustDifferError,
    CountWrongActorError,
    EngineAuthenticationError,
    EngineError,
    EngineForbiddenError,
    EngineNotFoundError,
    EngineUnavailableError,
    EngineValidationError,
    IntegrationBankConnectionConflictError,
    IntegrationBankConnectionNotFoundError,
    IntegrationChannelInUseError,
    IntegrationChannelKeyConflictError,
    IntegrationChannelNotFoundError,
    IntegrationChannelReservedError,
    IntegrationContractInvalidError,
    IntegrationContractNotFoundError,
    IntegrationCredentialsForbiddenError,
    IntegrationCredentialVaultUnavailableError,
    IntegrationHolidayNotFoundError,
    IntegrationMappingInvalidError,
    IntegrationMappingNotFoundError,
    IntegrationMappingNotPromotableError,
    IntegrationMappingSuggestionNotFoundError,
    IntegrationMappingSuggestionResolvedError,
    IntegrationSettingsInvalidError,
    IntegrationStoreKeyConflictError,
    IntegrationStoreNotFoundError,
    MoadianDocumentNotFoundError,
    MoadianDuplicateTaxIdError,
    MoadianInvalidDocumentError,
    MoadianInvalidStateError,
    MoadianProductionNotConnectedError,
    MoadianProfileNotFoundError,
    MoadianProfileSlugConflictError,
    MoadianSubmissionNotFoundError,
    OperationNotSupportedError,
    RecurringTemplateInUseError,
    RecurringTemplateNotFoundError,
    ReportValidationRunNotFoundError,
    ReviewActionNotPermittedError,
    ReviewInvalidSlaPolicyError,
    ReviewInvalidStateError,
    ReviewItemNotFoundError,
    ReviewReplayNotPossibleError,
    RuleExecutionNotFoundError,
    RuleInvalidError,
    RuleNotFoundError,
    ScheduledJobNotFoundError,
    ScheduledJobNotRunnableError,
    StandardNotFoundError,
    StandardSourceBlockedError,
    TaxDecisionNotFoundError,
    TaxPolicyNotFoundError,
    TaxPolicySourceBlockedError,
    TaxSourceNotFoundError,
    UnbalancedTrialBalanceError,
    ValidationRunNotFoundError,
)
from hesabyar.domain.bank.errors import (
    BankColumnMappingInvalidError,
    BankColumnMappingNotFoundError,
    BankImportFileError,
    BankImportInvalidStateError,
    BankImportNotFoundError,
    BankImportTooManyRowsError,
    BankLineAlreadyMatchedError,
    BankMatchRuleNotFoundError,
    BankRuleInvalidError,
    BankRuleNotFoundError,
    BankRuleNothingToUndoError,
    BankRuleNotPromotableError,
    BankRuleSuggestionNotFoundError,
    BankRuleSuggestionNotPendingError,
    BankStatementJournalNotFoundError,
    BankStatementLineNotFoundError,
    BankStatementNotDraftError,
)
from hesabyar.domain.cheques.errors import (
    ChequeDuplicateError,
    ChequeEngineError,
    ChequeNotFoundError,
    ChequePolicyError,
    ChequePreAcceptanceError,
    ChequeRegistryDivergenceError,
    ChequeTransitionNotAllowedError,
    ChequeValidationError,
)
from hesabyar.domain.cheques.sayyad import SayyadInquiryError, SayyadLiveNotReadyError
from hesabyar.domain.common.errors import (
    AccountNotFoundError,
    AnalyticAccountNotFoundError,
    BankStatementNotFoundError,
    FiscalPeriodNotFoundError,
    FiscalYearNotFoundError,
    HesabYarError,
    InvalidAnalyticAccountError,
    InvalidBankStatementError,
    InvalidInventoryError,
    InvalidInvoiceError,
    InvalidJournalError,
    InvalidOrderPointError,
    InvalidPaymentError,
    InvalidProductError,
    InvalidStockMoveError,
    InventoryNotFoundError,
    InvoiceNotFoundError,
    JournalAlreadyPostedError,
    JournalDraftNotFoundError,
    OrderPointNotFoundError,
    OutShipmentNotFoundError,
    PartyNotFoundError,
    PaymentNotFoundError,
    PeriodClosedError,
    PostedJournalNotFoundError,
    ProductNotFoundError,
    PurchaseRequestNotFoundError,
    StockLocationNotFoundError,
    UnbalancedJournalError,
)
from hesabyar.domain.events.errors import (
    EventNotDeadLetteredError,
    EventNotFoundError,
    EventNotReprocessableError,
    EventSignatureError,
    EventTypePendingAdrError,
    IdempotencyConflictError,
    InvalidEventEnvelopeError,
    InvalidStoreAmountError,
    InvalidSyncCursorError,
    InvalidSyncRequestError,
    ManualEntryNotFoundError,
    ManualEntryValidationError,
    MappingUnresolvedError,
    PayloadHashMismatchError,
    StoreDatabaseError,
    StoreNotConfiguredError,
    StoreSyncFailedError,
    StoreUnreachableError,
    UnknownEventTypeError,
    UnsupportedSchemaVersionError,
    WebhookVaultUnavailableError,
)
from hesabyar.domain.gateways.errors import (
    GatewayAuthError,
    GatewayInvalidResponseError,
    GatewayNotConfiguredError,
    GatewayRejectedError,
    GatewayUnreachableError,
    GatewayUnsupportedOperationError,
    GatewayVerificationMismatchError,
)
from hesabyar.domain.installments.errors import (
    InstallmentAlreadySettledError,
    InstallmentBnplAlreadyRejectedError,
    InstallmentBnplEarlySettlementError,
    InstallmentBnplGatewayMismatchError,
    InstallmentBnplPlanMethodError,
    InstallmentBnplPlanRequiredError,
    InstallmentBnplUnavailableError,
    InstallmentChequeAlreadyUsedError,
    InstallmentChequeAmountError,
    InstallmentChequeLinkError,
    InstallmentChequeNotReceivableError,
    InstallmentContractNotFoundError,
    InstallmentInvoiceMismatchError,
    InstallmentLateFeeAccountMissingError,
    InstallmentLinkageError,
    InstallmentNotBnplReceiptError,
    InstallmentPaymentMethodError,
    InstallmentPlanExistsError,
    InstallmentPlanNotFoundError,
    InstallmentReceiptNotFoundError,
    InstallmentSettlementAmountError,
    InstallmentValidationError,
)
from hesabyar.domain.manual_imports.errors import (
    ManualImportDuplicateError,
    ManualImportFileError,
    ManualImportInvalidStateError,
    ManualImportNotFoundError,
    SettlementColumnMappingInvalidError,
    SettlementColumnMappingNotFoundError,
)
from hesabyar.domain.packaging.errors import (
    CarrierRateNotFoundError,
    CostCenterExistsError,
    CountNotFoundError,
    FifoUnavailableError,
    InvalidCarrierRateError,
    InvalidConsumptionError,
    InvalidCountError,
    InvalidMaterialError,
    InvalidPriceQuoteError,
    InvalidReorderPointError,
    InvalidShipmentCostError,
    MaterialNotFoundError,
    PackagingSetupError,
)
from hesabyar.domain.recurring.errors import InvalidRecurringTemplateError
from hesabyar.domain.reminders.errors import (
    ReminderAlreadySentError,
    ReminderValidationError,
)
from hesabyar.domain.settlement.errors import (
    SettlementContractMissingError,
    SettlementDepositNotFoundError,
    SettlementDerivationError,
    SettlementEventNotFoundError,
    SettlementExceptionNotFoundError,
    SettlementFeeVatRateUnknownError,
    SettlementFileImportExistsError,
    SettlementFileImportNotFoundError,
    SettlementImportInvalidError,
    SettlementImportStateError,
    SettlementInvalidTransitionError,
    SettlementMatchError,
    SettlementPaymentExistsError,
    SettlementPaymentInvalidError,
    SettlementPaymentNotFoundError,
    SettlementPostingError,
    SettlementPostingExistsError,
    SettlementPostingNotConfiguredError,
    SettlementPostingPreconditionError,
    SettlementReconciliationFailedError,
    SettlementTieOutFailedError,
    SettlementTrancheNotFoundError,
    SettlementWiringUnresolvedError,
    SettlementWriteOffNotAllowedError,
)
from hesabyar.domain.sms.errors import (
    SmsLegNotFoundError,
    SmsLegNotRemindableError,
    SmsNoProviderAvailableError,
    SmsNoUnpaidLegError,
    SmsProviderAuthError,
    SmsProviderClientUnavailableError,
    SmsProviderInUseError,
    SmsProviderInvalidResponseError,
    SmsProviderKeyConflictError,
    SmsProviderNotConfiguredError,
    SmsProviderNotEnabledError,
    SmsProviderNotFoundError,
    SmsProviderRejectedError,
    SmsProviderUnreachableError,
    SmsRecipientPhoneMissingError,
    SmsReminderAlreadySentError,
    SmsReminderNotOptedInError,
    SmsValidationError,
    SmsVaultUnavailableError,
)

logger = logging.getLogger(__name__)

PROBLEM_CONTENT_TYPE = "application/problem+json"
PROBLEM_BASE_URL = "https://hesabyar.local/problems"


class ProblemDetails(BaseModel):
    """RFC 9457 problem details plus HesabYar's stable code and request id."""

    type: str = Field(description="URI identifying this problem type")
    title: str = Field(description="Short human-readable summary")
    status: int = Field(description="HTTP status code")
    detail: str = Field(description="Human-readable explanation of this occurrence")
    code: str = Field(description="Stable HesabYar machine-readable error code")
    request_id: str | None = Field(default=None, description="Correlation id (also in the X-Request-ID header)")


class ApiError(StarletteHTTPException):
    """An error raised by HesabYar code, carrying a stable machine code."""

    def __init__(self, status_code: int, code: str, detail: str) -> None:
        super().__init__(status_code=status_code, detail=detail)
        self.code = code


_STATUS_TITLES = {
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    422: "Unprocessable Entity",
    500: "Internal Server Error",
    501: "Not Implemented",
    502: "Bad Gateway",
    503: "Service Unavailable",
}

_STATUS_CODES = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    422: "VALIDATION_ERROR",
    500: "INTERNAL_ERROR",
    501: "NOT_IMPLEMENTED",
    502: "BAD_GATEWAY",
    503: "SERVICE_UNAVAILABLE",
}


def api_error_from_hesabyar_error(exc: HesabYarError) -> ApiError:
    """Map a domain/application error onto a typed HTTP problem.

    Not-found errors keep the caller's own identifiers in the detail
    (they are safe to echo); engine failures get a generic detail so no
    engine internal can leak. Engine errors map to 502 (the upstream
    engine failed) except unavailability, which is 503 and retryable.
    """
    if isinstance(exc, CompanyNotFoundError):
        return ApiError(status_code=404, code="COMPANY_NOT_FOUND", detail=exc.message)
    if isinstance(exc, StandardNotFoundError):
        return ApiError(status_code=404, code="STANDARD_NOT_FOUND", detail=exc.message)
    if isinstance(exc, ValidationRunNotFoundError):
        return ApiError(status_code=404, code="VALIDATION_RUN_NOT_FOUND", detail=exc.message)
    if isinstance(exc, ReportValidationRunNotFoundError):
        return ApiError(status_code=404, code="REPORT_VALIDATION_RUN_NOT_FOUND", detail=exc.message)
    if isinstance(exc, UnbalancedTrialBalanceError):
        return ApiError(
            status_code=502,
            code="TRIAL_BALANCE_UNBALANCED",
            detail="The accounting engine's posted moves do not balance. No validation run was built on these figures.",
        )
    if isinstance(exc, StandardSourceBlockedError):
        return ApiError(status_code=422, code="STANDARD_SOURCE_BLOCKED", detail=exc.message)
    if isinstance(exc, TaxSourceNotFoundError):
        return ApiError(status_code=404, code="TAX_SOURCE_NOT_FOUND", detail=exc.message)
    if isinstance(exc, TaxPolicyNotFoundError):
        return ApiError(status_code=404, code="TAX_POLICY_NOT_FOUND", detail=exc.message)
    if isinstance(exc, TaxDecisionNotFoundError):
        return ApiError(status_code=404, code="TAX_DECISION_NOT_FOUND", detail=exc.message)
    if isinstance(exc, TaxPolicySourceBlockedError):
        return ApiError(status_code=422, code="TAX_POLICY_SOURCE_BLOCKED", detail=exc.message)
    if isinstance(exc, MoadianProfileNotFoundError):
        return ApiError(status_code=404, code="MOADIAN_PROFILE_NOT_FOUND", detail=exc.message)
    if isinstance(exc, MoadianDocumentNotFoundError):
        return ApiError(status_code=404, code="MOADIAN_DOCUMENT_NOT_FOUND", detail=exc.message)
    if isinstance(exc, MoadianSubmissionNotFoundError):
        return ApiError(status_code=404, code="MOADIAN_SUBMISSION_NOT_FOUND", detail=exc.message)
    if isinstance(exc, MoadianInvalidStateError):
        return ApiError(status_code=409, code="MOADIAN_INVALID_STATE", detail=exc.message)
    if isinstance(exc, MoadianInvalidDocumentError):
        problems = exc.details.get("problems")
        detail = exc.message
        if isinstance(problems, list) and problems:
            detail = f"{exc.message} Problems: {'; '.join(str(p) for p in problems)}"
        return ApiError(status_code=422, code="MOADIAN_INVALID_DOCUMENT", detail=detail)
    if isinstance(exc, MoadianProductionNotConnectedError):
        return ApiError(status_code=501, code="MOADIAN_PRODUCTION_NOT_CONNECTED", detail=exc.message)
    if isinstance(exc, MoadianProfileSlugConflictError):
        return ApiError(status_code=409, code="MOADIAN_PROFILE_SLUG_CONFLICT", detail=exc.message)
    if isinstance(exc, MoadianDuplicateTaxIdError):
        return ApiError(status_code=409, code="MOADIAN_DUPLICATE_TAXID", detail=exc.message)
    if isinstance(exc, IntegrationChannelNotFoundError):
        return ApiError(status_code=404, code="INTEGRATION_CHANNEL_NOT_FOUND", detail=exc.message)
    if isinstance(exc, IntegrationChannelKeyConflictError):
        return ApiError(
            status_code=409, code="INTEGRATION_CHANNEL_KEY_CONFLICT", detail=exc.message
        )
    if isinstance(exc, IntegrationChannelInUseError):
        return ApiError(status_code=409, code="INTEGRATION_CHANNEL_IN_USE", detail=exc.message)
    if isinstance(exc, IntegrationChannelReservedError):
        return ApiError(status_code=422, code="INTEGRATION_CHANNEL_RESERVED", detail=exc.message)
    if isinstance(exc, IntegrationContractNotFoundError):
        return ApiError(status_code=404, code="INTEGRATION_CONTRACT_NOT_FOUND", detail=exc.message)
    if isinstance(exc, IntegrationContractInvalidError):
        return ApiError(status_code=422, code="INTEGRATION_CONTRACT_INVALID", detail=exc.message)
    if isinstance(exc, IntegrationMappingNotFoundError):
        return ApiError(status_code=404, code="INTEGRATION_MAPPING_NOT_FOUND", detail=exc.message)
    if isinstance(exc, IntegrationMappingInvalidError):
        return ApiError(status_code=422, code="INTEGRATION_MAPPING_INVALID", detail=exc.message)
    if isinstance(exc, IntegrationMappingSuggestionNotFoundError):
        return ApiError(
            status_code=404, code="INTEGRATION_MAPPING_SUGGESTION_NOT_FOUND", detail=exc.message
        )
    if isinstance(exc, IntegrationMappingSuggestionResolvedError):
        return ApiError(
            status_code=409, code="INTEGRATION_MAPPING_SUGGESTION_RESOLVED", detail=exc.message
        )
    if isinstance(exc, IntegrationMappingNotPromotableError):
        return ApiError(
            status_code=409, code="INTEGRATION_MAPPING_NOT_PROMOTABLE", detail=exc.message
        )
    if isinstance(exc, IntegrationBankConnectionNotFoundError):
        return ApiError(
            status_code=404, code="INTEGRATION_BANK_CONNECTION_NOT_FOUND", detail=exc.message
        )
    if isinstance(exc, IntegrationBankConnectionConflictError):
        return ApiError(
            status_code=409, code="INTEGRATION_BANK_CONNECTION_CONFLICT", detail=exc.message
        )
    if isinstance(exc, IntegrationCredentialsForbiddenError):
        return ApiError(
            status_code=422, code="INTEGRATION_CREDENTIALS_FORBIDDEN", detail=exc.message
        )
    if isinstance(exc, IntegrationCredentialVaultUnavailableError):
        return ApiError(
            status_code=503,
            code="INTEGRATION_CREDENTIAL_VAULT_UNAVAILABLE",
            detail=exc.message,
        )
    if isinstance(exc, IntegrationStoreNotFoundError):
        return ApiError(status_code=404, code="INTEGRATION_STORE_NOT_FOUND", detail=exc.message)
    if isinstance(exc, IntegrationStoreKeyConflictError):
        return ApiError(
            status_code=409, code="INTEGRATION_STORE_KEY_CONFLICT", detail=exc.message
        )
    if isinstance(exc, IntegrationHolidayNotFoundError):
        return ApiError(status_code=404, code="INTEGRATION_HOLIDAY_NOT_FOUND", detail=exc.message)
    if isinstance(exc, IntegrationSettingsInvalidError):
        return ApiError(status_code=422, code="INTEGRATION_SETTINGS_INVALID", detail=exc.message)
    if isinstance(exc, ReviewItemNotFoundError):
        return ApiError(status_code=404, code="REVIEW_ITEM_NOT_FOUND", detail=exc.message)
    if isinstance(exc, ReviewActionNotPermittedError):
        return ApiError(status_code=422, code="REVIEW_ACTION_NOT_PERMITTED", detail=exc.message)
    if isinstance(exc, ReviewInvalidStateError):
        return ApiError(status_code=409, code="REVIEW_INVALID_STATE", detail=exc.message)
    if isinstance(exc, ReviewInvalidSlaPolicyError):
        return ApiError(status_code=422, code="REVIEW_INVALID_SLA_POLICY", detail=exc.message)
    if isinstance(exc, ReviewReplayNotPossibleError):
        return ApiError(status_code=422, code="REVIEW_REPLAY_NOT_POSSIBLE", detail=exc.message)
    if isinstance(exc, ScheduledJobNotFoundError):
        return ApiError(status_code=404, code="SCHEDULED_JOB_NOT_FOUND", detail=exc.message)
    if isinstance(exc, ScheduledJobNotRunnableError):
        return ApiError(status_code=503, code="SCHEDULED_JOB_NOT_RUNNABLE", detail=exc.message)
    if isinstance(exc, RuleNotFoundError):
        return ApiError(status_code=404, code="RULE_NOT_FOUND", detail=exc.message)
    if isinstance(exc, RuleInvalidError):
        return ApiError(status_code=422, code="RULE_INVALID", detail=exc.message)
    if isinstance(exc, RuleExecutionNotFoundError):
        return ApiError(status_code=404, code="RULE_EXECUTION_NOT_FOUND", detail=exc.message)
    if isinstance(exc, InvalidRecurringTemplateError):
        return ApiError(status_code=422, code="RECURRING_TEMPLATE_INVALID", detail=exc.message)
    if isinstance(exc, RecurringTemplateNotFoundError):
        return ApiError(status_code=404, code="RECURRING_TEMPLATE_NOT_FOUND", detail=exc.message)
    if isinstance(exc, RecurringTemplateInUseError):
        return ApiError(status_code=409, code="RECURRING_TEMPLATE_IN_USE", detail=exc.message)
    if isinstance(exc, MaterialNotFoundError):
        return ApiError(status_code=404, code="PACKAGING_MATERIAL_NOT_FOUND", detail=exc.message)
    if isinstance(exc, CountNotFoundError):
        return ApiError(status_code=404, code="PACKAGING_COUNT_NOT_FOUND", detail=exc.message)
    if isinstance(exc, CarrierRateNotFoundError):
        return ApiError(status_code=404, code="PACKAGING_CARRIER_RATE_NOT_FOUND", detail=exc.message)
    if isinstance(exc, CostCenterExistsError):
        return ApiError(status_code=409, code="PACKAGING_COST_CENTER_EXISTS", detail=exc.message)
    if isinstance(exc, FifoUnavailableError):
        return ApiError(status_code=422, code="PACKAGING_FIFO_UNAVAILABLE", detail=exc.message)
    if isinstance(exc, PackagingSetupError):
        return ApiError(status_code=422, code="PACKAGING_SETUP_REQUIRED", detail=exc.message)
    if isinstance(exc, InvalidMaterialError):
        return ApiError(status_code=400, code="PACKAGING_INVALID_MATERIAL", detail=exc.message)
    if isinstance(exc, InvalidReorderPointError):
        return ApiError(status_code=400, code="PACKAGING_INVALID_REORDER_POINT", detail=exc.message)
    if isinstance(exc, InvalidConsumptionError):
        return ApiError(status_code=400, code="PACKAGING_INVALID_CONSUMPTION", detail=exc.message)
    if isinstance(exc, InvalidShipmentCostError):
        return ApiError(status_code=400, code="PACKAGING_INVALID_SHIPMENT_COST", detail=exc.message)
    if isinstance(exc, InvalidPriceQuoteError):
        return ApiError(status_code=400, code="PACKAGING_INVALID_PRICE_QUOTE", detail=exc.message)
    if isinstance(exc, InvalidCountError):
        return ApiError(status_code=400, code="PACKAGING_INVALID_COUNT", detail=exc.message)
    if isinstance(exc, InvalidCarrierRateError):
        return ApiError(status_code=400, code="PACKAGING_INVALID_CARRIER_RATE", detail=exc.message)
    if isinstance(exc, CashBoxNotFoundError):
        return ApiError(status_code=404, code="CASHBOX_NOT_FOUND", detail=exc.message)
    if isinstance(exc, CashBoxJournalConflictError):
        return ApiError(status_code=409, code="CASHBOX_JOURNAL_CONFLICT", detail=exc.message)
    if isinstance(exc, CashBoxInvalidError):
        return ApiError(status_code=422, code="CASHBOX_INVALID", detail=exc.message)
    if isinstance(exc, CashBoxSettingsInvalidError):
        return ApiError(status_code=422, code="CASHBOX_SETTINGS_INVALID", detail=exc.message)
    if isinstance(exc, CountSessionNotFoundError):
        return ApiError(status_code=404, code="COUNT_SESSION_NOT_FOUND", detail=exc.message)
    if isinstance(exc, CountSessionInvalidStateError):
        return ApiError(status_code=409, code="COUNT_SESSION_INVALID_STATE", detail=exc.message)
    if isinstance(exc, CountSessionAlreadyOpenError):
        return ApiError(status_code=409, code="COUNT_SESSION_ALREADY_OPEN", detail=exc.message)
    if isinstance(exc, CountSupervisorMustDifferError):
        return ApiError(status_code=422, code="COUNT_SUPERVISOR_MUST_DIFFER", detail=exc.message)
    if isinstance(exc, CountMinutesRequiredError):
        return ApiError(status_code=422, code="COUNT_MINUTES_REQUIRED", detail=exc.message)
    if isinstance(exc, CountReviewNotApprovedError):
        return ApiError(status_code=409, code="COUNT_REVIEW_NOT_APPROVED", detail=exc.message)
    if isinstance(exc, CountDenominationsMismatchError):
        return ApiError(status_code=422, code="COUNT_DENOMINATIONS_MISMATCH", detail=exc.message)
    if isinstance(exc, CountRecountNotRequestedError):
        return ApiError(status_code=409, code="COUNT_RECOUNT_NOT_REQUESTED", detail=exc.message)
    if isinstance(exc, CountWrongActorError):
        return ApiError(status_code=422, code="COUNT_WRONG_ACTOR", detail=exc.message)
    if isinstance(exc, AccountNotFoundError):
        return ApiError(status_code=404, code="ACCOUNT_NOT_FOUND", detail=exc.message)
    if isinstance(exc, StockLocationNotFoundError):
        return ApiError(status_code=404, code="LEDGER_STOCK_LOCATION_NOT_FOUND", detail=exc.message)
    if isinstance(exc, OrderPointNotFoundError):
        return ApiError(status_code=404, code="LEDGER_ORDER_POINT_NOT_FOUND", detail=exc.message)
    if isinstance(exc, PurchaseRequestNotFoundError):
        return ApiError(status_code=404, code="LEDGER_PURCHASE_REQUEST_NOT_FOUND", detail=exc.message)
    if isinstance(exc, AnalyticAccountNotFoundError):
        return ApiError(status_code=404, code="LEDGER_ANALYTIC_ACCOUNT_NOT_FOUND", detail=exc.message)
    if isinstance(exc, InventoryNotFoundError):
        return ApiError(status_code=404, code="LEDGER_INVENTORY_NOT_FOUND", detail=exc.message)
    if isinstance(exc, OutShipmentNotFoundError):
        return ApiError(status_code=404, code="LEDGER_OUT_SHIPMENT_NOT_FOUND", detail=exc.message)
    if isinstance(exc, ProductNotFoundError):
        return ApiError(status_code=404, code="LEDGER_PRODUCT_NOT_FOUND", detail=exc.message)
    if isinstance(exc, InvalidStockMoveError):
        return ApiError(status_code=400, code="LEDGER_INVALID_STOCK_MOVE", detail=exc.message)
    if isinstance(exc, InvalidProductError):
        return ApiError(status_code=400, code="LEDGER_INVALID_PRODUCT", detail=exc.message)
    if isinstance(exc, InvalidOrderPointError):
        return ApiError(status_code=400, code="LEDGER_INVALID_ORDER_POINT", detail=exc.message)
    if isinstance(exc, InvalidAnalyticAccountError):
        return ApiError(status_code=400, code="LEDGER_INVALID_ANALYTIC_ACCOUNT", detail=exc.message)
    if isinstance(exc, InvalidInventoryError):
        return ApiError(status_code=400, code="LEDGER_INVALID_INVENTORY", detail=exc.message)
    if isinstance(exc, FiscalYearNotFoundError):
        return ApiError(status_code=404, code="FISCAL_YEAR_NOT_FOUND", detail=exc.message)
    if isinstance(exc, FiscalPeriodNotFoundError):
        return ApiError(status_code=404, code="FISCAL_PERIOD_NOT_FOUND", detail=exc.message)
    if isinstance(exc, (JournalDraftNotFoundError, PostedJournalNotFoundError)):
        return ApiError(status_code=404, code="LEDGER_JOURNAL_NOT_FOUND", detail=exc.message)
    if isinstance(exc, JournalAlreadyPostedError):
        return ApiError(status_code=409, code="LEDGER_POSTED_IMMUTABLE", detail=exc.message)
    if isinstance(exc, UnbalancedJournalError):
        return ApiError(status_code=400, code="LEDGER_UNBALANCED", detail=exc.message)
    if isinstance(exc, PeriodClosedError):
        return ApiError(status_code=400, code="LEDGER_PERIOD_CLOSED", detail=exc.message)
    if isinstance(exc, InvalidJournalError):
        return ApiError(status_code=400, code="LEDGER_INVALID_JOURNAL", detail=exc.message)
    if isinstance(exc, InvoiceNotFoundError):
        return ApiError(status_code=404, code="LEDGER_INVOICE_NOT_FOUND", detail=exc.message)
    if isinstance(exc, InvalidInvoiceError):
        return ApiError(status_code=400, code="LEDGER_INVALID_INVOICE", detail=exc.message)
    if isinstance(exc, PaymentNotFoundError):
        return ApiError(status_code=404, code="LEDGER_PAYMENT_NOT_FOUND", detail=exc.message)
    if isinstance(exc, InvalidPaymentError):
        return ApiError(status_code=400, code="LEDGER_INVALID_PAYMENT", detail=exc.message)
    if isinstance(exc, BankImportNotFoundError):
        return ApiError(status_code=404, code="BANK_IMPORT_NOT_FOUND", detail=exc.message)
    if isinstance(exc, BankColumnMappingNotFoundError):
        return ApiError(status_code=404, code="BANK_COLUMN_MAPPING_NOT_FOUND", detail=exc.message)
    if isinstance(exc, BankMatchRuleNotFoundError):
        return ApiError(status_code=404, code="BANK_MATCH_RULE_NOT_FOUND", detail=exc.message)
    if isinstance(exc, BankRuleNotFoundError):
        return ApiError(status_code=404, code="BANK_RULE_NOT_FOUND", detail=exc.message)
    if isinstance(exc, BankRuleInvalidError):
        return ApiError(status_code=422, code="BANK_RULE_INVALID", detail=exc.message)
    if isinstance(exc, BankRuleNotPromotableError):
        return ApiError(status_code=409, code="BANK_RULE_NOT_PROMOTABLE", detail=exc.message)
    if isinstance(exc, BankRuleSuggestionNotFoundError):
        return ApiError(status_code=404, code="BANK_RULE_SUGGESTION_NOT_FOUND", detail=exc.message)
    if isinstance(exc, BankRuleSuggestionNotPendingError):
        return ApiError(status_code=409, code="BANK_RULE_SUGGESTION_NOT_PENDING", detail=exc.message)
    if isinstance(exc, BankRuleNothingToUndoError):
        return ApiError(status_code=409, code="BANK_RULE_NOTHING_TO_UNDO", detail=exc.message)
    if isinstance(exc, BankStatementJournalNotFoundError):
        return ApiError(status_code=404, code="BANK_STATEMENT_JOURNAL_NOT_FOUND", detail=exc.message)
    if isinstance(exc, BankStatementLineNotFoundError):
        return ApiError(status_code=404, code="BANK_STATEMENT_LINE_NOT_FOUND", detail=exc.message)
    if isinstance(exc, BankImportInvalidStateError):
        return ApiError(status_code=409, code="BANK_IMPORT_INVALID_STATE", detail=exc.message)
    if isinstance(exc, BankStatementNotDraftError):
        return ApiError(status_code=409, code="BANK_STATEMENT_NOT_DRAFT", detail=exc.message)
    if isinstance(exc, BankLineAlreadyMatchedError):
        return ApiError(status_code=409, code="BANK_LINE_ALREADY_MATCHED", detail=exc.message)
    if isinstance(exc, BankImportFileError):
        return ApiError(status_code=422, code="BANK_IMPORT_FILE_ERROR", detail=exc.message)
    if isinstance(exc, BankImportTooManyRowsError):
        return ApiError(status_code=422, code="BANK_IMPORT_TOO_MANY_ROWS", detail=exc.message)
    if isinstance(exc, BankColumnMappingInvalidError):
        return ApiError(status_code=422, code="BANK_COLUMN_MAPPING_INVALID", detail=exc.message)
    if isinstance(exc, BankStatementNotFoundError):
        return ApiError(status_code=404, code="LEDGER_STATEMENT_NOT_FOUND", detail=exc.message)
    if isinstance(exc, InvalidBankStatementError):
        return ApiError(status_code=400, code="LEDGER_INVALID_STATEMENT", detail=exc.message)
    if isinstance(exc, PartyNotFoundError):
        return ApiError(status_code=404, code="LEDGER_PARTY_NOT_FOUND", detail=exc.message)
    if isinstance(exc, EventNotFoundError):
        return ApiError(status_code=404, code="INBOX_EVENT_NOT_FOUND", detail=exc.message)
    if isinstance(exc, EventNotDeadLetteredError):
        return ApiError(status_code=409, code="EVENT_NOT_DEAD_LETTERED", detail=exc.message)
    if isinstance(exc, EventNotReprocessableError):
        return ApiError(status_code=409, code="EVENT_NOT_REPROCESSABLE", detail=exc.message)
    if isinstance(exc, IdempotencyConflictError):
        return ApiError(status_code=409, code="IDEMPOTENCY_CONFLICT", detail=exc.message)
    if isinstance(exc, EventSignatureError):
        return ApiError(status_code=401, code="EVENT_SIGNATURE_INVALID", detail=exc.message)
    if isinstance(exc, UnsupportedSchemaVersionError):
        return ApiError(status_code=422, code="EVENT_UNSUPPORTED_SCHEMA_VERSION", detail=exc.message)
    if isinstance(exc, UnknownEventTypeError):
        return ApiError(status_code=422, code="EVENT_UNKNOWN_TYPE", detail=exc.message)
    if isinstance(exc, EventTypePendingAdrError):
        return ApiError(status_code=422, code="EVENT_TYPE_PENDING_ADR_APPROVAL", detail=exc.message)
    if isinstance(exc, WebhookVaultUnavailableError):
        return ApiError(
            status_code=503,
            code="EVENT_WEBHOOK_VAULT_UNAVAILABLE",
            detail=exc.message,
        )
    if isinstance(exc, PayloadHashMismatchError):
        return ApiError(status_code=422, code="EVENT_PAYLOAD_HASH_MISMATCH", detail=exc.message)
    if isinstance(exc, InvalidStoreAmountError):
        return ApiError(status_code=422, code="EVENT_INVALID_AMOUNT", detail=exc.message)
    if isinstance(exc, MappingUnresolvedError):
        return ApiError(status_code=422, code="EVENT_MAPPING_UNRESOLVED", detail=exc.message)
    if isinstance(exc, InvalidEventEnvelopeError):
        return ApiError(status_code=422, code="EVENT_INVALID_ENVELOPE", detail=exc.message)
    if isinstance(exc, InvalidSyncCursorError):
        return ApiError(status_code=422, code="SYNC_INVALID_CURSOR", detail=exc.message)
    if isinstance(exc, InvalidSyncRequestError):
        return ApiError(status_code=422, code="SYNC_INVALID_REQUEST", detail=exc.message)
    if isinstance(exc, StoreNotConfiguredError):
        return ApiError(status_code=422, code="STORE_NOT_CONFIGURED", detail=exc.message)
    if isinstance(exc, StoreUnreachableError):
        return ApiError(status_code=502, code="STORE_UNREACHABLE", detail=exc.message)
    if isinstance(exc, StoreSyncFailedError):
        return ApiError(status_code=502, code="STORE_SYNC_FAILED", detail=exc.message)
    if isinstance(exc, StoreDatabaseError):
        return ApiError(status_code=502, code="STORE_DB_ERROR", detail=exc.message)
    if isinstance(exc, ManualEntryValidationError):
        return ApiError(status_code=422, code="MANUAL_ENTRY_INVALID", detail=exc.message)
    if isinstance(exc, ManualEntryNotFoundError):
        return ApiError(status_code=404, code="MANUAL_ENTRY_NOT_FOUND", detail=exc.message)
    if isinstance(exc, SettlementPaymentNotFoundError):
        return ApiError(status_code=404, code="SETTLEMENT_PAYMENT_NOT_FOUND", detail=exc.message)
    if isinstance(exc, SettlementPaymentExistsError):
        return ApiError(status_code=409, code="SETTLEMENT_PAYMENT_EXISTS", detail=exc.message)
    if isinstance(exc, SettlementTrancheNotFoundError):
        return ApiError(status_code=404, code="SETTLEMENT_TRANCHE_NOT_FOUND", detail=exc.message)
    if isinstance(exc, SettlementInvalidTransitionError):
        return ApiError(status_code=409, code="SETTLEMENT_INVALID_TRANSITION", detail=exc.message)
    if isinstance(exc, SettlementContractMissingError):
        return ApiError(status_code=422, code="SETTLEMENT_CONTRACT_MISSING", detail=exc.message)
    if isinstance(exc, SettlementFeeVatRateUnknownError):
        return ApiError(status_code=422, code="SETTLEMENT_FEE_VAT_RATE_UNKNOWN", detail=exc.message)
    if isinstance(exc, SettlementDerivationError):
        return ApiError(status_code=422, code="SETTLEMENT_DERIVATION_ERROR", detail=exc.message)
    if isinstance(exc, SettlementPaymentInvalidError):
        return ApiError(status_code=422, code="SETTLEMENT_PAYMENT_INVALID", detail=exc.message)
    if isinstance(exc, SettlementWriteOffNotAllowedError):
        return ApiError(status_code=422, code="SETTLEMENT_WRITE_OFF_NOT_ALLOWED", detail=exc.message)
    if isinstance(exc, SettlementExceptionNotFoundError):
        return ApiError(status_code=404, code="SETTLEMENT_EXCEPTION_NOT_FOUND", detail=exc.message)
    if isinstance(exc, SettlementFileImportExistsError):
        return ApiError(status_code=409, code="SETTLEMENT_FILE_IMPORT_EXISTS", detail=exc.message)
    if isinstance(exc, SettlementFileImportNotFoundError):
        return ApiError(status_code=404, code="SETTLEMENT_FILE_IMPORT_NOT_FOUND", detail=exc.message)
    if isinstance(exc, SettlementPostingExistsError):
        return ApiError(status_code=409, code="SETTLEMENT_POSTING_EXISTS", detail=exc.message)
    if isinstance(exc, SettlementPostingPreconditionError):
        return ApiError(status_code=422, code="SETTLEMENT_POSTING_PRECONDITION", detail=exc.message)
    if isinstance(exc, SettlementTieOutFailedError):
        return ApiError(status_code=422, code="SETTLEMENT_TIE_OUT_FAILED", detail=exc.message)
    if isinstance(exc, SettlementWiringUnresolvedError):
        return ApiError(status_code=422, code="SETTLEMENT_WIRING_UNRESOLVED", detail=exc.message)
    if isinstance(exc, SettlementReconciliationFailedError):
        return ApiError(status_code=422, code="SETTLEMENT_RECONCILIATION_FAILED", detail=exc.message)
    if isinstance(exc, SettlementImportInvalidError):
        return ApiError(status_code=422, code="SETTLEMENT_IMPORT_INVALID", detail=exc.message)
    if isinstance(exc, SettlementImportStateError):
        return ApiError(status_code=409, code="SETTLEMENT_IMPORT_STATE", detail=exc.message)
    if isinstance(exc, ManualImportNotFoundError):
        return ApiError(status_code=404, code="MANUAL_IMPORT_NOT_FOUND", detail=exc.message)
    if isinstance(exc, ManualImportInvalidStateError):
        return ApiError(status_code=409, code="MANUAL_IMPORT_INVALID_STATE", detail=exc.message)
    if isinstance(exc, ManualImportFileError):
        return ApiError(status_code=422, code="MANUAL_IMPORT_FILE_ERROR", detail=exc.message)
    if isinstance(exc, ManualImportDuplicateError):
        return ApiError(status_code=409, code="MANUAL_IMPORT_DUPLICATE", detail=exc.message)
    if isinstance(exc, SettlementColumnMappingNotFoundError):
        return ApiError(status_code=404, code="SETTLEMENT_MAPPING_NOT_FOUND", detail=exc.message)
    if isinstance(exc, SettlementColumnMappingInvalidError):
        return ApiError(status_code=422, code="SETTLEMENT_MAPPING_INVALID", detail=exc.message)
    if isinstance(exc, SettlementDepositNotFoundError):
        return ApiError(status_code=404, code="SETTLEMENT_DEPOSIT_NOT_FOUND", detail=exc.message)
    if isinstance(exc, SettlementEventNotFoundError):
        return ApiError(status_code=404, code="SETTLEMENT_EVENT_NOT_FOUND", detail=exc.message)
    if isinstance(exc, SettlementMatchError):
        return ApiError(status_code=422, code="SETTLEMENT_MATCH_ERROR", detail=exc.message)
    if isinstance(exc, SettlementPostingNotConfiguredError):
        return ApiError(
            status_code=422, code="SETTLEMENT_POSTING_NOT_CONFIGURED", detail=exc.message
        )
    if isinstance(exc, SettlementPostingError):
        return ApiError(status_code=502, code="SETTLEMENT_POSTING_ERROR", detail=exc.message)
    if isinstance(exc, ChequeNotFoundError):
        return ApiError(status_code=404, code="CHEQUE_NOT_FOUND", detail=exc.message)
    if isinstance(exc, ChequeValidationError):
        detail = exc.message
        if exc.field_errors:
            problems = "; ".join(f"{name}: {problem}" for name, problem in sorted(exc.field_errors.items()))
            detail = f"{exc.message} Problems: {problems}"
        return ApiError(status_code=422, code="CHEQUE_VALIDATION_FAILED", detail=detail)
    if isinstance(exc, ChequeDuplicateError):
        return ApiError(status_code=409, code="CHEQUE_DUPLICATE_SAYAD_ID", detail=exc.message)
    if isinstance(exc, ChequeTransitionNotAllowedError):
        return ApiError(status_code=409, code="CHEQUE_TRANSITION_NOT_ALLOWED", detail=exc.message)
    if isinstance(exc, ChequePreAcceptanceError):
        return ApiError(status_code=422, code="CHEQUE_PRE_ACCEPTANCE_REFUSED", detail=exc.message)
    if isinstance(exc, ChequeRegistryDivergenceError):
        return ApiError(
            status_code=502,
            code="CHEQUE_REGISTRY_DIVERGENCE",
            detail=(
                "The engine transition succeeded but the HesabYar registry "
                "could not be updated; a divergence review item was filed. "
                "Do not retry the transition."
            ),
        )
    if isinstance(exc, ChequePolicyError):
        return ApiError(status_code=422, code="CHEQUE_POLICY_DENIED", detail=exc.message)
    if isinstance(exc, ChequeEngineError):
        return ApiError(
            status_code=502,
            code="CHEQUE_ENGINE_ERROR",
            detail="The cheque engine failed to answer the request.",
        )
    if isinstance(exc, InstallmentPlanNotFoundError):
        return ApiError(status_code=404, code="INSTALLMENT_PLAN_NOT_FOUND", detail=exc.message)
    if isinstance(exc, InstallmentContractNotFoundError):
        return ApiError(status_code=404, code="INSTALLMENT_CONTRACT_NOT_FOUND", detail=exc.message)
    if isinstance(exc, InstallmentPlanExistsError):
        return ApiError(status_code=409, code="INSTALLMENT_PLAN_EXISTS", detail=exc.message)
    if isinstance(exc, InstallmentLinkageError):
        return ApiError(
            status_code=422,
            code="INSTALLMENT_RECEIPT_LINKAGE_REQUIRED",
            detail=exc.message,
        )
    if isinstance(exc, InstallmentInvoiceMismatchError):
        return ApiError(status_code=422, code="INSTALLMENT_INVOICE_MISMATCH", detail=exc.message)
    if isinstance(exc, InstallmentPaymentMethodError):
        return ApiError(
            status_code=422, code="INSTALLMENT_PAYMENT_METHOD_UNSUPPORTED", detail=exc.message
        )
    if isinstance(exc, InstallmentReceiptNotFoundError):
        return ApiError(status_code=404, code="INSTALLMENT_RECEIPT_NOT_FOUND", detail=exc.message)
    if isinstance(exc, InstallmentBnplPlanRequiredError):
        return ApiError(status_code=422, code="INSTALLMENT_BNPL_PLAN_REQUIRED", detail=exc.message)
    if isinstance(exc, InstallmentBnplPlanMethodError):
        return ApiError(
            status_code=422, code="INSTALLMENT_BNPL_PLAN_METHOD_NOT_ALLOWED", detail=exc.message
        )
    if isinstance(exc, InstallmentBnplGatewayMismatchError):
        return ApiError(
            status_code=422, code="INSTALLMENT_BNPL_GATEWAY_MISMATCH", detail=exc.message
        )
    if isinstance(exc, InstallmentNotBnplReceiptError):
        return ApiError(status_code=422, code="INSTALLMENT_NOT_A_BNPL_RECEIPT", detail=exc.message)
    if isinstance(exc, InstallmentBnplAlreadyRejectedError):
        return ApiError(
            status_code=409, code="INSTALLMENT_BNPL_ALREADY_REJECTED", detail=exc.message
        )
    if isinstance(exc, InstallmentBnplUnavailableError):
        return ApiError(status_code=503, code="INSTALLMENT_BNPL_UNAVAILABLE", detail=exc.message)
    if isinstance(exc, InstallmentBnplEarlySettlementError):
        return ApiError(
            status_code=422,
            code="INSTALLMENT_BNPL_EARLY_SETTLEMENT_NOT_ALLOWED",
            detail=exc.message,
        )
    if isinstance(exc, InstallmentChequeNotReceivableError):
        return ApiError(
            status_code=422, code="INSTALLMENT_CHEQUE_NOT_RECEIVABLE", detail=exc.message
        )
    if isinstance(exc, InstallmentChequeLinkError):
        return ApiError(
            status_code=422, code="INSTALLMENT_CHEQUE_LINK_MISMATCH", detail=exc.message
        )
    if isinstance(exc, InstallmentChequeAlreadyUsedError):
        return ApiError(
            status_code=422, code="INSTALLMENT_CHEQUE_ALREADY_USED", detail=exc.message
        )
    if isinstance(exc, InstallmentChequeAmountError):
        return ApiError(
            status_code=422, code="INSTALLMENT_CHEQUE_AMOUNT_EXCEEDED", detail=exc.message
        )
    if isinstance(exc, InstallmentLateFeeAccountMissingError):
        return ApiError(
            status_code=422, code="INSTALLMENT_LATE_FEE_ACCOUNT_MISSING", detail=exc.message
        )
    if isinstance(exc, InstallmentSettlementAmountError):
        return ApiError(
            status_code=422, code="INSTALLMENT_SETTLEMENT_AMOUNT_INSUFFICIENT", detail=exc.message
        )
    if isinstance(exc, InstallmentAlreadySettledError):
        return ApiError(
            status_code=409, code="INSTALLMENT_PLAN_ALREADY_SETTLED", detail=exc.message
        )
    if isinstance(exc, InstallmentValidationError):
        detail = exc.message
        if exc.field_errors:
            problems = "; ".join(f"{name}: {problem}" for name, problem in sorted(exc.field_errors.items()))
            detail = f"{exc.message} Problems: {problems}"
        return ApiError(status_code=422, code="INSTALLMENT_VALIDATION_FAILED", detail=detail)
    if isinstance(exc, SmsValidationError):
        detail = exc.message
        if exc.field_errors:
            problems = "; ".join(f"{name}: {problem}" for name, problem in sorted(exc.field_errors.items()))
            detail = f"{exc.message} Problems: {problems}"
        return ApiError(status_code=422, code="SMS_VALIDATION_FAILED", detail=detail)
    if isinstance(exc, ReminderValidationError):
        detail = exc.message
        if exc.field_errors:
            problems = "; ".join(f"{name}: {problem}" for name, problem in sorted(exc.field_errors.items()))
            detail = f"{exc.message} Problems: {problems}"
        return ApiError(status_code=422, code="REMINDER_VALIDATION_FAILED", detail=detail)
    if isinstance(exc, ReminderAlreadySentError):
        return ApiError(status_code=409, code="REMINDER_ALREADY_SENT", detail=exc.message)
    if isinstance(exc, SmsProviderNotFoundError):
        return ApiError(status_code=404, code="SMS_PROVIDER_NOT_FOUND", detail=exc.message)
    if isinstance(exc, SmsProviderKeyConflictError):
        return ApiError(status_code=409, code="SMS_PROVIDER_KEY_CONFLICT", detail=exc.message)
    if isinstance(exc, SmsProviderInUseError):
        return ApiError(status_code=409, code="SMS_PROVIDER_IN_USE", detail=exc.message)
    if isinstance(exc, SmsProviderNotConfiguredError):
        return ApiError(status_code=422, code="SMS_PROVIDER_NOT_CONFIGURED", detail=exc.message)
    if isinstance(exc, SmsProviderNotEnabledError):
        return ApiError(status_code=422, code="SMS_PROVIDER_NOT_ENABLED", detail=exc.message)
    if isinstance(exc, SmsProviderClientUnavailableError):
        return ApiError(status_code=501, code="SMS_PROVIDER_CLIENT_UNAVAILABLE", detail=exc.message)
    if isinstance(exc, SmsProviderUnreachableError):
        return ApiError(status_code=502, code="SMS_PROVIDER_UNREACHABLE", detail=exc.message)
    if isinstance(exc, SmsProviderAuthError):
        return ApiError(status_code=422, code="SMS_PROVIDER_AUTH_FAILED", detail=exc.message)
    if isinstance(exc, SmsProviderRejectedError):
        return ApiError(status_code=422, code="SMS_PROVIDER_REJECTED", detail=exc.message)
    if isinstance(exc, SmsProviderInvalidResponseError):
        return ApiError(status_code=502, code="SMS_PROVIDER_INVALID_RESPONSE", detail=exc.message)
    if isinstance(exc, SmsVaultUnavailableError):
        return ApiError(status_code=503, code="SMS_CREDENTIAL_VAULT_UNAVAILABLE", detail=exc.message)
    if isinstance(exc, SmsNoProviderAvailableError):
        return ApiError(status_code=422, code="SMS_NO_PROVIDER_AVAILABLE", detail=exc.message)
    if isinstance(exc, SmsReminderNotOptedInError):
        return ApiError(status_code=422, code="SMS_REMINDER_NOT_OPTED_IN", detail=exc.message)
    if isinstance(exc, SmsReminderAlreadySentError):
        return ApiError(status_code=409, code="SMS_REMINDER_ALREADY_SENT", detail=exc.message)
    if isinstance(exc, SmsRecipientPhoneMissingError):
        return ApiError(status_code=422, code="SMS_RECIPIENT_PHONE_MISSING", detail=exc.message)
    if isinstance(exc, SmsLegNotFoundError):
        return ApiError(status_code=404, code="SMS_LEG_NOT_FOUND", detail=exc.message)
    if isinstance(exc, SmsLegNotRemindableError):
        return ApiError(status_code=422, code="SMS_LEG_NOT_REMINDABLE", detail=exc.message)
    if isinstance(exc, SmsNoUnpaidLegError):
        return ApiError(status_code=422, code="SMS_NO_UNPAID_LEG", detail=exc.message)
    if isinstance(exc, SayyadLiveNotReadyError):
        return ApiError(status_code=501, code="SAYYAD_LIVE_NOT_CONNECTED", detail=exc.message)
    if isinstance(exc, SayyadInquiryError):
        return ApiError(status_code=422, code="SAYYAD_INQUIRY_FAILED", detail=exc.message)
    if isinstance(exc, GatewayUnreachableError):
        return ApiError(status_code=502, code="GATEWAY_UNREACHABLE", detail=exc.message)
    if isinstance(exc, GatewayAuthError):
        return ApiError(status_code=422, code="GATEWAY_AUTH_FAILED", detail=exc.message)
    if isinstance(exc, GatewayRejectedError):
        return ApiError(status_code=422, code="GATEWAY_REJECTED", detail=exc.message)
    if isinstance(exc, GatewayInvalidResponseError):
        return ApiError(status_code=502, code="GATEWAY_INVALID_RESPONSE", detail=exc.message)
    if isinstance(exc, GatewayNotConfiguredError):
        return ApiError(status_code=422, code="GATEWAY_NOT_CONFIGURED", detail=exc.message)
    if isinstance(exc, GatewayUnsupportedOperationError):
        return ApiError(status_code=422, code="GATEWAY_OPERATION_UNSUPPORTED", detail=exc.message)
    if isinstance(exc, GatewayVerificationMismatchError):
        return ApiError(status_code=422, code="GATEWAY_VERIFICATION_MISMATCH", detail=exc.message)
    if isinstance(exc, OperationNotSupportedError):
        return ApiError(status_code=501, code="NOT_IMPLEMENTED", detail=exc.message)
    if isinstance(exc, EngineUnavailableError):
        return ApiError(
            status_code=503,
            code="ENGINE_UNAVAILABLE",
            detail="The accounting engine is unavailable. Please retry later.",
        )
    if isinstance(exc, EngineNotFoundError):
        return ApiError(
            status_code=404,
            code="ENGINE_NOT_FOUND",
            detail="The requested resource was not found in the accounting engine.",
        )
    if isinstance(exc, EngineValidationError):
        return ApiError(
            status_code=400,
            code="ENGINE_VALIDATION_FAILED",
            detail="The accounting engine rejected the request as invalid.",
        )
    if isinstance(exc, (EngineAuthenticationError, EngineForbiddenError, EngineError)):
        return ApiError(
            status_code=502,
            code=exc.code,
            detail="The accounting engine failed to answer the request.",
        )
    return ApiError(
        status_code=500,
        code="INTERNAL_ERROR",
        detail="An unexpected error occurred. Please retry later.",
    )


@contextmanager
def translate_errors() -> Generator[None, None, None]:
    """Translate domain/application errors raised inside the block into typed HTTP problems.

    Routes wrap use-case calls in this so a ``CompanyNotFoundError`` or
    an engine failure becomes the right problem-details response instead
    of a generic 500. ``ApiError`` passes through untouched.
    """
    try:
        yield
    except HesabYarError as exc:
        raise api_error_from_hesabyar_error(exc) from exc


def _request_id(request: Request) -> str | None:
    """Return the request id set by RequestIdMiddleware, if any."""
    return getattr(request.state, "request_id", None)


def _problem_response(
    request: Request,
    *,
    status: int,
    code: str,
    title: str,
    detail: str,
) -> JSONResponse:
    problem = ProblemDetails(
        type=f"{PROBLEM_BASE_URL}/{code.lower().replace('_', '-')}",
        title=title,
        status=status,
        detail=detail,
        code=code,
        request_id=_request_id(request),
    )
    headers: dict[str, str] = {}
    request_id = _request_id(request)
    if request_id is not None:
        # The middleware also sets this header; setting it here keeps the
        # envelope self-describing even if a handler runs outside it.
        headers["X-Request-ID"] = request_id
    return JSONResponse(
        status_code=status,
        content=problem.model_dump(mode="json"),
        headers=headers,
        media_type=PROBLEM_CONTENT_TYPE,
    )


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render HTTPException (including FastAPI's 404) as problem details."""
    http_exc = cast("StarletteHTTPException", exc)
    status = http_exc.status_code
    code = getattr(http_exc, "code", None) or _STATUS_CODES.get(status, f"HTTP_{status}")
    detail = http_exc.detail if isinstance(http_exc.detail, str) else _STATUS_TITLES.get(status, "Error")
    return _problem_response(
        request,
        status=status,
        code=code,
        title=_STATUS_TITLES.get(status, "Error"),
        detail=detail,
    )


async def validation_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render request-validation failures (422) as problem details."""
    validation_exc = cast("RequestValidationError", exc)
    parts = []
    for error in validation_exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ()))
        message = str(error.get("msg", "invalid value"))
        parts.append(f"{location}: {message}" if location else message)
    detail = "; ".join(parts) if parts else "Request validation failed."
    return _problem_response(
        request,
        status=422,
        code="VALIDATION_ERROR",
        title="Unprocessable Entity",
        detail=detail,
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render unhandled exceptions as a generic 500 problem.

    The exception itself is logged server-side only; the response carries
    a generic message so no traceback, path, credential, or engine
    internal can leak to the client.
    """
    logger.exception(
        "Unhandled error for %s %s (request_id=%s)",
        request.method,
        request.url.path,
        _request_id(request),
        exc_info=exc,
    )
    return _problem_response(
        request,
        status=500,
        code="INTERNAL_ERROR",
        title="Internal Server Error",
        detail="An unexpected error occurred. Please retry later.",
    )
