# Unified dashboard (US-551)

The dashboard home (`/`) is the unified view (§7.3): six live
sections, each with company + period + unit on every KPI, each KPI
linking to its filtered operational page (§11.3). The page is
`apps/admin/app/(dashboard)/page.tsx` (thin server wrapper) +
`features/dashboard/dashboard-home.tsx` (client fetch + layout) +
`features/dashboard/dashboard-data.ts` (pure summarizers, vitest
covered) + `features/dashboard/widgets.tsx` (cards).

## The six sections

| Section | What it shows | Source |
| --- | --- | --- |
| پولِ امروز | Today's succeeded receipts + payments, grouped by journal | `listPayments` (state filter is client-side: only `succeeded` counts; draft/approved/processing/failed excluded) |
| پولِ در راه | Gateway receivables + this month's expected settlement tranches + in-clearing received cheques + internal installments due this month | `listTranches` (all pages), `listInClearingAging`, plan details for ≤10 open internal plans |
| نیازمند توجه | Review-queue counters by type + oldest item + SLA-breached items | `getReviewQueueCounts` (US-504; read-only here — US-552 owns the queue page) |
| خزانه | Cashbox balances + issued cheques due this week + cheque exposure / in-clearing / due-soon widgets | `listCashBoxes`, `listCheques`, `getCollateralSummary`, `listInClearingAging` — all company-scoped |
| هشدارها | Overdue tranche, cheque due soon (3/7 days), overdue installment, packaging reorder crossed, cashbox uncounted, stuck inbox > 24h | `buildAlerts` over the section data; critical first |
| ایمپورت‌های دستی | Most recent import batches across both flows, with status + computed effect | `listStatementImports` (US-514) + `listSettlementImports` (US-523), merged newest-first |

Two more sections follow: پیش‌بینی نقدینگی (a clearly-marked
placeholder — US-553 adds the cash forecast calendar there; do not
build it here) and وضعیت کلی (the US-302 overview widgets: drafts,
receivables, payables, recent invoices/payments, setup).

## Company filter

The dashboard scopes to the shell's active company. Cheque figures
use the backend company filter added in this story: `GET /cheques`,
`GET /cheques/collateral-summary` and
`GET /cheques/reports/in-clearing-aging` accept an optional
`company` query param (HesabYar row id or engine company id);
absent means tenant-wide. The filter is applied at the repository
level (`ChequeRepositoryPort.list(company_engine_id=...)`), never
client-side. The cheque center (`/cheques`) has a company select
with «همهٔ شرکت‌ها» (tenant-wide) as the default.

Two honest gaps:

- **Installments have no backend company filter.** The dashboard
  filters plans client-side by `plan.company_engine_id` (≤10 open
  internal plans; BNPL excluded — their money arrives via gateway
  tranches, counting both would double-count). The overdue alert is
  scoped the same way via a plan-id join; if either call fails, the
  alert is omitted rather than shown tenant-wide under a company
  label.
- **Settlement tranches and the stale-inbox alert are
  installation-wide** (no company field in those APIs). The widgets
  say so in a muted note.

The cheque reports page (`/cheques/reports`) still calls the cheque
endpoints tenant-wide — out of scope for US-551; a follow-up should
thread the company filter through it.

## Honest empty / coming-soon policy

Where a figure's source does not exist, the widget shows an honest
gap — never a zero presented as a real figure:

- **Bank book balances / real withdrawable balance:** no API source
  exists, so the treasury widget shows a coming-soon row explaining
  it. (A trial-balance mapping was considered and rejected: guessing
  bank account codes would fabricate a balance.)
- **A failed section call:** the dashboard wraps every section call
  in `tolerant()` — one failed source never takes the page down.
  The affected widget shows «در دسترس نیست» for that figure, and
  the alerts/imports widgets name the failed sources in a muted
  note. Only the core batch (drafts, invoices, payments) hard-fails
  the page.
- **Empty states** say what is empty («در این ماه پولی در راه نیست»),
  not zero.

## Follow-ups

- **US-554** replaces the merged import view with the batch model
  (one flow, accounting-effect preview). The current widget shows
  what exists today (statement + settlement imports).
- **US-553** fills the cash-forecast section (calendar, 30–120 days,
  confirmed vs forecast). The placeholder carries an
  `INTEGRATION POINT` comment marking the exact block.
- **Cheque reports page** company filter (see above).
- **Restricted keys:** a company-restricted key calling a list
  endpoint without `company=` still sees tenant-wide results (the
  task's literal default); single-cheque endpoints already enforce
  403. Tightening the list default is a product decision, not a bug
  fix — raise it with the coordinator before changing it.
- **Dev key scopes:** `apps/admin/scripts/mint-dev-key.py` now
  includes `installments:read` + `installments:write` (the
  dashboard's installments figures and the US-545 spec need them).
  Keys minted before this change must be re-minted.

## Tests

- Backend: `tests/unit/test_cheque_company_filter_us551.py`
  (8 tests), `tests/interfaces/test_rest_cheques.py` (8 REST tests),
  `tests/infrastructure/test_cheque_repository.py` (SQL-level filter
  test).
- Frontend: `features/dashboard/dashboard-data.test.ts` (helpers),
  `e2e/dashboard.spec.ts` (rewritten for the new dashboard +
  current constants: 153 accounts, 114 postable, FY 2027, 24 open
  periods, standards nav live; file timeout 180s).
