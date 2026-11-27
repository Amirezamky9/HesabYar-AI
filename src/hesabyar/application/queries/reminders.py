"""Reminder query service (US-564, Phase 5-7 ۵خ W3).

Reads for the reminder policy and the reminder log. Everything is
tenant/installation-scoped: a row of another installation answers
404 as if it did not exist.

The policy read returns the installation's DEFAULTS when no row
was ever saved (disabled, default leads/channels) — a
never-configured installation reads as "reminders off", never as
an error.
"""

from __future__ import annotations

from hesabyar.domain.reminders.models import (
    ReminderChannel,
    ReminderLogEntry,
    ReminderLogStatus,
    ReminderPreferences,
    ReminderType,
)
from hesabyar.domain.reminders.ports import ReminderRepositoryPort


class ReminderQueryService:
    """Reads for reminder preferences and the reminder log."""

    def __init__(self, repository: ReminderRepositoryPort) -> None:
        self._repository = repository

    # -- preferences ---------------------------------------------------

    def get_preferences(
        self, *, tenant_id: str, installation_id: str
    ) -> ReminderPreferences:
        """Return the installation's reminder policy (defaults when never saved)."""
        stored = self._repository.get_preferences(installation_id=installation_id)
        if stored is not None:
            return stored
        return ReminderPreferences.defaults(
            installation_id=installation_id, tenant_id=tenant_id
        )

    # -- reminder log ------------------------------------------------------

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
        """List log rows, newest first (all filters optional)."""
        return self._repository.list_log(
            installation_id=installation_id,
            reminder_type=reminder_type,
            subject_id=subject_id,
            channel=channel,
            status=status,
            limit=limit,
            offset=offset,
        )
