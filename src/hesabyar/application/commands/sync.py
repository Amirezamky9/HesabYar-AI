"""Store reconciliation & backfill use case (US-512, S2 — spec §10.1).

Polls one store's changes through its :class:`CommerceConnectorPort`
(HTTP changes API or read-only store-DB read) and ingests every
mapped event through the SAME :class:`EventIngestService` path the
webhooks use, so a missed webhook is filled with the same
idempotency key and has no duplicate accounting effect.

Run semantics:

- Pages loop until the connector reports no more pages or
  ``max_pages`` is reached; the report carries ``next_cursor`` +
  ``has_more`` so the operator (or a scheduler) resumes the run.
- A connector failure AFTER at least one page was fetched ends the
  run as ``partial``: the fetched pages stay ingested (the caller
  commits), the error is recorded on the report, and ``next_cursor``
  points at the failed page so a retry resumes there. A failure on
  the FIRST page propagates (nothing was fetched; the caller gets
  the typed error, e.g. 502 ``STORE_UNREACHABLE``).
- Per-event failures are recorded, not fatal: a conflicting
  re-send (same key, different payload) or a malformed polled row is
  listed in ``errors`` and the run continues with the next event.
  An unexpected per-event failure propagates — an unknown failure
  must not be retried blindly inside the run; the operator retries
  the run from its cursor and the §10.4 constraints dedup the
  already-ingested events.
- Multi-store isolation is enforced, not assumed: every event a
  connector returns must belong to the store being synced
  (``source_system``/``source_store_id`` match); a foreign event
  aborts the run immediately.
- Unmapped changes (cheques today — their canonical event type
  needs ADR approval) are counted in the report, never ingested
  and never silently dropped.

The service depends only on ports (the ingest service + the
connector); it never sees HTTP, SQLAlchemy, or the store's database.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Literal

from hesabyar.application.commands.events import EventIngestService
from hesabyar.domain.events.errors import (
    IdempotencyConflictError,
    InvalidEventEnvelopeError,
    InvalidSyncRequestError,
    StoreSyncFailedError,
)
from hesabyar.domain.events.ports import CommerceConnectorPort
from hesabyar.domain.events.sync import (
    DEFAULT_SYNC_LOOKBACK_DAYS,
    DEFAULT_SYNC_PAGE_SIZE,
    SyncCursor,
    validate_page_size,
)

#: Max pages fetched in one sync run (bounds a single request's work;
#: the caller resumes with ``next_cursor`` when more pages remain).
DEFAULT_MAX_SYNC_PAGES = 10

#: Hard cap on pages per run.
MAX_SYNC_PAGES = 50

#: Max per-event errors kept on a report (the run may hit more; the
#: count is exact, the list is a sample for the operator).
MAX_SYNC_ERROR_ITEMS = 20


@dataclass(frozen=True)
class SyncItemError:
    """One polled event that could not be ingested (recorded, run continues)."""

    source_event_id: str
    code: str
    message: str


@dataclass(frozen=True)
class SyncReport:
    """Outcome of one sync run over one store."""

    source_system: str
    source_store_id: str
    connector_kind: str
    status: Literal["completed", "partial"]
    pages_fetched: int
    events_created: int
    events_duplicate: int
    changes_unmapped: int
    events_by_type: dict[str, int] = field(default_factory=dict)
    errors: tuple[SyncItemError, ...] = ()
    error_count: int = 0
    error: str | None = None
    next_cursor: str | None = None
    has_more: bool = False
    started_at: datetime.datetime | None = None
    finished_at: datetime.datetime | None = None


class StoreSyncService:
    """Poll one store's changes and ingest them through the event pipeline."""

    def __init__(self, ingest: EventIngestService) -> None:
        self._ingest = ingest

    def sync(
        self,
        connector: CommerceConnectorPort,
        *,
        source_system: str,
        source_store_id: str,
        connector_kind: str,
        tenant_id: str | None,
        installation_id: str,
        updated_since: datetime.datetime | None = None,
        cursor: SyncCursor | None = None,
        page_size: int = DEFAULT_SYNC_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_SYNC_PAGES,
    ) -> SyncReport:
        """Run one sync (or backfill) over a store; flushes, the caller commits.

        ``updated_since`` bounds the window from below; when neither
        it nor ``cursor`` is given, the run looks back
        ``DEFAULT_SYNC_LOOKBACK_DAYS`` days. ``max_pages`` bounds the
        run (1..``MAX_SYNC_PAGES``); a run that hits the bound
        reports ``has_more=True`` with ``next_cursor`` for resuming.
        """
        page_size = validate_page_size(page_size)
        if isinstance(max_pages, bool) or not isinstance(max_pages, int):
            raise InvalidSyncRequestError(
                "max_pages must be an integer.",
                details={"max_pages": str(max_pages)},
            )
        if max_pages < 1 or max_pages > MAX_SYNC_PAGES:
            raise InvalidSyncRequestError(
                f"max_pages must be between 1 and {MAX_SYNC_PAGES}, got {max_pages}.",
                details={"max_pages": max_pages},
            )
        started_at = datetime.datetime.now(datetime.UTC)
        if updated_since is None and (cursor is None or cursor.value is None):
            updated_since = started_at - datetime.timedelta(days=DEFAULT_SYNC_LOOKBACK_DAYS)
        if updated_since is not None and updated_since.tzinfo is None:
            updated_since = updated_since.replace(tzinfo=datetime.UTC)

        verification_note = (
            f"Polled from the store ({connector_kind}); authenticated server-to-server, "
            "no webhook HMAC applies."
        )
        pages_fetched = 0
        events_created = 0
        events_duplicate = 0
        changes_unmapped = 0
        events_by_type: dict[str, int] = {}
        errors: list[SyncItemError] = []
        error_count = 0
        run_error: str | None = None
        current_cursor = cursor

        for _ in range(max_pages):
            try:
                batch = connector.fetch_changes(
                    current_cursor,
                    updated_since=updated_since,
                    page_size=page_size,
                )
            except Exception as exc:
                # A failure after ≥1 fetched page ends the run as
                # partial (the fetched pages stay ingested; the
                # report's cursor resumes at the failed page). A
                # failure on the first page propagates — nothing was
                # fetched, so the caller gets the typed error.
                if pages_fetched == 0:
                    raise
                run_error = str(exc)
                break
            pages_fetched += 1
            if batch.has_more and (batch.next_cursor is None or batch.next_cursor.value is None):
                # A connector that reports more pages but gives no
                # cursor to resume with violates the polling
                # contract — fail closed instead of silently
                # dropping the remaining pages.
                raise StoreSyncFailedError(
                    "The connector reported more pages but returned no next_cursor.",
                    details={"source_system": source_system, "source_store_id": source_store_id},
                )
            for synced in batch.events:
                event = synced.event
                if (
                    event.source_system != source_system
                    or event.source_store_id != source_store_id
                ):
                    # Cross-store contamination: a connector returning
                    # another store's events aborts the run — events
                    # must never mix between stores.
                    raise StoreSyncFailedError(
                        f"The connector returned an event for store "
                        f"{event.source_system}/{event.source_store_id} while syncing "
                        f"{source_system}/{source_store_id}.",
                        details={
                            "source_system": event.source_system,
                            "source_store_id": event.source_store_id,
                            "source_event_id": event.source_event_id,
                        },
                    )
                kind = synced.event_type.value
                events_by_type[kind] = events_by_type.get(kind, 0) + 1
                try:
                    result = self._ingest.ingest_verified(
                        event,
                        synced.event_type,
                        tenant_id=tenant_id,
                        installation_id=installation_id,
                        signature_verified=False,
                        verification_note=verification_note,
                    )
                except (IdempotencyConflictError, InvalidEventEnvelopeError) as exc:
                    error_count += 1
                    if len(errors) < MAX_SYNC_ERROR_ITEMS:
                        errors.append(
                            SyncItemError(
                                source_event_id=event.source_event_id,
                                code=exc.code,
                                message=exc.message,
                            )
                        )
                    continue
                if result.status == "created":
                    events_created += 1
                else:
                    events_duplicate += 1
            for change in batch.unmapped_changes:
                if (
                    change.source_system != source_system
                    or change.source_store_id != source_store_id
                ):
                    raise StoreSyncFailedError(
                        f"The connector returned a change for store "
                        f"{change.source_system}/{change.source_store_id} while syncing "
                        f"{source_system}/{source_store_id}.",
                        details={
                            "source_system": change.source_system,
                            "source_store_id": change.source_store_id,
                            "source_event_id": change.event_id,
                        },
                    )
            changes_unmapped += len(batch.unmapped_changes)
            current_cursor = batch.next_cursor
            if not batch.has_more:
                break

        has_more = run_error is not None or (
            pages_fetched == max_pages
            and current_cursor is not None
            and current_cursor.value is not None
        )
        # A run that stopped because the connector reported no more
        # pages has no resume cursor; a partial run resumes at the
        # failed page; a max-pages run resumes at the next page.
        resume_cursor = current_cursor if has_more else None
        return SyncReport(
            source_system=source_system,
            source_store_id=source_store_id,
            connector_kind=connector_kind,
            status="partial" if run_error is not None else "completed",
            pages_fetched=pages_fetched,
            events_created=events_created,
            events_duplicate=events_duplicate,
            changes_unmapped=changes_unmapped,
            events_by_type=events_by_type,
            errors=tuple(errors),
            error_count=error_count,
            error=run_error,
            next_cursor=resume_cursor.value if resume_cursor is not None else None,
            has_more=has_more,
            started_at=started_at,
            finished_at=datetime.datetime.now(datetime.UTC),
        )
