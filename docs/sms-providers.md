# SMS providers + due-date reminders (US-544, Phase 5-5 ۵ج)

ADR 0003 amendment (user decision 2026-11-18): SMS notifications
are multi-provider and pluggable — one registry, one interface,
first built-ins Kavenegar + sms.ir, additional providers addable
per installation. Per-provider variable facts (price per SMS, rate
limits, service vs dedicated line, contract/eNamad requirements)
are ENTERED MANUALLY by the user per provider and NEVER hard-coded
(the same principle as gateway fees); what is code is only each
provider's API integration shape.

NO live SMS credentials exist yet — the simulator IS the
deliverable. Live provider verification (a real send against
Kavenegar / sms.ir) is a later step needing the user's
credentials. Until then every reminder flow runs end-to-end
against the simulator, and the live clients are exercised only
against `httpx.MockTransport` with canned provider payloads.

## The registry model

| Piece | Table | Notes |
|---|---|---|
| Provider rows | `sms_providers` | One row per provider per installation. `provider_key` is the stable slug (unique per installation); `provider_kind` selects the client (`kavenegar` / `sms_ir` / `simulator` / `custom`). Exactly one default per installation (partial unique index on `is_default`). |
| Reminder policy | `sms_reminder_settings` | One row per installation: master opt-in (`enabled`, default off), offsets, user-editable templates. |
| Contract opt-ins | `sms_contract_reminder_settings` | One row per (installation, contract): `reminders_enabled`, born disabled. |
| Reminder log | `sms_reminder_log` | Every attempt — sent, failed, skipped, dry-run — with provider, cost, delivery status. The DLQ + the audit trail. |

Seed (`POST /integrations/sms/providers/seed`, idempotent):
Kavenegar + sms.ir are born DISABLED (`setup`) — the operator
enables a provider only after attaching its credentials — and the
simulator is born ENABLED and DEFAULT, so reminders work as logged
simulations with zero configuration and no live send can happen
before an operator configures a provider.

Credentials are Fernet-encrypted in `credentials_ciphertext`
(the US-503 bank-credentials pattern: the vault key comes from
settings, never from `.env` per provider, never logged, never
returned — responses carry only `has_credentials`). A provider
whose credentials are missing or undecryptable fails closed
(typed error, DLQ + review queue) — a live call is never
attempted down a path that cannot succeed.

## The provider interface

One interface, every provider (`hesabyar.domain.sms.ports`):

- `send(to, message, sender_line) -> SmsSendResult` — one message
  to one canonical `09…` mobile. The result carries the
  provider's message id, a normalized delivery status, the raw
  provider status (verbatim), and cost — `cost_toman` ONLY when
  the provider's unit is known, otherwise `cost_raw` verbatim
  with an unverified unit. No guessed conversions, ever.
- `get_status(provider_message_id) -> SmsDeliveryUpdate` — a
  delivery lookup. Providers whose delivery-state vocabulary is
  unverified return `UNKNOWN` with the raw value preserved.
- `close()` — release resources (a no-op for the simulator).

`DefaultSmsClientFactory` is the ONLY place that maps a kind to a
client implementation. `custom` has a registry row but no client —
sending through it is refused (501) until a client is built for
its API shape.

## Per-provider integration shape + verification status

### Kavenegar — shape VERIFIED, live send UNVERIFIED

Verified from the official REST documentation
(https://kavenegar.com/rest.html, fetched 2026-11-20) and the
official SDK (kavenegar/kavenegar-node):

- Send: `POST https://api.kavenegar.com/v1/{api_key}/sms/send.json`,
  form-encoded `receptor` (required), `message` (required),
  `sender` (optional). The API key travels in the URL path —
  request URLs are NEVER logged.
- Response: `{"return": {"status": 200, "message": "…"}, "entries":
  [{"messageid", "status", "statustext", "sender", "receptor",
  "date", "cost"}]}`. `cost` is Rial (documented in Persian).
- Provider error codes (`return.status`): 400 incomplete params,
  401 inactive account, 402 operation failed, 403 invalid API key,
  411 invalid receptor, 412 invalid sender, 413 empty/too-long
  (max 4000 chars domestic / 1800 others), 414 too many
  recipients (max 200/call), 417 bad send date, 418 insufficient
  credit, 607 bad tag. 401/403 map to auth errors; the rest to
  rejections.
- Delivery statuses (docs Table 2): 1 queued, 2 scheduled, 4/5
  sent to telecom, 6 failed, 10 delivered, 11 undelivered, 13
  cancelled, 14 blocked, 100 invalid message id. Mapped to
  `queued` / `sent` / `delivered` / `failed`; 100 and unknown
  codes map to `unknown`.
- Status lookup: `POST /v1/{api_key}/sms/status.json` with form
  param `messageid`.

Live end-to-end verification (a real send) is still pending — it
needs the user's Kavenegar credentials.

### sms.ir — shape VERIFIED, live send UNVERIFIED

Verified from the official published OpenAPI document
(https://api.sms.ir/swagger/v1/swagger.json, fetched 2026-11-20):

- Server `https://api.sms.ir`; auth: API key in the `x-api-key`
  header.
- Send: `POST /v1/send/bulk`, JSON
  `{lineNumber: int64, messageText: string, mobiles: [string]}`.
  Response: `{status, message, data: {packId, messageIds: [int32],
  cost: double}}`.
- Status: `GET /v1/send/{messageId}` → `data:
  ApiSentSmsStatusView {messageId, mobile, messageText,
  sendDateTime, lineNumber, cost, deliveryState: int32?,
  deliveryDateTime: int32?}`.

Deliberate gaps (NOT guessed):

- The envelope `status` code semantics are not published, so
  success is judged STRUCTURALLY: HTTP 200 + a `data` object
  carrying a non-empty `messageIds` list. Anything else is a
  rejection carrying the provider's `message`.
- The `deliveryState` values are not published anywhere
  authoritative, so `get_status` returns `UNKNOWN` with the raw
  value preserved — never a guessed mapping.
- The `cost` unit is not published: it is stored verbatim
  (`provider_cost_raw`) with unit `"unverified"` and NEVER
  converted to Toman.

The legacy token API (`ws.sms.ir`) is retired and is NOT used;
the legacy URL-send endpoint (credentials as query parameters)
is deliberately not implemented (credentials in URLs leak into
logs and proxies).

Live end-to-end verification (a real send) is still pending — it
needs the user's sms.ir credentials.

### Simulator — the default

Logs the message (recipient, sender line, text) and returns a
fake delivery id (`sim-<12 hex>`) with status `sent` and cost
None (no money moved). The log row a simulated send produces is
indistinguishable in shape from a live send's row — provider,
cost, delivery status — except its `provider_key` is `simulator`.
Every reminder flow (scheduled job, manual trigger, dry-run,
failure filing) is exercised end-to-end without a live provider.

## Reminders

### What fires, when

The daily job (`sms.daily_reminders`, below) scans due-soon and
overdue legs. A leg is due a reminder today when it is unpaid,
not awaiting cheque clearing, and today is exactly one of the
installation's configured offsets from its due date:

- `offsets_days_before` (default `[3, 0]`): 3 days before the due
  date, and on the day. `offset_days` is negative before the due
  date (`-3` = 3 days before), `0` on the day.
- `offsets_days_overdue` (default `[1]`): 1 day overdue.
  `offset_days` is positive when overdue (`+1` = 1 day overdue).

An offset fires ONLY on its day: a missed day (worker down) means
a missed reminder, visible as an absent log row — the message
text always states the true days remaining, so a late catch-up
send would lie about the date. The manual trigger is the recovery
path.

No reminders for `pending_clearance` legs (design rule — the
cheque may still clear; US-542 documents this). The reminder
service accepts the pending-clearance leg set as an injected
parameter; the worker passes an empty set for now and US-542
wires the real cheque-repository source later. The exclusion
logic is implemented and tested via the injected set.

### Opt-in gating (no auto-send without explicit opt-in)

Automatic sends require ALL of: the installation's settings
`enabled`, the plan's contract opted in
(`sms_contract_reminder_settings.reminders_enabled`), and the
default provider enabled. A plan without a contract — or whose
contract has no opt-in row — receives NO automatic reminders.

The manual trigger IS the opt-in for that one send (an explicit
operator action); the contract opt-in gates AUTOMATIC sends only.
The provider must still be enabled.

### Message content

Persian, Jalali due dates, Persian digits; the template is
user-editable per installation (never hard-coded marketing copy).
Placeholders (closed set): `{amount_toman}`, `{due_date_jalali}`,
`{days_remaining}`, `{days_overdue}`, `{invoice_id}`,
`{leg_sequence}`. The amount is the leg's REMAINING balance in
Toman (dunning continues on the balance). An unknown placeholder
is a validation error at save time, never a surprise at send time.

Dry-run mode (`POST .../remind` with `{"dry_run": true}`) renders
the message, logs a `dry_run` row, and sends nothing. A dry run
never blocks a later real send.

### Dedup (one reminder per leg + offset)

A leg + offset is reminded at most once: the service checks
`find_sent_log` first, and the partial unique index on
`(installation_id, leg_id, offset_days)` over the sent statuses
(`queued` / `sent` / `delivered`) is the backstop. A manual send
after a successful scheduled send for the same leg + offset is a
409. Failed / skipped / dry-run rows never block a retry — the
manual trigger is the replay.

### Failures: DLQ + review queue, never silent

- Per-send failure (transport, auth, rejection, provider-reported
  `failed` delivery): a `failed` log row + one review item of the
  tenth type `sms_delivery_failed` + a raised typed error. The
  daily job continues with the next leg.
- Provider cannot send at all (live kind without credentials, or
  a custom kind with no client): ONE review item per installation
  + one `skipped` row per due reminder — never one review item
  per leg.
- No phone on file: a `skipped` log row, no review item (a data
  gap, not a failure).
- No enabled default provider: the installation is skipped with
  an error in the job report.

The DLQ is the reminder log's `failed` rows; the replay is the
manual trigger.

## The daily job

`scripts/run_event_worker.py` runs the `sms.daily_reminders`
Procrastinate task (queue `sms`, default cron `30 5 * * *` =
daily 09:00 Tehran — Iran is UTC+3:30 year-round, no DST).
Jalali-aware scheduling, NO Jalali cron (ADR 0003 decision 9):
the job runs on an ordinary Gregorian cron and the targets are
computed in-app from the Jalali due dates. The job is idempotent
(a leg + offset is reminded at most once), so a missed or
duplicated run is harmless. It never posts a journal and never
touches the engine: it only reads installment schedules and
sends SMS.

## REST API

All routes are under `/api/v1`. Scopes: `sms:manage` (registry,
settings, opt-ins, manual trigger, global log) and
`installments:read` (the per-plan log read).

| Route | Scope | Notes |
|---|---|---|
| `POST /integrations/sms/providers/seed` | `sms:manage` | Seed the built-ins (idempotent); 201. |
| `POST /integrations/sms/providers` | `sms:manage` | Create a provider; 201. Credentials encrypted, never returned. |
| `GET /integrations/sms/providers` | `sms:manage` | Built-ins first, then by key. |
| `GET /integrations/sms/providers/{id}` | `sms:manage` | 404 when missing / out of scope. |
| `PATCH /integrations/sms/providers/{id}` | `sms:manage` | Only supplied fields change; null clears a clearable field; `credentials` rotates. |
| `DELETE /integrations/sms/providers/{id}` | `sms:manage` | 204; the default provider cannot be deleted (409). Log rows survive (FK SET NULL, `provider_key` denormalized). |
| `GET /integrations/sms/settings` | `sms:manage` | Defaults when never saved. |
| `PUT /integrations/sms/settings` | `sms:manage` | Replace the policy (all fields required). |
| `GET /integrations/sms/contracts/{id}/reminders` | `sms:manage` | Disabled when never configured. |
| `PUT /integrations/sms/contracts/{id}/reminders` | `sms:manage` | The contract must exist (404 otherwise). |
| `GET /integrations/sms/reminders` | `sms:manage` | Filters: `plan_id`, `leg_id`, `provider_key`, `status`, `trigger`. |
| `POST /installments/plans/{id}/remind` | `sms:manage` | Manual trigger (`leg_id?`, `provider_id?`, `recipient_phone?`, `dry_run?`); 201. |
| `GET /installments/plans/{id}/reminders` | `installments:read` | Filters: `status`, `leg_id`. |

Errors: `SMS_PROVIDER_NOT_FOUND` (404), `SMS_PROVIDER_KEY_CONFLICT`
(409), `SMS_PROVIDER_IN_USE` (409), `SMS_PROVIDER_NOT_CONFIGURED`
(422), `SMS_PROVIDER_NOT_ENABLED` (422),
`SMS_PROVIDER_CLIENT_UNAVAILABLE` (501), `SMS_PROVIDER_UNREACHABLE`
(502), `SMS_PROVIDER_AUTH_FAILED` (422), `SMS_PROVIDER_REJECTED`
(422), `SMS_PROVIDER_INVALID_RESPONSE` (502),
`SMS_CREDENTIAL_VAULT_UNAVAILABLE` (503), `SMS_NO_PROVIDER_AVAILABLE`
(422), `SMS_REMINDER_ALREADY_SENT` (409),
`SMS_RECIPIENT_PHONE_MISSING` (422), `SMS_LEG_NOT_FOUND` (404),
`SMS_LEG_NOT_REMINDABLE` (422), `SMS_NO_UNPAID_LEG` (422),
`SMS_VALIDATION_FAILED` (422, with field problems).

SMS prices are informational Toman display values, user-entered —
the Toman→Rial ×10 conversion does NOT apply anywhere in this
module.

## Privacy note

Recipient mobiles are stored on the log rows (canonical
`09…` form) so a reminder is auditable — who was texted, when,
about what. They are never logged outside the log table, never
sent to any provider other than the one that sends the message,
and never appear in review items (the review item carries the leg
and plan refs, not the number).

## Deferred

- **Live send verification** — Kavenegar + sms.ir end-to-end
  sends, needs the user's credentials.
- **Delivery-status refresh** — `get_status` is implemented on
  every client (and tested against MockTransport), but no refresh
  endpoint is wired in this story: a `sent` row stays `sent`
  until a later story polls or webhooks the provider.
- **Kavenegar `localid` idempotency** — the send endpoint accepts
  a caller-supplied `localid` for provider-side dedup; wiring it
  needs a numeric id mapping (the log id is a UUID) and is future
  hardening, not needed while the log dedup + the manual trigger
  own idempotency.
- **US-542 wires the real `pending_clearance` source** — the
  worker currently passes an empty set.
- **sms.ir `deliveryState` + cost unit** — unverified (see above);
  if sms.ir publishes the vocabulary, the mapping lands in
  `sms_ir.py` with no interface change.

## Tests

- `tests/unit/test_sms_domain.py` — phone normalization, payload
  validation, templates, scheduling, credentials, and the
  provider clients against `httpx.MockTransport` (canned
  Kavenegar + sms.ir payloads, error codes, status mapping).
- `tests/unit/test_sms_services.py` — registry + reminder
  services over in-memory fakes (seed, CRUD, settings, opt-ins,
  manual trigger incl. dry-run / dedup / failures, daily job
  incl. opt-in gating / no-phone / pending clearance /
  provider-not-sendable).
- `tests/interfaces/test_rest_sms.py` — the 13 endpoints over the
  real app with dependency overrides (scopes, 403/404/409/422s,
  dry-run, filters).
- `tests/contract/test_sms_us544.py` — the real stack against the
  live HesabYar DB (seed idempotency, CRUD, credentials round-trip,
  settings/opt-ins, the dedup index, log filters, the manual
  trigger end-to-end, the tenth review-queue type). Skips cleanly
  without `HESABYAR_DATABASE_URL`.
- `tests/unit/test_procrastinate_queue.py` — the
  `sms.daily_reminders` task is registered on the `sms` queue and
  runs (InMemoryConnector).
