# Scheduler + maintenance jobs (US-560, Phase 5-7 ۵خ W1)

The scheduler lives on the **same Procrastinate app** as the event
platform (US-501/F1) — it is not a second scheduler. Procrastinate
is the trigger only; HesabYar owns the job registry, the
Jalali-aware `next_run` computation, run recording, and the failure
path. See [event-platform.md](event-platform.md) §4 for the worker.

## Job registry

Two tables (migration 0035):

- `scheduled_jobs` — one row per job: `id` (the Procrastinate task
  name), `queue`, `schedule_kind` (`daily` / `interval` / `monthly` /
  `yearly`), `time_of_day` (Tehran wall time, daily jobs),
  `interval_minutes`, `day_of_month` / `month` (Jalali), `skip_friday`,
  `enabled`, `created_at`, `updated_at`.
- `scheduled_job_runs` — one row per run: `job_id`, `started_at`,
  `finished_at`, `outcome` (`success` / `failed` / `skipped`),
  `triggered_by` (`schedule` / `manual`), `summary` (JSONB),
  `error`, `duration_ms`.

The registry is **closed**: the 12 jobs in
`hesabyar.domain.scheduler.models.DEFAULT_JOBS` are the whole
catalog. They are seeded by `ensure_defaults` (insert missing only
— it never overwrites an operator's enable/disable), in list order,
which is also the order `GET /jobs` returns.

## The jobs

All times are **Tehran wall time** in the registry; the
Procrastinate crons are **UTC** (Tehran = UTC+3:30, no DST since
2022). The drift guard test (`test_scheduler_queue.py`) parses each
UTC cron, adds 3:30, and compares it against the seed schedule — a
mismatch fails CI.

| Job | Queue | Schedule | Purpose |
|---|---|---|---|
| `events.sweep` | `events` | every 1 min | Inbox sweep (US-501): claim + process pending events |
| `settlement.tick` | `settlement` | daily 09:30 | Settlement tick (US-521): `tranche.expected → due` at maturity, expected→due only — W1 only schedules/records it |
| `packaging.supply_check` | `packaging` | daily 10:00 | Supply-wizard result read: the wizard itself runs on `ir.cron` **inside Tryton**; HesabYar only reads the result and emits `low_stock` |
| `cheque.daily` | `cheques` | daily 10:00 | Cheque/installment due alerts (US-531) |
| `sms.daily_reminders` | `sms` | daily 09:00 | SMS daily reminders (US-544) |
| `scheduler.dunning_sweep` | `scheduler` | daily 08:00 | ۵ج dunning sweep (US-541), previously on-demand only via `POST /dunning/run` |
| `scheduler.store_sync` | `scheduler` | every 15 min | Store order sync (US-533): poll each configured store with a cursor; see below |
| `scheduler.inbox_staleness` | `scheduler` | every 60 min | Inbox staleness alert (US-501): events stuck in `received`/`processing` > 24 h |
| `reminders.daily` | `reminders` | daily 08:30 | Daily reminders (US-564): cheque due-soon, overdue installments, uncounted cash boxes, eNamad expiry — in-app + email + SMS via the US-544 registry; see `docs/reminders.md` |
| `scheduler.recurring_generation` | `scheduler` | daily 07:30 | Recurring invoice/journal generation (US-563): generate each due occurrence as a draft; see `docs/recurring.md` |
| `backup.databases` | `backup` | daily 02:00, Friday included | Database backups (US-703): pg_dump both databases, encrypt, upload to the configured S3-compatible destination, prune; see `docs/backup-restore.md` |
| `backup.restore_drill` | `backup` | Jalali 1st, 03:00 | Restore drill (US-703): restore the newest backup pair into scratch databases and reconcile (Alembic heads, orphaned documents, settlement tranches, trial balance). The Procrastinate cron fires daily; the recorder's Jalali-monthly gate runs it on the 1st only. |
| `monitoring.evaluate` | `scheduler` | every 15 min | Monitoring evaluation (US-704): evaluate the alert rules, persist newly fired alerts and dispatch them down the configured channels; see `docs/monitoring.md` |

On-demand endpoints keep working — the schedule **adds** to them,
it does not replace them (`POST /dunning/run`,
`POST /integrations/{connector}/sync`,
`GET /integrations/inbox/stale`).

## Jalali-aware scheduling

No Jalali cron exists, so `next_run` is computed in-app
(`hesabyar.domain.scheduler.schedules.compute_next_run`):

- **Friday is the weekend default.** A daily job with
  `skip_friday=True` (the default) never fires on Friday; an
  explicit `skip_friday=False` overrides it.
- **Interval jobs never skip.** Sub-daily polling (sweep, store
  sync, staleness) runs on Friday — skipping would break the
  polling contract.
- **Monthly/yearly jobs fire on their (clamped) Jalali date even
  on Friday** — skipping would lose the whole month/year.
- **Clamp honestly, never silently skip.** A monthly job on day 31
  fires on day 30 in 30-day months, and on Esfand 29 in 29-day
  years. `next_run` reports `clamped: true` plus the requested and
  actual Jalali day.
- Tehran time is a fixed UTC+3:30 offset (no tzdata dependency;
  Iran has no DST). `next_run` is computed from the last run's
  start when it points forward, else from now; interval `next_run`
  rounds up to the whole minute.

Consequence of the Friday skip: a cheque whose deadline falls on a
Friday is not alerted until Saturday, and the settlement tick
catches up on Saturday (a tranche maturing Friday transitions
Saturday morning). This is the intended behaviour — Friday is a
non-business day.

## Run recording

Every scheduled execution is wrapped by `run_recorded` in
`hesabyar.infrastructure.events.scheduler_queue`. The recorder:

1. Seeds the registry (if needed) and reads the job definition.
2. **Gates** the run: a disabled job, or a daily job on a
   non-scheduled day (Friday), is recorded as `skipped` and the
   function never runs.
3. Runs the job and records `success` with the job's summary dict.
4. On failure records `failed` with the error, files review items
   (below), and **re-raises** so Procrastinate marks the job failed.

Recording happens on the recorder's **own session**, committed
immediately — a run record survives a job rollback. If the registry
is unreachable, the recorder **fails open** (warning log): the job
runs unrecorded rather than being blocked by its own bookkeeping.
The recorder is an optional parameter (`None` = pre-US-560
behaviour), so existing tests are unaffected.

## Failure path: DLQ + review queue, never silent

A failed job is never silent:

- A **wholesale failure** (the job itself raised) files one
  `inbox_dead_letter` review item per tenant, deduped by ref
  `scheduled-job:{job_id}:failure:{YYYY-MM-DD}`.
- A **per-unit failure** (one store/installation inside a sweep)
  files one item per unit, ref
  `scheduled-job:{job_id}:{unit}:{date}`.
- Items are filed through the review queue's command service
  (`ReviewQueueCommandService`), so they get the same dedup + audit
  as any other review item. Review items need a real tenant
  (`tenant_id` is NOT NULL) — tenantless stale events are counted
  in the summary, not filed.

## Store sync (15-minute cadence)

`ScheduledJobsService.run_store_sync_all` polls each configured
store in turn:

- **Identity hazard:** the scheduled sync must use the *same*
  `source_store_id` webhooks use, or orders duplicate under
  different idempotency keys. Resolution: the newest inbox event's
  `source_store_id` for (tenant, `source_system`), else the
  store key (recorded in the summary).
- **Cursor:** persisted in the existing `integration_cursors`
  table, stream `"changes"`. The resume cursor is saved when
  `has_more`, else cleared and `last_synced_at` stamped. Fresh
  polls use `updated_since=last_synced_at`, not the 7-day default
  lookback. A failed store's cursor is left untouched for retry.
- **Skips, not failures:** unconfigured stores (no API URL/DB DSN,
  no API key, no vault, no resolvable tenant) are skipped and
  counted, not failed. `max_pages=10` per run.

## REST API

Scope: `jobs:manage` (all endpoints).

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/jobs` | List all jobs with schedule, last run, next run, last outcome |
| `GET /api/v1/jobs/{job_id}` | One job |
| `GET /api/v1/jobs/{job_id}/runs?limit=50` | Run history (newest first) |
| `POST /api/v1/jobs/{job_id}/trigger` | One manual run now |
| `POST /api/v1/jobs/{job_id}/enable` | Enable the schedule |
| `POST /api/v1/jobs/{job_id}/disable` | Disable the schedule |

Semantics:

- **Disable stops the schedule, not manual triggers.** A manual
  trigger ignores the calendar *and* the enabled flag.
- A failed manual trigger returns the failed run record (HTTP 200
  with `outcome: failed`), not an exception.
- Triggering a job with no runner wired in the REST process
  returns 503 `SCHEDULED_JOB_NOT_RUNNABLE`. Ten of the twelve jobs
  have runners there (`build_job_runners`); the two backup jobs
  (`backup.databases`, `backup.restore_drill`) deliberately do
  not — backups only make sense in the worker (they need pg_dump
  + the object store), so a manual backup/drill runs via
  `scripts/backup.py` instead (see `docs/backup-restore.md` §7).
- Unknown job id: 404 `SCHEDULED_JOB_NOT_FOUND`.

## Configuration

`SchedulerSettings` (`HESABYAR_SCHEDULER__` prefix):

- `dunning_sweep_cron` (default `30 4 * * *`, daily 08:00 Tehran)
- `store_sync_cron` (default `*/15 * * * *`)
- `staleness_cron` (default `0 * * * *`)
- `recurring_generation_cron` (default `0 4 * * *`, daily 07:30 Tehran)

The other jobs' crons are constants in their queue modules (they
predate the scheduler settings). Changing a cron in config changes
when Procrastinate fires the task — the registry's
`time_of_day`/`interval_minutes` is the display/scheduling
contract, and the drift guard test keeps the two in agreement.

## Worker

`scripts/run_event_worker.py` builds the scheduler factory and the
run recorder and passes them to `create_event_app`; the
`scheduler` queue is added to the worker's queue list. The REST
process uses a defer-only app whose tasks raise if they somehow
execute there (unchanged from US-501).

## Deferred / known limitations

- **No REST schedule editing yet.** Schedules are seed-time only;
  changing one needs a code change (seed values) + a registry
  update. The `PUT /jobs/{job_id}` endpoint is a future story.
- **Run retention:** `scheduled_job_runs` grows unboundedly. A
  purge job (keep N days) is deferred — no retention policy was
  specified for W1.
- **No live-Tryton contract tests** were added: US-560 adds no new
  Tryton reads. The Supply-wizard read and the tranche transition
  are existing behaviour, now scheduled/recorded.
- The recorder's Friday gate uses the **Tehran date at the time
  the task executes**. A task deferred late Friday and executed
  Saturday runs (its execution date is Saturday) — the gate is on
  execution time, not deferral time.
