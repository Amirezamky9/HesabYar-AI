"""Reminder command service (US-564, Phase 5-7 ۵خ W3).

- :meth:`ReminderService.save_preferences` — the installation's
  reminder policy (PUT).
- :meth:`ReminderService.run_daily_reminder_job` — the daily
  Procrastinate job (``reminders.daily``): one scan across every
  installation with reminders enabled, four reminder types
  (cheque due-soon, overdue installments, uncounted cash boxes,
  eNamad expiry), three channels (in-app + email + SMS).

Channel matrix (see ``docs/reminders.md``):

- in-app = a review-queue item of type ``reminder`` (the
  operator's in-app surface — the same surface the cheque daily
  job and the dunning sweep already use for reminders). It may
  still be recorded during quiet hours.
- email = the F3 notification registry's email provider — NOT
  YET BUILT. No sender is wired, so email attempts are logged as
  skipped (``EMAIL_NOT_CONFIGURED``), never as sent.
- SMS = the US-544 multi-provider registry, through the shared
  client builder (:func:`build_provider_client`). Cost per SMS
  is recorded on the log row (the US-544 informational-Toman
  rule). NO live SMS without credentials — the simulator is the
  only path until an operator configures a live provider.

Consent (customer-facing SMS only — in-app + email go to the
operator, never the customer):

- installments: the plan's contract must have opted in
  (``SmsContractReminderSettings`` — the US-544 rule). No
  contract, or no opt-in, means no automatic SMS.
- cheques: only RECEIVED cheques are SMS'd (the issuer is the
  customer); the cheque must link to an installment leg whose
  plan's contract opted in. An unlinked cheque has no consent
  on file — no SMS, fail-closed. Issued cheques are never SMS'd
  (no customer to remind; the operator funds them).

Shared SMS dedup with US-544: the ``sms.daily_reminders`` job
already SMSes installment legs at its own offsets (default 1 day
overdue — the same offset US-564 fires at). Whichever job sends
first wins: US-564 checks the US-544 log
(``find_sent_log``) before sending an installment SMS, and after
sending it mirrors the send into the US-544 log — so the other
job's dedup sees it too. The mirror insert is the race backstop
(a concurrent send surfaces as ``SmsReminderAlreadySentError``;
the SMS went out once, and the reminder log says so honestly).

Ordering: validation first (fail-closed), then the gates (type
enabled, channel enabled, dedup, quiet hours, consent, phone,
provider), then the send. One channel's failure never blocks the
others; one installation's failure never blocks the rest. The
service never commits — the caller (router / worker) owns the
transaction.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from hesabyar.application.commands.installments import resolve_cheque_states
from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.application.commands.sms import (
    build_provider_client,
    provider_sendable_reason,
)
from hesabyar.domain.cashbox.models import CountSessionStatus
from hesabyar.domain.cashbox.ports import CashBoxPort, CountSessionFilters
from hesabyar.domain.cheques.models import Cheque, ChequeKind
from hesabyar.domain.cheques.ports import ChequeRepositoryPort
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.domain.installments.bnpl import plan_enters_dunning
from hesabyar.domain.installments.models import InstallmentPlan
from hesabyar.domain.installments.ports import InstallmentRepositoryPort
from hesabyar.domain.installments.state_machine import split_allocations
from hesabyar.domain.integrations.ports import CredentialVaultPort
from hesabyar.domain.reminders.errors import ReminderAlreadySentError
from hesabyar.domain.reminders.messages import (
    cashbox_uncounted_message,
    cheque_due_soon_message,
    enmad_expiry_message,
    installment_overdue_message,
)
from hesabyar.domain.reminders.models import (
    ReminderChannel,
    ReminderLogEntry,
    ReminderLogStatus,
    ReminderPreferences,
    ReminderType,
    build_idempotency_key,
    is_quiet_hour,
    new_log_id,
)
from hesabyar.domain.reminders.ports import (
    EmailSenderPort,
    ReminderRepositoryPort,
)
from hesabyar.domain.reminders.triggers import (
    CashboxCountView,
    cashbox_uncounted,
    cheque_due_soon,
    enmad_expiry,
)
from hesabyar.domain.reminders.triggers import (
    ChequeDueView as TriggerChequeView,
)
from hesabyar.domain.reminders.validation import validate_preferences_payload
from hesabyar.domain.review_queue.models import (
    ReviewItemStatus,
    ReviewItemType,
    ReviewSeverity,
)
from hesabyar.domain.review_queue.ports import ReviewItemFilters, ReviewQueuePort
from hesabyar.domain.scheduler.schedules import tehran_now
from hesabyar.domain.sms.errors import SmsError, SmsReminderAlreadySentError
from hesabyar.domain.sms.models import (
    SmsDeliveryStatus,
    SmsLogStatus,
    SmsProvider,
    SmsReminderLogEntry,
    SmsReminderTrigger,
)
from hesabyar.domain.sms.models import (
    new_log_id as new_sms_log_id,
)
from hesabyar.domain.sms.ports import SmsClientFactory, SmsRepositoryPort
from hesabyar.domain.sms.scheduling import LegReminderView, due_reminders
from hesabyar.domain.sms.templates import to_persian_digits
from hesabyar.domain.sms.validation import normalize_iranian_mobile

logger = logging.getLogger(__name__)

#: Page size for the daily job's plan / cheque scans.
_JOB_PAGE_SIZE = 200

#: Actor recorded on review items filed by the daily job.
_JOB_ACTOR = "worker:reminders.daily"

#: SMS delivery states mapped to reminder-log statuses (the US-544
#: mapping: an UNKNOWN delivery is recorded as sent — the provider
#: accepted the message; its final state is unverified).
_DELIVERY_TO_LOG_STATUS: dict[SmsDeliveryStatus, ReminderLogStatus] = {
    SmsDeliveryStatus.QUEUED: ReminderLogStatus.QUEUED,
    SmsDeliveryStatus.SENT: ReminderLogStatus.SENT,
    SmsDeliveryStatus.DELIVERED: ReminderLogStatus.DELIVERED,
    SmsDeliveryStatus.UNKNOWN: ReminderLogStatus.SENT,
}

#: SMS delivery states mapped to sms-log statuses (the mirror row).
_DELIVERY_TO_SMS_LOG_STATUS: dict[SmsDeliveryStatus, SmsLogStatus] = {
    SmsDeliveryStatus.QUEUED: SmsLogStatus.QUEUED,
    SmsDeliveryStatus.SENT: SmsLogStatus.SENT,
    SmsDeliveryStatus.DELIVERED: SmsLogStatus.DELIVERED,
    SmsDeliveryStatus.UNKNOWN: SmsLogStatus.SENT,
}

#: Suggested actions for the in-app review items, per reminder type.
_SUGGESTED_ACTIONS: dict[ReminderType, str] = {
    ReminderType.CHEQUE_DUE_SOON: (
        "Check the cheque's due date and follow up with the issuer (received) "
        "or fund the account (issued) before it falls due."
    ),
    ReminderType.INSTALLMENT_OVERDUE: (
        "Follow up on the overdue installment leg; the dunning sweep tracks "
        "its stage and late fee."
    ),
    ReminderType.CASHBOX_UNCOUNTED: (
        "Count the cash box (a supervised counting session) and reconcile "
        "any variance."
    ),
    ReminderType.ENMAD_EXPIRY: (
        "Renew the eNamad before it expires — expiry disables gateway and "
        "BNPL service."
    ),
}


@dataclass(frozen=True)
class ReminderDailyReport:
    """The daily reminder job's outcome (logged by the worker)."""

    installations_scanned: int
    reminders_due: int
    sent_in_app: int
    sent_email: int
    sent_sms: int
    failed: int
    skipped_quiet_hours: int
    skipped_no_consent: int
    skipped_no_phone: int
    skipped_no_provider: int
    skipped_already_sent: int
    skipped_email_not_configured: int
    #: Skipped for any other reason (review queue unwired, email
    #: recipient missing) — the log row carries the exact reason.
    skipped_other: int = 0
    errors: tuple[str, ...] = ()

    def summary(self) -> dict[str, Any]:
        """JSON-serializable form for the run record."""
        return {
            "installations_scanned": self.installations_scanned,
            "reminders_due": self.reminders_due,
            "sent_in_app": self.sent_in_app,
            "sent_email": self.sent_email,
            "sent_sms": self.sent_sms,
            "failed": self.failed,
            "skipped_quiet_hours": self.skipped_quiet_hours,
            "skipped_no_consent": self.skipped_no_consent,
            "skipped_no_phone": self.skipped_no_phone,
            "skipped_no_provider": self.skipped_no_provider,
            "skipped_already_sent": self.skipped_already_sent,
            "skipped_email_not_configured": self.skipped_email_not_configured,
            "skipped_other": self.skipped_other,
            "errors": list(self.errors),
        }


@dataclass(frozen=True)
class _Target:
    """One reminder event, ready for per-channel dispatch.

    ``sms_phone`` / ``sms_consent`` / ``sms_consent_reason`` apply
    to the SMS channel only; ``sms_leg`` carries the installment
    leg identity for the US-544 dedup check + mirror row (None
    for cheques — the sms log is leg-shaped, a cheque SMS lives
    only in the reminder log).
    """

    reminder_type: ReminderType
    subject_type: str
    subject_id: str
    lead_days: int
    event_ref: str
    message: str
    amount_irr: int | None
    severity: ReviewSeverity
    company_engine_id: int | None = None
    sms_phone: str | None = None
    sms_consent: bool = False
    sms_consent_reason: str | None = None
    sms_plan_id: str | None = None
    sms_leg_id: str | None = None
    sms_offset_days: int | None = None

    @property
    def sms_applicable(self) -> bool:
        """Whether SMS is even a possible channel for this target.

        Cash-box + eNamad reminders have no external recipient —
        they are operator reminders, in-app + email only.
        """
        return self.reminder_type in (
            ReminderType.CHEQUE_DUE_SOON,
            ReminderType.INSTALLMENT_OVERDUE,
        )


class ReminderService:
    """Reminder preferences + the daily reminder job."""

    def __init__(
        self,
        repository: ReminderRepositoryPort,
        cheques: ChequeRepositoryPort,
        installments: InstallmentRepositoryPort,
        cashbox: CashBoxPort,
        sms: SmsRepositoryPort,
        client_factory: SmsClientFactory,
        vault: CredentialVaultPort | None = None,
        review: ReviewQueueCommandService | None = None,
        queue: ReviewQueuePort | None = None,
        email: EmailSenderPort | None = None,
    ) -> None:
        self._repository = repository
        self._cheques = cheques
        self._installments = installments
        self._cashbox = cashbox
        self._sms = sms
        self._clients = client_factory
        self._vault = vault
        self._review = review
        self._queue = queue
        self._email = email

    # -- preferences ----------------------------------------------------

    def save_preferences(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        payload: dict[str, object],
    ) -> ReminderPreferences:
        """Insert or replace the installation's reminder policy (PUT)."""
        validated = validate_preferences_payload(payload)
        return self._repository.save_preferences(
            ReminderPreferences(
                installation_id=installation_id,
                tenant_id=tenant_id,
                enabled=validated.enabled,
                cheque_due_soon_enabled=validated.cheque_due_soon_enabled,
                installment_overdue_enabled=validated.installment_overdue_enabled,
                cashbox_uncounted_enabled=validated.cashbox_uncounted_enabled,
                enmad_expiry_enabled=validated.enmad_expiry_enabled,
                channel_in_app=validated.channel_in_app,
                channel_email=validated.channel_email,
                channel_sms=validated.channel_sms,
                cheque_lead_days=validated.cheque_lead_days,
                installment_overdue_days=validated.installment_overdue_days,
                cashbox_uncounted_days=validated.cashbox_uncounted_days,
                enmad_lead_days=validated.enmad_lead_days,
                quiet_hours_enabled=validated.quiet_hours_enabled,
                quiet_hours_start=validated.quiet_hours_start,
                quiet_hours_end=validated.quiet_hours_end,
                enmad_expiry_date=validated.enmad_expiry_date,
                notification_email=validated.notification_email,
            )
        )

    # -- daily job ---------------------------------------------------------

    def run_daily_reminder_job(
        self,
        *,
        today: datetime.date | None = None,
        now: datetime.datetime | None = None,
        actor: str = _JOB_ACTOR,
    ) -> ReminderDailyReport:
        """Run the daily scan across every installation with reminders enabled.

        For each installation: gather the four scans' targets, then
        dispatch each target over its enabled channels. A skipped
        attempt is always logged (never silent); a failed send gets
        a ``failed`` row + (for SMS) a review item, and the job
        continues with the next target. One installation's failure
        never blocks the others.
        """
        day = today or tehran_now().date()
        moment = now or tehran_now()
        installations_scanned = 0
        reminders_due = 0
        sent_in_app = 0
        sent_email = 0
        sent_sms = 0
        failed = 0
        skipped_quiet_hours = 0
        skipped_no_consent = 0
        skipped_no_phone = 0
        skipped_no_provider = 0
        skipped_already_sent = 0
        skipped_email_not_configured = 0
        skipped_other = 0
        errors: list[str] = []

        for prefs in self._repository.list_preferences_enabled():
            installation_id = prefs.installation_id
            tenant_id = prefs.tenant_id
            installations_scanned += 1
            try:
                targets = self._gather_targets(prefs, day)
            except Exception as exc:  # noqa: BLE001 - one installation never blocks the rest
                error = str(exc) or exc.__class__.__name__
                errors.append(f"installation {installation_id}: scan failed: {error}")
                logger.exception(
                    "Reminder job: scan failed for installation %s", installation_id
                )
                continue
            reminders_due += len(targets)
            if not targets:
                continue
            provider = self._resolve_provider(installation_id) if prefs.channel_sms else None
            provider_sendable = (
                provider_sendable_reason(provider) if provider is not None else None
            )
            if (
                provider is not None
                and provider_sendable is not None
                and any(target.sms_applicable for target in targets)
            ):
                # One review item for the installation (deduped
                # against open items), not one per reminder.
                self._file_provider_not_configured(
                    tenant_id=tenant_id,
                    installation_id=installation_id,
                    provider=provider,
                    reason=provider_sendable,
                    actor=actor,
                )
            for target in targets:
                for channel in prefs.enabled_channels():
                    if channel is ReminderChannel.SMS and not target.sms_applicable:
                        continue
                    outcome = self._dispatch(
                        target,
                        channel,
                        prefs=prefs,
                        provider=provider,
                        provider_sendable_reason_text=provider_sendable,
                        moment=moment,
                        actor=actor,
                    )
                    if outcome == "sent_in_app":
                        sent_in_app += 1
                    elif outcome == "sent_email":
                        sent_email += 1
                    elif outcome == "sent_sms":
                        sent_sms += 1
                    elif outcome == "failed":
                        failed += 1
                    elif outcome == "skipped_quiet_hours":
                        skipped_quiet_hours += 1
                    elif outcome == "skipped_no_consent":
                        skipped_no_consent += 1
                    elif outcome == "skipped_no_phone":
                        skipped_no_phone += 1
                    elif outcome == "skipped_no_provider":
                        skipped_no_provider += 1
                    elif outcome == "skipped_already_sent":
                        skipped_already_sent += 1
                    elif outcome == "skipped_email_not_configured":
                        skipped_email_not_configured += 1
                    elif outcome == "skipped_other":
                        skipped_other += 1

        return ReminderDailyReport(
            installations_scanned=installations_scanned,
            reminders_due=reminders_due,
            sent_in_app=sent_in_app,
            sent_email=sent_email,
            sent_sms=sent_sms,
            failed=failed,
            skipped_quiet_hours=skipped_quiet_hours,
            skipped_no_consent=skipped_no_consent,
            skipped_no_phone=skipped_no_phone,
            skipped_no_provider=skipped_no_provider,
            skipped_already_sent=skipped_already_sent,
            skipped_email_not_configured=skipped_email_not_configured,
            skipped_other=skipped_other,
            errors=tuple(errors),
        )

    # -- target gathering ---------------------------------------------------

    def _gather_targets(
        self, prefs: ReminderPreferences, today: datetime.date
    ) -> list[_Target]:
        """Gather every reminder due today for one installation."""
        targets: list[_Target] = []
        if prefs.type_enabled(ReminderType.CHEQUE_DUE_SOON):
            targets.extend(self._cheque_targets(prefs, today))
        if prefs.type_enabled(ReminderType.INSTALLMENT_OVERDUE):
            targets.extend(self._installment_targets(prefs, today))
        if prefs.type_enabled(ReminderType.CASHBOX_UNCOUNTED):
            targets.extend(self._cashbox_targets(prefs, today))
        if prefs.type_enabled(ReminderType.ENMAD_EXPIRY):
            target = self._enmad_target(prefs, today)
            if target is not None:
                targets.append(target)
        return targets

    def _cheque_targets(
        self, prefs: ReminderPreferences, today: datetime.date
    ) -> list[_Target]:
        max_lead = max(prefs.cheque_lead_days) if prefs.cheque_lead_days else 0
        due_before = today + datetime.timedelta(days=max_lead + 1)
        cheques: list[Cheque] = []
        offset = 0
        while True:
            page = self._cheques.list(
                tenant_id=prefs.tenant_id,
                installation_id=prefs.installation_id,
                due_before=due_before,
                limit=_JOB_PAGE_SIZE,
                offset=offset,
            )
            cheques.extend(page)
            if len(page) < _JOB_PAGE_SIZE:
                break
            offset += len(page)
        by_id = {cheque.id: cheque for cheque in cheques}
        views = [
            TriggerChequeView(
                cheque_id=cheque.id,
                due_date=cheque.due_date,
                state=cheque.state.value,
                purpose=cheque.purpose.value,
                kind=cheque.kind.value,
            )
            for cheque in cheques
        ]
        targets: list[_Target] = []
        for item in cheque_due_soon(views, today, prefs.cheque_lead_days):
            cheque = by_id[item.cheque_id]
            phone: str | None = None
            consent = False
            consent_reason: str | None = None
            if cheque.kind is ChequeKind.RECEIVED:
                # SMS goes to the issuer (the customer) — only with
                # consent: the cheque must link to an installment
                # leg whose plan's contract opted in (the US-544
                # rule, extended to cheques). An unlinked cheque
                # has no consent on file: no SMS, fail-closed.
                phone = normalize_iranian_mobile(cheque.customer_ref)
                consent, consent_reason = self._cheque_sms_consent(
                    cheque, tenant_id=prefs.tenant_id, installation_id=prefs.installation_id
                )
            message = cheque_due_soon_message(
                amount_toman=cheque.amount_irr // 10,
                due_date=cheque.due_date,
                days_remaining=item.lead_days,
                sayad_id=cheque.sayad_id,
                issuer_name=(
                    cheque.issuer_name
                    if cheque.kind is ChequeKind.RECEIVED
                    else cheque.payee_name
                ),
                kind=cheque.kind.value,
            )
            targets.append(
                _Target(
                    reminder_type=ReminderType.CHEQUE_DUE_SOON,
                    subject_type="cheque",
                    subject_id=cheque.id,
                    lead_days=item.lead_days,
                    event_ref=cheque.due_date.gregorian_date.isoformat(),
                    message=message,
                    amount_irr=cheque.amount_irr,
                    severity=ReviewSeverity.WARNING,
                    company_engine_id=cheque.company_engine_id,
                    sms_phone=phone,
                    sms_consent=consent,
                    sms_consent_reason=consent_reason,
                )
            )
        return targets

    def _cheque_sms_consent(
        self, cheque: Cheque, *, tenant_id: str, installation_id: str
    ) -> tuple[bool, str | None]:
        """Consent for a received cheque's SMS: the linked leg's plan contract.

        Returns (consented, reason-when-not). An unlinked cheque
        has no consent on file; a linked cheque follows its plan's
        contract opt-in (US-544).
        """
        leg_id = cheque.installment_leg_id
        if leg_id is None:
            return False, (
                "the cheque is not linked to an installment plan, so no "
                "customer consent for SMS reminders is on file."
            )
        leg = self._installments.get_leg(
            leg_id, tenant_id=tenant_id, installation_id=installation_id
        )
        if leg is None:
            return False, "the cheque's linked installment leg no longer exists."
        plan = self._installments.get_plan(
            leg.plan_id, tenant_id=tenant_id, installation_id=installation_id
        )
        if plan is None or plan.contract_id is None:
            return False, "the cheque's linked plan has no sale contract."
        opt_in = self._sms.get_contract_settings(
            installation_id=installation_id, contract_id=plan.contract_id
        )
        if opt_in is None or not opt_in.reminders_enabled:
            return False, "the linked plan's contract has not opted in to SMS reminders."
        return True, None

    def _installment_targets(
        self, prefs: ReminderPreferences, today: datetime.date
    ) -> list[_Target]:
        opt_ins = {
            row.contract_id: row.reminders_enabled
            for row in self._sms.list_contract_settings(
                installation_id=prefs.installation_id
            )
        }
        targets: list[_Target] = []
        offset = 0
        while True:
            plans = self._installments.list_plans(
                tenant_id=prefs.tenant_id,
                installation_id=prefs.installation_id,
                limit=_JOB_PAGE_SIZE,
                offset=offset,
            )
            if not plans:
                break
            offset += len(plans)
            for plan in plans:
                if not plan_enters_dunning(plan):
                    # BNPL (US-543): the customer owes the gateway,
                    # not the seller — no reminder from the seller.
                    continue
                targets.extend(
                    self._plan_targets(plan, prefs, today, opt_ins)
                )
        return targets

    def _plan_targets(
        self,
        plan: InstallmentPlan,
        prefs: ReminderPreferences,
        today: datetime.date,
        opt_ins: dict[str, bool],
    ) -> list[_Target]:
        views = self._leg_views(
            plan.id, tenant_id=prefs.tenant_id, installation_id=prefs.installation_id
        )
        phone = normalize_iranian_mobile(plan.customer_ref)
        consent = bool(plan.contract_id) and opt_ins.get(plan.contract_id or "", False)
        consent_reason: str | None = None
        if not consent:
            consent_reason = (
                "the plan's contract has not opted in to SMS reminders."
                if plan.contract_id is not None
                else "the plan has no sale contract."
            )
        targets: list[_Target] = []
        for item in due_reminders(views, today, (), prefs.installment_overdue_days):
            message = installment_overdue_message(
                amount_toman=item.balance.amount // 10,
                due_date=item.due_date,
                days_overdue=item.offset_days,
                invoice_id=plan.invoice_id,
                leg_sequence=item.sequence,
            )
            targets.append(
                _Target(
                    reminder_type=ReminderType.INSTALLMENT_OVERDUE,
                    subject_type="installment_leg",
                    subject_id=item.leg_id,
                    lead_days=item.offset_days,
                    event_ref=item.due_date.gregorian_date.isoformat(),
                    message=message,
                    amount_irr=item.balance.amount,
                    severity=ReviewSeverity.WARNING,
                    company_engine_id=plan.company_engine_id,
                    sms_phone=phone,
                    sms_consent=consent,
                    sms_consent_reason=consent_reason,
                    sms_plan_id=plan.id,
                    sms_leg_id=item.leg_id,
                    sms_offset_days=item.offset_days,
                )
            )
        return targets

    def _leg_views(
        self,
        plan_id: str,
        *,
        tenant_id: str,
        installation_id: str,
    ) -> list[LegReminderView]:
        """Cheque-aware leg views (the US-544 split, same public pieces).

        Received money counts toward the balance; pending-cheque
        money does not, and a leg fully covered by a cheque in
        flight is not reminded (the cheque may still clear). A
        bounced cheque's money counts as nothing — the leg is
        remindable again.
        """
        legs = self._installments.list_legs(
            plan_id, tenant_id=tenant_id, installation_id=installation_id
        )
        receipts = self._installments.list_receipts(
            plan_id, tenant_id=tenant_id, installation_id=installation_id
        )
        cheque_states = resolve_cheque_states(
            receipts, self._cheques, tenant_id=tenant_id, installation_id=installation_id
        )
        received_by_leg, pending_by_leg = split_allocations(receipts, cheque_states)
        views: list[LegReminderView] = []
        for leg in legs:
            received = received_by_leg.get(leg.id, 0)
            pending = pending_by_leg.get(leg.id, 0)
            views.append(
                LegReminderView(
                    leg_id=leg.id,
                    sequence=leg.sequence,
                    due_date=leg.due_date,
                    principal=leg.principal,
                    received=MoneyIRR(received),
                    pending_clearance=(
                        pending > 0 and received + pending >= leg.principal.amount
                    ),
                )
            )
        return views

    def _cashbox_targets(
        self, prefs: ReminderPreferences, today: datetime.date
    ) -> list[_Target]:
        views: list[CashboxCountView] = []
        for box in self._cashbox.list_cash_boxes(prefs.tenant_id):
            if not box.active:
                continue
            sessions = self._cashbox.list_sessions(
                prefs.tenant_id, CountSessionFilters(cash_box_id=box.id)
            )
            archived = [
                session
                for session in sessions
                if session.status is CountSessionStatus.ARCHIVED
            ]
            has_open = len(archived) != len(sessions)
            if archived:
                # Sessions list newest first: the first archived row
                # is the latest completed count.
                reference = archived[0].created_at.date()
                never_counted = False
            elif box.created_at is not None:
                reference = box.created_at.date()
                never_counted = True
            else:
                reference = None
                never_counted = True
            views.append(
                CashboxCountView(
                    box_id=box.id,
                    box_name=box.name_fa,
                    reference_date=reference,
                    never_counted=never_counted,
                    has_open_session=has_open,
                )
            )
        targets: list[_Target] = []
        for item in cashbox_uncounted(views, today, prefs.cashbox_uncounted_days):
            last_count = (
                None
                if item.never_counted
                else to_persian_digits(
                    FiscalDate(item.reference_date).to_jalali_str(delimiter="/")
                )
            )
            targets.append(
                _Target(
                    reminder_type=ReminderType.CASHBOX_UNCOUNTED,
                    subject_type="cashbox",
                    subject_id=item.box_id,
                    lead_days=prefs.cashbox_uncounted_days,
                    event_ref=item.reference_date.isoformat(),
                    message=cashbox_uncounted_message(
                        box_name=item.box_name,
                        days_uncounted=item.days_uncounted,
                        last_count_jalali=last_count,
                    ),
                    amount_irr=None,
                    severity=ReviewSeverity.WARNING,
                )
            )
        return targets

    def _enmad_target(
        self, prefs: ReminderPreferences, today: datetime.date
    ) -> _Target | None:
        if prefs.enmad_expiry_date is None:
            return None
        firing = enmad_expiry(
            FiscalDate(prefs.enmad_expiry_date), today, prefs.enmad_lead_days
        )
        if firing is None:
            return None
        return _Target(
            reminder_type=ReminderType.ENMAD_EXPIRY,
            subject_type="installation",
            subject_id=prefs.installation_id,
            lead_days=firing.lead_days,
            event_ref=firing.expiry_date.gregorian_date.isoformat(),
            message=enmad_expiry_message(
                expiry_date=firing.expiry_date,
                days_remaining=firing.lead_days,
            ),
            amount_irr=None,
            severity=(
                ReviewSeverity.CRITICAL
                if firing.lead_days == 0
                else ReviewSeverity.WARNING
            ),
        )

    # -- dispatch -----------------------------------------------------------

    def _dispatch(
        self,
        target: _Target,
        channel: ReminderChannel,
        *,
        prefs: ReminderPreferences,
        provider: SmsProvider | None,
        provider_sendable_reason_text: str | None,
        moment: datetime.datetime,
        actor: str,
    ) -> str:
        """Dispatch one target over one channel; returns the outcome bucket.

        The dedup check runs first (a sent row for the key means
        "already sent" — counted, not re-logged). Then the
        channel-specific gates, then the send. Every non-dedup
        outcome writes exactly one log row.
        """
        key = build_idempotency_key(
            reminder_type=target.reminder_type,
            subject_id=target.subject_id,
            event_ref=target.event_ref,
            channel=channel,
        )
        already = self._repository.find_sent_log(
            installation_id=prefs.installation_id, idempotency_key=key
        )
        if already is not None:
            return "skipped_already_sent"
        if channel is ReminderChannel.IN_APP:
            return self._dispatch_in_app(target, prefs, key, actor)
        if channel is ReminderChannel.EMAIL:
            return self._dispatch_email(target, prefs, key, moment)
        return self._dispatch_sms(
            target,
            prefs,
            key,
            provider=provider,
            provider_sendable_reason_text=provider_sendable_reason_text,
            moment=moment,
            actor=actor,
        )

    def _dispatch_in_app(
        self,
        target: _Target,
        prefs: ReminderPreferences,
        key: str,
        actor: str,
    ) -> str:
        """In-app = a review-queue item (may still record during quiet hours)."""
        if self._review is None:
            self._insert_log(
                target,
                prefs,
                key,
                ReminderChannel.IN_APP,
                ReminderLogStatus.SKIPPED,
                error_code="REVIEW_QUEUE_UNAVAILABLE",
                error_detail="No review service is wired; the in-app reminder was not recorded.",
            )
            return "skipped_other"
        try:
            self._review.file_item(
                tenant_id=prefs.tenant_id,
                item_type=ReviewItemType.REMINDER,
                severity=target.severity,
                reason=target.message,
                suggested_action=_SUGGESTED_ACTIONS[target.reminder_type],
                company_id=(
                    str(target.company_engine_id)
                    if target.company_engine_id is not None
                    else None
                ),
                subtype=target.reminder_type.value,
                channel="in_app",
                source_event_ref=key,
                amount_irr=target.amount_irr,
                actor=actor,
            )
        except Exception as exc:  # noqa: BLE001 - one channel never blocks the others
            self._insert_log(
                target,
                prefs,
                key,
                ReminderChannel.IN_APP,
                ReminderLogStatus.FAILED,
                error_code="REMINDER_IN_APP_FAILED",
                error_detail=f"Could not file the in-app reminder: {exc}",
            )
            return "failed"
        self._insert_log(
            target,
            prefs,
            key,
            ReminderChannel.IN_APP,
            ReminderLogStatus.SENT,
            sent_at=datetime.datetime.now(datetime.UTC),
        )
        return "sent_in_app"

    def _dispatch_email(
        self,
        target: _Target,
        prefs: ReminderPreferences,
        key: str,
        moment: datetime.datetime,
    ) -> str:
        """Email — no sender exists yet, so every attempt is an honest skip."""
        if self._email is None:
            self._insert_log(
                target,
                prefs,
                key,
                ReminderChannel.EMAIL,
                ReminderLogStatus.SKIPPED,
                error_code="EMAIL_NOT_CONFIGURED",
                error_detail=(
                    "No email provider is configured for reminders "
                    "(the F3 notification registry's email provider is not built yet)."
                ),
            )
            return "skipped_email_not_configured"
        if prefs.notification_email is None:
            self._insert_log(
                target,
                prefs,
                key,
                ReminderChannel.EMAIL,
                ReminderLogStatus.SKIPPED,
                error_code="EMAIL_RECIPIENT_MISSING",
                error_detail="No notification email address is configured in the reminder preferences.",
            )
            return "skipped_other"
        if prefs.quiet_hours_enabled and is_quiet_hour(
            moment, start=prefs.quiet_hours_start, end=prefs.quiet_hours_end
        ):
            self._insert_log(
                target,
                prefs,
                key,
                ReminderChannel.EMAIL,
                ReminderLogStatus.SKIPPED,
                error_code="QUIET_HOURS",
                error_detail="Email is suppressed during quiet hours.",
            )
            return "skipped_quiet_hours"
        try:
            result = self._email.send(
                to=prefs.notification_email,
                subject="یادآوری حسابیار",
                body=target.message,
            )
        except Exception as exc:  # noqa: BLE001 - one channel never blocks the others
            self._insert_log(
                target,
                prefs,
                key,
                ReminderChannel.EMAIL,
                ReminderLogStatus.FAILED,
                recipient=prefs.notification_email,
                error_code="EMAIL_SEND_FAILED",
                error_detail=f"Unexpected email send error: {exc}",
            )
            return "failed"
        self._insert_log(
            target,
            prefs,
            key,
            ReminderChannel.EMAIL,
            ReminderLogStatus.SENT,
            recipient=prefs.notification_email,
            provider_message_id=result.provider_message_id,
            provider_status_raw=result.provider_status_raw,
            sent_at=datetime.datetime.now(datetime.UTC),
        )
        return "sent_email"

    def _dispatch_sms(
        self,
        target: _Target,
        prefs: ReminderPreferences,
        key: str,
        *,
        provider: SmsProvider | None,
        provider_sendable_reason_text: str | None,
        moment: datetime.datetime,
        actor: str,
    ) -> str:
        """SMS via the US-544 registry (consent + dedup + cost recording)."""
        if prefs.quiet_hours_enabled and is_quiet_hour(
            moment, start=prefs.quiet_hours_start, end=prefs.quiet_hours_end
        ):
            self._insert_log(
                target,
                prefs,
                key,
                ReminderChannel.SMS,
                ReminderLogStatus.SKIPPED,
                recipient=target.sms_phone,
                error_code="QUIET_HOURS",
                error_detail="SMS is suppressed during quiet hours.",
            )
            return "skipped_quiet_hours"
        if not target.sms_consent:
            self._insert_log(
                target,
                prefs,
                key,
                ReminderChannel.SMS,
                ReminderLogStatus.SKIPPED,
                recipient=target.sms_phone,
                error_code="SMS_NOT_OPTED_IN",
                error_detail=target.sms_consent_reason
                or "No customer consent for SMS reminders is on file.",
            )
            return "skipped_no_consent"
        if target.sms_phone is None:
            self._insert_log(
                target,
                prefs,
                key,
                ReminderChannel.SMS,
                ReminderLogStatus.SKIPPED,
                error_code="SMS_RECIPIENT_PHONE_MISSING",
                error_detail="No usable mobile number is on file for this reminder's recipient.",
            )
            return "skipped_no_phone"
        if provider is None or not provider.enabled:
            self._insert_log(
                target,
                prefs,
                key,
                ReminderChannel.SMS,
                ReminderLogStatus.SKIPPED,
                recipient=target.sms_phone,
                error_code="SMS_NO_PROVIDER_AVAILABLE",
                error_detail="No enabled default SMS provider exists in this installation.",
            )
            return "skipped_no_provider"
        if provider_sendable_reason_text is not None:
            self._insert_log(
                target,
                prefs,
                key,
                ReminderChannel.SMS,
                ReminderLogStatus.SKIPPED,
                recipient=target.sms_phone,
                provider_id=provider.id,
                provider_key=provider.provider_key,
                error_code="SMS_PROVIDER_NOT_CONFIGURED",
                error_detail=provider_sendable_reason_text,
            )
            return "skipped_no_provider"
        # Shared dedup with US-544: an installment SMS the
        # sms.daily_reminders job already sent for this leg + offset
        # is never sent twice.
        if (
            target.sms_leg_id is not None
            and target.sms_offset_days is not None
            and self._sms.find_sent_log(
                installation_id=prefs.installation_id,
                leg_id=target.sms_leg_id,
                offset_days=target.sms_offset_days,
            )
            is not None
        ):
            return "skipped_already_sent"
        now = datetime.datetime.now(datetime.UTC)
        try:
            client = build_provider_client(
                self._sms, self._clients, self._vault, provider
            )
            try:
                result = client.send(
                    to=target.sms_phone,
                    message=target.message,
                    sender_line=provider.line_number,
                )
            finally:
                client.close()
        except SmsError as exc:
            return self._record_sms_failure(
                target,
                prefs,
                key,
                provider,
                error_code=exc.code,
                error_detail=exc.message,
                actor=actor,
            )
        except Exception as exc:  # noqa: BLE001 - an unexpected client error is still a failed send
            return self._record_sms_failure(
                target,
                prefs,
                key,
                provider,
                error_code="SMS_SEND_FAILED",
                error_detail=f"Unexpected send error: {exc}",
                actor=actor,
            )
        if result.status is SmsDeliveryStatus.FAILED:
            return self._record_sms_failure(
                target,
                prefs,
                key,
                provider,
                error_code="SMS_PROVIDER_REJECTED",
                error_detail=(
                    "The provider reported the message as failed"
                    + (f": {result.provider_status_raw}" if result.provider_status_raw else ".")
                ),
                actor=actor,
            )
        status = _DELIVERY_TO_LOG_STATUS[result.status]
        # Mirror installment sends into the US-544 log, so the
        # sms.daily_reminders job's dedup sees them too. A race
        # (US-544 sent in the meantime) surfaces as
        # SmsReminderAlreadySentError — the SMS went out once via
        # US-544 and once via us; the log row says so honestly.
        race_note: str | None = None
        if (
            target.sms_plan_id is not None
            and target.sms_leg_id is not None
            and target.sms_offset_days is not None
        ):
            try:
                self._sms.insert_log(
                    SmsReminderLogEntry(
                        id=new_sms_log_id(),
                        tenant_id=prefs.tenant_id,
                        installation_id=prefs.installation_id,
                        plan_id=target.sms_plan_id,
                        leg_id=target.sms_leg_id,
                        provider_id=provider.id,
                        provider_key=provider.provider_key,
                        recipient_phone=target.sms_phone,
                        message_text=target.message,
                        offset_days=target.sms_offset_days,
                        trigger=SmsReminderTrigger.SCHEDULED,
                        status=_DELIVERY_TO_SMS_LOG_STATUS[result.status],
                        provider_message_id=result.provider_message_id,
                        provider_status_raw=result.provider_status_raw,
                        cost_toman=result.cost_toman,
                        provider_cost_raw=result.cost_raw,
                        provider_cost_unit=result.cost_unit,
                        sent_at=now,
                    )
                )
            except SmsReminderAlreadySentError:
                race_note = (
                    "Duplicate SMS race: the US-544 daily job sent for this "
                    "leg + offset concurrently; the customer received two messages."
                )
        self._insert_log(
            target,
            prefs,
            key,
            ReminderChannel.SMS,
            status,
            recipient=target.sms_phone,
            provider_id=provider.id,
            provider_key=provider.provider_key,
            provider_message_id=result.provider_message_id,
            provider_status_raw=result.provider_status_raw,
            cost_toman=result.cost_toman,
            provider_cost_raw=result.cost_raw,
            provider_cost_unit=result.cost_unit,
            error_detail=race_note,
            sent_at=now,
        )
        return "sent_sms"

    def _record_sms_failure(
        self,
        target: _Target,
        prefs: ReminderPreferences,
        key: str,
        provider: SmsProvider,
        *,
        error_code: str,
        error_detail: str,
        actor: str,
    ) -> str:
        """Log a failed SMS row + file a review item (never silent)."""
        self._insert_log(
            target,
            prefs,
            key,
            ReminderChannel.SMS,
            ReminderLogStatus.FAILED,
            recipient=target.sms_phone,
            provider_id=provider.id,
            provider_key=provider.provider_key,
            error_code=error_code,
            error_detail=error_detail,
        )
        if self._review is not None:
            try:
                self._review.file_item(
                    tenant_id=prefs.tenant_id,
                    item_type=ReviewItemType.SMS_DELIVERY_FAILED,
                    severity=ReviewSeverity.WARNING,
                    reason=(
                        f"SMS reminder failed for {target.subject_type} "
                        f"{target.subject_id} ({target.reminder_type.value}, "
                        f"lead {target.lead_days} days) via provider "
                        f"{provider.provider_key}: {error_code}."
                    ),
                    suggested_action=(
                        "Check the provider configuration and re-send the "
                        "reminder once the cause is fixed."
                    ),
                    company_id=(
                        str(target.company_engine_id)
                        if target.company_engine_id is not None
                        else None
                    ),
                    subtype="reminder_sms_failed",
                    channel=provider.provider_key,
                    source_event_ref=key,
                    amount_irr=target.amount_irr,
                    actor=actor,
                )
            except Exception:  # noqa: BLE001 - filing must never mask the send outcome
                logger.exception(
                    "Failed to file SMS failure review item for %s", key
                )
        return "failed"

    def _file_provider_not_configured(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        provider: SmsProvider,
        reason: str,
        actor: str,
    ) -> None:
        """One review item per installation for an unsendable default provider.

        Deduped against open items with the same ref, so a
        misconfigured provider files once — not once per run.
        """
        if self._review is None or self._queue is None:
            return
        ref = f"sms_provider:{provider.provider_key}"
        try:
            existing_refs = {
                item.source_event_ref
                for item in self._queue.list_items(
                    tenant_id,
                    ReviewItemFilters(item_type=ReviewItemType.SMS_DELIVERY_FAILED),
                )
                if item.status in (ReviewItemStatus.OPEN, ReviewItemStatus.IN_REVIEW)
                and item.source_event_ref is not None
            }
        except Exception:  # noqa: BLE001 - the dedup lookup must never block the job
            existing_refs = set()
        if ref in existing_refs:
            return
        try:
            self._review.file_item(
                tenant_id=tenant_id,
                item_type=ReviewItemType.SMS_DELIVERY_FAILED,
                severity=ReviewSeverity.WARNING,
                reason=(
                    f"SMS provider {provider.provider_key!r} is the default but "
                    f"cannot send ({reason}); due reminders were skipped."
                ),
                suggested_action=(
                    "Configure the provider's credentials (or pick another default "
                    "provider) and the skipped reminders will send on the next run "
                    "where their trigger still matches."
                ),
                subtype="sms_provider_not_configured",
                channel=provider.provider_key,
                source_event_ref=ref,
                actor=actor,
            )
        except Exception:  # noqa: BLE001 - filing must never block the job
            logger.exception(
                "Failed to file provider-not-configured item for installation %s",
                installation_id,
            )

    # -- helpers -----------------------------------------------------------

    def _resolve_provider(self, installation_id: str) -> SmsProvider | None:
        provider = self._sms.get_default_provider(installation_id=installation_id)
        if provider is None or not provider.enabled:
            return None
        return provider

    def _insert_log(
        self,
        target: _Target,
        prefs: ReminderPreferences,
        key: str,
        channel: ReminderChannel,
        status: ReminderLogStatus,
        *,
        recipient: str | None = None,
        provider_id: str | None = None,
        provider_key: str | None = None,
        provider_message_id: str | None = None,
        provider_status_raw: str | None = None,
        cost_toman: Decimal | None = None,
        provider_cost_raw: str | None = None,
        provider_cost_unit: str | None = None,
        error_code: str | None = None,
        error_detail: str | None = None,
        sent_at: datetime.datetime | None = None,
    ) -> ReminderLogEntry | None:
        """Insert one log row; a dedup race returns None (already sent)."""
        entry = ReminderLogEntry(
            id=new_log_id(),
            tenant_id=prefs.tenant_id,
            installation_id=prefs.installation_id,
            reminder_type=target.reminder_type,
            subject_type=target.subject_type,
            subject_id=target.subject_id,
            lead_days=target.lead_days,
            channel=channel,
            status=status,
            idempotency_key=key,
            message_text=target.message,
            recipient=recipient,
            provider_id=provider_id,
            provider_key=provider_key,
            provider_message_id=provider_message_id,
            provider_status_raw=provider_status_raw,
            cost_toman=cost_toman,
            provider_cost_raw=provider_cost_raw,
            provider_cost_unit=provider_cost_unit,
            error_code=error_code,
            error_detail=error_detail,
            sent_at=sent_at,
        )
        try:
            return self._repository.insert_log(entry)
        except ReminderAlreadySentError:
            # A concurrent run sent this key first — the reminder
            # went out once; nothing more to do.
            return None
