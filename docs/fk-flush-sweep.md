# FK-flush sweep (US-555, Phase 5-6 ۵ح)

## The rule

SQLAlchemy 2.0 does **not** order a flush by bare-FK dependency
without a `relationship()` between the mappers: when a parent row
and its child rows are added to one session and flushed together,
the child INSERTs can be emitted first and violate the child's FK
(`*_fkey`). Proven by probe in the ۵ج fix pass — CI run #6 failed
with FK violations on `installment_legs.plan_id` (26 tests).

The established fix (installments repository, ۵ج) is an explicit
`session.flush()` of the parent **before** adding the children.
No SQLAlchemy relationships are added as the fix — the bare-FK
design stays. Every repository must flush parents before children
wherever the pattern recurs.

This sweep audited every writer in
`src/hesabyar/infrastructure/persistence/postgres/` (method by
method, via an AST scan of the add/flush sequence in each method,
plus a grep confirming no `session.add` exists anywhere else in
`src/` and no Core `insert()` / bulk operation exists at all).
Sessions use SQLAlchemy's defaults (`autoflush=True`); the
codebase does **not** rely on autoflush — the explicit flush is
the pattern, so a method is only "clean" when its own flushes
order the writes.

## Verdicts

| Repository | Verdict | Detail |
| --- | --- | --- |
| `bank_repository.py` | **FIXED** | `save_batch` added the batch + all rows in ONE flush — the known hash-order-luck case. Now flushes the batch before adding rows (`bank_import_rows.batch_id_fkey`). `save_mapping` / `save_rule` / `save_match`: single-row adds, clean. |
| `installments_repository.py` | clean (verified) | The ۵ج fix is complete: `create_plan` flushes the plan before the legs, `create_receipt` flushes the receipt before the allocations. Single-row creates (`create_contract`, `create_late_fee_assessment`, `create_schedule_amendment`) reference pre-existing parents. |
| `settlement_repository.py` | clean | Every save flushes immediately. Parent-creating methods (`save_payment`, `save_file_import`) flush before the caller adds children — call order verified: `settlement.py` 282→293 (`save_payment` → `save_tranches`), `settlement_import.py` 562→636 (`save_file_import` → `save_file_rows`). `save_tranches` / `save_file_rows` add same-table siblings only, referencing a pre-existing parent. |
| `cheques_repository.py` | clean | Every write adds at most one row and flushes: `create`, `record_match_history`, `add_custody_entry`, `add_inquiry_receipt`, `save_settings`. Children always reference a pre-existing cheque. |
| `manual_import_repository.py` | clean | `save_batch` and `save_settlement_mapping` are single-row adds + flush. |
| `standards_repository.py` | clean | `save_validation_run` already follows the pattern: run add + flush, then `add_all` results + flush. |
| `event_repositories.py` | clean | Inbox `store`/`save`, outbox `save`/`save_integration_attempt`, external-id-map save, decision-audit `record`, connector-settings save — all single-row adds + flush. |
| `integrations_repository.py` | clean | All 9 `save_*` methods (channel, contract, mapping, suggestion, policy config, bank connection, store, installation settings, holiday) are single-row adds + flush. |
| `repositories.py` | clean | Actor / ApiClient / ApiKey / Tenant saves are single-row adds + flush. These are also the only models WITH relationships (`TenantModel.actors` etc.), so even a multi-add would be ordered by SQLAlchemy. |
| `review_queue_repository.py` | clean | `save_item`, `upsert_policy`, `record` — single-row adds + flush. |
| `cashbox_repository.py` | clean | `save_cash_box`, `save_session`, `upsert_settings` — single-row adds + flush. |
| `moadian_repository.py` | clean | All 6 `save_*` methods (profile, document, submission, status event, outbox event, integration attempt) — single-row adds + flush. |
| `packaging_repository.py` | clean | All 8 `save_*` methods — single-row adds + flush. |
| `sms_repository.py` | clean | `create_provider`, `save_reminder_settings`, `save_contract_settings`, `insert_log` — single-row adds + flush. |
| `audit.py` | clean | `record` — single-row add + flush. |
| `idempotency.py` | clean | `claim` — single-row add + flush. |
| `manual_entry_repository.py` | clean | `save` — single-row add + flush. |
| `report_snapshot_repository.py` | clean | `save_snapshot` — single-row add + flush. |
| `tax_policy_repository.py` | clean | `save_decision` — single-row add + flush. |
| `seed_payment_channels.py` | N/A | Seeds the settings row + channel rows in one flush, but `PaymentChannelModel.installation_id` is a plain string column — **no FK** to `installation_settings` exists, so there is no ordering hazard. (The loop's SELECT would autoflush the settings row first anyway.) |
| `seed_sms_providers.py` | clean | Flushes after each row. |
| `seed_standards.py` | clean | Sources flushed before revisions, revisions before rules. |
| `seed_tax_policy.py` | clean | Sources flushed before versions. |

## What the sweep did NOT find

- No other parent+child single-flush method exists in the tree.
- No service creates a child through one repository method before
  its parent's repository method has flushed — every repository
  save flushes, so cross-repository ordering follows call order.
- No `session.add` / `add_all` outside `persistence/postgres/`
  (verified by grep over `src/` + `scripts/`).

## Regression tests

- `tests/unit/test_bank_repository.py` — DB-free, deterministic:
  a recording session asserts the batch add → flush → row adds →
  flush order (the DB error itself is hash-order luck, so a DB
  test alone cannot prove the fix).
- `tests/infrastructure/test_bank_repository.py` — real-DB:
  `save_batch` persists a batch + its rows in one call.
