# Recurring invoices + journals (US-563, Phase 5-7 ۵خ W3)

A **recurring template** is a standing instruction: "for this
company, on this Jalali schedule, create this invoice / journal".
Each firing creates one **occurrence** — a draft document in the
engine plus a HesabYar-side occurrence row that links the document
to its template and occurrence number.

## The template model

A template (`recurring_templates`, migration 0037) carries:

- **Identity**: tenant, installation (the policy gate evaluates
  auto-post against *its* registry policy), company (the
  `engine_companies` row id + the Tryton company id the generator
  drafts into), a name, a kind (`invoice` | `journal`).
- **Schedule**: a Jalali schedule (see below) with optional end
  conditions (end date and/or max occurrences).
- **Payload**: the document the template generates — an invoice
  payload (type `out`/`in`, party, priced lines, optional journal /
  account / address) or a journal payload (at least two one-sided
  lines, balanced, total > 0). The payload is validated against
  the same accounting invariants as the live draft flows
  (US-202/US-204), so a template that could never produce a valid
  draft is refused at creation, not at its first firing.
- **auto_post**: the explicit auto-post opt-in (default `false`).
- **Status**: `active` | `paused` | `completed`.

An occurrence (`recurring_occurrences`) carries: the template id,
the occurrence number (position in the sequence from the start
date), the scheduled date (Gregorian + its Jalali label + the clamp
report), the status (`pending` | `draft` | `posted` | `failed`),
the total, the **snapshot** (payload + schedule + total the
occurrence was generated from), the engine document id, the
policy-gate verdict (when auto-post was attempted), an error, and
the generation timestamp.

## Jalali schedule math

Three shapes, all computed in-app (no Jalali cron exists):

| Shape | Fires on |
|---|---|
| `monthly` | One Jalali day each month |
| `every_n_months` | One Jalali day every N months, anchored at the start month |
| `yearly` | One Jalali date each year |

- **Occurrence numbers** are positions in the sequence from the
  start date; a candidate before the start date consumes no
  number. Numbering is global: previewing from December still
  shows occurrence #2, not #1.
- **Day-31 clamping (honest, never silent).** Jalali month lengths
  vary: months 1–6 have 31 days, 7–11 have 30, Esfand has 29 or 30
  (1403: 30, 1404: 29, 1405: 29). A day-31 recurrence fires on the
  month's **last day** — Aban 30, Esfand 29 — and the occurrence
  says so: `clamped: true` with `requested_jalali_day: 31` and
  `actual_jalali_day: 30` (or 29). It never skips the month, and
  the preview shows the clamp before anything is saved.
- **End conditions** truncate the sequence: an end date (no
  occurrence after it) and/or a max occurrence count. Both stay
  editable; the schedule *shape* (kind / Jalali day / month /
  interval / start date) is immutable once the template has any
  occurrence — it would renumber history.
- **Enumeration is always bounded**: a caller must pass a count,
  an `until` date, or rely on the template's own end condition.
  An unbounded enumeration is refused rather than looped.

## Preview

`POST /recurring-templates/preview` takes the full create body
(validated exactly as a create would be — the company resolves,
the payload invariants hold) and returns the next N occurrences
with dates + amounts + clamp flags, saving nothing. It needs the
kind's **write** scope (it validates a write).

`GET /recurring-templates/{id}/preview` previews an existing
template's next N occurrences from today (Tehran) and needs the
kind's **read** scope. Preview count is 1..120.

## The generator job

`scheduler.recurring_generation` (registry job #9, daily 07:30
Tehran, Procrastinate cron `0 4 * * *` UTC, queue `scheduler`)
runs `RecurringGenerationService.generate_due` across every active
template:

- **Draft by default.** Every occurrence is created as a DRAFT.
  Auto-post needs BOTH the template's explicit opt-in AND the
  three-layer policy gate (F3) evaluated for event type
  `recurring.generate` (policy row + installation opt-in + open
  fiscal period). A denied gate leaves a draft, never an error.
  With no gate wired in the process, auto-post is denied
  (fail-closed), not errored. An invoice auto-post finalizes
  first, then posts (the events.py order).
- **Idempotent.** One occurrence per template per scheduled date:
  the claim-first flow (insert a `pending` row, then create the
  document) plus the `(template, scheduled date)` and
  `(template, occurrence number)` unique constraints are the
  backstop — a conflict means another run claimed it first, and
  the run skips it, never duplicates it. The claim flush runs
  inside a SAVEPOINT, so a conflict rolls back only the claim,
  never the run's earlier work.
- **Catch-up, not skip.** The generator looks at every occurrence
  with `scheduled_date <= today`: a Friday due date (the daily job
  skips Friday) generates on Saturday, and a template resumed
  after a pause generates its missed occurrences. The occurrence
  keeps its scheduled date either way.
- **Failure handling.** A draft-creation failure marks the
  occurrence `failed` (with the error), files one deduped
  dead-letter review item per template per date, and the run
  continues — one template never blocks the rest. The next run
  retries the failed occurrence under its own row and number. A
  draft whose *post* failed is NOT retried: the draft exists, so
  an accountant posts it by hand (retrying would create a second
  draft).
- **Completion.** A template becomes `completed` when no
  occurrence falls after today AND no due occurrence is still
  open (pending/failed) — a failed last occurrence keeps the
  template active so the next run retries it instead of silently
  dropping it. A completed template cannot be edited, paused, or
  resumed.
- **Lost-engine-response window (known limitation).** If the
  engine creates the draft but the response is lost, a retry
  could create a second draft: the occurrence claim prevents a
  duplicate *scheduled* generation, but it cannot see a draft
  whose creation response never arrived. The engine has no
  idempotency key for draft creation to close this window.

## Editing never rewrites history

Each occurrence stores the payload + schedule snapshot it was
generated from, and the engine document is created from that
snapshot — never from the live template. A template edit only
changes **future** occurrences; an already-generated draft (and
its occurrence row) is never touched. The one exception: a
**failed** occurrence refreshes its snapshot from the current
template on retry — nothing was generated yet, so fixing the
template is exactly how a failed occurrence heals.

The generated document also carries a visible marker in its
description — `[نام قالب — تکرار N]` — so an accountant reading
the draft inside Tryton sees the same link the occurrence row
records.

Deleting a template is only allowed while it never fired (a
template with any occurrence — even a failed one — is 409
`RECURRING_TEMPLATE_IN_USE`; pause it instead).

## REST endpoints

Base: `/api/v1/recurring-templates`. No new scopes — the existing
catalog fits: an invoice template is guarded by `invoice:read` /
`invoice:draft:create`, a journal template by `ledger:read` /
`ledger:draft:create`. The scope depends on the template's kind,
so it is checked inside the handler after the kind is known.

| Method + path | Scope | Notes |
|---|---|---|
| `POST /recurring-templates` | kind's write | 201; `Idempotency-Key` required |
| `GET /recurring-templates` | ≥1 read scope | Cursor-paginated; a key sees only the kinds it may read; `company` / `kind` / `status` filters |
| `GET /recurring-templates/{id}` | kind's read | 404 `RECURRING_TEMPLATE_NOT_FOUND` (another tenant's looks exactly like missing) |
| `PUT /recurring-templates/{id}` | kind's write | `Idempotency-Key` required; only given fields change |
| `DELETE /recurring-templates/{id}` | kind's write | 204; 409 when the template already fired |
| `POST /recurring-templates/preview` | kind's write | Pre-save preview; saves nothing |
| `GET /recurring-templates/{id}/preview` | kind's read | From today (Tehran); `count` 1..120 |
| `POST /recurring-templates/{id}/pause` | kind's write | Idempotent |
| `POST /recurring-templates/{id}/resume` | kind's write | Idempotent; missed occurrences catch up |
| `GET /recurring-templates/{id}/occurrences` | kind's read | Cursor-paginated, number order |

Idempotency (spec §10.4): the key is required on create and
update — same key + same payload replays the stored response,
same key + different payload is 409 `IDEMPOTENCY_CONFLICT`.
Pause/resume/delete are idempotent by nature and take no key.
Every mutation writes an audit event, success and failure alike
(spec §14.3).

Error codes: `RECURRING_TEMPLATE_INVALID` (422),
`RECURRING_TEMPLATE_NOT_FOUND` (404),
`RECURRING_TEMPLATE_IN_USE` (409),
`RECURRING_OCCURRENCE_CONFLICT` (409, internal — the generator
absorbs it as a skip).

## Files

- Domain: `src/hesabyar/domain/recurring/` — `schedules.py`
  (Jalali math), `models.py` (templates, payloads, occurrences,
  snapshots), `ports.py` (repository port), `errors.py`.
- Application: `src/hesabyar/application/commands/recurring.py`
  (`RecurringTemplateService`, `RecurringGenerationService`);
  the job delegate is
  `ScheduledJobsService.run_recurring_generation_all`.
- Infrastructure: `migrations/versions/0037_us563_recurring_templates.py`,
  `src/hesabyar/infrastructure/persistence/postgres/recurring_repository.py`,
  the scheduler wiring in `scheduler_queue.py` /
  `procrastinate_queue.py` / `config.py` / `scripts/run_event_worker.py`.
- REST: `src/hesabyar/interfaces/rest/routers/recurring.py`,
  `src/hesabyar/interfaces/rest/schemas/recurring.py`.
- Tests: `tests/unit/test_recurring_schedules.py`,
  `tests/unit/test_recurring_services.py`,
  `tests/interfaces/test_rest_recurring.py`.
