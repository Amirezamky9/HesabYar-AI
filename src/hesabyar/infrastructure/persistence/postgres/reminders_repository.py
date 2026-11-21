"""SQLAlchemy reminder repository (US-564, Phase 5-7 ۵خ W3).

Implements :class:`hesabyar.domain.reminders.ports.ReminderRepositoryPort`
against ``reminder_preferences`` + ``reminder_log``. The repository
never commits — the caller (router / worker) owns the transaction.

Dedup: ``insert_log`` flushes immediately so a violation of the
sent-dedup partial unique index surfaces as
``ReminderAlreadySentError``, never a raw ``IntegrityError``.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from hesabyar.domain.reminders.errors import ReminderAlreadySentError
from hesabyar.domain.reminders.models import (
    SENT_REMINDER_LOG_STATUSES,
    ReminderChannel,
    ReminderLogEntry,
    ReminderLogStatus,
    ReminderPreferences,
    ReminderType,
)
from hesabyar.domain.reminders.ports import ReminderRepositoryPort
from hesabyar.infrastructure.persistence.postgres.models import (
    ReminderLogModel,
    ReminderPreferencesModel,
)


def _preferences_from_row(row: ReminderPreferencesModel) -> ReminderPreferences:
    return ReminderPreferences(
        installation_id=row.installation_id,
        tenant_id=row.tenant_id,
        enabled=row.enabled,
        cheque_due_soon_enabled=row.cheque_due_soon_enabled,
        installment_overdue_enabled=row.installment_overdue_enabled,
        cashbox_uncounted_enabled=row.cashbox_uncounted_enabled,
        enmad_expiry_enabled=row.enmad_expiry_enabled,
        channel_in_app=row.channel_in_app,
        channel_email=row.channel_email,
        channel_sms=row.channel_sms,
        cheque_lead_days=tuple(int(item) for item in (row.cheque_lead_days or [])),
        installment_overdue_days=tuple(
            int(item) for item in (row.installment_overdue_days or [])
        ),
        cashbox_uncounted_days=row.cashbox_uncounted_days,
        enmad_lead_days=tuple(int(item) for item in (row.enmad_lead_days or [])),
        quiet_hours_enabled=row.quiet_hours_enabled,
        quiet_hours_start=row.quiet_hours_start,
        quiet_hours_end=row.quiet_hours_end,
        enmad_expiry_date=row.enmad_expiry_date,
        notification_email=row.notification_email,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _log_from_row(row: ReminderLogModel) -> ReminderLogEntry:
    return ReminderLogEntry(
        id=row.id,
        tenant_id=row.tenant_id,
        installation_id=row.installation_id,
        reminder_type=ReminderType(row.reminder_type),
        subject_type=row.subject_type,
        subject_id=row.subject_id,
        lead_days=row.lead_days,
        channel=ReminderChannel(row.channel),
        status=ReminderLogStatus(row.status),
        idempotency_key=row.idempotency_key,
        message_text=row.message_text,
        recipient=row.recipient,
        provider_id=row.provider_id,
        provider_key=row.provider_key,
        provider_message_id=row.provider_message_id,
        provider_status_raw=row.provider_status_raw,
        cost_toman=Decimal(row.cost_toman) if row.cost_toman is not None else None,
        provider_cost_raw=row.provider_cost_raw,
        provider_cost_unit=row.provider_cost_unit,
        error_code=row.error_code,
        error_detail=row.error_detail,
        sent_at=row.sent_at,
        created_at=row.created_at,
    )


class SqlAlchemyReminderRepository(ReminderRepositoryPort):
    """SQLAlchemy implementation of the reminder repository port."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- preferences ---------------------------------------------------

    def get_preferences(
        self, *, installation_id: str
    ) -> ReminderPreferences | None:
        row = self._session.get(ReminderPreferencesModel, installation_id)
        return _preferences_from_row(row) if row is not None else None

    def save_preferences(
        self, preferences: ReminderPreferences
    ) -> ReminderPreferences:
        row = self._session.get(
            ReminderPreferencesModel, preferences.installation_id
        )
        if row is None:
            row = ReminderPreferencesModel(
                installation_id=preferences.installation_id,
                tenant_id=preferences.tenant_id,
            )
            self._session.add(row)
        row.tenant_id = preferences.tenant_id
        row.enabled = preferences.enabled
        row.cheque_due_soon_enabled = preferences.cheque_due_soon_enabled
        row.installment_overdue_enabled = preferences.installment_overdue_enabled
        row.cashbox_uncounted_enabled = preferences.cashbox_uncounted_enabled
        row.enmad_expiry_enabled = preferences.enmad_expiry_enabled
        row.channel_in_app = preferences.channel_in_app
        row.channel_email = preferences.channel_email
        row.channel_sms = preferences.channel_sms
        row.cheque_lead_days = list(preferences.cheque_lead_days)
        row.installment_overdue_days = list(preferences.installment_overdue_days)
        row.cashbox_uncounted_days = preferences.cashbox_uncounted_days
        row.enmad_lead_days = list(preferences.enmad_lead_days)
        row.quiet_hours_enabled = preferences.quiet_hours_enabled
        row.quiet_hours_start = preferences.quiet_hours_start
        row.quiet_hours_end = preferences.quiet_hours_end
        row.enmad_expiry_date = preferences.enmad_expiry_date
        row.notification_email = preferences.notification_email
        self._session.flush()
        return _preferences_from_row(row)

    def list_preferences_enabled(self) -> list[ReminderPreferences]:
        rows = self._session.execute(
            select(ReminderPreferencesModel).where(
                ReminderPreferencesModel.enabled.is_(True)
            )
        ).scalars().all()
        return [_preferences_from_row(row) for row in rows]

    # -- reminder log ----------------------------------------------------

    def insert_log(self, entry: ReminderLogEntry) -> ReminderLogEntry:
        row = ReminderLogModel(
            id=entry.id,
            tenant_id=entry.tenant_id,
            installation_id=entry.installation_id,
            reminder_type=entry.reminder_type.value,
            subject_type=entry.subject_type,
            subject_id=entry.subject_id,
            lead_days=entry.lead_days,
            channel=entry.channel.value,
            status=entry.status.value,
            idempotency_key=entry.idempotency_key,
            message_text=entry.message_text,
            recipient=entry.recipient,
            provider_id=entry.provider_id,
            provider_key=entry.provider_key,
            provider_message_id=entry.provider_message_id,
            provider_status_raw=entry.provider_status_raw,
            cost_toman=str(entry.cost_toman) if entry.cost_toman is not None else None,
            provider_cost_raw=entry.provider_cost_raw,
            provider_cost_unit=entry.provider_cost_unit,
            error_code=entry.error_code,
            error_detail=entry.error_detail,
            sent_at=entry.sent_at,
        )
        self._session.add(row)
        try:
            self._session.flush()
        except IntegrityError as exc:
            # The only unique constraint an insert can violate here is
            # the sent-dedup index (PK collisions are UUID-random):
            # a sent/queued/delivered row already exists for this
            # idempotency key. Surface it as the typed dedup error.
            raise ReminderAlreadySentError(
                idempotency_key=entry.idempotency_key
            ) from exc
        return _log_from_row(row)

    def find_sent_log(
        self, *, installation_id: str, idempotency_key: str
    ) -> ReminderLogEntry | None:
        row = self._session.execute(
            select(ReminderLogModel).where(
                ReminderLogModel.installation_id == installation_id,
                ReminderLogModel.idempotency_key == idempotency_key,
                ReminderLogModel.status.in_(
                    [status.value for status in SENT_REMINDER_LOG_STATUSES]
                ),
            )
        ).scalar_one_or_none()
        return _log_from_row(row) if row is not None else None

    def list_log(
        self,
        *,
        installation_id: str,
        reminder_type: ReminderType | None = None,
        subject_id: str | None = None,
        channel: ReminderChannel | None = None,
        status: ReminderLogStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ReminderLogEntry]:
        stmt = (
            select(ReminderLogModel)
            .where(ReminderLogModel.installation_id == installation_id)
            .order_by(ReminderLogModel.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        if reminder_type is not None:
            stmt = stmt.where(
                ReminderLogModel.reminder_type == reminder_type.value
            )
        if subject_id is not None:
            stmt = stmt.where(ReminderLogModel.subject_id == subject_id)
        if channel is not None:
            stmt = stmt.where(ReminderLogModel.channel == channel.value)
        if status is not None:
            stmt = stmt.where(ReminderLogModel.status == status.value)
        rows = self._session.execute(stmt).scalars().all()
        return [_log_from_row(row) for row in rows]
