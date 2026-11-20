"""Cash-forecast query service (Phase 5-6, US-553).

Read-only views over the registered records that promise future
cash: settlement tranches (US-521), installment legs (US-540),
cheques (the report 14 rule), posted invoices with an unpaid
balance, and open packaging purchase requests. Every lookup is
tenant/installation-scoped on the registry side and company-scoped
on the engine side; a row of another installation answers as if
it did not exist.

The service loads the records and the pure builders in
:mod:`hesabyar.domain.cash_forecast.forecast` aggregate them —
the service owns the reads, the builders own the math, and the
two can never disagree about a rule.

Double-count guards (see docs/cash-forecast.md):

- BNPL legs are never items: the gateway pays them in settlement
  tranches, and the tranche is the cash.
- A leg covered by a pending-clearance cheque contributes only
  its UNCOVERED remainder: the pending slice is expected through
  the cheque channel (report 14), not the leg channel.
- An invoice with an installment plan is excluded from the
  due-invoice source: the legs are the installment view of that
  invoice. The plan's ``invoice_id`` is a free-form string, so the
  match tries both the engine invoice id and the invoice number.
- Collateral cheques appear in NEITHER bucket, ever (§7.5): the
  cheque read filters ``purpose=payment`` at the source, and the
  builder re-checks every row.

پیش‌بینی (expected sales) has NO source yet: the bucket is always
empty, honestly so — the service never invents expected sales.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from dataclasses import replace
from decimal import ROUND_HALF_UP, Decimal

from hesabyar.application.commands.installments import resolve_cheque_states
from hesabyar.application.errors import translate_port_errors
from hesabyar.application.queries.companies import CompanyQueryService
from hesabyar.domain.accounting.ports import AccountingEnginePort
from hesabyar.domain.accounting.refs import AccountRef, CompanyRef, ProductRef
from hesabyar.domain.accounting.value_objects import InvoiceView, ReportFreshness
from hesabyar.domain.cash_forecast.forecast import (
    aggregate_items,
    build_calendar,
    build_horizon,
    cheque_forecast_items,
    invoice_forecast_items,
    jalali_month_key,
    leg_forecast_items,
    purchase_request_items,
    tranche_forecast_items,
)
from hesabyar.domain.cash_forecast.models import CashForecastView, ForecastItem
from hesabyar.domain.cheques.models import Cheque, ChequeKind, ChequePurpose
from hesabyar.domain.cheques.ports import ChequeRepositoryPort
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.installments.models import InstallmentLeg, PlanKind
from hesabyar.domain.installments.ports import InstallmentRepositoryPort
from hesabyar.domain.installments.state_machine import split_allocations
from hesabyar.domain.packaging.ports import PackagingRepositoryPort
from hesabyar.domain.reports.statements import (
    CALCULATION_VERSIONS,
    ENGINE_VERSION,
    REPORT_VERSION,
)
from hesabyar.domain.settlement.models import TrancheState
from hesabyar.domain.settlement.ports import SettlementRepositoryPort

#: Purchase-request states that still promise a purchase. ``done`` /
#: ``cancelled`` / ``rejected`` promise nothing.
OPEN_PURCHASE_REQUEST_STATES: frozenset[str] = frozenset({"draft", "quotation"})

#: Tranche states that still promise future cash (mirrors the
#: builder's OPEN_TRANCHE_STATES — the repository filters first so
#: settled/received rows never travel).
_OPEN_TRANCHE_STATES: tuple[TrancheState, ...] = (
    TrancheState.EXPECTED,
    TrancheState.DUE,
    TrancheState.OVERDUE,
    TrancheState.CONFIRMED,
    TrancheState.PARTIALLY_CONFIRMED,
    TrancheState.PARTIALLY_RECEIVED,
)

#: Registry list page size (the same 1000 the cheque query service
#: uses for report reads — the registries are small).
_REGISTRY_PAGE_SIZE = 1000


class CashForecastQueryService:
    """Read-only cash-forecast queries over the ports."""

    def __init__(
        self,
        engine: AccountingEnginePort,
        settlement: SettlementRepositoryPort,
        installments: InstallmentRepositoryPort,
        cheques: ChequeRepositoryPort,
        packaging: PackagingRepositoryPort,
        *,
        today: datetime.date | None = None,
    ) -> None:
        self._engine = engine
        self._settlement = settlement
        self._installments = installments
        self._cheques = cheques
        self._packaging = packaging
        self._companies = CompanyQueryService(engine)
        self._today = today

    def _clock_date(self) -> datetime.date:
        return self._today or datetime.date.today()

    def get_cash_forecast(
        self,
        company: CompanyRef,
        *,
        tenant_id: str,
        installation_id: str,
        company_row_id: str,
        date_from: datetime.date,
        date_to: datetime.date,
    ) -> CashForecastView:
        """Return the cash forecast for a company and date range.

        A missing company raises :class:`CompanyNotFoundError`: the
        engine answers an unknown company id with an empty result,
        so the company is verified first and an empty forecast
        always means a real company with no registered records in
        the range. A reversed range raises ``ValueError`` (the route
        maps it to a typed 422 ``VALIDATION_ERROR``).
        """
        if date_to < date_from:
            raise ValueError("date_to must be on or after date_from.")
        self._companies.get_company(company)
        today = self._clock_date()

        items: list[ForecastItem] = []
        source_counts: dict[str, int] = {}
        skipped_undated = 0
        with translate_port_errors():
            tranche_items = tranche_forecast_items(
                self._settlement.list_tranches(
                    installation_id,
                    states=_OPEN_TRANCHE_STATES,
                    due_from=date_from,
                    due_to=date_to,
                ),
                date_from=date_from,
                date_to=date_to,
            )
            items.extend(tranche_items)
            source_counts["settlement_tranche"] = len(tranche_items)

            leg_items = self._leg_items(
                tenant_id=tenant_id,
                installation_id=installation_id,
                date_from=date_from,
                date_to=date_to,
            )
            items.extend(leg_items)
            source_counts["installment_leg"] = len(leg_items)

            cheque_items = cheque_forecast_items(
                self._commercial_cheques(
                    tenant_id=tenant_id, installation_id=installation_id
                ),
                date_from=date_from,
                date_to=date_to,
            )
            items.extend(cheque_items)
            source_counts["cheque_received"] = sum(
                1 for item in cheque_items if item.source.value == "cheque_received"
            )
            source_counts["cheque_issued"] = sum(
                1 for item in cheque_items if item.source.value == "cheque_issued"
            )

            invoice_items, skipped_undated = self._invoice_items(
                company,
                tenant_id=tenant_id,
                installation_id=installation_id,
                date_from=date_from,
                date_to=date_to,
            )
            items.extend(invoice_items)
            source_counts["invoice_receivable"] = sum(
                1 for item in invoice_items if item.source.value == "invoice_receivable"
            )
            source_counts["invoice_payable"] = sum(
                1 for item in invoice_items if item.source.value == "invoice_payable"
            )

            purchase_items = self._packaging_items(
                company,
                tenant_id=tenant_id,
                company_row_id=company_row_id,
                date_from=date_from,
                date_to=date_to,
            )
            items.extend(purchase_items)
            source_counts["packaging_purchase"] = len(purchase_items)

        # پیش‌بینی (expected sales) has no source yet: the bucket
        # stays empty — no items are ever added for it.
        source_counts["expected_sale"] = 0
        items.sort(key=lambda item: (item.date.gregorian_date, item.source.value, item.ref))
        view = CashForecastView(
            company_id=str(company.engine_id),
            date_from=FiscalDate(date_from),
            date_to=FiscalDate(date_to),
            as_of=FiscalDate(today),
            items=tuple(items),
            months=build_calendar(items, date_from=date_from, date_to=date_to),
            horizon=build_horizon(items, today=today),
            totals=aggregate_items(items),
            source_counts=source_counts,
            skipped_undated=skipped_undated,
        )
        return self._stamp(view, company, FiscalDate(date_from), FiscalDate(date_to))

    # -- sources -----------------------------------------------------------

    def _leg_items(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        date_from: datetime.date,
        date_to: datetime.date,
    ) -> list[ForecastItem]:
        """Unpaid internal legs with their UNCOVERED balances.

        BNPL plans are skipped entirely (the gateway's tranches are
        the cash). A leg's uncovered balance is principal − received
        − pending clearance: the pending slice is expected through
        the cheque channel, so counting it here would double-count
        the same money.
        """
        legs_with_balances: list[tuple[InstallmentLeg, int]] = []
        for plan in self._installments.list_plans(
            tenant_id=tenant_id,
            installation_id=installation_id,
            kind=PlanKind.INTERNAL,
            limit=_REGISTRY_PAGE_SIZE,
        ):
            legs = self._installments.list_legs(
                plan.id, tenant_id=tenant_id, installation_id=installation_id
            )
            receipts = self._installments.list_receipts(
                plan.id, tenant_id=tenant_id, installation_id=installation_id
            )
            cheque_states = resolve_cheque_states(
                receipts,
                self._cheques,
                tenant_id=tenant_id,
                installation_id=installation_id,
            )
            received_by_leg, pending_by_leg = split_allocations(receipts, cheque_states)
            for leg in legs:
                uncovered = (
                    leg.principal.amount
                    - received_by_leg.get(leg.id, 0)
                    - pending_by_leg.get(leg.id, 0)
                )
                legs_with_balances.append((leg, max(0, uncovered)))
        return leg_forecast_items(
            legs_with_balances, date_from=date_from, date_to=date_to
        )

    def _commercial_cheques(
        self, *, tenant_id: str, installation_id: str
    ) -> Sequence[Cheque]:
        """Commercial cheques only — the segregation seam (§2.2).

        ``purpose=payment`` at the source: a collateral cheque can
        never leak into the forecast. Both kinds are read (received
        → inflow, issued → outflow); the builder applies the
        report 14 state rule.
        """
        cheques: list[Cheque] = []
        for kind in (ChequeKind.RECEIVED, ChequeKind.ISSUED):
            cheques.extend(
                self._cheques.list(
                    tenant_id=tenant_id,
                    installation_id=installation_id,
                    kind=kind,
                    purpose=ChequePurpose.PAYMENT,
                    limit=_REGISTRY_PAGE_SIZE,
                )
            )
        return cheques

    def _invoice_items(
        self,
        company: CompanyRef,
        *,
        tenant_id: str,
        installation_id: str,
        date_from: datetime.date,
        date_to: datetime.date,
    ) -> tuple[list[ForecastItem], int]:
        """Posted invoices with an unpaid balance, dated by maturity.

        The due date is the receivable/payable line's maturity date
        (the aged-balance rule's date), falling back to the invoice
        date when the move carries no maturity. Invoices with an
        installment plan are excluded — the legs are the installment
        view of that invoice. Maturity dates are read batched per
        account (one ``list_move_lines`` per distinct account), not
        one read per invoice.
        """
        plan_invoice_ids = self._plan_invoice_ids(
            tenant_id=tenant_id, installation_id=installation_id
        )
        rows: list[tuple[str, str, int, datetime.date | None, str]] = []
        for invoice_type in ("out", "in"):
            invoices = self._engine.list_invoices(
                company, invoice_type=invoice_type, state="posted"
            )
            open_invoices = [
                invoice
                for invoice in invoices
                if invoice.amount_to_pay is not None
                and invoice.amount_to_pay.amount > 0
                and str(invoice.ref.engine_id) not in plan_invoice_ids
                and (invoice.number is None or invoice.number not in plan_invoice_ids)
            ]
            maturity_by_move = self._maturity_by_move(company, open_invoices)
            for invoice in open_invoices:
                assert invoice.amount_to_pay is not None  # noqa: S101 - filtered above
                due: datetime.date | None = None
                if invoice.move is not None:
                    due = maturity_by_move.get(invoice.move.engine_id)
                if due is None and invoice.invoice_date is not None:
                    due = invoice.invoice_date.gregorian_date
                rows.append(
                    (
                        str(invoice.ref.engine_id),
                        invoice_type,
                        invoice.amount_to_pay.amount,
                        due,
                        invoice.party_name,
                    )
                )
        return invoice_forecast_items(rows, date_from=date_from, date_to=date_to)

    def _plan_invoice_ids(
        self, *, tenant_id: str, installation_id: str
    ) -> set[str]:
        """Every invoice id a plan covers (any kind — BNPL too)."""
        ids: set[str] = set()
        offset = 0
        while True:
            plans = self._installments.list_plans(
                tenant_id=tenant_id,
                installation_id=installation_id,
                limit=_REGISTRY_PAGE_SIZE,
                offset=offset,
            )
            if not plans:
                break
            ids.update(plan.invoice_id for plan in plans)
            offset += len(plans)
            if len(plans) < _REGISTRY_PAGE_SIZE:
                break
        return ids

    def _maturity_by_move(
        self, company: CompanyRef, invoices: Sequence[InvoiceView]
    ) -> dict[int, datetime.date]:
        """Maturity date per invoice move (batched per account).

        Reads the unreconciled lines of each distinct receivable /
        payable account once and maps the invoice's move to its
        line's maturity date. A move with no maturity-dated line
        simply has no entry — the caller falls back to the invoice
        date.
        """
        by_account: dict[int, list[InvoiceView]] = {}
        for invoice in invoices:
            if invoice.move is None:
                continue
            by_account.setdefault(invoice.account.engine_id, []).append(invoice)
        maturity: dict[int, datetime.date] = {}
        for account_id, account_invoices in by_account.items():
            move_ids = {
                invoice.move.engine_id
                for invoice in account_invoices
                if invoice.move is not None
            }
            lines = self._engine.list_move_lines(
                company,
                account=AccountRef(engine_id=account_id),
                unreconciled_only=True,
            )
            for line in lines:
                if line.move.engine_id not in move_ids:
                    continue
                if line.maturity_date is None:
                    continue
                move_date = line.maturity_date.gregorian_date
                previous = maturity.get(line.move.engine_id)
                if previous is None or move_date < previous:
                    maturity[line.move.engine_id] = move_date
        return maturity

    def _packaging_items(
        self,
        company: CompanyRef,
        *,
        tenant_id: str,
        company_row_id: str,
        date_from: datetime.date,
        date_to: datetime.date,
    ) -> list[ForecastItem]:
        """Open packaging purchase requests, priced by cost price.

        The supply wizard's open requests (``draft`` / ``quotation``)
        for PACKAGING products only (other products' requests are
        not packaging purchases). Priced as quantity × the product's
        current cost price; a request with no cost price cannot be
        priced honestly and is dropped (never guessed). Dated by
        supply date, else purchase date, else the request date.
        """
        materials = self._packaging.list_materials(tenant_id, company_row_id)
        product_names = {
            material.tryton_product_id: material.name_fa for material in materials
        }
        if not product_names:
            return []
        rows: list[tuple[str, str, int, datetime.date | None]] = []
        for request in self._engine.list_purchase_requests(company):
            if request.state not in OPEN_PURCHASE_REQUEST_STATES:
                continue
            product_id = request.product.engine_id
            if product_id not in product_names:
                continue
            cost_price = self._engine.get_product_cost_price(
                ProductRef(product_id), company
            )
            if cost_price is None:
                continue
            amount = int(
                (request.quantity * cost_price).quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP
                )
            )
            due: datetime.date | None = None
            if request.supply_date is not None:
                due = request.supply_date.gregorian_date
            elif request.purchase_date is not None:
                due = request.purchase_date.gregorian_date
            elif request.created_at is not None:
                due = request.created_at.date()
            rows.append(
                (
                    str(request.ref.engine_id),
                    product_names[product_id],
                    amount,
                    due,
                )
            )
        return purchase_request_items(rows, date_from=date_from, date_to=date_to)

    # -- freshness -----------------------------------------------------------

    def _stamp(
        self,
        view: CashForecastView,
        company: CompanyRef,
        period_start: FiscalDate,
        period_end: FiscalDate,
    ) -> CashForecastView:
        """Attach the §11.1 freshness block to a forecast view.

        A live read: every source is read at generation time, so
        ``source_timestamp`` equals ``generated_at``. The sources
        are named in ``reproducibility`` so the read can be
        repeated exactly.
        """
        now = datetime.datetime.now(datetime.UTC)
        freshness = ReportFreshness(
            source="hesabyar-registry+tryton:cash-forecast",
            source_timestamp=now,
            generated_at=now,
            company_id=str(company.engine_id),
            period_start=period_start,
            period_end=period_end,
            report_version=REPORT_VERSION,
            calculation_version=CALCULATION_VERSIONS["cash_forecast"],
            reproducibility={
                "engine": "tryton",
                "engine_version": ENGINE_VERSION,
                "sources": [
                    "settlement_tranches",
                    "installment_legs",
                    "cheques (report 14 rule)",
                    "posted_invoices",
                    "purchase_requests",
                ],
                "company_id": str(company.engine_id),
                "period_start": period_start.gregorian_date.isoformat(),
                "period_end": period_end.gregorian_date.isoformat(),
                "as_of": view.as_of.gregorian_date.isoformat(),
                "jalali_month": jalali_month_key(view.as_of.gregorian_date),
            },
        )
        return replace(view, freshness=freshness)
