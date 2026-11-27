"""Domain exceptions and error hierarchy for HesabYar."""

from typing import Any


class HesabYarError(Exception):
    """Root exception for all HesabYar system, domain, and application errors."""

    def __init__(
        self,
        message: str,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or self.__class__.__name__
        self.details = details or {}

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(message={self.message!r}, code={self.code!r}, details={self.details!r})"


class DomainError(HesabYarError):
    """Base exception for domain logic, entity lifecycle, and business rule violations."""


class InvariantViolationError(DomainError):
    """Raised when an accounting invariant or entity integrity rule is violated."""


class TenantMismatchError(DomainError):
    """Raised when operations cross tenant isolation boundaries or tenant IDs mismatch."""


class InvalidMoneyError(DomainError):
    """Raised when money creation, arithmetic, or rounding violates financial rules."""


class InvalidDateError(DomainError):
    """Raised when date parsing, Jalali conversion, or fiscal period bounds are invalid."""


class InvalidIdentifierError(DomainError):
    """Raised when an entity or tenant identifier fails format or validation rules."""


class UnauthorizedScopeError(DomainError):
    """Raised when an actor lacks the required permission scope for a domain operation."""


class LedgerError(InvariantViolationError):
    """Base exception for accounting-ledger invariant and engine-contract violations.

    Codes follow the stable ledger taxonomy in docs/ARCHITECTURE.md §20.1.
    """


class UnbalancedJournalError(LedgerError):
    """Raised when a journal's debit total does not equal its credit total."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="LEDGER_UNBALANCED", details=details)


class InvalidJournalError(LedgerError):
    """Raised when a journal draft is structurally invalid (too few lines, zero total, two-sided line, ...)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="LEDGER_INVALID_JOURNAL", details=details)


class AccountNotFoundError(LedgerError):
    """Raised when an account does not exist, belongs to another company, or is closed/inactive."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="LEDGER_ACCOUNT_NOT_FOUND", details=details)


class PeriodClosedError(LedgerError):
    """Raised when no open fiscal period covers a journal date, or the period is closed."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="LEDGER_PERIOD_CLOSED", details=details)


class JournalDraftNotFoundError(LedgerError):
    """Raised when a referenced journal draft does not exist in the engine."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="LEDGER_JOURNAL_NOT_FOUND", details=details)


class PostedJournalNotFoundError(LedgerError):
    """Raised when a referenced posted journal does not exist in the engine."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="LEDGER_JOURNAL_NOT_FOUND", details=details)


class JournalAlreadyPostedError(LedgerError):
    """Raised when posting a journal that is already posted (spec §8.2: a posted journal is immutable)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="LEDGER_POSTED_IMMUTABLE", details=details)


class InvalidInvoiceError(LedgerError):
    """Raised when an invoice draft is structurally invalid (bad type, no lines, zero total, ...)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="LEDGER_INVALID_INVOICE", details=details)


class InvoiceNotFoundError(LedgerError):
    """Raised when a referenced invoice does not exist in the engine."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="LEDGER_INVOICE_NOT_FOUND", details=details)


class PaymentNotFoundError(LedgerError):
    """Raised when a referenced payment does not exist in the engine."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="LEDGER_PAYMENT_NOT_FOUND", details=details)


class InvalidPaymentError(LedgerError):
    """Raised when a payment request is invalid (bad kind/state filter, ...)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="LEDGER_INVALID_PAYMENT", details=details)


class BankStatementNotFoundError(LedgerError):
    """Raised when a referenced bank/cash statement does not exist in the engine."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="LEDGER_STATEMENT_NOT_FOUND", details=details)


class InvalidBankStatementError(LedgerError):
    """Raised when a bank-statement request is invalid (bad state filter, ...)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="LEDGER_INVALID_STATEMENT", details=details)


class PartyNotFoundError(LedgerError):
    """Raised when a referenced party does not exist in the engine."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="LEDGER_PARTY_NOT_FOUND", details=details)


class FiscalYearNotFoundError(LedgerError):
    """Raised when a referenced fiscal year does not exist for the company."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="LEDGER_FISCAL_YEAR_NOT_FOUND", details=details)


class FiscalPeriodNotFoundError(LedgerError):
    """Raised when a referenced fiscal period does not exist for the company."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="LEDGER_FISCAL_PERIOD_NOT_FOUND", details=details)


class SaleNotFoundError(LedgerError):
    """Raised when a referenced sale order does not exist in the engine."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="LEDGER_SALE_NOT_FOUND", details=details)


class InvalidSaleError(LedgerError):
    """Raised when a sale request is invalid (bad method, no lines, zero total, ...)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="LEDGER_INVALID_SALE", details=details)


class ProductNotFoundError(LedgerError):
    """Raised when a referenced product does not exist (or is not sellable) in the engine."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="LEDGER_PRODUCT_NOT_FOUND", details=details)


class StockLocationNotFoundError(LedgerError):
    """Raised when a referenced stock location does not exist in the engine."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="LEDGER_STOCK_LOCATION_NOT_FOUND", details=details)


class InvalidStockMoveError(LedgerError):
    """Raised when a stock move request violates an invariant."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="LEDGER_INVALID_STOCK_MOVE", details=details)


class InvalidProductError(LedgerError):
    """Raised when a product definition violates an invariant (cost method, ...)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="LEDGER_INVALID_PRODUCT", details=details)


class OrderPointNotFoundError(LedgerError):
    """Raised when a referenced order point does not exist in the engine."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="LEDGER_ORDER_POINT_NOT_FOUND", details=details)


class InvalidOrderPointError(LedgerError):
    """Raised when an order point request violates an invariant."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="LEDGER_INVALID_ORDER_POINT", details=details)


class PurchaseRequestNotFoundError(LedgerError):
    """Raised when a referenced purchase request does not exist in the engine."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="LEDGER_PURCHASE_REQUEST_NOT_FOUND", details=details)


class AnalyticAccountNotFoundError(LedgerError):
    """Raised when a referenced analytic account does not exist in the engine."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="LEDGER_ANALYTIC_ACCOUNT_NOT_FOUND", details=details)


class InvalidAnalyticAccountError(LedgerError):
    """Raised when an analytic account request violates an invariant."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="LEDGER_INVALID_ANALYTIC_ACCOUNT", details=details)


class InventoryNotFoundError(LedgerError):
    """Raised when a referenced stock inventory does not exist in the engine."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="LEDGER_INVENTORY_NOT_FOUND", details=details)


class InvalidInventoryError(LedgerError):
    """Raised when an inventory request violates an invariant (state, lines)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="LEDGER_INVALID_INVENTORY", details=details)


class OutShipmentNotFoundError(LedgerError):
    """Raised when a referenced outgoing shipment does not exist in the engine."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="LEDGER_OUT_SHIPMENT_NOT_FOUND", details=details)
