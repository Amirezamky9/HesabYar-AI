# Implementation handoff: HY-ARCH-1.2.0-R1

Baseline reviewed: `06c99b58e01f3341f981eb16b334d3f1ed84a10c`.
Start from the commit containing this handoff, not from the historical R0 design.
Record the actual checkout SHA before work. Scope: implementation and verification;
do not reopen the product vision or introduce a new framework without an ADR.
Production remains blocked. A milestone is complete only with code and acceptance
evidence, not a diagram, placeholder or unchecked claim in README.

## F0: establish a reproducible foundation

Create `pyproject.toml`, a dependency lock and `src/hesabyar/` domain/application/
adapter boundaries. Select and pin the official MCP Python SDK and the supported
Python/SQLite/client versions; verify SQLite >=3.37 and JSON/foreign-key/recursive-
trigger behavior on supported platforms. Keep source-conversion dependencies
optional. Preserve upstream LICENSE and knowledge paths. Implement typed manifest
loaders with archive exclusion and zero active fiscal rules by default.

Acceptance: clean environment install, packaged resources present, no import-time
stdout mutation/logging that corrupts stdio MCP, offline startup, manifest invalid
or missing -> fail closed. Unit tests run without credentials or fiscal endpoints.

## F1: fix the inherited validator before trusting any financial result

Implement a safe expression interpreter/operation registry in
`src/hesabyar/validation/`. Migrate consumers of `scripts/validator.py` through an
explicit compatibility adapter. Remove string replacement/eval and float coercion.
Preserve useful formulas, not their unsafe evaluation semantics. Quote YAML source
paragraphs; separate mandatory identities from configurable risk heuristics.

Acceptance: PASS/FAIL/NOT_APPLICABLE/MISSING_DATA/ENGINE_ERROR are distinguishable;
empty rules or mandatory missing data cannot make `is_valid=True`. Add regressions
for nested paths, missing aggregates, missing method/context, overlapping names,
zero division, large IRR values, unsupported expressions and unavailable rule files.
Update tests that currently call skipped/unevaluable rules successful. Do not weaken
rules to keep the old sample green; correct samples or mark genuine non-applicability.

## F2: implement the approved local ledger slice

Turn the reference `docs/contracts/ledger.sql` into numbered migrations plus the
application obligations in `docs/contracts/domain-contracts.md`. Implement strict
DTOs, entity binding, chart seeding/versioning, fiscal periods, draft revisions,
trusted approval, atomic posting/audit/idempotency, exact reversal, closing/reopening
and consistent trial balance. Keep all financial network effects disabled.

Acceptance: the reference tests plus production-adapter tests for concurrent number
allocation, post+audit rollback, approval replay/tampering, book mismatch, account
snapshot stability, exact reversal/dimensions, migration upgrades, backup/restore
and per-connection PRAGMAs. Audit that a posted object cannot mutate through another
public method. No arbitrary SQL tool may be exposed to an agent.

## F3: expose a thin MCP interface

Implement stdio read-only knowledge/validation/report tools first, then separately
authorized local prepare/post/reverse tools. Use bounded typed structured results,
error mapping and pagination. Remote Streamable HTTP is optional for the first
slice and requires authentication, origin checks, TLS and a pinned compatibility
matrix. Parser outputs remain drafts with field provenance and resource limits.

Acceptance: an actual client round trip, generated tool-schema snapshot, denied
unauthorized effects, no keys/PII leaking into summaries/logs, malicious document
text cannot change permissions, protocol errors and domain errors tested separately.
No `uvx`/PyPI release instruction until that exact artifact exists and is tested.

## F4: implement invoice/outbox behavior entirely offline

Create the invoice kind/pattern/subject registry, immutable revisions, calculation
traces, approval-bound enqueue, durable worker, append-only transmission events,
cap observations/reservations and simulator using a labelled TEST profile. Do not
label self-generated crypto vectors or simulator error codes as official.

Acceptance: timeouts/duplicate sends/crashes recover without allocating a second
invoice; idempotency conflicts fail; unknown outcomes require inquiry; stale cap
values cannot masquerade as official; cancellation semantics are tested. No real
government endpoint or production credential is present in fixtures/CI.

## F5: unlock only the native protocol integration

Prerequisites: retrievable authoritative technical document, version and hash,
independently sourced TaxID/canonicalization/signature/encryption vectors, verified
endpoint/authentication/error/status mappings and documented testing environment.
Review the former epoch, reference-invoice restrictions and FX/export distinction;
none is inherited as fact. Isolate native signing/transport behind existing ports.

Acceptance: independent vectors pass, protocol-profile changes invalidate affected
approvals, test integration evidence recorded without real invoice side effects.
A working simulator alone does not close F5. If evidence is unavailable, F5 stays
blocked while F0-F4 implementation proceeds.

## F6: reviewed tax features and production release

For each fiscal feature separately, verify effective date, primary source, taxpayer
scope and eligible evidence; activate one immutable rule version only after review.
Tax positions, cap decisions, exemptions and credits need domain-specific tests.
No all-or-nothing activation of the original research report is permitted.

Release requires owner approval, legal/accounting review for enabled features,
auth/security review, restore exercise, key lifecycle validation, reconciled test
results and a documented rollback/disable plan. Approval to merge architecture is
not approval to send a production invoice.

## Initial coding assignment

Implement F0 followed by F1 and F2. Deliver one small vertical slice: prepare an
IRR journal, show the human its exact preview, approve out of band, post once,
produce trial balance and demonstrate reversal. Keep fiscal sending and automatic
tax treatment disabled. Report commit SHA, changed contracts, executed commands,
results and remaining gates at each milestone.

Run the reference contract tests now:

```sh
python -m unittest discover -s tests/architecture -p 'test_*.py' -v
```

These tests are not the full future application suite. The existing legacy CI stays
separate so its green status cannot certify unimplemented ledger/Moadian features.
