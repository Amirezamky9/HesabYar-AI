"""Unit tests for the payment.settlement.received consumer (US-554).

DB-free: the handler runs over the REAL settlement import service
(its fakes come from ``tests.unit.test_settlement_import_services``)
and the pipeline tests run over the event fakes in
``tests/event_fixtures.py``. The consumer must use the SAME
tie-out + auto-post path as file imports — the tests prove it by
driving one batch through the file path and the same batch through
the event path.
"""

from __future__ import annotations

import datetime
from typing import Any

import pytest

from hesabyar.application.commands.events import (
    EventPipelineService,
    SettlementReceivedOutcome,
)
from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.application.commands.settlement import SettlementCommandService
from hesabyar.application.commands.settlement_import import SettlementImportService
from hesabyar.application.commands.settlement_received import (
    SettlementReceivedHandler,
    validate_settlement_received_payload,
)
from hesabyar.application.queries.review_queue import ReviewQueueQueryService
from hesabyar.domain.events.envelope import CanonicalEvent, CanonicalEventType
from hesabyar.domain.events.errors import (
    InvalidEventEnvelopeError,
    MappingUnresolvedError,
)
from hesabyar.domain.events.models import (
    AutomationDecisionKind,
    InboxEventStatus,
    NormalizedEvent,
    PipelineOutcome,
    ResolvedMapping,
    StoreSettings,
)
from hesabyar.domain.review_queue.models import ReviewItemType
from hesabyar.domain.settlement.commands import (
    ApproveSettlementImport,
    CreateGatewayPayment,
    ImportSettlementFile,
    RecordBankDeposit,
)
from hesabyar.domain.settlement.models import PaymentSource
from hesabyar.infrastructure.events.defaults import (
    InboxReviewQueueAdapter,
)
from tests.event_fixtures import (
    INSTALLATION_ID,
    TENANT_ID,
    FakeAuditLog,
    FakeDraftBuilder,
    FakeEngine,
    FakeExternalIdMap,
    FakeInboxRepository,
    FakeMappingResolver,
    FakeOutboxRepository,
    FakePolicyGate,
    FakeStoreSettings,
    must_get,
    raw_envelope,
    resolving_mapping,
)
from tests.unit.test_bank_services import FakeReviewQueue as BankFakeReviewQueue
from tests.unit.test_settlement_import_services import (
    COLUMNS,
    HEADERS,
    TRANCHE_NET,
    FakeFileReader,
    FakeOutbox,
    FakePosting,
    _detail_row,
    _make_registry,
)
from tests.unit.test_settlement_import_services import (
    FakePolicyGate as SettlementFakePolicyGate,
)
from tests.unit.test_settlement_services import (
    FakeSettlementAudit,
    FakeSettlementRepository,
)

NOW = datetime.datetime(2026, 11, 6, 12, 0, tzinfo=datetime.UTC)


@pytest.fixture()
def world() -> dict[str, Any]:
    """A fresh fake world: settlement service + the consumer handler."""
    settlement_repo = FakeSettlementRepository()
    # The registry is installation-scoped: seed snapppay under THIS
    # module's installation id ("installation-event-tests"), not the
    # import-services module's default.
    registry = _make_registry(INSTALLATION_ID)
    audit = FakeSettlementAudit()
    commands = SettlementCommandService(settlement_repo, registry, audit)
    queue = BankFakeReviewQueue()
    review = ReviewQueueCommandService(queue, queue, now=NOW)
    review_queries = ReviewQueueQueryService(queue, queue, now=NOW)
    reader = FakeFileReader()
    outbox = FakeOutbox()
    posting = FakePosting()
    gate = SettlementFakePolicyGate()
    settlement_import = SettlementImportService(
        settlement_repo,
        registry,
        audit,
        commands,
        review,
        review_queries,
        reader,  # type: ignore[arg-type]
        outbox,  # type: ignore[arg-type]
        posting,  # type: ignore[arg-type]
        gate,  # type: ignore[arg-type]
        now=NOW,
    )
    handler = SettlementReceivedHandler(
        settlement_import, registry, settlement_repo, review
    )
    return {
        "settlement_repo": settlement_repo,
        "registry": registry,
        "commands": commands,
        "queue": queue,
        "review": review,
        "reader": reader,
        "posting": posting,
        "gate": gate,
        "settlement_import": settlement_import,
        "handler": handler,
    }


def _record_payment(commands: SettlementCommandService) -> Any:
    """Record a 100M-gross SnappPay payment (4 tranches of 25M)."""
    return commands.record_payment(
        CreateGatewayPayment(
            installation_id=INSTALLATION_ID,
            channel_key="snapppay",
            gateway_transaction_id="TXN-1",
            gross_irr=100_000_000,
            gateway_confirmed_at=NOW,
            source=PaymentSource.STORE_WEBHOOK,
            store_order_id="ORD-1",
        )
    ).payment


def _import_and_approve(world: dict[str, Any]) -> Any:
    """Import one detail row + approve: a batch ready for tie-out."""
    _record_payment(world["commands"])
    world["reader"].grid = [HEADERS, _detail_row()]
    result = world["settlement_import"].import_file(
        ImportSettlementFile(
            installation_id=INSTALLATION_ID,
            channel_key="snapppay",
            file_type="detail",
            period_jalali="1405/08",
            filename="snapppay-1405-08.xlsx",
            content=b"fake-xlsx-bytes",
            currency_unit="rial",
            columns=COLUMNS,
            date_kind="jalali",
            actor="tester",
        ),
        tenant_id=TENANT_ID,
    )
    approved = world["settlement_import"].approve_import(
        INSTALLATION_ID,
        ApproveSettlementImport(import_id=result.file_import.id, actor="tester"),
        tenant_id=TENANT_ID,
    )
    return approved


def _import_only(world: dict[str, Any]) -> Any:
    """Import one detail row, left awaiting review."""
    _record_payment(world["commands"])
    world["reader"].grid = [HEADERS, _detail_row()]
    return world["settlement_import"].import_file(
        ImportSettlementFile(
            installation_id=INSTALLATION_ID,
            channel_key="snapppay",
            file_type="detail",
            period_jalali="1405/08",
            filename="snapppay-1405-08.xlsx",
            content=b"fake-xlsx-bytes",
            currency_unit="rial",
            columns=COLUMNS,
            date_kind="jalali",
            actor="tester",
        ),
        tenant_id=TENANT_ID,
    )


def _deposit(world: dict[str, Any], *, amount_irr: int) -> Any:
    return world["settlement_import"].record_deposit(
        RecordBankDeposit(
            installation_id=INSTALLATION_ID,
            tenant_id=TENANT_ID,
            deposit_date=datetime.date(2026, 11, 7),
            amount_irr=amount_irr,
            description=None,
            tracking_ref=None,
            source="manual",
            channel_key="snapppay",
            actor="tester",
        ),
        company=None,
    )


def _normalized(payload: dict[str, Any]) -> NormalizedEvent:
    """A settlement-received event, already normalized (Rial payload)."""
    canonical = CanonicalEvent.from_raw(raw_envelope(payload=payload))
    return NormalizedEvent(
        event=canonical,
        event_type=CanonicalEventType.PAYMENT_SETTLEMENT_RECEIVED,
        store_settings=StoreSettings(
            source_system="hayat", source_store_id="store-1", prices_in_toman=False
        ),
        converted_payload=dict(payload),
    )


def _receipt_payload(
    *, net_irr: int = TRANCHE_NET, **overrides: Any
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "channel_key": "snapppay",
        "payout_date": "2026-11-07",
        "net_irr": net_irr,
    }
    payload.update(overrides)
    return payload


def _handle(
    world: dict[str, Any],
    payload: dict[str, Any],
    mapping: ResolvedMapping | None = None,
) -> SettlementReceivedOutcome:
    return world["handler"].handle_settlement_received(
        event=_normalized(payload),
        mapping=mapping if mapping is not None else resolving_mapping(),
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
    )


# -- payload validation -----------------------------------------------------------


class TestPayloadValidation:
    """The receipt contract: required fields, whole-Rial amounts."""

    def test_valid_payload(self) -> None:
        channel, payout, net, event_id, batch_id = (
            validate_settlement_received_payload(
                _receipt_payload(
                    settlement_event_id="evt-9", import_batch_id="batch-9"
                )
            )
        )
        assert channel == "snapppay"
        assert payout == datetime.date(2026, 11, 7)
        assert net == TRANCHE_NET
        assert event_id == "evt-9"
        assert batch_id == "batch-9"

    @pytest.mark.parametrize("field", ["settlement_amount", "net_amount"])
    def test_alternate_amount_fields_accepted(self, field: str) -> None:
        payload = _receipt_payload()
        del payload["net_irr"]
        payload[field] = TRANCHE_NET
        _, _, net, _, _ = validate_settlement_received_payload(payload)
        assert net == TRANCHE_NET

    def test_missing_channel_key_rejected(self) -> None:
        payload = _receipt_payload()
        del payload["channel_key"]
        with pytest.raises(InvalidEventEnvelopeError, match="channel_key"):
            validate_settlement_received_payload(payload)

    def test_bad_payout_date_rejected(self) -> None:
        with pytest.raises(InvalidEventEnvelopeError, match="payout_date"):
            validate_settlement_received_payload(
                _receipt_payload(payout_date="7 Nov 2026")
            )

    def test_missing_amount_rejected(self) -> None:
        payload = _receipt_payload()
        del payload["net_irr"]
        with pytest.raises(InvalidEventEnvelopeError, match="net amount"):
            validate_settlement_received_payload(payload)

    def test_negative_amount_rejected(self) -> None:
        with pytest.raises(InvalidEventEnvelopeError, match="negative"):
            validate_settlement_received_payload(_receipt_payload(net_irr=-1))

    @pytest.mark.parametrize("bad", [175.5, "100", True])
    def test_non_integer_amount_rejected(self, bad: Any) -> None:
        with pytest.raises(InvalidEventEnvelopeError, match="whole-Rial"):
            validate_settlement_received_payload(_receipt_payload(net_irr=bad))

    def test_bad_tranche_ids_rejected(self) -> None:
        with pytest.raises(InvalidEventEnvelopeError, match="tranche_ids"):
            validate_settlement_received_payload(
                _receipt_payload(tranche_ids="not-a-list")
            )


# -- handler: the shared tie-out path ------------------------------------------------


class TestHandler:
    """Receipts flow through the same tie-out + auto-post as file imports."""

    def test_unknown_channel_is_permanent(self, world: dict[str, Any]) -> None:
        with pytest.raises(MappingUnresolvedError, match="not in this installation"):
            _handle(world, _receipt_payload(channel_key="unknown-gateway"))

    def test_unattributed_receipt_files_review_item(
        self, world: dict[str, Any]
    ) -> None:
        outcome = _handle(world, _receipt_payload())
        assert outcome.posted is False
        assert outcome.needs_review is True
        assert outcome.settlement_event_id is None
        assert "unattributed" in outcome.detail
        items = [
            item
            for item in world["queue"].items.values()
            if item.item_type == ReviewItemType.SETTLEMENT_VARIANCE
        ]
        assert len(items) == 1
        assert items[0].subtype == "unknown_reference"
        assert items[0].amount_irr == TRANCHE_NET

    def test_unknown_settlement_event_id_is_unattributed(
        self, world: dict[str, Any]
    ) -> None:
        outcome = _handle(
            world, _receipt_payload(settlement_event_id="no-such-event")
        )
        assert outcome.needs_review is True
        assert outcome.settlement_event_id == "no-such-event"

    def test_receipt_posts_when_tieout_holds(self, world: dict[str, Any]) -> None:
        approved = _import_and_approve(world)
        _deposit(world, amount_irr=TRANCHE_NET)
        outcome = _handle(
            world,
            _receipt_payload(
                settlement_event_id=approved.settlement_event.id,
                import_batch_id=approved.file_import.import_batch_id,
            ),
        )
        assert outcome.posted is True
        assert outcome.tieout_holds is True
        assert outcome.tryton_journal_id == 777
        assert outcome.needs_review is False
        assert len(world["posting"].calls) == 1
        assert world["posting"].calls[0].amount_irr == TRANCHE_NET
        file_import = world["settlement_repo"].get_file_import(
            INSTALLATION_ID, approved.file_import.id
        )
        assert file_import is not None
        assert file_import.status == "settled"

    def test_receipt_by_batch_id_only_posts(self, world: dict[str, Any]) -> None:
        approved = _import_and_approve(world)
        _deposit(world, amount_irr=TRANCHE_NET)
        outcome = _handle(
            world,
            _receipt_payload(import_batch_id=approved.file_import.import_batch_id),
        )
        assert outcome.posted is True
        assert outcome.settlement_event_id == approved.settlement_event.id

    def test_receipt_without_deposit_needs_review(self, world: dict[str, Any]) -> None:
        approved = _import_and_approve(world)
        outcome = _handle(
            world,
            _receipt_payload(
                settlement_event_id=approved.settlement_event.id,
            ),
        )
        assert outcome.posted is False
        assert outcome.tieout_holds is False
        assert outcome.needs_review is True  # tie-out exception
        assert len(world["posting"].calls) == 0

    def test_receipt_held_when_gate_denies(self, world: dict[str, Any]) -> None:
        approved = _import_and_approve(world)
        _deposit(world, amount_irr=TRANCHE_NET)
        world["gate"].allowed = False
        world["gate"].reason = "fiscal period closed"
        outcome = _handle(
            world,
            _receipt_payload(
                settlement_event_id=approved.settlement_event.id,
            ),
        )
        assert outcome.posted is False
        assert outcome.tieout_holds is True
        assert outcome.needs_review is False  # held, not an exception
        assert len(world["posting"].calls) == 0

    def test_receipt_without_company_fails_closed(self, world: dict[str, Any]) -> None:
        approved = _import_and_approve(world)
        _deposit(world, amount_irr=TRANCHE_NET)
        mapping = resolving_mapping()
        mapping_no_company = ResolvedMapping(
            mapping_version=mapping.mapping_version,
            company=None,
            accounts=mapping.accounts,
            journal=mapping.journal,
            party=mapping.party,
            unresolved=mapping.unresolved,
        )
        outcome = _handle(
            world,
            _receipt_payload(
                settlement_event_id=approved.settlement_event.id,
            ),
            mapping=mapping_no_company,
        )
        assert outcome.posted is False
        assert outcome.tieout_holds is True
        assert outcome.needs_review is False
        assert len(world["posting"].calls) == 0

    def test_receipt_for_settled_batch_is_idempotent(
        self, world: dict[str, Any]
    ) -> None:
        approved = _import_and_approve(world)
        _deposit(world, amount_irr=TRANCHE_NET)
        payload = _receipt_payload(
            settlement_event_id=approved.settlement_event.id,
        )
        first = _handle(world, payload)
        assert first.posted is True
        second = _handle(world, payload)
        assert second.posted is False
        assert second.needs_review is False
        assert second.tieout_holds is True
        assert "already settled" in second.detail
        assert len(world["posting"].calls) == 1  # no re-post

    def test_receipt_for_batch_without_event_is_unattributed(
        self, world: dict[str, Any]
    ) -> None:
        # No settlement event exists yet (approve creates it), so a
        # receipt naming the batch finds nothing to tie out.
        result = _import_only(world)
        outcome = _handle(
            world,
            _receipt_payload(import_batch_id=result.file_import.import_batch_id),
        )
        assert outcome.needs_review is True
        assert outcome.settlement_event_id is None

    def test_receipt_for_awaiting_review_batch_waits(
        self, world: dict[str, Any]
    ) -> None:
        # The settlement event exists but its import went back to
        # awaiting review (re-opened by a re-run): the receipt is
        # recorded and the approve step will tie it out — no review
        # item, no retry.
        approved = _import_and_approve(world)
        world["settlement_repo"].update_file_import(
            approved.file_import.id, status="awaiting_review"
        )
        outcome = _handle(
            world,
            _receipt_payload(
                settlement_event_id=approved.settlement_event.id,
            ),
        )
        assert outcome.posted is False
        assert outcome.needs_review is False
        assert outcome.tieout_holds is None
        assert "awaiting review" in outcome.detail
        items = [
            item
            for item in world["queue"].items.values()
            if item.item_type == ReviewItemType.SETTLEMENT_VARIANCE
        ]
        assert items == []

    def test_receipt_for_rejected_batch_files_review_item(
        self, world: dict[str, Any]
    ) -> None:
        approved = _import_and_approve(world)
        # Reject the import after approval is impossible through the
        # service (approve is one-way); simulate the rejected state
        # the consumer must handle: the batch was rejected before
        # the receipt arrived, but its settlement event exists.
        world["settlement_repo"].update_file_import(
            approved.file_import.id, status="rejected"
        )
        outcome = _handle(
            world,
            _receipt_payload(
                settlement_event_id=approved.settlement_event.id,
            ),
        )
        assert outcome.posted is False
        assert outcome.needs_review is True
        assert "rejected" in outcome.detail
        items = [
            item
            for item in world["queue"].items.values()
            if item.item_type == ReviewItemType.SETTLEMENT_VARIANCE
        ]
        assert len(items) == 1
        assert items[0].subtype == "unknown_reference"


# -- pipeline branch ---------------------------------------------------------------------


def _pipeline_with_handler(
    world: dict[str, Any],
    *,
    inbox: FakeInboxRepository | None = None,
) -> tuple[
    EventPipelineService,
    FakeInboxRepository,
    FakeOutboxRepository,
    FakeExternalIdMap,
    FakeAuditLog,
]:
    """An event pipeline with the settlement consumer wired."""
    inbox = inbox or FakeInboxRepository()
    outbox = FakeOutboxRepository()
    external_ids = FakeExternalIdMap()
    audit_log = FakeAuditLog()
    settings = FakeStoreSettings()
    service = EventPipelineService(
        inbox=inbox,
        outbox=outbox,
        external_ids=external_ids,
        audit_log=audit_log,
        store_settings=settings,
        mapping_resolver=FakeMappingResolver(),
        draft_builder=FakeDraftBuilder(),
        policy_gate=FakePolicyGate(allowed=False),
        review_queue=InboxReviewQueueAdapter(),
        engine=FakeEngine(),
        settlement_received=world["handler"],
    )
    return service, inbox, outbox, external_ids, audit_log


def _ingest_receipt(
    inbox: FakeInboxRepository,
    payload: dict[str, Any],
    *,
    source_event_id: str,
    external_entity_id: str | None = None,
) -> str:
    """Store a settlement-received inbox event (with installation)."""
    event = CanonicalEvent.from_raw(
        raw_envelope(
            payload=payload,
            source_event_id=source_event_id,
            external_entity_type="settlement_receipt",
            external_entity_id=external_entity_id or f"receipt-{source_event_id}",
        )
    )
    outcome = inbox.store(
        event,
        CanonicalEventType.PAYMENT_SETTLEMENT_RECEIVED,
        TENANT_ID,
        installation_id=INSTALLATION_ID,
    )
    assert outcome.status == "created"
    assert outcome.event is not None
    return outcome.event.id


class TestPipelineBranch:
    """The pipeline routes settlement receipts to the consumer."""

    def test_pipeline_posts_receipt_and_records_mapping(
        self, world: dict[str, Any]
    ) -> None:
        approved = _import_and_approve(world)
        _deposit(world, amount_irr=TRANCHE_NET)
        service, inbox, outbox, external_ids, audit_log = _pipeline_with_handler(world)
        event_id = _ingest_receipt(
            inbox,
            _receipt_payload(
                settlement_event_id=approved.settlement_event.id,
            ),
            source_event_id="evt-recv-1",
        )
        result = service.process(event_id)
        assert result.outcome == PipelineOutcome.PROCESSED_POSTED
        assert result.draft_ref == approved.settlement_event.id
        assert result.posted_ref == "777"
        assert must_get(inbox, event_id).status == InboxEventStatus.PROCESSED
        # §10.4 mapping: gateway receipt → HesabYar settlement event.
        mapping = external_ids.get("hayat", "settlement_receipt", "receipt-evt-recv-1")
        assert mapping is not None
        assert mapping.target_external_id == approved.settlement_event.id
        assert mapping.target_entity_type == "settlement.event"
        # Decision audit: auto_post, no draft builder involved.
        assert len(audit_log.decisions) == 1
        assert audit_log.decisions[0].decision == AutomationDecisionKind.AUTO_POST
        assert len(outbox.list_events(TENANT_ID)) == 1

    def test_pipeline_unwired_handler_routes_to_review(self) -> None:
        # No consumer wired: the receipt fails closed to review.
        inbox2 = FakeInboxRepository()
        unwired = EventPipelineService(
            inbox=inbox2,
            outbox=FakeOutboxRepository(),
            external_ids=FakeExternalIdMap(),
            audit_log=FakeAuditLog(),
            store_settings=FakeStoreSettings(),
            mapping_resolver=FakeMappingResolver(),
            draft_builder=FakeDraftBuilder(),
            policy_gate=FakePolicyGate(allowed=False),
            review_queue=InboxReviewQueueAdapter(),
            engine=FakeEngine(),
        )
        event_id = _ingest_receipt(
            inbox2, _receipt_payload(), source_event_id="evt-recv-2"
        )
        result = unwired.process(event_id)
        assert result.outcome == PipelineOutcome.NEEDS_REVIEW
        assert must_get(inbox2, event_id).status == InboxEventStatus.NEEDS_REVIEW

    def test_pipeline_unattributed_receipt_is_processed_not_retried(
        self, world: dict[str, Any]
    ) -> None:
        service, inbox, _outbox, _external_ids, _audit = _pipeline_with_handler(world)
        event_id = _ingest_receipt(
            inbox, _receipt_payload(), source_event_id="evt-recv-3"
        )
        result = service.process(event_id)
        # Processed (not retried, not dead-lettered): the review item
        # is already filed; retrying cannot create the event.
        assert result.outcome == PipelineOutcome.PROCESSED_DRAFT
        assert must_get(inbox, event_id).status == InboxEventStatus.PROCESSED
        items = [
            item
            for item in world["queue"].items.values()
            if item.item_type == ReviewItemType.SETTLEMENT_VARIANCE
        ]
        assert len(items) == 1

    def test_pipeline_redelivery_suppresses_via_mapping(
        self, world: dict[str, Any]
    ) -> None:
        approved = _import_and_approve(world)
        _deposit(world, amount_irr=TRANCHE_NET)
        service, inbox, _outbox, external_ids, _audit = _pipeline_with_handler(world)
        first_id = _ingest_receipt(
            inbox,
            _receipt_payload(
                settlement_event_id=approved.settlement_event.id,
            ),
            source_event_id="evt-recv-4",
        )
        first = service.process(first_id)
        assert first.outcome == PipelineOutcome.PROCESSED_POSTED
        assert len(world["posting"].calls) == 1
        # A second inbox event for the SAME gateway receipt (same
        # external entity, new source event id) suppresses in the
        # §10.4 pre-check — no second tie-out, no second posting.
        second_id = _ingest_receipt(
            inbox,
            _receipt_payload(
                settlement_event_id=approved.settlement_event.id,
            ),
            source_event_id="evt-recv-4b",
            external_entity_id="receipt-evt-recv-4",
        )
        second = service.process(second_id)
        assert second.outcome == PipelineOutcome.PROCESSED_DRAFT
        assert second.draft_ref == approved.settlement_event.id
        assert len(world["posting"].calls) == 1

    def test_pipeline_invalid_payload_routes_to_review(
        self, world: dict[str, Any]
    ) -> None:
        service, inbox, _outbox, _external_ids, _audit = _pipeline_with_handler(world)
        payload = _receipt_payload()
        del payload["channel_key"]
        event_id = _ingest_receipt(
            inbox, payload, source_event_id="evt-recv-5"
        )
        result = service.process(event_id)
        assert result.outcome == PipelineOutcome.NEEDS_REVIEW
        assert must_get(inbox, event_id).status == InboxEventStatus.NEEDS_REVIEW
