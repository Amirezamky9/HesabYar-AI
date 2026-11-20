"""Response schemas for the chart-of-accounts endpoints (US-105) and the
fiscal-year / period / setup-status endpoints (US-203).

``AccountResponse.id`` is the account's id in the accounting engine
(accounts have no HesabYar-side mapping row); ``company_id`` is the
HesabYar API id of the owning company, matching ``CompanyResponse.id``.
Responses map from the domain views — no Tryton record or ORM object
ever leaks.

Dates are Gregorian ISO dates (``start_date`` / ``end_date``) plus their
Jalali rendering (``start_date_jalali`` / ``end_date_jalali``,
``YYYY/MM/DD``) for the Persian-first dashboard.
"""

from __future__ import annotations

import datetime

from pydantic import BaseModel, Field

from hesabyar.domain.accounting.value_objects import (
    AccountView,
    CompanySetupStatus,
    FiscalPeriodView,
    FiscalYearView,
)


class AccountResponse(BaseModel):
    """One chart-of-accounts entry (group / kol / moein)."""

    id: int = Field(description="Id of the account in the accounting engine")
    company_id: str = Field(
        description="HesabYar API id of the owning company (the engine_companies row id)"
    )
    code: str = Field(description="Account code (hierarchical, e.g. '1.1.1'); empty for the chart root")
    name: str = Field(description="Account display name from the engine")
    type: str | None = Field(
        default=None, description="Account type name from the engine (e.g. 'Asset'); null for groups"
    )
    parent_id: int | None = Field(
        default=None, description="Engine id of the parent account; null for the chart root"
    )
    closed: bool = Field(description="True when the account is closed")
    party_required: bool = Field(description="True when journal lines on this account need a party (tafsili)")
    active: bool = Field(description="True when the account is active")
    postable: bool = Field(
        description="True when the account may receive journal lines (leaf, open and active)"
    )

    @classmethod
    def from_view(cls, view: AccountView, company_id: str) -> AccountResponse:
        """Build a response from a domain view and its company's API id."""
        return cls(
            id=view.ref.engine_id,
            company_id=company_id,
            code=view.code,
            name=view.name,
            type=view.type_name,
            parent_id=view.parent.engine_id if view.parent is not None else None,
            closed=view.closed,
            party_required=view.party_required,
            active=view.active,
            postable=view.is_postable,
        )


class FiscalYearResponse(BaseModel):
    """One fiscal year of a company (US-203)."""

    id: int = Field(description="Id of the fiscal year in the accounting engine")
    company_id: str = Field(
        description="HesabYar API id of the owning company (the engine_companies row id)"
    )
    name: str = Field(description="Fiscal year name from the engine (e.g. 'FY 2026')")
    start_date: datetime.date = Field(description="First day of the fiscal year (Gregorian)")
    end_date: datetime.date = Field(description="Last day of the fiscal year (Gregorian)")
    start_date_jalali: str = Field(
        description="First day of the fiscal year in the Jalali calendar (YYYY/MM/DD)"
    )
    end_date_jalali: str = Field(
        description="Last day of the fiscal year in the Jalali calendar (YYYY/MM/DD)"
    )
    state: str = Field(description="Engine state: 'open', 'closed' or 'locked'")
    is_open: bool = Field(description="True when the fiscal year is open")

    @classmethod
    def from_view(cls, view: FiscalYearView, company_id: str) -> FiscalYearResponse:
        """Build a response from a domain view and its company's API id."""
        return cls(
            id=view.ref.engine_id,
            company_id=company_id,
            name=view.name,
            start_date=view.start_date.gregorian_date,
            end_date=view.end_date.gregorian_date,
            start_date_jalali=view.start_date.to_jalali_str(),
            end_date_jalali=view.end_date.to_jalali_str(),
            state=view.state,
            is_open=view.is_open,
        )


class FiscalPeriodResponse(BaseModel):
    """One accounting period of a fiscal year (US-203)."""

    id: int = Field(description="Id of the fiscal period in the accounting engine")
    company_id: str = Field(
        description="HesabYar API id of the owning company (the engine_companies row id)"
    )
    fiscal_year_id: int = Field(
        description="Engine id of the fiscal year this period belongs to"
    )
    name: str = Field(description="Period name from the engine")
    start_date: datetime.date = Field(description="First day of the period (Gregorian)")
    end_date: datetime.date = Field(description="Last day of the period (Gregorian)")
    start_date_jalali: str = Field(
        description="First day of the period in the Jalali calendar (YYYY/MM/DD)"
    )
    end_date_jalali: str = Field(
        description="Last day of the period in the Jalali calendar (YYYY/MM/DD)"
    )
    state: str = Field(description="Engine state: 'open', 'closed' or 'locked'")
    is_open: bool = Field(description="True when the period is open")
    type: str = Field(description="Period type: 'standard' or 'adjustment'")

    @classmethod
    def from_view(cls, view: FiscalPeriodView, company_id: str) -> FiscalPeriodResponse:
        """Build a response from a domain view and its company's API id.

        ``list_periods`` always fills in ``ref`` / ``fiscal_year``; a view
        built from a partial record (e.g. the period embedded in a journal
        read) has neither and cannot be rendered here.
        """
        if view.ref is None or view.fiscal_year is None:
            raise ValueError("FiscalPeriodView without ref/fiscal_year cannot be rendered")
        return cls(
            id=view.ref.engine_id,
            company_id=company_id,
            fiscal_year_id=view.fiscal_year.engine_id,
            name=view.name,
            start_date=view.start_date.gregorian_date,
            end_date=view.end_date.gregorian_date,
            start_date_jalali=view.start_date.to_jalali_str(),
            end_date_jalali=view.end_date.to_jalali_str(),
            state=view.state,
            is_open=view.is_open,
            type=view.period_type,
        )


class SetupStatusResponse(BaseModel):
    """Read-only setup state of a company, for the dashboard wizard (US-203)."""

    company_id: str = Field(
        description="HesabYar API id of the company (the engine_companies row id)"
    )
    chart_installed: bool = Field(
        description="True when the company has a non-empty chart of accounts"
    )
    account_count: int = Field(description="Number of accounts in the company's chart")
    chart_template: str | None = Field(
        default=None,
        description=(
            "Name of the chart template the chart was installed from; null "
            "until a later adapter story exposes it — the engine does "
            "record the template on every account "
            "(account.account.template), the port just does not read it "
            "yet (see docs/setup-wizard.md §5)"
        ),
    )
    fiscal_year_present: bool = Field(
        description="True when the company has at least one fiscal year"
    )
    fiscal_year_count: int = Field(description="Number of fiscal years of the company")
    open_fiscal_year: FiscalYearResponse | None = Field(
        default=None,
        description="The newest open fiscal year, or null when none is open",
    )
    open_period_present: bool = Field(
        description="True when the company has at least one open fiscal period"
    )
    open_period_count: int = Field(description="Number of open fiscal periods")
    ready: bool = Field(
        description=(
            "True when the company can take journals: a chart is installed, "
            "a fiscal year exists and at least one period is open"
        )
    )

    @classmethod
    def from_view(cls, view: CompanySetupStatus, company_id: str) -> SetupStatusResponse:
        """Build a response from a domain view and its company's API id."""
        return cls(
            company_id=company_id,
            chart_installed=view.chart_installed,
            account_count=view.account_count,
            chart_template=view.chart_template,
            fiscal_year_present=view.fiscal_year_present,
            fiscal_year_count=view.fiscal_year_count,
            open_fiscal_year=(
                FiscalYearResponse.from_view(view.open_fiscal_year, company_id)
                if view.open_fiscal_year is not None
                else None
            ),
            open_period_present=view.open_period_present,
            open_period_count=view.open_period_count,
            ready=view.ready,
        )
