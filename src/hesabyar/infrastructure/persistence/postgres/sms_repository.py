"""SQLAlchemy SMS repository (US-544, Phase 5-5 ۵ج).

Implements :class:`SmsRepositoryPort` over the ``sms_providers`` /
``sms_reminder_settings`` / ``sms_contract_reminder_settings`` /
``sms_reminder_log`` tables. The repository never talks to a
provider API and never commits: the caller (router / worker) owns
the transaction.

Decimal prices are stored as exact decimal TEXT (the same
convention as the installment late-fee rate): no float ever
touches a price. Offsets are stored as JSONB arrays of ints.
The sent-dedup partial unique index
(``uq_sms_reminder_log_installation_leg_offset_sent``) is the
backstop for "one reminder per leg + offset" — an insert that
violates it surfaces as ``SmsReminderAlreadySentError``, never a
raw ``IntegrityError``.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from hesabyar.domain.sms.commands import (
    UNSET,
    CreateSmsProvider,
    RotateSmsProviderCredentials,
    UpdateSmsProvider,
)
from hesabyar.domain.sms.errors import SmsReminderAlreadySentError
from hesabyar.domain.sms.models import (
    SENT_LOG_STATUSES,
    SmsContractReminderSettings,
    SmsLogStatus,
    SmsProvider,
    SmsReminderLogEntry,
    SmsReminderSettings,
    SmsReminderTrigger,
)
from hesabyar.domain.sms.ports import SmsRepositoryPort
from hesabyar.infrastructure.persistence.postgres.models import (
    SmsContractReminderSettingsModel,
    SmsProviderModel,
    SmsReminderLogModel,
    SmsReminderSettingsModel,
)
from hesabyar.infrastructure.persistence.postgres.seed_sms_providers import (
    provider_from_row,
)
from hesabyar.infrastructure.persistence.postgres.seed_sms_providers import (
    seed_builtin_providers as _seed_builtin_providers,
)


def _settings_from_row(row: SmsReminderSettingsModel) -> SmsReminderSettings:
    return SmsReminderSettings(
        installation_id=row.installation_id,
        tenant_id=row.tenant_id,
        enabled=row.enabled,
        offsets_days_before=tuple(int(item) for item in (row.offsets_days_before or [])),
        offsets_days_overdue=tuple(int(item) for item in (row.offsets_days_overdue or [])),
        template_before_due=row.template_before_due,
        template_overdue=row.template_overdue,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _contract_settings_from_row(
    row: SmsContractReminderSettingsModel,
) -> SmsContractReminderSettings:
    return SmsContractReminderSettings(
        id=row.id,
        installation_id=row.installation_id,
        contract_id=row.contract_id,
        reminders_enabled=row.reminders_enabled,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _log_from_row(row: SmsReminderLogModel) -> SmsReminderLogEntry:
    return SmsReminderLogEntry(
        id=row.id,
        tenant_id=row.tenant_id,
        installation_id=row.installation_id,
        plan_id=row.plan_id,
        leg_id=row.leg_id,
        provider_id=row.provider_id,
        provider_key=row.provider_key,
        recipient_phone=row.recipient_phone,
        message_text=row.message_text,
        offset_days=row.offset_days,
        trigger=SmsReminderTrigger(row.trigger),
        status=SmsLogStatus(row.status),
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


class SqlAlchemySmsRepository(SmsRepositoryPort):
    """SQLAlchemy implementation of the SMS repository port."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- providers -----------------------------------------------------

    def create_provider(self, command: CreateSmsProvider) -> SmsProvider:
        row = SmsProviderModel(
            installation_id=command.installation_id,
            provider_key=command.provider_key,
            display_name_fa=command.display_name_fa,
            provider_kind=command.provider_kind,
            is_builtin=command.is_builtin,
            enabled=command.enabled,
            is_default=command.is_default,
            status=command.status.value,
            line_number=command.line_number,
            price_per_sms_toman=(
                str(command.price_per_sms_toman)
                if command.price_per_sms_toman is not None
                else None
            ),
            rate_limit_per_minute=command.rate_limit_per_minute,
            line_type=command.line_type,
            contract_required=command.contract_required,
            enmad_required=command.enmad_required,
            notes=command.notes,
        )
        self._session.add(row)
        self._session.flush()
        return provider_from_row(row)

    def get_provider(
        self, provider_id: str, *, installation_id: str
    ) -> SmsProvider | None:
        row = self._provider_row(provider_id, installation_id)
        return provider_from_row(row) if row is not None else None

    def get_provider_by_key(
        self, provider_key: str, *, installation_id: str
    ) -> SmsProvider | None:
        row = self._session.execute(
            select(SmsProviderModel).where(
                SmsProviderModel.installation_id == installation_id,
                SmsProviderModel.provider_key == provider_key,
            )
        ).scalar_one_or_none()
        return provider_from_row(row) if row is not None else None

    def get_default_provider(self, *, installation_id: str) -> SmsProvider | None:
        row = self._session.execute(
            select(SmsProviderModel).where(
                SmsProviderModel.installation_id == installation_id,
                SmsProviderModel.is_default.is_(True),
            )
        ).scalar_one_or_none()
        return provider_from_row(row) if row is not None else None

    def list_providers(self, *, installation_id: str) -> list[SmsProvider]:
        rows = self._session.execute(
            select(SmsProviderModel)
            .where(SmsProviderModel.installation_id == installation_id)
            .order_by(
                SmsProviderModel.is_builtin.desc(),
                SmsProviderModel.provider_key.asc(),
            )
        ).scalars().all()
        return [provider_from_row(row) for row in rows]

    def update_provider(self, command: UpdateSmsProvider) -> SmsProvider | None:
        row = self._provider_row(command.provider_id, command.installation_id)
        if row is None:
            return None
        if command.display_name_fa is not None:
            row.display_name_fa = command.display_name_fa
        if command.enabled is not None:
            row.enabled = command.enabled
        if command.is_default is not None:
            row.is_default = command.is_default
        if command.status is not None:
            row.status = command.status.value
        if command.line_number is not UNSET:
            row.line_number = command.line_number
        if command.price_per_sms_toman is not UNSET:
            row.price_per_sms_toman = (
                str(command.price_per_sms_toman)
                if command.price_per_sms_toman is not None
                else None
            )
        if command.rate_limit_per_minute is not UNSET:
            row.rate_limit_per_minute = command.rate_limit_per_minute
        if command.line_type is not UNSET:
            row.line_type = command.line_type
        if command.contract_required is not None:
            row.contract_required = command.contract_required
        if command.enmad_required is not None:
            row.enmad_required = command.enmad_required
        if command.notes is not UNSET:
            row.notes = command.notes
        self._session.flush()
        return provider_from_row(row)

    def delete_provider(self, provider_id: str, *, installation_id: str) -> bool:
        row = self._provider_row(provider_id, installation_id)
        if row is None:
            return False
        self._session.delete(row)
        self._session.flush()
        return True

    def rotate_provider_credentials(
        self, command: RotateSmsProviderCredentials
    ) -> SmsProvider | None:
        row = self._provider_row(command.provider_id, command.installation_id)
        if row is None:
            return None
        row.credentials_ciphertext = command.credentials_ciphertext
        row.credentials_key_id = command.credentials_key_id
        self._session.flush()
        return provider_from_row(row)

    def load_provider_credentials(
        self, provider_id: str, *, installation_id: str
    ) -> str | None:
        row = self._provider_row(provider_id, installation_id)
        return row.credentials_ciphertext if row is not None else None

    def seed_builtin_providers(self, installation_id: str) -> list[SmsProvider]:
        return _seed_builtin_providers(self._session, installation_id)

    def _provider_row(
        self, provider_id: str, installation_id: str
    ) -> SmsProviderModel | None:
        return self._session.execute(
            select(SmsProviderModel).where(
                SmsProviderModel.id == provider_id,
                SmsProviderModel.installation_id == installation_id,
            )
        ).scalar_one_or_none()

    # -- reminder settings ----------------------------------------------

    def get_reminder_settings(
        self, *, installation_id: str
    ) -> SmsReminderSettings | None:
        row = self._session.get(SmsReminderSettingsModel, installation_id)
        return _settings_from_row(row) if row is not None else None

    def save_reminder_settings(self, settings: SmsReminderSettings) -> SmsReminderSettings:
        row = self._session.get(SmsReminderSettingsModel, settings.installation_id)
        if row is None:
            row = SmsReminderSettingsModel(
                installation_id=settings.installation_id,
                tenant_id=settings.tenant_id,
                enabled=settings.enabled,
                offsets_days_before=list(settings.offsets_days_before),
                offsets_days_overdue=list(settings.offsets_days_overdue),
                template_before_due=settings.template_before_due,
                template_overdue=settings.template_overdue,
            )
            self._session.add(row)
        else:
            row.tenant_id = settings.tenant_id
            row.enabled = settings.enabled
            row.offsets_days_before = list(settings.offsets_days_before)
            row.offsets_days_overdue = list(settings.offsets_days_overdue)
            row.template_before_due = settings.template_before_due
            row.template_overdue = settings.template_overdue
        self._session.flush()
        return _settings_from_row(row)

    def list_reminder_settings_enabled(self) -> list[SmsReminderSettings]:
        rows = self._session.execute(
            select(SmsReminderSettingsModel).where(
                SmsReminderSettingsModel.enabled.is_(True)
            )
        ).scalars().all()
        return [_settings_from_row(row) for row in rows]

    # -- per-contract opt-ins --------------------------------------------

    def get_contract_settings(
        self, *, installation_id: str, contract_id: str
    ) -> SmsContractReminderSettings | None:
        row = self._session.execute(
            select(SmsContractReminderSettingsModel).where(
                SmsContractReminderSettingsModel.installation_id == installation_id,
                SmsContractReminderSettingsModel.contract_id == contract_id,
            )
        ).scalar_one_or_none()
        return _contract_settings_from_row(row) if row is not None else None

    def save_contract_settings(
        self, settings: SmsContractReminderSettings
    ) -> SmsContractReminderSettings:
        row = self._session.execute(
            select(SmsContractReminderSettingsModel).where(
                SmsContractReminderSettingsModel.installation_id == settings.installation_id,
                SmsContractReminderSettingsModel.contract_id == settings.contract_id,
            )
        ).scalar_one_or_none()
        if row is None:
            row = SmsContractReminderSettingsModel(
                id=settings.id,
                installation_id=settings.installation_id,
                contract_id=settings.contract_id,
                reminders_enabled=settings.reminders_enabled,
            )
            self._session.add(row)
        else:
            row.reminders_enabled = settings.reminders_enabled
        self._session.flush()
        return _contract_settings_from_row(row)

    def list_contract_settings(
        self, *, installation_id: str
    ) -> list[SmsContractReminderSettings]:
        rows = self._session.execute(
            select(SmsContractReminderSettingsModel).where(
                SmsContractReminderSettingsModel.installation_id == installation_id
            )
        ).scalars().all()
        return [_contract_settings_from_row(row) for row in rows]

    # -- reminder log ------------------------------------------------------

    def insert_log(self, entry: SmsReminderLogEntry) -> SmsReminderLogEntry:
        row = SmsReminderLogModel(
            id=entry.id,
            tenant_id=entry.tenant_id,
            installation_id=entry.installation_id,
            plan_id=entry.plan_id,
            leg_id=entry.leg_id,
            provider_id=entry.provider_id,
            provider_key=entry.provider_key,
            recipient_phone=entry.recipient_phone,
            message_text=entry.message_text,
            offset_days=entry.offset_days,
            trigger=entry.trigger.value,
            status=entry.status.value,
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
            # leg + offset. Surface it as the typed dedup error.
            raise SmsReminderAlreadySentError(
                leg_id=entry.leg_id, offset_days=entry.offset_days
            ) from exc
        return _log_from_row(row)

    def get_log_entry(
        self, log_id: str, *, installation_id: str
    ) -> SmsReminderLogEntry | None:
        row = self._session.execute(
            select(SmsReminderLogModel).where(
                SmsReminderLogModel.id == log_id,
                SmsReminderLogModel.installation_id == installation_id,
            )
        ).scalar_one_or_none()
        return _log_from_row(row) if row is not None else None

    def find_sent_log(
        self, *, installation_id: str, leg_id: str, offset_days: int
    ) -> SmsReminderLogEntry | None:
        row = self._session.execute(
            select(SmsReminderLogModel).where(
                SmsReminderLogModel.installation_id == installation_id,
                SmsReminderLogModel.leg_id == leg_id,
                SmsReminderLogModel.offset_days == offset_days,
                SmsReminderLogModel.status.in_([status.value for status in SENT_LOG_STATUSES]),
            )
        ).scalar_one_or_none()
        return _log_from_row(row) if row is not None else None

    def list_log(
        self,
        *,
        installation_id: str,
        plan_id: str | None = None,
        leg_id: str | None = None,
        provider_key: str | None = None,
        status: SmsLogStatus | None = None,
        trigger: SmsReminderTrigger | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SmsReminderLogEntry]:
        stmt = (
            select(SmsReminderLogModel)
            .where(SmsReminderLogModel.installation_id == installation_id)
            .order_by(SmsReminderLogModel.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        if plan_id is not None:
            stmt = stmt.where(SmsReminderLogModel.plan_id == plan_id)
        if leg_id is not None:
            stmt = stmt.where(SmsReminderLogModel.leg_id == leg_id)
        if provider_key is not None:
            stmt = stmt.where(SmsReminderLogModel.provider_key == provider_key)
        if status is not None:
            stmt = stmt.where(SmsReminderLogModel.status == status.value)
        if trigger is not None:
            stmt = stmt.where(SmsReminderLogModel.trigger == trigger.value)
        rows = self._session.execute(stmt).scalars().all()
        return [_log_from_row(row) for row in rows]
