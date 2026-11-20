# ADR-0002: Tryton as the accounting engine

- **Status:** Accepted
- **Date:** 2026-11-16

## Context

`docs/ARCHITECTURE.md` v1.4.0-FROZEN (HYA-A1-FINAL, 2026-09-20) includes a
production-grade native double-entry ledger as an R0 goal (§3.1), with
HesabYar-owned voucher/journal tables (§8, §19) and a native-ledger Stage F1.

The design & engineering specification (rev 1.0-draft) assigns the canonical
accounting ledger to Tryton: "Tryton will own accounting transactions and
their lifecycle. HesabYar will provide the Iranian-specific rules,
deterministic validation, policy versioning, API orchestration, integration
adapters, report enrichment and MCP access to approved information." Its §1.1
states these two positions conflict and "must not be silently mixed", and
requires an ADR plus an architecture update to v1.5.0 before implementation
starts.

The current repository state makes this decision cheap to record now: the only
Alembic migration creates `tenants` and `actors`; no ledger tables have been
built yet, so no built ledger is being discarded — only the documented
direction changes.

The specification's fixed decisions (§0) that bear on this ADR:

1. "Tryton is the only active general-ledger authority in the chosen
   deployment mode. Do not post the same transaction independently into a
   second HesabYar ledger."
2. "HesabYar remains a modular monolith with Ports & Adapters. It owns
   Iranian policy and standards logic, not the canonical Tryton journal
   tables."
3. "The product will not be a fork of the entire Tryton ERP, and it will not
   maintain two competing general ledgers."

## Decision

1. **Tryton is the default accounting engine provider** and the only active
   general-ledger authority in the default deployment mode.
2. **HesabYar exposes an `AccountingEnginePort` and a Tryton adapter.**
   Domain code depends only on the engine-neutral port (spec §2.2); a
   `TrytonAccountingAdapter` is the production provider. Tryton-specific data
   mapping lives under infrastructure adapters, never in the domain. The exact
   port interface must be adapted to real Tryton operations, not implemented
   as a speculative mirror of Tryton's internal model.
3. **HesabYar does not create duplicate journal-entry tables in the default
   deployment.** Chart of accounts, journals and posted moves, fiscal years
   and accounting periods, customer/supplier invoices, payments and bank
   statements, fixed assets and depreciation, and analytical dimensions are
   Tryton-owned (spec §2 system-of-record table). HesabYar stores only
   external references to Tryton records in engine-mapping tables
   (`engine_connections`, `engine_companies`, `external_id_map`), never SQL
   foreign keys into Tryton's database (spec §7.1, §7.4).
4. **A native ledger adapter is mutually exclusive with the Tryton provider
   per installation.** A future `NativeLedgerAdapter` may exist only as a
   separately configured standalone mode; one installation selects exactly one
   provider for its primary books (spec §2.2). Introducing it requires its own
   ADR.
5. **The same financial event is never posted into both engines.** No dual
   posting, no shadow ledger, no second authoritative book for the same event.
6. **System-of-record split (spec §2).** Tryton owns accounting transactions
   and their lifecycle. HesabYar owns the Iranian accounting standards corpus,
   Iranian tax policy, Moadian protocol profiles and evidence, API keys and
   integration credentials, and AI/MCP access policy — and provides
   deterministic checks, workflow orchestration, and audit correlation over
   engine data.
7. **Two separate logical databases per installation (spec §7.1).** A Tryton
   database, owned and migrated by Tryton and its installed modules, and a
   HesabYar database, owned and migrated by HesabYar/Alembic. HesabYar code
   never writes directly to Tryton's database; all engine access goes through
   the adapter.
8. **`docs/ARCHITECTURE.md` is bumped to version 1.5.0** to record this
   decision. The 1.4.0 native-ledger R0 goal is explicitly superseded, with
   the 1.4.0 text preserved in git history and a version-history note in the
   document itself — not silently rewritten.

## Consequences

- The ledger package is refactored into engine-neutral contracts, commands,
  queries, and value objects (`domain/accounting/`, spec §1.2, §6.2). No
  `vouchers`, `voucher_lines`, `accounts`, `fiscal_years`, or other
  journal-entry tables appear in HesabYar Alembic migrations in the default
  deployment.
- Posting invariants (balanced entries, one-sided lines, open periods,
  approval workflow, posted immutability) remain HesabYar validation rules
  applied to drafts before delegation, but transactional enforcement lives in
  the Tryton database. HesabYar verifies postings via the adapter and
  reconciliation.
- Ledger invariant tests become `AccountingEnginePort` contract tests (test
  double in unit/property suites; a real Tryton instance in integration).
  Database-level enforcement tests for ledger invariants belong to the engine,
  not to HesabYar's schema.
- Deployment gains a Tryton server and a Tryton database. The Tryton server
  runs in its own runtime; it must not be forced to share HesabYar's Python
  environment (spec §6.1).
- Electronic commercial-books exports read from the Tryton ledger through the
  adapter; the export remains a separate versioned compliance boundary and
  must not mutate ledger postings.
- Tenant isolation for ledger objects is realized through the Tryton company
  mapped to each tenant (`engine_companies`); HesabYar-owned records keep
  their `tenant_id` scoping.
- A future native standalone mode remains possible but requires its own ADR
  and stays mutually exclusive with Tryton per installation.

## Non-decisions

This ADR does not decide the Tryton version or module set (Phase 0
environment work), the exact `AccountingEnginePort` method signatures (to be
adapted to real Tryton operations), or Sao/UI reuse (allowed only after an
explicit GPL compliance decision, spec §0, §4.3).

## References

- Design & engineering specification, rev 1.0-draft: §0 (fixed decisions),
  §1.1 (conflict + ADR requirements), §1.2 (kept assets), §2 (system-of-record
  rules), §2.2 (engine provider abstraction), §6.2/§6.3 (bounded contexts,
  dependency direction), §7.1/§7.4 (separate databases, HesabYar-owned table
  groups).
- `docs/ARCHITECTURE.md` v1.5.0 (this ADR's companion update); v1.4.0-FROZEN
  superseded 2026-11-16.
- ADR 0001 (versioned policy boundary): the ledger stays deterministic and
  independent from tax-policy and MCP frameworks — unchanged by this decision.
