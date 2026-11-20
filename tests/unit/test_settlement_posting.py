"""Service tests for settlement posting (US-522).

DB-free and engine-free: the posting service runs against an
in-memory settlement fake (extended with the US-522 port methods),
the shared fake integrations registry, a recording audit fake, and a
small in-memory engine fake that models moves, payments, clearing
moves and reconciliations. The live Tryton behaviour is covered by
``tests/contract/test_settlement_posting.py``.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any, cast

import pytest

from hesabyar.application.commands.integrations import (
    ChannelRegistryService,
    GatewayContractService,
)
from hesabyar.application.commands.settlement import SettlementCommandService
from hesabyar.application.commands.settlement_posting import (
    SettlementPostingService,
)
from hesabyar.application.errors import IntegrationChannelNotFoundError
from hesabyar.domain.accounting.commands import (
    CreateEngineCheque,
    CreateJournalDraft,
    CreatePayment,
    PostJournal,
    ReconcileMoveLines,
    SucceedPayment,
    TransitionEngineCheque,
)
from hesabyar.domain.accounting.ports import AccountingEnginePort
from hesabyar.domain.accounting.refs import (
    AccountRef,
    CheckbookEngineRef,
    ChequeEngineRef,
    CompanyRef,
    JournalDraftRef,
    JournalRef,
    MoveLineRef,
    PartyRef,
    PaymentJournalRef,
    PaymentRef,
    PostedJournalRef,
    ReconciliationRef,
)
from hesabyar.domain.accounting.value_objects import (
    AccountView,
    CheckbookEngineView,
    ChequeEngineView,
    MoveLineView,
    PaymentJournalView,
    PaymentView,
    ReconciliationView,
)
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.domain.integrations.models import (
    IntegrationMapping,
    MappingKind,
    TrancheLeg,
)
from hesabyar.domain.settlement.commands import (
    ApplyRefund,
    CreateBankDeposit,
    CreateGatewayPayment,
    CreateSettlementEvent,
    CreateSettlementMatch,
    PostBankReceipt,
    PostFeeVariance,
    PostSaleFee,
    PostTrancheClearing,
    SettlementPostingWiring,
    TransitionTranche,
)
from hesabyar.domain.settlement.errors import (
    SettlementPostingExistsError,
    SettlementPostingPreconditionError,
    SettlementReconciliationFailedError,
    SettlementTieOutFailedError,
    SettlementWiringUnresolvedError,
)
from hesabyar.domain.settlement.models import (
    BankDeposit,
    GatewayPayment,
    PaymentSource,
    SettlementEvent,
    SettlementFeeRecord,
    SettlementMatch,
    SettlementPosting,
    SettlementPostingKind,
    SettlementPostingStatus,
    SettlementTranche,
    TrancheState,
)
from tests.unit.test_integrations_services import FakeIntegrationsRepository
from tests.unit.test_settlement_services import (
    FOUR_TRANCHES,
    INSTALLATION_ID,
    NOW,
    FakeSettlementAudit,
    FakeSettlementRepository,
)

TENANT_ID = "00000000-0000-0000-0000-000000000522"
SALE_DATE = datetime.date(2026, 11, 19)

COMPANY = CompanyRef(1)
GATEWAY_PARTY = PartyRef(10)
PAYMENT_JOURNAL = PaymentJournalRef(20)
RECEIVABLE = AccountRef(100)
CLEARING = AccountRef(101)
BANK = AccountRef(102)
FEE_EXPENSE = AccountRef(103)
FEE_VAT = AccountRef(104)
REVENUE = AccountRef(105)
GENERAL_JOURNAL = JournalRef(9)

WIRING = SettlementPostingWiring(
    company=COMPANY,
    gateway_party=GATEWAY_PARTY,
    payment_journal=PAYMENT_JOURNAL,
    receivable_account=RECEIVABLE,
    clearing_account=CLEARING,
    bank_account=BANK,
    fee_expense_account=FEE_EXPENSE,
    fee_vat_account=FEE_VAT,
    revenue_account=REVENUE,
    general_journal=GENERAL_JOURNAL,
)


def _new_id() -> str:
    return str(uuid.uuid4())


@dataclass
class PostingFakeRepository(FakeSettlementRepository):
    """The settlement fake extended with the US-522 port methods."""

    postings: dict[str, SettlementPosting] = field(default_factory=dict)

    def update_tranche(
        self,
        tranche_id: str,
        *,
        state: TrancheState | None = None,
        confirmed_net_irr: int | None = None,
        received_net_irr: int | None = None,
        variance_irr: int | None = None,
        variance_reason: str | None = None,
        match_method: str | None = None,
        match_confidence: Any | None = None,
        tryton_receivable_line_id: int | None = None,
        expected_window_start: datetime.date | None = None,
        expected_date: datetime.date | None = None,
        sales_period_jalali: str | None = None,
        gross_irr: int | None = None,
        fee_irr: int | None = None,
        fee_vat_irr: int | None = None,
        net_irr: int | None = None,
    ) -> SettlementTranche | None:
        updated = super().update_tranche(
            tranche_id,
            state=state,
            confirmed_net_irr=confirmed_net_irr,
            received_net_irr=received_net_irr,
            variance_irr=variance_irr,
            variance_reason=variance_reason,
            match_method=match_method,
            match_confidence=match_confidence,
            tryton_receivable_line_id=tryton_receivable_line_id,
            expected_window_start=expected_window_start,
            expected_date=expected_date,
            sales_period_jalali=sales_period_jalali,
        )
        if updated is None:
            return None
        values = dict(updated.__dict__)
        if gross_irr is not None:
            values["gross_irr"] = gross_irr
        if fee_irr is not None:
            values["fee_irr"] = fee_irr
        if fee_vat_irr is not None:
            values["fee_vat_irr"] = fee_vat_irr
        if net_irr is not None:
            values["net_irr"] = net_irr
        final = SettlementTranche(**values)
        self.tranches[tranche_id] = final
        return final

    def get_settlement_event(
        self, installation_id: str, event_id: str
    ) -> SettlementEvent | None:
        event = self.events.get(event_id)
        if event is None or event.installation_id != installation_id:
            return None
        return event

    def get_bank_deposit(
        self, installation_id: str, deposit_id: str
    ) -> BankDeposit | None:
        deposit = self.deposits.get(deposit_id)
        if deposit is None or deposit.installation_id != installation_id:
            return None
        return deposit

    def list_matches_for_event(
        self, installation_id: str, settlement_event_id: str
    ) -> Sequence[SettlementMatch]:
        return [
            m
            for m in self.matches.values()
            if m.installation_id == installation_id
            and m.settlement_event_id == settlement_event_id
        ]

    def update_fee_record(
        self,
        fee_record_id: str,
        *,
        actual_fee_irr: int | None = None,
        actual_vat_irr: int | None = None,
        fee_variance_irr: int | None = None,
        vat_variance_irr: int | None = None,
        tryton_fee_journal_id: int | None = None,
        source: str | None = None,
    ) -> SettlementFeeRecord | None:
        record = self.fee_records.get(fee_record_id)
        if record is None:
            return None
        values = dict(record.__dict__)
        if actual_fee_irr is not None:
            values["actual_fee_irr"] = actual_fee_irr
        if actual_vat_irr is not None:
            values["actual_vat_irr"] = actual_vat_irr
        if fee_variance_irr is not None:
            values["fee_variance_irr"] = fee_variance_irr
        if vat_variance_irr is not None:
            values["vat_variance_irr"] = vat_variance_irr
        if tryton_fee_journal_id is not None:
            values["tryton_fee_journal_id"] = tryton_fee_journal_id
        if source is not None:
            values["source"] = source
        updated = SettlementFeeRecord(**values)
        self.fee_records[fee_record_id] = updated
        return updated

    def save_posting(
        self,
        *,
        installation_id: str,
        gateway_payment_id: str,
        kind: str,
        amount_irr: int,
        status: str = "posted",
        tranche_id: str | None = None,
        tryton_move_id: int | None = None,
        tryton_payment_id: int | None = None,
        tryton_reconciliation_id: int | None = None,
        reason: str | None = None,
        event_ref: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> SettlementPosting:
        posting = SettlementPosting(
            id=_new_id(),
            installation_id=installation_id,
            gateway_payment_id=gateway_payment_id,
            kind=SettlementPostingKind(kind),
            amount_irr=amount_irr,
            status=SettlementPostingStatus(status),
            tranche_id=tranche_id,
            tryton_move_id=tryton_move_id,
            tryton_payment_id=tryton_payment_id,
            tryton_reconciliation_id=tryton_reconciliation_id,
            reason=reason,
            event_ref=event_ref,
            details=details,
            created_at=NOW,
            updated_at=NOW,
        )
        self.postings[posting.id] = posting
        return posting

    def get_posting(
        self, installation_id: str, posting_id: str
    ) -> SettlementPosting | None:
        posting = self.postings.get(posting_id)
        if posting is None or posting.installation_id != installation_id:
            return None
        return posting

    def list_postings_for_payment(
        self, installation_id: str, payment_id: str
    ) -> Sequence[SettlementPosting]:
        return [
            p
            for p in self.postings.values()
            if p.installation_id == installation_id and p.gateway_payment_id == payment_id
        ]

    def list_postings_for_tranche(
        self, installation_id: str, tranche_id: str
    ) -> Sequence[SettlementPosting]:
        return [
            p
            for p in self.postings.values()
            if p.installation_id == installation_id and p.tranche_id == tranche_id
        ]

    def get_posting_by_kind(
        self,
        installation_id: str,
        kind: str,
        *,
        gateway_payment_id: str | None = None,
        tranche_id: str | None = None,
    ) -> SettlementPosting | None:
        for posting in self.postings.values():
            if posting.installation_id != installation_id:
                continue
            if posting.kind.value != kind:
                continue
            if gateway_payment_id is not None and posting.gateway_payment_id != gateway_payment_id:
                continue
            if tranche_id is not None and posting.tranche_id != tranche_id:
                continue
            return posting
        return None


class FakeEngine:
    """In-memory engine fake: moves, payments, clearing moves, reconciliations."""

    def __init__(self, *, clearing_configured: bool = True) -> None:
        self._next_id = 1000
        self.moves: dict[int, list[MoveLineView]] = {}
        self.posted: set[int] = set()
        self.payments: dict[int, PaymentView] = {}
        self.reconciliations: list[ReconciliationView] = []
        self.process_batches: list[tuple[int, ...]] = []
        self.clearing_configured = clearing_configured
        self.chart: list[AccountView] = [
            AccountView(
                ref=ref,
                company=COMPANY,
                code=code,
                name=code,
                type_name="other",
                parent=None,
                closed=False,
                party_required=code == "1.2.8",
                active=True,
            )
            for ref, code in (
                (RECEIVABLE, "1.2.8"),
                (CLEARING, "1.1.6"),
                (BANK, "1.1.1"),
                (FEE_EXPENSE, "5.2.4"),
                (FEE_VAT, "6.3.6"),
                (REVENUE, "4.1.1"),
            )
        ]

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    # -- chart / journals -------------------------------------------------

    def get_chart_of_accounts(self, company: CompanyRef) -> Sequence[AccountView]:
        return list(self.chart)

    def get_payment_journal(
        self, journal: PaymentJournalRef
    ) -> PaymentJournalView | None:
        if journal != PAYMENT_JOURNAL:
            return None
        return PaymentJournalView(
            ref=PAYMENT_JOURNAL,
            company=COMPANY,
            name="SnappPay",
            process_method="manual",
            clearing_account=CLEARING if self.clearing_configured else None,
            clearing_journal=GENERAL_JOURNAL if self.clearing_configured else None,
        )

    # -- journals ---------------------------------------------------------

    def create_journal_draft(self, command: CreateJournalDraft) -> JournalDraftRef:
        move_id = self._new_id()
        lines = [
            MoveLineView(
                ref=MoveLineRef(self._new_id()),
                move=JournalDraftRef(move_id),
                account=line.account,
                debit=line.debit,
                credit=line.credit,
                state="valid",
                party=line.party,
                maturity_date=line.maturity_date,
                description=line.description,
            )
            for line in command.lines
        ]
        self.moves[move_id] = lines
        return JournalDraftRef(move_id)

    def post_journal(self, command: PostJournal) -> PostedJournalRef:
        self.posted.add(command.draft.engine_id)
        return PostedJournalRef(command.draft.engine_id)

    def list_move_lines(
        self,
        company: CompanyRef,
        *,
        move: JournalDraftRef | None = None,
        account: AccountRef | None = None,
        party: PartyRef | None = None,
        maturity_date: FiscalDate | None = None,
        unreconciled_only: bool = False,
    ) -> Sequence[MoveLineView]:
        found: list[MoveLineView] = []
        for move_id, lines in self.moves.items():
            if move is not None and move.engine_id != move_id:
                continue
            for line in lines:
                if account is not None and line.account != account:
                    continue
                if party is not None and line.party != party:
                    continue
                if maturity_date is not None and line.maturity_date != maturity_date:
                    continue
                if unreconciled_only and line.reconciliation is not None:
                    continue
                found.append(line)
        return found

    def reconcile_move_lines(self, command: ReconcileMoveLines) -> ReconciliationView:
        lines = {
            line.ref.engine_id: line
            for move_lines in self.moves.values()
            for line in move_lines
        }
        chosen = [lines[ref.engine_id] for ref in command.lines]
        total = sum(line.balance.amount for line in chosen)
        assert total == 0, f"fake reconcile of unbalanced lines: {total}"
        assert len({line.account for line in chosen}) == 1
        assert len({line.party for line in chosen}) == 1
        rec = ReconciliationView(
            ref=ReconciliationRef(self._new_id()),
            number=f"R-{len(self.reconciliations) + 1}",
            lines=tuple(command.lines),
        )
        for move_id, move_lines in self.moves.items():
            self.moves[move_id] = [
                replace(line, reconciliation=rec.ref)
                if line.ref in command.lines
                else line
                for line in move_lines
            ]
        self.reconciliations.append(rec)
        return rec

    # -- payments ---------------------------------------------------------

    def create_payment(self, command: CreatePayment) -> PaymentRef:
        payment_id = self._new_id()
        self.payments[payment_id] = PaymentView(
            ref=PaymentRef(payment_id),
            company=command.company,
            number=f"P-{payment_id}",
            kind=command.kind,
            party=command.party,
            party_name="SnappPay",
            date=command.date,
            amount=command.amount,
            state="draft",
            reference=command.reference,
            journal_name="SnappPay",
            currency_code="IRR",
            move_line=command.line,
        )
        return PaymentRef(payment_id)

    def submit_payment(self, payment: PaymentRef) -> PaymentView:
        view = self.payments[payment.engine_id]
        updated = replace(view, state="submitted")
        self.payments[payment.engine_id] = updated
        return updated

    def process_payments(self, payments: Sequence[PaymentRef]) -> Sequence[PaymentView]:
        self.process_batches.append(tuple(p.engine_id for p in payments))
        out: list[PaymentView] = []
        for payment in payments:
            view = self.payments[payment.engine_id]
            updated = replace(view, state="processing")
            self.payments[payment.engine_id] = updated
            out.append(updated)
        return out

    def succeed_payment(self, command: SucceedPayment) -> PaymentView:
        view = self.payments[command.payment.engine_id]
        clearing_move_id = self._new_id()
        clearing_lines = [
            MoveLineView(
                ref=MoveLineRef(self._new_id()),
                move=JournalDraftRef(clearing_move_id),
                account=RECEIVABLE,
                debit=MoneyIRR.zero(),
                credit=view.amount,
                state="valid",
                party=view.party,
                description="clearing receivable leg",
            ),
            MoveLineView(
                ref=MoveLineRef(self._new_id()),
                move=JournalDraftRef(clearing_move_id),
                account=CLEARING,
                debit=view.amount,
                credit=MoneyIRR.zero(),
                state="valid",
                description="clearing leg",
            ),
        ]
        self.moves[clearing_move_id] = clearing_lines
        updated = replace(
            view, state="succeeded", clearing_move=JournalDraftRef(clearing_move_id)
        )
        self.payments[command.payment.engine_id] = updated
        return updated

    def get_payment(self, payment: PaymentRef) -> PaymentView | None:
        return self.payments.get(payment.engine_id)

    # -- cheques (US-531): stubs, never exercised here --

    def create_engine_cheque(self, command: CreateEngineCheque) -> ChequeEngineView:
        raise NotImplementedError

    def get_engine_cheque(self, cheque: ChequeEngineRef) -> ChequeEngineView | None:
        raise NotImplementedError

    def list_engine_cheques(
        self, company: CompanyRef, *, state: str | None = None
    ) -> Sequence[ChequeEngineView]:
        raise NotImplementedError

    def transition_engine_cheque(self, command: TransitionEngineCheque) -> ChequeEngineView:
        raise NotImplementedError

    # -- cheques (US-532): stubs, never exercised here --

    def list_engine_checkbooks(self, company: CompanyRef) -> Sequence[CheckbookEngineView]:
        raise NotImplementedError

    def get_engine_checkbook(
        self, company: CompanyRef, checkbook: CheckbookEngineRef
    ) -> CheckbookEngineView | None:
        raise NotImplementedError

    # -- test helpers -----------------------------------------------------

    def seed_invoice_lines(
        self, tranches: Sequence[SettlementTranche]
    ) -> dict[str, int]:
        """Seed one gross invoice line per tranche; return tranche id → line id."""
        move_id = self._new_id()
        lines: list[MoveLineView] = []
        by_tranche: dict[str, int] = {}
        for tranche in tranches:
            line_id = self._new_id()
            by_tranche[tranche.id] = line_id
            lines.append(
                MoveLineView(
                    ref=MoveLineRef(line_id),
                    move=JournalDraftRef(move_id),
                    account=RECEIVABLE,
                    debit=MoneyIRR(tranche.gross_irr),
                    credit=MoneyIRR.zero(),
                    state="valid",
                    party=GATEWAY_PARTY,
                    maturity_date=FiscalDate(tranche.expected_date),
                    description=f"invoice line tranche {tranche.tranche_index}",
                )
            )
        self.moves[move_id] = lines
        self.posted.add(move_id)
        return by_tranche

    def receivable_balance(self) -> int:
        """Posted gateway-receivable balance (debits − credits)."""
        total = 0
        for move_id, lines in self.moves.items():
            if move_id not in self.posted:
                continue
            for line in lines:
                if line.account == RECEIVABLE and line.party == GATEWAY_PARTY:
                    total += line.balance.amount
        return total


@pytest.fixture()
def registry() -> FakeIntegrationsRepository:
    repo = FakeIntegrationsRepository()
    ChannelRegistryService(repo).create_channel(
        INSTALLATION_ID, "snapppay", "اسنپ‌پی", "bnpl"
    )
    GatewayContractService(repo).create_version(
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
    return repo


@pytest.fixture()
def repo() -> PostingFakeRepository:
    return PostingFakeRepository()


@pytest.fixture()
def audit() -> FakeSettlementAudit:
    return FakeSettlementAudit()


@pytest.fixture()
def engine() -> FakeEngine:
    return FakeEngine()


@pytest.fixture()
def commands(
    repo: PostingFakeRepository,
    registry: FakeIntegrationsRepository,
    audit: FakeSettlementAudit,
) -> SettlementCommandService:
    return SettlementCommandService(repo, registry, audit)


@pytest.fixture()
def service(
    repo: PostingFakeRepository,
    registry: FakeIntegrationsRepository,
    engine: FakeEngine,
    audit: FakeSettlementAudit,
) -> SettlementPostingService:
    return SettlementPostingService(
        repo, registry, cast(AccountingEnginePort, engine), audit
    )


def _record(
    commands: SettlementCommandService, *, txn: str = "TXN-1", gross: int = 100_000_000
) -> GatewayPayment:
    result = commands.record_payment(
        CreateGatewayPayment(
            installation_id=INSTALLATION_ID,
            channel_key="snapppay",
            gateway_transaction_id=txn,
            gross_irr=gross,
            gateway_confirmed_at=NOW,
            source=PaymentSource.STORE_WEBHOOK,
            store_order_id="ORD-1",
        )
    )
    return result.payment


def _tranches(
    repo: PostingFakeRepository, payment: GatewayPayment
) -> list[SettlementTranche]:
    return list(repo.list_tranches_for_payment(INSTALLATION_ID, payment.id))


def _confirm(
    commands: SettlementCommandService,
    tranche: SettlementTranche,
    *,
    confirmed_net_irr: int | None = None,
) -> SettlementTranche:
    commands.transition_tranche(
        INSTALLATION_ID,
        TransitionTranche(
            tranche_id=tranche.id,
            to_state=TrancheState.DUE.value,
        ),
    )
    return commands.transition_tranche(
        INSTALLATION_ID,
        TransitionTranche(
            tranche_id=tranche.id,
            to_state=TrancheState.CONFIRMED.value,
            confirmed_net_irr=(
                confirmed_net_irr
                if confirmed_net_irr is not None
                else tranche.net_irr
            ),
        ),
    )


def _receive(
    commands: SettlementCommandService,
    tranche: SettlementTranche,
    *,
    received_net_irr: int | None = None,
) -> SettlementTranche:
    return commands.transition_tranche(
        INSTALLATION_ID,
        TransitionTranche(
            tranche_id=tranche.id,
            to_state=TrancheState.RECEIVED.value,
            received_net_irr=(
                received_net_irr if received_net_irr is not None else tranche.net_irr
            ),
        ),
    )


def _setup_sale(
    commands: SettlementCommandService,
    repo: PostingFakeRepository,
    engine: FakeEngine,
    service: SettlementPostingService,
    *,
    txn: str = "TXN-1",
    gross: int = 100_000_000,
) -> tuple[GatewayPayment, list[SettlementTranche]]:
    """Record the SnappPay sale, seed its invoice lines, post the fee."""
    payment = _record(commands, txn=txn, gross=gross)
    tranches = _tranches(repo, payment)
    line_ids = engine.seed_invoice_lines(tranches)
    for tranche in tranches:
        repo.update_tranche(
            tranche.id, tryton_receivable_line_id=line_ids[tranche.id]
        )
    service.post_sale_fee(
        INSTALLATION_ID,
        PostSaleFee(payment_id=payment.id, wiring=WIRING, date=SALE_DATE),
    )
    return payment, _tranches(repo, payment)


def _setup_event_with_deposit(
    repo: PostingFakeRepository,
    tranche: SettlementTranche,
    *,
    event_net_irr: int,
    deposit_irr: int,
    deductions_irr: int = 0,
) -> str:
    event = repo.save_settlement_event(
        CreateSettlementEvent(
            installation_id=INSTALLATION_ID,
            channel_key="snapppay",
            payout_date=datetime.date(2026, 11, 26),
            net_irr=event_net_irr,
            source="file",
            deductions_irr=deductions_irr,
        ),
        channel_id="snapppay",
    )
    deposit = repo.save_bank_deposit(
        CreateBankDeposit(
            installation_id=INSTALLATION_ID,
            deposit_date=datetime.date(2026, 11, 26),
            amount_irr=deposit_irr,
        )
    )
    repo.save_settlement_match(
        CreateSettlementMatch(
            installation_id=INSTALLATION_ID,
            tranche_id=tranche.id,
            allocated_amount_irr=deposit_irr,
            method="exact",
            bank_deposit_id=deposit.id,
            settlement_event_id=event.id,
        )
    )
    return event.id


class TestMoveLineBalance:
    def test_balance_is_debit_minus_credit(self) -> None:
        line = MoveLineView(
            ref=MoveLineRef(1),
            move=JournalDraftRef(1),
            account=RECEIVABLE,
            debit=MoneyIRR(100),
            credit=MoneyIRR(30),
            state="valid",
        )
        assert line.balance.amount == 70


class TestResolveWiring:
    def _seed_mappings(
        self, registry: FakeIntegrationsRepository, *, omit: str | None = None
    ) -> None:
        def put(kind: MappingKind, channel: str, source: str, target_type: str, target: str) -> None:
            if source == omit:
                return
            mapping = IntegrationMapping(
                id=_new_id(),
                installation_id=INSTALLATION_ID,
                mapping_kind=kind,
                channel_key=channel,
                source_key=source,
                target_type=target_type,
                target_ref=target,
                version=1,
            )
            registry.mappings[mapping.id] = mapping

        put(MappingKind.EXTERNAL_PARTY, "snapppay", "snapppay", "party", "10")
        put(MappingKind.GATEWAY_JOURNAL, "snapppay", "snapppay", "journal", "20")
        put(MappingKind.GATEWAY_JOURNAL, "snapppay", "receivable", "account", "1.2.8")
        put(MappingKind.GATEWAY_JOURNAL, "snapppay", "clearing", "account", "1.1.6")
        put(MappingKind.GATEWAY_JOURNAL, "snapppay", "bank", "account", "1.1.1")
        put(MappingKind.GATEWAY_JOURNAL, "snapppay", "fee_expense", "account", "5.2.4")
        put(MappingKind.GATEWAY_JOURNAL, "snapppay", "fee_vat", "account", "6.3.6")
        put(MappingKind.GATEWAY_JOURNAL, "snapppay", "revenue", "account", "4.1.1")

    def test_resolves_all_wiring_from_mappings(
        self, service: SettlementPostingService, registry: FakeIntegrationsRepository
    ) -> None:
        self._seed_mappings(registry)
        wiring = service.resolve_wiring(INSTALLATION_ID, "snapppay", COMPANY)
        assert wiring.company == COMPANY
        assert wiring.gateway_party == GATEWAY_PARTY
        assert wiring.payment_journal == PAYMENT_JOURNAL
        assert wiring.receivable_account == RECEIVABLE
        assert wiring.clearing_account == CLEARING
        assert wiring.bank_account == BANK
        assert wiring.fee_expense_account == FEE_EXPENSE
        assert wiring.fee_vat_account == FEE_VAT
        assert wiring.revenue_account == REVENUE
        assert wiring.general_journal is None  # optional, unmapped

    def test_missing_mapping_fails_closed_with_list(
        self, service: SettlementPostingService, registry: FakeIntegrationsRepository
    ) -> None:
        self._seed_mappings(registry, omit="fee_vat")
        with pytest.raises(SettlementWiringUnresolvedError) as excinfo:
            service.resolve_wiring(INSTALLATION_ID, "snapppay", COMPANY)
        assert "gateway_journal:fee_vat" in str(excinfo.value)

    def test_unknown_channel_rejected(self, service: SettlementPostingService) -> None:
        with pytest.raises(IntegrationChannelNotFoundError):
            service.resolve_wiring(INSTALLATION_ID, "nope", COMPANY)

    def test_resolve_wiring_for_payment_and_tranche(
        self,
        commands: SettlementCommandService,
        repo: PostingFakeRepository,
        service: SettlementPostingService,
        registry: FakeIntegrationsRepository,
    ) -> None:
        self._seed_mappings(registry)
        payment = _record(commands)
        tranches = _tranches(repo, payment)
        by_payment = service.resolve_wiring_for_payment(
            INSTALLATION_ID, payment.id, COMPANY
        )
        by_tranche = service.resolve_wiring_for_tranche(
            INSTALLATION_ID, tranches[0].id, COMPANY
        )
        assert by_payment.gateway_party == GATEWAY_PARTY
        assert by_payment.receivable_account == RECEIVABLE
        assert by_tranche == by_payment


class TestPostSaleFee:
    def test_posts_fee_split_per_tranche(
        self,
        commands: SettlementCommandService,
        repo: PostingFakeRepository,
        engine: FakeEngine,
        service: SettlementPostingService,
    ) -> None:
        payment = _record(commands)
        tranches = _tranches(repo, payment)
        result = service.post_sale_fee(
            INSTALLATION_ID,
            PostSaleFee(payment_id=payment.id, wiring=WIRING, date=SALE_DATE),
        )
        assert result.fee_irr == 10_000_000
        assert result.fee_vat_irr == 1_000_000
        assert result.posting is not None
        assert result.posting.kind == SettlementPostingKind.FEE_AT_SALE
        assert result.posting.amount_irr == 11_000_000
        assert result.tryton_move_id is not None
        assert result.tryton_move_id in engine.posted
        lines = engine.moves[result.tryton_move_id]
        debits = [(line.account, line.debit.amount) for line in lines if line.debit.is_positive]
        credits = [
            (line.account, line.credit.amount, line.party, line.maturity_date)
            for line in lines
            if line.credit.is_positive
        ]
        assert (FEE_EXPENSE, 10_000_000) in debits
        assert (FEE_VAT, 1_000_000) in debits
        assert len(credits) == 4
        assert all(c[0] == RECEIVABLE and c[1] == 2_750_000 for c in credits)
        assert all(c[2] == GATEWAY_PARTY for c in credits)
        assert all(c[3] is not None for c in credits)
        assert {c[3].gregorian_date for c in credits if c[3] is not None} == {
            t.expected_date for t in tranches
        }
        # The receivable now nets to 89M (gross 100M not yet seeded here —
        # the fee move alone credits 11M).
        assert engine.receivable_balance() == -11_000_000
        records = repo.list_fee_records(INSTALLATION_ID, gateway_payment_id=payment.id)
        assert records[0].tryton_fee_journal_id == result.tryton_move_id

    def test_second_post_conflicts(
        self,
        commands: SettlementCommandService,
        service: SettlementPostingService,
    ) -> None:
        payment = _record(commands)
        command = PostSaleFee(payment_id=payment.id, wiring=WIRING, date=SALE_DATE)
        service.post_sale_fee(INSTALLATION_ID, command)
        with pytest.raises(SettlementPostingExistsError):
            service.post_sale_fee(INSTALLATION_ID, command)

    def test_policy_b_refuses(
        self,
        repo: PostingFakeRepository,
        registry: FakeIntegrationsRepository,
        audit: FakeSettlementAudit,
        service: SettlementPostingService,
    ) -> None:
        ChannelRegistryService(registry).create_channel(
            INSTALLATION_ID, "digipay", "دیجی‌پی", "bnpl"
        )
        GatewayContractService(registry).create_version(
            INSTALLATION_ID,
            "digipay",
            valid_from=datetime.date(2026, 10, 23),
            fee_percent=Decimal("5"),
            fee_vat_rate=Decimal("10"),
            fee_accrual_policy="at_settlement",
            tranche_legs=(TrancheLeg(0, 5, Decimal("100")),),
            day_lag=1,
        )
        commands = SettlementCommandService(repo, registry, audit)
        result = commands.record_payment(
            CreateGatewayPayment(
                installation_id=INSTALLATION_ID,
                channel_key="digipay",
                gateway_transaction_id="TXN-D",
                gross_irr=10_000_000,
                gateway_confirmed_at=NOW,
                source=PaymentSource.STORE_WEBHOOK,
            )
        )
        with pytest.raises(SettlementPostingPreconditionError, match="at_settlement"):
            service.post_sale_fee(
                INSTALLATION_ID,
                PostSaleFee(
                    payment_id=result.payment.id, wiring=WIRING, date=SALE_DATE
                ),
            )

    def test_zero_fee_is_a_noop(
        self,
        repo: PostingFakeRepository,
        registry: FakeIntegrationsRepository,
        audit: FakeSettlementAudit,
        engine: FakeEngine,
        service: SettlementPostingService,
    ) -> None:
        ChannelRegistryService(registry).create_channel(
            INSTALLATION_ID, "freepay", "رایگان", "bnpl"
        )
        GatewayContractService(registry).create_version(
            INSTALLATION_ID,
            "freepay",
            valid_from=datetime.date(2026, 10, 23),
            fee_percent=Decimal("0"),
            fee_vat_rate=Decimal("10"),
            tranche_legs=(TrancheLeg(0, 5, Decimal("100")),),
            day_lag=1,
        )
        commands = SettlementCommandService(repo, registry, audit)
        result = commands.record_payment(
            CreateGatewayPayment(
                installation_id=INSTALLATION_ID,
                channel_key="freepay",
                gateway_transaction_id="TXN-F",
                gross_irr=10_000_000,
                gateway_confirmed_at=NOW,
                source=PaymentSource.STORE_WEBHOOK,
            )
        )
        outcome = service.post_sale_fee(
            INSTALLATION_ID,
            PostSaleFee(
                payment_id=result.payment.id, wiring=WIRING, date=SALE_DATE
            ),
        )
        assert outcome.posting is None
        assert outcome.tryton_move_id is None
        assert engine.moves == {}


class TestPostTrancheClearing:
    def test_full_clearing_reconciles_invoice_fee_and_clearing(
        self,
        commands: SettlementCommandService,
        repo: PostingFakeRepository,
        engine: FakeEngine,
        service: SettlementPostingService,
    ) -> None:
        payment, tranches = _setup_sale(commands, repo, engine, service)
        assert engine.receivable_balance() == 89_000_000
        first = _confirm(commands, tranches[0])
        result = service.post_tranche_clearing(
            INSTALLATION_ID,
            PostTrancheClearing(
                tranche_id=first.id,
                wiring=WIRING,
                payout_date=datetime.date(2026, 11, 26),
            ),
        )
        assert result.amount_irr == 22_250_000
        assert result.cleared_total_irr == 22_250_000
        assert result.payment.state == "succeeded"
        assert result.reconciliation is not None
        assert len(result.reconciliation.lines) == 3  # invoice + fee + clearing
        assert result.posting.kind == SettlementPostingKind.CLEARING
        assert result.posting.tryton_payment_id == result.payment.ref.engine_id
        assert result.posting.tryton_reconciliation_id == result.reconciliation.ref.engine_id
        assert len(engine.process_batches) == 1  # one batch call, one group
        assert result.posting.tryton_move_id in engine.posted
        assert engine.receivable_balance() == 66_750_000

    def test_same_day_sales_to_one_gateway_clear_independently(
        self,
        commands: SettlementCommandService,
        repo: PostingFakeRepository,
        engine: FakeEngine,
        service: SettlementPostingService,
    ) -> None:
        # Regression: two same-day SnappPay sales share (account,
        # party, maturity_date) on their first tranches — the gather
        # must scope each tranche's receivable lines to its own
        # payment, or the first clearing mixes both sales' lines.
        payment_a, tranches_a = _setup_sale(commands, repo, engine, service)
        payment_b, tranches_b = _setup_sale(
            commands, repo, engine, service, txn="TXN-2", gross=60_000_000
        )
        assert tranches_a[0].expected_date == tranches_b[0].expected_date
        assert engine.receivable_balance() == 89_000_000 + 53_400_000
        first_a = _confirm(commands, tranches_a[0])
        result_a = service.post_tranche_clearing(
            INSTALLATION_ID,
            PostTrancheClearing(
                tranche_id=first_a.id,
                wiring=WIRING,
                payout_date=datetime.date(2026, 11, 26),
            ),
        )
        assert result_a.amount_irr == 22_250_000
        assert result_a.reconciliation is not None
        assert len(result_a.reconciliation.lines) == 3  # A's invoice + fee + clearing
        assert engine.receivable_balance() == 66_750_000 + 53_400_000
        # B's first tranche is untouched: still expected, its lines open.
        assert _tranches(repo, payment_b)[0].state == TrancheState.EXPECTED
        first_b = _confirm(commands, tranches_b[0])
        result_b = service.post_tranche_clearing(
            INSTALLATION_ID,
            PostTrancheClearing(
                tranche_id=first_b.id,
                wiring=WIRING,
                payout_date=datetime.date(2026, 11, 26),
            ),
        )
        assert result_b.amount_irr == 13_350_000
        assert result_b.reconciliation is not None
        assert len(result_b.reconciliation.lines) == 3  # B's invoice + fee + clearing
        assert engine.receivable_balance() == 66_750_000 + 40_050_000

    def test_received_tranche_clears_late_for_the_batch_flow(
        self,
        commands: SettlementCommandService,
        repo: PostingFakeRepository,
        engine: FakeEngine,
        service: SettlementPostingService,
    ) -> None:
        # The batch flow: the file confirms and the deposit proves
        # receipt in the same step, so by tie-out time the tranche is
        # already RECEIVED — the clearing transfer still posts before
        # the bank receipt (US-525 e2e diagnosis, 2026-11-19).
        payment, tranches = _setup_sale(commands, repo, engine, service)
        assert engine.receivable_balance() == 89_000_000
        first = _confirm(commands, tranches[0])
        first = _receive(commands, first)
        assert first.state == TrancheState.RECEIVED
        result = service.post_tranche_clearing(
            INSTALLATION_ID,
            PostTrancheClearing(
                tranche_id=first.id,
                wiring=WIRING,
                payout_date=datetime.date(2026, 11, 26),
            ),
        )
        assert result.amount_irr == 22_250_000
        assert result.cleared_total_irr == 22_250_000
        assert result.payment.state == "succeeded"
        assert result.reconciliation is not None
        assert len(result.reconciliation.lines) == 3  # invoice + fee + clearing
        assert result.posting.kind == SettlementPostingKind.CLEARING
        assert engine.receivable_balance() == 66_750_000

    def test_unclearable_state_refuses(
        self,
        commands: SettlementCommandService,
        repo: PostingFakeRepository,
        engine: FakeEngine,
        service: SettlementPostingService,
    ) -> None:
        _, tranches = _setup_sale(commands, repo, engine, service)
        with pytest.raises(SettlementPostingPreconditionError, match="expected"):
            service.post_tranche_clearing(
                INSTALLATION_ID,
                PostTrancheClearing(
                    tranche_id=tranches[0].id,
                    wiring=WIRING,
                    payout_date=datetime.date(2026, 11, 26),
                ),
            )

    def test_unconfigured_journal_refuses(
        self,
        commands: SettlementCommandService,
        repo: PostingFakeRepository,
        audit: FakeSettlementAudit,
        registry: FakeIntegrationsRepository,
    ) -> None:
        engine = FakeEngine(clearing_configured=False)
        service = SettlementPostingService(
            repo, registry, cast(AccountingEnginePort, engine), audit
        )
        payment = _record(commands)
        tranches = _tranches(repo, payment)
        engine.seed_invoice_lines(tranches)
        first = _confirm(commands, tranches[0])
        with pytest.raises(SettlementPostingPreconditionError, match="clearing"):
            service.post_tranche_clearing(
                INSTALLATION_ID,
                PostTrancheClearing(
                    tranche_id=first.id,
                    wiring=WIRING,
                    payout_date=datetime.date(2026, 11, 26),
                ),
            )

    def test_partial_clearing_defers_reconciliation_until_complete(
        self,
        commands: SettlementCommandService,
        repo: PostingFakeRepository,
        engine: FakeEngine,
        service: SettlementPostingService,
    ) -> None:
        _, tranches = _setup_sale(commands, repo, engine, service)
        first = tranches[0]
        commands.transition_tranche(
            INSTALLATION_ID,
            TransitionTranche(tranche_id=first.id, to_state=TrancheState.DUE.value),
        )
        commands.transition_tranche(
            INSTALLATION_ID,
            TransitionTranche(
                tranche_id=first.id,
                to_state=TrancheState.PARTIALLY_CONFIRMED.value,
                confirmed_net_irr=20_000_000,
            ),
        )
        partial = service.post_tranche_clearing(
            INSTALLATION_ID,
            PostTrancheClearing(
                tranche_id=first.id,
                wiring=WIRING,
                payout_date=datetime.date(2026, 11, 26),
            ),
        )
        assert partial.amount_irr == 20_000_000
        assert partial.reconciliation is None
        assert engine.reconciliations == []
        # The remainder confirms; the completing clearing reconciles all four legs.
        repo.update_tranche(first.id, confirmed_net_irr=22_250_000)
        commands.transition_tranche(
            INSTALLATION_ID,
            TransitionTranche(
                tranche_id=first.id, to_state=TrancheState.CONFIRMED.value
            ),
        )
        complete = service.post_tranche_clearing(
            INSTALLATION_ID,
            PostTrancheClearing(
                tranche_id=first.id,
                wiring=WIRING,
                payout_date=datetime.date(2026, 11, 27),
            ),
        )
        assert complete.amount_irr == 2_250_000
        assert complete.cleared_total_irr == 22_250_000
        assert complete.reconciliation is not None
        assert len(complete.reconciliation.lines) == 4  # invoice + fee + 2 clearing
        # A third clearing is a no-op refusal, not a duplicate journal.
        with pytest.raises(SettlementPostingPreconditionError, match="fully cleared"):
            service.post_tranche_clearing(
                INSTALLATION_ID,
                PostTrancheClearing(
                    tranche_id=first.id,
                    wiring=WIRING,
                    payout_date=datetime.date(2026, 11, 28),
                ),
            )

    def test_missing_fee_leg_fails_reconciliation(
        self,
        commands: SettlementCommandService,
        repo: PostingFakeRepository,
        engine: FakeEngine,
        service: SettlementPostingService,
    ) -> None:
        payment = _record(commands)
        tranches = _tranches(repo, payment)
        line_ids = engine.seed_invoice_lines(tranches)
        for tranche in tranches:
            repo.update_tranche(
                tranche.id, tryton_receivable_line_id=line_ids[tranche.id]
            )
        # No post_sale_fee: the fee leg is missing, so the lines cannot sum to zero.
        first = _confirm(commands, tranches[0])
        moves_before = len(engine.moves)
        with pytest.raises(SettlementReconciliationFailedError):
            service.post_tranche_clearing(
                INSTALLATION_ID,
                PostTrancheClearing(
                    tranche_id=first.id,
                    wiring=WIRING,
                    payout_date=datetime.date(2026, 11, 26),
                ),
            )
        # The pre-check fails BEFORE any engine write: no payment, no clearing move.
        assert len(engine.moves) == moves_before
        assert engine.payments == {}

    def test_policy_b_recognises_fee_at_clearing(
        self,
        repo: PostingFakeRepository,
        registry: FakeIntegrationsRepository,
        audit: FakeSettlementAudit,
        engine: FakeEngine,
    ) -> None:
        ChannelRegistryService(registry).create_channel(
            INSTALLATION_ID, "digipay", "دیجی‌پی", "bnpl"
        )
        GatewayContractService(registry).create_version(
            INSTALLATION_ID,
            "digipay",
            valid_from=datetime.date(2026, 10, 23),
            fee_percent=Decimal("5"),
            fee_vat_rate=Decimal("10"),
            fee_accrual_policy="at_settlement",
            tranche_legs=(TrancheLeg(0, 5, Decimal("100")),),
            day_lag=1,
        )
        commands = SettlementCommandService(repo, registry, audit)
        service = SettlementPostingService(
            repo, registry, cast(AccountingEnginePort, engine), audit
        )
        result = commands.record_payment(
            CreateGatewayPayment(
                installation_id=INSTALLATION_ID,
                channel_key="digipay",
                gateway_transaction_id="TXN-D",
                gross_irr=10_000_000,
                gateway_confirmed_at=NOW,
                source=PaymentSource.STORE_WEBHOOK,
            )
        )
        payment = result.payment
        assert payment.fee_irr == 500_000
        assert payment.fee_vat_irr == 50_000
        assert payment.net_irr == 9_450_000
        tranches = _tranches(repo, payment)
        line_ids = engine.seed_invoice_lines(tranches)
        repo.update_tranche(
            tranches[0].id, tryton_receivable_line_id=line_ids[tranches[0].id]
        )
        first = _confirm(commands, tranches[0])
        outcome = service.post_tranche_clearing(
            INSTALLATION_ID,
            PostTrancheClearing(
                tranche_id=first.id,
                wiring=WIRING,
                payout_date=datetime.date(2026, 11, 26),
            ),
        )
        assert outcome.fee_posting is not None
        assert outcome.fee_posting.kind == SettlementPostingKind.FEE_AT_SETTLEMENT
        assert outcome.fee_posting.amount_irr == 550_000
        assert outcome.reconciliation is not None  # fee leg present for the sum


class TestPostBankReceipt:
    def _setup_received(
        self,
        commands: SettlementCommandService,
        repo: PostingFakeRepository,
        engine: FakeEngine,
        service: SettlementPostingService,
    ) -> tuple[GatewayPayment, SettlementTranche, str]:
        payment, tranches = _setup_sale(commands, repo, engine, service)
        first = _confirm(commands, tranches[0])
        service.post_tranche_clearing(
            INSTALLATION_ID,
            PostTrancheClearing(
                tranche_id=first.id,
                wiring=WIRING,
                payout_date=datetime.date(2026, 11, 26),
            ),
        )
        received = _receive(commands, first)
        event_id = _setup_event_with_deposit(
            repo,
            received,
            event_net_irr=22_250_000,
            deposit_irr=22_250_000,
        )
        return payment, received, event_id

    def test_full_receipt_posts_bank_move_reconciles_and_settles(
        self,
        commands: SettlementCommandService,
        repo: PostingFakeRepository,
        engine: FakeEngine,
        service: SettlementPostingService,
    ) -> None:
        _, tranche, event_id = self._setup_received(commands, repo, engine, service)
        reconciliations_before = len(engine.reconciliations)
        result = service.post_bank_receipt(
            INSTALLATION_ID,
            PostBankReceipt(
                tranche_id=tranche.id,
                wiring=WIRING,
                settlement_event_id=event_id,
                deposit_date=datetime.date(2026, 11, 26),
            ),
        )
        assert result.amount_irr == 22_250_000
        assert result.tie_out.passes
        assert result.tie_out.confirmed_net_irr == 22_250_000
        assert result.tie_out.matched_deposits_irr == 22_250_000
        assert result.reconciliation is not None
        assert len(result.reconciliation.lines) == 2  # Dr clearing + Cr clearing
        assert len(engine.reconciliations) == reconciliations_before + 1
        assert result.tranche.state == TrancheState.SETTLED
        assert result.posting.kind == SettlementPostingKind.BANK_RECEIPT
        assert result.posting.tryton_move_id is not None
        lines = engine.moves[result.posting.tryton_move_id]
        assert (BANK, 22_250_000) in [
            (line.account, line.debit.amount) for line in lines if line.debit.is_positive
        ]
        assert (CLEARING, 22_250_000) in [
            (line.account, line.credit.amount)
            for line in lines
            if line.credit.is_positive
        ]

    def test_tie_out_failure_blocks_posting(
        self,
        commands: SettlementCommandService,
        repo: PostingFakeRepository,
        engine: FakeEngine,
        service: SettlementPostingService,
    ) -> None:
        payment, tranches = _setup_sale(commands, repo, engine, service)
        first = _confirm(commands, tranches[0])
        service.post_tranche_clearing(
            INSTALLATION_ID,
            PostTrancheClearing(
                tranche_id=first.id,
                wiring=WIRING,
                payout_date=datetime.date(2026, 11, 26),
            ),
        )
        received = _receive(commands, first)
        moves_before = len(engine.moves)
        # The batch confirms TWO tranches (44.5M) but the bank delivered
        # only 22.25M, undocumented — the tie-out fails even though
        # tranche 1's own deposits cover its receipt.
        second = _confirm(commands, tranches[1])
        event_id = _setup_event_with_deposit(
            repo, received, event_net_irr=22_250_000, deposit_irr=22_250_000
        )
        repo.save_settlement_match(
            CreateSettlementMatch(
                installation_id=INSTALLATION_ID,
                tranche_id=second.id,
                allocated_amount_irr=0,
                method="exact",
                settlement_event_id=event_id,
            )
        )
        with pytest.raises(SettlementTieOutFailedError) as excinfo:
            service.post_bank_receipt(
                INSTALLATION_ID,
                PostBankReceipt(
                    tranche_id=received.id,
                    wiring=WIRING,
                    settlement_event_id=event_id,
                    deposit_date=datetime.date(2026, 11, 26),
                ),
            )
        assert excinfo.value.details is not None
        assert excinfo.value.details["difference_irr"] == 22_250_000
        assert len(engine.moves) == moves_before  # nothing posted

    def test_documented_deduction_passes_tie_out(
        self,
        commands: SettlementCommandService,
        repo: PostingFakeRepository,
        engine: FakeEngine,
        service: SettlementPostingService,
    ) -> None:
        payment, tranches = _setup_sale(commands, repo, engine, service)
        first = _confirm(commands, tranches[0])
        service.post_tranche_clearing(
            INSTALLATION_ID,
            PostTrancheClearing(
                tranche_id=first.id,
                wiring=WIRING,
                payout_date=datetime.date(2026, 11, 26),
            ),
        )
        # The bank delivered 22M of the confirmed 22.25M; the 250k
        # shortfall is a documented deduction on the event.
        received = _receive(commands, first, received_net_irr=22_000_000)
        event_id = _setup_event_with_deposit(
            repo,
            received,
            event_net_irr=22_000_000,
            deposit_irr=22_000_000,
            deductions_irr=250_000,
        )
        result = service.post_bank_receipt(
            INSTALLATION_ID,
            PostBankReceipt(
                tranche_id=received.id,
                wiring=WIRING,
                settlement_event_id=event_id,
                deposit_date=datetime.date(2026, 11, 26),
            ),
        )
        assert result.amount_irr == 22_000_000
        assert result.tie_out.passes
        assert result.tie_out.deductions_irr == 250_000
        assert result.tranche.state == TrancheState.RECEIVED  # not settled: 250k open

    def test_unmatched_deposits_refuse(
        self,
        commands: SettlementCommandService,
        repo: PostingFakeRepository,
        engine: FakeEngine,
        service: SettlementPostingService,
    ) -> None:
        payment, tranches = _setup_sale(commands, repo, engine, service)
        first = _confirm(commands, tranches[0])
        service.post_tranche_clearing(
            INSTALLATION_ID,
            PostTrancheClearing(
                tranche_id=first.id,
                wiring=WIRING,
                payout_date=datetime.date(2026, 11, 26),
            ),
        )
        received = _receive(commands, first)
        event = repo.save_settlement_event(
            CreateSettlementEvent(
                installation_id=INSTALLATION_ID,
                channel_key="snapppay",
                payout_date=datetime.date(2026, 11, 26),
                net_irr=22_250_000,
                source="file",
            ),
            channel_id="snapppay",
        )
        with pytest.raises(SettlementPostingPreconditionError, match="matched"):
            service.post_bank_receipt(
                INSTALLATION_ID,
                PostBankReceipt(
                    tranche_id=received.id,
                    wiring=WIRING,
                    settlement_event_id=event.id,
                    deposit_date=datetime.date(2026, 11, 26),
                ),
            )

    def test_partial_receipt_defers_settlement_and_reconciliation(
        self,
        commands: SettlementCommandService,
        repo: PostingFakeRepository,
        engine: FakeEngine,
        service: SettlementPostingService,
    ) -> None:
        payment, tranches = _setup_sale(commands, repo, engine, service)
        first = _confirm(commands, tranches[0])
        service.post_tranche_clearing(
            INSTALLATION_ID,
            PostTrancheClearing(
                tranche_id=first.id,
                wiring=WIRING,
                payout_date=datetime.date(2026, 11, 26),
            ),
        )
        commands.transition_tranche(
            INSTALLATION_ID,
            TransitionTranche(
                tranche_id=first.id,
                to_state=TrancheState.PARTIALLY_RECEIVED.value,
                received_net_irr=20_000_000,
            ),
        )
        received = repo.get_tranche(INSTALLATION_ID, first.id)
        assert received is not None
        event_id = _setup_event_with_deposit(
            repo, received, event_net_irr=20_000_000, deposit_irr=20_000_000
        )
        # Tie-out: confirmed 22.25M vs deposits 20M — the 2.25M gap is
        # the not-yet-received remainder, so this event's tie-out as
        # computed over the event's matches only counts THIS tranche's
        # confirmed 22.25M... the batch must still tie: the event
        # carries the remainder as an open item, not a deduction.
        # For the unit test, the remainder arrives in the same batch
        # as a second deposit matched to the same event.
        deposit2 = repo.save_bank_deposit(
            CreateBankDeposit(
                installation_id=INSTALLATION_ID,
                deposit_date=datetime.date(2026, 11, 26),
                amount_irr=2_250_000,
            )
        )
        repo.save_settlement_match(
            CreateSettlementMatch(
                installation_id=INSTALLATION_ID,
                tranche_id=received.id,
                allocated_amount_irr=2_250_000,
                method="exact",
                bank_deposit_id=deposit2.id,
                settlement_event_id=event_id,
            )
        )
        partial = service.post_bank_receipt(
            INSTALLATION_ID,
            PostBankReceipt(
                tranche_id=received.id,
                wiring=WIRING,
                settlement_event_id=event_id,
                deposit_date=datetime.date(2026, 11, 26),
            ),
        )
        assert partial.amount_irr == 20_000_000
        assert partial.reconciliation is None  # 2.25M still open on clearing
        assert partial.tranche.state == TrancheState.PARTIALLY_RECEIVED
        # The remainder arrives and settles the tranche.
        repo.update_tranche(received.id, received_net_irr=22_250_000)
        commands.transition_tranche(
            INSTALLATION_ID,
            TransitionTranche(
                tranche_id=received.id, to_state=TrancheState.RECEIVED.value
            ),
        )
        complete = service.post_bank_receipt(
            INSTALLATION_ID,
            PostBankReceipt(
                tranche_id=received.id,
                wiring=WIRING,
                settlement_event_id=event_id,
                deposit_date=datetime.date(2026, 11, 27),
            ),
        )
        assert complete.amount_irr == 2_250_000
        assert complete.reconciliation is not None
        assert len(complete.reconciliation.lines) == 3  # Dr + 2 Cr clearing legs
        assert complete.tranche.state == TrancheState.SETTLED


class TestPostFeeVariance:
    def test_higher_fee_posts_adjustment_and_exception(
        self,
        commands: SettlementCommandService,
        repo: PostingFakeRepository,
        engine: FakeEngine,
        service: SettlementPostingService,
        audit: FakeSettlementAudit,
    ) -> None:
        payment, tranches = _setup_sale(commands, repo, engine, service)
        result = service.post_fee_variance(
            INSTALLATION_ID,
            PostFeeVariance(
                payment_id=payment.id,
                wiring=WIRING,
                actual_fee_irr=11_000_000,
                actual_vat_irr=1_100_000,
                reason="Gateway file shows 11% fee for Aban",
                date=datetime.date(2026, 11, 26),
                event_ref="file:snapppay-aban",
            ),
            tenant_id=TENANT_ID,
        )
        assert result.fee_variance_irr == 1_000_000
        assert result.vat_variance_irr == 100_000
        assert result.posting is not None
        assert result.posting.kind == SettlementPostingKind.FEE_VARIANCE
        assert result.posting.amount_irr == 1_100_000
        assert result.exception is not None
        assert result.exception.exception_type.value == "fee_mismatch"
        assert result.exception.expected_amount_irr == 11_000_000
        assert result.exception.actual_amount_irr == 12_100_000
        assert result.tryton_move_id is not None
        lines = engine.moves[result.tryton_move_id]
        assert (FEE_EXPENSE, 1_000_000) in [
            (line.account, line.debit.amount) for line in lines if line.debit.is_positive
        ]
        assert (FEE_VAT, 100_000) in [
            (line.account, line.debit.amount) for line in lines if line.debit.is_positive
        ]
        # Allocated across the 4 open tranches by fee share (equal here).
        assert len(result.allocations) == 4
        assert all(fee == 250_000 and vat == 25_000 for _, fee, vat in result.allocations)
        updated = _tranches(repo, payment)
        assert all(t.net_irr == 21_975_000 for t in updated)
        assert all(t.fee_irr == 2_750_000 for t in updated)
        records = repo.list_fee_records(INSTALLATION_ID, gateway_payment_id=payment.id)
        assert records[0].actual_fee_irr == 11_000_000
        assert records[0].fee_variance_irr == 1_000_000
        assert records[0].source == "file"
        assert audit.entries[-1]["action"] == "settlement.fee_variance"

    def test_lower_fee_reverses_expense(
        self,
        commands: SettlementCommandService,
        repo: PostingFakeRepository,
        engine: FakeEngine,
        service: SettlementPostingService,
    ) -> None:
        payment, _ = _setup_sale(commands, repo, engine, service)
        result = service.post_fee_variance(
            INSTALLATION_ID,
            PostFeeVariance(
                payment_id=payment.id,
                wiring=WIRING,
                actual_fee_irr=9_000_000,
                actual_vat_irr=900_000,
                reason="Promotional fee month",
                date=datetime.date(2026, 11, 26),
            ),
        )
        assert result.fee_variance_irr == -1_000_000
        assert result.tryton_move_id is not None
        lines = engine.moves[result.tryton_move_id]
        assert (FEE_EXPENSE, 1_000_000) in [
            (line.account, line.credit.amount)
            for line in lines
            if line.credit.is_positive
        ]
        # The receivable legs are debits (the gateway owes us back).
        assert all(
            line.debit.is_positive
            for line in lines
            if line.account == RECEIVABLE
        )

    def test_zero_variance_is_a_noop(
        self,
        commands: SettlementCommandService,
        repo: PostingFakeRepository,
        engine: FakeEngine,
        service: SettlementPostingService,
    ) -> None:
        payment, _ = _setup_sale(commands, repo, engine, service)
        moves_before = len(engine.moves)
        result = service.post_fee_variance(
            INSTALLATION_ID,
            PostFeeVariance(
                payment_id=payment.id,
                wiring=WIRING,
                actual_fee_irr=10_000_000,
                actual_vat_irr=1_000_000,
                reason="File matches contract",
                date=datetime.date(2026, 11, 26),
            ),
        )
        assert result.posting is None
        assert result.exception is None
        assert len(engine.moves) == moves_before

    def test_missing_reason_refuses(
        self,
        commands: SettlementCommandService,
        repo: PostingFakeRepository,
        engine: FakeEngine,
        service: SettlementPostingService,
    ) -> None:
        payment, _ = _setup_sale(commands, repo, engine, service)
        with pytest.raises(SettlementPostingPreconditionError, match="reason"):
            service.post_fee_variance(
                INSTALLATION_ID,
                PostFeeVariance(
                    payment_id=payment.id,
                    wiring=WIRING,
                    actual_fee_irr=11_000_000,
                    actual_vat_irr=1_100_000,
                    reason="  ",
                    date=datetime.date(2026, 11, 26),
                ),
            )

    def test_tranche_scoped_variance_adjusts_only_that_tranche(
        self,
        commands: SettlementCommandService,
        repo: PostingFakeRepository,
        engine: FakeEngine,
        service: SettlementPostingService,
    ) -> None:
        payment, tranches = _setup_sale(commands, repo, engine, service)
        first = tranches[0]
        result = service.post_fee_variance(
            INSTALLATION_ID,
            PostFeeVariance(
                payment_id=payment.id,
                wiring=WIRING,
                actual_fee_irr=3_000_000,
                actual_vat_irr=300_000,
                reason="First tranche fee corrected by gateway",
                date=datetime.date(2026, 11, 26),
                tranche_id=first.id,
            ),
        )
        assert result.fee_variance_irr == 500_000  # 3.0M vs 2.5M expected
        assert result.allocations == ((first.id, 500_000, 50_000),)
        updated = _tranches(repo, payment)
        assert updated[0].net_irr == 22_250_000 - 550_000
        assert all(t.net_irr == 22_250_000 for t in updated[1:])


class TestApplyRefund:
    def test_partial_refund_deducts_last_open_tranche_first(
        self,
        commands: SettlementCommandService,
        repo: PostingFakeRepository,
        engine: FakeEngine,
        service: SettlementPostingService,
    ) -> None:
        payment, tranches = _setup_sale(commands, repo, engine, service)
        result = service.apply_refund(
            INSTALLATION_ID,
            ApplyRefund(
                payment_id=payment.id,
                wiring=WIRING,
                refund_amount_irr=30_000_000,
                reason="Customer returned part of the order",
                date=datetime.date(2026, 11, 20),
                event_ref="refund:evt-1",
            ),
            tenant_id=TENANT_ID,
        )
        # 22.25M from tranche 4, 7.75M from tranche 3; 1 and 2 untouched.
        assert result.allocations == (
            (tranches[3].id, 22_250_000),
            (tranches[2].id, 7_750_000),
        )
        assert result.excess_irr == 0
        assert result.exception is None
        updated = _tranches(repo, payment)
        assert updated[3].net_irr == 0
        assert updated[3].state == TrancheState.CANCELLED
        assert updated[2].net_irr == 14_500_000
        assert updated[2].gross_irr == 17_250_000
        assert updated[2].fee_irr == 2_500_000  # fee unchanged
        assert updated[0].net_irr == 22_250_000
        assert result.posting.tryton_move_id is not None
        lines = engine.moves[result.posting.tryton_move_id]
        assert (REVENUE, 30_000_000) in [
            (line.account, line.debit.amount) for line in lines if line.debit.is_positive
        ]
        receivable_credits = [
            (line.credit.amount, line.maturity_date)
            for line in lines
            if line.account == RECEIVABLE
        ]
        assert sorted(a for a, _ in receivable_credits) == [7_750_000, 22_250_000]
        assert all(m is not None for _, m in receivable_credits)
        assert result.posting.reason == "Customer returned part of the order"
        assert result.posting.event_ref == "refund:evt-1"

    def test_refund_beyond_open_balance_becomes_excess(
        self,
        commands: SettlementCommandService,
        repo: PostingFakeRepository,
        engine: FakeEngine,
        service: SettlementPostingService,
    ) -> None:
        payment, tranches = _setup_sale(commands, repo, engine, service)
        # Confirm tranche 1: it is no longer open, so only 3 tranches
        # (66.75M) can absorb the refund.
        _confirm(commands, tranches[0])
        result = service.apply_refund(
            INSTALLATION_ID,
            ApplyRefund(
                payment_id=payment.id,
                wiring=WIRING,
                refund_amount_irr=80_000_000,
                reason="Full order return after first settlement",
                date=datetime.date(2026, 11, 27),
            ),
        )
        assert sum(take for _, take in result.allocations) == 66_750_000
        assert result.excess_irr == 13_250_000
        assert result.exception is not None
        assert result.exception.exception_type.value == "cancelled_after_settlement"
        updated_payment = repo.get_payment(INSTALLATION_ID, payment.id)
        assert updated_payment is not None
        assert updated_payment.refunds_deducted_irr == 13_250_000
        updated = _tranches(repo, payment)
        assert updated[0].state == TrancheState.CONFIRMED  # untouched
        assert updated[0].net_irr == 22_250_000
        assert result.posting.tryton_move_id is not None
        lines = engine.moves[result.posting.tryton_move_id]
        unmatured = [
            line
            for line in lines
            if line.account == RECEIVABLE and line.maturity_date is None
        ]
        assert len(unmatured) == 1
        assert unmatured[0].credit.amount == 13_250_000

    def test_non_positive_refund_refuses(
        self,
        commands: SettlementCommandService,
        repo: PostingFakeRepository,
        engine: FakeEngine,
        service: SettlementPostingService,
    ) -> None:
        payment, _ = _setup_sale(commands, repo, engine, service)
        with pytest.raises(SettlementPostingPreconditionError):
            service.apply_refund(
                INSTALLATION_ID,
                ApplyRefund(
                    payment_id=payment.id,
                    wiring=WIRING,
                    refund_amount_irr=0,
                    reason="Zero refund",
                    date=datetime.date(2026, 11, 20),
                ),
            )
