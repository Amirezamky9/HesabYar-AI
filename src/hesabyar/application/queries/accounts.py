"""Account query use cases over the accounting engine port.

REST, MCP and workers all call these services instead of the engine port
directly, so chart reads have one implementation and one error contract
(design-eng spec §6.2/§6.3).
"""

from __future__ import annotations

from collections.abc import Sequence

from hesabyar.application.errors import translate_port_errors
from hesabyar.application.queries.companies import CompanyQueryService
from hesabyar.domain.accounting.ports import AccountingEnginePort
from hesabyar.domain.accounting.refs import CompanyRef
from hesabyar.domain.accounting.value_objects import AccountView


class AccountQueryService:
    """Read-only chart-of-accounts queries over an engine-neutral port."""

    def __init__(self, engine: AccountingEnginePort) -> None:
        self._engine = engine
        self._companies = CompanyQueryService(engine)

    def get_chart_of_accounts(self, company: CompanyRef) -> Sequence[AccountView]:
        """Return the company's chart of accounts, ordered by account code.

        A missing company raises :class:`CompanyNotFoundError`: the engine
        answers an unknown company id with an empty chart, so the company
        is verified first and an empty result always means a real company
        with no accounts yet.
        """
        self._companies.get_company(company)
        with translate_port_errors():
            return self._engine.get_chart_of_accounts(company)
