"""Reminder preferences + log models (US-564, Phase 5-7 ۵خ W3).

Four reminder types, one daily job (``reminders.daily``), three
channels:

- ``cheque_due_soon`` — a commercial cheque (purpose ``payment``)
  in ``held`` / ``issued`` / ``called_in`` state whose due date is
  N days away (default leads 7 + 3). A cheque already at the bank
  (``in_clearing``) gets no due-soon reminder: the bank has the
  leaf, the due date is the bank's problem now. A collateral
  cheque (purpose ``collateral``) never reminds — it appears in
  no commercial surface (§2.2).
- ``installment_overdue`` — an internal plan's leg that is unpaid
  N days past its due date (default offsets 1 + 7). BNPL plans
  never remind (US-543: the customer owes the gateway, not the
  seller), and a leg a cheque in flight covers is not reminded
  (US-542: the cheque may still clear).
- ``cashbox_uncounted`` — a cash box with no completed count for
  more than N days (default 7, user-configured). A box with a
  count in progress is never reminded — someone is counting it.
- ``enmad_expiry`` — the installation's eNamad (نماد اعتماد)
  expires in N days (default leads 30 + 7 + 0). Expiry DISABLES
  gateway/BNPL service (phased design §8.3), so the reminder is
  a warning, not a suggestion — the message says so plainly in
  Persian (see :mod:`hesabyar.domain.reminders.messages`).

Channels: ``in_app`` (a review-queue item of type ``reminder`` —
the operator's in-app surface), ``email`` (the F3 notification
registry's email provider — NOT YET BUILT: no email sender exists
in the codebase, so email attempts are logged as skipped with
``EMAIL_NOT_CONFIGURED`` until a provider lands; see
``docs/reminders.md``), and ``sms`` (the US-544 multi-provider
registry — Kavenegar + sms.ir + simulator; provider specifics
user-entered, never hard-coded).

Idempotency: one reminder per event per lead-time per channel.
The key is ``{type}:{subject_id}:{event_ref}:{channel}`` where
``event_ref`` pins the event's identity — the due/expiry date for
date-offset types (a changed due date is a NEW event), the last
count date for a cash box (a new count starts a new uncounted
period). A ``sent`` / ``queued`` / ``delivered`` row for a key
blocks every later send for that key (backstopped by a partial
unique index); ``failed`` / ``skipped`` rows never block, so a
persistent trigger (cash box, eNamad) retries on the next run
while a missed date-offset day is simply missed (the US-544
rule: a late catch-up send would lie about the date).

Quiet hours: no SMS or email is sent while Tehran wall time is
inside the installation's quiet window (default 22:00–08:00);
the attempt is logged as ``skipped`` (``QUIET_HOURS``) and the
reminder is NOT consumed. In-app notifications may still be
recorded during quiet hours — they make no noise.

Money: SMS costs are INFORMATIONAL Toman values, exactly like
US-544 — ``cost_toman`` is only set when the provider reports a
cost in a KNOWN unit (Kavenegar reports Rial); otherwise the
provider's verbatim figure stays in ``provider_cost_raw`` with
an unverified unit. The Toman→Rial ×10 conversion does NOT apply
to them. Reminder amounts in messages are Toman display values
(IRR // 10 at the boundary only).
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum, unique

__all__ = [
    "DEFAULT_CASHBOX_UNCOUNTED_DAYS",
    "DEFAULT_CHEQUE_LEAD_DAYS",
    "DEFAULT_ENMAD_LEAD_DAYS",
    "DEFAULT_INSTALLMENT_OVERDUE_DAYS",
    "DEFAULT_QUIET_HOURS_END",
    "DEFAULT_QUIET_HOURS_START",
    "SENT_REMINDER_LOG_STATUSES",
    "ReminderChannel",
    "ReminderLogEntry",
    "ReminderLogStatus",
    "ReminderPreferences",
    "ReminderType",
    "build_idempotency_key",
    "is_quiet_hour",
    "new_log_id",
]

#: Default cheque due-soon leads (days before the due date): 7 + 3.
DEFAULT_CHEQUE_LEAD_DAYS: tuple[int, ...] = (7, 3)

#: Default installment-overdue offsets (days past the due date):
#: 1 day overdue + 7 days overdue. Offset 1 is also US-544's
#: default overdue offset — the shared dedup (the sms log check)
#: keeps the customer from getting the same SMS twice; US-564
#: still adds the in-app + email channels at that offset.
DEFAULT_INSTALLMENT_OVERDUE_DAYS: tuple[int, ...] = (1, 7)

#: Default uncounted-cashbox threshold (days since the last
#: completed count). Mirrors CashBoxSettings' default (US-516).
DEFAULT_CASHBOX_UNCOUNTED_DAYS = 7

#: Default eNamad-expiry leads (days before expiry): 30 + 7 + the
#: expiry day itself (0 — the last warning before the service is
#: disabled).
DEFAULT_ENMAD_LEAD_DAYS: tuple[int, ...] = (30, 7, 0)

#: Default quiet hours (Tehran wall time): 22:00–08:00.
DEFAULT_QUIET_HOURS_START = datetime.time(22, 0)
DEFAULT_QUIET_HOURS_END = datetime.time(8, 0)


@unique
class ReminderType(StrEnum):
    """The four reminder types (US-564 acceptance criterion 1)."""

    CHEQUE_DUE_SOON = "cheque_due_soon"
    INSTALLMENT_OVERDUE = "installment_overdue"
    CASHBOX_UNCOUNTED = "cashbox_uncounted"
    ENMAD_EXPIRY = "enmad_expiry"


@unique
class ReminderChannel(StrEnum):
    """Where a reminder goes: in-app, email, or SMS."""

    IN_APP = "in_app"
    EMAIL = "email"
    SMS = "sms"


@unique
class ReminderLogStatus(StrEnum):
    """Outcome of one reminder attempt, as recorded in the log.

    ``queued`` / ``sent`` / ``delivered`` are the dedup set: a key
    with one of these rows is never reminded again for that key
    (backstopped by a partial unique index). ``failed`` rows are
    retryable (the next run re-attempts a still-matching trigger);
    ``skipped`` rows record a blocked attempt (quiet hours, no
    consent, no phone, no provider, email not configured) so
    nothing is ever silent.
    """

    QUEUED = "queued"
    SENT = "sent"
    DELIVERED = "delivered"
    FAILED = "failed"
    SKIPPED = "skipped"


#: Log statuses that count as "the reminder went out" for dedup.
SENT_REMINDER_LOG_STATUSES: frozenset[ReminderLogStatus] = frozenset(
    {ReminderLogStatus.QUEUED, ReminderLogStatus.SENT, ReminderLogStatus.DELIVERED}
)


@dataclass(frozen=True)
class ReminderPreferences:
    """One installation's reminder policy (one row per installation).

    ``enabled`` is the master opt-in for AUTOMATIC reminders: the
    daily job sends nothing for an installation whose preferences
    are disabled (or missing — no row means "never configured",
    which is also disabled). The per-type flags switch individual
    reminder types; the channel flags switch channels GLOBALLY
    (per-type channels are a future refinement — the channel
    matrix in ``docs/reminders.md`` records which types can use
    which channels: cashbox + eNamad have no external recipient,
    so SMS never fires for them). Lead times are per type;
    ``cashbox_uncounted_days`` is the single N for the cash-box
    threshold. ``enmad_expiry_date`` is the store's eNamad expiry
    (phased design: settings → store profile; no store-profile
    model exists yet, so the date lives here until one does — no
    date means no eNamad reminders). ``notification_email`` is the
    operator address email reminders go to once an email provider
    exists (no sender is wired yet — see the module docstring).
    """

    installation_id: str
    tenant_id: str
    enabled: bool = False
    cheque_due_soon_enabled: bool = True
    installment_overdue_enabled: bool = True
    cashbox_uncounted_enabled: bool = True
    enmad_expiry_enabled: bool = True
    channel_in_app: bool = True
    channel_email: bool = True
    channel_sms: bool = True
    cheque_lead_days: tuple[int, ...] = DEFAULT_CHEQUE_LEAD_DAYS
    installment_overdue_days: tuple[int, ...] = DEFAULT_INSTALLMENT_OVERDUE_DAYS
    cashbox_uncounted_days: int = DEFAULT_CASHBOX_UNCOUNTED_DAYS
    enmad_lead_days: tuple[int, ...] = DEFAULT_ENMAD_LEAD_DAYS
    quiet_hours_enabled: bool = True
    quiet_hours_start: datetime.time = DEFAULT_QUIET_HOURS_START
    quiet_hours_end: datetime.time = DEFAULT_QUIET_HOURS_END
    enmad_expiry_date: datetime.date | None = None
    notification_email: str | None = None
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None

    @classmethod
    def defaults(cls, *, installation_id: str, tenant_id: str) -> ReminderPreferences:
        """The never-configured policy: disabled, default leads/channels."""
        return cls(installation_id=installation_id, tenant_id=tenant_id)

    def enabled_channels(self) -> tuple[ReminderChannel, ...]:
        """The channels this policy turns on, in dispatch order."""
        channels: list[ReminderChannel] = []
        if self.channel_in_app:
            channels.append(ReminderChannel.IN_APP)
        if self.channel_email:
            channels.append(ReminderChannel.EMAIL)
        if self.channel_sms:
            channels.append(ReminderChannel.SMS)
        return tuple(channels)

    def type_enabled(self, reminder_type: ReminderType) -> bool:
        """Whether this reminder type is on (master switch NOT included)."""
        return {
            ReminderType.CHEQUE_DUE_SOON: self.cheque_due_soon_enabled,
            ReminderType.INSTALLMENT_OVERDUE: self.installment_overdue_enabled,
            ReminderType.CASHBOX_UNCOUNTED: self.cashbox_uncounted_enabled,
            ReminderType.ENMAD_EXPIRY: self.enmad_expiry_enabled,
        }[reminder_type]


@dataclass(frozen=True)
class ReminderLogEntry:
    """One reminder attempt in the log (the DLQ + the audit trail).

    Every attempt is logged — sent, failed, or skipped — with its
    channel, cost, and reason, so a missing reminder is always
    explainable. ``subject_type`` names what the reminder is about
    (``cheque`` / ``installment_leg`` / ``cashbox`` /
    ``installation``); ``subject_id`` is its id. ``lead_days`` is
    the lead/offset that fired (days before due for cheques and
    eNamad, days overdue for installments, the N threshold for a
    cash box). ``idempotency_key`` is the dedup key (see
    :func:`build_idempotency_key`). ``recipient`` is the address
    the reminder went to (mobile for SMS, email for email, None
    for in-app — the operator's review queue has no address).
    ``cost_toman`` is informational and only set when the
    provider's reported cost has a KNOWN unit (the US-544 rule).
    """

    id: str
    tenant_id: str
    installation_id: str
    reminder_type: ReminderType
    subject_type: str
    subject_id: str
    lead_days: int
    channel: ReminderChannel
    status: ReminderLogStatus
    idempotency_key: str
    message_text: str
    recipient: str | None = None
    provider_id: str | None = None
    provider_key: str | None = None
    provider_message_id: str | None = None
    provider_status_raw: str | None = None
    cost_toman: Decimal | None = None
    provider_cost_raw: str | None = None
    provider_cost_unit: str | None = None
    error_code: str | None = None
    error_detail: str | None = None
    sent_at: datetime.datetime | None = None
    created_at: datetime.datetime | None = None


def build_idempotency_key(
    *,
    reminder_type: ReminderType,
    subject_id: str,
    event_ref: str,
    channel: ReminderChannel,
) -> str:
    """Build the dedup key for one (reminder, lead, channel).

    ``event_ref`` pins the event's identity: the due/expiry date
    (ISO) for date-offset types, the last-count date (ISO) for a
    cash box. The key is per channel on purpose: an SMS failure
    must not block the in-app reminder, and a sent in-app item
    must not swallow a retryable SMS.
    """
    return f"{reminder_type.value}:{subject_id}:{event_ref}:{channel.value}"


def is_quiet_hour(
    moment: datetime.datetime,
    *,
    start: datetime.time,
    end: datetime.time,
) -> bool:
    """True when Tehran wall time ``moment`` falls inside quiet hours.

    The window may cross midnight (22:00–08:00): when
    ``start > end`` the window is ``time >= start or time < end``.
    When ``start == end`` the window is empty (quiet hours off in
    effect — the payload validator also rejects equal bounds).
    Naive datetimes are treated as Tehran wall time (the caller
    passes ``tehran_now()`` or a Tehran-aware value).
    """
    current = moment.time()
    if start == end:
        return False
    if start < end:
        return start <= current < end
    return current >= start or current < end


def new_log_id() -> str:
    return str(uuid.uuid4())
