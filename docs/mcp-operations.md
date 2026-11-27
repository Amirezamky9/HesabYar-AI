# MCP operations (US-606)

The operator guide for the HesabYar MCP server: how to connect an
AI client, the scope model, the audit log, revocation, and the
security review of the MCP boundary. The server itself is
documented in `docs/mcp-server.md`; the REST endpoints in
`docs/rest-api.md`.

## Connect an AI client

### 1. Issue a client in the admin panel

هوش مصنوعی و MCP ← کلاینت‌های MCP → «کلاینت جدید»:

1. Name the client (e.g. «Claude Desktop — حسابداری»).
2. Pick the scopes it needs (see [Scope model](#scope-model)
   below). `mcp:connect` is always on — without it the key cannot
   open a session.
3. Optionally restrict it to one company. A client with no company
   grant sees every company of the tenant.
4. Optionally set an expiry (30 / 90 days / 1 year / none).
5. «صدور کلید» — the secret appears **exactly once**. Copy it now;
   closing the dialog loses it for good. (A lost secret is not a
   disaster: rotate the client for a new one.)

The operator needs the `api_keys:manage` scope to manage clients
(managing an MCP client IS managing an API key — no separate scope
exists).

### 2a. stdio (local developer / single-user host)

The host spawns the server as a subprocess with the client's key
in the environment. Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "hesabyar": {
      "command": "uv",
      "args": ["run", "python", "-m", "hesabyar.interfaces.mcp", "--transport", "stdio"],
      "cwd": "/path/to/HesabYar-AI",
      "env": {
        "HESABYAR_MCP__STDIO_API_KEY": "hya_live_...",
        "HESABYAR_DATABASE_URL": "postgresql://..."
      }
    }
  }
}
```

The stdio process authenticates its key once at startup and runs
as that key's grant. It fails fast (exit 1) when the key is
invalid, revoked or expired, or lacks `mcp:connect`, and (exit 2)
when no key is given at all. Prefer the environment variable over
`--api-key` — a CLI argument is visible in the process list.

Notes:

- One stdio process = one key = one grant. Two hosts need two
  clients (two keys); never share one client's key between hosts —
  the audit log cannot tell them apart otherwise.
- The process needs the HesabYar package + the database reachable
  from wherever the host runs. For a host on another machine, use
  Streamable HTTP instead.

### 2b. Streamable HTTP (remote / production)

Start the server:

```bash
python -m hesabyar.interfaces.mcp --transport streamable-http --port 8002
```

The host connects to the public URL (default
`http://127.0.0.1:8002/mcp`) with the client's key as a Bearer
token, per request:

```
Authorization: Bearer hya_live_...
```

Production MUST serve behind TLS (directly via
`HESABYAR_MCP__TLS_CERTFILE` / `HESABYAR_MCP__TLS_KEYFILE`, or via
a terminating proxy) — API keys must never cross a network in
cleartext. `HESABYAR_MCP__PUBLIC_URL` MUST match the URL clients
use to reach the server (it is the audience the verifier checks),
and `HESABYAR_MCP__ALLOWED_HOSTS` / `HESABYAR_MCP__ALLOWED_ORIGINS`
must list the production host (DNS-rebinding protection is always
on). See the configuration table in `docs/mcp-server.md`.

## Scope model

| Scope | What it unlocks |
| --- | --- |
| `mcp:connect` | Open an MCP session at all. Mandatory on every client. |
| `company:read` | List companies, company info. |
| `account:read` | Chart of accounts (tool + resource). |
| `ledger:read` | Trial balance, general ledger, fiscal periods. |
| `reports:read` | Income statement, balance sheet, cash-flow, report snapshots. |
| `banking:read` | Bank reconciliation status, unreconciled activity. |
| `compliance:read` | Compliance / Moadian status reads. |
| `standards:read` | Standards registry + validation snapshots. |
| `standards:validate` | Run a standards validation. |
| `workflow:read` | Pending review items. |
| `review:read` | Review-queue reads. |
| `settlement:read` | Settlement status. |
| `cheques:read` | Cheque status reads. |
| `packaging:read` | Packaging reads. |
| `cashbox:read` | Cashbox status + cash forecast. |
| `tax:read` | Tax-policy reads. |
| `integrations:read` | Integration status reads. |

Rules of the model:

- **Read-only by design.** The admin UI offers only read scopes
  (plus `standards:validate`, which runs a validation and records
  it — it posts nothing to the ledger). No MCP client should ever
  hold a write scope; the server cannot silently post or submit
  (spec §17 phase-6 exit criterion).
- **Least privilege.** A reporting client needs `company:read` +
  `reports:read` + `ledger:read`, not the full set. A denied call
  is a 403 with `missing_scope` — the audit row names the tool and
  the company, so a too-narrow grant is easy to spot and widen.
- **Company grant is orthogonal to scopes.** A client with
  `reports:read` and a grant for company A cannot read company B's
  reports — the grant is enforced on every call, on every
  transport, and a cross-company attempt fails closed (403) and is
  audited.

## Audit

Every `tools/call`, `resources/read` and `prompts/get` lands one
row in `ai_access_audit`: client, key prefix, tool, kind, company,
scopes carried, outcome (`allowed` / `denied` / `error`), error
code, request id, transport, client name/version, timestamp.
Denied and failed attempts are audited too — a denied call is the
audit event that matters most.

Client lifecycle events land in the same log as `kind='management'`
rows (`transport='rest'`): `mcp.client.create`,
`mcp.client.rotate`, `mcp.client.revoke` — so the audit tab shows
who was given access, when a key was rotated, and when access was
cut, next to the calls themselves. The acting operator is recorded
separately in the REST audit trail (`audit_events`).

The audit stores **no** tool arguments, **no** hidden
chain-of-thought or model reasoning, and **no** secrets — only the
key's visible prefix. An audit-write failure fails the call
closed: the result is withheld rather than serving an unaudited
call.

In the admin panel (گزارش دسترسی tab): filter by kind, outcome,
management event, or client; «بارگذاری بیشتر» pages through the
log (keyset pagination, newest first).

## Revocation

- **Revoke** (ابطال) in the admin panel cuts access at once: the
  key stops authenticating immediately, on both transports. A
  running stdio process keeps its already-authenticated session
  only until it restarts — the server re-authenticates the key at
  startup; Streamable HTTP authenticates per request, so it stops
  on the next call. Revocation is idempotent and irreversible: a
  revoked client cannot rotate — create a new client instead.
- **Rotate** (چرخش کلید) when a secret may have leaked, when a
  host is decommissioned, or on a schedule. The old key dies the
  moment the new one is issued; the host must be reconfigured with
  the new secret (shown once, as at creation).
- **Expiry** is a backstop, not a substitute: set an expiry on
  clients for contractors and short-lived hosts, and revoke
  explicitly when the work ends.

## Security review of the MCP boundary

Reviewed for US-606 (spec §13.8). The boundary is: the MCP server
process, its two transports, the API-key verifier, the middleware,
and the audit writer.

### What the boundary guarantees

- **Authenticated everywhere.** Every request is authenticated
  (except `initialize`/`ping`/notifications, which carry no data
  access). Unauthenticated → 401; wrong scope → 403
  `missing_scope`; cross-company → 403. All three are audited.
- **Fail-closed scope + company checks.** Scopes and the company
  grant are enforced centrally in the middleware for tools, and —
  since US-606 — for resources too (template-aware matching); the
  resource/prompt handlers keep their own checks as defence in
  depth. An unknown tool, resource or prompt is a protocol error,
  never a data leak. A key that cannot be authenticated cannot
  call anything.
- **No second credential system.** MCP clients ARE API keys
  (HMAC-SHA-256 + pepper, never stored in cleartext). Revoking the
  key revokes the client; there is no separate MCP password to
  leak or rotate.
- **No direct database access.** No MCP code touches PostgreSQL
  directly (spec §0 rule 4): tools call application services only,
  and the services use the repositories/ports. There is no raw-SQL
  tool for MCP or LLM clients (spec §19 rule 5).
- **No secrets in the protocol surface.** No API keys in tool
  arguments, URLs, logs or bundles. The raw secret is never
  retained past verification (the token holds the visible prefix,
  not the secret). No secrets in tool descriptions or results. The
  audit log records only the key prefix.
- **No token forwarding.** The server authenticates the caller,
  then calls Tryton with its own configured engine credential —
  one service's bearer token is never forwarded to another service.
- **Audience binding.** The verifier binds tokens to the public
  URL (`PUBLIC_URL`); a token issued for another resource is
  refused. (No OAuth authorization server is implemented — the
  binding applies to the API-key credentials in use today.)
- **Transport hardening.** Origin + Host validated (DNS-rebinding
  protection, always on); 4 MiB request-body limit (413); 120
  requests/key/minute (429 + `Retry-After`); 30-minute session
  idle timeout. Production MUST serve behind TLS.
- **Audit is mandatory, not best-effort.** An audit-write failure
  withholds the result. The audit records the client, the tool,
  the company and the outcome for allowed, denied and failed calls
  alike.

### Known limitations (do not mistake them for guarantees)

- **Live host interop is UNVERIFIED.** The server is exercised
  with the official SDK client and a raw JSON-RPC client in CI,
  but not yet against a real AI host. The first real-host
  connection should be watched in the audit log.
- **Rate limiting is per-process** (in-memory). A multi-worker
  deployment needs a shared limiter before the 120/min figure can
  be relied on as a global bound.
- **Audit failure does not roll back side effects.** The tool has
  already run by then. All Wave-B tools are read-only, so this is
  a non-issue today; the US-605 draft tools document it in their
  own safety notes.
- **No idempotency keys on client create/rotate.** A retried
  rotate issues a second key. The admin UI disables the button
  while acting; API callers should retry only on a confirmed
  failure.
- **A revoked stdio process keeps running until restarted.**
  Revocation stops new authentications; a long-lived stdio process
  authenticated before the revocation is not killed remotely.
  Kill the process (or the host) as well when revoking for cause.
- **OAuth is not implemented.** If a future host requires OAuth,
  the authorization server is new work — do not improvise one by
  minting API keys as OAuth tokens.
