"""Request/response schemas for the recurring-template endpoints (US-563).

Money follows the §9.3 contract: integer Rial amounts (``unit``:
``rial`` on every response), never floats, never Toman. Quantities
are Decimals (quantities multiply into money, and spec §8.3 forbids
float arithmetic in financial paths); line amounts (quantity x unit
price) must be integral Rial. Dates are ISO Gregorian
(``YYYY-MM-DD``); every occurrence also carries its Jalali label
(``1405-08-15``) plus the clamp report, so the preview shows a
day-31 clamp honestly instead of hiding it.

A template body carries exactly one payload: ``invoice`` for an
invoice template, ``journal`` for a journal template — the other
must be absent (a model validator enforces this, so a mixed body
is a 422, never a silent choice).
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, model_validator

from hesabyar.domain.recurring.errors import InvalidRecurringTemplateError
from hesabyar.domain.recurring.models import (
    InvoiceTemplateLine,
    InvoiceTemplatePayload,
    JournalTemplateLine,
    JournalTemplatePayload,
    OccurrencePreview,
    RecurringOccurrence,
    RecurringTemplate,
    TemplateKind,
)
from hesabyar.domain.recurring.schedules import RecurringSchedule, RecurringScheduleKind


class RecurringScheduleRequest(BaseModel):
    """A template's Jalali schedule."""

    kind: str = Field(
        description="Schedule shape: 'monthly' | 'every_n_months' | 'yearly'"
    )
    jalali_day: int = Field(
        ge=1, le=31, description="Jalali day of month to fire on (clamped to the month's length)"
    )
    jalali_month: int | None = Field(
        default=None,
        ge=1,
        le=12,
        description="Jalali month (yearly only)",
    )
    interval_months: int | None = Field(
        default=None,
        ge=1,
        description="Month step (every_n_months only; anchored at the start month)",
    )
    start_date: datetime.date = Field(
        description="First day an occurrence may fall on, ISO Gregorian (YYYY-MM-DD)"
    )
    end_date: datetime.date | None = Field(
        default=None,
        description="Last day an occurrence may fall on (end condition; optional)",
    )
    max_occurrences: int | None = Field(
        default=None,
        ge=1,
        description="Maximum number of occurrences (end condition; optional)",
    )

    def to_schedule(self) -> RecurringSchedule:
        """Build the domain schedule (invariants enforced at construction)."""
        try:
            kind = RecurringScheduleKind(self.kind)
        except ValueError as exc:
            # A raw ValueError here would be a 500; the client sent a
            # bad kind, so it is a 422 like every other bad schedule.
            raise InvalidRecurringTemplateError(
                f"Unknown schedule kind {self.kind!r}; allowed: 'monthly', "
                "'every_n_months', 'yearly'.",
                details={"kind": self.kind},
            ) from exc
        return RecurringSchedule(
            kind=kind,
            jalali_day=self.jalali_day,
            start_date=self.start_date,
            jalali_month=self.jalali_month,
            interval_months=self.interval_months,
            end_date=self.end_date,
            max_occurrences=self.max_occurrences,
        )


class RecurringScheduleResponse(BaseModel):
    """A template's Jalali schedule, as stored."""

    kind: str = Field(description="monthly | every_n_months | yearly")
    jalali_day: int = Field(description="Jalali day of month")
    jalali_month: int | None = Field(description="Jalali month (yearly only)")
    interval_months: int | None = Field(description="Month step (every_n_months only)")
    start_date: datetime.date = Field(description="First occurrence date (Gregorian)")
    end_date: datetime.date | None = Field(description="End date (Gregorian); null when open-ended")
    max_occurrences: int | None = Field(description="Max occurrences; null when open-ended")

    @classmethod
    def from_schedule(cls, schedule: RecurringSchedule) -> RecurringScheduleResponse:
        """Map a domain schedule to its wire form."""
        return cls(
            kind=schedule.kind.value,
            jalali_day=schedule.jalali_day,
            jalali_month=schedule.jalali_month,
            interval_months=schedule.interval_months,
            start_date=schedule.start_date,
            end_date=schedule.end_date,
            max_occurrences=schedule.max_occurrences,
        )


class RecurringInvoiceLineRequest(BaseModel):
    """One priced line of an invoice template."""

    account_id: int = Field(gt=0, description="Engine id of the line account")
    unit_price: int = Field(
        ge=0, description="Unit price in integer Rial (0 for a free line)"
    )
    quantity: Decimal = Field(
        default=Decimal(1),
        gt=0,
        description="Quantity (Decimal; quantity x unit price must be integral Rial)",
    )
    description: str = Field(default="", description="Line description")


class RecurringInvoicePayloadRequest(BaseModel):
    """The invoice a template generates, once per occurrence."""

    invoice_type: str = Field(description="Invoice type: 'out' (customer) or 'in' (supplier)")
    party_id: int = Field(gt=0, description="Engine id of the party (customer/supplier)")
    description: str = Field(default="", description="Invoice description")
    reference: str = Field(default="", description="External reference (e.g. order number)")
    journal_id: int | None = Field(
        default=None, gt=0, description="Engine id of the accounting journal (optional)"
    )
    account_id: int | None = Field(
        default=None,
        gt=0,
        description="Engine id of the receivable (out) / payable (in) account (optional)",
    )
    invoice_address_id: int | None = Field(
        default=None, gt=0, description="Engine id of the party's invoice address (optional)"
    )
    lines: list[RecurringInvoiceLineRequest] = Field(
        description="Invoice lines (at least one priced line; the total must be > 0)"
    )

    def to_payload(self) -> InvoiceTemplatePayload:
        """Build the domain payload (invariants enforced at construction)."""
        return InvoiceTemplatePayload(
            invoice_type=self.invoice_type,
            party_engine_id=self.party_id,
            lines=tuple(
                InvoiceTemplateLine(
                    account_engine_id=line.account_id,
                    unit_price_irr=line.unit_price,
                    quantity=line.quantity,
                    description=line.description,
                )
                for line in self.lines
            ),
            description=self.description,
            reference=self.reference,
            journal_engine_id=self.journal_id,
            account_engine_id=self.account_id,
            invoice_address_engine_id=self.invoice_address_id,
        )


class RecurringInvoiceLineResponse(BaseModel):
    """One priced line of an invoice template, as stored."""

    account_id: int = Field(description="Engine id of the line account")
    unit_price: int = Field(description="Unit price in integer Rial")
    quantity: Decimal = Field(description="Quantity (serializes as a decimal string)")
    amount: int = Field(description="Line amount in integer Rial (quantity x unit price)")
    description: str = Field(description="Line description")

    @classmethod
    def from_line(cls, line: InvoiceTemplateLine) -> RecurringInvoiceLineResponse:
        """Map a domain line to its wire form."""
        return cls(
            account_id=line.account_engine_id,
            unit_price=line.unit_price_irr,
            quantity=line.quantity,
            amount=line.amount_irr,
            description=line.description,
        )


class RecurringInvoicePayloadResponse(BaseModel):
    """The invoice payload of an invoice template, as stored."""

    invoice_type: str = Field(description="out (customer) | in (supplier)")
    party_id: int = Field(description="Engine id of the party")
    description: str = Field(description="Invoice description")
    reference: str = Field(description="External reference")
    journal_id: int | None = Field(description="Engine id of the accounting journal")
    account_id: int | None = Field(description="Engine id of the receivable/payable account")
    invoice_address_id: int | None = Field(description="Engine id of the invoice address")
    lines: list[RecurringInvoiceLineResponse] = Field(description="Invoice lines")

    @classmethod
    def from_payload(cls, payload: InvoiceTemplatePayload) -> RecurringInvoicePayloadResponse:
        """Map a domain payload to its wire form."""
        return cls(
            invoice_type=payload.invoice_type,
            party_id=payload.party_engine_id,
            description=payload.description,
            reference=payload.reference,
            journal_id=payload.journal_engine_id,
            account_id=payload.account_engine_id,
            invoice_address_id=payload.invoice_address_engine_id,
            lines=[RecurringInvoiceLineResponse.from_line(line) for line in payload.lines],
        )


class RecurringJournalLineRequest(BaseModel):
    """One line of a journal template: exactly one side carries an amount."""

    account_id: int = Field(gt=0, description="Engine id of the account")
    debit: int = Field(
        default=0, ge=0, description="Debit amount in integer Rial (0 when credit-side)"
    )
    credit: int = Field(
        default=0, ge=0, description="Credit amount in integer Rial (0 when debit-side)"
    )
    description: str = Field(default="", description="Line description")
    party_id: int | None = Field(
        default=None, gt=0, description="Engine id of the line's party (tafsili)"
    )


class RecurringJournalPayloadRequest(BaseModel):
    """The journal a template generates, once per occurrence."""

    description: str = Field(default="", description="Journal description")
    journal_id: int | None = Field(
        default=None, gt=0, description="Engine id of the accounting journal (optional)"
    )
    lines: list[RecurringJournalLineRequest] = Field(
        description=(
            "Journal lines (at least two; each one-sided; debit total "
            "must equal credit total and be greater than zero)"
        )
    )

    def to_payload(self) -> JournalTemplatePayload:
        """Build the domain payload (invariants enforced at construction)."""
        return JournalTemplatePayload(
            lines=tuple(
                JournalTemplateLine(
                    account_engine_id=line.account_id,
                    debit_irr=line.debit,
                    credit_irr=line.credit,
                    description=line.description,
                    party_engine_id=line.party_id,
                )
                for line in self.lines
            ),
            description=self.description,
            journal_engine_id=self.journal_id,
        )


class RecurringJournalLineResponse(BaseModel):
    """One line of a journal template, as stored."""

    account_id: int = Field(description="Engine id of the account")
    debit: int = Field(description="Debit amount in integer Rial")
    credit: int = Field(description="Credit amount in integer Rial")
    description: str = Field(description="Line description")
    party_id: int | None = Field(description="Engine id of the line's party (tafsili)")

    @classmethod
    def from_line(cls, line: JournalTemplateLine) -> RecurringJournalLineResponse:
        """Map a domain line to its wire form."""
        return cls(
            account_id=line.account_engine_id,
            debit=line.debit_irr,
            credit=line.credit_irr,
            description=line.description,
            party_id=line.party_engine_id,
        )


class RecurringJournalPayloadResponse(BaseModel):
    """The journal payload of a journal template, as stored."""

    description: str = Field(description="Journal description")
    journal_id: int | None = Field(description="Engine id of the accounting journal")
    lines: list[RecurringJournalLineResponse] = Field(description="Journal lines")

    @classmethod
    def from_payload(cls, payload: JournalTemplatePayload) -> RecurringJournalPayloadResponse:
        """Map a domain payload to its wire form."""
        return cls(
            description=payload.description,
            journal_id=payload.journal_engine_id,
            lines=[RecurringJournalLineResponse.from_line(line) for line in payload.lines],
        )


def _check_template_body(values: Any) -> Any:
    """A template body carries exactly the payload its kind needs."""
    if not isinstance(values, dict):
        return values
    kind = values.get("kind")
    invoice = values.get("invoice")
    journal = values.get("journal")
    if kind == TemplateKind.INVOICE.value:
        if invoice is None:
            raise ValueError("An invoice template needs an 'invoice' payload.")
        if journal is not None:
            raise ValueError("An invoice template must not carry a 'journal' payload.")
    elif kind == TemplateKind.JOURNAL.value:
        if journal is None:
            raise ValueError("A journal template needs a 'journal' payload.")
        if invoice is not None:
            raise ValueError("A journal template must not carry an 'invoice' payload.")
    else:
        raise ValueError(f"kind must be 'invoice' or 'journal', got {kind!r}.")
    return values


class CreateRecurringTemplateRequest(BaseModel):
    """Create a recurring template."""

    company_id: str = Field(
        description="API company id (HesabYar row id or engine company id)"
    )
    kind: str = Field(description="Template kind: 'invoice' or 'journal'")
    name: str = Field(description="Operator-facing template name")
    schedule: RecurringScheduleRequest = Field(description="Jalali schedule")
    auto_post: bool = Field(
        default=False,
        description=(
            "Explicit auto-post opt-in (draft is the default; the policy "
            "gate still decides per occurrence)"
        ),
    )
    invoice: RecurringInvoicePayloadRequest | None = Field(
        default=None, description="Invoice payload (invoice templates only)"
    )
    journal: RecurringJournalPayloadRequest | None = Field(
        default=None, description="Journal payload (journal templates only)"
    )

    _check_body = model_validator(mode="before")(_check_template_body)


class UpdateRecurringTemplateRequest(BaseModel):
    """Update a recurring template; only the given fields change.

    The schedule is replaced wholesale when given (its shape cannot
    change once the template has an occurrence — only the end date
    and occurrence count stay editable then). The payload is
    replaced wholesale when given and must match the template's
    kind; a payload edit only affects future occurrences.
    """

    name: str | None = Field(default=None, description="New template name")
    schedule: RecurringScheduleRequest | None = Field(
        default=None, description="Replacement schedule (whole object)"
    )
    auto_post: bool | None = Field(default=None, description="New auto-post opt-in")
    invoice: RecurringInvoicePayloadRequest | None = Field(
        default=None, description="Replacement invoice payload (invoice templates only)"
    )
    journal: RecurringJournalPayloadRequest | None = Field(
        default=None, description="Replacement journal payload (journal templates only)"
    )

    @model_validator(mode="after")
    def _check_body(self) -> UpdateRecurringTemplateRequest:
        if self.invoice is not None and self.journal is not None:
            raise ValueError("An update carries at most one payload (invoice or journal).")
        if (
            self.name is None
            and self.schedule is None
            and self.auto_post is None
            and self.invoice is None
            and self.journal is None
        ):
            raise ValueError("An update must change at least one field.")
        return self


class OccurrencePreviewResponse(BaseModel):
    """One previewed occurrence: date + amount + clamp report."""

    occurrence_number: int = Field(description="Position in the sequence (1-based)")
    scheduled_date: datetime.date = Field(description="Scheduled date, ISO Gregorian")
    jalali_date: str = Field(description="The same date as a Jalali string (1405-08-15)")
    total_amount_irr: int = Field(description="Occurrence total in integer Rial")
    clamped: bool = Field(
        description="True when the Jalali day was clamped to the month's length"
    )
    requested_jalali_day: int = Field(description="The Jalali day the schedule asked for")
    actual_jalali_day: int = Field(description="The Jalali day the occurrence fires on")

    @classmethod
    def from_preview(cls, preview: OccurrencePreview) -> OccurrencePreviewResponse:
        """Map a domain preview row to its wire form."""
        return cls(
            occurrence_number=preview.occurrence_number,
            scheduled_date=preview.scheduled_date,
            jalali_date=preview.jalali_date,
            total_amount_irr=preview.total_amount_irr,
            clamped=preview.clamped,
            requested_jalali_day=preview.requested_jalali_day,
            actual_jalali_day=preview.actual_jalali_day,
        )


class RecurringTemplateResponse(BaseModel):
    """A recurring template, as stored."""

    id: str = Field(description="Template id (UUID)")
    company_id: str = Field(
        description="HesabYar API id of the owning company (the engine_companies row id)"
    )
    engine_company_id: int = Field(description="Engine id of the owning company")
    kind: str = Field(description="invoice | journal")
    name: str = Field(description="Operator-facing template name")
    status: str = Field(description="active | paused | completed")
    schedule: RecurringScheduleResponse = Field(description="Jalali schedule")
    auto_post: bool = Field(description="Explicit auto-post opt-in")
    total_amount_irr: int = Field(description="Payload total in integer Rial")
    unit: str = Field(default="rial", description="Money unit of all amounts")
    invoice: RecurringInvoicePayloadResponse | None = Field(
        description="Invoice payload (invoice templates only)"
    )
    journal: RecurringJournalPayloadResponse | None = Field(
        description="Journal payload (journal templates only)"
    )
    created_at: datetime.datetime = Field(description="Creation timestamp (UTC)")
    updated_at: datetime.datetime = Field(description="Last-modified timestamp (UTC)")

    @classmethod
    def from_template(cls, template: RecurringTemplate) -> RecurringTemplateResponse:
        """Map a domain template to its wire form."""
        invoice_payload = None
        journal_payload = None
        if isinstance(template.payload, InvoiceTemplatePayload):
            invoice_payload = RecurringInvoicePayloadResponse.from_payload(template.payload)
        elif isinstance(template.payload, JournalTemplatePayload):
            journal_payload = RecurringJournalPayloadResponse.from_payload(template.payload)
        return cls(
            id=template.id,
            company_id=template.company_id,
            engine_company_id=template.company_engine_id,
            kind=template.kind.value,
            name=template.name,
            status=template.status.value,
            schedule=RecurringScheduleResponse.from_schedule(template.schedule),
            auto_post=template.auto_post,
            total_amount_irr=template.total_amount_irr,
            invoice=invoice_payload,
            journal=journal_payload,
            created_at=template.created_at,
            updated_at=template.updated_at,
        )


class RecurringOccurrenceResponse(BaseModel):
    """One generated (or failed) occurrence of a template."""

    id: str = Field(description="Occurrence id (UUID)")
    template_id: str = Field(description="Owning template id")
    occurrence_number: int = Field(description="Position in the sequence (1-based)")
    scheduled_date: datetime.date = Field(description="Scheduled date, ISO Gregorian")
    jalali_date: str = Field(description="The same date as a Jalali string (1405-08-15)")
    clamped: bool = Field(description="True when the Jalali day was clamped")
    requested_jalali_day: int = Field(description="The Jalali day the schedule asked for")
    actual_jalali_day: int = Field(description="The Jalali day the occurrence fires on")
    status: str = Field(description="pending | draft | posted | failed")
    total_amount_irr: int = Field(description="Occurrence total in integer Rial")
    engine_document_id: int | None = Field(
        description="Engine id of the generated invoice / journal; null until generated"
    )
    gate_decision: dict[str, Any] | None = Field(
        description="Policy-gate verdict when auto-post was attempted"
    )
    error: str | None = Field(description="Failure text (failed occurrences / failed posts)")
    generated_at: datetime.datetime | None = Field(
        description="When the document was generated (UTC); null until generated"
    )

    @classmethod
    def from_occurrence(cls, occurrence: RecurringOccurrence) -> RecurringOccurrenceResponse:
        """Map a domain occurrence to its wire form."""
        return cls(
            id=occurrence.id,
            template_id=occurrence.template_id,
            occurrence_number=occurrence.occurrence_number,
            scheduled_date=occurrence.scheduled_date,
            jalali_date=occurrence.jalali_date,
            clamped=occurrence.clamped,
            requested_jalali_day=occurrence.requested_jalali_day,
            actual_jalali_day=occurrence.actual_jalali_day,
            status=occurrence.status.value,
            total_amount_irr=occurrence.total_amount_irr,
            engine_document_id=occurrence.engine_document_id,
            gate_decision=occurrence.gate_decision,
            error=occurrence.error,
            generated_at=occurrence.generated_at,
        )
