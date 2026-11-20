"""Gateway reconciliation service (US-524, Phase 5-3 Wave B).

Orchestrates status reconciliation between the gateway clients and
the settlement engine: verify a payment with its gateway and record
it, re-inquire a payment's status and converge the recorded row,
record a store-verified callback, and refund through the gateway.

The service never invents a fee % or a settlement schedule — both
come from the channel's ACTIVE contract version via
``SettlementCommandService.record_payment`` (US-521). It never
touches the engine (Tryton): settlement accounting — posting the
receivable against the GATEWAY party, never the customer — is
US-522's job. The BNPL rule is structural here: the payment row is
recorded under the gateway's channel, so the receivable US-522
posts belongs to the gateway; ``customer_party_id`` on the payment
names the customer for the SALE only.

Fail-closed throughout: a disabled channel reconciles nothing; a
channel without stored credentials cannot verify; a gateway answer
that disagrees with the expected amount records nothing; a manual
channel (Sadad, SalamPay, custom) verifies nothing through an API
— its payments arrive via the store capture or a manual import
(``record_gateway_callback`` with source ``manual_entry`` /
``store_webhook``).

Idempotency: webhook + poll + manual import of the same
transaction converge on ONE ``gateway_payment`` row (phased design
§7.1) — recording an already-recorded transaction returns the
existing payment instead of raising.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from hesabyar.application.commands.settlement import SettlementCommandService
from hesabyar.application.errors import (
    IntegrationChannelNotFoundError,
    IntegrationCredentialVaultUnavailableError,
)
from hesabyar.domain.gateways.credentials import parse_gateway_credentials
from hesabyar.domain.gateways.errors import (
    GatewayNotConfiguredError,
    GatewayRejectedError,
    GatewayVerificationMismatchError,
)
from hesabyar.domain.gateways.models import (
    GatewayCapabilities,
    GatewayInquiry,
    GatewayTransactionStatus,
    RefundResult,
    VerifiedPayment,
)
from hesabyar.domain.gateways.ports import GatewayClientFactoryPort
from hesabyar.domain.integrations.models import PaymentChannel
from hesabyar.domain.integrations.ports import (
    CredentialVaultPort,
    IntegrationsRegistryPort,
)
from hesabyar.domain.settlement.commands import (
    CancelGatewayPayment,
    CreateGatewayPayment,
)
from hesabyar.domain.settlement.errors import (
    SettlementPaymentExistsError,
    SettlementPaymentInvalidError,
    SettlementPaymentNotFoundError,
)
from hesabyar.domain.settlement.models import (
    GatewayPayment,
    GatewayPaymentStatus,
    PaymentSource,
    SettlementTranche,
)
from hesabyar.domain.settlement.ports import SettlementRepositoryPort

__all__ = [
    "GatewayReconciliationService",
    "ReconcileOutcome",
    "ReconcileResult",
    "RefundPaymentResult",
    "VerifiedCallbackResult",
]


@dataclass(frozen=True)
class VerifiedCallbackResult:
    """A recorded (or already-recorded) gateway-verified payment."""

    payment: GatewayPayment
    tranches: tuple[SettlementTranche, ...]
    created: bool
    """False when the payment already existed (idempotent replay)."""


@dataclass(frozen=True)
class ReconcileResult:
    """One reconciliation run: what the gateway said, what changed."""

    outcome: str
    inquiry: GatewayInquiry
    payment: GatewayPayment | None = None
    tranches: tuple[SettlementTranche, ...] = ()


@dataclass(frozen=True)
class RefundPaymentResult:
    """A gateway refund + the settlement cancel that followed it."""

    refund: RefundResult
    payment: GatewayPayment
    cancelled_tranche_ids: tuple[str, ...]


class ReconcileOutcome:
    """Outcome labels for :class:`ReconcileResult` (stable strings)."""

    RECORDED = "recorded"
    ALREADY_RECORDED = "already_recorded"
    CANCELLED = "cancelled"
    ALREADY_CANCELLED = "already_cancelled"
    PENDING = "pending"
    FAILED = "failed"
    UNKNOWN = "unknown"


class GatewayReconciliationService:
    """Reconcile gateway payment status into the settlement engine."""

    def __init__(
        self,
        registry: IntegrationsRegistryPort,
        settlement_repository: SettlementRepositoryPort,
        settlement_service: SettlementCommandService,
        factory: GatewayClientFactoryPort,
        vault: CredentialVaultPort | None,
    ) -> None:
        self._registry = registry
        self._settlement_repository = settlement_repository
        self._settlement = settlement_service
        self._factory = factory
        self._vault = vault

    # -- helpers ----------------------------------------------------------

    def _require_channel(self, installation_id: str, channel_key: str) -> PaymentChannel:
        """Resolve a channel or 404 (a row of another installation looks absent)."""
        channel = self._registry.get_channel(installation_id, channel_key)
        if channel is None:
            raise IntegrationChannelNotFoundError(
                f"Payment channel {channel_key!r} is not in this installation's registry.",
                details={"channel_key": channel_key},
            )
        return channel

    def _require_enabled_channel(self, installation_id: str, channel_key: str) -> PaymentChannel:
        """Resolve a channel and require it enabled (fail-closed writes)."""
        channel = self._require_channel(installation_id, channel_key)
        if not channel.enabled:
            raise GatewayNotConfiguredError(
                f"Payment channel {channel_key!r} is disabled; enable it (with an active contract) before reconciling.",
                details={"channel_key": channel_key},
            )
        return channel

    def _build_client(self, channel: PaymentChannel):
        """Build the gateway client from the channel's stored credentials.

        Marketplace channels are refused by the factory; manual
        channels need no credentials and get the manual client.
        """
        if not self._factory.requires_credentials(channel):
            return self._factory.build(channel, {})
        ciphertext = self._registry.get_channel_ciphertext(channel.id)
        if ciphertext is None:
            raise GatewayNotConfiguredError(
                f"Payment channel {channel.channel_key!r} has no stored gateway "
                "credentials; rotate them in the channel registry first.",
                details={"channel_key": channel.channel_key},
            )
        if self._vault is None:
            raise IntegrationCredentialVaultUnavailableError(
                "Cannot decrypt gateway credentials: no credential-encryption "
                "key is configured (HESABYAR_AUTH__CREDENTIALS_ENCRYPTION_KEY).",
            )
        try:
            plaintext = self._vault.decrypt(ciphertext)
        except ValueError as exc:
            raise GatewayNotConfiguredError(
                f"Payment channel {channel.channel_key!r} has stored credentials "
                "this vault cannot decrypt; rotate them.",
                details={"channel_key": channel.channel_key},
            ) from exc
        credentials = parse_gateway_credentials(plaintext, channel_key=channel.channel_key)
        return self._factory.build(channel, credentials)

    def _record_verified(
        self,
        installation_id: str,
        channel: PaymentChannel,
        verified: VerifiedPayment,
        *,
        source: PaymentSource,
        store_order_id: str | None,
        store_payment_id: str | None,
    ) -> VerifiedCallbackResult:
        """Record a verified payment, or return the existing row."""
        existing = self._settlement_repository.get_payment_by_transaction(
            installation_id, channel.id, verified.gateway_transaction_id
        )
        if existing is not None:
            tranches = self._settlement_repository.list_tranches_for_payment(installation_id, existing.id)
            return VerifiedCallbackResult(payment=existing, tranches=tuple(tranches), created=False)
        try:
            result = self._settlement.record_payment(
                CreateGatewayPayment(
                    installation_id=installation_id,
                    channel_key=channel.channel_key,
                    gateway_transaction_id=verified.gateway_transaction_id,
                    gross_irr=verified.gross_irr,
                    gateway_confirmed_at=verified.gateway_confirmed_at,
                    source=source,
                    store_order_id=store_order_id or verified.store_order_id,
                    store_payment_id=store_payment_id,
                )
            )
        except SettlementPaymentExistsError:
            # Raced with another recording of the same transaction —
            # converge on the existing row instead of failing.
            existing = self._settlement_repository.get_payment_by_transaction(
                installation_id, channel.id, verified.gateway_transaction_id
            )
            if existing is None:  # pragma: no cover - defensive
                raise
            tranches = self._settlement_repository.list_tranches_for_payment(installation_id, existing.id)
            return VerifiedCallbackResult(payment=existing, tranches=tuple(tranches), created=False)
        return VerifiedCallbackResult(payment=result.payment, tranches=result.tranches, created=True)

    # -- capabilities -------------------------------------------------------

    def get_capabilities(self, installation_id: str, channel_key: str) -> GatewayCapabilities:
        """Return what a channel can and cannot do (no credentials needed)."""
        channel = self._require_channel(installation_id, channel_key)
        return self._factory.capabilities_for(channel)

    # -- gateway-verified callbacks ------------------------------------------

    def verify_and_record(
        self,
        installation_id: str,
        channel_key: str,
        payment_ref: str,
        *,
        expected_amount_irr: int | None = None,
        store_order_id: str | None = None,
        store_payment_id: str | None = None,
    ) -> VerifiedCallbackResult:
        """Verify a payment with the gateway, then record it.

        ``payment_ref`` is the gateway-side reference (ZarinPal
        authority, SnappPay/TorobPay payment token, DigiPay
        providerId). The gateway is the authority for the amount:
        when ``expected_amount_irr`` is given, a mismatch records
        nothing. Source is ``gateway_poll`` — the proof came from
        the gateway, not from the store's claim.
        """
        channel = self._require_enabled_channel(installation_id, channel_key)
        client = self._build_client(channel)
        try:
            verified = client.verify_payment(payment_ref, expected_amount_irr=expected_amount_irr)
        finally:
            client.close()
        if verified.status != GatewayTransactionStatus.PAID:
            raise GatewayVerificationMismatchError(
                f"The gateway did not confirm {payment_ref!r} as paid "
                f"(status {verified.status.value}); nothing was recorded.",
                details={
                    "channel_key": channel_key,
                    "payment_ref": payment_ref,
                    "status": verified.status.value,
                },
            )
        return self._record_verified(
            installation_id,
            channel,
            verified,
            source=PaymentSource.GATEWAY_POLL,
            store_order_id=store_order_id,
            store_payment_id=store_payment_id,
        )

    def record_gateway_callback(
        self,
        installation_id: str,
        channel_key: str,
        *,
        gateway_transaction_id: str,
        gross_irr: int,
        gateway_confirmed_at: datetime.datetime,
        source: PaymentSource,
        store_order_id: str | None = None,
        store_payment_id: str | None = None,
        tracking_ref: str | None = None,
    ) -> VerifiedCallbackResult:
        """Record a callback the STORE already verified with the gateway.

        The store ran the gateway's verify (or the payment arrived
        through a manual capture) and forwards the result: HesabYar
        records it under the gateway's channel. Idempotent — a
        replay returns the existing payment. ``tracking_ref`` is
        informational (the gateway's reference number); identity is
        ``(channel, gateway_transaction_id)``.
        """
        if source not in (
            PaymentSource.STORE_WEBHOOK,
            PaymentSource.MANUAL_ENTRY,
            PaymentSource.MANUAL_IMPORT,
        ):
            raise SettlementPaymentInvalidError(
                f"record_gateway_callback accepts store_webhook / manual_entry / "
                f"manual_import, not {source.value} (gateway_poll goes through "
                "verify_and_record).",
                details={"source": source.value},
            )
        channel = self._require_enabled_channel(installation_id, channel_key)
        verified = VerifiedPayment(
            gateway_transaction_id=gateway_transaction_id,
            gross_irr=gross_irr,
            gateway_confirmed_at=gateway_confirmed_at,
            status=GatewayTransactionStatus.PAID,
            store_order_id=store_order_id,
            tracking_ref=tracking_ref,
        )
        return self._record_verified(
            installation_id,
            channel,
            verified,
            source=source,
            store_order_id=store_order_id,
            store_payment_id=store_payment_id,
        )

    # -- status reconciliation -----------------------------------------------

    def reconcile_transaction(
        self,
        installation_id: str,
        channel_key: str,
        gateway_transaction_id: str,
        *,
        payment_ref: str | None = None,
        expected_amount_irr: int | None = None,
        store_order_id: str | None = None,
    ) -> ReconcileResult:
        """Inquire one payment's status and converge the recorded row.

        - gateway says PAID + no row → record it (source
          ``gateway_poll``); the amount comes from the gateway's
          answer, falling back to ``expected_amount_irr``.
        - gateway says PAID + row exists → ``already_recorded``.
        - gateway says CANCELLED/REVERTED + an open row exists →
          cancel the row (the settlement engine handles already-paid
          tranches as a negative adjustment).
        - PENDING / FAILED / UNKNOWN → no write; the outcome names
          the status so the operator can retry later.
        """
        channel = self._require_enabled_channel(installation_id, channel_key)
        client = self._build_client(channel)
        try:
            inquiry = client.inquire_status(payment_ref or gateway_transaction_id)
        finally:
            client.close()
        transaction_id = inquiry.gateway_transaction_id or gateway_transaction_id
        existing = self._settlement_repository.get_payment_by_transaction(installation_id, channel.id, transaction_id)
        if inquiry.status == GatewayTransactionStatus.PAID:
            if existing is not None:
                tranches = self._settlement_repository.list_tranches_for_payment(installation_id, existing.id)
                return ReconcileResult(
                    outcome=ReconcileOutcome.ALREADY_RECORDED,
                    inquiry=inquiry,
                    payment=existing,
                    tranches=tuple(tranches),
                )
            amount = inquiry.amount_irr
            if amount is None:
                amount = expected_amount_irr
            if amount is None:
                raise GatewayVerificationMismatchError(
                    f"The gateway confirmed {transaction_id!r} as paid but "
                    "reported no amount, and no expected amount was given; "
                    "nothing was recorded.",
                    details={
                        "channel_key": channel_key,
                        "gateway_transaction_id": transaction_id,
                    },
                )
            if (
                expected_amount_irr is not None
                and inquiry.amount_irr is not None
                and inquiry.amount_irr != expected_amount_irr
            ):
                raise GatewayVerificationMismatchError(
                    f"The gateway reports {transaction_id!r} as {inquiry.amount_irr} "
                    f"IRR, but {expected_amount_irr} IRR was expected.",
                    details={
                        "channel_key": channel_key,
                        "gateway_transaction_id": transaction_id,
                        "expected_amount_irr": expected_amount_irr,
                        "actual_amount_irr": inquiry.amount_irr,
                    },
                )
            # Inquiries carry no confirmation timestamp — the
            # reconciliation time stands in (the exact time refines
            # from the transaction list where one exists).
            verified = VerifiedPayment(
                gateway_transaction_id=transaction_id,
                gross_irr=amount,
                gateway_confirmed_at=datetime.datetime.now(datetime.UTC),
                status=GatewayTransactionStatus.PAID,
                store_order_id=store_order_id,
            )
            recorded = self._record_verified(
                installation_id,
                channel,
                verified,
                source=PaymentSource.GATEWAY_POLL,
                store_order_id=store_order_id,
                store_payment_id=None,
            )
            return ReconcileResult(
                outcome=(ReconcileOutcome.RECORDED if recorded.created else ReconcileOutcome.ALREADY_RECORDED),
                inquiry=inquiry,
                payment=recorded.payment,
                tranches=recorded.tranches,
            )
        if inquiry.status in (
            GatewayTransactionStatus.CANCELLED,
            GatewayTransactionStatus.REVERTED,
        ):
            if existing is None:
                return ReconcileResult(outcome=ReconcileOutcome.FAILED, inquiry=inquiry)
            if existing.status in (
                GatewayPaymentStatus.CANCELLED,
                GatewayPaymentStatus.PARTIALLY_CANCELLED,
            ):
                return ReconcileResult(
                    outcome=ReconcileOutcome.ALREADY_CANCELLED,
                    inquiry=inquiry,
                    payment=existing,
                )
            cancelled = self._settlement.cancel_payment(
                installation_id,
                CancelGatewayPayment(
                    payment_id=existing.id,
                    reason=(
                        f"Gateway reports the payment {inquiry.status.value} (reconciliation of {transaction_id!r})."
                    ),
                ),
            )
            return ReconcileResult(
                outcome=ReconcileOutcome.CANCELLED,
                inquiry=inquiry,
                payment=cancelled.payment,
            )
        if inquiry.status == GatewayTransactionStatus.PENDING:
            return ReconcileResult(outcome=ReconcileOutcome.PENDING, inquiry=inquiry, payment=existing)
        if inquiry.status == GatewayTransactionStatus.FAILED:
            return ReconcileResult(outcome=ReconcileOutcome.FAILED, inquiry=inquiry, payment=existing)
        return ReconcileResult(outcome=ReconcileOutcome.UNKNOWN, inquiry=inquiry, payment=existing)

    # -- refunds ---------------------------------------------------------------

    def refund_payment(
        self,
        installation_id: str,
        channel_key: str,
        payment_id: str,
        amount_irr: int,
        *,
        reason: str | None = None,
    ) -> RefundPaymentResult:
        """Refund a payment through its gateway, then cancel the row.

        FULL refunds only: the settlement engine's cancel flow
        cancels every open tranche, and partial-refund tranche
        semantics do not exist yet (US-522 territory). A partial
        amount is refused rather than recorded wrong. The gateway
        refund runs FIRST — if it fails, the payment row is
        untouched. The payment must belong to ``channel_key``.
        """
        payment = self._settlement_repository.get_payment(installation_id, payment_id)
        if payment is None:
            raise SettlementPaymentNotFoundError(
                f"Gateway payment {payment_id!r} does not exist.",
                details={"payment_id": payment_id},
            )
        if amount_irr != payment.gross_irr:
            raise SettlementPaymentInvalidError(
                f"Only full refunds are supported (payment gross is "
                f"{payment.gross_irr} IRR, got {amount_irr} IRR); partial "
                "refunds need tranche-level adjustment semantics.",
                details={
                    "payment_id": payment_id,
                    "gross_irr": payment.gross_irr,
                    "amount_irr": amount_irr,
                },
            )
        channel = self._registry.get_channel_by_id(payment.payment_channel_id)
        if channel is None:  # pragma: no cover - the channel outlives payments
            raise IntegrationChannelNotFoundError(
                f"The channel of payment {payment_id!r} no longer exists.",
                details={"payment_id": payment_id},
            )
        if channel.channel_key != channel_key:
            raise SettlementPaymentNotFoundError(
                f"Gateway payment {payment_id!r} does not belong to channel {channel_key!r}.",
                details={"payment_id": payment_id, "channel_key": channel_key},
            )
        if not channel.enabled:
            raise GatewayNotConfiguredError(
                f"Payment channel {channel.channel_key!r} is disabled; refunds need an enabled channel.",
                details={"channel_key": channel.channel_key},
            )
        client = self._build_client(channel)
        try:
            refund = client.refund(payment.gateway_transaction_id, amount_irr, reason=reason)
        finally:
            client.close()
        if refund.status == GatewayTransactionStatus.FAILED:
            raise GatewayRejectedError(
                f"The gateway refused the refund of payment {payment_id!r}.",
                details={"payment_id": payment_id, "channel_key": channel.channel_key},
            )
        cancelled = self._settlement.cancel_payment(
            installation_id,
            CancelGatewayPayment(
                payment_id=payment.id,
                reason=reason or f"Refunded through the gateway ({refund.refund_id}).",
            ),
        )
        return RefundPaymentResult(
            refund=refund,
            payment=cancelled.payment,
            cancelled_tranche_ids=cancelled.cancelled_tranche_ids,
        )
