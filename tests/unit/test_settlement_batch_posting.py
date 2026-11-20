"""Unit tests for the batch → tranche settlement posting fan-out.

``EngineSettlementPosting`` adapts the batch-level
:class:`SettlementPostingPort` (called by the US-523 tie-out after
the tie-out holds and the policy gate allows) to the tranche-level
:class:`SettlementPostingService` (US-522). The posting service is
faked here — the real service is covered by
``test_settlement_posting.py`` — and the repository is the shared
``PostingFakeRepository`` (the posting ledger).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field

import pytest

from hesabyar.application.commands.settlement_posting import (
    BankReceiptResult,
    ClearingResult,
    TieOutFigures,
)
from hesabyar.domain.accounting.refs import (
    AccountRef,
    CompanyRef,
    JournalRef,
    PartyRef,
    PaymentJournalRef,
    PaymentRef,
)
from hesabyar.domain.accounting.value_objects import PaymentView
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.domain.settlement.commands import (
    PostBankReceipt,
    PostBatchBankReceipt,
    PostTrancheClearing,
    SettlementPostingWiring,
)
from hesabyar.domain.settlement.errors import (
    SettlementPostingError,
    SettlementPostingExistsError,
    SettlementPostingPreconditionError,
    SettlementTieOutFailedError,
    SettlementWiringUnresolvedError,
)
from hesabyar.domain.settlement.models import (
    SettlementPostingKind,
    SettlementTranche,
    TrancheState,
)
from hesabyar.infrastructure.accounting.tryton.settlement_posting import (
    EngineSettlementPosting,
)
from tests.unit.test_settlement_posting import PostingFakeRepository

INSTALLATION_ID = "00000000-0000-0000-0000-000000000523"
TENANT_ID = "00000000-0000-0000-0000-000000000001"
COMPANY_ID = 7
EVENT_ID = "00000000-0000-0000-0000-000000000e01"
DEPOSIT_DATE = datetime.date(2026, 11, 19)
PAYOUT_DATE = datetime.date(2026, 11, 18)
DUE_DATE = datetime.date(2026, 11, 5)


def _wiring(company_id: int = COMPANY_ID) -> SettlementPostingWiring:
    return SettlementPostingWiring(
        company=CompanyRef(company_id),
        gateway_party=PartyRef(11),
        payment_journal=PaymentJournalRef(12),
        receivable_account=AccountRef(21),
        clearing_account=AccountRef(22),
        bank_account=AccountRef(23),
        fee_expense_account=AccountRef(24),
        fee_vat_account=AccountRef(25),
        revenue_account=AccountRef(26),
        general_journal=JournalRef(27),
    )


def _tranche(
    tranche_id: str,
    *,
    net_irr: int,
    received_net_irr: int,
    payment_id: str = "pay-1",
    index: int = 1,
) -> SettlementTranche:
    return SettlementTranche(
        id=tranche_id,
        installation_id=INSTALLATION_ID,
        gateway_payment_id=payment_id,
        tranche_index=index,
        tranche_count=2,
        sales_period_jalali="1405-08",
        expected_window_start=DUE_DATE,
        expected_date=DUE_DATE,
        gross_irr=net_irr,
        fee_irr=0,
        fee_vat_irr=0,
        net_irr=net_irr,
        state=TrancheState.RECEIVED,
        confirmed_net_irr=net_irr,
        received_net_irr=received_net_irr,
    )


def _batch(
    tranche_ids: tuple[str, ...],
    *,
    company_engine_id: int | None = COMPANY_ID,
    amount_irr: int = 0,
) -> PostBatchBankReceipt:
    return PostBatchBankReceipt(
        installation_id=INSTALLATION_ID,
        tenant_id=TENANT_ID,
        channel_key="snapppay",
        settlement_event_id=EVENT_ID,
        bank_deposit_ids=("dep-1",),
        tranche_ids=tranche_ids,
        amount_irr=amount_irr,
        payout_date=PAYOUT_DATE,
        deposit_date=DEPOSIT_DATE,
        import_batch_id="batch-1",
        company_engine_id=company_engine_id,
    )


@dataclass
class FakePostingService:
    """Configurable tranche-level posting service (records calls)."""

    repo: PostingFakeRepository
    calls: list[PostBankReceipt] = field(default_factory=list)
    clearing_calls: list[PostTrancheClearing] = field(default_factory=list)
    wiring_calls: list[str] = field(default_factory=list)
    fail_wiring: set[str] = field(default_factory=set)
    fail_post: dict[str, Exception] = field(default_factory=dict)
    fail_clearing: dict[str, Exception] = field(default_factory=dict)
    post_then_exists: set[str] = field(default_factory=set)
    post_then_already_posted: set[str] = field(default_factory=set)
    next_move_id: int = 1000
    next_payment_id: int = 5000

    def resolve_wiring_for_tranche(
        self,
        installation_id: str,
        tranche_id: str,
        company: CompanyRef,
    ) -> SettlementPostingWiring:
        self.wiring_calls.append(tranche_id)
        if tranche_id in self.fail_wiring:
            raise SettlementWiringUnresolvedError(
                f"Cannot resolve settlement wiring for tranche {tranche_id}: "
                "gateway_journal:bank. Configure the mappings.",
                details={"tranche_id": tranche_id, "unresolved": ["gateway_journal:bank"]},
            )
        return _wiring(company.engine_id)

    def post_tranche_clearing(
        self, installation_id: str, command: PostTrancheClearing
    ) -> ClearingResult:
        self.clearing_calls.append(command)
        tranche = self.repo.get_tranche(installation_id, command.tranche_id)
        assert tranche is not None
        if command.tranche_id in self.fail_clearing:
            raise self.fail_clearing[command.tranche_id]
        already = sum(
            posting.amount_irr
            for posting in self.repo.list_postings_for_tranche(
                installation_id, command.tranche_id
            )
            if posting.kind == SettlementPostingKind.CLEARING
        )
        amount = tranche.confirmed_net_irr - already
        move_id = self.next_move_id
        self.next_move_id += 1
        payment_id = self.next_payment_id
        self.next_payment_id += 1
        posting = self.repo.save_posting(
            installation_id=installation_id,
            gateway_payment_id=tranche.gateway_payment_id,
            kind=SettlementPostingKind.CLEARING.value,
            amount_irr=amount,
            tranche_id=tranche.id,
            tryton_move_id=move_id,
            tryton_payment_id=payment_id,
        )
        return ClearingResult(
            tranche=tranche,
            amount_irr=amount,
            cleared_total_irr=already + amount,
            payment=PaymentView(
                ref=PaymentRef(payment_id),
                company=command.wiring.company,
                number=f"P-{payment_id}",
                kind="receivable",
                party=command.wiring.gateway_party,
                party_name="SnappPay",
                date=FiscalDate(command.payout_date),
                amount=MoneyIRR(amount),
                state="succeeded",
                reference=f"settlement:{tranche.id}",
                journal_name="SnappPay",
                currency_code="IRR",
            ),
            posting=posting,
            reconciliation=None,
            fee_posting=None,
        )

    def post_bank_receipt(
        self, installation_id: str, command: PostBankReceipt
    ) -> BankReceiptResult:
        self.calls.append(command)
        tranche = self.repo.get_tranche(installation_id, command.tranche_id)
        assert tranche is not None
        if command.tranche_id in self.fail_post:
            raise self.fail_post[command.tranche_id]
        already = sum(
            posting.amount_irr
            for posting in self.repo.list_postings_for_tranche(
                installation_id, command.tranche_id
            )
            if posting.kind == SettlementPostingKind.BANK_RECEIPT
        )
        amount = tranche.received_net_irr - already
        move_id = self.next_move_id
        self.next_move_id += 1
        posting = self.repo.save_posting(
            installation_id=installation_id,
            gateway_payment_id=tranche.gateway_payment_id,
            kind=SettlementPostingKind.BANK_RECEIPT.value,
            amount_irr=amount,
            tranche_id=tranche.id,
            tryton_move_id=move_id,
        )
        if command.tranche_id in self.post_then_exists:
            # The move posted, then a duplicate signal arrived (a
            # concurrent worker posted the same tranche between the
            # adapter's pre-check and this call).
            raise SettlementPostingExistsError(
                f"The bank receipt for tranche {tranche.id} was already posted.",
                details={"tranche_id": tranche.id, "posting_id": posting.id},
            )
        if command.tranche_id in self.post_then_already_posted:
            raise SettlementPostingPreconditionError(
                f"Tranche {tranche.id} has no newly received amount to post.",
                details={
                    "tranche_id": tranche.id,
                    "received_net_irr": tranche.received_net_irr,
                    "already_posted_irr": tranche.received_net_irr,
                },
            )
        return BankReceiptResult(
            tranche=tranche,
            amount_irr=amount,
            received_total_irr=already + amount,
            posting=posting,
            tie_out=TieOutFigures(
                confirmed_net_irr=tranche.net_irr,
                deductions_irr=0,
                matched_deposits_irr=tranche.received_net_irr,
                difference_irr=0,
                tolerance_irr=0,
            ),
            reconciliation=None,
        )


def _seed(
    repo: PostingFakeRepository,
    tranche_id: str,
    *,
    net_irr: int,
    received_net_irr: int,
    index: int = 1,
) -> SettlementTranche:
    tranche = _tranche(
        tranche_id, net_irr=net_irr, received_net_irr=received_net_irr, index=index
    )
    repo.tranches[tranche.id] = tranche
    return tranche


def _seed_receipt(
    repo: PostingFakeRepository,
    tranche_id: str,
    *,
    amount_irr: int,
    move_id: int,
) -> None:
    repo.save_posting(
        installation_id=INSTALLATION_ID,
        gateway_payment_id="pay-1",
        kind=SettlementPostingKind.BANK_RECEIPT.value,
        amount_irr=amount_irr,
        tranche_id=tranche_id,
        tryton_move_id=move_id,
    )


def _seed_clearing(
    repo: PostingFakeRepository,
    tranche_id: str,
    *,
    amount_irr: int,
    move_id: int,
) -> None:
    repo.save_posting(
        installation_id=INSTALLATION_ID,
        gateway_payment_id="pay-1",
        kind=SettlementPostingKind.CLEARING.value,
        amount_irr=amount_irr,
        tranche_id=tranche_id,
        tryton_move_id=move_id,
    )


def _postings_of_kind(
    repo: PostingFakeRepository, tranche_id: str, kind: SettlementPostingKind
) -> list:
    return [
        posting
        for posting in repo.list_postings_for_tranche(INSTALLATION_ID, tranche_id)
        if posting.kind == kind
    ]


@pytest.fixture
def repo() -> PostingFakeRepository:
    return PostingFakeRepository()


@pytest.fixture
def service(repo: PostingFakeRepository) -> FakePostingService:
    return FakePostingService(repo=repo)


@pytest.fixture
def adapter(
    service: FakePostingService, repo: PostingFakeRepository
) -> EngineSettlementPosting:
    return EngineSettlementPosting(service, repo)


class TestHappyPath:
    def test_posts_all_tranches_in_order(
        self,
        adapter: EngineSettlementPosting,
        service: FakePostingService,
        repo: PostingFakeRepository,
    ) -> None:
        _seed(repo, "t1", net_irr=100, received_net_irr=100, index=1)
        _seed(repo, "t2", net_irr=200, received_net_irr=200, index=2)

        result = adapter.post_bank_receipt(_batch(("t1", "t2"), amount_irr=300))

        assert result.posted is True
        assert result.amount_irr == 300
        assert result.tryton_journal_id == 1001
        assert result.tryton_journal_ids == (1001, 1003)
        assert service.wiring_calls == ["t1", "t2"]
        assert [call.tranche_id for call in service.calls] == ["t1", "t2"]
        for call in service.calls:
            assert call.settlement_event_id == EVENT_ID
            assert call.deposit_date == DEPOSIT_DATE
            assert call.actor is None
            assert call.wiring.company == CompanyRef(COMPANY_ID)
        assert len(repo.list_postings_for_tranche(INSTALLATION_ID, "t1")) == 2
        assert len(_postings_of_kind(repo, "t1", SettlementPostingKind.CLEARING)) == 1
        assert (
            len(_postings_of_kind(repo, "t1", SettlementPostingKind.BANK_RECEIPT))
            == 1
        )
        assert len(repo.list_postings_for_tranche(INSTALLATION_ID, "t2")) == 2
        assert len(_postings_of_kind(repo, "t2", SettlementPostingKind.CLEARING)) == 1
        assert (
            len(_postings_of_kind(repo, "t2", SettlementPostingKind.BANK_RECEIPT))
            == 1
        )

    def test_partially_posted_tranche_is_topped_up_not_skipped(
        self,
        adapter: EngineSettlementPosting,
        service: FakePostingService,
        repo: PostingFakeRepository,
    ) -> None:
        _seed(repo, "t1", net_irr=100, received_net_irr=100, index=1)
        _seed_receipt(repo, "t1", amount_irr=40, move_id=555)

        result = adapter.post_bank_receipt(_batch(("t1",), amount_irr=100))

        # Only the newly received remainder (60) is posted now, but
        # the batch total + journal ids cover the whole receipt.
        # (The clearing move takes id 1000; the receipt top-up 1001.)
        assert [call.tranche_id for call in service.calls] == ["t1"]
        assert result.amount_irr == 100
        assert result.tryton_journal_ids == (555, 1001)
        assert result.tryton_journal_id == 555


class TestPartialPostingAndRetry:
    def test_second_tranche_failure_keeps_first_posted(
        self,
        adapter: EngineSettlementPosting,
        service: FakePostingService,
        repo: PostingFakeRepository,
    ) -> None:
        _seed(repo, "t1", net_irr=100, received_net_irr=100, index=1)
        _seed(repo, "t2", net_irr=200, received_net_irr=200, index=2)
        service.fail_post["t2"] = SettlementTieOutFailedError(
            "Batch tie-out failed for settlement event.",
            details={"settlement_event_id": EVENT_ID, "difference_irr": 5},
        )

        with pytest.raises(SettlementPostingError) as exc_info:
            adapter.post_bank_receipt(_batch(("t1", "t2"), amount_irr=300))

        assert exc_info.value.code == "SETTLEMENT_POSTING_ERROR"
        assert exc_info.value.details["tranche_id"] == "t2"
        assert exc_info.value.details["posted_tranche_ids"] == ["t1"]
        # Tranche 1 stays posted: its clearing + receipt rows stand.
        assert len(repo.list_postings_for_tranche(INSTALLATION_ID, "t1")) == 2
        receipts = _postings_of_kind(
            repo, "t1", SettlementPostingKind.BANK_RECEIPT
        )
        assert len(receipts) == 1
        assert receipts[0].amount_irr == 100
        assert receipts[0].tryton_move_id == 1001
        # Tranche 2 was cleared before its receipt failed: the clearing
        # row stands (the retry skips clearing via the ledger check),
        # but no bank receipt was posted for it.
        assert (
            len(_postings_of_kind(repo, "t2", SettlementPostingKind.CLEARING)) == 1
        )
        assert (
            _postings_of_kind(repo, "t2", SettlementPostingKind.BANK_RECEIPT) == []
        )

    def test_retry_after_partial_failure_skips_posted_tranche(
        self,
        adapter: EngineSettlementPosting,
        service: FakePostingService,
        repo: PostingFakeRepository,
    ) -> None:
        _seed(repo, "t1", net_irr=100, received_net_irr=100, index=1)
        _seed(repo, "t2", net_irr=200, received_net_irr=200, index=2)
        service.fail_post["t2"] = SettlementTieOutFailedError("tie-out failed")
        with pytest.raises(SettlementPostingError):
            adapter.post_bank_receipt(_batch(("t1", "t2"), amount_irr=300))

        # The tie-out is fixed; the retry must NOT re-post t1 — and
        # must NOT re-clear t2 (its clearing row stands from the
        # first attempt).
        service.fail_post.clear()
        calls_before = len(service.calls)
        clearing_before = len(service.clearing_calls)
        result = adapter.post_bank_receipt(_batch(("t1", "t2"), amount_irr=300))

        assert result.amount_irr == 300
        assert result.tryton_journal_ids == (1001, 1003)
        assert len(service.calls) == calls_before + 1
        assert service.calls[-1].tranche_id == "t2"
        assert len(service.clearing_calls) == clearing_before
        assert len(repo.list_postings_for_tranche(INSTALLATION_ID, "t1")) == 2

    def test_already_posted_tranche_skipped_on_retry(
        self,
        adapter: EngineSettlementPosting,
        service: FakePostingService,
        repo: PostingFakeRepository,
    ) -> None:
        _seed(repo, "t1", net_irr=100, received_net_irr=100, index=1)
        _seed(repo, "t2", net_irr=200, received_net_irr=200, index=2)
        _seed_receipt(repo, "t1", amount_irr=100, move_id=555)

        result = adapter.post_bank_receipt(_batch(("t1", "t2"), amount_irr=300))

        assert [call.tranche_id for call in service.calls] == ["t2"]
        assert service.wiring_calls == ["t2"]
        assert result.amount_irr == 300
        assert result.tryton_journal_ids == (555, 1001)
        assert result.tryton_journal_id == 555
        assert len(repo.list_postings_for_tranche(INSTALLATION_ID, "t1")) == 1

    def test_all_tranches_already_posted_returns_existing_moves(
        self,
        adapter: EngineSettlementPosting,
        service: FakePostingService,
        repo: PostingFakeRepository,
    ) -> None:
        _seed(repo, "t1", net_irr=100, received_net_irr=100, index=1)
        _seed(repo, "t2", net_irr=200, received_net_irr=200, index=2)
        _seed_receipt(repo, "t1", amount_irr=100, move_id=555)
        _seed_receipt(repo, "t2", amount_irr=200, move_id=556)

        result = adapter.post_bank_receipt(_batch(("t1", "t2"), amount_irr=300))

        assert service.calls == []
        assert result.posted is True
        assert result.amount_irr == 300
        assert result.tryton_journal_ids == (555, 556)


class TestDuplicateSignals:
    def test_exists_error_with_covering_ledger_is_already_posted(
        self,
        adapter: EngineSettlementPosting,
        service: FakePostingService,
        repo: PostingFakeRepository,
    ) -> None:
        _seed(repo, "t1", net_irr=100, received_net_irr=100, index=1)
        _seed(repo, "t2", net_irr=200, received_net_irr=200, index=2)
        service.post_then_exists.add("t1")

        result = adapter.post_bank_receipt(_batch(("t1", "t2"), amount_irr=300))

        assert result.amount_irr == 300
        assert result.tryton_journal_ids == (1001, 1003)
        assert len(repo.list_postings_for_tranche(INSTALLATION_ID, "t1")) == 2

    def test_exists_error_without_covering_ledger_fails_closed(
        self,
        adapter: EngineSettlementPosting,
        service: FakePostingService,
        repo: PostingFakeRepository,
    ) -> None:
        _seed(repo, "t1", net_irr=100, received_net_irr=100, index=1)
        service.fail_post["t1"] = SettlementPostingExistsError(
            "The bank receipt was already posted."
        )

        with pytest.raises(SettlementPostingError):
            adapter.post_bank_receipt(_batch(("t1",), amount_irr=100))

    def test_already_posted_precondition_is_already_posted(
        self,
        adapter: EngineSettlementPosting,
        service: FakePostingService,
        repo: PostingFakeRepository,
    ) -> None:
        _seed(repo, "t1", net_irr=100, received_net_irr=100, index=1)
        service.post_then_already_posted.add("t1")

        result = adapter.post_bank_receipt(_batch(("t1",), amount_irr=100))

        assert result.amount_irr == 100
        assert result.tryton_journal_ids == (1001,)
        assert len(repo.list_postings_for_tranche(INSTALLATION_ID, "t1")) == 2

    def test_other_precondition_errors_fail_the_batch(
        self,
        adapter: EngineSettlementPosting,
        service: FakePostingService,
        repo: PostingFakeRepository,
    ) -> None:
        _seed(repo, "t1", net_irr=100, received_net_irr=100, index=1)
        service.fail_post["t1"] = SettlementPostingPreconditionError(
            "Tranche t1's tie-out gate failed inside the receipt.",
            details={"tranche_id": "t1"},
        )

        with pytest.raises(SettlementPostingError) as exc_info:
            adapter.post_bank_receipt(_batch(("t1",), amount_irr=100))

        assert exc_info.value.details["tranche_id"] == "t1"
        assert exc_info.value.details["posted_tranche_ids"] == []


class TestFailClosed:
    def test_wiring_failure_fails_the_batch(
        self,
        adapter: EngineSettlementPosting,
        service: FakePostingService,
        repo: PostingFakeRepository,
    ) -> None:
        _seed(repo, "t1", net_irr=100, received_net_irr=100, index=1)
        _seed(repo, "t2", net_irr=200, received_net_irr=200, index=2)
        service.fail_wiring.add("t2")

        with pytest.raises(SettlementPostingError) as exc_info:
            adapter.post_bank_receipt(_batch(("t1", "t2"), amount_irr=300))

        assert exc_info.value.details["tranche_id"] == "t2"
        assert exc_info.value.details["posted_tranche_ids"] == ["t1"]
        assert exc_info.value.details["cause_code"] == "SETTLEMENT_WIRING_UNRESOLVED"
        assert len(repo.list_postings_for_tranche(INSTALLATION_ID, "t1")) == 2
        assert repo.list_postings_for_tranche(INSTALLATION_ID, "t2") == []

    def test_unexpected_engine_error_fails_closed(
        self,
        adapter: EngineSettlementPosting,
        service: FakePostingService,
        repo: PostingFakeRepository,
    ) -> None:
        _seed(repo, "t1", net_irr=100, received_net_irr=100, index=1)
        service.fail_post["t1"] = RuntimeError("connection reset")

        with pytest.raises(SettlementPostingError) as exc_info:
            adapter.post_bank_receipt(_batch(("t1",), amount_irr=100))

        assert exc_info.value.details["cause_code"] == "RuntimeError"

    def test_no_company_fails_closed(
        self, adapter: EngineSettlementPosting, service: FakePostingService
    ) -> None:
        with pytest.raises(SettlementPostingError):
            adapter.post_bank_receipt(
                _batch(("t1",), company_engine_id=None, amount_irr=100)
            )
        assert service.calls == []
        assert service.wiring_calls == []

    def test_empty_tranche_list_fails_closed(
        self, adapter: EngineSettlementPosting, service: FakePostingService
    ) -> None:
        with pytest.raises(SettlementPostingError):
            adapter.post_bank_receipt(_batch((), amount_irr=0))
        assert service.calls == []


class TestLateClearing:
    """The batch flow clears a tranche late, before its bank receipt.

    A file-imported tranche reaches the tie-out already RECEIVED and
    was never cleared — the bank receipt refuses an uncleared tranche,
    so the adapter posts the clearing transfer first (US-525 e2e
    diagnosis, 2026-11-19).
    """

    def test_clears_uncleared_tranche_before_its_receipt(
        self,
        adapter: EngineSettlementPosting,
        service: FakePostingService,
        repo: PostingFakeRepository,
    ) -> None:
        _seed(repo, "t1", net_irr=100, received_net_irr=100, index=1)

        result = adapter.post_bank_receipt(_batch(("t1",), amount_irr=100))

        assert result.amount_irr == 100
        assert [call.tranche_id for call in service.clearing_calls] == ["t1"]
        # The clearing date is the tranche's scheduled payout date.
        assert service.clearing_calls[0].payout_date == DUE_DATE
        assert service.clearing_calls[0].wiring.company == CompanyRef(COMPANY_ID)
        assert [call.tranche_id for call in service.calls] == ["t1"]
        assert (
            len(_postings_of_kind(repo, "t1", SettlementPostingKind.CLEARING)) == 1
        )
        assert (
            len(_postings_of_kind(repo, "t1", SettlementPostingKind.BANK_RECEIPT))
            == 1
        )

    def test_skips_clearing_when_clearing_row_exists(
        self,
        adapter: EngineSettlementPosting,
        service: FakePostingService,
        repo: PostingFakeRepository,
    ) -> None:
        _seed(repo, "t1", net_irr=100, received_net_irr=100, index=1)
        _seed_clearing(repo, "t1", amount_irr=100, move_id=777)

        result = adapter.post_bank_receipt(_batch(("t1",), amount_irr=100))

        assert result.amount_irr == 100
        assert service.clearing_calls == []
        assert (
            len(_postings_of_kind(repo, "t1", SettlementPostingKind.CLEARING)) == 1
        )
        assert (
            len(_postings_of_kind(repo, "t1", SettlementPostingKind.BANK_RECEIPT))
            == 1
        )

    def test_clearing_failure_fails_the_batch_and_keeps_prior_posted(
        self,
        adapter: EngineSettlementPosting,
        service: FakePostingService,
        repo: PostingFakeRepository,
    ) -> None:
        _seed(repo, "t1", net_irr=100, received_net_irr=100, index=1)
        _seed(repo, "t2", net_irr=200, received_net_irr=200, index=2)
        service.fail_clearing["t2"] = SettlementPostingPreconditionError(
            "The payment journal has no clearing account configured.",
            details={"tranche_id": "t2"},
        )

        with pytest.raises(SettlementPostingError) as exc_info:
            adapter.post_bank_receipt(_batch(("t1", "t2"), amount_irr=300))

        assert exc_info.value.details["tranche_id"] == "t2"
        assert exc_info.value.details["posted_tranche_ids"] == ["t1"]
        # t1 stands fully posted; t2 has no postings at all (its
        # clearing failed before any engine write for it).
        assert len(repo.list_postings_for_tranche(INSTALLATION_ID, "t1")) == 2
        assert repo.list_postings_for_tranche(INSTALLATION_ID, "t2") == []
