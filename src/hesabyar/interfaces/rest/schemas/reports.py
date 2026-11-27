"""Response schemas for the financial-report endpoints (US-403, US-550).

Responses map from the domain views — no ORM object ever leaks. Every
figure carries its provenance: the source endpoint it came from, the
snapshot id, the ruleset version and the as-of date. Amounts are
integer Rials (no floats anywhere in financial paths).

Every US-550 report response carries a ``freshness`` block (§11.1):
source, source timestamp, generated-at, company + period, report +
calculation version, freshness indicator and reproducibility metadata.
The block is stamped by the report query service; a view without one
is a programming error and ``from_view`` fails loudly rather than
fabricating metadata.
"""

from __future__ import annotations

from datetime import date, datetime
from datetime import date as _date
from typing import Any

from pydantic import BaseModel, Field, model_validator

from hesabyar.domain.accounting.value_objects import (
    BalanceSheetView,
    CashFlowView,
    GeneralLedgerView,
    IncomeStatementView,
    NotesBalanceRowView,
    NotesBalanceView,
    ReportFreshness,
    StatementSectionView,
    TrialBalanceView,
)
from hesabyar.domain.cash_forecast.models import (
    CalendarDay,
    CalendarMonth,
    CashForecastTotals,
    CashForecastView,
    ForecastItem,
    HorizonBucket,
)
from hesabyar.domain.reports.models import ReportValidationRun
from hesabyar.interfaces.rest.schemas.standards import ValidationRunResponse


class ReportFreshnessResponse(BaseModel):
    """§11.1 freshness metadata attached to every financial report."""

    source: str = Field(description="Engine read the figures came from, e.g. 'tryton:account.move.line (Tryton REST)'")
    source_timestamp: datetime = Field(
        description="When the source data was read (UTC; equals generated_at for live reads)"
    )
    generated_at: datetime = Field(description="When the response was generated (UTC)")
    company_id: str = Field(description="Engine company id the report was read for")
    period_start: date | None = Field(
        description="Start of the report period (the as-of date for point-in-time reports)"
    )
    period_end: date | None = Field(description="End of the report period (the as-of date for point-in-time reports)")
    report_version: str = Field(description="Version of the report definition")
    calculation_version: str = Field(description="Version of the calculation rule that produced the figures")
    freshness_indicator: str = Field(
        description="Always 'live': the engine is read live on every request, never from a HesabYar cache"
    )
    reproducibility: dict[str, Any] = Field(
        description="Exact read parameters (engine, version, source, company, period, filters) to repeat the read"
    )

    @classmethod
    def from_model(cls, freshness: ReportFreshness) -> ReportFreshnessResponse:
        """Build a freshness response from the domain model."""
        return cls(
            source=freshness.source,
            source_timestamp=freshness.source_timestamp,
            generated_at=freshness.generated_at,
            company_id=freshness.company_id,
            period_start=freshness.period_start.gregorian_date if freshness.period_start else None,
            period_end=freshness.period_end.gregorian_date if freshness.period_end else None,
            report_version=freshness.report_version,
            calculation_version=freshness.calculation_version,
            freshness_indicator=freshness.freshness_indicator,
            reproducibility=dict(freshness.reproducibility),
        )


def _require_freshness(view: Any) -> ReportFreshnessResponse:
    """The freshness block of a stamped view, or a loud failure."""
    freshness = view.freshness
    if freshness is None:
        raise ValueError(
            "Report views must be stamped by the report query service before serialization."
        )
    return ReportFreshnessResponse.from_model(freshness)


class TrialBalanceLineResponse(BaseModel):
    """One account's totals over posted moves in the trial-balance period."""

    account_id: int = Field(description="Engine account id")
    code: str = Field(description="Account code, e.g. '1.1.1'")
    name: str = Field(description="Account name")
    total_debit: int = Field(description="Total debits over posted moves in the period (IRR)")
    total_credit: int = Field(description="Total credits over posted moves in the period (IRR)")
    balance: int = Field(description="Signed balance: debit minus credit (IRR)")


class TrialBalanceResponse(BaseModel):
    """A trial balance read from POSTED moves only, with source metadata."""

    company_id: str = Field(description="Engine company id the trial balance was read for")
    date_from: date = Field(description="Start of the trial-balance period")
    date_to: date = Field(description="End of the trial-balance period")
    as_of_date: date = Field(description="As-of date of the trial balance (the period end)")
    lines: list[TrialBalanceLineResponse] = Field(description="Per-account totals, ordered by account code")
    total_debit: int = Field(description="Total debits over posted moves in the period (IRR)")
    total_credit: int = Field(description="Total credits over posted moves in the period (IRR)")
    balanced: bool = Field(description="True when total debits equal total credits")
    move_count: int = Field(description="Posted moves contributing lines in the period")
    line_count: int = Field(description="Posted move lines aggregated in the period")
    source_endpoint: str = Field(description="Engine read the figures came from")
    freshness: ReportFreshnessResponse = Field(description="§11.1 freshness metadata")

    @classmethod
    def from_view(cls, view: TrialBalanceView, company_id: str) -> TrialBalanceResponse:
        """Build a trial-balance response from the domain view."""
        return cls(
            company_id=company_id,
            date_from=view.date_from.gregorian_date,
            date_to=view.date_to.gregorian_date,
            as_of_date=view.as_of_date.gregorian_date,
            lines=[
                TrialBalanceLineResponse(
                    account_id=line.account.engine_id,
                    code=line.code,
                    name=line.name,
                    total_debit=line.total_debit.amount,
                    total_credit=line.total_credit.amount,
                    balance=line.balance.amount,
                )
                for line in view.lines
            ],
            total_debit=view.total_debit.amount,
            total_credit=view.total_credit.amount,
            balanced=view.is_balanced,
            move_count=view.move_count,
            line_count=view.line_count,
            source_endpoint=view.source_endpoint,
            freshness=_require_freshness(view),
        )


class GeneralLedgerLineResponse(BaseModel):
    """One posted move line in a general-ledger account section."""

    move_id: int = Field(description="Engine move id")
    move_number: str = Field(description="Engine move number ('' when the move has none)")
    date: _date = Field(description="Effective date of the move")
    description: str = Field(description="Line description, falling back to the move description")
    debit: int = Field(description="Debit (IRR)")
    credit: int = Field(description="Credit (IRR)")
    balance: int = Field(description="Running balance (debit minus credit) after this line (IRR)")


class GeneralLedgerAccountResponse(BaseModel):
    """One account's general-ledger section: opening balance + lines."""

    account_id: int = Field(description="Engine account id")
    code: str = Field(description="Account code, e.g. '1.1.1'")
    name: str = Field(description="Account name")
    opening_balance: int = Field(description="Balance at the day before the period (IRR)")
    total_debit: int = Field(description="Total debits over posted moves in the period (IRR)")
    total_credit: int = Field(description="Total credits over posted moves in the period (IRR)")
    closing_balance: int = Field(description="Opening balance plus the period's net activity (IRR)")
    lines: list[GeneralLedgerLineResponse] = Field(
        description="The account's posted lines, oldest first; empty in summary mode (no single account requested)"
    )


class GeneralLedgerResponse(BaseModel):
    """A general ledger over POSTED moves only, with §11.1 freshness metadata."""

    company_id: str = Field(description="Engine company id the ledger was read for")
    date_from: date = Field(description="Start of the ledger period")
    date_to: date = Field(description="End of the ledger period")
    accounts: list[GeneralLedgerAccountResponse] = Field(description="Per-account sections, ordered by account code")
    total_debit: int = Field(description="Total debits over posted moves in the period (IRR)")
    total_credit: int = Field(description="Total credits over posted moves in the period (IRR)")
    move_count: int = Field(description="Posted moves contributing lines (detail mode only; 0 in summary mode)")
    line_count: int = Field(description="Posted move lines in the ledger (detail mode only; 0 in summary mode)")
    source_endpoint: str = Field(description="Engine read the figures came from")
    freshness: ReportFreshnessResponse = Field(description="§11.1 freshness metadata")

    @classmethod
    def from_view(cls, view: GeneralLedgerView, company_id: str) -> GeneralLedgerResponse:
        """Build a general-ledger response from the domain view."""
        return cls(
            company_id=company_id,
            date_from=view.date_from.gregorian_date,
            date_to=view.date_to.gregorian_date,
            accounts=[
                GeneralLedgerAccountResponse(
                    account_id=section.account.engine_id,
                    code=section.code,
                    name=section.name,
                    opening_balance=section.opening_balance.amount,
                    total_debit=section.total_debit.amount,
                    total_credit=section.total_credit.amount,
                    closing_balance=section.closing_balance.amount,
                    lines=[
                        GeneralLedgerLineResponse(
                            move_id=line.move_id,
                            move_number=line.move_number,
                            date=line.date.gregorian_date,
                            description=line.description,
                            debit=line.debit.amount,
                            credit=line.credit.amount,
                            balance=line.balance.amount,
                        )
                        for line in section.lines
                    ],
                )
                for section in view.accounts
            ],
            total_debit=view.total_debit.amount,
            total_credit=view.total_credit.amount,
            move_count=view.move_count,
            line_count=view.line_count,
            source_endpoint=view.source_endpoint,
            freshness=_require_freshness(view),
        )


class StatementLineResponse(BaseModel):
    """One row of a financial-statement section."""

    kind: str = Field(description="'type' (an account-type subtotal) or 'account' (one account's figure)")
    ref_id: int = Field(description="Engine type id (kind 'type') or account id (kind 'account')")
    label: str = Field(description="Type name or account name")
    code: str | None = Field(description="Account code (kind 'account' only)")
    depth: int = Field(description="Tree depth for indentation (0 = the section's top-level rows)")
    amount: int = Field(
        description="Statement amount (IRR): positive magnitudes, except the result line (negative for a net loss)"
    )
    amount_cmp: int | None = Field(description="Same figure for the comparison period (null when no comparison)")


class StatementSectionResponse(BaseModel):
    """One section of a financial statement."""

    key: str = Field(description="Stable section key ('assets', 'liabilities', 'equity', 'revenue', 'expenses')")
    title: str = Field(description="English type name the section is rooted at")
    lines: list[StatementLineResponse] = Field(description="Section rows in tree order")
    total: int = Field(description="Section total (IRR, positive magnitude)")
    total_cmp: int | None = Field(description="Section total for the comparison period (IRR)")


def _section_response(section: StatementSectionView) -> StatementSectionResponse:
    return StatementSectionResponse(
        key=section.key,
        title=section.title,
        lines=[
            StatementLineResponse(
                kind=line.kind,
                ref_id=line.ref_id,
                label=line.label,
                code=line.code,
                depth=line.depth,
                amount=line.amount.amount,
                amount_cmp=line.amount_cmp.amount if line.amount_cmp is not None else None,
            )
            for line in section.lines
        ],
        total=section.total.amount,
        total_cmp=section.total_cmp.amount if section.total_cmp is not None else None,
    )


class BalanceSheetResponse(BaseModel):
    """A balance sheet at a point in time, with prior-period comparison."""

    company_id: str = Field(description="Engine company id the balance sheet was read for")
    as_of: date = Field(description="As-of date of the balance sheet")
    comparison_as_of: date | None = Field(description="Comparison date (same Jalali date one year earlier by default)")
    sections: list[StatementSectionResponse] = Field(description="Assets / liabilities / equity sections")
    total_assets: int = Field(description="Total assets (IRR)")
    total_liabilities: int = Field(description="Total liabilities (IRR)")
    total_equity: int = Field(description="Total equity, including the current-period result (IRR)")
    total_assets_cmp: int | None = Field(description="Total assets at the comparison date (IRR)")
    total_liabilities_cmp: int | None = Field(description="Total liabilities at the comparison date (IRR)")
    total_equity_cmp: int | None = Field(description="Total equity at the comparison date (IRR)")
    balanced: bool = Field(description="True when total assets equal total liabilities plus total equity")
    balanced_cmp: bool | None = Field(description="Same check for the comparison date")
    source_endpoint: str = Field(description="Engine read the figures came from")
    freshness: ReportFreshnessResponse = Field(description="§11.1 freshness metadata")

    @classmethod
    def from_view(cls, view: BalanceSheetView, company_id: str) -> BalanceSheetResponse:
        """Build a balance-sheet response from the domain view."""
        return cls(
            company_id=company_id,
            as_of=view.as_of.gregorian_date,
            comparison_as_of=view.comparison_as_of.gregorian_date if view.comparison_as_of else None,
            sections=[_section_response(section) for section in view.sections],
            total_assets=view.total_assets.amount,
            total_liabilities=view.total_liabilities.amount,
            total_equity=view.total_equity.amount,
            total_assets_cmp=view.total_assets_cmp.amount if view.total_assets_cmp is not None else None,
            total_liabilities_cmp=view.total_liabilities_cmp.amount if view.total_liabilities_cmp is not None else None,
            total_equity_cmp=view.total_equity_cmp.amount if view.total_equity_cmp is not None else None,
            balanced=view.balanced,
            balanced_cmp=view.balanced_cmp,
            source_endpoint=view.source_endpoint,
            freshness=_require_freshness(view),
        )


class MonthlyRevenueExpenseResponse(BaseModel):
    """One Jalali month's net revenue and expense (P&L chart data)."""

    jalali_month: str = Field(description="Jalali year-month, e.g. '1405-08'")
    month_start: date = Field(description="Gregorian start of the month (clipped to the period)")
    month_end: date = Field(description="Gregorian end of the month (clipped to the period)")
    revenue: int = Field(
        description="Net credit on revenue accounts in the month (IRR; negative when reversals exceed activity)"
    )
    expense: int = Field(
        description="Net debit on expense accounts in the month (IRR; negative when reversals exceed activity)"
    )


class IncomeStatementResponse(BaseModel):
    """An income statement (P&L) over a period, with prior-period comparison."""

    company_id: str = Field(description="Engine company id the income statement was read for")
    date_from: date = Field(description="Start of the income-statement period")
    date_to: date = Field(description="End of the income-statement period")
    comparison_from: date | None = Field(
        description="Start of the comparison period (same Jalali period one year earlier by default)"
    )
    comparison_to: date | None = Field(description="End of the comparison period")
    sections: list[StatementSectionResponse] = Field(description="Revenue / expenses sections")
    total_revenue: int = Field(description="Total revenue (IRR, positive magnitude)")
    total_expense: int = Field(description="Total expense (IRR, positive magnitude)")
    net_income: int = Field(description="Net income: revenue minus expense (IRR; negative for a net loss)")
    total_revenue_cmp: int | None = Field(description="Total revenue for the comparison period (IRR)")
    total_expense_cmp: int | None = Field(description="Total expense for the comparison period (IRR)")
    net_income_cmp: int | None = Field(description="Net income for the comparison period (IRR)")
    monthly: list[MonthlyRevenueExpenseResponse] = Field(
        description="Jalali-month revenue/expense breakdown for the chart"
    )
    monthly_ties_out: bool = Field(description="True when the monthly figures sum to the statement totals")
    source_endpoint: str = Field(description="Engine read the figures came from")
    freshness: ReportFreshnessResponse = Field(description="§11.1 freshness metadata")

    @classmethod
    def from_view(cls, view: IncomeStatementView, company_id: str) -> IncomeStatementResponse:
        """Build an income-statement response from the domain view."""
        return cls(
            company_id=company_id,
            date_from=view.date_from.gregorian_date,
            date_to=view.date_to.gregorian_date,
            comparison_from=view.comparison_from.gregorian_date if view.comparison_from else None,
            comparison_to=view.comparison_to.gregorian_date if view.comparison_to else None,
            sections=[_section_response(section) for section in view.sections],
            total_revenue=view.total_revenue.amount,
            total_expense=view.total_expense.amount,
            net_income=view.net_income.amount,
            total_revenue_cmp=view.total_revenue_cmp.amount if view.total_revenue_cmp is not None else None,
            total_expense_cmp=view.total_expense_cmp.amount if view.total_expense_cmp is not None else None,
            net_income_cmp=view.net_income_cmp.amount if view.net_income_cmp is not None else None,
            monthly=[
                MonthlyRevenueExpenseResponse(
                    jalali_month=month.jalali_month,
                    month_start=month.month_start.gregorian_date,
                    month_end=month.month_end.gregorian_date,
                    revenue=month.revenue.amount,
                    expense=month.expense.amount,
                )
                for month in view.monthly
            ],
            monthly_ties_out=view.monthly_ties_out,
            source_endpoint=view.source_endpoint,
            freshness=_require_freshness(view),
        )


class CashFlowSectionResponse(BaseModel):
    """One cash-flow section (operating / investing / financing)."""

    key: str = Field(description="Stable section key ('operating', 'investing', 'financing')")
    title: str = Field(description="English section title")
    inflow: int = Field(description="Cash received in the section (IRR, positive magnitude)")
    outflow: int = Field(description="Cash paid in the section (IRR, positive magnitude)")
    net: int = Field(description="Net cash flow of the section: inflow minus outflow (IRR)")
    line_count: int = Field(description="Cash move lines classified into the section")


class CashFlowResponse(BaseModel):
    """A direct-method cash-flow statement over a period."""

    company_id: str = Field(description="Engine company id the cash-flow statement was read for")
    date_from: date = Field(description="Start of the cash-flow period")
    date_to: date = Field(description="End of the cash-flow period")
    opening_cash: int = Field(description="Cash at the day before the period (IRR)")
    closing_cash: int = Field(description="Cash at the period end (IRR)")
    sections: list[CashFlowSectionResponse] = Field(description="Operating / investing / financing sections")
    net_change: int = Field(description="Net change in cash: closing minus opening (IRR)")
    ties_out: bool = Field(
        description="True when opening + operating + investing + financing equals closing with no unclassified lines"
    )
    cash_accounts: list[str] = Field(description="Codes of the cash accounts read")
    transfer_count: int = Field(description="Cash-to-cash transfers excluded from every section")
    unclassified_count: int = Field(description="Cash lines whose counterpart could not be classified")
    unclassified_total: int = Field(description="Net amount of the unclassified lines (IRR)")
    source_endpoint: str = Field(description="Engine read the figures came from")
    freshness: ReportFreshnessResponse = Field(description="§11.1 freshness metadata")

    @classmethod
    def from_view(cls, view: CashFlowView, company_id: str) -> CashFlowResponse:
        """Build a cash-flow response from the domain view."""
        return cls(
            company_id=company_id,
            date_from=view.date_from.gregorian_date,
            date_to=view.date_to.gregorian_date,
            opening_cash=view.opening_cash.amount,
            closing_cash=view.closing_cash.amount,
            sections=[
                CashFlowSectionResponse(
                    key=section.key,
                    title=section.title,
                    inflow=section.inflow.amount,
                    outflow=section.outflow.amount,
                    net=section.net.amount,
                    line_count=section.line_count,
                )
                for section in view.sections
            ],
            net_change=view.net_change.amount,
            ties_out=view.ties_out,
            cash_accounts=list(view.cash_accounts),
            transfer_count=view.transfer_count,
            unclassified_count=view.unclassified_count,
            unclassified_total=view.unclassified_total.amount,
            source_endpoint=view.source_endpoint,
            freshness=_require_freshness(view),
        )


class NotesBalanceRowResponse(BaseModel):
    """One party's notes balance on one notes account (cheque report 7)."""

    party_id: int | None = Field(description="Engine party id (null for the totals row)")
    party_name: str = Field(description="Party name ('' for the totals row)")
    instrument: str = Field(description="'receivable' or 'payable'")
    account_code: str = Field(description="Notes account code ('1.2.4', '1.2.5' or '2.1.5'; '' for the totals row)")
    account_name: str = Field(description="Notes account name ('' for the totals row)")
    bucket_not_yet_due: int = Field(description="Notes maturing after the as-of date (IRR)")
    bucket_0_30: int = Field(description="Notes 0–29 days past maturity (IRR)")
    bucket_30_60: int = Field(description="Notes 30–59 days past maturity (IRR)")
    bucket_60_90: int = Field(description="Notes 60–89 days past maturity (IRR)")
    bucket_90_120: int = Field(description="Notes 90–119 days past maturity (IRR)")
    bucket_120_plus: int = Field(description="Notes 120+ days past maturity (IRR)")
    total: int = Field(description="Total open notes balance for this row (IRR)")
    line_count: int = Field(description="Open move lines in this row")


def _notes_row_response(row: NotesBalanceRowView) -> NotesBalanceRowResponse:
    return NotesBalanceRowResponse(
        party_id=row.party_id,
        party_name=row.party_name,
        instrument=row.instrument,
        account_code=row.account_code,
        account_name=row.account_name,
        bucket_not_yet_due=row.bucket_not_yet_due.amount,
        bucket_0_30=row.bucket_0_30.amount,
        bucket_30_60=row.bucket_30_60.amount,
        bucket_60_90=row.bucket_60_90.amount,
        bucket_90_120=row.bucket_90_120.amount,
        bucket_120_plus=row.bucket_120_plus.amount,
        total=row.total.amount,
        line_count=row.line_count,
    )


class NotesBalanceResponse(BaseModel):
    """Notes receivable/payable balances with aging (cheque report 7)."""

    company_id: str = Field(description="Engine company id the notes balance was read for")
    as_of: date = Field(description="As-of date the notes are aged at")
    instrument: str = Field(description="Instrument filter applied ('both', 'receivable' or 'payable')")
    rows: list[NotesBalanceRowResponse] = Field(
        description="Per-party, per-account rows, ordered by party name then account code"
    )
    totals: NotesBalanceRowResponse = Field(description="Bucket totals over all rows")
    accounts_read: list[str] = Field(
        description="Notes account codes that exist on the company (empty when the chart lacks them)"
    )
    source_endpoint: str = Field(description="Engine read the figures came from")
    freshness: ReportFreshnessResponse = Field(description="§11.1 freshness metadata")

    @classmethod
    def from_view(cls, view: NotesBalanceView, company_id: str) -> NotesBalanceResponse:
        """Build a notes-balance response from the domain view."""
        totals = view.totals
        if totals is None:
            raise ValueError("NotesBalanceView must carry totals.")
        return cls(
            company_id=company_id,
            as_of=view.as_of.gregorian_date,
            instrument=view.instrument,
            rows=[_notes_row_response(row) for row in view.rows],
            totals=_notes_row_response(totals),
            accounts_read=list(view.accounts_read),
            source_endpoint=view.source_endpoint,
            freshness=_require_freshness(view),
        )


class ReportProvenanceResponse(BaseModel):
    """Provenance attached to every figure of a report validation run."""

    snapshot_id: str = Field(description="Report snapshot id the run was built on")
    source_endpoint: str = Field(description="Engine read the figures came from")
    as_of_date: date = Field(description="As-of date of the snapshot (the trial-balance period end)")
    payload_hash: str = Field(description="SHA-256 of the trial-balance document (equals the run's input hash)")
    ruleset_version: str = Field(description="Ruleset version the run executed")
    total_debit: int = Field(description="Trial-balance total debits (IRR)")
    total_credit: int = Field(description="Trial-balance total credits (IRR)")
    balanced: bool = Field(description="True when the trial balance balanced")


class ReportValidationRunResponse(BaseModel):
    """A report validation run: the standards run plus its provenance."""

    run: ValidationRunResponse = Field(description="Validation run with per-rule results and coverage")
    provenance: ReportProvenanceResponse = Field(description="Snapshot provenance of every figure in the run")
    note: str | None = Field(
        default=None,
        description="Persian note when some rules could not be evaluated from a trial balance alone",
    )

    @classmethod
    def from_record(cls, record: ReportValidationRun) -> ReportValidationRunResponse:
        """Build a report-run response from the domain record."""
        snapshot = record.snapshot
        run = record.run
        note: str | None = None
        if run.rules_errored > 0:
            note = (
                "برخی قواعد به ارقام صورت‌های مالی (مانند ترازنامه و صورت سود و زیان) "
                "نیاز دارند که در تراز آزمایشی وجود ندارد؛ این قواعد با وضعیت «خطا» "
                "ثبت شده‌اند و به‌معنای قبولی نیستند."
            )
        return cls(
            run=ValidationRunResponse.from_record(run),
            provenance=ReportProvenanceResponse(
                snapshot_id=snapshot.snapshot_id,
                source_endpoint=snapshot.source_endpoint,
                as_of_date=snapshot.as_of_date,
                payload_hash=snapshot.payload_hash,
                ruleset_version=snapshot.ruleset_version or "unknown",
                total_debit=snapshot.total_debit,
                total_credit=snapshot.total_credit,
                balanced=snapshot.balanced,
            ),
            note=note,
        )


class CreateReportValidationRunRequest(BaseModel):
    """Request to validate a company's live trial balance against the standards."""

    company_id: str = Field(
        description="API company id (HesabYar row id or engine company id)",
    )
    period_start: date = Field(
        description="Start of the validated period (drives effective-date applicability)",
    )
    period_end: date = Field(description="End of the validated period")
    standard_codes: list[str] | None = Field(
        default=None,
        description="Standard codes to validate (e.g. ['1']); null means all standards in the registry",
    )

    @model_validator(mode="after")
    def _check_period_range(self) -> CreateReportValidationRunRequest:
        if self.period_end < self.period_start:
            raise ValueError("period_end must be on or after period_start.")
        return self


# -- cash forecast (US-553) ---------------------------------------------------


class ForecastItemResponse(BaseModel):
    """One expected cash movement on one date (قطعی or پیش‌بینی)."""

    date: _date = Field(description="Expected date (Gregorian, YYYY-MM-DD)")
    direction: str = Field(description="'inflow' or 'outflow'")
    bucket: str = Field(description="'confirmed' (قطعی) or 'forecast' (پیش‌بینی)")
    source: str = Field(
        description="Source record: settlement_tranche | installment_leg | cheque_received | "
        "cheque_issued | invoice_receivable | invoice_payable | packaging_purchase | expected_sale"
    )
    amount_irr: int = Field(description="Expected amount in Rial")
    ref: str = Field(description="Source record ref, e.g. 'tranche:<id>' or 'cheque:<sayad_id>'")
    label: str = Field(description="Human-readable caption (party / channel / product name)")

    @classmethod
    def from_model(cls, item: ForecastItem) -> ForecastItemResponse:
        return cls(
            date=item.date.gregorian_date,
            direction=item.direction.value,
            bucket=item.bucket.value,
            source=item.source.value,
            amount_irr=item.amount_irr,
            ref=item.ref,
            label=item.label,
        )


class CalendarDayResponse(BaseModel):
    """One calendar day's forecast totals (قطعی and پیش‌بینی split)."""

    date: _date = Field(description="Calendar day (Gregorian, YYYY-MM-DD)")
    inflow_confirmed_irr: int = Field(description="Expected قطعی inflow in Rial")
    outflow_confirmed_irr: int = Field(description="Expected قطعی outflow in Rial")
    inflow_forecast_irr: int = Field(description="Expected پیش‌بینی inflow in Rial")
    outflow_forecast_irr: int = Field(description="Expected پیش‌بینی outflow in Rial")
    item_count: int = Field(description="Forecast items on this day")

    @classmethod
    def from_model(cls, day: CalendarDay) -> CalendarDayResponse:
        return cls(
            date=day.date.gregorian_date,
            inflow_confirmed_irr=day.inflow_confirmed_irr,
            outflow_confirmed_irr=day.outflow_confirmed_irr,
            inflow_forecast_irr=day.inflow_forecast_irr,
            outflow_forecast_irr=day.outflow_forecast_irr,
            item_count=day.item_count,
        )


class CalendarMonthResponse(BaseModel):
    """One Jalali month of the forecast calendar."""

    month_jalali: str = Field(description="Jalali month key 'YYYY-MM'")
    days: list[CalendarDayResponse] = Field(description="Every day of the month in the range")

    @classmethod
    def from_model(cls, month: CalendarMonth) -> CalendarMonthResponse:
        return cls(
            month_jalali=month.month_jalali,
            days=[CalendarDayResponse.from_model(day) for day in month.days],
        )


class HorizonBucketResponse(BaseModel):
    """One 30/60/90/120-day horizon bucket (§11.2 settlement report)."""

    key: str = Field(description="Bucket key: overdue | 0_30 | 31_60 | 61_90 | 91_120 | beyond_120")
    from_days: int | None = Field(description="Inclusive lower day offset from the as-of date")
    to_days: int | None = Field(description="Inclusive upper day offset from the as-of date")
    inflow_confirmed_irr: int = Field(description="Expected قطعی inflow in Rial")
    outflow_confirmed_irr: int = Field(description="Expected قطعی outflow in Rial")
    inflow_forecast_irr: int = Field(description="Expected پیش‌بینی inflow in Rial")
    outflow_forecast_irr: int = Field(description="Expected پیش‌بینی outflow in Rial")
    item_count: int = Field(description="Forecast items in this bucket")

    @classmethod
    def from_model(cls, bucket: HorizonBucket) -> HorizonBucketResponse:
        return cls(
            key=bucket.key,
            from_days=bucket.from_days,
            to_days=bucket.to_days,
            inflow_confirmed_irr=bucket.inflow_confirmed_irr,
            outflow_confirmed_irr=bucket.outflow_confirmed_irr,
            inflow_forecast_irr=bucket.inflow_forecast_irr,
            outflow_forecast_irr=bucket.outflow_forecast_irr,
            item_count=bucket.item_count,
        )


class CashForecastTotalsResponse(BaseModel):
    """Whole-range totals, قطعی and پیش‌بینی kept apart."""

    inflow_confirmed_irr: int = Field(description="Expected قطعی inflow in Rial")
    outflow_confirmed_irr: int = Field(description="Expected قطعی outflow in Rial")
    inflow_forecast_irr: int = Field(description="Expected پیش‌بینی inflow in Rial")
    outflow_forecast_irr: int = Field(description="Expected پیش‌بینی outflow in Rial")
    item_count: int = Field(description="Forecast items in the range")

    @classmethod
    def from_model(cls, totals: CashForecastTotals) -> CashForecastTotalsResponse:
        return cls(
            inflow_confirmed_irr=totals.inflow_confirmed_irr,
            outflow_confirmed_irr=totals.outflow_confirmed_irr,
            inflow_forecast_irr=totals.inflow_forecast_irr,
            outflow_forecast_irr=totals.outflow_forecast_irr,
            item_count=totals.item_count,
        )


class CashForecastResponse(BaseModel):
    """The cash forecast for one company over one date range (US-553).

    Built ONLY from registered records: settlement tranches,
    installment legs, cheques (report 14 rule), posted invoices and
    packaging purchase requests. Collateral cheques appear in
    NEITHER column, ever. The پیش‌بینی column is empty until an
    expected-sales source exists — never invented.
    """

    company_id: str = Field(description="Engine company id the forecast was read for")
    date_from: date = Field(description="Start of the forecast range")
    date_to: date = Field(description="End of the forecast range")
    as_of: date = Field(description="As-of date the horizon buckets are counted from")
    items: list[ForecastItemResponse] = Field(description="Dated forecast items, oldest first")
    months: list[CalendarMonthResponse] = Field(description="The items grouped by Jalali month/day")
    horizon: list[HorizonBucketResponse] = Field(description="30/60/90/120-day horizon buckets")
    totals: CashForecastTotalsResponse = Field(description="Whole-range totals")
    source_counts: dict[str, int] = Field(description="Item counts per source (zero = no records)")
    skipped_undated: int = Field(
        description="Posted invoices with an unpaid balance that could not be dated at all"
    )
    freshness: ReportFreshnessResponse = Field(description="§11.1 freshness metadata")

    @classmethod
    def from_view(cls, view: CashForecastView, company_id: str) -> CashForecastResponse:
        """Build a cash-forecast response from the domain view."""
        return cls(
            company_id=company_id,
            date_from=view.date_from.gregorian_date,
            date_to=view.date_to.gregorian_date,
            as_of=view.as_of.gregorian_date,
            items=[ForecastItemResponse.from_model(item) for item in view.items],
            months=[CalendarMonthResponse.from_model(month) for month in view.months],
            horizon=[HorizonBucketResponse.from_model(bucket) for bucket in view.horizon],
            totals=CashForecastTotalsResponse.from_model(view.totals),
            source_counts=dict(view.source_counts),
            skipped_undated=view.skipped_undated,
            freshness=_require_freshness(view),
        )
