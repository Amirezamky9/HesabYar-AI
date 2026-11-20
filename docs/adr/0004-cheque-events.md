# ADR-0004: Cheque event names — canonical intake events + internal cycle events

- **Status:** Accepted
- **Date:** 2026-11-19
- **Phase:** 5-4 (۵ث — cheque module), story US-530
- **Design:** `hesabyar-reference/phased-design-checklist-2026-11-17.md`
  §5 (§1.2, §2, §6) + §1.3 ۵ث
- **Supersedes nothing; extends:** ADR 0003 decision 7 (build
  `account_check_ir`) and ADR 0003 round-2 update 8 (new event names
  beyond spec §10.2 require ADR approval)

## Context

Spec §10.2 defines exactly nine canonical event types, carried in a
15-field envelope (§10.2) with the event type travelling *alongside*
the envelope in the `X-HesabYar-Event-Type` header (US-501 contract;
`hesabyar.domain.events.envelope`). The phased design (§5 §1.2) needs
cheque intake from the store site: a cheque registered on the site
must flow into HesabYar as a claim — an intake record with **no
journal** until the physical leaf arrives and Sayyad registration is
confirmed. The store-side webhook topic `cheque.registered` already
exists in `hesabyar.domain.events.store_webhooks` with
`pending_adr=True` and proposed target `cheque.received.registered`;
until this ADR it is rejected with `EVENT_TYPE_PENDING_ADR_APPROVAL`
(422), never ingested under an unapproved name.

The cheque lifecycle (§5 §2) also produces dashboard-facing signals
(held, in clearing, cleared, bounced, …) that the treasury dashboard
and the unified review queue need. Those signals are *internal*: they
are not store events, they never enter the store inbox, and they must
not be confused with the canonical contract.

## Decision

### 1. Two new canonical event types (beyond the nine in §10.2)

| Canonical name | Meaning |
|---|---|
| `cheque.received.registered` | A cheque was registered on the store site (or entered manually as the same claim). Intake only: creates one `account.check` record in state `registered` with **no accounting journal**. |
| `cheque.received.cancelled` | The site cancelled a cheque registration. Before `held` it voids the intake; after `held` it goes to the unified review queue (manual return, never automatic void). |

The canonical set is now eleven. The store webhook topic
`cheque.registered` maps to `cheque.received.registered` (and the
site's cancellation topic to `cheque.received.cancelled`).

### 2. The envelope + header contract (US-501, unchanged)

Both canonical cheque events travel in the same 15-field envelope as
every other canonical event —

`id, schema_version, source_system, source_store_id, source_event_id,
occurred_at, received_at, company_mapping, currency,
external_entity_type, external_entity_id, correlation_id,
idempotency_key, payload, payload_hash`

— with the event type carried *alongside* in the required
`X-HesabYar-Event-Type` header (never as a field of the envelope;
an `event_type` key inside the body is ignored). `schema_version`
is 1; `payload_hash` is the SHA-256 of the canonical JSON of the
payload. For `cheque.received.registered` the envelope fields are:

- `source_system` = the store installation name;
  `external_entity_type = "cheque"`;
  `correlation_id` = the order id (the same event's
  `sales.order.completed` correlation id for that order);
  `idempotency_key` = `cheque:{sayad_id}` (or the site's unique id);
  `external_entity_id` = the cheque record id on the store site.
- `payload` = the 24-field cheque payload (§5 §1.2: 14 mandatory +
  10 optional, incl. `installment_leg_id`); validated fail-closed
  at the adapter boundary (`sayad_id` exactly 16 digits,
  `issuer_national_id` 10/11 digits, `amount_toman > 0`,
  `due_date >= issue_date`, `purpose` payment|collateral only);
  Toman→Rial ×10 at the connector boundary; `payee_national_id`
  must match the mapped company; `sayad_registered=false` records
  the cheque with a `sayad_unconfirmed` flag and the explicit
  "do not accept an unregistered cheque" warning (ruling 870).

The §10.4 idempotency rules apply unchanged: identical
re-registration is idempotent OK; a different payload under the
same `sayad_id` is a conflict to the review queue; the database
unique constraint on `sayad_id` (excluding the
`expired_unreceived` / `rejected_mismatch` states) is the backstop.

### 3. Internal cheque cycle events (non-canonical, dashboard-only)

The following names are approved **for internal use only** — the
treasury dashboard widgets, the cheque card timeline, and
review-queue filing. They are **not** canonical events: they never
travel in a §10.2 envelope, never arrive in the
`X-HesabYar-Event-Type` header, never enter the store inbox, and
the ingest endpoint rejects them. They mirror the §5 §2 lifecycle
transitions, one per transition:

`cheque.held`, `cheque.in_clearing`, `cheque.cleared`,
`cheque.bounced`, `cheque.returned`, `cheque.endorsed`,
`cheque.issued`, `cheque.voided`, `cheque.expired_unreceived`,
`cheque.rejected_mismatch`, `cheque.called_in`.

Because no external producer or consumer depends on them, renaming
an internal event does not need a new ADR. The two Sayyad
"in-transit" meanings stay apart (§5 §1.3): Sayyad in-transit ≠
accounting `in_clearing`; clearing status comes only from the bank
statement.

### 4. What this ADR does not decide

- The Fintech Sayyad scope field shapes are UNVERIFIED (§5 §9.2
  #1–3). No field is guessed here; US-534 builds the simulator +
  manual fallback from the documented scope names only.
- The journal design of the accountant-gated transitions (§5 §9.1
  #3 bounced endorsed cheque, #4 `called_in`, #5 re-presentment,
  #12 endorsement to a non-supplier) — see
  `docs/cheque-decisions.md`. No journal is designed by this ADR.
- The enum / docs update that makes the two canonical names
  ingestible (`CanonicalEventType`, `docs/event-platform.md`, the
  `pending_adr` flag on the `cheque.registered` mapping) LANDED
  with US-533 (branch `ralph/phase-5-4-us533`, rebased onto the
  US-534 merge): the names are ingestible now, the
  `pending_adr` flags are cleared, and `cheque.cancelled` is a
  ninth store webhook topic.

## Consequences

- The canonical event set grew 9 → 11 with US-533 (the intake on
  these names), US-531 the `registered → held` journal, US-534
  the Sayyad reconciliation.
- The `cheque.registered` store topic is unblocked since US-533:
  its `pending_adr` flag is cleared, and `cheque.cancelled` is a
  ninth store topic.
- Dashboard widgets and the review queue may use the §3 internal
  names without further ADR approval; nothing outside HesabYar may
  emit or consume them.
