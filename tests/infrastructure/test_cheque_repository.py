"""Tests for the SQLAlchemy cheque repository (US-531).

Runs against the local PostgreSQL dev DB with a transactional
session (rolled back after each test). The service layer is tested
DB-free against an in-memory fake
(``tests/unit/test_cheque_services.py``); this module verifies the
repository implementation behind the port — including the partial
unique index on live sayad_ids and the expiry/reminder queries.
"""

from __future__ import annotations

import datetime
import os
import uuid
from collections.abc import Generator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from hesabyar.domain.cheques.models import (
    Cheque,
    ChequeSettings,
    ChequeState,
    CustodyLogEntry,
    InquiryChannel,
    InquiryReceipt,
    InquiryType,
    RedBrownPolicy,
    StorageLocation,
)
from hesabyar.domain.cheques.ports import registration_to_cheque
from hesabyar.domain.cheques.validation import validate_registration_payload
from hesabyar.infrastructure.persistence.postgres.cheques_repository import (
    SqlAlchemyChequeRepository,
)
from hesabyar.infrastructure.persistence.postgres.models import Base

TEST_DB_URL = os.getenv(
    "HESABYAR_DATABASE_URL",
    "postgresql+psycopg://hesabyar@localhost:5433/hesabyar",
)

TENANT_ID = "00000000-0000-0000-0000-000000000531"
INSTALLATION_ID = "00000000-0000-0000-0000-000000000532"
OTHER_INSTALLATION_ID = "00000000-0000-0000-0000-000000000533"


@pytest.fixture(scope="module")
def engine() -> Generator[Engine, None, None]:
    eng = create_engine(TEST_DB_URL, echo=False)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def db(engine: Engine) -> Generator[Session, None, None]:
    connection = engine.connect()
    transaction = connection.begin()
    session = sessionmaker(bind=connection)()
    try:
        yield session
    finally:
        session.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()


@pytest.fixture()
def repository(db: Session) -> SqlAlchemyChequeRepository:
    return SqlAlchemyChequeRepository(db)


def _payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "sayad_id": "1234567890123456",
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
        "order_id": "ord-1",
        "customer_ref": "cust-1",
        "sayad_registered": True,
    }
    base.update(overrides)
    return base


def _create(repository: SqlAlchemyChequeRepository, **overrides: object):
    registration = validate_registration_payload(_payload(**overrides))
    return repository.create(
        registration_to_cheque(
            registration,
            cheque_id=str(uuid.uuid4()),
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            company_engine_id=2,
            source="manual",
        )
    )


# -- create / get ---------------------------------------------------------------------


def test_create_and_get_round_trip(repository: SqlAlchemyChequeRepository) -> None:
    created = _create(repository, leaf_number="847291", notes="test note")
    found = repository.get(
        created.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID
    )
    assert found is not None
    assert found.sayad_id == "1234567890123456"
    assert found.amount_irr == 17_500_000
    assert found.state is ChequeState.REGISTERED
    assert found.leaf_number == "847291"
    assert found.notes == "test note"
    assert found.move_ids == {}
    assert found.engine_id is None


def test_get_is_tenant_and_installation_scoped(
    repository: SqlAlchemyChequeRepository,
) -> None:
    created = _create(repository)
    assert (
        repository.get(
            created.id, tenant_id="other-tenant", installation_id=INSTALLATION_ID
        )
        is None
    )
    assert (
        repository.get(
            created.id, tenant_id=TENANT_ID, installation_id=OTHER_INSTALLATION_ID
        )
        is None
    )


def test_save_persists_state_and_move_links(
    repository: SqlAlchemyChequeRepository,
) -> None:
    created = _create(repository)
    created.state = ChequeState.HELD
    created.engine_id = 1001
    created.move_ids = {"confirm_receipt": 5000}
    created.storage_location = StorageLocation.VAULT
    saved = repository.save(created)
    assert saved.state is ChequeState.HELD
    assert saved.move_ids == {"confirm_receipt": 5000}
    found = repository.get(
        created.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID
    )
    assert found is not None
    assert found.state is ChequeState.HELD
    assert found.engine_id == 1001
    assert found.storage_location is StorageLocation.VAULT


def test_save_missing_row_raises(repository: SqlAlchemyChequeRepository) -> None:
    created = _create(repository)
    created.id = str(uuid.uuid4())
    with pytest.raises(ValueError, match="does not exist"):
        repository.save(created)


# -- sayad uniqueness (partial unique index) -------------------------------------------


def test_live_sayad_id_is_unique(repository: SqlAlchemyChequeRepository) -> None:
    _create(repository)
    with pytest.raises(IntegrityError):
        _create(repository)


def test_expired_sayad_id_may_be_reregistered(
    repository: SqlAlchemyChequeRepository,
) -> None:
    created = _create(repository)
    created.state = ChequeState.EXPIRED_UNRECEIVED
    repository.save(created)
    assert (
        repository.get_by_sayad_id(
            "1234567890123456", tenant_id=TENANT_ID, installation_id=INSTALLATION_ID
        )
        is None
    )
    # The DB index agrees: a new row with the same sayad_id inserts.
    second = _create(repository)
    assert second.id != created.id
    found = repository.get_by_sayad_id(
        "1234567890123456", tenant_id=TENANT_ID, installation_id=INSTALLATION_ID
    )
    assert found is not None and found.id == second.id


def test_rejected_sayad_id_may_be_reregistered(
    repository: SqlAlchemyChequeRepository,
) -> None:
    created = _create(repository)
    created.state = ChequeState.REJECTED_MISMATCH
    repository.save(created)
    second = _create(repository)
    assert second.id != created.id


# -- list / job queries ------------------------------------------------------------------


def test_list_filters_by_state_and_due_before(
    repository: SqlAlchemyChequeRepository,
) -> None:
    first = _create(repository)
    _create(repository, sayad_id="2222222222222222", due_date="1405-11-01")
    first.state = ChequeState.HELD
    repository.save(first)
    held = repository.list(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        state=ChequeState.HELD,
    )
    assert [c.id for c in held] == [first.id]
    due_early = repository.list(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        due_before=datetime.date(2027, 1, 10),
    )
    assert len(due_early) == 1
    assert due_early[0].sayad_id == "1234567890123456"


def test_find_registered_cheques_and_list_registered_before(
    repository: SqlAlchemyChequeRepository, db: Session
) -> None:
    old = _create(repository, sayad_id="1111111111111111")
    # created_at is set at insert only; move the row directly for the
    # age-based query.
    db.execute(
        text("UPDATE cheques SET created_at = :at WHERE id = :id"),
        {
            "at": datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=8),
            "id": old.id,
        },
    )
    db.flush()
    _create(repository, sayad_id="2222222222222222")
    registered = repository.find_registered_cheques()
    assert {c.sayad_id for c in registered} >= {
        "1111111111111111",
        "2222222222222222",
    }
    before = repository.list_registered_before(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        registered_before=datetime.datetime.now(datetime.UTC)
        - datetime.timedelta(days=7),
    )
    assert [c.sayad_id for c in before] == ["1111111111111111"]


# -- custody + receipts --------------------------------------------------------------------


def test_custody_entries_are_append_only_and_ordered(
    repository: SqlAlchemyChequeRepository,
) -> None:
    created = _create(repository)
    repository.add_custody_entry(
        CustodyLogEntry(
            cheque_id=created.id,
            from_location=None,
            to_location=StorageLocation.VAULT,
            actor="operator-1",
            reason="leaf received",
        )
    )
    repository.add_custody_entry(
        CustodyLogEntry(
            cheque_id=created.id,
            from_location=StorageLocation.VAULT,
            to_location=StorageLocation.CASHBOX,
            actor="operator-1",
            reason="moved to cashbox",
        )
    )
    entries = repository.list_custody_entries(created.id)
    assert [e.to_location for e in entries] == [
        StorageLocation.VAULT,
        StorageLocation.CASHBOX,
    ]


def test_inquiry_receipts_round_trip(repository: SqlAlchemyChequeRepository) -> None:
    created = _create(repository)
    receipt = repository.add_inquiry_receipt(
        InquiryReceipt(
            cheque_id=created.id,
            channel=InquiryChannel.FINNOTECH_API,
            inquiry_type=InquiryType.COLOR,
            result_code="white",
            result_summary="رنگ سفید — تضمین نیست",
            reference="inq-2",
            recorded_by="operator-1",
            cost_irr=3500,
        )
    )
    receipts = repository.list_inquiry_receipts(created.id)
    assert len(receipts) == 1
    assert receipts[0].id == receipt.id
    assert receipts[0].result_code == "white"
    assert receipts[0].cost_irr == 3500


# -- settings --------------------------------------------------------------------------------


def test_settings_upsert_and_defaults(repository: SqlAlchemyChequeRepository) -> None:
    assert repository.get_settings(INSTALLATION_ID) is None
    saved = repository.save_settings(
        ChequeSettings(
            installation_id=INSTALLATION_ID,
            leaf_arrival_deadline_days=10,
            red_brown_policy=RedBrownPolicy.REJECT,
        )
    )
    assert saved.leaf_arrival_deadline_days == 10
    assert saved.red_brown_policy is RedBrownPolicy.REJECT
    # Upsert: a second save updates the same row.
    saved = repository.save_settings(
        ChequeSettings(
            installation_id=INSTALLATION_ID,
            leaf_arrival_deadline_days=5,
        )
    )
    assert saved.leaf_arrival_deadline_days == 5
    assert saved.red_brown_policy is RedBrownPolicy.MANAGER_APPROVAL
    found = repository.get_settings(INSTALLATION_ID)
    assert found is not None and found.leaf_arrival_deadline_days == 5


# -- US-532: issued / collateral columns + filters ------------------------------


def _create_issued(
    repository: SqlAlchemyChequeRepository,
    **overrides: object,
) -> Cheque:
    """Create an issued cheque with checkbook + obligation columns."""
    from hesabyar.domain.cheques.models import ChequeKind

    registration = validate_registration_payload(
        _payload(purpose="collateral", **overrides), kind=ChequeKind.ISSUED
    )
    return repository.create(
        registration_to_cheque(
            registration,
            cheque_id=str(uuid.uuid4()),
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            company_engine_id=2,
            source="manual",
            secured_obligation="قرارداد اجاره ۱۴۰۵/۰۳",
            checkbook_engine_id=7,
        )
    )


def test_us532_columns_round_trip(
    repository: SqlAlchemyChequeRepository,
) -> None:
    created = _create_issued(repository)
    found = repository.get(
        created.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID
    )
    assert found is not None
    assert found.kind.value == "issued"
    assert found.purpose.value == "collateral"
    assert found.checkbook_engine_id == 7
    assert found.secured_obligation == "قرارداد اجاره ۱۴۰۵/۰۳"
    assert found.void_reason is None
    assert found.unexpected_call_in is False
    assert found.call_in_statement_reference is None
    # Mutating the lifecycle fields persists too.
    found.state = ChequeState.VOIDED
    found.void_reason = "اشتباه در مبلغ"
    found.call_in_approved_by = "حسابدار ارشد"
    found.unexpected_call_in = True
    found.call_in_statement_reference = "stmt-1"
    repository.save(found)
    reloaded = repository.get(
        created.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID
    )
    assert reloaded is not None
    assert reloaded.state is ChequeState.VOIDED
    assert reloaded.void_reason == "اشتباه در مبلغ"
    assert reloaded.call_in_approved_by == "حسابدار ارشد"
    assert reloaded.unexpected_call_in is True
    assert reloaded.call_in_statement_reference == "stmt-1"


def test_list_filters_by_kind_purpose_and_storage(
    repository: SqlAlchemyChequeRepository,
) -> None:
    from hesabyar.domain.cheques.models import ChequeKind, ChequePurpose

    commercial = _create(repository)
    collateral = _create(
        repository, sayad_id="2222222222222222", purpose="collateral"
    )
    issued = _create_issued(repository, sayad_id="3333333333333333")
    collateral.state = ChequeState.HELD
    collateral.storage_location = StorageLocation.CASHBOX
    repository.save(collateral)

    by_kind = repository.list(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        kind=ChequeKind.ISSUED,
    )
    assert [c.id for c in by_kind] == [issued.id]

    by_purpose = repository.list(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        purpose=ChequePurpose.COLLATERAL,
    )
    assert {c.id for c in by_purpose} == {collateral.id, issued.id}

    by_storage = repository.list(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        storage_location=StorageLocation.CASHBOX,
    )
    assert [c.id for c in by_storage] == [collateral.id]

    by_endorsee = repository.list(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        endorsee_type="supplier",
    )
    assert by_endorsee == []
    commercial.endorsee_type = "supplier"
    commercial.state = ChequeState.ENDORSED
    repository.save(commercial)
    by_endorsee = repository.list(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        endorsee_type="supplier",
    )
    assert [c.id for c in by_endorsee] == [commercial.id]
