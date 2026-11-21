# HesabYar Threat Model (US-701)

**Status:** current as of 2026-11-21 (Phase 7, story US-701).
**Scope:** the whole HesabYar system as built through Phase 6 — the REST API,
the MCP server, the event/webhook pipeline, the admin dashboard (Next.js BFF),
the worker, the Tryton adapter, and the credential vault.
**Method:** code review against the spec (design-eng §13.8, §17 phase 7, §19),
the phased-design checklist §1.3 phase 7, and the existing security docs
(`docs/mcp-server.md`, `docs/mcp-operations.md`, `docs/rest-api.md`,
`docs/api-key-auth.md`), followed by targeted hardening and a security test
suite. Every finding from the review is either fixed or explicitly accepted
with a written rationale — see §6. Nothing is left silently open.

**What this document is not:** it does not invent a new auth system. HesabYar
authenticates machines and operators with scoped API keys; there is no human
password login, no JWT session system, and no OAuth server of its own. This
document reviews and hardens what exists.

## 1. System overview and trust boundaries

```
  ┌──────────┐  HTTPS + API key   ┌─────────────────────────────┐
  │ Operator │ ─────────────────▶ │  Admin dashboard (Next.js)  │
  │ browser  │  httpOnly cookie   │  BFF proxy (same origin)    │
  └──────────┘                    └──────────────┬──────────────┘
                                                 │ server-side key
  ┌──────────┐  HTTPS + API key                  ▼
  │ MCP      │ ─────────────────▶ ┌─────────────────────────────┐
  │ client   │  (HTTP transport) │  HesabYar REST API (FastAPI)│
  └──────────┘                   │  + MCP server (same keys,   │
  ┌──────────┐  stdio (local)    │  same scopes, same audit)   │
  │ MCP      │ ─────────────────▶ └──────┬──────────────┬───────┘
  │ client   │                          │              │
  └──────────┘                          ▼              ▼
  ┌──────────┐  HTTPS + API key   ┌──────────┐  ┌──────────────┐
  │ Store /  │  + HMAC webhook    │ Postgres │  │ Tryton 8.2   │
  │ gateway  │ ─────────────────▶ │ (HesabYar│  │ (ledger,     │
  └──────────┘  signed payloads   │  data)   │  │  REST, key)  │
                                 └──────────┘  └──────────────┘
```

Trust boundaries:

1. **Public internet → HesabYar API.** Everything here is untrusted: requests
   arrive over TLS, authenticated with scoped API keys. Only `/health` and
   `/readiness` are unauthenticated (see A8). There is no CORS middleware, so
   browsers block cross-origin calls by default; the dashboard talks to the
   backend only through its same-origin BFF.
2. **HesabYar API → Postgres.** Trusted private network. The database
   password lives in `HESABYAR_DATABASE__URL`. The API never exposes raw SQL
   to callers, and MCP has no raw-SQL tool (spec §19 rule 14).
3. **HesabYar API → Tryton.** Trusted private network, authenticated with a
   Tryton application key sent as a Bearer header. The Tryton client sets
   `trust_env=False` (no proxy interception from environment variables) and
   uses httpx default TLS verification. There is no `verify=False` anywhere
   in the codebase, and HesabYar never touches Tryton's internal database
   directly (spec §19 rule 11).
4. **HesabYar → store / gateway webhooks.** Inbound webhooks are untrusted
   input: they are HMAC-verified when a store secret is configured, and the
   endpoint additionally requires an `integrations:manage` API key. Outbound
   fetches (cheque scan downloads) are SSRF-guarded (F3).
5. **Operator browser → dashboard BFF.** The browser never sees an API key:
   it holds only an opaque session id in an httpOnly cookie. The key lives
   server-side in the BFF's in-memory session store.

## 2. Assets

| Asset | Why it matters |
| --- | --- |
| Scoped API keys (`hya_live_…`) | Bearer credentials for every REST and MCP call; a leaked key is usable until revoked. |
| Credential vault contents | Store API keys, gateway OAuth credentials, SMS provider credentials, webhook secrets — Fernet-encrypted at rest. |
| Vault key + API-key pepper | Both are environment secrets; compromise of either undermines the corresponding store. |
| Tryton application key | Bearer credential for the ledger; grants whatever Tryton allows that key. |
| Financial data (journals, invoices, bank statements, cheques) | The product's core data; company-scoped, tenant-scoped. |
| Webhook secrets | Authenticate inbound store events; a leaked secret lets an attacker forge events — but only with a valid `integrations:manage` API key as well (defence in depth). |
| Gateway OAuth tokens | Short-lived access tokens for SnappPay / DigiPay / TorobPay. |
| Admin BFF sessions | Opaque session ids; stealing one impersonates the operator until the 12h inactivity TTL. |
| Audit trail | Integrity matters: it is the record of who did what, used for dispute and compliance. |

## 3. Token inventory — the US-704 handoff

US-704 (monitoring) builds expiry alerts on this table. **Read it literally:**
a token listed as "no expiry" has no expiry to alert on; the alert surface is
the rows marked with an expiry mechanism.

| Token / secret | Stored where | Expiry | Rotation | Revocation |
| --- | --- | --- | --- | --- |
| HesabYar API key (`hya_live_…`) | DB `api_keys`: 16-char prefix + `v1:` HMAC-SHA-256 (peppered) hash only; the secret is shown once at issuance and never stored | Optional per-key `expires_at` (NULL = no expiry); checked on every auth | `rotate_key` / MCP-client rotate / store API-key rotate — old key revoked immediately, expiry never extended | `revoked_at` + reason, checked on every auth |
| Tryton application key | Env `HESABYAR_TRYTON__API_KEY`; in memory in `TrytonRestClient` | **None** — Tryton application keys do not expire | Manual: create a new key in Tryton, update config, restart | Delete the key in Tryton |
| Webhook secret (per store) | Fernet-encrypted in the store registry; plaintext shown once at create/rotate | **None** | `rotate-webhook-secret` — immediate, no grace period (A5) | Delete the store or clear the secret |
| Store API key (polling) | Fernet-encrypted in the store registry | **None** | `rotate-api-key` | Delete the store / revoke at the store |
| Gateway OAuth credentials (client id/secret, username/password) | Fernet-encrypted in the channel registry | **None** (the credentials; the access token below expires) | `rotate-channel-credentials` | Disable the channel |
| Gateway OAuth access token (JWT, ~3600s) | **In memory only** in the gateway client; never persisted | ~3600s; refreshed shortly before expiry | Automatic refresh via the stored credentials | Revoke the credentials at the gateway / disable the channel |
| SMS provider credentials | Fernet-encrypted in the SMS provider registry | **None** | Re-save the provider credentials | Disable the provider |
| Fernet vault key | Env `HESABYAR_AUTH__CREDENTIALS_ENCRYPTION_KEY`; `key_id` = first 8 hex of SHA-256 of the key, recorded per vault row | **None** | Manual re-encryption — **no tooling yet** (A6); the per-row `key_id` exists for future multi-key support | Replace the key (requires re-encrypting every row) |
| API-key pepper | Env `HESABYAR_AUTH__API_KEY_PEPPER` | **None** | Rotation invalidates **all** stored key hashes — reissue every key (A7) | N/A (long-lived secret) |
| MCP stdio bootstrap key | Env `HESABYAR_MCP__STDIO_API_KEY` or `--api-key` CLI flag (CLI is visible in the process list — env preferred) | Per-key `expires_at`, checked **at process start only** | Reissue + restart the stdio process | Revoke the key + restart (A3) |
| Admin BFF session id | In-memory Map on the Next.js server; the browser holds an opaque 256-bit id in an httpOnly cookie | 12h inactivity TTL (server) + 12h cookie `maxAge` | N/A (logout destroys the server-side entry) | Logout, or wait for TTL; server restart drops all sessions |
| Moadian signing key | **Not stored.** Production Moadian is not connected; a profile holds only a key reference + SHA-256 of the public key (A13) | — | — | — |
| Fintech OAuth token | **Does not exist yet.** The ۵ت remainder is deferred; only the simulator exists. US-704's acceptance criteria mention Fintech token-expiry alerts — the alert must be built against this token model, not a live token, until the ۵ت remainder lands. | — | — | — |
| Postgres credentials | Env `HESABYAR_DATABASE__URL` (password in the DSN) | **None** | Change the DB password + update config + restart | Revoke at the database |
| S3 credentials (backups) | Env `HESABYAR_BACKUP__ACCESS_KEY_ID` / `HESABYAR_BACKUP__SECRET_ACCESS_KEY`; used only by the backup client (SigV4), never logged | **None** | Replace the key at the provider + update config + restart the worker (the next run picks it up — services are built fresh per run) | Revoke the key at the provider |
| Backup encryption key (Fernet) | Env `HESABYAR_BACKUP__ENCRYPTION_KEY`; `key_id` = first 8 hex of SHA-256 of the key, recorded per backup manifest | **None** | Manual — **no tooling** (same accepted gap as the vault key, A6): mint a new key, update config, restart; keep the old key until its backups age out of retention (docs/backup-restore.md §6) | Replace the key (old backups need the old key to decrypt — keep it until they expire) |
| Tryton backup DSN | Env `HESABYAR_BACKUP__TRYTON_DATABASE_URL` (password in the DSN); used ONLY by pg_dump/pg_restore in the operations layer — never by application code, never in argv/logs (password travels via `PGPASSWORD` env) | **None** | Change the DB password + update config + restart the worker | Revoke at the database |

## 4. Threats by area

### 4.1 Encrypted credentials — at rest and in transit

**At rest.** Secrets are encrypted with Fernet (AES-128-CBC + HMAC-SHA256)
in `credential_vault.py`; the vault key comes from
`HESABYAR_AUTH__CREDENTIALS_ENCRYPTION_KEY` and its `key_id` (first 8 hex of
the key's SHA-256) is recorded per row. API-key hashes are HMAC-SHA-256 with
a server-side pepper, never the secret itself. No production secret is
hard-coded anywhere in `src/` (only dev defaults in config and a CI-script
default for the dev Tryton key). `echo_sql` defaults to False, so SQL
statements — and any bound parameters — are not logged.

**In transit.** All external traffic is HTTPS with default TLS verification
(no `verify=False` anywhere). The Tryton client sets `trust_env=False` so a
poisoned proxy environment variable cannot intercept the ledger connection.

**Residual.** Vault-key rotation has no tooling (A6); pepper rotation
invalidates all keys (A7). Both are long-lived secrets whose rotation is a
planned operator procedure, not a gap an attacker can exploit remotely.

### 4.2 OAuth tokens

HesabYar is an OAuth *client*, never a server: it holds gateway credentials
(SnappPay, DigiPay, TorobPay) encrypted in the vault and exchanges them for
short-lived JWT access tokens (~3600s) that live **in memory only** in the
gateway client and are refreshed before expiry. No access token or refresh
token is ever persisted, logged, or placed in a URL. Internet-banking
passwords are explicitly rejected as a credential type (ADR 0003 decision
10) — the system will not store them.

Audience binding: the MCP HTTP transport binds tokens to the server's public
URL (`resource`, RFC 8707) with `validate_token_resource=True`, so a token
minted for one deployment cannot be replayed against another. See §4.5.

### 4.3 HMAC webhooks

**Signature verification.** When a store has a webhook secret configured,
the ingest endpoint verifies an HMAC-SHA256 over the raw request body, in
hex or base64, with a timing-safe comparison (`hmac.compare_digest`). A
missing, malformed, or wrong signature is a 401 `EVENT_SIGNATURE_INVALID`;
a signature for a different body does not transfer (§7). The endpoint also
requires an `integrations:manage` API key, so a leaked webhook secret alone
is not enough to inject events.

**Replay window (F4).** HMAC covers the body (including `occurred_at`) but
nothing used to stop a captured signed payload for a never-delivered event
from being replayed arbitrarily late. New events older than
`events.webhook_max_event_age_hours` (default 168h / 7 days, `0` disables)
are now rejected with 422 `EVENT_TOO_OLD`. The check applies only to
never-seen events: a re-delivery of an already-stored event still replays as
a 200 duplicate, so legitimate store retries are unaffected; future-dated
events are never stale; manual entries (which legitimately carry old
`occurred_at`) and the polling/sync path (`ingest_verified`) are exempt. The
recovery path for a genuinely missed old event is sync/backfill. Severity was
tempered throughout: idempotency already prevented double accounting effects
for already-seen events (see below), and the endpoint requires an API key.

**Replay of delivered events.** Two layers prevent double effects: an
idempotency record keyed by `event:{source_system}:{source_store_id}:{source_event_id}`
(30-day TTL) plus the inbox source-event unique constraint, which covers
replays after the TTL expires. A re-sent payload returns 200 `duplicate`
indefinitely (A14).

**Secret rotation.** `rotate-webhook-secret` is immediate with no grace
period (A5) — the fail-closed direction: a stale secret stops working at
once, and the operator must update the store's configuration in the same
change window.

**Unsigned webhooks (A1, F5).** When no secret is configured for a store,
the endpoint still ingests the event — unsigned does not mean
unauthenticated, because the `integrations:manage` API key is still
required. This is accepted. What changed in US-701 is that the skip is no
longer silent: `ingest()` now logs a warning (source system, store, event
id — no secrets) whenever a non-manual event ingests unsigned.
`signature_verified` is reported in the response but is **not** persisted on
the inbox row; a future migration could add a column (deferred, §7).

### 4.4 API keys

- **Format:** `hya_live_` + 32 random bytes (256 bits). Brute force is not a
  realistic attack, which is one reason REST has no rate limiting (A2).
- **Storage:** prefix (16 chars) + `v1:` HMAC-SHA-256 hex keyed with the
  server-side pepper. The secret is shown once at issuance and never stored.
- **Verification:** constant-time `hmac.compare_digest` on the hash; every
  auth checks `revoked_at` and `expires_at`.
- **Rotation:** `rotate_key` revokes the old key immediately and never
  extends the expiry; token replay after rotation is covered by tests (§7).
- **Scopes:** every endpoint declares its scope; the audit in US-701
  verified that every REST route requires auth except the intentionally
  public `/health` and `/readiness` (A8). A key with `reports:read` cannot
  call a `banking:read` endpoint or tool, in either direction (§7).
- **Production guard (F1).** The production guard used to reject only the
  dev pepper. A production deploy that forgot the Tryton key or the database
  URL would silently have run against well-known dev credentials (the dev
  Tryton key is a published value; the dev DB URL carries a well-known
  password). The guard now rejects all three dev defaults
  (`DEV_TRYTON_API_KEY`, `DEV_DATABASE_URL`, and the dev pepper) when
  `env=production`.

### 4.5 MCP boundary

The MCP server uses the **same** API-key + scope system as REST — there is
no second credential universe. `HesabYarTokenVerifier` authenticates the
key on every request (HTTP transport) and sets `AccessToken.token` to the
key prefix (never the secret); the token is audience-bound to the server's
public URL (`resource`, RFC 8707, `validate_token_resource=True`).

The middleware enforces, on every data call:

1. **Authentication** — unknown, revoked, or expired keys fail closed with
   `MCP_ERROR_UNAUTHENTICATED`.
2. **Scope checks** — a tool's declared scopes must be a subset of the key's
   grant, or the call is denied with `MCP_ERROR_FORBIDDEN` and audited as
   denied (`missing_scope`).
3. **Company filter** — the `company` argument (or the resource's company)
   must be inside the key's company grant; cross-company and cross-tenant
   reads fail closed with `MCP_ERROR_FORBIDDEN` and are audited as denied.
   A key with no company restriction may read any company of its own tenant,
   and never another tenant's.
4. **Audit** — every call (allowed and denied) is written to the audit log
   with the key prefix, tool, outcome, and error code — **no arguments, no
   secrets**.

MCP has per-process in-memory fixed-window rate limiting (120/min default)
(A11). The stdio transport authenticates its bootstrap key once at process
start (A3): revocation or expiry after start needs a restart; stdio is a
local-developer transport with no network exposure.

### 4.6 Session and password handling

There is **no human password authentication** anywhere in HesabYar: no JWT,
no cookie-based login, no password hashing. Dashboard login is an operator
API key, validated against the backend and stored server-side only, in an
in-memory Map in the Next.js BFF (`apps/admin/lib/auth.ts`). The browser
receives an opaque 256-bit session id in an httpOnly, `sameSite=lax` cookie
(`secure` in production), with a 12h inactivity TTL and a lazy sweep. The
store is single-instance (A12) — a multi-instance deployment would need a
shared session store.

The BFF proxy (`apps/admin/app/api/bff/[...path]/route.ts`) percent-encodes
each path segment (`encodeURIComponent`), so a crafted path cannot traverse
out of the backend origin, and the backend origin itself is fixed in config.

Gateway OAuth passwords (SnappPay / DigiPay / TorobPay) are stored encrypted
in the vault; internet-banking passwords are rejected outright (ADR 0003
decision 10).

### 4.7 File uploads

- Uploads arrive as **base64 in JSON bodies** — there is no multipart path
  to traverse, and no uploaded file is ever served back over HTTP (no
  `FileResponse` / `StaticFiles` in the REST app).
- **Size caps:** 10 MiB per file and 2,000 rows per import in
  `file_readers.py`; the REST body limit (F2) caps the request itself at
  20 MiB, which covers a 10 MiB file as ~13.4 MiB base64 plus JSON overhead.
- **Parsers:** csv / openpyxl / pdfplumber. No XML parsing anywhere, so no
  XXE. There are no CSV/Excel export endpoints, so no formula-injection
  vector.
- **Cheque scans (F3).** `LocalChequeScanStore` used to fetch
  attacker-influenced URLs (from webhook payloads) with only an http/https
  scheme check: no private-IP block (localhost Tryton, cloud metadata
  `169.254.169.254`) and urllib follows redirects to **any** scheme,
  including `file://` (local file read into the filestore). The store now
  resolves the host and requires **every** resolved address to be public
  (explicit denylist, not `ipaddress.is_global`, whose predicates changed
  across Python versions for CGNAT `100.64.0.0/10`), fails closed on
  unresolvable hosts, refuses all redirects, restricts `sayad_id` to
  `[A-Za-z0-9_-]+` in the filename, and refuses invalid ports. A
  `allow_private_hosts` constructor parameter exists as a **test-only** hook
  (local test servers listen on 127.0.0.1); the production wiring never sets
  it. The DNS-rebinding TOCTOU (resolve-then-fetch gap) remains (A10):
  closing it needs IP pinning with Host/SNI handling, and an attacker would
  already need store-level compromise, since scan URLs arrive in signed
  webhooks.

### 4.8 Cross-cutting

**No secrets in logs, errors, URLs, or bundles.** API keys are never logged
(the verifier logs only the prefix); error responses carry generic messages
and problem-detail codes, never credential material; the auth failure log
records only the reason and the prefix. The security suites grep the log
output of a full request cycle (REST + MCP + webhook) for the test secrets
(§7). Secrets never appear in URLs (keys travel in the `Authorization`
header; webhook secrets travel in the `X-HesabYar-Signature` header, as an
HMAC of the body, never the secret itself). The admin bundle contains no
secrets — the BFF holds keys server-side only.

**Denial of service (F2).** REST had no request-body size limit:
FastAPI/Starlette reads unbounded bodies, and the 10 MiB upload cap runs
only after the whole base64 body is parsed into memory. A pure-ASGI
`RequestSizeLimitMiddleware` now enforces 20 MiB (`MAX_REQUEST_BODY_BYTES`),
rejecting early on Content-Length and capping streamed/chunked bodies, with
a 413 problem-details response (`REQUEST_BODY_TOO_LARGE`). It sits inside
the request-id middleware so 413s still carry `X-Request-ID`. There is no
REST rate limiting (A2) — MCP's per-process limiter does not apply to REST;
deploy behind a rate-limiting reverse proxy (US-706).

**SSRF.** Only the cheque scan downloader fetches URLs from untrusted input;
it is guarded (F3, A10). No other outbound fetch takes a caller-supplied URL.

**Supply chain.** Dependencies are pinned in `pyproject.toml` / lockfile;
the review found no vendored code, no `eval`/`exec` on untrusted input, and
no dynamic imports of user input. This review did not audit third-party
packages themselves.

**Tryton boundary.** HesabYar talks to Tryton over its REST API with an
application key; it has no SQL access to Tryton's internal database (spec
§19 rule 11), and report balance reads go through `_report_context`
(`flat_balance: True`) — a correctness rule with a security shadow: a wrong
balance is a wrong report.

**Worker.** Procrastinate job arguments are IDs only (e.g. `event_id`) — no
secrets in job payloads, so a leaked job queue reveals no credentials.

## 5. What the review checked and found clean

- **Endpoint-auth audit:** every REST route requires auth except the
  intentionally public `/health` and `/readiness` (generic details only —
  service/version/environment + ok/not-ok dependency status, no URLs or
  credentials).
- **OpenAPI docs** (`/docs`, `/openapi.json`) are unauthenticated but contain
  only the API surface description and the `hya_live_…` scheme example (A4).
- **Health/readiness details** are generic; `check_database` /
  `check_tryton` expose no connection strings.
- **`X-Request-ID` echo** is length-capped at 128 chars; Starlette/uvicorn
  reject CRLF in header values.
- **No CORS middleware** — cross-origin browser calls are blocked by
  default; the dashboard uses the same-origin BFF.
- **No hardcoded production secrets** in `src/`.
- **Moadian:** production is not connected; no private key material is
  stored (A13).

## 6. Findings register

### 6.1 Fixed in US-701

| ID | Finding | Fix | Commit |
| --- | --- | --- | --- |
| F1 | Production guard rejected only the dev pepper; a deploy forgetting the Tryton key or DB URL would silently run against well-known dev credentials. | Guard now rejects all three dev defaults (`DEV_TRYTON_API_KEY`, `DEV_DATABASE_URL`, dev pepper) when `env=production`. | `780b656` |
| F2 | REST had no request-body size limit (memory-exhaustion DoS: the 10 MiB upload cap runs only after the whole body is parsed). | Pure-ASGI `RequestSizeLimitMiddleware`, 20 MiB default, 413 `REQUEST_BODY_TOO_LARGE`, Content-Length early reject + streaming cap for chunked bodies. | `88bc6f7` |
| F3 | Cheque scan downloader: SSRF (no private-IP block) + redirect-to-`file://` (local file read). | Resolve host, require every address public (explicit denylist), fail closed on unresolvable hosts, refuse all redirects, restrict `sayad_id` in filenames, refuse invalid ports. | `89784da` |
| F4 | Webhook replay window: no freshness check — a captured signed payload for a never-delivered event could be replayed arbitrarily late. | `events.webhook_max_event_age_hours` (default 168h, 0 disables); new events older than the window are 422 `EVENT_TOO_OLD`; already-stored events still replay as duplicates; manual entries and sync exempt. | `780b656` (setting), `55dc99d` (enforcement) |
| F5 | Unsigned-webhook skip was silent: `signature_verified` is not persisted on the inbox row, and nothing durable recorded the skip. | `ingest()` logs a warning (source system/store/event id, no secrets) when a non-manual event ingests unsigned. | `55dc99d` |

### 6.2 Explicitly accepted (with rationale)

| ID | Finding | Rationale |
| --- | --- | --- |
| A1 | Unsigned webhooks accepted when no secret is configured. | The endpoint still requires an `integrations:manage` API key, so unsigned ≠ unauthenticated. The skip is now logged (F5). |
| A2 | No REST rate limiting. | There is no password to brute-force — keys are 256-bit. In-memory per-process limiting is ineffective across workers and risks breaking legitimate bulk clients and CI. MCP already has rate limiting. Deploy behind a rate-limiting reverse proxy — delivered in US-706: `docs/deployment-iran.md` §7 (nginx sketch: per-key + per-IP zones, health endpoints on their own generous zone, no API caching); US-702 load tests will measure auth-path cost. |
| A3 | Stdio bootstrap identity verified once at startup; revocation/expiry after start needs a restart. | Stdio is a local-developer transport with no network exposure; a restart is an acceptable rotation step. |
| A4 | OpenAPI docs unauthenticated. | They describe the public API surface; they contain no credentials. |
| A5 | Webhook secret rotation is immediate, no grace period. | Fail-closed direction: a stale secret stops working at once. Operator note needed in the US-705 manual. |
| A6 | Fernet vault key rotation requires manual re-encryption; no tooling. | Single-key vault; `key_id` is recorded per row for future multi-key support. Manual procedure for the US-705 operator manual; tooling is a future story. |
| A7 | Pepper rotation invalidates **all** API keys. | Documented in `config.py`; the pepper is a long-lived secret and rotation means reissuing all keys. |
| A8 | `/health` + `/readiness` unauthenticated. | They expose only generic details (service/version/environment + ok/not-ok status). |
| A9 | Tryton application key has no expiry. | Tryton provides none. Manual rotation: create a new key in Tryton, update config, restart. |
| A10 | SSRF DNS-rebinding TOCTOU residual (resolve-then-fetch gap). | Closing it needs IP pinning with Host/SNI handling. An attacker would already need store-level compromise, since scan URLs arrive in signed webhooks. |
| A11 | MCP rate limiting is per-process. | Already documented in `docs/mcp-server.md`; acceptable for a per-client transport. |
| A12 | Admin BFF session store is in-memory, single-instance. | Already documented in `apps/admin/lib/auth.ts`; a multi-instance deployment needs a shared session store. |
| A13 | Moadian production credentials don't exist yet. | No private key material is stored; the vault path is reserved for when production Moadian connects. |
| A14 | Replay of an already-stored event returns 200 duplicate indefinitely. | No duplicate effect: two-layer dedup (idempotency record, 30-day TTL, + inbox source-event unique constraint covering post-TTL replays). |

## 7. Test map

All tests run on GitHub Actions CI (no test runs on the dev VM, per standing
rule). New suites are marked **(US-701)**.

| Property | Test |
| --- | --- |
| Auth-bypass attempts: missing/invalid/expired/revoked keys, wrong-company access, cross-tenant access | `tests/interfaces/test_rest_auth.py` (pre-existing) |
| Rotation replay (REST): old key 401 with generic detail, new key 200 with same scopes, old stays dead | `tests/interfaces/test_rest_security_us701.py` **(US-701)** |
| Scope escalation (REST): reports:read ↔ banking:read both directions, 403 naming the missing scope | `tests/interfaces/test_rest_security_us701.py` **(US-701)** |
| Body size limit: 413 via Content-Length and via chunked streaming; 1 MB body passes the 20 MiB limit | `tests/interfaces/test_rest_security_us701.py` **(US-701)** |
| No secrets in logs/responses over a full REST cycle (200/401/403/404 paths) | `tests/interfaces/test_rest_security_us701.py` **(US-701)** |
| Webhook signature forgery: wrong-secret signature and tampered body both 401, no secret in response or logs | `tests/interfaces/test_rest_webhook_security_us701.py` **(US-701)** |
| Webhook replay: identical signed payload → 200 duplicate, exactly one inbox row | `tests/interfaces/test_rest_webhook_security_us701.py` **(US-701)** |
| Replay window: stale new event 422 `EVENT_TOO_OLD`; stale re-delivery of a stored event still replays | `tests/interfaces/test_rest_webhook_security_us701.py` **(US-701)** |
| Unsigned ingest is logged, response flags `signature_verified: false` | `tests/interfaces/test_rest_webhook_security_us701.py` **(US-701)** |
| Basic HMAC behaviour (valid verifies, bad/missing 401, no-store skips) | `tests/interfaces/test_rest_store_webhooks.py` (pre-existing) |
| Scope escalation (MCP): reports:read key cannot call a banking:read tool and vice versa; denied calls audited | `tests/mcp/test_mcp_security_us701.py` **(US-701)** |
| Rotation replay (MCP verifier): old secret verifies to None, replacement verifies with the same grant | `tests/mcp/test_mcp_security_us701.py` **(US-701)** |
| No secrets in logs over a full MCP cycle (allowed, scope-denied, company-denied, unauthenticated, verifier failures) | `tests/mcp/test_mcp_security_us701.py` **(US-701)** |
| MCP auth: verifier valid/revoked/expired/unknown, middleware scope/company/cross-tenant enforcement | `tests/mcp/test_mcp_auth.py` (pre-existing) |
| MCP cross-company reads fail closed (tools + resources + report snapshots) | `tests/mcp/test_mcp_auth.py`, `tests/mcp/test_mcp_resources.py` (pre-existing) |
| SSRF: 15 non-public IPs refused, mixed public+private refused, unresolvable host fails closed, redirect-to-`file://` and redirect-to-private-host refused, `sayad_id`/port validation | `tests/unit/test_scan_store_security_us701.py` **(US-701)** |
| Production guard rejects all dev defaults; replay-window default 168h | `tests/infrastructure/test_config.py` **(US-701)** |
| Key rotation at the service level | `tests/infrastructure/test_api_key_auth.py::test_rotate_key_replaces_old_with_new` (pre-existing) |

**Affected-area updates** (required by the US-701 fixes, not new coverage):
`tests/unit/test_cheque_intake_us533.py` passes `allow_private_hosts=True`
for its local 127.0.0.1 test server;
`tests/interfaces/test_rest_store_webhooks.py`,
`tests/interfaces/test_rest_integrations_events.py`, and
`tests/interfaces/test_rest_integrations_sync.py` now post a fresh
`occurred_at` (the shared fixtures' fixed 2026-11-17 dates would age out of
the 168h replay window after 2026-11-24).

## 8. Residual risks and recommendations for later stories

- **US-702 (load tests):** measure auth-path cost (HMAC verification per
  request) and confirm the 20 MiB body limit does not throttle legitimate
  bulk clients; the 413 path is the backstop, not a substitute for
  load-testing.
- **US-704 (monitoring):** build expiry alerts on the token inventory in
  §3. The only tokens with a real expiry are HesabYar API keys with a
  per-key `expires_at`, gateway OAuth access tokens (~3600s, in-memory,
  auto-refreshed), MCP stdio bootstrap keys (checked at process start
  only), and BFF sessions (12h TTL). **There is no live Fintech token to
  alert on yet** — the ۵ت remainder has not landed. Alert on the
  unsigned-webhook warning log line (F5) as a configuration-smell signal.
- **US-705 (operator manual):** document the webhook-secret rotation
  procedure (no grace period, A5), the manual Fernet vault-key re-encryption
  procedure (A6), the pepper-rotation = reissue-all-keys procedure (A7),
  and the Tryton key rotation procedure (A9).
- **US-706 (deployment):** deploy behind a rate-limiting reverse proxy (A2);
  set all three production secrets (pepper, Tryton key, DB URL) — the
  production guard (F1) now refuses to start without them; terminate TLS at
  the proxy or the app, never in cleartext between proxy and app on an
  untrusted network.
- **Future stories:** persist `signature_verified` on the inbox row (a
  migration; the skip is currently only logged); close the SSRF
  DNS-rebinding TOCTOU with IP pinning (A10); build Fernet vault-key
  rotation tooling (A6); consider a Redis-backed rate limiter if REST
  abuse becomes a real concern (A2).
