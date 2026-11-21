# Reminders (US-564, Phase 5-7 ۵خ W3)

One daily job, `reminders.daily`, sends every reminder type
(cheque due-soon, overdue installments, uncounted cash boxes,
eNamad expiry) over three channels — in-app, email, SMS — under
one per-installation policy. The job runs on the US-560
scheduler: daily **08:30 Tehran** (`0 5 * * *` UTC), Fridays
skipped by default, queue `reminders`. 08:30 is deliberate: it
is just after the default quiet hours end (08:00), so the run
almost never hits its own quiet-hours suppression.

## Shape: one job, per-type enablement

One job, not one per type — the four types share the same
policy, the same channel dispatch, the same idempotency log, and
the same run. Per-type enablement lives in the reminder
preferences (`cheque_due_soon_enabled`, …), so an operator turns
a type off without touching the job registry. The registry job
itself is the 9th entry in `DEFAULT_JOBS` (see
`docs/scheduler.md`).

## The four reminder types

### Cheque due-soon (`cheque_due_soon`)

Fires when a cheque's due date is exactly N days out, for each
configured lead (default 7 and 3 days). Only **payment-purpose**
cheques in a remindable state — `held`, `issued`, `called_in`.
Excluded on purpose:

- **Collateral** cheques — they are not payments; reminding
  about them would be noise.
- **`in_clearing`** — the bank has the leaf; the due date is the
  bank's problem now.
- **`registered`** — the leaf never arrived; the US-531 job owns
  those intake reminders.
- Terminal states (`cleared`, `bounced`, `returned`, `endorsed`,
  `voided`, …) and overdue cheques (a due-soon reminder for a
  cheque that is already late is a different reminder nobody
  asked for).

The message names the amount (Toman, Persian digits), the due
date (Jalali `YYYY/MM/DD`), the days remaining, the 16-digit
Sayad id verbatim, and the issuer (received) or the payee
(issued).

### Overdue installments (`installment_overdue`)

Fires when a leg is exactly N days overdue, for each configured
offset (default 1 and 7 days). It reuses the US-544 dunning
views (`plan_enters_dunning`, `due_reminders`) — the same
`LegBalanceView` math, the same balance split — so US-564 and
US-544 can never disagree about which leg is overdue by how
much. Excluded:

- **BNPL plans** — the gateway owns the customer relationship;
  HesabYar must not remind a customer the gateway is already
  dunning.
- **Fully paid legs** and **legs fully covered by a cheque in
  flight** (pending clearance) — the cheque may still clear;
  reminding about money that is already on its way is wrong.
  The cheque-aware split mirrors `SmsReminderService._leg_views`
  exactly (`resolve_cheque_states` + `split_allocations`).
- Plans whose contract did not opt in to SMS (see Consent) are
  still reminded in-app and by email — consent gates SMS only.

The message states the unpaid balance, the due date, the days
overdue, the leg number, and the invoice id.

### Uncounted cash box (`cashbox_uncounted`)

Fires when the days since a box's last **archived** (completed)
count — or since the box was created, if it was never counted —
is **greater than** N (default 7, user-configured per
installation, 1–90). A count in progress (any non-archived
session) suppresses the reminder: somebody is already counting.
Inactive boxes never remind.

N lives in the reminder preferences, **not** in
`CashBoxSettings.count_reminder_days` (US-516). The two fields
are the same idea in two places — unifying them is deferred
(there is no migration that moves one into the other yet, and
the cash-box settings row predates reminders). Until then the
reminder N is the one that fires.

The idempotency event is the **last-count date**, not the
calendar day: a box uncounted for 8 days reminds once, and does
not remind again the next day for the same uncounted period.
After a new count, the clock restarts and the next uncounted
period reminds again.

### eNamad expiry (`enmad_expiry`)

Fires when the store's eNamad certificate expires in exactly N
days, for each configured lead (default 30, 7, and 0). Lead 0 is
the expiry day itself and files a **CRITICAL** review item; the
other leads file WARNING. An already-expired certificate never
reminds — the service is already disabled, and a reminder cannot
undo that.

The expiry date lives on the reminder preferences
(`enmad_expiry_date`, nullable) because no store-profile model
exists yet to own it. When one lands, the date moves there and
the preferences field becomes a read-through.

**The message states plainly, in Persian, that expiry disables
the payment gateway and BNPL service** (phased design §8.3):
«با انقضای اینماد، درگاه پرداخت و سرویس BNPL غیرفعال می‌شود.»
This is a warning, not a suggestion — and the message text is
**not user-editable** (see Messages), precisely so the warning
cannot be edited away.

## Preference model

One `reminder_preferences` row per installation
(`installation_id` unique). The policy is **born disabled**:
`enabled` defaults to `False`, so a fresh installation sends
nothing until an operator turns reminders on. Everything else
defaults to on — per-type flags, all three channels — so
enabling the master switch is the only step that matters.

| Field | Default | Bounds |
|---|---|---|
| `enabled` | `false` | — |
| `cheque_due_soon_enabled` | `true` | — |
| `installment_overdue_enabled` | `true` | — |
| `cashbox_uncounted_enabled` | `true` | — |
| `enmad_expiry_enabled` | `true` | — |
| `channel_in_app` / `channel_email` / `channel_sms` | `true` | — |
| `cheque_lead_days` | `[7, 3]` | 0–30 days, ≤ 5 entries |
| `installment_overdue_days` | `[1, 7]` | 0–30 days, ≤ 5 entries |
| `cashbox_uncounted_days` | `7` | 1–90 |
| `enmad_lead_days` | `[30, 7, 0]` | 0–30 days, ≤ 5 entries |
| `quiet_hours_enabled` | `true` | — |
| `quiet_hours_start` / `quiet_hours_end` | `22:00` / `08:00` | must differ; the window may cross midnight |
| `enmad_expiry_date` | `null` | ISO date |
| `notification_email` | `null` | the operator address email goes to |

`PUT /reminders/preferences` is a **full replace**: only
`enabled` is required in the body; every absent field falls
back to its default. Lead lists are deduplicated and sorted on
save. All field errors are reported at once (422
`REMINDER_VALIDATION_FAILED`).

## Channel matrix

| Type | In-app | Email | SMS |
|---|---|---|---|
| Cheque due-soon | ✓ | ✓ | ✓ (received + consented only) |
| Overdue installment | ✓ | ✓ | ✓ (consented only) |
| Uncounted cash box | ✓ | ✓ | — (operator reminder, no customer) |
| eNamad expiry | ✓ | ✓ | — (operator reminder, no customer) |

- **In-app** is a review-queue item of type `reminder` (the
  11th `ReviewItemType`; SLA 24 h, warn at 6 h). The item's
  `source_event_ref` is the reminder's idempotency key, so the
  item and the log row are the same reminder. In-app is the
  only channel that may record during quiet hours.
- **Email** is a port with **no implementation yet**: the F3
  email provider was never built (only SMS landed in US-544).
  Until a sender exists, every email attempt is logged as
  skipped (`EMAIL_NOT_CONFIGURED`) and nothing is sent. The
  preferences already carry `notification_email` for when a
  sender lands; the worker passes `email=None` until then.
- **SMS** goes through the US-544 multi-provider registry (see
  below).

A channel the preferences disable is never attempted — no log
row, no skip, nothing.

## Quiet hours

Default 22:00–08:00 Tehran, crossing midnight. During the
window, SMS and email attempts are **suppressed and logged
skipped** (`QUIET_HOURS`); in-app still records. A suppressed
reminder is **not consumed**: the idempotency key is only
written on a send, so a persistent trigger (cash box, eNamad
lead 0) retries on the next run. A date-offset trigger whose
day passes during quiet hours is missed, the same as US-544's
rule — the daily run is at 08:30, so in practice this only
matters for a manually triggered run or a changed quiet-hours
window.

## Idempotency keys

`{type}:{subject_id}:{event_ref}:{channel}` — one key per
reminder per channel. Per-channel keys are deliberate: an SMS
failure must not block the in-app reminder for the same event,
and vice versa.

| Type | `event_ref` |
|---|---|
| Cheque due-soon | due date (ISO) — a changed due date is a new event |
| Overdue installment | due date (ISO) |
| Uncounted cash box | last-count date (ISO) — one reminder per uncounted period |
| eNamad expiry | expiry date (ISO) |

The log table enforces it with a **partial unique index** on
`(installation_id, idempotency_key)` where the status is
`queued`, `sent`, or `delivered`. Failed and skipped rows are
outside the index, so a failure retries on the next run and a
skip never blocks a send. The service also pre-checks the log
before each send (a cheap read that keeps the duplicate out of
the provider entirely); the index is the backstop for the race
between two workers.

## SMS: registry, consent, cost, dedup

SMS goes through the **US-544 multi-provider registry**
(Kavenegar + sms.ir first built-ins, provider specifics
user-entered, never hard-coded). US-564 shares the client-build
path with US-544: `build_provider_client` and
`provider_sendable_reason` are module-level helpers in
`application/commands/sms.py`, used by both services, so a
provider that cannot send fails identically in both jobs.

**No live SMS without credentials.** The registry's default is
the simulator; a live provider without stored credentials is
unsendable (`SMS_PROVIDER_NOT_CONFIGURED`), the SMS is skipped,
and **one** review item per installation is filed (deduped
against open items with ref `sms_provider:{key}` — unlike
US-544, which files every run). No provider at all is
`SMS_NO_PROVIDER_AVAILABLE`.

**Consent gates SMS only** — in-app and email go to the
operator, never the customer. Installments require the plan
contract's opt-in (`SmsContractReminderSettings`, the US-544
rule). Cheques: SMS only for **received** cheques linked to an
installment leg whose plan's contract opted in; an unlinked
cheque has no consent on file, so no SMS (fail-closed). Issued
cheques are never SMS'd — there is no customer to remind.
Mobile numbers are normalized (Persian/Arabic-Indic digits,
`+98`/`0098` forms) before validating; a missing or
unparseable number is skipped (`SMS_RECIPIENT_PHONE_MISSING`).

**Cost** is recorded only when the provider reports a cost in
a known unit (the US-544 rule): the informational Toman figure
in `cost_toman`, the verbatim figure in `provider_cost_raw`
with its unit. No conversion is ever guessed.

**Shared dedup with US-544.** `sms.daily_reminders` already
SMSes installment legs at offset +1 — the same offset as
US-564's default. Before sending an installment SMS, US-564
checks the US-544 log (`find_sent_log`); if US-544 already
sent for that leg + offset, US-564 skips the SMS (and still
sends in-app + email). After sending, US-564 **mirrors the
send into the US-544 log** (trigger `scheduled`), so the other
job's dedup sees it. The mirror-insert race is caught:
if US-544 sent between the check and the insert, the reminder
log row still records the send, with a `duplicate_race` note.

**SMS failure** files an `SMS_DELIVERY_FAILED` review item
(the US-544 pattern) and logs the row `failed` with the typed
error code (`SMS_PROVIDER_REJECTED` for a provider refusal or
a provider-reported FAILED status, `SMS_PROVIDER_UNAVAILABLE`
for a client-build failure). A failure is retried on the next
run — failed rows are outside the idempotency index.

## Messages

Fixed Persian text, in `domain/reminders/messages.py` — **not
user-editable templates**. The eNamad disabling warning must
not be editable away, and the other messages carry identifiers
(Sayad id, invoice id) that must render verbatim. Amounts are
Toman in Persian digits (ungrouped); dates are Jalali
`YYYY/MM/DD` in Persian digits; identifiers stay in Latin
digits. The Jalali rendering reuses `to_persian_digits` from
`domain/sms/templates.py`.

## REST

Everything needs the `reminders:manage` scope (new — no
existing scope fits both the preferences and the log; the
single-scope pattern mirrors `sms:manage`). All routes are
tenant/installation-scoped.

- `GET /reminders/preferences` — the policy (defaults when
  never saved: disabled).
- `PUT /reminders/preferences` — full replace (only `enabled`
  required); 422 `REMINDER_VALIDATION_FAILED` on bad input.
- `GET /reminders/log` — the reminder log, newest first;
  filters `reminder_type`, `subject_id`, `channel`, `status`;
  unknown query params rejected (400 `INVALID_FILTER`).

## Migration

Migration **0038** (`down_revision 0035`) creates
`reminder_preferences` and `reminder_log`, and alters the
`review_items` type check constraint to admit `'reminder'`.
Two sibling Wave B workers (US-561, US-563) also branch from
0035 — the coordinator re-chains at merge.

## Deferred

- **Email sender** (F3): the port exists, no provider is
  implemented. Email reminders are logged skipped until one
  lands.
- **Cash-box N unification**: `CashBoxSettings.count_reminder_days`
  (US-516) and the reminder `cashbox_uncounted_days` are the
  same idea in two places; no migration moves one into the
  other yet.
- **eNamad date ownership**: `enmad_expiry_date` lives on the
  reminder preferences until a store-profile model exists.
- **No frontend**: US-564 has no UI story; preferences and the
  log are REST-only.
