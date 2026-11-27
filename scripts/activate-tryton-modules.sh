#!/usr/bin/env bash
# Activate the HesabYar Tryton module set in a database (US-502, F2).
#
#   scripts/activate-tryton-modules.sh -c <trytond.conf> -d <database>
#
# The module list is the full HesabYar set: Phase 0 foundation + the
# Phase 5 F2 sale/stock/clearing/dunning modules + hesabyar_setup.
# trytond-admin -u is idempotent, so re-running it on a database that
# already has the modules is safe (it updates them instead).
#
# REMOTE DATABASES (Supabase): do NOT run trytond-admin directly
# against a remote DB -- at ~127 ms/query a full module activation
# takes hours. Instead:
#   1. pg_dump the remote Tryton DB to a local file,
#   2. restore it into a LOCAL PostgreSQL cluster,
#   3. run this script against the local DB,
#   4. pg_dump the local DB, drop + recreate the remote DB, restore,
#   5. restart trytond and verify via REST (ir.module search).
# See docs/tryton-modules.md for the activation record.
#
# SERIALIZATION: never run this while a pytest process is running
# anywhere (the live adapter tests use the same Tryton DB). Check
# `pgrep -af pytest` first and hold the shared lock:
#   mkdir ~/workspace/coord-logs/pytest.lock  # wait/retry if exists
#   ... run this script ...
#   rmdir ~/workspace/coord-logs/pytest.lock
#
# CI / FRESH-DB USE: scripts/ci/setup-tryton.sh always targets a fresh
# database no pytest process touches, so it sets
# HESABYAR_SKIP_PYTEST_GUARD=1 to skip the check below. Never set it
# for a shared dev database.
set -euo pipefail

TRYTON_ENV="${TRYTON_ENV:-$HOME/workspace/projects/hesabyar-reference/tryton-env}"
TRYTOND_ADMIN="$TRYTON_ENV/bin/trytond-admin"

# Phase 0 foundation modules.
PHASE0_MODULES=(
    account account_invoice account_payment account_statement
    account_asset analytic_account company currency party product
)

# Phase 5-4 (5ث) cheque module (US-530). Depends on the F2 clearing /
# dunning / statement modules above; install it after them.
CHEQUE_MODULES=(
    account_check_ir
)

# Phase 5 F2 modules (US-502). Dependencies (purchase, sale_history,
# account_invoice_stock, account_stock_continental, analytic_invoice)
# are pulled in automatically by --activate-dependencies.
F2_MODULES=(
    sale sale_shipment_cost sale_payment sale_discount sale_amendment
    sale_invoice_grouping
    account_payment_clearing account_dunning account_dunning_fee
    stock stock_supply purchase_request stock_package stock_shipment_cost
    carrier account_stock_landed_cost account_stock_anglo_saxon
    analytic_sale account_statement_rule
    hesabyar_setup
)

usage() {
    echo "Usage: $0 -c <trytond.conf> -d <database>" >&2
    echo "  TRYTON_ENV may override the Tryton venv (default:" >&2
    echo "  \$HOME/workspace/projects/hesabyar-reference/tryton-env)." >&2
    exit 2
}

CONF=""
DB=""
while getopts ":c:d:h" opt; do
    case "$opt" in
        c) CONF="$OPTARG" ;;
        d) DB="$OPTARG" ;;
        h) usage ;;
        *) usage ;;
    esac
done
[ -n "$CONF" ] && [ -n "$DB" ] || usage
[ -x "$TRYTOND_ADMIN" ] || {
    echo "trytond-admin not found at $TRYTOND_ADMIN (set TRYTON_ENV)" >&2
    exit 1
}

# A bare `pgrep -f pytest` also matches other agents' wait loops, whose
# shell command lines merely mention the word pytest. Match only real
# pytest processes: the `uv run pytest` wrapper or a venv `bin/pytest`.
# Skipped when HESABYAR_SKIP_PYTEST_GUARD=1 (CI fresh-DB use only).
if [ -z "${HESABYAR_SKIP_PYTEST_GUARD:-}" ] && pgrep -af "bin/pytest|run pytest" >/dev/null; then
    echo "REFUSING: a pytest process is running (see header)." >&2
    pgrep -af "bin/pytest|run pytest" >&2
    exit 1
fi

# shellcheck disable=SC2086
exec "$TRYTOND_ADMIN" -c "$CONF" -d "$DB" \
    -u "${PHASE0_MODULES[@]}" "${F2_MODULES[@]}" "${CHEQUE_MODULES[@]}" \
    --activate-dependencies
