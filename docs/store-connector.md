# Store connector (Phase 5-2 streams S1 + S2)

Two parts: **Part I** is the webhook receiver (S1, US-511) — the
primary path, a store pushing events into HesabYar; **Part II** is
reconciliation & backfill (S2, US-512) — polling that closes every
gap the webhooks miss. Both feed the same §10.3 pipeline through
the same idempotency keys, so a polled fill of a missed webhook
is a duplicate, never a second accounting effect.

The store→HesabYar contract: how a store (any platform — the
connector is generic, not Hayat-specific) pushes orders, refunds
and payments into HesabYar, and how HesabYar turns them into
Tryton documents. Built on the [event platform](event-platform.md)
(US-501): the 15-field envelope, the §10.3 pipeline, the §10.4
exactly-once rules.

Source of truth for the design: the phased design §1.3 (5ب) and §2
(§1.2–§1.6), ADR 0003, spec §10. Implementation:
`hesabyar/domain/events/store_webhooks.py` (mapping + validation),
`hesabyar/application/commands/store_events.py` (resolver +
builder), `hesabyar/infrastructure/events/store_secrets.py`
(HMAC secret store).

## Part I — Webhook receiver (US-511, S1)

## 1. The 8 store webhooks

The store sends one webhook per event to
`POST /api/v1/integrations/{connector}/events` with the event type
in the required `X-HesabYar-Event-Type` header. The header may carry
a canonical type (the US-501 contract) or a store webhook topic,
which maps to canonical types before ingest:

| Store webhook topic | Canonical event(s) | Status |
|---|---|---|
| `order.completed` | `sales.order.completed` | ✅ live |
| `order.cancelled` | `sales.order.cancelled` | ✅ live |
| `refund.created` | `sales.refund.created` + `payment.refunded` | ✅ live (fan-out, §4) |
| `payment.captured` | `payment.captured` | ✅ live |
| `payment.refunded` | `payment.refunded` | ✅ live |
| `cheque.registered` | `cheque.received.registered` | ✅ live (US-533) |
| `cheque.cancelled` | `cheque.received.cancelled` | ✅ live (US-533) |
| `bank_transfer.recorded` | `payment.bank_transfer.recorded` (proposed) | ⛔ 422 — needs ADR approval |
| `shipment.dispatched` | `inventory.packaging.consumed` + `shipment.cost.charged` (proposed) | ⛔ 422 — needs ADR approval |

Pending-ADR topics are rejected with
`EVENT_TYPE_PENDING_ADR_APPROVAL` (422) — never ingested under an
unapproved name. Only the 11 canonical types of spec §10.2 (nine
from US-501 plus the two cheque names from ADR 0004 / US-533) may
be used; any new type name needs ADR approval first.

### Cheque intake (US-533, §5 §1.2)

`cheque.registered` carries the 24-field registration payload (14
mandatory + 10 optional incl. `installment_leg_id`), validated
fail-closed at the adapter boundary (`sayad_id` exactly 16 digits,
`issuer_national_id` 10/11 digits, `amount_toman` > 0, `due_date` ≥
`issue_date`, `purpose` payment|collateral only); Toman→Rial ×10
happens in the same validation. The payee's national id must match
the mapped company (its Tryton party `code`) — a cheque payable to
another business is rejected to the review queue, never created.
`sayad_registered=false` sets the `sayad_unconfirmed` flag and the
"do not accept an unregistered cheque" warning (ruling 870).

The intake is a CLAIM: state `registered`, source `site`, NO
journal — the first journal is issued only at `registered → held`,
when the leaf has arrived and Sayyad registration is confirmed.
Exactly one HesabYar cheque per site registration (DB partial
unique index on `sayad_id`): an identical re-registration is
idempotent OK; a different payload files a `duplicate_sayad_id`
review item. A site-registered cheque later entered manually
converges to the same record. `cheque.cancelled` voids the intake
before `held`, and files a `cancellation_after_held` review item
after it (manual return, never automatic void). A scan at
`scan_url` is downloaded into the filestore best-effort (never
fails the intake); the Sayyad leaf's QR code is the site-side
`sayad_id` capture (no typing errors) — HesabYar validates the 16
digits fail-closed and stores nothing QR-specific. Polling
(`cheque.registered` / `cheque.cancelled` via S2) and the webhook
share the store event id, so a redelivery dedups in ingest.

**One webhook per payment** (store→HesabYar contract): each payment
the store takes produces exactly one `payment.captured` webhook,
and each refund exactly one `refund.created` (which fans out) or
one `payment.refunded`.

## 2. HMAC verification

When the store has a webhook secret (created with the store in the
registry, shown to the operator exactly once), the webhook MUST be
signed: `X-HesabYar-Signature` = HMAC-SHA256 of the raw request
body with the secret, hex or base64. Missing signature → 401;
wrong signature → 401 (`EVENT_SIGNATURE_INVALID`).

The secret is looked up installation-scoped from the store
registry and decrypted through the credential vault
(`RegistryWebhookSecretStore`). Fail-closed: a store WITH a secret
whose ciphertext is missing, whose vault is unconfigured, or whose
secret cannot be decrypted fails ingest with
`EVENT_WEBHOOK_VAULT_UNAVAILABLE` (503) — never silently skipping
verification. A store with no secret configured skips the check
and records the skip in the ingest result.

One secret per connector (keyed by `source_system`); per-store-id
secrets inside one connector are a follow-up if multi-store
connectors need them.

## 3. The envelope

Exactly the 15 §10.2 fields (see `event-platform.md` §1) — the
event type travels alongside the envelope, never inside it. The
envelope's `source_system` must match the `{connector}` path
parameter. Amounts in the payload are in **Toman** by default
(per-store `prices_in_toman` setting); the connector boundary
converts them to whole Rial (×10) during normalization, with
rounding evidence per field. Installment amounts are converted
too (§9).

Every store event must identify its OWN entity in the envelope
(`external_entity_type` / `external_entity_id`) — the §10.4
exactly-once key:

| Event | Entity |
|---|---|
| `sales.order.completed` | `order` / `order_id` |
| `sales.order.cancelled` | `order_cancellation` / `order_id` |
| `sales.refund.created` | `refund` / `refund_id` |
| `payment.captured` | `payment` / `gateway_payment_id` |
| `payment.refunded` | `payment_refund` / `refund_id` |

A missing or mismatched entity fails closed to the review queue.
The cancellation's entity is the ORDER being cancelled (the
store's `cancellation_id` is payload metadata, not the entity):
one cancellation per order, and a late completion is detectable
through the same mapping (§8).

## 4. Fan-out: `refund.created`

A refund is two facts — the sale is refunded AND money goes back
to the customer — so one webhook ingests TWO canonical events:

- `sales.refund.created` (the primary leg): entity `refund` /
  `refund_id`, the webhook's own `source_event_id`.
- `payment.refunded` (the derived leg): entity `payment_refund` /
  `refund_id`, `source_event_id` = `{original}:payment-refunded`,
  a derived idempotency key, the same payload and payload hash.

The `:payment-refunded` suffix is the shared webhook-vs-poll
convention (US-512's `domain/events/sync.py` derives the same id
for the same refund), so a refund that arrives by webhook and is
later re-polled dedups on both legs instead of ingesting the
payment leg twice. The derivation is deterministic: a re-sent
webhook re-derives the same events and replays as duplicates. Both legs are stored in the
same request transaction — a failure anywhere rolls the whole
webhook back. The response returns the derived inbox ids in
`derived_inbox_event_ids`.

The derived payment leg needs `gross_amount` in the refund payload
(it builds the payable payment from it). A refund payload without
`gross_amount` still ingests both legs, but the payment leg fails
closed to review.

## 5. Payload contracts

Validated in the document builder (after money conversion), so a
contract violation routes the event to the review queue with the
field named — never guessed. All amounts are whole-Rial integers
after conversion; quantities are exact Decimals; a `line_total`,
when present, must equal quantity × unit price.

### `sales.order.completed`

```json
{
  "order_id": "order-1",
  "customer": {"id": "cust-1", "email": "a@b.ir", "phone": "+98..."},
  "lines": [
    {"product_sku": "SKU-1", "quantity": 2, "unit_price": 100000,
     "line_total": 200000, "description": "...", "revenue_category": "electronics"}
  ],
  "subtotal_amount": 250000,
  "discount_amount": 10000,
  "shipping_amount": 30000,
  "tax_amount": 5000,
  "total_amount": 275000,
  "status": "completed",
  "installment": {"installment_count": 4, "down_payment_amount": 75000,
                  "installment_amount": 50000, "interval": "monthly",
                  "first_due_date": "1405-09-01"}
}
```

Required: `order_id`, `customer` (with `id`), non-empty `lines[]`
(`product_sku`, `quantity` > 0, `unit_price` ≥ 0), `total_amount`
> 0. Consistency: `subtotal_amount` (when present) = line sum;
`total_amount` = lines + shipping − discount + tax. `status`,
when present, must be a completed-equivalent status
(`completed` / `paid` / `fulfilled` / `done`).

### `sales.order.cancelled`

```json
{"order_id": "order-1", "reason": "customer request",
 "total_amount": 275000, "cancelled_at": "2026-11-18T10:00:00Z",
 "cancellation_id": "cancel-9", "refund_expected": true}
```

Required: `order_id`. Everything else optional.

### `sales.refund.created`

```json
{"refund_id": "refund-1", "order_id": "order-1", "refund_amount": 50000,
 "reason": "...", "customer": {"id": "cust-1"},
 "gateway_payment_id": "pay-1", "gross_amount": 50000,
 "payment_channel": "zarinpal",
 "lines": [{"product_sku": "SKU-1", "quantity": 1, "line_total": 50000}]}
```

Required: `refund_id`, `order_id`, `refund_amount` > 0,
`payment_channel` (the fan-out payment leg resolves its journal
from the channel; a cash refund uses a custom `"cash"` channel in
the registry). `gross_amount` is optional — see §4.

### `payment.captured`

```json
{"payment_channel": "zarinpal", "gateway_payment_id": "pay-1",
 "gross_amount": 275000, "fee_amount": 5000, "fee_tax_amount": 0,
 "net_amount": 270000, "fee_rate_snapshot": 2.5,
 "order_id": "order-1", "customer": {"id": "cust-1"}, "verified": true}
```

Required: `payment_channel`, `gateway_payment_id`,
`gross_amount` > 0. `customer` is optional: when the payload names
the order, the builder falls back to the order's invoice party.
`verified`, when present, must be `true` — a payment webhook is
sent only after a successful gateway Verify. Fee fields are
carried (fee accounting lands with the settlement engine, 5پ);
`fee_rate_snapshot` is a percentage in [0, 100].

### `payment.refunded`

```json
{"refund_id": "refund-1", "gross_amount": 50000, "fee_amount": 0,
 "net_amount": 50000, "gateway_payment_id": "pay-1",
 "order_id": "order-1", "payment_channel": "zarinpal"}
```

Required: `refund_id`, `gross_amount` > 0, `payment_channel`.

## 6. Order-status rules

The `status` field of an order payload classifies how HesabYar
treats the order (case-insensitive, `-`/`_` equivalent):

| Status | Action |
|---|---|
| `completed`, `paid`, `fulfilled`, `done` | Create the document |
| `pending`, `on-hold`, `processing`, `awaiting-payment`, `awaiting-fulfillment` | Ignore (not final yet) |
| `cancelled`, `canceled` | Cancel |
| `refunded`, `partially-refunded` | Refund |
| anything else / missing / blank | Review (never guessed) |

The webhook topics carry the transition, so these rules classify
the `status` field inside the payload — an `order.completed`
payload whose status is not completed-equivalent is rejected as
contradicting its topic.

## 7. Customer / product matching

- **Customer**: the external customer id is the primary key,
  resolved through the mapping center's `external_party` kind.
  Email and phone are recorded only as fallback hints for the
  operator — matching by contact alone is never done silently,
  so an unmatched customer fails closed to review.
- **Product**: SKU → §10.4 external-id map
  (`(source_system, "product", sku)` → Tryton product). Products
  are resolved (for the Path A sale migration) but do NOT block
  the invoice path — invoice lines are account-based. Products
  are never auto-created from a webhook.

## 8. What the pipeline builds

The pipeline dispatches on the document kind the builder returns
(journal draft, sale, invoice, credit note, payment, invoice
cancellation). Draft is the default; auto-post is an explicit,
reversible opt-in per installation.

### Completed order → customer invoice draft (Path B)

The invoice carries the full total (products + shipping) via
account-mapped lines: each line's revenue account comes from the
`revenue_category` mapping (keyed by the line's category, or
`default`); shipping from the `shipping_charge` mapping. The
invoice total is products + shipping — **tax and discount are
validated for arithmetic consistency but NOT posted as lines**
(no tax account is resolvable from the nine §10.5 kinds, and
invoice drafts cannot carry negative discount lines). The gap is
recorded in the invoice description and the audit log, so the
reviewing human sees exactly what is missing. The invoice is a
draft, human-reviewed before posting.

The invoice reference is `{source_system}:{order_id}` and the
description names the source event — full traceability to the
webhook. The §10.4 mapping ties the order to its invoice.

Path A (Sale → Invoice) is the later migration: the canonical
event and the mapping center are designed for both paths from the
start, and product resolution becomes blocking then.

### Cancellation

- Draft/validated invoice → `CancelInvoice` (the invoice keeps its
  history and can never be posted).
- Posted/paid invoice → a full credit note (corrective document —
  the posted invoice is never rewritten or deleted).
- Already-cancelled invoice → review.

### Refund

- Draft/validated invoice + full refund → cancel the invoice.
- Draft/validated invoice + partial refund → review (manual
  amendment of the draft).
- Posted/paid invoice + full refund → full credit note (every
  line mirrored).
- Posted/paid invoice + partial refund → amount-based partial
  credit note (a single line for the amount against the source's
  revenue account — requires all priced source lines to share one
  account; the VAT split follows the tax-line work).
- Refund exceeding the invoice total → review.
- Refund of a cancelled invoice → review.

### Payments

- `payment.captured` → a `receivable` payment draft; the journal
  comes from the `gateway_journal` mapping for the channel.
- `payment.refunded` → a `payable` payment draft.
- The party comes from the payload customer, falling back to the
  linked order's invoice party.
- The payment is an **unlinked draft** (no invoice line, no
  account yet — the invoice may not exist when the payment
  arrives); settlement (5پ) links it.
- **Payments are never auto-posted**, even when the gate allows:
  a captured payment is money the gateway owes, not money
  received — the draft waits for settlement to confirm the
  deposit.

### The policy gate (draft is the default)

The pipeline's `PolicyGatePort` is the registry-backed US-503 gate
(`RegistryPolicyGate` over `PolicyGateService`), wired in
`dependencies.py` and `run_event_worker.py`. Auto-post is allowed
only when ALL layers pass:

1. the installation opted in (`InstallationSettings.auto_post_enabled`);
2. the event type has an explicit policy row with `allowed=True`;
3. the amount is within the cap (the stricter of the policy row's
   `max_amount_irr` and the channel contract's
   `auto_post_max_amount_irr` when the event carries a channel);
4. the event's fiscal period is open in Tryton;
5. when the event carries a `payment_channel`, that channel exists,
   is enabled, and holds an active contract.

Anything missing denies → the document stays a draft for a human.
The gate never routes to review by itself; review only happens on
mapping gaps, invalid payloads, or out-of-order events. A policy
that requires a settlement tie-out denies until the tie-out exists
(store events carry none — that arrives with 5پ).

### Out-of-order events

- **Completed after cancelled** → review. The exactly-once
  pre-check lets a late completion through when the order's
  invoice was cancelled or a cancellation mapping exists, and
  the builder's guard raises with the reason. No second invoice
  is ever created.
- **Cancel / refund before the order** → review (the order event
  may not have arrived yet; a fully automatic pending-state
  registry is US-512 territory).
- **Second cancellation of the same order** → suppressed as a
  duplicate (the cancellation entity is the order).
- **Duplicate re-send** → suppressed as a duplicate (the §10.4
  pre-check), no second document.

## 9. Installments

Installment fields inside `order.completed`
(`installment_count`, `down_payment_amount`,
`installment_amount`, `interval`, `first_due_date`) are validated
and carried (amounts converted to Rial) but NOT acted on: the
derived `installment.schedule.created` event needs ADR approval
first, and installment handling lands in phase 5ج.

## 10. Mapping center setup

The resolver needs these mappings (installation-scoped):

| Kind | Channel | Source key | Target |
|---|---|---|---|
| `store_company` | — | `{source_system}:{source_store_id}` | `company` / engine id |
| `external_party` | — | external customer id | `party` / engine id |
| `revenue_category` | — | revenue category (or `default`) | `account` / code or engine id |
| `shipping_charge` | — | shipping method (or `default`) | `account` / code or engine id |
| `gateway_journal` | channel key | channel key | `journal` / engine id |

Account targets may be a chart code (`4.1.1`) or an engine id
(`42`, `tryton:account:42`); company/party/journal targets are
engine ids. Every gap is collected and reported at once, so one
review item lists everything the operator must map.

## 11. Error codes

| Code | HTTP | Meaning |
|---|---|---|
| `EVENT_UNKNOWN_TYPE` | 422 | Unknown header value (canonical or topic) |
| `EVENT_TYPE_PENDING_ADR_APPROVAL` | 422 | Topic whose canonical name needs ADR approval |
| `EVENT_INVALID_ENVELOPE` | 422 | Envelope / payload contract violation |
| `EVENT_SIGNATURE_INVALID` | 401 | Missing or wrong HMAC signature |
| `EVENT_WEBHOOK_VAULT_UNAVAILABLE` | 503 | Store has a secret that cannot be decrypted |
| `EVENT_MAPPING_UNRESOLVED` | 422 | Mapping gap (also the review-queue reason) |
| `IDEMPOTENCY_CONFLICT` | 409 | Same source event id, different payload hash |

## 12. Deferred (not in US-511)

- Path A (Sale → Invoice) migration.
- Discount lines and tax lines on order invoices.
- Partial refund of a draft/validated invoice (manual amendment).
- Cheque webhooks are LIVE since US-533 (ADR 0004):
  `cheque.registered` → `cheque.received.registered`,
  `cheque.cancelled` → `cheque.received.cancelled`.
- Bank-transfer / shipment webhooks (pending ADR).
- Derived `installment.schedule.created` event (pending ADR).
- Multi-store-per-connector webhook secrets.
- Gateway fee accounting (with the settlement engine, 5پ).
- `POST /integrations/{connector}/sync` polling sync (5ب, US-512).

---

## Part II — Reconciliation & backfill (US-512, S2)

The generic (custom-coded store) contract: how any store — the
custom-coded Hayat store first — connects to HesabYar for
reconciliation and backfill. The webhook receiver (S1, US-511) is
the primary path; this document covers the polling side (S2) that
closes every gap the webhooks miss.

Related: spec §10 (integration), §19 rule 2 (SQL ban — Tryton's DB
only) + rule 7 (money), ADR 0003 decisions 2–4, phased design §2
§1.1/§1.2/§1.5, `docs/event-platform.md`.

## II.1. The two transports

| Transport | When | Module |
|---|---|---|
| **HTTP changes API** (primary) | Always, when the store exposes it | `GenericHttpStoreConnector` |
| **Read-only store-DB read** | Backfill, or when the changes API is unavailable | `StoreDatabaseConnector` + `StoreDatabaseReader` |

Both implement `CommerceConnectorPort.fetch_changes` and both feed
the same `EventIngestService` path as webhooks. A missed webhook is
filled by polling with the SAME idempotency key — no duplicate
accounting effect (§II.4).

## II.2. HTTP changes API (the generic contract)

The store exposes one read-only endpoint:

```text
GET {api_base_url}/changes?updated_since=<ISO>&cursor=<opaque>&page_size=<int>
Authorization: Bearer <store read-only API key>
Accept: application/json
```

- `api_base_url` is the store's API base (e.g.
  `https://store.example.com/api/v1`); the path is always
  `/changes` under it.
- The API key is the store's scoped read-only key from the HesabYar
  registry (`hya_store_*`, generated by HesabYar, shown once at
  store creation/rotation, stored encrypted). The store team
  configures their changes API to accept it. The key can never
  write — its scopes are `orders:read` / `payments:read` /
  `cheques:read`.
- `updated_since` bounds the window from below (changes at or after
  it). `cursor` resumes a previous page (the store's own opaque
  value, passed through unchanged). `page_size` is 1–500
  (default 100).

Response (JSON):

```json
{
  "changes": [
    {
      "event_id": "evt-123",
      "event_name": "order.completed",
      "entity_type": "order",
      "entity_id": "order-42",
      "updated_at": "2026-11-18T09:00:00Z",
      "status": "completed",
      "payload": {"total_amount": 175000}
    }
  ],
  "next_cursor": "...",
  "has_more": false
}
```

### II.2.1 The store's obligations (the contract)

1. **Stable sort on `updated_at` ascending**, with a stable
   internal tiebreaker (the store's cursor encodes it). HesabYar
   VALIDATES non-decreasing `updated_at` and rejects an out-of-order
   page instead of ingesting it.
2. **The same `event_id` as the webhooks.** The `event_id` in a
   change row MUST be the id the store sends as `source_event_id`
   on the corresponding webhook — this is what makes polling dedup
   against webhooks (§II.4).
3. **Cancelled/deleted rows stay in the feed** with their status
   flag — never physically removed. A cancellation is a change, and
   HesabYar needs to see it.
4. **Money in the store's currency.** Amounts arrive as the store
   prices them; HesabYar converts Toman→Rial (×10) at the connector
   boundary during pipeline normalization, per the store's
   `prices_in_toman` setting. A missing or null money field means
   "unknown", never zero.
5. **No secrets in the feed.** Never send gateway passwords, full
   card numbers, CVV, or internet-banking credentials (phased
   design §2 §1.3).
6. **`has_more=true` MUST come with a `next_cursor`.** A page that
   reports more pages but gives no cursor to resume with fails the
   sync (fail-closed — the remaining pages must not be silently
   dropped).

### II.2.2 Event-name mapping

| Store `event_name` | Canonical event(s) |
|---|---|
| `order.completed` | `sales.order.completed` |
| `order.cancelled` | `sales.order.cancelled` |
| `refund.created` | `sales.refund.created` + `payment.refunded` |
| `payment.captured` | `payment.captured` |
| `payment.refunded` | `payment.refunded` |
| `cheque.registered` | `cheque.received.registered` (US-533) |
| `cheque.cancelled` | `cheque.received.cancelled` (US-533) |
| `bank_transfer.recorded` | *(unmapped — see §II.2.3)* |
| `shipment.dispatched` | *(unmapped — see §II.2.3)* |

A refund yields TWO canonical events from one change row: the first
(`sales.refund.created`) keeps the store event id; the second
(`payment.refunded`) appends the suffix `:payment-refunded`. The
webhook receiver (S1) MUST use the same convention, so a refund
that arrives by webhook and is later re-polled dedups on both
events.

### II.2.3 Unmapped changes (bank transfer / shipment today)

`bank_transfer.recorded` and `shipment.dispatched` have no
canonical event type yet — their canonical names
(`payment.bank_transfer.recorded`, …) need ADR approval before use
(ADR 0003 round-2 update 8). Their rows are LISTED by polling and
COUNTED in the sync report (`changes_unmapped`), never ingested
and never silently dropped. Cheque changes are NO LONGER unmapped:
`cheque.registered` / `cheque.cancelled` ingest since US-533 (ADR
0004) — a registration becomes a `registered` intake (no journal),
a cancellation voids the intake before `held` or files a
`cancellation_after_held` review item after it (§5 §1.2, §2.1).

## II.3. Read-only store-DB read (ADR 0003 decision 3)

Directly reading the store's own database is explicitly allowed —
read-only, reconciliation/backfill only. The spec's SQL ban covers
Tryton's database only; the store's database is the store's own
data, read with the store team's read-only credentials. The primary
path is always webhook + API polling.

### II.3.1 The v1 contract (what the store team provides)

The store database exposes ONE view, `hesabyar_changes` — a union
of the store's orders, payments and registered cheques, one row per
change:

| Column | Type | Meaning |
|---|---|---|
| `store_id` | text | Storefront id within the store database |
| `entity_type` | text | `order` / `payment` / `cheque` |
| `entity_id` | text | The order/payment/cheque id in the store |
| `event_id` | text | The store's event id (= webhook `source_event_id`); the keyset tiebreaker |
| `event_name` | text | Store-native webhook name |
| `status` | text | Entity status flag |
| `updated_at` | timestamptz | Last change; the stable-sort key |
| `payload` | jsonb / text | The event payload as a JSON object |

The store team owns the view definition (their schema is theirs);
HesabYar owns the query. `updated_at` is compared as ISO-8601
(SQLite: text; PostgreSQL: timestamptz — the reader binds ISO
strings, which PostgreSQL casts).

### II.3.2 Read-only by construction (three layers)

1. **The port has no write method.** `StoreDatabaseReaderPort`
   exposes only `fetch_changes` — there is no API surface that
   could write.
2. **The connection is read-only.** SQLite databases open with
   `mode=ro`; PostgreSQL connections start with
   `default_transaction_read_only=on`. The database itself rejects
   writes — proven in tests (`tests/unit/test_store_connectors.py`:
   INSERT / UPDATE / DELETE / CREATE / DROP through the reader's
   connection all fail with "attempt to write a readonly
   database"). The DB user itself must additionally hold
   SELECT-only grants (provisioned by the store team, ideally on a
   read replica, reachable only from the HesabYar server's IP);
   the connection setting is defense-in-depth, the grant is the
   real control.
3. **Only registered queries run.** The reader executes ONLY the
   versioned SELECTs in
   `hesabyar.infrastructure.store_connectors.queries`, each
   re-checked before execution (single SELECT/WITH statement, no
   `;`, no write verbs). There is no raw-SQL escape hatch.

### II.3.3 Versioned queries

Queries live under a version key (`v1` today). A store-side schema
change that breaks the view is a NEW query version, never an edit
of v1: old versions stay readable so an in-flight backfill never
breaks mid-run. An unknown version fails closed at reader
construction.

### II.3.4 Credentials

The read-only DSN is supplied by the store team and stored
encrypted (Fernet) in the store registry — never in `.env`, never
in git, never in a log or a response:

```text
POST /api/v1/integrations/stores/{store_key}/db-credentials
{"dsn": "postgresql://hesabyar_ro:<password>@<host>:5432/<store_db>"}
```

The response carries only `has_store_db_dsn: true`. Accepted
schemes: `postgresql://` (production) and `sqlite://` (dev/test
only). Env-var names for the DSN were deliberately NOT defined:
per-store credentials belong in the encrypted registry, not in a
shared `.env` (ADR 0003 decision 10, phased design §2.1).

## II.4. Idempotency: the same key on both paths

The envelope's default idempotency key is
`event:{source_system}:{source_store_id}:{source_event_id}`. The
webhook path and the polling path both derive it from the STORE's
event id, so the same business event ingests exactly once
whichever path delivers it first:

- Webhook arrives, then polling re-lists the change → the poll
  ingests as a **duplicate** (same key + same payload hash → 200,
  no second inbox row, no second draft).
- Webhook missed, polling fills it → the event is **created** once.
- Same key + different payload hash → **409 Conflict**, recorded
  on the sync report as a per-event error; the run continues with
  the next event (a conflict is a data mismatch to resolve, not a
  reason to drop the rest of the page).

Polled events travel the same pipeline as webhooks (validation,
mapping, policy gate, audit log) — the sync service calls
`EventIngestService.ingest_verified`, the post-verification half
of webhook ingest. No webhook HMAC applies to polled events: the
connector already authenticated server-to-server (scoped API key /
read-only DB user), and that is recorded on the ingest result.

## II.5. The sync endpoint

```text
POST /api/v1/integrations/{connector}/sync
Scope: integrations:manage
```

```json
{
  "source_store_id": "store-1",
  "updated_since": "2026-11-01T00:00:00Z",
  "backfill_days": 30,
  "cursor": "...",
  "page_size": 100,
  "source": "api",
  "max_pages": 10
}
```

- `updated_since` and `backfill_days` are mutually exclusive (422
  if both). With neither (and no `cursor`), the run looks back 7
  days. `backfill_days` is 1–365: the historical backfill on first
  connection (e.g. 30/90 days — the window is an install-time
  choice, per the phased design).
- `source` is `api` (default) or `db`.
- `max_pages` bounds one run (1–50, default 10); the response
  carries `next_cursor` + `has_more` so the operator (or a
  scheduler) resumes. A run that hits the bound reports
  `has_more: true`.

Response — a sync report:

```json
{
  "source_system": "hayat",
  "source_store_id": "store-1",
  "connector_kind": "api",
  "status": "completed",
  "pages_fetched": 3,
  "events_created": 24,
  "events_duplicate": 2,
  "changes_unmapped": 1,
  "events_by_type": {"sales.order.completed": 20, "payment.captured": 4},
  "errors": [],
  "error_count": 0,
  "error": null,
  "next_cursor": null,
  "has_more": false,
  "started_at": "...",
  "finished_at": "..."
}
```

**Partial runs.** A connector failure AFTER at least one page was
fetched ends the run as `partial`: the fetched pages stay ingested,
`error` records the failure, and `next_cursor` points at the
failed page so a retry resumes there. A failure on the FIRST page
propagates as a typed error (502 `STORE_UNREACHABLE` /
`STORE_SYNC_FAILED`) — nothing was fetched, so there is nothing to
keep. A failed run is always safe to retry from its cursor: the
§10.4 constraints dedup the already-ingested events.

**Per-event errors** (conflicts, malformed rows) are recorded in
`errors` (first 20; `error_count` is exact) and the run continues.
An unexpected per-event failure aborts the run — an unknown failure
must not be retried blindly inside the run.

After a run that fetched pages, the store's `last_poll_at` is
updated (visible on `GET /integrations/stores/{store_key}`).

## II.6. Multi-store isolation

One installation may host many stores. Every connector is bound to
ONE `(source_system, source_store_id)`; every change it returns
carries that identity; the sync service REFUSES a batch whose
events belong to another store (fail-closed, run aborted). The
store-DB query always filters by `store_id`, so one store's rows
never leak into another store's sync even when they share a
database. Two storefronts may reuse the same event ids — their
idempotency keys differ by `source_store_id`, so their events stay
separate inbox rows.

## II.7. Error taxonomy (REST problems)

| Code | HTTP | Meaning |
|---|---|---|
| `SYNC_INVALID_CURSOR` | 422 | The resume cursor is malformed or the wrong version |
| `SYNC_INVALID_REQUEST` | 422 | Bad page size / max pages / sync source / change row |
| `STORE_NOT_CONFIGURED` | 422 | The store lacks what the source needs (`api_base_url` + API key, or a stored DSN) |
| `STORE_UNREACHABLE` | 502 | The store's changes API cannot be reached (transport/timeout/5xx) |
| `STORE_SYNC_FAILED` | 502 | Malformed store response, out-of-order rows, cross-store events |
| `STORE_DB_ERROR` | 502 | The read-only store-DB read failed (connect/query/malformed row) |

## II.8. What this story deliberately does NOT do

- **No webhook receiver.** That is S1 (US-511). The connectors
  implement `verify_event`/`normalize_event` from the §10.1
  contract so the receiver can reuse them; the receiver itself
  (endpoint + HMAC + async processing) is US-511's.
- **No bank-transfer/shipment ingestion.** Their canonical
  event types need ADR approval (§II.2.3). Cheque ingestion is
  live since US-533 (ADR 0004; §II.2.3).
- **No per-type payload required-field validation** beyond the
  envelope + money-field parseability (deferred to the 5ب store
  contract — the pipeline fails closed to review on bad data).
- **No browser automation / RPA** as a product path (ToS +
  computer-crimes law); no-API sources use store-side capture +
  expected-settlement scheduling + automatic bank matching.
- **No scheduled polling.** The sync endpoint is operator-driven
  (and scheduler-ready: resume cursors + idempotent re-runs). A
  periodic reconciliation job (daily compare of store vs HesabYar,
  mismatches → review queue) is phased-design §2 §1.5 follow-up
  work, not this story.
- **No store-team-generated API key intake.** The registry only
  stores HesabYar-generated `hya_store_*` keys; a store that
  insists on issuing its own polling key needs a "set external
  key" endpoint (follow-up).
