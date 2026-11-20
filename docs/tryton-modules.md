# Tryton modules — inventory and activation record

Which Tryton 8.2 modules HesabYar installs, why, and how to reproduce
the activation on a fresh database. Pinned package versions live in
`~/workspace/projects/hesabyar-reference/tryton-requirements.txt`;
the repeatable activation command is
`scripts/activate-tryton-modules.sh`.

## Inventory

### Phase 0 (foundation)

| Module | Package | Why |
| --- | --- | --- |
| `account` | trytond-account 8.2.2 | Ledger, chart, fiscal years |
| `account_invoice` | trytond-account-invoice 8.2.0 | Invoices |
| `account_payment` | trytond-account-payment 8.2.0 | Payments |
| `account_statement` | trytond-account-statement 8.2.0 | Bank statements |
| `account_asset` | trytond-account-asset 8.2.0 | Fixed assets / depreciation |
| `analytic_account` | trytond-analytic-account 8.2.0 | Analytic (cost-centre) accounting |
| `company` | trytond-company 8.2.0 | Companies |
| `currency` | trytond-currency 8.2.0 | Currencies (IRR) |
| `party` | trytond-party 8.2.1 | Parties |
| `product` | trytond-product 8.2.1 | Products |

Pulled in as dependencies: `account_product` (by `account_invoice`),
`bank` (by `account_payment`), `country` (by `party`). Core: `ir`,
`res`.

### Phase 5, US-502 (F2 — sale / stock / clearing / dunning)

All packages 8.2.0 except `trytond-stock` 8.2.1.

| Module | Package | Why |
| --- | --- | --- |
| `sale` | trytond-sale | Store orders → Sale |
| `sale_shipment_cost` | trytond-sale-shipment-cost | Shipping cost on sales (phase 5چ) |
| `sale_payment` | trytond-sale-payment | Payments linked to sales |
| `sale_discount` | trytond-sale-discount | Line discounts |
| `sale_amendment` | trytond-sale-amendment | Amending confirmed sales |
| `sale_invoice_grouping` | trytond-sale-invoice-grouping | Grouped invoicing |
| `account_payment_clearing` | trytond-account-payment-clearing | Clearing account on payment success (gateway settlement) |
| `account_dunning` | trytond-account-dunning | Dunning (overdue follow-up) |
| `account_dunning_fee` | trytond-account-dunning-fee | Late fees on dunnings |
| `stock` | trytond-stock 8.2.1 | Inventory |
| `stock_supply` | trytond-stock-supply | Replenishment / purchase requests from stock |
| `purchase_request` | trytond-purchase-request | Purchase requests (packaging reorder, phase 5چ) |
| `stock_package` | trytond-stock-package | Packaging |
| `stock_shipment_cost` | trytond-stock-shipment-cost | Shipment costing |
| `carrier` | trytond-carrier | Carriers |
| `account_stock_landed_cost` | trytond-account-stock-landed-cost | Landed cost allocation |
| `account_stock_anglo_saxon` | trytond-account-stock-anglo-saxon | **Inventory method: anglo-saxon** (see below) |
| `analytic_sale` | trytond-analytic-sale | Analytic accounting on sales |
| `account_statement_rule` | trytond-account-statement-rule | Statement import rules (phases 5ت, 5چ) |
| `hesabyar_setup` | trytond-hesabyar-setup 8.2.3 (local) | `install_chart` / `update_chart` / `process_rest` / `assign_and_do_rest` RPC + Iranian chart extension (1.2.9 Bounced Cheques added US-530) |

### Phase 5-4, US-530 (۵ث — cheque module)

| Module | Package | Why |
| --- | --- | --- |
| `account_check_ir` | trytond-account-check-ir 8.2.0 (local) | Iranian cheque registry: `account.checkbook` (bank, leaf range, computed used/voided/remaining), `account.check` (kind received/issued, purpose payment/collateral mandatory, 16-digit Sayyad id with partial-unique constraint, Sayyad status separate from accounting state, storage location, endorsement history, invoice/sale/installment-leg links), `account.check.endorsement`, `account.check.custody_log` (append-only, read-only). No lifecycle journals yet — those land in US-531/US-532. |
| `account_statement`, `account_payment_clearing`, `account_dunning` + `account_dunning_fee` | (already installed, US-502) | Statement import, clearing account, dunning for bounced cheques — installed as dependencies of `account_check_ir`, no rewrites. |

Pulled in as dependencies: `purchase` (by `stock_supply`),
`sale_history` (by `sale`), `account_invoice_stock` +
`account_stock_continental` (by `account_stock_anglo_saxon`),
`analytic_invoice` (by `analytic_sale`).

**Inventory method: anglo-saxon.** Decided 2026-11-18 before
installing `account_stock_*`; see `docs/tryton-spikes-502.md` §7.
Iranian Standard 8 mandates neither method; anglo-saxon (perpetual)
fits real-time store connectivity and is the mainstream Iranian
default (Sepidar). `account_stock_continental` installs as a
dependency of the anglo-saxon module; anglo-saxon behaviour governs.

**Not installed:** no manufacturing, CRM, HR, warehouse or POS
module. `account_dunning`'s email/letter sending is not configured —
dunnings are created and tracked, not mailed, until a later phase
decides otherwise.

## Activation record

- **2026-11-18 (US-502):** the F2 modules above + `hesabyar_setup`
  activated in `hesabyar_tryton_test` (Supabase) via the local
  dump → activate → restore path (direct `trytond-admin -u` against
  Supabase is too slow at ~127 ms/query). 40 modules active in
  total (15 Phase 0 + 20 F2 + 5 transitive). Verified afterwards
  via REST (`ir.module` search) and by the live probes in
  `docs/tryton-spikes-502.md` §6. Pre-activation dump:
  `hesabyar-reference/backups/us502-pre-activation-20261118.dump`;
  activated dump: `backups/us502-activated-20261118.dump`.
  Provisioning fixed at the same time (see spike doc §8): config
  defaults 1.2.1/2.1.1 for the Adapter Test company, and the 4
  invoice sequences + `fiscalyear.invoice_sequence` row for FY
  2026 (both now in `setup_adapter_test_company.py`).
- **Module code updates need no DB migration** when they change no
  schema or XML data (e.g. the `process_rest` addition): rebuild
  the wheel, reinstall into the Tryton venv, restart trytond — the
  pool loads the new code at startup (verified live 2026-11-18).
- **Packaging provisioning (US-515, 2026-11-18) adds no modules** —
  `stock`, `stock_supply`, `purchase_request`, `stock_shipment_cost`,
  `carrier`, `analytic_account`, `sale_shipment_cost`,
  `account_stock_anglo_saxon` were already activated by US-502. It is
  data-only (`setup_packaging_us515.py`, idempotent): the PACK storage
  location under WH, the PACK-CONSUME production location, the
  «مواد بسته‌بندی» category with company accounting, the «سفارش‌ها»
  analytic root, `account_stock_method = anglo_saxon` on all open
  fiscal years (needs `--lock-table account_fiscalyear`), and the
  daily `stock.order_point|supply_stock` cron. Deliberately NOT
  activated: `product_cost_fifo` (FIFO is 422 until it is) and
  `account_stock_shipment_cost` (wizard-only posting; see
  `docs/packaging.md`).
- **2026-11-18 (US-516):** `hesabyar_setup` chart extended with 4
  cash-box accounts (1.1.5 Petty Cash, 1.2.6 Cash Shortage and
  Overage, 1.2.7 Temporary Debtors — Cash Shortage, 2.1.6
  Miscellaneous Creditors — Cash Overage; see `docs/cashbox.md`).
  Module updated with a direct `trytond-admin -u hesabyar_setup`
  against Supabase (acceptable for one small module — the
  dump/restore warning is for full activation), then `update_chart`
  on the Adapter Test company: 145 → 149 accounts. Statement
  journals provisioned for the Adapter Test company
  («HesabYar US-516 Store Cash Box» on 1.1.1, «HesabYar US-516
  Petty Cash Box» on 1.1.5) via
  `~/workspace/coord-logs/us516-provision-cashboxes.py`. The IRR
  Test company (id 2) has no chart — only two hand-made test
  accounts (1000/4000) — and was left alone.
- **2026-11-19 (US-530):** `account_check_ir` 8.2.0 + `hesabyar_setup`
  8.2.3 (chart + 1.2.9 Bounced Cheques) activated on all three Tryton
  DBs (Supabase lane 1 :8010, lane 2 :8011, local :8020) via direct
  `trytond-admin -u account_check_ir hesabyar_setup
  --activate-dependencies` (one small module + one chart update —
  the US-516 precedent), then `update_chart` on the Adapter Test
  company on each DB (lane 1: 157 accounts, 1.2.9 added by an
  earlier timed-out call; lane 2: 149 → 157; local: 149 → 157).
  Two fixes found by the live tests and re-applied with `-u
  account_check_ir` + trytond restart on each DB: optional
  Selection fields need an explicit `(None, '')` option or Tryton
  rejects `None`, and a multi-name Function getter must return
  `{field: {record: value}}`, not the transposed shape. The
  `bank.bank` model name does not exist — the Bank model is named
  `bank` (only `bank.account` carries the prefix). Live contract
  tests: `tests/contract/test_tryton_cheques_us530.py` (lane 1).
  `update_chart` over REST needs a long client timeout (600s) on
  Supabase — the 30s default times out client-side while the
  server-side transaction still commits.

## How to reproduce on a fresh database

```bash
# 1. Install the packages into the Tryton venv
uv pip install --python <tryton-env>/bin/python \
  -r ~/workspace/projects/hesabyar-reference/tryton-requirements.txt
#    (the requirements file also lists trytond-hesabyar-setup; build
#    the wheel first from tryton-modules/hesabyar_setup/ if it is
#    not on an index: python3 setup.py bdist_wheel)

# 2. Activate all modules (idempotent; --activate-dependencies
#    pulls in the transitive set)
scripts/activate-tryton-modules.sh -c <trytond.conf> -d <database>
```

For a remote database (Supabase), activate against a local copy
instead — see the script's `--help` and the US-502 activation notes
in the script header.
