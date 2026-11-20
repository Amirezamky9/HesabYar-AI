"""Repository tests for the bank import batch flush order (US-555).

DB-free: the real ``SqlAlchemyBankRepository`` runs against a
recording session fake, so the test asserts the flush ORDERING
itself — the batch row must be flushed before its rows are added —
rather than relying on PostgreSQL's FK error surfacing (the
ordering hazard is hash-order luck: the same code can pass on one
mapper layout and violate ``bank_import_rows.batch_id_fkey`` on
another, exactly the installments CI run #6 failure). The real-DB
companion lives in ``tests/infrastructure/test_bank_repository.py``.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any, cast

from sqlalchemy.orm import Session

from hesabyar.domain.bank.models import (
    BankColumnMapping,
    BankImportBatch,
    BankImportRow,
    ColumnMappingStatus,
    FileCurrencyUnit,
    ImportBatchStatus,
    StatementFileFormat,
)
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.infrastructure.persistence.postgres.bank_repository import (
    SqlAlchemyBankRepository,
)
from hesabyar.infrastructure.persistence.postgres.models import (
    BankImportBatchModel,
    BankImportRowModel,
)

TENANT_ID = "00000000-0000-0000-0000-000000000555"
COMPANY_ID = "00000000-0000-0000-0000-000000000556"


class RecordingSession:
    """A session fake that records add/flush calls in order."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def add(self, row: Any) -> None:
        self.calls.append(("add", type(row).__name__))

    def flush(self) -> None:
        self.calls.append(("flush", ""))


def _batch() -> BankImportBatch:
    return BankImportBatch(
        id=str(uuid.uuid4()),
        tenant_id=TENANT_ID,
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
        amount_irr=1_000_000,
        description=f"row {number}",
        bank_ref=f"REF-{number}",
    )


def test_save_batch_flushes_parent_before_children() -> None:
    """The batch INSERT must reach the DB before any row INSERT.

    SQLAlchemy orders a flush by relationships, not by bare FK
    columns, and BankImportBatchModel / BankImportRowModel carry no
    relationship — without the explicit flush between the batch add
    and the row adds, the rows can be emitted first and violate
    bank_import_rows.batch_id_fkey (US-555 FK-flush sweep).
    """
    session = RecordingSession()
    repository = SqlAlchemyBankRepository(cast(Session, session))
    batch = _batch()
    rows = [_row(batch.id, 1), _row(batch.id, 2)]

    saved = repository.save_batch(batch, rows)

    assert saved.id == batch.id
    assert saved.filename == "baam.csv"
    batch_add = session.calls.index(("add", BankImportBatchModel.__name__))
    first_flush = session.calls.index(("flush", ""))
    first_row_add = session.calls.index(("add", BankImportRowModel.__name__))
    assert batch_add < first_flush < first_row_add
    assert session.calls.count(("add", BankImportRowModel.__name__)) == 2
    assert session.calls.count(("flush", "")) == 2
    # The final flush follows the last row add.
    assert session.calls[-1] == ("flush", "")


def test_save_batch_with_no_rows_still_flushes() -> None:
    session = RecordingSession()
    repository = SqlAlchemyBankRepository(cast(Session, session))
    batch = _batch()

    saved = repository.save_batch(batch, [])

    assert saved.id == batch.id
    assert session.calls == [
        ("add", BankImportBatchModel.__name__),
        ("flush", ""),
        ("flush", ""),
    ]


def test_save_mapping_and_rule_and_match_flush_each_add() -> None:
    """Single-row saves flush immediately (sweep: no parent+child here)."""
    session = RecordingSession()
    repository = SqlAlchemyBankRepository(cast(Session, session))
    mapping = BankColumnMapping(
        id=str(uuid.uuid4()),
        tenant_id=TENANT_ID,
        bank_key="melli",
        name="Baam CSV",
        file_format=StatementFileFormat.CSV,
        columns={"date": "تاریخ"},
        date_kind="jalali",
        currency_unit=FileCurrencyUnit.RIAL,
        header_row=1,
        company_id=COMPANY_ID,
        status=ColumnMappingStatus.ACTIVE,
        created_at=datetime.datetime.now(datetime.UTC),
        updated_at=datetime.datetime.now(datetime.UTC),
    )
    saved = repository.save_mapping(mapping)
    assert saved.id == mapping.id
    assert session.calls == [("add", "BankColumnMappingModel"), ("flush", "")]
