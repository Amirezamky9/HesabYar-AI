"""Contract tests for US-533 site intake (live Tryton lane 2 + Supabase lane-2 HesabYar DB).

The REAL pipeline (EventPipelineService + SiteChequeIntakeHandler +
ChequeCommandService, real SQLAlchemy repositories, real Tryton
adapter) against trytond on :8011 and the lane-2 HesabYar DB, using
ONLY the dedicated test company "HesabYar Adapter Test". Each test
uses a FRESH installation id + tenant and a RANDOM sayad_id (the
live-sayad index is global on the shared DB, so fixed ids would
collide across runs). Teardown deletes the test's registry rows,
its Tryton ``account.check`` records and its tenant/mappings.

The mapped company's Tryton party ``code`` is the fixture payee
national id (``00123456789``) — test-infra state, set once per
Tryton DB (lane 2: direct SQL on ``party_party``, was ``1``; the
REST user may not write ``party.code``). The fixture verifies it
and skips with the fix if it differs.

Configuration (all optional, dev defaults match docs/dev-setup.md):
  HESABYAR_DATABASE_URL (the lane-2 Supabase HesabYar DB)
  TRYTON_BASE_URL       (default http://localhost:8000 — use :8011 for lane 2)
  TRYTON_DATABASE       (default hesabyar_tryton_test)
  TRYTON_API_KEY        (default hesabyar-dev-rest-key)
  TRYTON_TEST_COMPANY   (default "HesabYar Adapter Test")

When a server is not reachable (or the test company has not been
provisioned yet), every test SKIPS cleanly with a clear message.
"""

from __future__ import annotations

import datetime
import os
import random
import uuid
from collections.abc import Generator
from dataclasses import dataclass

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from hesabyar.application.commands.cheque_intake import SiteChequeIntakeHandler
from hesabyar.application.commands.cheques import ChequeCommandService
from hesabyar.application.commands.events import EventPipelineService
from hesabyar.application.commands.integrations import PolicyGateService
from hesabyar.application.commands.policy_gate import RegistryPolicyGate
from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.application.commands.store_events import StoreDraftBuilder, StoreMappingResolver
from hesabyar.domain.accounting.refs import ChequeEngineRef, CompanyRef, PartyRef
from hesabyar.domain.cheques.models import ChequeState, StorageLocation
from hesabyar.domain.events.envelope import CanonicalEvent, CanonicalEventType
from hesabyar.domain.events.models import PipelineOutcome
from hesabyar.domain.integrations.commands import CreateIntegrationMapping
from hesabyar.domain.integrations.models import MappingKind
from hesabyar.infrastructure.accounting.tryton.adapter import TrytonAccountingAdapter
from hesabyar.infrastructure.accounting.tryton.client import TrytonRestClient
from hesabyar.infrastructure.accounting.tryton.fiscal_period_checker import (
    TrytonFiscalPeriodChecker,
)
from hesabyar.infrastructure.events.defaults import InboxReviewQueueAdapter
from hesabyar.infrastructure.persistence.postgres.cheques_repository import (
    SqlAlchemyChequeRepository,
)
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
    ChequeCustodyLogModel,
    ChequeInquiryReceiptModel,
    ChequeModel,
    InstallationSettingsModel,
    IntegrationMappingModel,
    ReviewItemModel,
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
CUSTOMER_REF = "cust-533"
CUSTOMER_NAME = "HesabYar US-533 Contract Customer"
PAYEE_NATIONAL_ID = "00123456789"
ACTOR = "contract-us533"


def _unique_sayad_id() -> str:
    """A random 16-digit sayad_id (the live-sayad index is global)."""
    return str(random.randint(10**15, 10**16 - 1))


def _registration_payload(sayad_id: str, **overrides: object) -> dict:
    payload: dict[str, object] = {
        "sayad_id": sayad_id,
        "amount_toman": 1750000,
        "amount_currency": "IRT",
        "issue_date": "1405-08-26",
        "due_date": "1405-10-15",
        "issuer_name": "مشتری نمونه",
        "issuer_national_id": "0012345678",
        "issuer_bank": "056 سامان",
        "payee_name": "کافه ایوان",
        "payee_national_id": PAYEE_NATIONAL_ID,
        # Deterministic per sayad_id: the convergence tests re-issue
        # the SAME payload, and order_id is part of the claim.
        "order_id": f"ord-{sayad_id[-8:]}",
        "customer_ref": CUSTOMER_REF,
        "sayad_registered": True,
        "purpose": "payment",
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Fixtures: live servers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def hesabyar_engine() -> Generator[Engine, None, None]:
    if not HESABYAR_DB_URL:
        pytest.skip("HESABYAR_DATABASE_URL is not set (source .env.supabase-lane2).")
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
            "(start it with start-trytond-supabase-lane2.sh to run the contract tests)"
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
def customer(tryton_client: TrytonRestClient) -> Generator[PartyRef, None, None]:
    party = tryton_client.create("party.party", {"name": CUSTOMER_NAME}, fields=["id"])
    party_id = int(party["id"])
    yield PartyRef(party_id)
    try:
        tryton_client.delete("party.party", party_id)
    except Exception:
        pass  # referenced by a cheque party link


@pytest.fixture(scope="module")
def company_party_code(
    tryton_client: TrytonRestClient, company: CompanyRef
) -> Generator[None, None, None]:
    """Verify the test company party code is the fixture payee id.

    The payee check reads the mapped company's national id from its
    Tryton party ``code`` (US-533). The REST user may NOT write
    ``party.code`` on the company party (Tryton refuses it), so the
    code is test-infra state, set once per Tryton DB — on lane 2 it
    was set to ``00123456789`` (was ``1``) via direct SQL on
    ``party_party``; the local DB needs the same. If it is anything
    else, these tests skip with the fix instead of failing.
    """
    records = tryton_client.search(
        "company.company",
        domain=[("id", "=", company.engine_id)],
        fields=["party"],
    )
    assert records, f"Company {company.engine_id} not found in Tryton."
    party_id = int(records[0]["party"])
    party = tryton_client.read("party.party", party_id, fields=["code"])
    code = (party or {}).get("code")
    if code != PAYEE_NATIONAL_ID:
        pytest.skip(
            f"Test company party code is {code!r}, not the fixture payee id "
            f"{PAYEE_NATIONAL_ID!r} — set it (party_party.code for the "
            "company party) and re-run; the payee check needs it."
        )
    yield


# ---------------------------------------------------------------------------
# Fixtures: per-test pipeline world
# ---------------------------------------------------------------------------


@dataclass
class World:
    installation_id: str
    tenant_id: str
    session: Session
    pipeline: EventPipelineService
    commands: ChequeCommandService
    inbox: SqlAlchemyEventInboxRepository
    sayad_ids: list[str]
    tryton_ids: list[int]


@pytest.fixture()
def world(
    hesabyar_engine: Engine,
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    customer: PartyRef,
    company_party_code: None,
    tryton_client: TrytonRestClient,
) -> Generator[World, None, None]:
    """Seed a tenant + registry mappings; build the real pipeline + intake."""
    installation_id = str(uuid.uuid4())
    tenant_id = ""
    session = sessionmaker(bind=hesabyar_engine)()
    sayad_ids: list[str] = []
    tryton_ids: list[int] = []
    try:
        tenant = TenantModel(
            id=str(uuid.uuid4()),
            slug=f"contract-533-{uuid.uuid4().hex[:8]}",
            name="533 Contract Tenant",
        )
        session.add(tenant)
        session.flush()
        tenant_id = tenant.id
        registry = SqlAlchemyIntegrationsRepository(session)
        registry.save_mapping(
            CreateIntegrationMapping(
                installation_id=installation_id,
                mapping_kind=MappingKind.STORE_COMPANY,
                channel_key="",
                source_key=f"{SOURCE_SYSTEM}:{SOURCE_STORE_ID}",
                target_type="company",
                target_ref=str(company.engine_id),
                version=1,
            )
        )
        registry.save_mapping(
            CreateIntegrationMapping(
                installation_id=installation_id,
                mapping_kind=MappingKind.EXTERNAL_PARTY,
                channel_key="",
                source_key=CUSTOMER_REF,
                target_type="party",
                target_ref=str(customer.engine_id),
                version=1,
            )
        )
        session.commit()

        inbox = SqlAlchemyEventInboxRepository(session)
        outbox = SqlAlchemyEventOutboxRepository(session)
        external_ids = SqlAlchemyExternalIdMapRepository(session)
        review_repo = SqlAlchemyReviewQueueRepository(session)
        commands = ChequeCommandService(
            SqlAlchemyChequeRepository(session),
            adapter,
            gate=PolicyGateService(registry, TrytonFiscalPeriodChecker(adapter)),
            review=ReviewQueueCommandService(review_repo, review_repo),
        )
        pipeline = EventPipelineService(
            inbox=inbox,
            outbox=outbox,
            external_ids=external_ids,
            audit_log=SqlAlchemyDecisionAuditLogRepository(session),
            store_settings=SqlAlchemyStoreSettingsRepository(session),
            mapping_resolver=StoreMappingResolver(registry, external_ids, adapter),
            draft_builder=StoreDraftBuilder(adapter, external_ids),
            policy_gate=RegistryPolicyGate(
                PolicyGateService(registry, TrytonFiscalPeriodChecker(adapter))
            ),
            review_queue=InboxReviewQueueAdapter(),
            engine=adapter,
            cheque_intake=SiteChequeIntakeHandler(commands),
        )
        yield World(
            installation_id=installation_id,
            tenant_id=tenant_id,
            session=session,
            pipeline=pipeline,
            commands=commands,
            inbox=inbox,
            sayad_ids=sayad_ids,
            tryton_ids=tryton_ids,
        )
    finally:
        session.close()
        cleanup = sessionmaker(bind=hesabyar_engine)()
        try:
            if sayad_ids:
                cheque_ids = [
                    row[0]
                    for row in cleanup.execute(
                        select(ChequeModel.id).where(
                            ChequeModel.sayad_id.in_(sayad_ids)
                        )
                    ).all()
                ]
                if cheque_ids:
                    # confirm_receipt writes a custody entry (and
                    # receipts, when present) referencing the cheque.
                    cleanup.execute(
                        delete(ChequeCustodyLogModel).where(
                            ChequeCustodyLogModel.cheque_id.in_(cheque_ids)
                        )
                    )
                    cleanup.execute(
                        delete(ChequeInquiryReceiptModel).where(
                            ChequeInquiryReceiptModel.cheque_id.in_(cheque_ids)
                        )
                    )
                cleanup.execute(
                    delete(ChequeModel).where(ChequeModel.sayad_id.in_(sayad_ids))
                )
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
        for engine_id in tryton_ids:
            try:
                tryton_client.delete(
                    "account.check",
                    engine_id,
                    context={"company": company.engine_id},
                )
            except Exception:
                pass  # voided/held records may refuse deletion; residue is fine


def _store_event(
    world: World,
    event_type: CanonicalEventType,
    payload: dict,
    *,
    source_event_id: str,
    entity_type: str,
    entity_id: str,
) -> str:
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


def _registry_row(world: World, sayad_id: str) -> ChequeModel:
    row = world.session.execute(
        select(ChequeModel).where(ChequeModel.sayad_id == sayad_id)
    ).scalar_one()
    return row


def _review_subtypes(world: World) -> list[str | None]:
    rows = world.session.execute(
        select(ReviewItemModel.subtype).where(
            ReviewItemModel.tenant_id == world.tenant_id
        )
    ).all()
    return [row[0] for row in rows]


def _register_via_pipeline(world: World, sayad_id: str, **overrides: object):
    """Ingest a registration; return the pipeline result.

    The entity id IS the store event id (webhook and poll share
    it), so a second event with a different event id exercises the
    convergence path instead of the pre-check suppression.
    """
    event_ref = f"evt-reg-{uuid.uuid4().hex[:8]}"
    event_id = _store_event(
        world,
        CanonicalEventType.CHEQUE_RECEIVED_REGISTERED,
        _registration_payload(sayad_id, **overrides),
        source_event_id=event_ref,
        entity_type="cheque",
        entity_id=event_ref,
    )
    return _process(world, event_id)


# ---------------------------------------------------------------------------
# Intake: webhook-style registration
# ---------------------------------------------------------------------------


def test_site_registration_creates_registered_cheque_with_no_journal(
    world: World, adapter: TrytonAccountingAdapter, company: CompanyRef
) -> None:
    sayad_id = _unique_sayad_id()
    world.sayad_ids.append(sayad_id)
    event_ref = f"evt-{uuid.uuid4().hex[:8]}"
    event_id = _store_event(
        world,
        CanonicalEventType.CHEQUE_RECEIVED_REGISTERED,
        _registration_payload(sayad_id),
        source_event_id=event_ref,
        entity_type="cheque",
        entity_id=event_ref,
    )
    result = _process(world, event_id)
    assert result.outcome == PipelineOutcome.PROCESSED_DRAFT, result.error
    assert result.draft_ref is not None
    engine_id = int(result.draft_ref)
    world.tryton_ids.append(engine_id)

    row = _registry_row(world, sayad_id)
    assert row.state == ChequeState.REGISTERED.value
    assert row.source == "site"
    assert row.move_ids in (None, {})
    assert row.amount_irr == 17_500_000  # Toman -> Rial x10 at the boundary
    assert row.engine_id == engine_id

    view = adapter.get_engine_cheque(ChequeEngineRef(engine_id=engine_id))
    assert view is not None
    assert view.state == "registered"


def test_idempotent_reregistration_returns_the_same_cheque(world: World) -> None:
    sayad_id = _unique_sayad_id()
    world.sayad_ids.append(sayad_id)
    first = _register_via_pipeline(world, sayad_id)
    assert first.outcome == PipelineOutcome.PROCESSED_DRAFT, first.error
    assert first.draft_ref is not None
    world.tryton_ids.append(int(first.draft_ref))

    second = _register_via_pipeline(world, sayad_id)
    assert second.outcome == PipelineOutcome.PROCESSED_DRAFT, second.error
    assert second.draft_ref == first.draft_ref  # the same engine cheque
    rows = world.session.execute(
        select(ChequeModel).where(ChequeModel.sayad_id == sayad_id)
    ).all()
    assert len(rows) == 1  # exactly one HesabYar cheque


def test_different_payload_conflicts_to_the_review_queue(world: World) -> None:
    sayad_id = _unique_sayad_id()
    world.sayad_ids.append(sayad_id)
    first = _register_via_pipeline(world, sayad_id)
    assert first.outcome == PipelineOutcome.PROCESSED_DRAFT, first.error
    assert first.draft_ref is not None
    world.tryton_ids.append(int(first.draft_ref))

    conflict = _register_via_pipeline(world, sayad_id, amount_toman=9990000)
    assert conflict.outcome == PipelineOutcome.PROCESSED_DRAFT, conflict.error
    assert conflict.draft_ref == first.draft_ref  # the existing cheque stands
    assert "duplicate_sayad_id" in _review_subtypes(world)
    rows = world.session.execute(
        select(ChequeModel).where(ChequeModel.sayad_id == sayad_id)
    ).all()
    assert len(rows) == 1


def test_manual_entry_converges_with_the_site_registration(
    world: World, company: CompanyRef
) -> None:
    sayad_id = _unique_sayad_id()
    world.sayad_ids.append(sayad_id)
    first = _register_via_pipeline(world, sayad_id)
    assert first.outcome == PipelineOutcome.PROCESSED_DRAFT, first.error
    assert first.draft_ref is not None
    world.tryton_ids.append(int(first.draft_ref))

    manual = world.commands.register_manual(
        tenant_id=world.tenant_id,
        installation_id=world.installation_id,
        company_engine_id=company.engine_id,
        payload=_registration_payload(sayad_id),
        actor=ACTOR,
    )
    world.session.commit()
    row = _registry_row(world, sayad_id)
    assert manual.id == row.id  # one record, not two
    assert row.source == "site"  # the site intake stands


def test_payee_mismatch_is_rejected_to_review(world: World) -> None:
    sayad_id = _unique_sayad_id()
    world.sayad_ids.append(sayad_id)
    event_ref = f"evt-{uuid.uuid4().hex[:8]}"
    event_id = _store_event(
        world,
        CanonicalEventType.CHEQUE_RECEIVED_REGISTERED,
        _registration_payload(sayad_id, payee_national_id="98765432109"),
        source_event_id=event_ref,
        entity_type="cheque",
        entity_id=event_ref,
    )
    result = _process(world, event_id)
    assert result.outcome == PipelineOutcome.NEEDS_REVIEW, result
    rows = world.session.execute(
        select(ChequeModel).where(ChequeModel.sayad_id == sayad_id)
    ).all()
    assert rows == []  # nothing was created


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


def test_cancellation_before_held_voids_the_intake(
    world: World, adapter: TrytonAccountingAdapter, company: CompanyRef
) -> None:
    sayad_id = _unique_sayad_id()
    world.sayad_ids.append(sayad_id)
    first = _register_via_pipeline(world, sayad_id)
    assert first.outcome == PipelineOutcome.PROCESSED_DRAFT, first.error
    assert first.draft_ref is not None
    engine_id = int(first.draft_ref)
    world.tryton_ids.append(engine_id)

    event_id = _store_event(
        world,
        CanonicalEventType.CHEQUE_RECEIVED_CANCELLED,
        {"sayad_id": sayad_id, "reason": "issuer withdrew"},
        source_event_id=f"evt-cancel-{uuid.uuid4().hex[:8]}",
        entity_type="cheque_cancellation",
        entity_id=sayad_id,
    )
    result = _process(world, event_id)
    assert result.outcome == PipelineOutcome.PROCESSED_DRAFT, result.error
    row = _registry_row(world, sayad_id)
    assert row.state == ChequeState.VOIDED.value
    assert row.move_ids in (None, {})
    view = adapter.get_engine_cheque(ChequeEngineRef(engine_id=engine_id))
    assert view is not None
    assert view.state == "voided"


def test_cancellation_after_held_files_review_and_keeps_the_cheque(
    world: World, adapter: TrytonAccountingAdapter, company: CompanyRef
) -> None:
    sayad_id = _unique_sayad_id()
    world.sayad_ids.append(sayad_id)
    first = _register_via_pipeline(world, sayad_id)
    assert first.outcome == PipelineOutcome.PROCESSED_DRAFT, first.error
    assert first.draft_ref is not None
    engine_id = int(first.draft_ref)
    world.tryton_ids.append(engine_id)

    row = _registry_row(world, sayad_id)
    held = world.commands.confirm_receipt(
        tenant_id=world.tenant_id,
        installation_id=world.installation_id,
        cheque_id=row.id,
        actor=ACTOR,
        items_matched=True,
        sayad_accepted=True,
        color_code=2,
        storage_location=StorageLocation.VAULT,
    )
    world.session.commit()
    assert held.state is ChequeState.HELD

    event_id = _store_event(
        world,
        CanonicalEventType.CHEQUE_RECEIVED_CANCELLED,
        {"sayad_id": sayad_id},
        source_event_id=f"evt-cancel-{uuid.uuid4().hex[:8]}",
        entity_type="cheque_cancellation",
        entity_id=sayad_id,
    )
    result = _process(world, event_id)
    assert result.outcome == PipelineOutcome.PROCESSED_DRAFT, result.error
    assert "cancellation_after_held" in _review_subtypes(world)
    row = _registry_row(world, sayad_id)
    assert row.state == ChequeState.HELD.value  # never auto-voided
    view = adapter.get_engine_cheque(ChequeEngineRef(engine_id=engine_id))
    assert view is not None
    assert view.state == "held"
