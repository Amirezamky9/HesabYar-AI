#!/usr/bin/env bash
# setup-tryton.sh — Build a complete Tryton 8.2 test server from scratch.
#
# Used by GitHub Actions (.github/workflows/ci.yml, job python-tests-tryton)
# so CI can run the FULL pytest suite including the live-Tryton contract
# tests, and usable locally to (re)build a disposable Tryton test DB.
#
# What it does, in order:
#   1. Creates a Tryton venv (uv) and installs trytond 8.2.3 + the pinned
#      module packages from scripts/ci/tryton-requirements.txt, plus the
#      two local modules built from tryton-modules/ (hesabyar_setup,
#      account_check_ir). Skipped when the venv already has trytond 8.2.3
#      (the local modules are still reinstalled from the checkout).
#   2. Creates the Tryton role + database on the local PostgreSQL server
#      (via psql as the superuser).
#   3. Initialises the DB (trytond-admin --all), refreshes the module
#      list (-m), activates the full 41-module set
#      (scripts/activate-tryton-modules.sh).
#   4. Starts trytond, creates the REST application key
#      (hesabyar-dev-rest-key), provisions the Adapter Test company +
#      chart (FY 2026), runs update_chart (HesabYar extension accounts),
#      provisions FY 2027, packaging (US-515) and cash-box journals
#      (US-516), restarts trytond, and verifies the result.
#
# SAFETY: this script initialises a database. It REFUSES to touch a
# database that already contains Tryton tables, unless TRYTON_DB_RESET=1
# (and then only when the database name contains "test" or "ci").
# It never touches the HesabYar database (Alembic owns that) and never
# touches a remote database -- PostgreSQL is always local.
#
# Configuration (all optional; CI defaults shown):
#   TRYTON_ENV              Tryton venv dir (default: $REPO/.tryton-env)
#   TRYTON_DATA             trytond data/config/log dir (default: $REPO/.tryton-data)
#   TRYTON_DB               Tryton database (default: hesabyar_tryton_test)
#   TRYTON_DB_ROLE          Tryton DB role (default: tryton)
#   TRYTON_DB_PASSWORD      Tryton DB role password (default: tryton_pass;
#                           empty = trust auth, no password in the URI)
#   TRYTON_PORT             trytond listen port (default: 8000)
#   TRYTON_ADMIN_EMAIL      admin email for --all (default: admin@hesabyar.local)
#   TRYTON_ADMIN_PASSWORD   admin password (default: tryton-admin-dev)
#   TRYTON_REST_KEY         REST application key (default: hesabyar-dev-rest-key)
#   PG_HOST / PG_PORT       PostgreSQL host/port (default: 127.0.0.1 / 5432)
#   PG_SUPERUSER            superuser for role/DB creation (default: postgres)
#   PGPASSWORD              superuser password (default: empty = trust auth)
#   PSQL_BIN                psql binary (default: psql)
#   FIND_LINKS              extra --find-links dir for uv pip install
#                           (dev VMs: the wheels-us502 cache)
#   TRYTON_DB_RESET=1       drop + recreate the DB first (name must
#                           contain "test" or "ci")
#   TRYTON_NO_START=1       provision but do not leave trytond running
#
# All credentials above are dev/CI-only test values. Never reuse them
# in staging or production.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CI_DIR="$REPO/scripts/ci"

TRYTON_ENV="${TRYTON_ENV:-$REPO/.tryton-env}"
TRYTON_DATA="${TRYTON_DATA:-$REPO/.tryton-data}"
TRYTON_DB="${TRYTON_DB:-hesabyar_tryton_test}"
TRYTON_DB_ROLE="${TRYTON_DB_ROLE:-tryton}"
TRYTON_DB_PASSWORD="${TRYTON_DB_PASSWORD:-tryton_pass}"
TRYTON_PORT="${TRYTON_PORT:-8000}"
TRYTON_ADMIN_EMAIL="${TRYTON_ADMIN_EMAIL:-admin@hesabyar.local}"
TRYTON_ADMIN_PASSWORD="${TRYTON_ADMIN_PASSWORD:-tryton-admin-dev}"
TRYTON_REST_KEY="${TRYTON_REST_KEY:-hesabyar-dev-rest-key}"
PG_HOST="${PG_HOST:-127.0.0.1}"
PG_PORT="${PG_PORT:-5432}"
PG_SUPERUSER="${PG_SUPERUSER:-postgres}"
PSQL_BIN="${PSQL_BIN:-psql}"
TRYTON_DB_RESET="${TRYTON_DB_RESET:-0}"
TRYTON_NO_START="${TRYTON_NO_START:-0}"

CONF="$TRYTON_DATA/trytond.conf"
PASSFILE="$TRYTON_DATA/tryton-passfile"
PIDFILE="$TRYTON_DATA/trytond.pid"
LOGFILE="$TRYTON_DATA/trytond.log"
BASE_URL="http://localhost:$TRYTON_PORT"

step() { echo; echo "== $1 =="; }
elapsed() { echo "   (${SECONDS}s elapsed)"; }

# --- 0. Preconditions -------------------------------------------------------
step "0. Preconditions"
command -v uv >/dev/null 2>&1 || { echo "uv is required (install it or add ~/.local/bin to PATH)" >&2; exit 1; }
command -v "$PSQL_BIN" >/dev/null 2>&1 || { echo "$PSQL_BIN not found (set PSQL_BIN)" >&2; exit 1; }
"$PSQL_BIN" -h "$PG_HOST" -p "$PG_PORT" -U "$PG_SUPERUSER" -d postgres -tAc "SELECT 1" >/dev/null \
    || { echo "cannot connect to PostgreSQL at $PG_HOST:$PG_PORT as $PG_SUPERUSER" >&2; exit 1; }
echo "PostgreSQL reachable at $PG_HOST:$PG_PORT; uv: $(uv --version)"
mkdir -p "$TRYTON_DATA"

# --- 1. Tryton venv + packages ----------------------------------------------
step "1. Tryton venv + packages"
if [ -x "$TRYTON_ENV/bin/trytond" ] && "$TRYTON_ENV/bin/trytond" --version 2>/dev/null | grep -q "8.2.3"; then
    echo "trytond 8.2.3 already present at $TRYTON_ENV -- skipping PyPI install"
else
    echo "Creating venv at $TRYTON_ENV"
    uv venv --python 3.12 "$TRYTON_ENV"
    echo "Installing pinned Tryton packages from PyPI"
    # shellcheck disable=SC2086
    uv pip install --python "$TRYTON_ENV/bin/python" \
        ${FIND_LINKS:+--find-links "$FIND_LINKS"} \
        -r "$CI_DIR/tryton-requirements.txt"
fi
echo "Installing local modules from tryton-modules/ (repo checkout is the source of truth)"
uv pip install --python "$TRYTON_ENV/bin/python" --force-reinstall \
    "$REPO/tryton-modules/hesabyar_setup" "$REPO/tryton-modules/account_check_ir"
"$TRYTON_ENV/bin/trytond" --version
elapsed

# --- 2. Role + database ------------------------------------------------------
step "2. Role + database $TRYTON_DB"
export PGPASSWORD="${PGPASSWORD:-}"
"$PSQL_BIN" -h "$PG_HOST" -p "$PG_PORT" -U "$PG_SUPERUSER" -d postgres -v ON_ERROR_STOP=1 <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '$TRYTON_DB_ROLE') THEN
    CREATE ROLE "$TRYTON_DB_ROLE" LOGIN PASSWORD '$TRYTON_DB_PASSWORD';
  END IF;
END \$\$;
SQL
if [ "$TRYTON_DB_RESET" = "1" ]; then
    case "$TRYTON_DB" in
        *test*|*ci*) ;;
        *) echo "REFUSING to drop database '$TRYTON_DB' (name must contain 'test' or 'ci')" >&2; exit 1 ;;
    esac
    echo "TRYTON_DB_RESET=1: dropping + recreating $TRYTON_DB"
    "$PSQL_BIN" -h "$PG_HOST" -p "$PG_PORT" -U "$PG_SUPERUSER" -d postgres -c "DROP DATABASE IF EXISTS \"$TRYTON_DB\";"
fi
"$PSQL_BIN" -h "$PG_HOST" -p "$PG_PORT" -U "$PG_SUPERUSER" -d postgres -v ON_ERROR_STOP=1 <<SQL
SELECT 'CREATE DATABASE "$TRYTON_DB" OWNER "$TRYTON_DB_ROLE" ENCODING ''UTF8'''
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '$TRYTON_DB') \gexec
SQL
# Refuse to initialise a database that already has Tryton tables.
if "$PSQL_BIN" -h "$PG_HOST" -p "$PG_PORT" -U "$PG_SUPERUSER" -d "$TRYTON_DB" -tAc \
        "SELECT to_regclass('public.ir_module')" | grep -q "ir_module"; then
    echo "REFUSING: database $TRYTON_DB already contains Tryton tables." >&2
    echo "Set TRYTON_DB_RESET=1 to drop + recreate it, or point TRYTON_DB at a fresh database." >&2
    exit 1
fi
echo "Database $TRYTON_DB ready (empty, owned by $TRYTON_DB_ROLE)"
elapsed

# --- 3. Config + passfile ----------------------------------------------------
step "3. trytond config"
if [ -n "$TRYTON_DB_PASSWORD" ]; then
    DB_URI="postgresql://$TRYTON_DB_ROLE:$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$TRYTON_DB_PASSWORD")@$PG_HOST:$PG_PORT/"
else
    DB_URI="postgresql://$TRYTON_DB_ROLE@$PG_HOST:$PG_PORT/"
fi
sed -e "s|@TRYTON_PORT@|$TRYTON_PORT|g" \
    -e "s|@TRYTON_DB_URI@|$DB_URI|g" \
    -e "s|@TRYTON_DATA@|$TRYTON_DATA|g" \
    "$CI_DIR/trytond.conf.template" > "$CONF"
printf '%s' "$TRYTON_ADMIN_PASSWORD" > "$PASSFILE"
chmod 600 "$PASSFILE"
echo "Wrote $CONF (port $TRYTON_PORT) + passfile (0600)"

# --- 4. Initialise + activate modules ----------------------------------------
step "4. Initialise DB + activate 41 modules"
export TRYTONPASSFILE="$PASSFILE"
"$TRYTON_ENV/bin/trytond-admin" -c "$CONF" -d "$TRYTON_DB" --all --email "$TRYTON_ADMIN_EMAIL"
"$TRYTON_ENV/bin/trytond-admin" -c "$CONF" -d "$TRYTON_DB" -m
# The pytest guard in activate-tryton-modules.sh protects the SHARED dev
# DBs; this script always targets a fresh CI database, so it is skipped
# here (HESABYAR_SKIP_PYTEST_GUARD=1).
TRYTON_ENV="$TRYTON_ENV" HESABYAR_SKIP_PYTEST_GUARD=1 \
    bash "$REPO/scripts/activate-tryton-modules.sh" -c "$CONF" -d "$TRYTON_DB"
elapsed

# --- 5. Start trytond ---------------------------------------------------------
step "5. Start trytond on :$TRYTON_PORT"
start_trytond() {
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        echo "trytond already running (pid $(cat "$PIDFILE"))" >&2; exit 1
    fi
    nohup "$TRYTON_ENV/bin/trytond" -c "$CONF" > "$LOGFILE" 2>&1 &
    echo $! > "$PIDFILE"
    for _ in $(seq 1 60); do
        code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$BASE_URL/" || true)"
        [ "$code" = "405" ] && { echo "trytond up (GET / -> 405)"; return 0; }
        sleep 2
    done
    echo "trytond did not come up within 120s; last log lines:" >&2
    tail -20 "$LOGFILE" >&2
    exit 1
}
stop_trytond() {
    [ -f "$PIDFILE" ] || return 0
    kill "$(cat "$PIDFILE")" 2>/dev/null || true
    sleep 2
    rm -f "$PIDFILE"
}
start_trytond
elapsed

# --- 6. REST key ---------------------------------------------------------------
step "6. REST application key"
python3 "$CI_DIR/create_rest_key.py" \
    --base-url "$BASE_URL" --database "$TRYTON_DB" \
    --admin-password "$TRYTON_ADMIN_PASSWORD" --key "$TRYTON_REST_KEY"
elapsed

# --- 7. Provision test data (trytond-console) ----------------------------------
step "7. Provision test company + chart + FY 2027 + packaging + cash-box"
CONSOLE="$TRYTON_ENV/bin/trytond-console"
LOCK_ALL="--lock-table account_fiscalyear account_period account_journal account_configuration"
# shellcheck disable=SC2086
"$CONSOLE" -c "$CONF" -d "$TRYTON_DB" $LOCK_ALL -f "$CI_DIR/provision_adapter_test_company.py"
"$CONSOLE" -c "$CONF" -d "$TRYTON_DB" -f "$CI_DIR/update_chart.py"
# shellcheck disable=SC2086
"$CONSOLE" -c "$CONF" -d "$TRYTON_DB" $LOCK_ALL -f "$CI_DIR/provision_fy2027.py"
"$CONSOLE" -c "$CONF" -d "$TRYTON_DB" --lock-table account_fiscalyear -f "$CI_DIR/provision_packaging.py"
"$CONSOLE" -c "$CONF" -d "$TRYTON_DB" --lock-table account_journal account_statement_journal -f "$CI_DIR/provision_cashbox_journals.py"
elapsed

# --- 8. Restart trytond (clear server cache) + verify ---------------------------
step "8. Restart trytond + verify"
stop_trytond
start_trytond

fail=0
check() { # check <label> <actual> <expected>
    if [ "$2" = "$3" ]; then echo "  OK   $1: $2";
    else echo "  FAIL $1: got $2, expected $3"; fail=1; fi
}
psql_tryton() { "$PSQL_BIN" -h "$PG_HOST" -p "$PG_PORT" -U "$PG_SUPERUSER" -d "$TRYTON_DB" -tAc "$1"; }
check "activated modules" "$(psql_tryton "SELECT count(*) FROM ir_module WHERE state='activated'")" "41"
# Chart total is a sanity bound, not an exact count: the dev DBs have 159
# accounts for this company, but the meaningful assertions are the
# individual extension codes below (a template drift of a few accounts
# must not fail the build).
chart_total="$(psql_tryton "SELECT count(*) FROM account_account a JOIN company_company c ON a.company=c.id JOIN party_party p ON c.party=p.id WHERE p.name='HesabYar Adapter Test'")"
if [ "${chart_total:-0}" -ge 150 ]; then echo "  OK   Adapter Test chart accounts: $chart_total (>= 150)";
else echo "  FAIL Adapter Test chart accounts: got ${chart_total:-?}, expected >= 150"; fail=1; fi
check "FY 2027 open periods" "$(psql_tryton "SELECT count(*) FROM account_period p JOIN account_fiscalyear f ON p.fiscalyear=f.id WHERE f.name='FY 2027' AND p.state='open'")" "12"
check "US-516 statement journals" "$(psql_tryton "SELECT count(*) FROM account_statement_journal WHERE name LIKE 'HesabYar US-516%'")" "2"
check "REST key present" "$(psql_tryton "SELECT count(*) FROM res_user_application WHERE key='$TRYTON_REST_KEY'")" "1"
# With company_filter='one', User.get_companies() returns [] and every
# company-scoped read comes back empty -- the contract tests then see
# an empty chart. The provisioning sets 'all'; assert it here.
check "admin company_filter" "$(psql_tryton "SELECT company_filter FROM res_user WHERE login='admin'")" "all"
for code in 1.1.5 1.1.6 1.2.4 1.2.5 1.2.6 1.2.7 1.2.8 1.2.9 2.1.5 2.1.6 5.2.4; do
    check "chart account $code" "$(psql_tryton "SELECT count(*) FROM account_account a JOIN company_company c ON a.company=c.id JOIN party_party p ON c.party=p.id WHERE p.name='HesabYar Adapter Test' AND a.code='$code'")" "1"
done
# REST probe: the adapter's own auth path (Bearer key) must work. Note
# company.company has NO name field (names live on party.party) --
# requesting f=name on it is a server 500, so probe the party instead.
probe="$(curl -s --max-time 30 "$BASE_URL/$TRYTON_DB/rest/model/party.party?f=name" -H "Authorization: Bearer $TRYTON_REST_KEY" || true)"
case "$probe" in
    *"HesabYar Adapter Test"*) echo "  OK   REST probe: Adapter Test party listed" ;;
    *) echo "  FAIL REST probe: $probe" | head -c 300; echo; fail=1 ;;
esac
[ "$fail" = "0" ] || { echo "VERIFICATION FAILED -- see above" >&2; exit 1; }
elapsed

if [ "$TRYTON_NO_START" = "1" ]; then
    stop_trytond
    echo "TRYTON_NO_START=1: trytond stopped after provisioning."
else
    echo
    echo "TRYTON READY: $BASE_URL (db $TRYTON_DB, pid $(cat "$PIDFILE"), log $LOGFILE)"
    echo "Run the suite with (all three env families must agree -- see docs/ci-tryton.md):"
    echo "  TRYTON_BASE_URL=$BASE_URL TRYTON_DATABASE=$TRYTON_DB TRYTON_API_KEY=$TRYTON_REST_KEY \\"
    echo "  HESABYAR_TRYTON__BASE_URL=$BASE_URL HESABYAR_TRYTON__DATABASE=$TRYTON_DB HESABYAR_TRYTON__API_KEY=$TRYTON_REST_KEY \\"
    echo "  TRYTON_DB=$TRYTON_DB TRYTON_REST_KEY=$TRYTON_REST_KEY \\"
    echo "  uv run pytest -q"
fi
