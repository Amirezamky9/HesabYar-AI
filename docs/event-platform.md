# Event platform (US-501, Phase 5 foundation 5الف)

One verified, exactly-once pipeline for every store / gateway / bank
event. This document is the contract the other Phase 5 streams build
against: F2 (Tryton modules) extends the engine port, F3 (registries +
policy gate) replaces the fail-closed defaults, F4 (review queue)
replaces the inbox-backed review adapter.

Related: spec §10 (integration), §19 (money rules), ADR 0003
(store connectivity + banking), phased design §2 (event platform),
§6 (workflows), §8 (cross-cutting).

## 1. Canonical envelope (spec §10.2)

Every event arrives as one JSON object with exactly these 15 fields
(`hesabyar.domain.events.envelope.CanonicalEvent`):

| Field | Meaning |
|---|---|
| `id` | HesabYar UUID (assigned at ingest when absent) |
| `schema_version` | Envelope version; **1** for all new events |
| `source_system` | Connector key, e.g. `hayat`, `zarinpal` — must match the URL connector |
| `source_store_id` | Store id within the source system |
| `source_event_id` | The source's own event id (part of the §10.4 identity) |
| `occurred_at` | When the event happened at the source (ISO 8601) |
| `received_at` | When HesabYar received it (defaults to now) |
| `company_mapping` | Optional source→company mapping hints (object or null) |
| `currency` | 3-letter code, normalized to uppercase (`IRR` in practice) |
| `external_entity_type` / `external_entity_id` | The source entity (e.g. `order` / `order-1`) |
| `correlation_id` | End-to-end trace id (optional) |
| `idempotency_key` | Defaults to `event:{source_system}:{source_store_id}:{source_event_id}` |
| `payload` | The event body (a JSON object) |
| `payload_hash` | SHA-256 of the canonical JSON (sorted keys, compact separators); computed when absent, must match when supplied |

Canonical event types (exactly eleven; a new name needs ADR approval
and is rejected with `EVENT_UNKNOWN_TYPE` until then):

`sales.order.completed`, `sales.order.cancelled`,
`sales.refund.created`, `payment.captured`, `payment.refunded`,
`payment.settlement.received`, `purchase.invoice.received`,
`inventory.cost.recognized`, `bank.transaction.imported`,
`cheque.received.registered`, `cheque.received.cancelled` (the two
cheque names approved by ADR 0004 and wired ingestible by US-533).

**The event type is NOT one of the 15 envelope fields.** It is the
routing discriminator that travels *alongside* the envelope: on the
ingest request it arrives in the `X-HesabYar-Event-Type` header
(required), and the webhook receiver assigns and validates it when
normalizing (phased design §2 §1.3) — the same pattern as
WooCommerce's `X-WC-Webhook-Topic` and the existing
`X-HesabYar-Signature` header. The header keeps the request body
exactly the 15-field envelope, so the HMAC signature covers the
envelope alone. From ingest on, the type is threaded as an explicit
parameter (`parse_event_type` in
`hesabyar.domain.events.envelope`; `VerifiedEvent.event_type`,
`NormalizedEvent.event_type`, and the `event_type` argument of
`EventInboxRepositoryPort.store`), and it is stored in the
`inbox_events.event_type` column (migration 0004). A missing header
is `EVENT_INVALID_ENVELOPE`; an unknown type is `EVENT_UNKNOWN_TYPE`
— both 422. An `event_type` key inside the envelope body is ignored:
it is not a §10.2 field.

Schema versioning: a missing `schema_version` defaults to 1; any
other version is rejected (`EVENT_UNSUPPORTED_SCHEMA_VERSION`). A
newer producer must never silently feed an older consumer.

## 2. The §10.3 pipeline

```
verify → inbox store → normalize → map → draft → validate
  → policy gate → post-or-draft → retry / dead-letter queue
```

| Step | What happens | Failure |
|---|---|---|
| 1. Verify | Envelope validation + the alongside event type (`X-HesabYar-Event-Type`, one of the eleven §10.2 types) + HMAC-SHA256 signature when the store has a secret configured (hex or base64; timing-safe compare). No secret → the check is skipped and the skip is recorded on the ingest response. | 422 / 401 |
| 2. Inbox store | Idempotency key claimed first (§10.4 constraint 2), then the inbox row under a transaction-scoped advisory lock on the source-event identity (§10.4 constraint 1, unique constraint as backstop). | 409 Conflict (same key, different hash) |
| 3. Normalize | Money fields converted to whole Rial at the connector boundary (§5 below). The raw envelope is never rewritten. | `needs_review` (permanent) |
| 4. Map | Versioned account/company/journal mapping resolved. | `needs_review` — never a default account (§10.5) |
| 5. Draft | A balanced engine journal draft is created. | Transient → retry; permanent → review |
| 6. Validate | Read-only engine validation of the draft. | `needs_review` |
| 7. Policy gate | Three-layer gate (event type, amount cap, period lock). | Denied → draft only, never posted |
| 8. Post-or-draft | Allowed → post; denied → leave as draft. The external-entity mapping (§10.4 constraint 3) is saved. | Transient → retry |
| 9. Finish | Decision recorded in the audit log (§6), an `events.processed` outbox event emitted, inbox marked `processed`. | — |
| 10. Retry / DLQ | Transient failures retry with bounded exponential backoff (60s, doubling, cap 3600s, max 5 attempts), then dead-letter. | DLQ → manual replay (§7) |

Exactly-once: before creating a draft, the pipeline checks the
external-entity mapping — a reprocessed event whose engine record
already exists is marked processed without a second draft. A
reprocessed **already-processed** event is a no-op. The residual
window is an engine draft created but the HesabYar transaction lost
(no engine-side idempotency key exists for drafts): the retry creates
a second draft. This window is inherent, not hidden — the review
queue + audit log make it visible.

Retryable vs permanent (`application.commands.events.is_retryable`):
mapping/envelope/amount failures and non-retryable application
errors are permanent (fail closed to review immediately); engine
unavailability (`retryable=True`) and unexpected exceptions are
transient (retried, then dead-lettered). An unknown failure is
treated as transient — the DLQ is the backstop, never silent loss.

## 3. Idempotency (spec §10.4)

Three uniqueness constraints, all enforced in Postgres:

1. `uq_inbox_events_source_event` — (source_system, source_store_id, source_event_id).
2. `uq_idempotency_records_installation_operation_key` — (installation, operation, key).
3. `uq_external_id_map_source_target` — (source_system, external_entity_type, external_entity_id).

Plus a concurrent-duplicate lock: ingest claims the idempotency key
first, and the inbox store takes `pg_advisory_xact_lock` on the
source-event identity. A duplicate re-send with the same hash replays
the stored event (200); the same key with a different hash is a 409.

TTL: a pending claim expires after 10 minutes (a stale pending claim
is released and re-claimed); a completed event record is kept for
**30 days** (`IDEMPOTENCY_TTL`) — store webhooks retry for days, not
minutes. Expired records are purged by maintenance
(`EventIdempotencyPort.purge_expired`).

## 4. Inbox / outbox / worker

- **Inbox** (`inbox_events`): statuses `received`, `processing`,
  `processed`, `failed`, `needs_review`, `dead_lettered`. Claims use
  `FOR UPDATE SKIP LOCKED`, so concurrent workers never collide; a
  `processing` claim older than 10 minutes (`CLAIM_TIMEOUT`) may be
  reclaimed by another worker.
- **Outbox** (`outbox_events`): result events (`events.processed`)
  written in the same transaction as the inbox update. The outbox —
  not the queue — is the durable record. The in-process publisher
  dispatches to registered handlers; an event type with no handlers
  publishes to zero consumers (returns True).
- **Worker**: Procrastinate (MIT, Postgres-native, no Redis) is the
  trigger/scheduler only. Tasks: `events.process_inbox_event`
  (one-shot, deferred on ingest/replay) and `events.sweep`
  (periodic, every minute, `* * * * *`). Deferring is best-effort:
  a queue failure is logged at warning and never fails an ingest —
  the sweep is the backstop. The Procrastinate schema is managed by
  Procrastinate itself (`app.schema_manager.apply_schema()`), NOT by
  Alembic. Run the worker with `scripts/run_event_worker.py`
  (`--once` for a single sweep, `--skip-schema` when the schema is
  already applied). The REST process uses a defer-only app whose
  tasks raise if they somehow execute there.
- **Staleness**: an event stuck in `received`/`processing` longer
  than N hours (default 24) raises the alert
  (`GET /api/v1/integrations/inbox/stale`).

## 5. Currency: Toman→Rial at the connector boundary

ADR 0003 decision 4 + spec §19 rule 7: inside the system everything
is whole Rial; the conversion happens once, at the connector
boundary, and is explicit, tested, and never silent.

- Rate: exactly 10 (`TOMAN_TO_RIAL_RATE`). `175` Toman → `1,750` Rial.
- Per-store setting (`connector_store_settings`): `prices_in_toman`
  (default True for an Iranian store) + `currency` (default `IRR`).
  Managed at `GET/PUT /api/v1/integrations/stores/{source_system}/{source_store_id}/settings`.
  This narrow table owns only the conversion setting; the F3 store
  registry owns the full store record (API keys, webhook secrets).
- Rounding: a Toman amount that does not convert to a whole Rial
  rounds HALF_UP (0.05 Toman = 0.5 Rial → 1 Rial) and the rounding
  evidence (`exact_rial`, `was_rounded`, `rounding_delta_rial`) is
  recorded on the normalized event. A store claiming Rial prices
  must send whole Rial amounts — a fractional Rial amount is a data
  error, never a silent rounding.
- Money fields are converted per event type
  (`MONEY_FIELDS_BY_EVENT_TYPE`, dot paths, `[]` iterates lists).
  A missing or null money field is skipped — null means "unknown"
  (e.g. an unreported gateway fee), never zero.
- Floats, bools, NaN, and unparseable strings are rejected. Persian
  and Arabic-Indic digits are normalized in string amounts.

## 6. Automation decision audit log

Every auto-post/draft/review/dead-letter/replay decision is appended
to `automation_decision_log` with its **policy version** and
**mapping version** (plus gate results, actor, correlation id, and
reason details). `policy_version` is `"n/a"` when no gate ran
(review/dead-letter/replay decisions). The log is append-only and
readable at `GET /api/v1/integrations/audit-log`.

## 7. DLQ replay

A dead-lettered event is replayed manually from the UI:
`POST /api/v1/integrations/inbox/{event_id}/replay`. The replay
keeps the event's idempotency key and source identity and resets
attempts to 0, so reprocessing has no duplicate effect (the §10.4
constraints + the external-entity mapping suppress it). The replay
is recorded in the audit log (`REPLAYED`, with the actor and the
previous attempt count). Only dead-lettered events can be replayed
(otherwise 409 `EVENT_NOT_DEAD_LETTERED`).

## 8. REST API

All under `/api/v1/integrations` (RFC 9457 problem envelope on
errors). Reads need `integrations:read`, mutations
`integrations:manage`.

| Endpoint | Purpose |
|---|---|
| `POST /integrations/{connector}/events` | Ingest one event (raw JSON body = the 15-field envelope; required `X-HesabYar-Event-Type` header — a canonical type or a store webhook topic, see [store-connector.md](store-connector.md); `X-HesabYar-Signature` when the store has a secret). 201 created / 200 duplicate / 409 conflict / 422 invalid (incl. `EVENT_TYPE_PENDING_ADR_APPROVAL` for topics whose canonical names need ADR approval) / 401 bad signature / 503 vault unavailable. `refund.created` fans out to two inbox rows; the response returns the derived ids in `derived_inbox_event_ids` |
| `GET /integrations/inbox` | List inbox events (status/source/event-type filters, cursor pagination) |
| `GET /integrations/inbox/stale` | Staleness alert (`?hours=24`) |
| `GET /integrations/inbox/{event_id}` | One inbox event |
| `POST /integrations/inbox/{event_id}/replay` | Manual DLQ replay |
| `GET /integrations/outbox` | List outbox events |
| `GET /integrations/audit-log` | List automation decisions |
| `GET/PUT /integrations/stores/{source_system}/{source_store_id}/settings` | Per-store Toman setting |
| `POST /integrations/worker/poll` | Run one worker sweep now (ops endpoint) |

## 9. Seams for F2 / F3 / F4

F1 defines the domain ports
(`hesabyar.domain.events.ports`) and ships fail-closed defaults
(`hesabyar.infrastructure.events.defaults`); the later streams plug
in behind the same ports. In production with F1 alone, every event
routes to `needs_review` — correct per §10.5 (unknown mapping never
falls back to a default account). US-511 (Phase 5-2, stream S1)
filled the three F3 seams marked below with the store
implementations; the inbox-backed review queue stays until F4
replaces it.

| Port | F1 default | Replaced by |
|---|---|---|
| `MappingResolverPort` | `FailClosedMappingResolver` (always raises `MappingUnresolvedError`) | US-511 `StoreMappingResolver` (registry + id map + chart) |
| `DraftBuilderPort` | `FailClosedDraftBuilder` (always raises) | US-511 `StoreDraftBuilder` (invoice / cancel / credit note / payment) |
| `PolicyGatePort` | `DraftOnlyPolicyGate` (never auto-posts; version `draft-only-v1`) | US-511 `RegistryPolicyGate` (US-503 `PolicyGateService`: settings opt-in + policy row + cap + open period + channel contract) |
| `ReviewQueuePort` | `InboxReviewQueueAdapter` (the inbox row IS the review item) | F4 review queue |
| `WebhookSecretPort` | `NoWebhookSecretStore` (no secret → signature check skipped + recorded) | US-511 `RegistryWebhookSecretStore` (registry + vault, fail-closed 503) |
| `AccountingEnginePort` | Real Tryton adapter (unused while mappings fail closed) | F2 extends it (sale/invoice/payment) |
| `OutboxPublisherPort` | `InProcessOutboxPublisher` (zero handlers by default) | Consumers subscribe as modules land |
| `JobQueuePort` | `ProcrastinateJobQueue` (production) / `InMemoryJobQueue` (tests) | — |

The pipeline dispatches on the document kind the builder returns
(US-511): journal draft, sale, invoice, credit note, payment, or
invoice cancellation — each created, validated and (gate allowing)
posted through its matching engine operation. Payments are never
auto-posted (settlement confirms the deposit first); cancellations
are terminal.

## 10. What F1 deliberately does NOT do

- ~~`POST /integrations/{connector}/sync` (polling-based sync) is 5ب
  scope, not F1 — the inbox/outbox/queue are ready for it.~~
  **Built in US-512 (S2):** `POST /integrations/{connector}/sync`
  (scope `integrations:manage`) polls the generic store contract
  (changes API or read-only store-DB read) and ingests each change
  through `EventIngestService.ingest_verified` with the SAME
  idempotency key as the webhook path, so a missed webhook is
  filled without a duplicate accounting effect. The sync service
  (`application/commands/sync.py`) reports per-run counts, partial
  runs with resume cursors, and per-event errors; `SyncBatch`
  carries the type-carrying shape (`SyncedEvent` = canonical event
  + canonical type) this platform deferred in F1. Full contract:
  `docs/store-connector.md`.
- Per-type payload required-field validation beyond envelope +
  money-field parseability was deferred to the 5ب store contract —
  US-511 built it (see [store-connector.md](store-connector.md) §5).
- No browser automation / RPA / screen-scraping as a product path
  (ToS + computer-crimes law); no-API sources use store-side
  capture + expected-settlement scheduling + automatic bank matching.
- No hard-coded gateway fee % or channel lists — fees are entered
  manually per channel/contract (a rate change is a new versioned
  contract; past settlements keep the old rate).
- Fintech inquiry results are ephemeral: store the verdict/code/time,
  never the raw PII.

## 11. Manual entries — dual entry (US-513, Phase 5 wave 2, stream S3)

A manual order, refund, or cancellation produces the **same** 15-field
canonical envelope (§1) a store webhook would produce and is ingested
through the **same** `EventIngestService` and the **same** §10.3 pipeline
(§2) — validation, mapping, policy gate, audit log. There is no second
pipeline: manual entry is a second *source* for the one pipeline, not a
parallel implementation. This is ADR 0003 decision 1 (dual entry) and
the pattern every later module reuses: anything entered by hand travels
the pipeline exactly like its automatic twin.

| Manual entry | Canonical event(s) produced |
|---|---|
| Order | `sales.order.completed` |
| Refund (with payment) | `sales.refund.created` + `payment.refunded` |
| Refund (without payment) | `sales.refund.created` |
| Cancellation | `sales.order.cancelled` |

Only the eleven canonical event types (§1) — no new event name.

### 11.1 Envelope construction

- `source_system` is always `"manual"`. `source_store_id` defaults to
  `"manual"`; the operator may name a store, which the mapping center
  resolves to a company via the `store_company` mapping kind.
- `correlation_id` is the manual-entry id, so a refund's two events
  stay tied together and the entry traces to every event it produced.
- `source_event_id` is `manual-<uuid hex>` per event — a manual entry
  has no external source id of its own.
- Money in the manual-entry request and response is whole Toman
  (`int`); the pipeline converts ×10 to Rial at the connector boundary
  exactly like a store webhook (§5). A line total that is not a whole
  Toman is rejected with the field named, so manual events never
  trigger the pipeline's HALF_UP rounding.
- Quantities are exact Decimals; payloads carry a quantity as an int
  when whole else a decimal string — never a float (the currency module
  rejects floats).
- The payload carries `entry_source: "manual"` and `manual_entry_id`
  (event → entry traceability); the `manual_entries` row carries the
  produced events (entry → inbox event → pipeline decision → engine
  record, via the §10.4 external-id map).

### 11.2 External-entity types

The §10.4 exactly-once pre-check keys on
`(tenant, source_system, external_entity_type, external_entity_id)`,
so the entity vocabulary is chosen so a legitimate follow-up is never
suppressed as a re-processed event:

| Entry | Entity type | Entity id |
|---|---|---|
| Order | `order` | the order number — a second entry of the same number is correctly duplicate-suppressed |
| Refund | `refund` | the refund reference (generated `manual-refund-<12 hex>` when omitted) |
| Refund payment leg | `payment_refund` | the same refund reference |
| Cancellation | `order_cancellation` | the order number — distinct from the order itself, so cancelling a processed order is never mistaken for re-processing it |

### 11.3 REST API

| Endpoint | Scope | Notes |
|---|---|---|
| `POST /manual-entries/orders` | `integrations:manage` | 201; **required** `Idempotency-Key` header — retries replay the original 201, a different payload under the same key is 409 |
| `POST /manual-entries/refunds` | `integrations:manage` | 201; `refund_payment` defaults to `true` (mirrors the S1 webhook mapping) |
| `POST /manual-entries/cancellations` | `integrations:manage` | 201 |
| `GET /manual-entries` | `integrations:read` | `entry_type` filter, sort, cursor pagination |
| `GET /manual-entries/{entry_id}` | `integrations:read` | Full per-event trace: inbox status, last error, `processed_at`, engine record id, latest audit decision |

Errors are RFC 9457: `MANUAL_ENTRY_INVALID` → 422 (domain validation —
empty lines, non-positive quantities, negative amounts, line discount
above the line total, order discount above the subtotal, a zero/negative
order total, refund lines not summing to the refund amount),
`MANUAL_ENTRY_NOT_FOUND` → 404.

### 11.4 Dashboard

The dashboard page is «فروش و خرید ← فاکتور جدید (دستی)»
(`/invoices/manual`, phased design §7): tabbed order/refund/
cancellation forms, an entries list with type filter and cursor
pagination, and a detail dialog showing the per-event trace. The page
goes through the BFF like every other dashboard page — no direct
browser-to-backend calls, no browser-held credentials.

### 11.5 Known limitation (shared, not US-513 scope)

Production wiring still uses the F1 fail-closed defaults
(`FailClosedMappingResolver`, `FailClosedDraftBuilder`,
`DraftOnlyPolicyGate`) — US-503 built the registries but did not wire a
registry-backed resolver/draft builder/gate into the pipeline. So in
production today, manual events route to `needs_review` exactly like
webhook events. The pipeline path is proven both ways in unit tests
(fail-closed → needs_review; resolving fakes → draft/post); wiring the
registries into the pipeline is a later story.
