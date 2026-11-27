"""Request/response schemas for the journal endpoints (US-202, spec §9.2).

Money follows the §9.3 contract: integer Rial amounts (``currency`` /
``unit`` metadata on every journal response), never floats, never
Toman. Dates are ISO Gregorian (``YYYY-MM-DD``); Jalali display is a
presentation concern, not an API field.

``JournalDraftResponse.id`` / ``PostedJournalResponse.id`` are the
move's id in the accounting engine (drafts and journals have no
HesabYar-side mapping row); ``company_id`` is the HesabYar API id of
the owning company, matching ``CompanyResponse.id``.
"""

from __future__ import annotations

import datetime

from pydantic import BaseModel, Field

from hesabyar.domain.accounting.value_objects import (
    JournalDraftView,
    JournalLineView,
    PostedJournalView,
    ValidationSummary,
)


class JournalDraftLineRequest(BaseModel):
    """One line of a journal draft: exactly one side carries an amount."""

    account_id: int = Field(gt=0, description="Engine id of the account")
    debit: int = Field(
        default=0, ge=0, description="Debit amount in integer Rial (0 when credit-side)"
    )
    credit: int = Field(
        default=0, ge=0, description="Credit amount in integer Rial (0 when debit-side)"
    )
    description: str = Field(default="", description="Line description")


class CreateJournalDraftRequest(BaseModel):
    """Create a draft journal (an unposted move) in the engine."""

    company_id: str = Field(
        description="API company id (HesabYar row id or engine company id)"
    )
    date: datetime.date = Field(description="Accounting date, ISO Gregorian (YYYY-MM-DD)")
    description: str = Field(default="", description="Journal description")
    journal_id: int | None = Field(
        default=None,
        gt=0,
        description=(
            "Engine id of the accounting journal; when omitted, the "
            "company's single general journal is used"
        ),
    )
    lines: list[JournalDraftLineRequest] = Field(
        description=(
            "Journal lines (at least two; each one-sided; debit total "
            "must equal credit total and be greater than zero)"
        )
    )


class UpdateJournalDraftRequest(BaseModel):
    """Update a draft journal: only present fields change.

    A present ``lines`` value replaces the draft's lines wholesale;
    absent fields are left unchanged. At least one field must be
    present — an empty body is a 400 ``LEDGER_INVALID_JOURNAL``.
    """

    date: datetime.date | None = Field(
        default=None, description="New accounting date, ISO Gregorian (YYYY-MM-DD)"
    )
    description: str | None = Field(default=None, description="New journal description")
    lines: list[JournalDraftLineRequest] | None = Field(
        default=None, description="Complete replacement lines, when present"
    )


class ReverseJournalRequest(BaseModel):
    """Reverse a posted journal with a new posted reversal journal."""

    description: str = Field(
        default="",
        description=(
            "Description for the reversal; when empty, the original "
            "journal's description is kept"
        ),
    )


class JournalLineResponse(BaseModel):
    """One line of a journal, as read back from the engine."""

    account_id: int = Field(description="Engine id of the account")
    debit: int = Field(description="Debit amount in integer Rial")
    credit: int = Field(description="Credit amount in integer Rial")
    description: str = Field(description="Line description")
    state: str = Field(
        description="Engine line state: 'draft' (unbalanced move) or 'valid' (balanced move)"
    )

    @classmethod
    def from_view(cls, view: JournalLineView) -> JournalLineResponse:
        """Build a response line from a domain view."""
        return cls(
            account_id=view.account.engine_id,
            debit=view.debit.amount,
            credit=view.credit.amount,
            description=view.description,
            state=view.state,
        )


class JournalPeriodResponse(BaseModel):
    """The fiscal period a journal belongs to."""

    name: str = Field(description="Period name from the engine")
    start_date: datetime.date = Field(description="Period start date")
    end_date: datetime.date = Field(description="Period end date")
    state: str = Field(description="Period state: 'open', 'closed' or 'locked'")


def _period_response(view: JournalDraftView | PostedJournalView) -> JournalPeriodResponse:
    return JournalPeriodResponse(
        name=view.period.name,
        start_date=view.period.start_date.gregorian_date,
        end_date=view.period.end_date.gregorian_date,
        state=view.period.state,
    )


class JournalDraftResponse(BaseModel):
    """A journal draft (unposted move) as read back from the engine."""

    id: int = Field(description="Id of the draft in the accounting engine")
    company_id: str = Field(
        description="HesabYar API id of the owning company (the engine_companies row id)"
    )
    engine_company_id: int = Field(description="Engine id of the owning company")
    journal_id: int = Field(description="Engine id of the accounting journal")
    period: JournalPeriodResponse = Field(description="Fiscal period of the journal")
    date: datetime.date = Field(description="Accounting date")
    state: str = Field(description="Move state: 'draft', or 'posted' once posted")
    description: str = Field(description="Journal description")
    lines: list[JournalLineResponse] = Field(description="Journal lines")
    total_debit: int = Field(description="Total debit in integer Rial")
    total_credit: int = Field(description="Total credit in integer Rial")
    currency: str = Field(default="IRR", description="Accounting currency (ISO code)")
    unit: str = Field(default="rial", description="Money unit of all amounts")

    @classmethod
    def from_view(cls, view: JournalDraftView, company_id: str) -> JournalDraftResponse:
        """Build a response from a domain view and its company's API id."""
        return cls(
            id=view.ref.engine_id,
            company_id=company_id,
            engine_company_id=view.company.engine_id,
            journal_id=view.journal.engine_id,
            period=_period_response(view),
            date=view.date.gregorian_date,
            state=view.state,
            description=view.description,
            lines=[JournalLineResponse.from_view(line) for line in view.lines],
            total_debit=view.total_debit.amount,
            total_credit=view.total_credit.amount,
        )


class PostedJournalResponse(BaseModel):
    """A posted journal (a posted move) as read back from the engine."""

    id: int = Field(description="Id of the posted journal in the accounting engine")
    company_id: str = Field(
        description="HesabYar API id of the owning company (the engine_companies row id)"
    )
    engine_company_id: int = Field(description="Engine id of the owning company")
    journal_id: int = Field(description="Engine id of the accounting journal")
    period: JournalPeriodResponse = Field(description="Fiscal period of the journal")
    date: datetime.date = Field(description="Accounting date")
    post_date: datetime.date | None = Field(
        default=None, description="Date the engine posted the journal"
    )
    number: str = Field(description="Engine-assigned journal number")
    description: str = Field(description="Journal description")
    origin_id: int | None = Field(
        default=None,
        description="Engine id of the journal this one reverses; null unless this is a reversal",
    )
    lines: list[JournalLineResponse] = Field(description="Journal lines")
    total_debit: int = Field(description="Total debit in integer Rial")
    total_credit: int = Field(description="Total credit in integer Rial")
    currency: str = Field(default="IRR", description="Accounting currency (ISO code)")
    unit: str = Field(default="rial", description="Money unit of all amounts")

    @classmethod
    def from_view(cls, view: PostedJournalView, company_id: str) -> PostedJournalResponse:
        """Build a response from a domain view and its company's API id."""
        return cls(
            id=view.ref.engine_id,
            company_id=company_id,
            engine_company_id=view.company.engine_id,
            journal_id=view.journal.engine_id,
            period=_period_response(view),
            date=view.date.gregorian_date,
            post_date=(
                view.post_date.gregorian_date if view.post_date is not None else None
            ),
            number=view.number,
            description=view.description,
            origin_id=view.origin.engine_id if view.origin is not None else None,
            lines=[JournalLineResponse.from_view(line) for line in view.lines],
            total_debit=view.total_debit.amount,
            total_credit=view.total_credit.amount,
        )


class ValidationIssueResponse(BaseModel):
    """One problem found while validating a journal draft."""

    code: str = Field(description="Stable issue code (e.g. 'LEDGER_UNBALANCED')")
    message: str = Field(description="Human-readable explanation")
    field: str | None = Field(default=None, description="Field the issue concerns, if any")


class ValidationResponse(BaseModel):
    """The outcome of validating a draft (journal or invoice) against the invariants."""

    valid: bool = Field(description="True when the draft passes every invariant")
    issues: list[ValidationIssueResponse] = Field(
        default_factory=list, description="Problems found (empty when valid)"
    )

    @classmethod
    def from_summary(cls, summary: ValidationSummary) -> ValidationResponse:
        """Build a response from a domain validation summary."""
        return cls(
            valid=summary.valid,
            issues=[
                ValidationIssueResponse(
                    code=issue.code, message=issue.message, field=issue.field
                )
                for issue in summary.issues
            ],
        )
