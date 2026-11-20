# Installments (۵ج — in-house installments + BNPL)

In-house installment plans (phased design §1.3 ۵ج, flow I1) + the
user's key rule from batch 1 item 4: **every installment receipt
must reference the original invoice AND the accounting document
(journal)**. The BNPL side (flow I2 — the seller sees only
"receivable from the gateway", never the customer's installments to
the gateway) is US-543. This doc covers the in-house core built by
US-540, the BNPL side built by US-543, and the decisions the later
stories build on.

## The model

| Entity | Table | Notes |
|---|---|---|
| `InstallmentPlan` | `installment_plans` | Exactly one sales invoice; the multi-stage payment terms (leg count, interval, first due date) live here when the legs were generated from terms. `kind` (`internal` \| `bnpl`, US-543) + `gateway_channel_key` for BNPL plans. |
| `InstallmentLeg` | `installment_legs` | Sequence + due date (Jalali in the API, Gregorian stored) + principal (`MoneyIRR`, digits=0). No received amount, no status — both computed. |
| `InstallmentReceipt` | `installment_receipts` | Plan-level receipt + its allocations + its advance. `invoice_id` AND `journal_move_id` are both NOT NULL. US-542 adds `cheque_id` (the cheque backing a cheque receipt) and `collected_at` (the collection date — the income-timing record; see the collection hook below). BNPL receipts (US-543) also carry the gateway link (`gateway_channel_key` / `gateway_reference` / `gateway_payment_id` / `bnpl_status`). |
| `InstallmentReceiptAllocation` | `installment_receipt_allocations` | One slice of a receipt on one leg (never more than the leg's balance). |
| `InstallmentSaleContract` | `installment_sale_contracts` | NEW entity, separate from `gateway_contract` (gateways-only): late-fee rule, penalty account, early-settlement terms; versioned with an effective date. Internal plans only — a BNPL plan never carries one. |

One plan per invoice, ever — enforced by the service (409
`INSTALLMENT_PLAN_EXISTS`) and backstopped by the unique constraint
on `(installation_id, invoice_id)`. A plan is linked to its invoice
by reference (`invoice_id`); US-540 does **not** verify the invoice
exists in the engine — the reference is recorded and auditable, and
verification is deferred (see below).

## The leg state machine (computed, never stored)

A leg's status is a pure function of (due date, principal, received,
pending_clearance, today) — `hesabyar.domain.installments.state_machine.leg_status`.
Nothing in the database can disagree with the receipts, because
there is no status column to disagree with. "Received" counts only
money that has actually arrived; a cheque receipt's money is
PENDING until the cheque clears — `split_allocations` splits every
allocation by its cheque's state (cleared → received; held /
in_clearing / registered → pending; bounced / returned / endorsed /
voided / expired / rejected → neither, the allocation stays as
history and contributes nothing; a cheque the registry cannot find
→ pending, fail-closed).

```
expected → due → partially_paid → paid
   │          │         │
   └──────────┴─────────┴──→ overdue (past due date, balance > 0)
                         │
                         └──→ pending_clearance (cheque received,
                              not yet cleared — wins over overdue)
```

- `paid` — received >= principal (a leg is never overpaid: the
  allocation rule caps every slice at the leg's balance).
- `pending_clearance` — the leg's balance is covered by pending
  (uncleared) cheque money. This wins over `overdue`: the pending
  period is NOT counted as overdue (§5 §2.1) — the customer HAS
  paid, the money just hasn't arrived. While a leg is
  `pending_clearance`, **NO overdue reminder or dunning is sent
  for it** — the dunning engine (US-541) MUST skip
  `pending_clearance` legs. A partly-pending leg (pending money
  but still a balance) is NOT `partially_paid` either: pending
  money is not received money.
- `overdue` — past the due date with an unpaid balance > 0. This
  wins over `partially_paid`: a leg that was partly paid and then
  passed its due date is overdue, not partially paid.
- `partially_paid` — something received, balance > 0, due date not
  yet passed.
- `due` — nothing received, due date reached (today >= due date).
- `expected` — nothing received, due date in the future.

A leg is `due` ON its due date and `overdue` only AFTER it (the
comparison is by calendar day). Plan status is derived from the
legs: all legs paid → `paid`; any leg overdue → `overdue`;
otherwise `active` — a plan whose only unpaid legs are
`pending_clearance` is `active`, not overdue.

Status precedence: paid → `pending_clearance` → overdue →
partially_paid → due → expected. No leg-side write happens on
clear or bounce: the status is recomputed from the cheque's state
every time it is read, so the cheque registry is the single status
authority and a leg can never disagree with its cheque.

## The allocation rule (multi-stage receipt)

A receipt is plan-level: the caller pays an amount, and the rule
decides which legs it pays. `allocate_receipt` in
`hesabyar.domain.installments.state_machine`:

1. **Oldest overdue leg first** — overdue legs ordered by due date,
   then sequence; each takes up to its remaining balance.
2. **Then the earliest due leg** — `due` / `partially_paid` legs
   ordered by due date, then sequence; each takes up to its balance.
3. **The remainder is the advance** — recorded on the receipt
   (`advance_irr`) and held on the plan.

Two things the rule NEVER does:

- **Never prepays a not-yet-due (`expected`) leg.** A receipt pays
  what is due (or overdue); it does not reach into the future. A
  caller who wants to prepay a future leg has no API for it in
  US-540 — the advance is the honest record of the overpayment,
  and applying it (early settlement / re-amortisation) is US-541's.
- **Never silently overpays a leg.** Every allocation slice is
  capped at the leg's balance, so the sum of a leg's slices never
  exceeds its principal. An overpayment is visible as the advance,
  never hidden inside a leg.

The advance does NOT reduce the plan's outstanding principal in
the detail view: outstanding is the sum of the legs' balances, and
the advance is reported alongside it. (It is money received but
not yet applied — conflating the two would understate what the
customer still owes on the schedule.)

Receipts are multi-stage by construction: a leg may receive many
receipts, each contributing slices; a leg's received total is the
sum of its slices across all receipts.

## Cheque receipts (US-542)

A receipt may be paid BY CHEQUE (`payment_method=cheque` +
`cheque_id`). The leg it pays enters `pending_clearance` until
the cheque clears (clear → leg paid; bounce → leg unpaid +
overdue again, the late fee then computed by US-541 from the
ORIGINAL due date — §9.1 #13 — because the obligation was never
satisfied). The two-way contract test deferred from US-531 lives
in `tests/contract/test_installments_us542.py`.

Rules:

- **Receivable states only.** The cheque must be a received,
  payment-purpose cheque in `held` or `in_clearing`. A
  `registered` cheque is rejected — intake creates NO journal
  until the cheque reaches `held` (the US-533 rule, unchanged),
  so a registered cheque cannot back a receipt: the receipt's
  `journal_move_id` honestly references سند ۱ (the held journal),
  which does not exist yet. Cleared, bounced, returned, endorsed,
  voided, expired, rejected, issued and collateral cheques are
  all rejected with the typed 422
  `INSTALLMENT_CHEQUE_NOT_RECEIVABLE`; an unknown cheque is a
  404 (`CHEQUE_NOT_FOUND`).
- **Single-leg allocation.** A cheque receipt allocates to at
  most ONE leg (`allocate_receipt(..., single_leg=True)`) — the
  cheque ↔ leg link is single-leg by design. Any remainder is
  the advance, as usual.
- **One receipt per cheque.** A cheque that already backs a
  receipt cannot back another (`INSTALLMENT_CHEQUE_ALREADY_USED`).
- **Amount rule.** The receipt amount must not exceed the cheque
  amount (`INSTALLMENT_CHEQUE_AMOUNT_EXCEEDED`); the allocated
  slice is recorded as the cheque's `applied_amount_irr` (the
  cheque may be larger than the leg it pays).
- **Link fields on the cheque.** Recording the receipt sets the
  cheque's `installment_leg_id` (the leg the slice paid),
  `applied_amount_irr`, and `installment_leg_status =
  pending_clearance`. The cheque service (US-531, unchanged)
  flips that status to `paid` on clear and `unpaid` on bounce.
  A cheque already linked to a DIFFERENT leg is refused
  (`INSTALLMENT_CHEQUE_LINK_MISMATCH`); a cheque already linked
  to the SAME leg (linked at intake) is accepted. A receipt that
  is all advance (no due leg) leaves the cheque unlinked — the
  cheque backs a receipt, not a leg.
- **The pending leg shows its cheque.** The plan detail reports
  `pending_clearance_irr` per leg (and a plan total) plus the
  linked cheque: `sayad_id`, due date, state, amount. The leg
  is NOT re-payable while pending — the allocation rule skips
  `pending_clearance` legs, so a second receipt cannot pay the
  same leg twice.

## The collection hook (US-542 → US-541)

Income is recognised on COLLECTION, never on receipt or
`pending_clearance` — and US-541 owns all income recognition.
US-542 records the collection the income engine consumes:

- Every receipt carries `collected_at` (a Jalali date, nullable).
  Cash / bank / other receipts are collected on receipt:
  `collected_at == received_date` at creation. A cheque receipt
  is collected when its cheque CLEARS: the
  `InstallmentCollectionHook` (wired into the cheque command
  service, `hesabyar.application.commands.cheques`) sets the
  receipt's `collected_at` to the clear date. A bounced cheque's
  receipt keeps `collected_at` NULL — the money never arrived,
  and no income is ever recognised for it.
- `collected_at` is the income-TIMING record, NOT a second
  status: the cheque's state remains the status authority (see
  above). US-541 reads `collected_at` to know WHEN a receipt was
  collected; a NULL `collected_at` on a NON-cheque receipt (only
  possible for rows predating US-542) means "collected on its
  `received_date`".
- The hook fires AFTER the engine transition + registry mirror
  succeed, only for received cheques carrying an
  `installment_leg_id`, and NEVER raises into the transition: a
  hook failure is logged and filed as an
  `installment_collection_hook_failed` review item, and the
  cheque transition still stands. The hook is idempotent
  (re-firing never moves `collected_at`) and writes nothing on
  bounce. No new canonical event type was introduced — the
  recorded collection IS the hook; a new event type would need
  an ADR.

## The linkage guard (the user's key rule)

Every installment receipt MUST reference:

- the **original invoice** (`invoice_id`), and
- the **accounting document** (`journal_move_id` — the engine move
  id of the journal for this receipt).

The guard is enforced in `validate_receipt_payload`, BEFORE any
other validation, so the missing link is the finding — a receipt
without both refs is rejected with a typed 422:

```json
{
  "type": "about:blank",
  "title": "Unprocessable Entity",
  "status": 422,
  "code": "INSTALLMENT_RECEIPT_LINKAGE_REQUIRED",
  "detail": "An installment receipt must reference both the original invoice and the accounting document (journal); missing: invoice_id, journal_move_id."
}
```

The service then checks the invoice MATCHES the plan's invoice
(a receipt naming the wrong invoice is a typed 422,
`INSTALLMENT_INVOICE_MISMATCH`), and the database backstops the
guard with NOT NULL on both columns. The guard is covered by unit
tests (validation + service) and contract tests (real DB).

What the guard does NOT do (deferred, deliberately): verify that
the `journal_move_id` exists in the engine. The receipt records
the reference; a verification round-trip would couple receipt
recording to engine availability (receipts could not be recorded
while the engine is down) and belongs with the journal-creation
stories (US-541 creates the late-fee journal; a later story may
verify move existence as part of the linkage-compliance report,
§7.5). The invoice reference is likewise recorded, not verified,
for the same reason.

## The sale contract

`InstallmentSaleContract` carries, per contract (all user-entered,
nothing hard-coded):

- **Late-fee rule** — `late_fee_basis` (`none` | `daily_percent` |
  `monthly_percent` | `fixed_per_day`) + `late_fee_rate` (an exact
  decimal: a fraction for the percent bases, whole Rial per day for
  `fixed_per_day`) + `late_fee_grace_days`. A contract with basis
  `none` charges no late fee; a rate with basis `none` is refused
  (a contract must not carry a meaningless rate). The late fee is
  computed from the ORIGINAL due date (see the US-541 section) and
  posts to the penalty account as a DRAFT journal by default.
- **Penalty account** — `penalty_account_engine_id` (+ chart code
  for display): the finance-income account the late fee posts to —
  never a vague "miscellaneous" account. NULL until configured; a
  contract without one cannot post a late-fee journal.
- **Finance income** — `finance_income_rate` (US-541): the fraction
  of principal charged as finance income over the whole plan (e.g.
  `0.10` = 10%), an exact decimal, never hard-coded. Per-leg income
  is the leg principal × the rate, HALF_UP to whole Rial per leg.
- **Receivable account** — `receivable_account_engine_id` (+ chart
  code, US-541): the debit side of the late-fee journal. Both the
  receivable and the penalty account must be configured for a fee
  to be journaled.
- **Dunning schedule** — `dunning_offsets` (US-541): the day
  offsets (strictly increasing, 1..3650, at most 10) at which an
  overdue leg ages into the next dunning stage.
- **Early-settlement terms** — `early_settlement_fee_policy`
  (`waived` | `kept`) and `early_settlement_income_policy`
  (`waived` | `discounted` | `full`) + `early_settlement_discount_rate`
  (US-541; required — strictly between 0 and 1 — exactly when the
  income policy is `discounted`, 0 otherwise). The payoff quote +
  settlement receipt read them.

**Versioning:** `version` increments per contract `code`,
`effective_from` says when the version applies, and
`supersedes_contract_id` links a version to the one it replaces.
**Effective-version selection (US-541):** the version in force on
a date is the latest ACTIVE version with `effective_from` ≤ that
date (ties break toward the highest version); none in force means
no contract governs. A plan resolves its contract ONCE, at
creation — by `contract_id`, or by `contract_code` (the version in
force then) — and keeps that version's terms for its whole life:
a newer version never re-prices an existing plan.

## Schedule generation (terms → legs)

Terms-based plans (`total_principal_toman` + `leg_count` +
`interval_days` + `first_due_date`) generate legs by equal split:
`total // leg_count` per leg, the integer-division remainder on
the LAST leg (deterministic — the last leg may be a few Rial
larger), due dates `first_due_date + (i * interval_days)` by
Gregorian day arithmetic. The leg principals always sum to exactly
the total. Explicit legs (a list of `{due_date, principal_toman}`)
are the alternative; supplying both shapes, or neither, is a 422.

`interval_days` is deliberately in DAYS, not months: Jalali-month
arithmetic ("same day next month") is ambiguous at month ends, and
a day count is exact. A 30-day interval is the caller's way of
saying "monthly".

## Posting: draft by default

US-540 creates NO journal: a plan is a schedule, and a receipt
records a payment whose accounting document the caller already
posted (or drafted) — the receipt carries the move id, it does not
create the move. The plan's `auto_post` flag (default `false`) is
the posting preference for the journals later stories create
(late fee, finance income — US-541): draft by default, auto-post
only with an explicit opt-in (ADR 0003 decision 1).

## Finance income, dunning, late fees, early settlement (US-541)

### Decision: finance income is recognised on COLLECTION

Finance income is recognised GRADUALLY as legs are COLLECTED —
the installment method — never on receipt alone and never on
`pending_clearance`. A bounced cheque never recognises income:
the collection was never confirmed, so no income is earned. This
closes the batch-1 open tension (income on collection vs on
receipt) in favour of collection.

The consumption hook is `recognised_income(collected, rate)` in
`domain/installments/finance.py`: the income recognised on a
collected portion is that portion × the contract rate. Ordinary
receipts call it implicitly (a receipt's collected amount is a
confirmed collection); **US-542's clear path calls it when a
cheque CLEARS** — the clear is the confirmation event, and the
income on the cleared amount is recognised then. US-542 must not
call it on receipt of the cheque (the leg is `pending_clearance`,
not collected) and must not touch the leg's due date on bounce
(the fee clock runs from the original due date — see below).

Per-leg income is the leg principal × the contract rate, HALF_UP
to whole Rial per leg; the plan total is the sum of the legs.
There is no time-based accrual: the rate is a fraction of
principal over the whole plan, charged once per leg.

### Dunning: stepped stages + the review queue

An overdue leg ages through dunning stages by the contract's
`dunning_offsets`: stage = the number of offsets reached (stage 0
= overdue but before the first reminder — the leg appears in the
overdue report but files nothing). Each stage files ONE review
item of type `installment_overdue` (one of the 9 review-queue
types), with `source_event_ref =
"installment-leg:{leg_id}:dunning-stage:{n}"` and subtype
`dunning-stage-{n}` — so a re-run files nothing new for a stage
already open or in review. Severity is `critical` at the final
stage, `warning` before it; the item amount is the leg balance +
its accrued fee. The sweep (`POST /dunning/run`, or a scheduled
call) scans every plan, assesses fees for every overdue leg, and
files the stage items; it is idempotent per (leg, stage, day).

### Decision: the late fee runs from the ORIGINAL due date

When a cheque bounces, the late fee is computed from the ORIGINAL
due date — the obligation was never satisfied, so the clock never
stopped and the `pending_clearance` window is not excluded. This
closes the batch-1 open tension (fee from the original due date
vs the bounce date) in favour of the original due date. The fee
base is the leg's unpaid BALANCE (never the original principal),
and the fee clock is a pure function of (due date, today, grace):

- `daily_percent`: balance × rate × chargeable days (days past due
  minus grace, floored at 0).
- `monthly_percent`: balance × rate × STARTED months
  (`ceil(days / 30)`).
- `fixed_per_day`: rate (Rial/day) × chargeable days.

The engine operates on overdue legs REGARDLESS of cause —
including legs US-542 returns to overdue on bounce — and depends
only on leg state + the US-540 contract model.

### The late-fee journal (draft by default)

Dr the contract's receivable account / Cr the penalty account,
amount = the fee INCREMENT since the last journaled assessment,
date = the assessment date, description names the plan + leg +
fee period. Draft by default; posted only when the plan's
`auto_post` is set (an explicit opt-in, ADR 0003). Both accounts
must be configured: a settlement with a missing account fails
closed (422 `INSTALLMENT_LATE_FEE_ACCOUNT_MISSING`); a dunning
run skips journaling that leg (listed in the result) but still
assesses the fee. Assessments are CUMULATIVE snapshots — the total
fee accrued through a date, unique per (leg, date) — so a fee
assessed without a journal (no engine, no accounts) never blocks
a later journal of the same money. The engine journal is created
BEFORE the DB writes; if a DB write then fails, the draft journal
survives orphaned (findable by description) and the retry journals
only after the assessment row exists.

### Payoff quote + early settlement

`GET /plans/{id}/payoff-quote` (optional `as_of` Jalali date):

```
payoff = remaining principal + income due + late fees due − advance held
```

(floored at 0). Income due has TWO parts: (a) the remaining legs'
UNREALISED income, adjusted by the contract policy (waived → 0,
discounted → unrealised × (1 − discount), full → all of it),
PLUS (b) income already REALISED on collections but not yet PAID
— charged in full whatever the policy, because it was already
earned (ordinary receipts pay principal only, so realised income
accumulates unpaid until settlement). Late fees due = the accrued
fees if the fee policy is `kept`, else 0.

`POST /plans/{id}/early-settlement` (201) closes the plan
atomically: a settlement receipt whose identity is
`amount + applied_advance == allocated + finance_income + late_fee
+ advance`, and — for a FULL settlement — allocations covering
EVERY open leg including not-yet-due ones (the one exception to
"never prepay expected legs"). A full settlement below the payoff
is refused (422 `INSTALLMENT_SETTLEMENT_AMOUNT_INSUFFICIENT`); the
linkage guard applies (invoice id + journal move id required).
The held advance is consumed by the settlement (`applied_advance`
on the receipt + the plan).

A PARTIAL settlement pays principal only: the payment allocates
by the normal rule, and its advance PLUS any already-held advance
reduces the remaining principal — the still-open legs re-split
equally (remainder on the LAST leg, the same rule as generation),
due dates unchanged, each leg's new principal never below what it
already received. The re-amortisation is recorded as a schedule
amendment (before/after principals + applied advance + receipt
id). A partial payment that would close the last open leg while
income or fees are due is refused — the caller must use a full
settlement. Applying a pre-existing advance with NO new money is
not supported (a partial settlement requires a positive payment).

### The overdue report

`GET /installments/overdue` is the source of report 1 in §7.5:
every overdue leg in the installation (optionally scoped to one
plan), most-overdue first, with days overdue, balance, dunning
stage, next reminder date and accrued fee; the totals cover the
whole filtered set, not just the page.

### Deferred from US-541

- Fees already journaled by dunning runs are NOT auto-reversed
  when a settlement waives them — a human reverses the draft if
  needed.
- A plan that runs to term without an early settlement leaves its
  realised income as an uncollected receivable: collecting the
  final leg's income is a later story.
- The dunning sweep has no scheduler of its own yet — it runs on
  demand (`POST /dunning/run`) until a later story wires it to a
  daily job.

## BNPL (US-543, flow I2)

A BNPL plan is a sale whose legs are paid by the gateway in
tranches. The customer→gateway installment schedule is NOT in the
seller's books — HesabYar tracks only the gateway receivable + the
gateway's SETTLEMENT tranches to the seller (Phase 5-3). A BNPL
plan's legs are the sale's legs; each leg, when paid, is paid by
the gateway, and the gateway settles that payment in tranches per
its contract.

### The separation (criterion 2)

BNPL and internal installments are separate worlds, in the model
AND the reports:

- **Separate plan kind.** `kind` is `internal` or `bnpl` (stored,
  default `internal`). A BNPL plan pins exactly one gateway
  channel (`gateway_channel_key` — required at creation); an
  internal plan forbids it. A BNPL plan never carries a sale
  contract (no late fee, no early settlement — the gateway owns
  collection).
- **Separate receipts.** The plan-kind guard refuses a BNPL
  receipt on an internal plan (422
  `INSTALLMENT_BNPL_PLAN_REQUIRED`) and a cash/bank/other receipt
  on a BNPL plan (422
  `INSTALLMENT_BNPL_PLAN_METHOD_NOT_ALLOWED`); a BNPL receipt
  naming a different gateway than the plan's is refused (422
  `INSTALLMENT_BNPL_GATEWAY_MISMATCH`). BNPL legs and internal
  legs are NEVER summed or netted together.
- **Separate receivable accounts.** A BNPL receivable posts to the
  gateway's receivable account (1.2.8 Gateway Receivables,
  resolved per channel from the mapping center by the settlement
  posting service, US-522); an internal installment receivable is
  a customer receivable. No new account column is needed — the
  plan's `gateway_channel_key` IS the link to the channel's
  wiring.
- **Separate report filters.** `GET /installments/plans` filters
  by `kind=internal|bnpl` (a SQL predicate — kind is stored).
  US-545's reports build on this filter.
- **No finance income, no dunning.** A BNPL plan NEVER accrues
  internal finance income and NEVER enters dunning — the gateway
  owns collection. The single place that rule lives is
  `bnpl.plan_accrues_finance_income` /
  `bnpl.plan_enters_dunning` (both false for BNPL plans); US-541's
  finance-income recognition and dunning must call them before
  accruing or queuing anything for a plan.

### The Snapp Pay price rule, resolved

Snapp Pay's "installment price <= cash price" rule vs finance
income: the tension dissolves because a BNPL plan carries NO
internal finance income BY CONSTRUCTION (see above). On a BNPL
sale the seller charges the customer no interest at all — the
gateway's fee is the seller's cost, recognised by the settlement
engine from the gateway contract (fee % user-entered per contract,
never hard-coded), never charged to the customer as a finance
charge. There is no installment price to compare against the cash
price, because the seller's price is the cash price and the
gateway's fee is a separate cost line.

### The BNPL receipt (reuse, not duplication)

`POST /installments/plans/{id}/receipts` with
`payment_method=bnpl` requires `gateway` + `gateway_reference`
(the gateway's transaction id). The service creates ONE
`gateway_payment` per receipt through the US-521 settlement
engine (`SettlementCommandService.record_payment` — the tranche
derivation, fee/VAT math, rounding and contract snapshotting are
all the engine's; nothing is re-implemented here):

- gross = the receipt amount (the gateway's transaction covers the
  whole receipt, advance included);
- confirmation time = the receipt's received date (its Jalali
  month anchors the tranche schedule);
- source = `manual_entry` (the caller supplies the gateway's
  confirmation as attested by the store callback / portal — no
  live gateway call is made; no live credentials exist);
- the journal move id travels as `tryton_journal_id`; the invoice
  ref travels as `tryton_invoice_id` only when it parses as an
  engine id.

The receipt stores only the link (`gateway_payment_id` +
`bnpl_status=active`). A duplicate gateway reference is a 409
from the engine (the (channel, transaction) pair is unique per
installation); an unknown channel or a channel with no active
contract fails closed there too — in every case no receipt is
stored. The allocation rule is the same as for internal receipts
(oldest overdue first, then earliest due; remainder as advance),
computed against live receipts only.

Settlement models come from the gateway's contract, never from
code: Snapp Pay has four contractual models (monthly 100%, lump
after 2 months, 2 equal tranches, 4 equal tranches — the 4×25%
over 4 months is the one the user named); DigiPay settles lump-sum
in ~14–30 days. The fee % is user-entered per contract version.

### The BNPL leg lifecycle (computed, never stored)

```
expected → gateway_confirmed → settled_per_tranche → paid
```

Derived by `hesabyar.domain.installments.bnpl.bnpl_leg_status`
from the leg's ACTIVE BNPL receipts' settlement tranches:

- `expected` — no live BNPL receipt allocates to the leg.
- `gateway_confirmed` — a BNPL receipt exists and its tranches are
  all still open: the gateway confirmed the payment and owes the
  seller, but no tranche has settled yet.
- `settled_per_tranche` — at least one linked tranche has settled
  (or is confirmed/received), but not all: partial gateway
  settlement allocates per tranche, and the leg reflects exactly
  that progress (a leg paid by two BNPL receipts moves with both
  payments' tranches).
- `paid` — every linked tranche is settled: the gateway has paid
  everything it owes for this leg (net of the fee).

There is NO `overdue` for a BNPL leg, and a BNPL plan is never
`overdue` either — the gateway owns collection, so dunning never
applies. A leg past its due date with a live receipt is still
`gateway_confirmed`.

Gateway confirmation after recording comes through the existing
paths — the settlement-file import (US-523 confirms tranches from
the gateway's file) and manual confirmation
(`transition_tranche`) — and the leg status follows automatically
at read time. Tests drive the same manual path as the simulator
confirmation (no live gateway call, no new simulator class).

### Rejection / cancellation

`POST /installments/plans/{id}/receipts/{receipt_id}/reject-bnpl`
with a `reason`:

1. cancels the gateway payment through the settlement engine
   (open tranches → cancelled; tranches already settled stay
   settled and their net is recorded as `refunds_deducted_irr` —
   the gateway deducts it from later settlements);
2. marks the receipt `rejected`, so its allocations stop counting
   and its legs return to `expected` (the customer owes again);
3. files ONE review-queue item (`settlement_variance` /
   `bnpl_gateway_rejection`) — WARNING normally, CRITICAL when
   money was already settled. The settlement cancel is called
   without a tenant id so the engine does not file a second
   `cancelled_after_settlement` item on top.

The receipt row is kept as the audit record. A second rejection
is a 409 (`INSTALLMENT_BNPL_ALREADY_REJECTED`). A payment already
cancelled through the settlement API is not an error — the
receipt still needs marking dead and the item still needs filing.

### Gateway totals (detail view only)

The plan detail carries two gateway-side totals, never summed
with the customer-side totals: `gateway_receivable_irr` (open
tranche nets — what the gateway still owes) and
`gateway_settled_irr` (received nets — what has landed). Both are
zero for internal plans. The list view stays light (no per-plan
tranche reads).

## The schedule event (ADR 0005)

`installment.schedule.created` is the twelfth canonical event
type, approved by ADR 0005. It is DERIVED inside HesabYar from
`sales.order.completed` (when the order carries installment terms)
and is NOT a store webhook topic. The derivation lands in a later
5ج story; US-540 approves the name, wires it into
`CanonicalEventType`, and records the envelope contract in the ADR.
A manually created plan (REST) does not emit the event in US-540.

## REST

| Method + path | Scope | Notes |
|---|---|---|
| `POST /api/v1/installments/plans` | `installments:write` | Terms-based or explicit legs; 201. US-543: `kind` (`internal` \| `bnpl`) + `gateway_channel_key` for BNPL plans. |
| `GET /api/v1/installments/plans` | `installments:read` | Filters: `invoice_id`, `kind` (US-543), `status` (derived). |
| `GET /api/v1/installments/plans/{id}` | `installments:read` | Legs + receipts + derived totals (+ gateway totals for BNPL plans). |
| `POST /api/v1/installments/plans/{id}/receipts` | `installments:write` | Linkage guard enforced; 201. `payment_method=cheque` requires `cheque_id` (US-542); `cheque_id` is forbidden for other methods. US-543: `payment_method=bnpl` with `gateway` + `gateway_reference` (BNPL plans only). |
| `POST /api/v1/installments/plans/{id}/receipts/{receipt_id}/reject-bnpl` | `installments:write` | US-543: gateway rejection/cancellation; legs return to `expected` + review item. |
| `POST /api/v1/installments/contracts` | `installments:write` | One contract version; 201. |
| `GET /api/v1/installments/contracts` | `installments:read` | Filter: `code`. |
| `GET /api/v1/installments/contracts/{id}` | `installments:read` | One version. |
| `GET /api/v1/installments/plans/{id}/amendments` | `installments:read` | The plan's schedule amendments, oldest first — the plan-change audit history (US-545). |
| `GET /api/v1/installments/plans/{id}/payoff-quote` | `installments:read` | Optional `as_of` (Jalali); US-541. |
| `POST /api/v1/installments/plans/{id}/early-settlement` | `installments:write` | Full or partial; 201; US-541. |
| `GET /api/v1/installments/overdue` | `installments:read` | Filters: `plan_id`, `limit`, `offset`; US-541. |
| `POST /api/v1/installments/dunning/run` | `installments:write` | Optional `{"as_of": ...}`; idempotent; US-541. |
| `POST /api/v1/installments/plans/{id}/remind` | `sms:manage` | Manually trigger an SMS reminder for one leg (US-544); 201. |
| `GET /api/v1/installments/plans/{id}/reminders` | `installments:read` | The plan's reminder log (US-544). |

Amounts in requests are Toman (×10 to Rial at the boundary, once);
responses carry Rial (`*_irr`) with Toman alongside. Dates are
Jalali `YYYY-MM-DD` in the API, Gregorian in storage.

## SMS reminders (US-544)

Due-date reminders live in the SMS module
(`docs/sms-providers.md`): a multi-provider registry (Kavenegar +
sms.ir built-ins, simulator default), a daily Procrastinate job
that sends at contract offsets (3 days before, on the day, 1 day
overdue by default), and the manual trigger above. Automatic
sends require the installation opt-in AND the contract opt-in;
the manual trigger is itself the opt-in for that one send. No
reminders for `pending_clearance` legs (the cheque may still
clear) — the reminder service reads the real cheque states
through the cheque repository (wired at the US-544 merge) and
computes received vs pending with US-542's `split_allocations`.
No reminders for BNPL plans either — the customer owes the
gateway, not the seller.

## Deferred (later 5ج stories)

- **US-541** — DONE (this doc's finance section): principal/interest
  split per leg (user-entered rate); finance income recognised
  GRADUALLY on COLLECTION (not on receipt — a bounced cheque never
  recognises income); stepped dunning + `installment_overdue`
  review items; late fee computation + draft journal; early
  settlement (payoff quote + atomic close) + applying the advance
  / re-amortisation; effective-contract-version selection;
  `GET /overdue`.
- **US-542** — DONE: receipts by cheque (`pending_clearance`
  until the cheque clears; clear → leg paid, bounce → leg
  unpaid); the two-way contract test deferred from US-531;
  `collected_at` as the collection hook for US-541.
- **US-543** — DONE (this doc's BNPL section): BNPL receipts
  (SnappPay / DigiPay) — a BNPL-paid leg is a new
  `gateway_payment` linked from the receipt, settled per tranche
  by the US-521 engine; the customer's own installments to the
  gateway are NEVER in the seller's books.
- **US-544** — DONE (this doc's SMS section + `docs/sms-providers.md`):
  SMS due-date reminders (multi-provider registry: Kavenegar +
  sms.ir + simulator default; daily Procrastinate job; manual
  trigger); no reminders for `pending_clearance` legs or BNPL
  plans.
- **US-545** — installments center UI + 11 reports. The one backend
  addition: `GET /installments/plans/{id}/amendments` (the
  plan-change audit history — US-541 recorded the amendment rows
  but exposed no read for them). The plan-change flow in the UI is
  the early-settlement (partial) flow + the amendment history view;
  there is no separate plan-change write API.
- **Later** — the `order.completed` → schedule derivation emitting
  `installment.schedule.created` (ADR 0005); move-existence
  verification + the linkage-compliance report (§7.5); schedule
  amendment with history (`sale_amendment`); collateral-cheque
  call-in on overdue legs (human decision in the review queue).
