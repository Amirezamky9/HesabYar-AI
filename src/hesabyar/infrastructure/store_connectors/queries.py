"""Versioned read-only queries for the store database (US-512, S2).

ADR 0003 decision 3 allows a DIRECT read of the store's own
database — read-only, reconciliation/backfill only, read-only DB
user, zero writes, documented versioned queries. This module is the
"documented versioned queries" part: every query the read-only
reader may execute lives here, under a version key, and the reader
refuses to execute anything else.

## The v1 contract (what the store team provides)

The store database exposes ONE view, ``hesabyar_changes`` — a union
of the store's orders, payments and registered cheques, one row per
change:

| Column | Type | Meaning |
|---|---|---|
| ``store_id`` | text | Storefront id within the store database |
| ``entity_type`` | text | ``order`` / ``payment`` / ``cheque`` |
| ``entity_id`` | text | The order/payment/cheque id in the store |
| ``event_id`` | text | The store's event id (= webhook ``source_event_id``); the keyset tiebreaker |
| ``event_name`` | text | Store-native webhook name (``order.completed``, ``refund.created``, ...) |
| ``status`` | text | Entity status flag (cancelled/deleted rows stay in the feed) |
| ``updated_at`` | timestamptz | Last change; the stable-sort key |
| ``payload`` | jsonb / text | The event payload as a JSON object |

The store team owns the view definition (their schema is theirs —
for the custom-coded Hayat store the view is a thin select over
their tables); HesabYar owns this query. A schema change on the
store side that breaks the view is a NEW query version here, never
an edit of v1: old versions stay readable so an in-flight backfill
never breaks mid-run.

## Query rules (enforced by the reader)

- SELECT only, single statement — the reader rejects anything else
  before it reaches the database.
- Parameterized, never string-interpolated — ``:store_id``,
  ``:updated_since``, ``:cursor_updated_at``, ``:cursor_id``,
  ``:limit`` are bound parameters.
- Keyset pagination on ``(updated_at, event_id)`` ascending — stable
  across pages even when many rows share a timestamp. The event id
  is the tiebreaker: it is unique per event (the same uniqueness
  the §10.4 source-event identity relies on), so no separate row-id
  column is needed.
- ``store_id`` always filters — one store's rows never leak into
  another store's sync.
"""

from __future__ import annotations

from collections.abc import Mapping

from hesabyar.domain.events.errors import StoreDatabaseError

#: Current query version. New versions are added, never edited.
DEFAULT_STORE_DB_QUERY_VERSION = "v1"

#: Versioned queries: {version: {name: sql}}. One query per version
#: today (the unified ``hesabyar_changes`` view lists all three
#: entity kinds); the per-name slot exists so a future version can
#: split per-entity queries without changing the reader's shape.
STORE_DB_QUERIES: Mapping[str, Mapping[str, str]] = {
    "v1": {
        "changes": """
            SELECT store_id, entity_type, entity_id, event_id,
                   event_name, status, updated_at, payload
            FROM hesabyar_changes
            WHERE store_id = :store_id
              AND (:updated_since IS NULL OR updated_at >= :updated_since)
              AND (:cursor_updated_at IS NULL
                   OR updated_at > :cursor_updated_at
                   OR (updated_at = :cursor_updated_at AND event_id > :cursor_event_id))
            ORDER BY updated_at ASC, event_id ASC
            LIMIT :limit
        """,
    },
}


def get_store_db_query(version: str, name: str = "changes") -> str:
    """Return one versioned query, or raise for an unknown version/name.

    Unknown versions fail closed — the reader never falls back to a
    guessed query.
    """
    versions = STORE_DB_QUERIES.get(version)
    if versions is None:
        raise StoreDatabaseError(
            f"Unknown store-DB query version {version!r}; known versions: "
            f"{', '.join(sorted(STORE_DB_QUERIES))}.",
            details={"query_version": version},
        )
    query = versions.get(name)
    if query is None:
        raise StoreDatabaseError(
            f"Unknown store-DB query {name!r} in version {version!r}.",
            details={"query_version": version, "query_name": name},
        )
    return query
