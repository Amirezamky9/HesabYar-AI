"""Request/response schemas for the cheque endpoints (US-531 + US-532).

Responses map from the domain objects — no ORM object ever leaks.
The create body carries the SAME 24 fields as the site payload
(§5 §1.2), with the same validations applied by the service; the
schema itself only enforces JSON types. Dates travel as Jalali
``YYYY-MM-DD`` strings and are stored as Gregorian dates. Amounts in
the request are Toman (``amount_toman``); all amounts in responses
are Rial (``amount_irr``) with ``amount_toman`` alongside for display.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from hesabyar.application.queries.cheques import (
    ChequeDetail,
    CollateralSummary,
    EndorsementSummary,
)
from hesabyar.domain.cheques.checkbook import CheckbookStatus
from hesabyar.domain.cheques.models import Cheque, CustodyLogEntry, InquiryReceipt
from hesabyar.domain.cheques.validation import to_jalali_string

if TYPE_CHECKING:
    from hesabyar.application.commands.cheque_clearing import ChequeClearingResult


class InquiryReceiptRequest(BaseModel):
    """One structured Sayyad inquiry receipt (manual entry, §1.4)."""

    model_config = ConfigDict(extra="forbid")

    channel: str = Field(
        description="Inquiry channel: sms | cbi_website | bank_app | payment_app | "
        "finnotech_api | ehrazino_api"
    )
    inquiry_type: str = Field(
        description="Inquiry type: color | leaf_status | items_match | "
        "issuer_history | back_cheques | transfer_chain"
    )
    result_code: str = Field(description="Result code, e.g. 'color=3' (verdict only, no PII)")
    result_summary: str = Field(description="Short summary of the result (never the full response)")
    reference: str = Field(description="Response tracking number / redacted reference")
    cost_irr: int = Field(default=0, ge=0, description="Inquiry cost in Rial (SMS 350 Toman = 3,500 Rial)")
    requested_at: datetime | None = Field(default=None, description="When the inquiry was requested")
    responded_at: datetime | None = Field(default=None, description="When the inquiry response arrived")


class CreateChequeRequest(BaseModel):
    """Manual cheque registration: the 24-field site payload (§5 §1.2).

    ``company_id`` is the HesabYar ``engine_companies`` row id or the
    engine's company id (both accepted, tenant-scoped).
    """

    model_config = ConfigDict(extra="forbid")

    company_id: str = Field(description="HesabYar company row id or engine company id")
    sayad_id: str = Field(description="16-digit Sayyad identifier")
    amount_toman: int = Field(gt=0, description="Amount in Toman (converted x10 to Rial at the boundary)")
    amount_currency: str = Field(default="IRT", description='"IRT" (Toman) or "IRR" (Rial)')
    issue_date: str = Field(description="Issue date, Jalali YYYY-MM-DD")
    due_date: str = Field(description="Due date, Jalali YYYY-MM-DD")
    issuer_name: str = Field(description="Issuer name (customer or third party)")
    issuer_national_id: str = Field(description="Issuer national id: 10 digits (natural) or 11 digits (legal)")
    issuer_bank: str = Field(description="Issuing bank: 3-digit code + name, e.g. '056 سامان'")
    payee_name: str = Field(description="Payee registered in Sayyad (must be the business)")
    payee_national_id: str = Field(description="Payee national id: 10 digits (natural) or 11 digits (legal)")
    purpose: str = Field(default="payment", description='"payment" (commercial) or "collateral"')
    kind: str = Field(default="received", description='"received" (a customer\'s cheque) or "issued" (our cheque)')
    checkbook_id: int | None = Field(
        default=None,
        description="Engine checkbook id — required for an issued cheque, "
        "forbidden for a received cheque",
    )
    secured_obligation: str | None = Field(
        default=None,
        description="What the collateral cheque secures (e.g. a contract or "
        "loan id) — collateral only, travels alongside the 24-field payload",
    )
    order_id: str = Field(description="Site order id")
    customer_ref: str = Field(description="Site customer reference")
    sayad_registered: bool = Field(description="Whether the issuer registered the leaf in Sayyad")
    leaf_number: str | None = Field(default=None, description="Serial number printed on the leaf")
    issuer_branch: str | None = Field(default=None, description="Issuing bank branch name/code")
    issuer_account_number: str | None = Field(default=None, description="Issuer current-account number")
    reason: str | None = Field(default=None, description="'بابت' — the reason for the cheque")
    invoice_id: str | None = Field(default=None, description="Linked HesabYar/Tryton invoice id")
    installment_leg_id: str | None = Field(default=None, description="Linked installment leg id")
    scan_url: str | None = Field(default=None, description="Signed scan/photo URL from the site")
    scan_ref: str | None = Field(default=None, description="Scan file reference in the site")
    registered_by: str | None = Field(default=None, description="Who registered the cheque on the site")
    notes: str | None = Field(default=None, description="Free-form note")

    def payload_fields(self) -> dict[str, object]:
        """The 24 payload fields (everything except ``company_id``).

        ``kind`` / ``checkbook_id`` / ``secured_obligation`` are
        registry fields, NOT part of the 24-field site payload — they
        travel alongside it and are excluded here.
        """
        data = self.model_dump(
            exclude={"company_id", "kind", "checkbook_id", "secured_obligation"}
        )
        return {key: value for key, value in data.items()}


class ConfirmReceiptRequest(BaseModel):
    """Confirm the physical leaf arrived + Sayyad confirmed: registered -> held."""

    model_config = ConfigDict(extra="forbid")

    items_matched: bool = Field(
        description="All four Sayyad items (sayad_id, amount, due date, payee identity) "
        "matched the leaf"
    )
    sayad_accepted: bool = Field(description="The receipt was accepted in Sayyad (bank app / SMS / Fintech)")
    color_code: int | None = Field(default=None, ge=1, le=5, description="Issuer colour code 1-5 (1=white … 5=red)")
    back_cheques_count: int | None = Field(default=None, ge=0, description="Issuer bounced-cheque count")
    in_transit_commitments_irr: int | None = Field(
        default=None,
        ge=0,
        description="Issuer in-transit (Sayyad) commitments in Rial",
    )
    manager_approval: str | None = Field(
        default=None,
        description="Approving manager (required when pre-acceptance holds for approval)",
    )
    inquiry_receipt: InquiryReceiptRequest | None = Field(
        default=None,
        description="Structured inquiry receipt (at least one is expected; "
        "missing it records a warning)",
    )
    storage_location: str | None = Field(
        default=None,
        description="Physical location to record at held (defaults to the installation default)",
    )


class DepositRequest(BaseModel):
    """Hand the leaf to the bank for clearing: held -> in_clearing."""

    model_config = ConfigDict(extra="forbid")

    destination_account_id: int = Field(description="Destination bank account (engine account id)")
    destination_bank_name: str | None = Field(default=None, description="Destination bank account display name")


class ClearRequest(BaseModel):
    """Confirm clearing: in_clearing -> cleared (manual confirmation)."""

    model_config = ConfigDict(extra="forbid")

    statement_reference: str | None = Field(
        default=None,
        description="Bank-statement reference for the clearing match "
        "(US-534 wires auto-matching)",
    )


class BounceRequest(BaseModel):
    """Record a bounce: in_clearing -> bounced (bank document required)."""

    model_config = ConfigDict(extra="forbid")

    bounce_reason: str = Field(description="Bounce reason")
    bounce_date: str = Field(description="Bounce date, Jalali YYYY-MM-DD")
    certificate_reference: str | None = Field(
        default=None,
        description="Bank non-payment certificate / inquiry receipt reference "
        "(required for a received cheque; an issued cheque bounces "
        "with reason + date only, §2.3)",
    )
    bounce_destination: str | None = Field(
        default=None,
        description="Claim destination override: 'receivables' | 'bounced_receivables' "
        "(defaults to the installation setting)",
    )


class EndorseRequest(BaseModel):
    """Endorse the cheque on: held -> endorsed (Sayyad transfer required)."""

    model_config = ConfigDict(extra="forbid")

    endorsee_name: str = Field(description="Endorsee name (supplier or other person)")
    endorsee_type: str = Field(description="Endorsee type: 'supplier' | 'other'")
    sayad_transfer_reference: str = Field(description="Sayyad transfer registration receipt")


class RejectMismatchRequest(BaseModel):
    """Reject a registered cheque whose leaf disagrees with Sayyad."""

    model_config = ConfigDict(extra="forbid")

    mismatch_detail: str = Field(description="What mismatched (site vs leaf vs Sayyad)")


class IssueRequest(BaseModel):
    """Issue a cheque from our checkbook: registered -> issued (سند ۱۰)."""

    model_config = ConfigDict(extra="forbid")

    destination_account_id: int = Field(description="Bank account the cheque clears against (engine account id)")
    destination_bank_name: str | None = Field(default=None, description="Destination bank account display name")


class VoidRequest(BaseModel):
    """Void an issued cheque: issued -> voided (سند ۱۳, reason required)."""

    model_config = ConfigDict(extra="forbid")

    void_reason: str = Field(description="Why the issued cheque is voided (required)")


class CallInRequest(BaseModel):
    """Call in a received collateral cheque: held -> called_in (سند ۹الف + ۹ب).

    Accountant-gated (§9.1 #4): the per-case approval is required.
    """

    model_config = ConfigDict(extra="forbid")

    accountant_approval: str = Field(description="Approving accountant for this call-in (required)")


class UnexpectedCallInRequest(BaseModel):
    """Record an unexpected call-in of an ISSUED collateral cheque.

    The withdrawal was matched from the bank statement by leaf
    number — the statement reference is the evidence (required).
    """

    model_config = ConfigDict(extra="forbid")

    statement_reference: str = Field(description="Bank-statement withdrawal reference matched by leaf number")


class EndorsedBounceRequest(BaseModel):
    """Record that a cheque we endorsed away has bounced (§9.1 #3).

    No journal is guessed: reason + date + bank certificate are
    recorded for the accountant, who designs the
    reverse-of-endorsement journal.
    """

    model_config = ConfigDict(extra="forbid")

    bounce_reason: str = Field(description="Bounce reason")
    bounce_date: str = Field(description="Bounce date, Jalali YYYY-MM-DD")
    certificate_reference: str = Field(description="Bank non-payment certificate reference")


class CustodyEntryResponse(BaseModel):
    """One append-only physical-leaf move."""

    id: str
    from_location: str | None
    to_location: str
    actor: str
    reason: str
    at: datetime

    @classmethod
    def from_entry(cls, entry: CustodyLogEntry) -> CustodyEntryResponse:
        return cls(
            id=entry.id,
            from_location=entry.from_location.value if entry.from_location else None,
            to_location=entry.to_location.value,
            actor=entry.actor,
            reason=entry.reason,
            at=entry.at,
        )


class InquiryReceiptResponse(BaseModel):
    """One structured Sayyad inquiry receipt."""

    id: str
    channel: str
    inquiry_type: str
    result_code: str
    result_summary: str
    reference: str
    recorded_by: str
    cost_irr: int
    requested_at: datetime | None
    responded_at: datetime | None

    @classmethod
    def from_receipt(cls, receipt: InquiryReceipt) -> InquiryReceiptResponse:
        return cls(
            id=receipt.id,
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


class ChequeResponse(BaseModel):
    """One cheque in the registry."""

    id: str
    state: str
    kind: str
    purpose: str
    sayad_id: str
    sayad_status: str
    sayad_unconfirmed: bool
    amount_irr: int
    amount_toman: int
    issue_date: str
    due_date: str
    issuer_name: str
    issuer_national_id: str
    issuer_bank: str
    payee_name: str
    payee_national_id: str
    order_id: str
    customer_ref: str
    leaf_number: str | None
    issuer_branch: str | None
    issuer_account_number: str | None
    reason: str | None
    invoice_id: str | None
    installment_leg_id: str | None
    installment_leg_status: str | None
    scan_url: str | None
    scan_ref: str | None
    scan_filestore_ref: str | None
    source_event_id: str | None
    age_days: int
    storage_location: str | None
    engine_id: int | None
    move_ids: dict[str, int]
    destination_bank_account_id: int | None
    destination_bank_name: str | None
    deposit_date: str | None
    clearing_confidence: str | None
    clearing_statement_ref: str | None
    clearing_reconciliation_id: int | None
    issuer_color_code: int | None
    issuer_back_cheques: int | None
    bounce_reason: str | None
    bounce_date: str | None
    bounce_certificate_reference: str | None
    endorsee_name: str | None
    endorsee_type: str | None
    sayad_transfer_reference: str | None
    checkbook_engine_id: int | None
    secured_obligation: str | None
    void_reason: str | None
    call_in_approved_by: str | None
    call_in_approved_at: datetime | None
    unexpected_call_in: bool
    call_in_statement_reference: str | None
    journal_pending_review: bool
    receipt_missing_warning: bool
    mismatch_detail: str | None
    warnings: list[str]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_cheque(cls, cheque: Cheque) -> ChequeResponse:
        warnings: list[str] = []
        if cheque.sayad_unconfirmed:
            warnings.append("چک ثبت‌نشده را نپذیرید — the issuer has not registered this leaf in Sayyad (ruling 870).")
        if cheque.issuer_color_code == 1:
            warnings.append(
                "رنگ سفید تضمین نیست — a white issuer colour is history only, "
                "not a guarantee this leaf will clear."
            )
        if cheque.receipt_missing_warning:
            warnings.append("بدون رسید استعلام ثبت شد — recorded without an inquiry receipt; see the mismatch report.")
        if cheque.unexpected_call_in:
            warnings.append(
                "چک تضمینی صادرشده بدون اطلاع برگشت خورده — this issued collateral "
                "cheque was called in without notice; the commercial journal is "
                "accountant-gated and has NOT been posted."
            )
        if cheque.journal_pending_review and cheque.state.value == "called_in":
            warnings.append(
                "سند تجاری در انتظار حسابدار — the commercial journal of this "
                "called-in cheque awaits the accountant."
            )
        return cls(
            id=cheque.id,
            state=cheque.state.value,
            kind=cheque.kind.value,
            purpose=cheque.purpose.value,
            sayad_id=cheque.sayad_id,
            sayad_status=cheque.sayad_status.value,
            sayad_unconfirmed=cheque.sayad_unconfirmed,
            amount_irr=cheque.amount_irr,
            amount_toman=cheque.amount_toman,
            issue_date=to_jalali_string(cheque.issue_date.date),
            due_date=to_jalali_string(cheque.due_date.date),
            issuer_name=cheque.issuer_name,
            issuer_national_id=cheque.issuer_national_id,
            issuer_bank=cheque.issuer_bank,
            payee_name=cheque.payee_name,
            payee_national_id=cheque.payee_national_id,
            order_id=cheque.order_id,
            customer_ref=cheque.customer_ref,
            leaf_number=cheque.leaf_number,
            issuer_branch=cheque.issuer_branch,
            issuer_account_number=cheque.issuer_account_number,
            reason=cheque.reason,
            invoice_id=cheque.invoice_id,
            installment_leg_id=cheque.installment_leg_id,
            installment_leg_status=(
                cheque.installment_leg_status.value if cheque.installment_leg_status else None
            ),
            scan_url=cheque.scan_url,
            scan_ref=cheque.scan_ref,
            scan_filestore_ref=cheque.scan_filestore_ref,
            source_event_id=cheque.source_event_id,
            age_days=(datetime.now(UTC).date() - cheque.created_at.date()).days,
            storage_location=(
                cheque.storage_location.value if cheque.storage_location else None
            ),
            engine_id=cheque.engine_id,
            move_ids=dict(cheque.move_ids),
            destination_bank_account_id=cheque.destination_bank_account_id,
            destination_bank_name=cheque.destination_bank_name,
            deposit_date=(
                to_jalali_string(cheque.deposit_date.date) if cheque.deposit_date else None
            ),
            clearing_confidence=cheque.clearing_confidence,
            clearing_statement_ref=cheque.clearing_statement_ref,
            clearing_reconciliation_id=cheque.clearing_reconciliation_id,
            issuer_color_code=cheque.issuer_color_code,
            issuer_back_cheques=cheque.issuer_back_cheques,
            bounce_reason=cheque.bounce_reason,
            bounce_date=(
                to_jalali_string(cheque.bounce_date.date) if cheque.bounce_date else None
            ),
            bounce_certificate_reference=cheque.bounce_certificate_reference,
            endorsee_name=cheque.endorsee_name,
            endorsee_type=cheque.endorsee_type,
            sayad_transfer_reference=cheque.sayad_transfer_reference,
            checkbook_engine_id=cheque.checkbook_engine_id,
            secured_obligation=cheque.secured_obligation,
            void_reason=cheque.void_reason,
            call_in_approved_by=cheque.call_in_approved_by,
            call_in_approved_at=cheque.call_in_approved_at,
            unexpected_call_in=cheque.unexpected_call_in,
            call_in_statement_reference=cheque.call_in_statement_reference,
            journal_pending_review=cheque.journal_pending_review,
            receipt_missing_warning=cheque.receipt_missing_warning,
            mismatch_detail=cheque.mismatch_detail,
            warnings=warnings,
            created_at=cheque.created_at,
            updated_at=cheque.updated_at,
        )


class ChequeDetailResponse(ChequeResponse):
    """One cheque with its custody history and inquiry receipts."""

    custody_entries: list[CustodyEntryResponse]
    inquiry_receipts: list[InquiryReceiptResponse]

    @classmethod
    def from_detail(cls, detail: ChequeDetail) -> ChequeDetailResponse:
        base = ChequeResponse.from_cheque(detail.cheque)
        return cls(
            **base.model_dump(),
            custody_entries=[CustodyEntryResponse.from_entry(e) for e in detail.custody_entries],
            inquiry_receipts=[InquiryReceiptResponse.from_receipt(r) for r in detail.inquiry_receipts],
        )


class ChequeSettingsResponse(BaseModel):
    """The installation's cheque policy (US-531 defaults, §9.1)."""

    installation_id: str
    leaf_arrival_deadline_days: int
    clearing_window_days: int | None
    bounce_destination: str
    red_brown_policy: str
    max_in_transit_commitments_irr: int | None
    default_storage_location: str


class SayyadInquiryRequest(BaseModel):
    """Run one Fintech Sayyad scope inquiry (simulator until B3, US-534).

    ``scope`` is one of the 18 documented Fintech scope names
    (``credit:<name>:<verb>``). ``scenario`` selects a rehearsal
    fixture (``clean`` | ``red``); without it the simulator needs a
    seeded fixture for the cheque's sayad_id and answers
    ``not_found`` otherwise — it never invents a verdict.
    """

    model_config = ConfigDict(extra="forbid")

    scope: str = Field(description="Fintech Sayyad scope name, e.g. 'credit:cheque-color-inquiry:get'")
    scenario: str | None = Field(
        default=None, description="Rehearsal fixture: 'clean' (white, no back-cheques) or 'red'"
    )


class EhrazinoInquiryRequest(BaseModel):
    """Run one Ehrazino inquiry (simulator until B3 verifies transport, US-534).

    ``kind`` is ``color`` (issuer colour by national code — history
    only, never a leaf verdict) or ``sayad_check`` (leaf identity by
    sayad_id). Costs: colour 1,700 Toman, sayad-check 6,900 Toman.
    """

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(description="'color' or 'sayad_check'")
    scenario: str | None = Field(
        default=None, description="Rehearsal fixture: 'clean' (white / items match) or 'red'"
    )


class ClearingConfirmRequest(BaseModel):
    """Human-confirm a suggested clearing match (probable/ambiguous, US-534)."""

    model_config = ConfigDict(extra="forbid")

    statement_id: int = Field(description="Engine bank statement id")
    line_id: int = Field(description="Engine statement line id carrying the suggested match")


class ClearingRunRequest(BaseModel):
    """Run clearing matching over one draft statement (US-534)."""

    model_config = ConfigDict(extra="forbid")

    bank_key: str | None = Field(
        default=None,
        description="Bank key for the per-bank leaf rule ('melli' caps confidence at probable — §9.2 #7)",
    )


class ClearingLineDecisionResponse(BaseModel):
    """What one statement line's clearing decision did (US-534)."""

    line_id: int
    confidence: str
    cheque_id: str | None
    sayad_id: str | None
    action: str
    reconciliation_id: int | None
    review_item_id: str | None
    reasons: list[str]


class ChequeClearingResponse(BaseModel):
    """Outcome of one cheque-clearing run over a statement (US-534)."""

    statement_id: int
    total_lines: int
    exact_count: int
    probable_count: int
    ambiguous_count: int
    unmatched_count: int
    cleared_count: int
    review_items_filed: int
    lines: list[ClearingLineDecisionResponse]

    @classmethod
    def from_result(cls, result: ChequeClearingResult) -> ChequeClearingResponse:
        return cls(
            statement_id=result.statement_id,
            total_lines=result.total_lines,
            exact_count=result.exact_count,
            probable_count=result.probable_count,
            ambiguous_count=result.ambiguous_count,
            unmatched_count=result.unmatched_count,
            cleared_count=result.cleared_count,
            review_items_filed=result.review_items_filed,
            lines=[
                ClearingLineDecisionResponse(
                    line_id=line.line_id,
                    confidence=line.confidence.value,
                    cheque_id=line.cheque_id,
                    sayad_id=line.sayad_id,
                    action=line.action,
                    reconciliation_id=line.reconciliation_id,
                    review_item_id=line.review_item_id,
                    reasons=list(line.reasons),
                )
                for line in result.lines
            ],
        )


class ClearingAgingResponse(BaseModel):
    """One in-clearing cheque's age for the aging report (report 4, US-534)."""

    cheque_id: str
    sayad_id: str
    days_in_clearing: int
    stale: bool
    window_days: int | None
    window_unset: bool


class InquiryLedgerResponse(BaseModel):
    """Aggregate inquiry ledger (report 13 inputs, US-534)."""

    total_receipts: int
    by_channel: dict[str, int]
    color_distribution: dict[int, int]
    total_cost_irr: int


class ReceiptRuleAuditResponse(BaseModel):
    """Outcome of one mandatory-receipt-rule audit run (US-534, §1.4)."""

    checked: int
    missing: int
    items_filed: int
    cheque_ids: list[str]


class CollateralSummaryResponse(BaseModel):
    """Report 6 data: collateral cheques, memorandum-only (§2.2)."""

    cheques: list[ChequeResponse]
    received_total_irr: int
    issued_total_irr: int
    by_obligation: dict[str | None, list[ChequeResponse]]

    @classmethod
    def from_summary(cls, summary: CollateralSummary) -> CollateralSummaryResponse:
        return cls(
            cheques=[ChequeResponse.from_cheque(c) for c in summary.cheques],
            received_total_irr=summary.received_total_irr,
            issued_total_irr=summary.issued_total_irr,
            by_obligation={
                key: [ChequeResponse.from_cheque(c) for c in cheques]
                for key, cheques in summary.by_obligation.items()
            },
        )


class EndorsementSummaryResponse(BaseModel):
    """Report 5 data: endorsed cheques, supplier vs other (§9.1 #12)."""

    supplier: list[ChequeResponse]
    other: list[ChequeResponse]
    supplier_total_irr: int
    other_total_irr: int

    @classmethod
    def from_summary(cls, summary: EndorsementSummary) -> EndorsementSummaryResponse:
        return cls(
            supplier=[ChequeResponse.from_cheque(c) for c in summary.supplier],
            other=[ChequeResponse.from_cheque(c) for c in summary.other],
            supplier_total_irr=summary.supplier_total_irr,
            other_total_irr=summary.other_total_irr,
        )


class CheckbookResponse(BaseModel):
    """One checkbook with leaf counts + Fintech inquiry (report 8)."""

    checkbook_engine_id: int
    name: str
    bank_account_id: int | None
    total_leaves: int
    used_leaves: int
    voided_leaves: int
    remaining_leaves: int
    leaves_registered_in_sayad: int | None
    inquiry_simulated: bool
    inquiry_notes: list[str]

    @classmethod
    def from_status(cls, status: CheckbookStatus) -> CheckbookResponse:
        view = status.checkbook
        return cls(
            checkbook_engine_id=view.ref.engine_id,
            name=view.name,
            bank_account_id=view.bank_account_engine_id,
            total_leaves=view.total_leaves,
            used_leaves=view.used_leaves,
            voided_leaves=view.voided_leaves,
            remaining_leaves=view.remaining_leaves,
            leaves_registered_in_sayad=status.inquiry.leaves_registered_in_sayad,
            inquiry_simulated=status.inquiry.source == "simulator",
            inquiry_notes=[status.inquiry.note],
        )
