# Upgrade + rollback runbook (US-705)

How to upgrade a HesabYar installation to a new release, and how
to roll it back when the upgrade goes wrong. Spec §17 phase 7;
phased design §1.3 phase 7.

**The one rule:** never upgrade without a backup you could restore
from. The upgrade procedure takes a dedicated pre-upgrade backup
after the writes stop (§3 step 3) — the daily backup is not a
substitute, because it predates the upgrade window.

## 0. What an upgrade touches

Four layers, upgraded in order, rolled back in reverse order:

| Layer | What changes | Rollback |
|---|---|---|
| 1. Code | The HesabYar checkout (`git` tag) + Python deps (`uv sync`) | Check out the previous tag (§4.1) |
| 2. HesabYar schema | Alembic migrations (`alembic upgrade head`) | `alembic downgrade` — ONLY when the migration is non-destructive (§4.2) |
| 3. Tryton modules | Package versions in the Tryton venv + `trytond-admin -u` | **There is no module downgrade** — backup restore only (§4.3) |
| 4. Frontend | The admin build (`pnpm build`) + BFF restart | Rebuild the previous tag (§4.4) |

Deployment mechanics (process supervision, reverse proxy, TLS,
where the code lives on the server) belong to US-706; this
runbook is the PROCEDURE — the order, the checks, and the
decisions — and assumes the US-706 deployment exists.

Versioning, the changelog and environment promotion live in
`docs/release.md`. Backups and restores live in
`docs/backup-restore.md`. Day-to-day operations live in the
operator manual (`docs/operator-manual.md`, Persian).

## 1. Before you start

1. **Read the release notes** (`docs/release.md` + the release's
   CHANGELOG entry). A release may carry a manual step this
   runbook cannot foresee (a config rename, a one-off data fix);
   the notes say so, and the manual step runs in its documented
   place in §3.
2. **Pick a maintenance window.** Upgrades stop the worker, the
   API, the MCP server and trytond (§3). Announce the window to
   the store: no orders, no webhooks, no panel use while it runs.
   A routine upgrade takes well under an hour; a release with a
   Tryton module update takes longer (module updates rewrite data
   — budget the staging rehearsal's measured time, §3 step 0).
3. **Name the rollback decision-maker** — one person, reachable
   during the window, who can call the rollback in §5 without a
   meeting.
4. **Rehearse on staging first** (`docs/release.md` §4). The first
   production upgrade of a release is never the first time the
   procedure runs.

## 2. Pre-upgrade checks

Run the scriptable checks, then the manual ones. The script
blocks the upgrade unless every check passes.

### 2.1 Scripted checks

```bash
uv run python scripts/pre_upgrade_check.py
```

Exit 0 = every check passed. Any other exit = STOP and fix the
reported check (the script prints what to do). The checks
(`src/hesabyar/infrastructure/operations/pre_upgrade_checks.py`,
tests in `tests/unit/test_pre_upgrade_checks_us705.py`):

| Check | What it proves | On failure |
|---|---|---|
| `hesabyar.migrations` | The database is at the single Alembic head (the US-703 drill check, reused) | The database is behind (or ahead of) the code — resolve before upgrading; never upgrade from a half-migrated state |
| `pre_upgrade.backup_fresh` | Both databases have a fresh successful backup (the US-703/US-704 backup-status contract) | Take a backup (`scripts/backup.py backup`) and re-run; an UNKNOWN status means no backup has ever run — configure backups first |
| `pre_upgrade.tryton_reachable` | The Tryton engine answers a minimal read | The post-upgrade module verification needs a reachable engine — fix connectivity first |

A SKIPPED check blocks the upgrade exactly like a FAILED one:
an unverified backup is not a backup, and the script says so
instead of guessing.

### 2.2 Manual checks (no script can verify these)

The script prints them as a reminder; the operator confirms each:

- [ ] The release notes were read, and any manual step they call
      out is understood (§1 step 1).
- [ ] The maintenance window was announced to the store.
- [ ] The configuration diff was reviewed (new `HESABYAR_*`
      settings in this release — a new setting with no value may
      fail closed at startup, which is correct but must not
      surprise anyone mid-window).
- [ ] The rollback decision-maker (§1 step 3) is reachable.

### 2.3 Record the current state

Write down where you are — a rollback needs the starting point,
and memory is not a record:

```bash
uv run alembic current                    # HesabYar revision (expect the old head)
curl -s http://127.0.0.1:8001/api/v1/installation \
  -H "Authorization: Bearer $API_KEY"     # revision + Tryton release + modules
git rev-parse HEAD && git describe --tags # code version
```

(`GET /api/v1/installation` needs the `settings:manage` scope;
see `docs/rest-api.md`.)

## 3. Upgrade steps

**Step 0 — Rehearse on staging.** Run this whole procedure on
staging first and time it. If staging diverges from production
(different data volume, different module set), say so in the
release notes — the rehearsal is only as good as the staging
mirror.

**Step 1 — Stop the worker.**

```bash
# Stop the worker process (systemd unit / supervisor / the shell
# running it). Wait for running jobs to finish — check /jobs for
# runs still in flight; do not kill -9 a worker mid-job.
```

Stopping the worker stops intake processing (the `events.sweep`
runs every minute) and the scheduled jobs. Webhooks already
received stay in the inbox and process after the restart; nothing
is lost, only delayed.

**Step 2 — Stop the API, the MCP server, and trytond.**

```bash
# Stop, in this order: MCP server, REST API, trytond.
# (MCP first: it is read-only, but its clients must not observe a
# half-upgraded system; trytond last, so in-flight REST calls can
# finish against a live engine.)
```

**Step 3 — Take the pre-upgrade backup.** The writes are stopped
now, so this backup is a consistent snapshot of the exact state
the upgrade starts from:

```bash
uv run python scripts/backup.py backup
uv run python scripts/backup.py list   # confirm both targets, note the object keys
```

Keep the object keys with the §2.3 record. If time allows, run
the drill (`scripts/backup.py drill`) — a backup proven good
before the upgrade is worth more than one proven after it. If the
window is short, the drill may run after the upgrade instead, but
the BACKUP itself is never skipped.

**Step 4 — Upgrade the code.**

```bash
git fetch --tags
git checkout vX.Y.Z            # the release tag (docs/release.md §1)
uv sync --frozen               # Python deps for the new release
```

**Step 5 — Migrate the HesabYar schema.**

```bash
uv run alembic current         # the old head (compare with §2.3)
uv run alembic upgrade head
uv run alembic current         # the new head — must match the release notes
```

If `alembic upgrade head` fails halfway: STOP. Do not re-run it
blindly, do not hand-edit `alembic_version`. Alembic runs each
migration in a transaction — a failed migration leaves the
database at the previous revision. Read the error, fix the cause
(usually a data conflict the migration did not expect), and
re-run; if the cause is not clear within the window, roll back
(§5).

**Step 6 — Upgrade the Tryton modules** (only when the release
notes say the module set changed; otherwise skip to step 7).

```bash
# 1. Install the new package versions into the Tryton venv
#    (pinned in scripts/ci/tryton-requirements.txt; rebuild the
#    local wheels from tryton-modules/ first when they changed).
uv pip install --python <tryton-env>/bin/python \
  -r scripts/ci/tryton-requirements.txt
uv pip install --python <tryton-env>/bin/python --force-reinstall \
  tryton-modules/hesabyar_setup tryton-modules/account_check_ir

# 2. Update the modules in the database (idempotent; -u updates
#    already-installed modules). trytond is stopped (step 2).
scripts/activate-tryton-modules.sh -c <trytond.conf> -d <database>
```

Remote-database warning (from `docs/tryton-modules.md`): do NOT
run `trytond-admin` directly against a remote database — at
~127 ms/query a full activation takes hours. Use the dump →
local activate → restore path the script documents.

**Step 7 — Rebuild the frontend** (only when the release notes
say the admin changed; otherwise skip).

```bash
cd apps/admin
pnpm install --frozen-lockfile
pnpm build
# Restart the admin (Next.js) process; the new build serves from it.
```

Restarting the admin logs every operator out: BFF sessions are
in-memory (threat model §6.2 A12), and a restart clears them.
That is expected — warn the operators, do not treat it as a
failure.

**Step 8 — Restart, in dependency order.**

```bash
# 1. trytond (the engine first — everything else reads it)
# 2. REST API (uvicorn hesabyar.interfaces.rest.app:app)
# 3. Worker (scripts/run_event_worker.py)
# 4. MCP server (python -m hesabyar.interfaces.mcp --transport streamable-http)
```

**Step 9 — Verify before declaring the upgrade done.**

```bash
curl -s http://127.0.0.1:8001/api/v1/readiness   # {"status": "ready"}
curl -s http://127.0.0.1:8001/api/v1/installation \
  -H "Authorization: Bearer $API_KEY"
#   — Alembic revision == the new head, Tryton modules == the release's set
curl -s http://127.0.0.1:8002/health              # {"status": "ok", "service": "hesabyar-mcp"}
```

Then the smoke checks (the same list as the post-restore smoke
in `docs/backup-restore.md` §9.1 step 6):

- one report (trial balance for the current fiscal year);
- one webhook (replay one recent event or confirm the inbox is
  draining — the worker's `events.sweep` runs every minute, so
  the queue depth in `/monitoring` should fall);
- one MCP call (any read tool through an authorized client);
- the admin panel loads and the dashboard renders;
- `POST /api/v1/monitoring/evaluate` (or the panel's
  «ارزیابی اکنون») — no new critical alert;
- the review queue (`/review-queue`) shows no new failure items
  from the upgrade window.

Only when all of §3 step 9 passes is the upgrade DONE. Announce
the end of the window, and keep the §2.3 record + the §3 step 3
backup keys with the release notes until the release is declared
stable (at least one daily backup cycle later).

## 4. Rollback, per layer

Roll back in REVERSE order of §3: frontend, Tryton, schema, code.
Stop the writes first (steps 1–2 of §3) before touching any
layer — a rollback into a live system races with new writes.

### 4.1 Code rollback

```bash
git checkout vX.Y.(Z-1)        # the previous release tag (§2.3 record)
uv sync --frozen
# Restart the API / worker / MCP (step 8 order).
```

Code rollback alone is safe ONLY when the schema and the modules
did not change in the release. When they did, roll those layers
back first (§4.2–§4.3) — old code against a new schema is an
untested combination, and the old code may not even start.

### 4.2 Migration downgrade (HESABYAR schema)

```bash
uv run alembic current         # the new head
uv run alembic downgrade <previous-revision>
uv run alembic current         # the previous head — must match §2.3
```

The mechanism is proven in CI: `tests/infrastructure/
test_migration_rollback_us705.py` downgrades 0042 → 0041 and
re-upgrades to head on a scratch database (tables + the partial
unique index drop and return).

**The data-safety rule — read this before any downgrade:**

> **Never roll back a migration that destroyed data without a
> backup restore.** A `downgrade()` that drops a table or a column
> DESTROYS the data in it — the downgrade succeeds and the data is
> gone. Downgrading 0042 drops the monitoring tables (alerts,
> channels, token records): acceptable only when losing them is
> acceptable, i.e. when they can be recreated (channels + token
> records are operator config — re-enter them; alerts are history
> — accept the loss explicitly, or restore from backup instead).

Before downgrading, READ the migration's `downgrade()`:

- drops nothing (adds a column, creates an index, adds a
  constraint) → the downgrade is non-destructive; proceed.
- drops a table/column, or alters a column in a way that loses
  values → the downgrade is DESTRUCTIVE. The safe rollback is a
  **backup restore** (§4.5), not a downgrade. Downgrade only when
  the data loss is explicitly accepted and recorded (who decided,
  what is lost) — never silently.

A migration with no `downgrade()` (or a `downgrade()` that raises)
cannot be rolled back at all: backup restore is the only path.
Releases whose migrations are destructive or irreversible say so
in their release notes (`docs/release.md` §3).

### 4.3 Tryton module rollback — backup restore only

**Tryton has no module downgrade.** `trytond-admin` cannot
uninstall a module version or revert a module update; there is no
`downgrade` for the Tryton layer. A Tryton-layer rollback is:

1. Restore the Tryton database from the §3 step 3 pre-upgrade
   backup (`docs/backup-restore.md` §9 — stop the writes first,
   drop + recreate, `pg_restore`).
2. Reinstall the PREVIOUS module package versions into the Tryton
   venv (the §2.3 record + the previous release's requirements).
3. Restart trytond and verify via REST (`ir.module` search) that
   the module versions match the previous release.

A code-only Tryton change (module code with no schema/XML change)
needs no database step: reinstall the previous wheel and restart
trytond — the pool loads the new code at startup
(`docs/tryton-modules.md`, activation record). When in doubt
whether a module change touched schema or data, assume it did and
restore.

### 4.4 Frontend rollback

```bash
git checkout vX.Y.(Z-1)        # the previous release tag
cd apps/admin
pnpm install --frozen-lockfile
pnpm build
# Restart the admin process.
```

The frontend holds no state of its own (the BFF holds keys
server-side; sessions are in-memory and a restart clears them),
so a frontend rollback is always safe.

### 4.5 Full rollback (backup restore)

When the schema rollback is destructive (§4.2), when the Tryton
layer must roll back (§4.3), or when the database is in any doubt
at all: restore BOTH databases from the §3 step 3 pre-upgrade
backup, then roll the code back (§4.1). The restore procedure is
`docs/backup-restore.md` §9 — including the rule that the
pre-upgrade backup predates the upgrade, so any data written
between the backup and the rollback decision is LOST (that is
why the writes stop for the whole window: nothing should be
written after the backup except the upgrade itself).

After ANY rollback: re-run §3 step 9 (verification) against the
rolled-back system before reopening it. A rollback that was not
verified is a second outage waiting to happen.

## 5. Rollback decision criteria

| Situation | Decision |
|---|---|
| `alembic upgrade head` fails and the cause is not clear within the window | Roll back (§4) — do not debug a half-migrated production database under time pressure |
| Readiness stays `not_ready` > 15 minutes after restart | Roll back — the new release does not run in this environment |
| Smoke checks fail on a CORE flow (reports, webhooks, MCP) | Roll back — the release is broken for daily work |
| Smoke checks fail on ONE non-critical feature, core flows green | Roll FORWARD: declare the upgrade done, file the bug, fix in a patch release (a rollback costs a second outage for a feature nobody uses at 3 a.m.) |
| Data corruption is suspected (trial balance no longer balances, tranche tie-out fails) | STOP. Do not downgrade, do not restore yet. Freeze the system (writes stopped), preserve the evidence (logs, the pre-upgrade backup, a fresh dump of the current state), and escalate to the developer. A restore destroys the evidence of what went wrong |
| The upgrade ran fine but the release is wrong (a bad feature, a wrong default) | Roll forward with a patch release, unless the feature corrupts data — then §4 |
| Tryton module update fails halfway | Restore the Tryton database from the pre-upgrade backup (§4.3) — there is no partial-module-update state worth keeping |

When the decision-maker cannot be reached and the system is down:
roll back. A running old version beats a broken new one, and the
rollback is reversible (the release can be re-attempted).

## 6. Data-safety rules (summary)

1. A dedicated pre-upgrade backup, taken after the writes stop,
   is mandatory (§3 step 3). The daily backup is not a
   substitute.
2. Never downgrade a destructive migration without a backup
   restore (§4.2). When in doubt, restore.
3. Never restore into a live system — stop the writes first
   (`docs/backup-restore.md` §9).
4. Never hand-edit either database to "fix" an upgrade. Fix
   through migrations, module updates, or the application — a
   hand edit is unrepeatable and unverifiable.
5. Keep the pre-upgrade backup until the release is stable (at
   least one daily backup cycle). Retention prunes by age
   (30 days + 12 monthly keeps), not by release — the backup
   survives on its own; do not delete it early.
6. Record every upgrade and rollback: version from/to, revisions,
   backup object keys, who decided, what went wrong. The next
   upgrade reads the last one's record.

## 7. What is NOT tested (honest limits)

- **The full upgrade procedure has never run against a
  production installation** — no production server exists yet
  (US-706). The procedure is written against the CI/dev install
  and the documented commands; the first production upgrade is a
  rehearsal-first event (§1 step 4), not a routine run.
- **The migration downgrade mechanism IS tested** (0042 → 0041 →
  head on a scratch DB, in CI) — but only for 0042. Each future
  release's own downgrade is reviewed in its release checklist
  (`docs/release.md` §3), not re-proven by a new test per
  migration; a release whose migration cannot downgrade says so
  in its notes.
- **Tryton module rollback via backup restore is untested
  end-to-end in CI**: the restore drill proves restores, and the
  module reinstall is the documented `trytond-admin` path, but
  no CI job performs the combined restore + reinstall sequence.
  It is a staging-rehearsal item, not a CI item.
- **The pre-upgrade checks are tested with fakes** (unit tests),
  not against a live installation: the backup-status read, the
  Tryton probe and the Alembic read each have live coverage
  elsewhere (US-703/US-704), but the combined CLI run against a
  real deployment is a staging item.
