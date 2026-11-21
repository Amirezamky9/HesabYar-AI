"""Unit tests for the data-migration reconciliation checks (US-705).

The executable form of ``docs/data-migration.md`` §4: trial balance
balances, aging ties out, settlement tranches tie out (plus the
orphan sweep and the cross-database external-reference check).
Four of the five checks are the US-703 drill checks, reused — the
tests here pin the NEW aging check and the runner's composition
(including "a failure never silences the other checks"), not the
reused checks' own logic (covered by
``tests/unit/test_restore_drill_us703.py``).

The HesabYar side runs against a SQLite database built from the
real ORM metadata (the US-703 unit-test pattern); the Tryton side
is a fake engine port.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterator
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from hesabyar.domain.accounting.refs import CompanyRef, FiscalYearRef
from hesabyar.domain.accounting.value_objects import (
    CompanyView,
    FiscalYearView,
    NotesBalanceRowView,
    NotesBalanceView,
    TrialBalanceView,
)
from hesabyar.domain.backup.models import DrillCheckStatus
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.infrastructure.operations.migration_reconciliation import (
    CHECK_NOTES_AGING,
    check_notes_aging,
    run_migration_reconciliation,
)
from hesabyar.infrastructure.operations.restore_drill import (
    CHECK_EXTERNAL_REFS,
    CHECK_ORPHANS,
    CHECK_TRANCHES,
    CHECK_TRIAL_BALANCE,
)
from hesabyar.infrastructure.persistence.postgres.models import (
    Base,
    ExternalIdMapModel,
    GatewayContractModel,
    GatewayPaymentModel,
    PaymentChannelModel,
    SettlementTrancheModel,
    TenantModel,
)

INSTALLATION_ID = "00000000-0000-0000-0000-000000000001"
AS_OF = FiscalDate.from_gregorian(datetime.date(2026, 11, 21))


@pytest.fixture()
def engine() -> Iterator[Engine]:
    """An empty HesabYar schema on SQLite (FKs unenforced — see below)."""
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine) as sess:
        yield sess


def _company(company_id: int = 1) -> CompanyView:
    return CompanyView(ref=CompanyRef(company_id), name="Test Co", currency_code="IRR")


def _fiscal_year(company_id: int = 1) -> FiscalYearView:
    return FiscalYearView(
        ref=FiscalYearRef(1),
        company=CompanyRef(company_id),
        name="FY 1405",
        start_date=FiscalDate.from_gregorian(datetime.date(2026, 3, 21)),
        end_date=FiscalDate.from_gregorian(datetime.date(2027, 3, 20)),
        state="open",
    )


def _trial_balance(company_id: int = 1, *, debit: int, credit: int) -> TrialBalanceView:
    return TrialBalanceView(
        company=CompanyRef(company_id),
        date_from=FiscalDate.from_gregorian(datetime.date(2026, 3, 21)),
        date_to=FiscalDate.from_gregorian(datetime.date(2027, 3, 20)),
        total_debit=MoneyIRR(debit),
        total_credit=MoneyIRR(credit),
        move_count=3,
    )


def _notes_row(
    party: str,
    *,
    buckets: tuple[int, int, int, int, int, int] = (0, 0, 0, 0, 0, 0),
    total: int | None = None,
) -> NotesBalanceRowView:
    if total is None:
        total = sum(buckets)
    return NotesBalanceRowView(
        party_id=None,
        party_name=party,
        instrument="receivable",
        account_code="1.2.4",
        account_name="Notes Receivable Held",
        bucket_not_yet_due=MoneyIRR(buckets[0]),
        bucket_0_30=MoneyIRR(buckets[1]),
        bucket_30_60=MoneyIRR(buckets[2]),
        bucket_60_90=MoneyIRR(buckets[3]),
        bucket_90_120=MoneyIRR(buckets[4]),
        bucket_120_plus=MoneyIRR(buckets[5]),
        total=MoneyIRR(total),
        line_count=2,
    )


def _notes_view(
    company_id: int = 1,
    *,
    rows: tuple[NotesBalanceRowView, ...] = (),
    totals: NotesBalanceRowView | None = None,
    accounts_read: tuple[str, ...] = ("1.2.4", "1.2.5", "2.1.5"),
) -> NotesBalanceView:
    return NotesBalanceView(
        company=CompanyRef(company_id),
        as_of=AS_OF,
        instrument="both",
        rows=rows,
        totals=totals,
        accounts_read=accounts_read,
    )


class FakeEngine:
    """A minimal AccountingEnginePort stand-in for the reconciliation checks."""

    def __init__(
        self,
        *,
        companies: list[CompanyView] | None = None,
        fiscal_years: dict[int, list[FiscalYearView]] | None = None,
        trial_balances: dict[int, TrialBalanceView] | None = None,
        notes_balances: dict[int, NotesBalanceView] | None = None,
        invoices: set[int] | None = None,
        moves: set[int] | None = None,
        payments: set[int] | None = None,
        explode_on: str | None = None,
    ) -> None:
        self._companies = companies if companies is not None else []
        self._fiscal_years = fiscal_years or {}
        self._trial_balances = trial_balances or {}
        self._notes_balances = notes_balances or {}
        self._invoices = invoices or set()
        self._moves = moves or set()
        self._payments = payments or set()
        self._explode_on = explode_on

    def _maybe_explode(self, name: str) -> None:
        if self._explode_on == name:
            raise RuntimeError(f"engine exploded in {name}")

    def list_companies(self) -> list[CompanyView]:
        self._maybe_explode("list_companies")
        return self._companies

    def list_fiscal_years(self, company: CompanyRef) -> list[FiscalYearView]:
        self._maybe_explode("list_fiscal_years")
        return self._fiscal_years.get(company.engine_id, [])

    def get_trial_balance(
        self, company: CompanyRef, date_from: FiscalDate, date_to: FiscalDate
    ) -> TrialBalanceView:
        self._maybe_explode("get_trial_balance")
        return self._trial_balances[company.engine_id]

    def get_notes_balance(
        self, company: CompanyRef, as_of: FiscalDate, instrument: str = "both", party: Any = None
    ) -> NotesBalanceView:
        self._maybe_explode("get_notes_balance")
        return self._notes_balances[company.engine_id]

    def get_invoice(self, invoice: Any) -> Any:
        self._maybe_explode("get_invoice")
        return object() if invoice.engine_id in self._invoices else None

    def get_journal(self, journal: Any) -> Any:
        self._maybe_explode("get_journal")
        return object() if journal.engine_id in self._moves else None

    def get_payment(self, payment: Any) -> Any:
        self._maybe_explode("get_payment")
        return object() if payment.engine_id in self._payments else None


def _balanced_engine() -> FakeEngine:
    """One company, balanced trial balance, tying-out notes aging."""
    row = _notes_row("Party A", buckets=(100_000, 50_000, 0, 0, 0, 0))
    totals = _notes_row("", buckets=(100_000, 50_000, 0, 0, 0, 0))
    return FakeEngine(
        companies=[_company()],
        fiscal_years={1: [_fiscal_year()]},
        trial_balances={1: _trial_balance(debit=1_000_000, credit=1_000_000)},
        notes_balances={1: _notes_view(rows=(row,), totals=totals)},
    )


def _seed_balanced_payment(session: Session) -> GatewayPaymentModel:
    """One payment + two tranches that tie out (the US-703 seed shape)."""
    channel = PaymentChannelModel(
        installation_id=INSTALLATION_ID,
        channel_key="snapppay",
        display_name_fa="اسنپ‌پی",
        channel_type="bnpl",
        environment="sandbox",
    )
    session.add(channel)
    session.flush()
    contract = GatewayContractModel(
        channel_id=channel.id,
        version=1,
        valid_from=datetime.date(2026, 1, 1),
        fee_percent=Decimal("2"),
    )
    session.add(contract)
    session.flush()
    payment = GatewayPaymentModel(
        installation_id=INSTALLATION_ID,
        payment_channel_id=channel.id,
        gateway_transaction_id=f"txn-{uuid4().hex[:8]}",
        gross_irr=100_000,
        fee_irr=2_000,
        fee_vat_irr=200,
        net_irr=97_800,
        contract_version_id=contract.id,
        fee_rate_snapshot=Decimal("2"),
        sales_period_jalali="1405-08",
        gateway_confirmed_at=datetime.datetime(2026, 11, 20, 10, 0, tzinfo=datetime.UTC),
        source="store",
    )
    session.add(payment)
    session.flush()
    for index, (gross, net) in enumerate(((50_000, 48_900), (50_000, 48_900)), start=1):
        session.add(
            SettlementTrancheModel(
                installation_id=INSTALLATION_ID,
                gateway_payment_id=payment.id,
                tranche_index=index,
                tranche_count=2,
                sales_period_jalali="1405-08",
                expected_window_start=datetime.date(2026, 11, 20),
                expected_date=datetime.date(2026, 12, 20),
                gross_irr=gross,
                fee_irr=0,
                fee_vat_irr=0,
                net_irr=net,
                confirmed_net_irr=net if index == 1 else 0,
                received_net_irr=net if index == 1 else 0,
            )
        )
    session.commit()
    return payment


# ---------------------------------------------------------------------------
# Notes aging (the one NEW check)
# ---------------------------------------------------------------------------


def test_notes_aging_passes_when_buckets_tie_out() -> None:
    result = check_notes_aging(_balanced_engine(), as_of=AS_OF)  # type: ignore[arg-type]
    assert result.name == CHECK_NOTES_AGING
    assert result.status is DrillCheckStatus.PASS


def test_notes_aging_fails_when_a_row_mismatches() -> None:
    row = _notes_row("Party A", buckets=(100_000, 50_000, 0, 0, 0, 0), total=999_999)
    totals = _notes_row("", buckets=(100_000, 50_000, 0, 0, 0, 0), total=999_999)
    engine = FakeEngine(
        companies=[_company()],
        notes_balances={1: _notes_view(rows=(row,), totals=totals)},
    )
    result = check_notes_aging(engine, as_of=AS_OF)  # type: ignore[arg-type]
    assert result.status is DrillCheckStatus.FAIL
    assert "Party A" in result.detail
    assert "150000" in result.detail


def test_notes_aging_fails_when_totals_buckets_mismatch() -> None:
    row = _notes_row("Party A", buckets=(100_000, 50_000, 0, 0, 0, 0))
    totals = _notes_row("", buckets=(1, 0, 0, 0, 0, 0), total=150_000)
    engine = FakeEngine(
        companies=[_company()],
        notes_balances={1: _notes_view(rows=(row,), totals=totals)},
    )
    result = check_notes_aging(engine, as_of=AS_OF)  # type: ignore[arg-type]
    assert result.status is DrillCheckStatus.FAIL
    assert "totals" in result.detail


def test_notes_aging_fails_when_row_totals_do_not_sum_to_totals() -> None:
    row = _notes_row("Party A", buckets=(100_000, 50_000, 0, 0, 0, 0))
    totals = _notes_row("", buckets=(200_000, 0, 0, 0, 0, 0))  # buckets tie, rows do not
    engine = FakeEngine(
        companies=[_company()],
        notes_balances={1: _notes_view(rows=(row,), totals=totals)},
    )
    result = check_notes_aging(engine, as_of=AS_OF)  # type: ignore[arg-type]
    assert result.status is DrillCheckStatus.FAIL
    assert "Σ row totals 150000" in result.detail


def test_notes_aging_skips_when_no_companies_exist() -> None:
    result = check_notes_aging(FakeEngine(companies=[]), as_of=AS_OF)  # type: ignore[arg-type]
    assert result.status is DrillCheckStatus.SKIPPED


def test_notes_aging_skips_a_company_without_notes_accounts() -> None:
    """A chart without 1.2.4/1.2.5/2.1.5 reads an honest empty report — skipped, not failed."""
    engine = FakeEngine(
        companies=[_company()],
        notes_balances={1: _notes_view(rows=(), totals=None, accounts_read=())},
    )
    result = check_notes_aging(engine, as_of=AS_OF)  # type: ignore[arg-type]
    assert result.status is DrillCheckStatus.SKIPPED


def test_notes_aging_passes_when_one_company_skips_and_one_ties_out() -> None:
    row = _notes_row("Party A", buckets=(100_000, 50_000, 0, 0, 0, 0))
    totals = _notes_row("", buckets=(100_000, 50_000, 0, 0, 0, 0))
    second = CompanyView(ref=CompanyRef(2), name="No Notes Co", currency_code="IRR")
    engine = FakeEngine(
        companies=[_company(), second],
        notes_balances={
            1: _notes_view(rows=(row,), totals=totals),
            2: _notes_view(2, rows=(), totals=None, accounts_read=()),
        },
    )
    result = check_notes_aging(engine, as_of=AS_OF)  # type: ignore[arg-type]
    assert result.status is DrillCheckStatus.PASS
    assert "No Notes Co" in result.detail  # the skip is named, never silent


def test_notes_aging_fails_when_the_engine_raises() -> None:
    engine = FakeEngine(companies=[_company()], explode_on="get_notes_balance")
    result = check_notes_aging(engine, as_of=AS_OF)  # type: ignore[arg-type]
    assert result.status is DrillCheckStatus.FAIL


# ---------------------------------------------------------------------------
# Runner composition
# ---------------------------------------------------------------------------


def test_runner_passes_when_everything_reconciles(engine: Engine, session: Session) -> None:
    _seed_balanced_payment(session)
    results = run_migration_reconciliation(
        hesabyar_engine=engine,
        tryton_engine=_balanced_engine(),  # type: ignore[arg-type]
        as_of=AS_OF,
    )
    assert [r.name for r in results] == [
        CHECK_ORPHANS,
        CHECK_TRANCHES,
        CHECK_EXTERNAL_REFS,
        CHECK_TRIAL_BALANCE,
        CHECK_NOTES_AGING,
    ]
    assert all(r.status is DrillCheckStatus.PASS for r in results)


def test_runner_reports_a_tranche_failure_without_silencing_the_rest(
    engine: Engine, session: Session
) -> None:
    """One broken tie-out fails its own check; the other four still report."""
    payment = _seed_balanced_payment(session)
    broken = session.execute(
        select(SettlementTrancheModel).where(
            SettlementTrancheModel.gateway_payment_id == payment.id
        )
    ).scalars().first()
    assert broken is not None
    broken.net_irr = 1  # Σ tranche net no longer equals the payment net
    session.commit()
    results = run_migration_reconciliation(
        hesabyar_engine=engine,
        tryton_engine=_balanced_engine(),  # type: ignore[arg-type]
        as_of=AS_OF,
    )
    by_name = {r.name: r.status for r in results}
    assert by_name[CHECK_TRANCHES] is DrillCheckStatus.FAIL
    assert by_name[CHECK_ORPHANS] is DrillCheckStatus.PASS
    assert by_name[CHECK_TRIAL_BALANCE] is DrillCheckStatus.PASS
    assert by_name[CHECK_NOTES_AGING] is DrillCheckStatus.PASS


def test_runner_reports_an_unresolvable_external_reference(
    engine: Engine, session: Session
) -> None:
    """An external-id-map row pointing at a Tryton record that does not exist fails."""
    _seed_balanced_payment(session)
    tenant = TenantModel(id=str(uuid4()), slug="recon-test", name="Recon Test")
    session.add(tenant)
    session.flush()
    session.add(
        ExternalIdMapModel(
            id=str(uuid4()),
            tenant_id=tenant.id,
            source_system="hayat",
            external_entity_type="order",
            external_entity_id="order-1",
            target_engine="tryton",
            target_entity_type="account.invoice",
            target_external_id="4242",  # the fake engine knows no such invoice
        )
    )
    session.commit()
    results = run_migration_reconciliation(
        hesabyar_engine=engine,
        tryton_engine=_balanced_engine(),  # type: ignore[arg-type]
        as_of=AS_OF,
    )
    by_name = {r.name: r.status for r in results}
    assert by_name[CHECK_EXTERNAL_REFS] is DrillCheckStatus.FAIL
    assert by_name[CHECK_TRANCHES] is DrillCheckStatus.PASS
    assert by_name[CHECK_NOTES_AGING] is DrillCheckStatus.PASS
