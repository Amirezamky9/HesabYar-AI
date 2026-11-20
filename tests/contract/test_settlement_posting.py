"""Contract tests for US-522 settlement posting (live Tryton + live HesabYar DB).

The acceptance test is the §3.5 SnappPay worked example end-to-end:
a 100,000,000 IRR sale, 10% fee + 10% fee VAT, 89,000,000 net in
4 tranches of 22,250,000 — the receivable from SnappPay decreases
89M → 66.75M → 44.5M → 22.25M → 0 as each tranche clears.

These tests run against the LIVE trytond server (lane 1, Supabase DB
``hesabyar_tryton_test`` on :8010) and the LIVE HesabYar PostgreSQL
database, using ONLY the dedicated test company "HesabYar Adapter
Test" and a FRESH HesabYar installation id per test. They never touch
another company's records or another installation's rows.

When either server is not reachable, every test SKIPS cleanly.

Prerequisites (one-time, durable in the shared lane-1 DBs):
  - the ``hesabyar_setup`` module deployed + updated (chart accounts
    1.1.6 / 1.2.8 / 5.2.4; the ``accounts`` fixture runs update_chart,
    which is idempotent);
  - fiscal year FY 2027 + 12 open monthly periods (the Jan/Feb 2027
    tranches clear and settle in 2027 — created once via REST, see
    the us522 log; the fixture only verifies they exist);
  - the HesabYar DB migrated to 0021 (``settlement_postings``).

The sale date is dynamic (today), NOT fixed: Tryton numbers invoices
chronologically and the shared DB accumulates posted invoices, so a
fixed date eventually falls behind the sequence. The tranche dues
come from the real derivation (5th of the next 4 Jalali months) and
the payment term's day-deltas are computed from the actual sale date,
so the test stays green whenever it runs.

Configuration (all optional, dev defaults match docs/dev-setup.md):
  TRYTON_BASE_URL       (default http://localhost:8000)
  TRYTON_DATABASE       (default hesabyar_tryton_test)
  TRYTON_API_KEY        (default hesabyar-dev-rest-key)
  TRYTON_TEST_COMPANY   (default "HesabYar Adapter Test")
  HESABYAR_DATABASE_URL (default: the local docker test DB)

NOTE: the dev server serves Supabase on :8010, so run with
TRYTON_BASE_URL=http://localhost:8010 and the Supabase env sourced
(the .env.supabase file only sets HESABYAR_TRYTON__BASE_URL, which
these tests do not read).
"""

from __future__ import annotations

import datetime
import os
import uuid
from collections.abc import Generator
from dataclasses import dataclass
from decimal import Decimal

import jdatetime
import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from hesabyar.application.commands.integrations import (
    ChannelRegistryService,
    GatewayContractService,
)
from hesabyar.application.commands.settlement import SettlementCommandService
from hesabyar.application.commands.settlement_posting import SettlementPostingService
from hesabyar.domain.accounting.commands import (
    CreateInvoiceDraft,
    CreateJournalDraft,
    InvoiceDraftLine,
    JournalDraftLine,
    PostJournal,
    ReconcileMoveLines,
)
from hesabyar.domain.accounting.refs import (
    CompanyRef,
    JournalRef,
    PartyAddressRef,
    PartyRef,
    PaymentJournalRef,
    PaymentTermRef,
)
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.domain.integrations.models import TrancheLeg
from hesabyar.domain.settlement.commands import (
    ApplyRefund,
    PostBankReceipt,
    PostFeeVariance,
    PostSaleFee,
    PostTrancheClearing,
    SettlementPostingWiring,
)
from hesabyar.domain.settlement.commands import (
    CreateGatewayPayment as SettlementCreateGatewayPayment,
)
from hesabyar.domain.settlement.errors import SettlementPostingPreconditionError
from hesabyar.domain.settlement.models import (
    PaymentSource,
    SettlementExceptionType,
    SettlementTranche,
)
from hesabyar.infrastructure.accounting.tryton.adapter import TrytonAccountingAdapter
from hesabyar.infrastructure.accounting.tryton.client import TrytonRestClient
from hesabyar.infrastructure.persistence.postgres.integrations_repository import (
    SqlAlchemyIntegrationsRepository,
)
from hesabyar.infrastructure.persistence.postgres.models import (
    BankDepositModel,
    GatewayContractModel,
    GatewayPaymentModel,
    PaymentChannelModel,
    SettlementEventModel,
    SettlementExceptionModel,
    SettlementFeeRecordModel,
    SettlementFileImportModel,
    SettlementMatchModel,
    SettlementPostingModel,
    SettlementTrancheModel,
)
from hesabyar.infrastructure.persistence.postgres.settlement_repository import (
    SqlAlchemySettlementAudit,
    SqlAlchemySettlementRepository,
)

BASE_URL = os.environ.get("TRYTON_BASE_URL", "http://localhost:8000")
DATABASE = os.environ.get("TRYTON_DATABASE", "hesabyar_tryton_test")
API_KEY = os.environ.get("TRYTON_API_KEY", "hesabyar-dev-rest-key")
TEST_COMPANY_NAME = os.environ.get("TRYTON_TEST_COMPANY", "HesabYar Adapter Test")
TEST_JOURNAL_NAME = "HesabYar Adapter Test General Journal"
TEST_DB_URL = os.environ.get(
    "HESABYAR_DATABASE_URL",
    "postgresql+psycopg://hesabyar:hesabyar_pass@hesabyar-postgres:5432/hesabyar_test",
)

# Chart codes (universal chart + hesabyar_setup, see docs/adapter.md).
BANK_ACCOUNT_CODE = "1.1.1"
CLEARING_ACCOUNT_CODE = "1.1.6"
RECEIVABLE_ACCOUNT_CODE = "1.2.8"
REVENUE_ACCOUNT_CODE = "4.1.1"
FEE_EXPENSE_ACCOUNT_CODE = "5.2.4"
FEE_VAT_ACCOUNT_CODE = "6.3.6"

CUSTOMER_NAME = "HesabYar US-522 Contract Customer"
GATEWAY_NAME = "HesabYar US-522 Contract Gateway"
CLEARING_JOURNAL_NAME = "HesabYar US-522 Contract Clearing"
PLAIN_JOURNAL_NAME = "HesabYar US-522 Contract Plain"
PAYMENT_TERM_NAME = "HesabYar US-522 Contract 4 Tranches"

FOUR_TRANCHES = (
    TrancheLeg(month_offset=1, payout_day=5, percent=Decimal("25")),
    TrancheLeg(month_offset=2, payout_day=5, percent=Decimal("25")),
    TrancheLeg(month_offset=3, payout_day=5, percent=Decimal("25")),
    TrancheLeg(month_offset=4, payout_day=5, percent=Decimal("25")),
)

GROSS_IRR = 100_000_000
FEE_IRR = 10_000_000
FEE_VAT_IRR = 1_000_000
NET_IRR = 89_000_000
TRANCHE_NET_IRR = 22_250_000

# The sale/invoice dates are dynamic (today): Tryton numbers invoices
# chronologically and the shared DB accumulates posted invoices, so a
# fixed date eventually falls behind the sequence. "Today" always
# stays ahead of past test residue.
SALE_DATE = datetime.date.today()

#: Settlement tables keyed directly by installation id, for teardown.
_SETTLEMENT_TABLES = (
    SettlementPostingModel,
    SettlementMatchModel,
    SettlementExceptionModel,
    SettlementFeeRecordModel,
    SettlementFileImportModel,
    SettlementTrancheModel,
    SettlementEventModel,
    BankDepositModel,
    GatewayPaymentModel,
)


def _expected_dues(sale_date: datetime.date) -> list[datetime.date]:
    """The 5th of the next 4 Jalali months after the sale month."""
    sale_jalali = jdatetime.date.fromgregorian(date=sale_date)
    dues = []
    for offset in range(1, 5):
        month = sale_jalali.month + offset
        year = sale_jalali.year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        dues.append(jdatetime.date(year, month, 5).togregorian())
    return dues


# ---------------------------------------------------------------------------
# Tryton fixtures (module scope)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client() -> Generator[TrytonRestClient, None, None]:
    # 120s timeout (not the 30s default): update_chart is a heavy admin
    # action over the slow remote DB.
    rest_client = TrytonRestClient(BASE_URL, DATABASE, API_KEY, timeout=120.0)
    if not rest_client.ping():
        rest_client.close()
        pytest.skip(
            f"trytond server not reachable at {BASE_URL} (start it per docs/dev-setup.md §3 to run the contract tests)"
        )
    yield rest_client
    rest_client.close()


@pytest.fixture(scope="module")
def adapter(client: TrytonRestClient) -> TrytonAccountingAdapter:
    return TrytonAccountingAdapter(client)


@pytest.fixture(scope="module")
def company(adapter: TrytonAccountingAdapter) -> CompanyRef:
    for view in adapter.list_companies():
        if view.name == TEST_COMPANY_NAME:
            return view.ref
    pytest.skip(
        f"Test company {TEST_COMPANY_NAME!r} not found in Tryton DB {DATABASE!r}. "
        "Provision it with setup_adapter_test_company.py (see docs/adapter.md)."
    )


@pytest.fixture(scope="module")
def accounts(client: TrytonRestClient, adapter: TrytonAccountingAdapter, company):
    # Idempotent: adds the hesabyar_setup accounts on a fresh chart,
    # reports 0 added on a chart that already has them.
    client.action("company.company", "update_chart", company.engine_id)
    chart = {view.code: view for view in adapter.get_chart_of_accounts(company)}
    wanted = (
        BANK_ACCOUNT_CODE,
        CLEARING_ACCOUNT_CODE,
        RECEIVABLE_ACCOUNT_CODE,
        REVENUE_ACCOUNT_CODE,
        FEE_EXPENSE_ACCOUNT_CODE,
        FEE_VAT_ACCOUNT_CODE,
    )
    missing = [code for code in wanted if code not in chart]
    if missing:
        pytest.skip(f"Chart accounts missing from test company: {missing}.")
    return {code: chart[code] for code in wanted}


@pytest.fixture(scope="module")
def fiscal_year_2027(client: TrytonRestClient, company: CompanyRef):
    """Verify FY 2027 + 12 open periods exist (one-time setup, see header)."""
    years = client.search(
        "account.fiscalyear",
        domain=[("company", "=", company.engine_id), ("name", "=", "FY 2027")],
        fields=["name"],
    )
    if not years:
        pytest.skip(
            "Fiscal year FY 2027 is missing from the test DB — the Jan/Feb "
            "2027 tranches have no open period. Create it once via REST "
            "(fiscal year + 12 monthly periods, own move sequence)."
        )
    periods = client.search(
        "account.period",
        domain=[("fiscalyear", "=", int(years[0]["id"]))],
        fields=["name", "state"],
    )
    closed = [p["name"] for p in periods if p["state"] != "open"]
    if len(periods) != 12 or closed:
        pytest.skip(f"FY 2027 periods incomplete/closed: {len(periods)} found, {closed}.")


@pytest.fixture(scope="module")
def irr_currency_id(client: TrytonRestClient) -> int:
    records = client.search("currency.currency", domain=[("code", "=", "IRR")], fields=["code"])
    assert records, "IRR currency not found in the test DB"
    return int(records[0]["id"])


@pytest.fixture(scope="module")
def general_journal(client: TrytonRestClient) -> int:
    records = client.search(
        "account.journal",
        domain=[("name", "=", TEST_JOURNAL_NAME), ("type", "=", "general")],
        fields=["name"],
    )
    if not records:
        pytest.skip(
            f"Test journal {TEST_JOURNAL_NAME!r} not found. "
            "Provision it with setup_adapter_test_company.py (see docs/adapter.md)."
        )
    return int(records[0]["id"])


@pytest.fixture(scope="module")
def customer(client: TrytonRestClient):
    """A customer party with one address, created via REST.

    Teardown is best-effort: posted invoices leave move lines
    referencing the party, which blocks deletion (expected residue).
    """
    party = client.create("party.party", {"name": CUSTOMER_NAME}, fields=["id"])
    party_id = int(party["id"])
    address = client.create("party.address", {"party": party_id, "city": "Tehran"}, fields=["id"])
    address_id = int(address["id"])
    yield PartyRef(party_id), PartyAddressRef(address_id)
    for model, rid in (
        ("party.address", address_id),
        ("party.party", party_id),
    ):
        try:
            client.delete(model, rid)
        except Exception:
            pass  # referenced by posted invoice move lines


@pytest.fixture()
def gateway(client: TrytonRestClient):
    """A FRESH gateway party per test — party-scoped balance sums start at 0.

    Sharing one gateway across tests would mix the e2e's residue into
    the variance/refund tests' assertions. Teardown is best-effort:
    posted move lines reference the party.
    """
    party = client.create("party.party", {"name": f"{GATEWAY_NAME} {uuid.uuid4().hex[:8]}"}, fields=["id"])
    party_id = int(party["id"])
    yield PartyRef(party_id)
    try:
        client.delete("party.party", party_id)
    except Exception:
        pass  # referenced by posted move lines


@pytest.fixture(scope="module")
def clearing_payment_journal(
    client: TrytonRestClient,
    company: CompanyRef,
    irr_currency_id: int,
    accounts,
    general_journal: int,
):
    """A manual payment journal WITH clearing account (1.1.6) + journal."""
    record = client.create(
        "account.payment.journal",
        {
            "name": f"{CLEARING_JOURNAL_NAME} {uuid.uuid4().hex[:8]}",
            "currency": irr_currency_id,
            "company": company.engine_id,
            "process_method": "manual",
            "clearing_account": accounts[CLEARING_ACCOUNT_CODE].ref.engine_id,
            "clearing_journal": general_journal,
        },
        fields=["id"],
        context={"company": company.engine_id},
    )
    journal_id = int(record["id"])
    yield PaymentJournalRef(journal_id)
    try:
        client.delete(
            "account.payment.journal",
            journal_id,
            context={"company": company.engine_id},
        )
    except Exception:
        pass  # referenced by test payments


@pytest.fixture(scope="module")
def plain_payment_journal(
    client: TrytonRestClient,
    company: CompanyRef,
    irr_currency_id: int,
):
    """A manual payment journal WITHOUT clearing config (fail-closed test)."""
    record = client.create(
        "account.payment.journal",
        {
            "name": f"{PLAIN_JOURNAL_NAME} {uuid.uuid4().hex[:8]}",
            "currency": irr_currency_id,
            "company": company.engine_id,
            "process_method": "manual",
        },
        fields=["id"],
        context={"company": company.engine_id},
    )
    journal_id = int(record["id"])
    yield PaymentJournalRef(journal_id)
    try:
        client.delete(
            "account.payment.journal",
            journal_id,
            context={"company": company.engine_id},
        )
    except Exception:
        pass  # referenced by test payments


# ---------------------------------------------------------------------------
# HesabYar DB fixtures (per test — fresh installation)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def db_engine() -> Generator[Engine, None, None]:
    eng = create_engine(TEST_DB_URL, echo=False)
    try:
        with eng.connect() as connection:
            connection.execute(select(1))
    except Exception:
        eng.dispose()
        pytest.skip(
            f"HesabYar database not reachable at {TEST_DB_URL} (source .env.supabase to run the contract tests)"
        )
    yield eng
    eng.dispose()


@dataclass
class World:
    """One test's HesabYar installation: session, repos, services."""

    installation_id: str
    session: Session
    repository: SqlAlchemySettlementRepository
    registry: SqlAlchemyIntegrationsRepository
    settlement: SettlementCommandService
    posting: SettlementPostingService
    channels: ChannelRegistryService
    contracts: GatewayContractService

    def commit(self) -> None:
        self.session.commit()


@pytest.fixture()
def world(db_engine: Engine, adapter: TrytonAccountingAdapter) -> Generator[World, None, None]:
    installation_id = str(uuid.uuid4())
    session = sessionmaker(bind=db_engine)()
    repository = SqlAlchemySettlementRepository(session)
    registry = SqlAlchemyIntegrationsRepository(session)
    audit = SqlAlchemySettlementAudit(session)
    channels = ChannelRegistryService(registry)
    contracts = GatewayContractService(registry)
    settlement = SettlementCommandService(repository, registry, audit)
    posting = SettlementPostingService(repository, registry, adapter, audit)
    world = World(
        installation_id=installation_id,
        session=session,
        repository=repository,
        registry=registry,
        settlement=settlement,
        posting=posting,
        channels=channels,
        contracts=contracts,
    )
    try:
        yield world
    finally:
        session.close()
        cleanup = sessionmaker(bind=db_engine)()
        try:
            for model in _SETTLEMENT_TABLES:
                cleanup.execute(
                    delete(model).where(model.installation_id == installation_id)  # type: ignore[attr-defined]
                )
            channel_ids = list(
                cleanup.execute(
                    select(PaymentChannelModel.id).where(PaymentChannelModel.installation_id == installation_id)
                ).scalars()
            )
            if channel_ids:
                cleanup.execute(delete(GatewayContractModel).where(GatewayContractModel.channel_id.in_(channel_ids)))
            cleanup.execute(delete(PaymentChannelModel).where(PaymentChannelModel.installation_id == installation_id))
            cleanup.commit()
        finally:
            cleanup.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _posted_line_balance(
    client: TrytonRestClient,
    account_id: int,
    *,
    party_id: int | None = None,
) -> int:
    """Posted debit − credit on one account (optionally party-scoped).

    Party-scoped sums are exact even with other tests' residue on the
    shared chart, because each test uses a fresh gateway party.
    """
    domain: list = [
        ("account", "=", account_id),
        ("move.state", "=", "posted"),
    ]
    if party_id is not None:
        domain.append(("party", "=", party_id))
    total = 0
    offset = 0
    while True:
        page = client.search(
            "account.move.line",
            domain=domain,
            fields=["debit", "credit"],
            limit=200,
            offset=offset,
        )
        for record in page:
            total += int(record["debit"]) - int(record["credit"])
        if len(page) < 200:
            break
        offset += len(page)
    return total


def _setup_channel_and_contract(
    world: World,
    *,
    fee_accrual_policy: str = "at_sale",
) -> None:
    """Create the snapppay channel + active 4-tranche contract."""
    world.channels.create_channel(
        world.installation_id,
        "snapppay",
        "اسنپ‌پی",
        "bnpl",
    )
    world.contracts.create_version(
        world.installation_id,
        "snapppay",
        valid_from=SALE_DATE - datetime.timedelta(days=30),
        fee_percent=Decimal("10"),
        fee_vat_rate=Decimal("10"),
        settlement_model="tranches_4",
        tranche_legs=FOUR_TRANCHES,
        payout_window_start_day=1,
        payout_window_end_day=5,
        grace_days_after_due=3,
        fee_accrual_policy=fee_accrual_policy,
    )
    world.commit()


def _record_sale(world: World, *, transaction_id: str | None = None):
    """Record the 100M SnappPay payment; returns (payment, tranches)."""
    result = world.settlement.record_payment(
        SettlementCreateGatewayPayment(
            installation_id=world.installation_id,
            channel_key="snapppay",
            gateway_transaction_id=transaction_id or f"TXN-522-{uuid.uuid4().hex[:8]}",
            gross_irr=GROSS_IRR,
            gateway_confirmed_at=datetime.datetime.combine(SALE_DATE, datetime.time(12, 0), tzinfo=datetime.UTC),
            source=PaymentSource.STORE_WEBHOOK,
            store_order_id="ORD-522",
        )
    )
    world.commit()
    return result.payment, result.tranches


def _create_payment_term(
    client: TrytonRestClient,
    company: CompanyRef,
    dues: list[datetime.date],
) -> PaymentTermRef:
    """A 4-leg term: 3 × 25% on total + remainder, day-deltas to the dues."""
    assert len(dues) == 4
    lines = []
    for index, due in enumerate(dues):
        leg: dict = {
            "type": "percent_on_total" if index < 3 else "remainder",
            "relativedeltas": [
                [
                    "create",
                    [
                        {
                            "months": 0,
                            "weeks": 0,
                            "days": (due - SALE_DATE).days,
                        }
                    ],
                ]
            ],
        }
        if index < 3:
            leg["ratio"] = Decimal("0.25")
            leg["divisor"] = Decimal("4")
        lines.append(leg)
    record = client.create(
        "account.invoice.payment_term",
        {
            "name": f"{PAYMENT_TERM_NAME} {uuid.uuid4().hex[:8]}",
            "lines": [["create", lines]],
        },
        fields=["id"],
        context={"company": company.engine_id},
    )
    return PaymentTermRef(int(record["id"]))


def _post_sale_invoice(
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    customer: PartyRef,
    gateway: PartyRef,
    accounts,
    term: PaymentTermRef,
):
    """Create + validate + post the 100M invoice (payable by the gateway)."""
    ref = adapter.create_invoice_draft(
        CreateInvoiceDraft(
            company=company,
            invoice_type="out",
            party=customer,
            date=FiscalDate(SALE_DATE),
            account=accounts[RECEIVABLE_ACCOUNT_CODE].ref,
            lines=(
                InvoiceDraftLine(
                    account=accounts[REVENUE_ACCOUNT_CODE].ref,
                    unit_price=MoneyIRR(GROSS_IRR),
                    quantity=Decimal(1),
                    description="US-522 contract sale",
                ),
            ),
            payment_term=term,
            alternative_payees=(gateway,),
            description="US-522 SnappPay sale",
        )
    )
    adapter.validate_invoice(ref)
    adapter.post_invoice(ref)
    return ref


def _invoice_receivable_lines(
    client: TrytonRestClient,
    invoice_id: int,
    receivable_account_id: int,
) -> list[dict]:
    """The posted invoice's receivable lines, ordered by maturity date."""
    invoice = client.read("account.invoice", invoice_id, fields=["move"])
    assert invoice is not None and invoice["move"], "posted invoice has no move"
    lines = client.search(
        "account.move.line",
        domain=[
            ("move", "=", int(invoice["move"])),
            ("account", "=", receivable_account_id),
        ],
        fields=["debit", "credit", "party", "maturity_date", "reconciliation"],
        order=[["maturity_date", "ASC"]],
    )
    return lines


def _link_invoice_lines(
    world: World,
    tranches: tuple[SettlementTranche, ...],
    line_ids: list[int],
) -> None:
    """Point each tranche at its invoice receivable line (due-date order)."""
    assert len(tranches) == len(line_ids)
    for tranche, line_id in zip(tranches, line_ids, strict=True):
        updated = world.repository.update_tranche(tranche.id, tryton_receivable_line_id=line_id)
        assert updated is not None
    world.commit()


def _wiring(
    company: CompanyRef,
    gateway: PartyRef,
    payment_journal: PaymentJournalRef,
    accounts,
    general_journal: int,
) -> SettlementPostingWiring:
    return SettlementPostingWiring(
        company=company,
        gateway_party=gateway,
        payment_journal=payment_journal,
        receivable_account=accounts[RECEIVABLE_ACCOUNT_CODE].ref,
        clearing_account=accounts[CLEARING_ACCOUNT_CODE].ref,
        bank_account=accounts[BANK_ACCOUNT_CODE].ref,
        fee_expense_account=accounts[FEE_EXPENSE_ACCOUNT_CODE].ref,
        fee_vat_account=accounts[FEE_VAT_ACCOUNT_CODE].ref,
        revenue_account=accounts[REVENUE_ACCOUNT_CODE].ref,
        general_journal=JournalRef(general_journal),
    )


def _set_tranche_state(
    world: World,
    tranche_id: str,
    state: str,
    *,
    confirmed_net_irr: int | None = None,
    received_net_irr: int | None = None,
) -> None:
    """Direct DB write: tranche states have no service path yet (US-523)."""
    row = world.session.get(SettlementTrancheModel, tranche_id)
    assert row is not None
    row.state = state
    if confirmed_net_irr is not None:
        row.confirmed_net_irr = confirmed_net_irr
    if received_net_irr is not None:
        row.received_net_irr = received_net_irr
    world.commit()


def _setup_event_with_deposit(
    world: World,
    channel_id: str,
    tranche_id: str,
    payout_date: datetime.date,
    *,
    deposit_irr: int,
) -> str:
    """Seed one settlement event + bank deposit + match (tie-out inputs)."""
    event = SettlementEventModel(
        id=str(uuid.uuid4()),
        installation_id=world.installation_id,
        payment_channel_id=channel_id,
        payout_date=payout_date,
        net_irr=deposit_irr,
        source="file",
        deductions_irr=0,
    )
    deposit = BankDepositModel(
        id=str(uuid.uuid4()),
        installation_id=world.installation_id,
        deposit_date=payout_date,
        amount_irr=deposit_irr,
        source="statement_import",
    )
    world.session.add_all([event, deposit])
    world.session.flush()
    world.session.add(
        SettlementMatchModel(
            id=str(uuid.uuid4()),
            installation_id=world.installation_id,
            tranche_id=tranche_id,
            allocated_amount_irr=deposit_irr,
            method="exact",
            bank_deposit_id=deposit.id,
            settlement_event_id=event.id,
            is_automatic=True,
        )
    )
    world.commit()
    return event.id


def _move_lines(client: TrytonRestClient, move_id: int) -> list[dict]:
    return client.search(
        "account.move.line",
        domain=[("move", "=", move_id)],
        fields=["account", "debit", "credit", "party", "reconciliation", "state"],
        order=[["id", "ASC"]],
    )


# ---------------------------------------------------------------------------
# Acceptance e2e: the §3.5 SnappPay worked example
# ---------------------------------------------------------------------------


def test_snapppay_worked_example_e2e(
    client: TrytonRestClient,
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    accounts,
    fiscal_year_2027,
    general_journal: int,
    customer,
    gateway: PartyRef,
    clearing_payment_journal: PaymentJournalRef,
    world: World,
):
    """§3.5: 100M sale → 10M fee + 1M VAT → 89M net in 4 × 22.25M tranches.

    Receivable from SnappPay decreases 89M → 66.75M → 44.5M →
    22.25M → 0 as each tranche clears; each tranche then settles
    through its bank receipt, and the bank ends up +89M.
    """
    customer_party, _address = customer
    receivable_id = accounts[RECEIVABLE_ACCOUNT_CODE].ref.engine_id
    bank_id = accounts[BANK_ACCOUNT_CODE].ref.engine_id
    wiring = _wiring(company, gateway, clearing_payment_journal, accounts, general_journal)

    # -- sale: channel + contract + payment + derived schedule -----------
    _setup_channel_and_contract(world)
    payment, tranches = _record_sale(world)
    assert len(tranches) == 4
    dues = _expected_dues(SALE_DATE)
    assert [t.expected_date for t in tranches] == dues, "derived dues must be the 5th of the next 4 Jalali months"
    for tranche in tranches:
        assert tranche.gross_irr == 25_000_000
        assert tranche.fee_irr == 2_500_000
        assert tranche.fee_vat_irr == 250_000
        assert tranche.net_irr == TRANCHE_NET_IRR

    # -- Tryton invoice: 4 receivable lines, party=gateway, maturity=due -
    term = _create_payment_term(client, company, dues)
    invoice_ref = _post_sale_invoice(adapter, company, customer_party, gateway, accounts, term)
    invoice_lines = _invoice_receivable_lines(client, invoice_ref.engine_id, receivable_id)
    assert len(invoice_lines) == 4
    for line, due in zip(invoice_lines, dues, strict=True):
        assert int(line["debit"]) == 25_000_000
        assert int(line["credit"]) == 0
        assert int(line["party"]) == gateway.engine_id
        assert line["maturity_date"] == due
    _link_invoice_lines(world, tranches, [int(line["id"]) for line in invoice_lines])
    assert _posted_line_balance(client, receivable_id, party_id=gateway.engine_id) == GROSS_IRR

    # -- fee at sale (policy A): Dr fee + VAT / Cr receivable ------------
    result = world.posting.post_sale_fee(
        world.installation_id,
        PostSaleFee(payment_id=payment.id, wiring=wiring, date=SALE_DATE),
    )
    world.commit()
    assert result.fee_irr == FEE_IRR
    assert result.fee_vat_irr == FEE_VAT_IRR
    assert result.tryton_move_id is not None
    assert _posted_line_balance(client, receivable_id, party_id=gateway.engine_id) == NET_IRR

    # -- clearing + bank receipt, tranche by tranche ----------------------
    channel = world.registry.get_channel(world.installation_id, "snapppay")
    assert channel is not None
    bank_before = _posted_line_balance(client, bank_id)
    expected_receivable = NET_IRR
    for index, tranche in enumerate(tranches):
        due = dues[index]
        _set_tranche_state(
            world,
            tranche.id,
            "confirmed",
            confirmed_net_irr=TRANCHE_NET_IRR,
        )
        clearing = world.posting.post_tranche_clearing(
            world.installation_id,
            PostTrancheClearing(tranche_id=tranche.id, wiring=wiring, payout_date=due),
        )
        world.commit()
        assert clearing.amount_irr == TRANCHE_NET_IRR
        assert clearing.reconciliation is not None
        assert clearing.posting.tryton_move_id is not None
        # The clearing move is POSTED and dated to the payout date.
        clearing_move = client.read("account.move", clearing.posting.tryton_move_id, fields=["date", "state"])
        assert clearing_move is not None
        assert clearing_move["date"] == due
        assert clearing_move["state"] == "posted"
        # The tranche's 3 receivable lines share one reconciliation.
        for line in _move_lines(client, clearing.posting.tryton_move_id):
            if int(line["account"]) == receivable_id:
                assert int(line["reconciliation"]) == clearing.reconciliation.ref.engine_id
        expected_receivable -= TRANCHE_NET_IRR
        assert _posted_line_balance(client, receivable_id, party_id=gateway.engine_id) == expected_receivable, (
            f"receivable after tranche {index + 1} clearing"
        )

        event_id = _setup_event_with_deposit(world, channel.id, tranche.id, due, deposit_irr=TRANCHE_NET_IRR)
        _set_tranche_state(
            world,
            tranche.id,
            "received",
            received_net_irr=TRANCHE_NET_IRR,
        )
        receipt = world.posting.post_bank_receipt(
            world.installation_id,
            PostBankReceipt(
                tranche_id=tranche.id,
                wiring=wiring,
                settlement_event_id=event_id,
                deposit_date=due,
            ),
        )
        world.commit()
        assert receipt.amount_irr == TRANCHE_NET_IRR
        assert receipt.tie_out.passes
        assert receipt.reconciliation is not None
        assert receipt.tranche.state.value == "settled"
        # The bank is up by one tranche net (delta: the account is shared).
        assert _posted_line_balance(client, bank_id) == bank_before + (index + 1) * TRANCHE_NET_IRR

    assert _posted_line_balance(client, receivable_id, party_id=gateway.engine_id) == 0
    assert _posted_line_balance(client, bank_id) == bank_before + NET_IRR


# ---------------------------------------------------------------------------
# reconcile_rest, live (adapter level)
# ---------------------------------------------------------------------------


def test_reconcile_rest_direct(
    client: TrytonRestClient,
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    accounts,
    general_journal: int,
    gateway: PartyRef,
):
    """Two offsetting posted lines on 1.2.8 reconcile via reconcile_rest."""
    receivable = accounts[RECEIVABLE_ACCOUNT_CODE].ref
    draft = adapter.create_journal_draft(
        CreateJournalDraft(
            company=company,
            date=FiscalDate(SALE_DATE),
            lines=(
                JournalDraftLine(
                    account=receivable,
                    debit=MoneyIRR(1_000_000),
                    party=gateway,
                    description="US-522 reconcile probe Dr",
                ),
                JournalDraftLine(
                    account=receivable,
                    credit=MoneyIRR(1_000_000),
                    party=gateway,
                    description="US-522 reconcile probe Cr",
                ),
            ),
            description="US-522 reconcile probe",
            journal=JournalRef(general_journal),
        )
    )
    posted = adapter.post_journal(PostJournal(draft=draft))
    assert posted.engine_id == draft.engine_id
    lines = adapter.list_move_lines(company, move=draft)
    assert len(lines) == 2
    assert all(line.state == "valid" for line in lines)
    assert all(line.reconciliation is None for line in lines)

    view = adapter.reconcile_move_lines(
        ReconcileMoveLines(
            company=company,
            lines=tuple(line.ref for line in lines),
        )
    )
    assert view.ref.engine_id > 0
    assert view.number != ""
    after = adapter.list_move_lines(company, move=draft)
    assert all(
        line.reconciliation is not None and line.reconciliation.engine_id == view.ref.engine_id for line in after
    )


# ---------------------------------------------------------------------------
# Fail-closed clearing without a clearing-configured journal
# ---------------------------------------------------------------------------


def test_clearing_fails_closed_without_clearing_config(
    client: TrytonRestClient,
    company: CompanyRef,
    accounts,
    general_journal: int,
    gateway: PartyRef,
    plain_payment_journal: PaymentJournalRef,
    world: World,
):
    """post_clearing refuses (before any engine write) when the journal
    has no clearing account + journal — `succeed` would silently
    create no clearing move."""
    _setup_channel_and_contract(world)
    payment, tranches = _record_sale(world)
    tranche = tranches[0]
    wiring = _wiring(company, gateway, plain_payment_journal, accounts, general_journal)
    _set_tranche_state(world, tranche.id, "confirmed", confirmed_net_irr=TRANCHE_NET_IRR)
    with pytest.raises(SettlementPostingPreconditionError, match="no clearing account"):
        world.posting.post_tranche_clearing(
            world.installation_id,
            PostTrancheClearing(
                tranche_id=tranche.id,
                wiring=wiring,
                payout_date=tranche.expected_date,
            ),
        )
    # No payment was created for the tranche (the refusal precedes it).
    payments = client.search(
        "account.payment",
        domain=[("reference", "=", f"settlement:{tranche.id}")],
        fields=["reference"],
    )
    assert payments == []


# ---------------------------------------------------------------------------
# Fee variance + refund, live
# ---------------------------------------------------------------------------


def test_fee_variance_live(
    client: TrytonRestClient,
    company: CompanyRef,
    accounts,
    general_journal: int,
    gateway: PartyRef,
    clearing_payment_journal: PaymentJournalRef,
    world: World,
):
    """A real fee above the expected fee posts an adjustment move —
    never silently absorbed — and opens a fee_mismatch exception."""
    receivable_id = accounts[RECEIVABLE_ACCOUNT_CODE].ref.engine_id
    wiring = _wiring(company, gateway, clearing_payment_journal, accounts, general_journal)
    _setup_channel_and_contract(world)
    payment, tranches = _record_sale(world)
    world.posting.post_sale_fee(
        world.installation_id,
        PostSaleFee(payment_id=payment.id, wiring=wiring, date=SALE_DATE),
    )
    world.commit()
    before = _posted_line_balance(client, receivable_id, party_id=gateway.engine_id)
    assert before == -(FEE_IRR + FEE_VAT_IRR)  # fee legs only, no invoice here

    result = world.posting.post_fee_variance(
        world.installation_id,
        PostFeeVariance(
            payment_id=payment.id,
            wiring=wiring,
            actual_fee_irr=11_000_000,
            actual_vat_irr=1_100_000,
            reason="gateway file shows 11% fee",
            date=SALE_DATE,
            event_ref="file:snapppay-2026-11",
        ),
    )
    world.commit()
    assert result.fee_variance_irr == 1_000_000
    assert result.vat_variance_irr == 100_000
    assert result.posting is not None
    assert result.exception is not None
    assert result.exception.exception_type == SettlementExceptionType.FEE_MISMATCH
    # The adjustment credits the receivable (a higher fee means the
    # gateway keeps more, so the net receivable drops by 1.1M).
    assert _posted_line_balance(client, receivable_id, party_id=gateway.engine_id) == before - 1_100_000


def test_refund_live(
    client: TrytonRestClient,
    company: CompanyRef,
    accounts,
    general_journal: int,
    gateway: PartyRef,
    clearing_payment_journal: PaymentJournalRef,
    world: World,
):
    """A 5M refund posts the sales-return journal and reduces the LAST
    open tranche first (gross and net; fee/VAT unchanged)."""
    receivable_id = accounts[RECEIVABLE_ACCOUNT_CODE].ref.engine_id
    wiring = _wiring(company, gateway, clearing_payment_journal, accounts, general_journal)
    _setup_channel_and_contract(world)
    payment, tranches = _record_sale(world)
    world.posting.post_sale_fee(
        world.installation_id,
        PostSaleFee(payment_id=payment.id, wiring=wiring, date=SALE_DATE),
    )
    world.commit()
    before = _posted_line_balance(client, receivable_id, party_id=gateway.engine_id)

    result = world.posting.apply_refund(
        world.installation_id,
        ApplyRefund(
            payment_id=payment.id,
            wiring=wiring,
            refund_amount_irr=5_000_000,
            reason="customer returned the order",
            date=SALE_DATE,
            event_ref="refund:evt-1",
        ),
    )
    world.commit()
    assert result.refund_amount_irr == 5_000_000
    assert result.posting is not None
    # The return journal credits the receivable (Dr revenue / Cr receivable).
    assert _posted_line_balance(client, receivable_id, party_id=gateway.engine_id) == before - 5_000_000
    updated = world.repository.list_tranches_for_payment(world.installation_id, payment.id)
    last = updated[-1]
    assert last.gross_irr == 20_000_000
    assert last.net_irr == 17_250_000
    assert last.fee_irr == 2_500_000  # fee already earned, unchanged
    assert last.fee_vat_irr == 250_000
    for tranche in updated[:-1]:
        assert tranche.net_irr == TRANCHE_NET_IRR  # untouched


# ---------------------------------------------------------------------------
# Policy B: fee recognised per tranche at confirmation
# ---------------------------------------------------------------------------


def test_policy_b_fee_at_settlement(
    client: TrytonRestClient,
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    accounts,
    fiscal_year_2027,
    general_journal: int,
    customer,
    gateway: PartyRef,
    clearing_payment_journal: PaymentJournalRef,
    world: World,
):
    """Under policy B the sale fee refuses at sale and is recognised
    per tranche as each tranche confirms (inside post_clearing)."""
    customer_party, _address = customer
    receivable_id = accounts[RECEIVABLE_ACCOUNT_CODE].ref.engine_id
    wiring = _wiring(company, gateway, clearing_payment_journal, accounts, general_journal)
    _setup_channel_and_contract(world, fee_accrual_policy="at_settlement")
    payment, tranches = _record_sale(world)
    dues = _expected_dues(SALE_DATE)
    term = _create_payment_term(client, company, dues)
    invoice_ref = _post_sale_invoice(adapter, company, customer_party, gateway, accounts, term)
    invoice_lines = _invoice_receivable_lines(client, invoice_ref.engine_id, receivable_id)
    assert len(invoice_lines) == 4
    _link_invoice_lines(world, tranches, [int(line["id"]) for line in invoice_lines])

    with pytest.raises(SettlementPostingPreconditionError, match="at_settlement"):
        world.posting.post_sale_fee(
            world.installation_id,
            PostSaleFee(payment_id=payment.id, wiring=wiring, date=SALE_DATE),
        )
    assert _posted_line_balance(client, receivable_id, party_id=gateway.engine_id) == GROSS_IRR

    tranche = tranches[0]
    _set_tranche_state(world, tranche.id, "confirmed", confirmed_net_irr=TRANCHE_NET_IRR)
    clearing = world.posting.post_tranche_clearing(
        world.installation_id,
        PostTrancheClearing(tranche_id=tranche.id, wiring=wiring, payout_date=dues[0]),
    )
    world.commit()
    assert clearing.fee_posting is not None
    assert clearing.fee_posting.amount_irr == 2_750_000
    assert clearing.reconciliation is not None
    # Receivable: 100M − tranche 1 fee leg (2.75M) − clearing leg (22.25M).
    assert _posted_line_balance(client, receivable_id, party_id=gateway.engine_id) == 75_000_000
