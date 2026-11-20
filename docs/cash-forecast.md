# Cash forecast calendar + monthly match-trend (US-553)

The cash forecast is the §11.2 settlement-report horizon view:
expected inflows and outflows from **registered records only**,
shown as a monthly Jalali calendar plus a 30/60/90/120-day
forecast. Nothing is invented: where a source has no records, the
UI shows an honest empty state.

## Sources

| Direction | Source | What counts | Date used |
| --- | --- | --- | --- |
| Inflow | Gateway settlement tranches (US-521) | Open tranches (`expected`/`due`/`overdue`/`confirmed`/`partially_confirmed`/`partially_received`), outstanding net (`net_irr − received_net_irr`) | `expected_date` |
| Inflow | Sales installments (US-540) | Internal (non-BNPL) legs, uncovered balance only (principal − received − pending clearance) | leg `due_date` |
| Inflow | Received cheques (report 14 rule) | Commercial (`purpose=payment`) cheques, state `held`/`in_clearing` | `due_date` |
| Inflow | Due invoices | Posted customer invoices (`invoice_type=out`, state `posted`), amount `amount_to_pay` | move-line `maturity_date`, invoice date as fallback |
| Outflow | Issued cheques (report 14 rule) | Commercial cheques, state `issued` | `due_date` |
| Outflow | Payable installments / suppliers | Posted supplier invoices (`invoice_type=in`, state `posted`), amount `amount_to_pay` | move-line `maturity_date`, invoice date as fallback |
| Outflow | Packaging purchases (US-515) | Open purchase requests (`draft`/`quotation`) for packaging products only, priced quantity × cost price (ROUND_HALF_UP, whole Rial) | `supply_date` → `purchase_date` → `created_at` |

## Buckets: قطعی vs پیش‌بینی

Every item carries a bucket, and every aggregation keeps the two
apart:

- **قطعی (confirmed)** — a registered contract / cheque /
  installment / invoice / purchase request: a real record with a
  due date and an amount. All seven sources above are قطعی.
- **پیش‌بینی (forecast)** — expected sales with no registered
  record yet. **No source exists for it yet**, so the column is
  always empty — honestly so, and the UI says so («فروش
  موردانتظار هنوز منبع ندارد»). The model, API and UI carry the
  bucket so a future sales-forecast source can fill it without a
  schema change.

## Collateral cheques — never, in either column

Collateral cheques (`purpose=collateral`) appear in NEITHER the
قطعی nor the پیش‌بینی column — ever (§7.5). The rule is enforced
twice: the query service reads only commercial cheques
(`list_commercial_cheques`, the segregation seam), and the pure
builder re-checks `purpose == "payment"` before emitting an item,
so a future caller that passes the wrong list still cannot leak a
collateral cheque into the forecast. A dedicated unit test covers
it.

## Double-count guards

1. **BNPL legs are never items.** A BNPL leg is paid by a gateway
   tranche; the tranche is the cash. Counting both would double
   the inflow.
2. **A leg contributes only its uncovered balance.** The slice a
   cheque receipt covers while the cheque is still pending
   clearance comes through the cheque channel (the cheque's due
   date), not through the leg. A bounced cheque frees the leg
   again (the receipt counts as neither received nor pending).
3. **An invoice with an installment plan is excluded from the
   due-invoice source** — its cash is already the legs. The match
   tries both `str(engine invoice id)` and `invoice.number`
   against the plan's free-form `invoice_id` (the plan field is a
   string, not reliably the engine id — see CI risks).
4. **Report 14 is the calendar's cheque input, not a second
   source.** The backend mirrors report 14's rule (received
   held/in-clearing → inflow, issued → outflow, commercial only);
   the frontend's report 14 and the calendar read the same
   registry, so they agree.

## Horizon buckets

Relative to `as_of` (today): `overdue` (< 0 days), `0_30`,
`31_60`, `61_90`, `91_120`, `beyond_120`. The UI fetches
[today − 180d, today + 120d] in one request so the overdue bucket
sees items older than the current month; month navigation is
client-side, clamped to that window.

## Undated invoices

An invoice whose move lines carry no maturity date and whose
invoice date is missing is counted in `skipped_undated` and
reported in the UI — never silently dropped, never dated by a
guess. (In practice the invoice date is always present, so the
fallback covers it; the counter exists for the pathological case.)

## Endpoints

| Endpoint | Scope | Notes |
| --- | --- | --- |
| `GET /reports/cash-forecast?company=&date_from=&date_to=` | `reports:read` | Monthly calendar + horizon + items + totals; §11.1 freshness (`live`, calculation version `registered-records/1.0`) |
| `GET /cheques/reports/monthly-match-trend?months=12` | `cheques:read` | Stored monthly match history; freshness indicator `stored`, source `hesabyar:cheque_match_history`, source timestamp = newest row's `updated_at` |

Both honor tenant/company scoping (`resolve_accounts_company`;
unknown or other-tenant company → 404 `COMPANY_NOT_FOUND`).
`date_from > date_to` → 422; unknown query params → 422.

## Report 12: monthly match history (migration 0033)

Table `cheque_match_history`: one row per (installation, Jalali
month `YYYY-MM`) with `exact_count`, `probable_count`,
`ambiguous_count`, `unmatched_count`, `total_lines`,
`match_rate`. The clearing service (`ChequeClearingService.
match_statement`, US-534) records each run's **newly decided
lines only** — lines whose decision is `already_matched` are
excluded, so re-running a statement never inflates the month.
The write is upsert-add and fail-soft: a recording failure is
logged loudly but never fails the clearing run (the match
decisions themselves are the source of truth; the history is a
rollup).

The trend chart (donut of rates + monthly trend line, §7.5)
renders **from the stored history, never recomputed from raw
rows at read time**.

### No backfill — and why

Pre-existing `BankStatementMatch` rows cannot seed the history
faithfully:

- An **unmatched clearing line is indistinguishable** from a
  generic-reconcile unmatched line: both are
  `target_type=MANUAL` with an empty `target_ref`.
- **Suggested matches carry no decision timestamp**, so they
  cannot be attributed to a month.

So history starts with the first matching run after deploy, and
the UI says so honestly («هنوز هیچ تطبیق وصلی اجرا نشده است»)
until then. This is the documented choice the story allowed.

## UI

- `features/cash-forecast/cash-forecast-section.tsx` — the
  calendar section (month navigation, Jalali grid Sat–Fri,
  day-detail panel, horizon table, source breakdown, freshness
  line). Used by the reports tab and the standalone page.
- `app/(dashboard)/reports/cash-forecast/page.tsx` — standalone
  route (reports ← cash forecast).
- `features/reports/reports-page.tsx` — «پیش‌بینی جریان نقد» tab.
- `features/cash-forecast/dashboard-cash-card.tsx` — the
  dashboard card: next-30-days قطعی net only. Self-contained
  (own file, own fetch, one import + one JSX block in
  `dashboard-home.tsx`) so the US-551 dashboard rebuild can
  place it anywhere.
- Cheque report 12 (`features/cheques/cheque-reports-page.tsx`,
  `ClearingMatchReport`) reads the stored trend: donut from
  stored totals, `LineChart` of monthly rates, monthly table,
  stored-freshness line. The run-clearing form and the unmatched
  table are unchanged.

## Tests (CI only — never run on the VM)

- `tests/unit/test_cash_forecast.py` — pure builders: report-14
  cheque rule, collateral exclusion (dedicated test), tranche
  open states / outstanding, leg uncovered balance, invoice
  direction / undated counting, purchase-request rules, Jalali
  calendar grouping, horizon boundaries, bucket separation,
  `match_rate`, trend from stored rows.
- `tests/unit/test_cash_forecast_service.py` — service with
  fakes: double-count guards (BNPL, pending-clearance,
  bounced-cheque leg, plan-linked invoice by id AND by number),
  maturity dating + fallback, packaging pricing/date/state
  rules, forecast-bucket-always-empty, sorting, validation.
- `tests/unit/test_match_history.py` — recording on
  `match_statement` (counts, re-run no double-count, two runs
  accumulate, installation scoping), trend read, empty trend,
  months validation.
- `tests/interfaces/test_rest_cash_forecast.py` — REST: empty
  forecast + freshness, aggregation, scope 403s,
  COMPANY_REQUIRED 400, unknown-company 404, reversed-range
  422, unknown-param rejection, OpenAPI path listing, trend
  rendering / limit / validation / scope.
- `features/cash-forecast/cash-forecast-data.test.ts` — vitest
  for the calendar helpers (Jalali month range/shift, grid
  layout, labels, horizon totals).
- `e2e/cash-forecast.spec.ts` — Playwright: calendar page,
  month navigation, day click, reports tab, dashboard card,
  report 12. Resilient to empty AND non-empty dev DB (no seeded
  data, no figure assertions).

## CI risks

- **Invoice ↔ plan matching is a heuristic.** `InstallmentPlan.
  invoice_id` is a free-form string; the service matches it
  against both the engine invoice id and `invoice.number`. A
  plan whose `invoice_id` matches neither leaves its invoice in
  the due-invoice source (double count with the legs). If CI or
  the field's real usage shows a third form, extend the match.
- **Purchase-request pricing via cost price** assumes
  `get_product_cost_price` returns the current cost for the
  packaging product; a missing cost price drops the request
  (never guessed). If the engine returns stale costs, the
  outflow is stale too — the freshness block names the engine
  read.
- **Match-history backfill behavior** is "starts at first run
  after deploy" by design (see above); the trend is empty until
  a clearing run happens. The e2e spec asserts the empty state
  as a valid outcome.
- **Tranche/instalment read shapes** are the US-521/US-540
  repositories' existing shapes, reused without change; the
  service tests fake them. A repository change in a sibling
  story would surface in the service tests first.
- **Migration 0033 chains to 0031.** If a sibling story lands
  migration 0032 on the same branch, the coordinator merges the
  heads (0033's `down_revision` may need re-chaining).
