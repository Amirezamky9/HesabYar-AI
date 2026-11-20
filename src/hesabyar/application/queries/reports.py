"""Trial-balance query use cases over the accounting engine port (US-403).

REST, MCP and workers all call this service instead of the engine port
directly, so trial-balance reads have one implementation and one error
contract (design-eng spec §6.2/§6.3).
"""

from __future__ import annotations

from datetime import date

from hesabyar.application.errors import translate_port_errors
from hesabyar.application.queries.companies import CompanyQueryService
from hesabyar.domain.accounting.ports import AccountingEnginePort
from hesabyar.domain.accounting.refs import CompanyRef
from hesabyar.domain.accounting.value_objects import TrialBalanceView
from hesabyar.domain.common.dates import FiscalDate


class ReportQueryService:
    """Read-only trial-balance queries over an engine-neutral port."""

    def __init__(self, engine: AccountingEnginePort) -> None:
        self._engine = engine
        self._companies = CompanyQueryService(engine)

    def get_trial_balance(
        self,
        company: CompanyRef,
        date_from: date,
        date_to: date,
    ) -> TrialBalanceView:
        """Return the company's trial balance over posted moves only.

        A missing company raises :class:`CompanyNotFoundError`: the
        engine answers an unknown company id with an empty result, so
        the company is verified first and an empty trial balance always
        means a real company with no posted moves in the period. A
        reversed range raises ``ValueError`` (the route maps it to a
        typed 422 ``VALIDATION_ERROR``).
        """
        if date_to < date_from:
            raise ValueError("date_to must be on or after date_from.")
        self._companies.get_company(company)
        with translate_port_errors():
            return self._engine.get_trial_balance(
                company,
                FiscalDate(date_from),
                FiscalDate(date_to),
            )
