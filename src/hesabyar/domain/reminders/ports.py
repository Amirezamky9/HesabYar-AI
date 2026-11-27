"""Reminder ports (US-564, Phase 5-7 ۵خ W3).

The application service depends only on these protocols; the
SQLAlchemy implementation lives in
``hesabyar.infrastructure.persistence.postgres.reminders_repository``.

Two ports, split on purpose:

- :class:`ReminderRepositoryPort` owns the preferences (one row
  per installation) and the reminder log (the DLQ + audit trail).
- :class:`EmailSenderPort` is THE email interface for reminders.
  NO implementation exists yet — the F3 notification registry's
  email provider was never built (only SMS landed, US-544). The
  service takes an optional sender (``None`` = not configured)
  and logs email attempts as skipped until a provider exists;
  when one lands it implements this protocol and the channel
  starts working with no service change.

Tenant/installation scoping: preferences are installation-scoped
(like the SMS reminder policy); log rows carry both tenant and
installation ids. The repository never commits: the caller
(router / worker) owns the transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from hesabyar.domain.reminders.models import (
    ReminderChannel,
    ReminderLogEntry,
    ReminderLogStatus,
    ReminderPreferences,
    ReminderType,
)


@dataclass(frozen=True, slots=True)
class EmailSendResult:
    """The outcome of one email send, normalized across providers.

    ``provider_message_id`` is the provider's id for the message
    (None when the provider reports none). Email has no per-message
    cost in this model — the field is absent on purpose, not
    forgotten: no email provider exists yet to report one.
    """

    provider_message_id: str | None = None
    provider_status_raw: str | None = None


class EmailSenderPort(Protocol):
    """THE email interface for reminders (no implementation yet).

    A future email provider (F3 notification registry) implements
    this one method. Transport failures raise a plain ``Exception``
    (typed email errors arrive with the provider); the service
    logs them as failed attempts and continues.
    """

    def send(self, *, to: str, subject: str, body: str) -> EmailSendResult:
        """Send one message to one recipient."""
        ...


class ReminderRepositoryPort(Protocol):
    """HesabYar-side reminder preferences + log persistence."""

    # -- preferences ---------------------------------------------------

    def get_preferences(
        self, *, installation_id: str
    ) -> ReminderPreferences | None:
        """Return the installation's reminder policy, or None when never configured."""
        ...

    def save_preferences(
        self, preferences: ReminderPreferences
    ) -> ReminderPreferences:
        """Insert or replace the installation's reminder policy."""
        ...

    def list_preferences_enabled(self) -> list[ReminderPreferences]:
        """All installations with automatic reminders enabled (the daily job's scan)."""
        ...

    # -- reminder log ----------------------------------------------------

    def insert_log(self, entry: ReminderLogEntry) -> ReminderLogEntry:
        """Insert one log row.

        Raises ``ReminderAlreadySentError`` when the row would
        violate the sent-dedup index (a sent/queued/delivered row
        already exists for this idempotency key).
        """
        ...

    def find_sent_log(
        self, *, installation_id: str, idempotency_key: str
    ) -> ReminderLogEntry | None:
        """Return the sent/queued/delivered row for a key, if any (dedup check)."""
        ...

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
        """List log rows, newest first (all filters optional, AND-combined)."""
        ...
