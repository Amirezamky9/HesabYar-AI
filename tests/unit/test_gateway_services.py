"""Service tests for gateway reconciliation (US-524).

DB-free: the reconciliation service runs against the shared fake
integrations registry, the fake settlement repository (from
``tests.unit.test_settlement_services``), a fake vault, and a fake
gateway-client factory with scripted clients. No HTTP anywhere.
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

import pytest

from hesabyar.application.commands.gateways import (
    GatewayReconciliationService,
    ReconcileOutcome,
)
from hesabyar.application.commands.integrations import (
    ChannelRegistryService,
    GatewayContractService,
)
from hesabyar.application.commands.settlement import SettlementCommandService
from hesabyar.application.errors import IntegrationChannelNotFoundError
from hesabyar.domain.gateways.errors import (
    GatewayNotConfiguredError,
    GatewayUnsupportedOperationError,
    GatewayVerificationMismatchError,
)
from hesabyar.domain.gateways.models import (
    GatewayCapabilities,
    GatewayInquiry,
    GatewayTransaction,
    GatewayTransactionStatus,
    RefundResult,
    SettlementSource,
    VerifiedPayment,
)
from hesabyar.domain.integrations.models import PaymentChannel, TrancheLeg
from hesabyar.domain.settlement.errors import (
    SettlementPaymentInvalidError,
    SettlementPaymentNotFoundError,
)
from hesabyar.domain.settlement.models import GatewayPaymentStatus, PaymentSource
from tests.unit.test_integrations_services import FakeIntegrationsRepository
from tests.unit.test_settlement_services import (
    FOUR_TRANCHES,
    FakeSettlementAudit,
    FakeSettlementRepository,
)

INSTALLATION_ID = "00000000-0000-0000-0000-000000000524"
NOW = datetime.datetime(2026, 11, 6, 12, 0, tzinfo=datetime.UTC)


class FakeVault:
    """Identity vault: ciphertext IS the plaintext (tests only)."""

    key_id = "test-key"

    def encrypt(self, plaintext: str) -> str:
        return plaintext

    def decrypt(self, ciphertext: str) -> str:
        return ciphertext


@dataclass
class FakeGatewayClient:
    """Scripted gateway client (records what the service asked)."""

    channel_key: str
    verified: VerifiedPayment | None = None
    inquiry: GatewayInquiry | None = None
    refund_result: RefundResult | None = None
    verify_error: Exception | None = None
    inquiry_error: Exception | None = None
    refund_error: Exception | None = None
    closed: bool = False
    verify_calls: list[str] = None  # type: ignore[assignment]
    inquiry_calls: list[str] = None  # type: ignore[assignment]
    refund_calls: list[tuple[str, int]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.verify_calls = []
        self.inquiry_calls = []
        self.refund_calls = []

    @property
    def capabilities(self) -> GatewayCapabilities:
        return GatewayCapabilities(
            channel_key=self.channel_key,
            has_payment_api=True,
            supports_verify=True,
            supports_inquiry=True,
            supports_refund=True,
            supports_list=False,
            settlement_source=SettlementSource.PORTAL_FILE,
        )

    def verify_payment(self, payment_ref: str, *, expected_amount_irr: int | None = None) -> VerifiedPayment:
        self.verify_calls.append(payment_ref)
        if self.verify_error is not None:
            raise self.verify_error
        assert self.verified is not None
        if expected_amount_irr is not None and self.verified.gross_irr != expected_amount_irr:
            raise GatewayVerificationMismatchError(
                "amount mismatch", details={"expected_amount_irr": expected_amount_irr}
            )
        return self.verified

    def inquire_status(self, payment_ref: str) -> GatewayInquiry:
        self.inquiry_calls.append(payment_ref)
        if self.inquiry_error is not None:
            raise self.inquiry_error
        assert self.inquiry is not None
        return self.inquiry

    def refund(self, payment_ref: str, amount_irr: int, *, reason: str | None = None) -> RefundResult:
        self.refund_calls.append((payment_ref, amount_irr))
        if self.refund_error is not None:
            raise self.refund_error
        assert self.refund_result is not None
        return self.refund_result

    def list_transactions(self, *, limit: int = 50, offset: int = 0) -> Sequence[GatewayTransaction]:
        raise GatewayUnsupportedOperationError("no list", details={})

    def close(self) -> None:
        self.closed = True


class FakeGatewayFactory:
    """Factory returning one scripted client per channel key."""

    def __init__(self) -> None:
        self.clients: dict[str, FakeGatewayClient] = {}
        self.built: list[str] = []

    def build(self, channel: PaymentChannel, credentials: Mapping[str, object]) -> FakeGatewayClient:
        self.built.append(channel.channel_key)
        client = self.clients.get(channel.channel_key)
        if client is None:
            raise GatewayNotConfiguredError(
                f"No fake client for {channel.channel_key!r}.",
                details={"channel_key": channel.channel_key},
            )
        return client

    def requires_credentials(self, channel: PaymentChannel) -> bool:
        return channel.channel_key in ("zarinpal", "snapppay", "torobpay", "digipay")

    def capabilities_for(self, channel: PaymentChannel) -> GatewayCapabilities:
        client = self.clients.get(channel.channel_key)
        if client is not None:
            return client.capabilities
        return GatewayCapabilities(
            channel_key=channel.channel_key,
            has_payment_api=False,
            supports_verify=False,
            supports_inquiry=False,
            supports_refund=False,
            supports_list=False,
            settlement_source=SettlementSource.MANUAL_IMPORT,
        )


@pytest.fixture()
def registry() -> FakeIntegrationsRepository:
    """Registry with an ENABLED snapppay channel + active contract."""
    repo = FakeIntegrationsRepository()
    channels = ChannelRegistryService(repo)
    contracts = GatewayContractService(repo)
    channels.create_channel(INSTALLATION_ID, "snapppay", "اسنپ‌پی", "bnpl")
    contracts.create_version(
        INSTALLATION_ID,
        "snapppay",
        valid_from=datetime.date(2026, 10, 23),
        fee_percent=Decimal("10"),
        fee_vat_rate=Decimal("10"),
        tranche_legs=FOUR_TRANCHES,
        payout_window_start_day=1,
        payout_window_end_day=5,
        grace_days_after_due=3,
    )
    channels.update_channel(INSTALLATION_ID, "snapppay", enabled=True)
    channels.create_channel(INSTALLATION_ID, "sadad", "سداد", "gateway")
    contracts.create_version(
        INSTALLATION_ID,
        "sadad",
        valid_from=datetime.date(2026, 10, 23),
        fee_percent=Decimal("0"),
        fee_vat_rate=Decimal("10"),
        settlement_model="daily",
        tranche_legs=(TrancheLeg(0, 5, Decimal("100")),),
        day_lag=1,
    )
    channels.update_channel(INSTALLATION_ID, "sadad", enabled=True)
    return repo


@pytest.fixture()
def factory() -> FakeGatewayFactory:
    return FakeGatewayFactory()


@pytest.fixture()
def service(registry: FakeIntegrationsRepository, factory: FakeGatewayFactory) -> GatewayReconciliationService:
    repo = FakeSettlementRepository()
    settlement = SettlementCommandService(repo, registry, FakeSettlementAudit())
    return GatewayReconciliationService(registry, repo, settlement, factory, FakeVault())


def _store_credentials(registry: FakeIntegrationsRepository, channel_key: str, payload: dict) -> None:
    channel = registry.get_channel(INSTALLATION_ID, channel_key)
    assert channel is not None
    registry.channel_ciphertexts[channel.id] = json.dumps(payload)


def _paid_verified(txn: str = "TXN-1", gross: int = 100_000_000) -> VerifiedPayment:
    return VerifiedPayment(
        gateway_transaction_id=txn,
        gross_irr=gross,
        gateway_confirmed_at=NOW,
        status=GatewayTransactionStatus.PAID,
        store_order_id="ORD-1",
        tracking_ref="REF-1",
    )


class TestCapabilities:
    def test_capabilities_need_no_credentials(
        self, service: GatewayReconciliationService, factory: FakeGatewayFactory
    ) -> None:
        factory.clients["snapppay"] = FakeGatewayClient("snapppay")
        caps = service.get_capabilities(INSTALLATION_ID, "snapppay")
        assert caps.channel_key == "snapppay"
        assert caps.has_payment_api is True

    def test_capabilities_unknown_channel_is_404(self, service: GatewayReconciliationService) -> None:
        with pytest.raises(IntegrationChannelNotFoundError):
            service.get_capabilities(INSTALLATION_ID, "nope")


class TestVerifyAndRecord:
    def test_verify_and_record_creates_payment(
        self,
        service: GatewayReconciliationService,
        registry: FakeIntegrationsRepository,
        factory: FakeGatewayFactory,
    ) -> None:
        _store_credentials(registry, "snapppay", {"client_id": "c"})
        client = FakeGatewayClient("snapppay", verified=_paid_verified())
        factory.clients["snapppay"] = client
        result = service.verify_and_record(INSTALLATION_ID, "snapppay", "tok-1", expected_amount_irr=100_000_000)
        assert result.created is True
        assert result.payment.gateway_transaction_id == "TXN-1"
        assert result.payment.gross_irr == 100_000_000
        assert result.payment.net_irr == 89_000_000  # 10% fee + 10% VAT on fee
        assert result.payment.source == PaymentSource.GATEWAY_POLL
        assert result.payment.sales_period_jalali == "1405/08"
        assert len(result.tranches) == 4
        assert client.closed is True

    def test_verify_and_record_is_idempotent(
        self,
        service: GatewayReconciliationService,
        registry: FakeIntegrationsRepository,
        factory: FakeGatewayFactory,
    ) -> None:
        _store_credentials(registry, "snapppay", {"client_id": "c"})
        factory.clients["snapppay"] = FakeGatewayClient("snapppay", verified=_paid_verified())
        first = service.verify_and_record(INSTALLATION_ID, "snapppay", "tok-1")
        second = service.verify_and_record(INSTALLATION_ID, "snapppay", "tok-1")
        assert first.created is True
        assert second.created is False
        assert second.payment.id == first.payment.id

    def test_verify_mismatch_records_nothing(
        self,
        service: GatewayReconciliationService,
        registry: FakeIntegrationsRepository,
        factory: FakeGatewayFactory,
    ) -> None:
        _store_credentials(registry, "snapppay", {"client_id": "c"})
        factory.clients["snapppay"] = FakeGatewayClient("snapppay", verified=_paid_verified())
        with pytest.raises(GatewayVerificationMismatchError):
            service.verify_and_record(INSTALLATION_ID, "snapppay", "tok-1", expected_amount_irr=1)

    def test_verify_without_credentials_is_not_configured(
        self,
        service: GatewayReconciliationService,
        factory: FakeGatewayFactory,
    ) -> None:
        factory.clients["snapppay"] = FakeGatewayClient("snapppay", verified=_paid_verified())
        with pytest.raises(GatewayNotConfiguredError, match="no stored gateway credentials"):
            service.verify_and_record(INSTALLATION_ID, "snapppay", "tok-1")

    def test_verify_disabled_channel_is_refused(
        self,
        service: GatewayReconciliationService,
        registry: FakeIntegrationsRepository,
        factory: FakeGatewayFactory,
    ) -> None:
        ChannelRegistryService(registry).update_channel(INSTALLATION_ID, "snapppay", enabled=False)
        factory.clients["snapppay"] = FakeGatewayClient("snapppay", verified=_paid_verified())
        with pytest.raises(GatewayNotConfiguredError, match="disabled"):
            service.verify_and_record(INSTALLATION_ID, "snapppay", "tok-1")

    def test_verify_unpaid_status_records_nothing(
        self,
        service: GatewayReconciliationService,
        registry: FakeIntegrationsRepository,
        factory: FakeGatewayFactory,
    ) -> None:
        _store_credentials(registry, "snapppay", {"client_id": "c"})
        pending = VerifiedPayment(
            gateway_transaction_id="TXN-1",
            gross_irr=100_000_000,
            gateway_confirmed_at=NOW,
            status=GatewayTransactionStatus.PENDING,
        )
        factory.clients["snapppay"] = FakeGatewayClient("snapppay", verified=pending)
        with pytest.raises(GatewayVerificationMismatchError, match="did not confirm"):
            service.verify_and_record(INSTALLATION_ID, "snapppay", "tok-1")


class TestRecordCallback:
    def test_store_verified_callback_creates_payment(self, service: GatewayReconciliationService) -> None:
        result = service.record_gateway_callback(
            INSTALLATION_ID,
            "sadad",
            gateway_transaction_id="SAD-1",
            gross_irr=50_000_000,
            gateway_confirmed_at=NOW,
            source=PaymentSource.STORE_WEBHOOK,
            store_order_id="ORD-7",
        )
        assert result.created is True
        assert result.payment.source == PaymentSource.STORE_WEBHOOK
        assert result.payment.store_order_id == "ORD-7"

    def test_callback_replay_returns_existing(self, service: GatewayReconciliationService) -> None:
        first = service.record_gateway_callback(
            INSTALLATION_ID,
            "sadad",
            gateway_transaction_id="SAD-1",
            gross_irr=50_000_000,
            gateway_confirmed_at=NOW,
            source=PaymentSource.MANUAL_ENTRY,
        )
        second = service.record_gateway_callback(
            INSTALLATION_ID,
            "sadad",
            gateway_transaction_id="SAD-1",
            gross_irr=50_000_000,
            gateway_confirmed_at=NOW,
            source=PaymentSource.MANUAL_ENTRY,
        )
        assert second.created is False
        assert second.payment.id == first.payment.id

    def test_callback_rejects_gateway_poll_source(self, service: GatewayReconciliationService) -> None:
        with pytest.raises(SettlementPaymentInvalidError):
            service.record_gateway_callback(
                INSTALLATION_ID,
                "sadad",
                gateway_transaction_id="SAD-1",
                gross_irr=50_000_000,
                gateway_confirmed_at=NOW,
                source=PaymentSource.GATEWAY_POLL,
            )


class TestReconcile:
    def _setup(
        self,
        registry: FakeIntegrationsRepository,
        factory: FakeGatewayFactory,
        inquiry: GatewayInquiry,
    ) -> FakeGatewayClient:
        _store_credentials(registry, "snapppay", {"client_id": "c"})
        client = FakeGatewayClient("snapppay", inquiry=inquiry)
        factory.clients["snapppay"] = client
        return client

    def test_reconcile_paid_records_payment(
        self,
        service: GatewayReconciliationService,
        registry: FakeIntegrationsRepository,
        factory: FakeGatewayFactory,
    ) -> None:
        self._setup(
            registry,
            factory,
            GatewayInquiry("TXN-1", GatewayTransactionStatus.PAID, amount_irr=100_000_000),
        )
        result = service.reconcile_transaction(INSTALLATION_ID, "snapppay", "TXN-1")
        assert result.outcome == ReconcileOutcome.RECORDED
        assert result.payment is not None
        assert result.payment.gross_irr == 100_000_000
        assert len(result.tranches) == 4

    def test_reconcile_paid_already_recorded(
        self,
        service: GatewayReconciliationService,
        registry: FakeIntegrationsRepository,
        factory: FakeGatewayFactory,
    ) -> None:
        self._setup(
            registry,
            factory,
            GatewayInquiry("TXN-1", GatewayTransactionStatus.PAID, amount_irr=100_000_000),
        )
        service.reconcile_transaction(INSTALLATION_ID, "snapppay", "TXN-1")
        result = service.reconcile_transaction(INSTALLATION_ID, "snapppay", "TXN-1")
        assert result.outcome == ReconcileOutcome.ALREADY_RECORDED

    def test_reconcile_paid_uses_expected_amount_when_gateway_reports_none(
        self,
        service: GatewayReconciliationService,
        registry: FakeIntegrationsRepository,
        factory: FakeGatewayFactory,
    ) -> None:
        self._setup(
            registry,
            factory,
            GatewayInquiry("TXN-1", GatewayTransactionStatus.PAID, amount_irr=None),
        )
        result = service.reconcile_transaction(INSTALLATION_ID, "snapppay", "TXN-1", expected_amount_irr=100_000_000)
        assert result.outcome == ReconcileOutcome.RECORDED
        assert result.payment is not None
        assert result.payment.gross_irr == 100_000_000

    def test_reconcile_cancelled_cancels_recorded_payment(
        self,
        service: GatewayReconciliationService,
        registry: FakeIntegrationsRepository,
        factory: FakeGatewayFactory,
    ) -> None:
        self._setup(
            registry,
            factory,
            GatewayInquiry("TXN-1", GatewayTransactionStatus.PAID, amount_irr=100_000_000),
        )
        service.reconcile_transaction(INSTALLATION_ID, "snapppay", "TXN-1")
        factory.clients["snapppay"].inquiry = GatewayInquiry("TXN-1", GatewayTransactionStatus.CANCELLED)
        result = service.reconcile_transaction(INSTALLATION_ID, "snapppay", "TXN-1")
        assert result.outcome == ReconcileOutcome.CANCELLED
        assert result.payment is not None
        assert result.payment.status == GatewayPaymentStatus.CANCELLED

    def test_reconcile_pending_writes_nothing(
        self,
        service: GatewayReconciliationService,
        registry: FakeIntegrationsRepository,
        factory: FakeGatewayFactory,
    ) -> None:
        self._setup(
            registry,
            factory,
            GatewayInquiry("TXN-1", GatewayTransactionStatus.PENDING),
        )
        result = service.reconcile_transaction(INSTALLATION_ID, "snapppay", "TXN-1")
        assert result.outcome == ReconcileOutcome.PENDING
        assert result.payment is None

    def test_reconcile_manual_channel_raises_unsupported(
        self,
        service: GatewayReconciliationService,
        factory: FakeGatewayFactory,
    ) -> None:
        client = FakeGatewayClient(
            "sadad",
            inquiry_error=GatewayUnsupportedOperationError("no api", details={}),
        )
        factory.clients["sadad"] = client
        with pytest.raises(GatewayUnsupportedOperationError):
            service.reconcile_transaction(INSTALLATION_ID, "sadad", "SAD-1")


class TestRefund:
    def test_full_refund_cancels_payment(
        self,
        service: GatewayReconciliationService,
        registry: FakeIntegrationsRepository,
        factory: FakeGatewayFactory,
    ) -> None:
        _store_credentials(registry, "snapppay", {"client_id": "c"})
        client = FakeGatewayClient(
            "snapppay",
            verified=_paid_verified(),
            refund_result=RefundResult(
                refund_id="REF-9", amount_irr=100_000_000, status=GatewayTransactionStatus.REFUNDED
            ),
        )
        factory.clients["snapppay"] = client
        recorded = service.verify_and_record(INSTALLATION_ID, "snapppay", "tok-1")
        result = service.refund_payment(
            INSTALLATION_ID, "snapppay", recorded.payment.id, 100_000_000, reason="customer return"
        )
        assert result.refund.refund_id == "REF-9"
        assert result.payment.status == GatewayPaymentStatus.CANCELLED
        assert len(result.cancelled_tranche_ids) == 4
        assert client.refund_calls == [("TXN-1", 100_000_000)]

    def test_partial_refund_is_refused(
        self,
        service: GatewayReconciliationService,
        registry: FakeIntegrationsRepository,
        factory: FakeGatewayFactory,
    ) -> None:
        _store_credentials(registry, "snapppay", {"client_id": "c"})
        factory.clients["snapppay"] = FakeGatewayClient("snapppay", verified=_paid_verified())
        recorded = service.verify_and_record(INSTALLATION_ID, "snapppay", "tok-1")
        with pytest.raises(SettlementPaymentInvalidError, match="full refunds"):
            service.refund_payment(INSTALLATION_ID, "snapppay", recorded.payment.id, 1_000_000)

    def test_refund_gateway_failure_leaves_row_untouched(
        self,
        service: GatewayReconciliationService,
        registry: FakeIntegrationsRepository,
        factory: FakeGatewayFactory,
    ) -> None:
        _store_credentials(registry, "snapppay", {"client_id": "c"})
        client = FakeGatewayClient(
            "snapppay",
            verified=_paid_verified(),
            refund_error=GatewayVerificationMismatchError("nope", details={}),
        )
        factory.clients["snapppay"] = client
        recorded = service.verify_and_record(INSTALLATION_ID, "snapppay", "tok-1")
        with pytest.raises(GatewayVerificationMismatchError):
            service.refund_payment(INSTALLATION_ID, "snapppay", recorded.payment.id, 100_000_000)
        assert recorded.payment.status == GatewayPaymentStatus.SCHEDULED

    def test_refund_unknown_payment_is_404(self, service: GatewayReconciliationService) -> None:
        with pytest.raises(SettlementPaymentNotFoundError):
            service.refund_payment(INSTALLATION_ID, "snapppay", "missing", 100)

    def test_refund_wrong_channel_is_404(
        self,
        service: GatewayReconciliationService,
        registry: FakeIntegrationsRepository,
        factory: FakeGatewayFactory,
    ) -> None:
        _store_credentials(registry, "snapppay", {"client_id": "c"})
        factory.clients["snapppay"] = FakeGatewayClient("snapppay", verified=_paid_verified())
        recorded = service.verify_and_record(INSTALLATION_ID, "snapppay", "tok-1")
        with pytest.raises(SettlementPaymentNotFoundError):
            service.refund_payment(INSTALLATION_ID, "sadad", recorded.payment.id, 100_000_000)
