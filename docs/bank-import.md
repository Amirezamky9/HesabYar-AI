# Bank statement import + reconciliation (US-514, B1/B2)

Bank Melli has no public statement API, so manual import is the first
bank capability: the operator uploads a bank-export file (Excel, CSV or
PDF) and HesabYar registers it as a **draft** statement in Tryton. A
second step reconciles the draft statement's lines against invoices and
payments. Nothing here ever asks for or stores an internet-banking
password — the OAuth/consent model (ADR 0003, decision 10) is the only
credential model, and import needs no credential at all.

B3 (the Fintech aggregator + inquiry) is explicitly out of scope: no
aggregator code lives in this story.

## Import flow (B1)

```
upload (base64 JSON) → detect → create batch → preview → confirm → draft statement
                                  (saved or inline column mapping)   (review item resolved)
```

1. **Detect** — `POST /bank/statements/imports/detect` sniffs the format
   from content (`%PDF` → pdf, `PK\x03\x04` → xlsx, else CSV; the
   extension is ignored), detects the CSV encoding (BOM first, then
   UTF-8 → windows-1256 → windows-1252), and suggests a column mapping
   from the header row. Nothing is persisted.
2. **Create** — `POST /bank/statements/imports` parses the file with a
   saved column mapping or an inline one. Dates are Jalali (or Gregorian,
   when the file says so); Persian/Arabic-Indic digits are normalized;
   amounts convert Toman → Rial ×10 at this boundary (HALF_UP, with a
   `was_rounded` flag; fractional Rial is rejected). The 2,000-row
   bank-export cap is enforced here (`BANK_IMPORT_TOO_MANY_ROWS`).
   Files are at most 10 MB; **file bytes are never stored** — only the
   filename, for provenance.
3. **Dedup (two layers)** — rows with a bank reference dedup on
   (tenant, company, bank, bank_ref, date), enforced by a partial unique
   index; rows without one dedup on a match key
   (sha256 of date + amount + normalized description). Within-file
   duplicates are flagged too.
4. **Preview + review** — every batch is born `awaiting_review` with an
   accounting-effect preview (each row's suggested account — a learned
   rule, else the import's default/suspense account) and one
   `manual_import_review` queue item. Tryton requires an account on
   every statement line, so uncoded rows take the default account and
   the preview says so.
5. **Confirm** — `POST .../imports/{id}/confirm` writes the draft
   statement to the engine (`create_bank_statement`) and resolves the
   review item. Cancel writes nothing and resolves the item as
   cancelled. Confirming/cancelling twice is a 409.

Column mappings are per bank and versioned by supersede
(`POST /bank/column-mappings/{id}/supersede`); a mapping is valid when
it has a date and a description and either a signed amount or a
debit+credit pair. A row with both debit and credit set is a parse
error, not a silent choice.

## Reconciliation (B2)

`POST /bank/statements/{id}/reconcile` implements the Phase 2 typed-501
stub. It runs only on **draft** statements and is idempotent per line:
already-matched lines are reported, not re-matched; unmatched lines do
not file a second review item while the first is still open.

Matching priorities, per line:

1. **Exact reference** — the line's bank reference equals an
   invoice/payment reference (confidence 100).
2. **Amount + date + direction** — exact amount, date within
   ±`match_window_days` (default 2, from the installation settings),
   same direction (money in/out). A unique candidate auto-matches (95);
   several candidates make the line `ambiguous` (70).
3. **Smart suggestion** — user-defined W3 bank rules first (US-565:
   AUTO rules auto-apply at 90; SUGGEST rules suggest at 90 and a
   pending suggestion row is recorded for the operator to confirm),
   then learned rules (AUTO rules auto-apply at 85; SUGGEST rules
   suggest), then a scored suggestion (amount 40 + date 20 + party
   25 + partial reference 15; threshold 50, capped 95, top 3 kept).
   Wrong-direction candidates score 0.

**Tie-out guard (US-565).** When the statement carries start/end
balances and `start + Σ lines ≠ end`, every rule-target AUTO
decision (W3 and B2 alike) degrades to SUGGESTED with a Persian
reason naming the tie-out failure — the line is not coded and waits
for a human. When the balances tie out, auto-coding proceeds; when
the statement carries no balances, the tie-out cannot be checked
and reconcile behaves exactly as before (the statement stays a
draft for a human to validate in the engine). A wrong auto-code can
be undone while the statement is a draft
(`POST /bank/statements/{id}/lines/{line_id}/undo-auto-code` — the
auto match is rejected and the corrected coding is written; see
`docs/bank-rules.md`).

**P2 destination note.** The phased design calls for a destination-Shaba
check at P2, but Tryton 8.2 core has no link from `account.payment.journal`
to an account journal or bank account, so a per-candidate destination
check is impossible with today's data. P2 is amount + date + direction;
the statement journal's bank account and Shaba are recorded on every
match record. The full per-candidate destination filter is a 5پ/5ث
follow-up (settlement tranches and cheque clearing carry the
destination). Do not invent it.

**Unmatched deposits never auto-post to revenue.** Unmatched,
ambiguous and suggested lines file `bank_unmatched` queue items
(WARNING for deposits, INFO for withdrawals) whose suggested action
points the accountant at the reconcile page. A human applies the match
(`POST /bank/statements/{id}/lines/{line_id}/match`, optionally
`save_as_rule`); confirmed mappings become rules (SUGGEST → AUTO after
3 confirmations) that future imports and reconciles reuse.

**No-validate boundary.** Reconciliation is matching plus line-field
writes only (account, party, `related_to` invoice). It never validates
or posts the statement: the Tryton Reconcile Statement wizard is UI-only
(wizards are not REST-callable — the same verdict as the Phase 2
setup-wizard spike), and the accountant validates in the engine, where
Tryton natively reconciles lines whose `related_to` is set. `related_to`
accepts invoices only; payment matches set party + account.

## Report (§11.2)

`GET /bank/reconciliation-status` returns per-journal matched /
suggested / ambiguous / unmatched lines for a period, with the §11.1
freshness indicator (`live` when a source record exists, else `empty`),
`match_rate`, `unmatched_balance_irr`, and the oldest unmatched line.
Cancelled statements are excluded; lines are classified from the
engine's `related_to` plus the latest non-rejected match record.

## Endpoints

| Method & path | Scope | Notes |
|---|---|---|
| `POST /bank/statements/imports/detect` | `banking:import` | No persistence |
| `POST /bank/statements/imports` | `banking:import` | Idempotency-Key required |
| `GET /bank/statements/imports` | `banking:read` | `company`, `status` |
| `GET /bank/statements/imports/{id}` | `banking:read` | Batch + rows |
| `POST /bank/statements/imports/{id}/confirm` | `banking:import` | Idempotency-Key required |
| `POST /bank/statements/imports/{id}/cancel` | `banking:import` | Idempotency-Key required |
| `POST /bank/column-mappings` | `banking:import` | Idempotency-Key required |
| `GET /bank/column-mappings` | `banking:read` | `bank_key` |
| `POST /bank/column-mappings/{id}/supersede` | `banking:import` | Idempotency-Key required |
| `GET /bank/statement-journals` | `banking:read` | `company` |
| `POST /bank/statements/{id}/reconcile` | `banking:reconcile` | Idempotency-Key required |
| `GET /bank/statements/{id}/matches` | `banking:read` | |
| `POST /bank/statements/{id}/lines/{line_id}/match` | `banking:reconcile` | Idempotency-Key required |
| `POST /bank/statements/{id}/lines/{line_id}/undo-auto-code` | `banking:reconcile` | Idempotency-Key required; US-565 |
| `GET /bank/match-rules` | `banking:read` | `company`, `bank_key` |
| `POST /bank/match-rules/{id}/mode` | `banking:reconcile` | Idempotency-Key required |
| `GET /bank/reconciliation-status` | `banking:read` | `company`, `date_from`, `date_to` |

The bank auto-coding rules endpoints (CRUD, enable/disable,
promote/demote, suggestions) live in `docs/bank-rules.md`.

Uploads are base64 JSON, not multipart: the BFF catch-all proxy parses
bodies as JSON only.

## Storage

Migration `0012_us514_bank_import_reconciliation` adds five tables:
`bank_column_mappings`, `bank_import_batches`, `bank_import_rows`,
`bank_match_rules`, `bank_statement_matches`. Match status includes
`unmatched` ("looked, found nothing, review item filed") so reconcile
idempotency does not need a second query; `outcome` on a match record
distinguishes ambiguous from suggested in the report.
