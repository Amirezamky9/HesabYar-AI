"""SQLAlchemy implementation of the settlement repository port (US-521).

Implements :class:`hesabyar.domain.settlement.ports.SettlementRepositoryPort`
over the ``gateway_payments`` / ``settlement_tranches`` /
``settlement_events`` / ``bank_deposits`` / ``settlement_matches`` /
``settlement_exceptions`` / ``settlement_fee_records`` /
``settlement_file_imports`` / ``settlement_file_rows`` tables. The
repository speaks domain types only at its boundary: ORM rows are
mapped to domain dataclasses here, so the application layer never
sees SQLAlchemy.

Every read/write is scoped by ``installation_id`` (the caller's
installation, from the authenticated API key): a row of another
installation looks exactly like a row that does not exist. The tick
query is the exception — the daily job runs across ALL installations.

The repository flushes on save (so ids exist) but never commits —
the caller owns the transaction.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from typing import Any

from sqlalchemy import delete, exists, select
from sqlalchemy.orm import Session

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
    BankDeposit,
    DerivedTranche,
    GatewayPayment,
    GatewayPaymentStatus,
    PaymentSource,
    SettlementEvent,
    SettlementException,
    SettlementExceptionResolution,
    SettlementExceptionSeverity,
    SettlementExceptionStatus,
    SettlementExceptionType,
    SettlementFeeRecord,
    SettlementFileImport,
    SettlementFileRow,
    SettlementMatch,
    SettlementPosting,
    SettlementPostingKind,
    SettlementPostingStatus,
    SettlementTranche,
    TrancheState,
)
from hesabyar.infrastructure.persistence.postgres.models import (
    BankDepositModel,
    GatewayContractModel,
    GatewayPaymentModel,
    SettlementEventModel,
    SettlementExceptionModel,
    SettlementFeeRecordModel,
    SettlementFileImportModel,
    SettlementFileRowModel,
    SettlementMatchModel,
    SettlementPostingModel,
    SettlementTrancheModel,
)


def _payment_from_row(row: GatewayPaymentModel) -> GatewayPayment:
    return GatewayPayment(
        id=row.id,
        installation_id=row.installation_id,
        payment_channel_id=row.payment_channel_id,
        gateway_transaction_id=row.gateway_transaction_id,
        gross_irr=row.gross_irr,
        fee_irr=row.fee_irr,
        fee_vat_irr=row.fee_vat_irr,
        net_irr=row.net_irr,
        contract_version_id=row.contract_version_id,
        fee_rate_snapshot=row.fee_rate_snapshot,
        sales_period_jalali=row.sales_period_jalali,
        gateway_confirmed_at=row.gateway_confirmed_at,
        source=PaymentSource(row.source),
        status=GatewayPaymentStatus(row.status),
        store_order_id=row.store_order_id,
        store_payment_id=row.store_payment_id,
        tryton_invoice_id=row.tryton_invoice_id,
        tryton_journal_id=row.tryton_journal_id,
        customer_party_id=row.customer_party_id,
        fee_vat_rate_snapshot=row.fee_vat_rate_snapshot,
        canonical_event_id=row.canonical_event_id,
        refunds_deducted_irr=row.refunds_deducted_irr,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _tranche_from_row(row: SettlementTrancheModel) -> SettlementTranche:
    return SettlementTranche(
        id=row.id,
        installation_id=row.installation_id,
        gateway_payment_id=row.gateway_payment_id,
        tranche_index=row.tranche_index,
        tranche_count=row.tranche_count,
        sales_period_jalali=row.sales_period_jalali,
        expected_window_start=row.expected_window_start,
        expected_date=row.expected_date,
        gross_irr=row.gross_irr,
        fee_irr=row.fee_irr,
        fee_vat_irr=row.fee_vat_irr,
        net_irr=row.net_irr,
        state=TrancheState(row.state),
        confirmed_net_irr=row.confirmed_net_irr,
        received_net_irr=row.received_net_irr,
        tryton_receivable_line_id=row.tryton_receivable_line_id,
        variance_irr=row.variance_irr,
        variance_reason=row.variance_reason,
        match_method=row.match_method,
        match_confidence=row.match_confidence,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _event_from_row(row: SettlementEventModel) -> SettlementEvent:
    return SettlementEvent(
        id=row.id,
        installation_id=row.installation_id,
        payment_channel_id=row.payment_channel_id,
        payout_date=row.payout_date,
        net_irr=row.net_irr,
        source=row.source,
        deductions_irr=row.deductions_irr,
        tracking_ref=row.tracking_ref,
        destination_iban=row.destination_iban,
        gateway_batch_ref=row.gateway_batch_ref,
        import_batch_id=row.import_batch_id,
        canonical_event_id=row.canonical_event_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _deposit_from_row(row: BankDepositModel) -> BankDeposit:
    return BankDeposit(
        id=row.id,
        installation_id=row.installation_id,
        deposit_date=row.deposit_date,
        amount_irr=row.amount_irr,
        bank_account_ref=row.bank_account_ref,
        description=row.description,
        tracking_ref=row.tracking_ref,
        bank_import_row_id=row.bank_import_row_id,
        source=row.source,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _match_from_row(row: SettlementMatchModel) -> SettlementMatch:
    return SettlementMatch(
        id=row.id,
        installation_id=row.installation_id,
        tranche_id=row.tranche_id,
        allocated_amount_irr=row.allocated_amount_irr,
        method=row.method,
        bank_deposit_id=row.bank_deposit_id,
        settlement_event_id=row.settlement_event_id,
        confidence=row.confidence,
        matched_by=row.matched_by,
        is_automatic=row.is_automatic,
        matched_at=row.matched_at,
        created_at=row.created_at,
    )


def _exception_from_row(row: SettlementExceptionModel) -> SettlementException:
    return SettlementException(
        id=row.id,
        installation_id=row.installation_id,
        exception_type=SettlementExceptionType(row.exception_type),
        severity=SettlementExceptionSeverity(row.severity),
        status=SettlementExceptionStatus(row.status),
        tranche_id=row.tranche_id,
        settlement_event_id=row.settlement_event_id,
        bank_deposit_id=row.bank_deposit_id,
        gateway_payment_id=row.gateway_payment_id,
        expected_amount_irr=row.expected_amount_irr,
        actual_amount_irr=row.actual_amount_irr,
        escalation_level=row.escalation_level,
        resolution=(
            SettlementExceptionResolution(row.resolution) if row.resolution else None
        ),
        resolved_by=row.resolved_by,
        resolved_at=row.resolved_at,
        note=row.note,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _fee_record_from_row(row: SettlementFeeRecordModel) -> SettlementFeeRecord:
    return SettlementFeeRecord(
        id=row.id,
        installation_id=row.installation_id,
        gateway_payment_id=row.gateway_payment_id,
        contract_version_id=row.contract_version_id,
        fee_percent_applied=row.fee_percent_applied,
        expected_fee_irr=row.expected_fee_irr,
        expected_vat_irr=row.expected_vat_irr,
        source=row.source,
        tranche_id=row.tranche_id,
        fee_vat_rate_applied=row.fee_vat_rate_applied,
        actual_fee_irr=row.actual_fee_irr,
        fee_variance_irr=row.fee_variance_irr,
        actual_vat_irr=row.actual_vat_irr,
        vat_variance_irr=row.vat_variance_irr,
        tryton_fee_journal_id=row.tryton_fee_journal_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _file_import_from_row(row: SettlementFileImportModel) -> SettlementFileImport:
    return SettlementFileImport(
        id=row.id,
        installation_id=row.installation_id,
        payment_channel_id=row.payment_channel_id,
        file_type=row.file_type,
        period_jalali=row.period_jalali,
        file_hash_sha256=row.file_hash_sha256,
        import_batch_id=row.import_batch_id,
        status=row.status,
        uploaded_by=row.uploaded_by,
        uploaded_at=row.uploaded_at,
        row_count=row.row_count,
        matched_count=row.matched_count,
        new_count=row.new_count,
        error_count=row.error_count,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _file_row_from_row(row: SettlementFileRowModel) -> SettlementFileRow:
    return SettlementFileRow(
        id=row.id,
        import_id=row.import_id,
        row_no=row.row_no,
        raw_json=dict(row.raw_json or {}),
        match_status=row.match_status,
        gateway_transaction_id=row.gateway_transaction_id,
        tran_type=row.tran_type,
        amount_irr=row.amount_irr,
        fee_irr=row.fee_irr,
        net_irr=row.net_irr,
        transaction_date=row.transaction_date,
        tracking_ref=row.tracking_ref,
        matched_tranche_id=row.matched_tranche_id,
        note=row.note,
        created_at=row.created_at,
    )


def _posting_from_row(row: SettlementPostingModel) -> SettlementPosting:
    return SettlementPosting(
        id=row.id,
        installation_id=row.installation_id,
        gateway_payment_id=row.gateway_payment_id,
        kind=SettlementPostingKind(row.kind),
        amount_irr=row.amount_irr,
        status=SettlementPostingStatus(row.status),
        tranche_id=row.tranche_id,
        tryton_move_id=row.tryton_move_id,
        tryton_payment_id=row.tryton_payment_id,
        tryton_reconciliation_id=row.tryton_reconciliation_id,
        reason=row.reason,
        event_ref=row.event_ref,
        details=dict(row.details) if row.details is not None else None,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlAlchemySettlementRepository:
    """SQLAlchemy implementation of the settlement repository port."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- gateway payments -------------------------------------------------

    def get_payment(self, installation_id: str, payment_id: str) -> GatewayPayment | None:
        stmt = select(GatewayPaymentModel).where(
            GatewayPaymentModel.installation_id == installation_id,
            GatewayPaymentModel.id == payment_id,
        )
        row = self._session.execute(stmt).scalar_one_or_none()
        return _payment_from_row(row) if row is not None else None

    def get_payment_by_transaction(
        self, installation_id: str, channel_id: str, gateway_transaction_id: str
    ) -> GatewayPayment | None:
        stmt = select(GatewayPaymentModel).where(
            GatewayPaymentModel.installation_id == installation_id,
            GatewayPaymentModel.payment_channel_id == channel_id,
            GatewayPaymentModel.gateway_transaction_id == gateway_transaction_id,
        )
        row = self._session.execute(stmt).scalar_one_or_none()
        return _payment_from_row(row) if row is not None else None

    def list_payments(
        self,
        installation_id: str,
        *,
        channel_id: str | None = None,
        status: str | None = None,
        sales_period_jalali: str | None = None,
    ) -> Sequence[GatewayPayment]:
        stmt = select(GatewayPaymentModel).where(
            GatewayPaymentModel.installation_id == installation_id
        )
        if channel_id is not None:
            stmt = stmt.where(GatewayPaymentModel.payment_channel_id == channel_id)
        if status is not None:
            stmt = stmt.where(GatewayPaymentModel.status == status)
        if sales_period_jalali is not None:
            stmt = stmt.where(GatewayPaymentModel.sales_period_jalali == sales_period_jalali)
        stmt = stmt.order_by(
            GatewayPaymentModel.gateway_confirmed_at.desc(), GatewayPaymentModel.id.desc()
        )
        return [_payment_from_row(row) for row in self._session.execute(stmt).scalars().all()]

    def save_payment(
        self,
        command: CreateGatewayPayment,
        *,
        channel_id: str,
        fee_irr: int,
        fee_vat_irr: int,
        net_irr: int,
        contract_version_id: str,
        fee_rate_snapshot: Any,
        fee_vat_rate_snapshot: Any,
        sales_period_jalali: str,
    ) -> GatewayPayment:
        row = GatewayPaymentModel(
            installation_id=command.installation_id,
            payment_channel_id=channel_id,
            gateway_transaction_id=command.gateway_transaction_id,
            gross_irr=command.gross_irr,
            fee_irr=fee_irr,
            fee_vat_irr=fee_vat_irr,
            net_irr=net_irr,
            contract_version_id=contract_version_id,
            fee_rate_snapshot=fee_rate_snapshot,
            fee_vat_rate_snapshot=fee_vat_rate_snapshot,
            sales_period_jalali=sales_period_jalali,
            gateway_confirmed_at=command.gateway_confirmed_at,
            source=command.source.value,
            store_order_id=command.store_order_id,
            store_payment_id=command.store_payment_id,
            tryton_invoice_id=command.tryton_invoice_id,
            tryton_journal_id=command.tryton_journal_id,
            customer_party_id=command.customer_party_id,
            canonical_event_id=command.canonical_event_id,
        )
        self._session.add(row)
        self._session.flush()
        return _payment_from_row(row)

    def update_payment_status(
        self, payment_id: str, status: str, *, refunds_deducted_irr: int | None = None
    ) -> GatewayPayment | None:
        row = self._session.get(GatewayPaymentModel, payment_id)
        if row is None:
            return None
        row.status = status
        if refunds_deducted_irr is not None:
            row.refunds_deducted_irr = refunds_deducted_irr
        self._session.flush()
        return _payment_from_row(row)

    def update_payment_sales_period(
        self, payment_id: str, sales_period_jalali: str
    ) -> GatewayPayment | None:
        row = self._session.get(GatewayPaymentModel, payment_id)
        if row is None:
            return None
        row.sales_period_jalali = sales_period_jalali
        self._session.flush()
        return _payment_from_row(row)

    def update_payment_amounts(
        self,
        payment_id: str,
        *,
        fee_irr: int,
        fee_vat_irr: int,
        net_irr: int,
        contract_version_id: str,
        fee_rate_snapshot: Any,
        fee_vat_rate_snapshot: Any,
    ) -> GatewayPayment | None:
        row = self._session.get(GatewayPaymentModel, payment_id)
        if row is None:
            return None
        row.fee_irr = fee_irr
        row.fee_vat_irr = fee_vat_irr
        row.net_irr = net_irr
        row.contract_version_id = contract_version_id
        row.fee_rate_snapshot = fee_rate_snapshot
        row.fee_vat_rate_snapshot = fee_vat_rate_snapshot
        self._session.flush()
        return _payment_from_row(row)

    # -- tranches ---------------------------------------------------------

    def get_tranche(self, installation_id: str, tranche_id: str) -> SettlementTranche | None:
        stmt = select(SettlementTrancheModel).where(
            SettlementTrancheModel.installation_id == installation_id,
            SettlementTrancheModel.id == tranche_id,
        )
        row = self._session.execute(stmt).scalar_one_or_none()
        return _tranche_from_row(row) if row is not None else None

    def list_tranches_for_payment(
        self, installation_id: str, payment_id: str
    ) -> Sequence[SettlementTranche]:
        stmt = (
            select(SettlementTrancheModel)
            .where(
                SettlementTrancheModel.installation_id == installation_id,
                SettlementTrancheModel.gateway_payment_id == payment_id,
            )
            .order_by(SettlementTrancheModel.tranche_index)
        )
        return [_tranche_from_row(row) for row in self._session.execute(stmt).scalars().all()]

    def list_tranches(
        self,
        installation_id: str,
        *,
        channel_id: str | None = None,
        state: TrancheState | None = None,
        states: Sequence[TrancheState] | None = None,
        sales_period_jalali: str | None = None,
        due_from: datetime.date | None = None,
        due_to: datetime.date | None = None,
    ) -> Sequence[SettlementTranche]:
        stmt = select(SettlementTrancheModel).where(
            SettlementTrancheModel.installation_id == installation_id
        )
        if channel_id is not None:
            stmt = stmt.join(
                GatewayPaymentModel,
                SettlementTrancheModel.gateway_payment_id == GatewayPaymentModel.id,
            ).where(GatewayPaymentModel.payment_channel_id == channel_id)
        if states is not None:
            stmt = stmt.where(
                SettlementTrancheModel.state.in_([item.value for item in states])
            )
        elif state is not None:
            stmt = stmt.where(SettlementTrancheModel.state == state.value)
        if sales_period_jalali is not None:
            stmt = stmt.where(SettlementTrancheModel.sales_period_jalali == sales_period_jalali)
        if due_from is not None:
            stmt = stmt.where(SettlementTrancheModel.expected_date >= due_from)
        if due_to is not None:
            stmt = stmt.where(SettlementTrancheModel.expected_date <= due_to)
        stmt = stmt.order_by(
            SettlementTrancheModel.expected_date, SettlementTrancheModel.tranche_index
        )
        return [_tranche_from_row(row) for row in self._session.execute(stmt).scalars().all()]

    def save_tranches(
        self,
        installation_id: str,
        payment_id: str,
        tranches: Sequence[DerivedTranche],
    ) -> Sequence[SettlementTranche]:
        rows: list[SettlementTrancheModel] = []
        for derived in tranches:
            row = SettlementTrancheModel(
                installation_id=installation_id,
                gateway_payment_id=payment_id,
                tranche_index=derived.tranche_index,
                tranche_count=derived.tranche_count,
                sales_period_jalali=derived.sales_period_jalali,
                expected_window_start=derived.expected_window_start,
                expected_date=derived.expected_date,
                gross_irr=derived.gross_irr,
                fee_irr=derived.fee_irr,
                fee_vat_irr=derived.fee_vat_irr,
                net_irr=derived.net_irr,
            )
            self._session.add(row)
            rows.append(row)
        self._session.flush()
        return [_tranche_from_row(row) for row in rows]

    def update_tranche(
        self,
        tranche_id: str,
        *,
        state: TrancheState | None = None,
        confirmed_net_irr: int | None = None,
        received_net_irr: int | None = None,
        variance_irr: int | None = None,
        variance_reason: str | None = None,
        match_method: str | None = None,
        match_confidence: Any | None = None,
        tryton_receivable_line_id: int | None = None,
        expected_window_start: datetime.date | None = None,
        expected_date: datetime.date | None = None,
        sales_period_jalali: str | None = None,
        gross_irr: int | None = None,
        fee_irr: int | None = None,
        fee_vat_irr: int | None = None,
        net_irr: int | None = None,
    ) -> SettlementTranche | None:
        row = self._session.get(SettlementTrancheModel, tranche_id)
        if row is None:
            return None
        if state is not None:
            row.state = state.value
        if confirmed_net_irr is not None:
            row.confirmed_net_irr = confirmed_net_irr
        if received_net_irr is not None:
            row.received_net_irr = received_net_irr
        if variance_irr is not None:
            row.variance_irr = variance_irr
        if variance_reason is not None:
            row.variance_reason = variance_reason
        if match_method is not None:
            row.match_method = match_method
        if match_confidence is not None:
            row.match_confidence = match_confidence
        if tryton_receivable_line_id is not None:
            row.tryton_receivable_line_id = tryton_receivable_line_id
        if expected_window_start is not None:
            row.expected_window_start = expected_window_start
        if expected_date is not None:
            row.expected_date = expected_date
        if sales_period_jalali is not None:
            row.sales_period_jalali = sales_period_jalali
        if gross_irr is not None:
            row.gross_irr = gross_irr
        if fee_irr is not None:
            row.fee_irr = fee_irr
        if fee_vat_irr is not None:
            row.fee_vat_irr = fee_vat_irr
        if net_irr is not None:
            row.net_irr = net_irr
        self._session.flush()
        return _tranche_from_row(row)

    def delete_tranches(self, tranche_ids: Sequence[str]) -> int:
        if not tranche_ids:
            return 0
        ids = list(tranche_ids)
        existing = self._session.execute(
            select(SettlementTrancheModel.id).where(SettlementTrancheModel.id.in_(ids))
        ).scalars().all()
        self._session.execute(
            delete(SettlementTrancheModel).where(SettlementTrancheModel.id.in_(ids))
        )
        self._session.flush()
        return len(existing)

    def find_tranches_due_for_tick(
        self, today: datetime.date
    ) -> Sequence[tuple[SettlementTranche, int]]:
        """Tick candidates across ALL installations.

        ``expected``/``due`` tranches whose due date has arrived, with
        the grace days of the contract version the payment was built
        from. The service decides expected→due vs due→overdue from
        the grace — the query only narrows the candidate set.
        """
        stmt = (
            select(SettlementTrancheModel, GatewayContractModel.grace_days_after_due)
            .join(
                GatewayPaymentModel,
                SettlementTrancheModel.gateway_payment_id == GatewayPaymentModel.id,
            )
            .join(
                GatewayContractModel,
                GatewayPaymentModel.contract_version_id == GatewayContractModel.id,
            )
            .where(
                SettlementTrancheModel.state.in_(["expected", "due"]),
                SettlementTrancheModel.expected_date <= today,
            )
            .order_by(SettlementTrancheModel.expected_date, SettlementTrancheModel.id)
        )
        return [
            (_tranche_from_row(row), grace)
            for row, grace in self._session.execute(stmt).all()
        ]

    # -- settlement events / deposits / matches ---------------------------

    def save_settlement_event(
        self, command: CreateSettlementEvent, *, channel_id: str
    ) -> SettlementEvent:
        row = SettlementEventModel(
            installation_id=command.installation_id,
            payment_channel_id=channel_id,
            payout_date=command.payout_date,
            net_irr=command.net_irr,
            source=command.source,
            deductions_irr=command.deductions_irr,
            tracking_ref=command.tracking_ref,
            destination_iban=command.destination_iban,
            gateway_batch_ref=command.gateway_batch_ref,
            import_batch_id=command.import_batch_id,
            canonical_event_id=command.canonical_event_id,
        )
        self._session.add(row)
        self._session.flush()
        return _event_from_row(row)

    def get_settlement_event(
        self, installation_id: str, event_id: str
    ) -> SettlementEvent | None:
        stmt = select(SettlementEventModel).where(
            SettlementEventModel.installation_id == installation_id,
            SettlementEventModel.id == event_id,
        )
        row = self._session.execute(stmt).scalar_one_or_none()
        return _event_from_row(row) if row is not None else None

    def list_settlement_events(
        self,
        installation_id: str,
        *,
        channel_id: str | None = None,
        import_batch_id: str | None = None,
    ) -> Sequence[SettlementEvent]:
        stmt = select(SettlementEventModel).where(
            SettlementEventModel.installation_id == installation_id
        )
        if channel_id is not None:
            stmt = stmt.where(SettlementEventModel.payment_channel_id == channel_id)
        if import_batch_id is not None:
            stmt = stmt.where(SettlementEventModel.import_batch_id == import_batch_id)
        stmt = stmt.order_by(
            SettlementEventModel.payout_date.desc(), SettlementEventModel.id.desc()
        )
        return [_event_from_row(row) for row in self._session.execute(stmt).scalars().all()]

    def save_bank_deposit(self, command: CreateBankDeposit) -> BankDeposit:
        row = BankDepositModel(
            installation_id=command.installation_id,
            deposit_date=command.deposit_date,
            amount_irr=command.amount_irr,
            bank_account_ref=command.bank_account_ref,
            description=command.description,
            tracking_ref=command.tracking_ref,
            bank_import_row_id=command.bank_import_row_id,
            source=command.source,
        )
        self._session.add(row)
        self._session.flush()
        return _deposit_from_row(row)

    def get_bank_deposit(
        self, installation_id: str, deposit_id: str
    ) -> BankDeposit | None:
        stmt = select(BankDepositModel).where(
            BankDepositModel.installation_id == installation_id,
            BankDepositModel.id == deposit_id,
        )
        row = self._session.execute(stmt).scalar_one_or_none()
        return _deposit_from_row(row) if row is not None else None

    def list_bank_deposits(
        self, installation_id: str, *, unmatched_only: bool = False
    ) -> Sequence[BankDeposit]:
        stmt = select(BankDepositModel).where(
            BankDepositModel.installation_id == installation_id
        )
        if unmatched_only:
            stmt = stmt.where(
                ~exists().where(
                    SettlementMatchModel.bank_deposit_id == BankDepositModel.id
                )
            )
        stmt = stmt.order_by(
            BankDepositModel.deposit_date.desc(), BankDepositModel.id.desc()
        )
        return [_deposit_from_row(row) for row in self._session.execute(stmt).scalars().all()]

    def save_settlement_match(self, command: CreateSettlementMatch) -> SettlementMatch:
        row = SettlementMatchModel(
            installation_id=command.installation_id,
            tranche_id=command.tranche_id,
            allocated_amount_irr=command.allocated_amount_irr,
            method=command.method,
            bank_deposit_id=command.bank_deposit_id,
            settlement_event_id=command.settlement_event_id,
            confidence=command.confidence,
            matched_by=command.matched_by,
            is_automatic=command.is_automatic,
            matched_at=datetime.datetime.now(datetime.UTC),
        )
        self._session.add(row)
        self._session.flush()
        return _match_from_row(row)

    def update_match(
        self, match_id: str, *, settlement_event_id: str | None = None
    ) -> SettlementMatch | None:
        row = self._session.get(SettlementMatchModel, match_id)
        if row is None:
            return None
        if settlement_event_id is not None:
            row.settlement_event_id = settlement_event_id
        self._session.flush()
        return _match_from_row(row)

    def list_matches_for_tranche(
        self, installation_id: str, tranche_id: str
    ) -> Sequence[SettlementMatch]:
        stmt = (
            select(SettlementMatchModel)
            .where(
                SettlementMatchModel.installation_id == installation_id,
                SettlementMatchModel.tranche_id == tranche_id,
            )
            .order_by(SettlementMatchModel.created_at, SettlementMatchModel.id)
        )
        return [_match_from_row(row) for row in self._session.execute(stmt).scalars().all()]

    def list_matches_for_deposit(
        self, installation_id: str, deposit_id: str
    ) -> Sequence[SettlementMatch]:
        stmt = (
            select(SettlementMatchModel)
            .where(
                SettlementMatchModel.installation_id == installation_id,
                SettlementMatchModel.bank_deposit_id == deposit_id,
            )
            .order_by(SettlementMatchModel.created_at, SettlementMatchModel.id)
        )
        return [_match_from_row(row) for row in self._session.execute(stmt).scalars().all()]

    def list_matches_for_event(
        self, installation_id: str, settlement_event_id: str
    ) -> Sequence[SettlementMatch]:
        stmt = (
            select(SettlementMatchModel)
            .where(
                SettlementMatchModel.installation_id == installation_id,
                SettlementMatchModel.settlement_event_id == settlement_event_id,
            )
            .order_by(SettlementMatchModel.created_at, SettlementMatchModel.id)
        )
        return [_match_from_row(row) for row in self._session.execute(stmt).scalars().all()]

    # -- exceptions ---------------------------------------------------------

    def save_exception(self, command: CreateSettlementException) -> SettlementException:
        row = SettlementExceptionModel(
            installation_id=command.installation_id,
            exception_type=command.exception_type,
            severity=command.severity,
            tranche_id=command.tranche_id,
            settlement_event_id=command.settlement_event_id,
            bank_deposit_id=command.bank_deposit_id,
            gateway_payment_id=command.gateway_payment_id,
            expected_amount_irr=command.expected_amount_irr,
            actual_amount_irr=command.actual_amount_irr,
            escalation_level=command.escalation_level,
            note=command.note,
        )
        self._session.add(row)
        self._session.flush()
        return _exception_from_row(row)

    def get_exception(
        self, installation_id: str, exception_id: str
    ) -> SettlementException | None:
        stmt = select(SettlementExceptionModel).where(
            SettlementExceptionModel.installation_id == installation_id,
            SettlementExceptionModel.id == exception_id,
        )
        row = self._session.execute(stmt).scalar_one_or_none()
        return _exception_from_row(row) if row is not None else None

    def list_exceptions(
        self,
        installation_id: str,
        *,
        exception_type: str | None = None,
        status: str | None = None,
        tranche_id: str | None = None,
    ) -> Sequence[SettlementException]:
        stmt = select(SettlementExceptionModel).where(
            SettlementExceptionModel.installation_id == installation_id
        )
        if exception_type is not None:
            stmt = stmt.where(SettlementExceptionModel.exception_type == exception_type)
        if status is not None:
            stmt = stmt.where(SettlementExceptionModel.status == status)
        if tranche_id is not None:
            stmt = stmt.where(SettlementExceptionModel.tranche_id == tranche_id)
        stmt = stmt.order_by(
            SettlementExceptionModel.created_at.desc(), SettlementExceptionModel.id.desc()
        )
        return [_exception_from_row(row) for row in self._session.execute(stmt).scalars().all()]

    def get_open_exception_for_tranche(
        self, installation_id: str, tranche_id: str, exception_type: str
    ) -> SettlementException | None:
        stmt = (
            select(SettlementExceptionModel)
            .where(
                SettlementExceptionModel.installation_id == installation_id,
                SettlementExceptionModel.tranche_id == tranche_id,
                SettlementExceptionModel.exception_type == exception_type,
                SettlementExceptionModel.status.in_(["open", "in_review"]),
            )
            .order_by(SettlementExceptionModel.created_at.desc())
        )
        row = self._session.execute(stmt).scalars().first()
        return _exception_from_row(row) if row is not None else None

    def get_open_exception_for_deposit(
        self, installation_id: str, deposit_id: str, exception_type: str
    ) -> SettlementException | None:
        stmt = (
            select(SettlementExceptionModel)
            .where(
                SettlementExceptionModel.installation_id == installation_id,
                SettlementExceptionModel.bank_deposit_id == deposit_id,
                SettlementExceptionModel.exception_type == exception_type,
                SettlementExceptionModel.status.in_(["open", "in_review"]),
            )
            .order_by(SettlementExceptionModel.created_at.desc())
        )
        row = self._session.execute(stmt).scalars().first()
        return _exception_from_row(row) if row is not None else None

    def get_open_exception_for_event(
        self, installation_id: str, settlement_event_id: str, exception_type: str
    ) -> SettlementException | None:
        stmt = (
            select(SettlementExceptionModel)
            .where(
                SettlementExceptionModel.installation_id == installation_id,
                SettlementExceptionModel.settlement_event_id == settlement_event_id,
                SettlementExceptionModel.exception_type == exception_type,
                SettlementExceptionModel.status.in_(["open", "in_review"]),
            )
            .order_by(SettlementExceptionModel.created_at.desc())
        )
        row = self._session.execute(stmt).scalars().first()
        return _exception_from_row(row) if row is not None else None

    def update_exception(
        self,
        exception_id: str,
        *,
        status: str | None = None,
        escalation_level: int | None = None,
        resolution: str | None = None,
        resolved_by: str | None = None,
        resolved_at: datetime.datetime | None = None,
        note: str | None = None,
    ) -> SettlementException | None:
        row = self._session.get(SettlementExceptionModel, exception_id)
        if row is None:
            return None
        if status is not None:
            row.status = status
        if escalation_level is not None:
            row.escalation_level = escalation_level
        if resolution is not None:
            row.resolution = resolution
        if resolved_by is not None:
            row.resolved_by = resolved_by
        if resolved_at is not None:
            row.resolved_at = resolved_at
        if note is not None:
            row.note = note
        self._session.flush()
        return _exception_from_row(row)

    # -- fee records ------------------------------------------------------

    def save_fee_record(
        self,
        *,
        installation_id: str,
        gateway_payment_id: str,
        contract_version_id: str,
        fee_percent_applied: Any,
        fee_vat_rate_applied: Any,
        expected_fee_irr: int,
        expected_vat_irr: int,
        source: str = "calculated",
        tranche_id: str | None = None,
        actual_fee_irr: int | None = None,
        fee_variance_irr: int | None = None,
        actual_vat_irr: int | None = None,
        vat_variance_irr: int | None = None,
    ) -> SettlementFeeRecord:
        row = SettlementFeeRecordModel(
            installation_id=installation_id,
            gateway_payment_id=gateway_payment_id,
            contract_version_id=contract_version_id,
            fee_percent_applied=fee_percent_applied,
            fee_vat_rate_applied=fee_vat_rate_applied,
            expected_fee_irr=expected_fee_irr,
            expected_vat_irr=expected_vat_irr,
            source=source,
            tranche_id=tranche_id,
            actual_fee_irr=actual_fee_irr,
            fee_variance_irr=fee_variance_irr,
            actual_vat_irr=actual_vat_irr,
            vat_variance_irr=vat_variance_irr,
        )
        self._session.add(row)
        self._session.flush()
        return _fee_record_from_row(row)

    def list_fee_records(
        self,
        installation_id: str,
        *,
        gateway_payment_id: str | None = None,
        channel_id: str | None = None,
    ) -> Sequence[SettlementFeeRecord]:
        stmt = select(SettlementFeeRecordModel).where(
            SettlementFeeRecordModel.installation_id == installation_id
        )
        if gateway_payment_id is not None:
            stmt = stmt.where(
                SettlementFeeRecordModel.gateway_payment_id == gateway_payment_id
            )
        if channel_id is not None:
            stmt = stmt.join(
                GatewayPaymentModel,
                SettlementFeeRecordModel.gateway_payment_id == GatewayPaymentModel.id,
            ).where(GatewayPaymentModel.payment_channel_id == channel_id)
        stmt = stmt.order_by(
            SettlementFeeRecordModel.created_at.desc(), SettlementFeeRecordModel.id.desc()
        )
        return [_fee_record_from_row(row) for row in self._session.execute(stmt).scalars().all()]

    def update_fee_record(
        self,
        fee_record_id: str,
        *,
        actual_fee_irr: int | None = None,
        actual_vat_irr: int | None = None,
        fee_variance_irr: int | None = None,
        vat_variance_irr: int | None = None,
        tryton_fee_journal_id: int | None = None,
        source: str | None = None,
    ) -> SettlementFeeRecord | None:
        row = self._session.get(SettlementFeeRecordModel, fee_record_id)
        if row is None:
            return None
        if actual_fee_irr is not None:
            row.actual_fee_irr = actual_fee_irr
        if actual_vat_irr is not None:
            row.actual_vat_irr = actual_vat_irr
        if fee_variance_irr is not None:
            row.fee_variance_irr = fee_variance_irr
        if vat_variance_irr is not None:
            row.vat_variance_irr = vat_variance_irr
        if tryton_fee_journal_id is not None:
            row.tryton_fee_journal_id = tryton_fee_journal_id
        if source is not None:
            row.source = source
        self._session.flush()
        return _fee_record_from_row(row)

    # -- postings (US-522) ------------------------------------------------

    def save_posting(
        self,
        *,
        installation_id: str,
        gateway_payment_id: str,
        kind: str,
        amount_irr: int,
        status: str = "posted",
        tranche_id: str | None = None,
        tryton_move_id: int | None = None,
        tryton_payment_id: int | None = None,
        tryton_reconciliation_id: int | None = None,
        reason: str | None = None,
        event_ref: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> SettlementPosting:
        row = SettlementPostingModel(
            installation_id=installation_id,
            gateway_payment_id=gateway_payment_id,
            kind=kind,
            amount_irr=amount_irr,
            status=status,
            tranche_id=tranche_id,
            tryton_move_id=tryton_move_id,
            tryton_payment_id=tryton_payment_id,
            tryton_reconciliation_id=tryton_reconciliation_id,
            reason=reason,
            event_ref=event_ref,
            details=details,
        )
        self._session.add(row)
        self._session.flush()
        return _posting_from_row(row)

    def get_posting(
        self, installation_id: str, posting_id: str
    ) -> SettlementPosting | None:
        stmt = select(SettlementPostingModel).where(
            SettlementPostingModel.installation_id == installation_id,
            SettlementPostingModel.id == posting_id,
        )
        row = self._session.execute(stmt).scalar_one_or_none()
        return _posting_from_row(row) if row is not None else None

    def list_postings_for_payment(
        self, installation_id: str, payment_id: str
    ) -> Sequence[SettlementPosting]:
        stmt = (
            select(SettlementPostingModel)
            .where(
                SettlementPostingModel.installation_id == installation_id,
                SettlementPostingModel.gateway_payment_id == payment_id,
            )
            .order_by(SettlementPostingModel.created_at, SettlementPostingModel.id)
        )
        return [_posting_from_row(row) for row in self._session.execute(stmt).scalars().all()]

    def list_postings_for_tranche(
        self, installation_id: str, tranche_id: str
    ) -> Sequence[SettlementPosting]:
        stmt = (
            select(SettlementPostingModel)
            .where(
                SettlementPostingModel.installation_id == installation_id,
                SettlementPostingModel.tranche_id == tranche_id,
            )
            .order_by(SettlementPostingModel.created_at, SettlementPostingModel.id)
        )
        return [_posting_from_row(row) for row in self._session.execute(stmt).scalars().all()]

    def get_posting_by_kind(
        self,
        installation_id: str,
        kind: str,
        *,
        gateway_payment_id: str | None = None,
        tranche_id: str | None = None,
    ) -> SettlementPosting | None:
        stmt = select(SettlementPostingModel).where(
            SettlementPostingModel.installation_id == installation_id,
            SettlementPostingModel.kind == kind,
        )
        if gateway_payment_id is not None:
            stmt = stmt.where(
                SettlementPostingModel.gateway_payment_id == gateway_payment_id
            )
        if tranche_id is not None:
            stmt = stmt.where(SettlementPostingModel.tranche_id == tranche_id)
        stmt = stmt.order_by(SettlementPostingModel.created_at)
        # First, not one-or-none: fee_variance/refund postings repeat.
        row = self._session.execute(stmt).scalars().first()
        return _posting_from_row(row) if row is not None else None

    # -- file imports -----------------------------------------------------

    def get_file_import_by_hash(
        self, installation_id: str, channel_id: str, file_hash_sha256: str
    ) -> SettlementFileImport | None:
        stmt = select(SettlementFileImportModel).where(
            SettlementFileImportModel.installation_id == installation_id,
            SettlementFileImportModel.payment_channel_id == channel_id,
            SettlementFileImportModel.file_hash_sha256 == file_hash_sha256,
        )
        row = self._session.execute(stmt).scalar_one_or_none()
        return _file_import_from_row(row) if row is not None else None

    def get_file_import(
        self, installation_id: str, import_id: str
    ) -> SettlementFileImport | None:
        stmt = select(SettlementFileImportModel).where(
            SettlementFileImportModel.installation_id == installation_id,
            SettlementFileImportModel.id == import_id,
        )
        row = self._session.execute(stmt).scalar_one_or_none()
        return _file_import_from_row(row) if row is not None else None

    def get_file_import_by_batch(
        self, installation_id: str, import_batch_id: str
    ) -> SettlementFileImport | None:
        stmt = select(SettlementFileImportModel).where(
            SettlementFileImportModel.installation_id == installation_id,
            SettlementFileImportModel.import_batch_id == import_batch_id,
        )
        row = self._session.execute(stmt).scalar_one_or_none()
        return _file_import_from_row(row) if row is not None else None

    def list_file_imports(
        self,
        installation_id: str,
        *,
        channel_id: str | None = None,
        status: str | None = None,
    ) -> Sequence[SettlementFileImport]:
        stmt = select(SettlementFileImportModel).where(
            SettlementFileImportModel.installation_id == installation_id
        )
        if channel_id is not None:
            stmt = stmt.where(SettlementFileImportModel.payment_channel_id == channel_id)
        if status is not None:
            stmt = stmt.where(SettlementFileImportModel.status == status)
        stmt = stmt.order_by(
            SettlementFileImportModel.created_at.desc(), SettlementFileImportModel.id.desc()
        )
        return [_file_import_from_row(row) for row in self._session.execute(stmt).scalars().all()]

    def save_file_import(
        self, command: CreateSettlementFileImport, *, channel_id: str
    ) -> SettlementFileImport:
        row = SettlementFileImportModel(
            installation_id=command.installation_id,
            payment_channel_id=channel_id,
            file_type=command.file_type,
            period_jalali=command.period_jalali,
            file_hash_sha256=command.file_hash_sha256,
            import_batch_id=command.import_batch_id,
            uploaded_by=command.uploaded_by,
            uploaded_at=datetime.datetime.now(datetime.UTC),
            row_count=command.row_count,
        )
        self._session.add(row)
        self._session.flush()
        return _file_import_from_row(row)

    def update_file_import(
        self,
        import_id: str,
        *,
        status: str | None = None,
        row_count: int | None = None,
        matched_count: int | None = None,
        new_count: int | None = None,
        error_count: int | None = None,
    ) -> SettlementFileImport | None:
        row = self._session.get(SettlementFileImportModel, import_id)
        if row is None:
            return None
        if status is not None:
            row.status = status
        if row_count is not None:
            row.row_count = row_count
        if matched_count is not None:
            row.matched_count = matched_count
        if new_count is not None:
            row.new_count = new_count
        if error_count is not None:
            row.error_count = error_count
        self._session.flush()
        return _file_import_from_row(row)

    def save_file_rows(
        self, rows: Sequence[CreateSettlementFileRow]
    ) -> Sequence[SettlementFileRow]:
        saved: list[SettlementFileRowModel] = []
        for command in rows:
            row = SettlementFileRowModel(
                import_id=command.import_id,
                row_no=command.row_no,
                raw_json=dict(command.raw_json),
                match_status=command.match_status,
                gateway_transaction_id=command.gateway_transaction_id,
                tran_type=command.tran_type,
                amount_irr=command.amount_irr,
                fee_irr=command.fee_irr,
                net_irr=command.net_irr,
                transaction_date=command.transaction_date,
                tracking_ref=command.tracking_ref,
                matched_tranche_id=command.matched_tranche_id,
                note=command.note,
            )
            self._session.add(row)
            saved.append(row)
        self._session.flush()
        return [_file_row_from_row(row) for row in saved]

    def list_file_rows(
        self, import_id: str, *, match_status: str | None = None
    ) -> Sequence[SettlementFileRow]:
        stmt = select(SettlementFileRowModel).where(
            SettlementFileRowModel.import_id == import_id
        )
        if match_status is not None:
            stmt = stmt.where(SettlementFileRowModel.match_status == match_status)
        stmt = stmt.order_by(SettlementFileRowModel.row_no)
        return [_file_row_from_row(row) for row in self._session.execute(stmt).scalars().all()]

    def update_file_row(
        self,
        row_id: str,
        *,
        match_status: str | None = None,
        matched_tranche_id: str | None = None,
        note: str | None = None,
    ) -> SettlementFileRow | None:
        row = self._session.get(SettlementFileRowModel, row_id)
        if row is None:
            return None
        if match_status is not None:
            row.match_status = match_status
        if matched_tranche_id is not None:
            row.matched_tranche_id = matched_tranche_id
        if note is not None:
            row.note = note
        self._session.flush()
        return _file_row_from_row(row)


class SqlAlchemySettlementAudit:
    """Audit-log adapter: settlement decisions into ``audit_events``.

    Wraps :class:`hesabyar.infrastructure.persistence.postgres.audit.AuditRecorder`
    behind the :class:`SettlementAuditPort` boundary. Like the recorder,
    it flushes but never commits — the caller owns the transaction.
    """

    def __init__(self, session: Session) -> None:
        from hesabyar.infrastructure.persistence.postgres.audit import AuditRecorder

        self._recorder = AuditRecorder(session)

    def record_settlement_audit(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        api_client_id: str | None,
        action: str,
        resource_type: str,
        resource_id: str | None,
        actor: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = dict(metadata or {})
        if actor is not None:
            payload["actor"] = actor
        self._recorder.record(
            tenant_id=tenant_id,
            installation_id=installation_id,
            api_client_id=api_client_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            company_external_id=None,
            outcome="success",
            source_system="api",
            metadata_after=payload,
        )
