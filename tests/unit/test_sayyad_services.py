"""Service tests for Sayyad inquiries (US-534, Layers 1–2).

DB-free: the inquiry service runs against the in-memory cheque
repository fake from the US-531 service tests and the deterministic
simulators. They pin: verdict-only receipts (never raw PII), colour
/ back-cheque fields updated from verdicts, Ehrazino costs, the
unknown-scope fail-closed rule, and the mandatory receipt-rule
audit (idempotent per open item).
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass, field
from typing import Any, cast

import pytest

from hesabyar.application.commands.sayyad import SayyadInquiryService
from hesabyar.domain.cheques.errors import ChequeNotFoundError
from hesabyar.domain.cheques.models import ChequeState, InquiryType
from hesabyar.domain.cheques.ports import registration_to_cheque
from hesabyar.domain.cheques.sayyad import SayyadInquiryError
from hesabyar.domain.cheques.validation import validate_registration_payload
from hesabyar.domain.review_queue.models import (
    ReviewItem,
    ReviewItemStatus,
    ReviewItemType,
    ReviewSeverity,
)
from hesabyar.domain.review_queue.ports import ReviewItemFilters, ReviewQueuePort
from hesabyar.infrastructure.sayyad.simulator import (
    DeterministicEhrazinoSimulator,
    DeterministicFintechSayyadSimulator,
    EhrazinoColorScenario,
    SayyadScenario,
)
from tests.unit.test_cheque_services import (
    ACTOR,
    COMPANY_ENGINE_ID,
    INSTALLATION_ID,
    TENANT_ID,
    FakeChequeRepository,
    RecordingReview,
    _payload,
)


@dataclass
class FakeQueue:
    """Review-queue read port fake (audit idempotency only)."""

    items: list[ReviewItem] = field(default_factory=list)

    def list_items(self, tenant_id: str, filters: ReviewItemFilters) -> list[ReviewItem]:
        return [
            item
            for item in self.items
            if item.tenant_id == tenant_id
            and (filters.item_type is None or item.item_type == filters.item_type)
        ]

    def get_item(self, item_id: str, tenant_id: str) -> ReviewItem | None:
        return next(
            (item for item in self.items if item.id == item_id and item.tenant_id == tenant_id),
            None,
        )


def _registered_cheque(repo: FakeChequeRepository, **overrides: object):
    registration = validate_registration_payload(_payload(**overrides))
    cheque = registration_to_cheque(
        registration,
        cheque_id=str(uuid.uuid4()),
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        company_engine_id=COMPANY_ENGINE_ID,
        source="manual",
    )
    return repo.create(cheque)


def _service(
    repo: FakeChequeRepository,
    *,
    review: RecordingReview | None = None,
    queue: FakeQueue | None = None,
) -> SayyadInquiryService:
    finnotech = DeterministicFintechSayyadSimulator()
    finnotech.seed(
        "1234567890123456",
        SayyadScenario(color_code=3, back_cheques_count=2, status="in_transit"),
    )
    ehrazino = DeterministicEhrazinoSimulator()
    ehrazino.seed_color("0012345678", EhrazinoColorScenario(color_code=2))
    return SayyadInquiryService(
        cast(Any, repo),
        finnotech=finnotech,
        ehrazino=ehrazino,
        review=cast(Any, review),
        queue=cast(ReviewQueuePort, queue) if queue is not None else None,
    )


def test_finnotech_color_inquiry_stores_verdict_only_and_updates_colour() -> None:
    repo = FakeChequeRepository()
    cheque = _registered_cheque(repo)
    service = _service(repo)
    receipt = service.inquire_finnotech(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        cheque_id=cheque.id,
        actor=ACTOR,
        scope="credit:cheque-color-inquiry:get",
    )
    assert receipt.channel.value == "finnotech_api"
    assert receipt.inquiry_type == InquiryType.COLOR
    assert receipt.result_code == "color=3"
    assert "0012345678" not in receipt.result_code + receipt.result_summary + receipt.reference
    stored = repo.get(cheque.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID)
    assert stored is not None
    assert stored.issuer_color_code == 3
    assert stored.issuer_color_inquired_at is not None
    assert len(repo.list_inquiry_receipts(cheque.id)) == 1


def test_finnotech_back_cheques_inquiry_updates_count() -> None:
    repo = FakeChequeRepository()
    cheque = _registered_cheque(repo)
    service = _service(repo)
    receipt = service.inquire_finnotech(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        cheque_id=cheque.id,
        actor=ACTOR,
        scope="credit:back-cheques:get",
    )
    assert receipt.result_code == "back_cheques=2"
    stored = repo.get(cheque.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID)
    assert stored is not None
    assert stored.issuer_back_cheques == 2


def test_finnotech_status_in_transit_does_not_touch_accounting_state() -> None:
    """Sayyad in-transit ≠ accounting in_clearing (§1.3): the verdict stays on the receipt."""
    repo = FakeChequeRepository()
    cheque = _registered_cheque(repo)
    service = _service(repo)
    receipt = service.inquire_finnotech(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        cheque_id=cheque.id,
        actor=ACTOR,
        scope="credit:cheque-status-inquiry:post",
    )
    assert receipt.result_code == "status=in_transit"
    stored = repo.get(cheque.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID)
    assert stored is not None
    assert stored.state == ChequeState.REGISTERED


def test_finnotech_unknown_scope_fails_closed() -> None:
    repo = FakeChequeRepository()
    cheque = _registered_cheque(repo)
    service = _service(repo)
    with pytest.raises(SayyadInquiryError):
        service.inquire_finnotech(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            cheque_id=cheque.id,
            actor=ACTOR,
            scope="credit:not-a-scope:get",
        )
    assert repo.list_inquiry_receipts(cheque.id) == []


def test_finnotech_unknown_cheque_is_404() -> None:
    repo = FakeChequeRepository()
    service = _service(repo)
    with pytest.raises(ChequeNotFoundError):
        service.inquire_finnotech(
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            cheque_id="missing",
            actor=ACTOR,
            scope="credit:cheque-color-inquiry:get",
        )


def test_ehrazino_color_inquiry_costs_1700_toman_and_updates_colour() -> None:
    repo = FakeChequeRepository()
    cheque = _registered_cheque(repo)
    service = _service(repo)
    receipt = service.inquire_ehrazino_color(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        cheque_id=cheque.id,
        actor=ACTOR,
    )
    assert receipt.channel.value == "ehrazino_api"
    assert receipt.result_code == "color=2"
    assert receipt.cost_irr == 17_000
    stored = repo.get(cheque.id, tenant_id=TENANT_ID, installation_id=INSTALLATION_ID)
    assert stored is not None
    assert stored.issuer_color_code == 2
    assert stored.issuer_color_inquired_at is not None


def test_ehrazino_sayad_check_costs_6900_toman() -> None:
    repo = FakeChequeRepository()
    cheque = _registered_cheque(repo)
    service = _service(repo)
    receipt = service.inquire_ehrazino_sayad_check(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        cheque_id=cheque.id,
        actor=ACTOR,
        scenario="clean",
    )
    assert receipt.inquiry_type == InquiryType.ITEMS_MATCH
    assert receipt.result_code == "items_match=true"
    assert receipt.cost_irr == 69_000


def test_receipt_rule_audit_files_one_item_per_held_cheque_without_receipt() -> None:
    repo = FakeChequeRepository()
    held = _registered_cheque(repo)
    held.state = ChequeState.HELD
    held.receipt_missing_warning = True
    repo.save(held)
    with_receipt = _registered_cheque(repo, sayad_id="9999888877776666")
    with_receipt.state = ChequeState.HELD
    repo.save(with_receipt)
    review = RecordingReview()
    service = _service(repo, review=review)
    # The second cheque has an items-match receipt, so only the first is missing.
    service.inquire_finnotech(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        cheque_id=with_receipt.id,
        actor=ACTOR,
        scope="credit:sayad-serial-inquiry:get",
    )
    audit = service.audit_receipt_rule(
        tenant_id=TENANT_ID, installation_id=INSTALLATION_ID, actor=ACTOR
    )
    assert audit.checked == 2
    assert audit.missing == 1
    assert audit.items_filed == 1
    assert audit.cheque_ids == (held.id,)
    assert review.items[0]["subtype"] == "missing_inquiry_receipt"
    assert review.items[0]["source_event_ref"] == f"cheque:{held.id}"


def test_receipt_rule_audit_is_idempotent_while_the_item_is_open() -> None:
    repo = FakeChequeRepository()
    held = _registered_cheque(repo)
    held.state = ChequeState.HELD
    held.receipt_missing_warning = True
    repo.save(held)
    queue = FakeQueue(
        items=[
            ReviewItem(
                id="item-1",
                tenant_id=TENANT_ID,
                item_type=ReviewItemType.CHEQUE_EXCEPTION,
                severity=ReviewSeverity.WARNING,
                status=ReviewItemStatus.OPEN,
                reason="missing receipt",
                suggested_action="record it",
                created_at=datetime.datetime.now(datetime.UTC),
                subtype="missing_inquiry_receipt",
                source_event_ref=f"cheque:{held.id}",
            )
        ]
    )
    review = RecordingReview()
    service = _service(repo, review=review, queue=queue)
    audit = service.audit_receipt_rule(
        tenant_id=TENANT_ID, installation_id=INSTALLATION_ID, actor=ACTOR
    )
    assert audit.missing == 1
    assert audit.items_filed == 0
    assert review.items == []
