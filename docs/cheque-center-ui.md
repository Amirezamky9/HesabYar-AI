# Cheque center UI (US-535) — build summary

Cheque center pages + all 17 reports (§7.5) + treasury widgets (§7.3)
for Phase 5-4 (۵ث, story US-535, the last story of the phase). The
backend API surface is US-530–534's deliverable (see `docs/cheques.md`,
`docs/sayyad-reconciliation.md`); this story is the admin UI against
it. No backend code was changed except the dev-key mint script (see
«Backend fix» below); no migration was needed (lane-1 dev DB already
at Alembic 0026 with all cheque tables).

## What was built

- **API client** (`apps/admin/lib/api-client.ts`): cheque types
  mirroring the Pydantic schemas (`ChequeResponse`,
  `ChequeDetailResponse`, `ChequeSettingsResponse`,
  `CheckbookResponse`, `CollateralSummaryResponse`,
  `EndorsementSummaryResponse`, `ClearingAgingResponse`,
  `InquiryLedgerResponse`, `ReceiptRuleAuditResponse`,
  `ClearingLineDecisionResponse`, `ChequeClearingResponse`,
  `CreateChequeRequest`, `ConfirmReceiptRequest`,
  `InquiryReceiptRequest`) + ~30 `api.*` methods (list/get/create/
  settings/checkbooks/summaries/intake, all 14 transitions,
  receipts, Sayyad/Ehrazino inquiries, clearing confirm, aging,
  ledger, receipt-rule audit, run-clearing). All mutations pass a
  fresh idempotency key. `returnCheque` sends no body.
- **Shared cheque module** (`apps/admin/features/cheques/`):
  `cheque-data.ts` — Persian labels (states/kinds/purposes/
  storage/Sayyad status/colours/channels/inquiry types/clearing
  confidence, 18 Fintech scopes) + pure helpers (`dueBucket`,
  `clearingAgeBucket`, `intakeReminderState`, `cashForecast` —
  12 weekly buckets, collateral never enters — `lifecycleSteps`,
  `CHEQUE_REPORTS` catalog of all 17);
  `cheque-shared.tsx` — `ChequeStateBadge`, `chequeSourceLabel`
  (presence of `source_event_id` → «سایت», else «دستی»),
  `chequeTableColumns`, `applyChequeFilters`;
  `cheque-report-charts.tsx` — `BarChart`, `DonutChart`
  (conic-gradient, token colours), `AreaChart` (two-series SVG);
  every charted value also appears in a table.
- **Pages** (routes under `app/(dashboard)/cheques/`):
  - `cheque-center-page.tsx` (`/cheques`, report 9's home) —
    tabs همه/دریافتی/پرداختی/پشتوانه, server filters
    (state/kind/purpose/due_before) + client filters, summary
    cards, CSV export, offset pagination (page size 50).
  - `cheque-new-page.tsx` (`/cheques/new`) — manual registration
    (24-field payload, Toman amount, Jalali pickers → Jalali
    strings, issued requires a checkbook, collateral requires a
    secured obligation, unregistered-Sayyad warning per ruling 870).
  - `cheque-detail-page.tsx` (`/cheques/[id]`) — specs, warnings,
    lifecycle timeline, custody log, inquiry receipts, all
    state-appropriate transitions + receipt/Sayyad/Ehrazino forms.
    Accountant-gated actions show their gated state and never
    silently post a journal: call-in needs an approving
    accountant; endorse-to-other and unexpected-call-in show
    «no journal posted, awaits accountant».
  - `cheque-intake-page.tsx` (`/cheques/intake`) — intake queue
    (report 11 rows): oldest first, `age_days` + reminder state,
    deadline from settings.
  - `checkbooks-page.tsx` (`/cheques/checkbooks`) — per-checkbook
    cards with used/voided/remaining bar (report 8).
  - `cheque-reports-page.tsx` (`/cheques/reports`) — all 17
    reports (see table); deep links `?report=<id>` from the
    dashboard widgets and the center page.
- **Treasury widgets** (`apps/admin/features/dashboard/`):
  `ChequeExposureWidget` («چقدر چک دادیم» + «چقدر چک پشتوانه
  کردیم», kept strictly apart), `ChequeInClearingWidget`
  (aging endpoint: count, oldest days, stale alert),
  `ChequeDueSoonWidget` (received commercial cheques due within
  7 days or overdue). All three are failure-tolerant like the
  review-queue widget. **Tenant-wide, not company-scoped**: the
  cheque list has no company filter in the API, so the tiles say
  «همهٔ شرکت‌ها · همهٔ دوره‌ها». Real withdrawable balance has
  no endpoint, so it is NOT shown as a number.
- **Navigation** (`apps/admin/components/layout/navigation.ts`):
  «مرکز چک» module (children: مرکز چک `/cheques`, ثبت چک
  `/cheques/new`, در انتظار برگ `/cheques/intake`, دسته‌چک‌ها
  `/cheques/checkbooks`, گزارش‌های چک `/cheques/reports`) +
  `ROUTE_LABELS` entries.

## The 17 reports (§7.5)

| # | Report | Source | Status |
|---|--------|--------|--------|
| 1 | دریافتی‌های سررسید نزدیک/گذشته | registry list | live (bank/party filters, due buckets) |
| 2 | پرداختی‌های سررسید آینده («چقدر چک دادیم») | registry list | live (bank filter, monthly bars) |
| 3 | چک‌های برگشتی | registry list | live (bank filter, reason donut + monthly trend) |
| 4 | در جریان وصول | `in-clearing-aging` + list join | live (age buckets 0–7/8–14/15+, stale flag) |
| 5 | خرج‌شده/واگذارشده | `endorsement-summary` | live (supplier vs other split) |
| 6 | چک‌های پشتوانه («چقدر چک پشتوانه کردیم») | `collateral-summary` | live (memorandum totals + per-obligation) |
| 7 | ماندهٔ اسناد دریافتنی/پرداختنی | Tryton reference | **coming-soon** — §11.1 forbids client-side recompute when a Tryton report is the source; wiring deferred to ۵ح |
| 8 | صورت وضعیت دسته‌چک | `checkbooks` | live (donut + per-checkbook table) |
| 9 | دفتر چک | registry list | live (state/bank/party filters, CSV) |
| 10 | مغایرت سایت ↔ صیاد ↔ برگ | review queue (`cheque_exception`) + row flags | live (never auto-approve a mismatch) |
| 11 | چک‌های سایت و نرسیده | `intake` + settings deadline | live (reminder-state bars) |
| 12 | تطبیق وصول | `monthly-match-trend` (stored history, US-553) + run-clearing form | live (donut from stored totals + monthly trend line + monthly table; history starts at the first clearing run after deploy — see docs/cash-forecast.md) |
| 13 | دفتر استعلام‌ها | `inquiry-ledger` + receipt-rule audit button | live (channel bars, colour donut, costs) |
| 14 | پیش‌بینی نقدینگی چک | derived (`cashForecast`, 12 weeks) | live (area chart + weekly table; collateral excluded) |
| 15 | صورت وضعیت چکی مشتری | derived per-issuer selector | live (held / in-clearing / bounced history + latest colour with date) |
| 16 | چک‌ها به تفکیک محل نگهداری | derived (`storage_location`) | live (donut + per-location totals) |
| 17 | تاریخچهٔ انتقال و واگذاری | endorsed rows + per-cheque custody timeline | live (chain table + lifecycle + custody log + linked moves) |

Every report shows its source, chart type and filters in the header
plus a freshness line (source / generated time / company / unit:
تومان, amounts stored in Rial). Reports load all data once (list
limit 100, intake, aging, ledger, collateral, endorsement,
checkbooks, review items, settings).

## House rules kept

Persian RTL-first; VibeFarsi components only; Persian digits via
`fa()`; Jalali dates via `lib/jalali`; logical Tailwind only;
touch targets ≥44px. No fake data anywhere — the one report
without a backend is an honest coming-soon card, never invented
numbers. The BFF catch-all proxies everything to `/api/v1`, so no
new BFF route was needed. Dates in API responses are Jalali
`YYYY-MM-DD` strings (lexicographic order works); create-body
amounts are Toman (×10 to Rial at the backend boundary).

## Backend fix (US-530–534 defect, fixed in-branch)

`apps/admin/scripts/mint-dev-key.py` — the `OPERATOR_SCOPES` list
predated ۵ث, so a freshly minted dev key lacked
`cheques:read/write/transition` and every `/cheques` call answered
403. Fixed minimally in-branch: added the three scopes (they
already exist in the catalog,
`src/hesabyar/infrastructure/auth/scopes.py` lines 55–57).
Commit `a4c4a51` — kept separate from the UI commit. No tests
reference `OPERATOR_SCOPES`; the script has no pytest coverage.

## Verification (targeted only — no full suite)

- `tsc --noEmit`: 0 errors (19 fixed along the way: `Select`
  `onValueChange` takes `string | null`, so bare `setX` needs a
  `(v) => setX(v ?? default)` wrap; `setState` in effect bodies
  moved to the timer-callback pattern used by the settlement
  pages; DonutChart accumulator rewritten as a plain loop).
- `eslint` (cheques + dashboard + navigation + cheque routes):
  0 errors, 0 warnings.
- `vitest`: `cheque-data.test.ts` 13/13 green.
- `pnpm build`: green; routes `/cheques`, `/cheques/[id]`,
  `/cheques/checkbooks`, `/cheques/intake`, `/cheques/new`,
  `/cheques/reports` all present.
- Playwright `e2e/cheques.spec.ts` (against `pnpm start` on
  :3000, lane-1 backend on :8001, trytond on :8010): **9/9
  green** — center + nav, register received/collateral/issued
  via the UI form, center list, intake queue, checkbooks, all
  17 reports (report 7 coming-soon asserted), dashboard widgets.
  Two spec bugs fixed during the run (label substring collision;
  `issuer_bank` must start with the 3-digit bank code
  `056 سامان` — the 422 that exposed it).
- Python: `ruff check` on the mint script clean; repo-wide
  `uv run pyright` 0 errors / 0 warnings / 0 informations.
  Targeted pytest: N/A — the only Python file touched is the
  mint script, which has no pytest coverage; no backend code
  was changed, so no backend tests were run.
- Live probe (fresh dev key, `Authorization: Bearer`): all 7
  cheque endpoints 200 (`/cheques`, `/cheques/settings`,
  `/cheques/intake`, `/cheques/collateral-summary`,
  `/cheques/endorsement-summary`,
  `/cheques/reports/in-clearing-aging`,
  `/cheques/reports/inquiry-ledger`).

## Deferred / needs a decision

- Report 7 (notes balance): needs the Tryton reference report
  wired in ۵ح; the UI must not recompute it from partial data.
- Report 12 monthly match-trend: DONE in US-553 (stored
  `cheque_match_history`, migration 0033; trend renders from it).
  History starts at the first clearing run after deploy — no
  backfill (see docs/cash-forecast.md).
- Cheque widgets are tenant-wide: if the coordinator wants
  company-scoped cheque figures, the list endpoint needs a
  company filter (backend change, not UI).
- The dev DB had 0 cheques when the story started; the e2e spec
  seeds its own (unique Sayyad id per run) and leaves them in
  the lane-1 dev DB — harmless test rows, same as other stories'
  seeds.
- VM replacement during the story wiped `/tmp` and all servers;
  trytond :8010 + backend :8001 were restarted from the saved
  scripts/env and re-probed before Playwright. The first
  re-probe 401'd only because the probe used the wrong header
  (`X-HesabYar-Api-Key`); the backend authenticates with
  `Authorization: Bearer`. A fresh key was minted anyway and is
  saved in the worktree's `apps/admin/.env.local` (exactly one
  `HESABYAR_DEV_API_KEY=` line, chmod 600).
