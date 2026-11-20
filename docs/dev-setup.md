# Development setup — HesabYar Phase 0

Local development environment for HesabYar: PostgreSQL 16 + Tryton 8.2 test
databases, plus the HesabYar Python project itself.

> **Scope note:** there is **no Docker** in this VM. Everything below is a
> native install (apt / pip / uv). All credentials on this page are
> **local-development-only** test values. Never reuse them in staging or
> production.

## 1. Layout

| Piece | Location |
| --- | --- |
| HesabYar repo | `~/workspace/projects/hesabyar/` (branch `ralph/phase-0`) |
| HesabYar Python venv | `~/workspace/projects/hesabyar/.venv` (managed by `uv`) |
| Tryton venv (isolated, **outside** the repo) | `~/workspace/projects/hesabyar-reference/tryton-env/` |
| Tryton config | `~/workspace/projects/hesabyar-reference/trytond.conf` |
| Tryton pinned requirements | `~/workspace/projects/hesabyar-reference/tryton-requirements.txt` |
| Tryton filestore / data | `~/workspace/projects/hesabyar-reference/tryton-data/` |
| Tryton admin passfile (chmod 600) | `~/workspace/projects/hesabyar-reference/tryton-passfile` |
| Tryton server log | `~/workspace/projects/hesabyar-reference/trytond.log` |

Tryton lives in its own venv because it has its own dependency set; the
only Tryton code inside the HesabYar repo is the `hesabyar_setup`
module source at `tryton-modules/hesabyar_setup/` (US-502) plus this
document and `docs/tryton-modules.md`.

## 2. PostgreSQL 16

Installed natively from Ubuntu 24.04 apt: **PostgreSQL 16.15**
(`postgresql` + `postgresql-contrib`).

> **VM replacement wipes PostgreSQL.** Runtime deployments replace the VM
> roughly hourly: apt packages and everything under `/var` (including the
> PostgreSQL binaries and data directory) disappear, while `~` survives.
> After a replacement, **both databases are gone** and must be rebuilt from
> scratch — the Tryton venv, configs and scripts in `~` survive, the data
> does not. One-command recovery (reinstalls PostgreSQL, recreates both
> DBs, re-inits Tryton, re-creates the REST key, re-runs Alembic,
> re-provisions the Adapter Test company, restarts trytond, runs pytest):
>
> ```bash
> bash ~/workspace/projects/hesabyar-reference/restore-dev-env.sh
> ```
>
> Recovery verified 2026-11-16: full suite back to 250 passed. Gotchas the
> script handles: `trytond-console` needs the `--lock-table
> account_fiscalyear account_period account_journal account_configuration`
> flags or `FiscalYear.create` raises `_TransactionLockError` (and the
> console still exits 0 while rolling back); trytond must be restarted
> after console writes or its server cache hides the new company.

### Start / stop

```bash
pg_ctlcluster 16 main start     # start
pg_ctlcluster 16 main stop      # stop
pg_lsclusters                   # status (cluster "16 main", port 5432)
```

Note: the package's auto-start is blocked by `policy-rc.d` in this VM, so
the cluster must be started manually after a reboot (or with the commands
above). The cluster listens on `localhost:5432`.

### Databases and roles

Per design-eng spec §7.1 the Tryton database and the HesabYar database are
**separate databases with separate roles and passwords**, on the same local
server:

| Database | Role | Password | Purpose |
| --- | --- | --- | --- |
| `hesabyar_test` | `hesabyar` | `hesabyar_pass` | HesabYar test DB (Alembic-owned) |
| `hesabyar_tryton_test` | `tryton` | `tryton_pass` | Tryton test DB (Tryton-owned) |

Created with:

```sql
CREATE ROLE hesabyar LOGIN PASSWORD 'hesabyar_pass';
CREATE DATABASE hesabyar_test OWNER hesabyar ENCODING 'UTF8';
CREATE ROLE tryton LOGIN PASSWORD 'tryton_pass';
CREATE DATABASE hesabyar_tryton_test OWNER tryton ENCODING 'UTF8';
```

Verify connectivity:

```bash
PGPASSWORD=hesabyar_pass psql -h 127.0.0.1 -U hesabyar -d hesabyar_test -c 'SELECT 1;'
PGPASSWORD=tryton_pass psql -h 127.0.0.1 -U tryton -d hesabyar_tryton_test -c 'SELECT 1;'
```

### Test database URL

`tests/infrastructure/test_postgres.py` reads the URL from
`HESABYAR_DATABASE_URL`, defaulting to
`postgresql+psycopg://hesabyar:hesabyar_pass@hesabyar-postgres:5432/hesabyar_test`.
The hostname `hesabyar-postgres` cannot be added to `/etc/hosts` in this VM
(`/etc/hosts` is a read-only squashfs mount), so run pytest with the
override pointing at localhost:

```bash
HESABYAR_DATABASE_URL="postgresql+psycopg://hesabyar:hesabyar_pass@127.0.0.1:5432/hesabyar_test" \
  uv run pytest
```

(Export the variable in your shell profile if you want plain `uv run pytest`
to work.) No repo code change was needed: the test file already honours the
env var, and `src/hesabyar/infrastructure/config.py` applies the same
override to `AppSettings`.

## 3. Tryton 8.2

### Pinned versions

Verified against PyPI on 2026-11-16. `trytond` **8.2.3** is the latest 8.2.x
patch release; Tryton releases each module independently, so module packages
carry their own patch levels. Pin source: `https://pypi.org/pypi/<pkg>/json`
and `https://docs.tryton.org/8.2/`.

| Package | Version | Provides |
| --- | --- | --- |
| `trytond` | 8.2.3 | Server core |
| `trytond-account` | 8.2.2 | `account` |
| `trytond-account-invoice` | 8.2.0 | `account_invoice` |
| `trytond-account-payment` | 8.2.0 | `account_payment` |
| `trytond-account-statement` | 8.2.0 | `account_statement` |
| `trytond-account-asset` | 8.2.0 | `account_asset` |
| `trytond-analytic-account` | 8.2.0 | `analytic_account` |
| `trytond-company` | 8.2.0 | `company` |
| `trytond-currency` | 8.2.0 | `currency` |
| `trytond-party` | 8.2.1 | `party` |
| `trytond-product` | 8.2.1 | `product` |
| `psycopg[pool,binary]` | 3.3.9 | PostgreSQL driver (`trytond`'s `postgresql` extra; not a default dependency) |

Phase 5 (US-502, F2) adds the sale / stock / clearing / dunning family
(all 8.2.0 except `trytond-stock` 8.2.1) plus the local
`trytond-hesabyar-setup` 8.2.0 module — see
`docs/tryton-modules.md` for the full inventory with reasons, and
`tryton-requirements.txt` for the exact pins. The local module is
built from `tryton-modules/hesabyar_setup/` in this repo
(`python3 setup.py bdist_wheel`, then install the wheel into the
Tryton venv).

Install method (per the official 8.2 install docs, which list pip as the
supported non-Docker route): `pip install trytond==8.2.3
trytond-account==8.2.2 ...` — see `tryton-requirements.txt` for the exact
list. Python supported by Tryton 8.2: 3.10–3.14; this venv uses 3.12.

Rebuild the venv from scratch:

```bash
export PATH="$HOME/.local/bin:$PATH"   # uv lives here
uv venv --python 3.12 ~/workspace/projects/hesabyar-reference/tryton-env
uv pip install --python ~/workspace/projects/hesabyar-reference/tryton-env/bin/python \
  -r ~/workspace/projects/hesabyar-reference/tryton-requirements.txt
~/workspace/projects/hesabyar-reference/tryton-env/bin/trytond --version
# -> trytond 8.2.3
```

### Activated modules

The database `hesabyar_tryton_test` has the Phase 0 module set
activated (design-eng spec §4.1), plus the Phase 5 F2 set (US-502),
plus the transitive dependencies Tryton requires and the builtin core
modules. Full inventory with reasons: `docs/tryton-modules.md`.

- Phase 0: `account`, `account_invoice`, `account_payment`,
  `account_statement`, `account_asset`, `analytic_account`, `company`,
  `currency`, `party`, `product`
- Phase 5 F2: `sale`, `sale_shipment_cost`, `sale_payment`,
  `sale_discount`, `sale_amendment`, `sale_invoice_grouping`,
  `account_payment_clearing`, `account_dunning`,
  `account_dunning_fee`, `stock`, `stock_supply`, `purchase_request`,
  `stock_package`, `stock_shipment_cost`, `carrier`,
  `account_stock_landed_cost`, `account_stock_anglo_saxon`,
  `analytic_sale`, `account_statement_rule`, `hesabyar_setup`
- Pulled in as dependencies: `account_product` (by `account_invoice`),
  `bank` (by `account_payment`), `country` (by `party`), `purchase`
  (by `stock_supply`), `sale_history` (by `sale`),
  `account_invoice_stock` + `account_stock_continental` (by
  `account_stock_anglo_saxon`), `analytic_invoice` (by
  `analytic_sale`)
- Core: `ir`, `res`

No manufacturing, CRM, HR, warehouse or POS module is installed or
activated. Verify with:

```sql
SELECT name, state FROM ir_module ORDER BY name;  -- in hesabyar_tryton_test
```

### Configuration

`trytond.conf` sets:

```ini
[web]
listen = localhost:8000
[database]
uri = postgresql://tryton:tryton_pass@localhost:5432/
path = /home/hatch/workspace/projects/hesabyar-reference/tryton-data
list = True
```

### Initialise / reset the test database

```bash
cd ~/workspace/projects/hesabyar-reference
export TRYTONPASSFILE=$PWD/tryton-passfile   # admin password, non-interactive

# 1. (Re)create the empty database — only needed for a reset:
PGPASSWORD=tryton_pass dropdb -h 127.0.0.1 -U tryton hesabyar_tryton_test
PGPASSWORD=tryton_pass createdb -h 127.0.0.1 -U tryton hesabyar_tryton_test

# 2. Initialise (creates ir/res, sets the admin user):
./tryton-env/bin/trytond-admin -c trytond.conf -d hesabyar_tryton_test \
  --all --email admin@hesabyar.local

# 3. Refresh the module list, then activate the Phase 0 set:
./tryton-env/bin/trytond-admin -c trytond.conf -d hesabyar_tryton_test -m
./tryton-env/bin/trytond-admin -c trytond.conf -d hesabyar_tryton_test \
  -u account account_invoice account_payment account_statement account_asset \
     analytic_account company currency party product --activate-dependencies

# 4. Re-create the REST application key (see §4) — it lives in the DB, so a
#    reset wipes it. See the JSON-RPC snippet in §4.
```

Admin login: `admin` / password in `tryton-passfile` (`tryton-admin-dev`),
email `admin@hesabyar.local`.

### Start / stop the server

```bash
cd ~/workspace/projects/hesabyar-reference
nohup ./tryton-env/bin/trytond -c trytond.conf > trytond.log 2>&1 &
echo $! > trytond.pid   # note the PID for stopping
# stop:
kill "$(cat trytond.pid)"
```

The server listens on `http://localhost:8000` (werkzeug dev server — local
use only). Startup order per spec §15.3: PostgreSQL first, then the Tryton
DB init/upgrade, then the adapter health check, then HesabYar services.

## 4. Talking to Tryton (for adapter work, US-003+)

Verified live against this install. Two protocols, both on port 8000:

### JSON-RPC

Endpoints (note the `/rpc/` path — the legacy `/<db>/` POST path returns
405 in 8.2):

- `POST /rpc/` — server-level: `common.server.version` → `"8.2"`,
  `common.db.list` → `["hesabyar_tryton_test"]`
- `POST /<db>/rpc/` — database-level: `common.db.login`,
  `model.<model>.<method>`

Login:

```bash
curl -X POST http://localhost:8000/hesabyar_tryton_test/rpc/ \
  -H 'Content-Type: application/json' \
  -d '{"id": 1, "method": "common.db.login",
       "params": ["admin", {"password": "tryton-admin-dev"}]}'
# -> {"id":1,"result":[3,"<session-token>",null]}
```

Authenticated calls use a `Session` header — base64 of
`username:userid:session-token` — **not** Basic auth and **not** params:

```bash
HDR="Session $(printf 'admin:3:%s' "$TOKEN" | base64 -w0)"
curl -X POST http://localhost:8000/hesabyar_tryton_test/rpc/ \
  -H "Authorization: $HDR" -H 'Content-Type: application/json' \
  -d '{"id": 2, "method": "model.ir.module.search_read",
       "params": [[["state", "=", "activated"]], 0, null,
                  [["name", "ASC"]], ["name"], {}]}'
```

Model calls take a trailing `{}` context argument (omitting it returns 422
"Missing context argument"). `search_read` signature is
`(domain, offset, limit, order, fields_names)` — offset before limit.

### REST API

Routes live under `/<db>/rest/model/<name>` etc. REST auth is a **user
application key** (`res.user.application`, `application='rest'`,
state `validated`), sent as `Authorization: Bearer <key>` — a session token
does **not** work here.

A key is already provisioned for the admin user in the test DB:

- Key: `hesabyar-dev-rest-key` (dev-only)
- Example:

```bash
curl "http://localhost:8000/hesabyar_tryton_test/rest/model/res.user?f=login" \
  -H "Authorization: Bearer hesabyar-dev-rest-key"
# -> [{"login": "admin", "id": 3, "__name__": "res.user"}]
```

To re-create the key after a DB reset (using the Session header from above):

```bash
# create (state defaults to validated when created by an admin)
curl -X POST http://localhost:8000/hesabyar_tryton_test/rpc/ \
  -H "Authorization: $HDR" -H 'Content-Type: application/json' \
  -d '{"id": 3, "method": "model.res.user.application.create",
       "params": [[{"key": "hesabyar-dev-rest-key", "user": 3,
                    "application": "rest"}], {}]}'
```

REST query params use short names: `f=<field>` (repeatable) for fields,
`d=<base64url JSON domain>` for the domain, `s`/`p`/`o` for limit/offset/
order — see `trytond/protocols/rest.py` in the venv.

### REST client library (naiad) — open question for US-003

The official 8.2 docs point at the `naiad` REST client
(`from naiad import Client`), but PyPI only publishes `naiad 1.0.dev` (no
stable release as of 2026-11-16). US-003 should decide between installing
`naiad` from source, pinning the dev release, or calling the REST/JSON-RPC
endpoints directly with `httpx`/`requests`. The raw endpoints above are
verified working either way.

## 5. HesabYar project checks

```bash
cd ~/workspace/projects/hesabyar
export PATH="$HOME/.local/bin:$PATH"
export HESABYAR_DATABASE_URL="postgresql+psycopg://hesabyar:hesabyar_pass@127.0.0.1:5432/hesabyar_test"

uv run pytest          # full suite (160 passed, incl. 15 PostgreSQL tests)
uv run ruff check .    # lint
uv run pyright         # typecheck
```

## 6. Troubleshooting

- `psycopg.OperationalError` / connection refused in tests: PostgreSQL is
  not running — `pg_ctlcluster 16 main start`.
- `could not translate host name "hesabyar-postgres"`: the env var override
  was not set — see §2.
- `trytond-admin` asks for a password interactively: `TRYTONPASSFILE` was
  not exported.
- `405 Method Not Allowed` on JSON-RPC: use `/rpc/` and `/<db>/rpc/`, not
  `/<db>/`.
- `401` on REST with a session token: REST needs the application key
  (Bearer), not a session.
- `422 Missing context argument` on model RPC: append `{}` to params.
- `httpx.InvalidURL: Invalid port: ':1]'` when constructing any
  `httpx.Client()`: this VM exports proxy variables whose `no_proxy` list
  contains bracketed IPv6 entries that httpx 0.28 cannot parse. Local
  Tryton traffic must never go through a proxy — construct clients with
  `trust_env=False`. (US-004 found that the US-003 `TrytonRestClient`
  does not do this yet, so it cannot even be constructed in this VM
  without scrubbing the proxy variables first; that is a US-003
  follow-up, not a US-004 change.)

## 7. IRR precision + Jalali mapping (US-004)

Contract tests: `tests/infrastructure/test_tryton_irr_jalali.py`
(49 tests: 28 live against trytond, 21 pure-domain). The live tests skip
cleanly when trytond is not reachable, so the suite stays green without a
server; the fixture is idempotent, so re-running setup on an existing DB
is a no-op.

### IRR currency configuration

Created through the REST API (never by SQL — the spec forbids writing to
Tryton's database directly):

```bash
curl -X POST "http://localhost:8000/hesabyar_tryton_test/rest/model/currency.currency" \
  -H "Authorization: Bearer hesabyar-dev-rest-key" \
  -H 'Content-Type: application/json' \
  -d '{"name": "Iranian Rial", "symbol": "﷼", "code": "IRR",
       "numeric_code": "364", "digits": 0, "rounding": 1}'
```

| Field | Value | Meaning |
| --- | --- | --- |
| `code` / `numeric_code` | `IRR` / `364` | ISO 4217 alpha/numeric codes |
| `digits` | `0` | Zero decimal digits — Rials are integers |
| `rounding` | `1` (`Decimal("1")`) | Minimum representable amount: 1 Rial |
| `symbol` | `﷼` | Rial sign (U+FDFC) |

A base rate of `1` dated `2026-03-21` was also created
(`currency.currency.rate`), per the official docs' best practice that the
base currency's rate is 1 from the date it is first used. Verified by
REST read-back (2026-11-16):

```json
[{"name": "Iranian Rial", "rounding": {"__class__": "Decimal", "decimal": "1"},
  "digits": 0, "numeric_code": "364", "code": "IRR", "symbol": "﷼",
  "id": 1, "__name__": "currency.currency"}]
```

### How Tryton enforces digits=0 (verified in the installed 8.2.0 source)

- `currency.currency.digits` is the display/validation precision and
  `rounding` the minimum amount (`trytond_currency/currency.py`).
  `DigitsMixin.get_digits()` returns `(None, digits)`, i.e. `(None, 0)`
  for IRR: no magnitude cap, zero fractional digits.
- `ModelStorage` validates every Monetary value against those digits on
  create/write (`trytond/model/modelstorage.py`, `digits_test`): a
  `Decimal` with a negative exponent raises `DigitsValidationError`,
  which the REST layer surfaces as HTTP 400:
  `The number of digits in the value "'100.5'" ... exceeds the limit of "(None, 0)".`
  Fractional Rials are therefore rejected by Tryton itself — never
  silently rounded (and never silently converted to Toman).
- The `NUMERIC` columns are unbounded (`Numeric._sql_type = 'NUMERIC'`
  with no precision), so the only cap is the digits validation above.
- Decimal values travel over REST/JSON-RPC as exact strings
  (`{"__class__": "Decimal", "decimal": "…"}`,
  `trytond/protocols/jsonrpc.py`), never as floats — the wire format
  cannot lose precision.

### Test evidence (live, 2026-11-16)

Large-integer round-trips through `account.move` / `account.move.line`
(draft moves, balanced debit/credit, read back bit-exact as `Decimal`):

| Amount (IRR) | Result |
| --- | --- |
| 1,750,000 | exact |
| 9,876,543,210,987 | exact |
| 999,999,999,999,999 | exact |
| 9,999,999,999,999,999,999 (19 digits, stress) | exact |

Fractional rejection: `100.5`, `0.1`, `1750000.5` (exact Decimal
encoding) and JSON float `100.5` are all rejected with HTTP 400, both via
raw REST and via the US-003 `TrytonRestClient`, which translates the 400
to the stable `TrytonAdapterError` (`code=ENGINE_VALIDATION_FAILED`,
`status_code=400`) and creates no move. The command boundary is
structurally integer-only as well: `MoneyIRR` rejects every non-`int`
amount at construction, so a `CreateJournalDraft` cannot express a
fractional Rial at all; and the read-side mapping
(`money_from_tryton`) rejects fractional/float/NaN amounts from the
engine with `InvalidMoneyError`. The large-integer + Jalali guarantees
are also re-proven end-to-end through the port-level
`TrytonAccountingAdapter.create_journal_draft` /
`get_journal_draft` / `validate_journal_draft` (9,876,543,210,987 IRR,
1405/01/15, validation summary valid).

### FiscalDate mapping (verified against `account.move.date`)

Jalali → Gregorian via the real `FiscalDate` code, written to a Tryton
date field, read back, converted to Jalali, asserted equal:

| Jalali | Gregorian | Note |
| --- | --- | --- |
| 1405/01/01 | 2026-03-21 | Nowruz |
| 1405/06/31 | 2026-09-22 | last day of a 31-day month |
| 1405/07/01 | 2026-09-23 | first day of a 30-day month |
| 1405/12/29 | 2027-03-20 | last day of non-leap 1405 |
| 1403/12/30 | 2025-03-20 | leap day (1403 is a leap year) |
| 1404/01/01 | 2025-03-21 | — |

Digit normalisation: Persian digits (`۱۴۰۵/۰۱/۱۵`) and Arabic-Indic
digits (`١٤٠٥/٠١/١٥`) both parse to `1405/01/15` and round-trip.
Rejected (pure-domain tests, `InvalidDateError`): `1405/12/30` (Esfand
30 in non-leap 1405), `1405/13/01`, `1405/07/31` (Mehr has 30 days),
`1405/00/10`, `1405/01/00`, `2026/04/04` (Gregorian year is ambiguous),
`15/01/1405`, empty/malformed strings, `datetime` objects, and
slash-delimited ISO strings.

### Fixture (dedicated test company — do not reuse for other stories)

All US-004 data lives under the company **"HesabYar IRR Test"**
(id 3, currency IRR), created idempotently by the test fixture:

- Party "HesabYar IRR Test"; company timezone `Asia/Tehran`.
- Account types "IRR Test Assets" (balance) / "IRR Test Revenue"
  (income); accounts `1000` IRR Test Cash / `4000` IRR Test Revenue.
- Fiscal years 1403/1404/1405, each with one full-year open period
  (`1403 IRR Test`, …) and its **own** move sequence — Tryton enforces
  move-sequence uniqueness per fiscal year, and the sequence must use
  the account module's "Account Move" sequence type and belong to the
  company (fiscal-year domain rule).
- Moves use the shared "Miscellaneous" journal (journals are not
  company-scoped).

Prerequisite the fixture also ensures (needed on any fresh DB, because
the account module's "User in companies" rule gates all company-scoped
writes on it): the `admin` user must list the company in its
`companies` **and** have `company_filter='all'` — with filter `'one'`
and no current company, `User.get_companies()` returns `[]` and every
account/fiscal-year create fails with 400 "User in companies".

### REST gotchas found while writing these tests

- The `d` (domain) query param is base64url JSON **with** its `=`
  padding — stripping the padding raises `binascii.Error: Incorrect
  padding` (HTTP 500) server-side.
- `httpx.Client()` needs `trust_env=False` in this VM (see §6).
- One2Many values in create payloads need Tryton's command syntax,
  `"lines": [["create", [{...}, {...}]]]` — a plain list of dicts is
  rejected.
