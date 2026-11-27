"""Ports (interfaces) for the cash-box bounded context.

The application layer talks to persistence ONLY through this port —
it never imports infrastructure (spec §6.3; the architecture guards
enforce it). The review queue is reached through its own ports
(``ReviewQueuePort`` / ``ReviewAuditPort``) and services — the
cash-box module files ``cashbox_variance`` items through the
``ReviewQueueCommandService``, never by writing the queue table
directly.
"""

from __future__ import annotations

import datetime
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass

from hesabyar.domain.cashbox.models import (
    CashBox,
    CashBoxSettings,
    CountSession,
    CountSessionStatus,
)


@dataclass(frozen=True)
class CountSessionFilters:
    """List filters for counting sessions (all optional; AND-combined)."""

    cash_box_id: str | None = None
    company_id: str | None = None
    status: CountSessionStatus | None = None
    date_from: datetime.date | None = None
    date_to: datetime.date | None = None


class CashBoxPort(ABC):
    """Abstract port for cash boxes, counting sessions and settings."""

    # -- cash boxes ------------------------------------------------------

    @abstractmethod
    def list_cash_boxes(
        self, tenant_id: str, company_id: str | None = None
    ) -> Sequence[CashBox]:
        """The tenant's cash boxes (optionally one company), oldest first."""
        ...

    @abstractmethod
    def get_cash_box(self, box_id: str, tenant_id: str) -> CashBox | None:
        """One cash box for the owning tenant, or None (also when another tenant's)."""
        ...

    @abstractmethod
    def get_cash_box_by_journal(
        self, tenant_id: str, company_id: str, statement_journal_id: int
    ) -> CashBox | None:
        """The box registered on a Statement Journal, or None.

        One box per journal: a journal belongs to exactly one box, so
        registering a second box on the same journal is a conflict.
        """
        ...

    @abstractmethod
    def save_cash_box(self, box: CashBox) -> CashBox:
        """Insert a new cash box; returns it with timestamps set."""
        ...

    @abstractmethod
    def update_cash_box(self, box: CashBox) -> CashBox:
        """Persist a changed cash box (name/kind/ceiling/active); returns it."""
        ...

    # -- counting sessions -------------------------------------------------

    @abstractmethod
    def list_sessions(
        self, tenant_id: str, filters: CountSessionFilters
    ) -> Sequence[CountSession]:
        """The tenant's sessions matching the filters, newest first."""
        ...

    @abstractmethod
    def get_session(self, session_id: str, tenant_id: str) -> CountSession | None:
        """One session for the owning tenant, or None (also when another tenant's)."""
        ...

    @abstractmethod
    def get_open_session_for_box(self, cash_box_id: str, tenant_id: str) -> CountSession | None:
        """The box's open (non-archived) session, or None.

        One open session per box: a box cannot be counted twice at
        once, so starting a session while one is open is a conflict.
        """
        ...

    @abstractmethod
    def save_session(self, session: CountSession) -> CountSession:
        """Insert a new counting session; returns it with timestamps set."""
        ...

    @abstractmethod
    def update_session(self, session: CountSession) -> CountSession:
        """Persist a changed session (status/count/signatures/links); returns it."""
        ...

    # -- settings ------------------------------------------------------------

    @abstractmethod
    def get_settings(self, tenant_id: str) -> CashBoxSettings | None:
        """The tenant's cash-box settings, or None when never configured."""
        ...

    @abstractmethod
    def upsert_settings(self, settings: CashBoxSettings) -> CashBoxSettings:
        """Insert or replace the tenant's cash-box settings row."""
        ...
