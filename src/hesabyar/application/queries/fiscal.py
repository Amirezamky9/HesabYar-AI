"""Fiscal-year, period and setup-status query use cases (US-203).

REST, MCP and workers all call these services instead of the engine port
directly, so fiscal reads and setup-state reads have one implementation
and one error contract (design-eng spec §6.2/§6.3, §17 Phase 2).

The services call ONLY the engine port (``list_fiscal_years`` /
``list_periods`` / ``get_chart_of_accounts``, all US-201/US-104 port
reads); they never touch the adapter or Tryton. A fiscal year or period
of another company looks exactly like one that does not exist.
"""

from __future__ import annotations

from collections.abc import Sequence

from hesabyar.application.errors import translate_port_errors
from hesabyar.application.queries.accounts import AccountQueryService
from hesabyar.application.queries.companies import CompanyQueryService
from hesabyar.domain.accounting.ports import AccountingEnginePort
from hesabyar.domain.accounting.refs import (
    CompanyRef,
    FiscalPeriodRef,
    FiscalYearRef,
)
from hesabyar.domain.accounting.value_objects import (
    CompanySetupStatus,
    FiscalPeriodView,
    FiscalYearView,
)
from hesabyar.domain.common.errors import (
    FiscalPeriodNotFoundError,
    FiscalYearNotFoundError,
)


class FiscalQueryService:
    """Read-only fiscal-year / period queries over an engine-neutral port."""

    def __init__(self, engine: AccountingEnginePort) -> None:
        self._engine = engine
        self._companies = CompanyQueryService(engine)

    def list_fiscal_years(self, company: CompanyRef) -> Sequence[FiscalYearView]:
        """Return the company's fiscal years, newest first.

        A missing company raises :class:`CompanyNotFoundError`: the
        engine answers an unknown company id with an empty list, so the
        company is verified first and an empty result always means a
        real company with no fiscal years yet.
        """
        self._companies.get_company(company)
        with translate_port_errors():
            return self._engine.list_fiscal_years(company)

    def get_fiscal_year(
        self, company: CompanyRef, fiscal_year: FiscalYearRef
    ) -> FiscalYearView:
        """Return one fiscal year of the company.

        The port has no single-year lookup, so this filters the year's
        list; a year of another company looks exactly like a year that
        does not exist.
        """
        for view in self.list_fiscal_years(company):
            if view.ref == fiscal_year:
                return view
        raise FiscalYearNotFoundError(
            f"Fiscal year {fiscal_year.engine_id} does not exist for this company.",
            details={
                "fiscal_year_id": fiscal_year.engine_id,
                "company_id": company.engine_id,
            },
        )

    def list_periods(
        self,
        company: CompanyRef,
        fiscal_year: FiscalYearRef | None = None,
    ) -> Sequence[FiscalPeriodView]:
        """Return the company's fiscal periods, ordered by start date.

        When ``fiscal_year`` is given, only that year's periods are
        returned — and the year is verified to belong to the company
        first, so a year of another company is a 404, not an empty list
        or a leak.
        """
        if fiscal_year is not None:
            self.get_fiscal_year(company, fiscal_year)
        else:
            self._companies.get_company(company)
        with translate_port_errors():
            return self._engine.list_periods(company, fiscal_year)

    def get_period(
        self, company: CompanyRef, period: FiscalPeriodRef
    ) -> FiscalPeriodView:
        """Return one fiscal period of the company.

        The port has no single-period lookup, so this filters the
        company's periods; a period of another company looks exactly
        like a period that does not exist.
        """
        for view in self.list_periods(company):
            if view.ref == period:
                return view
        raise FiscalPeriodNotFoundError(
            f"Fiscal period {period.engine_id} does not exist for this company.",
            details={
                "fiscal_period_id": period.engine_id,
                "company_id": company.engine_id,
            },
        )


class SetupStatusService:
    """Read-only company/chart setup-state queries for the dashboard wizard.

    The status is derived purely from port reads — a non-empty chart
    means a chart is installed, fiscal years / open periods come from
    the fiscal reads — so the wizard can show what is done and what
    still needs setting up without inventing data the engine cannot
    tell it (design-eng spec §17 Phase 2, "Company and chart setup
    wizard"). The wizard itself is a later story; see
    ``docs/setup-wizard.md`` for the research spike on how company
    creation + chart installation can be driven.
    """

    def __init__(self, engine: AccountingEnginePort) -> None:
        self._engine = engine
        self._accounts = AccountQueryService(engine)
        self._fiscal = FiscalQueryService(engine)

    def get_setup_status(self, company: CompanyRef) -> CompanySetupStatus:
        """Return the company's setup state.

        A missing company raises :class:`CompanyNotFoundError` (via the
        chart read, which verifies the company first).
        """
        chart = self._accounts.get_chart_of_accounts(company)
        years = self._fiscal.list_fiscal_years(company)
        periods = self._fiscal.list_periods(company)
        open_years = [year for year in years if year.is_open]
        open_periods = [period for period in periods if period.is_open]
        return CompanySetupStatus(
            company=company,
            chart_installed=len(chart) > 0,
            account_count=len(chart),
            # The engine records the template on every account, but the
            # port does not expose it yet; see CompanySetupStatus and
            # docs/setup-wizard.md §5.
            chart_template=None,
            fiscal_year_present=len(years) > 0,
            fiscal_year_count=len(years),
            # Years come newest first, so the first open year is the newest.
            open_fiscal_year=open_years[0] if open_years else None,
            open_period_present=len(open_periods) > 0,
            open_period_count=len(open_periods),
        )
