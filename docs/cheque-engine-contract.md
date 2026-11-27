# Cheque engine contract (`account_check_ir`, US-530/US-531/US-532)

The contract the HesabYar layer expects from the Tryton module
`account_check_ir`. The HesabYar side reaches the module ONLY
through `AccountingEnginePort` (`create_engine_cheque` /
`transition_engine_cheque` / `get_engine_cheque` /
`list_engine_cheques` in `TrytonAccountingAdapter`); it never
touches the module's tables directly.

> **Status (US-532, verified live on lane 1, 2026-11-19):** the
> US-530 scaffold is installed and activated on lane 1
> (trytond :8010) and lane 2 (trytond :8011) — model, validations,
> chart accounts, custody log, endorsement history.
> `transition_rest` covers the received lifecycle (US-531, سند ۱–۶)
> AND the issued + collateral lifecycle (US-532, سند ۷–۱۳): the
> transition tests in `tests/contract/test_cheques_us531.py` run
> against it on lane 2, and `tests/contract/test_cheques_us532.py`
> on lane 1 — including move-LINE assertions on the account codes
> (see the سند ۵/۱۰ correction below).

## Model

`account.check` — one row per cheque leaf. Verified live via
`ir.model.field` on lane 2 (the field list below is the module
as installed, not the prd's paraphrase).

| Field | Type | Notes |
|---|---|---|
| `id` | int | Engine cheque id (`ChequeEngineRef.engine_id`) |
| `company` | Many2One `company.company` | The Tryton company; every call passes `context={"company": <id>}` |
| `kind` | Selection | `received` \| `issued` |
| `purpose` | Selection | `payment` \| `collateral` — the engine flips it to `payment` at `call_in` / `unexpected_call_in` (US-532) |
| `sayad_id` | Char, 16 digits | Unique among live states (exclusion constraint exempts `expired_unreceived` / `rejected_mismatch`) |
| `sayad_status` | Selection | `unregistered` \| `registered` \| `confirmed` \| `transferred` (default `unregistered`) |
| `state` | Selection | `registered` \| `held` \| `in_clearing` \| `cleared` \| `bounced` \| `returned` \| `endorsed` \| `issued` \| `voided` \| `expired_unreceived` \| `rejected_mismatch` \| `called_in` (default `registered`) |
| `storage_location` | Selection | `cashbox` \| `vault` \| `bank` \| `with_endorsee` \| `returned_to_issuer` (nullable) |
| `amount` | Numeric (Rial, digits 0) | Toman→Rial ×10 happens in HesabYar; the engine always sees Rial |
| `issue_date`, `due_date` | Date | Gregorian on the wire (`{"__class__": "date", ...}`) |
| `number` | Char | Leaf serial number (optional) — NOT the Sayyad id |
| `party` | Many2One `party.party` | Issuer for a received cheque, BENEFICIARY (payee) for an issued cheque (optional at intake; REQUIRED for journals — the chart accounts are `party_required`) |
| `bank` | Many2One `bank` | Issuing bank (optional; no code field — see "Open gaps") |
| `checkbook` | Many2One `account.checkbook` | Issued cheques only |
| `branch` | Char | Issuing bank branch (optional) |
| `issuer_account_number` | Char | (optional) |
| `invoice` | Many2One `account.invoice` | Linked invoice (optional) |
| `sale` | Many2One `sale.sale` | Linked sale/order (optional) |
| `installment_leg` | Char | Linked installment leg id, as a string (optional; ۵ج consumes it) |
| `applied_amount_irr` | Numeric | Amount allocated to the linked leg, when it differs from the cheque amount |
| `endorsee` | Many2One `party.party` | Endorsee (set at endorsement) |
| `endorsee_type` | Selection | `supplier` \| `other` |
| `destination_account` | Many2One `account.account` | Bank/cash account chosen at `deposit` (received) or at `issue` (issued), used by `clear` for سند ۳ / سند ۱۱; for an issued cheque with no account recorded at issue, `clear` falls back to the checkbook's bank account |
| `secured_obligation` | Char | Collateral obligation reference |
| `attachment` | Many2One `ir.attachment` | Primary scan/photo of the leaf |
| `source` | Selection | `site` \| `manual` (default `manual`) |
| `endorsement_history` | One2Many `account.check.endorsement` | `check`, `date`, `sayad_transfer_receipt`, `reason` |
| `moves` | One2Many `account.move` via `origin`, readonly | The journal move of every transition — a list of move **ids** on read, with NO transition attribution (see below) |

`sayad_id` uniqueness: a DB exclusion constraint, live states
only — an `expired_unreceived` or `rejected_mismatch` leaf frees
its `sayad_id` for re-registration as a NEW record (the old
record never revives).

### Fields with NO engine home (registry-only)

The 24-field site payload carries fields the engine model does
not have. They live in the HesabYar registry only and NEVER
travel to the engine: `issuer_name`, `issuer_national_id`,
`payee_name`, `payee_national_id`, `order_id`, `customer_ref`,
`reason`, `scan_ref`. (`sayad_registered` maps to `sayad_status`;
`issuer_bank` "056 سامان" has no engine field — see "Open gaps".)

## Intake — direct REST `create` (verified live)

`POST /<db>/rest/model/account.check` with
`context={"company": <id>}` and the fields above. There is NO
`create_rest` action — the model validates + defaults itself
(16-digit sayad_id, amount > 0, due ≥ issue, leaf inside its
checkbook range when a checkbook is set). The adapter sends:

- `company`, `kind`, `purpose`, `sayad_id`, `amount`,
  `issue_date`, `due_date`
- `sayad_status`: `registered` when the payload says the leaf is
  Sayyad-registered, else `unregistered`
- `source`: `manual` | `site`
- `party` — only when the mapping center resolved the issuer
- `number` ← leaf_number, `branch`, `issuer_account_number`,
  `invoice` (engine id), `installment_leg` (string id) — only
  when present

The create returns the record; the adapter reads it back with
`get_engine_cheque` (fields `CHEQUE_FIELDS`). A duplicate live
`sayad_id` fails in the engine (exclusion constraint) and
surfaces as `ChequeEngineError`.

## Transitions — `transition_rest` (implemented, US-531 + US-532)

One `__rpc__` method on `account.check`, following the
`hesabyar_setup` wrapper pattern (`cls.__rpc__.update({...})`,
access checks disabled for the duration of the call — the same
scope as `process_rest` / `reconcile_rest` /
`assign_and_do_rest`, because the stock engine calls read records
the REST service account cannot read with checks on; HesabYar
enforces its own authorisation before calling):

`POST /<db>/rest/model/account.check/<id>/transition_rest`,
`context={"company": <id>}`, body:

```json
{
  "transition": "confirm_receipt | deposit | clear | bounce | endorse | return | expire | reject_mismatch | issue | void | call_in | unexpected_call_in | endorsed_bounce",
  "auto_post": false,
  "defer_journal": false,
  "destination_account": 42,
  "bounce_destination": "receivables | bounced_receivables",
  "bounce_reason": "...",
  "bounce_date": {"__class__": "date", "year": 2027, "month": 1, "day": 10},
  "certificate_reference": "...",
  "endorsee_name": "...",
  "endorsee_type": "supplier | other",
  "sayad_transfer_reference": "...",
  "storage_location": "vault",
  "statement_reference": "...",
  "mismatch_detail": "...",
  "accountant_approval": "...",
  "void_reason": "..."
}
```

Only the parameters the transition needs are sent (all others
omitted). The method performs the state change AND its journal
(§2.4 سند ۱–۶) in **one** Tryton transaction and returns:

```json
{"id": 1001, "state": "held", "move_id": 5000}
{"id": 1001, "state": "called_in", "move_id": null, "move_ids": {"call_in_reversal": 6001, "call_in": 6002}}
```

- `move_id` is the journal this transition created, or `null`
  when the transition has no journal (expiry, reject-mismatch,
  bounced→returned, `endorsed_bounce`) or when it is deferred
  (`defer_journal`). `move_ids` is the full map for transitions
  that post more than one move (`call_in`: سند ۹الف +
  سند ۹ب in one transaction). The return MUST carry it: a plain
  read cannot attribute moves to transitions (`moves` is an
  unattributed list of move ids), so the adapter merges the
  returned move(s) into the view and the HesabYar registry
  accumulates the transition→move map across transitions.
- `auto_post`: the F3/F4 policy-gate verdict. `false` (default)
  leaves the created move a **draft**; `true` posts it. The engine
  never decides this itself.
- `defer_journal`: the accountant-gated path (endorsement to a
  non-supplier, §9.1 #12): the state moves, NO journal is created,
  and the HesabYar side files a review item for the journal design.
- `bounce` reverses in the **current** period only (the engine
  enforces the open period; a closed period fails the transition).
- The journal is the MISC general journal (journals are global in
  Tryton — `account.journal` has no company field — so the lookup
  is by code alone) and the move is dated today, in the open
  period covering today (`Period.find`).
- `bounced → in_clearing` (re-presentment) is DISABLED by design
  (§9.1 #5): the method must refuse it, and there is no endpoint
  for it.
- `return` from `held` creates سند ۶; `return` from `bounced`
  creates NO journal (the claim was re-established by سند ۴).
- `expire` / `reject_mismatch` create NO journal.
- The deposit destination account is chosen at `deposit` and the
  method stores it on the check (a `destination_account` field to
  be added with the method) for `clear` to use at سند ۳.
- The journal lines use the cheque's `party` (the issuer for a
  received cheque, the beneficiary for an issued cheque); when
  it is unset the transition fails closed — the chart accounts
  are `party_required`. (Party resolution: see "Open gaps".)
- **Issued + collateral scope (US-532).** The engine enforces
  the same scope the HesabYar state machine enforces
  (`_check_scope`, the mirror of `assert_transition_scope`):
  `issue` / `void` / `unexpected_call_in` are issued-only,
  `deposit` / `confirm_receipt` are received-only, `call_in`
  is received-collateral-only, `clear` from `called_in` is
  issued-only. A transition outside its scope fails closed.
- **Issued lifecycle (US-532, سند ۱۰–۱۳).** `issue` (registered
  → issued): commercial Dr Trade Payables / Cr Notes Payable;
  collateral issues the memorandum in the issued direction
  (Dr اسناد تضمینی نزد دیگران / Cr طرف حساب اسناد تضمینی نزد
  دیگران). `clear` from `issued` (سند ۱۱) credits the bank
  account recorded at issue, else the checkbook's bank account.
  `bounce` from `issued` (سند ۱۲) needs reason + date only — no
  bank certificate. `void` (سند ۱۳) needs `void_reason`; the
  leaf is counted voided in its checkbook (report 8).
- **Collateral (US-532, سند ۷–۹).** `confirm_receipt` on a
  collateral cheque posts the memorandum ONLY (Dr اسناد تضمینی
  دریافتی / Cr طرف حساب‌های انتظامی — no commercial account is
  ever touched); `return` reverses it (سند ۸). `call_in`
  (held → called_in) posts TWO journals in ONE transaction
  (سند ۹الف memorandum reversal + سند ۹ب commercial
  recognition, identical to سند ۱) and flips `purpose` to
  `payment`; it REQUIRES `accountant_approval` (§9.1 #4).
  `unexpected_call_in` (issued collateral presented without
  notice) posts ONLY the memorandum reversal and flips
  `purpose` to `payment` — the commercial journal is
  accountant-gated and is NEVER posted by the engine; it
  REQUIRES `statement_reference` (the matched withdrawal).
  `endorsed_bounce` (endorsed → bounced) posts NO journal
  (§9.1 #3) and requires reason + date + certificate.
- **سند ۵ / سند ۱۰ account correction (US-532, verified live).**
  US-531 debited Notes Payable 2.1.5 for سند ۵; the live chart
  says 2.1.1 is Trade Payables and 2.1.5 is Notes Payable, so
  سند ۵ and سند ۱۰ debit **2.1.1**. The contract tests assert
  the move-line codes for both (`test_cheques_us532.py`).
- **Checkbooks (US-532).** `account.checkbook` leaf counts are
  computed Function fields (`used_leaves` counts issued cheques
  in a non-voided state, `voided_leaves` counts `voided` ones).
  The adapter reads them via `list_engine_checkbooks` /
  `get_engine_checkbook` (report 8); the Fintech
  chequebook-inquiry is a SIMULATOR until the ۵ت remainder (B3)
  and never invents Sayyad data.

## Reads

- `GET /<db>/rest/model/account.check/<id>` with
  `fields=CHEQUE_FIELDS` (`id`, `company`, `kind`, `purpose`,
  `sayad_id`, `sayad_status`, `state`, `storage_location`,
  `amount`, `issue_date`, `due_date`, `number`, `bank`, `branch`,
  `issuer_account_number`, `endorsee`, `endorsee_type`, `moves`).
  404 → the adapter returns `None`. `moves` reads back as a list
  of move ids — the adapter does NOT attribute them (see above).
- `GET /<db>/rest/model/account.check` (search, `company = <id>`,
  optional `state = <state>`, `id DESC`) for `list_engine_cheques`.
- `GET /<db>/rest/model/account.checkbook` (search, `company =
  <id>`, `id DESC`) and `GET
  /<db>/rest/model/account.checkbook/<id>` with
  `fields=CHECKBOOK_FIELDS` (`id`, `company`, `name`, `bank`,
  `bank_account`, `first_number`, `last_number`, `total_leaves`,
  `used_leaves`, `voided_leaves`, `remaining_leaves`) for
  `list_engine_checkbooks` / `get_engine_checkbook` (US-532).
  404 → the adapter returns `None`.

## Company currency

Every cheque call first verifies the company is Rial
(`_require_irr_company`: `company.company` → `currency.currency`
code `IRR`). A non-Rial company fails the call before anything
reaches the module.

## Open gaps (rebase step, after US-530 merges)

1. **`transition_rest` — DONE (US-531, this branch).** Added to
   `tryton-modules/account_check_ir` (one method + the
   `destination_account` field + a `move.py` extending
   `account.move._get_origin` with `account.check`, so the
   journal's `origin` links back to the cheque), installed on
   lane 2 (`trytond-admin -u account_check_ir` + trytond
   restart), live contract tests
   (`tests/contract/test_cheques_us531.py`) run against it.
2. **Party resolution — DONE (US-531 + US-532).** The adapter
   resolves the party at intake (find-or-create `party.party` by
   national id → `party.code`; an explicit party on the command
   is kept as-is): the ISSUER for a received cheque, the
   BENEFICIARY (payee) for an issued cheque (US-532). The module
   fails a journal transition closed when the cheque has no party.
3. **Bank resolution.** `issuer_bank` ("056 سامان", code + name)
   has no engine field to land in (`bank` is a Many2One to a
   `bank` record; no `bank` records exist in the test DBs). Leave
   unset in US-531; resolve (find-or-create `bank` + its party)
   with the party work.
4. **Move attribution on plain reads — DONE (US-531).** `moves`
   is an unattributed id list; the move description now carries
   the attribution (`Cheque <sayad_id> — سند <n> (<transition>)`)
   and the registry's accumulated map is the audit trail
   (report 17).
