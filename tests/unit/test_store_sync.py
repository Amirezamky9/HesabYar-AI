"""Unit tests for store reconciliation & backfill (US-512, S2).

DB-free: the sync service runs against the in-memory event fakes
(``tests/event_fixtures.py``) and in-memory fake connectors. The
missed-webhook test drives the real ``EventIngestService`` for the
webhook half, so the same-key dedup is proven through the production
ingest path, not through a mock of it.
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Sequence

import pytest

from hesabyar.application.commands.events import EventIngestService
from hesabyar.application.commands.sync import StoreSyncService
from hesabyar.domain.events.envelope import CanonicalEvent, CanonicalEventType
from hesabyar.domain.events.errors import (
    IdempotencyConflictError,
    InvalidSyncCursorError,
    InvalidSyncRequestError,
    StoreSyncFailedError,
    StoreUnreachableError,
)
from hesabyar.domain.events.models import IncomingEvent
from hesabyar.domain.events.store_webhooks import validate_store_payload
from hesabyar.domain.events.sync import (
    DEFAULT_SYNC_PAGE_SIZE,
    MAX_SYNC_PAGE_SIZE,
    REFUND_PAYMENT_EVENT_SUFFIX,
    StoreChange,
    StoreEntityKind,
    SyncBatch,
    SyncCursor,
    decode_sync_cursor,
    encode_sync_cursor,
    map_store_change,
    validate_page_size,
)
from hesabyar.infrastructure.events.in_memory_queue import InMemoryJobQueue
from tests.event_fixtures import (
    INSTALLATION_ID,
    TENANT_ID,
    FakeIdempotencyStore,
    FakeInboxRepository,
    FakeWebhookSecrets,
    raw_envelope,
)

NOW = datetime.datetime(2026, 11, 18, 9, 0, tzinfo=datetime.UTC)


def make_change(
    *,
    event_id: str,
    event_name: str = "order.completed",
    entity_type: StoreEntityKind = StoreEntityKind.ORDER,
    entity_id: str | None = None,
    source_system: str = "hayat",
    source_store_id: str = "store-1",
    updated_at: datetime.datetime | None = None,
    status: str = "completed",
    payload: dict | None = None,
) -> StoreChange:
    """Build a change row (valid by default)."""
    return StoreChange(
        source_system=source_system,
        source_store_id=source_store_id,
        entity_type=entity_type,
        entity_id=entity_id or f"order-{event_id}",
        event_id=event_id,
        event_name=event_name,
        updated_at=updated_at or NOW,
        status=status,
        payload=payload if payload is not None else {"total_amount": 175000},
    )


class FakeConnector:
    """In-memory CommerceConnectorPort serving a fixed change list, paged."""

    def __init__(
        self,
        changes: Sequence[StoreChange],
        *,
        fail_on_page: int | None = None,
        has_more_without_cursor: bool = False,
    ) -> None:
        self._changes = list(changes)
        self._fail_on_page = fail_on_page
        self._has_more_without_cursor = has_more_without_cursor
        self.fetch_calls: list[dict] = []

    def verify_event(self, request):  # pragma: no cover - not used by sync
        raise NotImplementedError

    def normalize_event(self, event):  # pragma: no cover - not used by sync
        raise NotImplementedError

    def fetch_changes(self, cursor, *, updated_since=None, page_size=DEFAULT_SYNC_PAGE_SIZE):
        self.fetch_calls.append(
            {"cursor": cursor.value if cursor else None, "updated_since": updated_since, "page_size": page_size}
        )
        page_number = len(self.fetch_calls)
        if self._fail_on_page is not None and page_number == self._fail_on_page:
            raise StoreUnreachableError("boom")
        start = int(cursor.value) if cursor is not None and cursor.value else 0
        rows = self._changes[start : start + page_size]
        events = []
        unmapped = []
        for change in rows:
            mapped = map_store_change(change)
            if mapped:
                events.extend(mapped)
            else:
                unmapped.append(change)
        end = start + len(rows)
        has_more = end < len(self._changes)
        next_cursor = None
        if has_more:
            if not self._has_more_without_cursor:
                next_cursor = SyncCursor(value=str(end))
        return SyncBatch(
            events=events,
            unmapped_changes=unmapped,
            next_cursor=next_cursor,
            has_more=has_more,
        )

    def acknowledge(self, event, result):  # pragma: no cover - not used by sync
        return None


def make_ingest() -> tuple[EventIngestService, FakeInboxRepository, FakeIdempotencyStore]:
    """Build the real ingest service over in-memory fakes."""
    inbox = FakeInboxRepository()
    idempotency = FakeIdempotencyStore()
    service = EventIngestService(inbox, idempotency, FakeWebhookSecrets(), InMemoryJobQueue())
    return service, inbox, idempotency


def ingest_webhook(
    service: EventIngestService,
    *,
    source_event_id: str,
    payload: dict | None = None,
    source_system: str = "hayat",
    source_store_id: str = "store-1",
    event_type: str = "sales.order.completed",
):
    """Ingest one event the way a webhook would (raw JSON body + header type)."""
    body = json.dumps(raw_envelope(
        source_system=source_system,
        source_store_id=source_store_id,
        source_event_id=source_event_id,
        payload=payload or {"total_amount": 175000},
    )).encode("utf-8")
    return service.ingest(
        IncomingEvent(connector=source_system, raw_body=body, event_type=event_type),
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
    )


# ---------------------------------------------------------------------------
# Cursor codec
# ---------------------------------------------------------------------------


class TestSyncCursor:
    def test_round_trip(self):
        cursor = encode_sync_cursor(NOW, "evt-9")
        assert cursor.value
        decoded = decode_sync_cursor(cursor)
        assert decoded is not None
        updated_at, last_id = decoded
        assert updated_at == NOW
        assert last_id == "evt-9"

    def test_first_page_is_none(self):
        assert decode_sync_cursor(None) is None
        assert decode_sync_cursor(SyncCursor(value=None)) is None
        assert decode_sync_cursor(SyncCursor(value="  ")) is None

    @pytest.mark.parametrize(
        "value",
        ["not-base64!!!", "aGVsbG8=", "e30=", "eyJ2IjoyLCJ1IjoiMjAyNi0xMS0xOFQwOTowMDowMFoiLCJpIjoiZSJ9"],
    )
    def test_malformed_cursor_rejected(self, value):
        with pytest.raises(InvalidSyncCursorError):
            decode_sync_cursor(SyncCursor(value=value))

    def test_cursor_with_missing_fields_rejected(self):
        import base64

        raw = base64.urlsafe_b64encode(b'{"v":1}').decode("ascii")
        with pytest.raises(InvalidSyncCursorError):
            decode_sync_cursor(SyncCursor(value=raw))

    def test_cursor_with_bad_timestamp_rejected(self):
        import base64

        raw = base64.urlsafe_b64encode(b'{"v":1,"u":"not-a-date","i":"e"}').decode("ascii")
        with pytest.raises(InvalidSyncCursorError):
            decode_sync_cursor(SyncCursor(value=raw))


class TestValidatePageSize:
    def test_valid(self):
        assert validate_page_size(1) == 1
        assert validate_page_size(MAX_SYNC_PAGE_SIZE) == MAX_SYNC_PAGE_SIZE

    @pytest.mark.parametrize("bad", [0, -1, MAX_SYNC_PAGE_SIZE + 1, True, "10", 1.5])
    def test_invalid(self, bad):
        with pytest.raises(InvalidSyncRequestError):
            validate_page_size(bad)


# ---------------------------------------------------------------------------
# Store change → canonical event mapping
# ---------------------------------------------------------------------------


class TestMapStoreChange:
    @pytest.mark.parametrize(
        "event_name,expected_types,payload,expected_entity",
        [
            (
                "order.completed",
                ("sales.order.completed",),
                {"order_id": "ord-1", "total_amount": 175000},
                ("order", "ord-1"),
            ),
            (
                "order.cancelled",
                ("sales.order.cancelled",),
                {"order_id": "ord-1", "total_amount": 175000},
                ("order_cancellation", "ord-1"),
            ),
            (
                "payment.captured",
                ("payment.captured",),
                {"gateway_payment_id": "pay-1", "gross_amount": 175000},
                ("payment", "pay-1"),
            ),
            (
                "payment.refunded",
                ("payment.refunded",),
                {"refund_id": "ref-1", "gross_amount": 50000},
                ("payment_refund", "ref-1"),
            ),
        ],
    )
    def test_single_event_mappings(self, event_name, expected_types, payload, expected_entity):
        # The row's own entity deliberately disagrees with the
        # canonical one (a cancellation row carries the parent
        # order's entity): the mapper derives the canonical entity
        # from the payload, never copies the row.
        mapped = map_store_change(
            make_change(event_id="evt-1", event_name=event_name, payload=payload)
        )
        assert tuple(s.event_type.value for s in mapped) == expected_types
        assert mapped[0].event.source_event_id == "evt-1"
        assert mapped[0].event.source_system == "hayat"
        assert mapped[0].event.source_store_id == "store-1"
        assert mapped[0].event.occurred_at == NOW
        assert mapped[0].event.external_entity_type == expected_entity[0]
        assert mapped[0].event.external_entity_id == expected_entity[1]
        assert mapped[0].event.currency == "IRR"
        assert mapped[0].event.payload == payload
        # The default idempotency key is the webhook's key for the same event.
        assert mapped[0].event.idempotency_key == "event:hayat:store-1:evt-1"

    def test_refund_yields_two_events_with_distinct_keys(self):
        mapped = map_store_change(
            make_change(
                event_id="refund-7",
                event_name="refund.created",
                payload={"refund_id": "ref-7", "refund_amount": 50000},
            )
        )
        assert tuple(s.event_type.value for s in mapped) == (
            "sales.refund.created",
            "payment.refunded",
        )
        assert mapped[0].event.source_event_id == "refund-7"
        assert mapped[1].event.source_event_id == f"refund-7{REFUND_PAYMENT_EVENT_SUFFIX}"
        assert mapped[0].event.idempotency_key == "event:hayat:store-1:refund-7"
        assert (
            mapped[1].event.idempotency_key
            == f"event:hayat:store-1:refund-7{REFUND_PAYMENT_EVENT_SUFFIX}"
        )
        # Both events carry the same refund payload (the S1 mapping).
        assert mapped[0].event.payload == mapped[1].event.payload == {
            "refund_id": "ref-7",
            "refund_amount": 50000,
        }
        # Each leg identifies its OWN entity (the payment leg is the
        # payment refund, never the parent refund).
        assert mapped[0].event.external_entity_type == "refund"
        assert mapped[0].event.external_entity_id == "ref-7"
        assert mapped[1].event.external_entity_type == "payment_refund"
        assert mapped[1].event.external_entity_id == "ref-7"

    def test_missing_payload_id_falls_back_to_source_event_id(self):
        # The mapper never raises for a missing id (it runs inside
        # fetch_changes, where a raise would fail the whole page):
        # the pipeline validator rejects the event with the honest
        # "missing required field" error instead.
        mapped = map_store_change(
            make_change(event_id="evt-9", event_name="order.cancelled", payload={})
        )
        assert mapped[0].event.external_entity_type == "order_cancellation"
        assert mapped[0].event.external_entity_id == "evt-9"

    def test_polled_events_pass_the_store_validator(self):
        # The proof a poll-only refund/cancel now processes instead
        # of routing to review: a fully valid payload per type maps
        # to an envelope the US-511 validator accepts (entity +
        # payload together).
        cases = [
            (
                "order.completed",
                {
                    "order_id": "order-1",
                    "customer": {"id": "cust-1", "email": "a@b.ir"},
                    "lines": [
                        {
                            "product_sku": "SKU-1",
                            "quantity": 2,
                            "unit_price": 100000,
                            "line_total": 200000,
                        },
                        {"product_sku": "SKU-2", "quantity": 1, "unit_price": 50000},
                    ],
                    "subtotal_amount": 250000,
                    "shipping_amount": 30000,
                    "discount_amount": 10000,
                    "tax_amount": 5000,
                    "total_amount": 275000,
                    "status": "completed",
                },
            ),
            (
                "order.cancelled",
                {"order_id": "order-1", "reason": "customer request"},
            ),
            (
                "refund.created",
                {
                    "refund_id": "ref-1",
                    "order_id": "order-1",
                    "refund_amount": 50000,
                    "payment_channel": "zarinpal",
                    "gross_amount": 50000,
                },
            ),
            (
                "payment.captured",
                {
                    "payment_channel": "zarinpal",
                    "gateway_payment_id": "pay-1",
                    "gross_amount": 275000,
                    "order_id": "order-1",
                },
            ),
            (
                "payment.refunded",
                {
                    "refund_id": "ref-1",
                    "gross_amount": 50000,
                    "payment_channel": "zarinpal",
                },
            ),
        ]
        for event_name, payload in cases:
            mapped = map_store_change(
                make_change(event_id="evt-valid", event_name=event_name, payload=payload)
            )
            assert mapped, f"{event_name} mapped to no events"
            for synced in mapped:
                validate_store_payload(
                    synced.event_type,
                    synced.event.payload,
                    external_entity_type=synced.event.external_entity_type,
                    external_entity_id=synced.event.external_entity_id,
                )

    @pytest.mark.parametrize(
        "event_name",
        ["bank_transfer.recorded", "shipment.dispatched", "order.shipped"],
    )
    def test_unmapped_event_names_return_no_events(self, event_name):
        assert map_store_change(make_change(event_id="evt-1", event_name=event_name)) == ()

    def test_cheque_change_is_mapped_to_intake(self):
        """US-533: cheque.registered maps to cheque.received.registered."""
        change = make_change(
            event_id="chq-1",
            event_name="cheque.registered",
            entity_type=StoreEntityKind.CHEQUE,
            entity_id="cheque-1",
            payload={
                "sayad_id": "1234567890123456",
                "amount_toman": 1750000,
                "amount_currency": "IRT",
                "issue_date": "1405-08-28",
                "due_date": "1405-09-28",
                "issuer_name": "شرکت نمونه",
                "issuer_national_id": "1234567890",
                "issuer_bank": "056 سامان",
                "payee_name": "کافه ایوان",
                "payee_national_id": "00123456789",
                "order_id": "order-1",
                "customer_ref": "cust-1",
                "sayad_registered": True,
                "purpose": "payment",
            },
        )
        mapped = map_store_change(change)
        assert len(mapped) == 1
        assert mapped[0].event_type == CanonicalEventType.CHEQUE_RECEIVED_REGISTERED
        assert mapped[0].event.external_entity_type == "cheque"
        assert mapped[0].event.external_entity_id == "chq-1"
        validate_store_payload(
            mapped[0].event_type,
            mapped[0].event.payload,
            external_entity_type=mapped[0].event.external_entity_type,
            external_entity_id=mapped[0].event.external_entity_id,
        )
        assert change.entity_type == StoreEntityKind.CHEQUE

    def test_store_change_rejects_empty_identity_fields(self):
        with pytest.raises(InvalidSyncRequestError):
            make_change(event_id="  ")
        with pytest.raises(InvalidSyncRequestError):
            make_change(event_id="evt-1", event_name="")

    def test_naive_updated_at_is_assumed_utc(self):
        change = make_change(
            event_id="evt-1", updated_at=datetime.datetime(2026, 11, 18, 9, 0)
        )
        assert change.updated_at.tzinfo == datetime.UTC


# ---------------------------------------------------------------------------
# ingest_verified (the shared post-verification path)
# ---------------------------------------------------------------------------


class TestIngestVerified:
    def test_duplicate_replay_via_verified_path(self):
        service, inbox, _ = make_ingest()
        first = ingest_webhook(service, source_event_id="evt-1")
        assert first.status == "created"
        event = first.event
        # Re-ingest the same envelope through ingest_verified (the sync path).
        canonical = CanonicalEvent.from_raw(
            raw_envelope(source_event_id="evt-1", payload={"total_amount": 175000})
        )
        result = service.ingest_verified(
            canonical,
            CanonicalEventType.SALES_ORDER_COMPLETED,
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            verification_note="polled",
        )
        assert result.status == "duplicate"
        assert result.event.id == event.id
        assert len(inbox.events) == 1

    def test_same_key_different_payload_conflicts(self):
        service, _, _ = make_ingest()
        ingest_webhook(service, source_event_id="evt-1", payload={"total_amount": 175000})
        canonical = CanonicalEvent.from_raw(
            raw_envelope(source_event_id="evt-1", payload={"total_amount": 999})
        )
        with pytest.raises(IdempotencyConflictError):
            service.ingest_verified(
                canonical,
                CanonicalEventType.SALES_ORDER_COMPLETED,
                tenant_id=TENANT_ID,
                installation_id=INSTALLATION_ID,
            )


# ---------------------------------------------------------------------------
# Sync service
# ---------------------------------------------------------------------------


class TestStoreSyncService:
    def test_missed_webhook_filled_by_polling_with_same_key(self):
        """The 5ب exit criterion: webhook + poll of the same event = one inbox row."""
        service, inbox, _ = make_ingest()
        sync = StoreSyncService(service)
        # The webhook arrived for evt-1 but evt-2's webhook was missed.
        ingest_webhook(service, source_event_id="evt-1")
        connector = FakeConnector(
            [
                make_change(event_id="evt-1"),
                make_change(event_id="evt-2"),
            ]
        )
        report = sync.sync(
            connector,
            source_system="hayat",
            source_store_id="store-1",
            connector_kind="api",
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            updated_since=NOW - datetime.timedelta(days=1),
        )
        assert report.status == "completed"
        assert report.events_created == 1  # only the missed evt-2
        assert report.events_duplicate == 1  # evt-1 was already there
        assert report.pages_fetched == 1
        assert len(inbox.events) == 2
        assert report.has_more is False
        assert report.next_cursor is None

    def test_backfill_pages_and_rerun_is_all_duplicates(self):
        service, inbox, _ = make_ingest()
        sync = StoreSyncService(service)
        changes = [make_change(event_id=f"evt-{i}") for i in range(5)]
        connector = FakeConnector(changes)
        report = sync.sync(
            connector,
            source_system="hayat",
            source_store_id="store-1",
            connector_kind="db",
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            updated_since=NOW - datetime.timedelta(days=30),
            page_size=2,
        )
        assert report.status == "completed"
        assert report.events_created == 5
        assert report.events_duplicate == 0
        assert report.pages_fetched == 3
        assert len(inbox.events) == 5
        # A 30-day backfill re-run creates nothing.
        rerun = sync.sync(
            connector,
            source_system="hayat",
            source_store_id="store-1",
            connector_kind="db",
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            updated_since=NOW - datetime.timedelta(days=30),
            page_size=2,
        )
        assert rerun.events_created == 0
        assert rerun.events_duplicate == 5
        assert len(inbox.events) == 5

    def test_multi_store_same_event_ids_stay_separate(self):
        service, inbox, _ = make_ingest()
        sync = StoreSyncService(service)
        store_a = FakeConnector([make_change(event_id="evt-1", source_store_id="store-a")])
        store_b = FakeConnector([make_change(event_id="evt-1", source_store_id="store-b")])
        report_a = sync.sync(
            store_a,
            source_system="hayat",
            source_store_id="store-a",
            connector_kind="api",
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
        )
        report_b = sync.sync(
            store_b,
            source_system="hayat",
            source_store_id="store-b",
            connector_kind="api",
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
        )
        assert report_a.events_created == 1
        assert report_b.events_created == 1
        assert len(inbox.events) == 2
        keys = {event.idempotency_key for event in inbox.events.values()}
        assert keys == {"event:hayat:store-a:evt-1", "event:hayat:store-b:evt-1"}

    def test_cross_store_event_aborts_the_run(self):
        service, inbox, _ = make_ingest()
        sync = StoreSyncService(service)
        connector = FakeConnector(
            [
                make_change(event_id="evt-1", source_store_id="store-1"),
                make_change(event_id="evt-2", source_store_id="store-OTHER"),
            ]
        )
        with pytest.raises(StoreSyncFailedError, match="while syncing"):
            sync.sync(
                connector,
                source_system="hayat",
                source_store_id="store-1",
                connector_kind="api",
                tenant_id=TENANT_ID,
                installation_id=INSTALLATION_ID,
            )
        # The first event was ingested before the foreign event was seen;
        # the caller rolls the run back on the raised error (the service
        # never commits). The foreign event itself was never ingested.
        assert all(
            event.source_store_id == "store-1" for event in inbox.events.values()
        )

    def test_conflict_is_recorded_and_the_run_continues(self):
        service, inbox, _ = make_ingest()
        sync = StoreSyncService(service)
        ingest_webhook(service, source_event_id="evt-1", payload={"total_amount": 175000})
        connector = FakeConnector(
            [
                make_change(event_id="evt-1", payload={"total_amount": 999}),
                make_change(event_id="evt-2"),
            ]
        )
        report = sync.sync(
            connector,
            source_system="hayat",
            source_store_id="store-1",
            connector_kind="api",
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
        )
        assert report.status == "completed"
        assert report.error_count == 1
        assert report.errors[0].source_event_id == "evt-1"
        assert report.errors[0].code == "IDEMPOTENCY_CONFLICT"
        assert report.events_created == 1  # evt-2 still ingested
        assert len(inbox.events) == 2

    def test_cheque_changes_are_ingested_since_us533(self):
        """US-533: cheque changes now ingest (previously counted unmapped)."""
        service, inbox, _ = make_ingest()
        sync = StoreSyncService(service)
        connector = FakeConnector(
            [
                make_change(
                    event_id="chq-1",
                    event_name="cheque.registered",
                    entity_type=StoreEntityKind.CHEQUE,
                    entity_id="cheque-1",
                ),
                make_change(event_id="evt-1"),
            ]
        )
        report = sync.sync(
            connector,
            source_system="hayat",
            source_store_id="store-1",
            connector_kind="api",
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
        )
        assert report.changes_unmapped == 0
        assert report.events_created == 2
        assert len(inbox.events) == 2

    def test_refund_creates_two_events(self):
        service, inbox, _ = make_ingest()
        sync = StoreSyncService(service)
        connector = FakeConnector(
            [make_change(event_id="refund-7", event_name="refund.created")]
        )
        report = sync.sync(
            connector,
            source_system="hayat",
            source_store_id="store-1",
            connector_kind="api",
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
        )
        assert report.events_created == 2
        assert report.events_by_type == {
            "sales.refund.created": 1,
            "payment.refunded": 1,
        }
        assert len(inbox.events) == 2

    def test_max_pages_caps_the_run_with_resume_cursor(self):
        service, inbox, _ = make_ingest()
        sync = StoreSyncService(service)
        changes = [make_change(event_id=f"evt-{i}") for i in range(5)]
        connector = FakeConnector(changes)
        report = sync.sync(
            connector,
            source_system="hayat",
            source_store_id="store-1",
            connector_kind="api",
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            page_size=2,
            max_pages=1,
        )
        assert report.pages_fetched == 1
        assert report.events_created == 2
        assert report.has_more is True
        assert report.next_cursor is not None
        # Resume from the cursor and finish.
        resumed = sync.sync(
            connector,
            source_system="hayat",
            source_store_id="store-1",
            connector_kind="api",
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            cursor=SyncCursor(value=report.next_cursor),
            page_size=2,
        )
        assert resumed.events_created == 3
        assert resumed.has_more is False
        assert len(inbox.events) == 5

    def test_partial_run_keeps_fetched_pages_and_resumes(self):
        service, inbox, _ = make_ingest()
        sync = StoreSyncService(service)
        changes = [make_change(event_id=f"evt-{i}") for i in range(4)]
        connector = FakeConnector(changes, fail_on_page=2)
        report = sync.sync(
            connector,
            source_system="hayat",
            source_store_id="store-1",
            connector_kind="api",
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            page_size=2,
        )
        assert report.status == "partial"
        assert report.pages_fetched == 1
        assert report.events_created == 2
        assert report.error is not None
        assert report.has_more is True
        assert report.next_cursor is not None
        # The failed page is retried from the resume cursor.
        connector._fail_on_page = None
        resumed = sync.sync(
            connector,
            source_system="hayat",
            source_store_id="store-1",
            connector_kind="api",
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
            cursor=SyncCursor(value=report.next_cursor),
            page_size=2,
        )
        assert resumed.status == "completed"
        assert resumed.events_created == 2
        assert len(inbox.events) == 4

    def test_first_page_failure_propagates(self):
        service, _, _ = make_ingest()
        sync = StoreSyncService(service)
        connector = FakeConnector([make_change(event_id="evt-1")], fail_on_page=1)
        with pytest.raises(StoreUnreachableError):
            sync.sync(
                connector,
                source_system="hayat",
                source_store_id="store-1",
                connector_kind="api",
                tenant_id=TENANT_ID,
                installation_id=INSTALLATION_ID,
            )

    def test_has_more_without_cursor_fails_closed(self):
        service, _, _ = make_ingest()
        sync = StoreSyncService(service)
        connector = FakeConnector(
            [make_change(event_id=f"evt-{i}") for i in range(3)],
            has_more_without_cursor=True,
        )
        with pytest.raises(StoreSyncFailedError, match="next_cursor"):
            sync.sync(
                connector,
                source_system="hayat",
                source_store_id="store-1",
                connector_kind="api",
                tenant_id=TENANT_ID,
                installation_id=INSTALLATION_ID,
                page_size=2,
            )

    def test_invalid_page_size_and_max_pages_rejected(self):
        service, _, _ = make_ingest()
        sync = StoreSyncService(service)
        connector = FakeConnector([])
        with pytest.raises(InvalidSyncRequestError):
            sync.sync(
                connector,
                source_system="hayat",
                source_store_id="store-1",
                connector_kind="api",
                tenant_id=TENANT_ID,
                installation_id=INSTALLATION_ID,
                page_size=0,
            )
        with pytest.raises(InvalidSyncRequestError):
            sync.sync(
                connector,
                source_system="hayat",
                source_store_id="store-1",
                connector_kind="api",
                tenant_id=TENANT_ID,
                installation_id=INSTALLATION_ID,
                max_pages=0,
            )
        with pytest.raises(InvalidSyncRequestError):
            sync.sync(
                connector,
                source_system="hayat",
                source_store_id="store-1",
                connector_kind="api",
                tenant_id=TENANT_ID,
                installation_id=INSTALLATION_ID,
                max_pages=51,
            )

    def test_default_lookback_is_seven_days(self):
        service, _, _ = make_ingest()
        sync = StoreSyncService(service)
        connector = FakeConnector([])
        sync.sync(
            connector,
            source_system="hayat",
            source_store_id="store-1",
            connector_kind="api",
            tenant_id=TENANT_ID,
            installation_id=INSTALLATION_ID,
        )
        assert len(connector.fetch_calls) == 1
        lookback = connector.fetch_calls[0]["updated_since"]
        assert lookback is not None
        delta = datetime.datetime.now(datetime.UTC) - lookback
        assert datetime.timedelta(days=6, hours=23) < delta < datetime.timedelta(days=7, hours=1)
