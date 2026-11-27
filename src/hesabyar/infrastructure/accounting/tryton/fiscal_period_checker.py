"""Engine-backed fiscal-period checker for the policy gate (US-503).

Implements
:class:`hesabyar.domain.integrations.ports.FiscalPeriodCheckerPort` by
reading fiscal periods through the ``AccountingEnginePort`` — no
direct SQL to the Tryton database, ever (the Phase 4 port-bypass
lesson; ADR 0002).

Fail-closed: the gate never auto-posts into a period it cannot prove
open, so ANY uncertainty answers False — no period contains the date,
the containing period is closed or locked, or the engine cannot be
reached at all.
"""

from __future__ import annotations

import datetime

from hesabyar.domain.accounting.ports import AccountingEnginePort
from hesabyar.domain.accounting.refs import CompanyRef
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.errors import HesabYarError


class TrytonFiscalPeriodChecker:
    """Fiscal-period state via the accounting engine port."""

    def __init__(self, engine: AccountingEnginePort) -> None:
        self._engine = engine

    def is_period_open(self, company: CompanyRef, on: datetime.date) -> bool:
        """True only when ``on`` falls in an OPEN fiscal period of ``company``."""
        try:
            periods = self._engine.list_periods(company)
        except HesabYarError:
            # Engine unreachable or the read failed: fail closed.
            return False
        day = FiscalDate.from_gregorian(on)
        for period in periods:
            if period.contains(day):
                return period.is_open
        # No period contains the date: fail closed.
        return False
