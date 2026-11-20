# Tryton in CI — full pytest suite on GitHub Actions

GitHub Actions runs the **full** Python test suite including the
live-Tryton contract tests (`.github/workflows/ci.yml`, job
`python-tests-tryton`). The job builds a disposable Tryton 8.2 server
from scratch on the runner — same modules, same chart, same test data
as the dev databases — then runs `pytest -q` with `TRYTON_BASE_URL`
pointing at it.

The sibling `python-tests` job keeps running the same suite without
Tryton (contract + live-e2e tests skip there via their
skip-when-unreachable fixtures). The trytond job is the authoritative
full run.

## How it works

`scripts/ci/setup-tryton.sh` (run by the job after checkout + `uv sync`
+ Alembic) does, in order:

1. **Tryton venv** — `uv venv --python 3.12`, installs trytond 8.2.3 +
   the pinned module packages from `scripts/ci/tryton-requirements.txt`
   (PyPI), plus the two local modules built from `tryton-modules/`
   (`hesabyar_setup` 8.2.3, `account_check_ir` 8.2.0).
2. **Database** — creates the `tryton` role + `hesabyar_tryton_test`
   database on the job's `postgres:17` service (superuser `postgres`).
   Refuses to touch a database that already has Tryton tables unless
   `TRYTON_DB_RESET=1` (and then only for names containing `test`/`ci`).
3. **Init + modules** — `trytond-admin --all`, `-m` (refresh module
   list), then `scripts/activate-tryton-modules.sh` activates the full
   **41-module** set (Phase 0 + F2 sale/stock/clearing/dunning +
   `account_check_ir` + transitive deps). The script's pytest guard is
   skipped here (`HESABYAR_SKIP_PYTEST_GUARD=1`) because the database
   is fresh and no pytest process touches it.
4. **Server + REST key** — starts trytond on `:8000`, creates the REST
   application key `hesabyar-dev-rest-key` via JSON-RPC
   (`scripts/ci/create_rest_key.py`).
5. **Test data** (all via `trytond-console`, all idempotent):
   - `provision_adapter_test_company.py` — "HesabYar Adapter Test"
     company + universal chart + FY 2026 (12 open periods) + invoice
     sequences + general journal. In-repo copy of the dev script.
   - `update_chart.py` — `hesabyar_setup.update_chart`: adds the
     HesabYar extension accounts (cash-box, notes, bounced cheques,
     settlement, memorandum).
   - `provision_fy2027.py` — FY 2027 + 12 open periods + move
     sequence + invoice-sequence row (the settlement contract tests
     clear/settle Jan/Feb 2027 tranches and skip without it).
   - `provision_packaging.py` — US-515: PACK / PACK-CONSUME
     locations, packaging category + accounting, analytic root,
     anglo-saxon on open fiscal years, supply cron.
   - `provision_cashbox_journals.py` — US-516: the two statement
     journals (Store / Petty Cash Box).
6. **Restart + verify** — restarts trytond (clears the server cache),
   then asserts: 41 activated modules, ≥150 chart accounts for the
   test company, all 11 extension codes present, 12 open FY 2027
   periods, 2 cash-box journals, REST key present, and a live REST
   probe (Bearer key lists the test company).

## Environment variables — three families must agree

Three env families point at Tryton, and a test file reads only one
of them. If any family is missing or points elsewhere, the affected
tests **skip silently** (skip-when-unreachable fixtures) — the suite
stays green while covering nothing:

| Family | Used by | CI value |
| --- | --- | --- |
| `TRYTON_BASE_URL` / `TRYTON_DATABASE` / `TRYTON_API_KEY` | Most contract files (`tests/contract/*`) | `http://localhost:8000` / `hesabyar_tryton_test` / `hesabyar-dev-rest-key` |
| `HESABYAR_TRYTON__BASE_URL` / `HESABYAR_TRYTON__DATABASE` / `HESABYAR_TRYTON__API_KEY` | The REST app + `AppSettings`-based tests (cashbox contract file, all live-e2e `tests/interfaces/*`) | Same server/DB/key |
| `TRYTON_DB` / `TRYTON_REST_KEY` | The IRR/Jalali fixture (`tests/infrastructure/test_tryton_irr_jalali.py`) | Same DB/key |

The workflow sets all three in the job `env:` block. Locally, set
all three too (see below) — the first local run of the cashbox file
skipped all 13 tests because only `TRYTON_BASE_URL` was set.

`HESABYAR_DATABASE_URL` must use the **`postgresql+psycopg://`**
scheme. A bare `postgresql://` URL makes SQLAlchemy default to the
`psycopg2` driver, which is not installed (only psycopg 3 is) — both
Alembic and the settlement contract tests then fail with
`ModuleNotFoundError: No module named 'psycopg2'`. The app's own
settings convert the scheme, but Alembic's `env.py` and the
settlement fixture use the raw env var.

## What the tests provision themselves (not in the setup)

- **"HesabYar IRR Test" company + IRR digits=0** — created by the
  `tests/infrastructure/test_tryton_irr_jalali.py` fixture itself via
  REST. The setup deliberately does NOT provision it: the fixture is
  idempotent and owns that company. Consequence: the cross-company
  isolation test in `test_tryton_adapter.py` skips by design in CI
  ("no other company in the DB") because pytest collects
  `tests/contract/*` before `tests/infrastructure/*`, so the IRR
  company does not exist yet when it runs.
- **Settlement accounts via update_chart** — the settlement contract
  tests re-run `update_chart` in their own fixture; the setup's
  `update_chart` step covers the cash-box tests, which only check.
- **Checkbooks / cheques** — the cheque contract tests create and
  delete their own checkbooks, banks and cheques via REST.
- **US-304 seed data** (party / revenue account / payment / statement)
  is Playwright-only, not pytest — it is deliberately not provisioned
  here (see Known gaps).

## Gotchas found while building this

- **`company_filter='all'` is mandatory.** The admin user's
  `company_filter` defaults to `'one'`; with no current company,
  `User.get_companies()` returns `[]` and every company-scoped read
  comes back empty (the chart and fiscal-year lists look empty, and
  most contract tests skip with "Chart accounts missing"). The dev
  DBs got `'all'` implicitly from the IRR fixture; a fresh database
  must set it explicitly — `provision_adapter_test_company.py` does
  that in `grant_admin_access`, and the setup's verification step
  asserts it.
- **Chart count: 153 is the clean number.** The dev DBs show 157
  accounts for the Adapter Test company because four cash-box codes
  (`1.1.5`, `1.2.6`, `1.2.7`, `2.1.6`) exist twice there — a residue
  of repeated provisioning runs. The CI database has the same 153
  unique accounts with no duplicates (the often-quoted "159" is
  157 + the IRR Test company's 2 accounts). The setup asserts
  ≥150, not an exact count, so small chart growth does not break it.
- **`company.company` has no `name` field.** A REST probe requesting
  `f=name` on it is a server 500 (`KeyError: 'name'`) — names live
  on `party.party`. The setup's REST probe uses `party.party`.
- **Restart trytond after console provisioning.** The server caches
  model data; the setup restarts it before verification.

## Running it locally

```bash
# Needs a local PostgreSQL server (any version 16+; the dev cluster on
# :5433 works) and uv + psql on PATH. Uses a FRESH database + port so
# it never touches the dev databases or the running trytond processes.
TRYTON_ENV=/tmp/tryton-ci-env TRYTON_DATA=/tmp/tryton-ci-data \
  TRYTON_DB=hesabyar_tryton_ci TRYTON_PORT=8021 \
  PG_PORT=5433 TRYTON_DB_ROLE=postgres TRYTON_DB_PASSWORD= \
  bash scripts/ci/setup-tryton.sh
# Then (all three env families — see above):
TRYTON_BASE_URL=http://localhost:8021 TRYTON_DATABASE=hesabyar_tryton_ci \
  TRYTON_API_KEY=hesabyar-dev-rest-key \
  HESABYAR_TRYTON__BASE_URL=http://localhost:8021 \
  HESABYAR_TRYTON__DATABASE=hesabyar_tryton_ci \
  HESABYAR_TRYTON__API_KEY=hesabyar-dev-rest-key \
  TRYTON_DB=hesabyar_tryton_ci TRYTON_REST_KEY=hesabyar-dev-rest-key \
  uv run pytest tests/contract/test_tryton_adapter.py -q
# Tests that also touch the HesabYar DB (settlement) need a fresh,
# migrated HesabYar DB too:
#   createdb hesabyar_ci_test
#   HESABYAR_DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:5433/hesabyar_ci_test \
#     uv run alembic upgrade head
```

On the dev VM, PyPI is very slow — pass the local wheel cache to reuse
the US-502 wheels instead of downloading them:
`FIND_LINKS=~/workspace/projects/hesabyar-reference/wheels-us502`.
Re-running the script against an existing venv skips the PyPI install
(the local modules are still rebuilt from the checkout).

To tear down: kill the trytond pid in `$TRYTON_DATA/trytond.pid` and
drop the database (`DROP DATABASE hesabyar_tryton_ci;`).

## Known gaps

- **Playwright is not in CI yet.** The admin UI e2e specs need the
  US-304 seed, a running backend + `pnpm start` server, and a minted
  dev API key — none of that is wired into the workflow. The pytest
  suite is the CI gate for now.
- **trytond-cron does not run in CI.** The supply cron
  (`stock.order_point|supply_stock`) is provisioned but never fires;
  the packaging tests only read its configuration, so this is fine.
- **No seeded HesabYar dev data.** The CI HesabYar DB is a fresh
  Alembic head; tests create their own fixtures. Anything that
  depends on the dev DB's hand-built rows (e.g. the US-304 seed ids
  in `apps/admin/.env.local`) does not apply here.
- **Setup time.** Measured locally on the dev VM (with the wheel
  cache): the full setup — venv install + init + 41 modules +
  provisioning + verification — takes **~110s** end to end. On
  GitHub runners PyPI is fast, so install is ~1–2 min and activation
  ~2–4 min. If it ever gets slow, the install step is the place to
  cache (`actions/cache` on the venv, keyed by
  `scripts/ci/tryton-requirements.txt` + `tryton-modules/`).

## Local verification (2026-11-20)

Setup run against a fresh local database `hesabyar_tryton_ci` on
`:8021` (log `~/workspace/coord-logs/ci-tryton-setup-local4.log`):
all 18 verification checks OK in 113s (41 modules, 153 chart
accounts, 12 FY 2027 periods, 2 cash-box journals, REST key,
`company_filter='all'`, REST probe). Targeted contract runs against
it — **142 passed, 1 skipped, 0 failed**:

| File | Result |
| --- | --- |
| `tests/contract/test_tryton_adapter.py` | 49 passed, 1 skipped (cross-company test, by design — see above) |
| `tests/contract/test_tryton_adapter_cashbox.py` | 13 passed |
| `tests/contract/test_settlement_posting.py` | 6 passed (needs a migrated HesabYar DB) |
| `tests/contract/test_tryton_packaging.py` + `test_tryton_cheques_us530.py` + `tests/infrastructure/test_tryton_irr_jalali.py` | 74 passed, 0 skipped |

The full suite was NOT run locally for this branch (a wave-end shard
was using shared resources); the first full CI run on the workflow
is the real end-to-end proof.
- **PostgreSQL version.** The trytond job uses `postgres:17-alpine`
  (matching the dev DBs, 17.12); the plain `python-tests` job still
  uses `postgres:16-alpine`. Both are fine for the HesabYar DB; only
  the Tryton job needs 17 for parity with dev.
- **The requirements files must stay in sync.** `scripts/ci/tryton-requirements.txt`
  is a copy of `hesabyar-reference/tryton-requirements.txt` (the dev
  source of truth, which lives outside the repo). When a pin changes,
  update both, and update `docs/tryton-modules.md`.

## Files

| File | Purpose |
| --- | --- |
| `scripts/ci/setup-tryton.sh` | Main setup script (see above) |
| `scripts/ci/tryton-requirements.txt` | Pinned PyPI packages |
| `scripts/ci/trytond.conf.template` | trytond config template |
| `scripts/ci/create_rest_key.py` | REST key via JSON-RPC (stdlib only) |
| `scripts/ci/provision_adapter_test_company.py` | Test company + chart + FY 2026 |
| `scripts/ci/update_chart.py` | HesabYar extension accounts |
| `scripts/ci/provision_fy2027.py` | FY 2027 + periods + sequences |
| `scripts/ci/provision_packaging.py` | US-515 packaging provisioning |
| `scripts/ci/provision_cashbox_journals.py` | US-516 cash-box journals |
| `scripts/activate-tryton-modules.sh` | Module activation (shared with dev; `HESABYAR_SKIP_PYTEST_GUARD=1` for CI) |
| `.github/workflows/ci.yml` | `python-tests-tryton` job |
