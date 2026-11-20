# Cash Box (US-516)

Cash-box counting and shortage/surplus journals (Phase 5 wave 2,
stream K2; phased design §1.3 5چ, §6 هـ.۲, §7; ADR 0003). The cash
daybook lives on Tryton `account_statement` — one Statement Journal
per cash box, each with its own cash account — while the counting
session is a HesabYar-side entity (Tryton has none).

The Iranian three-step method (§هـ.۲.۲):

1. **Count in the presence of a supervisor** who is a different user
   than the cashier (internal control).
2. **Signed minutes (صورتجلسه) as supporting documents** (اسناد
   مثبته), prepared BEFORE any accounting entry — only when the
   variance is non-zero; with a zero variance, supervisor attendance
   is enough.
3. **Shortage/surplus journal** — a DRAFT until a manager approves it
   in the review queue. A cash shortage is NEVER auto-posted.

## What lives where

| Piece | Location |
|---|---|
| Tables | Alembic `0012`: `cash_boxes`, `cash_count_sessions`, `cashbox_settings` |
| ORM models | `src/hesabyar/infrastructure/persistence/postgres/models.py` (`CashBoxModel`, `CountSessionModel`, `CashBoxSettingsModel`) |
| Domain model | `src/hesabyar/domain/cashbox/models.py` (variance math, policy journal lines, report) |
| Ports | `src/hesabyar/domain/cashbox/ports.py` (`CashBoxPort`) + `domain/accounting/ports.py` (`get_account_balance`, `get_account_by_code`, `list_invoice_totals`, `list_statement_journals`, `delete_journal_draft`, `get_general_journal`) |
| Repository | `src/hesabyar/infrastructure/persistence/postgres/cashbox_repository.py` |
| Query service | `src/hesabyar/application/queries/cashbox.py` |
| Command service | `src/hesabyar/application/commands/cashbox.py` |
| REST | `src/hesabyar/interfaces/rest/routers/cashbox.py` (+ `schemas/cashbox.py`) — 15 endpoints |
| Admin pages | `apps/admin/app/(dashboard)/banking/cash-boxes/page.tsx` + `apps/admin/features/cash-box/`; report tab in `features/reports/` («گزارش‌ها ← صندوق») |
| Chart extension | `tryton-modules/hesabyar_setup/.../account_chart_en.xml` (4 accounts, §2 below) |
| Provisioning | `~/workspace/coord-logs/us516-provision-cashboxes.py` (trytond-console, idempotent) |

## Tryton setup

### Chart accounts (hesabyar_setup)

| Code | Name | Type | `party_required` |
|---|---|---|---|
| 1.1.5 | Petty Cash | petty cash | False |
| 1.2.6 | Cash Shortage and Overage | accounts receivable | False |
| 1.2.7 | Temporary Debtors — Cash Shortage | accounts receivable | False |
| 2.1.6 | Miscellaneous Creditors — Cash Overage | payable | False |

The store cash box uses the existing 1.1.1 (Cash and Cash
Equivalents). `party_required` is False on all four: the cashier is
named in the session/minutes, not a Tryton party. None are
reconciled: under policy (a) the 1.2.6 balance closes at period end
(shortage → misc expenses, overage → other income); under policy (b)
the 1.2.7/2.1.6 balances settle within 24h. After a module update,
run `update_chart` on each company (REST action
`POST /rest/model/company.company/<id>/update_chart`) — the Adapter
Test company went 145 → 149 accounts. Note: `update_chart` takes
longer than the REST client's 30s timeout on a remote DB; the call
may time out client-side while completing server-side — verify with
an account search afterwards instead of retrying blindly.

No separate Tryton 8.2 petty-cash module was found (UNVERIFIED):
petty cash is a separate cash box with its own ceiling, modelled
with the same Statement Journal.

### Statement journals

One statement-type `account.journal` + one `account.statement.journal`
(validation `balance`) per cash box, on the company's IRR currency.
The provisioning script creates them for the Adapter Test company:
«HesabYar US-516 Store Cash Box» (account 1.1.1) and «HesabYar
US-516 Petty Cash Box» (account 1.1.5). A cash box's cash account is
always the journal's account — derived from the engine, never typed
in. One box per (tenant, company, statement journal), enforced in
the service and by a unique constraint.

## The counting session

States: `in_progress → awaiting_supervisor → awaiting_manager →
archived`. One open session per box. The book balance is snapshotted
read-only from the engine at start (posted moves only, as of the
start date) and never changes afterwards.

| Step | Actor | Rule |
|---|---|---|
| Start | cashier + supervisor | Supervisor must differ from the cashier (`COUNT_SUPERVISOR_MUST_DIFFER`); inactive box refuses (409) |
| Enter count | cashier | In `in_progress`/`awaiting_supervisor` only, while no signature/attendance exists; after any signature the count is locked (recount flow only). Optional denomination breakdown must sum to the counted amount (`COUNT_DENOMINATIONS_MISMATCH`) |
| Supervisor sign | supervisor | Records attendance in EVERY session; signs the minutes when the variance is non-zero. A balanced session archives here — no minutes, no journal, no review item |
| Cashier sign | cashier | Only when minutes are required; carries the minutes text (`COUNT_MINUTES_REQUIRED` without it) |
| Manager sign | manager | Only when minutes are required; must differ from both cashier and supervisor (`COUNT_WRONG_ACTOR`) |

When all three minutes signatures are present, the session moves to
`awaiting_manager`: the DRAFT variance journal is created in Tryton
and a `cashbox_variance` review item is filed (severity `critical`
when a material threshold is set and |variance| ≥ threshold, else
`warning`). If completion fails after the signatures were saved
(e.g. the variance account is missing from the chart), a re-sign by
the matching signer retries the completion instead of raising a
duplicate-signature error — otherwise the session could never
complete.

### Variance journal

| Policy | Shortage (counted < book) | Overage (counted > book) |
|---|---|---|
| (a) `expense` (default) | Dr 1.2.6 / Cr cash | Dr cash / Cr 1.2.6 |
| (b) `receivable_from_cashier` | Dr 1.2.7 / Cr cash | Dr cash / Cr 2.1.6 |

The journal is for the variance amount ONLY; if the variance comes
from a prior posting error, correcting the posting takes precedence
over booking a variance. Posting is a SEPARATE endpoint that
verifies the linked review item is RESOLVED with resolution
`ADJUSTED` and the audit trail contains an `approve_variance_entry`
action — otherwise 409 `COUNT_REVIEW_NOT_APPROVED`. Posting needs
`cashbox:manage` AND `ledger:post`.

### Recount

The manager's `recount` action on the review item moves it to
`in_review`; the cashier then reopens the session (the superseded
DRAFT is deleted via `delete_journal_draft`; an already-deleted
draft is tolerated). Recount completion updates the SAME review
item's figures (status → open, new reason/action/amount/severity,
audit entry `figures_updated`) and creates a NEW draft. A
zero-variance recount archives the session and refreshes the item
to amount 0 + `info` severity. If the manager resolves the item
mid-recount, a NEW item is filed with the recount's figures and
linked to the session (the old item stays resolved as history).

## Settings (per tenant)

| Setting | Default | Meaning |
|---|---|---|
| `variance_policy` | `expense` | (a) shortage/surplus account or (b) temporary debit/credit |
| `material_variance_threshold_irr` | null | \|variance\| ≥ threshold → critical review item (null = no split) |
| `count_reminder_days` | 7 | «Uncounted box» alert threshold |
| `count_schedule` | `daily` | `daily` / `weekly` / `period_end` |
| `supervisor_required` | true | Supervisor-must-differ control |
| `shortage_settlement_hours` | 24 | Settlement deadline under policy (b) |

A missing settings row means the defaults apply. Updates need
`cashbox:manage`.

## Report (spec §11.1)

Per cash box + period: opening/closing book balances (posted moves
only), receipts/payments (trial balance), sessions with
shortage/overage totals, recorded sales total (posted customer
invoices — labelled `sales_source: "tryton_invoices"`), and the
unrecorded-sales indicator = net overage floored at 0 (an indicator,
not proof). The sales side is Tryton invoices only until the store
link lands (5ب), and says so honestly.

## API

| Endpoint | Scope |
|---|---|
| `GET /cash-boxes/settings`, `PATCH /cash-boxes/settings` | read / manage |
| `GET /cash-boxes/statement-journals` | read |
| `GET /cash-boxes/report` | read |
| `GET /cash-boxes`, `POST /cash-boxes` | read / manage |
| `GET /cash-boxes/{id}`, `PATCH /cash-boxes/{id}` | read / manage |

`GET /cash-boxes/{id}` takes no `company` parameter: the box
carries its own engine company id (`engine_company_id`,
denormalized at registration, migration 0013 — the same pattern
as on the counting session). The variance draft is booked to the
company's general journal resolved explicitly through the port
(`get_general_journal` — prefers `"<Company Name> General
Journal"`, the provisioning convention): `create_journal_draft`
with no journal fails once the engine holds several general
journals, and Tryton's own modules already ship several
(`Stock`, `Miscellaneous`). The variance account is read
targeted (`get_account_by_code`), never via the whole chart:
the chart fetch costs ~28s against a remote engine — nearly
the BFF's whole 30s budget for the manager-sign request that
completes a variance session (measured 2026-11-18: chart 28s +
journal 4.6s + draft 9.1s ≈ 42s before the fix). The dialog
also refreshes after a failed action, since a BFF timeout can
land server-side anyway. The report was slow the same way
(28s): the adapter now memoizes the per-request company IRR
check and currency codes, the opening balance is derived
(closing − net activity, exact), and recorded sales come from
`list_invoice_totals` (one search, no lines/parties/currencies)
— 14–16s after the fixes.
| `POST /cash-boxes/{id}/count-sessions` | count |
| `GET /count-sessions`, `GET /count-sessions/{id}` | read |
| `POST /count-sessions/{id}/count` | count |
| `POST /count-sessions/{id}/sign` | count |
| `POST /count-sessions/{id}/post-variance-journal` | manage + ledger:post |
| `POST /count-sessions/{id}/recount` | count |

New scopes: `cashbox:read`, `cashbox:count`, `cashbox:manage`
(35 scopes total). Error codes: `CASHBOX_NOT_FOUND`,
`CASHBOX_JOURNAL_CONFLICT`, `CASHBOX_INVALID`,
`CASHBOX_SETTINGS_INVALID`, `COUNT_SESSION_NOT_FOUND`,
`COUNT_SESSION_INVALID_STATE`, `COUNT_SESSION_ALREADY_OPEN`,
`COUNT_SUPERVISOR_MUST_DIFFER`, `COUNT_MINUTES_REQUIRED`,
`COUNT_REVIEW_NOT_APPROVED`, `COUNT_DENOMINATIONS_MISMATCH`,
`COUNT_RECOUNT_NOT_REQUESTED`, `COUNT_WRONG_ACTOR`.

## Extension points (links land with their source modules)

The session carries optional link refs — `cheque_refs` (cheques ←
5ث), `deposit_ref` (bank deposit ← B1/B2), `order_refs` (COD ←
S1/P1), `installment_refs` (cash installment ← 5ج) — empty in v1.
The other modules' halves are NOT built here. No new canonical
events were added: new event names need ADR approval first, and the
cash-box events (`cashbox.count.completed`,
`cashbox.variance.posted`, …) are still pending that approval.

## Tests

- Unit: `tests/unit/test_cashbox_services.py` (83 tests),
  `tests/unit/test_tryton_adapter_cashbox.py` (24 tests, MockTransport).
- REST e2e: `tests/interfaces/test_rest_cashbox.py` (real app + DB +
  live Tryton; skips cleanly without the server/provisioning).
- Contract: `tests/contract/test_tryton_adapter_cashbox.py`
  (balance cross-check, draft-delete semantics).
- Frontend: `apps/admin/features/cash-box/cash-box-data.test.ts`,
  `apps/admin/e2e/cash-box.spec.ts`.
