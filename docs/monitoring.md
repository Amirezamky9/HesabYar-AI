# Monitoring + alerting (US-704, Phase 7)

HesabYar watches itself: every process reports health, seven alert
rules evaluate the system's state every 15 minutes, newly fired
alerts persist (deduped) and dispatch down the operator's
configured channels (SMS / email / webhook), and the operator
panel shows the whole picture in Persian.

Three layers, three modules:

| Layer | Where | What |
|---|---|---|
| Snapshot | `MonitoringService.overview()` | Processes, queue depths, stale inbox, worker lag, drift, webhooks, 5xx rate, token expiries, backups — one `MonitoringSnapshot` |
| Rules | `domain/monitoring/rules.py` | Pure evaluation: snapshot + thresholds → `NewAlert` list (no I/O, no clock) |
| Dispatch | `MonitoringService._dispatch()` | Per-channel send via the SMS registry / `EmailSenderPort` / webhook POST |

The service never commits: the caller (router / worker) owns the
transaction. Dispatch never raises: one channel's failure is a
`failed`/`skipped` result, never a broken evaluation.

Alert text is Persian, fixed (not user-editable templates — the
same rule as the reminder messages): an alert's wording is an
operational fact, and a mistyped template must never silence one.
Counts and day counts render in Persian digits; money renders in
Rial (never Toman — no silent conversion).

## Health endpoints

| Process | Endpoint | Auth | What it checks |
|---|---|---|---|
| API (liveness) | `GET /api/v1/health` | none | The process is up; no dependency is checked |
| API (readiness) | `GET /api/v1/readiness` | none | HesabYar DB `SELECT 1` + Tryton adapter ping; 503 when not ready |
| MCP server | `GET /health` (MCP origin, e.g. `http://127.0.0.1:8002/health`) | none | Liveness: `{"status": "ok", "service": "hesabyar-mcp", "version": ...}` |
| Worker | — (no HTTP) | — | Heartbeat: the last recorded `events.sweep` run in `scheduled_job_runs` (the sweep runs every minute; the freshest row IS the heartbeat — no separate table) |
| Tryton adapter | via readiness + the snapshot's `tryton` process | — | Minimal REST read through the existing `HealthService` probe |

The MCP `/health` is unauthenticated by accepted decision
(threat model §6.2 A8 — the REST `/health` is public the same
way) and sits OUTSIDE the rate limiter (`HealthCheckMiddleware`
is the outermost wrapper in `build_streamable_http_app`), so
health probes are never throttled. The monitoring snapshot probes
it with `HttpxMcpHealthProber`, whose URL derives from
`settings.mcp.public_url` (strip `/mcp`, append `/health`) — no
second URL to configure, and a misconfigured public URL fails the
probe instead of probing the wrong server.

## The snapshot

`GET /monitoring/overview` (scope `monitoring:read`) returns the
snapshot the operator panel shows:

- **processes**: `api` (always `ok` — answering proves it),
  `database` + `tryton` (the existing `HealthService` probes),
  `worker` (`ok` / `error` when lagging / `unknown` when no sweep
  was ever recorded), `mcp` (the prober). A process with no probe
  configured reports `unknown`, never a fabricated `ok`.
- **queue_depths**: inbox counts per status, outbox pending, and
  pending Procrastinate jobs per queue (read behind a SAVEPOINT
  from `procrastinate.procrastinate_jobs` where `status='todo'`;
  `null` when the schema is absent — unknown, not zero).
- **stale_inbox_count** + **oldest_unprocessed_age_minutes**:
  events stuck past `events.inbox_stale_hours` (default 24h — the
  same threshold the US-501 staleness job uses).
- **worker_last_sweep_at** / **worker_lag_minutes**.
- **failed_webhook_attempts_24h**, **open_drift_exceptions** +
  **drift_total_irr** (Σ|expected − actual| in Rial).
- **requests_total** / **requests_5xx** / **error_rate_5xx**:
  in-process counters (see §5xx below); `null` in a process that
  serves no HTTP.
- **token_expiries**: live API keys with an expiry + enabled
  configured token records, nearest expiry first (see §Tokens).
- **backups**: one status per database (see §Backup contract).

## Alert rules

Seven rules, evaluated in this order (stable — the first
evaluation's order is the panel's order):

| Rule | Fires when | Severity | Dedup key |
|---|---|---|---|
| `inbox_stale` | ≥1 event stuck past the staleness threshold | critical | `inbox_stale` |
| `worker_lag` | no sweep ever recorded | warning | `worker_lag` |
| `worker_lag` | lag strictly above `worker_lag_minutes` (default 10) | critical | `worker_lag` |
| `token_expiring` | a token inside the warning window (default 14 days) | warning | `token_expiring:{source}:{subject}` |
| `token_expiring` | a token inside the critical window (default 3 days) or expired | critical | `token_expiring:{source}:{subject}` |
| `settlement_drift` | ≥1 open `amount_mismatch`/`fee_mismatch` exception | critical | `settlement_drift` |
| `webhook_failed` | failed attempts in 24h ≥ `webhook_failures_24h` (default 5) | warning | `webhook_failed` |
| `backup_failed` | a database's latest backup failed | critical | `backup_failed:{database}` |
| `backup_failed` | a database has no success within `backup_max_age_hours` (default 26) | warning | `backup_failed:{database}` |
| `error_rate_5xx` | 5xx rate ≥ `error_rate_5xx` (default 5%) over ≥ `error_rate_min_requests` (default 100) | critical | `error_rate_5xx` |

Notes:

- **Drift needs no amount threshold**: an open mismatch exception
  already means the tie-out difference passed its tolerance
  (settlement C6), so ANY open drift exception alerts.
- **UNKNOWN backups never alert**: before US-703 lands there is
  no backup source at all, and an alert nobody can act on is
  noise.
- **Dedup**: while an alert with a rule's dedup key is open OR
  acknowledged, the rule does not fire again (a resolved alert
  CAN fire again). A partial unique index on `dedup_key` over
  firing rows is the race backstop for concurrent evaluations;
  the service treats the race as "already open" (the save uses a
  SAVEPOINT so one race doesn't discard the other alerts in the
  same evaluation).
- Every alert carries a `runbook_ref` — a documentation reference
  (`docs/monitoring.md` section), not a URL: there is no deployed
  docs site to link to.

## Channels

Alert channels are operator-configured (`/monitoring/channels`,
scope `monitoring:manage` for writes). Each channel has a kind, a
target, an enabled flag, and a minimum severity (only alerts at
or above it go out on that channel). No provider is hard-coded
anywhere.

| Kind | Target | How it sends |
|---|---|---|
| `sms` | Iranian mobile (11 digits, `09…`; Persian/Arabic digits normalized) | The existing pluggable SMS registry (US-544): `provider_key` names the provider (Kavenegar / sms.ir / simulator first), `installation_id` names the installation whose registry sends (filled from the operator's session at creation when omitted). Fail-closed exactly like the reminder service: disabled provider → skipped; no credentials → skipped; vault unavailable → failed. |
| `email` | `local@domain.tld` | `EmailSenderPort` — **no email provider exists yet** (the F3 email provider was never built), so email sends are honestly skipped, exactly like the reminder service. The channel still validates and stores, so the operator's intent is recorded for the day the provider lands. |
| `webhook` | https URL (http is refused — alert payloads carry operational detail) | `WebhookAlertSender` POSTs one JSON object per alert (`alert_id`, `rule`, `severity`, Persian `title_fa`/`message_fa`, `dedup_key`, `runbook_ref`, `context`, `fired_at`). The URL itself is the secret (like a Slack webhook URL); rotate by replacing the channel. 10s timeout; non-2xx or transport error → `failed`. |

## Tokens

The token model is threat model §3, read literally. The only
tokens with an expiry to alert on are:

- **HesabYar API keys** (`api_keys.expires_at`), read live from
  that table — every non-revoked key with an expiry, nearest
  first. The panel lists them all (not just the alerting ones)
  so the operator sees the full expiry picture.
- **Configured token records** (`monitoring_token_records`) —
  tokens the operator tells HesabYar about. The Fintech OAuth
  token is the first: **no live Fintech token exists yet** (the
  ۵ت remainder is deferred, simulator only), so the alert fires
  from a configured record — its `expires_at`, the
  warning/critical day thresholds, and its
  `rotation_runbook_ref` — never from a live token.

Tokens the threat model marks "no expiry" (Tryton key, webhook
secrets, SMS credentials, vault key, pepper, Postgres
credentials) have no expiry row and no alert. Gateway OAuth
access tokens live in memory only (~3600s, auto-refreshed) and
are not alertable from the database either.

A token record's `expires_at` must be in the future (a record
for an already-expired token would fire critical immediately —
the operator records the NEW token's expiry after rotating).

## The evaluation job

`monitoring.evaluate` runs on the worker every 15 minutes
(`*/15 * * * *`, `scheduler` queue, recorded in the job registry
like every other periodic job — visible and enable/disable-able
in `/jobs`). `POST /monitoring/evaluate` (scope
`monitoring:manage`) runs it now and returns the
`EvaluationReport` (fired alerts, already-open count, dispatch
outcomes).

The worker evaluation differs from the API-process evaluation in
two deliberate ways:

- **No health checks**: the DB/Tryton probes live in the REST
  layer; the worker must not import the web stack. Worker
  evaluations report those processes as `unknown`.
- **No request metrics**: the 5xx counters live in the API
  process (see below), so a worker evaluation skips the 5xx rule
  (rate `None`). The manual `POST /monitoring/evaluate` is the
  only way the 5xx rule fires — and the panel's «ارزیابی اکنون»
  button exists for exactly that.

## The 5xx rate (in-process by design)

`RequestMetrics` is a thread-safe total + 5xx counter in the API
process, fed by `RequestMetricsMiddleware` (outermost, so it
counts every response including error responses). It is
deliberately in-memory and per-process:

- a restart resets the counters (`started_at` says so honestly);
- a multi-worker deployment needs a shared metrics store (a
  future story) — until then the panel shows the rate for the
  process that answered, and this doc says so.

## The operator panel

`/monitoring` (Settings ← پایش سیستم, spec §18 module 13):
three tabs — «نمای کلی» (processes, queues + lag + 5xx + drift,
recent alerts with acknowledge/resolve, token expiries, backup
status), «کانال‌های هشدار» (channel CRUD), «توکن‌ها» (token-record
CRUD). The dashboard's `AlertsWidget` is the starting point for
the alert rows (severity, empty state); the dashboard itself is
unchanged. The panel is tenantless (operator surface, like the
jobs registry).

## قرارداد وضعیت پشتیبان‌گیری (the US-703 contract)

The backup-status alert reads a documented status source — it
does NOT know how US-703 records backups. The interface is
`BackupStatusSource` (`domain/monitoring/ports.py`):

```python
class BackupStatusSource(Protocol):
    def statuses(self, *, now: datetime) -> list[BackupStatus]: ...
```

US-703's backup job implements this protocol — however it
records runs (a `backup_runs` table, a status file, an S3
listing), it must be able to answer, for each of `hesabyar` and
`tryton`:

| Field | Meaning |
|---|---|
| `database` | `"hesabyar"` or `"tryton"` |
| `status` | `"ok"` (a successful backup within the expected cadence), `"failed"` (the latest attempt failed), `"stale"` (no success within the cadence), `"unknown"` (no information) |
| `last_success_at` | Last successful backup (UTC, `None` when none) |
| `last_attempt_at` | Last backup attempt (UTC, `None` when none) |
| `error` | The latest failure's short text — no credentials, no URLs, no internals: it renders in the operator panel |
| `source` | Where the status came from (e.g. `"backup_runs"`) — `"none"` until US-703 lands |

Rules for the implementation:

- The service calls `statuses()` once per snapshot and once per
  evaluation: the implementation must be cheap (a single indexed
  read — no S3 round-trip per call; cache or read the recorded
  status).
- The cadence decision (`ok` vs `stale`) belongs to US-703: it
  knows the backup schedule. The monitoring rule only maps the
  reported status to an alert (`failed` → critical, `stale` →
  warning, `unknown` → silent).

**Implemented (Phase 7 merge):** the wired source is
`JobRegistryBackupStatusSource`
(`infrastructure/persistence/postgres/monitoring_repository.py`):
it reads the US-560 job registry — the `backup.databases` job
(US-703) backs up both databases in one run, so its latest run IS
the backup status for both. No backup table exists (and none was
created): the status comes from two indexed reads on
`scheduled_job_runs`, with the ok-vs-stale cadence from
`monitoring.backup_max_age_hours` (default 26h — the daily 02:00
backup plus a 2h grace). Before any run is recorded, both
databases report UNKNOWN and the panel shows «نامشخص», never a
fabricated success. The failure text shown in the panel is
sanitized (URLs stripped, truncated to 200 chars). Wired into
both `get_monitoring_service` (`interfaces/rest/dependencies.py`)
and `build_monitoring_factory` (`scripts/run_event_worker.py`).

## Runbooks

Each alert's `runbook_ref` points at one of these sections.
The operator manual (`docs/operator-manual.md` §4, Persian)
expands each of these into a fuller incident procedure — the
section titles here and the manual's incident table are kept in
agreement by `tests/unit/test_runbook_refs_us705.py`.

### صندوق ورودی گیرکرده

Events are stuck in `received`/`processing` past the staleness
threshold (default 24h). Check the review queue (`/review-queue`)
for the stuck events; check the worker is running (the worker
process in the panel); check the sweep's recent runs in `/jobs`
(`events.sweep`). A stuck event that is genuinely unprocessable
belongs in the review queue, not retried forever.

### کارگر (Worker) پاسخ نمی‌دهد

The last sweep run is older than the lag threshold (default 10
min) — or no sweep was ever recorded. Check the worker process is
running (`scripts/run_event_worker.py`); check its logs for a
crash loop; check the database is reachable from the worker host.
The worker's own heartbeat IS the sweep run — if the sweep runs,
the worker is alive.

### چرخش توکن فینوتک

The Fintech OAuth token expires soon (warning: 14 days; critical:
3 days) or has expired. Rotate it in the Fintech provider's
console, then update the token record (`/monitoring` ← «توکن‌ها»)
with the NEW token's expiry — the alert fires from the record,
so the record must follow the rotation. Until the ۵ت remainder
lands a live token, the record is the only source of truth: there
is no live token to rotate yet, only the record to keep current.

### چرخش کلید API

A HesabYar API key expires soon or has expired. Rotate it in the
API-key management (the MCP client rotation flow for MCP clients,
or the key's own rotation) and update every client that uses it.
The alert reads `api_keys` live — once the new key is in place
(with its own expiry), the alert clears on the next evaluation.

### مغایرت تسویه

Open `amount_mismatch`/`fee_mismatch` exceptions exist: the
tie-out already failed its tolerance, so ANY open row is a live
drift. Open the settlement center, review the exceptions, and
resolve them there (the monitoring alert only reports; it never
resolves a financial exception on its own).

### وب‌هوک‌های ناموفق

Failed integration attempts in the last 24h reached the threshold
(default 5). Check the integration report for the failing
endpoint (inbox or outbox), verify the remote endpoint is up, and
replay the dead-lettered events from the review queue where
possible.

### پشتیبان‌گیری ناموفق

A database's latest backup failed (critical) or has no success
within the cadence (warning, default 26h). Check the backup job's
logs (US-703), verify disk/S3 space and credentials, and run a
backup manually. The alert clears when the status source reports
a fresh success.

### نرخ خطای ۵xx

The 5xx rate in the API process passed the threshold (default 5%
over ≥100 requests). Check the API server's logs for the failing
endpoints; check the database and Tryton (a down dependency
turns into 5xx fast). The counters are in-process: a restart
resets them, and the rate covers only the process that answered.

## Configuration

`MonitoringSettings` (`HESABYAR_MONITORING__*`):

| Setting | Default | Meaning |
|---|---|---|
| `worker_lag_minutes` | `10` | Worker-lag alert threshold |
| `token_warning_days` | `14` | Token warning window |
| `token_critical_days` | `3` | Token critical window |
| `webhook_failures_24h` | `5` | Failed-webhooks alert threshold |
| `error_rate_5xx` | `0.05` | 5xx-rate alert threshold |
| `error_rate_min_requests` | `100` | Minimum requests before the 5xx rule fires |
| `backup_max_age_hours` | `26` | Backup staleness threshold (used by the rule's text; the ok/stale decision is US-703's) |
| `evaluation_cron` | `*/15 * * * *` | The `monitoring.evaluate` schedule |
| `mcp_probe_timeout_seconds` | `5.0` | MCP `/health` probe timeout |

The inbox-staleness threshold is NOT duplicated here: the rules
read the existing `events.inbox_stale_hours` (default 24).

## REST endpoints

All under `/api/v1/monitoring` (see `docs/rest-api.md` for the
table). Reads need `monitoring:read`; evaluation, alert lifecycle
and channel/token writes need `monitoring:manage`. The surface is
tenantless (operator surface, like `/jobs`).

Errors: `MONITORING_VALIDATION_FAILED` (422, with per-field
errors), `MONITORING_NOT_FOUND` (404),
`MONITORING_ALERT_ALREADY_OPEN` (409 — a concurrent evaluation
fired the same rule first).

## Tests

- `tests/unit/test_monitoring_rules.py` — all seven rules,
  severity boundaries, Persian digits, dedup keys.
- `tests/unit/test_monitoring_metrics.py` — the 5xx counters.
- `tests/unit/test_monitoring_service.py` — overview assembly,
  evaluation dedup (+ race + re-fire), dispatch across all three
  channels (incl. the fail-closed SMS paths and the honest email
  skip).
- `tests/unit/test_monitoring_dispatch.py` — webhook sender +
  MCP prober over `httpx.MockTransport`.
- `tests/interfaces/test_rest_monitoring.py` — all 13 endpoints
  via dependency overrides + the 5xx middleware counting check.
- `tests/mcp/test_mcp_health_us704.py` — the MCP `/health`
  middleware + the wired app.
- `apps/admin/features/monitoring/monitoring-data.test.ts` —
  labels, form validation, pure helpers (vitest).
- `apps/admin/e2e/monitoring.spec.ts` — the panel journey
  (sections, evaluate-now, tabs, 360px overflow).
