# Sayyad reconciliation — US-534

Three layers, one rule: **clearing status comes ONLY from the bank
statement.** Sayyad tells you what the banking network says about a
leaf; it never moves a cheque's accounting state.

## Layer 1 — Fintech (legal tier): simulator + manual fallback

The 18 Sayyad scopes from §5 §1.3 are wired as a *simulator* with
the same pattern as the US-404 Moadian simulator
(`src/hesabyar/infrastructure/sayyad/simulator.py`):
deterministic, in-memory, `simulated=True` on every result, uuid5
references (`SIM-…`). Scenarios are seeded explicitly
(`SayyadScenario`); an unseeded lookup returns `not_found` — the
simulator never invents a verdict.

Scopes (documented names only — §9.2 #1–3 UNVERIFIED, no field
guessed):

| Group | Scopes |
|---|---|
| Status | `credit:cheque-status-inquiry:get`, `credit:sayad-serial-inquiry:get`, `credit:transfers-chain-inquiry:get`, `credit:cheque-color-inquiry:get`, `credit:back-cheques:get`, `credit:chequebook-inquiry:get` |
| Actions | `credit:ac-sayad-issue-cheque:post`, `credit:ac-sayad-transfer-cheque:post` |
| Legal variants | the six `*-legal` / `ac-sayad-*-legal` scopes |
| SMS variants | the eight `sms-*` scopes |

The live clients (`FintechSayyadLiveClient`) raise
`SayyadLiveNotReadyError` on every call until the ۵ت remainder (B3)
verifies the request shapes, auth model and tariff and wires
credentials. **No production Fintech call without credentials.**

Two meanings of "in-transit" are kept apart on purpose:

- **Sayyad in-transit** — the network says the leaf is moving
  between holders. It is a *status*, never an accounting state.
- **Accounting `in_clearing`** — the cheque was deposited and its
  سند ۲ is waiting for the bank statement. Only the statement moves
  it to `cleared`.

## Layer 2 — Ehrazino (natural tier): colour + sayad-check

Verified 2026-11-19 from Ehrazino's public API pages
(search results on `ehrazino.com`):

- **cheque-color** — input `nationalCode` (10 digits natural /
  11 digits legal, type inferred from length); output colour name +
  numeric code **1–5**; **1,700 Toman = 17,000 Rial per call**;
  panel minimum charge 1,000,000 Toman.
- **sayad-check** — input `sayadId` (16 digits); outputs
  `branch_code`, `check_type`, `expiration_date`, `iban`,
  `issue_date`, `name`, `returned_cheques`, `sayad_id`,
  `serial_no`, `series_no`; **6,900 Toman = 69,000 Rial per call**.
  Does NOT return a colour. Both date fields are Jalali.

Still UNVERIFIED: the endpoint URL path + auth header (the docs
page could not be opened), and whether Ehrazino opens a business
account for natural persons (§9.2 #9). The live client
(`EhrazinoLiveClient`) raises `SayyadLiveNotReadyError` until
those are verified.

**Colour is issuer history only, never a statement about a specific
leaf.** It is recorded as a point in time (`inquired_at` shown in
the UI) and decision logic runs on the **numeric code 1–5**, never
on amount thresholds. The sayad-check's `name` / `iban` are dropped
at the boundary — only verdict, code and time are stored (§3.5
never-store).

### Structured receipts + the mandatory receipt rule

Every inquiry writes a `cheque_inquiry_receipts` row (US-531 table):
`cheque_id`, `channel` (`sms` | `cbi_website` | `bank_app` |
`payment_app` | `finnotech_api` | `ehrazino_api`),
`inquiry_type`, `requested_at`, `responded_at`, `result_code`,
`result_summary`, `reference`, `recorded_by`, `cost_irr`
(SMS 350 Toman = 3,500 Rial; Ehrazino colour 17,000 Rial;
sayad-check 69,000 Rial; Fintech cost 0 until the tariff is
verified).

**Mandatory receipt rule:** every important transition —
especially `registered → held` — needs at least one item/status
receipt (`items_match` or `leaf_status`). A transition without
one is recorded with a warning and shows up in the mismatch
report (`POST /cheques/reports/receipt-rule-audit` files a
`missing_inquiry_receipt` review item per held cheque, idempotent
per open item). The reports `GET /cheques/reports/inquiry-ledger`
(total / by channel / colour distribution 1–5 / total cost) make
the rule auditable.

## Layer 3 — Clearing matching (4 confidence levels)

Built on the US-514 statement-import infrastructure
(`BankReconcileService` / `BankStatementMatch` /
`reconcile_move_lines`). Matching keys, in strength order:

1. **Exact amount in Rial — MANDATORY, zero tolerance.**
2. **Leaf number in the transaction description** — per-bank
   extraction rule (`LEAF_EXTRACTION_RULES`; generic `\d{4,}`).
   Melli is UNVERIFIED (§9.2 #7): leaf evidence from Melli
   descriptions caps confidence at `probable`, never `exact`.
3. **Date window** — `[deposit date, deposit date + clearing
   window]`. The window is configurable; **no hard-coded default**
   (§9.2 #6): with no window configured there is no upper bound,
   but confidence caps at `probable` with an explicit reason so the
   go-live checklist item stays visible.
4. **Destination bank account — MANDATORY** (= the statement
   journal's account).
5. **Transaction direction — MANDATORY** (deposits clear received
   cheques, withdrawals clear issued cheques).

| Confidence | Meaning | Action |
|---|---|---|
| `exact` | All keys | Auto-clear (auto-post policy; else draft + one-click confirm) |
| `probable` | No leaf number (or unset window / Melli leaf) | Review queue (`clearing_probable`) |
| `ambiguous` | Several same-amount cheques | **Never guess** — review queue with the candidate list (`clearing_ambiguous`) |
| `unmatched` | No candidate | Stays `in_clearing` + aging report |

**Never auto-bounce:** `in_clearing → bounced` needs a bank
document (non-payment certificate) or an inquiry — there is no
bounce path in the clearing service. A stale in-clearing cheque
stays and is only flagged in the aging report (report 4,
`GET /cheques/reports/in-clearing-aging`; `flag_stale_in_clearing`
files `stale_in_clearing` items, idempotent). **No partial
clearing:** a mismatched deposit is `unmatched` + manual
follow-up, never a partial clear.

**Deposit date is recorded at the `held → in_clearing`
transition and never backfilled.** Cheques deposited before
US-534 have `deposit_date = NULL` and can never auto-match (the
window key is unverifiable); manual clear remains.

### What "reconcile" means here

Applying an exact match calls the engine `clear` transition
(سند ۳) and then reconciles the deposit move's clearing-account
debit (سند ۲) with the clear move's clearing-account credit
(سند ۳) via the existing `reconcile_move_lines`. Both legs are
the same account + same party (the cheque's issuer party on all
lines) + equal amounts, so the adapter's pre-validation passes.
The contract test proves live that draft-move lines are `valid`
and reconcile. A reconcile failure never un-clears the cheque:
it files a `clearing_reconcile_failed` review item.

**Limitation:** the statement line itself is NOT re-coded
(that would double-book the bank debit). The match is recorded
as a `BankStatementMatch` (target `MANUAL`,
`target_ref="cheque:<id>"`) so the generic reconcile never
double-matches the line.

### Issued-cheque clearing (pending US-532)

Issued cheques are matched against withdrawals (exact amount +
leaf number + window + account). Until US-532 merges, the engine
refuses issued transitions: an exact issued match is recorded
and filed as `issued_clearing_pending_us532` — never applied.
