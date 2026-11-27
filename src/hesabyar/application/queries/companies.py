"""Company query use cases over the accounting engine port.

REST, MCP and workers all call these services instead of the engine port
directly, so company reads have one implementation and one error contract
(design-eng spec §6.2/§6.3).
"""

from __future__ import annotations

from collections.abc import Sequence

from hesabyar.application.errors import CompanyNotFoundError, translate_port_errors
from hesabyar.domain.accounting.ports import AccountingEnginePort
from hesabyar.domain.accounting.refs import CompanyRef
from hesabyar.domain.accounting.value_objects import CompanyView


class CompanyQueryService:
    """Read-only company queries over an engine-neutral port."""

    def __init__(self, engine: AccountingEnginePort) -> None:
        self._engine = engine

    def list_companies(self) -> Sequence[CompanyView]:
        """Return every company visible to the engine credentials."""
        with translate_port_errors():
            return self._engine.list_companies()

    def get_company(self, company: CompanyRef) -> CompanyView:
        """Return one company, or raise :class:`CompanyNotFoundError`.

        The port has no single-company read, so this filters the company
        list; the engine holds few companies, so no extra round trip per
        company is needed.
        """
        for view in self.list_companies():
            if view.ref == company:
                return view
        raise CompanyNotFoundError(
            f"Company {company.engine_id} does not exist in the engine.",
            details={"company_id": company.engine_id},
        )
