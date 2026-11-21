# Rules engine v1 (US-561, Phase 5-7 ۵خ W2)

User-configurable automation: `when <event> + <conditions> → do <action>`
(phased design §6.4). The engine is deliberately small: twelve canonical
event types, a safe condition DSL, six closed actions, dry-run, and a
full audit log. There is no arbitrary user code, no visual DAG builder,
no BPMN/Temporal — those are explicit non-goals (§6.4).

## The rule model

| Field | Meaning |
|---|---|
| `event_type` | One of the twelve canonical event types (§10.2 + ADR 0004 + ADR 0005). Never an ad-hoc name — new names need ADR approval. |
| `conditions` | A condition DSL tree (below); `{}` means "no conditions". |
| `action` | One action from the closed set (below), with its parameters. |
| `priority` | Order inside a rule set (lower runs first); ties break by creation time, then id. Default 100. |
| `enabled` | A disabled rule never fires live; it can still be dry-run (test before activation). |
| `installation_id` | NULL means tenant-wide (fires for every installation); otherwise one installation. |

Tables (Alembic `0036`): `rules`, `rule_executions`, `rule_set_configs`;
plus `review_items.tags` (a JSONB list, for the `tag` action).

## The condition DSL

A small safe JSONLogic-style language over the event's facts. No `eval`:
a condition is a JSON tree walked by the evaluator, and anything outside
the grammar is rejected when the rule is saved.

```
condition := {}                                  # always true
          |  {"and": [condition, ...]}
          |  {"or":  [condition, ...]}
          |  {"not": condition}
          |  {"==": [operand, operand]}  (also !=, >, >=, <, <=)
          |  {"in": [operand, operand]}  (also "not_in")
operand   := {"var": "dotted.path"} | <JSON scalar> | [<JSON scalar>, ...]
```

Facts (built by `build_facts` from the converted payload + context):

| Fact | Source |
|---|---|
| `event_type` | The canonical type string. |
| `payload` | The converted payload — amounts in whole Rial (the pipeline already converted Toman at the boundary). Fields under `payload.<path>`. |
| `amount_irr` | Best-effort total (`total_amount` / `gross_amount` / `refund_amount` / `amount`, first integer found — mirrors `NormalizedEvent.total_amount_rial`). Absent when the payload carries no total. |
| `channel` | `payment_channel` from the payload, else the context's channel key. |
| `party` | `party_id` / `customer_id` from the payload. |
| `company` | Engine company id (string) from the execution context. |
| `occurred_at` | ISO date/datetime from the context or payload. |

Semantics (fail-closed):

- A missing field never matches — even `!=`. A rule cannot fire on an
  absence it did not ask about.
- Numbers compare numerically (booleans are not numbers). Strings
  compare lexicographically — except ISO date/datetime strings on both
  sides, which compare as dates. A bare date never equals a datetime.
- Type mismatches compare False (`!=` included).
- `in` is list membership, or substring when both sides are strings.
- Limits (abuse guard): at most 64 nodes, depth at most 8, field paths
  at most 5 dotted segments of `[A-Za-z0-9_-]+`.

Example — large ZarinPal orders:

```json
{"and": [
  {">=": [{"var": "amount_irr"}, 1000000]},
  {"==": [{"var": "channel"}, "zarinpal"]}
]}
```

## The closed action set

Six actions, no others. An unknown kind is rejected at rule creation
**and** again by the executor (defense in depth — a row written by hand
with an unknown kind cannot execute). Adding a kind is a design
decision, not a configuration change.

| Action | Parameters | Effect |
|---|---|---|
| `create_draft` | `{"document": "journal", "description"?, "lines": [{"account_code", "debit" \| "credit", "description"?}]}` | Creates a draft journal in the engine. At least two lines; exactly one side per line; amounts are whole Rial — literals or `{"var": "path"}` resolved from the event facts. Account codes resolve through the company's chart (an unknown code fails the execution, never a default account). |
| `post_if_policy_allows` | `{"draft_kind": "journal"?, "draft_id"?, "draft_id_field"?}` | Posts the draft — **only if the 3-layer policy gate allows it** (see below). The draft resolves from `draft_id`, from the payload field named by `draft_id_field`, or from the execution context (the draft the event already produced). No draft resolves → skipped, never invented. |
| `notify` | `{"recipients": [str], "channels": ["in_app", "email", "sms"]?, "subject", "message"}` | Notifies recipients. `{{dotted.path}}` placeholders in subject/message render from the event facts. See "Notification delivery" below. |
| `assign` | `{"assignee", "review_item_id"?}` | Assigns the review item (from the params, else the execution context). |
| `tag` | `{"tag", "review_item_id"?}` | Tags the review item (deduplicated). |
| `set_field` | `{"target": "review_item", "field": "severity", "value": "info" \| "warning" \| "critical", "review_item_id"?}` | Sets a whitelisted field on the review item. |

Review-queue actions skip (never fail) when there is no target item,
when the item does not exist in the tenant, or when the item is
terminal (resolved/written-off). Every mutation appends a US-552 audit
entry attributed to the rule (`actor: "rule:<rule-id>"`).

**Not in v1** (documented, not silently missing): invoice drafts
(`create_draft` needs party/address resolution the engine does not
expose yet), invoice posting (no invoice finalize use case exists
yet), and `set_field` on draft documents (the engine exposes no draft
update). Each is a named follow-up, not a hidden gap.

## Priority + match mode

Rules are evaluated in priority order within a rule set — one tenant's
rules for one event type. The set's match mode is explicit and stored
in `rule_set_configs` (default `first_match`):

- `first_match` (the Xero pattern): stop at the first matching rule.
- `all_matches`: run every matching rule.

`POST /rules/evaluate` accepts a `match_mode` override for one
evaluation. One rule's failure never breaks the others: a raising
action is recorded as a `failed` execution and evaluation continues.

## The policy-gate guarantee

**No rule can auto-post without passing the 3-layer policy gate (F3).**
`post_if_policy_allows` evaluates the gate through
`PolicyGateServiceAdapter` (the US-503 registry gate — the same gate
the event pipeline uses, via `RegistryPolicyGate`) before anything is
posted:

- Gate denies → decision `blocked_by_policy_gate`, outcome
  `draft_kept`. The draft stays a draft, exactly like a manual post
  blocked by the gate.
- No gate wired, no installation context, no company, no event date,
  or no amount → the post fails closed (denied before the gate is
  even asked, or denied by the adapter's own fail-closed layers).
- Gate evaluation raises (e.g. Tryton unreachable) → `failed`
  execution; nothing is posted.
- Gate allows → the draft is posted through `DraftPostingPort`, and
  the adapter verifies the draft belongs to the rule's company first
  (a rule must never post another company's draft).

The gate verdict (policy version + every layer's result) is recorded
on the execution, so every blocked post is auditable.

## Dry-run

`POST /rules/{rule_id}/dry-run` (or `/rules/dry-run-definition` for an
unsaved definition) tests a rule against a past inbox event
(`event_id` — the payload is loaded and normalized exactly like the
pipeline sees it, Toman→Rial included) or a sample payload
(`event_type` + `payload`, amounts in whole Rial).

Dry-run has **zero side effects**: no effect port is ever called (no
drafts, no posts, no notifications, no review-queue writes). The one
exception is the policy gate — gate evaluation is a read-only
decision, so a dry-run of a post action evaluates it and reports the
verdict (`gate_evaluated`, `would_post`, layers) in the result. A
disabled rule can be dry-run: test before activation (§6.4).

Every dry-run is audit-logged like a live execution, with
`mode=dry_run` and decision `would_execute` / `not_matched`.

## The audit log (§6.7)

`rule_executions` is the automation decision audit log for rules.
Every dry-run and every live execution of a matched rule appends a
row: rule id, event id, decision, outcome, plus the evidence in
`details` (gate verdicts, created draft ids, posted refs,
notification channel results). The log is append-only; executions
survive their rule's deletion (`rule_id` SET NULL). A live evaluation
that matches nothing records nothing — there is no execution to audit.

Decisions: `not_matched`, `would_execute` (dry-run), `executed`,
`blocked_by_policy_gate`, `skipped`, `failed`. Outcomes are
machine-readable codes (`draft_created`, `posted`, `draft_kept`,
`notified`, `assigned`, `tagged`, `field_set`, `no_draft`,
`no_review_item`, `no_company_context`, `port_not_wired`, `error`, …).

## Notification delivery (v1 honesty note)

The `notify` action executes through `NotificationPort`; the v1
production adapter (`LoggingNotificationAdapter`) reports per-channel
results on the execution record:

- `in_app` — `recorded`: the execution log is the durable in-app
  record until the rules UI (US-562) surfaces a notifications feed.
- `email` — `skipped`: no SMTP backend exists yet (phased design
  §2.6). The notification is recorded, never claimed sent.
- `sms` — `unsupported`: the channel lights up with US-564 through
  this same port (the schema already accepts it).

A skipped channel is visible on the execution, never silent.

## REST endpoints

| Endpoint | Scope | Notes |
|---|---|---|
| `GET /rules` | `rules:read` | List (filters: `event_type`, `enabled`), priority order. |
| `POST /rules` | `rules:manage` | Create (201; validated before storage). |
| `GET /rules/{rule_id}` | `rules:read` | One rule. |
| `PUT /rules/{rule_id}` | `rules:manage` | Update (only given fields change). |
| `DELETE /rules/{rule_id}` | `rules:manage` | Delete (204; executions survive). |
| `POST /rules/{rule_id}/enable` · `/disable` | `rules:manage` | Enable/disable. |
| `POST /rules/{rule_id}/dry-run` | `rules:manage` | Dry-run a stored rule. |
| `POST /rules/dry-run-definition` | `rules:manage` | Dry-run an unsaved definition. |
| `POST /rules/evaluate` | `rules:manage` | Evaluate the enabled rules against one live event (past event or sample payload). Matched rules EXECUTE. |
| `GET /rules/executions` | `rules:read` | History (filters: `rule_id`, `event_id`, `limit`). |
| `GET /rules/executions/{execution_id}` | `rules:read` | One execution. |
| `GET /rules/{rule_id}/executions` | `rules:read` | One rule's history. |
| `GET /rules/sets` · `PUT /rules/sets/{event_type}` | `rules:read` / `rules:manage` | Rule-set match modes. |

Errors: unknown rule → 404 `RULE_NOT_FOUND`; invalid definition →
422 `RULE_INVALID`; unknown execution → 404
`RULE_EXECUTION_NOT_FOUND`; unknown past event → 404
`EVENT_NOT_FOUND`; missing/conflicting event input → 400.

## Live execution: the pipeline hook (deferred)

US-561 ships the engine, the audit log, and the live trigger
(`POST /rules/evaluate` + `RuleEngineService.evaluate_event`). The
automatic hook — evaluating rules after the event pipeline finishes
an event — is deliberately **not** wired into `EventPipelineService`
in this story: the pipeline's `_finish_processed` does not carry the
normalized event/mapping at every call site, and touching the
pipeline risks the US-501 suite. The hook is a small, well-defined
follow-up: call `evaluate_event` from the pipeline (or the worker)
with a `RuleExecutionContext` built from the normalized event
(installation, resolved company, event date, channel) plus the
pipeline's draft ref (`draft_kind`/`draft_engine_id`) and any review
item the event filed. Until then, live execution runs through
`/rules/evaluate` (operators, US-565's bank rules) and the documented
seam.

## The management UI (US-562)

`یکپارچه‌سازی‌ها ← گردش‌کارها` (`/integrations/workflows`,
`apps/admin/features/rules/`): a Persian RTL page over the REST
endpoints above — the frontend never touches the database. Three
tabs: the rules list, the execution history + audit log (all rules,
newest first, filterable by rule), and the rule-set match modes
(one `first_match` / `all_matches` select per event type).

- **List.** Every rule shows its status (فعال/غیرفعال), its owner
  badge (below), its event type, a one-line Persian summary of its
  conditions and action, its priority, and its last execution
  (decision + Jalali time, from one `GET /rules/executions` fetch
  shared with the history tab) — or «هنوز اجرا نشده است».
- **Create/edit.** The event picker offers only the twelve canonical
  types; the condition builder edits the DSL as rows (field +
  operator + value, combined with «و»/«یا»); the action picker
  offers only the closed six kinds, each with its own parameter
  fields. Amounts are typed in تومان and converted to whole Rial
  at the boundary. A stored condition tree the builder cannot edit
  (`not`, nesting, a var on the right) opens as raw JSON — the form
  never silently rewrites a rule into a shape it did not have.
- **Test before activation.** The form's «آزمایش پیش از ذخیره»
  section dry-runs the unsaved definition
  (`POST /rules/dry-run-definition`); the detail dialog dry-runs
  the stored rule and can live-evaluate it. Both take a past event
  (by id) or a sample payload (amount in تومان, channel, party,
  plus optional extra JSON). The dry-run result is rendered in
  Persian from the rule's own action — the backend's English
  `would_do` is never shown — with the gate verdict (for a post
  action) read from the details' booleans. A disabled rule can be
  dry-run: test before activation (§6.4).
- **Live evaluate.** The detail dialog's «اجرای زنده» runs
  `POST /rules/evaluate`, which executes *every* matching enabled
  rule of the event type per the set's match mode — not just this
  rule — and the panel says so before running, and notes when this
  rule did not run.
- **History.** The detail dialog shows the rule's own executions
  (`GET /rules/{id}/executions`); the history tab shows all of
  them. Each row carries its mode (زنده/آزمایشی), decision,
  outcome, Jalali time, and expandable details: known keys get
  Persian labels (gate verdict, draft refs, per-channel delivery),
  the backend's English free text is never rendered raw, and
  unknown keys still show (raw key + JSON value) so no evidence
  disappears.

### Ownership: user-defined vs system

Every rule carries an owner badge. v1 has no seeded system rules —
every rule comes through the API with the operator client as
`created_by` — so in practice every badge reads «تعریف کاربر»;
a null or `system` creator reads «سیستمی», and the distinction is
rendered (and forward-compatible) from day one.

### Learned mappings are suggestions only

B2 learned mappings (US-514/US-552) enter the engine as
**suggestions only, never as auto-rules** — the UI states this on
the page (with a link to the review queue, where suggestions are
confirmed) and never presents a learned mapping as an active
automation. A suggestion becomes automatic only through the US-552
promotion path (3 confirmed suggestions), never by appearing here.

### Bank rules are W3 rules (US-565)

User-defined bank auto-coding rules are W3 rules: `when
<statement-line pattern> → suggest <account>` — the same
Suggest → Auto shape (threshold 3 confirmed suggestions, human
promotion), but a separate store (`bank_rules`, Alembic `0039`)
and a separate management page: بانک و صندوق ← قواعد تطبیق
(`/banking/bank-rules`), linked from this page. They fire at
reconcile priority 3, ahead of the B2 learned rules, and an
auto-coded line still goes through the tie-out guard before
anything posts; a wrong auto-code can be undone from the statement
detail page. Full model, pattern language, promotion, undo and
endpoints: `docs/bank-rules.md`.

## Tests

- `tests/unit/test_rules_conditions.py` — the DSL (operators, dates,
  missing fields, type mismatches, limits, no-eval safety).
- `tests/unit/test_rules_engine.py` — matching/priority/modes, the
  closed action set, dry-run zero side effects, the policy-gate
  guarantee (allow/deny/unwired/failing/no-company/no-draft).
- `tests/unit/test_rules_service.py` — CRUD validation, match modes,
  dry-run + live evaluation through the service, execution history.
- `tests/interfaces/test_rest_rules.py` — the endpoints (scopes,
  404/422s, dry-run against a past event with Toman→Rial
  normalization, live evaluate, history, OpenAPI listing).
- `apps/admin/features/rules/rules-data.test.ts` (US-562) — the
  condition builder round-trip (rows ⇄ DSL tree), action
  descriptions + validation, rule ⇄ form conversion, ownership,
  the audit-log labels (English free text never rendered raw),
  and the dry-run sample-payload builder.
- `apps/admin/lib/api-client.test.ts` (US-562) — the method-count
  test now pins 246 methods (231 + the 15 rules methods).
- `apps/admin/e2e/rules.spec.ts` (US-562) — one rules journey
  (create disabled → test-before-save → dry-run → enable → live
  evaluate → execution in history → delete), resilient to the
  shared CI DB (unique stamp, no figure assertions on
  pre-existing data); `e2e/responsive.spec.ts` covers
  `/integrations/workflows` at 320/390px.
