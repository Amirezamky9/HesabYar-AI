# ADR-0005: The `installment.schedule.created` event — approved canonical name, derived inside HesabYar

- **Status:** Accepted
- **Date:** 2026-11-20
- **Phase:** 5-5 (۵ج — installments + BNPL), story US-540
- **Design:** `hesabyar-reference/phased-design-checklist-2026-11-17.md`
  §1.3 ۵ج (flow I1) + `hesabyar-reference/user-requirements-2026-11-18.md`
  batch 1 item 4 (installments)
- **Supersedes nothing; extends:** ADR 0003 round-2 update 8 (new event
  names beyond spec §10.2 require ADR approval) and ADR 0004 (the
  precedent for approving canonical names beyond §10.2's nine)

## Context

Spec §10.2 defines nine canonical event types; ADR 0004 added two
cheque intake types, bringing the canonical set to eleven. The
phased design (§1.3 ۵ج, flow I1) needs an installment schedule to
come into existence when a store order carries installment terms:
an installment order arrives as `sales.order.completed` with an
`installment` object (`installment_count`, `down_payment_amount`,
`installment_amount`, `interval`, `first_due_date` — validated by
`_validate_installment` in `hesabyar.domain.events.store_webhooks`
and carried through normalization with its money fields converted
to whole Rial). The schedule itself — the per-invoice legs with due
dates and principal amounts — is HesabYar's own record, built from
that order.

The open question, deferred from US-511/US-512 ("the event name
still needs ADR approval before implementation"), was whether the
schedule's creation is a canonical event at all, and if so under
which name. Two options were on the table:

1. **Approve a canonical event name** — `installment.schedule.created`
   — travelling in the 15-field envelope (§10.2) with the event type
   alongside in the `X-HesabYar-Event-Type` header (US-501 contract).
2. **Internal-only scheduling** — no canonical event; the schedule
   is created silently as a side effect of processing
   `sales.order.completed`, visible only in the plan tables.

The design already constrains the answer in one direction: the
event is **DERIVED inside HesabYar from `order.completed`** and is
**NOT a store webhook topic**. The store never sends it; no ninth
(or tenth) store topic is created. The question this ADR settles is
only whether the derived schedule creation is a canonical event
(option 1) or a silent side effect (option 2).

## Decision

**Option 1 — APPROVED.** `installment.schedule.created` is the
twelfth canonical event type.

### 1. The canonical name and what it means

| Canonical name | Meaning |
|---|---|
| `installment.schedule.created` | HesabYar derived an installment schedule (plan + legs) from a completed order's installment terms, or a plan was created that establishes the schedule. One event per plan. |

The canonical set is now twelve. The name is wired into
`CanonicalEventType` (`INSTALLMENT_SCHEDULE_CREATED`) by US-540,
and `parse_event_type` accepts it; the constants live in
`hesabyar.domain.events.installment_events`.

### 2. Derived inside HesabYar — never a store topic

- There is **no store webhook topic** for this event and no row in
  the `store_webhooks` topic→canonical table. The store speaks
  `order.completed` (with its `installment` object); HesabYar
  derives the schedule event from it, the same way `refund.created`
  fans out a derived `payment.refunded` (`derive_fanout_event`).
- The derivation itself is **not built by US-540** — US-540 creates
  plans through the REST API (`POST /api/v1/installments/plans`,
  `source="manual"`). The `order.completed` → schedule derivation
  lands in a later ۵ج story, which will emit this event with the
  envelope fields of §3 below. This ADR approves the name now so
  that story has a decided contract to build against, not an open
  question.
- Because the name is canonical, a store that sent it directly to
  the ingest endpoint would pass `parse_event_type` — but the
  store document builder has no handler for it (it is not in
  `STORE_DOCUMENT_EVENT_TYPES`), so it fails closed to the review
  queue exactly like the other non-store canonical types
  (settlement, purchase invoice, inventory cost, bank transaction).
  Nothing in the store contract changes.

### 3. The envelope + header contract (US-501, unchanged)

The event travels in the same 15-field envelope as every other
canonical event —

`id, schema_version, source_system, source_store_id, source_event_id,
occurred_at, received_at, company_mapping, currency,
external_entity_type, external_entity_id, correlation_id,
idempotency_key, payload, payload_hash`

— with the event type carried *alongside* in the required
`X-HesabYar-Event-Type` header (never as a field of the envelope;
an `event_type` key inside the body is ignored). `schema_version`
is 1; `payload_hash` is the SHA-256 of the canonical JSON of the
payload. For `installment.schedule.created` the envelope fields are:

- `source_system` = the HesabYar installation name (the event is
  produced inside HesabYar, not by a store);
  `source_store_id` = the store the order came from (when derived
  from an order; null for a manually created plan);
  `external_entity_type = "installment_plan"`;
  `external_entity_id` = the plan id;
  `correlation_id` = the order id (the same event's
  `sales.order.completed` correlation id for that order);
  `idempotency_key` = `installment-schedule:{order_id}` when
  derived from an order, `installment-schedule:manual:{plan_id}`
  for a manual plan;
  `source_event_id` = the derived id
  `{order_source_event_id}:installment-schedule` (the shared
  webhook-vs-poll derivation convention), so the §10.4
  source-event constraint sees the schedule as distinct from the
  order event it derives from.
- `payload` = the schedule summary: `plan_id`, `invoice_id`,
  `contract_id` (when set), `customer_ref`, `total_principal_irr`,
  `leg_count`, `interval_days`, `first_due_date` (Jalali
  `YYYY-MM-DD`), and `legs` — one entry per leg with `sequence`,
  `due_date` (Jalali), `principal_irr`. Amounts are whole Rial
  (Toman→Rial ×10 at the boundary, as everywhere); dates are
  Jalali in the payload, Gregorian in storage.

The §10.4 idempotency rules apply unchanged: re-deriving the same
order's schedule is idempotent OK (same idempotency key, same
plan); a different schedule under the same order is a conflict to
the review queue. The database unique constraint on
`(installation_id, invoice_id)` on `installment_plans` is the
backstop — one plan per invoice, ever.

### 4. Why not internal-only (option 2, rejected)

The schedule is a first-class business fact other parts of the
system consume: the cash calendar (۵ح) needs due legs, the dunning
jobs (US-541) need the schedule's existence, and the audit trail
needs the order→schedule derivation to be traceable as an event,
not reconstructable only from timestamps. An internal-only side
effect would leave each consumer to poll the plan tables and would
hide the derivation from the event log. A canonical name costs
one enum member and buys the envelope, idempotency, and traceability
the platform already provides. The "internal" half of the design
is preserved where it matters: no store topic, no store payload,
derivation inside HesabYar.

### 5. What this ADR does not decide

- The derivation trigger and handler (which pipeline step turns an
  `order.completed` with installment terms into a plan + this
  event) — a later ۵ج story.
- The leg-level lifecycle signals (`installment.leg.due`,
  `installment.leg.overdue`, …) — if the dunning jobs or the cash
  calendar need named signals, they are internal events in the ADR
  0004 §3 sense and need no new ADR; they are not decided here.
- The principal/interest split and finance-income recognition —
  US-541, documented in `docs/installments.md`.
- Whether a manually created plan (REST) emits this event at
  creation time — US-540 does not emit it (no producer is wired);
  the derivation story decides whether manual creation also emits,
  using the `installment-schedule:manual:{plan_id}` key above.

## Consequences

- The canonical event set grew 11 → 12 with US-540 (the name is
  approved and wired into the enum; no producer emits it yet).
- `docs/event-platform.md` and the `store_webhooks` module
  docstring now say twelve, and the store-webhooks docstring
  records that the derived event is still not built there — the
  name is approved, the derivation is a later story.
- The `order.completed` installment fields finally have a decided
  destination: the derivation story builds the plan and emits
  `installment.schedule.created` under this contract.
