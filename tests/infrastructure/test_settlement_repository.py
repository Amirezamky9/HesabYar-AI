"""Tests for the SQLAlchemy settlement repository (US-521).

Runs against the shared Supabase test DB with a transactional session
(rolled back after each test). Channels + contracts are created inside
the transaction via the integrations repository, so tests never depend
on rows committed by other modules. The services themselves are tested
DB-free against an in-memory fake (tests/unit/test_settlement_services.py);
this module verifies the repository implementation behind the port —
including the §3.2 unique constraints and the tick query.
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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from hesabyar.domain.integrations.commands import (
    CreateGatewayContract,
    CreatePaymentChannel,
)
from hesabyar.domain.integrations.models import TrancheLeg
from hesabyar.domain.settlement.commands import (
    CreateBankDeposit,
    CreateGatewayPayment,
    CreateSettlementEvent,
    CreateSettlementException,
    CreateSettlementFileImport,
    CreateSettlementFileRow,
    CreateSettlementMatch,
)
from hesabyar.domain.settlement.models import (
    DerivedTranche,
    PaymentSource,
    TrancheState,
)
from hesabyar.infrastructure.persistence.postgres.integrations_repository import (
    SqlAlchemyIntegrationsRepository,
)
from hesabyar.infrastructure.persistence.postgres.models import Base
from hesabyar.infrastructure.persistence.postgres.settlement_repository import (
    SqlAlchemySettlementRepository,
)

TEST_DB_URL = os.getenv(
    "HESABYAR_DATABASE_URL",
    "postgresql+psycopg://hesabyar:hesabyar_pass@hesabyar-postgres:5432/hesabyar_test",
)

INSTALLATION_ID = "00000000-0000-0000-0000-000000000521"
OTHER_INSTALLATION_ID = "00000000-0000-0000-0000-000000000529"
NOW = datetime.datetime(2026, 11, 6, 12, 0, tzinfo=datetime.UTC)

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


def _payment_command(txn: str = "TXN-1") -> CreateGatewayPayment:
    return CreateGatewayPayment(
        installation_id=INSTALLATION_ID,
        channel_key="snapppay",
        gateway_transaction_id=txn,
        gross_irr=100_000_000,
        gateway_confirmed_at=NOW,
        source=PaymentSource.STORE_WEBHOOK,
        store_order_id="ORD-1",
    )


def _save_payment(
    repository: SqlAlchemySettlementRepository, channel_id: str, contract_id: str, txn: str = "TXN-1"
):
    return repository.save_payment(
        _payment_command(txn),
        channel_id=channel_id,
        fee_irr=10_000_000,
        fee_vat_irr=1_000_000,
        net_irr=89_000_000,
        contract_version_id=contract_id,
        fee_rate_snapshot=Decimal("10"),
        fee_vat_rate_snapshot=Decimal("10"),
        sales_period_jalali="1405/08",
    )


#: (window start, due date) per tranche — 5 Azar / 5 Dey / 5 Bahman / 5 Esfand.
_TRANCHE_DATES = [
    (datetime.date(2026, 11, 22), datetime.date(2026, 11, 26)),
    (datetime.date(2026, 12, 22), datetime.date(2026, 12, 26)),
    (datetime.date(2027, 1, 21), datetime.date(2027, 1, 25)),
    (datetime.date(2027, 2, 20), datetime.date(2027, 2, 24)),
]


def _derived_tranches() -> list[DerivedTranche]:
    return [
        DerivedTranche(
            tranche_index=i + 1,
            tranche_count=4,
            sales_period_jalali="1405/08",
            expected_window_start=window_start,
            expected_date=due,
            gross_irr=25_000_000,
            fee_irr=2_500_000,
            fee_vat_irr=250_000,
            net_irr=22_250_000,
        )
        for i, (window_start, due) in enumerate(_TRANCHE_DATES)
    ]


class TestPayments:
    def test_save_and_get_roundtrip(
        self,
        repository: SqlAlchemySettlementRepository,
        channel_and_contract: tuple[str, str],
    ) -> None:
        channel_id, contract_id = channel_and_contract
        saved = _save_payment(repository, channel_id, contract_id)
        found = repository.get_payment(INSTALLATION_ID, saved.id)
        assert found is not None
        assert found.gateway_transaction_id == "TXN-1"
        assert found.gross_irr == 100_000_000
        assert found.fee_rate_snapshot == Decimal("10")
        assert found.status.value == "scheduled"
        assert found.store_order_id == "ORD-1"

    def test_other_installation_is_invisible(
        self,
        repository: SqlAlchemySettlementRepository,
        channel_and_contract: tuple[str, str],
    ) -> None:
        channel_id, contract_id = channel_and_contract
        saved = _save_payment(repository, channel_id, contract_id)
        assert repository.get_payment(OTHER_INSTALLATION_ID, saved.id) is None
        assert repository.list_payments(OTHER_INSTALLATION_ID) == []

    def test_duplicate_transaction_rejected(
        self,
        repository: SqlAlchemySettlementRepository,
        channel_and_contract: tuple[str, str],
        db: Session,
    ) -> None:
        channel_id, contract_id = channel_and_contract
        _save_payment(repository, channel_id, contract_id)
        with pytest.raises(IntegrityError):
            _save_payment(repository, channel_id, contract_id)
        db.rollback()

    def test_get_by_transaction(
        self,
        repository: SqlAlchemySettlementRepository,
        channel_and_contract: tuple[str, str],
    ) -> None:
        channel_id, contract_id = channel_and_contract
        saved = _save_payment(repository, channel_id, contract_id)
        found = repository.get_payment_by_transaction(INSTALLATION_ID, channel_id, "TXN-1")
        assert found is not None
        assert found.id == saved.id
        assert repository.get_payment_by_transaction(INSTALLATION_ID, channel_id, "TXN-9") is None

    def test_list_payments_filters(
        self,
        repository: SqlAlchemySettlementRepository,
        channel_and_contract: tuple[str, str],
    ) -> None:
        channel_id, contract_id = channel_and_contract
        _save_payment(repository, channel_id, contract_id, "TXN-1")
        _save_payment(repository, channel_id, contract_id, "TXN-2")
        assert len(repository.list_payments(INSTALLATION_ID)) == 2
        assert len(repository.list_payments(INSTALLATION_ID, status="settled")) == 0
        assert (
            len(repository.list_payments(INSTALLATION_ID, sales_period_jalali="1405/08")) == 2
        )
        assert (
            len(repository.list_payments(INSTALLATION_ID, sales_period_jalali="1405/09")) == 0
        )

    def test_update_payment_status_and_period(
        self,
        repository: SqlAlchemySettlementRepository,
        channel_and_contract: tuple[str, str],
    ) -> None:
        channel_id, contract_id = channel_and_contract
        saved = _save_payment(repository, channel_id, contract_id)
        updated = repository.update_payment_status(
            saved.id, "partially_cancelled", refunds_deducted_irr=22_250_000
        )
        assert updated is not None
        assert updated.status.value == "partially_cancelled"
        assert updated.refunds_deducted_irr == 22_250_000
        moved = repository.update_payment_sales_period(saved.id, "1405/09")
        assert moved is not None
        assert moved.sales_period_jalali == "1405/09"


class TestTranches:
    def test_save_and_list_ordered_by_index(
        self,
        repository: SqlAlchemySettlementRepository,
        channel_and_contract: tuple[str, str],
    ) -> None:
        channel_id, contract_id = channel_and_contract
        payment = _save_payment(repository, channel_id, contract_id)
        saved = repository.save_tranches(INSTALLATION_ID, payment.id, _derived_tranches())
        assert len(saved) == 4
        listed = repository.list_tranches_for_payment(INSTALLATION_ID, payment.id)
        assert [t.tranche_index for t in listed] == [1, 2, 3, 4]
        assert all(t.state == TrancheState.EXPECTED for t in listed)

    def test_duplicate_tranche_index_rejected(
        self,
        repository: SqlAlchemySettlementRepository,
        channel_and_contract: tuple[str, str],
        db: Session,
    ) -> None:
        channel_id, contract_id = channel_and_contract
        payment = _save_payment(repository, channel_id, contract_id)
        repository.save_tranches(INSTALLATION_ID, payment.id, _derived_tranches())
        with pytest.raises(IntegrityError):
            repository.save_tranches(INSTALLATION_ID, payment.id, _derived_tranches()[:1])
        db.rollback()

    def test_update_tranche_partial(
        self,
        repository: SqlAlchemySettlementRepository,
        channel_and_contract: tuple[str, str],
    ) -> None:
        channel_id, contract_id = channel_and_contract
        payment = _save_payment(repository, channel_id, contract_id)
        saved = repository.save_tranches(INSTALLATION_ID, payment.id, _derived_tranches())
        updated = repository.update_tranche(
            saved[0].id,
            state=TrancheState.DUE,
            confirmed_net_irr=10_000_000,
            variance_irr=-500,
            variance_reason="gateway short-paid",
        )
        assert updated is not None
        assert updated.state == TrancheState.DUE
        assert updated.confirmed_net_irr == 10_000_000
        assert updated.variance_irr == -500
        assert updated.net_irr == 22_250_000  # untouched fields stay

    def test_delete_tranches(
        self,
        repository: SqlAlchemySettlementRepository,
        channel_and_contract: tuple[str, str],
    ) -> None:
        channel_id, contract_id = channel_and_contract
        payment = _save_payment(repository, channel_id, contract_id)
        saved = repository.save_tranches(INSTALLATION_ID, payment.id, _derived_tranches())
        assert repository.delete_tranches([t.id for t in saved[:2]]) == 2
        assert len(repository.list_tranches_for_payment(INSTALLATION_ID, payment.id)) == 2
        assert repository.delete_tranches([]) == 0

    def test_list_tranches_filters(
        self,
        repository: SqlAlchemySettlementRepository,
        channel_and_contract: tuple[str, str],
    ) -> None:
        channel_id, contract_id = channel_and_contract
        payment = _save_payment(repository, channel_id, contract_id)
        saved = repository.save_tranches(INSTALLATION_ID, payment.id, _derived_tranches())
        repository.update_tranche(saved[0].id, state=TrancheState.DUE)
        assert len(repository.list_tranches(INSTALLATION_ID, state=TrancheState.DUE)) == 1
        assert len(repository.list_tranches(INSTALLATION_ID, state=TrancheState.EXPECTED)) == 3
        assert (
            len(
                repository.list_tranches(
                    INSTALLATION_ID,
                    due_from=datetime.date(2026, 12, 1),
                    due_to=datetime.date(2026, 12, 31),
                )
            )
            == 1
        )
        assert len(repository.list_tranches(INSTALLATION_ID, channel_id=channel_id)) == 4

    def test_find_tranches_due_for_tick(
        self,
        repository: SqlAlchemySettlementRepository,
        channel_and_contract: tuple[str, str],
    ) -> None:
        channel_id, contract_id = channel_and_contract
        payment = _save_payment(repository, channel_id, contract_id)
        saved = repository.save_tranches(INSTALLATION_ID, payment.id, _derived_tranches())
        repository.update_tranche(saved[1].id, state=TrancheState.DUE)
        repository.update_tranche(saved[2].id, state=TrancheState.SETTLED)
        # Due dates: 2026-11-26, 2026-12-26, 2027-01-25, 2027-02-24.
        candidates = repository.find_tranches_due_for_tick(datetime.date(2026, 12, 31))
        found_ids = {t.id for t, _ in candidates}
        assert saved[0].id in found_ids  # expected, due 2026-11-26
        assert saved[1].id in found_ids  # due, due 2026-12-26
        assert saved[2].id not in found_ids  # settled
        assert saved[3].id not in found_ids  # not due yet
        grace = dict((t.id, g) for t, g in candidates)
        assert grace[saved[0].id] == 3  # from the contract version


class TestEventsDepositsMatches:
    def test_event_deposit_match_roundtrip(
        self,
        repository: SqlAlchemySettlementRepository,
        channel_and_contract: tuple[str, str],
    ) -> None:
        channel_id, contract_id = channel_and_contract
        payment = _save_payment(repository, channel_id, contract_id)
        tranche = repository.save_tranches(
            INSTALLATION_ID, payment.id, _derived_tranches()[:1]
        )[0]
        event = repository.save_settlement_event(
            CreateSettlementEvent(
                installation_id=INSTALLATION_ID,
                channel_key="snapppay",
                payout_date=datetime.date(2026, 11, 27),
                net_irr=22_250_000,
                source="payouts_file",
                tracking_ref="TRK-1",
            ),
            channel_id=channel_id,
        )
        deposit = repository.save_bank_deposit(
            CreateBankDeposit(
                installation_id=INSTALLATION_ID,
                deposit_date=datetime.date(2026, 11, 27),
                amount_irr=22_250_000,
                tracking_ref="TRK-1",
            )
        )
        match = repository.save_settlement_match(
            CreateSettlementMatch(
                installation_id=INSTALLATION_ID,
                tranche_id=tranche.id,
                allocated_amount_irr=22_250_000,
                method="exact_reference",
                bank_deposit_id=deposit.id,
                settlement_event_id=event.id,
                confidence=Decimal("0.95"),
            )
        )
        assert match.tranche_id == tranche.id
        assert len(repository.list_matches_for_tranche(INSTALLATION_ID, tranche.id)) == 1
        assert len(repository.list_settlement_events(INSTALLATION_ID)) == 1
        assert len(repository.list_bank_deposits(INSTALLATION_ID)) == 1

    def test_match_without_target_rejected(
        self,
        repository: SqlAlchemySettlementRepository,
        channel_and_contract: tuple[str, str],
        db: Session,
    ) -> None:
        channel_id, contract_id = channel_and_contract
        payment = _save_payment(repository, channel_id, contract_id)
        tranche = repository.save_tranches(
            INSTALLATION_ID, payment.id, _derived_tranches()[:1]
        )[0]
        with pytest.raises(IntegrityError):
            repository.save_settlement_match(
                CreateSettlementMatch(
                    installation_id=INSTALLATION_ID,
                    tranche_id=tranche.id,
                    allocated_amount_irr=1_000,
                    method="manual",
                )
            )
        db.rollback()


class TestExceptionsAndFees:
    def test_exception_lifecycle(
        self,
        repository: SqlAlchemySettlementRepository,
        channel_and_contract: tuple[str, str],
    ) -> None:
        channel_id, contract_id = channel_and_contract
        payment = _save_payment(repository, channel_id, contract_id)
        tranche = repository.save_tranches(
            INSTALLATION_ID, payment.id, _derived_tranches()[:1]
        )[0]
        exc = repository.save_exception(
            CreateSettlementException(
                installation_id=INSTALLATION_ID,
                exception_type="late_tranche",
                severity="warning",
                tranche_id=tranche.id,
                expected_amount_irr=22_250_000,
                actual_amount_irr=0,
            )
        )
        assert exc.status.value == "open"
        open_exc = repository.get_open_exception_for_tranche(
            INSTALLATION_ID, tranche.id, "late_tranche"
        )
        assert open_exc is not None
        assert open_exc.id == exc.id
        resolved = repository.update_exception(
            exc.id,
            status="resolved",
            resolution="escalated_to_gateway",
            resolved_by="ops",
            resolved_at=datetime.datetime.now(datetime.UTC),
            note="called the gateway",
        )
        assert resolved is not None
        assert resolved.status.value == "resolved"
        assert repository.get_open_exception_for_tranche(
            INSTALLATION_ID, tranche.id, "late_tranche"
        ) is None

    def test_fee_record_roundtrip(
        self,
        repository: SqlAlchemySettlementRepository,
        channel_and_contract: tuple[str, str],
    ) -> None:
        channel_id, contract_id = channel_and_contract
        payment = _save_payment(repository, channel_id, contract_id)
        record = repository.save_fee_record(
            installation_id=INSTALLATION_ID,
            gateway_payment_id=payment.id,
            contract_version_id=contract_id,
            fee_percent_applied=Decimal("10"),
            fee_vat_rate_applied=Decimal("10"),
            expected_fee_irr=10_000_000,
            expected_vat_irr=1_000_000,
        )
        assert record.expected_fee_irr == 10_000_000
        assert len(repository.list_fee_records(INSTALLATION_ID)) == 1
        assert (
            len(
                repository.list_fee_records(
                    INSTALLATION_ID, gateway_payment_id=payment.id
                )
            )
            == 1
        )
        assert len(repository.list_fee_records(INSTALLATION_ID, channel_id=channel_id)) == 1


class TestFileImports:
    def test_import_and_rows_roundtrip(
        self,
        repository: SqlAlchemySettlementRepository,
        channel_and_contract: tuple[str, str],
    ) -> None:
        channel_id, _ = channel_and_contract
        imp = repository.save_file_import(
            CreateSettlementFileImport(
                installation_id=INSTALLATION_ID,
                channel_key="snapppay",
                file_type="payouts",
                period_jalali="1405/09",
                file_hash_sha256="a" * 64,
                import_batch_id=str(uuid.uuid4()),
                uploaded_by="ops",
                row_count=2,
            ),
            channel_id=channel_id,
        )
        assert repository.get_file_import_by_hash(INSTALLATION_ID, channel_id, "a" * 64) is not None
        rows = repository.save_file_rows(
            [
                CreateSettlementFileRow(
                    import_id=imp.id,
                    row_no=1,
                    raw_json={"txn": "TXN-1", "net": "22250000"},
                    gateway_transaction_id="TXN-1",
                    net_irr=22_250_000,
                    match_status="matched",
                ),
                CreateSettlementFileRow(
                    import_id=imp.id,
                    row_no=2,
                    raw_json={"txn": "TXN-9"},
                    gateway_transaction_id="TXN-9",
                ),
            ]
        )
        assert len(rows) == 2
        listed = repository.list_file_rows(imp.id)
        assert [r.row_no for r in listed] == [1, 2]
        assert listed[0].raw_json["net"] == "22250000"

    def test_duplicate_file_hash_rejected(
        self,
        repository: SqlAlchemySettlementRepository,
        channel_and_contract: tuple[str, str],
        db: Session,
    ) -> None:
        channel_id, _ = channel_and_contract
        command = CreateSettlementFileImport(
            installation_id=INSTALLATION_ID,
            channel_key="snapppay",
            file_type="payouts",
            period_jalali="1405/09",
            file_hash_sha256="b" * 64,
            import_batch_id=str(uuid.uuid4()),
        )
        repository.save_file_import(command, channel_id=channel_id)
        with pytest.raises(IntegrityError):
            repository.save_file_import(command, channel_id=channel_id)
        db.rollback()
