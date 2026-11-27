"""SMS provider registry + reminder models (US-544, Phase 5-5 ۵ج).

ADR 0003 amendment (2026-11-18): SMS notifications are
multi-provider and pluggable — one registry, one interface, first
built-ins Kavenegar + sms.ir, additional providers addable per
installation. Per-provider variable facts (price per SMS, rate
limits, service vs dedicated line, contract/eNamad requirements)
are ENTERED MANUALLY by the user per provider and NEVER hard-coded
(the same principle as gateway fees); the registry provides
editable fields for them. What is code is only each provider's API
integration shape (see ``hesabyar.infrastructure.sms``).

Money: SMS prices are INFORMATIONAL display values, user-entered
in Toman (``price_per_sms_toman``) — the Toman→Rial ×10 conversion
does NOT apply to them (US-544 acceptance criterion 4). The
reminder log's ``cost_toman`` is likewise informational: it is
only set when the provider reports a cost in a KNOWN unit
(Kavenegar reports Rial); otherwise the provider's verbatim cost
stays in ``provider_cost_raw`` with an unverified unit — never a
guessed conversion.

Dates: reminder offsets are computed from Jalali due dates, but
the arithmetic is Gregorian day arithmetic on the stored date
(the same day-count rule as the installment schedule: no
Jalali-month arithmetic anywhere).
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum, unique

__all__ = [
    "BUILTIN_PROVIDER_KEYS",
    "DEFAULT_OFFSETS_DAYS_BEFORE",
    "DEFAULT_OFFSETS_DAYS_OVERDUE",
    "DEFAULT_TEMPLATE_BEFORE_DUE",
    "DEFAULT_TEMPLATE_OVERDUE",
    "PROVIDER_KIND_CUSTOM",
    "PROVIDER_KIND_KAVENEGAR",
    "PROVIDER_KIND_SIMULATOR",
    "PROVIDER_KIND_SMS_IR",
    "PROVIDER_KINDS",
    "SmsContractReminderSettings",
    "SmsDeliveryStatus",
    "SmsLogStatus",
    "SmsProvider",
    "SmsProviderStatus",
    "SmsReminderLogEntry",
    "SmsReminderSettings",
    "SmsReminderTrigger",
    "new_contract_settings_id",
    "new_log_id",
    "new_provider_id",
    "new_settings_id",
]

#: Provider kinds with a client implementation (``custom`` has a
#: registry row but no client — sending through it is refused until
#: a client is built for its API shape).
PROVIDER_KIND_KAVENEGAR = "kavenegar"
PROVIDER_KIND_SMS_IR = "sms_ir"
PROVIDER_KIND_SIMULATOR = "simulator"
PROVIDER_KIND_CUSTOM = "custom"

#: All provider kinds the registry accepts.
PROVIDER_KINDS: frozenset[str] = frozenset(
    {
        PROVIDER_KIND_KAVENEGAR,
        PROVIDER_KIND_SMS_IR,
        PROVIDER_KIND_SIMULATOR,
        PROVIDER_KIND_CUSTOM,
    }
)

#: Built-in provider keys (seed data — see
#: ``hesabyar.infrastructure.persistence.postgres.seed_sms_providers``).
#: For built-ins the key equals the kind; custom providers carry a
#: user-chosen key with kind ``custom``.
BUILTIN_PROVIDER_KEYS: frozenset[str] = frozenset(
    {
        PROVIDER_KIND_KAVENEGAR,
        PROVIDER_KIND_SMS_IR,
        PROVIDER_KIND_SIMULATOR,
    }
)

#: Default reminder offsets (days before the due date): 3 days
#: before + on the day. User-editable per installation.
DEFAULT_OFFSETS_DAYS_BEFORE: tuple[int, ...] = (3, 0)

#: Default reminder offsets (days after the due date): 1 day overdue.
DEFAULT_OFFSETS_DAYS_OVERDUE: tuple[int, ...] = (1,)

#: Default message template for a not-yet-overdue leg. The template
#: is user-editable per installation — this is a starting value,
#: not marketing copy. Placeholders: see
#: ``hesabyar.domain.sms.templates.TEMPLATE_PLACEHOLDERS``.
DEFAULT_TEMPLATE_BEFORE_DUE = (
    "یادآوری سررسید قسط: مبلغ {amount_toman} تومان، سررسید {due_date_jalali} "
    "({days_remaining} روز مانده). فاکتور {invoice_id}."
)

#: Default message template for an overdue leg.
DEFAULT_TEMPLATE_OVERDUE = (
    "قسط شما به مبلغ {amount_toman} تومان با سررسید {due_date_jalali}، "
    "{days_overdue} روز معوق است. فاکتور {invoice_id}. لطفاً پرداخت فرمایید."
)


@unique
class SmsProviderStatus(StrEnum):
    """Operational status of an SMS provider (mirrors the channel registry)."""

    SETUP = "setup"
    ACTIVE = "active"
    DISABLED = "disabled"
    ERROR = "error"


@unique
class SmsDeliveryStatus(StrEnum):
    """Provider-reported delivery state of one sent message.

    ``UNKNOWN`` is the honest value when the provider's own status
    vocabulary is unverified (sms.ir ``deliveryState``) or the
    provider returned a code with no known mapping — the raw value
    is always preserved alongside it.
    """

    QUEUED = "queued"
    SENT = "sent"
    DELIVERED = "delivered"
    FAILED = "failed"
    UNKNOWN = "unknown"


@unique
class SmsReminderTrigger(StrEnum):
    """What caused a reminder send."""

    #: The daily Procrastinate job (``sms.daily_reminders``).
    SCHEDULED = "scheduled"
    #: An operator's explicit ``POST /installments/plans/{id}/remind``.
    MANUAL = "manual"


@unique
class SmsLogStatus(StrEnum):
    """Outcome of one reminder attempt, as recorded in the log.

    ``queued`` / ``sent`` / ``delivered`` are the dedup set: a leg +
    offset with one of these rows is never reminded again for that
    offset (backstopped by a partial unique index). ``failed`` rows
    are retryable (manual trigger = the replay); ``skipped`` rows
    record a blocked attempt (no phone, not opted in, provider
    unavailable, pending clearance) so nothing is ever silent;
    ``dry_run`` rows record a previewed message (no send happened).
    """

    QUEUED = "queued"
    SENT = "sent"
    DELIVERED = "delivered"
    FAILED = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"


#: Log statuses that count as "the reminder went out" for dedup.
SENT_LOG_STATUSES: frozenset[SmsLogStatus] = frozenset(
    {SmsLogStatus.QUEUED, SmsLogStatus.SENT, SmsLogStatus.DELIVERED}
)


@dataclass(frozen=True)
class SmsProvider:
    """One SMS provider in an installation's registry.

    ``provider_key`` is the stable slug (data, never an enum — the
    same rule as payment channels). ``provider_kind`` selects the
    client implementation (``kavenegar`` / ``sms_ir`` /
    ``simulator`` / ``custom``); for built-ins it equals the key.
    ``is_builtin`` marks the rows the seed creates. A live built-in
    is born disabled (``enabled=False``, ``status=SETUP``) until the
    operator enables it and attaches credentials — fail-closed: a
    seeded provider can never send before it is configured. The
    simulator is the exception: it is born enabled AND default, so
    reminders work (as logged simulations) with zero configuration.

    Price / rate limit / line type / contract / eNamad are all
    USER-ENTERED (ADR 0003 amendment): they describe the
    installation's own deal with the provider and are never
    hard-coded. ``has_credentials`` is the only credential signal
    this read model carries — no credential material ever reaches a
    REST response or a log.
    """

    id: str
    installation_id: str
    provider_key: str
    display_name_fa: str
    provider_kind: str
    is_builtin: bool
    enabled: bool
    is_default: bool
    status: SmsProviderStatus
    line_number: str | None = None
    #: Informational price per SMS in Toman, user-entered. None =
    #: not entered. NEVER converted ×10 (US-544 criterion 4).
    price_per_sms_toman: Decimal | None = None
    #: User-entered rate limit (messages per minute), if the
    #: provider documents one. None = unknown / not entered.
    rate_limit_per_minute: int | None = None
    #: Free string with conventional values (``service`` /
    #: ``dedicated``); deliberately NOT an enum, so a new line kind
    #: needs no code change.
    line_type: str | None = None
    #: Whether the provider requires a contract with the operator.
    contract_required: bool = False
    #: Whether the provider requires an eNamad (نماد اعتماد) for the store.
    enmad_required: bool = False
    notes: str | None = None
    has_credentials: bool = False
    credentials_key_id: str | None = None
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


@dataclass(frozen=True)
class SmsReminderSettings:
    """One installation's reminder policy (one row per installation).

    ``enabled`` is the master opt-in for AUTOMATIC sends: the daily
    job sends nothing for an installation whose settings are
    disabled (or missing — no row means "never configured", which
    is also disabled). The offsets say which days fire: ``3`` in
    ``offsets_days_before`` fires 3 days before the due date,
    ``0`` fires on the day, ``1`` in ``offsets_days_overdue``
    fires 1 day after it. The templates are user-editable Persian
    text with ``{placeholders}`` (see
    ``hesabyar.domain.sms.templates``). The default provider lives
    on the provider row (``is_default``), not here — one source of
    truth for "which provider sends".
    """

    installation_id: str
    tenant_id: str
    enabled: bool = False
    offsets_days_before: tuple[int, ...] = DEFAULT_OFFSETS_DAYS_BEFORE
    offsets_days_overdue: tuple[int, ...] = DEFAULT_OFFSETS_DAYS_OVERDUE
    template_before_due: str = DEFAULT_TEMPLATE_BEFORE_DUE
    template_overdue: str = DEFAULT_TEMPLATE_OVERDUE
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None

    @classmethod
    def defaults(cls, *, installation_id: str, tenant_id: str) -> SmsReminderSettings:
        """The never-configured policy: disabled, default offsets/templates."""
        return cls(installation_id=installation_id, tenant_id=tenant_id)


@dataclass(frozen=True)
class SmsContractReminderSettings:
    """The per-contract opt-in for automatic reminders.

    Reminders send automatically only when the provider is enabled
    AND the plan's contract opts in (US-544 standing rule). A plan
    without a contract — or whose contract has no row here, or a
    row with ``reminders_enabled=False`` — receives NO automatic
    reminders; the manual trigger (an explicit operator action)
    still works. Born disabled: opt-in is explicit, never implied.
    """

    id: str
    installation_id: str
    contract_id: str
    reminders_enabled: bool = False
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


@dataclass(frozen=True)
class SmsReminderLogEntry:
    """One reminder attempt in the log (the DLQ + the audit trail).

    Every attempt is logged — sent, failed, skipped, or dry-run —
    with its provider, cost, and delivery status, so a missing
    reminder is always explainable. ``offset_days`` is the
    trigger's distance from the due date in days: negative = days
    before (``-3`` = 3 days before), ``0`` = on the day, positive
    = days overdue. ``provider_key`` is denormalized (the provider
    row may later be deleted; the log keeps the key). ``cost_toman``
    is informational and only set when the provider's reported cost
    has a KNOWN unit; ``provider_cost_raw`` carries the provider's
    verbatim figure (and ``provider_cost_unit`` its unit, or
    ``"unverified"`` when the provider never documents it).
    """

    id: str
    tenant_id: str
    installation_id: str
    plan_id: str
    leg_id: str
    provider_id: str | None
    provider_key: str
    recipient_phone: str
    message_text: str
    offset_days: int
    trigger: SmsReminderTrigger
    status: SmsLogStatus
    provider_message_id: str | None = None
    provider_status_raw: str | None = None
    cost_toman: Decimal | None = None
    provider_cost_raw: str | None = None
    provider_cost_unit: str | None = None
    error_code: str | None = None
    error_detail: str | None = None
    sent_at: datetime.datetime | None = None
    created_at: datetime.datetime | None = None


def new_provider_id() -> str:
    return str(uuid.uuid4())


def new_settings_id() -> str:
    return str(uuid.uuid4())


def new_contract_settings_id() -> str:
    return str(uuid.uuid4())


def new_log_id() -> str:
    return str(uuid.uuid4())
