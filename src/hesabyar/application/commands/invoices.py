"""Invoice draft use cases over the accounting engine port (US-205).

REST, MCP and workers all call this service instead of the engine port
directly, so invoice workflows have one implementation and one error
contract (design-eng spec §6.2/§6.3). The service owns the workflow —
draft, validate — but no engine behaviour: every state change goes
through the port, and Tryton remains the only posting authority.

Finalizing an invoice in the engine (the Tryton ``validate_invoice``
transition, draft -> validated) is deliberately NOT here: the port
exposes only the read-only HesabYar validation check
(``validate_invoice_draft``), mirroring how ``post_journal`` came in
US-201 after ``validate_journal_draft`` in US-003. A finalize use case
joins when its port method lands (see ``docs/adapter.md``).
"""

from __future__ import annotations

from collections.abc import Sequence

from hesabyar.application.errors import translate_port_errors
from hesabyar.application.queries.companies import CompanyQueryService
from hesabyar.domain.accounting.commands import CreateInvoiceDraft, InvoiceDraftLine
from hesabyar.domain.accounting.ports import AccountingEnginePort
from hesabyar.domain.accounting.refs import (
    AccountRef,
    CompanyRef,
    InvoiceRef,
    JournalRef,
    PartyAddressRef,
    PartyRef,
)
from hesabyar.domain.accounting.value_objects import InvoiceView, ValidationSummary
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.errors import InvoiceNotFoundError


class InvoiceDraftService:
    """Invoice draft workflows over an engine-neutral port."""

    def __init__(self, engine: AccountingEnginePort) -> None:
        self._engine = engine
        self._companies = CompanyQueryService(engine)

    # -- create / read ----------------------------------------------------

    def create_draft(
        self,
        company: CompanyRef,
        invoice_type: str,
        party: PartyRef,
        date: FiscalDate,
        lines: Sequence[InvoiceDraftLine],
        description: str = "",
        reference: str = "",
        journal: JournalRef | None = None,
        account: AccountRef | None = None,
        invoice_address: PartyAddressRef | None = None,
    ) -> InvoiceRef:
        """Create a draft invoice in the engine.

        The company is verified first so a mapping row pointing at a
        company the engine does not know fails as ``COMPANY_NOT_FOUND``
        instead of surfacing as a confusing party/account error. The
        ``CreateInvoiceDraft`` command enforces the invoice invariants
        at construction (≥ 1 priced line, positive quantities,
        integral line amounts, total > 0); the adapter additionally
        verifies the party and its invoice address, the receivable /
        payable account and the journal, and every line account.
        """
        self._companies.get_company(company)
        command = CreateInvoiceDraft(
            company=company,
            invoice_type=invoice_type,
            party=party,
            date=date,
            lines=tuple(lines),
            description=description,
            reference=reference,
            journal=journal,
            account=account,
            invoice_address=invoice_address,
        )
        with translate_port_errors():
            return self._engine.create_invoice_draft(command)

    def get_invoice(self, invoice: InvoiceRef) -> InvoiceView:
        """Return one invoice, or raise ``InvoiceNotFoundError``."""
        with translate_port_errors():
            view = self._engine.get_invoice(invoice)
        if view is None:
            raise InvoiceNotFoundError(
                f"Invoice {invoice.engine_id} does not exist in the engine.",
                details={"invoice_id": invoice.engine_id},
            )
        return view

    def list_invoices(
        self,
        company: CompanyRef,
        invoice_type: str | None = None,
        state: str | None = None,
    ) -> Sequence[InvoiceView]:
        """Return the company's invoices, newest first.

        A missing company raises :class:`CompanyNotFoundError`: the
        engine answers an unknown company id with an empty list, so the
        company is verified first and an empty result always means a
        real company with no invoices yet. Invalid ``invoice_type`` /
        ``state`` filters fail as ``LEDGER_INVALID_INVOICE`` (the
        adapter validates them before delegating).
        """
        self._companies.get_company(company)
        with translate_port_errors():
            return self._engine.list_invoices(company, invoice_type, state)

    # -- validate ----------------------------------------------------------

    def validate_invoice_draft(self, invoice: InvoiceRef) -> ValidationSummary:
        """Validate a draft invoice against the postability invariants.

        Read-only: never mutates the invoice and never calls the
        engine's ``validate_invoice`` transition. A missing invoice
        raises ``InvoiceNotFoundError``.
        """
        with translate_port_errors():
            return self._engine.validate_invoice_draft(invoice)
