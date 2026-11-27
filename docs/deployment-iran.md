# Production deployment guide — Iran hosting / Iranian egress (US-706)

How to install HesabYar on a production server in Iran (or with
Iranian egress), provision the first store, and verify the deployment
with the smoke suite. Spec §17 phase 7 + §20; phased design §1.3
phase 7.

> **Status: no live production server exists yet.** The user provides
> the server later. This guide + the smoke suite
> (`scripts/smoke.py`) are the deliverable of US-706 — nothing in
> this story was deployed to a real server, and nothing here
> provisions one. Every procedure below is written to be followed
> by the operator on the day the server arrives; the install
> procedure (§6) is the one part already proven end to end, by the
> CI install scenario (`tests/contract/test_fresh_install_us706.py`).

> **Decisions still pending (not made here):** the hosting provider
> and the S3 backup destination (Iran-hosted S3-compatible vs S3
> with non-Iranian egress — UNVERIFIED, see §13 and
> `docs/backup-restore.md` §2). This guide names the requirements
> the decision must satisfy; it does not pick a provider.

## 1. What a deployment is

One store = one independent deployment with **two separate
databases** (spec §20): the HesabYar database (tenants, API keys,
inbox/outbox, registries, settlement, cheques, installments, jobs)
and the Tryton database (the ledger). They have separate roles and
separate passwords, and HesabYar talks to Tryton over its REST API
only — never SQL against Tryton's database (spec §19.2; the
backup/restore whole-database exception is documented in
`docs/backup-restore.md` §3).

| Process | What it is | Port (loopback) | Entry point |
|---|---|---|---|
| PostgreSQL | Both databases | 5432 | system service |
| trytond | The ledger (Tryton 8.2) | 8000 | `trytond -c <conf> -d <db>` |
| HesabYar API | FastAPI REST (`/api/v1`) | 8001 | `hesabyar.interfaces.rest.app:app` under uvicorn |
| HesabYar worker | Procrastinate event/scheduled jobs | — (no HTTP) | `scripts/run_event_worker.py` |
| HesabYar MCP | Streamable HTTP MCP server | 8002 | `python -m hesabyar.interfaces.mcp --transport streamable-http --port 8002` |
| Admin dashboard | Next.js (BFF proxy to the API) | 3000 | `pnpm run build` + `pnpm start` in `apps/admin` |
| Reverse proxy | nginx (TLS, rate limiting, headers) | 443 / 80 | system service |

Only the reverse proxy listens on a public interface. Every other
process binds loopback (`127.0.0.1`) and is reachable only through
the proxy or from the host itself. trytond is NEVER exposed
publicly: its REST API key is a bearer credential for the ledger,
and the only client is the HesabYar API/worker on the same host.

## 2. Server requirements

Minimum for one store (a single deployment; scale up, not out —
the admin BFF session store is in-memory and single-instance,
threat model A12):

| Resource | Minimum | Notes |
|---|---|---|
| OS | Ubuntu 24.04 LTS (or equivalent) | The dev/CI reference is Ubuntu 24.04 |
| CPU | 2 vCPU | trytond + API + worker + Next.js share the box |
| RAM | 4 GiB | PostgreSQL + trytond are the hungry two |
| Disk | 40 GiB SSD | Databases + Tryton filestore + backups staging; grow with the store's data |
| Network | Iranian IP egress (see §3) | Nearly all banking services require it |
| Python | 3.12 | CI pins 3.12; the repo is `uv`-managed |
| Node.js | 24 + pnpm 10 | CI pins Node 24 / pnpm 10 (admin build only) |
| PostgreSQL | 16 or 17 | Dev runs 16; CI runs 17 — either is supported |
| Tryton | 8.2.3 + pinned modules | `scripts/ci/tryton-requirements.txt` is the pin list |

No Docker is used anywhere in this project (dev, CI and this guide
are all native installs). No Redis: the worker/queue is
Procrastinate (Postgres-native) by design, and Redis is optional
in the spec.

Clock: the host clock must be accurate (NTP) — webhook replay
windows, token expiries, Jalali date math and the daily backup
schedule all depend on it. Timezone: keep the host on UTC; the
application renders Jalali/`Asia/Tehran` itself.

## 3. Network + Iranian egress

Nearly all Iranian banking services require an Iranian IP
(phased design §1.3 phase 7). That is the deployment's central
network requirement, and it applies to **development/test as well
as production**: the Fintech sandbox also needs an Iranian IP.
A server outside Iran, or behind non-Iranian egress, will fail at:

- Fintech API calls (account/banking data — the ۵ت remainder,
  §14);
- gateway callbacks/verification that check the caller's IP;
- SMS provider APIs, if the provider restricts by origin.

**Verify egress before going live** (and after any network change):

```bash
# The IP the outside world sees for outbound connections:
curl -s https://ifconfig.me
curl -s https://api.ipify.org
# Confirm it geolocates to Iran (any IP lookup the operator trusts).
```

If the server itself is hosted in Iran, egress is Iranian by
construction. If the server is elsewhere, the deployment needs an
Iranian egress path (a VPN/tunnel exit in Iran, or a proxy the
banking calls route through) — that path is part of the hosting
decision, and its reliability is the deployment's reliability for
every banking call. There is no fallback: a banking call from a
non-Iranian IP fails at the provider, not in HesabYar.

**Inbound:** only 443 (and 80 for ACME challenges, §5) from the
internet. SSH from the operator's addresses only. PostgreSQL
(5432), trytond (8000), the API (8001), MCP (8002) and Next.js
(3000) accept loopback connections only — firewall them closed
even from the private network unless a second host genuinely needs
one (it should not, in a single-host deployment).

## 4. Installation

Run as a dedicated service user (e.g. `hesabyar`), never root.
Paths below use `/srv/hesabyar` for the checkout and
`/srv/hesabyar/tryton-env` for the Tryton venv; adjust to the
installation and keep the two venvs separate (Tryton has its own
dependency set).

### 4.1 System packages

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv postgresql-16 \
  postgresql-client-16 nginx certbot python3-certbot-nginx \
  build-essential libpq-dev git curl
# Node 24 (via the operator's preferred Node install) + pnpm 10.
# uv (the Python package manager the repo uses):
curl -LsSf https://astral.sh/uv/install.sh | sh
```

The worker image needs `postgresql-client` at least as new as the
PostgreSQL server (`pg_dump` refuses a newer server) — on a
single-host install the system client satisfies this
(`docs/backup-restore.md` §12).

### 4.2 PostgreSQL: two databases, two roles

```sql
CREATE ROLE hesabyar LOGIN PASSWORD '<strong-password>';
CREATE DATABASE hesabyar OWNER hesabyar ENCODING 'UTF8';
CREATE ROLE tryton LOGIN PASSWORD '<strong-password>';
CREATE DATABASE tryton_db OWNER tryton ENCODING 'UTF8';
```

Names are the operator's choice — the examples here (`hesabyar`,
`tryton_db`) are illustrative, not required. What is required is
the separation: separate databases, separate roles, separate
passwords (spec §7.1). The backup drill additionally needs a
CREATEDB role on both servers (the admin DSNs,
`docs/backup-restore.md` §3) — on a single-host install the
postgres superuser serves, or a dedicated backup role.

### 4.3 Tryton: venv, modules, database, REST key

```bash
# 1. Isolated venv (outside the repo checkout).
python3.12 -m venv /srv/hesabyar/tryton-env
/srv/hesabyar/tryton-env/bin/pip install -r scripts/ci/tryton-requirements.txt
# 2. The HesabYar setup module (chart install/update over REST).
/srv/hesabyar/tryton-env/bin/pip install ./tryton-modules/hesabyar_setup
# 3. Initialise the database + activate the module set (the same
#    flow as docs/dev-setup.md §3 and scripts/ci/setup-tryton.sh
#    step 4; the module list lives in scripts/activate-tryton-modules.sh).
export TRYTONPASSFILE=/srv/hesabyar/tryton-passfile  # admin password file
/srv/hesabyar/tryton-env/bin/trytond-admin -c /srv/hesabyar/trytond.conf \
  -d tryton_db --all --email admin@<the store's domain>
/srv/hesabyar/tryton-env/bin/trytond-admin -c /srv/hesabyar/trytond.conf \
  -d tryton_db -m
TRYTON_ENV=/srv/hesabyar/tryton-env \
  bash scripts/activate-tryton-modules.sh -c /srv/hesabyar/trytond.conf -d tryton_db
# 4. Create the REST application key for the admin user
#    (scripts/ci/create_rest_key.py does this over JSON-RPC; the key
#    value goes into HESABYAR_TRYTON__API_KEY, §10).
# 5. Set company_filter='all' on the API user (setup-wizard §4.2):
#    with the default 'one' and no current company, every
#    company-scoped read comes back empty.
```

The `hesabyar_setup` module is a deployment artifact of the Tryton
side (GPL-3.0+, like all Tryton modules); HesabYar's own code stays
MIT. Pin its version like the other `trytond-*` packages.

### 4.4 HesabYar: repo, venv, migrations

```bash
git clone <the store's HesabYar repo> /srv/hesabyar/app
cd /srv/hesabyar/app
uv sync --frozen
# Environment file (§10) — create it BEFORE the first start.
# (The repo ships no .env.example — write the file from the §10
# checklist; every secret is minted per §9.)
touch .env && chmod 600 .env
# HesabYar schema (Alembic owns it — never hand-create tables).
uv run alembic upgrade head
# Procrastinate schema (the worker applies it idempotently on start;
# --skip-schema when the deploy pipeline applied it already).
```

### 4.5 Admin dashboard: build

```bash
cd /srv/hesabyar/app/apps/admin
pnpm install --frozen-lockfile
pnpm run build
# Served by `pnpm start` (Next.js, port 3000) behind the proxy (§7).
```

The BFF talks to the backend through its same-origin proxy; the
browser never sees an API key (threat model §1). The admin bundle
contains no secrets.

## 5. TLS

Terminate TLS at the reverse proxy (§7). Certificates from the
operator's ACME provider (certbot + nginx plugin is the reference
flow) or from the hosting provider's certificate service — either
way, the private key lives on the proxy host with 600 permissions
and is never committed anywhere.

- Redirect HTTP → HTTPS (except `/.well-known/acme-challenge/`).
- TLS 1.2 minimum, 1.3 preferred; no TLS 1.0/1.1.
- HSTS (see §7 headers) once the HTTPS deployment is verified —
  enable it only after the first successful smoke run, so a bad
  certificate does not brick the domain for the HSTS duration.
- Never run the API, MCP or admin in cleartext across an untrusted
  network (threat model §8): proxy → app traffic stays on
  loopback, so it is not exposed — but if any hop ever leaves the
  host, it must be TLS too.

## 6. Fresh-store provisioning

A new store is provisioned in three layers: the engine company
(Tryton), the HesabYar tenant mapping, and the store connection.
The engine-company flow is `docs/setup-wizard.md` §1 — the same
steps the CI install scenario executes, so the operator procedure
and the tested procedure are one procedure.

### 6.1 Engine company (Tryton, over REST)

| Step | How | Notes |
|---|---|---|
| 1. Currency | IRR must exist (`currency.currency`, code `IRR`) | The install refuses a non-IRR company; digits are 0 (MoneyIRR) |
| 2. Party + company | REST `POST party.party`, `POST company.company` (party + currency) | Only those two fields are required |
| 3. Grant | REST `PUT res.user/<api-user>` — `companies` add + `company_filter='all'` | **Required before anything company-scoped** (setup-wizard §4.1–4.2) |
| 4. Chart | REST action `POST /rest/model/company.company/<id>/install_chart` `{"template_id": <root>}` | Refuses a company that already has a chart; then `update_chart` for the HesabYar extension accounts (1.1.6, 1.2.8, 5.2.4, cheque accounts, …) |
| 5. Move sequence + fiscal year | REST `POST ir.sequence.strict`, `POST account.fiscalyear` | `move_sequence` is required on the fiscal year |
| 6. Periods | REST action `account.fiscalyear/<id>/create_period` | 12 open monthly periods |
| 7. Invoice sequences | 4× `ir.sequence.strict` + `account.fiscalyear.invoice_sequence` | Posting an invoice needs them |
| 8. Verify | `GET /api/v1/accounting/setup-status` (or the dashboard's «وضعیت راه‌اندازی» tab) | Chart + fiscal year + open periods → `ready: true` |

Provision fiscal years for the current year AND the next year: a
settlement due date lands in the next Jalali month, which can cross
the Gregorian year boundary, and its clearing/receipt moves need an
open period there.

### 6.2 HesabYar tenant mapping

Create the tenant, the engine connection (Tryton base URL +
database name) and the `engine_companies` row tying the tenant to
the engine company id. Without the mapping row the dashboard cannot
see the company at all (the list endpoint joins engine companies
against the tenant's mappings). Mint the operator API key with the
scopes the operator needs (see `apps/admin/scripts/mint-dev-key.py`
for the dev equivalent; production keys are minted through the
same key service with production scopes, and shown once).

### 6.3 Store connection + first business

1. **Register the store** in the integrations registry (generic
   connector — `docs/store-connector.md`): the store's changes-API
   base URL + read-only API key, or the read-only store-DB DSN for
   backfill. The custom-coded Hayat store is the reference; the
   connector must stay generic.
2. **Map** the store in the mapping center: `store_company`,
   `external_party` (per customer), `revenue_category` (+ `default`),
   `shipping_charge` (+ `default`), `gateway_journal` per channel
   (`docs/store-connector.md` §10). Every gap fails closed to the
   review queue — nothing is guessed.
3. **Opening balances**: post a balanced opening journal (cash
   against issued capital, or the store's real opening position).
   The trial balance must balance immediately after.
4. **Backfill** the store's history on first connection
   (`POST /integrations/{connector}/sync` with `backfill_days`,
   e.g. 30/90 — an install-time choice) and reconcile: the sync
   report's counts + the review queue show what needs a human.
5. **First order → invoice → payment → settlement**, then the
   trial balance again. The CI install scenario does exactly this
   sequence (`tests/contract/test_fresh_install_us706.py`) — a new
   install that cannot complete it is not ready.

Draft is the default everywhere: auto-post is an explicit,
reversible opt-in per installation (ADR 0003), and payments are
never auto-posted even when the gate allows (a captured payment is
money the gateway owes, not money received).

## 7. Reverse proxy

The reverse proxy is a security control, not just plumbing:
**REST has no rate limiting in the application** (threat model A2 —
keys are 256-bit, so there is no password to brute-force, and
in-memory per-process limiting is ineffective across workers).
The proxy provides it. The sketch below is nginx; any proxy that
provides the same controls serves.

```nginx
# Rate limiting: per API key when present, else per client IP.
# Keys are 256-bit bearer credentials — the limit is abuse control,
# not brute-force protection, so keep it generous: legitimate bulk
# clients (and CI) must never trip it. Tune from the load-test
# baselines (docs/performance-baselines.md).
limit_req_zone $http_authorization zone=api_key:10m rate=120r/m;
limit_req_zone $binary_remote_addr zone=api_ip:10m rate=120r/m;
limit_req_zone $binary_remote_addr zone=health:10m rate=600r/m;

upstream hesabyar_api { server 127.0.0.1:8001; }
upstream hesabyar_mcp { server 127.0.0.1:8002; }
upstream hesabyar_admin { server 127.0.0.1:3000; }

server {
    listen 443 ssl;
    server_name hesabyar.example.com;  # the store's domain

    # TLS (see §5): certbot-managed or provider certificates.
    ssl_certificate     /etc/letsencrypt/live/hesabyar.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/hesabyar.example.com/privkey.pem;

    # Security headers on every response.
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "DENY" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;
    add_header Content-Security-Policy "frame-ancestors 'none'" always;

    # Body size: match the application's own 20 MiB limit
    # (RequestSizeLimitMiddleware, threat model F2).
    client_max_body_size 20m;

    # Health endpoints are unauthenticated by accepted decision
    # (threat model A8 — generic details only) and must never be
    # throttled into a false outage: their own generous zone.
    location = /api/v1/health {
        limit_req zone=health burst=20 nodelay;
        proxy_pass http://hesabyar_api;
    }
    location = /api/v1/readiness {
        limit_req zone=health burst=20 nodelay;
        proxy_pass http://hesabyar_api;
    }

    location /api/ {
        limit_req zone=api_key burst=30 nodelay;
        limit_req zone=api_ip burst=30 nodelay;
        proxy_pass http://hesabyar_api;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        # API responses are never cached.
        proxy_cache off;
        add_header Cache-Control "no-store" always;
    }

    # MCP: Streamable HTTP is long-lived (SSE) — long timeouts, no
    # buffering. The MCP server has its own per-key rate limit
    # (120/min default); the proxy limit above does not apply here.
    location /mcp {
        proxy_pass http://hesabyar_mcp;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }

    location / {
        proxy_pass http://hesabyar_admin;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Notes:

- The `Authorization` header is the rate-limit key when present:
  one abusive key cannot exhaust the shared IP bucket, and one
  abusive IP cannot exhaust a key's budget. Keys are never logged
  (see §9 — the proxy must not log the `Authorization` header).
- `limit_req` returns 503 by default in nginx; map it to 429
  (`limit_req_status 429;`) so clients see the standard code.
- The MCP `/health` endpoint sits outside the MCP rate limiter by
  design (monitoring.md) — probe it directly, not through a
  limited location.
- There is no CORS middleware in the application: browsers block
  cross-origin calls by default and the dashboard talks to the
  backend only through its same-origin BFF. Do not add
  `Access-Control-Allow-Origin: *` at the proxy.

## 8. Process supervision

Every process runs under systemd (one unit per process), restarts
on failure, and logs to the journal. Sketch (one unit shown — the
others follow the same shape):

```ini
# /etc/systemd/system/hesabyar-api.service
[Unit]
Description=HesabYar REST API
After=network.target postgresql.service trytond.service
Requires=postgresql.service

[Service]
Type=simple
User=hesabyar
Group=hesabyar
WorkingDirectory=/srv/hesabyar/app
EnvironmentFile=/srv/hesabyar/app/.env
ExecStart=/srv/hesabyar/app/.venv/bin/uvicorn hesabyar.interfaces.rest.app:app \
  --host 127.0.0.1 --port 8001 --workers 1
Restart=on-failure
RestartSec=5
# No secrets on the command line, no secrets in the unit file:
# everything sensitive comes from the EnvironmentFile (§10).

[Install]
WantedBy=multi-user.target
```

| Unit | ExecStart (shape) | Notes |
|---|---|---|
| `hesabyar-api` | uvicorn `hesabyar.interfaces.rest.app:app` 127.0.0.1:8001 | `--workers 1`: the 5xx counters are in-process (monitoring.md); multi-worker needs a shared metrics store first |
| `hesabyar-worker` | `uv run python scripts/run_event_worker.py` | Applies the Procrastinate schema idempotently on start; the heartbeat IS the `events.sweep` run |
| `hesabyar-mcp` | `python -m hesabyar.interfaces.mcp --transport streamable-http --port 8002` | Per-request auth; needs `HESABYAR_MCP__PUBLIC_URL` = the public URL (§11) |
| `trytond` | `trytond -c /srv/hesabyar/trytond.conf -d tryton_db` | Restart after any `trytond-console` write (server cache) |
| `hesabyar-admin` | `pnpm start` in `apps/admin` | Single instance (BFF sessions are in-memory, A12) |

- `Restart=on-failure` everywhere, with `RestartSec=5`. A crash
  loop is visible in the monitoring panel (worker lag, stale inbox)
  and in the journal — check both, not just one.
- Logs go to the journal (`journalctl -u hesabyar-api -f`); set a
  retention policy (`SystemMaxUse=`) so logs cannot fill the disk.
- **No secrets in logs** (threat model §4.8): the application never
  logs keys, and the proxy must not log `Authorization` headers —
  strip them in the log format if the proxy logs headers.
- Deployments restart processes in dependency order: trytond first,
  then API/worker/MCP, then admin. The upgrade/rollback runbook
  (US-705) owns the full procedure.

## 9. Secrets management

Secrets live in the installation's secret store (the `.env` file
on the host, `chmod 600`, owned by the service user) — never in
the repo, never in CI logs, never in a chat message, never on a
command line (visible in `ps`), never in a URL.

| Secret | Env var | Mint / rotate |
|---|---|---|
| API-key pepper | `HESABYAR_AUTH__API_KEY_PEPPER` | Random 32+ bytes; rotation invalidates ALL keys — reissue every key (threat model A7) |
| Credential-vault key (Fernet) | `HESABYAR_AUTH__CREDENTIALS_ENCRYPTION_KEY` | `scripts/backup.py gen-key` mints Fernet keys; rotation needs manual re-encryption (A6, no tooling yet) |
| Tryton application key | `HESABYAR_TRYTON__API_KEY` | Create in Tryton, update config, restart (A9 — Tryton keys do not expire) |
| HesabYar DB password | in `HESABYAR_DATABASE__URL` / `HESABYAR_DATABASE_URL` | Change at the DB, update config, restart |
| Tryton DB password | in `HESABYAR_BACKUP__TRYTON_DATABASE_URL` (backup only) | Change at the DB, update config, restart the worker |
| Backup encryption key (Fernet) | `HESABYAR_BACKUP__ENCRYPTION_KEY` | Keep an OFFLINE copy — a backup whose key is lost is unrecoverable; keep old keys until their backups age out (`docs/backup-restore.md` §6) |
| S3 backup credentials | `HESABYAR_BACKUP__ACCESS_KEY_ID` / `SECRET_ACCESS_KEY` | Least privilege: only the four operations on the backup prefix (§13) |
| Operator API keys | (issued, shown once) | Rotate via the key service; old key revoked immediately, expiry never extended |
| Webhook secrets (per store) | (in the encrypted registry) | `rotate-webhook-secret` — immediate, no grace period (A5): update the store in the same change window |
| SMS provider credentials | (in the encrypted registry) | Re-save the provider credentials |
| MCP stdio bootstrap key | `HESABYAR_MCP__STDIO_API_KEY` | Reissue + restart the stdio process (A3) |

The production guard refuses to start with any dev default
(`HESABYAR_ENV=production` + the dev pepper, dev Tryton key or dev
DB URL — threat model F1). That is the last line of defence, not
the first: the checklist in §11 is the first.

First-run minting (before the first start):

```bash
# Fernet keys (vault + backup — two DIFFERENT keys):
uv run python scripts/backup.py gen-key   # twice; store each separately
# Pepper (32 random bytes, hex):
python3 -c "import secrets; print(secrets.token_hex(32))"
# Passwords (DB roles, Tryton admin): the operator's password manager.
```

## 10. Environment configuration checklist

All settings use the `HESABYAR_` prefix with `__` nesting
(`AppSettings`). The table lists what production must set
explicitly; everything else keeps its default unless the operator
has a reason to change it.

| Setting | Required in production? | Notes |
|---|---|---|
| `HESABYAR_ENV=production` | **Yes** | Enables the dev-default guard (§9) |
| `HESABYAR_DATABASE__URL` (or `HESABYAR_DATABASE_URL`) | **Yes** | The HesabYar DB DSN (role `hesabyar`) |
| `HESABYAR_TRYTON__BASE_URL` | **Yes** | `http://127.0.0.1:8000` on a single host |
| `HESABYAR_TRYTON__DATABASE` | **Yes** | The Tryton DB name |
| `HESABYAR_TRYTON__API_KEY` | **Yes** | The production Tryton application key |
| `HESABYAR_TRYTON__TIMEOUT_SECONDS` | No (default 120) | Heavy engine actions exceed 30s; do not lower it |
| `HESABYAR_AUTH__API_KEY_PEPPER` | **Yes** | Random; see §9 |
| `HESABYAR_AUTH__CREDENTIALS_ENCRYPTION_KEY` | **Yes** | Fernet key for the credential vault |
| `HESABYAR_MCP__PUBLIC_URL` | **Yes** | MUST be the URL clients use (`https://…/mcp`) — the RFC 8707 audience; a mismatch rejects every MCP request |
| `HESABYAR_MCP__ALLOWED_HOSTS` | **Yes** | JSON list with the real public host (DNS-rebinding protection) |
| `HESABYAR_MCP__PORT` / `HOST` / `PATH` | No (8002 / 127.0.0.1 / `/mcp`) | Change only for a nonstandard layout |
| `HESABYAR_MCP__RATE_LIMIT_PER_MINUTE` | No (default 120) | Per-key, per-process |
| `HESABYAR_BACKUP__ENDPOINT_URL` / `BUCKET` / `REGION` / credentials | **Yes, once the S3 decision is made** (§13) | No defaults — the code picks no provider |
| `HESABYAR_BACKUP__ENCRYPTION_KEY` | **Yes** | Backups fail closed without it |
| `HESABYAR_BACKUP__TRYTON_DATABASE_URL` | **Yes** | Backup-only DSN (the §19.2 operational exception) |
| `HESABYAR_BACKUP__LOCAL_DIRECTORY` | No | Dev/CI only — NOT a production destination |
| `HESABYAR_BACKUP__KEY_PREFIX` | No (default `hesabyar-backups`) | One prefix per installation |
| `HESABYAR_MONITORING__*` | No | Thresholds + cron; defaults are sane (monitoring.md) |
| `HESABYAR_EVENTS__INBOX_STALE_HOURS` | No (default 24) | The staleness threshold (shared with monitoring) |
| `HESABYAR_SCHEDULER__*` / `HESABYAR_SETTLEMENT__*` / `HESABYAR_PACKAGING__*` | No | Cron schedules; defaults are sane |
| `HESABYAR_FILESTORE__DIRECTORY` | **Yes** | Absolute path OUTSIDE the repo checkout (cheque scans live here — and need their own backup, §13) |

Admin (Next.js) environment: the BFF's backend origin is fixed in
config (`apps/admin` env — see its README); the browser origin is
the public domain. No API key ever goes into a `NEXT_PUBLIC_*`
variable (those ship to the browser).

## 11. Deployment checklist + smoke tests

### 11.1 Pre-deploy

- [ ] Egress IP verified Iranian (§3).
- [ ] TLS certificate valid for the public domain (§5).
- [ ] `.env` complete per §10, `chmod 600`, no dev defaults.
- [ ] Backups configured (or the daily job explicitly disabled with
      a recorded decision — never left failing silently).
- [ ] S3 destination decided (§13) or explicitly deferred with a date.
- [ ] Alert channels configured (SMS / webhook — monitoring.md).
- [ ] Operator API key minted with the smoke scopes (§11.3).

### 11.2 Deploy

- [ ] Code at the release tag; `uv sync --frozen` clean.
- [ ] `alembic upgrade head` (single head — verify first).
- [ ] Tryton modules updated if the release changes them
      (`trytond-admin -u …` + restart trytond).
- [ ] Admin rebuilt (`pnpm run build`).
- [ ] Processes restarted in dependency order (§8).
- [ ] No process in a crash loop (`systemctl status`, journal).

### 11.3 Post-deploy: the smoke suite

```bash
export HESABYAR_SMOKE_API_KEY='<redacted>'
uv run python scripts/smoke.py \
  --api-url https://hesabyar.example.com \
  --mcp-url https://mcp.example.com/mcp
```

The smoke key needs scopes `company:read` (login check),
`reports:read` (report + MCP tool), `mcp:connect` (MCP transport)
— and `integrations:manage` only when running the webhook check.
Mint it as a dedicated smoke key (narrow scopes, short expiry),
not the operator's full key.

| Check | What it proves | Touches production data? |
|---|---|---|
| `health` | The API process is up (`GET /api/v1/health`, unauthenticated) | No |
| `readiness` | DB + Tryton reachable (`GET /api/v1/readiness`) | No |
| `login` | The smoke key authenticates (`GET /api/v1/companies`) | No |
| `report` | One trial balance reads + balances (`GET /api/v1/reports/trial-balance`) | No (read-only) |
| `webhook` (opt-in, `--webhook`) | Ingest + HMAC + idempotent re-delivery on the DEDICATED smoke connector | One event in the smoke tenant's review queue — clear it afterwards |
| `mcp` | One read-only MCP tool call (`get_trial_balance`) over Streamable HTTP | No (read-only) |

Exit code 0 = all checks passed (skips do not fail); 1 = a check
failed (the report names it); 2 = usage error. The report never
contains the API key or the webhook secret.

The webhook check is OFF by default and stays off unless the
operator passes `--webhook` with a dedicated smoke connector +
store id and the smoke webhook secret in
`HESABYAR_SMOKE__WEBHOOK_SECRET`. It never posts to a real store's
connector. There is no destructive step in the suite, by default
or by flag.

Run the smoke suite after every deploy, after every restart, and
after any restore (`docs/backup-restore.md` §9.1 step 6). A failed
smoke run blocks the release: fix the deployment, do not ship
around it.

## 12. Backups

See `docs/backup-restore.md` (the full runbook). The deployment
facts:

- Both databases back up daily (02:00 Tehran) via `pg_dump -Fc`,
  Fernet-encrypted, to the configured object store; the monthly
  restore drill (Jalali 1st) proves the backups restore + reconcile.
- **The S3 destination decision is PENDING (UNVERIFIED):**
  Iran-hosted S3-compatible storage, or S3 with non-Iranian egress.
  The code deliberately picks no provider and hard-codes no
  endpoint/region/bucket — decide it together with the hosting
  decision (§3), then set the `HESABYAR_BACKUP__*` variables (§10).
  Until then, backups run against the local-directory backend in
  dev/CI only — that backend is NOT a production destination.
- The backup key needs only `s3:PutObject`, `s3:GetObject`,
  `s3:DeleteObject`, `s3:ListBucket` on the backup prefix; enable
  bucket versioning (a second line of defence against deletes).
- **Tryton file attachments are NOT in the database backups**
  (cheque scans, invoice attachments live on trytond's filesystem).
  They need a separate backup (filesystem snapshot or object-store
  copy of trytond's data directory) — until it exists, a restore
  recovers the books but not the attached files.
- Configuration and secrets are not backed up by the backup jobs:
  keep the `.env`, the Fernet keys and the DB passwords in the
  secret store AND offline (the backup encryption key especially —
  §9).

## 13. Monitoring

See `docs/monitoring.md`. The deployment facts:

- Health endpoints: API liveness `GET /api/v1/health` + readiness
  `GET /api/v1/readiness` (unauthenticated, A8), MCP `GET /health`
  on the MCP origin; the worker's heartbeat is the `events.sweep`
  run; the smoke suite (§11.3) hits the API ones on every deploy.
- Seven alert rules evaluate every 15 minutes (stale inbox, worker
  lag, token expiry, settlement drift, failed webhooks, backup
  failures, 5xx rate) and dispatch over the operator's configured
  channels (SMS / email / webhook). Configure at least one channel
  before going live — an alert nobody receives is not an alert.
- The operator panel (`/monitoring`) shows processes, queues, lag,
  drift, token expiries and backup status in Persian.
- Email alerts are honestly skipped until an email provider exists
  (the F3 provider was never built); SMS (Kavenegar / sms.ir first)
  and webhook channels work today. The SMTP provider for Iran
  hosting is UNVERIFIED (phased design §2) — until one is chosen,
  dashboard alerts are the default and email is opt-in.

## 14. Fintech go-live (a later step)

Fintech live connectivity (the ۵ت remainder, B3) is NOT part of
this story and NOT part of the first deployment. It needs, all
together:

1. the user's Fintech **contract** (a legal-entity agreement —
   Fintech is a legal-only aggregator);
2. the user's Fintech **credentials** (OAuth client — requested
   from the user when the time comes, never guessed);
3. **Iranian IP egress** (§3 — the Fintech sandbox needs it too).

Until then, Fintech runs on the simulator only, and the Fintech
token-expiry alert fires from a configured token record
(monitoring.md §Tokens), never from a live token. Do not promise a
go-live date until all three exist.

## 15. Upgrade + rollback

The upgrade/rollback runbook is US-705's deliverable
(`docs/runbook-upgrade-rollback.md` — forthcoming at the time of
writing): upgrade steps per layer, pre-upgrade checks, rollback
decision criteria, and the data-safety rules (never roll back a
migration that destroyed data without a backup restore). This
guide's §11 checklist is the deploy-time companion to that
runbook, not a replacement for it.

## 16. Known gaps + limitations

Honest list — nothing here is silently open:

- **No live server yet.** This guide has not been executed against
  a real production host; the first real deploy will find the gaps
  this text cannot see. Fix the guide when it does.
- **S3 destination UNVERIFIED** (§12) and **SMTP provider
  UNVERIFIED** (§13).
- **Tryton file attachments need a separate backup** (§12).
- **Single-instance BFF sessions** (threat model A12): the admin
  runs one Next.js instance; multi-instance needs a shared session
  store first.
- **In-process 5xx counters** (monitoring.md): the API runs one
  worker until a shared metrics store exists.
- **No Fernet vault-key rotation tooling** (A6) and **pepper
  rotation = reissue all keys** (A7) — both are manual operator
  procedures (§9).
- **No login rate-limiting in the application** (A2) — the reverse
  proxy provides it (§7). If the proxy is ever removed, REST has
  no rate limiting at all.
- **Moadian production is not connected** (A13): no private key
  material is stored; the vault path is reserved for when
  production Moadian connects.
- **Point-in-time recovery does not exist**: `pg_dump` backups are
  snapshots (up to ~24h of data loss on the daily schedule). WAL
  archiving is a separate story (`docs/backup-restore.md` §9.3).
- **The dashboard has no setup-wizard UI yet**: company creation +
  chart installation are operator procedures (§6.1) driven over
  the Tryton REST API, proven by the CI install scenario — the
  dashboard shows the result (setup-status tab), it does not yet
  perform the steps.
