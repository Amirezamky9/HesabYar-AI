# Bank auto-coding rules (US-565, Phase 5-7 ۵خ W3)

Xero-style bank rules for auto-coding statement lines:
`when <statement-line pattern> → suggest <account + mapping>`.
User-defined bank rules are **W3 rules** (phased design §7.4 (ح)):
they are managed at بانک و صندوق ← قواعد تطبیق
(`/banking/bank-rules`), and they live in their own store
(`bank_rules`, Alembic `0039`) — separate from the B2 learned
mappings (`bank_match_rules`, US-514/US-552), which enter the engine
as suggestions only, never as auto-rules.

## The rule model

| Field | Meaning |
|---|---|
| `name` | Operator-facing rule name. |
| `bank_key` | Bank key the rule applies to; empty = every bank. Recorded on the rule; the matcher does not enforce it (reconcile cannot reliably know a statement's bank key — the same limitation as B2). |
| `description_contains` | Description substring the line must contain (NULL = no condition). |
| `counterparty_contains` | Counterparty substring, matched against the normalized description OR the line's party name (statement lines have no dedicated counterparty field). |
| `amount_min_irr` / `amount_max_irr` | Inclusive bounds on the line's magnitude in whole Rial (NULL = unbounded). |
| `direction` | `in` (deposit), `out` (withdrawal) or `any`. |
| `target_account_id` | Engine account id the rule codes to. |
| `target_party_id` | Engine party id, when known. |
| `target_kind` | `account` or `misc_income`. |
| `mode` | `suggest` or `auto` (below). |
| `confirmations` | Confirmed suggestions behind this rule. |
| `priority` | Firing order inside one company's set (lower runs first); ties break by creation time, then id. Default 100. |
| `enabled` | A disabled rule never fires — live or suggested. |

Tables (Alembic `0039`): `bank_rules`, `bank_rule_suggestions`.
Suggestions survive rule deletion (`ON DELETE SET NULL`, with the
rule name + target snapshotted on the row) so the audit trail of
what was suggested stays readable.

## The pattern language

All conditions are ANDed; at least one condition is required —
a rule with no conditions would match every line, so creation and
update reject it (`BANK_RULE_INVALID`). A condition-less row written
outside the service still matches nothing (fail-closed guard in
`bank_rule_matches`).

Pattern fragments are normalised before storage and before
matching (Persian digits, Arabic ي/ك → ی/ک, case folding,
whitespace): «اسنپ‌پي» matches «پرداخت اسنپ‌پی». Amount bounds
compare against the line's magnitude (a −۵۰٬۰۰۰ withdrawal is
۵۰٬۰۰۰ for range purposes), inclusive on both ends.

## Suggest → Auto (threshold 3)

A rule is born in SUGGEST mode with 0 confirmations; **there is no
way to create an AUTO rule directly**. When a SUGGEST rule's
pattern matches a line during reconcile, the line is SUGGESTED
(never coded) and a pending `BankRuleSuggestion` row is recorded
(deduped per rule + line — a re-run records no second row). The
operator confirms the suggestion from the قواعد تطبیق page (or the
review queue item it files): the coding is applied, a CONFIRMED
match is recorded, and the rule's confirmation count grows by one.

At **3 confirmed suggestions** the rule becomes promotable; a human
promotes it (`POST /bank/rules/{id}/promote`) and only then does it
auto-code. Promotion keeps the count (promoting is not a
confirmation). This is the same pattern as the US-552 mapping
learning — same threshold constant
(`RULE_AUTO_MIN_CONFIRMATIONS = 3`), same human decision point.

- **Demote** (`POST /bank/rules/{id}/demote`) returns an AUTO rule
  to SUGGEST and resets its confirmations to 0 — the trust is
  re-earned from zero.
- **Editing the pattern or the target** (description, counterparty,
  amount range, direction, bank key, target account/party/kind)
  resets the confirmations and drops an AUTO rule back to SUGGEST:
  the trust was earned for the old definition. Name, priority and
  enabled changes do not reset anything.
- **Deleting a rule** keeps its suggestions (rule_id NULL).

## Where rules fire (P3)

Reconcile's priority 3 (smart suggestion, see
`docs/bank-import.md`) now runs user-defined W3 rules **first**,
before the B2 learned rules: an explicit user definition outranks a
learned mapping, and a W3 SUGGEST rule holds a line for review even
when an AUTO learned rule would have coded it. First match wins
(priority, then creation time, then id); disabled rules are
skipped. W3 matches record confidence **90** (above B2's 85, below
P1/P2's 100/95).

The import preview (`_suggest_coding`) is NOT extended to W3 rules
in this story: it still suggests from B2 rules + the default
account, and W3 rules suggest at reconcile. Extending the preview
is a follow-up, not a gap in the acceptance criteria.

## The gate + tie-out guarantee

Auto-coded lines still go through the policy gate + tie-out before
posting. Concretely, in three layers:

1. **Tie-out guard (in reconcile).** When the statement carries
   start/end balances and `start + Σ lines ≠ end`, every
   rule-target AUTO decision (W3 and B2 alike) degrades to
   SUGGESTED with a Persian reason naming the tie-out failure —
   the line is not coded and waits for a human. When the balances
   tie out, auto-coding proceeds; when the statement carries no
   balances, the tie-out cannot be checked and reconcile behaves
   exactly as US-514 did (the statement stays a draft for a human
   to validate in the engine).
2. **No-post structure.** Reconcile only ever calls
   `update_bank_statement_line` (line-field writes on a draft
   statement); there is no validate/post port for statements, so
   reconcile cannot post anything. The F3 policy gate is
   event-pipeline-shaped (it evaluates event drafts, not statement
   lines) and is deliberately NOT wired into reconcile — there is
   nothing for it to allow or deny on this path, because reconcile
   posts nothing. Validation stays human, in Tryton, where the
   accountant approves the draft.
3. **Undo (below).** A wrong auto-code is correctable while the
   statement is a draft.

## Undo / correction

`POST /bank/statements/{id}/lines/{line_id}/undo-auto-code`
(scope `banking:reconcile`, Idempotency-Key required) undoes a
wrong auto-code, but only a **live AUTO_APPLIED match whose target
is a rule**, and only while the statement is a draft (409
`BANK_RULE_NOTHING_TO_UNDO` / `BANK_STATEMENT_NOT_DRAFT`
otherwise). The corrected account is **required**: an engine line
cannot be left uncoded (Tryton requires an account on every line),
so undo always means "correct the mapping", never "clear it". The
auto match is rejected, the corrected coding is written to the line
and recorded as a CONFIRMED manual match, and the response names
the undone rule (`undone_rule_id`) so the operator can demote or
disable it from the قواعد تطبیق page. The statement stays a draft
throughout.

The UI exposes undo inline on the statement-detail page, in the
reconcile-result area, for lines with outcome `matched` or
`already_matched` and `target_type: rule`.

## REST endpoints

| Method & path | Scope | Notes |
|---|---|---|
| `GET /bank/rules` | `rules:read` | `company`, `enabled`, `mode` |
| `POST /bank/rules` | `rules:manage` | 201; born SUGGEST |
| `GET /bank/rules/{id}` | `rules:read` | |
| `PUT /bank/rules/{id}` | `rules:manage` | Only set fields change; explicit null clears a condition |
| `DELETE /bank/rules/{id}` | `rules:manage` | 204; suggestions survive |
| `POST /bank/rules/{id}/enable` | `rules:manage` | |
| `POST /bank/rules/{id}/disable` | `rules:manage` | |
| `POST /bank/rules/{id}/promote` | `rules:manage` | 409 `BANK_RULE_NOT_PROMOTABLE` below 3 confirmations |
| `POST /bank/rules/{id}/demote` | `rules:manage` | Confirmations reset to 0 |
| `GET /bank/rules/suggestions` | `rules:read` | `rule_id`, `status`, `company` |
| `POST /bank/rules/suggestions/{id}/confirm` | `banking:reconcile` | Idempotency-Key required; applies the coding |
| `POST /bank/rules/suggestions/{id}/reject` | `rules:manage` | Also rejects the reconcile's SUGGESTED match |
| `POST /bank/statements/{id}/lines/{line_id}/undo-auto-code` | `banking:reconcile` | Idempotency-Key required |

No new scope: bank rules are W3 rules, so rule configuration uses
the existing `rules:read` / `rules:manage` scopes. Confirm and undo
write engine lines (like a manual match), so they use
`banking:reconcile`.

Error codes: `BANK_RULE_NOT_FOUND` (404),
`BANK_RULE_INVALID` (422), `BANK_RULE_NOT_PROMOTABLE` (409),
`BANK_RULE_SUGGESTION_NOT_FOUND` (404),
`BANK_RULE_SUGGESTION_NOT_PENDING` (409),
`BANK_RULE_NOTHING_TO_UNDO` (409). Not to be confused with the
pre-existing B2 `BANK_MATCH_RULE_NOT_FOUND`.

## The management UI (US-565)

بانک و صندوق ← قواعد تطبیق (`/banking/bank-rules`,
`apps/admin/features/bank-rules/`): a Persian RTL page over the
REST endpoints above — the frontend never touches the database.
Two tabs:

- **قوانین.** Rule cards with the mode badge (پیشنهاد/خودکار),
  a one-line Persian pattern summary, the target account, the
  confirmation count («۲ از ۳») and a promotion hint, and actions:
  promote (only when eligible), demote, edit, delete, enable/disable.
  The create/edit dialog takes amounts in تومان (×10 to Rial at
  the boundary) and warns when an edit would reset the rule's
  confirmations.
- **پیشنهادها.** Suggestions newest first, filterable by status,
  with confirm/reject on pending rows and a link to the statement.

The page states the B2 rule plainly (learned mappings are
suggestions only) and the tie-out + undo guarantee. The workflows
page (`/integrations/workflows`) links to it.

## Tests

- `tests/unit/test_bank_rules.py` — the pattern language (all
  conditions, normalization, fail-closed), matcher priority (W3
  beats B2, first-match order, disabled skipped), CRUD validation,
  promotion at threshold 3 / demote, confirm/reject (incl. review
  item resolution and the deleted-rule path), undo (incl. the
  non-rule and non-draft rejections), reconcile integration (W3
  auto-code, suggestion recording + dedup, the full
  suggest → confirm ×3 → promote → auto loop), the tie-out guard
  (mismatch degrades, tie-out codes, no balances codes), and the
  no-post guarantee (only `update_bank_statement_line` is called;
  the statement stays a draft).
- `tests/interfaces/test_rest_bank_rules.py` — the endpoints
  (CRUD, 404/422/409s, promote/demote, suggestions, confirm/reject,
  undo, scope checks, OpenAPI listing).
- `apps/admin/features/bank-rules/bank-rules-data.test.ts` — the
  Persian pattern summary, the تومان ⇄ Rial boundary, validation,
  the update diff.
- `apps/admin/e2e/workflows.spec.ts` — the rule journey, the
  recurring journey (BFF — US-563 shipped no UI), the bank-rule
  journey (suggest → confirm ×3 → promote → auto-code → undo),
  responsive checks at 360/390px.
