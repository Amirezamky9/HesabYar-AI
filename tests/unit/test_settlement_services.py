"""Service tests for the settlement engine (US-521).

DB-free: the command/query services run against an in-memory fake of
the settlement port, the shared fake integrations registry (from
``tests.unit.test_integrations_services``), and a recording audit
fake. The SQLAlchemy repository has its own DB tests
(``tests/infrastructure/test_settlement_repository.py``).
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pytest

from hesabyar.application.commands.integrations import (
    ChannelRegistryService,
    GatewayContractService,
)
from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.application.commands.settlement import SettlementCommandService
from hesabyar.application.errors import IntegrationChannelNotFoundError
from hesabyar.application.queries.settlement import SettlementQueryService
from hesabyar.domain.integrations.models import (
    ChannelStatus,
    TrancheLeg,
)
from hesabyar.domain.review_queue.models import (
    ReviewAuditEntry,
    ReviewItem,
    ReviewItemStatus,
    ReviewItemType,
    ReviewSeverity,
    SlaPolicy,
)
from hesabyar.domain.review_queue.ports import (
    ReviewAuditPort,
    ReviewItemFilters,
    ReviewQueuePort,
)
from hesabyar.domain.settlement.commands import (
    CancelGatewayPayment,
    CreateBankDeposit,
    CreateGatewayPayment,
    CreateSettlementEvent,
    CreateSettlementException,
    CreateSettlementFileImport,
    CreateSettlementFileRow,
    CreateSettlementMatch,
    ReassignSalesPeriod,
    RecordTrancheWriteOff,
    ResolveSettlementException,
    TransitionTranche,
)
from hesabyar.domain.settlement.errors import (
    SettlementContractMissingError,
    SettlementExceptionNotFoundError,
    SettlementFeeVatRateUnknownError,
    SettlementInvalidTransitionError,
    SettlementPaymentExistsError,
    SettlementPaymentInvalidError,
    SettlementPaymentNotFoundError,
    SettlementTrancheNotFoundError,
    SettlementWriteOffNotAllowedError,
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
from tests.unit.test_integrations_services import FakeIntegrationsRepository

INSTALLATION_ID = "00000000-0000-0000-0000-000000000521"
TENANT_ID = "00000000-0000-0000-0000-000000000522"
NOW = datetime.datetime(2026, 11, 6, 12, 0, tzinfo=datetime.UTC)

FOUR_TRANCHES = (
    TrancheLeg(1, 5, Decimal("25")),
    TrancheLeg(2, 5, Decimal("25")),
    TrancheLeg(3, 5, Decimal("25")),
    TrancheLeg(4, 5, Decimal("25")),
)


def _new_id() -> str:
    return str(uuid.uuid4())


@dataclass
class FakeSettlementAudit:
    """Recording audit fake (one entry per audited decision)."""

    entries: list[dict[str, Any]] = field(default_factory=list)

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
        self.entries.append(
            {
                "tenant_id": tenant_id,
                "installation_id": installation_id,
                "api_client_id": api_client_id,
                "action": action,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "actor": actor,
                "metadata": metadata or {},
            }
        )


@dataclass
class FakeSettlementRepository:
    """In-memory implementation of the settlement port (for service tests)."""

    payments: dict[str, GatewayPayment] = field(default_factory=dict)
    tranches: dict[str, SettlementTranche] = field(default_factory=dict)
    events: dict[str, SettlementEvent] = field(default_factory=dict)
    deposits: dict[str, BankDeposit] = field(default_factory=dict)
    matches: dict[str, SettlementMatch] = field(default_factory=dict)
    exceptions: dict[str, SettlementException] = field(default_factory=dict)
    fee_records: dict[str, SettlementFeeRecord] = field(default_factory=dict)
    file_imports: dict[str, SettlementFileImport] = field(default_factory=dict)
    file_rows: dict[str, SettlementFileRow] = field(default_factory=dict)
    postings: dict[str, SettlementPosting] = field(default_factory=dict)
    #: (tranche id, grace days) returned by the tick query.
    tick_candidates: list[tuple[SettlementTranche, int]] = field(default_factory=list)

    # -- payments ---------------------------------------------------------

    def get_payment(self, installation_id: str, payment_id: str) -> GatewayPayment | None:
        payment = self.payments.get(payment_id)
        if payment is None or payment.installation_id != installation_id:
            return None
        return payment

    def get_payment_by_transaction(
        self, installation_id: str, channel_id: str, gateway_transaction_id: str
    ) -> GatewayPayment | None:
        for payment in self.payments.values():
            if (
                payment.installation_id == installation_id
                and payment.payment_channel_id == channel_id
                and payment.gateway_transaction_id == gateway_transaction_id
            ):
                return payment
        return None

    def list_payments(
        self,
        installation_id: str,
        *,
        channel_id: str | None = None,
        status: str | None = None,
        sales_period_jalali: str | None = None,
    ) -> Sequence[GatewayPayment]:
        found = [p for p in self.payments.values() if p.installation_id == installation_id]
        if channel_id is not None:
            found = [p for p in found if p.payment_channel_id == channel_id]
        if status is not None:
            found = [p for p in found if p.status.value == status]
        if sales_period_jalali is not None:
            found = [p for p in found if p.sales_period_jalali == sales_period_jalali]
        return sorted(found, key=lambda p: p.gateway_confirmed_at, reverse=True)

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
        payment = GatewayPayment(
            id=_new_id(),
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
            source=command.source,
            store_order_id=command.store_order_id,
            store_payment_id=command.store_payment_id,
            tryton_invoice_id=command.tryton_invoice_id,
            tryton_journal_id=command.tryton_journal_id,
            customer_party_id=command.customer_party_id,
            canonical_event_id=command.canonical_event_id,
            created_at=NOW,
            updated_at=NOW,
        )
        self.payments[payment.id] = payment
        return payment

    def update_payment_status(
        self, payment_id: str, status: str, *, refunds_deducted_irr: int | None = None
    ) -> GatewayPayment | None:
        payment = self.payments.get(payment_id)
        if payment is None:
            return None
        updated = GatewayPayment(
            **{
                **payment.__dict__,
                "status": GatewayPaymentStatus(status),
                "refunds_deducted_irr": (
                    refunds_deducted_irr
                    if refunds_deducted_irr is not None
                    else payment.refunds_deducted_irr
                ),
            }
        )
        self.payments[payment_id] = updated
        return updated

    def update_payment_sales_period(
        self, payment_id: str, sales_period_jalali: str
    ) -> GatewayPayment | None:
        payment = self.payments.get(payment_id)
        if payment is None:
            return None
        updated = GatewayPayment(**{**payment.__dict__, "sales_period_jalali": sales_period_jalali})
        self.payments[payment_id] = updated
        return updated

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
        payment = self.payments.get(payment_id)
        if payment is None:
            return None
        updated = GatewayPayment(
            **{
                **payment.__dict__,
                "fee_irr": fee_irr,
                "fee_vat_irr": fee_vat_irr,
                "net_irr": net_irr,
                "contract_version_id": contract_version_id,
                "fee_rate_snapshot": fee_rate_snapshot,
                "fee_vat_rate_snapshot": fee_vat_rate_snapshot,
            }
        )
        self.payments[payment_id] = updated
        return updated

    # -- tranches ---------------------------------------------------------

    def get_tranche(self, installation_id: str, tranche_id: str) -> SettlementTranche | None:
        tranche = self.tranches.get(tranche_id)
        if tranche is None or tranche.installation_id != installation_id:
            return None
        return tranche

    def list_tranches_for_payment(
        self, installation_id: str, payment_id: str
    ) -> Sequence[SettlementTranche]:
        found = [
            t
            for t in self.tranches.values()
            if t.installation_id == installation_id and t.gateway_payment_id == payment_id
        ]
        return sorted(found, key=lambda t: t.tranche_index)

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
        found = [t for t in self.tranches.values() if t.installation_id == installation_id]
        if channel_id is not None:
            payment_ids = {
                p.id for p in self.payments.values() if p.payment_channel_id == channel_id
            }
            found = [t for t in found if t.gateway_payment_id in payment_ids]
        if state is not None:
            found = [t for t in found if t.state == state]
        if states is not None:
            found = [t for t in found if t.state in states]
        if sales_period_jalali is not None:
            found = [t for t in found if t.sales_period_jalali == sales_period_jalali]
        if due_from is not None:
            found = [t for t in found if t.expected_date >= due_from]
        if due_to is not None:
            found = [t for t in found if t.expected_date <= due_to]
        return sorted(found, key=lambda t: (t.expected_date, t.tranche_index))

    def save_tranches(
        self,
        installation_id: str,
        payment_id: str,
        tranches: Sequence[DerivedTranche],
    ) -> Sequence[SettlementTranche]:
        saved: list[SettlementTranche] = []
        for derived in tranches:
            tranche = SettlementTranche(
                id=_new_id(),
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
                created_at=NOW,
                updated_at=NOW,
            )
            self.tranches[tranche.id] = tranche
            saved.append(tranche)
        return saved

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
        tranche = self.tranches.get(tranche_id)
        if tranche is None:
            return None
        values = dict(tranche.__dict__)
        if state is not None:
            values["state"] = state
        if confirmed_net_irr is not None:
            values["confirmed_net_irr"] = confirmed_net_irr
        if received_net_irr is not None:
            values["received_net_irr"] = received_net_irr
        if variance_irr is not None:
            values["variance_irr"] = variance_irr
        if variance_reason is not None:
            values["variance_reason"] = variance_reason
        if match_method is not None:
            values["match_method"] = match_method
        if match_confidence is not None:
            values["match_confidence"] = match_confidence
        if tryton_receivable_line_id is not None:
            values["tryton_receivable_line_id"] = tryton_receivable_line_id
        if expected_window_start is not None:
            values["expected_window_start"] = expected_window_start
        if expected_date is not None:
            values["expected_date"] = expected_date
        if sales_period_jalali is not None:
            values["sales_period_jalali"] = sales_period_jalali
        if gross_irr is not None:
            values["gross_irr"] = gross_irr
        if fee_irr is not None:
            values["fee_irr"] = fee_irr
        if fee_vat_irr is not None:
            values["fee_vat_irr"] = fee_vat_irr
        if net_irr is not None:
            values["net_irr"] = net_irr
        updated = SettlementTranche(**values)
        self.tranches[tranche_id] = updated
        return updated

    def delete_tranches(self, tranche_ids: Sequence[str]) -> int:
        count = 0
        for tranche_id in tranche_ids:
            if self.tranches.pop(tranche_id, None) is not None:
                count += 1
        return count

    def find_tranches_due_for_tick(
        self, today: datetime.date
    ) -> Sequence[tuple[SettlementTranche, int]]:
        return list(self.tick_candidates)

    # -- events / deposits / matches --------------------------------------

    def save_settlement_event(self, command: CreateSettlementEvent, *, channel_id: str) -> SettlementEvent:
        event = SettlementEvent(
            id=_new_id(),
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
            created_at=NOW,
            updated_at=NOW,
        )
        self.events[event.id] = event
        return event

    def get_settlement_event(
        self, installation_id: str, event_id: str
    ) -> SettlementEvent | None:
        event = self.events.get(event_id)
        if event is None or event.installation_id != installation_id:
            return None
        return event

    def list_settlement_events(
        self,
        installation_id: str,
        *,
        channel_id: str | None = None,
        import_batch_id: str | None = None,
    ) -> Sequence[SettlementEvent]:
        found = [e for e in self.events.values() if e.installation_id == installation_id]
        if channel_id is not None:
            found = [e for e in found if e.payment_channel_id == channel_id]
        if import_batch_id is not None:
            found = [e for e in found if e.import_batch_id == import_batch_id]
        return sorted(found, key=lambda e: e.payout_date, reverse=True)

    def save_bank_deposit(self, command: CreateBankDeposit) -> BankDeposit:
        deposit = BankDeposit(
            id=_new_id(),
            installation_id=command.installation_id,
            deposit_date=command.deposit_date,
            amount_irr=command.amount_irr,
            bank_account_ref=command.bank_account_ref,
            description=command.description,
            tracking_ref=command.tracking_ref,
            bank_import_row_id=command.bank_import_row_id,
            source=command.source,
            created_at=NOW,
            updated_at=NOW,
        )
        self.deposits[deposit.id] = deposit
        return deposit

    def get_bank_deposit(
        self, installation_id: str, deposit_id: str
    ) -> BankDeposit | None:
        deposit = self.deposits.get(deposit_id)
        if deposit is None or deposit.installation_id != installation_id:
            return None
        return deposit

    def list_bank_deposits(
        self, installation_id: str, *, unmatched_only: bool = False
    ) -> Sequence[BankDeposit]:
        found = [d for d in self.deposits.values() if d.installation_id == installation_id]
        if unmatched_only:
            matched_ids = {
                m.bank_deposit_id for m in self.matches.values() if m.bank_deposit_id
            }
            found = [d for d in found if d.id not in matched_ids]
        return sorted(found, key=lambda d: d.deposit_date, reverse=True)

    def save_settlement_match(self, command: CreateSettlementMatch) -> SettlementMatch:
        match = SettlementMatch(
            id=_new_id(),
            installation_id=command.installation_id,
            tranche_id=command.tranche_id,
            allocated_amount_irr=command.allocated_amount_irr,
            method=command.method,
            bank_deposit_id=command.bank_deposit_id,
            settlement_event_id=command.settlement_event_id,
            confidence=command.confidence,
            matched_by=command.matched_by,
            is_automatic=command.is_automatic,
            matched_at=NOW,
            created_at=NOW,
        )
        self.matches[match.id] = match
        return match

    def update_match(
        self, match_id: str, *, settlement_event_id: str | None = None
    ) -> SettlementMatch | None:
        match = self.matches.get(match_id)
        if match is None:
            return None
        values = dict(match.__dict__)
        if settlement_event_id is not None:
            values["settlement_event_id"] = settlement_event_id
        updated = SettlementMatch(**values)
        self.matches[match_id] = updated
        return updated

    def list_matches_for_tranche(
        self, installation_id: str, tranche_id: str
    ) -> Sequence[SettlementMatch]:
        return [
            m
            for m in self.matches.values()
            if m.installation_id == installation_id and m.tranche_id == tranche_id
        ]

    def list_matches_for_deposit(
        self, installation_id: str, deposit_id: str
    ) -> Sequence[SettlementMatch]:
        return [
            m
            for m in self.matches.values()
            if m.installation_id == installation_id and m.bank_deposit_id == deposit_id
        ]

    def list_matches_for_event(
        self, installation_id: str, settlement_event_id: str
    ) -> Sequence[SettlementMatch]:
        return [
            m
            for m in self.matches.values()
            if m.installation_id == installation_id
            and m.settlement_event_id == settlement_event_id
        ]

    # -- exceptions ---------------------------------------------------------

    def save_exception(self, command: CreateSettlementException) -> SettlementException:
        exc = SettlementException(
            id=_new_id(),
            installation_id=command.installation_id,
            exception_type=SettlementExceptionType(command.exception_type),
            severity=SettlementExceptionSeverity(command.severity),
            tranche_id=command.tranche_id,
            settlement_event_id=command.settlement_event_id,
            bank_deposit_id=command.bank_deposit_id,
            gateway_payment_id=command.gateway_payment_id,
            expected_amount_irr=command.expected_amount_irr,
            actual_amount_irr=command.actual_amount_irr,
            escalation_level=command.escalation_level,
            note=command.note,
            created_at=NOW,
            updated_at=NOW,
        )
        self.exceptions[exc.id] = exc
        return exc

    def get_exception(
        self, installation_id: str, exception_id: str
    ) -> SettlementException | None:
        exc = self.exceptions.get(exception_id)
        if exc is None or exc.installation_id != installation_id:
            return None
        return exc

    def list_exceptions(
        self,
        installation_id: str,
        *,
        exception_type: str | None = None,
        status: str | None = None,
        tranche_id: str | None = None,
    ) -> Sequence[SettlementException]:
        found = [e for e in self.exceptions.values() if e.installation_id == installation_id]
        if exception_type is not None:
            found = [e for e in found if e.exception_type.value == exception_type]
        if status is not None:
            found = [e for e in found if e.status.value == status]
        if tranche_id is not None:
            found = [e for e in found if e.tranche_id == tranche_id]
        return found

    def get_open_exception_for_tranche(
        self, installation_id: str, tranche_id: str, exception_type: str
    ) -> SettlementException | None:
        for exc in self.exceptions.values():
            if (
                exc.installation_id == installation_id
                and exc.tranche_id == tranche_id
                and exc.exception_type.value == exception_type
                and exc.status
                in (SettlementExceptionStatus.OPEN, SettlementExceptionStatus.IN_REVIEW)
            ):
                return exc
        return None

    def get_open_exception_for_deposit(
        self, installation_id: str, deposit_id: str, exception_type: str
    ) -> SettlementException | None:
        for exc in self.exceptions.values():
            if (
                exc.installation_id == installation_id
                and exc.bank_deposit_id == deposit_id
                and exc.exception_type.value == exception_type
                and exc.status
                in (SettlementExceptionStatus.OPEN, SettlementExceptionStatus.IN_REVIEW)
            ):
                return exc
        return None

    def get_open_exception_for_event(
        self, installation_id: str, settlement_event_id: str, exception_type: str
    ) -> SettlementException | None:
        for exc in self.exceptions.values():
            if (
                exc.installation_id == installation_id
                and exc.settlement_event_id == settlement_event_id
                and exc.exception_type.value == exception_type
                and exc.status
                in (SettlementExceptionStatus.OPEN, SettlementExceptionStatus.IN_REVIEW)
            ):
                return exc
        return None

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
        exc = self.exceptions.get(exception_id)
        if exc is None:
            return None
        values = dict(exc.__dict__)
        if status is not None:
            values["status"] = SettlementExceptionStatus(status)
        if escalation_level is not None:
            values["escalation_level"] = escalation_level
        if resolution is not None:
            values["resolution"] = SettlementExceptionResolution(resolution)
        if resolved_by is not None:
            values["resolved_by"] = resolved_by
        if resolved_at is not None:
            values["resolved_at"] = resolved_at
        if note is not None:
            values["note"] = note
        updated = SettlementException(**values)
        self.exceptions[exception_id] = updated
        return updated

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
        record = SettlementFeeRecord(
            id=_new_id(),
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
            created_at=NOW,
            updated_at=NOW,
        )
        self.fee_records[record.id] = record
        return record

    def list_fee_records(
        self,
        installation_id: str,
        *,
        gateway_payment_id: str | None = None,
        channel_id: str | None = None,
    ) -> Sequence[SettlementFeeRecord]:
        found = [r for r in self.fee_records.values() if r.installation_id == installation_id]
        if gateway_payment_id is not None:
            found = [r for r in found if r.gateway_payment_id == gateway_payment_id]
        if channel_id is not None:
            payment_ids = {
                p.id for p in self.payments.values() if p.payment_channel_id == channel_id
            }
            found = [r for r in found if r.gateway_payment_id in payment_ids]
        return found

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
        record = self.fee_records.get(fee_record_id)
        if record is None:
            return None
        values = dict(record.__dict__)
        if actual_fee_irr is not None:
            values["actual_fee_irr"] = actual_fee_irr
        if actual_vat_irr is not None:
            values["actual_vat_irr"] = actual_vat_irr
        if fee_variance_irr is not None:
            values["fee_variance_irr"] = fee_variance_irr
        if vat_variance_irr is not None:
            values["vat_variance_irr"] = vat_variance_irr
        if tryton_fee_journal_id is not None:
            values["tryton_fee_journal_id"] = tryton_fee_journal_id
        if source is not None:
            values["source"] = source
        updated = SettlementFeeRecord(**values)
        self.fee_records[fee_record_id] = updated
        return updated

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
        posting = SettlementPosting(
            id=_new_id(),
            installation_id=installation_id,
            gateway_payment_id=gateway_payment_id,
            kind=SettlementPostingKind(kind),
            amount_irr=amount_irr,
            status=SettlementPostingStatus(status),
            tranche_id=tranche_id,
            tryton_move_id=tryton_move_id,
            tryton_payment_id=tryton_payment_id,
            tryton_reconciliation_id=tryton_reconciliation_id,
            reason=reason,
            event_ref=event_ref,
            details=dict(details) if details is not None else None,
            created_at=NOW,
            updated_at=NOW,
        )
        self.postings[posting.id] = posting
        return posting

    def get_posting(
        self, installation_id: str, posting_id: str
    ) -> SettlementPosting | None:
        posting = self.postings.get(posting_id)
        if posting is None or posting.installation_id != installation_id:
            return None
        return posting

    def list_postings_for_payment(
        self, installation_id: str, payment_id: str
    ) -> Sequence[SettlementPosting]:
        return [
            p
            for p in self.postings.values()
            if p.installation_id == installation_id
            and p.gateway_payment_id == payment_id
        ]

    def list_postings_for_tranche(
        self, installation_id: str, tranche_id: str
    ) -> Sequence[SettlementPosting]:
        return [
            p
            for p in self.postings.values()
            if p.installation_id == installation_id and p.tranche_id == tranche_id
        ]

    def get_posting_by_kind(
        self,
        installation_id: str,
        kind: str,
        *,
        gateway_payment_id: str | None = None,
        tranche_id: str | None = None,
    ) -> SettlementPosting | None:
        for posting in self.postings.values():
            if (
                posting.installation_id == installation_id
                and posting.kind.value == kind
                and (gateway_payment_id is None or posting.gateway_payment_id == gateway_payment_id)
                and (tranche_id is None or posting.tranche_id == tranche_id)
            ):
                return posting
        return None

    # -- file imports -----------------------------------------------------

    def get_file_import_by_hash(
        self, installation_id: str, channel_id: str, file_hash_sha256: str
    ) -> SettlementFileImport | None:
        for imp in self.file_imports.values():
            if (
                imp.installation_id == installation_id
                and imp.payment_channel_id == channel_id
                and imp.file_hash_sha256 == file_hash_sha256
            ):
                return imp
        return None

    def get_file_import(
        self, installation_id: str, import_id: str
    ) -> SettlementFileImport | None:
        imp = self.file_imports.get(import_id)
        if imp is None or imp.installation_id != installation_id:
            return None
        return imp

    def get_file_import_by_batch(
        self, installation_id: str, import_batch_id: str
    ) -> SettlementFileImport | None:
        for imp in self.file_imports.values():
            if (
                imp.installation_id == installation_id
                and imp.import_batch_id == import_batch_id
            ):
                return imp
        return None

    def list_file_imports(
        self,
        installation_id: str,
        *,
        channel_id: str | None = None,
        status: str | None = None,
    ) -> Sequence[SettlementFileImport]:
        found = [
            imp for imp in self.file_imports.values() if imp.installation_id == installation_id
        ]
        if channel_id is not None:
            found = [imp for imp in found if imp.payment_channel_id == channel_id]
        if status is not None:
            found = [imp for imp in found if imp.status == status]
        return found

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
        imp = self.file_imports.get(import_id)
        if imp is None:
            return None
        values = dict(imp.__dict__)
        if status is not None:
            values["status"] = status
        if row_count is not None:
            values["row_count"] = row_count
        if matched_count is not None:
            values["matched_count"] = matched_count
        if new_count is not None:
            values["new_count"] = new_count
        if error_count is not None:
            values["error_count"] = error_count
        updated = SettlementFileImport(**values)
        self.file_imports[import_id] = updated
        return updated

    def update_file_row(
        self,
        row_id: str,
        *,
        match_status: str | None = None,
        matched_tranche_id: str | None = None,
        note: str | None = None,
    ) -> SettlementFileRow | None:
        row = self.file_rows.get(row_id)
        if row is None:
            return None
        values = dict(row.__dict__)
        if match_status is not None:
            values["match_status"] = match_status
        if matched_tranche_id is not None:
            values["matched_tranche_id"] = matched_tranche_id
        if note is not None:
            values["note"] = note
        updated = SettlementFileRow(**values)
        self.file_rows[row_id] = updated
        return updated

    def save_file_import(
        self, command: CreateSettlementFileImport, *, channel_id: str
    ) -> SettlementFileImport:
        imp = SettlementFileImport(
            id=_new_id(),
            installation_id=command.installation_id,
            payment_channel_id=channel_id,
            file_type=command.file_type,
            period_jalali=command.period_jalali,
            file_hash_sha256=command.file_hash_sha256,
            import_batch_id=command.import_batch_id,
            uploaded_by=command.uploaded_by,
            uploaded_at=NOW,
            row_count=command.row_count,
            created_at=NOW,
            updated_at=NOW,
        )
        self.file_imports[imp.id] = imp
        return imp

    def save_file_rows(
        self, rows: Sequence[CreateSettlementFileRow]
    ) -> Sequence[SettlementFileRow]:
        saved: list[SettlementFileRow] = []
        for command in rows:
            row = SettlementFileRow(
                id=_new_id(),
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
                created_at=NOW,
            )
            self.file_rows[row.id] = row
            saved.append(row)
        return saved

    def list_file_rows(
        self, import_id: str, *, match_status: str | None = None
    ) -> Sequence[SettlementFileRow]:
        found = [r for r in self.file_rows.values() if r.import_id == import_id]
        if match_status is not None:
            found = [r for r in found if r.match_status == match_status]
        return sorted(found, key=lambda r: r.row_no)


class FakeReviewQueue(ReviewQueuePort, ReviewAuditPort):
    """In-memory review-queue + audit ports (one object, both ports)."""

    def __init__(self) -> None:
        self.items: dict[str, ReviewItem] = {}
        self.audit: list[ReviewAuditEntry] = []

    def list_items(self, tenant_id: str, filters: ReviewItemFilters) -> Sequence[ReviewItem]:
        return [item for item in self.items.values() if item.tenant_id == tenant_id]

    def get_item(self, item_id: str, tenant_id: str) -> ReviewItem | None:
        item = self.items.get(item_id)
        return item if item is not None and item.tenant_id == tenant_id else None

    def save_item(self, item: ReviewItem) -> ReviewItem:
        self.items[item.id] = item
        return item

    def update_item(self, item: ReviewItem) -> ReviewItem:
        self.items[item.id] = item
        return item

    def list_policies(self, tenant_id: str) -> Sequence[SlaPolicy]:
        return []

    def upsert_policy(self, tenant_id: str, policy: SlaPolicy) -> SlaPolicy:
        return policy

    def record(self, entry: ReviewAuditEntry) -> ReviewAuditEntry:
        self.audit.append(entry)
        return entry

    def list_for_item(self, item_id: str, tenant_id: str) -> Sequence[ReviewAuditEntry]:
        return [entry for entry in self.audit if entry.item_id == item_id]


@dataclass
class FakeTenantResolver:
    """In-memory installation→tenant map (US-555)."""

    tenants: dict[str, str] = field(default_factory=dict)

    def tenant_id_for_installation(self, installation_id: str) -> str | None:
        return self.tenants.get(installation_id)


@pytest.fixture()
def registry() -> FakeIntegrationsRepository:
    """Registry with a snapppay channel + an active 4-tranche contract."""
    repo = FakeIntegrationsRepository()
    channels = ChannelRegistryService(repo)
    contracts = GatewayContractService(repo)
    channel = channels.create_channel(
        INSTALLATION_ID, "snapppay", "اسنپ‌پی", "bnpl"
    )
    contracts.create_version(
        INSTALLATION_ID,
        "snapppay",
        valid_from=datetime.date(2026, 10, 23),
        fee_percent=Decimal("10"),
        fee_vat_rate=Decimal("10"),
        tranche_legs=FOUR_TRANCHES,
        payout_window_start_day=1,
        payout_window_end_day=5,
        grace_days_after_due=3,
    )
    assert channel.status == ChannelStatus.SETUP  # born disabled; the engine does not care
    return repo


@pytest.fixture()
def repo() -> FakeSettlementRepository:
    return FakeSettlementRepository()


@pytest.fixture()
def audit() -> FakeSettlementAudit:
    return FakeSettlementAudit()


@pytest.fixture()
def commands(
    repo: FakeSettlementRepository,
    registry: FakeIntegrationsRepository,
    audit: FakeSettlementAudit,
) -> SettlementCommandService:
    return SettlementCommandService(repo, registry, audit)


@pytest.fixture()
def queries(
    repo: FakeSettlementRepository, registry: FakeIntegrationsRepository
) -> SettlementQueryService:
    return SettlementQueryService(repo, registry)


def _record(
    commands: SettlementCommandService, *, txn: str = "TXN-1", gross: int = 100_000_000
) -> GatewayPayment:
    result = commands.record_payment(
        CreateGatewayPayment(
            installation_id=INSTALLATION_ID,
            channel_key="snapppay",
            gateway_transaction_id=txn,
            gross_irr=gross,
            gateway_confirmed_at=NOW,
            source=PaymentSource.STORE_WEBHOOK,
            store_order_id="ORD-1",
        )
    )
    return result.payment


class TestRecordPayment:
    def test_records_payment_with_schedule_and_fee_record(
        self, commands: SettlementCommandService, repo: FakeSettlementRepository
    ) -> None:
        result = commands.record_payment(
            CreateGatewayPayment(
                installation_id=INSTALLATION_ID,
                channel_key="snapppay",
                gateway_transaction_id="TXN-1",
                gross_irr=100_000_000,
                gateway_confirmed_at=NOW,
                source=PaymentSource.STORE_WEBHOOK,
            )
        )
        assert result.payment.fee_irr == 10_000_000
        assert result.payment.fee_vat_irr == 1_000_000
        assert result.payment.net_irr == 89_000_000
        assert result.payment.fee_rate_snapshot == Decimal("10")
        assert result.payment.fee_vat_rate_snapshot == Decimal("10")
        assert result.payment.sales_period_jalali == "1405/08"
        assert result.payment.status == GatewayPaymentStatus.SCHEDULED
        assert len(result.tranches) == 4
        assert [t.net_irr for t in result.tranches] == [22_250_000] * 4
        assert all(t.state == TrancheState.EXPECTED for t in result.tranches)
        assert result.fee_record.expected_fee_irr == 10_000_000
        assert result.fee_record.expected_vat_irr == 1_000_000
        assert result.fee_record.source == "calculated"

    def test_duplicate_transaction_rejected(
        self, commands: SettlementCommandService
    ) -> None:
        _record(commands)
        with pytest.raises(SettlementPaymentExistsError):
            _record(commands)

    def test_unknown_channel_rejected(self, commands: SettlementCommandService) -> None:
        with pytest.raises(IntegrationChannelNotFoundError):
            commands.record_payment(
                CreateGatewayPayment(
                    installation_id=INSTALLATION_ID,
                    channel_key="nope",
                    gateway_transaction_id="TXN-9",
                    gross_irr=1_000_000,
                    gateway_confirmed_at=NOW,
                    source=PaymentSource.MANUAL_ENTRY,
                )
            )

    def test_no_active_contract_rejected(
        self,
        repo: FakeSettlementRepository,
        registry: FakeIntegrationsRepository,
        audit: FakeSettlementAudit,
    ) -> None:
        ChannelRegistryService(registry).create_channel(
            INSTALLATION_ID, "zarinpal", "زرین‌پال", "gateway"
        )
        service = SettlementCommandService(repo, registry, audit)
        with pytest.raises(SettlementContractMissingError):
            service.record_payment(
                CreateGatewayPayment(
                    installation_id=INSTALLATION_ID,
                    channel_key="zarinpal",
                    gateway_transaction_id="TXN-9",
                    gross_irr=1_000_000,
                    gateway_confirmed_at=NOW,
                    source=PaymentSource.MANUAL_ENTRY,
                )
            )

    def test_missing_fee_vat_rate_fails_closed(
        self,
        repo: FakeSettlementRepository,
        registry: FakeIntegrationsRepository,
        audit: FakeSettlementAudit,
    ) -> None:
        ChannelRegistryService(registry).create_channel(
            INSTALLATION_ID, "zarinpal", "زرین‌پال", "gateway"
        )
        GatewayContractService(registry).create_version(
            INSTALLATION_ID,
            "zarinpal",
            valid_from=datetime.date(2026, 10, 23),
            fee_percent=Decimal("2"),
            fee_vat_rate=None,  # unknown rate
            tranche_legs=(TrancheLeg(0, 5, Decimal("100")),),
            day_lag=1,
        )
        service = SettlementCommandService(repo, registry, audit)
        with pytest.raises(SettlementFeeVatRateUnknownError):
            service.record_payment(
                CreateGatewayPayment(
                    installation_id=INSTALLATION_ID,
                    channel_key="zarinpal",
                    gateway_transaction_id="TXN-9",
                    gross_irr=1_000_000,
                    gateway_confirmed_at=NOW,
                    source=PaymentSource.MANUAL_ENTRY,
                )
            )

    def test_fee_vat_rate_override_used(
        self, commands: SettlementCommandService
    ) -> None:
        result = commands.record_payment(
            CreateGatewayPayment(
                installation_id=INSTALLATION_ID,
                channel_key="snapppay",
                gateway_transaction_id="TXN-OVERRIDE",
                gross_irr=100_000_000,
                gateway_confirmed_at=NOW,
                source=PaymentSource.MANUAL_ENTRY,
                fee_vat_rate_override=Decimal("0"),
            )
        )
        assert result.payment.fee_vat_irr == 0
        assert result.payment.fee_vat_rate_snapshot == Decimal("0")
        assert result.payment.net_irr == 90_000_000

    def test_non_positive_gross_rejected(self, commands: SettlementCommandService) -> None:
        with pytest.raises(SettlementPaymentInvalidError):
            commands.record_payment(
                CreateGatewayPayment(
                    installation_id=INSTALLATION_ID,
                    channel_key="snapppay",
                    gateway_transaction_id="TXN-0",
                    gross_irr=0,
                    gateway_confirmed_at=NOW,
                    source=PaymentSource.MANUAL_ENTRY,
                )
            )


class TestTransitions:
    def test_valid_transition_applies(
        self, commands: SettlementCommandService, repo: FakeSettlementRepository
    ) -> None:
        payment = _record(commands)
        tranche = repo.list_tranches_for_payment(INSTALLATION_ID, payment.id)[0]
        updated = commands.transition_tranche(
            INSTALLATION_ID,
            TransitionTranche(tranche_id=tranche.id, to_state="due"),
        )
        assert updated.state == TrancheState.DUE

    def test_invalid_transition_rejected(
        self, commands: SettlementCommandService, repo: FakeSettlementRepository
    ) -> None:
        payment = _record(commands)
        tranche = repo.list_tranches_for_payment(INSTALLATION_ID, payment.id)[0]
        with pytest.raises(SettlementInvalidTransitionError):
            commands.transition_tranche(
                INSTALLATION_ID,
                TransitionTranche(tranche_id=tranche.id, to_state="settled"),
            )

    def test_unknown_state_rejected(
        self, commands: SettlementCommandService, repo: FakeSettlementRepository
    ) -> None:
        payment = _record(commands)
        tranche = repo.list_tranches_for_payment(INSTALLATION_ID, payment.id)[0]
        with pytest.raises(SettlementInvalidTransitionError):
            commands.transition_tranche(
                INSTALLATION_ID,
                TransitionTranche(tranche_id=tranche.id, to_state="flying"),
            )

    def test_unknown_tranche_rejected(self, commands: SettlementCommandService) -> None:
        with pytest.raises(SettlementTrancheNotFoundError):
            commands.transition_tranche(
                INSTALLATION_ID,
                TransitionTranche(tranche_id="missing", to_state="due"),
            )


class TestCancel:
    def test_cancel_all_open_tranches(
        self, commands: SettlementCommandService, repo: FakeSettlementRepository
    ) -> None:
        payment = _record(commands)
        result = commands.cancel_payment(
            INSTALLATION_ID,
            CancelGatewayPayment(payment_id=payment.id, reason="customer refund"),
        )
        assert result.payment.status == GatewayPaymentStatus.CANCELLED
        assert result.refunds_deducted_irr == 0
        assert result.exception is None
        assert len(result.cancelled_tranche_ids) == 4
        tranches = repo.list_tranches_for_payment(INSTALLATION_ID, payment.id)
        assert all(t.state == TrancheState.CANCELLED for t in tranches)

    def test_cancel_after_partial_settlement_deducts_paid_part(
        self, commands: SettlementCommandService, repo: FakeSettlementRepository
    ) -> None:
        payment = _record(commands)
        tranches = repo.list_tranches_for_payment(INSTALLATION_ID, payment.id)
        first = tranches[0]
        # The first tranche was confirmed + received in full.
        repo.update_tranche(first.id, state=TrancheState.RECEIVED, received_net_irr=22_250_000)
        result = commands.cancel_payment(
            INSTALLATION_ID,
            CancelGatewayPayment(payment_id=payment.id, reason="customer refund"),
        )
        assert result.payment.status == GatewayPaymentStatus.PARTIALLY_CANCELLED
        assert result.refunds_deducted_irr == 22_250_000
        assert result.exception is not None
        assert (
            result.exception.exception_type
            == SettlementExceptionType.CANCELLED_AFTER_SETTLEMENT
        )
        assert len(result.cancelled_tranche_ids) == 3
        kept = repo.get_tranche(INSTALLATION_ID, first.id)
        assert kept is not None
        assert kept.state == TrancheState.RECEIVED

    def test_double_cancel_rejected(self, commands: SettlementCommandService) -> None:
        payment = _record(commands)
        commands.cancel_payment(
            INSTALLATION_ID, CancelGatewayPayment(payment_id=payment.id, reason="r1")
        )
        with pytest.raises(SettlementPaymentInvalidError):
            commands.cancel_payment(
                INSTALLATION_ID, CancelGatewayPayment(payment_id=payment.id, reason="r2")
            )

    def test_unknown_payment_rejected(self, commands: SettlementCommandService) -> None:
        with pytest.raises(SettlementPaymentNotFoundError):
            commands.cancel_payment(
                INSTALLATION_ID, CancelGatewayPayment(payment_id="missing", reason="x")
            )


class TestReassignPeriod:
    def test_open_tranches_rederive_confirmed_untouched(
        self, commands: SettlementCommandService, repo: FakeSettlementRepository
    ) -> None:
        payment = _record(commands)
        tranches = repo.list_tranches_for_payment(INSTALLATION_ID, payment.id)
        first = tranches[0]
        repo.update_tranche(first.id, state=TrancheState.CONFIRMED, confirmed_net_irr=22_250_000)
        old_first_date = first.expected_date
        result = commands.reassign_period(
            INSTALLATION_ID,
            ReassignSalesPeriod(
                payment_id=payment.id,
                new_sales_period_jalali="1405/09",
                reason="gateway file shows Azar",
            ),
        )
        assert result.payment.sales_period_jalali == "1405/09"
        assert len(result.rederived_tranche_ids) == 3
        assert result.untouched_tranche_ids == (first.id,)
        assert result.exception is not None
        assert result.exception.exception_type == SettlementExceptionType.PERIOD_REASSIGNED
        assert result.exception.severity == SettlementExceptionSeverity.INFO
        # Confirmed tranche kept its date; open ones moved a month later.
        kept = repo.get_tranche(INSTALLATION_ID, first.id)
        assert kept is not None
        assert kept.expected_date == old_first_date
        moved = repo.get_tranche(INSTALLATION_ID, tranches[1].id)
        assert moved is not None
        assert moved.sales_period_jalali == "1405/09"
        assert moved.expected_date > tranches[1].expected_date

    def test_same_period_is_noop(
        self, commands: SettlementCommandService, repo: FakeSettlementRepository
    ) -> None:
        payment = _record(commands)
        result = commands.reassign_period(
            INSTALLATION_ID,
            ReassignSalesPeriod(
                payment_id=payment.id,
                new_sales_period_jalali="1405/08",
                reason="no change",
            ),
        )
        assert result.rederived_tranche_ids == ()
        assert result.exception is None
        assert len(repo.exceptions) == 0

    def test_bad_period_format_rejected(self, commands: SettlementCommandService) -> None:
        payment = _record(commands)
        with pytest.raises(SettlementPaymentInvalidError):
            commands.reassign_period(
                INSTALLATION_ID,
                ReassignSalesPeriod(
                    payment_id=payment.id,
                    new_sales_period_jalali="1405-09",
                    reason="bad format",
                ),
            )


class TestWriteOff:
    def test_write_off_requires_approver_and_reason(
        self, commands: SettlementCommandService, repo: FakeSettlementRepository
    ) -> None:
        payment = _record(commands)
        tranche = repo.list_tranches_for_payment(INSTALLATION_ID, payment.id)[0]
        repo.update_tranche(tranche.id, state=TrancheState.DUE)
        with pytest.raises(SettlementWriteOffNotAllowedError):
            commands.write_off_tranche(
                INSTALLATION_ID,
                RecordTrancheWriteOff(tranche_id=tranche.id, approved_by="", reason="x"),
                tenant_id=TENANT_ID,
                api_client_id=None,
            )
        with pytest.raises(SettlementWriteOffNotAllowedError):
            commands.write_off_tranche(
                INSTALLATION_ID,
                RecordTrancheWriteOff(tranche_id=tranche.id, approved_by="ops", reason=" "),
                tenant_id=TENANT_ID,
                api_client_id=None,
            )

    def test_write_off_expected_tranche_rejected(
        self, commands: SettlementCommandService, repo: FakeSettlementRepository
    ) -> None:
        payment = _record(commands)
        tranche = repo.list_tranches_for_payment(INSTALLATION_ID, payment.id)[0]
        with pytest.raises(SettlementWriteOffNotAllowedError):
            commands.write_off_tranche(
                INSTALLATION_ID,
                RecordTrancheWriteOff(
                    tranche_id=tranche.id, approved_by="ops", reason="gateway gone"
                ),
                tenant_id=TENANT_ID,
                api_client_id=None,
            )

    def test_write_off_due_tranche_audits_and_resolves_exceptions(
        self,
        commands: SettlementCommandService,
        repo: FakeSettlementRepository,
        audit: FakeSettlementAudit,
    ) -> None:
        payment = _record(commands)
        tranche = repo.list_tranches_for_payment(INSTALLATION_ID, payment.id)[0]
        repo.update_tranche(tranche.id, state=TrancheState.OVERDUE)
        exc = repo.save_exception(
            CreateSettlementException(
                installation_id=INSTALLATION_ID,
                exception_type=SettlementExceptionType.LATE_TRANCHE.value,
                severity=SettlementExceptionSeverity.WARNING.value,
                tranche_id=tranche.id,
            )
        )
        updated = commands.write_off_tranche(
            INSTALLATION_ID,
            RecordTrancheWriteOff(
                tranche_id=tranche.id, approved_by="ops-lead", reason="gateway insolvent"
            ),
            tenant_id=TENANT_ID,
            api_client_id="client-1",
        )
        assert updated.state == TrancheState.WRITTEN_OFF
        resolved = repo.get_exception(INSTALLATION_ID, exc.id)
        assert resolved is not None
        assert resolved.status == SettlementExceptionStatus.WRITTEN_OFF
        assert resolved.resolution == SettlementExceptionResolution.WRITE_OFF
        assert len(audit.entries) == 1
        assert audit.entries[0]["action"] == "settlement.tranche.write_off"
        assert audit.entries[0]["actor"] == "ops-lead"
        assert audit.entries[0]["metadata"]["amount_irr"] == 22_250_000


class TestResolveException:
    def test_resolve_open_exception(
        self, commands: SettlementCommandService, repo: FakeSettlementRepository
    ) -> None:
        exc = repo.save_exception(
            CreateSettlementException(
                installation_id=INSTALLATION_ID,
                exception_type=SettlementExceptionType.AMOUNT_MISMATCH.value,
                severity=SettlementExceptionSeverity.WARNING.value,
            )
        )
        resolved = commands.resolve_exception(
            INSTALLATION_ID,
            ResolveSettlementException(
                exception_id=exc.id,
                resolution="adjusted",
                resolved_by="ops",
                note="adjusted by 500 IRR",
            ),
        )
        assert resolved.status == SettlementExceptionStatus.RESOLVED
        assert resolved.resolution == SettlementExceptionResolution.ADJUSTED
        assert resolved.resolved_by == "ops"
        assert resolved.resolved_at is not None

    def test_resolve_twice_rejected(
        self, commands: SettlementCommandService, repo: FakeSettlementRepository
    ) -> None:
        exc = repo.save_exception(
            CreateSettlementException(
                installation_id=INSTALLATION_ID,
                exception_type=SettlementExceptionType.AMOUNT_MISMATCH.value,
                severity=SettlementExceptionSeverity.WARNING.value,
            )
        )
        commands.resolve_exception(
            INSTALLATION_ID,
            ResolveSettlementException(
                exception_id=exc.id, resolution="confirmed_ok", resolved_by="ops"
            ),
        )
        with pytest.raises(SettlementInvalidTransitionError):
            commands.resolve_exception(
                INSTALLATION_ID,
                ResolveSettlementException(
                    exception_id=exc.id, resolution="adjusted", resolved_by="ops"
                ),
            )

    def test_unknown_exception_rejected(self, commands: SettlementCommandService) -> None:
        with pytest.raises(SettlementExceptionNotFoundError):
            commands.resolve_exception(
                INSTALLATION_ID,
                ResolveSettlementException(
                    exception_id="missing", resolution="adjusted", resolved_by="ops"
                ),
            )


class TestBulkRederive:
    def test_rederives_payment_on_old_version(
        self,
        commands: SettlementCommandService,
        repo: FakeSettlementRepository,
        registry: FakeIntegrationsRepository,
        audit: FakeSettlementAudit,
    ) -> None:
        payment = _record(commands)
        # A rate change creates version 2 (fee 12% now).
        GatewayContractService(registry).create_version(
            INSTALLATION_ID,
            "snapppay",
            valid_from=datetime.date(2026, 11, 7),
            fee_percent=Decimal("12"),
            fee_vat_rate=Decimal("10"),
            tranche_legs=FOUR_TRANCHES,
            payout_window_start_day=1,
            payout_window_end_day=5,
        )
        result = commands.bulk_rederive(
            INSTALLATION_ID,
            tenant_id=TENANT_ID,
            api_client_id=None,
            actor="ops",
            reason="rate change, re-derive open schedules",
        )
        assert result.rederived_payment_ids == (payment.id,)
        assert result.skipped == ()
        updated = repo.get_payment(INSTALLATION_ID, payment.id)
        assert updated is not None
        assert updated.fee_rate_snapshot == Decimal("12")
        assert updated.fee_irr == 12_000_000
        assert updated.net_irr == 86_800_000
        assert len(audit.entries) == 1
        assert audit.entries[0]["action"] == "settlement.bulk_rederive"

    def test_skips_payment_with_closed_tranche(
        self,
        commands: SettlementCommandService,
        repo: FakeSettlementRepository,
        registry: FakeIntegrationsRepository,
    ) -> None:
        payment = _record(commands)
        tranches = repo.list_tranches_for_payment(INSTALLATION_ID, payment.id)
        repo.update_tranche(tranches[0].id, state=TrancheState.CONFIRMED)
        GatewayContractService(registry).create_version(
            INSTALLATION_ID,
            "snapppay",
            valid_from=datetime.date(2026, 11, 7),
            fee_percent=Decimal("12"),
            fee_vat_rate=Decimal("10"),
            tranche_legs=FOUR_TRANCHES,
        )
        result = commands.bulk_rederive(
            INSTALLATION_ID,
            tenant_id=TENANT_ID,
            api_client_id=None,
            actor="ops",
            reason="rate change",
        )
        assert result.rederived_payment_ids == ()
        assert len(result.skipped) == 1
        assert "confirmed/received" in result.skipped[0][1]

    def test_requires_actor_and_reason(self, commands: SettlementCommandService) -> None:
        with pytest.raises(SettlementPaymentInvalidError):
            commands.bulk_rederive(
                INSTALLATION_ID, tenant_id=TENANT_ID, api_client_id=None, actor="", reason="x"
            )
        with pytest.raises(SettlementPaymentInvalidError):
            commands.bulk_rederive(
                INSTALLATION_ID, tenant_id=TENANT_ID, api_client_id=None, actor="ops", reason=""
            )


class TestDailyTick:
    def _tick_tranche(
        self,
        repo: FakeSettlementRepository,
        *,
        state: TrancheState,
        expected_date: datetime.date,
        grace: int,
    ) -> SettlementTranche:
        payment = _record_for_tick(repo)
        tranche = SettlementTranche(
            id=_new_id(),
            installation_id=INSTALLATION_ID,
            gateway_payment_id=payment.id,
            tranche_index=1,
            tranche_count=1,
            sales_period_jalali="1405/08",
            expected_window_start=expected_date,
            expected_date=expected_date,
            gross_irr=25_000_000,
            fee_irr=2_500_000,
            fee_vat_irr=250_000,
            net_irr=22_250_000,
            state=state,
        )
        repo.tranches[tranche.id] = tranche
        repo.tick_candidates.append((tranche, grace))
        return tranche

    def test_expected_moves_to_due(
        self, commands: SettlementCommandService, repo: FakeSettlementRepository
    ) -> None:
        tranche = self._tick_tranche(
            repo, state=TrancheState.EXPECTED, expected_date=datetime.date(2026, 11, 26), grace=3
        )
        report = commands.run_daily_tick(datetime.date(2026, 11, 26))
        assert report.moved_to_due == 1
        assert report.moved_to_overdue == 0
        moved = repo.get_tranche(INSTALLATION_ID, tranche.id)
        assert moved is not None
        assert moved.state == TrancheState.DUE

    def test_due_moves_to_overdue_past_grace_and_opens_exception(
        self, commands: SettlementCommandService, repo: FakeSettlementRepository
    ) -> None:
        tranche = self._tick_tranche(
            repo, state=TrancheState.DUE, expected_date=datetime.date(2026, 11, 26), grace=3
        )
        # 2026-11-30 is 4 days past due, grace is 3 → overdue.
        report = commands.run_daily_tick(datetime.date(2026, 11, 30))
        assert report.moved_to_overdue == 1
        assert report.exceptions_created == 1
        overdue = repo.get_tranche(INSTALLATION_ID, tranche.id)
        assert overdue is not None
        assert overdue.state == TrancheState.OVERDUE
        exc = repo.get_open_exception_for_tranche(
            INSTALLATION_ID, tranche.id, SettlementExceptionType.LATE_TRANCHE.value
        )
        assert exc is not None
        assert exc.escalation_level == 0  # 4 days < 7
        assert exc.expected_amount_irr == 22_250_000

    def test_due_within_grace_stays_due(
        self, commands: SettlementCommandService, repo: FakeSettlementRepository
    ) -> None:
        tranche = self._tick_tranche(
            repo, state=TrancheState.DUE, expected_date=datetime.date(2026, 11, 26), grace=3
        )
        report = commands.run_daily_tick(datetime.date(2026, 11, 29))
        assert report.moved_to_overdue == 0
        assert report.exceptions_created == 0
        still_due = repo.get_tranche(INSTALLATION_ID, tranche.id)
        assert still_due is not None
        assert still_due.state == TrancheState.DUE

    def test_overdue_escalates_at_thresholds(
        self, commands: SettlementCommandService, repo: FakeSettlementRepository
    ) -> None:
        tranche = self._tick_tranche(
            repo, state=TrancheState.OVERDUE, expected_date=datetime.date(2026, 11, 26), grace=3
        )
        existing = repo.save_exception(
            CreateSettlementException(
                installation_id=INSTALLATION_ID,
                exception_type=SettlementExceptionType.LATE_TRANCHE.value,
                severity=SettlementExceptionSeverity.WARNING.value,
                tranche_id=tranche.id,
                escalation_level=0,
            )
        )
        # 30 days past due → level 2.
        report = commands.run_daily_tick(datetime.date(2026, 12, 26))
        assert report.exceptions_created == 0
        assert report.exceptions_escalated == 1
        escalated = repo.get_exception(INSTALLATION_ID, existing.id)
        assert escalated is not None
        assert escalated.escalation_level == 2


class TestDailyTickReviewFiling:
    """The tick files new late tranches in the review queue (US-555)."""

    @pytest.fixture()
    def queue(self) -> FakeReviewQueue:
        return FakeReviewQueue()

    @pytest.fixture()
    def filing_commands(
        self,
        repo: FakeSettlementRepository,
        registry: FakeIntegrationsRepository,
        audit: FakeSettlementAudit,
        queue: FakeReviewQueue,
    ) -> SettlementCommandService:
        review = ReviewQueueCommandService(queue, queue, now=NOW)
        resolver = FakeTenantResolver({INSTALLATION_ID: TENANT_ID})
        return SettlementCommandService(
            repo, registry, audit, review=review, tenant_resolver=resolver
        )

    def _due_tranche(
        self,
        repo: FakeSettlementRepository,
        commands: SettlementCommandService,
    ) -> SettlementTranche:
        """A real payment's first tranche, DUE and past grace on 2026-11-30."""
        payment = _record(commands)
        tranches = repo.list_tranches_for_payment(INSTALLATION_ID, payment.id)
        assert tranches[0].expected_date == datetime.date(2026, 11, 26)
        assert tranches[0].net_irr == 22_250_000
        due = repo.update_tranche(tranches[0].id, state=TrancheState.DUE)
        assert due is not None
        repo.tick_candidates.append((due, 3))
        return due

    def test_tick_files_review_item_for_new_late_tranche(
        self,
        filing_commands: SettlementCommandService,
        repo: FakeSettlementRepository,
        queue: FakeReviewQueue,
    ) -> None:
        tranche = self._due_tranche(repo, filing_commands)
        report = filing_commands.run_daily_tick(datetime.date(2026, 11, 30))

        assert report.moved_to_overdue == 1
        assert report.exceptions_created == 1
        assert report.review_items_filed == 1
        assert len(queue.items) == 1
        item = next(iter(queue.items.values()))
        assert item.tenant_id == TENANT_ID
        assert item.item_type == ReviewItemType.SETTLEMENT_VARIANCE
        assert item.subtype == SettlementExceptionType.LATE_TRANCHE.value
        assert item.severity == ReviewSeverity.WARNING
        assert item.status == ReviewItemStatus.OPEN
        assert item.channel == "snapppay"
        assert item.amount_irr == 22_250_000
        assert item.source_event_ref is not None
        assert item.source_event_ref.startswith("settlement-exception:")
        # The reason carries the tranche + gateway + amount context.
        assert "قسط 1 از 4" in item.reason
        assert "snapppay" in item.reason
        assert "22,250,000" in item.reason
        assert "4 روز" in item.reason
        assert "2026-11-26" in item.reason
        assert item.suggested_action
        # The filing left a "filed" audit entry.
        assert [entry.action for entry in queue.audit] == ["filed"]
        assert queue.audit[0].item_id == item.id
        # The review item points at the exception the tick opened.
        exc = repo.get_open_exception_for_tranche(
            INSTALLATION_ID, tranche.id, SettlementExceptionType.LATE_TRANCHE.value
        )
        assert exc is not None
        assert item.source_event_ref == f"settlement-exception:{exc.id}"

    def test_re_tick_files_no_duplicate(
        self,
        filing_commands: SettlementCommandService,
        repo: FakeSettlementRepository,
        queue: FakeReviewQueue,
    ) -> None:
        self._due_tranche(repo, filing_commands)
        first = filing_commands.run_daily_tick(datetime.date(2026, 11, 30))
        assert first.review_items_filed == 1
        assert len(queue.items) == 1

        second = filing_commands.run_daily_tick(datetime.date(2026, 11, 30))
        assert second.exceptions_created == 0
        assert second.review_items_filed == 0
        assert len(queue.items) == 1

    def test_escalation_only_tick_files_nothing(
        self,
        filing_commands: SettlementCommandService,
        repo: FakeSettlementRepository,
        queue: FakeReviewQueue,
    ) -> None:
        tranche = self._due_tranche(repo, filing_commands)
        repo.save_exception(
            CreateSettlementException(
                installation_id=INSTALLATION_ID,
                exception_type=SettlementExceptionType.LATE_TRANCHE.value,
                severity=SettlementExceptionSeverity.WARNING.value,
                tranche_id=tranche.id,
                escalation_level=0,
            )
        )
        # 30 days past due → level 2: escalation only, no new item.
        report = filing_commands.run_daily_tick(datetime.date(2026, 12, 26))
        assert report.exceptions_created == 0
        assert report.exceptions_escalated == 1
        assert report.review_items_filed == 0
        assert queue.items == {}

    def test_resolved_exception_refiles_on_later_tick(
        self,
        filing_commands: SettlementCommandService,
        repo: FakeSettlementRepository,
        queue: FakeReviewQueue,
    ) -> None:
        tranche = self._due_tranche(repo, filing_commands)
        first = filing_commands.run_daily_tick(datetime.date(2026, 11, 30))
        assert first.review_items_filed == 1
        exc = repo.get_open_exception_for_tranche(
            INSTALLATION_ID, tranche.id, SettlementExceptionType.LATE_TRANCHE.value
        )
        assert exc is not None
        repo.update_exception(exc.id, status=SettlementExceptionStatus.RESOLVED.value)

        # The tranche is still overdue and no exception is open: the
        # next tick opens a NEW exception and files a NEW item.
        second = filing_commands.run_daily_tick(datetime.date(2026, 12, 1))
        assert second.exceptions_created == 1
        assert second.review_items_filed == 1
        assert len(queue.items) == 2

    def test_unresolvable_tenant_keeps_exception_without_filing(
        self,
        repo: FakeSettlementRepository,
        registry: FakeIntegrationsRepository,
        audit: FakeSettlementAudit,
        queue: FakeReviewQueue,
    ) -> None:
        review = ReviewQueueCommandService(queue, queue, now=NOW)
        commands = SettlementCommandService(
            repo, registry, audit, review=review, tenant_resolver=FakeTenantResolver({})
        )
        self._due_tranche(repo, commands)
        report = commands.run_daily_tick(datetime.date(2026, 11, 30))
        assert report.exceptions_created == 1
        assert report.review_items_filed == 0
        assert queue.items == {}

    def test_unwired_review_keeps_exception_without_filing(
        self, commands: SettlementCommandService, repo: FakeSettlementRepository
    ) -> None:
        self._due_tranche(repo, commands)
        report = commands.run_daily_tick(datetime.date(2026, 11, 30))
        assert report.exceptions_created == 1
        assert report.review_items_filed == 0


def _record_for_tick(repo: FakeSettlementRepository) -> GatewayPayment:
    """Minimal payment row for tick tests (no derivation involved)."""
    payment = GatewayPayment(
        id=_new_id(),
        installation_id=INSTALLATION_ID,
        payment_channel_id="channel-1",
        gateway_transaction_id=f"TXN-{uuid.uuid4().hex[:8]}",
        gross_irr=100_000_000,
        fee_irr=10_000_000,
        fee_vat_irr=1_000_000,
        net_irr=89_000_000,
        contract_version_id="contract-1",
        fee_rate_snapshot=Decimal("10"),
        fee_vat_rate_snapshot=Decimal("10"),
        sales_period_jalali="1405/08",
        gateway_confirmed_at=NOW,
        source=PaymentSource.STORE_WEBHOOK,
    )
    repo.payments[payment.id] = payment
    return payment


class TestQueryService:
    def test_get_payment_schedule(
        self,
        commands: SettlementCommandService,
        queries: SettlementQueryService,
    ) -> None:
        payment = _record(commands)
        schedule = queries.get_payment_schedule(INSTALLATION_ID, payment.id)
        assert schedule.payment.id == payment.id
        assert len(schedule.tranches) == 4

    def test_unknown_payment_raises(
        self, queries: SettlementQueryService
    ) -> None:
        with pytest.raises(SettlementPaymentNotFoundError):
            queries.get_payment(INSTALLATION_ID, "missing")

    def test_list_tranches_filters_by_state(
        self,
        commands: SettlementCommandService,
        queries: SettlementQueryService,
        repo: FakeSettlementRepository,
    ) -> None:
        payment = _record(commands)
        tranches = repo.list_tranches_for_payment(INSTALLATION_ID, payment.id)
        repo.update_tranche(tranches[0].id, state=TrancheState.DUE)
        due = queries.list_tranches(INSTALLATION_ID, state=TrancheState.DUE)
        assert len(due) == 1
        expected = queries.list_tranches(INSTALLATION_ID, state=TrancheState.EXPECTED)
        assert len(expected) == 3

    def test_unknown_channel_filter_raises(
        self, queries: SettlementQueryService
    ) -> None:
        with pytest.raises(IntegrationChannelNotFoundError):
            queries.list_payments(INSTALLATION_ID, channel_key="nope")

    def test_other_installation_is_invisible(
        self, commands: SettlementCommandService, queries: SettlementQueryService
    ) -> None:
        payment = _record(commands)
        with pytest.raises(SettlementPaymentNotFoundError):
            queries.get_payment("00000000-0000-0000-0000-000000000999", payment.id)
