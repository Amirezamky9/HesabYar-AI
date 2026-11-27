"""Unit tests for the generic store connectors (US-512, S2).

DB-free with respect to the shared Supabase database: the HTTP
connector runs against ``httpx.MockTransport`` and the store-DB
reader runs against a local SQLite FILE database in ``tmp_path`` —
the read-only proof (a write attempt fails) runs against that real
file, not a mock.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from hesabyar.domain.events.envelope import CanonicalEvent, CanonicalEventType
from hesabyar.domain.events.errors import (
    EventSignatureError,
    InvalidEventEnvelopeError,
    InvalidSyncCursorError,
    InvalidSyncRequestError,
    StoreDatabaseError,
    StoreNotConfiguredError,
    StoreSyncFailedError,
    StoreUnreachableError,
)
from hesabyar.domain.events.models import IncomingEvent, VerifiedEvent
from hesabyar.domain.events.sync import SyncCursor
from hesabyar.domain.integrations.models import Store
from hesabyar.infrastructure.auth.credential_vault import (
    FernetCredentialVault,
    generate_encryption_key,
)
from hesabyar.infrastructure.store_connectors.common import BaseStoreConnector
from hesabyar.infrastructure.store_connectors.factory import StoreConnectorFactory
from hesabyar.infrastructure.store_connectors.http_connector import GenericHttpStoreConnector
from hesabyar.infrastructure.store_connectors.queries import (
    DEFAULT_STORE_DB_QUERY_VERSION,
    get_store_db_query,
)
from hesabyar.infrastructure.store_connectors.store_db import (
    StoreDatabaseConnector,
    StoreDatabaseReader,
    assert_read_only_query,
    create_read_only_engine,
)
from tests.event_fixtures import raw_envelope

NOW = datetime.datetime(2026, 11, 18, 9, 0, tzinfo=datetime.UTC)


def make_change_row(
    *,
    event_id: str,
    event_name: str = "order.completed",
    entity_type: str = "order",
    entity_id: str | None = None,
    updated_at: str = "2026-11-18T09:00:00Z",
    status: str = "completed",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one changes-API row (valid by default)."""
    return {
        "event_id": event_id,
        "event_name": event_name,
        "entity_type": entity_type,
        "entity_id": entity_id or f"order-{event_id}",
        "updated_at": updated_at,
        "status": status,
        "payload": payload if payload is not None else {"total_amount": 175000},
    }


def make_store(**overrides: Any) -> Store:
    """Build a registry Store (valid by default)."""
    values: dict[str, Any] = {
        "id": "store-row-1",
        "installation_id": "installation-1",
        "store_key": "hayat",
        "display_name": "Hayat",
        "store_type": "custom",
        "prices_in_toman": True,
        "has_webhook_secret": False,
        "has_api_key": True,
        "has_store_db_dsn": False,
        "api_key_scopes": ("orders:read", "payments:read", "cheques:read"),
        "api_base_url": "https://store.example.com/api/v1",
        "status": "active",
    }
    values.update(overrides)
    return Store(**values)


class FakeRegistry:
    """Minimal IntegrationsRegistryPort for the factory tests."""

    def __init__(self, ciphertexts: dict[tuple[str, str], str]) -> None:
        self._ciphertexts = ciphertexts

    def get_store_ciphertext(self, store_id: str, kind: str) -> str | None:
        return self._ciphertexts.get((store_id, kind))


# ---------------------------------------------------------------------------
# HTTP connector
# ---------------------------------------------------------------------------


class TestHttpConnector:
    def _connector(
        self, handler, *, source_store_id: str = "store-1", prices_in_toman: bool = True
    ) -> GenericHttpStoreConnector:
        return GenericHttpStoreConnector(
            base_url="https://store.example.com/api/v1",
            api_key="hya_store_test_key",
            source_system="hayat",
            source_store_id=source_store_id,
            prices_in_toman=prices_in_toman,
            transport=httpx.MockTransport(handler),
        )

    def test_fetch_sends_auth_params_and_maps_rows(self):
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("Authorization")
            seen["accept"] = request.headers.get("Accept")
            return httpx.Response(
                200,
                json={
                    "changes": [
                        make_change_row(event_id="evt-1"),
                        make_change_row(
                            event_id="chq-1",
                            event_name="cheque.registered",
                            entity_type="cheque",
                            entity_id="cheque-1",
                            updated_at="2026-11-18T09:01:00Z",
                        ),
                    ],
                    "next_cursor": "store-cursor-9",
                    "has_more": True,
                },
            )

        connector = self._connector(handler)
        batch = connector.fetch_changes(
            None, updated_since=NOW - datetime.timedelta(days=7), page_size=50
        )
        assert seen["url"].startswith("https://store.example.com/api/v1/changes")
        assert "page_size=50" in seen["url"]
        assert "updated_since=" in seen["url"]
        assert seen["auth"] == "Bearer hya_store_test_key"
        assert seen["accept"] == "application/json"
        assert len(batch.events) == 2
        assert batch.events[0].event_type == CanonicalEventType.SALES_ORDER_COMPLETED
        assert batch.events[0].event.source_event_id == "evt-1"
        assert batch.events[0].event.idempotency_key == "event:hayat:store-1:evt-1"
        # chq-1 maps since ADR 0004 (US-530) approved cheque.registered
        # and US-533 wired it in — it was the unmapped example before.
        assert batch.events[1].event_type == CanonicalEventType.CHEQUE_RECEIVED_REGISTERED
        assert batch.events[1].event.source_event_id == "chq-1"
        assert batch.unmapped_changes == []
        assert batch.next_cursor is not None and batch.next_cursor.value == "store-cursor-9"
        assert batch.has_more is True
        connector.close()

    def test_store_cursor_passed_through_unchanged(self):
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, json={"changes": [], "has_more": False})

        connector = self._connector(handler)
        connector.fetch_changes(SyncCursor(value="opaque-store-cursor/abc+123=="))
        assert "cursor=opaque-store-cursor%2Fabc%2B123%3D%3D" in seen[0]
        connector.close()

    def test_out_of_order_page_rejected(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "changes": [
                        make_change_row(event_id="evt-2", updated_at="2026-11-18T09:02:00Z"),
                        make_change_row(event_id="evt-1", updated_at="2026-11-18T09:01:00Z"),
                    ],
                    "has_more": False,
                },
            )

        connector = self._connector(handler)
        with pytest.raises(StoreSyncFailedError, match="out of order"):
            connector.fetch_changes(None)
        connector.close()

    def test_equal_timestamps_are_allowed(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "changes": [
                        make_change_row(event_id="evt-1"),
                        make_change_row(event_id="evt-2"),
                    ],
                    "has_more": False,
                },
            )

        connector = self._connector(handler)
        batch = connector.fetch_changes(None)
        assert len(batch.events) == 2
        connector.close()

    def test_server_error_is_unreachable(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"error": "down"})

        connector = self._connector(handler)
        with pytest.raises(StoreUnreachableError):
            connector.fetch_changes(None)
        connector.close()

    def test_client_error_is_sync_failed(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "bad key"})

        connector = self._connector(handler)
        with pytest.raises(StoreSyncFailedError):
            connector.fetch_changes(None)
        connector.close()

    def test_transport_error_is_unreachable(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        connector = self._connector(handler)
        with pytest.raises(StoreUnreachableError):
            connector.fetch_changes(None)
        connector.close()

    def test_timeout_is_unreachable(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=request)

        connector = self._connector(handler)
        with pytest.raises(StoreUnreachableError):
            connector.fetch_changes(None)
        connector.close()

    def test_malformed_page_is_sync_failed(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"changes": [{"event_id": "evt-1"}]})

        connector = self._connector(handler)
        with pytest.raises(StoreSyncFailedError):
            connector.fetch_changes(None)
        connector.close()

    def test_unknown_entity_type_is_sync_failed(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"changes": [make_change_row(event_id="evt-1", entity_type="coupon")]},
            )

        connector = self._connector(handler)
        with pytest.raises(StoreSyncFailedError):
            connector.fetch_changes(None)
        connector.close()

    def test_invalid_base_url_rejected(self):
        with pytest.raises(StoreSyncFailedError):
            GenericHttpStoreConnector(
                base_url="ftp://store.example.com",
                api_key="k",
                source_system="hayat",
                source_store_id="store-1",
            )

    def test_invalid_page_size_rejected(self):
        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            return httpx.Response(200, json={"changes": []})

        connector = self._connector(handler)
        with pytest.raises(InvalidSyncRequestError):
            connector.fetch_changes(None, page_size=0)
        connector.close()


# ---------------------------------------------------------------------------
# Shared connector behavior (verify / normalize / acknowledge)
# ---------------------------------------------------------------------------


class TestBaseConnector:
    def _connector(self, **kwargs: Any) -> GenericHttpStoreConnector:
        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            return httpx.Response(200, json={"changes": []})

        return GenericHttpStoreConnector(
            base_url="https://store.example.com/api/v1",
            api_key="k",
            source_system="hayat",
            source_store_id="store-1",
            transport=httpx.MockTransport(handler),
            **kwargs,
        )

    def _incoming(
        self, *, signature: str | None = None, event_type: str = "sales.order.completed"
    ) -> IncomingEvent:
        body = json.dumps(raw_envelope()).encode("utf-8")
        return IncomingEvent(
            connector="hayat", raw_body=body, signature=signature, event_type=event_type
        )

    def test_verify_without_secret_skips_and_records(self):
        connector = self._connector()
        verified = connector.verify_event(self._incoming())
        assert verified.signature_verified is False
        assert "skipped" in verified.verification_note
        assert verified.event_type == CanonicalEventType.SALES_ORDER_COMPLETED
        connector.close()

    def test_verify_with_secret_checks_hmac(self):
        secret = "whsec-test"
        connector = self._connector(webhook_secret=secret)
        body = json.dumps(raw_envelope()).encode("utf-8")
        signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        verified = connector.verify_event(
            IncomingEvent(
                connector="hayat",
                raw_body=body,
                signature=signature,
                event_type="sales.order.completed",
            )
        )
        assert verified.signature_verified is True
        with pytest.raises(EventSignatureError):
            connector.verify_event(
                IncomingEvent(
                    connector="hayat",
                    raw_body=body,
                    signature="0" * 64,
                    event_type="sales.order.completed",
                )
            )
        with pytest.raises(EventSignatureError):
            connector.verify_event(self._incoming(signature=None))
        connector.close()

    def test_verify_rejects_foreign_store(self):
        connector = self._connector()
        body = json.dumps(raw_envelope(source_store_id="store-OTHER")).encode("utf-8")
        with pytest.raises(InvalidEventEnvelopeError):
            connector.verify_event(
                IncomingEvent(
                    connector="hayat", raw_body=body, event_type="sales.order.completed"
                )
            )
        connector.close()

    def test_normalize_converts_toman_to_rial(self):
        connector = self._connector(prices_in_toman=True)
        event = CanonicalEvent.from_raw(raw_envelope(payload={"total_amount": 175}))
        verified = VerifiedEvent(
            event=event,
            event_type=CanonicalEventType.SALES_ORDER_COMPLETED,
            signature_verified=False,
            verification_note="test",
        )
        normalized = connector.normalize_event(verified)
        assert normalized.converted_payload["total_amount"] == 1750
        assert normalized.store_settings.prices_in_toman is True
        assert len(normalized.conversions) == 1
        connector.close()

    def test_acknowledge_is_a_noop(self):
        connector = self._connector()
        event = CanonicalEvent.from_raw(raw_envelope())
        connector.acknowledge(event, None)  # type: ignore[arg-type]
        connector.close()

    def test_base_connector_is_abstract_on_fetch_changes(self):
        with pytest.raises(TypeError):
            BaseStoreConnector(source_system="hayat", source_store_id="store-1")  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# Read-only store-DB read
# ---------------------------------------------------------------------------


def build_store_db(path: Path) -> None:
    """Create a SQLite store database with the v1 view contract (as a table here)."""
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE hesabyar_changes (
                store_id TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                event_name TEXT NOT NULL,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                payload TEXT NOT NULL
            )
            """
        )
        rows = [
            ("store-1", "order", "order-1", "evt-1", "order.completed", "completed",
             "2026-11-18T09:00:00+00:00", json.dumps({"total_amount": 175000})),
            ("store-1", "order", "order-2", "evt-2", "order.completed", "completed",
             "2026-11-18T09:00:00+00:00", json.dumps({"total_amount": 200000})),
            ("store-1", "payment", "pay-1", "evt-3", "payment.captured", "captured",
             "2026-11-18T09:01:00+00:00", json.dumps({"gross_amount": 175000})),
            ("store-1", "cheque", "cheque-1", "chq-1", "cheque.registered", "registered",
             "2026-11-18T09:02:00+00:00", json.dumps({"amount": 1000000})),
            ("store-1", "order", "order-3", "evt-4", "refund.created", "refunded",
             "2026-11-18T09:03:00+00:00", json.dumps({"refund_amount": 50000})),
            ("store-2", "order", "order-9", "evt-9", "order.completed", "completed",
             "2026-11-18T09:00:00+00:00", json.dumps({"total_amount": 999})),
        ]
        conn.executemany(
            "INSERT INTO hesabyar_changes VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def store_db_path(tmp_path: Path) -> Path:
    path = tmp_path / "store.db"
    build_store_db(path)
    return path


class TestReadOnlyQueryGuard:
    def test_v1_query_passes(self):
        assert_read_only_query(get_store_db_query("v1"))

    @pytest.mark.parametrize(
        "sql",
        [
            "INSERT INTO hesabyar_changes VALUES (1)",
            "UPDATE hesabyar_changes SET status='x'",
            "DELETE FROM hesabyar_changes",
            "DROP TABLE hesabyar_changes",
            "SELECT 1; DELETE FROM hesabyar_changes",
            "SELECT 1",
            "",
            "   ",
            "CREATE TABLE x (a INT)",
            "WITH x AS (SELECT 1) DELETE FROM hesabyar_changes",
        ],
    )
    def test_non_select_rejected(self, sql):
        if sql.strip() == "SELECT 1":
            # A bare SELECT is fine — the guard allows it.
            assert_read_only_query(sql)
            return
        with pytest.raises(StoreDatabaseError):
            assert_read_only_query(sql)

    def test_updated_at_column_does_not_trip_update_check(self):
        assert_read_only_query("SELECT updated_at FROM hesabyar_changes ORDER BY updated_at")


class TestCreateReadOnlyEngine:
    def test_memory_sqlite_rejected(self):
        with pytest.raises(StoreDatabaseError):
            create_read_only_engine("sqlite:///:memory:")

    def test_unknown_scheme_rejected(self):
        with pytest.raises(StoreDatabaseError):
            create_read_only_engine("mysql://user@host/db")

    def test_unknown_query_version_rejected_at_construction(self, store_db_path: Path):
        engine = create_read_only_engine(f"sqlite:///{store_db_path}")
        try:
            with pytest.raises(StoreDatabaseError):
                StoreDatabaseReader(engine, source_system="hayat", query_version="v99")
        finally:
            engine.dispose()


class TestStoreDatabaseReader:
    def test_zero_writes_proven_insert_update_delete_create_fail(
        self, store_db_path: Path
    ):
        """The AC's proof: a write attempt through the reader's connection FAILS."""
        engine = create_read_only_engine(f"sqlite:///{store_db_path}")
        try:
            reader = StoreDatabaseReader(engine, source_system="hayat")
            # The port exposes no write method at all.
            assert not hasattr(reader, "insert")
            assert not hasattr(reader, "update")
            assert not hasattr(reader, "delete")
            assert not hasattr(reader, "execute")
            for sql in (
                "INSERT INTO hesabyar_changes VALUES ('s','order','o','e','n','s','t','{}')",
                "UPDATE hesabyar_changes SET status='x'",
                "DELETE FROM hesabyar_changes",
                "CREATE TABLE evil (a INT)",
                "DROP TABLE hesabyar_changes",
            ):
                # A fresh connection per attempt: a failed statement
                # must not poison the next attempt.
                with engine.connect() as attempt:
                    with pytest.raises(OperationalError, match="readonly"):
                        attempt.execute(text(sql))
            # And the data is untouched: reads still see all six rows.
            page = reader.fetch_changes(
                store_id="store-1", updated_since=None, cursor=None, page_size=100
            )
            assert len(page.changes) == 5  # store-1 only (store-2's row excluded)
        finally:
            engine.dispose()

    def test_fetch_changes_stable_sort_and_keyset_pagination(self, store_db_path: Path):
        engine = create_read_only_engine(f"sqlite:///{store_db_path}")
        try:
            reader = StoreDatabaseReader(engine, source_system="hayat")
            first = reader.fetch_changes(
                store_id="store-1", updated_since=None, cursor=None, page_size=2
            )
            assert [c.event_id for c in first.changes] == ["evt-1", "evt-2"]
            assert first.has_more is True
            # Same timestamp (09:00) for evt-1/evt-2: the event-id
            # tiebreaker keeps the order total and stable.
            assert first.changes[0].updated_at == first.changes[1].updated_at
            # Build the resume cursor the way the connector does.
            from hesabyar.domain.events.sync import encode_sync_cursor

            last = first.changes[-1]
            cursor = encode_sync_cursor(last.updated_at, last.event_id)
            second = reader.fetch_changes(
                store_id="store-1", updated_since=None, cursor=cursor, page_size=2
            )
            assert [c.event_id for c in second.changes] == ["evt-3", "chq-1"]
            assert second.has_more is True
            last = second.changes[-1]
            cursor = encode_sync_cursor(last.updated_at, last.event_id)
            third = reader.fetch_changes(
                store_id="store-1", updated_since=None, cursor=cursor, page_size=2
            )
            assert [c.event_id for c in third.changes] == ["evt-4"]
            assert third.has_more is False
        finally:
            engine.dispose()

    def test_fetch_changes_filters_by_store_and_updated_since(self, store_db_path: Path):
        engine = create_read_only_engine(f"sqlite:///{store_db_path}")
        try:
            reader = StoreDatabaseReader(engine, source_system="hayat")
            store2 = reader.fetch_changes(
                store_id="store-2", updated_since=None, cursor=None, page_size=100
            )
            assert [c.event_id for c in store2.changes] == ["evt-9"]
            since = reader.fetch_changes(
                store_id="store-1",
                updated_since=datetime.datetime(2026, 11, 18, 9, 1, tzinfo=datetime.UTC),
                cursor=None,
                page_size=100,
            )
            assert [c.event_id for c in since.changes] == ["evt-3", "chq-1", "evt-4"]
            empty = reader.fetch_changes(
                store_id="store-1",
                updated_since=datetime.datetime(2026, 11, 19, tzinfo=datetime.UTC),
                cursor=None,
                page_size=100,
            )
            assert empty.changes == []
            assert empty.has_more is False
        finally:
            engine.dispose()

    def test_malformed_cursor_rejected(self, store_db_path: Path):
        engine = create_read_only_engine(f"sqlite:///{store_db_path}")
        try:
            reader = StoreDatabaseReader(engine, source_system="hayat")
            with pytest.raises(InvalidSyncCursorError):
                reader.fetch_changes(
                    store_id="store-1",
                    updated_since=None,
                    cursor=SyncCursor(value="garbage"),
                    page_size=10,
                )
        finally:
            engine.dispose()

    def test_malformed_rows_fail_closed(self, tmp_path: Path):
        path = tmp_path / "bad.db"
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                """
                CREATE TABLE hesabyar_changes (
                    store_id TEXT, entity_type TEXT, entity_id TEXT, event_id TEXT,
                    event_name TEXT, status TEXT, updated_at TEXT, payload TEXT
                )
                """
            )
            conn.execute(
                "INSERT INTO hesabyar_changes VALUES (?,?,?,?,?,?,?,?)",
                ("store-1", "coupon", "c-1", "evt-x", "order.completed", "completed",
                 "2026-11-18T09:00:00+00:00", "{}"),
            )
            conn.commit()
        finally:
            conn.close()
        engine = create_read_only_engine(f"sqlite:///{path}")
        try:
            reader = StoreDatabaseReader(engine, source_system="hayat")
            with pytest.raises(StoreDatabaseError, match="entity_type"):
                reader.fetch_changes(
                    store_id="store-1", updated_since=None, cursor=None, page_size=10
                )
        finally:
            engine.dispose()

    def test_reader_stamps_source_system(self, store_db_path: Path):
        engine = create_read_only_engine(f"sqlite:///{store_db_path}")
        try:
            reader = StoreDatabaseReader(engine, source_system="hayat")
            page = reader.fetch_changes(
                store_id="store-1", updated_since=None, cursor=None, page_size=1
            )
            assert page.changes[0].source_system == "hayat"
            assert page.changes[0].source_store_id == "store-1"
            assert page.changes[0].payload == {"total_amount": 175000}
        finally:
            engine.dispose()


class TestStoreDatabaseConnector:
    def test_fetch_changes_end_to_end_with_cursor(self, store_db_path: Path):
        engine = create_read_only_engine(f"sqlite:///{store_db_path}")
        try:
            reader = StoreDatabaseReader(engine, source_system="hayat")
            connector = StoreDatabaseConnector(
                reader, source_system="hayat", source_store_id="store-1"
            )
            assert connector.query_version == DEFAULT_STORE_DB_QUERY_VERSION
            first = connector.fetch_changes(None, page_size=3)
            assert [s.event.source_event_id for s in first.events] == ["evt-1", "evt-2", "evt-3"]
            assert first.unmapped_changes == []
            assert first.has_more is True
            assert first.next_cursor is not None
            second = connector.fetch_changes(first.next_cursor, page_size=3)
            # chq-1 maps since ADR 0004 (US-530); evt-4 is a refund → two events.
            assert [s.event.source_event_id for s in second.events] == [
                "chq-1",
                "evt-4",
                "evt-4:payment-refunded",
            ]
            assert second.unmapped_changes == []
            assert second.has_more is False
            assert second.next_cursor is None
            connector.close()
        finally:
            engine.dispose()

    def test_connector_scopes_to_its_store(self, store_db_path: Path):
        engine = create_read_only_engine(f"sqlite:///{store_db_path}")
        try:
            reader = StoreDatabaseReader(engine, source_system="hayat")
            connector = StoreDatabaseConnector(
                reader, source_system="hayat", source_store_id="store-2"
            )
            batch = connector.fetch_changes(None, page_size=100)
            assert [s.event.source_event_id for s in batch.events] == ["evt-9"]
            connector.close()
        finally:
            engine.dispose()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


class TestStoreConnectorFactory:
    def _factory(
        self, ciphertexts: dict[tuple[str, str], str]
    ) -> tuple[StoreConnectorFactory, FernetCredentialVault]:
        vault = FernetCredentialVault(generate_encryption_key())
        return StoreConnectorFactory(FakeRegistry(ciphertexts), vault), vault

    def test_build_http_connector(self):
        vault = FernetCredentialVault(generate_encryption_key())
        ciphertext = vault.encrypt("hya_store_secret_key")
        factory = StoreConnectorFactory(
            FakeRegistry({("store-row-1", "api_key"): ciphertext}), vault
        )
        connector = factory.build(make_store(), source_store_id="store-1", source="api")
        assert isinstance(connector, GenericHttpStoreConnector)
        assert connector.source_system == "hayat"
        assert connector.source_store_id == "store-1"
        factory.close()

    def test_build_http_without_base_url_fails(self):
        factory, _ = self._factory({})
        with pytest.raises(StoreNotConfiguredError, match="base URL"):
            factory.build(
                make_store(api_base_url=None), source_store_id="store-1", source="api"
            )

    def test_build_http_without_api_key_fails(self):
        factory, _ = self._factory({})
        with pytest.raises(StoreNotConfiguredError, match="API key"):
            factory.build(make_store(), source_store_id="store-1", source="api")

    def test_build_db_connector(self, store_db_path: Path):
        vault = FernetCredentialVault(generate_encryption_key())
        ciphertext = vault.encrypt(f"sqlite:///{store_db_path}")
        factory = StoreConnectorFactory(
            FakeRegistry({("store-row-1", "store_db_dsn"): ciphertext}), vault
        )
        connector = factory.build(
            make_store(has_store_db_dsn=True), source_store_id="store-1", source="db"
        )
        assert isinstance(connector, StoreDatabaseConnector)
        batch = connector.fetch_changes(None, page_size=100)
        assert len(batch.events) == 6  # 5 mapped rows (chq-1 maps since ADR 0004) + refund's second event
        factory.close()

    def test_build_db_without_dsn_fails(self):
        factory, _ = self._factory({})
        with pytest.raises(StoreNotConfiguredError, match="DSN"):
            factory.build(make_store(), source_store_id="store-1", source="db")

    def test_unknown_source_rejected(self):
        factory, _ = self._factory({})
        with pytest.raises(InvalidSyncRequestError):
            factory.build(make_store(), source_store_id="store-1", source="carrier-pigeon")

    def test_close_is_idempotent(self):
        factory, _ = self._factory({})
        factory.close()
        factory.close()
