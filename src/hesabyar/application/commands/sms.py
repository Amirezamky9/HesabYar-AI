"""SMS command services (US-544, Phase 5-5 ۵ج).

- :class:`SmsRegistryService` — the provider registry (CRUD +
  seed), the installation reminder policy, and the per-contract
  opt-ins. Provider credentials are encrypted through the vault
  (Fernet) and never logged or returned.
- :class:`SmsReminderService` — the manual trigger
  (``POST /installments/plans/{id}/remind``) and the daily
  Procrastinate job (``sms.daily_reminders``). Both share one
  send path: resolve the provider, render the (user-editable)
  template, send through the provider client, log the attempt,
  and file a review item on failure — never silent.

Ordering: validation first (fail-closed, nothing stored on a bad
payload), then the gates (provider enabled, leg remindable,
dedup), then the send. The services never commit — the caller
(router / worker) owns the transaction.
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import NoReturn

from hesabyar.application.commands.installments import resolve_cheque_states
from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.domain.cheques.ports import ChequeRepositoryPort
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.domain.installments.errors import (
    InstallmentContractNotFoundError,
    InstallmentPlanNotFoundError,
)
from hesabyar.domain.installments.models import InstallmentPlan, PlanKind
from hesabyar.domain.installments.ports import InstallmentRepositoryPort
from hesabyar.domain.installments.state_machine import split_allocations
from hesabyar.domain.integrations.ports import CredentialVaultPort
from hesabyar.domain.review_queue.models import ReviewItemType, ReviewSeverity
from hesabyar.domain.sms.commands import (
    UNSET,
    CreateSmsProvider,
    RotateSmsProviderCredentials,
    UpdateSmsProvider,
)
from hesabyar.domain.sms.credentials import (
    parse_sms_credentials,
    require_credential_fields,
)
from hesabyar.domain.sms.errors import (
    SmsError,
    SmsLegNotFoundError,
    SmsLegNotRemindableError,
    SmsNoProviderAvailableError,
    SmsNoUnpaidLegError,
    SmsProviderInUseError,
    SmsProviderKeyConflictError,
    SmsProviderNotConfiguredError,
    SmsProviderNotEnabledError,
    SmsProviderNotFoundError,
    SmsProviderRejectedError,
    SmsRecipientPhoneMissingError,
    SmsReminderAlreadySentError,
    SmsValidationError,
    SmsVaultUnavailableError,
)
from hesabyar.domain.sms.models import (
    PROVIDER_KIND_CUSTOM,
    PROVIDER_KIND_SIMULATOR,
    SmsContractReminderSettings,
    SmsDeliveryStatus,
    SmsLogStatus,
    SmsProvider,
    SmsProviderStatus,
    SmsReminderLogEntry,
    SmsReminderSettings,
    SmsReminderTrigger,
    new_contract_settings_id,
    new_log_id,
)
from hesabyar.domain.sms.ports import (
    SmsClientFactory,
    SmsProviderClient,
    SmsRepositoryPort,
)
from hesabyar.domain.sms.scheduling import LegReminderView, due_reminders
from hesabyar.domain.sms.templates import ReminderTemplateContext, render_reminder_template
from hesabyar.domain.sms.validation import (
    normalize_iranian_mobile,
    validate_provider_payload,
    validate_reminder_settings_payload,
)

logger = logging.getLogger(__name__)

#: Page size for the daily job's plan scan (the installment
#: repository paginates; the job loops until a page comes back short).
_JOB_PLAN_PAGE_SIZE = 200

#: Actor recorded on review items filed by the daily job.
_JOB_ACTOR = "worker:sms.daily_reminders"


def _encode_credentials(
    credentials: dict[str, str], vault: CredentialVaultPort | None
) -> tuple[str, str]:
    """Encrypt a credential payload (JSON, sorted keys) via the vault.

    Fails closed with a typed 503 when no vault is configured —
    secrets are never stored unencrypted, never in .env.
    """
    import json

    if vault is None:
        raise SmsVaultUnavailableError()
    plaintext = json.dumps(credentials, sort_keys=True, ensure_ascii=False)
    return vault.encrypt(plaintext), vault.key_id


def build_provider_client(
    repository: SmsRepositoryPort,
    client_factory: SmsClientFactory,
    vault: CredentialVaultPort | None,
    provider: SmsProvider,
) -> SmsProviderClient:
    """Build a provider's client from its decrypted credentials.

    Shared by the US-544 reminder service and the US-564 reminder
    service (both send through the same registry): ONE definition
    of "build the client", so the vault/decrypt/require-fields
    fail-closed path cannot drift between the two senders.

    Fail-closed: a live provider without stored credentials, a
    ciphertext the vault cannot decrypt, or a payload missing its
    required fields all refuse here — no live call is ever
    attempted down a path that cannot succeed.
    """
    credentials: dict[str, str] = {}
    if provider.has_credentials:
        if vault is None:
            raise SmsVaultUnavailableError()
        ciphertext = repository.load_provider_credentials(
            provider.id, installation_id=provider.installation_id
        )
        if ciphertext is None:
            raise SmsProviderNotConfiguredError(
                provider.provider_key,
                reason="the provider reports stored credentials but none were found; "
                "rotate the credentials.",
            )
        try:
            plaintext = vault.decrypt(ciphertext)
        except ValueError as exc:
            raise SmsProviderNotConfiguredError(
                provider.provider_key,
                reason="the stored credentials cannot be decrypted; rotate them.",
            ) from exc
        payload = parse_sms_credentials(plaintext, provider_key=provider.provider_key)
        credentials = require_credential_fields(
            payload,
            provider_kind=provider.provider_kind,
            provider_key=provider.provider_key,
        )
    return client_factory.client_for(
        provider_kind=provider.provider_kind,
        provider_key=provider.provider_key,
        credentials=credentials,
        line_number=provider.line_number,
    )


def provider_sendable_reason(provider: SmsProvider) -> str | None:
    """Why this provider cannot send right now, or None when it can.

    Shared by the US-544 daily job's pre-flight check and the
    US-564 reminder service: a live kind needs stored credentials,
    and a custom kind has no client at all. The simulator is always
    sendable.
    """
    if provider.provider_kind == PROVIDER_KIND_CUSTOM:
        return "no SMS client exists for custom providers yet."
    if provider.provider_kind == PROVIDER_KIND_SIMULATOR:
        return None
    if not provider.has_credentials:
        return "no credentials are stored for this provider."
    return None


class SmsRegistryService:
    """Provider registry writes + reminder policy + contract opt-ins."""

    def __init__(
        self,
        repository: SmsRepositoryPort,
        installments: InstallmentRepositoryPort,
        vault: CredentialVaultPort | None = None,
    ) -> None:
        self._repository = repository
        self._installments = installments
        self._vault = vault

    # -- providers -----------------------------------------------------

    def seed_builtin_providers(self, installation_id: str) -> list[SmsProvider]:
        """Create the built-in provider rows (idempotent).

        Kavenegar + sms.ir are born disabled (``setup``); the
        simulator is born enabled AND default — reminders work as
        logged simulations with zero configuration, and no live
        send can happen before an operator configures a provider.
        """
        return self._repository.seed_builtin_providers(installation_id)

    def create_provider(
        self, *, installation_id: str, payload: dict[str, object]
    ) -> SmsProvider:
        """Create a provider row (custom kinds, or a re-created built-in).

        New providers are born disabled with status ``setup``
        unless the payload explicitly enables them. A duplicate key
        is a 409; making the new row default clears the previous
        default first. Credentials, when supplied, are encrypted
        and stored — never returned.
        """
        validated = validate_provider_payload(payload, partial=False)
        assert validated.provider_key is not None
        assert validated.display_name_fa is not None
        assert validated.provider_kind is not None
        existing = self._repository.get_provider_by_key(
            validated.provider_key, installation_id=installation_id
        )
        if existing is not None:
            raise SmsProviderKeyConflictError(validated.provider_key)
        if validated.is_default:
            self._clear_default(installation_id)
        status = validated.status
        if status is None:
            status = (
                SmsProviderStatus.ACTIVE if validated.enabled else SmsProviderStatus.SETUP
            )
        provider = self._repository.create_provider(
            CreateSmsProvider(
                installation_id=installation_id,
                provider_key=validated.provider_key,
                display_name_fa=validated.display_name_fa,
                provider_kind=validated.provider_kind,
                enabled=bool(validated.enabled),
                is_default=bool(validated.is_default),
                status=status,
                line_number=validated.line_number,
                price_per_sms_toman=validated.price_per_sms_toman,
                rate_limit_per_minute=validated.rate_limit_per_minute,
                line_type=validated.line_type,
                contract_required=bool(validated.contract_required),
                enmad_required=bool(validated.enmad_required),
                notes=validated.notes,
            )
        )
        if validated.credentials is not None:
            provider = self._rotate_credentials(provider, validated.credentials)
        return provider

    def update_provider(
        self, *, installation_id: str, provider_id: str, payload: dict[str, object]
    ) -> SmsProvider:
        """Patch a provider row (only the supplied fields change).

        ``provider_key`` / ``provider_kind`` are immutable (a kind
        change would orphan the stored credentials). Making the row
        default clears the previous default first. ``credentials``
        rotates the stored payload (encrypted); any other field set
        to null clears it.
        """
        existing = self._repository.get_provider(
            provider_id, installation_id=installation_id
        )
        if existing is None:
            raise SmsProviderNotFoundError(provider_id)
        validated = validate_provider_payload(
            payload, partial=True, provider_kind=existing.provider_kind
        )
        if validated.is_default and not existing.is_default:
            self._clear_default(installation_id)
        command = UpdateSmsProvider(
            provider_id=provider_id,
            installation_id=installation_id,
            display_name_fa=validated.display_name_fa,
            enabled=validated.enabled,
            is_default=validated.is_default,
            status=validated.status,
            line_number=validated.line_number if "line_number" in payload else UNSET,
            price_per_sms_toman=(
                validated.price_per_sms_toman if "price_per_sms_toman" in payload else UNSET
            ),
            rate_limit_per_minute=(
                validated.rate_limit_per_minute
                if "rate_limit_per_minute" in payload
                else UNSET
            ),
            line_type=validated.line_type if "line_type" in payload else UNSET,
            contract_required=validated.contract_required,
            enmad_required=validated.enmad_required,
            notes=validated.notes if "notes" in payload else UNSET,
        )
        updated = self._repository.update_provider(command)
        if updated is None:
            raise SmsProviderNotFoundError(provider_id)
        if validated.credentials is not None:
            updated = self._rotate_credentials(updated, validated.credentials)
        return updated

    def delete_provider(self, *, installation_id: str, provider_id: str) -> None:
        """Delete a provider row.

        The default provider cannot be deleted (mark another
        provider default first). Log rows survive a delete: they
        keep their denormalized ``provider_key`` and their
        ``provider_id`` is set NULL.
        """
        existing = self._repository.get_provider(
            provider_id, installation_id=installation_id
        )
        if existing is None:
            raise SmsProviderNotFoundError(provider_id)
        if existing.is_default:
            raise SmsProviderInUseError(
                existing.provider_key,
                reason="it is the installation's default provider; "
                "mark another provider default first.",
            )
        deleted = self._repository.delete_provider(
            provider_id, installation_id=installation_id
        )
        if not deleted:
            raise SmsProviderNotFoundError(provider_id)

    def _clear_default(self, installation_id: str) -> None:
        current = self._repository.get_default_provider(installation_id=installation_id)
        if current is not None:
            self._repository.update_provider(
                UpdateSmsProvider(
                    provider_id=current.id,
                    installation_id=installation_id,
                    is_default=False,
                )
            )

    def _rotate_credentials(
        self, provider: SmsProvider, credentials: dict[str, str]
    ) -> SmsProvider:
        ciphertext, key_id = _encode_credentials(credentials, self._vault)
        rotated = self._repository.rotate_provider_credentials(
            RotateSmsProviderCredentials(
                provider_id=provider.id,
                installation_id=provider.installation_id,
                credentials_ciphertext=ciphertext,
                credentials_key_id=key_id,
                credential_field_names=tuple(sorted(credentials)),
            )
        )
        assert rotated is not None  # the provider existed a moment ago
        return rotated

    # -- reminder policy -------------------------------------------------

    def save_reminder_settings(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        payload: dict[str, object],
    ) -> SmsReminderSettings:
        """Insert or replace the installation's reminder policy (PUT)."""
        validated = validate_reminder_settings_payload(payload)
        return self._repository.save_reminder_settings(
            SmsReminderSettings(
                installation_id=installation_id,
                tenant_id=tenant_id,
                enabled=validated.enabled,
                offsets_days_before=validated.offsets_days_before,
                offsets_days_overdue=validated.offsets_days_overdue,
                template_before_due=validated.template_before_due,
                template_overdue=validated.template_overdue,
            )
        )

    # -- per-contract opt-ins ---------------------------------------------

    def save_contract_settings(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        contract_id: str,
        payload: dict[str, object],
    ) -> SmsContractReminderSettings:
        """Insert or replace a contract's reminder opt-in.

        The contract must exist in this installation (404
        otherwise) — an opt-in for a dangling contract ref is
        never stored.
        """
        contract = self._installments.get_contract(
            contract_id, tenant_id=tenant_id, installation_id=installation_id
        )
        if contract is None:
            raise InstallmentContractNotFoundError(contract_id)
        enabled = payload.get("reminders_enabled")
        if not isinstance(enabled, bool):
            raise SmsValidationError(
                "The contract reminder settings are invalid.",
                field_errors={"reminders_enabled": "must be a boolean."},
            )
        existing = self._repository.get_contract_settings(
            installation_id=installation_id, contract_id=contract_id
        )
        return self._repository.save_contract_settings(
            SmsContractReminderSettings(
                id=existing.id if existing is not None else new_contract_settings_id(),
                installation_id=installation_id,
                contract_id=contract_id,
                reminders_enabled=enabled,
            )
        )


@dataclass(frozen=True)
class SmsDailyReport:
    """The daily reminder job's outcome (logged by the worker)."""

    installations_scanned: int
    plans_scanned: int
    reminders_due: int
    sent: int
    failed: int
    skipped_no_phone: int
    skipped_not_opted_in: int
    skipped_no_provider: int
    skipped_pending_clearance: int
    skipped_already_sent: int
    #: BNPL plans skipped by the daily job (US-543): the customer
    #: owes the gateway, not the seller — there is nothing for the
    #: seller to remind about. Defaulted so older constructions
    #: stay valid.
    skipped_bnpl: int = 0
    errors: tuple[str, ...] = ()


class SmsReminderService:
    """Manual reminder trigger + the daily reminder job.

    Both paths share :meth:`_send_one`: build the client from the
    provider's decrypted credentials (fail-closed when
    unconfigured), send, log the attempt with provider + cost +
    delivery status, and file a review item on failure — never
    silent. The DLQ is the reminder log's ``failed`` rows; the
    replay is the manual trigger.
    """

    def __init__(
        self,
        repository: SmsRepositoryPort,
        installments: InstallmentRepositoryPort,
        client_factory: SmsClientFactory,
        vault: CredentialVaultPort | None = None,
        review: ReviewQueueCommandService | None = None,
        cheques: ChequeRepositoryPort | None = None,
    ) -> None:
        self._repository = repository
        self._installments = installments
        self._clients = client_factory
        self._vault = vault
        self._review = review
        # The cheque repository (US-542) lets the leg views split
        # received vs pending money; without it cheque receipts
        # stay pending (fail-closed — no reminder for a leg the
        # cheque may still cover).
        self._cheques = cheques

    # -- manual trigger ---------------------------------------------------

    def send_manual_reminder(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        plan_id: str,
        leg_id: str | None = None,
        provider_id: str | None = None,
        recipient_phone: str | None = None,
        dry_run: bool = False,
        today: datetime.date | None = None,
        pending_clearance_leg_ids: frozenset[str] = frozenset(),
        actor: str = "api",
    ) -> SmsReminderLogEntry:
        """Send (or dry-run) a reminder for one leg of one plan.

        The manual trigger is itself the opt-in for this one send
        (an explicit operator action) — the contract opt-in gates
        AUTOMATIC sends only. The provider must still be enabled,
        the leg must be unpaid and not awaiting cheque clearing,
        and a reminder for this leg + offset must not already have
        gone out (409). ``leg_id`` omitted means "the earliest
        unpaid leg". A dry run renders the message, logs a
        ``dry_run`` row, and sends nothing.
        """
        day = today or datetime.date.today()
        plan = self._installments.get_plan(
            plan_id, tenant_id=tenant_id, installation_id=installation_id
        )
        if plan is None:
            raise InstallmentPlanNotFoundError(plan_id)
        views = self._leg_views(
            plan_id,
            tenant_id=tenant_id,
            installation_id=installation_id,
            pending_clearance_leg_ids=pending_clearance_leg_ids,
        )
        view: LegReminderView | None = None
        if leg_id is not None:
            view = next((item for item in views if item.leg_id == leg_id), None)
            if view is None:
                raise SmsLegNotFoundError(leg_id=leg_id, plan_id=plan_id)
        else:
            unpaid = sorted(
                (item for item in views if item.balance.amount > 0),
                key=lambda item: (item.due_date.gregorian_date, item.sequence),
            )
            if not unpaid:
                raise SmsNoUnpaidLegError(plan_id=plan_id)
            view = unpaid[0]
        if plan.kind == PlanKind.BNPL:
            # BNPL (US-543): the customer owes the gateway, not the
            # seller — there is nothing for the seller to remind
            # about. A rejected BNPL receipt returns the legs to
            # "expected", but the plan is still a BNPL plan.
            raise SmsLegNotRemindableError(
                leg_id=view.leg_id,
                reason="the plan is a BNPL plan — the customer pays the gateway, not the seller.",
            )
        if view.pending_clearance:
            raise SmsLegNotRemindableError(
                leg_id=view.leg_id,
                reason="the leg was paid by cheque and the cheque has not cleared yet.",
            )
        if view.balance.amount <= 0:
            raise SmsLegNotRemindableError(leg_id=view.leg_id, reason="the leg is paid.")
        phone = self._resolve_phone(recipient_phone, plan.customer_ref, plan_id=plan_id)
        provider = self._resolve_provider(
            installation_id, provider_id=provider_id
        )
        offset_days = -(view.due_date.gregorian_date - day).days
        if not dry_run:
            already = self._repository.find_sent_log(
                installation_id=installation_id,
                leg_id=view.leg_id,
                offset_days=offset_days,
            )
            if already is not None:
                raise SmsReminderAlreadySentError(
                    leg_id=view.leg_id, offset_days=offset_days
                )
        settings = self._settings_for(installation_id, tenant_id)
        message = self._render_message(settings, plan.invoice_id, view, offset_days)
        if dry_run:
            entry = SmsReminderLogEntry(
                id=new_log_id(),
                tenant_id=tenant_id,
                installation_id=installation_id,
                plan_id=plan_id,
                leg_id=view.leg_id,
                provider_id=provider.id,
                provider_key=provider.provider_key,
                recipient_phone=phone,
                message_text=message,
                offset_days=offset_days,
                trigger=SmsReminderTrigger.MANUAL,
                status=SmsLogStatus.DRY_RUN,
            )
            return self._repository.insert_log(entry)
        return self._send_one(
            tenant_id=tenant_id,
            installation_id=installation_id,
            plan_id=plan_id,
            company_engine_id=plan.company_engine_id,
            leg_id=view.leg_id,
            balance_irr=view.balance.amount,
            offset_days=offset_days,
            trigger=SmsReminderTrigger.MANUAL,
            provider=provider,
            phone=phone,
            message=message,
            actor=actor,
        )

    # -- daily job ---------------------------------------------------------

    def run_daily_reminder_job(
        self,
        *,
        today: datetime.date | None = None,
        pending_clearance_leg_ids: frozenset[str] = frozenset(),
        actor: str = _JOB_ACTOR,
    ) -> SmsDailyReport:
        """Run the daily scan across every installation with reminders enabled.

        For each installation: resolve the default provider, scan
        the opted-in plans' legs for offsets firing today, and send
        one reminder per (leg, offset). A leg with no phone on file
        gets a ``skipped`` log row (never silent, never a review
        item — it is a data gap, not a failure). A provider that
        cannot send (live kind without credentials, or a custom
        kind with no client) gets ONE review item per installation
        plus a ``skipped`` row per due reminder — never one review
        item per leg. Per-send failures get a ``failed`` row + one
        review item each, and the job continues with the next leg.
        """
        day = today or datetime.date.today()
        installations_scanned = 0
        plans_scanned = 0
        reminders_due = 0
        sent = 0
        failed = 0
        skipped_no_phone = 0
        skipped_not_opted_in = 0
        skipped_no_provider = 0
        skipped_pending_clearance = 0
        skipped_already_sent = 0
        skipped_bnpl = 0
        errors: list[str] = []

        for settings in self._repository.list_reminder_settings_enabled():
            installation_id = settings.installation_id
            tenant_id = settings.tenant_id
            installations_scanned += 1
            provider = self._repository.get_default_provider(
                installation_id=installation_id
            )
            if provider is None or not provider.enabled:
                skipped_no_provider += 1
                errors.append(
                    f"installation {installation_id}: no enabled default SMS provider; "
                    "reminders skipped."
                )
                continue
            opt_ins = {
                row.contract_id: row.reminders_enabled
                for row in self._repository.list_contract_settings(
                    installation_id=installation_id
                )
            }
            due_targets: list[tuple[InstallmentPlan, LegReminderView, int, str | None]] = []
            for plan in self._iter_plans(tenant_id, installation_id):
                plans_scanned += 1
                if plan.contract_id is None or not opt_ins.get(plan.contract_id, False):
                    skipped_not_opted_in += 1
                    continue
                if plan.kind == PlanKind.BNPL:
                    # BNPL (US-543): the customer owes the gateway,
                    # not the seller — no reminder from the seller.
                    skipped_bnpl += 1
                    continue
                views = self._leg_views(
                    plan.id,
                    tenant_id=tenant_id,
                    installation_id=installation_id,
                    pending_clearance_leg_ids=pending_clearance_leg_ids,
                )
                # Legs that would fire but for pending clearance, counted (not sent).
                unfiltered = due_reminders(
                    [
                        LegReminderView(
                            leg_id=item.leg_id,
                            sequence=item.sequence,
                            due_date=item.due_date,
                            principal=item.principal,
                            received=item.received,
                            pending_clearance=False,
                        )
                        for item in views
                    ],
                    day,
                    settings.offsets_days_before,
                    settings.offsets_days_overdue,
                )
                due = due_reminders(
                    views,
                    day,
                    settings.offsets_days_before,
                    settings.offsets_days_overdue,
                )
                skipped_pending_clearance += len(unfiltered) - len(due)
                phone = normalize_iranian_mobile(plan.customer_ref)
                for item in due:
                    view = next(v for v in views if v.leg_id == item.leg_id)
                    due_targets.append((plan, view, item.offset_days, phone))
            reminders_due += len(due_targets)
            if not due_targets:
                continue
            sendable_reason = self._provider_sendable_reason(provider)
            if sendable_reason is not None:
                # One review item for the installation, one skipped row per reminder.
                for plan, view, offset_days, phone in due_targets:
                    message = self._render_message(
                        settings, plan.invoice_id, view, offset_days
                    )
                    self._repository.insert_log(
                        SmsReminderLogEntry(
                            id=new_log_id(),
                            tenant_id=tenant_id,
                            installation_id=installation_id,
                            plan_id=plan.id,
                            leg_id=view.leg_id,
                            provider_id=provider.id,
                            provider_key=provider.provider_key,
                            recipient_phone=phone or "",
                            message_text=message,
                            offset_days=offset_days,
                            trigger=SmsReminderTrigger.SCHEDULED,
                            status=SmsLogStatus.SKIPPED,
                            error_code="SMS_PROVIDER_NOT_CONFIGURED",
                            error_detail=sendable_reason,
                        )
                    )
                self._file_review(
                    tenant_id=tenant_id,
                    company_id=None,
                    subtype="sms_provider_not_configured",
                    severity=ReviewSeverity.WARNING,
                    reason=(
                        f"SMS provider {provider.provider_key!r} is the default but "
                        f"cannot send ({sendable_reason}); "
                        f"{len(due_targets)} due reminder(s) were skipped."
                    ),
                    suggested_action=(
                        "Configure the provider's credentials (or pick another default "
                        "provider) and re-send the skipped reminders from each plan."
                    ),
                    channel=provider.provider_key,
                    source_event_ref=f"sms_provider:{provider.provider_key}",
                    amount_irr=None,
                    actor=actor,
                )
                errors.append(
                    f"installation {installation_id}: provider {provider.provider_key!r} "
                    f"cannot send ({sendable_reason}); {len(due_targets)} reminder(s) skipped."
                )
                continue
            for plan, view, offset_days, phone in due_targets:
                message = self._render_message(
                    settings, plan.invoice_id, view, offset_days
                )
                if phone is None:
                    self._repository.insert_log(
                        SmsReminderLogEntry(
                            id=new_log_id(),
                            tenant_id=tenant_id,
                            installation_id=installation_id,
                            plan_id=plan.id,
                            leg_id=view.leg_id,
                            provider_id=provider.id,
                            provider_key=provider.provider_key,
                            recipient_phone="",
                            message_text=message,
                            offset_days=offset_days,
                            trigger=SmsReminderTrigger.SCHEDULED,
                            status=SmsLogStatus.SKIPPED,
                            error_code="SMS_RECIPIENT_PHONE_MISSING",
                            error_detail=(
                                "No usable mobile number is on file for this plan's customer."
                            ),
                        )
                    )
                    skipped_no_phone += 1
                    continue
                already = self._repository.find_sent_log(
                    installation_id=installation_id,
                    leg_id=view.leg_id,
                    offset_days=offset_days,
                )
                if already is not None:
                    skipped_already_sent += 1
                    continue
                try:
                    self._send_one(
                        tenant_id=tenant_id,
                        installation_id=installation_id,
                        plan_id=plan.id,
                        company_engine_id=plan.company_engine_id,
                        leg_id=view.leg_id,
                        balance_irr=view.balance.amount,
                        offset_days=offset_days,
                        trigger=SmsReminderTrigger.SCHEDULED,
                        provider=provider,
                        phone=phone,
                        message=message,
                        actor=actor,
                    )
                    sent += 1
                except SmsError as exc:
                    failed += 1
                    errors.append(
                        f"installation {installation_id} leg {view.leg_id}: "
                        f"{exc.code}: {exc.message}"
                    )
                except Exception as exc:  # noqa: BLE001 - one leg must never kill the job
                    failed += 1
                    errors.append(
                        f"installation {installation_id} leg {view.leg_id}: "
                        f"unexpected error: {exc}"
                    )

        return SmsDailyReport(
            installations_scanned=installations_scanned,
            plans_scanned=plans_scanned,
            reminders_due=reminders_due,
            sent=sent,
            failed=failed,
            skipped_no_phone=skipped_no_phone,
            skipped_not_opted_in=skipped_not_opted_in,
            skipped_no_provider=skipped_no_provider,
            skipped_pending_clearance=skipped_pending_clearance,
            skipped_already_sent=skipped_already_sent,
            skipped_bnpl=skipped_bnpl,
            errors=tuple(errors),
        )

    # -- shared send path ---------------------------------------------------

    def _send_one(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        plan_id: str,
        company_engine_id: int,
        leg_id: str,
        balance_irr: int,
        offset_days: int,
        trigger: SmsReminderTrigger,
        provider: SmsProvider,
        phone: str,
        message: str,
        actor: str,
    ) -> SmsReminderLogEntry:
        """Build the client, send, log the attempt, file on failure.

        The client is built per send (cheap for a daily job; a
        later optimization may pool it per installation). A
        provider-reported FAILED delivery state at send time is a
        failure: a ``failed`` row + a review item + a raised
        ``SmsProviderRejectedError`` — never a false success.
        """
        now = datetime.datetime.now(datetime.UTC)
        try:
            client = self._build_client(provider)
            try:
                result = client.send(
                    to=phone, message=message, sender_line=provider.line_number
                )
            finally:
                client.close()
        except SmsError as exc:
            self._record_failure(
                tenant_id=tenant_id,
                installation_id=installation_id,
                plan_id=plan_id,
                company_engine_id=company_engine_id,
                leg_id=leg_id,
                balance_irr=balance_irr,
                offset_days=offset_days,
                trigger=trigger,
                provider=provider,
                phone=phone,
                message=message,
                error_code=exc.code,
                error_detail=exc.message,
                actor=actor,
                raise_error=exc,
            )
        except Exception as exc:  # noqa: BLE001 - an unexpected client error is still a failed send
            self._record_failure(
                tenant_id=tenant_id,
                installation_id=installation_id,
                plan_id=plan_id,
                company_engine_id=company_engine_id,
                leg_id=leg_id,
                balance_irr=balance_irr,
                offset_days=offset_days,
                trigger=trigger,
                provider=provider,
                phone=phone,
                message=message,
                error_code="SMS_SEND_FAILED",
                error_detail=f"Unexpected send error: {exc}",
                actor=actor,
                raise_error=SmsProviderRejectedError(
                    provider.provider_key, reason=f"Unexpected send error: {exc}"
                ),
            )
        if result.status is SmsDeliveryStatus.FAILED:
            self._record_failure(
                tenant_id=tenant_id,
                installation_id=installation_id,
                plan_id=plan_id,
                company_engine_id=company_engine_id,
                leg_id=leg_id,
                balance_irr=balance_irr,
                offset_days=offset_days,
                trigger=trigger,
                provider=provider,
                phone=phone,
                message=message,
                error_code="SMS_PROVIDER_REJECTED",
                error_detail=(
                    "The provider reported the message as failed"
                    + (f": {result.provider_status_raw}" if result.provider_status_raw else ".")
                ),
                actor=actor,
                raise_error=SmsProviderRejectedError(
                    provider.provider_key,
                    reason=(
                        "the provider reported the message as failed"
                        + (f": {result.provider_status_raw}" if result.provider_status_raw else ".")
                    ),
                ),
            )
        status = {
            SmsDeliveryStatus.QUEUED: SmsLogStatus.QUEUED,
            SmsDeliveryStatus.SENT: SmsLogStatus.SENT,
            SmsDeliveryStatus.DELIVERED: SmsLogStatus.DELIVERED,
            SmsDeliveryStatus.UNKNOWN: SmsLogStatus.SENT,
        }[result.status]
        entry = SmsReminderLogEntry(
            id=new_log_id(),
            tenant_id=tenant_id,
            installation_id=installation_id,
            plan_id=plan_id,
            leg_id=leg_id,
            provider_id=provider.id,
            provider_key=provider.provider_key,
            recipient_phone=phone,
            message_text=message,
            offset_days=offset_days,
            trigger=trigger,
            status=status,
            provider_message_id=result.provider_message_id,
            provider_status_raw=result.provider_status_raw,
            cost_toman=result.cost_toman,
            provider_cost_raw=result.cost_raw,
            provider_cost_unit=result.cost_unit,
            sent_at=now,
        )
        return self._repository.insert_log(entry)

    def _record_failure(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        plan_id: str,
        company_engine_id: int,
        leg_id: str,
        balance_irr: int,
        offset_days: int,
        trigger: SmsReminderTrigger,
        provider: SmsProvider,
        phone: str,
        message: str,
        error_code: str,
        error_detail: str,
        actor: str,
        raise_error: SmsError,
    ) -> NoReturn:
        """Log a ``failed`` row, file a review item, then raise (never silent)."""
        entry = SmsReminderLogEntry(
            id=new_log_id(),
            tenant_id=tenant_id,
            installation_id=installation_id,
            plan_id=plan_id,
            leg_id=leg_id,
            provider_id=provider.id,
            provider_key=provider.provider_key,
            recipient_phone=phone,
            message_text=message,
            offset_days=offset_days,
            trigger=trigger,
            status=SmsLogStatus.FAILED,
            error_code=error_code,
            error_detail=error_detail,
        )
        self._repository.insert_log(entry)
        self._file_review(
            tenant_id=tenant_id,
            company_id=str(company_engine_id),
            subtype="sms_reminder_failed",
            severity=ReviewSeverity.WARNING,
            reason=(
                f"SMS reminder failed for installment leg {leg_id} (plan {plan_id}, "
                f"offset {offset_days} days) via provider {provider.provider_key}: "
                f"{error_code}."
            ),
            suggested_action=(
                "Check the provider configuration and re-send the reminder "
                "from the plan (manual trigger)."
            ),
            channel=provider.provider_key,
            source_event_ref=f"installment_leg:{leg_id}",
            amount_irr=balance_irr,
            actor=actor,
        )
        raise raise_error

    def _build_client(self, provider: SmsProvider) -> SmsProviderClient:
        """Build the provider's client (shared helper — see module level)."""
        return build_provider_client(
            self._repository, self._clients, self._vault, provider
        )

    def _provider_sendable_reason(self, provider: SmsProvider) -> str | None:
        """Why this provider cannot send right now (shared helper — see module level)."""
        return provider_sendable_reason(provider)

    def _file_review(
        self,
        *,
        tenant_id: str,
        company_id: str | None,
        subtype: str,
        severity: ReviewSeverity,
        reason: str,
        suggested_action: str,
        channel: str | None,
        source_event_ref: str | None,
        amount_irr: int | None,
        actor: str,
    ) -> None:
        """File an SMS delivery-failure review item; never masks the send outcome."""
        if self._review is None:
            logger.warning(
                "No review service wired; SMS failure not filed: %s (%s)",
                subtype,
                source_event_ref,
            )
            return
        try:
            self._review.file_item(
                tenant_id=tenant_id,
                item_type=ReviewItemType.SMS_DELIVERY_FAILED,
                severity=severity,
                reason=reason,
                suggested_action=suggested_action,
                company_id=company_id,
                subtype=subtype,
                channel=channel,
                source_event_ref=source_event_ref,
                amount_irr=amount_irr,
                actor=actor,
            )
        except Exception:  # noqa: BLE001 - filing must never mask the send outcome
            logger.exception(
                "Failed to file SMS review item %s for %s", subtype, source_event_ref
            )

    # -- helpers -----------------------------------------------------------

    def _settings_for(
        self, installation_id: str, tenant_id: str
    ) -> SmsReminderSettings:
        stored = self._repository.get_reminder_settings(installation_id=installation_id)
        if stored is not None:
            return stored
        return SmsReminderSettings.defaults(
            installation_id=installation_id, tenant_id=tenant_id
        )

    def _resolve_phone(
        self, explicit: str | None, customer_ref: str | None, *, plan_id: str
    ) -> str:
        if explicit is not None:
            phone = normalize_iranian_mobile(explicit)
            if phone is None:
                raise SmsValidationError(
                    "The recipient phone number is invalid.",
                    field_errors={
                        "recipient_phone": "must be an Iranian mobile number (09xxxxxxxxx)."
                    },
                )
            return phone
        phone = normalize_iranian_mobile(customer_ref)
        if phone is None:
            raise SmsRecipientPhoneMissingError(plan_id=plan_id)
        return phone

    def _resolve_provider(
        self, installation_id: str, *, provider_id: str | None
    ) -> SmsProvider:
        if provider_id is not None:
            provider = self._repository.get_provider(
                provider_id, installation_id=installation_id
            )
            if provider is None:
                raise SmsProviderNotFoundError(provider_id)
        else:
            provider = self._repository.get_default_provider(
                installation_id=installation_id
            )
            if provider is None:
                raise SmsNoProviderAvailableError()
        if not provider.enabled:
            raise SmsProviderNotEnabledError(provider.provider_key)
        return provider

    def _render_message(
        self,
        settings: SmsReminderSettings,
        invoice_id: str,
        view: LegReminderView,
        offset_days: int,
    ) -> str:
        template = (
            settings.template_overdue if offset_days > 0 else settings.template_before_due
        )
        context = ReminderTemplateContext(
            amount_toman=view.balance.amount // 10,
            due_date=view.due_date,
            days_remaining=max(0, -offset_days),
            days_overdue=max(0, offset_days),
            invoice_id=invoice_id,
            leg_sequence=view.sequence,
        )
        return render_reminder_template(template, context)

    def _leg_views(
        self,
        plan_id: str,
        *,
        tenant_id: str,
        installation_id: str,
        pending_clearance_leg_ids: frozenset[str],
    ) -> list[LegReminderView]:
        legs = self._installments.list_legs(
            plan_id, tenant_id=tenant_id, installation_id=installation_id
        )
        receipts = self._installments.list_receipts(
            plan_id, tenant_id=tenant_id, installation_id=installation_id
        )
        # The cheque-aware split (US-542): received money counts
        # toward the balance; pending-cheque money does not, and a
        # leg fully covered by a cheque in flight is not reminded
        # (the cheque may still clear). A bounced cheque's money
        # counts as nothing — the leg is remindable again. The
        # explicit set (tests / callers) unions with the computed
        # one; without a cheque repository every cheque receipt
        # stays pending (fail-closed).
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
                        leg.id in pending_clearance_leg_ids
                        or (
                            pending > 0
                            and received + pending >= leg.principal.amount
                        )
                    ),
                )
            )
        return views

    def _iter_plans(self, tenant_id: str, installation_id: str) -> Iterator[InstallmentPlan]:
        """Yield every plan of an installation (paginated through the repository)."""
        offset = 0
        while True:
            page = self._installments.list_plans(
                tenant_id=tenant_id,
                installation_id=installation_id,
                limit=_JOB_PLAN_PAGE_SIZE,
                offset=offset,
            )
            yield from page
            if len(page) < _JOB_PLAN_PAGE_SIZE:
                return
            offset += len(page)
