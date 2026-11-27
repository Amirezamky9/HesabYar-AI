"""Read-only store-database read (US-512, S2 — ADR 0003 decision 3).

The second reconciliation transport: HesabYar reads the store's own
database DIRECTLY — read-only, reconciliation/backfill only — with
the store team's read-only DB user. The primary path is always
webhook + API polling; the DB read exists for backfill and for
stores whose changes API is unavailable.

Read-only BY CONSTRUCTION, in three independent layers:

1. **The port has no write method.** :class:`StoreDatabaseReaderPort`
   exposes only ``fetch_changes`` — there is no API surface that
   could write, so no caller can write through it by accident.
2. **The connection is read-only.** :func:`create_read_only_engine`
   opens SQLite databases with ``mode=ro`` and PostgreSQL
   connections with ``default_transaction_read_only=on`` — the
   database itself rejects writes (proven in tests: an INSERT /
   UPDATE / DELETE through the reader's connection fails). The DB
   user itself must additionally hold SELECT-only grants (the store
   team provisions it); the connection setting is defense-in-depth,
   the grant is the real control.
3. **Only registered queries run.** The reader executes ONLY the
   versioned SELECTs in :mod:`hesabyar.infrastructure.store_connectors.queries`,
   each re-checked by :func:`assert_read_only_query` before
   execution. There is no raw-SQL escape hatch.

The reader returns raw :class:`StoreChange` rows; the connector
maps them through the shared domain mapper, so both transports
produce identical canonical events (and identical idempotency keys)
for the same store change.
"""

from __future__ import annotations

import datetime
import json
import re
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import SQLAlchemyError

from hesabyar.domain.events.errors import (
    InvalidSyncRequestError,
    StoreDatabaseError,
)
from hesabyar.domain.events.sync import (
    DEFAULT_SYNC_PAGE_SIZE,
    StoreChange,
    StoreChangePage,
    StoreEntityKind,
    SyncBatch,
    SyncCursor,
    SyncedEvent,
    decode_sync_cursor,
    encode_sync_cursor,
    map_store_change,
    validate_page_size,
)
from hesabyar.infrastructure.store_connectors.common import BaseStoreConnector
from hesabyar.infrastructure.store_connectors.queries import (
    DEFAULT_STORE_DB_QUERY_VERSION,
    get_store_db_query,
)

#: SQL verbs that must never appear in a reader query (whole-word
#: match — ``updated_at`` does not trip ``UPDATE`` because ``_`` is
#: a word character).
_FORBIDDEN_SQL_VERBS = (
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "ALTER",
    "CREATE",
    "TRUNCATE",
    "GRANT",
    "REVOKE",
    "VACUUM",
    "COPY",
    "CALL",
)

_FORBIDDEN_SQL_RE = re.compile(
    r"\b(?:" + "|".join(_FORBIDDEN_SQL_VERBS) + r")\b", re.IGNORECASE
)


def assert_read_only_query(sql: str) -> str:
    """Reject any SQL that is not a single SELECT/WITH statement.

    Defense-in-depth around the versioned query registry: even if a
    future version somehow carried a write, the reader would refuse
    it before it reached the database.
    """
    cleaned = sql.strip()
    if not cleaned:
        raise StoreDatabaseError("Refusing to execute an empty store-DB query.")
    lowered = cleaned.lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        raise StoreDatabaseError(
            "Refusing to execute a non-SELECT store-DB query: only SELECT is allowed.",
            details={"sql_prefix": cleaned[:40]},
        )
    if ";" in cleaned:
        raise StoreDatabaseError(
            "Refusing to execute a multi-statement store-DB query.",
            details={"sql_prefix": cleaned[:40]},
        )
    forbidden = _FORBIDDEN_SQL_RE.search(cleaned)
    if forbidden is not None:
        raise StoreDatabaseError(
            f"Refusing to execute a store-DB query containing {forbidden.group(0)!r}.",
            details={"sql_prefix": cleaned[:40]},
        )
    return sql


def create_read_only_engine(dsn: str) -> Engine:
    """Create a SQLAlchemy engine whose connections cannot write.

    SQLite: opened with ``mode=ro`` (URI form) — the file is never
    even created if missing, and any write fails at the database.
    PostgreSQL: every connection starts with
    ``default_transaction_read_only=on``. An in-memory SQLite DSN is
    rejected: a read-only reader over an empty in-memory database is
    meaningless (and the test suite uses file databases instead).
    """
    url = make_url(dsn)
    scheme = url.drivername.split("+", 1)[0]
    if scheme == "sqlite":
        database = url.database or ""
        if database in ("", ":memory:"):
            raise StoreDatabaseError(
                "A read-only store-DB reader needs a SQLite file database, "
                "not :memory:.",
                details={"dsn_scheme": "sqlite"},
            )
        # URI form with mode=ro; the path is taken verbatim from the DSN.
        ro_url = f"sqlite:///file:{database}?mode=ro&uri=true"
        return create_engine(ro_url)
    if scheme == "postgresql":
        return create_engine(
            dsn,
            connect_args={"options": "-c default_transaction_read_only=on"},
        )
    raise StoreDatabaseError(
        f"Unsupported store database scheme {scheme!r}; use postgresql:// or sqlite://.",
        details={"dsn_scheme": scheme},
    )


def _iso(value: datetime.datetime | None) -> str | None:
    """Render a datetime bound parameter as UTC ISO-8601 text.

    The reader binds datetimes as ISO strings, never as native
    datetimes: SQLite compares ``updated_at`` lexicographically as
    ISO text (and Python 3.12 deprecates sqlite3's default datetime
    adapter), while PostgreSQL casts the ISO string to timestamptz
    in the comparison. One binding shape serves both.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.UTC)
    return value.astimezone(datetime.UTC).isoformat()


def _parse_updated_at(value: Any, *, event_id: str) -> datetime.datetime:
    """Parse a view's ``updated_at`` (datetime from Postgres, ISO text from SQLite)."""
    if isinstance(value, datetime.datetime):
        parsed = value
    elif isinstance(value, str):
        text_value = value.strip()
        if text_value.endswith("Z"):
            text_value = text_value[:-1] + "+00:00"
        try:
            parsed = datetime.datetime.fromisoformat(text_value)
        except ValueError as exc:
            raise StoreDatabaseError(
                f"Store-DB row for event {event_id!r} has an unparseable updated_at.",
                details={"event_id": event_id, "updated_at": value},
            ) from exc
    else:
        raise StoreDatabaseError(
            f"Store-DB row for event {event_id!r} has an invalid updated_at.",
            details={"event_id": event_id},
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return parsed.astimezone(datetime.UTC)


def _parse_payload(value: Any, *, event_id: str) -> dict[str, Any]:
    """Parse a view's ``payload`` (dict from Postgres jsonb, JSON text from SQLite)."""
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError as exc:
            raise StoreDatabaseError(
                f"Store-DB row for event {event_id!r} has a payload that is not valid JSON.",
                details={"event_id": event_id},
            ) from exc
        if not isinstance(parsed, dict):
            raise StoreDatabaseError(
                f"Store-DB row for event {event_id!r} has a payload that is not a JSON object.",
                details={"event_id": event_id},
            )
        return parsed
    raise StoreDatabaseError(
        f"Store-DB row for event {event_id!r} has an invalid payload.",
        details={"event_id": event_id},
    )


class StoreDatabaseReader:
    """Read-only reader over the store's ``hesabyar_changes`` view.

    Implements :class:`StoreDatabaseReaderPort`. The reader is bound
    to one source system (the view has no such column — the store
    database does not know HesabYar's connector names); the store
    id is scoped per call, so one reader serves one store system
    across its storefronts. The engine is owned by the caller (the
    connector factory disposes it via :meth:`close`); the reader
    never commits anything — it only SELECTs inside short-lived read
    connections.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        source_system: str,
        query_version: str = DEFAULT_STORE_DB_QUERY_VERSION,
    ) -> None:
        self._engine = engine
        self._source_system = source_system
        self._query_version = query_version
        # Fail fast on an unknown version — at construction, not on
        # the first poll.
        get_store_db_query(query_version)

    @property
    def query_version(self) -> str:
        """Versioned query set this reader executes."""
        return self._query_version

    @property
    def engine(self) -> Engine:
        """The underlying read-only engine (tests prove its zero-write property)."""
        return self._engine

    def close(self) -> None:
        """Dispose the engine."""
        self._engine.dispose()

    def fetch_changes(
        self,
        *,
        store_id: str,
        updated_since: datetime.datetime | None,
        cursor: SyncCursor | None,
        page_size: int,
    ) -> StoreChangePage:
        """Read one page of change rows for one store, stably sorted on ``updated_at``."""
        page_size = validate_page_size(page_size)
        position = decode_sync_cursor(cursor)
        sql = assert_read_only_query(get_store_db_query(self._query_version))
        params = {
            "store_id": store_id,
            "updated_since": _iso(updated_since),
            "cursor_updated_at": _iso(position[0]) if position is not None else None,
            "cursor_event_id": position[1] if position is not None else None,
            # One extra row detects whether more pages follow.
            "limit": page_size + 1,
        }
        try:
            with self._engine.connect() as connection:
                rows = connection.execute(text(sql), params).mappings().all()
        except SQLAlchemyError as exc:
            raise StoreDatabaseError(
                f"Reading the store database failed: {exc}",
                details={"store_id": store_id},
            ) from exc
        has_more = len(rows) > page_size
        changes: list[StoreChange] = []
        for row in rows[:page_size]:
            try:
                entity_type = StoreEntityKind(row["entity_type"])
            except ValueError as exc:
                raise StoreDatabaseError(
                    f"Store-DB row for event {row['event_id']!r} has unknown entity_type "
                    f"{row['entity_type']!r}.",
                    details={"event_id": row["event_id"], "entity_type": row["entity_type"]},
                ) from exc
            try:
                changes.append(
                    StoreChange(
                        source_system=self._source_system,
                        source_store_id=store_id,
                        entity_type=entity_type,
                        entity_id=str(row["entity_id"]),
                        event_id=str(row["event_id"]),
                        event_name=str(row["event_name"]),
                        updated_at=_parse_updated_at(row["updated_at"], event_id=str(row["event_id"])),
                        status=str(row["status"]),
                        payload=_parse_payload(row["payload"], event_id=str(row["event_id"])),
                    )
                )
            except InvalidSyncRequestError as exc:
                raise StoreDatabaseError(
                    f"Store-DB row for event {row['event_id']!r} is malformed: {exc.message}",
                    details={"event_id": row["event_id"]},
                ) from exc
        return StoreChangePage(changes=changes, has_more=has_more)


class StoreDatabaseConnector(BaseStoreConnector):
    """Poll a store's database directly (read-only reconciliation/backfill)."""

    def __init__(
        self,
        reader: StoreDatabaseReader,
        *,
        source_system: str,
        source_store_id: str,
        prices_in_toman: bool = True,
        webhook_secret: str | None = None,
    ) -> None:
        super().__init__(
            source_system=source_system,
            source_store_id=source_store_id,
            prices_in_toman=prices_in_toman,
            webhook_secret=webhook_secret,
        )
        self._reader = reader

    @property
    def query_version(self) -> str:
        """Versioned query set the underlying reader executes."""
        return self._reader.query_version

    def close(self) -> None:
        """Dispose the reader's engine."""
        self._reader.close()

    def fetch_changes(
        self,
        cursor: SyncCursor | None,
        *,
        updated_since: datetime.datetime | None = None,
        page_size: int = DEFAULT_SYNC_PAGE_SIZE,
    ) -> SyncBatch:
        """Read one page of changes from the store database."""
        page_size = validate_page_size(page_size)
        page = self._reader.fetch_changes(
            store_id=self._source_store_id,
            updated_since=updated_since,
            cursor=cursor,
            page_size=page_size,
        )
        events: list[SyncedEvent] = []
        unmapped: list[StoreChange] = []
        for change in page.changes:
            mapped = map_store_change(change)
            if mapped:
                events.extend(mapped)
            else:
                unmapped.append(change)
        next_cursor: SyncCursor | None = None
        if page.has_more and page.changes:
            last = page.changes[-1]
            next_cursor = encode_sync_cursor(last.updated_at, last.event_id)
        return SyncBatch(
            events=events,
            unmapped_changes=unmapped,
            next_cursor=next_cursor,
            has_more=page.has_more,
        )
