"""Contract tests for the US-552 review-queue completion (live Tryton + HesabYar DB).

The REAL pipeline (StoreMappingResolver + StoreDraftBuilder +
EventPipelineService, real SQLAlchemy repositories, real Tryton
adapter) with the REAL review queue wired in: pipeline exceptions
file real ``ReviewItem`` rows through
:class:`PersistentReviewQueueAdapter
<hesabyar.infrastructure.events.review_queue_adapter.PersistentReviewQueueAdapter>`,
and the queue's ``complete_mapping_retry`` action reprocesses the
event through the REAL DLQ replay service (same idempotency key) —
so a mapping gap the operator completes ends as an invoice draft in
Tryton, with no duplicate draft on re-run.

Uses ONLY the dedicated test company "HesabYar Adapter Test"
(provisioned by
``~/workspace/projects/hesabyar-reference/setup_adapter_test_company.py``;
see docs/adapter.md). When the server is not reachable (or the test
company has not been provisioned yet), every test SKIPS cleanly with
a clear message — the suite stays green without a server. With the
server up, the tests must PASS.

Each test uses a FRESH installation id + tenant: teardown deletes
the installation's registry rows (mappings + suggestions) and the
tenant (which cascades to inbox events + review items + audit).
Tryton drafts the test created are best-effort deleted.

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

from hesabyar.application.commands.events import DlqReplayService, EventPipelineService
from hesabyar.application.commands.integrations import MappingService, PolicyGateService
from hesabyar.application.commands.policy_gate import RegistryPolicyGate
from hesabyar.application.commands.review_queue import MappingLearningInput, ReviewQueueCommandService
from hesabyar.application.commands.store_events import StoreDraftBuilder, StoreMappingResolver
from hesabyar.application.queries.review_queue import ReviewQueueQueryService
from hesabyar.domain.accounting.refs import (
    CompanyRef,
    InvoiceRef,
    PartyAddressRef,
    PartyRef,
    PaymentJournalRef,
)
from hesabyar.domain.events.envelope import CanonicalEvent, CanonicalEventType
from hesabyar.domain.events.models import PipelineOutcome
from hesabyar.domain.integrations.models import MappingKind
from hesabyar.domain.review_queue.models import ReviewItemStatus, ReviewItemType
from hesabyar.domain.review_queue.ports import ReviewItemFilters
from hesabyar.infrastructure.accounting.tryton.adapter import TrytonAccountingAdapter
from hesabyar.infrastructure.accounting.tryton.client import TrytonRestClient
from hesabyar.infrastructure.accounting.tryton.fiscal_period_checker import (
    TrytonFiscalPeriodChecker,
)
from hesabyar.infrastructure.events.in_memory_queue import InMemoryJobQueue
from hesabyar.infrastructure.events.review_queue_adapter import PersistentReviewQueueAdapter
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
    Base,
    IntegrationMappingModel,
    MappingSuggestionModel,
    TenantModel,
)
from hesabyar.infrastructure.persistence.postgres.review_queue_repository import (
    SqlAlchemyReviewQueueRepository,
)
from tests.event_fixtures import raw_envelope

HESABYAR_DB_URL = os.environ.get("HESABYAR_DATABASE_URL", "")
TRYTON_BASE_URL = os.environ.get("TRYTON_BASE_URL", "http://localhost:8000")
TRYTON_DATABASE = os.environ.get("TRYTON_DATABASE", "hesabyar_tryton_test")
TRYTON_API_KEY = os.environ.get("TRYTON_API_KEY", "hesabyar-dev-rest-key")
TEST_COMPANY_NAME = os.environ.get("TRYTON_TEST_COMPANY", "HesabYar Adapter Test")

SOURCE_SYSTEM = "hayat"
SOURCE_STORE_ID = "store-1"
CUSTOMER_ID = "cust-552"
CUSTOMER_NAME = "HesabYar US-552 Contract Customer"
PAYMENT_JOURNAL_NAME = "HesabYar US-552 Contract Payments"

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
# Fixtures: live servers (mirrors test_store_pipeline.py)
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
# Fixtures: per-test world (pipeline + review queue, NO mappings seeded)
# ---------------------------------------------------------------------------


@dataclass
class World:
    installation_id: str
    tenant_id: str
    session: Session
    pipeline: EventPipelineService
    inbox: SqlAlchemyEventInboxRepository
    external_ids: SqlAlchemyExternalIdMapRepository
    review_commands: ReviewQueueCommandService
    review_query: ReviewQueueQueryService
    mapping_service: MappingService
    company: CompanyRef
    party_ref: PartyRef
    payment_journal: PaymentJournalRef
    tryton_ids: list[tuple[str, int]]


@pytest.fixture()
def world(
    hesabyar_engine: Engine,
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    accounts,
    customer,
    payment_journal: PaymentJournalRef,
    tryton_client: TrytonRestClient,
) -> Generator[World, None, None]:
    """Seed a tenant; build the real pipeline + the real review queue.

    Deliberately NO registry mappings: the tests seed the mappings
    the scenario needs (or learn them through the queue) after the
    event has failed closed into the queue.
    """
    installation_id = str(uuid.uuid4())
    tenant_id = ""
    session = sessionmaker(bind=hesabyar_engine)()
    tryton_ids: list[tuple[str, int]] = []
    try:
        tenant = TenantModel(
            id=str(uuid.uuid4()),
            slug=f"contract-552-{uuid.uuid4().hex[:8]}",
            name="552 Contract Tenant",
        )
        session.add(tenant)
        session.flush()
        tenant_id = tenant.id
        registry = SqlAlchemyIntegrationsRepository(session)
        review_repository = SqlAlchemyReviewQueueRepository(session)
        inbox = SqlAlchemyEventInboxRepository(session)
        outbox = SqlAlchemyEventOutboxRepository(session)
        external_ids = SqlAlchemyExternalIdMapRepository(session)
        audit_log = SqlAlchemyDecisionAuditLogRepository(session)
        # The review queue's effect seams are the REAL services over
        # the real repositories; the job queue is in-memory (no
        # worker runs in this test — the test calls process() itself).
        replay = DlqReplayService(inbox, audit_log, InMemoryJobQueue())
        mapping_service = MappingService(registry)
        review_commands = ReviewQueueCommandService(
            review_repository, review_repository, replay=replay, mapping=mapping_service
        )
        review_query = ReviewQueueQueryService(review_repository, review_repository)
        pipeline = EventPipelineService(
            inbox=inbox,
            outbox=outbox,
            external_ids=external_ids,
            audit_log=audit_log,
            store_settings=SqlAlchemyStoreSettingsRepository(session),
            mapping_resolver=StoreMappingResolver(registry, external_ids, adapter),
            draft_builder=StoreDraftBuilder(adapter, external_ids),
            policy_gate=RegistryPolicyGate(
                PolicyGateService(registry, TrytonFiscalPeriodChecker(adapter))
            ),
            review_queue=PersistentReviewQueueAdapter(review_commands),
            engine=adapter,
        )
        party_ref, _address_ref = customer
        yield World(
            installation_id=installation_id,
            tenant_id=tenant_id,
            session=session,
            pipeline=pipeline,
            inbox=inbox,
            external_ids=external_ids,
            review_commands=review_commands,
            review_query=review_query,
            mapping_service=mapping_service,
            company=company,
            party_ref=party_ref,
            payment_journal=payment_journal,
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
                delete(MappingSuggestionModel).where(
                    MappingSuggestionModel.installation_id == installation_id
                )
            )
            if tenant_id:
                cleanup.query(TenantModel).filter(
                    TenantModel.id == tenant_id
                ).delete(synchronize_session=False)
            cleanup.commit()
        finally:
            cleanup.close()
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
    # chronologically (see test_store_pipeline.py).
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


def _seed_mapping(
    world: World,
    kind: MappingKind,
    channel: str,
    source: str,
    target_type: str,
    target_ref: str,
) -> None:
    """Create one mapping through the real mapping service (as the operator would)."""
    world.mapping_service.create_mapping(
        world.installation_id,
        mapping_kind=kind,
        channel_key=channel,
        source_key=source,
        target_type=target_type,
        target_ref=target_ref,
    )
    world.session.commit()


def _seed_all_mappings(world: World, *, skip_revenue_category: str | None = None) -> None:
    """Seed every mapping the order event needs (optionally one short)."""
    _seed_mapping(
        world,
        MappingKind.STORE_COMPANY,
        "",
        f"{SOURCE_SYSTEM}:{SOURCE_STORE_ID}",
        "company",
        str(world.company.engine_id),
    )
    _seed_mapping(
        world,
        MappingKind.EXTERNAL_PARTY,
        "",
        CUSTOMER_ID,
        "party",
        str(world.party_ref.engine_id),
    )
    for category in ("electronics", "default"):
        if category == skip_revenue_category:
            continue
        _seed_mapping(
            world,
            MappingKind.REVENUE_CATEGORY,
            "",
            category,
            "account",
            REVENUE_ACCOUNT_CODE,
        )
    _seed_mapping(
        world,
        MappingKind.SHIPPING_CHARGE,
        "",
        "default",
        "account",
        SHIPPING_ACCOUNT_CODE,
    )
    _seed_mapping(
        world,
        MappingKind.GATEWAY_JOURNAL,
        "zarinpal",
        "zarinpal",
        "journal",
        str(world.payment_journal.engine_id),
    )


def _mapping_items(world: World):
    """The tenant's mapping-type queue items (via the real query service)."""
    return world.review_query.list_items(
        world.tenant_id, ReviewItemFilters(item_type=ReviewItemType.MAPPING)
    )


# ---------------------------------------------------------------------------
# Mapping gap → queue → complete elsewhere → reprocess → invoice draft
# ---------------------------------------------------------------------------


def test_mapping_gap_files_real_item_and_reprocess_creates_draft(
    world: World, adapter: TrytonAccountingAdapter, company: CompanyRef
) -> None:
    """The full US-552 loop: no mappings → NEEDS_REVIEW + a real queue
    item → the operator completes the mappings in the mapping center →
    complete_mapping_retry reprocesses the event → one invoice draft,
    and a re-run creates no second draft."""
    order_id = _unique("order-552")
    event_id = _store_event(
        world,
        CanonicalEventType.SALES_ORDER_COMPLETED,
        order_payload(order_id),
        source_event_id=_unique("evt-order"),
        entity_type="order",
        entity_id=order_id,
    )
    result = _process(world, event_id)
    assert result.outcome == PipelineOutcome.NEEDS_REVIEW, result.error

    # The pipeline filed a REAL review item (not just an inbox row).
    items = _mapping_items(world)
    assert len(items) == 1
    item = items[0].item
    assert item.status is ReviewItemStatus.OPEN
    assert item.source_event_ref == event_id
    assert item.subtype == CanonicalEventType.SALES_ORDER_COMPLETED.value
    assert items[0].suggested_action_code == "complete_mapping_retry"

    # The operator completes the mappings in the mapping center.
    _seed_all_mappings(world)

    # complete_mapping_retry with no mapping details: only reprocess.
    moved = world.review_commands.perform_action(
        item_id=item.id,
        tenant_id=world.tenant_id,
        action_code="complete_mapping_retry",
        actor="operator",
        installation_id=world.installation_id,
    )
    world.session.commit()
    assert moved.status is ReviewItemStatus.IN_REVIEW
    view = world.review_query.get_item(item.id, world.tenant_id)
    assert [entry.action for entry in view.item.audit] == ["filed", "complete_mapping_retry"]
    assert f"reprocessed source event {event_id}" in (view.item.audit[-1].note or "")

    # The reprocessed event now builds its invoice draft.
    result = _process(world, event_id)
    assert result.outcome == PipelineOutcome.PROCESSED_DRAFT, result.error
    assert result.draft_ref is not None
    invoice_id = int(result.draft_ref)
    world.tryton_ids.append(("account.invoice", invoice_id))
    view = adapter.get_invoice(InvoiceRef(invoice_id))
    assert view is not None
    assert view.state == "draft"
    assert view.company == company
    assert view.total_amount.amount == 280000  # products + shipping (Toman ×10)
    assert view.reference == f"{SOURCE_SYSTEM}:{order_id}"

    # Re-running the event creates NO second draft (same idempotency key).
    again = _process(world, event_id)
    assert again.outcome == PipelineOutcome.PROCESSED_DRAFT, again.error
    assert again.draft_ref == str(invoice_id)
    found = world.external_ids.get(SOURCE_SYSTEM, "order", order_id)
    assert found is not None
    assert found.target_external_id == str(invoice_id)


# ---------------------------------------------------------------------------
# Mapping gap → learn the mapping in the queue → accept → reprocess → draft
# ---------------------------------------------------------------------------


def test_mapping_learning_loop_creates_draft(
    world: World, adapter: TrytonAccountingAdapter, company: CompanyRef
) -> None:
    """The Suggest → Auto loop end to end: one mapping short → the
    reviewer completes it in the queue (a suggestion is learned) →
    accepting the suggestion creates the mapping → the reprocessed
    event builds its invoice draft."""
    # Everything except the "electronics" revenue category.
    _seed_all_mappings(world, skip_revenue_category="electronics")

    order_id = _unique("order-552")
    event_id = _store_event(
        world,
        CanonicalEventType.SALES_ORDER_COMPLETED,
        order_payload(order_id),
        source_event_id=_unique("evt-order"),
        entity_type="order",
        entity_id=order_id,
    )
    result = _process(world, event_id)
    assert result.outcome == PipelineOutcome.NEEDS_REVIEW, result.error
    items = _mapping_items(world)
    assert len(items) == 1
    item = items[0].item

    # The reviewer completes the missing mapping in the queue: it is
    # LEARNED as a mapping-center suggestion, and the event is
    # reprocessed (best-effort — it will still fail until the
    # suggestion is accepted, but the reprocess itself is recorded).
    moved = world.review_commands.perform_action(
        item_id=item.id,
        tenant_id=world.tenant_id,
        action_code="complete_mapping_retry",
        actor="operator",
        installation_id=world.installation_id,
        mapping=MappingLearningInput(
            mapping_kind="revenue_category",
            source_key="electronics",
            target_type="account",
            target_ref=REVENUE_ACCOUNT_CODE,
        ),
    )
    world.session.commit()
    assert moved.status is ReviewItemStatus.IN_REVIEW
    view = world.review_query.get_item(item.id, world.tenant_id)
    audit_entry = view.item.audit[-1]
    assert audit_entry.action == "complete_mapping_retry"
    assert audit_entry.mapping_version is not None
    assert audit_entry.mapping_version.startswith("suggestion:")
    suggestion_id = audit_entry.mapping_version.removeprefix("suggestion:")
    registry = SqlAlchemyIntegrationsRepository(world.session)
    suggestion = registry.get_suggestion(suggestion_id)
    assert suggestion is not None
    assert suggestion.source_key == "electronics"
    assert suggestion.suggested_target_ref == REVENUE_ACCOUNT_CODE
    assert suggestion.basis == f"review_queue:{item.id}"

    # Accepting the suggestion creates the SUGGEST-mode mapping.
    mapping = world.mapping_service.accept_suggestion(world.installation_id, suggestion_id)
    world.session.commit()
    assert mapping.source_key == "electronics"
    assert mapping.target_ref == REVENUE_ACCOUNT_CODE

    # The event (back to received by the reprocess) now builds its draft.
    result = _process(world, event_id)
    assert result.outcome == PipelineOutcome.PROCESSED_DRAFT, result.error
    assert result.draft_ref is not None
    invoice_id = int(result.draft_ref)
    world.tryton_ids.append(("account.invoice", invoice_id))
    invoice = adapter.get_invoice(InvoiceRef(invoice_id))
    assert invoice is not None
    assert invoice.state == "draft"
    assert invoice.company == company
    assert invoice.total_amount.amount == 280000
