"""Cash-box query use cases (US-516, Phase 5 wave 2, stream K2).

Read-only cash-box queries over the domain ports: the cash-box
registry (with live book balances from the engine), counting
sessions, statement journals, settings, and the cash-box vs sales
variance report (phased design §6 هـ.۲.۷ / §11.1).

Like every query service, this layer talks to persistence ONLY
through the domain ports — never infrastructure (spec §6.3).
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from dataclasses import dataclass

from hesabyar.application.errors import (
    CashBoxInvalidError,
    CashBoxNotFoundError,
    CountSessionNotFoundError,
    translate_port_errors,
)
from hesabyar.application.queries.companies import CompanyQueryService
from hesabyar.domain.accounting.ports import AccountingEnginePort
from hesabyar.domain.accounting.refs import AccountRef, CompanyRef
from hesabyar.domain.accounting.value_objects import StatementJournalView
from hesabyar.domain.cashbox.models import (
    CashBox,
    CashBoxReport,
    CashBoxReportLine,
    CashBoxSettings,
    CountSession,
)
from hesabyar.domain.cashbox.ports import CashBoxPort, CountSessionFilters
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.errors import AccountNotFoundError


@dataclass(frozen=True, slots=True)
class CashBoxView:
    """A cash box with its live book balance and counting state.

    ``book_balance_irr`` is the Tryton book balance of the box's cash
    account as of today (posted moves only) — None when the engine no
    longer returns the box's account (a stale registry row: the box
    is still listed, but its balance is honestly unknown rather than
    invented). ``last_count_at`` is the newest session's creation
    time (any status); ``days_since_last_count`` counts whole days
    from it (None when the box was never counted);
    ``uncounted_alert`` is True when the box has been uncounted
    longer than the installation's reminder threshold.
    """

    box: CashBox
    book_balance_irr: int | None
    last_count_at: datetime.datetime | None
    days_since_last_count: int | None
    uncounted_alert: bool


class CashBoxQueryService:
    """Read-only cash-box queries over the domain ports."""

    def __init__(
        self,
        cashbox: CashBoxPort,
        engine: AccountingEnginePort,
        *,
        now: datetime.datetime | None = None,
    ) -> None:
        self._cashbox = cashbox
        self._engine = engine
        self._companies = CompanyQueryService(engine)
        self._now = now

    def _clock(self) -> datetime.datetime:
        if self._now is not None:
            return self._now
        return datetime.datetime.now(datetime.UTC)

    def _today(self) -> FiscalDate:
        return FiscalDate(self._clock().date())

    def get_settings(self, tenant_id: str) -> CashBoxSettings:
        """Effective cash-box settings: the stored row, or the defaults."""
        stored = self._cashbox.get_settings(tenant_id)
        if stored is not None:
            return stored
        return CashBoxSettings(tenant_id=tenant_id)

    # -- cash boxes ------------------------------------------------------

    def list_cash_boxes(
        self,
        tenant_id: str,
        company: CompanyRef,
        company_row_id: str,
    ) -> Sequence[CashBoxView]:
        """The company's cash boxes with live book balances, oldest first.

        A missing company raises :class:`CompanyNotFoundError` (the
        engine answers an unknown company id with empty reads, so the
        company is verified first). A box whose cash account the
        engine no longer returns is listed with a None balance — the
        registry row is stale, and the UI must say so honestly.
        """
        self._companies.get_company(company)
        settings = self.get_settings(tenant_id)
        boxes = self._cashbox.list_cash_boxes(tenant_id, company_id=company_row_id)
        return [self._view_box(box, company, settings) for box in boxes]

    def get_cash_box(
        self,
        tenant_id: str,
        box_id: str,
    ) -> CashBoxView:
        """One cash box with its live book balance.

        The company comes from the box itself (``engine_company_id``,
        denormalized at registration) — the caller needs no company
        parameter. Raises :class:`CashBoxNotFoundError` for an unknown
        id (or another tenant's box — it looks exactly like a missing
        one).
        """
        box = self._cashbox.get_cash_box(box_id, tenant_id)
        if box is None:
            raise CashBoxNotFoundError(
                f"Cash box {box_id!r} does not exist.",
                details={"cash_box_id": box_id},
            )
        settings = self.get_settings(tenant_id)
        return self._view_box(box, CompanyRef(box.engine_company_id), settings)

    def _view_box(
        self, box: CashBox, company: CompanyRef, settings: CashBoxSettings
    ) -> CashBoxView:
        now = self._clock()
        try:
            with translate_port_errors():
                balance = self._engine.get_account_balance(
                    company, AccountRef(box.cash_account_id), self._today()
                )
            book_balance = balance.balance.amount
        except AccountNotFoundError:
            # A stale registry row: the account is gone from the
            # engine. The box is still listed; its balance is
            # honestly unknown rather than invented or zero.
            book_balance = None
        sessions = self._cashbox.list_sessions(
            box.tenant_id, CountSessionFilters(cash_box_id=box.id)
        )
        last_count_at = sessions[0].created_at if sessions else None
        days_since = (now.date() - last_count_at.date()).days if last_count_at is not None else None
        uncounted_alert = days_since is not None and days_since > settings.count_reminder_days
        return CashBoxView(
            box=box,
            book_balance_irr=book_balance,
            last_count_at=last_count_at,
            days_since_last_count=days_since,
            uncounted_alert=uncounted_alert,
        )

    def list_statement_journals(self, company: CompanyRef) -> Sequence[StatementJournalView]:
        """The company's statement journals (the daybooks a box can register on).

        A missing company raises :class:`CompanyNotFoundError`: the
        engine answers an unknown company id with an empty list, so
        the company is verified first and an empty result always
        means a real company with no statement journals yet.
        """
        self._companies.get_company(company)
        with translate_port_errors():
            return self._engine.list_statement_journals(company)

    # -- counting sessions -------------------------------------------------

    def list_sessions(
        self, tenant_id: str, filters: CountSessionFilters
    ) -> Sequence[CountSession]:
        """The tenant's counting sessions matching the filters, newest first."""
        return self._cashbox.list_sessions(tenant_id, filters)

    def get_session(self, tenant_id: str, session_id: str) -> CountSession:
        """One counting session.

        Raises :class:`CountSessionNotFoundError` for an unknown id
        (or another tenant's session — it looks exactly like a
        missing one).
        """
        session = self._cashbox.get_session(session_id, tenant_id)
        if session is None:
            raise CountSessionNotFoundError(
                f"Counting session {session_id!r} does not exist.",
                details={"session_id": session_id},
            )
        return session

    # -- report ------------------------------------------------------------

    def cashbox_report(
        self,
        tenant_id: str,
        company: CompanyRef,
        company_row_id: str,
        date_from: FiscalDate,
        date_to: FiscalDate,
        cash_box_id: str | None = None,
    ) -> CashBoxReport:
        """Cash-box vs sales variance report for a company and period.

        Per cash box: opening/closing book balances (posted moves
        only, from the engine), receipts/payments inside the period
        (from the trial balance — never from a partial read), the
        period's counting sessions with their shortage/overage
        totals, and the recorded-sales total (posted customer
        invoices in the period). The sales side is Tryton invoices
        only until the 5ب store link lands (``sales_source`` says so
        honestly). ``date_from`` must be on or before ``date_to``.

        The opening balance is derived, not read: closing minus the
        period's net activity (receipts − payments) — exact, since
        both come from the same posted-move set — which saves one
        full-history balance read per box against a remote engine.
        """
        if date_to < date_from:
            raise CashBoxInvalidError("date_to must be on or after date_from.")
        self._companies.get_company(company)
        if cash_box_id is not None:
            box = self._cashbox.get_cash_box(cash_box_id, tenant_id)
            if box is None:
                raise CashBoxNotFoundError(
                    f"Cash box {cash_box_id!r} does not exist.",
                    details={"cash_box_id": cash_box_id},
                )
            boxes = [box]
        else:
            boxes = list(self._cashbox.list_cash_boxes(tenant_id, company_id=company_row_id))
        with translate_port_errors():
            trial_balance = self._engine.get_trial_balance(company, date_from, date_to)
            # Totals only — the report sums sales; it needs no invoice
            # lines, party names, or currency codes.
            invoices = self._engine.list_invoice_totals(company, invoice_type="out", state="posted")
        activity = {
            line.account.engine_id: (line.total_debit.amount, line.total_credit.amount)
            for line in trial_balance.lines
        }
        recorded_sales = 0
        for invoice in invoices:
            if invoice.invoice_date is None:
                continue
            if date_from <= invoice.invoice_date <= date_to:
                recorded_sales += invoice.total_amount.amount
        lines: list[CashBoxReportLine] = []
        for box in boxes:
            closing = self._balance_at(company, box, date_to)
            receipts, payments = activity.get(box.cash_account_id, (0, 0))
            # Opening = closing − net activity: exact (same posted-move
            # set), and it saves one full-history balance read per box.
            opening = closing - (receipts - payments)
            sessions = self._cashbox.list_sessions(
                tenant_id,
                CountSessionFilters(
                    cash_box_id=box.id,
                    date_from=date_from.date,
                    date_to=date_to.date,
                ),
            )
            shortage = sum(
                -session.variance_irr
                for session in sessions
                if session.variance_irr is not None and session.variance_irr < 0
            )
            overage = sum(
                session.variance_irr
                for session in sessions
                if session.variance_irr is not None and session.variance_irr > 0
            )
            lines.append(
                CashBoxReportLine(
                    cash_box_id=box.id,
                    cash_box_name_fa=box.name_fa,
                    cash_account_code=box.cash_account_code,
                    opening_book_balance_irr=opening,
                    closing_book_balance_irr=closing,
                    receipts_irr=receipts,
                    payments_irr=payments,
                    session_count=len(sessions),
                    shortage_irr=shortage,
                    overage_irr=overage,
                    recorded_sales_irr=recorded_sales,
                )
            )
        return CashBoxReport(
            company_id=company_row_id,
            date_from=date_from.date,
            date_to=date_to.date,
            lines=tuple(lines),
            generated_at=self._clock(),
        )

    def _balance_at(self, company: CompanyRef, box: CashBox, as_of: FiscalDate) -> int:
        """One box's book balance at a date (0 when the account is gone)."""
        try:
            with translate_port_errors():
                balance = self._engine.get_account_balance(
                    company, AccountRef(box.cash_account_id), as_of
                )
            return balance.balance.amount
        except AccountNotFoundError:
            # A stale registry row inside a report: the account is
            # gone, so its balance is honestly zero — the box's line
            # still shows its sessions and variances.
            return 0
