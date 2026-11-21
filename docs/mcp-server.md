# MCP server (US-601)

The HesabYar MCP server (design-eng spec §13) exposes approved
accounting tools, reports, resources and prompts to compatible AI
clients and agents. It is another interface to the application layer,
parallel to the REST API — not a second ledger, not a second auth
system, not a second implementation of any report.

The implementation lives in `src/hesabyar/interfaces/mcp/`; the tests
in `tests/mcp/`.

## Status

US-601 (this document) is the server core: SDK v2, both transports,
REST-grade auth + scopes + audit, and the service wiring. US-602
wired the 14 read-only tools of spec §13.4 into `create_mcp_server`
(`tools_core.register_core_tools`). US-603 adds the five read-only
tools for the Phase 5 reports (see [Tools](#tools) below),
registered from `src/hesabyar/interfaces/mcp/tools_phase5.py` via
`register_phase5_tools(server, context)` and wired in `main()`.
US-604 adds the six resources + six prompts (see below), wired in
`main()` via `register_resources` / `register_prompts`. US-605 adds
the four draft-only mutation tools of spec §13.5 (see [Tools](#tools)
below), wired in `main()` via `register_draft_tools`. US-606 adds
the authorized-clients registry + admin UI + the full-surface
conformance suite (see [Client management](#client-management-us-606)
and [Tests](#tests) below), and resolves the US-604 follow-ups:
template-aware resource-scope enforcement in the middleware,
`company_id` on resource/prompt audit rows, live-Tryton reads for
the engine-backed resources, and prompt↔tool integration against
the real tools.

## Protocol and SDK

- Target: **MCP 2026-07-28**, on the official Python MCP SDK v2
  (`mcp` 2.x, locked to 2.3.0 in `uv.lock`; Python 3.12 baseline).
- The server is the SDK's `MCPServer` (`mcp.server.mcpserver`) —
  `FastMCP` was renamed to `MCPServer` in SDK v2, and the server code
  follows the installed 2.3.0 API (verified from site-packages).
- Transports: **stdio** (local developer) and **Streamable HTTP**
  (remote/production). No legacy HTTP+SSE implementation exists and
  none will be added (spec §13.2).
- The MCP process shares the HesabYar Python package and runs as a
  separate process/container; the system stays a modular monolith
  (spec §0 rule 2).

## Architecture

```text
Claude / ChatGPT-compatible host / IDE agent / local agent
                          |
                   MCP Streamable HTTP (or stdio)
                          |
                          v
                 HesabYar MCP Server (MCPServer)
                          |
             HesabYarMcpMiddleware (auth + scopes + company + audit)
                          |
                          v
               Application Query/Command Services (McpServices)
                          |
          Reporting / Standards / Tryton Adapter
                          |
                          v
                  Tryton + HesabYar DB
```

- `server.py` — `create_mcp_server(context)`: builds the `MCPServer`
  with the token verifier, auth settings, lifespan (which yields the
  `McpContext`), and the middleware. Also the CLI entry point.
- `context.py` — `McpContext`: settings, session factory, scope
  registry, audit writer, transport name, and the optional stdio
  bootstrap client. Tool handlers reach it as
  `ctx.request_context.lifespan_context`.
- `auth.py` — `HesabYarTokenVerifier` (the SDK `TokenVerifier` over
  the API-key service), identity claims, and the company-grant check.
- `middleware.py` — `HesabYarMcpMiddleware`: transport-agnostic
  enforcement + audit (see below).
- `registry.py` — required scopes per tool/resource/prompt. Tools
  register through `register_hesabyar_tool` so a scope record cannot
  be forgotten; resources and prompts through the
  `register_hesabyar_resource` / `register_hesabyar_prompt` siblings
  in `resources.py` / `prompts.py`.
- `tools_core.py` — the 14 read-only tools of spec §13.4 (US-602),
  registered by `register_core_tools` (see below).
- `tools_phase5.py` — the five Phase 5 report tools (US-603),
  registered by `register_phase5_tools`.
- `tools_drafts.py` — the four draft-only mutation tools (US-605),
  registered by `register_draft_tools`.
- `resources.py` — the six US-604 resource templates (all
  company-scoped) and their readers over `McpServices`.
- `prompts.py` — the six US-604 deterministic prompt templates over
  the read-only tools.
- `guards.py` — shared handler guards: lifespan-context extraction
  plus identity + scope checks.
- `provenance.py` — the shared §13.7 provenance builder
  (`build_provenance`): every financial result carries it.
- `audit.py` — `AiAccessAuditWriter`: one `ai_access_audit` row per
  call.
- `services.py` — `McpServices`: the per-call application-service
  container, mirroring REST's `get_*_service` functions.
- `http.py` — the Streamable HTTP app (DNS-rebinding protection,
  body-size + rate limits) and the uvicorn runner.
- `rate_limit.py` — fixed-window rate limiting.
- `errors.py` — typed MCP (JSON-RPC) errors.
- `provenance.py` — the shared §13.7 provenance builder
  (`build_provenance`); every financial tool result carries its block.
- `tools_phase5.py` — the US-603 read-only Phase 5 report tools (see
  [Tools](#tools)).

## Tools (US-602)

The 14 read-only tools of spec §13.4, in `tools_core.py`:

| Tool | Scope | Service (same as REST) |
| --- | --- | --- |
| `list_companies` | `company:read` | `CompanyQueryService` (+ tenant mapping join, as REST) |
| `get_accounting_summary` | `reports:read` | `ReportQueryService` × 4 (composed — see below) |
| `get_trial_balance` | `reports:read` | `ReportQueryService.get_trial_balance` |
| `get_general_ledger` | `ledger:read` | `ReportQueryService.get_general_ledger` |
| `get_balance_sheet` | `reports:read` | `ReportQueryService.get_balance_sheet` |
| `get_income_statement` | `reports:read` | `ReportQueryService.get_income_statement` |
| `get_cash_flow` | `reports:read` | `ReportQueryService.get_cash_flow` |
| `get_receivables_aging` | `reports:read` | `ReportQueryService.get_notes_balance` (receivable) |
| `get_payables_aging` | `reports:read` | `ReportQueryService.get_notes_balance` (payable) |
| `get_bank_reconciliation_status` | `banking:read` | `BankQueryService.reconciliation_status` |
| `get_tax_compliance_status` | `compliance:read` | `TaxPolicyQueryService` + `MoadianQueryService` (composed) |
| `validate_financial_statement` | `standards:validate` | `ReportValidationService` (same as `POST /report-validation-runs`) |
| `explain_accounting_rule` | `standards:read` | `StandardsQueryService.get_standard` |
| `list_pending_review_items` | `workflow:read` | `ReviewQueueQueryService.list_items` |

Every tool returns a JSON envelope `{"result": <REST-shaped payload>,
"provenance": <§13.7 block>}`. The `result` is serialized with the
SAME REST response schema as the matching endpoint
(`interfaces.rest.schemas`), so MCP and REST figures agree by
construction — the equality tests in `test_mcp_tools_core.py` prove
it per tool. Amounts are integer Rial values or safe decimal strings
(`model_dump(mode="python")` + an ISO/decimal-string JSON hook —
`mode="json"` would turn Decimals into binary floats). `match_rate`
and `age_hours` are ratios/durations, not amounts, and serialize
exactly as REST serializes them.

Provenance (§13.7) is attached to every company-scoped financial
result. Two tools carry none, by design: `list_companies` (a
multi-company listing — no single company or period) and
`explain_accounting_rule` (a registry lookup with no company and no
figures).

Known gaps (no matching service/endpoint exists — the tools wire the
closest existing service and say so, instead of inventing a report):

- **Accounting summary**: no summary service or endpoint exists. The
  tool composes four `ReportQueryService` reads (trial balance,
  balance sheet, income statement, cash flow) and carries each
  component's §11.1 freshness block in `result.sources`.
- **Receivables/payables aging**: no party open-balance aging exists
  (Tryton `aged_balance` over the receivable/payable accounts is not
  wired anywhere). The tools return NOTES aging (cheque report 7)
  and say so in `result.coverage_note` (`aging_scope`:
  `notes_receivable_only` / `notes_payable_only`).
- **Tax compliance status**: no single compliance-status service
  exists. The tool composes the tax-policy registry (active versions
  effective on the as-of date) with Moadian (profiles +
  document/submission counts). Tax policy is a global registry;
  Moadian data is tenant-scoped, not company-scoped
  (`result.scope_note`).
- **Validation runs persist**: `validate_financial_statement`
  persists a validation run + trial-balance snapshot, exactly like
  the REST endpoint it mirrors. That is a validation record, not a
  posting: nothing is journaled, finalized or submitted.
- **Review-item company filter**: items are filtered by the HesabYar
  API company id (the `engine_companies` row id). Items filed with
  the engine id as `company_id` will not match.

`workflow:read` is a new scope (added to `KNOWN_SCOPES` in US-602;
also added to the dev-key mint script's scope list, since the CI
Playwright job mints its key from it).

## Running it

```bash
# stdio (local developer) — needs one API key at startup
HESABYAR_MCP__STDIO_API_KEY='hya_live_...' python -m hesabyar.interfaces.mcp --transport stdio

# Streamable HTTP (remote/production) — per-request auth
python -m hesabyar.interfaces.mcp --transport streamable-http --port 8002
```

The stdio process authenticates its key once at startup and runs as
that key's grant; it fails fast (exit 1) when the key is invalid,
revoked or expired, when it lacks `mcp:connect`, and (exit 2) when no
key is given at all. Prefer the environment variable over `--api-key`
(a CLI argument is visible in the process list).

### Configuration

All settings live in `McpSettings` (`HESABYAR_MCP__` prefix):

| Setting | Env var | Default |
| --- | --- | --- |
| Bind address | `HESABYAR_MCP__HOST` | `127.0.0.1` |
| Port | `HESABYAR_MCP__PORT` | `8002` (8000 is Tryton, 8001 is REST) |
| Endpoint path | `HESABYAR_MCP__PATH` | `/mcp` |
| Public URL (resource/audience) | `HESABYAR_MCP__PUBLIC_URL` | `http://127.0.0.1:8002/mcp` |
| Allowed Host values | `HESABYAR_MCP__ALLOWED_HOSTS` (JSON list) | `["127.0.0.1:8002", "localhost:8002"]` |
| Allowed Origin values | `HESABYAR_MCP__ALLOWED_ORIGINS` (JSON list) | `[]` |
| Max request body | `HESABYAR_MCP__MAX_REQUEST_BODY_BYTES` | `4194304` (4 MiB) |
| Rate limit / key / minute | `HESABYAR_MCP__RATE_LIMIT_PER_MINUTE` | `120` |
| Session idle timeout (s) | `HESABYAR_MCP__SESSION_IDLE_TIMEOUT_SECONDS` | `1800` |
| Max sessions | `HESABYAR_MCP__MAX_SESSIONS` | `10000` |
| stdio API key | `HESABYAR_MCP__STDIO_API_KEY` | _(none)_ |
| TLS certificate / key | `HESABYAR_MCP__TLS_CERTFILE` / `HESABYAR_MCP__TLS_KEYFILE` | _(none)_ |

`PUBLIC_URL` MUST match the URL clients use to reach the server: it
is the RFC 8707 resource/audience the token verifier binds tokens to,
and a mismatch rejects every request. Production must set the real
public URL, the real allowed host, and TLS (see below).

## Tools

### US-603 — read-only tools for the Phase 5 reports

Every tool takes `company` (required, checked against the grant —
see below) and returns a JSON string: the report figures plus the
§13.7 provenance block (`provenance`). Amounts are integer Rial;
rates and quantities serialize as decimal strings, never binary
floating-point. Results are serialized with the same pydantic
response models REST uses, so an MCP result and the matching REST
response for the same data and period carry the same figures; the
equality tests in `tests/mcp/test_mcp_tools_phase5.py` prove it end
to end. List outputs are capped at `limit` rows (default 100, max
100, matching REST's page max); totals always cover ALL matching
rows, and each list carries its `returned_*_count` so a caller can
tell when it was truncated.

| Tool | Scope | Arguments | Reuses |
| --- | --- | --- | --- |
| `get_settlement_status` | `settlement:read` | `company`, `channel_key?`, `state?`, `sales_period?` (Jalali `YYYY/MM`), `due_from?`, `due_to?` (Gregorian), `include_tranches?` (default true), `limit?` | US-521 `SettlementQueryService.list_tranches` |
| `get_cheque_status` | `cheques:read` | `company`, `kind?` (`received`/`issued`), `state?`, `due_within_days?` (default 30, 0–365), `include_collateral?` (default true), `limit?` | US-531/US-534 `ChequeQueryService` (`list_cheques` with `purpose=payment`, `list_in_clearing_aging`, `collateral_summary`) |
| `get_packaging_costs` | `packaging:read` | `company`, `order_ref?`, `include_consumptions?` (default true), `include_shipment_costs?` (default true), `limit?` | US-515 `PackagingQueryService` (`get_order_cost`, `list_consumptions`, `list_shipment_costs`) |
| `get_cashbox_status` | `cashbox:read` | `company`, `date_from`, `date_to` (Gregorian, required), `cash_box_id?`, `include_boxes?` (default true), `include_sessions?` (default true), `limit?` | US-516 `CashBoxQueryService` (`cashbox_report`, `list_cash_boxes`, `list_sessions`) |
| `get_cash_forecast` | `reports:read` | `company`, `date_from`, `date_to` (Gregorian, required) | US-553 `CashForecastQueryService.get_cash_forecast` |

Notes per tool:

- **Settlement**: totals are expected/confirmed/received/outstanding
  net + variance over all matching tranches, with counts by tranche
  state. Settlement data is installation-scoped; the `company`
  argument still gates the call (middleware + handler check) and
  identifies the company in the provenance block.
- **Cheques**: the `registry` and `due_soon` sections are commercial
  cheques ONLY (`purpose=payment` at the source — the §2.2
  segregation seam); `collateral` is the memorandum-only report 6
  summary, never mixed into the commercial totals. `due_soon` is
  due within `[today, today + due_within_days]`. `in_clearing_aging`
  flags stale cheques; nothing is auto-bounced.
- **Packaging**: `order_cost` appears only when `order_ref` is given.
  Its goods/revenue figures come from the Tryton analytic lines and
  are read only when the order has a cost centre; without one they
  are honestly 0. The order-cost packaging sum counts only
  consumptions with an engine stock move (the service's own rule).
- **Cashbox**: `report` is the cash-box vs sales variance report;
  `boxes` carry live book balances; `sessions` the counting
  sessions in the period with their variances. Engine-backed (live
  Tryton reads).
- **Cash forecast**: the full view (items, Jalali-month calendar,
  30/60/90/120-day horizon, totals, source counts) plus the §13.7
  provenance built from the view's own §11.1 freshness block.
  Collateral cheques appear in NEITHER column; the forecast
  (پیش‌بینی) column is empty until an expected-sales source exists.
  Engine-backed (live Tryton reads).

Error shapes: an anticipated failure (unknown channel, unknown
state/kind, bad `limit`, reversed date range) is an `is_error`
result carrying the message; an authorization failure (missing
scope, company outside the grant) is a JSON-RPC protocol error
(`MCP_ERROR_FORBIDDEN`), like every other tool.

Wiring: `register_phase5_tools(server, context)` registers all five
through `register_hesabyar_tool` (scope record created with the
tool). The server entry point (`main` in `server.py`) calls it — one
line per story module, the same way US-604 wires its resources +
prompts — so `create_mcp_server` stays bare for the US-601
conformance tests (only the stdio smoke test, which spawns the real
server, sees the registered tools).

### US-605 — draft-only mutation tools

The four tools of spec §13.5, in `tools_drafts.py`. Every tool
creates a DRAFT object only and returns its ID, validation findings
and the next human action; each result also carries the §13.7
provenance block and the draft as read back from the engine (or, for
the tax document, the document metadata — the payload itself is NOT
echoed back: it is the caller's own input, and a Moadian payload can
carry float rates such as `vra`, which MCP results never contain;
the `payload_hash` lets the caller verify integrity instead).
Amounts are integer Rial; invoice quantities serialize as decimal
strings, never binary floating-point. Results are serialized with
the same pydantic response models REST uses.

| Tool | Scope | Arguments | Reuses |
| --- | --- | --- | --- |
| `prepare_journal_draft` | `ledger:draft:create` | `company`, `journal_date` (Gregorian), `lines` (≥ 2, each `account_id` + one of `debit_irr`/`credit_irr`, optional `description`/`party_id`/`maturity_date`), `description?`, `journal_id?` | US-202 `JournalDraftService` (`create_draft`, `get_draft`, `validate_draft`) |
| `prepare_invoice_draft` | `invoice:draft:create` | `company`, `invoice_type` (`out`/`in`), `party_id`, `invoice_date` (Gregorian), `lines` (≥ 1, each `account_id` + `unit_price_irr` + optional `quantity`/`description`), `description?`, `reference?`, `journal_id?`, `account_id?`, `invoice_address_id?` | US-204/US-205 `InvoiceDraftService` (`create_draft`, `get_invoice`, `validate_invoice_draft`) |
| `prepare_reconciliation_suggestions` | `banking:reconcile` | `company`, `statement_id`, `window_days?` (default: installation setting, else 2) | US-514 `BankReconcileService.reconcile` (+ `McpServices.bank_reconcile`, wired with the W3 bank-rules repository) |
| `prepare_tax_document_draft` | `compliance:draft:create` | `company`, `profile_id`, `payload` (Moadian invoice JSON per Table 7), `invoice_id?` | US-404 `MoadianDocumentService.create_document` |

Notes per tool:

- **Journal / invoice**: the draft goes through the SAME validation
  as a dashboard-created draft (the §8.2 / invoice invariants,
  enforced by the same command handlers and the same engine
  adapter), is read back, and is validated again for the findings.
  The auto-post policy gate is NOT consulted: the gate governs
  whether a pipeline event may auto-post, and MCP has no post path
  at all — the draft-by-default verdict (ADR 0003 decision 1) is
  structural, not a gate decision that could flip.
- **Reconciliation**: the same three-priority matcher the
  dashboard's reconcile button runs (same learned + user rules,
  same tie-out guard). High-confidence lines are matched; the rest
  come back as suggestions or unmatched with a review item each.
  The statement is NEVER validated or posted — it stays a draft for
  a human to validate in the engine, and unmatched deposits NEVER
  auto-post to revenue. The run is attributed to the MCP client
  (actor `mcp:<client_id>`) on the review items it files.
- **Tax document**: the payload is validated against Moadian
  instruction Table 7 (an invalid payload is refused with the full
  problem list, never stored) and the document is created in
  `draft` status. No submission is created, no approval is
  recorded, nothing is queued, and nothing is sent to Moadian — the
  tool needs no `moadian:submit` scope and has no code path to any
  submission (spec §0 rule 8, §19.11).

SAFETY — the never-post guarantee (spec §13.5): a tool NEVER posts
the journal, finalizes the invoice, approves a reconciliation, or
submits anything to Moadian. The guarantee is structural — the
module never calls `post_draft`, `finalize_invoice`, `post_invoice`,
any statement validation, `approve_submission` or
`queue_submission` — and the tests in
`tests/mcp/test_mcp_tools_drafts.py` prove it behaviourally per tool
(the draft exists AND its state is still `draft` AND no
posted/finalized/approved/submitted object resulted). There is NO
`approve_and_post` tool in this release: a model's natural-language
statement is NEVER approval, no tool accepts free-text approval,
and no tool path treats a prompt argument as confirmation. The
draft is attributed to the MCP client in the audit log (one
`ai_access_audit` row per call: client, key, tool, company,
outcome).

Transactions: the handler commits the Postgres session after a
successful mutation (the reconciliation and tax tools write
Postgres rows through repositories that flush but never commit —
the caller owns the transaction, like every REST route) and rolls
back on failure. Engine-side writes (journal/invoice drafts) are
committed by the engine itself. Audit-failure limitation (see
`middleware.py`): a failed audit write fails the call closed and
withholds the result, but the tool has already run — a failed audit
does NOT roll back the draft. The draft survives as an ordinary
unposted draft for a human to review; it is never posted by the
failure path.

Error shapes: an anticipated failure (unbalanced lines, unknown
account/party/profile/statement, bad invoice type, invalid Moadian
payload with its Table 7 problems) is an `is_error` result carrying
the message; an authorization failure (missing scope, company
outside the grant) is a JSON-RPC protocol error
(`MCP_ERROR_FORBIDDEN`), like every other tool.

Wiring: `register_draft_tools(server, context)` registers all four
through `register_hesabyar_tool` (scope record created with the
tool). The server entry point (`main` in `server.py`) calls it —
one line per story module, the same way US-603 wires its tools — so
`create_mcp_server` stays bare for the US-601 conformance tests.
No new scopes were needed: all four already exist in
`KNOWN_SCOPES`, and no migration was needed (drafts use the
existing journal/invoice/reconciliation/Moadian tables).

## Authentication and authorization

The MCP server uses the **same** API-key + scope system as REST
(`hya_live_` keys, HMAC-SHA-256 + pepper, `Authorization: Bearer` —
see `docs/api-key-auth.md`). There is no second credential system.

- **Streamable HTTP**: every request is authenticated by the SDK's
  bearer backend against `HesabYarTokenVerifier`, which verifies the
  key through `ApiKeyService` (and commits `last_used_at`, like
  REST). Missing/invalid/revoked/expired keys get one generic 401;
  keys without `mcp:connect` get 403 `insufficient_scope` — both
  before any MCP handler runs.
- **stdio**: one key, authenticated at startup (see above).
- The verified identity travels in the `AccessToken` claims (tenant,
  key id, key prefix, client name, installation, company
  restriction) so handlers rebuild the exact `AuthenticatedClient`
  REST would have built. A token whose claims do not carry a HesabYar
  identity establishes no identity (fail closed).
- **Base scope**: every MCP key needs `mcp:connect` (spec §14.2
  catalog) in addition to the scopes of the tools it calls. It is
  enforced twice: by the HTTP layer (`AuthSettings.required_scopes`)
  and by the middleware (so stdio and in-memory transports get the
  same guarantee).
- **Tool scopes**: each tool declares its exact required scopes in
  the `ToolRegistry`; the middleware denies calls from keys missing
  any of them. There is no wildcard scope.
- **Company check (SAFETY)**: a tool call whose arguments carry a
  `company` value has it resolved tenant-scoped (row id or engine
  id, mirroring REST) and checked against the key's company
  restriction. A caller-provided company ID is **never** proof of
  authorization — a company outside the grant fails closed, and a
  company of another tenant looks exactly like a nonexistent one.
  Resource reads and prompt renders apply the same check to the
  `company` URI segment / argument, in their handlers (see
  Resources / Prompts below).

## Resources (US-604, spec §13.6)

Six resource templates, all company-scoped. Every URI carries a
`{company}` segment (the HesabYar company row id or the engine
company id — both resolve), and every read resolves that segment
against the authenticated grant before any data is touched. There is
no unfiltered all-company resource, and the shape of the URIs makes
one impossible to publish by accident
(`test_no_unfiltered_resource` proves every template carries the
company parameter). Payloads are JSON (`application/json`) and carry
a §13.7 provenance block; amounts are integer Rial or safe decimal
strings.

| URI template | Name | Scopes |
| --- | --- | --- |
| `hesabyar://companies/{company}/chart-of-accounts` | `chart-of-accounts` | `account:read` |
| `hesabyar://companies/{company}/fiscal-periods` | `fiscal-periods` | `company:read`, `reports:read` |
| `hesabyar://companies/{company}/standards` | `standards` | `standards:read` |
| `hesabyar://companies/{company}/tax-policy` | `tax-policy` | `tax:read` |
| `hesabyar://companies/{company}/report-snapshots/{run_id}` | `report-snapshot` | `reports:read` |
| `hesabyar://companies/{company}/integration-compliance-status` | `integration-compliance-status` | `compliance:read`, `integrations:read` |

- **chart-of-accounts** — active chart metadata only: code, name,
  type, parent code, postable/closed/party-required flags.
- **fiscal-periods** — available fiscal years and periods (Gregorian
  + Jalali dates) plus company metadata (name, currency, national id).
- **standards** — versioned standards summaries with verified source
  references (revision, effective dates, verification status, source
  URL, snapshot URI, SHA-256) and registry coverage. Standards are
  installation-wide reference data; the company segment gates
  *access*, and the payload says so.
- **tax-policy** — the active tax-policy profile: currently active
  versions with applicability metadata (rule / performance / filing
  year, effective dates, verification status) and source references.
  Parameter values are decimal strings, never floats.
- **report-snapshot** — one approved report snapshot per read
  (trial-balance document + validation outcome). Bounded access: the
  snapshot is served only when it belongs to the named company — a
  snapshot of another company (or with no company) fails closed with
  the same message as a missing run. A snapshot is "approved" when
  its validation run is valid; failed runs are still served, with the
  failure stated plainly.
- **integration-compliance-status** — channels with active contracts
  (fee percent as a decimal string), the company's bank connections,
  stores, Moadian profile/document status counts, standards coverage
  and the active tax-policy version count. No credential material
  anywhere: bank connections expose only `has_credentials`, stores
  only the `has_*` flags.

Scope enforcement note: since US-606 the registry matches concrete
resource URIs against their templates (`ToolRegistry.match_resource_uri`,
`{param}` segments match one path segment), so the middleware
enforces resource scopes centrally, before the handler runs. The
handlers keep their own scope check as defence in depth — the
registration helper builds the handler from a pure reader, so the
check cannot be forgotten.

## Prompts (US-604, spec §13.6)

Six deterministic prompt templates over the read-only tools. The host
flow is: call the read-only tools first (US-602/US-603), then pass
their results — unchanged, as JSON arguments — to `prompts/get`. The
template renders the results verbatim inside a Persian framing; it
never invents figures, never computes counts/deltas/percentages, and
its boilerplate contains no digits at all, so a render can be proven
to cite only tool-provided data. Every figure the model cites must
come with the provenance block of the tool result it came from.

| Prompt | Arguments | Scopes | Tools rendered |
| --- | --- | --- | --- |
| `monthly-close-review` | `company`, `period`, `trial_balance`, `income_statement`, `pending_review_items?` | `reports:read`, `review:read` | `get_trial_balance`, `get_income_statement`, `list_pending_review_items` |
| `explain-unusual-balance-movement` | `company`, `account`, `period`, `general_ledger`, `trial_balance?` | `ledger:read`, `reports:read` | `get_general_ledger`, `get_trial_balance` |
| `summarize-overdue-receivables` | `company`, `as_of`, `receivables_aging` | `reports:read` | `get_receivables_aging` |
| `review-unreconciled-bank-activity` | `company`, `period`, `bank_reconciliation_status` | `banking:read` | `get_bank_reconciliation_status` |
| `validate-financial-statements` | `company`, `period`, `validation`, `balance_sheet?`, `income_statement?` | `reports:read`, `standards:validate` | `validate_financial_statement`, `get_balance_sheet`, `get_income_statement` |
| `compare-month-over-month` | `company`, `current_period`, `prior_period`, `current_income_statement`, `prior_income_statement`, `current_balance_sheet?`, `prior_balance_sheet?` | `reports:read` | `get_income_statement`, `get_balance_sheet` |

(`?` marks an optional argument.) Each prompt handler resolves the
`company` argument against the grant and checks the prompt's scopes;
a malformed tool-result argument is a clear error, never a render
around invented data. Note: `monthly-close-review` uses `review:read`
for the review-queue data — `prd.json` names the underlying tool's
scope `workflow:read`, which does not exist in the §14.2 catalog
(`KNOWN_SCOPES`); see the US-602 note in UNVERIFIED below.

OAuth: the server is its own issuer for the self-issued API-key
credentials (no separate OAuth authorization server is implemented in
US-601). If OAuth is ever added, access tokens are bound to the
intended MCP resource/audience: the verifier sets
`AccessToken.resource` to the public URL and
`AuthSettings.validate_token_resource` makes the backend refuse
tokens issued for another resource.

## Security properties (spec §13.8)

- Authenticate every request (except the `initialize` handshake
  itself, `ping`, and notifications, which carry no data access).
- Scope + company restrictions apply to every tool call, on every
  transport, enforced centrally in the middleware — a tool author
  cannot forget them. Resources and prompts apply the same company
  check in their handlers (the `company` URI segment / argument is
  resolved against the grant); since US-606 the middleware enforces
  resource scopes centrally too (template-aware matching), with the
  handler check kept as defence in depth.
- No unfiltered all-company resource exists: every resource URI
  names one company, and the no-unfiltered-resource test proves it.
- Origin + Host validated (DNS-rebinding protection, always on);
  request-size limits (413), rate limits (429 + `Retry-After`),
  session idle timeout.
- TLS: production MUST serve behind TLS (directly via
  `TLS_CERTFILE`/`TLS_KEYFILE`, or via a terminating proxy) — API
  keys travel as Bearer tokens and must never cross a network in
  cleartext.
- One service's bearer token is never forwarded to another service:
  the MCP server authenticates the caller, then calls Tryton with its
  own configured engine credential.
- No API keys in tool arguments, URLs, logs or bundles. The raw
  secret is never retained past verification (the `AccessToken.token`
  field holds the visible prefix, not the secret). No secrets in tool
  descriptions or results.
- No direct PostgreSQL access from any MCP code (spec §0 rule 4):
  tools call application services only, and the services use the
  repositories/ports. No raw SQL tool for MCP or LLM clients
  (spec §19 rule 5).
- Rate limiting is per-process (in-memory). The MCP server is a
  single-process deployment (one installation per store, spec §15.1);
  a multi-worker deployment would need a shared limiter.

## Audit

Every `tools/call`, `resources/read` and `prompts/get` lands one row
in `ai_access_audit` (migration 0040): client, key prefix, tool,
kind, company, scopes carried, outcome (`allowed` / `denied` /
`error`), error code, request id, transport, client name/version,
timestamp. Denied and failed attempts are audited too — a denied call
is the audit event that matters most.

The audit stores **no** tool arguments, **no** hidden
chain-of-thought or model reasoning, and **no** secrets — only the
key's visible prefix. List operations (`tools/list`, ...) and
`initialize`/`ping` are not audited: they access no company data.

An audit-write failure fails the call closed: the result is withheld
and the client gets an internal error, rather than serving an
unaudited call. Known limitation: the tool has already run by then,
so a failed audit does not roll back side effects — US-601 tools are
read-only, and the US-605 draft tools document this in their own
safety notes.

Since US-606, resource and prompt audit rows carry `company_id`
too: the middleware extracts the company from the resource URI's
`{company}` segment (via the template match) or from the prompt's
`company` argument, and resolves it tenant-scoped for the audit
record — including on denied calls, so a denied call still names
the company it tried to touch. The lookup is audit-only, never
authorization: enforcement stays in the handlers and in
`_checked_company` (tools).

## Client management (US-606)

The authorized-clients registry (`mcp_authorized_clients`,
migration 0041) is a **management layer over the existing API-key
system, not a second credential system**: an MCP client is one
`api_clients` row + one ordinary `hya_live_` key (issued through
`ApiKeyService`, HMAC-SHA-256 + pepper, never stored in cleartext)
+ one registry row pointing at the current key. The MCP server
never consults the registry; registry membership gates nothing at
call time — authentication, scopes and the company grant are
enforced from the key, exactly as for REST.

- **Scopes**: a client carries a scope set; `mcp:connect` is
  mandatory (without it the key cannot open a session). The admin
  UI offers only read scopes — the MCP server is read-only by
  design, and no MCP client should ever hold a write scope.
- **Company grant**: one company (row id or engine id) or NULL for
  every company of the tenant — the same model as the key system.
- **Rotation**: issues a new key with the same scopes/grant/expiry
  and revokes the old one at once; the new secret is returned
  exactly once. Rotating a revoked client is a 409.
- **Revocation**: revokes the key at once; idempotent (a second
  revoke is a no-op and writes no second audit row).
- **Secret**: returned only by create/rotate, only in the response
  to the request that issued it. List/get never carry it.

REST endpoints (all under `/api/v1/mcp/*`, all requiring
`api_keys:manage` — managing an MCP client IS managing an API
key, so no new scope was added):

| Endpoint | Behaviour |
| --- | --- |
| `GET /mcp/clients` | List the tenant's clients, newest first. `include_revoked` (default false) shows revoked ones. |
| `POST /mcp/clients` | Create (201) + the one-time secret. Missing `mcp:connect` → 400 `INVALID_SCOPES`; unknown scope → 400 `UNKNOWN_SCOPE`; unknown company → 404 `COMPANY_NOT_FOUND`; duplicate name → 409 `MCP_CLIENT_NAME_TAKEN`. |
| `GET /mcp/clients/{id}` | One client. Other tenant → 404 `MCP_CLIENT_NOT_FOUND`. |
| `POST /mcp/clients/{id}/rotate` | New key + the one-time secret. Revoked → 409 `MCP_CLIENT_REVOKED`. |
| `POST /mcp/clients/{id}/revoke` | Revoke (optional `reason`). Idempotent. |
| `GET /mcp/audit` | The `ai_access_audit` log, keyset-paginated. Filters: `client`, `kind`, `outcome`, `tool`, `company`, `from`, `to`, `sort`. |

Every mutation writes TWO audit records in the same transaction:
a REST `audit_events` row (`mcp.client.create|rotate|revoke`,
actor = the operating key) and an `ai_access_audit` management row
(`kind='management'`, `transport='rest'`, subject = the MCP client
acted upon) — so the admin audit tab shows lifecycle events
alongside tool calls. The admin UI lives at
`/ai/mcp-clients` (هوش مصنوعی و MCP ← کلاینت‌های MCP):
list/create/revoke/rotate + the audit log view. See
`docs/mcp-operations.md` for the operator guide.

Known limitation: create/rotate carry no idempotency key (the admin
UI disables the button while acting). A retried rotate would issue
a second key; `Idempotency-Key` support for these two endpoints is
a follow-up.

## Services

`McpServices` (per call, built from the call's session) wires the
same application services REST uses — company/account/fiscal/report/
payment/statement queries over the Tryton adapter, standards,
tax-policy, Moadian, integrations, bank, packaging, cashbox,
settlement, cheques, installments, cash-forecast and review-queue
queries over the SQLAlchemy repositories, plus the journal/invoice/Moadian draft
services US-605 needs. All services are synchronous: tool handlers
must run service calls via `asyncio.to_thread`, never directly on
the event loop. The Tryton adapter is built lazily and closed when
the call ends.

## Tests

`tests/mcp/` (run in CI; no test runs on the dev VM by standing
order):

- `test_mcp_server.py` — conformance over in-memory streams
  (initialize, list tools/resources/prompts, tool call) + the stdio
  subprocess smoke test.
- `test_mcp_auth.py` — the verifier + middleware enforcement,
  including the company SAFETY property.
- `test_mcp_audit.py` — audit row shape, no secrets in audit/logs,
  audit failure fails closed.
- `test_mcp_http.py` — Streamable HTTP: 401/403 gates, Origin/Host
  validation, body-size and rate limits, and the real SDK
  `streamable_http_client` end to end.
- `test_mcp_services.py`, `test_mcp_rate_limit.py` — container
  construction + limiter unit tests.
- `test_mcp_tools_core.py` (US-602) — conformance per tool (narrow
  input schemas, registry scope records), scope denial per tool,
  cross-company fail-closed (restricted key, other tenant, unknown
  company), argument validation error shapes, live success paths,
  provenance completeness, no-binary-floats, and MCP-vs-REST
  equality per report tool (same company + period → same figures,
  modulo per-call timestamps). The live tests use the Adapter Test
  company and skip when Tryton is down.
- `test_mcp_tools_phase5.py` (US-603) — conformance, empty states,
  per-tool scope denial, cross-company fail-closed, error shapes,
  provenance completeness, and MCP-vs-REST equality for all five
  tools. The engine-backed equality tests (cashbox, cash forecast)
  skip when no trytond is reachable, like the contract tests.
- `test_mcp_tools_drafts.py` (US-605) — conformance per tool
  (narrow input schemas, registry scope records, no
  `approve_and_post` tool), per-tool scope denial, cross-company
  fail-closed, error shapes, provenance completeness, and the
  never-post SAFETY property per tool (draft exists AND state is
  still `draft` AND no posted/finalized/approved/submitted object
  resulted, read back from the engine / database). The tax-document
  tests are Postgres-only; the journal/invoice/reconcile tests need
  live Tryton and skip when it is unreachable, like the contract
  tests. Engine rows created by the live tests are deleted in
  teardown.
- `test_mcp_resources.py` — US-604: registration + the
  no-unfiltered-resource property, scope denial per resource, the
  cross-company SECURITY property (fails closed), other-tenant and
  unknown-company denial, end-to-end reads of the session-backed
  resources, snapshot bounded access (seeded run + snapshot), and
  reader unit tests against stub services.
- `test_mcp_prompts.py` — US-604: prompt definitions + registration,
  renderer unit tests proving figure containment (every figure in a
  render appears in the fixture tool results; empty results render
  no figures), and harness tests for list, scope denial,
  cross-company denial, and end-to-end renders.
- `test_mcp_conformance.py` + `conformance.py` (US-606) — the
  full-surface conformance suite: protocol version 2026-07-28, the
  exact production surface (23 tools, 6 resource templates, 6 prompts),
  narrow input schemas per tool, capabilities, a raw host-style
  JSON-RPC client (no SDK client) over memory streams and over the
  real stdio subprocess, full-surface scope denial, audit rows
  carrying `company_id`, live-Tryton reads for chart-of-accounts
  and fiscal-periods, and prompt↔tool integration — all six prompts
  rendered against REAL tool results with figure containment proven.
  `make_full_surface_server` builds the factory + phase5 tools +
  draft tools + resources + prompts; it never re-registers the core
  tools. The expected surface is stated explicitly (exact equality,
  not a subset) — drift fails loudly.
- `tests/interfaces/test_rest_mcp.py` (US-606) — the client
  management endpoints end to end through the FastAPI app (real
  DB): create (secret once, company grant, 409/400/404 shapes),
  list/get, rotate (old key dies), revoke (idempotent), audit list
  (filters + keyset pagination), scope enforcement (403/401),
  cross-tenant invisibility.

The harness (`tests/mcp/harness.py`) connects a real SDK
`ClientSession` to the real server over the SDK's in-memory streams.
Tests are sync functions driving the async harness with
`asyncio.run()` (no pytest-asyncio in this repo). The fixture echo
tool is registered through the production helper with real scopes.

## UNVERIFIED

- **Prompt↔tool integration**: resolved by US-606 —
  `test_mcp_conformance.py` renders all six prompts against REAL
  US-602/US-603 tool results and proves figure containment.
- **Engine-backed resource reads end-to-end**: resolved by US-606 —
  the conformance suite reads chart-of-accounts and fiscal-periods
  against live Tryton (Adapter Test company).
- **`workflow:read` scope**: resolved by US-602 — `workflow:read`
  is now in `KNOWN_SCOPES` (+ `mint-dev-key.py`), and
  `list_pending_review_items` uses it. The `monthly-close-review`
  prompt uses `review:read`.
- **Middleware resource-scope matching**: resolved by US-606 —
  the registry matches concrete URIs against templates and the
  middleware enforces resource scopes centrally (handlers keep
  their check as defence in depth).
- **Live client interop**: the server is exercised with the official
  SDK client (in-memory, stdio, Streamable HTTP) and with a raw
  host-style JSON-RPC client (in-memory + the real stdio
  subprocess) in CI, but NOT yet against a real AI host
  (Claude/ChatGPT-compatible host, IDE agent). Do not claim live
  host interop without demonstrating it.
- **OAuth flow**: no OAuth authorization server is implemented; the
  audience-binding notes above describe the verifier's behaviour for
  the API-key credentials in use today.
- **Production TLS termination**: the uvicorn TLS flags are
  configured but no production deployment has exercised them yet.
- **Multi-worker rate limiting**: see the per-process note above.
