"""Event-platform command use cases + worker (spec §10.3, US-501).

- :class:`EventIngestService` — §10.3 steps 1–2: verify the envelope
  (and HMAC signature when a secret is configured) and persist the
  raw event + inbox status in one transaction, under the §10.4
  idempotency constraints. Same key + different hash → 409 Conflict,
  never silent.
- :func:`normalize_event` — §10.3 step 3: validate the payload and
  convert every money field to whole Rial at the connector boundary.
- :class:`EventPipelineService` — §10.3 steps 3–10: normalize → map
  → draft → validate → policy gate → post-or-draft → retry/DLQ,
  with every decision recorded in the automation audit log.
- :class:`EventWorker` — the database poller: claims due inbox
  events (``FOR UPDATE SKIP LOCKED``) and pending outbox events and
  runs them through the pipeline/publisher. Procrastinate only
  triggers the poller; the outbox stays in HesabYar's own tables.
- :class:`DlqReplayService` — manual DLQ replay from the UI with the
  same idempotency key (no duplicate effect).
- :class:`StoreSettingsService` — the per-store "prices are in
  Toman" setting.

Persistence: services persist through the repository ports
(:mod:`hesabyar.domain.events.ports`) and never see SQLAlchemy or
ORM rows (spec §6.3). Repositories flush but do NOT commit — the
caller (REST route, worker pass) owns the transaction, like every
other service that writes through a request session.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import uuid
from dataclasses import dataclass
from typing import Literal, Protocol

from hesabyar.application.errors import (
    ApplicationError,
    translate_engine_error,
    translate_port_errors,
)
from hesabyar.domain.accounting.commands import (
    CancelInvoice,
    CreateCreditNote,
    CreateInvoiceDraft,
    CreateJournalDraft,
    CreatePayment,
    CreateSale,
    PostJournal,
)
from hesabyar.domain.accounting.ports import AccountingEnginePort
from hesabyar.domain.accounting.refs import (
    InvoiceRef,
    JournalDraftRef,
    PaymentRef,
    SaleRef,
)
from hesabyar.domain.common.errors import HesabYarError
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.domain.events.envelope import (
    EVENT_INGEST_OPERATION,
    CanonicalEvent,
    CanonicalEventType,
    canonical_payload_hash,
    parse_event_type,
    verify_hmac_sha256,
)
from hesabyar.domain.events.errors import (
    EventNotDeadLetteredError,
    EventNotFoundError,
    EventSignatureError,
    IdempotencyConflictError,
    InvalidEventEnvelopeError,
    InvalidStoreAmountError,
    MappingUnresolvedError,
)
from hesabyar.domain.events.models import (
    AutomationDecision,
    AutomationDecisionKind,
    ExternalIdMapping,
    GateResult,
    InboxEvent,
    InboxEventStatus,
    IncomingEvent,
    IntegrationAttempt,
    IntegrationAttemptStatus,
    NormalizedEvent,
    OutboxEvent,
    OutboxEventStatus,
    PipelineOutcome,
    PipelineResult,
    PolicyDecision,
    ResolvedMapping,
    ReviewItemDraft,
    StoreSettings,
    VerifiedEvent,
)
from hesabyar.domain.events.normalization import normalize_payload
from hesabyar.domain.events.pipeline import (
    DEFAULT_STALE_AFTER,
    MAX_ATTEMPTS,
    next_retry_at,
)
from hesabyar.domain.events.ports import (
    DecisionAuditLogPort,
    DraftBuilderPort,
    EngineDocumentCommand,
    EventIdempotencyPort,
    EventInboxRepositoryPort,
    EventOutboxRepositoryPort,
    ExternalIdMapPort,
    JobQueuePort,
    MappingResolverPort,
    OutboxPublisherPort,
    PolicyGatePort,
    ReviewQueuePort,
    StoreSettingsPort,
    WebhookSecretPort,
)
from hesabyar.domain.events.store_webhooks import (
    ORDER_CANCELLATION_ENTITY_TYPE,
    derive_fanout_event,
    resolve_header_event_types,
    validate_store_payload,
)

#: Outbox event type for a processed inbox event (§10.3 step 9).
EVENT_PROCESSED_OUTBOX_TYPE = "events.processed"

#: Aggregate type for inbox events in the outbox.
INBOX_EVENT_AGGREGATE = "inbox_event"

#: Target engine key for the §10.4 external-entity mapping.
TARGET_ENGINE_TRYTON = "tryton"

#: Target entity type for a cheque intake in the §10.4 external-id
#: map: a registered site cheque maps to its ``account.check``
#: engine record (US-533).
TARGET_ENTITY_CHEQUE = "account.check"


@dataclass(frozen=True)
class ChequeIntakeOutcome:
    """Outcome of one site cheque event handled by the intake handler.

    ``engine_cheque_id`` is the engine ``account.check`` id the
    event converged on (None only when the existing cheque could
    not be re-loaded after a duplicate conflict — the review item
    is still filed and the event is still processed).
    ``duplicate_conflict`` is True when the event contradicted an
    existing live cheque: the conflict was already filed to the
    review queue by the handler, and the event is processed (not
    retried) with the existing cheque as its reference.
    """

    engine_cheque_id: int | None
    amount_irr: int
    detail: str
    duplicate_conflict: bool = False


class ChequeIntakeHandlerPort(Protocol):
    """The cheque intake handler the pipeline delegates site cheque events to.

    Intake creates NO engine document — it writes the HesabYar
    cheque registry (+ the engine cheque record) through
    :class:`ChequeCommandService
    <hesabyar.application.commands.cheques.ChequeCommandService>`,
    so it cannot go through the draft-builder path. The pipeline
    owns the envelope, idempotency, mapping resolution and audit;
    the handler owns the registry + payee check + convergence.
    """

    def register_site_cheque(
        self,
        *,
        event: NormalizedEvent,
        mapping: ResolvedMapping,
        tenant_id: str,
        installation_id: str,
    ) -> ChequeIntakeOutcome:
        """Handle ``cheque.received.registered``; return its outcome."""
        ...

    def cancel_site_cheque(
        self,
        *,
        event: NormalizedEvent,
        mapping: ResolvedMapping,
        tenant_id: str,
        installation_id: str,
    ) -> ChequeIntakeOutcome:
        """Handle ``cheque.received.cancelled``; return its outcome."""
        ...


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def is_retryable(error: BaseException) -> bool:
    """True when a failure is transient and worth retrying.

    Permanent (fail-closed to review, never retried): mapping
    failures, envelope/amount validation failures, and any
    application error that does not mark itself retryable.
    Transient (retried, then dead-lettered): engine unavailability
    (``retryable=True``) and unexpected exceptions — an unknown
    failure is treated as transient, with the DLQ as the backstop,
    rather than silently dropping the event.
    """
    if isinstance(error, (MappingUnresolvedError, InvalidEventEnvelopeError, InvalidStoreAmountError)):
        return False
    if isinstance(error, ApplicationError):
        return error.retryable
    if isinstance(error, HesabYarError):
        return False
    return True


# ---------------------------------------------------------------------------
# Normalization (§10.3 step 3)
# ---------------------------------------------------------------------------


def normalize_event(
    event: CanonicalEvent,
    settings: StoreSettings,
    *,
    event_type: CanonicalEventType,
    installation_id: str | None = None,
) -> NormalizedEvent:
    """Normalize a verified event: convert its money fields to whole Rial.

    ``event_type`` arrives alongside the envelope (it selects the
    money fields converted here) and is carried on the normalized
    event for the mapping/policy steps downstream. ``installation_id``
    (from the inbox row) scopes the mapping resolver's registry
    reads; None for rows ingested before migration 0012.

    The raw envelope is untouched — normalization produces a new
    payload with converted amounts plus the rounding evidence for
    every converted field, so the conversion is auditable and never
    silent (spec §19 rule 7). The conversion itself lives in the
    domain (:mod:`hesabyar.domain.events.normalization`), shared
    with the store connectors' ``normalize_event``.
    """
    converted_payload, conversions = normalize_payload(
        event.payload,
        event_type,
        prices_in_toman=settings.prices_in_toman,
    )
    return NormalizedEvent(
        event=event,
        event_type=event_type,
        store_settings=settings,
        converted_payload=converted_payload,
        conversions=tuple(conversions),
        installation_id=installation_id,
    )


# ---------------------------------------------------------------------------
# Ingest (§10.3 steps 1–2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IngestResult:
    """Outcome of ingesting one event through the inbound API.

    ``derived_events`` holds the fan-out inbox events (US-511): a
    ``refund.created`` webhook ingests two canonical events, and the
    second (``payment.refunded``) is reported here alongside the
    primary (``sales.refund.created``) in ``event``.
    """

    status: Literal["created", "duplicate"]
    event: InboxEvent
    signature_verified: bool
    signature_note: str
    derived_events: tuple[InboxEvent, ...] = ()


class EventIngestService:
    """Verify + store raw events under the §10.4 constraints.

    Steps: parse + validate the envelope (§10.2) → validate the
    canonical event type carried alongside it (the
    ``X-HesabYar-Event-Type`` header) → check the connector
    matches the envelope's source system → verify the HMAC
    signature when the store has a secret configured → claim the
    idempotency key → store the inbox row. A duplicate re-send with
    the same hash replays the stored event (200); a re-used key or
    source event id with a different hash is a 409 Conflict.
    """

    def __init__(
        self,
        inbox: EventInboxRepositoryPort,
        idempotency: EventIdempotencyPort,
        secrets: WebhookSecretPort,
        job_queue: JobQueuePort,
    ) -> None:
        self._inbox = inbox
        self._idempotency = idempotency
        self._secrets = secrets
        self._job_queue = job_queue

    def verify(self, request: IncomingEvent, installation_id: str | None = None) -> VerifiedEvent:
        """Verify one incoming event: envelope + type + (when configured) HMAC.

        The event type travels alongside the envelope (the
        ``X-HesabYar-Event-Type`` header); the receiver assigns and
        validates it here, before anything is stored. The header may
        carry a canonical type (the US-501 contract) or a store
        webhook topic, which maps to one or more canonical types
        (``refund.created`` fans out to two) — see
        :func:`resolve_header_event_types`. The webhook secret lookup
        is scoped by ``installation_id``: store keys are unique per
        installation.
        """
        try:
            raw = json.loads(request.raw_body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise InvalidEventEnvelopeError(
                "The event body is not valid JSON.",
                details={"connector": request.connector},
            ) from exc
        if not isinstance(raw, dict):
            raise InvalidEventEnvelopeError(
                "The event body must be a JSON object.",
                details={"connector": request.connector},
            )
        event = CanonicalEvent.from_raw(raw)
        event_types = resolve_header_event_types(request.event_type)
        event_type = event_types[0]
        if event.source_system != request.connector:
            raise InvalidEventEnvelopeError(
                f"The envelope's source_system {event.source_system!r} does not "
                f"match the connector {request.connector!r}.",
                details={"source_system": event.source_system, "connector": request.connector},
            )
        secret = self._secrets.get_secret(
            event.source_system, event.source_store_id, installation_id
        )
        if secret is None:
            return VerifiedEvent(
                event=event,
                event_type=event_type,
                signature_verified=False,
                verification_note="No webhook secret is configured for this store; signature check skipped.",
                event_types=event_types,
            )
        if request.signature is None:
            raise EventSignatureError(
                "This store requires a signed webhook (missing signature header).",
                details={"source_system": event.source_system},
            )
        if not verify_hmac_sha256(secret, request.raw_body, request.signature):
            raise EventSignatureError(
                "The webhook signature does not match the body.",
                details={"source_system": event.source_system},
            )
        return VerifiedEvent(
            event=event,
            event_type=event_type,
            signature_verified=True,
            verification_note="HMAC-SHA256 signature verified.",
            event_types=event_types,
        )

    def ingest(
        self,
        request: IncomingEvent,
        *,
        tenant_id: str | None,
        installation_id: str,
    ) -> IngestResult:
        """Verify and store one event; flushes, the caller commits.

        A webhook that maps to several canonical types (today only
        ``refund.created``) stores one inbox event per type, each
        under its own idempotency key and source-event identity, in
        the same request transaction: a failure anywhere rolls the
        whole webhook back. The overall status is ``created`` when
        any event was created, ``duplicate`` when every event replayed.
        """
        verified = self.verify(request, installation_id)
        event = verified.event
        primary_status: Literal["created", "duplicate"] | None = None
        primary_event: InboxEvent | None = None
        derived_events: list[InboxEvent] = []
        for index, event_type in enumerate(verified.event_types):
            canonical = event if index == 0 else derive_fanout_event(event, event_type)
            status, inbox_event = self._ingest_one(
                canonical, event_type, tenant_id=tenant_id, installation_id=installation_id
            )
            if index == 0:
                primary_status = status
                primary_event = inbox_event
            else:
                derived_events.append(inbox_event)
                if status == "created":
                    primary_status = "created"
        assert primary_status is not None and primary_event is not None
        return IngestResult(
            status=primary_status,
            event=primary_event,
            signature_verified=verified.signature_verified,
            signature_note=verified.verification_note,
            derived_events=tuple(derived_events),
        )

    def ingest_verified(
        self,
        event: CanonicalEvent,
        event_type: CanonicalEventType,
        *,
        tenant_id: str | None,
        installation_id: str,
        signature_verified: bool = False,
        verification_note: str = "",
    ) -> IngestResult:
        """Store an already-verified event under the §10.4 constraints.

        The post-verification half of :meth:`ingest`: claim the
        idempotency key, store the inbox row, complete the claim, and
        trigger processing for a new event. The reconciliation sync
        (US-512) uses this for polled events — they arrive already
        verified by their connector (authenticated server-to-server
        with the store's scoped read-only key / read-only DB user),
        so no webhook HMAC applies — while sharing the exact same
        idempotency path as webhooks: a polled fill of a missed
        webhook is a duplicate, never a second accounting effect.
        Flushes; the caller commits.
        """
        status, inbox_event = self._ingest_one(
            event, event_type, tenant_id=tenant_id, installation_id=installation_id
        )
        return IngestResult(
            status=status,
            event=inbox_event,
            signature_verified=signature_verified,
            signature_note=verification_note,
        )

    def _ingest_one(
        self,
        event: CanonicalEvent,
        event_type: CanonicalEventType,
        *,
        tenant_id: str | None,
        installation_id: str,
    ) -> tuple[Literal["created", "duplicate"], InboxEvent]:
        """Claim the idempotency key and store one canonical event."""
        # §10.4 (2): claim the idempotency key first, so a concurrent
        # duplicate with a different payload conflicts here instead of
        # racing the inbox insert.
        record = self._idempotency.claim(
            installation_id,
            EVENT_INGEST_OPERATION,
            event.idempotency_key,
            event.payload_hash,
        )
        if record is None:
            existing = self._idempotency.find(
                installation_id, EVENT_INGEST_OPERATION, event.idempotency_key
            )
            if existing is None:
                # Lost a race with a concurrent release; re-claim once.
                record = self._idempotency.claim(
                    installation_id,
                    EVENT_INGEST_OPERATION,
                    event.idempotency_key,
                    event.payload_hash,
                )
            elif existing.request_hash != event.payload_hash:
                raise IdempotencyConflictError(
                    f"Idempotency key {event.idempotency_key!r} was already used "
                    "with a different payload.",
                    details={"idempotency_key": event.idempotency_key},
                )
            elif existing.is_completed:
                reference = existing.response_reference or ""
                inbox_event = self._inbox.get_by_source_event(
                    event.source_system, event.source_store_id, event.source_event_id
                )
                if inbox_event is None and reference.startswith("inbox_events/"):
                    inbox_event = self._inbox.get(reference.split("/", 1)[1])
                if inbox_event is None:
                    raise IdempotencyConflictError(
                        f"Idempotency key {event.idempotency_key!r} was already used, "
                        "but its event is no longer available.",
                        details={"idempotency_key": event.idempotency_key},
                    )
                return "duplicate", inbox_event
            elif existing.expires_at is not None and existing.expires_at <= _now():
                # Stale pending claim (a crashed ingest): release and re-claim.
                self._idempotency.release(existing)
                record = self._idempotency.claim(
                    installation_id,
                    EVENT_INGEST_OPERATION,
                    event.idempotency_key,
                    event.payload_hash,
                )
            else:
                # The same key is being ingested concurrently right now.
                raise IdempotencyConflictError(
                    f"Idempotency key {event.idempotency_key!r} is already being processed.",
                    details={"idempotency_key": event.idempotency_key},
                )
            if record is None:
                raise IdempotencyConflictError(
                    f"Idempotency key {event.idempotency_key!r} is already being processed.",
                    details={"idempotency_key": event.idempotency_key},
                )

        # §10.4 (1): the inbox store serializes concurrent duplicates
        # of the same source event (advisory lock + unique constraint).
        outcome = self._inbox.store(event, event_type, tenant_id, installation_id)
        if outcome.status == "conflict":
            self._idempotency.release(record)
            raise IdempotencyConflictError(
                f"Source event {event.source_system}/{event.source_store_id}/"
                f"{event.source_event_id} was already received with a different payload.",
                details={
                    "source_system": event.source_system,
                    "source_store_id": event.source_store_id,
                    "source_event_id": event.source_event_id,
                },
            )
        assert outcome.event is not None  # created/duplicate always carry the row
        self._idempotency.complete(
            record,
            response_status=201 if outcome.status == "created" else 200,
            response_body={"inbox_event_id": outcome.event.id, "status": outcome.event.status.value},
            response_reference=f"inbox_events/{outcome.event.id}",
        )
        if outcome.status == "created":
            # Best-effort trigger: the job re-checks the event exists,
            # and the periodic sweep is the backstop if this fires
            # before the request transaction commits.
            self._job_queue.defer_process_event(outcome.event.id)
        return outcome.status, outcome.event


# ---------------------------------------------------------------------------
# Pipeline (§10.3 steps 3–10)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _CreatedDocument:
    """An engine document created for one event (step 5, US-511).

    ``kind`` selects the validate/post operations; ``total`` is the
    gate amount (the document's magnitude in Rial); the target entity
    type is what the §10.4 external-id mapping records.
    """

    kind: str  # journal | sale | invoice | credit_note | payment | invoice_cancel
    engine_id: int
    total: MoneyIRR
    target_entity_type: str


class EventPipelineService:
    """Run one inbox event through the §10.3 pipeline.

    Steps: normalize (currency conversion) → resolve mappings →
    build + create the engine document → validate the document →
    policy gate → post-or-draft → persist the engine reference +
    correlation mapping → mark processed + emit the outbox result
    event → retry/DLQ. Every decision is recorded in the automation
    audit log with its policy and mapping versions.

    The document is whatever the draft builder returns: a journal
    draft (the generic path), or — for store events (US-511) — a
    customer invoice, a credit note, a payment, a sale, or an
    invoice cancellation. Payments are never auto-posted (settlement
    confirms the deposit first); cancellations are terminal.

    Exactly-once observable effect: before creating a draft, the
    pipeline checks the §10.4 external-entity mapping — a
    reprocessed event whose engine record already exists is marked
    processed without creating a second draft. (The residual window
    is an engine draft created but the HesabYar transaction lost;
    see docs/event-platform.md — no engine-side idempotency key
    exists for drafts, so this window is inherent, not hidden.)
    """

    def __init__(
        self,
        inbox: EventInboxRepositoryPort,
        outbox: EventOutboxRepositoryPort,
        external_ids: ExternalIdMapPort,
        audit_log: DecisionAuditLogPort,
        store_settings: StoreSettingsPort,
        mapping_resolver: MappingResolverPort,
        draft_builder: DraftBuilderPort,
        policy_gate: PolicyGatePort,
        review_queue: ReviewQueuePort,
        engine: AccountingEnginePort,
        cheque_intake: ChequeIntakeHandlerPort | None = None,
    ) -> None:
        self._inbox = inbox
        self._outbox = outbox
        self._external_ids = external_ids
        self._audit_log = audit_log
        self._store_settings = store_settings
        self._mapping_resolver = mapping_resolver
        self._draft_builder = draft_builder
        self._policy_gate = policy_gate
        self._review_queue = review_queue
        self._engine = engine
        self._cheque_intake = cheque_intake

    def process(self, event_id: str) -> PipelineResult:
        """Process one inbox event; flushes, the caller commits."""
        inbox_event = self._inbox.get(event_id)
        if inbox_event is None:
            raise EventNotFoundError(f"Inbox event {event_id!r} does not exist.")
        if inbox_event.status == InboxEventStatus.PROCESSED:
            return PipelineResult(
                inbox_event_id=inbox_event.id,
                outcome=PipelineOutcome.PROCESSED_DRAFT,
                error="Event was already processed; no duplicate effect.",
            )
        if inbox_event.status in (InboxEventStatus.DEAD_LETTERED, InboxEventStatus.NEEDS_REVIEW):
            return PipelineResult(
                inbox_event_id=inbox_event.id,
                outcome=(
                    PipelineOutcome.DEAD_LETTERED
                    if inbox_event.status == InboxEventStatus.DEAD_LETTERED
                    else PipelineOutcome.NEEDS_REVIEW
                ),
                error=f"Event is {inbox_event.status.value}; use replay to reprocess it.",
            )

        started_at = _now()
        try:
            return self._run_pipeline(inbox_event, started_at)
        except (MappingUnresolvedError, InvalidStoreAmountError, InvalidEventEnvelopeError) as exc:
            # Permanent failure: fail closed to the review queue.
            return self._route_to_review(inbox_event, started_at, exc)
        except HesabYarError as exc:
            translated = translate_engine_error(exc)
            if is_retryable(translated):
                return self._schedule_retry_or_dead_letter(inbox_event, started_at, translated)
            return self._route_to_review(inbox_event, started_at, translated)
        except Exception as exc:  # unknown failure: transient, DLQ is the backstop
            return self._schedule_retry_or_dead_letter(inbox_event, started_at, exc)

    def _run_pipeline(self, inbox_event: InboxEvent, started_at: datetime.datetime) -> PipelineResult:
        event = inbox_event.to_canonical_event()
        # The type was validated at ingest and stored on the inbox
        # row; re-validating here fails closed to review if the
        # stored value is somehow not a canonical type.
        event_type = parse_event_type(inbox_event.event_type)

        # Step 3: normalize (Toman→Rial at the connector boundary).
        settings = self._store_settings.get(event.source_system, event.source_store_id)
        if settings is None:
            settings = StoreSettings(
                source_system=event.source_system,
                source_store_id=event.source_store_id,
            )
        normalized = normalize_event(
            event,
            settings,
            event_type=event_type,
            installation_id=inbox_event.installation_id,
        )

        # Exactly-once pre-check (§10.4, third constraint): an entity
        # that already maps to an engine record is not drafted twice.
        # Exception (US-511): a late completion for a cancelled order
        # is NOT suppressed — it is an anomaly the review queue must
        # see, and the builder's out-of-order guard raises for it.
        # Two signals prove the order is gone: its invoice was
        # cancelled, or a cancellation mapping exists (the invoice
        # may have been credited instead of cancelled).
        if event.external_entity_type is not None and event.external_entity_id is not None:
            existing = self._external_ids.get(
                event.source_system, event.external_entity_type, event.external_entity_id
            )
            suppress = False
            if existing is not None:
                suppress = True
                if event_type is CanonicalEventType.SALES_ORDER_COMPLETED:
                    # A late completion for a cancelled order is an
                    # anomaly, not a duplicate: two signals prove the
                    # order is gone — its invoice was cancelled, or a
                    # cancellation mapping exists (the invoice may
                    # have been credited instead of cancelled). The
                    # builder's out-of-order guard raises for it.
                    # Other event types suppress as before (a second
                    # cancellation of the same order is a duplicate).
                    suppress = not self._is_cancelled_invoice(existing) and not (
                        self._order_was_cancelled(event)
                    )
            if suppress:
                assert existing is not None
                return self._finish_processed(
                    inbox_event,
                    started_at,
                    draft_ref=existing.target_external_id,
                    posted_ref=None,
                    decision_kind=AutomationDecisionKind.DRAFT,
                    policy_version="n/a",
                    mapping_version=None,
                    gate_results=(),
                    reason=(
                        f"Engine record {existing.target_external_id} already exists "
                        "for this entity; no duplicate draft created."
                    ),
                    duplicate=True,
                )

        # Step 4: resolve versioned mappings (fail-closed on gaps).
        mapping = self._mapping_resolver.resolve(normalized)
        if not mapping.is_complete:
            raise MappingUnresolvedError(
                "Mapping is incomplete for this event.",
                details={"unresolved": list(mapping.unresolved)},
            )

        # Cheque intake (US-533) branches off here: it creates NO
        # engine document, so the draft-builder / validate / gate /
        # post steps below do not apply. The intake handler owns the
        # registry + payee check + convergence; the pipeline still
        # owns the §10.4 mapping + audit + processed marking.
        if event_type in (
            CanonicalEventType.CHEQUE_RECEIVED_REGISTERED,
            CanonicalEventType.CHEQUE_RECEIVED_CANCELLED,
        ):
            return self._run_cheque_intake(
                inbox_event, started_at, normalized, mapping
            )

        # Step 5: build + create the engine document (US-511: the
        # builder may return a journal draft, a sale, an invoice, a
        # credit note, a payment, or an invoice cancellation — the
        # pipeline creates, validates and posts each kind through
        # the matching engine operation).
        document_command = self._draft_builder.build(normalized, mapping)
        document = self._create_document(document_command)

        # Step 6: validate the document (read-only check, never mutates).
        self._validate_document(document_command, document)

        # Step 7: policy gate — auto-post only when explicitly allowed.
        policy = self._policy_gate.evaluate(normalized, mapping, document.total)
        posted_ref, decision_kind, reason = self._post_or_hold(
            document_command, document, policy
        )

        # Step 8: persist the engine reference + correlation mapping.
        if event.external_entity_type is not None and event.external_entity_id is not None:
            self._external_ids.save(
                ExternalIdMapping(
                    source_system=event.source_system,
                    external_entity_type=event.external_entity_type,
                    external_entity_id=event.external_entity_id,
                    target_engine=TARGET_ENGINE_TRYTON,
                    target_entity_type=document.target_entity_type,
                    target_external_id=str(document.engine_id),
                    engine_company_id=None,
                    tenant_id=inbox_event.tenant_id,
                )
            )

        return self._finish_processed(
            inbox_event,
            started_at,
            draft_ref=str(document.engine_id),
            posted_ref=posted_ref,
            decision_kind=decision_kind,
            policy_version=policy.policy_version,
            mapping_version=mapping.mapping_version,
            gate_results=policy.gate_results,
            reason=reason,
        )

    def _run_cheque_intake(
        self,
        inbox_event: InboxEvent,
        started_at: datetime.datetime,
        normalized: NormalizedEvent,
        mapping: ResolvedMapping,
    ) -> PipelineResult:
        """Process a site cheque event: intake, never a journal (US-533).

        The payload is validated fail-closed at the adapter boundary
        (the 24-field contract); the handler registers the intake
        (or voids it, for a cancellation) through
        :class:`ChequeCommandService
        <hesabyar.application.commands.cheques.ChequeCommandService>`.
        No policy gate applies — intake creates no journal; the gate
        is evaluated at the first journal-creating transition
        (``registered -> held``). The §10.4 external-entity mapping
        (cheque / cheque_cancellation → ``account.check``) is still
        recorded, so a redelivery suppresses in the pre-check. A
        duplicate conflict is translated by the handler into a
        processed outcome (the review item is already filed); the
        existing cheque is the reference and its mapping is saved
        for the redelivered event too.
        """
        event = normalized.event
        event_type = normalized.event_type
        validate_store_payload(
            event_type,
            normalized.converted_payload,
            external_entity_type=event.external_entity_type,
            external_entity_id=event.external_entity_id,
        )
        if self._cheque_intake is None:
            raise MappingUnresolvedError(
                "No cheque intake handler is wired for this pipeline; "
                f"a {event_type.value} event cannot be ingested.",
                details={"event_type": event_type.value},
            )
        if inbox_event.tenant_id is None or inbox_event.installation_id is None:
            raise MappingUnresolvedError(
                "A cheque intake event needs a tenant context.",
                details={"event_type": event_type.value},
            )
        if mapping.company is None:
            raise MappingUnresolvedError(
                "No company mapping resolved for this cheque event.",
                details={"unresolved": list(mapping.unresolved)},
            )
        if event_type is CanonicalEventType.CHEQUE_RECEIVED_REGISTERED:
            outcome = self._cheque_intake.register_site_cheque(
                event=normalized,
                mapping=mapping,
                tenant_id=inbox_event.tenant_id,
                installation_id=inbox_event.installation_id,
            )
        else:
            outcome = self._cheque_intake.cancel_site_cheque(
                event=normalized,
                mapping=mapping,
                tenant_id=inbox_event.tenant_id,
                installation_id=inbox_event.installation_id,
            )
        if (
            event.external_entity_type is not None
            and event.external_entity_id is not None
            and outcome.engine_cheque_id is not None
        ):
            self._external_ids.save(
                ExternalIdMapping(
                    source_system=event.source_system,
                    external_entity_type=event.external_entity_type,
                    external_entity_id=event.external_entity_id,
                    target_engine=TARGET_ENGINE_TRYTON,
                    target_entity_type=TARGET_ENTITY_CHEQUE,
                    target_external_id=str(outcome.engine_cheque_id),
                    engine_company_id=None,
                    tenant_id=inbox_event.tenant_id,
                )
            )
        return self._finish_processed(
            inbox_event,
            started_at,
            draft_ref=(
                str(outcome.engine_cheque_id)
                if outcome.engine_cheque_id is not None
                else None
            ),
            posted_ref=None,
            decision_kind=AutomationDecisionKind.DRAFT,
            policy_version="n/a",
            mapping_version=mapping.mapping_version,
            gate_results=(),
            reason=outcome.detail,
        )

    def _is_cancelled_invoice(self, mapping: ExternalIdMapping) -> bool:
        """True when a §10.4 mapping points at a cancelled invoice (US-511).

        Only invoice targets are read; any other target (a journal,
        a payment, ...) suppresses as before. A missing invoice reads
        as not-cancelled — the mapping is the record.
        """
        if mapping.target_entity_type != "account.invoice":
            return False
        if not mapping.target_external_id.isdigit():
            return False
        with translate_port_errors():
            view = self._engine.get_invoice(InvoiceRef(int(mapping.target_external_id)))
        return view is not None and view.state == "cancelled"

    def _order_was_cancelled(self, event: CanonicalEvent) -> bool:
        """True when the order this event names was already cancelled (US-511).

        The cancellation mapping is keyed by the order id (the
        cancellation acts on the order), so it proves the order is
        gone even when the invoice was credited instead of cancelled.
        Runs on the raw payload (before validation): a missing or
        non-string order id reads as not-cancelled.
        """
        order_id = event.payload.get("order_id")
        if not isinstance(order_id, str) or not order_id.strip():
            return False
        found = self._external_ids.get(
            event.source_system, ORDER_CANCELLATION_ENTITY_TYPE, order_id.strip()
        )
        return found is not None

    def _create_document(self, command: EngineDocumentCommand) -> _CreatedDocument:
        """Create the engine document for a builder command (step 5)."""
        with translate_port_errors():
            if isinstance(command, CreateJournalDraft):
                ref = self._engine.create_journal_draft(command)
                return _CreatedDocument("journal", ref.engine_id, command.total_debit, "account.move")
            if isinstance(command, CreateSale):
                ref = self._engine.create_sale(command)
                return _CreatedDocument("sale", ref.engine_id, command.total_amount, "sale.sale")
            if isinstance(command, CreateInvoiceDraft):
                ref = self._engine.create_invoice_draft(command)
                return _CreatedDocument(
                    "invoice", ref.engine_id, command.total_amount, "account.invoice"
                )
            if isinstance(command, CreateCreditNote):
                ref = self._engine.create_credit_note(command)
                if command.partial_amount is not None:
                    total = command.partial_amount
                else:
                    source = self._engine.get_invoice(command.invoice)
                    total = source.total_amount if source is not None else MoneyIRR(0)
                return _CreatedDocument("credit_note", ref.engine_id, total, "account.invoice")
            if isinstance(command, CreatePayment):
                ref = self._engine.create_payment(command)
                return _CreatedDocument("payment", ref.engine_id, command.amount, "account.payment")
            if isinstance(command, CancelInvoice):
                view = self._engine.cancel_invoice(command.invoice)
                return _CreatedDocument(
                    "invoice_cancel", view.ref.engine_id, view.total_amount, "account.invoice"
                )
        raise AssertionError(f"unhandled document command {type(command).__name__}")

    def _validate_document(
        self, command: EngineDocumentCommand, document: _CreatedDocument
    ) -> None:
        """Validate a created document (step 6; read-only, never mutates)."""
        with translate_port_errors():
            if isinstance(command, CreateJournalDraft):
                validation = self._engine.validate_journal_draft(JournalDraftRef(document.engine_id))
                if not validation.valid:
                    raise InvalidEventEnvelopeError(
                        "The engine draft failed validation.",
                        details={"issues": list(validation.codes())},
                    )
            elif isinstance(command, CreateSale):
                view = self._engine.get_sale(SaleRef(document.engine_id))
                if view is None or not view.is_draft:
                    raise InvalidEventEnvelopeError(
                        f"Sale {document.engine_id} was not created as a draft.",
                        details={"sale_id": document.engine_id},
                    )
            elif isinstance(command, (CreateInvoiceDraft, CreateCreditNote)):
                validation = self._engine.validate_invoice_draft(InvoiceRef(document.engine_id))
                if not validation.valid:
                    raise InvalidEventEnvelopeError(
                        "The engine invoice draft failed validation.",
                        details={"issues": list(validation.codes())},
                    )
            elif isinstance(command, CreatePayment):
                view = self._engine.get_payment(PaymentRef(document.engine_id))
                if view is None or not view.is_draft:
                    raise InvalidEventEnvelopeError(
                        f"Payment {document.engine_id} was not created as a draft.",
                        details={"payment_id": document.engine_id},
                    )
            elif isinstance(command, CancelInvoice):
                view = self._engine.get_invoice(InvoiceRef(document.engine_id))
                if view is None or view.state != "cancelled":
                    raise InvalidEventEnvelopeError(
                        f"Invoice {document.engine_id} was not cancelled.",
                        details={"invoice_id": document.engine_id},
                    )

    def _post_or_hold(
        self,
        command: EngineDocumentCommand,
        document: _CreatedDocument,
        policy: PolicyDecision,
    ) -> tuple[str | None, AutomationDecisionKind, str]:
        """Step 7: auto-post when the gate allows — with two exceptions.

        Payments are never auto-posted: a captured payment is money
        the gateway owes, not money received, so the draft waits for
        settlement to confirm the deposit (5پ). A cancellation is
        terminal by construction, so no posting applies. In both
        cases the gate's verdict is still recorded in the audit log.
        """
        if not policy.allowed:
            return None, policy.decision, policy.reason
        if document.kind == "payment":
            return (
                None,
                AutomationDecisionKind.DRAFT,
                "Payment drafts are never auto-posted; settlement confirms the deposit "
                f"first. Gate verdict was: {policy.reason}",
            )
        if document.kind == "invoice_cancel":
            return (
                None,
                AutomationDecisionKind.DRAFT,
                f"Invoice cancellation is terminal; no posting applies. Gate verdict was: {policy.reason}",
            )
        with translate_port_errors():
            if isinstance(command, CreateJournalDraft):
                posted = self._engine.post_journal(PostJournal(draft=JournalDraftRef(document.engine_id)))
                return str(posted.engine_id), policy.decision, policy.reason
            if isinstance(command, CreateSale):
                self._engine.quote_sale(SaleRef(document.engine_id))
                confirmed = self._engine.confirm_sale(SaleRef(document.engine_id))
                return str(confirmed.ref.engine_id), policy.decision, policy.reason
            if isinstance(command, (CreateInvoiceDraft, CreateCreditNote)):
                self._engine.validate_invoice(InvoiceRef(document.engine_id))
                posted = self._engine.post_invoice(InvoiceRef(document.engine_id))
                return str(posted.ref.engine_id), policy.decision, policy.reason
        raise AssertionError(f"unhandled document command {type(command).__name__}")

    def _finish_processed(
        self,
        inbox_event: InboxEvent,
        started_at: datetime.datetime,
        *,
        draft_ref: str | None,
        posted_ref: str | None,
        decision_kind: AutomationDecisionKind,
        policy_version: str,
        mapping_version: str | None,
        gate_results: tuple[GateResult, ...],
        reason: str,
        duplicate: bool = False,
    ) -> PipelineResult:
        """Step 9: record the decision, emit the outbox event, mark processed."""
        now = _now()
        decision = self._audit_log.record(
            AutomationDecision(
                id=str(uuid.uuid4()),
                tenant_id=inbox_event.tenant_id,
                inbox_event_id=inbox_event.id,
                event_type=inbox_event.event_type,
                decision=decision_kind,
                policy_version=policy_version,
                mapping_version=mapping_version,
                gate_results=gate_results,
                decided_by="system",
                decided_at=now,
                correlation_id=inbox_event.correlation_id,
                details={
                    "reason": reason,
                    "draft_ref": draft_ref,
                    "posted_ref": posted_ref,
                    "duplicate_suppressed": duplicate,
                },
            )
        )
        outbox_payload = {
            "inbox_event_id": inbox_event.id,
            "event_type": inbox_event.event_type,
            "decision": decision_kind.value,
            "draft_ref": draft_ref,
            "posted_ref": posted_ref,
            "duplicate_suppressed": duplicate,
        }
        if inbox_event.tenant_id is not None:
            self._outbox.save(
                OutboxEvent(
                    id=str(uuid.uuid4()),
                    tenant_id=inbox_event.tenant_id,
                    aggregate_type=INBOX_EVENT_AGGREGATE,
                    aggregate_id=inbox_event.id,
                    event_type=EVENT_PROCESSED_OUTBOX_TYPE,
                    payload=outbox_payload,
                    payload_hash=canonical_payload_hash(outbox_payload),
                    status=OutboxEventStatus.PENDING,
                    attempts=0,
                    last_attempt_at=None,
                    last_error=None,
                    published_at=None,
                    correlation_id=inbox_event.correlation_id,
                    created_at=now,
                    updated_at=now,
                )
            )
        self._inbox.save(
            dataclasses.replace(
                inbox_event,
                status=InboxEventStatus.PROCESSED,
                last_attempt_at=now,
                next_attempt_at=None,
                last_error=None,
                processed_at=now,
                updated_at=now,
            )
        )
        self._record_attempt(inbox_event, started_at, succeeded=True)
        return PipelineResult(
            inbox_event_id=inbox_event.id,
            outcome=(
                PipelineOutcome.PROCESSED_POSTED
                if posted_ref is not None
                else PipelineOutcome.PROCESSED_DRAFT
            ),
            draft_ref=draft_ref,
            posted_ref=posted_ref,
            decision_id=decision.id,
        )

    def _route_to_review(
        self,
        inbox_event: InboxEvent,
        started_at: datetime.datetime,
        exc: BaseException,
    ) -> PipelineResult:
        """Fail closed: the event waits for a human; nothing is posted."""
        now = _now()
        message = _error_message(exc)
        code = exc.code if isinstance(exc, HesabYarError) else type(exc).__name__
        review_id = self._review_queue.enqueue(
            ReviewItemDraft(
                type="mapping" if isinstance(exc, MappingUnresolvedError) else "inbox_dead_letter",
                severity="high",
                source_event_ref=inbox_event.id,
                reason=message,
                suggested_action="Resolve the mapping or fix the source data, then reprocess the event.",
                amount_irr=None,
                details={"error_code": code, "event_type": inbox_event.event_type},
            )
        )
        decision = self._audit_log.record(
            AutomationDecision(
                id=str(uuid.uuid4()),
                tenant_id=inbox_event.tenant_id,
                inbox_event_id=inbox_event.id,
                event_type=inbox_event.event_type,
                decision=AutomationDecisionKind.NEEDS_REVIEW,
                policy_version="n/a",
                mapping_version=None,
                gate_results=(),
                decided_by="system",
                decided_at=now,
                correlation_id=inbox_event.correlation_id,
                details={"reason": message, "error_code": code, "review_item_id": review_id},
            )
        )
        self._inbox.save(
            dataclasses.replace(
                inbox_event,
                status=InboxEventStatus.NEEDS_REVIEW,
                last_attempt_at=now,
                next_attempt_at=None,
                last_error=message,
                updated_at=now,
            )
        )
        self._record_attempt(inbox_event, started_at, succeeded=False, error_code=code, error_message=message)
        return PipelineResult(
            inbox_event_id=inbox_event.id,
            outcome=PipelineOutcome.NEEDS_REVIEW,
            decision_id=decision.id,
            error=message,
        )

    def _schedule_retry_or_dead_letter(
        self,
        inbox_event: InboxEvent,
        started_at: datetime.datetime,
        exc: BaseException,
    ) -> PipelineResult:
        """Step 10: bounded retry, then the dead-letter queue."""
        now = _now()
        message = _error_message(exc)
        code = exc.code if isinstance(exc, HesabYarError) else type(exc).__name__
        next_attempt = next_retry_at(inbox_event.attempts, now)
        if next_attempt is None:
            # Retries exhausted (MAX_ATTEMPTS): dead-letter.
            review_id = self._review_queue.enqueue(
                ReviewItemDraft(
                    type="inbox_dead_letter",
                    severity="critical",
                    source_event_ref=inbox_event.id,
                    reason=f"Event failed {MAX_ATTEMPTS} attempts: {message}",
                    suggested_action="Inspect the error, fix the cause, then replay the event from the review queue.",
                    amount_irr=None,
                    details={"error_code": code, "event_type": inbox_event.event_type},
                )
            )
            decision = self._audit_log.record(
                AutomationDecision(
                    id=str(uuid.uuid4()),
                    tenant_id=inbox_event.tenant_id,
                    inbox_event_id=inbox_event.id,
                    event_type=inbox_event.event_type,
                    decision=AutomationDecisionKind.DEAD_LETTER,
                    policy_version="n/a",
                    mapping_version=None,
                    gate_results=(),
                    decided_by="system",
                    decided_at=now,
                    correlation_id=inbox_event.correlation_id,
                    details={
                        "reason": message,
                        "error_code": code,
                        "attempts": inbox_event.attempts,
                        "review_item_id": review_id,
                    },
                )
            )
            self._inbox.save(
                dataclasses.replace(
                    inbox_event,
                    status=InboxEventStatus.DEAD_LETTERED,
                    last_attempt_at=now,
                    next_attempt_at=None,
                    last_error=message,
                    dead_lettered_at=now,
                    updated_at=now,
                )
            )
            self._record_attempt(
                inbox_event, started_at, succeeded=False, error_code=code, error_message=message
            )
            return PipelineResult(
                inbox_event_id=inbox_event.id,
                outcome=PipelineOutcome.DEAD_LETTERED,
                decision_id=decision.id,
                error=message,
            )
        self._inbox.save(
            dataclasses.replace(
                inbox_event,
                status=InboxEventStatus.RECEIVED,
                last_attempt_at=now,
                next_attempt_at=next_attempt,
                last_error=message,
                updated_at=now,
            )
        )
        self._record_attempt(
            inbox_event, started_at, succeeded=False, error_code=code, error_message=message
        )
        return PipelineResult(
            inbox_event_id=inbox_event.id,
            outcome=PipelineOutcome.RETRY_SCHEDULED,
            error=message,
            next_attempt_at=next_attempt,
        )

    def _record_attempt(
        self,
        inbox_event: InboxEvent,
        started_at: datetime.datetime,
        *,
        succeeded: bool,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        self._outbox.save_integration_attempt(
            IntegrationAttempt(
                id=str(uuid.uuid4()),
                inbox_event_id=inbox_event.id,
                outbox_event_id=None,
                attempt_number=inbox_event.attempts,
                status=IntegrationAttemptStatus.SUCCEEDED if succeeded else IntegrationAttemptStatus.FAILED,
                started_at=started_at,
                finished_at=_now(),
                error_code=error_code,
                error_message=error_message,
                created_at=_now(),
            )
        )


def _error_message(exc: BaseException) -> str:
    if isinstance(exc, HesabYarError):
        return exc.message
    return f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Worker (database poller)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkerReport:
    """Outcome of one worker sweep (inbox + outbox)."""

    claimed: int = 0
    processed_draft: int = 0
    processed_posted: int = 0
    needs_review: int = 0
    dead_lettered: int = 0
    retries_scheduled: int = 0
    outbox_published: int = 0
    outbox_failed: int = 0
    errors: tuple[str, ...] = ()


class EventWorker:
    """Poll the inbox/outbox tables and process what is due.

    One sweep: claim due inbox events (``FOR UPDATE SKIP LOCKED``,
    so concurrent workers never collide) and run each through the
    pipeline; then claim pending outbox events and publish them.
    A failure in one event never aborts the sweep — the pipeline
    records it and moves on.

    Flushes; the caller commits (the procrastinate task commits per
    event; the REST poll endpoint commits once per sweep).
    """

    def __init__(
        self,
        pipeline: EventPipelineService,
        inbox: EventInboxRepositoryPort,
        outbox: EventOutboxRepositoryPort,
        publisher: OutboxPublisherPort,
    ) -> None:
        self._pipeline = pipeline
        self._inbox = inbox
        self._outbox = outbox
        self._publisher = publisher

    def process_one(self, event_id: str) -> PipelineResult | None:
        """Process one inbox event by id (the procrastinate job entry).

        Claims the event first (``FOR UPDATE SKIP LOCKED``), so two
        triggers for the same event never process it twice. Returns
        None when the event does not exist (yet — the ingest
        transaction may not have committed when the trigger fired),
        is not due, or is already claimed; the periodic sweep is the
        backstop for all three cases.
        """
        claimed = self._inbox.claim_one(event_id)
        if claimed is None:
            return None
        return self._pipeline.process(event_id)

    def poll_once(self, limit: int = 50) -> WorkerReport:
        """Run one sweep over due inbox events + pending outbox events."""
        claimed = self._inbox.claim_due(limit)
        processed_draft = processed_posted = needs_review = 0
        dead_lettered = retries_scheduled = 0
        errors: list[str] = []
        for inbox_event in claimed:
            try:
                result = self._pipeline.process(inbox_event.id)
            except Exception as exc:  # defensive: the pipeline handles its own errors
                errors.append(f"{inbox_event.id}: {_error_message(exc)}")
                continue
            if result.outcome == PipelineOutcome.PROCESSED_DRAFT:
                processed_draft += 1
            elif result.outcome == PipelineOutcome.PROCESSED_POSTED:
                processed_posted += 1
            elif result.outcome == PipelineOutcome.NEEDS_REVIEW:
                needs_review += 1
            elif result.outcome == PipelineOutcome.DEAD_LETTERED:
                dead_lettered += 1
            elif result.outcome == PipelineOutcome.RETRY_SCHEDULED:
                retries_scheduled += 1
        published, failed = self._publish_pending(limit)
        return WorkerReport(
            claimed=len(claimed),
            processed_draft=processed_draft,
            processed_posted=processed_posted,
            needs_review=needs_review,
            dead_lettered=dead_lettered,
            retries_scheduled=retries_scheduled,
            outbox_published=published,
            outbox_failed=failed,
            errors=tuple(errors),
        )

    def _publish_pending(self, limit: int) -> tuple[int, int]:
        published = failed = 0
        for event in self._outbox.claim_pending(limit):
            started_at = _now()
            try:
                delivered = self._publisher.publish(event)
            except Exception as exc:  # a broken consumer must not kill the sweep
                delivered = False
                last_error = _error_message(exc)
            else:
                last_error = None
            now = _now()
            if delivered:
                self._outbox.save(
                    dataclasses.replace(
                        event,
                        status=OutboxEventStatus.PUBLISHED,
                        attempts=event.attempts + 1,
                        last_attempt_at=now,
                        last_error=None,
                        published_at=now,
                        updated_at=now,
                    )
                )
                self._outbox.save_integration_attempt(
                    IntegrationAttempt(
                        id=str(uuid.uuid4()),
                        inbox_event_id=None,
                        outbox_event_id=event.id,
                        attempt_number=event.attempts + 1,
                        status=IntegrationAttemptStatus.SUCCEEDED,
                        started_at=started_at,
                        finished_at=now,
                        error_code=None,
                        error_message=None,
                        created_at=now,
                    )
                )
                published += 1
            else:
                self._outbox.save(
                    dataclasses.replace(
                        event,
                        status=OutboxEventStatus.FAILED,
                        attempts=event.attempts + 1,
                        last_attempt_at=now,
                        last_error=last_error or "Outbox publisher reported failure.",
                        updated_at=now,
                    )
                )
                self._outbox.save_integration_attempt(
                    IntegrationAttempt(
                        id=str(uuid.uuid4()),
                        inbox_event_id=None,
                        outbox_event_id=event.id,
                        attempt_number=event.attempts + 1,
                        status=IntegrationAttemptStatus.FAILED,
                        started_at=started_at,
                        finished_at=now,
                        error_code="OUTBOX_PUBLISH_FAILED",
                        error_message=last_error,
                        created_at=now,
                    )
                )
                failed += 1
        return published, failed


# ---------------------------------------------------------------------------
# DLQ replay + store settings + staleness
# ---------------------------------------------------------------------------


class DlqReplayService:
    """Replay a dead-lettered event manually, from the UI.

    The replay keeps the event's idempotency key and source identity,
    so the §10.4 constraints + the external-entity mapping make the
    reprocessing effect-free when the engine record already exists:
    no duplicate draft, no duplicate post. Attempts reset so the
    event gets a fresh retry budget.
    """

    def __init__(
        self,
        inbox: EventInboxRepositoryPort,
        audit_log: DecisionAuditLogPort,
        job_queue: JobQueuePort,
    ) -> None:
        self._inbox = inbox
        self._audit_log = audit_log
        self._job_queue = job_queue

    def replay(self, event_id: str, *, actor: str, tenant_id: str | None = None) -> InboxEvent:
        """Reset a dead-lettered event to ``received``; flushes, caller commits."""
        inbox_event = self._inbox.get(event_id, tenant_id)
        if inbox_event is None:
            raise EventNotFoundError(f"Inbox event {event_id!r} does not exist.")
        if inbox_event.status != InboxEventStatus.DEAD_LETTERED:
            raise EventNotDeadLetteredError(
                f"Inbox event {event_id!r} is {inbox_event.status.value}, not dead-lettered; "
                "only dead-lettered events can be replayed.",
                details={"status": inbox_event.status.value},
            )
        now = _now()
        replayed = self._inbox.save(
            dataclasses.replace(
                inbox_event,
                status=InboxEventStatus.RECEIVED,
                attempts=0,
                last_attempt_at=None,
                next_attempt_at=None,
                last_error=None,
                dead_lettered_at=None,
                updated_at=now,
            )
        )
        self._audit_log.record(
            AutomationDecision(
                id=str(uuid.uuid4()),
                tenant_id=inbox_event.tenant_id,
                inbox_event_id=inbox_event.id,
                event_type=inbox_event.event_type,
                decision=AutomationDecisionKind.REPLAYED,
                policy_version="n/a",
                mapping_version=None,
                gate_results=(),
                decided_by=actor,
                decided_at=now,
                correlation_id=inbox_event.correlation_id,
                details={"reason": "Manual DLQ replay from the UI.", "previous_attempts": inbox_event.attempts},
            )
        )
        self._job_queue.defer_process_event(replayed.id)
        return replayed


class StoreSettingsService:
    """Read/write the per-store connector settings (ADR 0003 decision 4)."""

    def __init__(self, settings: StoreSettingsPort) -> None:
        self._settings = settings

    def get_settings(self, source_system: str, source_store_id: str) -> StoreSettings:
        """Return a store's settings, or the Iranian defaults when none are stored."""
        stored = self._settings.get(source_system, source_store_id)
        if stored is not None:
            return stored
        return StoreSettings(source_system=source_system, source_store_id=source_store_id)

    def update_settings(
        self,
        source_system: str,
        source_store_id: str,
        *,
        prices_in_toman: bool,
        currency: str = "IRR",
    ) -> StoreSettings:
        """Store a store's settings; flushes, the caller commits."""
        normalized_currency = currency.strip().upper()
        if len(normalized_currency) != 3 or not normalized_currency.isalpha():
            raise InvalidEventEnvelopeError(
                f"Currency must be a 3-letter code, got {currency!r}.",
                details={"currency": currency},
            )
        return self._settings.save(
            StoreSettings(
                source_system=source_system,
                source_store_id=source_store_id,
                prices_in_toman=prices_in_toman,
                currency=normalized_currency,
                updated_at=_now(),
            )
        )


class StalenessService:
    """Inbox staleness alerts: events stuck longer than N hours."""

    def __init__(self, inbox: EventInboxRepositoryPort) -> None:
        self._inbox = inbox

    def list_stale(
        self, *, older_than: datetime.timedelta = DEFAULT_STALE_AFTER
    ) -> list[InboxEvent]:
        """Return inbox events stuck in received/processing past the threshold, oldest first."""
        return list(self._inbox.list_stale(older_than))

    def count_stale(self, *, older_than: datetime.timedelta = DEFAULT_STALE_AFTER) -> int:
        """Count inbox events stuck past the threshold."""
        return self._inbox.count_stale(older_than)
