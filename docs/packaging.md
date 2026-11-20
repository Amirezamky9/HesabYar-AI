# Packaging & Shipping Cost (US-515)

K1 — packaging materials + shipping cost tracking (Phase 5 wave 2;
phased design §1.3 stream 5چ, §6 هـ.۱, §7; ADR 0003). The user's own
requested feature, separate from the design-eng spec: packaging
materials are **consumables** (operating expense) — never depreciation.
Depreciation is fixed assets only.

## What lives where

| Piece | Location |
|---|---|
| Tables | Alembic `0017`: `packaging_materials`, `packaging_consumptions`, `packaging_shipment_costs`, `packaging_carrier_rates`, `packaging_counts`, `packaging_order_cost_centers`, `packaging_settings`, `packaging_supply_watermarks` |
| ORM models | `src/hesabyar/infrastructure/persistence/postgres/models.py` (`Packaging*Model`) |
| Domain model | `src/hesabyar/domain/packaging/models.py` (+ `coverage.py`, `calculator.py`, `errors.py`, `ports.py`) |
| Ports | `src/hesabyar/domain/packaging/ports.py` (`PackagingRepositoryPort`); engine side: `src/hesabyar/domain/accounting/ports.py` (17 packaging methods) |
| Repository | `src/hesabyar/infrastructure/persistence/postgres/packaging_repository.py` |
| Query service | `src/hesabyar/application/queries/packaging.py` |
| Command service | `src/hesabyar/application/commands/packaging.py` |
| Adapter | `src/hesabyar/infrastructure/accounting/tryton/adapter.py` (+ `mapping.py`) |
| REST | `src/hesabyar/interfaces/rest/routers/packaging.py` (+ `schemas/packaging.py`) — 26 operations, 19 paths under `/api/v1/packaging` |
| Supply-check worker | `src/hesabyar/infrastructure/events/packaging_queue.py` (Procrastinate queue `packaging`, periodic `packaging.supply_check`, default `30 6 * * *`); wired in `scripts/run_event_worker.py` |
| Admin pages | `apps/admin/app/(dashboard)/packaging/` + `apps/admin/features/packaging/` |
| Provisioning | `~/workspace/projects/hesabyar-reference/setup_packaging_us515.py` |
| Tests | `tests/unit/test_packaging_services.py` (51), `tests/unit/test_packaging_adapter.py` (25), `tests/interfaces/test_rest_packaging.py` (25), `tests/contract/test_tryton_packaging.py` (13) |

## Modelling decisions (verified against Tryton 8.2.3 source)

### Materials are goods, NOT the Tryton `consumable` flag

"Consumable" is the *business* classification (operating expense, never
depreciation). The Tryton `consumable` flag means something else: "assign
moves regardless of stock level" — and it *excludes* products from
`stock.order_point` (domain `consumable=False`),
`purchase.request.generate_requests` (searches `consumable=False`) and
inventory line-filling. A material flagged consumable could never get a
reorder point or a purchase request. So materials are:

- type `goods`, `consumable=False`, `purchasable=True`, `salable=False`
- cost method `average` (default) or `fifo` — FIFO needs the
  `product_cost_fifo` module, which is NOT installed; the adapter raises
  `PACKAGING_FIFO_UNAVAILABLE` (422) until it is activated. The base
  `COST_PRICE_METHODS` are fixed/average only, and the dev DB's product
  configuration defaults to `fixed` — the adapter sets `average`
  explicitly on every material template.
- `purchase_uom` is set to the default UoM: a purchasable template
  requires it (the `purchase` module validates it — the contract
  tests caught the missing field against live Tryton).
- The single variant is created explicitly (`product.product` create
  with the template id) after the template: `Template.default_products`
  returns `[]` for the REST user (`transaction.user == 0`), so a
  template created via REST has NO variant unless one is created for
  it — the Sao default of one empty variant does not apply here, and
  a nested `"products": [{}]` payload is rejected by the REST layer.

### Consumption moves go packaging location → production location

Under anglo-saxon accounting this posts debit `account_stock_out`
(packaging expense) / credit `account_stock` (inventory). Storage →
storage internal moves post nothing; storage → lost-found is
semantically wrong (reserved for inventory discrepancies).

`Move.do` is a button (REST-callable) but it is **readonly on draft
moves out of a storage location** (`assignation_required` is true
for storage/view origins), and `Move.assign` (draft → assigned) is
neither a button nor in `__rpc__` — so a standalone consumption move
cannot be completed through stock Tryton over REST at all. The
`hesabyar_setup` module (v8.2.2) adds `assign_and_do_rest`, a thin
`__rpc__` wrapper that calls stock Tryton's own `assign` + `do` in
one transaction — the same pattern as `process_rest` for payments
(see `tryton-modules/hesabyar_setup/.../move.py`). Access checks
are disabled for the call (the `do` body reads `res.user`
internally, which the group-less REST service account cannot read
with checks on). `assign` does NOT
check stock availability, so consumption larger than stock goes
negative by design (a warning signal, not an error, per design
§هـ.۱.۶). The move carries NO currency and NO unit price:
`stock.move`'s domains require both to be None when
`unit_price_required` is False (standalone internal moves); sending
either is rejected (contract test caught it). The engine computes
the accounting cost price itself at `do` time.

### Category accounting (dev chart, company 1)

The `account_stock_in` / `account_stock_out` domain requires
`type.stock=True` (and, under anglo-saxon, statement `income` or
`balance`). The only stock-flagged types in the universal chart are
`Inventories` (balance) and `Selling, General and Administrative
expenses` (income). The 5.1.1 expense type does NOT have `stock=True`,
so the design's first draft (5.1.1 for stock_in/out) cannot be used.
The provisioning script sets:

| Field | Account | Why |
|---|---|---|
| `account_stock` | 1.3.2 Raw Material, Parts And Supplies | inventory |
| `account_stock_in` | 1.3.2 | clearing: receipt credits it, the supplier invoice debits it (invoice line account is `account_stock_in_used` under anglo-saxon); the pair nets on the inventory account |
| `account_stock_out` | 5.2.2 Selling, General And Administrative | the consumption expense — packaging used in operations is SG&A |
| `account_expense` | 5.1.1 | purchase expense (fixed-cost price differences) |
| `account_cogs` | 5.2.1 | cost of sales |
| `account_revenue` | 4.1.1 | goods revenue |

Category account fields are MultiValue (company-dependent, stored in
`product.category.account` rows); the provisioning script sets them
with company context. All six account ids are resolved by code at
provision time — never hard-coded (the probe's "ids" 21/37/38 were
account *type* ids, not account ids).

### Anglo-saxon must be switched on per fiscal year

Every fiscal year on the dev DB had `account_stock_method = None`, so
stock moves created NO accounting moves. The provisioning script sets
`anglo_saxon` on every open fiscal year of the company (and needs
`--lock-table account_fiscalyear` — `FiscalYear.write` calls `lock()`).

The contract test `test_consumption_move_posts_anglo_saxon_accounting`
is the proof: receive 10 @ 12000, consume 3 → stock 7 and an account
move debit 5.2.2 / credit 1.3.2 for 36000.

### Order points attach to the warehouse

`stock.order_point` with type `purchase` requires a location of type
`warehouse`; quantities roll up child locations (so the PACK location's
stock counts toward the warehouse point). `max_quantity` is ignored for
purchase points.

### The supply wizard runs inside Tryton, not via REST

`stock_supply` extends `ir.cron` methods with
`stock.order_point|supply_stock`; `OrderPoint.supply_stock()` runs the
wizard server-side. `methods_company_needed` means one cron row runs
per company. The provisioning script creates the cron (daily, 06:00
UTC). **Deployment requirement: `trytond-cron` must run** — the cron
never fires in this dev environment. HesabYar only READS the run's
results: the Procrastinate supply-check worker (default 06:30, after
the wizard) lists new `purchase.request` rows since a monotonic
watermark and files `packaging_reorder` review items for them, plus
low-stock items from live days-of-cover. Filing is deduplicated
against open items by `source_event_ref`
(`purchase_request:{id}` / `low_stock:{material_id}`), so a daily run
never spams the queue.

### Days-of-cover is computed HesabYar-side

From recent consumption moves (30-day window by default): average over
the FULL window, None when there was no consumption in the window (the
UI shows "—", never a fake number), negative when stock is negative.
`is_low` is true at/below the alert threshold — or whenever stock is
<= 0.

### Engine-level analytic tagging of consumption moves is impossible

`analytic.account.entry` origins are only `analytic_account.rule`,
`sale.line` (analytic_sale) and `account.invoice.line` (+
`account.asset`) — NOT `stock.move` or `account.move.line`. So
consumption and shipment costs carry the order's analytic account as a
**HesyabYar-side tag**; the per-order cost report merges Tryton
analytic lines (goods cost, revenue) with HesabYar-side records
(packaging, shipment). One analytic account per order, created under
the «سفارش‌ها» root (type `root`, `root=None`); normal accounts take
the root as both root and parent.

### Shipment cost is three separate figures

- **Invoiced to the customer**: `stock.shipment.cost_sale` rows for the
  sale (`sale_shipment_cost`: On Order / On Shipment). When a shipment
  cost is recorded with a linked sale, the engine figure is
  authoritative — a mismatch with the submitted figure is 422.
- **Actual paid to the carrier**: `stock.shipment.out.cost`
  (`stock_shipment_cost`, editable via `cost_edit`) or manual entry.
- **Absorbed into margin**: `actual - invoiced` (derived, never stored
  independently).

Carrier rates live HesabYar-side as user-entered config: the base
`carrier` module alone has no cost method (`carrier_weight` etc. are
not installed), and rates must never be hard-coded.

### `account_stock_shipment_cost` — UNVERIFIED item, RESOLVED, NOT used

Read from the 8.2.0 wheel source (2026-11-18): the module adds
`account.shipment_cost`, linking *posted supplier invoice lines*
(products flagged `shipment_cost=True`, service type) to *done
outgoing shipments*, allocating the carrier invoice cost across
shipments (factor 1/N) onto each shipment's `cost` field. It creates
**no accounting move itself**; posting is **wizard-only**
(`post_wizard` is a button_action; `post` is a plain transition, not a
button/RPC). Decision: do NOT install or use it in US-515 — it would
need a `hesabyar_setup` REST wrapper plus module activation on the
shared DB mid-verification, and K1's three-figure model already
captures the actual carrier cost via `stock.shipment.out.cost` +
manual entry. If it is ever adopted, the path is: activate the module,
add a REST wrapper for the post wizard in `hesabyar_setup`, and wire
the allocated cost into the shipment-cost record.

### Inventory counts

`stock.inventory` states draft/done/cancelled; `confirm` and `cancel`
are buttons (REST-callable); `complete_lines` is a button (fills
expected quantities); `do_count` is a wizard (not needed — lines are
created directly via REST). The counted location must be type
`storage`. Adjustments use the warehouse's lost-found location
(`location.lost_found_used` → warehouse `lost_found_location`, id 4 on
the dev DB). The HesabYar count keeps expected vs counted per line in
JSONB; confirming requires every line counted (the human approval
step).

Two inventory gotchas, both found by the contract tests:

- **Company context is mandatory.** `confirm` runs the tolerance
  check, which reads each product's `cost_price` — a company-dependent
  MultiValue resolving to None without a company in context, crashing
  the confirm with `None * Decimal`. The adapter reads the
  inventory's company first and passes it as action context.
- **`empty_quantity` must be set when lines are uncounted.**
  `complete_lines` fills a line per stocked product, and `confirm`
  refuses to proceed while any line has no counted quantity unless
  `empty_quantity` is set. The adapter sets `'keep'` at creation
  (uncounted lines keep their expected quantity, no adjustment);
  `'zero'` would wipe the stock of any material left uncounted.

### Engine timeouts: 30s is too short over a remote DB

Measured on lane 2 against Supabase (2026-11-18): create product
~17s, a single stock-move `do` 24–41s, a 14-line inventory confirm
**53s**. With the old 30s timeouts the client gave up even though the
server committed — inventories 10–12 all landed `done` while the
client reported a timeout. Two timeouts were raised to 120s, with the
measurements cited in the code comments:

- `TrytonSettings.timeout_seconds` (default 30 → 120; the production
  REST path) and the packaging contract-test fixture — same pattern
  as the sales contract test's 120s timeout.
- `UPSTREAM_TIMEOUT_MS` in `apps/admin/lib/backend.ts` (30s → 120s):
  the BFF aborted writes the engine had already committed, failing
  4/5 packaging e2e with false UI errors. This also addresses the
  Phase 4 journal e2e timeouts (same class of failure).

### Materials list: batch the product reads

`list_materials` did one product+template read pair per material:
~30s for 20 materials, right at the BFF timeout. New port method
`list_packaging_products` does one `product.product` search plus one
`product.template` search, joined in memory (list: 30.1s → 14.6s).
A product missing from the batch is reported as unknown stock, never
re-read singly.

### Create-material detail is built, not re-read

The create endpoint used to re-read the full detail after creating
(order points + moves + locations + product, ~12s), pushing it to
~29s. A fresh product has zero stock, no consumption and no reorder
point by construction, so `create_material` now returns the
`MaterialDetail` built from those facts (cost price from the create
read-back it already does). The location name is empty in that
response — it resolves on the list refresh the UI does anyway.

### Price calculator

HesyabYar-side only: goods + packaging (current cost) + shipment
(carrier rate or explicit cost — mutually exclusive) + margin (percent
XOR fixed). Line rounding is HALF_UP. The result is labelled
approximate (`approximate: true`) and NEVER auto-applies to an
invoice. Warnings fire when a material is out of stock (stale price)
or no packaging location is configured (unknown stock).

### No new event types

`inventory.packaging.*` / `shipment.cost.charged` were proposed in the
phased design but are NOT approved event names — new canonical event
names need ADR approval first (ADR 0003 round-2 update 8; phased
design §8.4). US-515 adds no event types to the event platform. The
low-stock signal goes to the review queue (`packaging_reorder` item)
instead. If events are wanted later, they need an ADR amendment first.

## Provisioning

```bash
cd ~/workspace/projects/hesabyar-reference
./tryton-env/bin/trytond-console -c trytond-supabase.conf \
    -d hesabyar_tryton_test \
    --lock-table account_fiscalyear \
    -f setup_packaging_us515.py
```

Idempotent (safe to re-run). Creates, for company 1 "HesabYar Adapter
Test": the PACK storage location (code `PACK`) under warehouse WH, the
PACK-CONSUME production location, the «مواد بسته‌بندی» category with
the accounting above, the «سفارش‌ها» analytic root, anglo-saxon on all
open fiscal years, and the daily supply cron. It prints the engine ids
at the end — store them in packaging settings
(`PUT /api/v1/packaging/settings`) or let the service resolve them by
code (`PACK` / `PACK-CONSUME` / `WH`).

Dev DB ids (2026-11-18; re-run the script to confirm after any
re-provision): PACK location 15, PACK-CONSUME 16, warehouse 5,
category 7, analytic root 2, supply cron 17. Accounts: 1.3.2 → 43,
5.2.2 → 113, 5.1.1 → 106, 5.2.1 → 112, 4.1.1 → 99.

## REST API

26 operations / 19 paths under `/api/v1/packaging` (scopes
`packaging:read` / `packaging:manage`; catalog now 34 scopes):

| Method | Path | Purpose |
|---|---|---|
| GET/POST | `/packaging/materials` | List (cursor-paginated, detail per row) / create |
| GET/PATCH | `/packaging/materials/{id}` | Detail (live stock, reorder point, coverage) / update catalog fields |
| PUT | `/packaging/materials/{id}/reorder-point` | Set min/target order point |
| GET | `/packaging/materials/{id}/coverage` | Days-of-cover |
| POST/GET | `/packaging/consumptions` | Record (idempotent by `pack:use:{order}:{shipment}:{material}` — replay returns 200 with the same record) / list |
| POST | `/packaging/orders/{order_ref}/cost-center` | Create the order's analytic cost centre (duplicate → 409) |
| GET | `/packaging/orders/{order_ref}/cost` | Full order cost (goods + packaging + shipment, margin) |
| GET/POST | `/packaging/shipment-costs` | List / record the three figures |
| POST | `/packaging/price-calculator/quote` | Approximate quote (read-only, `packaging:read`) |
| GET/POST | `/packaging/carrier-rates` | List / create carrier rates |
| PATCH | `/packaging/carrier-rates/{id}` | Update a rate |
| POST/GET | `/packaging/counts` | Open a count / list counts |
| GET | `/packaging/counts/{id}` | Count detail (refreshed from the engine) |
| POST | `/packaging/counts/{id}/lines` | Set a counted quantity |
| POST | `/packaging/counts/{id}/confirm` | Confirm (requires all lines counted) |
| POST | `/packaging/counts/{id}/cancel` | Cancel |
| GET/PUT | `/packaging/settings` | Per-company settings (defaults when never configured) |
| POST | `/packaging/supply-check` | Run the supply check now (same logic as the worker) |
| GET | `/packaging/locations` | Stock locations for the company |

Company travels as a `company_id` query parameter on GETs and a body
field on writes (API row id or engine id). Money is whole IRR ints;
quantities and unit costs are decimals. Dates are Gregorian ISO.

## Deferred / not in US-515

- FIFO costing until `product_cost_fifo` is activated (422 until then).
- `account_stock_shipment_cost` (see above).
- New event types (need ADR approval first).
- The order link to S1 (US-511): order association is manual entry
  for now; it wires to the store connector when US-511 lands.
- `POST /integrations/{connector}/sync` and per-type payload
  validation (5ب work, not packaging).
- `trytond-cron` in production (deployment requirement — the supply
  cron never fires without it).
