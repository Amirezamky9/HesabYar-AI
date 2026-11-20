# Manual imports — unified batch cycle (US-554)

One flow for every manual import: bank statements (US-514, B1) and
gateway settlement files (US-523, P2). The batch is **orchestration
only** — parse, validation, dedup, matching, the policy gate and the
tie-out all stay in the existing import services. The batch wraps one
underlying import and drives it through a single lifecycle.

## Batch lifecycle

```
uploaded → mapped → validated → previewed → awaiting_review
    → approved → posted
    (any pre-approve state) → rejected → archived → (re-run) → mapped
```

| State | Meaning |
| --- | --- |
| `uploaded` | File received, bytes read (never stored) |
| `mapped` | Column mapping resolved (saved mapping or inline) |
| `validated` | Rows validated + deduped by the underlying import |
| `previewed` | Accounting-effect preview computed |
| `awaiting_review` | A `manual_import_review` item is filed; a human must act |
| `approved` | The underlying import is approved (draft by default) |
| `posted` | The import's posting is complete (statement: draft statement written; settlement: tie-out passed and auto-post ran, when opted in) |
| `rejected` → `archived` | Rejected with a reason; rows + reason kept, re-runnable |

**The parse stages complete inside the upload call.** File bytes are
never stored, so a staged upload (detect → map → validate across
separate calls) would need the parsed grid persisted between calls —
deferred (see below). The batch is therefore born `awaiting_review`
with its preview and review item, and the earlier stages are recorded
in the stage history with timestamps.

**Reject = `rejected → archived` in one call.** There is no separate
archive endpoint. A rejected batch keeps its rows and its reason;
re-run reopens the underlying import from the stored rows (no
re-upload), recomputes the preview, files a fresh review item,
increments `attempt`, and clears the rejection fields (the history
stays in the stage log).

## Accounting-effect preview

The preview is computed by the **same code that computes the real
posting effect** — there is no second calculator:

- Settlement: `SettlementImportService.preview_for_import` returns the
  `ImportPreview` the import itself produced (gross, fee, tax, refunds,
  net, deductions, receivable before/after, expected deposit).
- Statement: `ImportBatchPreview.from_statement_totals` derives the
  preview from the bank import batch's own totals (gross/net/new rows;
  fee, tax and refunds are 0 and receivable is null — a statement has
  no receivable).

## Approve → the normal pipeline

Approve drives the underlying import through exactly the path an
automatic import takes:

- Statement: `BankImportService.confirm_import` writes a DRAFT
  statement in the engine; the batch moves to `posted`. (No statement
  auto-post exists; `auto_post` is recorded but has no additional
  effect for statements.)
- Settlement: `SettlementImportService.approve_import` confirms the
  tranches, then — **only with explicit `auto_post=true`** (ADR 0003
  decision 1) — the shared tie-out + auto-post runs
  (`run_tieout_for_batch`). Without the opt-in the batch stays
  `approved` with `posting_status=draft_hold`. The policy gate still
  applies: a gate denial holds the batch at `approved` with
  `posting_status=held_gate`, it does not post and does not error.

## Batch variance report

`GET /imports/batches/{id}/variance` reports expected vs imported vs
posted, with the tie-out result:

- Expected = the preview's expected deposit (settlement) or signed net
  (statement).
- Imported = settlement event nets minus deductions / statement net.
- Posted = tie-out `matched_deposits_irr` once posted (the bank receipt
  posts exactly the matched deposits) / statement net once the engine
  statement exists.

Tie-out figures are recomputed read-only via
`SettlementImportService.compute_tieout_figures` — again, the same
sums the live tie-out uses (`_tieout_sums`), not a second calculator.

## Column mappings — why a batch-scoped table

Bank column mappings already live in `bank_column_mappings` (US-514).
Settlement column mappings live in the new `settlement_column_mappings`
table (migration 0034), **not** in the US-503 mapping center: the
center's nine `MappingKind`s are a closed vocabulary of entity
mappings (gateway → journal, category → account, …) with a different
lifecycle, while a column mapping is *file layout per source* (channel
+ file type). A mapping is saved per source and reused on the next
import of the same source; the wizards offer "save this mapping" when
the user maps columns by hand.

## `payment.settlement.received` consumer (US-523 deferred item)

The event type existed with no consumer. US-554 registers one in the
event pipeline (`EventPipelineService`, after the cheque branch):
`SettlementReceivedHandler` validates the payload
(`channel_key` + `payout_date` required; net from `net_irr` |
`settlement_amount` | `net_amount`, whole Rial), resolves the channel,
finds the settlement event, and runs the **shared** tie-out
(`run_tieout_for_batch`) — the same tie-out + auto-post rules as file
imports, including the fail-closed no-company path.

Outcomes:

- Batch tie-out passes and the gate allows → posted (auto-post).
- Batch is `awaiting_review` → recorded; the approve step ties out.
- Batch already settled → idempotent success ("already settled").
- Batch rejected, or the receipt has no matching settlement event →
  a `settlement_variance` review item (subtype `unknown_reference`)
  and the event is processed, **not** retried — an unattributed receipt
  is a human question, not a transient failure.
- Unknown channel → `MappingUnresolvedError`, routed to review
  (permanent).
- Idempotency: the pipeline's existing idempotency-key handling plus
  the §10.4 source→settlement-event mapping (a redelivery with a new
  source event id but the same external entity id is suppressed).

## REST endpoints

Prefix `/api/v1/imports`, tag `imports`. Mutations require an
`Idempotency-Key` header (missing key → 400
`IDEMPOTENCY_KEY_REQUIRED`).

| Method | Path | Scope |
| --- | --- | --- |
| POST | `/imports/batches/detect` | `banking:read` / `settlement:read` (by kind) |
| POST | `/imports/batches` | `banking:import` / `settlement:manage` (by kind) |
| GET | `/imports/batches` | list returns only kinds the key may read |
| GET | `/imports/batches/{id}` | per batch kind |
| GET | `/imports/batches/{id}/preview` | per batch kind |
| GET | `/imports/batches/{id}/variance` | per batch kind |
| POST | `/imports/batches/{id}/approve` | per batch kind |
| POST | `/imports/batches/{id}/reject` | per batch kind |
| POST | `/imports/batches/{id}/re-run` | per batch kind |
| POST | `/imports/batches/{id}/retry-posting` | settlement only |
| GET/POST | `/imports/settlement-column-mappings` | `settlement:manage` (write), `settlement:read` (list) |
| GET | `/imports/settlement-column-mappings/{id}` | `settlement:read` |
| POST | `/imports/settlement-column-mappings/{id}/supersede` | `settlement:manage` |

Scope enforcement is per kind: `require_scopes` is ALL, so
kind-dependent routes authenticate with `get_authenticated_client`
and check the applicable scope inside. Upload dedupes on the file
hash — re-uploading the same file returns the existing batch with
`duplicate=true`.

## Panel placement (§7.1/§7.4)

Imports are per module — there is no unified "imports" page:

- Statement import wizard: banking ← statements ← import
  (`features/statements/statement-import.tsx`).
- Settlement import wizard: settlements ← import
  (`features/settlement/settlement-import.tsx`).
- Batch detail: `features/imports/import-batch-detail.tsx`, routed at
  `/banking/statements/imports/[batchId]` and
  `/settlements/imports/batch/[batchId]`.
- The settlement import detail page shows a batch-cycle section
  (looked up by `source_import_id`) and approves/rejects through the
  batch when one wraps the import.
- The dashboard's "ایمپورت‌های دستی" section is US-551's — it reads
  `GET /imports/batches`; this story only provides the endpoint.

## Deferred

- **Staged upload** (detect → map → validate as separate calls):
  deferred — file bytes are never stored, so the parsed grid would
  need to be persisted between calls. Parse stages complete inside the
  upload call instead.
- **IMAP fetch**: out of scope (still deferred).
- **Statement auto-post**: no such flow exists; statement approve
  always writes a DRAFT statement.
- **Per-type payload validation** for the settlement-received consumer
  beyond the current contract (US-511 deferred item, unchanged).

## Tests (CI only)

- `tests/unit/test_manual_imports.py` — state machine (all transitions
  incl. reject → archive → re-run), preview computation, mapping
  detection/validation, dedup, service-level approve/reject/re-run/
  retry-posting/variance.
- `tests/unit/test_settlement_received_consumer.py` — payload
  validation, handler outcomes, pipeline branch (wired/unwired,
  idempotent redelivery, unattributed receipt).
- `tests/interfaces/test_rest_imports.py` — batch endpoints (scopes
  per kind, state guards, preview → approve → post, reject with
  reason, mapping CRUD).
- `apps/admin/features/imports/import-batch-data.test.ts` — vitest for
  the batch labels + mapping validation helpers.
- `apps/admin/e2e/import-batches.spec.ts` — one full settlement batch
  flow (upload → mapping → preview → approve) plus reject → archive →
  re-run.
