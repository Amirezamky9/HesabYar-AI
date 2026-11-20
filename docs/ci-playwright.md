# Playwright in CI — browser e2e on GitHub Actions

GitHub Actions runs the **full** Playwright suite
(`.github/workflows/ci.yml`, job `playwright`) against a disposable
stack built on the runner: a fresh Tryton 8.2 server (same build as
the `python-tests-tryton` job), a migrated HesabYar DB, the FastAPI
backend, and the Next.js production server. The specs run against a
local database, so a page load costs milliseconds — not the 13–19s
per backend call the remote Supabase dev DB needs (which is what
made the dashboard-widgets test flaky locally).

## How it works

The job builds the stack in order:

1. **Checkout + toolchains** — Python 3.12 + `uv sync`, Node 24 +
   `pnpm install --frozen-lockfile` (apps/admin).
2. **HesabYar database** — the `hesabyar` role + `hesabyar_test`
   database on the job's `postgres:17` service (same as the tryton
   job).
3. **Tryton** — `scripts/ci/setup-tryton.sh` (see
   `docs/ci-tryton.md`): trytond 8.2.3 + the 41-module set, the
   Adapter Test company + chart, FY 2026 + FY 2027, packaging and
   cash-box data, trytond on `:8000`.
4. **Playwright stack** — `scripts/ci/setup-playwright.sh` (below).
5. **Browsers** — `pnpm exec playwright install --with-deps chromium`.
6. **The suite** — `pnpm exec playwright test --config
   playwright.ci.config.ts` (the FULL suite, no file filter).
7. **Artifacts on failure** — `test-results/`,
   `playwright-report/`, the backend/Next/seed logs and the trytond
   log (see below).

## What setup-playwright.sh does

`scripts/ci/setup-playwright.sh` provisions everything the specs
need on top of a running `setup-tryton.sh` server:

1. **Migrate** — `alembic upgrade head` on the HesabYar DB.
2. **Seed the registries** — the migrations create the registry
   tables but do NOT populate them, and the specs assert the live
   seeded values, so the script runs the three idempotent seed
   CLIs: `seed_standards` (35 standards, 36 rules on Standard 1),
   `seed_tax_policy` (5 gated 1405 parameters),
   `seed_payment_channels` for the dev installation id
   (`00000000-0000-0000-0000-000000000004` — the id the mint script
   issues the dev key under; the settlement spec wires the
   `snapppay` built-in channel).
3. **US-304 seed** — `scripts/ci/seed_us304.py` via
   `trytond-console`, capturing the printed `US304_*` ids (party,
   revenue account, payment, statement). This is a VENDORED copy of
   `hesabyar-reference/seed_us304.py` (the dev source of truth,
   which lives outside the repo) — CI checks out only the repo, so
   the job needs this copy. When the seed changes, update BOTH
   copies. trytond-console exits 0 even on a traceback, so the
   captured output — not the exit code — is the gate: all four ids
   must be present and no Traceback may appear.
4. **E2E checkbook** — `scripts/ci/provision_checkbook.py` creates
   the bank + checkbook ("HesabYar E2E Checkbook", leaves 100–199)
   the cheques spec needs: the spec registers an ISSUED cheque
   through the UI form (which requires picking an existing
   checkbook) and the checkbooks page test expects a card — but the
   checkbooks page has no create UI, so a fresh database has none.
   Idempotent (matched by name).
5. **Restart trytond** — the server caches model data; the seed
   wrote rows directly through the console.
6. **Mint the dev key + `.env.local`** — runs
   `apps/admin/scripts/mint-dev-key.py` (`HESABYAR_ALLOW_DEV_KEY=1`)
   and writes `apps/admin/.env.local` (0600, gitignored) with the
   key + `TRYTON_BASE_URL` / `TRYTON_DATABASE` / `TRYTON_API_KEY` +
   the seed ids. The key secret is NEVER printed to the console:
   the mint output goes to a 0600 log file and only the key prefix
   is shown. (Note for future maintainers: the mint script indents
   the key line by 4 spaces, so the extraction grep must not anchor
   to the start of the line.)
7. **Backend** — starts `uvicorn` on `:8001`, waits for
   `/api/v1/health`, then probes `GET /api/v1/companies` with the
   minted key (a stale/wrong key is a 401 that otherwise only shows
   up at login time).
8. **Next production server** — `pnpm run build`, then `pnpm start`
   on `:3000` with `HESABYAR_API_BASE_URL` pointing at the backend,
   waits for `/login`. The PRODUCTION server is deliberate: `pnpm
   dev` does not hydrate in this environment.

Pids + logs live in `.playwright-data/` (gitignored). The servers
are left running for the Playwright run.

## The CI Playwright config

`apps/admin/playwright.ci.config.ts` — same specs, reporters and
timeouts as the dev config (`playwright.config.ts`), with two
deliberate differences:

1. **No `webServer` block.** The setup script starts `pnpm start`
   explicitly and waits for it. The dev config's `pnpm dev` server
   does not hydrate here, and auto-starting it would hide a failed
   production server behind a dev-server fallback. If the server is
   not up, the specs fail fast with a connection error.
2. **`baseURL` from `PLAYWRIGHT_BASE_URL`** (default `:3000`), so a
   local run can point at a non-default Next port without editing
   any config.

## First-run verification

**The first CI run IS the verification.** No test runs are possible
on the dev VM by standing rule (user order 2026-11-20: no pytest,
vitest, Playwright or local stack runs on the VM at all), so this
job was validated STATICALLY only: `bash -n` on the scripts,
`ruff` on the new Python files, `tsc` + `eslint` on the new
config, YAML validity of `ci.yml`, and a logic review of every step
against the already-proven `setup-tryton.sh`, `seed_us304.py` and
`mint-dev-key.py`. Nothing here has been executed end to end yet.

The job therefore runs the **FULL suite (87 tests, 18 spec files)**
with no file filter. Read the first run's results, then:

- If specs fail on stale expectations (see "Expected first-run
  risks" below), fix the expectations — the same class of fix as
  the CI run #1 test fixes — and re-run.
- If a spec fails for a real product reason, file it as a story
  follow-up; do NOT loosen timeouts or assertions to force green.
- If a spec cannot run in CI at all (missing data, environment
  need), remove it from the job's invocation with a comment citing
  the reason, and list it in "Known gaps" below.

## Expected first-run risks (static analysis, UNVERIFIED)

These were found by reading the specs against the current code and
chart — they are predictions, not measured failures. The first CI
run confirms or refutes each one.

### dashboard.spec.ts — stale hardcoded constants (RESOLVED by US-551)

The spec was written at US-302 time, when the chart had 135
accounts and only FY 2026 existed. US-551 (Phase 5-6) rewrote the
spec for the unified dashboard with the current constants, verified
against the code (chart XML: 18 template accounts, 15 typed, 6
receivable; `provision_fy2027.py`: FY 2027 = 2027-01-01..2027-12-31;
`list_fiscal_years` newest-first). The table below is the record of
what changed; the spec now asserts the live values.

| Old assertion (US-302 spec) | Live value on a fresh DB | Why |
| --- | --- | --- |
| `۱۳۵ حساب نصب‌شده` (setup widget) | `۱۵۳ حساب نصب‌شده` | Chart grew: 135 + 18 template accounts (`hesabyar_setup` `account_chart_en.xml`) |
| `۱۳۵ حساب · ۹۹ حساب قابل‌ثبت` (accounts page, 3 places) | `۱۵۳ حساب · ۱۱۴ حساب قابل‌ثبت` | 15 of the 18 added accounts carry a type (postable); the 3 `7.x` groups are typeless |
| `۱ حساب از ۱۳۵ حساب` (code search) | `۱ حساب از ۱۵۳ حساب` | Total changed; the match count (1) is unchanged |
| `۳ حساب از ۱۳۵ حساب` (receivable filter) | `۹ حساب از ۱۵۳ حساب` | 6 added accounts use the `Accounts receivable` type (1.2.4/1.2.5/1.2.6/1.2.7/1.2.8/1.2.9) |
| `۱۲ دوره` (setup widget) / `۱۲ دورهٔ باز` (setup tab) | `۲۴ دوره` / `۲۴ دورهٔ باز` | `open_period_count` counts ALL open periods: FY 2026 (12) + FY 2027 (12) |
| `دوره: سال مالی FY 2026` (widget meta) | `دوره: سال مالی FY 2027` | `open_fiscal_year` is the NEWEST open year (`SetupStatusService`) |
| Periods tab shows `2026-01`/`2026-12` | Defaults to FY 2027 | The tab selects the first open year (newest first); the test picks FY 2026 from `#fiscal-year-select` for the FY 2026 assertions |
| `استانداردها و انطباق` is a DISABLED coming-soon nav item | It is a live nav button | The compliance center shipped in US-405; the live list now also includes مرکز چک، مرکز اقساط، مرکز تسویه، انبار و بسته‌بندی |

The FY 2026 Jalali dates (`۱۱ دی ۱۴۰۴` / `۱۰ دی ۱۴۰۵`) and the
`آمادهٔ ثبت اسناد` setup state are unchanged. The dev DB's counts
differ again (157 accounts — 4 duplicate cash-box codes from
repeated provisioning), so a hardcoded count can only ever be right
for ONE database; the fresh CI database is the source of truth.
The rewritten spec also raises the file timeout to 180s (the
unified dashboard fires ~16 API calls; the batch took ~105s
against remote Supabase in ۵ث).

### journey.spec.ts — stale report coming-soon tiles (US-550 follow-up, likely failure)

`journey.spec.ts` asserts `report-coming-soon-trial-balance` and
`report-coming-soon-ledger` are visible on the reports page, but
US-550 replaced those tiles with the live financial statements —
no `report-coming-soon-*` testId exists in the app anymore. The
journey spec needs a US-550 follow-up to assert the live report
tabs instead. (Found during US-551; not fixed there — it is
US-550's area.)

### Full-suite cross-spec interference (unknown)

Each spec file was previously run standalone or in small groups
against a shared dev DB. In CI all 18 files run serially (1
worker) against ONE fresh DB. Specs that create data use unique ids
per run (cheques: timestamp Sayyad ids; settlement: timestamp
transaction ids; journey: timestamped descriptions), and the seeds
are idempotent — but no full-suite run has ever happened, so
interference (e.g. a spec asserting an empty state that an earlier
spec populated) is possible. The compliance spec, for example,
asserts "هنوز اعتبارسنجی‌ای اجرا نشده است" (no validation run yet)
— true on a fresh DB, but only if no earlier spec ran a validation.

### Settlement spec duration

`settlement.spec.ts` has a 45-minute test timeout: against remote
Supabase the 4-tranche scenario took ~23 min, mostly Tryton
round-trips. On the runner's local DB it should be much faster,
but the deposit chain is still synchronous (~11 min was measured
for it once — a known deferred item: make it async). The job's
120-minute bound covers it; if the spec times out in CI, that is
the first place to look.

### `window is not defined` in the cheque reports SSR chunk

The parent's Next log showed a server-side `ReferenceError: window
is not defined` from the cheque-reports page chunk at startup. The
reports test passed anyway (client render recovers), and it is a
server-side error, not a browser console error — but specs that
collect console errors (journey, compliance) would fail if it ever
surfaced in the browser. Worth a look if those specs fail on
console errors.

## Running it locally (reference)

Local test runs are currently forbidden by standing rule; this
section is for when that changes, or for debugging the scripts
themselves. Use your own ports and DB names — never the parent's
`:8001`/`:3000`/`:8010`/`:8011` or the shared dev DBs:

```bash
export PATH="$HOME/.local/bin:$PATH"   # uv + pnpm live here
# 1. Tryton (fresh DB + port; trust auth on the local :5433 cluster)
TRYTON_ENV=/tmp/tryton-pw-env TRYTON_DATA=/tmp/tryton-pw-data \
  TRYTON_DB=hesabyar_tryton_ci_pw TRYTON_PORT=8022 \
  PG_PORT=5433 TRYTON_DB_ROLE=postgres TRYTON_DB_PASSWORD= \
  FIND_LINKS=~/workspace/projects/hesabyar-reference/wheels-us502 \
  bash scripts/ci/setup-tryton.sh
# 2. HesabYar DB (fresh, migrated by the script)
createdb -h 127.0.0.1 -p 5433 -U postgres hesabyar_ci_pw_test
# 3. Playwright stack (own ports)
TRYTON_ENV=/tmp/tryton-pw-env TRYTON_DATA=/tmp/tryton-pw-data \
  TRYTON_DB=hesabyar_tryton_ci_pw TRYTON_PORT=8022 \
  HESABYAR_DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:5433/hesabyar_ci_pw_test \
  BACKEND_PORT=8002 NEXT_PORT=3001 \
  bash scripts/ci/setup-playwright.sh
# 4. Specs
cd apps/admin
PLAYWRIGHT_BASE_URL=http://127.0.0.1:3001 \
  pnpm exec playwright test --config playwright.ci.config.ts
```

Teardown: kill the pids in `.playwright-data/*.pid` and
`$TRYTON_DATA/trytond.pid` (or `fuser -k <port>/tcp` per port:
8022 trytond, 8002 backend, 3001 Next), then drop the two DBs
(`DROP DATABASE hesabyar_tryton_ci_pw; DROP DATABASE
hesabyar_ci_pw_test;`).

## Known gaps

- **First-run verification pending** — see above. The spec list
  (currently: the full suite) will be trimmed from CI results if
  needed.
- **The dev DB's polluted counts** — the dev DBs have 157 accounts
  (4 duplicate cash-box codes) vs the CI DB's clean 153. Any spec
  with a hardcoded chart count can only be right for one of them;
  the CI database is the source of truth. Cleaning the dev DB
  duplicates is a separate task.
- **`seed_us304.py` lives in two places** — the dev copy
  (`hesabyar-reference/seed_us304.py`, outside the repo) and the
  vendored CI copy (`scripts/ci/seed_us304.py`). Keep them in sync;
  the CI copy's header says so.
- **No Playwright shard** — the job runs all 87 tests serially on
  one worker (the specs share one DB, so parallel workers would
  collide). If the suite grows slow, shard by spec file across
  jobs, each with its own DB — not by raising the worker count.

## Files

| File | Purpose |
| --- | --- |
| `.github/workflows/ci.yml` | `playwright` job (5th job) |
| `scripts/ci/setup-playwright.sh` | Seed + mint + servers (this doc's §2) |
| `scripts/ci/seed_us304.py` | Vendored US-304 seed (Tryton fixtures) |
| `scripts/ci/provision_checkbook.py` | E2E checkbook for the cheques spec |
| `apps/admin/playwright.ci.config.ts` | CI Playwright config (no webServer) |
| `docs/ci-tryton.md` | The Tryton build the job reuses |
