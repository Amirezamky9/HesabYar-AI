"""SQLAlchemy recurring repository (US-563, Phase 5-7 ۵خ W3).

Implements :class:`RecurringRepositoryPort` over
``recurring_templates`` / ``recurring_occurrences``. The repository
never commits: the caller (router / worker / recorder) owns the
transaction. Template reads are always tenant-scoped; the
generator's ``list_active_templates`` is the one cross-tenant read.

Idempotency: ``save_occurrence`` translates a unique-violation on
(template, scheduled date) — or (template, occurrence number) —
into :class:`RecurringOccurrenceConflictError`, so the generator
can tell "another run claimed it" from a real database failure.
The claim flush runs inside a SAVEPOINT (the event pipeline's
pattern), so a conflict rolls back only the claim.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from hesabyar.domain.recurring.errors import RecurringOccurrenceConflictError
from hesabyar.domain.recurring.models import (
    OccurrenceStatus,
    RecurringOccurrence,
    RecurringTemplate,
    TemplateKind,
    TemplateStatus,
    payload_from_dict,
)
from hesabyar.domain.recurring.ports import RecurringRepositoryPort
from hesabyar.domain.recurring.schedules import RecurringSchedule
from hesabyar.infrastructure.persistence.postgres.models import (
    RecurringOccurrenceModel,
    RecurringTemplateModel,
)


def _template_from_row(row: RecurringTemplateModel) -> RecurringTemplate:
    kind = TemplateKind(row.kind)
    return RecurringTemplate(
        id=row.id,
        tenant_id=row.tenant_id,
        installation_id=row.installation_id,
        company_id=row.company_id,
        company_engine_id=row.company_engine_id,
        kind=kind,
        name=row.name,
        status=TemplateStatus(row.status),
        schedule=RecurringSchedule.from_dict(dict(row.schedule)),
        payload=payload_from_dict(kind, dict(row.payload)),
        auto_post=row.auto_post,
        total_amount_irr=row.total_amount_irr,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _occurrence_from_row(row: RecurringOccurrenceModel) -> RecurringOccurrence:
    return RecurringOccurrence(
        id=row.id,
        template_id=row.template_id,
        occurrence_number=row.occurrence_number,
        scheduled_date=row.scheduled_date,
        jalali_date=row.jalali_date,
        clamped=row.clamped,
        requested_jalali_day=row.requested_jalali_day,
        actual_jalali_day=row.actual_jalali_day,
        status=OccurrenceStatus(row.status),
        total_amount_irr=row.total_amount_irr,
        snapshot=dict(row.snapshot or {}),
        engine_document_id=row.engine_document_id,
        gate_decision=dict(row.gate_decision) if row.gate_decision is not None else None,
        error=row.error,
        generated_at=row.generated_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlAlchemyRecurringRepository(RecurringRepositoryPort):
    """Recurring templates + occurrences over SQLAlchemy (flushes, never commits)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- templates -------------------------------------------------------------

    def save_template(self, template: RecurringTemplate) -> RecurringTemplate:
        """Insert or update a template by id (flushes; the caller commits)."""
        row = self._session.get(RecurringTemplateModel, template.id)
        if row is None:
            row = RecurringTemplateModel(
                id=template.id,
                tenant_id=template.tenant_id,
                installation_id=template.installation_id,
                company_id=template.company_id,
                company_engine_id=template.company_engine_id,
                kind=template.kind.value,
                name=template.name,
                status=template.status.value,
                schedule=template.schedule.to_dict(),
                payload=template.payload.to_dict(),
                auto_post=template.auto_post,
                total_amount_irr=template.total_amount_irr,
                created_at=template.created_at,
                updated_at=template.updated_at,
            )
            self._session.add(row)
        else:
            row.name = template.name
            row.status = template.status.value
            row.schedule = template.schedule.to_dict()
            row.payload = template.payload.to_dict()
            row.auto_post = template.auto_post
            row.total_amount_irr = template.total_amount_irr
            row.updated_at = template.updated_at
        self._session.flush()
        return template

    def get_template(
        self, tenant_id: str, template_id: str
    ) -> RecurringTemplate | None:
        """One template of this tenant by id, or None."""
        row = self._session.get(RecurringTemplateModel, template_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        return _template_from_row(row)

    def list_templates(
        self,
        tenant_id: str,
        *,
        company_id: str | None = None,
        kind: TemplateKind | None = None,
        status: TemplateStatus | None = None,
    ) -> Sequence[RecurringTemplate]:
        """A tenant's templates, newest first, optionally filtered."""
        stmt = select(RecurringTemplateModel).where(
            RecurringTemplateModel.tenant_id == tenant_id
        )
        if company_id is not None:
            stmt = stmt.where(RecurringTemplateModel.company_id == company_id)
        if kind is not None:
            stmt = stmt.where(RecurringTemplateModel.kind == kind.value)
        if status is not None:
            stmt = stmt.where(RecurringTemplateModel.status == status.value)
        stmt = stmt.order_by(
            RecurringTemplateModel.created_at.desc(), RecurringTemplateModel.id.desc()
        )
        rows = list(self._session.execute(stmt).scalars())
        return [_template_from_row(row) for row in rows]

    def list_active_templates(self) -> Sequence[RecurringTemplate]:
        """Every ACTIVE template across all tenants (the generator's scope)."""
        rows = list(
            self._session.execute(
                select(RecurringTemplateModel)
                .where(RecurringTemplateModel.status == TemplateStatus.ACTIVE.value)
                .order_by(
                    RecurringTemplateModel.created_at, RecurringTemplateModel.id
                )
            ).scalars()
        )
        return [_template_from_row(row) for row in rows]

    def delete_template(self, tenant_id: str, template_id: str) -> bool:
        """Delete a template with no occurrences; False when it does not exist."""
        row = self._session.get(RecurringTemplateModel, template_id)
        if row is None or row.tenant_id != tenant_id:
            return False
        self._session.delete(row)
        self._session.flush()
        return True

    def count_occurrences(self, template_id: str) -> int:
        """How many occurrence rows a template has (any status)."""
        return int(
            self._session.execute(
                select(func.count())
                .select_from(RecurringOccurrenceModel)
                .where(RecurringOccurrenceModel.template_id == template_id)
            ).scalar_one()
        )

    # -- occurrences -----------------------------------------------------------

    def save_occurrence(self, occurrence: RecurringOccurrence) -> RecurringOccurrence:
        """Insert or update an occurrence by id (flushes; the caller commits).

        A unique-violation on (template, scheduled date) or
        (template, occurrence number) becomes
        :class:`RecurringOccurrenceConflictError` — the generator's
        "another run claimed it" signal. The flush runs inside a
        SAVEPOINT (the event pipeline's pattern), so a conflict
        rolls back only this claim — never the run's earlier work.
        """
        row = self._session.get(RecurringOccurrenceModel, occurrence.id)
        if row is None:
            now = datetime.datetime.now(datetime.UTC)
            row = RecurringOccurrenceModel(
                id=occurrence.id,
                template_id=occurrence.template_id,
                occurrence_number=occurrence.occurrence_number,
                scheduled_date=occurrence.scheduled_date,
                jalali_date=occurrence.jalali_date,
                clamped=occurrence.clamped,
                requested_jalali_day=occurrence.requested_jalali_day,
                actual_jalali_day=occurrence.actual_jalali_day,
                status=occurrence.status.value,
                total_amount_irr=occurrence.total_amount_irr,
                snapshot=dict(occurrence.snapshot),
                engine_document_id=occurrence.engine_document_id,
                gate_decision=(
                    dict(occurrence.gate_decision)
                    if occurrence.gate_decision is not None
                    else None
                ),
                error=occurrence.error,
                generated_at=occurrence.generated_at,
                created_at=occurrence.created_at or now,
                updated_at=occurrence.updated_at or now,
            )
            self._session.add(row)
        else:
            row.status = occurrence.status.value
            row.total_amount_irr = occurrence.total_amount_irr
            row.snapshot = dict(occurrence.snapshot)
            row.engine_document_id = occurrence.engine_document_id
            row.gate_decision = (
                dict(occurrence.gate_decision)
                if occurrence.gate_decision is not None
                else None
            )
            row.error = occurrence.error
            row.generated_at = occurrence.generated_at
            if occurrence.updated_at is not None:
                row.updated_at = occurrence.updated_at
        try:
            with self._session.begin_nested():
                self._session.flush()
        except IntegrityError as exc:
            raise RecurringOccurrenceConflictError(
                f"Occurrence for template {occurrence.template_id!r} on "
                f"{occurrence.scheduled_date.isoformat()} already exists.",
                details={
                    "template_id": occurrence.template_id,
                    "scheduled_date": occurrence.scheduled_date.isoformat(),
                    "occurrence_number": occurrence.occurrence_number,
                },
            ) from exc
        return occurrence

    def get_occurrence_by_date(
        self, template_id: str, scheduled_date: datetime.date
    ) -> RecurringOccurrence | None:
        """A template's occurrence for one scheduled date, or None."""
        row = self._session.execute(
            select(RecurringOccurrenceModel).where(
                RecurringOccurrenceModel.template_id == template_id,
                RecurringOccurrenceModel.scheduled_date == scheduled_date,
            )
        ).scalar_one_or_none()
        return _occurrence_from_row(row) if row is not None else None

    def list_occurrences(
        self, template_id: str
    ) -> Sequence[RecurringOccurrence]:
        """A template's occurrences, in occurrence-number order."""
        rows = list(
            self._session.execute(
                select(RecurringOccurrenceModel)
                .where(RecurringOccurrenceModel.template_id == template_id)
                .order_by(
                    RecurringOccurrenceModel.occurrence_number,
                    RecurringOccurrenceModel.id,
                )
            ).scalars()
        )
        return [_occurrence_from_row(row) for row in rows]
