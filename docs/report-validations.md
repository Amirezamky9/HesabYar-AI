# Report Validations (US-403)

Financial-report validation against the Iranian standards, with full
source provenance (design-eng spec §7.4, §12.1; ARCHITECTURE.md §9).

A report validation run validates the company's **live trial
balance** — read from POSTED moves in the engine — against the
standards rules, and persists a **report snapshot** so every figure
in the run is traceable to the engine read that produced it. Only the
trial balance closes the Phase 2 `get_report` gap; P&L, balance sheet
and cash flow stay honestly deferred (they are not built here).

## What lives where

| Piece | Location |
|---|---|
| Table | Alembic `0008`: `report_snapshots` (validation runs reuse the US-401 `validation_runs` / `validation_results` tables) |
| ORM model | `src/hesabyar/infrastructure/persistence/postgres/models.py` (`ReportSnapshotModel`) |
| Snapshot port implementation | `src/hesabyar/infrastructure/persistence/postgres/report_snapshot_repository.py` (`SqlAlchemyReportSnapshotRepository`) |
| Domain | `src/hesabyar/domain/reports/` (`models.py`, `ports.py` — incl. `ReportSnapshotPort`) |
| Trial-balance views + port method | `src/hesabyar/domain/accounting/value_objects.py` (`TrialBalanceView`) and `ports.py` (`AccountingEnginePort.get_trial_balance`) |
| Adapter read | `src/hesabyar/infrastructure/accounting/tryton/adapter.py` (`TrytonAccountingAdapter.get_trial_balance`) |
| Query service | `src/hesabyar/application/queries/reports.py` (`ReportQueryService`) |
| Validation service | `src/hesabyar/application/commands/reports.py` (`ReportValidationService`) |
| REST | `src/hesabyar/interfaces/rest/routers/reports.py` (+ `schemas/reports.py`) |

## Trial balance (posted moves only)

`GET /api/v1/reports/trial-balance?company=<id>&date_from=...&date_to=...`
(scope `reports:read`) aggregates every `account.move.line` whose move
is `posted`, belongs to the company and is dated in the range. Draft
moves never contribute.

- The read pages through the engine's **complete** line set (500 rows
  at a time until a short page); no figure is ever computed from a
  paginated partial read.
- The company must exist and book in IRR (HesabYar is Rial-only):
  a missing company is `ENGINE_NOT_FOUND`, a non-IRR company is
  `ENGINE_VALIDATION_FAILED`.
- The response carries per-account debit/credit/balance (ordered by
  account code), totals, move/line counts, the balance check and the
  `source_endpoint` (`account.move.line (Tryton REST)`).

## Report validation runs

`POST /api/v1/report-validation-runs` (scope `standards:validate`)
validates the live trial balance for a company and period and persists
the run **and** its snapshot in one transaction:

1. The service fetches the trial balance through the engine port.
   An unbalanced trial balance raises `TRIAL_BALANCE_UNBALANCED`
   (502) — no run is ever built on unbalanced figures.
2. It builds the DSL document from the trial balance
   (`trial_balance` namespace with per-account balances under
   `trial_balance.accounts` keyed by account code, plus top-level
   `total_debit` / `total_credit`) and delegates rule execution to the
   US-401 `StandardsValidationService` — so the Standard 15
   `BLOCKED_SOURCE_GAP` gate, effective-date applicability and
   coverage reporting are exactly the same as a plain validation run.
3. It persists the snapshot: `snapshot_id`, `tenant_id`, `company_id`,
   `validation_run_id`, period, `as_of_date` (the period end),
   `source_endpoint`, `ruleset_version`, `payload_hash` (SHA-256 of
   the canonical document — equal to the run's `input_hash`) and the
   `payload` (the document itself).

`GET /api/v1/report-validation-runs/{run_id}` (scope `reports:read`)
returns the run with per-rule results, coverage and provenance. A run
with no trial-balance snapshot (e.g. one created by the plain
`POST /validation-runs` endpoint) or of another tenant answers 404
`REPORT_VALIDATION_RUN_NOT_FOUND`.

## Honest limits

- A trial balance alone does not contain statement-level figures
  (`total_assets`, `revenue`, ...), so the statement rules that need
  them yield **ERROR, never PASS** — the DSL enforces this and the
  service preserves it. A run over a trial balance therefore reports
  35 ERRORs (the 35 executable Standard 1 rules) and `CF005` as not
  executed, with a Persian note on the response explaining why. The
  snapshot payload is the document that was evaluated, so the gap is
  reproducible, not hidden.
- Rules distinguish executed (`PASS`/`FAIL`/`ERROR`) from skipped
  (`NOT_APPLICABLE`, e.g. a Standard 44 rule for a 1404 period) from
  not executed (no DSL formula). Coverage is reported separately:
  corpus vs implemented vs verified vs executed.
- Standard 15 blocks 1405+ periods with 422
  `STANDARD_SOURCE_BLOCKED` (same gate as US-401). Standard 43 rules
  apply from 1404/01/01, Standard 44 rules from 1405/01/01.
- No hard-coded tax rates or annual parameters; every figure comes
  from the live trial balance.
