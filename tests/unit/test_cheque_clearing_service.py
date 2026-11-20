"""Service tests for cheque clearing (US-534, Layer 3, §1.5).

DB-free: the clearing service runs over the US-531 cheque fakes
(extended with statement + move-line reads), an in-memory bank
repository fake and a recording review queue. They pin: exact
auto-clear + clearing-leg reconciliation, probable/ambiguous review
filing (never guessed), unmatched staying in_clearing, the
issued-cheque deferral to US-532, human confirm of a probable
match, idempotent re-runs, and stale flagging without auto-bounce.
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass, field
from typing import Any, cast

import pytest

from hesabyar.application.commands.cheque_clearing import ChequeClearingService
from hesabyar.application.commands.cheques import ChequeCommandService
from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.domain.accounting.commands import ReconcileMoveLines
from hesabyar.domain.accounting.ports import AccountingEnginePort
from hesabyar.domain.accounting.refs import (
    AccountRef,
    BankStatementRef,
    CompanyRef,
    JournalDraftRef,
    MoveLineRef,
    PartyRef,
    ReconciliationRef,
    StatementJournalRef,
)
from hesabyar.domain.accounting.value_objects import (
    BankStatementLineView,
    BankStatementView,
    MoveLineView,
    ReconciliationView,
    StatementJournalView,
)
from hesabyar.domain.bank.models import BankStatementMatch, MatchStatus
from hesabyar.domain.cheques.errors import ChequeValidationError
from hesabyar.domain.cheques.models import ChequeSettings, ChequeState
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.domain.review_queue.models import (
    ReviewItem,
    ReviewItemStatus,
)
from tests.unit.test_cheque_services import (
    ACTOR,
    COMPANY_ENGINE_ID,
    INSTALLATION_ID,
    TENANT_ID,
    TODAY,
    FakeChequeRepository,
    FakeEngine,
    _in_clearing,
    _service,
)

COMPANY = CompanyRef(engine_id=COMPANY_ENGINE_ID)
DESTINATION_ACCOUNT = 42
CLEARING_ACCOUNT = 800
HELD_ACCOUNT = 700
AMOUNT_IRR = 17_500_000  # _payload amount_toman 1,750,000 x10


@dataclass
class RecordingReviewQueue:
    """Review-queue command fake that returns real items."""

    items: list[ReviewItem] = field(default_factory=list)

    def file_item(self, **kwargs: Any) -> ReviewItem:
        item = ReviewItem(
            id=str(uuid.uuid4()),
            tenant_id=kwargs["tenant_id"],
            item_type=kwargs["item_type"],
            severity=kwargs["severity"],
            status=ReviewItemStatus.OPEN,
            reason=kwargs["reason"],
            suggested_action=kwargs["suggested_action"],
            created_at=datetime.datetime.now(datetime.UTC),
            company_id=kwargs.get("company_id"),
            subtype=kwargs.get("subtype"),
            source_event_ref=kwargs.get("source_event_ref"),
            amount_irr=kwargs.get("amount_irr"),
        )
        self.items.append(item)
        return item

    def perform_action(self, **kwargs: Any) -> ReviewItem:
        item = next(i for i in self.items if i.id == kwargs["item_id"])
        return item


@dataclass
class FakeBankRepo:
    """In-memory bank repository (match records only)."""

    matches: list[BankStatementMatch] = field(default_factory=list)

    def list_matches_for_statement(
        self, tenant_id: str, statement_id: int
    ) -> list[BankStatementMatch]:
        return [
            m
            for m in self.matches
            if m.tenant_id == tenant_id and m.statement_id == statement_id
        ]

    def save_match(self, match: BankStatementMatch) -> BankStatementMatch:
        self.matches.append(match)
        return match

    def update_match(self, match: BankStatementMatch) -> BankStatementMatch:
        for index, stored in enumerate(self.matches):
            if stored.id == match.id:
                self.matches[index] = match
                return match
        self.matches.append(match)
        return match


@dataclass
class ClearingFakeEngine(FakeEngine):
    """US-531 fake engine + statement / move-line reads for clearing."""

    statements: dict[int, BankStatementView] = field(default_factory=dict)
    journals: list[StatementJournalView] = field(default_factory=list)
    reconciled: list[tuple[int, ...]] = field(default_factory=list)
    fail_reconcile_once: bool = False
    amounts: dict[int, int] = field(default_factory=dict)
    move_transition: dict[int, str] = field(default_factory=dict)
    move_cheque: dict[int, int] = field(default_factory=dict)

    def create_engine_cheque(self, command):  # type: ignore[no-untyped-def]
        view = super().create_engine_cheque(command)
        self.amounts[view.ref.engine_id] = command.amount.amount
        return view

    def transition_engine_cheque(self, command):  # type: ignore[no-untyped-def]
        view = super().transition_engine_cheque(command)
        move_id = view.move_ids.get(command.transition)
        if move_id is not None:
            self.move_transition[move_id] = command.transition
            self.move_cheque[move_id] = command.cheque.engine_id
        return view

    def get_bank_statement(self, statement: BankStatementRef) -> BankStatementView | None:
        return self.statements.get(statement.engine_id)

    def list_statement_journals(self, company: CompanyRef) -> list[StatementJournalView]:
        return list(self.journals)

    def list_move_lines(self, company: CompanyRef, **kwargs: Any) -> list[MoveLineView]:
        move = kwargs.get("move")
        if move is None:
            return []
        transition = self.move_transition.get(move.engine_id)
        engine_id = self.move_cheque.get(move.engine_id)
        if transition is None or engine_id is None:
            return []
        amount = self.amounts[engine_id]
        if transition == "deposit":
            return [
                self._move_line(9101, move.engine_id, CLEARING_ACCOUNT, amount, 0),
                self._move_line(9102, move.engine_id, HELD_ACCOUNT, 0, amount),
            ]
        if transition == "clear":
            return [
                self._move_line(9103, move.engine_id, DESTINATION_ACCOUNT, amount, 0),
                self._move_line(9104, move.engine_id, CLEARING_ACCOUNT, 0, amount),
            ]
        return []

    @staticmethod
    def _move_line(
        line_id: int, move_id: int, account_id: int, debit: int, credit: int
    ) -> MoveLineView:
        return MoveLineView(
            ref=MoveLineRef(line_id),
            move=JournalDraftRef(move_id),
            account=AccountRef(account_id),
            debit=MoneyIRR(debit),
            credit=MoneyIRR(credit),
            state="valid",
            party=PartyRef(1),
        )

    def reconcile_move_lines(self, command: ReconcileMoveLines) -> ReconciliationView:
        if self.fail_reconcile_once:
            self.fail_reconcile_once = False
            raise RuntimeError("simulated reconcile outage")
        self.reconciled.append(tuple(line.engine_id for line in command.lines))
        return ReconciliationView(
            ref=ReconciliationRef(4242), number="R-1", lines=command.lines
        )


@dataclass
class World:
    repo: FakeChequeRepository
    engine: ClearingFakeEngine
    bank: FakeBankRepo
    review: RecordingReviewQueue
    cheques: ChequeCommandService
    clearing: ChequeClearingService


def _world(*, window_days: int | None = 7) -> World:
    repo = FakeChequeRepository()
    engine = ClearingFakeEngine()
    engine.journals = [
        StatementJournalView(
            ref=StatementJournalRef(700),
            company=COMPANY,
            name="بانک سامان",
            account=AccountRef(DESTINATION_ACCOUNT),
            validation="balance",
            currency_code="IRR",
            bank_account_number="IR062060000000100324200001",
        )
    ]
    bank = FakeBankRepo()
    review = RecordingReviewQueue()
    cheques = _service((repo, engine, review), today=TODAY)  # type: ignore[arg-type]
    repo.save_settings(
        ChequeSettings(
            installation_id=INSTALLATION_ID, clearing_window_days=window_days
        )
    )
    clearing = ChequeClearingService(
        cast(AccountingEnginePort, engine),
        cast(Any, repo),
        cast(Any, bank),
        cheques,
        cast(ReviewQueueCommandService, review),
        None,
        today=TODAY,
    )
    return World(repo, engine, bank, review, cheques, clearing)


def _statement(world: World, *, line_id: int = 9001, **line_overrides: object) -> int:
    line_base: dict[str, object] = {
        "id": line_id,
        "date": FiscalDate(datetime.date(2026, 11, 20)),
        "amount": MoneyIRR(AMOUNT_IRR),
        "account": AccountRef(DESTINATION_ACCOUNT),
        "description": "وصول چک 123456",
    }
    line_base.update(line_overrides)
    statement_id = 700
    world.engine.statements[statement_id] = BankStatementView(
        ref=BankStatementRef(statement_id),
        company=COMPANY,
        name="صورت‌حساب آبان",
        journal_name="بانک سامان",
        journal_id=700,
        date=FiscalDate(datetime.date(2026, 11, 20)),
        state="draft",
        validation="balance",
        currency_code="IRR",
        start_balance=None,
        end_balance=None,
        balance=None,
        total_amount=None,
        number_of_lines=1,
        lines=(BankStatementLineView(**line_base),),  # type: ignore[arg-type]
    )
    return statement_id


def _run(world: World, statement_id: int, **kwargs: object):
    return world.clearing.match_statement(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        company=COMPANY,
        company_id="company-row-1",
        statement_id=statement_id,
        actor=ACTOR,
        **kwargs,  # type: ignore[arg-type]
    )


def test_exact_match_auto_clears_and_reconciles_clearing_legs() -> None:
    world = _world()
    cheque = _in_clearing(world.cheques)
    # The leaf number on the registry row is None by default; set it
    # to the number the statement description carries.
    cheque.leaf_number = "123456"
    world.repo.save(cheque)
    statement_id = _statement(world)
    result = _run(world, statement_id)
    assert result.exact_count == 1
    assert result.cleared_count == 1
    assert result.review_items_filed == 0
    cleared = world.repo.get(cheque.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID)
    assert cleared is not None
    assert cleared.state == ChequeState.CLEARED
    assert cleared.clearing_confidence == "exact"
    assert cleared.clearing_statement_ref == f"statement:{statement_id}:line:9001"
    assert cleared.clearing_reconciliation_id == 4242
    # The deposit move's clearing debit (9101) reconciled with the
    # clear move's clearing credit (9104).
    assert world.engine.reconciled == [(9101, 9104)]
    assert world.bank.matches[0].status == MatchStatus.AUTO_APPLIED


def test_probable_match_goes_to_review_without_clearing() -> None:
    world = _world()
    cheque = _in_clearing(world.cheques)
    statement_id = _statement(world, description="واریز وجه")
    result = _run(world, statement_id)
    assert result.probable_count == 1
    assert result.cleared_count == 0
    assert result.review_items_filed == 1
    assert world.review.items[0].subtype == "clearing_probable"
    still = world.repo.get(cheque.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID)
    assert still is not None
    assert still.state == ChequeState.IN_CLEARING
    assert world.bank.matches[0].status == MatchStatus.SUGGESTED


def test_ambiguous_match_never_guesses() -> None:
    world = _world()
    first = _in_clearing(world.cheques)
    second = _in_clearing(world.cheques, sayad_id="9999888877776666", order_id="ord-2")
    statement_id = _statement(world, description="واریز وجه")
    result = _run(world, statement_id)
    assert result.ambiguous_count == 1
    assert result.cleared_count == 0
    assert world.review.items[0].subtype == "clearing_ambiguous"
    assert first.sayad_id in world.review.items[0].reason
    assert second.sayad_id in world.review.items[0].reason
    for cheque in (first, second):
        stored = world.repo.get(cheque.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID)
        assert stored is not None
        assert stored.state == ChequeState.IN_CLEARING


def test_unmatched_line_leaves_the_cheque_in_clearing() -> None:
    world = _world()
    cheque = _in_clearing(world.cheques)
    statement_id = _statement(world, amount=MoneyIRR(AMOUNT_IRR + 10))
    result = _run(world, statement_id)
    assert result.unmatched_count == 1
    assert result.cleared_count == 0
    assert result.review_items_filed == 0  # the generic bank reconcile owns the line
    stored = world.repo.get(cheque.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID)
    assert stored is not None
    assert stored.state == ChequeState.IN_CLEARING
    assert world.bank.matches[0].status == MatchStatus.UNMATCHED


def test_human_confirm_of_a_probable_match_clears() -> None:
    world = _world()
    cheque = _in_clearing(world.cheques)
    statement_id = _statement(world, description="واریز وجه")
    _run(world, statement_id)
    decision = world.clearing.confirm_match(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        company=COMPANY,
        company_id="company-row-1",
        cheque_id=cheque.id,
        statement_id=statement_id,
        line_id=9001,
        actor=ACTOR,
    )
    assert decision.action == "cleared"
    assert decision.reconciliation_id == 4242
    stored = world.repo.get(cheque.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID)
    assert stored is not None
    assert stored.state == ChequeState.CLEARED
    assert stored.clearing_confidence == "probable"


def test_confirm_without_a_suggested_match_fails_closed() -> None:
    world = _world()
    cheque = _in_clearing(world.cheques)
    statement_id = _statement(world)
    with pytest.raises(ChequeValidationError, match="No suggested clearing match"):
        world.clearing.confirm_match(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            company=COMPANY,
            company_id="company-row-1",
            cheque_id=cheque.id,
            statement_id=statement_id,
            line_id=9001,
            actor=ACTOR,
        )


def test_rerun_is_idempotent_per_line() -> None:
    world = _world()
    cheque = _in_clearing(world.cheques)
    cheque.leaf_number = "123456"
    world.repo.save(cheque)
    statement_id = _statement(world)
    first = _run(world, statement_id)
    second = _run(world, statement_id)
    assert first.cleared_count == 1
    assert second.cleared_count == 0
    assert second.lines[0].action == "already_matched"
    assert len(world.bank.matches) == 1


def test_reconcile_failure_keeps_the_clear_and_files_a_follow_up() -> None:
    world = _world()
    world.engine.fail_reconcile_once = True
    cheque = _in_clearing(world.cheques)
    cheque.leaf_number = "123456"
    world.repo.save(cheque)
    statement_id = _statement(world)
    result = _run(world, statement_id)
    assert result.cleared_count == 1
    stored = world.repo.get(cheque.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID)
    assert stored is not None
    assert stored.state == ChequeState.CLEARED
    assert stored.clearing_reconciliation_id is None
    assert any(
        item.subtype == "clearing_reconcile_failed" for item in world.review.items
    )


def test_melli_bank_key_caps_exact_at_probable() -> None:
    world = _world()
    cheque = _in_clearing(world.cheques)
    cheque.leaf_number = "123456"
    world.repo.save(cheque)
    statement_id = _statement(world)
    result = _run(world, statement_id, bank_key="melli")
    assert result.probable_count == 1
    assert result.cleared_count == 0


def test_aging_flags_stale_without_bouncing() -> None:
    world = _world(window_days=7)
    cheque = _in_clearing(world.cheques)
    # Deposit was 2026-11-19 (TODAY); age it by moving the clock.
    aged_service = ChequeClearingService(
        cast(AccountingEnginePort, world.engine),
        cast(Any, world.repo),
        cast(Any, world.bank),
        world.cheques,
        cast(ReviewQueueCommandService, world.review),
        None,
        today=datetime.date(2026, 11, 30),
    )
    rows = aged_service.aging_report(
        tenant_id=TENANT_ID, installation_id=INSTALLATION_ID
    )
    assert len(rows) == 1
    assert rows[0].days_in_clearing == 11
    assert rows[0].stale is True
    report = aged_service.flag_stale_in_clearing(
        tenant_id=TENANT_ID, installation_id=INSTALLATION_ID, actor=ACTOR
    )
    assert report.stale == 1
    assert report.items_filed == 1
    assert world.review.items[0].subtype == "stale_in_clearing"
    # Still in_clearing — flagging never bounces.
    stored = world.repo.get(cheque.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID)
    assert stored is not None
    assert stored.state == ChequeState.IN_CLEARING
