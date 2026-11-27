"""Tests for the SQLAlchemy bank repository (US-514; US-555 sweep).

Runs against the local PostgreSQL dev DB with a transactional
session (rolled back after each test). The service layer is tested
DB-free against an in-memory fake
(``tests/unit/test_bank_services.py``); this module verifies the
repository implementation behind the port — in particular that
``save_batch`` persists a batch and its rows in one call (the
US-555 FK-flush sweep: the batch is flushed before its rows, so
the row INSERTs never precede the batch INSERT and violate
``bank_import_rows.batch_id_fkey``). The flush ORDERING itself is
asserted DB-free in ``tests/unit/test_bank_repository.py``.
"""

from __future__ import annotations

import datetime
import os
import uuid
from collections.abc import Generator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from hesabyar.domain.bank.models import (
    BankImportBatch,
    BankImportRow,
    FileCurrencyUnit,
    ImportBatchStatus,
    StatementFileFormat,
)
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.infrastructure.persistence.postgres.bank_repository import (
    SqlAlchemyBankRepository,
)
from hesabyar.infrastructure.persistence.postgres.models import Base, TenantModel

TEST_DB_URL = os.getenv(
    "HESABYAR_DATABASE_URL",
    "postgresql+psycopg://hesabyar@localhost:5433/hesabyar",
)

COMPANY_ID = "00000000-0000-0000-0000-000000000555"


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
def repository(db: Session) -> SqlAlchemyBankRepository:
    return SqlAlchemyBankRepository(db)


@pytest.fixture()
def tenant_id(db: Session) -> str:
    """A real tenant row: bank batches/rows FK to ``tenants.id``."""
    tenant = TenantModel(
        id=str(uuid.uuid4()),
        slug=f"bank-repo-555-{uuid.uuid4().hex[:8]}",
        name="Bank Repository Test Tenant",
    )
    db.add(tenant)
    db.flush()
    return tenant.id


def _batch(tenant_id: str) -> BankImportBatch:
    return BankImportBatch(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        company_id=COMPANY_ID,
        bank_key="melli",
        statement_journal_id=1,
        filename="baam.csv",
        file_format=StatementFileFormat.CSV,
        currency_unit=FileCurrencyUnit.RIAL,
        status=ImportBatchStatus.AWAITING_REVIEW,
        total_rows=2,
        new_rows=2,
        duplicate_rows=0,
        invalid_rows=0,
        total_deposits_irr=2_000_000,
        total_withdrawals_irr=500_000,
        default_account_id=1,
    )


def _row(batch_id: str, number: int) -> BankImportRow:
    return BankImportRow(
        id=str(uuid.uuid4()),
        batch_id=batch_id,
        row_number=number,
        row_date=FiscalDate.from_gregorian(datetime.date(2026, 11, 26)),
        amount_irr=1_000_000 if number == 1 else -500_000,
        description=f"row {number}",
        bank_ref=f"REF-555-{number}",
    )


def test_save_batch_persists_batch_and_rows(repository: SqlAlchemyBankRepository, tenant_id: str) -> None:
    """Batch + rows survive one save_batch call (US-555 sweep).

    Before the fix the batch and its rows went out in a single
    flush with no relationship between the mappers, so the row
    INSERTs could precede the batch INSERT and violate the FK —
    the same class as the installments CI run #6 failure.
    """
    batch = _batch(tenant_id)
    rows = [_row(batch.id, 1), _row(batch.id, 2)]

    saved = repository.save_batch(batch, rows)

    assert saved.id == batch.id
    assert saved.status == ImportBatchStatus.AWAITING_REVIEW
    fetched = repository.get_batch(batch.id, tenant_id)
    assert fetched is not None
    assert fetched.filename == "baam.csv"
    assert fetched.total_rows == 2
    stored_rows = repository.list_rows(batch.id, tenant_id)
    assert [row.row_number for row in stored_rows] == [1, 2]
    assert [row.bank_ref for row in stored_rows] == ["REF-555-1", "REF-555-2"]
    assert stored_rows[0].amount_irr == 1_000_000
    assert stored_rows[1].amount_irr == -500_000


def test_save_batch_with_no_rows_persists_batch(repository: SqlAlchemyBankRepository, tenant_id: str) -> None:
    batch = _batch(tenant_id)
    saved = repository.save_batch(batch, [])
    assert saved.id == batch.id
    assert repository.list_rows(batch.id, tenant_id) == []
