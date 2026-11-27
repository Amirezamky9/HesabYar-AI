"""Tests for the US-523 settlement repository methods.

Runs against the shared Supabase test DB with a transactional session
(rolled back after each test). Covers the import/matching reads the
US-523 service relies on: event/deposit getters, the unmatched-only
deposit query (NOT EXISTS), match lookups by deposit/event, the C7
``update_match`` attach, open-exception lookups by deposit/event,
file-import list/update, file-row update, fee-record actuals, and
the multi-state tranche filter used by batch matching.
"""

from __future__ import annotations

import datetime
import os
import uuid
from collections.abc import Generator
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from hesabyar.domain.integrations.commands import (
    CreateGatewayContract,
    CreatePaymentChannel,
)
from hesabyar.domain.integrations.models import TrancheLeg
from hesabyar.domain.settlement.commands import (
    CreateBankDeposit,
    CreateSettlementEvent,
    CreateSettlementException,
    CreateSettlementFileImport,
    CreateSettlementFileRow,
    CreateSettlementMatch,
)
from hesabyar.domain.settlement.models import (
    BankDeposit,
    SettlementEvent,
    SettlementFileImport,
    SettlementTranche,
    TrancheState,
)
from hesabyar.infrastructure.persistence.postgres.integrations_repository import (
    SqlAlchemyIntegrationsRepository,
)
from hesabyar.infrastructure.persistence.postgres.models import Base
from hesabyar.infrastructure.persistence.postgres.settlement_repository import (
    SqlAlchemySettlementRepository,
)
from tests.infrastructure.test_settlement_repository import (
    INSTALLATION_ID,
    OTHER_INSTALLATION_ID,
    _derived_tranches,
    _save_payment,
)

TEST_DB_URL = os.getenv(
    "HESABYAR_DATABASE_URL",
    "postgresql+psycopg://hesabyar:hesabyar_pass@hesabyar-postgres:5432/hesabyar_test",
)

FOUR_TRANCHES = (
    TrancheLeg(1, 5, Decimal("25")),
    TrancheLeg(2, 5, Decimal("25")),
    TrancheLeg(3, 5, Decimal("25")),
    TrancheLeg(4, 5, Decimal("25")),
)


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
def repository(db: Session) -> SqlAlchemySettlementRepository:
    return SqlAlchemySettlementRepository(db)


@pytest.fixture()
def registry(db: Session) -> SqlAlchemyIntegrationsRepository:
    return SqlAlchemyIntegrationsRepository(db)


@pytest.fixture()
def channel_and_contract(
    registry: SqlAlchemyIntegrationsRepository,
) -> tuple[str, str]:
    """A snapppay channel + active 4-tranche contract (fee 10%, VAT 10%)."""
    key = f"snapppay-{uuid.uuid4().hex[:8]}"
    channel = registry.save_channel(
        CreatePaymentChannel(
            installation_id=INSTALLATION_ID,
            channel_key=key,
            display_name_fa="اسنپ‌پی",
            channel_type="bnpl",
        )
    )
    contract = registry.save_contract(
        CreateGatewayContract(
            channel_id=channel.id,
            version=1,
            valid_from=datetime.date(2026, 10, 23),
            fee_percent=Decimal("10"),
            fee_vat_rate=Decimal("10"),
            tranche_legs=FOUR_TRANCHES,
            payout_window_start_day=1,
            payout_window_end_day=5,
            grace_days_after_due=3,
        )
    )
    return channel.id, contract.id


def _save_event(
    repository: SqlAlchemySettlementRepository,
    channel_id: str,
    *,
    batch_id: str | None = None,
    payout_date: datetime.date = datetime.date(2026, 11, 27),
    net_irr: int = 22_250_000,
) -> SettlementEvent:
    return repository.save_settlement_event(
        CreateSettlementEvent(
            installation_id=INSTALLATION_ID,
            channel_key="snapppay",
            payout_date=payout_date,
            net_irr=net_irr,
            source="payouts_file",
            tracking_ref="TRK-1",
            import_batch_id=batch_id,
        ),
        channel_id=channel_id,
    )


def _save_deposit(
    repository: SqlAlchemySettlementRepository,
    *,
    amount_irr: int = 22_250_000,
    deposit_date: datetime.date = datetime.date(2026, 11, 27),
) -> BankDeposit:
    return repository.save_bank_deposit(
        CreateBankDeposit(
            installation_id=INSTALLATION_ID,
            deposit_date=deposit_date,
            amount_irr=amount_irr,
            tracking_ref="TRK-1",
        )
    )


def _save_import(
    repository: SqlAlchemySettlementRepository,
    channel_id: str,
    *,
    file_hash: str | None = None,
    batch_id: str | None = None,
) -> SettlementFileImport:
    return repository.save_file_import(
        CreateSettlementFileImport(
            installation_id=INSTALLATION_ID,
            channel_key="snapppay",
            file_type="detail",
            period_jalali="1405/08",
            file_hash_sha256=file_hash or uuid.uuid4().hex * 2,
            import_batch_id=batch_id or str(uuid.uuid4()),
            uploaded_by="ops",
            row_count=1,
        ),
        channel_id=channel_id,
    )


def _setup_tranche(
    repository: SqlAlchemySettlementRepository,
    channel_and_contract: tuple[str, str],
) -> tuple[str, str, SettlementTranche]:
    channel_id, contract_id = channel_and_contract
    payment = _save_payment(repository, channel_id, contract_id)
    tranche = repository.save_tranches(
        INSTALLATION_ID, payment.id, _derived_tranches()[:1]
    )[0]
    return channel_id, contract_id, tranche


class TestEventsAndDeposits:
    def test_get_settlement_event(
        self,
        repository: SqlAlchemySettlementRepository,
        channel_and_contract: tuple[str, str],
    ) -> None:
        channel_id, _ = channel_and_contract
        event = _save_event(repository, channel_id)
        found = repository.get_settlement_event(INSTALLATION_ID, event.id)
        assert found is not None
        assert found.id == event.id
        assert found.net_irr == 22_250_000
        assert repository.get_settlement_event(OTHER_INSTALLATION_ID, event.id) is None
        assert repository.get_settlement_event(INSTALLATION_ID, str(uuid.uuid4())) is None

    def test_list_settlement_events_by_batch(
        self,
        repository: SqlAlchemySettlementRepository,
        channel_and_contract: tuple[str, str],
    ) -> None:
        channel_id, _ = channel_and_contract
        batch = str(uuid.uuid4())
        _save_event(repository, channel_id, batch_id=batch)
        _save_event(repository, channel_id, batch_id=str(uuid.uuid4()))
        assert len(repository.list_settlement_events(INSTALLATION_ID)) == 2
        by_batch = repository.list_settlement_events(
            INSTALLATION_ID, import_batch_id=batch
        )
        assert len(by_batch) == 1
        assert by_batch[0].import_batch_id == batch

    def test_get_bank_deposit(
        self, repository: SqlAlchemySettlementRepository
    ) -> None:
        deposit = _save_deposit(repository)
        found = repository.get_bank_deposit(INSTALLATION_ID, deposit.id)
        assert found is not None
        assert found.amount_irr == 22_250_000
        assert repository.get_bank_deposit(OTHER_INSTALLATION_ID, deposit.id) is None

    def test_list_bank_deposits_unmatched_only(
        self,
        repository: SqlAlchemySettlementRepository,
        channel_and_contract: tuple[str, str],
    ) -> None:
        channel_id, _, tranche = _setup_tranche(repository, channel_and_contract)
        event = _save_event(repository, channel_id)
        matched_deposit = _save_deposit(repository)
        unmatched_deposit = _save_deposit(
            repository, deposit_date=datetime.date(2026, 11, 28)
        )
        repository.save_settlement_match(
            CreateSettlementMatch(
                installation_id=INSTALLATION_ID,
                tranche_id=tranche.id,
                allocated_amount_irr=22_250_000,
                method="exact_reference",
                bank_deposit_id=matched_deposit.id,
                settlement_event_id=event.id,
            )
        )
        assert len(repository.list_bank_deposits(INSTALLATION_ID)) == 2
        unmatched = repository.list_bank_deposits(INSTALLATION_ID, unmatched_only=True)
        assert [d.id for d in unmatched] == [unmatched_deposit.id]


class TestMatches:
    def test_matches_by_deposit_and_event(
        self,
        repository: SqlAlchemySettlementRepository,
        channel_and_contract: tuple[str, str],
    ) -> None:
        channel_id, _, tranche = _setup_tranche(repository, channel_and_contract)
        event = _save_event(repository, channel_id)
        deposit = _save_deposit(repository)
        match = repository.save_settlement_match(
            CreateSettlementMatch(
                installation_id=INSTALLATION_ID,
                tranche_id=tranche.id,
                allocated_amount_irr=22_250_000,
                method="amount_and_date",
                bank_deposit_id=deposit.id,
                settlement_event_id=event.id,
                confidence=Decimal("0.95"),
            )
        )
        by_deposit = repository.list_matches_for_deposit(INSTALLATION_ID, deposit.id)
        assert [m.id for m in by_deposit] == [match.id]
        by_event = repository.list_matches_for_event(INSTALLATION_ID, event.id)
        assert [m.id for m in by_event] == [match.id]
        assert repository.list_matches_for_deposit(INSTALLATION_ID, str(uuid.uuid4())) == []

    def test_update_match_attaches_event(
        self,
        repository: SqlAlchemySettlementRepository,
        channel_and_contract: tuple[str, str],
    ) -> None:
        """C7: a reverse-matched link (tranche only) gains its event."""
        channel_id, _, tranche = _setup_tranche(repository, channel_and_contract)
        event = _save_event(repository, channel_id)
        deposit = _save_deposit(repository)
        match = repository.save_settlement_match(
            CreateSettlementMatch(
                installation_id=INSTALLATION_ID,
                tranche_id=tranche.id,
                allocated_amount_irr=22_250_000,
                method="amount_and_date",
                bank_deposit_id=deposit.id,
            )
        )
        assert match.settlement_event_id is None
        updated = repository.update_match(match.id, settlement_event_id=event.id)
        assert updated is not None
        assert updated.settlement_event_id == event.id
        assert updated.bank_deposit_id == deposit.id
        assert updated.allocated_amount_irr == 22_250_000
        assert len(repository.list_matches_for_event(INSTALLATION_ID, event.id)) == 1
        assert repository.update_match(str(uuid.uuid4()), settlement_event_id=event.id) is None


class TestOpenExceptions:
    def test_open_exception_for_deposit_and_event(
        self,
        repository: SqlAlchemySettlementRepository,
        channel_and_contract: tuple[str, str],
    ) -> None:
        channel_id, _, tranche = _setup_tranche(repository, channel_and_contract)
        event = _save_event(repository, channel_id)
        deposit = _save_deposit(repository)
        deposit_exc = repository.save_exception(
            CreateSettlementException(
                installation_id=INSTALLATION_ID,
                exception_type="deposit_without_file",
                severity="warning",
                bank_deposit_id=deposit.id,
            )
        )
        event_exc = repository.save_exception(
            CreateSettlementException(
                installation_id=INSTALLATION_ID,
                exception_type="file_without_deposit",
                severity="warning",
                settlement_event_id=event.id,
            )
        )
        found_deposit_exc = repository.get_open_exception_for_deposit(
            INSTALLATION_ID, deposit.id, "deposit_without_file"
        )
        assert found_deposit_exc is not None
        assert found_deposit_exc.id == deposit_exc.id
        found_event_exc = repository.get_open_exception_for_event(
            INSTALLATION_ID, event.id, "file_without_deposit"
        )
        assert found_event_exc is not None
        assert found_event_exc.id == event_exc.id
        # Another type / another target finds nothing.
        assert (
            repository.get_open_exception_for_deposit(
                INSTALLATION_ID, deposit.id, "overpayment"
            )
            is None
        )
        assert (
            repository.get_open_exception_for_event(
                INSTALLATION_ID, event.id, "amount_mismatch"
            )
            is None
        )
        # A resolved exception is no longer open.
        repository.update_exception(deposit_exc.id, status="resolved")
        assert (
            repository.get_open_exception_for_deposit(
                INSTALLATION_ID, deposit.id, "deposit_without_file"
            )
            is None
        )


class TestFileImports:
    def test_list_file_imports_filters(
        self,
        repository: SqlAlchemySettlementRepository,
        channel_and_contract: tuple[str, str],
    ) -> None:
        channel_id, _ = channel_and_contract
        first = _save_import(repository, channel_id)
        second = _save_import(repository, channel_id)
        repository.update_file_import(second.id, status="approved")
        assert len(repository.list_file_imports(INSTALLATION_ID)) == 2
        awaiting = repository.list_file_imports(INSTALLATION_ID, status="awaiting_review")
        assert awaiting == []
        imported = repository.list_file_imports(INSTALLATION_ID, status="imported")
        assert [i.id for i in imported] == [first.id]
        approved = repository.list_file_imports(INSTALLATION_ID, status="approved")
        assert [i.id for i in approved] == [second.id]
        assert (
            repository.list_file_imports(INSTALLATION_ID, channel_id=str(uuid.uuid4()))
            == []
        )
        assert repository.list_file_imports(OTHER_INSTALLATION_ID) == []

    def test_get_file_import_by_batch(
        self,
        repository: SqlAlchemySettlementRepository,
        channel_and_contract: tuple[str, str],
    ) -> None:
        channel_id, _ = channel_and_contract
        batch = str(uuid.uuid4())
        imp = _save_import(repository, channel_id, batch_id=batch)
        found = repository.get_file_import_by_batch(INSTALLATION_ID, batch)
        assert found is not None
        assert found.id == imp.id
        assert repository.get_file_import_by_batch(INSTALLATION_ID, str(uuid.uuid4())) is None
        assert repository.get_file_import_by_batch(OTHER_INSTALLATION_ID, batch) is None

    def test_update_file_import(
        self,
        repository: SqlAlchemySettlementRepository,
        channel_and_contract: tuple[str, str],
    ) -> None:
        channel_id, _ = channel_and_contract
        imp = _save_import(repository, channel_id)
        updated = repository.update_file_import(
            imp.id, status="awaiting_review", matched_count=3, error_count=1
        )
        assert updated is not None
        assert updated.status == "awaiting_review"
        assert updated.matched_count == 3
        assert updated.error_count == 1
        assert updated.row_count == 1  # untouched fields stay
        assert repository.update_file_import(str(uuid.uuid4()), status="approved") is None

    def test_update_file_row(
        self,
        repository: SqlAlchemySettlementRepository,
        channel_and_contract: tuple[str, str],
    ) -> None:
        channel_id, _, tranche = _setup_tranche(repository, channel_and_contract)
        imp = _save_import(repository, channel_id)
        rows = repository.save_file_rows(
            [
                CreateSettlementFileRow(
                    import_id=imp.id,
                    row_no=1,
                    raw_json={"txn": "TXN-1"},
                    gateway_transaction_id="TXN-1",
                    net_irr=22_250_000,
                )
            ]
        )
        updated = repository.update_file_row(
            rows[0].id,
            match_status="matched",
            matched_tranche_id=tranche.id,
            note="confirmed at approval",
        )
        assert updated is not None
        assert updated.match_status == "matched"
        assert updated.matched_tranche_id == tranche.id
        assert updated.note == "confirmed at approval"
        assert repository.update_file_row(str(uuid.uuid4()), match_status="matched") is None


class TestFeeRecordsAndTrancheStates:
    def test_fee_record_actuals_roundtrip(
        self,
        repository: SqlAlchemySettlementRepository,
        channel_and_contract: tuple[str, str],
    ) -> None:
        """The US-523 actual/variance columns persist and read back."""
        channel_id, contract_id = channel_and_contract
        payment = _save_payment(repository, channel_id, contract_id)
        record = repository.save_fee_record(
            installation_id=INSTALLATION_ID,
            gateway_payment_id=payment.id,
            contract_version_id=contract_id,
            fee_percent_applied=Decimal("10"),
            fee_vat_rate_applied=Decimal("10"),
            expected_fee_irr=2_500_000,
            expected_vat_irr=250_000,
            source="file",
            actual_fee_irr=3_000_000,
            fee_variance_irr=500_000,
            actual_vat_irr=None,
            vat_variance_irr=None,
        )
        found = repository.list_fee_records(
            INSTALLATION_ID, gateway_payment_id=payment.id
        )
        assert len(found) == 1
        assert found[0].id == record.id
        assert found[0].actual_fee_irr == 3_000_000
        assert found[0].fee_variance_irr == 500_000
        assert found[0].actual_vat_irr is None

    def test_list_tranches_states_filter(
        self,
        repository: SqlAlchemySettlementRepository,
        channel_and_contract: tuple[str, str],
    ) -> None:
        """The multi-state filter (batch matching) returns only those states."""
        channel_id, contract_id = channel_and_contract
        payment = _save_payment(repository, channel_id, contract_id)
        tranches = repository.save_tranches(
            INSTALLATION_ID, payment.id, _derived_tranches()
        )
        repository.update_tranche(tranches[0].id, state=TrancheState.DUE)
        repository.update_tranche(tranches[1].id, state=TrancheState.OVERDUE)
        due = repository.list_tranches(
            INSTALLATION_ID,
            channel_id=channel_id,
            states=[TrancheState.DUE, TrancheState.OVERDUE],
        )
        assert {t.id for t in due} == {tranches[0].id, tranches[1].id}
        expected = repository.list_tranches(
            INSTALLATION_ID,
            channel_id=channel_id,
            states=[TrancheState.EXPECTED],
        )
        assert {t.id for t in expected} == {tranches[2].id, tranches[3].id}
