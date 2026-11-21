"""Fresh-store install scenario (US-706, Phase 7 exit criterion).

A new store installs HesabYar end to end, against the LIVE trytond
server and the LIVE HesabYar database:

1. **Provision** a brand-new Tryton company through the REST API
   only — the ``docs/setup-wizard.md`` §1 flow, exercised in CI for
   the first time: party + company, grant the API user + set
   ``company_filter='all'``, ``install_chart`` + ``update_chart``
   (the ``hesabyar_setup`` module), move sequence, fiscal years for
   this year AND next year + 12 open monthly periods each, invoice
   sequences, a general journal, a clearing-configured payment
   journal, a customer party and a gateway party.
2. **Map** the company into a fresh HesabYar tenant (engine
   connection + ``engine_companies`` row) with the store mappings
   the generic connector needs.
3. **Import opening balances** — a balanced opening journal (cash
   against issued capital), posted.
4. **First order via the GENERIC connector** — an
   ``order.completed`` webhook through the real pipeline (the
   custom-coded Hayat store is the reference; the connector stays
   generic) → invoice draft → validated → posted.
5. **First payment** — a ``payment.captured`` webhook → payment
   draft (never auto-posted: money the gateway owes, not received).
6. **First settlement** — the gateway payment for the same
   transaction, a single-tranche contract, fee + clearing + bank
   receipt through the settlement engine; the gateway receivable
   ends at 0 and the bank is up by the net.
7. **The trial balance balances** — over posted moves, with exact
   expected totals (every move in the scenario is accounted for).

When either server is not reachable (or the chart install fails),
every test SKIPS cleanly with a clear message — the suite stays
green without a server. With the servers up, the tests must PASS.

Residue: the provisioned company, its journals/parties and its
posted moves stay in the shared Tryton DB (posted records cannot
be deleted — the same precedent as the other contract tests). The
company name is unique per run, so reruns never collide. The
HesabYar side (tenant, mappings, settlement rows, engine-company
mapping) IS deleted in teardown.

Configuration (all optional, dev defaults match docs/dev-setup.md):
  HESABYAR_DATABASE_URL (the HesabYar DB)
  TRYTON_BASE_URL       (default http://localhost:8000)
  TRYTON_DATABASE       (default hesabyar_tryton_test)
  TRYTON_API_KEY        (default hesabyar-dev-rest-key)
  TRYTON_ADMIN_LOGIN    (default "admin" — the user the REST
                         application key belongs to; the grant
                         step targets this user)
"""

from __future__ import annotations

import datetime
import os
import uuid
from collections.abc import Generator
from dataclasses import dataclass
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from hesabyar.application.commands.events import EventPipelineService
from hesabyar.application.commands.integrations import (
    ChannelRegistryService,
    GatewayContractService,
    PolicyGateService,
)
from hesabyar.application.commands.policy_gate import RegistryPolicyGate
from hesabyar.application.commands.settlement import SettlementCommandService
from hesabyar.application.commands.settlement_posting import SettlementPostingService
from hesabyar.application.commands.store_events import StoreDraftBuilder, StoreMappingResolver
from hesabyar.application.queries.fiscal import SetupStatusService
from hesabyar.domain.accounting.commands import (
    CreateInvoiceDraft,
    CreateJournalDraft,
    InvoiceDraftLine,
    JournalDraftLine,
    PostJournal,
)
from hesabyar.domain.accounting.refs import (
    AccountRef,
    CompanyRef,
    InvoiceRef,
    JournalRef,
    PartyAddressRef,
    PartyRef,
    PaymentJournalRef,
    PaymentRef,
    PaymentTermRef,
)
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.domain.events.envelope import CanonicalEvent, CanonicalEventType
from hesabyar.domain.events.models import PipelineOutcome
from hesabyar.domain.integrations.commands import CreateIntegrationMapping
from hesabyar.domain.integrations.models import MappingKind, TrancheLeg
from hesabyar.domain.settlement.commands import (
    CreateGatewayPayment as SettlementCreateGatewayPayment,
)
from hesabyar.domain.settlement.commands import (
    PostBankReceipt,
    PostSaleFee,
    PostTrancheClearing,
    SettlementPostingWiring,
)
from hesabyar.domain.settlement.models import PaymentSource, SettlementTranche
from hesabyar.infrastructure.accounting.tryton.adapter import TrytonAccountingAdapter
from hesabyar.infrastructure.accounting.tryton.client import TrytonAdapterError, TrytonRestClient
from hesabyar.infrastructure.accounting.tryton.fiscal_period_checker import (
    TrytonFiscalPeriodChecker,
)
from hesabyar.infrastructure.events.defaults import InboxReviewQueueAdapter
from hesabyar.infrastructure.persistence.postgres.event_repositories import (
    SqlAlchemyDecisionAuditLogRepository,
    SqlAlchemyEventInboxRepository,
    SqlAlchemyEventOutboxRepository,
    SqlAlchemyExternalIdMapRepository,
    SqlAlchemyStoreSettingsRepository,
)
from hesabyar.infrastructure.persistence.postgres.integrations_repository import (
    SqlAlchemyIntegrationsRepository,
)
from hesabyar.infrastructure.persistence.postgres.models import (
    AutoPostPolicyModel,
    BankDepositModel,
    Base,
    EngineCompanyModel,
    EngineConnectionModel,
    GatewayContractModel,
    GatewayPaymentModel,
    InstallationSettingsModel,
    IntegrationMappingModel,
    PaymentChannelModel,
    SettlementEventModel,
    SettlementExceptionModel,
    SettlementFeeRecordModel,
    SettlementFileImportModel,
    SettlementMatchModel,
    SettlementPostingModel,
    SettlementTrancheModel,
    TenantModel,
)
from hesabyar.infrastructure.persistence.postgres.settlement_repository import (
    SqlAlchemySettlementAudit,
    SqlAlchemySettlementRepository,
)
from tests.event_fixtures import raw_envelope

HESABYAR_DB_URL = os.environ.get("HESABYAR_DATABASE_URL", "")
TRYTON_BASE_URL = os.environ.get("TRYTON_BASE_URL", "http://localhost:8000")
TRYTON_DATABASE = os.environ.get("TRYTON_DATABASE", "hesabyar_tryton_test")
TRYTON_API_KEY = os.environ.get("TRYTON_API_KEY", "hesabyar-dev-rest-key")
TRYTON_ADMIN_LOGIN = os.environ.get("TRYTON_ADMIN_LOGIN", "admin")

# The generic connector's reference store (docs/store-connector.md):
# the custom-coded Hayat store. The connector stays generic — nothing
# in this scenario is Hayat-specific; "hayat" is just the reference
# source_system the contract tests use.
SOURCE_SYSTEM = "hayat"
SOURCE_STORE_ID = "store-1"
CUSTOMER_ID = "cust-706"
CUSTOMER_NAME = "HesabYar US-706 Fresh Install Customer"
GATEWAY_NAME = "HesabYar US-706 Fresh Install Gateway"

# Chart codes (universal chart + hesabyar_setup extension).
BANK_ACCOUNT_CODE = "1.1.1"
CLEARING_ACCOUNT_CODE = "1.1.6"
RECEIVABLE_ACCOUNT_CODE = "1.2.1"
GATEWAY_RECEIVABLE_CODE = "1.2.8"
PAYABLE_ACCOUNT_CODE = "2.1.1"
CAPITAL_ACCOUNT_CODE = "3.1.1"
REVENUE_ACCOUNT_CODE = "4.1.1"
SHIPPING_ACCOUNT_CODE = "4.1.2"
FEE_EXPENSE_ACCOUNT_CODE = "5.2.4"
FEE_VAT_ACCOUNT_CODE = "6.3.6"
REQUIRED_CODES = (
    BANK_ACCOUNT_CODE,
    CLEARING_ACCOUNT_CODE,
    RECEIVABLE_ACCOUNT_CODE,
    GATEWAY_RECEIVABLE_CODE,
    PAYABLE_ACCOUNT_CODE,
    CAPITAL_ACCOUNT_CODE,
    REVENUE_ACCOUNT_CODE,
    SHIPPING_ACCOUNT_CODE,
    FEE_EXPENSE_ACCOUNT_CODE,
    FEE_VAT_ACCOUNT_CODE,
)

# Opening balances: 50,000,000 IRR of owner's cash against issued capital.
OPENING_BALANCE_IRR = 50_000_000

# The first order (Toman, as the store prices it): 2×10,000 + 1×5,000,
# shipping 3,000, discount 1,000, tax 500, total 27,500. The invoice
# carries products + shipping only (28,000 Toman = 280,000 IRR —
# tax/discount are validated but not posted as lines, US-511).
ORDER_TOTAL_TOMAN = 27_500
INVOICE_TOTAL_IRR = 280_000

# The first settlement (IRR): the gateway payment is the invoice total;
# the contract takes a 2% fee + 10% VAT on the fee — all exact, no
# rounding ambiguity (5,600 + 560 → net 273,840). The webhook's fee
# fields carry the same figures, so the gateway's report and the
# contract derivation agree.
SETTLEMENT_GROSS_IRR = 280_000
SETTLEMENT_FEE_IRR = 5_600
SETTLEMENT_FEE_VAT_IRR = 560
SETTLEMENT_NET_IRR = 273_840

# Expected trial-balance totals over the scenario's six posted moves:
# opening 50,000,000 + customer invoice 280,000 + settlement invoice
# 280,000 + fee journal 6,160 + clearing 273,840 + bank receipt 273,840.
EXPECTED_TRIAL_BALANCE_TOTAL_IRR = 51_113_840
EXPECTED_MOVE_COUNT = 6
# Lines: opening 2 + customer invoice 4 (receivable + 3 invoice lines)
# + settlement invoice 2 + fee journal 3 + clearing 2 + receipt 2.
EXPECTED_LINE_COUNT = 15

TODAY = datetime.date.today()

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


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Fixtures: live servers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def hesabyar_engine() -> Generator[Engine, None, None]:
    if not HESABYAR_DB_URL:
        pytest.skip("HESABYAR_DATABASE_URL is not set (source .env.supabase).")
    eng = create_engine(HESABYAR_DB_URL, echo=False)
    try:
        with eng.connect():
            pass
    except Exception:
        eng.dispose()
        pytest.skip("HesabYar DB is not reachable.")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def tryton_client() -> Generator[TrytonRestClient, None, None]:
    # 120s timeout: install_chart + update_chart are heavy admin actions.
    rest_client = TrytonRestClient(TRYTON_BASE_URL, TRYTON_DATABASE, TRYTON_API_KEY, timeout=120.0)
    if not rest_client.ping():
        rest_client.close()
        pytest.skip(
            f"trytond server not reachable at {TRYTON_BASE_URL} "
            "(start it per docs/dev-setup.md §3 to run the contract tests)"
        )
    yield rest_client
    rest_client.close()


@pytest.fixture(scope="module")
def adapter(tryton_client: TrytonRestClient) -> TrytonAccountingAdapter:
    return TrytonAccountingAdapter(tryton_client)


# ---------------------------------------------------------------------------
# Provisioning: the docs/setup-wizard.md §1 flow, over REST only
# ---------------------------------------------------------------------------


@dataclass
class FreshCompany:
    """Everything the business-flow test needs from provisioning."""

    company: CompanyRef
    company_name: str
    party_id: int
    general_journal: JournalRef
    payment_journal: PaymentJournalRef
    customer: PartyRef
    customer_address: PartyAddressRef
    gateway: PartyRef
    accounts: dict[str, AccountRef]
    fiscal_years: tuple[int, int]


def _model_data_id(client: TrytonRestClient, module: str, fs_id: str) -> int:
    """Resolve any XML-loaded record's id via ``ir.model.data`` (REST)."""
    records = client.search(
        "ir.model.data",
        domain=[("module", "=", module), ("fs_id", "=", fs_id)],
        fields=["db_id"],
    )
    assert records, f"ir.model.data {module}.{fs_id} not found in the engine"
    return int(records[0]["db_id"])


def _sequence_type_id(client: TrytonRestClient, module: str, fs_id: str) -> int:
    """Resolve an ``ir.sequence`` type via ``ir.model.data`` (REST)."""
    records = client.search(
        "ir.model.data",
        domain=[("module", "=", module), ("fs_id", "=", fs_id)],
        fields=["db_id"],
    )
    assert records, f"sequence type {module}.{fs_id} not found in the engine"
    return int(records[0]["db_id"])


def _create_fiscal_year(
    client: TrytonRestClient, company_id: int, move_sequence_id: int, year: int
) -> int:
    """Create one fiscal year + 12 open monthly periods (REST).

    Periods come from the engine's ``create_period`` RPC action (the
    same action the setup-wizard spike verified live) — never from
    hand-created period rows, so the engine owns the period rules.
    """
    record = client.create(
        "account.fiscalyear",
        {
            "name": f"FY {year}",
            "start_date": f"{year}-01-01",
            "end_date": f"{year}-12-31",
            "company": company_id,
            "move_sequence": move_sequence_id,
        },
        fields=["id"],
        context={"company": company_id},
    )
    fiscal_year_id = int(record["id"])
    client.action(
        "account.fiscalyear",
        "create_period",
        fiscal_year_id,
        context={"company": company_id},
    )
    periods = client.search(
        "account.period",
        domain=[("fiscalyear", "=", fiscal_year_id)],
        fields=["name", "state"],
        context={"company": company_id},
    )
    open_periods = [p for p in periods if p["state"] == "open"]
    assert len(periods) == 12, f"FY {year}: expected 12 periods, got {len(periods)}"
    assert len(open_periods) == 12, f"FY {year}: not all periods are open"
    return fiscal_year_id


def _create_invoice_sequences(
    client: TrytonRestClient, company_id: int, fiscal_year_id: int, company_name: str
) -> None:
    """The 4 invoice sequences + the fiscalyear row (posting needs them)."""
    seq_type_id = _sequence_type_id(client, "account_invoice", "sequence_type_account_invoice")
    seqs: dict[str, int] = {}
    for key, name in [
        ("out_invoice", "Customer Invoice"),
        ("out_credit_note", "Customer Credit Note"),
        ("in_invoice", "Supplier Invoice"),
        ("in_credit_note", "Supplier Credit Note"),
    ]:
        record = client.create(
            "ir.sequence.strict",
            {
                "name": f"{company_name} {name} Sequence",
                "sequence_type": seq_type_id,
                "company": company_id,
                "type": "incremental",
                "number_next": 1,
                "number_increment": 1,
                "padding": 4,
            },
            fields=["id"],
            context={"company": company_id},
        )
        seqs[key] = int(record["id"])
    client.create(
        "account.fiscalyear.invoice_sequence",
        {
            "company": company_id,
            "fiscalyear": fiscal_year_id,
            "out_invoice_sequence": seqs["out_invoice"],
            "out_credit_note_sequence": seqs["out_credit_note"],
            "in_invoice_sequence": seqs["in_invoice"],
            "in_credit_note_sequence": seqs["in_credit_note"],
        },
        fields=["id"],
        context={"company": company_id},
    )


@pytest.fixture(scope="module")
def fresh_company(
    tryton_client: TrytonRestClient, adapter: TrytonAccountingAdapter
) -> Generator[FreshCompany, None, None]:
    """Provision a brand-new company via REST only (setup-wizard §1).

    One module-scoped company for the whole file: provisioning is the
    slow part (chart install + update), and the business-flow test
    runs exactly once against it. The company name is unique per run.
    """
    client = tryton_client
    company_name = f"HesabYar US-706 Fresh Install {_unique('store')}"

    # -- 1. IRR currency (must exist; a fresh DB without IRR cannot
    #    host a company at all — skip, do not invent a currency here).
    currencies = client.search("currency.currency", domain=[("code", "=", "IRR")], fields=["code"])
    if not currencies:
        pytest.skip("IRR currency not found in the test DB — cannot provision a company.")
    irr_currency_id = int(currencies[0]["id"])

    # -- 2. Party + company.
    party = client.create("party.party", {"name": company_name}, fields=["id"])
    party_id = int(party["id"])
    company_record = client.create(
        "company.company", {"party": party_id, "currency": irr_currency_id}, fields=["id"]
    )
    company_id = int(company_record["id"])
    context = {"company": company_id}
    company = CompanyRef(company_id)

    # -- 3. Grant the company to the API user + company_filter='all'.
    #    Without the grant, every company-scoped read/write fails the
    #    ir.rule (setup-wizard §4.1); without 'all', a fresh DB's user
    #    sees nothing at all (§4.2). The REST key belongs to the admin
    #    user (scripts/ci/create_rest_key.py), so the grant targets it.
    users = client.search(
        "res.user", domain=[("login", "=", TRYTON_ADMIN_LOGIN)], fields=["login"]
    )
    if not users:
        pytest.skip(f"Tryton user {TRYTON_ADMIN_LOGIN!r} not found — cannot grant the company.")
    admin_id = int(users[0]["id"])
    client.write(
        "res.user",
        admin_id,
        {"companies": [["add", [company_id]]], "company_filter": "all"},
    )

    # -- 4. Install the chart from the ENGLISH root template, then the
    #    HesabYar extension accounts (update_chart is what adds
    #    1.1.6 / 1.2.8 / 5.2.4 / ... — install_chart alone installs
    #    only the universal template). Four root templates ship in the
    #    account module (de/en/es/fr — tryton.cfg loads all four
    #    localized charts), so "exactly one root" is false; and the
    #    HesabYar extension accounts live under the EN root
    #    (hesabyar_setup/account_chart_en.xml), so the en root is the
    #    one this fixture needs. Resolve it deterministically via
    #    ir.model.data instead of searching for parentless templates.
    template_id = _model_data_id(client, "account", "account_template_root_en")
    summary = client.action(
        "company.company",
        "install_chart",
        company_id,
        data={"template_id": template_id},
        context=context,
    )
    assert summary["company"] == company_id
    assert summary["accounts"] >= 130, f"chart install looks partial: {summary}"
    update = client.action("company.company", "update_chart", company_id, context=context)
    assert update["company"] == company_id

    chart = {view.code: view for view in adapter.get_chart_of_accounts(company)}
    missing = [code for code in REQUIRED_CODES if code not in chart]
    if missing:
        pytest.skip(f"Chart accounts missing from the fresh company: {missing}.")
    accounts = {code: chart[code].ref for code in REQUIRED_CODES}

    # -- 5. Move sequence + fiscal years (this year AND next year:
    #    the settlement due date lands in the next Jalali month, which
    #    can cross the Gregorian year boundary) + invoice sequences
    #    for the current year (both invoices are dated today).
    move_seq_type = _sequence_type_id(client, "account", "sequence_type_account_move")
    move_sequence = client.create(
        "ir.sequence.strict",
        {
            "name": f"{company_name} Move Sequence",
            "sequence_type": move_seq_type,
            "company": company_id,
            "type": "incremental",
            "number_next": 1,
            "number_increment": 1,
            "padding": 4,
        },
        fields=["id"],
        context=context,
    )
    move_sequence_id = int(move_sequence["id"])
    this_year = TODAY.year
    fy_this = _create_fiscal_year(client, company_id, move_sequence_id, this_year)
    fy_next = _create_fiscal_year(client, company_id, move_sequence_id, this_year + 1)
    _create_invoice_sequences(client, company_id, fy_this, company_name)

    # -- 6. Journals: one general journal (explicitly passed everywhere
    #    — the adapter's implicit resolution needs EXACTLY ONE general
    #    journal in the engine, and the shared DB already has others)
    #    and one clearing-configured payment journal (the settlement
    #    wiring requires clearing_account + clearing_journal; the
    #    store pipeline's payment draft uses the same journal).
    journal_record = client.create(
        "account.journal",
        {
            "name": f"{company_name} General Journal",
            "code": f"HY706-{uuid.uuid4().hex[:6].upper()}",
            "type": "general",
        },
        fields=["id"],
        context=context,
    )
    general_journal = JournalRef(int(journal_record["id"]))
    payment_journal_record = client.create(
        "account.payment.journal",
        {
            "name": f"{company_name} Payments",
            "currency": irr_currency_id,
            "company": company_id,
            "process_method": "manual",
            "clearing_account": accounts[CLEARING_ACCOUNT_CODE].engine_id,
            "clearing_journal": general_journal.engine_id,
        },
        fields=["id"],
        context=context,
    )
    payment_journal = PaymentJournalRef(int(payment_journal_record["id"]))

    # -- 7. Customer party + address, gateway party.
    customer_record = client.create("party.party", {"name": CUSTOMER_NAME}, fields=["id"])
    customer_id = int(customer_record["id"])
    address_record = client.create(
        "party.address", {"party": customer_id, "city": "Tehran"}, fields=["id"]
    )
    gateway_record = client.create(
        "party.party", {"name": f"{GATEWAY_NAME} {uuid.uuid4().hex[:8]}"}, fields=["id"]
    )

    yield FreshCompany(
        company=company,
        company_name=company_name,
        party_id=party_id,
        general_journal=general_journal,
        payment_journal=payment_journal,
        customer=PartyRef(customer_id),
        customer_address=PartyAddressRef(int(address_record["id"])),
        gateway=PartyRef(int(gateway_record["id"])),
        accounts=accounts,
        fiscal_years=(fy_this, fy_next),
    )
    # No Tryton teardown: the company holds posted moves, which
    # reference the journals/parties — deletion would fail. The
    # company is uniquely named per run, so it never collides with
    # another run (the same residue precedent as the other contract
    # tests, which leave posted invoices behind).


# ---------------------------------------------------------------------------
# Fixtures: per-test HesabYar world (pipeline + settlement, one tenant)
# ---------------------------------------------------------------------------


@dataclass
class World:
    """One test's HesabYar installation: tenant, mappings, services."""

    installation_id: str
    tenant_id: str
    session: Session
    pipeline: EventPipelineService
    inbox: SqlAlchemyEventInboxRepository
    external_ids: SqlAlchemyExternalIdMapRepository
    settlement: SettlementCommandService
    posting: SettlementPostingService
    channels: ChannelRegistryService
    contracts: GatewayContractService
    registry: SqlAlchemyIntegrationsRepository
    repository: SqlAlchemySettlementRepository

    def commit(self) -> None:
        self.session.commit()


@pytest.fixture()
def world(
    hesabyar_engine: Engine,
    adapter: TrytonAccountingAdapter,
    fresh_company: FreshCompany,
) -> Generator[World, None, None]:
    """Seed a tenant + engine-company mapping + store mappings; build
    the real pipeline and the real settlement services."""
    installation_id = str(uuid.uuid4())
    tenant_id = ""
    connection_id = ""
    session = sessionmaker(bind=hesabyar_engine)()
    try:
        tenant = TenantModel(
            id=str(uuid.uuid4()),
            slug=f"fresh-706-{uuid.uuid4().hex[:8]}",
            name="US-706 Fresh Install Tenant",
        )
        session.add(tenant)
        session.flush()
        tenant_id = tenant.id
        connection = EngineConnectionModel(
            id=str(uuid.uuid4()),
            name=f"conn-706-{uuid.uuid4().hex[:8]}",
            engine="tryton",
            base_url=TRYTON_BASE_URL,
            database_name=TRYTON_DATABASE,
        )
        session.add(connection)
        session.flush()
        connection_id = connection.id
        # The tenant↔company mapping: without this row the dashboard
        # cannot see the company at all (the list endpoint joins
        # engine companies against the tenant's mappings).
        session.add(
            EngineCompanyModel(
                id=str(uuid.uuid4()),
                tenant_id=tenant.id,
                engine_connection_id=connection.id,
                external_company_id=str(fresh_company.company.engine_id),
                company_name=fresh_company.company_name,
            )
        )
        session.flush()

        registry = SqlAlchemyIntegrationsRepository(session)

        def _mapping(kind: MappingKind, channel: str, source: str, ttype: str, tref: str):
            registry.save_mapping(
                CreateIntegrationMapping(
                    installation_id=installation_id,
                    mapping_kind=kind,
                    channel_key=channel,
                    source_key=source,
                    target_type=ttype,
                    target_ref=tref,
                    version=1,
                )
            )

        _mapping(
            MappingKind.STORE_COMPANY,
            "",
            f"{SOURCE_SYSTEM}:{SOURCE_STORE_ID}",
            "company",
            str(fresh_company.company.engine_id),
        )
        _mapping(
            MappingKind.EXTERNAL_PARTY,
            "",
            CUSTOMER_ID,
            "party",
            str(fresh_company.customer.engine_id),
        )
        _mapping(
            MappingKind.REVENUE_CATEGORY, "", "electronics", "account", REVENUE_ACCOUNT_CODE
        )
        _mapping(
            MappingKind.REVENUE_CATEGORY, "", "default", "account", REVENUE_ACCOUNT_CODE
        )
        _mapping(
            MappingKind.SHIPPING_CHARGE, "", "default", "account", SHIPPING_ACCOUNT_CODE
        )
        _mapping(
            MappingKind.GATEWAY_JOURNAL,
            "zarinpal",
            "zarinpal",
            "journal",
            str(fresh_company.payment_journal.engine_id),
        )
        session.commit()

        inbox = SqlAlchemyEventInboxRepository(session)
        outbox = SqlAlchemyEventOutboxRepository(session)
        external_ids = SqlAlchemyExternalIdMapRepository(session)
        pipeline = EventPipelineService(
            inbox=inbox,
            outbox=outbox,
            external_ids=external_ids,
            audit_log=SqlAlchemyDecisionAuditLogRepository(session),
            store_settings=SqlAlchemyStoreSettingsRepository(session),
            mapping_resolver=StoreMappingResolver(registry, external_ids, adapter),
            draft_builder=StoreDraftBuilder(adapter, external_ids),
            # The REAL registry gate with no settings/policy rows: it
            # denies auto-post, so the scenario stays drafts until the
            # test posts explicitly — draft by default (ADR 0003).
            policy_gate=RegistryPolicyGate(
                PolicyGateService(registry, TrytonFiscalPeriodChecker(adapter))
            ),
            review_queue=InboxReviewQueueAdapter(),
            engine=adapter,
        )
        repository = SqlAlchemySettlementRepository(session)
        audit = SqlAlchemySettlementAudit(session)
        yield World(
            installation_id=installation_id,
            tenant_id=tenant_id,
            session=session,
            pipeline=pipeline,
            inbox=inbox,
            external_ids=external_ids,
            settlement=SettlementCommandService(repository, registry, audit),
            posting=SettlementPostingService(repository, registry, adapter, audit),
            channels=ChannelRegistryService(registry),
            contracts=GatewayContractService(registry),
            registry=registry,
            repository=repository,
        )
    finally:
        session.close()
        cleanup = sessionmaker(bind=hesabyar_engine)()
        try:
            for model in _SETTLEMENT_TABLES:
                cleanup.execute(
                    delete(model).where(model.installation_id == installation_id)  # type: ignore[attr-defined]
                )
            channel_ids = list(
                cleanup.execute(
                    select(PaymentChannelModel.id).where(
                        PaymentChannelModel.installation_id == installation_id
                    )
                ).scalars()
            )
            if channel_ids:
                cleanup.execute(
                    delete(GatewayContractModel).where(GatewayContractModel.channel_id.in_(channel_ids))
                )
            cleanup.execute(
                delete(PaymentChannelModel).where(PaymentChannelModel.installation_id == installation_id)
            )
            cleanup.execute(
                delete(IntegrationMappingModel).where(
                    IntegrationMappingModel.installation_id == installation_id
                )
            )
            cleanup.execute(
                delete(AutoPostPolicyModel).where(AutoPostPolicyModel.installation_id == installation_id)
            )
            cleanup.execute(
                delete(InstallationSettingsModel).where(
                    InstallationSettingsModel.installation_id == installation_id
                )
            )
            if tenant_id:
                cleanup.execute(
                    delete(EngineCompanyModel).where(EngineCompanyModel.tenant_id == tenant_id)
                )
                cleanup.query(TenantModel).filter(TenantModel.id == tenant_id).delete(
                    synchronize_session=False
                )
            if connection_id:
                cleanup.execute(
                    delete(EngineConnectionModel).where(EngineConnectionModel.id == connection_id)
                )
            cleanup.commit()
        finally:
            cleanup.close()


# ---------------------------------------------------------------------------
# Helpers: store events, settlement
# ---------------------------------------------------------------------------


def _store_event(
    world: World,
    event_type: CanonicalEventType,
    payload: dict,
    *,
    source_event_id: str,
    entity_type: str,
    entity_id: str,
) -> str:
    # occurred_at is dynamic (now): the draft builder dates the
    # invoice from it, and Tryton numbers invoices chronologically.
    event = CanonicalEvent.from_raw(
        raw_envelope(
            source_system=SOURCE_SYSTEM,
            source_store_id=SOURCE_STORE_ID,
            source_event_id=source_event_id,
            payload=payload,
            external_entity_type=entity_type,
            external_entity_id=entity_id,
            occurred_at=datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z"),
        )
    )
    outcome = world.inbox.store(
        event, event_type, world.tenant_id, installation_id=world.installation_id
    )
    assert outcome.status == "created", outcome
    assert outcome.event is not None
    world.session.commit()
    return outcome.event.id


def _process(world: World, event_id: str):
    result = world.pipeline.process(event_id)
    world.session.commit()
    return result


def _order_payload(order_id: str) -> dict:
    """A valid order.completed payload (Toman): 2×10,000 + 1×5,000,
    shipping 3,000, discount 1,000, tax 500, total 27,500."""
    return {
        "order_id": order_id,
        "customer": {"id": CUSTOMER_ID, "email": "a@b.ir"},
        "lines": [
            {
                "product_sku": "SKU-1",
                "quantity": 2,
                "unit_price": 10000,
                "line_total": 20000,
                "revenue_category": "electronics",
            },
            {"product_sku": "SKU-2", "quantity": 1, "unit_price": 5000},
        ],
        "subtotal_amount": 25000,
        "shipping_amount": 3000,
        "discount_amount": 1000,
        "tax_amount": 500,
        "total_amount": ORDER_TOTAL_TOMAN,
        "status": "completed",
    }


def _payment_payload(gateway_payment_id: str, order_id: str) -> dict:
    """A valid payment.captured payload (Toman) for the order's total.

    Gross 28,000 = the invoice total; fee 560 + fee VAT 56 agree
    with the settlement contract (2% + 10% VAT), so the gateway's
    report and the contract derivation tie out exactly.
    """
    return {
        "payment_channel": "zarinpal",
        "gateway_payment_id": gateway_payment_id,
        "gross_amount": 28000,
        "fee_amount": 560,
        "fee_tax_amount": 56,
        "net_amount": 27384,
        "order_id": order_id,
        "customer": {"id": CUSTOMER_ID},
        "verified": True,
    }


def _setup_channel_and_contract(world: World) -> None:
    """Create the zarinpal channel + an active single-tranche contract.

    One leg at 100% (``monthly_lump``): the whole net arrives in a
    single payout on the 5th of the next Jalali month. The fee % is
    entered manually per contract (ADR 0003) — 2% here, the
    scenario's own figure, never a hard-coded product default.
    """
    world.channels.create_channel(world.installation_id, "zarinpal", "زرین‌پال", "gateway")
    world.contracts.create_version(
        world.installation_id,
        "zarinpal",
        valid_from=TODAY - datetime.timedelta(days=30),
        fee_percent=Decimal("2"),
        fee_vat_rate=Decimal("10"),
        settlement_model="monthly_lump",
        tranche_legs=(TrancheLeg(month_offset=1, payout_day=5, percent=Decimal("100")),),
        payout_window_start_day=1,
        payout_window_end_day=5,
        grace_days_after_due=3,
        fee_accrual_policy="at_sale",
    )
    world.commit()


def _record_sale(world: World, *, transaction_id: str, order_id: str):
    """Record the gateway payment for the webhook's transaction."""
    result = world.settlement.record_payment(
        SettlementCreateGatewayPayment(
            installation_id=world.installation_id,
            channel_key="zarinpal",
            gateway_transaction_id=transaction_id,
            gross_irr=SETTLEMENT_GROSS_IRR,
            gateway_confirmed_at=datetime.datetime.combine(
                TODAY, datetime.time(12, 0), tzinfo=datetime.UTC
            ),
            source=PaymentSource.STORE_WEBHOOK,
            store_order_id=order_id,
        )
    )
    world.commit()
    return result.payment, result.tranches


def _create_single_leg_term(
    client: TrytonRestClient, company: CompanyRef, due: datetime.date
) -> PaymentTermRef:
    """A 1-leg payment term: remainder, maturing on the due date."""
    record = client.create(
        "account.invoice.payment_term",
        {
            "name": f"HesabYar US-706 Term {uuid.uuid4().hex[:8]}",
            "lines": [
                [
                    "create",
                    [
                        {
                            "type": "remainder",
                            "relativedeltas": [
                                [
                                    "create",
                                    [
                                        {
                                            "months": 0,
                                            "weeks": 0,
                                            "days": (due - TODAY).days,
                                        }
                                    ],
                                ]
                            ],
                        }
                    ],
                ]
            ],
        },
        fields=["id"],
        context={"company": company.engine_id},
    )
    return PaymentTermRef(int(record["id"]))


def _post_gateway_invoice(
    adapter: TrytonAccountingAdapter,
    fresh: FreshCompany,
    term: PaymentTermRef,
):
    """Create + validate + post the gateway-payable sale invoice.

    The receivable is from the GATEWAY (account 1.2.8, party=gateway
    via alternative payees) — the settlement engine's invoice, not
    the pipeline's customer invoice: the pipeline invoice books the
    sale against the customer, the settlement invoice books the same
    sale's receivable against the gateway that owes the money.
    """
    ref = adapter.create_invoice_draft(
        CreateInvoiceDraft(
            company=fresh.company,
            invoice_type="out",
            party=fresh.customer,
            date=FiscalDate(TODAY),
            account=fresh.accounts[GATEWAY_RECEIVABLE_CODE],
            lines=(
                InvoiceDraftLine(
                    account=fresh.accounts[REVENUE_ACCOUNT_CODE],
                    unit_price=MoneyIRR(SETTLEMENT_GROSS_IRR),
                    quantity=Decimal(1),
                    description="US-706 fresh-install gateway sale",
                ),
            ),
            payment_term=term,
            alternative_payees=(fresh.gateway,),
            description="US-706 first settlement sale",
        )
    )
    adapter.validate_invoice(ref)
    adapter.post_invoice(ref)
    return ref


def _invoice_receivable_lines(
    client: TrytonRestClient, invoice_id: int, receivable_account_id: int
) -> list[dict]:
    """The posted invoice's receivable lines, ordered by maturity date."""
    invoice = client.read("account.invoice", invoice_id, fields=["move"])
    assert invoice is not None and invoice["move"], "posted invoice has no move"
    return client.search(
        "account.move.line",
        domain=[
            ("move", "=", int(invoice["move"])),
            ("account", "=", receivable_account_id),
        ],
        fields=["debit", "credit", "party", "maturity_date", "reconciliation"],
        order=[["maturity_date", "ASC"]],
    )


def _wiring(fresh: FreshCompany) -> SettlementPostingWiring:
    return SettlementPostingWiring(
        company=fresh.company,
        gateway_party=fresh.gateway,
        payment_journal=fresh.payment_journal,
        receivable_account=fresh.accounts[GATEWAY_RECEIVABLE_CODE],
        clearing_account=fresh.accounts[CLEARING_ACCOUNT_CODE],
        bank_account=fresh.accounts[BANK_ACCOUNT_CODE],
        fee_expense_account=fresh.accounts[FEE_EXPENSE_ACCOUNT_CODE],
        fee_vat_account=fresh.accounts[FEE_VAT_ACCOUNT_CODE],
        revenue_account=fresh.accounts[REVENUE_ACCOUNT_CODE],
        general_journal=fresh.general_journal,
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


def _posted_line_balance(
    client: TrytonRestClient,
    account_id: int,
    *,
    party_id: int | None = None,
) -> int:
    """Posted debit − credit on one account (optionally party-scoped).

    Exact even with other tests' residue on the shared chart when
    party-scoped, because each run uses a fresh gateway party — and
    exact unscoped here too, because the fresh company has no other
    moves at all.
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


# ---------------------------------------------------------------------------
# Test 1: provisioning leaves the company ready
# ---------------------------------------------------------------------------


def test_provisioning_leaves_company_ready(
    tryton_client: TrytonRestClient,
    adapter: TrytonAccountingAdapter,
    fresh_company: FreshCompany,
) -> None:
    """The setup-status endpoint sees a ready company (chart + fiscal
    years + open periods), the invoice sequences exist, and a second
    chart install is refused cleanly."""
    status = SetupStatusService(adapter).get_setup_status(fresh_company.company)
    assert status.chart_installed
    assert status.account_count >= 150, f"chart looks partial: {status.account_count} accounts"
    assert status.fiscal_year_present
    assert status.fiscal_year_count == 2
    assert status.open_period_present
    assert status.open_period_count == 24, "two fiscal years × 12 open periods"
    assert status.ready
    assert status.open_fiscal_year is not None
    # Years come newest first, so the first open year is next year's.
    assert status.open_fiscal_year.name == f"FY {TODAY.year + 1}"

    # Invoice sequences exist for the current fiscal year.
    rows = tryton_client.search(
        "account.fiscalyear.invoice_sequence",
        domain=[("fiscalyear", "=", fresh_company.fiscal_years[0])],
        fields=["out_invoice_sequence"],
        context={"company": fresh_company.company.engine_id},
    )
    assert len(rows) == 1, "exactly one invoice-sequence row for the current fiscal year"

    # The duplicate-chart guard: a second install is refused with a
    # plain error, not a partial second chart.
    templates = tryton_client.search(
        "account.account.template",
        domain=[("parent", "=", None)],
        fields=["name"],
    )
    with pytest.raises(TrytonAdapterError):
        tryton_client.action(
            "company.company",
            "install_chart",
            fresh_company.company.engine_id,
            data={"template_id": int(templates[0]["id"])},
            context={"company": fresh_company.company.engine_id},
        )
    # And the chart is unchanged by the refused install.
    chart_after = adapter.get_chart_of_accounts(fresh_company.company)
    assert len(chart_after) == status.account_count


# ---------------------------------------------------------------------------
# Test 2: the business flow — opening balances → order → payment →
# settlement → trial balance
# ---------------------------------------------------------------------------


def test_fresh_store_business_flow(
    tryton_client: TrytonRestClient,
    adapter: TrytonAccountingAdapter,
    fresh_company: FreshCompany,
    world: World,
) -> None:
    """The phase exit criterion, end to end, on the fresh company."""
    company = fresh_company.company
    accounts = fresh_company.accounts
    client = tryton_client

    # -- Opening balances: owner's cash in, against issued capital. --
    opening_draft = adapter.create_journal_draft(
        CreateJournalDraft(
            company=company,
            date=FiscalDate(TODAY),
            lines=(
                JournalDraftLine(
                    account=accounts[BANK_ACCOUNT_CODE],
                    debit=MoneyIRR(OPENING_BALANCE_IRR),
                    description="Opening balance — cash",
                ),
                JournalDraftLine(
                    account=accounts[CAPITAL_ACCOUNT_CODE],
                    credit=MoneyIRR(OPENING_BALANCE_IRR),
                    description="Opening balance — issued capital",
                ),
            ),
            description="US-706 opening balances",
            journal=fresh_company.general_journal,
        )
    )
    summary = adapter.validate_journal_draft(opening_draft)
    assert summary.valid, summary.issues
    posted_opening = adapter.post_journal(PostJournal(opening_draft))
    opening_view = adapter.get_journal(posted_opening)
    assert opening_view is not None
    assert opening_view.number, "the posted opening journal has an engine number"

    # -- First order via the generic connector → invoice → posted. --
    order_id = _unique("order-706")
    order_event = _store_event(
        world,
        CanonicalEventType.SALES_ORDER_COMPLETED,
        _order_payload(order_id),
        source_event_id=_unique("evt-order"),
        entity_type="order",
        entity_id=order_id,
    )
    order_result = _process(world, order_event)
    assert order_result.outcome == PipelineOutcome.PROCESSED_DRAFT, order_result.error
    assert order_result.draft_ref is not None
    invoice_id = int(order_result.draft_ref)
    invoice = adapter.get_invoice(InvoiceRef(invoice_id))
    assert invoice is not None
    assert invoice.state == "draft"
    assert invoice.company == company
    assert invoice.total_amount.amount == INVOICE_TOTAL_IRR
    assert invoice.reference == f"{SOURCE_SYSTEM}:{order_id}"
    found = world.external_ids.get(SOURCE_SYSTEM, "order", order_id)
    assert found is not None and found.target_external_id == str(invoice_id)
    adapter.validate_invoice(InvoiceRef(invoice_id))
    posted_invoice = adapter.post_invoice(InvoiceRef(invoice_id))
    assert posted_invoice.state == "posted"

    # -- First payment via the generic connector → payment draft. --
    gateway_payment_id = _unique("pay-706")
    payment_event = _store_event(
        world,
        CanonicalEventType.PAYMENT_CAPTURED,
        _payment_payload(gateway_payment_id, order_id),
        source_event_id=_unique("evt-pay"),
        entity_type="payment",
        entity_id=gateway_payment_id,
    )
    payment_result = _process(world, payment_event)
    assert payment_result.outcome == PipelineOutcome.PROCESSED_DRAFT, payment_result.error
    assert payment_result.draft_ref is not None
    payment_view = adapter.get_payment(PaymentRef(int(payment_result.draft_ref)))
    assert payment_view is not None
    assert payment_view.kind == "receivable"
    assert payment_view.state == "draft"
    assert payment_view.amount.amount == SETTLEMENT_GROSS_IRR
    assert payment_view.reference == gateway_payment_id

    # -- First settlement: the same transaction through the engine. --
    _setup_channel_and_contract(world)
    payment, tranches = _record_sale(
        world, transaction_id=gateway_payment_id, order_id=order_id
    )
    assert len(tranches) == 1, "the single-leg contract derives exactly one tranche"
    tranche: SettlementTranche = tranches[0]
    assert tranche.gross_irr == SETTLEMENT_GROSS_IRR
    assert tranche.fee_irr == SETTLEMENT_FEE_IRR
    assert tranche.fee_vat_irr == SETTLEMENT_FEE_VAT_IRR
    assert tranche.net_irr == SETTLEMENT_NET_IRR
    due = tranche.expected_date
    assert due > TODAY, "the payout date is in the future"

    term = _create_single_leg_term(client, company, due)
    gateway_invoice_ref = _post_gateway_invoice(adapter, fresh_company, term)
    receivable_id = accounts[GATEWAY_RECEIVABLE_CODE].engine_id
    bank_id = accounts[BANK_ACCOUNT_CODE].engine_id
    invoice_lines = _invoice_receivable_lines(client, gateway_invoice_ref.engine_id, receivable_id)
    assert len(invoice_lines) == 1
    assert int(invoice_lines[0]["debit"]) == SETTLEMENT_GROSS_IRR
    assert int(invoice_lines[0]["credit"]) == 0
    assert int(invoice_lines[0]["party"]) == fresh_company.gateway.engine_id
    assert invoice_lines[0]["maturity_date"] == due
    updated = world.repository.update_tranche(
        tranche.id, tryton_receivable_line_id=int(invoice_lines[0]["id"])
    )
    assert updated is not None
    world.commit()
    assert (
        _posted_line_balance(client, receivable_id, party_id=fresh_company.gateway.engine_id)
        == SETTLEMENT_GROSS_IRR
    )

    wiring = _wiring(fresh_company)
    fee = world.posting.post_sale_fee(
        world.installation_id,
        PostSaleFee(payment_id=payment.id, wiring=wiring, date=TODAY),
    )
    world.commit()
    assert fee.fee_irr == SETTLEMENT_FEE_IRR
    assert fee.fee_vat_irr == SETTLEMENT_FEE_VAT_IRR
    assert fee.tryton_move_id is not None
    assert (
        _posted_line_balance(client, receivable_id, party_id=fresh_company.gateway.engine_id)
        == SETTLEMENT_NET_IRR
    )

    bank_before = _posted_line_balance(client, bank_id)
    _set_tranche_state(
        world, tranche.id, "confirmed", confirmed_net_irr=SETTLEMENT_NET_IRR
    )
    clearing = world.posting.post_tranche_clearing(
        world.installation_id,
        PostTrancheClearing(tranche_id=tranche.id, wiring=wiring, payout_date=due),
    )
    world.commit()
    assert clearing.amount_irr == SETTLEMENT_NET_IRR
    assert clearing.reconciliation is not None
    assert clearing.posting.tryton_move_id is not None
    clearing_move = client.read(
        "account.move", clearing.posting.tryton_move_id, fields=["date", "state"]
    )
    assert clearing_move is not None
    assert clearing_move["date"] == due
    assert clearing_move["state"] == "posted"
    assert (
        _posted_line_balance(client, receivable_id, party_id=fresh_company.gateway.engine_id)
        == 0
    )

    channel = world.registry.get_channel(world.installation_id, "zarinpal")
    assert channel is not None
    event_id = _setup_event_with_deposit(
        world, channel.id, tranche.id, due, deposit_irr=SETTLEMENT_NET_IRR
    )
    _set_tranche_state(world, tranche.id, "received", received_net_irr=SETTLEMENT_NET_IRR)
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
    assert receipt.amount_irr == SETTLEMENT_NET_IRR
    assert receipt.tie_out.passes
    assert receipt.reconciliation is not None
    assert receipt.tranche.state.value == "settled"
    assert _posted_line_balance(client, bank_id) == bank_before + SETTLEMENT_NET_IRR

    # -- The trial balance balances, with exact expected totals. --
    trial_balance = adapter.get_trial_balance(
        company,
        FiscalDate(datetime.date(TODAY.year, 1, 1)),
        FiscalDate(datetime.date(TODAY.year + 1, 12, 31)),
    )
    assert trial_balance.is_balanced
    assert trial_balance.total_debit.amount == EXPECTED_TRIAL_BALANCE_TOTAL_IRR
    assert trial_balance.total_credit.amount == EXPECTED_TRIAL_BALANCE_TOTAL_IRR
    assert trial_balance.move_count == EXPECTED_MOVE_COUNT
    assert trial_balance.line_count == EXPECTED_LINE_COUNT
    assert SetupStatusService(adapter).get_setup_status(company).ready
