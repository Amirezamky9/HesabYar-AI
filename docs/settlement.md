# Settlement engine core (US-521, Phase 5-3 Wave A) + settlement posting (US-522, Wave B)

Every gateway payment gets an exact expected settlement schedule —
one `settlement_tranche` row per leg of its contract — derived at
payment time from the ACTIVE contract version. The receivable is
from the GATEWAY, never from the customer: a BNPL payment moves the
customer's debt to the gateway at confirmation.

Design: phased design §3 (part B) + ADR 0003. This story is the
engine core (P1a): models, derivation, the state machine, period
reassignment, the REST API, and the daily tick. Settlement
accounting (posting the receivable/clearing/bank moves) is US-522;
file import + three-way matching is US-523; gateway payment APIs
are US-524; the settlement-center UI is US-525.

## What already existed (no rework)

The versioned `gateway_contract` already carries every §3.1 field
since migration 0010 (US-503): fee timing, accrual policy,
settlement model + `tranche_legs`, payout window/calendar,
working-day adjustment, day lag, grace days, tolerances, auto-post
policy, source note. US-521 derives from it; it does not extend
the table. The only registry change is `get_contract_by_id` on the
port (the engine re-derives from the version a payment was BUILT
from, never from the active version).

## Derivation (§3.2)

`hesabyar.domain.settlement.derivation.derive_tranches` — pure, no
I/O, no clock. The caller supplies the confirmation time and the
installation's holiday calendar.

- **Anchor**: the Jalali month of `gateway_confirmed_at` (the
  gateway's confirmation, never the store order date). The sales
  period is stored as `YYYY/MM`.
- **Due dates (month-based)**: due month = sale month +
  `month_offset`; due = `payout_day` of that month, clamped to the
  month's last day (day 31 in a 30-day month lands on the 30th).
  When `working_day_adjustment` is on and the due date is a holiday
  (Friday or a listed holiday), it moves to the next business day.
  The payout window runs from `payout_window_start_day` to the due
  date (SnappPay: days 1–5). Gregorian contracts (`payout_calendar`
  = `gregorian`) use Gregorian month arithmetic.
- **Due dates (day-based)**: `day_lag` set + a single leg with
  `month_offset=0`; due = `day_lag` business days after
  confirmation (SalamPay 10 working days, Sadad ~1 working day).
  A day-lag contract with more than one leg is a configuration
  error. A lag has no payout window (window starts on the due date).
- **Money** (all integer IRR via `MoneyIRR`): `fee = round(G × c)`,
  `fee_vat = round(fee × v)`, `net = G − fee − fee_vat`, half-up per
  row — the same rounding policy as the tax module (US-402). Net,
  fee and VAT are each split across the legs by percent with the
  LAST tranche = REMAINDER, so the tranches sum exactly to the
  payment totals; gross per tranche is `net + fee + vat`, which
  keeps the per-tranche identity `net = gross − fee − vat` exact.
  `fee_timing=upfront_first_tranche` puts the whole fee + VAT on
  the first tranche; the net is still split per legs.
- **Fee % is manual, never hard-coded** (ADR 0003 round-2 item 1):
  it lives only on the contract version. The fee-VAT rate comes
  from the contract's `fee_vat_rate` snapshot, or an explicit
  per-payment override; when neither exists the service fails
  closed (`SETTLEMENT_FEE_VAT_RATE_UNKNOWN`) instead of silently
  computing VAT as zero.

Worked example (§3.5, regression-tested): a 100,000,000 IRR sale
in Aban 1405 at 10% fee → fee 10,000,000 + VAT 1,000,000 → net
89,000,000 in 4 tranches of 22,250,000, due 5 Azar / 5 Dey /
5 Bahman / 5 Esfand.

## State machine (§3.3)

```
expected ──► due ──► confirmed ──► received ──► settled
   │           │          │            │
   │           │          └─ partial variants ─► partially_confirmed / partially_received
   │           └─ overdue (past due + grace, unconfirmed)
   │           └─ - - ► received (reverse match C7 — bank deposit before the gateway file)
   └─ cancelled (refund/cancel before settlement)
   └─ exception (variance/unknown reference — linked to settlement_exception)
   └─ written_off (human approval only, within policy cap, audit-logged)
```

Terminal: `settled`, `cancelled`, `written_off`. `overdue` never
goes back to `due`. `exception` leaves only by an explicit
resolution (back to an operational state, or to `cancelled` /
`written_off`). The full table is
`hesabyar.domain.settlement.state_machine.ALLOWED_TRANSITIONS`;
every transition is validated against it.

**Cancel**: open tranches cancel; already confirmed/received/
settled tranches stay — the gateway deducts them from later
settlements, so their net is recorded as `refunds_deducted_irr`
on the payment and a `cancelled_after_settlement` exception is
opened. The payment ends `cancelled` (nothing was paid) or
`partially_cancelled` (something was).

**Write-off**: approver + reason mandatory, eligible states only
(`due` / `overdue` / `exception` / partial states — never
`expected`, never `confirmed`/`received`/`settled`), one audit-log
entry per write-off, open exceptions for the tranche resolved as
`write_off`. The explicit policy cap (Tryton Reconcile Write-off
config) is US-522's.

## Period reassignment (§3.2)

When a gateway file shows a transaction in a different sales month,
the sales period is corrected and OPEN tranches (expected/due/
overdue) re-derive their due dates from the new period anchor —
same contract version, same amounts, only the month moves.
Confirmed/received tranches are untouched, and an informational
`period_reassigned` exception is logged.

## Bulk re-derivation (§3.1)

Explicit user action only (actor + reason mandatory) + one
audit-log entry for the run. Only payments whose tranches are ALL
still open are re-derived from the ACTIVE contract version;
payments with any confirmed/received tranche are reported as
skipped, never silently rewritten. Mixed open/closed
re-derivation needs the adjustment-row semantics of US-522.

## Daily tick (W1, §3.9)

`settlement.tick` (Procrastinate, daily 06:00 by default,
`HESABYAR_SETTLEMENT__TICK_CRON`): across ALL installations,
`expected → due` at the due date, `due → overdue` past due +
grace, opening (or escalating) a `late_tranche` exception at the
7/30/60-day thresholds. The tick never posts anything and never
touches the engine; it is idempotent (a re-run finds no new
candidates). `POST /settlements/tick` runs it on demand.

## REST API (13 paths, scopes `settlement:read` / `settlement:manage`)

| Method + path | Purpose |
|---|---|
| `POST /settlements/payments` | Record a payment; returns payment + derived schedule + fee record id (201; duplicate → 409) |
| `GET /settlements/payments` | List payments (channel/status/period filters) |
| `GET /settlements/payments/{id}` | Payment + full schedule |
| `POST /settlements/payments/{id}/cancel` | Cancel/refund |
| `POST /settlements/payments/{id}/reassign-period` | Correct the sales period |
| `POST /settlements/payments/bulk-rederive` | Re-derive open schedules from the ACTIVE contract |
| `GET /settlements/tranches` | List tranches (channel/state/period/due-range filters) |
| `GET /settlements/tranches/{id}` | One tranche |
| `POST /settlements/tranches/{id}/write-off` | Write off (human approval only) |
| `GET /settlements/exceptions` | List exceptions (§3.7) |
| `GET /settlements/exceptions/{id}` | One exception |
| `POST /settlements/exceptions/{id}/resolve` | Resolve (resolver + time + note recorded) |
| `GET /settlements/fee-records` | Expected vs actual fee + VAT (§3.6) |
| `POST /settlements/tick` | Run the daily tick now |

Channels and contracts are managed under `/integrations` (US-503);
this router reads them (channel_key filters) but never writes them.

## Tables (migration 0020)

`gateway_payments` (UNIQUE on channel + gateway transaction),
`settlement_tranches` (UNIQUE on payment + index),
`settlement_events`, `bank_deposits`, `settlement_matches`
(many-to-many with allocated amounts; CHECK: at least one target),
`settlement_exceptions` (11 §3.7 types),
`settlement_fee_records`, `settlement_file_imports` (UNIQUE on
channel + file hash) + `settlement_file_rows`. Tryton ids are
external references only — no SQL FKs into Tryton's database.

## Settlement posting (US-522, Phase 5-3 Wave B)

`SettlementPostingService` posts the §3.5 journals through the
`AccountingEnginePort`, following the §3.8 spike decision (Path 1b,
`docs/tryton-spikes-520.md`): tranche payments are button-driven
(`submit → process_rest → succeed` with the payout date as the
clearing date); direct journals are reserved for fee/fee-VAT
recognition, variance adjustments, and post-hoc legs. The BNPL
receivable is from the GATEWAY, never the customer: the sale
invoice carries `alternative_payees=[gateway]` and every
receivable line has party=gateway.

### The three money states

1. **Receivable (1.2.8 Gateway Receivables).** Sale: invoice posts
   Dr receivable (gross) / Cr sales revenue, one receivable line
   per tranche leg (payment term), each with party=gateway and
   maturity_date=tranche due. Fee (policy A, at sale): one posted
   journal, Dr fee expense (5.2.4) + Dr fee VAT (6.3.6
   Non-Deductible VAT — `fee_vat_claimable=false` until an official
   gateway invoice/Moadian record is verified) / Cr receivable,
   the receivable leg split per tranche so each tranche reconciles
   independently. The receivable balance is now net.
2. **Clearing (1.1.6 Gateway Clearing).** On tranche confirmation
   (C3, the gateway file confirms first — the service never
   confirms), the tranche's `account.payment` is created ONLY now
   (a Draft payment already reduces `amount_to_pay`), driven
   `submit → process_rest_batch → succeed` with the payout date as
   the clearing date, and the clearing move is posted immediately:
   Dr clearing / Cr receivable (tranche net). Clearing and bank
   receipt are REPEATABLE per tranche (partial confirmations /
   receipts); idempotency is cumulative-amount math (confirmed −
   already cleared), not a unique index.
3. **Bank (1.1.1).** On receipt (C6, after the tie-out gate —
   below), Dr bank / Cr clearing for the newly received amount. A
   fully received tranche moves to `settled`.

**Policy B (at settlement)** is the configured alternative: the fee
is recognised per tranche as it confirms, inside `post_clearing`
(the same fee-move builder, per-tranche amounts); `post_sale_fee`
refuses under policy B.

### Reconciliation (the §5 open point, resolved)

The tranche payment is NET while the invoice line is GROSS, so
clearing `succeed` cannot reconcile the line on its own. US-522
implemented option (a): a `reconcile_rest` wrapper in
`hesabyar_setup` (same pattern as `process_rest`), exposed on the
port as `reconcile_move_lines`. Once a tranche is fully cleared,
the service reconciles [invoice line + fee leg + clearing leg]
(they sum to zero); once fully received, it reconciles the two
clearing legs (payment Dr + bank Cr). Reconcile PRE-CHECKS run
before any engine write, so a missing leg fails before the payment
or bank move exists and a retry cannot double-post. Details +
live verification: `docs/tryton-spikes-520.md` §5.

### Fee variance (§3.5)

When the gateway file shows a real fee different from expected,
the difference posts as an adjustment move (Dr/Cr fee expense +
corresponding VAT against gateway receivable) — NEVER silently
absorbed. A `fee_mismatch` exception is always opened with the
reason, the fee record is updated with the actuals, and the
variance allocates across ADJUSTABLE tranches only (no clearing
posted yet, not settled/cancelled/written-off), by fee share; with
no adjustable tranche it posts as an unmatured receivable leg (a
claim against the gateway). The explicit call IS the approval; the
§3.7 draft-first variant for US-523's automatic import flow is a
follow-up.

### Post-settlement refunds (§3.5)

A refund is deducted FIRST from the payment's OPEN UNREACHED
tranches (last tranche backwards), then — if it exceeds the open
balance — as a negative adjustment (`refunds_deducted_irr`) on the
next tranche of the same gateway; confirmed/received tranches are
untouched. Refund reduces an open tranche's gross AND net (fee/VAT
unchanged — the fee was already earned); a fully-refunded tranche
(net 0) is cancelled. Every refund adjustment is logged with a
reason + the refund event reference.

> **NOTE — refund journal sides.** The design text (§3.5, part B)
> lists this entry as "Cr revenue / Dr gateway receivable", which
> is the reverse of the standard sales-return reversal (the sale
> was Dr receivable / Cr revenue; a return reverses it). The
> service posts the accounting-correct reversal — **Dr revenue
> (sales return) / Cr gateway receivable** — and this discrepancy
> is recorded here for the design's next revision.

### Tie-out gate (US-523 owns the tie-out record)

`post_bank_receipt` refuses without a passed tie-out: Σ confirmed
tranche nets − Σ documented deductions = Σ matched bank deposits,
within the contract's `tieout_tolerance_irr`. US-522 computes the
gate from the US-521 models at post time (no new tie-out table);
US-523 owns the tie-out record and supersedes this computed gate.
The receipt also requires the tranche `received` (or
`partially_received`), `received_net_irr` set, matched deposits
covering the amount, and a prior clearing posting.

### Batch auto-post (tie-out fan-out, US-522 × US-523)

The tie-out service calls the batch-level `SettlementPostingPort`
only after the mandatory tie-out holds and the policy gate allows
auto-post. The wired implementation is `EngineSettlementPosting`
(`infrastructure/accounting/tryton/settlement_posting.py`): it fans
the batch out to one `SettlementPostingService.post_bank_receipt`
call per tranche, resolving each tranche's wiring from the mapping
center via `resolve_wiring_for_tranche` (company from the batch
command's `company_engine_id`). The result's `tryton_journal_id`
is the first posted move (the tie-out audit + REST shape keep
working); `tryton_journal_ids` holds every move in tranche order
and `amount_irr` is the batch's total receipt.

PARTIAL POSTING is by design: each tranche posts its own engine
move and there is no cross-tranche transaction — if tranche 2
fails after tranche 1 posted, tranche 1 STAYS posted (its move
and its `bank_receipt` ledger row stand) and the call raises
`SETTLEMENT_POSTING_ERROR`, which the tie-out service catches:
the batch stays in exception and a review item is filed naming
the tranches already posted. A retry is safe: tranches whose
`bank_receipt` ledger rows already cover their received amount
are SKIPPED via the ledger (never re-posted); partially posted
tranches are topped up with the newly received remainder only;
and a duplicate-posting signal from the service
(`SETTLEMENT_POSTING_EXISTS`, or the "no newly received amount"
precondition) is treated as already-posted, verified against the
ledger (fail-closed if the ledger disagrees). The fail-closed
`UnconfiguredSettlementPosting` stub is kept but no longer wired.

### Wiring (mapping center, fail-closed)

`SettlementPostingWiring` carries the company, gateway party,
payment journal, and the six accounts (receivable / clearing /
bank / fee expense / fee VAT / revenue) + the optional general
journal. The REST layer resolves it from the mapping center:
`EXTERNAL_PARTY` with source key = the channel key for the gateway
party; `GATEWAY_JOURNAL` with source key = the channel key for the
payment journal; `GATEWAY_JOURNAL` rows scoped to the channel with
source keys `receivable` / `clearing` / `bank` / `fee_expense` /
`fee_vat` / `revenue` for the accounts (target `account`, by engine
id or chart code). Every missing key is listed, never defaulted.
The payment journal must be clearing-configured or clearing
refuses before any engine write.

### Posting ledger (migration 0021)

`settlement_postings` records every posting (kind, amount, tranche,
Tryton move/payment/reconciliation ids, reason, event ref, details
JSON) — the idempotency ledger for once-only kinds (`fee_at_sale`
per payment, `fee_at_settlement` per tranche; partial unique
indexes) and the audit trail for repeatable kinds
(`clearing`, `bank_receipt`, `fee_variance`, `refund`,
`refund_adjustment`).

### REST API (6 new paths, same scopes)

| Method + path | Purpose |
|---|---|
| `POST /settlements/payments/{id}/post-sale-fee` | Recognise fee + VAT at sale (policy A); 409 when already posted |
| `POST /settlements/tranches/{id}/post-clearing` | Clearing transfer for a confirmed tranche |
| `POST /settlements/tranches/{id}/post-bank-receipt` | Bank receipt after the tie-out gate |
| `POST /settlements/payments/{id}/post-fee-variance` | Fee-variance adjustment move + exception |
| `POST /settlements/payments/{id}/apply-refund` | Post-settlement refund (allocation + journal) |
| `GET /settlements/payments/{id}/postings` | The payment's posting ledger |

Mutations need `settlement:manage`; the GET needs
`settlement:read`. New error codes: 409
`SETTLEMENT_POSTING_EXISTS`; 422 `SETTLEMENT_POSTING_PRECONDITION`,
`SETTLEMENT_TIE_OUT_FAILED`, `SETTLEMENT_WIRING_UNRESOLVED`,
`SETTLEMENT_RECONCILIATION_FAILED`.

### Known hazard: dual write

Engine calls happen inside the service; the HesabYar DB commit
happens after (in the router). If the commit fails after the engine
post, the journal exists without a posting row — the same hazard as
the other engine-touching routes. The posting ledger's
cumulative-amount idempotency bounds the damage on retry for
clearing/bank receipt; once-only kinds surface as 409s on retry
(the posting row exists) only when the commit succeeded.

### Acceptance e2e

`tests/contract/test_settlement_posting.py::test_snapppay_worked_example_e2e`
runs the §3.5 worked example against live Tryton + the live
HesabYar DB: 100,000,000 IRR sale → 10,000,000 fee + 1,000,000 VAT
→ 89,000,000 net in 4 tranches of 22,250,000; the receivable from
SnappPay decreases 89M → 66.75M → 44.5M → 22.25M → 0, each tranche
settles through its bank receipt, and the bank ends up +89M. The
sale date is dynamic (today) and the dues are the 5th of the next
4 Jalali months, so the test stays green whenever it runs. It needs
FY 2027 + 12 open periods in the test DB (one-time setup; the
fixture verifies).

## Deferred to later stories

- File import + three-way matching + tie-out (US-523): the
  `confirmed`/`received`/`settled` transitions, `settlement_event`
  / `bank_deposit` / `settlement_match` creation, the tie-out
  record (superseding US-522's computed gate), the draft-first
  fee-variance flow for automatic import, and the remaining
  9 exception types' triggers.
- Statement-posting coordination (US-523/5ت): US-522's bank receipt
  is a direct move + clearing-leg reconcile; US-514 never posts
  statements, so nothing double-counts today, but a future
  statement-posting flow must coordinate with these receipts.
- Review-queue filing of settlement exceptions under
  `settlement_variance` (US-522/525): exception rows exist now;
  the unified-queue surfacing comes with matching.
- Gateway payment APIs (US-524) and the settlement-center UI
  (US-525).
