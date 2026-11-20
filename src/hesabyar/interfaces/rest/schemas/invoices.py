"""Request/response schemas for the invoice endpoints (US-205, spec §9.2).

Money follows the §9.3 contract: integer Rial amounts (``currency`` /
``unit`` metadata on every invoice response), never floats, never
Toman. Quantities are Decimals (quantities multiply into money, and
spec §8.3 forbids float arithmetic in financial paths); line amounts
(quantity x unit price) must be integral Rial. Dates are ISO Gregorian
(``YYYY-MM-DD``).

``InvoiceResponse.id`` is the invoice's id in the accounting engine
(invoices have no HesabYar-side mapping row); ``company_id`` is the
HesabYar API id of the owning company, matching ``CompanyResponse.id``.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from hesabyar.domain.accounting.value_objects import InvoiceLineView, InvoiceView


class InvoiceDraftLineRequest(BaseModel):
    """One priced line of an invoice draft."""

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


class CreateInvoiceDraftRequest(BaseModel):
    """Create a draft invoice in the engine."""

    company_id: str = Field(
        description="API company id (HesabYar row id or engine company id)"
    )
    invoice_type: str = Field(
        description="Invoice type: 'out' (customer) or 'in' (supplier)"
    )
    party_id: int = Field(gt=0, description="Engine id of the party (customer/supplier)")
    date: datetime.date = Field(description="Invoice date, ISO Gregorian (YYYY-MM-DD)")
    description: str = Field(default="", description="Invoice description")
    reference: str = Field(default="", description="External reference (e.g. order number)")
    journal_id: int | None = Field(
        default=None,
        gt=0,
        description=(
            "Engine id of the accounting journal; when omitted, the "
            "engine's single revenue (out) / expense (in) journal is used"
        ),
    )
    account_id: int | None = Field(
        default=None,
        gt=0,
        description=(
            "Engine id of the receivable (out) / payable (in) account; "
            "when omitted, the company's single account of the right type "
            "is used"
        ),
    )
    invoice_address_id: int | None = Field(
        default=None,
        gt=0,
        description=(
            "Engine id of the party's invoice address; when omitted, the "
            "party's first address is used"
        ),
    )
    lines: list[InvoiceDraftLineRequest] = Field(
        description=(
            "Invoice lines (at least one priced line; the total must be "
            "greater than zero)"
        )
    )


class InvoiceLineResponse(BaseModel):
    """One line of an invoice, as read back from the engine."""

    account_id: int = Field(description="Engine id of the line account")
    quantity: Decimal = Field(
        description="Quantity (serializes as a decimal string, spec §9.3)"
    )
    unit_price: int = Field(description="Unit price in integer Rial")
    amount: int = Field(description="Line amount in integer Rial (quantity x unit price)")
    line_type: str = Field(
        description="Engine line kind: 'line' (priced), 'subtotal', 'title' or 'comment'"
    )
    description: str = Field(description="Line description")

    @classmethod
    def from_view(cls, view: InvoiceLineView) -> InvoiceLineResponse:
        """Build a response line from a domain view."""
        return cls(
            account_id=view.account.engine_id,
            quantity=view.quantity,
            unit_price=view.unit_price.amount,
            amount=view.amount.amount,
            line_type=view.line_type,
            description=view.description,
        )


class InvoiceResponse(BaseModel):
    """An invoice as read back from the engine (any state)."""

    id: int = Field(description="Id of the invoice in the accounting engine")
    company_id: str = Field(
        description="HesabYar API id of the owning company (the engine_companies row id)"
    )
    engine_company_id: int = Field(description="Engine id of the owning company")
    invoice_type: str = Field(description="Invoice type: 'out' (customer) or 'in' (supplier)")
    party_id: int = Field(description="Engine id of the party (customer/supplier)")
    party_name: str = Field(description="Party display name from the engine")
    number: str | None = Field(
        default=None,
        description="Engine-assigned invoice number; null until the engine assigns it",
    )
    invoice_date: datetime.date | None = Field(
        default=None, description="Invoice date; null when the draft has no date yet"
    )
    accounting_date: datetime.date | None = Field(
        default=None, description="Accounting date, when set"
    )
    state: str = Field(
        description="Engine state: 'draft', 'validated', 'posted', 'paid' or 'cancelled'"
    )
    description: str = Field(description="Invoice description")
    reference: str = Field(description="External reference")
    journal_id: int | None = Field(
        default=None, description="Engine id of the accounting journal; null when unset"
    )
    account_id: int = Field(description="Engine id of the receivable/payable account")
    currency: str = Field(description="Invoice currency (ISO code)")
    unit: str = Field(default="rial", description="Money unit of all amounts")
    untaxed_amount: int = Field(description="Untaxed total in integer Rial")
    tax_amount: int = Field(description="Tax total in integer Rial")
    total_amount: int = Field(description="Invoice total in integer Rial")
    lines: list[InvoiceLineResponse] = Field(description="Invoice lines")

    @classmethod
    def from_view(cls, view: InvoiceView, company_id: str) -> InvoiceResponse:
        """Build a response from a domain view and its company's API id."""
        return cls(
            id=view.ref.engine_id,
            company_id=company_id,
            engine_company_id=view.company.engine_id,
            invoice_type=view.invoice_type,
            party_id=view.party.engine_id,
            party_name=view.party_name,
            number=view.number,
            invoice_date=(
                view.invoice_date.gregorian_date if view.invoice_date is not None else None
            ),
            accounting_date=(
                view.accounting_date.gregorian_date
                if view.accounting_date is not None
                else None
            ),
            state=view.state,
            description=view.description,
            reference=view.reference,
            journal_id=view.journal.engine_id if view.journal is not None else None,
            account_id=view.account.engine_id,
            currency=view.currency_code,
            untaxed_amount=view.untaxed_amount.amount,
            tax_amount=view.tax_amount.amount,
            total_amount=view.total_amount.amount,
            lines=[InvoiceLineResponse.from_view(line) for line in view.lines],
        )
