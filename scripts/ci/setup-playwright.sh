#!/usr/bin/env bash
# setup-playwright.sh — Provision the Playwright e2e stack on top of a
# setup-tryton.sh Tryton server: US-304 seed, dev API key, .env.local,
# backend (uvicorn) + Next production server.
#
# Used by GitHub Actions (.github/workflows/ci.yml, job `playwright`)
# and runnable locally against a CI-style stack (see docs/ci-playwright.md).
#
# Prerequisites (run first):
#   - scripts/ci/setup-tryton.sh (trytond up on $TRYTON_PORT, with the
#     Adapter Test company + chart + FY 2026/2027 provisioned)
#   - HESABYAR_DATABASE_URL pointing at the HesabYar database (the
#     script runs `alembic upgrade head` itself; idempotent)
#   - `uv sync` already done at the repo root (the backend venv)
#   - pnpm deps: installed by this script when node_modules is missing
#
# What it does, in order:
#   1. Migrates the HesabYar DB (alembic upgrade head), then seeds the
#      three registries the specs read (standards corpus, tax-policy
#      1405 candidates, built-in payment channels for the dev
#      installation) — the migrations create the tables but do NOT
#      populate them, and the compliance/settlement specs assert the
#      live seeded values (35 standards, 5 gated tax parameters,
#      the snapppay channel).
#   2. Runs the US-304 seed (scripts/ci/seed_us304.py) via
#      trytond-console and captures the printed US304_* ids, then
#      provisions the E2E checkbook (scripts/ci/provision_checkbook.py)
#      the cheques spec needs. trytond-console exits 0 even on a
#      traceback, so the captured output — not the exit code — is the
#      gate: all four seed ids + the checkbook marker must be present,
#      and no Traceback may appear.
#   3. Restarts trytond (the server caches model data; the seed wrote
#      rows directly through the console).
#   4. Mints a dev operator API key (apps/admin/scripts/mint-dev-key.py)
#      and writes apps/admin/.env.local (0600) with the key +
#      TRYTON_BASE_URL / TRYTON_DATABASE / TRYTON_API_KEY + the seed
#      ids. The key secret is NEVER printed to the console: the mint
#      output goes to a 0600 log file and only the key prefix is shown.
#   5. Starts the FastAPI backend (uvicorn) on $BACKEND_PORT and waits
#      for /api/v1/health.
#   6. Builds the admin app (pnpm run build) and starts `pnpm start`
#      (the PRODUCTION server — `pnpm dev` does not hydrate in this
#      environment) on $NEXT_PORT, with HESABYAR_API_BASE_URL pointing
#      at the backend; waits for /login.
#
# Servers are left running for the Playwright run. Pids + logs live in
# $PW_DATA; the teardown is documented in docs/ci-playwright.md.
#
# Configuration (all optional; CI defaults shown):
#   TRYTON_ENV / TRYTON_DATA    same as setup-tryton.sh
#                               (default: $REPO/.tryton-env / $REPO/.tryton-data)
#   TRYTON_DB / TRYTON_PORT / TRYTON_REST_KEY
#                               same as setup-tryton.sh
#                               (default: hesabyar_tryton_test / 8000 /
#                               hesabyar-dev-rest-key)
#   HESABYAR_DATABASE_URL       HesabYar DB (REQUIRED, postgresql+psycopg://)
#   HESABYAR_TRYTON__BASE_URL   backend's Tryton URL (default: http://localhost:$TRYTON_PORT)
#   HESABYAR_TRYTON__DATABASE   (default: $TRYTON_DB)
#   HESABYAR_TRYTON__API_KEY    (default: $TRYTON_REST_KEY)
#   BACKEND_PORT                uvicorn port (default: 8001)
#   NEXT_PORT                   pnpm start port (default: 3000)
#   PW_DATA                     server logs/pids dir (default: $REPO/.playwright-data)
#
# All credentials above are dev/CI-only test values. Never reuse them
# in staging or production.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CI_DIR="$REPO/scripts/ci"
ADMIN_DIR="$REPO/apps/admin"

TRYTON_ENV="${TRYTON_ENV:-$REPO/.tryton-env}"
TRYTON_DATA="${TRYTON_DATA:-$REPO/.tryton-data}"
TRYTON_DB="${TRYTON_DB:-hesabyar_tryton_test}"
TRYTON_PORT="${TRYTON_PORT:-8000}"
TRYTON_REST_KEY="${TRYTON_REST_KEY:-hesabyar-dev-rest-key}"
BACKEND_PORT="${BACKEND_PORT:-8001}"
NEXT_PORT="${NEXT_PORT:-3000}"
PW_DATA="${PW_DATA:-$REPO/.playwright-data}"

TRYTON_BASE_URL="http://localhost:$TRYTON_PORT"
export HESABYAR_TRYTON__BASE_URL="${HESABYAR_TRYTON__BASE_URL:-$TRYTON_BASE_URL}"
export HESABYAR_TRYTON__DATABASE="${HESABYAR_TRYTON__DATABASE:-$TRYTON_DB}"
export HESABYAR_TRYTON__API_KEY="${HESABYAR_TRYTON__API_KEY:-$TRYTON_REST_KEY}"
export PYTHONIOENCODING=utf-8 PYTHONUTF8=1

CONF="$TRYTON_DATA/trytond.conf"
TRYTON_PIDFILE="$TRYTON_DATA/trytond.pid"
TRYTON_LOG="$TRYTON_DATA/trytond.log"

step() { echo; echo "== $1 =="; }
elapsed() { echo "   (${SECONDS}s elapsed)"; }

# --- 0. Preconditions -------------------------------------------------------
step "0. Preconditions"
command -v uv >/dev/null 2>&1 || { echo "uv is required (install it or add ~/.local/bin to PATH)" >&2; exit 1; }
command -v pnpm >/dev/null 2>&1 || { echo "pnpm is required" >&2; exit 1; }
[ -n "${HESABYAR_DATABASE_URL:-}" ] || { echo "HESABYAR_DATABASE_URL is required" >&2; exit 1; }
[ -x "$TRYTON_ENV/bin/trytond-console" ] || { echo "trytond-console not found at $TRYTON_ENV/bin/trytond-console -- run setup-tryton.sh first" >&2; exit 1; }
[ -f "$CONF" ] || { echo "trytond config not found at $CONF -- run setup-tryton.sh first" >&2; exit 1; }
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$TRYTON_BASE_URL/" || true)"
[ "$code" = "405" ] || { echo "trytond is not up at $TRYTON_BASE_URL (GET / -> ${code:-none}) -- run setup-tryton.sh first" >&2; exit 1; }
mkdir -p "$PW_DATA"
echo "trytond up at $TRYTON_BASE_URL (db $TRYTON_DB); HesabYar DB set"

# Stop servers from a previous run of THIS script. Only the exact pids
# recorded in our own pidfiles are killed — never anything else — and
# then the port is freed too: `uv run` / `pnpm` wrap the real server
# process, so killing the wrapper pid alone can leave the server
# listening (the next start would then fail with "address in use").
stop_ours() { # stop_ours <pidfile> <label> <port>
    [ -f "$1" ] || return 0
    pid="$(cat "$1" 2>/dev/null || true)"
    if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then
        echo "Stopping previous $2 (pid $pid)"
        kill "$pid" 2>/dev/null || true
        sleep 2
        kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$1"
    if command -v fuser >/dev/null 2>&1; then
        fuser -k "$3/tcp" >/dev/null 2>&1 || true
    fi
}
stop_ours "$PW_DATA/backend.pid" "backend" "$BACKEND_PORT"
stop_ours "$PW_DATA/next.pid" "Next server" "$NEXT_PORT"

# --- 1. Migrate the HesabYar DB ----------------------------------------------
step "1. Migrate the HesabYar DB (alembic upgrade head)"
cd "$REPO"
uv run alembic upgrade head
elapsed

# --- 1b. Seed the registries ---------------------------------------------------
step "1b. Seed standards + tax policy + payment channels"
# The migrations create the registry tables but do not populate them;
# the compliance spec asserts the live seeded values (35 standards,
# 36 rules on Standard 1, 5 gated tax parameters) and the settlement
# spec wires the snapppay built-in channel. All three seed CLIs are
# idempotent. The channels are per-installation: seed them for the
# dev installation id the mint script issues the dev key under
# (apps/admin/scripts/mint-dev-key.py INSTALLATION_ID).
uv run python -m hesabyar.infrastructure.persistence.postgres.seed_standards
uv run python -m hesabyar.infrastructure.persistence.postgres.seed_tax_policy
uv run python -m hesabyar.infrastructure.persistence.postgres.seed_payment_channels \
    --installation-id 00000000-0000-0000-0000-000000000004
elapsed

# --- 2. US-304 seed -----------------------------------------------------------
step "2. US-304 seed (trytond-console)"
SEED_LOG="$PW_DATA/seed_us304.log"
"$TRYTON_ENV/bin/trytond-console" -c "$CONF" -d "$TRYTON_DB" \
    --lock-table account_fiscalyear account_period account_journal account_configuration \
    -f "$CI_DIR/seed_us304.py" > "$SEED_LOG" 2>&1 || true
# trytond-console exits 0 even on a traceback: the output is the gate.
if grep -q "Traceback" "$SEED_LOG"; then
    echo "SEED FAILED (traceback in $SEED_LOG):" >&2
    tail -20 "$SEED_LOG" >&2
    exit 1
fi
for var in US304_PARTY_ID US304_REVENUE_ACCOUNT_ID US304_PAYMENT_ID US304_STATEMENT_ID; do
    if ! grep -q "^$var=[0-9][0-9]*$" "$SEED_LOG"; then
        echo "SEED FAILED: no $var line in $SEED_LOG (tail):" >&2
        tail -20 "$SEED_LOG" >&2
        exit 1
    fi
done
# shellcheck disable=SC1090
eval "$(grep -E '^US304_(PARTY_ID|REVENUE_ACCOUNT_ID|PAYMENT_ID|STATEMENT_ID)=' "$SEED_LOG")"
echo "Seed ids: party=$US304_PARTY_ID revenue_account=$US304_REVENUE_ACCOUNT_ID payment=$US304_PAYMENT_ID statement=$US304_STATEMENT_ID"
elapsed

step "2b. E2E checkbook (trytond-console)"
CHECKBOOK_LOG="$PW_DATA/provision_checkbook.log"
"$TRYTON_ENV/bin/trytond-console" -c "$CONF" -d "$TRYTON_DB" \
    -f "$CI_DIR/provision_checkbook.py" > "$CHECKBOOK_LOG" 2>&1 || true
if grep -q "Traceback" "$CHECKBOOK_LOG" || ! grep -q "^CHECKBOOK-PROVISION-COMMITTED$" "$CHECKBOOK_LOG"; then
    echo "CHECKBOOK PROVISIONING FAILED (see $CHECKBOOK_LOG):" >&2
    tail -20 "$CHECKBOOK_LOG" >&2
    exit 1
fi
grep -E '^CHECKBOOK_ID=' "$CHECKBOOK_LOG" || true
elapsed

# --- 3. Restart trytond (server cache) -----------------------------------------
step "3. Restart trytond (clear server cache after the seed)"
if [ -f "$TRYTON_PIDFILE" ] && kill -0 "$(cat "$TRYTON_PIDFILE")" 2>/dev/null; then
    kill "$(cat "$TRYTON_PIDFILE")" 2>/dev/null || true
    sleep 3
fi
rm -f "$TRYTON_PIDFILE"
nohup "$TRYTON_ENV/bin/trytond" -c "$CONF" > "$TRYTON_LOG" 2>&1 &
echo $! > "$TRYTON_PIDFILE"
for _ in $(seq 1 60); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$TRYTON_BASE_URL/" || true)"
    [ "$code" = "405" ] && { echo "trytond up (GET / -> 405)"; break; }
    sleep 2
done
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$TRYTON_BASE_URL/" || true)"
[ "$code" = "405" ] || { echo "trytond did not come back up; last log lines:" >&2; tail -20 "$TRYTON_LOG" >&2; exit 1; }
elapsed

# --- 4. Mint the dev key + write .env.local ------------------------------------
step "4. Mint dev API key + write apps/admin/.env.local"
MINT_LOG="$PW_DATA/mint-dev-key.log"
# The mint output contains the secret: capture to a 0600 file, never stdout.
HESABYAR_ALLOW_DEV_KEY=1 uv run python apps/admin/scripts/mint-dev-key.py > "$MINT_LOG" 2>&1
chmod 600 "$MINT_LOG"
# NOTE: the mint script indents the key line by 4 spaces
# ("    HESABYAR_DEV_API_KEY=..."), so the grep must NOT anchor to
# the start of the line. The bare secret is printed on its own line
# too, but only the HESABYAR_DEV_API_KEY= line matches here.
DEV_KEY="$(grep -E 'HESABYAR_DEV_API_KEY=hya_live_' "$MINT_LOG" | cut -d= -f2- | head -1 || true)"
case "$DEV_KEY" in
    hya_live_*) ;;
    *) echo "MINT FAILED: no key line in $MINT_LOG (tail):" >&2; tail -10 "$MINT_LOG" >&2; exit 1 ;;
esac
grep -E '^Key id:' "$MINT_LOG" || true
cat > "$ADMIN_DIR/.env.local" <<EOF
US304_PARTY_ID=$US304_PARTY_ID
US304_REVENUE_ACCOUNT_ID=$US304_REVENUE_ACCOUNT_ID
US304_PAYMENT_ID=$US304_PAYMENT_ID
US304_STATEMENT_ID=$US304_STATEMENT_ID
TRYTON_BASE_URL=$TRYTON_BASE_URL
TRYTON_DATABASE=$TRYTON_DB
TRYTON_API_KEY=$TRYTON_REST_KEY
HESABYAR_DEV_API_KEY=$DEV_KEY
EOF
chmod 600 "$ADMIN_DIR/.env.local"
echo "Wrote $ADMIN_DIR/.env.local (0600, gitignored) with the key + seed ids + Tryton env"
elapsed

# --- 5. Backend (uvicorn) -------------------------------------------------------
step "5. Start the FastAPI backend on :$BACKEND_PORT"
cd "$REPO"
nohup uv run uvicorn hesabyar.interfaces.rest.app:app \
    --host 127.0.0.1 --port "$BACKEND_PORT" > "$PW_DATA/backend.log" 2>&1 &
echo $! > "$PW_DATA/backend.pid"
for _ in $(seq 1 60); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$BACKEND_PORT/api/v1/health" || true)"
    [ "$code" = "200" ] && { echo "backend up (GET /api/v1/health -> 200)"; break; }
    sleep 2
done
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$BACKEND_PORT/api/v1/health" || true)"
[ "$code" = "200" ] || { echo "backend did not come up; last log lines:" >&2; tail -20 "$PW_DATA/backend.log" >&2; exit 1; }
# The minted key must actually authenticate (a stale/wrong key is a 401
# that only shows up at login time — catch it here instead).
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 -H "Authorization: Bearer $DEV_KEY" "http://127.0.0.1:$BACKEND_PORT/api/v1/companies?limit=1" || true)"
[ "$code" = "200" ] || { echo "key probe failed (GET /api/v1/companies -> $code); see $PW_DATA/backend.log" >&2; exit 1; }
echo "Key probe OK (GET /api/v1/companies -> 200)"
elapsed

# --- 6. Next production server --------------------------------------------------
step "6. Build + start the Next production server on :$NEXT_PORT"
cd "$ADMIN_DIR"
if [ ! -x "node_modules/.bin/next" ]; then
    echo "node_modules missing -- running pnpm install"
    pnpm install --frozen-lockfile
fi
pnpm run build > "$PW_DATA/next-build.log" 2>&1 || {
    echo "NEXT BUILD FAILED (see $PW_DATA/next-build.log):" >&2
    tail -20 "$PW_DATA/next-build.log" >&2
    exit 1
}
echo "Next build OK"
HESABYAR_API_BASE_URL="http://127.0.0.1:$BACKEND_PORT" \
    nohup pnpm start --port "$NEXT_PORT" --hostname 127.0.0.1 > "$PW_DATA/next.log" 2>&1 &
echo $! > "$PW_DATA/next.pid"
for _ in $(seq 1 60); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$NEXT_PORT/login" || true)"
    [ "$code" = "200" ] && { echo "Next up (GET /login -> 200)"; break; }
    sleep 2
done
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$NEXT_PORT/login" || true)"
[ "$code" = "200" ] || { echo "Next did not come up; last log lines:" >&2; tail -20 "$PW_DATA/next.log" >&2; exit 1; }
elapsed

# --- Summary ---------------------------------------------------------------------
echo
echo "PLAYWRIGHT STACK READY in ${SECONDS}s:"
echo "  trytond : $TRYTON_BASE_URL (db $TRYTON_DB, pid $(cat "$TRYTON_PIDFILE"), log $TRYTON_LOG)"
echo "  backend : http://127.0.0.1:$BACKEND_PORT (pid $(cat "$PW_DATA/backend.pid"), log $PW_DATA/backend.log)"
echo "  Next    : http://127.0.0.1:$NEXT_PORT (pid $(cat "$PW_DATA/next.pid"), log $PW_DATA/next.log)"
echo "  env file: $ADMIN_DIR/.env.local (0600)"
echo
echo "Run the specs with:"
echo "  cd $ADMIN_DIR"
echo "  PLAYWRIGHT_BASE_URL=http://127.0.0.1:$NEXT_PORT pnpm exec playwright test --config playwright.ci.config.ts"
