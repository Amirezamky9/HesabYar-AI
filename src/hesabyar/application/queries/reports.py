"""Financial-report query use cases over the accounting engine port (US-403, US-550).

REST, MCP and workers all call this service instead of the engine port
directly, so report reads have one implementation and one error
contract (design-eng spec §6.2/§6.3).

Every method verifies the company first (the engine answers an unknown
company id with an empty result, so an empty report always means a real
company with no posted moves in the period) and stamps the §11.1
freshness block on the view before returning it. A reversed range
raises ``ValueError`` (the route maps it to a typed 422
``VALIDATION_ERROR``).

Prior-period comparison defaults (when the caller does not pass an
explicit comparison): the same Jalali date / period one Jalali year
earlier — the comparison a Persian accountant expects, and the same
default Tryton's own comparison contexts use (one year back).
"""

from __future__ import annotations

import datetime
from dataclasses import replace
from typing import Any, TypeVar

import jdatetime

from hesabyar.application.errors import translate_port_errors
from hesabyar.application.queries.companies import CompanyQueryService
from hesabyar.domain.accounting.ports import AccountingEnginePort
from hesabyar.domain.accounting.refs import AccountRef, CompanyRef, PartyRef
from hesabyar.domain.accounting.value_objects import (
    BalanceSheetView,
    CashFlowView,
    GeneralLedgerView,
    IncomeStatementView,
    NotesBalanceView,
    ReportFreshness,
    TrialBalanceView,
)
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.reports.statements import (
    CALCULATION_VERSIONS,
    ENGINE_VERSION,
    REPORT_VERSION,
)

_ViewT = TypeVar(
    "_ViewT",
    TrialBalanceView,
    GeneralLedgerView,
    BalanceSheetView,
    IncomeStatementView,
    CashFlowView,
    NotesBalanceView,
)


def _one_jalali_year_earlier(day: FiscalDate) -> FiscalDate:
    """The same Jalali date one Jalali year earlier (Esfand 30 clamps to 29)."""
    jalali = day.jalali_date
    try:
        earlier = jdatetime.date(jalali.year - 1, jalali.month, jalali.day)
    except ValueError:
        # Esfand 30 in a leap year: the previous year has 29 days.
        earlier = jdatetime.date(jalali.year - 1, jalali.month, jalali.day - 1)
    return FiscalDate.from_gregorian(earlier.togregorian())


class ReportQueryService:
    """Read-only financial-report queries over an engine-neutral port."""

    def __init__(self, engine: AccountingEnginePort) -> None:
        self._engine = engine
        self._companies = CompanyQueryService(engine)

    def _stamp(
        self,
        view: _ViewT,
        report_key: str,
        company: CompanyRef,
        period_start: FiscalDate | None,
        period_end: FiscalDate | None,
        reproducibility: dict[str, Any] | None = None,
    ) -> _ViewT:
        """Attach the §11.1 freshness block to a port view.

        Live reads have no older source timestamp: the engine is read
        at generation time, so ``source_timestamp`` equals
        ``generated_at`` — documented on the freshness model itself.
        """
        now = datetime.datetime.now(datetime.UTC)
        base: dict[str, Any] = {
            "engine": "tryton",
            "engine_version": ENGINE_VERSION,
            "source_endpoint": view.source_endpoint,
            "company_id": str(company.engine_id),
        }
        if period_start is not None:
            base["period_start"] = period_start.gregorian_date.isoformat()
        if period_end is not None:
            base["period_end"] = period_end.gregorian_date.isoformat()
        if reproducibility:
            base.update(reproducibility)
        freshness = ReportFreshness(
            source=f"tryton:{view.source_endpoint}",
            source_timestamp=now,
            generated_at=now,
            company_id=str(company.engine_id),
            period_start=period_start,
            period_end=period_end,
            report_version=REPORT_VERSION,
            calculation_version=CALCULATION_VERSIONS[report_key],
            reproducibility=base,
        )
        return replace(view, freshness=freshness)

    def get_trial_balance(
        self,
        company: CompanyRef,
        date_from: datetime.date,
        date_to: datetime.date,
    ) -> TrialBalanceView:
        """Return the company's trial balance over posted moves only.

        A missing company raises :class:`CompanyNotFoundError`: the
        engine answers an unknown company id with an empty result, so
        the company is verified first and an empty trial balance always
        means a real company with no posted moves in the period.
        """
        if date_to < date_from:
            raise ValueError("date_to must be on or after date_from.")
        self._companies.get_company(company)
        with translate_port_errors():
            view = self._engine.get_trial_balance(
                company,
                FiscalDate(date_from),
                FiscalDate(date_to),
            )
        return self._stamp(view, "trial_balance", company, FiscalDate(date_from), FiscalDate(date_to))

    def get_general_ledger(
        self,
        company: CompanyRef,
        date_from: datetime.date,
        date_to: datetime.date,
        account: AccountRef | None = None,
    ) -> GeneralLedgerView:
        """Return the company's general ledger over posted moves only.

        With ``account`` given, only that account is returned, with
        its lines; otherwise every account with posted activity in
        the period is returned with totals only (summary mode).
        """
        if date_to < date_from:
            raise ValueError("date_to must be on or after date_from.")
        self._companies.get_company(company)
        with translate_port_errors():
            view = self._engine.get_general_ledger(
                company,
                FiscalDate(date_from),
                FiscalDate(date_to),
                account,
            )
        return self._stamp(
            view,
            "general_ledger",
            company,
            FiscalDate(date_from),
            FiscalDate(date_to),
            {"account_id": account.engine_id if account is not None else None},
        )

    def get_balance_sheet(
        self,
        company: CompanyRef,
        as_of: datetime.date,
        comparison_as_of: datetime.date | None = None,
    ) -> BalanceSheetView:
        """Return the company's balance sheet at ``as_of``.

        Without an explicit comparison date, the comparison is the
        same Jalali date one year earlier.
        """
        self._companies.get_company(company)
        as_of_date = FiscalDate(as_of)
        comparison = (
            FiscalDate(comparison_as_of)
            if comparison_as_of is not None
            else _one_jalali_year_earlier(as_of_date)
        )
        with translate_port_errors():
            view = self._engine.get_balance_sheet(company, as_of_date, comparison)
        return self._stamp(
            view,
            "balance_sheet",
            company,
            as_of_date,
            as_of_date,
            {"comparison_as_of": comparison.gregorian_date.isoformat()},
        )

    def get_income_statement(
        self,
        company: CompanyRef,
        date_from: datetime.date,
        date_to: datetime.date,
        comparison_from: datetime.date | None = None,
        comparison_to: datetime.date | None = None,
    ) -> IncomeStatementView:
        """Return the company's income statement over a period.

        Without an explicit comparison period, the comparison is the
        same Jalali period one year earlier.
        """
        if date_to < date_from:
            raise ValueError("date_to must be on or after date_from.")
        if (comparison_from is None) != (comparison_to is None):
            raise ValueError("comparison_from and comparison_to go together.")
        if (
            comparison_from is not None
            and comparison_to is not None
            and comparison_to < comparison_from
        ):
            raise ValueError("comparison_to must be on or after comparison_from.")
        self._companies.get_company(company)
        from_date = FiscalDate(date_from)
        to_date = FiscalDate(date_to)
        if comparison_from is None or comparison_to is None:
            comparison_from_date = _one_jalali_year_earlier(from_date)
            comparison_to_date = _one_jalali_year_earlier(to_date)
        else:
            comparison_from_date = FiscalDate(comparison_from)
            comparison_to_date = FiscalDate(comparison_to)
        with translate_port_errors():
            view = self._engine.get_income_statement(
                company,
                from_date,
                to_date,
                comparison_from_date,
                comparison_to_date,
            )
        return self._stamp(
            view,
            "income_statement",
            company,
            from_date,
            to_date,
            {
                "comparison_from": comparison_from_date.gregorian_date.isoformat(),
                "comparison_to": comparison_to_date.gregorian_date.isoformat(),
            },
        )

    def get_cash_flow(
        self,
        company: CompanyRef,
        date_from: datetime.date,
        date_to: datetime.date,
    ) -> CashFlowView:
        """Return the company's direct-method cash-flow statement."""
        if date_to < date_from:
            raise ValueError("date_to must be on or after date_from.")
        self._companies.get_company(company)
        with translate_port_errors():
            view = self._engine.get_cash_flow(
                company,
                FiscalDate(date_from),
                FiscalDate(date_to),
            )
        return self._stamp(view, "cash_flow", company, FiscalDate(date_from), FiscalDate(date_to))

    def get_notes_balance(
        self,
        company: CompanyRef,
        as_of: datetime.date,
        instrument: str = "both",
        party: PartyRef | None = None,
    ) -> NotesBalanceView:
        """Return notes receivable/payable balances with aging (cheque report 7)."""
        if instrument not in {"both", "receivable", "payable"}:
            raise ValueError(
                f"instrument must be 'both', 'receivable' or 'payable', got {instrument!r}."
            )
        self._companies.get_company(company)
        with translate_port_errors():
            view = self._engine.get_notes_balance(
                company,
                FiscalDate(as_of),
                instrument,
                party,
            )
        return self._stamp(
            view,
            "notes_balance",
            company,
            FiscalDate(as_of),
            FiscalDate(as_of),
            {
                "instrument": instrument,
                "party_id": party.engine_id if party is not None else None,
            },
        )
