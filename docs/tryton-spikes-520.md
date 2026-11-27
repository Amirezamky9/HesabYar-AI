# Tryton §3.8 spikes — US-520 (Phase 5-3, Wave A)

**Question (gates US-522):** which posting path does the settlement
engine use — Path 1 (wizard-driven: Tryton creates the settlement
moves) or Path 2 (direct move via the port: the adapter posts journal
drafts itself)?

**Decision: Path 1b — button-driven (no wizards, no JSON-RPC).**
Per-tranche `account.payment` rows, driven `draft → submit →
process_rest → succeed` with `clearing_date` = the gateway payout
date in the REST context; the clearing module posts the
receivable → clearing move. Path 2 (direct journal via the port) is
reserved for fee / fee-VAT recognition moves, variance adjustments,
and post-hoc settlement legs where the invoice was posted without
`alternative_payees`. Pay Invoice is NOT used for settlement.

**Relationship to `docs/tryton-spikes-502.md`:** US-502 already closed
all four §3.8 spikes from source + live probes (2026-11-18) and
established "Path 1b". This document re-verifies each spike
independently against the installed Tryton 8.2.3 source, confirms the
findings against the adapter as it stands today, and makes the
US-522-gating decision explicit — including the one wrinkle US-502
left open (net-vs-gross reconciliation, §5). Nothing here contradicts
the 502 doc; where the two overlap, the 502 doc is the deeper record.

Researched 2026-11-19 against Tryton 8.2.3 (installed source in
`~/workspace/projects/hesabyar-reference/tryton-env/`, Python 3.12)
and the adapter at `ralph/phase-5-3` (`a3e02fe`). Every claim cites a
source file.

## 0. The mechanism (recap from the 502 doc)

The REST protocol (`trytond/protocols/rest.py`) has NO wizard route —
the word "wizard" does not appear in it. Wizards run only via
JSON-RPC/XML-RPC (`wizard.*.execute`). The REST action route
(`rest.py:250-281`) calls only methods listed in the model's
`__rpc__`; anything else returns 403.

The key fact is that **every `@ModelView.button` is auto-registered
in `__rpc__`** (`trytond/model/modelview.py:206-212`) — with one
correction the 502 work proved live: a button's `invisible` state is
enforced server-side (`modelview.py:791`), so "is it a button?" is
necessary but not sufficient — the button must also be *visible* in
the record's state. That is exactly why `proceed` is not the payment
forward step (§1).

## 1. Spike 1 — payment wizards (Process / Succeed with clearing date) via REST?

**Wizards: no. Workflow: yes, via buttons + one existing wrapper.
No new Tryton code needed for Path 1b.**

- `account.payment` buttons (`account_payment/payment.py`): `draft`,
  `submit`, `approve`, `proceed`, `succeed`, `fail` — all in
  `__rpc__`, all callable via the REST action route. The adapter
  already calls `submit` / `approve` / `succeed` / `fail` this way
  (`adapter.py:3229-3266`).
- `proceed` is NOT the forward step: its button state makes it
  visible only for succeeded/failed manual payments (re-processing),
  so calling it on a submitted payment fails with "You can not call
  proceed" (proven live by the US-502 contract tests). The real
  forward step is `process` (`payment.py:763-778`), a plain
  classmethod — NOT a button, NOT in `__rpc__` — reached in stock
  Tryton only through the `account.payment.process` wizard.
- The `hesabyar_setup` module already exposes a thin `process_rest`
  wrapper (`tryton-modules/hesabyar_setup/.../payment.py`) that
  mirrors the wizard's `do_process` (same eligibility filter, same
  grouping, same `process` call) with access checks disabled for the
  call — exactly the scope the wizard's `execute` RPC disables them
  for. A plain `__rpc__` registration of `process` does NOT work
  (verified live in US-502): `process` calls the `proceed` button
  internally, the button wrapper enforces `proceed`'s invisible
  state whenever access checks are on, and the REST dispatcher never
  consults the RPC `check_access` flag. The adapter already uses it
  (`adapter.py:3237-3245`, `process_payment`).
- The Process wizard does nothing the wrapper skips except the
  overpay warning (a dismissible user confirmation, meaningless over
  REST — the adapter validates amounts itself) and returning the
  group form action (a UI concern). For manual journals,
  `process_manual` is a no-op (`payment.py:142`), and the group is
  created by the wrapper (manual payments require a group
  server-side). The wizard's eligibility filter (approved, or
  submitted + receivable) is replicated in the wrapper.
- `succeed` stays a button under the clearing module
  (`account_payment_clearing/payment.py:209`): it creates the
  clearing move via `_get_clearing_move()`, reconciles on full
  payment, and reads the clearing date from
  `Transaction().context.get('clearing_date')` (`:304`, defaults to
  today). The Succeed wizard (`:498-511`) does nothing else — it sets
  the context and calls `succeed`. The REST `X-Tryton-Context`
  header IS decoded with Tryton's JSONDecoder (`rest.py:28-49`), so
  a wire-format date dict arrives as a real date; the adapter's
  `contracts.encode_context` already wire-encodes values, and
  `succeed_payment` already passes `clearing_date` this way
  (`adapter.py:3247-3260`). Proven live by the sales contract tests
  (clearing move dated to the clearing date, 2026-11-20).

**Consequence:** the settlement flow per tranche is
`create_payment(line=tranche line, amount=net tranche)` →
`submit` → `process_rest` → `succeed(clearing_date=payout date)`,
all over REST. No wizard, no JSON-RPC, no new module code.

**Fail-closed precondition (US-522 must enforce):**
`_get_clearing_move` returns `None` — silently, no move — when the
payment journal has no `clearing_account` / `clearing_journal`
(`account_payment_clearing/payment.py:302-306`). `succeed` still
transitions the payment to `succeeded`. So before processing, the
adapter/service MUST verify the channel's journal is clearing-
configured and refuse otherwise; a succeeded payment with no
clearing move is a silent accounting hole.

## 2. Spike 2 — can the adapter set party=gateway + maturity_date=tranche due on each line?

**Yes — with one constraint: party only on party_required accounts.**

- `account.move.line.party` is a plain writable Many2One and
  `maturity_date` a plain writable Date (`account/move.py:1015-1033`;
  `has_maturity_date` is display-only). Both are settable via REST
  create on a draft move.
- Server-side `Line.check_account` (`move.py:1422-1436`) enforces
  `bool(line.party) == bool(line.account.party_required)`:
  party is REQUIRED on party_required accounts and FORBIDDEN on
  non-party-required accounts (`party_set` / `party_required`
  AccessError). `on_change_account` clearing party is client-side
  only and never runs on REST writes.
- `maturity_date` has NO such constraint — it is a free Date field
  on any line. It is also in `_check_modify_exclude`, so it stays
  editable even on posted moves; both fields are frozen once the
  line is reconciled (`_reconciliation_modify_disallow`).

**Design consequence:** the transfer-move receivable leg carries
party=gateway + maturity_date=tranche due; the clearing/bank leg
carries no party (those accounts must NOT be party_required).
Provisioning must mark the gateway-receivable account
party_required. The adapter does NOT support this today:
`create_journal_draft` sends only account/debit/credit/description
per line (`adapter.py:529-538`) and `_resolve_accounts` REJECTS
party-required accounts ("party-linked lines are not supported
yet", `adapter.py:849-852`). US-522 needs a small adapter
extension: optional `party` / `maturity_date` on the journal-line
command, passed through to the REST create, with the
party_required rejection relaxed for lines that DO carry a party.
No engine change.

Note the path-1b corollary: the clearing module's auto-created
clearing move sets party on the receivable leg ONLY if that
account is party_required (`_get_clearing_move`,
`account_payment_clearing/payment.py:331-332`, party =
`clearing_party` = the payment line's party, or the payment's
party) and sets NO maturity_date at all. Tranche dues in path 1b
live on the invoice's `lines_to_pay` (via the payment term, §4),
not on the clearing move — which is correct: dunning/aging reads
the receivable lines, and the clearing leg is transient.

## 3. Spike 3 — amount_to_pay behaviour with a Draft payment?

**A Draft payment DOES reduce `amount_to_pay` — immediately, at creation.**

- `Payment.amount_line_paid` (`account_payment/payment.py:392-400`)
  returns `max(min(self.amount, payment_amount), 0)` for EVERY state
  except `'failed'` — draft included.
- The `get_amount_to_pay` override
  (`account_payment/account.py:806-847`) subtracts every payment on
  `line.payments` (plain One2Many, no state filter). The stored
  `payment_amount_cache` likewise sums all non-failed payments
  (`set_payment_amount`, `account_payment/account.py:167-190`,
  refreshed on payment create/write/delete), so the move line's
  `payment_amount` also drops at Draft creation.
- Pay Invoice enforces `amount <= invoice.amount_to_pay`
  (`account_invoice/invoice.py:4118-4127`), so a Draft payment for
  the full amount blocks a second payment for the same line.

**Design consequence (binding for US-522):** create `account.payment`
rows ONLY when the tranche is actually being processed (step C3,
after the gateway file confirms it) — never speculatively at
schedule time. The schedule lives in HesabYar
(`settlement_tranche`); Tryton payments are created at confirm
time. A Draft payment that never progresses leaves
`amount_to_pay` understated; the remedy is delete (only Drafts can
be deleted) or `fail` (failed payments don't count). The clearing
override (`account_payment_clearing/payment.py:147-165`) removes
the payment from `amount_line_paid` again once the clearing line is
reconciled (bank statement posted) — the invoice then reads paid
through the reconciliation itself, not through the payment.

## 4. Spike 4 — Pay Invoice: Party stays the customer, or can the gateway Party be set?

**The gateway Party CAN be set — via `alternative_payees` — but Pay
Invoice itself is NOT REST-reachable, so it plays no part in the
settlement flow.**

- `Invoice.pay_invoice(..., party=None)` accepts an explicit party
  (defaults to the invoice party,
  `account_invoice/invoice.py:1806-1810`) and puts it on every
  party_required line of the payment move (`:1890-1892`).
- The PayInvoice wizard exposes `payee`, restricted by domain to
  `[invoice.party] + invoice.alternative_payees`
  (`:3892-3912`, `:4040-4048`).
- `Invoice.alternative_payees` is a Many2Many with **max 1 before
  posting** (`:258-265`, `size=If(~Eval('move'), 1, None)`). When
  exactly one is set, the posted invoice move's receivable lines
  get party = the alternative payee instead of the customer
  (`_get_move_line`, `:1280-1285`), AND `line.maturity_date` is set
  from the payment-term line date (`:1286`). Reconciliation-line
  lookup filters by the payee party (`:1798-1801`), so a gateway
  payee reconciles only when the receivable line is also the
  gateway — which the single-alternative-payee rule guarantees.
- BUT `pay_invoice` is not a button and not in `__rpc__`, and the
  wizard cannot run over REST — so the adapter cannot drive Pay
  Invoice at all. (Proven live in US-502: an invoice with a single
  alternative payee posts its receivable line to the gateway party.)

**Design consequence:** the gateway party reaches the receivable
line at INVOICE time, by plain REST field write:
`alternative_payees=[gateway]` + a multi-step payment term whose
lines carry the tranche dues (maturity dates). This is the default
for store orders, where the gateway is known at invoice time.
`CreateInvoiceDraft` does not carry these fields today
(`domain/accounting/commands.py:267-297`) — US-521/US-522 adds
`payment_term` + `alternative_payees` (plain REST writes, no engine
work). When the gateway is NOT known at invoice time, the fallback
is Path 2: a direct transfer move with party=gateway +
maturity_date=tranche due on the receivable leg (§2).

## 5. The US-522 posting design (what the decision means in practice)

Per gateway payment (one `settlement_tranche` row per tranche):

1. **Sale (invoice posting, US-511/US-513 path):** invoice with
   party=customer, `alternative_payees=[gateway]`, payment term =
   the contract's `tranche_legs`. Posted receivable lines carry
   party=gateway, maturity_date=tranche due (§4). Revenue is gross.
2. **Fee recognition (policy A, at sale):** direct journal via the
   port (Path 2 mechanics — this is not a payment): Dr fee expense
   + fee VAT / Cr receivable-from-gateway, party=gateway on the
   receivable leg (§2). Account balance of the receivable is now
   net; the invoice's `lines_to_pay` lines still show gross.
3. **Tranche confirmed (C3, gateway file):** create
   `account.payment` (line = the tranche's `lines_to_pay` line,
   amount = NET tranche, date = payout date) → `submit` →
   `process_rest` → `succeed(clearing_date = payout date)`.
   The clearing module posts Dr clearing / Cr receivable (net) and
   sets `payment.clearing_move`. Create the payment ONLY at this
   point (§3).
4. **Bank deposit (C4–C6):** statement import + matching (US-514
   machinery) reconciles the clearing line against the bank
   statement line: Dr bank / Cr clearing. NOT part of the payment
   flow — base Tryton payments reconcile nothing on their own (502
   doc §1, end).

### Open design point for US-522: net-vs-gross reconciliation — RESOLVED (option (a))

US-522 implemented option **(a)**: a `reconcile_rest` wrapper in
`hesabyar_setup` (`account.move.line`, same pattern as
`process_rest` — `without_check_access()`, ids in the body, called
over the no-id REST route `/model/account.move.line/reconcile_rest`).
The adapter exposes it as `reconcile_move_lines` with the same
pre-validation `Reconciliation.check_lines` enforces (all lines
exist for the company, one account, one party, every line `valid`
and unreconciled, debits == credits) and the posting service
reconciles each tranche's [invoice line (gross debit) + fee leg
(fee credit) + clearing leg (net credit)] once the tranche is fully
cleared — plus the two clearing legs (payment Dr + bank Cr) once the
tranche is fully received.

Two facts make this work (both verified live, see §6):

- The clearing `succeed` creates the clearing move as **DRAFT**, and
  `Line.state` is balance-driven: a BALANCED draft move has `valid`
  lines, so the draft clearing leg is reconcilable. The service
  still posts the clearing move first, then reconciles.
- The fee move (policy A) splits its receivable leg per tranche
  (one credit line per tranche, party=gateway,
  maturity_date=tranche due), so each tranche's three lines sum to
  zero independently.

The original analysis below stands as the record of the decision:

The clearing `succeed` reconciles the payment's line ONLY when the
clearing leg + the invoice line sum to zero
(`account_payment_clearing/payment.py:220-228`). The tranche
payment is NET while the invoice line is GROSS, so they do NOT sum
to zero and the line stays unreconciled. The three lines that DO
sum to zero are [invoice line (gross debit) + fee line (fee credit)
+ clearing leg (net credit)] — but `account.move.line.reconcile`
is NOT RPC-exposed (adapter docstring already records this), so
HesabYar cannot reconcile them over REST today.

Options, in recommended order:

- **(a) `reconcile_rest` wrapper in `hesabyar_setup`** (same pattern
  as `process_rest`): a thin RPC-exposed method taking line ids and
  calling `Line.reconcile` (which accepts any list of lines summing
  to zero, `account/move.py:1605-1687`; the reconciliation date
  derives from the lines' maturity dates). Small, same-shape change
  as the one US-502 already shipped. Recommended — full
  reconciliation keeps dunning/aging exact. **← implemented by
  US-522; live-verified by the SnappPay e2e
  (`tests/contract/test_settlement_posting.py`).**
- **(b) Accept unreconciled lines.** The receivable ACCOUNT balance
  per gateway party is still exactly net (all three lines sit on
  the same account+party), so party-level aging is correct; but
  dunning works per LINE, so the gross invoice line would keep
  showing as overdue after its tranche was paid. Only acceptable if
  US-522 disables gateway-party dunning or scopes it to
  HesabYar-side tranche state instead of Tryton lines. **Not
  taken.**

US-522 also added `process_rest_batch` to `hesabyar_setup`
(`account.payment`, same no-id REST pattern): one call drives a
whole settlement batch's payments to `processing`, so the batch
lands in ONE `account.payment.group` instead of one group per
payment.

## 6. Preconditions checklist for US-522 (all done, US-522)

- [x] Payment journal per gateway channel, `process_method=manual`,
  with `clearing_account` + `clearing_journal` configured
  (fail-closed check before processing — §1). The posting service
  refuses to clear when the journal lacks clearing config
  (contract-tested).
- [x] Gateway-receivable account is `party_required`; clearing and
  bank accounts are NOT (§2). New chart accounts (hesabyar_setup):
  1.2.8 Gateway Receivables (party_required, reconcile), 1.1.6
  Gateway Clearing (cash type, reconcile, NOT party_required),
  5.2.4 Gateway Fees (SG&A). Fee VAT goes to 6.3.6 Non-Deductible
  VAT (`fee_vat_claimable=false` until an official gateway
  invoice/Moadian record is verified).
- [x] Adapter: `CreateInvoiceDraft` gains `payment_term` +
  `alternative_payees` (§4).
- [x] Adapter: journal-line command gains optional `party` /
  `maturity_date`; party_required rejection relaxed for lines
  carrying a party (§2). The rule now mirrors the engine's
  `Line.check_account`: `bool(party) == account.party_required`.
- [x] Adapter/service: payments created only at tranche-confirm
  time (C3), never at schedule time (§3).
- [x] US-522 decides the §5 reconciliation option (a) or (b):
  **(a) `reconcile_rest`**, implemented in `hesabyar_setup` and
  live-verified by the SnappPay e2e.

## 7. Sources

- Installed source (trytond 8.2.3): `trytond/protocols/rest.py`,
  `trytond/model/modelview.py`, `trytond/modules/account/move.py`,
  `trytond/modules/account_payment/`,
  `trytond/modules/account_payment_clearing/`,
  `trytond/modules/account_invoice/`.
- `docs/tryton-spikes-502.md` (§§0–4, §6 live probes — all green
  2026-11-18, contract suite 13/13).
- Adapter: `src/hesabyar/infrastructure/accounting/tryton/adapter.py`
  (`process_payment` / `succeed_payment` / `create_journal_draft`),
  `tryton-modules/hesabyar_setup/.../payment.py`
  (`process_rest`).
- Phased design §3.5 + §3.8
  (`hesabyar-reference/phased-design-parts/part-b-gateways-settlement.md`),
  ADR 0003 (`docs/adr/0003-store-connectivity-banking.md` §6:
  in BNPL the receivable is from the gateway, not the customer).
