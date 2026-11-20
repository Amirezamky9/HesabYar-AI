"""Bank-statement query use cases over the accounting engine port (US-205).

REST, MCP and workers all call this service instead of the engine port
directly, so statement reads have one implementation and one error
contract (design-eng spec §6.2/§6.3).

The port exposes statements read-only: importing and reconciling
statements is a later story (see ``docs/adapter.md``). Tryton owns the
documents; HesabYar keeps no duplicate statement ledger.

``get_import_status`` is a read-only summary derived purely from port
reads — how many statements the company has, in which states, and how
many of their lines are already matched to an invoice — so the
dashboard can show import state without inventing data. It is NOT an
import operation: ``POST /bank/statements/import`` (spec §9.2) needs
its own port method and lands with the import story.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from hesabyar.application.errors import translate_port_errors
from hesabyar.application.queries.companies import CompanyQueryService
from hesabyar.domain.accounting.ports import AccountingEnginePort
from hesabyar.domain.accounting.refs import BankStatementRef, CompanyRef
from hesabyar.domain.accounting.value_objects import BankStatementView
from hesabyar.domain.common.errors import BankStatementNotFoundError


@dataclass(frozen=True, slots=True)
class StatementImportStatus:
    """Read-only import status of a company's bank/cash statements.

    Derived purely from port reads (``list_bank_statements``):
    per-state counts, the newest statement, and how many statement
    lines are already matched to an invoice (``related_invoice`` set
    on the line) versus unmatched. Nothing here is invented — a
    company with no statements reports zeros and no latest statement.
    """

    company: CompanyRef
    total_statements: int
    draft_count: int
    validated_count: int
    posted_count: int
    cancelled_count: int
    latest_statement: BankStatementView | None
    total_lines: int
    matched_lines: int

    @property
    def unmatched_lines(self) -> int:
        """Lines not yet matched to an invoice."""
        return self.total_lines - self.matched_lines


class BankStatementQueryService:
    """Read-only bank-statement queries over an engine-neutral port."""

    def __init__(self, engine: AccountingEnginePort) -> None:
        self._engine = engine
        self._companies = CompanyQueryService(engine)

    def list_bank_statements(
        self, company: CompanyRef, state: str | None = None
    ) -> Sequence[BankStatementView]:
        """Return the company's bank/cash statements, newest first.

        A missing company raises :class:`CompanyNotFoundError`: the
        engine answers an unknown company id with an empty list, so the
        company is verified first and an empty result always means a
        real company with no statements yet. An invalid ``state``
        filter fails as ``LEDGER_INVALID_STATEMENT`` (the adapter
        validates it before delegating).
        """
        self._companies.get_company(company)
        with translate_port_errors():
            return self._engine.list_bank_statements(company, state)

    def get_bank_statement(self, statement: BankStatementRef) -> BankStatementView:
        """Return one bank/cash statement, or raise ``BankStatementNotFoundError``."""
        with translate_port_errors():
            view = self._engine.get_bank_statement(statement)
        if view is None:
            raise BankStatementNotFoundError(
                f"Bank statement {statement.engine_id} does not exist in the engine.",
                details={"statement_id": statement.engine_id},
            )
        return view

    def get_import_status(self, company: CompanyRef) -> StatementImportStatus:
        """Return the company's statement import status (derived from port reads)."""
        statements = self.list_bank_statements(company)
        by_state: dict[str, int] = {}
        total_lines = 0
        matched_lines = 0
        for view in statements:
            by_state[view.state] = by_state.get(view.state, 0) + 1
            total_lines += len(view.lines)
            matched_lines += sum(
                1 for line in view.lines if line.related_invoice is not None
            )
        # The port returns statements newest first, so the first is the latest.
        return StatementImportStatus(
            company=company,
            total_statements=len(statements),
            draft_count=by_state.get("draft", 0),
            validated_count=by_state.get("validated", 0),
            posted_count=by_state.get("posted", 0),
            cancelled_count=by_state.get("cancelled", 0),
            latest_statement=statements[0] if statements else None,
            total_lines=total_lines,
            matched_lines=matched_lines,
        )
