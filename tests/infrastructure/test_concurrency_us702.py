"""Concurrency tests (US-702, spec §19.9 + §17 phase 7).

Runs against the shared Postgres test DB with committed sessions
(the advisory locks / row locks these tests exercise are
transaction-scoped, so the rolled-back fixture session cannot
exercise them); each test uses unique ids and deletes its own rows
afterwards.

Covered here, at the layer that owns the guarantee:

- the event idempotency claim: N concurrent claims of one key, one
  winner, the completed record visible to every loser;
- the cheque registry: N concurrent creates / registrations with the
  same Sayyad number, one live row, the losers get the typed
  duplicate error (never a raw IntegrityError, never a second row),
  and the engine sees exactly one cheque creation;
- the event worker: two workers racing ``process_one`` on one inbox
  event, exactly one processes it (``FOR UPDATE SKIP LOCKED``) —
  the §19.9 exactly-once processing guarantee at the claim layer.
  (The HTTP-level duplicate-ingest race is in
  ``tests/interfaces/test_rest_load_us702.py``.)
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections.abc import Generator
from typing import cast

import pytest
from sqlalchemy import create_engine, delete, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from hesabyar.application.commands.cheques import ChequeCommandService
from hesabyar.application.commands.events import EventPipelineService, EventWorker
from hesabyar.domain.accounting.commands import CreateEngineCheque
from hesabyar.domain.accounting.ports import AccountingEnginePort
from hesabyar.domain.accounting.refs import ChequeEngineRef
from hesabyar.domain.accounting.value_objects import ChequeEngineView
from hesabyar.domain.cheques.errors import ChequeDuplicateError
from hesabyar.domain.cheques.models import Cheque, ChequeState
from hesabyar.domain.cheques.ports import registration_to_cheque
from hesabyar.domain.cheques.validation import validate_registration_payload
from hesabyar.domain.events.envelope import (
    EVENT_INGEST_OPERATION,
    CanonicalEvent,
    CanonicalEventType,
)
from hesabyar.domain.events.models import (
    InboxEventStatus,
    PipelineOutcome,
    PipelineResult,
)
from hesabyar.infrastructure.events.defaults import InProcessOutboxPublisher
from hesabyar.infrastructure.persistence.postgres.cheques_repository import (
    SqlAlchemyChequeRepository,
)
from hesabyar.infrastructure.persistence.postgres.event_repositories import (
    SqlAlchemyEventIdempotencyStore,
    SqlAlchemyEventInboxRepository,
    SqlAlchemyEventOutboxRepository,
)
from hesabyar.infrastructure.persistence.postgres.models import (
    Base,
    ChequeModel,
    IdempotencyRecordModel,
    InboxEventModel,
)
from tests.event_fixtures import raw_envelope
from tests.load_support import run_threads

TEST_DB_URL = os.getenv(
    "HESABYAR_DATABASE_URL",
    "postgresql+psycopg://hesabyar:hesabyar_pass@hesabyar-postgres:5432/hesabyar_test",
)

EVENT_TYPE = CanonicalEventType.SALES_ORDER_COMPLETED
COMPANY_ENGINE_ID = 2


@pytest.fixture(scope="module")
def engine() -> Generator[Engine, None, None]:
    eng = create_engine(TEST_DB_URL, echo=False)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


def _unique_sayad_id() -> str:
    """A 16-digit Sayyad id unique to this test run (first digit non-zero)."""
    return str(10**15 + uuid.uuid4().int % (9 * 10**15))


def _payload(sayad_id: str) -> dict[str, object]:
    return {
        "sayad_id": sayad_id,
        "amount_toman": 1750000,
        "amount_currency": "IRT",
        "issue_date": "1405-08-26",
        "due_date": "1405-10-15",
        "issuer_name": "مشتری نمونه",
        "issuer_national_id": "0012345678",
        "issuer_bank": "056 سامان",
        "payee_name": "کافه ایوان",
        "payee_national_id": "00123456789",
        "purpose": "payment",
        "order_id": "ord-702",
        "customer_ref": "cust-702",
        "sayad_registered": True,
    }


def _cheque(
    sayad_id: str, tenant_id: str, installation_id: str
) -> Cheque:
    registration = validate_registration_payload(_payload(sayad_id))
    return registration_to_cheque(
        registration,
        cheque_id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        installation_id=installation_id,
        company_engine_id=COMPANY_ENGINE_ID,
        source="manual",
    )


def _count_live_cheques(engine: Engine, sayad_id: str) -> int:
    session = sessionmaker(bind=engine)()
    try:
        return session.query(ChequeModel).filter(
            ChequeModel.sayad_id == sayad_id,
            ChequeModel.state.notin_(("voided", "expired_unreceived", "rejected_mismatch")),
        ).count()
    finally:
        session.close()


def _delete_cheques(engine: Engine, sayad_id: str) -> None:
    session = sessionmaker(bind=engine)()
    try:
        session.execute(
            delete(ChequeModel).where(ChequeModel.sayad_id == sayad_id)
        )
        session.commit()
    finally:
        session.close()


# -- idempotency claim ---------------------------------------------------------


def test_concurrent_idempotency_claim_exactly_one_wins(engine: Engine) -> None:
    """Ten concurrent claims of one key: one record, one winner, all see it."""
    installation_id = f"us702-{uuid.uuid4().hex[:12]}"
    try:
        def _claim(_index: int) -> bool:
            session = sessionmaker(bind=engine)()
            try:
                store = SqlAlchemyEventIdempotencyStore(session)
                record = store.claim(
                    installation_id, EVENT_INGEST_OPERATION, "key-702", "hash-702"
                )
                if record is None:
                    session.commit()
                    return False
                store.complete(
                    record,
                    response_status=201,
                    response_body={"id": "evt-702"},
                    response_reference="evt-702",
                )
                session.commit()
                return True
            finally:
                session.close()

        winners = run_threads(10, _claim)
        assert sum(winners) == 1

        session = sessionmaker(bind=engine)()
        try:
            found = SqlAlchemyEventIdempotencyStore(session).find(
                installation_id, EVENT_INGEST_OPERATION, "key-702"
            )
            assert found is not None
            assert found.is_completed is True
            assert found.response_status == 201
            assert found.response_reference == "evt-702"
            count = session.query(IdempotencyRecordModel).filter(
                IdempotencyRecordModel.installation_id == installation_id,
                IdempotencyRecordModel.idempotency_key == "key-702",
            ).count()
            assert count == 1
        finally:
            session.close()
    finally:
        session = sessionmaker(bind=engine)()
        try:
            session.execute(
                delete(IdempotencyRecordModel).where(
                    IdempotencyRecordModel.installation_id == installation_id
                )
            )
            session.commit()
        finally:
            session.close()


# -- concurrent cheque registration --------------------------------------------


def test_concurrent_cheque_create_same_sayad_id(engine: Engine) -> None:
    """Eight concurrent creates, one Sayyad number: one row, typed duplicates.

    The losers raise :class:`ChequeDuplicateError` naming the winner —
    never a raw IntegrityError, never a second live row.
    """
    sayad_id = _unique_sayad_id()
    tenant_id = str(uuid.uuid4())
    installation_id = str(uuid.uuid4())
    try:
        def _create(_index: int) -> str:
            session = sessionmaker(bind=engine)()
            try:
                repo = SqlAlchemyChequeRepository(session)
                try:
                    created = repo.create(_cheque(sayad_id, tenant_id, installation_id))
                except ChequeDuplicateError as exc:
                    session.rollback()
                    assert exc.details is not None
                    return f"duplicate:{exc.details['existing_cheque_id']}"
                session.commit()
                return f"created:{created.id}"
            finally:
                session.close()

        outcomes = run_threads(8, _create)
        created = [outcome for outcome in outcomes if outcome.startswith("created:")]
        duplicates = [outcome for outcome in outcomes if outcome.startswith("duplicate:")]
        assert len(created) == 1, outcomes
        assert len(duplicates) == 7, outcomes
        winner_id = created[0].removeprefix("created:")
        assert all(outcome == f"duplicate:{winner_id}" for outcome in duplicates)
        assert _count_live_cheques(engine, sayad_id) == 1
    finally:
        _delete_cheques(engine, sayad_id)


class _CountingFakeEngine:
    """Thread-safe fake engine: counts cheque creations, hands out ids."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_engine_id = 1000
        self.created_sayad_ids: list[str] = []

    def create_engine_cheque(self, command: CreateEngineCheque) -> ChequeEngineView:
        with self._lock:
            engine_id = self._next_engine_id
            self._next_engine_id += 1
            self.created_sayad_ids.append(command.sayad_id)
        return ChequeEngineView(
            ref=ChequeEngineRef(engine_id=engine_id),
            company=command.company,
            kind=command.kind,
            purpose=command.purpose,
            sayad_id=command.sayad_id,
            state=ChequeState.REGISTERED.value,
            sayad_status="unregistered",
            amount=command.amount,
            issue_date=command.issue_date,
            due_date=command.due_date,
            move_ids={},
        )


def test_concurrent_cheque_registration_service(engine: Engine) -> None:
    """Eight concurrent registrations, one Sayyad number: one cheque, one engine row.

    The winner registers + links the engine; each loser either gets
    :class:`ChequeDuplicateError` (it lost the index race) or the
    existing cheque back (the same-claim convergence: it ran its
    pre-check after the winner committed). Either way there is exactly
    one live row and the engine created exactly one cheque.
    """
    sayad_id = _unique_sayad_id()
    tenant_id = str(uuid.uuid4())
    installation_id = str(uuid.uuid4())
    fake_engine = _CountingFakeEngine()
    try:
        def _register(_index: int) -> str:
            session = sessionmaker(bind=engine)()
            try:
                service = ChequeCommandService(
                    SqlAlchemyChequeRepository(session),
                    cast(AccountingEnginePort, fake_engine),
                )
                try:
                    cheque = service.register_manual(
                        tenant_id=tenant_id,
                        installation_id=installation_id,
                        company_engine_id=COMPANY_ENGINE_ID,
                        payload=_payload(sayad_id),
                        actor="us702-load",
                    )
                except ChequeDuplicateError as exc:
                    session.rollback()
                    assert exc.details is not None
                    return f"duplicate:{exc.details['existing_cheque_id']}"
                session.commit()
                assert cheque.engine_id is not None
                return f"registered:{cheque.id}"
            finally:
                session.close()

        outcomes = run_threads(8, _register)
        registered = [o for o in outcomes if o.startswith("registered:")]
        duplicates = [o for o in outcomes if o.startswith("duplicate:")]
        assert len(registered) + len(duplicates) == 8, outcomes
        assert len(registered) >= 1, outcomes
        # Every outcome names the same single cheque.
        ids = {o.split(":", 1)[1] for o in outcomes}
        assert len(ids) == 1, outcomes
        assert _count_live_cheques(engine, sayad_id) == 1
        assert fake_engine.created_sayad_ids == [sayad_id]
    finally:
        _delete_cheques(engine, sayad_id)


# -- exactly-once worker processing --------------------------------------------


class _CountingFakePipeline:
    """A pipeline stand-in that records calls and holds the claim briefly.

    The sleep widens the window in which the winner still holds the
    row lock while the loser attempts its claim — without it the two
    claims could serialize too fast to exercise SKIP LOCKED. The
    sleep is a test-double delay, not a timing assertion: the test
    asserts counts, and either claim order gives the same counts.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.processed: list[str] = []

    def process(self, event_id: str) -> PipelineResult:
        with self._lock:
            self.processed.append(event_id)
        time.sleep(0.3)
        return PipelineResult(
            inbox_event_id=event_id, outcome=PipelineOutcome.PROCESSED_DRAFT
        )


def test_concurrent_worker_process_one_exactly_once(engine: Engine) -> None:
    """Two workers racing process_one on one event: exactly one processes it."""
    source = f"us702-{uuid.uuid4().hex[:10]}"
    event = CanonicalEvent.from_raw(
        raw_envelope(source_system=source, source_event_id="evt-702")
    )
    setup = sessionmaker(bind=engine)()
    try:
        outcome = SqlAlchemyEventInboxRepository(setup).store(event, EVENT_TYPE, None)
        assert outcome.status == "created"
        assert outcome.event is not None
        event_id = outcome.event.id
        setup.commit()
    finally:
        setup.close()
    pipeline = _CountingFakePipeline()
    try:
        def _process(_index: int) -> bool:
            session = sessionmaker(bind=engine)()
            try:
                worker = EventWorker(
                    pipeline=cast(EventPipelineService, pipeline),
                    inbox=SqlAlchemyEventInboxRepository(session),
                    outbox=SqlAlchemyEventOutboxRepository(session),
                    publisher=InProcessOutboxPublisher(),
                )
                result = worker.process_one(event_id)
                session.commit()
                return result is not None
            finally:
                session.close()

        processed = run_threads(2, _process)
        assert sum(processed) == 1
        assert pipeline.processed == [event_id]

        session = sessionmaker(bind=engine)()
        try:
            row = session.get(InboxEventModel, event_id)
            assert row is not None
            assert row.status == InboxEventStatus.PROCESSING.value
            assert row.attempts == 1
        finally:
            session.close()
    finally:
        session = sessionmaker(bind=engine)()
        try:
            session.execute(
                text("DELETE FROM inbox_events WHERE source_system = :source"),
                {"source": source},
            )
            session.commit()
        finally:
            session.close()
