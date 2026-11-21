# Performance baselines (US-702, Phase 7)

Latencies for the hot paths, measured by the load tests — **no number
in this document is claimed without a measurement behind it**. Every
cell marked *to be measured* is exactly that: the numbers can only
be produced when CI runs the tests on the CI hardware, and inventing
them would be worse than leaving the table empty.

## How the numbers are produced

The load tests record per-request latencies with
`tests/load_support.py` (`LatencyRecorder`) and print a percentile
summary to stdout on every run. The tests themselves assert on
outcomes + counts (exactly-once counts, no duplicate numbers, 429
counts) — never on milliseconds — so the numbers below are
measurements, not pass/fail gates.

To produce the JSON file the table is filled from:

```bash
HESABYAR_RECORD_BASELINES=1 HESABYAR_BASELINES_PATH=performance-baselines.json \
  uv run pytest \
    tests/interfaces/test_rest_load_us702.py \
    tests/contract/test_concurrent_reports_us702.py \
    tests/contract/test_invoice_numbering_us702.py \
    tests/unit/test_settlement_tieout_load_us702.py \
    tests/mcp/test_mcp_rate_limit_load_us702.py \
    -q -p no:cacheprovider
```

Each test merges its measurements into the one JSON file (same-named
measurements overwrite older ones), so a full run leaves a single
file with every path's numbers. The file is git-ignored; the
committed numbers live in the table below. The JSON also records
the platform, Python version and CPU count of the run that produced
it — copy those into the "measured on" line when filling the table.

The Tryton-gated tests (`test_concurrent_reports_us702.py`,
`test_invoice_numbering_us702.py`, and the dashboard core-batch test
inside `test_rest_load_us702.py`) only measure in the
`python-tests-tryton` CI job; in the plain `python-tests` job they
skip. The remaining paths measure in both jobs.

## Hardware the numbers are measured on

CI runs on GitHub-hosted `ubuntu-latest` runners
([GitHub-hosted runners reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)):

| Repository | vCPU | RAM | SSD |
| --- | --- | --- | --- |
| Public | 4 | 16 GB | 14 GB |
| Private | 2 | 8 GB | 14 GB |

The same label is half the machine in a private repository — the
"measured on" line must say which shape the run landed on (the JSON
file's `cpus` field shows it: 4 vs 2). The Postgres 16 service
container and, in the Tryton job, the trytond server run on the
same runner as the tests, so the numbers include local-container
database round-trips, not network latency to a remote database.

## What each path's load is

| Path (recorder name) | Load | Test |
| --- | --- | --- |
| `webhook.ingest` | 1,000 distinct events, sequential, one HTTP POST each | `test_rest_load_us702.py::test_webhook_ingestion_burst_1000_events` |
| `webhook.ingest.concurrent` | 1,000 distinct events, 10 threads x 100 | `test_rest_load_us702.py::test_webhook_ingestion_burst_concurrent` |
| `dashboard.review_queue.summary` | 10 concurrent users | `test_rest_load_us702.py::test_dashboard_read_set_concurrent_users` |
| `dashboard.cheques.list` | 10 concurrent users | same |
| `dashboard.integrations.inbox` | 10 concurrent users, 50 seeded events | same |
| `dashboard.settlements.tranches` | 10 concurrent users | same |
| `dashboard.imports.batches` | 10 concurrent users | same |
| `dashboard.journal_drafts` | 5 concurrent users (Tryton) | `test_rest_load_us702.py::test_dashboard_core_batch_concurrent_users_tryton` |
| `dashboard.invoices` | 5 concurrent users (Tryton) | same |
| `dashboard.payments` | 5 concurrent users (Tryton) | same |
| `dashboard.cash_boxes` | 5 concurrent users (Tryton) | same |
| `reports.trial_balance` | 8 concurrent threads, Nov 2026 (Tryton) | `test_concurrent_reports_us702.py` |
| `reports.balance_sheet` | 4 concurrent threads, Nov 2026 (Tryton) | same |
| `reports.income_statement` | 4 concurrent threads, Nov 2026 (Tryton) | same |
| `reports.general_ledger` | 4 concurrent threads, Nov 2026 (Tryton) | same |
| `invoices.create_validate_post` | 16 invoices, 8 threads x 2 (Tryton) | `test_invoice_numbering_us702.py` |
| `settlement.tie_out.2000_tranches` | One tie-out over 2,000 matched tranches (in-memory fakes) | `test_settlement_tieout_load_us702.py` |
| `mcp.rate_limit_middleware` | 100 concurrent requests, limit 20 (in-process ASGI) | `test_mcp_rate_limit_load_us702.py` |

## Baselines

**Measured on:** *to be measured on the first CI run that executes
the US-702 load tests (fill in the runner shape + date + run id
here).*

| Path | n | p50 | p95 | p99 | max |
| --- | --- | --- | --- | --- | --- |
| `webhook.ingest` | 1000 | *to be measured* | *to be measured* | *to be measured* | *to be measured* |
| `webhook.ingest.concurrent` | 1000 | *to be measured* | *to be measured* | *to be measured* | *to be measured* |
| `dashboard.review_queue.summary` | 10 | *to be measured* | *to be measured* | *to be measured* | *to be measured* |
| `dashboard.cheques.list` | 10 | *to be measured* | *to be measured* | *to be measured* | *to be measured* |
| `dashboard.integrations.inbox` | 10 | *to be measured* | *to be measured* | *to be measured* | *to be measured* |
| `dashboard.settlements.tranches` | 10 | *to be measured* | *to be measured* | *to be measured* | *to be measured* |
| `dashboard.imports.batches` | 10 | *to be measured* | *to be measured* | *to be measured* | *to be measured* |
| `dashboard.journal_drafts` | 5 | *to be measured* | *to be measured* | *to be measured* | *to be measured* |
| `dashboard.invoices` | 5 | *to be measured* | *to be measured* | *to be measured* | *to be measured* |
| `dashboard.payments` | 5 | *to be measured* | *to be measured* | *to be measured* | *to be measured* |
| `dashboard.cash_boxes` | 5 | *to be measured* | *to be measured* | *to be measured* | *to be measured* |
| `reports.trial_balance` | 8 | *to be measured* | *to be measured* | *to be measured* | *to be measured* |
| `reports.balance_sheet` | 4 | *to be measured* | *to be measured* | *to be measured* | *to be measured* |
| `reports.income_statement` | 4 | *to be measured* | *to be measured* | *to be measured* | *to be measured* |
| `reports.general_ledger` | 4 | *to be measured* | *to be measured* | *to be measured* | *to be measured* |
| `invoices.create_validate_post` | 16 | *to be measured* | *to be measured* | *to be measured* | *to be measured* |
| `settlement.tie_out.2000_tranches` | 1 | *to be measured* | *to be measured* | *to be measured* | *to be measured* |
| `mcp.rate_limit_middleware` | 100 | *to be measured* | *to be measured* | *to be measured* | *to be measured* |

## Reading the numbers (when they exist)

- **Per-request setup dominates the REST paths.** Every REST request
  builds a short-lived SQLAlchemy engine (`NullPool`, no pooling
  across requests) and, for engine-backed reads, a short-lived
  Tryton REST client (`interfaces/rest/dependencies.py`). The
  measured latency is therefore mostly connection + client setup,
  not query time — a shared engine/pool story would move these
  numbers more than any query tuning. Do not compare these numbers
  against a pooled deployment without saying so.
- **The webhook burst includes a best-effort job deferral.** Each
  created event defers a Procrastinate processing trigger; when the
  queue is unreachable the defer fails soft (the periodic sweep is
  the backstop), but the attempt still costs a round-trip. A burst
  against a live queue measures differently from one against a
  missing queue.
- **The settlement tie-out is in-memory.** It measures the
  computation over 2,000 matches, not database access — the real
  repository reads are not in this number.
- **The MCP middleware number is in-process.** It measures the
  limiter + ASGI plumbing through `httpx2.ASGITransport`, with no
  socket and no MCP server behind it.
- **n is small for the dashboard/report paths.** p95/p99 over 4–10
  samples are coarse by construction; they are recorded for
  completeness, and the p50 is the number to quote.
- **Re-measure, don't extrapolate.** A baseline is a snapshot of one
  run on one runner shape. If the load, the data volume or the
  runner shape changes, re-run the command above and update the
  table + the "measured on" line instead of scaling the old numbers.

## Concurrency guarantees pinned by these tests (outcomes, not latencies)

- Duplicate concurrent events are processed exactly once (spec
  §19.9): the inbox advisory lock + unique constraint serialize
  ingest (one 201, the rest 200s, one row, one completed
  idempotency record), and two workers racing `process_one` on one
  event claim it once (`FOR UPDATE SKIP LOCKED`).
- Concurrent cheque registration with one Sayyad number yields one
  live row; the losers get the typed `ChequeDuplicateError` (HTTP
  409) or the existing cheque back (same-claim convergence), never
  a raw `IntegrityError` / 500 — the repository translates the
  live-sayad unique-index violation (US-702 fix, following the
  inbox/idempotency savepoint pattern).
- Concurrent invoice posting assigns unique numbers (Tryton
  sequence); gaps are allowed by design (a rolled-back transaction
  loses its number) and are not asserted.
- The MCP rate limiter admits exactly the limit per key per window
  under concurrent load (US-702: `allow` is one locked critical
  section) and answers past-budget requests with 429 +
  `Retry-After`, never a 5xx. The REST API has no rate limiter —
  MCP Streamable HTTP is the only rate-limited surface.
