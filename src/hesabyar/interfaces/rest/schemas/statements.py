"""Response schemas for the bank-statement endpoints (US-205, spec §9.2).

Money follows the §9.3 contract: integer Rial amounts (``currency`` /
``unit`` metadata on every statement response), never floats, never
Toman. Statement line amounts are signed: positive for money in,
negative for money out. Dates are ISO Gregorian (``YYYY-MM-DD``).

``BankStatementResponse.id`` is the statement's id in the accounting
engine (statements have no HesabYar-side mapping row); ``company_id``
is the HesabYar API id of the owning company, matching
``CompanyResponse.id``.
"""

from __future__ import annotations

import datetime

from pydantic import BaseModel, Field

from hesabyar.application.queries.statements import StatementImportStatus
from hesabyar.domain.accounting.value_objects import (
    BankStatementLineView,
    BankStatementView,
)


class BankStatementLineResponse(BaseModel):
    """One line of a bank/cash statement, as read back from the engine."""

    id: int = Field(description="Engine id of the statement line")
    date: datetime.date = Field(description="Line date")
    amount: int = Field(
        description="Signed line amount in integer Rial (positive = money in)"
    )
    account_id: int = Field(description="Engine id of the line account")
    description: str = Field(description="Line description")
    number: str | None = Field(
        default=None, description="Line number, when the engine sets one"
    )
    party_id: int | None = Field(
        default=None, description="Engine id of the line's party, when set"
    )
    party_name: str | None = Field(
        default=None, description="Party display name, when the line has a party"
    )
    related_invoice_id: int | None = Field(
        default=None,
        description="Engine id of the invoice the line is matched to, when matched",
    )

    @classmethod
    def from_view(cls, view: BankStatementLineView) -> BankStatementLineResponse:
        """Build a response line from a domain view."""
        return cls(
            id=view.id,
            date=view.date.gregorian_date,
            amount=view.amount.amount,
            account_id=view.account.engine_id,
            description=view.description,
            number=view.number,
            party_id=view.party.engine_id if view.party is not None else None,
            party_name=view.party_name,
            related_invoice_id=(
                view.related_invoice.engine_id
                if view.related_invoice is not None
                else None
            ),
        )


class BankStatementResponse(BaseModel):
    """A bank/cash statement as read back from the engine (any state)."""

    id: int = Field(description="Id of the statement in the accounting engine")
    company_id: str = Field(
        description="HesabYar API id of the owning company (the engine_companies row id)"
    )
    engine_company_id: int = Field(description="Engine id of the owning company")
    name: str = Field(description="Statement name from the engine")
    journal_id: int = Field(description="Engine id of the statement journal")
    journal_name: str = Field(description="Name of the statement journal")
    date: datetime.date = Field(description="Statement date")
    state: str = Field(
        description="Engine state: 'draft', 'validated', 'posted' or 'cancelled'"
    )
    validation: str = Field(
        description="Journal validation type: 'balance', 'amount' or 'number_of_lines'"
    )
    currency: str = Field(description="Statement currency (ISO code)")
    unit: str = Field(default="rial", description="Money unit of all amounts")
    start_balance: int | None = Field(
        default=None,
        description="Opening balance in integer Rial (balance journals only)",
    )
    end_balance: int | None = Field(
        default=None,
        description="Closing balance in integer Rial (balance journals only)",
    )
    balance: int | None = Field(
        default=None,
        description="Engine-computed balance in integer Rial (balance journals only)",
    )
    total_amount: int | None = Field(
        default=None,
        description="Total amount in integer Rial (amount journals only)",
    )
    number_of_lines: int | None = Field(
        default=None, description="Line count (number_of_lines journals only)"
    )
    lines: list[BankStatementLineResponse] = Field(description="Statement lines")

    @classmethod
    def from_view(cls, view: BankStatementView, company_id: str) -> BankStatementResponse:
        """Build a response from a domain view and its company's API id."""
        return cls(
            id=view.ref.engine_id,
            company_id=company_id,
            engine_company_id=view.company.engine_id,
            name=view.name,
            journal_id=view.journal_id,
            journal_name=view.journal_name,
            date=view.date.gregorian_date,
            state=view.state,
            validation=view.validation,
            currency=view.currency_code,
            start_balance=(
                view.start_balance.amount if view.start_balance is not None else None
            ),
            end_balance=(
                view.end_balance.amount if view.end_balance is not None else None
            ),
            balance=view.balance.amount if view.balance is not None else None,
            total_amount=(
                view.total_amount.amount if view.total_amount is not None else None
            ),
            number_of_lines=view.number_of_lines,
            lines=[BankStatementLineResponse.from_view(line) for line in view.lines],
        )


class StatementImportStatusResponse(BaseModel):
    """Read-only import status of a company's bank/cash statements (US-205).

    A summary derived from the statements the engine already holds —
    per-state counts, the newest statement, and how many statement
    lines are matched to an invoice. It is not an import operation:
    importing statements (``POST /bank/statements/import``, spec §9.2)
    is a later story.
    """

    company_id: str = Field(
        description="HesabYar API id of the company (the engine_companies row id)"
    )
    engine_company_id: int = Field(description="Engine id of the company")
    total_statements: int = Field(description="Number of statements of the company")
    draft_count: int = Field(description="Statements in state 'draft'")
    validated_count: int = Field(description="Statements in state 'validated'")
    posted_count: int = Field(description="Statements in state 'posted'")
    cancelled_count: int = Field(description="Statements in state 'cancelled'")
    latest_statement_id: int | None = Field(
        default=None, description="Engine id of the newest statement, or null when none"
    )
    latest_statement_date: datetime.date | None = Field(
        default=None, description="Date of the newest statement, or null when none"
    )
    total_lines: int = Field(description="Statement lines across all statements")
    matched_lines: int = Field(
        description="Lines already matched to an invoice (related_invoice set)"
    )
    unmatched_lines: int = Field(description="Lines not yet matched to an invoice")

    @classmethod
    def from_status(
        cls, status: StatementImportStatus, company_id: str
    ) -> StatementImportStatusResponse:
        """Build a response from a domain status and its company's API id."""
        latest = status.latest_statement
        return cls(
            company_id=company_id,
            engine_company_id=status.company.engine_id,
            total_statements=status.total_statements,
            draft_count=status.draft_count,
            validated_count=status.validated_count,
            posted_count=status.posted_count,
            cancelled_count=status.cancelled_count,
            latest_statement_id=latest.ref.engine_id if latest is not None else None,
            latest_statement_date=(
                latest.date.gregorian_date if latest is not None else None
            ),
            total_lines=status.total_lines,
            matched_lines=status.matched_lines,
            unmatched_lines=status.unmatched_lines,
        )
