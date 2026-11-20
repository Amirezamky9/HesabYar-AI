"""Response schemas for the company endpoints (US-105).

``CompanyResponse.id`` is the HesabYar API id of the company — the
``engine_companies`` row id that scopes the company to the caller's
tenant. ``engine_id`` is the id the accounting engine knows the company
by; both are accepted wherever a ``{company_id}`` or ``company`` query
parameter is expected (see ``docs/rest-api.md``). Responses map from the
domain ``CompanyView`` — no Tryton record or ORM object ever leaks.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from hesabyar.domain.accounting.value_objects import CompanyView


class CompanyResponse(BaseModel):
    """One company visible to the caller."""

    id: str = Field(
        description="HesabYar API id of the company (the tenant-scoped engine_companies row id)"
    )
    engine_id: int = Field(description="Id of the company in the accounting engine")
    name: str = Field(description="Company display name from the engine")
    currency_code: str = Field(description="Company currency code (ISO 4217, e.g. 'IRR')")

    @classmethod
    def from_view(cls, view: CompanyView, row_id: str) -> CompanyResponse:
        """Build a response from a domain view and its tenant mapping row id."""
        return cls(
            id=row_id,
            engine_id=view.ref.engine_id,
            name=view.name,
            currency_code=view.currency_code,
        )
