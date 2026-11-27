"""Settlement tie-out over a large statement (US-702, spec §17 phase 7).

DB-free and engine-free: :meth:`SettlementPostingService.compute_tie_out`
runs against the in-memory settlement fake (``PostingFakeRepository``)
and the fake integrations registry, exactly like
``tests/unit/test_settlement_posting.py`` — only the statement is
large (500 payments x 4 tranches = 2,000 matches) instead of one sale.

The assertions are exact figures, never timings: confirmed total,
deposits total, difference and the pass/fail verdict must be exactly
right at scale, including the two edge legs (a twice-matched tranche
counts once toward confirmed but both deposits count; documented
deductions offset the difference). The elapsed time is recorded for
``docs/performance-baselines.md`` and bounded only by a generous
safety net against a pathological slowdown.
"""

from __future__ import annotations

import datetime
import time
import uuid
from decimal import Decimal
from typing import cast

from hesabyar.application.commands.integrations import (
    ChannelRegistryService,
    GatewayContractService,
)
from hesabyar.application.commands.settlement_posting import (
    SettlementPostingService,
)
from hesabyar.domain.accounting.ports import AccountingEnginePort
from hesabyar.domain.settlement.models import (
    BankDeposit,
    GatewayPayment,
    GatewayPaymentStatus,
    PaymentSource,
    SettlementEvent,
    SettlementMatch,
    SettlementTranche,
    TrancheState,
)
from tests.load_support import LatencyRecorder
from tests.unit.test_integrations_services import FakeIntegrationsRepository
from tests.unit.test_settlement_posting import FakeEngine, PostingFakeRepository
from tests.unit.test_settlement_services import FOUR_TRANCHES, INSTALLATION_ID, NOW

#: A monthly statement at scale: 500 gateway payments x 4 tranches.
PAYMENT_COUNT = 500
TRANCHES_PER_PAYMENT = 4
TOTAL_TRANCHES = PAYMENT_COUNT * TRANCHES_PER_PAYMENT

#: Safety net only (in-memory tie-out runs in milliseconds): fail the
#: test if the computation ever takes this long, so a pathological
#: slowdown cannot hide as a green run. NOT a performance claim.
TIE_OUT_SAFETY_NET_SECONDS = 60.0

PAYOUT_DATE = datetime.date(2026, 11, 26)


def _registry() -> tuple[FakeIntegrationsRepository, str]:
    """A registry with one SnappPay channel + contract (zero tolerance)."""
    registry = FakeIntegrationsRepository()
    ChannelRegistryService(registry).create_channel(
        INSTALLATION_ID, "snapppay", "اسنپ‌پی", "bnpl"
    )
    contract = GatewayContractService(registry).create_version(
        INSTALLATION_ID,
        "snapppay",
        valid_from=datetime.date(2026, 10, 23),
        fee_percent=Decimal("10"),
        fee_vat_rate=Decimal("10"),
        tranche_legs=FOUR_TRANCHES,
        payout_window_start_day=1,
        payout_window_end_day=5,
        grace_days_after_due=3,
        tieout_tolerance_irr=0,
    )
    return registry, contract.id


def _service(
    repo: PostingFakeRepository, registry: FakeIntegrationsRepository
) -> SettlementPostingService:
    # compute_tie_out never touches the engine; the fake is cast like
    # the existing posting tests do.
    return SettlementPostingService(
        repo, registry, cast(AccountingEnginePort, FakeEngine())
    )


def _tranche_net(payment_index: int, tranche_index: int) -> int:
    """A varying whole-Rial net per tranche (no two tranches alike)."""
    return 100_000 + (payment_index * TRANCHES_PER_PAYMENT + tranche_index) * 1_000


def _seed_large_statement(
    repo: PostingFakeRepository,
    contract_version_id: str,
    *,
    deductions_irr: int = 0,
    shortfall_tranche: tuple[int, int] | None = None,
    shortfall_irr: int = 0,
) -> tuple[GatewayPayment, str, int, int]:
    """Seed 500 payments x 4 tranches + one event + one deposit per tranche.

    Returns (one payment for the tie-out call, the event id, the
    expected confirmed total, the expected deposits total).
    ``shortfall_tranche`` (payment index, tranche index) under-pays
    one tranche's deposit by ``shortfall_irr``.
    """
    expected_confirmed = 0
    expected_deposits = 0
    first_payment: GatewayPayment | None = None
    event_id = str(uuid.uuid4())
    for payment_index in range(PAYMENT_COUNT):
        payment_net = sum(
            _tranche_net(payment_index, tranche_index)
            for tranche_index in range(TRANCHES_PER_PAYMENT)
        )
        payment = GatewayPayment(
            id=str(uuid.uuid4()),
            installation_id=INSTALLATION_ID,
            payment_channel_id="snapppay",
            gateway_transaction_id=f"TXN-702-{payment_index}",
            gross_irr=payment_net,
            fee_irr=0,
            fee_vat_irr=0,
            net_irr=payment_net,
            contract_version_id=contract_version_id,
            fee_rate_snapshot=Decimal("10"),
            sales_period_jalali="1405-08",
            gateway_confirmed_at=NOW,
            source=PaymentSource.STORE_WEBHOOK,
            status=GatewayPaymentStatus.SETTLING,
        )
        repo.payments[payment.id] = payment
        if first_payment is None:
            first_payment = payment
        for tranche_index in range(TRANCHES_PER_PAYMENT):
            net = _tranche_net(payment_index, tranche_index)
            payment_net += net
            tranche = SettlementTranche(
                id=str(uuid.uuid4()),
                installation_id=INSTALLATION_ID,
                gateway_payment_id=payment.id,
                tranche_index=tranche_index + 1,
                tranche_count=TRANCHES_PER_PAYMENT,
                sales_period_jalali="1405-08",
                expected_window_start=datetime.date(2026, 11, 1),
                expected_date=PAYOUT_DATE,
                gross_irr=net,
                fee_irr=0,
                fee_vat_irr=0,
                net_irr=net,
                state=TrancheState.CONFIRMED,
                confirmed_net_irr=net,
            )
            repo.tranches[tranche.id] = tranche
            expected_confirmed += net
            allocated = net
            if shortfall_tranche == (payment_index, tranche_index):
                allocated -= shortfall_irr
            deposit = BankDeposit(
                id=str(uuid.uuid4()),
                installation_id=INSTALLATION_ID,
                deposit_date=PAYOUT_DATE,
                amount_irr=allocated,
            )
            repo.deposits[deposit.id] = deposit
            match = SettlementMatch(
                id=str(uuid.uuid4()),
                installation_id=INSTALLATION_ID,
                tranche_id=tranche.id,
                allocated_amount_irr=allocated,
                method="exact",
                bank_deposit_id=deposit.id,
                settlement_event_id=event_id,
            )
            repo.matches[match.id] = match
            expected_deposits += allocated
    repo.events[event_id] = SettlementEvent(
        id=event_id,
        installation_id=INSTALLATION_ID,
        payment_channel_id="snapppay",
        payout_date=PAYOUT_DATE,
        net_irr=expected_confirmed,
        source="file",
        deductions_irr=deductions_irr,
    )
    assert first_payment is not None
    return first_payment, event_id, expected_confirmed, expected_deposits


def test_tie_out_over_large_statement_is_exact() -> None:
    """2,000 matched tranches: confirmed == deposits, difference 0, passes."""
    registry, contract_version_id = _registry()
    repo = PostingFakeRepository()
    service = _service(repo, registry)
    payment, event_id, expected_confirmed, expected_deposits = _seed_large_statement(
        repo, contract_version_id
    )
    assert len(repo.tranches) == TOTAL_TRANCHES
    assert len(repo.matches) == TOTAL_TRANCHES

    recorder = LatencyRecorder()
    started = time.monotonic()
    figures = service.compute_tie_out(INSTALLATION_ID, event_id, payment)
    elapsed = time.monotonic() - started
    recorder.record("settlement.tie_out.2000_tranches", elapsed)

    assert figures.confirmed_net_irr == expected_confirmed
    assert figures.matched_deposits_irr == expected_deposits
    assert figures.deductions_irr == 0
    assert figures.difference_irr == 0
    assert figures.tolerance_irr == 0
    assert figures.passes is True
    assert elapsed < TIE_OUT_SAFETY_NET_SECONDS
    print(recorder.report())
    recorder.maybe_write_json()


def test_tie_out_over_large_statement_detects_one_short_tranche() -> None:
    """One tranche under-paid by 50,000 IRR: difference is exactly that, fails."""
    registry, contract_version_id = _registry()
    repo = PostingFakeRepository()
    service = _service(repo, registry)
    payment, event_id, expected_confirmed, expected_deposits = _seed_large_statement(
        repo,
        contract_version_id,
        shortfall_tranche=(499, 3),
        shortfall_irr=50_000,
    )
    figures = service.compute_tie_out(INSTALLATION_ID, event_id, payment)
    assert figures.confirmed_net_irr == expected_confirmed
    assert figures.matched_deposits_irr == expected_deposits
    assert figures.difference_irr == 50_000
    assert figures.passes is False


def test_tie_out_over_large_statement_with_documented_deductions() -> None:
    """Documented deductions of exactly the deposit gap: difference 0, passes."""
    registry, contract_version_id = _registry()
    repo = PostingFakeRepository()
    service = _service(repo, registry)
    deductions = 1_000_000
    payment, event_id, expected_confirmed, expected_deposits = _seed_large_statement(
        repo,
        contract_version_id,
        deductions_irr=deductions,
        shortfall_tranche=(0, 0),
        shortfall_irr=deductions,
    )
    figures = service.compute_tie_out(INSTALLATION_ID, event_id, payment)
    assert figures.confirmed_net_irr == expected_confirmed
    assert figures.deductions_irr == deductions
    assert figures.matched_deposits_irr == expected_deposits
    assert figures.difference_irr == 0
    assert figures.passes is True


def test_tie_out_counts_a_twice_matched_tranche_once() -> None:
    """One tranche matched by two partial deposits: confirmed once, deposits summed."""
    registry, contract_version_id = _registry()
    repo = PostingFakeRepository()
    service = _service(repo, registry)
    payment = GatewayPayment(
        id=str(uuid.uuid4()),
        installation_id=INSTALLATION_ID,
        payment_channel_id="snapppay",
        gateway_transaction_id="TXN-702-split",
        gross_irr=1_000_000,
        fee_irr=0,
        fee_vat_irr=0,
        net_irr=1_000_000,
        contract_version_id=contract_version_id,
        fee_rate_snapshot=Decimal("10"),
        sales_period_jalali="1405-08",
        gateway_confirmed_at=NOW,
        source=PaymentSource.STORE_WEBHOOK,
        status=GatewayPaymentStatus.SETTLING,
    )
    repo.payments[payment.id] = payment
    tranche = SettlementTranche(
        id=str(uuid.uuid4()),
        installation_id=INSTALLATION_ID,
        gateway_payment_id=payment.id,
        tranche_index=1,
        tranche_count=1,
        sales_period_jalali="1405-08",
        expected_window_start=datetime.date(2026, 11, 1),
        expected_date=PAYOUT_DATE,
        gross_irr=1_000_000,
        fee_irr=0,
        fee_vat_irr=0,
        net_irr=1_000_000,
        state=TrancheState.CONFIRMED,
        confirmed_net_irr=1_000_000,
    )
    repo.tranches[tranche.id] = tranche
    event_id = str(uuid.uuid4())
    repo.events[event_id] = SettlementEvent(
        id=event_id,
        installation_id=INSTALLATION_ID,
        payment_channel_id="snapppay",
        payout_date=PAYOUT_DATE,
        net_irr=1_000_000,
        source="file",
    )
    for allocated in (600_000, 400_000):
        deposit = BankDeposit(
            id=str(uuid.uuid4()),
            installation_id=INSTALLATION_ID,
            deposit_date=PAYOUT_DATE,
            amount_irr=allocated,
        )
        repo.deposits[deposit.id] = deposit
        match = SettlementMatch(
            id=str(uuid.uuid4()),
            installation_id=INSTALLATION_ID,
            tranche_id=tranche.id,
            allocated_amount_irr=allocated,
            method="exact",
            bank_deposit_id=deposit.id,
            settlement_event_id=event_id,
        )
        repo.matches[match.id] = match
    figures = service.compute_tie_out(INSTALLATION_ID, event_id, payment)
    # The tranche's confirmed net counts once (1,000,000), not twice;
    # both deposits count toward the deposits total (600k + 400k).
    assert figures.confirmed_net_irr == 1_000_000
    assert figures.matched_deposits_irr == 1_000_000
    assert figures.difference_irr == 0
    assert figures.passes is True
