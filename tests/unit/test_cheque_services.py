"""Service tests for the cheque lifecycle (US-531 + US-532).

DB-free: the command service runs against an in-memory fake of the
cheque repository port, a fake engine port that mirrors
``account_check_ir`` (one atomic state+ journal per transition,
moves accumulated per cheque), a recording review fake and a stub
policy gate. The SQLAlchemy repository has its own DB tests
(``tests/infrastructure/test_cheque_repository.py``); the real
engine is covered by contract tests once US-530 installs the module.
"""

from __future__ import annotations

import copy
import datetime
import uuid
from dataclasses import dataclass, field
from typing import Any, cast

import pytest

from hesabyar.application.commands.cheques import ChequeCommandService
from hesabyar.application.commands.integrations import PolicyGateService
from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.domain.accounting.commands import CreateEngineCheque, TransitionEngineCheque
from hesabyar.domain.accounting.ports import AccountingEnginePort
from hesabyar.domain.accounting.refs import CheckbookEngineRef, ChequeEngineRef, CompanyRef
from hesabyar.domain.accounting.value_objects import CheckbookEngineView, ChequeEngineView
from hesabyar.domain.cheques.errors import (
    ChequeDuplicateError,
    ChequeEngineError,
    ChequeNotFoundError,
    ChequePreAcceptanceError,
    ChequeRegistryDivergenceError,
    ChequeTransitionNotAllowedError,
    ChequeValidationError,
)
from hesabyar.domain.cheques.models import (
    Cheque,
    ChequeKind,
    ChequePurpose,
    ChequeSettings,
    ChequeState,
    CustodyLogEntry,
    InquiryReceipt,
    InstallmentLegStatus,
    RedBrownPolicy,
    StorageLocation,
)
from hesabyar.domain.cheques.ports import InstallmentLegOutcomeHook
from hesabyar.domain.cheques.state_machine import journal_design_for

TENANT_ID = "00000000-0000-0000-0000-000000000531"
INSTALLATION_ID = "00000000-0000-0000-0000-000000000532"
OTHER_INSTALLATION_ID = "00000000-0000-0000-0000-000000000533"
COMPANY_ENGINE_ID = 2
ACTOR = "operator-1"
TODAY = datetime.date(2026, 11, 19)


def _payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "sayad_id": "1234567890123456",
        "amount_toman": 1750000,
        "amount_currency": "IRT",
        "issue_date": "1405-08-26",
        "due_date": "1405-10-15",
        "issuer_name": "مشتری نمونه",
        "issuer_national_id": "0012345678",
        "issuer_bank": "056 سامان",
        "payee_name": "کافه ایوان",
        "payee_national_id": "00123456789",
        "purpose": "payment",
        "order_id": "ord-1",
        "customer_ref": "cust-1",
        "sayad_registered": True,
    }
    base.update(overrides)
    return base


# -- fakes ---------------------------------------------------------------------


@dataclass
class FakeChequeRepository:
    """In-memory cheque repository (live-sayad rule mirrors the SQL one)."""

    cheques: dict[str, Cheque] = field(default_factory=dict)
    custody: list[CustodyLogEntry] = field(default_factory=list)
    receipts: list[InquiryReceipt] = field(default_factory=list)
    settings: dict[str, ChequeSettings] = field(default_factory=dict)
    fail_save_once: bool = False

    def create(self, cheque: Cheque) -> Cheque:
        self.cheques[cheque.id] = copy.deepcopy(cheque)
        return cheque

    def get(
        self, cheque_id: str, *, tenant_id: str, installation_id: str
    ) -> Cheque | None:
        cheque = self.cheques.get(cheque_id)
        if cheque is None or cheque.tenant_id != tenant_id:
            return None
        if cheque.installation_id != installation_id:
            return None
        # A fresh load, like a DB row: mutating it must not change the
        # stored snapshot until save() succeeds (atomicity tests rely
        # on this — a failed save leaves the stored row untouched).
        return copy.deepcopy(cheque)

    def get_by_sayad_id(
        self, sayad_id: str, *, tenant_id: str, installation_id: str
    ) -> Cheque | None:
        for cheque in self.cheques.values():
            if (
                cheque.sayad_id == sayad_id
                and cheque.tenant_id == tenant_id
                and cheque.installation_id == installation_id
                and cheque.state
                not in (
                    ChequeState.VOIDED,
                    ChequeState.EXPIRED_UNRECEIVED,
                    ChequeState.REJECTED_MISMATCH,
                )
            ):
                return copy.deepcopy(cheque)
        return None

    def find_by_sayad_id(
        self, sayad_id: str, *, tenant_id: str, installation_id: str
    ) -> Cheque | None:
        matches = [
            c
            for c in self.cheques.values()
            if c.sayad_id == sayad_id
            and c.tenant_id == tenant_id
            and c.installation_id == installation_id
        ]
        if not matches:
            return None
        return copy.deepcopy(max(matches, key=lambda c: c.created_at))

    def find_in_clearing_cheques(self) -> list[Cheque]:
        return [
            copy.deepcopy(c)
            for c in self.cheques.values()
            if c.state is ChequeState.IN_CLEARING
        ]

    def list_by_installment_leg_id(
        self, leg_id: str, *, tenant_id: str, installation_id: str
    ) -> list[Cheque]:
        rows = [
            c
            for c in self.cheques.values()
            if c.installment_leg_id == leg_id
            and c.tenant_id == tenant_id
            and c.installation_id == installation_id
        ]
        rows.sort(key=lambda c: c.created_at, reverse=True)
        return [copy.deepcopy(c) for c in rows]

    def list(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        state: ChequeState | None = None,
        issuer_national_id: str | None = None,
        due_before: datetime.date | None = None,
        kind: ChequeKind | None = None,
        purpose: ChequePurpose | None = None,
        endorsee_type: str | None = None,
        storage_location: StorageLocation | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Cheque]:
        rows = [
            c
            for c in self.cheques.values()
            if c.tenant_id == tenant_id and c.installation_id == installation_id
        ]
        if state is not None:
            rows = [c for c in rows if c.state == state]
        if issuer_national_id is not None:
            rows = [c for c in rows if c.issuer_national_id == issuer_national_id]
        if due_before is not None:
            rows = [c for c in rows if c.due_date.date <= due_before]
        if kind is not None:
            rows = [c for c in rows if c.kind == kind]
        if purpose is not None:
            rows = [c for c in rows if c.purpose == purpose]
        if endorsee_type is not None:
            rows = [c for c in rows if c.endorsee_type == endorsee_type]
        if storage_location is not None:
            rows = [c for c in rows if c.storage_location == storage_location]
        rows.sort(key=lambda c: c.created_at, reverse=True)
        return [copy.deepcopy(c) for c in rows[offset : offset + limit]]

    def save(self, cheque: Cheque) -> Cheque:
        if self.fail_save_once:
            self.fail_save_once = False
            raise RuntimeError("simulated registry write failure")
        self.cheques[cheque.id] = copy.deepcopy(cheque)
        return cheque

    def add_custody_entry(self, entry: CustodyLogEntry) -> CustodyLogEntry:
        self.custody.append(entry)
        return entry

    def list_custody_entries(self, cheque_id: str) -> list[CustodyLogEntry]:
        return [e for e in self.custody if e.cheque_id == cheque_id]

    def add_inquiry_receipt(self, receipt: InquiryReceipt) -> InquiryReceipt:
        self.receipts.append(receipt)
        return receipt

    def list_inquiry_receipts(self, cheque_id: str) -> list[InquiryReceipt]:
        return [r for r in self.receipts if r.cheque_id == cheque_id]

    def get_settings(self, installation_id: str) -> ChequeSettings | None:
        return self.settings.get(installation_id)

    def save_settings(self, settings: ChequeSettings) -> ChequeSettings:
        self.settings[settings.installation_id] = settings
        return settings

    def list_registered_before(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        registered_before: datetime.datetime,
    ) -> list[Cheque]:
        return [
            copy.deepcopy(c)
            for c in self.cheques.values()
            if c.tenant_id == tenant_id
            and c.installation_id == installation_id
            and c.state == ChequeState.REGISTERED
            and c.created_at < registered_before
        ]

    def find_registered_cheques(self) -> list[Cheque]:
        return [
            copy.deepcopy(c)
            for c in self.cheques.values()
            if c.state == ChequeState.REGISTERED
        ]


def created_kind(record: dict[str, Any]) -> str:
    """The kind of the cheque a fake engine record was created with."""
    return str(record["command"].kind)


@dataclass
class FakeEngine:
    """Mirrors account_check_ir: one atomic state + journal per transition.

    ``state``/``move_ids`` are kept per engine cheque; a journal move
    is created only when the transition has a §2.4 design and the
    journal is not deferred (endorsement to a non-supplier).
    """

    records: dict[int, dict[str, Any]] = field(default_factory=dict)
    next_engine_id: int = 1000
    next_move_id: int = 5000
    auto_posts: list[tuple[str, bool]] = field(default_factory=list)
    fail_create_once: bool = False
    fail_transition_once: bool = False
    checkbooks: dict[int, CheckbookEngineView] = field(default_factory=dict)

    def seed_checkbook(self, view: CheckbookEngineView) -> None:
        self.checkbooks[view.ref.engine_id] = view

    def list_engine_checkbooks(self, company: CompanyRef) -> list[CheckbookEngineView]:
        return [
            view
            for view in sorted(
                self.checkbooks.values(),
                key=lambda v: v.ref.engine_id,
                reverse=True,
            )
            if view.company.engine_id == company.engine_id
        ]

    def get_engine_checkbook(
        self, company: CompanyRef, checkbook: CheckbookEngineRef
    ) -> CheckbookEngineView | None:
        view = self.checkbooks.get(checkbook.engine_id)
        if view is None or view.company.engine_id != company.engine_id:
            return None
        return view

    def create_engine_cheque(self, command: CreateEngineCheque) -> ChequeEngineView:
        if self.fail_create_once:
            self.fail_create_once = False
            raise ChequeEngineError("simulated engine outage")
        engine_id = self.next_engine_id
        self.next_engine_id += 1
        self.records[engine_id] = {
            "command": command,
            "state": ChequeState.REGISTERED.value,
            "purpose": command.purpose,
            "move_ids": {},
        }
        return ChequeEngineView(
            ref=ChequeEngineRef(engine_id=engine_id),
            company=command.company,
            kind=command.kind,
            purpose=command.purpose,
            sayad_id=command.sayad_id,
            state=ChequeState.REGISTERED.value,
            sayad_status="unregistered",
            amount=command.amount,
            issue_date=command.issue_date,
            due_date=command.due_date,
            move_ids={},
        )

    def transition_engine_cheque(self, command: TransitionEngineCheque) -> ChequeEngineView:
        if self.fail_transition_once:
            self.fail_transition_once = False
            raise ChequeEngineError("simulated engine failure")
        record = self.records[command.cheque.engine_id]
        from hesabyar.domain.cheques.state_machine import target_state

        expected = target_state(
            ChequeState(record["state"]), command.transition
        )
        self.auto_posts.append((command.transition, command.auto_post))
        move_ids: dict[str, int] = dict(record["move_ids"])
        # سند ۶ applies only to held -> returned; bounced -> returned
        # carries NO journal (§2.1) — the journal design is keyed by
        # transition, so the from-state decides here. The design is
        # kind/purpose-aware (collateral confirm/return take the
        # memorandum سند ۷/۸; issued clear/bounce take سند ۱۱/۱۲).
        design = journal_design_for(
            command.transition,
            command.kind or created_kind(record),
            record["purpose"],
        )
        journals = design is not None and not (
            command.transition == "return" and record["state"] == "bounced"
        )
        if command.transition == "endorsed_bounce":
            # §9.1 #3: the reverse-of-endorsement journal is
            # accountant-gated — no journal is ever guessed.
            journals = False
        if journals and not command.defer_journal:
            if command.transition == "call_in":
                # Two simultaneous journals in one transaction:
                # سند ۹الف (memorandum reversal) + سند ۹ب (commercial).
                move_ids["call_in_reversal"] = self.next_move_id
                self.next_move_id += 1
                move_ids["call_in"] = self.next_move_id
                self.next_move_id += 1
            else:
                move_ids[command.transition] = self.next_move_id
                self.next_move_id += 1
        # A call-in commercialises the cheque: the engine flips its
        # purpose to ``payment`` in the same transaction (§2.2).
        purpose = record["purpose"]
        if command.transition in ("call_in", "unexpected_call_in"):
            purpose = ChequePurpose.PAYMENT.value
        record["state"] = expected.value
        record["purpose"] = purpose
        record["move_ids"] = move_ids
        created = record["command"]
        return ChequeEngineView(
            ref=ChequeEngineRef(engine_id=command.cheque.engine_id),
            company=command.company,
            kind=created.kind,
            purpose=purpose,
            sayad_id=created.sayad_id,
            state=expected.value,
            sayad_status="confirmed"
            if command.transition == "confirm_receipt"
            else "transferred"
            if command.transition == "endorse"
            else "unregistered",
            amount=created.amount,
            issue_date=created.issue_date,
            due_date=created.due_date,
            move_ids=move_ids,
        )


@dataclass
class RecordingReview:
    """Records file_item calls (the review-queue command service seam)."""

    items: list[dict[str, Any]] = field(default_factory=list)

    def file_item(self, **kwargs: Any) -> None:
        self.items.append(dict(kwargs))


@dataclass
class RecordingLegHook:
    """Records leg-outcome hook calls (US-542) — optionally failing."""

    calls: list[dict[str, Any]] = field(default_factory=list)
    fail: bool = False

    def on_leg_outcome(
        self,
        *,
        cheque: Cheque,
        outcome: InstallmentLegStatus,
        event_date: datetime.date,
    ) -> None:
        if self.fail:
            raise RuntimeError("simulated hook failure")
        self.calls.append(
            {
                "cheque_id": cheque.id,
                "leg_id": cheque.installment_leg_id,
                "outcome": outcome,
                "event_date": event_date,
            }
        )


@dataclass
class StubGate:
    """Policy gate stub: allow or deny every journal, recording the calls."""

    allowed: bool
    calls: list[dict[str, Any]] = field(default_factory=list)

    def evaluate(
        self,
        installation_id: str,
        *,
        event_type: str,
        company: CompanyRef,
        event_date: datetime.date,
        amount_irr: int,
    ) -> Any:
        from types import SimpleNamespace

        self.calls.append(
            {
                "installation_id": installation_id,
                "event_type": event_type,
                "company": company,
                "event_date": event_date,
                "amount_irr": amount_irr,
            }
        )
        return SimpleNamespace(allowed=self.allowed)


@pytest.fixture()
def world() -> tuple[FakeChequeRepository, FakeEngine, RecordingReview]:
    return FakeChequeRepository(), FakeEngine(), RecordingReview()


def _service(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
    *,
    gate: StubGate | None = None,
    today: datetime.date | None = None,
    leg_hook: InstallmentLegOutcomeHook | None = None,
) -> ChequeCommandService:
    repo, engine, review = world
    # The fakes implement only the port methods the service uses;
    # cast them to the full port types (the service never calls the
    # rest, and the real implementations are covered by DB/contract
    # tests).
    return ChequeCommandService(
        repo,
        cast(AccountingEnginePort, engine),
        gate=cast(PolicyGateService, gate) if gate is not None else None,
        review=cast(ReviewQueueCommandService, review),
        today=today,
        leg_hook=leg_hook,
    )


def _register(
    service: ChequeCommandService, **overrides: object
) -> Cheque:
    return service.register_manual(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        company_engine_id=COMPANY_ENGINE_ID,
        payload=_payload(**overrides),
        actor=ACTOR,
    )


def _held(service: ChequeCommandService, **overrides: object) -> Cheque:
    cheque = _register(service, **overrides)
    return service.confirm_receipt(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        cheque_id=cheque.id,
        actor=ACTOR,
        items_matched=True,
        sayad_accepted=True,
        color_code=2,
    )


def _in_clearing(service: ChequeCommandService, **overrides: object) -> Cheque:
    cheque = _held(service, **overrides)
    return service.deposit(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        cheque_id=cheque.id,
        actor=ACTOR,
        destination_account_id=42,
        destination_bank_name="بانک سامان",
    )


# -- registration ----------------------------------------------------------------


def test_register_creates_registered_row_with_no_journal_and_engine_link(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    service = _service(world)
    cheque = _register(service)
    assert cheque.state is ChequeState.REGISTERED
    assert cheque.move_ids == {}
    assert cheque.engine_id is not None
    assert cheque.amount_irr == 17_500_000
    assert cheque.storage_location is None  # no custody until the leaf arrives


def test_register_duplicate_live_sayad_is_rejected(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    """US-533 convergence: identical re-registration is idempotent OK;
    a DIFFERENT payload for a live sayad_id is a conflict (review + raise)."""
    service = _service(world)
    first = _register(service)
    again = _register(service)
    assert again.id == first.id  # identical payload -> the same record
    with pytest.raises(ChequeDuplicateError):
        _register(service, amount_toman=9990000)
    _, _, review = world
    assert review.items[-1]["subtype"] == "duplicate_sayad_id"


def test_register_invalid_payload_is_rejected_before_any_write(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    repo, _, _ = world
    service = _service(world)
    with pytest.raises(ChequeValidationError):
        _register(service, sayad_id="bad")
    assert repo.cheques == {}


def test_engine_link_failure_keeps_the_claim_and_retries_on_reregister(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    repo, engine, _ = world
    engine.fail_create_once = True
    service = _service(world)
    with pytest.raises(ChequeEngineError):
        _register(service)
    # The intake claim is kept, unlinked.
    rows = list(repo.cheques.values())
    assert len(rows) == 1 and rows[0].engine_id is None
    # Re-registering the same sayad_id retries the link, no duplicate.
    cheque = _register(service)
    assert cheque.engine_id is not None
    assert len(repo.cheques) == 1


def test_expired_registration_can_be_reregistered_as_a_new_record(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    repo, _, _ = world
    service = _service(world)
    cheque = _register(service)
    # Age the stored row (the service returns its own copy; the
    # repository holds the snapshot). Anchor the age to the fixed job
    # date (TODAY), never to the wall clock — the daily job computes
    # age against the date it is run with, so a wall-clock anchor
    # drifts one day per day (same defect as the four daily-job tests
    # that failed in CI run #1; this one passed on 2026-11-20 with
    # exactly one day of margin and would have failed on 2026-11-21).
    repo.cheques[cheque.id].created_at = datetime.datetime(
        TODAY.year, TODAY.month, TODAY.day, tzinfo=datetime.UTC
    ) - datetime.timedelta(days=8)
    service.run_daily_cheque_job(TODAY)
    assert repo.cheques[cheque.id].state is ChequeState.EXPIRED_UNRECEIVED
    new_cheque = _register(service)
    assert new_cheque.id != cheque.id
    assert new_cheque.state is ChequeState.REGISTERED


def test_unknown_cheque_is_404(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    service = _service(world)
    with pytest.raises(ChequeNotFoundError):
        service.get_cheque(
            str(uuid.uuid4()), tenant_id=TENANT_ID, installation_id=INSTALLATION_ID
        )


# -- confirm receipt: registered -> held (سند ۱) ----------------------------------


def test_confirm_receipt_moves_to_held_with_sand_1_and_custody(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    repo, engine, _ = world
    service = _service(world)
    cheque = _register(service)
    held = service.confirm_receipt(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        cheque_id=cheque.id,
        actor=ACTOR,
        items_matched=True,
        sayad_accepted=True,
        color_code=2,
        storage_location=StorageLocation.CASHBOX,
    )
    assert held.state is ChequeState.HELD
    assert held.move_ids["confirm_receipt"] == 5000
    assert held.storage_location is StorageLocation.CASHBOX
    assert held.receipt_missing_warning is True  # no receipt attached yet
    entries = repo.list_custody_entries(cheque.id)
    assert len(entries) == 1
    assert entries[0].from_location is None
    assert entries[0].to_location is StorageLocation.CASHBOX
    # The engine got the draft default (no gate wired).
    assert engine.auto_posts[-1] == ("confirm_receipt", False)


def test_confirm_receipt_requires_sayad_acceptance(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    service = _service(world)
    cheque = _register(service)
    with pytest.raises(ChequeValidationError, match="Sayyad"):
        service.confirm_receipt(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            cheque_id=cheque.id,
            actor=ACTOR,
            items_matched=True,
            sayad_accepted=False,
        )
    assert service.get_cheque(
        cheque.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID
    ).state is ChequeState.REGISTERED


def test_confirm_receipt_never_confirms_on_mismatch(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    service = _service(world)
    cheque = _register(service)
    with pytest.raises(ChequePreAcceptanceError):
        service.confirm_receipt(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            cheque_id=cheque.id,
            actor=ACTOR,
            items_matched=False,
            sayad_accepted=True,
        )
    assert service.get_cheque(
        cheque.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID
    ).state is ChequeState.REGISTERED


def test_red_colour_requires_manager_approval_then_passes_with_it(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    service = _service(world)
    cheque = _register(service)
    with pytest.raises(ChequePreAcceptanceError, match="manager"):
        service.confirm_receipt(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            cheque_id=cheque.id,
            actor=ACTOR,
            items_matched=True,
            sayad_accepted=True,
            color_code=5,
        )
    held = service.confirm_receipt(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        cheque_id=cheque.id,
        actor=ACTOR,
        items_matched=True,
        sayad_accepted=True,
        color_code=5,
        manager_approval="مدیر مالی",
    )
    assert held.state is ChequeState.HELD
    assert held.manager_approval_by == "مدیر مالی"


def test_red_colour_rejected_outright_when_policy_says_reject(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    repo, _, _ = world
    repo.save_settings(
        ChequeSettings(
            installation_id=INSTALLATION_ID, red_brown_policy=RedBrownPolicy.REJECT
        )
    )
    service = _service(world)
    cheque = _register(service)
    with pytest.raises(ChequePreAcceptanceError):
        service.confirm_receipt(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            cheque_id=cheque.id,
            actor=ACTOR,
            items_matched=True,
            sayad_accepted=True,
            color_code=4,
            manager_approval="مدیر مالی",
        )


def test_confirm_receipt_with_inline_receipt_clears_the_warning(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    repo, _, _ = world
    service = _service(world)
    cheque = _register(service)
    held = service.confirm_receipt(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        cheque_id=cheque.id,
        actor=ACTOR,
        items_matched=True,
        sayad_accepted=True,
        color_code=1,  # white: accepted with a warning, not a guarantee
        inquiry_receipt={
            "channel": "finnotech_api",
            "inquiry_type": "items_match",
            "result_code": "matched",
            "result_summary": "هر چهار قلم تطابق دارد",
            "reference": "inq-2026-11-19-001",
            "cost_irr": 3500,
        },
    )
    assert held.state is ChequeState.HELD
    assert held.receipt_missing_warning is False
    assert len(repo.list_inquiry_receipts(cheque.id)) == 1


def test_reject_mismatch_is_a_no_journal_terminal_state(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    service = _service(world)
    cheque = _register(service)
    rejected = service.reject_mismatch(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        cheque_id=cheque.id,
        actor=ACTOR,
        mismatch_detail="مبلغ چک با سامانه صیاد تطابق ندارد",
    )
    assert rejected.state is ChequeState.REJECTED_MISMATCH
    assert rejected.move_ids == {}
    assert rejected.mismatch_detail is not None
    assert rejected.mismatch_detail.startswith("مبلغ")
    with pytest.raises(ChequeTransitionNotAllowedError):
        service.confirm_receipt(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            cheque_id=cheque.id,
            actor=ACTOR,
            items_matched=True,
            sayad_accepted=True,
        )


# -- deposit / clear / bounce / endorse / return ----------------------------------


def test_deposit_moves_leaf_to_the_bank_with_sand_2(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    repo, _, _ = world
    service = _service(world)
    cheque = _held(service)
    clearing = service.deposit(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        cheque_id=cheque.id,
        actor=ACTOR,
        destination_account_id=42,
        destination_bank_name="بانک سامان",
    )
    assert clearing.state is ChequeState.IN_CLEARING
    assert clearing.move_ids["deposit"] == 5001
    assert clearing.storage_location is StorageLocation.BANK
    assert clearing.destination_bank_account_id == 42
    assert clearing.destination_bank_name == "بانک سامان"
    entries = repo.list_custody_entries(cheque.id)
    assert entries[-1].to_location is StorageLocation.BANK


def test_clear_marks_the_linked_installment_leg_paid(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    service = _service(world)
    cheque = _in_clearing(service, installment_leg_id="leg-7")
    cleared = service.clear(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        cheque_id=cheque.id,
        actor=ACTOR,
        statement_reference="stmt-2026-11-19",
    )
    assert cleared.state is ChequeState.CLEARED
    assert cleared.move_ids["clear"] == 5002
    assert cleared.installment_leg_status is InstallmentLegStatus.PAID


def test_bounce_records_reason_date_certificate_and_files_dunning(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    _, _, review = world
    service = _service(world)
    cheque = _in_clearing(service, installment_leg_id="leg-7")
    bounced = service.bounce(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        cheque_id=cheque.id,
        actor=ACTOR,
        bounce_reason="عدم کفایت موجودی",
        bounce_date="1405-10-20",
        certificate_reference="cert-2026-11-19-1",
    )
    assert bounced.state is ChequeState.BOUNCED
    assert bounced.move_ids["bounce"] == 5002
    assert bounced.bounce_reason == "عدم کفایت موجودی"
    assert bounced.bounce_date is not None
    assert bounced.bounce_date.date.isoformat() == "2027-01-10"
    assert bounced.bounce_certificate_reference == "cert-2026-11-19-1"
    assert bounced.dunning_notified_at is not None
    assert bounced.installment_leg_status is InstallmentLegStatus.UNPAID
    assert any(
        item["subtype"] == "cheque_bounced" for item in review.items
    )


# -- the leg-outcome hook (US-542) ------------------------------------------------------


def test_clear_fires_the_leg_outcome_hook_with_paid(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    hook = RecordingLegHook()
    service = _service(world, today=TODAY, leg_hook=hook)
    cheque = _in_clearing(service, installment_leg_id="leg-7")
    service.clear(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        cheque_id=cheque.id,
        actor=ACTOR,
    )
    assert hook.calls == [
        {
            "cheque_id": cheque.id,
            "leg_id": "leg-7",
            "outcome": InstallmentLegStatus.PAID,
            "event_date": TODAY,
        }
    ]


def test_bounce_fires_the_leg_outcome_hook_with_unpaid_and_the_bounce_date(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    hook = RecordingLegHook()
    service = _service(world, today=TODAY, leg_hook=hook)
    cheque = _in_clearing(service, installment_leg_id="leg-7")
    service.bounce(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        cheque_id=cheque.id,
        actor=ACTOR,
        bounce_reason="عدم کفایت موجودی",
        bounce_date="1405-10-20",
        certificate_reference="cert-1",
    )
    assert hook.calls == [
        {
            "cheque_id": cheque.id,
            "leg_id": "leg-7",
            "outcome": InstallmentLegStatus.UNPAID,
            "event_date": datetime.date(2027, 1, 10),
        }
    ]


def test_hook_is_not_fired_without_a_leg_link(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    hook = RecordingLegHook()
    service = _service(world, today=TODAY, leg_hook=hook)
    cheque = _in_clearing(service)
    service.clear(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        cheque_id=cheque.id,
        actor=ACTOR,
    )
    assert hook.calls == []


def test_hook_failure_never_masks_the_transition(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    _, _, review = world
    hook = RecordingLegHook(fail=True)
    service = _service(world, today=TODAY, leg_hook=hook)
    cheque = _in_clearing(service, installment_leg_id="leg-7")
    cleared = service.clear(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        cheque_id=cheque.id,
        actor=ACTOR,
    )
    assert cleared.state is ChequeState.CLEARED
    assert cleared.installment_leg_status is InstallmentLegStatus.PAID
    assert any(
        item["subtype"] == "installment_collection_hook_failed"
        for item in review.items
    )


def test_bounce_then_return_carries_no_journal(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    service = _service(world)
    cheque = _in_clearing(service)
    bounced = service.bounce(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        cheque_id=cheque.id,
        actor=ACTOR,
        bounce_reason="عدم کفایت موجودی",
        bounce_date="1405-10-20",
        certificate_reference="cert-1",
    )
    returned = service.return_cheque(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        cheque_id=cheque.id,
        actor=ACTOR,
    )
    assert returned.state is ChequeState.RETURNED
    assert returned.move_ids == bounced.move_ids  # no new move
    assert "return" not in returned.move_ids
    assert returned.storage_location is StorageLocation.RETURNED_TO_ISSUER


def test_bounced_representment_stays_disabled(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    service = _service(world)
    cheque = _in_clearing(service)
    service.bounce(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        cheque_id=cheque.id,
        actor=ACTOR,
        bounce_reason="عدم کفایت موجودی",
        bounce_date="1405-10-20",
        certificate_reference="cert-1",
    )
    with pytest.raises(ChequeTransitionNotAllowedError):
        service.deposit(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            cheque_id=cheque.id,
            actor=ACTOR,
            destination_account_id=42,
        )


def test_endorse_to_supplier_posts_sand_5(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    service = _service(world)
    cheque = _held(service)
    endorsed = service.endorse(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        cheque_id=cheque.id,
        actor=ACTOR,
        endorsee_name="تامین‌کننده الف",
        endorsee_type="supplier",
        sayad_transfer_reference="transfer-1",
    )
    assert endorsed.state is ChequeState.ENDORSED
    assert endorsed.move_ids["endorse"] == 5001
    assert endorsed.journal_pending_review is False
    assert endorsed.storage_location is StorageLocation.WITH_ENDORSEE
    assert endorsed.sayad_transfer_reference == "transfer-1"


def test_endorse_to_non_supplier_defers_the_journal_and_files_review(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    _, _, review = world
    service = _service(world)
    cheque = _held(service)
    endorsed = service.endorse(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        cheque_id=cheque.id,
        actor=ACTOR,
        endorsee_name="شخص ثالث",
        endorsee_type="other",
        sayad_transfer_reference="transfer-2",
    )
    assert endorsed.state is ChequeState.ENDORSED
    assert "endorse" not in endorsed.move_ids  # no journal guessed
    assert endorsed.journal_pending_review is True
    assert any(
        item["subtype"] == "endorsement_to_other_journal" for item in review.items
    )


def test_endorse_requires_a_sayad_transfer_reference(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    service = _service(world)
    cheque = _held(service)
    with pytest.raises(ChequeValidationError):
        service.endorse(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            cheque_id=cheque.id,
            actor=ACTOR,
            endorsee_name="تامین‌کننده الف",
            endorsee_type="supplier",
            sayad_transfer_reference="   ",
        )


def test_return_from_held_posts_sand_6(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    service = _service(world)
    cheque = _held(service)
    returned = service.return_cheque(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        cheque_id=cheque.id,
        actor=ACTOR,
    )
    assert returned.state is ChequeState.RETURNED
    assert returned.move_ids["return"] == 5001
    assert returned.storage_location is StorageLocation.RETURNED_TO_ISSUER


def test_collateral_cheque_refuses_commercial_transitions(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    # US-532: confirm_receipt IS valid for collateral — registered ->
    # held takes the memorandum branch (سند ۷), not the commercial one.
    service = _service(world)
    cheque = _register(service, purpose="collateral")
    held = service.confirm_receipt(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        cheque_id=cheque.id,
        actor=ACTOR,
        items_matched=True,
        sayad_accepted=True,
    )
    assert held.state is ChequeState.HELD
    assert "confirm_receipt" in held.move_ids
    # But the commercial deposit is refused while it stays collateral.
    with pytest.raises(ChequeTransitionNotAllowedError):
        service.deposit(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            cheque_id=cheque.id,
            actor=ACTOR,
            destination_account_id=42,
        )


# -- atomicity ----------------------------------------------------------------------


def test_engine_failure_leaves_neither_journal_nor_state(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    repo, engine, _ = world
    service = _service(world)
    cheque = _held(service)
    engine.fail_transition_once = True
    with pytest.raises(ChequeEngineError):
        service.deposit(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            cheque_id=cheque.id,
            actor=ACTOR,
            destination_account_id=42,
        )
    stored = repo.cheques[cheque.id]
    assert stored.state is ChequeState.HELD
    assert "deposit" not in stored.move_ids
    assert stored.storage_location is StorageLocation.VAULT
    assert repo.list_custody_entries(cheque.id)[-1].to_location is StorageLocation.VAULT


def test_registry_failure_after_engine_success_files_divergence(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    repo, _, review = world
    service = _service(world)
    cheque = _held(service)
    repo.fail_save_once = True
    with pytest.raises(ChequeRegistryDivergenceError):
        service.deposit(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            cheque_id=cheque.id,
            actor=ACTOR,
            destination_account_id=42,
        )
    # The engine moved; the registry did not — and the divergence was filed.
    assert any(
        item["subtype"] == "registry_divergence" for item in review.items
    )
    assert repo.cheques[cheque.id].state is ChequeState.HELD


# -- policy gate ----------------------------------------------------------------------


def test_gate_denied_keeps_the_journal_a_draft(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    repo, engine, _ = world
    gate = StubGate(allowed=False)
    service = _service(world, gate=gate)
    cheque = _register(service)
    service.confirm_receipt(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        cheque_id=cheque.id,
        actor=ACTOR,
        items_matched=True,
        sayad_accepted=True,
    )
    assert engine.auto_posts[-1] == ("confirm_receipt", False)
    assert gate.calls[0]["event_type"] == "cheque.transition.held"
    assert gate.calls[0]["amount_irr"] == 17_500_000


def test_gate_allowed_posts_the_journal(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    _, engine, _ = world
    gate = StubGate(allowed=True)
    service = _service(world, gate=gate)
    cheque = _register(service)
    service.confirm_receipt(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        cheque_id=cheque.id,
        actor=ACTOR,
        items_matched=True,
        sayad_accepted=True,
    )
    assert engine.auto_posts[-1] == ("confirm_receipt", True)


# -- custody + receipts -----------------------------------------------------------------


def test_custody_move_is_not_a_journal(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    repo, _, _ = world
    service = _service(world)
    cheque = _held(service)
    moved = service.move_custody(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        cheque_id=cheque.id,
        actor=ACTOR,
        to_location=StorageLocation.CASHBOX,
        reason="تحویل به صندوق",
    )
    assert moved.storage_location is StorageLocation.CASHBOX
    assert moved.move_ids == {"confirm_receipt": 5000}
    entries = repo.list_custody_entries(cheque.id)
    assert len(entries) == 2
    assert entries[-1].from_location is StorageLocation.VAULT
    assert entries[-1].to_location is StorageLocation.CASHBOX


def test_custody_move_refuses_non_held_cheques(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    service = _service(world)
    cheque = _register(service)
    with pytest.raises(ChequeValidationError):
        service.move_custody(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            cheque_id=cheque.id,
            actor=ACTOR,
            to_location=StorageLocation.CASHBOX,
            reason="x",
        )


def test_inquiry_receipt_stores_verdict_only(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    repo, _, _ = world
    service = _service(world)
    cheque = _register(service)
    receipt = service.record_inquiry_receipt(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        cheque_id=cheque.id,
        actor=ACTOR,
        channel="finnotech_api",
        inquiry_type="color",
        result_code="white",
        result_summary="رنگ سفید — تضمین نیست",
        reference="inq-2",
        cost_irr=3500,
    )
    assert receipt.cost_irr == 3500
    assert receipt.result_code == "white"
    assert len(repo.list_inquiry_receipts(cheque.id)) == 1


# -- daily job ----------------------------------------------------------------------------


def _register_aged(
    service: ChequeCommandService,
    repo: FakeChequeRepository,
    *,
    days_old: int,
    sayad_id: str,
) -> Cheque:
    cheque = _register(service, sayad_id=sayad_id)
    # Anchor the age to the fixed job date (TODAY), never to the wall
    # clock: the daily job computes age against the date it is run
    # with, so a wall-clock anchor drifts by one day per day and the
    # day-3 / day-6 / day-7 milestones stop lining up (CI run #1,
    # 2026-11-20, failed all four daily-job tests this way).
    cheque.created_at = datetime.datetime(
        TODAY.year, TODAY.month, TODAY.day, tzinfo=datetime.UTC
    ) - datetime.timedelta(days=days_old)
    repo.cheques[cheque.id] = cheque
    return cheque


def test_daily_job_reminds_day_3_and_day_6_then_expires_day_7(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    repo, _, review = world
    service = _service(world)
    _register_aged(service, repo, days_old=3, sayad_id="1111111111111111")
    _register_aged(service, repo, days_old=6, sayad_id="2222222222222222")
    day7 = _register_aged(service, repo, days_old=7, sayad_id="3333333333333333")
    fresh = _register_aged(service, repo, days_old=1, sayad_id="4444444444444444")

    report = service.run_daily_cheque_job(TODAY)
    assert report.reminders_sent == 2
    assert report.expired == 1
    assert report.skipped == 0
    assert repo.cheques[day7.id].state is ChequeState.EXPIRED_UNRECEIVED
    assert repo.cheques[day7.id].move_ids == {}  # expiry: no journal
    assert repo.cheques[fresh.id].state is ChequeState.REGISTERED
    assert any(
        item["subtype"] == "never_arrived_reminder_day3" for item in review.items
    )
    assert any(
        item["subtype"] == "never_arrived_reminder_final" for item in review.items
    )


def test_daily_job_reminders_are_idempotent(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    repo, _, review = world
    service = _service(world)
    _register_aged(service, repo, days_old=3, sayad_id="1111111111111111")
    first = service.run_daily_cheque_job(TODAY)
    second = service.run_daily_cheque_job(TODAY)
    assert first.reminders_sent == 1
    assert second.reminders_sent == 0
    assert len(review.items) == 1


def test_daily_job_skips_a_cheque_whose_expiry_fails(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    repo, engine, _ = world
    service = _service(world)
    cheque = _register_aged(service, repo, days_old=7, sayad_id="3333333333333333")
    engine.fail_transition_once = True
    report = service.run_daily_cheque_job(TODAY)
    assert report.expired == 0
    assert report.skipped == 1
    assert repo.cheques[cheque.id].state is ChequeState.REGISTERED
    # The next run retries it.
    report = service.run_daily_cheque_job(TODAY)
    assert report.expired == 1


# -- US-532: issued + collateral -----------------------------------------------


def _register_issued(
    service: ChequeCommandService, **overrides: object
) -> Cheque:
    return service.register_manual(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        company_engine_id=COMPANY_ENGINE_ID,
        payload=_payload(**overrides),
        actor=ACTOR,
        kind=ChequeKind.ISSUED,
        checkbook_engine_id=7,
    )


def _issued(service: ChequeCommandService, **overrides: object) -> Cheque:
    cheque = _register_issued(service, **overrides)
    return service.issue_cheque(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        cheque_id=cheque.id,
        actor=ACTOR,
        destination_account_id=42,
        destination_bank_name="بانک سامان",
    )


def _collateral_held(service: ChequeCommandService, **overrides: object) -> Cheque:
    cheque = _register(service, purpose="collateral", **overrides)
    return service.confirm_receipt(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        cheque_id=cheque.id,
        actor=ACTOR,
        items_matched=True,
        sayad_accepted=True,
    )


def _transition_kwargs(cheque: Cheque) -> dict[str, Any]:
    return {
        "tenant_id": TENANT_ID,
        "installation_id": INSTALLATION_ID,
        "cheque_id": cheque.id,
        "actor": ACTOR,
    }


def test_register_issued_requires_a_checkbook(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    service = _service(world)
    with pytest.raises(ChequeValidationError):
        service.register_manual(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            company_engine_id=COMPANY_ENGINE_ID,
            payload=_payload(),
            actor=ACTOR,
            kind=ChequeKind.ISSUED,
        )


def test_register_received_rejects_a_checkbook(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    service = _service(world)
    with pytest.raises(ChequeValidationError):
        service.register_manual(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            company_engine_id=COMPANY_ENGINE_ID,
            payload=_payload(),
            actor=ACTOR,
            kind=ChequeKind.RECEIVED,
            checkbook_engine_id=7,
        )


def test_register_issued_records_checkbook_and_obligation(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    service = _service(world)
    cheque = service.register_manual(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        company_engine_id=COMPANY_ENGINE_ID,
        payload=_payload(purpose="collateral"),
        actor=ACTOR,
        kind=ChequeKind.ISSUED,
        checkbook_engine_id=7,
        secured_obligation="قرارداد اجاره ۱۴۰۵/۰۳",
    )
    assert cheque.kind is ChequeKind.ISSUED
    assert cheque.purpose is ChequePurpose.COLLATERAL
    assert cheque.checkbook_engine_id == 7
    assert cheque.secured_obligation == "قرارداد اجاره ۱۴۰۵/۰۳"


def test_issue_posts_sand_10_and_records_the_clearing_account(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    service = _service(world)
    cheque = _issued(service)
    assert cheque.state is ChequeState.ISSUED
    assert "issue" in cheque.move_ids
    assert cheque.destination_bank_account_id == 42
    assert cheque.destination_bank_name == "بانک سامان"


def test_issue_collateral_keeps_the_collateral_purpose(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    service = _service(world)
    cheque = _issued(service, purpose="collateral")
    assert cheque.state is ChequeState.ISSUED
    assert cheque.purpose is ChequePurpose.COLLATERAL
    assert "issue" in cheque.move_ids  # the memorandum سند ۷


def test_issued_clear_posts_sand_11(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    service = _service(world)
    cheque = _issued(service)
    cleared = service.clear(**_transition_kwargs(cheque))
    assert cleared.state is ChequeState.CLEARED
    assert "clear" in cleared.move_ids


def test_issued_bounce_needs_no_certificate_and_files_a_review(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    _, _, review = world
    service = _service(world)
    cheque = _issued(service)
    bounced = service.bounce(
        **_transition_kwargs(cheque),
        bounce_reason="عدم کفایت موجودی",
        bounce_date="1405-10-20",
        certificate_reference=None,
    )
    assert bounced.state is ChequeState.BOUNCED
    assert "bounce" in bounced.move_ids
    assert bounced.dunning_notified_at is None  # no dunning for our own cheque
    assert any(
        item["subtype"] == "issued_cheque_bounced" for item in review.items
    )


def test_issued_bounce_without_a_reason_is_refused(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    service = _service(world)
    cheque = _issued(service)
    with pytest.raises(ChequeValidationError):
        service.bounce(
            **_transition_kwargs(cheque),
            bounce_reason="  ",
            bounce_date="1405-10-20",
            certificate_reference=None,
        )


def test_received_bounce_still_needs_the_certificate(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    service = _service(world)
    cheque = _in_clearing(service)
    with pytest.raises(ChequeValidationError):
        service.bounce(
            **_transition_kwargs(cheque),
            bounce_reason="عدم کفایت موجودی",
            bounce_date="1405-10-20",
            certificate_reference=None,
        )


def test_void_requires_a_reason_marks_the_leaf_and_files_a_review(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    _, _, review = world
    service = _service(world)
    cheque = _issued(service)
    voided = service.void_issued(
        **_transition_kwargs(cheque), void_reason="اشتباه در مبلغ"
    )
    assert voided.state is ChequeState.VOIDED
    assert "void" in voided.move_ids
    assert voided.void_reason == "اشتباه در مبلغ"
    assert any(item["subtype"] == "cheque_voided" for item in review.items)


def test_void_without_a_reason_is_refused(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    service = _service(world)
    cheque = _issued(service)
    with pytest.raises(ChequeValidationError):
        service.void_issued(**_transition_kwargs(cheque), void_reason="  ")


def test_call_in_needs_the_accountants_approval(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    service = _service(world)
    cheque = _collateral_held(service)
    with pytest.raises(ChequeValidationError):
        service.call_in(**_transition_kwargs(cheque), accountant_approval="  ")


def test_call_in_posts_two_moves_flips_the_purpose_and_files_a_review(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    _, _, review = world
    service = _service(world)
    cheque = _collateral_held(service)
    called = service.call_in(
        **_transition_kwargs(cheque), accountant_approval="حسابدار ارشد"
    )
    assert called.state is ChequeState.CALLED_IN
    assert "call_in_reversal" in called.move_ids  # سند ۹الف
    assert "call_in" in called.move_ids  # سند ۹ب
    assert called.purpose is ChequePurpose.PAYMENT
    assert called.call_in_approved_by == "حسابدار ارشد"
    assert called.call_in_approved_at is not None
    assert any(
        item["subtype"] == "collateral_called_in" for item in review.items
    )


def test_called_in_cheque_follows_the_commercial_cycle(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    service = _service(world)
    cheque = _collateral_held(service)
    called = service.call_in(
        **_transition_kwargs(cheque), accountant_approval="حسابدار ارشد"
    )
    deposited = service.deposit(
        **_transition_kwargs(called),
        destination_account_id=42,
        destination_bank_name="بانک سامان",
    )
    assert deposited.state is ChequeState.IN_CLEARING
    assert "deposit" in deposited.move_ids  # سند ۲, commercial now


def test_unexpected_call_in_reverses_only_the_memorandum_and_files_critical(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    repo, _, review = world
    service = _service(world)
    cheque = _issued(service, purpose="collateral")
    called = service.unexpected_call_in(
        **_transition_kwargs(cheque),
        statement_reference="صورتحساب ۱۴۰۵/۱۰/۲۱ — برداشت ۱۷٬۵۰۰٬۰۰۰ ریال",
    )
    assert called.state is ChequeState.CALLED_IN
    assert called.purpose is ChequePurpose.PAYMENT
    assert called.unexpected_call_in is True
    assert called.journal_pending_review is True
    assert "unexpected_call_in" in called.move_ids  # the memorandum reversal
    assert "call_in" not in called.move_ids  # the commercial journal is gated
    assert called.storage_location is StorageLocation.BANK
    entries = repo.list_custody_entries(cheque.id)
    assert entries and entries[-1].to_location is StorageLocation.BANK
    critical = [
        item
        for item in review.items
        if item["subtype"] == "issued_collateral_called_in_unexpected"
    ]
    assert len(critical) == 1
    assert critical[0]["severity"].value == "critical"


def test_unexpected_call_in_needs_the_statement_reference(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    service = _service(world)
    cheque = _issued(service, purpose="collateral")
    with pytest.raises(ChequeValidationError):
        service.unexpected_call_in(
            **_transition_kwargs(cheque), statement_reference="  "
        )


def test_unexpectedly_called_in_cheque_clears_after_the_accountant_posts(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    service = _service(world)
    cheque = _issued(service, purpose="collateral")
    called = service.unexpected_call_in(
        **_transition_kwargs(cheque), statement_reference="stmt-1"
    )
    cleared = service.clear(**_transition_kwargs(called))
    assert cleared.state is ChequeState.CLEARED
    assert "clear" in cleared.move_ids


def test_endorsed_bounce_posts_no_journal_and_files_critical(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    _, _, review = world
    service = _service(world)
    cheque = _held(service)
    endorsed = service.endorse(
        **_transition_kwargs(cheque),
        endorsee_name="تامین‌کننده الف",
        endorsee_type="supplier",
        sayad_transfer_reference="trf-1",
    )
    assert endorsed.state is ChequeState.ENDORSED
    moves_before = dict(endorsed.move_ids)
    bounced = service.record_endorsed_bounce(
        **_transition_kwargs(endorsed),
        bounce_reason="عدم کفایت موجودی",
        bounce_date="1405-10-20",
        certificate_reference="cert-9",
    )
    assert bounced.state is ChequeState.BOUNCED
    assert bounced.move_ids == moves_before  # §9.1 #3: no journal guessed
    assert bounced.journal_pending_review is True
    assert bounced.bounce_certificate_reference == "cert-9"
    critical = [
        item
        for item in review.items
        if item["subtype"] == "endorsed_cheque_bounced"
    ]
    assert len(critical) == 1
    assert critical[0]["severity"].value == "critical"


def test_endorsed_bounce_needs_the_bank_certificate(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    service = _service(world)
    cheque = _held(service)
    endorsed = service.endorse(
        **_transition_kwargs(cheque),
        endorsee_name="تامین‌کننده الف",
        endorsee_type="supplier",
        sayad_transfer_reference="trf-1",
    )
    with pytest.raises(ChequeValidationError):
        service.record_endorsed_bounce(
            **_transition_kwargs(endorsed),
            bounce_reason="عدم کفایت موجودی",
            bounce_date="1405-10-20",
            certificate_reference="  ",
        )


def test_endorsement_summary_splits_supplier_from_other(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    from hesabyar.application.queries.cheques import ChequeQueryService

    repo, _, _ = world
    service = _service(world)
    queries = ChequeQueryService(repo)
    first = _held(service, sayad_id="1111111111111111")
    service.endorse(
        **_transition_kwargs(first),
        endorsee_name="تامین‌کننده الف",
        endorsee_type="supplier",
        sayad_transfer_reference="trf-1",
    )
    second = _held(service, sayad_id="2222222222222222")
    service.endorse(
        **_transition_kwargs(second),
        endorsee_name="شخص ثالث",
        endorsee_type="other",
        sayad_transfer_reference="trf-2",
    )
    summary = queries.endorsement_summary(
        tenant_id=TENANT_ID, installation_id=INSTALLATION_ID
    )
    assert [c.sayad_id for c in summary.supplier] == ["1111111111111111"]
    assert [c.sayad_id for c in summary.other] == ["2222222222222222"]
    assert summary.supplier_total_irr == 17_500_000
    assert summary.other_total_irr == 17_500_000


def test_collateral_summary_and_commercial_list_segregate(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    from hesabyar.application.queries.cheques import ChequeQueryService

    repo, _, _ = world
    service = _service(world)
    queries = ChequeQueryService(repo)
    _held(service, sayad_id="1111111111111111")  # commercial
    _collateral_held(service, sayad_id="2222222222222222")  # collateral
    summary = queries.collateral_summary(
        tenant_id=TENANT_ID, installation_id=INSTALLATION_ID
    )
    assert [c.sayad_id for c in summary.cheques] == ["2222222222222222"]
    assert summary.received_total_irr == 17_500_000
    assert summary.issued_total_irr == 0
    commercial = queries.list_commercial_cheques(
        tenant_id=TENANT_ID, installation_id=INSTALLATION_ID
    )
    assert [c.sayad_id for c in commercial] == ["1111111111111111"]
    by_kind = queries.list_cheques(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        kind=ChequeKind.RECEIVED,
        purpose=ChequePurpose.COLLATERAL,
    )
    assert [c.sayad_id for c in by_kind] == ["2222222222222222"]


def test_issued_collateral_return_goes_back_to_the_vault(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    repo, _, _ = world
    service = _service(world)
    cheque = _issued(service, purpose="collateral")
    returned = service.return_cheque(**_transition_kwargs(cheque))
    assert returned.state is ChequeState.RETURNED
    assert returned.storage_location is StorageLocation.VAULT
    entries = repo.list_custody_entries(cheque.id)
    assert entries and entries[-1].to_location is StorageLocation.VAULT
    # The leaf was with the counterparty — the registry log records
    # an empty origin (the engine log cannot).
    assert entries[-1].from_location is None


def test_custody_move_is_allowed_on_a_called_in_cheque(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    service = _service(world)
    cheque = _collateral_held(service)
    called = service.call_in(
        **_transition_kwargs(cheque), accountant_approval="حسابدار ارشد"
    )
    moved = service.move_custody(
        **_transition_kwargs(called),
        to_location=StorageLocation.VAULT,
        reason="به صندوق امانات منتقل شد",
    )
    assert moved.storage_location is StorageLocation.VAULT
    # But not on an issued cheque (its leaf is with the beneficiary).
    issued = _issued(service, sayad_id="3333333333333333")
    with pytest.raises(ChequeValidationError):
        service.move_custody(
            **_transition_kwargs(issued),
            to_location=StorageLocation.VAULT,
            reason="بازگشت",
        )


def test_daily_job_skips_an_issued_cheque_in_registered(
    world: tuple[FakeChequeRepository, FakeEngine, RecordingReview],
) -> None:
    repo, _, _ = world
    service = _service(world)
    cheque = _register_issued(service, sayad_id="3333333333333333")
    # Same TODAY anchor as _register_aged (see there): the issued
    # cheque is skipped by kind whatever its age, but a wall-clock
    # anchor would still drift the fixture's meaning over time.
    cheque.created_at = datetime.datetime(
        TODAY.year, TODAY.month, TODAY.day, tzinfo=datetime.UTC
    ) - datetime.timedelta(days=7)
    repo.cheques[cheque.id] = cheque
    report = service.run_daily_cheque_job(TODAY)
    assert report.expired == 0
    assert report.reminders_sent == 0
    assert repo.cheques[cheque.id].state is ChequeState.REGISTERED
