"""Request/response schemas for the cash-box endpoints (US-516).

Responses map from the domain models/views — no ORM object ever
leaks. Money is IRR integers; the frontend renders them as تومان.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field

from hesabyar.application.queries.cashbox import CashBoxView
from hesabyar.domain.accounting.value_objects import StatementJournalView
from hesabyar.domain.cashbox.models import (
    CashBoxReport,
    CashBoxReportLine,
    CashBoxSettings,
    CountSession,
)


class CashBoxResponse(BaseModel):
    """One cash box with its live book balance and counting state."""

    id: str = Field(description="Cash box id")
    company_id: str = Field(description="HesabYar engine_companies row id")
    name_fa: str = Field(description="Cash-box name in Persian")
    kind: str = Field(description="Kind: store | petty_cash | other")
    statement_journal_id: int = Field(description="Engine id of the Tryton statement journal (daybook)")
    cash_account_id: int = Field(description="Engine id of the journal's cash account")
    cash_account_code: str = Field(description="Code of the cash account (e.g. '1.1.1')")
    ceiling_irr: int | None = Field(description="Petty-cash ceiling in IRR (petty-cash boxes only)")
    active: bool = Field(description="Inactive boxes cannot start counting sessions")
    book_balance_irr: int | None = Field(
        description=(
            "Live book balance in IRR (posted moves, as of today); "
            "null when the engine no longer returns the box's account"
        )
    )
    last_count_at: datetime | None = Field(description="When the box was last counted (any session status)")
    days_since_last_count: int | None = Field(description="Whole days since the last count (null when never counted)")
    uncounted_alert: bool = Field(description="True when uncounted longer than the reminder threshold")
    created_at: datetime | None = Field(description="When the box was registered (UTC)")
    updated_at: datetime | None = Field(description="Last update (UTC)")

    @classmethod
    def from_view(cls, view: CashBoxView) -> CashBoxResponse:
        """Build a response from the service's cash-box view."""
        box = view.box
        return cls(
            id=box.id,
            company_id=box.company_id,
            name_fa=box.name_fa,
            kind=box.kind.value,
            statement_journal_id=box.statement_journal_id,
            cash_account_id=box.cash_account_id,
            cash_account_code=box.cash_account_code,
            ceiling_irr=box.ceiling_irr,
            active=box.active,
            book_balance_irr=view.book_balance_irr,
            last_count_at=view.last_count_at,
            days_since_last_count=view.days_since_last_count,
            uncounted_alert=view.uncounted_alert,
            created_at=box.created_at,
            updated_at=box.updated_at,
        )


class CreateCashBoxRequest(BaseModel):
    """Register a cash box on one of the company's Statement Journals."""

    company_id: str = Field(description="API company id (HesabYar row id or engine id)")
    name_fa: str = Field(description="Cash-box name in Persian")
    kind: str = Field(description="Kind: store | petty_cash | other")
    statement_journal_id: int = Field(description="Engine id of the Tryton statement journal (daybook)")
    ceiling_irr: int | None = Field(
        default=None,
        description="Petty-cash ceiling in IRR (required for petty_cash, forbidden otherwise)",
    )


class UpdateCashBoxRequest(BaseModel):
    """Update a cash box's name, kind, ceiling and active flag."""

    name_fa: str = Field(description="Cash-box name in Persian")
    kind: str = Field(description="Kind: store | petty_cash | other")
    ceiling_irr: int | None = Field(
        default=None,
        description="Petty-cash ceiling in IRR (required for petty_cash, forbidden otherwise)",
    )
    active: bool = Field(default=True, description="Inactive boxes cannot start counting sessions")


class StatementJournalResponse(BaseModel):
    """One Tryton statement journal (a daybook a cash box can register on)."""

    id: int = Field(description="Engine id of the statement journal")
    name: str = Field(description="Journal name")
    account_id: int = Field(description="Engine id of the journal's bank/cash account")
    account_code: str = Field(description="Code of the journal's account")
    account_name: str = Field(description="Name of the journal's account")
    currency_code: str = Field(description="Journal currency code (IRR)")
    validation: str = Field(description="Validation type: balance | amount | number_of_lines")
    active: bool = Field(description="Whether the journal is active")

    @classmethod
    def from_view(cls, view: StatementJournalView) -> StatementJournalResponse:
        """Build a response from the engine's statement-journal view."""
        return cls(
            id=view.ref.engine_id,
            name=view.name,
            account_id=view.account.engine_id,
            account_code=view.account_code,
            account_name=view.account_name,
            currency_code=view.currency_code,
            validation=view.validation,
            active=view.active,
        )


class DenominationRequest(BaseModel):
    """One denomination line of a counted amount."""

    denomination_irr: int = Field(gt=0, description="Banknote/coin value in IRR")
    count: int = Field(gt=0, description="How many of this denomination were counted")


class DenominationResponse(BaseModel):
    """One denomination line of a counted amount."""

    denomination_irr: int = Field(description="Banknote/coin value in IRR")
    count: int = Field(description="How many of this denomination were counted")
    total_irr: int = Field(description="Value of this denomination line in IRR")


class CountSessionResponse(BaseModel):
    """One supervised cash-counting session."""

    id: str = Field(description="Session id")
    company_id: str = Field(description="HesabYar engine_companies row id")
    cash_box_id: str = Field(description="Cash box id")
    cashier: str = Field(description="Cashier who counts")
    supervisor: str = Field(description="Supervisor who witnesses")
    status: str = Field(description="Status: in_progress | awaiting_supervisor | awaiting_manager | archived")
    book_balance_irr: int = Field(description="Book balance snapshot in IRR (read-only from Tryton)")
    counted_amount_irr: int | None = Field(description="Physically counted amount in IRR (null until entered)")
    variance_irr: int | None = Field(
        description="Counted minus book (negative = shortage, positive = overage; null until counted)"
    )
    denominations: list[DenominationResponse] = Field(description="Optional denomination breakdown")
    minutes_text: str | None = Field(description="Signed-minutes body (Persian)")
    minutes_required: bool = Field(description="True when the variance is non-zero (signed minutes required)")
    minutes_signed: bool = Field(description="True when cashier + supervisor + manager have all signed the minutes")
    witnessed_by: str | None = Field(description="Supervisor who attended the count")
    witnessed_at: datetime | None = Field(description="When the supervisor attended (UTC)")
    minutes_cashier_signed_by: str | None = Field(description="Cashier who signed the minutes")
    minutes_cashier_signed_at: datetime | None = Field(description="When the cashier signed the minutes (UTC)")
    minutes_supervisor_signed_by: str | None = Field(description="Supervisor who signed the minutes")
    minutes_supervisor_signed_at: datetime | None = Field(description="When the supervisor signed the minutes (UTC)")
    minutes_manager_signed_by: str | None = Field(description="Manager who signed the minutes")
    minutes_manager_signed_at: datetime | None = Field(description="When the manager signed the minutes (UTC)")
    review_item_id: str | None = Field(description="Linked cashbox_variance review item id")
    draft_journal_id: int | None = Field(description="Engine id of the DRAFT variance journal")
    posted_journal_id: int | None = Field(description="Engine id of the variance journal once posted")
    notes: str | None = Field(description="Free-form session notes")
    created_at: datetime = Field(description="When the session started (UTC)")
    updated_at: datetime | None = Field(description="Last update (UTC)")

    @classmethod
    def from_session(cls, session: CountSession) -> CountSessionResponse:
        """Build a response from a domain counting session."""
        return cls(
            id=session.id,
            company_id=session.company_id,
            cash_box_id=session.cash_box_id,
            cashier=session.cashier,
            supervisor=session.supervisor,
            status=session.status.value,
            book_balance_irr=session.book_balance_irr,
            counted_amount_irr=session.counted_amount_irr,
            variance_irr=session.variance_irr,
            denominations=[
                DenominationResponse(
                    denomination_irr=line.denomination_irr,
                    count=line.count,
                    total_irr=line.total_irr,
                )
                for line in session.denominations
            ],
            minutes_text=session.minutes_text,
            minutes_required=session.minutes_required,
            minutes_signed=session.minutes_signed,
            witnessed_by=session.witnessed_by,
            witnessed_at=session.witnessed_at,
            minutes_cashier_signed_by=session.minutes_cashier_signed_by,
            minutes_cashier_signed_at=session.minutes_cashier_signed_at,
            minutes_supervisor_signed_by=session.minutes_supervisor_signed_by,
            minutes_supervisor_signed_at=session.minutes_supervisor_signed_at,
            minutes_manager_signed_by=session.minutes_manager_signed_by,
            minutes_manager_signed_at=session.minutes_manager_signed_at,
            review_item_id=session.review_item_id,
            draft_journal_id=session.draft_journal_id,
            posted_journal_id=session.posted_journal_id,
            notes=session.notes,
            created_at=session.created_at,
            updated_at=session.updated_at,
        )


class StartCountSessionRequest(BaseModel):
    """Start a counting session on a cash box (the box id comes from the path)."""

    company_id: str = Field(description="API company id (HesabYar row id or engine id)")
    cashier: str = Field(description="Cashier (actor name/id) who counts")
    supervisor: str = Field(description="Supervisor (actor name/id) who witnesses — must differ from the cashier")


class EnterCountRequest(BaseModel):
    """Enter (or correct) the counted amount of a session."""

    counted_amount_irr: int = Field(ge=0, description="Physically counted amount in IRR")
    denominations: list[DenominationRequest] = Field(
        default_factory=list,
        description="Optional denomination breakdown (must sum to the counted amount)",
    )
    actor: str = Field(description="Actor entering the count (must be the session's cashier)")


class SignSessionRequest(BaseModel):
    """Record one signature on a counting session."""

    role: str = Field(description="Signature role: cashier | supervisor | manager")
    actor: str = Field(description="Actor signing (must hold the role on this session)")
    minutes_text: str | None = Field(
        default=None,
        description="Minutes body (required with the cashier's signature when the variance is non-zero)",
    )


class PostVarianceJournalRequest(BaseModel):
    """Post a variance session's DRAFT journal after manager approval."""

    actor: str = Field(description="Actor posting the journal")


class RecountRequest(BaseModel):
    """Reopen a variance session for a recount, as the manager requested."""

    actor: str = Field(description="Actor reopening the session")


class CashBoxSettingsResponse(BaseModel):
    """The installation's cash-box policy settings."""

    variance_policy: str = Field(description="Variance policy: expense (a) | receivable_from_cashier (b)")
    material_variance_threshold_irr: int | None = Field(
        description="Material-variance threshold in IRR (null = no materiality split)"
    )
    count_reminder_days: int = Field(description="'Cash box uncounted for N days' alert threshold")
    count_schedule: str = Field(description="Count schedule: daily | weekly | period_end")
    supervisor_required: bool = Field(description="Internal control: the supervisor must differ from the cashier")
    shortage_settlement_hours: int = Field(description="Shortage-settlement deadline under policy (b), in hours")
    updated_at: datetime | None = Field(description="Last update (UTC)")

    @classmethod
    def from_settings(cls, settings: CashBoxSettings) -> CashBoxSettingsResponse:
        """Build a response from the domain settings."""
        return cls(
            variance_policy=settings.variance_policy.value,
            material_variance_threshold_irr=settings.material_variance_threshold_irr,
            count_reminder_days=settings.count_reminder_days,
            count_schedule=settings.count_schedule,
            supervisor_required=settings.supervisor_required,
            shortage_settlement_hours=settings.shortage_settlement_hours,
            updated_at=settings.updated_at,
        )


class UpdateCashBoxSettingsRequest(BaseModel):
    """Set the installation's cash-box policy settings (admin)."""

    variance_policy: str = Field(description="Variance policy: expense (a) | receivable_from_cashier (b)")
    material_variance_threshold_irr: int | None = Field(
        default=None,
        description="Material-variance threshold in IRR (null = no materiality split)",
    )
    count_reminder_days: int = Field(default=7, description="'Cash box uncounted for N days' alert threshold")
    count_schedule: str = Field(default="daily", description="Count schedule: daily | weekly | period_end")
    supervisor_required: bool = Field(
        default=True,
        description="Internal control: the supervisor must differ from the cashier",
    )
    shortage_settlement_hours: int = Field(
        default=24,
        description="Shortage-settlement deadline under policy (b), in hours",
    )


class CashBoxReportLineResponse(BaseModel):
    """One cash box's figures in a cash-box variance report."""

    cash_box_id: str = Field(description="Cash box id")
    cash_box_name_fa: str = Field(description="Cash-box name in Persian")
    cash_account_code: str = Field(description="Code of the box's cash account")
    opening_book_balance_irr: int = Field(description="Book balance at the period start (IRR, posted moves only)")
    closing_book_balance_irr: int = Field(description="Book balance at the period end (IRR, posted moves only)")
    receipts_irr: int = Field(description="Posted cash receipts in the period (IRR)")
    payments_irr: int = Field(description="Posted cash payments in the period (IRR)")
    session_count: int = Field(description="Counting sessions in the period")
    shortage_irr: int = Field(description="Total shortage in the period (IRR, absolute)")
    overage_irr: int = Field(description="Total overage in the period (IRR, absolute)")
    net_variance_irr: int = Field(description="Overage minus shortage (signed IRR)")
    recorded_sales_irr: int = Field(description="Recorded sales in the period (IRR, posted customer invoices)")
    possible_unrecorded_sales_irr: int = Field(
        description="Net overage floored at zero — the unrecorded-sales indicator (IRR)"
    )

    @classmethod
    def from_line(cls, line: CashBoxReportLine) -> CashBoxReportLineResponse:
        """Build a response from a domain report line."""
        return cls(
            cash_box_id=line.cash_box_id,
            cash_box_name_fa=line.cash_box_name_fa,
            cash_account_code=line.cash_account_code,
            opening_book_balance_irr=line.opening_book_balance_irr,
            closing_book_balance_irr=line.closing_book_balance_irr,
            receipts_irr=line.receipts_irr,
            payments_irr=line.payments_irr,
            session_count=line.session_count,
            shortage_irr=line.shortage_irr,
            overage_irr=line.overage_irr,
            net_variance_irr=line.net_variance_irr,
            recorded_sales_irr=line.recorded_sales_irr,
            possible_unrecorded_sales_irr=line.possible_unrecorded_sales_irr,
        )


class CashBoxReportResponse(BaseModel):
    """A cash-box vs sales variance report (spec §11.1 provenance)."""

    company_id: str = Field(description="HesabYar engine_companies row id")
    date_from: date = Field(description="Period start (Gregorian)")
    date_to: date = Field(description="Period end (Gregorian)")
    lines: list[CashBoxReportLineResponse] = Field(description="One line per cash box")
    total_shortage_irr: int = Field(description="Total shortage across the report's boxes (IRR)")
    total_overage_irr: int = Field(description="Total overage across the report's boxes (IRR)")
    total_possible_unrecorded_sales_irr: int = Field(
        description="Total unrecorded-sales indicator across the report's boxes (IRR)"
    )
    generated_at: datetime = Field(description="When the report was generated (UTC)")
    source: str = Field(description="Engine reads the figures came from")
    sales_source: str = Field(description="What the sales side is (tryton_invoices until the 5ب store link lands)")
    report_version: str = Field(description="Report version")
    calculation_version: str = Field(description="Calculation version")

    @classmethod
    def from_report(cls, report: CashBoxReport) -> CashBoxReportResponse:
        """Build a response from a domain report."""
        return cls(
            company_id=report.company_id,
            date_from=report.date_from,
            date_to=report.date_to,
            lines=[CashBoxReportLineResponse.from_line(line) for line in report.lines],
            total_shortage_irr=report.total_shortage_irr,
            total_overage_irr=report.total_overage_irr,
            total_possible_unrecorded_sales_irr=report.total_possible_unrecorded_sales_irr,
            generated_at=report.generated_at,
            source=report.source,
            sales_source=report.sales_source,
            report_version=report.report_version,
            calculation_version=report.calculation_version,
        )
