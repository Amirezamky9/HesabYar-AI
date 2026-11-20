"""Contract tests for the US-511 store pipeline (live Tryton + HesabYar DB).

The REAL pipeline (StoreMappingResolver + StoreDraftBuilder +
EventPipelineService, real SQLAlchemy repositories, real Tryton
adapter) against the live trytond server and the Supabase HesabYar
DB, using ONLY the dedicated test company "HesabYar Adapter Test"
(provisioned by
``~/workspace/projects/hesabyar-reference/setup_adapter_test_company.py``;
see docs/adapter.md). They never touch another company's records.

When the server is not reachable (or the test company has not been
provisioned yet), every test SKIPS cleanly with a clear message --
the suite stays green without a server. With the server up, the
tests must PASS.

Each test uses a FRESH installation id + tenant: teardown deletes
the installation's registry mappings and the tenant (which cascades
to inbox events + audit decisions). Tryton drafts the test created
are best-effort deleted; posted invoices/credit notes are residue
(like the other contract tests leave behind).

Configuration (all optional, dev defaults match docs/dev-setup.md):
  HESABYAR_DATABASE_URL (the Supabase HesabYar DB)
  TRYTON_BASE_URL       (default http://localhost:8000)
  TRYTON_DATABASE       (default hesabyar_tryton_test)
  TRYTON_API_KEY        (default hesabyar-dev-rest-key)
  TRYTON_TEST_COMPANY   (default "HesabYar Adapter Test")

NOTE: the dev server serves Supabase on :8010, so run with
TRYTON_BASE_URL=http://localhost:8010 (the .env.supabase file only sets
HESABYAR_TRYTON__BASE_URL, which these tests do not read).
"""

from __future__ import annotations

import datetime
import os
import uuid
from collections.abc import Generator
from dataclasses import dataclass

import pytest
from sqlalchemy import create_engine, delete
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from hesabyar.application.commands.events import EventPipelineService
from hesabyar.application.commands.integrations import PolicyGateService
from hesabyar.application.commands.policy_gate import RegistryPolicyGate
from hesabyar.application.commands.store_events import StoreDraftBuilder, StoreMappingResolver
from hesabyar.domain.accounting.refs import (
    CompanyRef,
    InvoiceRef,
    PartyAddressRef,
    PartyRef,
    PaymentJournalRef,
    PaymentRef,
)
from hesabyar.domain.events.envelope import CanonicalEvent, CanonicalEventType
from hesabyar.domain.events.models import PipelineOutcome
from hesabyar.domain.integrations.commands import (
    CreateIntegrationMapping,
    UpsertInstallationSettings,
    UpsertPolicyGateConfig,
)
from hesabyar.domain.integrations.models import MappingKind
from hesabyar.infrastructure.accounting.tryton.adapter import TrytonAccountingAdapter
from hesabyar.infrastructure.accounting.tryton.client import TrytonRestClient
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
    Base,
    InstallationSettingsModel,
    IntegrationMappingModel,
    TenantModel,
)
from tests.event_fixtures import raw_envelope

HESABYAR_DB_URL = os.environ.get("HESABYAR_DATABASE_URL", "")
TRYTON_BASE_URL = os.environ.get("TRYTON_BASE_URL", "http://localhost:8000")
TRYTON_DATABASE = os.environ.get("TRYTON_DATABASE", "hesabyar_tryton_test")
TRYTON_API_KEY = os.environ.get("TRYTON_API_KEY", "hesabyar-dev-rest-key")
TEST_COMPANY_NAME = os.environ.get("TRYTON_TEST_COMPANY", "HesabYar Adapter Test")

SOURCE_SYSTEM = "hayat"
SOURCE_STORE_ID = "store-1"
CUSTOMER_ID = "cust-511"
CUSTOMER_NAME = "HesabYar US-511 Contract Customer"
PAYMENT_JOURNAL_NAME = "HesabYar US-511 Contract Payments"

REVENUE_ACCOUNT_CODE = "4.1.1"
SHIPPING_ACCOUNT_CODE = "4.1.2"


def order_payload(order_id: str, **overrides: object) -> dict:
    """A valid order.completed payload (Toman): 2×10000 + 1×5000,
    shipping 3000, discount 1000, tax 500, total 27500."""
    payload: dict[str, object] = {
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
        "total_amount": 27500,
        "status": "completed",
    }
    payload.update(overrides)
    return payload


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


@pytest.fixture(scope="module")
def company(adapter: TrytonAccountingAdapter) -> CompanyRef:
    for view in adapter.list_companies():
        if view.name == TEST_COMPANY_NAME:
            return view.ref
    pytest.skip(
        f"Test company {TEST_COMPANY_NAME!r} not found in Tryton DB {TRYTON_DATABASE!r}. "
        "Provision it with setup_adapter_test_company.py (see docs/adapter.md)."
    )


@pytest.fixture(scope="module")
def accounts(adapter: TrytonAccountingAdapter, company: CompanyRef):
    chart = {view.code: view for view in adapter.get_chart_of_accounts(company)}
    missing = [code for code in (REVENUE_ACCOUNT_CODE, SHIPPING_ACCOUNT_CODE) if code not in chart]
    if missing:
        pytest.skip(f"Chart accounts missing from test company: {missing}.")
    return {code: chart[code] for code in (REVENUE_ACCOUNT_CODE, SHIPPING_ACCOUNT_CODE)}


@pytest.fixture(scope="module")
def irr_currency_id(tryton_client: TrytonRestClient) -> int:
    records = tryton_client.search(
        "currency.currency", domain=[("code", "=", "IRR")], fields=["code"]
    )
    assert records, "IRR currency not found in the test DB"
    return int(records[0]["id"])


@pytest.fixture(scope="module")
def customer(tryton_client: TrytonRestClient):
    """A customer party with one address, created via REST."""
    party = tryton_client.create("party.party", {"name": CUSTOMER_NAME}, fields=["id"])
    party_id = int(party["id"])
    address = tryton_client.create(
        "party.address", {"party": party_id, "city": "Tehran"}, fields=["id"]
    )
    address_id = int(address["id"])
    yield PartyRef(party_id), PartyAddressRef(address_id)
    for model, rid in (("party.address", address_id), ("party.party", party_id)):
        try:
            tryton_client.delete(model, rid)
        except Exception:
            pass  # referenced by posted invoice move lines


@pytest.fixture(scope="module")
def payment_journal(
    tryton_client: TrytonRestClient, company: CompanyRef, irr_currency_id: int
) -> Generator[PaymentJournalRef, None, None]:
    record = tryton_client.create(
        "account.payment.journal",
        {
            "name": PAYMENT_JOURNAL_NAME,
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
        tryton_client.delete(
            "account.payment.journal",
            journal_id,
            context={"company": company.engine_id},
        )
    except Exception:
        pass  # referenced by test payments


# ---------------------------------------------------------------------------
# Fixtures: per-test pipeline world
# ---------------------------------------------------------------------------


@dataclass
class World:
    installation_id: str
    tenant_id: str
    session: Session
    pipeline: EventPipelineService
    inbox: SqlAlchemyEventInboxRepository
    external_ids: SqlAlchemyExternalIdMapRepository
    tryton_ids: list[tuple[str, int]]


@pytest.fixture()
def world(
    hesabyar_engine: Engine,
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    customer,
    payment_journal: PaymentJournalRef,
    tryton_client: TrytonRestClient,
) -> Generator[World, None, None]:
    """Seed a tenant + registry mappings; build the real pipeline."""
    installation_id = str(uuid.uuid4())
    tenant_id = ""
    session = sessionmaker(bind=hesabyar_engine)()
    tryton_ids: list[tuple[str, int]] = []
    try:
        tenant = TenantModel(
            id=str(uuid.uuid4()),
            slug=f"contract-511-{uuid.uuid4().hex[:8]}",
            name="511 Contract Tenant",
        )
        session.add(tenant)
        session.flush()
        tenant_id = tenant.id
        registry = SqlAlchemyIntegrationsRepository(session)
        party_ref, _address_ref = customer

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
            MappingKind.STORE_COMPANY, "", f"{SOURCE_SYSTEM}:{SOURCE_STORE_ID}",
            "company", str(company.engine_id),
        )
        _mapping(
            MappingKind.EXTERNAL_PARTY, "", CUSTOMER_ID,
            "party", str(party_ref.engine_id),
        )
        _mapping(
            MappingKind.REVENUE_CATEGORY, "", "electronics",
            "account", REVENUE_ACCOUNT_CODE,
        )
        _mapping(
            MappingKind.REVENUE_CATEGORY, "", "default",
            "account", REVENUE_ACCOUNT_CODE,
        )
        _mapping(
            MappingKind.SHIPPING_CHARGE, "", "default",
            "account", SHIPPING_ACCOUNT_CODE,
        )
        _mapping(
            MappingKind.GATEWAY_JOURNAL, "zarinpal", "zarinpal",
            "journal", str(payment_journal.engine_id),
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
            # The REAL registry gate (US-511 wiring): with no settings /
            # policy rows it denies auto-post, so these tests stay drafts
            # unless a test opts in explicitly (see the auto-post test).
            policy_gate=RegistryPolicyGate(
                PolicyGateService(registry, TrytonFiscalPeriodChecker(adapter))
            ),
            review_queue=InboxReviewQueueAdapter(),
            engine=adapter,
        )
        # process() flushes; the caller commits (see _process).
        yield World(
            installation_id=installation_id,
            tenant_id=tenant_id,
            session=session,
            pipeline=pipeline,
            inbox=inbox,
            external_ids=external_ids,
            tryton_ids=tryton_ids,
        )
    finally:
        session.close()
        cleanup = sessionmaker(bind=hesabyar_engine)()
        try:
            cleanup.execute(
                delete(IntegrationMappingModel).where(
                    IntegrationMappingModel.installation_id == installation_id
                )
            )
            cleanup.execute(
                delete(AutoPostPolicyModel).where(
                    AutoPostPolicyModel.installation_id == installation_id
                )
            )
            cleanup.execute(
                delete(InstallationSettingsModel).where(
                    InstallationSettingsModel.installation_id == installation_id
                )
            )
            if tenant_id:
                cleanup.query(TenantModel).filter(
                    TenantModel.id == tenant_id
                ).delete(synchronize_session=False)
            cleanup.commit()
        finally:
            cleanup.close()
        # Best-effort Tryton cleanup: drafts delete, posted records stay.
        for model, record_id in tryton_ids:
            try:
                tryton_client.delete(
                    model, record_id, context={"company": company.engine_id}
                )
            except Exception:
                pass


def _store_event(
    world: World,
    event_type: CanonicalEventType,
    payload: dict,
    *,
    source_event_id: str,
    entity_type: str,
    entity_id: str,
) -> str:
    # The occurred_at is dynamic (now), NOT raw_envelope's fixed
    # 2026-11-17 default: the draft builder dates the invoice from
    # the event's occurred_at, and Tryton numbers invoices
    # chronologically — a fixed date eventually falls behind the
    # sequence when the shared DB holds later-numbered invoices
    # (local DB: invoice 0029 dated after 11/17). "Today" always
    # stays >= every past test residue date. Same class as 1eac7b2.
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


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Order → invoice draft
# ---------------------------------------------------------------------------


def test_order_creates_invoice_draft_in_tryton(
    world: World, adapter: TrytonAccountingAdapter, company: CompanyRef
) -> None:
    order_id = _unique("order-511")
    event_id = _store_event(
        world,
        CanonicalEventType.SALES_ORDER_COMPLETED,
        order_payload(order_id),
        source_event_id=_unique("evt-order"),
        entity_type="order",
        entity_id=order_id,
    )
    result = _process(world, event_id)
    assert result.outcome == PipelineOutcome.PROCESSED_DRAFT, result.error
    assert result.draft_ref is not None
    invoice_id = int(result.draft_ref)
    world.tryton_ids.append(("account.invoice", invoice_id))

    view = adapter.get_invoice(InvoiceRef(invoice_id))
    assert view is not None
    assert view.state == "draft"
    assert view.invoice_type == "out"
    assert view.company == company
    assert view.total_amount.amount == 280000  # products + shipping (Toman ×10)
    assert len(view.lines) == 3
    assert view.reference == f"{SOURCE_SYSTEM}:{order_id}"
    assert order_id in view.description
    # The §10.4 mapping ties the order to its invoice.
    found = world.external_ids.get(SOURCE_SYSTEM, "order", order_id)
    assert found is not None
    assert found.target_entity_type == "account.invoice"
    assert found.target_external_id == str(invoice_id)


def test_cancel_cancels_draft_invoice_in_tryton(
    world: World, adapter: TrytonAccountingAdapter
) -> None:
    order_id = _unique("order-511")
    order_event = _store_event(
        world,
        CanonicalEventType.SALES_ORDER_COMPLETED,
        order_payload(order_id),
        source_event_id=_unique("evt-order"),
        entity_type="order",
        entity_id=order_id,
    )
    order_result = _process(world, order_event)
    assert order_result.outcome == PipelineOutcome.PROCESSED_DRAFT, order_result.error
    invoice_id = int(order_result.draft_ref)  # type: ignore[arg-type]
    world.tryton_ids.append(("account.invoice", invoice_id))

    cancel_event = _store_event(
        world,
        CanonicalEventType.SALES_ORDER_CANCELLED,
        {"order_id": order_id, "reason": "customer request"},
        source_event_id=_unique("evt-cancel"),
        entity_type="order_cancellation",
        entity_id=order_id,
    )
    result = _process(world, cancel_event)
    assert result.outcome == PipelineOutcome.PROCESSED_DRAFT, result.error
    assert result.draft_ref == str(invoice_id)
    view = adapter.get_invoice(InvoiceRef(invoice_id))
    assert view is not None
    assert view.state == "cancelled"


def test_cancel_of_posted_invoice_creates_credit_note_in_tryton(
    world: World, adapter: TrytonAccountingAdapter
) -> None:
    order_id = _unique("order-511")
    order_event = _store_event(
        world,
        CanonicalEventType.SALES_ORDER_COMPLETED,
        order_payload(order_id),
        source_event_id=_unique("evt-order"),
        entity_type="order",
        entity_id=order_id,
    )
    order_result = _process(world, order_event)
    assert order_result.outcome == PipelineOutcome.PROCESSED_DRAFT, order_result.error
    invoice_id = int(order_result.draft_ref)  # type: ignore[arg-type]
    adapter.validate_invoice(InvoiceRef(invoice_id))
    adapter.post_invoice(InvoiceRef(invoice_id))
    assert adapter.get_invoice(InvoiceRef(invoice_id)).state == "posted"  # type: ignore[union-attr]

    cancel_event = _store_event(
        world,
        CanonicalEventType.SALES_ORDER_CANCELLED,
        {"order_id": order_id, "reason": "customer request"},
        source_event_id=_unique("evt-cancel"),
        entity_type="order_cancellation",
        entity_id=order_id,
    )
    result = _process(world, cancel_event)
    assert result.outcome == PipelineOutcome.PROCESSED_DRAFT, result.error
    assert result.draft_ref is not None
    assert result.draft_ref != str(invoice_id)
    credit_id = int(result.draft_ref)
    world.tryton_ids.append(("account.invoice", credit_id))
    credit = adapter.get_invoice(InvoiceRef(credit_id))
    assert credit is not None
    assert credit.state == "draft"
    assert credit.total_amount.amount == -280000
    assert adapter.get_invoice(InvoiceRef(invoice_id)).state == "posted"  # type: ignore[union-attr]


def test_partial_refund_creates_partial_credit_note_in_tryton(
    world: World, adapter: TrytonAccountingAdapter
) -> None:
    # No shipping line: a partial credit needs a single revenue account.
    order_id = _unique("order-511")
    order_event = _store_event(
        world,
        CanonicalEventType.SALES_ORDER_COMPLETED,
        order_payload(order_id, shipping_amount=0, total_amount=24500),
        source_event_id=_unique("evt-order"),
        entity_type="order",
        entity_id=order_id,
    )
    order_result = _process(world, order_event)
    assert order_result.outcome == PipelineOutcome.PROCESSED_DRAFT, order_result.error
    invoice_id = int(order_result.draft_ref)  # type: ignore[arg-type]
    adapter.validate_invoice(InvoiceRef(invoice_id))
    adapter.post_invoice(InvoiceRef(invoice_id))

    refund_id = _unique("refund-511")
    refund_event = _store_event(
        world,
        CanonicalEventType.SALES_REFUND_CREATED,
        {
            "refund_id": refund_id,
            "order_id": order_id,
            "refund_amount": 5000,
            "payment_channel": "zarinpal",
            "gross_amount": 5000,
        },
        source_event_id=_unique("evt-refund"),
        entity_type="refund",
        entity_id=refund_id,
    )
    result = _process(world, refund_event)
    assert result.outcome == PipelineOutcome.PROCESSED_DRAFT, result.error
    assert result.draft_ref is not None
    credit_id = int(result.draft_ref)
    world.tryton_ids.append(("account.invoice", credit_id))
    credit = adapter.get_invoice(InvoiceRef(credit_id))
    assert credit is not None
    assert credit.state == "draft"
    assert credit.total_amount.amount == -50000
    assert len(credit.lines) == 1


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------


def test_payment_captured_creates_payment_draft_in_tryton(
    world: World, adapter: TrytonAccountingAdapter
) -> None:
    gateway_id = _unique("pay-511")
    event_id = _store_event(
        world,
        CanonicalEventType.PAYMENT_CAPTURED,
        {
            "payment_channel": "zarinpal",
            "gateway_payment_id": gateway_id,
            "gross_amount": 27500,
            "fee_amount": 500,
            "net_amount": 27000,
            "customer": {"id": CUSTOMER_ID},
            "verified": True,
        },
        source_event_id=_unique("evt-pay"),
        entity_type="payment",
        entity_id=gateway_id,
    )
    result = _process(world, event_id)
    assert result.outcome == PipelineOutcome.PROCESSED_DRAFT, result.error
    assert result.draft_ref is not None
    payment_id = int(result.draft_ref)
    world.tryton_ids.append(("account.payment", payment_id))
    view = adapter.get_payment(PaymentRef(payment_id))
    assert view is not None
    assert view.kind == "receivable"
    assert view.state == "draft"
    assert view.amount.amount == 275000
    assert view.reference == gateway_id


# ---------------------------------------------------------------------------
# The registry policy gate (auto-post is an explicit opt-in)
# ---------------------------------------------------------------------------


def test_auto_post_opt_in_posts_the_invoice_in_tryton(
    world: World, adapter: TrytonAccountingAdapter
) -> None:
    """With the installation opted in + an allowed policy row with a cap
    + an open fiscal period, the registry gate allows auto-post and the
    pipeline posts the invoice instead of leaving a draft. The posted
    invoice is residue (like the other posted records these tests
    leave behind); the settings/policy rows are deleted in teardown.
    """
    registry = SqlAlchemyIntegrationsRepository(world.session)
    registry.save_installation_settings(
        UpsertInstallationSettings(
            installation_id=world.installation_id, auto_post_enabled=True
        )
    )
    registry.save_policy_config(
        UpsertPolicyGateConfig(
            installation_id=world.installation_id,
            event_type=CanonicalEventType.SALES_ORDER_COMPLETED.value,
            allowed=True,
            max_amount_irr=1_000_000,
        )
    )
    world.session.commit()

    order_id = _unique("order-511")
    event_id = _store_event(
        world,
        CanonicalEventType.SALES_ORDER_COMPLETED,
        order_payload(order_id),
        source_event_id=_unique("evt-order"),
        entity_type="order",
        entity_id=order_id,
    )
    result = _process(world, event_id)
    assert result.outcome == PipelineOutcome.PROCESSED_POSTED, result.error
    assert result.draft_ref is not None
    invoice_id = int(result.draft_ref)
    view = adapter.get_invoice(InvoiceRef(invoice_id))
    assert view is not None
    assert view.state == "posted"
    assert view.total_amount.amount == 280000
