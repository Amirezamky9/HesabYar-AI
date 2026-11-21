"""Bank-statement import + reconciliation domain errors (US-514, 5ت B1/B2).

Codes follow the ``BANK_*`` namespace (the ``LEDGER_*`` codes stay with
the engine-owned statement errors in ``domain.common.errors``). The
REST layer maps these in ``interfaces.rest.errors``.
"""

from __future__ import annotations

from typing import Any

from hesabyar.domain.common.errors import DomainError


class BankError(DomainError):
    """Base exception for the bank import/reconciliation bounded context."""


class BankImportNotFoundError(BankError):
    """Raised when an import batch id does not exist for the tenant."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="BANK_IMPORT_NOT_FOUND", details=details)


class BankImportInvalidStateError(BankError):
    """Raised when a batch is confirmed/cancelled from the wrong state."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="BANK_IMPORT_INVALID_STATE", details=details)


class BankImportFileError(BankError):
    """Raised when an uploaded statement file cannot be parsed at all.

    Per-row problems are NOT raised here — they land on the row as
    ``parse_errors`` and the batch preview shows them. This error is
    for whole-file failures: unknown format, unreadable content, no
    data rows, a header row that cannot be found.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="BANK_IMPORT_INVALID_FILE", details=details)


class BankImportTooManyRowsError(BankError):
    """Raised when a file exceeds the 2,000-row bank-export cap.

    Bank exports cap at 2,000 rows per file (verified for some banks;
    Baam's cap is UNVERIFIED — the design applies the cap to every
    import). The operator splits long ranges into smaller date ranges
    and imports each file separately; the dedup layers make overlapping
    ranges safe.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="BANK_IMPORT_TOO_MANY_ROWS", details=details)


class BankColumnMappingNotFoundError(BankError):
    """Raised when a saved column mapping id does not exist for the tenant."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="BANK_MAPPING_NOT_FOUND", details=details)


class BankColumnMappingInvalidError(BankError):
    """Raised when a column mapping is missing a required canonical field."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="BANK_MAPPING_INVALID", details=details)


class BankMatchRuleNotFoundError(BankError):
    """Raised when a learned match-rule id does not exist for the tenant."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="BANK_MATCH_RULE_NOT_FOUND", details=details)


class BankRuleNotFoundError(BankError):
    """Raised when a user-defined bank-rule id does not exist for the tenant.

    Distinct from :class:`BankMatchRuleNotFoundError`: that code is
    the B2 learned rule (``bank_match_rules``); this one is the W3
    user-defined rule (``bank_rules``, US-565).
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="BANK_RULE_NOT_FOUND", details=details)


class BankRuleInvalidError(BankError):
    """Raised when a bank-rule definition fails validation.

    A rule needs at least one pattern condition (description,
    counterparty, or amount bound), a sane amount range, a known
    direction, and a positive target account.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="BANK_RULE_INVALID", details=details)


class BankRuleNotPromotableError(BankError):
    """Raised when a bank rule cannot be promoted (or demoted).

    Promotion needs SUGGEST mode and at least
    ``RULE_AUTO_MIN_CONFIRMATIONS`` confirmed suggestions; demotion
    needs AUTO mode. Both are human decisions — nothing auto-promotes.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="BANK_RULE_NOT_PROMOTABLE", details=details)


class BankRuleSuggestionNotFoundError(BankError):
    """Raised when a bank-rule suggestion id does not exist for the tenant."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="BANK_RULE_SUGGESTION_NOT_FOUND", details=details)


class BankRuleSuggestionNotPendingError(BankError):
    """Raised when a suggestion is confirmed/rejected twice (no longer pending)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="BANK_RULE_SUGGESTION_NOT_PENDING", details=details)


class BankRuleNothingToUndoError(BankError):
    """Raised when undo targets a line with no live auto-coded match.

    Only an AUTO_APPLIED match whose target is a rule can be undone —
    an invoice/payment/manual match is a different decision, corrected
    through the manual-match flow, not the undo path.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="BANK_RULE_NOTHING_TO_UNDO", details=details)


class BankStatementNotDraftError(BankError):
    """Raised when reconcile/manual-match targets a non-draft statement.

    Matching writes line fields (``related_to`` / account / party),
    which Tryton only allows while the statement is a draft; a
    validated/posted statement's lines are read-only. The accountant
    reconciles before validating.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="BANK_STATEMENT_NOT_DRAFT", details=details)


class BankStatementLineNotFoundError(BankError):
    """Raised when a statement line id is not on the given statement."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="BANK_STATEMENT_LINE_NOT_FOUND", details=details)


class BankLineAlreadyMatchedError(BankError):
    """Raised when a manual match targets a line that already has a live match."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="BANK_LINE_ALREADY_MATCHED", details=details)


class BankStatementJournalNotFoundError(BankError):
    """Raised when a statement-journal id does not exist for the company."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="BANK_STATEMENT_JOURNAL_NOT_FOUND", details=details)
