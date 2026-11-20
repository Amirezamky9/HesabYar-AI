# Tryton REST adapter (US-003, US-201, US-204, US-202 follow-up)

The `AccountingEnginePort` (`src/hesabyar/domain/accounting/ports.py`) is the
engine-neutral seam between HesabYar and its accounting engine. The production
implementation is `TrytonAccountingAdapter`
(`src/hesabyar/infrastructure/accounting/tryton/adapter.py`), backed by a
small synchronous REST client (`client.py`).

Scope: company/chart reads, journal draft **create + validate + list +
update**, journal **post + reverse**, posted-journal read, fiscal-year/period
reads (US-201, Phase 2; list + update and the `get_journal_draft` port
method from the US-202 follow-up), invoice draft **create + validate**,
invoice reads, and payment and bank-statement reads (US-204, Phase 2),
plus bank-statement **create + line update** and statement-journal
reads (US-514, Phase 5-2).
Reporting is a later story; the port is the seam, so extending it does not
touch domain callers.

## Layout

```text
src/hesabyar/domain/accounting/          # engine-neutral, no Tryton/HTTP/SQL imports
  refs.py          CompanyRef / AccountRef / JournalRef / JournalDraftRef /
                   PostedJournalRef / FiscalYearRef / FiscalPeriodRef /
                   PartyRef / PartyAddressRef / InvoiceRef / PaymentRef /
                   BankStatementRef
  value_objects.py AccountView / CompanyView / FiscalYearView /
                   FiscalPeriodView / JournalDraftView / PostedJournalView /
                   InvoiceView / InvoiceLineView / PaymentView /
                   BankStatementView / BankStatementLineView /
                   ValidationSummary
  commands.py      CreateJournalDraft (+ invariant checks at construction) /
                   UpdateJournalDraft (same line invariants when lines change) /
                   PostJournal / ReverseJournal /
                   CreateInvoiceDraft / InvoiceDraftLine (+ invariant checks)
  ports.py         AccountingEnginePort protocol

src/hesabyar/infrastructure/accounting/tryton/   # Tryton specifics, REST only
  client.py        TrytonRestClient: search/read/create/write/delete/action + ping
  adapter.py       TrytonAccountingAdapter: the port implementation
  mapping.py       Tryton record <-> domain mapping; integer-IRR money rules
  errors.py        Tryton/HTTP failure -> stable HesabYar error translation
  contracts.py     REST wire format (Decimal/date encoding, domain/order/context encoding)

tests/unit/test_tryton_adapter.py        # no server: MockTransport + static architecture guards
tests/contract/test_tryton_adapter.py    # live server + hesabyar_tryton_test DB
```

## The naiad decision: direct httpx calls, no naiad dependency

The official Tryton 8.2 REST-client docs
(`https://docs.tryton.org/8.2/client-rest/index.html`) demonstrate the `naiad`
client (`from naiad import Client`). We investigated it on 2026-11-16 and
decided **not** to use it:

- PyPI (`https://pypi.org/pypi/naiad/json`) publishes only **`naiad 1.0.dev`** --
  no stable release exists, so pinning it would pin a dev build.
- Its home page is a personal GitHub repository
  (`github.com/bartfeenstra/naiad`), not a tryton.org project.
- The REST contract it wraps is small and fully specified by the installed
  server source (`trytond/protocols/rest.py`, Tryton 8.2.3) and verified live
  against this install (see below). The transport is ~200 lines we own and
  contract-test, instead of an unstable third-party dependency.
- `httpx` is already a project dependency.

The adapter therefore calls the documented REST endpoints directly with
httpx. It uses **only** the supported REST contract: no SQL against Tryton's
database, no JSON-RPC for adapter operations (the REST surface covers
everything the spike needs: search, create with nested lines, read, delete).
A unit test (`test_tryton_adapter_has_no_sql_or_rpc`) greps the adapter
package for SQL/RPC usage on every run.

## REST contract (verified live against Tryton 8.2.3)

Base: `http://localhost:8000/hesabyar_tryton_test/rest`.

| Operation | Request |
|---|---|
| Search | `GET /rest/model/<name>?d=<b64url domain>&f=<field>&s=<limit>&p=<offset>&o=<b64url order>` |
| Read | `GET /rest/model/<name>/<id>?f=<field>` |
| Create | `POST /rest/model/<name>` (JSON body, one record) |
| Update | `PUT /rest/model/<name>/<id>` |
| Delete | `DELETE /rest/model/<name>/<id>` (204) |
| Action | `POST /rest/model/<name>/[<id>/]<action>` (only `__rpc__` methods) |
| Report | `GET /rest/report/<name>/<id>` |

Auth: `Authorization: Bearer <user-application key>` (a `res.user.application`
record with `application='rest'`, state `validated`). A session token does
**not** work on REST. Dev key: `hesabyar-dev-rest-key` (see
`docs/dev-setup.md` §4).

Transaction context (notably the current company) travels in the
`X-Tryton-Context` header as base64url JSON.

### Wire format

Decimal/date/datetime values use the shared JSON-RPC encoding
(`trytond/protocols/jsonrpc.py`):

- Decimal: `{"__class__": "Decimal", "decimal": "1000000"}`
- date: `{"__class__": "date", "year": 2026, "month": 11, "day": 16}`

`contracts.encode_wire` / `decode_wire` handle both directions, including
dates nested inside search domains.

### One2Many values need command syntax

Nested lines in a create payload must use Tryton's command form:

```json
{"lines": [["create", [{"account": 170, "debit": {"__class__": "Decimal", "decimal": "1000"}, "credit": {"__class__": "Decimal", "decimal": "0"}}]]]}
```

A plain list of dicts is rejected with a 500 (`KeyError: 0` in
`one2many.set`).

### Quirks found while building this (all verified live)

- **Rule gating:** every accounting model carries a global `ir.rule`
  `[('company', 'in', Eval('companies', []))]`, where `companies` comes from
  `res.user.get_companies()`. That returns `[]` unless the user's
  `company_filter` is `'all'` (or `'one'` with a current company set). The
  admin user needs `company_filter='all'` plus the test company in
  `res.user.companies`, or account/period/move searches return empty lists.
- **Stale server cache:** `get_companies()` is cached in the running server
  process and is only invalidated by writes that go *through that process*.
  Changing `res.user.companies` from `trytond-console` (a separate process)
  does not clear it; a write through the server (or a restart) does.
- **`company.company` has no readable `name` via REST:** requesting `f=name`
  500s (`KeyError: 'name'`, a Function-field quirk). The adapter reads
  `rec_name` instead.
- **No proxy:** the client sets `trust_env=False`, so environment proxy
  variables can neither intercept the Bearer key nor break engine calls.
- **Journal scoping:** `account.journal` has no company field; journals are
  global. The test journal uses a distinct name to avoid collisions.
- **Validation failures are JSON, not HTML:** a `UserError` raised inside a
  REST handler comes back as HTTP 400 with a JSON body
  `{"status": 400, "message": "...", "description": "..."}`. The error
  sanitizer extracts the `message` instead of surfacing the raw JSON.
- **Period reopen does not reopen journal-periods:** `Period.close` closes
  the period's `account.journal.period` records, but `Period.reopen` leaves
  them closed, and a closed journal-period blocks all later moves in that
  period ("You cannot add/modify/delete lines on closed journal-period").
  Anything that closes and reopens a period must reopen its
  journal-periods too (`account.journal.period` `reopen` action, also
  RPC-exposed). The contract test for closed-period reversal does this in
  its cleanup.
- **Some post-time rejections are unreachable via REST:** the engine
  refuses to create lines on a closed account, refuses to close an account
  that has move lines, refuses to close a period with unposted moves, and
  refuses to create moves in closed periods -- so a draft on a closed
  account or in a closed period cannot be constructed through the API.
  The adapter still checks both at post time (defense-in-depth, for
  states reachable only outside REST); those paths are covered by unit
  tests with a mocked transport, not by contract tests.
- **Buttons are RPC methods:** `ModelView.__setup__` adds every button to
  the model's `__rpc__`, so workflow transitions ARE callable through the
  REST action endpoint -- verified live on 2026-11-17: the
  `validate_invoice` and `cancel` actions on `account.invoice` both
  worked via REST. (Only `post` is in `account.invoice.__rpc__`
  explicitly; the transitions arrive via the buttons.) `validate_invoice`
  is deliberately NOT exposed by this story: `validate_invoice_draft`
  is the read-only HesabYar check, and finalizing an invoice in the
  engine is a later story's port method.
- **Validated invoices must be cancelled before deletion:** deleting a
  validated invoice fails ("To delete invoice ... you must cancel it").
  Draft invoices, payments and statements delete cleanly via REST. The
  contract tests only ever create drafts, so their cleanup deletes
  directly.
- **`party.address` has no `name` field:** creating an address with a
  `name` key 500s (`KeyError: 'name'`). A minimal address is
  `{"party": <id>}` plus optional street/city.
- **Invoice line accounts have a direction domain** (enforced server-side
  by `InvoiceLine._account_domain`): customer (`out`) lines need accounts
  whose type has `revenue=True`; supplier (`in`) lines need `expense=True`
  or `debt=True`. The adapter pre-validates this so a wrong-direction
  account fails with `LEDGER_INVALID_INVOICE`, not the engine's domain
  error. A line account may NOT be the invoice's own receivable/payable
  account (engine line domain).
- **Invoice line party comes from the invoice:** unlike journal lines, a
  `party_required` line account is fine on invoice lines -- the engine
  fills the move-line party from the invoice when it posts
  (`InvoiceLine.get_move_lines`). The adapter does not reject such
  accounts.
- **Payment numbers need a sequence:** `account.payment` numbers are
  assigned from the `account.configuration` multivalue
  `payment_sequence` for the company. The test DB has one configured
  ("Default Account Payment"); without it, payment creation fails on the
  required `number` field.
- **Statement journals need a `statement`-type account journal:**
  `account.statement.journal.journal` has domain `type = 'statement'`
  (the type is added to `account.journal` by `account_statement`).
  Module activation already created a global "Statement" journal of that
  type, plus "Revenue" (revenue) and "Expense" (expense) journals, which
  the invoice tests use.

## Invoice / payment / statement mapping (US-204)

| HesabYar | Tryton model | States |
|---|---|---|
| `InvoiceView` / `InvoiceRef` | `account.invoice` (+ `account.invoice.line`) | `draft`, `validated`, `posted`, `paid`, `cancelled` |
| `PaymentView` / `PaymentRef` | `account.payment` | `draft`, `submitted`, `approved`, `processing`, `succeeded`, `failed` |
| `BankStatementView` / `BankStatementRef` | `account.statement` (+ `account.statement.line`) | `draft`, `validated`, `posted`, `cancelled` |

### Invoices

- A HesabYar invoice draft is an `account.invoice` in state `draft`.
  Required at creation: company, type (`out` = customer, `in` =
  supplier), party, invoice address, currency, and the receivable
  (`out`) / payable (`in`) account. The journal is required only from
  validation onwards, but the adapter always resolves one at creation
  (explicit, else the engine's single revenue/expense journal), so every
  draft it creates is postable. Lines use the One2Many command syntax;
  line company/currency default from the context company (verified live).
- The engine computes `untaxed_amount` / `tax_amount` / `total_amount`
  as Function fields; the adapter reads them back and
  `validate_invoice_draft` cross-checks them against the lines
  (untaxed == sum of line amounts, total == untaxed + tax).
- The invoice number is assigned by the engine later (on validation for
  supplier invoices, on posting for customer invoices), so it is `None`
  on drafts. `invoice_date` is required to validate supplier invoices
  (the `validate_invoice` button's pre-validation); for customer
  invoices the engine sets it at posting when missing.
- **No taxes in this story:** lines are created without taxes, so the
  engine computes a zero tax amount. Tax / VAT handling is a later story
  (Iranian tax policy, Phase 4).
- **No credit notes in this story:** the command requires a total
  greater than zero. Credit notes are invoices with negative totals in
  Tryton and need their own story.
- Quantities are `Decimal` in the domain (never float: quantities
  multiply into money). Tryton stores them in Float fields; the mapping
  converts through `str` so the sent value survives exactly, and sends
  integral quantities as ints. Line amounts (quantity x unit price) must
  be integral IRR at command construction -- fractional Rial amounts are
  rejected, never rounded.

Pre-validation before delegating a draft to the engine (mirroring
`create_journal_draft`):

1. Domain command construction enforces the invoice invariants (>= 1
   line, positive quantities, non-negative unit prices, integral line
   amounts, total > 0).
2. The adapter verifies the company currency is IRR, the party exists,
   and the party has an invoice address (explicit, else the party's
   first address by id).
3. The adapter resolves the receivable/payable account (explicit, else
   the company's single open account of the right type) and the journal
   (explicit, else the single revenue/expense journal).
4. The adapter verifies every line account exists for the company, is
   open and active, is not the invoice's own account, and matches the
   direction domain (revenue for `out`, expense/debt for `in`).
5. No period check happens at creation: a draft invoice carries no
   period. `validate_invoice_draft` checks the period instead (below).

`validate_invoice_draft` (read-only, returns `ValidationSummary`, never
mutates, never calls the engine's `validate_invoice` transition):

- state must be `draft`; at least one priced line; every line has a
  positive quantity, a non-negative unit price, and an amount equal to
  quantity x unit price; line accounts exist for the company and are
  open, active, and of the right direction;
- engine totals cross-checked (untaxed == line sum, total == untaxed +
  tax) and total > 0;
- invoice currency == company currency == IRR; party exists; invoice
  address belongs to the party; invoice account is open, active, and of
  the right type (receivable/payable); journal present and of the right
  type (revenue/expense);
- the accounting date (`accounting_date`, else `invoice_date`, else
  today -- the date posting derives the period from via
  `Period.find`) falls in an open fiscal period, or the issue is
  `LEDGER_PERIOD_CLOSED`.

### Payments (read-only)

- `get_payment` / `list_payments` read `account.payment`: number (from
  the company payment sequence), kind (`receivable` / `payable`), party
  (+ resolved name), date, amount, state, reference, payment-journal
  name, currency code. The payment's linked move line / origin invoice
  is not exposed yet; that mapping lands when invoice-payment linking is
  contract-verified in a later story.
- No create/approve/process methods exist on the adapter (a unit test
  asserts their absence): creating and processing payments is a later
  story.

> NOTE: the two bullets above are stale — US-502 added the payment
> workflow (`create_payment` / `submit_payment` / `process_payment` /
> `succeed_payment`, invoice-payment linking via `line`) and US-522
> added the batch + reconciliation methods below. The section is kept
> as the read-mapping record.

### Settlement posting (US-522)

- `JournalDraftLine` gains optional `party` + `maturity_date`
  (plain REST field writes). `_resolve_accounts` now mirrors the
  engine's `Line.check_account`: `bool(party) ==
  account.party_required` per line — a line on a party-required
  account MUST carry a party, a line on any other account MUST NOT.
- `CreateInvoiceDraft` gains `payment_term` (splits the posted
  invoice into one receivable line per leg, each with its maturity
  date) + `alternative_payees` (at most one before posting — the
  engine enforces this; moves the posted receivable lines' party
  from the customer to the payee, i.e. the gateway). Both are plain
  REST field writes, verified by the adapter before delegating.
- `PaymentView` gains `clearing_move` (the clearing move
  `succeed` creates, DRAFT until posted); `InvoiceView` gains
  `move`.
- `process_payments(payments)` drives several submitted/approved
  payments to `processing` in ONE call to the `hesabyar_setup`
  module's `process_rest_batch` action (no-id REST route, ids in
  the body), so a settlement batch's payments land in ONE
  `account.payment.group`; each payment is read back afterwards
  (a vanished payment is a stable not-found error). Empty input is
  a no-op (no call).
- `get_payment_journal(journal)` reads a payment journal with its
  clearing configuration (`clearing_account` / `clearing_journal`,
  `is_clearing_configured`); None when missing. The posting
  service fail-closes on an unconfigured journal before processing.
- `list_move_lines(company, *, move, account, party,
  maturity_date, unreconciled_only)` pages `account.move.line`
  (company-scoped through the move); at least one of `move` /
  `account` is required. Returns `MoveLineView` (ref, move,
  account, debit, credit, state, party, maturity_date,
  reconciliation, description, `balance`).
- `reconcile_move_lines(command)` reconciles a set of lines via
  the `hesabyar_setup` module's `reconcile_rest` action (no-id
  REST route, ids in the body). Pre-validation mirrors
  `Reconciliation.check_lines`: every line exists for the company,
  all lines share one account and one party, debits == credits,
  every line is `valid` and unreconciled. The engine's own
  `ReconciliationError` is the backstop for a race between the
  read and the call. Returns `ReconciliationView` (ref, number,
  lines).
- New Tryton module surface (`hesabyar_setup`, US-522): the
  `reconcile_rest` wrapper on `account.move.line` and the
  `process_rest_batch` action on `account.payment`, plus chart
  accounts 1.1.6 Gateway Clearing, 1.2.8 Gateway Receivables,
  5.2.4 Gateway Fees (see `docs/tryton-spikes-520.md` §5–§6).

### Bank statements (read + write, US-514)

- `get_bank_statement` / `list_bank_statements` read
  `account.statement` with its `account.statement.line` lines. Which
  balance fields are present depends on the statement journal's
  validation type: `balance` journals carry start/end balances,
  `amount` journals carry a total amount, `number_of_lines` journals
  carry a line count. The engine's `balance` Function is 0 on
  non-balance journals; the view maps that to `None` so "not
  applicable" stays distinct from zero.
- Statement line amounts are signed (positive = money in, negative =
  money out). A line's `related_to` reference (`"account.invoice,<id>"`)
  maps to an `InvoiceRef` when present.
- `create_bank_statement` writes a draft statement (one REST call with
  nested `[["create", lines]]`). It pre-validates the journal, the line
  accounts (open + postable), parties and invoice links (invoice must
  exist, belong to the same company, be posted, and its account must
  match the line account) — the engine would reject them anyway, but
  the pre-checks produce HesabYar error codes instead of raw 400s.
- `update_bank_statement_line` writes account/party/`related_to` on a
  line of a draft statement (the reconcile write path). `related_to`
  accepts invoices only (`Line._get_relations`); clearing it writes
  `None`.
- `list_statement_journals` reads `account.statement.journal` with the
  journal's bank account number (Shaba) resolved via `bank.account.number`
  (type `iban`).
- Validate/post/delete/reconcile are still NOT on the adapter: the
  Reconcile Statement wizard is UI-only (not REST-callable), and the
  accountant validates in the engine. See `docs/bank-import.md` for the
  no-validate boundary.

## Journal lifecycle mapping

- A HesabYar journal draft is a Tryton `account.move` in state `draft`.
- The engine marks a draft's lines `valid` when the move balances and
  `draft` otherwise (`Move.validate_move`, triggered automatically on line
  create/write). `validate_journal_draft` reads that verdict as an
  independent cross-check on HesabYar's own balance arithmetic.
- **Posting** is the `post` model action (`Move.post` is in
  `account.move.__rpc__`, `instantiate=0`): `POST
  /rest/model/account.move/<id>/post`. One REST call is one engine
  transaction, so posting is atomic: the engine enforces non-empty +
  balanced, assigns the move number from the period/fiscal-year sequence,
  stamps `post_date`, and sets state `posted`. `post_journal` re-validates
  the draft first (same checks as `validate_journal_draft`, raised as
  stable ledger errors) and verifies the engine's read-back says `posted`.
  Re-posting raises `JournalAlreadyPostedError` (`LEDGER_POSTED_IMMUTABLE`).
- **A posted move is immutable**: `Move.check_modification` raises on
  delete and on writes outside `lines`, and `Line.on_modification`
  raises on any line change of a posted move. The adapter exposes an
  update method for *drafts* only: updating a posted journal raises
  `JournalAlreadyPostedError` (`LEDGER_POSTED_IMMUTABLE`) before
  anything reaches the engine. The contract tests prove both levels
  (raw REST delete/write on a posted move fail; the adapter refuses
  posted updates).
- **Listing drafts** is a company-scoped `account.move` search
  (`state = 'draft'`), newest first, with lines and periods batched
  into the views (one line search + one period search per list, not
  one per move).
- **Updating a draft** is one REST `PUT` on the move: date,
  description and period as plain fields, lines rewritten wholesale
  with the One2Many command syntax
  (`[["delete", [old ids]], ["create", [new lines]]]`) — deletes run
  before creates (`One2Many.set`), the move's own columns are written
  before the line commands (`ModelSQL.write`), and the whole PUT is
  one engine transaction, so the update is atomic (a failed write
  leaves the original lines intact — verified live). The engine
  re-validates balance automatically (`Move.validate_move` on line
  create/delete) and the adapter re-runs `validate_journal_draft`
  before returning the read-back view. A date change must carry its
  period in the same write: the engine enforces the date-in-period
  domain on write, so a date-only move across periods is rejected
  (verified live); `on_change_date` is client-side only and never
  runs for a REST write, so the adapter resolves the open period for
  the new date itself. Reconciled lines refuse a rewrite (the engine
  raises on deleting them), so the adapter pre-checks for them and
  fails with a stable error instead.
- **Reversal** replicates the `account.move.cancel` wizard, which REST
  cannot drive: there is no wizard endpoint, and `Move.cancel` is not in
  `__rpc__`. `reverse_journal` creates a new move with debit/credit
  swapped (wizard `reversal=True`), `origin` set to the original move,
  each line's `origin` set to the original line (plus party,
  maturity date, and negated second-currency amount when present), then
  posts it. If the original's period has since closed, the reversal is
  dated today in the currently open period, as the wizard does. Limits,
  all documented in the adapter docstring: reversal is two REST calls,
  not one transaction (if the post fails, the reversal draft survives
  and its id is attached to the error's details for recovery), and
  reconciliation of reconcile-account lines is not replicated
  (`account.move.line.reconcile` is not RPC-exposed).
- **Fiscal years / periods** are plain reads: `account.fiscalyear`
  (name, dates, state, company) and `account.period` (name, dates, state,
  type, fiscal year), newest year first / periods by start date.

Pre-validation before delegating a draft to the engine (spec §8.2):

1. Domain command construction enforces §8.3 invariants (>= 2 lines,
   one-sided lines, non-negative, total > 0, balanced).
2. The adapter verifies every account exists for the company, is open and
   active, and needs no party (tafsili/party lines are a later story).
3. The adapter verifies the company currency is IRR (refuses to draft
   integer-IRR amounts into a foreign-currency company).
4. The adapter resolves an open fiscal period covering the journal date.
5. The journal is the command's explicit journal, or the engine's single
   general journal when exactly one exists.
6. Posting re-runs checks 1-4 (via `validate_journal_draft`) before the
   `post` action; failures surface as the matching stable ledger error
   (`LEDGER_UNBALANCED`, `LEDGER_ACCOUNT_NOT_FOUND`,
   `LEDGER_PERIOD_CLOSED`, `LEDGER_INVALID_JOURNAL`).
7. Updating re-runs the same checks for the changed fields before the
   `PUT` (new accounts via check 2, a new date via check 4 with the
   resolved period written alongside it, a lines-only update against
   the move's current open period), refuses posted journals
   (`LEDGER_POSTED_IMMUTABLE`) and reconciled lines, then re-validates
   the draft after the write (check 6's shape) before returning the
   read-back view.

## Money: integer IRR only

`mapping.money_from_tryton` converts a Tryton amount to `MoneyIRR` only
after proving it is finite and integral. A fractional Rial value is
**rejected** with `InvalidMoneyError` -- never silently rounded, never
converted to Toman (spec §8.3). Floats are rejected outright.

## Error taxonomy

| Code | Meaning | Source |
|---|---|---|
| `LEDGER_UNBALANCED` | debit total != credit total | domain / adapter validation |
| `LEDGER_INVALID_JOURNAL` | structural draft problem (< 2 lines, zero total, two-sided line, closed account, ...) | domain / adapter |
| `LEDGER_ACCOUNT_NOT_FOUND` | account missing, wrong company, closed/inactive | adapter |
| `LEDGER_PERIOD_CLOSED` | no open period covers the date, or period closed | adapter |
| `LEDGER_JOURNAL_NOT_FOUND` | draft / posted-journal id does not exist | adapter |
| `LEDGER_POSTED_IMMUTABLE` | draft is already posted (validation issue) / posting or updating an already-posted journal | adapter validation / post / update |
| `LEDGER_INVALID_INVOICE` | structural invoice problem (bad type, no lines, zero total, wrong account/journal type, missing journal, ...) | domain / adapter |
| `LEDGER_INVOICE_NOT_FOUND` | invoice id does not exist | adapter |
| `LEDGER_INVALID_PAYMENT` | bad payment filter (kind/state) | adapter |
| `LEDGER_PAYMENT_NOT_FOUND` | payment id does not exist | adapter |
| `LEDGER_INVALID_STATEMENT` | bad statement filter (state) | adapter |
| `LEDGER_STATEMENT_NOT_FOUND` | statement id does not exist | adapter |
| `LEDGER_PARTY_NOT_FOUND` | party id does not exist | adapter |
| `ENGINE_UNAVAILABLE` | connect/timeout/502/503/504 (retryable) | transport |
| `ENGINE_AUTHENTICATION_FAILED` | HTTP 401 | transport |
| `ENGINE_FORBIDDEN` | HTTP 403 | transport |
| `ENGINE_NOT_FOUND` | HTTP 404 | transport |
| `ENGINE_VALIDATION_FAILED` | HTTP 400/422 | transport |
| `ENGINE_ERROR` | other HTTP 5xx / unexpected responses | transport |

`errors._sanitize_message` strips HTML, tracebacks, and file paths from
engine messages before they reach a caller; no Tryton stack trace ever
leaks through the adapter.

## Test company and chart (provisioning)

The contract tests use a dedicated company, **"HesabYar Adapter Test"**
(company id 2 in the current test DB), so they never interfere with the
sibling US-004 story's own test company ("HesabYar IRR Test", id 3).

Provisioning script (outside the repo, next to the Tryton install):

```text
~/workspace/projects/hesabyar-reference/setup_adapter_test_company.py
```

Run it with:

```bash
cd ~/workspace/projects/hesabyar-reference
./tryton-env/bin/trytond-console -c trytond.conf -d hesabyar_tryton_test \
  --lock-table account_fiscalyear account_period account_journal account_configuration \
  -f setup_adapter_test_company.py
```

It is idempotent (safe to re-run; existing records are reused) and creates:

1. Currency **IRR** -- only if missing. It never modifies an existing IRR
   record: the IRR digits configuration belongs to US-004 (which has set
   digits=0 in the current test DB).
2. Party + company "HesabYar Adapter Test" (currency IRR), and appends the
   company to the admin user's allowed companies (merge-safe: it never
   replaces the list, so the sibling's grant is preserved).
3. The **universal chart of accounts** from the `trytond-account` templates
   (135 accounts), by running the exact code path of the
   `account.create_chart` wizard (`transition_create_account` +
   `transition_create_properties` in `trytond/modules/account/account.py`):
   account types, accounts, taxes, tax codes, tax rules, and default
   receivable/payable accounts.
4. A company move sequence (`ir.sequence.strict`, account-move type),
   fiscal year **FY 2026** (2026-01-01..2026-12-31, open) with 12 open
   monthly periods, and a general journal
   "HesabYar Adapter Test General Journal".

Notes:

- `trytond-console` rolls its transaction back on exit, so the script
  commits explicitly (`transaction.commit()`).
- `FiscalYear.create` calls `lock()`, which raises `_TransactionLockError`
  unless the table was pre-declared -- hence the `--lock-table` flags.
- The chart wizard path runs through Tryton's own ORM/template methods, not
  SQL: no Tryton table is ever touched directly.
- Module activation also created global journals the invoice/statement
  tests rely on: "Revenue" (revenue), "Expense" (expense), "Cash"
  (cash), "Statement" (statement). Journals are global, not
  company-scoped; the tests look them up by type, never by id.

US-204 fixtures (created by the contract tests themselves through the
REST API, deleted afterwards -- nothing is provisioned by the script):

- a customer party "HesabYar Contract Customer" + one address, and a
  second party with no address (for the address-required test);
- a manual payment journal "HesabYar Contract Payments"
  (`account.payment.journal`, process method `manual`);
- a balance-validated statement journal "HesabYar Contract Bank"
  (`account.statement.journal`, validation `balance`, cash account
  1.1.1, using the global "Statement" account journal).

The tests use these chart accounts (universal chart codes): receivable
`1.2.1`, payable `2.1.1`, revenue `4.1.1` / `4.1.2` (customer lines),
expense `5.1.1` / `5.1.3` (supplier lines). The payment number sequence
("Default Account Payment") is already configured in
`account.configuration` for the test company.

## Running the tests

```bash
export HESABYAR_DATABASE_URL="postgresql+psycopg://hesabyar:hesabyar_pass@127.0.0.1:5432/hesabyar_test"

uv run pytest tests/unit/test_tryton_adapter.py      # no server needed
uv run pytest tests/contract/test_tryton_adapter.py  # needs trytond running
uv run pytest                                        # full suite
```

Contract-test configuration (env vars, dev defaults in the test module):
`TRYTON_BASE_URL`, `TRYTON_DATABASE`, `TRYTON_API_KEY`,
`TRYTON_TEST_COMPANY`. With the server down, all contract tests skip
cleanly and the suite stays green.

## Design decisions recorded

- **Synchronous port.** The spec §2.2 sketch is async, but the repository
  has no async infrastructure yet and the Tryton calls are plain HTTP. The
  port can be made async in a later story when the MCP/application layer
  needs it; the adapter is the only implementation to change.
- **`list_companies` returns `CompanyView`** (ref + name + currency code)
  rather than bare `CompanyRef`s: a list of bare ids is unusable without a
  second call per company, and the currency code is needed to enforce the
  IRR-only rule.
- **`post_journal` / `reverse_journal` take command objects**
  (`PostJournal` / `ReverseJournal`), following the spec §2.2 sketch and
  the `CreateJournalDraft` pattern; `reverse_journal` returns the new
  posted reversal's ref, and `get_journal` reads a posted move back with
  its number, post date, and origin link.
- **No `get_report` yet.** It joins the port when its Tryton operations
  are contract-verified in a later story.
- **Party/tafsili on lines (US-522).** `JournalDraftLine` carries
  optional `party` + `maturity_date`; the adapter enforces
  `bool(party) == account.party_required` per line, mirroring the
  engine's `Line.check_account`. (Supersedes the earlier "no
  party/tafsili on lines yet" bullet.)
- **Payments have a workflow on the port (US-502/US-522).** Create
  + submit + process + succeed (+ batch processing and
  reconciliation) are port methods now; statements are still
  read-only for validate/post. (Supersedes the earlier "payments
  and statements are read-only" bullet.)
- **`validate_invoice_draft` is read-only.** Like `validate_journal_draft`,
  it returns a `ValidationSummary` and never mutates the invoice. The
  engine's `validate_invoice` transition (draft -> validated) is
  reachable via REST (buttons are RPC methods) but is deliberately not
  exposed here: finalizing an invoice is a later story's port method,
  mirroring how `post_journal` came in US-201 after
  `validate_journal_draft` in US-003.
- **One `InvoiceRef` for all states.** Journals split draft/posted refs
  because posting and reversal have different semantics per state;
  invoices have no port-level transition yet, so a single ref plus the
  view's `state` is enough. A finalize method may introduce a
  `ValidatedInvoiceRef` later if it earns the split.
- **No taxes, no credit notes yet.** Invoice lines carry no taxes (tax
  amount is always zero for now) and totals must be positive. Both are
  later stories (Iranian VAT/Moadian in Phase 4; credit notes with the
  invoice workflow).
