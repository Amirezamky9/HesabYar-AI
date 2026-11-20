"""PostgreSQL implementation of the cash-box port (US-516).

Implements ``domain/cashbox/ports.py`` (``CashBoxPort``) over the
``cash_boxes`` / ``cash_count_sessions`` / ``cashbox_settings``
tables. This is the ONLY place that knows the cash-box SQLAlchemy
models — the application layer talks to the port, never to these
tables (spec §6.3).

Persistence contract (same as every other writer in the codebase):
``save_*`` / ``update_*`` / ``upsert_settings`` flush so
ids/timestamps exist but do NOT commit — the caller (REST route)
owns the transaction.

Denominations and link refs are JSONB: denominations are a list of
``{denomination_irr, count}`` dicts; ``cheque_refs`` / ``order_refs``
/ ``installment_refs`` are lists of strings.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from hesabyar.domain.cashbox.models import (
    CashBox,
    CashBoxKind,
    CashBoxSettings,
    CountSession,
    CountSessionStatus,
    DenominationCount,
    VariancePolicy,
)
from hesabyar.domain.cashbox.ports import CashBoxPort, CountSessionFilters
from hesabyar.infrastructure.persistence.postgres.models import (
    CashBoxModel,
    CashBoxSettingsModel,
    CountSessionModel,
)


def _box_from_row(row: CashBoxModel) -> CashBox:
    """Map a cash-box row onto the domain dataclass."""
    return CashBox(
        id=row.id,
        tenant_id=row.tenant_id,
        company_id=row.company_id,
        name_fa=row.name_fa,
        kind=CashBoxKind(row.kind),
        statement_journal_id=row.statement_journal_id,
        cash_account_id=row.cash_account_id,
        cash_account_code=row.cash_account_code,
        engine_company_id=row.engine_company_id,
        ceiling_irr=row.ceiling_irr,
        active=row.active,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _denominations_from_json(raw: list[dict[str, Any]] | None) -> tuple[DenominationCount, ...]:
    """Parse the JSONB denomination breakdown (empty when none was stored)."""
    if not raw:
        return ()
    return tuple(
        DenominationCount(
            denomination_irr=int(item["denomination_irr"]),
            count=int(item["count"]),
        )
        for item in raw
    )


def _denominations_to_json(
    denominations: tuple[DenominationCount, ...],
) -> list[dict[str, int]]:
    """Render the denomination breakdown for JSONB storage."""
    return [
        {"denomination_irr": line.denomination_irr, "count": line.count}
        for line in denominations
    ]


def _session_from_row(row: CountSessionModel) -> CountSession:
    """Map a counting-session row onto the domain dataclass."""
    return CountSession(
        id=row.id,
        tenant_id=row.tenant_id,
        company_id=row.company_id,
        cash_box_id=row.cash_box_id,
        cashier=row.cashier,
        supervisor=row.supervisor,
        status=CountSessionStatus(row.status),
        book_balance_irr=row.book_balance_irr,
        created_at=row.created_at,
        counted_amount_irr=row.counted_amount_irr,
        denominations=_denominations_from_json(row.denominations),
        minutes_text=row.minutes_text,
        witnessed_by=row.witnessed_by,
        witnessed_at=row.witnessed_at,
        minutes_cashier_signed_by=row.minutes_cashier_signed_by,
        minutes_cashier_signed_at=row.minutes_cashier_signed_at,
        minutes_supervisor_signed_by=row.minutes_supervisor_signed_by,
        minutes_supervisor_signed_at=row.minutes_supervisor_signed_at,
        minutes_manager_signed_by=row.minutes_manager_signed_by,
        minutes_manager_signed_at=row.minutes_manager_signed_at,
        review_item_id=row.review_item_id,
        draft_journal_id=row.draft_journal_id,
        posted_journal_id=row.posted_journal_id,
        notes=row.notes,
        updated_at=row.updated_at,
        engine_company_id=row.engine_company_id,
        cheque_refs=tuple(row.cheque_refs or []),
        deposit_ref=row.deposit_ref,
        order_refs=tuple(row.order_refs or []),
        installment_refs=tuple(row.installment_refs or []),
    )


def _settings_from_row(row: CashBoxSettingsModel) -> CashBoxSettings:
    """Map a settings row onto the domain dataclass."""
    return CashBoxSettings(
        tenant_id=row.tenant_id,
        variance_policy=VariancePolicy(row.variance_policy),
        material_variance_threshold_irr=row.material_variance_threshold_irr,
        count_reminder_days=row.count_reminder_days,
        count_schedule=row.count_schedule,
        supervisor_required=row.supervisor_required,
        shortage_settlement_hours=row.shortage_settlement_hours,
        updated_at=row.updated_at,
    )


class SqlAlchemyCashBoxRepository(CashBoxPort):
    """SQLAlchemy implementation of the cash-box port."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- cash boxes ------------------------------------------------------

    def list_cash_boxes(
        self, tenant_id: str, company_id: str | None = None
    ) -> Sequence[CashBox]:
        stmt = select(CashBoxModel).where(CashBoxModel.tenant_id == tenant_id)
        if company_id is not None:
            stmt = stmt.where(CashBoxModel.company_id == company_id)
        stmt = stmt.order_by(CashBoxModel.created_at.asc(), CashBoxModel.id.asc())
        rows = self._session.execute(stmt).scalars().all()
        return [_box_from_row(row) for row in rows]

    def get_cash_box(self, box_id: str, tenant_id: str) -> CashBox | None:
        stmt = select(CashBoxModel).where(
            CashBoxModel.id == box_id,
            CashBoxModel.tenant_id == tenant_id,
        )
        row = self._session.execute(stmt).scalar_one_or_none()
        return _box_from_row(row) if row is not None else None

    def get_cash_box_by_journal(
        self, tenant_id: str, company_id: str, statement_journal_id: int
    ) -> CashBox | None:
        stmt = select(CashBoxModel).where(
            CashBoxModel.tenant_id == tenant_id,
            CashBoxModel.company_id == company_id,
            CashBoxModel.statement_journal_id == statement_journal_id,
        )
        row = self._session.execute(stmt).scalar_one_or_none()
        return _box_from_row(row) if row is not None else None

    def save_cash_box(self, box: CashBox) -> CashBox:
        row = CashBoxModel(
            id=box.id,
            tenant_id=box.tenant_id,
            company_id=box.company_id,
            engine_company_id=box.engine_company_id,
            name_fa=box.name_fa,
            kind=box.kind.value,
            statement_journal_id=box.statement_journal_id,
            cash_account_id=box.cash_account_id,
            cash_account_code=box.cash_account_code,
            ceiling_irr=box.ceiling_irr,
            active=box.active,
            created_at=box.created_at or datetime.datetime.now(datetime.UTC),
            updated_at=box.updated_at or datetime.datetime.now(datetime.UTC),
        )
        self._session.add(row)
        self._session.flush()
        return _box_from_row(row)

    def update_cash_box(self, box: CashBox) -> CashBox:
        stmt = select(CashBoxModel).where(
            CashBoxModel.id == box.id,
            CashBoxModel.tenant_id == box.tenant_id,
        )
        row = self._session.execute(stmt).scalar_one_or_none()
        if row is None:
            raise ValueError(f"Cash box {box.id!r} does not exist.")
        row.name_fa = box.name_fa
        row.kind = box.kind.value
        row.ceiling_irr = box.ceiling_irr
        row.active = box.active
        row.updated_at = box.updated_at or datetime.datetime.now(datetime.UTC)
        self._session.flush()
        return _box_from_row(row)

    # -- counting sessions -------------------------------------------------

    def list_sessions(
        self, tenant_id: str, filters: CountSessionFilters
    ) -> Sequence[CountSession]:
        stmt = select(CountSessionModel).where(CountSessionModel.tenant_id == tenant_id)
        if filters.cash_box_id is not None:
            stmt = stmt.where(CountSessionModel.cash_box_id == filters.cash_box_id)
        if filters.company_id is not None:
            stmt = stmt.where(CountSessionModel.company_id == filters.company_id)
        if filters.status is not None:
            stmt = stmt.where(CountSessionModel.status == filters.status.value)
        if filters.date_from is not None:
            start = datetime.datetime.combine(filters.date_from, datetime.time.min, datetime.UTC)
            stmt = stmt.where(CountSessionModel.created_at >= start)
        if filters.date_to is not None:
            end = datetime.datetime.combine(filters.date_to, datetime.time.max, datetime.UTC)
            stmt = stmt.where(CountSessionModel.created_at <= end)
        stmt = stmt.order_by(CountSessionModel.created_at.desc(), CountSessionModel.id.desc())
        rows = self._session.execute(stmt).scalars().all()
        return [_session_from_row(row) for row in rows]

    def get_session(self, session_id: str, tenant_id: str) -> CountSession | None:
        stmt = select(CountSessionModel).where(
            CountSessionModel.id == session_id,
            CountSessionModel.tenant_id == tenant_id,
        )
        row = self._session.execute(stmt).scalar_one_or_none()
        return _session_from_row(row) if row is not None else None

    def get_open_session_for_box(self, cash_box_id: str, tenant_id: str) -> CountSession | None:
        stmt = (
            select(CountSessionModel)
            .where(
                CountSessionModel.cash_box_id == cash_box_id,
                CountSessionModel.tenant_id == tenant_id,
                CountSessionModel.status != CountSessionStatus.ARCHIVED.value,
            )
            .order_by(CountSessionModel.created_at.desc())
            .limit(1)
        )
        row = self._session.execute(stmt).scalar_one_or_none()
        return _session_from_row(row) if row is not None else None

    def save_session(self, session: CountSession) -> CountSession:
        row = CountSessionModel(
            id=session.id,
            tenant_id=session.tenant_id,
            company_id=session.company_id,
            engine_company_id=session.engine_company_id,
            cash_box_id=session.cash_box_id,
            cashier=session.cashier,
            supervisor=session.supervisor,
            status=session.status.value,
            book_balance_irr=session.book_balance_irr,
            counted_amount_irr=session.counted_amount_irr,
            denominations=_denominations_to_json(session.denominations),
            minutes_text=session.minutes_text,
            witnessed_by=session.witnessed_by,
            witnessed_at=session.witnessed_at,
            minutes_cashier_signed_by=session.minutes_cashier_signed_by,
            minutes_cashier_signed_at=session.minutes_cashier_signed_at,
            minutes_supervisor_signed_by=session.minutes_supervisor_signed_by,
            minutes_supervisor_signed_at=session.minutes_supervisor_signed_at,
            minutes_manager_signed_by=session.minutes_manager_signed_by,
            minutes_manager_signed_at=session.minutes_manager_signed_at,
            review_item_id=session.review_item_id,
            draft_journal_id=session.draft_journal_id,
            posted_journal_id=session.posted_journal_id,
            notes=session.notes,
            cheque_refs=list(session.cheque_refs),
            deposit_ref=session.deposit_ref,
            order_refs=list(session.order_refs),
            installment_refs=list(session.installment_refs),
            created_at=session.created_at,
            updated_at=session.updated_at or session.created_at,
        )
        self._session.add(row)
        self._session.flush()
        return _session_from_row(row)

    def update_session(self, session: CountSession) -> CountSession:
        stmt = select(CountSessionModel).where(
            CountSessionModel.id == session.id,
            CountSessionModel.tenant_id == session.tenant_id,
        )
        row = self._session.execute(stmt).scalar_one_or_none()
        if row is None:
            raise ValueError(f"Counting session {session.id!r} does not exist.")
        row.status = session.status.value
        row.counted_amount_irr = session.counted_amount_irr
        row.denominations = _denominations_to_json(session.denominations)
        row.minutes_text = session.minutes_text
        row.witnessed_by = session.witnessed_by
        row.witnessed_at = session.witnessed_at
        row.minutes_cashier_signed_by = session.minutes_cashier_signed_by
        row.minutes_cashier_signed_at = session.minutes_cashier_signed_at
        row.minutes_supervisor_signed_by = session.minutes_supervisor_signed_by
        row.minutes_supervisor_signed_at = session.minutes_supervisor_signed_at
        row.minutes_manager_signed_by = session.minutes_manager_signed_by
        row.minutes_manager_signed_at = session.minutes_manager_signed_at
        row.review_item_id = session.review_item_id
        row.draft_journal_id = session.draft_journal_id
        row.posted_journal_id = session.posted_journal_id
        row.notes = session.notes
        row.cheque_refs = list(session.cheque_refs)
        row.deposit_ref = session.deposit_ref
        row.order_refs = list(session.order_refs)
        row.installment_refs = list(session.installment_refs)
        row.updated_at = session.updated_at or datetime.datetime.now(datetime.UTC)
        self._session.flush()
        return _session_from_row(row)

    # -- settings ------------------------------------------------------------

    def get_settings(self, tenant_id: str) -> CashBoxSettings | None:
        stmt = select(CashBoxSettingsModel).where(CashBoxSettingsModel.tenant_id == tenant_id)
        row = self._session.execute(stmt).scalar_one_or_none()
        return _settings_from_row(row) if row is not None else None

    def upsert_settings(self, settings: CashBoxSettings) -> CashBoxSettings:
        stmt = select(CashBoxSettingsModel).where(
            CashBoxSettingsModel.tenant_id == settings.tenant_id
        )
        row = self._session.execute(stmt).scalar_one_or_none()
        now = settings.updated_at or datetime.datetime.now(datetime.UTC)
        if row is None:
            row = CashBoxSettingsModel(
                tenant_id=settings.tenant_id,
                variance_policy=settings.variance_policy.value,
                material_variance_threshold_irr=settings.material_variance_threshold_irr,
                count_reminder_days=settings.count_reminder_days,
                count_schedule=settings.count_schedule,
                supervisor_required=settings.supervisor_required,
                shortage_settlement_hours=settings.shortage_settlement_hours,
                updated_at=now,
            )
            self._session.add(row)
        else:
            row.variance_policy = settings.variance_policy.value
            row.material_variance_threshold_irr = settings.material_variance_threshold_irr
            row.count_reminder_days = settings.count_reminder_days
            row.count_schedule = settings.count_schedule
            row.supervisor_required = settings.supervisor_required
            row.shortage_settlement_hours = settings.shortage_settlement_hours
            row.updated_at = now
        self._session.flush()
        return _settings_from_row(row)
