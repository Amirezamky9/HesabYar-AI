# Data migration + accounting reconciliation (US-705)

How a store's data moves into a fresh HesabYar installation, and
how the migrated books are proven to reconcile. Spec §17 phase 7;
phased design §1.3 phase 7.

This document is about migrating a STORE's data in — not about
upgrading HesabYar itself (that is
`docs/runbook-upgrade-rollback.md`). The migration runs once per
store, at onboarding; the reconciliation checks run at the end of
it, and again after any bulk re-import.

**The one rule:** a discrepancy is a review item, never a silent
adjustment. Nothing in this procedure edits a number to make a
check pass — a failing check means the migration is not done.

## 1. What migrates, and through which door

Every piece of data enters through an existing application flow —
never by direct database write (spec §19 rules 2 + 8, and the
upgrade runbook's rule 4). The doors:

| Data | Door | Notes |
|---|---|---|
| Company + chart of accounts | Setup wizard / `hesabyar_setup` `install_chart` + `update_chart` | The Iranian chart extension (1.2.4/1.2.5 notes, 1.2.9 bounced cheques, cash-box accounts) comes from the module, not from the store's old system |
| Fiscal years + periods | Setup wizard (REST/JSON-RPC wizard path) | See `docs/setup-wizard.md` |
| Orders, payments, refunds | Generic connector backfill (§2) | Draft by default (§6) |
| Cheques (received/issued) | Connector backfill (`cheque.registered`) + cheque intake | Sayyad ids, custody, storage location — see `docs/cheques.md` |
| Bank statements | Manual import batches (§3) | Draft statements; no statement auto-post exists |
| Gateway settlement files | Manual import batches (§3) | Tie-out + auto-post only with the explicit opt-in |
| Opening balances | Draft journals in Tryton (§3) | Posted by the accountant, never by the migration |
| Parties, products, mappings | Mapping center + connector matching | Unmapped entities become review items, not guesses |

What does NOT migrate: the old system's internal ids (HesabYar
keeps its own; the external-id map records the correspondence),
passwords, and gateway credentials (re-entered in the vault).

## 2. The generic connector backfill

The store exposes the generic contract (`docs/store-connector.md`
Part II): an HTTP changes API (`GET {api_base_url}/changes`) or,
when the API is unavailable, a read-only store-DB read. Both feed
the same `EventIngestService` path as webhooks.

For a migration, the backfill runs with a wide window
(`updated_since` at the migration's start date) and pages through
history. The properties that make it safe to re-run:

- **Same idempotency keys as webhooks.** A change row's
  `event_id` MUST be the id the store sends as `source_event_id`
  on the corresponding webhook — a re-polled order dedups against
  the webhook-delivered one (three §10.4 unique constraints +
  the source→settlement-event mapping). Re-running a backfill
  never duplicates an accounting effect.
- **Out-of-order pages are rejected, not ingested.** HesabYar
  validates non-decreasing `updated_at`; a store that reorders
  history fails the sync fail-closed.
- **Unmapped changes are listed, not dropped.**
  `bank_transfer.recorded` / `shipment.dispatched` have no
  canonical event yet — their rows are listed by polling and
  visible, never silently skipped.
- **Money converts at the boundary only.** Amounts arrive as the
  store prices them; HesabYar converts Toman→Rial (×10) during
  pipeline normalization, per the store's `prices_in_toman`
  setting. A missing money field means "unknown", never zero.

Practical notes:

- Run the backfill in bounded pages (`page_size` ≤ 500) and watch
  the inbox drain between pages — a 10,000-order history ingested
  in one burst will look like an outage in `/monitoring` (it is
  not; it is a queue doing its job). The `scheduler.store_sync`
  job polls every 15 minutes; a migration backfill runs on demand
  (`POST /integrations/{connector}/sync`), not by waiting for the
  scheduler.
- The identity hazard applies: the backfill must use the SAME
  `source_store_id` webhooks use, or orders duplicate under
  different idempotency keys (`docs/scheduler.md`, store sync).
  Confirm the store key before the first page.
- Cheque rows need their Sayyad ids (16 digits) and storage
  locations; a cheque without a Sayyad id imports as a review
  item, not as a cheque with a fabricated id.

## 3. Manual imports + opening balances

### 3.1 Bank statements and settlement files

Through the unified batch cycle (`docs/manual-imports.md`):
upload → mapped → validated → previewed → `awaiting_review` →
approved → posted. The batch wraps one underlying import; parse,
validation, dedup, matching, the policy gate and the tie-out all
stay in the existing import services.

- The accounting-effect preview is computed by the SAME code as
  the real posting — review the preview, not a second calculator.
- Approve drives the underlying import through exactly the path
  an automatic import takes. Settlement approve runs the shared
  tie-out and auto-posts ONLY with explicit `auto_post=true`
  (ADR 0003); without it the batch stays `approved` with
  `posting_status=draft_hold`. Statement approve always writes a
  DRAFT statement (no statement auto-post exists).
- The batch variance report (`GET /imports/batches/{id}/variance`)
  is the per-batch reconciliation: expected vs imported vs
  posted, with the tie-out result. A batch whose variance does
  not tie out stays open — it is not forced through.
- Upload dedupes on the file hash: re-uploading the same file
  returns the existing batch with `duplicate=true`.

### 3.2 Opening balances

Opening balances enter as **draft journals** in Tryton (created
through HesabYar's journal-draft flow, validated, then posted by
the accountant). They are the store's closing balances from the
old system, as of the migration date — one draft per account (or
one consolidated draft, per the accountant's preference), dated
the day before go-live.

- Opening balances are Rial integers (digits=0). A legacy balance
  quoted in Toman converts ×10 ONCE, explicitly, at entry — and
  the conversion is recorded on the draft (never silent).
- The opening-balance journals MUST balance (debit == credit) —
  Tryton refuses an unbalanced move, and the trial-balance check
  (§4) would fail anyway. An unbalanced opening position means
  the legacy figures are incomplete: find the missing side, do
  not plug it.
- Opening balances post BEFORE the reconciliation checks run:
  the checks verify the migrated books including their opening
  position, not a half-migrated state.

## 4. Reconciliation checks (executable)

The checks live in
`src/hesabyar/infrastructure/operations/migration_reconciliation.py`
(`run_migration_reconciliation`) and are tested in
`tests/unit/test_migration_reconciliation_us705.py`. Four of the
five are the US-703 drill checks, reused — a migration is
verified the same way a restore is:

| Check | What it proves | Source |
|---|---|---|
| `hesabyar.orphaned_documents` | No imported row references a missing row (generic FK sweep over every HesabYar table) | `restore_drill.check_orphaned_documents` |
| `hesabyar.settlement_tranches` | Every payment's tranches tie out (Σ net == payment net, Σ gross == payment gross, progress in bounds) | `restore_drill.check_settlement_tranches` |
| `cross_database.external_references` | Every external-id-map row pointing at Tryton resolves in the live Tryton DB (REST adapter, never SQL) | `restore_drill.check_external_references` |
| `tryton.trial_balance` | Every company's trial balance balances (posted moves only) | `restore_drill.check_trial_balance` |
| `migration.notes_aging` | The notes-receivable/payable aging ties out: per party-row Σ buckets == row total; the totals row's buckets == its total AND == Σ row totals | NEW (US-705) — the drill does not run an aging check |

Run them after the migration (and after any bulk re-import).
Every check returns PASS, FAIL or SKIPPED with a detail; an
unexpected error inside a check FAILS it — a check never aborts
the run, and a failure never silences the other checks.

```python
from hesabyar.infrastructure.operations.migration_reconciliation import (
    run_migration_reconciliation,
)

results = run_migration_reconciliation(
    hesabyar_engine=engine,       # SQLAlchemy engine for the HesabYar DB
    tryton_engine=adapter,        # AccountingEnginePort (Tryton REST)
    as_of=FiscalDate(...),        # the migration date
)
failed = [r for r in results if r.status is DrillCheckStatus.FAIL]
```

(A CLI entry point for this runner is deferred — the checks run
from a Python session or a notebook today. The drill CLI
(`scripts/backup.py drill`) covers the same checks against a
restored backup pair; see §7.)

### 4.1 Checks that cannot be scripted (manual, still required)

- **Opening-balance tie-out against the old books.** The scripted
  trial-balance check proves the books BALANCE; only a human can
  prove they MATCH the legacy system's closing trial balance.
  Compare the two reports line by line (same date, same accounts)
  and record the comparison. A difference is a discrepancy (§5),
  not a rounding error — Rial integers do not round.
- **Row counts.** Imported orders / payments / cheques vs the
  store's own counts for the migrated window (the sync summary
  counts + the store's export). A count mismatch means rows were
  dropped or duplicated — find them before go-live.
- **Per-batch settlement tie-out.** The batch variance report
  (§3.1) per settlement file — the tranche check proves the
  STORED tranches tie out; the variance report proves the FILE
  tied out when it was imported.

## 5. Discrepancy handling (review queue, never silent)

Every failed check becomes a review item — the operator files it
with the check's detail attached (the runner reports; it does not
file, because filing needs a tenant and the review service, and a
reconciliation report must stay readable without one). The item
type follows the discrepancy:

| Discrepancy | Review item type | Permitted actions (see `docs/review-queue.md`) |
|---|---|---|
| Settlement tie-out / tranche mismatch | `settlement_variance` | accept_variance, escalate_to_gateway, fix_fee_mapping |
| Bank statement row unmatched | `bank_unmatched` | manual_match, record_misc_income, snooze |
| Unmapped store entity (party/product/account) | `mapping` | complete_mapping_retry, auto_create_party_item, skip |
| Import batch problem (bad rows, failed preview) | `manual_import_review` | approve_preview, fix_rows_recalc, cancel_batch |
| Event failed in the pipeline | `inbox_dead_letter` | replay, fix_source_retry, close_with_reason |
| Trial balance does not balance | — (escalate, no review type fits) | STOP: a unbalanced ledger is a developer + accountant investigation, not a queue item. Do not post a "plug" journal to force a balance |

Rules:

1. **Fix through the normal flows** — re-ingest, re-map, a
   correcting journal — never by hand-editing either database.
   A hand edit is unrepeatable and unverifiable.
2. **Re-run the reconciliation until every check passes.**
   `fully_verified` (no FAIL, no SKIP) is the go-live gate; a
   SKIPPed check (e.g. a company with no notes accounts) is
   acceptable only when the detail explains WHY, and the reason
   is recorded.
3. **`accept_variance` is a decision, not a default.** Accepting
   a settlement variance writes off real money — it needs the
   accountant's explicit decision, recorded on the item.
4. **Never delete a discrepancy.** A rejected batch keeps its
   rows and its reason (`rejected → archived`, re-runnable); a
   closed review item keeps its audit trail. The migration's
   history is the audit trail of what was decided.

## 6. Currency + posting rules (non-negotiable)

- Money is Rial only (`MoneyIRR`, digits=0). Toman→Rial (×10)
  happens at the connector boundary during normalization, and
  at opening-balance entry (§3.2) — nowhere else, never silently
  (spec §19 rule 7).
- No binary floats for money, anywhere in the migration
  (spec §19 rule 6). A legacy export with decimal amounts is
  rejected or converted explicitly to integer Rial first — the
  conversion is part of the mapping, recorded, and reviewed.
- **Draft by default.** The migration creates drafts (invoices,
  statements, journals); nothing auto-posts without the
  installation's explicit opt-in per event type (ADR 0003
  decision 1). Go-live does NOT flip auto-post on — that is a
  separate, reversible decision the store owner makes after the
  reconciliation passes.
- No duplicate posting from repeated events (spec §19 rule 9):
  the backfill's idempotency (§2) is what makes re-runs safe.

## 7. What is NOT tested (honest limits)

- **No end-to-end migration against a real store runs in CI.**
  No live store exists; the connector contract is tested with
  fakes and the simulator, and the reconciliation checks are
  tested against SQLite + a fake engine port. The first real
  migration is a supervised event: the operator runs the
  backfill, the accountant runs the reconciliation, and both
  watch the review queue.
- **No real legacy export has been imported.** Legacy formats
  vary per store; the manual-import column mappings are
  per-source by design, and each new source's mapping is
  validated by its preview before approval.
- **The manual comparisons (§4.1) are human by definition.**
  No test can compare two reports across two systems — the
  procedure requires the comparison and the record, and the
  review queue holds the discrepancies.
- **The reconciliation runner has no CLI yet.** The checks run
  from Python (or via the drill CLI against a restored backup
  pair, which runs the same four reused checks). A
  `scripts/reconcile.py` CLI is a future story; until then, the
  runbook's snippet (§4) is the entry point.
