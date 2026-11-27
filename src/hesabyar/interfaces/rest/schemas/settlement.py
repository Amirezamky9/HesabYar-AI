"""Request/response schemas for the settlement endpoints (US-521 + US-523).

Responses map from the domain views — no ORM object ever leaks.
``fee_percent`` / ``fee_vat_rate`` / ``match_confidence`` follow the
§9.3 contract: Decimals that serialize as decimal strings (never a
binary float — see the tax-policy schemas). Trailing zeros are
stripped at the mapping boundary. All amounts are integer IRR.

Settlement-file uploads are base64 JSON (like the bank imports):
the admin BFF proxy parses request bodies as JSON only, so the file
travels as a base64 string inside the request body (max 10 MB
decoded). File bytes are never stored — only the filename, SHA-256
hash and parsed rows.
"""

from __future__ import annotations

import base64
import binascii
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator

from hesabyar.application.commands.settlement import (
    BulkRederiveResult,
    CancelPaymentResult,
    ReassignPeriodResult,
    RecordPaymentResult,
    TickReport,
)
from hesabyar.application.commands.settlement_import import (
    ApproveImportResult,
    ImportPreview,
    ImportSettlementFileResult,
    RecordDepositResult,
    TieOutRunResult,
)
from hesabyar.application.commands.settlement_posting import (
    BankReceiptResult,
    ClearingResult,
    FeeVarianceResult,
    RefundResult,
    SaleFeeResult,
)
from hesabyar.domain.settlement.matching import TieOutResult
from hesabyar.domain.settlement.models import (
    BankDeposit,
    GatewayPayment,
    SettlementEvent,
    SettlementException,
    SettlementFeeRecord,
    SettlementFileImport,
    SettlementFileRow,
    SettlementMatch,
    SettlementPosting,
    SettlementTranche,
)

#: Decoded upload size cap (matches the file reader's MAX_FILE_BYTES).
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def _decode_base64(value: str) -> bytes:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("content_base64 is not valid base64") from exc
    if len(decoded) > MAX_UPLOAD_BYTES:
        raise ValueError(f"file exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload limit")
    return decoded


def _clean_decimal(value: Decimal) -> Decimal:
    """Strip trailing zeros so the §9.3 decimal-string form is clean."""
    return Decimal(format(value.normalize(), "f"))


def _clean_optional_decimal(value: Decimal | None) -> Decimal | None:
    """Strip trailing zeros, preserving None."""
    return _clean_decimal(value) if value is not None else None


# -- gateway payments -----------------------------------------------------


class GatewayPaymentResponse(BaseModel):
    """One gateway payment: the money a gateway owes the merchant."""

    id: str = Field(description="Payment row id")
    payment_channel_id: str = Field(description="Owning payment channel row id")
    gateway_transaction_id: str = Field(description="Gateway-side transaction id")
    store_order_id: str | None = Field(description="Store order id, when any")
    store_payment_id: str | None = Field(description="Store payment id, when any")
    tryton_invoice_id: int | None = Field(description="Tryton invoice id, when linked")
    tryton_journal_id: int | None = Field(description="Tryton sale journal id, when linked")
    customer_party_id: int | None = Field(description="Tryton customer party id, when known")
    gross_irr: int = Field(description="Sale amount in whole rials")
    fee_irr: int = Field(description="Gateway fee in whole rials")
    fee_vat_irr: int = Field(description="VAT on the fee in whole rials")
    net_irr: int = Field(description="Net receivable from the gateway in whole rials")
    contract_version_id: str = Field(description="Contract version the schedule was derived from")
    fee_rate_snapshot: Decimal = Field(description="Fee percent applied (10 means 10%)")
    fee_vat_rate_snapshot: Decimal | None = Field(description="Fee VAT rate applied (10 means 10%)")
    sales_period_jalali: str = Field(description="Jalali sales period 'YYYY/MM'")
    gateway_confirmed_at: datetime = Field(description="When the gateway confirmed the transaction")
    source: str = Field(description="Payment origin")
    status: str = Field(description="Payment lifecycle status")
    refunds_deducted_irr: int = Field(description="Amount deducted from later settlements after a cancel")
    created_at: datetime | None = Field(description="Row creation timestamp (UTC)")
    updated_at: datetime | None = Field(description="Last-modified timestamp (UTC)")

    @classmethod
    def from_payment(cls, payment: GatewayPayment) -> GatewayPaymentResponse:
        """Map a domain payment to its response."""
        return cls(
            id=payment.id,
            payment_channel_id=payment.payment_channel_id,
            gateway_transaction_id=payment.gateway_transaction_id,
            store_order_id=payment.store_order_id,
            store_payment_id=payment.store_payment_id,
            tryton_invoice_id=payment.tryton_invoice_id,
            tryton_journal_id=payment.tryton_journal_id,
            customer_party_id=payment.customer_party_id,
            gross_irr=payment.gross_irr,
            fee_irr=payment.fee_irr,
            fee_vat_irr=payment.fee_vat_irr,
            net_irr=payment.net_irr,
            contract_version_id=payment.contract_version_id,
            fee_rate_snapshot=_clean_decimal(payment.fee_rate_snapshot),
            fee_vat_rate_snapshot=_clean_optional_decimal(payment.fee_vat_rate_snapshot),
            sales_period_jalali=payment.sales_period_jalali,
            gateway_confirmed_at=payment.gateway_confirmed_at,
            source=payment.source.value,
            status=payment.status.value,
            refunds_deducted_irr=payment.refunds_deducted_irr,
            created_at=payment.created_at,
            updated_at=payment.updated_at,
        )


class CreateGatewayPaymentRequest(BaseModel):
    """Record a gateway payment and derive its tranche schedule."""

    channel_key: str = Field(description="Channel slug, e.g. 'snapppay'")
    gateway_transaction_id: str = Field(description="Gateway-side transaction id")
    gross_irr: int = Field(gt=0, description="Sale amount in whole rials (must be positive)")
    gateway_confirmed_at: datetime = Field(
        description="When the gateway confirmed the transaction (the schedule anchor)"
    )
    source: str = Field(
        description="Payment origin: 'store_webhook' | 'gateway_poll' | 'manual_import' | 'manual_entry'"
    )
    store_order_id: str | None = Field(default=None, description="Store order id, when any")
    store_payment_id: str | None = Field(default=None, description="Store payment id, when any")
    tryton_invoice_id: int | None = Field(default=None, description="Tryton invoice id, when linked")
    tryton_journal_id: int | None = Field(default=None, description="Tryton sale journal id, when linked")
    customer_party_id: int | None = Field(default=None, description="Tryton customer party id, when known")
    canonical_event_id: str | None = Field(default=None, description="Inbox event id this payment was recorded from")
    fee_vat_rate_override: Decimal | None = Field(
        default=None, description="Explicit fee-VAT rate for this payment (overrides the contract snapshot)"
    )


class CancelGatewayPaymentRequest(BaseModel):
    """Cancel/refund a gateway payment."""

    reason: str = Field(min_length=1, description="Why the payment is cancelled")
    actor: str | None = Field(default=None, description="Actor performing the cancel")


class CancelGatewayPaymentResponse(BaseModel):
    """A cancelled payment: what was cancelled, what was already paid."""

    payment: GatewayPaymentResponse = Field(description="The payment after the cancel")
    cancelled_tranche_ids: list[str] = Field(description="Open tranches that were cancelled")
    refunds_deducted_irr: int = Field(description="Already-paid net the gateway deducts from later settlements")
    exception_id: str | None = Field(description="cancelled_after_settlement exception id, when any")

    @classmethod
    def from_result(cls, result: CancelPaymentResult) -> CancelGatewayPaymentResponse:
        """Map a cancel result to its response."""
        return cls(
            payment=GatewayPaymentResponse.from_payment(result.payment),
            cancelled_tranche_ids=list(result.cancelled_tranche_ids),
            refunds_deducted_irr=result.refunds_deducted_irr,
            exception_id=result.exception.id if result.exception is not None else None,
        )


class ReassignSalesPeriodRequest(BaseModel):
    """Correct a payment's sales period (gateway file disagreed)."""

    new_sales_period_jalali: str = Field(description="Corrected Jalali sales period 'YYYY/MM'")
    reason: str = Field(min_length=1, description="Why the period is corrected")
    actor: str | None = Field(default=None, description="Actor performing the correction")


class ReassignSalesPeriodResponse(BaseModel):
    """A period correction: which tranches moved, which were untouched."""

    payment: GatewayPaymentResponse = Field(description="The payment after the correction")
    rederived_tranche_ids: list[str] = Field(description="Open tranches whose due dates were re-derived")
    untouched_tranche_ids: list[str] = Field(description="Confirmed/received tranches, left untouched")
    exception_id: str | None = Field(description="Informational period_reassigned exception id, when any")

    @classmethod
    def from_result(cls, result: ReassignPeriodResult) -> ReassignSalesPeriodResponse:
        """Map a reassign result to its response."""
        return cls(
            payment=GatewayPaymentResponse.from_payment(result.payment),
            rederived_tranche_ids=list(result.rederived_tranche_ids),
            untouched_tranche_ids=list(result.untouched_tranche_ids),
            exception_id=result.exception.id if result.exception is not None else None,
        )


class BulkRederiveRequest(BaseModel):
    """Re-derive open schedules from the ACTIVE contract version.

    Explicit user action only: ``actor`` + ``reason`` are mandatory,
    and the run writes one audit-log entry.
    """

    channel_key: str | None = Field(default=None, description="Limit to one channel (None = all channels)")
    sales_period_jalali: str | None = Field(default=None, description="Limit to one Jalali sales period 'YYYY/MM'")
    actor: str = Field(min_length=1, description="Actor requesting the re-derivation")
    reason: str = Field(min_length=1, description="Why the schedules are re-derived")


class BulkRederiveSkipped(BaseModel):
    """One payment left untouched by a bulk re-derivation, with its reason."""

    payment_id: str = Field(description="Payment row id")
    reason: str = Field(description="Why the payment was skipped")


class BulkRederiveResponse(BaseModel):
    """A bulk re-derivation run: re-derived payments + skips with reasons."""

    rederived_payment_ids: list[str] = Field(description="Payments re-derived from the active contract")
    skipped: list[BulkRederiveSkipped] = Field(description="Payments left untouched, with reasons")

    @classmethod
    def from_result(cls, result: BulkRederiveResult) -> BulkRederiveResponse:
        """Map a bulk re-derive result to its response."""
        return cls(
            rederived_payment_ids=list(result.rederived_payment_ids),
            skipped=[
                BulkRederiveSkipped(payment_id=payment_id, reason=reason)
                for payment_id, reason in result.skipped
            ],
        )


# -- tranches -------------------------------------------------------------


class SettlementTrancheResponse(BaseModel):
    """One expected settlement instalment of one gateway payment."""

    id: str = Field(description="Tranche row id")
    gateway_payment_id: str = Field(description="Owning gateway payment row id")
    tranche_index: int = Field(description="1-based tranche number within the payment")
    tranche_count: int = Field(description="Total tranches of the payment's schedule")
    sales_period_jalali: str = Field(description="Jalali sales period 'YYYY/MM'")
    expected_window_start: date = Field(description="First day of the payout window")
    expected_date: date = Field(description="Due date (also the Tryton receivable line's maturity_date)")
    gross_irr: int = Field(description="Tranche gross share in whole rials")
    fee_irr: int = Field(description="Tranche fee share in whole rials")
    fee_vat_irr: int = Field(description="Tranche fee-VAT share in whole rials")
    net_irr: int = Field(description="Tranche net share in whole rials (expected settlement amount)")
    confirmed_net_irr: int = Field(description="Net amount confirmed by the gateway so far")
    received_net_irr: int = Field(description="Net amount matched to bank deposits so far")
    outstanding_net_irr: int = Field(description="Net amount not yet received")
    state: str = Field(description="Tranche state (§3.3)")
    tryton_receivable_line_id: int | None = Field(description="Tryton receivable move-line id, when linked")
    variance_irr: int | None = Field(description="Signed variance (actual - expected) in rials, when any")
    variance_reason: str | None = Field(description="Why the tranche differs from the schedule")
    match_method: str | None = Field(description="C5 matching rule that matched this tranche")
    match_confidence: Decimal | None = Field(description="Match confidence 0..1")
    created_at: datetime | None = Field(description="Row creation timestamp (UTC)")
    updated_at: datetime | None = Field(description="Last-modified timestamp (UTC)")

    @classmethod
    def from_tranche(cls, tranche: SettlementTranche) -> SettlementTrancheResponse:
        """Map a domain tranche to its response."""
        return cls(
            id=tranche.id,
            gateway_payment_id=tranche.gateway_payment_id,
            tranche_index=tranche.tranche_index,
            tranche_count=tranche.tranche_count,
            sales_period_jalali=tranche.sales_period_jalali,
            expected_window_start=tranche.expected_window_start,
            expected_date=tranche.expected_date,
            gross_irr=tranche.gross_irr,
            fee_irr=tranche.fee_irr,
            fee_vat_irr=tranche.fee_vat_irr,
            net_irr=tranche.net_irr,
            confirmed_net_irr=tranche.confirmed_net_irr,
            received_net_irr=tranche.received_net_irr,
            outstanding_net_irr=tranche.outstanding_net_irr,
            state=tranche.state.value,
            tryton_receivable_line_id=tranche.tryton_receivable_line_id,
            variance_irr=tranche.variance_irr,
            variance_reason=tranche.variance_reason,
            match_method=tranche.match_method,
            match_confidence=_clean_optional_decimal(tranche.match_confidence),
            created_at=tranche.created_at,
            updated_at=tranche.updated_at,
        )


class RecordPaymentResponse(BaseModel):
    """A recorded payment with its derived schedule."""

    payment: GatewayPaymentResponse = Field(description="The recorded payment")
    tranches: list[SettlementTrancheResponse] = Field(description="The derived tranche schedule")
    fee_record_id: str = Field(description="Fee record id for the payment")

    @classmethod
    def from_result(cls, result: RecordPaymentResult) -> RecordPaymentResponse:
        """Map a record-payment result to its response."""
        return cls(
            payment=GatewayPaymentResponse.from_payment(result.payment),
            tranches=[SettlementTrancheResponse.from_tranche(t) for t in result.tranches],
            fee_record_id=result.fee_record.id,
        )


class PaymentScheduleResponse(BaseModel):
    """A gateway payment with its full tranche schedule."""

    payment: GatewayPaymentResponse = Field(description="The payment")
    tranches: list[SettlementTrancheResponse] = Field(description="The payment's tranches, by index")


class WriteOffTrancheRequest(BaseModel):
    """Write off one tranche — human approval only."""

    approved_by: str = Field(min_length=1, description="Named approver of the write-off")
    reason: str = Field(min_length=1, description="Why the tranche is written off")


# -- exceptions + fee records ---------------------------------------------


class SettlementExceptionResponse(BaseModel):
    """One settlement exception (§3.7)."""

    id: str = Field(description="Exception row id")
    exception_type: str = Field(description="Exception type (§3.7)")
    severity: str = Field(description="Severity: 'info' | 'warning' | 'critical'")
    status: str = Field(description="Lifecycle: 'open' | 'in_review' | 'resolved' | 'written_off'")
    tranche_id: str | None = Field(description="Related tranche, when any")
    settlement_event_id: str | None = Field(description="Related settlement event, when any")
    bank_deposit_id: str | None = Field(description="Related bank deposit, when any")
    gateway_payment_id: str | None = Field(description="Related gateway payment, when any")
    expected_amount_irr: int | None = Field(description="Expected amount in whole rials, when any")
    actual_amount_irr: int | None = Field(description="Actual amount in whole rials, when any")
    escalation_level: int = Field(description="Overdue escalation level (0=none, 1=7d, 2=30d, 3=60d)")
    resolution: str | None = Field(description="How it was resolved, when resolved")
    resolved_by: str | None = Field(description="Actor that resolved the exception")
    resolved_at: datetime | None = Field(description="When the exception was resolved (UTC)")
    note: str | None = Field(description="Operator note")
    created_at: datetime | None = Field(description="Row creation timestamp (UTC)")
    updated_at: datetime | None = Field(description="Last-modified timestamp (UTC)")

    @classmethod
    def from_exception(cls, exc: SettlementException) -> SettlementExceptionResponse:
        """Map a domain exception to its response."""
        return cls(
            id=exc.id,
            exception_type=exc.exception_type.value,
            severity=exc.severity.value,
            status=exc.status.value,
            tranche_id=exc.tranche_id,
            settlement_event_id=exc.settlement_event_id,
            bank_deposit_id=exc.bank_deposit_id,
            gateway_payment_id=exc.gateway_payment_id,
            expected_amount_irr=exc.expected_amount_irr,
            actual_amount_irr=exc.actual_amount_irr,
            escalation_level=exc.escalation_level,
            resolution=exc.resolution.value if exc.resolution is not None else None,
            resolved_by=exc.resolved_by,
            resolved_at=exc.resolved_at,
            note=exc.note,
            created_at=exc.created_at,
            updated_at=exc.updated_at,
        )


class ResolveSettlementExceptionRequest(BaseModel):
    """Resolve an open settlement exception."""

    resolution: str = Field(
        description="How it was resolved: 'confirmed_ok' | 'adjusted' | 'write_off' | "
        "'escalated_to_gateway' | 'rescheduled'"
    )
    resolved_by: str = Field(min_length=1, description="Actor resolving the exception")
    note: str | None = Field(default=None, description="Resolution note")


class SettlementFeeRecordResponse(BaseModel):
    """Fee tracking for one payment (and optionally one tranche) (§3.6)."""

    id: str = Field(description="Fee record row id")
    gateway_payment_id: str = Field(description="Owning gateway payment row id")
    tranche_id: str | None = Field(description="Tranche this record is about (NULL = whole payment)")
    contract_version_id: str = Field(description="Contract version the expected figures came from")
    fee_percent_applied: Decimal = Field(description="Fee percent applied (10 means 10%)")
    fee_vat_rate_applied: Decimal | None = Field(description="Fee VAT rate applied (10 means 10%)")
    expected_fee_irr: int = Field(description="Expected fee in whole rials (from the contract)")
    actual_fee_irr: int | None = Field(description="Actual fee in whole rials (from the gateway file/portal)")
    fee_variance_irr: int | None = Field(description="Fee variance (actual - expected) in whole rials")
    expected_vat_irr: int = Field(description="Expected fee VAT in whole rials (from the contract)")
    actual_vat_irr: int | None = Field(description="Actual fee VAT in whole rials (from the gateway file/portal)")
    vat_variance_irr: int | None = Field(description="Fee-VAT variance (actual - expected) in whole rials")
    tryton_fee_journal_id: int | None = Field(description="Tryton fee journal id, when posted")
    source: str = Field(description="Figure origin: 'calculated' | 'file' | 'portal_statement'")
    created_at: datetime | None = Field(description="Row creation timestamp (UTC)")
    updated_at: datetime | None = Field(description="Last-modified timestamp (UTC)")

    @classmethod
    def from_fee_record(cls, record: SettlementFeeRecord) -> SettlementFeeRecordResponse:
        """Map a domain fee record to its response."""
        return cls(
            id=record.id,
            gateway_payment_id=record.gateway_payment_id,
            tranche_id=record.tranche_id,
            contract_version_id=record.contract_version_id,
            fee_percent_applied=_clean_decimal(record.fee_percent_applied),
            fee_vat_rate_applied=_clean_optional_decimal(record.fee_vat_rate_applied),
            expected_fee_irr=record.expected_fee_irr,
            actual_fee_irr=record.actual_fee_irr,
            fee_variance_irr=record.fee_variance_irr,
            expected_vat_irr=record.expected_vat_irr,
            actual_vat_irr=record.actual_vat_irr,
            vat_variance_irr=record.vat_variance_irr,
            tryton_fee_journal_id=record.tryton_fee_journal_id,
            source=record.source,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


# -- daily tick -----------------------------------------------------------


class TickResponse(BaseModel):
    """One daily-tick run: transitions made + exceptions touched."""

    moved_to_due: int = Field(description="Tranches moved expected → due")
    moved_to_overdue: int = Field(description="Tranches moved due → overdue")
    exceptions_created: int = Field(description="late_tranche exceptions opened")
    exceptions_escalated: int = Field(description="late_tranche exceptions escalated a level")
    review_items_filed: int = Field(
        description="settlement_variance review items filed for new late tranches (US-555)"
    )

    @classmethod
    def from_report(cls, report: TickReport) -> TickResponse:
        """Map a tick report to its response."""
        return cls(
            moved_to_due=report.moved_to_due,
            moved_to_overdue=report.moved_to_overdue,
            exceptions_created=report.exceptions_created,
            exceptions_escalated=report.exceptions_escalated,
            review_items_filed=report.review_items_filed,
        )


# -- settlement postings (US-522) -------------------------------------------
#
# The §3.5 journals, posted through the engine. Every request carries
# the Tryton company id (the settlement center is per-company); the
# remaining wiring (gateway party, payment journal, accounts) resolves
# from the mapping center for the payment's channel, failing closed
# (422 SETTLEMENT_WIRING_UNRESOLVED) on a missing mapping.


class SettlementPostingResponse(BaseModel):
    """One row of the settlement posting ledger (§3.5 audit trail)."""

    id: str = Field(description="Posting row id")
    gateway_payment_id: str = Field(description="Owning gateway payment row id")
    tranche_id: str | None = Field(description="Tranche this posting is for, when tranche-scoped")
    kind: str = Field(description="Posting kind")
    status: str = Field(description="'posted' (Tryton move posted) or 'draft'")
    amount_irr: int = Field(description="The posting's principal amount in whole rials")
    tryton_move_id: int | None = Field(description="Tryton account.move id, when any")
    tryton_payment_id: int | None = Field(description="Tryton account.payment id, for clearing postings")
    tryton_reconciliation_id: int | None = Field(description="Tryton reconciliation id, when reconciled")
    reason: str | None = Field(description="Reason for the posting (mandatory for adjustments/refunds)")
    event_ref: str | None = Field(description="Canonical event / refund event reference")
    details: dict | None = Field(description="Posting breakdown (allocations, tie-out figures, variances)")
    created_at: datetime | None = Field(description="Row creation timestamp (UTC)")
    updated_at: datetime | None = Field(description="Last-modified timestamp (UTC)")

    @classmethod
    def from_posting(cls, posting: SettlementPosting) -> SettlementPostingResponse:
        """Map a domain posting to its response."""
        return cls(
            id=posting.id,
            gateway_payment_id=posting.gateway_payment_id,
            tranche_id=posting.tranche_id,
            kind=posting.kind.value,
            status=posting.status.value,
            amount_irr=posting.amount_irr,
            tryton_move_id=posting.tryton_move_id,
            tryton_payment_id=posting.tryton_payment_id,
            tryton_reconciliation_id=posting.tryton_reconciliation_id,
            reason=posting.reason,
            event_ref=posting.event_ref,
            details=posting.details,
            created_at=posting.created_at,
            updated_at=posting.updated_at,
        )


class PostSaleFeeRequest(BaseModel):
    """Recognise a payment's fee + fee VAT at sale (policy A)."""

    company_id: int = Field(description="Tryton company engine id")
    journal_date: date = Field(
        validation_alias="date", description="Fee journal date (the sale date)"
    )
    actor: str | None = Field(default=None, description="Actor posting the fee")


class PostSaleFeeResponse(BaseModel):
    """A posted sale fee — or a zero-fee no-op (posting null)."""

    payment_id: str = Field(description="Gateway payment row id")
    fee_irr: int = Field(description="Fee recognised in whole rials")
    fee_vat_irr: int = Field(description="Fee VAT recognised in whole rials")
    posting: SettlementPostingResponse | None = Field(
        description="The posting row (null for a zero-fee payment: nothing to recognise)"
    )
    tryton_move_id: int | None = Field(description="Tryton fee journal id, when posted")

    @classmethod
    def from_result(cls, result: SaleFeeResult) -> PostSaleFeeResponse:
        """Map a sale-fee result to its response."""
        return cls(
            payment_id=result.payment.id,
            fee_irr=result.fee_irr,
            fee_vat_irr=result.fee_vat_irr,
            posting=(
                SettlementPostingResponse.from_posting(result.posting)
                if result.posting is not None
                else None
            ),
            tryton_move_id=result.tryton_move_id,
        )


class PostTrancheClearingRequest(BaseModel):
    """Post a confirmed tranche's clearing transfer (Path 1b)."""

    company_id: int = Field(description="Tryton company engine id")
    payout_date: date = Field(description="Gateway deposit date — the clearing date")
    actor: str | None = Field(default=None, description="Actor posting the clearing")


class PostTrancheClearingResponse(BaseModel):
    """A posted tranche clearing."""

    tranche: SettlementTrancheResponse = Field(description="The tranche as cleared")
    amount_irr: int = Field(description="Amount cleared by this posting in whole rials")
    cleared_total_irr: int = Field(description="Cumulative cleared amount for the tranche")
    tryton_payment_id: int = Field(description="Tryton account.payment id")
    payment_state: str = Field(description="Payment state after the drive ('succeeded')")
    posting: SettlementPostingResponse = Field(description="The clearing posting row")
    reconciliation_id: int | None = Field(
        description="Tryton reconciliation id (null while the tranche is only partially cleared)"
    )
    fee_posting_id: str | None = Field(
        description="Policy-B fee posting id, when one was made for this tranche"
    )

    @classmethod
    def from_result(cls, result: ClearingResult) -> PostTrancheClearingResponse:
        """Map a clearing result to its response."""
        return cls(
            tranche=SettlementTrancheResponse.from_tranche(result.tranche),
            amount_irr=result.amount_irr,
            cleared_total_irr=result.cleared_total_irr,
            tryton_payment_id=result.payment.ref.engine_id,
            payment_state=result.payment.state,
            posting=SettlementPostingResponse.from_posting(result.posting),
            reconciliation_id=(
                result.reconciliation.ref.engine_id
                if result.reconciliation is not None
                else None
            ),
            fee_posting_id=result.fee_posting.id if result.fee_posting is not None else None,
        )


class PostBankReceiptRequest(BaseModel):
    """Post a received tranche's bank receipt (after tie-out)."""

    company_id: int = Field(description="Tryton company engine id")
    settlement_event_id: str = Field(description="Settlement event whose batch is tied out")
    deposit_date: date = Field(description="Bank deposit date")
    actor: str | None = Field(default=None, description="Actor posting the receipt")


class TieOutFiguresResponse(BaseModel):
    """The batch tie-out figures behind a bank receipt (§3.6)."""

    confirmed_net_irr: int = Field(description="Confirmed tranche nets in the batch")
    deductions_irr: int = Field(description="Documented deductions on the event")
    matched_deposits_irr: int = Field(description="Bank deposits matched to the batch")
    difference_irr: int = Field(description="(confirmed − deductions) − deposits")
    tolerance_irr: int = Field(description="Contract tie-out tolerance")


class PostBankReceiptResponse(BaseModel):
    """A posted bank receipt."""

    tranche: SettlementTrancheResponse = Field(description="The tranche after the receipt")
    amount_irr: int = Field(description="Amount received by this posting in whole rials")
    received_total_irr: int = Field(description="Cumulative received amount for the tranche")
    posting: SettlementPostingResponse = Field(description="The bank-receipt posting row")
    tie_out: TieOutFiguresResponse = Field(description="The tie-out figures that allowed the receipt")
    reconciliation_id: int | None = Field(
        description="Tryton reconciliation id (null while the tranche is only partially received)"
    )

    @classmethod
    def from_result(cls, result: BankReceiptResult) -> PostBankReceiptResponse:
        """Map a bank-receipt result to its response."""
        return cls(
            tranche=SettlementTrancheResponse.from_tranche(result.tranche),
            amount_irr=result.amount_irr,
            received_total_irr=result.received_total_irr,
            posting=SettlementPostingResponse.from_posting(result.posting),
            tie_out=TieOutFiguresResponse(
                confirmed_net_irr=result.tie_out.confirmed_net_irr,
                deductions_irr=result.tie_out.deductions_irr,
                matched_deposits_irr=result.tie_out.matched_deposits_irr,
                difference_irr=result.tie_out.difference_irr,
                tolerance_irr=result.tie_out.tolerance_irr,
            ),
            reconciliation_id=(
                result.reconciliation.ref.engine_id
                if result.reconciliation is not None
                else None
            ),
        )


class PostFeeVarianceRequest(BaseModel):
    """Post a fee-variance adjustment move (never silently absorbed)."""

    company_id: int = Field(description="Tryton company engine id")
    actual_fee_irr: int = Field(ge=0, description="Real fee in whole rials (from the gateway file)")
    actual_vat_irr: int = Field(ge=0, description="Real fee VAT in whole rials (from the gateway file)")
    reason: str = Field(min_length=1, description="Why the fee differs — mandatory")
    journal_date: date = Field(
        validation_alias="date", description="Adjustment journal date"
    )
    tranche_id: str | None = Field(
        default=None, description="Scope the variance to one tranche (None = whole payment)"
    )
    event_ref: str | None = Field(default=None, description="Gateway file / event reference")
    actor: str | None = Field(default=None, description="Actor posting the variance")


class FeeVarianceAllocation(BaseModel):
    """One tranche's share of a fee variance."""

    tranche_id: str = Field(description="Tranche row id")
    fee_share_irr: int = Field(description="Fee share in whole rials")
    vat_share_irr: int = Field(description="VAT share in whole rials")


class PostFeeVarianceResponse(BaseModel):
    """A posted fee-variance adjustment — or a zero-variance no-op."""

    payment_id: str = Field(description="Gateway payment row id")
    fee_variance_irr: int = Field(description="Fee variance (actual - expected) in whole rials")
    vat_variance_irr: int = Field(description="Fee-VAT variance (actual - expected) in whole rials")
    posting: SettlementPostingResponse | None = Field(
        description="The variance posting row (null when actual == expected)"
    )
    exception_id: str | None = Field(description="fee_mismatch exception id, when any")
    tryton_move_id: int | None = Field(description="Tryton adjustment journal id, when posted")
    allocations: list[FeeVarianceAllocation] = Field(description="Variance shares per adjusted tranche")

    @classmethod
    def from_result(cls, result: FeeVarianceResult) -> PostFeeVarianceResponse:
        """Map a fee-variance result to its response."""
        return cls(
            payment_id=result.payment.id,
            fee_variance_irr=result.fee_variance_irr,
            vat_variance_irr=result.vat_variance_irr,
            posting=(
                SettlementPostingResponse.from_posting(result.posting)
                if result.posting is not None
                else None
            ),
            exception_id=result.exception.id if result.exception is not None else None,
            tryton_move_id=result.tryton_move_id,
            allocations=[
                FeeVarianceAllocation(
                    tranche_id=tranche_id, fee_share_irr=fee, vat_share_irr=vat
                )
                for tranche_id, fee, vat in result.allocations
            ],
        )


class ApplyRefundRequest(BaseModel):
    """Apply a (partial) post-settlement refund."""

    company_id: int = Field(description="Tryton company engine id")
    refund_amount_irr: int = Field(gt=0, description="Refunded amount in whole rials")
    reason: str = Field(min_length=1, description="Why the refund — mandatory")
    journal_date: date = Field(
        validation_alias="date", description="Sales-return journal date"
    )
    event_ref: str | None = Field(default=None, description="Refund event reference")
    actor: str | None = Field(default=None, description="Actor applying the refund")


class RefundAllocation(BaseModel):
    """One open tranche's share of a refund."""

    tranche_id: str = Field(description="Tranche row id")
    amount_irr: int = Field(description="Amount deducted from the tranche in whole rials")


class ApplyRefundResponse(BaseModel):
    """An applied refund: tranche allocations + the excess, if any."""

    payment: GatewayPaymentResponse = Field(description="The payment after the refund")
    refund_amount_irr: int = Field(description="Refunded amount in whole rials")
    allocations: list[RefundAllocation] = Field(
        description="Refund shares per open tranche, last-to-first"
    )
    excess_irr: int = Field(
        description="Refund beyond the open balance — a negative adjustment on a later settlement"
    )
    posting: SettlementPostingResponse = Field(description="The refund posting row")
    exception_id: str | None = Field(
        description="cancelled_after_settlement exception id, when an excess remains"
    )

    @classmethod
    def from_result(cls, result: RefundResult) -> ApplyRefundResponse:
        """Map a refund result to its response."""
        return cls(
            payment=GatewayPaymentResponse.from_payment(result.payment),
            refund_amount_irr=result.refund_amount_irr,
            allocations=[
                RefundAllocation(tranche_id=tranche_id, amount_irr=amount)
                for tranche_id, amount in result.allocations
            ],
            excess_irr=result.excess_irr,
            posting=SettlementPostingResponse.from_posting(result.posting),
            exception_id=result.exception.id if result.exception is not None else None,
        )


# -- settlement file imports (US-523) --------------------------------------


class SettlementFileRowResponse(BaseModel):
    """One parsed row of a settlement file import (the dry-run preview)."""

    id: str = Field(description="Row id")
    row_no: int = Field(description="1-based row number in the file")
    raw: dict[str, object] = Field(description="Original row as parsed from the file")
    gateway_transaction_id: str | None = Field(description="Gateway transaction id, when present")
    tran_type: str | None = Field(description="Row kind: detail / payout / deduction / refund / cancel")
    amount_irr: int | None = Field(description="Row gross in whole rials, when present")
    fee_irr: int | None = Field(description="Row fee in whole rials, when present")
    net_irr: int | None = Field(description="Row net in whole rials, when present")
    transaction_date: date | None = Field(description="Row date (Gregorian ISO), when present")
    tracking_ref: str | None = Field(description="Bank tracking reference, when present")
    matched_tranche_id: str | None = Field(description="Dry-run matched tranche id, when any")
    match_status: str = Field(
        description="Dry-run match outcome: matched / new_payment / unknown_reference / duplicate / amount_mismatch"
    )
    note: str | None = Field(description="Human-readable match note, when any")
    created_at: datetime | None = Field(description="When the row was stored")

    @classmethod
    def from_row(cls, row: SettlementFileRow) -> SettlementFileRowResponse:
        """Map a domain file row to its response."""
        return cls(
            id=row.id,
            row_no=row.row_no,
            raw=dict(row.raw_json or {}),
            gateway_transaction_id=row.gateway_transaction_id,
            tran_type=row.tran_type,
            amount_irr=row.amount_irr,
            fee_irr=row.fee_irr,
            net_irr=row.net_irr,
            transaction_date=row.transaction_date,
            tracking_ref=row.tracking_ref,
            matched_tranche_id=row.matched_tranche_id,
            match_status=row.match_status,
            note=row.note,
            created_at=row.created_at,
        )


class SettlementFileImportResponse(BaseModel):
    """One settlement file import: provenance + status + row counts."""

    id: str = Field(description="Import id")
    payment_channel_id: str = Field(description="Owning payment channel row id")
    file_type: str = Field(description="File kind: detail / payouts / statement")
    period_jalali: str = Field(description="Jalali period the file covers (YYYY/MM)")
    file_sha256: str = Field(description="SHA-256 of the file bytes (dedupe key)")
    import_batch_id: str = Field(description="Batch id shared by the import, its event and its posting")
    status: str = Field(description="awaiting_review / approved / rejected / exception / settled")
    uploaded_by: str | None = Field(description="Who uploaded the file, when known")
    uploaded_at: datetime | None = Field(description="When the file was uploaded")
    row_count: int = Field(description="Number of parsed rows stored")
    matched_count: int = Field(description="Rows the dry-run matched")
    new_count: int = Field(description="Rows that would create a new payment")
    error_count: int = Field(description="Rows that failed to parse")
    created_at: datetime | None = Field(description="When the import was created")
    updated_at: datetime | None = Field(description="When the import was last updated")

    @classmethod
    def from_import(cls, file_import: SettlementFileImport) -> SettlementFileImportResponse:
        """Map a domain file import to its response."""
        return cls(
            id=file_import.id,
            payment_channel_id=file_import.payment_channel_id,
            file_type=file_import.file_type,
            period_jalali=file_import.period_jalali,
            file_sha256=file_import.file_hash_sha256,
            import_batch_id=file_import.import_batch_id,
            status=file_import.status,
            uploaded_by=file_import.uploaded_by,
            uploaded_at=file_import.uploaded_at,
            row_count=file_import.row_count,
            matched_count=file_import.matched_count,
            new_count=file_import.new_count,
            error_count=file_import.error_count,
            created_at=file_import.created_at,
            updated_at=file_import.updated_at,
        )


class SettlementFileImportDetailResponse(BaseModel):
    """One import with its parsed rows (the detail read).

    The dry-run preview is a property of the upload response only —
    its receivable figures depend on channel state at import time
    and cannot be recomputed later — so the detail read returns the
    import + rows; each row carries its stored match_status.
    """

    file_import: SettlementFileImportResponse = Field(description="The import")
    rows: list[SettlementFileRowResponse] = Field(description="Parsed rows with match outcomes")

    @classmethod
    def from_detail(
        cls,
        file_import: SettlementFileImport,
        rows: list[SettlementFileRow],
    ) -> SettlementFileImportDetailResponse:
        """Map a domain import + rows to the response."""
        return cls(
            file_import=SettlementFileImportResponse.from_import(file_import),
            rows=[SettlementFileRowResponse.from_row(row) for row in rows],
        )


class ImportSettlementFileRequest(BaseModel):
    """Upload a gateway settlement file for parsing + dry-run preview."""

    filename: str = Field(description="Original file name (used for the format sniff)")
    content_base64: str = Field(description="File bytes, base64-encoded (max 10 MB decoded)")
    channel_key: str = Field(description="Payment channel key (e.g. 'snapppay')")
    file_type: str = Field(
        description="File kind: 'detail' (per-transaction rows), 'payouts' (one row per payout) or 'statement'"
    )
    period_jalali: str = Field(description="Jalali period the file covers (YYYY/MM)")
    currency_unit: str = Field(
        default="rial",
        description="Per-import currency setting: 'rial' or 'toman' (Toman ×10 at the boundary)",
    )
    columns: dict[str, str] | None = Field(
        default=None,
        description="Optional inline column mapping: canonical field → header text",
    )
    date_kind: str = Field(
        default="jalali", description="Date kind in the file: 'jalali' or 'gregorian'"
    )
    header_row: int = Field(default=0, description="0-based header row index")
    actor: str | None = Field(default=None, description="Who uploaded the file")

    @field_validator("content_base64")
    @classmethod
    def _check_base64(cls, value: str) -> str:
        _decode_base64(value)
        return value

    def content_bytes(self) -> bytes:
        """Return the decoded file bytes."""
        return _decode_base64(self.content_base64)


class ImportPreviewResponse(BaseModel):
    """The dry-run preview: accounting-effect totals + per-row match outcomes."""

    gross_irr: int = Field(description="Preview gross in whole rials")
    fee_irr: int = Field(description="Preview fee in whole rials")
    vat_irr: int = Field(description="Preview VAT on the fee in whole rials (0: file rows carry no VAT)")
    net_irr: int = Field(description="Preview net in whole rials")
    refunds_irr: int = Field(description="Preview refunds in whole rials")
    deductions_irr: int = Field(description="Preview documented deductions in whole rials")
    expected_deposit_irr: int = Field(description="Preview expected bank deposit (net − deductions)")
    receivable_before_irr: int = Field(description="Channel open receivable before this import")
    receivable_after_irr: int = Field(description="Channel open receivable after this import")
    matched_rows: int = Field(description="Rows the dry-run matched")
    new_payment_rows: int = Field(description="Rows that would create a new payment")
    unknown_rows: int = Field(description="Rows with an unresolvable transaction id")
    duplicate_rows: int = Field(description="Rows already settled (duplicates)")
    invalid_rows: int = Field(description="Rows that failed to parse")
    dropped_rows: int = Field(description="Net Sale / totals rows dropped at parse")

    @classmethod
    def from_preview(cls, preview: ImportPreview) -> ImportPreviewResponse:
        """Map a service preview to its response."""
        return cls(
            gross_irr=preview.gross_irr,
            fee_irr=preview.fee_irr,
            vat_irr=preview.vat_irr,
            net_irr=preview.net_irr,
            refunds_irr=preview.refunds_irr,
            deductions_irr=preview.deductions_irr,
            expected_deposit_irr=preview.expected_deposit_irr,
            receivable_before_irr=preview.receivable_before_irr,
            receivable_after_irr=preview.receivable_after_irr,
            matched_rows=preview.matched_rows,
            new_payment_rows=preview.new_payment_rows,
            unknown_rows=preview.unknown_rows,
            duplicate_rows=preview.duplicate_rows,
            invalid_rows=preview.invalid_rows,
            dropped_rows=preview.dropped_rows,
        )


class DroppedRowResponse(BaseModel):
    """One Net Sale / totals row dropped at parse (logged, never matched)."""

    row_number: int = Field(description="1-based row number in the file")
    reason: str = Field(description="Why the row was dropped")


class ImportSettlementFileResponse(BaseModel):
    """An import upload: the import, its preview and the review item filed."""

    file_import: SettlementFileImportResponse = Field(description="The created import")
    preview: ImportPreviewResponse = Field(description="Dry-run preview totals")
    review_item_id: str | None = Field(description="Review-queue item id (None on a dedupe replay)")
    is_duplicate: bool = Field(description="True when the same file was already imported (no-op)")
    dropped_rows: list[DroppedRowResponse] = Field(description="Net Sale / totals rows dropped at parse")
    rows: list[SettlementFileRowResponse] = Field(description="Parsed rows with dry-run outcomes")

    @classmethod
    def from_result(
        cls,
        result: ImportSettlementFileResult,
        rows: list[SettlementFileRow],
    ) -> ImportSettlementFileResponse:
        """Map a service result + rows to the response."""
        return cls(
            file_import=SettlementFileImportResponse.from_import(result.file_import),
            preview=ImportPreviewResponse.from_preview(result.preview),
            review_item_id=result.review_item_id,
            is_duplicate=result.duplicate,
            dropped_rows=[
                DroppedRowResponse(row_number=row_no, reason=reason)
                for row_no, reason in result.dropped_rows
            ],
            rows=[SettlementFileRowResponse.from_row(row) for row in rows],
        )


class ApproveImportRequest(BaseModel):
    """Approve an awaiting-review import (authoritative re-match + events)."""

    actor: str | None = Field(default=None, description="Who approved the import")


class ApproveImportResponse(BaseModel):
    """An approved import: the settlement event + what the re-match did."""

    import_batch_id: str = Field(description="Batch id")
    settlement_event_id: str = Field(description="The settlement event created for this import")
    canonical_event_id: str = Field(description="Outbox id of the canonical payment.settlement.received event")
    confirmed_tranche_ids: list[str] = Field(description="Tranche ids confirmed by this import")
    created_payment_ids: list[str] = Field(description="Payment ids created from unknown-reference rows")
    exception_ids: list[str] = Field(description="Exception ids opened by the re-match")
    review_item_ids: list[str] = Field(description="Review-queue item ids filed by the re-match")
    net_irr: int = Field(description="Settlement event net in whole rials")
    deductions_irr: int = Field(description="Documented deductions in whole rials")

    @classmethod
    def from_result(cls, result: ApproveImportResult) -> ApproveImportResponse:
        """Map a service result to its response."""
        return cls(
            import_batch_id=result.file_import.import_batch_id,
            settlement_event_id=result.settlement_event.id,
            canonical_event_id=result.canonical_event_id,
            confirmed_tranche_ids=list(result.confirmed_tranche_ids),
            created_payment_ids=list(result.created_payment_ids),
            exception_ids=[exc.id for exc in result.exceptions],
            review_item_ids=list(result.review_item_ids),
            net_irr=result.settlement_event.net_irr,
            deductions_irr=result.settlement_event.deductions_irr,
        )


class RejectImportRequest(BaseModel):
    """Reject an awaiting-review import (no events, no matching)."""

    reason: str | None = Field(default=None, description="Why the import is rejected")
    actor: str | None = Field(default=None, description="Who rejected the import")


# -- settlement events / deposits / matches (US-523) -----------------------


class SettlementEventResponse(BaseModel):
    """One settlement event: a gateway payout for one channel + period."""

    id: str = Field(description="Event id")
    payment_channel_id: str = Field(description="Owning payment channel row id")
    payout_date: date = Field(description="Payout date (Gregorian ISO)")
    net_irr: int = Field(description="Payout net in whole rials")
    source: str = Field(description="How the event was created: file / api / manual")
    deductions_irr: int = Field(description="Documented deductions in whole rials")
    tracking_ref: str | None = Field(description="Bank tracking reference, when known")
    destination_iban: str | None = Field(description="Destination IBAN, when known")
    gateway_batch_ref: str | None = Field(description="Gateway-side batch reference, when known")
    import_batch_id: str | None = Field(description="Batch id, when the event came from a file")
    canonical_event_id: str | None = Field(description="Outbox id of the canonical event, when published")
    created_at: datetime | None = Field(description="When the event was created")
    updated_at: datetime | None = Field(description="When the event was last updated")

    @classmethod
    def from_event(cls, event: SettlementEvent) -> SettlementEventResponse:
        """Map a domain event to its response."""
        return cls(
            id=event.id,
            payment_channel_id=event.payment_channel_id,
            payout_date=event.payout_date,
            net_irr=event.net_irr,
            source=event.source,
            deductions_irr=event.deductions_irr,
            tracking_ref=event.tracking_ref,
            destination_iban=event.destination_iban,
            gateway_batch_ref=event.gateway_batch_ref,
            import_batch_id=event.import_batch_id,
            canonical_event_id=event.canonical_event_id,
            created_at=event.created_at,
            updated_at=event.updated_at,
        )


class BankDepositResponse(BaseModel):
    """One bank deposit: money that arrived at the bank, matched or not."""

    id: str = Field(description="Deposit id")
    deposit_date: date = Field(description="Deposit date (Gregorian ISO)")
    amount_irr: int = Field(description="Deposit amount in whole rials")
    bank_account_ref: str | None = Field(description="Bank account reference, when known")
    description: str | None = Field(description="Bank narrative, when any")
    tracking_ref: str | None = Field(description="Bank tracking reference, when any")
    bank_import_row_id: str | None = Field(description="Bank import row id, when linked")
    source: str = Field(description="How the deposit was recorded: statement_import / manual / api")
    created_at: datetime | None = Field(description="When the deposit was recorded")
    updated_at: datetime | None = Field(description="When the deposit was last updated")

    @classmethod
    def from_deposit(cls, deposit: BankDeposit) -> BankDepositResponse:
        """Map a domain deposit to its response."""
        return cls(
            id=deposit.id,
            deposit_date=deposit.deposit_date,
            amount_irr=deposit.amount_irr,
            bank_account_ref=deposit.bank_account_ref,
            description=deposit.description,
            tracking_ref=deposit.tracking_ref,
            bank_import_row_id=deposit.bank_import_row_id,
            source=deposit.source,
            created_at=deposit.created_at,
            updated_at=deposit.updated_at,
        )


class TieOutResultResponse(BaseModel):
    """The three-way tie-out comparison (pure result, no gate/posting)."""

    holds: bool = Field(description="Whether the tie-out holds (difference within tolerance)")
    confirmed_total_irr: int = Field(description="Σ confirmed tranche nets")
    deductions_irr: int = Field(description="Σ documented period deductions")
    expected_deposits_irr: int = Field(description="Expected deposits (confirmed − deductions)")
    matched_deposits_irr: int = Field(description="Σ matched bank deposits (allocated amounts)")
    difference_irr: int = Field(description="expected deposits − matched deposits")
    tolerance_irr: int = Field(description="Tie-out tolerance in whole rials")

    @classmethod
    def from_result(cls, result: TieOutResult) -> TieOutResultResponse:
        """Map a domain tie-out result to its response."""
        return cls(
            holds=result.holds,
            confirmed_total_irr=result.confirmed_total_irr,
            deductions_irr=result.deductions_irr,
            expected_deposits_irr=result.expected_deposits_irr,
            matched_deposits_irr=result.matched_deposits_irr,
            difference_irr=result.difference_irr,
            tolerance_irr=result.tolerance_irr,
        )


class RecordDepositRequest(BaseModel):
    """Record a bank deposit and run the C5 confidence-ordered matching."""

    amount_irr: int = Field(description="Deposit amount in whole rials")
    deposit_date: date = Field(description="Deposit date (Gregorian ISO)")
    bank_account_ref: str | None = Field(default=None, description="Bank account reference, when known")
    description: str | None = Field(default=None, description="Bank narrative, when any")
    tracking_ref: str | None = Field(default=None, description="Bank tracking reference, when any")
    bank_import_row_id: str | None = Field(default=None, description="Bank import row id, when linked")
    source: str = Field(default="manual", description="How the deposit was recorded")
    channel_key: str | None = Field(default=None, description="Gateway channel key, when known")
    company_id: str | None = Field(
        default=None,
        description="HesabYar company row id; when given, a matching batch is tied out inline",
    )
    actor: str | None = Field(default=None, description="Who recorded the deposit")


class DepositMatchSuggestionResponse(BaseModel):
    """One ranked deposit→target suggestion (amount-only matches)."""

    target_kind: str = Field(description="Suggested target kind: 'event' or 'tranche'")
    target_id: str = Field(description="Suggested target id")
    confidence: Decimal = Field(description="Suggestion confidence (max 0.70)")
    reason: str = Field(description="Why the target was suggested")

    @classmethod
    def from_tuple(
        cls, suggestion: tuple[str, str, Decimal, str]
    ) -> DepositMatchSuggestionResponse:
        """Map a service suggestion tuple (kind, id, confidence, reason)."""
        kind, target_id, confidence, reason = suggestion
        return cls(
            target_kind=kind,
            target_id=target_id,
            confidence=_clean_decimal(confidence),
            reason=reason,
        )


class RecordDepositResponse(BaseModel):
    """A recorded deposit: its match decision + suggestions + tie-out outcome."""

    deposit: BankDepositResponse = Field(description="The recorded deposit")
    decision_method: str = Field(
        description="Match method: exact_reference / amount_and_date / amount_only / none / manual"
    )
    decision_confidence: Decimal = Field(description="Match confidence (0 when unmatched)")
    matched_tranche_ids: list[str] = Field(description="Tranche ids matched by this deposit")
    suggestions: list[DepositMatchSuggestionResponse] = Field(description="Ranked suggestions, if any")
    exception_ids: list[str] = Field(description="Exception ids opened by this recording")
    tieout: TieOutResultResponse | None = Field(description="Inline tie-out result, when a batch was tied out")
    posted_journal_id: int | None = Field(description="Engine journal id, when the inline tie-out posted")

    @classmethod
    def from_result(cls, result: RecordDepositResult) -> RecordDepositResponse:
        """Map a service result to its response."""
        return cls(
            deposit=BankDepositResponse.from_deposit(result.deposit),
            decision_method=result.decision_method,
            decision_confidence=_clean_decimal(result.decision_confidence),
            matched_tranche_ids=list(result.matched_tranche_ids),
            suggestions=[
                DepositMatchSuggestionResponse.from_tuple(s) for s in result.suggestions
            ],
            exception_ids=[exc.id for exc in result.exceptions],
            tieout=(
                TieOutResultResponse.from_result(result.tieout)
                if result.tieout is not None
                else None
            ),
            posted_journal_id=result.posted_journal_id,
        )


class MatchDepositManuallyRequest(BaseModel):
    """Manually attach a deposit to a tranche (operator decision)."""

    tranche_id: str = Field(description="Tranche to attach the deposit to")
    allocated_amount_irr: int = Field(description="Amount to allocate to the tranche, in whole rials")
    company_id: str | None = Field(
        default=None,
        description="HesabYar company row id; when given, the affected batch is tied out inline",
    )
    actor: str | None = Field(default=None, description="Who made the manual match")


class SettlementMatchResponse(BaseModel):
    """One match link: a tranche ↔ settlement event and/or bank deposit."""

    id: str = Field(description="Match id")
    tranche_id: str = Field(description="Tranche id")
    allocated_amount_irr: int = Field(description="Allocated amount in whole rials")
    method: str = Field(description="Match method: exact_transaction / batch / deposit / reverse / manual")
    bank_deposit_id: str | None = Field(description="Deposit id, when the link involves one")
    settlement_event_id: str | None = Field(description="Event id, when the link involves one")
    confidence: Decimal | None = Field(description="Match confidence, when applicable")
    matched_by: str | None = Field(description="Who/what made the match")
    is_automatic: bool = Field(description="Whether the match was automatic (False = manual)")
    matched_at: datetime | None = Field(description="When the match was made")
    created_at: datetime | None = Field(description="When the match was stored")

    @classmethod
    def from_match(cls, match: SettlementMatch) -> SettlementMatchResponse:
        """Map a domain match to its response."""
        return cls(
            id=match.id,
            tranche_id=match.tranche_id,
            allocated_amount_irr=match.allocated_amount_irr,
            method=match.method,
            bank_deposit_id=match.bank_deposit_id,
            settlement_event_id=match.settlement_event_id,
            confidence=_clean_optional_decimal(match.confidence),
            matched_by=match.matched_by,
            is_automatic=match.is_automatic,
            matched_at=match.matched_at,
            created_at=match.created_at,
        )


# -- tie-out (US-523) --------------------------------------------------------


class RunTieOutRequest(BaseModel):
    """Run the mandatory three-way tie-out for one import batch."""

    company_id: str | None = Field(
        default=None,
        description="HesabYar company row id (the posting company); without one the tie-out computes but never posts",
    )
    actor: str | None = Field(default=None, description="Who ran the tie-out")


class TieOutRunResponse(BaseModel):
    """A tie-out run: the three-way comparison + gate + posting outcome."""

    import_batch_id: str = Field(description="Batch id tied out")
    status: str = Field(description="Run outcome: settled / approved (held, draft-hold) / exception")
    tieout: TieOutResultResponse = Field(description="The three-way comparison")
    gate_allowed: bool | None = Field(
        description="Whether the policy gate allows auto-post (None = gate not evaluated)"
    )
    gate_reason: str | None = Field(description="Policy gate reason, when any")
    posted: bool = Field(description="Whether the bank move was posted")
    tryton_journal_id: int | None = Field(description="Engine journal id, when posted")
    settled_tranche_ids: list[str] = Field(description="Tranche ids closed (settled) by this run")
    exception_ids: list[str] = Field(description="Exception ids opened by this run")
    review_item_ids: list[str] = Field(description="Review-queue item ids filed by this run")

    @classmethod
    def from_result(cls, result: TieOutRunResult) -> TieOutRunResponse:
        """Map a service result to its response."""
        return cls(
            import_batch_id=result.file_import.import_batch_id,
            status=result.status,
            tieout=TieOutResultResponse.from_result(result.tieout),
            gate_allowed=result.gate_allowed,
            gate_reason=result.gate_reason,
            posted=result.posted,
            tryton_journal_id=result.tryton_journal_id,
            settled_tranche_ids=list(result.settled_tranche_ids),
            exception_ids=[exc.id for exc in result.exceptions],
            review_item_ids=list(result.review_item_ids),
        )
