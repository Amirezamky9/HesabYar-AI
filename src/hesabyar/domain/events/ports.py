"""Ports for the canonical event platform (spec §10, US-501).

The application layer depends on these ports — never on SQLAlchemy,
the Tryton adapter, Procrastinate, or any concrete connector.

Seams for the parallel Phase 5 streams (each port's production
implementation lands with its owner; F1 ships a fail-closed default
where one is needed to run the pipeline at all):

- :class:`MappingResolverPort` / :class:`DraftBuilderPort` — F3
  (mapping center). F1 default: fail closed to the review queue.
- :class:`PolicyGatePort` — F3 (three-layer gate). F1 default:
  draft-only, auto-post never allowed.
- :class:`ReviewQueuePort` — F4 (unified review queue). F1 default:
  inbox-backed (the ``needs_review`` inbox row + audit log).
- :class:`WebhookSecretPort` — F3 (store registry). F1 default: no
  secret configured (signature verification skipped, recorded).
- :class:`CommerceConnectorPort` — the §10.1 connector contract; the
  generic store connector implementations land in phase 5ب (US-512):
  the HTTP changes API and the read-only store-DB read.
- :class:`StoreDatabaseReaderPort` — the read-only store-DB read
  (ADR 0003 decision 3); read-only by construction, zero writes.

The port is synchronous, like the rest of the repository: there is
no async infrastructure yet (see ``AccountingEnginePort``).
"""

from __future__ import annotations

import datetime
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from hesabyar.domain.accounting.commands import (
    CancelInvoice,
    CreateCreditNote,
    CreateInvoiceDraft,
    CreateJournalDraft,
    CreatePayment,
    CreateSale,
)
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.domain.events.envelope import CanonicalEvent, CanonicalEventType
from hesabyar.domain.events.manual_entries import ManualEntry, ManualEntryType
from hesabyar.domain.events.models import (
    AutomationDecision,
    ExternalIdMapping,
    IdempotencyRecord,
    InboxEvent,
    InboxEventStatus,
    InboxStoreOutcome,
    IncomingEvent,
    IntegrationAttempt,
    NormalizedEvent,
    OutboxEvent,
    PolicyDecision,
    ProcessingResult,
    ResolvedMapping,
    ReviewItemDraft,
    StoreSettings,
    VerifiedEvent,
)
from hesabyar.domain.events.sync import (
    DEFAULT_SYNC_PAGE_SIZE,
    StoreChangePage,
    SyncBatch,
    SyncCursor,
)


class CommerceConnectorPort(Protocol):
    """Connector interface (spec §10.1).

    One implementation per commerce platform/storefront. Each adapter
    maps its native order/payment/refund/settlement structures into
    the canonical event contract; no store-specific field mapping
    lives in the accounting domain.
    """

    def verify_event(self, request: IncomingEvent) -> VerifiedEvent:
        """Verify an incoming event (signature + envelope)."""
        ...

    def normalize_event(self, event: VerifiedEvent) -> NormalizedEvent:
        """Normalize a verified event (currency conversion + validation)."""
        ...

    def fetch_changes(
        self,
        cursor: SyncCursor | None,
        *,
        updated_since: datetime.datetime | None = None,
        page_size: int = DEFAULT_SYNC_PAGE_SIZE,
    ) -> SyncBatch:
        """Poll the connector for changes (reconciliation/backfill).

        Lists orders/payments/cheques changed at or after
        ``updated_since`` (None = the connector's recent window),
        starting after ``cursor`` (None = first page), at most
        ``page_size`` rows (1..500). Rows are stably sorted on
        ``updated_at`` — ascending, with a stable tiebreaker — and
        include cancelled/deleted rows with their status flag, never
        physically removed. The cursor is opaque to the caller: pass
        the previous page's ``next_cursor`` back unchanged.

        Each returned event carries the store's own event id as its
        ``source_event_id``, so a change already received by webhook
        ingests as a duplicate (same idempotency key, same payload
        hash) instead of a second accounting effect.
        """
        ...

    def acknowledge(self, event: CanonicalEvent, result: ProcessingResult) -> None:
        """Acknowledge a processed event back to the source system."""
        ...


class StoreDatabaseReaderPort(Protocol):
    """Read-only store-database access for reconciliation/backfill only.

    ADR 0003 decision 3 explicitly allows a DIRECT read of the
    store's own database — read-only, reconciliation/backfill only,
    read-only DB user, documented versioned queries, zero writes.
    (The spec's SQL ban covers Tryton's database only; the store's
    database is the store's own data, read with the store team's
    read-only credentials.)

    Read-only BY CONSTRUCTION: this port exposes no write method at
    all — only :meth:`fetch_changes`. Implementations additionally
    open the connection in read-only mode and execute ONLY queries
    from the versioned registry (see
    ``hesabyar.infrastructure.store_connectors.queries``); the
    zero-write property is proven in tests (a write attempt through
    the reader's connection fails).
    """

    @property
    def query_version(self) -> str:
        """Versioned query set this reader executes (e.g. ``"v1"``)."""
        ...

    def fetch_changes(
        self,
        *,
        store_id: str,
        updated_since: datetime.datetime | None,
        cursor: SyncCursor | None,
        page_size: int,
    ) -> StoreChangePage:
        """Read one page of change rows for one store, stably sorted on ``updated_at``.

        ``store_id`` scopes the read to one storefront: rows of other
        stores in the same database are never returned. ``cursor`` is
        a keyset position from a previous page (None = first page);
        ``updated_since`` bounds the window from below (None = the
        reader's recent window). Returns at most ``page_size`` rows
        plus whether more rows follow.
        """
        ...


class EventInboxRepositoryPort(Protocol):
    """Persistence for inbox events (spec §10.3/§10.4).

    Implementations flush but never commit: the caller (REST route,
    worker pass) owns the transaction, like every other service that
    writes through a request session.
    """

    def store(
        self,
        event: CanonicalEvent,
        event_type: CanonicalEventType,
        tenant_id: str | None,
        installation_id: str | None = None,
    ) -> InboxStoreOutcome:
        """Store a raw event + inbox status in one transaction (§10.3 step 2).

        ``event_type`` is the validated canonical type, carried
        alongside the 15-field envelope — the envelope itself has
        no type field (§10.2). It is stored in the
        ``inbox_events.event_type`` column (migration 0004).

        ``installation_id`` is the logical installation that
        ingested the event (migration 0012, US-511): the pipeline
        resolves installation-scoped mappings against it. None for
        events ingested before the column existed.

        Serializes concurrent duplicates of the same source event
        (advisory lock + the §10.4 unique constraint): ``created``
        for a new event, ``duplicate`` for a re-send with the same
        payload hash, ``conflict`` for a re-used source event id
        with a different hash.
        """
        ...

    def get(self, event_id: str, tenant_id: str | None = None) -> InboxEvent | None:
        """Return one inbox event, or None when it does not exist (or is of another tenant)."""
        ...

    def get_by_source_event(
        self, source_system: str, source_store_id: str, source_event_id: str
    ) -> InboxEvent | None:
        """Return an inbox event by its §10.4 source-event identity, or None."""
        ...

    def list_events(
        self,
        tenant_id: str | None = None,
        *,
        status: InboxEventStatus | None = None,
        source_system: str | None = None,
        event_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[InboxEvent]:
        """List inbox events, newest first, optionally filtered."""
        ...

    def count_events(
        self,
        tenant_id: str | None = None,
        *,
        status: InboxEventStatus | None = None,
        source_system: str | None = None,
        event_type: str | None = None,
    ) -> int:
        """Count inbox events matching the same filters as :meth:`list_events`."""
        ...

    def claim_due(self, limit: int) -> Sequence[InboxEvent]:
        """Claim due inbox events for processing, oldest first.

        Claims rows in ``received`` status whose retry time has come
        (``next_attempt_at`` is null or past) and rows stuck in
        ``processing`` past the claim timeout, using
        ``SELECT ... FOR UPDATE SKIP LOCKED`` so concurrent workers
        never process the same event. Claimed rows are marked
        ``processing`` with an incremented attempt count.
        """
        ...

    def claim_one(self, event_id: str) -> InboxEvent | None:
        """Claim one inbox event for processing (``FOR UPDATE SKIP LOCKED``).

        Returns None when the event does not exist, is not due, is
        already in a terminal/review state, or is locked by another
        worker — the caller skips it and the sweep is the backstop.
        A claimed row is marked ``processing`` with an incremented
        attempt count.
        """
        ...

    def save(self, event: InboxEvent) -> InboxEvent:
        """Insert or update an inbox event (by id); return the persisted event."""
        ...

    def list_stale(self, older_than: datetime.timedelta) -> Sequence[InboxEvent]:
        """Return inbox events stuck in ``received``/``processing`` longer than the threshold."""
        ...

    def count_stale(self, older_than: datetime.timedelta) -> int:
        """Count inbox events stuck longer than the threshold."""
        ...


class EventOutboxRepositoryPort(Protocol):
    """Persistence for transactional-outbox result events (spec §10.3 step 9)."""

    def save(self, event: OutboxEvent) -> OutboxEvent:
        """Insert or update an outbox event (by id); return the persisted event."""
        ...

    def get(self, event_id: str) -> OutboxEvent | None:
        """Return one outbox event, or None when it does not exist."""
        ...

    def list_events(
        self,
        tenant_id: str | None = None,
        *,
        status: str | None = None,
        event_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[OutboxEvent]:
        """List outbox events, newest first, optionally filtered."""
        ...

    def claim_pending(self, limit: int) -> Sequence[OutboxEvent]:
        """Claim pending outbox events for publication (``FOR UPDATE SKIP LOCKED``)."""
        ...

    def save_integration_attempt(self, attempt: IntegrationAttempt) -> IntegrationAttempt:
        """Append an integration attempt; return the persisted attempt."""
        ...


class EventIdempotencyPort(Protocol):
    """Idempotency-key store (§10.4, second constraint).

    One row per ``(installation_id, operation, idempotency_key)``.
    A retry with the same key and the same request hash replays the
    stored response; a retry with a different hash is a conflict.
    """

    def find(self, installation_id: str, operation: str, key: str) -> IdempotencyRecord | None:
        """Return the record for a key, or None when the key is unused."""
        ...

    def claim(
        self, installation_id: str, operation: str, key: str, request_hash: str
    ) -> IdempotencyRecord | None:
        """Insert a pending record claiming the key; None when already held."""
        ...

    def complete(
        self,
        record: IdempotencyRecord,
        *,
        response_status: int,
        response_body: Mapping[str, Any],
        response_reference: str | None = None,
    ) -> None:
        """Store a completed response on a claimed record for replay."""
        ...

    def release(self, record: IdempotencyRecord) -> None:
        """Delete a pending record after a failed operation (its key frees up)."""
        ...

    def purge_expired(self, now: datetime.datetime) -> int:
        """Delete expired records; return how many were purged."""
        ...


class ExternalIdMapPort(Protocol):
    """Source-entity → engine-record mapping (§10.4, third constraint)."""

    def get(
        self, source_system: str, external_entity_type: str, external_entity_id: str
    ) -> ExternalIdMapping | None:
        """Return the engine record an external entity maps to, or None."""
        ...

    def save(self, mapping: ExternalIdMapping) -> ExternalIdMapping:
        """Insert a mapping; on conflict return the existing row (never a duplicate)."""
        ...


class DecisionAuditLogPort(Protocol):
    """Automation-decision audit log (§6.7).

    Append-only: decisions are never edited or deleted, so an auditor
    can always reconstruct what the platform decided and why.
    """

    def record(self, decision: AutomationDecision) -> AutomationDecision:
        """Append a decision; return the persisted decision."""
        ...

    def list_decisions(
        self,
        tenant_id: str | None = None,
        *,
        inbox_event_id: str | None = None,
        decision: str | None = None,
        event_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[AutomationDecision]:
        """List decisions, newest first, optionally filtered."""
        ...


class StoreSettingsPort(Protocol):
    """Per-store connector settings (ADR 0003 decision 4)."""

    def get(self, source_system: str, source_store_id: str) -> StoreSettings | None:
        """Return a store's settings, or None when the store has none stored."""
        ...

    def save(self, settings: StoreSettings) -> StoreSettings:
        """Insert or update a store's settings (by source identity)."""
        ...

    def list_settings(self) -> Sequence[StoreSettings]:
        """Return every stored store's settings, ordered by source identity."""
        ...


class MappingResolverPort(Protocol):
    """Resolve an event's versioned mappings (§10.5).

    The mapping center (F3) owns the production implementation. A
    mapping that cannot be resolved raises
    :class:`MappingUnresolvedError` — the pipeline fails closed to
    the review queue, never to a default account.
    """

    def resolve(self, event: NormalizedEvent) -> ResolvedMapping:
        """Resolve company/accounts/journal/party for a normalized event."""
        ...


#: What a draft builder may produce for one normalized event
#: (US-511 generalizes the F1 journal-only builder now that the F2
#: engine port has sale/invoice/payment operations). The pipeline
#: creates, validates and (when the gate allows) posts each kind
#: through the matching engine operation; a ``CancelInvoice`` is a
#: transition on an existing invoice, not a new document, and has no
#: post step. The store builder (US-511) builds invoices, credit
#: notes, payments and cancellations; sales join when the Path A
#: migration lands (phased design §2 §1.6).
EngineDocumentCommand = (
    CreateJournalDraft
    | CreateSale
    | CreateInvoiceDraft
    | CreateCreditNote
    | CreatePayment
    | CancelInvoice
)


class DraftBuilderPort(Protocol):
    """Build an engine document command from a normalized event + mappings.

    The command is one of the :data:`EngineDocumentCommand` kinds:
    the generic path builds a journal draft; the store builder
    (US-511) builds a customer invoice for a completed order, a
    credit note for a refund / post-invoice cancellation, a payment
    for a captured/refunded payment, or an invoice cancellation.
    """

    def build(self, event: NormalizedEvent, mapping: ResolvedMapping) -> EngineDocumentCommand:
        """Build the document command for a normalized event."""
        ...


class PolicyGatePort(Protocol):
    """Three-layer policy gate (§6.2): event type + amount cap + period lock.

    The registry-backed gate lands with F3; the F1 default is
    draft-only (auto-post never allowed), which is the safe
    pre-configuration state.
    """

    def evaluate(
        self,
        event: NormalizedEvent,
        mapping: ResolvedMapping,
        draft_total: MoneyIRR,
    ) -> PolicyDecision:
        """Decide whether the event's draft may auto-post."""
        ...


class ReviewQueuePort(Protocol):
    """Enqueue a review item for a human (F4 seam).

    Returns the review item's id. The F1 inbox-backed adapter
    records the routing in the decision audit log and returns the
    inbox event id; the F4 adapter persists a ``ReviewItem`` row.
    """

    def enqueue(self, item: ReviewItemDraft) -> str:
        """Enqueue one review item; return its id."""
        ...


class JobQueuePort(Protocol):
    """Scheduler/trigger queue for the event worker (Procrastinate).

    The outbox stays in HesabYar's own tables — the job queue only
    triggers processing; it never replaces the outbox (phased design
    §6.1: "outbox را نمی‌خریم، می‌سازیم؛ scheduler را می‌خریم").
    """

    def defer_process_event(self, event_id: str) -> None:
        """Schedule one inbox event for processing (at-least-once trigger)."""
        ...

    def defer_sweep(self) -> None:
        """Schedule a worker sweep (due inbox events + pending outbox events)."""
        ...


class OutboxPublisherPort(Protocol):
    """Publish one outbox event to its consumers.

    Returns True when the event was delivered (or had no consumers);
    False when delivery failed and the event must stay pending.
    """

    def publish(self, event: OutboxEvent) -> bool:
        """Publish one outbox event; True when delivered."""
        ...


class WebhookSecretPort(Protocol):
    """Look up a store's webhook secret for HMAC verification (§1.3).

    The store registry (F3) owns secret storage (encrypted in the
    database); the registry-backed implementation (US-511) decrypts
    through the credential vault. No secret configured → None →
    signature verification is skipped and the skip is recorded (never
    silently assumed). ``installation_id`` scopes the lookup: store
    keys are unique per installation, so an unscoped lookup could
    return another installation's secret. A store WITH a secret whose
    ciphertext cannot be decrypted fails closed (the implementation
    raises) instead of returning None.
    """

    def get_secret(
        self,
        source_system: str,
        source_store_id: str,
        installation_id: str | None = None,
    ) -> str | None:
        """Return the store's webhook secret, or None when none is configured."""
        ...


class ManualEntryRepositoryPort(Protocol):
    """Persist manual entries (US-513, stream S3).

    The manual-entry row is the traceability anchor: it links the
    operator's entry (who, when, what) to every canonical event the
    entry produced. Repositories flush but do NOT commit — the caller
    (REST route) owns the transaction, like every other service that
    writes through a request session.
    """

    def save(self, entry: ManualEntry) -> ManualEntry:
        """Insert one manual entry (flushes; the caller commits)."""
        ...

    def get(self, entry_id: str, tenant_id: str | None = None) -> ManualEntry | None:
        """Return one entry, or None when it does not exist in the tenant."""
        ...

    def list_entries(
        self,
        tenant_id: str | None = None,
        *,
        entry_type: ManualEntryType | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[ManualEntry]:
        """List entries, newest first, optionally filtered by type."""
        ...
