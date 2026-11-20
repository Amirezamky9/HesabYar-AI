# Review Queue (US-504)

The single review queue for every exception type (Phase 5 foundation
stream F4; phased design §1.2 F4, §7.2; ADR 0003). Every module files
its exceptions here with one uniform model; accountants work them from
one page (dashboard ← صف بازبینی); the dashboard shows per-type
counters, ageing and admin alerts for critical items past SLA.

## What lives where

| Piece | Location |
|---|---|
| Tables | Alembic `0011`: `review_items`, `review_item_audit`, `review_sla_policies` |
| ORM models | `src/hesabyar/infrastructure/persistence/postgres/models.py` (`ReviewItemModel`, `ReviewItemAuditModel`, `ReviewSlaPolicyModel`) |
| Domain model + 9 types + permitted actions + SLA defaults | `src/hesabyar/domain/review_queue/models.py` |
| Ports | `src/hesabyar/domain/review_queue/ports.py` (`ReviewQueuePort`, `ReviewAuditPort`) |
| Repository | `src/hesabyar/infrastructure/persistence/postgres/review_queue_repository.py` (`SqlAlchemyReviewQueueRepository`) |
| Query service | `src/hesabyar/application/queries/review_queue.py` |
| Command service | `src/hesabyar/application/commands/review_queue.py` |
| REST | `src/hesabyar/interfaces/rest/routers/review_queue.py` (+ `schemas/review_queue.py`) |
| Admin page | `apps/admin/app/(dashboard)/review-queue/page.tsx` + `apps/admin/features/review-queue/` |
| Dashboard widget | `apps/admin/features/dashboard/widgets.tsx` (`ReviewQueueWidget`) |

## The nine item types (§7.2)

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

Statuses: `open → in_review → resolved / written_off`. Resolved and
written-off items are terminal: no further action is accepted (409
`REVIEW_INVALID_STATE`); a new exception files a new item.

Action kinds decide the lifecycle effect:

| Kind | Effect |
|---|---|
| `resolve` | → `resolved` with the action's resolution |
| `dismiss` | → `resolved` (`skipped`/`deferred`) or `written_off` (`write_off`) |
| `retry` / `escalate` / `note` | → `in_review`, stays open |

The skeleton records the decision and the lifecycle change. The
type-specific effect (replaying a dead letter, rebuilding a mapping,
posting an adjustment journal) belongs to the owning module and is
requested through the audit trail — it is NOT faked here.

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
entries are served through `ReviewAuditPort` — today the SQLAlchemy
implementation stores them in `review_item_audit`; when F1's
decision audit log lands, that log becomes the audit source and the
port's implementation reads from it instead, with no change to the
application layer or the API. The `policy_version` /
`mapping_version` fields are null until F1 supplies them.

## API

All routes are under `/api/v1/review-queue`. Scopes: `review:read`
(reads), `review:file` (filing), `review:resolve` (actions,
assignment), `settings:manage` (SLA policy updates).

| Route | Scope | Notes |
|---|---|---|
| `GET /review-queue/types` | `review:read` | All nine types with permitted actions |
| `GET /review-queue/items` | `review:read` | Filters: type, severity, status, channel, company, assignee, min/max amount (IRR), min/max age (hours), overdue. Default sort: critical first, then oldest. Cursor pagination. |
| `POST /review-queue/items` | `review:file` | Files an item (status `open`, `filed` audit entry) |
| `GET /review-queue/items/{id}` | `review:read` | Detail with the audit trail |
| `POST /review-queue/items/{id}/actions` | `review:resolve` | `{action, note?}` — must be permitted for the type |
| `PATCH /review-queue/items/{id}` | `review:resolve` | `{assignee}` — null unassigns |
| `GET /review-queue/summary` | `review:read` | Per-type counters (all nine, even zero), totals, oldest item, admin notifications; optional `company` filter |
| `GET /review-queue/sla-policies` | `review:read` | Effective policy per type |
| `PATCH /review-queue/sla-policies/{type}` | `settings:manage` | `{sla_hours, warn_hours}` |

Errors: `REVIEW_ITEM_NOT_FOUND` (404), `REVIEW_ACTION_NOT_PERMITTED`
(422), `REVIEW_INVALID_STATE` (409), `REVIEW_INVALID_SLA_POLICY`
(422), `INVALID_FILTER` (400, unknown type/severity/status).

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
  over in-memory ports (no DB): the nine types, permitted actions,
  default sort, filters, SLA math, summary counters, admin
  notifications, action lifecycle, SLA validation.
- `tests/interfaces/test_rest_review_queue.py` — real app + auth +
  PostgreSQL: all nine types returned by the API (one sample item per
  type), filters, sort, pagination, actions, assignment, summary,
  SLA policies, tenant isolation.
- `apps/admin/features/review-queue/review-queue-data.test.ts` —
  Persian labels, age formatting, SLA state.
- `apps/admin/e2e/review-queue.spec.ts` — live flow: seed nine items
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
