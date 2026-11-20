# HesabYar REST API (US-101)

FastAPI application, versioned under `/api/v1`, with generated OpenAPI
docs. Spec: design-eng §6.2 (layout), §7.6 (installation status), §9.1
(API principles).

## Run it

```bash
cd ~/workspace/projects/hesabyar
export PATH="$HOME/.local/bin:$PATH"
export HESABYAR_DATABASE_URL="postgresql+psycopg://hesabyar:hesabyar_pass@127.0.0.1:5432/hesabyar_test"

# Port 8001: port 8000 is the Tryton server in the local dev environment.
uv run uvicorn hesabyar.interfaces.rest.app:app --port 8001
```

OpenAPI docs: `http://127.0.0.1:8001/docs` (Swagger UI),
`http://127.0.0.1:8001/openapi.json`.

## Layout

```text
src/hesabyar/interfaces/rest/
  app.py            create_app() factory + module-level `app` for uvicorn
  dependencies.py   shared deps: settings, health service, admin guard,
                    auth, company resolution, query services
  errors.py         RFC 9457 problem-details envelope + exception handlers
                    + domain/application error translation
  middleware.py     X-Request-ID middleware
  pagination.py     cursor pagination + allowlisted filtering/sorting (§9.1)
  services.py       HealthService: read-only DB / Tryton / Alembic probes
  routers/health.py /health, /readiness, /installation
  routers/companies.py /companies, /companies/{company_id}
  routers/accounting.py /accounting/accounts, /accounting/accounts/{account_id},
                        /accounting/fiscal-years (+ {fiscal_year_id}),
                        /accounting/fiscal-periods (+ {period_id}),
                        /accounting/setup-status
  routers/journals.py   /accounting/journal-drafts (+ {draft_id}, /validate, /post),
                        /accounting/journals/{journal_id}/reverse
  routers/invoices.py   /invoices (+ {invoice_id}, /drafts, /validate)
  routers/payments.py   /payments (+ {payment_id})
  routers/bank.py       /bank/statements (+ {statement_id}, /import-status,
                        /reconcile)
  routers/audit.py      /audit/events (+ {event_id})
  idempotency.py        Idempotency-Key guard (claim/replay/conflict) for mutations
  mutations.py          Shared idempotent-mutation helpers (complete/fail +
                        audit) for new routers; the journal router keeps its
                        own private copies (US-202 predates this module)
  schemas/health.py Pydantic v2 response models
  schemas/companies.py CompanyResponse
  schemas/accounting.py AccountResponse, FiscalYearResponse,
                        FiscalPeriodResponse, SetupStatusResponse
  schemas/journals.py   JournalDraftResponse, PostedJournalResponse,
                        ValidationResponse (+ request schemas)
  schemas/invoices.py   InvoiceResponse (+ request schemas)
  schemas/payments.py   PaymentResponse
  schemas/statements.py BankStatementResponse, StatementImportStatusResponse
  schemas/audit.py      AuditEventResponse
  schemas/pagination.py PaginatedResponse envelope
```

Routes are thin: no accounting rules in routes, no domain imports of
FastAPI/SQLAlchemy (guarded by
`tests/interfaces/test_rest_health.py::test_domain_layer_has_no_framework_or_infrastructure_imports`).
Company/account routes call the US-104 application query services
(`CompanyQueryService`, `AccountQueryService`) — never the adapter or
Tryton directly — and map domain views (`CompanyView`, `AccountView`)
onto Pydantic response schemas. Fiscal routes call the US-203 services
(`FiscalQueryService`, `SetupStatusService`) the same way. Invoice,
payment and statement routes call the US-205 services
(`InvoiceDraftService`, `PaymentQueryService`,
`BankStatementQueryService`) the same way; audit routes read the
`audit_events` table through `AuditReader`
(`infrastructure/persistence/postgres/audit.py`). No Tryton record or
ORM object ever appears in a response.

## Endpoints

| Endpoint | Auth | Behaviour |
| --- | --- | --- |
| `GET /api/v1/health` | none | Liveness: `{status: "ok", service, version, environment}`. No dependency is checked. |
| `GET /api/v1/readiness` | none | 200 `ready` when the HesabYar DB answers `SELECT 1` and the Tryton adapter pings; otherwise 503 `not_ready` with per-check statuses. |
| `GET /api/v1/installation` | admin (placeholder, see below) | Alembic revision (current + head + up-to-date), Tryton release + activated modules, DB connectivity. Read-only: it never runs migrations or any DDL. |
| `GET /api/v1/companies` | `company:read` | Cursor-paginated list of the tenant's companies (see below). |
| `GET /api/v1/companies/{company_id}` | `company:read` | One company. Unknown company → 404 `COMPANY_NOT_FOUND`. |
| `GET /api/v1/accounting/accounts` | `account:read` | Cursor-paginated chart of accounts for one company (see below). |
| `GET /api/v1/accounting/accounts/{account_id}` | `account:read` | One account of the company in scope. Unknown account → 404 `ACCOUNT_NOT_FOUND`. |
| `GET /api/v1/accounting/fiscal-years` | `ledger:read` | Cursor-paginated fiscal years for one company, newest first. |
| `GET /api/v1/accounting/fiscal-years/{fiscal_year_id}` | `ledger:read` | One fiscal year of the company in scope. Unknown year → 404 `FISCAL_YEAR_NOT_FOUND`. |
| `GET /api/v1/accounting/fiscal-periods` | `ledger:read` | Cursor-paginated fiscal periods for one company, earliest first. |
| `GET /api/v1/accounting/fiscal-periods/{period_id}` | `ledger:read` | One fiscal period of the company in scope. Unknown period → 404 `FISCAL_PERIOD_NOT_FOUND`. |
| `GET /api/v1/accounting/setup-status` | `ledger:read` | Read-only setup state of the company for the dashboard wizard. |
| `POST /api/v1/accounting/journal-drafts` | `ledger:draft:create` | Create a draft journal (201). Requires `Idempotency-Key`. |
| `GET /api/v1/accounting/journal-drafts` | `ledger:read` | Cursor-paginated list of the company's drafts, newest first. |
| `GET /api/v1/accounting/journal-drafts/{draft_id}` | `ledger:read` | One draft. Unknown draft → 404 `LEDGER_JOURNAL_NOT_FOUND`. |
| `PATCH /api/v1/accounting/journal-drafts/{draft_id}` | `ledger:draft:update` | Update a draft's date, description or lines (lines replace wholesale). Requires `Idempotency-Key`. Updating a posted journal → 409 `LEDGER_POSTED_IMMUTABLE`. |
| `POST /api/v1/accounting/journal-drafts/{draft_id}/validate` | `ledger:draft:update` | Validate a draft against §8.2 (never mutates). |
| `POST /api/v1/accounting/journal-drafts/{draft_id}/post` | `ledger:post` | Post a draft (atomic). Requires `Idempotency-Key`. |
| `POST /api/v1/accounting/journals/{journal_id}/reverse` | `ledger:reverse` | Reverse a posted journal (201). Requires `Idempotency-Key`. |
| `GET /api/v1/invoices` | `invoice:read` | Cursor-paginated invoices for one company, newest first. Filters: `type`, `state`. |
| `GET /api/v1/invoices/{invoice_id}` | `invoice:read` | One invoice. Unknown invoice → 404 `LEDGER_INVOICE_NOT_FOUND`. |
| `POST /api/v1/invoices/drafts` | `invoice:draft:create` | Create a draft invoice (201). Requires `Idempotency-Key`. |
| `POST /api/v1/invoices/{invoice_id}/validate` | `invoice:draft:create` | Validate a draft invoice (never mutates; never finalizes). |
| `GET /api/v1/payments` | `payment:read` | Cursor-paginated payments for one company, newest first. Filters: `kind`, `state`. |
| `GET /api/v1/payments/{payment_id}` | `payment:read` | One payment. Unknown payment → 404 `LEDGER_PAYMENT_NOT_FOUND`. |
| `GET /api/v1/bank/statements` | `banking:read` | Cursor-paginated bank/cash statements for one company, newest first. Filter: `state`. |
| `GET /api/v1/bank/statements/import-status` | `banking:read` | Read-only import summary: per-state counts, newest statement, matched/unmatched lines. |
| `GET /api/v1/bank/statements/{statement_id}` | `banking:read` | One statement. Unknown statement → 404 `LEDGER_STATEMENT_NOT_FOUND`. |
| `POST /api/v1/bank/statements/{statement_id}/reconcile` | `banking:reconcile` | Reconcile a statement — currently 501 `NOT_IMPLEMENTED` (matching is a later story). |
| `GET /api/v1/audit/events` | admin (`settings:manage`) | Cursor-paginated audit trail, newest first. Filters: `actor`, `action`, `company`, `from`, `to`. |
| `GET /api/v1/audit/events/{event_id}` | admin (`settings:manage`) | One audit event. Unknown event → 404 `AUDIT_EVENT_NOT_FOUND`. |
| `GET /api/v1/packaging/locations` | `packaging:read` | Stock locations for one company. |
| `GET /api/v1/packaging/settings` | `packaging:read` | Per-company packaging settings (defaults when never configured). |
| `PUT /api/v1/packaging/settings` | `packaging:manage` | Save packaging settings (location/category/root ids are verified against the engine). |
| `GET /api/v1/packaging/materials` | `packaging:read` | Cursor-paginated materials with live stock, reorder point and coverage per row. |
| `POST /api/v1/packaging/materials` | `packaging:manage` | Create a material (engine product + catalog row, 201). No category configured → 422 `PACKAGING_SETUP_REQUIRED`; FIFO without the module → 422 `PACKAGING_FIFO_UNAVAILABLE`. |
| `GET /api/v1/packaging/materials/{id}` | `packaging:read` | One material with live stock, reorder point and coverage. |
| `PATCH /api/v1/packaging/materials/{id}` | `packaging:manage` | Update catalog fields (name/code/active). |
| `PUT /api/v1/packaging/materials/{id}/reorder-point` | `packaging:manage` | Set min/target order point at the warehouse. |
| `GET /api/v1/packaging/materials/{id}/coverage` | `packaging:read` | Days-of-cover for one material. |
| `POST /api/v1/packaging/consumptions` | `packaging:manage` | Record a consumption (201; replay returns 200 with the same record — idempotent by order/shipment/material). |
| `GET /api/v1/packaging/consumptions` | `packaging:read` | Cursor-paginated consumptions. Filter: `order_ref`. |
| `POST /api/v1/packaging/orders/{order_ref}/cost-center` | `packaging:manage` | Create the order's analytic cost centre (201; duplicate → 409). |
| `GET /api/v1/packaging/orders/{order_ref}/cost` | `packaging:read` | Full order cost: goods + packaging + shipment, margin. |
| `GET /api/v1/packaging/shipment-costs` | `packaging:read` | Cursor-paginated shipment costs. Filter: `order_ref`. |
| `POST /api/v1/packaging/shipment-costs` | `packaging:manage` | Record the three shipment figures (201). |
| `POST /api/v1/packaging/price-calculator/quote` | `packaging:read` | Approximate price quote (read-only; never auto-applies to an invoice). |
| `GET /api/v1/packaging/carrier-rates` | `packaging:read` | Cursor-paginated carrier rates. |
| `POST /api/v1/packaging/carrier-rates` | `packaging:manage` | Create a carrier rate (201). |
| `PATCH /api/v1/packaging/carrier-rates/{id}` | `packaging:manage` | Update a carrier rate. |
| `POST /api/v1/packaging/counts` | `packaging:manage` | Open a materials count (201). |
| `GET /api/v1/packaging/counts` | `packaging:read` | Cursor-paginated counts. |
| `GET /api/v1/packaging/counts/{id}` | `packaging:read` | One count, refreshed from the engine. |
| `POST /api/v1/packaging/counts/{id}/lines` | `packaging:manage` | Set a counted quantity on one line. |
| `POST /api/v1/packaging/counts/{id}/confirm` | `packaging:manage` | Confirm a count (all lines must be counted). |
| `POST /api/v1/packaging/counts/{id}/cancel` | `packaging:manage` | Cancel a count. |
| `POST /api/v1/packaging/supply-check` | `packaging:manage` | Run the supply check now (reads the wizard's results, files reorder items). |

(Phases 4–5 endpoints — standards, tax, Moadian, compliance, events,
integrations, review queue — are documented in their own docs; see
`docs/packaging.md` for the packaging detail.)

## Companies and accounts (US-105)

### Company identity

A company has two ids, and both are accepted wherever a `{company_id}`
path parameter or a `company` query parameter is expected:

- the HesabYar API id — the `engine_companies` row id (a UUID) that
  scopes the company to the caller's tenant. This is what responses
  return as `id`;
- the engine id — the integer id the accounting engine knows the
  company by. Responses return it as `engine_id`.

Resolution is always tenant-scoped: a company with no `engine_companies`
row in the caller's tenant looks exactly like a company that does not
exist (404). `GET /companies` therefore lists only companies mapped to
the caller's tenant (a company-restricted key sees only its own
company), even though the engine itself may hold more companies.

### Which company accounts endpoints use

Accounts are company-scoped. The company comes from the `company` query
parameter — or, when the parameter is absent, from the API key's company
restriction. A key with no restriction and no parameter gets a typed 400
`COMPANY_REQUIRED`. A `company` the key is restricted away from gets
403; an unknown `company` gets 404 `COMPANY_NOT_FOUND`.

`GET /accounting/accounts/{account_id}` resolves the account from the
chart of the company in scope (the port has no single-account lookup):
an account id that is not in that chart — including an account of
another company — is a typed 404 `ACCOUNT_NOT_FOUND`.

### Fiscal years, periods and setup status (US-203)

Fiscal endpoints are company-scoped exactly like accounts: the company
comes from the `company` query parameter, or from the API key's company
restriction when the parameter is absent (unrestricted key + no
parameter → typed 400 `COMPANY_REQUIRED`). All five require the
`ledger:read` scope.

- `GET /accounting/fiscal-years` — newest first by default
  (`sort=start_date` ascending gives oldest first; `sort=name` also
  supported). Filter: `state` (exact: `open`, `closed`, `locked`).
- `GET /accounting/fiscal-years/{fiscal_year_id}` — one year; a year of
  another company looks exactly like a year that does not exist (404
  `FISCAL_YEAR_NOT_FOUND`).
- `GET /accounting/fiscal-periods` — earliest first by default.
  Filters: `fiscal_year` (engine id — only that year's periods; a year
  that does not belong to the company is a 404
  `FISCAL_YEAR_NOT_FOUND`), `state`, `type` (`standard` / `adjustment`).
- `GET /accounting/fiscal-periods/{period_id}` — one period (404
  `FISCAL_PERIOD_NOT_FOUND`).
- `GET /accounting/setup-status` — read-only setup state for the future
  dashboard wizard: whether the company has a chart installed
  (`chart_installed`, `account_count`), which template it came from
  (`chart_template` — currently always `null`: the engine records the
  template on every account, but the port does not expose it yet; see
  `docs/setup-wizard.md` §5), fiscal years present
  (`fiscal_year_present`, `fiscal_year_count`, `open_fiscal_year`), open
  periods (`open_period_present`, `open_period_count`), and `ready`
  (chart + fiscal year + open period). Everything is derived from engine
  reads; nothing is invented.

Year and period responses carry Gregorian dates (`start_date`,
`end_date`) plus their Jalali rendering (`start_date_jalali`,
`end_date_jalali`, `YYYY/MM/DD`) for the Persian-first dashboard, and
the engine `state` plus a computed `is_open` flag.

### Journal drafts and posted journals (US-202)

Journal routes call the US-202 `JournalDraftService`
(`application/commands/journals.py`), which owns the draft → validate
→ post → reverse workflow over the engine port — Tryton remains the
only posting authority. Routes contain no accounting rules.

**Company scoping.** Draft creation takes its company from the request
body (`company_id` — HesabYar row id or engine id, 404
`COMPANY_NOT_FOUND` / 403 when restricted away). Every other endpoint
reads the company from the engine record itself: a draft of a company
with no mapping row in the caller's tenant looks exactly like a draft
that does not exist (404 `LEDGER_JOURNAL_NOT_FOUND`).

**Draft read-back.** The port declares `get_journal_draft` (it joined the
port in the US-202 follow-up, collapsing the application-layer
`JournalEngine` protocol back to `AccountingEnginePort`), so the service
reads drafts through the port like every other operation.

**List + update.** `GET /accounting/journal-drafts` lists the company's
drafts only (posted journals never appear), cursor-paginated, newest
first by default. `PATCH /accounting/journal-drafts/{draft_id}` updates
a draft's date, description or lines — only present fields change, and
`lines` replaces the draft's lines wholesale; an empty body is a 400
`LEDGER_INVALID_JOURNAL`, and updating a posted journal is a 409
`LEDGER_POSTED_IMMUTABLE`. PATCH requires an `Idempotency-Key` like the
other mutations, but does not yet claim it: a retried PATCH re-executes
the update instead of replaying a stored response (safe by
construction — the same payload lands in the same state), and updates
write no audit event yet.

**Idempotency (§10.4).** `Idempotency-Key` is required on create, post
and reverse (missing/blank/over-long key → 400
`IDEMPOTENCY_KEY_REQUIRED`). The key is claimed before the engine call
(the unique constraint on `(installation_id, operation,
idempotency_key)` arbitrates races) and the completed response is
stored in `idempotency_records`: same key + same payload hash replays
the stored response verbatim with no second engine call; same key +
different payload → 409 `IDEMPOTENCY_CONFLICT`; same key while the
first request is still in flight → 409 `IDEMPOTENCY_IN_PROGRESS`. A
failed mutation releases its claim so the key can be retried. Pending
claims expire after 10 minutes (a crashed request's row is treated as
stale); completed records are kept 24 hours. Known window: if the
process dies between the engine confirming a post and the response
being stored, a retry surfaces 409 `LEDGER_POSTED_IMMUTABLE` instead
of the replay — never a silent duplicate posting.

**Audit (§14.3).** Every create/post/reverse writes an `audit_events`
row — success and failure alike — with tenant, installation, API
client, action (`ledger.draft.create` / `ledger.post` /
`ledger.reverse`), resource, company external id, Tryton reference
(`account.move:<id>`), payload hash, UTC timestamp, request id, and
the idempotency key (in `metadata_after`). Failures carry the stable
`error_code`; a failed reversal post also records the surviving
`reversal_draft_id` in `metadata_after` for recovery.

**Reversal recovery.** Reversal is two engine calls (create the
reversal draft, then post it), not one transaction. When the post
fails, the service retries the post of the surviving reversal draft
once: a retry that posts returns the reversal; a retry that raises
`LEDGER_POSTED_IMMUTABLE` means the first post actually landed and
only its confirmation was lost, so the reversal is returned as well;
any other retry failure re-raises the original error with
`reversal_draft_id` intact. Reversal does not reconcile
reconcile-account lines (no REST path — see `docs/adapter.md`).

### Invoices, payments and bank statements (US-205)

Invoice, payment and statement routes call the US-205 services
(`application/commands/invoices.py`, `application/queries/payments.py`,
`application/queries/statements.py`), which own the workflows over the
engine port — Tryton remains the only posting authority. Routes contain
no accounting rules.

**Company scoping.** List endpoints take their company from the
`company` query parameter (or the API key's company restriction, like
the accounts endpoints). Draft creation takes its company from the
request body. Get/validate/reconcile read the company from the engine
record itself: a document of a company with no mapping row in the
caller's tenant looks exactly like a document that does not exist
(404 `LEDGER_INVOICE_NOT_FOUND` / `LEDGER_PAYMENT_NOT_FOUND` /
`LEDGER_STATEMENT_NOT_FOUND`).

**Invoices.** `POST /invoices/drafts` creates a draft invoice (201)
and requires an `Idempotency-Key` with the same claim/replay/conflict
contract as journal creation; every creation writes an `audit_events`
row (action `invoice.draft.create`, Tryton reference
`account.invoice:<id>`), success and failure alike.
`POST /invoices/{invoice_id}/validate` is the read-only HesabYar
check — it never mutates the invoice and never calls the engine's
`validate_invoice` transition (finalizing an invoice is a later
story). Both draft creation and validation sit behind
`invoice:draft:create` (the draft-workflow scope; there is no
`invoice:draft:update` scope in the §14.2 catalog, and journal
validation analogously sits behind `ledger:draft:update` rather than
`ledger:read`). Quantities are Decimals and serialize as decimal
strings in JSON (spec §9.3); line amounts (quantity × unit price)
must be integral Rial. Invalid `type` / `state` filters are a 400
`LEDGER_INVALID_INVOICE` (the port validates filters before
delegating).

**Payments** are read-only through this API (list/get behind
`payment:read`): creating and processing payments is a later story.
Invalid `kind` / `state` filters are a 400 `LEDGER_INVALID_PAYMENT`.

**Bank statements** are read-only through this API (list/get behind
`banking:read`): importing and reconciling statements is a later
story. `GET /bank/statements/import-status` is a read-only summary
derived from the statements the engine already holds — per-state
counts, the newest statement, and how many statement lines are
already matched to an invoice (`related_invoice` set) versus
unmatched. It is not an import operation: `POST
/bank/statements/import` (spec §9.2) needs its own port method.
`POST /bank/statements/{statement_id}/reconcile` is a typed 501 stub
until statement matching lands: it still enforces auth, the
`banking:reconcile` scope, the `Idempotency-Key`, and
statement/company resolution, so its contract is stable when the
operation lands. Invalid `state` filters are a 400
`LEDGER_INVALID_STATEMENT`.

### Audit trail queries (US-205)

`GET /api/v1/audit/events` and `GET /api/v1/audit/events/{event_id}`
are admin-only (`settings:manage` scope) and always tenant-scoped: a
caller only ever sees its own tenant's events, and an event of
another tenant looks exactly like an event that does not exist (404
`AUDIT_EVENT_NOT_FOUND`). Unlike the engine-backed lists, audit
events live in HesabYar's own database, so pagination is true SQL
keyset pagination over `(occurred_at, id)` via `AuditReader` — page
boundaries stay stable while new events arrive, and no page ever
loads the whole table into memory. The cursor encoding is the same
opaque format as the other list endpoints.

Filters (all exact matches, combined with AND): `actor` (the source
API client id), `action` (e.g. `ledger.post`), `company` (HesabYar
row id or engine company id), `from` / `to` (inclusive UTC bounds on
`occurred_at`; ISO datetimes, a naive datetime is read as UTC; an
inverted range is a 400 `INVALID_FILTER`). A company-restricted key
is forced to its own company: with no `company` parameter the filter
is that company, and a parameter naming a different company is 403.
`sort` supports only `occurred_at` (default: newest first;
`sort=occurred_at` gives oldest first). Audit responses never contain
secrets — only the API client id, hashes and evidence pointers.

### Pagination, filtering, sorting (§9.1)

List endpoints return `{items, next_cursor}`. `next_cursor` is `null`
on the last page; pass it back as `cursor` for the next page. Cursors
are opaque keyset cursors (sort key + tiebreaker of the last item), so
page boundaries stay stable while the list changes. Pagination runs over
the in-memory list the use case returned — the company list and a chart
are bounded lists from the engine, not SQL rows.

- `limit`: page size, default 50, max 100.
- `sort`: a declared field, optionally prefixed with `-` for
  descending. Companies: `name` (default), `currency_code`. Accounts:
  `code` (default), `name`, `type`.
- Filters are an allowlist per endpoint. Companies: `name` (substring,
  case-insensitive), `currency_code` (exact). Accounts: `code` (prefix),
  `name` (substring, case-insensitive), `type` (exact), `active`,
  `closed` (booleans).
- Unknown query parameters → typed 400 `INVALID_FILTER`; unknown sort
  fields → typed 400 `INVALID_SORT`; malformed cursors → typed 400
  `INVALID_CURSOR`.

### Error mapping for engine failures

| Situation | Status | Code |
| --- | --- | --- |
| Unknown company / account | 404 | `COMPANY_NOT_FOUND` / `ACCOUNT_NOT_FOUND` |
| Unknown fiscal year / period | 404 | `FISCAL_YEAR_NOT_FOUND` / `FISCAL_PERIOD_NOT_FOUND` |
| Unknown journal draft / posted journal | 404 | `LEDGER_JOURNAL_NOT_FOUND` |
| Unknown invoice / payment / bank statement | 404 | `LEDGER_INVOICE_NOT_FOUND` / `LEDGER_PAYMENT_NOT_FOUND` / `LEDGER_STATEMENT_NOT_FOUND` |
| Unknown party | 404 | `LEDGER_PARTY_NOT_FOUND` |
| Unknown audit event | 404 | `AUDIT_EVENT_NOT_FOUND` |
| Unbalanced / structurally invalid journal | 400 | `LEDGER_UNBALANCED` / `LEDGER_INVALID_JOURNAL` |
| Structurally invalid invoice / payment / statement | 400 | `LEDGER_INVALID_INVOICE` / `LEDGER_INVALID_PAYMENT` / `LEDGER_INVALID_STATEMENT` |
| Journal date in a closed period | 400 | `LEDGER_PERIOD_CLOSED` |
| Posting an already-posted journal | 409 | `LEDGER_POSTED_IMMUTABLE` |
| Updating an already-posted journal | 409 | `LEDGER_POSTED_IMMUTABLE` |
| Missing `Idempotency-Key` on a mutation | 400 | `IDEMPOTENCY_KEY_REQUIRED` |
| Key reused with a different payload | 409 | `IDEMPOTENCY_CONFLICT` |
| Key reused while the first request is in flight | 409 | `IDEMPOTENCY_IN_PROGRESS` |
| Statement reconcile (no port operation yet) | 501 | `NOT_IMPLEMENTED` |
| Engine unreachable (retryable) | 503 | `ENGINE_UNAVAILABLE` |
| Engine failure (auth, forbidden, other) | 502 | same `ENGINE_*` code |
| Engine rejects the request as invalid | 400 | `ENGINE_VALIDATION_FAILED` |

Engine error details are generic — no engine internal ever appears in a
response body.

Tryton release: the REST API exposes no version endpoint and the core
`ir` module reports an empty version, so the release comes from the
documented read-only JSON-RPC call `common.server.version`. This is a
server-level metadata call, not an accounting operation; the adapter's
REST-only rule (docs/adapter.md) is untouched.

## X-Request-ID

Every response carries `X-Request-ID`. A client-supplied id (≤128 chars)
is echoed back; otherwise a UUID is generated. The id is also in the
error envelope's `request_id` field and in a context variable
(`hesabyar.interfaces.rest.middleware.get_current_request_id`) for
logging correlation.

## Error envelope

Every error is RFC 9457 problem details (`application/problem+json`):

```json
{
  "type": "https://hesabyar.local/problems/not-found",
  "title": "Not Found",
  "status": 404,
  "detail": "Not Found",
  "code": "NOT_FOUND",
  "request_id": "…"
}
```

Unhandled exceptions return a generic 500 (`INTERNAL_ERROR`, "An
unexpected error occurred. Please retry later."); the traceback is
logged server-side only. No stack trace, DB URL, credential, or Tryton
internal ever appears in a response.

## Admin guard: scoped API-key auth (US-103)

`/installation` requires an API key with the `settings:manage` scope
(`require_admin` in `interfaces/rest/dependencies.py` — see
`docs/api-key-auth.md` for the full auth reference). Send the key as
`Authorization: Bearer hya_live_…`. Missing/invalid keys get 401, keys
without the scope get 403. The US-101 placeholder admin token
(`HESABYAR_ADMIN_TOKEN`) is gone.

## Configuration

Tryton connection settings live in `AppSettings.tryton`
(`src/hesabyar/infrastructure/config.py`), overridable via
`HESABYAR_TRYTON__BASE_URL`, `HESABYAR_TRYTON__DATABASE`,
`HESABYAR_TRYTON__API_KEY`. Defaults match the local dev install
(docs/dev-setup.md §3-§4); the default API key is the dev-only key and
must be overridden in production.

## Tests

```bash
export HESABYAR_DATABASE_URL="postgresql+psycopg://hesabyar:hesabyar_pass@127.0.0.1:5432/hesabyar_test"
uv run pytest tests/interfaces/test_rest_health.py
```

15 fake-service tests (no servers needed) + 3 live tests against the
real PostgreSQL + trytond, which skip cleanly when either service is
down.

```bash
uv run pytest tests/interfaces/test_rest_companies_accounts.py \
  tests/interfaces/test_rest_companies_accounts_e2e.py
```

Companies/accounts: 27 tests with a faked engine (real app, real auth,
real database — no Tryton needed) + 6 end-to-end tests against the live
Tryton server with real issued keys, which skip cleanly when trytond is
down or the "HesabYar Adapter Test" company is not provisioned.

```bash
uv run pytest tests/unit/test_fiscal_services.py \
  tests/contract/test_fiscal_services.py \
  tests/interfaces/test_rest_fiscal.py \
  tests/interfaces/test_rest_fiscal_e2e.py
```

Fiscal years/periods/setup status: 21 unit tests (faked engine) + 11
contract tests (live Tryton, read-only) + 19 endpoint tests with a
faked engine (real app, real auth, real database) + 6 end-to-end tests
against the live Tryton server with real issued keys, which skip cleanly
when trytond is down or the test company is not provisioned.

```bash
uv run pytest tests/unit/test_journal_draft_service.py \
  tests/interfaces/test_rest_journal_drafts.py \
  tests/interfaces/test_rest_journal_drafts_e2e.py
```

Journals: 28 service unit tests (faked engine, incl. reversal
recovery and draft list/update) + 41 endpoint tests with a faked engine
(real app, real auth, real database — idempotency replay/conflict,
audit rows, draft list/update incl. pagination and scoping) + 11
end-to-end tests against the live Tryton server with real issued keys
(draft → validate → post → reverse, plus live list and PATCH, verified
in Tryton), which skip cleanly when trytond is down or the test company
is not provisioned. Posted e2e moves accumulate in the test company by
design; drafts left behind are deleted in teardown.

```bash
uv run pytest tests/unit/test_invoice_payment_statement_services.py \
  tests/interfaces/test_rest_invoices_payments_statements.py \
  tests/interfaces/test_rest_audit.py \
  tests/interfaces/test_rest_phase2_e2e.py
```

Invoices/payments/statements/audit: 21 service unit tests (faked
engine, incl. import-status counts and the reconcile stub) + 25
endpoint tests with a faked engine (real app, real auth, real database
— idempotency replay/conflict, audit rows, filters, pagination,
company scoping, reconcile 501) + 10 audit endpoint tests (real app,
real auth, real database — filters, SQL keyset pagination, admin-only
access, company-restricted keys) + 6 end-to-end tests against the live
Tryton server with real issued keys, which skip cleanly when trytond
is down or the test company is not provisioned. The Phase 2 exit proof
(`test_phase2_full_cycle_e2e`) walks draft → validate → post →
reverse → audit query and invoice draft → validate through the REST
API, with idempotent replays of the post and the invoice create, and
verifies every mutating step in the audit trail. Posted e2e moves
accumulate in the test company by design; drafts, invoices, payments
and statements left behind are deleted in teardown.
