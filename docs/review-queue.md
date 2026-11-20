# Review Queue (US-504, completed US-552)

The single review queue for every exception type (Phase 5 foundation
stream F4; phased design §1.2 F4, §7.2; ADR 0003). Every module files
its exceptions here with one uniform model; accountants work them from
one page (dashboard ← صف بازبینی); the dashboard shows per-type
counters, ageing and admin alerts for critical items past SLA.

US-504 built the skeleton (uniform model, actions, filters, SLA,
audit). US-552 completed it: every item carries a derived suggestion
(action + Persian reason), the queue really replays dead letters and
really learns mappings (Suggest → Auto), and pipeline exceptions file
real queue items instead of leaving the inbox row as the item.

## What lives where

| Piece | Location |
|---|---|
| Tables | Alembic `0011`: `review_items`, `review_item_audit`, `review_sla_policies`; Alembic `0032` (US-552): `integration_mappings.confirmations` |
| ORM models | `src/hesabyar/infrastructure/persistence/postgres/models.py` (`ReviewItemModel`, `ReviewItemAuditModel`, `ReviewSlaPolicyModel`) |
| Domain model + 10 types + permitted actions + SLA defaults | `src/hesabyar/domain/review_queue/models.py` |
| Derived suggestions | `src/hesabyar/domain/review_queue/suggestions.py` (`derive_suggestion`) |
| Ports | `src/hesabyar/domain/review_queue/ports.py` (`ReviewQueuePort`, `ReviewAuditPort`) |
| Repository | `src/hesabyar/infrastructure/persistence/postgres/review_queue_repository.py` (`SqlAlchemyReviewQueueRepository`) |
| Query service | `src/hesabyar/application/queries/review_queue.py` |
| Command service | `src/hesabyar/application/commands/review_queue.py` |
| Pipeline adapter (F4) | `src/hesabyar/infrastructure/events/review_queue_adapter.py` (`PersistentReviewQueueAdapter`) |
| REST | `src/hesabyar/interfaces/rest/routers/review_queue.py` (+ `schemas/review_queue.py`); promotion at `routers/integrations.py` |
| Admin page | `apps/admin/app/(dashboard)/review-queue/page.tsx` + `apps/admin/features/review-queue/` |
| Dashboard widget | `apps/admin/features/dashboard/widgets.tsx` (`ReviewQueueWidget`) |

## The ten item types (§7.2 + US-544)

| # | Type | Source module | Permitted actions |
|---|---|---|---|
| 1 | `mapping` | Store connector / mapping center (5ب) | complete_mapping_retry, auto_create_party_item, skip |
| 2 | `settlement_variance` | Settlement engine (5پ) | accept_variance, escalate_to_gateway, fix_fee_mapping |
| 3 | `cheque_exception` | Cheque module (5ث) | record_bounce, contact_customer, record_alternative_payment |
| 4 | `bank_unmatched` | Bank layer (5ت) | manual_match, record_misc_income, snooze |
| 5 | `packaging_reorder` | Packaging (5چ) | approve_purchase (manager), change_reorder_point, cash_purchase |
| 6 | `installment_overdue` | Installments (5ج) | send_reminder, extend_due_date, activate_collateral_cheque (manager) |
| 7 | `manual_import_review` | Imports (5پ/5ت) | approve_preview, fix_rows_recalc, cancel_batch |
| 8 | `cashbox_variance` | Cash box (5چ) | approve_variance_entry (manager), recount, escalate_to_manager |
| 9 | `inbox_dead_letter` | Event platform (5الف) | replay, fix_source_retry, close_with_reason |
| 10 | `sms_delivery_failed` | SMS reminders (5ج, US-544) | retry_reminder, contact_customer, close_with_reason |

The tenth type arrived with US-544: an SMS reminder whose send
failed files here (filing it as `installment_overdue` would be
semantically wrong — a pre-due reminder failure is not an overdue
installment). The §7.2 table lists nine; the type follows the
same uniform shape as the other nine.

The permitted actions are the ONLY actions the API accepts for a
type's items; anything else is 422 `REVIEW_ACTION_NOT_PERMITTED`.
Actions marked (manager) carry `requires_manager: true` — the API
exposes the flag; role enforcement lands with module 12 (users &
access), and until then the audit entry records who performed it.

## Uniform model

Every item has: `id, type, severity, source_event_ref, reason,
suggested_action, amount_irr, age + sla_deadline, actions[],
assignee, audit[]`. Age and SLA deadline are computed at read time
from `created_at` + the type's SLA policy — never stored, so a policy
change re-ages the queue immediately. `audit[]` is served only by the
detail endpoint (the list serves items without it).

US-552 adds the derived suggestion to every response:
`suggested_action_code` (one of the type's permitted actions) and
`suggested_reason_key` (a stable key the frontend maps to a Persian
reason sentence). The stored `suggested_action` free text is the
filer's note; the derived suggestion is computed at read time from
the item's type + payload (severity, subtype), so a rule change
re-suggests the whole queue immediately. See "Derived suggestions"
below.

Statuses: `open → in_review → resolved / written_off`. Resolved and
written-off items are terminal: no further action is accepted (409
`REVIEW_INVALID_STATE`); a new exception files a new item.

Action kinds decide the lifecycle effect:

| Kind | Effect |
|---|---|
| `resolve` | → `resolved` with the action's resolution |
| `dismiss` | → `resolved` (`skipped`/`deferred`) or `written_off` (`write_off`) |
| `retry` / `escalate` / `note` | → `in_review`, stays open |

The skeleton recorded only the decision and the lifecycle change.
US-552 executes the two effects the queue owns (see "Replay +
mapping learning" below); every other action stays a decision
record — its effect belongs to the owning module (the bank module
matches lines and learns bank rules, the SMS module resends, the
import module recalculates), several of which depend on this
service, so the queue cannot call back into them without a cycle.
Resolving an item NEVER auto-posts a journal: draft is the default
(ADR 0003 decision 1) and posting goes through the owning module's
policy gate.

## Derived suggestions (US-552)

`derive_suggestion(item)` (`domain/review_queue/suggestions.py`) is
a pure function of the item's type + payload — never hard-coded per
item. The returned action is always one of the type's permitted
§7.2 actions (enforced by tests):

| Type | Suggested action | When |
|---|---|---|
| `mapping` | `complete_mapping_retry` | always — the event cannot process until the mapping exists |
| `settlement_variance` | `fix_fee_mapping` / `escalate_to_gateway` / `accept_variance` | subtype `fee_mismatch` / critical / otherwise |
| `cheque_exception` | `record_bounce` / `contact_customer` | bounced subtype / otherwise |
| `bank_unmatched` | `manual_match` | always — an unmatched line never auto-posts |
| `packaging_reorder` | `approve_purchase` | always — the reorder point was hit |
| `installment_overdue` | `send_reminder` | always — dunning first, collateral activation is never the default |
| `manual_import_review` | `approve_preview` | always — the batch passed validation |
| `cashbox_variance` | `escalate_to_manager` / `approve_variance_entry` | critical / otherwise |
| `inbox_dead_letter` | `replay` | always — the dead letter's whole point |
| `sms_delivery_failed` | `retry_reminder` | always — a transient failure, retry first |

The reason key (e.g. `mapping_blocks_event`) maps to a Persian
sentence in `apps/admin/features/review-queue/review-queue-data.ts`
(`SUGGESTED_REASON_LABELS`), like every other user-facing label —
the backend stays English. The UI shows the suggestion on the card
and in the dialog, and pre-selects it for waiting items; the
operator can still pick any other permitted action.

## Replay + mapping learning (US-552)

Both effects run BEFORE the lifecycle change, so a failed effect
aborts the whole action in the caller's transaction — the item is
never left half-acted-on.

**Replay** (`replay` / `fix_source_retry` on `inbox_dead_letter`):
the action goes through the US-501 `DlqReplayService` — the SAME
idempotency key, no duplicate effect, one replay mechanism (no
second one was built). Strict: an item without a source event (or a
service without the replay seam wired) fails with 422
`REVIEW_REPLAY_NOT_POSSIBLE`; a missing event is 404
`INBOX_EVENT_NOT_FOUND`; a not-dead-lettered event is 409
`EVENT_NOT_DEAD_LETTERED`.

**Mapping learning** (`complete_mapping_retry` on `mapping`): when
the reviewer supplies the completed mapping, the queue learns it as
a mapping-center suggestion (confidence 1, basis
`review_queue:<item-id>`) — the mapping center IS the store, no new
table was needed. The operator accepts it there (one-click confirm)
and it becomes a SUGGEST-mode mapping. The audit entry records
`mapping_version = suggestion:<id>`. Then the source event is
reprocessed best-effort through `DlqReplayService.reprocess`
(accepts `needs_review` and `dead_lettered`; an already-processed or
in-flight event is noted in the audit trail, not an error — the
mapping completion is the action's real effect). With no mapping
supplied, only the reprocess runs (the mapping was completed
elsewhere). Mapping details with any other action are rejected
(422 `REVIEW_ACTION_NOT_PERMITTED`).

**Suggest → Auto promotion**: `IntegrationMapping.confirmations`
(migration 0032) counts how many times the same target was
confirmed — re-creating a mapping with an unchanged target carries
the count + 1; a changed target starts at 1. At
`MAPPING_AUTO_MIN_CONFIRMATIONS = 3` (mirroring the bank rules'
floor), an active SUGGEST mapping becomes promotable:
`POST /integrations/mappings/{id}/promote` (scope
`integrations:manage`) creates a NEW version in `auto` mode, the
old version superseded, the count carried unchanged (promoting is
not a confirmation). Under-confirmed, already-auto, or superseded
→ 409 `INTEGRATION_MAPPING_NOT_PROMOTABLE`. Promotion is always a
human decision; nothing auto-promotes.

## Pipeline adapter (F4, US-552)

`PersistentReviewQueueAdapter` replaces the F1
`InboxReviewQueueAdapter` in `get_event_pipeline_service`: pipeline
exceptions (mapping gaps, dead letters) now file real `ReviewItem`
rows in the tenant's queue — the queue is the one place a human
sees every exception, and a mapping gap that only exists as an
inbox row is invisible there. Severity maps `critical` → CRITICAL,
`high`/`warning` → WARNING, `info` → INFO (unknown degrades to
WARNING); the subtype is the event type (`details["event_type"]`).
A tenantless draft (an event ingested before tenant scoping)
cannot enter the tenant-scoped queue, so it falls back to the F1
behaviour (returns the source event id).

## Ageing / SLA

One SLA policy per (tenant, item type): `sla_hours` (deadline after
filing) and `warn_hours` (window before the deadline in which the item
shows "near deadline"). A missing row means the skeleton default
applies (mapping 48/12, settlement_variance 72/24, cheque_exception
24/6, bank_unmatched 72/24, packaging_reorder 168/48,
installment_overdue 48/12, manual_import_review 48/12,
cashbox_variance 24/6, inbox_dead_letter 24/6). These defaults are
starting values, not verified business rules — the PATCH endpoint
exists so an admin can set the real ones per installation
(`0 < warn_hours < sla_hours`, enforced by the service and by a DB
check constraint).

A critical item past its SLA produces one admin notification in the
summary response; the queue page renders it as an alert and the
dashboard widget as a red line. No delivery channel (email/SMS)
exists yet — delivery lands with the unified panel (phase 5ح).

## The F1 seam (audit trail)

Every action on every item appends an audit entry (who, when, which
action, which decision, with which policy/mapping version). The
entries are served through `ReviewAuditPort` — the SQLAlchemy
implementation stores them in `review_item_audit`. F1's decision
audit log is keyed by inbox event id, so review actions cannot be
mirrored into it (most items have no inbox event); `review_item_audit`
stays the store, and the replay's own decision-log entry is written
by the replay service itself. US-552 populates `mapping_version`
for learning actions (`suggestion:<id>`); `policy_version` stays
null — no review action consults a versioned policy, and the field
is not faked. Reprocess outcomes (reprocessed / already done /
missing event / no source event) are recorded in the audit note.

## API

All routes are under `/api/v1/review-queue`. Scopes: `review:read`
(reads), `review:file` (filing), `review:resolve` (actions,
assignment), `settings:manage` (SLA policy updates).

| Route | Scope | Notes |
|---|---|---|
| `GET /review-queue/types` | `review:read` | All ten types with permitted actions |
| `GET /review-queue/items` | `review:read` | Filters: type, severity, status, channel, company, assignee, min/max amount (IRR), min/max age (hours), overdue. Default sort: critical first, then oldest. Cursor pagination. |
| `POST /review-queue/items` | `review:file` | Files an item (status `open`, `filed` audit entry) |
| `GET /review-queue/items/{id}` | `review:read` | Detail with the audit trail |
| `POST /review-queue/items/{id}/actions` | `review:resolve` | `{action, note?, mapping?}` — must be permitted for the type; `mapping` (kind/source_key/target_type/target_ref/channel_key?) only with `complete_mapping_retry` |
| `PATCH /review-queue/items/{id}` | `review:resolve` | `{assignee}` — null unassigns |
| `GET /review-queue/summary` | `review:read` | Per-type counters (all ten, even zero), totals, oldest item, admin notifications; optional `company` filter |
| `GET /review-queue/sla-policies` | `review:read` | Effective policy per type |
| `PATCH /review-queue/sla-policies/{type}` | `settings:manage` | `{sla_hours, warn_hours}` |

Errors: `REVIEW_ITEM_NOT_FOUND` (404), `REVIEW_ACTION_NOT_PERMITTED`
(422), `REVIEW_INVALID_STATE` (409), `REVIEW_INVALID_SLA_POLICY`
(422), `INVALID_FILTER` (400, unknown type/severity/status).
US-552 adds: `REVIEW_REPLAY_NOT_POSSIBLE` (422, replay with no
source event), `INBOX_EVENT_NOT_FOUND` (404), `EVENT_NOT_DEAD_LETTERED`
(409), `EVENT_NOT_REPROCESSABLE` (409, reprocess of a done/in-flight
event — only surfaces through the DLQ path, never through the queue's
best-effort reprocess), `VALIDATION_ERROR` (422, unknown mapping kind
in the learning payload), `INTEGRATION_MAPPING_NOT_PROMOTABLE` (409).

The promotion endpoint lives with the mapping center:
`POST /api/v1/integrations/mappings/{id}/promote` (scope
`integrations:manage`); `IntegrationMappingResponse` carries
`confirmations` and `auto_eligible`.

## Dashboard widget

The dashboard home loads the summary company-scoped (like every
other widget) and renders `ReviewQueueWidget` first in the grid:
waiting count, per-type counts, overdue count, oldest item age, and
the critical-overdue alert, linking to `/review-queue`. Items filed
without a `company_id` are invisible to the company-scoped widget
but still appear on the queue page, which is tenant-wide. The widget
load is failure-tolerant: if the key lacks `review:read`, the tile
says so honestly instead of killing the dashboard.

## UI notes

- Every card and dialog shows the derived suggestion (action label +
  Persian reason); the dialog also shows the filer's stored text as
  the «یادداشت مبدأ». The dialog pre-selects the suggested action
  for waiting items.
- A mapping item's «تکمیل نگاشت و پردازش مجدد» action opens the
  learning form (kind, source key, target type, target ref, optional
  channel key): a complete form is learned as a mapping-center
  suggestion; an empty form means the mapping was completed
  elsewhere and only the source event is reprocessed. The audit
  trail shows the learned suggestion id.
- The item dialog has a single close control: the footer's «بستن»
  button. The dialog's built-in X is disabled (`showCloseButton`)
  because it duplicates the footer's accessible name and its icon
  target is below the 44px minimum; Escape still closes.
- After an action or assignment, the list shows skeletons while it
  refetches (`refreshAfterMutation` sets loading) instead of leaving
  the stale pre-action list rendered.
- Focus stays trapped in the dialog by the Base UI focus manager;
  its focus-guard span sits outside the dialog content, so a Tab can
  land on it for a sub-100ms hop before focus returns inside.

## Tests

- `tests/unit/test_review_queue_services.py` — domain + services
  over in-memory ports (no DB): the ten types, permitted actions,
  default sort, filters, SLA math, summary counters, admin
  notifications, action lifecycle, SLA validation. US-552 adds:
  suggestion derivation (all ten types + overrides + the
  always-permitted invariant), replay through the real DLQ service
  (positive + no-ref/unwired/missing/not-dead-lettered negatives),
  mapping learning + best-effort reprocess, the pipeline adapter.
- `tests/unit/test_event_pipeline.py` — US-552 adds the `reprocess`
  path (accepts `needs_review` + `dead_lettered`, rejects
  received/processing/processed/failed, keeps the idempotency key).
- `tests/unit/test_integrations_services.py` — US-552 adds mapping
  confirmations (first = 1, same-target +1, changed target resets,
  double-accept increments) and promotion (at threshold → AUTO
  version, below threshold / already-auto / superseded / unknown →
  errors).
- `tests/interfaces/test_rest_review_queue.py` — real app + auth +
  PostgreSQL: all ten types returned by the API (one sample item per
  type), filters, sort, pagination, actions, assignment, summary,
  SLA policies, tenant isolation. US-552 adds: derived suggestion
  fields on every item (+ overrides), replay positive (inbox row
  back to received) + 422/404/409 negatives, learning (suggestion
  row + audit `mapping_version` + reprocess) + wrong-action and
  unknown-kind 422s.
- `tests/interfaces/test_rest_integrations.py` — US-552 adds the
  promotion endpoints (`confirmations`/`auto_eligible` on responses,
  promote at threshold, 409 below threshold, 404 unknown, 403
  read-only key).
- `tests/contract/test_review_queue_replay_us552.py` — live Tryton +
  HesabYar DB, skips cleanly without a server: mapping gap → real
  queue item → complete elsewhere → reprocess → one invoice draft,
  no duplicate on re-run; and the learning loop (learn → accept →
  draft).
- `apps/admin/features/review-queue/review-queue-data.test.ts` —
  Persian labels, age formatting, SLA state. US-552 adds: all 14
  reason keys, all 9 mapping kinds, all 6 target types.
- `apps/admin/features/shared/api-error.test.ts` — US-552 adds the
  six new error codes' Persian messages.
- `apps/admin/e2e/review-queue.spec.ts` — live flow: seed ten items
  via the BFF, filters, detail dialog + action, SLA settings,
  dashboard widget; responsive (320/390/1280, no overflow) and
  keyboard + contrast AA. `/review-queue` is also in the shared
  `e2e/responsive.spec.ts` route list. The spec loads every queue
  page before asserting (`loadAllQueuedItems`): the shared dev DB
  accumulates items from earlier runs and the queue pages at 20, so
  a run's newest seeds sort onto later pages. Its console-error
  assertion ignores the dashboard's setup-status 502 (BFF 30s
  timeout on Supabase) and 503 (trytond restart) — both known
  pre-existing backend/env issues, same as the journal pickAccounts
  one; the widget assertion still guards the summary endpoint.
  US-552 adds a second flow: derived suggestions on the BFF
  responses + the card, the learning form (a suggestion really
  lands in the mapping center), the replay error for an item
  without a source event, and one action per remaining type-group.
  A positive replay through the UI is NOT covered (it needs a
  genuinely dead-lettered event — five transient failures — which
  the e2e cannot manufacture; the REST tests cover it).
