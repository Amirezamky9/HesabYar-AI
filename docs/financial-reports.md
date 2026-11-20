# Financial reports (US-403 trial balance, US-550 statements)

The five §11.2 financial reports plus cheque report 7, all read live
from Tryton through the `AccountingEnginePort` — never recomputed
client-side from a HesabYar cache (§11.1). The engine is the only
ledger authority (ADR 0002).

## Reports and their sources

| Report | REST endpoint | Engine read | Calculation version |
| --- | --- | --- | --- |
| Trial balance | `GET /reports/trial-balance` | `account.move.line` (posted, paged) | `tryton-posted-lines/1.0` |
| General ledger | `GET /reports/general-ledger` | `account.account` Function fields (`debit`/`credit`/`balance`) + `account.move.line` (detail) | `tryton-posted-lines/1.0` |
| Balance sheet | `GET /reports/balance-sheet` | `account.account` `balance` with `date` context | `tryton-account-balance/1.0` |
| Income statement | `GET /reports/income-statement` | `account.account` `balance` with `from_date`/`to_date` context + `account.move.line` (monthly) | `tryton-account-balance/1.0` |
| Cash flow | `GET /reports/cash-flow` | `account.move.line` on cash accounts (direct-method derivation) | `direct-method/1.0` |
| Notes balance (cheque report 7) | `GET /reports/notes-balance` | `account.move.line` on the notes accounts + `account.move.reconciliation` | `tryton-aged-balance-rule/1.0` |
| Cash forecast (§11.2 settlement horizon) | `GET /reports/cash-forecast` | HesabYar registered records (tranches, legs, cheques, invoices, purchase requests) — NOT a Tryton report | `registered-records/1.0` |

Every response carries the §11.1 freshness block: source, source
timestamp (= generated-at for live reads), generated-at, company +
period, report version (`1.0.0`), calculation version, freshness
indicator (`live`), and reproducibility metadata (engine, engine
version, source endpoint, company, period, filters). The service
(`application/queries/reports.py`) stamps it; the schemas fail loudly
if a view lacks it.

All endpoints need scope `reports:read` and honor tenant/company
scoping through `resolve_accounts_company` (unknown or other-tenant
company → 404 `COMPANY_NOT_FOUND`).

## Why statements read account balances, not type `amount`

Tryton's balance-sheet / income-statement reference report
(`account.account.type.statement`) aggregates the `account.account.type`
`amount` Function field. That field flips sign only when the type has
`assets=True` — and the universal chart sets that flag
*inconsistently*: it is set on accounts-receivable, inventories,
prepaid and non-current-asset types, but NOT on cash, petty cash,
current assets, or the Assets root (verified against the
`trytond-account` 8.2.2 source and the chart XML). Raw type `amount`
values are therefore sign-inconsistent across the tree.

The statements instead read each account's `balance` Function field
(the same figure the type statement aggregates — no `debit_type` /
`credit_type` exists in the chart, so move-line aggregation by
account type is exactly consistent with type amounts) and group by
type in the pure domain builders
(`domain/reports/statements.py`), with one consistent sign
convention (debit minus credit; display flips per section). This also
gives account-level statement lines, which the type report cannot.

## Per-report behavior

### General ledger

- Without `account_id`: summary mode — one row per account with
  posted activity (opening balance + debit/credit totals), built
  from two `account.account` searches (period `debit`/`credit`,
  opening `balance` at the day before). `lines` is empty and
  `move_count`/`line_count` are 0.
- With `account_id`: detail mode — the account's posted lines with
  running balances and move numbers (batch-read). Unknown account →
  404 `ACCOUNT_NOT_FOUND`.
- Only POSTED moves contribute (contract-tested with a draft move).

### Balance sheet

- Sections are the top-level balance types (Assets, Liabilities,
  Equity / capital); accounts group under their leaf type. The
  current-period result (income accounts' net) is a line inside
  equity, so the sheet balances whenever the ledger does.
- Off-balance (7.x memorandum) accounts are excluded; typeless
  accounts are excluded (every chart account has a type).
- Comparison defaults to the same Jalali date one year earlier
  (Esfand 30 clamps to 29 in a common year).
- **Known behavior:** the `balance` Function field with a `date`
  context sees the fiscal year containing the date plus closed
  years' deferrals — exactly like Tryton's own balance sheet. A
  balance sheet as of a date in FY 2027 on a database where FY 2026
  was never closed shows FY-2027-only activity. Close the fiscal
  year to carry balances forward.

### Income statement

- Revenue/expense classification is data-driven by each income
  type's net sign over the period (net credit → revenue, net debit
  → expense, zero → neither). The monthly breakdown reuses the
  same classification, so monthly figures always sum to the
  statement totals (`monthly_ties_out`).
- Monthly buckets are Jalali months, from ONE paged move-line
  search over the income accounts (no per-month reads).
- Comparison defaults to the same Jalali period one year earlier.

### Cash flow

Tryton has **no cash-flow reference report** (verified: no such
report exists in `trytond-account` 8.2.2), so the sections are a
documented HesabYar direct-method derivation from the engine's
posted cash moves — calculation version `direct-method/1.0`, so a
rule change is visible in the freshness block.

Each cash line is classified by its **largest non-cash
counterpart's** account code (`classify_cash_flow_section`):

- investing: fixed assets / intangibles / accumulated depreciation
  (`1.5.x`–`1.9.x`) and the investment accounts (`1.1.2`, `1.1.4`);
- financing: financial liabilities / loans (`2.3.x`) and equity (`3.x`);
- transfer: another cash account (`1.1.x`) — excluded from every
  section (cash-to-cash moves no cash in or out);
- operating: everything else that touches cash in the normal course
  of business (receivables, payables, inventory, accruals, income,
  expenses, taxes);
- unclassified: memorandum (`7.x`) accounts and unknown codes —
  counted and reported, never silently dropped; they break the
  tie-out loudly (`ties_out` is false while any exist).

Cash accounts are the accounts whose type is "Cash and Cash
Equivalents" or "Cash at bank, Petty Cash, Cash On Hand" (1.1.1,
1.1.3, 1.1.5, 1.1.6 in the chart). The tie-out (opening + sections
== closing) is asserted on the response.

### Notes balance (cheque report 7)

- Open posted lines on the notes accounts (1.2.4 Notes Receivable
  Held, 1.2.5 Notes Receivable in Clearing, 2.1.5 Notes Payable),
  grouped by party + account.
- Aging follows the Tryton `account.aged_balance` rule exactly: a
  line ages from its maturity date (falling back to the move date);
  a line is open while unreconciled or reconciled after the as-of
  date. The `account.aged_balance` model itself is NOT read
  directly — it cannot filter by account, so it would mix trade
  receivables/payables in.
- **Judgment call:** Tryton's model puts lines maturing after the
  as-of date in the balance but in no term. Here they get a
  `not_yet_due` bucket, so the buckets always sum to the row total.
- A company whose chart lacks the notes accounts reads an honest
  empty report (`accounts_read` is empty; the UI says so).

## Freshness and comparison defaults

See `application/queries/reports.py`: `_stamp` (freshness block),
`_one_jalali_year_earlier` (comparison defaults). Point-in-time
reports set both period bounds to the as-of date.

## UI

`apps/admin/features/reports/`: `financial-sections.tsx` (trial
balance, balance sheet, P&L, cash flow), `general-ledger-section.tsx`
(summary + detail, searchable account picker), `report-charts.tsx`
(grouped monthly bars, cash-flow waterfall), `report-csv.ts`
(exports — every export prepends a `#` filter-header block with the
report title, company, period and applied filters, AC 7).
Trial-balance rows drill down into the ledger for that account.
Cheque report 7 lives in `features/cheques/cheque-reports-page.tsx`
(`NotesBalanceReport`). The cash forecast is documented separately
in `docs/cash-forecast.md` (US-553): its UI is
`features/cash-forecast/` (calendar section, dashboard card) plus
the reports-page tab and the `/reports/cash-forecast` route.

## Tests

- `tests/unit/test_reports_us550.py` — builders (sections, totals,
  balance check, net income, classification incl. sign flips,
  cash-flow classification, aging buckets, Jalali months) + service
  (freshness, comparison defaults incl. Esfand clamp, validation,
  unknown company).
- `tests/interfaces/test_rest_reports.py` — the five endpoints
  (auth, scope, tenant scoping, 404s, 422s, cross-checks against
  the trial balance, freshness shape, OpenAPI paths).
- `tests/contract/test_tryton_adapter_financial_reports.py` — the
  adapter reads against live Tryton, each cross-checked against an
  independent read.
- `apps/admin/features/reports/financial-report-data.test.ts`,
  `report-csv.test.ts` — helpers + exports (filter header).
- `apps/admin/e2e/reports.spec.ts`, `e2e/cheques.spec.ts` — the five
  live reports, the trial-balance drill-down, the CSV filter header,
  and cheque report 7.

## CI risks

The integration points verifiable only by reasoning until CI runs
them:

- `account.account` Function-field reads (`balance`, `debit`,
  `credit`) with `date` / `from_date` / `to_date` context over the
  Tryton REST API — the trial balance already reads Function fields
  with context, but the `date`-context balance read is new.
- `account.account.type` readability over REST (names, parents,
  statements) — needed for the statement builders.
- Reconciliation-date reads (`account.move.reconciliation`) for the
  notes open-line rule.
- The balance-sheet `balanced` assertion in the REST/contract
  tests assumes every posted move on the Adapter Test company falls
  in FY 2026 (see the known behavior above).
- The cash-flow `ties_out` / `unclassified_count == 0` assertions
  assume no cash move on the test company has a memorandum or
  unknown-code counterpart.
