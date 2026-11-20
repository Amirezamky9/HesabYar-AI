# Tryton §3.8 spikes — US-502 (Phase 5, F2)

**Question:** can the phased design's store-connectivity flows (gateway
settlement, cheque clearing, BNPL tranches) be driven through Tryton's
REST API, or do they need wizards / JSON-RPC / direct journals?

**Verdict: "Path 1b — button-driven", with one small module bridge.**
No wizards, no JSON-RPC for the payment/sale/invoice flows. Every
workflow step the design needs is a `@ModelView.button` — EXCEPT the
payment forward step submitted/approved → processing, which is the
`process` method (wizard-only in stock Tryton). The hesabyar_setup
module exposes a thin `process_rest` wrapper over REST that
delegates to the engine's `process`; that is the only new Tryton
code the flows need. The
one exception is the Pay Invoice leg with a gateway party, which
needs a direct journal (Path 2) — see Spike 4.

Researched 2026-11-18 against Tryton 8.2.3 (installed source in
`~/workspace/projects/hesabyar-reference/tryton-env/`, Python 3.12).
Every claim cites a source file; live probes against the dev server
confirm the REST behaviour end to end (§6).

## 0. The mechanism: buttons are REST actions

The REST action route (`trytond/protocols/rest.py:250-281`) only calls
methods listed in the model's `__rpc__`; an unknown action returns 403
(`trytond/protocols/dispatcher.py:219-220`). There is no wizard route:
the word "wizard" does not appear in `rest.py`.

The key fact is that **every `@ModelView.button` is auto-registered in
`__rpc__`** (`trytond/model/modelview.py:206-212`). So the question
"is X reachable via REST?" reduces to "is X a button?". A
`button_action` (which launches a wizard) is also a button, but calling
it via REST returns only the action descriptor — the wizard itself
cannot run over REST.

`POST /rest/model/<model>/<id>/<action>` with a JSON body calls the
method with the body as `**kwargs`; when the method returns `None`,
the route returns the updated record (`_read`) — so a workflow
transition returns the updated view in one call.

## 1. Spike 1 — payment wizards (Process / Succeed with clearing date) via REST?

**Wizards: no. Workflow: yes, via buttons.**

`account.payment` buttons (`account_payment/payment.py`): `draft`,
`submit`, `approve`, `proceed`, `succeed`, `fail` — all REST-action
callable. But **a button being callable is not the same as it being
the forward step**: `proceed`'s button state (`payment.py:462-467`)
makes it visible only for succeeded/failed manual payments
(re-processing), so calling it on a submitted payment fails with "You
can not call proceed" (proven live by the contract tests, run 2).
The real forward step is `process` (`payment.py:763-778`, a
`@Workflow.transition('processing')` classmethod): it calls
`proceed` (reconciliation check + the transition) and then the
journal's `process_<method>` hook — `process_manual` is a no-op
(`payment.py:142`). `process` is NOT a button and NOT in `__rpc__`
in stock Tryton, and the `process_wizard` button_action launches the
`account.payment.process` wizard, which cannot run over REST
(calling it returns only the action descriptor). **So the
hesabyar_setup module exposes a thin `process_rest` wrapper**
(`tryton-modules/hesabyar_setup/.../payment.py`): it delegates to
the engine's own `process` with access checks disabled for the
duration of the call -- exactly the scope the wizard's `execute` RPC
disables them for. A plain `__rpc__` registration of `process` does
NOT work: `process` calls the `proceed` button internally, the
button wrapper enforces `proceed`'s invisible state whenever access
checks are on, and the REST dispatcher never consults the RPC
`check_access` flag (the REST auth wrapper in
`protocols/wrappers.py` enables checks for the whole request
unconditionally) -- verified live, it fails inside with "You can
not call proceed". Called
with no group (the REST action passes no arguments), it runs the
engine's own path for manual payments; no state is bypassed and no
engine logic is duplicated. Note the button-state rule this
correction taught us: a button's `invisible` condition is enforced
server-side (`modelview.py:791`), so "is it a button?" is necessary
but not sufficient — the button must also be *visible* in the
record's state.

The clearing module (`account_payment_clearing/payment.py`) keeps
`succeed` as a button (line 209): it creates the clearing move via
`_get_clearing_move()` (line 289), reconciles, and reads the clearing
date from `Transaction().context.get('clearing_date')` (line 304,
defaults to today). The REST `X-Tryton-Context` header IS decoded with
Tryton's JSONDecoder (`protocols/rest.py:28-49`), so a wire-format date
dict `{"__class__": "date", ...}` arrives as a real date; the adapter's
`contracts.encode_context` already wire-encodes values.

**Decision: draft → submit → process → succeed, with `clearing_date`
passed in the REST context.** `process` is reached over REST through
the hesabyar_setup module's `process_rest` wrapper (the only new
Tryton code the flows need).
CORRECTED 2026-11-18: the first draft of this doc said `proceed`
instead of `process` — wrong, see above. A plain `__rpc__`
registration of `process` was also tried and fails: the REST
dispatcher ignores the RPC `check_access` flag, so the wrapper is
required. (Confirmed by live probe §6 and the contract tests.)

**Reconciliation is NOT part of the payment flow.** A base Tryton
payment creates no accounting move (`Payment.succeed` is a bare
`pass` apart from the state transition) and reconciles nothing: the
invoice only *looks* paid because `amount_to_pay` subtracts
non-failed payments. Real reconciliation comes from (a) the clearing
module's `succeed`, which reconciles the linked line when the
payment covers it in full (clearing move leg + invoice line sum to
zero), or (b) bank-statement matching (phases 5ت/5چ). Consequences
for the design: settlement tranches that must close the receivable
need a clearing journal (or a statement match); the adapter's
reconciled-line refusal only triggers on lines reconciled by one of
those two paths.

## 2. Spike 2 — can the adapter set party=gateway and maturity_date=tranche due on each line?

**Yes, for receivable/payable lines.**

`account.move.line` has plain writable `party` (Many2One,
`account/move.py:1015`) and `maturity_date` (Date, `:1028`;
`has_maturity_date` is display-only). Server-side `Line.check_account`
(`:1422-1436`): `bool(line.party) != bool(line.account.party_required)`
raises AccessError — party is allowed ONLY on party_required accounts
and forbidden on non-party-required accounts (e.g. the bank/clearing
leg). `on_change_account` clearing party is client-side only and never
runs on REST writes.

**Design consequence:** the transfer-move receivable leg carries
party=gateway + maturity_date=tranche due; the clearing/bank leg
carries no party.

## 3. Spike 3 — amount_to_pay behaviour with Draft payments?

**Draft payments DO reduce it.**

`Payment.amount_line_paid` (`account_payment/payment.py:392-400`)
returns the amount for ANY state except `'failed'`; the
`get_amount_to_pay` override (`account_payment/account.py:806+`)
subtracts every payment on `line.payments` (plain One2Many, no state
filter, `:57`). Pay Invoice enforces `amount <=
invoice.amount_to_pay`, so a draft payment blocks paying the same
amount again.

**Design consequence:** create payments only when the tranche is
actually being processed, never speculatively. (To be confirmed by
live probe §6.)

## 4. Spike 4 — Pay Invoice: Party=customer, or can the gateway Party be set?

**Gateway party IS settable — but NOT via REST.**

`Invoice.pay_invoice(..., party=None)` accepts an explicit party
(defaults to the invoice party, `account_invoice/invoice.py:1806`);
the PayInvoice wizard exposes `payee` (invoice party +
`alternative_payees`, `:3892+`); `Invoice.alternative_payees`
(Many2Many, max 1 before posting, `:258`) — when exactly one is set,
the posted invoice move's receivable line gets party = alternative
payee (`_get_move_line`, `:1281-1282`).

BUT `pay_invoice` is not a button and not in `__rpc__`, and the wizard
is not REST-reachable — so the adapter cannot drive it. Two ways to
get the gateway party onto the receivable line:

- **(a) `alternative_payees` at invoice create** — a plain REST field
  write; the posted move then carries the gateway party. Works when
  the gateway is known at invoice time (store orders: yes).
- **(b) Path 2 — direct journal via the port** — the adapter posts a
  transfer move (receivable leg: party=gateway, maturity_date=tranche
  due) when the payment leg must be built after posting.

**Decision: (a) is the default; (b) stays available for post-hoc
settlement legs.** The port already supports (b) through
`create_journal_draft` + `post_journal`; no new engine code is needed
for either. (Live probe §6 will confirm (a): an invoice with a
single alternative payee posts its receivable line to the gateway
party.)

## 5. Related findings (same source reading)

- **Credit notes:** Tryton 8.2 has NO `out_credit_note` type
  (`account_invoice/invoice.py:98-101`, only `out`/`in`). A credit
  note is the same type with NEGATIVE quantities
  (`InvoiceLine._credit`, `:3192`). `Invoice.credit` (`:1973`) is
  not a button — wizard-driven only — so the adapter mirrors it with
  a plain REST create (negated quantities, `origin` linking the source
  lines).
- **Invoice transitions:** `validate_invoice` (`:2035`) and `post`
  (`:2082`) are buttons → REST actions (`post` also explicitly in
  `__rpc__` with `fresh_session=True`, `:542-545`).
- **Sale transitions:** `quote` (`sale/sale.py:1158`), `confirm`
  (`:1182`), `process` (`:1216`), `cancel` (`:1140`) are buttons →
  REST actions. `confirm` queues the engine's own processing via
  `__queue__` (needs a worker), so the adapter calls `process`
  directly afterwards for determinism. `do` (`:1211`) is not a button.
  `set_sale_date` (`:980-987`) only sets sale_date when empty — the
  adapter can set it at create. `invoices` is a Function Many2Many
  (`:260`).

## 6. Live probes

Two layers, both run after module activation (US-502 steps 7–9):

**A. Probe script** (`~/workspace/coord-logs/us502-probes.py`) — the
`hesabyar_setup` module on a scratch company, against the LOCAL
trytond (:8011, local PostgreSQL copy of the Supabase DB). Local, not
Supabase: a company with a chart cannot be deleted
(`account.account.company` is `ondelete="RESTRICT"`), so
scratch-company pollution must stay in the throwaway local copy; the
local DB is re-restored and re-activated before the Supabase restore,
so no probe data ships.

| Probe | Result |
| --- | --- |
| P0 `install_chart` via REST action (fresh company) | PASS 2026-11-18 — 145 accounts (135 universal + 10 HesabYar), defaults set |
| P1 chart contents: 135 universal + 10 HesabYar accounts; defaults 1.2.1/2.1.1 | PASS — missing=set(); memorandum leaf type=Off-Balance, party_required; config defaults 1.2.1/2.1.1 |
| P2 `install_chart` refuses a second install | PASS — UserError "already has a chart of accounts" |
| P3 `update_chart` on a fresh company adds 0 accounts | PASS — 145 → 145, accounts_added=0 |
| P4 fiscal year + 12 periods via REST | PASS — 12 periods |
| P5 draft sale create + quote via the adapter | PASS — sale quoted (number=4); needs the category revenue account (see §8, issue 1) |

Cleanup note: the scratch company/party cannot be deleted
(`account.account.company` is `ondelete="RESTRICT"`), as predicted
— local-only pollution, dropped by the re-restore before the
Supabase push.

**B. Contract tests** (`tests/contract/test_tryton_adapter_sales.py`)
— the port flows against the Adapter Test company on Supabase
(:8010):

| Probe | Result |
| --- | --- |
| Payment submit → process → succeed with `clearing_date` in context | PASS — clearing move dated to the clearing date (2026-11-20, draft); group assigned |
| Sale quote → confirm → process | PASS — invoice created (draft, 500,000 IRR) |
| Credit note from a posted invoice | PASS — negated quantities, origin links source lines |
| Invoice with single alternative payee = gateway party | PASS — posted receivable line carries the gateway party |
| Draft payment vs `amount_to_pay` | PASS — draft payment reduces amount_to_pay (750,000 → 500,000) |

**Status: all green 2026-11-18.** Contract suite 13/13:
run 3 passed 12/13 (20:10), the remaining
`test_create_payment_rejects_reconciled_line` passed alone in run 4
(5:16) after the clearing-journal fix. Unit tests 37/37 (sales) +
149 adapter tests; ruff clean; pyright 0 errors.

## 7. Inventory accounting method — ANGLO-SAXON

Decided 2026-11-18, **before** installing `account_stock_*`
(the research had left this UNVERIFIED).

- Iranian Standard 8 (repo corpus `standards/08-inventory/`, rev 1397,
  IAS 2 equivalent) governs measurement only: §24 explicitly allows
  both perpetual (محاسبه دایمی — moving weighted average) and periodic
  computation; §29 allows specific identification, FIFO, weighted
  average; LIFO is banned. It mandates neither bookkeeping method.
- Iranian practice has both ادواری (periodic ≈ continental: purchases
  to expense "خرید", stock adjusted at period end) and دائمی
  (perpetual ≈ anglo-saxon: purchases debit inventory directly, COGS
  at sale); Sepidar (mainstream Iranian software) uses perpetual as
  the default. Sources: mahaksoft, sepidarsystem, gheyas (web search
  2026-11-18).
- Anglo-saxon fits HesabYar: real-time store connectivity needs live
  stock value (phase 5چ packaging reorder + per-order cost).
- Tryton cost methods are FIFO + average only (no LIFO — consistent
  with the Iranian ban).

**Installed:** `account_stock_anglo_saxon` 8.2.0, which depends on
`account_stock_continental` + `account_invoice_stock` (so continental
installs as a dependency; anglo-saxon behaviour governs).
`account_stock_landed_cost` depends on neither — the decision was
genuinely open, and is now recorded here.

## 8. Live issues found during verification

All found 2026-11-18 while running the probes / contract tests
against the activated modules; each is fixed in code, tests, or
provisioning (not just documented).

1. **`account_revenue` does not exist on `product.template`.**
   The MultiValue exists only on `product.category`
   (`account_product/product.py:81`); the template resolves it via
   `account_category`. Setting it on a template create raises an
   engine KeyError. Provisioning pattern: create
   `product.category` (accounting=True) → `product.category.account`
   (category + company + account_revenue) → template with
   `account_category`. (Fixtures + probe script fixed.)
2. **`shipment_cost_method` default breaks quote.**
   `sale_shipment_cost` defaults it to `'order'`, but its domain
   requires `None` when the sale has no carrier. The adapter sends
   `shipment_cost_method: None` at create. (Adapter + unit test.)
3. **Config defaults were never set for the test company.**
   The provisioning script only pre-filled them when exactly one
   receivable/payable account existed (the universal chart has
   three of each), so `account.configuration` had no singleton row
   at all — and a REST write to the missing singleton silently does
   nothing (`ModelSingleton.read` returns a phantom record with
   None fields). The sale flow needs the defaults (party fallback).
   Fixed in the provisioning script (explicit 1.2.1/2.1.1) and set
   on the dev DB via trytond-console.
4. **No invoice sequences on FY 2026.** Posting an invoice needs
   `account.fiscalyear.invoice_sequence` rows (4 strict sequences);
   Phase 2 never posted, so the gap surfaced only now. Provisioned
   on the dev DB via REST; the provisioning script now creates them.
5. **`proceed` is not the payment forward step.** See §1: the
   forward step is `process` (wizard-only), reached via the
   `process_rest` wrapper; a plain `__rpc__` registration fails
   because the REST dispatcher ignores the RPC `check_access` flag.
6. **Base payments reconcile nothing.** See §1 (end): only the
   clearing move (full payment) or bank-statement matching
   reconciles a line. The reconciled-line contract test must use a
   clearing journal.
7. **A processed sale can never be cancelled** (workflow allows
   cancel only from draft/quotation), so the sale-flow test's sale
   stays in the shared DB as documented residue, like posted moves.

## 9. Sources

- Installed source (trytond 8.2.3): `trytond/protocols/rest.py`,
  `trytond/protocols/dispatcher.py`, `trytond/model/modelview.py`,
  `trytond/modules/account/move.py`, `trytond/modules/account_payment/`,
  `trytond/modules/account_payment_clearing/`,
  `trytond/modules/account_invoice/`, `trytond/modules/sale/`.
- Phased design checklist §3.8 (`hesabyar-reference/
  phased-design-checklist-2026-11-17.md`, lines 1433–1472).
- Iranian Standard 8: `standards/08-inventory/source/standard.md`.
