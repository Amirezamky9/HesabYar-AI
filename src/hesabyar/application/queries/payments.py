"""Payment query use cases over the accounting engine port (US-205).

REST, MCP and workers all call this service instead of the engine port
directly, so payment reads have one implementation and one error
contract (design-eng spec §6.2/§6.3).

The port exposes payments read-only: creating and processing payments
is a later story (see ``docs/adapter.md``). Tryton owns the documents;
HesabYar keeps no duplicate payment ledger.
"""

from __future__ import annotations

from collections.abc import Sequence

from hesabyar.application.errors import translate_port_errors
from hesabyar.application.queries.companies import CompanyQueryService
from hesabyar.domain.accounting.ports import AccountingEnginePort
from hesabyar.domain.accounting.refs import CompanyRef, PaymentRef
from hesabyar.domain.accounting.value_objects import PaymentView
from hesabyar.domain.common.errors import PaymentNotFoundError


class PaymentQueryService:
    """Read-only payment queries over an engine-neutral port."""

    def __init__(self, engine: AccountingEnginePort) -> None:
        self._engine = engine
        self._companies = CompanyQueryService(engine)

    def list_payments(
        self,
        company: CompanyRef,
        kind: str | None = None,
        state: str | None = None,
    ) -> Sequence[PaymentView]:
        """Return the company's payments, newest first.

        A missing company raises :class:`CompanyNotFoundError`: the
        engine answers an unknown company id with an empty list, so the
        company is verified first and an empty result always means a
        real company with no payments yet. Invalid ``kind`` / ``state``
        filters fail as ``LEDGER_INVALID_PAYMENT`` (the adapter
        validates them before delegating).
        """
        self._companies.get_company(company)
        with translate_port_errors():
            return self._engine.list_payments(company, kind, state)

    def get_payment(self, payment: PaymentRef) -> PaymentView:
        """Return one payment, or raise ``PaymentNotFoundError``."""
        with translate_port_errors():
            view = self._engine.get_payment(payment)
        if view is None:
            raise PaymentNotFoundError(
                f"Payment {payment.engine_id} does not exist in the engine.",
                details={"payment_id": payment.engine_id},
            )
        return view
