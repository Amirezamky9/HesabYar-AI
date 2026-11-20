"""Settlement command service (US-521, Phase 5-3 Wave A).

Orchestrates the settlement engine's writes over the repository and
registry ports: record a gateway payment (derive its schedule from
the ACTIVE contract version), cancel, reassign a sales period,
write off a tranche (human approval only), resolve exceptions,
bulk re-derive (explicit user action + audit log), and the daily
tick (W1: expected→due→overdue + late-tranche escalation).

The service never touches the engine (Tryton): settlement
accounting (posting the receivable/clearing/bank moves) is US-522's
job. It also never invents a fee % or a VAT rate — both come from
the contract version (or an explicit per-payment override), and a
missing VAT rate fails closed.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass

import jdatetime

from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.application.errors import IntegrationChannelNotFoundError
from hesabyar.domain.integrations.holidays import HolidayCalendar
from hesabyar.domain.integrations.ports import IntegrationsRegistryPort
from hesabyar.domain.review_queue.models import ReviewItemType, ReviewSeverity
from hesabyar.domain.settlement.commands import (
    CancelGatewayPayment,
    CreateGatewayPayment,
    CreateSettlementException,
    ReassignSalesPeriod,
    RecordTrancheWriteOff,
    ResolveSettlementException,
    TransitionTranche,
)
from hesabyar.domain.settlement.derivation import derive_tranches
from hesabyar.domain.settlement.errors import (
    SettlementContractMissingError,
    SettlementDerivationError,
    SettlementExceptionNotFoundError,
    SettlementFeeVatRateUnknownError,
    SettlementInvalidTransitionError,
    SettlementPaymentExistsError,
    SettlementPaymentInvalidError,
    SettlementPaymentNotFoundError,
    SettlementTrancheNotFoundError,
    SettlementWriteOffNotAllowedError,
)
from hesabyar.domain.settlement.models import (
    GatewayPayment,
    GatewayPaymentStatus,
    SettlementException,
    SettlementExceptionResolution,
    SettlementExceptionSeverity,
    SettlementExceptionStatus,
    SettlementExceptionType,
    SettlementFeeRecord,
    SettlementTranche,
    TrancheState,
    sales_period_from,
)
from hesabyar.domain.settlement.ports import (
    InstallationTenantResolver,
    SettlementAuditPort,
    SettlementRepositoryPort,
)
from hesabyar.domain.settlement.state_machine import (
    WRITE_OFF_ELIGIBLE_STATES,
    escalation_level_for,
    require_transition,
)

__all__ = [
    "BulkRederiveResult",
    "CancelPaymentResult",
    "ReassignPeriodResult",
    "RecordPaymentResult",
    "SettlementCommandService",
    "TickReport",
]

#: ``YYYY/MM`` Jalali period, e.g. ``1405/08``.
_PERIOD_RE = re.compile(r"^(\d{4})/(\d{2})$")

#: Actor recorded on exceptions the daily tick opens.
TICK_ACTOR = "settlement-worker"


@dataclass(frozen=True)
class RecordPaymentResult:
    """A recorded payment with its derived schedule and fee record."""

    payment: GatewayPayment
    tranches: tuple[SettlementTranche, ...]
    fee_record: SettlementFeeRecord


@dataclass(frozen=True)
class CancelPaymentResult:
    """A cancelled payment: what was cancelled, what was already paid."""

    payment: GatewayPayment
    cancelled_tranche_ids: tuple[str, ...]
    refunds_deducted_irr: int
    exception: SettlementException | None
    """``cancelled_after_settlement`` when tranches were already paid."""


@dataclass(frozen=True)
class ReassignPeriodResult:
    """A period correction: which tranches moved, which were untouched."""

    payment: GatewayPayment
    rederived_tranche_ids: tuple[str, ...]
    untouched_tranche_ids: tuple[str, ...]
    exception: SettlementException | None
    """Informational ``period_reassigned`` exception (None on a no-op)."""


@dataclass(frozen=True)
class BulkRederiveResult:
    """A bulk re-derivation run: re-derived payments + skips with reasons."""

    rederived_payment_ids: tuple[str, ...] = ()
    skipped: tuple[tuple[str, str], ...] = ()
    """(payment id, reason) for every payment left untouched."""


@dataclass(frozen=True)
class TickReport:
    """One daily-tick run: transitions made + exceptions touched."""

    moved_to_due: int = 0
    moved_to_overdue: int = 0
    exceptions_created: int = 0
    exceptions_escalated: int = 0
    review_items_filed: int = 0
    """``settlement_variance`` review items filed for new late tranches (US-555)."""


@dataclass
class _TickCounters:
    moved_to_due: int = 0
    moved_to_overdue: int = 0
    exceptions_created: int = 0
    exceptions_escalated: int = 0
    review_items_filed: int = 0


class SettlementCommandService:
    """Settlement writes over the repository + registry ports.

    ``review`` (US-523) is the unified review queue: when present AND
    the caller supplies a ``tenant_id``, exceptions this service opens
    (``cancelled_after_settlement``, ``period_reassigned``) are also
    filed as ``settlement_variance`` review items. The daily tick
    files its newly created ``late_tranche`` exceptions the same way
    (US-555): it has no caller tenant, so it resolves the tenant
    through ``tenant_resolver`` (installation → tenant via the
    API-key store). A tick without a wired resolver, or an
    installation with no tenant, still persists the exception —
    only the queue filing is conditional.
    """

    def __init__(
        self,
        repository: SettlementRepositoryPort,
        registry: IntegrationsRegistryPort,
        audit: SettlementAuditPort,
        *,
        review: ReviewQueueCommandService | None = None,
        tenant_resolver: InstallationTenantResolver | None = None,
    ) -> None:
        self._repository = repository
        self._registry = registry
        self._audit = audit
        self._review = review
        self._tenant_resolver = tenant_resolver

    # -- helpers ----------------------------------------------------------

    def _file_exception_review_item(
        self,
        *,
        tenant_id: str | None,
        exception: SettlementException,
        channel_key: str,
        reason: str,
        suggested_action: str,
    ) -> bool:
        """File a settlement exception in the unified review queue (US-523).

        A no-op (False) when no review service is wired or no tenant
        was supplied (the tick path without a resolver) — the
        exception itself is always persisted; only the queue filing
        is conditional. Returns True when an item was filed.
        """
        if self._review is None or tenant_id is None:
            return False
        amount = (
            exception.actual_amount_irr
            if exception.actual_amount_irr is not None
            else exception.expected_amount_irr
        )
        self._review.file_item(
            tenant_id=tenant_id,
            item_type=ReviewItemType.SETTLEMENT_VARIANCE,
            severity=ReviewSeverity(exception.severity.value),
            reason=reason,
            suggested_action=suggested_action,
            subtype=exception.exception_type.value,
            channel=channel_key,
            source_event_ref=f"settlement-exception:{exception.id}",
            amount_irr=amount,
            actor="settlement",
        )
        return True

    def _file_late_tranche_review_item(
        self,
        *,
        exception: SettlementException,
        tranche: SettlementTranche,
        days_overdue: int,
    ) -> bool:
        """File a tick-created ``late_tranche`` exception (US-555).

        Returns True when an item was filed. The tick has no caller
        tenant, so the tenant is resolved per installation; a
        missing resolver, an unresolvable installation, or a missing
        payment/channel degrades gracefully (no filing, or an empty
        channel) — the exception itself is always persisted.
        """
        if self._review is None or self._tenant_resolver is None:
            return False
        tenant_id = self._tenant_resolver.tenant_id_for_installation(
            tranche.installation_id
        )
        if tenant_id is None:
            return False
        payment = self._repository.get_payment(
            tranche.installation_id, tranche.gateway_payment_id
        )
        channel_key = ""
        if payment is not None:
            channel = self._registry.get_channel_by_id(payment.payment_channel_id)
            channel_key = channel.channel_key if channel is not None else ""
        return self._file_exception_review_item(
            tenant_id=tenant_id,
            exception=exception,
            channel_key=channel_key,
            reason=(
                f"قسط {tranche.tranche_index} از {tranche.tranche_count} درگاه "
                f"{channel_key or 'نامشخص'} به مبلغ {tranche.net_irr:,} ریال، "
                f"{days_overdue} روز از سررسید {tranche.expected_date.isoformat()} "
                "گذشته و هنوز تسویه نشده است."
            ),
            suggested_action=(
                "از درگاه پیگیری کنید؛ اگر تسویه انجام شده قسط را تأیید کنید، "
                "وگرنه آن را به درگاه ارجاع دهید."
            ),
        )

    def _calendar_for(self, installation_id: str) -> HolidayCalendar:
        """Build the installation's holiday calendar from the registry."""
        settings = self._registry.get_installation_settings(installation_id)
        holidays = self._registry.list_holidays(installation_id)
        return HolidayCalendar(
            weekly_holiday=settings.weekly_holiday if settings is not None else 4,
            holidays=frozenset(h.holiday_date for h in holidays),
        )

    # -- record a payment ---------------------------------------------------

    def record_payment(self, command: CreateGatewayPayment) -> RecordPaymentResult:
        """Record a gateway payment and derive its tranche schedule.

        The schedule comes from the ACTIVE contract version of the
        channel at recording time; the fee % and VAT rate actually
        applied are snapshotted on the payment and on a fee record.
        A duplicate (channel, gateway transaction) raises — the
        import/matching stories resolve duplicates by lookup instead.
        """
        if command.gross_irr <= 0:
            raise SettlementPaymentInvalidError(
                f"gross_irr must be positive, got {command.gross_irr}.",
                details={"gross_irr": command.gross_irr},
            )
        if not command.gateway_transaction_id.strip():
            raise SettlementPaymentInvalidError(
                "gateway_transaction_id must not be empty.",
                details={"channel_key": command.channel_key},
            )
        channel = self._registry.get_channel(command.installation_id, command.channel_key)
        if channel is None:
            raise IntegrationChannelNotFoundError(
                f"Payment channel {command.channel_key!r} is not in this installation's registry.",
                details={"channel_key": command.channel_key},
            )
        existing = self._repository.get_payment_by_transaction(
            command.installation_id, channel.id, command.gateway_transaction_id
        )
        if existing is not None:
            raise SettlementPaymentExistsError(
                f"Gateway transaction {command.gateway_transaction_id!r} is already recorded "
                f"for channel {command.channel_key!r} (payment {existing.id}).",
                details={
                    "channel_key": command.channel_key,
                    "gateway_transaction_id": command.gateway_transaction_id,
                    "payment_id": existing.id,
                },
            )
        contract = self._registry.get_active_contract(channel.id)
        if contract is None:
            raise SettlementContractMissingError(
                f"Channel {command.channel_key!r} has no active contract version; "
                "a payment cannot be scheduled without a contract.",
                details={"channel_key": command.channel_key},
            )
        fee_vat_rate = command.fee_vat_rate_override
        if fee_vat_rate is None:
            fee_vat_rate = contract.fee_vat_rate
        if fee_vat_rate is None:
            raise SettlementFeeVatRateUnknownError(
                f"No fee-VAT rate is available for channel {command.channel_key!r}: "
                "the active contract has none and no override was given.",
                details={"channel_key": command.channel_key, "contract_version": contract.version},
            )
        calendar = self._calendar_for(command.installation_id)
        schedule = derive_tranches(
            gross_irr=command.gross_irr,
            contract=contract,
            gateway_confirmed_at=command.gateway_confirmed_at,
            calendar=calendar,
            fee_vat_rate=fee_vat_rate,
        )
        payment = self._repository.save_payment(
            command,
            channel_id=channel.id,
            fee_irr=schedule.fee_irr,
            fee_vat_irr=schedule.fee_vat_irr,
            net_irr=schedule.net_irr,
            contract_version_id=contract.id,
            fee_rate_snapshot=contract.fee_percent,
            fee_vat_rate_snapshot=fee_vat_rate,
            sales_period_jalali=sales_period_from(command.gateway_confirmed_at),
        )
        tranches = self._repository.save_tranches(
            command.installation_id, payment.id, schedule.tranches
        )
        fee_record = self._repository.save_fee_record(
            installation_id=command.installation_id,
            gateway_payment_id=payment.id,
            contract_version_id=contract.id,
            fee_percent_applied=contract.fee_percent,
            fee_vat_rate_applied=fee_vat_rate,
            expected_fee_irr=schedule.fee_irr,
            expected_vat_irr=schedule.fee_vat_irr,
            source="calculated",
        )
        return RecordPaymentResult(
            payment=payment, tranches=tuple(tranches), fee_record=fee_record
        )

    # -- tranche transitions ------------------------------------------------

    def transition_tranche(
        self, installation_id: str, command: TransitionTranche
    ) -> SettlementTranche:
        """Move one tranche to a new state, validated against §3.3.

        The matching stories (US-522/523) drive confirmed/received/
        settled through this; the tick and the cancel/write-off flows
        use their own methods below.
        """
        tranche = self._repository.get_tranche(installation_id, command.tranche_id)
        if tranche is None:
            raise SettlementTrancheNotFoundError(
                f"Settlement tranche {command.tranche_id!r} does not exist.",
                details={"tranche_id": command.tranche_id},
            )
        try:
            to_state = TrancheState(command.to_state)
        except ValueError:
            raise SettlementInvalidTransitionError(
                f"Unknown tranche state {command.to_state!r}.",
                details={"to_state": command.to_state},
            ) from None
        require_transition(tranche.state, to_state)
        updated = self._repository.update_tranche(
            tranche.id,
            state=to_state,
            confirmed_net_irr=command.confirmed_net_irr,
            received_net_irr=command.received_net_irr,
            variance_irr=command.variance_irr,
            variance_reason=command.variance_reason,
            match_method=command.match_method,
            match_confidence=command.match_confidence,
        )
        assert updated is not None  # the tranche existed a moment ago
        return updated

    # -- cancel -------------------------------------------------------------

    def cancel_payment(
        self,
        installation_id: str,
        command: CancelGatewayPayment,
        *,
        tenant_id: str | None = None,
    ) -> CancelPaymentResult:
        """Cancel/refund a payment (phased design §3.3/§3.5).

        Open tranches (expected/due/overdue) are cancelled. Tranches
        already confirmed/received/settled stay as they are — the
        gateway deducts them from later settlements, so their net is
        recorded as ``refunds_deducted_irr`` on the payment and a
        ``cancelled_after_settlement`` exception is opened. The
        payment ends ``cancelled`` (nothing was paid) or
        ``partially_cancelled`` (something was).
        """
        payment = self._repository.get_payment(installation_id, command.payment_id)
        if payment is None:
            raise SettlementPaymentNotFoundError(
                f"Gateway payment {command.payment_id!r} does not exist.",
                details={"payment_id": command.payment_id},
            )
        if payment.status in (
            GatewayPaymentStatus.CANCELLED,
            GatewayPaymentStatus.PARTIALLY_CANCELLED,
        ):
            raise SettlementPaymentInvalidError(
                f"Gateway payment {payment.id} is already {payment.status.value}.",
                details={"payment_id": payment.id, "status": payment.status.value},
            )
        tranches = self._repository.list_tranches_for_payment(installation_id, payment.id)
        cancelled: list[str] = []
        paid_net = 0
        for tranche in tranches:
            if tranche.is_open:
                require_transition(tranche.state, TrancheState.CANCELLED)
                self._repository.update_tranche(tranche.id, state=TrancheState.CANCELLED)
                cancelled.append(tranche.id)
            elif tranche.state in (
                TrancheState.CONFIRMED,
                TrancheState.RECEIVED,
                TrancheState.SETTLED,
                TrancheState.PARTIALLY_CONFIRMED,
                TrancheState.PARTIALLY_RECEIVED,
            ):
                paid_net += max(tranche.received_net_irr, tranche.confirmed_net_irr)
        exception: SettlementException | None = None
        if paid_net > 0:
            exception = self._repository.save_exception(
                CreateSettlementException(
                    installation_id=installation_id,
                    exception_type=SettlementExceptionType.CANCELLED_AFTER_SETTLEMENT.value,
                    severity=SettlementExceptionSeverity.WARNING.value,
                    gateway_payment_id=payment.id,
                    expected_amount_irr=0,
                    actual_amount_irr=paid_net,
                    note=(
                        f"Payment cancelled after {paid_net} IRR was already paid; "
                        f"the gateway deducts it from later settlements. Reason: {command.reason}"
                    ),
                )
            )
        new_status = (
            GatewayPaymentStatus.PARTIALLY_CANCELLED
            if paid_net > 0
            else GatewayPaymentStatus.CANCELLED
        )
        updated = self._repository.update_payment_status(
            payment.id, new_status.value, refunds_deducted_irr=paid_net
        )
        assert updated is not None
        if exception is not None:
            channel = self._registry.get_channel_by_id(payment.payment_channel_id)
            self._file_exception_review_item(
                tenant_id=tenant_id,
                exception=exception,
                channel_key=channel.channel_key if channel is not None else "",
                reason=(
                    f"پرداخت {payment.id} پس از تسویه لغو شد؛ {paid_net:,} ریال قبلاً "
                    "پرداخت شده و درگاه آن را از تسویه‌های بعدی کسر می‌کند."
                ),
                suggested_action="از درگاه پیگیری کنید و تعدیل منفی را در تسویهٔ بعدی بررسی کنید.",
            )
        return CancelPaymentResult(
            payment=updated,
            cancelled_tranche_ids=tuple(cancelled),
            refunds_deducted_irr=paid_net,
            exception=exception,
        )

    # -- period reassignment ------------------------------------------------

    def reassign_period(
        self,
        installation_id: str,
        command: ReassignSalesPeriod,
        *,
        tenant_id: str | None = None,
    ) -> ReassignPeriodResult:
        """Correct a payment's sales period (phased design §3.2).

        Open tranches re-derive their due dates from the new period
        anchor (same contract version, same amounts — only the month
        moves); confirmed/received tranches are untouched; an
        informational ``period_reassigned`` exception is logged.
        """
        match = _PERIOD_RE.match(command.new_sales_period_jalali)
        if match is None:
            raise SettlementPaymentInvalidError(
                f"new_sales_period_jalali must be 'YYYY/MM', got {command.new_sales_period_jalali!r}.",
                details={"new_sales_period_jalali": command.new_sales_period_jalali},
            )
        year, month = int(match.group(1)), int(match.group(2))
        if month < 1 or month > 12:
            raise SettlementPaymentInvalidError(
                f"Jalali month must be 1..12, got {month}.",
                details={"new_sales_period_jalali": command.new_sales_period_jalali},
            )
        payment = self._repository.get_payment(installation_id, command.payment_id)
        if payment is None:
            raise SettlementPaymentNotFoundError(
                f"Gateway payment {command.payment_id!r} does not exist.",
                details={"payment_id": command.payment_id},
            )
        tranches = self._repository.list_tranches_for_payment(installation_id, payment.id)
        if payment.sales_period_jalali == command.new_sales_period_jalali:
            return ReassignPeriodResult(
                payment=payment,
                rederived_tranche_ids=(),
                untouched_tranche_ids=tuple(t.id for t in tranches),
                exception=None,
            )
        contract = self._registry.get_contract_by_id(payment.contract_version_id)
        if contract is None:
            raise SettlementContractMissingError(
                f"The contract version {payment.contract_version_id} this payment was built "
                "from no longer exists; its schedule cannot be re-derived.",
                details={
                    "payment_id": payment.id,
                    "contract_version_id": payment.contract_version_id,
                },
            )
        fee_vat_rate = payment.fee_vat_rate_snapshot
        if fee_vat_rate is None:
            raise SettlementFeeVatRateUnknownError(
                f"Payment {payment.id} has no fee-VAT rate snapshot; its schedule cannot "
                "be re-derived.",
                details={"payment_id": payment.id},
            )
        # The anchor for month arithmetic is the 1st of the new Jalali
        # month (only the month matters for month-based models; day-lag
        # models keep their dates — the lag counts from confirmation).
        anchor = jdatetime.date(year, month, 1).togregorian()
        anchor_dt = datetime.datetime.combine(anchor, datetime.time.min, tzinfo=datetime.UTC)
        calendar = self._calendar_for(installation_id)
        schedule = derive_tranches(
            gross_irr=payment.gross_irr,
            contract=contract,
            gateway_confirmed_at=anchor_dt,
            calendar=calendar,
            fee_vat_rate=fee_vat_rate,
        )
        by_index = {t.tranche_index: t for t in schedule.tranches}
        rederived: list[str] = []
        untouched: list[str] = []
        for tranche in tranches:
            if not tranche.is_open:
                untouched.append(tranche.id)
                continue
            derived = by_index.get(tranche.tranche_index)
            if derived is None:
                # The schedule shape changed under the payment (should
                # not happen — same version, same legs); leave the
                # tranche untouched rather than guessing.
                untouched.append(tranche.id)
                continue
            self._repository.update_tranche(
                tranche.id,
                expected_window_start=derived.expected_window_start,
                expected_date=derived.expected_date,
                sales_period_jalali=command.new_sales_period_jalali,
            )
            rederived.append(tranche.id)
        updated = self._repository.update_payment_sales_period(
            payment.id, command.new_sales_period_jalali
        )
        assert updated is not None
        exception = self._repository.save_exception(
            CreateSettlementException(
                installation_id=installation_id,
                exception_type=SettlementExceptionType.PERIOD_REASSIGNED.value,
                severity=SettlementExceptionSeverity.INFO.value,
                gateway_payment_id=payment.id,
                note=(
                    f"Sales period corrected {payment.sales_period_jalali} → "
                    f"{command.new_sales_period_jalali}; {len(rederived)} open tranche(s) "
                    f"re-derived, {len(untouched)} untouched. Reason: {command.reason}"
                ),
            )
        )
        channel = self._registry.get_channel_by_id(payment.payment_channel_id)
        self._file_exception_review_item(
            tenant_id=tenant_id,
            exception=exception,
            channel_key=channel.channel_key if channel is not None else "",
            reason=(
                f"دورهٔ فروش پرداخت {payment.id} از {payment.sales_period_jalali} به "
                f"{command.new_sales_period_jalali} اصلاح شد؛ {len(rederived)} قسط باز "
                "بازمشتق شد."
            ),
            suggested_action="بازبینی کنید؛ معمولاً اقدامی لازم نیست.",
        )
        return ReassignPeriodResult(
            payment=updated,
            rederived_tranche_ids=tuple(rederived),
            untouched_tranche_ids=tuple(untouched),
            exception=exception,
        )

    # -- write-off ----------------------------------------------------------

    def write_off_tranche(
        self,
        installation_id: str,
        command: RecordTrancheWriteOff,
        *,
        tenant_id: str,
        api_client_id: str | None,
    ) -> SettlementTranche:
        """Write off one tranche — human approval only (phased design §3.3).

        Requires a named approver and a reason, an eligible state, and
        writes one audit-log entry. Open exceptions for the tranche
        are resolved as ``write_off`` with the same approver/reason.
        (The explicit policy cap — Tryton Reconcile Write-off config —
        is US-522's; this story enforces approval + audit, the two
        gates the design names for the engine itself.)
        """
        if not command.approved_by.strip():
            raise SettlementWriteOffNotAllowedError(
                "A write-off requires a named approver (approved_by).",
                details={"tranche_id": command.tranche_id},
            )
        if not command.reason.strip():
            raise SettlementWriteOffNotAllowedError(
                "A write-off requires a reason.",
                details={"tranche_id": command.tranche_id},
            )
        tranche = self._repository.get_tranche(installation_id, command.tranche_id)
        if tranche is None:
            raise SettlementTrancheNotFoundError(
                f"Settlement tranche {command.tranche_id!r} does not exist.",
                details={"tranche_id": command.tranche_id},
            )
        if tranche.state not in WRITE_OFF_ELIGIBLE_STATES:
            raise SettlementWriteOffNotAllowedError(
                f"Tranche in state {tranche.state.value} cannot be written off.",
                details={"tranche_id": tranche.id, "state": tranche.state.value},
            )
        require_transition(tranche.state, TrancheState.WRITTEN_OFF)
        updated = self._repository.update_tranche(
            tranche.id,
            state=TrancheState.WRITTEN_OFF,
            variance_reason=(
                f"Written off by {command.approved_by}: {command.reason}"
            ),
        )
        assert updated is not None
        for exc in self._repository.list_exceptions(
            installation_id, tranche_id=tranche.id, status="open"
        ):
            self._repository.update_exception(
                exc.id,
                status=SettlementExceptionStatus.WRITTEN_OFF.value,
                resolution=SettlementExceptionResolution.WRITE_OFF.value,
                resolved_by=command.approved_by,
                resolved_at=datetime.datetime.now(datetime.UTC),
                note=command.reason,
            )
        self._audit.record_settlement_audit(
            tenant_id=tenant_id,
            installation_id=installation_id,
            api_client_id=api_client_id,
            action="settlement.tranche.write_off",
            resource_type="settlement_tranche",
            resource_id=tranche.id,
            actor=command.approved_by,
            metadata={
                "gateway_payment_id": tranche.gateway_payment_id,
                "amount_irr": tranche.outstanding_net_irr,
                "reason": command.reason,
                "previous_state": tranche.state.value,
            },
        )
        return updated

    # -- exceptions ---------------------------------------------------------

    def resolve_exception(
        self, installation_id: str, command: ResolveSettlementException
    ) -> SettlementException:
        """Resolve an open exception (resolver + time + note recorded)."""
        exc = self._repository.get_exception(installation_id, command.exception_id)
        if exc is None:
            raise SettlementExceptionNotFoundError(
                f"Settlement exception {command.exception_id!r} does not exist.",
                details={"exception_id": command.exception_id},
            )
        if exc.status not in (
            SettlementExceptionStatus.OPEN,
            SettlementExceptionStatus.IN_REVIEW,
        ):
            raise SettlementInvalidTransitionError(
                f"Settlement exception {exc.id} is already {exc.status.value}.",
                details={"exception_id": exc.id, "status": exc.status.value},
            )
        try:
            resolution = SettlementExceptionResolution(command.resolution)
        except ValueError:
            raise SettlementInvalidTransitionError(
                f"Unknown exception resolution {command.resolution!r}.",
                details={"resolution": command.resolution},
            ) from None
        if not command.resolved_by.strip():
            raise SettlementInvalidTransitionError(
                "Resolving an exception requires a named resolver (resolved_by).",
                details={"exception_id": exc.id},
            )
        updated = self._repository.update_exception(
            exc.id,
            status=SettlementExceptionStatus.RESOLVED.value,
            resolution=resolution.value,
            resolved_by=command.resolved_by,
            resolved_at=datetime.datetime.now(datetime.UTC),
            note=command.note,
        )
        assert updated is not None
        return updated

    # -- bulk re-derivation ---------------------------------------------------

    def bulk_rederive(
        self,
        installation_id: str,
        *,
        tenant_id: str,
        api_client_id: str | None,
        channel_key: str | None = None,
        sales_period_jalali: str | None = None,
        actor: str,
        reason: str,
    ) -> BulkRederiveResult:
        """Re-derive open schedules from the ACTIVE contract version.

        Explicit user action only (``actor`` + ``reason`` are
        mandatory) + one audit-log entry for the run (phased design
        §3.1). Only payments whose tranches are ALL still open are
        re-derived — a payment with any confirmed/received tranche
        keeps its schedule and is reported as skipped (mixed
        open/closed re-derivation needs the adjustment-row semantics
        of US-522; this story refuses to guess it).
        """
        if not actor.strip():
            raise SettlementPaymentInvalidError(
                "Bulk re-derivation requires a named actor.",
                details={"channel_key": channel_key},
            )
        if not reason.strip():
            raise SettlementPaymentInvalidError(
                "Bulk re-derivation requires a reason.",
                details={"channel_key": channel_key},
            )
        channel_id: str | None = None
        if channel_key is not None:
            channel = self._registry.get_channel(installation_id, channel_key)
            if channel is None:
                raise IntegrationChannelNotFoundError(
                    f"Payment channel {channel_key!r} is not in this installation's registry.",
                    details={"channel_key": channel_key},
                )
            channel_id = channel.id
        payments = self._repository.list_payments(
            installation_id,
            channel_id=channel_id,
            sales_period_jalali=sales_period_jalali,
        )
        calendar = self._calendar_for(installation_id)
        rederived: list[str] = []
        skipped: list[tuple[str, str]] = []
        for payment in payments:
            if payment.status in (
                GatewayPaymentStatus.CANCELLED,
                GatewayPaymentStatus.PARTIALLY_CANCELLED,
                GatewayPaymentStatus.SETTLED,
            ):
                skipped.append((payment.id, f"payment is {payment.status.value}"))
                continue
            tranches = self._repository.list_tranches_for_payment(
                installation_id, payment.id
            )
            if not all(t.is_open for t in tranches):
                skipped.append((payment.id, "has confirmed/received tranches"))
                continue
            contract = self._registry.get_active_contract(payment.payment_channel_id)
            if contract is None:
                skipped.append((payment.id, "channel has no active contract"))
                continue
            if contract.id == payment.contract_version_id:
                skipped.append((payment.id, "already on the active contract version"))
                continue
            fee_vat_rate = contract.fee_vat_rate
            if fee_vat_rate is None:
                skipped.append((payment.id, "active contract has no fee-VAT rate"))
                continue
            try:
                schedule = derive_tranches(
                    gross_irr=payment.gross_irr,
                    contract=contract,
                    gateway_confirmed_at=payment.gateway_confirmed_at,
                    calendar=calendar,
                    fee_vat_rate=fee_vat_rate,
                )
            except SettlementDerivationError as exc:
                skipped.append((payment.id, f"derivation failed: {exc.message}"))
                continue
            self._repository.delete_tranches([t.id for t in tranches])
            self._repository.save_tranches(installation_id, payment.id, schedule.tranches)
            self._repository.update_payment_amounts(
                payment.id,
                fee_irr=schedule.fee_irr,
                fee_vat_irr=schedule.fee_vat_irr,
                net_irr=schedule.net_irr,
                contract_version_id=contract.id,
                fee_rate_snapshot=contract.fee_percent,
                fee_vat_rate_snapshot=fee_vat_rate,
            )
            self._repository.save_fee_record(
                installation_id=installation_id,
                gateway_payment_id=payment.id,
                contract_version_id=contract.id,
                fee_percent_applied=contract.fee_percent,
                fee_vat_rate_applied=fee_vat_rate,
                expected_fee_irr=schedule.fee_irr,
                expected_vat_irr=schedule.fee_vat_irr,
                source="calculated",
            )
            rederived.append(payment.id)
        self._audit.record_settlement_audit(
            tenant_id=tenant_id,
            installation_id=installation_id,
            api_client_id=api_client_id,
            action="settlement.bulk_rederive",
            resource_type="settlement",
            resource_id=channel_id,
            actor=actor,
            metadata={
                "channel_key": channel_key,
                "sales_period_jalali": sales_period_jalali,
                "reason": reason,
                "rederived_payment_ids": rederived,
                "skipped": [{"payment_id": pid, "reason": why} for pid, why in skipped],
            },
        )
        return BulkRederiveResult(
            rederived_payment_ids=tuple(rederived), skipped=tuple(skipped)
        )

    # -- daily tick (W1) ------------------------------------------------------

    def run_daily_tick(self, today: datetime.date) -> TickReport:
        """Run the daily settlement tick (phased design §3.9, W1).

        Across ALL installations: ``expected → due`` at the due date;
        ``due → overdue`` past due + grace, opening (or escalating)
        a ``late_tranche`` exception at the 7/30/60-day thresholds;
        already-overdue tranches get their escalation level re-checked.
        """
        counters = _TickCounters()
        for tranche, grace_days in self._repository.find_tranches_due_for_tick(today):
            state = tranche.state
            if state == TrancheState.EXPECTED:
                require_transition(state, TrancheState.DUE)
                self._repository.update_tranche(tranche.id, state=TrancheState.DUE)
                counters.moved_to_due += 1
                state = TrancheState.DUE
            if state == TrancheState.DUE:
                overdue_since = tranche.expected_date + datetime.timedelta(days=grace_days)
                if today > overdue_since:
                    require_transition(state, TrancheState.OVERDUE)
                    self._repository.update_tranche(tranche.id, state=TrancheState.OVERDUE)
                    counters.moved_to_overdue += 1
                    state = TrancheState.OVERDUE
            if state == TrancheState.OVERDUE:
                self._escalate_late_tranche(tranche, today, counters)
        return TickReport(
            moved_to_due=counters.moved_to_due,
            moved_to_overdue=counters.moved_to_overdue,
            exceptions_created=counters.exceptions_created,
            exceptions_escalated=counters.exceptions_escalated,
            review_items_filed=counters.review_items_filed,
        )

    def _escalate_late_tranche(
        self,
        tranche: SettlementTranche,
        today: datetime.date,
        counters: _TickCounters,
    ) -> None:
        """Open or escalate the ``late_tranche`` exception for a tranche.

        A newly created exception is also filed as a
        ``settlement_variance`` review item (US-555) — never silent.
        Filing is idempotent per (tranche, exception): a re-run tick
        finds the open exception and files nothing; only a NEW
        exception (e.g. after the previous one was resolved) files a
        new item.
        """
        days_overdue = (today - tranche.expected_date).days
        level = escalation_level_for(days_overdue)
        existing = self._repository.get_open_exception_for_tranche(
            tranche.installation_id,
            tranche.id,
            SettlementExceptionType.LATE_TRANCHE.value,
        )
        if existing is None:
            exception = self._repository.save_exception(
                CreateSettlementException(
                    installation_id=tranche.installation_id,
                    exception_type=SettlementExceptionType.LATE_TRANCHE.value,
                    severity=(
                        SettlementExceptionSeverity.CRITICAL.value
                        if level >= 2
                        else SettlementExceptionSeverity.WARNING.value
                    ),
                    tranche_id=tranche.id,
                    gateway_payment_id=tranche.gateway_payment_id,
                    expected_amount_irr=tranche.net_irr,
                    actual_amount_irr=tranche.received_net_irr,
                    escalation_level=level,
                    note=(
                        f"Tranche {tranche.tranche_index}/{tranche.tranche_count} "
                        f"is {days_overdue} day(s) past its due date "
                        f"{tranche.expected_date.isoformat()} and unconfirmed."
                    ),
                )
            )
            counters.exceptions_created += 1
            if self._file_late_tranche_review_item(
                exception=exception, tranche=tranche, days_overdue=days_overdue
            ):
                counters.review_items_filed += 1
        elif level > existing.escalation_level:
            self._repository.update_exception(
                existing.id,
                escalation_level=level,
                note=(
                    f"Escalated to level {level}: tranche is {days_overdue} day(s) "
                    f"past its due date {tranche.expected_date.isoformat()}."
                ),
            )
            counters.exceptions_escalated += 1
