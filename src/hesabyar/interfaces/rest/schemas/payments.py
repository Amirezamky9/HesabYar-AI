"""Response schemas for the payment endpoints (US-205, spec §9.2).

Money follows the §9.3 contract: integer Rial amounts (``currency`` /
``unit`` metadata on every payment response), never floats, never
Toman. Dates are ISO Gregorian (``YYYY-MM-DD``).

``PaymentResponse.id`` is the payment's id in the accounting engine
(payments have no HesabYar-side mapping row); ``company_id`` is the
HesabYar API id of the owning company, matching ``CompanyResponse.id``.
"""

from __future__ import annotations

import datetime

from pydantic import BaseModel, Field

from hesabyar.domain.accounting.value_objects import PaymentView


class PaymentResponse(BaseModel):
    """A payment as read back from the engine (any state)."""

    id: int = Field(description="Id of the payment in the accounting engine")
    company_id: str = Field(
        description="HesabYar API id of the owning company (the engine_companies row id)"
    )
    engine_company_id: int = Field(description="Engine id of the owning company")
    number: str = Field(description="Engine-assigned payment number")
    kind: str = Field(description="Payment kind: 'receivable' or 'payable'")
    party_id: int = Field(description="Engine id of the party (customer/supplier)")
    party_name: str = Field(description="Party display name from the engine")
    date: datetime.date = Field(description="Payment date")
    amount: int = Field(description="Payment amount in integer Rial")
    state: str = Field(
        description=(
            "Engine state: 'draft', 'submitted', 'approved', 'processing', "
            "'succeeded' or 'failed'"
        )
    )
    reference: str = Field(description="Payment reference")
    journal_name: str = Field(description="Name of the payment journal")
    currency: str = Field(description="Payment currency (ISO code)")
    unit: str = Field(default="rial", description="Money unit of all amounts")

    @classmethod
    def from_view(cls, view: PaymentView, company_id: str) -> PaymentResponse:
        """Build a response from a domain view and its company's API id."""
        return cls(
            id=view.ref.engine_id,
            company_id=company_id,
            engine_company_id=view.company.engine_id,
            number=view.number,
            kind=view.kind,
            party_id=view.party.engine_id,
            party_name=view.party_name,
            date=view.date.gregorian_date,
            amount=view.amount.amount,
            state=view.state,
            reference=view.reference,
            journal_name=view.journal_name,
            currency=view.currency_code,
        )
