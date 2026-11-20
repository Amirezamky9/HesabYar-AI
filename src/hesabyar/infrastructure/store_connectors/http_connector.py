"""Generic HTTP store connector (US-512, S2): poll the store's changes API.

The primary reconciliation transport. HesabYar polls the store's
read-only changes endpoint with the store's scoped read-only API key
(``Authorization: Bearer`` — the key stored encrypted in the store
registry) and maps each change row to canonical events through the
shared domain mapper, so a missed webhook is filled with the same
idempotency key.

## The generic changes-API contract (see ``docs/store-connector.md``)

``GET {api_base_url}/changes?updated_since=<ISO>&cursor=<opaque>&page_size=<int>``

Response (JSON)::

    {
      "changes": [
        {
          "event_id": "evt-123",
          "event_name": "order.completed",
          "entity_type": "order",
          "entity_id": "order-42",
          "updated_at": "2026-11-18T09:00:00Z",
          "status": "completed",
          "payload": {"total_amount": 175000, "...": "..."}
        }
      ],
      "next_cursor": "...",
      "has_more": false
    }

Contract the store upholds: rows stably sorted on ``updated_at``
ascending (with a stable internal tiebreaker); cancelled/deleted
rows stay in the feed with their status flag; ``event_id`` is the
SAME id the store sends as ``source_event_id`` on its webhooks; the
cursor is the store's own opaque value, passed through unchanged.
The connector VALIDATES the sort (non-decreasing ``updated_at``)
and rejects an out-of-order page instead of ingesting it.
"""

from __future__ import annotations

import datetime
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from hesabyar.domain.events.errors import (
    StoreSyncFailedError,
    StoreUnreachableError,
)
from hesabyar.domain.events.sync import (
    DEFAULT_SYNC_PAGE_SIZE,
    StoreChange,
    StoreEntityKind,
    SyncBatch,
    SyncCursor,
    SyncedEvent,
    map_store_change,
    validate_page_size,
)
from hesabyar.infrastructure.store_connectors.common import BaseStoreConnector

#: Path of the changes endpoint under the store's API base URL.
CHANGES_PATH = "/changes"

#: Default HTTP timeout for a polling call (mirrors the Tryton client).
DEFAULT_POLL_TIMEOUT_SECONDS = 30.0


class _ChangeRow(BaseModel):
    """One change row as the store's changes API returns it."""

    model_config = ConfigDict(extra="ignore")

    event_id: str = Field(min_length=1)
    event_name: str = Field(min_length=1)
    entity_type: StoreEntityKind
    entity_id: str = Field(min_length=1)
    updated_at: datetime.datetime
    status: str = Field(min_length=1)
    payload: dict[str, Any]


class _ChangesPage(BaseModel):
    """One page of the store's changes API."""

    model_config = ConfigDict(extra="ignore")

    changes: list[_ChangeRow] = Field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False


class GenericHttpStoreConnector(BaseStoreConnector):
    """Poll a custom-coded store's changes API (the generic contract)."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        source_system: str,
        source_store_id: str,
        prices_in_toman: bool = True,
        webhook_secret: str | None = None,
        timeout: float = DEFAULT_POLL_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        super().__init__(
            source_system=source_system,
            source_store_id=source_store_id,
            prices_in_toman=prices_in_toman,
            webhook_secret=webhook_secret,
        )
        parsed = urlparse(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise StoreSyncFailedError(
                f"Store API base URL {base_url!r} is not an http(s) URL.",
                details={"base_url": base_url},
            )
        self._base_url = base_url.rstrip("/")
        # trust_env=False: the store API is a configured endpoint.
        # Environment proxies must never intercept polling traffic
        # (the Bearer <redacted> must not leak to a proxy), and malformed proxy env
        # vars must not break polling (the Tryton-client lesson).
        self._client = httpx.Client(
            base_url=self._base_url,
            timeout=timeout,
            transport=transport,
            trust_env=False,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
        )

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    def fetch_changes(
        self,
        cursor: SyncCursor | None,
        *,
        updated_since: datetime.datetime | None = None,
        page_size: int = DEFAULT_SYNC_PAGE_SIZE,
    ) -> SyncBatch:
        """Poll the store's changes API for one page."""
        page_size = validate_page_size(page_size)
        params: dict[str, str] = {"page_size": str(page_size)}
        if updated_since is not None:
            params["updated_since"] = updated_since.astimezone(datetime.UTC).isoformat()
        if cursor is not None and cursor.value:
            # The cursor is the store's own opaque value — passed
            # through unchanged; HesabYar never interprets it.
            params["cursor"] = cursor.value
        try:
            response = self._client.get(CHANGES_PATH, params=params)
        except httpx.TimeoutException as exc:
            raise StoreUnreachableError(
                f"Polling the store's changes API timed out ({self._base_url}).",
                details={"base_url": self._base_url},
            ) from exc
        except httpx.TransportError as exc:
            raise StoreUnreachableError(
                f"Could not reach the store's changes API ({self._base_url}).",
                details={"base_url": self._base_url},
            ) from exc
        if response.status_code >= 500:
            raise StoreUnreachableError(
                f"The store's changes API failed with HTTP {response.status_code}.",
                details={"base_url": self._base_url, "status_code": response.status_code},
            )
        if response.status_code != 200:
            raise StoreSyncFailedError(
                f"The store's changes API rejected the poll with HTTP {response.status_code}.",
                details={"base_url": self._base_url, "status_code": response.status_code},
            )
        try:
            page = _ChangesPage.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise StoreSyncFailedError(
                "The store's changes API returned a page that does not match the "
                "generic changes contract.",
                details={"base_url": self._base_url, "error": str(exc)},
            ) from exc
        self._assert_sorted(page.changes)
        events: list[SyncedEvent] = []
        unmapped: list[StoreChange] = []
        for row in page.changes:
            change = StoreChange(
                source_system=self._source_system,
                source_store_id=self._source_store_id,
                entity_type=row.entity_type,
                entity_id=row.entity_id,
                event_id=row.event_id,
                event_name=row.event_name,
                updated_at=row.updated_at,
                status=row.status,
                payload=row.payload,
            )
            mapped = map_store_change(change)
            if mapped:
                events.extend(mapped)
            else:
                unmapped.append(change)
        return SyncBatch(
            events=events,
            unmapped_changes=unmapped,
            next_cursor=SyncCursor(value=page.next_cursor) if page.next_cursor else None,
            has_more=page.has_more,
        )

    @staticmethod
    def _assert_sorted(changes: list[_ChangeRow]) -> None:
        """Reject a page whose rows violate the stable-sort contract.

        Only the checkable part is enforced: ``updated_at``
        non-decreasing. Rows sharing a timestamp may arrive in any
        order — the store's cursor encodes its internal tiebreaker.
        """
        previous: datetime.datetime | None = None
        for row in changes:
            updated_at = row.updated_at
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=datetime.UTC)
            if previous is not None and updated_at < previous:
                raise StoreSyncFailedError(
                    "The store's changes API returned rows out of order: "
                    "pages must be stably sorted on updated_at ascending.",
                    details={"event_id": row.event_id},
                )
            previous = updated_at
