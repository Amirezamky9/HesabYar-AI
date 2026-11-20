"""Service tests for the cash-forecast query service (US-553).

The service is built over the in-memory fakes (settlement,
installments, cheques) + a small fake engine (companies, invoices,
move lines, purchase requests, cost prices) + a stub packaging
repository. No database, no Tryton.

Covered: the source aggregation, the double-count guards (BNPL
legs, pending-clearance legs, plan-linked invoices), the collateral
exclusion at the source, the invoice maturity fallback, the
packaging pricing/date rules, the empty-forecast honesty, the
freshness stamp, and the company/range validation.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, cast

import pytest

from hesabyar.application.errors import CompanyNotFoundError
from hesabyar.application.queries.cash_forecast import CashForecastQueryService
from hesabyar.domain.accounting.ports import AccountingEnginePort
from hesabyar.domain.accounting.refs import (
    AccountRef,
    CompanyRef,
    InvoiceRef,
    JournalDraftRef,
    MoveLineRef,
    PartyRef,
    PostedJournalRef,
    ProductRef,
    PurchaseRequestRef,
)
from hesabyar.domain.accounting.value_objects import (
    CompanyView,
    InvoiceView,
    MoneyIRR,
    MoveLineView,
    PurchaseRequestView,
)
from hesabyar.domain.cash_forecast.models import ForecastBucket
from hesabyar.domain.cheques.models import ChequeKind, ChequePurpose, ChequeState
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.installments.models import (
    InstallmentPlan,
    InstallmentReceipt,
    LegAllocation,
    PaymentMethod,
    PlanKind,
)
from hesabyar.domain.packaging.models import PackagingMaterial
from hesabyar.domain.packaging.ports import PackagingRepositoryPort
from tests.unit.test_cash_forecast import (
    INSTALLATION_ID,
    TENANT_ID,
    TODAY,
    _cheque,
    _leg,
    _tranche,
)
from tests.unit.test_cheque_services import FakeChequeRepository
from tests.unit.test_installment_services import FakeInstallmentRepository
from tests.unit.test_settlement_services import FakeSettlementRepository

COMPANY_ENGINE_ID = 2
COMPANY_ROW_ID = "00000000-0000-0000-0000-000000000555"
DATE_FROM = datetime.date(2026, 11, 20)
DATE_TO = datetime.date(2027, 3, 20)  # 1405-12-29 (120 days out)


class FakeForecastEngine:
    """The engine reads the forecast needs: companies, invoices, move lines, purchase requests."""

    def __init__(self) -> None:
        self.companies: list[CompanyView] = [
            CompanyView(ref=CompanyRef(engine_id=COMPANY_ENGINE_ID), name="Test", currency_code="IRR")
        ]
        self.invoices: list[InvoiceView] = []
        self.move_lines: list[MoveLineView] = []
        self.purchase_requests: list[PurchaseRequestView] = []
        self.cost_prices: dict[int, Decimal | None] = {}

    def list_companies(self) -> Sequence[CompanyView]:
        return self.companies

    def list_invoices(
        self,
        company: CompanyRef,
        invoice_type: str | None = None,
        state: str | None = None,
    ) -> Sequence[InvoiceView]:
        return [
            invoice
            for invoice in self.invoices
            if (invoice_type is None or invoice.invoice_type == invoice_type)
            and (state is None or invoice.state == state)
        ]

    def list_move_lines(
        self,
        company: CompanyRef,
        *,
        move: JournalDraftRef | None = None,
        account: AccountRef | None = None,
        party: PartyRef | None = None,
        maturity_date: FiscalDate | None = None,
        unreconciled_only: bool = False,
    ) -> Sequence[MoveLineView]:
        lines = self.move_lines
        if account is not None:
            lines = [line for line in lines if line.account == account]
        if unreconciled_only:
            lines = [line for line in lines if line.reconciliation is None]
        return lines

    def list_purchase_requests(
        self,
        company: CompanyRef,
        state: str | None = None,
        after_id: int | None = None,
    ) -> Sequence[PurchaseRequestView]:
        return self.purchase_requests

    def get_product_cost_price(
        self, product: ProductRef, company: CompanyRef
    ) -> Decimal | None:
        return self.cost_prices.get(product.engine_id)


@dataclass
class StubPackagingRepository:
    """Only list_materials — the forecast's one packaging read."""

    materials: list[PackagingMaterial] = field(default_factory=list)

    def list_materials(self, tenant_id: str, company_id: str) -> Sequence[PackagingMaterial]:
        return [
            material
            for material in self.materials
            if material.tenant_id == tenant_id and material.company_id == company_id
        ]


@dataclass
class World:
    engine: FakeForecastEngine = field(default_factory=FakeForecastEngine)
    settlement: FakeSettlementRepository = field(default_factory=FakeSettlementRepository)
    installments: FakeInstallmentRepository = field(default_factory=FakeInstallmentRepository)
    cheques: FakeChequeRepository = field(default_factory=FakeChequeRepository)
    packaging: StubPackagingRepository = field(default_factory=StubPackagingRepository)

    def service(self) -> CashForecastQueryService:
        return CashForecastQueryService(
            cast(AccountingEnginePort, self.engine),
            cast(Any, self.settlement),
            cast(Any, self.installments),
            cast(Any, self.cheques),
            cast(PackagingRepositoryPort, self.packaging),
            today=TODAY,
        )

    def forecast(self, **overrides: object):
        kwargs: dict[str, object] = {
            "tenant_id": TENANT_ID,
            "installation_id": INSTALLATION_ID,
            "company_row_id": COMPANY_ROW_ID,
            "date_from": DATE_FROM,
            "date_to": DATE_TO,
        }
        kwargs.update(overrides)
        return self.service().get_cash_forecast(
            CompanyRef(engine_id=COMPANY_ENGINE_ID), **kwargs  # type: ignore[arg-type]
        )


def _invoice(**overrides: object) -> InvoiceView:
    values: dict[str, object] = {
        "ref": InvoiceRef(engine_id=101),
        "company": CompanyRef(engine_id=COMPANY_ENGINE_ID),
        "invoice_type": "out",
        "party": PartyRef(engine_id=11),
        "party_name": "مشتری الف",
        "number": "INV-101",
        "invoice_date": FiscalDate(datetime.date(2026, 11, 10)),
        "accounting_date": FiscalDate(datetime.date(2026, 11, 10)),
        "state": "posted",
        "description": "",
        "reference": "",
        "journal": None,
        "account": AccountRef(engine_id=1001),
        "currency_code": "IRR",
        "untaxed_amount": MoneyIRR(8_000_000),
        "tax_amount": MoneyIRR(0),
        "total_amount": MoneyIRR(8_000_000),
        "amount_to_pay": MoneyIRR(8_000_000),
        "move": PostedJournalRef(engine_id=5001),
    }
    values.update(overrides)
    return InvoiceView(**values)  # type: ignore[arg-type]


def _move_line(**overrides: object) -> MoveLineView:
    values: dict[str, object] = {
        "ref": MoveLineRef(engine_id=9001),
        "move": JournalDraftRef(engine_id=5001),
        "account": AccountRef(engine_id=1001),
        "debit": MoneyIRR(8_000_000),
        "credit": MoneyIRR(0),
        "state": "valid",
        "maturity_date": FiscalDate(datetime.date(2026, 12, 10)),
    }
    values.update(overrides)
    return MoveLineView(**values)  # type: ignore[arg-type]


def _purchase_request(**overrides: object) -> PurchaseRequestView:
    values: dict[str, object] = {
        "ref": PurchaseRequestRef(engine_id=201),
        "product": ProductRef(engine_id=301),
        "quantity": Decimal("10"),
        "state": "draft",
        "company": CompanyRef(engine_id=COMPANY_ENGINE_ID),
        "supply_date": FiscalDate(datetime.date(2026, 12, 5)),
    }
    values.update(overrides)
    return PurchaseRequestView(**values)  # type: ignore[arg-type]


def _material(**overrides: object) -> PackagingMaterial:
    values: dict[str, object] = {
        "id": "mat-1",
        "tenant_id": TENANT_ID,
        "company_id": COMPANY_ROW_ID,
        "tryton_product_id": 301,
        "tryton_template_id": 401,
        "name_fa": "کارتن",
    }
    values.update(overrides)
    return PackagingMaterial(**values)  # type: ignore[arg-type]


def _plan(**overrides: object) -> InstallmentPlan:
    values: dict[str, object] = {
        "id": "plan-1",
        "tenant_id": TENANT_ID,
        "installation_id": INSTALLATION_ID,
        "company_engine_id": COMPANY_ENGINE_ID,
        "invoice_id": "INV-540-1",
        "total_principal": MoneyIRR(30_000_000),
        "leg_count": 3,
    }
    values.update(overrides)
    return InstallmentPlan(**values)  # type: ignore[arg-type]


def _receipt(**overrides: object) -> InstallmentReceipt:
    values: dict[str, object] = {
        "id": "receipt-1",
        "plan_id": "plan-1",
        "tenant_id": TENANT_ID,
        "installation_id": INSTALLATION_ID,
        "invoice_id": "INV-540-1",
        "journal_move_id": 42,
        "amount": MoneyIRR(10_000_000),
        "received_date": FiscalDate(datetime.date(2026, 11, 19)),
        "payment_method": PaymentMethod.CASH,
        "allocations": (
            LegAllocation(leg_id="leg-1", amount=MoneyIRR(10_000_000)),
        ),
    }
    values.update(overrides)
    return InstallmentReceipt(**values)  # type: ignore[arg-type]


# -- validation -----------------------------------------------------------------------


def test_reversed_range_raises() -> None:
    world = World()
    with pytest.raises(ValueError, match="date_to must be on or after date_from"):
        world.forecast(date_from=datetime.date(2026, 12, 1), date_to=datetime.date(2026, 11, 1))


def test_unknown_company_raises() -> None:
    world = World()
    world.engine.companies = []
    with pytest.raises(CompanyNotFoundError):
        world.forecast()


def test_empty_forecast_is_honest_empty() -> None:
    world = World()
    view = world.forecast()
    assert view.items == ()
    assert view.totals.item_count == 0
    assert view.totals.inflow_confirmed_irr == 0
    assert view.totals.outflow_confirmed_irr == 0
    assert view.totals.inflow_forecast_irr == 0
    assert view.totals.outflow_forecast_irr == 0
    assert view.source_counts["settlement_tranche"] == 0
    assert view.source_counts["installment_leg"] == 0
    assert view.source_counts["cheque_received"] == 0
    assert view.source_counts["cheque_issued"] == 0
    assert view.source_counts["expected_sale"] == 0
    assert view.skipped_undated == 0
    assert view.freshness is not None
    assert view.freshness.freshness_indicator == "live"
    assert view.freshness.calculation_version == "registered-records/1.0"


# -- tranches + cheques ------------------------------------------------------------------


def test_tranche_and_cheque_items_aggregate() -> None:
    world = World()
    world.settlement.tranches["tranche-1"] = _tranche(
        expected_date=datetime.date(2026, 11, 25), net_irr=9_450_000
    )
    world.cheques.create(_cheque(id="c-1", due_date=FiscalDate(datetime.date(2026, 11, 26))))
    world.cheques.create(
        _cheque(
            id="c-2",
            sayad_id="SAYAD-2",
            kind=ChequeKind.ISSUED,
            state=ChequeState.ISSUED,
            amount_irr=4_000_000,
            due_date=FiscalDate(datetime.date(2026, 11, 27)),
        )
    )
    view = world.forecast()
    assert view.totals.inflow_confirmed_irr == 19_450_000
    assert view.totals.outflow_confirmed_irr == 4_000_000
    assert view.source_counts["settlement_tranche"] == 1
    assert view.source_counts["cheque_received"] == 1
    assert view.source_counts["cheque_issued"] == 1
    # The horizon bucket 0_30 (today + 5..7 days) carries all three.
    bucket = next(b for b in view.horizon if b.key == "0_30")
    assert bucket.item_count == 3
    assert bucket.net_confirmed_irr == 15_450_000


def test_collateral_cheques_excluded_at_the_source() -> None:
    world = World()
    world.cheques.create(
        _cheque(
            id="c-coll",
            purpose=ChequePurpose.COLLATERAL,
            amount_irr=100_000_000,
        )
    )
    view = world.forecast()
    assert view.items == ()
    assert view.source_counts["cheque_received"] == 0


# -- installment legs + double-count guards ------------------------------------------------


def _seed_plan(world: World, **plan_overrides: object) -> InstallmentPlan:
    plan = _plan(**plan_overrides)
    legs = [
        _leg(id="leg-1", plan_id=plan.id, sequence=1),
        _leg(
            id="leg-2",
            plan_id=plan.id,
            sequence=2,
            due_date=FiscalDate(datetime.date(2026, 12, 25)),
        ),
        _leg(
            id="leg-3",
            plan_id=plan.id,
            sequence=3,
            due_date=FiscalDate(datetime.date(2027, 1, 24)),
        ),
    ]
    world.installments.create_plan(plan, legs)
    return plan


def test_leg_items_use_uncovered_balance() -> None:
    world = World()
    _seed_plan(world)
    # A cash receipt pays leg 1 in full; leg 1 promises nothing more.
    world.installments.create_receipt(_receipt())
    view = world.forecast()
    assert view.source_counts["installment_leg"] == 2
    assert view.totals.inflow_confirmed_irr == 20_000_000


def test_bnpl_legs_never_counted() -> None:
    """A BNPL leg is paid by the gateway in tranches — counting the leg
    AND the tranche would double-count the same money."""
    world = World()
    _seed_plan(world, kind=PlanKind.BNPL, gateway_channel_key="snapppay")
    view = world.forecast()
    assert view.source_counts["installment_leg"] == 0
    assert view.items == ()


def test_pending_clearance_leg_counts_only_the_uncovered_remainder() -> None:
    """A leg fully covered by a pending cheque promises nothing HERE —
    the pending slice comes through the cheque channel (report 14)."""
    world = World()
    _seed_plan(world)
    cheque = _cheque(
        id="c-leg",
        sayad_id="SAYAD-LEG",
        state=ChequeState.HELD,
        amount_irr=10_000_000,
        due_date=FiscalDate(datetime.date(2026, 11, 25)),
    )
    world.cheques.create(cheque)
    world.installments.create_receipt(
        _receipt(payment_method=PaymentMethod.CHEQUE, cheque_id="c-leg")
    )
    view = world.forecast()
    # Leg 1 is fully covered by the pending cheque: no leg item for it.
    # Legs 2+3 are open (20,000,000); the cheque itself is the inflow
    # for leg 1's money (10,000,000) — counted exactly once.
    assert view.source_counts["installment_leg"] == 2
    assert view.source_counts["cheque_received"] == 1
    assert view.totals.inflow_confirmed_irr == 30_000_000


def test_bounced_cheque_leg_counts_again() -> None:
    """After a bounce the pending cover vanishes: the leg owes again."""
    world = World()
    _seed_plan(world)
    cheque = _cheque(
        id="c-leg",
        sayad_id="SAYAD-LEG",
        state=ChequeState.BOUNCED,
        amount_irr=10_000_000,
        due_date=FiscalDate(datetime.date(2026, 11, 25)),
    )
    world.cheques.create(cheque)
    world.installments.create_receipt(
        _receipt(payment_method=PaymentMethod.CHEQUE, cheque_id="c-leg")
    )
    view = world.forecast()
    assert view.source_counts["installment_leg"] == 3
    assert view.source_counts["cheque_received"] == 0  # a bounced cheque promises nothing
    assert view.totals.inflow_confirmed_irr == 30_000_000


def test_plan_linked_invoice_excluded_from_due_invoices() -> None:
    """The invoice behind a plan is the legs' invoice — counting the
    invoice AND the legs would double-count it."""
    world = World()
    _seed_plan(world, invoice_id="101")  # engine invoice id as a string
    world.engine.invoices.append(
        _invoice(
            ref=InvoiceRef(engine_id=101),
            invoice_date=FiscalDate(datetime.date(2026, 11, 25)),
        )
    )
    world.engine.invoices.append(
        _invoice(
            ref=InvoiceRef(engine_id=102),
            number="INV-102",
            invoice_date=FiscalDate(datetime.date(2026, 11, 25)),
            move=PostedJournalRef(engine_id=5002),
        )
    )
    view = world.forecast()
    assert view.source_counts["invoice_receivable"] == 1
    receivable = next(
        item for item in view.items if item.source.value == "invoice_receivable"
    )
    assert receivable.ref == "invoice:102"


def test_plan_linked_invoice_matched_by_number_too() -> None:
    world = World()
    _seed_plan(world, invoice_id="INV-101")  # the plan carries the invoice NUMBER
    world.engine.invoices.append(
        _invoice(
            ref=InvoiceRef(engine_id=101),
            number="INV-101",
            invoice_date=FiscalDate(datetime.date(2026, 11, 25)),
        )
    )
    view = world.forecast()
    assert view.source_counts["invoice_receivable"] == 0


# -- invoices --------------------------------------------------------------------------------


def test_invoice_dated_by_maturity_date() -> None:
    world = World()
    world.engine.invoices.append(_invoice())
    world.engine.move_lines.append(_move_line())
    view = world.forecast()
    assert view.source_counts["invoice_receivable"] == 1
    item = next(item for item in view.items if item.source.value == "invoice_receivable")
    assert item.date.gregorian_date == datetime.date(2026, 12, 10)


def test_invoice_falls_back_to_invoice_date_without_maturity() -> None:
    world = World()
    world.engine.invoices.append(
        _invoice(invoice_date=FiscalDate(datetime.date(2026, 11, 25)))
    )
    view = world.forecast()
    item = next(item for item in view.items if item.source.value == "invoice_receivable")
    assert item.date.gregorian_date == datetime.date(2026, 11, 25)


def test_supplier_invoice_is_outflow() -> None:
    world = World()
    world.engine.invoices.append(
        _invoice(
            ref=InvoiceRef(engine_id=201),
            invoice_type="in",
            number="SUP-201",
            party_name="تأمین‌کننده",
            invoice_date=FiscalDate(datetime.date(2026, 11, 25)),
            account=AccountRef(engine_id=2001),
            move=PostedJournalRef(engine_id=5002),
            amount_to_pay=MoneyIRR(6_000_000),
        )
    )
    view = world.forecast()
    assert view.source_counts["invoice_payable"] == 1
    assert view.totals.outflow_confirmed_irr == 6_000_000


def test_paid_or_draft_invoices_not_counted() -> None:
    world = World()
    world.engine.invoices.append(_invoice(amount_to_pay=MoneyIRR(0)))
    world.engine.invoices.append(
        _invoice(ref=InvoiceRef(engine_id=102), state="draft", number="INV-102")
    )
    view = world.forecast()
    assert view.source_counts["invoice_receivable"] == 0


def test_undated_invoice_counted_not_lost() -> None:
    world = World()
    world.engine.invoices.append(_invoice(invoice_date=None, move=None))
    view = world.forecast()
    assert view.source_counts["invoice_receivable"] == 0
    assert view.skipped_undated == 1


# -- packaging purchases -----------------------------------------------------------------------


def test_packaging_purchase_priced_and_dated() -> None:
    world = World()
    world.packaging.materials.append(_material())
    world.engine.purchase_requests.append(_purchase_request())
    world.engine.cost_prices[301] = Decimal("250000")
    view = world.forecast()
    assert view.source_counts["packaging_purchase"] == 1
    assert view.totals.outflow_confirmed_irr == 2_500_000
    item = next(item for item in view.items if item.source.value == "packaging_purchase")
    assert item.date.gregorian_date == datetime.date(2026, 12, 5)
    assert item.label == "کارتن"


def test_packaging_request_ignored_when_not_a_packaging_product() -> None:
    world = World()
    world.engine.purchase_requests.append(_purchase_request())
    world.engine.cost_prices[301] = Decimal("250000")
    view = world.forecast()
    assert view.source_counts["packaging_purchase"] == 0


def test_packaging_request_ignored_without_cost_price() -> None:
    """A request that cannot be priced honestly is dropped, never guessed."""
    world = World()
    world.packaging.materials.append(_material())
    world.engine.purchase_requests.append(_purchase_request())
    world.engine.cost_prices[301] = None
    view = world.forecast()
    assert view.source_counts["packaging_purchase"] == 0


def test_packaging_request_ignored_when_done_or_cancelled() -> None:
    world = World()
    world.packaging.materials.append(_material())
    world.engine.purchase_requests.append(_purchase_request(state="done"))
    world.engine.purchase_requests.append(
        _purchase_request(ref=PurchaseRequestRef(engine_id=202), state="cancelled")
    )
    world.engine.cost_prices[301] = Decimal("250000")
    view = world.forecast()
    assert view.source_counts["packaging_purchase"] == 0


def test_packaging_request_date_falls_back_to_purchase_date() -> None:
    world = World()
    world.packaging.materials.append(_material())
    world.engine.purchase_requests.append(
        _purchase_request(
            supply_date=None,
            purchase_date=FiscalDate(datetime.date(2026, 12, 8)),
        )
    )
    world.engine.cost_prices[301] = Decimal("100000")
    view = world.forecast()
    item = next(item for item in view.items if item.source.value == "packaging_purchase")
    assert item.date.gregorian_date == datetime.date(2026, 12, 8)


# -- forecast bucket ------------------------------------------------------------------------------


def test_forecast_bucket_always_empty() -> None:
    """پیش‌بینی has no source yet: the bucket reads zero, honestly."""
    world = World()
    world.settlement.tranches["tranche-1"] = _tranche()
    view = world.forecast()
    assert view.totals.inflow_forecast_irr == 0
    assert view.totals.outflow_forecast_irr == 0
    assert all(
        item.bucket is ForecastBucket.CONFIRMED for item in view.items
    )
    assert all(
        bucket.inflow_forecast_irr == 0 and bucket.outflow_forecast_irr == 0
        for bucket in view.horizon
    )


def test_items_sorted_by_date() -> None:
    world = World()
    world.settlement.tranches["t-late"] = _tranche(
        id="t-late", expected_date=datetime.date(2026, 12, 20)
    )
    world.settlement.tranches["t-early"] = _tranche(
        id="t-early", expected_date=datetime.date(2026, 11, 21)
    )
    view = world.forecast()
    dates = [item.date.gregorian_date for item in view.items]
    assert dates == sorted(dates)
