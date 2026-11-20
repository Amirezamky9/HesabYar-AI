"""SQLAlchemy cheque repository (US-531, Phase 5-4 ۵ث).

Implements :class:`ChequeRepositoryPort` over the ``cheques`` /
``cheque_custody_log`` / ``cheque_inquiry_receipts`` /
``cheque_settings`` tables. The repository never talks to the
engine — engine calls go through ``AccountingEnginePort`` — and it
never commits: the caller (router / worker) owns the transaction.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from hesabyar.domain.cheques.models import (
    BounceDestination,
    Cheque,
    ChequeKind,
    ChequePurpose,
    ChequeSettings,
    ChequeState,
    CustodyLogEntry,
    InquiryChannel,
    InquiryReceipt,
    InquiryType,
    InstallmentLegStatus,
    RedBrownPolicy,
    SayadStatus,
    StorageLocation,
)
from hesabyar.domain.cheques.ports import ChequeRepositoryPort
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.infrastructure.persistence.postgres.models import (
    ChequeCustodyLogModel,
    ChequeInquiryReceiptModel,
    ChequeModel,
    ChequeSettingsModel,
)


def _cheque_from_row(row: ChequeModel) -> Cheque:
    return Cheque(
        id=row.id,
        tenant_id=row.tenant_id,
        installation_id=row.installation_id,
        company_engine_id=row.company_engine_id,
        kind=ChequeKind(row.kind),
        purpose=ChequePurpose(row.purpose),
        sayad_id=row.sayad_id,
        amount_irr=row.amount_irr,
        issue_date=FiscalDate.from_gregorian(row.issue_date),
        due_date=FiscalDate.from_gregorian(row.due_date),
        issuer_name=row.issuer_name,
        issuer_national_id=row.issuer_national_id,
        issuer_bank=row.issuer_bank,
        payee_name=row.payee_name,
        payee_national_id=row.payee_national_id,
        order_id=row.order_id,
        customer_ref=row.customer_ref,
        sayad_registered=row.sayad_registered,
        source=row.source,
        state=ChequeState(row.state),
        sayad_status=SayadStatus(row.sayad_status),
        sayad_unconfirmed=row.sayad_unconfirmed,
        leaf_number=row.leaf_number,
        issuer_branch=row.issuer_branch,
        issuer_account_number=row.issuer_account_number,
        reason=row.reason,
        invoice_id=row.invoice_id,
        installment_leg_id=row.installment_leg_id,
        applied_amount_irr=row.applied_amount_irr,
        scan_url=row.scan_url,
        scan_ref=row.scan_ref,
        scan_filestore_ref=row.scan_filestore_ref,
        registered_by=row.registered_by,
        notes=row.notes,
        storage_location=(
            StorageLocation(row.storage_location) if row.storage_location else None
        ),
        engine_id=row.engine_id,
        move_ids=dict(row.move_ids or {}),
        destination_bank_account_id=row.destination_bank_account_id,
        destination_bank_name=row.destination_bank_name,
        deposit_date=(
            FiscalDate.from_gregorian(row.deposit_date) if row.deposit_date else None
        ),
        clearing_confidence=row.clearing_confidence,
        clearing_statement_ref=row.clearing_statement_ref,
        clearing_reconciliation_id=row.clearing_reconciliation_id,
        issuer_color_code=row.issuer_color_code,
        issuer_color_inquired_at=row.issuer_color_inquired_at,
        issuer_back_cheques=row.issuer_back_cheques,
        issuer_in_transit_commitments_irr=row.issuer_in_transit_commitments_irr,
        manager_approval_by=row.manager_approval_by,
        manager_approval_at=row.manager_approval_at,
        receipt_missing_warning=row.receipt_missing_warning,
        bounce_reason=row.bounce_reason,
        bounce_date=(
            FiscalDate.from_gregorian(row.bounce_date) if row.bounce_date else None
        ),
        bounce_certificate_reference=row.bounce_certificate_reference,
        dunning_notified_at=row.dunning_notified_at,
        endorsee_name=row.endorsee_name,
        endorsee_type=row.endorsee_type,
        sayad_transfer_reference=row.sayad_transfer_reference,
        journal_pending_review=row.journal_pending_review,
        installment_leg_status=(
            InstallmentLegStatus(row.installment_leg_status)
            if row.installment_leg_status
            else None
        ),
        mismatch_detail=row.mismatch_detail,
        reminder_day3_sent_at=row.reminder_day3_sent_at,
        reminder_final_sent_at=row.reminder_final_sent_at,
        secured_obligation=row.secured_obligation,
        checkbook_engine_id=row.checkbook_engine_id,
        call_in_approved_by=row.call_in_approved_by,
        call_in_approved_at=row.call_in_approved_at,
        void_reason=row.void_reason,
        unexpected_call_in=bool(row.unexpected_call_in),
        call_in_statement_reference=row.call_in_statement_reference,
        stale_in_clearing_filed_at=row.stale_in_clearing_filed_at,
        source_event_id=row.source_event_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _row_from_cheque(cheque: Cheque, row: ChequeModel) -> ChequeModel:
    row.tenant_id = cheque.tenant_id
    row.installation_id = cheque.installation_id
    row.company_engine_id = cheque.company_engine_id
    row.kind = cheque.kind.value
    row.purpose = cheque.purpose.value
    row.sayad_id = cheque.sayad_id
    row.amount_irr = cheque.amount_irr
    row.issue_date = cheque.issue_date.date
    row.due_date = cheque.due_date.date
    row.issuer_name = cheque.issuer_name
    row.issuer_national_id = cheque.issuer_national_id
    row.issuer_bank = cheque.issuer_bank
    row.payee_name = cheque.payee_name
    row.payee_national_id = cheque.payee_national_id
    row.order_id = cheque.order_id
    row.customer_ref = cheque.customer_ref
    row.sayad_registered = cheque.sayad_registered
    row.source = cheque.source
    row.state = cheque.state.value
    row.sayad_status = cheque.sayad_status.value
    row.sayad_unconfirmed = cheque.sayad_unconfirmed
    row.leaf_number = cheque.leaf_number
    row.issuer_branch = cheque.issuer_branch
    row.issuer_account_number = cheque.issuer_account_number
    row.reason = cheque.reason
    row.invoice_id = cheque.invoice_id
    row.installment_leg_id = cheque.installment_leg_id
    row.applied_amount_irr = cheque.applied_amount_irr
    row.scan_url = cheque.scan_url
    row.scan_ref = cheque.scan_ref
    row.scan_filestore_ref = cheque.scan_filestore_ref
    row.registered_by = cheque.registered_by
    row.notes = cheque.notes
    row.storage_location = (
        cheque.storage_location.value if cheque.storage_location else None
    )
    row.engine_id = cheque.engine_id
    row.move_ids = dict(cheque.move_ids)
    row.destination_bank_account_id = cheque.destination_bank_account_id
    row.destination_bank_name = cheque.destination_bank_name
    row.deposit_date = cheque.deposit_date.date if cheque.deposit_date else None
    row.clearing_confidence = cheque.clearing_confidence
    row.clearing_statement_ref = cheque.clearing_statement_ref
    row.clearing_reconciliation_id = cheque.clearing_reconciliation_id
    row.issuer_color_code = cheque.issuer_color_code
    row.issuer_color_inquired_at = cheque.issuer_color_inquired_at
    row.issuer_back_cheques = cheque.issuer_back_cheques
    row.issuer_in_transit_commitments_irr = cheque.issuer_in_transit_commitments_irr
    row.manager_approval_by = cheque.manager_approval_by
    row.manager_approval_at = cheque.manager_approval_at
    row.receipt_missing_warning = cheque.receipt_missing_warning
    row.bounce_reason = cheque.bounce_reason
    row.bounce_date = cheque.bounce_date.date if cheque.bounce_date else None
    row.bounce_certificate_reference = cheque.bounce_certificate_reference
    row.dunning_notified_at = cheque.dunning_notified_at
    row.endorsee_name = cheque.endorsee_name
    row.endorsee_type = cheque.endorsee_type
    row.sayad_transfer_reference = cheque.sayad_transfer_reference
    row.journal_pending_review = cheque.journal_pending_review
    row.installment_leg_status = (
        cheque.installment_leg_status.value if cheque.installment_leg_status else None
    )
    row.mismatch_detail = cheque.mismatch_detail
    row.reminder_day3_sent_at = cheque.reminder_day3_sent_at
    row.reminder_final_sent_at = cheque.reminder_final_sent_at
    row.secured_obligation = cheque.secured_obligation
    row.checkbook_engine_id = cheque.checkbook_engine_id
    row.call_in_approved_by = cheque.call_in_approved_by
    row.call_in_approved_at = cheque.call_in_approved_at
    row.void_reason = cheque.void_reason
    row.unexpected_call_in = cheque.unexpected_call_in
    row.call_in_statement_reference = cheque.call_in_statement_reference
    row.stale_in_clearing_filed_at = cheque.stale_in_clearing_filed_at
    row.source_event_id = cheque.source_event_id
    row.updated_at = cheque.updated_at
    return row


def _custody_from_row(row: ChequeCustodyLogModel) -> CustodyLogEntry:
    return CustodyLogEntry(
        id=row.id,
        cheque_id=row.cheque_id,
        from_location=StorageLocation(row.from_location) if row.from_location else None,
        to_location=StorageLocation(row.to_location),
        actor=row.actor,
        reason=row.reason,
        at=row.at,
    )


def _receipt_from_row(row: ChequeInquiryReceiptModel) -> InquiryReceipt:
    return InquiryReceipt(
        id=row.id,
        cheque_id=row.cheque_id,
        channel=InquiryChannel(row.channel),
        inquiry_type=InquiryType(row.inquiry_type),
        result_code=row.result_code,
        result_summary=row.result_summary,
        reference=row.reference,
        recorded_by=row.recorded_by,
        cost_irr=row.cost_irr,
        requested_at=row.requested_at,
        responded_at=row.responded_at,
    )


def _settings_from_row(row: ChequeSettingsModel) -> ChequeSettings:
    return ChequeSettings(
        installation_id=row.installation_id,
        leaf_arrival_deadline_days=row.leaf_arrival_deadline_days,
        clearing_window_days=row.clearing_window_days,
        bounce_destination=BounceDestination(row.bounce_destination),
        red_brown_policy=RedBrownPolicy(row.red_brown_policy),
        max_in_transit_commitments_irr=row.max_in_transit_commitments_irr,
        default_storage_location=StorageLocation(row.default_storage_location),
    )


class SqlAlchemyChequeRepository(ChequeRepositoryPort):
    """SQLAlchemy implementation of the cheque repository port."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, cheque: Cheque) -> Cheque:
        row = _row_from_cheque(cheque, ChequeModel(id=cheque.id))
        row.created_at = cheque.created_at
        self._session.add(row)
        self._session.flush()
        return _cheque_from_row(row)

    def get(
        self, cheque_id: str, *, tenant_id: str, installation_id: str
    ) -> Cheque | None:
        stmt = select(ChequeModel).where(
            ChequeModel.id == cheque_id,
            ChequeModel.tenant_id == tenant_id,
            ChequeModel.installation_id == installation_id,
        )
        row = self._session.execute(stmt).scalar_one_or_none()
        return _cheque_from_row(row) if row is not None else None

    def get_by_sayad_id(
        self, sayad_id: str, *, tenant_id: str, installation_id: str
    ) -> Cheque | None:
        stmt = (
            select(ChequeModel)
            .where(
                ChequeModel.sayad_id == sayad_id,
                ChequeModel.tenant_id == tenant_id,
                ChequeModel.installation_id == installation_id,
                ChequeModel.state.notin_(
                    ("voided", "expired_unreceived", "rejected_mismatch")
                ),
            )
            .order_by(ChequeModel.created_at.desc())
        )
        row = self._session.execute(stmt).scalar_one_or_none()
        return _cheque_from_row(row) if row is not None else None

    def find_by_sayad_id(
        self, sayad_id: str, *, tenant_id: str, installation_id: str
    ) -> Cheque | None:
        stmt = (
            select(ChequeModel)
            .where(
                ChequeModel.sayad_id == sayad_id,
                ChequeModel.tenant_id == tenant_id,
                ChequeModel.installation_id == installation_id,
            )
            .order_by(ChequeModel.created_at.desc())
        )
        row = self._session.execute(stmt).scalar_one_or_none()
        return _cheque_from_row(row) if row is not None else None

    def find_in_clearing_cheques(self) -> list[Cheque]:
        """All cheques currently in clearing (US-533 stale filing)."""
        stmt = (
            select(ChequeModel)
            .where(ChequeModel.state == "in_clearing")
            .order_by(ChequeModel.due_date.asc(), ChequeModel.created_at.asc())
        )
        return [_cheque_from_row(row) for row in self._session.execute(stmt).scalars()]

    def list(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        state: ChequeState | None = None,
        issuer_national_id: str | None = None,
        due_before: datetime.date | None = None,
        kind: ChequeKind | None = None,
        purpose: ChequePurpose | None = None,
        endorsee_type: str | None = None,
        storage_location: StorageLocation | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Cheque]:
        stmt = select(ChequeModel).where(
            ChequeModel.tenant_id == tenant_id,
            ChequeModel.installation_id == installation_id,
        )
        if state is not None:
            stmt = stmt.where(ChequeModel.state == state.value)
        if issuer_national_id is not None:
            stmt = stmt.where(ChequeModel.issuer_national_id == issuer_national_id)
        if due_before is not None:
            stmt = stmt.where(ChequeModel.due_date < due_before)
        if kind is not None:
            stmt = stmt.where(ChequeModel.kind == kind.value)
        if purpose is not None:
            stmt = stmt.where(ChequeModel.purpose == purpose.value)
        if endorsee_type is not None:
            stmt = stmt.where(ChequeModel.endorsee_type == endorsee_type)
        if storage_location is not None:
            stmt = stmt.where(ChequeModel.storage_location == storage_location.value)
        stmt = stmt.order_by(ChequeModel.created_at.desc(), ChequeModel.id.desc())
        stmt = stmt.limit(limit).offset(offset)
        return [_cheque_from_row(row) for row in self._session.execute(stmt).scalars().all()]

    def save(self, cheque: Cheque) -> Cheque:
        stmt = select(ChequeModel).where(ChequeModel.id == cheque.id)
        row = self._session.execute(stmt).scalar_one_or_none()
        if row is None:
            raise ValueError(f"Cheque {cheque.id!r} does not exist in the registry.")
        _row_from_cheque(cheque, row)
        self._session.flush()
        return _cheque_from_row(row)

    def add_custody_entry(self, entry: CustodyLogEntry) -> CustodyLogEntry:
        row = ChequeCustodyLogModel(
            id=entry.id,
            cheque_id=entry.cheque_id,
            from_location=entry.from_location.value if entry.from_location else None,
            to_location=entry.to_location.value,
            actor=entry.actor,
            reason=entry.reason,
            at=entry.at,
        )
        self._session.add(row)
        self._session.flush()
        return _custody_from_row(row)

    def list_custody_entries(self, cheque_id: str) -> Sequence[CustodyLogEntry]:
        stmt = (
            select(ChequeCustodyLogModel)
            .where(ChequeCustodyLogModel.cheque_id == cheque_id)
            .order_by(ChequeCustodyLogModel.at.asc(), ChequeCustodyLogModel.id.asc())
        )
        return [
            _custody_from_row(row) for row in self._session.execute(stmt).scalars().all()
        ]

    def add_inquiry_receipt(self, receipt: InquiryReceipt) -> InquiryReceipt:
        row = ChequeInquiryReceiptModel(
            id=receipt.id,
            cheque_id=receipt.cheque_id,
            channel=receipt.channel.value,
            inquiry_type=receipt.inquiry_type.value,
            result_code=receipt.result_code,
            result_summary=receipt.result_summary,
            reference=receipt.reference,
            recorded_by=receipt.recorded_by,
            cost_irr=receipt.cost_irr,
            requested_at=receipt.requested_at,
            responded_at=receipt.responded_at,
        )
        self._session.add(row)
        self._session.flush()
        return _receipt_from_row(row)

    def list_inquiry_receipts(self, cheque_id: str) -> Sequence[InquiryReceipt]:
        stmt = (
            select(ChequeInquiryReceiptModel)
            .where(ChequeInquiryReceiptModel.cheque_id == cheque_id)
            .order_by(
                ChequeInquiryReceiptModel.created_at.asc(),
                ChequeInquiryReceiptModel.id.asc(),
            )
        )
        return [
            _receipt_from_row(row) for row in self._session.execute(stmt).scalars().all()
        ]

    def get_settings(self, installation_id: str) -> ChequeSettings | None:
        stmt = select(ChequeSettingsModel).where(
            ChequeSettingsModel.installation_id == installation_id
        )
        row = self._session.execute(stmt).scalar_one_or_none()
        return _settings_from_row(row) if row is not None else None

    def save_settings(self, settings: ChequeSettings) -> ChequeSettings:
        stmt = select(ChequeSettingsModel).where(
            ChequeSettingsModel.installation_id == settings.installation_id
        )
        row = self._session.execute(stmt).scalar_one_or_none()
        if row is None:
            row = ChequeSettingsModel(installation_id=settings.installation_id)
            self._session.add(row)
        row.leaf_arrival_deadline_days = settings.leaf_arrival_deadline_days
        row.clearing_window_days = settings.clearing_window_days
        row.bounce_destination = settings.bounce_destination.value
        row.red_brown_policy = settings.red_brown_policy.value
        row.max_in_transit_commitments_irr = settings.max_in_transit_commitments_irr
        row.default_storage_location = settings.default_storage_location.value
        self._session.flush()
        return _settings_from_row(row)

    def list_registered_before(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        registered_before: datetime.datetime,
    ) -> Sequence[Cheque]:
        stmt = (
            select(ChequeModel)
            .where(
                ChequeModel.tenant_id == tenant_id,
                ChequeModel.installation_id == installation_id,
                ChequeModel.state == ChequeState.REGISTERED.value,
                ChequeModel.created_at < registered_before,
            )
            .order_by(ChequeModel.created_at.asc(), ChequeModel.id.asc())
        )
        return [_cheque_from_row(row) for row in self._session.execute(stmt).scalars().all()]

    def find_registered_cheques(self) -> Sequence[Cheque]:
        stmt = (
            select(ChequeModel)
            .where(ChequeModel.state == ChequeState.REGISTERED.value)
            .order_by(ChequeModel.created_at.asc(), ChequeModel.id.asc())
        )
        return [_cheque_from_row(row) for row in self._session.execute(stmt).scalars().all()]
